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
# as the HUD's own launcher reads it), run with node. That is PATH's node, unless PATH has none or its node is a
# version manager's shim (a node in a folder named shims), which looks the version up on every run (mise's cost
# about 40 ms of CPU a render, measured 2026-11-13): then it is the Node `mise which node` gives for the home
# folder (mise -C), looked up at most once an hour and kept in canon/hud-node with the lookup's time (none found is
# kept too, and then the shim runs), and looked up again at once when the kept Node is no longer executable. That is
# the HUD's own practice, whose installer puts one absolute Node in the status line command for every project
# (install.mjs, process.execPath): a project's own Node pin, which the shim follows, is not followed, so a project
# pinning a Node older than the HUD needs (22.18 at v0.15.0, package.json engines) does not stop it.
# It reads canon's claude-hud.json beside settings.json.
# When canon/fleet-rate.sh is installed beside this, the HUD is given it as --extra-cmd, with the opt-in
# CLAUDE_HUD_ALLOW_EXTRA_CMD=1 its README asks for, followed by the input's cost.total_lines_added and
# cost.total_lines_removed when both are whole numbers, and shows its label on the first line: the session's
# lines changed and the fleet's output speed.
# It gets the same input with model.display_name set to model.id, so the model actually in use is shown, and,
# when a compaction window applies, context_window.context_window_size set to it and used_percentage removed, so
# the HUD measures its bar against the point where this session compacts (its config sets no autoCompactWindow
# of its own, which would be one window for every model). The window, per
# https://code.claude.com/docs/en/model-config.md ("Set the auto-compact window"): CLAUDE_CODE_AUTO_COMPACT_WINDOW
# when usable (a token count, or with k or M in either case, which the docs do not settle (UNVERIFIED), held to
# 100K to 1M), else the user settings.json's
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
# Every process started is a large part of a render's own cost, so the input is read by four jq runs in all (its
# fields; the previous snapshot and reading; the new snapshot; the time, the HUD's input and the enabled
# marketplaces), the clock is jq's, and the HUD's version is chosen by the shell.
input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  echo "status line: jq not found"
  exit 0
fi
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
base="$cfg/canon"

# The input's fields used here, in one read, split on the unit separator (a tab would merge empty fields): id, model,
# directory, cost (JSON), whether rate_limits is an object, the repository owner to scrub (read apart from the
# settings, so that nothing about them can stop the scrub), and the session's lines added and removed (whole numbers)
sid="" now_model="" cwd="" cost="" has_rl="" owner="" added="" removed=""
IFS=$'\037' read -r sid now_model cwd cost has_rl owner added removed < <(printf '%s' "$input" | jq -r '
    def str(f): [f?] | map(strings) | first // "";
    def count(f): [f?] | map(numbers | select(. >= 0 and . < 1e9 and . == floor) | tostring) | first // "";
    (if type == "object" then . else {} end)
    | [ str(.session_id), str(.model.id), (str(.workspace.current_dir) | if . == "" then null else . end) // str(.cwd),
        (.cost.total_cost_usd? // null | tojson), ((.rate_limits | type) == "object"), str(.workspace.repo.owner),
        count(.cost.total_lines_added), count(.cost.total_lines_removed)
      ] | map(tostring | gsub("[\u001f\n]"; " ")) | join("\u001f")' 2>/dev/null)

canon_writes() {
  local dir="$base/sessions" ufile snap old old_model="" old_rl="" usage=""
  case "$sid" in ''|*[!A-Za-z0-9_-]*) return 0 ;; esac
  { [ -d "$dir" ] && [ -d "$base/ledger" ]; } || mkdir -p "$dir" "$base/ledger" 2>/dev/null || return 0
  ufile="$base/rate-limits/$sid.json"
  snap="$dir/$sid.json"; { [ -f "$snap" ] && [ -r "$snap" ]; } || snap=/dev/null
  old="$ufile"; { [ -f "$old" ] && [ -r "$old" ]; } || old=/dev/null
  # One read of the previous snapshot, the previous reading and the input: the model before this render, the cost
  # when the reading last came up to date (JSON), and the new usage reading, or nothing when it stays as it is.
  IFS=$'\037' read -r old_model old_rl usage < <(printf '%s' "$input" | jq -r --rawfile snap "$snap" --rawfile old "$old" '
      (now | floor) as $now
      | def win: if type == "object" and (.used_percentage | type) == "number"
                    and .used_percentage >= 0 and .used_percentage <= 100
                    and (.resets_at | type) == "number" and .resets_at > $now and .resets_at <= $now + 8 * 86400
                 then {used_percentage, resets_at} else empty end;
      ((try ($snap | fromjson) catch null) | if type == "object" then . else {} end) as $prev
      | (if $prev | has("rl_cost") then $prev.rl_cost else "none" end) as $was
      | (if type == "object" and (.rate_limits | type) == "object" then .rate_limits else {} end) as $r
      | ([("five_hour", "seven_day") as $k | $r[$k] | win | {key: $k, value: .}] | from_entries) as $w
      | ((try ($old | fromjson) catch null) | if type == "object" and (.rate_limits | type) == "object"
                                             then .rate_limits else {} end) as $o
      | (.cost.total_cost_usd? // null) as $cost
      | [ ($prev.model | strings // ""), ($was | tojson),
          (if ($w | length) == 0 then ""
           elif ($w | to_entries | any(. as $e | $o[$e.key] != $e.value)) or $cost != $was
             then {rate_limits: $w, t: $now} | tojson
           else "" end)
        ] | map(gsub("[\u001f\n]"; " ")) | join("\u001f")' 2>/dev/null)
  # A model change inside one session discards the prompt cache and may be an unintended fallback (ADR 0005).
  # Detect it here from the documented status-line input and record it in the event ledger.
  if [ -n "$now_model" ] && [ -n "$old_model" ] && [ "$now_model" != "$old_model" ]; then
    # the directory is left out when the input has none, so it never blanks the lane name
    jq -cn --arg sid "$sid" --arg from "$old_model" --arg to "$now_model" --arg cwd "$cwd" \
       '{ts:(now | floor | todate), ev:"PostModelSwitch", sid:$sid, from_model:$from, to_model:$to, via:"statusline"}
        + (if $cwd == "" then {} else {cwd:$cwd} end)' \
       >> "$base/ledger/$(date -u +%Y-%m-%d).jsonl" 2>/dev/null
  fi
  if [ -n "$usage" ] && { [ -d "$base/rate-limits" ] || mkdir -p "$base/rate-limits" 2>/dev/null; }; then
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
  printf '%s' "$input" | jq -c --arg rl "${old_rl:-\"none\"}" '{
      ts: (now | floor | todate), sid: .session_id, cwd: .cwd,
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

# hud_version_dir: in the enabled marketplaces, the newest claude-hud version folder by number (x.y.z, each part 1
# to 9 digits) that holds a hud.mjs, the last marketplace listed with one winning, or nothing. It is run through its
# own hud.mjs: the plugin's launcher would run the newest copy in any marketplace's cache instead.
hud_version_dir() {
  local plugins="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-$cfg/plugins}" mp f v x y z key best bkey
  hud=""
  for mp in "${mps[@]}"; do
    case "$mp" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
    best="" bkey=""
    for f in "$plugins/cache/$mp/claude-hud/"*/hud.mjs; do
      [ -f "$f" ] || continue
      v=${f%/hud.mjs}; v=${v##*/}
      x=${v%%.*}; y=${v#*.}; z=${y#*.}; y=${y%%.*}
      [ "$x.$y.$z" = "$v" ] || continue
      case "$x" in ''|*[!0-9]*|??????????*) continue ;; esac
      case "$y" in ''|*[!0-9]*|??????????*) continue ;; esac
      case "$z" in ''|*[!0-9]*|??????????*) continue ;; esac
      printf -v key '%09d%09d%09d' "$((10#$x))" "$((10#$y))" "$((10#$z))"
      if [ -z "$best" ] || [[ "$key" > "$bkey" ]]; then best=${f%/hud.mjs}; bkey=$key; fi
    done
    [ -z "$best" ] || hud=$best
  done
}

# hud_node: the Node the HUD runs on (above): PATH's node when it is not a shim; else the Node mise gives for the
# home folder, from canon/hud-node (its path, empty for none, then the lookup's time) when that lookup is less than
# an hour old by the clock and its Node still executable, else looked up and kept anew; PATH's shim when mise gives
# none; nothing when there is no node at all
hud_node() {
  local rest="$PATH:" d p="" t="" cache="$base/hud-node"
  node=""
  while [ -n "$rest" ]; do
    d=${rest%%:*}; rest=${rest#*:}
    if [ -n "$d" ] && [ -f "$d/node" ] && [ -x "$d/node" ]; then node="$d/node"; break; fi
  done
  case "$node" in ''|*/shims/node) ;; *) return 0 ;; esac
  case "$clock" in ''|*[!0-9]*|????????????*) clock="" ;; esac
  if [ -n "$clock" ] && [ -f "$cache" ] && [ -r "$cache" ]; then
    { IFS= read -r p; IFS= read -r t; } < "$cache"
    case "$t" in ''|*[!0-9]*|????????????*) t="" ;; esac
    if [ -n "$t" ] && [ "$t" -le "$clock" ] && [ $((clock - t)) -lt 3600 ]; then
      [ -n "$p" ] || return 0
      case "$p" in /*/node) case "$p" in */shims/node) ;; *) if [ -f "$p" ] && [ -x "$p" ]; then node=$p; return 0; fi ;; esac ;; esac
    fi
  fi
  p=$(mise -C "$HOME" which node 2>/dev/null) || p=""
  case "$p" in */shims/node) p="" ;; /*/node) ;; *) p="" ;; esac
  if [ -n "$p" ] && [ -f "$p" ] && [ -x "$p" ]; then node=$p; else p=""; fi
  [ -n "$clock" ] || return 0
  { printf '%s\n%s\n' "$p" "$clock" > "$cache.$$" && mv -f "$cache.$$" "$cache"; } 2>/dev/null || rm -f "$cache.$$" 2>/dev/null
}

# scrub: terminal hyperlinks, e-mail addresses, and the repository owner and the home folder's user name as
# whole words (not inside another word), out of what is shown. A word starts after a line's start, a character
# that cannot be in a name, or a colour code (ESC [ ... m), which ends in a letter: the HUD colours the branch.
scrub() {
  # without perl nothing can be scrubbed, so nothing is shown but this
  command -v perl >/dev/null 2>&1 || { cat >/dev/null; echo "status line: perl not found"; return 0; }
  OWNER="$owner" HUSER="${HOME##*/}" perl -pe '
    s/\e\]8;[^\a\e]*(?:\a|\e\\)//g;
    s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g;
    for my $w ($ENV{OWNER}, $ENV{HUSER}) { next unless defined $w && length($w) >= 3; s/(^|[^A-Za-z0-9_.-]|\e\[[0-9;]*m)\Q$w\E(?![A-Za-z0-9_])/$1~/g; }
  ' 2>/dev/null || echo "status line: could not scrub the line"
}

canon_writes
# The time, the HUD's input and the enabled marketplaces, in one read of the input and the user settings. The first
# line is the time (epoch seconds); the second the input with model.display_name = model.id and the session's
# compaction window as the context window's size (above), or nothing when the input cannot be read so, and the HUD
# then gets the input as it came; each line after them names a marketplace whose claude-hud entry the settings'
# enabledPlugins sets to true (an uninstalled plugin's files stay in the cache for 14 days, so the cache alone is not
# read). The input is read as text, so that none, however malformed, stops the marketplaces from being read. The
# model's entry is the one under its canonical name, which Claude Code also matches to the model's [1m],
# date-suffixed and provider IDs (settings-reference.md, modelSettings); "auto" there means the window tuned for the
# model, which this does not know, so the input is left as it came.
hud_input="" clock="" mps=() user_settings="$cfg/settings.json"
{ [ -f "$user_settings" ] && [ -r "$user_settings" ]; } || user_settings=/dev/null
{
  IFS= read -r clock
  IFS= read -r hud_input
  while IFS= read -r mp; do mps+=("$mp"); done
} < <(printf '%s' "$input" | jq -Rrs --arg envw "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" --rawfile s "$user_settings" '
    def usable: if type == "number" and . > 0 then ([[., 100000] | max, 1000000] | min | floor) else null end;
    def envwin: ($envw | capture("^(?<n>[0-9]+)(?<u>[kKmM])?$")?
                 | (.n | tonumber) * ({"k": 1000, "m": 1000000}[.u // "" | ascii_downcase] // 1) | usable) // null;
    def canonical: sub("^.*/"; "") | sub("\\[[^]]*\\]$"; "") | sub("^([a-z]+\\.)?anthropic\\."; "") | sub("-v[0-9]+:[0-9]+$"; "")
                   | sub("[-@][0-9]{8}$"; "");
    ((try ($s | fromjson) catch {}) | if type == "object" then . else {} end) as $set
    | (now | floor | tostring),
      (try (fromjson
        | if type == "object" and (.model | type) == "object" and (.model.id | type) == "string"
          then .model.display_name = .model.id else . end
        | (.model.id? | strings // "") as $m
        | (($set.modelSettings? | objects) // {}) as $ms
        | (([$ms[$m], $ms[$m | canonical]] | map(objects | .autoCompactWindow | select(. != null)) | first) // null) as $per
        | envwin as $e
        | (if $e != null then $e
           elif $per == "auto" then null
           else ($per | usable) // ($set.autoCompactWindow | usable) end) as $w
        | if $w != null and (.context_window | type) == "object" then
            .context_window |= ((.context_window_size | if type == "number" and . > 0 then [., $w] | min else $w end) as $z
                               | .context_window_size = $z | del(.used_percentage, .remaining_percentage))
          else . end
        | tojson) catch ""),
      ( ($set.enabledPlugins // {}) | objects | to_entries[]
        | select(.value == true and (.key | startswith("claude-hud@"))) | .key | ltrimstr("claude-hud@")
        | select(test("^[A-Za-z0-9._-]+$")) )' 2>/dev/null)
[ -n "$hud_input" ] || hud_input="$input"

cmd=()
if [ -n "${CANON_STATUSLINE_RENDERER:-}" ]; then
  cmd=("$CANON_STATUSLINE_RENDERER")
else
  hud_version_dir
  if [ -n "$hud" ]; then
    hud_node
    cmd=("${node:-node-not-found}" "$hud/hud.mjs")
    # the first line's label (fleet-rate.sh), through the HUD's --extra-cmd, which runs its argument in a shell: the
    # script's quoted path, then the session's lines added and removed, digits only
    if [ -x "$base/fleet-rate.sh" ]; then
      printf -v extra '%q' "$base/fleet-rate.sh"
      case "$added$removed" in *[!0-9]*) ;; *) [ -z "$added" ] || [ -z "$removed" ] || extra="$extra $added $removed" ;; esac
      cmd+=(--extra-cmd "$extra")
    fi
  fi
fi
if [ "${#cmd[@]}" -eq 0 ]; then
  canon_line | scrub
# the HUD reports its own failures (an internal error, a Node too old) as a line that sends the reader to its
# /hud skill and exits 0; that counts as a failure here, since canon does not run that skill's install
elif [ -x "${cmd[0]}" ] && shown=$(printf '%s' "$hud_input" | CLAUDE_HUD_ALLOW_EXTRA_CMD=1 "${cmd[@]}" 2>/dev/null) && [ -n "$shown" ] \
     && case "$shown" in *"/hud"*) false ;; *) true ;; esac; then
  printf '%s\n' "$shown" | scrub
else
  printf '%s (hud failed)\n' "$(canon_line)" | scrub
fi
exit 0
