---
status: proposed
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Overfitting probability implementation: nine read, none adopted until one passes this repo's tests

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

Proposed: **option 3**. The implementation is the open gap: nine were read and none is adopted, so a
ranking report carries no PBO yet. The paper's open points (leftover rows, ties, the boundary) are in
ADR 0027.

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

Nothing is adopted, because the implementations have not converged and nothing waits on this:

* The nine differ on leftover rows, ties, the split count and the boundary. No two were shown to agree.
* **trialgate 0.6.0** is closest to the statistic as written in ADR 0027, but has one maintainer, five releases
  since 2026-07-10, and does not declare Python 3.14.
* **RiskLabAI 3.2.0** is the most established, in an organisation's repository. Its blocks are equal only
  if the caller first trims T to a multiple of S, its tie order is unspecified, and it brings statsmodels
  and PyWavelets for one function.
* No adoption signal was verified for any of them. None is in the registry: an entry needs a resolved pin.

What would close the gap, either of:

* One candidate reproduces, in this repo's tests, a hand-computed S = 4 case and the §6 planted-signal
  case: RiskLabAI with T trimmed, or trialgate once it declares Python 3.14 and has a longer history or
  a second maintainer. It then goes through `adopt-upstream`.
* The owner decides that Algorithm 2.3 is a documented contract we may implement (ADR 0027, option 3). The same two
  test cases apply, plus agreement with a read package on a shared matrix.

Who decided. Adopting nothing now was chosen by the orchestrator on 2026-11-10 under the owner's
delegation quoted in ADR 0010's Evidence.

### Consequences

* Good: the candidates are read at pinned source instead of listed from summaries.
* Bad: a ranking report has no overfitting diagnostic until an implementation exists.

### Confirmation

* No PBO package is in `pyproject.toml`, and none is imported.
* When one is adopted, a test reproduces a hand-computed S = 4 case, and a ranking report states S and PBO
  beside the DSR.

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
* https://cran.r-project.org/web/packages/pbo/index.html — R `pbo` 1.3.5, 2022-05-26, MIT. Fetched. Its
  source was not read.
* An `upstream-scout` lane found three of the nine (RiskLabAI, backtest-overfitting, trialgate), and
  reported that mlfinlab has none. Its star counts and its GitHub-only repositories were not checked:
  UNVERIFIED.
* Second reader, 2026-11-10 (`evidence-verifier`), the part that concerns this record: the checksums and
  tags hold. It corrected the tie column, the alphaengine, ml4t-diagnostic and factor-qc rows, and the
  claim that every permissive candidate is new or small, which RiskLabAI and ml4t-diagnostic contradict.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of ADR 0027 is missing from the two
  records. It corrected an option number that pointed at this record's own list, the options' wording,
  which read as declining ADR 0027's option 2, and a reference to the statistic, which is in ADR 0027.
