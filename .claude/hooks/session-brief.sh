#!/bin/bash
# Claude Code SessionStart hook.
# Contract: https://code.claude.com/docs/en/hooks.md
#   plain stdout on SessionStart is added to Claude's context (capped at 10,000 characters).
# Prints at most three short lines. Offline: reads only files in this repo.
# Prefer the tree the session is working in (a worktree has its own canon/ and docs/adr/).
input=$(cat 2>/dev/null || true)
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
root=$(git -C "${cwd:-.}" rev-parse --show-toplevel 2>/dev/null) || root="${CLAUDE_PROJECT_DIR:-$PWD}"
lock="$root/canon/upstreams.lock.json"
today=$(date -u +%Y-%m-%d)

if ! command -v jq >/dev/null 2>&1 || [ ! -f "$lock" ]; then
  echo "canon: status unavailable (jq or canon/upstreams.lock.json missing). Run scripts/canon lint."
  exit 0
fi

pins=$(jq '.pins | length' "$lock")
at=$(jq -r '.resolved_at[0:10]' "$lock")
echo "canon: $pins upstreams pinned, last resolved $at. Decisions: docs/adr/."

expired=""; proposed=""
for f in "$root"/docs/adr/[0-9][0-9][0-9][0-9]-*.md; do
  [ -f "$f" ] || continue
  n=$(basename "$f" | cut -c1-4)
  by=$(sed -n 's/^review-by: *//p' "$f" | head -1)
  st=$(sed -n 's/^status: *//p' "$f" | head -1)
  # ISO dates compare correctly as strings
  if [ -n "$by" ] && [ "$by" \< "$today" ]; then expired="$expired $n"; fi
  [ "$st" = "proposed" ] && proposed="$proposed $n"
done
[ -n "$expired" ]  && echo "ADRs past review-by, re-verify before relying on them:$expired"
[ -n "$proposed" ] && echo "ADRs still proposed (open gaps):$proposed"

# Last result of the background landscape watch (.claude/hooks/canon-watch.sh), if there is one.
watch="$root/.canon/watch.json"
if [ -f "$watch" ]; then
  jq -r '
    "landscape watch " + (.checked_at[0:10]) + ": "
    + (if .ok then "" else "INCOMPLETE (" + (.errors | join("; ")) + "); " end)
    + ((.newer | length | tostring) + " newer release(s), "
       + (.advanced | length | tostring) + " tracked branch(es) with new commits, "
       + (.retagged | length | tostring) + " retagged")
    + (if (.retagged | length) > 0
       then ". RETAGGED: a pinned release tag now points at a different commit; treat as a supply-chain warning and do not re-resolve until it is explained: " + (.retagged | join(", "))
       else "" end)
    + (if (.newer | length) > 0
       then ". Newer: " + (.newer | map(.id + " " + .pinned + " -> " + .newest
              + (if .status == "STABLE-AVAILABLE" then " (stable now available)" else "" end)) | join("; "))
       else "" end)
    + (if ((.retagged | length) + (.newer | length)) > 0 then ". Use the landscape-refresh skill before moving any pin." else "." end)
    + (if has("bot_prs") and .bot_prs == null then " The dependency bot'"'"'s pull requests could not be listed (is gh signed in?)." else "" end)
    + (if ((.bot_prs // []) | length) > 0
       then " Dependency pull requests open: " + (.bot_prs | map("#" + (.number | tostring)) | join(", ")) + ". Land them (AGENTS.md, Landing)."
       else "" end)
  ' "$watch" 2>/dev/null || echo "landscape watch: .canon/watch.json is unreadable."
else
  echo "landscape watch: no result yet; the first check runs in the background now."
fi
exit 0
