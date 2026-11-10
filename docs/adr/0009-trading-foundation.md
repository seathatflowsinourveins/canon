---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Trading foundation: NautilusTrader engine, alpaca-py venue SDK, adapter built to upstream's guide

## Context and Problem Statement

The owner named the Alpaca org repos and NautilusTrader as the foundations. What exactly is pinned, and how do
the two connect?

## Decision Drivers

* One strategy codebase for backtest and live.
* Official, maintained sources only as foundations.
* No live capital exposed to unreviewed or pre-release code.

## Considered Options

1. NautilusTrader for backtests, direct alpaca-py calls for live trading.
2. NautilusTrader for both, using a community Alpaca adapter.
3. NautilusTrader for both, with an out-of-tree Alpaca adapter written to upstream's adapter guide on top of
   alpaca-py.
4. NautilusTrader with a venue it supports officially (for example Interactive Brokers) instead of Alpaca.

## Decision Outcome

Chosen: **option 3**, with option 4 recorded as the fallback that needs no custom adapter. Choosing a broker
is the owner's call, not this ADR's.

**The fact that shapes this decision: NautilusTrader has no official Alpaca adapter.** Alpaca is absent from
upstream's integrations list, the request is an open RFC, and the maintainer wrote that an official adapter is
"not in the near term" and suggested a community listing.

Pins (see `canon/upstreams.lock.json`):

* **NautilusTrader `v2.0.0rc8`** for development. v2 is the line upstream's current docs describe, and v1 now
  gets critical security backports only. It is still a release candidate, so: **paper trading only until
  v2.0.0 stable**. `v1.231.0` is pinned as a reference fallback. Install v2 with `--pre`; a plain install
  silently gives v1, which does not match the docs.
* **alpaca-py `v0.45.0`.** The older `alpaca-trade-api-python` is archived.
* **alpaca-mcp-server `v2.4.1`**, **alpaca-skills** and **alpacahq/agentic** are pinned for reference and
  **not enabled**. The four trading skills were read on 2026-11-09 and rejected for now (ADR 0004).
* **Docs for agents:** `https://nautilustrader.io/docs/llms.txt` and `https://docs.alpaca.markets/llms.txt`.

The adapter:

* Lives out of tree as its own package, implementing upstream's `DataClient` / `MarketDataClient` /
  `ExecutionClient` base classes and factories, calling alpaca-py.
* It is the largest piece of code this repo will own, so it is the highest-risk item. It needs its own tests
  against Alpaca's paper environment before anything else depends on it.
* Community adapters are read for reference only: both found have 0 stars and no releases. One calls
  itself alpha, the other "production-ready"; neither has been reviewed.
* Known upstream limits: Redis and PostgreSQL cache backing is unsupported with custom Python clients.
  NautilusTrader's `Equity` instrument is whole-share only and the maintainer prefers to keep it so, while
  Alpaca allows fractional shares, so the adapter cannot represent fractional equity positions.

Safety rules (also in `.claude/rules/trading-safety.md`):

* Paper credentials only in any environment an agent can read. Live keys never enter the repo, a settings
  file, or an agent's context.
* Agents do not place, modify or cancel live orders. Going live is a human action.

Enforced by `.claude/hooks/guard.sh`, which runs before every tool call in every permission mode: it
refuses commands, fetches and Python edits that name Alpaca's live trading or production broker hosts, pass
`paper=False` or `url_override`, or set the variables that switch Alpaca's CLI or MCP server to live. The
host names come from the pinned `alpaca-py` (`alpaca.common.enums.BaseURL`) and the variable names from the
pinned `alpaca-skills`. `scripts/test-hooks` covers it. It checks text an agent sends, so it cannot stop
code that builds a live URL at run time; keeping live credentials off this machine remains the real control.

### Consequences

* Good: backtest and live parity on one engine; official sources at every foundation.
* Bad: we own an adapter upstream does not; we build on a release candidate and must re-pin at v2 stable.

### Confirmation

`scripts/canon check` reports when either upstream moves. This ADR is reviewed when NautilusTrader v2.0.0
ships or in 30 days, whichever is first.

**Adapter, first slice (2026-11-09, branch `canon-alpaca-data`).** `packages/canon-alpaca` now holds
`constants.py`, `providers.py` (`AlpacaInstrumentProvider`), `data.py` (`AlpacaDataClient`, a
`MarketDataClient`) and `factories.py` (`AlpacaDataClientFactory`), in upstream's template layout. It loads
US equities from Alpaca's assets endpoint as `Equity` instruments, serves historical bars on request, and
streams quotes and trades. Paper data and the IEX feed only. There is no execution client, so nothing in it
can place, modify or cancel an order.

What the tests in `packages/canon-alpaca/tests/` cover:

* Offline, passing in `scripts/ci`. Conversions run on payloads hand-built from Alpaca's doc examples. The
  provider's queries, filters and failures are tested. A real `LiveNode` runs with stand-ins for the three
  alpaca-py clients, registered through config and through the builder, launched owned and hosted. In it,
  instruments, bars, quotes and trades reach the strategy and the cache. A failed fetch becomes the
  request's failure reason, and unsubscribes run on stop. Shutdown stops and joins the stream thread. The
  alpaca-py clients are built with `paper=True` and the IEX feed. A missing key pair fails the connect and
  is named in the log.
* Against Alpaca's paper environment: one asset and 14 days of daily bars through a real node. It is skipped
  until a paper key pair is in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, and had not run by this date, so
  the adapter is UNVERIFIED against Alpaca.
* Not tested against Alpaca at all: the live quote and trade stream, and `load_all` at full scale
  (UNVERIFIED).
* The template's fill test does not apply until an execution client exists.

Limits found while building it, beyond the whole-share limit above:

* Alpaca's assets endpoint documents `price_increment` for crypto only. Alpaca's US equity order rule is
  price-dependent: at most 2 decimals at or above $1.00, 4 below. `Equity` takes one increment, whose
  precision must equal `price_precision`. The adapter uses 0.0001, so market data is never rounded. That
  leaves the $1.00 rule to the execution slice. The owner should ratify this choice.
* Alpaca's stream schema gives quote sizes in round lots, with no lot size to convert by. They pass through
  unconverted.
* alpaca-py 0.45.0 reads no credential environment variable; `RESTClient._validate_credentials` uses its
  arguments only. The adapter reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, the names Alpaca's own CLI
  and MCP server read.
* alpaca-py's `StockDataStream.run()` drives its own event loop, and `subscribe_*` blocks on that loop once
  it is running. So the stream runs on a thread the client owns, and messages are handed to the node's loop.
* NautilusTrader v2.0.0rc8 logs a failed custom-client connect as "connection failed", without the
  exception's message (observed). The client logs the reason itself.

## Evidence

Read 2026-11-09 (fetched; fetches are model-summarised. Pins resolved by `scripts/canon`).

* https://nautilustrader.io/docs/latest/integrations/ — 21 integrations; Alpaca is not among them
  (fetched twice: by the research lane and again by the orchestrator).
* https://github.com/nautechsystems/nautilus_trader/issues/3374 — open RFC for an Alpaca adapter; maintainer
  comments of 2026-01-06 ("not in the near term") and 2026-10-18 (community listing; keep the
  `nautilus-alpaca` crate name free) (fetched twice, as above).
* https://nautilustrader.io/docs/latest/developer_guide/python_adapters — the out-of-tree adapter path and
  its base classes; cache-backing limit.
* https://github.com/nautechsystems/nautilus_trader/releases — v2.0.0rc8 is the newest release, a
  pre-release (resolved to `d53849b216e2`); v1.231.0 resolved to `27a8e54e7ac3`. Branches `develop`
  (default), `master`, `nightly`, `develop_v1` confirmed by ref advertisement.
* https://github.com/nautechsystems/nautilus_trader — LGPL-3.0; `--pre` needed for v2; v1 gets critical
  security backports only. Python 3.13 and 3.14 in practice: the README lists 3.15 as a source build only,
  and PyPI's rc8 metadata says `>=3.13,<3.15`.
* https://github.com/nautechsystems/nautilus_trader/issues/5462 — open; `Equity` is whole-share only and the
  maintainer wrote on 2026-10-26 that he prefers to keep that contract (found by the second reader).
* https://nautilustrader.io/docs/latest/concepts/live/ — the same strategy code runs in backtest and live;
  venue, transport and timing behaviour may not be reproduced in simulation.
* https://github.com/alpacahq/alpaca-py — Apache-2.0, v0.45.0 (resolved to `c42add7c0a70`).
* https://github.com/alpacahq/alpaca-trade-api-python — archived 2026-06-17, points to alpaca-py.
* https://github.com/alpacahq/alpaca-mcp-server — MIT; v2 is a full rewrite (tag `v2.4.1` resolved to
  `63cc4278d87b`; the releases page showed none, the tag exists in the ref advertisement).
* https://github.com/alpacahq/alpaca-skills and https://github.com/alpacahq/agentic — official skills and
  plugin marketplace; contents not yet read.
* https://github.com/jssyxd/nautilus-alpaca and https://github.com/gmatrunich/nautilus-alpaca — community
  adapters; last commits 2026-10-18 and 2026-10-01.
* Second reader, 2026-11-09 (`evidence-verifier`): 17 of 19 claims supported. The two corrections (the
  "alpha" wording and the Python range) and the fractional-share limit are applied above.

For the adapter's first slice, read 2026-11-09. These are raw markdown or source files, fetched with
`curl` and not summarised, and the installed pinned packages:

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md — client hooks, factory
  contract, lifecycle, typed responses, provider loading.
* https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186/examples/live/_template
  and `python/tests/integration/test_python_adapter_template.py` at the same commit — module layout and test
  shape, read for shape only (LGPL-3.0).
* https://nautilustrader.io/docs/md/latest/concepts/backtesting/bar-execution.md — for bars stamped at the
  open, `ts_init = ts_event + interval`.
* https://docs.alpaca.markets/us/reference/get-v2-assets-1.md — asset schema; `price_increment` is "Field
  available for crypto only".
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Sub-penny increments": 2 decimals at or above
  $1.00, 4 below.
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md — stream schema; quote sizes "in
  round lots".
* https://docs.alpaca.markets/us/docs/paper-trading.md — a paper-only account gets IEX data only.
* https://docs.alpaca.markets/us/docs/authentication.md — paper accounts call `paper-api` for trading and
  the shared market data host.
* https://github.com/alpacahq/cli/blob/4f83692cd24e7fc043ec05c7255d3f23108d4c3e/internal/config/config.go
  and https://github.com/alpacahq/alpaca-mcp-server/blob/63cc4278d87b31f3ed4efcfaa55624095c6d7be8/README.md
  — both read `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
* alpaca-py 0.45.0 as installed: `common/rest.py` (credentials from arguments only),
  `data/live/websocket.py` (`run`, `stop`, `_subscribe`), `data/models/bars.py` (bar timestamp is the
  open).
