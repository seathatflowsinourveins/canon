---
status: accepted
date: 2026-11-10
decision-makers: coordinator (Claude), under the owner's delegation of 2026-11-10; repo owner ratifies
review-by: 2026-12-10
---

# Reviewer benchmark: past commits with confirmed defects, one brief, found-or-missed scoring

## Context and Problem Statement

ADR 0005 adopts no second provider for runtime workers until "an eval on our own tasks" shows it wins,
and the README lists "No eval of our own". Reviews here now come from more than one model family: ADR
0043 adds a Codex reader on one trial ("One trial is one data point"). What is the smallest benchmark
that can ground which reviewer reads what, and what can it not show?

## Decision Drivers

* Use the practice of published review benchmarks and the vendors' eval guidance; invent no metric.
* A defect counts only when a fix on `main` and its regression test show it.
* Reviews are not free: the set stays small, so its limits must be stated with it.

## Considered Options

1. Reuse a public review benchmark.
2. Our own cases: past commits with later-confirmed defects, every reviewer given the same brief.
3. As 2, but each reviewer in its production form (`/code-review`, Codex's review command).

## Decision Outcome

Chosen: **option 2.** Option 1 measures other people's code, and the published ones are mostly run by
vendors whose own tools rank well on them. Option 3 gives each reviewer different wording, so a gap in
score could come from the prompt rather than the reviewer; it is possible later work, not this benchmark.

1. **A case** is one commit to review, the diff from its parent, as Greptile re-opens the change that
   introduced a bug traced from its fix. Its file in `evals/review/cases/` names the commit, the paths,
   and each confirmed defect: where it is, what it does, a `match` line (the least a finding must say),
   the fix on `main` and the regression test the fix adds or changes, as c-CRAB grounds review in a
   failing test. That the test failed or hung before the fix rests on the fix's message. A *clean* case
   has no confirmed defect; a *pending* case says what would confirm its defects and is not scored.
   Format and check: `evals/review/README.md`, `tests/test_review_cases.py`.
2. **Not shown to the reviewer**: the fix, any later commit, the case file. Its workspace is new and
   detached at the case's commit, and the brief forbids reading later history. Git still holds that
   history, so this rests on the brief; Anthropic reports a model that gained an unfair advantage by
   reading git history left by earlier trials.
3. **A reviewer** is a runtime, model and effort, given `evals/review/brief.md` word for word as its whole
   task, with only the commit, the paths and the report path filled in. It may run the offline tests.
4. **Scoring.** A confirmed defect is **found** if one finding names its location and says what its
   `match` line asks, as Greptile counts a bug only when a comment names the faulty code and its impact;
   else **missed**. Every other finding is **unconfirmed**, counted and listed, not called false: Tenki
   counts such findings as false positives and concedes some are real, and c-CRAB leaves them
   unevaluated. No precision is computed. A finding confirmed later becomes a defect for later scorings.
5. **Two scorers** work apart, the scoring task and a second reader it starts, as Tenki has several
   judges vote. The sheet lists every disagreement and the share of marks agreed (c-CRAB reports such a
   share for its two authors, on another judgement); an unsettled mark counts as missed, as disputed.
   Both are models, uncalibrated against people as Anthropic and OpenAI ask of a model grader: until a
   person has marked a sheet, agreement shows consistency, not correctness.
6. **Runs.** One per reviewer per case by default; the procedure takes a `runs` count. A sheet from
   single runs says it measures no variance between runs (Anthropic recommends several trials).
7. **What the set can show.** For n defects, taken as independent, a found-rate p has a standard error
   of about √(p(1−p)/n). The first run scored 10 defects, in three cases, 8 of them in one: at p = 0.5 that is about ±31 points per reviewer at 95% and, unpaired, about ±44
   on a difference; the 21 confirmed now (10 in `order-modify`, 9 in `trial-ledger-gate`) give about ±22 and ±31. Pairing narrows that only as far as the
   reviewers' results correlate; defects bunched in one case widen it (clustering tripled the standard
   error on one of the paper's sets, DROP). The paper's worked example needs about 1,000 questions for 3
   points, paired, at 80% power, on parameters it calls fictional. **A dozen cases rank nothing
   finely.** The set can show a reviewer missing most of what another finds, or claiming defects on
   clean cases; it cannot order close reviewers. Anthropic's "20-50 simple tasks drawn from real
   failures" is advice for starting an eval, not for comparing systems.
8. **The folder** is `evals/review/`, added to ADR 0012's table: `cases/`, `brief.md`, the procedure in
   `README.md`, and `runs/<date>-<label>/` for reports and score sheets, committed by the scoring task.

First cases: `exec-client-first-slice` (`12f19a0`: a startup-reconciliation race in a test helper, fixed
in `05899f8`), `data-client-hardening` (`f364348`: a log read before the writer thread flushed, fixed in
`2669c86`), `trial-ledger-gate` (`e21935c`: eight defects the gate fix `828c082` confirms with its
regression tests; the review's other findings stay unconfirmed), and two clean ones, `cancel-all`
(`4afa6a7`) and `stop-orders` (`724246e`). Ready since the first run: `order-modify` (`3a00f61`: ten
defects `52ac69c` confirms). Pending, their fixes not on `main`: `ema-cross-paper-node` (`7ce55cc`),
`paper-testers` (`273ff52`). The missing HTTP timeout
fixed in `5575460` is in no case, because each case commit's own module header states it as a limit.

### Consequences

* Good: each defect has a landed fix and a regression test; the rule is mechanical enough to compare
  two scorers.
* Bad: a clean case means none was confirmed, not that none exists; the brief, not the checkout, hides
  later history; few cases, so most differences are noise.
* Bad: the defects come from reviews. The modify defects are the findings of second readers on Codex,
  Muse and Antigravity (ADR 0043, ADR 0045), which favours those reviewers by construction; the sheet
  scores a reviewer's own finds apart.

### Confirmation

`tests/test_review_cases.py`, in `scripts/ci`, fails unless every case is well formed and each fix
exists, follows its case's commit, is on `HEAD`'s history, and changes and defines the named test. A
score sheet under `evals/review/runs/` follows `evals/review/README.md`. The first run,
`runs/2026-11-10-first/`, gave four reviewers the five ready cases once each: 20 of 20 reports, found
1, 1, 2 and 1 of 10, two scorers agreeing on 40 of 40 marks; it ranks none. The second readers of
`3a00f61` read it before the brief existed.

A second reader checked this record on 2026-11-10 (`evidence-verifier`).

## Evidence

Read 2026-11-10, by a research subagent that fetched each page and summarised it ("lane"), unless marked.

* https://www.greptile.com/benchmarks — 50 real bug-fix PRs from five repositories; the bug-introducing
  commit traced from the fix and re-opened; a bug counts only if a line comment names the faulty code
  and its impact; false positives not scored; whether the fix is hidden is not stated (lane).
* https://tenki.cloud/blog/benchmarking-ai-code-reviewers — Greptile's PRs "replayed in reverse"; three
  LLM judges vote; unmatched comments are false positives, some admitted real; 7 critical bugs called too
  few to rank (lane).
* https://www.qodo.ai/blog/how-we-built-a-real-world-benchmark-for-ai-code-review/ — injected defects,
  LLM judge on description, file and line; precision, recall, F1 (lane).
* https://arxiv.org/html/2603.23448v3 (c-CRAB) — ground truth as tests that fail on the original patch;
  other comments not evaluated; 84% agreement between two authors on whether a test captures a
  review comment (lane; scope corrected by the second reader).
* https://github.com/withmartian/code-review-benchmark — golden comments; an LLM judge asks whether two
  comments "describe the same underlying issue" (lane).
* https://arxiv.org/html/2411.00640 ("Adding Error Bars to Evals") — SE √(p(1−p)/n); paired differences;
  clustered SE 3.05× the naive one on DROP, 1.88× and 1.10× elsewhere; about 969 questions for 3 points
  under the stated assumptions (lane; second reader).
* https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents — multiple trials; calibrate LLM
  judges against experts; isolate trials, the git-history example; "20-50 simple tasks" (lane).
* https://platform.claude.com/docs/en/test-and-evaluate/develop-tests and
  https://developers.openai.com/api/docs/guides/evaluation-best-practices — grade by code, model or
  person; Anthropic: "Test to ensure reliability first, then scale"; OpenAI: validate a judge against
  human labels; judges' position and verbosity bias (lane; Anthropic page read in full by the second
  reader).
* This repository: commits `05899f8`, `2669c86` and `5575460`, the headers of `execution.py` at
  `12f19a0` and `data.py` at `f364348` ("alpaca-py sets no HTTP timeout"), and the tests the case
  files name (read; checked by `tests/test_review_cases.py`); the fifteen gate findings and the modify
  findings from their reviews' reports (read; not re-run, not in this repository).
* Second reader, 2026-11-10 (`evidence-verifier`): the benchmark descriptions held; it corrected the
  clustering, sample-size and c-CRAB statements, the Anthropic quote and the Context, and had the
  scorers' and the defects' sources stated as limits; all above.
* 2026-11-10, after the first run: the case count, the modify defects' sources and the Confirmation now
  state the run (`evals/review/runs/2026-11-10-first/scores.md`) and `order-modify` made ready.
* Not found first-hand: Cohen's kappa or McNemar's test used for review scoring, and Defects4J,
  BugsInPy or SWE-bench used as a review benchmark (UNVERIFIED).
