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
ADR 0035's numbers, 4 to 6; terms 1 to 3 (storage, recording, entry) are ADR 0035's. Term 7, added here
on 2026-11-10, is ADR 0081's since 2026-11-11.

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
6. **What stops the ranking**, beside ADR 0031's, ADR 0041's, ADR 0053's, ADR 0080's and ADR 0081's: a study with no record, or
   of other directions than one objective maximized (`record_trial` tells one value, the Sharpe
   ratio, and `open_study` refuses such a study); a key with no
   record that ran to the end; a `failed` or `RUNNING` record no later record of its key resolves;
   records of one key that disagree (term 5); an idle key (ADR 0010's Limits); and returns ADR 0010
   step 2 rejects, or a record not in the shape `record_trial` writes. Every record that ran on the
   sample has its returns read before any two are compared: each a float, finite, and the series of
   its session days accepted by the gate's one numeric check,
   `canon_trading.returns_matrix.session_returns` (corrected 2026-11-11, the seventh round: ADR
   0080 holds the check and replaces the ones this sentence listed until
   then, the sum of squares finite, skfolio's variance and fourth central moment finite, and both
   moments at or above float64's smallest normal for a series that is not constant). Its session days are each
   once, in order, as `record_trial` writes them, and include the session day of the sample's end,
   which `record_trial` refuses a sample without (ADR 0041 term 1); none falls on the account's
   registration day before the start (2026-11-11). A stored config whose data does
   not load exactly the bar types its strategies trade, each once, in order, as `trial_config` writes
   them, is not in that shape either, on every sample (2026-11-11). Each message names the key and
   trial; one ranking gathers every trial's stops before it raises.
   A configuration that can never run to the end stops the ranking for as long as that study is used.
   That is the strict reading: a tried configuration is a trial, and no source read gives it a value.
   Like the idle trial, it is undecided, not solved.
7. **Writing the ledger.** ADR 0081 term 7, moved there on 2026-11-11 with no change of decision:
   retiring another writer's queued record with its key still counted in N, the one model of who
   wrote a record (`canon_trading.ledger.origin`), one writer at a time under the study's write
   lock, the build as the check, and the hand-written class, each with its named limits.

Who decided. The orchestrator chose option 3 and the terms under the owner's delegation of 2026-11-10
(ADR 0010's Evidence). Who decided term 7 is in ADR 0081.

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
* Term 7's tests, those of its reviews and of the seventh round's two models: ADR 0081's Confirmation.
* From the gate's fourth round, tests that failed before their fix (`tests/test_ranking.py`): a study
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
  loading a bar type twice (`test_a_config_loading_one_bar_type_twice_stops`). From the gate's
  sixth round, tests that failed before their fix: a pick whose returns, rescaled by 1e-81, have a
  fourth central moment that rounds to 0.0 stops, where it went from DSR 0.9464, rejected, to
  0.9570, passed (`tests/test_ranking.py::test_a_pick_whose_moments_underflow_stops`,
  `tests/test_returns_matrix.py::test_returns_whose_moments_underflow_are_refused`); and session
  days reversed, or holding a day twice, are refused
  (`test_session_days_out_of_order_stop`,
  `test_session_days_out_of_record_trial_s_order_are_refused`).
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
  run of its key (narrowed by ADR 0081: one holding a system attribute stops the ranking instead); a refusal before `ask` would close that, and is not decided here (below). A value told against its returns, and a config holding only the data, passed. Under the
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
  retirement of the foreign record with its key still counted, is undecided (ADR 0081 decides it). Stated limit: any writer
  to the ledger can queue a keyed trial (`Study.enqueue_trial`) and so stop a study for good. The
  ledger is a local file whose writers are this repository's own sessions, so this is stated, not
  defended against.
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
* The gate's sixth outside round, 2026-11-11 (Antigravity, one reader; Codex read the same commit and
  found nothing): seven findings, each reproduced. Five are reachable from the writers this
  repository names or from an input that passed the gate's own input check, and are fixed, each
  shown by a test that failed first: returns whose moments underflow (term 6); records holding only
  a prefix of the sample, without its last session day, and records with a return on the account's
  registration day before a midnight start (ADR 0041 term 1); session days out of order or held
  twice (term 6); and a study pass on a moved sample with nothing left to record, or whose failed
  run's sample moved beneath it, which returned instead of stopping (ADR 0041 term 5). Two are
  reachable only by a hand-written record that no named writer produces, and are stated as ADR 0081's
  named limits rather than defended against: a cut-short record holding a sample no number of the
  study's, or results written before its sample and config; and a `FAIL`, `traded` record with no
  error and a finite Sharpe ratio. Counted under ADR 0073 rule 5: seven findings, all changing
  behaviour; no new family. No change of decision.
  Code review of the fix (`/code-review`, high): nine findings. Fixed, shown by a test that failed
  first: the pass's move check, which ran only after a run that raised and gave that run's error,
  now runs after every attempt and names both samples, so a last run recorded while the sample moved
  stops the pass too. Fixed with no test: the second central moment is skfolio's own
  (`measures.variance(biased=True)`) and decides alone, since the fourth is never below its square;
  one date helper in `returns_matrix`; a comment that named the wrong case. Stated in ADR 0041's
  Confirmation: a record made before the end-day refusal may lack the sample's last session day,
  and now stops. Not changed: the ranking-level test of trials with no day in common, which no
  record in the writer's shape can now reach (every one holds the last day); `returns_matrix`'s
  own tests keep that case.
* Term 7 with its `Who decided` sentences, consequences, tests and sources (its addition and reviews of
  2026-11-10, the two gate rounds after it, and the seventh round with the sources of its two models)
  moved to ADR 0081 on 2026-11-11, with no change of decision. The lines above keep what concerns terms
  4 to 6; the fourth round, which found members of term 7's origin family too, is kept whole here.
  Second reader of the split (`evidence-verifier`) and code review (`/code-review`, high), the same
  day: nothing of this record is missing from the two; one pointer here, repointed to ADR 0081 where
  its target is the entry below it, was put back.
