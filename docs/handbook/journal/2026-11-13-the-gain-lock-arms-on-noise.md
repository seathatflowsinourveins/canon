### 2026-11-13-the-gain-lock-arms-on-noise: The gain lock as written arms on noise, and it is not a default until its replay proof

* area: exits
* session: 2026-11-13
* result: The owner protector's gain lock arms when the bid reaches the entry plus the round-trip cost (zero on paper) plus the noise floor, the spread-floor multiple times the arming quote's spread, and once armed its trigger sits at about the entry. On a narrow spread that is a few cents over the entry, so in engine runs on recorded tapes it armed within seconds of the start: on a liquid tape its sale went out, and was refused, for a holding a hundredth of a per cent over its entry; on a thin name it released the sale at the entry milliseconds after arming, before the tape ran far higher. A replay of seven entries of one session under the trail alone, the lock as written and two variants ended with the lock as written and the variant whose trigger waits under the bid by the noise floor far ahead of the trail alone and close to each other, nearly all of the difference from one name; that one replay is a measurement, not grounds for a default
* evidence: `src/canon_trading/gain_lock.py` (`arms`, `arming_measures`) and `src/canon_trading/owner_protector.py:1507-1605` at 32389697; the engine runs, the variant replay and the lead's log of 2026-11-13 and 2026-11-14, kept outside the repository
* lesson: A rule that follows from its rows as written can still trade on noise. The owner's rule that a gain lock holds a covered gain is kept; how it arms is a measured choice, off by default until a replay over enough sessions shows which form earns it
* change: none on main, where the lock is on in the owner protector; the unit that makes it a named setting, off by default and on by a flag, is built and not on main, so it is UNVERIFIED here
* next: Replay the lock as written and its variants (arming only on a gain of at least the trail's own distance; a trigger strictly under the bid by the noise floor) over more sessions, and judge them by ADR 0083's adoption test
* status: open
* supersedes: none
* source: lead
* owner: the simulation lane
* closes: the gain lock off by default on main, and a variant replay over at least the sessions ADR 0089's floor asks, judged by the lead
