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
| `scripts/canon check` | Compare pins with live upstream; warns if a release tag was re-pointed or the lock is stale |
| `scripts/canon outdated` | Upstreams with a newer release than the one pinned |
| `scripts/canon resolve` | Re-resolve pins and rewrite the lock |
| `scripts/lanes` | State of every Claude Code session on this machine |
| `scripts/lanes --selftest` | Prove the lane report's flags fire |
| `scripts/automation status` | Every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | What a measured sample of repositories converges on for pull requests and GitHub automation |

## Run the first backtest

`src/canon_trading/` holds one strategy and one way to run it. `MaCross` (`ma_cross.py`) holds a position
while a fast moving average of bar closes is above a slow one. It is there to move bars, orders, fills and
an account through NautilusTrader, not as a trading idea. `run_backtest` (`backtest.py`) runs strategies
over their bars in a `ParquetDataCatalog` on a simulated Alpaca venue and returns the engine's own results.

It needs no network, no credentials and no downloaded data:

```bash
uv run --locked pytest tests/test_backtest.py
```

That test generates 40 daily bars, writes them and their instrument to a temporary catalog, runs the
strategy through a real `BacktestNode`, and checks what came out: every bar delivered, four orders filled
at known closes, two round trips, and 100,000 USD ending as 100,090. The arithmetic is worked by hand at
the top of the test file.

To run over a catalog of your own, it must hold the bars and the definition of their instrument, written
as the test writes them (`canon_alpaca.providers.parse_equity`, `canon_alpaca.data.parse_bar`, then
`write_instruments` and `write_bars`). Nothing in this repo fills `catalog/` from Alpaca yet.

```python
from pathlib import Path

from canon_trading.backtest import run_backtest
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity

bar_type = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
strategy = MaCross(MaCrossConfig(bar_type=bar_type, trade_size=Quantity.from_int(10)))
run = run_backtest(Path("catalog"), [strategy], starting_balances=["100_000 USD"])

print(run.result)
print(len(run.orders), "orders;", list(run.account.balances_total().values()))
```

`run.result` is NautilusTrader's `BacktestResult`; `run.orders`, `run.positions`,
`run.position_snapshots` and `run.account` are the objects in the engine's cache. The simulated venue's
settings and what it leaves out (slippage, latency, market hours, margin) are listed at the top of
`backtest.py`. A result here shows that the plumbing works. It may be reported, but not ranked against
another until [ADR 0010](docs/adr/0010-strategy-evaluation.md) is accepted.

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
| `canon/upstreams.json` | The registry: 37 upstreams with role, licence, evidence, ADR |
| `canon/upstreams.lock.json` | Resolved commit SHAs (generated) |
| `canon/models.json` | Model routing by lane and the landscape snapshot behind it |
| `docs/adr/` | Twenty-three decisions in MADR 4.0.0, each with Evidence and `review-by` |
| `pyproject.toml`, `uv.lock` | uv workspace root; one lockfile for every package |
| `src/canon_trading/` | Strategies and node configuration |
| `packages/canon-alpaca/` | The Alpaca adapter for NautilusTrader: a market-data client and a paper-only execution client |
| `tests/`, `packages/*/tests/` | Tests beside each package's `src/` |
| `catalog/` | Local market data; ignored by git |
| `.claude/settings.json` | Permissions and hooks for this repo |
| `orca.yaml` | The setup command Orca runs in a new worktree, once enabled and approved in the app |
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

* **Nothing proven against Alpaca.** NautilusTrader has no official Alpaca adapter; ADR 0009 decides to
  build one to upstream's guide. The adapter has a data client (US equities, historical bars, streamed
  quotes and trades, from Alpaca's paper data) and a first, paper-only execution client (market and limit
  orders, cancels, fills from trade updates). Both pass their offline tests; neither of the two paper
  tests has run yet, and modify, cancel-all, stop orders and fill history at start-up are not written.
  See `packages/canon-alpaca/README.md`.
* **No market data in `catalog/`.** The first backtest runs on bars generated in its test. Nothing here
  writes Alpaca's bars to the catalog yet.
* **No accepted method for comparing strategy variants** (ADR 0010 is `proposed`).
* **No OpenTelemetry backend.** The local event ledger covers this machine; `home/otel-settings.example.json`
  is ready for when a collector exists.
* **No eval of our own.** Model routing rests on external measurements until there is one (ADR 0005).
