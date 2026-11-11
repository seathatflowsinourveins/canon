# Second reader: rb62a
Mark: no
Blocker: 4. For an order this client never passed through `_accept` (open at start-up, or any order after a reconnect), a partial fill on the replacement that arrives before `replaced` now means no `OrderUpdated` is ever generated, and the replacement's later fill is dropped as an overfill. The starting tree generates `OrderUpdated` and the full fill for the same sequence.

## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1383 with :1618 and :1803. `_accept` seeds `_current_venue_id.setdefault(client_order_id, venue_order_id.value)` with the venue order ID of whichever update first reaches `_accept`. An order the node took over at start-up reconciliation, or any order after `_disconnect` clears `_accepted`, has never been through `_accept`. If the first update for it is the replacement's `partial_fill` or `fill`, the replacement's ID gets recorded as the order's current one. So the adoption check at :1618 (`_active_venue_id(order) != str(alpaca_order.id)`) is False, no `OrderUpdated` is generated, and the fill is applied under the old terms (defect 4 again). Then when `replaced` arrives, `_replaced` takes the early return at :1803 (`self._active_venue_id(order) == final_id`) and generates no `OrderUpdated` at all. The node's order never takes the replacement's quantity or price, and every later fill beyond the old quantity is dropped as an overfill.
   Sequence (scratch test `test_reconciled_order_partial_fill_on_replacement_before_replaced` in packages/canon-alpaca/tests/test_scratch_second_reader.py; it uses only the suite's own stand-in):
   - Alpaca holds an open limit buy of 2 at 150 when the node starts, and start-up reconciliation loads it.
   - `_modify_order` sets qty 5, and the answer is silent.
   - `partial_fill` 1 arrives on the replacement.
   - `replaced` arrives for the old order.
   - `fill` 4 arrives on the replacement (filled_qty 5).
   Patched result: events `[OrderInitialized, OrderAccepted, OrderFilled]`, quantity 2, filled 1, PARTIALLY_FILLED, and NautilusTrader logs "Order overfill rejected ... last_qty=4, quantity=2".
   Starting tree, same sequence: `[OrderInitialized, OrderAccepted, OrderFilled, OrderUpdated, OrderFilled]`, quantity 5, filled 5, FILLED.
   Modifying a working order after a restart is ordinary use, and the order of events is the one defect 4 names. The suite does not cover it, because every modify test submits its order in-session, so `_accept` has already seeded the old ID. (severity: blocker)
2. Same root cause, test `test_reconciled_order_fill_on_replacement_before_replaced`. For a reconciled order, a full `fill` (qty 5) on the replacement before `replaced` is still applied under the old terms and dropped as an overfill. So defect 4 is not removed on that path. The starting tree behaves the same here, so on its own this would be a fix finding. Finding 1 is where the change makes things worse. (severity: fix)
3. execution.py:1621. A `fill` whose `_fill` fails (for example, no `execution_id`) no longer adds the order to `_finished`. The starting tree did, through `TERMINAL_EVENTS`. This change in behaviour is not in the statement and not in the report. (severity: fix)
4. execution.py:765-780 and the call sites (for example :1251, :1510-1538). The change fixes defect 9 for `_answers` but adds nine collections that only grow for the session: `_filled`, `_current_venue_id`, `_replace_parent`, `_claimed_replaces`, `_settled_replacements`, `_stream_rejected_calls`, `_superseded`, `_adopted`, `_rejected_replace_count`. Unknown-outcome modifies also leave `_modifying` and `_modify_parent` entries behind. Each one is a small string, but this is the same kind of build-up defect 9 describes, and the dedupe resting on a counter (`_rejected_replace_count`) is heuristic. A review would ask for this state to be pruned or cut down. (severity: fix)

## Per-defect check
1. Removed. `_order_report` now skips only a REJECTED replacement (execution.py:2156). The filled replacement is reported when `replaced` was missed.
2. Removed. `_stream_rejected_calls` and `_rejected_replace_count` give one `OrderModifyRejected` whichever path comes first (`_replace_refused`, `_on_replace_rejected`).
3. Removed. `_replaced` returns when the old ID is already in `_superseded` (:1756).
4. Removed only for orders this client `_accept`ed on the current connection. Not removed for an order reconciled at start-up or after a reconnect, and on that path the change also loses the later `OrderUpdated` (findings 1 and 2).
5. Removed. A late `canceled` or `expired` is dropped when `_is_superseded` holds.
6. Removed. `pending_replace` is in OPEN_STATUSES and maps to PARTIALLY_FILLED when the order has fills (:312, :619).
7. Removed. `_replaced` follows `replaced_by` up to MAX_REPLACE_CHAIN, and a `pending_replace` replacement now maps.
8. Removed. `replaces_as_asked` compares omitted fields with the replaced order, and `_claimed_replaces` keeps an earlier modify's replacement from counting as a retry.
9. Removed for `_answers`: a late answer is not stored once the replacement is settled, and `_replaced` pops in a `finally`. New per-session sets are added instead (finding 4).
10. Removed. Both replace-rejection branches now run before the `_finished` return, settle `_modifying` and log "which had filled first; the fill is the outcome".

## What I ran
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests --deselect <scratch>`: 247 passed, 19 skipped. That covers the 11 fail_to_pass tests and the worker's new edge tests.
- Scratch test file packages/canon-alpaca/tests/test_scratch_second_reader.py, three tests, run on the patched tree:
  - fill before `replaced`, reconciled order: FAILED (overfill, no OrderUpdated).
  - `replaced` then fill: passed.
  - partial fill before `replaced`: FAILED (no OrderUpdated, filled 1 of 5).
- Same scratch tests on the starting tree's execution.py, swapped in temporarily and restored, checked with `cmp`:
  - partial fill before `replaced`: passed (OrderUpdated, filled 5).
  - fill before `replaced`: also fails there (pre-existing).
- Checked that NautilusTrader 2.0.0rc8's `_libnautilus` binary contains the string "Unhandled order status", which the report cites. I did not read the code around it.
- grade.json `flags` is empty and `restored` is empty. No test files were modified (git status: the only addition is the new edge test file).

## Reread (settling the first reader's land)
Final mark: no. Blocker: 4. Stands. The sequence has no reconnect: the order is loaded once by start-up reconciliation and every later step runs on one connected client, so it is not the shared reconnect class. The `_accept` seeding (execution.py:1383) and `_replaced`'s early return (:1803) are both new in this change; the starting tree ends filled 5 of 5. "A real restart is reconciled from the replacement's own report" assumes the replace came before a restart; here it comes after. Whether NautilusTrader's in-session reconciliation would later correct the order is UNVERIFIED.
