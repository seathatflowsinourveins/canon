# Gate and Trial Ledger Defect Report

## 1. What Was Wrong

Five defects in `src/canon_trading/` let a study pass the ADR 0010 / ADR 0035 / ADR 0041 gate on inputs those records reject, or left an interrupted study unusable:

1. **Post-replay catalog read for `session_days` and `data_digest` (`ledger.py`, `backtest.py`).**
   `record_trial` called `bars_read(catalog_path, bar_types, sample.start, sample.end)` (`ParquetDataCatalog.query`) *after* `run_backtest_config` finished. If the catalog was replaced or modified between the replay and that query, the old run received the new catalog's `session_days` and `data_digest`, allowing runs over different bars to agree on their digest and pass the gate. Nor did `record_trial` verify that every replayed iteration (`BacktestResult.iterations`) was accounted for by the bars hashed and lay within the study's sample.
2. **Partial overlap accepted instead of full sample coverage (`backtest.py`).**
   `backtest_run_config` checked `any(first <= high and last >= low for first, last in intervals)`, accepting any partial overlap between the catalog's stored intervals and `[start, end]`, and naming raw nanoseconds instead of missing ISO-8601 ranges when disjoint. Two configurations could therefore run on a fragment of the study's sample, share the shorter calendar, and pass the gate. A gap between two loads over a stretch holding no bar (such as a weekend) also gave no guidance on closing it with `ParquetDataCatalog.consolidate_data`.
3. **Keyed records without a valid study sample number ignored (`ranking.py`, `variant_study.py`, `ledger.py`).**
   `rank` filtered each key's records by `t.user_attrs.get("sample") == sample.number`. A keyed record that failed after writing `config_key` and before writing `sample` was omitted from `records`, so an unresolved failure did not stop the ranking. Conversely, a record that ran to the end carrying a `"sample"` value that is not a sample number of the study (e.g. `7`, `"0"`, `True`, or `False`, where in Python `bool` is a subclass of `int` so `False == 0` and `True == 1`) was either dropped from the current sample or matched an integer sample number instead of stopping the ranking.
4. **Interrupted study creation left an unrecoverable study name (`ledger.py`).**
   `create_ledger_study` called `optuna.create_study` (which commits the study row immediately) and then `study.set_user_attr("samples", ...)`. An interruption between those two writes left a study row with no `samples` attribute: `open_study` refused it via `samples(study)`, and calling `create_ledger_study` again raised Optuna's `DuplicatedStudyError`.
5. **Session days missing from `returns_series` silently dropped (`returns_matrix.py`).**
   `daily_series` built its calendar with `[key for key in keys if key in open_days]`, silently dropping any day in `session_days` absent from `returns_series`. Trials missing the same session day in `returns_series` therefore ranked on fewer days `T` than the sessions they recorded.

---

## 2. What Changed and Why

* **`src/canon_trading/backtest.py`**
  * Added `bars: list[Bar]` to `BacktestRun`. Before `node.dispose()` empties the engine cache, `run_backtest_config` collects external bar types from `config.data` and `cache.bar_types(AggregationSource.EXTERNAL)` and reads `reversed(cache.bars(bar_type) or ())` so cached bars are preserved oldest-first per bar type.
  * Updated `backtest_run_config` so that when `start` or `end` is given, an inverted range (`low > high`, when one bound lies past every stored interval of the catalog) still raises `LookupError` first (because `get_missing_intervals_for_request` returns `[]` when `start > end`), and then calls `catalog.get_missing_intervals_for_request(low, high, NautilusDataType.Bar, bar_type)`. Any missing interval raises `LookupError` before a trial is asked for, formatting each gap with `unix_nanos_to_iso8601` and naming `ParquetDataCatalog.consolidate_data` for closing an empty stretch.
* **`src/canon_trading/ledger.py`**
  * Removed `bars_read` and replaced the post-run catalog query in `record_trial` with `_bars_of_run(run, bar_types, sample)` over `run.bars`. Inside `record_trial`'s `try` block — before `returns_series`, `session_days`, or `data_digest` is written — `_bars_of_run` verifies that `len(run.bars) == run.result.iterations`, that every bar has `sample.start <= bar.ts_init <= sample.end`, and that no unexpected bar type was cached; any mismatch raises `RuntimeError` and is told `FAIL` with `outcome="failed"`.
  * Sorted bars by `(str(bar.bar_type), bar.ts_init, bar.ts_event)` inside `data_digest` itself so ADR 0041 term 3 holds regardless of caller order.
  * Added `_is_int` (`isinstance(value, int) and not isinstance(value, bool)`) and `sample_number(trial, known)` so booleans, strings, missing attributes, and unknown integers are never accepted as study sample numbers.
  * Added `_is_incomplete_creation(study)` (`study.directions == [StudyDirection.MAXIMIZE]`, empty `user_attrs`, and no trials). `samples(study)` now raises `LedgerNotFound` naming `create_ledger_study` when a creation was interrupted before `samples` was written, and `create_ledger_study` catches `DuplicatedStudyError` under the exclusive creation lock, loads the study, re-raises unless `_is_incomplete_creation(study)` holds, and writes `samples` to complete it. Foreign Optuna studies (e.g. default `minimize` direction, existing attributes, or existing trials) still raise `DuplicatedStudyError`.
* **`src/canon_trading/ranking.py` and `src/canon_trading/variant_study.py`**
  * Updated `rank` to check `bad_sample = [t for t in every if sample_number(t, known) is None and _ran(t)]` first, stopping and naming any record that ran with a `sample` that is not a sample number of the study.
  * Included keyed records that did not run and carry no valid sample number (`num is None and not _ran(t)`) in the current sample's `records` in both `ranking.rank` and `variant_study._to_record`, so a failure before `sample` was written stops the ranking until a later record of its key runs to the end, and `variant_study` re-runs that key on the next pass.
* **`src/canon_trading/returns_matrix.py`**
  * Updated `daily_series` to reject duplicate `session_days` entries and to check `[day for day in open_days if day not in key_set]`, raising `ReturnsRejected` naming the series, how many of its session days have no return, and the first missing UTC date.
* **Documentation and ADR updates (`README.md`, `docs/adr/0035-trial-ledger-in-an-optuna-study.md`, `docs/adr/0041-trial-ledger-sample-and-data-identity.md`)**
  * Updated module headers and docstrings in `backtest.py`, `ledger.py`, `ranking.py`, and `returns_matrix.py`.
  * Updated `README.md` to list `run.bars` among the cached objects on `BacktestRun`.
  * Updated ADR 0035 term 3 + Evidence and ADR 0041 terms 1, 2, 3, 5 + Evidence so neither record claims `session_days` or `data_digest` is read from `ParquetDataCatalog.query` after the replay.
* **Additional offline tests (`tests/test_gate_defects.py`)**
  * Added 8 offline tests covering `BacktestRun.bars` ordering and order-independent `data_digest`, unexpected cached bar types, single-sided partial sample bounds, `sample=False` against sample `0`, `variant_study` re-running a key that failed before writing `sample`, preserving `DuplicatedStudyError` on a foreign Optuna study, and duplicate `session_days` rejection. Existing test files were left untouched.

---

## 3. Upstream APIs and Contracts Followed

All upstream signatures and docstrings were verified in this checkout's `.venv` (`nautilus-trader==2.0.0rc8`, `optuna==5.0.0`, `pandas==3.1.1`):

* **NautilusTrader `v2.0.0rc8`**
  * `nautilus_trader.backtest.BacktestNode.get_engine_cache` and `nautilus_trader.common.Cache.bars(bar_type)` ("Returns cached bars for the `bar_type` in reverse chronological order by `ts_event`. Index 0 is the most recent bar. Returns `None` when no bars are cached for the bar type.") and `Cache.bar_types(AggregationSource.EXTERNAL)` (`nautilus_trader/common/__init__.pyi`).
  * `nautilus_trader.config.CacheConfig.bar_capacity` (defaults to `10000` in `.venv`, which is why `_bars_of_run` checks `len(run.bars) == run.result.iterations`).
  * `nautilus_trader.persistence.ParquetDataCatalog.get_intervals`, `ParquetDataCatalog.get_missing_intervals_for_request(start, end, data_type, identifier)`, and `ParquetDataCatalog.consolidate_data(data_type, identifier, ...)` (`nautilus_trader/persistence/__init__.pyi`).
  * `nautilus_trader.core.unix_nanos_to_iso8601` (`nautilus_trader/core/__init__.pyi`).
* **Optuna `v5.0.0`**
  * `optuna.create_study`, `optuna.load_study`, `optuna.exceptions.DuplicatedStudyError`, and `optuna.study.StudyDirection.MAXIMIZE` (`optuna/study/study.py`, where `storage.create_new_study` commits the study row before `Study.set_user_attr` is called).
* **Repository ADRs**
  * ADR 0010 (steps 1–3), ADR 0031, ADR 0035 (terms 1–3, 5, 6), and ADR 0041 (terms 1–3, 5, 6).

---

## 4. Verification Run and Results

1. **Baseline reproduction before edits**:
   * `mise exec -- uv run --locked pytest tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py`
   * Result: `14 failed, 110 passed` (the exact 10 test functions / 14 parametrized cases named in the task).
2. **Target reproduction tests after edits**:
   * `mise exec -- uv run --locked pytest tests/test_ledger.py::test_a_creation_interrupted_before_its_sample_is_completed_by_the_next tests/test_ledger.py::test_a_hole_no_load_can_fill_is_closed_by_consolidating_the_catalog tests/test_ledger.py::test_a_keyed_failure_before_its_sample_is_written_stops_the_ranking tests/test_ledger.py::test_a_sample_the_catalog_covers_in_part_is_refused_naming_the_gap tests/test_ledger.py::test_a_sample_the_catalog_does_not_reach_is_refused_before_a_trial tests/test_ledger.py::test_bars_the_cache_does_not_account_for_are_told_failed tests/test_ledger.py::test_the_digest_is_of_the_bars_the_run_read_not_a_later_catalog tests/test_ranking.py::test_a_record_that_ran_with_no_sample_number_of_the_study_stops tests/test_ranking.py::test_session_days_with_no_return_stop_and_are_named tests/test_returns_matrix.py::test_a_session_day_with_no_return_is_refused`
   * Result: `14 passed in 3.87s`.
3. **Full offline test suite**:
   * `mise exec -- uv run --locked pytest` (before adding `tests/test_gate_defects.py`): `513 passed, 24 skipped in 106.80s` (the 24 skipped tests are `test_paper_*.py`, which require Alpaca paper keys and were not run).
   * `mise exec -- uv run --locked pytest tests/test_gate_defects.py tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py tests/test_backtest.py tests/test_variant_study.py`: `165 passed in 19.14s`.
4. **Code and ADR checks**:
   * `mise exec -- uv run --locked ruff check .` -> `All checks passed!`
   * `mise exec -- uv run --locked ruff format --check .` -> `112 files already formatted`
   * `mise exec -- uv run --locked ty check` -> `All checks passed!`
   * `mise exec -- scripts/canon lint` -> `canon: lint clean`

---

## 5. Uncertainties and Known Limits

* **Engine bar cache capacity on large intraday samples**: At `nautilus-trader` `2.0.0rc8`, `CacheConfig().bar_capacity` defaults to `10,000` bars per bar type (`backtest.py` leaves `CacheConfig` at upstream's default). Any trial replaying more than `10,000` bars of one bar type will evict older bars from `Cache.bars`, causing `_bars_of_run` to raise `RuntimeError` (`len(bars) != result.iterations`) and tell the trial `FAIL` rather than recording a partial digest. Raising `CacheConfig.bar_capacity` for large intraday studies was not needed here and was not changed.
* **Paper test suite (`UNVERIFIED` in this session)**: As instructed, no paper tests (`test_paper_*.py`), network calls, or credentials were used.
