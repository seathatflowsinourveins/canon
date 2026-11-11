#!/bin/bash
# Paper session firing. Run by the launchd user agent local.canon.paper-session every 35 minutes while the
# Mac is awake (launchd.plist(5), StartInterval); installed and removed by scripts/automation (ADR 0050).
#
# Usage: paper-session.sh <mise> <checkout> <origin url> [paper:session arguments...]
#        paper-session.sh --checkout-only <mise> <checkout> <origin url>
#
# Each firing runs exactly the main that the origin has at that moment, in a clone used by nothing else:
#   1. bring <checkout> to origin's main, detached, by the sequence actions/checkout v7.0.1 uses on a
#      directory it reuses (src/git-directory-helper.ts, src/git-source-provider.ts and
#      src/git-command-manager.ts at 3d3c42e5aac5): recreate the clone when it has no .git or its fetch URL
#      differs; delete a stale index.lock and shallow.lock; detach; delete every local branch; `git clean
#      -ffdx` then `git reset --hard HEAD`, recreating the clone if either fails; `git fetch --no-tags --prune
#      --no-recurse-submodules --depth=1 origin +refs/heads/main:refs/remotes/origin/main`; check out with
#      `--force`. Two departures: the checkout is detached (`--detach`, not `-B main`), so the clone holds no
#      branch; and `-e /.venv` keeps the project environment between firings, so `uv run --locked --exact`
#      updates it to uv.lock instead of rebuilding it inside the window's interval (uv's docs, "Syncing the
#      environment"). Every other ignored folder (catalog/, ledger/, .canon/) is wiped; a window reads none.
#      Nothing the clone's .git holds runs: every git command here runs with hooks off (`-c
#      core.hooksPath=/dev/null`, git-config(1), core.hooksPath), and a clone whose local config holds any
#      setting this script and `git init` did not write (a core.fsmonitor or a filter driver would run a
#      command) is made again. The kept .venv is trusted local state, as a person's checkout's is: uv's
#      `--exact` removes extraneous packages, not other files, and uv documents no check of those (ADR 0050).
#   2. check that HEAD is that commit and no tracked file differs;
#   3. run `mise run paper:session <arguments>` there with CANON_COMMIT set to it, so the window's records
#      name the code that ran (canon_trading.session_window.running_commit). `mise run` trusts the config it
#      runs ("Commands that run project code ... trust their active config automatically", mise trust docs
#      at v2026.11.5); scripts/automation uninstall removes that trust with the clone.
# Each firing, before anything else, stamps its start in scheduled-firing.json in that folder; status reads
# its age, which no window run by hand changes, to tell a schedule that has stopped firing.
# An origin URL with user information in it (a token or password, RFC 3986 section 3.2.1) is refused, and
# a message names the origin by host and path only.
# A failure before the run writes last-firing.json as `failed: <why>` in the session windows' record folder,
# prints why and exits 1, which scripts/automation status shows; it never runs the code already in the
# clone (AGENTS.md rule 5). The paper lock, the flat-account check and the stale rule are paper:session's
# own and unchanged. A firing cannot overlap another: launchd misses a firing while the job runs, and kills
# what is left of the job's process group when it ends (launchd.plist(5), AbandonProcessGroup).
# CANON_PAPER_SESSION_DIR replaces the record folder (tests only).
# exported: under an empty environment bash keeps an assigned PATH to itself, and the task's exec()
# templates then failed with "exec command: No such file or directory" (observed with env -i, 2026-11-10;
# why mise needs PATH there is UNVERIFIED against its source)
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export GIT_TERMINAL_PROMPT=0   # never wait at a credential prompt: the clone fetches a public repository
# the clone reads no user git config (no insteadOf rewrite to ssh, no hooks or signing): git-config(1), ENVIRONMENT
export GIT_CONFIG_GLOBAL=/dev/null
only=0
if [ "${1:-}" = "--checkout-only" ]; then only=1; shift; fi
if [ $# -lt 3 ]; then echo "usage: paper-session.sh [--checkout-only] <mise> <checkout> <origin url> [arguments...]" >&2; exit 2; fi
mise=$1 dir=$2 url=$3; shift 3
records="${CANON_PAPER_SESSION_DIR:-$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)canon-paper-session}"

fail() {
  echo "paper-session: $1" >&2
  if [ "$only" = 0 ] && mkdir -p "$records"; then
    jq -n --arg o "failed: $1" '{at_epoch: now, outcome: $o, commit: "none: nothing ran"}' > "$records/.last-firing.json.tmp" \
      && mv -f "$records/.last-firing.json.tmp" "$records/last-firing.json"
  fi
  exit 1
}
# first, so the stamp counts every firing, one that fails included
if [ "$only" = 0 ]; then
  { mkdir -p "$records" && printf '{"at_epoch": %s}\n' "$(date +%s)" > "$records/.scheduled-firing.json.tmp" \
      && mv -f "$records/.scheduled-firing.json.tmp" "$records/scheduled-firing.json"; } \
    || fail "could not stamp the firing in $records"
fi
# a fetch that stalls below 1 KB/s for 60 s fails instead of holding the firing (git-config(1), http.lowSpeed*);
# no hook runs ("disable all hooks entirely by setting core.hooksPath to /dev/null", git-config(1))
g() { git -C "$dir" -c core.hooksPath=/dev/null -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "$@"; }

case "$dir" in /*) ;; *) fail "the checkout path must be absolute: $dir" ;; esac
# https only (a local path for tests): http.lowSpeed* bounds a stalled fetch there, and nothing bounds ssh;
# and no user information, which would be written into the clone's config and every message naming the URL
# (the one check: scripts/automation install runs this script with --checkout-only before it writes a plist).
# Neither refusal repeats the URL: an scp-style `user:token@host:path` is refused as not https.
case "$url" in
  https://*|/*) ;;
  *) fail "the origin must be an https URL; the one given is not (not repeated here: it may carry a credential)" ;;
esac
if [[ "$url" =~ ^https://[^/?#]*@ ]]; then
  fail "the origin URL carries credentials (user information before its host); give one without them"
fi
where=${url#https://}; where=${where%%[?#]*}   # the origin as messages name it: host and path
[ -x "$mise" ] || fail "mise not found at $mise; run: scripts/automation install"

# fresh: empty the directory's contents, keep the directory (actions/checkout's own way), and make it a clone
# of <origin url>. It empties only a directory that is empty or that it made itself (the clone's own config
# carries canon.paperCheckout), so a wrong path deletes nothing.
fresh() {
  mkdir -p "$dir" || return 1
  if [ -n "$(ls -A "$dir")" ] && { [ ! -d "$dir/.git" ] || [ "$(g config --local --get canon.paperCheckout 2>/dev/null)" != true ]; }; then
    echo "paper-session: $dir holds files and is not a clone this script made; not deleting it" >&2; return 1
  fi
  find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} + || return 1
  git -c core.hooksPath=/dev/null init -q "$dir" && g config canon.paperCheckout true && g remote add origin "$url"
}
# foreign_config: true when the clone's local config holds a setting neither this script nor `git init` writes.
# What `git init` writes is read from a throwaway repository made by the same git, so a git that writes
# other keys at init does not make every firing recreate the clone.
foreign_config() {
  local ref own rc
  ref=$(mktemp -d "${TMPDIR:-/tmp}/paper-session-init.XXXXXX") || return 0
  own=$(git -c core.hooksPath=/dev/null init -q "$ref" && git -C "$ref" config --local --name-only --list) || { rm -rf "$ref"; return 0; }
  rm -rf "$ref"
  own=$(printf '%s\ncanon.papercheckout\nremote.origin.url\nremote.origin.fetch\n' "$own")
  g config --local --name-only --list 2>/dev/null | grep -vxF "$own" >/dev/null; rc=$?
  [ "$rc" = 0 ]
}
# only a clone this script made is reused, cleaned and reset; anything else goes through fresh's refusal
if [ ! -d "$dir/.git" ] || [ "$(g config --local --get canon.paperCheckout 2>/dev/null)" != true ] \
   || [ "$(g config --get remote.origin.url 2>/dev/null)" != "$url" ]; then
  fresh || fail "could not create a clone in $dir"
elif foreign_config; then
  echo "paper-session: $dir holds local git config it did not write; recreating it" >&2
  fresh || fail "could not recreate the clone in $dir"
else
  rm -f "$dir/.git/index.lock" "$dir/.git/shallow.lock"
  if g rev-parse --verify -q HEAD >/dev/null; then
    { g checkout -q --detach \
        && refs=$(g for-each-ref --format='delete %(refname)' refs/heads/) \
        && { [ -z "$refs" ] || printf '%s\n' "$refs" | g update-ref --stdin; } \
        && g clean -ffdx -e /.venv -q && g reset -q --hard HEAD; } >/dev/null 2>&1 \
      || { echo "paper-session: could not clean $dir; recreating it" >&2; fresh || fail "could not recreate the clone in $dir"; }
  fi
fi
out=$(g fetch -q --no-tags --prune --no-recurse-submodules --depth=1 origin +refs/heads/main:refs/remotes/origin/main 2>&1) \
  || fail "could not fetch main from $where: $(printf '%s' "$out" | tr '\n' ' ')"
want=$(g rev-parse --verify -q refs/remotes/origin/main) || fail "the fetch left no origin/main in $dir"
out=$(g checkout -q --force --detach refs/remotes/origin/main 2>&1) || fail "could not check out $want: $(printf '%s' "$out" | tr '\n' ' ')"
head=$(g rev-parse --verify -q HEAD)
[ "$head" = "$want" ] || fail "HEAD is ${head:-nothing}, not origin/main $want"
[ -z "$(g status --porcelain -uno 2>&1)" ] || fail "tracked files differ from $want in $dir"

if [ "$only" = 1 ]; then echo "paper-session: $dir is at origin/main $want"; exit 0; fi
cd "$dir" || fail "cannot enter $dir"
echo "paper-session: $(date -u +%Y-%m-%dT%H:%M:%SZ) running origin/main $want"
CANON_COMMIT=$want exec "$mise" run paper:session "$@"
