---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); master orchestrator (Claude)
review-by: 2026-12-10
---

# Agent hosts and other runtimes: Orca is the cockpit, Claude Code the only runtime, one gate for whatever runs

## Context and Problem Statement

The owner asked (2026-11-10) to run Claude Code inside Orca, a desktop app that gives each task its own
git worktree and agent terminal, and to orchestrate more than one agent runtime. Such a host is a cockpit:
it starts the installed `claude` in a worktree and shows many sessions side by side. It is not a second
runtime. What does the repository commit for it, and what holds a second runtime to this repo's rules?

## Decision Drivers

* A host must launch the installed CLI, so this repo's settings, hooks and instructions apply unchanged.
* A tool that is cheap to leave is adopted on the owner's word and proven in use. Agreement across other
  repositories is asked of what is hard to reverse: a foundation, a format, the merge workflow.
* Enforcement must not depend on which host or runtime produced a change (ADR 0013, ADR 0017, ADR 0020).
* Paper trading only, and no credentials in an agent's context (ADR 0009).
* Nothing is built here on a feature its upstream does not call stable.

## Considered Options

1. Claude Code's own parallel sessions only (subagents, worktrees, the desktop app).
2. Orca as the cockpit, with Claude Code sessions inside it.
3. Another host: Conductor, Paradev or cmux.
4. A host's orchestration layer handing work between runtimes (Orca's `orca orchestration` commands).
5. A second runtime exposed to Claude Code as an MCP server.

## Decision Outcome

Chosen: **option 2**. Orca is the cockpit and Claude Code stays the only runtime. Option 1 keeps working
beside it; options 4 and 5 are not adopted. (The Codex CLI is since adopted as a reviewer: ADR 0043.)

This record first asked whether a host's file had converged across public repositories, and left the
request undone. That was the wrong test: a cockpit is a personal tool, as an editor is. Orca is chosen
because the owner named it, its licence is MIT, its source runs the installed `claude`, and leaving it is cheap.

1. **Orca is in the registry as a `tool`, followed on its `main` branch.** It auto-updates by default on
   its stable channel, about a release a day, so no pin here would say what is installed. The pages cited
   are the same at v1.4.255 and at the pinned commit.
2. **`orca.yaml` holds two documented keys.** `scripts.setup` runs `uv sync --locked` through mise in a
   new worktree, when setup is enabled in Orca and the command approved there. `setupAgentStartupPolicy:
   wait-for-setup` starts the agent only after setup has succeeded. The repository works without the file:
   `scripts/ci` passes in a worktree with no setup step.
3. **This repository's rules do not depend on the host.** It registers no hook before a tool call (ADR
   0020); the merge gate and the absence of live credentials hold whichever app starts a session. Orca
   adds hooks of its own to the user's settings, one before every tool call: each hands the event to the
   local app and exits 0, and that one prints back the app's answer when there is one. Its "Agent status
   hooks" setting removes them.
4. **Landing is `scripts/land` and nothing else.** A host's commit, push and pull request controls are
   not a landing path: the ruleset on `main` requires the `signoff` status that `scripts/land` posts for
   the commit it has checked (ADR 0014, ADR 0017).
5. **Claude Code is the only runtime adopted.** Anthropic: "In every approach the workers are Claude
   sessions. To involve a different tool, expose it to Claude as an MCP server." OpenAI has removed
   `codex mcp-server`, which leaves option 5 no documented route to Codex today.
   Superseded by ADR 0043: the Codex CLI is adopted as an Orca worker that reads a change and reports.
6. **A second runtime is adopted like any upstream**, when there is a job for it: its own worktree and
   branch, landing through rule 4 with the review ADR 0017 requires, and no credentials in its environment.
7. **Not adopted: option 4.** Orca labels its orchestration commands "Experimental" and no other host
   documents a handoff. Runtimes meet at a branch and a pull request. Superseded in part by ADR 0021.

### Consequences

* Good: one window shows every session, each in its own worktree with its environment built.
* Orca starts Claude Code with `--dangerously-skip-permissions`, its "Yolo" launch, unless Agent
  Permissions in its settings is switched to Manual or the agent's launch arguments are edited. Under Yolo
  the `ask` rules still prompt (ADR 0020).
* Leaving Orca means deleting `orca.yaml`, the registry entry and its pin, and the rows that name them in
  `AGENTS.md` and `README.md`. Its hooks stay in the user's settings until its setting removes them.
* Bad: no pin says what runs, so no changelog is read before the app changes.
* Orca's GitHub panel reports the GitHub CLI as missing until `gh` has a default outside this repository
  (`mise use -g gh@<version>`): Orca takes its PATH from an interactive login shell, where the mise shim
  fails without one. `scripts/land` is unaffected; it calls `gh` through `mise exec` inside the checkout.
* On a machine, Orca also sends anonymous usage events until Settings → Privacy turns them off. Two
  skills, `orca-cli` and `orchestration`, were installed for every session seconds after the app started,
  which its docs put down to its default agent setup; they are used within the limits of ADR 0021. What
  wrote `skipDangerousModePermissionPrompt` into the user's settings is UNVERIFIED. Orca also loads a
  plugin of its own, `orca-agent-view`, into each terminal session: ADR 0023, which also says when an
  `ask` rule does not prompt.

### Confirmation

First run, 2026-11-10: Orca 1.4.255 and Claude Code 2.1.311, driven through `orca worktree create --agent
claude --setup inherit`. Orca made the worktree under `~/orca/workspaces/canon/` on a branch from
`origin/main`, ran the setup command (the repository's policy in Orca was `run-by-default`), started the
agent only after "Setup finished", and marked the worktree trusted. The session wrote that tree's own
`.canon/watch.json` and quoted its brief, "canon: 37 upstreams pinned". `scripts/ci` passed there,
`scripts/land --dry-run` ran there, and `scripts/bootstrap --check` passed afterwards: Orca's hooks sit
beside the ledger's and the status line is untouched. A first attempt opened Orca from an agent session's
shell; the app and the agents it started then carried that session's environment. Open Orca from the Dock.
UNVERIFIED: landing a real change from an Orca worktree, the app's answer to a tool call in Manual mode,
and whether the app asks before it runs the setup command.

## Evidence

Read 2026-11-10. "Lane": a research subagent fetched and summarised the page. "Second reader": the verifier.

* https://github.com/stablyai/orca/tree/v1.4.255/docs/site/content/docs — tag object `63ba9c6212d1`,
  published 2026-11-09, MIT. `model/orca-yaml.mdx`: the two keys, "All keys are optional"; setup "Runs in
  the new worktree when setup is enabled"; "A valid file does not bypass Orca's command approval";
  "`wait-for-setup` waits for successful setup before agent startup". `cli/orchestration.mdx`:
  "Experimental". `agents/hooks-memory.mdx`: a repo's hooks "will run when Orca launches the agent in a
  worktree of that repo", and the status-hooks setting. `telemetry.mdx`: the switches (fetched; second
  reader). `model/agents-sessions.mdx`: Orca launches "Claude with `--dangerously-skip-permissions`";
  `agents/supported.mdx`: "Settings → Agents → Agent Permissions" switches "between **Yolo** and
  **Manual** launches"; `cli/reference.mdx`: the `worktree create` flags; `cli/skills.mdx`: "Default agent
  setup usually installs `orca-cli`, `orca-computer-use`, and `orchestration`" (fetched; second reader).
* https://github.com/stablyai/orca/tree/main/src — `tui-agent-config.ts` runs `claude`; `hook-settings.ts`
  and `claude-folder-trust-file.ts` write the hooks and the trust entry; `claude-managed-hook-events.ts`
  installs `PreToolUse` and `PermissionRequest` hooks with matcher `*` (lane; second reader). At v1.4.255,
  `startup/shell-path-probe.ts` asks the shell for its PATH with `-ilc` (fetched; second reader).
* https://www.onorca.dev/docs/install — "Orca auto-updates by default, tracking the stable channel"
  (second reader). GitHub releases: 24 stable ones from 2026-10-17 to 2026-11-09.
* https://code.claude.com/docs/en/agents.md — the sentence in rule 5;
  https://learn.chatgpt.com/docs/mcp-server — `codex mcp-server` and its standalone binary "have been
  removed", its replacement "isn't an MCP server" (lane; second reader).
* The first run on the owner's machine — the process list, the agent terminal read through `orca terminal
  read`, the event ledger, the worktree, and the user's settings before and after: sixteen hook entries
  added (`PreToolUse` and `PermissionRequest` among those with matcher `*`), each with a 10-second timeout
  and no `onFailure`; their script posts to `127.0.0.1` or spools the event and always ends in `exit 0`
  (read, run; second reader).
* `mise.toml` here and `gh --version` from a clean interactive shell: with no global mise config it failed
  in the home folder ("No version is set for shim: gh") and ran in the checkout; after `mise use -g` it
  ran in both. `scripts/land` — `"$MISE" exec -- gh`. The two skill folders under `~/.agents/skills/`,
  linked from `~/.claude/skills/`, are dated eight seconds after the app's start (run, read; second
  reader). UNVERIFIED: that the panel then finds `gh`.
* Second reader, 2026-11-10: every quote held in each version. This one was changed that day from
  `proposed` with option 1, on the owner's correction that a host is a cockpit and not an alternative to
  Claude Code; the reader's corrections are made above.
* ADR 0043, 2026-11-10 — supersedes rule 5 on the owner's instruction to use other runtimes as Orca
  workers where that raises quality; pointer lines were added to rule 5 and to the outcome, and nothing else changed.
* Refresh, 2026-11-10 (fetched; pages summarised by a small model; decision unchanged). Two projects
  entered the registry as `watch`, not adopted, nothing installed: `agent-orchestrator` and `gascity`.
  * https://github.com/OrchestratorInc/agent-orchestrator (Apache-2.0, `main`): "a local desktop workspace",
    and "The desktop app runs the daemon for you"; each task gets its own agent, branch and worktree. It is
    a desktop host in the same class as Orca. https://docs.orchestrator.inc/migration/: the npm package
    `@aoagents/ao` is "frozen at `0.10.0`" and "will receive no new features or fixes"; the `ao` command is
    "a bridge to fetch and open the desktop build". The releases page shows nightly pre-releases several
    times a day; stable tags v0.14.8 to v0.14.10 exist (GitHub API, not that page). The npm registry lists
    `@aoagents/ao` 0.10.3 as latest (12 Jul 2026), which disagrees with the docs' "frozen at `0.10.0`".
    The repository's former owner name resolves through the GitHub API to this one, so it was transferred or
    renamed; no notice was read.
  * https://github.com/gastownhall/gascity (MIT, `main`): "Orchestration-builder SDK for multi-agent coding
    workflows"; work is stored "in beads, which keeps its data in Dolt". A framework built on a work-item
    store, not a desktop host. Latest stable v1.5.1, 24 Oct 2026. https://yegge.ai/gastown: Gas Town was
    "Shipped at v1.0 in 2026, now retired. Its successor is Gas City." The Gas Town repository
    (gastownhall/gastown) is not archived (GitHub API) and had a commit on 10 Nov 2026; its last release is
    v1.2.1, 6 Jun 2026.
  * UNVERIFIED: AO's permission model and cost tracking (its migration page covers neither; its README
    mentions "needs approval" notifications in a mobile section and no permission model). For Gas City only
    release-note lines were read: v1.5.1 "a security fix for Claude permission prompts", cost estimates priced
    for the Claude 5 family, and an agent guide covering Claude's builtin `permission_mode = "unrestricted"`
    default; its permission model is otherwise UNVERIFIED.
  * https://raw.githubusercontent.com/stablyai/orca/main/docs/site/content/docs/cli/orchestration.mdx
    (fetched with curl, line 12): `<Callout title="Experimental">`, "Enable orchestration under Settings →
    Experimental before using these commands." An earlier summary of the page missed the callout; rule 7
    stands.
  * https://github.com/OrchestratorInc/agent-orchestrator/releases, release `v0.14.11-nightly.202611101246`,
    10 Nov 2026: "Add a Move from Conductor guide for the Cloud shutdown". Conductor was one of the hosts
    weighed under option 3; the note concerns its cloud service, and the shutdown itself is not read at
    Conductor's own source. Secondary reports (unite.ai, cellcog.ai; not fetched by the author) say Meta
    acquired Conductor and that its cloud shuts down on 20 Nov 2026: UNVERIFIED.
