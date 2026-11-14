### 2026-11-13-sim-overnight-entries-agree-exits-part-on-latency-and-depth: On an overnight window the engine's entries matched paper's and its exits parted on two named causes

* area: execution
* session: 2026-11-13
* result: A mechanism run, ranking nothing (2026-11-12 and 2026-11-13 are forward dates). One overnight Mover window, arm OWN3, replayed on NautilusTrader's engine on recorded overnight quotes and trades, with the window's own 45 recorded ranking generations published at their received times: 8 of 8 entries agreed with paper in time, size and trail source; 4 of 8 exits agreed to the second, 2 of them at the same price; flat at the end, 0 handler errors in 30,726 log lines; the engine's realized result was about 6 per cent harsher than paper's. Paper filled 0.76 to 0.94 s after each signal and the engine at the signal (zero latency in the profile). The engine took only displayed and printed size, so one sale filled in parts at a lower average than paper's single fill and one entry filled in part first; three exits sold at other times because their trails followed other peaks once the fill prices differed
* evidence: the sim lane's run report P1, kept outside the repository; the ranking was fed as recorded generations, not recomputed, and the code was main's Mover, not the window's own commit
* lesson: Given the same ranking, entries replay exactly; exits part on latency and on liquidity consumption, and here the engine's fill was the harsher, since paper's venue ignores quoted size. Those 0.8 s are from signal to fill, so a model of them is one of fill delay, not of the order's acceptance
* change: none
* next: The venue profile's second version, with a latency model from the paper records and the fill model's switches cited at the pin, reruns this window and counts the exits that agree
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's rerun of this window under venue profile v2, with agreeing exits counted against this entry's 4 of 8
