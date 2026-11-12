#!/bin/bash
# Claude Code status line command.
# Contract: https://code.claude.com/docs/en/statusline.md  (stdin = session JSON; first stdout line is shown)
#
# Two steps. First, canon's writes for scripts/lanes, each silent on failure and never changing what is shown:
#   * the latest snapshot per session, ~/.claude/canon/sessions/<id>.json: time, session, directory, model,
#     effort, cost, context percentage, and rl_cost (the session's cost when its usage reading last came up to
#     date, used below);
#   * a PostModelSwitch line in the event ledger when the model changes inside a session (ADR 0005);
#   * per session, the subscription usage windows, ~/.claude/canon/rate-limits/<id>.json (ADR 0007):
#     {"rate_limits": {"five_hour" | "seven_day": {used_percentage, resets_at}}, "t": <epoch seconds>}, the
#     input's used_percentage (0 to 100) and resets_at (epoch seconds) and nothing else from the input, which
#     carries the session's paths and model. The input has rate_limits only for Pro, Max and Ultra subscribers
#     or behind a Claude apps gateway with a spend limit, and only after the session's first API response;
#     each window may be absent, and Claude Code drops a window and re-runs the status line once its resets_at
#     passes. A status line also re-renders an idle session with its last response's numbers, so t moves only
#     for something new: a window that is new or changed, or a session cost that moved since the reading last
#     came up to date (a new API response). A window that only disappeared, or a re-render, leaves the file
#     alone. A window outside 0 to 100, or whose resets_at is not within the next eight days, is dropped.
#     Written to a temporary file and renamed; files unchanged for more than eight days are removed when a
#     session writes its first one.
# Second, the lines shown. Claude HUD (https://github.com/jarrodwatts/claude-hud, ADR 0007, ADR 0023) renders them
# when the user settings.json's enabledPlugins sets claude-hud@<marketplace> to true (canon installs it with
# --scope user; another scope's entry is not read): that marketplace's newest version by number,
# <plugins>/cache/<marketplace>/claude-hud/<version>/hud.mjs (the cache layout
# https://code.claude.com/docs/en/plugins/security.md gives; <plugins> is CLAUDE_CODE_PLUGIN_CACHE_DIR when set,
# as the HUD's own launcher reads it), run with node (from PATH, else mise). It reads canon's claude-hud.json beside settings.json.
# It gets the same input with model.display_name set to model.id, so the model actually in use is shown, and,
# when a compaction window applies, context_window.context_window_size set to it and used_percentage removed, so
# the HUD measures its bar against the point where this session compacts (its config sets no autoCompactWindow
# of its own, which would be one window for every model). The window, per
# https://code.claude.com/docs/en/model-config.md ("Set the auto-compact window"): CLAUDE_CODE_AUTO_COMPACT_WINDOW
# when usable (a token count, or with k or M, held to 100K to 1M), else the user settings.json's
# modelSettings.<model.id>.autoCompactWindow (what /autocompact writes), else its autoCompactWindow, capped at the
# input's context_window_size. Not visible here, so not followed: a --autocompact flag, the project, local and
# managed settings scopes, and the tuned window when none is set ("auto" or nothing), where the bar measures the
# full window, as canon's own line and the snapshot's ctx_pct always do. Its
# output loses its terminal hyperlinks (they carry the repository owner and absolute paths), and every line
# shown, the HUD's or canon's, is scrubbed of e-mail addresses, the input's workspace.repo.owner and the home
# folder's user name. CANON_STATUSLINE_RENDERER, when set, names another executable to use in the HUD's place.
# With no HUD installed canon's own line is shown: model · effort · context used · session cost · directory. If
# the HUD (or the named renderer) is there but fails, prints nothing, or prints its own failure line (one that
# names /hud), canon's line is shown with "(hud failed)", never silently.
# The session id names files, so only a plain one (letters, digits, _ and -) is used for the writes.
input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  echo "status line: jq not found"
  exit 0
fi

canon_writes() {
  local base="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/canon" dir sid now_model cwd cost has_rl old_model old_rl usage ufile
  dir="$base/sessions"
  # the input's fields used here, in one read, split on the unit separator (a tab would merge empty fields): id, model, directory, cost (JSON), whether rate_limits is an object
  IFS=$'\037' read -r sid now_model cwd cost has_rl < <(printf '%s' "$input" | jq -r '[
      (.session_id? | strings // ""), (.model.id? | strings // ""), (.workspace.current_dir? // .cwd? | strings // ""),
      (.cost.total_cost_usd? // null | tojson), (.rate_limits? | type == "object")
    ] | map(tostring | gsub("[\u001f\n]"; " ")) | join("\u001f")' 2>/dev/null)
  case "$sid" in ''|*[!A-Za-z0-9_-]*) return 0 ;; esac
  mkdir -p "$dir" "$base/ledger" 2>/dev/null || return 0
  # the previous snapshot, read once: its model and the cost at the last usage update (JSON)
  IFS=$'\037' read -r old_model old_rl < <(jq -r '[(.model | strings // ""), (if has("rl_cost") then .rl_cost else "none" end | tojson)]
      | map(gsub("[\u001f\n]"; " ")) | join("\u001f")' "$dir/$sid.json" 2>/dev/null)
  # A model change inside one session discards the prompt cache and may be an unintended fallback (ADR 0005).
  # Detect it here from the documented status-line input and record it in the event ledger.
  if [ -n "$now_model" ] && [ -n "$old_model" ] && [ "$now_model" != "$old_model" ]; then
    # the directory is left out when the input has none, so it never blanks the lane name
    jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg sid "$sid" --arg from "$old_model" --arg to "$now_model" \
       --arg cwd "$cwd" \
       '{ts:$ts, ev:"PostModelSwitch", sid:$sid, from_model:$from, to_model:$to, via:"statusline"}
        + (if $cwd == "" then {} else {cwd:$cwd} end)' \
       >> "$base/ledger/$(date -u +%Y-%m-%d).jsonl" 2>/dev/null
  fi
  # usage windows: prints the new file's content, or nothing when the reading stays as it is
  ufile="$base/rate-limits/$sid.json"
  usage=$(printf '%s' "$input" | jq -c --arg old "$(head -c 4096 "$ufile" 2>/dev/null)" --arg old_rl "${old_rl:-\"none\"}" '
      (now | floor) as $now
      | def win: if type == "object" and (.used_percentage | type) == "number"
                    and .used_percentage >= 0 and .used_percentage <= 100
                    and (.resets_at | type) == "number" and .resets_at > $now and .resets_at <= $now + 8 * 86400
                 then {used_percentage, resets_at} else empty end;
      (if type == "object" and (.rate_limits | type) == "object" then .rate_limits else {} end) as $r
      | ([("five_hour", "seven_day") as $k | $r[$k] | win | {key: $k, value: .}] | from_entries) as $w
      | ((try ($old | fromjson) catch null) | if type == "object" and (.rate_limits | type) == "object"
                                             then .rate_limits else {} end) as $o
      | (.cost.total_cost_usd? // null) as $cost
      | (try ($old_rl | fromjson) catch "none") as $was
      | if ($w | length) == 0 then empty
        elif ($w | to_entries | any(. as $e | $o[$e.key] != $e.value)) or $cost != $was
          then {rate_limits: $w, t: $now}
        else empty end' 2>/dev/null)
  if [ -n "$usage" ] && mkdir -p "$base/rate-limits" 2>/dev/null; then
    [ -e "$ufile" ] || find "$base/rate-limits" -maxdepth 1 -type f -name '*.json' -mtime +8 -delete 2>/dev/null
    if { printf '%s\n' "$usage" > "$ufile.tmp.$$" && mv -f "$ufile.tmp.$$" "$ufile"; } 2>/dev/null; then
      old_rl="$cost"
    else
      rm -f "$ufile.tmp.$$" 2>/dev/null
    fi
  elif [ "$has_rl" = true ] && [ -f "$ufile" ]; then
    # the reading is up to date for this cost
    old_rl="$cost"
  fi
  printf '%s' "$input" | jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg rl "${old_rl:-\"none\"}" '{
      ts: $ts, sid: .session_id, cwd: .cwd,
      model: (.model.id? // null), effort: (.effort.level? // null),
      cost_usd: (.cost.total_cost_usd? // null),
      ctx_pct: (.context_window.used_percentage? // null),
      rl_cost: (try ($rl | fromjson) catch "none")
    }' > "$dir/$sid.json.tmp" 2>/dev/null && mv "$dir/$sid.json.tmp" "$dir/$sid.json"
}

canon_line() {
  printf '%s' "$input" | jq -r '[
      (.model.display_name? // .model.id? // "model ?"),
      (.effort.level? // empty),
      ("ctx " + ((.context_window.used_percentage? // 0) | floor | tostring) + "%"),
      ("$" + ((((.cost.total_cost_usd? // 0) * 100) | round) / 100 | tostring)),
      ((.workspace.current_dir? // .cwd? // "") | split("/") | last)
    ] | map(select(. != null and . != "")) | join(" · ")' 2>/dev/null || echo "status line: could not parse input"
}

# hud_command: prints the renderer's path (and node's before it for the HUD), or nothing when none is installed
hud_command() {
  local cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}" plugins dir="" node mp v best
  if [ -n "${CANON_STATUSLINE_RENDERER:-}" ]; then printf '%s\n' "$CANON_STATUSLINE_RENDERER"; return 0; fi
  plugins="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-$cfg/plugins}"
  # only while the user settings enable it (an uninstalled plugin's files stay in the cache for 14 days), and
  # that marketplace's newest version by number, run through its own hud.mjs: the plugin's launcher would run
  # the newest copy in any marketplace's cache instead
  while IFS= read -r mp; do
    case "$mp" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
    best=$(find "$plugins/cache/$mp/claude-hud" -mindepth 2 -maxdepth 2 -name hud.mjs 2>/dev/null \
             | sed -n 's#.*/claude-hud/\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)/hud\.mjs$#\1#p' \
             | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)
    [ -z "$best" ] || dir="$plugins/cache/$mp/claude-hud/$best"
  done < <(jq -r '.enabledPlugins // {} | objects | to_entries[] | select(.value == true and (.key | startswith("claude-hud@")))
                   | .key | ltrimstr("claude-hud@")' "$cfg/settings.json" 2>/dev/null)
  [ -n "$dir" ] || return 0
  node=$(command -v node 2>/dev/null) || node=$(mise which node 2>/dev/null) || node=""
  v="$dir/hud.mjs"
  printf '%s\n%s\n' "${node:-node-not-found}" "$v"
}

# scrub: terminal hyperlinks, e-mail addresses, and the repository owner and the home folder's user name as
# whole words (not inside another word), out of what is shown
scrub() {
  # without perl nothing can be scrubbed, so nothing is shown but this
  command -v perl >/dev/null 2>&1 || { cat >/dev/null; echo "status line: perl not found"; return 0; }
  OWNER="$owner" HUSER="${HOME##*/}" perl -pe '
    s/\e\]8;[^\a\e]*(?:\a|\e\\)//g;
    s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g;
    for my $w ($ENV{OWNER}, $ENV{HUSER}) { next unless defined $w && length($w) >= 3; s/(?<![A-Za-z0-9_.-])\Q$w\E(?![A-Za-z0-9_])/~/g; }
  ' 2>/dev/null || echo "status line: could not scrub the line"
}

canon_writes
# the repository owner, to scrub, read apart from the settings so that nothing about them can stop the scrub
owner=$(printf '%s' "$input" | jq -r '.workspace.repo.owner? | strings // "" | gsub("\n"; " ")' 2>/dev/null)
# the HUD's input: the same with model.display_name = model.id and the session's compaction window as the context
# window's size (above). The model's entry is the one under its canonical name, which Claude Code also matches to
# the model's [1m], date-suffixed and provider IDs (settings-reference.md, modelSettings); "auto" there means the
# window tuned for the model, which this does not know, so the input is left as it came.
hud_input="" user_settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[ -r "$user_settings" ] || user_settings=/dev/null
hud_input=$(printf '%s' "$input" | jq -c --arg envw "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" --rawfile s "$user_settings" '
    def usable: if type == "number" and . > 0 then ([[., 100000] | max, 1000000] | min | floor) else null end;
    def envwin: ($envw | capture("^(?<n>[0-9]+)(?<u>k|M)?$")?
                 | (.n | tonumber) * ({"k": 1000, "M": 1000000}[.u // ""] // 1) | usable) // null;
    def canonical: sub("\\[[^]]*\\]$"; "") | sub("^([a-z]+\\.)?anthropic\\."; "") | sub("-v[0-9]+:[0-9]+$"; "")
                   | sub("[-@][0-9]{8}$"; "");
    ((try ($s | fromjson) catch {}) | if type == "object" then . else {} end) as $set
    | if type == "object" and (.model | type) == "object" and (.model.id | type) == "string"
      then .model.display_name = .model.id else . end
    | (.model.id? | strings // "") as $m
    | (($set.modelSettings? | objects) // {}) as $ms
    | ((($ms[$m] // $ms[$m | canonical]) | objects | .autoCompactWindow) // null) as $per
    | (if envwin != null then envwin
       elif $per == "auto" then null
       else ($per | usable) // ($set.autoCompactWindow | usable) end) as $w
    | if $w != null and (.context_window | type) == "object" then
        .context_window |= ((.context_window_size | if type == "number" and . > 0 then [., $w] | min else $w end) as $z
                           | .context_window_size = $z | del(.used_percentage, .remaining_percentage))
      else . end' 2>/dev/null)
[ -n "$hud_input" ] || hud_input="$input"
cmd=()
while IFS= read -r part; do cmd+=("$part"); done < <(hud_command)
if [ "${#cmd[@]}" -eq 0 ]; then
  canon_line | scrub
# the HUD reports its own failures (an internal error, a Node too old) as a line that sends the reader to its
# /hud skill and exits 0; that counts as a failure here, since canon does not run that skill's install
elif [ -x "${cmd[0]}" ] && shown=$(printf '%s' "$hud_input" | "${cmd[@]}" 2>/dev/null) && [ -n "$shown" ] \
     && case "$shown" in *"/hud"*) false ;; *) true ;; esac; then
  printf '%s\n' "$shown" | scrub
else
  printf '%s (hud failed)\n' "$(canon_line)" | scrub
fi
exit 0
