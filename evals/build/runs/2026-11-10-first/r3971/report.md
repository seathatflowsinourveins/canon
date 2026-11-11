# Trial gate integrity repair

All five defects are fixed in the working tree. The final offline suite passed **526 tests**, including all supplied regressions and 13 additional cases; lint, formatting and type checks passed. Nothing was committed or landed.

## What was wrong and what changed

1. **The ledger identified a later catalog instead of the replay.** `record_trial` queried the catalog after the engine had finished, so replacing it could attach corrected bars' digest and session days to an earlier run. `BacktestRun` now carries the externally sourced bars taken from the engine cache before disposal. The ledger checks that their count equals the bars-only replay's `BacktestResult.iterations`, rejects any `ts_init` outside the inclusive sample, and sorts the retained bars before calculating the existing digest and session days. A mismatch raises inside the existing failure-recording block, tells the trial `FAIL` with outcome `failed`, and writes no digest.

2. **Any overlap passed the sample check.** Bounded backtests now use `ParquetDataCatalog.get_missing_intervals_for_request` for each bar type instead of accepting one overlapping interval. Leading, trailing and internal gaps, a wholly disjoint sample, and an empty catalog are refused before `study.ask`, with the missing UTC ranges named to nanosecond precision. A single supplied bound takes its other endpoint from stored coverage; an inverted resulting range is still refused. The diagnostic names upstream's `consolidate_data` for a gap between files that a load has established contains no bar. Production code does not consolidate or assume that missing data is empty.

3. **Sample filtering hid interrupted keyed records.** Ranking validates a record's sample against the study's sample history before filtering. An unfinished keyed record without a valid sample participates in the current sample's existing later-record check, so an earlier successful run cannot resolve a later failure and a later successful record can. A completed record with a missing, unknown or incorrectly typed sample number always stops ranking, named with its key and trial number. Booleans and floats are not accepted as integer sample numbers. Every configuration key still counts toward N.

4. **Interrupted creation stranded a study name.** While holding the existing creation lock, `create_ledger_study` can load and complete a duplicate name only if the study has exactly the maximising direction and no user attributes or trials. It writes the requested initial sample through Optuna's public API. An existing sample, malformed attributes, trials or another direction still cause `DuplicatedStudyError`; no record is deleted or reset. Opening an unfinished creation remains read-only and now names `create_ledger_study` as the recovery call.

5. **Missing returns silently shortened the calendar.** `daily_series` now checks the session-day set against the return keys before dropping closed days. Missing session returns are refused with their count and first missing UTC date, and ranking includes the key and trial number in the resulting stop.

The changes are in `backtest.py`, `ledger.py`, `ranking.py` and `returns_matrix.py`. ADR 0035 and ADR 0041 and the affected module documentation now describe the actual cache provenance, coverage checks, creation recovery and sample validation. `tests/test_trial_integrity.py` adds malformed-sample cases that remain rejected after a valid rerun, protections against reinitialising an existing study, real cache eviction, and an empty catalog's missing-range diagnostic. No existing test file, dependency manifest, registry or lockfile was changed.

## Contracts checked this session

The installed package versions were verified as NautilusTrader **2.0.0rc8**, Optuna **5.0.0**, pandas **3.1.1** and skfolio **1.12.8**. No network documentation or other repository copy was used.

- NautilusTrader's installed `common/__init__.pyi` exposes `Cache.bar_types(AggregationSource.EXTERNAL)`, `Cache.bars(bar_type)` and `CacheConfig.bar_capacity`. The runtime `Cache.bars` docstring specifies reverse chronological order by `ts_event`; the ledger restores ADR 0041's canonical order before hashing. The installed `backtest/__init__.pyi` exposes `BacktestNode.get_engine_cache` and `BacktestResult.iterations`. Real offline replays exercise these interfaces.
- Installed `persistence/__init__.pyi` and runtime docstrings expose `get_intervals`, `get_missing_intervals_for_request` and `consolidate_data`. Coverage comes from that upstream API rather than a new interval algorithm. The supplied consolidation regression verifies that the known empty weekend gap closes without changing the data digest.
- Optuna's installed `optuna/study/study.py` documents `create_study`, `DuplicatedStudyError`, `load_study`, `Study.directions`, `get_trials` and `set_user_attr`. Recovery uses those public interfaces under the existing lock, with no private storage mutation.
- ADR 0010, ADR 0035 and ADR 0041 provide the repository contracts for the complete sample, configuration counting, failure resolution, data identity and synchronous session returns. Their review dates have not expired. The statistical implementation and digest encoding were retained.

## Verification

The initial command `mise exec -- uv run --locked pytest tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py -q` reproduced **14 failures and 110 passes** before any code change.

Final full offline command, with all paper-test modules excluded during collection:

```sh
env GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache" mise exec -- uv run --locked pytest --ignore-glob='**/test_paper_*.py' -q --tb=short --show-capture=no --basetemp=.pytest_cache/final-offline-tests
```

Result: **526 passed in 82.02 seconds**, exit 0.

| Final check | Result |
| --- | --- |
| `mise exec -- uv run --locked ruff check .` | Passed, exit 0 |
| `mise exec -- uv run --locked ruff format --check .` | Passed; 112 files already formatted, exit 0 |
| `mise exec -- uv run --locked ty check` | Passed, exit 0 |
| `git diff --check` | Passed, exit 0 |
| Diff of existing tests, manifests, registry and lockfiles | Empty |

Intermediate verification found two formatting issues and one nested-context lint issue in the added test; these were corrected before the final checks. The additional invalid-sample cases and empty-catalog diagnostic were observed failing before their corresponding changes. A first run with temporary files inside the checkout also failed `test_outside_a_git_checkout_there_is_no_default_ledger`, because its nominally external directory inherited this repository; later runs use `GIT_CEILING_DIRECTORIES` only for the test temporary directory's ancestor so that this fixture remains outside Git discovery while test writes stay within the checkout. Source-directory Git discovery is unaffected, and the test passes in the final suite.

## Limits and work left

The cache remains bounded by upstream's default capacity, observed as 10,000 bars per bar type. A trial whose cache cannot retain its replay fails explicitly; this repair does not increase that capacity. The added eviction test uses the real engine with capacity 3 and confirms that a 40-bar trial is told failed without a digest.

The final diff was reviewed against the five requirements by the author. Independent review was not performed because this task forbids other agents. Paper tests and `scripts/ci` were not run, as instructed; the latter requires files omitted from this task copy. Upstream Rust internals and historical evidence not present in the installed interfaces remain **UNVERIFIED this session**. No required implementation or offline verification remains.
