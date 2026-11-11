# Report: six defects in the ledger, the returns matrix and the gate

## What was wrong

1. **Creation race.** Building an Optuna `RDBStorage` sets up the file it opens, and not atomically.
   `metadata.create_all` checks for each table and then creates it, and `_VersionManager` adds the
   version row and stamps the alembic revision when it finds none (read in the installed
   `optuna/storages/_rdb/storage.py`, 5.0.0). Two processes doing this on one new file collided with
   "table studies already exists".
2. **Recorded range in local time.** `TrialModel.datetime_start` stores naive UTC but returns
   `.astimezone().replace(tzinfo=None)`, which is naive *local* time (installed
   `optuna/storages/_rdb/models.py`). The report printed that time with `isoformat()`, so the range
   read differently in each time zone and had no offset.
3. **`returns_matrix` took a non-finite return.** Only `daily_series` checked for finiteness. Called
   directly, `returns_matrix` passed a NaN to skfolio's `Portfolio`, which reads it as 0.0. I ran this
   with the installed 1.12.8: `[0.01, nan, 0.02]` comes back as `[0.01, 0.0, 0.02]`.
4. **The told value did not come from the gate's series.** `_session_sharpe` took the returns on the
   session days and ignored the rest. The gate's `daily_series` refuses more than that: a non-zero
   return on a day with no bar, gaps, and keys off midnight. So a run whose series the gate refuses was
   told `COMPLETE` with a Sharpe ratio.
5. **No strategy was not refused.** `run_backtest_config(config, [])` built the node and ran it. Only
   `backtest_run_config` refused an empty list.
6. **`.gitignore` matched too much.** The rule `ledger/` has no leading slash, so it matched a folder
   named `ledger` at any depth.

## What I changed, and why that way

- **`src/canon_trading/ledger.py`, defect 1.** `_storage` now builds the `RDBStorage` while holding an
  exclusive `fcntl.flock` on the folder that holds the SQLite file. Both creating and opening go
  through it. The lock is released as soon as the storage is built. I locked the folder rather than a
  separate lock file because:
  - `open_study` promises to create no file, and a test checks the folder's contents.
  - The kernel releases the lock if the process dies, so a killed process leaves no stale lock.
  - Opening and closing an fd on the SQLite file itself would drop SQLite's own POSIX locks.

  I looked at Optuna's public `optuna.storages.journal.JournalFileOpenLock` as the upstream option and
  did not use it. It creates a `<file>.lock` file, which `open_study` must not do. A crashed holder
  makes others wait 30 s before the lock is forced. And its `acquire`, if interrupted while waiting,
  calls `release()`, which removes the lock file another process holds.
- **`ledger.py`, defect 4.** `_session_sharpe` now builds the series with
  `canon_trading.returns_matrix.daily_series`, the call the ranking makes on a record. It then takes
  `Portfolio(X=series.to_frame(), weights=[1.0]).sharpe_ratio`, the same form `ranking.py` uses. If
  the gate raises `ReturnsRejected`, or there are fewer than two sessions, the result is NaN. The run
  is then told `FAIL`, still `traded`, with no value.
  - To avoid a circular import, `DAY_NS` moved to `returns_matrix.py`. `ledger.py` imports it from
    there, so `from canon_trading.ledger import DAY_NS` still works.
- **`src/canon_trading/ranking.py`, defect 2.** Each `datetime_start` is converted with
  `.astimezone(UTC)`, so the report gives UTC with `+00:00`. Python's `datetime.astimezone` reads a
  naive time as local time, which undoes Optuna's conversion.
- **`src/canon_trading/returns_matrix.py`, defect 3.** `returns_matrix` refuses every series that holds
  a non-finite return before skfolio sees it. It names each such series in the message and in
  `ReturnsRejected.names`.
- **`src/canon_trading/backtest.py`, defect 5.** `run_backtest_config` raises
  `ValueError("run_backtest_config needs at least one strategy")` before building a node.
- **`.gitignore`, defect 6.** The rule is now `/ledger/`, the root's folder only.
- **Text kept true:**
  - The ledger module header now describes the lock and where the told value comes from.
  - The `record_trial` and `_session_sharpe` docstrings match the new behaviour.
  - The `returns_matrix` header, the `RankingReport` docstring and the comment in `.gitignore` are
    updated.
  - ADR 0035: term 2 now says the value is taken over the series the gate accepts, with `FAIL` and no
    value when the gate refuses that series. A dated Evidence entry records this fix. It is a
    correction with no change of decision.

## Upstream API or contract followed

- Optuna 5.0.0 `RDBStorage`, in the installed source: what its constructor writes, and the naive local
  time `datetime_start` returns.
- Python 3.14 `fcntl.flock` and `os.open` (flock(2) on macOS and Linux), and `datetime.astimezone`.
- skfolio 1.12.8 `Portfolio(...).sharpe_ratio`, called as `ranking.py` calls it.
- NautilusTrader `BacktestNode`: this is unchanged; the refusal comes before it.
- The new comment cites SQLite's "How To Corrupt" page, section 2.2. I could not reach the network to
  re-read it, so the comment labels it UNVERIFIED.

## What I ran

| Command | Result |
|---|---|
| The six named tests, before the change | 6 failed |
| The six named tests, after the change | 6 passed |
| `mise exec -- uv run --locked pytest -q`, before | 6 failed, 446 passed, 21 skipped |
| The same, after | 452 passed, 21 skipped |
| The two-process race test, run 5 more times (3 rounds each) | passed every time |
| `ruff check .` | clean, after `ruff check --fix` sorted one import in `ledger.py` |
| `ruff format --check .` | 106 files already formatted |
| `ty check` | All checks passed |
| `scripts/canon lint`, after the ADR edit | lint clean |
| A directory-fd `flock` on this macOS, and skfolio reading a NaN as 0.0 | both confirmed by running them |

I did not run `scripts/ci`, because the brief says this copy lacks files it needs. I added no test
files.

## What I am unsure of

- **DST overlap.** Optuna's naive local time loses information in the hour a DST change repeats.
  `astimezone(UTC)` reads such a time as the first of the two (fold 0), so a trial started in that
  hour could be reported an hour off. Fixing that fully would mean reading Optuna's private
  `_datetime_start_utc` column, which I avoided.
- **The lock's limits.** It is POSIX only: there is no `fcntl` on Windows, and CI runs Ubuntu. It
  covers only processes using this module, and it does not cover NFS, which ADR 0035 already leaves to
  Optuna's journal storage. It also serialises the setup of two different ledgers in one folder for a
  few milliseconds.
- **Not reviewed.** Nothing landed, so the review ADR 0017 requires before landing has not run on this
  change.
