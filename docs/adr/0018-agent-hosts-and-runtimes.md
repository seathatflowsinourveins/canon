---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); master orchestrator (Claude)
review-by: 2026-12-10
---

# Agent hosts and other runtimes: Orca is the cockpit, Claude Code the only runtime, one gate for whatever runs

## Context and Problem Statement

The owner asked (2026-11-10) to run Claude Code inside Orca, a desktop app that gives each task its own git
worktree and agent terminal, and to orchestrate more than one agent runtime. A host of this kind is a
cockpit: it starts the installed `claude` in a worktree and shows many sessions side by side. It is not a
second runtime. What does the repository commit for the cockpit, and what holds a second runtime to this
repo's rules?

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
beside it; options 4 and 5 are not adopted.

This record first asked whether a host's file had converged across public repositories, and left the
request undone. That was the wrong test: a cockpit is a personal tool, as an editor is. Orca is chosen
because the owner named it, its licence is MIT, its source runs the installed `claude`, and leaving it is cheap.

1. **Orca is in the registry as a `tool`, followed on its `main` branch.** It auto-updates by default on
   its stable channel, about a release a day, so no pin here would say what is installed. The pages cited
   are the same at v1.4.255 and at the pinned commit.
2. **`orca.yaml` holds two documented keys.** `scripts.setup` runs `uv sync --locked` through mise in a
   new worktree, when setup is enabled in Orca and the command approved there.
   `setupAgentStartupPolicy: wait-for-setup` starts the agent only after setup has succeeded. The
   repository works without the file: a new worktree passes `scripts/ci` with no setup step.
3. **This repository's rules do not depend on the host.** It registers no hook before a tool call
   (ADR 0020), and the merge gate and the absence of live credentials hold whichever app starts a
   session. Orca adds hooks of its own to the user's settings, one of them before every tool call. Each
   hands the event to the local app and exits 0; the one before a tool call prints back the app's answer
   when there is one. Outside Orca they return at once. Its "Agent status hooks" setting removes them.
4. **Landing is `scripts/land` and nothing else.** A host's commit, push and pull request controls are
   not a landing path: the ruleset on `main` requires the `signoff` status that `scripts/land` posts for
   the commit it has checked (ADR 0014, ADR 0017).
5. **Claude Code is the only runtime adopted.** Anthropic: "In every approach the workers are Claude
   sessions. To involve a different tool, expose it to Claude as an MCP server." OpenAI has removed
   `codex mcp-server`, which leaves option 5 no documented route to Codex today.
6. **A second runtime is adopted like any upstream**, when there is a job for it. It reads `AGENTS.md`,
   works in its own worktree and branch, lands through rule 4 with the review ADR 0017 requires, and its
   environment carries no credentials, paper ones included.
7. **Not adopted: option 4.** Orca labels its orchestration commands "Experimental" and no other host
   documents a handoff. Runtimes meet at a branch and a pull request.

### Consequences

* Good: one window shows every session, each in its own worktree with its environment built.
* Orca starts Claude Code with `--dangerously-skip-permissions`, its "Yolo" launch, unless Settings →
  Agents → Agent Permissions is switched to Manual or the agent's own launch arguments are edited. Under
  Yolo the `ask` rules still prompt, among a few other things (ADR 0020).
* Leaving Orca means deleting `orca.yaml`, the registry entry and its pin, and the rows that name them in
  `AGENTS.md` and `README.md`. Its hooks stay in the user's settings until its setting removes them.
* Bad: no pin says what runs, so no changelog is read before the app changes. 1,199 `bug` issues are open.
* Orca's GitHub panel reports the GitHub CLI as missing: `gh` is a mise tool of this repository, not a
  global one. `scripts/land` calls it through `mise exec` inside the checkout, where it resolves.
* On a machine, Orca also marks each worktree trusted in Claude Code's global config, leaves an existing
  status line alone, and sends anonymous usage events until Settings → Privacy turns them off. After the
  first run the user's settings also held `skipDangerousModePermissionPrompt`; what wrote it is UNVERIFIED.

### Confirmation

First run, 2026-11-10: Orca 1.4.255 and Claude Code 2.1.311, driven through `orca worktree create
--agent claude --setup inherit`. Orca made the worktree under `~/orca/workspaces/canon/` on a branch from
`origin/main`, ran the setup command (the repository's policy in Orca was `run-by-default`), started the
agent only after "Setup finished", and marked the worktree trusted. The session wrote that tree's own `.canon/watch.json` and
quoted its brief, "canon: 37 upstreams pinned". `scripts/ci` passed there, `scripts/land --dry-run` ran
there, and `scripts/bootstrap --check` passed afterwards: Orca's hooks sit beside the ledger's and the
status line is untouched. Outside Orca one call of its hook took 0.01 seconds. A first attempt opened Orca
from an agent session's shell; the app and the agents it started then carried that session's
environment. Open Orca from the Dock. UNVERIFIED: landing a real change from an Orca worktree, the app's
answer to a tool call in Manual mode, and whether the app asks before it runs the setup command.

## Evidence

Read 2026-11-10. "Lane": a research subagent fetched the page and a model summarised it. "Second reader":
the `evidence-verifier` re-read the source the same day.

* https://github.com/stablyai/orca/tree/v1.4.255/docs/site/content/docs — tag object `63ba9c6212d1`,
  published 2026-11-09, MIT. `model/orca-yaml.mdx`: the two keys, "All keys are optional"; setup "Runs in
  the new worktree when setup is enabled"; "A valid file does not bypass Orca's command approval";
  "`wait-for-setup` waits for successful setup before agent startup". `cli/orchestration.mdx`:
  "Experimental". `agents/hooks-memory.mdx`: a repo's hooks "will run when Orca launches the agent in a
  worktree of that repo", and the status-hooks setting. `telemetry.mdx`: the switches (fetched; second
  reader). `model/agents-sessions.mdx`: Orca launches "Claude with `--dangerously-skip-permissions`";
  `agents/supported.mdx`: "Settings → Agents → Agent Permissions" switches "between **Yolo** and
  **Manual** launches"; `cli/reference.mdx`: the `worktree create` flags (fetched).
* https://github.com/stablyai/orca/tree/main/src — `shared/agent-launch/catalog/tui-agent-config.ts`
  runs `claude`; `main/claude/hook-settings.ts`, `statusline-install.ts` and
  `claude-folder-trust-file.ts` write the files named above; `claude-managed-hook-events.ts` installs
  `PreToolUse` and `PermissionRequest` hooks with matcher `*` (lane; second reader).
* https://www.onorca.dev/docs/install — "Orca auto-updates by default, tracking the stable channel";
  `orca-macos-arm64.dmg` (second reader). GitHub releases: 24 stable ones from 2026-10-17 to 2026-11-09.
* https://github.com/stablyai/orca/issues?q=is%3Aissue+is%3Aopen+label%3Abug — 1,199 (second reader).
* https://code.claude.com/docs/en/agents.md — the sentence in rule 5 (lane; second reader).
* https://learn.chatgpt.com/docs/mcp-server — "The `codex mcp-server` command and the standalone
  `codex-mcp-server` binary have been removed."; its replacement "isn't an MCP server" (lane; second reader).
* The first run on the owner's machine — the process list, the agent terminal read through `orca terminal
  read`, the event ledger, the worktree, and `~/.claude/settings.json` before and after: sixteen hook
  entries added, `PreToolUse` and `PermissionRequest` among those with matcher `*`, a 10-second timeout
  and no `onFailure`; the script they call posts to `127.0.0.1` or spools the event, and every path
  through it ends in `exit 0` (read, run; second reader for the files and the ledger).
* `mise.toml` here, no global mise config, and `gh --version` from a clean interactive shell: it failed in
  the home folder and ran in the checkout; `scripts/land` — `"$MISE" exec -- gh` (run, read; second reader).
* Second reader, 2026-11-10: every quote held in each version. This one was changed that day from
  `proposed` with option 1, on the owner's correction that a host is a cockpit for Claude Code and not an
  alternative to it. Its corrections, on Orca's own hooks, the branch pin, when setup runs, what Yolo
  still asks, and how `gh` is reached, are made above.
