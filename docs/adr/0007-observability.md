---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-01-09
---

# Observability: watch every lane through contracts Claude Code documents

## Context and Problem Statement

The orchestrator must watch all lanes, itself included: token spend, session status, stalls, skill and MCP
use, hidden errors, silent fallbacks. Which data sources and tools?

## Decision Drivers

* Use documented interfaces. The transcript JSONL format is documented as internal and subject to change.
* Work on this Mac today, with no container runtime installed.
* Record names and counts, never prompt or tool content.

## Considered Options

1. Parse session transcripts directly.
2. OpenTelemetry export to a local collector and dashboards.
3. Adopt a community fleet monitor.
4. Tiers: built-in commands, a local event ledger fed by hooks, OpenTelemetry when a backend exists.

## Decision Outcome

Chosen: **option 4**.

**Tier 1, built in, no setup.** `/usage` (spend and cache hit rate), `/context`, `/insights`, `/doctor`.

**Tier 2, local ledger, installed machine-wide from `home/`.**

* `~/.claude/canon/ledger.sh` is registered as an `async` command hook. It appends one line per hook event to
  `~/.claude/canon/ledger/YYYY-MM-DD.jsonl`, named by UTC date: time (`ts` in whole seconds, `t` in epoch
  seconds with milliseconds), event, session, directory, agent type, tool name, skill name, subagent type,
  permission mode, effort, `from_model` and `to_model` when the payload has them, and the event's source,
  reason or trigger. No prompts, inputs or outputs.
* `~/.claude/canon/statusline.sh` shows model, effort, context use and cost, saves the latest snapshot per
  session, and writes a ledger line when the model changes inside a session.
* `scripts/lanes` reads the ledger files of the last N UTC dates and reports per session: state, minutes
  quiet, tool calls, failures, permission denials, compactions, model switches, and the tool, MCP, skill
  and subagent mix. The state comes from the last turn boundary (`UserPromptSubmit` against `Stop`,
  `StopFailure`, or a `SessionStart` not written by a compaction): working, idle, QUIET, abandoned (open
  and silent six hours or more), ended, or unknown when the window holds no boundary. Events are ordered
  by `t`, else `ts`; a line without a valid session and time is counted as unreadable and skipped.
* `ccusage` (pinned) gives token and cost history from Claude Code's own logs.

What the report can detect:

| Concern | Signal |
|---|---|
| Stall | Last turn boundary is an open turn, with no event for N minutes (QUIET) |
| Hidden errors | `PostToolUseFailure` count and rate; `StopFailure` (rate limit, overloaded, billing) |
| Silent fallback | Model change inside a session, seen by the status line |
| Blocked lane | `PermissionDenied`, which fires only when auto mode denies a call, not in other modes |
| Context pressure | `PreCompact` count; context percentage in the snapshot |
| Skill, MCP, subagent use | `Skill` tool calls, `mcp__*` tool names, `SubagentStart` |

A long single tool call looks the same as a stall, so "quiet" is a prompt to look, not a verdict. A
background subagent still working after its parent turn's `Stop` reads as idle.

**Tier 3, OpenTelemetry, deferred.** Claude Code exports metrics and events natively. The variables must be
set in user or managed settings; they are ignored from project files. `home/otel-settings.example.json` holds the
block. Anthropic's monitoring guide is the reference stack and needs a container runtime, which this Mac
does not have yet.

**Gap, stated plainly:** no fleet or stall-detection tool has converged upstream. `scripts/lanes` is thin
glue over documented hook payloads and is provisional; it is replaced when an upstream tool earns adoption.

### Consequences

* Good: works now; nothing sensitive is logged; every signal comes from a documented payload.
* Bad: the ledger sees only sessions on this machine with the hooks installed; cloud sessions are invisible
  to it. Stall detection is a heuristic. Model-change detection and the cost and context columns need a
  status line, so they cover terminal sessions only.

### Confirmation

`scripts/lanes --selftest` replays a synthetic ledger and checks each flag fires.

`ccusage` 20.0.48 was run on 2026-11-09 and reported the day's use across four models: 57.0M tokens, of
which 54.9M were cache reads (about 97% of input, above the 80% bar in ADR 0006), and an estimated $37.36
at API rates. That figure is ccusage's own estimate, not a bill.

Observed live on 2026-11-09: after install, the desktop session that built this repo recorded its own
prompts, tool calls and stops, and `scripts/lanes` reported it. One blind spot showed up the same day: a
command refused by a built-in safety check produced neither `PostToolUseFailure` nor `PermissionDenied`, so
refusals of that kind do not reach the ledger.

## Evidence

Read 2026-11-09 (fetched; fetches are model-summarised).

* https://code.claude.com/docs/en/monitoring-usage.md — `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_*` exporters;
  metrics `claude_code.token.usage`, `claude_code.cost.usage`, `claude_code.session.count`; events
  `claude_code.tool_result`, `claude_code.api_error`, `claude_code.api_request`; content logging off by
  default.
* https://code.claude.com/docs/en/hooks.md — events used here (`SessionStart`, `UserPromptSubmit`,
  `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `SubagentStop`, `PreCompact`,
  `Stop`, `StopFailure`, `SessionEnd`); common stdin fields; `async: true` runs without
  blocking. Per-event schemas beyond the common fields were not read: UNVERIFIED, so the ledger reads them
  defensively.
* https://json.schemastore.org/claude-code-settings.json — every event registered in `home/.claude/settings.json`
  is in the published schema (checked by the orchestrator). `PostModelSwitch` is on the hooks docs page but
  not in the schema, so it is not registered; the status line detects model changes instead.
* https://code.claude.com/docs/en/statusline.md — stdin fields `model`, `cost.total_cost_usd`,
  `context_window.used_percentage`, `effort.level`, `rate_limits`.
* https://code.claude.com/docs/en/sessions.md — transcript location; the format is internal and may change.
* https://code.claude.com/docs/en/settings.md — telemetry export variables are ignored from project and
  local settings files.
* https://code.claude.com/docs/en/costs.md — `/usage` reports prompt-cache hit rate.
* https://github.com/anthropics/claude-code-monitoring-guide — collector, Prometheus and Grafana reference
  (resolved to `02777441f2a3`).
* https://github.com/ryoppippi/ccusage — MIT, v20.0.48 (resolved to `8995f3b7fd71`).
* https://github.com/mercurialsolo/claudectl — 204 stars; the only fleet tool fetched. Other candidates were
  not fetched: UNVERIFIED. No leader.
* Second reader, 2026-11-09 (`evidence-verifier`): the telemetry names, hook events and status-line fields
  are supported. `PermissionDenied` is narrower than first written (auto mode only), and the ledger's field
  list was incomplete; both corrected. The monitoring guide's last change was 2025-07-29, so check it
  against the current telemetry docs before using it.
* Restated 2026-11-10 against the headers of `scripts/lanes` and `home/.claude/canon/ledger.sh`: the
  ledger's time fields and file date, the session states and the limits.
