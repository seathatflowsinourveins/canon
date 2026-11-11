---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Strategy evaluation: every trial in a ledger, the pick's deflated Sharpe ratio at 0.95, then paper

## Context and Problem Statement

The owner wants backtests, A/B tests "and beyond" taken from upstream practice. NautilusTrader runs variants,
but a collaborator on the project wrote that parameter optimisation was "explicitly left out" and that
walk-forward "probably won't feature in the core codebase". What must a variant show before we say it beats
another, and which code computes it?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: where upstream is silent, say so. Every statistic comes from a published source.

## Considered Options

1. Write our own selection statistics.
2. Alpaca's official backtest skill.
3. Walk-forward analysis as the out-of-sample test.
4. NautilusTrader runs and a trial ledger, then the deflated Sharpe ratio (DSR) as the gate, then Alpaca
   paper accounts.

## Decision Outcome

Chosen: **option 4**, for variants whose rules and parameters are fixed before the run. Option 1 invents
statistics. Option 2 writes one script per run, outside NautilusTrader (ADR 0004). Option 3 is a series of
hold-outs, and a hold-out ignores how many trials were run (DSR p. 6).

Protocol. The pick is the variant with the highest Sharpe ratio over the whole sample, and N is the number
of trials.

1. **Ledger every trial**: its config, data range and returns. N is counted from the ledger and never typed
   (DSR p. 3). An optimiser search counts every configuration it tried. An entry holds NautilusTrader's
   `BacktestRunConfig` with its `id`, and the `BacktestResult` fields `run_config_id`, `backtest_start`,
   `backtest_end` and `returns_series`. `canon_trading.ledger.record_trial` writes it, in the store and
   on the terms of ADR 0035, ADR 0054 and ADR 0081.
2. **Run each variant over the whole period**, one `BacktestNode` per variant, through
   `canon_trading.backtest.run_backtest`. Accept `BacktestResult.returns_series` only if it is the daily
   portfolio series, with one key per UTC day. Any other shape is the per-position fallback, and that run is
   rejected. Drop days the venue was closed. The trim to a common range of T rows is superseded by
   ADR 0041: every variant runs over its study's sample and must cover the same session days, or the
   ranking stops. `canon_trading.returns_matrix` does this through skfolio's `Population` and pandas.
3. **DSR of the pick.** ADR 0031 holds how it is computed: eqs. 1–2, their conventions, skfolio, and the
   input that stops the ranking; ADR 0080 holds the numeric check of each series and when two ratios
   are equal. `canon_trading.ranking.rank` applies it.
   * **Reject the pick below 0.95**, with N = all trials. The paper's N is the number of independent trials,
     and counting dependent ones overstates SR₀ (App. A.3), so this is the strict reading.
   * Report N̂ = ρ̂ + (1−ρ̂)N (eq. 9) only when T ≥ N(N−1)/2 (p. 15). N̂ never replaces N in the gate.
4. **Paper.** A pick that passes runs beside the incumbent on separate Alpaca paper accounts, through
   `canon-alpaca`. This checks fills, costs and trade counts against the backtest and against the limits of
   paper fills. It is not a significance test, and no rule for one was read.

Who decided. Under the owner's delegation of 2026-11-10 (Evidence), the orchestrator chose the 0.95
threshold, N as every trial in the ledger and the scope above; ADR 0031 holds the choice of skfolio. The
probability of backtest overfitting (PBO) is a diagnostic outside this protocol: ADR 0027 holds it, and
ADR 0032 what each of the nine implementations that were read does.

Limits:

* The DSR does not check look-ahead, costs or fills; a leaky strategy passes it (Gençay 2026). It assumes
  roughly IID returns.
* A trial that never traded has no Sharpe ratio (0/0). It is not dropped, so it stops the ranking. What
  value it should count as is not decided, and the paper does not say.
* Out of scope: strategies that fit a model to the data, which need purged cross-validation (López de Prado
  2018, ch. 7 and 12; not read, UNVERIFIED), and any test that one variant's Sharpe ratio exceeds another's.

### Consequences

* Bad: an idle trial stops every ranking of its study (Limits), and so does a configuration that never
  runs to the end (ADR 0054).
* The consequences of computing the gate with skfolio are in ADR 0031.

### Confirmation

* The test of the gate's implementation against the paper's example is in ADR 0031.
* A ranking report states N, T, V, the DSR, and the range of the ledger it read: `RankingReport`,
  checked in `tests/test_ranking.py` with a passing and a failing pick worked without skfolio.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed in this
checkout. The lines on skfolio, its test run and NautilusTrader's Sharpe ratio moved to ADR 0031 when it
was split from this record on 2026-11-10, with no change of decision; the paper, the delegation and the
second reader's line support both records and are kept whole in each.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — the DSR paper, version of 2014-07-31, sha256
  ca4a1e83. Read: eqs. 1 and 2, the example on pp. 9–10, pp. 3, 6 and 14–15, where the paper writes M for
  all trials. Fetched; the equations read from page images.
* The owner decided on 2026-11-10 that the orchestrator proceeds automatically on state-of-the-art
  convergence, leaving the open choices to it. The choices under "Who decided" were made under that delegation by the
  orchestrator, not by the owner item by item.
* https://github.com/nautechsystems/nautilus_trader/discussions/912 — limx0, December 2022: optimisation is
  "explicitly left out"; walk-forward "probably won't feature in the core codebase". Fetched.
* nautilus-trader 2.0.0rc8 in `.venv`, `backtest/__init__.pyi` — `BacktestRunConfig.id`, and
  `BacktestResult.run_config_id`, `backtest_start`, `backtest_end` and `returns_series`. `bundled`.
* https://arxiv.org/abs/2608.27734 — Gençay (2026), a preprint: a leaky oracle with Sharpe 35 survives DSR
  and PBO testing. Fetched, abstract only.
* Read on 2026-11-09 and checked again by the second reader on 2026-11-10:
  * https://nautilustrader.io/docs/md/latest/concepts/portfolio.md — daily mark-to-market returns, with a
    per-position fallback. Fetched.
  * https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/analysis/src/analyzer.rs
    — returns by UTC day, forward-filled. Fetched.
  * https://docs.alpaca.markets/us/docs/paper-trading.md — separate paper accounts; 10% random partial
    fills. Fetched.
* ADR 0004 — Alpaca's backtest skill has the agent write a script per run. Read there on 2026-11-09 at the
  pinned commit of `alpacahq/alpaca-skills`; not re-read: UNVERIFIED this session.
* Full review, 2026-11-10 (`/code-review`): the trim let one short trial set T for every trial and turn a
  rejection into a pass. Step 2's trim clause is superseded by ADR 0041; nothing else here changes.
* Second reader (`evidence-verifier`). 2026-11-09: sent the PBO half back to `proposed`; it is now ADR
  0027. 2026-11-10: the equations, the example, the pins and the test hold; corrected how skfolio counts
  N, "maintainers", the release cadence and the idle-trial limit; could not check the delegation or
  option 2's source.
