# First reader: r5539
Mark: no
Blocker: 4 — `open_study` now opens the sibling lock file read-write, so a ledger the reader cannot write (made read-only, or owned by another account) can no longer be opened or ranked; it raises an uncaught `PermissionError`
## Findings
1. src/canon_trading/ledger.py:167 and :186 — `_storage` now wraps every `RDBStorage` build, the read-only `open_study` path (mode `rw`) included, in `_creation_lock`, which does `os.open(lock, os.O_RDWR ...)` whenever the lock file exists. Before the change `open_study` on a write-protected ledger worked (SQLite's `mode=rw` opens read-only when the file is write-protected). Sequence: `create_ledger_study("s", ..., path=p)`; `chmod 0444` on `trials.db` and `trials.db.lock`; `open_study("s", p)` -> `PermissionError: [Errno 13] Permission denied: '.../trials.db.lock'`, not even `LedgerNotFound`. The same sequence on r3124's tree (open_study unchanged) opens the study. No test covers a ledger read without write access. Verified by probe (severity: blocker)
2. src/canon_trading/ledger.py:176 — every reader also takes `LOCK_EX`, so concurrent `open_study`/`rank` calls serialise against each other with no timeout; a reader needs no creation lock at all. Folded into 1; verified by reading (severity: fix)
## Review findings not taken
1. ranking.py:315 (DST fold): inherent to Optuna's getter; stated in the report.
2. returns_matrix.py:172 (`day.date()` on a non-datetime index): a direct call with an integer index; the existing coverage message already calls `.date()` the same way.
3. ledger.py:29 (header's "no lock file, no creation under way" untrue for a legacy ledger): only for a ledger made before this change, whose tables already exist, so no race follows.
4. ledger.py:594 (Sharpe expression copied from ranking): cleanup.
5. returns_matrix.py:199 (per-element loop): efficiency.
6. tests/test_ledger_creation_lock.py, docs edits outside the paths: not counted on their own; the ADR 0035 and architecture.md edits are true of the new behaviour.
7. ledger.py:166 (two yield sites): style.
## Per-defect check
1. Creation race: removed for creators (lock around `RDBStorage(rwc)`); the same lock now also gates readers, which is finding 1.
2. Recorded range: `astimezone(UTC)`. Removed.
3. `returns_matrix` refuses a non-finite return, every such series named. Removed.
4. `_session_sharpe` uses `daily_series` and the gate's `Portfolio(X=series.to_frame(), ...)` call; refusal gives FAIL, `traded`, no value. Removed.
5. `run_backtest_config` refuses no strategy. Removed.
6. `/ledger/`. Removed.
## What I ran
- Probe script run with the tree's locked environment (`uv run --locked python -I`): create ledger, chmod 0444 both files, `open_study` -> PermissionError on `trials.db.lock`. Same probe on r3124's tree -> opens. Files restored to 0644 and removed after.

## Reread (settling the second reader's fix)
Final mark: fix. Blocker: none. The `PermissionError` on a write-protected ledger still reproduces, but no record or docstring promises such a ledger opens, and nothing in `src/` or `scripts/` calls `open_study`; it is library API used by tests. A review round would fix it (open the lock read-only for readers, or skip it for mode `rw`), with the readers' exclusive lock.
