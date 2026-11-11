# First reader: ra5ef
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1110-1111 — the re-read comment in `_cancel_all_orders` still gives "or been sent a cancel of its own" as a reason to re-read, beside the condition that no longer skips a pending-cancel order. The change made the comment untrue, and a reader could restore the skip from it; verified by reading (severity: fix)
2. packages/canon-alpaca/src/canon_alpaca/execution.py:637-640 — the edited `order_status_report` docstring has a broken wrap ("rather than leaving a working" / "order unknown to the node"); cosmetic, folded into the same round; verified by reading (severity: fix)
## Review findings not taken
1. execution.py:1532 (multi-link chain reported under the middle link's ID): probed; true, but it follows the adapter's chain-root contract and the README sentence is about the direct replacement; not untrue.
2. execution.py:1486 (info log for an unholdable stream order, open-check fails): the node cannot hold the order; the mass-status failure on an open one is what the statement asks for.
3. execution.py:1113 (two concurrent cancels can both be rejected): requires a fill between two DELETEs; the second cancel is what defect 1 asks for.
4. ADR 0036:42 (upstream adapters claim not labelled UNVERIFIED): ADR 0036's own raw-fetched evidence (lines 35-38, 113-117) already records that only Coinbase skips; the claim restates the record, not a guess.
5. ADR 0036:139 (evidence still cites the pending-cancel skip sources): those lines are the dated history of the earlier reading, not a current claim.
6. execution.py:1485 (`_replacements` lookup skipped for an unholdable own ID): no code writes an unholdable string as a key for a node order; speculative.
7. execution.py:1489 (no test of the trade-update path): a coverage wish, not a defect.
8. README:289 (whitespace not named): whitespace is also refused (probed); the text says "empty or not ASCII", incomplete but not false about what it names.
## Per-defect check
1. Cancel-all PENDING_CANCEL: skip removed (execution.py:1113); a pending-cancel order is cancelled again. Removed.
2. Unholdable client order ID: `holdable_client_order_id` returns None; `_root_id` returns None for an own unholdable ID, so `order_status_report`'s existing try skips a closed one and fails an open one by name; the trace stops at an unholdable prior, so the replacement keeps its own ID. Probed whitespace closed order: skipped. Removed.
## What I ran
- Scratch probe (removed), same four scenarios as for the other runs of this case: whitespace closed order skipped; rejected replacement of an unholdable filled original -> start-up passes; two-link chain -> open order under the middle link's ID; open "" order replacing a holdable external order -> start-up fails naming it. 4 passed.
- Read the patch and the tree's ADR 0036 at HEAD for the upstream-adapter evidence.
