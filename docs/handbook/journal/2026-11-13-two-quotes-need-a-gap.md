### 2026-11-13-two-quotes-need-a-gap: Two quotes of one burst are one observation, so a count of quotes needs a gap

* area: exits
* session: 2026-11-13
* result: An engine run of the owner protector's quote count at two, built off main, sent its sale 476 microseconds after the level went over the bid, on two quotes of one burst, because the count's gap defaulted to zero; the same tape with a 1000 ms gap sent it about a second later and one cent lower. One tape, two settings. On main the protector guards the first observation of a breach and confirms on a second quote with a strictly greater stamp, with no gap between them
* evidence: `src/canon_trading/owner_protector.py:121-127` at 32389697 (the guard, and two quotes with one stamp as one observation); ADR 0119 and `src/canon_trading/mover.py:987-991` at 32389697 (the Mover's gap between counted prints, 100 ms by default, zero admitting any strictly newer print); the engine run and the lead's log of 2026-11-13, kept outside the repository
* lesson: A count over a span means little if two observations can be a microsecond apart: the gap between counted observations is part of the rule, required whenever the count is above one and refused by name when left out
* change: none on main; the protector unit that counts above one and requires the gap there (1000 ms to start, UNCALIBRATED) is built and not on main, so it is UNVERIFIED here
* next: Measure the protector's gap at 250, 1000 and 3000 ms on recorded tapes before a value is called calibrated
* status: open
* supersedes: none
* source: review
* owner: the owner protector's build lane
* closes: the protector's count above one on main with its gap required, and its measurement at the three gaps recorded
