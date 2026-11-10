#!/bin/bash
# Claude Code PostToolUse hook, matcher "Edit|Write".
# Contract: https://code.claude.com/docs/en/hooks.md
#   stdin  = JSON with tool_input; ${CLAUDE_PROJECT_DIR} is exported to the hook.
#   exit 2 = stderr is shown to Claude (the edit has already happened, so this is feedback, not a block).
# Runs `scripts/canon lint` only when the edited file is under canon/ or docs/adr/.
input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  echo "canon-lint hook: jq not found, so the lint did NOT run." >&2
  exit 2
fi
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path? // empty')
case "$file" in
  */canon/*|*/docs/adr/*) ;;
  *) exit 0 ;;
esac
# In a worktree session hooks run from the main checkout and CLAUDE_PROJECT_DIR points there
# (https://code.claude.com/docs/en/worktrees.md), so find the tree that holds the edited file.
root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || root="${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}"
if [ ! -x "$root/scripts/canon" ]; then
  echo "canon-lint hook: $root/scripts/canon is missing or not executable, so the lint did NOT run." >&2
  exit 2
fi
if out=$("$root/scripts/canon" lint 2>&1); then
  exit 0
fi
echo "scripts/canon lint failed after editing ${file#"$root"/}:" >&2
echo "$out" >&2
exit 2
