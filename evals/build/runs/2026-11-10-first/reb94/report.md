# Gate, Ledger, and Sample Defect Report

## 1. What Was Wrong

Five defects in `src/canon_trading/` allowed a study to pass the deflated Sharpe ratio gate on invalid or inconsistent inputs, or left a study name permanently unusable:

1. **Post-replay catalog query for `data_digest` and `session_days` (`ledger.py`, `backtest.py`):**
   `record_trial` called `bars_read(catalog_path, ...)` (`ParquetDataCatalog.query`) *after* `run_backtest_config` returned and disposed the node. Replacing or modifying the catalog between replay and recording gave the earlier run the later catalog's digest and session days, allowing runs on different bars to agree and rank together. Nothing verified that all replayed bars (`BacktestResult.iterations`) were accounted for or lay within `[sample.start, sample.end]`.
2. **Any catalog overlap accepted as covering the sample (`backtest.py`):**
   `backtest_run_config` checked only `any(first <= high and last >= low for first, last in intervals)`. A sample only partially covered by stored intervals—or spanning an unconsolidated empty stretch between two loads—ran silently over the fragment present, and a sample completely outside the catalog raised an overlap error rather than naming the missing sample coverage.
3. **Keyed records without a valid study sample number (`ranking.py`, `variant_study.py`, `ledger.py`):**
   `ranking.rank` filtered each key's records with `t.user_attrs.get("sample") == sample.number` and checked `unresolved` only within that filtered list. Consequently:
   - A keyed record that failed before `trial.set_user_attr("sample", sample.number)` ran had no `"sample"` attribute and was silently omitted instead of stopping the ranking until a later run of its key resolved it (and `variant_study._to_record` likewise skipped re-running it).
   - A record that ran to the end (`traded` or `idle`) with an unknown sample integer, a string such as `"0"`, or a `bool` (`True == 1`, `False == 0` in Python) was either silently ignored or, for `bool`, matched sample `0` or `1` via `==`, instead of stopping the ranking as foreign/malformed input.
4. **Interrupted study creation left an unrecoverable study name (`ledger.py`):**
   `create_ledger_study` committed the study row via `optuna.create_study` before writing the `samples` user attribute. An interruption between those two writes left a study in SQLite that `open_study` refused (no `samples` attribute) and `create_ledger_study` refused (`DuplicatedStudyError`).
5. **Session days missing from `returns_series` silently dropped (`returns_matrix.py`):**
   `daily_series` filtered `kept = [key for key in keys if key in open_days]` and checked only `if not kept`, without checking `open_days - set(keys)`. Any session day in `session_days` absent from `returns_series` was silently dropped from the trial's calendar, letting trials rank on fewer sessions `T` than recorded.

---

## 2. What Changed and Why

* **`src/canon_trading/backtest.py`:**
  - Added `bars: list[Bar]` to `BacktestRun`, populated before `node.dispose()` from `cache.bar_types(AggregationSource.EXTERNAL)` and `reversed(cache.bars(bar_type) or ())` (`Cache.bars` returns reverse chronological order; `AggregationSource.EXTERNAL` matches the catalog bars replayed by the engine, excluding any internally aggregated bars).
  - Configured `BacktestEngineConfig(..., cache=CacheConfig(bar_capacity=BAR_CAPACITY))` with `BAR_CAPACITY = 1_000_000` (the maximum `CacheConfig` accepts at NautilusTrader v2.0.0rc8, up from the 10,000 default) so long runs keep their bars in cache without evicting earlier bars.
  - Replaced the `any(...)` overlap check in `backtest_run_config` with full range coverage verification when `start` or `end` is given: after rejecting an inverted range (`low > high`, where `get_missing_intervals_for_request` returns `[]`), `catalog.get_missing_intervals_for_request(low, high, NautilusDataType.Bar, bar_type)` is called and any missing intervals raise `LookupError` before a trial is asked for, naming the missing UTC ISO ranges and `ParquetDataCatalog.consolidate_data` for closing barless stretches between loads.
* **`src/canon_trading/ledger.py`:**
  - Replaced post-run `bars_read` with `_bars_replayed(run, bar_types, sample)`, which reads `run.bars` and raises `RuntimeError` (caught by `record_trial`'s `try` block and told `FAIL` with `outcome = "failed"` before `data_digest` or `session_days` is written) if `len(bars) != run.result.iterations`, if any cached bar belongs to an unexpected bar type, or if any bar's `ts_init` falls outside `[sample.start, sample.end]`.
  - Sorted bars in `data_digest` by `(str(bar.bar_type), bar.ts_init, bar.ts_event)` so digests follow ADR 0041 term 3 regardless of input sequence order.
  - Added `_unfinished_creation(study)` (no `user_attrs`, no trials, single `StudyDirection.MAXIMIZE` direction): `samples(study)` raises `LedgerNotFound` noting that `create_ledger_study` completes an interrupted creation, and `create_ledger_study` (under the existing exclusive `flock`) catches `DuplicatedStudyError`, verifies `_unfinished_creation(study)` (re-raising for any completed or foreign study), writes `samples`, and returns the study.
  - Added `is_sample_number(trial, numbers)` checking `isinstance(sample, int) and not isinstance(sample, bool) and sample in numbers`.
* **`src/canon_trading/ranking.py` and `src/canon_trading/variant_study.py`:**
  - In `ranking.rank`, read `all_samples = samples(study)` and stop on any record that `_ran(t)` without a valid sample number of the study (`not is_sample_number(t, sample_numbers)`), naming the key, trial, outcome, and invalid `sample` value.
  - Read keyed records that did *not* run (`not _ran(t)`) and lack a valid study sample number as belonging to the current sample, and check `unresolved` across `every` record of the key so a keyed failure before `sample` was written stops the ranking until a later record of that key runs to the end.
  - Applied the same `is_sample_number` rule in `variant_study._to_record`.
* **`src/canon_trading/returns_matrix.py`:**
  - In `daily_series`, rejected off-midnight entries in `session_days`, preserved `"no return falls on a session day"` when `open_days` is empty, and rejected non-empty `missing_days = sorted(open_days - set(keys))` with `ReturnsRejected(f"{name}: no return on {len(missing_days)} of its {len(open_days)} session days, the first {first_missing}")`.
* **Documentation & Records (`README.md`, `docs/adr/0035-*.md`, `docs/adr/0041-*.md`) and New Tests (`tests/test_gate_contracts.py`):**
  - Updated `README.md` (`run.bars`), ADR 0035 term 3 + Evidence, and ADR 0041 terms 1, 2, 3, 5 + Evidence so no comment, README, or ADR in this checkout states the superseded catalog post-query or partial-overlap behavior.
  - Added `tests/test_gate_contracts.py` covering boundary cases without modifying any existing test file.

---

## 3. Upstream APIs and Local Contracts Followed

* **NautilusTrader v2.0.0rc8** (verified in `.venv/lib/python3.14/site-packages/nautilus_trader/{backtest,common,persistence}/__init__.pyi` and runtime execution):
  - `BacktestNode.get_engine_cache`, `Cache.bar_types(AggregationSource.EXTERNAL)`, and `Cache.bars(bar_type)` (documented as reverse chronological order by `ts_event`, index 0 most recent, `None` when empty).
  - `CacheConfig(bar_capacity=1_000_000)` on `BacktestEngineConfig(cache=...)` (validated range `[1, 1_000_000]`, default `10_000`).
  - `BacktestResult.iterations` (count of external data events replayed by the engine).
  - `ParquetDataCatalog.get_intervals`, `ParquetDataCatalog.get_missing_intervals_for_request`, and `ParquetDataCatalog.consolidate_data`.
* **Optuna v5.0.0** (verified in `.venv/lib/python3.14/site-packages/optuna/study/study.py`):
  - `optuna.create_study` (raises `DuplicatedStudyError`), `optuna.load_study`, `Study.directions` (`StudyDirection.MAXIMIZE`), `Study.user_attrs`, `Study.set_user_attr`, `Study.get_trials(deepcopy=False)`, and `Study.ask` / `Study.tell`.
* **Repository Contracts:**
  - ADR 0010 (steps 1–3), ADR 0031, ADR 0035 (terms 1–6), and ADR 0041 (terms 1–6).

---

## 4. Verification Run and Results

Executed offline in `<tree>`:

1. `mise exec -- uv run --locked pytest`
   - **Result:** Exit `0` — `520 passed, 24 skipped in 101.52s` (all 10 reproduction test functions / 14 parametrized items pass, all 499 pre-existing offline tests pass, and all 7 new tests in `tests/test_gate_contracts.py` pass; the 24 skipped tests are the `paper` tests).
2. `mise exec -- uv run --locked ruff check .`
   - **Result:** Exit `0` — `All checks passed!`
3. `mise exec -- uv run --locked ruff format --check .`
   - **Result:** Exit `0` — `112 files already formatted`
4. `mise exec -- uv run --locked ty check`
   - **Result:** Exit `0` — `All checks passed!`
5. `./scripts/canon lint`
   - **Result:** Exit `0` — `canon: lint clean`

---

## 5. Uncertainties and Limits

* **Runs exceeding `1,000,000` bars of a single external bar type:** `CacheConfig.bar_capacity` rejects values above `1_000_000` (`ValueError: invalid bar_capacity: must be in range [1, 1000000]`). If a single trial replays more than `1,000,000` bars of one bar type, the cache evicts older bars and `_bars_replayed` fails the trial explicitly (`len(bars) != run.result.iterations`) rather than recording a partial digest. Behavior of `BacktestEngineConfig.streaming` for larger runs was not read (`UNVERIFIED`).
* **Paper tests and `scripts/ci`:** Per task instructions, no paper tests, credentials, or network calls were run, and `scripts/ci` was not used as a gate because this isolated copy omits repository metadata it expects.
