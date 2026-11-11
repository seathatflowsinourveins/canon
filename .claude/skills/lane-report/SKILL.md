---
name: lane-report
description: Report the state of every Claude Code session on this machine, covering which are working, idle or quiet, tool failures, permission denials, model switches, compactions, skill, MCP and subagent use, and spend. Use when asked how the lanes or sessions are doing, and before starting more parallel work.
context: fork
model: haiku
---

# Lane report

1. Run `scripts/lanes`. If it says the ledger is empty, the machine-level hooks are not installed; say so
   and point to `scripts/bootstrap --user-config`. Do not guess at session state.
2. If `npx` is available, run `npx ccusage@20.0.49 daily` for token and cost totals. If it is not, say the
   spend history was not checked.
3. Reply in under 15 lines:
   * flagged sessions first, one line each: lane, flag, the number behind it;
   * then one line of totals: sessions, tool calls, failures, subagents;
   * then spend, if step 2 ran.

What the flags mean:

| Flag | Meaning | Usual next step |
|---|---|---|
| `QUIET` | A turn is open and nothing has happened for the threshold | Look at the session; a long tool call looks the same as a stall |
| `failures` | Many tool calls failed | Read the session for a repeated failing command |
| `api-failure` | A turn ended on an API error (rate limit, overload, billing) | Check `/usage` and rate limits |
| `model-switch` | The model changed mid-session | Check for an unintended fallback; the prompt cache was lost |
| `denied` | A permission request was denied | Check whether the lane is blocked waiting |
| `compactions` | The context was compacted more than once | The task may be too large for one session |

Report what the ledger shows. It sees only sessions on this machine that ran with the hooks installed.
