#!/bin/bash
# The status line's fleet label: every measured worker's output tokens per second summed, and how many Claude
# Code sessions are working, e.g. "Fleet 368 tok/s · 9 working" (ADR 0007).
# Contract: Claude HUD's --extra-cmd (https://github.com/jarrodwatts/claude-hud README, "--extra-cmd"; at
# v0.15.0 src/extra-cmd.ts and collector.mjs): the HUD runs this on every render when CLAUDE_HUD_ALLOW_EXTRA_CMD
# is set, gives it 3 s, and shows the "label" of the JSON it prints (at most 50 characters) on the first line.
# The figures are scripts/lanes --rates (installed as <config>/canon/lanes by scripts/automation install), whose
# window is its last 10 minutes of OTLP events. That report takes about 4 s, too long for a render, so this
# prints the summary a previous run cached in <config>/canon/fleet-rate.json at once and, when that is older
# than FLEET_RATE_TTL seconds (60), starts one refresh in the background under a lock (mkdir), which writes
# the summary through a temporary file. The TTL counts from the last attempt (fleet-rate.tried), not the last
# success, and the report runs under timeout(1) for FLEET_RATE_SECS (50), so a report that fails or hangs
# still runs at most once a TTL, and only while a status line renders. A summary older than
# FLEET_RATE_MAX_AGE (600), or dated ahead of the clock, no summary, or no installed report shows no label
# rather than an old figure. A lock older than two minutes is taken as left by a stopped run, and a run
# removes the lock only while it is still its own.
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
command -v jq >/dev/null 2>&1 || exit 0
now=$(date +%s)
t=$(jq -r '.t | numbers | floor' "$cache" 2>/dev/null) || t=""
[ -n "$t" ] || t=0
a=$(stat -f %m "$tried" 2>/dev/null) || a=0
[ "$a" -gt "$t" ] || a=$t
# the last attempt; one dated ahead of the clock counts as none
[ "$a" -le "$now" ] || a=0

if [ $((now - a)) -ge "$ttl" ] && [ -x "$lanes" ]; then
  # a lock left by a run that was stopped
  [ -z "$(find "$lock" -maxdepth 0 -mmin +2 2>/dev/null)" ] || { rm -f "$lock/owner"; rmdir "$lock" 2>/dev/null; }
  if mkdir "$lock" 2>/dev/null; then
    touch "$tried" 2>/dev/null
    (
      me="$$-$RANDOM-$now"; tmp=""
      printf '%s\n' "$me" > "$lock/owner"
      # the lock only while it is still this run's; the temporary file in any case
      trap '[ "$(cat "$lock/owner" 2>/dev/null)" != "$me" ] || { rm -f "$lock/owner"; rmdir "$lock" 2>/dev/null; }; [ -z "$tmp" ] || rm -f "$tmp"' EXIT
      tmp=$(mktemp "$base/fleet-rate.XXXXXX") || exit 0
      run=("$lanes"); [ ! -x /usr/bin/timeout ] || run=(/usr/bin/timeout "$secs" "$lanes")
      if "${run[@]}" --rates --json 2>/dev/null | jq -c --argjson t "$(date +%s)" '
           (.workers // []) as $w
           | ([$w[] | select(.measured == true) | .tok_s | numbers] | add // 0) as $r
           | ([$w[] | select(.runtime == "claude-code" and .state == "working")] | length) as $n
           | {t: $t, label: "Fleet \($r | round) tok/s · \($n) working"}' > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        mv -f "$tmp" "$cache" && tmp=""
      fi
    ) </dev/null >/dev/null 2>&1 &
  fi
fi

[ "$t" -le "$now" ] && [ $((now - t)) -lt "$max_age" ] || exit 0
jq -c '{label: (.label | strings)}' "$cache" 2>/dev/null
exit 0
