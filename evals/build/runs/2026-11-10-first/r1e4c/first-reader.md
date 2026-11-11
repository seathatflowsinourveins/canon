# First reader: r1e4c
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. backtest.py:222 (default `CacheConfig.bar_capacity` of 10,000 fails any run with more bars of one type): the supplied test pins the design (the cache's bars counted against the replay, "the engine's cache holds 39 bars and the run replayed 40"); none of the four runs sets the capacity, and this one states the limit in its code and report. Not held against one run.
2. ledger.py:431 (iterations counts non-bar data too): no non-bar data is loaded by any run config here; future-facing.
3. ranking.py:185 (a record that ran with a foreign sample stops permanently): the statement asks for exactly that stop, named.
4. ranking.py:198 (an orphan from an older sample now reads as on the current one, changing the stop's wording): the ranking still stops and the runner re-runs the key (`_to_record` uses the same rule); probed the runner's recovery (G1).
5. returns_matrix.py:111 (message when `session_days` is empty): still true of that input.
6. variant_study.py:140 (predicate written twice): cleanup.
7-9. Triple sort, union of bar types, duplicate int checks: cleanups.
10. ledger.py:197 (an empty maximize study from another writer is completed): probed a foreign multi-objective study: open refuses it with LedgerNotFound and creation with DuplicatedStudyError, as before. A foreign single-objective empty study is indistinguishable from an interrupted creation; speculative.
## Per-defect check
1. Digest and session days from `BacktestRun.bars` (the engine cache, read before disposal), checked against `result.iterations`, the sample and the bar types; a mismatch is told failed with no digest. Removed.
2. `get_missing_intervals_for_request` over the sample; any gap refused before a trial, named, with consolidation as the remedy for an empty stretch. Removed.
3. Orphan failure stops the ranking until a later record of its key runs; the runner re-runs it (probe G1: one pass records the key, ranking then passes with N = 2); a record that ran with a foreign sample stops, named. Removed.
4. Interrupted creation completed under the lock; `open_study` names the remedy. Removed.
5. A session day without a return refused, counted and named. Removed.
## What I ran
- Probe G1 (scratch test in the tree, removed): two configurations recorded, an orphan failure of one (sample write raising), then `variant_study.record_grid` and `rank`: the pass re-records the key and the ranking passes (N = 2).
- Probe (scratch script outside the tree): a foreign multi-objective study "x" in the ledger: `open_study` -> LedgerNotFound, `create_ledger_study` -> DuplicatedStudyError.
