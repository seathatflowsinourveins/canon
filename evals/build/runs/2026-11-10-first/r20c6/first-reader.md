# First reader: r20c6
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. execution.py:1506 (pydantic/TypeError from an ancestor fetch escapes `_order_report`): pre-existing; the change did not widen or narrow that path, and the defect is about the ID conversion, which is now handled.
2. execution.py:1099 (second cancel of an order whose first cancel was answered): this is what defect 1 asks for; a 422 still goes through `_cancel_refused`, which generates nothing when Alpaca shows canceled or pending cancel.
3. execution.py:2125 (fill reports of a skipped closed order still emitted): the same holds for every closed order the slice already skips; the statement asks for exactly "skipped with a warning".
4. execution.py:2107 (rejected replacement of an unholdable original reported as an external REJECTED order): probed; start-up goes on and the cache holds one closed external REJECTED order. Harmless and no worse than any external rejected order; not a defect of this case.
5. execution.py:1546 (info log "reconciliation reports it" for an unholdable stream order): the node cannot hold such an order, so the "does not hold" path is the right one; wording nit only.
6. execution.py:1471 (a node order replaced with an unholdable Alpaca-made ID): speculative; Alpaca makes replacement IDs, observed ASCII.
7. execution.py:1520 (three try/except sites): style; not a correctness item.
8. execution.py:2125 (generic warning text): pre-existing message shape; the change made it include the new cause.
9. execution.py:1529 (trace stops at an unholdable middle link): needs an external replace with an unholdable ID in the middle of a node chain; speculative, and it was a start-up abort before.
## Per-defect check
1. Cancel-all PENDING_CANCEL: removed the skip (execution.py:1099); a pending-cancel order is cancelled again by ID; the stale "sent a cancel of its own" comment was corrected. Removed, not only tested.
2. Unholdable client order ID: `_root_id` returns None for an own ID `ClientOrderId` refuses and the converter's existing try applies the closed/open policy (closed skipped, open raises naming the order); an unholdable ancestor stops the trace with a warning, so a valid replacement is reported under its own ID. Probed whitespace "   " (also refused by `ClientOrderId`): skipped. Removed.
## What I ran
- `uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py`: 140 passed.
- Read `OrderStatus.__doc__` at the pin: PENDING_CANCEL is listed as open and in flight, as the ADR 0036 evidence line says (verified).
- `ClientOrderId` probe: "" and "   " and non-ASCII raise ValueError; " a" and "a b" are accepted.
- Scratch probe (removed): whitespace closed order skipped; rejected replacement of an unholdable filled original -> start-up passes, one external REJECTED order; two-link chain from an unholdable original -> open order under the middle link's ID; open "" order replacing a holdable external order -> start-up fails naming the order.
