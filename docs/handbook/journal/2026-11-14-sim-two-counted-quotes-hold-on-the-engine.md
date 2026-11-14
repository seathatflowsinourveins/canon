### 2026-11-14-sim-two-counted-quotes-hold-on-the-engine: On the engine a protective sale waited for a second counted quote past the gap, every time

* area: exits
* session: 2026-11-14
* result: A mechanism run, ranking nothing, on 2026-11-13's recorded SIP tapes (a forward date). The owner protector at a branch's tip not yet on main, row OWN1, a breach count of two and a confirm gap of 1000 ms (the node configuration's starting value, UNCALIBRATED), five holdings booked under the protector's id so that the simulated venue fills its sales: each holding's protective sale went out once, on the second counted quote and never the first, from 1,002 to 2,032 ms after the first, and filled once; 5 sales, 5 fills, 0 refusals, 0 ALERTs, 0 handler errors. Quotes inside the gap were not counted, and one holding's count returned to zero when its bid recovered above the level and then counted again from one
* evidence: the run farm's report e-R6 and its run, kept outside the repository
* lesson: It confirms 2026-11-13-two-quotes-need-a-gap over five holdings on the engine: the two-quote rule holds on NautilusTrader's engine as written: no sale on one quote, a reset when the bid recovers. What it cannot say is how often a live feed's second quote arrives in time, since the recorded tape's cadence is not a live feed's gaps, duplicates and late quotes
* change: none; the configurable count and its required gap are not on main
* next: The same run at the landing head of the unit that brings the count to main, then a paper window that counts how often the second quote arrives in time
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's rerun at the count unit's landing head on main, and a paper window's count of second quotes in time
