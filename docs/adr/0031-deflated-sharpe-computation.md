---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Deflated Sharpe computation: eqs. 1–2 through skfolio, with the input it lets pass refused

## Context and Problem Statement

ADR 0010 ranks strategy variants and rejects the pick when its deflated Sharpe ratio (DSR) is below 0.95,
with N = all trials in the ledger. Which formula and conventions compute that DSR, which code runs them,
and what input must the ranking refuse before it does?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: where upstream is silent, say so. Every statistic comes from a published source.

## Considered Options

1. Write our own selection statistics (ADR 0010, option 1).
2. NautilusTrader's Sharpe ratio, skewness and kurtosis.
3. skfolio's measures, under the paper's conventions.

## Decision Outcome

Chosen: **option 3**. Option 1 invents statistics. Option 2 is ruled out by the conventions below.

ADR 0010, step 3, **DSR of the pick** (eqs. 1–2):
SR₀ = √V·[(1−γ)Z⁻¹(1−1/N) + γZ⁻¹(1−1/(Ne))] and DSR = Z[(SR−SR₀)√(T−1) / √(1 − γ₃SR + (γ₄−1)/4·SR²)].

* SR and V are per session, not annualised. V is the variance of every trial's SR.
* γ₃ is the pick's skewness and γ₄ its raw kurtosis, which is 3 for a Normal distribution.
* The gate, at 0.95 with N = all trials, and the report of N̂ are ADR 0010's, step 3.
* Do not take Sharpe, skewness or kurtosis from NautilusTrader. Its default Sharpe is annualised, its
  skewness is bias-corrected, and its kurtosis is excess.
* Implementation: **skfolio v1.12.9**, `measures.deflated_sharpe_ratio(returns, trial_sharpe_ratios)`.
  It takes the pick's return series and every trial's Sharpe ratio, takes N from that array, and uses
  the sample standard deviation. For N̂, call `expected_max_sharpe_ratio` and then
  `probabilistic_sharpe_ratio`.
* skfolio raises nothing on three inputs that make a pick pass more easily. It leaves a NaN trial
  Sharpe ratio out of N; it ignores a NaN in the pick's returns; and for one trial, or trials with
  equal Sharpe ratios, SR₀ is 0. So the ranking stops unless every trial's Sharpe ratio and every
  return of the pick is finite, N ≥ 2 and V > 0. V counts as 0 when every trial's Sharpe ratio equals
  the first's under `math.isclose` at its documented default (`rel_tol` 1e-9): copies of one
  configuration differ only in rounding. That tolerance is a definition of floating-point noise chosen
  here; no statistical source gives one, and whether it is enough is UNVERIFIED. With `abs_tol` 0 no
  nonzero value is close to 0, so ratios within rounding of 0 are not counted equal; their pick's DSR
  is then near 0.5, below the gate.
* Two more stops. Keys tied at the highest Sharpe ratio under the same tolerance are one pick when
  their returns are equal day by day under it, with `abs_tol` 1e-9 of the largest return so a 0 against
  rounding counts as equal (each still counts in N, and they give one DSR); tied
  keys whose returns differ leave no one pick and stop the ranking. A DSR skfolio gives as not finite
  stops it too. Each stop names its keys and trial numbers. N̂ below 1 (ρ̂ rounding above 1, its
  bound on p. 14) is raised to 1, skfolio's least; the DSR at N̂ is withheld when its benchmark is
  below 0, and the report says why. Neither enters the gate.

Who decided. Under the owner's delegation of 2026-11-10 (Evidence), the orchestrator chose skfolio.

### Consequences

* Good: the gate is specified down to the formula, and its implementation reproduces the paper's example.
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
  same values. And it pins what skfolio does with input ADR 0010's protocol forbids: the three cases above,
  and annualised trial Sharpe ratios beside daily returns, which send a passing pick below 0.01.
* That test covers skfolio. The code that applies the gate, `canon_trading.ranking`, is tested in
  `tests/test_ranking.py`: each refusal above, and a passing and a failing pick worked without skfolio.

## Evidence

* Split from ADR 0010 on 2026-11-10 with no change of decision: the text above is ADR 0010's own, moved,
  except the title, the Context, the Considered Options, the `Chosen` line, the line introducing step 3,
  the line pointing to ADR 0010's gate, the `Who decided` line, which keeps only the choice of skfolio,
  and the reference to the three cases in Confirmation. The Decision Drivers are ADR 0010's, copied. The
  lines below keep their original dates and how each was checked. The paper and the delegation support
  both records and are kept whole in each; the second reader's line keeps only what concerns this record.

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed in this
checkout.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — the DSR paper, version of 2014-07-31, sha256
  ca4a1e83. Read: eqs. 1 and 2, the example on pp. 9–10, pp. 3, 6 and 14–15, where the paper writes M for
  all trials. Fetched; the equations read from page images.
* The owner decided on 2026-11-10 that the orchestrator proceeds automatically on state-of-the-art
  convergence, leaving the open choices to it. The choices under "Who decided" were made under that delegation by the
  orchestrator, not by the owner item by item.
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
* Moved to v1.12.9, 2026-11-10. https://github.com/skfolio/skfolio/releases/tag/v1.12.9 lists two
  prior-model fixes (EntropyPooling, BlackLitterman) and type-check rules updated for ty 0.0.100. The compare
  v1.12.8...v1.12.9 changes 20 files. `measures/` and `population/` are not among them, and
  `portfolio/_base.py`, `portfolio/_portfolio.py` and `utils/stats.py` change only in `ty: ignore`
  comments. Read with `gh api`. `measures/_measures.py` has the same sha256 (761cbbe3) at tag `v1.12.8`
  (`433991778607`), at tag `v1.12.9` (`772109053763`) and in the installed wheel. Fetched. At
  https://pypi.org/pypi/skfolio/1.12.9/json, `requires-python` is `>=3.10` and there is one
  `py3-none-any` wheel. Fetched. `uv add skfolio==1.12.9`, then
  `uv run --locked pytest tests/test_deflated_sharpe.py tests/test_ledger.py tests/test_returns_matrix.py
  tests/test_ranking.py tests/test_variant_study.py`: 230 passed. Run.
* Second reader of the move, 2026-11-10 (`evidence-verifier`): every claim of the move holds. It
  corrected the release-note wording above: the notes update type-check rules, and the rule rename shows
  only in the compare's `pyproject.toml`.
* Read on 2026-11-09 and checked again by the second reader on 2026-11-10:
  * nautilus-trader 2.0.0rc8 in `.venv`, on a known series: `SharpeRatio()` is mean/std(ddof 1)·√252,
    skewness is bias-corrected and kurtosis is excess. `bundled`, run.
* Second reader (`evidence-verifier`), 2026-11-10, the part that concerns this record: the equations, the
  example, the pins and the test hold; corrected how skfolio counts N, "maintainers" and the release
  cadence; could not check the delegation.
* Full review, 2026-11-10 (`/code-review`): V > 0 was an exact test, and one configuration at two
  scales gave V = 1.1e-27 and passed with an undeflated DSR of 0.9843. The tolerance above corrects it.
* https://docs.python.org/3/library/math.html — `math.isclose(a, b, *, rel_tol=1e-09, abs_tol=0.0)`;
  the default "assures that the two values are the same within about 9 decimal digits". Fetched.
* Full review, 2026-11-10 (`/code-review`): an exact tie between bit-identical columns stopped for good;
  N̂ of 0.9999999999999998 reached skfolio and raised a bare `ValueError`; N̂ = 1.003 gave a "DSR at
  N̂" above the undeflated value. The tie, DSR and N̂ terms above answer them.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of ADR 0010 is missing from the two
  records and every citation names the right one. It corrected a reference to a step 3 this record does
  not have, and the second reader's line above, which had named ADR 0010's option 2 and its PBO half.
