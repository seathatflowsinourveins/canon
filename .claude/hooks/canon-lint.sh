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
[ -n "$file" ] || exit 0
# In a worktree session hooks run from the main checkout and CLAUDE_PROJECT_DIR points there
# (https://code.claude.com/docs/en/worktrees.md), so find the tree that holds the edited file. Both the
# file's directory and the tree root are physical paths, so the file's path relative to the root is exact
# even when the tree lives under a symlink. The match is on that relative path, so a tree whose own
# directory is named canon does not match every edit. A file in another repository, or outside the project
# when the project is not a git checkout, is not this tree's and is left alone.
project="${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}"
project=$(cd "$project" 2>/dev/null && pwd -P) || project=""
# common_dir <dir>  -> the physical git common directory of the checkout at dir, shared by its worktrees
common_dir() { (cd "$1" 2>/dev/null && d=$(git rev-parse --git-common-dir 2>/dev/null) && [ -n "$d" ] && cd "$d" && pwd -P); }
if dir=$(cd "$(dirname "$file")" 2>/dev/null && pwd -P); then file="$dir/$(basename "$file")"; else dir=""; fi
if [ -z "$dir" ] || ! root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null); then root="$project"; fi
if [ -n "$project" ]; then
  if pc=$(common_dir "$project") && [ -n "$pc" ]; then
    [ "$(common_dir "$root")" = "$pc" ] || exit 0
  else
    case "$file" in "$project"/*) ;; *) exit 0 ;; esac
  fi
fi
[ -n "$root" ] || exit 0
case "$file" in
  "$root"/*) rel=${file#"$root"/} ;;
  *) exit 0 ;;
esac
case "$rel" in
  canon/*|docs/adr/*) ;;
  *) exit 0 ;;
esac
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
