# Review of `4afa6a7c62f0f186761a402cec9df5fb5374f78a` in `packages/canon-alpaca/`

Scope: `git diff 4afa6a7c62f0f186761a402cec9df5fb5374f78a^ 4afa6a7c62f0f186761a402cec9df5fb5374f78a -- packages/canon-alpaca/`
(`execution.py`, `tests/conftest.py`, `tests/test_execution_client.py`,
`tests/test_paper_execution.py`, `tests/test_upstream_contract.py`, package `README.md`).
HEAD was verified to be that commit. No file in the checkout was edited (`git status` clean before and after).

Verification run at HEAD (offline only; no paper test, no key, no network beyond the locked `uv` environment):

- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests -q` → **166 passed, 3 skipped**.
- `ruff check packages/canon-alpaca/` and `ty check` on the changed files → clean.
- Additional probes were run in a `/tmp` copy of the test harness (checkout untouched) for paths the
  committed tests do not exercise: a `SUBMITTED`/in-flight order under cancel-all, a `PENDING_CANCEL`
  order under cancel-all, an HTTP-500 cancel refusal followed by a good cancel, a batch cancel sent
  through a real node, and the node-cache state of the reconciled external order. All behaved as the
  new code intends, so the items below are, unless stated otherwise, defects in the *tests and
  statements* the commit adds, not in `_cancel_all_orders`'s current behaviour on those paths.

Defects, most severe first:

## 1. Paper-test cleanup can abort halfway, mask the real result, and leave the shared paper account dirty

- **File/function:** [test_paper_execution.py](<checkout>/packages/canon-alpaca/tests/test_paper_execution.py:149) `aapl_left_as_found`, the `finally` block at lines 171–177 (used by both paper tests).
- **What goes wrong:** The cleanup the docstring promises — "afterwards, whatever happened, cancel
  every open AAPL order and close any AAPL position" — is two unguarded calls in sequence:
  `trading.cancel_order_by_id(order.id)` for each order returned by `open_aapl_orders(trading)`, then
  `trading.close_position("AAPL")`. Neither is wrapped, and there is no per-order isolation. If the
  first (or any) cancel raises, the `finally` raises, the remaining orders are never attempted, the
  position is never closed, and the cleanup exception replaces whatever the test itself was doing —
  including masking a genuine assertion/node failure inside the `with` body with an unrelated
  `APIError` traceback. This is the one place in the commit whose failure mode *creates* the hazard
  the commit's own safety text says it prevents (an order/position left open on the single shared
  paper account, ready to make the next run refuse to start or, worse, be touched by a later
  cancel-all).
- **Showing sequence:** Account is clean at entry; the test's own order fills (or is canceled by a
  third party / expires) in the window between `open_aapl_orders(trading)` listing it as OPEN and
  `cancel_order_by_id(order.id)` reaching Alpaca. Alpaca's documented answer for cancelling an order
  that is no longer cancelable is **422** (noted in the commit's own
  [execution.py header](<checkout>/packages/canon-alpaca/src/canon_alpaca/execution.py:20)).
  That `APIError` propagates straight out of the `finally`: `cleaned["positions"]` is never reached,
  a long AAPL position opened by the fill is left open, and the test reports the 422 instead of its
  own outcome. Shown by reading lines 171–177 against that documented 422 path (paper tests were not
  run — no key, per instructions), so the *abort-and-mask* structure is certain from the code; how
  often Alpaca hits the race is UNVERIFIED.

## 2. Same cleanup can fail a paper test that actually passed (order double-cancelled via REST)

- **File/function:** same block, [test_paper_execution.py](<checkout>/packages/canon-alpaca/tests/test_paper_execution.py:171) lines 171–174, together with the assertions at lines 192 and 225 (`assert cleaned == {"orders": 0, "positions": 0}`).
- **What goes wrong:** The test's pass condition is "the cleanup found nothing to do". The cleanup
  decides that from a fresh REST `get_orders(OPEN)` listing taken immediately after the node has
  stopped/disposed. The node's own cancel is confirmed by the *trade-updates stream*
  (`OrderCanceled`), which is a different channel from the REST order listing; nothing in the test
  waits for the REST view to agree with the stream view. If the REST listing still shows the just-
  cancelled order as open, the cleanup cancels it a second time. Two outcomes, both wrong for a
  test whose adapter behaviour was correct: (a) the second cancel is answered 422 and the test fails
  with that `APIError` out of the `finally` (see 1); or (b) the second cancel is accepted, `cleaned["orders"]`
  becomes 1, and lines 192/225 fail a test in which every order the test placed was in fact cancelled
  by the node.
- **Showing sequence:** Node cancels its AAPL order, stream delivers `canceled`, strategy sees
  `OrderCanceled`, node is disposed; REST `GET /v2/orders?status=open` still returns the order
  (pending-cancel/open during Alpaca's propagation); cleanup issues a second
  `cancel_order_by_id`. This is a **suspected** defect: the channel split is visible in the code
  (stream-confirmed cancel in `AcceptThenCancel`/`ExecTester` vs REST re-listing in
  `open_aapl_orders`), but Alpaca's stream-vs-REST ordering was not observed this session (no paper
  run permitted), so I am unsure the lag is real in practice. The 422-masking consequence in 1 does
  not depend on this lag.

## 3. `run_exec_tester` hides its own timeout, so TC-E41 can pass without the accepted-then-stop sequence it claims to drive

- **File/function:** [test_paper_execution.py](<checkout>/packages/canon-alpaca/tests/test_paper_execution.py:234) `run_exec_tester` (lines 259–273), called by `test_paper_cancel_all_on_stop_tc_e41` (lines 203–231).
- **What goes wrong:** The docstring promises it runs "until it has `orders` orders accepted, then
  stop it" (the parameter is in fact named `count`, not `orders`). The loop gives up silently after
  the 60 s deadline: it falls into `finally`, stops the node anyway, and returns whatever events
  exist, with no flag that the deadline fired. The caller then asserts only `len(events) == 1` and
  that each order's *last* event is `OrderCanceled`. A test can therefore pass through a path the
  test never intended: the order was submitted but never observed `ACCEPTED` within 60 s; stopping
  the node triggers `ExecTester`'s stop-time `CancelAllOrders`, which (correctly, per the new
  client) also selects the still-`SUBMITTED` in-flight order and cancels it; the returned last event
  is `OrderCanceled` and both assertions pass. The test then reports TC-E41 green although the
  accepted-order precondition it waited for never happened, i.e. it passes for the wrong reason in
  exactly the degraded-timing situation a deadline exists to catch. (If no order was ever placed it
  does still fail, on `len(events)`.)
- **Shown by:** reading lines 263–273 (`while … time() < deadline` → unconditional stop in
  `finally` → return `events`) together with lines 225–231; and by the `/tmp` probe in which a
  cancel-all issued while the submit answer was deliberately held back (`submit_delay`) still ended
  `OrderCanceled` via the in-flight path, confirming that "canceled on stop" does not imply
  "was accepted first". Paper path itself UNVERIFIED (not run).

## 4. The batch-cancel contract test never touches the Alpaca client, so it cannot show what its docstring claims

- **File/function:** [test_upstream_contract.py](<checkout>/packages/canon-alpaca/tests/test_upstream_contract.py:95) `test_batch_cancel_default_calls_the_single_cancel_hook_in_order` (lines 95–111).
- **What goes wrong:** The docstring says this pins "The guide's default, **kept by the client**".
  The body calls the *base class's* implementation directly —
  `ExecutionClient._batch_cancel_orders(Recorder(), batch)` — with a stand-in `Recorder`, and never
  references `AlpacaExecutionClient`. It would pass unchanged if the Alpaca client overrode
  `_batch_cancel_orders` to do anything else (call Alpaca's account-wide Delete All, drop the list,
  reverse it): no other committed offline test sends a batch cancel through the Alpaca client at
  all. The related addition of `"_batch_cancel_orders"` to `EXECUTION_HOOKS` (lines 45–56) has the
  same target problem: `test_execution_hook_exists` checks `ExecutionClient`, the base class, which
  already had the hook before this commit. Current behaviour is correct — I verified in a `/tmp`
  probe that `Strategy.cancel_orders([...])` through a real node cancels both Alpaca orders by ID —
  but these two tests provide no evidence for it and can pass while the claimed behaviour is broken.
- **Shown by:** the test bodies as read above, plus the `/tmp` batch probe just described (which is
  the test the commit does not contain).

## 5. The cancel-all test's reconciled-order assertion reads the stand-in's own mutated state, not the node's

- **File/function:** [test_execution_client.py](<checkout>/packages/canon-alpaca/tests/test_execution_client.py:603) `test_cancel_all_cancels_each_open_order_of_the_instrument_by_id`, final assertion at line 631; stand-in at [conftest.py](<checkout>/packages/canon-alpaca/tests/conftest.py:424) `FakeOrdersClient.cancel_order_by_id` (lines 424–440) and `_send` (lines 409–422).
- **What goes wrong:** For the one order that matters most to the test's claim — the order the node
  only knows from start-up reconciliation, owned by no strategy — the only node-side evidence
  asserted is that its venue ID appears in `venue.trading.cancels`. The "it ended canceled" check,
  `get_order_by_client_id(external).status.value == "canceled"`, reads the fake's dict, which the
  fake itself rewrites to `canceled` *before* it sends the stream update (`_send` mutates `placed`
  then queues the update). If the adapter or engine dropped the `canceled` update for a
  strategy-less reconciled order, the node's cache would hold the order open forever and this test
  would still pass. The two strategy-owned orders are properly checked through strategy events;
  the external one is not checked through the cache (`node.cache.order(...)`) at all.
- **Shown by:** reading conftest lines 409–440 against line 631; and a `/tmp` probe repeating the
  scenario and asserting `cache.order(external)` directly, which showed `OrderStatus.CANCELED` /
  `is_closed` at HEAD — so today's behaviour is right, but the committed test would not catch its
  regression.

## 6. The paper-test module docstring states an order count the tests do not place

- **File/line:** [test_paper_execution.py](<checkout>/packages/canon-alpaca/tests/test_paper_execution.py:1) lines 1–2: "limit orders … accepted and then canceled, **one by `CancelOrder` and two by one `CancelAllOrders`**".
- **What goes wrong:** No reading of the file places two orders under the cancel-all. The first
  test places exactly one order (`AcceptThenCancel.on_start`, lines 80–88); the TC-E41 test places
  exactly one (`ExecTesterConfig(order_qty=1, enable_limit_buys=True, enable_limit_sells=False)`,
  `count=1`, lines 212–223). "Two by one `CancelAllOrders`" is therefore a false statement in the
  record a future reader will use to judge what was proven on paper. The file's other count-bearing
  docstring has the same carelessness in miniature: `run_exec_tester` (line 235) says "until it has
  `orders` orders accepted" for a parameter named `count`.
- **Shown by:** counting the placing code in the same file, as cited; corroborated by the package
  README table row for this file, which (correctly) describes one order per test.

## 7. Unsure / coverage gap: the two headline cancel-all behaviours have no committed offline test

- **File/function:** [execution.py](<checkout>/packages/canon-alpaca/src/canon_alpaca/execution.py:714) `_cancel_all_orders` (lines 714–740); tests in [test_execution_client.py](<checkout>/packages/canon-alpaca/tests/test_execution_client.py:603) lines 603–689.
- **What is missing:** The docstring and package README sell two specific behaviours — in-flight
  (`SUBMITTED`) orders are selected "because Alpaca may hold an order the node still has as
  `SUBMITTED`", and an order already `PENDING_CANCEL` is skipped — but none of the five new tests
  stages either state: the `CancelAll` strategy only fires once every placed order is `ACCEPTED`
  (line 576), and no test puts an order into `PENDING_CANCEL` before the cancel-all. If either
  branch (the `orders_inflight` half of line 728, or the skip at line 738) were deleted, the whole
  committed suite would stay green; only the signature pin in
  [test_upstream_contract.py](<checkout>/packages/canon-alpaca/tests/test_upstream_contract.py:85) would remain, and it checks upstream's method signatures, not this selection. I am listing this as a defect of the test set, not of the code:
  my `/tmp` probes staged both states at HEAD (cancel-all fired on `OrderSubmitted`; cancel-all
  after a taken-but-refused single cancel) and the client behaved exactly as documented
  (one cancel sent in the first case, no second cancel in the second), so the behaviour is
  presently correct but unguarded.
- **Status:** behaviour verified by probe this session; absence from the committed tests verified
  by reading lines 541–689.

## Not defects (checked and cleared)

- `_cancel_all_orders`'s instrument/side selection, de-duplication across `orders_open`/
  `orders_inflight`, re-read-and-skip at each order's turn, 422→`OrderCancelRejected` (with the
  canceled/pending-cancel exception via `_cancel_refused`), non-422/500 and unparsable (`ValueError`)
  failures leaving the order untouched while the rest proceed, and the inherited
  `_batch_cancel_orders` through a real node: all exercised (committed tests or `/tmp` probes) and
  correct at HEAD.
- The `order_side=None` claim: inside the hook the command's `order_side` is genuinely `None` in
  Python (probed), despite the native log printing `NO_ORDER_SIDE`, and the no-side tests cancel
  both BUY and SELL orders.
- Paper-test helper APIs (`GetOrdersRequest(status=OPEN, symbols=["AAPL"])`,
  `TradingClient.get_open_position`/`close_position`/`cancel_order_by_id`, `APIError.status_code`)
  were checked against the installed alpaca-py 0.45.0 signatures; `TOB_OFFSET_TICKS = 500_000` is
  $50 at the adapter's 0.0001 price increment.
