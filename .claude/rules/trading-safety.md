---
paths:
  - "src/**"
  - "packages/**"
  - "tests/**"
  - "catalog/**"
  - "**/*.py"
---

# Trading code

* **Paper for every session but the live lane** (ADR 0103). Every session but the live lane uses Alpaca's
  paper credentials and paper endpoints. Take endpoint names from `https://docs.alpaca.markets/llms.txt`, not
  from memory. The live lane, one Claude Code session started for it, may place, modify and cancel live
  orders, and only through its `mise run live:*` tasks. Nothing trades live until ADR 0110 is accepted and
  its live order tasks are on `main`. No other path: no direct alpaca-py call, CLI, MCP server or
  connector. Subagents, reviewers and every worker of another runtime never touch live.
* `alpaca-py`'s `TradingClient` defaults to `paper=True` at the pinned version (a test asserts it). The paper
  configuration and paper code never pass `paper=False` or `url_override`; only the separate live
  configuration ADR 0110 lists, once it lands, reaches the live host.
* Outside the live lane's tasks, never place, modify or cancel a live order. No session ever reads, prints,
  logs or commits a credential, paper or live; the live pair is entered by the owner and reaches only the
  `live:*` tasks.
* Credentials come from the environment. `.env` files are denied to agents by `.claude/settings.json`.
  Account A's paper pair is in the login keychain and reaches only `mise run paper:test` (ADR 0029),
  `mise run paper:data` in data-only mode (ADR 0070), `mise run paper:long-only tests`,
  `mise run paper:account tests` and `mise run paper:close tests`;
  account B's, stored beside it with `paper:store windows`, reaches only `mise run paper:session`, by hand
  or from the scheduled agent (ADR 0044, ADR 0050, ADR 0069), `mise run paper:long-only windows`,
  `mise run paper:account windows` and `mise run paper:close windows`;
  account C's, stored with `paper:store protect`, reaches only `mise run paper:account protect` (ADR 0114).
  `mise run paper:history <tests|windows>` reads either one, GET only in data-only mode, a historical
  load that neither trades nor streams (ADR 0093).
  No task reads both pairs. `paper:long-only <account> <check|on|off>` reads, or sets, Alpaca's long-only
  setting and prints only its value; `paper:account <account>` reads, GET only, and prints the account's
  balances, positions, open orders with their lineage (ours or foreign) and `no_shorting`, never an account
  number or order ID; `paper:close <account>`, under the account's lock, cancels every open order of our
  nodes' lineage (our client order IDs) and closes only the quantity our orders opened; a symbol with
  anything else in it is frozen and named, and its other orders and quantity are never touched. Never run
  `security find-generic-password` with `-w` or `-g` on any pair, paper or live, and never put a key value
  in a file, a command line or output.
* A paper run needs no ask, at any hour, as long as the paper tests follow the practice of Alpaca's own
  repositories and NautilusTrader (the owner's standing instruction, 2026-11-10; for the adapter that is
  upstream's Execution Testing Spec and its `ExecTester` where a row covers the case, ADR 0036 and ADR
  0042): run `mise run paper:test`, which holds account A's lock for the whole run. This no-ask rule is for
  paper only; live is the live lane's alone (ADR 0103).
  The owner decided on 2026-11-11 that this covers both paper accounts, each under its own lock, whoever
  starts the run (a lane, a coordinator or the scheduled agent): `mise run paper:test` on account A,
  `mise run paper:session` on account B and `mise run paper:data` (ADR 0069, ADR 0070).
  A paper test that opens a market-data stream carries the `alpaca_stream` marker, so it holds account
  A's stream lock, which `paper:data`'s streaming modules also take; a window takes account B's.
  Every paper test places nothing if the account holds an open order it could touch, and cancels what it
  opened and closes any position it opened, in a `finally`.
* NautilusTrader is pinned to a v2 release candidate in `pyproject.toml` and `uv.lock`. Install with
  `uv sync`, never a bare `pip install`, which gives v1 and does not match the docs. Read
  `https://nautilustrader.io/docs/llms.txt` for the page you need before writing against an API.
* Strategies and node configuration go in `src/canon_trading/`. The adapter goes in
  `packages/canon-alpaca/`. Upstream keeps the two apart; so do we.
* Market data goes in `catalog/` and is never committed.
* Add a dependency with `uv add`, pinned exactly, after it is in `canon/upstreams.json`.
* Rank backtest variants only by ADR 0010's protocol: every trial in the ledger, and the pick's deflated
  Sharpe ratio at 0.95 or above, through `canon_trading.ranking.rank` on trials recorded by
  `canon_trading.ledger.record_trial` (ADR 0035).
* This is engineering work, not investment advice. Do not recommend trades, positions or strategies to hold.
