---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# The Alpaca adapter: out of tree, to upstream's adapter guide, on alpaca-py

## Context and Problem Statement

ADR 0009 chooses an out-of-tree Alpaca adapter written to upstream's adapter guide on top of alpaca-py (its
option 3), and holds the safety rules it obeys. How is that adapter built, what does each slice hold, and
what is still unverified against Alpaca?

## Considered Options

ADR 0009's four; this record builds its option 3.

## Decision Outcome

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

### Confirmation

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
  precision must equal `price_precision`. The adapter uses 0.0001. (Corrected 2026-11-11: a trade or bar
  price finer than 0.0001 is rounded as upstream rounds and named, and such a quote dropped; ADR 0055,
  "Prices finer than the instrument's precision".) That
  leaves the $1.00 rule to the execution slice. The owner should ratify this choice.
* The execution slice denied a limit price finer than the $1.00 rule allows until 2026-11-11. Since then
  it sends such a limit on Alpaca's increment, a buy rounded up and a sell down, so never less marketable,
  on a submit and a modify alike, and generates `OrderUpdated` with the price sent just before
  `OrderAccepted`, so the node's order and a status report of Alpaca's agree, and a strategy that
  modifies on acceptance does not take that update for its modify's answer. A modify whose limit rounds
  to the order's own price, with nothing else changed, is refused before Alpaca. Why: NautilusTrader's order emulator
  releases a triggered stop-limit or trailing stop-limit at the instrument's fixed increment
  (`crates/execution/src/order_emulator/emulator.rs` builds its matching core with `price_increment`;
  `crates/execution/src/trailing.rs` rounds the trailing price to it), so a strategy holding its prices in
  cents still has a released limit like 10.4237 above $1.00, which the denial turned into an unprotected
  position (`tests/test_mover.py::test_a_fractional_cent_bid_never_releases_a_sub_penny_limit_above_one_dollar`).
  Stop prices finer than the increment are still denied: which way a trigger rounds is the strategy's
  call. Paper proof of a released exit above $1.00 accepted at the rounded price: UNVERIFIED until the
  first firing on such a name.
* A gap, recorded 2026-11-12: two senders place a limit through alpaca-py directly and skip these
  checks: paper:close (`src/canon_trading/custody.py`, `_CloseOnly._close`) and the window's close
  after a stop (`src/canon_trading/session_window.py`, `_AccountCheck._close`). They are on the step
  only because `through_the_touch` quantizes to whole cents; nothing checks it. The step is held in
  more than one place. It is in this slice's `venue_limit_price` and `sub_penny_denial`. It is in the
  tiered tick scheme with its `tick`, now in `src/canon_trading/price_step.py`. It is in the Mover's
  trailing-limit offset, which quantizes to a cent itself (`mover.offsets`), and in `through_the_touch`.
  `session_ma_cross.py` and `session_window.py` each define their own `CENT`. `price_step` is the
  function every sender is to price through before the adapter. Each sender moves to it in a change
  of its own.
* Alpaca's stream schema gives quote sizes in round lots, with no lot size to convert by. They pass through
  unconverted.
* alpaca-py 0.45.0 reads no credential environment variable; `RESTClient._validate_credentials` uses its
  arguments only. The adapter reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, the names Alpaca's own CLI
  and MCP server read.
* alpaca-py's `StockDataStream.run()` drives its own event loop, and `subscribe_*` blocks on that loop once
  it is running. So the stream runs on a thread the client owns, and messages are handed to the node's loop.
* NautilusTrader v2.0.0rc8 logs a failed custom-client connect as "connection failed", without the
  exception's message (observed). The client logs the reason itself.

**Adapter, execution slice (2026-11-10, pull request 24).** `execution.py` now holds `AlpacaExecutionClient`,
so the first slice's "There is no execution client" no longer holds: the adapter can place and cancel orders
on Alpaca's paper Trading API. It sends market and limit orders for US equities, cancels them, turns trade
updates into order events and fills, and reports orders and positions at start-up.
`AlpacaExecutionClientConfig` refuses `paper` other than `True` and any `url_override`, and the client builds
alpaca-py's clients with `paper=True` whatever the config holds. It denies a limit price with more than 2
decimals at or above $1.00, or more than 4 below, the rule the first slice left to it. The template's fill
test now applies and runs offline, in `scripts/ci`. The one paper test, a limit order placed and canceled, has
not run, so the client is UNVERIFIED against Alpaca. Its limits are in the package README.

**Against Alpaca's paper environment (2026-11-10).** With a paper key pair in the login keychain (ADR 0029),
`mise run paper:test` ran both paper tests and both passed: one asset and a week of daily bars reached a
strategy through a real node, and a DAY limit buy of 1 AAPL at $1.00 was accepted and then canceled. So the
two "had not run" statements above no longer hold for those two paths. A fill and a partial fill ran at
Alpaca the same day, outside the adapter, in ADR 0024's paper test. Upstream's data and execution testers
then ran through the adapter (the package README's matrix): the live quote and trade stream passed, trades
without an aggressor side; so did fills, a venue rejection and cancel-all. Still UNVERIFIED against
Alpaca: `load_all` at full scale and a partial fill through the adapter.

TC-E84, "Reconcile open orders" (2026-11-10, after hours, `mise run paper:test`): a $1.00 buy placed with
alpaca-py was loaded by a node's start-up reconciliation, with no lookback, with its venue order ID,
`ACCEPTED`, price, quantity, side and type, through the open-order read that now has no window.

ADR 0036 records the order-management slice, one change at a time, starting with cancel-all.

**Feed (2026-11-10).** The first slice's "the IEX feed only" no longer holds: a subscription or bars
request names its feed in params and gets IEX when it names none (ADR 0055).

## Evidence

* Split from ADR 0009 on 2026-11-10 with no change of decision: the text above is ADR 0009's own, moved,
  except the title, the Context and the Considered Options. The lines below keep their dates and how each
  was checked; the adapter guide, community adapter and second-reader lines are kept whole in each record.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing lost; its three gaps are fixed.

Read 2026-11-09 (fetched; fetches are model-summarised. Pins resolved by `scripts/canon`).

* https://nautilustrader.io/docs/latest/developer_guide/python_adapters — the out-of-tree adapter path and
  its base classes; cache-backing limit.
* https://github.com/nautechsystems/nautilus_trader/issues/5462 — open; `Equity` is whole-share only and the
  maintainer wrote on 2026-10-26 that he prefers to keep that contract (found by the second reader).
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

For the execution slice, read 2026-11-10:

* `packages/canon-alpaca/src/canon_alpaca/execution.py` at `12f19a0` — the config's refusals, and
  `TradingClient` and `TradingStream` built with `paper=True` and no other argument but the key pair
  (read). `uv run --locked pytest`: 175 passed, 2 skipped, both paper tests (run).
* `mise run paper:test` on `main` at `c87ea07`, 2026-11-10, during the regular session — 2 passed;
  the output held neither key (run).
* `test_paper_fill_ids.py`, 2026-11-10, six runs, passed: fills and partial fills at Alpaca (ADR 0024; run).
* `test_paper_matrix.py`, 2026-11-10: upstream's `DataTester` and `ExecTester` rows, in the README (run).
* A second reader after landing, 2026-11-10: a report request with no window read only Alpaca's newest 500
  orders of every status, so 500 newer closed orders hid an older working order from start-up
  reconciliation, and an open-only request applied its window to the open orders. Each reproduced with a
  test and fixed: open orders are read first, on their own, with no window, up to 500, as upstream's
  Kraken spot reads its open orders before its windowed history (`request_order_status_reports_checked`,
  `crates/adapters/kraken/src/http/spot/client.rs` at the pin, fetched, raw); "Get All Orders" gives the
  limit ("Defaults to 50 and max is 500") and newest-first order
  (https://docs.alpaca.markets/us/reference/getallorders-1.md, fetched, raw).
* A second reader after landing, 2026-11-10: a subscription after the market data stream ended by itself
  was recorded on a stream that was not running; a client given to a constructor was dropped at
  disconnect, so a reconnect built one from the environment in its place; and a shielded worker call
  whose hook was cancelled left its later error "never retrieved". Each reproduced with a test and fixed:
  the subscription fails, naming the "No restart" limit; a given client is kept, as the adapter guide
  closes only what `_connect` creates (`docs/developer_guide/python_adapters.md` at the pin, "Bind
  resources at startup", "Disconnect and drain tasks", fetched, raw); and the call's outcome is read when
  it ends (asyncio's `shield` leaves the inner task running).
* The reviewer benchmark's triage, 2026-11-10: a fractional position in another symbol failed a report
  asked for one; a position in an instrument not loaded failed start-up ("instrument missing from cache",
  observed); a non-numeric value raised `InvalidOperation` past the closed-order skip; an order replaced
  between the open and the windowed read was reported twice. Each reproduced with a test and fixed:
  positions are read for the instrument asked and only for loaded instruments, as Coinbase's
  `generate_order_status_report` keeps to its bootstrapped instruments (`crates/adapters/coinbase/src/
  execution.rs` at the pin, fetched, raw); a value that is not a number is a `ValueError`; a working copy
  whose replacement is in the reads is not reported.
