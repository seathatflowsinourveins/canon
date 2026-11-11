# First reader: r1b0e
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. execution.py:1529 (`order_replace_rejected` then a replacement `rejected` for one modify gives two rejections): needs Alpaca to send both for one replace, which no source read here shows; the base code also generated both.
2. execution.py:1242 (`_open_modify` picks an unanswered modify first): needs two modifies in flight on one Alpaca order with the first's stream rejection arriving in between; speculative ordering.
3. execution.py:1572 (fill on a second-generation replacement, two `replaced` missed): beyond defect 4's single replace; the report states it as not covered.
4. execution.py:1625 (`_superseded` written before the report check raises): only when the replacement is unmappable; exotic.
5. execution.py:318 (pending_replace old order and open replacement both reported at start-up): every run of this case maps `pending_replace` as the supplied test requires; whether Alpaca lists both open at once is not shown by any source read.
6. execution.py:1599 (intermediate modifies left in `_modifying` after a chain follow): needs a second modify sent on the first replacement before either `replaced`; probed two modifies in sequence, nothing left (P4).
7. execution.py:1150 (unknown-outcome modify stays open and takes the next stream rejection): each modify still gets one outcome; the attribution is unknowable after a 500.
8. execution.py:1208 (retry judged before `replaced`, then `replaced` missed): speculative ordering.
9-10. Duplicated chain loop, redundant supersede write, linear scans: cleanups.
Also not counted: across a client disconnect/connect all four runs of this case lose replace state; here a late `canceled` of the old order and a repeated `replaced` after a reconnect are applied (probed P12, P13). Not in the statement, and the reconnect class affects every run, so it is not held against one.
## Per-defect check
1. Missed `replaced`, replacement filled: `_took_over` reports a closed replacement whose old order shows `replaced_by` it. Removed (probed also canceled-with-fill, P6: reported CANCELED with filled 1).
2. 422 plus `order_replace_rejected`: one rejection, stream first (test) and 422 first (P3). Removed.
3. `replaced` twice: `_superseded` drops the repeat. Removed.
4. Fill before `replaced`: `_take_over` updates first; partial fill then late `replaced` gives one update (P5). Removed.
5. Late `canceled`/`expired` of the old order: ignored; `expired` probed (P1). Removed.
6. `pending_replace`: ACCEPTED, PARTIALLY_FILLED with fills. Removed.
7. Replacement moved on: `_replaced` follows `replaced_by`. Removed.
8. Second modify refused: compared on full terms and on which modify holds the replacement; same-terms second modify (P8) and `replaced` landing during the refusal lookup (P9) both rejected. Removed.
9. Answers kept: kept only while the modify is open; nothing left after two modifies (P4) or after a fill then a rejected replacement (P14b). Removed.
10. Rejected replacement after fill: handled before the end check, logged, modify settled. Removed.
## What I ran
Scratch probe file in the tree's tests (removed after; tree diff unchanged), on the real node with the conftest stand-ins: P1 late expired, P3 422 then stream rejection, P4 two modifies then state, P5 partial fill before replaced then replaced, P6 replaced missed and replacement canceled partly filled, P8 same-terms second modify, P9 first `replaced` applied during the second modify's refusal lookup, P10 fill on replacement after a reconnect, P12/P13 late cancel and repeated replaced after a reconnect, P14/P14b answer after the old order filled. All pass here except P12 and P13.

## Reread (settling the second reader's no)
Final mark: no. Blocker: 4. Reproduced: modify n0 answered 500 and not applied; modify n1 applied, Alpaca sends `replaced`, alpaca-py's retry refused 422 "order already replaced". Patched: `OrderSubmitted, OrderAccepted, OrderUpdated, OrderModifyRejected`; starting execution.py: `OrderSubmitted, OrderAccepted, OrderUpdated`. The stale n0 is settled in n1's place, so a modify that succeeded is reported rejected after its update. Note 7 above was wrong: n1's own outcome was known. The same probe gives the same false rejection on r9ac6 and rb62a, not on ra205.
