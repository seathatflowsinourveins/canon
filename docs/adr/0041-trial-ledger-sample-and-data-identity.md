---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Trial ledger sample: one fixed range per study, the data each trial read, and moving it

## Context and Problem Statement

ADR 0010 picks the variant with the highest Sharpe ratio "over the whole sample", then step 2 trims every
variant to a common range of T rows. ADR 0035 records each trial with no range of its own: a run read
whatever the catalog held. The full review of 2026-11-10 found that one short trial set T for all and
flipped a rejection to a pass, and that bars added to the catalog left new trials on another range than
old ones, with no way to clear the stop that followed. What is a study's sample, what identifies the
data a trial read, and what happens when the data grows?

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
   check its own historical requests use. The run must read the bars that check read: the same catalog
   object gives them (`ParquetDataCatalog.query`), and since the node reads the catalog again from its
   path, a run whose bars (term 3) have another digest is told `FAIL`. A catalog covers the window a
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
3. **Data identity.** A trial's data is its sample and `data_digest`: for each bar type the run read,
   the SHA-256 hex digest of the UTF-8 bytes of one line per bar,
   `bar_type|open|high|low|close|volume|ts_event|ts_init`, each field as NautilusTrader prints it, the
   bars sorted by `ts_init` and `ts_event`, then by their text, the lines joined by `\n`. The bars, and the session days,
   are those the run read, as the engine's cache kept them (`Cache.bars`), not a later read of the
   catalog. The cache's `CacheConfig.bar_capacity` is raised to the count of the sample's bars in the
   catalog, read after every other check. A run whose cache holds another number of bars than it
   replayed (`BacktestResult.iterations`, one per data item at the pinned commit), or a bar outside
   the sample, is told `FAIL`.
   Nothing upstream identifies data,
   so this is local glue like the config key. It is taken from the bars, not the catalog's file layout, so
   a writer that adds, rewrites or consolidates files moves no digest unless a bar of the range changes.
   Records of one key on one sample that differ in range, returns, session days or digest disagree and
   stop the ranking (ADR 0035 term 5), and so do any two records on one sample that read one bar type
   as different bars, whatever their keys.
4. **The key is the configuration.** This term supersedes what ADR 0035 term 4 said `config` holds,
   which was every property upstream reflects and the run's range. `config` now holds what this repo
   sets: each strategy's class (`module:Class`) and its config's own fields, the venue's name,
   settings, fee rates and starting balances, and the bar types; not the sample. The strategies are
   built from the configs after the key is taken. Floats are written by `repr`, enums by name, decimals
   normalised, balances as `Money` prints them, other NautilusTrader types by `str()`; a value with no
   stable text form is refused. N is the number of distinct keys over every record, on every sample.
5. **Moving the sample.** `move_sample(study, start, end)` appends a sample numbered after the last; the
   same range again is a new sample, the way a corrected catalog is taken in. A move reads and writes the
   samples under ADR 0035 term 1's lock, so two moves at once are both kept. No record is overwritten or
   deleted. The ranking reads only records on the current sample, and stops, naming it, on a key with no
   record there: every configuration the study tried is run again before a ranking. A record with a key
   and no sample number of the study's is read as on the current sample, so its failure stops the
   ranking until a later record of its key resolves it. One that ran to the end with no such number
   came from another writer and stops the ranking, named.
6. **A record with no key ran no backtest.** `record_trial` writes the key before the run starts, so a
   record without one is what a process killed between `ask` and that write leaves. The ranking stops,
   naming it, until `mark_not_run(study, number)` clears it: a `RUNNING` one is told `FAIL`, its number
   joins the study user attribute `not_run`, and it is kept and not counted in N. A record with a key
   is refused, since only a later record of its key resolves it; so is a keyless record that shows a
   run (an outcome, returns or session days, or a result Optuna keeps: `COMPLETE` or `PRUNED`, a value
   or an intermediate value), a trial of an unknown configuration, which stops for good.

Local, no upstream. The ranking's own glue, where the upstreams read say nothing:

* `not_run` (term 6): Optuna 5.0.0 has states, not a "ran nothing" mark, and its `fail_stale_trials`
  only tells a stale trial `FAIL`; the DSR paper counts trials and says nothing of a record with none.
* A tie at the top: keys within `math.isclose` of the best Sharpe ratio with equal returns day by day
  are one pick; tied keys with other returns stop. The paper's pick is "the" highest; skfolio takes
  one series and says nothing of ties.
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
* The cache's bar capacity is sized from a count of the catalog's bars before the run (term 3);
  upstream's default keeps 10,000 bars of a type.

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
* Known limit: the engine's cache holds every bar of the sample for the length of a run, so memory
  grows with the bar count. That is small for daily bars (a study of about 1,400 daily bars per
  instrument), but a sample of minute bars over years needs its memory checked before it is recorded.
* Bad: a catalog that changes inside the sample between the coverage check and the replay leaves a
  failed record of that configuration, which a later record of it resolves.
* Bad: the digest is local glue, and a NautilusTrader pin that prints a field differently moves it; the
  records then disagree and the sample must be moved.

### Confirmation

* `tests/test_ledger.py` records real backtests: the run is bounded by the sample with both ends
  replayed; bars appended after the sample change no trial on it; a moved sample stops the ranking on
  each configuration not run on it and ranks T = 70 once all are; a corrected catalog disagrees until
  the sample moves.
* `tests/test_ranking.py` and `tests/test_returns_matrix.py`: a trial over a shorter range, and trials
  with no day in common, stop the ranking naming each; N counts keys across samples; a keyless record
  stops until marked, then N leaves it out, and marking a record with a key is refused.
* The second review's cases, each a test that failed before its fix: the catalog replaced between the
  replay and the digest (`test_the_digest_is_of_the_bars_the_run_read_not_a_later_catalog`); a
  sample the catalog covers in part, at either end
  (`test_a_sample_the_catalog_covers_in_part_is_refused_naming_the_gap`); an error on the sample write
  (`test_a_keyed_failure_before_its_sample_is_written_stops_the_ranking`); a creation interrupted
  before its sample (`test_a_creation_interrupted_before_its_sample_is_completed_by_the_next`); and
  session days with no return (`test_a_session_day_with_no_return_is_refused`,
  `test_session_days_with_no_return_stop_and_are_named`). With them: a cache short of a bar, or
  holding one outside the sample, is told `FAIL`; a hole closed by `consolidate_data` records the
  same digest as a catalog loaded whole; a record that ran with no sample number of the study stops.
* The third review's cases, each a test that failed before its fix: a 40-bar fragment swapped in between
  the check and the replay of a 70-bar sample
  (`test_a_catalog_swapped_between_the_check_and_the_replay_is_told_failed`), or between the check
  and its query (`test_a_catalog_swapped_between_the_check_and_its_query_is_refused`); records out of the
  writer's shape (`test_a_record_that_ran_out_of_the_writers_shape_stops_naming_it`); a keyless record
  told a value (`test_a_keyless_record_told_a_value_cannot_be_marked_and_stops`); and two moves at once
  (`test_two_sample_moves_at_once_are_both_kept`).

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
  study: the digest was a read of the catalog after the replay, so a catalog replaced in between gave
  the old run the new bars' digest and mixed inputs passed (DSR 0.95144); any overlap of the catalog
  with the sample was accepted, and two configurations passed on a 40-day fragment of a year's sample
  (DSR 0.98118); a keyed failure with no sample number was left out and the ranking passed; a
  creation interrupted before its sample could be neither opened nor created again; session days with
  no return were dropped, and three 31-day calendars passed at T = 30. Terms 1, 2, 3 and 5 corrected,
  with no change of decision. Sources: nautilus-trader 2.0.0rc8 in `.venv`,
  `persistence/__init__.pyi` (`get_missing_intervals_for_request`, `write_bars(start, end)`) and
  `common/__init__.pyi` (`CacheConfig.bar_capacity`, `Cache.bars`), `bundled`; a fresh catalog reported
  nothing missing over two adjacent recorded windows, and an object asked again after its own write
  answered from before it; a run of 40 bars kept 10 at a capacity of 10 and 40 at 100, with the same
  orders. Run. Optuna 5.0.0 `study/study.py`, `create_study`: the study and its direction are one
  `create_new_study` write, and `load_if_exists` returns the study holding a taken name. `bundled`.
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/engine.rs
  — `self.iteration += 1` once per data item in `run_impl`'s loop, copied to `BacktestResult.iterations`
  by `get_result`. Fetched. `ParquetDataCatalog.consolidate_data` closed a stretch between two files
  with no bar (run).
* A third review of the gate, 2026-11-10, read only, each finding reproduced here as a failing test:
  the coverage check read one catalog and the node another, so a 40-bar fragment swapped in between
  ranked a 70-bar sample at T = 40 (DSR 0.99853); records with prior-year returns, no run bounds or an
  empty digest passed (DSR 0.98849); a keyless record told a value could be marked not run, leaving N
  short; two moves at once both returned sample 1. Terms 1, 5 and 6 corrected, with no change of
  decision. Sources: nautilus-trader 2.0.0rc8 in `.venv`, `ParquetDataCatalog.query`, `bundled`; a run's
  `Cache.bars` and the check's query over one catalog gave one digest on every ledger test (run).
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/engine.rs
  — `backtest_start` is the start given (`run_impl`), `backtest_end` the engine's clock at the end
  (`end_impl`), at or before the end given. Fetched. Optuna 5.0.0 `storages/_rdb/storage.py`,
  `set_study_user_attr` replaces the attribute whole, and `trial/_state.py`, `COMPLETE` and `PRUNED`.
  `bundled`.
* Code review (`/code-review`, high), 2026-11-10: a catalog swapped between the coverage check and
  its query gave the query and the node one fragment, so the digests agreed. The check's intervals are
  read again after the query and a change is refused; a catalog swapped and restored around each read
  in turn is left, since no upstream call reads a catalog's intervals and bars as one. No change of
  decision.
