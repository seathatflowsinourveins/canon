# Trial ledger, returns matrix and gate fixes

Implemented all six requested fixes without adding dependencies or changing any existing test file. Changes remain uncommitted in the supplied checkout; nothing was landed.

## Defects and changes

| Defect | Change and reason |
| --- | --- |
| Two processes race while initializing one new SQLite ledger. | `ledger._storage` takes an exclusive `fcntl.flock` on a persistent sibling `<filename>.lock` while constructing `RDBStorage`. The constructor performs table creation and schema-version initialization separately, so SQLite's individual transaction locks do not protect the whole setup. The lock is explicitly released before yielding the storage, including on constructor failure. Keeping the lock file avoids processes locking different inodes after an unlink. Opening an existing study creates no lock file. |
| Recorded trial ranges contain naive local timestamps. | `ranking.rank` converts each Optuna start timestamp with `astimezone(UTC)` before sorting. ISO formatting then includes `+00:00`, and reading the same ledger in Tokyo, UTC or New York produces the same range. Treating the naive timestamp as already UTC would change the instant. |
| Direct `returns_matrix` calls accept non-finite returns. | Validate every input series before constructing skfolio portfolios and raise `ReturnsRejected`, naming all offending series. This rejects NaN and both infinities before upstream can convert NaN to zero or raise its own error. |
| The trial's value comes from different validation than the gate uses. | `_session_sharpe` now uses `daily_series`, the same validator and session filtering the gate uses, and passes that series to `Portfolio`. A refused series gives no value and `TrialState.FAIL`, with outcome still `traded`. `DAY_NS` lives in `returns_matrix` and is imported by `ledger`, preserving existing imports while avoiding a circular dependency. |
| An empty strategy list is run through `run_backtest_config`. | Refuse it with `ValueError` before constructing a node, matching `backtest_run_config`'s existing requirement. Valid runs retain the documented build, add-strategy, run and dispose sequence. |
| `ledger/` ignores nested directories too. | Anchor the rule as `/ledger/`, which ignores only the root directory and its contents. |

Updated the affected module headers and docstrings, ADR 0035, the architecture description and the roadmap. They now describe initialization locking, values computed only from accepted daily series, UTC report timestamps and the completed follow-up fixes. No registry, dependency manifest or lockfile changed.

## Contracts and evidence checked offline

- **Optuna 5.0.0:** installed `optuna/storages/_rdb/storage.py`, especially `RDBStorage.__init__` and `_VersionManager`, establish the non-atomic initialization sequence. `optuna/storages/_rdb/models.py`, `TrialModel.datetime_start`, converts stored naive UTC into naive local time. `optuna/study/study.py`, `Study.tell`, and `optuna/study/_tell.py` require no objective value for `FAIL`. The code continues to use the public storage and ask/tell APIs.
- **skfolio 1.12.8:** installed `portfolio/_portfolio.py` documents and implements NaN-to-zero conversion. Its one-asset `Portfolio(X=series.to_frame(), weights=[1.0])` retains the series' returns; `Portfolio.sharpe_ratio` supplies the existing statistic. `Population.returns_df` remains responsible for joining the series.
- **Python 3.14.10:** the installed `fcntl.flock` docstring and local `flock(2)` manual specify exclusive advisory locks and explicit unlocking. The installed `_pydatetime.datetime.astimezone` implementation treats naive input as local time; `datetime.isoformat` documents the emitted offset.
- **NautilusTrader 2.0.0rc8:** installed `backtest/__init__.pyi` and `BacktestNode` method docstrings confirm the existing high-level node lifecycle. The change adds input validation before that lifecycle.
- **Git:** local `gitignore(5)` documents the leading slash's directory-relative scope; local `git(1)` documents `GIT_CEILING_DIRECTORIES` used for the test setup below.
- **Repository contracts:** read the applicable, unexpired ADRs 0010, 0031, 0035 and 0041 and the existing `daily_series` implementation and tests. The accepted daily portfolio series and failure outcomes are existing contracts, reused here.

## Verification

- Baseline: the six supplied regression test IDs all failed before implementation. After implementation, the same six passed in **5.30 seconds**, including three rounds of two processes initializing a fresh ledger together.
- Full offline suite: **452 passed in 59.90 seconds**, exit 0, using:

  ```sh
  GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache" mise exec -- uv run --locked pytest \
    --basetemp=.pytest_cache/tmp --ignore-glob='*test_paper_*.py' \
    --tb=short --show-capture=no
  ```

- `mise exec -- uv run --locked ruff check .`: **passed**, exit 0.
- `mise exec -- uv run --locked ruff format --check .`: **passed**, 106 files already formatted, exit 0.
- `mise exec -- uv run --locked ty check`: **passed**, exit 0.
- `scripts/canon lint`: **passed**, `canon: lint clean`, exit 0.
- `git diff --check`: **passed**, exit 0. `git status` shows only the eight intended source, ignore-rule and documentation changes; all existing tests are untouched.

Two test-setup issues were corrected transparently. The first focused run had two assertion failures and four fixture errors because `.pytest_cache/tmp` initially lacked its parent; after creating the parent, all six original defects reproduced. The first full run had **451 passed and one failure**, `test_outside_a_git_checkout_there_is_no_default_ledger`, because placing pytest's temporary directories inside this checkout made its intended outside-Git fixture part of the repository. Setting Git's discovery ceiling at `.pytest_cache` restored that fixture's intended semantics while keeping temporary writes inside the checkout; the isolated test and then all 452 offline tests passed without further production changes.

## Limits and remaining work

No implementation work remains for the six defects. No tests were added or edited. Paper tests were excluded from collection; no credentials, other repository copies, repository history, remote operations or landing commands were used. Package installation was limited to `uv run --locked` creating the environment from the existing lockfile.

`scripts/ci` was not run because the task identifies omitted files in this copy and specifies the offline pytest suite and three Python checks instead. Paper execution, network filesystems and non-POSIX locking behavior are **UNVERIFIED** in this task; the concurrency regression was verified on this macOS local filesystem. Upstream web pages and Rust source cited by existing records were not fetched during this offline task.
