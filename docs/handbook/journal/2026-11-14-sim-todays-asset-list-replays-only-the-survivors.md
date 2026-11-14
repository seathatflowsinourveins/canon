### 2026-11-14-sim-todays-asset-list-replays-only-the-survivors: A past day replayed from today's asset list holds only the names that survived to today

* area: data
* session: 2026-11-14
* result: The sim lead counted, on the development days of the date map, the distinct names on a past year's daily gainer and loser lists that have no instrument definition in our set (today's list of active assets): 57 of 99 in 2022 (58%), 61 of 100 in 2023 (61%), 95 of 263 in 2024 (36%) and 87 of 271 in 2025 (32%). The missing names are former listings, acquired or delisted under those symbols, not a format mismatch. The loader asks only for defined names, and the adapter loads active assets alone
* evidence: the sim lead's survivorship count and its read-only script, kept outside the repository; only active assets are loaded at packages/canon-alpaca/src/canon_alpaca/providers.py:33-34 and :514-523; the bars' raw adjustment at src/canon_trading/bars_to_catalog.py:77-78
* lesson: A historical day built from today's definitions is the surviving names only, so the ranking misses most of an early year's movers, and since the names that spike and then delist are the gain lock's own case, leaving them out biases the arms' differences and not only their level. Raw bars keep prices as traded, but a lookback that crosses a later split reads a jump nobody traded
* change: none; every historical replay names this limit by year until a universe of every name with bars on the day is loaded
* next: The data-foundation lane's unit for names no longer listed (the adapter refuses inactive assets; a first probe was refused by name, with no request sent) says whether such a universe can be loaded
* status: open
* supersedes: none
* source: lead
* owner: the sim lane
* closes: that unit's report, and a replay universe of every name with bars on its day or a recorded limit
