# Canon

A harness repo for building on Alpaca and NautilusTrader with coding agents. Nothing here is invented: every
dependency, pattern and practice traces to a pinned upstream or a dated source. The decisions are in
`docs/adr/`. They outrank this summary, the owner's recollection, and yours.

## Rules

1. **Evidence over memory.** Your training data is older than this repo's pins. Before using an upstream API,
   config key, model name or CLI flag, read the pinned source or its current docs. Docs for agents:
   `https://nautilustrader.io/docs/llms.txt`, `https://docs.alpaca.markets/llms.txt`,
   `https://code.claude.com/docs/llms.txt`.
2. **Adopt through the canon.** Anything not already in `canon/upstreams.json` (library, tool, skill, plugin,
   practice) goes through the `adopt-upstream` skill before it is installed, imported, enabled or copied.
3. **Glue yes, invention no.** Code here calls an upstream API as documented or implements a contract an
   upstream documents, and its header names that contract. If no upstream covers a need, write an ADR with
   status `proposed` stating the gap, and stop.
4. **Label uncertainty.** Anything not checked this session against a source is `UNVERIFIED`. Never present a
   guess as a finding.
5. **No silent fallback.** If a check, fetch, install or test cannot run, stop and report it. Do not substitute
   something else and carry on.
6. **Pins move deliberately.** Only `scripts/canon resolve` writes the lock, after reading the upstream
   changelog. Never edit `canon/upstreams.lock.json` by hand.
7. **Decisions expire.** An ADR past its `review-by` date is re-verified before you rely on it.
8. **Paper trading only.** Agents never place, modify or cancel live orders, and live credentials never enter
   this repo or an agent's context (ADR 0009).
9. **One model per conversation.** Switching model mid-session discards the prompt cache; delegate bulky or
   cheap work to a subagent instead. Changing effort is fine (ADR 0005, ADR 0006).

## Commands

| Command | Does |
|---|---|
| `scripts/canon list` | Show every upstream, its role and pinned commit |
| `scripts/canon lint` | Check registry, lock and ADRs for required evidence (offline) |
| `scripts/ci` | Run every check: registry, shell, hooks, lockfile, ruff, ty, pytest, workflow, secrets |
| `scripts/land` | Land the current branch on `main` once every check passes; no manual merge step. `--pr <n>` lands a pull request the dependency bot opened |
| `scripts/canon check` | Compare pins with live upstream; exit 3 if a pinned release tag was re-pointed |
| `scripts/canon outdated` | Show upstreams that have a newer release tag than the one pinned |
| `scripts/canon resolve <id>` | Resolve one pin and merge it into the lock; without an id, re-resolve all (ask first) |
| `uv sync` | Install exactly what `uv.lock` records into `.venv` |
| `uv run pytest` | Run every test in the workspace |
| `scripts/lanes` | Report every Claude Code session on this machine: state, failures, spend |
| `scripts/automation status` | Show every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | Print what a measured sample of repositories converges on for pull requests and GitHub automation |
| `scripts/bootstrap --check` | Show which toolchain pieces are installed |

## Layout

| Path | Holds |
|---|---|
| `canon/upstreams.json` | The registry: every upstream we may build on, with role, licence, evidence, ADR |
| `canon/upstreams.lock.json` | Resolved commit SHAs. Generated |
| `canon/models.json` | Model routing table and the landscape snapshot behind it |
| `canon/github-practice.json` | Measured pull request and automation practice of the repositories in `canon/github-practice.sample.json` |
| `docs/adr/` | Decisions in MADR 4.0.0 with an Evidence section and `review-by` date |
| `pyproject.toml`, `uv.lock` | uv workspace root and its single lockfile |
| `src/canon_trading/` | Strategies and node configuration |
| `packages/canon-alpaca/` | The Alpaca adapter for NautilusTrader (not started); read its README first |
| `tests/`, `packages/*/tests/` | Tests, beside each package's `src/` |
| `catalog/` | Local market data (`ParquetDataCatalog`); ignored by git |
| `.claude/` | Claude Code settings, hooks, scoped rules, skills, subagents |
| `home/` | Machine-level Claude Code config installed to `~/.claude/` by the bootstrap |
| `scripts/` | `canon`, `ci`, `land`, `test-hooks`, `lanes`, `automation`, `bootstrap`, `survey-github` |
| `.githooks/` | The pre-commit gate |
| `.github/` | The pull request template, the Dependabot config and the CI workflow |

## Decisions

| ADR | Decision |
|---|---|
| 0001 | Canon-first: pinned, evidenced upstreams are the source of truth |
| 0002 | ADRs are MADR 4.0.0 plus Evidence and `review-by` |
| 0003 | Which harness layer holds which kind of instruction |
| 0004 | Third-party skills and plugins: pinned, read, then enabled |
| 0005 | Model routing by lane |
| 0006 | Token efficiency: lever order |
| 0007 | Observability: built-ins, local event ledger, OpenTelemetry later |
| 0008 | Toolchain: Command Line Tools, mise, uv, native Claude Code |
| 0009 | Trading foundation: NautilusTrader, alpaca-py, adapter per upstream guide |
| 0010 | Strategy evaluation (proposed): variant comparison is an open gap |
| 0011 | Automation: scripts run free and often; a model runs weekly on what they flag |
| 0012 | Repository layout: a uv workspace with the adapter as its own package |
| 0013 | Quality gates: one script, pinned tools, the same checks here and in CI |
| 0014 | Landing workflow: a branch, one check, a squash merge, no manual merge step |
| 0015 | GitHub practice: what a measured sample converges on, and where this repo differs |
| 0016 | Dependency updates: Dependabot proposes, a session lands |

## Automation

These run without being asked (ADR 0011, ADR 0013). Every commit runs `scripts/ci` through
`.githooks/pre-commit`. Every edit to a Python file runs ruff on it, and every edit under `canon/` or
`docs/adr/` runs the canon lint. Once a day a session-start hook compares the canon with upstream and the
next session start prints the result. A background watchdog raises a macOS notification when a session
stalls or fails repeatedly. On Mondays a scheduled session runs the `landscape-refresh` skill. Dependabot
opens at most two grouped pull requests a week, for the indirect packages in `uv.lock` and for the
workflow's actions; the session brief lists the open ones (ADR 0016).

`.claude/hooks/guard.sh` checks every tool call before it runs. For every session it refuses a commit that
skips the gate, an install that bypasses `uv.lock`, and anything that points at live trading. For an
unattended session it refuses everything except reading. If the guard refuses a call, do not look for
another route: say what you wanted to do and why.

## Landing

The owner's standing instruction (2026-11-09): work lands automatically once its checks pass. Do not ask
"merge into main?" (ADR 0014).

1. One branch per change. A session that runs in parallel with another works in its own git worktree;
   two sessions never write in one checkout.
2. Commit, then run `scripts/land`. It runs `scripts/ci` on what is committed and lands the branch: on
   GitHub, a pull request that it signs off, waits for the hosted checks on and squash-merges; with no
   remote, a fast-forward of `main`. The same checks run on this machine and on GitHub's runners (ADR 0014).
3. For a change under `src/` or `packages/`, run `/code-review` first and resolve what it finds.
4. Pull request text has `Summary`, `Test Plan` and `Evidence`, written for a later reader: no conversation
   history, discarded attempts or tool usage. Titles are a plain sentence, with no `feat:` style prefix.
5. This covers work a session of the owner's produced. It does not cover creating a remote, changing a
   repository's visibility, or anything the guard refuses; those still go to the owner.
6. It also covers the dependency bot's pull requests (ADR 0016). When the session brief lists any, land
   them with `scripts/land --pr <n>`, which checks the pull request out in its own worktree and merges
   `main` into it if it is behind. A change to `uv.lock` alone lands on passing checks. One that moves a
   workflow action fails the canon lint until its registry entry moves on the same branch, through
   `adopt-upstream`.

## Reporting

End a task by saying what you verified and how, what you could not verify, and what is left.
