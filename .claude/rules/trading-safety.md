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
* NautilusTrader is pinned to a v2 release candidate in `pyproject.toml` and `uv.lock`. Install with
  `uv sync`, never a bare `pip install`, which gives v1 and does not match the docs. Read
  `https://nautilustrader.io/docs/llms.txt` for the page you need before writing against an API.
* Strategies and node configuration go in `src/canon_trading/`. The adapter goes in
  `packages/canon-alpaca/`. Upstream keeps the two apart; so do we.
* Market data goes in `catalog/` and is never committed.
* Add a dependency with `uv add`, pinned exactly, after it is in `canon/upstreams.json`.
* Backtest results may be reported but not ranked as winners until ADR 0010 is accepted.
* This is engineering work, not investment advice. Do not recommend trades, positions or strategies to hold.
