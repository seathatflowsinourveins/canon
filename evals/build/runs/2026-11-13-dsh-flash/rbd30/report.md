# Report: five defects in the trial ledger, data identity and calendar

## What was wrong

The ledger and gate trusted the catalog and the study file rather than the data each run actually
read:

1. `record_trial` ran the node, then re-queried the catalog with `bars_read` for the digest, session
   days and sample bounds. A catalog replaced between the replay and that query gave the old run the
   new bars' digest, so mixed inputs could pass the gate. Nothing checked that the engine's cache held
   every bar the run replayed, or that a cached bar was inside the sample.
2. `backtest_run_config` accepted any overlap between the catalog's recorded intervals and the
   sample. A catalog covering only part of the sample was accepted, so two configurations could rank
   on a shared fragment. A sample before or after the catalog was only caught by the old overlap
   check, and a gap between two loads was not distinguished from a range the catalog did not cover.
3. `rank` selected records with `trial.user_attrs.get("sample") == current.number`. A keyed record that
   failed before its `sample` write was therefore invisible, and a later record of its key was never
   needed. A record that ran with a sample value the study does not have (`7`, `"0"`, `True`) was
   likewise dropped.
4. `create_ledger_study` committed the Optuna study name before `samples` was written. An interrupt in
   between left a name that `open_study` refused (no sample) and `create_ledger_study` refused
   (`DuplicatedStudyError`).
5. `daily_series` kept only returns whose days were also session days. A session day with no return
   simply disappeared from the series, so the ranking ran on fewer days than the trial had sessions.

## What changed, and why this way

### 1. Digest and session days from the replayed bars (`ledger.py`, `backtest.py`)

`BacktestRun` now carries `bars`, read from the engine's cache with `Cache.bars` for every bar type in
the run config, before `node.dispose()` (`run_backtest_config`). `record_trial` uses those bars for
`session_days` and `data_digest`, and fails the trial before writing a digest when:

- `len(run.bars) != result.iterations` (`"the engine's cache holds N bars and the run replayed M"`), or
- a bar's `ts_init` is outside the current sample (`"the run replayed N bars outside sample …"`).

The failed path goes through the existing `_tell_failed`, so the record is `FAIL` / `failed` with the
error and no `data_digest`, as the tests require.

`data_digest` now sorts by `(bar_type, ts_init, ts_event)`. The engine cache returns bars in reverse
replay order (observed here), and ADR 0041 term 3 already specified the sorted order, so this both
follows the record and makes the digest independent of cache order.

The now-unused `bars_read` helper was removed rather than left as a second way to identify data.

### 2. Full sample coverage (`backtest.py`)

When both `start` and `end` are given (the ledger always gives both), `backtest_run_config` now walks
the catalog's sorted `get_intervals` result and finds the first instant in `[start, end]` that no
recorded interval covers (`_first_uncovered`). If there is one, it raises `LookupError` naming the
instant and bar type before `study.ask()` is reached. When the missing instant lies between two
recorded intervals, the message points at `ParquetDataCatalog.consolidate_data`, which merges them.

With fewer than both bounds the old overlap check is kept: the other side is open, so full coverage is
not definable, and the existing message for a bound past the catalog is preserved.

### 3. Sample-less and invalid-sample records (`ranking.py`)

`rank` now reads the study's sample history once and classifies every keyed record:

- a valid number equal to the current sample is read as before;
- a valid number of an older sample is left out, as before;
- no `sample` attribute at all is an “unknown sample” record: a failed/RUNNING one joins the key's
  unresolved records and stops the ranking until a later record of that key runs to the end; one that
  nevertheless ran stops the ranking, named;
- a present but non-integer, boolean, or unknown sample number is invalid: a record that ran with it
  stops the ranking with “`#N (...) ran with sample X, not a sample number of the study`”, and a
  failing one is named too.

This keeps ADR 0035 term 5's “later record resolves a failure” rule while no longer dropping records
whose sample write never happened.

### 4. Completing an interrupted creation (`ledger.py`)

On `DuplicatedStudyError`, `create_ledger_study` now loads the existing study under the same creation
lock. If its `samples` attribute is absent or empty, the study was interrupted before its sample was
written, and this call writes sample 0 with the requested range. If `samples` is present, the duplicate
is re-raised. `samples()` now distinguishes “created without a sample” (message says
`create_ledger_study completes it`) from a malformed non-empty `samples` value.

### 5. Refusing a session day with no return (`returns_matrix.py`)

`daily_series` computes `session_days - returns` after shape checks and raises `ReturnsRejected` naming
the trial, the number of missing session days, and the first missing date. The calendar can no longer
be shortened silently. The module header and docstrings now say closed days are still dropped but a
session day without a return is refused.

### Records corrected

`docs/adr/0035` term 3 and `docs/adr/0041` terms 1–3 and 5 were updated where they said session days
and the digest were read from the catalog after the run, and to record full coverage, consolidation,
interrupted-creation completion, session-less keyed records and session days without returns.
`docs/adr/0041`'s Confirmation and Evidence were extended with the new cases and the installed symbols
read this session. No README or architecture claim was found to be made untrue by the change.

## Upstream API / contract followed

Read from the installed packages this session (NautilusTrader 2.0.0rc8, Optuna 5.0.0):

- `BacktestResult.iterations` (`nautilus_trader/backtest/__init__.pyi`), `BacktestNode.get_engine_cache`,
  and `Cache.bars(bar_type)` (`nautilus_trader/common/__init__.pyi`) — the run's replayed bar count and
  its cached bars, read before disposal.
- `ParquetDataCatalog.get_intervals` and `consolidate_data`
  (`nautilus_trader/persistence/__init__.pyi`) — recorded covered ranges and the upstream operation
  that makes the recorded range contiguous.
- Optuna `study.ask` / `Study.tell` / `Study.set_user_attr` / `Study.user_attrs`, `create_study`
  raising `DuplicatedStudyError`, and `load_study` — the existing ledger contract.
- ADR 0010 protocol steps 2–3, ADR 0035 terms 3 and 5, ADR 0041 terms 1–3 and 5.

No dependency was added; `pandas` was already a project dependency and is used only to format the
missing nanosecond instant.

## What ran, with results

All commands used `mise exec -- uv run --locked …`; no network was used beyond `uv` installing the
pinned lock, and no paper test, credential, or `scripts/land` was run.

- The ten named test functions (14 parametrized cases):
  `test_a_creation_interrupted_before_its_sample_is_completed_by_the_next`,
  `test_a_hole_no_load_can_fill_is_closed_by_consolidating_the_catalog`,
  `test_a_keyed_failure_before_its_sample_is_written_stops_the_ranking`,
  `test_a_sample_the_catalog_covers_in_part_is_refused_naming_the_gap`,
  `test_a_sample_the_catalog_does_not_reach_is_refused_before_a_trial`,
  `test_bars_the_cache_does_not_account_for_are_told_failed`,
  `test_the_digest_is_of_the_bars_the_run_read_not_a_later_catalog`,
  `test_a_record_that_ran_with_no_sample_number_of_the_study_stops`,
  `test_session_days_with_no_return_stop_and_are_named`,
  `test_a_session_day_with_no_return_is_refused`
  → **14 passed**.
- The affected offline files (`test_ledger`, `test_ranking`, `test_returns_matrix`, `test_backtest`,
  `test_variant_study`, `test_bars_to_catalog`) → **170 passed**.
- Every offline test file in `tests/` and `packages/` except files whose names contain `paper`
  (paper tests were excluded as instructed) → **513 passed**.
- `ruff check .` → **All checks passed**.
- `ruff format --check .` → **111 files already formatted**.
- `ty check` → **All checks passed**.

`scripts/ci` was not run: this copy omits files it expects, and the task states a failure there may not
be mine; the graded tests and the three code checks were run directly.

## Unsure / limits

- `BacktestResult.iterations` has no docstring in the installed stub. I used it as “data points
  replayed” and observed it equal the bar count in these runs (`Iterations: 40`, `70`, and the bounded
  25). For a run that also loaded non-bar data the equality would not hold, but this repo's runs load
  bars only. That broader meaning is **UNVERIFIED**.
- `Cache.bars` returned bars in reverse replay order here; the digest and session days are both made
  order-independent, so this is handled, but the ordering contract itself is **UNVERIFIED** beyond
  observation.
- `get_intervals` is treated as an inclusive `ts_init` coverage interval, as the existing code and the
  hole tests assume. `consolidate_data` merging a gap was observed directly in this checkout.
- The new coverage check only applies when both bounds are supplied. That preserves the existing
  one-bound API and its tests; the ledger path always supplies both.

The working tree holds the six edited files; nothing was committed and nothing was landed.
