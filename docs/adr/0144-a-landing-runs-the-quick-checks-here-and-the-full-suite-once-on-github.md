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
   reported as before. The review gate,
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
* Lead's assignment, dated 2026-11-13 12:54, read 2026-11-13 — the chosen split, the timing/failure/load
  figures, retained local unit tests and full pytest for a new module, and the instruction to leave
  today's in-flight landings alone. The measurement logs were not supplied or independently read:
  UNVERIFIED independently; no URL is available for this instruction.
