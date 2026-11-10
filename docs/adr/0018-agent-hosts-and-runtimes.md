---
status: proposed
date: 2026-11-10
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-10
---

# Agent hosts and other runtimes: no host has converged, and one gate holds whatever runs

## Context and Problem Statement

The owner asked (2026-11-10) to run Claude Code inside Orca, a desktop app that gives each task its own git
worktree and agent terminal, and to orchestrate more than one agent runtime. Sessions here run in the
worktrees Claude Code creates. Which host, what does the repository commit for it, and what holds a
second runtime to this repo's rules?

## Decision Drivers

* The vendor's own tooling comes before community work (`adopt-upstream`).
* A host must launch the installed CLI, so this repo's settings, hooks and instructions apply unchanged.
* Enforcement must not depend on which host or runtime produced a change (ADR 0013, ADR 0017).
* Paper trading only, and no credentials in an agent's context (ADR 0009).
* Nothing is built here that its upstream does not document as stable.

## Considered Options

1. Claude Code's own parallel sessions (subagents, worktrees, the desktop app), as now.
2. Orca as the host, with Claude Code orchestrating inside it.
3. Another host: Conductor, Paradev (formerly Superset), cmux or Claude Squad.
4. A host's orchestration layer handing work between runtimes (Orca's `orca orchestration` commands).
5. A second runtime exposed to Claude Code as an MCP server.

## Decision Outcome

Chosen for now: **option 1, with Orca the candidate for option 2.** The status is `proposed` because the
sources have not converged on a host and no upstream documents a stable handoff between runtimes.

| Host | Licence | Latest release | Public repositories committing its file |
|---|---|---|---|
| Conductor | not stated | 0.94.0, 2026-11-09 | 387 (`conductor.json`) |
| Orca | MIT | v1.4.255, 2026-11-09; about one a day | 248 (`orca.yaml`) |
| Paradev (was Superset) | MIT | desktop-v2.0.8, 2026-11-10 | 213 (`.superset/config.json`) |
| cmux | GPL-3.0-or-later; server BUSL-1.1 | v0.65.2.2, 2026-10-27 | 12 (`.cmux/cmux.json`) |
| Claude Squad | AGPL-3.0 | v1.0.20, 2026-08-20 | none: its config is per user |

Two more, Vibe Kanban and Crystal, were wound down in 2026. None of the 29 repositories of ADR 0015
commits any host's file; 14 carry `AGENTS.md`, 8 `CLAUDE.md`, 7 `.agents/skills` and 4 `.codex`. Where a
project commits one, it is a few lines of setup, run or teardown commands. Orca is the candidate, though
Conductor's file is the more common: the owner named it, its licence is stated, and its source shows it
running the installed `claude`.

What holds today, whichever host starts a session:

1. **The repository needs no host file.** A new worktree outside it passes `scripts/ci` with no setup
   step, so the optional setup script that Orca, Conductor and Paradev offer is not needed.
2. **No rule depends on the host.** Nothing runs before a tool call (ADR 0020). The merge gate and the
   absence of live credentials hold whichever app starts a session.
3. **Landing is `scripts/land` and nothing else.** A host's commit, push and pull request controls are
   not a landing path: the ruleset on `main` requires the `signoff` status that `scripts/land` posts for
   the commit it has checked (ADR 0014, ADR 0017).
4. **Claude Code is the only runtime adopted.** Anthropic: "In every approach the workers are Claude
   sessions. To involve a different tool, expose it to Claude as an MCP server." OpenAI has removed
   `codex mcp-server`, which leaves option 5 no documented route to Codex today.
5. **A second runtime is adopted like any upstream**, when there is a job for it. It reads `AGENTS.md`,
   works in its own worktree and branch, and lands through rule 3 with the review ADR 0017 requires. This
   repo's permission rules do not reach it, so its environment carries no credentials, paper ones included.
6. **Not adopted: option 4.** Orca labels its orchestration commands "Experimental" and no other host
   documents a handoff. Runtimes meet at a branch and a pull request.

The gap closes with a host that leads the sample or one the owner ratifies, a handoff between runtimes
that its upstream calls stable, and the first-run checks under Confirmation. On ratification Orca enters
the registry as a `tool` pinned at the release read here, with an `orca.yaml` of two documented keys:
`scripts.setup` running `uv sync --locked` through mise, and `setupAgentStartupPolicy: wait-for-setup`.

### Consequences

* Good: nothing in the repository depends on a host, so trying one, or leaving it, changes nothing here.
* Bad: the request is not carried out yet; sessions stay in the worktrees Claude Code creates.
* On a machine, Orca adds status hooks to `~/.claude/settings.json` (one setting removes them), writes a
  status line there only when none is set, marks each worktree trusted in Claude Code's global config,
  and sends anonymous usage events until Settings → Privacy or `ORCA_TELEMETRY_DISABLED=1` turns them
  off. The bootstrap never overwrites a settings file that differs from its own; where Orca ran first it
  leaves its settings as a proposal and `--check` reports them missing. Orca has 1,199 open `bug` issues.

### Confirmation

Tested 2026-11-10: `git worktree add` into a directory outside the repository, then `scripts/ci` there
with no setup: every check passed. The setup command above ran under `env -i` with only `HOME` and a
minimal `PATH`, built `.venv` and left the tree clean. That this repo's settings and hooks load in a
host's worktree follows from two statements: Claude Code "reads the shared `.claude/settings.json` from
the session's primary working directory", and Orca "launches it with the worktree as the working
directory". A session started that way has not been run: UNVERIFIED end to end. First-run checks once a
host is installed: the session brief prints; `scripts/land --dry-run` works from the host's worktree;
`scripts/bootstrap --check` still passes.

## Evidence

Read 2026-11-10. "Lane": a research subagent fetched the page and a model summarised it. "Second reader":
the `evidence-verifier` re-read the source the same day.

* GitHub contents API, the root of each repository in `canon/github-practice.sample.json` — the counts in
  the text; no `orca.yaml`, `conductor.json`, `.superset`, `.paradev` or `.cmux` (fetched; second reader).
* GitHub code search — `filename:orca.yaml path:/ "scripts:"`, `filename:conductor.json path:/ "scripts"`,
  `path:.superset filename:config.json`, `path:.cmux filename:cmux.json`, and `path:.paradev
  filename:config.json` (4). A weak signal: forks and examples count (fetched; second reader).
* https://github.com/PostHog/posthog — `orca.yaml` and `.superset/config.json` call one script (fetched).
* https://github.com/vercel/next.js — `conductor.json`: setup and run scripts, and more (fetched).
* https://github.com/stablyai/orca/tree/v1.4.255/docs/site/content/docs — tag object `63ba9c6212d1`,
  published 2026-11-09, MIT. `model/orca-yaml.mdx`: the two keys, "All keys are optional".
  `cli/orchestration.mdx`: "Experimental". `agents/hooks-memory.mdx`: a repo's hooks "will run when Orca
  launches the agent in a worktree of that repo", and the status-hooks setting. `agents/claude-code.mdx`:
  the working-directory sentence. `telemetry.mdx`: the switches (fetched; second reader).
* https://github.com/stablyai/orca/tree/main/src — `shared/agent-launch/catalog/tui-agent-config.ts`
  runs `claude`; `main/claude/hook-settings.ts`, `statusline-install.ts` and
  `claude-folder-trust-file.ts` write the files named above (lane; second reader).
* https://github.com/stablyai/orca/issues?q=is%3Aissue+is%3Aopen+label%3Abug — 1,199 (second reader).
* https://www.conductor.build/changelog — 0.94.0, November 9, 2026; its licence: UNVERIFIED (second reader).
* https://github.com/ParadevAI/Paradev — MIT; the release in the table (second reader).
* https://github.com/manaflow-ai/cmux — both licences, the release, the project file (second reader).
* https://github.com/smtg-ai/claude-squad — AGPL-3.0; config in `~/.claude-squad` (lane; second reader).
* https://github.com/BloopAI/vibe-kanban — "Vibe Kanban is sunsetting." (lane; second reader).
* https://github.com/stravu/crystal — "deprecated and replaced by Nimbalyst", February 2026 (lane;
  second reader).
* https://code.claude.com/docs/en/agents.md — the sentence in rule 4 (lane; second reader).
* https://code.claude.com/docs/en/settings.md — the sentence in Confirmation (second reader).
* https://learn.chatgpt.com/docs/mcp-server — "The `codex mcp-server` command and the standalone
  `codex-mcp-server` binary have been removed."; its replacement "isn't an MCP server" (lane; second reader).
* `scripts/bootstrap` here — `settings.json` and `CLAUDE.md` are copied only when absent or identical,
  else written as `*.canon-proposed`; `--check` looks for `canon/ledger.sh` and `canon/statusline.sh` (read).
* Second reader, 2026-11-10: every quote held. It corrected three table rows, the telemetry switches and
  the wording of rules 1 and 3, and showed that the settings sentence is documented. All are fixed above.
* Changed 2026-11-10 with ADR 0020: the context, rules 2 and 5 and the first-run checks no longer rest on
  a guard hook. A second reader the same day had rule 2 cut to what its sources support.
