---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2027-02-09
---

# Overfitting probability: a diagnostic beside the Sharpe gate, never a pass or fail

## Context and Problem Statement

ADR 0010 gates a ranking on the deflated Sharpe ratio (DSR). The probability of backtest overfitting (PBO),
estimated by combinatorially symmetric cross-validation (CSCV), asks something else: how often the variant
that was best in-sample lands at or below the median out-of-sample. What role does it have here, and which
code computes it?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: every dependency traces to a pinned upstream, and where sources have not converged we wait.

## Considered Options

1. A hard gate, at the paper's "customary" 0.05 or at another level.
2. A diagnostic reported beside the DSR gate, computed by an adopted package.
3. A diagnostic computed by our own code for Algorithm 2.3.

## Decision Outcome

Accepted: **option 2** for the role: a diagnostic. No PBO level passes or rejects a pick. The
implementation is ADR 0032's: skfolio, already adopted, makes the halvings and the Sharpe ratios, and the
last steps of Algorithm 2.3 are glue here (option 3 for those steps only), because no package read does
them as this record settles them.

Why not a gate:

* The paper's rule rejects above 0.05 (§3.1). Its own planted-signal example, which it calls a valid
  backtest, scores 13%, and its pure-noise example 55% (§6). The rule would reject the valid one.
* Its error bar, below 0.0045 for S = 16, is the formula for independent draws, applied to splits that
  share rows (§4). A high PBO can also arise among strategies that are all skilful (§5.2).
* No package uses 0.05 (ADR 0032's Evidence). Four flag above 0.5; ml4t-diagnostic labels bands at 0.10,
  0.30 and 0.50.

The statistic (Algorithm 2.3, §3.1). Take the T×N matrix of ADR 0010, step 2. Cut it into S = 16 equal row
blocks. For each of the C(16,8) = 12,870 halvings, take the in-sample best and its out-of-sample rank r,
with N for the best; ω = r/(N+1), λ = ln(ω/(1−ω)), and PBO is the share of λ ≤ 0. Use it only when N ≥ 20
(the paper asks N ≫ 10, §4; 20 is our extrapolation), never as a search objective (§5.2). The paper does
not say what to do with T mod S leftover rows or with tied ranks, and Definition 2.2 writes the boundary as
strictly below the median where §3.1 integrates up to λ = 0.

Settled here, each against the pick (the first Decision Driver): the earliest T mod 16 rows are dropped,
keeping the most recent; a configuration tied out-of-sample with the pick ranks above it; and λ ≤ 0
counts as overfit. Floors: N ≥ 20 and T ≥ 32, skfolio's splitter's least for 16 blocks. Below a floor
the report says "not computable: N = …, T = …" and prints no number.

Who decided. The role was chosen by the orchestrator on 2026-11-10 under the owner's delegation quoted in
ADR 0010's Evidence. The three choices, the floors and acceptance were decided by the lead on 2026-11-11
under ADR 0074, for the extreme-movers study, whose adoption test needs the diagnostic before its replay
study.

### Consequences

* Good: the role is settled.
* Good: nothing waits on PBO. ADR 0010's protocol is complete without it.
* Bad: a ranking report has no overfitting diagnostic until ADR 0032's code lands.

### Confirmation

* When ADR 0032's code lands, a ranking report states S, N, T and PBO beside the DSR, or "not
  computable" below a floor; its tests are in ADR 0032.
* Re-read 2026-11-10 against skfolio 1.12.9: the role holds and nothing in skfolio changes it. Its
  `CombinatorialPurgedCV` splits rows but computes no PBO; ADR 0032 has the detail.
* Re-read 2026-11-11 at the same pin, for the extreme-movers study: the role holds. skfolio still
  computes no PBO. Its `CombinatorialPurgedCV(n_folds=16, n_test_folds=8)`, with no purge or embargo,
  yields exactly the 12,870 halvings of Algorithm 2.3's third step, each with its complement, in equal
  halves once T is a multiple of 16 (ADR 0032 has the call). The open points were settled the same day,
  in the Decision Outcome.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source. The lines on the nine
packages, numpy's sort, skfolio, R's `pbo` and the scout's search moved to ADR 0032 when it was split from
this record on 2026-11-10, with no change of decision; the paper line stays here and ADR 0032 points to
it, and the second reader's line keeps only what concerns this record.

* https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf — the PBO paper, revised 2015-02, sha256 d8bfbada.
  Read in full as extracted text: Definition 2.2, Algorithm 2.3 and §§3–6. Fetched. Its figures were not
  viewed. "Independent draws" is our reading of its binomial formula.
  * Slips: it prints C(16,8) as "12,780"; it gives one PBO as both 4% and 0.04%; step c names the testing
    set where it means the training set; §7 says "mean" where §3 says "median"; §6 gives one Sharpe ratio
    as both 1.27 and 1.3.
  * Not read: SSRN 2840838, or the version in *J. Computational Finance* 20(4), 2017.
* https://www.mdpi.com/2227-9091/9/1/18 — Witzany, *Risks* 9(1):18 (2021). MDPI returns 403; the
  published version was read 2026-11-10 at
  https://www.econstor.eu/bitstream/10419/258108/1/risks-09-00018-v3.pdf. Fetched. Its Conclusions find
  CSCV biased both ways: negatively when every strategy's mean return is near zero, overoptimistic when one
  stands well above the others.
* Second reader, 2026-11-10 (`evidence-verifier`), the part that concerns this record: the paper claims and
  thresholds hold.
* The package sources behind "No package uses 0.05" are read in ADR 0032's Evidence.
* skfolio 1.12.9 at the pinned commit 7721090 (tag `v1.12.9`), GitHub source archive, sha256 848e1fee.
  Fetched 2026-11-10. `src/skfolio/model_selection/_combinatorial.py` is its only splitter over time
  blocks, and nothing in the tree names PBO, CSCV or an overfitting probability. Settles none of the
  open points above (leftover rows, ties, the boundary). The details are in ADR 0032's Evidence.
* Second reader of the skfolio re-read, 2026-11-10 (`evidence-verifier`): the paper's claims and slips hold
  on a fresh extraction (sha256 d8bfbada). It read Witzany at EconStor and narrowed the skfolio wording.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of this record is missing from it
  and ADR 0032 together. It corrected the pointer for the package thresholds, which are in ADR 0032's
  Evidence, and kept the ranking report's line in Confirmation here.
* skfolio 1.12.9 at commit 7721090, read again 2026-11-11 from the pinned source:
  `model_selection/_combinatorial.py` lines 71-190 (the class and its parameters), 259-264
  (`test_set_index`, every combination of `n_test_folds` of `n_folds`) and 349-358 (blocks of
  `n_samples // n_folds` rows, the last taking the rest). A case-insensitive search of `src/` finds no
  PBO and no CSCV; "overfit" appears only in the deflated Sharpe ratio's reference title
  (`measures/_measures.py` lines 1315 and 1478) and in warnings about overfitting risk
  (`optimization/hierarchical/_clustering/_nco.py` line 97, `optimization/ensemble/_stacking.py` lines
  71 and 87).
  Run here with the locked 1.12.9 on zero matrices of 64 and 70 rows: 12,870 distinct splits, each a
  partition of the rows whose complement is also a split; 32 and 32 rows at T = 64, 32 and 38 at T = 70.
* Second reader of the 2026-11-11 re-read (`evidence-verifier`): the paper's claims, slips and open points
  hold on a fresh download (sha256 d8bfbada), the paper has no purging or embargo, and the skfolio lines and
  the run reproduce. It corrected the search scope and the step the halvings belong to.
