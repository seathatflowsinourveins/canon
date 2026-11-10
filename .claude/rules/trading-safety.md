---
paths:
  - "src/**"
  - "packages/**"
  - "tests/**"
  - "catalog/**"
  - "**/*.py"
---

# Trading code

* **Paper only.** Use Alpaca paper credentials and paper endpoints. Take endpoint names from
  `https://docs.alpaca.markets/llms.txt`, not from memory.
* `alpaca-py`'s `TradingClient` defaults to `paper=True` at the pinned version (a test asserts it). Never
  pass `paper=False` or `url_override`.
* Never place, modify or cancel a live order. Never read, print, log or commit a credential.
* Credentials come from the environment. `.env` files are denied to agents by `.claude/settings.json`.
  The paper pair is in the login keychain and reaches only `mise run paper:test` (ADR 0029). Never run
  `security find-generic-password` with `-w` or `-g` on it, and never put a key value in a file, a command
  line or output.
* A paper run needs no ask, at any hour, as long as the paper tests follow the practice of Alpaca's own
  repositories and NautilusTrader (the owner's standing instruction, 2026-11-10; for the adapter that is
  upstream's Execution Testing Spec and its `ExecTester` where a row covers the case, ADR 0036 and ADR
  0042): run `mise run paper:test`, which holds the paper account's lock for the whole run. Never live.
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
