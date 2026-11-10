#!/bin/bash
# Claude Code UserPromptSubmit hook.
# Contract: https://code.claude.com/docs/en/hooks.md — stdin carries the session id and the submitted prompt.
#
# Scheduled tasks for this repo put the token CANON-UNATTENDED-RUN in their prompt. When a prompt carries
# it, this hook marks the session, and guard.sh then holds that session to read-only tools for its whole
# life. Marking can only make a session more restricted, never less. Prints nothing: stdout on this event
# would be added to Claude's context.
input=$(cat)
command -v jq >/dev/null 2>&1 || exit 0     # guard.sh fails closed without jq, so nothing is left open here
root="${CLAUDE_PROJECT_DIR:-$(jq -r '.cwd // empty' <<<"$input")}"
sid=$(jq -r '.session_id // empty' <<<"$input")
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac
if jq -e '[.. | strings] | any(contains("CANON-UNATTENDED-RUN"))' <<<"$input" >/dev/null 2>&1; then
  mkdir -p "$root/.canon/unattended" 2>/dev/null && : > "$root/.canon/unattended/$sid"
fi
exit 0
