---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# A trial's identity in the ledger: the digest of the bars it read, a key of what this repo sets, and a record with no key

## Context and Problem Statement

ADR 0035 records each trial with a key of every property upstream reflects and the run's range, and with
nothing of the data it read. ADR 0041 fixes a study's sample, the range every trial runs over. What
identifies the data a trial read, what does the key hold, and what is a record that holds no key? Split
from ADR 0041 on 2026-11-10, with no change of decision.

## Decision Drivers

* A comparison method that flatters a strategy is worse than none (ADR 0010).
* The counter of trials cannot be turned back (DSR paper, p. 10, fn. 1): no clear may lower N.
* ADR 0001: glue, not invention; what upstream offers is used as documented.

## Considered Options

1. The range in the config key: a re-run on new data is a new configuration (ADR 0041's option 4).
2. A key of what this repo sets, with the data a trial read identified beside it, by its sample and a
   digest of its bars: the terms of ADR 0041's chosen option 2, a fixed sample per study.

## Decision Outcome

Chosen: **option 2**, the terms ADR 0041 set with its sample. Option 1 counts one configuration once per data step, so N
grows with the data and not with what was tried. Terms 1, 2 and 3 below were ADR 0041 terms 3, 4 and 6.

1. **Data identity.** A trial's data is its sample and `data_digest`: for each bar type the run read,
   the SHA-256 hex digest of the UTF-8 bytes of one line per bar,
   `bar_type|open|high|low|close|volume|ts_event|ts_init`, each field as NautilusTrader prints it, the
   bars sorted by `ts_init` and `ts_event`, then by their text, the lines joined by `\n`. The bars, and the session days,
   are those the run read, as the engine's cache kept them (`Cache.bars`), not a later read of the
   catalog. The cache's `CacheConfig.bar_capacity` is raised to the count of the sample's bars in the
   catalog, read after every other check. A run whose cache holds another number of bars than it
   replayed (`BacktestResult.iterations`, one per data item at the pinned commit), or a bar outside
   the sample, is told `FAIL`. The digest also holds, keyed by instrument ID, the SHA-256 of each
   instrument the bar types name, as the engine's cache held it (`Cache.instrument`): its definition as
   NautilusTrader's backtest node compares two (`instrument_definitions_match`, its serialized form
   without `ts_event` and `ts_init`, every field, a value's text and `info` included), read as
   upstream's `Instrument.to_dict()`, in the key's canonical JSON, and beside it each `Currency` the
   instrument holds, by the fields that type has (`code`, `precision`, `iso4217`, `name`,
   `currency_type`). Upstream writes a currency as its code, which names one currency within a
   process; the ledger compares runs of different processes. The node loads
   the definition from the catalog with the bars, and the engine trades against it. A cache with no
   definition of one is told `FAIL`.
   Nothing upstream identifies data,
   so this is local glue like the config key. It is taken from the bars, not the catalog's file layout, so
   a writer that adds, rewrites or consolidates files moves no digest unless a bar of the range changes.
   Records of one key on one sample that differ in range, returns, session days or digest disagree and
   stop the ranking (ADR 0054 term 5), and so do any two records on one sample that read one bar type
   as different bars, or one instrument as different definitions, whatever their keys; a record whose
   digest lacks a bar type or instrument it loads stops it too.
2. **The key is the configuration.** This term supersedes what ADR 0035 term 4 (now ADR 0054 term 4) said `config` holds,
   which was every property upstream reflects and the run's range. `config` now holds what this repo
   sets: each strategy's class (`module:Class`), its config's own fields and each field of
   NautilusTrader's `StrategyConfig` it holds at another value than the default at the pinned release
   (read from the class by reflection, so a field upstream adds is not missed), the venue's name,
   settings, fee rates and starting balances, and the bar types; not the sample. The strategies are
   built from the configs after the key is taken. Floats are written by `repr`, enums by name, decimals
   normalised, balances as `Money` prints them, one per currency and sorted (two of one currency are
   refused, since the venue keeps one), other NautilusTrader types by `str()`; a value with no
   stable text form is refused, naming its field. N is the number of distinct keys over every record,
   on every sample. Known limit: the defaults are those of the pinned NautilusTrader, so a pin that
   changes one moves the key of every config that holds that field at its old or new default; a pin
   move compares `canon_trading.ledger.base_config_defaults()` before and after, which
   `tests/test_ledger.py` pins, and names the keys that move. Such a pin also changes how a config
   that keeps the default runs, under the same key, as any change of upstream behaviour does; its
   records then disagree on one sample (ADR 0054 term 5), and the sample is moved.
3. **A record with no key ran no backtest.** `record_trial` writes the key before the run starts, so a
   record without one is what a process killed between `ask` and that write leaves. The ranking stops,
   naming it, until `mark_not_run(study, number)` clears it: a `RUNNING` one is told `FAIL`, its number
   joins the study user attribute `not_run`, and it is kept and not counted in N. A `WAITING` one stays
   `WAITING` and its number joins `not_run_waiting`: it is cleared only while it is `WAITING`, or once
   `record_trial` took it from Optuna's queue and ran nothing in it; started by another writer after its
   mark, it may have run anything, stops for good and is not marked again. A record with a key
   is refused, since only a later record of its key resolves it; so is a keyless record that shows a
   run (an outcome, returns or session days, or a result Optuna keeps: `COMPLETE` or `PRUNED`, a value
   or an intermediate value), a trial of an unknown configuration, which stops for good.

Local, no upstream, where the upstreams read say nothing:

* `not_run` and `not_run_waiting` (term 3): Optuna 5.0.0 has states, not a "ran nothing" mark, and its `fail_stale_trials`
  only tells a stale trial `FAIL`; the DSR paper counts trials and says nothing of a record with none.
* The cache's bar capacity is sized from a count of the catalog's bars before the run (term 1);
  upstream's default keeps 10,000 bars of a type.

Who decided. The orchestrator chose option 2 and its terms under the owner's delegation of
2026-11-10 (ADR 0010's Evidence).

### Consequences

* Good: N counts what was tried, not the data steps; a record left by a killed process is cleared
  without lowering N.
* Known limit: the engine's cache holds every bar of the sample for the length of a run, so memory
  grows with the bar count. That is small for daily bars (a study of about 1,400 daily bars per
  instrument), but a sample of minute bars over years needs its memory checked before it is recorded.
* Bad: the digest is local glue, and a NautilusTrader pin that prints a field differently moves it; the
  records then disagree and the sample must be moved (ADR 0041 term 5).

### Confirmation

* `tests/test_ranking.py`: N counts keys across samples; a keyless
  record stops until marked, then N leaves it out, and marking a record with a key is refused.
* A corrected catalog disagrees until the sample moves: ADR 0041's Confirmation (`tests/test_ledger.py`).
* The second review's cases, each a test that failed before its fix: the catalog replaced between the
  replay and the digest (`test_the_digest_is_of_the_bars_the_run_read_not_a_later_catalog`). With it:
  a cache short of a bar, or holding one outside the sample, is told `FAIL`; a hole closed by
  `consolidate_data` records the same digest as a catalog loaded whole.
* The third review's case, a test that failed before its fix: a keyless record told a value
  (`test_a_keyless_record_told_a_value_cannot_be_marked_and_stops`).
* The fourth review's cases, each a test that failed before its fix: a keyless record with outcome
  `failed` (`test_a_keyless_record_with_a_failed_outcome_shows_a_run`) or with a suggested parameter
  (`test_a_keyless_record_with_a_suggested_parameter_shows_a_run`); a keyless `WAITING` record, marked
  and left `WAITING`, which stops again once an `ask` starts it
  (`test_a_keyless_waiting_record_can_be_marked_not_run`); two marks at once
  (`test_two_marks_at_once_are_both_kept`), and a mark only on a study the ledger opened; a `not_run`
  attribute of another shape (`test_a_not_run_list_not_in_the_writers_shape_stops`); an `IntEnum`, a
  `StrEnum`, or a NaN or infinite float or `Decimal` in a config
  (`test_a_value_keyed_by_what_it_is_not_is_refused`). A store that refuses the key's write leaves a
  `FAIL` record with no outcome, which `mark_not_run` clears
  (`test_an_error_after_the_trial_is_asked_for_is_told_failed`).
* The fourth review's second part, each a test that failed before its fix: a bar type the catalog covers
  with no bar in the sample is in the digest
  (`test_a_bar_type_with_no_bar_in_the_sample_is_in_the_digest`), and a digest that lacks a bar type the
  run loads stops the ranking (`test_a_digest_that_lacks_a_bar_type_the_run_loads_stops`); a base
  `StrategyConfig` field set away from its default is in the key
  (`test_a_base_strategy_config_field_set_away_from_its_default_is_in_the_key`), a field with no stable
  text form is named (`test_a_field_with_no_stable_text_form_is_named`), and the pinned defaults are
  pinned (`test_the_base_defaults_the_key_is_taken_against_are_the_pinned_ones`). The eight keys of the
  step 7 study and the tests' default key are byte for byte as before
  (`test_the_keys_of_default_configs_are_as_they_were`, which passes before and after).
* A fifth review's case, a test that failed before its fix: a `SessionMaCrossConfig`, a subclass
  `record_trial`'s type admits, is refused before a trial, naming its class
  (`test_a_config_of_another_strategy_class_is_refused_before_a_trial`).
* A fifth review's case, a test that failed before its fix: two definitions of MSFT under the same
  bars (a 5-share order limit in one) traded other returns under one key, and three keys recorded across
  them ranked and passed at DSR 0.9916; they now stop on the instrument
  (`test_trials_on_two_definitions_of_one_instrument_stop`). With it: a digest without its instruments
  stops (`test_a_digest_without_its_instruments_stops`), a definition stamped again keeps its digest
  (`test_an_instrument_s_digest_is_its_definition_not_when_it_was_stamped`), and a catalog holding two
  definitions of one instrument does not run, so the cache's is the one traded against
  (`test_a_catalog_with_two_definitions_of_one_instrument_does_not_run`).
* A sixth review of the gate, tests that failed before their fix: USD at precisions 2, 0 and 8 gives
  three digests (`test_an_instrument_s_digest_holds_the_precision_of_its_currency`); starting balances
  are keyed one per currency in any order, and two of one currency are refused
  (`test_starting_balances_are_keyed_one_per_currency_in_any_order`); a queued record marked while
  `WAITING`, then started and told `FAIL` by another writer, stops and cannot be marked again
  (`test_a_waiting_record_marked_and_then_failed_by_another_writer_stops`,
  `test_a_keyless_waiting_record_can_be_marked_not_run`, which had asserted the old reading). With
  them, passing before and after: one marked while `WAITING` stays cleared once `record_trial` takes it
  (`test_a_queued_record_marked_while_waiting_stays_cleared_once_record_trial_takes_it`); `5` and `5.0`,
  and `0.1` and `0.10`, are two definitions to the node and to the digest
  (`test_two_texts_of_one_value_are_two_definitions_to_the_node_and_the_digest`); and the digest's
  instruments are tested with an instrument (`test_the_digest_holds_each_instrument_the_run_names`,
  which fails with the instruments left out, where the test it replaces passed).

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; bundled, in the installed
package; run, executed in this checkout.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — version of 2014-07-31. p. 7: a trial is
  one of "a set of N independent backtests"; p. 8: "T is the sample length", listed among the selected
  strategy's quantities; pp. 3–4: "multiple strategies on the same dataset", "multiple tests on the same
  data set"; p. 9: "the set of trials carried out so far"; p. 10 fn. 1: "the counter of trials cannot be
  turned back". The paper does not state that all trials share one T, and says nothing on different
  periods or a growing sample. Fetched by an `upstream-scout`, text and page images.
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
  the old run the new bars' digest and mixed inputs passed (DSR 0.95144). Term 1 (then ADR 0041 term 3)
  corrected, with no change of decision; the review's other findings are ADR 0041's. Sources:
  nautilus-trader 2.0.0rc8 in `.venv`, `common/__init__.pyi` (`CacheConfig.bar_capacity`,
  `Cache.bars`), `bundled`; a run of 40 bars kept 10 at a capacity of 10 and 40 at 100, with the same
  orders. Run.
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/engine.rs
  — `self.iteration += 1` once per data item in `run_impl`'s loop, copied to `BacktestResult.iterations`
  by `get_result`. Fetched. `ParquetDataCatalog.consolidate_data` closed a stretch between two files
  with no bar (run).
* A third review of the gate, 2026-11-10, read only, each finding reproduced here as a failing test:
  a keyless record told a value could be marked not run, leaving N short. Term 3 (then ADR 0041 term 6)
  corrected, with no change of decision; the review's other findings are ADR 0041's. Source: Optuna
  5.0.0 `trial/_state.py`, `COMPLETE` and `PRUNED`. `bundled`.
* ADR 0041 as it stood on `main` before 2026-11-10's split: terms 3, 4 and 6, their glue, consequences,
  tests and evidence, moved here with no change of decision (run: `git show` of the record on `main`).
* Second reader (`evidence-verifier`), 2026-11-10: the sources and the moved text hold; it corrected
  option 2, which had been worded as ADR 0041's option 2, and a pointer left on ADR 0041 in
  `ranking.py`. Code review (`/code-review`, medium) of the split, the same day: the tests credited
  under Confirmation, and pointers left on ADR 0041; corrected.
* A fourth review of the gate, 2026-11-10, read only, by a second reader, each finding reproduced here
  as a failing test: a keyless record with outcome `failed`, or one `Study.optimize` told `FAIL` for a
  NaN with its suggested parameter and no value (`study/_tell.py`), could be marked not run and the
  ranking then passed at N = 3; a keyless `WAITING` record could never be marked, since `mark_not_run`
  told it `FAIL`; two marks at once kept one number; a `not_run` attribute of `True` cleared trial #1;
  an `IntEnum` or `StrEnum` was keyed by value, and a NaN dict key or a `Decimal` NaN or infinity passed
  as text. The code now follows terms 2 and 3 as written, with no change of decision: an outcome shows a
  run, enums are keyed by name, and a value with no stable text form is refused. Sources: Optuna 5.0.0
  in `.venv`, `study/_tell.py` (only a `RUNNING` trial is told), `study/study.py` (`Study.ask` takes the
  oldest `WAITING` trial, `_pop_waiting_trial_id`) and `storages/_base.py` (only a `WAITING` trial's
  state may change, and the class "is not supposed to be directly accessed by library users"),
  `bundled`; NautilusTrader's enums are pyo3 classes, not Python enums (run). Kept as a known limit: a
  keyless record in the shape `record_trial` wrote before this change on a refused key write (an error
  and outcome `failed`) now shows a run and stops for good; the shared ledger held no keyless record
  when the third review's fixes landed.
* A fourth review of the gate, second part, 2026-11-10, read only, by a second reader, each finding
  reproduced here as a failing test: a bar type the catalog covered with no bar in the sample was left
  out of `data_digest`, so a trial that read 0 of its bars and one that read 40 agreed and passed (DSR
  0.9987 in the reader's run; the failing test here is the digest itself), and a run of that bar type
  alone wrote `data_digest = {}`; term 1's "each bar type the run read" now includes one read with no
  bar, at the digest of no lines. `trial_config` hashed only `vars(config)`, so a base field
  `MaCrossConfig` passes to the engine, `order_id_tag` among them, was left out and two configurations
  shared a key. A second reader corrected term 2 on 2026-11-10: a base field set away from its pinned
  default is something this repo set, so the key holds it; a default config's key does not move. Source:
  nautilus-trader 2.0.0rc8 in `.venv`, `nautilus_trader.trading.StrategyConfig`, whose 16 fields are
  pyo3 getset descriptors, listed with `inspect.getmembers(StrategyConfig, inspect.isgetsetdescriptor)`
  (run); reflection rather than a list written here, so a field a later pin adds is compared too. Its
  code review added that a record with no `config` in the writer's form skipped the bar-type check, now
  a stop. Records already in the shared ledger are not affected: each step 7 trial read one bar type
  with 1,366 bars (ADR 0052) and holds no base field away from its default.
* A fifth review of the gate, 2026-11-10, by a second reader: `record_trial` runs `MaCross` for every
  config while term 2's key held a subclass's own fields, so two `SessionMaCrossConfig`s differing only
  in `limit_offset` were two trials of one run, and one with `wind_down_at` was refused for its value.
  Term 2's class is the class that runs; a config not exactly `MaCrossConfig` is now refused, the
  narrow reading, since only `MaCross` is run and building another class from a config is a change of
  decision this record has not made. The step 7 study's eight are `MaCross` (run). Reproduced as a
  failing test. No change of decision.
* A fifth review of the gate, 2026-11-10, by a second reader: term 1 held the bars only, though the
  intent (ADR 0041's) is the data each trial read from the catalog, and the instrument definition is
  part of it. Term 1 corrected. Run at the pin: on identical bars, of the fields NautilusTrader's
  `Equity` constructor takes (`model/__init__.pyi`, `bundled`; it takes no size precision, size
  increment or fees, so the reviewers' `size_precision` cannot be set on an `Equity`), `min_quantity`
  20, `max_quantity` 5, `price_precision` 2 or 5 and `currency` EUR turned a one-instrument run of 10
  shares idle; `lot_size` 3 or 100, `max_quantity` and `min_quantity` 10, `max_price`, `min_price`,
  `margin_init`, `price_increment` 0.0002 or 0.01, `isin`, `tick_scheme` and `info` changed nothing; in
  a two-instrument run, `max_quantity` 5 on one kept the run `traded` with other returns. An idle run
  stops the ranking already, and one key on both definitions disagreed on its returns (ADR 0054 term
  5), so only records of different keys passed. `info` is hashed with the rest: a load writes a
  definition only when the catalog holds none (`canon_trading.bars_to_catalog`), so a refresh does not
  restamp it. Effects of other instrument classes' fields: UNVERIFIED. Every record made before this correction stops, the step 7 study's eight included
  (ADR 0052).
* A sixth review of the gate, 2026-11-10, by three second readers and a third, each finding reproduced
  here as a failing test unless it says otherwise. Two pulled the instrument digest apart: one found
  definitions equal as numbers but written at another precision (`5` and `5.0`) stop the ranking, the
  other that USD at precisions 2, 0 and 8, registered again in the recording process, gave one digest
  and three keys across them passed at DSR 0.9922. Both are settled by upstream's rule for two
  definitions of one instrument: the backtest node refuses a second definition unless its serialized
  form without the two stamps is the same (`instrument_definitions_match`,
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/exchange.rs,
  fetched), and that form writes a `Quantity` as its text with its precision (`Serialize`,
  `crates/model/src/types/quantity.rs`, fetched) and a `Currency` as its code alone (`Serialize`,
  `PartialEq` and `Hash` read only `code`, `crates/model/src/types/currency.rs`, fetched). So `5` and
  `5.0` are two definitions to upstream as to the digest: the node refused a catalog holding both, and
  `0.1` and `0.10` likewise (run); not a defect. The currency's precision lives in the process's
  registry, not the catalog, and changed what a run traded: term 1 now holds each currency the
  instrument holds by its fields, found by reflection over the instrument's getters (`Equity.to_dict`
  names it `currency`, its getter `quote_currency`; `crates/model/src/python/instruments/equity.rs`,
  fetched). Nothing in this repository registers a currency (read). The digest of every instrument
  moves with this; a record made since the instruments joined the digest disagrees with a new one on
  its sample, and the sample is moved. The step 7 study's records hold no instrument digest and stop
  until run again on a moved sample, as before (ADR 0052). Starting balances were keyed in the order
  given and with repeats (a third reader, run); term 2 corrected, and no key of a single balance moves
  (`test_the_keys_of_default_configs_are_as_they_were` passes). A queued record marked while `WAITING`,
  later started and told `FAIL` by another writer, stayed out of N (a third reader); term 3 corrected,
  keeping the case `record_trial`'s own `ask` makes, which takes the oldest queued trial (Optuna 5.0.0
  `Study._pop_waiting_trial_id`, `bundled`) and tells it `FAIL` with a note and no run. A ranking of a
  ledger that holds a `WAITING` record in `not_run` from before stops until it is marked again. By
  reading, test only: the test of the digest's bar types passed no instrument (a second reader). Left
  to the strategy config's own change: a `bool` keyed apart from the `int` it equals, and fields
  `MaCrossConfig` takes without using. No change of decision.
* Code review (`/code-review`, high) of the sixth review's fixes, 2026-11-10: a value told where the
  returns give no Sharpe ratio passed the check of told values, now a stop
  (`test_a_value_told_where_record_trial_tells_none_stops`, which failed first); the told value is
  computed by one function, `told_sharpe`, for the writer and the gate; only the getters named for a
  currency are read, an error there naming the instrument. Kept as known limits: a number `not_run` holds from a mark made
  while `WAITING` before `not_run_waiting` existed reads as a mark of a `FAIL` record, so one started
  since by another writer stays cleared (the step 7 study has no mark, ADR 0052); a refused write of
  the note on a queued trial `record_trial` takes leaves a marked one stopped for good, the safe side;
  and a configuration with balances in two currencies given out of order takes a new key, though a
  ranking of one is UNVERIFIED, since NautilusTrader's daily series takes an account in one currency
  only (`calculate_snapshot_returns`, `crates/analysis/src/analyzer.rs` at the pinned commit, fetched).
  Not done: the series of a record whose value is checked is built again for the matrix.
