---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Trial ledger: an Optuna study in a local SQLite file, one trial per backtest

## Context and Problem Statement

ADR 0010 ranks strategy variants on every trial in a ledger. It says what an entry holds (the
`BacktestRunConfig` with its `id`, and `run_config_id`, `backtest_start`, `backtest_end` and
`returns_series`) and that N is counted from the ledger, never typed. Where are entries kept, and what
writes them? How the key is hashed, N counted and the ranking stopped is ADR 0054, split from this
record on 2026-11-10.

## Decision Drivers

* Every trial is kept, one that raised or never traded included, whatever the run does.
* N is read back from the store, and a later session can open the store from a file.
* ADR 0001: glue, not invention.

## Considered Options

1. NautilusTrader's own persistence.
2. skfolio's `Population`.
3. An Optuna study.
4. MLflow runs.
5. A JSON Lines file of our own, written with the standard library.

## Decision Outcome

Chosen: **option 3**, **Optuna 5.0.0**. Options 1 and 2 hold no set of trials: at v2.0.0rc8 the run
config and result reach Python with no serialisation, and `Population` is a list in memory. Option 4
is the heaviest and adds nothing. Option 5 is a format this repo would own, states and locking included.
Maintained backtesting projects have **not converged** on a store (Evidence): this is the one maintained
trial store that meets every driver. Leaving it is cheap, since every trial reads back through
`Study.trials`; if Optuna fails here in use, option 5 replaces it.

1. **Storage.** `RDBStorage` on SQLite, the local storage Optuna's FAQ gives for saving and resuming a
   study; it keeps `JournalFileBackend` for parallel and NFS use. The file is `ledger/trials.db` in the
   main checkout, the folder holding the clone's common git directory, so every worktree shares it;
   outside a git checkout a path must be given. Opening never creates a file or study; creating one is
   its own call, with its sample (ADR 0041), under an exclusive `fcntl.flock` on `trials.db.lock`:
   two processes creating one new file raced in `create_all` and crashed in 14 of 15 rounds (measured
   here). The lock guards first creation and a sample's move (ADR 0041 term 5), not recording. The name and the sample are two writes; a creation
   interrupted between them is completed by the next creation of that name (ADR 0041 term 1). Recording stays on `RDBStorage`, not the
   `JournalFileBackend` the FAQ gives for parallel optimisation: sessions record trials now and then,
   not in a parallel search, and SQLite serialises those writes (3,100 recordings from 10 processes
   did not fail in the full review). The folder is ignored like `catalog/`. A ranking reads one
   study.
2. **Recording**, through ask and tell, the tutorial's way to apply Optuna to an existing problem. The
   key (term 4) is computed first; a call refused before a config exists is not a trial. The trial is
   then asked for, so a run that never returns stays as `RUNNING`. A run that raises is told `FAIL`
   by this repo's code, with user attribute `outcome` = `failed`; one with no filled order `FAIL` with
   `idle`; any other `COMPLETE` with `traded` and skfolio's per-session Sharpe ratio over its session
   days (for a later sampler; the gate does not read it), or `FAIL`, still `traded`, if that ratio is
   not finite. Such a record ran to the end: no later record resolves it, and ADR 0031 stops the gate.
3. **Entry.** User attributes `config`, `config_key`, `run_config_id`, `backtest_start`, `backtest_end`,
   `returns_series` (keyed by nanoseconds as text), and `session_days`, this record's addition: the UTC
   days on which a bar of the run arrived (one at exactly 00:00 counts for the day ending there, as
   NautilusTrader's daily series books it), read from the catalog, since `BacktestResult` has none.
   ADR 0010's `BacktestRunConfig` cannot be stored as an object, so `config` stands for it in the form
   ADR 0053 term 2 gives; `sample` is ADR 0041's and `data_digest` ADR 0053's.
4. **Identity.** ADR 0054 term 4: `config_key`, the SHA-256 of the canonical JSON of `config`.
5. **N.** ADR 0054 term 5: the number of distinct keys, on every sample.
6. **What stops the ranking.** ADR 0054 term 6.

Who decided. The orchestrator chose option 3 and the terms under the owner's delegation of 2026-11-10
(ADR 0010's Evidence).

### Consequences

* Good: the ledger is upstream code called as documented, and N is read from it.
* Bad: Optuna adds eight packages to `uv.lock`, and the config key is local glue (ADR 0054).
* Known limit: Optuna returns a trial's start as naive local time; the report converts it to UTC in
  the zone the process reads in, taking the earlier instant in the hour a fall-back repeats. A study
  object kept open while its process changes zone mixes zones.
* Known limit: when `RDBStorage`'s constructor fails (an older schema), its engine is left to the
  garbage collector; nothing public lets this repo dispose it.

### Confirmation

* Tests record a trial that traded, one that failed and one that never traded, and read them back
  through a new `Study` object (`tests/test_ledger.py`).
* A creation interrupted before its sample is completed by the next creation of its name
  (`tests/test_ledger.py`).
* N, the keys and the ranking's stops: ADR 0054's Confirmation.
* A trial queued in Optuna is never recorded in: `record_trial` tells it `FAIL`, keyless, and asks again
  (`test_a_trial_is_recorded_in_its_own_trial_not_one_from_optuna_s_queue`,
  `test_a_waiting_trial_added_with_attributes_is_not_recorded_in`), tests that failed before their fix.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/config.rs
  — `BacktestRunConfig` derives `Debug, Clone`, `id` defaults to `UUID4::new()`, and no config is
  hashed; `…/result.rs` and `…/python/result.rs`, the canonical result's BLAKE3 `digest` and Python
  getters only. Fetched. In `.venv` the config is not picklable and two equal configs are unequal. Run.
  `BacktestEngineConfig.streaming` is in the stub; its writer's files were not read: UNVERIFIED.
* skfolio 1.12.8 in `.venv`, `population/_population.py` — `Population(list)`; `returns_df` is
  `pd.concat(axis=1)` then `sort_index`. `bundled`.
* https://raw.githubusercontent.com/optuna/optuna/v5.0.0/optuna/study/_optimize.py — under `optimize`
  an exception sets `FAIL`; under ask and tell nothing does, so this repo's code tells it.
  `…/study/_tell.py`, a NaN told with no state is `FAIL`; `…/study/study.py`, `trials` returns every
  state; `…/storages/_rdb/models.py`, `value_json` is `Text`. Fetched; `v5.0.0` resolved to `01ddd17ab9f3`.
* https://raw.githubusercontent.com/optuna/optuna/v5.0.0/docs/source/faq.rst — SQLite to save and resume;
  `JournalFileBackend` for parallel or NFS use. `…/tutorial/20_recipes/009_ask_and_tell.py`, and
  `008_specify_params.py` (`add_trial` for sets already evaluated). Fetched.
* https://github.com/optuna/optuna/releases/tag/v5.0.0 — breaking: timestamps in UTC; the default
  sampler changed (multivariate TPE, constant liar), and the ledger suggests no parameters. https://pypi.org/pypi/optuna/json — 5.0.0, 2026-09-07, the latest;
  MIT; a Python 3.14 classifier. Fetched.
* `uv pip compile` of the lock's 41 pins plus `optuna==5.0.0` on Python 3.14 moves no locked version;
  `uv add` added alembic 1.20.1, colorlog 6.12.0, mako 1.4.3, markupsafe 3.0.4, pyyaml 6.0.3,
  sqlalchemy 2.1.8 and tqdm 4.70.1. An ask/tell round trip on SQLite, read from a second process: four
  trials, `COMPLETE`, two `FAIL` and one `RUNNING`, a 1,000-entry series intact. Run.
* https://pypi.org/pypi/mlflow-skinny/json — 3.18.0 resolves to 43 packages, itself included. Run.
* https://raw.githubusercontent.com/freqtrade/freqtrade/2026.10/freqtrade/optimize/hyperopt/hyperopt.py
  — epochs appended as JSON lines to a `.fthypt` file; `hyperopt_optimizer.py` keeps an epoch under the
  minimum trade count at `MAX_LOSS` and uses Optuna as sampler only. GPL-3.0. Fetched. Whether a raised
  epoch is written: UNVERIFIED.
* https://raw.githubusercontent.com/eslazarev/purged-cross-validation/v0.1.12/src/purgedcv/optuna_integration.py
  — `TrialSharpeRecorder` keeps only finite Sharpe ratios from Optuna user attributes, which ADR 0031
  forbids. Not adopted. Fetched.
* Reported by an `upstream-scout` search and not opened, so UNVERIFIED: MLflow marks a run `FAILED`
  inside `start_run` and is moving its file store to SQLite; backtesting.py keeps `optimize` results in
  memory; DVC, Aim and Sacred; other DSR and PBO projects take arrays or CSV files and own no store.
* Second reader, 2026-11-10 (`evidence-verifier`), two passes: the sources hold. It corrected the key's
  fields, term 6's conditions, the FAIL claim, the unmarked readings of ADR 0010, the ignore rule, and a
  failed record that would have stopped the ranking for good, now resolved by a later run of its key.
* Full review, 2026-11-10 (`/code-review`): the ledger's place hung on the current directory and
  opening created what was missing; the key hashed text as typed and upstream's reflected defaults, was
  read from a config that could change after the strategy was built, and held nothing of the data. Terms
  1 and 5 corrected, terms 3 and 4 point to ADR 0041, which holds the key, the sample and the data.
* Full review, 2026-11-10: a bar at exactly a UTC midnight fell on another day than its return; the
  writes after `ask` sat outside the `try`; stops named no trial and came one stage at a time. Terms 3
  and 6 corrected. `crates/analysis/src/analyzer.rs` at the pinned commit: `snapshot_day_start` moves a
  non-zero midnight back one day. Fetched; a run of daily bars stamped at 00:00 UTC ranked T = 40.
* Full review, 2026-11-10: first creation by two processes crashed; the report's times were naive local
  time. Term 1 and the two known limits answer them. The standard library's `fcntl.flock`; Optuna
  5.0.0 `storages/_rdb/models.py`, which stores `datetime_start` in UTC and returns it local. `bundled`.
* Opening without creating, read 2026-11-10: `optuna.load_study` raises `KeyError` for a missing study
  and `RDBStorage(skip_table_creation=True)` creates no table (Optuna 5.0.0, `study.py`,
  `storages/_rdb/storage.py`, `bundled`); https://www.sqlite.org/uri.html gives `rw` beside `rwc`,
  "read-write and created if it does not exist", and `%HH` escapes in the path (fetched; that `rw`
  refuses a missing file is not stated there, and was run on seven paths); SQLAlchemy 2.1.8
  percent-decodes a URL's database part (`engine/url.py`) and takes `uri=true` (`dialects/sqlite/
  pysqlite.py`, `bundled`); `git rev-parse --git-common-dir` names the clone's git directory from any
  worktree (run). Opening an empty file raised `StorageInternalError` and left it unchanged (run).
* Code review, 2026-11-10 (`/code-review`): term 2 missed a traded run with no finite Sharpe ratio, and
  the value told used calendar days. Both corrected, with no change of decision.
* A second review of the gate, 2026-11-10, read only: a creation interrupted between the name and the
  sample could be neither opened nor created again, and a keyed failure written before its sample
  number was left out of the ranking. Term 1 corrected here; the rest, and the sources, in ADR 0041's
  Evidence. No change of decision.
* A third review of the gate, 2026-11-10, read only: records of one key differing only in outcome gave
  an answer that hung on their order, and the report credited the pick with records that never ran.
  Terms 1 and 5 corrected here, with the rest and the sources in ADR 0041's Evidence. No change of
  decision.
* ADR 0041's key and data identity, and part of the reviews' sources cited above, moved to ADR 0053 on
  2026-11-10, with no change of decision; terms 3 to 6 point to ADR 0053 for them.
* Terms 4 to 6, the key, N and what stops the ranking, with their consequences, tests and evidence,
  moved to ADR 0054 on 2026-11-10, with no change of decision; the Evidence lines that support both
  records are kept in both.
* A fourth review of the gate, 2026-11-10, read only, by a second reader, with its code review:
  `Study.ask` takes the oldest queued `WAITING` trial before it creates one (Optuna 5.0.0
  `study/study.py`, `_pop_waiting_trial_id`, `bundled`), so a record could be written into another
  writer's trial, with its attributes, at a number below records already made, where ADR 0054 term 5
  reads a higher number as later. Term 2's recording now asks for a trial `ask` created; a taken one is
  told `FAIL` with no key. Reproduced as a failing test. No change of decision.
