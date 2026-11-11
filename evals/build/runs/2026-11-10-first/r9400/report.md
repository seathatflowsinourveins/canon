# Report: six defects in the trial ledger, the returns matrix and the gate

Checkout: copy at one commit, no remote, no earlier history. `AGENTS.md` was read first; nothing was
landed and `scripts/land` was not run, as the task instructed. No paper test, credential, or network
access (beyond `uv` installing the pinned `uv.lock` packages via `mise exec`) was used. No test file
was edited. No dependency was added.

## What was wrong

1. **Concurrent ledger creation raced.** `create_ledger_study` in `src/canon_trading/ledger.py` opened
   `RDBStorage` with `mode=rwc` (table creation enabled) with no cross-process serialisation. Two
   processes creating studies in one new file both ran table creation and one crashed with
   `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) table studies already exists`
   (reproduced: 1 of 2 children exited 1). Optuna's `RDBStorage` runs
   `BaseModel.metadata.create_all` unguarded and its own docstring warns against SQLite for parallel
   work, so the race is upstream's documented gap, filled here with local glue.
2. **Recorded range was zone-dependent.** `rank` in `src/canon_trading/ranking.py` formatted
   `trial.datetime_start` directly. Optuna's RDB storage stores that instant as naive UTC and returns
   it as naive local time (`TrialModel.datetime_start`), so the bare `isoformat()` read differently in
   each time zone and carried no offset. Reproduced: the same trials gave three different
   `(first_recorded, last_recorded)` pairs under `Asia/Tokyo`, `UTC` and `America/New_York`.
3. **`returns_matrix` accepted non-finite returns when called directly.** `daily_series` refuses them,
   but `returns_matrix` in `src/canon_trading/returns_matrix.py` built skfolio `Portfolio`s first.
   Skfolio treats a NaN in `X` as a zero return, and the join reads a NaN as a missing day, so a direct
   caller got a matrix holding a return nobody earned instead of a refusal. Reproduced: no exception.
4. **Told value bypassed the gate.** `record_trial` computed the told Sharpe ratio in
   `_session_sharpe` from the returns falling on session days only. A series the gate
   (`daily_series`, ADR 0010 step 2) rejects — e.g. non-zero returns on days with no bar — still gave a
   finite subset ratio and was told `COMPLETE` with that value. Reproduced via the test's truncated
   `session_days`: state was `COMPLETE` instead of `FAIL`.
5. **Empty run configuration ran.** `run_backtest_config` in `src/canon_trading/backtest.py` built and
   ran a `BacktestNode` even with `strategies=[]`, replaying bars with nothing to trade them and
   returning an empty result. Reproduced: no `ValueError`. (`backtest_run_config` already refused this;
   the config-taking entry point did not.)
6. **Ignore rule too broad.** `.gitignore` used `ledger/`, which git matches at any depth, so
   `src/canon_trading/ledger/module.py` was ignored along with the root trial ledger. Reproduced with
   `git check-ignore`.

## What I changed, and why that way

- **`src/canon_trading/ledger.py` — creation lock.** Added `_creation_lock`: `fcntl.flock(LOCK_EX)` on
  `<path>.lock` beside the ledger file. `create_ledger_study` now creates tables and the study under
  that lock, releases it, then yields the study reopened with `mode=rw`
  (`skip_table_creation=True`, creates no tables). Releasing before the yield matters: a study held
  open (as tests do through an `ExitStack`) must not block a second creator in the same process, which
  holding the lock across the yield would deadlock. The lock is per ledger file, advisory, and released
  by the OS if a holder dies, so no stale lock can wedge later creators. The yielded study is loaded,
  not created, so `DuplicatedStudyError` behaviour for a taken name is unchanged.
- **`src/canon_trading/ledger.py` — told value from the gate's series.** `_session_sharpe` now builds
  `daily_series("trial", returns, days)` — the same series `canon_trading.ranking` ranks — and takes
  skfolio's `Portfolio.sharpe_ratio` over `series.to_frame()`, exactly as `rank` constructs it. A
  `ReturnsRejected` gives NaN, so `record_trial`'s existing `isinfinite` branch tells `FAIL`, still
  `traded`, with no value; subset-finite-but-rejected series (closed-day moves, gaps, fallback keys)
  all take this path, while accepted series (including zero closed days, which `daily_series` drops)
  give the identical ratio as before. The import is function-local because `returns_matrix` imports
  `DAY_NS` from `ledger`; a module-level import would cycle, and the docstring says so.
- **`src/canon_trading/returns_matrix.py` — finite check before skfolio.** `returns_matrix` now scans
  every input series first and raises `ReturnsRejected` naming each offending series
  (`"<name>: a return that is not finite: <value> on <stamp>"`, `names` in input order) before any
  `Portfolio` is built. This keeps NaN from being read either as skfolio's zero or as the join's
  missing day, and covers `inf`/`-inf` and non-numeric values as not finite.
- **`src/canon_trading/ranking.py` — UTC recorded range.** Each `datetime_start` is converted with
  `.astimezone(UTC)` before sorting and formatting. Python reads a naive value as local time for
  `astimezone`, which is exactly the inverse of Optuna's stored-UTC-to-naive-local conversion, so the
  report states the stored UTC instant with `+00:00` in every zone; an already-aware value (e.g. from
  journal storage) converts correctly too.
- **`src/canon_trading/backtest.py` — refuse no strategy.** `run_backtest_config` raises
  `ValueError("run_backtest_config needs at least one strategy")` before building any node, mirroring
  `backtest_run_config`'s existing refusal.
- **`.gitignore` — root-only ledger.** `ledger/` became `/ledger/`. Git anchors a pattern containing a
  leading slash to the `.gitignore`'s directory, so only the root trial ledger is ignored; the comment
  now says so.
- **Records kept true.** `ledger.py`'s header (creation lock; told value), `record_trial`'s docstring,
  `returns_matrix.py`'s header (it previously claimed skfolio's `Portfolio` "raises on" a non-finite
  return — it treats NaN as zero), `ranking.py`'s `_ran` docstring (a no-value traded `FAIL` now also
  covers a rejected series), `backtest.py`'s `run_backtest_config` docstring, and ADR 0035 term 2
  (`docs/adr/0035-trial-ledger-in-an-optuna-study.md`: the told ratio is over the series the gate
  accepts, with no value when it is rejected) were corrected to match the new behaviour. ADR status,
  dates and decision were not changed. `README.md` and `docs/architecture.md` only name the root
  `ledger/` folder and remain true, so they were left alone.

## Upstream API or contract each change follows

- Creation lock: Optuna 5.0.0 `optuna/storages/_rdb/storage.py` (`RDBStorage.__init__` →
  `BaseModel.metadata.create_all`; the "never recommend SQLite3 for parallel optimization" note) and
  `skip_table_creation` semantics already used by `_storage`; Python's `fcntl.flock` advisory lock.
  Both are named in `ledger.py`'s header and `_creation_lock`'s docstring. The store, URI modes
  (`rw`/`rwc`) and `optuna.create_study`/`load_study` contract from ADR 0035 are otherwise untouched.
- Told value: ADR 0010 step 2 / ADR 0041 acceptance via `canon_trading.returns_matrix.daily_series`,
  and skfolio 1.12.8 `Portfolio.sharpe_ratio` (`portfolio/_base.py`) constructed as `ranking.rank`
  constructs it (`Portfolio(X=series.to_frame(), weights=[1.0])`). Named in `_session_sharpe`'s
  docstring and the module header.
- Finite refusal: ADR 0031 (every return finite) and skfolio's `Portfolio` `X` contract
  (`portfolio/_portfolio.py`: NaN treated as zero returns), named in the module header; refusal type
  and `names` follow the existing `ReturnsRejected` convention in the same file.
- UTC range: Optuna 5.0.0 `optuna/storages/_rdb/models.py`, `TrialModel.datetime_start` ("stored as
  naive UTC and converted to naive local time"), and `datetime.astimezone`'s naive-is-local rule.
  Verified against the installed package and by probe (below). Named at the conversion site.
- Empty run: this repo's own `backtest_run_config` refusal and the `BacktestNode` high-level API in
  `backtest.py`'s header (a node with no strategy returns an empty result); no upstream behaviour is
  relied on.
- Ignore rule: git's `gitignore` pattern rule (a pattern with a slash is anchored to the `.gitignore`
  location), exercised through `git check-ignore` by the test itself.

## What I ran, with its result

All commands via `mise exec -- uv run --locked ...` from the checkout root, offline.

- Before fixing, each named test was run and failed as described above (backtest: DID NOT RAISE;
  `.gitignore`: nested path ignored; ranking: three zone-dependent ranges; returns matrix: DID NOT
  RAISE; ledger value: `COMPLETE` with a value; race: one child exit 1, `table studies already
  exists`).
- Timezone probe (`/tmp/probe_tz.py`, scratch, not committed): raw `datetime_start` read as
  `15:21`/`06:21`/`01:21` naive under Tokyo/UTC/New York for one instant; `.astimezone(UTC)` gave
  `06:21...+00:00` in all three, inside the measured write window. This is the second, runtime route
  confirming the `models.py` source route.
- Edge probes (`/tmp/probe_edges.py`, `/tmp/probe_race.py`, scratch): `inf`, `-inf`, NaN in either or
  both series refused with the right `names`, clean series unchanged; `_session_sharpe` finite for a
  normal series, NaN for a non-zero closed day, finite and unchanged for a zero closed day; four
  concurrent creators of one new file succeeded in two rounds (`s0`–`s3` all present).
- The six named tests: **6 passed**.
- Touched-area files (`test_ledger.py`, `test_ranking.py`, `test_returns_matrix.py`,
  `test_backtest.py`): **133 passed**.
- Full suite (`pytest`): **452 passed, 21 skipped** (skips are the paper tests needing a key, which
  the task forbids running). Run once after the logic fixes and again on the final tree.
- `ruff check .`: passed. `ruff format --check .`: passed (106 files). `ty check`: passed.
- `scripts/ci` was not used as the gate: the task notes this copy leaves out files it expects, so its
  failure here would not be attributable; the graded tests and three code checks above were run
  instead. Working tree left uncommitted with only the six files below changed; no lock files or
  scratch files were written into the checkout (probes live in `/tmp`).

Changed files: `.gitignore`, `docs/adr/0035-trial-ledger-in-an-optuna-study.md`,
`src/canon_trading/backtest.py`, `src/canon_trading/ledger.py`, `src/canon_trading/ranking.py`,
`src/canon_trading/returns_matrix.py`.

## Anything I am unsure of

- `fcntl` is Unix-only. This repo's CI (`ubuntu-24.04`) and development host (macOS) are Unix, and no
  Windows path exists in the checkout's tooling, so this holds; a future Windows port would need
  `msvcrt.locking` beside it. Flagged rather than papered over with a fallback the repo cannot test.
- ADR 0035 term 2 was edited in place (one sentence) because the fix otherwise made it false; if the
  repo prefers superseding records over in-place term corrections for accepted ADRs, that sentence is
  the place to move into a new record instead. No other ADR statement was found to be made untrue.
- The creation lock serialises creation only. Concurrent `record_trial` writes to one study still rely
  on SQLite's own locking, as before; ADR 0041 already calls out the analogous `move_sample` race as
  one session's act, and no test or defect here covers concurrent trial writes.
- No tests were added: the six defect tests already in the checkout are the focused maintained checks
  for exactly these behaviours (including multi-round and multi-process cases), and the edge probes
  above were scratch verification under `/tmp`, not deliverables.
