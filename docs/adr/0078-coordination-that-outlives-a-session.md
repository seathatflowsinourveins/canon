---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-11 (ADR 0074)
review-by: 2026-12-11
---

# Coordination that outlives a session: recurring duties on launchd, the desktop scheduler or the hosted workflow, the queue and lane table in Orca's Run, decisions and round counts in git

## Context and Problem Statement

ADR 0071's stopping check 3 asks that `scripts/automation status` show every standing duty healthy, and
that none depend on a session timer. The maintenance agent took the reinstall and the runtime updates off
session timers (ADR 0065). What a coordinator still holds only in its session is lost when the session
exits, and blurred when it compacts: its timers, the queue of work not yet dispatched, which lane holds
which branch and workspace, the decisions made and what waits on the lead or the owner, and the review
round counts ADR 0073 needs. Where does each live so that a new coordinator reads it cold?

## Decision Drivers

* Prefer what an adopted upstream already provides over a new file format (`AGENTS.md`, rule 3).
* The repository is public: nothing naming an account, an address or the owner's private facts is tracked.
* Session-scoped scheduled tasks stop when the session exits, and recurring ones expire after seven days.
* Off the trading path: documentation and a landscape comparison suffice (ADR 0074).

## Considered Options

1. A progress file and a task list in the repository, read first by each session (Anthropic's pattern).
2. Orca's Run for the queue and lanes, git for what has landed, and each recurring duty on a scheduler
   that runs without a session, shown by `scripts/automation status`.
3. Keep it as it is: scratchpad files outside the repository and the coordinator's own timers.

## Decision Outcome

Chosen: **option 2**. It uses three upstreams already adopted (Orca, ADR 0018 and ADR 0021; launchd
through `scripts/automation`, ADR 0011; the desktop scheduler, ADR 0026) and adds no format. Option 1 is
the nearest documented pattern, and ADR 0071's gap 3 named it ("a handover is a file in the repository").
This record departs from both on purpose, and amends gap 3's closing text by a dated line in ADR 0071:
the queue names lanes, workspaces and waits on the owner, which change by the hour and do not belong in a
public history, and Orca already keeps such a ledger, with dependencies, statuses and decision gates,
outside the repository. What that pattern commits, what was done and what passed, is in git here already:
the records and the commit messages.

1. **A recurring duty runs where no session's exit drops it.** Work with no model in it runs from a
   launchd agent installed by `scripts/automation` (paper windows, ADR 0050; the lane watchdog and the
   maintenance agent, ADR 0011 and ADR 0065). Work that needs a model runs as a desktop scheduled task
   (the weekly refresh, ADR 0026), or on GitHub's schedule (the hosted watch, ADR 0064). Each shows as a
   row of `scripts/automation status`. A new recurring duty is one of these, with its row, never a
   `/loop` or a recurring `CronCreate` in a coordinator.
2. **The queue and the lane table are the coordinator's Orca Run.** A piece of work is a task
   (`task-create`, with `--deps` for what it waits on). Its status says where it is (pending, ready,
   dispatched, completed, failed, blocked). Who holds it and in which workspace is the dispatch
   (`worker-list --run <id>` gives each dispatch's agent and workspace; the branch is that workspace's
   `git branch --show-current`). A wait on the lead or the owner is a decision gate on the task
   (`gate-create`), answered there (`gate-resolve`). A run owed at a market time, such as a paper test
   that needs the regular session, is a `blocked` task whose title names when it runs. The store is a
   database in Orca's application folder, outside the repository, so it may name what the public history
   may not; a credential never goes in.
3. **Decisions and round counts live in git once they land**: a record or a commit message, with its
   date and who made it; ADR 0073's counts are in each fix's message (its rule 5). Before it lands, a
   decision is the task's result (`task-update --result`) or a resolved gate.
4. **A new coordinator, starting cold, reads in this order:** `AGENTS.md` and the session brief;
   `orca orchestration run-list`, then `run-use --id <run>` to take the Run over, `task-list --ready`,
   `gate-list` and `worker-list`; then `git log` on `main`; then `scripts/automation status`.
5. **What the session start prints.** The brief gains one static line naming item 4's reads, and the
   line printed after a compaction points at the Run instead of a state file. Both are static text, so a
   hook never waits on the app. A follow-up lane builds them in `.claude/hooks/session-brief.sh` and
   `.claude/hooks/compact-brief.sh`, with a case in `scripts/test-hooks`.

**Left in sessions, as limits.**

* *A one-shot reminder* (read the first regular window, report a result at an hour) stays a session's
  one-shot `CronCreate`: a wake-up, not a duty. What it points at is a task or a status row, so a lost
  reminder delays a report and drops nothing.
* *An owed paper run waits for a coordinator.* No agent runs a queued `paper:test` selection at the
  open; with no coordinator during that session, it waits a session. A runner would be a new automation,
  which ADR 0071 sends to its own record.
* *The coordinator's reasoning* is the session's own; the compaction summary keeps what `CLAUDE.md`'s
  compact instructions name. The owner's private facts stay in the memory folder outside the repository.
* *Orca's orchestration is labelled experimental* (ADR 0021), and its store is on this Mac only. If a
  release changes it, the tasks are recreated from `git log` and the reports, which is cheap.

### Consequences

* Good: a coordinator that exits or compacts loses no duty and no queue; the next one takes over the Run.
* Good: nothing private reaches the public history, and no new format is kept.
* Bad: the ledger is only as current as the coordinator keeps it; a task left `dispatched` after its
  worker settled reads as live until `worker-list` is checked.
* Bad: this record places the duties; it does not make them healthy. On 2026-11-11 the paper session row
  read FAILED (no market data in a pre-market window), the maintenance row UNKNOWN (no round yet), and the
  weekly refresh row reads ok from its task file alone, its schedule visible only in the desktop app.

### Confirmation

* 2026-11-11: item 5 is built. `.claude/hooks/session-brief.sh` and `.claude/hooks/compact-brief.sh` print
  static lines naming item 4's reads, for coordinators only, and run none of them; `scripts/test-hooks`
  checks both.
* `scripts/automation status` lists every recurring duty; a coordinator starts no `/loop` and no
  recurring `CronCreate` (`CronList` alone cannot show it: a self-paced loop's wakeup lists as one-shot).
* A coordinator's handover is `run-use` on its Run. That a new terminal takes over a Run another
  coordinator held, with its workers' mail, is UNVERIFIED: the help says only "Resume coordinating a
  Run", and the bundled guide that "`run-use` keeps only the Run it names". The first handover tries it.
* Measured 2026-11-11: `orca orchestration run-list` listed the Run created on 2026-11-10, its
  `task-list` the tasks completed that day, and `worker-list` 282 dispatches with each one's workspace; the
  store is `orchestration.db` in Orca's application folder. Whether it survives an Orca update that
  restarts the app is UNVERIFIED (Orca was at 1.4.255 on both days).

## Evidence

Read 2026-11-11.

* https://code.claude.com/docs/en/scheduled-tasks.md — "Tasks are session-scoped"; "Closing the terminal
  or letting the session exit stops them firing"; "Recurring tasks automatically expire 7 days after
  creation"; on `--resume` tasks are restored but a self-paced `/loop` is not; for scheduling that
  survives a session, cloud, desktop scheduled tasks or GitHub Actions (fetched, read in full).
* https://code.claude.com/docs/en/desktop-scheduled-tasks.md — a local task "starts a fresh session" when
  due, "only fires while the app is open and your computer is awake", one catch-up run for missed times,
  its prompt in `~/.claude/scheduled-tasks/<task-name>/SKILL.md` (fetched, read in full).
* https://code.claude.com/docs/en/context-window.md — after compaction, project-root `CLAUDE.md` and auto
  memory are re-injected from disk, context that hooks added earlier is summarized, and SessionStart hooks
  matching `compact` run and join the compacted context (fetched, read in full).
* https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents — a
  `claude-progress.txt` log, a JSON feature list whose `passes` field is the only one agents change,
  progress committed "with descriptive commit messages", and each session, after `pwd`, reads "the git
  logs and progress files" (fetched, summarised by the fetch tool).
* Orca 1.4.255, `orca orchestration --help` and the help of `run-use` ("Resume coordinating a Run"),
  `task-create` (`--deps`), `task-update` (statuses pending, ready, dispatched, completed, failed,
  blocked), `gate-create` ("a decision gate blocking a task"), `gate-list` and `worker-list`; the bundled
  `orchestration` guide and its coordinator-loop reference (`orca skills get`) (bundled, read).
* `orca orchestration run-list`, `task-list --run`, `worker-list --run --json` and `gate-list --run` on
  this machine; the store's file name under Orca's application folder (run, 2026-11-11).
* `scripts/automation status` on 2026-11-11: the rows and their states quoted under Consequences (run).
* ADR 0011, 0026, 0050, 0064, 0065 for where each duty runs; ADR 0018 and 0021 for Orca's terms; ADR 0071
  gap 3 and check 3; ADR 0073 rule 5; ADR 0074 (read).
* A research report of 2026-11-10 comparing eight harnesses and four vendors' docs found a task ledger
  in three harnesses (gastown, AgentField, Anthropic's) and no vendor; a pointer only, those harnesses not
  fetched here (UNVERIFIED by this record).
* An `evidence-verifier` pass on 2026-11-11 corrected the seven-day expiry to recurring tasks, a wrong
  rule citation, the `CronList` test, the summary of Anthropic's pattern, and marked the `run-use`
  takeover UNVERIFIED (run).
