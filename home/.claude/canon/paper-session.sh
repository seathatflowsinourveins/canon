#!/bin/bash
# Paper session firing. Run by the launchd user agent local.canon.paper-session every 35 minutes while the
# Mac is awake (launchd.plist(5), StartInterval); installed and removed by scripts/automation (ADR 0047).
#
# Usage: paper-session.sh <mise> <checkout> <origin url> [paper:session arguments...]
#        paper-session.sh --checkout-only <mise> <checkout> <origin url>
#
# Each firing runs exactly the main that the origin has at that moment, in a clone used by nothing else:
#   1. bring <checkout> to origin's main, detached, by the sequence actions/checkout v7.0.1 uses on a
#      directory it reuses (src/git-directory-helper.ts, src/git-source-provider.ts and
#      src/git-command-manager.ts at 3d3c42e5aac5): recreate the clone when it has no .git or its fetch URL
#      differs; delete a stale index.lock and shallow.lock; detach; `git clean -ffdx` then
#      `git reset --hard HEAD`, recreating the clone if either fails; `git fetch --no-tags --prune
#      --no-recurse-submodules --depth=1 origin +refs/heads/main:refs/remotes/origin/main`; check out with
#      `--force`. Two departures: the checkout is detached (`--detach`, not `-B main`), so the clone holds no
#      branch; and `-e /.venv` keeps the project environment between firings, so `uv run --locked --exact`
#      updates it to uv.lock instead of rebuilding it inside the window's interval (uv's docs, "Syncing the
#      environment"). Every other ignored folder (catalog/, ledger/, .canon/) is wiped; a window reads none.
#   2. check that HEAD is that commit and no tracked file differs;
#   3. run `mise run paper:session <arguments>` there with CANON_COMMIT set to it, so the window's records
#      name the code that ran (canon_trading.session_window.running_commit). `mise run` trusts the config it
#      runs ("Commands that run project code ... trust their active config automatically", mise trust docs
#      at v2026.11.5); scripts/automation uninstall removes that trust with the clone.
# A failure before the run writes last-firing.json as `failed: <why>` in the session windows' record folder,
# prints why and exits 1, which scripts/automation status shows; it never runs the code already in the
# clone (AGENTS.md rule 5). The paper lock, the flat-account check and the stale rule are paper:session's
# own and unchanged. A firing cannot overlap another: launchd misses a firing while the job runs, and kills
# what is left of the job's process group when it ends (launchd.plist(5), AbandonProcessGroup).
# CANON_PAPER_SESSION_DIR replaces the record folder (tests only).
PATH=/usr/bin:/bin:/usr/sbin:/sbin
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
# a fetch that stalls below 1 KB/s for 60 s fails instead of holding the firing (git-config(1), http.lowSpeed*)
g() { git -C "$dir" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "$@"; }

case "$dir" in /*) ;; *) fail "the checkout path must be absolute: $dir" ;; esac
# https only (a local path for tests): http.lowSpeed* bounds a stalled fetch there, and nothing bounds ssh
case "$url" in https://*|/*) ;; *) fail "the origin must be an https URL, not $url" ;; esac
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
  git init -q "$dir" && g config canon.paperCheckout true && g remote add origin "$url"
}
# only a clone this script made is reused, cleaned and reset; anything else goes through fresh's refusal
if [ ! -d "$dir/.git" ] || [ "$(g config --local --get canon.paperCheckout 2>/dev/null)" != true ] \
   || [ "$(g config --get remote.origin.url 2>/dev/null)" != "$url" ]; then
  fresh || fail "could not create a clone in $dir"
else
  rm -f "$dir/.git/index.lock" "$dir/.git/shallow.lock"
  if g rev-parse --verify -q HEAD >/dev/null; then
    { g checkout -q --detach && g clean -ffdx -e /.venv -q && g reset -q --hard HEAD; } >/dev/null 2>&1 \
      || { echo "paper-session: could not clean $dir; recreating it" >&2; fresh || fail "could not recreate the clone in $dir"; }
  fi
fi
out=$(g fetch -q --no-tags --prune --no-recurse-submodules --depth=1 origin +refs/heads/main:refs/remotes/origin/main 2>&1) \
  || fail "could not fetch main from $url: $(printf '%s' "$out" | tr '\n' ' ')"
want=$(g rev-parse --verify -q refs/remotes/origin/main) || fail "the fetch left no origin/main in $dir"
out=$(g checkout -q --force --detach refs/remotes/origin/main 2>&1) || fail "could not check out $want: $(printf '%s' "$out" | tr '\n' ' ')"
head=$(g rev-parse --verify -q HEAD)
[ "$head" = "$want" ] || fail "HEAD is ${head:-nothing}, not origin/main $want"
[ -z "$(g status --porcelain -uno 2>&1)" ] || fail "tracked files differ from $want in $dir"

if [ "$only" = 1 ]; then echo "paper-session: $dir is at origin/main $want"; exit 0; fi
cd "$dir" || fail "cannot enter $dir"
echo "paper-session: $(date -u +%Y-%m-%dT%H:%M:%SZ) running origin/main $want"
CANON_COMMIT=$want exec "$mise" run paper:session "$@"
