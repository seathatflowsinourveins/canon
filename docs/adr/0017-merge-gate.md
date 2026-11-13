---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-10
---

# Merge gate: checks decide, an independent review comes first, a model never blocks

## Context and Problem Statement

Hosted CI now runs on every pull request, with no limit on runs. The owner asked for the merge gate to use
what the ecosystem has converged on, and named model review (local `/code-review`, several models), agent
workers in workflows and more automation. What should block a merge, what should run before one, and what
does not belong in the gate?

## Decision Drivers

* The gate stays fast: seconds here, about a minute on GitHub. (2026-11-13: no longer true on GitHub; see
  Consequences.)
* What blocks must be reproducible. A model's judgement is not.
* Changes here are written by agents and merge without a person, so an independent reading has to happen
  before the merge (ADR 0015 rule 5).
* No stored credential, and no third party with write access (ADR 0016).

## Considered Options

1. Deterministic checks only, as before.
2. Checks block. An independent model review must have run before the push where a defect costs most; the
   author acts on it, the gate does not.
3. A hosted model review on every pull request, advisory.
4. A model review as a required check, or several models voting.

## Decision Outcome

Chosen: **option 2**.

| What the evidence shows | Source |
|---|---|
| A model's verdict blocks a merge in none of the 28 agent workflows read, at six repositories | read |
| uv runs a model review on its own pull requests and leaves it out of its one required check | read |
| A coding agent runs in a workflow at 5 of 28 repositories; three of them review pull requests | measured, read |
| Most of those workflows act on issues; fewer than ten act on pull requests | read |
| Claude's managed review "never approves or blocks"; the docs place review before merging | vendor |
| Several models on one change: one vendor preview describes it; none publishes accuracy with a sample | vendor |
| Independent readings here found three defects in two days that the checks had passed | ADR 0011, 0015, 0016 |
| A scheduled workflow 25/28, timeouts 22/28, caching at least 21/28, a `matrix:` 27/28 | measured |

Rules:

1. **Checks decide.** A merge needs `signoff` and `all required checks passed`, and nothing else
   (ADR 0014). No model output is a required check.
2. **An independent review runs before the push** where a defect costs most: code (`src/`, `packages/`),
   harness scripts (`scripts/`, `home/`, hooks), workflows, and any new decision record. `/code-review`
   reads code; the `evidence-verifier` reads a record. The author resolves what it finds. `scripts/land`
   refuses such a change without `--reviewed "<what ran and what came of it>"` and records the note on the
   commit as the `review` status. Every other change skips it. This widens ADR 0014 rule 9.
3. **The reviewer does not share the author's context.** Both reviewers start fresh. Effort follows
   ADR 0005. For a substantial change the owner may start `/code-review ultra`, which is billed.
4. **One model family, for now.** A second vendor's reviewer needs a stored key, and no source measures
   what it adds. `watch`. Superseded by ADR 0043: a Codex worker reads named changes as an additional
   reader that never blocks.
5. **No agent in a workflow, for now.** The repositories that run one keep it advisory, limit who can
   start it and do most writing in a separate job. Here `scripts/land` merges as soon as the required
   checks pass, so a hosted review would report after the merge unless the gate waited for it, which
   rule 1 excludes. It also needs a credential: a stored token, or identity federation with the model
   provider. If pull requests ever wait for a person, `anthropics/claude-code-action` through
   `adopt-upstream` is the path. `watch`, with GitHub's agentic workflows.
6. **A hosted watch.** `.github/workflows/watch.yml` runs `scripts/canon check` and `outdated` once a day
   on GitHub, so a re-pointed release tag is caught while the development machine sleeps (ADR 0011).
7. **Converged, and not taken:** caching, because the whole run takes about half a minute (true on
   2026-11-10; for the run's length now, see Consequences); a matrix,
   because the same script already runs on macOS here and on Linux there; release automation (at least
   19/28), because nothing is released. A workflow that merges bot pull requests is rare (at most 3/28);
   ADR 0016 stands, and a Dependabot change to a workflow now lands with a note like any other.

Rule 2's list of what needs a review and option 2's "where a defect costs most" are superseded by ADR 0019.

### Consequences

* Good: the gate stays about a minute, and review happens where it can still change the code.
  (2026-11-13: the first half no longer holds. The hosted `checks` job took 18 min 46 s and 19 min 09 s
  (ADR 0138). The workflow's tool cache is still off (`cache: false` on `mise-action`), and a cache of
  check results stays deferred under ADR 0097; whether a tool cache would now pay is not weighed here.)
* Bad: the `review` note is the lander's own statement, like `signoff`. It shows that a review was
  claimed, not what it read, and its depth is the author's choice. One model family reads its own work (ADR 0043 since adds a second).
  GitHub switches a schedule off after 60 days without activity in a public repository;
  `scripts/automation status` shows the hosted watch's last run so that is noticed.

### Confirmation

`scripts/test-land` holds the gate's behaviour, offline against throwaway repositories and a stub `gh`; the
paths it covers are in ADR 0019. `scripts/land` refuses a blank note or another option in its place. The
watch can be started by hand with `gh workflow run watch.yml`.

## Evidence

Read 2026-11-10. "Lane" means a research subagent fetched the page and a model summarised it.

* `canon/github-practice.json` — the 29-repository survey, re-run with automation measures (fetched
  through GitHub's API by `scripts/survey-github`). Its caching, release and reusable-workflow counts are
  lower bounds and its bot auto-merge count an upper bound: the patterns are named in the report.
* https://raw.githubusercontent.com/astral-sh/uv/main/.github/workflows/ci.yml — the job
  `review-code-quality` runs on same-repository pull requests and is not among the `needs` of
  `all required jobs passed` (fetched).
* 28 workflow files that run an agent, in five repositories of the sample (astral-sh/uv,
  anthropics/claude-agent-sdk-python, anthropics/claude-code-action, openai/codex,
  google-gemini/gemini-cli) and in anthropics/claude-code: none fails a merge on the agent's verdict; most
  act on issues; writes are mostly done by a separate job, though two give the agent a write token
  (fetched; lane). Their prompt files and branch rules were not read: UNVERIFIED.
* https://code.claude.com/docs/en/code-review.md — the managed review is for Team and Enterprise plans and
  "never approves or blocks the pull request" (fetched; lane).
* https://code.claude.com/docs/en/code-review-local.md — `/code-review` works on any plan and "counts
  toward your plan's usage or API billing" (fetched; lane).
* https://code.claude.com/docs/en/ultrareview.md — "before merging a substantial change"; "typically $5 to
  $25" after three free runs (fetched; lane).
* https://code.claude.com/docs/en/github-actions.md — "review Claude's changes before merging"; the action
  can run on a subscription token from `claude setup-token` or on identity federation (fetched; lane).
* https://raw.githubusercontent.com/anthropics/claude-code-action/main/docs/security.md — on a public
  repository outside parties can cause workflow events; write access is required by default (fetched; lane).
* https://github.blog/changelog/2026-10-28-copilot-code-review-merge-with-confidence-with-max-and-approvals/
  — several providers' models on one review, in preview, with no accuracy figure (fetched; lane).
* https://github.blog/changelog/2026-07-30-github-models-is-now-retired/ — GitHub Models is retired, so it
  is not considered here as a way to run a model from a workflow (fetched; lane).
* https://github.blog/changelog/2026-10-28-github-agentic-workflows-generally-available/ — agentic
  workflows, generally available (fetched; lane).
* ADR 0011, ADR 0015 and ADR 0016, their second-reader lines — independent readings found a guard
  allow-list that let four changing commands through, a survey that read half the history without saying
  so, and a landing path that failed on a stale branch. The checks had passed each time. Other readings
  of other records found nothing wrong.
* Second reader, 2026-11-10 (`evidence-verifier`): every measured row matched the data and every quoted
  phrase was on its page. It found that renames, oddly named files and the hook wiring got past the
  review gate, that the two statuses were posted in an unsafe order, and that rule 5 and the "three
  readings" row said more than their sources. All are fixed above and in `scripts/land`.
* Restated 2026-11-10 against `scripts/test-land` and a run of it: Confirmation.
* ADR 0043, 2026-11-10 — supersedes rule 4: the second family's reviewer runs on the owner's own sign-in,
  which Codex keeps in its own home or the OS credential store, not a key stored in this repository or a workflow; ADR 0043 states
  that an unsandboxed worker can reach it. A pointer line was added to rule 4.
* ADR 0138, read 2026-11-13 — two hosted `checks` jobs of 18 min 46 s and 19 min 09 s, setup included;
  `.github/workflows/ci.yml`, read the same day — `mise-action` runs with `cache: false`. The dated notes in
  the drivers, rule 7 and Consequences rest on these.
