# Canon

A harness repo for building on Alpaca and NautilusTrader with Claude Code, under one rule: **nothing is
invented here.** Every dependency, pattern and practice traces to an upstream pinned by commit SHA or to a
dated source, and every decision is an ADR that carries its evidence and an expiry date.

"The canon" is the registry of upstreams this repo is allowed to build on. The practice is *canon-first*:
find it in the canon, or put it there with evidence, before you use it.

## Start here

```bash
scripts/bootstrap --check
```

That shows what is installed. On a new Mac, install Apple's Command Line Tools first
(`xcode-select --install`), then:

```bash
scripts/bootstrap --install --with-user-config
```

It installs mise, the tools in `mise.toml`, the Claude Code CLI, and the machine-level config in `home/`.
It uses no `sudo`. It never overwrites `settings.json` or `CLAUDE.md` in `~/.claude`: a differing one gets a
`.canon-proposed` copy beside it. It replaces the ledger and status-line scripts and keeps a differing
previous copy of each.

## Everyday commands

| Command | Does |
|---|---|
| `scripts/canon list` | Every upstream, its role and pinned commit |
| `scripts/ci` | Every check in one run: registry, shell, self-tests and `scripts/test-*`, lockfile, ruff, ty, pytest, workflow, secrets |
| `scripts/land` | Land the current branch on `main` once every check passes |
| `scripts/canon lint` | Registry, lock and ADRs have the required evidence (offline) |
| `scripts/canon check` | Compare pins with live upstream; warns if a release tag was re-pointed |
| `scripts/canon outdated` | Upstreams with a newer release than the one pinned |
| `scripts/canon resolve` | Re-resolve pins and rewrite the lock |
| `scripts/lanes` | State of every Claude Code session on this machine |
| `scripts/lanes --selftest` | Prove the lane report's flags fire |
| `scripts/automation status` | Every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | What a measured sample of repositories converges on for pull requests and GitHub automation |

## What runs on its own

| Layer | Runs | Costs tokens |
|---|---|---|
| Commit gate (`scripts/ci`) | Every `git commit` | No |
| Address check (`scripts/check-addresses`) | Every `git push` | No |
| Hosted CI (`scripts/ci` on GitHub) | Every pull request and push to `main` | No |
| Edit checks (canon lint, ruff) | Every matching edit by an agent | No |
| Hosted watch (`canon check` on GitHub) | Once a day | No |
| Landscape watch | Once a day, at session start | No |
| Lane watchdog | Every 10 minutes while the Mac is awake | No |
| Weekly refresh | Weekly, when the desktop app is open | One session, short when nothing changed |
| Dependency bot (Dependabot) | Weekly, on GitHub | Only when a session lands its pull request |

`scripts/automation install` turns on the commit gate, the address check and the lane watchdog. The edit
checks and the landscape watch are hooks in `.claude/settings.json`. The weekly refresh is a
scheduled task in the Claude desktop app, and the dependency bot is `.github/dependabot.yml`, which GitHub
runs.
`scripts/automation uninstall` reverses the install. The reasoning is in [ADR 0011](docs/adr/0011-automation.md).

`scripts/canon` and `scripts/lanes` need only `curl` and `jq`, which ship with macOS.

## What is where

| Path | Holds |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | Instructions for agents; `CLAUDE.md` imports `AGENTS.md` |
| `canon/upstreams.json` | The registry: 33 upstreams with role, licence, evidence, ADR |
| `canon/upstreams.lock.json` | Resolved commit SHAs (generated) |
| `canon/models.json` | Model routing by lane and the landscape snapshot behind it |
| `docs/adr/` | Nineteen decisions in MADR 4.0.0, each with Evidence and `review-by` |
| `pyproject.toml`, `uv.lock` | uv workspace root; one lockfile for every package |
| `src/canon_trading/` | Strategies and node configuration |
| `packages/canon-alpaca/` | The Alpaca adapter for NautilusTrader: a market-data client, no execution client yet |
| `tests/`, `packages/*/tests/` | Tests beside each package's `src/` |
| `catalog/` | Local market data; ignored by git |
| `.claude/settings.json` | Permissions and hooks for this repo |
| `.claude/rules/` | Rules scoped to `canon/`, `docs/adr/` and trading code |
| `.claude/skills/` | `adopt-upstream`, `new-adr`, `landscape-refresh`, `lane-report` |
| `.claude/agents/` | `upstream-scout` (Sonnet), `evidence-verifier` (Opus) |
| `home/` | Machine-level config: event ledger hook, status line, lane watchdog, user `CLAUDE.md` |
| `mise.toml` | Pinned toolchain |

## How a decision gets made

1. A scout researches the field and returns findings with URLs.
2. The upstream goes in `canon/upstreams.json`; `scripts/canon resolve` pins it to a commit.
3. An ADR records the decision, the options, and each source with how it was checked.
4. A verifier re-reads the sources. `scripts/canon lint` enforces the structure.
5. On its `review-by` date the ADR is re-verified. `landscape-refresh` does this weekly.

When sources disagree, precedence is: pinned upstream code and docs, vendor primary docs, results measured
here, community convergence, then anyone's recollection.

## What is not here yet

* **No execution.** NautilusTrader has no official Alpaca adapter; ADR 0009 decides to build one to
  upstream's guide. The adapter's data client exists (US equities, historical bars, streamed quotes and
  trades, from Alpaca's paper data), with offline tests; its paper integration test has not run yet, and
  there is no execution client. See `packages/canon-alpaca/README.md`.
* **No accepted method for comparing strategy variants** (ADR 0010 is `proposed`).
* **No OpenTelemetry backend.** The local event ledger covers this machine; `home/otel-settings.example.json`
  is ready for when a collector exists.
* **No eval of our own.** Model routing rests on external measurements until there is one (ADR 0005).
