# Second reader: r6160
Mark: fix
Blocker: none
## Findings
1. `src/canon_trading/ranking.py:318`, with the new docstrings at `:24-27` and `:80-83`. The docstrings say `datetime.astimezone` "converts back" Optuna's naive local start, and that without the conversion "the same ledger would read differently in another time zone". Neither holds in a fall-back hour. Optuna drops the fold when it hands back naive local time, so `astimezone(UTC)` takes the earlier instant, and in that hour the report still depends on the zone it is read in. Neither the code nor ADR 0035 records this limit; only the worker's report names it, marked UNVERIFIED and "theoretically". Shown by probe `p2b_fall_back_through_rank.py` on the change. A ledger whose trials are stored as 2026-11-01 06:30 and 07:30 UTC ranks as `2026-11-01T05:30:00+00:00 to 07:30:00+00:00` in America/New_York and as `06:30:00+00:00 to 07:30:00+00:00` in Asia/Tokyo. Optuna's public `FrozenTrial` gives only the naive local time, so the fix a review would ask for is to record the limit next to the conversion and in ADR 0035, not to change the expression. The case's own fix uses the same expression and records the limit in both places. (severity: fix)
## Per-defect check
1. Two processes that create one new ledger file at the same moment race, and one of them crashes: removed. Checked three ways:
   - The supplied test fails on the starting tree (`IntegrityError: UNIQUE constraint failed`) and passes on the change.
   - Probe `p1_concurrent_create.py`, 4 processes creating different studies in one new ledger, 8 rounds: no round failed on the change. On the starting tree all 8 rounds failed, with errors among them `table studies already exists`, `table alembic_version already exists` and `UNIQUE constraint failed: alembic_version.version_num`.
   - With one study name, each of 8 rounds gives one `ok` and one `DuplicatedStudyError`. The lock is released before the study is yielded: a second creator finished 2.12 s before the first left its `with` block.
2. The report's recorded range is in the naive local time the store returns, so it reads differently in another time zone. It should be UTC, with its offset: removed, apart from the fall-back hour (finding 1). Checked two ways:
   - The supplied test fails on the starting tree and passes on the change.
   - Probe `p2_utc_range.py` writes trials in Asia/Tokyo and reads them in New York, UTC, Lord Howe (UTC+11 in November) and Tokyo. The change gives one reading ending `+00:00`, inside the UTC instants taken around the writing. The starting tree gives four naive readings.
3. `returns_matrix`, called directly, accepts a return that is not finite: removed for series on dates. Checked two ways:
   - The supplied test fails on the starting tree and passes on the change.
   - Probe `p3_returns_matrix_not_finite.py`: NaN, +inf, -inf, `None` in an object series and `pd.NA` in a Float64 series are each refused with `ReturnsRejected`, which names the series. The finite control is accepted. On the starting tree, NaN, `None` and `pd.NA` were accepted as 0.0, and ±inf raised skfolio's own `ValueError`.
   - One input outside the dated contract still passes; see Declined to judge.
4. The value told for a trial does not come from the series as the gate accepts it; a run whose returns the gate rejects should be told no value, and failed: removed. Checked two ways:
   - The supplied test (a return on a closed day) fails on the starting tree and passes on the change.
   - Probe `p4_value_told.py`: an ordinary traded run is told `COMPLETE 0.36314201247925004` on both trees, equal to the gate's own Sharpe ratio from the stored attributes. With a gap in the recorded series (a zero return on a Monday removed), the change tells `FAIL`, `traded`, value `None`. The starting tree told `COMPLETE 0.36314201247925004`, although the gate refuses that series ("skips from ..."). With keys an hour off midnight, both trees tell `FAIL`, `traded`.
5. A run configuration with no strategy is run instead of refused: removed. Checked two ways:
   - Read: `run_backtest_config` (`src/canon_trading/backtest.py:250-251`) raises `ValueError` for any empty sequence before the node is built.
   - The supplied test fails on the starting tree and passes on the change.
6. The rule that ignores the `ledger/` folder matches a folder of that name anywhere in the tree, not only at the root: removed. Checked two ways:
   - The supplied test fails on the starting tree and passes on the change.
   - Probe `p6_gitignore.sh`: `/ledger/` ignores `ledger/`, `ledger/trials.db` and the new `ledger/trials.db.lock`. It ignores none of `src/canon_trading/ledger/module.py`, `tests/ledger/x.py`, `packages/canon-alpaca/ledger/x.py` and `docs/ledger/notes.md`. The starting tree ignored all of them.
   - The other `ledger/` folders the scripts name are under `~/.claude/canon/` or temporary folders, outside the repository.
## Probes
All probe scripts are in `<score>/r6160/second/probes/`. Each was run from the root of `second/copy` (the change) or `second/base` (the starting tree, made by `harness.py tree ledger-remaining-defects`, with the same tree hash `2e3396cf` as the copy's commit), with `mise exec -- uv run --locked python <script> <scratch dir under second/scratch/>`.
- The copy's `git diff` is byte-identical to `change.patch`. `grade.json` has no `flags` and nothing `restored`.
- The six `fail_to_pass` tests: change `6 passed`; starting tree `6 failed`.
- Full offline suite on the change: `452 passed, 21 skipped` (`second/probe-full-suite-copy.log`). On the change, `ruff check` passes, `ruff format --check` reports 106 files already formatted, `ty check` passes and `scripts/canon lint` reports lint clean. These match the report.
- The report's two Optuna 5.0.0 claims were read in the copy's installed source and hold. `storages/_rdb/storage.py:262-263` calls `create_all` unless `skip_table_creation` is set. `storages/_rdb/models.py:189-208` stores `datetime_start` as naive UTC and returns `.astimezone().replace(tzinfo=None)`.
- `p1_concurrent_create.py`:
  - Change: 0 of 8 rounds failed; same name `[('DuplicatedStudyError', 'ok')] x 8`; the second creator took 0.155 s.
  - Starting tree: 8 of 8 rounds failed; same name `[('IntegrityError', 'ok'), ('OperationalError', 'ok')]`.
- `p1b_reader_during_creation.py` (context only): 1 creator and 3 readers looping `open_study`, 10 rounds. On both trees the creator succeeded 10 times and the readers 30 times. The only error the readers met first was `LedgerNotFound: there is no ledger`.
- `p2_utc_range.py`:
  - Change: a single reading, `2026-11-13T20:41:23.458357+00:00 .. 20:41:24.597246+00:00`, in all four zones, inside the write window.
  - Starting tree: `15:37:27`, `20:37:27`, `07:37:27` (next day) and `05:37:27` (next day), all naive.
  - Optuna's model returns the stored `2026-11-01 06:30` UTC as `01:30` in New York, and `astimezone(UTC)` gives `05:30+00:00`. In Tokyo it returns `15:30` and gives `06:30+00:00`.
- `p2b_fall_back_through_rank.py`:
  - Change: New York `05:30:00+00:00 to 07:30:00+00:00`; Tokyo `06:30:00+00:00 to 07:30:00+00:00`.
  - Starting tree: New York `01:30:00 to 02:30:00`; Tokyo `15:30:00 to 16:30:00`.
- `p3_returns_matrix_not_finite.py`:
  - Change: NaN, ±inf, `None` and `pd.NA` are each refused with names `('a',)`; the control is accepted; a NaN at index label `None` is accepted as `[0.01, 0.02, 0.0]`.
  - Starting tree: NaN, `None` and `pd.NA` are accepted as 0.0; ±inf raise `ValueError: Input contains infinity`; the `None`-label case gives the same as the change.
- `p3b_finite_check_cost.py` (change only): 200 series of 2,500 rows. The new per-value finite check takes 0.193 s of a 0.621 s `returns_matrix` call.
- `p4_value_told.py`:
  - Change: `none` COMPLETE 0.36314201247925004, which the gate accepts with the same value; `gap` FAIL, traded, None, which the gate refuses; `off-midnight` FAIL, traded, None, which the gate refuses.
  - Starting tree: `none` the same; `gap` COMPLETE 0.36314201247925004, which the gate refuses; `off-midnight` FAIL, traded, None.
- `p6_gitignore.sh`: results as in per-defect check 6.
## Declined to judge
- A NaN at an index label of `None` passes `returns_matrix` and becomes 0.0 (probe p3). `_first_not_finite` (`src/canon_trading/returns_matrix.py:156-164`, `:181`) returns the label, and `None` also means "none found". `returns_matrix` takes series on dates, and a missing date there is `NaT`, which is refused. I set aside whether this input is within its contract and whether a review round would ask for it.
- `open_study` does not take part in the creation lock. A reader opening a ledger that is still being created is not one of the six defects. Probe p1b showed no failure on either tree in 10 rounds. That is not proof of absence, so it is not marked. The case's own fix adds a shared lock for readers.
- `docs/roadmap.md` step 3 still says "Smaller defects are not yet fixed. No ranking is to be relied on until they are". The copy cannot tell whether these six are all the defects that line means, so it is not marked.
- The per-value finite check costs about a third of a large `returns_matrix` call (probe p3b, 0.19 s). That is speed, not correctness, so it is not marked.
- UNVERIFIED:
  - `scripts/ci` was not run in the copy, because the build brief says it expects files the copy leaves out. The tests, ruff, ruff format, ty and `scripts/canon lint` were run in its place.
  - Nothing ran on Linux, the hosted runner; every probe ran on this Mac only.
  - That a review round would have raised finding 1 is a judgement. It rests on the repository recording known limits in its ADRs (as the case's fix does), not on a run.
