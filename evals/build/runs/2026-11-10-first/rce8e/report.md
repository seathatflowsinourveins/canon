All six reported defects are fixed in the working tree. The six supplied regression tests pass, all 455 offline tests pass, and all three required code checks pass. No existing test file, dependency declaration, or lockfile was changed.

Verified on 2026-11-11 with the checkout's pinned packages and Python 3.14.10.

| Defect | Change and reason | Contract followed |
| --- | --- | --- |
| Simultaneous creation of one ledger raced during table initialization. | `create_ledger_study` takes an exclusive lock on a persistent adjacent `.lock` file before constructing storage, creating the study, and recording its sample. It releases the lock before yielding the study, so callers can keep studies open and run independently. Keeping the lock file avoids separate lock identities for waiting creators. | Optuna 5.0.0 `RDBStorage.__init__` initializes tables and `_VersionManager` initializes version metadata; Python's `fcntl.flock` and the installed `flock(2)` manual specify exclusive acquisition and unlocking. |
| Recorded times were naive local times and changed across zones. | Convert every trial start through `astimezone(UTC)` before sorting and calling `isoformat`, producing an explicit `+00:00` offset. | Optuna 5.0.0 `TrialModel.datetime_start` converts stored UTC into naive local time; Python 3.14.10 `datetime.astimezone` interprets a naive datetime in local time. |
| Direct matrix callers could supply NaN, which skfolio changed to zero. | Validate every input series for finite returns before constructing portfolios, and name every rejected series. | ADR 0031's finite-return requirement; skfolio 1.12.8 `_to_numpy_returns` permits NaN and `_nan_to_zero` replaces it with zero. |
| Trial scoring used a filtered subset even when the gate rejected the full series. | Compute the score from the same `daily_series` validator and session-indexed frame as ranking. Rejected series receive no value and state `FAIL`, retaining outcome `traded`; accepted series still use upstream's per-session Sharpe ratio. Move `DAY_NS` into the validation module and import it into the ledger to avoid a circular dependency while preserving `ledger.DAY_NS`. | ADRs 0010, 0031, 0035, and 0041; skfolio `Portfolio(X=series.to_frame(), weights=[1.0]).sharpe_ratio`; Optuna `Study.tell` accepts no value for `FAIL`. |
| `run_backtest_config` accepted an empty strategy list. | Refuse it before constructing a node, matching the existing configuration builder's requirement. | The surrounding NautilusTrader v2.0.0rc8 `BacktestNode` workflow builds a node, adds the supplied strategies with `add_strategy`, then runs it. |
| `ledger/` ignored matching directories anywhere. | Anchor the pattern as `/ledger/`, limiting it to the repository root. | Git's root-relative ignore pattern behavior, verified by the supplied `git check-ignore` regression. |

Updated the affected module documentation, ADR 0035, and `docs/architecture.md` to describe creation locking and rejected-series scoring. Added `tests/test_returns_matrix_nonfinite.py`, covering NaN and both infinities across multiple named series. The existing README remains accurate.

The upstream evidence was read offline from the installed pinned packages: `optuna/storages/_rdb/storage.py` and `models.py`; `skfolio/portfolio/_portfolio.py`, `portfolio/_base.py`, and `population/_population.py`; and `nautilus_trader/backtest/__init__.pyi`. Python's installed `fcntl` docstring and `_pydatetime.datetime.astimezone` implementation, and the installed `flock(2)` and `git(1)` manuals supplied the standard-library and Git contracts. The code headers name the relevant contracts; no upstream API was invented.

Verification results:

- Before changes, the six supplied test selectors reproduced **6 failures** in 8.56 seconds.
- After changes, those same selectors produced **6 passes** in 10.48 seconds, including all three rounds of simultaneous two-process ledger creation and the three-zone timestamp check.
- The complete offline suite produced **455 passes** in 71.07 seconds using:

  ```sh
  TMPDIR="$PWD/.pytest_cache/task-tmp" \
  UV_CACHE_DIR="$PWD/.pytest_cache/uv-cache" \
  GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache/task-tmp" \
  mise exec -- uv run --locked pytest -q --tb=short --ignore-glob="**/test_paper_*.py"
  ```

- `mise exec -- uv run --locked ruff check .`: **passed**.
- `mise exec -- uv run --locked ruff format --check .`: **passed**, 107 files already formatted.
- `mise exec -- uv run --locked ty check`: **passed**.
- `scripts/canon lint`: **clean**.
- `git diff --check`: **passed**.

The first full-suite run had 454 passes and one environment-related failure: placing temporary files inside this checkout made the test's simulated outside-checkout module discover the enclosing Git repository. I kept temporary files inside the permitted checkout and used Git's documented `GIT_CEILING_DIRECTORIES` boundary to isolate that temporary area, then reran the full suite successfully. No application code or existing test was changed to accommodate that environment issue. The final suite log is `tree/.pytest_cache/offline-suite.log`.

Paper tests were excluded during collection, so they read no credentials and made no account calls. Only `uv` fetching the locked packages used the external network. `scripts/ci` and `scripts/land` were not run; no agents, jobs, commits, pushes, or pull requests were created.

Nothing remains for the six defects within the requested offline scope. Other operating systems, network filesystems, and paper execution are **UNVERIFIED** in this session; the lock was exercised with local SQLite on macOS. The changes are uncommitted and left in the working tree as requested.
