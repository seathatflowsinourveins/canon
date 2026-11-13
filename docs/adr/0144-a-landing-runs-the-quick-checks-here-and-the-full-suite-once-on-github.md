---
status: accepted
date: 2026-11-13
decision-makers: the owner (approved 2026-11-13); the lead (2026-11-13), under the owner's delegation (ADR 0074); Claude for the record
review-by: 2026-12-13
---

# A landing runs the quick checks here and the full suite once on GitHub

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

1. **Local landing scope.** `scripts/land` invokes `scripts/ci --staged` instead of `scripts/ci --landing`.
   Despite its name, that mode runs the lock and environment checks, ruff, format, ty, test collection,
   canon lint and shell checks across the tree, including the separate projects' lock, environment, ty
   and collection checks. Landing already refuses a dirty tree, so these see the whole committed branch
   tree, not just the last commit's files. The mode's secret scan is still staged-only: the clean index
   has no staged diff. Full history and working-tree secret scans remain in hosted `scripts/ci`; this
   local command does not claim to scan the branch's committed history for secrets.
2. **Hosted scope.** The existing `checks` job runs full `scripts/ci`; its aggregating
   `all required checks passed` job must succeed before merge. "Once" means the full required validation
   of the pull request is hosted, with no full local copy at landing. The workflow also runs on pushes
   to `main`, so this is not a claim that a landing causes only one workflow run overall; that trigger
   and the reporting macOS job stay as they are.
3. **Statuses and merge.** The `review` status keeps the same note and precedes `signoff`. The `signoff`
   description becomes `scripts/ci --staged (quick checks) passed locally`, attached to the checked commit.
   The review gate, address check, branch rebase/merge rule, both status contexts, required-check wait,
   exact-head squash-merge arguments, subject and body stay as they are. Repository settings, rulesets
   and merge queues are outside this decision and are untouched.
4. **The lane's tests.** A lane still runs its unit's own tests before landing, and one full pytest when
   it adds a test module. Quick checks and test collection do not replace those runs.
5. **Records amended.** This amends only ADR 0014's requirement to repeat the full suite
   locally at landing (rule 5, and the scope of the local pass in rules 2 and 4). It settles ADR 0138's
   division of checks: quick at commit and at local landing, full in the required hosted run. ADR 0138's
   unrecorded timing comparison stays UNVERIFIED; this decision does not supply it or claim seconds.
   ADR 0014's historical rejection of a merge queue is unchanged: its six-repository survey found none,
   and the record said it was unavailable to personal accounts. That reason was read here, not surveyed
   again or turned into a proposal for settings changes.

Option 1 keeps the measured duplicate work. Option 2 would remove the independently required hosted
check; that is outside the lead's decision. Option 3 keeps the existing hosted enforcement and changes
only the local scope and the text that attests to it.

### Consequences

* Good: the local landing no longer runs the full suite that GitHub runs before merging. The anticipated
  saving is the reported approximately 15-minute local pass, not a new measured speed-up.
* Cost recorded today: about 15 minutes locally plus 18-26 minutes on GitHub per landing; three local
  landing runs failed, one real and two not (load, a damaged environment); load was 40-54 with three
  local runs. These are the lead's 2026-11-13 measurements, independently UNVERIFIED here.
* Bad: a failure outside the quick set may first appear on GitHub after the push. The lane's own tests
  remain necessary. A local landing with no `origin` keeps its fast-forward behavior but now receives
  only the quick set; it has no hosted full-suite result.
* Bad: the staged-only secret scan adds no coverage of committed history at a clean landing. The
  commit hook scans staged changes, and the hosted required run still scans history and the tree.
* GitHub documents 20 concurrent standard hosted jobs on Free, with at most 5 macOS jobs; these are
  limits, not a guarantee of immediate starts. Actual account usage and runner contention are UNVERIFIED.
* The existing nine-minute merge-wait default is unchanged, even though the reported hosted duration
  is longer. A timeout still reports the last observed state; an accepted auto-merge request can outlive
  the local wait. This decision adds no new wait or retry mechanism.
* The quick mode still prints its existing commit-oriented skip wording, and uses shared CI slots
  rather than `--landing`'s reserved slot. This unit does not change `scripts/ci` or its mode interface.

### Confirmation

* `scripts/test-land`, red against the base landing implementation: the actual CI invocation must be
  exactly one `--staged` call on a clean multi-commit branch, including the no-remote, `--pr` and dry-run
  paths; signoff must name the quick checks and both statuses must name the checked commit.
* The same tests retain refusals for a missing `--reviewed` note and failed local checks before any
  status or merge; existing cases retain the address, rebase, merge-head, merge-text and wait checks.
* `scripts/test-land-merge-budget-blind` keeps its existing six cases and seven controls unchanged.
* Reproduced by the landing lane on 2026-11-13: with the new `scripts/test-land` and the base
  `scripts/land`, 6 cases failed (123 passed); with this change, 129 passed and 0 failed.
  No full local CI, actual remote landing, or new hosted timing run was performed for this record:
  UNVERIFIED. The first landing under it is its live check. Existing hosted workflow and ruleset behavior was read from the repository and the task,
  not tested against GitHub settings.

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
  aggregator requiring `check` to succeed, pull-request and push-to-main triggers. Live settings were
  not queried: UNVERIFIED beyond the repository's description and the task's stated contract.
* Lead's assignment, dated 2026-11-13 12:54, read 2026-11-13 — the chosen split, the timing/failure/load
  figures, retained local unit tests and full pytest for a new module, and the instruction to leave
  today's in-flight landings alone. The measurement logs were not supplied or independently read:
  UNVERIFIED independently; no URL is available for this instruction.
