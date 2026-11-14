### 2026-11-13-sim-protective-sale-fills-in-parts-inside-its-limit: A protective sale larger than the tape filled in parts, every part inside its limit

* area: exits
* session: 2026-11-13
* result: A mechanism run, ranking nothing (2026-11-13 is a forward date). Two owner-protector runs on a recorded mover tape, row OWN1, on NautilusTrader's engine with liquidity consumption; the buy was booked under the protector's id so that the simulated venue would fill its sale, and in the first, up to the release, its 13 protector lines equal, timestamps included, those of the same tape booked as another strategy's. In the second, with a holding larger than the tape's printed size, the gain lock's sale filled in 54 parts over 97 ms, every part at or above its floor, with no reprice and no wait at the floor; 0 handler errors. A measurement, never a counted proof
* evidence: the sim lane's run report P9, kept outside the repository; the selection it sets in its own process is the constant src/canon_trading/owner_protector.py selects external holdings by
* lesson: Under the engine's liquidity consumption a sale larger than the printed size completes in parts without leaving its limit, so a binding cap is a matter of time in the book, not of price; the refused-sale branch is shown only with the foreign booking, and the chase's re-price is not reached here
* change: none
* next: An engine run in which the bid passes a protective sale's limit, so that the chase re-prices, among the protector's open items
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's protector run that shows the chase's re-price on the engine
