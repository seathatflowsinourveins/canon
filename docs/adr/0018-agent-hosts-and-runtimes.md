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
3. Another host: Conductor, Paradev (formerly Superset) or cmux.
4. A host's orchestration layer handing work between runtimes (Orca's `orca orchestration` commands).
5. A second runtime exposed to Claude Code as an MCP server.

## Decision Outcome

Chosen: **option 2**. Orca is the cockpit and Claude Code stays the only runtime. Option 1 keeps working
beside it; options 4 and 5 are not adopted.

Counted in public repositories, Conductor's file appears in 387, Orca's in 248, Paradev's in 213 and
cmux's in 12, and none of the 29 repositories of ADR 0015 commits one. This record first read those
counts as "no host has converged" and left the request undone. That was the wrong test: a cockpit is a
personal tool, as an editor is, and a repository need not carry a trace of one. Orca is chosen because the
owner named it, its licence is MIT, its source runs the installed `claude`, and leaving it is cheap.

1. **Orca is in the registry as a `tool`, followed on its `main` branch.** It auto-updates by default on
   its stable channel, about a release a day, so this repo does not choose the version that runs and no
   pin here would say what is installed. The pages cited are the same at v1.4.255 and at the pinned commit.
2. **`orca.yaml` holds two documented keys.** `scripts.setup` runs `uv sync --locked` through mise in a
   new worktree, when setup is enabled in Orca and the command approved there.
   `setupAgentStartupPolicy: wait-for-setup` starts the agent only after setup has succeeded. The
   repository works without the file: a new worktree passes `scripts/ci` with no setup step.
3. **This repository's rules do not depend on the host.** It registers no hook before a tool call
   (ADR 0020), and the merge gate and the absence of live credentials hold whichever app starts a
   session. Orca adds status hooks of its own to the user's settings, one of them before every tool call.
   They report to the app, and its "Agent status hooks" setting removes them.
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
* Leaving Orca means deleting `orca.yaml`, the registry entry and its pin, and the rows that name them in
  `AGENTS.md` and `README.md`. On the machine, the status hooks it wrote stay until its setting or a
  hand edit removes them.
* Bad: no pin says what runs, so no changelog is read before the app changes. Orca has 1,199 open `bug`
  issues.
* On a machine, Orca also writes a status line when none is set, marks each worktree trusted in Claude
  Code's global config, and sends anonymous usage events until Settings → Privacy or
  `ORCA_TELEMETRY_DISABLED=1` turns them off. Where Orca ran first, the bootstrap leaves its own settings
  as a proposal and `--check` reports them missing.

### Confirmation

Tested 2026-11-10: `git worktree add` into a directory outside the repository, then `scripts/ci` there
with no setup: every check passed. The setup command in `orca.yaml` ran under `env -i` with only `HOME`
and a minimal `PATH`, built `.venv` and left the tree clean. Orca is not installed yet, so a session it
starts has not been run: UNVERIFIED end to end. That this repo's settings and hooks load there follows
from three statements: Claude Code "reads the shared `.claude/settings.json` from the session's primary
working directory", `${CLAUDE_PROJECT_DIR}` is "the project root where the session started", and Orca
"launches it with the worktree as the working directory". First-run checks once it is installed: the
session brief prints this repo's pin count; no hook Orca adds blocks or denies a tool call;
`scripts/land --dry-run` works from Orca's worktree; `scripts/bootstrap --check` still passes.

## Evidence

Read 2026-11-10. "Lane": a research subagent fetched the page and a model summarised it. "Second reader":
the `evidence-verifier` re-read the source the same day.

* GitHub contents API, the root of each repository in `canon/github-practice.sample.json` — no
  `orca.yaml`, `conductor.json`, `.superset`, `.paradev` or `.cmux` (fetched; second reader).
* GitHub code search — `filename:orca.yaml path:/ "scripts:"`, `filename:conductor.json path:/ "scripts"`,
  `path:.superset filename:config.json`, `path:.cmux filename:cmux.json`, and `path:.paradev
  filename:config.json` (4). A weak signal: forks and examples count (fetched; second reader).
* https://github.com/stablyai/orca/tree/v1.4.255/docs/site/content/docs — tag object `63ba9c6212d1`,
  published 2026-11-09, MIT. `model/orca-yaml.mdx`: the two keys, "All keys are optional"; setup "Runs in
  the new worktree when setup is enabled"; "A valid file does not bypass Orca's command approval";
  "`wait-for-setup` waits for successful setup before agent startup". `cli/orchestration.mdx`:
  "Experimental". `agents/hooks-memory.mdx`: a repo's hooks "will run when Orca
  launches the agent in a worktree of that repo", and the status-hooks setting. `agents/claude-code.mdx`:
  the working-directory sentence. `telemetry.mdx`: the switches (fetched; second reader).
* https://github.com/stablyai/orca/tree/main/src — `shared/agent-launch/catalog/tui-agent-config.ts`
  runs `claude`; `main/claude/hook-settings.ts`, `statusline-install.ts` and
  `claude-folder-trust-file.ts` write the files named above; `claude-managed-hook-events.ts` installs
  `PreToolUse` and `PermissionRequest` hooks with matcher `*` (lane; second reader).
* https://www.onorca.dev/docs/install — "Orca auto-updates by default, tracking the stable channel";
  `orca-macos-arm64.dmg` (second reader). GitHub releases: 24 stable ones from 2026-10-17 to 2026-11-09.
* https://github.com/stablyai/orca/issues?q=is%3Aissue+is%3Aopen+label%3Abug — 1,199 (second reader).
* https://github.com/ParadevAI/Paradev — the project formerly named Superset (second reader).
* https://code.claude.com/docs/en/agents.md — the sentence in rule 5 (lane; second reader).
* https://code.claude.com/docs/en/settings.md and https://code.claude.com/docs/en/hooks.md — the two
  Claude Code sentences in Confirmation (second reader).
* https://learn.chatgpt.com/docs/mcp-server — "The `codex mcp-server` command and the standalone
  `codex-mcp-server` binary have been removed."; its replacement "isn't an MCP server" (lane; second reader).
* `scripts/bootstrap` here — `settings.json` and `CLAUDE.md` are copied only when absent or identical,
  else written as `*.canon-proposed`; `--check` looks for `canon/ledger.sh` and `canon/statusline.sh` (read).
* Second reader, 2026-11-10: every quote held in both versions. On the first it corrected the telemetry
  switches and the wording of two rules. This one was changed the same day from `proposed` with option 1,
  on the owner's correction that a host is a cockpit for Claude Code and not an alternative to it. Here
  the reader found that Orca's own hooks run before a tool call, that the reasons first given for the
  branch pin were unsupported, that setup runs only when enabled and approved, and that a session Orca
  starts is untested. All are fixed or stated above.
