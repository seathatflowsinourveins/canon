# First reader: rd513
Mark: fix
Blocker: none
## Findings
1. change.patch, `.pytest-tmp/` (54 files) — the patch carries pytest's temporary output from the worker's first runs: per-test SQLite `trials.db`, `trials.db.lock` and parquet catalogs. `.gitignore` covers `.pytest_cache/` only, so these are untracked and unignored, and would be committed by `git add -A` into a public repository. They are not read by any test (pytest makes and clears its own basetemp), so they do not change what a test run means; but they must be removed before landing. The report states they were left in place after `rm -rf` was refused. Verified by reading the patch and the grade's `changed` list (severity: fix)
2. src/canon_trading/ledger.py:518 — the out-of-sample refusal says "the first at {outside[0]}", but `run.bars` is `Cache.bars` order, newest first per bar type, so the date named is the latest stray bar, not the first. Verified by reading `run_backtest_config` (bars not reversed) and the message (severity: fix)
## Review findings not taken
1. backtest.py:227 (bar cache capacity): the supplied test pins the cache design; no run sets the capacity; the change names `CacheConfig.bar_capacity` in the error.
2. ranking.py:201 (orphan from an older sample read on the current one): the ranking stops and the runner re-runs the key (probe G1).
3. ledger.py:434 (tie order of identical timestamps): duplicate bars of one timestamp; speculative.
4. ledger.py:520 (unexpected bar type check unreachable): harmless defensive branch.
5. backtest.py:196 (two interval scans, uncapped list of gaps): efficiency and message length.
6. backtest.py:294 (bars copied for every caller): efficiency.
7. CLAUDE.md:1 (`scripts/ci` not run): the brief tells the worker this copy lacks its inputs; the graded checks ran.
8. `.pytest-tmp/`: finding 1. Looked at for blocker 1 (the grade flags these files as "a file the harness or a runtime reads"): they are test outputs, not inputs to any test, so not counted as blocker 1.
## Per-defect check
1. Digest and session days from the cached bars of the run, checked against iterations, the sample and the bar types; told failed otherwise. Removed.
2. Missing intervals over the sample refused before a trial, named; consolidation named for an empty stretch. Removed.
3. Orphan failures read on the current sample until a later record runs; runner re-runs them (G1); a run with a foreign sample number stops, named. Removed.
4. Interrupted creation completed only for an empty maximizing study with no attributes or trials. Removed.
5. A session day without a return refused, named; the ranking adds the trial number. Removed.
## What I ran
- Probe G1 (scratch test, removed): orphan failure, then `record_grid`, then `rank`: the key is re-recorded and the ranking passes (N = 2).
- Probe (scratch script outside the tree): foreign multi-objective study: open -> LedgerNotFound, create -> DuplicatedStudyError.
- Read the grade's `flags` (all 54 are `.pytest-tmp/` outputs) and the report's note on them.
