# Report: Trial Ledger, Sample Coverage, Returns Matrix, and Gate Fixes

## What Was Wrong

Five defects in `src/canon_trading/` allowed a study to pass the evaluation gate on inputs refused by ADR 0010, ADR 0035, and ADR 0041, or left an interrupted study unusable:

1. **Post-replay catalog re-read for `session_days` and `data_digest` (`src/canon_trading/ledger.py`, `src/canon_trading/backtest.py`).**
   `record_trial` called `bars_read(catalog_path, bar_types, sample.start, sample.end)` *after* `run_backtest_config` finished, querying `ParquetDataCatalog.query` on disk rather than reading the bars the engine replayed. Replacing or rewriting the catalog during or immediately after the replay gave the old run the new catalog's `data_digest` and `session_days`, letting mixed data pass the gate. In addition, `data_digest` hashed bars in caller order instead of sorting by `(ts_init, ts_event)` as ADR 0041 term 3 specifies.
2. **Partial overlap accepted instead of full sample coverage (`src/canon_trading/backtest.py`).**
   `backtest_run_config` checked `any(first <= high and last >= low for first, last in intervals)`, accepting any partial overlap of stored catalog intervals with `[start, end]`, and raised a generic `"holds no ... bars from ..."` message when the sample lay completely outside the catalog. Two configurations could therefore rank on a fragment of the study's sample, and empty stretches between two loads (such as a weekend with no bars) had neither gap diagnostics nor guidance on closing them via catalog consolidation.
3. **Missing or invalid `sample` attribute on keyed trial records (`src/canon_trading/ranking.py`, `src/canon_trading/variant_study.py`).**
   `rank` (and `variant_study._to_record`) filtered keyed trials with `t.user_attrs.get("sample") == sample.number`. Because Python evaluates `False == 0` and `True == 1`, booleans matched integer sample numbers; a keyed trial that failed before `trial.set_user_attr("sample", sample.number)` had `None != sample.number` and was silently omitted from `records` (allowing an earlier completed trial of that key to pass the gate without resolving the later failure); and a record that ran to the end (`traded` or `idle`) with an unknown, string, boolean, or missing `sample` was treated as merely not yet run on the current sample instead of stopping the ranking as a foreign/corrupt record.
4. **Study creation interrupted between study row and `samples` attribute (`src/canon_trading/ledger.py`).**
   `optuna.create_study` commits the study name before `study.set_user_attr("samples", ...)` runs. An interrupt or failure between those two writes left a study row with no `samples` user attribute: `open_study` refused it with `LedgerNotFound` and a subsequent `create_ledger_study` raised `DuplicatedStudyError`, stranding the name.
5. **Session days missing from `returns_series` silently dropped (`src/canon_trading/returns_matrix.py`).**
   `daily_series` built `kept = [key for key in keys if key in open_days]` without verifying `open_days <= set(keys)`. Any day in a trial's `session_days` absent from `returns_series` was silently dropped from the trial's calendar, allowing trials with missing return days to rank on fewer sessions than they had bars.

---

## What Changed and Why

* **`src/canon_trading/backtest.py`**
  * Added `bars: list[Bar]` to `BacktestRun`, populated in `run_backtest_config` from the engine's `Cache` (`node.get_engine_cache(config.id)`) before `node.dispose()`, across `AggregationSource.EXTERNAL` and `AggregationSource.INTERNAL`, sorted by `(str(bar.bar_type), bar.ts_init, bar.ts_event)`. Reading from the engine cache captures the exact in-memory `Bar` objects processed by that node rather than whatever is on disk afterwards.
  * Updated `backtest_run_config` so that when `start` or `end` is given, missing bounds default to `intervals[0][0]` / `intervals[-1][1]`, inverted ranges (`low > high`) are refused first with `LookupError("The catalog at ... holds no ... bars from ...")` (because upstream's `get_missing_intervals_for_request` returns `[]` when `start > end`), and `catalog.get_missing_intervals_for_request(low, high, NautilusDataType.Bar, bar_type)` is checked. Any uncovered interval raises `LookupError` before a trial is asked for, formatting every gap with `unix_nanos_to_iso8601` and noting `ParquetDataCatalog.consolidate_data` for stretches a load found empty.
* **`src/canon_trading/ledger.py`**
  * Added `_replayed_bars(run, bar_types, sample)` and removed `bars_read`. Inside `record_trial`'s `try` block (so any failure marks the trial `FAIL` with `outcome = "failed"` before `data_digest` is written), `_replayed_bars` verifies `len(run.bars) == run.result.iterations`, checks that no unexpected bar types are present, and verifies `sample.start <= bar.ts_init <= sample.end` for every bar. `session_days` and `data_digest` are computed from those validated `run.bars`.
  * Updated `data_digest` to sort each bar type's bars by `(bar.ts_init, bar.ts_event)` before hashing, matching ADR 0041 term 3 regardless of input order (including `Cache.bars`'s reverse-chronological order).
  * Added `_incomplete_study(study)` (`not study.user_attrs and not study.get_trials(deepcopy=False) and study.directions == [StudyDirection.MAXIMIZE]`). Under the exclusive `flock` in `create_ledger_study`, `DuplicatedStudyError` loads the existing study and completes `samples` if and only if `_incomplete_study(study)` holds (otherwise re-raising `DuplicatedStudyError`). In `samples(study)`, an incomplete study raises `LedgerNotFound` stating that creation was interrupted and `create_ledger_study` completes it.
* **`src/canon_trading/returns_matrix.py`**
  * In `daily_series`, checked that every entry of `session_days` lies on a UTC midnight (`day % DAY_NS == 0`) and that `open_days - set(keys)` is empty; otherwise raised `ReturnsRejected` naming the series, how many of its session days lack a return, and the first missing UTC date.
* **`src/canon_trading/ranking.py` and `src/canon_trading/variant_study.py`**
  * Added `_is_sample_number(value, valid_samples)` (`isinstance(value, int) and not isinstance(value, bool) and value in valid_samples`) and `_on_sample(trial, sample, valid_samples)`.
  * In `rank`, any record that `_ran(t)` whose `sample` attribute is not a valid sample number of the study stops the ranking naming the key, trial, outcome, and `sample` value. A keyed record that did *not* run to the end (`failed` or `RUNNING`) without a valid sample number is read as on the current sample in both `rank` and `variant_study._to_record`, stopping the ranking (and scheduling a re-run in `variant_study`) until a later record of that key runs to the end.
* **Documentation, comments, ADRs, and new tests**
  * Updated module headers and docstrings in `backtest.py`, `ledger.py`, `returns_matrix.py`, and `ranking.py`, plus `README.md` (`run.bars`), `docs/adr/0035-trial-ledger-in-an-optuna-study.md` (term 3 and Evidence), and `docs/adr/0041-trial-ledger-sample-and-data-identity.md` (terms 1, 2, 3, 5 and Evidence).
  * Added `tests/test_gate_contracts.py` (8 test cases) covering reverse-ordered `data_digest`, unexpected cached bar types, explicit inverted bounds, non-interrupted duplicate study guards, `False`/`None` sample numbers on completed records, `variant_study.record_grid` recovery after a failed `sample` write, and off-midnight / multi-day missing session returns. Existing test files were not modified.

---

## Upstream APIs and Contracts Followed

Verified against the pinned packages installed in `.venv` (`nautilus-trader==2.0.0rc8`, `optuna==5.0.0`, `pandas==3.1.1`, `skfolio==1.12.8`) and repo ADRs 0010, 0031, 0035, 0040, and 0041:

* **NautilusTrader v2.0.0rc8 (`nautilus_trader` in `.venv`)**:
  * `BacktestNode.get_engine_cache(run_config_id) -> Cache`, `Cache.bar_types(aggregation_source) -> list[BarType]`, and `Cache.bars(bar_type) -> list[Bar] | None` (returns cached bars in reverse chronological order by `ts_event`; bounded per bar type by `CacheConfig.bar_capacity`, default `10_000`).
  * `BacktestResult.iterations: int` (total data items replayed by the engine, checked against `len(run.bars)` to detect cache eviction or unaccounted items).
  * `ParquetDataCatalog.get_intervals(data_type, identifier)`, `ParquetDataCatalog.get_missing_intervals_for_request(start, end, data_type, identifier)` (returns missing `(start, end)` nanosecond intervals; returns `[]` when `start > end`, so inverted bounds are checked first), and `ParquetDataCatalog.consolidate_data(data_type, identifier, ...)` (merges files so empty stretches between loads become one contiguous covered interval).
  * `nautilus_trader.core.datetime.unix_nanos_to_iso8601(timestamp_ns)` for formatting missing nanosecond intervals as RFC 3339 / ISO 8601 UTC timestamps.
* **Optuna v5.0.0 (`optuna` in `.venv`)**:
  * `optuna.create_study(..., direction="maximize")` raising `optuna.exceptions.DuplicatedStudyError`, `optuna.load_study`, `Study.set_user_attr`, `Study.get_trials(deepcopy=False)`, and `optuna.study.StudyDirection.MAXIMIZE`.

---

## Verification Run and Results

1. **Baseline test suite (before changes)**:
   * `mise exec -- uv run --locked pytest` -> `14 failed, 499 passed, 24 skipped in 109.47s` (the 14 failures were the 10 named test functions across their parameterizations).
2. **Full offline test suite (after changes)**:
   * `mise exec -- uv run --locked pytest` -> `521 passed, 24 skipped in 95.00s` (`0` failures; all 14 previously failing items and all 8 new items in `tests/test_gate_contracts.py` passed; the 24 skipped items are the paper tests).
3. **Code and ADR checks**:
   * `mise exec -- uv run --locked ruff check .` -> `All checks passed!` (exit 0).
   * `mise exec -- uv run --locked ruff format --check .` -> `112 files already formatted` (exit 0).
   * `mise exec -- uv run --locked ty check` -> `All checks passed!` (exit 0).
   * `./scripts/canon lint` -> `canon: lint clean` (exit 0).

---

## Uncertainties and Limits

* **Paper tests (`UNVERIFIED`)**: Per task rules and ADR 0029, no paper tests (`test_paper_*.py`) or commands reading credentials / network were run.
* **Cache bar capacity on very long runs**: `BacktestEngineConfig` uses NautilusTrader's default `CacheConfig` (`bar_capacity = 10_000` per bar type; upstream allows up to `1_000_000`). Runs exceeding `10_000` bars of a single bar type will now deterministically fail in `record_trial` (`len(run.bars) != run.result.iterations`) rather than recording a partial digest. Raising `bar_capacity` was not done here because daily-bar studies (such as ADR 0048's 1,366 bars per symbol) sit well below `10_000` and no ADR specifies a higher cache cap.
* **`scripts/ci` (`UNVERIFIED` in full)**: As noted in the task prompt, this isolated checkout omits files `scripts/ci` expects; the graded checks (`pytest`, `ruff check .`, `ruff format --check .`, `ty check`) and `scripts/canon lint` were all run directly and passed.
