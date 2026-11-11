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
6. **What stops the ranking**, beside ADR 0031's, ADR 0041's and ADR 0053's: a study with no record, or
   of other directions than one objective maximized (`record_trial` tells one value, the Sharpe
   ratio, and `open_study` refuses such a study); a key with no
   record that ran to the end; a `failed` or `RUNNING` record no later record of its key resolves;
   records of one key that disagree (term 5); an idle key (ADR 0010's Limits); and returns ADR 0010
   step 2 rejects, or a record not in the shape `record_trial` writes. Every record that ran on the
   sample has its returns read before any two are compared: each a float, finite, the sum of their
   squares finite, and skfolio's variance and fourth central moment of them finite, or its Sharpe
   ratio of them reads 0.0 and the DSR's kurtosis NaN (2026-11-11). A stored config whose data does
   not load exactly the bar types its strategies trade, each once, in order, as `trial_config` writes
   them, is not in that shape either, on every sample (2026-11-11). Each message names the key and
   trial; one ranking gathers every trial's stops before it raises.
   A configuration that can never run to the end stops the ranking for as long as that study is used.
   That is the strict reading: a tried configuration is a trial, and no source read gives it a value.
   Like the idle trial, it is undecided, not solved.
7. **Retiring another writer's queued record** (added 2026-11-10). A record with a config key that
   another writer queued in Optuna (`WAITING`) never ran here, and whatever results it holds are that
   writer's. `record_trial` refuses to ask for a trial while one waits, naming it and the remedy:
   `retire_queued(study, n)`, an operator's action on that one record. Retired, a record is never read
   as a run, and its key stays counted in N as a configuration that was tried: the strict side, which
   is the gate's. It is kept, as a record in `not_run_waiting` is (ADR 0053 term 3), but unlike that
   keyless record its key is known, and counted. Its number joins the study user attribute `retired`,
   written under the ledger's lock before the record is taken from the queue (`Study.ask`) and told
   `FAIL` with a note, so a retirement cut short reads as retired, never as a run; nothing is deleted,
   and a second retirement is refused. A keyed record taken from the queue with a note before (as
   `record_trial` took one before it refused) is retired by the list alone if it is `FAIL`, since
   Optuna writes nothing on a finished trial, and told `FAIL` after the list if its `tell` was refused
   and it is still `RUNNING`. A retired record is not read in any state: whatever another writer does
   to it later, it gives the ranking no returns, and its key is counted either way. A key tried only
   in retired records stops the ranking until `record_trial` runs it on the current sample; a key no
   configuration `record_trial` runs hashes to therefore stops it for good, the strict side, and so does
   one outside the study's plan (ADR 0048) for as long as the plan is kept. Only another writer's queued record is retired: a record
   `record_trial` wrote is a trial of its key, and only a later record of that key resolves it. Every
   stop on a record `retire_queued` takes names it; the stop on a key tried only in retired records
   names `record_trial`. A `retired` list in another form is refused by `record_trial`, `retire_queued`
   and the study's pass (`canon_trading.variant_study`), and stops the ranking.
   Named limits. A keyed trial queued between `record_trial`'s check and its `ask` is still taken,
   told `FAIL` with `record_trial`'s note, and the ranking stops on it naming `retire_queued`. So is a
   trial `ask` gives, in `record_trial` or in `retire_queued`'s own take, that the store refuses to read
   back: nothing read tells a trial `ask` created from one it took, so it carries the queue's note and
   the read's error is raised naming it (one `ask` created holds no key, and `mark_not_run` clears
   it, as one `record_trial` takes from the queue). If the store refuses that note too, the error
   raised names the trial and the limit (and the refused `tell`, if that is refused as well): for a
   trial read back, the store's error, which names its key too; for one not read back, the read's
   error, since no key was read. What the operator sees is that error. The record is then `FAIL`
   (or `RUNNING`, if that `tell` is refused too) with the other writer's outcome and no note: one
   queued through
   `Study.enqueue_trial` holds the system attribute `fixed_params`, and the ranking stops on it for
   good; one queued with no system attribute reads as a run of its key, as before (Evidence, sixth
   review). `retire_queued` refuses either, since nothing on it tells it from `record_trial`'s own
   `FAIL`, so the ledger offers no remedy for it. And a writer of the study's attributes other than
   `retire_queued` could put a record of `record_trial`'s in the list, which hides it from every check
   but not its key from N; like the queue, the ledger's writers are this repository's own sessions, so
   this is stated, not defended against.
   **One model of who wrote a record** (2026-11-11; ADR 0073 rule 4, the queued record's family found
   in a second round). It is decided in one place, `canon_trading.ledger.origin`, which `record_trial`,
   `retire_queued`, `mark_not_run`, the ranking and the study's pass all read. By key, state, note and
   attributes, in that order (values first, 2026-11-11): a record told more than one value is
   another writer's; a keyless record that shows a run is another writer's; a `WAITING` one
   is queued; one `FAIL` or `RUNNING` with a queue note this module writes on it was taken from the
   queue here and ran nothing (on a keyless record that is `record_trial`'s note alone, since
   `retire_queued` takes only a keyed one); one with a queue note in another state, a system attribute, a user attribute `record_trial` does
   not write (on a keyless record, any but its error), a parameter or an intermediate value was
   started, told or written by another writer outside the queue's take, and may have run anything;
   anything else is `record_trial`'s own. When: a keyless record leaves N only by its mark, and only
   while it is in the state the mark leaves (`FAIL` in `not_run`; in `not_run_waiting`, `WAITING`, or
   `FAIL` once taken here; one taken here and left `RUNNING`, its `tell` refused, `mark_not_run` tells
   `FAIL`, marked or not). One another writer started is cleared by nothing, a mark made before
   included, so `mark_not_run` refuses it and the ranking stops on it for good. Only a keyed record
   of `record_trial`'s own is read by the pass as its written failure. This narrows ADR 0053 term 3:
   a keyless record ran no backtest only if it is `record_trial`'s own or one taken here. A trial
   number given to `mark_not_run` or `retire_queued` is an `int`, never a `bool` or a float. Named
   limit: a keyless queued trial taken here whose note the store refuses holds `fixed_params` and no
   note, so it reads as another writer's and stops the study for good, the strict side; the error
   raised says so.

Who decided. The orchestrator chose option 3 and the terms under the owner's delegation of 2026-11-10
(ADR 0010's Evidence). Term 7 was decided by the orchestrator under the same delegation, 2026-11-10, over a
chain of studies (ADR 0041's option 3, not chosen) and over carrying N by key into a named successor
study, a larger mechanism than one foreign record needs.

### Consequences

* Good: N is read from the ledger, never typed.
* Bad: the config key is local glue.
* Bad: a configuration that fails every time, or never trades, blocks the study's ranking.
* Good: another writer's queued record of a configuration the study runs no longer stops it for good;
  retired, its key still counts, so the remedy never lowers N (term 7).
* Bad: retirement is an operator's action; a retired key no configuration of the study runs stops it
  for good, and the race term 7 names is left to the operator.

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
* Term 7, tests that failed before their change: `record_trial` refuses beside a keyed queued record,
  with its note refused too, and the record stays `WAITING`
  (`test_record_trial_refuses_beside_another_writer_s_keyed_queued_trial`); N is the same or higher
  after a retirement, on every path: a retired record alone on its key, then with a later run of its
  key (`test_a_retired_queued_record_keeps_its_key_counted_and_record_trial_goes_on`,
  `test_a_key_tried_only_in_a_retired_record_stays_counted_and_names_record_trial`), and a record the
  code before took from the queue, retired by the list alone
  (`test_a_record_taken_by_the_code_before_is_retired_by_the_list_alone`,
  `test_a_retired_record_is_not_read_and_its_key_stays_counted`); a second retirement is refused and
  `record_trial` goes on afterwards (the first of those); a retirement stopped between the list and the
  take, or with its note refused, reads as retired and never as a run
  (`test_a_retirement_cut_short_reads_as_retired_never_as_a_run`); the queue is retired in its order,
  and every other record is refused (`test_retire_queued_retires_only_another_writer_s_queued_record_in_queue_order`);
  the stops name the remedy (`test_a_waiting_keyed_record_names_retire_queued`, and the tests above);
  a `retired` list in another form stops (`test_a_retired_list_not_in_its_form_stops`); the study's
  pass runs a key whose only record is retired
  (`tests/test_variant_study.py::test_a_key_whose_only_record_on_the_sample_is_retired_is_run`).
  From the review of term 7, tests that failed before their fix: a retirement whose trial another
  process took meanwhile gives the next trial `ask` takes `record_trial`'s note, and that one can be
  retired (`test_a_retirement_that_finds_its_trial_gone_takes_the_next_with_record_trial_s_note`); in
  the race, both refusals reach the error raised
  (`test_a_keyed_trial_queued_after_the_check_names_itself_when_nothing_can_be_written`); a `retired`
  list in another form is refused by `record_trial` and `retire_queued` alike
  (`test_a_retired_list_not_in_its_form_is_refused_by_record_trial_and_retire_queued`); a taken record left `RUNNING` is
  retired and told `FAIL` (`test_a_taken_record_left_running_is_retired_and_told_fail`); a queued
  record that another check stops on still names the remedy
  (`test_a_queued_record_with_another_fault_still_names_retire_queued`). From its second review, tests
  that failed before their fix: a trial retired between `record_trial`'s note and its `tell` lets
  `record_trial` go on
  (`test_a_trial_retired_between_record_trial_s_note_and_its_tell_lets_record_trial_go_on`); a trial
  `retire_queued` takes and cannot read back is still told `FAIL` with its note
  (`test_a_retired_trial_that_cannot_be_read_back_is_still_told_fail`); no remedy is named while the
  `retired` list is not in its form
  (`test_no_remedy_is_named_while_the_retired_list_is_not_in_its_form`). From the gate round after
  term 7's landing, tests that failed before their fix: a raced keyed trial whose read back is refused
  carries the queue's note, stops the ranking and is retired
  (`tests/test_ranking.py::test_a_raced_queued_record_whose_read_back_is_refused_stops_and_is_retired`),
  with its note refused too the error names it and the limit
  (`test_a_raced_trial_not_read_back_with_its_note_refused_names_itself`), and the same refused read in
  `retire_queued`'s take gives the queue's note
  (`test_a_raced_trial_retire_queued_cannot_read_back_carries_the_queue_note`), and one told by
  another process before its read is left as it is
  (`test_a_trial_told_by_another_process_before_its_read_is_left_as_it_is`); and three that fail
  on the mutation of a guard no test held: either queue note on a record not in the `retired` list
  (`test_a_record_with_either_queue_note_not_in_the_retired_list_stops`), an outcome `record_trial`
  never writes on a `FAIL` record
  (`test_a_failed_record_with_an_outcome_the_writer_never_writes_stops_for_good`), and a keyless
  record holding only returns or only session days
  (`test_a_keyless_record_holding_only_returns_or_session_days_shows_a_run`). From the gate's second
  round after term 7, tests that failed before the one model: a keyless record another writer queued
  and started, or wrote an empty key, a key not text or an attribute of its own on, is not cleared,
  under a mark made before either (`test_a_keyless_record_another_writer_started_is_not_cleared`,
  which replaces `test_an_empty_key_is_keyless_and_mark_not_run_clears_it`, whose reading it
  reverses); a trial number that is a `bool` or a float is refused before anything is written
  (`test_a_trial_number_that_is_not_an_int_is_refused`); a raced queued record of the pass's key is
  no failure the pass goes on from, with a second keyed record waiting or the read back refused
  (`tests/test_variant_study.py::test_a_raced_queued_record_of_the_key_is_not_a_failure_the_pass_goes_on_from`).
  A keyless queued record `record_trial` took is still cleared
  (`test_a_keyless_queued_record_record_trial_took_is_still_cleared`), which passed before too. From
  the code review of the model, tests that failed before their fix: a keyless queued trial taken
  without its note names the stop for good
  (`test_a_keyless_queued_trial_taken_without_its_note_says_it_stops_for_good`); a marked `WAITING`
  record written on after its mark is named as such
  (`test_a_marked_waiting_record_written_on_after_its_mark_is_named_as_such`); a marked record taken
  with the retirement's note still stops
  (`test_a_marked_waiting_record_told_fail_with_the_retirement_s_note_still_stops`); and the pass runs
  a key whose record was taken from the queue
  (`tests/test_variant_study.py::test_a_record_taken_from_the_queue_is_no_run_the_pass_skips_its_key_for`).
  From the gate's fourth round, tests that failed before their fix (`tests/test_ranking.py`): a study
  of two objectives, or minimized, is refused by `open_study` and stops the ranking, with each
  two-valued record named
  (`test_a_study_not_of_the_ledger_s_one_maximized_objective_is_refused_by_name`,
  `test_a_record_told_more_than_one_value_is_another_writers`); a keyless record with the
  retirement's note is another writer's, unmarked, queued or not
  (`test_a_keyless_record_with_the_retirement_s_note_is_another_writers`); a marked `WAITING` record
  taken whose `tell` was refused is finished by its mark
  (`test_a_marked_waiting_record_taken_whose_tell_was_refused_is_finished_by_its_mark`); returns
  whose squares overflow stop, in the three cases found
  (`test_returns_whose_squares_overflow_stop_by_name`); and every record of a key has its returns
  read, in either order (`test_every_record_of_a_key_has_its_returns_checked_whatever_their_order`).
  `tests/test_returns_matrix.py` refuses a `bool`, an `int`, a NaN and overflowing squares at
  `daily_series` itself. From the code review of that fix, tests that failed before their change:
  a minimized study is refused as a two-objective one is (the test above); a rejected return no
  longer hides its key's other stops
  (`test_a_return_refused_still_lets_its_key_s_disagreement_be_named`); and returns whose fourth
  central moment overflows stop by name
  (`test_returns_whose_fourth_central_moment_overflows_stop_by_name`). From the gate's fifth round,
  tests that failed before their fix (`tests/test_ranking.py`): a config whose strategies trade
  other bar types than its data loads, none, or none in the writer's form, stops
  (`test_a_config_whose_strategies_trade_other_bars_than_its_data_loads_stops`), and so does one
  loading a bar type twice (`test_a_config_loading_one_bar_type_twice_stops`).
* The trial count is per study (term 5): nothing in the code stops a new study of the same
  configurations from starting at N = 0, and no record carries an earlier study's keys into it. What
  holds that line is pre-registering every study in a record before its first run, as ADR 0048 does
  for step 7. A study that repeats configurations of an earlier one says so in its pre-registration
  and counts them in its N. Read in these records on 2026-11-10 (the Evidence below); no test can
  assert it.

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
  run of its key (narrowed by term 7: one holding a system attribute stops the ranking instead); a refusal before `ask` would close that, and is not decided here (below). A value told against its returns, and a config holding only the data, passed. Under the
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
  retirement of the foreign record with its key still counted, is undecided (term 7 decides it). Stated limit: any writer
  to the ledger can queue a keyed trial (`Study.enqueue_trial`) and so stop a study for good. The
  ledger is a local file whose writers are this repository's own sessions, so this is stated, not
  defended against.
* Term 7, added 2026-11-10, decides the remedy left open above: a recorded retirement of the foreign
  record, its key still counted, and no chain of studies. Optuna 5.0.0, read in the installed package
  (`v5.0.0`, `01ddd17ab9f3`): `Study.ask` takes the oldest `WAITING` trial before it creates one
  (`study/study.py`, `_pop_waiting_trial_id`, over `get_all_trials` in order of trial ID), and no public
  call takes a chosen one; `Study.tell` tells only a `RUNNING` trial (`study/_tell.py`); every write
  of a trial attribute checks `check_trial_is_updatable` (`storages/_rdb/storage.py`,
  `storages/_base.py`), so `Trial.set_user_attr` after `tell` raises `UpdateFinishedTrialError` (run).
  So a record is taken through `ask` in queue order, and the retirement of a finished one is the study
  user attribute (`Study.set_user_attr`), with no private storage call. `record_trial` now refuses
  before `ask`, which closes the known limit above on its path: it no longer takes a keyed queued
  record, so no note of its is needed to keep one from reading as a run, and `retire_queued` writes its
  list before it takes one. What is left is the race term 7 names. Reproduced as failing tests on the
  code before this change (Confirmation).
* Review of term 7, 2026-11-10. Code review (`/code-review`, high): ten findings. Fixed, each shown by
  a test that failed first: a retirement's own race took a newer queued trial with a note
  `retire_queued` refused; a refused `tell` replaced the error naming a raced trial; a remedy named for
  a taken record left `RUNNING`, which `retire_queued` refused; a `retired` list in another form read
  three ways; and three cleanups (one take helper for `record_trial` and `retire_queued`, one lookup, one
  helper in the tests). Corrected in this record: a retired key nothing runs stops for good, and a raced
  record holding `fixed_params` stops rather than reading as a run. Not changed, stated in term 7: a
  retired record is read in no state, since checking its state could not tell a retirement cut short
  from `record_trial`'s own failure, and its key is counted either way. Second reader
  (`evidence-verifier`): the Optuna claims hold against `v5.0.0` (the installed files are upstream's,
  run), and so do the records cited; it corrected the race's wording, a stop that did not name the
  remedy when another check fired first, the comparison with `not_run_waiting`, and a test that did not
  measure N before a retirement. A second code review (`/code-review`, high) of those fixes: nine
  findings. Fixed, each shown by a test that failed first: a `RUNNING` record retired while a live
  `record_trial` held it made that `record_trial`'s `tell` raise (now `Study.tell(...,
  skip_if_finished=True)`, Optuna 5.0.0 `study/_tell.py`, and `retire_queued` asks that a `RUNNING`
  record's process be gone, as `mark_not_run` does); a trial `retire_queued` took and could not read
  back stayed `RUNNING`; a remedy was named while the `retired` list was not in its form. Fixed with no
  test: a refused note in a retirement's race now names the trial, a docstring, the pass's refusal of
  such a list, a test fixture's import and a test's pattern. Not changed: two extra reads of the study
  per retirement, which hold the lock a little longer and change nothing read.
* Gate round after term 7's landing, 2026-11-11 (ADR 0073), two outside readers of the gate as
  checked out: one high finding, of the family term 7 names (the race), and three test-only findings.
  The first reader (Codex) found that a refused read of the trial `ask` gave told it `FAIL` with a
  generic error, so a keyed trial another writer queued after the check, with no system attribute,
  lost the only sign it was taken from the queue, and the ranking passed on its returns at N = 3,
  DSR 0.9885, with no backtest run (run in a temporary ledger with an injected refusal). A trial not
  read back now carries the queue's note, through Optuna's public `Trial.set_user_attr` and
  `Study.tell` only, as term 7's other takes do; `retire_queued`'s own take, which noted such a trial
  by the queue it read before `ask`, does the same. The second reader (Muse) found no behaviour fault
  and three guards no test failed on, each shown by a mutation that passed at DSR 0.9885; each now
  has a test that fails on its mutation (run). Code review of the fix (`/code-review`, high): eight
  findings. Fixed, each shown by a test that failed first: a trial another process told before its
  read, which only `mark_not_run` does, was given term 7's limit note (now left as it is, Optuna
  5.0.0's `UpdateFinishedTrialError`, `storages/_base.py`, read); an interrupt while the note was
  written was raised with no note naming the trial. Fixed with no test: this record's wording for the
  unread case, two docstrings, and a test that faked the race by its check rather than at `ask`.
  Not changed: one helper for both notes on a refused note, and one test helper each file keeps.
  Confirmation lists the tests. No change of decision.
* Gate's second round after term 7, 2026-11-11 (ADR 0073), two outside readers of the gate as
  checked out at #172. The first (Codex) found nothing that changes behaviour. The second
  (Antigravity) reproduced three findings, each now a test that failed first (Confirmation). High: a
  keyless record another writer queued (`Study.enqueue_trial`'s `fixed_params`) or wrote attributes
  on, then started and told `FAIL` before any mark, was cleared by `mark_not_run` as `record_trial`'s
  own record lost before its key, and the gate passed at N = 3, DSR 0.9974. High: `mark_not_run` and
  `retire_queued` took `False`, `True` or `0.0` as a trial number (`0 == False == 0.0`) and wrote it
  into lists every reader refuses, which un-marked or un-retired the records before it and stopped
  the study's ranking and recording for good. Medium: the study's pass read a raced queued record of
  its key, taken with the queue's note, as `record_trial`'s written failure, and went on from an
  error that left none. The first is the queued record's family, found in a second round, so it is
  redesigned as one model (term 7) rather than patched; the third is read through that model too.
  Optuna 5.0.0's `FrozenTrial` keeps `user_attrs`, `system_attrs`, `distributions` and
  `intermediate_values`, and `Study.enqueue_trial` writes `fixed_params` (run, in the installed
  package). No change of decision.
  Code review of the model (`/code-review`, high): nine findings. Fixed, each shown by a test that
  failed first: a keyless queued trial taken without its note stopped the study for good with no
  word of it in the error; a marked `WAITING` record written on was said to have been started; the
  rule had widened a marked record's clearing to the retirement's note; the pass skipped a key whose
  record was taken from the queue, where the gate stops on it. Fixed with no test: a check that
  could never fail, the queue notes held in two places, and `origin`'s word on the one reading it
  leaves to `ask`. A NumPy integer stays refused as a trial number, its error naming `int()`. Not
  changed: `origin` read twice for one record's remedy, which changes nothing read.
* Gate's fourth round, 2026-11-11 (ADR 0073), two outside readers of the gate as checked out at
  #177; five findings, each reproduced here by a test that failed first (Confirmation). The first
  reader (Codex): a study of two objectives, opened with the ledger's sample, ranked records told
  `[Sharpe ratio, 123.0]` at N = 3, DSR 0.988485, as one objective does; `origin` now reads a record
  told more than one value as another writer's, and the ranking stops on the study by its directions
  (Optuna 5.0.0 `Study.directions`, run). The second reader (Antigravity): `origin` read a keyless
  `FAIL` or `RUNNING` record with `retire_queued`'s note as taken here, so `mark_not_run` cleared
  another writer's started record and the gate passed with N short (fixed inside `origin`:
  `carries_a_queue_note` accepts on a keyless record only `record_trial`'s note); finite returns
  whose squares pass float64's largest (about 1.34e154 each) gave skfolio a Sharpe ratio of 0.0,
  which passed a rejected study, inverted the pick, or raised a bare `ValueError` from N-hat (run at
  skfolio 1.12.9); a marked `WAITING` record taken here whose `tell` was refused stopped for good,
  and `mark_not_run` refused it (the mark now tells it `FAIL`); and with several records of one key,
  only the first was read by `daily_series`, the rest compared by Python's `==`, under which `False`
  equals 0.0, so the gate passed or stopped by trial order. The last two of those are one input
  check where returns enter the gate (`daily_series`): every return a float, finite, the squares'
  sum finite, on every record that ran. Two of the five are of the origin family, three of input
  validation. Code review of the fix (`/code-review`, high): nine findings. Fixed, each shown by a
  test that failed first: only the count of objectives was read, so a minimized study passed and
  `open_study` opened either (now one `MAXIMIZE` objective, read by `open_study` and the ranking);
  a rejected return hid its key's other stops; and with squares finite, the fourth central moment of
  returns of about 1e77 or more overflowed, so the gate stopped only on skfolio's DSR of NaN, naming
  no series (skfolio 1.12.9 `measures.kurtosis` gives NaN for a quotient not finite, read; scaled by
  1e80 to 1e150, run). The input check now also takes skfolio's own `measures.variance` and
  `measures.fourth_central_moment`, so it rounds as the gate does, where a sum of squares in
  Python could round otherwise at float64's edge. Fixed with no test: `mark_not_run`'s `tell` of a
  take left `RUNNING` skips a trial already finished, as `retire_queued`'s does; a stop that named
  `record_trial` as the taker, where `retire_queued` takes with the same note; and a header's
  threshold. Not changed: each record's returns parsed again by the told value's check, and `origin`
  read up to three times for one marked record, neither of which changes what is read. No change of
  decision.
* Gate's fifth round, 2026-11-11 (ADR 0073), two outside readers of the gate as checked out at #179;
  three findings that change behaviour, each reproduced here by a test that failed first
  (Confirmation here and in ADR 0041). The first reader (Codex), medium: another reader's
  `move_sample` between the study pass's plan check and its recording put every record on a range
  nobody planned, and the pass returned a ranking that passed at N = 2, T = 35, DSR 0.9868 (run in
  a temporary ledger, with the move injected at the recording's entry). The pass now hands the
  sample it checked to every `record_trial`, which refuses a trial once the study has moved, and it
  refuses a report on any other sample than that one, by name (ADR 0041 term 5, ADR 0048). The
  second reader (Muse): a sample holding no bar, a weekend inside the catalog's window, skipped the
  end check and was recorded as an idle trial counted in N, where ADR 0041 term 1 refuses it before
  a trial; and a record whose strategies trade one instrument while its data and digest name
  another, rehashed to its key, passed at DSR 0.98849. `trial_config` cannot write the last, since
  it writes the data's bar types from the strategies'; it is closed by the shape check in term 6,
  one comparison, rather than stated as a limit. One finding is a sample race, one an input
  refusal, one reachable only by a handcrafted record; no new family. Not checked, and not
  changed: the inner form of a stored config's venue, which no reader showed lowering N, since the
  key binds the config it is stored with. No change of decision.
  Code review of the fix (`/code-review`, high): seven findings. Fixed, shown by a test that failed
  first: a move after the last trial left the moved sample with no run, and its stop was returned
  as the planned study's own; the pass now reads the sample again after a stop and refuses one that
  moved. Fixed with no test: the report carries the `Sample` it ranked, so the pass compares that
  sample rather than its text; the rule deriving a trial's data bar types from its strategies' is one
  function (`data_bar_types`), shared by `trial_config` and the shape check; a comment citing term 7
  for term 6; a docstring's wrapping. Not changed: holding off `move_sample` while a pass runs, since
  a record made during a move carries the sample number it was bounded by, the next trial is refused
  and the ranking checks the sample it read, so no record or report of the pass answers for an
  unplanned range; and refusing a multi-instrument trial for one instrument with no bar in its
  sample, which ADR 0053 records as such (its Confirmation: a bar type with no bar in the sample is
  in the digest), outside this round's findings.
