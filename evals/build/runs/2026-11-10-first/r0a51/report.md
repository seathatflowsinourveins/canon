# Fix Report: Trial Ledger, Returns Matrix, Backtest Config, and `.gitignore`

## Summary

All six confirmed defects in `src/canon_trading/` and `.gitignore` have been resolved. All six reproducing tests, seven added edge-case tests in `tests/test_evaluation_edges.py`, all existing offline tests across the workspace, `ruff check .`, `ruff format --check .`, `ty check`, and `scripts/canon lint` pass. No existing test file or lockfile was edited, and no dependency was added.

---

## Defects, Changes, and Upstream Contracts

### 1. Concurrent creation race on a new ledger file

* **What was wrong**: `canon_trading.ledger.create_ledger_study` opened `RDBStorage(_sqlite_url(path, "rwc"), skip_table_creation=False)` with no cross-process lock. In Optuna 5.0.0 (`optuna/storages/_rdb/storage.py`), `RDBStorage.__init__` runs `models.BaseModel.metadata.create_all(self.engine)` (non-atomic `PRAGMA table_info` followed by `CREATE TABLE`), `_VersionManager._init_version_info_model()` (`SELECT` then `INSERT`), and `_VersionManager._init_alembic()` (checks `alembic_version` on one connection and stamps it on another). Two processes creating one new SQLite ledger file at the same moment raced on table creation (`sqlite3.OperationalError: table studies already exists`) or version stamping (`sqlalchemy.orm.exc.StaleDataError`).
* **What changed and why**: Added `_creation_lock(path)` in `src/canon_trading/ledger.py`, acquiring an exclusive `fcntl.flock(..., fcntl.LOCK_EX)` on a sidecar file `path.with_name(f"{path.name}.lock")` opened for writing (`"ab"`) and kept on disk. `create_ledger_study` holds `_creation_lock(path)` while entering `_storage(path, "rwc")` (kept open in an `ExitStack`), calling `optuna.create_study(...)`, and writing `study.set_user_attr(SAMPLES, ...)`, releasing the creation lock before `yield study` so concurrent callers are not blocked for the duration of each other's `with` blocks. A sidecar file is used instead of locking `trials.db` itself because on macOS (`man 2 fcntl`), BSD `flock(2)` and SQLite's POSIX `fcntl(F_SETLK)` locks share the kernel vnode lock table and conflict within the same process (`sqlite3.OperationalError: database is locked`, reproduced in `.venv`). `open_study` does not touch `<ledger>.lock`, preserving its guarantee never to create a file on disk. Updated module docstrings and `docs/adr/0035-trial-ledger-in-an-optuna-study.md` term 1.
* **Upstream API / contract**: Python 3.14 `fcntl.flock`, macOS `flock(2)` and `fcntl(2)`, `lockf(1)` (`-w` write descriptor for exclusive locks, `-k` keeping the lock file on disk so unlinking cannot split locks across inodes), Optuna 5.0.0 `RDBStorage` (`optuna/storages/_rdb/storage.py`), and ADR 0035 term 1 / ADR 0041 term 1.

### 2. Naive local timestamps in `RankingReport` (`first_recorded` and `last_recorded`)

* **What was wrong**: `canon_trading.ranking.rank` formatted `trial.datetime_start.isoformat()` directly. In Optuna 5.0.0 (`optuna/storages/_rdb/models.py`, `TrialModel.datetime_start`), `_datetime_start_utc` is stored in SQLite as naive UTC and returned via `self._datetime_start_utc.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)` — a naive `datetime` in the reading process's local `TZ` (`InMemoryStorage` and `JournalStorage` likewise produce naive local `datetime` values). Consequently, `first_recorded` and `last_recorded` lacked a UTC offset and read differently across time zones.
* **What changed and why**: In `src/canon_trading/ranking.py`, converted each non-`None` `trial.datetime_start` with `.astimezone(UTC)` before sorting `starts` and calling `.isoformat()`. Python's `datetime.astimezone(UTC)` treats a naive `datetime` as system local time (reversing Optuna's `.astimezone().replace(tzinfo=None)`) and converts both naive and aware `datetime` values to UTC with `+00:00`, preserving microsecond precision. Documented Optuna's `TrialModel.datetime_start` behaviour in `ranking.py`'s header.
* **Upstream API / contract**: Optuna 5.0.0 `TrialModel.datetime_start` (`optuna/storages/_rdb/models.py`) and `FrozenTrial.datetime_start` (`optuna/trial/_frozen.py`), Python 3.14 `datetime.datetime.astimezone` and `datetime.UTC`, and ADR 0010 Confirmation.

### 3. `returns_matrix`, called directly, accepting non-finite returns

* **What was wrong**: `daily_series` refused non-finite returns, but `returns_matrix(series)` passed each `pd.Series.to_frame()` directly to `skfolio.Portfolio`. In addition, `returns_matrix.py`'s module docstring falsely claimed that `skfolio`'s `Portfolio` raises on a non-finite return; in `skfolio` 1.12.8 (`skfolio/portfolio/_portfolio.py`), `_to_numpy_returns` calls `sklearn.utils.validation.check_array(..., ensure_all_finite="allow-nan")` (raising sklearn's bare `ValueError` on `inf`/`-inf` while allowing `NaN`) and `_nan_to_zero` silently replaces `NaN` with `0.0`.
* **What changed and why**: Corrected the module docstring in `src/canon_trading/returns_matrix.py`, refused unnamed (`name is None`) series with `ValueError` and empty series with `ReturnsRejected`, and checked every element of each input `pd.Series` with `_is_finite_return` (`isinstance(value, numbers.Real)` excluding `bool`, and `math.isfinite(value)`) before building `Population([Portfolio(...)])`. Every series containing a non-finite value is collected in order and refused with `ReturnsRejected` (`f"{name}: a return that is not finite ({value} on ...)"` and `names=[...]`).
* **Upstream API / contract**: `skfolio` 1.12.8 `Portfolio` (`skfolio/portfolio/_portfolio.py`, `_to_numpy_returns` and `_nan_to_zero`), Python 3.14 `math.isfinite` and `numbers.Real`, `pandas.Series`, and ADR 0010 step 2 / ADR 0031 / ADR 0041.

### 4. Trial value told for returns rejected by the gate

* **What was wrong**: `_session_sharpe` in `src/canon_trading/ledger.py` sliced `[returns[day] for day in days if day in returns]` directly instead of building the series with `canon_trading.returns_matrix.daily_series`. When a traded run produced returns that `daily_series` rejects (such as non-zero returns on closed days, off-midnight keys from NautilusTrader's per-position fallback, gaps between UTC days, or a non-finite return on a closed day), `record_trial` still computed a finite Sharpe ratio on the remaining slice and told Optuna `COMPLETE` with that value instead of `FAIL` with `value=None`.
* **What changed and why**: Defined `DAY_NS` in `src/canon_trading/returns_matrix.py` (re-exported in `ledger.py` so existing `canon_trading.ledger.DAY_NS` imports stay intact with an acyclic import graph) and updated `_session_sharpe(key, returns, days)` in `src/canon_trading/ledger.py` to build `series = daily_series(key, returns, days)`. On `ReturnsRejected` or `len(series) < 2`, `_session_sharpe` returns `math.nan`; otherwise it returns `float(Portfolio(X=series.to_frame(), weights=[1.0], name=key).sharpe_ratio)`, matching `ranking.py` identically. `record_trial` then tells `TrialState.FAIL` (with no value and `outcome="traded"`) whenever `sharpe` is not finite. Updated docstrings in `ledger.py` and `ranking.py`, step 3 in `docs/architecture.md`, and term 2 in `docs/adr/0035-trial-ledger-in-an-optuna-study.md`.
* **Upstream API / contract**: Optuna 5.0.0 `Study.tell` (`optuna/study/study.py` and `optuna/study/_tell.py`: `TrialState.FAIL` takes `values=None`), `skfolio` 1.12.8 `Portfolio.sharpe_ratio` (`skfolio/portfolio/_base.py`), NautilusTrader v2.0.0rc8 `calculate_daily_returns` (`crates/analysis/src/analyzer.rs`), and ADR 0010 step 2 / ADR 0031 / ADR 0035 term 2.

### 5. `run_backtest_config` running when given no strategy

* **What was wrong**: `backtest_run_config` refused an empty `strategies` sequence, but `run_backtest_config(config, strategies)` in `src/canon_trading/backtest.py` did not check `strategies` before building `BacktestNode([config])` and calling `node.run()`. In NautilusTrader v2.0.0rc8, strategies are attached after `node.build()` via `node.add_strategy(config.id, strategy)`, so calling `node.run()` with zero strategies replays `config`'s bars with nothing trading them and returns an empty `BacktestResult`.
* **What changed and why**: Added `if not strategies: raise ValueError("run_backtest_config needs at least one strategy")` at the top of `run_backtest_config` before constructing `BacktestNode([config])`, and documented the refusal in `run_backtest_config`'s docstring.
* **Upstream API / contract**: NautilusTrader v2.0.0rc8 `nautilus_trader.backtest.BacktestNode` (`concepts/backtesting/apis-and-runs.md`, `BacktestNode.add_strategy` and `BacktestNode.run`), matching `backtest_run_config`.

### 6. `.gitignore` matching `ledger/` anywhere in the tree

* **What was wrong**: Line 21 of `.gitignore` was `ledger/`, which has a trailing slash (matching directories only) but no leading or middle slash, causing git to ignore any directory named `ledger` at any depth in the repository (for example `src/canon_trading/ledger/module.py`).
* **What changed and why**: Changed `ledger/` to `/ledger/` in `.gitignore` so only `<root>/ledger/` (including `trials.db` and `trials.db.lock`) is ignored.
* **Upstream API / contract**: Git `gitignore(5)` ("PATTERN FORMAT": a leading `/` anchors the pattern to the `.gitignore` directory; a trailing `/` matches directories only), and ADR 0012 / ADR 0035 term 1.

---

## Verification Run

All commands were executed offline in `<tree>`:

1. **Initial reproduction before edits**:
   * `mise exec -- uv run --locked pytest tests/test_backtest.py::test_a_run_config_run_without_a_strategy_is_refused tests/test_ledger.py::test_the_ledger_folder_is_ignored_only_at_the_root tests/test_ledger.py::test_the_value_told_is_none_for_a_series_the_gate_rejects tests/test_ledger.py::test_two_processes_creating_one_new_ledger_both_succeed tests/test_ranking.py::test_the_recorded_range_is_utc_whatever_the_zone tests/test_returns_matrix.py::test_a_return_that_is_not_finite_is_refused_when_called_directly`
   * Result: **6 failed** (reproduced all six defects).
2. **Six reproducing tests after edits**:
   * Same command as above.
   * Result: **6 passed in 5.84s** (exit code 0).
3. **Full workspace offline test suite**:
   * `mise exec -- uv run --locked pytest` (before adding `tests/test_evaluation_edges.py`: **452 passed, 21 skipped**; paper tests skipped cleanly with no credentials) and `mise exec -- uv run --locked pytest tests/` (with `tests/test_evaluation_edges.py`: **217 passed, 2 skipped** — **459 passed, 21 skipped** total across `tests/` and `packages/`).
4. **Linter, formatter, type checker, and canon lint**:
   * `mise exec -- uv run --locked ruff check .` -> **All checks passed!** (exit code 0).
   * `mise exec -- uv run --locked ruff format --check .` -> **107 files already formatted** (exit code 0).
   * `mise exec -- uv run --locked ty check` -> **All checks passed!** (exit code 0).
   * `scripts/canon lint` -> **canon: lint clean** (exit code 0).

---

## Uncertainties and Known Limits

* **Autumn DST fall-back overlap on Optuna's `FrozenTrial.datetime_start`**: Because Optuna 5.0.0's `TrialModel.datetime_start` (`optuna/storages/_rdb/models.py`) converts `_datetime_start_utc` to local time and strips `tzinfo` via `.replace(tzinfo=None)` (leaving `fold=0`), two distinct UTC instants separated by one hour during an autumn DST fall-back overlap in a DST-observing local zone produce identical naive local `datetime` values on `FrozenTrial.datetime_start`. Reading in `TZ=UTC` has no overlap, whereas reading in a DST zone cannot distinguish the two folds through Optuna's public `Study.trials` API without reading Optuna's private SQLite table directly.
* **Non-local filesystems for `fcntl.flock`**: `_creation_lock` was verified on macOS APFS (`flock(2)`). Behaviour over NFS or non-POSIX filesystems was not tested (`UNVERIFIED`); ADR 0035 term 1 scopes `RDBStorage` on SQLite to a local file (`JournalFileBackend` is Optuna's documented store for NFS).
* **Paper tests and full `scripts/ci`**: Not run per task instructions (`UNVERIFIED`; paper tests require credentials and network access, and `scripts/ci` expects repository files omitted from this checkout copy).
