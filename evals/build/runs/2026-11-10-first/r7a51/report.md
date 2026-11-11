# Defect Fix Report: Trial Ledger, Returns Matrix, Backtest Runner, Ranking Gate, and `.gitignore`

## Summary

Fixed all six confirmed defects across `src/canon_trading/` and `.gitignore`, updated the comments, docstrings, architecture overview, and ADR 0035 text whose statements changed, and added edge-case coverage in a new test module (`tests/test_defect_edges.py`) without modifying any pre-existing test file or adding any dependency.

---

## Defects, Changes, Rationale, and Upstream Contracts

### 1. Concurrent creation of a new ledger SQLite file raced and crashed (`src/canon_trading/ledger.py`)

* **What was wrong:** `create_ledger_study` opened `_storage(path, "rwc")` and called `optuna.create_study` directly. In Optuna 5.0.0 (`optuna/storages/_rdb/storage.py`, `RDBStorage.__init__`, and `optuna/storages/_rdb/alembic` `_VersionManager._init_alembic`), opening `RDBStorage` on a fresh SQLite database runs `BaseModel.metadata.create_all(self.engine)` followed by Alembic's `context.stamp(...)` without cross-process coordination. When two processes created studies on the same new `trials.db` simultaneously, they raced on DDL table creation and `INSERT INTO alembic_version`, causing one process to fail with `OperationalError` (`table studies already exists`) or `IntegrityError` (`UNIQUE constraint failed: alembic_version.version_num`).
* **What changed and why that way:** Added `_creation_lock(path)` in `src/canon_trading/ledger.py` using `fcntl.flock(fd, fcntl.LOCK_EX)` on a companion lock file `path.with_name(f"{path.name}.lock")` opened with `"a+b"`. In `create_ledger_study`, `_storage(path, "rwc")` is entered on an `ExitStack` while holding `_creation_lock(path)`, along with `optuna.create_study(...)` and `study.set_user_attr(SAMPLES, ...)`, and the lock is released before `yield study`:
  1. **Separate companion file, not `trials.db`:** Under POSIX `fcntl(2)`, closing *any* file descriptor for a file in a process releases all `F_SETLK` byte-range locks that process holds on that file—which includes SQLite's own database locks. Locking `trials.db.lock` avoids ever closing an extra descriptor on `trials.db`.
  2. **Kept on disk, never unlinked:** Unlinking a lock file lets a late arrival create and lock a second inode while an earlier holder still owns the unlinked inode (`lockf(1)` `-k` pattern). Because the root ledger directory `/ledger/` is git-ignored, `ledger/trials.db.lock` stays ignored alongside `ledger/trials.db`.
  3. **Released before `yield study`:** Releasing the creation lock once schema initialization, study row creation, and initial `samples` user-attribute setup finish prevents deadlocks when one process nests `create_ledger_study` calls on two studies in the same file, and lets trials run concurrently afterward under SQLite WAL + `busy_timeout`.
* **Upstream contract:** Optuna 5.0.0 `RDBStorage` (`optuna/storages/_rdb/storage.py`); SQLite URI filenames (`https://www.sqlite.org/uri.html`) and WAL locking; Python standard library `fcntl.flock` (`flock(2)`, `fcntl(2)`, `lockf(1)` `-k`).

### 2. Ranking report recorded range used naive local time instead of UTC (`src/canon_trading/ranking.py`)

* **What was wrong:** `rank()` formatted `first_recorded` and `last_recorded` from `trial.datetime_start.isoformat()`. Optuna 5.0.0's `RDBStorage` (`optuna/storages/_rdb/models.py`, `TrialModel.datetime_start`) stores UTC as a naive timestamp in SQLite and converts it on read via `.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)`, returning a **naive local** `datetime`. Consequently, `isoformat()` produced an offset-free local timestamp string that changed depending on `TZ`.
* **What changed and why that way:** Imported `datetime.UTC` in `src/canon_trading/ranking.py` and converted each `trial.datetime_start` with `.astimezone(UTC)` when building `starts`. In Python 3.14, `datetime.astimezone(UTC)` interprets naive `datetime` instances as system local time (matching what `RDBStorage` produced) and converts both naive-local and timezone-aware `datetime` instances (such as custom or future aware storages) to aware UTC (`+00:00`), so `.isoformat()` always emits UTC with `+00:00`.
* **Upstream contract:** Optuna 5.0.0 `FrozenTrial.datetime_start` (`optuna/trial/_frozen.py`) and `TrialModel.datetime_start` (`optuna/storages/_rdb/models.py`); Python 3.14 `datetime.datetime.astimezone` and `datetime.UTC`.

### 3. `returns_matrix` accepted non-finite returns when called directly (`src/canon_trading/returns_matrix.py`)

* **What was wrong:** `daily_series` checked finiteness on ledger dicts, but `returns_matrix(series)` passed caller-supplied `pd.Series` straight into `skfolio.Portfolio(X=s.to_frame(), weights=[1.0], ...)`. Inspection of installed `skfolio` 1.12.8 (`skfolio/portfolio/_portfolio.py`, `_to_numpy_returns` and `_nan_to_zero`) showed that `Portfolio` silently converts `NaN` returns to `0.0` (only rejecting an all-`NaN` asset) and raises a bare `ValueError("Input X contains infinity...")` on `±inf` instead of `ReturnsRejected` naming every offending series. The module docstring in `returns_matrix.py` also inaccurately claimed that `skfolio`'s `Portfolio` raises on a non-finite return.
* **What changed and why that way:**
  * Added `_is_finite_return(value)` using `numbers.Real` (excluding `bool`) and `math.isfinite(float(value))`—using only Python stdlib types rather than importing `numpy` directly, since `numpy` is not a direct upstream in `canon/upstreams.json` (`AGENTS.md` Rule 2).
  * Validated in `returns_matrix` that every series has a non-`None` distinct name (`ValueError`), is non-empty (`ReturnsRejected` with `names`), and contains only finite real returns (`ReturnsRejected` listing every offending series in order and populating `rejected.names`).
  * Defined `DAY_NS = 86_400 * 1_000_000_000` in `returns_matrix.py` (re-imported in `ledger.py`) so `returns_matrix` no longer has a circular import back into `ledger.py`.
  * Corrected the `returns_matrix.py` module and function docstrings to state `skfolio` 1.12.8's actual behavior (`_to_numpy_returns` and `_nan_to_zero`).
* **Upstream contract:** ADR 0010 step 2, ADR 0031, ADR 0041; `skfolio` 1.12.8 `Portfolio` and `Population` (`skfolio/portfolio/_portfolio.py`, `skfolio/population/_population.py`); `pandas` `Series` / `DataFrame`.

### 4. Trial value was computed without running `daily_series`, telling a value for gate-rejected series (`src/canon_trading/ledger.py`, docs)

* **What was wrong:** In `record_trial`, `_session_sharpe(returns, days)` took `[returns[day] for day in days if day in returns]` and computed `Portfolio(...).sharpe_ratio` directly. A run that traded whose `returns_series` failed the gate's series contract in `daily_series`—such as a non-zero return on a closed day (a day absent from `session_days`), a non-midnight timestamp (NautilusTrader's sub-day resampling fallback), a missing calendar day inside the span, or a non-finite return on a closed day—still received `TrialState.COMPLETE` and a numeric trial value, contradicting the gate (`daily_series`).
* **What changed and why that way:**
  * Changed `_session_sharpe(key, returns, days)` in `src/canon_trading/ledger.py` to build the series through `daily_series(key, returns, list(days))`, returning `math.nan` if `daily_series` raises `ReturnsRejected` or if `len(series) < 2`, and otherwise computing `float(Portfolio(X=series.to_frame(), weights=[1.0], name=key).sharpe_ratio)`.
  * When `sharpe` is `NaN`, `record_trial` keeps `outcome = "traded"` (because orders filled and the run completed to the end, so `rank` still sees it and stops rather than treating it as crashed or idle) and calls `study.tell(trial, state=TrialState.FAIL)` with no value (`trial.value is None`).
  * Updated `src/canon_trading/ledger.py`, `src/canon_trading/ranking.py` (`_ran`), `docs/adr/0035-trial-ledger-in-an-optuna-study.md` (term 2), and `docs/architecture.md` (step 3) so every description matches this behavior.
* **Upstream contract:** ADR 0010 step 2, ADR 0031, ADR 0035 terms 2 & 5; Optuna 5.0.0 `Study.tell(trial, state=TrialState.FAIL)` (`optuna/study/study.py`); `skfolio` 1.12.8 `Portfolio.sharpe_ratio` (`skfolio/portfolio/_base.py`).

### 5. `run_backtest_config` accepted an empty `strategies` sequence (`src/canon_trading/backtest.py`)

* **What was wrong:** `backtest_run_config` refused an empty `strategies` sequence, but `run_backtest_config(config, strategies)` built a `BacktestNode([config])`, attached zero strategies to `node.get_engine(config.id).trader`, and ran an empty simulation instead of refusing the call.
* **What changed and why that way:** Added `if not strategies: raise ValueError("run_backtest_config needs at least one strategy")` at the start of `run_backtest_config` before constructing `BacktestNode`, matching `backtest_run_config`'s guard and message style, and documented it in `run_backtest_config`'s docstring.
* **Upstream contract:** NautilusTrader v2.0.0rc8 `BacktestNode` / `Trader.add_strategies` (`nautilus_trader/backtest/__init__.pyi`), matching `backtest_run_config` in `src/canon_trading/backtest.py`.

### 6. `.gitignore` ignored `ledger/` anywhere in the repository tree (`.gitignore`)

* **What was wrong:** `.gitignore` contained `ledger/` with no leading slash. Under `gitignore(5)`, a pattern with no slash (other than a trailing slash) matches a directory of that name at *any* depth in the tree (for example `src/canon_trading/ledger/` or `tests/ledger/`), rather than only the local root `/ledger/` folder defined by ADR 0035.
* **What changed and why that way:** Anchored the rule at the repository root as `/ledger/` in `.gitignore` and updated the comment above it citing ADR 0035 and `gitignore(5)`.
* **Upstream contract:** Git `gitignore(5)` ("If there is a separator at the beginning or middle (or both) of the pattern, then the pattern is relative to the directory level of the particular `.gitignore` file itself"); ADR 0035.

### Additional Tests (`tests/test_defect_edges.py`)

Added `tests/test_defect_edges.py` (leaving all pre-existing test files untouched) covering:
* `returns_matrix` rejecting `±inf`, naming every offending series in order while omitting clean series;
* `returns_matrix` rejecting empty series, object-dtype `None` returns, and unnamed series;
* Nested `create_ledger_study` calls in a single process without self-deadlock, plus `DuplicatedStudyError` preservation;
* Concurrent two-process `create_ledger_study` on a missing nested parent directory with an identical study name (verifying one succeeds and one cleanly raises `DuplicatedStudyError` with no SQLite/Alembic crash);
* `record_trial` telling `TrialState.FAIL` with `value is None` and `outcome == "traded"` when returns hit sub-day fallback timestamps or calendar gaps;
* Clean import of `canon_trading.returns_matrix` and `canon_trading.ledger` in either order;
* `git check-ignore` ignoring `ledger/trials.db` and `ledger/trials.db.lock` at the root while not ignoring nested `ledger/` paths;
* `run_backtest_config` refusing an empty tuple `()`.

---

## Verification Run and Results

All offline tests and static/canon checks ran cleanly in this checkout:

1. **Named reproducing tests:**
   ```sh
   mise exec -- uv run --locked pytest \
     tests/test_backtest.py::test_a_run_config_run_without_a_strategy_is_refused \
     tests/test_ledger.py::test_the_ledger_folder_is_ignored_only_at_the_root \
     tests/test_ledger.py::test_the_value_told_is_none_for_a_series_the_gate_rejects \
     tests/test_ledger.py::test_two_processes_creating_one_new_ledger_both_succeed \
     tests/test_ranking.py::test_the_recorded_range_is_utc_whatever_the_zone \
     tests/test_returns_matrix.py::test_a_return_that_is_not_finite_is_refused_when_called_directly
   ```
   * **Result:** `6 passed in 6.29s` (exit 0; all 6 failed prior to the changes).

2. **Full offline pytest suite across `tests/` and `packages/`:**
   ```sh
   mise exec -- uv run --locked pytest
   ```
   * **Result:** `460 passed, 21 skipped in 61.53s` (exit 0; the 21 skipped tests are paper-trading tests gated on credentials).

3. **Linter, formatter, type checker, and offline canon lint:**
   ```sh
   mise exec -- uv run --locked ruff check .
   mise exec -- uv run --locked ruff format --check .
   mise exec -- uv run --locked ty check
   scripts/canon lint
   ```
   * **Result:**
     * `ruff check .`: `All checks passed!` (exit 0)
     * `ruff format --check .`: `107 files already formatted` (exit 0)
     * `ty check`: `All checks passed!` (exit 0)
     * `scripts/canon lint`: `canon: lint clean` (exit 0)

---

## Uncertainties and Upstream Boundaries

* **DST fall-back ambiguity in Optuna's `RDBStorage`:** Optuna 5.0.0's `TrialModel.datetime_start` (`optuna/storages/_rdb/models.py`) converts stored naive UTC to naive local time via `.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)`, discarding `datetime.fold`. For a trial recorded during a local DST fall-back overlap hour, Python's `datetime.astimezone(UTC)` defaults `fold=0`, so two instants an hour apart in that overlap window cannot be distinguished through `FrozenTrial.datetime_start` alone without bypassing Optuna's public API to query SQLite directly. For all non-ambiguous instants and every fixed-offset or non-DST zone, `.astimezone(UTC)` recovers the exact UTC instant.
* **Network filesystems and `flock(2)`:** `_creation_lock` relies on POSIX/BSD `fcntl.flock` (`flock(2)`), which serialises processes reliably on local filesystems (macOS and Linux). Its behavior on network filesystems (such as older NFS mounts) is `UNVERIFIED` here; SQLite WAL mode itself is unsupported over network filesystems, where Optuna's documentation recommends `JournalStorage` or a client-server RDB instead.
* **Checks intentionally not run:** Paper-trading tests (`mise run paper:test`), `scripts/canon check` (live network upstream comparison), and `scripts/ci` (which checks files omitted from this task checkout) were not run per the task instructions and `AGENTS.md`.
