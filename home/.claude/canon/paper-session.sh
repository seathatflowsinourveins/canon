#!/bin/bash
# Paper session firing. Run by the launchd user agent local.canon.paper-session PAPER_INTERVAL s after each firing ends (scripts/automation; about a minute, ADR 0050) while the
# Mac is awake (launchd.plist(5), StartInterval); installed and removed by scripts/automation (ADR 0050).
#
# Usage: paper-session.sh <mise> <checkout> <origin url> [paper:session arguments...]
#        paper-session.sh --checkout-only <mise> <checkout> <origin url>
#        paper-session.sh --recorder-checkout <mise> <checkout> <origin url>
# The recorder-only mode reuses its marked, matching clone (actions/checkout's existing-directory
# contract, src/git-directory-helper.ts at 3d3c42e5aac5). A failed fetch restores refs/remotes/origin/main;
# force checkout and clean -ffdx -e .venv restore that commit before use (git-checkout(1), git-clean(1)).
# It waits at most CANON_RECORDER_LOCK_SECS (default 60, UNCALIBRATED) for leftover uv descendants.
# Ordinary paper firings and install's --checkout-only retain their fresh-checkout contract.
#
# Each firing runs exactly the main that the origin has at that moment, in a clone used by nothing else:
#   1. make <checkout> a new clone of origin's main at every firing, by the path actions/checkout v7.0.1 takes
#      for a directory it does not reuse (src/git-directory-helper.ts, src/git-source-provider.ts and
#      src/git-command-manager.ts at 3d3c42e5aac5): delete the directory's contents, keep the directory,
#      `git init`, add the remote, `git fetch --no-tags --prune --no-recurse-submodules --depth=1 origin
#      +refs/heads/main:refs/remotes/origin/main`, check out with `--force`. Nothing an earlier firing or
#      anyone else left in the clone's .git or worktree reaches this one: no hook, local config, second
#      remote URL, branch, replace ref, index bit (skip-worktree), info/attributes rule or untracked file
#      (a mise.local.toml, which `mise run` would trust). Two departures: the checkout is detached
#      (`--detach`, not `-B main`), so the clone holds no branch; and the project environment, .venv, is
#      kept, so `uv run --locked --exact` updates it to uv.lock instead of rebuilding it inside the window's
#      interval (uv's docs, "Syncing the environment"). The kept .venv is trusted local state, as a person's
#      checkout's is: `--exact` removes extraneous packages, not other files, and uv documents no check of
#      those (ADR 0050). A fresh clone of GitHub's main took 0.5 s (ADR 0050). Every git command runs with
#      hooks off (`-c core.hooksPath=/dev/null`, git-config(1)), no user or system config
#      (GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM, git(1)) and no replace refs (GIT_NO_REPLACE_OBJECTS, git(1)).
#   2. check that HEAD is that commit and no tracked file differs;
#   3. run `mise run paper:session <arguments>` there (a `--strategy` among them reaches the window; the
#      schedule gives none, so it runs SessionMaCross) with CANON_COMMIT set to it, so the window's records
#      name the code that ran (canon_trading.session_window.running_commit). `mise run` trusts the config it
#      runs ("Commands that run project code ... trust their active config automatically", mise trust docs
#      at v2026.11.5); scripts/automation uninstall removes that trust with the clone.
# Bounds (timeout(1), macOS's /usr/bin/timeout): the fetch at FETCH_SECS (git's http.lowSpeed* stops only a
# fetch under 1 KB/s), and the whole `mise run` at RUN_SECS, SIGKILL to its process group: mise renders the
# task's env and installs a missing tool before the task's own `timeout` starts (observed with mise
# 2026.11.5: a 3 s exec() template under a 1 s task timeout completed), so RUN_SECS is paper:session's
# timeout (TASK_SECS) and START_SECS for mise's own start. scripts/automation status counts both (PAPER_FIRING).
# Scheduled or by hand: the agent's plist sets CANON_PAPER_SCHEDULED=1 (launchd.plist(5),
# EnvironmentVariables). Only then does a firing stamp its start in scheduled-firing.json, which status
# reads to tell a schedule that has stopped firing, and write to the records folder status reads; a run by
# hand writes to its by-hand/ folder, as paper:session does (mise.toml).
# An origin URL with user information, a query or a fragment in it (a token or password, RFC 3986 sections
# 3.2.1 and 3.4, which git repeats in its own messages) is refused, and a message names the origin by host
# and path only.
# A failure before the run (but an unknown records folder, below) writes last-firing.json as `failed: <why>`,
# prints why and exits 1, which
# scripts/automation status shows; it never runs the code already in the clone (AGENTS.md rule 5). The paper
# lock, the flat-account check and the stale rule are paper:session's own. A firing cannot overlap another:
# launchd misses a firing while the job runs, and kills what is left of the job's process group when it
# ends (launchd.plist(5), AbandonProcessGroup).
# CANON_PAPER_SESSION_DIR replaces the record folder, CANON_PAPER_FETCH_SECS and CANON_PAPER_RUN_SECS the
# bounds (tests only).
# exported: under an empty environment bash keeps an assigned PATH to itself, and the task's exec()
# templates then failed with "exec command: No such file or directory" (observed with env -i, 2026-11-10;
# why mise needs PATH there is UNVERIFIED against its source)
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export GIT_TERMINAL_PROMPT=0   # never wait at a credential prompt: the clone fetches a public repository
# no user or system git config (no insteadOf rewrite to ssh, no hooks, signing or credential helper), and no
# replacement objects: git(1), ENVIRONMENT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1
FETCH_SECS=${CANON_PAPER_FETCH_SECS:-120}
TASK_SECS=6300                 # mise.toml's paper:session timeout, 105 minutes (scripts/test-lanes holds them equal)
START_SECS=300                 # mise's start: a tool install, and the two keychain reads at 30 s each (mise.toml)
RUN_SECS=${CANON_PAPER_RUN_SECS:-$((TASK_SECS + START_SECS))}
only=0
recorder=0
if [ "${1:-}" = "--checkout-only" ]; then only=1; shift; fi
if [ "${1:-}" = "--recorder-checkout" ]; then only=1; recorder=1; shift; fi
if [ $# -lt 3 ]; then echo "usage: paper-session.sh [--checkout-only | --recorder-checkout] <mise> <checkout> <origin url> [arguments...]" >&2; exit 2; fi
mise=$1 dir=$2 url=$3; shift 3
# the records folder is in the per-user temporary folder (getconf(1)); a getconf that gives none stops a
# firing here, before anything runs, rather than write records under a relative name that status never reads.
# It can write no record of that, so it says so in its log and exits 1, which status shows as a failed firing
# (launchctl). --checkout-only writes no records. (CANON_GETCONF replaces getconf: tests only.)
utmp=""
if [ "$only" = 0 ] && [ -z "${CANON_PAPER_SESSION_DIR:-}" ]; then
  utmp=$("${CANON_GETCONF:-/usr/bin/getconf}" DARWIN_USER_TEMP_DIR 2>/dev/null) || utmp=""
  case "$utmp" in /*/) ;; *) echo "paper-session: the per-user temporary folder is not known (getconf DARWIN_USER_TEMP_DIR gave '$utmp'); nothing was run" >&2; exit 1 ;; esac
fi
records="${CANON_PAPER_SESSION_DIR:-${utmp}canon-paper-session}"
scheduled=false
# a run by hand says so to paper:session and the window too (CANON_PAPER_BY_HAND, mise.toml)
if [ "${CANON_PAPER_SCHEDULED:-}" = 1 ]; then scheduled=true; else records="$records/by-hand"; export CANON_PAPER_BY_HAND=1; fi

# write <name> <json>: replace <records>/<name> whole, through a temporary file of this writer's own
# (mktemp(1)), so two writers never share one
write() {
  local t
  [ ! -d "$records/$1" ] || return 1   # mv would move the file into it
  mkdir -p "$records" && t=$(mktemp "$records/.$1.XXXXXX") || return 1
  if printf '%s\n' "$2" > "$t" && mv -f "$t" "$records/$1"; then return 0; fi
  rm -f "$t"; return 1
}
fail() {
  echo "paper-session: $1" >&2
  [ "$only" = 1 ] || write last-firing.json "$(jq -cn --arg o "failed: $1" --argjson s "$scheduled" \
      '{at_epoch: now, outcome: $o, commit: "none: nothing ran", scheduled: $s}')"
  exit 1
}
# first, so the stamp counts every scheduled firing, one that fails included
if [ "$only" = 0 ] && [ "$scheduled" = true ]; then
  write scheduled-firing.json "{\"at_epoch\": $(date +%s)}" || fail "could not stamp the firing in $records"
fi
[ -x /usr/bin/timeout ] || fail "no /usr/bin/timeout, which bounds the fetch and the run"
# no hook runs ("disable all hooks entirely by setting core.hooksPath to /dev/null", git-config(1))
g() { git -C "$dir" -c core.hooksPath=/dev/null "$@"; }

case "$dir" in /*) ;; *) fail "the checkout path must be absolute: $dir" ;; esac
# https only (a local path for tests): http.lowSpeed* and the fetch bound stop a stalled fetch there, and
# nothing bounds ssh; no user information, query or fragment, which git writes into the clone's config and
# repeats in its own error messages (observed: a token in the query reached the firing's record). The one
# check: scripts/automation install runs this script with --checkout-only before it writes a plist. No
# refusal repeats the URL: an scp-style `user:token@host:path` is refused as not https.
case "$url" in
  https://*|/*) ;;
  *) fail "the origin must be an https URL; the one given is not (not repeated here: it may carry a credential)" ;;
esac
if [[ "$url" =~ ^https://[^/?#]*@ ]]; then
  fail "the origin URL carries credentials (user information before its host); give one without them"
fi
where=${url#https://}; where=${where%%[?#]*}   # the origin as messages name it: host and path
case "$url" in *[?#]*) fail "the origin URL $where has a query or fragment, which can carry a token; give one without" ;; esac
[ -x "$mise" ] || fail "mise not found at $mise; run: scripts/automation install"

[ ! -L "$dir" ] || fail "the checkout path is a link; give the folder itself: $dir"
# bounded git: a fetch that stalls below 1 KB/s for 60 s fails (git-config(1), http.lowSpeed*), any at FETCH_SECS
bg() { /usr/bin/timeout -k 5 "$FETCH_SECS" git -c core.hooksPath=/dev/null -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "$@"; }
# One run at a time in the clone: each holds an exclusive lock on <checkout>.lock, on descriptor 8, which
# `mise run` and every process of the window inherit (lockf(1)), so a run by hand, an install or the next
# firing never deletes the clone under a window still running from it (one the runner's bound or launchd's
# bootout did not reach: the task's deadline puts uv in a process group of its own, mise.toml).
mkdir -p "$(dirname "$dir")" || fail "cannot create the folder of $dir"
exec 8>>"$dir.lock" || fail "cannot open $dir.lock"
if [ "$recorder" = 1 ]; then
  lock_secs=${CANON_RECORDER_LOCK_SECS:-60}
  [[ "$lock_secs" =~ ^[1-9][0-9]*$ ]] || fail "invalid recorder checkout wait bound"
  end=$((SECONDS + lock_secs))
  while :; do
    rc=0; "${CANON_LOCKF:-/usr/bin/lockf}" -t 0 8 2>/dev/null || rc=$?
    [ "$rc" != 0 ] || break
    [ "$rc" = 75 ] || fail "checkout_lock_failed: lockf exited $rc"
    [ "$SECONDS" -lt "$end" ] || fail "checkout_lock_timeout: still held after $lock_secs s (UNCALIBRATED); firing ended"
    sleep 1
  done
elif ! /usr/bin/lockf -t 0 8 2>/dev/null; then
  [ "$only" = 1 ] || fail "a window is still running from $dir; nothing was run"
  # install's one check of the origin still runs, outside the clone
  out=$(bg ls-remote --exit-code "$url" refs/heads/main 2>&1) || fail "could not reach main at $where: $(printf '%s' "$out" | tr '\n' ' ')"
  echo "paper-session: main is at $where; a window is running from $dir, left as it is (the next firing makes it anew)"; exit 0
fi
# others: what the directory holds besides .venv
others() { find "$dir" -mindepth 1 -maxdepth 1 ! -name .venv -print | head -n 1; }
# marked <git dir>: a git directory, not a link, whose own config file carries the mark (read from the file
# itself, so a clone whose HEAD is broken is still known as ours)
marked() { [ -d "$1" ] && [ ! -L "$1" ] && [ "$(git config --file "$1/config" --get canon.paperCheckout 2>/dev/null)" = true ]; }
# fresh: empty the directory's contents but .venv, keep the directory (actions/checkout's own way), and make
# it a new clone of <origin url>. It empties only a directory that holds nothing but .venv or that it made
# itself (its .git, or the .git.old or .git.new a run stopped part way left, carries canon.paperCheckout), so
# a wrong path deletes nothing. The new .git is made and marked aside, then swapped in by two renames, so at
# every point a run can be stopped the folder holds a marked .git, .git.old or .git.new, or .venv alone (a
# first making stopped before its last rename left a marked .git.new alone, which every later firing refused).
fresh() {
  local t
  mkdir -p "$dir" || return 1
  if [ -n "$(others)" ] && ! marked "$dir/.git" && ! marked "$dir/.git.old" && ! marked "$dir/.git.new"; then
    echo "paper-session: $dir holds files and is not a clone this script made; not deleting it" >&2; return 1
  fi
  [ ! -L "$dir/.venv" ] || rm -f "$dir/.venv" || return 1   # a link would point the environment elsewhere
  find "$dir" -mindepth 1 -maxdepth 1 ! -name .venv ! -name .git ! -name .git.old -exec rm -rf {} + || return 1
  t=$(mktemp -d "${TMPDIR:-/tmp}/paper-session-init.XXXXXX") || return 1
  if ! { git -c core.hooksPath=/dev/null init -q "$t" && git -C "$t" config canon.paperCheckout true \
           && git -C "$t" remote add origin "$url" && mv "$t/.git" "$dir/.git.new"; }; then
    rm -rf "$t"; return 1
  fi
  rm -rf "$t"
  if [ -d "$dir/.git" ]; then rm -rf "$dir/.git.old" && mv "$dir/.git" "$dir/.git.old" || return 1; fi
  mv "$dir/.git.new" "$dir/.git" && rm -rf "$dir/.git.old" || return 1
  [ -z "$(find "$dir" -mindepth 1 -maxdepth 1 ! -name .venv ! -name .git -print | head -n 1)" ]
}
last_good=""; fetch_note=""
if [ "$recorder" = 1 ] && marked "$dir/.git" \
   && [ "$(g config --get remote.origin.url)" = "$url" ] \
   && g rev-parse --verify -q HEAD >/dev/null; then
  last_good=$(g rev-parse --verify -q refs/remotes/origin/main) || last_good=""
fi
if [ -z "$last_good" ]; then fresh || fail "could not make a new clone in $dir"; fi
if [ "$recorder" = 1 ]; then
  [ ! -L "$dir/.venv" ] || rm -f "$dir/.venv" || fail "could not remove the recorder venv symlink"
fi
out=$(bg -C "$dir" fetch -q --no-tags --prune --no-recurse-submodules --depth=1 origin +refs/heads/main:refs/remotes/origin/main 2>&1); rc=$?
if [ "$rc" != 0 ] && [ -n "$last_good" ]; then
  want=$last_good
  fetch_note="fetch_failed, checkout at ${want:0:12}"
  echo "paper-session: fetch_failed (exit $rc); reusing last good main $want" >&2
else
  [ "$rc" != 124 ] && [ "$rc" != 137 ] || fail "the fetch of main from $where did not finish within $FETCH_SECS s (or was killed)"
  [ "$rc" = 0 ] || fail "could not fetch main from $where: $(printf '%s' "$out" | tr '\n' ' ')"
  want=$(g rev-parse --verify -q refs/remotes/origin/main) || fail "the fetch left no origin/main in $dir"
fi
out=$(g checkout -q --force --detach "$want" 2>&1) || fail "could not check out $want: $(printf '%s' "$out" | tr '\n' ' ')"
if [ "$recorder" = 1 ]; then g clean -q -ffdx -e .venv || fail "could not clean the recorder checkout"; fi
head=$(g rev-parse --verify -q HEAD)
[ "$head" = "$want" ] || fail "HEAD is ${head:-nothing}, not origin/main $want"
[ -z "$(g status --porcelain -uno 2>&1)" ] || fail "tracked files differ from $want in $dir"

if [ "$only" = 1 ]; then echo "paper-session: $dir is at origin/main $want${fetch_note:+; $fetch_note}"; exit 0; fi
cd "$dir" || fail "cannot enter $dir"
# mise reads every config from the folder it runs in up to the root, here the automation folder, the Claude
# config folder and HOME above the clone; it searches no higher than MISE_CEILING_PATHS, "which is not
# searched itself" (mise settings, ceiling_paths), so the clone's parent keeps the run to the clone's own
# mise.toml (mise's global config, ~/.config/mise, still applies)
ceiling=$(cd "$(dirname "$dir")" && pwd -P) || fail "cannot read the folder above $dir"
# a list, so a path holding its separator would not be one ceiling (UNVERIFIED which character mise splits on;
# both are refused)
case "$ceiling" in *:*|*,*) fail "the folder above $dir holds a : or a , which MISE_CEILING_PATHS would split; give another path" ;; esac
export MISE_CEILING_PATHS="$ceiling"
echo "paper-session: $(date -u +%Y-%m-%dT%H:%M:%SZ) running origin/main $want"
# timeout(1): "makes itself the leader of a process group that the command inherits"; -s KILL ends the
# group whole, timeout and mise included, so the job ends on signal 9, which status reads (launchctl list).
# The task mise was running is in a process group of its own (observed with mise 2026.11.5), out of this
# kill's reach: a window then still runs to its own deadline, holding the paper lock and the clone's.
CANON_COMMIT=$want exec /usr/bin/timeout -s KILL "$RUN_SECS" "$mise" run paper:session "$@"
