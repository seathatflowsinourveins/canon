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
   `backtest_end` and `returns_series`. The code that writes it is not written yet.
2. **Run each variant over the whole period**, one `BacktestNode` per variant, through
   `canon_trading.backtest.run_backtest`. Accept `BacktestResult.returns_series` only if it is the daily
   portfolio series, with one key per UTC day. Any other shape is the per-position fallback, and that run is
   rejected. Drop days the venue was closed, and trim every variant to a common range of T rows.
3. **DSR of the pick** (eqs. 1–2).
   SR₀ = √V·[(1−γ)Z⁻¹(1−1/N) + γZ⁻¹(1−1/(Ne))] and DSR = Z[(SR−SR₀)√(T−1) / √(1 − γ₃SR + (γ₄−1)/4·SR²)].
   * SR and V are per session, not annualised. V is the variance of every trial's SR.
   * γ₃ is the pick's skewness and γ₄ its raw kurtosis, which is 3 for a Normal distribution.
   * **Reject the pick below 0.95**, with N = all trials. The paper's N is the number of independent trials,
     and counting dependent ones overstates SR₀ (App. A.3), so this is the strict reading.
   * Report N̂ = ρ̂ + (1−ρ̂)N (eq. 9) only when T ≥ N(N−1)/2 (p. 15). N̂ never replaces N in the gate.
   * Do not take Sharpe, skewness or kurtosis from NautilusTrader. Its default Sharpe is annualised, its
     skewness is bias-corrected, and its kurtosis is excess.
   * Implementation: **skfolio v1.12.8**, `measures.deflated_sharpe_ratio(returns, trial_sharpe_ratios)`.
     It takes the pick's return series and every trial's Sharpe ratio, takes N from that array, and uses
     the sample standard deviation. For N̂, call `expected_max_sharpe_ratio` and then
     `probabilistic_sharpe_ratio`.
   * skfolio raises nothing on three inputs that make a pick pass more easily. It leaves a NaN trial
     Sharpe ratio out of N; it ignores a NaN in the pick's returns; and for one trial, or trials with
     equal Sharpe ratios, SR₀ is 0. So the ranking stops unless every trial's Sharpe ratio and every
     return of the pick is finite, N ≥ 2 and V > 0.
4. **Paper.** A pick that passes runs beside the incumbent on separate Alpaca paper accounts, through
   `canon-alpaca`. This checks fills, costs and trade counts against the backtest and against the limits of
   paper fills. It is not a significance test, and no rule for one was read.

Who decided. Under the owner's delegation of 2026-11-10 (Evidence), the orchestrator chose the 0.95
threshold, N as every trial in the ledger, the scope above and skfolio. The probability of backtest
overfitting (PBO) is a diagnostic outside this protocol: ADR 0027 holds it, and what each of the nine
implementations that were read does.

Limits:

* The DSR does not check look-ahead, costs or fills; a leaky strategy passes it (Gençay 2026). It assumes
  roughly IID returns.
* A trial that never traded has no Sharpe ratio (0/0). It is not dropped, so it stops the ranking. What
  value it should count as is not decided, and the paper does not say.
* Out of scope: strategies that fit a model to the data, which need purged cross-validation (López de Prado
  2018, ch. 7 and 12; not read, UNVERIFIED), and any test that one variant's Sharpe ratio exceeds another's.

### Consequences

* Good: the gate is specified down to the formula, and its implementation reproduces the paper's example.
* Bad: the ledger and the code that builds the returns and applies the gate are not written. Until they
  are, backtests are reported, not ranked.
* Bad: skfolio brings 13 packages for three functions, among them scikit-learn, scipy, cvxpy-base, clarabel
  and plotly. It has 130 releases since 2023-12-14, 27 of them since 2026-10-16.

### Confirmation

* `tests/test_deflated_sharpe.py` runs the DSR example on pp. 9–10 through skfolio: N = 100, V = 1/2,
  T = 1250, γ₃ = −3, γ₄ = 10, SR = 2.5, with V and SR annualised at 250 periods per year. It passed on
  Python 3.14.10 on 2026-11-10 with these results:
  * SR₀ ≈ 0.1132 and DSR ≈ 0.9004;
  * DSR = 0.9505 at N = 46;
  * DSR = 0.9505 at N = 88, with γ₃ = 0 and γ₄ = 3.

  skfolio takes a return series, not moments, so the test builds series that carry them. γ₃ = −3 with
  γ₄ = 10 is only reachable by a two-valued series, and 1250 observations give −2.9994 and 9.9965.
  It also works eqs. 1–2 without skfolio, under the conventions read in skfolio's source, and gets the
  same values. And it pins what skfolio does with input this protocol forbids: the three cases in step 3,
  and annualised trial Sharpe ratios beside daily returns, which send a passing pick below 0.01.
* The test covers skfolio, not this repo: the code that applies the gate does not exist yet.
* A ranking report states N, T, V, the DSR, and the range of the ledger it read.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed in this
checkout.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — the DSR paper, version of 2014-07-31, sha256
  ca4a1e83. Read: eqs. 1 and 2, the example on pp. 9–10, pp. 3, 6 and 14–15, where the paper writes M for
  all trials. Fetched; the equations read from page images.
* The owner, 2026-11-10: "auto proceed with sota convergence you as master orchestrator". The choices under
  "Who decided" were made under that sentence by the orchestrator, not by the owner item by item.
* https://pypi.org/pypi/skfolio/json — 1.12.8, released 2026-11-08, is the latest of 130 releases; BSD
  3-Clause; `requires-python >=3.10`, with a Python 3.14 classifier. Fetched.
* https://raw.githubusercontent.com/skfolio/skfolio/433991778607285ecd274942e89029190c45e44c/src/skfolio/measures/_measures.py
  — the three functions: raw kurtosis, √(T−1), N as the count of finite trial Sharpe ratios, sample
  standard deviation. Fetched; identical to the file in the PyPI source archive (sha256 3f479188). Tag
  `v1.12.8` resolved to that commit.
* At the same commit: `tests/test_measures/test_measures.py` asserts 0.9004 through
  `probabilistic_sharpe_ratio`, on the two-valued series this repo's test also uses; and
  `.github/workflows/ci.yml` tests Python 3.10 to 3.14 on Linux, and 3.14 on macOS and Windows. Fetched.
* `uv add skfolio==1.12.8` on Python 3.14.10, macOS arm64 — installed skfolio and 13 dependencies from
  wheels. `uv run --locked pytest tests/test_deflated_sharpe.py`: 12 passed. Run.
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
  * nautilus-trader 2.0.0rc8 in `.venv`, on a known series: `SharpeRatio()` is mean/std(ddof 1)·√252,
    skewness is bias-corrected and kurtosis is excess. `bundled`, run.
  * https://docs.alpaca.markets/us/docs/paper-trading.md — separate paper accounts; 10% random partial
    fills. Fetched.
* ADR 0004 — Alpaca's backtest skill has the agent write a script per run. Read there on 2026-11-09 at the
  pinned commit of `alpacahq/alpaca-skills`; not re-read: UNVERIFIED this session.
* Second reader (`evidence-verifier`). 2026-11-09: sent the PBO half back to `proposed`; it is now ADR
  0027. 2026-11-10: the equations, the example, the pins and the test hold; corrected how skfolio counts
  N, "maintainers", the release cadence and the idle-trial limit; could not check the delegation or
  option 2's source.
