#!/bin/bash
# Claude Code status line command.
# Contract: https://code.claude.com/docs/en/statusline.md  (stdin = session JSON; first stdout line is shown)
#
# Shows: model · effort · context used · session cost · directory.
# Also saves the latest snapshot per session to ~/.claude/canon/sessions/<id>.json for scripts/lanes.
input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  echo "status line: jq not found"
  exit 0
fi
base="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/canon"
dir="$base/sessions"
sid=$(printf '%s' "$input" | jq -r '.session_id? // empty' 2>/dev/null)
if [ -n "$sid" ] && mkdir -p "$dir" "$base/ledger" 2>/dev/null; then
  # A model change inside one session discards the prompt cache and may be an unintended fallback (ADR 0005).
  # Detect it here from the documented status-line input and record it in the event ledger.
  now_model=$(printf '%s' "$input" | jq -r '.model.id? // empty' 2>/dev/null)
  old_model=$(jq -r '.model // empty' "$dir/$sid.json" 2>/dev/null)
  if [ -n "$now_model" ] && [ -n "$old_model" ] && [ "$now_model" != "$old_model" ]; then
    # the directory is left out when the input has none, so it never blanks the lane name
    jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg sid "$sid" --arg from "$old_model" --arg to "$now_model" \
       --arg cwd "$(printf '%s' "$input" | jq -r '.workspace.current_dir? // .cwd? // empty' 2>/dev/null)" \
       '{ts:$ts, ev:"PostModelSwitch", sid:$sid, from_model:$from, to_model:$to, via:"statusline"}
        + (if $cwd == "" then {} else {cwd:$cwd} end)' \
       >> "$base/ledger/$(date -u +%Y-%m-%d).jsonl" 2>/dev/null
  fi
  printf '%s' "$input" | jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{
      ts: $ts, sid: .session_id, cwd: .cwd,
      model: (.model.id? // null), effort: (.effort.level? // null),
      cost_usd: (.cost.total_cost_usd? // null),
      ctx_pct: (.context_window.used_percentage? // null)
    }' > "$dir/$sid.json.tmp" 2>/dev/null && mv "$dir/$sid.json.tmp" "$dir/$sid.json"
fi
printf '%s' "$input" | jq -r '[
    (.model.display_name? // .model.id? // "model ?"),
    (.effort.level? // empty),
    ("ctx " + ((.context_window.used_percentage? // 0) | floor | tostring) + "%"),
    ("$" + ((((.cost.total_cost_usd? // 0) * 100) | round) / 100 | tostring)),
    ((.workspace.current_dir? // .cwd? // "") | split("/") | last)
  ] | map(select(. != null and . != "")) | join(" · ")' 2>/dev/null || echo "status line: could not parse input"
