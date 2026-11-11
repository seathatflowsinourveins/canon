#!/bin/bash
# Lane watchdog. Run by a launchd user agent every 10 minutes while the Mac is awake.
# Contract: launchd.plist(5), key StartInterval. Installed by scripts/automation install (ADR 0011).
#
# Reads the event ledger through ~/.claude/canon/lanes --json. The first time a live session shows a flag
# (QUIET, failures, api-failure, model-switch, denied, compactions) it:
#   - appends the alert to ~/.claude/canon/watch/alerts.jsonl
#   - raises one macOS notification (best effort; the alert file is the record)
# Each (session, flag) alerts once: a key in ~/.claude/canon/watch/alerted.json, which keeps the keys alerted
# while their session is in the report window, or in alerts.jsonl's last 2000 alerts (a run stopped after it
# wrote the alert and before it remembered the key; a session resumed later with the same flag), is not
# alerted again. A notification that fails is kept in
# unnotified.json and sent again at the next run while its flag stands. It always rewrites
# ~/.claude/canon/watch/lanes-status.json, through a temporary file, so a broken watchdog is visible, and
# exits non-zero when it cannot; `scripts/automation status` reads its age. An empty ledger counts as ok only
# while the ledger hook is installed and registered in ~/.claude/settings.json.
# The lanes report runs under timeout(1) (macOS's /usr/bin/timeout) for LANES_SECS, so a report that hangs
# fails this run instead of holding the job while launchd misses its intervals (launchd.plist(5)).
# CLAUDE_CONFIG_DIR replaces ~/.claude. CANON_WATCH_NO_NOTIFY=1 keeps the alert file and skips the notification.
# CANON_WATCH_OSASCRIPT and CANON_WATCH_LANES_SECS replace osascript and the bound (tests only).
# It runs no model, costs no tokens, and never deletes anything.
# exported: under an empty environment bash keeps an assigned PATH to itself and a child gets no PATH; the
# lanes report, a bash script, then fell back to bash's compiled-in /usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.
# (observed with env -i and /bin/bash 3.2, 2026-11-10); bash(1), ENVIRONMENT and the export builtin
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
base="$cfg/canon"
w="$base/watch"
mkdir -p "$w" || exit 1
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LANES_SECS=${CANON_WATCH_LANES_SECS:-120}   # scripts/automation's WATCH_SECS
OSA=${CANON_WATCH_OSASCRIPT:-/usr/bin/osascript}

# put <file> <content>: replace $w/<file> whole, through a temporary file of this run's own (mktemp(1))
put() {
  local t
  [ ! -d "$w/$1" ] || return 1   # mv would move the file into it
  t=$(mktemp "$w/.$1.XXXXXX") || return 1
  if printf '%s\n' "$2" > "$t" && mv -f "$t" "$w/$1"; then return 0; fi
  rm -f "$t"; return 1
}
fail() {
  if command -v jq >/dev/null 2>&1; then
    put lanes-status.json "$(jq -cn --arg at "$now" --arg e "$1" '{checked_at: $at, ok: false, error: $e}')"
  else
    put lanes-status.json "{\"checked_at\":\"$now\",\"ok\":false,\"error\":\"jq not found\"}"
  fi
  echo "$now ERROR $1" >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || fail "jq not found"
[ -x /usr/bin/timeout ] || fail "no /usr/bin/timeout, which bounds the lanes report"
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

report=$(/usr/bin/timeout -k 5 "$LANES_SECS" "$base/lanes" --json 2> "$w/last-stderr.txt"); rc=$?
[ "$rc" != 124 ] && [ "$rc" != 137 ] || fail "the lanes report did not finish within $LANES_SECS s"
if [ "$rc" -eq 4 ]; then
  ledger_registered || fail "ledger empty, and no executable $base/ledger.sh is registered for UserPromptSubmit and Stop in $cfg/settings.json; run scripts/bootstrap --user-config"
  put lanes-status.json "$(jq -cn --arg at "$now" --argjson kb "$ledger_kb" \
     '{checked_at: $at, ok: true, sessions: 0, flagged_now: 0, new_alerts: 0, ledger_kb: $kb,
       note: "no ledger events in the window; the ledger hook is registered"}')" || { echo "$now ERROR could not write $w/lanes-status.json" >&2; exit 1; }
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
# keys alerts.jsonl already holds, in its last 2000 alerts (a live session's are recent); a line that does
# not parse is skipped
recorded='[]'
[ ! -f "$w/alerts.jsonl" ] || recorded=$(tail -n 2000 "$w/alerts.jsonl" | jq -cRn '[inputs | fromjson? | .key? // empty | strings]') \
  || fail "could not read $w/alerts.jsonl"
new=$(jq -cn --argjson cur "$current" --argjson prev "$prev" --argjson rec "$recorded" \
        '$cur | map(select(.key as $k | ($prev | index($k)) == null and ($rec | index($k)) == null))')
count=$(jq 'length' <<<"$new")
# notifications that failed at an earlier run, for flags that still stand
pend='[]'
if [ -f "$w/unnotified.json" ] && jq -e 'type == "array"' "$w/unnotified.json" >/dev/null 2>&1; then pend=$(cat "$w/unnotified.json"); fi
notify=$(jq -cn --argjson cur "$current" --argjson new "$new" --argjson pend "$pend" \
           '($new + ($cur | map(select(.key as $k | $pend | index($k))))) | unique_by(.key)')
ncount=$(jq 'length' <<<"$notify")

# pending first, before the alert is recorded, so a run stopped anywhere after this sends it next time
notifying=false
if [ "$ncount" -gt 0 ] && [ "${CANON_WATCH_NO_NOTIFY:-0}" != "1" ]; then
  notifying=true
  put unnotified.json "$(jq -c 'map(.key)' <<<"$notify")" || fail "could not write $w/unnotified.json"
fi
[ "$count" -eq 0 ] || jq -c --arg at "$now" '.[] | {at: $at} + .' <<<"$new" >> "$w/alerts.jsonl" \
  || fail "could not append to $w/alerts.jsonl"
notified=false
if [ "$notifying" = true ]; then
  msg=$(jq -r 'map(.lane + ": " + .flag) | unique | .[0:4] | join("  ·  ")' <<<"$notify")
  # text is passed as arguments, never spliced into the script, so a directory name cannot inject AppleScript.
  if "$OSA" -e 'on run argv' \
       -e 'display notification (item 1 of argv) with title "Canon lanes" subtitle (item 2 of argv)' \
       -e 'end run' "$msg" "$ncount new flag(s). Run scripts/lanes" >/dev/null 2>&1; then
    notified=true
    put unnotified.json '[]' || fail "could not write $w/unnotified.json"
  fi
fi

# remember every key alerted so far, dropping those whose session is no longer in the report
keep=$(jq -cn --argjson cur "$current" --argjson prev "$prev" --argjson sids "$sids" \
     '($prev + ($cur | map(.key))) | unique
      | map(select(sub(":[^:]*$"; "") as $s | $sids | any(. == $s)))') || fail "could not read the keys to keep"
put alerted.json "$keep" || fail "could not write $w/alerted.json"
put lanes-status.json "$(jq -cn --arg at "$now" --argjson kb "$ledger_kb" --argjson new "$count" --argjson notified "$notified" \
      --argjson sessions "$(jq '.sessions | length' <<<"$report")" --argjson flagged "$(jq 'length' <<<"$current")" \
      '{checked_at: $at, ok: true, sessions: $sessions, flagged_now: $flagged, new_alerts: $new,
        notified: $notified, ledger_kb: $kb}')" || { echo "$now ERROR could not write $w/lanes-status.json" >&2; exit 1; }
exit 0
