#!/bin/bash
# Claude Code PreToolUse hook for every tool in this repo.
# Contract: https://code.claude.com/docs/en/hooks.md — exit 2 blocks the tool call and shows stderr to Claude.
# Hooks run in every permission mode, including bypass, so these rules hold when nothing else asks.
#
# Part 1, every session (ADR 0009, ADR 0011):
#   - no commit that skips the commit gate
#   - no install that bypasses uv.lock
#   - no call to Alpaca's live trading or production broker hosts (names read from pinned alpaca-py)
#   - no Python that opts out of paper trading
# Part 2, unattended sessions (marked by unattended-mark.sh): read-only by default-deny.
#
# Fails closed: if this hook cannot evaluate a call, the call is blocked.
input=$(cat)
deny() { echo "guard: blocked. $1" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || deny "jq is not installed, so tool calls in this repo cannot be checked."

root="${CLAUDE_PROJECT_DIR:-$(jq -r '.cwd // empty' <<<"$input")}"
tool=$(jq -r '.tool_name // empty' <<<"$input") || deny "the hook input was not valid JSON."
sid=$(jq -r '.session_id // empty' <<<"$input")

# alpaca-py 0.45.0, alpaca.common.enums.BaseURL: TRADING_LIVE, TRADING_STREAM_LIVE, BROKER_PRODUCTION.
# The leading // keeps paper-api.alpaca.markets and broker-api.sandbox.alpaca.markets allowed.
LIVE_HOSTS='//(api|broker-api)\.alpaca\.markets'
# Alpaca's CLI and MCP server switch to live through these variables (alpaca-skills at the pinned commit:
# paper-trading-cli/SKILL.md, paper-trading-mcp/SKILL.md). Matches shell, JSON and YAML spellings.
LIVE_ENV='ALPACA_LIVE_TRADE["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?(true|1|yes)|ALPACA_PAPER_TRADE["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?(false|0|no)'

case "$tool" in
  Bash)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
    # Broad on purpose: any git invocation with the word "commit" plus a skip flag. It can refuse an
    # innocent "git log --grep commit -n 5"; that is the safe direction to be wrong in.
    if grep -qE '(^|[;&|[:space:]])git[^|;&]*[[:space:]]commit([[:space:]]|$)' <<<"$cmd" \
       && grep -qE '(^|[[:space:]])(--no-verify|-n)([[:space:]]|$)' <<<"$cmd"; then
      deny "Commits go through .githooks/pre-commit; do not pass --no-verify or -n. Fix what the gate reports."
    fi
    if grep -qE '(^|[;&|[:space:]])(uv[[:space:]]+)?pip3?[[:space:]]+install([[:space:]]|$)' <<<"$cmd"; then
      deny "Dependencies come from uv.lock. Use 'uv add <pkg>==<version>' after the adopt-upstream skill, or 'uv sync --locked'."
    fi
    if grep -qE "$LIVE_HOSTS" <<<"$cmd"; then
      deny "That command names Alpaca's live trading or production broker host. This repo is paper-only (ADR 0009)."
    fi
    if grep -qiE "$LIVE_ENV" <<<"$cmd"; then
      deny "That command switches Alpaca's CLI or MCP server to live trading. This repo is paper-only (ADR 0009)."
    fi
    ;;
  WebFetch)
    url=$(jq -r '.tool_input.url // empty' <<<"$input")
    grep -qE "$LIVE_HOSTS" <<<"$url" && deny "That URL is Alpaca's live trading or production broker host. This repo is paper-only (ADR 0009)."
    ;;
  Edit|Write|NotebookEdit)
    path=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$input")
    new=$(jq -r '[.tool_input.content?, .tool_input.new_string?, .tool_input.new_source?] | map(select(. != null)) | join("\n")' <<<"$input")
    case "$path" in
      *.md) ;;                                  # documentation may name the switches in order to forbid them
      *) grep -qiE "$LIVE_ENV" <<<"$new" && deny "That change switches Alpaca's CLI or MCP server to live trading. This repo is paper-only (ADR 0009)." ;;
    esac
    case "$path" in
      *.py|*.ipynb)
        if grep -qE 'paper[[:space:]]*=[[:space:]]*False|url_override[[:space:]]*=|TRADING_LIVE|TRADING_STREAM_LIVE|BROKER_PRODUCTION' <<<"$new" \
           || grep -qE "$LIVE_HOSTS" <<<"$new"; then
          deny "That change would point Python code at live trading (paper=False, url_override, a live BaseURL or a live host). This repo is paper-only (ADR 0009)."
        fi
        ;;
    esac
    ;;
esac

# ---- Part 2: unattended sessions are read-only -------------------------------------------------------
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac          # no usable session id: nothing to look up
[ -e "$root/.canon/unattended/$sid" ] || exit 0

UNATTENDED="This is an unattended run, which is read-only (ADR 0011). Report what you wanted to do instead."
case "$tool" in
  Read|Grep|Glob|WebSearch|Skill|Agent|Task|ToolSearch|TodoWrite|mcp__ccd_turn__*) exit 0 ;;
  WebFetch)
    host=$(jq -r '.tool_input.url // empty' <<<"$input" | sed -E 's|^[a-zA-Z]+://||; s|[/:?#].*$||' | tr '[:upper:]' '[:lower:]')
    case "$host" in
      github.com|raw.githubusercontent.com|nautilustrader.io|docs.alpaca.markets|code.claude.com|platform.claude.com|artificialanalysis.ai|pypi.org|docs.astral.sh|mise.jdx.dev|docs.pytest.org|www.anthropic.com) exit 0 ;;
      *) deny "$UNATTENDED Fetching '$host' is outside the allowed documentation hosts." ;;
    esac
    ;;
  Bash)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
    # one simple command, or a pipeline of them: no chaining, substitution, redirection or newlines
    if grep -qE '[;&`<>]|\$\(' <<<"$cmd" || [ "$(printf '%s' "$cmd" | wc -l | tr -d ' ')" != "0" ]; then
      deny "$UNATTENDED Run one plain command at a time: no ';', '&&', redirection or substitution."
    fi
    # Each git subcommand is listed with the only forms that cannot change or run anything. `git branch`
    # takes listing flags only (a bare name would create a branch, -D would delete one). `rg` is left out
    # because `rg --pre` runs a program. A second reader found those holes on 2026-11-09 (ADR 0011).
    ok='^(\./)?scripts/canon (check|lint|list|selftest|outdated( --json)?)$'
    ok="$ok"'|^(\./)?scripts/lanes( --(json|selftest|days [0-9]+|quiet-min [0-9]+))*$'
    ok="$ok"'|^(\./)?scripts/automation status$'
    ok="$ok"'|^git rev-parse( [^|]*)?$'
    ok="$ok"'|^git status( (--short|--porcelain|-s|-sb))*$'
    ok="$ok"'|^git branch( (--show-current|--list|-a|-r|-v|-vv))*$'
    ok="$ok"'|^git (log|show|diff)( [^|]*)?$'
    ok="$ok"'|^(ls|cat|head|tail|grep|jq|wc|date)( [^|]*)?$'
    allowed="scripts/canon check|outdated|lint|list|selftest, scripts/lanes, scripts/automation status, git rev-parse, git status, git branch (listing flags only), git log|show|diff, ls, cat, head, tail, grep, jq, wc, date"
    while IFS= read -r seg; do
      seg=$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$seg")
      grep -qE -- '--output|--ext-diff|--textconv|--exec|--pre([ =]|-glob|$)' <<<"$seg" \
        && deny "$UNATTENDED That flag writes a file or runs a program."
      grep -qE "$ok" <<<"$seg" || deny "$UNATTENDED Allowed commands: $allowed."
    done < <(tr '|' '\n' <<<"$cmd")
    exit 0
    ;;
  *) deny "$UNATTENDED The tool '$tool' can change things." ;;
esac
