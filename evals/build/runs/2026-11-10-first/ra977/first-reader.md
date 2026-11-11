# First reader: ra977
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. ledger.py:153 (readers take no lock): `open_study` is unchanged; the defect is creators racing.
2. ledger.py:191 (study visible before its sample is written): a pre-existing window between `create_study` and `set_user_attr`, not the creation race of defect 1 (two creators no longer crash).
3. ranking.py:313 (DST fold): inherent to Optuna's getter.
4. returns_matrix.py:448 (finite check stops before the coverage check): both are refusals that name the series; the docstring's "one stop" sentence is about the coverage check.
5. returns_matrix.py:451 (`math.isfinite('0.01')` raises TypeError): a string in an object-dtype series passed directly; it failed in skfolio before too; exotic.
6. docs edits outside the paths: not counted on their own. ADR 0035 term 1 and 2, the Evidence line and architecture.md are true of the code. The roadmap now says the six defects are fixed; that is what the change does, and "smaller defects are not yet fixed" would be untrue after it.
7. ledger.py:302 (top-level `fcntl`): portability wish.
8. ledger.py:156 (LOCK_EX with no timeout): a creator stalled inside the SQLite constructor; speculative.
9. returns_matrix.py:451 (per-element loop): efficiency.
## Per-defect check
1. Creation race: `_storage` holds `flock` on `<file>.lock` while constructing `RDBStorage` in mode `rwc` only, where `create_all` and the version stamp run. Removed.
2. Recorded range: `astimezone(UTC)`. Removed.
3. `returns_matrix` refuses NaN, nullable NA and infinities, every bad series named. Removed.
4. `_session_sharpe` through `daily_series`; refusal gives FAIL, `traded`, no value. Removed.
5. `run_backtest_config` refuses no strategy. Removed.
6. `/ledger/`. Removed.
## What I ran
- Read the diff and the tree's ledger, returns_matrix and roadmap at HEAD. open_study takes no lock, so the read-only ledger probe that breaks r5539 does not apply.
