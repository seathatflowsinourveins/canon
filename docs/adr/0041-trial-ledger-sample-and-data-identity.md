---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Trial ledger sample: one fixed range per study, every trial over all of it, and moving it

## Context and Problem Statement

ADR 0010 picks the variant with the highest Sharpe ratio "over the whole sample", then step 2 trims every
variant to a common range of T rows. ADR 0035 records each trial with no range of its own: a run read
whatever the catalog held. The full review of 2026-11-10 found that one short trial set T for all and
flipped a rejection to a pass, and that bars added to the catalog left new trials on another range than
old ones, with no way to clear the stop that followed. What is a study's sample, and what happens when
the data grows? What identifies the data, what the key holds and
what a record with no key is are ADR 0053, split from this record on 2026-11-10.

## Decision Drivers

* A comparison method that flatters a strategy is worse than none (ADR 0010).
* The counter of trials cannot be turned back (DSR paper, p. 10, fn. 1): no clear may lower N.
* ADR 0001: glue, not invention; what upstream offers is used as documented.

## Considered Options

1. Keep the trim; name the trial that bounds T, and stop below some T.
2. A study fixes its sample; every trial covers it; the sample is moved only by an explicit call.
3. A new study for each sample, naming the one before it, with N counted over the chain.
4. The range in the config key: a re-run on new data is a new configuration.

## Decision Outcome

Chosen: **option 2**. Option 1 needs a threshold no source gives. Option 3 is option 2 spread over
several studies. Option 4 counts one configuration once per data step, so N grows with the data and not
with what was tried.

1. **Sample.** A study is created with one: a range [start, end] in UTC nanoseconds, both ends included,
   and a sequence number from 0, kept as the study user attribute `samples`, oldest first
   (`canon_trading.ledger.create_ledger_study`). A date with no time is its midnight. A sample the
   catalog does not cover in full is refused before a trial is asked for, naming each missing part:
   what NautilusTrader's `ParquetDataCatalog.get_missing_intervals_for_request` reports missing, the
   check its own historical requests use. So is a sample whose ends NautilusTrader's daily series
   cannot book: a bar on the day before the start's, the day it books the account's registration on
   as the base of the first return, with no return of its own (`snapshot_day_start`), as a bar at
   exactly a 00:00 UTC start is; or no bar on the session day of the end, where it books the run's
   last fills (a strategy's `on_stop`). The run must read the bars that check read: the same catalog
   object gives them (`ParquetDataCatalog.query`), and since the node reads the catalog again from its
   path, a run whose bars (ADR 0053 term 1) have another digest is told `FAIL`. A catalog covers the window a
   load asked for (`canon_trading.bars_to_catalog`), or, for bars written without one, their first to
   last `ts_init`.
   A stretch no load can record, such as a weekend between two loads, is closed by upstream's
   `ParquetDataCatalog.consolidate_data`, which merges the files around it. It marks any hole
   covered, so it is used only on a stretch a load showed to have no bar. `record_trial` runs every trial with `BacktestRunConfig(start, end)` set to the current sample, and
   records its number as `sample`. A creation interrupted between the study's name and its sample is
   completed by the next `create_ledger_study` of that name, through Optuna's `load_if_exists`; a
   study that has a sample or a trial is a duplicate.
2. **The whole sample.** Every trial the ranking reads must have the same session days over the sample.
   A trial that lacks one stops the ranking, named with its number, how many days it lacks and the days
   it covers; nothing is trimmed. This supersedes the clause of ADR 0010 step 2 that trims every variant
   to a common range. A session day with no return in a trial's own series stops it too, and is never
   dropped from the calendar. Different instruments may share a study when their session days agree.
3. **Data identity.** ADR 0053 term 1: the sample and a digest of the bars the run read.
4. **The key is the configuration.** ADR 0053 term 2, which supersedes what ADR 0035 term 4 (now ADR 0054 term 4) said
   `config` holds.
5. **Moving the sample.** `move_sample(study, start, end)` appends a sample numbered after the last; the
   same range again is a new sample, the way a corrected catalog is taken in. A move reads and writes the
   samples under ADR 0035 term 1's lock, so two moves at once are both kept. No record is overwritten or
   deleted. The ranking reads only records on the current sample, and stops, naming it, on a key with no
   record there: every configuration the study tried is run again before a ranking. A record with a key
   and no sample number of the study's is read as on the current sample, so its failure stops the
   ranking until a later record of its key resolves it. One that ran to the end with no such number
   came from another writer and stops the ranking, named.
6. **A record with no key ran no backtest.** ADR 0053 term 3: `mark_not_run` clears it.

Local, no upstream. The ranking's own glue, where the upstreams read say nothing:

* A tie at the top: keys within `math.isclose` of the best Sharpe ratio with equal returns day by day
  are one pick; tied keys with other returns stop. The paper's pick is "the" highest; skfolio takes
  one series and says nothing of ties. Since 2026-11-11 the keys tied are those whose ratios are
  within the rounding of their own computation of the highest, and their returns are compared
  exactly: ADR 0031's addition of that date, term 3, which replaces `math.isclose` here.
* The DSR at N̂ is withheld when its benchmark is below 0. skfolio 1.12.8's
  `expected_max_sharpe_ratio` returns a negative benchmark for 1 < N̂ < about 1.28 without comment;
  the expected maximum of N̂ ≥ 1 trials of mean 0 is not below 0.
* A record that ran on the current sample and is not in `record_trial`'s shape is refused, naming its
  trial: its returns, a start other than the sample's or an end outside it, a `run_config_id` other
  than its key, an empty digest, or a return or session day outside the sample's days. Optuna stores
  user attributes as any JSON and validates none.
* One ranking gathers every trial's stops before it raises, and the study's (N, V, the tie, the DSR)
  after: no upstream ranks a ledger, so the order is this repo's.
* A record with a key and no sample number is read as on the current sample (term 5): Optuna writes
  one user attribute at a time, and an error between the key and the sample leaves such a record.
* A study with no sample, no trial and the ledger's direction is completed, not refused (term 1): the
  mechanism is Optuna's `load_if_exists`, and reading such a study as a leftover of an interrupted
  creation is this repo's, sound because every creator holds the creation lock.

Who decided. The orchestrator chose option 2 and its terms under the owner's delegation of
2026-11-10 (ADR 0010's Evidence).

### Consequences

* Good: T is the session days every trial shares over the sample, and new data cannot reach a study
  silently.
* Good: a stop on data that moved is cleared by moving the sample and re-running, with N kept.
* Bad: after a move, every configuration must run again before any ranking; a configuration whose data
  cannot cover the sample, such as an instrument listed later, stops the study until the sample moves
  to a range it covers.
* Moving the sample takes the ledger's lock, so only a study the ledger opened (`open_study`,
  `create_ledger_study`) can be moved.
* Bad: a sample must lie inside what the catalog recorded. Bars written without a window cover only
  their first to last `ts_init`, and a stretch with no bar between two loads is never recorded;
  `record_trial` refuses the sample, naming it, and consolidating the catalog's files closes it.
  Consolidating over a stretch never loaded would hide a real gap; nothing here can tell the two apart.
* Bad: a catalog that changes inside the sample between the coverage check and the replay leaves a
  failed record of that configuration, which a later record of it resolves.

### Confirmation

* `tests/test_ledger.py` records real backtests: the run is bounded by the sample with both ends
  replayed; bars appended after the sample change no trial on it; a moved sample stops the ranking on
  each configuration not run on it and ranks T = 70 once all are; a corrected catalog disagrees until
  the sample moves.
* `tests/test_ranking.py` and `tests/test_returns_matrix.py`: a trial over a shorter range, and trials
  with no day in common, stop the ranking naming each.
* The second review's cases, each a test that failed before its fix: a
  sample the catalog covers in part, at either end
  (`test_a_sample_the_catalog_covers_in_part_is_refused_naming_the_gap`); an error on the sample write
  (`test_a_keyed_failure_before_its_sample_is_written_stops_the_ranking`); a creation interrupted
  before its sample (`test_a_creation_interrupted_before_its_sample_is_completed_by_the_next`); and
  session days with no return (`test_a_session_day_with_no_return_is_refused`,
  `test_session_days_with_no_return_stop_and_are_named`). With them: a record that ran with no
  sample number of the study stops.
* The third review's cases, each a test that failed before its fix: a 40-bar fragment swapped in between
  the check and the replay of a 70-bar sample
  (`test_a_catalog_swapped_between_the_check_and_the_replay_is_told_failed`), or between the check
  and its query (`test_a_catalog_swapped_between_the_check_and_its_query_is_refused`); records out of the
  writer's shape (`test_a_record_that_ran_out_of_the_writers_shape_stops_naming_it`); and two moves at once
  (`test_two_sample_moves_at_once_are_both_kept`).
* The fourth review's cases, each a test that failed before its fix: returns and session days after the
  run's own end (`test_returns_after_the_run_s_end_stop`); a null, a 31-digit, a 5,000-digit, a negative
  and a past-64-bit session day (`test_a_session_day_that_crashed_the_ranking_stops_naming_it`); series
  named `1` and `"1"` (`test_names_distinct_only_by_type_are_refused`).
* The fourth review's second part: a sample no nanosecond timestamp holds is refused on write
  (`test_a_sample_no_timestamp_holds_is_refused`), and a stored sample not in the writer's form on read
  (`test_a_stored_sample_not_in_the_writers_form_is_refused`), tests that failed before their fix.
* A sixth review of the gate, tests that failed before their fix: a sample starting at 00:00 UTC on a
  bar (`test_a_sample_starting_at_a_bar_at_utc_midnight_is_refused_before_a_trial`) and one ending two
  days after its last bar (`test_a_sample_ending_on_a_day_with_no_bar_is_refused_before_a_trial`) are
  refused before a trial; series named by numbers that cover other days are refused by name
  (`test_series_named_by_numbers_that_cover_other_days_are_refused_by_name`, `tests/test_returns_matrix.py`).
* The gate's fifth outside round, 2026-11-11, tests that failed before their fix: a sample holding no
  bar, a weekend inside the catalog's window, is refused before a trial
  (`test_a_sample_holding_no_bar_is_refused_before_a_trial`), where it was recorded as an idle trial;
  and a sample moved by another reader beneath the study's pass, after its plan check, stops the
  pass before a trial on the moved sample, at its first trial or a later one, and a ranking of the
  moved sample is refused, by name (`tests/test_variant_study.py`:
  `test_a_sample_moved_after_the_plan_check_stops_the_pass_before_a_trial`,
  `test_a_sample_moved_during_the_recording_stops_the_pass_at_the_next_trial`,
  `test_a_ranking_of_a_sample_moved_beneath_the_pass_is_refused`), and from the code review of that
  fix, a stop on the moved sample is refused too
  (`test_a_stop_on_a_sample_moved_beneath_the_pass_is_refused`).
* The gate's sixth outside round, 2026-11-11, tests that failed before their fix
  (`tests/test_ranking.py`, `tests/test_variant_study.py`): records that stop 10 days before the
  sample's end, without its last session day, which every run `record_trial` records holds, stop
  the ranking (`test_a_record_that_lacks_the_sample_s_last_session_day_stops`), where they passed as
  the whole sample; records with a return on the account's registration day, the day before a
  00:00 UTC start, stop it (`test_returns_on_the_registration_day_before_a_midnight_start_stop`);
  and the study's pass refuses a moved sample before reading which keys to run, where it skipped
  every key already run on the moved sample and returned
  (`test_a_pass_on_a_moved_sample_with_nothing_to_record_is_refused`), and after a run that raised
  on a sample moved beneath it (`test_a_failed_run_on_a_sample_moved_beneath_it_stops_the_pass`)
  or one that ran to the end (`test_a_run_recorded_on_a_sample_moved_beneath_it_stops_the_pass`, from
  the code review of that fix). A record made before term 1 refused a sample with no bar on its
  last session day may lack that day; it now stops the ranking, and moving the sample to one that
  ends on its last bar's day and running every configuration again is the remedy, as for any
  record not in the writer's shape.
* The gate's seventh outside round, 2026-11-11, tests that failed before their fix
  (`tests/test_returns_matrix.py`, `tests/test_ranking.py`): the numeric check now reads the session
  days alone, after the closed days are dropped, where the zeros of closed days hid session-day
  moments that underflow or overflow (`test_closed_day_zeros_do_not_hide_moments_that_underflow`,
  `test_closed_day_zeros_do_not_hide_moments_that_overflow`); and returns in the top group are
  compared exactly, where two series that differ within 1e-9 of their largest return were one pick
  and the result followed the order of recording
  (`test_a_tie_between_other_returns_stops_whatever_the_recording_order`). ADR 0031's addition of
  that date holds the terms. The step 7 study (ADR 0052), ranked again on its stored records and on
  a fresh recording: N = 8, T = 1,366, the same pick, DSR 0.9338299302788473, rejected. V differs
  from the 2026-11-10 run in the last bit, by key order, with the DSR and verdict identical:
  0.00011297439028764905 summed in recording order on 2026-11-10, 0.00011297439028764906 in key
  order. From this change on the key-order figures are the ones a later run is held to. Run.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; bundled, in the installed
package; run, executed in this checkout.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — version of 2014-07-31. p. 7: a trial is
  one of "a set of N independent backtests"; p. 8: "T is the sample length", listed among the selected
  strategy's quantities; pp. 3–4: "multiple strategies on the same dataset", "multiple tests on the same
  data set"; p. 9: "the set of trials carried out so far"; p. 10 fn. 1: "the counter of trials cannot be
  turned back". The paper does not state that all trials share one T, and says nothing on different
  periods or a growing sample. Fetched by an `upstream-scout`, text and page images.
* https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf — Bailey, Borwein, López de Prado and Zhu,
  the PBO paper, February 2015 revision, p. 11: the T×N matrix must have "the same number of rows for each
  column, where observations are synchronous for every row across the N trials". Term 2 rests on this.
  Fetched by the same scout.
* Optuna 5.0.0 in `.venv`, `optuna/study/study.py`: `Study.tell` takes a `Trial` or a trial number, so
  a `RUNNING` record left by a dead process can be told `FAIL` from another; `Study.set_user_attr`
  keeps study-level JSON; `create_study` without `load_if_exists` raises `DuplicatedStudyError`. `bundled`.
* nautilus-trader 2.0.0rc8 in `.venv`, `backtest/__init__.pyi`: `BacktestRunConfig(start, end)`;
  `persistence/__init__.pyi`: `ParquetDataCatalog.query(start, end)`, no content hash. `bundled`. A run
  with `start` and `end` on two bars' `ts_init` replayed both (`iterations` 25 of 25). Run.
* The full review of 2026-11-10 of the ledger, the returns matrix and the gate (`/code-review`): one short
  trial set T for every trial, and new bars left a study on two ranges with a stop nothing cleared. The
  choices above answer those findings; the owner's delegation is ADR 0010's Evidence line.
* A second review of the gate, 2026-11-10, read only, each finding reproduced on a temporary catalog or
  study: any overlap of the catalog with the sample was accepted, and two configurations passed on a
  40-day fragment of a year's sample (DSR 0.98118); a keyed failure with no sample number was left out and
  the ranking passed; a creation interrupted before its sample could be neither opened nor created again;
  session days with no return were dropped, and three 31-day calendars passed at T = 30. Terms 1, 2 and 5
  corrected, with no change of decision; its finding on the digest, term 3 then, and that finding's
  sources are in ADR 0053's Evidence. Sources: nautilus-trader 2.0.0rc8 in `.venv`,
  `persistence/__init__.pyi` (`get_missing_intervals_for_request`, `write_bars(start, end)`), `bundled`; a
  fresh catalog reported nothing missing over two adjacent recorded windows, and an object asked again
  after its own write answered from before it. Run. Optuna 5.0.0 `study/study.py`, `create_study`: the
  study and its direction are one `create_new_study` write, and `load_if_exists` returns the study holding
  a taken name. `bundled`. `ParquetDataCatalog.consolidate_data` closed a stretch between two files with
  no bar (run).
* A third review of the gate, 2026-11-10, read only, each finding reproduced here as a failing test:
  the coverage check read one catalog and the node another, so a 40-bar fragment swapped in between
  ranked a 70-bar sample at T = 40 (DSR 0.99853); records with prior-year returns, no run bounds or an
  empty digest passed (DSR 0.98849); two moves at once both returned sample 1. Terms 1 and 5
  corrected, with no change of decision; its finding on a keyless record, term 6 then, is in ADR
  0053's Evidence. Sources: nautilus-trader 2.0.0rc8 in `.venv`, `ParquetDataCatalog.query`, `bundled`; a run's
  `Cache.bars` and the check's query over one catalog gave one digest on every ledger test (run).
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/engine.rs
  — `backtest_start` is the start given (`run_impl`), `backtest_end` the engine's clock at the end
  (`end_impl`), at or before the end given. Fetched. Optuna 5.0.0 `storages/_rdb/storage.py`,
  `set_study_user_attr` replaces the attribute whole. `bundled`.
* Code review (`/code-review`, high), 2026-11-10: a catalog swapped between the coverage check and
  its query gave the query and the node one fragment, so the digests agreed. The check's intervals are
  read again after the query and a change is refused; a catalog swapped and restored around each read
  in turn is left, since no upstream call reads a catalog's intervals and bars as one. No change of
  decision.
* Terms 3, 4 and 6, the data identity, the key and the record with no key, with their glue,
  consequences, tests and evidence, moved to ADR 0053 on 2026-11-10, with no change of decision.
* A fourth review of the gate, 2026-11-10, read only, by a second reader, each finding reproduced here
  as a failing test: records whose `backtest_end` was the sample's start carried 29 more days of returns
  and passed, since returns and session days were checked against the sample and not the run; a null or
  a 31-digit session day raised `TypeError` or `OverflowError` instead of naming the trial, and the code
  review added one of 5,000 digits (`ValueError`). A nanosecond text is now a decimal of at most 2**63 -
  1, not negative: NautilusTrader's `UnixNanos` wraps a `u64` (`crates/core/src/nanos.rs` at the
  pinned commit, fetched) and pandas reads -2**63 as `NaT` (run). Series named `1` and `"1"` passed as
  distinct, though skfolio names a portfolio's column by `str` (`bundled`). By reading only: `rank` read
  the current sample and the sample numbers in two reads, now one. Kept as a known limit: a ranking
  answers for the sample current when it read the study, and a move after that read is first seen by the
  next one. No change of decision.
* A fourth review of the gate, second part, 2026-11-10, read only, by a second reader: a sample ending
  at 10**20 ns was written and every later read crashed in pandas, a date before 1677-09-21 or after
  2262-04-11 raised a bare `OverflowError`, and `samples()` took a stored sample with a `bool` field or
  a start after its end. Term 1's range is now checked on write against what NautilusTrader's
  `UnixNanos` (a `u64`) and pandas (at most 2**63 - 1) both hold, and on read for that and for the form
  `create_ledger_study` writes. Reproduced as failing tests. No change of decision.
* A sixth review of the gate, 2026-11-10, by a third reader, each finding reproduced here as a
  failing test: a sample starting at 00:00 UTC on a bar's `ts_init` had `record_trial` tell its own run
  `FAIL` (returns from 2025-03-11, session days from 2025-03-10), and so did one ending two days after
  its last bar, on bars too thin for the closing order (a return of -9.0e-9 on 2025-05-04, a day with no
  bar). Term 1 corrected: both are refused before a trial, as a sample the catalog does not cover is,
  rather than the session days redefined, so no record reads differently. Source:
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/analysis/src/analyzer.rs
  — `snapshot_day_start` books an account's first snapshot on the day before the one holding it, and
  `calculate_daily_returns` gives the first day of the series no return. Fetched. The step 7 study's
  window passes both: its start, 16:00 UTC, is no midnight, and its last bar's session day is its end's,
  2026-01-01 (run). Refusing an end day with no bar also refuses a study whose run books nothing there,
  as one test's did (its sample now ends on its last bar's day); kept, since the refusal is the same
  for every run and says how to end the sample, where the run's failure depends on a fill. `returns_matrix`
  looked up a short series by its name where skfolio's columns hold `str` of it, and raised `KeyError`
  for series named `1` and `2` (`bundled`, run). No change of decision.
