---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Automation: scripts run free and often; a model runs weekly, only on what the scripts flag

## Context and Problem Statement

The owner asked for the harness's upkeep to be automated. Claude Code documents five ways to run unattended
(desktop scheduled tasks, cloud routines, `/loop`, headless `claude -p`, GitHub Actions), and the OS and git
add their own. Which mechanism runs which job?

## Decision Drivers

* A scheduled model run spends subscription usage every time; a script spends nothing (ADR 0006).
* An unattended run needs a stated end, a self-check, a boundary, a stop and an approver.
* When this was decided there was no git remote. There is one now (ADR 0014); the CLI is still not signed in.
* Standing automation must be visible in one place, removable in one command, and loud when broken.

## Considered Options

1. Run every job as a scheduled model session.
2. Put every check in CI.
3. Layers: deterministic checks at the cheapest trigger that catches the problem, a model only for judgement.

## Decision Outcome

Chosen: **option 3**.

| Layer | Runs | Mechanism | Tokens | Result goes to |
|---|---|---|---|---|
| Commit gate | Every `git commit` | `.githooks/pre-commit` through `core.hooksPath`; since 2026-11-13 the fast checks and test collection (`scripts/ci --staged`, ADR 0138) | None | Aborts the commit |
| Address check | Every `git push` | `.githooks/pre-push` running `scripts/check-addresses`, through `core.hooksPath` | None | Aborts the push |
| Hosted CI | Every pull request and push to `main` | GitHub Actions running `scripts/ci` (ADR 0014) | None | A required check |
| Edit lint | Every edit under `canon/`, `docs/adr/` | `PostToolUse` hook (ADR 0003) | None | Feedback to Claude |
| Hosted watch | Once a day, on GitHub | Scheduled workflow; `canon check` + `canon outdated` (ADR 0017) | None | The run's summary; a failed run if a tag was re-pointed or the lock is stale |
| Landscape watch | Once a day, at session start | Async `SessionStart` hook; `canon check` + `canon outdated` | None | `.canon/watch.json`, printed at the next session start |
| Lane watchdog | Every 10 minutes, Mac awake | launchd user agent running `lanes --json`, given the install's `CLAUDE_CONFIG_DIR` | None | macOS notification, `alerts.jsonl`, status file |
| Weekly refresh | Mondays 09:09, desktop app open | Scheduled task `canon-landscape-refresh` running the `landscape-refresh` skill (ADR 0026) | One session | A report |

How the weekly model run is started and bounded is ADR 0026: its mechanism, rules 1 and 2, the
`Unattended guard` row and what was deferred. The `Unattended guard` row, the enforcement in rule 2 and
the guard's consequence are there, and ADR 0020 supersedes them there. Rules 3 to 5 stay here.

Rules:

3. **No hook wakes the model on its own.** `asyncRewake` is not used: on exit code 2 it starts a turn even
   when the session is idle.
4. **Automation never deletes data.** The watchdog reports the ledger's size; pruning is a manual act.
5. **One view, one off switch.** `scripts/automation status` shows every layer and exits non-zero when any
   row is not ok, UNKNOWN included, except the dependency bot's OFF row when there is no GitHub remote;
   `scripts/automation uninstall` removes what `install` added and exits non-zero naming what it could not.

`scripts/canon check` separates two upstream changes: **RETAGGED** (a pinned release tag now points at a
different commit; a supply-chain warning, exit 3) and **advanced** (a tracked branch has new commits; normal).
**STALE** (exit 4) is this repository's own state: the lock no longer matches the registry until it is resolved.

### Consequences

* Good: the frequent jobs cost nothing; the one model run is bounded and usually short.
* Bad: nothing runs while the Mac sleeps, and the weekly task needs the desktop app open (it catches up once
  on next launch). Notification delivery from the background agent is unproven until a real alert fires;
  the status file records whether each one was delivered.
* The consequences of the weekly run and of the guard are in ADR 0026.

### Confirmation

`scripts/automation status`. Tested 2026-11-09: version ordering (`v2.0.0rc10` < `v2.0.0` < `v2.0.1rc1`);
a simulated re-pointed tag gives RETAGGED and exit 3; the watch skips inside 24 hours; the watchdog alerts
once per session and flag, and treats a hostile directory name as text.

The weekly task's first run and the guard self-test are in ADR 0026.

Installed 2026-11-09: launchd reports the watchdog loaded, one run, exit code 0. In a throwaway copy, git
refused a commit with an evidence-free ADR and one with a broken script, and accepted a clean one. The
first install crashed (a function shadowed the system `install`) and loaded nothing; the rehearsal mode
`AUTOMATION_SKIP_LAUNCHD=1`, every step except the launchctl calls, ran before the second.

## Evidence

Read 2026-11-09 (fetched; fetches are model-summarised). The permissions and settings line moved to ADR
0026 when it was split from this record on 2026-11-10, with no change of decision; the lines below that
support both records are kept in each, and the 2026-11-10 restatement line names here only what this
record holds.

* https://code.claude.com/docs/en/desktop-scheduled-tasks.md — runs locally in a chosen folder; app must be
  running and the Mac awake; one catch-up run for a missed time within 7 days; each run is a fresh session;
  model and permission mode are set per task; stored in `~/.claude/scheduled-tasks/<name>/SKILL.md`.
* https://code.claude.com/docs/en/routines.md — cloud routines clone the repo from GitHub on every run.
* https://code.claude.com/docs/en/scheduled-tasks.md — `/loop` and `CronCreate` are session-scoped and
  recurring tasks expire after 7 days, so they do not suit standing automation.
* https://code.claude.com/docs/en/best-practices.md — before leaving a run unattended: what done looks like,
  how it checks its work, what it may touch, what stops it, who approves.
* https://code.claude.com/docs/en/headless.md — `--permission-mode` and `--max-budget-usd`; `--max-turns`
  is on the best-practices page.
* Second reader, 2026-11-09 (`evidence-verifier`): the scheduling, hook and launchd facts are supported.
  Four statements were false and are corrected: that there was no remote, that the guard's list was
  read-only, that the docs say hooks run in every mode, and that uninstall reversed everything install
  added. The status command's weekly row now says it can see only the task file.
* Restated 2026-11-10 against `scripts/automation` and `.githooks/pre-push`: rule 5, the commit gate,
  address check and watchdog rows, the rehearsal mode, and wording in Confirmation.
* https://code.claude.com/docs/en/hooks.md — `async: true` runs in the background and cannot block; an async
  `SessionStart` hook is allowed; `asyncRewake` wakes Claude on exit code 2 even when idle.
* https://git-scm.com/docs/githooks — a non-zero exit from `pre-commit` aborts the commit; and
  https://git-scm.com/docs/git-config — `core.hooksPath` (read from the man pages of git 2.54.0 on this Mac;
  the two URLs are the same pages online and were not fetched).
* `launchd.plist(5)`, `launchd(8)` and `launchctl(1)` on macOS 27.2 — `StartInterval` starts a job every N
  seconds and skips intervals missed during sleep; `~/Library/LaunchAgents` holds per-user agents;
  `bootstrap`, `bootout`, `print` (read from the local man pages; https://support.apple.com/guide/terminal/apdc6c1077b-5d5d-4d35-9c19-60f2397b2369/mac
  is Apple's overview and was not fetched: UNVERIFIED).
