#!/bin/bash
# Lane watchdog. Run by a launchd user agent every 10 minutes while the Mac is awake.
# Contract: launchd.plist(5), key StartInterval. Installed by scripts/automation install (ADR 0011).
#
# Reads the event ledger through ~/.claude/canon/lanes --json. The first time a live session shows a flag
# (QUIET, failures, api-failure, model-switch, denied, compactions) it:
#   - appends the alert to ~/.claude/canon/watch/alerts.jsonl
#   - raises one macOS notification (best effort; the alert file is the record)
# Each (session, flag) alerts once: ~/.claude/canon/watch/alerted.json keeps the keys already alerted until
# their session leaves the report window. It always rewrites ~/.claude/canon/watch/lanes-status.json so a
# broken watchdog is visible; an empty ledger counts as ok only while the ledger hook is installed and
# registered in ~/.claude/settings.json.
# CLAUDE_CONFIG_DIR replaces ~/.claude. CANON_WATCH_NO_NOTIFY=1 keeps the alert file and skips the notification.
# It runs no model, costs no tokens, and never deletes anything.
PATH=/usr/bin:/bin:/usr/sbin:/sbin
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
base="$cfg/canon"
w="$base/watch"
mkdir -p "$w" || exit 1
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

fail() {
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg at "$now" --arg e "$1" '{checked_at: $at, ok: false, error: $e}' > "$w/lanes-status.json"
  else
    printf '{"checked_at":"%s","ok":false,"error":"jq not found"}\n' "$now" > "$w/lanes-status.json"
  fi
  echo "$now ERROR $1" >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || fail "jq not found"
[ -x "$base/lanes" ] || fail "lanes script missing at $base/lanes; run scripts/automation install"

# ledger_registered: true when settings.json runs an executable $base/ledger.sh for UserPromptSubmit and Stop.
# A command is resolved the way this repo writes it: quotes dropped, $HOME and a leading ~ expanded.
ledger_registered() {
  local ev c p found
  for ev in UserPromptSubmit Stop; do
    found=0
    while IFS= read -r c; do
      p=${c//\"/}
      case "$p" in \~/*) p="$HOME/${p#\~/}" ;; esac
      p=${p//\$\{HOME\}/$HOME}; p=${p//\$HOME/$HOME}
      if [ "$p" = "$base/ledger.sh" ] && [ -x "$p" ]; then found=1; fi
    done < <(jq -r --arg ev "$ev" '.hooks[$ev][]?.hooks[]?.command // empty' "$cfg/settings.json" 2>/dev/null)
    [ "$found" = 1 ] || return 1
  done
}

ledger_kb=$(du -sk "$base/ledger" 2>/dev/null | awk '{print $1}'); ledger_kb=${ledger_kb:-0}

report=$("$base/lanes" --json 2> "$w/last-stderr.txt"); rc=$?
if [ "$rc" -eq 4 ]; then
  ledger_registered || fail "ledger empty, and no executable $base/ledger.sh is registered for UserPromptSubmit and Stop in $cfg/settings.json; run scripts/bootstrap --user-config"
  jq -n --arg at "$now" --argjson kb "$ledger_kb" \
     '{checked_at: $at, ok: true, sessions: 0, flagged_now: 0, new_alerts: 0, ledger_kb: $kb,
       note: "no ledger events in the window; the ledger hook is registered"}' > "$w/lanes-status.json"
  exit 0
fi
[ "$rc" -eq 0 ] || fail "lanes exited $rc: $(head -c 300 "$w/last-stderr.txt")"

# flags on sessions that are still live; one key per (session, flag)
current=$(jq -c '[.sessions[] | select(.state != "ended" and .state != "abandoned") | . as $s
                  | .flags[] | {key: ($s.sid + ":" + .), sid: $s.sid, lane: $s.lane, flag: .,
                                state: $s.state, quiet_min: $s.quiet_min, fail: $s.fail}]' <<<"$report") \
  || fail "could not read the lanes report"
sids=$(jq -c '[.sessions[].sid]' <<<"$report") || fail "could not read the lanes report"
prev='[]'
if [ -f "$w/alerted.json" ] && jq -e 'type == "array"' "$w/alerted.json" >/dev/null 2>&1; then prev=$(cat "$w/alerted.json"); fi
new=$(jq -cn --argjson cur "$current" --argjson prev "$prev" '$cur | map(select(.key as $k | ($prev | index($k)) == null))')
count=$(jq 'length' <<<"$new")

notified=false
if [ "$count" -gt 0 ]; then
  jq -c --arg at "$now" '.[] | {at: $at} + .' <<<"$new" >> "$w/alerts.jsonl"
  msg=$(jq -r 'map(.lane + ": " + .flag) | unique | .[0:4] | join("  ·  ")' <<<"$new")
  # text is passed as arguments, never spliced into the script, so a directory name cannot inject AppleScript.
  if [ "${CANON_WATCH_NO_NOTIFY:-0}" != "1" ] && /usr/bin/osascript -e 'on run argv' \
       -e 'display notification (item 1 of argv) with title "Canon lanes" subtitle (item 2 of argv)' \
       -e 'end run' "$msg" "$count new flag(s). Run scripts/lanes" >/dev/null 2>&1; then
    notified=true
  fi
fi

# remember every key alerted so far, dropping those whose session is no longer in the report
if ! jq -cn --argjson cur "$current" --argjson prev "$prev" --argjson sids "$sids" \
     '($prev + ($cur | map(.key))) | unique
      | map(select(sub(":[^:]*$"; "") as $s | $sids | any(. == $s)))' > "$w/alerted.json.tmp" \
   || ! mv "$w/alerted.json.tmp" "$w/alerted.json"; then
  fail "could not write $w/alerted.json"
fi
jq -n --arg at "$now" --argjson kb "$ledger_kb" --argjson new "$count" --argjson notified "$notified" \
      --argjson sessions "$(jq '.sessions | length' <<<"$report")" --argjson flagged "$(jq 'length' <<<"$current")" \
      '{checked_at: $at, ok: true, sessions: $sessions, flagged_now: $flagged, new_alerts: $new,
        notified: $notified, ledger_kb: $kb}' > "$w/lanes-status.json"
exit 0
