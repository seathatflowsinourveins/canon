---
status: proposed
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Overfitting probability: a diagnostic beside the Sharpe gate, with no implementation adopted yet

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

Proposed: **option 2**. The role is decided: a diagnostic. No PBO level passes or rejects a pick. The
implementation is the open gap: nine were read and none is adopted, so a ranking report carries no PBO yet.
ADR 0032 holds the nine, what each does, and what would close the gap.

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

Who decided. The role was chosen by the orchestrator on 2026-11-10 under the owner's delegation quoted in
ADR 0010's Evidence; ADR 0032 holds adopting nothing now.

### Consequences

* Good: the role is settled.
* Good: nothing waits on PBO. ADR 0010's protocol is complete without it.
* Bad: a ranking report has no overfitting diagnostic until an implementation exists (ADR 0032).

### Confirmation

* When an implementation is adopted, a ranking report states S and PBO beside the DSR.
* What confirms that none is adopted yet, and the test an adoption needs, are in ADR 0032.

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
* https://www.mdpi.com/2227-9091/9/1/18 — Witzany, *Risks* 9(1):18 (2021), reported to find CSCV biased in
  both directions. Returned 403 today; read in a browser on 2026-11-09. UNVERIFIED this session.
* Second reader, 2026-11-10 (`evidence-verifier`), the part that concerns this record: the paper claims and
  thresholds hold.
* The package sources behind "No package uses 0.05" are read in ADR 0032's Evidence.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of this record is missing from it
  and ADR 0032 together. It corrected the pointer for the package thresholds, which are in ADR 0032's
  Evidence, and kept the ranking report's line in Confirmation here.
