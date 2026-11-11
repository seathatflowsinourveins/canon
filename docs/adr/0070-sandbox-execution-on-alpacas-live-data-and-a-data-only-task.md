---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Simulation without the paper account: NautilusTrader's sandbox execution client on Alpaca's live data, and a data-only task that cannot trade

## Context and Problem Statement

The owner asked on 2026-11-10 whether simulation could run without limit on NautilusTrader's native
features. ADR 0068
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

Chosen: **option 2**, under the owner's delegation of 2026-11-10; option 1 served until it was built
(2026-11-11, Confirmation), after the reading side of ADR 0069. Option 3 is not shown for paper keys (below). What is untested is UNVERIFIED.

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
reached only `paper:test` and `paper:session` when this was decided (ADR 0029, ADR 0044), so a sandbox run
then held the paper lock; `paper:data` now gives it account A's pair on the terms below. Its instrument provider builds a `TradingClient` for the asset reads (`providers.py`), and
`/v2/assets` is documented on the trading hosts only, so a data-only process holds a pair that can trade.

**The data-only task's terms.**

1. `paper:data` reads the pair the tests use (today's, service `canon-alpaca-paper`) into its own
   environment, as `paper:test` does (ADR 0029), and by no other route.
2. It runs one named module, never a test runner over a path. Two modules are allowed; a third needs a
   new record:
   * the sandbox node, which holds the stream lock ADR 0069 defines (whether Alpaca's limit spans a
     second paper account of one login is UNVERIFIED);
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
* Good: a data-only process cannot send an order through this repository's clients: two checks in code,
  shown offline by term 4's test (Confirmation).
* Bad: a sandbox run holds the stream lock for its whole run, up to an hour, and a window waits at most
  900 s for that lock (ADR 0050), so a window that starts during a longer sandbox run fails placing
  nothing. Runs by hand are chosen around the schedule until an unattended sandbox lane exists (ADR 0068).
* Bad: the guard does not reach code that builds its own HTTP client with the pair.
* Bad: the stream is one connection on many plans, so a sandbox node and a streaming paper test take turns; Alpaca's own
  rules are not simulated.

### Confirmation

* Built 2026-11-11, with term 4's test in `scripts/ci`:
  * `mise run paper:data` reads account A's pair (service `canon-alpaca-paper`) into its own environment, sets
    `CANON_ALPACA_DATA_ONLY=1`, and runs one module from the list `sandbox` (`canon_trading.sandbox_node`) and
    `quotes`, which it refuses as not built yet (ADR 0066); mise refuses any other name before the script
    runs. The sandbox node takes the stream lock after uv's sync, under a bounded deadline
    (`scripts/test-lanes`, under mise, with a stand-in keychain and uv).
  * Term 3: a `get_only` mode of `providers.TimeoutAdapter`, raising `MethodRefused` before anything is
    sent; `with_http_timeout` keeps it once set and replaces an adapter mounted for any other prefix. The
    data client's connect calls `require_get_only` on its historical client before its first request, and
    the provider calls it on its own client each time it fetches it, before anything is sent. `sandbox_node` sets the mode itself, refuses to build outside it, and
    after the build stops if an `AlpacaExecutionClient` exists (read from the collector: the node lists no
    clients at the pin).
  * Term 4 (`packages/canon-alpaca/tests/test_data_only.py`, `tests/test_sandbox_node.py`): submit (POST),
    replace (PATCH), cancel and close (DELETE) through the provider's client, fetched twice, each refused
    with nothing reaching requests' transport, while a GET goes out; a node built with the Alpaca execution
    client refused. Each behaviour test failed with its guard removed (run); the enumeration tests are
    checked instead on made-up source, where an aliased import and an unguarded client are found (run).
  * The build condition: the test enumerates, by name in the source, alpaca-py 0.45.0's `Session()` constructions (one, in
    `RESTClient.__init__`), its `_session` assignments (the same one), its module-level requests calls and
    other HTTP libraries (none), and its `RESTClient` subclasses (eight); and every REST client
    construction in the adapter and `canon_trading` (four: the data client's, the execution client's, the
    provider's and the window's account check's), each passed to `with_http_timeout`. A node in data-only
    mode whose historical or provider client sends on a session the refusing adapter is not on (a
    session replaced before the connect) does not start: the data client's connect fails and the node ends
    with "readiness timeout while waiting for engine connections"; with both clients refusing it starts
    (run, offline).
  * `mise run paper:data sandbox --minutes 3`, 2026-11-11, 07:47:28 to 07:50:34 UTC, exit 0: every engine
    client connected, the account was the sandbox's (`ALPACA-SANDBOX-001`), no request was refused, and the
    tester placed no order, since a built-in streams IEX, which carries nothing overnight (ADR 0055).
    That run was on a working tree before the review's fixes; the run on this change's code is the
    next bullet.
  * `mise run paper:data sandbox --minutes 3` at the branch's ea7c1fa, whose code `main` took unchanged,
    2026-11-11: started 08:09:12 UTC and waited on the stream lock, which a scheduled window held until
    08:15:40; the node started at 08:15:41 and stopped at 08:18:46, exit 0, every engine client connected on
    the sandbox's account (`ALPACA-SANDBOX-001`), no order placed (IEX, overnight), nothing left open, and
    no request refused (run). Fills on this task wait for a run in the regular session (UNVERIFIED until then).
  * A limit of term 3's check: it reads each client's session once, at the connect or the fetch, so a
    session an alpaca-py client replaced after that, which alpaca-py 0.45.0 never does (its one `_session`
    assignment is in `RESTClient.__init__`), would not be checked again.
* A condition on that build, since term 3 holds "fails closed" in one place, the adapter
  `with_http_timeout` mounts: its test enumerates every place the adapter, the provider and alpaca-py
  construct a REST client or a `requests.Session` (a library path that does not pass through
  `with_http_timeout`, a session replaced after the mount), and shows that the node refuses to start in
  data-only mode if any client it holds lacks the refusing adapter.
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
* Follow-up, 2026-11-11: the build's enumeration condition added to Confirmation; the owner's question
  dated.
* alpaca-py 0.45.0 in `.venv` — `RESTClient._one_request` sends through `self._session.request`, and an
  error other than `HTTPError` leaves its retry loop, so `MethodRefused` ends the call. Read and run,
  2026-11-11.
* NautilusTrader v2.0.0rc8 in `.venv` — `LiveNode` exposes no list of its clients; `nautilus_trader.adapters.sandbox`
  is the example's import path at the pin; a data client whose connect fails leaves the hosted run ending
  with "readiness timeout while waiting for engine connections". Run, 2026-11-11.
