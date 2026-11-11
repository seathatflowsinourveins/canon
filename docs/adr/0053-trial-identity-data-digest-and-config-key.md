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
   stop the ranking (ADR 0035 term 5), and so do any two records on one sample that read one bar type
   as different bars, whatever their keys.
2. **The key is the configuration.** This term supersedes what ADR 0035 term 4 said `config` holds,
   which was every property upstream reflects and the run's range. `config` now holds what this repo
   sets: each strategy's class (`module:Class`) and its config's own fields, the venue's name,
   settings, fee rates and starting balances, and the bar types; not the sample. The strategies are
   built from the configs after the key is taken. Floats are written by `repr`, enums by name, decimals
   normalised, balances as `Money` prints them, other NautilusTrader types by `str()`; a value with no
   stable text form is refused. N is the number of distinct keys over every record, on every sample.
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
  tests and evidence, moved here with no change of decision (read).
* Second reader (`evidence-verifier`), 2026-11-10: the sources and the moved text hold; it corrected
  option 2, which had been worded as ADR 0041's option 2, and a pointer left on ADR 0041 in
  `ranking.py`. Code review (`/code-review`, medium) of the split, the same day: the tests credited
  under Confirmation, and pointers left on ADR 0041; corrected.
