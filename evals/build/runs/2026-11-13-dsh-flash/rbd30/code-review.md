# /code-review high on rbd30's first/copy

Run from the scoring session as a forked `/code-review` at high effort, target the copy's uncommitted diff. Finished 2026-11-13 by 15:34:53 New York, read from date after it ended (187 s, 28 tool uses). These are the review's candidate findings, in its order, unverified by the scoring session. They are not marks.

```json
[
  {
    "file": "src/canon_trading/backtest.py",
    "line": 276,
    "summary": "The cache bars come from `Cache.bars`, which keeps at most `CacheConfig.bar_capacity` bars per bar type (10,000 by default at the pin). `backtest_run_config` sets no CacheConfig, so on any longer run the new check `len(bars) != result.iterations` fails.",
    "failure_scenario": "Take a sample of minute bars covering more than about 10 to 25 sessions (over 10,000 bars of one bar type). The engine replays all N bars, but `BoundedVecDeque::new(bar_capacity)` keeps only the newest 10,000 (crates/common/src/cache/config.rs:73-76, mod.rs:2473-2477). `record_trial` raises 'the engine's cache holds 10000 bars and the run replayed N', so every trial on a realistic sample is told FAIL/failed and no study can ever be ranked."
  },
  {
    "file": "src/canon_trading/ledger.py",
    "line": 422,
    "summary": "The cache does not hold one bar per iteration even within capacity. `insert_bar` replaces a time bar whose `ts_event` equals the front bar's, and skips a bar whose `ts_event` is older than the front, so the strict equality with `iterations` rejects valid runs.",
    "failure_scenario": "Two cases hit this: a catalog with two bars of one type sharing a `ts_event`, and bars whose `ts_event` order differs from their `ts_init` order (crates/common/src/cache/mod.rs:7140-7157). The engine iterates both bars but the cache keeps one, so `record_trial` raises 'cache holds N-1 bars and the run replayed N' and records the trial as failed. Before this change the catalog's bars were digested instead."
  },
  {
    "file": "src/canon_trading/ranking.py",
    "line": 228,
    "summary": "A keyed record with no `sample` attribute now counts as unresolved, but `variant_study._to_record` still looks only at records on the current sample. The variant-study runner thinks the key is done and never re-runs it, so the ranking stops for good.",
    "failure_scenario": "Key K has a record #2 that ran on the current sample. A later run, #5, fails on the `sample` write and is left with a key but no sample. `rank` stops with 'no later record ran to the end after #5'. `record_grid` calls `_to_record`, which finds #2 as the latest on-sample record and has it ran, so K is skipped as 'already run'. Every `variant_study` pass then ends with the same RankingStopped, and nothing in the tool clears it."
  },
  {
    "file": "src/canon_trading/returns_matrix.py",
    "line": 100,
    "summary": "A session day with no key in `returns_series` is now refused. NautilusTrader's daily series only runs from the first to the last day that has a balance or equity event, so a trial whose last account event comes before the last session day is rejected. Before, those trailing days were dropped.",
    "failure_scenario": "In `calculate_daily_returns` (analyzer.rs:514-551), `last_day` is the last key in `daily_equity`. On the account-event path that is the day of the last fill. A strategy whose final fill is several sessions before the sample ends gets no returns on those days, so `daily_series` raises 'no return on k of its M session days' and the ranking stops on a trial that ran correctly."
  },
  {
    "file": "src/canon_trading/backtest.py",
    "line": 236,
    "summary": "The new full-coverage check needs the catalog's recorded intervals to cover every nanosecond of [start, end]. A sample that starts at a date's midnight is therefore refused whenever the catalog's interval starts at the first bar's `ts_init`, which is the catalog writer's default when no start is given.",
    "failure_scenario": "`create_ledger_study(start='2025-06-02', ...)` sets the sample start to 00:00 UTC. A catalog written with `write_bars(series)` and no explicit start records [first bar ts_init 13:30, ...]. `_first_uncovered` returns 2025-06-02T00:00, and `record_trial` raises LookupError 'does not cover the sample: missing 2025-06-02T00:00:00+00:00' even though every bar in the range is present. The consolidate hint is not shown either, because the gap is before the first interval."
  },
  {
    "file": "src/canon_trading/ranking.py",
    "line": 199,
    "summary": "A record with an invalid or unknown sample number stops the ranking even when it did not run. Nothing can clear that stop: `mark_not_run` refuses any keyed record, and a later record does not resolve an invalid one. The ADR text says only a record that ran with such a sample stops the ranking.",
    "failure_scenario": "Another writer or a corrupted attribute leaves a keyed, failed record whose `sample` is 7 or '0'. Every `rank` call appends 'has sample 7, not a sample number of the study', so the study is stuck. Unlike the `unknown` case, re-running the key does not help, and this does not match ADR 0041 term 5 ('A record that ran with a sample number the study does not have stops it')."
  },
  {
    "file": "src/canon_trading/backtest.py",
    "line": 183,
    "summary": "`_first_uncovered` (and `_between_intervals`) re-implement gap finding that the pinned upstream already provides: `ParquetDataCatalog.get_missing_intervals_for_request(start, end, data_type, identifier)` in persistence/__init__.pyi:387.",
    "failure_scenario": "This adds hand-written interval logic that can drift from how the catalog defines coverage. It breaks the rule 'Prefer upstream to invention. Use what a maintained upstream provides before writing a replacement.' (~/.claude/CLAUDE.md) and AGENTS.md rule 3 'Glue yes, invention no'."
  },
  {
    "file": "src/canon_trading/ledger.py",
    "line": 335,
    "summary": "Any existing study whose `samples` attribute is missing or empty is now adopted by `create_ledger_study` instead of raising DuplicatedStudyError. That includes a study a non-ledger writer created with `optuna.create_study`, possibly with trials or a different direction.",
    "failure_scenario": "Study 'x' was made directly with `direction='minimize'` and holds trials with no ledger attributes. `create_ledger_study('x', ...)` loads it, writes sample 0 and yields it as a ledger study. The foreign trials then stop rankings as keyless, and Optuna's best-trial direction is the opposite of what the ledger assumes. The recovery cannot tell an interrupted ledger creation apart from a foreign study."
  },
  {
    "file": "src/canon_trading/backtest.py",
    "line": 228,
    "summary": "The intervals are sorted twice: `backtest_run_config` sorts them, then `_first_uncovered` calls `sorted(intervals)` again.",
    "failure_scenario": "This does redundant work on every run. It is minor, but either the caller's sort or the helper's sort can go."
  },
  {
    "file": "src/canon_trading/backtest.py",
    "line": 322,
    "summary": "`BacktestRun` now holds every replayed `Bar`, copied out of the cache. `record_trial` copies them again with `list(run.bars)` and returns them in `RecordedTrial.run`, so all of a run's bars stay alive for as long as the caller holds the result.",
    "failure_scenario": "`variant_study.record_grid` records many trials on a large minute-bar sample. Each RecordedTrial keeps a full bar list (two copies at peak inside `record_trial`), so memory grows with trials × bars. The digest and session days could be computed inside `run_backtest_config` and only those returned."
  }
]
```
