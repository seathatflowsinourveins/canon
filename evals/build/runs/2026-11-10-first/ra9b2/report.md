# Report: Six Defects in the Trial Ledger, Returns Matrix, Backtest Runner, Gate, and `.gitignore`

## 1. What Was Wrong

1. **Concurrent creation of one new SQLite ledger file raced (`src/canon_trading/ledger.py`).**
   `create_ledger_study` opened `RDBStorage(_sqlite_url(path, "rwc"))` with no cross-process synchronisation. In Optuna 5.0.0 (`optuna/storages/_rdb/storage.py`), `RDBStorage.__init__` calls SQLAlchemy's `models.BaseModel.metadata.create_all(self.engine)` and `_VersionManager` (`_init_version_info_model` and `_init_alembic`). When two processes entered `create_ledger_study` on the same new file at once, both inspected an empty database before either committed DDL and both issued `CREATE TABLE studies (...)`, crashing the second with `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) table studies already exists`.

2. **Recorded timestamps in `RankingReport` were naive local times (`src/canon_trading/ranking.py`).**
   In Optuna 5.0.0 (`optuna/storages/_rdb/models.py`, `TrialModel.datetime_start`), SQLite stores `_datetime_start_utc` as naive UTC and the `datetime_start` getter converts it to naive local time via `_datetime_start_utc.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)`. `rank` sorted `trial.datetime_start` and called `.isoformat()` directly, emitting naive local strings without `+00:00` that changed whenever `TZ` changed.

3. **`returns_matrix`, called directly, accepted non-finite returns (`src/canon_trading/returns_matrix.py`).**
   `daily_series` checked `math.isfinite`, whereas `returns_matrix` passed its input `pd.Series` straight into `skfolio.Portfolio`. In skfolio 1.12.8 (`skfolio/portfolio/_portfolio.py`), `_to_numpy_returns` validates returns with `ensure_all_finite="allow-nan"` (raising a bare `ValueError` on `±inf` rather than `ReturnsRejected`) and then calls `_nan_to_zero`, silently turning `NaN` inside a series into `0.0` before `Population.returns_df` ran. Consequently `matrix.isna()` only caught dates absent from a series' index and let `NaN` pass as a `0.0` return. The module header in `returns_matrix.py` also falsely claimed that skfolio's `Portfolio` raises on any non-finite return.

4. **The value told for a trial ignored ADR 0010 step 2 (`src/canon_trading/ledger.py`).**
   `_session_sharpe` sliced `[returns[day] for day in days if day in returns]` directly and passed those numbers to `Portfolio`, bypassing `returns_matrix.daily_series`. When a traded run's `returns_series` failed ADR 0010 step 2 (for instance, non-zero returns on days absent from `session_days`, keys off UTC midnight, or missing UTC days), `record_trial` still told the trial `COMPLETE` with a finite Sharpe ratio instead of `FAIL` (`outcome="traded"`, `value=None`).

5. **`run_backtest_config` ran an empty strategy list instead of refusing it (`src/canon_trading/backtest.py`).**
   `backtest_run_config` checked `if not strategies`, whereas `run_backtest_config(config, strategies)` built `BacktestNode([config])`, added zero strategies, replayed every bar in `config`, and returned an empty `BacktestRun`. It also failed to check that `strategies` were on `ALPACA`, used one bar type per instrument, and subscribed only to bar types loaded by `config.data`.

6. **`.gitignore` ignored `ledger/` at any depth (`.gitignore`).**
   Line 21 of `.gitignore` was `ledger/`. Under `gitignore(5)`, a pattern whose only slash is a trailing slash has no directory separator at the beginning or middle and therefore matches a directory named `ledger` anywhere in the tree (such as `src/canon_trading/ledger/`), not only at the repository root.

---

## 2. What Was Changed, and Why That Way

* **`.gitignore`**
  * Changed `ledger/` to `/ledger/` so git ignores the root `ledger/` directory (including `ledger/trials.db` and `ledger/trials.db.lock`) and no `ledger/` directory deeper in the tree.

* **`src/canon_trading/backtest.py`**
  * Extracted `_strategy_bar_types(strategies, caller)` and called it in both `backtest_run_config` and `run_backtest_config` after materialising `strategies = list(strategies)`.
  * `run_backtest_config` now refuses an empty `strategies` (`ValueError: run_backtest_config needs at least one strategy`), a strategy on another venue, two bar types for one instrument, or a strategy whose `bar_type` is not in `config.data`, before constructing `BacktestNode`.

* **`src/canon_trading/returns_matrix.py`**
  * Defined `DAY_NS = 86_400 * 1_000_000_000` in `returns_matrix.py` (instead of importing it from `.ledger`) so `ledger.py` can import `DAY_NS`, `ReturnsRejected`, and `daily_series` from `.returns_matrix` with an acyclic import graph (`returns_matrix` → `ledger` → `ranking`) while keeping `canon_trading.ledger.DAY_NS` intact.
  * Added `_is_finite_return` and checked every element of every input `pd.Series` in `returns_matrix` before building `Population([Portfolio(...)])`. Every series holding a non-finite value (`NaN`, `±inf`, `None`, booleans, non-numeric objects) is collected in order and refused together via `ReturnsRejected("\n".join(bad_lines), names=bad_names)` with message `<name>: a return that is not finite (<value> on <day>)`.
  * Corrected the module header and `returns_matrix` docstring to state accurately how `skfolio.Portfolio` treats `±inf` and `NaN`.

* **`src/canon_trading/ledger.py`**
  * In `create_ledger_study`, acquired an exclusive advisory lock (`fcntl.flock(lock, fcntl.LOCK_EX)`) on companion file `<path>.lock` (`path.with_name(f"{path.name}.lock")`, opened in `"ab"` mode and kept on disk). Held the lock across `_storage(path, "rwc")` initialization, `optuna.create_study`, and `study.set_user_attr(SAMPLES, ...)`, and released it before `yield study` while keeping `_storage` registered on an outer `ExitStack` so `remove_session()` and `engine.dispose()` still run when the `with` block exits.
  * Why a companion `<path>.lock` released before `yield`: on macOS (XNU), `flock(2)` and SQLite's POSIX `fcntl(F_SETLK)` byte-range locks share the same inode lock table, so taking `flock` on `trials.db` itself causes SQLite in the same process to fail with `database is locked` (verified here by running it). Releasing `<path>.lock` once the tables, study, and initial `samples` attribute are committed prevents self-deadlock when a single process nests `create_ledger_study` calls on one `ExitStack` and avoids holding an exclusive lock while callers run backtests inside the `with` block. `open_study` touches no lock file and still creates nothing.
  * Updated `_session_sharpe(key, returns, days)` to validate and extract session returns via `daily_series(key, returns, list(days))`, returning `math.nan` if `ReturnsRejected` is raised or `len(series) < 2`, and otherwise returning `float(Portfolio(X=series.to_frame(), weights=[1.0], name=key).sharpe_ratio)`. A traded run whose series the gate rejects at step 2 now gets `sharpe = nan`, so `record_trial` tells it `TrialState.FAIL` (`value=None`) with `outcome="traded"`.
  * Updated the module header and `create_ledger_study`, `record_trial`, and `_session_sharpe` docstrings.

* **`src/canon_trading/ranking.py`**
  * Converted `trial.datetime_start` with `.astimezone(UTC)` before sorting (`starts = sorted(trial.datetime_start.astimezone(UTC) for trial in current if trial.datetime_start is not None)`), so `first_recorded` and `last_recorded` are always ISO-8601 UTC strings ending in `+00:00`, invariant to `TZ`, and safe even if naive and aware `FrozenTrial` timestamps mix.
  * Updated the module header and `_ran` docstring.

* **`docs/architecture.md` and `docs/adr/0035-trial-ledger-in-an-optuna-study.md`**
  * Updated step 3 of "A backtest, from catalog to gate" in `docs/architecture.md` and term 2 + `## Evidence` in `docs/adr/0035-trial-ledger-in-an-optuna-study.md` so neither states that `record_trial` computes the told Sharpe ratio over session days without ADR 0010 step 2's check.

* **`tests/test_ledger_and_matrix_edges.py` (new test file; no existing test file modified)**
  * Added 10 offline tests covering `±inf`/`None` and multi-series rejections in `returns_matrix`, fresh-process import order (`returns_matrix` before `ledger`), concurrent `create_ledger_study` races on an identical study name (`DuplicatedStudyError` vs zero schema crashes), end-to-end `record_trial` → `rank` stop on step-2 rejected returns, `run_backtest_config` refusal of unloaded bar types and empty iterators, aware non-UTC `datetime_start` normalization in `rank`, and root-only `.gitignore` anchoring including `ledger/trials.db.lock`.

---

## 3. Upstream APIs and Contracts Followed

* **Optuna 5.0.0** (`.venv` pinned `v5.0.0`):
  * `optuna/storages/_rdb/storage.py` (`RDBStorage.__init__`, `_VersionManager`): `BaseModel.metadata.create_all` and Alembic version setup are non-atomic across simultaneous creators of a new SQLite file.
  * `optuna/storages/_rdb/models.py` (`TrialModel.datetime_start`): converts stored naive UTC to naive local time via `.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)`; `Study.tell(..., state=TrialState.FAIL)` records `FAIL` with `value=None`.
* **Python 3.14 standard library & macOS (`darwin`)**:
  * `fcntl.flock(..., fcntl.LOCK_EX)`, `flock(2)`, and `lockf(1)` (`-k` keep file so contenders lock one inode, `-w` open for writing).
  * `datetime.datetime.astimezone(datetime.UTC)`: interprets naive `datetime` instances as system local time (matching Optuna's `TrialModel.datetime_start` setter at `models.py:208` and `optuna/storages/journal/_storage.py:250`) and converts naive or aware `datetime` instances to aware UTC (`+00:00`).
* **skfolio 1.12.8** (`.venv` pinned `v1.12.8`):
  * `skfolio/portfolio/_portfolio.py` (`Portfolio`, `_to_numpy_returns`, `_nan_to_zero`) and `skfolio/portfolio/_base.py` (`Portfolio.sharpe_ratio`); ADR 0010 step 2, ADR 0031, ADR 0035, and ADR 0041.
* **NautilusTrader v2.0.0rc8** (`.venv` pinned `v2.0.0rc8`):
  * `nautilus_trader.backtest.BacktestNode` and `nautilus_trader.config.BacktestRunConfig` (`data` / `BacktestDataConfig.bar_types`).
* **Git (`gitignore(5)`)**:
  * Leading slash `/ledger/` anchors the directory pattern to the `.gitignore` root.

---

## 4. What Was Run, and Its Result

1. **Six reproducing tests**:
   ```sh
   mise exec -- uv run --locked pytest -q \
     tests/test_backtest.py::test_a_run_config_run_without_a_strategy_is_refused \
     tests/test_ledger.py::test_the_ledger_folder_is_ignored_only_at_the_root \
     tests/test_ledger.py::test_the_value_told_is_none_for_a_series_the_gate_rejects \
     tests/test_ledger.py::test_two_processes_creating_one_new_ledger_both_succeed \
     tests/test_ranking.py::test_the_recorded_range_is_utc_whatever_the_zone \
     tests/test_returns_matrix.py::test_a_return_that_is_not_finite_is_refused_when_called_directly
   ```
   Result: `6 passed in 8.71s` (baseline before changes: `6 failed`).

2. **Full offline test suite**:
   ```sh
   mise exec -- uv run --locked pytest
   ```
   Result: `462 passed, 21 skipped in 59.19s` (446 original passing + 6 fixed + 10 new edge-case tests; the 21 skipped tests are the credential-gated paper tests).

3. **Code checks and canon lint**:
   ```sh
   mise exec -- uv run --locked ruff check .
   mise exec -- uv run --locked ruff format --check .
   mise exec -- uv run --locked ty check
   ./scripts/canon lint
   ```
   Results:
   * `ruff check .`: `All checks passed!`
   * `ruff format --check .`: `107 files already formatted`
   * `ty check`: `All checks passed!`
   * `./scripts/canon lint`: `canon: lint clean`

---

## 5. Uncertainties and Upstream Limits

* **Local DST fall-back ambiguity in Optuna's `FrozenTrial.datetime_start`**:
  In Optuna 5.0.0 (`optuna/storages/_rdb/models.py:197-201`), `TrialModel.datetime_start` converts UTC to local time via `.astimezone().replace(tzinfo=None)`. Because `.astimezone()` with no argument attaches a fixed-offset `datetime.timezone` (`fold=0`), stripping `tzinfo` discards `fold=1` during the repeated hour of a local autumn DST transition. Since `rank(study: optuna.Study)` receives an `optuna.Study` and reads `FrozenTrial.datetime_start` through Optuna's public API rather than querying SQLite's private `_datetime_start_utc` column, `.astimezone(UTC)` is exact at every instant outside a local DST fall-back overlap, and can differ by one hour only for a trial started during the second pass of a local fall-back hour when read in a DST-observing zone.
* **`fcntl.flock` outside macOS local filesystems**:
  `<ledger>.lock` locking was verified with concurrent subprocesses on macOS (`darwin`). Behavior on Linux or over live NFSv4 mounts was not executed in this environment: `UNVERIFIED` (ADR 0035 term 1 notes Optuna's FAQ recommends `JournalFileBackend` for NFS).
* **Paper tests and full `scripts/ci`**:
  The 21 paper tests (`test_paper_*.py`) and `scripts/ci` checks that depend on files omitted from this standalone copy were not run per the task constraints: `UNVERIFIED`.
