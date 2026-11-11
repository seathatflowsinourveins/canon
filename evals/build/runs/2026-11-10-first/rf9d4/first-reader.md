# First reader: rf9d4
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/tests/test_execution_unholdable_and_cancel_all.py:341 — `assert "Alpaca trade update new" not in text or "failed:" not in text` passes whenever either substring is absent, and misses the outer handler's "could not be applied" message, so the test does not check that the update is dropped without error, as its name and the report say; verified by reading (severity: fix)
2. packages/canon-alpaca/tests/test_execution_unholdable_and_cancel_all.py:50-200 — the new file copies the `venue` and `dispose_nodes` fixtures, `build_node`, `run`, `run_with_client`, `until_idle` and `logged` from test_execution_client.py (about 150 lines), with a second strategy class; two harnesses to keep in step with the client internals `until_idle` polls. A review round would move the cases into the existing file or a shared conftest; verified by reading (severity: fix)
## Review findings not taken
1. execution.py:1550 (no cache when root is None): only for a chain on which no ID at all is holdable; repeated REST walks in that exotic case, not a defect of this case.
2. execution.py:1114 (two concurrent cancels both rejected): needs a fill between two DELETEs; the second cancel is what defect 1 asks for.
3. ADR 0036:142 (upstream adapter claim unlabelled): ADR 0036's raw-fetched evidence (lines 35-38, 113-117) already records that only Coinbase skips; the line restates the record. The decision correction has a dated Evidence line.
4. execution.py:1541 (trace goes past an unholdable link): a design choice that keeps a node order reachable across an odd external link; the case test passes and the docs say "earliest holdable ID", which is what the code does (probed: two-link chain under the middle link's ID).
5. execution.py:1508 (empty-string guards are redundant): harmless defensive guards, nit.
6. execution.py:833 (key `_replacements` by Alpaca order ID): an altitude suggestion outside the defect.
7. execution.py:1259 (silent drop of an empty answer ID): Alpaca's replace answer carries an Alpaca-made ID; speculative.
## Per-defect check
1. Cancel-all PENDING_CANCEL: skip removed (execution.py:1115); stale comment corrected. Removed.
2. Unholdable client order ID: `holdable_client_order_id`; `_root_id` returns None when no holdable ID is on the chain, so `order_status_report`'s try (now building `ClientOrderId(order.client_order_id or "")` inside it) skips a closed one and fails an open one by name; a valid replacement keeps the earliest holdable ID. Probed whitespace closed order: skipped. Removed.
## What I ran
- Scratch probe (removed): whitespace closed order skipped; rejected replacement of an unholdable filled original -> start-up passes; two-link chain -> open order under the middle link's ID; open "" order replacing a holdable external order -> start-up passes, order held under the ancestor's ID. 4 passed.
- `ClientOrderId` probe: whitespace-only is refused, so the report's "empty, whitespace or not ASCII" is true.
- Read the new test file and the ADR diffs.
