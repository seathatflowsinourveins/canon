---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Token efficiency: free wins before trade-offs, and protect the prompt cache

## Context and Problem Statement

Agent sessions resend the whole conversation every turn, so cost grows roughly with the square of turn count.
Which practices does this harness adopt to keep spend proportional to work done?

## Decision Drivers

* Free wins (no quality cost) before trade-offs.
* Measured effects over intuition.
* Practices must be visible in the harness, not just written down.

## Considered Options

1. Rely on cheaper models.
2. Aggressive context pruning and compaction everywhere.
3. Free wins first (caching, context hygiene, output shape, batch), then the trade-offs (effort, model).

## Decision Outcome

Chosen: **option 3**. The order below is the one in the cost guide bundled with Claude Code. Anthropic's
platform page lists the same levers in a table ordered differently (batch earlier; upgrading to a newer
model before lowering effort), so the order is a working sequence, not a ranking both sources share.

1. **Protect the prompt cache.** Claude Code caches automatically. Within a session, switching model or
   toggling fast mode discards the cache; so do compaction, enabling a plugin and denying a whole tool.
   Edits to `CLAUDE.md` take effect after `/clear`, `/compact` or a restart. `/usage` shows the cache rate.
2. **Keep fixed context small** (ADR 0003): instruction files under 200 lines, scoped rules, skills for
   procedures. Leave MCP tool search on so tool schemas load on demand; with it on, adding or removing an
   MCP server does not disturb the cache.
3. **Keep bulky reading out of the main context.** Delegate log reading, web research and wide searches to a
   subagent that returns the conclusion. `/clear` between unrelated tasks; `/compact <focus>` at task
   boundaries.
4. **Ask for the output shape.** Subagent briefs state the report format and a length cap.
5. **Batch unattended runtime work** (50% off, including cached tokens).
6. **Tune effort per task.** Claude Code's default is `medium` on Fable 5.2 and the 5.5 models. If a model
   is a generation behind, upgrade it before tuning anything.
7. **Step down to a cheaper model last** (ADR 0005).

Where the sources disagree: Claude Code's docs say a changed effort level keeps the cache on the 5.5 models
and on Fable 5.1 and later. Anthropic's platform page reports an effort change on Opus 5.5, through the API,
rewriting 81,000 tokens. This repo therefore treats effort as safe to change inside a Claude Code session and
as a cache cost in runtime code that calls the API directly.

Guard rails:

* **Multi-agent work multiplies tokens.** Agents use about 4 times the tokens of chat and multi-agent systems
  about 15 times; Claude Code's own figure for agent teams is about 7 times a standard session. It pays when
  the work is parallel and bulky: an Opus lead with Sonnet workers matched Opus alone for 31% less on defect
  search. One dependent chain stays in one session.
* **Context editing is not a savings lever.** It makes room in the window and rewrites the cached
  conversation. Its measured cost penalty ranged from 73% on a short run to 4% on a long one.

### Consequences

* Good: the largest lever (caching) costs nothing and the things that break it are named.
* Bad: nothing here measures this repo's own cache rate over time; `/usage` and `ccusage` show it on demand.

### Confirmation

`/usage` (cache hit rate), `/context` (what fills the window), and `scripts/lanes`, which flags model
switches and compactions per session.

## Evidence

Read 2026-11-09.

* Cost-optimisation guide bundled with Claude Code 2.1.321 (`claude-api` skill, `shared/cost-optimization.md`)
  — the lever order used here; task cost grows with roughly the square of turn count; context editing "cost
  more than it saved" in the measured run; a subagent starts a fresh prefix with no cache shared with its
  parent (bundled; read by the orchestrator and again by the second reader).
* https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md — without
  caching, cost per task was 3.3x (Opus 5.5 at `medium`, SWE-bench Pro) and 7.3x (Sonnet 5.5, a 20-issue
  triage job); over 14 days of organisation-level traffic, agent loops read a median 84% of input from cache,
  and below about 80% something is breaking it; batch is 50% off; the 81,000-token effort figure; the
  orchestrator result (read in full by the second reader).
* https://code.claude.com/docs/en/prompt-caching.md — what does and does not invalidate the cache in Claude
  Code, including that an effort change keeps it on current models (fetched, then re-read by the second
  reader, whose reading corrected this record).
* https://code.claude.com/docs/en/costs.md — `/compact`, `/clear`, `/context`, `/usage` (cache rate needs
  v2.1.251 or later); move workflows into skills; delegate verbose work to subagents; agent teams use about
  7 times the tokens of a standard session (fetched).
* https://code.claude.com/docs/en/model-config.md — default effort is `medium` on Fable 5.2 and Opus, Sonnet
  and Haiku 5.5, and `high` on Opus 5, Fable 5 and Fable 5.1 (fetched).
* https://code.claude.com/docs/en/mcp.md — MCP tool search is on by default on supported models;
  `ENABLE_TOOL_SEARCH`, `MAX_MCP_OUTPUT_TOKENS` (fetched; first 100k of 157k characters).
* https://www.anthropic.com/engineering/multi-agent-research-system (2025-06-13) — about 4x and 15x token
  use; suited to parallel work, not to work with many dependencies (fetched).
* Second reader, 2026-11-09 (`evidence-verifier`): three claims in the first version were not supported.
  It attributed the lever order to the platform page, said an effort change or an MCP change discards the
  cache, and said `medium` is the default on every 5.x model. All three are corrected above.
