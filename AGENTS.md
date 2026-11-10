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
| `scripts/ci` | Run every check: registry, shell, self-tests and `scripts/test-*`, lockfile, ruff, ty, pytest, workflow, secrets |
| `scripts/land` | Land the current branch on `main` once every check passes; no manual merge step. `--pr <n>` lands a pull request the dependency bot opened |
| `scripts/canon check` | Compare pins with live upstream; exit 3 if a pinned release tag was re-pointed, 4 if the lock is stale against the registry |
| `scripts/canon outdated` | Show upstreams that have a newer release tag than the one pinned |
| `scripts/canon resolve <id>` | Resolve one pin and merge it into the lock; without an id, re-resolve all (ask first) |
| `uv sync` | Install exactly what `uv.lock` records into `.venv` |
| `uv run --locked pytest` | Run every test in the workspace; `--locked` fails instead of rewriting `uv.lock` |
| `scripts/lanes` | Report every Claude Code session on this machine: state, failures, spend |
| `scripts/automation status` | Show every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | Print what a measured sample of repositories converges on for pull requests and GitHub automation |
| `scripts/bootstrap --check` | Show which toolchain pieces are installed, and whether the Claude Code CLI is behind its pin |
| `mise run paper:store`, `paper:check`, `paper:remove`, `paper:test` | Enter the Alpaca paper key pair into the login keychain (the owner, in their own terminal), check it is there, remove it, run the two paper tests with it (ADR 0029) |

## Layout

| Path | Holds |
|---|---|
| `canon/upstreams.json` | The registry: every upstream we may build on, with role, licence, evidence, ADR |
| `canon/upstreams.lock.json` | Resolved commit SHAs. Generated |
| `canon/models.json` | Model routing table and the landscape snapshot behind it |
| `canon/github-practice.json` | Measured pull request and automation practice of the repositories in `canon/github-practice.sample.json` |
| `docs/adr/` | Decisions in MADR 4.0.0 with an Evidence section and `review-by` date |
| `pyproject.toml`, `uv.lock` | uv workspace root and its single lockfile |
| `orca.yaml` | Worktree setup for the Orca cockpit: two keys (ADR 0018) |
| `src/canon_trading/` | Strategies and node configuration |
| `packages/canon-alpaca/` | The Alpaca adapter for NautilusTrader: a market-data client and a paper-only execution client; read its README first |
| `tests/`, `packages/*/tests/` | Tests, beside each package's `src/` |
| `catalog/` | Local market data (`ParquetDataCatalog`); ignored by git |
| `.claude/` | Claude Code settings, hooks, scoped rules, skills, subagents |
| `home/` | Machine-level Claude Code config installed to `~/.claude/` by the bootstrap |
| `scripts/` | `canon`, `ci`, `land`, `check-addresses`, `lanes`, `automation`, `bootstrap`, `survey-github`, and the test scripts `test-canon`, `test-ci`, `test-hooks`, `test-land`, `test-lanes` |
| `.githooks/` | The pre-commit gate and the pre-push address check |
| `.github/` | The pull request template, the Dependabot config, the CI workflow and the daily watch |

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
| 0010 | Strategy evaluation: every trial in a ledger, the pick's deflated Sharpe ratio at least 0.95, then paper |
| 0011 | Automation: scripts run free and often; a model runs weekly on what they flag |
| 0012 | Repository layout: a uv workspace with the adapter as its own package |
| 0013 | Quality gates: one script, pinned tools, the same checks here and in CI |
| 0014 | Landing workflow: a branch, one check, a squash merge, no manual merge step |
| 0015 | GitHub practice: what a measured sample converges on, and where this repo differs |
| 0016 | Dependency updates: Dependabot proposes, a session lands |
| 0017 | Merge gate: checks decide, an independent review comes first, a model never blocks |
| 0018 | Agent hosts and other runtimes: Orca is the cockpit, Claude Code the only runtime; one gate holds whatever runs |
| 0019 | Review gate scope: every change carries a review note except a named few |
| 0020 | Rule enforcement: no hook runs before a tool call; the merge gate and permission rules hold the rules |
| 0021 | Orca's skills: used from sessions inside Orca, within written limits |
| 0022 | Claude Code CLI: installed at the registry's pin and never below it |
| 0023 | Orca's agent-view plugin (proposed): on for now; what a plugin's hooks may approve on this machine |
| 0024 | Fill history for the Alpaca adapter (proposed): Alpaca's activities endpoint through alpaca-py's `get`, held until a paper test settles the trade ID |
| 0025 | Claude Code's sandbox: part of it adopted, subject to a trial that costs no lane a stop; nothing is on yet |
| 0026 | The weekly model run: a desktop scheduled task, gated by scripts, that reports and does not decide |
| 0027 | Overfitting probability (proposed): a diagnostic beside the Sharpe gate; no implementation adopted |
| 0028 | Permission prompts: a lane's own steps run without one; what rewrites everything or leaves the checkout still asks |
| 0029 | Alpaca paper keys: the login keychain, read by the `paper:test` mise task when it runs, checked against the paper API at entry |

## Automation

These run without being asked (ADR 0011, ADR 0013, ADR 0026). Every commit runs `scripts/ci` through
`.githooks/pre-commit`. Every push runs `scripts/check-addresses` through `.githooks/pre-push`, which
refuses an email address that is not a known service or example address in what the push would publish.
Every edit to a Python file runs ruff on it, and every edit under `canon/` or
`docs/adr/` runs the canon lint. Once a day a session-start hook compares the canon with upstream and the
next session start prints the result; a scheduled workflow does the same comparison on GitHub every day.
A background watchdog raises a macOS notification when a session
stalls or fails repeatedly. On Mondays a scheduled session runs the `landscape-refresh` skill. Dependabot
opens at most two grouped pull requests a week, for the indirect packages in `uv.lock` and for the
workflow's actions; the session brief lists the open ones (ADR 0016).

No hook runs before a tool call (ADR 0020). The rules are held where they cannot be skipped: GitHub merges
nothing that has not passed `scripts/ci` and does not carry the `signoff` status `scripts/land` posts,
`scripts/ci` fails when `uv.lock` is out of date, and live credentials never enter this repo or an agent's
context. `.claude/settings.json` pre-approves a short list of commands, among them `scripts/land`,
`scripts/canon resolve <id>` for one pin and `uv add name==version` pinned exactly, so a lane following a
skill is not stopped. What rewrites everything or reaches outside the checkout asks the owner first:
`scripts/canon resolve` with no id, every `uv sync` (a lane syncs with `uv run --locked`), `uv add` with a
range, a URL or an upgrade flag, the installers, and a direct `git push` (ADR 0028). If a prompt or a check
stops you, do not look for another route: say what you wanted to do and why.

This is the same for every runtime, Claude Code or not: use no credentials, and leave landing to
`scripts/land` (ADR 0018).

## Orca

Orca is the cockpit (ADR 0018). From a session Orca started, its `orca-cli` and `orchestration` skills may
be used within these limits (ADR 0021):

* Workers and handoffs use `--agent claude`. No other runtime is adopted.
* A worker that changes files gets its own workspace (`--worktree new-top-level`). Share yours only for
  work that reads.
* Do not run through an Orca terminal what a prompt would have asked the owner, and do not answer another
  agent's permission prompt. A worker's change lands like any other: reviewed first, then `scripts/land`.
* Research, record checks and code review stay subagents. Use an Orca worker when the work needs its own
  workspace or the owner asks to watch it, on the model `canon/models.json` gives that work (`--model`).
* Not used: remote placement, the browser and desktop control, plugins, automations, session search, and
  Orca's other bundled skills. Orca loads one plugin of its own into each session, `orca-agent-view`
  (ADR 0023); a session installs, loads or enables none.
* Do not start Orca from a session (`orca open`): the app would inherit that session's environment.

## Landing

The owner's standing instruction (2026-11-09): work lands automatically once its checks pass. Do not ask
"merge into main?" (ADR 0014).

1. One branch per change. A session that runs in parallel with another works in its own git worktree;
   two sessions never write in one checkout.
2. Commit, then run `scripts/land`. It runs `scripts/ci` on what is committed, on a branch that contains
   the current `main`, and lands the branch. With an `origin` remote, `gh` must be signed in, or it stops
   and lands nothing; it checks every address it would publish, pushes, opens a pull request, posts the
   `review` and `signoff` statuses, and waits up to nine minutes (`LAND_WAIT_SECONDS`, default 540) for
   GitHub to squash-merge exactly the checked commit with the checked subject and message. If auto-merge
   is on when the wait runs out, it stops and GitHub merges once the checks pass. With no `origin` remote
   it fast-forwards the local `main`. The same checks run on this machine and on GitHub's runners
   (ADR 0014).
3. Every change needs an independent review before it lands, unless every path it changes is
   `README.md`, `packages/*/README.md`, `catalog/README.md`, an edit to an existing
   `docs/adr/NNNN-*.md` or an edit to `uv.lock`; a new or renamed record needs one. `/code-review` reads
   code, the `evidence-verifier` reads a record. Resolve what it finds, then land with
   `--reviewed "<what ran and what came of it>"`; `scripts/land` refuses such a change without it and
   records the note on the commit as the `review` status. With no `origin` remote the note is printed and
   not recorded anywhere (ADR 0017, ADR 0019).
4. Pull request text has `Summary`, `Test Plan` and `Evidence`, written for a later reader: no conversation
   history, discarded attempts or tool usage. Titles are a plain sentence, with no `feat:` style prefix.
5. This covers work a session of the owner's produced. It does not cover creating a remote, changing a
   repository's visibility, or anything a permission rule asks about; those still go to the owner.
6. It also covers the dependency bot's pull requests (ADR 0016). When the session brief lists any, land
   them with `scripts/land --pr <n>`, which refuses a pull request from a fork, checks the pull request
   out in its own worktree, merges `main` into it if it is behind, lands it under its own title and
   description, and removes the worktree and branch. An edit to `uv.lock` alone lands on passing checks, with no note. One that moves a
   workflow action fails the canon lint until its registry entry moves on the same branch, through
   `adopt-upstream`, and then lands with a `--reviewed` note like any other workflow change.

## Reporting

End a task by saying what you verified and how, what you could not verify, and what is left.
