---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-07
---

# GitHub practice: what a measured sample converges on, and where this repo differs

## Context and Problem Statement

ADR 0014 took its pull request and automation practice from six repositories that a research lane read and
a model summarised. The owner asked for the converged practice with evidence. What do current exemplar
repositories actually do, counted from their own files, settings and history, and does this repo match?

## Decision Drivers

* A count that anyone can re-derive outranks a summary (ADR 0001).
* Keep three kinds of evidence apart: what repositories do, what they write down, what GitHub documents.
* A difference from a converged practice needs a stated reason and a condition that would end it.

## Considered Options

1. Leave ADR 0014's evidence as it is.
2. Measure a wider sample with a script, keep the data in the repo, and test each choice against it.
3. Adopt whatever a majority of the sample does.

## Decision Outcome

Chosen: **option 2**. `scripts/survey-github` read 29 repositories on 2026-11-10 (UTC): the 14 upstreams
the registry lists as a foundation or a tool, 5 coding-agent tools and 10 large projects. The rows are in
`canon/github-practice.json`, and `scripts/survey-github report` prints every table. A practice is
**converged** at two thirds or more of the repositories it applies to, **rare** at one fifth or fewer, and
**split** between.

Converged, and in place here: a protected default branch (26/29, 19 through rulesets), no merge commits
(22/29), commits arriving through pull requests (20/29), squash merge allowed (28/29, the only method at
16), head branch deleted on merge (27/29), auto-merge switched on (20/29), actions pinned by commit SHA
(21/28) and `cancel-in-progress` (20/28). Split, and in place here: token `permissions:` (18/28), a pull
request template (17/29; `Summary` in 7, `Test Plan` in 4), AGENTS.md (14/29), a workflow linter (14/28) and
one aggregating job (11/28; written here, though `signoff` is the required check). Split, and absent
here: CODEOWNERS (13/29), because there is one owner.

| Practice | Sample | Verdict | Here |
|---|---|---|---|
| CI on GitHub Actions | 28/29 | converged | yes (rule 2) |
| Dependency update bot (Dependabot 13, Renovate 10) | 23/29 | converged | yes (ADR 0016) |
| Approving review on half or more of human pull requests | 12/28 | split | **no** (rule 5) |
| Conventional Commits on 80% or more of subjects (10% or fewer at 13) | 11/29 | split | no (ADR 0014) |
| An AI co-author trailer in the last 100 commits (Claude's at 12) | 18/29 | split | yes (ADR 0014) |
| A bot account performs half or more of the merges | 4/29 | rare | no (rule 4) |
| Auto-merge enabled on a fifth or more of pull requests | 2/29 | rare | no (rule 4) |
| Merge queue in use | 2/29 | rare | no |
| A coding agent runs in a workflow | 5/28 | rare | no |
| Workflow lockfile | 0/28 | rare | no: `watch` |

Rules:

1. **ADR 0014's mechanics stand.** Eight of the ten converged practices were already in place. The other
   two, hosted CI and a dependency bot, followed from this survey (rule 2, ADR 0016). Commit style and
   AI trailers stay contested in practice and in written rules, so its choices there are unchanged.
2. **Hosted CI is on.** The workflow runs `scripts/ci` on every pull request and on `main`, and the
   ruleset requires its `all required checks passed` job beside `signoff`. `scripts/land` waits for it.
3. **A dependency bot is the converged practice, and this repo has the gap it closes.** `scripts/canon`
   reads releases for the 22 upstreams pinned to a tag and new commits for the 10 pinned to a branch, at
   most once a day and only when a session starts. Nothing watches the packages in `uv.lock` beyond the
   five direct ones the registry names. Adoption was its own change through `adopt-upstream`
   (ADR 0016): Dependabot, for indirect packages and actions only, which leaves exact pins with the canon.
4. **Merging automatically once checks pass is the owner's instruction, not a practice the sample supports.** A bot account
   performs most merges at four repositories (Rust, Kubernetes, codex, ccusage), and two more (gemini-cli,
   zed) merge through a queue with an approval on 100% and 94% of pull requests. In the rest a person
   presses merge. This repo merges on checks alone because the owner said so (ADR 0014).
5. **Review before merging is what Claude Code's docs ask for, and this repo does not have it.** Here
   `/code-review` runs before code lands, which is an agent reading an agent's change, and the owner reads
   pull requests after they merge. That is the largest gap between this repo and the vendor's guidance. It
   is bounded by paper trading only (ADR 0009) and by squash commits that revert in one step. If the owner
   wants the documented practice for trading code, `scripts/land` can leave pull requests that touch
   `src/` or `packages/` open for them.
6. **Watch, and re-run.** Watch workflow lockfiles (GitHub's docs say one supersedes SHA pinning; generally
   available 2026-10-28, adopted by none of the sample) and stacked pull requests (2026-10-06, not
   measured). Re-run the survey when this ADR is reviewed; it takes eight minutes, so not unattended.

### Consequences

* Good: each claim about what others do is a row someone can recompute.
* Bad: the sample was picked by hand and holds no small single-owner repository, which is what this repo
  is. File checks are text patterns, not a YAML parse. A repository's last 100 commits are days for some
  and months for others. Approval by bot command (`r+` at Rust, `/lgtm` at Kubernetes) is not a GitHub
  review and is not counted, so the review row understates review there.

### Confirmation

`scripts/survey-github selftest` runs in `scripts/ci`: 67 cases on fixed input. The data file is from the
run of 2026-11-10, which reads triggers from each workflow's `on:` block and names what it could not
read: two repositories were partly read and are left out of the trigger rows. Two surprising rows were
re-derived through a different endpoint and agreed: of the 12 newest commits, 1 at NautilusTrader and 5 at
claude-agent-sdk-python had a merged pull request, against 16% and 23% of 100. The survey was run three
times. The first run cut long commit subjects short and missed job-level `permissions:`. The second read
only 50 commits at seven repositories without saying so; the third reads 100 everywhere, in pages.

## Evidence

Read 2026-11-09. "Lane" means a research subagent fetched the page and a model summarised it.

* `canon/github-practice.json` — 29 rows from GitHub's REST and GraphQL APIs and raw file fetches (measured
  by the orchestrator with `scripts/survey-github run`; sample in `canon/github-practice.sample.json`).
* Second reader, 2026-11-09 (`evidence-verifier`): the 23 sample counts it checked matched the data, 19
  recomputed directly. It found the silent 50-commit fallback, a repository with no human pull requests counted in
  two rows, and three patterns matched too loosely; the script was fixed and the survey re-run. It found
  rules 1, 3, 4 and 5 worded beyond their evidence; all four are rewritten above.
* https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows — `$/.github/workflows/…` is
  "the recommended syntax for referencing a reusable workflow in the same repository" and resolves to the
  caller's commit, so the survey counts `$/` and `./` references as local, not as unpinned (fetched; lane).
* https://docs.github.com/en/actions/reference/security/secure-use — a workflow lockfile "supersedes SHA
  pinning for repositories that adopt it"; otherwise pin to a commit SHA (fetched; lane).
* https://github.blog/changelog/2026-10-28-github-actions-workflow-lockfiles-are-generally-available/ —
  `.github/workflows/actions.lock`; and …/2026-10-06-stacked-pull-requests-generally-available/ (fetched; lane).
* Restated 2026-11-10, when hosted CI was switched on: the first two table rows and rules 1 to 3.
  Re-measured the same day: the workflow-linter count was 12 through a counting error in the script; it
  is 14. The approving-review row moved from 13 to 12 as one repository's last 100 commits changed.
  Every other count held.
* https://code.claude.com/docs/en/github-actions.md — "review Claude's changes before merging"; the action
  "runs on GitHub-hosted runners" and bills Actions minutes plus tokens. No Claude Code page recommends a
  title convention, a description template or a merge method (fetched; lane).
* AGENTS.md, CONTRIBUTING and AI policy files (fetched; lane; a model's reading, not a count) of uv, ruff,
  ty, nautilus_trader, mise, alpaca-py, pytest, cpython, pydantic, polars, fastapi, codex, gemini-cli,
  claude-code-action, claude-agent-sdk-python, ghostty, zed, rust, kubernetes and astropy. Conventional
  Commits: required by mise and polars, forbidden by nautilus_trader, pydantic and zed. AI co-author
  trailers: forbidden by nautilus_trader, rust and kubernetes, unmentioned by 16.
