---
status: accepted
date: 2026-11-10
decision-makers: lead orchestrator (Claude), under the owner's delegation of 2026-11-10; repo owner ratifies
review-by: 2026-12-10
---

# Compounding GitHub automation: the hosted watch keeps one locked drift issue, and nothing else is built yet

## Context and Problem Statement

The owner asked for GitHub automation that compounds: what the repository learns becomes a standing check
or source without anyone remembering to add it. The hosted watch (ADR 0017 rule 6) compares the canon with
upstream daily, but its result reaches only a run summary, and a failed run e-mails one person. Five
candidates: drift as a tracking issue, a fix drafting its eval entry, a score history, the landscape
refresh on a hosted schedule, the ecosystem manifest diffed on a schedule. Which hold, and does drift go
to an issue or a bot pull request?

## Decision Drivers

* Converged practice over invention (ADR 0001); counts over examples (ADR 0015).
* No stored credential, and no pull request that needs the owner (ADR 0016's drivers).
* Nothing blocks a landing except a real check (ADR 0017 rule 1); least `GITHUB_TOKEN` permission per job.
* A scheduled job that fails is loud (AGENTS.md rule 5), not a red mark one person may be e-mailed about.

## Considered Options

1. Drift as one standing bot pull request against a tracked drift file.
2. Drift as one tracking issue, with Issues on and bounded.
3. Leave drift in the run summary.

## Decision Outcome

Chosen: **option 2**, on the evidence below. The owner turned Issues on and set issue creation to
collaborators only on 2026-11-10.

| Evidence | Count or source |
|---|---|
| Issues switched off in the 29-repository sample | 0 of 29 |
| A standing dependency issue (Renovate's open "Dependency Dashboard") | 6 of 29 (of the 10 on Renovate) |
| Issues a workflow opened (`github-actions[bot]`); of them, a scheduled job's failure filed as an issue | 5 of 29; 4 (ruff, ty, gemini-cli, pydantic) |
| Drift kept as a pull request against a tracked file | none in the workflows read; not counted |
| A pull request opened with `GITHUB_TOKEN` gets its checks only "in an approval-required state", a click per push, or with a stored App or personal token | GitHub docs |

1. **`scripts/drift report`** turns the watch's `scripts/canon check` and `outdated --json` and the
   records' front matter into one list: a retag, a stale lock, an upstream not read, a newer release, an
   unrankable pin, a record past `review-by`, the proposed records for reference. A list from a failed or
   missing input is INCOMPLETE, never clean. It goes to the run summary daily.
2. **`scripts/drift issue`**, in the watch's second job, keeps one issue labelled `canon-drift`, opened by
   the job's own token, in step with that list and with the first job's result: open and lock it when
   there is something to track, rewrite its body each day, comment only when the list changes, close it
   when nothing has drifted, close duplicates. A failed watch is tracked there too. Only that job has
   `issues: write`, and only on `main`, and not when the run was cancelled. A refused write fails the
   job by name. No new action: `gh` is preinstalled on GitHub's runners.
3. **The session brief names the open issue.** The async session-start watch asks GitHub for it the same
   way and records it as `drift_issue`; "could not be read" is said, never taken as none.
4. **The bound is the owner's standing setting, and the job reads it before it writes.** Issues are
   readable by anyone. With issue creation set to collaborators only, only collaborators can open one,
   and the lock leaves comments to people with write access. The owner set it on 2026-11-10. REST has no
   field for it, but GraphQL's `Repository.issueCreationPolicy` does, and it reads `COLLABORATORS_ONLY`.
   `scripts/drift issue` checks `hasIssuesEnabled` and that field first, and writes nothing, failing by
   name with the owner's step, when either is off or cannot be read.

Not built, with the reason:

* **A fix drafting its eval entry.** Fix messages report a failing test in free prose; a case needs the
  fix, its `fail_to_pass` functions and the defects it confirms (ADR 0046, ADR 0057). The one maintained
  example starts from an issue and runs an agent (uv's `reproduce-bug.yml`). Open: git trailers such as
  `Fails-before: <file>::<function>` and `Confirms: <case>/<defect>` would make it mechanical, and the
  drift list could then name fixes no case cites.
* **A score history.** Eval runs are started by the coordinator, not CI, and `evals/*/runs/<date>-<label>/`
  is already a dated series; a machine-readable sheet is ADR 0046's and ADR 0057's to define.
* **The landscape refresh's model half, hosted.** Its script half (ADR 0026 rule 1) now runs on GitHub
  daily. The model half needs an agent in a workflow (ADR 0017 rule 5 rules it out) and a stored secret:
  laid out under Consequences for the owner, not built.
* **The ecosystem manifest on a schedule.** Its source is not settled; once it is, its diff becomes one
  more input to `scripts/drift report`.
* **Release automation, issue forms.** Nothing is released (ADR 0017 rule 7); only collaborators open issues.

### Consequences

* Good: drift and a failed watch reach the owner, GitHub and the next session as one locked item. Each
  new kind of drift is a new input to one script and one test.
* Bad: anyone can read the issue. GitHub does not document whether `GITHUB_TOKEN` may read
  `issueCreationPolicy`, or open an issue under collaborators-only creation (UNVERIFIED until the first
  run). If it may not, the job fails by name and the options go to the owner; the bound is not widened.
* GitHub does not define the "repository activity" that keeps a public repository's schedule on past
  60 days. `scripts/automation status`'s hosted-watch row stays the guard (ADR 0017).
* For the owner, if the model half is wanted hosted: `anthropics/claude-code-action` (MIT) takes
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or identity federation; its weekly example grants
  `contents`, `issues`, `pull-requests` and `id-token: write`; what it sends to the model API is not
  stated in its docs (UNVERIFIED). `openai/codex-action` (Apache-2.0) takes an API key; `run-gemini-cli`
  (Apache-2.0) a Gemini key or federation, and runs on a schedule. DeepSeek has no official action; a
  community one exists (`Lixiaoyiao/DSH-action`, MIT; second reader). Each is its own adoption.

### Confirmation

`scripts/test-drift` holds the list and the issue's lifecycle offline, against a stub `gh` (72 cases).
`scripts/test-canon` holds the brief's four `drift_issue` cases. Run by hand on this machine on
2026-11-10, the session-start watch recorded `drift_issue: []` from GitHub after Issues were turned on.
The workflow passes actionlint. First hosted runs, 2026-11-11, started by hand on `main`, with issue
creation limited to collaborators: the job's `GITHUB_TOKEN` read `issueCreationPolicy`, then opened issue
#136 as `github-actions[bot]`, labelled and locked, with 8 items; the second run rewrote its body, said
"the list is unchanged" and posted no comment. Both runs succeeded. So the default token may read the
bound and open an issue under it, and the UNVERIFIED point under Consequences is settled for this
repository.

## Evidence

Read 2026-11-10. "Scout" means a research subagent fetched the page and a model summarised it.

* `canon/github-practice.json` (29 repositories, `scripts/survey-github report`) — a scheduled workflow
  at 25 of 28; a dependency bot at 23 of 29 (read).
* GitHub's REST API over the same 29 repositories, 2026-11-10: `GET /repos/{r}` for `has_issues`, and
  `GET /repos/{r}/issues` with `creator` set to `renovate[bot]` (open) and to `github-actions[bot]`
  (all states), the latest 100 items each. Workflow counts are therefore lower bounds. Re-measuring them
  in `scripts/survey-github` would take these two calls per repository as new rows (fetched).
* https://docs.github.com/en/actions/concepts/security/github_token — a `GITHUB_TOKEN` pull request's
  runs are "approval-required"; "a user with write access ... can start the runs"; to have them "execute
  without requiring approval, use a GitHub App installation access token or a personal access token
  instead" (fetched; the wording as the second reader read it).
* https://github.blog/changelog/2026-06-11-bot-created-pull-requests-can-run-workflows-if-approved/ — the
  same, dated (fetched).
* https://docs.github.com/en/rest/actions/workflow-runs#approve-a-workflow-run-for-a-fork-pull-request —
  documented for "a public fork of a first time contributor" only (fetched).
* https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository
  — one setting lets workflows "create or approve" pull requests; off by default for a personal account;
  no risk stated (fetched).
* https://raw.githubusercontent.com/peter-evans/create-pull-request/main/README.md — a bot pull request
  needs `contents: write` and `pull-requests: write` and that setting (fetched; MIT). It still says such a
  pull request triggers no `pull_request` workflow, which the 2026-06-11 change makes out of date.
* https://github.blog/changelog/2026-06-29-restrict-issue-creation-to-collaborators-only/ and
  https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/disabling-issues
  — issue creation limited to collaborators; in a personal repository "a collaborator is anyone who has
  been invited" (fetched). They disagree in wording: the changelog says "collaborators with write access"
  and labels the control "Creation allowed by", the docs "Collaborators only", and the settings page
  "Issue permissions" (the owner's reading of the page, 2026-11-10: UNVERIFIED by this record).
* https://docs.github.com/en/rest/repos/repos?apiVersion=2026-03-10#update-a-repository — `has_issues` and
  `pull_request_creation_policy`, no issue-creation field (fetched). This repository's API answer
  afterwards: `has_issues: true`, `pull_request_creation_policy: all`, nothing for issues (fetched). The
  settings page notes that restricted issues "will still be readable by everyone" (the owner's reading,
  UNVERIFIED by this record).
* GitHub's GraphQL schema — `Repository.issueCreationPolicy`, "The policy controlling who can create
  issues in this repository", enum `ALL` or `COLLABORATORS_ONLY`; this repository answers
  `hasIssuesEnabled: true`, `COLLABORATORS_ONLY` (fetched through `gh api graphql`, introspection and the
  query `scripts/drift` makes).
* https://docs.github.com/en/communities/moderating-comments-and-conversations/locking-conversations —
  on a locked issue only people with write access, owners and collaborators can comment; no expiry
  (fetched). https://docs.github.com/en/communities/moderating-comments-and-conversations/limiting-interactions-in-your-repository
  — interaction limits last 24 hours to 6 months (fetched).
* https://docs.github.com/en/actions/using-workflows/using-github-cli-in-workflows — `gh` is preinstalled
  on GitHub-hosted runners and takes `GH_TOKEN` (scout).
* https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions —
  `issues: write` "permits an action to add a comment to an issue"; once one key is set, every unset key
  is `none` (fetched, first 100,000 of 236,283 characters).
* https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows
  and .../notifications-for-workflow-runs — a schedule runs on the default branch's latest commit and may
  be delayed; its notifications go to "the user who last modified the cron syntax" (scout).
  .../disabling-and-enabling-a-workflow — the 60-day rule, with "activity" undefined (scout).
* https://raw.githubusercontent.com/astral-sh/uv/main/.github/workflows/diagnose-workflow-failure.yml and
  .../reproduce-bug.yml — an issue found by title before a new one is opened; a regression test drafted by
  an agent from an issue (scout).
* https://github.com/anthropics/claude-code-action (docs/usage.md, docs/security.md, docs/solutions.md),
  https://github.com/openai/codex-action, https://github.com/google-github-actions/run-gemini-cli,
  https://github.com/benchmark-action/github-action-benchmark — the options and needs above (scout).
* The records were searched (`docs/adr/`, `README.md`, `AGENTS.md`, `docs/`) for a rule that routes
  outside contributions through pull requests; there is none. `scripts/land --pr` refuses a pull
  request from a fork (read).
* Second reader, 2026-11-10 (`evidence-verifier`): the counts re-derive on all 29 repositories and the
  decision stands. It found that GraphQL reads the creation setting the record had called unreadable,
  a quote not word for word, two owner-sourced lines without a method, the drift-PR row without one,
  the disagreeing wordings of the setting, an unsourced claim of what the Anthropic action sends, a
  community DeepSeek action, and four brief cases, not three. All are corrected above, and the job now
  reads the setting before it writes.
