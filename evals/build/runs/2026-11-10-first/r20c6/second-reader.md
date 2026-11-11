# Second reader: r20c6
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.

Observations, not findings:
- packages/canon-alpaca/src/canon_alpaca/execution.py:1470-1473 — `_node_order_id` now returns None for an
  own ID `ClientOrderId` refuses, so a stream update for such an order gets the existing info line "which
  this node does not hold; reconciliation reports it" in place of the earlier error log. The node cannot
  hold such an order (its cache is keyed by `ClientOrderId`), so this is the same class as any external
  order; the "reconciliation reports it" wording was already inexact for external orders this slice cannot
  map. Not a new hidden failure.
- An open replacement made outside the node with an invalid own ID whose root is a node order would fail
  start-up rather than be reported under the root. The node never sends a client order ID on a replace
  (Alpaca makes one, observed), so this needs an outside actor to set an invalid one; it matches the
  statement's "an open one cannot be represented, and start-up should fail".
## Per-defect check
1. Cancel-all skips PENDING_CANCEL — removed: execution.py:1097-1101 drops the `PENDING_CANCEL` skip; a
   repeat cancel answered 422 is absorbed by the existing `_cancel_refused` (lookup shows canceled or
   pending_cancel, nothing generated), so the second cancel risks no spurious `OrderCancelRejected`.
2. Unholdable client order ID aborts start-up — removed: `_root_id` (1494-1497) returns None for an own ID
   `ClientOrderId` refuses, so the conversion happens inside `order_status_report`'s `except ValueError`
   (line 673 under the try at ~685): closed -> None and `_order_report` warns (2124-2128); open -> raises
   "Alpaca order <id> (<symbol>) is open with <reason>", which leaves `_order_status_reports` and fails
   mass status naming the order. An invalid ancestor ends the trace at the last valid ID with a warning
   (1519-1527), so the valid replacement is reported under its own ID (renamed is None when equal).
## What I ran
- `git -C tree2 diff` / `status`: four files changed (execution.py, README, ADR 0036, ADR 0042); no tests,
  deps or lock touched. grade.json flags: [] (nothing to examine for blocker 1).
- Read `_cancel_all_orders`, `_cancel`, `_cancel_refused`, `_node_order_id`, `_root_id`, `_apply`,
  `_order_status_reports`, `_order_report`, `order_status_report`; grepped every `ClientOrderId(`
  construction (673, 1471, 1496, 1520 only).
- `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_execution_client.py`:
  140 passed in 66.26s (includes the four fail_to_pass tests, five cases).
- Checked report claims against the code (None for an invalid own ID, ancestor stop with warning, open
  failure naming Alpaca ID/symbol/reason, warning text naming client order IDs, stream lookup handling,
  497 tests per grade.json): each holds.
