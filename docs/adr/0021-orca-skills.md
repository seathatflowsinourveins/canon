---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); master orchestrator (Claude)
review-by: 2026-12-10
---

# Orca's skills: used from sessions inside Orca, within written limits

## Context and Problem Statement

Orca's default agent setup installed two skills for every Claude Code session on the machine, `orca-cli`
and `orchestration` (ADR 0018). ADR 0004 asks that a third-party skill be pinned and read before it is
used, and ADR 0018 rule 7 left Orca's orchestration out because its docs call it experimental. The owner
then said the skills may be used if the workflow is seamless (2026-11-10). Is it, and under what limits?

## Decision Drivers

* The owner's word decides a tool that is cheap to leave (ADR 0018).
* A third-party skill is read before it is used: it runs with the agent's permissions (ADR 0004).
* Some rules do not move: landing through `scripts/land`, the owner's prompts, paper trading, and Claude
  Code as the only runtime (ADR 0009, ADR 0014, ADR 0018, ADR 0020).

## Considered Options

1. Use neither skill.
2. Use `orca-cli` only, to read state.
3. Use both from sessions inside Orca, within limits written in `AGENTS.md`.
4. Use both as installed.

## Decision Outcome

Chosen: **option 3**. Both skills are stubs. Each tells the agent to load a guide from the installed
`orca` command, so what they say may change with any app update and cannot be pinned. The guides were read
at 1.4.255. One trial ran the whole loop: a Claude Code session in an Orca workspace loaded
`orchestration`, started one Claude worker, ended its turn, was woken by Orca with the worker's report and
answered, in under a minute.

The guides allow much that this repo does not: other runtimes (their examples start Codex), a worker in
the caller's checkout, commands typed into Orca terminals, answering another agent's prompts, remote
placement, the browser and desktop control, plugins, automations and search across past sessions. The
stubs carry instructions of their own as well: to start Orca with `orca open`, to re-run a blocked command
"with escalated permissions", and to read a request for a review as a request for an Orca worker. The
limits:

1. **From inside Orca only.** Orca wakes a coordinator by typing into its pane, and a session Orca did not
   start has none. Such a session does not start Orca either: `orca open` hands the app that session's
   environment (ADR 0018).
2. **`--agent claude`, always.** The guides' examples start Codex and document no default; no other
   runtime is adopted (ADR 0018 rule 5).
3. **A worker that changes files gets its own workspace** (`--worktree new-top-level`). The caller's
   workspace is shared only for work that reads.
4. **Nothing goes through an Orca terminal to get past a prompt.** A push, a pin move and an install stay
   the owner's, and an agent does not answer another agent's permission prompt. A worker's change lands
   like any other: the review ADR 0017 requires, then `scripts/land`.
   ADR 0028 has since taken one pin move and an exactly pinned `uv add` off the prompts.
5. **The repo's own lanes come first.** Research, record checks and code review stay subagents (ADR 0017).
   An Orca worker is for work that needs its own workspace or that the owner asks to watch, and it runs on
   the model `canon/models.json` gives that work, named with `--model` when Orca's default differs.
6. **Not used:** remote servers and SSH placement, the browser and desktop control, plugins, automations,
   session search, and the other skills Orca bundles.

Supersedes ADR 0018 rule 7 in part: orchestration between Claude workers is used; between runtimes it
stays out. That sets aside ADR 0018's driver against building on what an upstream does not call stable:
Orca still labels orchestration "Experimental". It is used on the owner's word because nothing in this
repository depends on it.

It is also an exception to ADR 0004, none of whose four conditions is met. The skills cannot be pinned.
They are installed for the user, not the project. The registry names them under Orca's `tool` entry, not
as a `source`. And not every file was read: three references the `orca-cli` guide loads for the browser,
automations and plugins were not, since limit 6 leaves those out.

### Consequences

* Good: parallel work shows in the cockpit, each worker in its own workspace, and a coordinator is woken
  instead of polling.
* Bad: under its default Yolo setting Orca starts every worker with permissions bypassed (ADR 0018), so
  only the `ask` rules and the merge gate stand behind limit 4. The limits are text; nothing enforces
  them.
* Bad: the guides may change with any app update, about one a day, unread until the review date.

### Confirmation

Trial, 2026-11-10, Orca 1.4.255 and Claude Code 2.1.311, with `--worktree current --agent claude`. The
coordinator loaded the skill, ran two shell commands and reported a dispatch id. The worker counted the
records and reported. Orca typed "You have 1 orchestration message" into the coordinator's pane, and the
coordinator answered "20 files" with the same dispatch id. `git status` in the workspace was empty
afterwards. UNVERIFIED: that a session outside Orca cannot be woken (limit 1 infers it from how the wake
arrived); a worker in its own workspace, where the guide and its placement reference disagree on passing
`--repo`; a worker that edits and lands; more than one worker; a stopped or failed worker; and who
switched orchestration on under Orca's experimental settings.

## Evidence

Read 2026-11-10.

* `~/.agents/skills/orca-cli/SKILL.md` and `~/.agents/skills/orchestration/SKILL.md` — "This discovery
  stub loads the version-matched guide from the Orca executable used for this session", and the stubs' own
  instructions quoted above (read; second reader).
* `orca skills get orca-cli`, `orca skills get orchestration` and its five references, at 1.4.255 — the
  guides, 327 and 114 lines: `worker-start --spec "<task>" --worktree current --agent codex`; "end your
  turn"; "Orca wakes you"; "Add `--model` only if the user named one" (bundled; the second read in full by
  the orchestrator, all of them by an independent reader; second reader for the quotes).
* https://github.com/stablyai/orca/tree/v1.4.255/docs/site/content/docs — `cli/orchestration.mdx`:
  "Experimental", and agent ids `claude`, `codex`, `cursor` with no default named; `cli/skills.mdx`:
  "Default agent setup usually installs `orca-cli`, `orca-computer-use`, and `orchestration`" (fetched by
  the second reader).
* The trial — both agent terminals read through `orca terminal read`, and `git status` (run). The event
  ledger and the two transcripts show the same sequence between 14:04:58 and 14:05:37 UTC, both sessions
  in bypass mode (second reader).
* Independent reader, 2026-11-10: it listed what the guides allow beyond this repo's rules, which became
  the limits, and advised against using `orchestration` at all. The owner's instruction and the trial
  decide otherwise; its objections are the Bad consequences above.
* The owner's instruction, 2026-11-10: the skills may be used if the workflow is seamless.
* Second reader, 2026-11-10 (`evidence-verifier`): every quote held. It corrected the claim of a default
  runtime, what Yolo covers and how often the guides change, had the exceptions to ADR 0004 and to ADR
  0018's driver stated in full, and supplied the model and review limits. All are made above.
