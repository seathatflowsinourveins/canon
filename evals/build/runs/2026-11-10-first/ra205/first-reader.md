# First reader: ra205
Mark: no
Blocker: 1 — defect 8 is not removed: when the first modify's `replaced` lands while the second modify's 422 is being looked up, the refusal is still taken for alpaca-py's retry and the second modify gets no OrderModifyRejected; the named test passes only in its own timing
## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1188 — `_replace_refused` calls `replaces_as_asked(replacement, request, order.quantity)` after two awaited lookups, reading the cached order's quantity at refusal time, while the `replaces_as_asked` docstring says `current_qty` is "the node's quantity when the modify was sent". Sequence: modify A (qty 2 to 5) answered, `replaced` not yet seen; modify B (price 150, quantity unchanged, so qty 2) is sent to the old order and refused 422 "order already replaced"; during B's lookup Alpaca's `replaced` for A arrives and is applied (OrderUpdated, qty now 5). The comparison then matches A's replacement (qty 5) and B is logged as "an earlier attempt reached it": events `[OrderSubmitted, OrderAccepted, OrderPendingUpdate, OrderUpdated]`, no OrderModifyRejected for B, which asked for qty 2. r1b0e, r9ac6 and rb62a reject B in the same probe. The settle-by-client-order-ID in `_settle_modify` (line 1324) also lets A's `replaced` clear B's entry. Verified by probe P9 (severity: blocker)
2. packages/canon-alpaca/src/canon_alpaca/execution.py:1437-1441 — when the old order fills during the PATCH and Alpaca then rejects the replacement, the branch settles `_modifying` but never pops the PATCH answer, and the order is then finished, so the answer stays for the session (defect 9's leak on this path). Probe P14b: `_answers` holds 1 entry at the end; r1b0e, r9ac6 and rb62a hold 0. Verified by probe (severity: fix)
## Review findings not taken
1. execution.py:1535 (a second modify's entry settled by the first's `replaced`): taken into finding 1.
2. execution.py:1189 (quantity read at refusal time): finding 1.
3. execution.py:1505 (fill on a skipped intermediate replacement rolls back terms): needs two replaces in flight and a fill on the middle one; not probed.
4. execution.py:1340 (stale stream rejection taken for the next modify): needs a stream rejection after the HTTP 422 and a new modify in between; not probed.
5. execution.py:1340 (untracked rejection dropped with no log): after a disconnect cleared state; reconnect class, see below.
6. execution.py:1156 (answers kept on other paths): case (a) is finding 2; (b) not probed.
7. execution.py:1486 (late cancel of an intermediate replacement): beyond the statement's old order; P1 (late expired of the old order) passes.
8-10. Extra REST read, growing `_replace_applied`, keying: efficiency and cleanups.
Also not counted: a repeated `replaced` after a client reconnect gives a second OrderUpdated (P13), as in other runs.
## Per-defect check
1. Replaced missed: reported (test; P6). Removed.
2. One rejection, both orders (test, P3). Removed.
3. Repeat `replaced`: `_replace_applied`. Removed.
4. Fill before `replaced`: P5 passes. Removed.
5. Late close of the old order: P1 passes. Removed.
6. `pending_replace` mapped. Removed.
7. Chain followed. Removed.
8. Not removed: only the quantity comparison was added, with the quantity read late (finding 1); a same-terms second modify is also taken for the retry (P8), which leaves the order at the terms B asked for, so P8 alone is not counted.
9. Removed after `replaced` (test, P4); not on the fill-then-rejected path (finding 2).
10. Rejected replacement after fill settles the modify. Removed.
## What I ran
The same scratch probe set as for the other runs of this case (removed after; tree diff unchanged). P9 and P14b fail here and pass in the other three runs; P8 fails here only.
