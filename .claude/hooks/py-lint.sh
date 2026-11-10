#!/bin/bash
# Claude Code PostToolUse hook, matcher "Edit|Write".
# Contract: https://code.claude.com/docs/en/hooks.md — exit 2 shows stderr to Claude; the edit has
# already happened, so this is feedback, not a block.
# After a Python file is edited, runs the pinned ruff on that one file (lint, then format check), so
# problems surface at the edit instead of at the commit gate. Other files are ignored.
input=$(cat)
command -v jq >/dev/null 2>&1 || { echo "py-lint hook: jq not found, so ruff did NOT run." >&2; exit 2; }
file=$(jq -r '.tool_input.file_path? // empty' <<<"$input")
case "$file" in *.py) ;; *) exit 0 ;; esac
[ -f "$file" ] || exit 0
# A worktree session runs hooks from the main checkout; use the tree that holds the edited file.
root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || root="${CLAUDE_PROJECT_DIR:-$(jq -r '.cwd // empty' <<<"$input")}"
if command -v mise >/dev/null 2>&1; then mise=$(command -v mise)
elif [ -x "$HOME/.local/bin/mise" ]; then mise="$HOME/.local/bin/mise"
else echo "py-lint hook: mise not found, so ruff did NOT run on ${file#"$root"/}." >&2; exit 2; fi
cd "$root" || exit 0
if ! out=$("$mise" exec -- uv run --locked ruff check "$file" 2>&1); then
  echo "ruff check failed on ${file#"$root"/}:" >&2; echo "$out" | tail -30 >&2; exit 2
fi
if ! out=$("$mise" exec -- uv run --locked ruff format --check "$file" 2>&1); then
  echo "ruff format --check failed on ${file#"$root"/}. Run: uv run ruff format ${file#"$root"/}" >&2; exit 2
fi
exit 0
