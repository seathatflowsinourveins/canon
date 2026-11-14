### 2026-11-13-sim-warm-up-needs-catalog-requests: A replay that leaves the strategy's history request unanswered replays a different strategy

* area: data
* session: 2026-11-13
* result: A mechanism run, ranking nothing (2026-11-13 is a forward date). One regular-session Mover window replayed on NautilusTrader's BacktestEngine and its simulated venue, on recorded SIP quotes, trades and minute bars, arm OWN1, main's Mover. With no data client to answer the Mover's history request for its ATR, 4 of the engine's 6 entries took `cold_atr_fallback` where paper's 7 of 7 took the ATR, and since size follows the trail, sizes and exits departed from paper's. Two reruns with the engine's catalog-backed requests (the backtest config's `catalogs`) made 7 entries, 6 from the ATR and 1 from the spread floor; every run ended flat with 0 handler errors (in 61,914, 61,475 and 61,528 log lines)
* evidence: the sim lane's run report R1 and the run summaries of its reruns R1b and R1c (which have no report of their own), kept outside the repository; `catalogs` in crates/backtest/src/config.rs:136-139 at the nautilus-trader pin d53849b2; the fallback at src/canon_trading/mover.py:5250
* lesson: A cold fallback changes the trail, the trail changes the size, and the comparison with paper is lost before the first exit; a replay answers the strategy's own requests from the catalog before any difference is read
* change: none on main; the sim lane's harness runs with catalog requests from R1b on
* next: The farm's parity set, run with catalog requests, counts each entry whose trail source matches paper's
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's parity-set report, with the trail source of each entry set beside paper's
