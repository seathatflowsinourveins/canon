---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Landing workflow: a branch, one check, a squash merge, and no manual merge step

## Context and Problem Statement

The owner's standing instruction (2026-11-09) is that work lands automatically once it passes its checks.
Until now each change was merged by asking. How does a change get from a session to `main`, and what does
a pull request look like, taken from what current exemplar repositories actually do?

## Decision Drivers

* No manual merge step, and no step whose only purpose is ceremony.
* Take the practice from repositories that manifest it, and say where they disagree.
* Quality first: the checks and the review are the point; the mechanics should be invisible.
* Work the same with or without a GitHub remote. (There was none when this was written; there is one now.)

## Considered Options

1. Commit straight to `main`.
2. Branch, checks, then a merge that happens by itself: GitHub auto-merge where available, a script otherwise.
3. Option 2 plus a merge queue, required reviewers and a dependency bot.

## Decision Outcome

Chosen: **option 2**, through `scripts/land`.

1. **One branch per change, one worktree per parallel session.** Two sessions never write in one checkout.
2. **`scripts/land` is the merge step.** It refuses a dirty tree or a branch not based on `main`, runs
   `scripts/ci`, then lands: without `origin` it fast-forwards `main`; with `origin` it needs a signed-in
   `gh`, pushes, opens a pull request, posts `review` and `signoff`, and waits up to nine minutes for GitHub
   (`LAND_WAIT_SECONDS`) to squash-merge exactly the checked commit, then stops; auto-merge, if on, merges.
3. **Squash merge.** One commit on `main` per change. Subject: the open pull request's title, or with none
   the last commit's subject, then ` (#<number>)`. Message: the `--body` file, else for `--pr` the pull
   request's description, else the commit messages. Both pass the address check and go to the merge call.
4. **Two required checks.** A ruleset on `main` requires a pull request, linear history, the `signoff`
   status and the hosted workflow's `all required checks passed` job. `scripts/land` posts `signoff` for
   the exact commit it has just checked. This is local CI as 37signals practise it, with the same status
   name as their tool, plus an independent run on GitHub's runners.
5. **The same script runs in both places.** `scripts/ci` takes seconds here, so a failure shows before
   the push. The hosted workflow runs it again on a clean machine, and `scripts/land` waits for that run
   before it merges.
6. **The repository is public.** GitHub's standard hosted runners are free for public repositories.
   Commits carry noreply addresses: `scripts/check-addresses`, run by `scripts/land` and `.githooks/pre-push`,
   refuses any but a service or example address in commits, annotated tags and what `scripts/land` posts.
7. **Pull request text:** `Summary` and `Test Plan`, plus `Evidence` for what the change rests on. It is
   written for a later reader: no conversation history, discarded attempts or tool usage.
8. **Titles:** a plain sentence. No Conventional Commits prefix and no PR number.
9. **Review before landing code.** For a change under `src/` or `packages/`, run `/code-review` and resolve
   what it finds. Documentation and ADR changes do not need it. (Superseded in part by ADR 0017 and ADR 0019.)

Not adopted, with the reason:

* **Merge queue:** none of the six repositories read uses one, and it is not offered to personal accounts.
* **Required reviewers and CODEOWNERS:** one owner, who cannot approve their own pull request.
* **A dependency bot:** three of six use Renovate, one Dependabot, two none. Here `scripts/canon outdated`
  and the weekly refresh find new releases, and `scripts/canon lint` fails if a version changes in one file
  and not in the registry, so a bot's pull request could not merge without the canon process anyway.
* **Conventional Commits:** contested. mise requires them; NautilusTrader forbids them. This repo follows
  NautilusTrader because the adapter may be offered to its community listing.

ADR 0015 re-measures all four on 29 repositories. Three choices stand. The dependency bot bullet is
superseded: the larger sample converges on one, and ADR 0016 adopts it.

Where upstreams disagree with this repo's own habits: NautilusTrader forbids AI co-author trailers and
branded footers. Commits here carry the harness's co-author trailer; anything contributed to NautilusTrader
must follow its rule instead.

### Consequences

* Good: landing is one command and needs nobody; the same checks decide everywhere.
* Bad: landing waits a minute or two for the hosted run. Anything committed here is public, strategies
  included.

### Confirmation

`scripts/land --dry-run` runs the checks and says what would happen. `scripts/land` has landed every
change since 2026-11-09, first by fast-forward with no remote and then through pull requests. The
hosted workflow's first run, on 2026-11-10, passed all fifteen checks on a GitHub runner in 22 seconds,
and the change that recorded this waited for its hosted check before it merged.

## Evidence

Read 2026-11-09. "Read by the orchestrator" means the raw file was fetched and read directly.

* https://raw.githubusercontent.com/astral-sh/uv/main/.github/PULL_REQUEST_TEMPLATE.md and the same file in
  astral-sh/ruff — headings `Summary` and `Test Plan` (read by the orchestrator).
* https://raw.githubusercontent.com/astral-sh/uv/main/AGENTS.md — "DO NOT leak our conversation, prompt, or
  iteration history into code comments, pull request descriptions"; ruff's AGENTS.md says the same of
  discarded alternatives and intermediate edits (read by the orchestrator).
* https://raw.githubusercontent.com/astral-sh/uv/main/.github/workflows/ci.yml — the `required-checks-passed`
  job, built from `toJSON(needs)` (read by the orchestrator). ruff and mise have an equivalent job.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/AGENTS.md — no Conventional
  Commits, no PR number in titles, no AI co-author trailers, no branded footers; its CONTRIBUTING.md says the
  squash merge makes the PR title the commit subject (read by the orchestrator).
* https://code.claude.com/docs/en/desktop.md — "Merge when ready" turns on GitHub's auto-merge and requests
  a squash merge; a session can run in its own worktree under `.claude/worktrees/` (read by the orchestrator).
* https://code.claude.com/docs/en/worktrees.md — a worktree session's hooks run from the main checkout, so
  hooks must find the tree from the edited file (read by the orchestrator; hooks changed to match).
* https://code.claude.com/docs/en/code-review-local.md — `/code-review` looks for correctness bugs in a
  branch's changes and counts toward the session's usage (read by the orchestrator).
* Six-repository survey by a research lane (uv, ruff, NautilusTrader, mise, claude-code-action, alpaca-py;
  model-summarised): squash-style history at four or five; an aggregating required job at three; a merge
  queue at none; Renovate at three, Dependabot at one. Replaced as evidence by the measured survey in
  ADR 0015.
* https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request
  — auto-merge waits for required reviews and checks (fetched by a research lane). The research could not
  confirm whether rulesets work on a private personal repository; creating one on this repository settled it.
* https://raw.githubusercontent.com/basecamp/gh-signoff/main/README.md — "A GitHub CLI extension for local
  CI. Run your tests on your own machine and sign off when they pass"; the status is named `signoff` (read
  by the orchestrator; v0.4.1 resolved to `02e0cf9c11c9`). The tool is 2,670 lines of shell, so it is
  pinned as a reference and `scripts/land` makes the one API call itself.
* https://docs.github.com/en/rest/commits/statuses — "Users with push access in a repository can create
  commit statuses" (fetched by a research lane for ADR 0015; the call was also observed on this repository).
* https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-a-pull-request-with-a-merge-queue
  — merge queues are for organisation-owned repositories (read by the second reader).
* Second reader, 2026-11-09 (`evidence-verifier`): the templates, the quoted agent rules, the aggregating
  job and the ruleset are as described. It found the ruleset allowed every merge method, with squash
  enforced only by a repository setting; the ruleset now restricts it too.
* https://docs.github.com/en/billing/concepts/product-billing/github-actions — "The use of standard
  GitHub-hosted runners is free: In public repositories" (fetched by a research lane, 2026-11-09).
* Restated 2026-11-10, when hosted CI was switched on: rules 2 and 4 to 6, Consequences and Confirmation.
* `gh` 2.103.3's own help for `pr create`, `pr merge`, `pr checks`, `repo edit` and `api` — the flags the
  script uses (read by the orchestrator).
* Restated 2026-11-10 against `scripts/land` and `scripts/check-addresses`: rules 2, 3, 6 and 9.
