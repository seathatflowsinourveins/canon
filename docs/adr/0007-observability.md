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
The settings template also sets `showThinkingSummaries`, so a session's reasoning can be read with
`Ctrl+O` (added 2026-11-10 at the owner's request; it changes what is shown, not what is generated).

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

**Tier 3, OpenTelemetry, for token events only (enabled 2026-11-10).** Claude Code and Codex export OTLP
log events natively, and neither writes a file, so a local OpenTelemetry Collector receives them and writes
the ones `scripts/lanes --rates` reads. Dashboards are still deferred: Anthropic's monitoring guide is the
reference stack and needs a container runtime, which this Mac does not have.

* **The Collector** is the core distribution, `otelcol` (registry: `otelcol`, v0.165.0, Apache-2.0). Its
  manifest includes the OTLP receiver, the `file` exporter and the `filter` and `transform` processors,
  so the larger contrib distribution is not needed. Neither the mise nor the aqua registry lists it, so
  `mise.toml` installs it from its GitHub releases through mise's `github:` backend. `matching = "otelcol_"`
  picks the core archive for each platform from the six distributions a release ships.
* **Its config**, `home/.claude/canon/otel/config.yaml`, is one logs pipeline. The OTLP receiver listens
  on 127.0.0.1:4317 (gRPC) and :4318 (HTTP) only. The `file` exporter writes
  `~/.claude/canon/otel/logs.jsonl` with rotation at 10 MB, keeping 3 backups for 7 days. The Collector's
  own metrics are off (`service.telemetry.metrics.level: none`), so it opens no other port. Between the
  receiver and the exporter, a `filter` drops every record except Claude Code's `api_request` and Codex's
  `codex.sse_event` `response.completed` and `codex.turn_cost`. A `transform` then keeps only the
  attributes the manifest reads and clears the resource and scope attributes. **The filter is the guard,
  not a convenience:** at rust-v0.167.0, Codex logs every `codex.tool_result` with the tool's full
  `arguments` and a 2 KB output preview. It has a setting for the preview's size (`tool_result.max_bytes`)
  but none for the arguments (`codex-rs/otel/src/tool_result.rs`), and its shared fields carry
  `user.email`. Claude Code's events carry `user.email` and account ids too.
* **It runs** as the launchd user agent `local.canon.otel-collector` (`KeepAlive`, `RunAtLoad`), which
  `scripts/automation install` writes beside the watchdog and the paper session. Install validates the
  config with `otelcol validate` before it replaces the installed one, so a running agent never restarts
  on a refused config. It names the binary by the absolute, versioned path `mise which otelcol` gives, so
  a move of the `otelcol` pin needs `scripts/bootstrap --install` and then `scripts/automation install`
  again, and `status` says FAILED once that binary is gone. The Collector logs warnings and errors only,
  to `~/.claude/canon/watch/otel-collector.log`, which each install empties. `status` shows whether
  launchd has the agent running and how old `logs.jsonl` is. `uninstall` removes the agent and the
  config and keeps the events.
* **Claude Code** gets `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp`,
  `OTEL_METRICS_EXPORTER=none`, `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` and
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317`, in the `env` block of `home/.claude/settings.json`.
  Every content setting (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS`,
  `OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES`) stays unset, which is off. The variables are ignored
  in a project's settings, so `scripts/bootstrap --user-config` adds the ones an existing
  `~/.claude/settings.json` lacks, and keeps a copy of the previous file. A value the owner set
  differently is left alone and named, as the owner's choice, and `--check` shows it as `OWNER`.
* **Codex** gets the `[otel]` table of `home/.codex/otel.toml`: `exporter = { otlp-http = { endpoint =
  "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }`, `log_user_prompt = false` (its default) and
  `log_sse_deltas = false`. The last one departs from Codex's default (`true`) so that the per-token
  `.delta` events are not sent at all; completion events still are. `otel` is ignored in a project's
  `.codex/config.toml`, so `scripts/bootstrap --user-config` appends the table to
  `${CODEX_HOME:-~/.codex}/config.toml` when that folder exists (Codex is installed) and the file sets no
  `otel` of its own. It keeps a copy of the previous file and changes nothing else in it. A different
  `otel` setting is left alone and named as the owner's choice.

**Tokens per second, from OpenTelemetry only (added 2026-11-10).** `scripts/lanes --rates [--window MIN]
[--json]` prints one row per running worker of every runtime: lane, runtime, model, state, output tokens
per second over the window (default 10 minutes), and spend where the runtime reports it. A runtime with no
documented token source gets a row saying "not measured" and why; no figure is estimated.

* The status line cannot give a rate. Its `context_window.total_output_tokens` is the output of the most
  recent response only, and its updates are debounced, so samples of it miss responses. It still gives
  the session's cost (`cost.total_cost_usd`, cumulative), which the manifest shows.
* Claude Code: the `claude_code.api_request` event carries `session.id`, `model`, `output_tokens`,
  `duration_ms` and `cost_usd` per request. TOK/S is the window's output tokens over the window's seconds;
  GEN is output tokens over the durations of the requests that carry one. Claude Code rows are the
  ledger's working and QUIET sessions, and its idle or unknown ones heard from within six hours. A subagent's requests carry an `agent_id`;
  that they also carry the parent's `session.id`, and so count toward its row, is UNVERIFIED.
* Codex CLI: `codex.sse_event` with `event.kind` `response.completed` carries `output_token_count`, and
  `codex.turn_cost` carries `usage.estimated_usd`, both keyed by `conversation.id`. No documented field
  links a conversation to a directory or process, so conversations and `codex` processes are separate rows.
* Muse Code, Antigravity and DeepSeek Harness: no documented token telemetry was found, so their processes
  are listed from the process table (`ps`, with `lsof` for the directory) and marked not measured.
* Claude Code documents no file exporter (`otlp`, `prometheus`, `console`, `none`) and Codex only OTLP
  (`otlp-http`, `otlp-grpc`, `none`), so the events need an OpenTelemetry Collector. The manifest reads
  the Collector `file` exporter's output, JSON Lines of OTLP/JSON log requests, at
  `~/.claude/canon/otel/logs.jsonl` (`CANON_OTLP_FILE` overrides it). It copies out only the attributes
  named above; Codex also sends account fields, which are never read.
* The Collector of tier 3 writes that file. A session started before the telemetry settings reached
  `~/.claude/settings.json` sends nothing and reads "not measured" until it restarts. The manifest reads
  only the current `logs.jsonl`, not its rotated copies. A rotation therefore drops the window's earlier
  events, and one is due only every 10 MB.

**Gap, stated plainly:** no fleet or stall-detection tool has converged upstream. `scripts/lanes` is thin
glue over documented hook payloads and is provisional; it is replaced when an upstream tool earns adoption.

### Consequences

* Good: works now; nothing sensitive is logged; every signal comes from a documented payload.
* Bad: the ledger sees only sessions on this machine with the hooks installed; cloud sessions are invisible
  to it. Stall detection is a heuristic. Model-change detection and the cost and context columns need a
  status line, so they cover terminal sessions only.

### Confirmation

`scripts/lanes --selftest` replays a synthetic ledger and checks each flag fires. `scripts/test-lanes` runs `--rates`
against fixture files (a ledger, a snapshot, an OTLP file shaped as both runtimes' events, and `ps` and
`lsof` stubs). It checks the rate and spend arithmetic, that only Codex's `response.completed` events
count, that a helper child process is not a second worker, that a session with no telemetry and a missing
file each say why they are not measured, and that no attribute beyond the named ones reaches the output.
On 2026-11-10 a run on this machine listed the open Claude Code sessions, every one not measured because
no OTLP file exists yet, and no worker of another runtime was running.

`scripts/test-otel` runs the pinned `otelcol` with `home/.claude/canon/otel/config.yaml` on two free ports
of 127.0.0.1. It sends one OTLP/JSON request over HTTP, holding Claude Code's `api_request` and
`user_prompt` and Codex's `response.completed`, a `.delta` event, `codex.turn_cost` and a
`codex.tool_result` with arguments and output, with email, account and host fields on the resource and on
the records. It checks that the file keeps exactly the three events with only the named attributes, that
no argument, output, prompt, email, account or resource attribute reaches it, that the Collector listens
on the two ports of 127.0.0.1 and no other, and that `scripts/lanes --rates` gives the Codex conversation
2 tok/s from the written line. `scripts/test-lanes` checks the agent's plist, the validation before
install, the status row's states, uninstall, and the bootstrap's merge into an existing
`settings.json` and Codex config. `scripts/test-canon` checks the lint reads a `version` from a one-line
inline table in `mise.toml`.

Rehearsed on 2026-11-10 with a Collector run from the worktree on scratch ports. A real Claude Code
request (`claude -p`, with the env block above in its environment, pointed at the scratch gRPC port)
arrived as one `api_request` record carrying `session.id`, `model`, `output_tokens`, `duration_ms`,
`cost_usd`, `event.name` and `event.timestamp` and nothing else. A fixture request through
`scripts/lanes --rates` gave the Codex conversation row 2 tok/s. No real Codex run was sent: that Codex
exports to this endpoint with this table is UNVERIFIED, read from its source and docs only.

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
* https://code.claude.com/docs/en/settings-reference.md — `showThinkingSummaries`: default `false`, when
  "the Anthropic API redacts thinking blocks"; `true` gives "full thinking summaries when you expand
  thinking with `Ctrl+O`"; "Redaction changes only what you see" (fetched 2026-11-10, not summarised).
* https://github.com/anthropics/claude-code-monitoring-guide — collector, Prometheus and Grafana reference
  (resolved to `02777441f2a3`).
* https://github.com/ryoppippi/ccusage — MIT, v20.0.48 (resolved to `8995f3b7fd71`).
* https://github.com/mercurialsolo/claudectl — 204 stars; the only fleet tool fetched. Other candidates were
  not fetched: UNVERIFIED. No leader.
* Second reader, 2026-11-09 (`evidence-verifier`): the telemetry names, hook events and status-line fields
  are supported. `PermissionDenied` is narrower than first written (auto mode only), and the ledger's field
  list was incomplete; both corrected. The monitoring guide's last change was 2025-07-29, so check it
  against the current telemetry docs before using it.
* Rates manifest, read 2026-11-10. Statusline page fetched in full and searched: `total_output_tokens` is
  "from the most recent API response"; updates are debounced at 300 ms. Monitoring page fetched in full:
  the `claude_code.api_request` attributes above; exporter values; no file exporter.
  https://learn.chatgpt.com/docs/config-file/config-advanced — `[otel]` is off by default, exporters
  `none`, `otlp-http`, `otlp-grpc`; "token counts on response.completed". The attribute names
  (`output_token_count`, `conversation.id`, `model`, `event.timestamp`, `usage.estimated_usd`) were read from
  `codex-rs/otel/src/events/session_telemetry.rs` and `shared.rs` at the pinned `rust-v0.167.0`; the
  shared fields include `user.email`.
  https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/ (status Development) and
  https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/fileexporter —
  JSON Lines of OTLP/JSON, the file exporter's default `format: json`. 64-bit integers arrive as decimal
  strings (https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md), so
  both forms are read.
  Muse Code (https://dev.meta.ai/docs/muse-code), Antigravity (https://antigravity.google/docs/settings/)
  and DeepSeek Harness (https://github.com/deepseek-ai/deepseek-harness, `docs/architecture.md`) document
  no token telemetry on the pages read. The Antigravity and Muse figures that third parties and an
  Antigravity issue describe (`agy --print` JSON `usage`, `muse usage`) are UNVERIFIED and not used. A
  The Collector adoption and its distribution were settled the same day (below).
* Collector, read 2026-11-10 at v0.165.0 (released 2026-11-07, asset list read with `gh api`):
  https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-releases/v0.165.0/distributions/otelcol/manifest.yaml
  (`otlpreceiver`, `fileexporter`, `filterprocessor`, `transformprocessor`; `otelcol-otlp` has no file
  exporter); https://github.com/open-telemetry/opentelemetry-collector-releases (Apache-2.0; no Homebrew
  formula in the project's tap, which has only `ocb`);
  https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/v0.165.0/exporter/fileexporter/README.md
  (`path` required, `format` default `json`, `rotation` `max_megabytes`, `max_days`, `max_backups`; alpha
  for logs); https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector/v0.165.0/receiver/otlpreceiver/README.md
  (`protocols.grpc.endpoint`, `protocols.http.endpoint`, `/v1/logs`);
  `processor/filterprocessor/README.md` (`log_conditions`, `error_mode`; a matching record is dropped) and
  `processor/transformprocessor/README.md` (`log_statements`, `keep_keys`, `set`) in the contrib
  repository at v0.165.0; https://opentelemetry.io/docs/collector/internal-telemetry/
  (`service::telemetry::metrics::level: none`) and https://opentelemetry.io/docs/collector/configuration/
  (`otelcol validate`, `${env:NAME}`). Fetches of the docs pages were model-summarised; the READMEs and the
  manifest were read from raw files.
  https://mise.jdx.dev/dev-tools/backends/github.html: `matching` "when a release ships several tools for
  each platform"; `mise lock` then recorded `otelcol_0.165.0_<os>_<arch>.tar.gz` for every platform. `mise
  registry` lists no otelcol, and mise found no aqua package for it.
  Claude Code's content settings and their defaults, and its standard attributes (`user.email` "Always
  included when available", with no switch; `organization.id`, `user.account_uuid`, `user.account_id`):
  https://code.claude.com/docs/en/monitoring-usage.md (fetched 2026-11-10, model-summarised). Codex's `[otel]` keys: `OtelConfigToml` and `OtelExporterKind`
  in `codex-rs/config/src/types.rs`, `ToolResultLogConfig` (default 2048 bytes) in
  `codex-rs/protocol/src/config_types.rs`, `codex.tool_result`'s log fields in
  `codex-rs/otel/src/tool_result.rs`, and `CODEX_HOME` in `codex-rs/utils/home-dir/src/lib.rs`, all at
  rust-v0.167.0.
* Restated 2026-11-10 against the headers of `scripts/lanes` and `home/.claude/canon/ledger.sh`: the
  ledger's time fields and file date, the session states and the limits.
