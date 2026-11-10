---
status: proposed
date: 2026-11-10
decision-makers: coordinator (Claude), by the owner's delegation of 2026-11-10; repo owner ratifies
review-by: 2026-12-10
---

# Context layers for the runtimes: watch OpenViking, WeKnora, Serena and Context7 until a reviewer trial shows a gain

## Context and Problem Statement

The owner asked for the runtimes (Claude Code, and the reviewers Codex, Muse Code, Antigravity and DeepSeek
Harness of ADR 0043 and ADR 0045) to be enhanced with a context layer, naming OpenViking and Tencent's
WeKnora, "and beyond", to raise the quality of automation and workflow. Should any such layer be adopted
now, and what would show that one helps here?

## Decision Drivers

* A reviewer stays independent: a new workspace per review (ADR 0043 rule 3); the fix, later commits and
  the case file are not shown (ADR 0046 rule 2); its task never names another reviewer's report
  (`evals/review/README.md`); the brief forbids reading `evals/` and the network beyond `uv`.
* Adopt on evidence that it helps this kind of work; this repository has under 200 tracked files.
* A small footprint: licence, maturity, services and where data goes.

## Considered Options

1. Adopt OpenViking for the runtimes that have an integration.
2. Adopt WeKnora as a knowledge base behind MCP.
3. Adopt a code-symbol or documentation MCP server (Serena, Context7).
4. Watch all four; keep what each runtime already reads; adopt only after a trial on ADR 0046's benchmark.

## Decision Outcome

Chosen: **option 4**. The four are registry entries with role `watch`; nothing is installed or enabled.

1. **Independence.** OpenViking's Codex plugin appends each turn and verbatim tool output to its server
   and injects recalled memories at session start and on every prompt. Memory is written under a peer
   taken from the git `origin`, so every worktree here shares one, and recall defaults to `all`: global
   memory and other workspaces' memories as well. Its DSH plugin captures messages and tool results by
   default and recalls in every conversation. A memory that outlives a review carries it into the next,
   and a server on the network is outside the brief. The defaults can be tightened: `capture.enabled`
   and `recall.enabled` (Codex), `syncTurns: false` (DSH, which keeps recall), recall scope `actor`
   (user-level memories still shared). Peer source `none` widens memory rather than narrowing it. Serena's
   cross-session memory can be disabled. **The same holds for Claude Code today**: its auto memory is on
   by default and shared by all worktrees of a repository, and nothing here turns it off for a reviewer.
   That gap is ADR 0046's to close, not this record's.
2. **No evidence it helps here.** No study found measures a retrieval or context layer for code review
   on a repository this small. Cursor's semantic search raised offline accuracy 12.5% on average, and
   code retention 2.6% only on codebases of 1,000 files or more (0.3% overall). A preprint found grep
   mostly above vector retrieval on 116 LongMemEval questions (conversations, not code), with Gemini CLI
   on Gemini 3.1 Pro favouring vector, and does "not claim that grep 'beats' vector in general".
   OpenViking's figures are LoCoMo and tau2-bench, its own.
3. **Footprint and maturity.** OpenViking: AGPL-3.0 core, with a README that also restricts rewriting and
   translation; pre-1.0; v0.5.4 (2026-11-09) lists six "upgrade and migration" items, not called
   breaking; its server needs an embedding model and a VLM from a configured provider. Its DSH plugin is
   verified to DSH 0.2.1-rc.3, and `dsh-v0.2.2-rc.1` is tagged. WeKnora: MIT, for documents, with GitLab
   as a sync source and its own cross-session memory; Docker Compose with PostgreSQL and Redis, or a Lite
   binary on SQLite; a DeepSeek Harness plugin with read-only tools. Serena: GPL-3.0-or-later. Context7:
   MIT server, but every query goes to a hosted service whose backend is private.
4. **What the runtimes already read.** `AGENTS.md`: Codex (within 32 KiB), Muse Code (once the workspace
   is trusted), Antigravity CLI (with `GEMINI.md`, `.agents/rules/*.md`), DeepSeek Harness (`AGENTS.md`
   and `CLAUDE.md` by default) and Claude Code through `CLAUDE.md`, which imports it. All five take MCP
   servers, so a later adoption needs no new mechanism; none is configured here.

Where sources disagree: Cursor reports a retrieval gain, the grep preprint mostly the reverse, on other
data. One preprint found context files did not generally improve success and raised cost over 20%,
useful for non-standard practices and not as overviews; another associated `AGENTS.md` with 28.6% lower
median runtime and 16.6% fewer output tokens on 124 pull requests. Keeping this `AGENTS.md` as the
baseline is a judgement; the trial's "without" arm measures it as it stands. Against the request: ADR
0046 does not name other reports; OpenViking's capture can be switched off; WeKnora has a Lite mode and a
DSH plugin; OpenViking names no Muse Code or Antigravity integration, but any Streamable HTTP MCP client
can reach its `/mcp` directly, tools only, with no capture hook.

### The trial that would change this

Run ADR 0046's benchmark for one reviewer on the same ready cases, without and with the candidate layer,
everything else equal, each arm with a `runs` count above one (rule 6: a single-run sheet measures no
variance between runs). Scope memory to one review: a fresh store per case, emptied afterwards, no peer
or user-level memory shared between cases, and the reviewer runtime's own memory off. Add the layer's
network access to the brief only for that run. Adopt for that reviewer if the arm with the layer finds
more confirmed defects than the spread between its own runs and rule 7's sampling error explain.

### Consequences

* Good: reviews stay independent and comparable; no service, licence or data flow is added.
* Bad: nothing is tried until a trial runs, and showing a modest gain needs more cases than the set has.

### Confirmation

`scripts/canon lint` holds the four `watch` entries to this record. `scripts/canon outdated` and the
daily canon watch report their releases. Nothing installs them: no `mise.toml`, `pyproject.toml` or MCP
configuration here names them. A move to adopt goes through `adopt-upstream` with the trial's sheet as
evidence and a record that supersedes this one.

## Evidence

Read 2026-11-10; web pages summarised by the fetch tool, files at a pin read directly.

* https://github.com/volcengine/OpenViking — licences, rewriting and translation terms, server needs,
  integrations, LoCoMo and tau2-bench at 0.3.22 — fetched. `tag:v0.5.4` → `9dfa9017a8c6` (resolved); at
  it, `docs/en/configuration/02-client.md` (peer source, `recall.peer_scope`, `none`) and
  `examples/codex-memory-plugin/README.md` (broad recall, `capture.enabled`, `recall.enabled`) — read.
* https://github.com/volcengine/OpenViking/releases/tag/v0.5.4 — six "Upgrade and migration" items —
  fetched; date 2026-11-09 from `gh release list`.
* `docs/en/agent-integrations/06-mcp-clients.md` at the v0.5.4 pin — Streamable HTTP clients "can connect
  directly" to `/mcp`; Codex through its plugin — read.
* https://docs.openviking.ai/en/agent-integrations/04-codex and
  https://docs.openviking.ai/en/agent-integrations/17-dsh — capture and recall hooks, origin peer, Codex
  0.130.0+; DSH capture defaults, `syncTurns`, recall scope `all` by default, verified to 0.2.1-rc.3 —
  fetched; the DSH page also read at the v0.5.4 pin.
* https://github.com/Tencent/WeKnora — MIT, formats, GitLab source, cross-session memory, deployments,
  MCP server, DSH plugin `@wxg-prc-cpg/dsh-weknora` — fetched, README read at the pin; GitHub licence
  `NOASSERTION` (`gh api`). `tag:v0.9.0` → `f17ee8a8da90` (resolved).
* https://github.com/oraios/serena — GPL-3.0-or-later application, MIT SolidLSP, clients, memory that can
  be disabled — fetched. `tag:v2.1.0` → `5e4f5cd173c7` (resolved).
* https://github.com/upstash/context7 — MIT, hosted `mcp.context7.com`, private backend — fetched.
  `tag:mcpb-v4.6.0` → `6e68ffcbad0e`, the commit of `@upstash/context7-mcp@4.6.0` (resolved, `git ls-remote`).
* https://github.com/deepseek-ai/deepseek-harness — at `dsh-v0.2.1-rc.3` (`1f81c047fc48`),
  `packages/context/agent-instructions/src/config.ts` candidates `AGENTS.md`, `CLAUDE.md`, enabled by
  `dsh-base` per its README — read; `dsh-v0.2.2-rc.1` tagged (`git ls-remote`).
  https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/mcp.md — MCP client plugin,
  stdio and Streamable HTTP — fetched.
* https://cursor.com/blog/semsearch — 12.5% (6.5–23.5%); retention +0.3%, +2.6% at 1,000+ files; vendor
  study — fetched.
* https://arxiv.org/abs/2605.15184 and https://arxiv.org/html/2605.15184 — "Is Grep All You Need?":
  experiment 1, experiment 2 by harness, the authors' disclaimer — fetched.
* https://arxiv.org/abs/2602.11988 — "Evaluating AGENTS.md": no general success gain, cost over 20% —
  fetched (abstract). https://arxiv.org/abs/2601.20404 — `AGENTS.md` and efficiency, 10 repositories,
  124 pull requests, −28.64% median runtime, −16.58% output tokens — fetched (abstract).
* https://code.claude.com/docs/en/memory — auto memory on by default, one directory for all worktrees of
  a repository, `autoMemoryEnabled`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY` — fetched.
  https://code.claude.com/docs/en/mcp — `claude mcp add`, scopes — fetched (first 100,000 characters).
* https://learn.chatgpt.com/docs/agent-configuration/agents-md, https://learn.chatgpt.com/docs/extend/mcp?surface=cli,
  https://dev.meta.ai/docs/muse-code/configuration, https://dev.meta.ai/docs/muse-code/extending,
  https://antigravity.google/docs/rules/, https://antigravity.google/docs/mcp/ — what Codex, Muse Code
  and Antigravity read, and their MCP configuration — fetched.
* ADR 0043, ADR 0045, ADR 0046, `evals/review/brief.md`, `evals/review/README.md` — read.
* 2026-11-10, second reader: corrected peer source `none`, default cross-project recall, DSH `AGENTS.md`,
  WeKnora's memory and DSH plugin, the trial's use of rules 6 and 7, and added Claude Code's auto memory
  and the conflicting `AGENTS.md` study.
