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
   the sample, is told `FAIL`.
   Nothing upstream identifies data,
   so this is local glue like the config key. It is taken from the bars, not the catalog's file layout, so
   a writer that adds, rewrites or consolidates files moves no digest unless a bar of the range changes.
   Records of one key on one sample that differ in range, returns, session days or digest disagree and
   stop the ranking (ADR 0054 term 5), and so do any two records on one sample that read one bar type
   as different bars, whatever their keys.
2. **The key is the configuration.** This term supersedes what ADR 0035 term 4 (now ADR 0054 term 4) said `config` holds,
   which was every property upstream reflects and the run's range. `config` now holds what this repo
   sets: each strategy's class (`module:Class`), its config's own fields and each field of
   NautilusTrader's `StrategyConfig` it holds at another value than the default at the pinned release
   (read from the class by reflection, so a field upstream adds is not missed), the venue's name,
   settings, fee rates and starting balances, and the bar types; not the sample. The strategies are
   built from the configs after the key is taken. Floats are written by `repr`, enums by name, decimals
   normalised, balances as `Money` prints them, other NautilusTrader types by `str()`; a value with no
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
   joins the study user attribute `not_run`, and it is kept and not counted in N. A record with a key
   is refused, since only a later record of its key resolves it; so is a keyless record that shows a
   run (an outcome, returns or session days, or a result Optuna keeps: `COMPLETE` or `PRUNED`, a value
   or an intermediate value), a trial of an unknown configuration, which stops for good.

Local, no upstream, where the upstreams read say nothing:

* `not_run` (term 3): Optuna 5.0.0 has states, not a "ran nothing" mark, and its `fail_stale_trials`
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
