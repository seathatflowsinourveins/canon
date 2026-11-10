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
| Commit gate | Every `git commit` | `.githooks/pre-commit` through `core.hooksPath` | None | Aborts the commit |
| Hosted CI | Every pull request and push to `main` | GitHub Actions running `scripts/ci` (ADR 0014) | None | A required check |
| Edit lint | Every edit under `canon/`, `docs/adr/` | `PostToolUse` hook (ADR 0003) | None | Feedback to Claude |
| Hosted watch | Once a day, on GitHub | Scheduled workflow; `canon check` + `canon outdated` (ADR 0017) | None | The run's summary; a failed run if a tag was re-pointed |
| Landscape watch | Once a day, at session start | Async `SessionStart` hook; `canon check` + `canon outdated` | None | `.canon/watch.json`, printed at the next session start |
| Lane watchdog | Every 10 minutes, Mac awake | launchd user agent running `lanes --json` | None | macOS notification, `alerts.jsonl`, status file |
| Weekly refresh | Mondays 09:09, desktop app open | Scheduled task `canon-landscape-refresh` running the `landscape-refresh` skill | One session | A report |
| Unattended guard | Every tool call | `PreToolUse` hook `guard.sh` | None | Refuses the call |

Rules:

1. **Scripts gate the model.** The weekly run starts with `canon check`, `canon outdated` and the expired-ADR
   list. If all three are clean it reports that in three lines and stops.
2. **Unattended runs report; they do not decide.** They never move a pin, change an ADR's status, commit,
   push, or touch anything outside the repo. The owner or orchestrator approves the report. This is
   enforced, not only asked. A scheduled task's prompt starts with a marker; a `UserPromptSubmit` hook
   records that session as unattended; `guard.sh` then refuses every edit, every fetch outside a list of
   documentation hosts, and every shell command outside a short list of read-only forms. The weekly task
   was saved in bypass-permissions mode, where nothing else would ask. Claude Code's docs say a
   `PreToolUse` hook runs before the permission prompt and that exit 2 blocks before permission rules are
   evaluated. That this holds in bypass mode is this repo's inference, backed by the marking hook firing in
   a bypass run; the docs do not say "every mode".
3. **No hook wakes the model on its own.** `asyncRewake` is not used: on exit code 2 it starts a turn even
   when the session is idle.
4. **Automation never deletes data.** The watchdog reports the ledger's size; pruning is a manual act.
5. **One view, one off switch.** `scripts/automation status` shows every layer; `scripts/automation uninstall`
   removes what `install` added.

`scripts/canon check` separates two upstream changes: **RETAGGED** (a pinned release tag now points at a
different commit; a supply-chain warning, exit 3) and **advanced** (a tracked branch has new commits; normal).

Deferred, with the reason:

* **Cloud scheduled tasks** (formerly routines). A research preview that clones the repo on every run; not
  needed while the desktop task covers the weekly refresh.
* **Headless `claude -p` under launchd.** The CLI is not signed in, and it would need `--permission-mode
  dontAsk` and `--max-budget-usd` set.

### Consequences

* Good: the frequent jobs cost nothing; the one model run is bounded and usually short.
* Bad: nothing runs while the Mac sleeps, and the weekly task needs the desktop app open (it catches up once
  on next launch). Notification delivery from the background agent is unproven until a real alert fires;
  the status file records whether each one was delivered.
* "Scripts gate the model" is this repo's inference from the cost guidance. The Claude Code docs do not
  state it as a recommendation.
* The guard reads the text of a call. It holds a cooperative or confused agent; it is not a sandbox. Its
  first read-only list let through four commands that could change or run something; a second reader found
  them and they are fixed and tested. Claude Code's docs say hook edits reach a running session, yet a guard
  added mid-session did not act in the session that added it. The cause is not established.
* The weekly task has no hard cap on turns or spend. A desktop task exposes a model and a permission mode,
  not a budget; only its prompt tells it to stop.

### Confirmation

`scripts/automation status`. Tested 2026-11-09: version ordering (`v2.0.0rc10` < `v2.0.0` < `v2.0.1rc1`);
a simulated re-pointed tag gives RETAGGED and exit 3; the watch skips inside 24 hours; the watchdog alerts
once per session and flag, and treats a hostile directory name as text.

Weekly task, first run 2026-11-09 (started by hand, bypass mode): 80 seconds, 19 tool calls, marked
unattended by the hook, report delivered. It called a pytest development tag a newer release; that was the
release rule's fault, now fixed and covered by `scripts/canon selftest`. Guard self-test the same day, in
a fresh unattended session (task `canon-guard-probe`, default mode): the read-only command ran and both
attempted writes were refused, with neither file created. Blocking under bypass mode itself was not
separately provoked; the docs say hooks run in every mode, and the marking hook did fire in the bypass run.

Installed 2026-11-09. launchd reports the watchdog loaded, one run, exit code 0. In a throwaway copy of the
repo, git refused a commit with an evidence-free ADR and one with a broken script, then accepted a clean one.
The first real install crashed: the script's own `install` function shadowed the system command and
recursed. It loaded nothing, was fixed, and the installer gained a rehearsal mode
(`AUTOMATION_SKIP_LAUNCHD=1`) that was run before the second attempt.

## Evidence

Read 2026-11-09 (fetched; fetches are model-summarised).

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
* https://code.claude.com/docs/en/permissions.md — `PreToolUse` hooks run before the permission prompt, and
  exit 2 blocks before permission rules are evaluated; https://code.claude.com/docs/en/settings.md — edits
  to `hooks` reach a running session without a restart (both read by the second reader).
* Second reader, 2026-11-09 (`evidence-verifier`): the scheduling, hook and launchd facts are supported.
  Four statements were false and are corrected: that there was no remote, that the guard's list was
  read-only, that the docs say hooks run in every mode, and that uninstall reversed everything install
  added. The status command's weekly row now says it can see only the task file.
* https://code.claude.com/docs/en/hooks.md — `async: true` runs in the background and cannot block; an async
  `SessionStart` hook is allowed; `asyncRewake` wakes Claude on exit code 2 even when idle.
* https://git-scm.com/docs/githooks — a non-zero exit from `pre-commit` aborts the commit; and
  https://git-scm.com/docs/git-config — `core.hooksPath` (read from the man pages of git 2.54.0 on this Mac;
  the two URLs are the same pages online and were not fetched).
* `launchd.plist(5)`, `launchd(8)` and `launchctl(1)` on macOS 27.2 — `StartInterval` starts a job every N
  seconds and skips intervals missed during sleep; `~/Library/LaunchAgents` holds per-user agents;
  `bootstrap`, `bootout`, `print` (read from the local man pages; https://support.apple.com/guide/terminal/apdc6c1077b-5d5d-4d35-9c19-60f2397b2369/mac
  is Apple's overview and was not fetched: UNVERIFIED).
