All five study-integrity defects are fixed, and all 523 offline tests pass. The change remains uncommitted in the supplied working tree; no existing test file or dependency pin was changed.

The defects and repairs are:

1. The ledger queried the catalog after replay for its digest and session days, so replacing the catalog could give an old run a new identity. `BacktestRun` now captures the engine's cached bars before disposal, and the ledger derives both fields from those bars. The digest sorts bars by `ts_init` and `ts_event`, preserving ADR 0041's bytes despite the cache's reverse ordering. Before writing results or a digest, the ledger checks the bar count against replay iterations, checks every bar's inclusive sample bounds, and rejects unrequested bar types. An accounting failure is told `FAIL` with outcome `failed` through the existing exception handler.
2. The coverage check accepted any overlap. A bounded sample now uses the catalog's own missing-interval API for each bar type and refuses a missing interval before `Study.ask`, naming its exact UTC bounds. The diagnostic describes upstream consolidation for a gap a load showed contains no bar; checking coverage does not change the catalog.
3. Filtering records by sample number silently dropped keyed failures interrupted before writing their sample. Those incomplete records now participate on the current sample until a later completed record resolves them. Records showing a run with a missing, non-integer, or unknown sample number stop ranking and are named even after a later run of the same key, including booleans and floats that compare equal to integers. The study runner's retry selector follows the same rule, so the normal recording pass can recover an incomplete keyed record.
4. Study creation committed the name before its sample, leaving an interrupted creation neither openable nor creatable. Under the existing exclusive creation lock, creation now loads and completes only an empty maximising study with no user attributes or trials. Other duplicates remain errors. Opening an incomplete creation explains how to complete it without modifying it.
5. Session dates absent from a trial's returns were lost when intersecting the two calendars. `daily_series` now refuses missing session returns before filtering closed days, naming the series, missing count, and first missing date. Ranking adds the trial number to that diagnostic.

The changes follow ADR 0010, ADR 0035 and ADR 0041. I corrected the README, the affected module contracts, and ADRs 0035 and 0041 to describe the actual replay data and recovery rules. The new `tests/test_trial_integrity.py` adds ten cases covering automatic retry after an interrupted sample write, protection against overwriting another study, actual cache truncation, and malformed sample numbers that a later run cannot resolve.

Upstream interfaces were checked locally in the packages installed from this checkout's `uv.lock`; no upstream network lookup was used. NautilusTrader 2.0.0rc8's `common/__init__.pyi` and installed `Cache.bars` docstring expose the cached bars and capacity, `backtest/__init__.pyi` exposes `BacktestNode.get_engine_cache` and `BacktestResult.iterations`, and `persistence/__init__.pyi` plus installed docstrings expose `get_missing_intervals_for_request` and `consolidate_data`. The offline replay tests verify iteration counts on both one and two instruments, and the added test verifies a real capacity limit. Optuna 5.0.0's installed `study/study.py` documents duplicate creation, loading, attributes, directions and reading every trial; its ask/tell contract remains responsible for persistence and state changes. The sample, digest and record-validation rules remain the local glue already specified by the ADRs.

Verification performed:

- Before the repair, the ten named regressions failed in all 14 parameter cases.
- The initial broader run after the repair passed 147 tests and failed one environmental assumption: keeping pytest's temporary directory inside the checkout made an intended non-repository path discover the parent Git repository. Git's installed manual documents `GIT_CEILING_DIRECTORIES`; setting it to the test-cache directory isolates repository discovery while keeping test writes inside this checkout. That individual test then passed. No production code or existing test was changed for this issue.
- The additional integrity tests and existing study-runner tests passed together: 19 passed.
- The complete offline run below passed: **523 passed in 100.56 seconds**. Paper test modules were excluded from collection.

```sh
GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache" mise exec -- uv run --locked pytest \
  --basetemp=.pytest_cache/offline-tmp --ignore-glob='**/test_paper_*.py' \
  -q --tb=short --show-capture=no
```

- `mise exec -- uv run --locked ruff check .`: passed.
- `mise exec -- uv run --locked ruff format --check .`: passed, 112 files already formatted.
- `mise exec -- uv run --locked ty check`: passed.
- `scripts/canon lint`: clean.
- `git diff --check`: clean; the tracked existing tests and dependency files have no changes.

Paper operation and external-service behaviour are **UNVERIFIED**, intentionally outside this task. No paper test, credential access, independent agent, landing, push, or pull request was used; `scripts/ci` was not run because the task identifies missing inputs in this copy. Cache retention remains bounded by upstream's configured capacity: a run exceeding it now fails explicitly instead of receiving an incomplete digest. No implementation work or failing offline check remains for the reported defects.

The initial temporary directory `.pytest-tmp/` remains untracked. Automatic approval review rejected `rm -rf -- .pytest-tmp` with “rm -f style commands are not permitted. Use a safer approach”; I left it in place and attempted no alternate deletion. Other test artifacts are under the ignored `.pytest_cache/` directory.
