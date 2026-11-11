# Six ledger, matrix and gate defects: report

All six tests named in the brief failed on the original tree and pass now. The full offline suite
passes, and so do the three code checks. The change is uncommitted in the working tree. It touches
`src/canon_trading/{ledger,ranking,returns_matrix,backtest}.py`, `.gitignore` and
`docs/adr/0035-trial-ledger-in-an-optuna-study.md`. No test file was edited and none was added.

## What was wrong, and what changed

### 1. Two processes creating one new ledger race

**What was wrong.** Optuna 5.0.0's `RDBStorage.__init__` (installed `optuna/storages/_rdb/storage.py`,
lines 262–267) checks for and then creates the tables, using `metadata.create_all`. `_VersionManager`
then checks for and then inserts its version and alembic rows. Two processes doing this on one new file
interleave, and one of them fails with "table studies already exists".

**What changed.** `ledger._storage` now builds a storage in `rwc` mode while holding an exclusive
`fcntl.flock` on `<ledger file>.lock`, a file next to the ledger. The lock covers only the storage's
construction. The kernel releases it when the descriptor closes, including when the process crashes, so
a stale lock can never be left behind. `rw` mode (`open_study`) takes no lock and creates no file, so
"opening never creates a file" still holds.

**Why this way.**
- **Upstream contract:** Python's standard `fcntl.flock`, which is flock(2). It adds no dependency.
- **Lock on a separate file:** the lock is not taken on the database file itself, because SQLite holds
  its own POSIX locks on that file.
- **Not Optuna's journal locks:** `JournalFileOpenLock` belongs to Optuna's other backend and deletes
  stale locks after a grace period. I considered it and did not use it.

The module header now explains the lock.

### 2. The recorded range depended on the reader's time zone

**What was wrong.** The installed `optuna/storages/_rdb/models.py` (`TrialModel`, lines 183–227) says it
directly: trial datetimes are *stored as naive UTC and converted to naive local time* in the reading
process's zone when a `FrozenTrial` is built. `rank` printed that naive local value, so the same ledger
read as a different range in each zone.

**What changed.** `rank` now converts each `datetime_start` with `.astimezone(UTC)` in the same process
that read it. This undoes Optuna's conversion exactly, and the report prints the range as
`…+00:00`. `RankingReport`'s docstring says so.

### 3. `returns_matrix`, called directly, accepted a return that was not finite

**What was wrong.** I ran it to check: skfolio 1.12.8's `Portfolio` turns a NaN return into `0.0`, so
the matrix held a return nobody earned. Only `daily_series` refused such a return.

**What changed.** `returns_matrix` now refuses every series that holds a non-finite return before it
builds a `Portfolio`. It raises `ReturnsRejected` with `names` set to those series, the same way its
coverage stop does. I corrected the module header's claim that skfolio raises on such a return: it does
not.

### 4. The value told did not come from the series as the gate accepts it

**What was wrong.** `ledger._session_sharpe` silently filtered the returns down to the session days.
A return on a day with no bar, which ADR 0010 step 2 and `daily_series` reject, was therefore dropped,
and the run was told `COMPLETE` with a Sharpe ratio.

**What changed.**
- `record_trial` now builds the series with `returns_matrix.daily_series`. If that rejects the series,
  the run is told `FAIL` with no value, and its outcome stays `traded`.
- Otherwise the value is the new `ledger.session_sharpe(series)`: skfolio's `Portfolio(X=series.to_frame(),
  weights=[1.0]).sharpe_ratio`. `ranking` now calls the same function, so the value told and the value
  ranked cannot drift apart.
- A one-day series still gives NaN, so it is still told `FAIL`; I checked this with `-W error`.

**An import cycle.** `returns_matrix` imported `DAY_NS` from `ledger`. So that `ledger` can import
`daily_series` without a cycle, `DAY_NS` is now defined in `returns_matrix`, and `ledger` imports it
from there. `canon_trading.ledger.DAY_NS` still exists, so the existing tests import it unchanged.

**Records corrected.**
- The `ledger` module header and the `record_trial` docstring.
- The `ranking._ran` docstring and the ranking module header.
- ADR 0035 term 2, which said the value comes from "its session days". It now says the value comes from
  the series as step 2 accepts it, and that a rejected series is told `FAIL`. The decision itself is
  unchanged, and I added one Evidence line dated 2026-11-10.

### 5. A run configuration with no strategy was run

**What changed.** `run_backtest_config` now raises
`ValueError("run_backtest_config needs at least one strategy")`, the same refusal `backtest_run_config`
already makes. Without it, the `BacktestNode` replays the bars with nothing to trade them and returns an
empty result. The docstring says so.

### 6. `ledger/` was ignored anywhere in the tree

**What changed.** `.gitignore` now has `/ledger/`. In gitignore syntax a leading slash anchors a pattern
to the directory that holds the `.gitignore`. The comment beside the rule says so. `catalog/*` was
already anchored, because a pattern with a slash in the middle is always anchored.

## What I ran

| Command | Result |
|---|---|
| The six named tests, on the original tree (my change stashed) | 6 failed |
| The same six, on the final tree | 6 passed |
| `mise exec -- uv run --locked pytest` (full suite, before a final one-line typing fix) | 452 passed, 21 skipped |
| The same full run, repeated on the final tree | 452 passed, 21 skipped |
| `uv run --locked ruff check .` | All checks passed |
| `uv run --locked ruff format --check .` | 106 files already formatted |
| `uv run --locked ty check` | All checks passed (after `names=` was given `str` values) |
| `scripts/canon lint` (offline) | lint clean |

The 21 skips are the paper and integration tests, which skip because no Alpaca credentials are in the
environment, as the brief requires. I did not run `scripts/ci` or `scripts/land`.

## What I am unsure of

- **Lock file and platform.** The lock leaves a `trials.db.lock` file next to the ledger. Inside
  `ledger/` it is ignored by git. For a ledger at a path someone passes in, it is a stray file.
  `fcntl` is Unix-only. That fits this repository (macOS and Linux runners), but I did not check a
  stated platform policy.
- **A read during creation.** `open_study` does not wait for a creation in progress. A reader that hits
  the moment between file creation and table creation can still get `LedgerNotFound` ("not an Optuna
  ledger"). The same holds between `create_study` and the sample attribute being written. That is
  outside the six defects; I left it alone.
- **Limits of the UTC fix.**
  - A local time that is ambiguous in a DST fall-back hour comes out as one of its two instants. That
    error comes from Optuna's naive-local conversion and cannot be recovered from it.
  - A process that changes its zone after Optuna has cached a finished trial
    (`_CachedStorage`) would convert with the wrong zone. This is UNVERIFIED: I did not test it. Every
    reopened study reads fresh.
- **No independent review.** The repository's rules call for `/code-review` (ADR 0017) before landing.
  The brief limits me to this task alone with no subagents, so no independent review was run.
  Nothing here lands.
