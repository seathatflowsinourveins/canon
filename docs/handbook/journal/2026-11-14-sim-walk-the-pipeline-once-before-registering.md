### 2026-11-14-sim-walk-the-pipeline-once-before-registering: Register a comparison only after its whole pipeline has run once, end to end, on a day outside the sample

* area: strategies
* session: 2026-11-14
* result: Within an hour, the sim lead's frozen registrations of the first diagnostic family were undone three times, by its count, each before any tick loaded and each by a fact one walk through the pipeline would have shown: 39 of the first draw's 40 sample days fell before the quote floor of 2025-11-04; the same draw was mostly blind to that year's movers, since names no longer listed have no definition; and the next registration defined its population by a pass over bars alone, where the Mover refuses an entry that has no quote. Minute bars had loaded for the second draw's days, but no tick was loaded and no outcome read, so no day was spent
* evidence: the sim lead's registration files of the first family, the superseding marker and the survivorship count, kept outside the repository; the quote floor at src/canon_trading/history_load.py:134-136 under docs/adr/0066-alpaca-quotes-into-the-catalog.md; no quote refuses an entry at src/canon_trading/mover.py:1007; active assets only at packages/canon-alpaca/src/canon_alpaca/providers.py:33-34
* lesson: A registration written from the record and the code, without a run, carries the facts no one has run into yet; one development day outside the sample, run through every stage as a mechanism run (data, ranking, entries, ticks, arms), shows them before the freeze, and its measured load time sizes the sample against the loading deadline, so that fewer days are drawn by the same seed instead of cutting after results exist
* change: the lead's ruling of 2026-11-14, received by the sim lane at 01:26: before a family file is frozen, one such walk runs, and the registration is written from what it showed
* next: The next family's registration names the day it walked, each stage's outcome on it, and the load time that sized its sample
* status: open
* supersedes: none
* source: lead
* owner: the sim lane
* closes: a frozen family registration that names its walked day, each stage's outcome and the measured load time behind its sample size
