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
#       (ADR 0065 rule 4), and Codex, DeepSeek Harness and dsh-tui are exact mise pins, which move through the
#       drift issue and adopt-upstream.
# Nothing here reads a secret, installs a tool (`scripts/bootstrap --install` stays a deliberate step) or
# changes the registry or the pins.
#
# Usage: maintenance.sh <checkout> <mise>
#
# The paper locks are the files mise.toml's paper tasks lock (ADR 0069): canon-paper-windows.lock, account B's,
# which a window holds; canon-paper-test.lock, account A's, which a paper test holds; and canon-alpaca-stream.lock,
# the login's market-data stream, which paper:data's sandbox node takes alone and a window or a streaming test
# also takes. A round takes them without waiting (lockf(1) -t 0) and never waits for one: when one is held,
# the round ends there, saying which. It takes all three at its start and for (a), since any of those runs may
# run from the checkout, and only the windows' lock for the install, which reloads the paper agent and touches
# nothing else they use. The daily updates, the compare and the settings step touch none of them and run with
# them let go. A scheduled window waits 900 s for its lock (mise.toml). The round holds its own lock,
# <config>/canon/maintenance.lock, for the whole round and the reinstall it starts, so rounds never overlap.
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
# there). CANON_GETCONF, CANON_MAINTENANCE_FETCH_SECS, CANON_MAINTENANCE_STEP_SECS and
# CANON_MAINTENANCE_UPDATE_SECS replace getconf and the bounds (tests only).
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
add() { local v; v=$(jq -c --arg x "$2" '. + [$x]' <<<"${!1}") && printf -v "$1" '%s' "$v"; }
# record [install json]: this round's record; the last round that found something is kept as last_change
record() {
  local prev='{}'
  [ ! -f "$STATUS" ] || prev=$(jq -c 'objects' "$STATUS" 2>/dev/null) || prev='{}'
  [ -n "$prev" ] || prev='{}'
  put "$STATUS" "$(jq -cn --argjson p "$prev" --arg at "$(now)" --arg o "$OUTCOME" --arg ff "$FF" --arg n "$NOTE" \
      --argjson f "$FOUND" --argjson i "$INSTALLED" --argjson x "$FAILED" --arg r "$root" --argjson ins "${1:-null}" \
      --argjson tr "$TRIED" '
      {at: $at, at_epoch: now, checkout: $r, outcome: $o, ff: $ff, found: $f, installed: $i, failed: $x,
       note: $n, install: $ins, tried: (if $tr == "keep" then ($p.tried // null) else $tr end)}
      | .last_change = (if ($f | length) > 0 then {at: $at, found: $f, installed: $i, failed: $x} else ($p.last_change // null) end)')" \
    || log "could not write $STATUS"
}
# bounded <secs> <command...>: the command under timeout(1), SIGKILL 5 s after SIGTERM, with stdin empty and
# the locks closed, so nothing it leaves running holds them
bounded() { local s=$1; shift; /usr/bin/timeout -k 5 "$s" "$@" < /dev/null 6>&- 7>&- 8>&- 9>&-; }
timed_out() { [ "$1" = 124 ] || [ "$1" = 137 ]; }
g() { bounded "$GIT_SECS" git -C "$root" "$@"; }

# one round at a time, the reinstall it started included
exec 7>>"$base/maintenance.lock" || { log "cannot open $base/maintenance.lock"; exit 1; }
if ! /usr/bin/lockf -t 0 7 2>/dev/null; then log "an earlier round, or the reinstall it started, still runs; nothing this round"; exit 0; fi
log "round starts in $root"
# the paper locks are in the per-user temporary folder, where mise.toml's paper tasks lock them on a descriptor
tmp=$("${CANON_GETCONF:-/usr/bin/getconf}" DARWIN_USER_TEMP_DIR 2>/dev/null) || tmp=""
case "$tmp" in
  /*/) ;;
  *) OUTCOME=failed FF="not run"; add FAILED "paper lock: the per-user temporary folder is not known (getconf DARWIN_USER_TEMP_DIR)"
     record; log "the per-user temporary folder is not known, so there is no paper lock to take; nothing was run"; exit 1 ;;
esac
paper_free() { exec 6>&- 8>&- 9>&-; }
# paper_lock <checkout|install>: takes, without waiting, the windows' lock on descriptor 9 and, for
# `checkout`, the paper tests' on 8 and the stream lock on 6. 0 when this round holds them all; 1 when one is
# held, with HELD saying which (every caller then ends the round). A lock file that cannot be opened ends the
# round FAILED, naming it.
paper_lock() {
  local spec fd f who
  paper_free
  for spec in "9 canon-paper-windows.lock a paper window" "8 canon-paper-test.lock a paper test" \
              "6 canon-alpaca-stream.lock a run on the market-data stream (paper:data's sandbox node, a window or a paper test)"; do
    read -r fd f who <<<"$spec"
    [ "$1" = checkout ] || [ "$fd" = 9 ] || continue
    if ! eval "exec $fd>>\"\${tmp}\$f\""; then
      OUTCOME=failed; add FAILED "paper lock: cannot open ${tmp}$f"; record; log "cannot open ${tmp}$f"; exit 1
    fi
    if ! /usr/bin/lockf -t 0 "$fd" 2>/dev/null; then HELD="$f is held: $who runs"; return 1; fi
  done
}
HELD=""
if ! paper_lock checkout; then
  OUTCOME=waited FF="not run: the paper lock is held ($HELD), so nothing this round"
  record; log "$FF"; exit 0
fi

# (c) the runtimes' own updates, once a day, first and with the paper lock let go: they touch nothing a
# window uses
paper_free
last=$(jq -r '.at_epoch // 0 | floor' "$UPDATES" 2>/dev/null) || last=0
[[ "$last" =~ ^[0-9]+$ ]] || last=0
if [ $(( $(date +%s) - last )) -ge "$DAY" ]; then
  log "(c) runtime updates (Codex, Muse Code, DeepSeek Harness and dsh-tui are not updated here: Codex, DeepSeek Harness and dsh-tui are exact mise pins)"
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

[ "$FAILED" = '[]' ] || OUTCOME=failed
if [ "$auto" != 1 ]; then
  [ "$compared" != 1 ] || TRIED=null
  record; log "round ends: $OUTCOME"; exit 0
fi
# (b) the agents and their scripts, last and detached: install reloads launchd agents, this one included.
# Not again within a day for the very differences an install already left: each restarts the paper
# agent's interval (ADR 0050), so a difference no install can fix would hold every window off.
autof=$(jq -c 'map(select(startswith("automation: ")))' <<<"$FOUND")
prev=$(jq -c --argjson a "$autof" --arg c "$want" --argjson day "$DAY" \
         '.tried // empty | select(.found == $a and .commit == $c and (now - (.at_epoch // 0)) < $day
                                   and (.result == "done" or (.result == "failed" and (.timeout | not)))) | .at' "$STATUS" 2>/dev/null) || prev=""
if [ -n "$prev" ]; then
  add FAILED "automation install not repeated: these differences remained after the install at ${prev//\"/} at this origin/main; see that install in the log, or run scripts/automation install by hand"
  OUTCOME=failed; record; log "round ends: $OUTCOME (install not repeated)"; exit 0
fi
# the windows' lock again, without waiting: install reloads the paper agent
if ! paper_lock install; then
  NOTE="${NOTE:+$NOTE; }automation install not started: the windows' lock was taken ($HELD); the next round installs"
  record; log "round ends: $OUTCOME (install waits for the windows' lock)"; exit 0
fi
TRIED=$(jq -cn --arg at "$(now)" --argjson a "$autof" --arg c "$want" '{at: $at, at_epoch: now, found: $a, commit: $c}')
start=$(jq -cn '{state: "started", at_epoch: now}')
record "$start"
log "(b) scripts/automation install, detached"
set -m
(
  set +m
  io=$(bounded "$STEP_SECS" "$root/scripts/automation" install 2>&1); rc=$?
  printf '%s\n' "$io" | sed 's/^/  /'
  # the failures install repeats after its status table, after "install: FAILED (exit N):"
  fl=$(printf '%s\n' "$io" | sed -n '/^install: FAILED (exit/,$p' | sed '1d' | sed 's/^ *FAILED: //' | grep -v '^$' | jq -Rsc 'split("\n") | map(select(length > 0))')
  to=false
  if timed_out "$rc"; then st=failed to=true; fl='["automation install (timeout after '"$STEP_SECS"' s)"]'
  elif [ "$rc" != 0 ]; then st=failed; [ "$fl" != '[]' ] || fl='["automation install (exit '"$rc"')"]'
  else st="done"; fl='[]'; fi
  cur=$(jq -c 'objects' "$STATUS" 2>/dev/null) || cur='{}'
  [ -n "$cur" ] || cur='{}'
  put "$STATUS" "$(jq -c --arg s "$st" --argjson fl "$fl" --argjson rc "$rc" --argjson to "$to" '
      .install = {state: $s, at_epoch: now, exit: $rc, failed: $fl}
      | if .tried != null then .tried.result = $s | .tried.timeout = $to else . end
      | if $s == "done" then .installed += ["automation install"] else .failed += $fl | .outcome = "failed" end
      | .last_change = {at: .at, found: .found, installed: .installed, failed: .failed}' <<<"$cur")" \
    || log "could not write $STATUS"
  log "(b) scripts/automation install: $st (exit $rc)"
) &
log "round ends; the reinstall runs on as process $!"
exit 0
