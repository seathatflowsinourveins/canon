---
status: proposed
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Strategy evaluation: the deflated Sharpe ratio is ready; the overfitting probability still blocks ranking

## Context and Problem Statement

The owner wants backtests, A/B tests "and beyond" taken from upstream practice. NautilusTrader runs variants,
but its maintainer says parameter optimisation was "explicitly left out" and walk-forward is not in the core.
What must a variant show before we say it beats another, and which code computes it?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: where upstream is silent, say so. Every statistic comes from a published source.

## Considered Options

1. Write our own selection statistics.
2. Alpaca's official backtest skill.
3. Walk-forward analysis as the out-of-sample test.
4. NautilusTrader runs and a trial ledger, then the deflated Sharpe ratio (DSR) and the probability of
   backtest overfitting (PBO) by CSCV, then Alpaca paper accounts.

## Decision Outcome

Proposed: **option 4**, for variants whose rules and parameters are fixed before the run. Option 1 invents
statistics. Option 2 writes one script per run, outside NautilusTrader (ADR 0004, ADR 0009). Option 3 is a
series of hold-outs, and a hold-out ignores how many trials were run (DSR p. 6, PBO §1).

Protocol. The pick is the variant with the highest Sharpe ratio over the whole sample, and N is the number
of trials.

1. **Ledger every trial**: its config, data range and returns. N is counted from the ledger and never typed
   (DSR p. 3, PBO §5.2). An optimiser search gives CSCV one column, its converged result, but N still counts
   every configuration it tried.
2. **Run each variant over the whole period**, one `BacktestNode` per variant. Accept
   `BacktestResult.returns_series` only if it is the daily portfolio series, with one key per UTC day. Any other
   shape is the per-position fallback, and that run is rejected. Drop days the venue was closed, and trim every
   variant to a common range. The result is a T×N matrix with synchronous rows (PBO Algorithm 2.3, cond. i).
3. **DSR of the pick** (eqs. 1–2). This step is ready.
   SR₀ = √V·[(1−γ)Z⁻¹(1−1/N) + γZ⁻¹(1−1/(Ne))] and DSR = Z[(SR−SR₀)√(T−1) / √(1 − γ₃SR + (γ₄−1)/4·SR²)].
   * SR and V are per session, not annualised. V is the variance of every trial's SR.
   * γ₃ is the pick's skewness and γ₄ its raw kurtosis, which is 3 for a Normal distribution.
   * Reject the pick below 0.95, with N = all trials. Correlated trials make this strict (App. A.3).
   * Report N̂ = ρ̂ + (1−ρ̂)N (eq. 9) only when T ≥ N(N−1)/2 (p. 15).
   * Do not take Sharpe, skewness or kurtosis from NautilusTrader. Its default Sharpe is annualised, its
     skewness is bias-corrected, and its kurtosis is excess.
   * Implementation: **skfolio v1.12.8**, `measures.deflated_sharpe_ratio`. For N̂, use
     `expected_max_sharpe_ratio` and then `probabilistic_sharpe_ratio`. Its code matches eqs. 1–2.
4. **PBO by CSCV** (Algorithm 2.3, §3). This step is blocked.
   Cut the matrix into S = 16 equal row blocks. For each of the C(16,8) = 12,870 halvings, take the in-sample
   best and its out-of-sample rank r; ω = r/(N+1), λ = ln(ω/(1−ω)), and PBO is the share of λ ≤ 0. Use it only
   when N ≥ 20 (the paper asks N ≫ 10, §4; 20 is our extrapolation), never as a search objective (§5.2).
5. **Paper.** A pick that passes runs beside the incumbent on separate Alpaca paper accounts. This checks
   fills, costs and trade counts against the backtest and against the limits of paper fills. It is not a
   significance test, and no rule for one was read.

What blocks acceptance. Until these are resolved, backtests are reported, not ranked.

1. **PBO implementation.** The second reader found five permissively licensed Python packages with CSCV PBO.
   None has been audited, and rule 2 sends them through `adopt-upstream` before we write our own. If none
   fits, implement Algorithm 2.3. With no exact paper example, test a hand-computed S = 4 case, splits equal to
   skfolio `CombinatorialPurgedCV(n_folds=S, n_test_folds=S/2)`, and a planted signal well below noise (§6).
2. **PBO's role.** The paper's "customary" 0.05 gate (§3.1) would reject its own planted-signal example, which
   scores 13% (§6). Its error bar treats overlapping splits as independent (§4), and CSCV is biased in both
   directions (Witzany 2021). The owner chooses: a hard gate at a stated level, or a diagnostic reported
   beside the DSR gate.
3. **skfolio is not in the canon.** It needs a registry entry, then `uv add skfolio==1.12.8`, which has not
   been tried on Python 3.14.
4. **The owner ratifies** DSR ≥ 0.95, N counted as all trials, and the scope.

Limits:

* Neither statistic checks look-ahead, costs or fills (PBO §5.2); a leaky strategy passes both (Gençay 2026).
  CSCV is biased against the pick when all means are near zero and over-optimistic when one variant stands
  out (Witzany 2021). Both assume roughly IID returns.
* Out of scope: strategies that fit a model to the data, which need purged cross-validation (López de Prado
  2018, ch. 7 and 12, unread), and any test that one variant's Sharpe ratio exceeds another's.

### Consequences

* Good: DSR is specified down to the formula, and its implementation reproduces the paper.
* Good: what blocks the rest is named.
* Bad: nothing can be ranked yet.
* Bad: skfolio brings scikit-learn, scipy, cvxpy-base, clarabel and plotly for three functions, and it shipped
  28 releases between 2026-10-16 and 2026-11-08.

### Confirmation

* A test runs the DSR example on pp. 9–10 through skfolio. Inputs: N = 100, V = 1/2, T = 1250, γ₃ = −3,
  γ₄ = 10, SR = 2.5, with V and SR annualised at 250 periods per year. Expected results:
  * SR₀ ≈ 0.1132 and DSR ≈ 0.9004;
  * DSR = 0.9505 at N = 46;
  * DSR = 0.9505 at N = 88, with γ₃ = 0 and γ₄ = 3.

  The Python standard library reproduced all four on 2026-11-09.
* A ranking report states N, T, S, V, PBO, DSR, and the range of the ledger it read.

## Evidence

Read 2026-11-09. Fetched means read in this session. Lane means read by a subagent in this session, and
summarised.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — the DSR paper, version of 2014-07-31, sha256
  ca4a1e83. Read: eqs. 1, 2 and 9, and pp. 6, 9–10 and 14–15. Fetched in full; equations read from page
  images. SSRN 2460551 returned 403.
* https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf — the PBO paper, revised 2015-02, sha256 d8bfbada.
  Read: Algorithm 2.3 and §§3–6. Fetched in full. SSRN 2326253 returned 403.
  * Not read: SSRN 2840838, or the version in *J. Computational Finance* 20(4), 2017.
  * Slips: it prints C(16,8) as "12,780", and gives one PBO as both 4% and 0.04%.
* https://www.mdpi.com/2227-9091/9/1/18 — Witzany, *Risks* 9(1):18 (2021), on CSCV bias (§2.3, §4.8). Fetched
  in full, in the in-app browser.
* https://arxiv.org/abs/2608.27734 — Gençay (2026), a preprint: a leaky oracle with Sharpe 35 survives DSR
  and PBO. Fetched, abstract only.
* Arian, Norouzi Mobarekeh and Seco, *Knowledge-Based Systems* 305 (2024), doi:10.1016/j.knosys.2024.112477 —
  reported to rank walk-forward last. Search summaries only: UNVERIFIED.
* https://raw.githubusercontent.com/skfolio/skfolio/v1.12.8/src/skfolio/measures/_measures.py — DSR code:
  raw kurtosis, √(T−1), N as the number of trials, ddof 1. Fetched.
* skfolio's `tests/test_measures/test_measures.py` — asserts 0.9004 through `probabilistic_sharpe_ratio`, on
  synthetic returns that approximate the paper's moments. Lane.
* https://github.com/skfolio/skfolio/releases/tag/v1.12.8 — released 2026-11-08, commit 4339917, BSD 3-Clause.
  Fetched.
* https://cran.r-project.org/web/packages/pbo/index.html — R `pbo` 1.3.5, 2022-05-26, MIT. Fetched. `pypbo`:
  AGPL-3.0, no releases (lane).
* https://pypi.org/project/alphaengine/, https://pypi.org/project/factor-qc/, ml4t-diagnostic, sharpebench and
  cpz-quant — candidate PBO packages. Lane, not audited.
* https://github.com/nautechsystems/nautilus_trader/discussions/912 — maintainer limx0, December 2022. Fetched.
* https://nautilustrader.io/docs/md/latest/concepts/portfolio.md — daily mark-to-market returns, with a
  per-position fallback. Lane.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/analysis/src/analyzer.rs
  — returns by UTC day, forward-filled. Lane.
* nautilus-trader 2.0.0rc8 in `.venv`, run on a known series. `SharpeRatio()` is mean/std(ddof 1)·√252, and
  kurtosis is excess. `bundled`.
* https://docs.alpaca.markets/us/docs/paper-trading.md — separate paper accounts; 10% random partial fills.
  Lane.
* López de Prado, *Advances in Financial Machine Learning* (2018), and Pardo (2008) — books, not read:
  UNVERIFIED.
* 2026-11-09: a second reader (`evidence-verifier`) corrected the N̂ condition, the planted-signal test, the
  skfolio test wording and its release cadence, and the claim that no PBO library exists. It also sent the
  PBO half back to `proposed`.
