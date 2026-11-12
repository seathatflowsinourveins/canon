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
  attributes the manifest reads, clears the resource and scope attributes and every other text field a
  record can carry (event name, severity text, trace and span ids, scope name and version, schema URLs),
  and turns a count or cost sent as a decimal string into a number. It removes a model name with a
  character beyond letters, digits and `. _ - : [ ]` (an `@` and eight digits may end it, as Vertex AI model
  IDs do) and a time that is not ISO 8601, so neither an address nor a path is kept and the record still
  counts. A second `filter` then drops a record whose count or cost is not a number or whose identifier has a
  character beyond letters, digits, `_` and `-`. It fails closed (`error_mode: propagate`): a condition that
  errs drops the whole request, valid records in it included. This holds for anything that reaches 127.0.0.1, not only the two clients. **The filter is the guard,
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
  to `~/.claude/canon/watch/otel-collector.log`, which each install empties. Neither otelcol (its
  `service.telemetry.logs` takes no rotation key at v0.165.0) nor launchd rotates that file. **Known limit:**
  at `level: warn` a healthy run writes nothing to it, and a failed start (measured with a port already in
  use) writes 439 bytes, two lines naming the error. The agent's `ThrottleInterval` of 60 s (launchd's default
  is 10) spawns it at most 1440 times a day, so a restart loop grows the file by at most about
  1440 x 439 B = 0.63 MB a day until the next `scripts/automation install` empties it; at the default it
  would be about 3.8 MB. `KeepAlive` stays `true`: restarting only on failure (`SuccessfulExit` false) would
  leave the Collector down after a SIGKILL or a clean exit. `status` shows whether
  launchd has the agent running and how old `logs.jsonl` is. `uninstall` removes the agent and the
  config and keeps the events.
* **Claude Code** gets `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp`,
  `OTEL_METRICS_EXPORTER=none`, `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` and
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317`, in the `env` block of `home/.claude/settings.json`.
  Every content setting (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS`,
  `OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES`) stays unset, which is off. The variables are ignored
  in a project's settings, so `scripts/bootstrap --user-config` adds the ones an existing
  `~/.claude/settings.json` lacks, and keeps a copy of the previous file. A value the owner set
  differently is left alone and named, as the owner's choice, and `--check` shows it as `OWNER`. So is
  an `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or `OTEL_EXPORTER_OTLP_LOGS_PROTOCOL` in the settings or the
  environment, which overrides the general endpoint for logs, so they do not reach the local Collector. A
  settings file that cannot be written fails the run.
* **Codex** gets the `[otel]` table of `home/.codex/otel.toml`: `exporter = { otlp-http = { endpoint =
  "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }`, `log_user_prompt = false` (its default) and
  `log_sse_deltas = false`. The last one departs from Codex's default (`true`) so that the per-token
  `.delta` events are not sent at all; completion events still are. `otel` is ignored in a project's
  `.codex/config.toml`, so `scripts/bootstrap --user-config` appends the table to
  `${CODEX_HOME:-~/.codex}/config.toml` when that folder exists (Codex is installed) and the file, read as
  TOML with Python's `tomllib` (never a pattern), sets no
  `otel` of its own. It keeps a copy of the previous file and changes nothing else in it. A different
  `otel` setting is left alone and named as the owner's choice. A file that is not TOML is left alone and
  named `INVALID`; with no `python3` that has `tomllib`, nothing is written and the row is `UNKNOWN`. The
  appended copy must read back as TOML with canon's table before it replaces the file.

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
  `~/.claude/settings.json` sends nothing and reads "not measured" until it restarts. The manifest also reads
  the rotated copies the file exporter leaves beside `logs.jsonl` (`logs-<UTC time>-size.jsonl`) that were
  written within the window, so a rotation drops none of the window's events. A row whose window holds
  events but no output token count (a cost-only turn, a completion with no usage) is "not measured", not 0.

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

Fixes after a second reader's review, 2026-11-10, each with a test that failed before it:

* `scripts/test-otel` sends a second request, as a local producer that is not one of the clients would, with an
  email address and a home path in the event name, severity text, trace and span ids, scope name, version and
  attributes, schema URLs, a count, the identifiers, the model, the time and an `event.kind`; none reaches the
  file, the well-formed record keeps its count as a number, the four with text for a count or an identifier
  are dropped, and the two with text for a model or a time keep their record without it; a Vertex AI model
  ID and a time with an offset are kept. Rehearsed the same
  day with the pinned Collector on scratch ports: a real `claude -p` request arrived as one `api_request` with
  only the named attributes, and of that request only the well-formed record was written.
* `scripts/test-lanes`: a quoted `['otel']` table is the owner's and is left valid; an `[otel]` line inside a
  multi-line string gets canon's table added; a config that is not TOML is `INVALID`; with no `tomllib`,
  nothing is written (`UNKNOWN`); an unwritable `settings.json` fails the run and is not reported added; a
  per-signal logs endpoint or protocol, in the settings or the environment, is `OWNER` and not called local,
  named without its value; canon's table with a key of the owner's added is still canon's.
* `scripts/test-lanes`: five completions in a window, four of them in a rotated copy, give 10 tok/s; a
  completion with no token count and a cost-only turn are not measured, their spend kept.
* `scripts/test-lanes`: the Collector agent's plist sets `ThrottleInterval` 60; the bytes of a failed start
  were measured with the pinned otelcol on a port in use (439 bytes).
* `scripts/test-canon`: the pipefail rule finds a pipe whose reader starts the next line.

**Usage against the subscription limits (added 2026-11-11).** The status line also writes, per session,
`~/.claude/canon/rate-limits/<session>.json` atomically (a temporary file, then a rename). The file holds the
input's `rate_limits.five_hour` and `rate_limits.seven_day`, each window's `used_percentage` and `resets_at`,
and a time `t`. Nothing else from the input goes in, since the input carries the session's paths and model.
A window outside the documented types, outside 0 to 100, or with a `resets_at` not within the next eight
days is dropped. The status line also re-renders an idle session with its last response's numbers, and
again when a window reaches its `resets_at` (Claude Code then drops that window). So `t` moves only for
something new: a window that is new or changed, or a session cost (`cost.total_cost_usd`, cumulative) that
moved since the reading last came up to date. The session snapshot keeps that cost as `rl_cost`. A window
that only disappeared, or a re-render, leaves the file alone. That every response moves the cost is
UNVERIFIED. An input with neither window, from a non-subscriber or from before the session's first API
response, leaves the file as it was; `spend_limit` is not read. Each session writes only its own file, so no
session's reading overwrites another's. A session's first file removes files unchanged for more than eight
days. `scripts/lanes` takes, per window, the reading with the newest `t` among files changed in the last
eight days. It prints a Usage row: the 7-day and 5-hour percentages, each with its reset time and the age of
its reading. A window is "not reported" when there is no readable reading, when the window is absent, when
its newest reading is 30 or more minutes old (no older number is shown as current), and when it is past its
reset. An unreadable file is skipped and counted, and a failure to read the usage never stops the session
report. With `--json` the same object is `.usage`; with no ledger the row still prints, as `{"usage": ...}`
with `--json`, and the exit stays 4. The status line also now uses a session id only if it is plain
(letters, digits, `_`, `-`), as an id with `/` or `..` would have written its snapshot outside `sessions/`.

The status-line input has no per-model window. The Agent SDK documents two related sources, neither read
here. TypeScript's `SDKUsageReport` is experimental. Its `rate_limits.limits[]` rows have a `kind` such as
`weekly_scoped` and an optional `scope` that is "a model or a surface". It is attached to a `/usage` reply
only in a session with a claude.ai credential, with a known plan type or the `user:profile` scope, on an
account not billed by usage. Python's `RateLimitEvent` is "emitted when rate limit status changes"; its
`RateLimitInfo` can name `seven_day_opus` or `seven_day_sonnet`, so it is an event, not a running reading.
A per-model reading, such as one model's weekly meter, stays the owner's reading in the app until a
decision takes up the SDK source.

**The display (added 2026-11-11).** Claude HUD renders the status line when the owner has it installed
(ADR 0023's exception). `statusline.sh` stays the configured command. It does the writes above, then runs
the newest installed version's `hud.mjs` (by number) in the marketplace that the user `settings.json`'s
`enabledPlugins` enables, with `node` from PATH, else mise's. It does not use the plugin's launcher, which
would load the newest copy from any marketplace's cache, a stale one kept 14 days included. Another scope's
`enabledPlugins` entry is not read, since canon installs it with `--scope user`. The HUD gets the input
with `model.display_name` set to the documented `model.id`, so the model actually in use is shown. The
lines it prints, and canon's own line, lose their terminal hyperlinks (the HUD links the branch to
`github.com/<owner>/<repo>` and the project to a `file://` path, with no setting to stop it). They are also
scrubbed of e-mail addresses, and of the input's `workspace.repo.owner` and the home folder's user name as
whole words of three characters or more. This repository's branch names begin with the owner's handle,
which shows as `~`. With the plugin absent, canon's own line is shown. If the HUD fails, prints nothing, or
prints its own failure line (an internal error or an old Node, a line naming `/hud`), canon's line is shown
marked `(hud failed)`. On each render the HUD also writes `~/.claude/plugins/claude-hud/heartbeat.json`
(mode 600), holding the status-line input, and its own data folder, on this machine only.
`home/.claude/claude-hud.json`, which `scripts/bootstrap --user-config` installs beside `settings.json`,
sets the HUD's options at v0.15.0 (`src/config.ts`, `src/schema.ts`). The choices, by group:

* **Shown.** The model with effort as text: the ID in use and its effort. The project, one path level: the
  lane's folder, never a full path. The git branch with `*` for uncommitted changes and ahead/behind counts:
  a branch is unsafe to leave until it is pushed. A context bar and value as `40% (120k/300k)`: tokens and
  percent. The bar measures against the session's own compaction point, with the warning colour at 70% and
  critical at 85% of it: `autoCompactWindow` is left `null` here, since one value would serve every model,
  and `statusline.sh` sets the input's `context_window_size` to the window Claude Code documents for the
  session (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, else the user settings' `modelSettings.<model>`, which
  `/autocompact` writes under the canonical name that `[1m]`, date-suffixed and provider IDs also match,
  else `autoCompactWindow`, capped at the model's window; model-config.md and settings-reference.md, read
  2026-11-12) and drops `used_percentage`, which measures the full window. Not followed, since the status
  line cannot see them: a `--autocompact` flag, the project, local and managed settings scopes, and the
  window tuned for the model when none is set or the model's entry is `"auto"`; there the bar, like
  canon's own line and `ctx_pct`, measures the full window. Amended 2026-11-12: a fixed 300000 pinned a
  model compacting at 600000 at a red 100% from 300k on. The session cost: `cost.total_cost_usd`. The
  duration: the elapsed session time. Usage compact, always, both windows, with countdown and 24-hour reset
  time: one short line at any width. The Claude Code version: the running CLI. A newer installed version
  waiting on a restart is not available, since no documented input field or command gives it.
* **Off, for identifiers.** `showAuth` and `showAuthUser` (plan and account; v0.15.0 has no other
  account key, and an unknown key is dropped),
  `showProvider`, `showSessionName` (free text), `showLaunchDir`, `showAddedDirs` (other paths),
  `showWorktree` (its name repeats the folder), `showModelScopedUsage` (it would read an undocumented stdin
  field), and `externalUsagePath` and `externalUsageWritePath` (canon keeps its own reading).
* **Off, for undocumented sources.** Tools, skills, MCP, agents, todos, advisor, compactions, session start,
  last response and agent effort all come from the session transcript, whose format the docs call internal.
  `modelSource` stays `stdin`.
* **Shown since 2026-11-12: the prompt cache.** One line, `Cache ⏱ until 11:25 │ Cache hit 97.3%`: the clock
  time the cache goes cold and the session's hit rate, from the input's documented `prompt_cache` object
  (statusline.md, read 2026-11-12) and nothing from the transcript
  (`src/render/lines.ts` at v0.15.0). The status line refreshes when a warm cache reaches `expires_at` (statusline.md, "when it updates"; the
  HUD's v0.15.0 comment that it does not repaint between turns predates that). Its fields: `caching_observed`
  (no line without it), `warm`, `expires_at`, `ttl` (the warning colour) and `hit_ratio`; a cold cache
  shows `expired`.
  The cache's TTL decides what a returning session pays to re-cache its context, and idle compaction
  runs just before a one-hour cache expires (settings-reference.md, `idleCompaction`), so the time is
  when an idle lane's next message gets dearer or its context gets summarised.
* **Off, for cost or space.** Memory (it runs `vm_stat`), daily and weekly cost, speed, the token breakdown,
  config counts, output style, the custom line and jj.

Checked on 2026-11-11 at 60 and 100 columns, through `statusline.sh` with the pinned HUD placed in a
temporary config folder's plugin cache: five lines at 60, three at 100, about 0.2 s a render, no hyperlink
left, the branch's handle shown as `~`, and the bar in the warning colour at 250k of 300k. Re-checked on
2026-11-12 with the cache line and a session window of 600k: six lines at 60, three at 100, about 0.17 s a
render. The status line's refresh is event-driven and debounced at 300 ms, and a new
event cancels a run still going (statusline.md).

`scripts/test-usage` feeds the status line fake inputs: both windows, an input without `rate_limits`, an
idle re-render with the same cost and a new response with a higher one, three sessions (an idle one older,
one with a single window), out-of-range windows (`resets_at` 1e18, in milliseconds or past; a percentage of
4120 or -1; wrong types), malformed JSON, an unsafe session id, a corrupt file and an unwritable directory.
It checks the status line exits 0 and prints what it printed before, and that the file holds only the named
fields. It checks the row shows current numbers, and reports as not reported a reading 40 minutes old, a
reset window, a stored `resets_at` too large for a date, and unreadable or missing files. It runs the
renderer path with stubs. A renderer's lines are shown scrubbed of a hyperlink, an address, the owner and
the home user, and it is given the model ID. One that fails, prints nothing or is missing shows canon's
line marked, and so does one printing a line that names `/hud`. A HUD in the cache that settings do not
enable is not run. Of an enabled marketplace's 0.9.0 and 0.15.0, 0.15.0 is run with `node`, and a newer copy
in a marketplace that is not enabled is not; one whose `node` fails shows the marked line. Names are
scrubbed as whole words only (`dev` leaves `devtools`). In `scripts/lanes` and `scripts/automation status`,
which the owner pastes, a path under `$HOME` prints as `~` (a sibling such as `$HOME-old` does not), a
session started in the home folder is lane `~`, and the landing row names the host and repository without
its owner, for an SSH remote too. Three code reviews and two record checks on 2026-11-11
shaped this. A shared merged file relied on two undocumented assumptions (usage only grows within a window,
and `resets_at` stays fixed), and a window's reset re-stamped an idle session's old numbers. The per-session
files, with the cost and changed-window rule, fix both, and each fix has a test that failed before it.

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
  Re-read 2026-11-11 (fetched in full, searched): `rate_limits.five_hour.used_percentage` and
  `rate_limits.seven_day.used_percentage`, "from 0 to 100"; each window's `resets_at`, "Unix epoch seconds";
  `rate_limits` "appears only for claude.ai Pro, Max, and Ultra subscribers, or behind a Claude apps gateway
  that sets a spend limit for you", "only after the first API response in the session", each window "may be
  independently absent", and "Claude Code drops a window once its `resets_at` time passes". The page names
  no per-model window. https://code.claude.com/docs/en/agent-sdk/typescript.md (fetched 2026-11-11, raw):
  `SDKUsageReport`, "experimental: its shape may change", `rate_limits.limits[]` rows with `kind` such as
  `weekly_scoped`, `percent` 0-100, `resets_at` and an optional `scope`, "a model or a surface", attached to
  the `/usage` reply under the conditions above, Agent SDK v0.3.273 or later; its `SDKRateLimitEvent` has
  no window type. https://code.claude.com/docs/en/agent-sdk/python.md (fetched 2026-11-11, raw):
  `RateLimitEvent`, "emitted when rate limit status changes", whose `RateLimitInfo.rate_limit_type` is one of
  `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `overage`, with `utilization` 0.0 to 1.0.
  Whether either reports a weekly meter for every model in use is UNVERIFIED. The statusline page also says
  `cost.total_cost_usd` "accumulates the estimated cost of all API calls in the current session", and lists
  the update triggers above (an idle re-render on a permission-mode change, at a window's `resets_at`).
  Second reader, 2026-11-11 (`evidence-verifier`, twice): the `rate_limits` claims hold; the first text said
  no documented source gives a per-model limit, which the two Agent SDK pages contradict; then the
  `weekly_scoped` scope, the `/usage` conditions and the event's nature were overstated, a gateway sending
  only `spend_limit` was unbacked, and a reset re-stamped an idle reading; all corrected above.
* Claude HUD, read 2026-11-11 from a clone at tag v0.15.0 (`b86e1c3e5255`, `resolved`): `src/config.ts`
  and `src/schema.ts` (every option above and its default), `src/utils/hyperlinks.ts` and `src/git.ts`
  (the OSC 8 links and their targets), `scripts/statusline.mjs`, `hud.mjs` and `fence.mjs` (ADR 0023).
  https://code.claude.com/docs/en/plugins/security.md: installed copies under
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, kept 14 days after removal;
  https://code.claude.com/docs/en/settings-reference.md: `enabledPlugins` keyed by
  `plugin-name@marketplace-name` (both `fetched`, raw).
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
* Second reader's fixes, read 2026-11-10 at the pins. OTTL at contrib v0.165.0, raw files:
  `pkg/ottl/contexts/ottllog/README.md` (`log.event_name`, `log.severity_text`, `log.trace_id.string`,
  `log.span_id.string`), `pkg/ottl/contexts/ottlscope/README.md` (`scope.name`, `scope.version`),
  `pkg/ottl/ottlfuncs/README.md` (`Int`, `Double`, `IsInt`, `IsDouble`, `IsString`, `IsMatch`,
  `delete_key`), `processor/filterprocessor/README.md` (`error_mode: propagate` drops the payload), and the
  `schema_url` paths, which the READMEs omit, in `pkg/ottl/contexts/internal/ctxresource/resource.go` and
  `ctxscope/scope.go`. `otelcol validate` refused `service.telemetry.logs.rotation` ("invalid keys:
  rotation"). launchd.plist(5) on this Mac: `ThrottleInterval` ("by default, jobs will not be spawned more
  than once every 10 seconds"), `KeepAlive` `SuccessfulExit` and `Crashed`. Rotated file names: `exporter/fileexporter/README.md`, "File Rotation" (`data.json` becomes
  `data-<time>-size.json`). Per-signal precedence: https://code.claude.com/docs/en/monitoring-usage.md
  (`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` and `_PROTOCOL` each "overrides general setting") and
  https://opentelemetry.io/docs/specs/otel/protocol/exporter/ ("The per-signal endpoint configuration options
  take precedence"). `tomllib`: https://docs.python.org/3/library/tomllib.html (Python 3.11 and later; the
  pinned Python is 3.14, macOS's `/usr/bin/python3` has none).
* ccusage pin moved 2026-11-11 to `tag:v20.0.49` → `d1d95c994067` (resolved).
  https://github.com/ryoppippi/ccusage/releases/tag/v20.0.49 — published 2026-11-10: 1h cache writes in JSON
  model breakdowns, a cline adapter, in-window counting in unified session reports, parser fixes and a
  faster `ccusage claude`; nothing breaking (`gh`). The 2026-11-09 run above used 20.0.48.
