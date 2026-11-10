# Review of 4afa6a7 (`packages/canon-alpaca/`): cancel-all by ID

Reviewed: `git diff 4afa6a7^ 4afa6a7 -- packages/canon-alpaca/`, read against NautilusTrader's source at
the pinned commit `d53849b2` (v2.0.0rc8): `crates/trading/src/strategy/mod.rs`,
`crates/execution/src/engine/mod.rs`, `crates/adapters/kraken/src/execution/spot.rs`,
`crates/testkit/src/testers/exec/strategy.rs`, `crates/model/src/orders/mod.rs`,
`docs/concepts/execution/policies.md`. I also read the installed `nautilus_trader` stubs and alpaca-py 0.45.0
in `.venv`. The upstream source came from a local checkout at that SHA, read with `git show HEAD:<path>`.

Offline runs:

- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py packages/canon-alpaca/tests/test_upstream_contract.py -k "cancel or logged or batch"`: 18 passed.
- Two probe tests of my own, kept outside the checkout in the session scratchpad
  (`.../scratchpad/probe/`). They load this package's `conftest.py` and `test_execution_client.py` by path.
  The checkout is unchanged (`git status` is clean; HEAD is still `4afa6a7`).

I ran no paper test.

## Defects, most severe first

### 1. Cancel-all skips an order left `PENDING_CANCEL` by a failed cancel, so nothing in the node can cancel it

**Where:** `packages/canon-alpaca/src/canon_alpaca/execution.py`, `AlpacaExecutionClient._cancel_all_orders`
(the `order.status == OrderStatus.PENDING_CANCEL: continue` check, line 738). The same claim appears in its
docstring ("An order already `PENDING_CANCEL` has a cancel in flight and is left to it", line 722), in the
header's known limits ("skipping an order already pending cancel"), and in `packages/canon-alpaca/README.md`
line 201.

**What goes wrong:** the check assumes `PENDING_CANCEL` means a cancel is still on its way. That is not
always so:

- Upstream's `Strategy::cancel_order` marks the order `PENDING_CANCEL` in the cache before it sends
  `CancelOrder` (`mark_order_pending_cancel`, `crates/trading/src/strategy/mod.rs` at `d53849b2`).
- If that cancel gets no definitive answer, `_cancel` logs "the order is left as it is" and generates no
  event. Examples are an `OSError` such as a connection reset or timeout, an HTTP status other than 422, or
  the `ValueError`/`TypeError`/`RuntimeError` now caught by `_cancel_logged`.
- So the node keeps the order `PENDING_CANCEL` while Alpaca still has it working.
- From then on, `Strategy.cancel_order` refuses it, because `mark_order_pending_cancel` returns false for a
  pending-cancel order. `cancel_all_orders(strategy_only=True)` goes through the same `cancel_order` and
  refuses it too.
- `cancel_all_orders(strategy_only=False)` reaches this hook, and the hook skips it on purpose.

That last route is the only one left that could retry the cancel, and it is closed. It is also the command
upstream's `ExecTester` sends on stop, so a strategy stopping after one lost cancel leaves the order working
at Alpaca.

The skip adds nothing for correctness. If a cancel really is in flight, a second cancel is answered 422, and
`_cancel_refused` already sees `pending_cancel`/`canceled` and generates nothing. Upstream's Kraken spot
adapter, which the header cites as doing "the same way", has no such skip: it chains `orders_open` and
`orders_inflight` and cancels every match (`crates/adapters/kraken/src/execution/spot.rs`, `cancel_all_orders`).

**Shown by:** probe test `test_pending_cancel_after_failed_cancel_is_skipped_by_cancel_all`. The setup is
the stand-in venue with `script="accept"` and `cancel_error = ConnectionError("connection reset")`, with a
strategy that places one limit buy and calls `cancel_order` on `OrderAccepted`. The steps:

1. The one cancel fails and is logged: "Alpaca cancel for O-…-1 got no answer (ConnectionError('connection
   reset')); the order is left as it is".
2. I cleared `cancel_error`, so the stand-in would now cancel, and waited 9 s.
3. The order was still `PENDING_CANCEL` in the node's cache. NautilusTrader's default in-flight checks had
   not resolved it in that time.
4. I awaited `client._cancel_all_orders(SimpleNamespace(instrument_id=AAPL, order_side=None))` on the node's
   loop, as `test_cancel_all_with_no_matching_order_sends_nothing` does.

Result: `venue.trading.cancels` still held only the first ID. The node's order stayed `PENDING_CANCEL`
(logged at dispose as "Residual LimitOrder(... status=PENDING_CANCEL ...)"), and the stand-in's order stayed
`new`.

I did not test whether a longer wait, or a non-default `LiveExecEngineConfig`, makes the in-flight check
resolve the order later.

### 2. `aapl_left_as_found` says the paper lock keeps other sessions out, which holds only under `mise run paper:test`

**Where:** `packages/canon-alpaca/tests/test_paper_execution.py`, the docstring of `aapl_left_as_found`
(line 153): "The account held none at the start and the paper lock keeps other sessions out, so whatever is
found is this test's."

**What goes wrong:** the `lockf` lock is taken only by the `paper:test` task in `mise.toml`. The module's own
docstring says the tests run whenever `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are in the environment, so a
plain `uv run pytest` with the keys set runs them without the lock. In that case, the `finally` block's
claim that "whatever is found is this test's" is false. The block cancels every open AAPL order on the paper
account and closes any AAPL position, including ones another session opened while the test ran. It then
counts them as this test's leftovers and fails the test.

**Shown by:** reading `mise.toml` (`[tasks."paper:test"]`, the `lockf` line) beside the test's `skipif`,
which checks only the environment. Not run, since running it needs keys. Low severity: it is the paper
account only.

### 3. The paper test module's docstring says one `CancelAllOrders` cancels two orders; the test places one

**Where:** `packages/canon-alpaca/tests/test_paper_execution.py`, module docstring, line 2: "accepted and then
canceled, one by `CancelOrder` and two by one `CancelAllOrders`."

**What goes wrong:** `test_paper_cancel_all_on_stop_tc_e41` sets `enable_limit_buys=True` and
`enable_limit_sells=False`, passes `count=1`, and asserts `len(events) == 1`. Its own docstring, and the same
module docstring a few lines later, describe one limit buy. The package README also says "a limit buy
canceled by `CancelAllOrders` on stop". The "two" is false.

**Shown by:** reading the test.

### 4. The `run_exec_tester` docstring names a parameter that does not exist

**Where:** `packages/canon-alpaca/tests/test_paper_execution.py`, `run_exec_tester` docstring (line 235):
"until it has `orders` orders accepted".

**What goes wrong:** the parameter is `count`, and there is no `orders`. The sentence points a reader at the
wrong name. It is cosmetic. The same docstring area also has a stray indented line in
`test_paper_cancel_all_on_stop_tc_e41` ("which the client cancels order by / order."), which is formatting
only.

**Shown by:** reading the function signature.

## Checked and found sound

- **Upstream contract:**
  - With `strategy_only=False`, `Strategy::cancel_all_orders` sends one `CancelAllOrders`; with the default
    `True`, it cancels the strategy's own orders one by one.
  - The engine emits no `OrderPendingCancel` for cancel-all (`handle_cancel_all_orders`).
  - `OrderCancelRejected` is a valid transition from `SUBMITTED` and `ACCEPTED`.
  - The Python binding has no `generate_order_pending_cancel`.
  - `ClientCache.orders_open`/`orders_inflight` take `instrument_id` and `side`.
  - The base `_batch_cancel_orders` loops over `_cancel_order`.
- **`ValueError` path:** alpaca-py's `cancel_order_by_id` can raise `ValueError` (`validate_uuid_id_param`).
  A JSON decode error from `requests` is an `OSError`, which `_cancel` already handles.
- **`logged()` guard:** `test_logged_waits_for_lines_still_queued_after_a_run` really does guard `logged()`.
  With `logging_sync_to_disk` patched to a no-op it failed in 3 of 3 runs, and with it in place it passed.
- **Offline cancel-all tests:** I found no way for them to pass for the wrong reason. The side filter test
  would catch a second cancel, because `until_idle` waits for the REST lock that a second cancel would hold.

## Not verified

- No paper test was run, so the observations quoted from Alpaca are unchecked: the 403 on the TC-E41 limit
  sell, and "no `OrderPendingCancel` (observed)".
- That `add_builtin_strategy("ExecTester", …)` yields the strategy ID `ExecTester-000`.
- Whether NautilusTrader's in-flight check would eventually resolve the stuck order in defect 1 after more
  than 9 s, or under another engine config.
