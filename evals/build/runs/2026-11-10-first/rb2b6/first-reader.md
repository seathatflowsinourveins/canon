# First reader: rb2b6
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. execution.py:1536 (node order replaced externally with an unholdable ID, then replaced again): needs an external replace that supplies a non-ASCII or empty client order ID on a node order; speculative, and before the change it aborted start-up.
2. execution.py:1117 (second cancel while the first is in flight): what defect 1 asks for; a 422 goes through `_cancel_refused`, and ADR 0049 states the cost.
3. execution.py:1537 (open unholdable order replacing a holdable external order is reported under that order's ID): probed, true (start-up passes, order held as `dash-1`). It follows the adapter's chain-root contract for replacements; the README's "fails start-up" is broader than this exotic case, not a review-round item.
4. execution.py:689 (reason text does not name the field): the message is NautilusTrader's own ("invalid string for 'value'...") with the order ID, which is what the case asks for ("names it").
5. execution.py:1555 (info log path for an unholdable stream order): the node cannot hold such an order; wording only.
6. execution.py:1888 (an open unholdable order fails every mass status): the statement asks for exactly that for an open order.
7. ADR 0049:2 (accepted with second reader pending): cheap-to-reverse code change on a defect the owner confirmed; the second reader is the landing lane's step (ADR 0017), and the record labels what is UNVERIFIED.
8. ADR 0036:42 (decision edited in place, not marked superseded): the worker followed the rule by writing ADR 0049 and noting the partial supersession; a partial supersession is not a whole-record status change.
9. tests/test_client_order_ids.py:37 (SimpleNamespace stub): additional tests, not a defect; the case tests use the real node.
10. execution.py:1489 (double conversion): efficiency nit.
## Per-defect check
1. Cancel-all PENDING_CANCEL: skip removed (execution.py:1118); stale comment corrected. Removed.
2. Unholdable client order ID: `holdable_client_order_id`; `_root_id` returns None (not cached) when no holdable ID is reached, so the converter's existing try skips a closed order and fails an open one by name; an unholdable ancestor ends the trace, so the replacement keeps its own ID. Probed whitespace closed order: skipped. Removed.
## What I ran
- Scratch probe (removed): whitespace closed order skipped; rejected replacement of an unholdable filled original -> start-up passes; two-link chain -> open order under the middle link's ID; open "" order replacing a holdable external order -> start-up passes, order held under the ancestor's ID. 4 passed.
- Read ADR 0049, `.claude/rules/adr.md`, the new test file and the README diff.
