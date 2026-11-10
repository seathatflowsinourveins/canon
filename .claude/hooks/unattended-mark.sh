#!/bin/bash
# Claude Code UserPromptSubmit hook.
# Contract: https://code.claude.com/docs/en/hooks.md — stdin carries the session id and the submitted prompt;
# exit 2 blocks the prompt, so it never reaches Claude, and stderr is the message shown with the block.
#
# Scheduled tasks for this repo put the token CANON-UNATTENDED-RUN in their prompt. When a prompt carries
# it, this hook marks the session, and guard.sh then holds that session to read-only tools for its whole
# life. Marking can only make a session more restricted, never less. Fails closed: if a prompt carries the
# token and the mark cannot be written (no usable session id, .canon not a directory, not writable), or the
# input cannot be read, the prompt is blocked. Prints nothing on stdout: stdout on this event would be
# added to Claude's context.
input=$(cat)
block() { echo "unattended-mark: prompt blocked. $1" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || exit 0     # guard.sh fails closed without jq, so nothing is left open here
has=$(jq '[.. | strings] | any(contains("CANON-UNATTENDED-RUN"))' <<<"$input" 2>/dev/null) \
  || block "The hook input was not valid JSON, so the session could not be checked for an unattended run."
[ "$has" = true ] || exit 0
root="${CLAUDE_PROJECT_DIR:-$(jq -r '.cwd // empty' <<<"$input")}"
[ -n "$root" ] || block "No project directory, so this unattended session could not be marked read-only."
sid=$(jq -r '.session_id // empty' <<<"$input")
case "$sid" in
  ''|*[!A-Za-z0-9_-]*) block "The session id is missing or unusable, so this unattended session could not be marked read-only." ;;
esac
if ! { mkdir -p "$root/.canon/unattended" && : > "$root/.canon/unattended/$sid"; } 2>/dev/null; then
  block "Could not write $root/.canon/unattended/$sid, so this unattended session would not be read-only."
fi
exit 0
