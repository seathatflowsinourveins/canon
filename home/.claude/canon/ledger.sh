#!/bin/bash
# Claude Code command hook, registered with "async": true for the events listed in settings.json.
# Contract: https://code.claude.com/docs/en/hooks.md  (stdin = one JSON object per event)
#
# Appends one line per event to ~/.claude/canon/ledger/YYYY-MM-DD.jsonl for scripts/lanes (ADR 0007).
# Records names and counts only: never prompts, tool inputs or tool outputs.
# Fields beyond the documented common ones are read defensively and omitted when absent.
# Always exits 0 so it can never interrupt a session; problems go to ledger-errors.log.
base="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/canon"
dir="$base/ledger"
mkdir -p "$dir" 2>/dev/null || exit 0
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if ! command -v jq >/dev/null 2>&1; then
  echo "$now ledger: jq not found; event not recorded" >> "$base/ledger-errors.log"
  exit 0
fi
# ts is whole seconds (ISO 8601, UTC); t is the same moment in epoch seconds with milliseconds, so readers
# can order events that land in the same second.
jq -c '
  now as $n
  | {
    ts: ($n | floor | todate),
    t: (($n * 1000 | floor) / 1000),
    ev: .hook_event_name,
    sid: .session_id,
    cwd: .cwd,
    agent: (.agent_type? // null),
    tool: (.tool_name? // null),
    skill: (if (.tool_name? // "") == "Skill" then (.tool_input.skill? // null) else null end),
    subagent: (if (.tool_name? // "") == "Agent" then (.tool_input.subagent_type? // null) else null end),
    mode: (.permission_mode? // null),
    effort: (.effort.level? // null),
    from_model: (.from_model? // null),
    to_model: (.to_model? // null),
    source: (.source? // null),
    reason: (.reason? // null),
    trigger: (.trigger? // null)
  } | with_entries(select(.value != null))
' >> "$dir/$(date -u +%Y-%m-%d).jsonl" 2>> "$base/ledger-errors.log"
exit 0
