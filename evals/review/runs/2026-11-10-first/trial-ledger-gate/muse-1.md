# Review of commit `e21935c1ada09318cdb92f929333b6295eca24c8`

Scope reviewed: the change from the parent of `e21935c` to `e21935c` in
`src/canon_trading/` and `tests/` (`git diff e21935c^ e21935c -- src/canon_trading/ tests/`),
read against ADR 0010, ADR 0031 and ADR 0035 in the same checkout.

How this was checked: the new modules and tests were read in full, then the offline
tests were run, then the inputs below were run as standalone probe scripts kept
outside the checkout (under `/tmp`), against a real Optuna study in a temporary
directory and against the functions directly.

- `mise exec -- uv run --locked pytest tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py tests/test_deflated_sharpe.py -q` — 62 passed.
- `mise exec -- uv run --locked pytest tests/ -q` — 83 passed.

So every defect below is in behaviour the committed tests do not cover. The
checkout was not edited.

## Defects, most severe first

### 1. `rank` silently ignores a `session_days` disagreement between records of one configuration, so the same ledger can pass or stop depending on which record came first

- **Where:** `src/canon_trading/ranking.py`, `rank()` — the disagreement check at
  lines 148–157 (`keep = ("backtest_start", "backtest_end", "returns_series")`)
  and the choice of record at line 164 (`chosen[key] = first`).
- **What goes wrong:** Records that ran to the end are supposed to be checked
  against each other (ADR 0035 term 5: "Every record of a key that ran to the
  end must carry the same range and returns, else the ranking stops"). The check
  compares only `backtest_start`, `backtest_end` and `returns_series`. It never
  compares `session_days`, even though `daily_series` (lines 169–173) builds the
  trial's series from the *first* record's `session_days` alone and that
  calendar decides which days are kept, which non-zero returns are refused as
  "a day with no bar of the run", and how many rows `T` has. Two records of the
  same configuration with the same range and the same `returns_series` but
  different `session_days` are therefore treated as agreeing, and the gate runs
  on whichever calendar was recorded first. This is reachable without hand-editing
  the store: `trial_config` leaves the catalog out of `config_key` on purpose
  (`src/canon_trading/ledger.py`, `trial_config`, `left_out=("catalog",)`), so
  re-running the same configuration after the catalog's bars change (a bar added
  on a previously closed day inside the same range, with a zero return that day)
  produces exactly this pair of records — same key, same start/end, same returns,
  different `session_days`.
- **How shown:** Probe with three traded records, then a fourth that repeats
  the second configuration with identical `returns_series`, `backtest_start`
  and `backtest_end` but with `session_days` missing day 3, a day whose return
  in that series is `0.001` (non-zero, so that record alone is rejected by
  `daily_series` as a return on a day with no bar):
  - Clean record first, inconsistent record second:
    `rank()` **passed** — `pick='pick', T=30`. The later record's calendar,
    which ADR 0010 step 2 refuses, was never looked at.
  - Same two records in the opposite order (inconsistent calendar first):
    `rank()` raised `RankingStopped: "second: return 0.001 on 2025-03-14, a
    day with no bar of the run (#1)"`.
  Same set of records, opposite gate outcome, decided only by trial order.
  The committed tests cannot see this: in `tests/test_ranking.py` the `write`
  helper (lines 86–93) always derives `session_days` from the same days as
  `returns_series`, and `tests/test_ledger.py` never re-runs one configuration
  against a changed catalog.

### 2. `rank` crashes with a bare `ValueError` / `KeyError` on ledger records whose attributes are not the exact types the ledger writes, instead of stopping with `RankingStopped` naming the trial

- **Where:** `src/canon_trading/ranking.py`, `rank()` lines 166–175 (only
  `ReturnsRejected` is caught and converted); `src/canon_trading/returns_matrix.py`,
  `daily_series()` lines 62, 75 and 82–83 (`int(key)`, `returns[str(key)]`,
  `int(day)` with no error translation).
- **What goes wrong:** `rank` documents its contract as "Rank every trial of
  `study` and test the pick, or raise `RankingStopped` saying why not"
  (`rank()` docstring, and `RankingStopped`: "the message names every trial
  that stopped it"). For any record that stops the ranking for an ADR 0035
  term 5/6 or ADR 0010 step 2 reason the caller is promised that exception.
  Records whose stored attributes are malformed in type or presence escape as
  ordinary Python errors with no trial named, so a caller handling
  `RankingStopped` as "the gate refused" instead gets a crash. `daily_series`
  likewise promises `ReturnsRejected` ("Returns that ADR 0010 step 2 does not
  accept; the message names the trial") but only raises it for the shapes it
  explicitly checks.
- **How shown:** Three separate probes, each a study that `rank` must refuse:
  1. A traded record with `session_days = ["not-a-day", …]` (rest well-formed):
     `ValueError: invalid literal for int() with base 10: 'not-a-day'`
     from `daily_series`, not `RankingStopped`.
  2. A traded record whose `returns_series` has one key `"abc"` alongside
     well-formed nanosecond keys: same bare `ValueError` from
     `int(key)` in `daily_series`.
  3. A traded record (`outcome=traded`, told `COMPLETE`) with no
     `returns_series` attribute at all: `KeyError: 'returns_series'` from
     `trial.user_attrs["returns_series"]` at `ranking.py` line 171.
  Each was run against a real study built with `open_study` in a temporary
  directory, beside well-formed traded records so only the one record is bad.
  Uncertain element: a record written by `record_trial` itself always has
  well-typed attributes (it writes `returns_series` before `outcome`), so
  these inputs need a record from outside that writer — an interrupted or
  older writer, or a study assembled by hand, which is exactly how
  `tests/test_ranking.py` itself builds studies. The committed ranking tests
  cover only well-typed rejections (e.g. `test_a_rejected_series_stops`,
  off-midnight keys that still parse as integers).

### 3. `daily_series` silently drops a session day that has no return, instead of refusing the series

- **Where:** `src/canon_trading/returns_matrix.py`, `daily_series()` lines
  82–97 (`kept = [key for key in keys if key in open_days]`).
- **What goes wrong:** The function promises "One trial's daily portfolio
  returns on its session days", and the module contract (ADR 0010 step 2) is
  that the daily series has one key per UTC day and any other shape is
  rejected. The code checks the returns keys exhaustively (midnight,
  consecutiveness, finiteness, non-zero on a closed day) but never checks the
  other direction: a `session_days` entry that is not a returns key is
  silently ignored. Such a day is one on which a bar of the run arrived (ADR
  0035 term 3) yet the portfolio series has no return for it, i.e. the two
  halves of the ledger entry disagree — but the trial passes step 2 with a
  shorter series. `returns_matrix` cannot catch it afterwards either, because
  the missing day never becomes a row, so its "calendars differ" check
  (lines 117–124) never sees it; `T`, and with it the Sharpe ratio and the
  DSR, are computed as if that session had not happened. A session day off a
  UTC midnight (e.g. a raw bar timestamp rather than its day floor) is the
  same gap in milder form: it matches no midnight key and is dropped the
  same way when the return that day is `0.0`.
- **How shown:** Direct call —
  `daily_series("t", {day0: 0.01, day0+1d: 0.02}, session_days=[day0, day0+1d, day0+5d])`
  returned a 2-row series with no error, although a third session day was
  claimed and has no return. The committed tests only ever pass
  `session_days` that are a subset of the returns keys
  (`tests/test_returns_matrix.py`, `sessions(...)` in every case).

### 4. False statement: `run_backtest`'s docstring says `record_trial` calls it; it does not

- **Where:** `src/canon_trading/backtest.py`, `run_backtest()` docstring,
  line 206: "A run meant as a trial goes through
  `canon_trading.ledger.record_trial`, which calls this."
- **What goes wrong:** `record_trial` never calls `run_backtest`. It builds
  the config itself and calls the two halves directly —
  `backtest_run_config(...)` then `run_backtest_config(run_config, strategies)`
  (`src/canon_trading/ledger.py` lines 222–235 and 241). A reader tracing
  which code a trial actually runs (for example to check the logging or the
  disposal behaviour described in `run_backtest`'s own docstring) is sent to
  a function that is not on that path; the two paths can also drift, since
  anything added to `run_backtest` alone would silently not apply to trials.
- **How shown:** Read of `ledger.py` `record_trial` in full; the only
  `canon_trading.backtest` names it imports are `backtest_run_config` and
  `run_backtest_config` (line 49–50), and `run_backtest` appears nowhere in
  the module.

### 5. Minor false statement: `backtest_run_config`'s error names a different function

- **Where:** `src/canon_trading/backtest.py`, `backtest_run_config()` line 144:
  `raise ValueError("run_backtest needs at least one strategy")`.
- **What goes wrong:** This check moved into the new public
  `backtest_run_config` in this commit, but the message kept the old
  function's name. Called directly — as the new API invites, and as
  `record_trial` and the tests do — an empty strategy list is refused with a
  message naming `run_backtest`, a function the caller did not call.
  Behaviour (it raises before anything is recorded) is right; only the
  statement in the message is false.
- **How shown:** Read of the diff: this line was `run_backtest`'s own check
  before the commit and was carried into `backtest_run_config` unchanged, now
  with `run_config_id` plumbing around it (lines 123–181).

### 6. Unsure, low severity: `returns_matrix` assumes its series are sorted and non-empty, and neither its contract nor its error says so

- **Where:** `src/canon_trading/returns_matrix.py`, `returns_matrix()` lines
  109–110 (`max(s.index[0] …)`, `min(s.index[-1] …)`).
- **What goes wrong:** The docstring only requires "one or more series with
  distinct names", but the common range is taken from the first and last
  element of each index, i.e. the series must already be date-sorted and
  non-empty. Outside that unstated precondition it fails wrongly:
  an empty series raises `IndexError: index 0 is out of bounds`, and a
  descending two-day series that overlaps another trial completely raises
  `ReturnsRejected: "the trials have no day in common: the latest first day
  is 2025-03-12 and the earliest last day 2025-03-11"` — a false statement
  about the input, which has both days in common.
- **How shown:** Direct calls with hand-built `pd.Series`:
  `returns_matrix([pd.Series([], dtype=float, name="a")])` → the
  `IndexError` above; a series indexed `[day1, day0]` joined with a
  `daily_series` over `[day0, day1]` → the false "no day in common"
  rejection above.
- **Why unsure:** Every in-repo caller passes `daily_series` output, which
  is always sorted and non-empty, so no committed path reaches this. It is
  a defect only if `returns_matrix` is meant to stand by its own stated
  contract for arbitrary series; reported as such.

## What was checked and found sound

- `record_trial`'s tell/outcome ordering (ADR 0035 term 2): a run that raises
  is told `FAIL`/`failed`, an idle run `FAIL`/`idle`, a traded run with a
  non-finite per-session Sharpe `FAIL` while still `traded`, and the outcome
  is written last — matches the code at `ledger.py` lines 240–264 and the
  ranking's treatment of those records in `_ran`.
- The keys in `trial_config` do change for every one-input change the tests
  name (strategy config, count, venue, data, engine, range, chunk size) and
  do not change for logging, catalog path or run id, per ADR 0035 term 4;
  verified by the passing parametrized tests above and by printing
  `canonical_json(trial_config(...))` for a default run config.
- The worked DSR numbers in `tests/test_ranking.py` (passing DSR 0.98849,
  failing 0.69241, N̂ 2.7755) are asserted both against skfolio and against
  an independent standard-library computation, and pass; the N̂ condition
  `T >= N(N-1)/2` and its absence from the gate match ADR 0010 step 3.
