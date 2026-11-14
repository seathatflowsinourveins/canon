---
status: accepted
date: 2026-11-13
decision-makers: the owner (approved 2026-11-13); the lead (2026-11-13), under the owner's delegation (ADR 0074); Claude for the record
review-by: 2026-12-13
---

# A landing runs the quick checks and a secret scan of the history here, and the full suite once on GitHub

## Context and Problem Statement

ADR 0138 shortened the commit hook but left a full local suite at landing, followed by another full
suite on GitHub. The lead reports about 15 minutes locally and 18-26 minutes on GitHub per landing
on 2026-11-13, with three local landing runs failing that day, one on a real defect and two because of
load and a damaged environment; load reached 40-54 with three local runs (measurements supplied in the
assignment; their logs were not read here, so independently UNVERIFIED). The lead's decision is to remove
the repeated full local landing run after today's finish line, while keeping the existing merge gate.
The owner approved this on 2026-11-13 and asked that landings run at full speed: the repository is public,
so its hosted runs are not a limit to plan around.

## Decision Drivers

* A full suite must pass before GitHub merges the checked commit.
* The local landing should catch the quick failures without competing with the trading work for a full
  suite's worth of machine time.
* Keep the address check, review requirement, commit-specific statuses and squash-merge wait as they are.
* Use the quick mode already implemented in `scripts/ci`, with no passed-tree cache or new dependency.

## Considered Options

1. Keep ADR 0138's full local landing run and the hosted full run.
2. Follow gh-signoff's local-only practice: run the full suite locally and rely on its signoff.
3. Run the existing quick set locally at landing, with the full suite in the existing required hosted run.

## Decision Outcome

Chosen: **option 3**, on the lead's instruction and the owner's approval of 2026-11-13. It applies to
landings started after it is on `main`; it changes no landing already in flight.

1. **Local landing scope.** `scripts/land` still invokes `scripts/ci --landing`, but that mode now runs
   the quick set the commit hook runs (the lock and environment checks, ruff, format, ty, test collection,
   canon lint and shell checks across the tree, including the separate projects' lock, environment, ty and
   collection checks) and, in place of the staged-only scan, gitleaks over the whole git history and the
   working tree, and on macOS `scripts/test-lanes` and `scripts/test-maintenance`, whose macOS-only cases
   no hosted job runs (see Consequences). Landing refuses a dirty tree, so these see the whole committed branch, and nothing the
   push would publish goes out unscanned for secrets. Each other check is a skip line naming the hosted
   required check, and the verdict line says how many were left to it. `--landing` keeps the CI slot
   reserved for landings.
2. **No origin, no hosted run.** With no `origin` remote there is no hosted run to leave anything to, so
   `scripts/land` runs the whole of `scripts/ci` before it fast-forwards `main`.
3. **Hosted scope.** The existing `checks` job runs full `scripts/ci`; its aggregating
   `all required checks passed` job must succeed before merge, and the live ruleset on `main` requires
   it and `signoff` (read 2026-11-13). `scripts/land` already fails the land when a required check fails
   or is cancelled during its wait, and asks for auto-merge otherwise, which GitHub only completes once
   the required checks pass. "Once" means the full required validation of the pull request is hosted,
   with no full local copy at landing. The workflow also runs on pushes to `main`; that trigger and the
   reporting macOS job stay as they are.
4. **Statuses and merge.** The `review` status keeps the same note and precedes `signoff`. The `signoff`
   context keeps its name; its description becomes
   `scripts/ci --landing (quick checks, history secret scan, macOS self-tests on macOS) passed locally`,
   attached to the checked
   commit, so the status says what ran here. The wait for the merge becomes 45 minutes
   (`LAND_WAIT_SECONDS`, default 2700), above the `checks` job's timeout, raised from 30 to 40 minutes
   because the full suite now runs only there and its two latest passing runs on `main` took 22 min 23 s
   and 22 min 31 s (read 2026-11-13 by the evidence-verifier from the workflow's run history), so a landing whose
   hosted run finishes ends with `main` updated; queueing beyond that still ends in an unconfirmed merge,
   reported as before. Amended 2026-11-14, on the lead's order: the `checks` job's timeout is 90 minutes
   and the wait 95 (`LAND_WAIT_SECONDS`, default 5700), still above it. In a day of about 25 landings the
   hosted suite grew from about 22 to about 36 minutes: passing runs on 2026-11-14 took 23-38 minutes
   created to updated (on `main` 43617194822 33.9 and 43614583851 36.5, the `checks` job 33 min 48 s and
   36 min 21 s; pull request run 43579294655 38.4, its job 38 min 19 s), and `main`'s push run 43621264808
   and pull request run 43621201927 were cancelled at the 40-minute timeout while pytest ran. About 20 more
   landings are expected, so the limit leaves room for further growth. Amended again the same day, when
   the check ran in parts (the amendment of 2026-11-14 below): each part's limit is 30 minutes and the wait
   45 (`LAND_WAIT_SECONDS`, default 2700), still above it. The review gate,
   address check, branch rebase/merge rule, exact-head squash-merge arguments, subject and body stay as
   they are. Repository settings, rulesets and merge queues are untouched.
5. **The lane's tests.** A lane still runs its unit's own tests before landing, and one full pytest when
   it adds a test module. Quick checks and test collection do not replace those runs.
6. **Records amended.** This supersedes in part ADR 0138, whose drivers kept a full local suite at
   landing: its commit scope stands, its landing scope is replaced here, and its unrecorded timing
   comparison stays UNVERIFIED. It amends ADR 0013's rule 1 (what `--landing` runs) and ADR 0014's rules
   2 and 5 (the local pass and the wait). ADR 0014's rejection of a merge queue is unchanged, and GitHub's
   own terms still exclude one here: merge queues are for public repositories owned by an organisation,
   and this repository's owner is a user account.

Option 1 keeps the measured duplicate work. Option 2 would remove the independently required hosted
check; that is outside the lead's decision. Option 3 keeps the existing hosted enforcement and changes
only the local scope and the text that attests to it.

### Consequences

* Good: the local landing no longer runs the full suite that GitHub runs before merging. The anticipated
  saving is the reported approximately 15-minute local pass, not a new measured speed-up.
* Cost recorded today: about 15 minutes locally plus 18-26 minutes on GitHub per landing; three local
  landing runs failed, one real and two not (load, a damaged environment); load was 40-54 with three
  local runs. These are the lead's 2026-11-13 measurements, independently UNVERIFIED here. ADR 0138
  records two hosted runs of 18 min 46 s and 19 min 09 s.
* Bad: a failure outside the quick set first appears on GitHub after the push. The lane's own tests
  remain necessary.
* Cost kept: the self-test cases that need macOS (in `scripts/test-lanes` and `scripts/test-maintenance`)
  skip on the hosted Ubuntu runner, and the workflow's macOS job runs none of them while its image lacks
  `/usr/bin/timeout` (`.github/workflows/ci.yml`, ADR 0082). So `--landing` on macOS still runs those two
  scripts in full; they are the slowest part of the local landing that remains. A landing from Linux
  skips them and does not block on them.
* GitHub documents 20 concurrent standard hosted jobs on Free, with at most 5 macOS jobs, and cancels a
  job past a limit; these are limits, not a guarantee of immediate starts. Actual
  account usage and runner contention are UNVERIFIED.

### Confirmation

* `scripts/test-ci`: `--landing` runs the quick set, and on macOS (`CANON_CI_OS=Darwin`) `test-lanes` and
  `test-maintenance` and no other test script, scans the whole history and the working tree and no
  staged diff, runs no self-test and no full pytest, and says what it left to the hosted run; a plain run
  still runs everything. 396 passed on 2026-11-13; against the previous scripts/ci, the macOS landing case fails 3.
* `scripts/test-land`, red against the base landing implementation: exactly one `scripts/ci --landing`
  call on a clean multi-commit branch with `origin` (including the `--pr` and dry-run paths), one plain
  `scripts/ci` call with no `origin`; signoff names what ran and both statuses name the checked commit.
  The same tests retain refusals for a missing `--reviewed` note and failed local checks before any status
  or merge. 129 passed on 2026-11-13.
* `scripts/test-land-merge-budget-blind` keeps its existing cases unchanged.
* No actual remote landing or new hosted timing run was performed for this record: UNVERIFIED. The first
  landing under it is its live check.

## Evidence

Read 2026-11-13. Sources below distinguish direct reads from the lead's supplied measurements.

* https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging
  — required checks gate merging, can be checks or commit statuses, and must be successful, skipped or
  neutral; external statuses can be posted through the commit-status API (fetched 2026-11-13).
* https://docs.github.com/en/actions/reference/limits#job-concurrency-limits-for-github-hosted-runners
  — Free's standard hosted-runner row lists 20 concurrent jobs and a maximum of 5 concurrent macOS jobs;
  the limits may change (fetched 2026-11-13).
* https://github.com/basecamp/gh-signoff/blob/02e0cf9c11c9a1587784fec17c07b93fcb22ec24/README.md
  — local tests followed by a commit signoff are the reference's alternative to hosted CI; its
  partial-signoff section also describes attesting individual checks. Read 2026-11-13 from the existing
  read-only pinned checkout; `git rev-parse HEAD` matched the full locked SHA (resolved, v0.4.1).
  `canon/upstreams.json` lists it as a reference, not an installed tool. Its local-only choice is evidence
  for local attestation, not evidence for removing Canon's required hosted full run.
* `docs/adr/0014-landing-workflow.md`, rules 2, 4 and 5 and "Not adopted" (read 2026-11-13 at `41f37333`)
  — the duplicated suite, statuses and the record's reason for rejecting a merge queue. Its cited URL is
  https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-a-pull-request-with-a-merge-queue
  — cited as ADR 0014's historical evidence; that page and the survey were not re-read for this unit,
  so their current applicability is UNVERIFIED.
* `docs/adr/0138-a-commit-runs-only-the-checks-that-take-seconds-and-a-landing-and-github-run-the-whole-suite.md`
  and `scripts/ci:57-62,254-264,275-307,347-466` (read 2026-11-13 at `41f37333`)
  — the proposed division, implemented quick set, whole-tree checks, staged-only secret scan and shared
  versus landing slot selection (`scripts/ci:189-196`). The record's earlier upstream citations were
  not re-read; this draft relies on the implementation read here.
* `scripts/land` and `.github/workflows/ci.yml` (read 2026-11-13 at `41f37333`)
  — local `--landing` call, status order, exact-head merge, nine-minute wait; hosted full `scripts/ci`,
  aggregator requiring `check` to succeed, the non-required macOS job, pull-request and push-to-main
  triggers.
* The live ruleset on `main`, read with `gh api repos/{owner}/{repo}/rulesets/<id>` on 2026-11-13
  — active; deletion, non-fast-forward, linear history, pull request, and required status checks
  `signoff` and `all required checks passed`; no merge-queue rule. `gh api repos/{owner}/{repo}` gives the
  owner type `User` (resolved).
* https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
  and https://github.blog/changelog/2023-07-12-pull-request-merge-queue-is-now-generally-available/
  — "Pull request merge queues are available in any public repository owned by an organization", and the
  changelog says the same (fetched 2026-11-13).
* `gh run list --workflow ci.yml` and `gh run view <id> --json jobs`, read 2026-11-14 — the run and
  `checks` job times and the two cancelled runs in the amendment of 2026-11-14 to rule 4; each run is
  this repository's `actions/runs/<id>` page on GitHub (fetched 2026-11-14 through GitHub's REST API by `gh`).
* Lead's assignment, dated 2026-11-13 12:54, read 2026-11-13 — the chosen split, the timing/failure/load
  figures, retained local unit tests and full pytest for a new module, and the instruction to leave
  today's in-flight landings alone. The measurement logs were not supplied or independently read:
  UNVERIFIED independently; no URL is available for this instruction.

## Amendment, 2026-11-14: the hosted check runs in five parts at once, behind the same required check

**What changes.** Rule 3's `checks` job no longer runs the whole of `scripts/ci` in one job. It is a job
matrix of five parts that run at once on their own runners, `part: [rest, 1/4, 2/4, 3/4, 4/4]` with
`fail-fast: false`, each running `scripts/ci --part <part>` (`.github/workflows/ci.yml`):

* A `parts` job runs `scripts/ci --list-parts`, which prints the parts as a JSON array, and the matrix is
  read from its output. `scripts/ci` sets the number of pytest parts in one place, `PYTEST_PARTS=4`.
* `scripts/ci --part rest` runs every check a plain run runs except the root pytest, which is a skip line
  naming the parts that run it. The self-test scripts, canon lint, ty, ruff, the shell and workflow
  checks, the secret scan and the projects apart are all here.
* `scripts/ci --part I/N` runs the lock check and the environment, then the root pytest on part I of N of
  the tests pytest's own collection lists (with the file of any module skipped at collection); every
  other check is a skip line naming part `rest`. A test function goes to the part a hash of its node ID,
  up to the parametrization, modulo N picks, so each test is in exactly one part and a part does not
  depend on the others. The parametrization is left out because a parameter drawn at import changes the
  ID in each process (`tests/test_custody.py` parametrizes on a `uuid4()`); naming the function runs
  every one of its cases. The part's functions reach pytest as one argument file (`@file`), in
  collection order. The node IDs the part reads must number what pytest reports it collected, or the part
  fails. The part prints how many functions it holds, of how many collected tests, and pytest's summary
  line. A collection that fails fails the part.

Together the five parts run what one plain `scripts/ci` runs, each collected test once and each check once
but the lock check and the environment, which every part runs. One gate of a plain run is not kept: there a
failed ruff or ty check skips the root pytest, while in a pytest part those checks are skip lines, so the
pytest parts run even when part `rest` fails them. The `all required checks passed` job needs `plan`
(named `parts`) and `check` and runs `scripts/ci --verdict`, which
fails, naming each, unless every job it needs has the result `success`: a part that failed, was cancelled
or timed out leaves `check`'s result otherwise. The ruleset's two required contexts, `signoff` and `all
required checks passed`, are unchanged, and so is the way `scripts/land` reads them, by bucket and not by
name. Each part's limit is 30 minutes, the lead's cap, to be set to about twice the slowest part once a
hosted run of the parts names it; `scripts/land`'s wait is 45 minutes, above it (rule 4). The required
context resolves only after three jobs in series, `plan` (at most 5 minutes), the slowest part (at most 30)
and the aggregator (at most 5), so at the limits the wait leaves about 5 minutes for runners to queue; a
wait that runs out stops `scripts/land`, while GitHub may still merge. Local runs
(`--staged`, `--landing`, plain) are unchanged.

**Why.** The one job's run took 33 to 38 minutes on 2026-11-14 when it passed (`gh run list --workflow
ci.yml`: on `main` 43617194822 33.9 min, 43614583851 36.5, 43611536636 35.4; pull request runs 32.9 to
38.4), and three runs were cancelled at the 40-minute timeout, 40.4 min each (`main` at `fa321292`,
43621264808, and two pull request runs). Its log prints no per-check time, but the root pytest alone ran
for 1,862.72 s (31 min 3 s) in one local run at `fa321292` (`pytest --durations=0` under `nice -n 15`,
12,466 passed, 66 skipped, 30 xfailed), so it is the run's long pole. The split is by test, not by file:
one file, `packages/canon-alpaca/tests/test_execution_client.py`, took 551 of the 1,834 s the durations
list sums, and the four parts a hash of each file would have made summed 167, 504, 305 and 859 s (1,835 s, by rounding). Hashed
by test function, the same durations sum to 474, 420, 486 and 454 s, each part under nine minutes of test
time before its environment install and collection. At `fa321292` the four parts' argument files,
collected by pytest, held 2,782, 2,513, 3,562 and 3,705 tests, 12,562 together with no ID in two parts,
the count one collection of the whole suite gives at that commit. A hash of the full node ID would have
lost the two `uuid4()` cases: collected again, one part reported `ERROR: not found` for it. No file of the suite holds a module fixture whose setup
takes more than 0.3 s, so running a file's tests in several processes costs little.

**Costs.** One pull request now starts eight hosted jobs (the parts job, five parts, the macOS job and the
aggregator) where it started three, against the 20 concurrent standard jobs GitHub documents for Free (above), so
several landings at once can queue. Each part installs the environment and collects the suite itself.
The hash moves tests between parts as IDs are added, so a part's time drifts; the parts' times in each
run's jobs list show it.

**Not verified.** GitHub's matrix and contexts pages say nothing about the `needs.<job>.result` of a matrix
job one of whose legs failed (read 2026-11-14). That a failed, cancelled or timed-out part makes `check`'s
result other than `success` is UNVERIFIED by such a hosted run here (uv's practice below is about a
reusable workflow's result, not a direct matrix job's);
`scripts/test-ci` shows that the verdict fails on any result but `success`.
That every part ends within fifteen minutes is UNVERIFIED, to be shown by the first landing under this amendment, whose pull
request names each part's wall time and the summed count against one collection at the same commit; the
time of part `rest`, whose checks the one job's log does not time, is UNVERIFIED until that run.

**Confirmation.** `scripts/test-ci` case `parts`: each part I of 3 runs its pytest on its own test
functions, given as one argument file, and no ruff, ty or test script, leaving those to part `rest`; it
prints its count and pytest's summary; together the parts name every collected function once, the file of
a module skipped at collection included, a function's parametrized cases go to one part as one name, and
one file's functions can fall in different parts; part `rest` runs no root pytest and
runs ruff, ty and the test scripts; a malformed part (`0/3`, `4/3`, `3`, `x/y`, `foo`) is refused by
name; a failed collection fails the part and the run, and so does a count of node IDs that differs from
the count pytest reports; `--list-parts` prints `rest` and `1/4` to `4/4`. Case `verdict`: the verdict
passes only when every job needed succeeded, and fails naming a part's `failure`, `cancelled` or
`skipped`, and fails on a missing or malformed `NEEDS_JSON`. 510 passed and 0 failed on 2026-11-14; against
`main`'s `scripts/ci` 58 fail, and against this amendment's first version, before its review's fixes, 25 fail.

**Evidence** (read 2026-11-14):

* uv at its pin `316c827e058c`, `.github/workflows/test.yml:243-248,318`: the `cargo-test-windows` job is
  a matrix `partition: [1, 2, 3]` with `fail-fast: false`, each leg running
  `--partition hash:${{ matrix.partition }}/3`, cargo-nextest's hash partition, which hashes each test;
  this repository applies the same form to each pytest test function. `.github/workflows/ci.yml:440-490`: uv's
  `required-checks-passed` job runs `if: ${{ always() && github.ref != 'refs/heads/main' }}`, needs
  `test`, a reusable workflow holding that matrix job, and fails on a need whose result is neither
  `success` nor `skipped` (and on `skipped` for `check-vet` and `check-cargo-cooldowns`). This
  repository's verdict is stricter: it fails on `skipped` too.
* pytest 9.2.0, as `uv.lock` pins it: `_pytest/config/argparsing.py:509` builds its parser with
  `fromfile_prefix_chars="@"`, so `@<file>` reads one argument per line; `_pytest/main.py:1155` splits a
  node ID's parametrization off at its first `[` before splitting the rest at `::`, so an ID whose
  parameters hold `::` (the suite has some) names one test; `_pytest/terminal.py:1387-1404` prints a skip
  under `--no-fold-skipped` as `SKIPPED <node ID> - <reason>`.
* https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs — a matrix runs one job per
  combination; with `fail-fast: true` "all jobs that are in progress or queued will be cancelled" once one
  fails, which `fail-fast: false` turns off. The workflow syntax reference,
  https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax, says of
  `jobs.<job_id>.strategy.fail-fast`: "This property defaults to `true`" (read 2026-11-14 through a
  page extractor's quote).
* https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations,
  "Using an output to define two matrices" — a job's output, `fromJSON(needs.<job>.outputs.<name>)`, as a
  later job's matrix. https://docs.github.com/en/actions/reference/workflows-and-actions/contexts —
  `needs.<job_id>.result` is `success`, `failure`, `cancelled` or `skipped` (both read 2026-11-14).
* https://docs.github.com/en/actions/using-jobs/using-jobs-in-a-workflow — "If you would like a job to run
  even if a job it is dependent on did not succeed, use the `always()` conditional expression".
* `gh run list --workflow ci.yml` and `gh run view 43617194822 --json jobs,--log`, read 2026-11-14 — the
  run times above; the `checks` job's "Run every check" step took 33.6 of its 33.8 minutes.
* The live ruleset on `main`, read with `gh api repos/{owner}/{repo}/rules/branches/main` on 2026-11-14 —
  required status checks `signoff` and `all required checks passed`, unchanged.
