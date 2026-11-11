# Second reader: ra205
Mark: no
Blocker: 4. The new `_modifying` gate drops a refused modify's `OrderModifyRejected` whenever another outcome for the same order settled it first. That covers two modifies in flight, and also the first modify's `replaced` landing while the second's refusal is still being looked up. The original code rejected both cases and the suite does not cover them.

## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1113, 1324-1343 (`_modifying`, `_settle_modify`, `_modify_rejected_once`). Severity: blocker.
   - `_modifying` is keyed by the venue order ID being replaced, so a second modify of the same order overwrites the first one's entry.
   - `_settle_modify` removes every entry for the node's client order ID, and `_apply_replacement` (line 1535) and every rejection call it.
   - `_modify_rejected_once` returns False and generates nothing when the order is no longer in the map, and it logs nothing either. So whichever outcome lands first for any modify of the order silences the refusal of every other modify of it that is still in flight.
   - Shown with scratch tests that use the suite's own stand-ins, run against the patched and the original `execution.py`. The tests are kept at `second-reader-probe.py.txt`, beside this sheet.
     - (a) Defect 8's own case, in a different order of events. Modify 1 (qty 5) is answered under the "silent" script. Modify 2 (price 151, qty unchanged) is sent and Alpaca refuses it 422 "order already replaced". While its lookup is in progress, modify 1's `replaced` arrives and is applied. Patched: `['OrderSubmitted','OrderAccepted','OrderPendingUpdate','OrderUpdated']`, with no `OrderModifyRejected` and no log line. Original: `[... 'OrderUpdated','OrderModifyRejected']`.
     - (b) Two modifies of an accepted order are sent at once and both are refused 422 "order cannot be replaced". Patched: one `OrderModifyRejected`. Original: two.
   - So defect 8 ("a second modify ... refused by Alpaca ... gets no `OrderModifyRejected`") is removed only for the order of events the named test uses. In (a) the defect is still there, and in (b) the change creates a new instance of it. Both drops are silent, which also falls under rule 5.
2. packages/canon-alpaca/src/canon_alpaca/execution.py:1188-1190 (`replaces_as_asked(replacement, request, order.quantity)`). Severity: fix. `order` is the node's cached order object, which an `OrderUpdated` changes in place. The docstring says "the node's quantity when the modify was sent", but the code reads the quantity at the time of the refusal. If modify 1's `replaced` is applied before modify 2's refusal is checked, modify 2's "unchanged" quantity becomes modify 1's new quantity. A second modify that only repeats the price then looks like alpaca-py's retry. The fix is to capture the quantity when the request is built.
3. packages/canon-alpaca/src/canon_alpaca/execution.py:1437-1443. Severity: fix. The branch for a rejected replacement on a finished order logs "which had filled first; the fill is the outcome" for any order in `_finished`, including one that was canceled or expired. The log then states an outcome that did not happen.
4. packages/canon-alpaca/src/canon_alpaca/execution.py:1485-1491, against the report's "or that names a superseded venue ID". Severity: fix. The venue ID test applies only when the update is for the node's original order (`not is_replacement`). A late `canceled` for an intermediate replacement (B, after B was replaced by C) is caught only if Alpaca's order carries `replaced_by`. The report describes a broader check than the code makes.

## Per-defect check
1. Filled replacement lost when `replaced` was missed: removed. The `_order_report` guard (around line 1917) now drops only a REJECTED replacement.
2. Refused replace rejected twice: removed for a single modify (`_modify_rejected_once`). See finding 1 for more than one modify.
3. `replaced` applied twice: removed (the `_replace_applied` set, `_apply_replacement` around line 1520).
4. Replacement fill before `replaced`: removed. In `_apply` (around line 1501), a fill on an unapplied replacement goes through `_apply_replacement` before `_fill`.
5. Late cancel of a replaced order: removed for the original order, and for any order whose update carries `replaced_by` (line 1485). See finding 4.
6. `pending_replace` fails start-up: removed. `OPEN_STATUSES` maps it to ACCEPTED, and to PARTIALLY_FILLED when the order has fills (around line 616).
7. Replacement that has moved on: removed. `_replaced` walks `replaced_by` up to `MAX_REPLACE_CHAIN`, and `pending_replace` now maps.
8. Second modify's refusal taken for a retry: not removed. The named ordering is fixed by comparing effective terms. When the first modify's `replaced` lands before the second's refusal is checked, the second gets no `OrderModifyRejected` (finding 1a, plus finding 2).
9. Answers kept for the session: removed. `_apply_replacement` pops the answer, and `_modify_order` does not store an answer for a replacement already applied.
10. Rejected replacement after a fill never reached: removed. The branch now comes before the `_finished` return, settles the modify and logs.

## What I ran
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q` on the patched tree: 114 passed.
- I appended two scratch tests to a temporary copy of `test_execution_client.py` and ran them with `-k "while_first_replaced_lands or both_refused_in_flight" -s`:
  - with `execution.py` at HEAD (the original): 2 passed, and the events show `OrderModifyRejected` in each;
  - with the patched `execution.py`: 2 failed. The events are listed in finding 1.
- Afterwards the test file and `execution.py` were restored. `git diff` of tree2 matched `in/change.patch`, apart from the `index` lines.
- The grade's `flags` are empty, so blocker 1 does not apply. The change adds no dependency and uses no mechanism of its own in place of an upstream one, so blocker 2 does not apply.
