---
status: accepted
date: 2026-11-12
decision-makers: the lead, under the owner's delegation (ADR 0074), for the adoption; master orchestrator (Claude) for the contract below
review-by: 2026-12-12
---

# Arm comparison: arch 9.0.0 adopted as a tool for SPA and StepM over the registered arms, one row per session day, reported beside the deflated Sharpe gate and never in its place; the report is not built yet

## Context and Problem Statement

ADR 0010's gate asks whether one pick's Sharpe ratio survives the number of trials. It leaves out of scope
"any test that one variant's Sharpe ratio exceeds another's". The study records (ADR 0092, ADR 0096,
ADR 0100) register dozens of arms against a base arm and judge them at one holdout opening, and nothing here
says which arms beat the base once the number of arms is counted. Which code computes that comparison?
The tests chosen below compare mean outcomes, not Sharpe ratios, so the test ADR 0010 leaves out stays
out.

## Decision Drivers

* Rule 3: a statistic comes from a maintained upstream, not from this repository.
* The gate is the trading path (ADR 0074). Nothing here may loosen it or stand in for it.
* Proportion (ADR 0074): the comparison keeps the gate's reading of the arms honest. As a tool beside the
  gate it lands on the documentation at the pin, the landscape below and one verifier pass, with what was
  not run marked UNVERIFIED.

## Considered Options

1. Write the bootstrap tests here.
2. Stay with skfolio alone.
3. `arch`: `SPA` (White's reality check as refined by Hansen) and `StepM` (Romano and Wolf).

## Decision Outcome

Chosen: **option 3**. Option 1 invents statistics. Option 2 has nothing to call: skfolio at its pin ships
none of these tests. Landscape: the study-practice scout found `arch` as the one maintained Python
implementation of SPA, StepM and the model confidence set; that there is no other is UNVERIFIED.

* **Pin `arch==9.0.0` exactly, and not an earlier release.** Its notes say `studentize=True`, the default
  of `SPA` and `StepM`, "had no effect in previous versions". 9.0.0 is the first release whose default
  statistics are the documented ones. It was published on 2026-11-11, one day before this record, which is
  why the review date is 30 days out.
* **The consumer** is an arm comparison report over the registered arms, built test-first in its own
  change. Until it lands nothing imports `arch`. It calls `arch.bootstrap.SPA` and `arch.bootstrap.StepM`
  only. `MCS` and the rest of the package (volatility models, unit-root tests) are not used.
* **The contract, as the pinned source documents it.** `benchmark` is T losses and `models` T by k losses,
  where a lower loss is better, so an outcome that is a return enters with its sign reversed. T is at
  least 3 and every value finite; NaN is refused. `bootstrap="stationary"` is Politis and Romano's, and
  `block_size` is the average block length. The docstring says it "should be provided"; the default is
  √T. `StepM`'s `size` is the family-wise error rate, and it selects with `SPA`'s consistent p-value.
  `SPA` reports three p-values (lower, consistent, upper). Both test each model's mean loss differential
  against the benchmark, studentized by default; neither tests a Sharpe ratio. For a model identical to
  the benchmark `arch` only issues a `RuntimeWarning` and says such models "should be removed"; it does
  not remove them, so the report does, before the test. The report passes `seed` and writes it down.
* **"Day blocks" means one row per session day.** The bootstraps resample consecutive rows; none takes a
  grouping argument. So each arm's outcomes are first reduced to one value per session day, and
  `block_size` counts days. Resampling event rows clustered by day is not something `arch` documents, and
  it is not built.
* **Beside the gate.** The report is a diagnostic. ADR 0010's gate, its N and its 0.95 are unchanged, and
  a StepM result neither passes nor fails a pick. Making it a condition of the gate needs its own record.

Accepted on the evidence-verifier's pass of 2026-11-12, as a tool that is cheap to leave (ADR 0018).
Not settled here: the block length for daily arm outcomes (`optimal_block_length` exists and needs 11
observations; its fit is UNVERIFIED); the value of a day on which an arm made no entry; whether the
reduction to days discards what the arms differ on. The report's change answers the first two with a
test each.

### Consequences

* Good: the comparison across arms rests on a published procedure with a family-wise error rate.
* Bad: five indirect packages enter `uv.lock`: statsmodels 0.15.0, formulaic 1.2.2, interface-meta 2.0.1,
  patsy 1.0.3 and wrapt 2.5.2.
* Bad: a one-day-old release carries the fix this adoption needs; no older release is a fallback.
* Bad: the studentized default is "about 3 times slower" than before, by the release's own measurement.

### Confirmation

* `scripts/canon lint` compares the registry's `arch` version with `uv.lock`, and the `uv lock --check`
  of `scripts/ci` holds `uv.lock` to `pyproject.toml`.
* The report's change adds its tests and its row in `docs/architecture.md`, and cites this record.

## Evidence

Read 2026-11-12. "Pin" is `bashtage/arch` at `ef63ff467d329320f5e03fcf426df95214648539` (tag `v9.0.0`),
resolved by `scripts/canon resolve arch` and read through `scripts/canon src arch`.

* https://github.com/bashtage/arch/releases/tag/v9.0.0 — the release, published 2026-11-11T22:04:21Z; read through GitHub's API. Fetched.
* Pin, `doc/source/changes/9.0.rst` — "The option had no effect in previous versions", of `studentize` in `SPA` and `StepM`; `:196-197` "about 3 times slower (measured with 2000 observations, 100 models and 1000 replications)"; Python 3.10 to 3.15 supported. Resolved, read. The same text: https://bashtage.github.io/arch/changes/9.0.html — fetched.
* Pin, `arch/bootstrap/multiple_comparison.py:621-750` (`StepM`) and `:796-1020` (`SPA`) — the losses, the three-observation minimum, the refusal of NaN, `block_size`, `size` as the family-wise error rate, the three p-values, the warning on a constant differential (`:898-901`, `:983-992`), the statistic as the average loss differential (`:864-865`), `seed`. Resolved, read.
* Pin, `arch/bootstrap/base.py:1915-1923` — the stationary bootstrap's `block_size` is the "Average size of block"; `:153-163` — `optimal_block_length` needs at least 11 observations; no `group` or `cluster` in the file. Resolved, read.
* https://bashtage.github.io/arch/multiple-comparison/generated/arch.bootstrap.StepM.html — the project's documentation at 9.0.0. Fetched. https://arch.readthedocs.io/ serves 7.2.0 and is not the current documentation.
* https://pypi.org/pypi/arch/json — 9.0.0, `license_expression` NCSA (its `license` field is empty), `requires_python >=3.10`, a `cp314` `macosx_11_0_arm64` wheel uploaded 2026-11-11, requirements numpy, pandas, scipy, statsmodels, packaging. Fetched.
* Pin, `LICENSE.md` — the University of Illinois/NCSA text under a heading that names no licence; GitHub reports NOASSERTION. Resolved, read.
* `uv add --no-sync arch==9.0.0` — resolved 57 packages; `uv.lock` gained arch and the five packages above and changed no other version. Run.
* skfolio at its pin `7721090537634a549008a546c8968cdec0eaf69a`, `src/` — no file names StepM, a reality check or superior predictive ability. Resolved, searched.
* The papers `arch` cites (White 2000; Hansen 2005; Romano and Wolf 2005; Politis and Romano 1994) — not read. UNVERIFIED.
* That `arch`'s results at the pin match those papers, and any run on this repository's arms — not run. UNVERIFIED.
* The evidence-verifier's pass, 2026-11-12: the decision held. It corrected two statements: `arch` warns about a model identical to the benchmark and does not remove it, and the tests are of mean loss differentials, not of Sharpe ratios. It added the slower default and PyPI's field name. Applied.
