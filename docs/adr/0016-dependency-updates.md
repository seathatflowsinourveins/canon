---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-07
---

# Dependency updates: Dependabot proposes, a session lands

## Context and Problem Statement

ADR 0015 measured a dependency update bot at 23 of 29 repositories and found the gap one closes here:
nothing watches the packages in `uv.lock` beyond the five the registry names. The owner approved adopting
one (2026-11-09). Which bot, covering what, and how does its pull request land?

## Decision Drivers

* The platform vendor's own tooling comes before community work (`adopt-upstream`).
* No third party gets write access to the repository, and no credential is stored.
* A pin the registry names still moves only through the canon, after its changelog is read (ADR 0001).
* At most a couple of pull requests a week, and none that needs the owner.

## Considered Options

1. Dependabot: GitHub's own service, switched on by a config file.
2. Renovate through Mend's hosted GitHub App.
3. Renovate's CLI run on this Mac on a schedule.
4. Stay with `scripts/canon outdated` alone (ADR 0014).

## Decision Outcome

Chosen: **option 1**, through `.github/dependabot.yml`.

| | Dependabot | Renovate |
|---|---|---|
| Indirect packages in `uv.lock` | yes: `allow: dependency-type: indirect` | only to fix a vulnerability, or by rebuilding the whole lock |
| GitHub Actions pinned by SHA with a version comment | yes, comment included | yes |
| `mise.toml` | no | yes, with `mise.lock` |
| This repo's registry file | no | a custom manager can; same pull request not confirmed |
| Access it needs | none beyond GitHub itself | hosted app: read and write on code, workflows, checks and statuses; CLI: a personal token |
| Licence | MIT | AGPL-3.0; the hosted app has separate terms |

Renovate covers more files. What it adds is already watched by `scripts/canon outdated` (mise tools and
every registry pin), and both ways of running it cost what the drivers rule out.

Rules:

1. **Python: indirect packages only.** Weekly, grouped into one pull request, seven-day cooldown, with
   the workspace's own two packages ignored. It changes `uv.lock` alone, so it lands as soon as
   `scripts/ci` passes.
2. **GitHub Actions.** Weekly, grouped, seven-day cooldown. Both actions are registry pins, so the bot's
   pull request fails `scripts/canon lint` until the registry entry moves on the same branch
   (`adopt-upstream`, "Moving an existing pin"). Lint then compares Dependabot's SHA with the one
   `scripts/canon resolve` derives from the tag: two sources for one pin.
3. **Direct Python dependencies and mise tools stay with the canon.** `scripts/canon outdated` reports
   them and the weekly refresh reads their release notes.
4. **Landing.** The daily watch lists the bot's open pull requests and the session brief prints them. An
   attended session lands each with `scripts/land --pr <n>`: its own worktree, `main` merged in if the
   branch is behind, then the usual checks, sign-off and squash merge. An unattended session only reports
   them (ADR 0011).
5. **The bot never merges.** Bot-side automerge is rare in the sample (2 of 23) and this repo's merge needs
   the `signoff` status, which only a run of `scripts/ci` posts.

Supersedes ADR 0014's "not adopted: a dependency bot". The rest of ADR 0014 stands.

### Consequences

* Good: the indirect packages are watched, and an action bump arrives with a second source for its SHA.
* Bad: GitHub runs Dependabot at a version this repo cannot pin. The registry records the source that was
  read, on a branch that moves daily, so the session brief always counts it as advanced. Nothing covers
  `mise.toml` beyond the canon's own check.
* First run, 2026-11-10, minutes after the config landed: all four Dependabot jobs completed. The uv
  job ran uv 0.13.7, the version pinned here (GitHub's docs still say "v0.11"). It read the workspace's
  lock, skipped the two workspace packages, checked 22 indirect packages and held back newer releases of
  four that are inside the cooldown. The actions job read both actions and their version comments.
  Neither had anything to update, so no pull request exists yet.
* Still not observed: an update being written. Upstream issue 14073 ("Transitive dependencies are not
  updated") is open; its fix merged on 2026-10-27. Whether the deployed service writes a correct `uv.lock`
  for this workspace shows when the first held-back release leaves the cooldown, within a week.

### Confirmation

`scripts/automation status` shows the config and the open pull requests from the last watch, and says so
when the watch could not ask. `scripts/land --pr` landed the pull request that added it, from a
checkout sitting on `main`. The first Dependabot run was read from the repository's Actions runs and job
logs; it is described under Consequences.

## Evidence

Read 2026-11-09. "Lane" means a research subagent fetched the page and a model summarised it.

* `canon/github-practice.json` and the 13 Dependabot configs of its repositories, fetched raw and read by
  the orchestrator: all 13 cover `github-actions`; fastapi uses `uv`; six set a cooldown (7 days at four,
  14 at two); seven run weekly, three monthly; nine use `groups`.
* https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference
  — `uv` is a `package-ecosystem`; `allow` `dependency-type: indirect` is supported for `uv`; `cooldown`
  with `default-days`, three days when unset; `groups`; weekly schedules (fetched, truncated; lane).
* https://docs.github.com/en/code-security/dependabot/ecosystems-supported-by-dependabot/supported-ecosystems-and-repositories
  — actions referenced by commit are updated, and "the version documentation ... when the comment is on
  the same line"; no mise ecosystem (fetched, truncated; lane).
* https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuring-dependabot-version-updates
  — "You enable Dependabot version updates by committing a `dependabot.yml` configuration file" (fetched;
  lane).
* https://docs.github.com/en/code-security/concepts/supply-chain-security/dependabot-on-actions — the jobs
  run on GitHub Actions runners, do not count toward included minutes, and run even where Actions is
  disabled for the repository (fetched; lane).
* https://github.com/dependabot/dependabot-core/issues/10478 — a contributor, 2026-01-14: Dependabot
  updates `uv.lock`; a report of a workspace root lock not updated, 2026-05-05, was told to open a
  separate issue (fetched; lane, second reader).
* https://github.com/dependabot/dependabot-core/issues/14073 and pull request 16686 — transitive uv
  updates were not made; the fix merged 2026-10-27 (fetched; second reader).
* https://raw.githubusercontent.com/dependabot/dependabot-core/main/LICENSE — MIT (fetched; `main`
  resolved to `de277ef139da`). At that commit `uv/Dockerfile` installs uv 0.13.7, and the lock parser gives
  editable workspace members no requirements, so they pass the `indirect` filter (fetched; second reader).
* https://docs.renovatebot.com/modules/manager/pep621/ — covers uv, `uv.lock` and workspaces; transitive
  entries "only updated to remediate a vulnerability"; https://docs.renovatebot.com/modules/manager/mise/
  (fetched, summarised; lane).
* https://docs.renovatebot.com/modules/manager/github-actions/ — updates the commit SHA for the tag in the
  comment; https://docs.renovatebot.com/security-and-permissions/ — the hosted app's permissions;
  https://docs.renovatebot.com/modules/manager/regex/ — custom managers. Whether a custom-manager change
  shares a pull request with the manifest change is UNVERIFIED (fetched; lane).
* https://github.com/renovatebot/renovate — AGPL-3.0-only; https://github.com/apps/renovate — the hosted
  app is "governed by separate terms of service", which were not read: UNVERIFIED (fetched; lane).
* `gh` 2.103.3's own help — `pr checkout --worktree`, `pr list --app dependabot` (bundled).
* The job logs of that first run, read through `gh run view --log` (fetched).
* Second reader, 2026-11-09 (`evidence-verifier`): the config's keys and values are valid and a pull
  request from the uv block changes only `uv.lock`; the counts from the 13 configs re-derive. It found the
  landing path broke for a branch behind `main` and when `main` is checked out in another worktree, the
  status row said "0 open" when nothing had been asked, and four evidence lines were wrong or unsourced.
  All are fixed above and in `scripts/land`.
