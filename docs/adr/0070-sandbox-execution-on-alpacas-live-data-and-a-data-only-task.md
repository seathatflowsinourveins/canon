---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Simulation without the paper account: NautilusTrader's sandbox execution client on Alpaca's live data, and a data-only task that cannot trade

## Context and Problem Statement

The owner asked whether simulation can run without limit on NautilusTrader's native features. ADR 0068
lays out the lanes; the backtest forms are there. This record covers the third native form, the sandbox
execution client, which matches orders locally on live market data: whether it works here at the pin, what
it models, how many strategies it carries, and what a process that only streams would need so that it
cannot place an order.

## Decision Drivers

* Upstream's form as documented (`examples/live/sandbox/exec_tester.py`), configured only.
* No order reaches Alpaca from a sandbox run, held in code, not by intent (ADR 0009); until the task
  is built, by how the node is built and a check after the build (term 6).
* The paper pair reaches a process only on ADR 0029's terms.

## Considered Options

1. Sandbox runs through `paper:test`, under the paper lock, as the proof below ran.
2. A data-only task, `paper:data`, that gives the pair to a process that streams or reads and never
   trades, with "never trades" failing closed in code.
3. A key or token that cannot trade, held at Alpaca.

## Decision Outcome

Chosen: **option 2**, under the owner's delegation of 2026-11-10; option 1 serves until it is built, with
the reading side of ADR 0069 (not yet written). Option 3 is not shown for paper keys (below). What is untested is UNVERIFIED.

**The sandbox at the pin.** `add_simulated_exec_client(name, SandboxExecutionClientFactory(),
SandboxExecutionClientConfig(venue=...))` on a `LiveNode`, reachable from Python. The client matches in
the `OrderMatchingEngine` on the quotes, trades and bars the bus carries for its venue (`execution.rs`,
`register_message_handlers`), so on venue `ALPACA` it fills on the Alpaca data client's live data: proved
once (Confirmation). Fill, fee, latency and margin models, queue position and liquidity consumption are
its, off unless set (`config.rs`); Alpaca's rules are not: the adapter's denials, the 403 wash-trade
refusal ("potential wash trade detected", as `test_paper_matrix.py` records it), paper's random
partial fills ("Paper Trading"). A sandbox run is comparable to paper only for
orders the adapter would pass. NautilusTrader v2.0.0rc9 fixes the sandbox's open-order check rejecting
resting orders and GTD fills after expiry (release notes), so an unattended sandbox lane waits for that
pin move through `adopt-upstream`.

**Many strategies, one node, one stream.** `NETTING` keeps "one position per instrument and strategy", ID
`{instrument_id}-{strategy_id}` (`positions.md`), and the sandbox reports no positions ("Position reports
are always empty"), so nothing merges them. They share the one simulated account's cash, and each needs
its own `order_id_tag` (refused otherwise; run). They share one data client and one stream, within the
plan's symbols (Basic: 30, "About Market Data API"); Alpaca allows "in many subscriptions (or without
one)" one connection, 406 beyond ("Streaming Market Data"), so parallel sandbox nodes need a stream each
and parallel strategies go in one node. A built-in subscribes with no params, so it streams IEX (ADR 0055);
overnight it needs a strategy that names `boats`, one night's observation, not an entitlement (ADR 0055).

**The pair, the lock and the trading host.** The data client authenticates with the paper pair, which
reaches only `paper:test` and `paper:session` (ADR 0029, ADR 0044), so a sandbox run holds the paper lock
today. Its instrument provider builds a `TradingClient` for the asset reads (`providers.py`), and
`/v2/assets` is documented on the trading hosts only, so a data-only process holds a pair that can trade.

**The data-only task's terms.**

1. `paper:data` reads the pair the tests use (today's, service `canon-alpaca-paper`) into its own
   environment, as `paper:test` does (ADR 0029), and by no other route.
2. It runs one named module, never a test runner over a path. Two modules are allowed; a third needs a
   new record:
   * the sandbox node, which holds the stream lock ADR 0069 is to define (whether Alpaca's limit binds
     per endpoint or per user is UNVERIFIED);
   * the historical quote loader ADR 0066 proposes, once built: REST reads only, no stream and so no stream lock, at a rate
     that leaves room for the windows' own data calls once that share is measured (UNVERIFIED until then).
3. "Never places an order" fails closed in code, twice: a transport adapter refusing every method but GET
   before anything is sent, on every alpaca-py REST client the process builds. It is part of the adapter
   `providers.with_http_timeout` mounts (a mode of `TimeoutAdapter`), not a second one: that function
   replaces any other adapter each time it runs, and the provider runs it on every `_client()` call, so a
   separate refusing adapter would come off silently (shown offline by a second reader). And a check, after the node is built, that no Alpaca
   execution client is registered, failing the run.
4. Its test shows `submit_order` (POST), `replace_order_by_id` (PATCH), `cancel_order_by_id` and
   `close_position` (DELETE) each refused with nothing sent, after the provider's client is fetched again,
   and a node carrying an Alpaca execution client refused.
5. It guards this repository's code paths, not any code holding the pair. It lets a process read assets,
   stream quotes and trades and request historical data; it does not let one submit, replace, cancel or
   close.
6. Until it is built, a sandbox run under `paper:test` checks after the build that no Alpaca execution
   client is registered and fails otherwise, as the proof's probe did.

The next step after the transport refusal, not built now: instruments read from the catalog would keep the
process off the trading host altogether, an adapter change.

**Option 3.** Held at Alpaca it would be stronger, but the key access levels read are the Broker API's
(credential management); an OAuth token "assumes" read-only access by default and adds `trading` only when
asked, and whether a `data`-only token reads `/v2/assets` is UNVERIFIED.

### Consequences

* Good: live-data runs with no account and many strategies on one stream; the proof ran on `main`'s code.
* Good: once built, a data-only process cannot send an order through this repository's clients: two
  checks in code (UNVERIFIED until term 4's test runs).
* Bad: the guard does not reach code that builds its own HTTP client with the pair.
* Bad: the stream is one connection on many plans, so a sandbox node and a streaming paper test take turns; Alpaca's own
  rules are not simulated.

### Confirmation

* When `paper:data` is built: its test (term 4) in `scripts/ci`; until then the task does not exist.
* `mise run paper:test -- -c pytest.toml -v -s -k sandbox <probe>`, 2026-11-10, 23:18 to 23:20 New York,
  exit 0, 4 passed; the probe is not committed. A `LiveNode` with the Alpaca data client and the sandbox
  client (venue `ALPACA`, `CASH`, `NETTING`, $100k, zero fees). Mid-run the venue's account was the
  sandbox's (`ALPACA-SANDBOX-001`) and no `AlpacaExecutionClient` existed; one `TradingClient` did, the
  provider's by `providers.py` (the probe's own holder check found none and passed on an empty list). A
  small strategy on `boats`: 358 SPY quotes and 9 trades in 96 s; buy filled at 754.26, the ask; sell at
  754.17, the bid; a limit at 90% of the bid accepted and canceled; flat. `EmaCross` (SPY) and
  `GridMarketMaker` (QQQ) as shipped: stream on `v2/iex`, 0 orders in 28 s. Two read-only GETs before and
  after found no open order and no position; they cannot show nothing reached Alpaca, which rests on there
  being no Alpaca execution client. This run started at 23:18:28 and its node at 23:18:30; an earlier run started
  at 23:00:51 and its node at 23:16:11, a wait for the lock inferred, not logged. Three earlier runs failed in the probe's code: reading node state mid-run and
  a cancel by order object; `Cache.accounts`; a check that counted the provider's `TradingClient`.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/examples/live/sandbox/exec_tester.py
  — the sandbox beside a venue's live data client. Fetched. `…/crates/adapters/sandbox/src/config.rs` and
  `src/execution.rs`, `…/docs/concepts/positions.md` at the commit — as cited. Fetched.
* NautilusTrader v2.0.0rc9 release notes (published 2026-11-10) — the sandbox fixes. Fetched by ADR 0068's
  second reader; the pin is rc8 (`scripts/canon outdated`).
* nautilus-trader 2.0.0rc8 in `.venv` — `SandboxExecutionClientConfig`, `add_simulated_exec_client`,
  `order_id_tag` refusal. Run.
* `canon_alpaca` `data.py`, `providers.py` — one stream per client, the IEX default, the provider's
  `TradingClient`. Read.
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — the connection limit, 406. Fetched.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — 30 symbols on Basic. Fetched.
* https://docs.alpaca.markets/us/docs/paper-trading.md — what paper simulates. Fetched.
* https://docs.alpaca.markets/us/reference/get-v2-assets-1.md — the trading hosts only. Fetched by a scout.
* https://docs.alpaca.markets/us/docs/credential-management.md (Broker API) and
  https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api.md (scopes). Fetched by a scout and
  ADR 0068's second reader.
* alpaca-py 0.45.0 in `.venv` — the four calls' methods (POST, PATCH, DELETE, DELETE), all through
  `RESTClient._session`. Read by the second reader.
* Second reader (`evidence-verifier`), 2026-11-10: a refusing adapter separate from `TimeoutAdapter`
  would be replaced by `with_http_timeout` (an offline run); the wash-trade refusal was cited to the wrong
  page; the run's start times were another run's; pair A and ADR 0069 were undefined; "one stream per
  login" overstated; ADR 0066 is `proposed`; a third module needs a new record. All applied above.
