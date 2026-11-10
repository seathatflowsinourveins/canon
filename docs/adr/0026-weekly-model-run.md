---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# The weekly model run: a desktop scheduled task, gated by scripts, that reports and does not decide

## Context and Problem Statement

ADR 0011 layers the harness's upkeep: scripts run free and often, and a model runs weekly, only on what the
scripts flag. Claude Code documents five ways to run unattended (desktop scheduled tasks, cloud routines,
`/loop`, headless `claude -p`, GitHub Actions). Which one runs the weekly model session, and what bounds it?

## Decision Drivers

* A scheduled model run spends subscription usage every time; a script spends nothing (ADR 0006).
* An unattended run needs a stated end, a self-check, a boundary, a stop and an approver.
* When this was decided there was no git remote. There is one now (ADR 0014); the CLI is still not signed in.

## Considered Options

1. A desktop scheduled task.
2. A cloud scheduled task (formerly a routine).
3. Headless `claude -p` under launchd.
4. `/loop` or `CronCreate` in a session.
5. A GitHub Actions workflow. ADR 0011 records no reason for or against it for this run.

## Decision Outcome

Chosen: **option 1**.

| Layer | Runs | Mechanism | Tokens | Result goes to |
|---|---|---|---|---|
| Weekly refresh | Mondays 09:09, desktop app open | Scheduled task `canon-landscape-refresh` running the `landscape-refresh` skill | One session | A report |
| Unattended guard | Every tool call | `PreToolUse` hook `guard.sh` | None | Refuses the call |

The `Unattended guard` row, the enforcement in rule 2 and the guard's consequence are superseded by ADR 0020.

Rules (ADR 0011 holds rules 3 to 5):

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

Deferred, with the reason:

* **Cloud scheduled tasks** (formerly routines). A research preview that clones the repo on every run; not
  needed while the desktop task covers the weekly refresh.
* **Headless `claude -p` under launchd.** The CLI is not signed in, and it would need `--permission-mode
  dontAsk` and `--max-budget-usd` set.

### Consequences

* Good: the one model run is bounded and usually short.
* Bad: the weekly task needs the desktop app open (it catches up once on next launch).
* "Scripts gate the model" is this repo's inference from the cost guidance, not a recommendation in the docs.
* The guard reads the text of a call. It holds a cooperative or confused agent; it is not a sandbox. Its
  first read-only list let through four commands that could change or run something. The docs say hook
  edits reach a running session, yet a guard added mid-session did not act there; the cause is unknown.
* The weekly task has no hard cap on turns or spend. A desktop task exposes a model and a permission mode,
  not a budget; only its prompt tells it to stop.

### Confirmation

Weekly task, first run 2026-11-09 (started by hand, bypass mode): 80 seconds, 19 tool calls, marked
unattended by the hook, report delivered; it called a pytest development tag a newer release, a fault of
the release rule that `scripts/canon selftest` covers. Guard self-test the same day, in a fresh unattended
session (task `canon-guard-probe`, default mode): the read-only command ran and both attempted writes were
refused, with neither file created. Blocking under bypass mode was not provoked; the marking hook fired there.

## Evidence

* Split from ADR 0011 on 2026-11-10 with no change of decision: the text above is ADR 0011's own, moved,
  except the title, the Context, the Considered Options, the `Chosen` line, the line introducing the
  rules, and the first two Consequences bullets, which shorten ADR 0011's first two; the lines below keep
  their original dates and how each was checked. Every line below except the permissions and settings
  one supports both records and is kept whole in each.

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
* Restated 2026-11-10 against `scripts/automation` and `.githooks/pre-push`: rule 5, the commit gate,
  address check and watchdog rows, the rehearsal mode, and wording in Consequences and Confirmation.
