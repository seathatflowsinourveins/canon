#!/bin/bash
# The status line's first-line label: the session's lines added and removed, when it has changed any, then the
# fleet's output speed, every measured worker's output tokens per second summed and how many Claude Code sessions
# are working, e.g. "+156 -23 │ Fleet 368 tok/s · 9 working" (ADR 0007).
# Usage: fleet-rate.sh [<lines added> <lines removed>]. statusline.sh passes the input's documented
# cost.total_lines_added and cost.total_lines_removed (https://code.claude.com/docs/en/statusline.md); the lines
# are shown only when both are whole numbers of at most nine digits and one is above zero, a count of a million or
# more as the HUD writes token counts (1.2M, src/utils/format.ts at v0.15.0), so the label keeps within the HUD's 50
# characters. The " │ " between the two parts is the HUD's own separator, so a line too narrow for both wraps there.
# Contract: Claude HUD's --extra-cmd (https://github.com/jarrodwatts/claude-hud README, "--extra-cmd"; at
# v0.15.0 src/extra-cmd.ts and collector.mjs): the HUD runs this on every render when CLAUDE_HUD_ALLOW_EXTRA_CMD
# is set, gives it 3 s, and shows the "label" of the JSON it prints (at most 50 characters) on the first line.
# The figures are scripts/lanes --rates (installed as <config>/canon/lanes by scripts/automation install), whose
# window is its last 10 minutes of OTLP events. That report takes about 4 s, too long for a render, so this
# prints the summary a previous run cached in <config>/canon/fleet-rate.json at once and, when that is older
# than FLEET_RATE_TTL seconds (60), starts one refresh in the background under a lock (mkdir), which writes
# the summary through a temporary file. The TTL counts from the last attempt (its time in fleet-rate.tried), not the last
# success, and the report runs under timeout(1) for FLEET_RATE_SECS (50), so a report that fails or hangs
# still runs at most once a TTL, and only while a status line renders. A summary older than
# FLEET_RATE_MAX_AGE (600), or dated ahead of the clock, no summary, or no installed report shows no fleet
# figure rather than an old one. A lock older than two minutes is taken as left by a stopped run, and a run
# removes the lock only while it is still its own. A render starts one process, jq, which reads the summary,
# takes the time and composes the label; the shell's own reads do the rest.
# CLAUDE_CONFIG_DIR replaces ~/.claude; FLEET_RATE_LANES replaces the report's path (tests only).
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
base="$cfg/canon"
cache="$base/fleet-rate.json"
tried="$base/fleet-rate.tried"
lock="$base/fleet-rate.lock"
lanes="${FLEET_RATE_LANES:-$base/lanes}"
ttl="${FLEET_RATE_TTL:-60}"
max_age="${FLEET_RATE_MAX_AGE:-600}"
secs="${FLEET_RATE_SECS:-50}"
case "$ttl" in ''|*[!0-9]*) ttl=60 ;; esac
case "$max_age" in ''|*[!0-9]*) max_age=600 ;; esac
case "$secs" in ''|*[!0-9]*) secs=50 ;; esac
command -v jq >/dev/null 2>&1 || exit 0
# the last attempt's time: the first 20 characters of its file when they are digits, else none
a=""
if [ -f "$tried" ] && [ -r "$tried" ]; then IFS= read -r a < "$tried"; fi
a=${a:0:20}
case "$a" in ''|*[!0-9]*) a=0 ;; esac
src="$cache"; { [ -f "$src" ] && [ -r "$src" ]; } || src=/dev/null
# Line 1: the time, and "due" when a refresh is (the last attempt, or the summary when newer, at least a TTL ago;
# one dated ahead of the clock counts as none). Line 2, when there is anything to show: the label as the HUD's JSON.
out=$(jq -Rrs --arg a "$a" --arg ttl "$ttl" --arg max "$max_age" --arg add "${1:-}" --arg del "${2:-}" '
    def count: if test("^[0-9]{1,9}$") then tonumber else null end;
    def short: if . >= 1000000 then (. / 100000 | round) as $t | "\($t / 10 | floor).\($t % 10)M" else tostring end;
    (now | floor) as $now
    | (try fromjson catch null) as $c
    | (if ($c | type) == "object" and ($c.t | type) == "number" then $c.t | floor else 0 end) as $t
    | ([($a | tonumber), $t] | max | if . > $now then 0 else . end) as $last
    | (if $t <= $now and $now - $t < ($max | tonumber) and ($c.label? | type) == "string" then $c.label else null end) as $fleet
    | ($add | count) as $ad | ($del | count) as $de
    | (if $ad != null and $de != null and $ad + $de > 0 then "+\($ad | short) -\($de | short)" else null end) as $lines
    | "\($now) \(if $now - $last >= ($ttl | tonumber) then "due" else "fresh" end)",
      ([$lines, $fleet] | map(select(. != null)) | if length > 0 then {label: join(" │ ")} | tojson else empty end)
  ' "$src" 2>/dev/null) || exit 0
first=${out%%$'\n'*}; label=""
[ "$first" = "$out" ] || label=${out#*$'\n'}
now=${first%% *}
case "$now" in ''|*[!0-9]*) exit 0 ;; esac

if [ "${first#* }" = due ] && [ -x "$lanes" ]; then
  # a lock left by a run that was stopped
  [ -z "$(find "$lock" -maxdepth 0 -mmin +2 2>/dev/null)" ] || { rm -f "$lock/owner"; rmdir "$lock" 2>/dev/null; }
  if mkdir "$lock" 2>/dev/null; then
    printf '%s\n' "$now" > "$tried" 2>/dev/null
    (
      me="$$-$RANDOM-$now"; tmp=""
      printf '%s\n' "$me" > "$lock/owner"
      # the lock only while it is still this run's; the temporary file in any case
      trap '[ "$(cat "$lock/owner" 2>/dev/null)" != "$me" ] || { rm -f "$lock/owner"; rmdir "$lock" 2>/dev/null; }; [ -z "$tmp" ] || rm -f "$tmp"' EXIT
      tmp=$(mktemp "$base/fleet-rate.XXXXXX") || exit 0
      run=("$lanes"); [ ! -x /usr/bin/timeout ] || run=(/usr/bin/timeout "$secs" "$lanes")
      if "${run[@]}" --rates --json 2>/dev/null | jq -c '
           (.workers // []) as $w
           | ([$w[] | select(.measured == true) | .tok_s | numbers] | add // 0) as $r
           | ([$w[] | select(.runtime == "claude-code" and .state == "working")] | length) as $n
           | {t: (now | floor), label: "Fleet \($r | round) tok/s · \($n) working"}' > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        mv -f "$tmp" "$cache" && tmp=""
      fi
    ) </dev/null >/dev/null 2>&1 &
  fi
fi

[ -z "$label" ] || printf '%s\n' "$label"
exit 0
