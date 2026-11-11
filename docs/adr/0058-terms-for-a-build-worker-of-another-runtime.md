---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); orchestrator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# A build worker of another runtime: its own workspace, no landing, a Claude lane reviews and lands; not gated on the build eval, which keeps or retires it

## Context and Problem Statement

Every runtime but Claude Code read and reported here and never edited (ADR 0043, ADR 0045). The owner's
direction of 2026-11-10 is that more runtimes become workers under one orchestrator, and the owner's
decision of the same day is that the workflow is not gated: build work is assigned to Claude's build
lane and to the latest models of other families through the runtimes already adopted. Orca starts
those runtimes with no sandbox and no approvals, and no `ask` rule prompts for them. Under what terms
does one change files here?

## Decision Drivers

* The owner's word decides what is cheap to leave; what is untested is UNVERIFIED (ADR 0018).
* A model never blocks and checks decide the merge; every change is reviewed before it lands (ADR 0017).
* Paper only, and no credential in an agent's context (ADR 0009).
* No hook runs before a tool call (ADR 0020): a term is text, or a check after the fact.

## Considered Options

1. No other runtime builds.
2. Another runtime builds only after it passes the build eval (ADR 0057).
3. An adopted runtime builds now, in its own workspace; a Claude lane or the coordinator checks, reviews
   and lands; the build eval runs alongside and keeps or retires it.
4. As 3, and the worker lands its own change, as a Claude worker does.

## Decision Outcome

Chosen: **option 3**, on the owner's instruction. Option 2 was this record's first draft; the owner
decided against a gate. Nothing measured here supports the choice yet: no build eval has run, and
ADR 0005's table rests on other people's workloads.

1. **Who.** The adopted runtimes: Codex (an OpenAI model, on the model and effort ADR 0045 rule 7
   names), Antigravity (Google) and Muse Code (Meta) on their defaults (from 2026-11-10 set by ADR 0059:
   Antigravity's in its settings file; Muse's only as launch flags, `--model muse-wonder-1.0 --effort max`,
   so a Muse worker started without them is not on that default), and DeepSeek Harness only while
   `scripts/bootstrap --check` shows its review patch ok (ADR 0045 rule 6). A runtime with no adoption
   record builds nothing: OMP and Hermes have none.
2. **Terms**, as the Orca section of `AGENTS.md` now states them. The worker has its own workspace
   (`--worktree new-top-level`) and a task that names the paths it may change. It commits on its branch
   and reports. It never lands, pushes, opens a pull request, moves a pin, installs anything, starts
   another agent or a scheduled job, runs paper tests or reads a credential. A Claude lane or the
   coordinator checks that the branch changes only the task's paths and that nothing was pushed or left
   running, reproduces its tests, runs the review ADR 0017 requires and lands it with `scripts/land`.
3. **Not gated.** A worker may be given build work before its label has a trial. ADR 0057's eval is
   the measure that keeps or retires it (its rule 8), and one broken term retires it at once.
4. **Which paths stay with Claude is the owner's to say, and is not yet said.** The coordinator's
   recommendation, awaiting the owner: the adapter's order path, the gate, credential handling and the
   paper runner stay with Claude lanes until the eval has a result. This record adds one of its own:
   dot-directories, `AGENTS.md` and `CLAUDE.md`, since a repository file can configure a runtime (OMP's
   settings have a project layer, and its "built-in default is `yolo`").
5. **What no sandbox means for a worker that edits.** Each term is text; a reader was already so
   (ADR 0043 rule 2), but an editor has reason to run commands and write files, so a breach looks like
   work until someone reads what it did. The merge gate holds the hosted run of `scripts/ci`, but not
   the review: the `signoff` status is posted through this machine's `gh` sign-in, which an unsandboxed
   process can use. What holds is the check in rule 2, every time. A runtime's own sandbox would hold
   more (Codex has `--sandbox workspace-write`; OMP's approvals page describes none and says to "run
   automation in a suitably restricted account or container"; Hermes documents container backends);
   whether Orca can start a worker without its bypass flag is the owner's setting (ADR 0043). A runtime
   with memory, scheduled jobs or a messaging gateway, as Hermes has, is adopted as a builder only
   with those off.

UNVERIFIED when this takes effect: any build task by a runtime other than Claude Code; that such a
worker keeps to the terms with no sandbox, no approvals and no `ask` rule; that it follows this
repository's rules when it edits (glue, a named upstream, no new dependency); whether its changes land
as often, or after as few review rounds, as a Claude lane's; and the eval itself, which has not run.

Supersedes, for a worker given a build task: ADR 0043 rule 1 and ADR 0045 rule 1 ("never an edit,
commit"), and ADR 0021 limit 2 (`--agent claude`). A review stays under those records' terms.

### Consequences

* Good: work on other families' latest models starts now, with review and landing where they were.
* Bad: the first edits by these runtimes are real work in a real checkout, with the owner's `gh` in
  reach, before anything here has measured them. A Claude lane's time goes into checking and landing
  each change, and the terms rest on that check being done every time.
* Leaving: restore the bullet in `AGENTS.md`; the runtimes go back to reading; mark this superseded.

### Confirmation

The check before each review is recorded in the landing's review note, and `scripts/lanes --rates`
lists each worker's runtime. The first build tasks, and the first trial under ADR 0057, are recorded
here when they have run. The `evidence-verifier` read this record's first draft on 2026-11-10, when it
proposed option 2; its corrections to the merge gate, the sandbox and the OMP lines are in the text, and
the owner's decision came after its reading.

## Evidence

Read 2026-11-10.

* The owner's decision, 2026-11-10: the workflow is not gated; build work goes to the build lane's
  model and to other families' latest models through the adopted runtimes.
* ADR 0043 rule 2, ADR 0045 rules 2 and 6, `AGENTS.md` (Orca, Landing) and the header of `scripts/land`
  — the bypass flags, no `ask` rule for other runtimes, the `review` and `signoff` statuses posted
  through `gh` (read).
* `codex --help` at 0.167.0 — `--sandbox` "read-only, workspace-write, danger-full-access" (bundled).
* https://omp.sh/docs/approvals — "The built-in default is `yolo`"; "Run automation in a suitably
  restricted account or container"; "the same global, project, overlay, and runtime settings"
  (fetched); a project's file outranks the global one (second reader, from the page). That no trust
  prompt guards it is from a research report (lane), UNVERIFIED here.
* https://hermes-agent.nousresearch.com/docs/user-guide/security — `approvals.mode` "smart | manual |
  off"; `cron_mode`; "the messaging gateway"; its denylist "does not sandbox a hostile or compromised
  agent" (fetched). Its memory across sessions: the same report (lane).
* No published terms or entry bar for giving an agent write access to a repository were found (lane).
