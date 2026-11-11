# First reader: r9ac6
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. execution.py:1228 (`_pending_modify` prefers a queued, unsent modify): needs a second modify waiting on the REST lock when the first's stream rejection arrives; speculative ordering.
2. execution.py:1127 (expected qty on a partly filled order): what Alpaca puts in a replacement's `qty` for a partly filled order is UNVERIFIED in ADR 0036; not shown.
3. execution.py:1162 (state left after an unknown outcome or a canceled replacement): an unknown-outcome modify is the existing "trade update or reconciliation resolves it" stance; the realistic fill-then-rejected sequence leaves nothing (P14b). Without the replacement's `rejected`, which Alpaca documents it sends, the answer stays (P14); not counted.
4. execution.py:1245 (`_reject_modify` silent when no attempt): only after a disconnect cleared state, or after finding 1.
5. execution.py:1528 (unmappable replacement drops the fill): the replacement is the node's own order with its own type and side; exotic.
6. execution.py:1575 (chain read not caught): the outer handler logs it as an error; not hidden.
7. execution.py:1964 (canceled replacement that never took over reported): Alpaca rejects such a replacement (quoted in the code); a canceled one that never took over is not shown by any source.
8. Duplicated chain loop: cleanup.
Also not counted: a repeated `replaced` after a client reconnect gives a second OrderUpdated (P13); every run of this case loses some replace state across a reconnect.
## Per-defect check
1. Replaced missed: non-rejected closed replacement reported (test; P6 canceled partly filled reported). Removed.
2. One rejection, both orders of 422 and stream (test, P3). Removed.
3. Repeat `replaced`: `_updated_orders` dedupes. Removed.
4. Fill before `replaced`: terms applied first; partial fill then `replaced` gives one update (P5). Removed.
5. Late close of a confirmed old order ignored; `expired` probed (P1). Removed.
6. `pending_replace` mapped. Removed.
7. Chain followed. Removed.
8. Retry only when no other call claims the replacement and full terms match; P8 and P9 rejected. Removed.
9. Answers kept only while the attempt is pending (test, P4, P14b). Removed.
10. Rejected replacement after fill settles the modify with a log. Removed.
## What I ran
The same scratch probe set as for the other runs of this case (removed after): P1, P3, P4, P5, P6, P8, P9, P10, P14b pass; P13 (reconnect) and P14 (no replacement rejection sent) fail, both not counted as above.
