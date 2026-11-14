# First reader: rf5bf
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1539-1540, with the docstring at 1497-1499. A root of None is not cached. An order whose own client order ID NautilusTrader cannot hold, and whose `replaces` chain holds no ID it can hold, is therefore traced again on every `_node_order_id` call, which runs once per trade update for it, at one `get_order_by_id` per link. Each lookup takes the client's REST lock (`_call`, 2141-2158), and `_consume_updates` (1452-1466) holds later updates behind it. The docstring still says "no chain is walked twice". Shown by probe P1. On the change, three calls returned None with 6 lookups, three `new` updates cost 6 more, and nothing was cached under the raw ID. On the starting code, each call raised `ValueError` with no lookup. The case's own fix caches the None result (read). (Review finding 5.) (severity: fix)
2. execution.py:1501-1502. The `_root_id` docstring says an order whose chain ends at an ID NautilusTrader cannot hold is "skipped when that is the ID it cannot hold". An open one fails start-up instead. Shown by `test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name` and by probe P2: an open order with a non-ASCII ID that replaced another such order fails start-up with "Alpaca order <id> (AAPL) is open with invalid string". (Review finding 7, first half.) (severity: fix)
3. packages/canon-alpaca/README.md:290-296 and execution.py:144-148. The new sentences state the rule by an order's own client order ID, but `_root_id` reports a replacement under the first holdable ID up its chain. Probe P6: an open order with an empty ID that replaced a holdable order does not fail start-up, and is reconciled as `outside-p`. Probe P3: a replacement with a holdable ID is not "reported under it" when a holdable order lies beyond the unholdable one it replaced; it is reconciled as `outside-h`. Both runs fail start-up on the starting code. In P6 the code does what the case's own fix does (read), so only the wording is wrong. (Not in the review.) (severity: fix)
4. execution.py:1109-1110. The comment on the re-read still gives "been sent a cancel of its own" as a reason for it, but such an order is no longer skipped (read; the condition is at 1112). The case's own fix keeps the same comment. (Review finding 3.) (severity: fix)
## Review findings not taken
1. execution.py:1540 (review 1: a root cached under a raw ID NautilusTrader cannot hold, such as "", is returned for an unrelated order with the same raw ID): the mechanism holds. Probe P4: on the change, an unrelated order with "" resolves to `outside-p`. But it needs two orders in one Alpaca account with the same client order ID, and no source shows that. Per ADR 0042's record, Alpaca's page gives a replacement's client order ID as "A unique identifier for the new order", and paper refused a repeated one with 422 "client_order_id must be unique". alpaca-py 0.45.0's `ReplaceOrderRequest.client_order_id` reads "A unique identifier for the order" (read this session). The cache is keyed by the raw Alpaca ID in the starting code too, and the case's own fix keys it the same way (read).
2. execution.py:1112 (review 2: a repeat cancel to an order that filled before its fill update arrived brings a spurious `OrderCancelRejected`): the statement asks for the cancel to be sent again, and the case's own fix sends the same repeat. Under `_cancel_refused` (1158-1193, unchanged) and ADR 0036's existing rule, a 422 whose lookup shows the order filled is `OrderCancelRejected`, and that cancel was in fact refused. The starting code gives the same sequence for an `ACCEPTED` order that fills just before cancel-all's cancel reaches Alpaca.
3. execution.py:1112 (review 4: a duplicate DELETE and a lookup per order whose cancel is still in flight): this is what the repeat the statement asks for costs, and the case's own fix makes the same requests. No source read shows the "common on_stop pattern" the review names, each order canceled and then `cancel_all_orders(strategy_only=False)`. The README (231-233) says that with the default, a strategy cancels its own orders one by one.
4. execution.py:1113 (review 6: a lost `canceled` update still leaves the order `PENDING_CANCEL`, since a 422 while Alpaca shows it canceled generates nothing): the statement's defect is a cancel with no answer while the order still works at Alpaca, and that one is removed. A lost stream update is a different event. The rule that such a 422 generates nothing is ADR 0036's, is unchanged here, and is kept by the case's own fix.
5. execution.py:1535-1538 (review 7, second half: the `MAX_REPLACE_CHAIN` warning says "traced to the order 16 links back" when the root is None or nearer): it needs a chain of more than 16 replaces with IDs NautilusTrader cannot hold among its links, and no source shows one. The warning is the starting code's.
6. docs/adr/0036-alpaca-adapter-order-management.md:41-43 (review 8: the claim that Kraken spot, Binance spot, Bybit and Interactive Brokers include every open order cites no file, line or pin, and Coinbase is dropped): the Evidence section already cites those adapters' cancel-all at the pin (lines 115-117), and the claim holds there. Read at `d53849b216e2`, the lock's commit, in the `scripts/canon src` cache:
   - Kraken spot takes `orders_open` and `orders_inflight`, filtered by side only (crates/adapters/kraken/src/execution/spot.rs:1629-1632).
   - Interactive Brokers takes `orders_open` (interactive_brokers/src/execution/core.rs:2294-2301).
   - Binance spot and Bybit take `orders_open` for a sided command (binance/src/spot/execution.rs:2128, bybit/src/execution.rs:2061).
   - None of the four filters by status, and `OrderStatus::is_open` includes `PendingCancel` (crates/model/src/enums.rs:1476-1485).
   - Only Coinbase filters it out (coinbase/src/execution.rs:1235-1251).

   The new text says nothing false about Coinbase.
7. packages/canon-alpaca/README.md:230 and 293 (review 9: re-wrapped lines cut short mid-sentence): Markdown joins source lines, so nothing rendered changes. The starting README already broke this bullet at "cancels every open order of the". `scripts/canon lint` is clean on the copy.
## Per-defect check
1. "Cancel-all skips an order that is already `PENDING_CANCEL` ... a later cancel-all does not cancel it again": removed. `test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer` fails on the starting tree and passes on the copy. The only skip left is a missing or closed order (execution.py:1111-1113).
2. "one closed order like that aborts the mass status and with it start-up reconciliation. A closed one should be skipped with a warning": removed. `test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped` (empty and non-ASCII) fails on the starting tree and passes on the copy. Probe P5: on the copy the node starts and logs "... is not reported: its client order ID, type, side, class, quantity or time in force is not in this slice", as the report says. The starting tree fails start-up.
3. "An open one cannot be represented, and start-up should fail with a reason that names it": removed. `test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name` fails on the starting tree and passes on the copy. Probe P2: the copy names the order even when its whole chain holds no ID NautilusTrader can hold. The starting tree fails start-up without naming it.
4. "A valid order that replaced one should still be reported, under its own ID": removed for the statement's case. `test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own` fails on the starting tree and passes on the copy. When a holdable order lies beyond the unholdable one, the replacement is reported, but under that order's ID (probe P3); see Declined to judge.
## Probes
The starting tree was made with `harness.py tree cancel-again-and-unholdable-id $S/rf5bf/first/base`. `diff -rq` shows it differs from the copy only in the three changed files, so the test files are the starting ones.

- The four named tests (`pytest packages/canon-alpaca/tests/test_execution_client.py -k "<the four>" -q -p no:cacheprovider`): 5 passed on the copy, 5 failed on the starting tree.
- The whole `test_execution_client.py` on the copy: 140 passed.
- Inline `python -I -c` against the copy's pinned binding: nautilus_trader 2.0.0rc8. The `ClientOrderId` doc is "Represents a valid client order ID (assigned by the Nautilus system)." It refuses "", " ", "\t" and "订单-1" with `ValueError` (empty, all whitespace, non-ASCII), and accepts "abc", " abc " and "a b". This matches the report and the new header.
- `scripts/canon lint` in the copy: "canon: lint clean", as the report says.
- `$S/rf5bf/first/probes/test_reader_probes.py`, with `probes/conftest.py` (loads the running tree's own conftest fixtures and failure hook) and `probes/pytest.ini`. Each tree runs it from its own root with `mise exec -- uv run --locked pytest $S/rf5bf/first/probes/test_reader_probes.py -q -p no:cacheprovider`. The probes record and do not assert: 6 passed on each tree. Results are in `probes/results-copy.txt` and `probes/results-base.txt`:
  - P1. An outside order "订单-3" replacing "订单-2", which replaced "订单-1". Three direct `_node_order_id` calls, then three `new` updates on the stream.
    - Change: None ×3, 6 lookups for the calls, 6 for the updates, not cached, 3 "does not hold" lines.
    - Starting code: `ValueError` ×3, 0 lookups, 3 "failed: ValueError" lines.
  - P2. Start-up with an open "订单-2" replacing "订单-1".
    - Change: RuntimeError "Failed to get mass status from ALPACA", and the order is named ("is open with invalid string").
    - Starting code: the same RuntimeError, with the order not named.
  - P3. Start-up with an open "alpaca-made-2" replacing "订单-1", which replaced "outside-h".
    - Change: the node starts, and the one open order is `outside-h` with the replacement's venue ID.
    - Starting code: RuntimeError (mass status).
  - P4. After start-up, `_root_id` for "" replacing "outside-p", then for an unrelated "".
    - Change: `outside-p` both times.
    - Starting code: `ValueError` both times.
  - P5. Start-up with a canceled order with "".
    - Change: the node starts and the warning is logged.
    - Starting code: RuntimeError (mass status).
  - P6. Start-up with an open "" replacing "outside-p".
    - Change: the node starts, and the one open order is `outside-p` with the replacement's venue ID.
    - Starting code: RuntimeError (mass status).
## Declined to judge
- Which ID a replacement should be reported under when a holdable order lies beyond the unholdable one it replaced. In P3 the change gives `outside-h`. Reading the case's own fix, it stops at the unholdable link and keeps the replacement's own ID. The statement's sentence and the named test cover only a replacement of an unholdable original. The change's choice matches what the live path maps through `_replaced` and `_adopt` (execution.py:1664-1715, read), and no source or test decides. Only the docs' wording is taken (finding 3).
- What Alpaca answers a repeated cancel, and the status its lookup shows while a cancel is pending: UNVERIFIED. No paper run in this task; the offline stand-in answers 204 and sends `canceled`.
- Whether Alpaca holds an order with an empty or all-whitespace client order ID at all: UNVERIFIED. The statement says an order made outside the node can carry one. Alpaca's pages were not fetched this session; the uniqueness of client order IDs is taken from ADR 0042's record and alpaca-py's docstring.
- The whole workspace suite: not re-run by this reader. grade.json and the report give 497 passed, 24 skipped.
