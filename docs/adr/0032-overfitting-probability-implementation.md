---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2027-02-09
---

# Overfitting probability implementation: skfolio's splits and Sharpe ratios, Algorithm 2.3's last steps here

## Context and Problem Statement

ADR 0027 makes the probability of backtest overfitting (PBO), estimated by combinatorially symmetric
cross-validation (CSCV) as Algorithm 2.3 of the paper, a diagnostic beside ADR 0010's deflated Sharpe
gate. Which code computes it?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: every dependency traces to a pinned upstream, and where sources have not converged we wait.

## Considered Options

1. Adopt one of the packages read, now.
2. Our own code for Algorithm 2.3 (ADR 0027, option 3).
3. Adopt nothing until one candidate passes this repo's tests.

Options 1 and 3 both sit under ADR 0027's option 2, a diagnostic computed by an adopted package.

## Decision Outcome

Accepted: **option 2**, narrowed to what no adopted upstream ships. skfolio 1.12.9, already pinned for
the DSR (ADR 0031), does Algorithm 2.3 up to each half's Sharpe ratios:
`CombinatorialPurgedCV(n_folds=16, n_test_folds=8, purged_size=0, embargo_size=0)` makes the 12,870
halvings of `returns_matrix`'s rows, trimmed to a multiple of 16, and `Portfolio.sharpe_ratio` the ratios.
The fourth step's pick and out-of-sample rank, ω and λ, and the fifth step's share are code here, a
contract its module header names: the paper, the algorithm and its step numbers, and skfolio's two
symbols. Its tests, each failing first: a hand-computed S = 4 case and the §6 planted-signal case. The
paper's open points are settled in ADR 0027.

What each implementation does. "Unspecified" ties are ranked by `np.argsort`, whose default sort is not
stable.

| Package | Licence | Blocks when S does not divide T | Splits | Ties | Other |
|---|---|---|---|---|---|
| trialgate 0.6.0 | MIT | equal; drops the last rows, reports how many | all | against the pick | no dependencies; refuses non-finite input; returns no logits; declares Python 3.11–3.13 |
| backtest-overfitting 1.1.1 | MIT | equal; drops the last rows silently | all | for the pick | numpy, scipy; one PyPI release |
| RiskLabAI 3.2.0 | BSD-3-Clause | unequal | all | unspecified | 71 releases since 2022; declares Python 3.14; needs statsmodels, PyWavelets, joblib |
| factor-qc 0.1.3 | MIT | unequal | all | for the pick | tolerates NaN: N can differ per split, and a split with no in-sample value is skipped |
| cpz-quant 1.2.0 | Apache-2.0 | unequal | all | unspecified | an odd S becomes S+1; result rounded to 4 places |
| alphaengine 2.0.3 | Apache-2.0 | unequal | all at its default S = 10; 2,000 of 12,870 at S = 16 unless asked | unspecified | result rounded to 4 places |
| ml4t-diagnostic 0.2.0 | MIT | n/a | every halving of the caller's folds | unspecified | averages per-fold values instead of recomputing on joined rows; counts strictly below the median |
| sharpebench 0.28.0 | MIT OR Apache-2.0 | unequal | all | against the pick | Rust; wheels for Linux x86-64 and Python 3.10–3.13 only |
| pypbo `4d723f0` | AGPL-3.0 | equal; drops the first rows | all | average rank | not on PyPI, no tags; excluded by licence |

No package is adopted, because the implementations have not converged:

* The nine differ on leftover rows, ties, the split count and the boundary. No two were shown to agree.
* **trialgate 0.6.0** is closest to the statistic as written in ADR 0027, but has one maintainer, five releases
  since 2026-07-10, and does not declare Python 3.14.
* **RiskLabAI 3.2.0** is the most established, in an organisation's repository. Its blocks are equal only
  if the caller first trims T to a multiple of S, its tie order is unspecified, and it brings statsmodels
  and PyWavelets for one function.
* No adoption signal was verified for any of them. None is in the registry: an entry needs a resolved pin.

Why a few lines here rather than RiskLabAI 3.2.0, the most established candidate. Its
`probability_of_backtest_overfitting` (`src/RiskLabAI/backtest/probability_of_backtest_overfitting.py`)
matches Algorithm 2.3 on the halvings (lines 129-149, every C(S, S/2), once T is trimmed), the rank and
logit (lines 69-74: ascending 1-based rank, the best N, ω = r/(N+1)) and the boundary (line 77, λ ≤ 0).
It departs on the one open point it decides: ties. Line 69 ranks by `np.argsort(np.argsort(...))`, with
numpy's default sort, which is not stable, so a configuration tied with the pick lands above or below it
in no stated order, where ADR 0027 ranks it above. Its blocks come from `np.array_split` (line 129),
unequal unless T is trimmed first, and its default Sharpe ratio uses `np.std` with ddof 0
(`backtest_statistics.py` line 51), which leaves the ranks of one halving unchanged. Adopting it would
also bring joblib, statsmodels and PyWavelets for one function.

Who decided. Adopting no package was chosen by the orchestrator on 2026-11-10 under the owner's
delegation quoted in ADR 0010's Evidence. This record said on 2026-11-10 that implementing Algorithm 2.3
here was the owner's decision; that is superseded by the owner's delegation of 2026-11-11 (ADR 0074),
under which the lead chose this route on 2026-11-11, on the condition that RiskLabAI's departure be
stated, as above.

### Consequences

* Good: the candidates are read at pinned source instead of listed from summaries.
* Good: the split and the ratio are upstream; what is code here is the part on which upstreams disagree.
* Bad: a ranking report has no overfitting diagnostic until the code lands, and the code is ours to keep.

### Confirmation

* No PBO package is in `pyproject.toml`, and none is imported.
* When the code lands, its header names the paper, Algorithm 2.3's steps and skfolio's two symbols; its
  tests reproduce the hand-computed S = 4 case and the §6 planted-signal case, each failing first; and a
  ranking report states S, N, T and PBO beside the DSR, or "not computable" below ADR 0027's floors.
* Re-read 2026-11-10 against skfolio 1.12.9 (ADR 0031's pin): the gap is open. `CombinatorialPurgedCV`
  only splits, with unequal blocks; Algorithm 2.3's pick, rank and logit would be our own code (option 2).
* Re-read 2026-11-11 at the same pin, for the extreme-movers study's adoption test: the gap
  is open, and narrower. With skfolio, everything up to each half's Sharpe ratios is upstream: the
  matrix (the first step) from `returns_df`, and the blocks and halvings (the second, after trimming,
  and the third and the start of the fourth) from the splitter. How the
  gate would call it: take `returns_matrix` (a T×N `DataFrame` of daily returns, one column per
  configuration, from skfolio's `Population.returns_df`), drop T mod 16 rows, and iterate
  `CombinatorialPurgedCV(n_folds=16, n_test_folds=8, purged_size=0, embargo_size=0).split(M)`, which
  yields the training rows and a list of eight test blocks, joined, for each of the 12,870 halvings. It
  raises unless T // 16 is at least 2, so T is at least 32 (line 350).
  Each half's Sharpe ratio per column is skfolio's (`Portfolio.sharpe_ratio`, as `session_sharpe`
  already takes it). The pick, its out-of-sample rank, the logit and the share at or below zero are the
  four steps no adopted upstream ships; the nine packages above each do them, and differently.
* skfolio's splitter is the combinatorial purged cross-validation of López de Prado (2018), built to
  refit a model on each training set and recombine test paths (`_combinatorial.py` lines 81-82 and 236;
  López de Prado, SSRN 3104816, 2018, Solution 9, read through a mirror because SSRN returned 403).
  Used here only for its split indices, with purging and embargo at zero, which Algorithm 2.3 does not
  have. That source also puts the remainder rows in the last group, as skfolio does, so trimming T is a
  departure CSCV's equal blocks need, not a correction of skfolio.

## Evidence

* Split from ADR 0027 on 2026-11-10 with no change of decision: the text above is ADR 0027's own, moved,
  except the title, the Context, the Considered Options, the `Proposed` paragraph, the `Who decided` line,
  which keeps only adopting nothing, and the first Consequences bullet, which keeps only its second half.
  The Decision Drivers are ADR 0027's, copied. The lines below keep their original dates and how each was
  checked. The paper line stays in ADR 0027, and this record points to it; the second reader's line keeps
  only what concerns this record.

Read 2026-11-10. Fetched means read in this session as raw text or source. No package below was installed
or run.

* The PBO paper (Definition 2.2, Algorithm 2.3, §§3.1 and 6): its line, and the slips read in it, are in ADR 0027's Evidence.
* Each package: the PyPI source archive of that version, with its sha256 checked against
  `https://pypi.org/pypi/<name>/json`. The file read is byte-identical to the one at the repository's tag,
  except factor-qc 0.1.3, whose repository has no such tag. Fetched.
  * https://pypi.org/pypi/trialgate/json — sha256 8adcadbd; `src/trialgate/validation.py`; tag `v0.6.0` is
    commit dfd175d of 0Smallcat0/trialgate. Its ten other modules were only searched for network and
    subprocess calls; none found.
  * https://pypi.org/pypi/backtest-overfitting/json — sha256 6b005dfe; `overfit.py`; tag `v1.1.1`, 8d19820.
  * https://pypi.org/pypi/RiskLabAI/json — sha256 c498676a;
    `src/RiskLabAI/backtest/probability_of_backtest_overfitting.py`; tag `v3.2.0`, 7d5aa11; 71 releases
    since 2022-08-27.
  * https://pypi.org/pypi/factor-qc/json — sha256 ca6773be; `src/factor_qc/stats.py`.
  * https://pypi.org/pypi/cpz-quant/json — sha256 e65e6a71; `cpz_quant/certification/overfitting.py`; tag
    `v1.2.0`, 7abc99b.
  * https://pypi.org/pypi/alphaengine/json — sha256 83bcfc68; `src/alphaengine/core/validation.py`; tag
    `v2.0.3`, 85c76c6; 31 releases since 2026-07-30.
  * https://pypi.org/pypi/ml4t-diagnostic/json — sha256 f9325095; `evaluation/stats/backtest_overfitting.py`
    and `evaluation/uncertainty.py` under `src/ml4t/diagnostic/`; tag `v0.2.0`, 19e7607; 46 releases since
    2026-01-19.
  * https://pypi.org/pypi/sharpebench/json — sha256 a351a1e4; `sharpebench-edge/src/pbo.rs`; the four
    wheels are `manylinux` x86-64, cp310 to cp313.
* https://raw.githubusercontent.com/esvhd/pypbo/4d723f06498267a2a6280cb9d7d5649348e961d1/pypbo/pbo.py and
  `LICENSE` at that commit — AGPL-3.0. https://pypi.org/pypi/pypbo/json returns 404. Fetched.
* https://numpy.org/doc/2.5/reference/generated/numpy.sort.html — the default `quicksort` is not stable.
  Fetched.
* skfolio 1.12.8, source archive — no PBO or CSCV in its source. Fetched, by search.
* skfolio 1.12.9 at commit 7721090 (tag `v1.12.9`), GitHub source archive, sha256 848e1fee, read
  2026-11-10: `model_selection/_combinatorial.py` and `docs/user_guide/model_selection.rst`. Fetched. Still
  no PBO, CSCV or logit. `CombinatorialPurgedCV` yields training indices and test-fold lists for each of
  C(n_folds, n_test_folds) splits (16 and 8 give 12,870; its cap is 100,000). The first `n_folds - 1`
  blocks hold `T // n_folds` rows and the last holds the rest, where Algorithm 2.3 needs equal blocks, so T
  is trimmed first. It cites López de Prado (2018), with no section (lines 185-186). The book's
  chapter 12 on this splitter was not read (its table of contents is scanned images): UNVERIFIED.
* https://cran.r-project.org/web/packages/pbo/index.html — R `pbo` 1.3.5, 2022-05-26, MIT. Fetched. Its
  source was not read.
* An `upstream-scout` lane found three of the nine (RiskLabAI, backtest-overfitting, trialgate), and
  reported that mlfinlab has none. Its star counts and its GitHub-only repositories were not checked:
  UNVERIFIED.
* Second reader, 2026-11-10 (`evidence-verifier`), the part that concerns this record: the checksums and
  tags hold. It corrected the tie column, the alphaengine, ml4t-diagnostic and factor-qc rows, and the
  claim that every permissive candidate is new or small, which RiskLabAI and ml4t-diagnostic contradict.
* Second reader of the skfolio 1.12.9 re-read, 2026-11-10 (`evidence-verifier`): the lines hold. It
  corrected an option that allowed unequal blocks, against Algorithm 2.3, and two loose wordings.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of ADR 0027 is missing from the two
  records. It corrected an option number that pointed at this record's own list, the options' wording,
  which read as declining ADR 0027's option 2, and a reference to the statistic, which is in ADR 0027.
* The 2026-11-11 skfolio read and the run on 64 and 70 rows are in ADR 0027's Evidence.
  `src/canon_trading/returns_matrix.py`, `returns_matrix` and `session_sharpe`, read the same day for
  what the gate already provides.
* Second reader of the 2026-11-11 re-read (`evidence-verifier`): the call, `returns_matrix` and
  `session_sharpe` hold. It corrected which of Algorithm 2.3's steps skfolio covers and the attribution
  of §12.4, which skfolio does not cite, and added the T ≥ 32 floor, the refit sources and the
  remainder rows of López de Prado's own splitter. The nine-package table was not re-read.
* RiskLabAI 3.2.0, PyPI source archive `risklabai-3.2.0.tar.gz`, sha256 c498676a (matches
  https://pypi.org/pypi/RiskLabAI/3.2.0/json), read 2026-11-11:
  `src/RiskLabAI/backtest/probability_of_backtest_overfitting.py` lines 56-77 and 122-156, and
  `src/RiskLabAI/backtest/backtest_statistics.py` lines 34-56. Fetched. Not installed or run.
