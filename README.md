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
`ema_cross_paper_node` (`paper_node.py`) builds a live node on Alpaca's paper environment that runs
upstream's built-in `EmaCross` under the risk engine's native limits; the paper test runs it for one order
([ADR 0037](docs/adr/0037-ema-cross-paper-run-under-native-risk-limits.md)).

It needs no network, no credentials and no downloaded data:

```bash
uv run --locked pytest tests/test_backtest.py
```

That test generates 40 daily bars, writes them and their instrument to a temporary catalog, runs the
strategy through a real `BacktestNode`, and checks what came out: every bar delivered, four orders filled
at known closes, two round trips, and 100,000 USD ending as 100,090. The arithmetic is worked by hand at
the top of the test file.

To run over Alpaca's bars, load them into `catalog/` first with
`canon_trading.bars_to_catalog.load_bars(bar_type, start, end)`: it requests the bars through the adapter's
data client with NautilusTrader's catalog write-back, writes the instrument's definition, and fetches only
what the catalog does not already cover (ADR 0040). It needs the paper key pair, so it runs inside
`mise run paper:test` (`mise run paper:test -k bars_to_catalog` for that test alone);
`tests/test_paper_bars_to_catalog.py` loads AAPL's daily bars for the first quarter
of 2025 that way and runs the backtest below on them.

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
`backtest.py`. A result here shows that the plumbing works. Variants are ranked only by the protocol in
[ADR 0010](docs/adr/0010-strategy-evaluation.md): `canon_trading.ledger.record_trial` runs a backtest as a
trial of an Optuna study in `ledger/trials.db` ([ADR 0035](docs/adr/0035-trial-ledger-in-an-optuna-study.md)),
and `canon_trading.ranking.rank` ranks a study's trials and tests the pick's deflated Sharpe ratio.

## Enter the Alpaca paper keys

The paper tests in `packages/canon-alpaca/tests/` (`test_paper_*.py`) run against Alpaca's paper environment and are
skipped until a paper key pair is in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. On a Mac, enter the pair
once, from this checkout, in your own terminal:

```bash
mise run paper:store    # asks for the key ID and the secret at a hidden prompt; stores only a paper pair
mise run paper:check    # says whether both keychain items exist; prints neither value
mise run paper:remove   # deletes both items
mise run paper:test     # runs the paper tests with the pair, one session at a time (they place DAY
                        # limit buys of 1 AAPL far below the market and cancel each one, and let
                        # EmaCross trade 1 SPY, then close it)
```

`paper:store` refuses a key ID starting with `AK`, Alpaca's hint for a live key, then sends the pair to
Alpaca's paper API (`GET /v2/account`) and stores nothing unless it is accepted there; a live pair is
refused there. Each value then sits in the login keychain, service `canon-alpaca-paper`. Of this repo's
commands, only `paper:test` puts it in an environment, its own, when it starts (`paper:store` reads it
back once to confirm the write); no file, shell, `mise env` or `scripts/ci` gets it. The keychain does not stop other
programs you run from reading it: any process of yours can, with `security find-generic-password -w`, and
no dialog appears. To replace the pair, remove it and store it again.
Why it works this way, and what the keychain does not protect against:
[ADR 0029](docs/adr/0029-paper-keys-in-the-login-keychain.md); how the pair is checked before it is stored:
[ADR 0033](docs/adr/0033-paper-key-entry-checked-against-the-paper-api.md).

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
`scripts/automation uninstall` reverses the install. The reasoning is in [ADR 0011](docs/adr/0011-automation.md) and
[ADR 0026](docs/adr/0026-weekly-model-run.md).

`scripts/canon` and `scripts/lanes` need only `curl` and `jq`, which ship with macOS.

## What is where

| Path | Holds |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | Instructions for agents; `CLAUDE.md` imports `AGENTS.md` |
| `canon/upstreams.json` | The registry: 41 upstreams with role, licence, evidence, ADR |
| `canon/upstreams.lock.json` | Resolved commit SHAs (generated) |
| `canon/models.json` | Model routing by lane and the landscape snapshot behind it |
| `docs/adr/` | The decisions, one record each, in MADR 4.0.0 with Evidence and `review-by` |
| `docs/architecture.md`, `docs/roadmap.md` | How the pieces fit together on `main`, and the steps planned next |
| `pyproject.toml`, `uv.lock` | uv workspace root; one lockfile for every package |
| `src/canon_trading/` | Strategies and node configuration |
| `packages/canon-alpaca/` | The Alpaca adapter for NautilusTrader: a market-data client and a paper-only execution client |
| `tests/`, `packages/*/tests/` | Tests beside each package's `src/` |
| `catalog/` | Local market data, loaded by `canon_trading.bars_to_catalog`; ignored by git |
| `ledger/` | The trial ledger, an Optuna SQLite study; ignored by git |
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

* **Little proven against Alpaca.** NautilusTrader has no official Alpaca adapter; ADR 0009 decides to
  build one to upstream's guide. The adapter has a data client (US equities, historical bars, streamed
  quotes and trades, from Alpaca's paper data) and a first, paper-only execution client (market, limit,
  stop and stop-limit orders, modify, cancels and cancel-all, extended hours for limit orders, fills from
  trade updates). Both pass their offline
  tests, and the two paper tests that existed then passed on 2026-11-10: daily bars through a real node,
  and a limit order accepted and canceled (ADR 0030); the order-management paper tests are ADR 0036's.
  Another showed that Alpaca's fill activities carry the trade updates' `execution_id`s (ADR 0024).
  Upstream's data and execution testers then ran through the adapter on paper: quotes, trades, bars,
  market and limit orders, fills, cancels and a rejection passed (see the package README). The full asset
  list has not run against Alpaca, and fill history at start-up is not written.
  See `packages/canon-alpaca/README.md`.
* **Little market data in `catalog/`.** `load_bars` writes Alpaca's bars there (ADR 0040), and the only
  range anything here loads is the paper test's: AAPL's daily bars for the first quarter of 2025. A fresh
  clone has none until that test runs. Bars are raw and from the IEX feed, the only feed and adjustment
  the adapter requests.
* **No ranking of real strategies yet.** The ledger (ADR 0035), the returns matrix and the gate (ADR 0010,
  ADR 0031) are written and tested on generated bars and series. A trial that never traded, or a
  configuration that never runs to the end, stops a ranking: what value it should count as is undecided.
  The overfitting probability has no adopted implementation (ADR 0027 and ADR 0032 are `proposed`).
* **No OpenTelemetry backend.** The local event ledger covers this machine; `home/otel-settings.example.json`
  is ready for when a collector exists.
* **No eval of our own.** Model routing rests on external measurements until there is one (ADR 0005).
