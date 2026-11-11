# First reader: rb62a
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. execution.py:1383 (`_accept` seeds `_current_venue_id` from the first update after a reconnect): probed (P10): after a client disconnect/connect between the PATCH answer and the fill, the replacement's fill of 5 is refused as an overfill of 2. Every run of this case loses replace state across a client reconnect in some way (r1b0e applies a late cancel and a repeated `replaced`; r9ac6 and ra205 a repeated `replaced`), the statement does not name reconnects, and a real restart is reconciled from the replacement's own report; so the class is not held against one run.
2. execution.py:1157 (stale call key after a 500 takes the next stream rejection): each modify still gets one outcome; after a 500 the attribution is unknowable.
3. execution.py:783 (tracking sets cleared only on disconnect): ID sets like the existing `_accepted` and `_finished`; the PATCH answers themselves do not build up (P4, P14b).
4. execution.py:1621 (a fill with no `execution_id` leaves the order unfinished): the node did not get that fill either, so leaving the order open matches what it holds.
5. execution.py:1734 (adoption raises for an unmappable replacement): exotic.
6. execution.py:2156 (two reports at start-up): shared by every run, as the supplied pending_replace test requires the mapping.
7. execution.py:1251 (leftover refusal count swallows a later unmatched rejection): needs a 422 with no stream event and then an unmatched stream rejection; speculative.
8-10. Duplicated settlement blocks, dead code, altitude: cleanups.
## Per-defect check
1. Non-rejected closed replacement reported (test; P6). Removed.
2. One rejection, both orders (test, P3). Removed.
3. Repeat `replaced` ignored (test; P13 also passes). Removed.
4. Fill before `replaced` adopts first (test, P5); not after a client reconnect (P10, not counted). Removed for the stated sequence.
5. Late `canceled`/`expired` ignored (test, P1, P12). Removed.
6. `pending_replace` mapped. Removed.
7. Chain followed. Removed.
8. Claimed replacements and full terms; P8 and P9 rejected. Removed.
9. Answers popped on settlement (test, P4, P14b). Removed.
10. Rejected replacement after fill logged and settled. Removed.
## What I ran
The same scratch probe set as for the other runs of this case (removed after): all pass except P10 (reconnect, above) and P14 (no replacement rejection sent, not a documented sequence).

## Reread (settling the second reader's no)
Final mark: no. Blocker: 4. Reproduced with no reconnect: start-up reconciliation loads an open buy of 2 at 150; modify to 5 with a silent answer; partial fill 1 on the replacement; `replaced` for the old order; fill 4 on the replacement. rb62a ends at quantity 2, filled 1, PARTIALLY_FILLED (`OrderInitialized, OrderAccepted, OrderFilled`; NautilusTrader logs "Order overfill rejected ... last_qty=4, quantity=2"); r1b0e, r9ac6 and ra205 all end at quantity 5, filled 5, FILLED. Not the reconnect class every run shares; this run's own regression on ordinary use.
