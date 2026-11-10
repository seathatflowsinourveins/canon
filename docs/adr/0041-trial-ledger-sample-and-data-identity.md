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
   catalog's stored bars do not reach is refused before a trial is asked for. `record_trial` runs every trial with
   `BacktestRunConfig(start, end)` set to the current sample, and records its number as `sample`.
2. **The whole sample.** Every trial the ranking reads must have the same session days over the sample.
   A trial that lacks one stops the ranking, named with its number, how many days it lacks and the days
   it covers; nothing is trimmed. This supersedes the clause of ADR 0010 step 2 that trims every variant
   to a common range. Different instruments may share a study when their session days agree.
3. **Data identity.** A trial's data is its sample and `data_digest`: for each bar type the run read,
   the SHA-256 hex digest of the UTF-8 bytes of one line per bar,
   `bar_type|open|high|low|close|volume|ts_event|ts_init`, each field as NautilusTrader prints it, the
   bars sorted by `ts_init` and `ts_event`, the lines joined by `\n`. The bars are those of the run's bar types with `ts_init` in the sample, read
   with the same `ParquetDataCatalog.query` that gives the session days. Nothing upstream identifies data,
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
   same range again is a new sample, the way a corrected catalog is taken in. No record is overwritten or
   deleted. The ranking reads only records on the current sample, and stops, naming it, on a key with no
   record there: every configuration the study tried is run again before a ranking.
6. **A record with no key ran no backtest.** `record_trial` writes the key before the run starts, so a
   record without one is what a process killed between `ask` and that write leaves. The ranking stops,
   naming it, until `mark_not_run(study, number)` clears it: a `RUNNING` one is told `FAIL`, its number
   joins the study user attribute `not_run`, and it is kept and not counted in N. A record with a key
   is refused, since only a later record of its key resolves it; so is a keyless record that shows a
   run (an outcome, returns or session days), a trial of an unknown configuration, which stops for good.

Local, no upstream. The ranking's own glue, where the upstreams read say nothing:

* `not_run` (term 6): Optuna 5.0.0 has states, not a "ran nothing" mark, and its `fail_stale_trials`
  only tells a stale trial `FAIL`; the DSR paper counts trials and says nothing of a record with none.
* A tie at the top: keys within `math.isclose` of the best Sharpe ratio with equal returns day by day
  are one pick; tied keys with other returns stop. The paper's pick is "the" highest; skfolio takes
  one series and says nothing of ties.
* The DSR at N̂ is withheld when its benchmark is below 0. skfolio 1.12.8's
  `expected_max_sharpe_ratio` returns a negative benchmark for 1 < N̂ < about 1.28 without comment;
  the expected maximum of N̂ ≥ 1 trials of mean 0 is not below 0.
* A returns record not in `record_trial`'s shape is refused, naming its trial: Optuna stores user
  attributes as any JSON and validates none.
* One ranking gathers every trial's stops before it raises, and the study's (N, V, the tie, the DSR)
  after: no upstream ranks a ledger, so the order is this repo's.

Who decided. The orchestrator chose option 2 and its terms under the owner's delegation of
2026-11-10 (ADR 0010's Evidence).

### Consequences

* Good: T is the session days every trial shares over the sample, and new data cannot reach a study
  silently.
* Good: a stop on data that moved is cleared by moving the sample and re-running, with N kept.
* Bad: after a move, every configuration must run again before any ranking; a configuration whose data
  cannot cover the sample, such as an instrument listed later, stops the study until the sample moves
  to a range it covers.
* Bad: moving the sample reads and rewrites the `samples` list; two sessions moving one study at
  once can lose one move. Moving a study's sample is one session's act.
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
