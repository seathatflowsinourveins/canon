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
| `scripts/lanes --rates` | Every running worker of every runtime, with output tokens per second or why not measured |
| `scripts/automation status` | Every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | What a measured sample of repositories converges on for pull requests and GitHub automation |

## Run the first backtest

`src/canon_trading/` holds one strategy and one way to run it. `MaCross` (`ma_cross.py`) holds a position
while a fast moving average of bar closes is above a slow one. It is there to move bars, orders, fills and
an account through NautilusTrader, not as a trading idea. `run_backtest` (`backtest.py`) runs strategies
over their bars in a `ParquetDataCatalog` on a simulated Alpaca venue and returns the engine's own results.
`ema_cross_paper_node` (`paper_node.py`) builds a live node on Alpaca's paper environment that runs
upstream's built-in `EmaCross` under the risk engine's native limits; the paper test runs it for one order
([ADR 0037](docs/adr/0037-ema-cross-paper-run-under-native-risk-limits.md); the limits are
[ADR 0049](docs/adr/0049-paper-node-risk-limits-within-alpacas-rate-limit.md)). `SessionMaCross`
(`session_ma_cross.py`) is `MaCross` guarded for live use, and `session_window.py` runs it, or with
`--strategy EmaCross` upstream's `EmaCross` in the regular session only, for one bounded window on paper
with a one-share cap inside the node
([ADR 0044](docs/adr/0044-paper-session-windows-for-macross.md)). The cap stops the node and sends no order:
in a window `SessionMaCross` exits at market on stop and the window then closes what is left, while
upstream's `EmaCross` does not flatten on stop, so its caller closes the position.

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
[ADR 0010](docs/adr/0010-strategy-evaluation.md): `canon_trading.ledger.create_ledger_study` creates a study
over a fixed sample in the main checkout's `ledger/trials.db`, which every worktree shares
([ADR 0035](docs/adr/0035-trial-ledger-in-an-optuna-study.md), [ADR 0041](docs/adr/0041-trial-ledger-sample-and-data-identity.md));
`record_trial` runs a backtest over that sample as one of its trials, under a key of its configuration
and a digest of the bars it read and of each instrument definition it traded against ([ADR 0053](docs/adr/0053-trial-identity-data-digest-and-config-key.md)); `move_sample` takes in new data; and
`canon_trading.ranking.rank` ranks a study's trials, N counted from their keys
([ADR 0054](docs/adr/0054-trial-ledger-key-count-and-stops.md)), and tests the pick's deflated Sharpe ratio.

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
mise run paper:store windows    # the same three for account B, the session windows' account
mise run paper:session  # one SessionMaCross window on account B, 30 minutes by default; it ends
                        # flat or fails (--session extended outside the regular session; --strategy
                        # EmaCross runs upstream's built-in instead, in the regular session only)
```

There are two paper accounts ([ADR 0069](docs/adr/0069-two-paper-accounts.md)): account A, the default,
whose pair only `paper:test` and the data-only `paper:data` read, and account B (`windows`), whose pair only `paper:session` and its
schedule read. Each account has its own lock, so a test and a window do not wait for each other; the two
accounts share the login's market-data stream (one connection per endpoint, ADR 0069), so a paper test that streams and a window take turns on
a third lock. `mise run paper:data sandbox [--minutes N]` runs NautilusTrader's sandbox execution client on
Alpaca's live data with account A's pair, every request but GET refused and no Alpaca execution client
(ADR 0070); `quotes`, the other listed module, is refused until ADR 0066's loader is built.

`paper:store` refuses a key ID starting with `AK`, Alpaca's hint for a live key, then sends the pair to
Alpaca's paper API (`GET /v2/account`) and stores nothing unless it is accepted there; a live pair is
refused there. Each value then sits in the login keychain, service `canon-alpaca-paper` for account A and
`canon-alpaca-paper-windows` for account B. Of this repo's commands, only `paper:test` (A) and
`paper:session` (B) put a pair in an environment, each its own, when it starts (`paper:store` reads it
back once to confirm the write, and reads the other account's key ID to refuse the same pair twice); no
file, shell, `mise env` or `scripts/ci` gets it. The keychain does not stop other
programs you run from reading it: any process of yours can, with `security find-generic-password -w`, and
no dialog appears. To replace the pair, remove it and store it again.
Why it works this way, and what the keychain does not protect against:
[ADR 0029](docs/adr/0029-paper-keys-in-the-login-keychain.md); how the pair is checked before it is stored:
[ADR 0033](docs/adr/0033-paper-key-entry-checked-against-the-paper-api.md).

## Run Codex through the OmniRoute gateway

OmniRoute is a local OpenAI-compatible gateway for GPT and other non-Anthropic providers
([ADR 0056](docs/adr/0056-omniroute-gateway-for-non-anthropic-runtimes.md)). Claude is never routed through
it: no Anthropic or Claude credential, account or model goes in, and Claude Code keeps its own login. It is
optional: neither `mise install` nor `scripts/bootstrap --install` installs it. It lives in mise's `gateway`
config environment (`mise.gateway.toml`, locked in `mise.gateway.lock`). On a Mac, in your own terminal:

```bash
mise -E gateway install --locked npm:omniroute   # the pinned release, exactly the locked dependency graph
mise -E gateway run omniroute:store      # makes its storage key into the keychain; nothing typed or shown
mise -E gateway run omniroute:serve      # the gateway on 127.0.0.1:20128 with ADR 0056's settings, until Ctrl-C
mise -E gateway run omniroute:setup      # with it running: dashboard login off, a no-log API key minted into the keychain
mise -E gateway run omniroute:provider openai  # yours: a provider's API key at a hidden prompt, registered in the gateway
mise -E gateway run omniroute:codex      # Codex with its provider set to the gateway; arguments go to codex
mise -E gateway run omniroute:check      # which keychain items exist (prints none), and whether the gateway requires its key
mise -E gateway run omniroute:remove     # deletes them; without the storage key its stored credentials are lost
```

The provider key is the one thing you type: API keys only, never Anthropic or a sign-in, best one made for
this gateway alone with a spend limit. The dashboard at `http://127.0.0.1:20128` opens without a login.
`scripts/bootstrap --check` shows the installed version against the pin, and fails if a connection there
says Anthropic or Claude, is not an API key, or an API key logs request bodies.

## What runs on its own

| Layer | Runs | Costs tokens |
|---|---|---|
| Commit gate (`scripts/ci`) | Every `git commit` | No |
| Address check (`scripts/check-addresses`) | Every `git push` | No |
| Hosted CI (`scripts/ci` on GitHub) | Every pull request and push to `main` | No |
| Edit checks (canon lint, ruff) | Every matching edit by an agent | No |
| Hosted watch (`canon check` on GitHub) | Once a day; keeps one locked `canon-drift` issue in step with what has drifted (ADR 0064) | No |
| Landscape watch | Once a day, at session start | No |
| Lane watchdog | Every 10 minutes while the Mac is awake | No |
| Paper session | Every 35 minutes while the Mac is awake; a window only when Alpaca's clock says a session is open, on the `main` GitHub has then (ADR 0050) | No |
| OTLP collector | Always, while the Mac is awake: the pinned OpenTelemetry Collector on 127.0.0.1, writing the events `scripts/lanes --rates` reads (ADR 0007) | No |
| Weekly refresh | Weekly, when the desktop app is open | One session, short when nothing changed |
| Dependency bot (Dependabot) | Weekly, on GitHub | Only when a session lands its pull request |

`scripts/automation install` turns on the commit gate, the address check, the lane watchdog, the paper
session agent, whose runner keeps its own clone of `main` in `~/.claude/canon/`, and the OTLP collector. The edit
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
| `ledger/` | The trial ledger, an Optuna SQLite study, used from the main checkout by every worktree; ignored by git |
| `evals/review/` | The reviewer benchmark: cases, the brief every reviewer gets, the procedure |
| `evals/build/` | The build eval: cases made from past fixes, the brief every worker gets, the harness, the procedure |
| `.claude/settings.json` | Permissions and hooks for this repo |
| `orca.yaml` | The setup command Orca runs in a new worktree, once enabled and approved in the app |
| `.claude/rules/` | Rules scoped to `canon/`, `docs/adr/` and trading code |
| `.claude/skills/` | `adopt-upstream`, `new-adr`, `landscape-refresh`, `lane-report` |
| `.claude/agents/` | `upstream-scout` (Sonnet), `evidence-verifier` (Opus) |
| `home/` | Machine-level config: event ledger hook, status line and its Claude HUD settings, lane watchdog, paper session runner, OTLP collector config, Claude Code and Codex telemetry settings, user `CLAUDE.md` |
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
  and a limit order accepted and canceled (ADR 0030);the order-management paper tests are ADR 0036's and ADR 0042's.
  Another showed that Alpaca's fill activities carry the trade updates' `execution_id`s (ADR 0024).
  Upstream's data and execution testers then ran through the adapter on paper: quotes, trades, bars,
  market and limit orders, fills, cancels and a rejection passed (see the package README). The full asset
  list has not run against Alpaca. Fill history at start-up comes from Alpaca's fill activities (ADR
  0024).
  See `packages/canon-alpaca/README.md`.
* **Little market data in `catalog/`.** `load_bars` writes Alpaca's bars there (ADR 0040), and the only
  ranges anything here loads are the paper tests': AAPL's daily bars for the first quarter of 2025, and
  SPY's and QQQ's from 2020-07-27 to 2025-12-31 for ADR 0048's study. A fresh clone has none until those
  tests run. Bars are raw and from the IEX feed, the only feed and adjustment
  the adapter requests.
* **No strategy upstream ships has been ranked yet.** The one study on real bars ranked eight variants
  of the repo's `MaCross` on SPY and QQQ daily bars (ADR 0048, `canon_trading.variant_study`): no
  variant passed the gate (ADR 0052). Upstream's built-in `EmaCross` reads quotes, which the catalog does not
  hold. A trial that never traded, or a configuration that never runs to the end, stops a ranking:
  what value it should count as is undecided. The overfitting probability has no adopted
  implementation (ADR 0027 and ADR 0032 are `proposed`).
* **Tokens per second for Claude Code and Codex only.** A local OpenTelemetry Collector keeps their token
  events, names and counts only, in `~/.claude/canon/otel/logs.jsonl` once `scripts/bootstrap
  --user-config` has set their telemetry and `scripts/automation install` has started it. Muse Code,
  Antigravity and DeepSeek Harness document no token telemetry, so `scripts/lanes --rates` lists them as
  not measured, and there are no dashboards (ADR 0007).
* **No model-routing eval of our own has run.** Model routing rests on external measurements (ADR 0005). A reviewer
  benchmark is defined, with its cases, brief and procedure in `evals/review/` (ADR 0046); its first run
  (2026-11-10) scored 10 confirmed defects, in `evals/review/runs/`. Its cases now hold 23, 10 in
  `order-modify`, 10 in `trial-ledger-gate` and one each in three others, so with most bunched in two cases it
  can show only gross differences. A build eval is defined too, for a runtime and model that would build
  here: four cases made from past fixes, each shown sound by running, in `evals/build/` (ADR 0057). No
  trial of it has run. It is not a gate: another adopted runtime may be given build work under ADR
  0058's terms, and the eval is what keeps or retires it.
