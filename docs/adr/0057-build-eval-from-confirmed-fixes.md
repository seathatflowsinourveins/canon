---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10, the direction); orchestrator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Build eval: a fix's parent tree and its failing tests, graded by tests and a reader, to keep or retire a worker that builds here

## Context and Problem Statement

ADR 0043 and ADR 0045 let other runtimes read a change and report, never build, and the one eval here
is for reviewers (ADR 0046). The owner's direction of 2026-11-10 is that more runtimes become workers
under one orchestrator, that only those that measure up are kept, and that a runtime may be measured in
more than one configuration; work is not gated on the measure (ADR 0058). What must a worker show to
go on building here, and how is that measured on a repository this small?

## Decision Drivers

* Take the construction and grading the maintained coding benchmarks converge on; invent no metric.
* Tests pass on changes nobody would merge, so a reader is needed, and ADR 0017's review is one.
* A handful of cases ranks nothing; the result is read only as far as it can bear.
* These runtimes start with no sandbox and no approvals (ADR 0043), so a term holds only where breaking
  it is harmless or seen.

## Considered Options

1. No build eval: keep a runtime on the owner's word or its reviewer benchmark score.
2. A public benchmark's score for the runtime and model.
3. Our own cases from confirmed fixes, the tests hidden from the worker, as the public benchmarks do.
4. As 3, but the fix's failing tests are in the worker's tree, and a grader outside its reach and a
   reader decide.
5. A container harness (Harbor) running each runtime on our cases.

## Decision Outcome

Chosen: **option 4.** Finding defects is not fixing them (1). A public score is of other code, and of
a scaffold that is not this repository's rules and cockpit (2). Hidden tests here call the fix's own
helpers, so they would fail correct fixes: the "overly narrow tests" SWE-Bench Pro Verified corrected
by hand, where this record shows them instead (3). Harbor's task is "an instruction, environment, and
test script" for an environment it runs; what is to be measured is a runtime as the cockpit starts it
on the owner's sign-in (5; not tried).

1. **A case** is a fix on `main` that confirmed a defect (ADR 0046). The worker's tree is the fix's
   parent with the fix's test files, as SWE-bench "appl[ies] the PR's test content" before the rest.
   Tests that fail there and pass after are its fail-to-pass set; every other test must keep passing.
   A case is admitted when three runs of both trees show exactly that, as SWE-bench-Live keeps "only
   instances with consistent results across all runs". A fix that cannot make a sound case is left
   out with its reason. Four are ready and three left out (`evals/build/README.md`).
2. **The tests are shown.** This departs from the benchmarks, which hold them back; SWE-smith alone
   leaves them "visible and runnable". It is this repository's own task: a finding is reproduced "by a
   failing test" and then fixed (ADR 0045). The cost is a low mechanical bar, the shape Anthropic gives
   a regression eval ("nearly 100%"), and a test that can be satisfied without a fix: rules 4 and 5.
3. **Withheld by construction**: the fix and all history. The tree is one commit with no remote, as
   SWE-Bench Pro Verified rebuilt each task "as a fresh single-commit repository", having seen an agent
   read the fix with `git log --all`; `evals/` is left out, and the statement says what is wrong, never how
   it was fixed. **Not fenced**: the public repository, other checkouts on the machine, a runtime's
   memory, an earlier review of the fix, training data. The brief forbids the first two and the
   transcript is read afterwards; METR found such an instruction had "nearly negligible effect" (one
   task, one model), so a breach voids the run and counts as a broken term. The README says how the rest is recorded.
4. **The mechanical grade** is made out of the worker's reach ("Isolate the agent from the evaluator",
   Berkeley RDI): the harness makes the tree again, takes only the worker's files, puts every starting
   test file and `pytest.toml` back, and runs the suite before the change and after it, then
   `scripts/ci`'s three code checks. Resolved: the fail-to-pass tests pass, nothing fails, no test that
   passed before is skipped or gone, the checks pass. The fix itself must resolve; a change that fixes
   nothing must not.
5. **A reader then decides whether it would land.** METR found "roughly half" of test-passing
   SWE-bench Verified changes would not be merged. Two readers apart, not told the worker, mark each
   resolved run `land`, `fix` or `no`; `no` needs a named blocker, as FrontierCode's "hard stops during
   code review". The first reading is ADR 0017's `/code-review`. Both readers are models, uncalibrated
   against a person (ADR 0046 rule 5).
6. **A worker** is a runtime, model, effort and configuration: `stock`, or a named set of skills and
   plugins, each adopted through `adopt-upstream` first. Stock and enhanced runs of one runtime sit
   side by side as separate workers. Three runs per case: keeping a worker asks for consistency,
   tau-bench's pass^k, not one success in k.
7. **What four cases can show.** The case is the unit; runs of one case are not independent. Telling a
   worker that resolves 90% of cases from one at 50% takes about 17 cases, 90% from 70% about 60 (two
   independent rates; pairing on the same cases needs fewer, by how far the workers' results agree).
   Four show all against none. So the bar counts single facts, not rates.
8. **Keep or retire.** Work is not gated on a trial: an adopted runtime may build before its label has
   one (ADR 0058). A worker is retired by any one of: a term broken in any run, or once in real work; a
   case it does not resolve in every run that the reference worker (the Claude build lane, at the
   effort ADR 0005 gives a hard change) resolves in every run; a run both readers mark `no`; or, of any
   five changes in a row it builds, two that are abandoned or take a third review round. Otherwise it
   is kept. No source sets such a bar; these levels are this record's, and **the first run sets
   them**: it runs the reference beside the others, and if the reference fails the bar, the cases or
   the brief are at fault and no verdict is given. A failed trial is not repeated unchanged, since
   repeating until it passes turns pass^k into pass@k; a new model, effort, configuration or runtime
   version is a new worker and a new trial, so a retired runtime can return in another configuration.
   The question is asked again at each `review-by`, on the workers' records and refreshed cases. The
   same pruning applies to readers on ADR 0046's benchmark, whose retire rule that record has yet to
   state.
   *Superseded in part 2026-11-11 by ADR 0079:* the levels are set after three counted rounds, relative
   to the Claude Opus worker on the same cases, not by the first run; no runtime is retired or promoted
   on one wave. A broken term still retires at once.
9. **The terms a worker builds under** are ADR 0058's: its own workspace, a task that names its paths,
   no landing, a Claude lane or the coordinator checking, reviewing and landing. A trial runs under the
   brief's terms, which are stricter.
10. **The folder** is `evals/build/`: `cases/`, `brief.md`, `harness.py`, `README.md`, and `runs/` once
    one exists (ADR 0012's table).

### Consequences

* Good: the fail-to-pass and pass-to-pass sets are shown by running, not taken from a commit message;
  stock and enhanced configurations compare on the same cases; a trial's tree has no remote, so a push
  from inside it reaches nothing (`gh` or another checkout still would, which the transcript check
  looks for).
* Bad: four cases from two modules, all on paths ADR 0058 recommends leaving with Claude, so a trial
  may be harder than the work a worker is given and says little about that work. Keeping or retiring
  rests on a few observed facts and on the record of real changes, not on a measured rate.
* Bad: the fence against reading the fix is a brief and a transcript check. The reader is a Claude
  model reading, among others, Claude's changes.
* Leaving: delete the folder, its test and the row in ADR 0012; mark this superseded.

### Confirmation

`tests/test_build_cases.py`, in `scripts/ci`, checks every case file and the harness on a scratch
repository. `harness.py admit` showed each ready case sound on three runs on 2026-11-10 (the case's
`admitted`): two of them before the code review's changes to the harness, when `admit` did not yet ask
that the listed tests pass on the fix, and two after. For the first two, a tree holding the fix's own
files then graded resolved, and for one a change that fixes nothing graded unresolved with its four
tests failed. The first trial's first wave ran from 03:17 to 04:14 UTC on 2026-11-11 (the trial is
labelled 2026-11-10), each worker in its tree as a folder of its own; how the cockpit started it is not
recorded (UNVERIFIED). It is scored in `evals/build/runs/2026-11-10-first/scores.md`: all sixteen runs
resolved; settled marks 3 `land`, 8 `fix`, 5 `no` (one disputed); the readers agreed on 9 of 16 marks
before rereading. Waves 2 and 3 are scored in `scores-waves-2-3.md` beside it, and `scores.md` joins
the three waves; the reference worker fails rule 8's bar in every wave, so that sheet gives no
verdict, and the levels are ADR 0079's to state.

## Evidence

Read 2026-11-10. `fetched`: downloaded and searched for the words quoted. "Lane": a research subagent
fetched the page and a model summarised it.

* https://arxiv.org/html/2310.06770v3 (SWE-bench) — "we apply the PR's test content, and log the
  associated test results before and after the PR's other content is applied"; instances without a
  fail-to-pass test filtered out; `FAIL_TO_PASS`, `PASS_TO_PASS` (fetched).
* https://arxiv.org/html/2505.23419 (SWE-bench-Live) — the two transitions; the consistency quote (fetched).
* https://arxiv.org/html/2609.08149 (SWE-Bench Pro Verified, preprint) — the single-commit quote; "block
  online sources of target commits"; a trajectory using `git log --all`; "Overly narrow tests" (fetched).
* https://arxiv.org/html/2504.21798 (SWE-smith) — "All FAIL_TO_PASS bugs are visible and runnable in the
  repository at inference time"; it builds training data, not an eval (fetched).
* https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/ — the isolation quote; "A conftest.py file
  with 10 lines of Python 'resolves' every instance on SWE-bench Verified"; "Run a null agent" (fetched).
* https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/ — the
  "roughly half" finding, in a note METR calls unpolished (fetched).
  https://metr.org/blog/2025-06-05-recent-reward-hacking/ — the "negligible effect" quote (fetched).
* https://cognition.com/blog/frontier-code — "A solution passes if it clears all blocker criteria, i.e.,
  criteria that a maintainer would consider hard stops during code review"; five runs each (fetched).
* https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents — regression evals "should have a
  nearly 100% pass rate"; "pass^k measures the probability that all k trials succeed"; "grade what the
  agent produced, not the path it took"; an advantage gained from "the git history from previous
  trials"; "20-50 simple tasks drawn from real failures is a great start" (fetched).
* https://arxiv.org/html/2406.12045 (tau-bench) — pass^k against pass@k (fetched).
  https://arxiv.org/html/2411.00640 — the standard errors; paired differences recommended; clustered
  questions (fetched). The 17 and 60 are computed here from them, for two independent rates.
* https://docs.harborframework.com/tasks/overview — the Harbor quote (fetched); not installed or run.
* No published entry bar for an agent's write access was found (lane): rule 8's levels are ours.
* Second reader, 2026-11-10 (`evidence-verifier`): every quotation held, and the two case counts. It
  had the scope of both METR sources, what SWE-Bench Pro Verified did about narrow tests, and the
  pairing stated, and found a left-out reason and a pre-trial check that were wrong; all corrected. It
  read this record before the owner's decision that work is not gated, which changed rules 8 and 9.
* This repository: the seven fixes, their tests and messages (read); each ready case's start and
  reference trees run by `harness.py admit` (run). `orca --help` lists `repo add --path <path> [--kind git|folder]`
  (bundled; untried).
