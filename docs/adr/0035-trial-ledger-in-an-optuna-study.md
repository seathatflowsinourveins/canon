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
writes them?

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
   study; it keeps `JournalFileBackend` for parallel and NFS use. The file is `ledger/trials.db`, a
   folder to be git-ignored like `catalog/` by the code that writes it. A ranking reads one study.
2. **Recording**, through ask and tell, the tutorial's way to apply Optuna to an existing problem. The
   key (term 4) is computed first; a call refused before a config exists is not a trial. The trial is
   then asked for, so a run that never returns stays as `RUNNING`. A run that raises is told `FAIL`
   by this repo's code, with user attribute `outcome` = `failed`; one with no filled order `FAIL` with
   `idle`; any other `COMPLETE` with `traded`, and the value told is skfolio's per-session Sharpe
   ratio of its own series, for a later sampler; the gate does not read it.
3. **Entry.** User attributes `config`, `config_key`, `run_config_id`, `backtest_start`, `backtest_end`,
   `returns_series` (keyed by nanoseconds as text), and `session_days`, this record's addition: the UTC
   days on which a bar of the run arrived, read here from the catalog, since `BacktestResult` has none.
   ADR 0010's `BacktestRunConfig` cannot be stored as an object, so `config` is its canonical form.
4. **Identity.** `id` defaults to a fresh UUID4, and no config identity reaches Python. `config_key` is
   the SHA-256 hex digest of the UTF-8 bytes of `json.dumps(config, sort_keys=True,
   separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, passed as `BacktestRunConfig(id=…)`.
   `config` covers what `run_backtest` sets: each strategy's class (`module:Class`) and every public
   config field (it adds no actor or execution algorithm); every public property of each venue config,
   of each data config but its catalog, and of the engine config but its logging, instance ID,
   catalogs and streaming, which name places, not data; and the run's `start`, `end` and `chunk_size`.
   Floats are written by `repr`, enums by name, NautilusTrader types and decimals by `str()`; a value
   with no stable text form is refused. Upstream hashes only a result
   (`CanonicalBacktestResult::digest`, BLAKE3, not exposed); an exposed upstream hash replaces this.
5. **N** is the number of distinct `config_key` values in the study, in every state, never filtered by
   state. ADR 0010 counts trials; reading a configuration run again as the same trial is this record's.
   Records are never overwritten or deleted. Per key, a `failed` or `RUNNING` record is resolved by a
   later record of that key that ran to the end (traded or idle); the earlier one stays. Every record
   of a key that ran to the end must carry the same range and returns, else the ranking stops and
   names them. A key is one column of the returns matrix.
6. **What stops the ranking**, beside ADR 0031's four conditions (every trial's Sharpe ratio and every
   return of the pick finite, N ≥ 2, V > 0): a key with no record that ran to the end; an idle key
   (ADR 0010's Limits); and a key whose returns ADR 0010 step 2 rejects. Each message names the trial.
   A configuration that can never run to the end stops the ranking for as long as that study is used.
   That is the strict reading: a tried configuration is a trial, and no source read gives it a value.
   Like the idle trial, it is undecided, not solved.

Who decided. The orchestrator chose option 3 and the terms under the owner's delegation of 2026-11-10
(ADR 0010's Evidence).

### Consequences

* Good: the ledger is upstream code called as documented, and N is read from it.
* Bad: Optuna adds eight packages to `uv.lock`, and the config key is local glue.
* Bad: a configuration that fails every time, or never trades, blocks the study's ranking.

### Confirmation

* Tests record a trial that traded, one that failed and one that never traded, and one configuration
  twice, then read N back through a new `Study` object; runs differing in one input get different keys.
* A ranking report names the study.

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
* Second reader, 2026-11-10 (`evidence-verifier`): the sources hold. It corrected the key, which left out
  data, engine and run fields; term 6, which named one of ADR 0031's four conditions; the FAIL claim,
  true of `optimize` only; the unmarked readings of ADR 0010's entry and N; and a failed or crashed
  record that would have stopped the ranking for good, now resolved by a later run of its key. On a
  second pass it corrected the ignore rule, the engine's paths in the key, and two broad phrasings.
