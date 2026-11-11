# Trial ledger, returns matrix and gate defect repair

All six supplied regressions now pass. The final offline suite passed **456 tests**; Ruff lint,
Ruff formatting, ty and whitespace checks also passed. Changes remain uncommitted in the working
tree. No existing test file, dependency declaration or lockfile was changed.

## Defects and changes

| Defect | Change and reason |
|---|---|
| Two creators raced while Optuna created tables and initialised schema versions in a new ledger. | `_storage` serialises the `RDBStorage` constructor with Python's `fcntl.flock(LOCK_EX)` on a sibling `trials.db.lock` file. The file remains in place so contenders use the same inode. Its handle closes when construction finishes or raises, releasing the lock before a study is yielded. Opening a study creates no lock file. |
| The report formatted Optuna's naive local start times directly. | `rank` converts each start with `datetime.astimezone(UTC)` before sorting and formatting it, producing UTC ISO timestamps with `+00:00`. |
| Direct matrix callers could supply NaN returns that skfolio replaced with zero. | `returns_matrix` checks every input series before constructing any portfolio. It raises `ReturnsRejected` naming all offending series for NaN, infinities and pandas nullable missing values. The existing missing-calendar-day check still applies after the join. |
| The trial value used a separate, weaker session-day filter than the gate. | `_session_sharpe` now calls the gate's `daily_series` validator and computes skfolio's Sharpe ratio over that accepted series. Rejected series produce no objective value: the trial is told `FAIL` and retains outcome `traded`, as the supplied regression requires. |
| `run_backtest_config` accepted an empty strategy sequence. | It now raises `ValueError` before constructing a node, matching the existing requirement in `backtest_run_config`. |
| `ledger/` ignored directories with that name at any depth. | The pattern is now `/ledger/`, anchored at the checkout root. |

`DAY_NS` moved into `returns_matrix` and is imported by `ledger`, preserving `ledger.DAY_NS` while
allowing the ledger to use the shared validator without a circular import. No statistics were
reimplemented and no dependency was added.

The module headers now describe the verified contracts, including skfolio's NaN conversion. ADR 0035
and `docs/architecture.md` describe rejected-series trial values; ADR 0035 also records the creation
lock and root ignore rule. `docs/roadmap.md` no longer says these six defects remain unfixed.

The new `tests/test_returns_matrix_finite.py` adds four cases: NaN, positive infinity and negative
infinity with multiple offending columns, plus pandas nullable missing returns. Existing tests were
left untouched.

## Contracts checked offline

The relevant ADRs were read, including ADR 0031, ADR 0035 and ADR 0041; their review dates are
2026-12-10. Implementation evidence was read from the installed locked packages and local manuals:

* **Optuna 5.0.0:** `optuna/storages/_rdb/storage.py` shows table creation and schema-version
  initialisation in the constructor. `optuna/storages/_rdb/models.py` shows UTC storage converted to
  naive local `datetime_start` values. `Study.tell` documents that `FAIL` takes no value.
* **Python 3.14.10:** the `fcntl.flock` docstring and local `flock(2)` and `close(2)` manuals establish
  exclusive advisory locking and release on closing the handle. The installed `_pydatetime.py`
  implementation of `astimezone` interprets a naive datetime as local time and converts it to the
  requested zone.
* **skfolio 1.12.8:** `portfolio/_portfolio.py` allows missing asset returns and replaces them with
  zero before calculating portfolio returns; `portfolio/_base.py` documents the Sharpe ratio.
  `population/_population.py` joins returns by date through pandas. The existing one-asset portfolio
  contract remains in use for both the gate and recorded trial value.
* **pandas 3.1.1:** `Series.isna` documents detection of missing values; infinity is checked separately
  with `math.isfinite`.
* **NautilusTrader 2.0.0rc8:** the installed backtest stub and `BacktestNode.add_strategy` and `run`
  docstrings confirm the existing node lifecycle. The empty-strategy guard is the repository's existing
  trial precondition applied at its second entry point; it introduces no new upstream API.
* **Git:** local `gitignore(5)` documents root anchoring with a leading slash, and `git(1)` documents
  `GIT_CEILING_DIRECTORIES`, used only to isolate test temporary directories.

## Verification

The initial checkout had no `.venv`. The authorised `uv run --locked` invocation created one from
`uv.lock`, installing its pinned packages. UV caches and temporary files were directed into
`.canon/eval/` inside this checkout, and the two Alpaca key environment variables were removed from
test and check subprocesses without reading their values.

| Run | Result |
|---|---|
| `mise exec -- uv run --locked pytest` with the six supplied node IDs and `--basetemp=.canon/eval/pytest-baseline` | **6 failed**, reproducing every reported defect before editing code. |
| `mise exec -- uv run --locked pytest --basetemp=.canon/eval/pytest-targeted tests/test_backtest.py tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py --tb=short` | **132 passed, 1 failed**; all six supplied regressions passed. The remaining failure was the test that expects its temporary directory to be outside Git. |
| First complete offline suite, with a Git discovery ceiling for temporary directories | **455 passed**, before the additional nullable-return regression. |
| `mise exec -- uv run --locked pytest --basetemp=.canon/eval/pytest-nullable tests/test_returns_matrix_finite.py --tb=short` before the nullable fix | **3 passed, 1 failed**: the new nullable case exposed a raw `TypeError`. Adding the missing-value check corrected it. |
| Final complete offline suite, command below | **456 passed in 66.90 seconds**, including every supplied regression and all four new cases. |
| `mise exec -- uv run --locked ruff check .` | **Passed**. |
| `mise exec -- uv run --locked ruff format --check .` | **Passed**, 107 files already formatted. |
| `mise exec -- uv run --locked ty check` | **Passed**. |
| `git diff --check` | **Passed**. |

Keeping pytest's temporary files inside this checkout initially made the outside-Git test's directory
belong to the repository. Final runs set `GIT_CEILING_DIRECTORIES` to `.canon/eval`, so Git discovery
from a temporary fixture stops before reaching the checkout. Discovery from application source still
reaches the real repository. No test or application code was changed for this environment issue.

The final suite command was:

```sh
env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY \
  UV_CACHE_DIR="$PWD/.canon/eval/uv-cache" \
  TMPDIR="$PWD/.canon/eval/tmp" \
  GIT_CEILING_DIRECTORIES="$PWD/.canon/eval" \
  mise exec -- uv run --locked pytest \
  --basetemp=.canon/eval/pytest-offline \
  --ignore-glob='**/test_paper_*.py' --tb=short
```

The supplied concurrency regression passes its three synchronised rounds of two independent creator
processes. The supplied timestamp regression passes after reopening one ledger in Tokyo, UTC and
New York.

## Limits and remaining work

Paper test files were excluded explicitly. No credential was read, no paper or live API was called,
and no upstream network documentation was fetched. `scripts/ci` and `scripts/land` were not run, as
directed for this repository copy. No other agent was started.

Verification ran on macOS with CPython 3.14.10 and the locked packages. Execution on other operating
systems is **UNVERIFIED** in this session. No requested implementation or offline verification work
remains.
