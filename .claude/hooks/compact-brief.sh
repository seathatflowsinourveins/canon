#!/bin/bash
# Claude Code SessionStart hook, registered with the matcher "compact" (ADR 0059).
# Contract: https://code.claude.com/docs/en/hooks.md (SessionStart, source "compact"; plain stdout is added to
# the context) and https://code.claude.com/docs/en/context-window.md (such hooks run after a compaction and
# their output joins the compacted context). It runs after a compaction, never before a tool call (ADR 0020).
# Prints one static line pointing a coordinator at its Orca Run (ADR 0078 item 5), which holds the queue and
# lane table. It runs none of the commands, so it never waits on the app. Reads nothing and always exits 0.
echo "Context was just compacted. A coordinator only (a worker or reviewer takes no Run): re-read your Run before acting (orca orchestration run-list, then run-use --id <run>, task-list --ready, gate-list, worker-list; then git log on main and scripts/automation status). Every session: AGENTS.md and docs/adr/ still hold, unchanged."
exit 0
