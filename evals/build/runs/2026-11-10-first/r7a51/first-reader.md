# First reader: r7a51
Mark: land
Blocker: none
## Findings
None that a review round would have had fixed before landing.
## Review findings not taken
1. ranking.py:313 (DST fold): inherent to Optuna's getter; the report states it.
2. ledger.py:226 (open_study can read a half-created file): `open_study` is unchanged; the defect is two creators racing, which the lock removes. Pre-existing window.
3. returns_matrix.py:157 (`numbers.Real` vs `_is_number`): the two entry points take different inputs (a ledger dict vs a pandas Series); a float32 accepted by the matrix is not a defect.
4. returns_matrix.py:184 (nested comprehension with walrus): readability cleanup.
5. returns_matrix.py:189 (per-element loop): efficiency.
6. ledger.py:576 (Sharpe expression copied): cleanup.
7. ledger.py:46 (top-level `fcntl`): portability wish.
8. docs/ADR edits outside the paths: not counted on their own; ADR 0035 term 2 and architecture step 3 are true of the new behaviour.
9. tests/test_defect_edges.py:213 (tests repeat the supplied ones): additional tests are allowed; most cases are new (infinities, empty and non-numeric series, nested creation, import order).
10. backtest.py:249 (strategies not checked against the config): outside defect 5.
## Per-defect check
1. Creation race: `flock` on `<file>.lock` across `RDBStorage(rwc)` and `create_study`, released before yield; `open_study` untouched. Removed.
2. Recorded range: `astimezone(UTC)`. Removed.
3. `returns_matrix` refuses non-finite and non-real values, every bad series named, and an empty or unnamed series. Removed.
4. `_session_sharpe` through `daily_series`; refusal gives FAIL, `traded`, no value. Removed.
5. `run_backtest_config` refuses no strategy. Removed.
6. `/ledger/`. Removed.
## What I ran
- Read the diff, the new test file's list, and the tree's ledger and returns_matrix at HEAD. No probe beyond reading: open_study is unchanged, so the read-only ledger case that breaks r5539 does not arise.
