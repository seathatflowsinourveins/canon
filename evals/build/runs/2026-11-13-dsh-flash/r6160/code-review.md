# /code-review high on r6160's first/copy

Run from the scoring session as a forked `/code-review` at high effort, target the copy's uncommitted diff. Finished 2026-11-13 by 15:31:33 New York, read from date after it ended (117 s, 12 tool uses). These are the review's candidate findings, in its order. They are not marks. The review's own note: of its eight findings, two were checked against source (finding 3: the Optuna 5.0.0 in the venv stores `datetime_start` as naive UTC and returns it as naive local time, `storages/_rdb/models.py`; finding 6: skfolio turns NaN into 0.0, `_nan_to_zero`); the rest were not run.

```json
[
  {
    "file": "src/canon_trading/ledger.py",
    "line": 217,
    "summary": "Only creators take the new creation lock; `open_study` opens the ledger without it, so a reader can still race a creator.",
    "failure_scenario": "Process A is inside `create_ledger_study`: the SQLite file exists, but its tables or the study's `samples` attribute are not written yet. Process B calls `open_study` on the same path and passes `path.is_file()`. It then hits a missing table (StorageInternalError, reported as 'is not an Optuna ledger') or a study with no sample (`samples()` raises LedgerNotFound). A healthy ledger is reported as corrupt or missing, even though the docstring says creating it is 'one moment'."
  },
  {
    "file": "src/canon_trading/ledger.py",
    "line": 496,
    "summary": "`record_trial` catches `ReturnsRejected` and does nothing with it, so a traded run whose series the gate refuses is told FAIL with no `error` attribute and no reason recorded.",
    "failure_scenario": "NautilusTrader falls back to per-position returns keyed off midnight. `daily_series` raises, the `pass` drops the message, and the trial is stored as FAIL/traded with no explanation. In the ledger it looks just like a NaN-Sharpe FAIL; the cause only shows up again if someone runs `rank()`. Record the rejection text, for example as `trial.set_user_attr('error', str(rejected))`."
  },
  {
    "file": "src/canon_trading/ranking.py",
    "line": 318,
    "summary": "`astimezone(UTC)` is applied to the naive local time Optuna returns, which has already lost DST fold information, so trials started in the repeated fall-back hour come out an hour off.",
    "failure_scenario": "With TZ=America/New_York, a trial starts at 05:30 UTC on the fall-back day (01:30 EST, the second 01:30). Optuna returns a naive 01:30 with fold=0, and `astimezone(UTC)` reads it as EDT, giving 05:30 UTC -> 04:30 UTC. `first_recorded`/`last_recorded` are then wrong and differ from the Tokyo/UTC readings. That breaks the 'same in every zone' property the new docstring and test claim, and can reorder starts."
  },
  {
    "file": ".gitignore",
    "line": 21,
    "summary": "Changing `ledger/` to `/ledger/` stops ignoring `ledger/` folders below the root, which can now be committed with their SQLite study and the new `.lock` file.",
    "failure_scenario": "`create_ledger_study(..., path=Path('filings/ledger/trials.db'))` (a separate project under the root), or any relative path run from a subfolder, creates `<sub>/ledger/trials.db` and `trials.db.lock`. These now show as untracked and can be committed by `git add -A`. ADR 0035 says the ledger is 'never committed'."
  },
  {
    "file": "src/canon_trading/ledger.py",
    "line": 50,
    "summary": "A module-level `import fcntl` makes `canon_trading.ledger` impossible to import where fcntl is missing, which also takes down `returns_matrix` and `ranking`, both of which import it.",
    "failure_scenario": "On a non-POSIX interpreter (Windows), `import canon_trading.ranking` fails with ModuleNotFoundError: fcntl, even for read-only ranking that never creates a ledger. Importing fcntl inside `_creation_lock` would keep the failure inside creation."
  },
  {
    "file": "src/canon_trading/returns_matrix.py",
    "line": 178,
    "summary": "The new not-finite check in `returns_matrix` repeats the check `daily_series` already makes, is dead on the ranking path, and raises before the missing-day check, so one stop no longer lists every bad trial.",
    "failure_scenario": "`rank()` only passes series built by `daily_series`, which already refuses non-finite values, so the check never fires there. A direct caller with series 'a' (contains NaN) and 'b' (missing days) gets only the NaN stop; 'b' appears only after a second call. That breaks the docstring's 'one stop lists every such trial'. The hand-written `_first_not_finite` loop with try/except also re-implements a vectorised `~np.isfinite(pd.to_numeric(series, errors='coerce'))`."
  },
  {
    "file": "src/canon_trading/backtest.py",
    "line": 250,
    "summary": "The new empty-strategies guard in `run_backtest_config` duplicates the one `backtest_run_config` already has, with a different message. If it ever fired from `record_trial` it would do so after `study.ask()` and leave a FAIL record.",
    "failure_scenario": "Both callers (`run_backtest` and `record_trial`) go through `backtest_run_config`, which raises 'run_backtest needs at least one strategy' first, so the new check is unreachable. If a caller ever built the config another way, the check would fire inside `record_trial`'s try. The failure would then be recorded as a keyed FAIL trial, against the rule that 'a call refused before a config exists is not a trial'. Checking the strategies against the config's bar types would be the more useful precondition."
  },
  {
    "file": "src/canon_trading/ledger.py",
    "line": 492,
    "summary": "A local import inside `record_trial` works around the ledger <-> returns_matrix import cycle instead of removing it, and `_session_sharpe` repeats checks and Portfolio construction that `ranking` already does differently.",
    "failure_scenario": "Because `returns_matrix` imports `DAY_NS` from `ledger`, the import has to sit inside the hot `try` block, where an import error is recorded as a failed trial. Moving `DAY_NS` (and `session_day`) into a small shared module would remove the cycle. `_session_sharpe` also rechecks finiteness that `daily_series` guarantees and builds `Portfolio(X=[[v]...])`, while `ranking` uses `Portfolio(X=series.to_frame(), weights=[1.0])`. Two copies of the 'per-session Sharpe' that can drift apart; one shared helper would do."
  }
]
```
