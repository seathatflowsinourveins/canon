---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Reading the trial ledger: a hash of the config as the key, N as the count of keys, and what stops the ranking

## Context and Problem Statement

ADR 0035 keeps every backtest as a trial in an Optuna study, and ADR 0010 ranks the variants on every
trial in it, with N counted from the ledger, never typed. How is a trial's configuration identified, how
is N counted, and what stops the ranking from reading the study? Split from ADR 0035 on 2026-11-10, with
no change of decision.

## Decision Drivers

* Every trial is kept, one that raised or never traded included, whatever the run does.
* N is read back from the store, and a later session can open the store from a file.
* ADR 0001: glue, not invention.

## Considered Options

These terms were decided with the store, ADR 0035's option 3 (an Optuna study), and weighed no options
of their own; ADR 0035's Considered Options are the record's.

## Decision Outcome

Chosen with ADR 0035's option 3, split from it on 2026-11-10 with no change of decision. The terms keep
ADR 0035's numbers, 4 to 6; terms 1 to 3 (storage, recording, entry) are ADR 0035's.

4. **Identity.** `id` defaults to a fresh UUID4, and no config identity reaches Python. `config_key` is
   the SHA-256 hex digest of the UTF-8 bytes of `json.dumps(config, sort_keys=True,
   separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, passed as `BacktestRunConfig(id=…)`.
   Superseded in part by ADR 0053 term 2: what `config` holds and how its values are written. Until
   then it held every public property upstream reflects and the run's `start` and `end`. Upstream
   hashes only a result (`CanonicalBacktestResult::digest`, BLAKE3, not exposed); an exposed upstream
   hash replaces this.
5. **N** is the number of distinct `config_key` values in the study, in every state and on every
   sample (ADR 0041), never filtered by state. ADR 0010 counts trials; reading a configuration run
   again as the same trial is this record's. Records are never overwritten or deleted. Per key, a
   `failed` or `RUNNING` record is resolved by a later record of that key that ran to the end (traded
   or idle); the earlier one stays. Every record of a key that ran to the end on one sample must carry
   the same outcome, range, returns and data (ADR 0053), else the ranking stops and names them. A key is one
   column of the returns matrix.
6. **What stops the ranking**, beside ADR 0031's, ADR 0041's and ADR 0053's: a study with no record; a key with no
   record that ran to the end; a `failed` or `RUNNING` record no later record of its key resolves;
   records of one key that disagree (term 5); an idle key (ADR 0010's Limits); and returns ADR 0010
   step 2 rejects, or a record not in the shape `record_trial` writes. Each message names the key and
   trial; one ranking gathers every trial's stops before it raises.
   A configuration that can never run to the end stops the ranking for as long as that study is used.
   That is the strict reading: a tried configuration is a trial, and no source read gives it a value.
   Like the idle trial, it is undecided, not solved.

Who decided. The orchestrator chose option 3 and the terms under the owner's delegation of 2026-11-10
(ADR 0010's Evidence).

### Consequences

* Good: N is read from the ledger, never typed.
* Bad: the config key is local glue.
* Bad: a configuration that fails every time, or never trades, blocks the study's ranking.

### Confirmation

* Tests read N back through a new `Study` object after recording three configurations, and record one
  configuration twice as one key with two records; runs differing in one input get different keys
  (`tests/test_ledger.py`).
* A ranking report names the study (`RankingReport.study`, `ranking.py`, read); no test asserts it.
* A keyed failure with no sample number stops the ranking until a later record of its key resolves it
  (`tests/test_ledger.py`).
* Records of one key that differ only in outcome stop the ranking in either order, and a report names
  only the pick's records that ran (`tests/test_ranking.py`).
* A keyed record in a state and outcome `record_trial` never leaves stops for good, and no later record
  resolves it (`test_a_keyed_record_with_a_result_and_no_outcome_stops_for_good`,
  `test_a_record_that_ran_in_a_state_the_writer_never_tells_stops`), tests that failed before their fix.
* A fifth review's case, tests that failed before their fix: a record whose stored `config` does not
  hash to its key, one that ran (as a fourth configuration under the first's key, or as the pick) or
  one that failed, stops the ranking for good, named, on any sample
  (`test_a_record_whose_config_is_not_the_one_its_key_hashes_stops`,
  `test_a_pick_whose_config_is_another_configuration_s_stops`,
  `test_a_failed_record_whose_config_is_not_its_key_s_stops`).
* A sixth review's cases, tests that failed before their fix: another writer's record stops on every
  sample, so a move to the same range clears none
  (`test_a_record_of_another_writer_on_an_earlier_sample_still_stops`,
  `test_a_record_that_ran_on_an_earlier_sample_with_no_config_still_stops`); a keyed record queued or
  holding a system attribute (`test_a_keyed_record_queued_or_with_system_attributes_is_another_writers`),
  one `record_trial` took from Optuna's queue (`test_a_keyed_record_record_trial_took_from_the_queue_stops`,
  `test_a_queued_trial_with_a_key_that_record_trial_takes_stops_the_ranking`), a value told that is not its
  returns' Sharpe ratio (`test_a_told_value_that_is_not_the_sharpe_ratio_of_its_returns_stops`) and a
  config not in `record_trial`'s three parts (`test_a_config_not_in_the_form_record_trial_writes_stops`)
  stop for good. `test_a_keyed_record_with_a_result_and_no_outcome_stops_for_good` now keys its record
  with the pick's own key, as the regression it names needs.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here. The
lines below are ADR 0035's, kept in both records where they support both; three are reworded to read
from this record (which term was corrected where, and that the key and data moved on to ADR 0053).
Term numbers are ADR 0035's.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/config.rs
  — `BacktestRunConfig` derives `Debug, Clone`, `id` defaults to `UUID4::new()`, and no config is
  hashed; `…/result.rs` and `…/python/result.rs`, the canonical result's BLAKE3 `digest` and Python
  getters only. Fetched. In `.venv` the config is not picklable and two equal configs are unequal. Run.
  `BacktestEngineConfig.streaming` is in the stub; its writer's files were not read: UNVERIFIED.
* https://raw.githubusercontent.com/optuna/optuna/v5.0.0/optuna/study/_optimize.py — under `optimize`
  an exception sets `FAIL`; under ask and tell nothing does, so this repo's code tells it.
  `…/study/_tell.py`, a NaN told with no state is `FAIL`; `…/study/study.py`, `trials` returns every
  state; `…/storages/_rdb/models.py`, `value_json` is `Text`. Fetched; `v5.0.0` resolved to `01ddd17ab9f3`.
* Second reader, 2026-11-10 (`evidence-verifier`), two passes: the sources hold. It corrected the key's
  fields, term 6's conditions, the FAIL claim, the unmarked readings of ADR 0010, the ignore rule, and a
  failed record that would have stopped the ranking for good, now resolved by a later run of its key.
* Full review, 2026-11-10 (`/code-review`): the ledger's place hung on the current directory and
  opening created what was missing; the key hashed text as typed and upstream's reflected defaults, was
  read from a config that could change after the strategy was built, and held nothing of the data. Terms
  1 and 5 corrected, terms 3 and 4 point to ADR 0041, which holds the key, the sample and the data (the key and the
  data since moved to ADR 0053).
* Full review, 2026-11-10: a bar at exactly a UTC midnight fell on another day than its return; the
  writes after `ask` sat outside the `try`; stops named no trial and came one stage at a time. Terms 3
  and 6 corrected. `crates/analysis/src/analyzer.rs` at the pinned commit: `snapshot_day_start` moves a
  non-zero midnight back one day. Fetched; a run of daily bars stamped at 00:00 UTC ranked T = 40.
* A second review of the gate, 2026-11-10, read only: a creation interrupted between the name and the
  sample could be neither opened nor created again, and a keyed failure written before its sample
  number was left out of the ranking. Term 1 corrected in ADR 0035; the rest, and the sources, in ADR 0041's
  Evidence. No change of decision.
* A third review of the gate, 2026-11-10, read only: records of one key differing only in outcome gave
  an answer that hung on their order, and the report credited the pick with records that never ran.
  Terms 1 and 5 corrected, with the rest and the sources in ADR 0041's Evidence. No change of
  decision.
* ADR 0041's key and data identity, and part of the reviews' sources cited above, moved to ADR 0053 on
  2026-11-10, with no change of decision; terms 3 to 6 point to ADR 0053 for them.
* ADR 0035 as it stood on `main` before 2026-11-10's split: terms 4 to 6, their consequences, tests and
  evidence, moved here with no change of decision (run: `git show` of the record on `main`).
* Second reader (`evidence-verifier`), 2026-11-10: the sources and the moved terms hold; it corrected
  the first Confirmation line, which had joined two tests' halves, this section's preamble and its
  labels. Code review (`/code-review`, medium) of the split, the same day: references left on ADR 0035
  for N, the key and the stops; corrected.
* A fourth review of the gate, 2026-11-10, read only, by a second reader: a keyed `COMPLETE` record with
  a value and no outcome was read as a failure, so a later traded record of its key passed and the
  completed one was never checked; its code review added `PRUNED` and `traded`, and `COMPLETE` and
  `idle`, read as runs. Term 5 resolves a record cut short as `record_trial` leaves one, `RUNNING`, or
  `FAIL` with outcome `failed` or none; the ranking now stops for good on a keyed record in any state
  and outcome `record_trial` never leaves, a result (`COMPLETE`, `PRUNED`, a value or an intermediate
  value; Optuna 5.0.0 `trial/_state.py`, `bundled`) or a suggested parameter among them. Reproduced as
  failing tests. No change of decision.
* A fifth review of the gate, 2026-11-10, by a second reader: term 4's key was never checked against
  the `config` stored beside it, so another writer's record could take a configuration's place under
  another's key and lower N (four configurations ranked as N = 3 and passed). The ranking now hashes
  every stored `config` and stops on one that is not its key's. The eight step 7 records hash to their
  keys (read from a backup copy of the shared ledger, run). Reproduced as failing tests. No change of
  decision.
* A sixth review of the gate, 2026-11-10, by a second reader and a third, each finding reproduced here
  as a failing test: term 6's records not in `record_trial`'s shape were read only on the current
  sample, so a move of the sample to the same range cleared another writer's record, one completed with
  no outcome or one that ran with no `config`, and ranked at N = 3; they are now read on every sample,
  as N counts keys on every sample, except what is measured against a sample's own bounds. A keyed
  `WAITING` record (`Study.enqueue_trial`, which writes `fixed_params`) or one with a system attribute
  was read as a failure a later record resolves; `record_trial` writes neither. `record_trial` took
  another writer's queued record of a key and told it `FAIL` with that writer's outcome kept, which read
  as a run of its key, and the ranking passed on returns no backtest gave: taken (Optuna 5.0.0
  `Study.ask` takes the oldest queued trial, `_pop_waiting_trial_id`, `bundled`), it carries
  `record_trial`'s note, and the ranking stops on a keyed record with that note for good. Kept as a
  known limit: a refused write of the note leaves it `FAIL` with the other writer's outcome, read as a
  run of its key; a refusal before `ask` would close that, and is not decided here (below). A value told against its returns, and a config holding only the data, passed. Under the
  regression its docstring names, the test of a record with a result and no outcome passed, its key the
  text `pick` and not the pick's (run). No change of decision.
* What a study stopped for good does to N, read in these records on 2026-11-10: N is the number of
  keys in the study (term 5), a ranking reads one study (ADR 0035 term 1), and counting N over a chain
  of studies, each naming the one before, was considered and not chosen (ADR 0041, option 3). So a
  study that another writer's record stops for good keeps its N but is never ranked again, and a new
  study of the same configurations is ranked at its own N, counted from 0: no record carries the old
  study's keys into a successor. A refusal that leaves a new study as the only remedy would therefore
  be a way to lower N, which the protocol forbids (DSR paper, p. 10, fn. 1), and `record_trial` does
  not refuse a study that holds such a record; carrying N by key into a named successor, or a recorded
  retirement of the foreign record with its key still counted, is undecided. Stated limit: any writer
  to the ledger can queue a keyed trial (`Study.enqueue_trial`) and so stop a study for good. The
  ledger is a local file whose writers are this repository's own sessions, so this is stated, not
  defended against.
