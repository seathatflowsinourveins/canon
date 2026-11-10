#!/bin/bash
# Claude Code SessionStart hook, registered with "async": true.
# Contract: https://code.claude.com/docs/en/hooks.md — an async command hook runs in the background and
# cannot block or delay the session.
#
# At most once every 24 hours it compares the canon with live upstream:
#   scripts/canon check      RETAGGED: a pinned release tag now points at a different commit (a supply-chain
#                            warning); advanced: a tracked branch has new commits (normal); STALE (exit 4): the
#                            registry names another repo or track than the lock, so that pin was not compared
#   scripts/canon outdated   a newer release tag than the one pinned
#   gh pr list --app dependabot   the dependency bot's open pull requests (ADR 0016), where there is a
#                            GitHub remote; flags are from gh's own help
# and writes .canon/watch.json. .claude/hooks/session-brief.sh prints that result at the next session start.
# It costs no tokens and moves no pins. A run where `canon check` or `canon outdated` could not read every
# upstream is recorded as not ok and retried after an hour, never reported as a pass. Stale pins are recorded
# as `stale` (their ids) without marking the run not ok: every other pin was compared, and only a resolve,
# reviewed before it lands (ADR 0028), changes them. A dependency bot listing that fails is recorded as
# `bot_prs: null` without marking the run not ok, so it is tried again only at the next daily run.
# Each tree (the main checkout or a worktree, which has its own canon/ and docs/adr/) compares its own
# registry and keeps the result in its own .canon/watch.json. The record carries `registry`, a checksum of
# that tree's canon/upstreams.json and canon/upstreams.lock.json; session-brief.sh and scripts/automation
# show a record only when it matches the tree's files now, and this hook runs again at once when it does not.
input=$(cat 2>/dev/null || true)
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
root=$(git -C "${cwd:-.}" rev-parse --show-toplevel 2>/dev/null) || root="${CLAUDE_PROJECT_DIR:-$PWD}"
state="$root/.canon"
out="$state/watch.json"
command -v jq >/dev/null 2>&1 || exit 0          # session-brief.sh already reports a missing jq
[ -x "$root/scripts/canon" ] || exit 0
mkdir -p "$state" 2>/dev/null || exit 0
# the same checksum is computed in session-brief.sh and scripts/automation
fp=$(cat "$root/canon/upstreams.json" "$root/canon/upstreams.lock.json" 2>/dev/null | cksum | awk '{ print $1 "-" $2 }')

now=$(date -u +%s)
if [ -f "$out" ] && [ "$(jq -r '.registry // ""' "$out" 2>/dev/null)" = "$fp" ]; then
  last=$(jq -r '.checked_at_epoch // 0' "$out" 2>/dev/null); last=${last:-0}
  ok=$(jq -r '.ok // false' "$out" 2>/dev/null)
  age=$((now - last))
  if [ "$ok" = "true" ] && [ "$age" -lt 86400 ]; then exit 0; fi
  if [ "$ok" != "true" ] && [ "$age" -lt 3600 ]; then exit 0; fi
fi

# one run at a time: mkdir is atomic. A lock older than 10 minutes is from a run that died; take it over.
lock="$state/watch.lock"
if ! mkdir "$lock" 2>/dev/null; then
  [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ] || exit 0
fi
trap 'rmdir "$lock" 2>/dev/null' EXIT

errors='[]'
check_out=$("$root/scripts/canon" check 2>&1); check_rc=$?
retagged=$(printf '%s\n' "$check_out" | awk '$1 == "RETAGGED" { print $2 }' | jq -R . | jq -sc 'map(select(. != ""))')
advanced=$(printf '%s\n' "$check_out" | awk '$1 == "advanced" { print $2 }' | jq -R . | jq -sc 'map(select(. != ""))')
stale=$(printf '%s\n' "$check_out" | awk '$1 == "STALE" { print $2 }' | jq -R . | jq -sc 'map(select(. != ""))')
# Exit 1 outranks 3 and 4 (scripts/canon, cmd_check), so 3 or 4 means every pin that could be compared was.
if [ "$check_rc" -ne 0 ] && [ "$check_rc" -ne 3 ] && [ "$check_rc" -ne 4 ]; then
  errors=$(jq -c '. + ["canon check could not read every upstream"]' <<<"$errors")
fi

# A pin the release rule cannot rank (UNPARSEABLE) is kept too: nothing else would say it is unwatched.
newer='[]'; unparseable='[]'
if rel=$("$root/scripts/canon" outdated --json 2>/dev/null) && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$rel"; then
  newer=$(jq -c '[.[] | select(.status == "NEWER" or .status == "STABLE-AVAILABLE")]' <<<"$rel")
  unparseable=$(jq -c '[.[] | select(.status == "UNPARSEABLE") | {id, pinned}]' <<<"$rel")
else
  errors=$(jq -c '. + ["canon outdated could not read every upstream"]' <<<"$errors")
fi

# A gh that cannot answer is recorded as null, never as "none open". It does not mark the whole watch
# as failed: the upstream comparison above succeeded and need not be repeated every hour.
bot_prs='[]'
if git -C "$root" remote get-url origin >/dev/null 2>&1; then
  mise_bin=$(command -v mise 2>/dev/null) || mise_bin="$HOME/.local/bin/mise"
  # perl's alarm bounds the call at 60 seconds; macOS ships perl and has no timeout command.
  if prs=$(cd "$root" && perl -e 'alarm 60; exec @ARGV' "$mise_bin" exec -- gh pr list --app dependabot --state open --json number,title 2>/dev/null) \
     && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$prs"; then
    bot_prs=$(jq -c '[.[] | {number, title}]' <<<"$prs")
  else
    bot_prs=null      # not asked, which is different from none open; the brief and the status row say so
  fi
fi

jq -n --argjson epoch "$now" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --argjson retagged "$retagged" --argjson advanced "$advanced" --argjson newer "$newer" \
      --argjson unparseable "$unparseable" --argjson bot_prs "$bot_prs" --argjson errors "$errors" \
      --argjson stale "$stale" --arg registry "$fp" \
      '{checked_at: $at, checked_at_epoch: $epoch, registry: $registry, ok: ($errors | length == 0),
        retagged: $retagged, advanced: $advanced, stale: $stale, newer: $newer, unparseable: $unparseable,
        bot_prs: $bot_prs, errors: $errors}' > "$out.tmp" && mv "$out.tmp" "$out"
exit 0
