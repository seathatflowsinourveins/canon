#!/bin/bash
# Maintenance agent. Run by the launchd user agent local.canon.maintenance every 30 minutes while the Mac is
# awake (launchd.plist(5), StartInterval); installed, reported and removed by scripts/automation (ADR 0065,
# dated addition of 2026-11-11). It keeps this machine's layer on main, so nobody has to remember an installer:
#   (a) brings the main checkout to origin/main by fast-forward only (`git merge --ff-only`, git-merge(1)),
#       and only when the checkout is on main with no tracked change; otherwise it says why and stops there.
#       It never resets, stashes or switches branches.
#   (b) when the checkout is at origin/main, asks `scripts/automation differs` what the installers manage that
#       differs from the tree, and runs the matching installer: `scripts/bootstrap --user-config` for the
#       settings and the runtime defaults, `scripts/automation install` for the agents and their scripts.
#   (c) once a day, the runtimes' own update commands, `claude update` and `agy update` (ADR 0065).
#       Codex, Muse Code, DeepSeek Harness and dsh-tui are not updated here: none has its updater turned on
#       (ADR 0065 rule 4), and Codex, DeepSeek Harness and dsh-tui are exact mise pins, which (d) keeps.
#   (d) when the checkout is at origin/main, the exact pins main's registry records for what mise installs
#       outside this repository: mise itself (`mise self-update --yes --no-plugins <version>`) and the tools
#       of mise's global config listed in PINS (`mise use --global --yes <tool>@<version>`, mise's documented
#       way to install a version and record it there). A tool already at its pin is left alone.
# Nothing here reads a secret, installs a tool beyond (d)'s pins (`scripts/bootstrap --install` stays a
# deliberate step) or changes the registry or the pins.
#
# Usage: maintenance.sh <checkout> <mise>
#
# The paper locks are the files mise.toml's paper tasks lock (ADR 0069): canon-paper-windows.lock, account B's,
# which a window holds; canon-paper-test.lock, account A's, which a paper test holds; and
# canon-alpaca-stream-tests.lock, the market-data stream of account A's pair, which a streaming paper test and
# paper:data's streaming modules take (account B's stream lock is taken only by a window, under B's lock). A
# round takes them without waiting (lockf(1) -t 0). A paper test's lock, or A's stream lock, ends the round
# there, saying which, whether or not a window runs, since those runs may run from the checkout.
# A window runs from the paper runner's own clone (ADR 0050), so its lock defers only what that window
# depends on: the paper section of `scripts/automation install` (the runner, its plist and the clone the
# runner makes anew at each firing), which the round's install then skips (`install --defer-paper`). The
# rest of the round runs at once: the fast-forward, the settings step, the other agents' install and the
# updates; a pin that differs waits too, the window running with mise and its global config. The deferred part waits for the session boundary, the moment the window's lock goes
# free: the session map ends each run at its session's end by Alpaca's clock and calendar, which this agent
# cannot read itself, since it reads no secret. A round that defers starts one detached waiter (its own
# process group, as the reinstall below), which holds <config>/canon/watch/maintenance-boundary.lock, so a
# second is never started beside it, waits on the windows' lock for at most BOUNDARY_SECS (waitlock, below),
# and once it holds it, and the clone's own lock has gone free too, runs a round with it held
# (CANON_MAINTENANCE_AT_BOUNDARY=1), which installs the whole, paper section included, and moves the pins;
# or it records that it timed out. It writes maintenance-boundary.json, which also says when the boundary's
# round ended before its install.
# Without a window, the round holds all three it takes for (a) and the windows' lock alone for the install, which
# reloads the paper agent and touches nothing else they use. The daily updates, the compare, the settings
# step and the pins touch none of them and run with them let go. A scheduled window waits 900 s for its lock
# (mise.toml). The round holds its own lock, <config>/canon/maintenance.lock, for the whole round and the
# reinstall it starts, so rounds never overlap; the round a waiter starts waits for it at most
# BOUNDARY_ROUND_SECS. Limits: a window started by hand from the main checkout is not told apart from the
# scheduled one, so the checkout may move under it; and a run that started before the stream lock was split
# per account (ADR 0069, 2026-11-11) holds the old canon-alpaca-stream.lock, which no round checks.
# An install is not repeated within a day for the same differences at the same origin/main once an install
# for them finished (each install restarts the paper agent's interval, ADR 0050); the row says FAILED until
# then. One that timed out or never reported is. `scripts/automation install` reloads launchd agents, this
# one included when its plist changed, so it runs last and detached: a background job of its own process
# group (bash `set -m`), which launchd's kill of the job's process group (launchd.plist(5),
# AbandonProcessGroup) does not reach, while this process exits at once. The locks it holds pass to it; the
# installers and update commands run with every lock closed, so nothing they leave running holds one. Every step is bounded by timeout(1) (macOS's /usr/bin/timeout).
# Each round rewrites <config>/canon/watch/maintenance-status.json, which `scripts/automation status` reads;
# the daily updates write maintenance-updates.json beside it. The log is launchd's, watch/maintenance.log.
# CLAUDE_CONFIG_DIR replaces ~/.claude (the plist sets it to the folder install used, so the installers write
# there). CANON_GETCONF, CANON_MAINTENANCE_FETCH_SECS, CANON_MAINTENANCE_STEP_SECS,
# CANON_MAINTENANCE_UPDATE_SECS, CANON_MAINTENANCE_BOUNDARY_SECS and CANON_MAINTENANCE_BOUNDARY_ROUND_SECS
# replace getconf and the bounds (tests only).
# mise's folder goes first on PATH, so the installers find the mise the plists name (scripts/automation
# find_mise); exported, as under an empty environment a child gets no PATH (lanes-watch.sh).
[ $# -ge 2 ] || { echo "usage: maintenance.sh <checkout> <mise>" >&2; exit 2; }
root=$1 mise=$2
export PATH="${mise%/*}:/usr/bin:/bin:/usr/sbin:/sbin"
export GIT_TERMINAL_PROMPT=0          # never wait at a credential prompt: the fetch is of a public repository
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
export CLAUDE_CONFIG_DIR="$cfg"
base="$cfg/canon"; w="$base/watch"
STATUS="$w/maintenance-status.json"; UPDATES="$w/maintenance-updates.json"
FETCH_SECS=${CANON_MAINTENANCE_FETCH_SECS:-120}    # the fetch of main
GIT_SECS=30                                        # each local git command
STEP_SECS=${CANON_MAINTENANCE_STEP_SECS:-900}      # `differs`, each installer (scripts/automation MAINT_STEP)
UPDATE_SECS=${CANON_MAINTENANCE_UPDATE_SECS:-900}  # each update command, a download included
# the waiter's bound: the longest session the session map runs in, the overnight one (20:00 to 04:00 New York,
# session_window.OVERNIGHT_STARTS), and an hour more; past it a deferral has outlived a whole session
# (scripts/automation MAINT_BOUNDARY)
BOUNDARY_SECS=${CANON_MAINTENANCE_BOUNDARY_SECS:-32400}
BOUNDARY_ROUND_SECS=${CANON_MAINTENANCE_BOUNDARY_ROUND_SECS:-300}   # the round it starts waits this for its lock
BOUNDARY="$w/maintenance-boundary.json"
# PINS: <tool as mise's global config names it>|<registry id>|<the registry tag's prefix before the version>.
# The tools mise installs machine-level whose registry entry records an exact version (canon/upstreams.json);
# not agy, which updates itself (ADR 0065), nor muse, which follows latest.
PINS="codex|codex-cli|rust-v pnpm|pnpm|v npm:@deepseek-ai/dsh|deepseek-harness|dsh-v npm:@deepseek-harness-tui/dsh-tui|dsh-tui|v"
AT_BOUNDARY=${CANON_MAINTENANCE_AT_BOUNDARY:-0}; unset CANON_MAINTENANCE_AT_BOUNDARY
DAY=86400
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s maintenance: %s\n' "$(now)" "$*"; }
mkdir -p "$w" || { log "cannot create $w"; exit 1; }
# launchd only appends to the log; it starts again past 2 MB
lf="$w/maintenance.log"
[ ! -f "$lf" ] || [ "$(wc -c < "$lf" 2>/dev/null || echo 0)" -lt 2097152 ] || : > "$lf"
command -v jq >/dev/null 2>&1 || { log "jq not found; nothing was run"; exit 1; }
[ -x /usr/bin/timeout ] && [ -x /usr/bin/lockf ] || { log "no /usr/bin/timeout or /usr/bin/lockf; nothing was run"; exit 1; }

# put <file> <json>: replace it whole, through a temporary file of this writer's own (mktemp(1))
put() {
  local t
  t=$(mktemp "$w/.${1##*/}.XXXXXX") || return 1
  if printf '%s\n' "$2" > "$t" && mv -f "$t" "$1"; then return 0; fi
  rm -f "$t"; return 1
}
FOUND='[]' INSTALLED='[]' FAILED='[]' FF="" OUTCOME=ok NOTE=""
# TRIED: the differences the last automation install was started for, {at, at_epoch, found, commit}, with
# its result once it reports; "keep" carries the earlier record's, null clears it (a compare that found no
# agent or script differing)
TRIED='"keep"'
# DEFER: what this round does to the record of a deferred paper section: "set" (deferred now; the earliest
# deferral's time is kept), "clear" (compared, and nothing of it is left waiting), "keep" (not known)
DEFER=keep PINRES='[]'
add() { local v; v=$(jq -c --arg x "$2" '. + [$x]' <<<"${!1}") && printf -v "$1" '%s' "$v"; }
# record [install json]: this round's record; the last round that found something is kept as last_change
record() {
  local prev='{}'
  [ ! -f "$STATUS" ] || prev=$(jq -c 'objects' "$STATUS" 2>/dev/null) || prev='{}'
  [ -n "$prev" ] || prev='{}'
  put "$STATUS" "$(jq -cn --argjson p "$prev" --arg at "$(now)" --arg o "$OUTCOME" --arg ff "$FF" --arg n "$NOTE" \
      --argjson f "$FOUND" --argjson i "$INSTALLED" --argjson x "$FAILED" --arg r "$root" --argjson ins "${1:-null}" \
      --argjson tr "$TRIED" --arg d "$DEFER" --argjson pins "$PINRES" --argjson b "$([ "$AT_BOUNDARY" = 1 ] && echo true || echo false)" '
      {at: $at, at_epoch: now, checkout: $r, outcome: $o, ff: $ff, found: $f, installed: $i, failed: $x,
       note: $n, install: $ins, tried: (if $tr == "keep" then ($p.tried // null) else $tr end), pins: $pins,
       at_boundary: $b,
       deferred: (if $d == "set" then ($p.deferred // {since: $at, since_epoch: now, what: "the paper checkout"})
                  elif $d == "clear" then null else ($p.deferred // null) end)}
      | .last_change = (if ($f | length) > 0 then {at: $at, found: $f, installed: $i, failed: $x} else ($p.last_change // null) end)')" \
    || log "could not write $STATUS"
}
# boundary_put <state> [why]: the waiter's record, maintenance-boundary.json; `since_epoch` is when the
# waiter started, kept from the record it replaces while one waiter runs
boundary_put() {
  local prev='{}'
  [ ! -f "$BOUNDARY" ] || prev=$(jq -c 'objects' "$BOUNDARY" 2>/dev/null) || prev='{}'
  [ -n "$prev" ] || prev='{}'
  put "$BOUNDARY" "$(jq -cn --argjson p "$prev" --arg s "$1" --arg why "${2:-}" --arg at "$(now)" --argjson bound "$BOUNDARY_SECS" '
      {state: $s, at: $at, at_epoch: now, bound: $bound, why: $why,
       since: (if $s == "waiting" then $at else ($p.since // $at) end),
       since_epoch: (if $s == "waiting" then now else ($p.since_epoch // now) end)}')" || log "could not write $BOUNDARY"
}
# waitlock <fd> <seconds>: take the lock on <fd>, trying each second for at most <seconds>; 1 when the
# time ran out, lockf's own status for any error but a held lock (75, EX_TEMPFAIL). lockf(1)'s own -t on
# a descriptor spins a whole core for as long as it waits (measured here), so a wait is -t 0 between
# sleeps, which hold none of the locks. Limit: a run that takes the lock within the second between two
# tries takes it first.
waitlock() {
  local end=$((SECONDS + $2)) rc
  while :; do
    /usr/bin/lockf -t 0 "$1" 2>/dev/null; rc=$?
    [ "$rc" = 75 ] || return "$rc"
    [ "$SECONDS" -lt "$end" ] || return 1
    sleep 1 4>&- 5>&- 7>&- 9>&-
  done
}
# bounded <secs> <command...>: the command under timeout(1), SIGKILL 5 s after SIGTERM, with stdin empty and
# the locks closed (the waiter's too), so nothing it leaves running holds them
bounded() { local s=$1; shift; /usr/bin/timeout -k 5 "$s" "$@" < /dev/null 5>&- 6>&- 7>&- 8>&- 9>&-; }
timed_out() { [ "$1" = 124 ] || [ "$1" = 137 ]; }
g() { bounded "$GIT_SECS" git -C "$root" "$@"; }

# at the boundary, the waiter that started this round holds the windows' lock on descriptor 9 and its own on 5
if [ "$AT_BOUNDARY" = 1 ] && ! { true >&9; } 2>/dev/null; then log "started as the boundary's round with no windows' lock held; nothing was run"; exit 1; fi
# one round at a time, the reinstall it started included; the boundary's round waits for one still running
exec 7>>"$base/maintenance.lock" || { log "cannot open $base/maintenance.lock"; exit 1; }
if [ "$AT_BOUNDARY" = 1 ]; then
  if ! waitlock 7 "$BOUNDARY_ROUND_SECS"; then
    boundary_put failed "an earlier round, or the reinstall it started, still ran after $BOUNDARY_ROUND_SECS s; the next round that finds a window defers again"
    log "at the boundary: an earlier round still ran after $BOUNDARY_ROUND_SECS s; nothing this round"; exit 0
  fi
  log "round starts in $root, at the session boundary: the windows' lock is held for this round"
  # it says whether its round installed what waited: one that ended first (a paper test's lock held, the
  # checkout off main) leaves it to the next round that finds a window, which arms a waiter again
  trap 'if [ "$DEFER" = clear ]; then boundary_put ran "the round at the boundary ran; its record is maintenance-status.json"
        else boundary_put missed "the round at the boundary ended $OUTCOME before its install; the next round that finds a window waits again"; fi' EXIT
elif ! /usr/bin/lockf -t 0 7 2>/dev/null; then log "an earlier round, or the reinstall it started, still runs; nothing this round"; exit 0
else log "round starts in $root"; fi
# the paper locks are in the per-user temporary folder, where mise.toml's paper tasks lock them on a descriptor
tmp=$("${CANON_GETCONF:-/usr/bin/getconf}" DARWIN_USER_TEMP_DIR 2>/dev/null) || tmp=""
case "$tmp" in
  /*/) ;;
  *) OUTCOME=failed FF="not run"; add FAILED "paper lock: the per-user temporary folder is not known (getconf DARWIN_USER_TEMP_DIR)"
     record; log "the per-user temporary folder is not known, so there is no paper lock to take; nothing was run"; exit 1 ;;
esac
# at the boundary the windows' lock stays held for the whole round
paper_free() { exec 6>&- 8>&-; [ "$AT_BOUNDARY" = 1 ] || exec 9>&-; }
# plock <fd> <file>: opens the lock file on the descriptor and takes it without waiting; 1 when another
# holds it. A lock file that cannot be opened ends the round FAILED, naming it.
plock() {
  if ! eval "exec $1>>\"\${tmp}\$2\""; then
    OUTCOME=failed; add FAILED "paper lock: cannot open ${tmp}$2"; record; log "cannot open ${tmp}$2"; exit 1
  fi
  /usr/bin/lockf -t 0 "$1" 2>/dev/null
}
# paper_lock <checkout|install>: WINDOW=1 when a window holds the windows' lock (descriptor 9, which this
# round holds otherwise); for `checkout`, then the paper tests' lock on 8 and account A's stream lock on 6. 0 when the round may go on; 1 when a paper test or a run on the stream holds one, with
# HELD saying which (every caller then ends the round).
paper_lock() {
  paper_free
  WINDOW=0 HELD=""
  if [ "$AT_BOUNDARY" != 1 ] && ! plock 9 canon-paper-windows.lock; then WINDOW=1; exec 9>&-; fi
  [ "$1" = checkout ] || return 0
  if ! plock 8 canon-paper-test.lock; then HELD="canon-paper-test.lock is held: a paper test runs"; return 1; fi
  if ! plock 6 canon-alpaca-stream-tests.lock; then
    HELD="canon-alpaca-stream-tests.lock is held: a run on account A's market-data stream (paper:data or a paper test)"; return 1
  fi
  return 0
}
HELD="" WINDOW=0
if ! paper_lock checkout; then
  OUTCOME=waited FF="not run: the paper lock is held ($HELD), so nothing this round"
  record; log "$FF"; exit 0
fi
[ "$WINDOW" = 0 ] || log "a window runs (canon-paper-windows.lock is held): its runner and clone wait for the session boundary; the rest of the round runs"

# (c) the runtimes' own updates, once a day, first and with the paper lock let go: they touch nothing a
# window uses. Not at the boundary, where the next window waits for this round's windows' lock.
paper_free
last=$(jq -r '.at_epoch // 0 | floor' "$UPDATES" 2>/dev/null) || last=0
[[ "$last" =~ ^[0-9]+$ ]] || last=0
if [ "$AT_BOUNDARY" = 1 ]; then
  log "(c) runtime updates: not at the boundary; the next round runs them when due"
elif [ $(( $(date +%s) - last )) -ge "$DAY" ]; then
  log "(c) runtime updates (Codex, Muse Code, DeepSeek Harness and dsh-tui are not updated here: Codex, DeepSeek Harness and dsh-tui are exact mise pins, which (d) keeps)"
  ures='[]'
  # update <name> <binary>: its update command, bounded; the outcome joins ures
  update() {
    local o rc r
    o=$(bounded "$UPDATE_SECS" "$2" update 2>&1); rc=$?
    printf '%s\n' "$o" | sed 's/^/  /'
    if timed_out "$rc"; then r="FAILED: $1 update did not finish within $UPDATE_SECS s"
    elif [ "$rc" != 0 ]; then r="FAILED: $1 update exited $rc: $(printf '%s' "$o" | tail -n 1)"
    else r="$1: $(printf '%s' "$o" | grep -v '^[[:space:]]*$' | tail -n 1)"; fi
    ures=$(jq -c --arg r "$r" '. + [$r]' <<<"$ures")
  }
  c=""; [ -x "$HOME/.local/bin/claude" ] && c="$HOME/.local/bin/claude"; [ -n "$c" ] || c=$(command -v claude 2>/dev/null) || c=""
  if [ -n "$c" ]; then update claude "$c"; else ures=$(jq -c '. + ["claude: not installed"]' <<<"$ures"); fi
  # agy is installed machine-level through mise (its global config), so mise names it from HOME
  a=$(cd "$HOME" && bounded 30 "$mise" which agy 2>/dev/null) || a=""
  if [ -n "$a" ] && [ -x "$a" ]; then update agy "$a"; else ures=$(jq -c '. + ["agy: not installed"]' <<<"$ures"); fi
  put "$UPDATES" "$(jq -cn --arg at "$(now)" --argjson r "$ures" '{at: $at, at_epoch: now, results: $r}')" || log "could not write $UPDATES"
  while IFS= read -r r; do log "(c) $r"; case "$r" in FAILED:*) add FAILED "${r#FAILED: }" ;; esac; done < <(jq -r '.[]' <<<"$ures")
else
  log "(c) runtime updates ran less than a day ago"
fi

# the paper locks again, without waiting, for (a)
if ! paper_lock checkout; then
  OUTCOME=waited FF="not run: the paper lock was taken during the updates ($HELD), so nothing more this round"
  [ "$FAILED" = '[]' ] || OUTCOME=failed
  record; log "$FF"; exit 0
fi

# (a) the main checkout to origin/main, fast-forward only
at_main=0 want=""
log "(a) bring $root to origin/main"
if ! br=$(g symbolic-ref -q --short HEAD); then
  OUTCOME=skipped FF="skipped: the checkout has a detached HEAD, not main"
elif [ "$br" != main ]; then
  OUTCOME=skipped FF="skipped: the checkout is on $br, not main"
elif ! st=$(g status --porcelain --untracked-files=no 2>&1); then
  OUTCOME=failed FF="FAILED: git status did not answer"; add FAILED "fast-forward: git status"
elif [ -n "$st" ]; then
  OUTCOME=skipped FF="skipped: the checkout has tracked changes"
else
  out=$(bounded "$FETCH_SECS" git -C "$root" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 \
          fetch -q --no-tags origin +refs/heads/main:refs/remotes/origin/main 2>&1); rc=$?
  if timed_out "$rc"; then FF="FAILED: the fetch of main did not finish within $FETCH_SECS s"; add FAILED "fetch (timeout)"
  elif [ "$rc" != 0 ]; then FF="FAILED: could not fetch main: $(printf '%s' "$out" | tr '\n' ' ')"; add FAILED "fetch"; fi
  head=$(g rev-parse -q --verify HEAD) want=$(g rev-parse -q --verify refs/remotes/origin/main)
  if [ -n "$FF" ]; then
    :                                   # origin/main is not known to be main's: neither (a) nor (b) this round
  elif [ -z "$head" ] || [ -z "$want" ]; then
    FF="FAILED: cannot read HEAD or origin/main"; add FAILED "fast-forward: rev-parse"
  elif [ "$head" = "$want" ]; then
    FF="at origin/main ${want:0:12}"; at_main=1
  elif g merge-base --is-ancestor "$head" "$want"; then
    if out=$(g merge --ff-only -q "$want" 2>&1); then FF="fast-forwarded ${head:0:12}..${want:0:12}"; at_main=1
    else FF="FAILED: the fast-forward to ${want:0:12} was refused: $(printf '%s' "$out" | tr '\n' ' ')"; add FAILED "fast-forward"; fi
  elif g merge-base --is-ancestor "$want" "$head"; then
    OUTCOME=skipped FF="skipped: main has commits origin/main lacks (${head:0:12}); left as it is"
  else
    OUTCOME=skipped FF="skipped: main and origin/main have diverged; left as it is"
  fi
fi
log "(a) $FF"

# (b) what differs from main's tree, the settings step first, with the paper lock let go
paper_free
auto=0 compared=0
if [ "$at_main" = 1 ]; then
  log "(b) compare what the installers manage with the tree"
  out=$(bounded "$STEP_SECS" "$root/scripts/automation" differs 2>&1); rc=$?
  [ "$rc" != 0 ] && [ "$rc" != 1 ] || compared=1
  printf '%s\n' "$out" | sed 's/^/  /'
  if timed_out "$rc"; then add FAILED "compare (timeout after $STEP_SECS s)"
  elif [ "$rc" != 0 ] && [ "$rc" != 1 ]; then
    add FAILED "compare: $(printf '%s\n' "$out" | sed -n 's/^unknown: //p' | head -3 | tr '\n' ';' | sed 's/;$//')"
  fi
  while IFS= read -r l; do
    case "$l" in "automation: "*|"user-config: "*) add FOUND "$l" ;; esac
  done <<<"$out"
  grep -q '^automation: ' <<<"$out" && auto=1
  if grep -q '^user-config: ' <<<"$out"; then
    log "(b) scripts/bootstrap --user-config"
    uo=$(bounded "$STEP_SECS" "$root/scripts/bootstrap" --user-config 2>&1); rc=$?
    printf '%s\n' "$uo" | sed 's/^/  /'
    if timed_out "$rc"; then add FAILED "bootstrap --user-config (timeout after $STEP_SECS s)"
    elif [ "$rc" != 0 ]; then add FAILED "bootstrap --user-config (exit $rc)"
    else
      add INSTALLED "bootstrap --user-config"
      bounded "$GIT_SECS" "$root/scripts/automation" mark-user-config >/dev/null 2>&1 || add FAILED "recording the settings step"
      ex=$(printf '%s\n' "$uo" | sed -n 's/^  EXISTS *\([^ ]*\) differs.*/\1/p' | tr '\n' ' ')
      [ -z "$ex" ] || NOTE="left for the owner to merge: ${ex% } (.canon-proposed beside each)"
    fi
  fi
else
  log "(b) skipped: the checkout is not at origin/main, so its tree is not main's"
fi

# (d) the exact pins main's registry records, at origin/main only, with the locks let go. While a window runs
# none moves (its mise and mise's global config are in use): each that differs waits for the session
# boundary, whose round moves it with the windows' lock held (PINWAIT=1 arms the waiter)
# pin_of <registry id> <tag prefix>: the version of the entry's track, tag:<prefix><version>; empty if none
pin_of() {
  jq -r --arg id "$1" '.upstreams[]? | select(.id == $id) | .track // empty' "$root/canon/upstreams.json" 2>/dev/null \
    | sed -n "s/^tag:$2\([0-9][0-9A-Za-z.+-]*\)\$/\1/p" | head -n 1
}
pin_fail() { add PINRES "FAILED: $1"; add FAILED "pin $1"; log "(d) FAILED: $1"; }
mise_version() { bounded 30 "$mise" --version 2>/dev/null | head -n 1 | awk '{ print $1 }'; }
# global_at <tool> <version>: "at" when mise's global config requests that version and it is installed,
# "absent" when the global config names no version of the tool, otherwise what it requests; 1 when mise
# did not answer
global_at() {
  local j
  j=$(cd "$HOME" && bounded 30 "$mise" ls --global --json "$1" 2>/dev/null) || return 1
  jq -r --arg v "$2" '(if type == "object" then [.[][]] else . end) as $a
    | if ($a | map(select(.installed and (.requested_version // .version) == $v)) | length) > 0 then "at"
      elif ($a | length) == 0 then "absent" else ($a | map(.requested_version // .version) | join(", ")) end' <<<"$j" 2>/dev/null
}
PINWAIT=0 PW=0
if [ "$at_main" = 1 ]; then
  log "(d) exact pins: mise and its global tools to main's registry"
  # a window now, read afresh (the round let the locks go for the updates); at the boundary the round holds its lock
  if [ "$AT_BOUNDARY" != 1 ] && ! /usr/bin/lockf -k -t 0 "${tmp}canon-paper-windows.lock" true 2>/dev/null; then
    PW=1; log "(d) a window runs: a pin that differs waits for the session boundary"
  fi
  v=$(pin_of mise v)
  if [ -z "$v" ]; then pin_fail "mise: main's registry gives no tag:v<version> for it"
  elif cur=$(mise_version) && [ "$cur" = "$v" ]; then add PINRES "mise: at $v"
  elif [ "$PW" = 1 ]; then add PINRES "mise: ${cur:-no version} -> $v at the session boundary"; PINWAIT=1
  else
    # The recorder also invokes this binary between batches. Share its loop lock through replacement;
    # the update command gets no copy of descriptor 4, so only this round owns the guard.
    recorder_lock=$(bounded 30 "$root/scripts/automation" recorder-lock-path 2>/dev/null) || recorder_lock=""
    if [[ "$recorder_lock" != /* || "$recorder_lock" == *$'\n'* ]]; then pin_fail "mise: could not read the recorder lock path from automation"
    elif ! exec 4>>"$recorder_lock"; then pin_fail "mise: could not open the recorder lock"
    else
      recorder_lock_rc=0; /usr/bin/lockf -t 0 4 2>/dev/null || recorder_lock_rc=$?
      if [ "$recorder_lock_rc" = 75 ]; then add PINRES "mise: ${cur:-no version} -> $v after the recorder ends"
      elif [ "$recorder_lock_rc" != 0 ]; then pin_fail "mise: could not take the recorder lock (exit $recorder_lock_rc)"
      else
        o=$(cd "$HOME" && bounded "$UPDATE_SECS" "$mise" self-update --yes --no-plugins "$v" 4>&- 2>&1); rc=$?
        printf '%s\n' "$o" | sed 's/^/  /'
        if timed_out "$rc"; then pin_fail "mise: self-update to $v did not finish within $UPDATE_SECS s"
        elif [ "$rc" != 0 ]; then pin_fail "mise: self-update to $v exited $rc: $(printf '%s' "$o" | tail -n 1)"
        elif [ "$(mise_version)" != "$v" ]; then pin_fail "mise: self-update to $v exited 0, but mise reports ${cur:-no version} still"
        else add PINRES "mise: ${cur:-no version} -> $v"; log "(d) mise: ${cur:-no version} -> $v"; fi
      fi
      exec 4>&-
    fi
  fi
  for spec in $PINS; do
    IFS='|' read -r tool id pre <<<"$spec"
    v=$(pin_of "$id" "$pre")
    if [ -z "$v" ]; then pin_fail "$tool: main's registry gives no tag:${pre}<version> for $id"; continue; fi
    if ! cur=$(global_at "$tool" "$v") || [ -z "$cur" ]; then pin_fail "$tool: mise ls --global did not answer"; continue; fi
    case "$cur" in
      at) add PINRES "$tool: at $v" ;;
      absent) add PINRES "$tool: not in mise's global config; left alone" ;;
      *) if [ "$PW" = 1 ]; then add PINRES "$tool: $cur -> $v at the session boundary"; PINWAIT=1; continue; fi
        o=$(cd "$HOME" && bounded "$UPDATE_SECS" "$mise" use --global --yes "$tool@$v" 2>&1); rc=$?
        printf '%s\n' "$o" | sed 's/^/  /'
        if timed_out "$rc"; then pin_fail "$tool: mise use $tool@$v did not finish within $UPDATE_SECS s"
        elif [ "$rc" != 0 ]; then pin_fail "$tool: mise use $tool@$v exited $rc: $(printf '%s' "$o" | tail -n 1)"
        elif [ "$(global_at "$tool" "$v")" != at ]; then pin_fail "$tool: mise use $tool@$v exited 0, but the global config does not have it installed"
        else add PINRES "$tool: $cur -> $v"; log "(d) $tool: $cur -> $v"; fi ;;
    esac
  done
fi

# arm_waiter <what>: one detached waiter for the session boundary, never two (it holds the boundary lock
# for as long as it lives). It waits for the windows' lock at most BOUNDARY_SECS; once it holds it, it waits
# for the clone's own lock too (the runner holds it on its way out, after the task has let the windows' lock
# go), lets that one go, and runs a round with the windows' lock held
arm_waiter() {
  if ! exec 5>>"$w/maintenance-boundary.lock"; then add FAILED "boundary waiter: cannot open $w/maintenance-boundary.lock"; OUTCOME=failed; return; fi
  if ! /usr/bin/lockf -t 0 5 2>/dev/null; then exec 5>&-; log "a boundary waiter is already waiting; none added"; return; fi
  boundary_put waiting
  set -m
  (
    set +m
    exec 6>&- 7>&- 8>&- 9>&- < /dev/null
    if ! exec 9>>"${tmp}canon-paper-windows.lock"; then boundary_put failed "cannot open ${tmp}canon-paper-windows.lock"; exit 1; fi
    waitlock 9 "$BOUNDARY_SECS"; rc=$?
    if [ "$rc" = 0 ]; then
      if exec 4>>"$base/paper-checkout.lock" && ! waitlock 4 "$BOUNDARY_ROUND_SECS"; then
        log "boundary: the clone's lock was still held after $BOUNDARY_ROUND_SECS s; the round runs, and install leaves the clone as it is"
      fi
      exec 4>&-
      boundary_put "at the boundary"
      log "boundary: the windows' lock is free; a round runs the deferred part with it held"
      CANON_MAINTENANCE_AT_BOUNDARY=1 exec /bin/bash "$0" "$root" "$mise"
    fi
    if [ "$rc" != 1 ]; then boundary_put failed "lockf exited $rc on ${tmp}canon-paper-windows.lock"; exit 1; fi
    boundary_put "timed out" "the windows' lock stayed held for $BOUNDARY_SECS s"
    log "boundary: FAILED: the windows' lock stayed held for $BOUNDARY_SECS s; the next round that finds a window waits again"
  ) &
  set +m
  exec 5>&-
  log "deferred to the session boundary: $1; a waiter waits for the windows' lock as process $!"
}
[ "$FAILED" = '[]' ] || OUTCOME=failed
[ "$PINWAIT" != 1 ] || arm_waiter "the pins that differ"
if [ "$auto" != 1 ]; then
  [ "$compared" != 1 ] || { TRIED=null; DEFER=clear; }
  record; log "round ends: $OUTCOME"; exit 0
fi
# what the paper section of install manages: the runner, its plist and its agent (scripts/automation differs
# names each by its path or label)
autoall=$(jq -c 'map(select(startswith("automation: ")))' <<<"$FOUND")
autop=$(jq -c 'map(select(test("paper-session")))' <<<"$autoall")
# the windows' lock again, without waiting: the paper section of install reloads the paper agent
paper_lock install
mode=(install)
if [ "$WINDOW" = 1 ]; then
  # a window runs: install all but the paper section now, and leave that to the session boundary
  mode=(install --defer-paper)
  autof=$(jq -c --argjson p "$autop" '. - $p' <<<"$autoall")
  if [ "$autop" = '[]' ]; then DEFER="clear"
  else
    DEFER="set"
    NOTE="${NOTE:+$NOTE; }deferred to the session boundary: the paper checkout"
    arm_waiter "the paper checkout"
  fi
  if [ "$autof" = '[]' ]; then record; log "round ends: $OUTCOME (the paper checkout deferred to the session boundary)"; exit 0; fi
else
  autof=$autoall DEFER=clear
fi
# Not again within a day for the very differences an install already left: each restarts the paper
# agent's interval (ADR 0050), so a difference no install can fix would hold every window off.
prev=$(jq -c --argjson a "$autof" --arg c "$want" --argjson day "$DAY" \
         '.tried // empty | select(.found == $a and .commit == $c and (now - (.at_epoch // 0)) < $day
                                   and (.result == "done" or (.result == "failed" and (.timeout | not)))) | .at' "$STATUS" 2>/dev/null) || prev=""
if [ -n "$prev" ]; then
  add FAILED "automation install not repeated: these differences remained after the install at ${prev//\"/} at this origin/main; see that install in the log, or run scripts/automation install by hand"
  OUTCOME=failed; record; log "round ends: $OUTCOME (install not repeated)"; exit 0
fi
TRIED=$(jq -cn --arg at "$(now)" --argjson a "$autof" --arg c "$want" '{at: $at, at_epoch: now, found: $a, commit: $c}')
start=$(jq -cn '{state: "started", at_epoch: now}')
record "$start"
log "(b) scripts/automation ${mode[*]}, detached"
set -m
(
  set +m
  io=$(bounded "$STEP_SECS" "$root/scripts/automation" "${mode[@]}" 2>&1); rc=$?
  printf '%s\n' "$io" | sed 's/^/  /'
  # the failures install repeats after its status table, after "install: FAILED (exit N):"
  fl=$(printf '%s\n' "$io" | sed -n '/^install: FAILED (exit/,$p' | sed '1d' | sed 's/^ *FAILED: //' | grep -v '^$' | jq -Rsc 'split("\n") | map(select(length > 0))')
  to=false
  if timed_out "$rc"; then st=failed to=true; fl='["automation install (timeout after '"$STEP_SECS"' s)"]'
  elif [ "$rc" != 0 ]; then st=failed; [ "$fl" != '[]' ] || fl='["automation install (exit '"$rc"')"]'
  else st="done"; fl='[]'; fi
  # A recorder owns its loop lock until the firing finishes or fails. Its explicit deferral is
  # unfinished work, unlike an install that completed but left an unfixable difference: retry next round.
  deferred=$(printf '%s\n' "$io" | sed -n 's/^  deferred   movers recorder:.*$/movers recorder/p' \
      | jq -Rsc 'split("\n") | map(select(length > 0)) | unique')
  cur=$(jq -c 'objects' "$STATUS" 2>/dev/null) || cur='{}'
  [ -n "$cur" ] || cur='{}'
  put "$STATUS" "$(jq -c --arg s "$st" --argjson fl "$fl" --argjson rc "$rc" --argjson to "$to" --argjson d "$deferred" '
      .install = {state: $s, at_epoch: now, exit: $rc, failed: $fl, deferred: $d}
      | if .tried != null then .tried.result = (if $s == "done" and ($d | length) > 0 then "deferred" else $s end) | .tried.timeout = $to else . end
      | if $s == "done" then .installed += ["automation install"] else .failed += $fl | .outcome = "failed" end
      | .last_change = {at: .at, found: .found, installed: .installed, failed: .failed}' <<<"$cur")" \
    || log "could not write $STATUS"
  log "(b) scripts/automation install: $st (exit $rc)"
) &
log "round ends; the reinstall runs on as process $!"
exit 0
