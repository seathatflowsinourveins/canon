#!/bin/bash
# Claude Code SessionStart hook, registered with the matcher "compact" (ADR 0059).
# Contract: https://code.claude.com/docs/en/hooks.md (SessionStart, source "compact"; plain stdout is added to
# the context) and https://code.claude.com/docs/en/context-window.md (such hooks run after a compaction and
# their output joins the compacted context). It runs after a compaction, never before a tool call (ADR 0020).
# Prints one line pointing a coordinator at its state file, whose path the summary keeps by CLAUDE.md's
# "Compact instructions". Reads nothing and always exits 0.
echo "Context was just compacted. A coordinator: re-read your state file, at the path the summary above keeps, before acting. Every session: AGENTS.md and docs/adr/ still hold, unchanged."
exit 0
