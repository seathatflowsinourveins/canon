# canon-alpaca

An out-of-tree NautilusTrader adapter for Alpaca, built on `alpaca-py`.

**Status: data slice and first execution slice, paper only.** It loads US equities as instruments, serves
historical bars on request and streams quotes and trades, all against Alpaca's paper data. Its execution
client sends market, limit, stop and stop-limit orders for US equities to Alpaca's **paper** Trading API, cancels them one
at a time or all of an instrument's at once, turns Alpaca's trade updates into order events and fills, and
reports orders and positions for reconciliation at start-up. Its config refuses anything but paper
trading. The offline tests pass, and the paper integration tests run with `mise run paper:test` (see
[Tests](#tests));what each has shown against Alpaca is in ADR 0030, ADR 0036, ADR 0038 and ADR 0042.

NautilusTrader has no official Alpaca adapter, and its maintainer has said one is not planned in the near
term. The decision to build one, and the alternatives, are in
[ADR 0009](../../docs/adr/0009-trading-foundation.md).

## Module layout

Follows upstream's adapter template, `examples/live/_template/` at the pinned NautilusTrader commit
(`d53849b216e2`, tag `v2.0.0rc8`). Each module's header names the upstream contract it implements.

| Module | Holds | State |
|---|---|---|
| `constants.py` | Venue identity (`ALPACA`), credential variable names, feed, price increment, each with its source | Built |
| `providers.py` | `AlpacaInstrumentProvider`: US equities from Alpaca's `/v2/assets` as `Equity` instruments | Built |
| `data.py` | `AlpacaDataClient` (`MarketDataClient`): `_request_bars`, `_subscribe_quotes`, `_subscribe_trades` and their unsubscribes | Built |
| `factories.py` | `AlpacaDataClientFactory`, `AlpacaExecutionClientFactory` | Built |
| `execution.py` | `AlpacaExecutionClient` (`ExecutionClient`): account state on connect, `_submit_order`, `_modify_order`, `_cancel_order` and `_cancel_all_orders` for market, limit, stop and stop-limit orders, trade updates as order events and fills, order and position status reports; `AlpacaExecutionClientConfig`, which refuses anything but paper trading | Built (first slice) |

The template is LGPL-3.0; copying from it is a licensing decision for the owner, so it was read for shape
only and this package's code is written against the documented interface.

## Use

Register the factory under a client name and say which instruments to load:

```python
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.config import DataClientConfig, InstrumentProviderConfig

data_clients = {
    "ALPACA": DataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
    ),
}
data_factories = {"ALPACA": AlpacaDataClientFactory}
```

A subscription or bars request names its Alpaca feed in its params, and gets IEX when it names none
([ADR 0055](../../docs/adr/0055-alpaca-data-feed-named-in-params.md)):

```python
self.subscribe_quotes(instrument_id, params={"feed": "boats"})  # in a strategy
self.request_bars(bar_type, start=start, end=end, params={"feed": "boats"})
```

Register the execution client the same way, with its own config class:

```python
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory

exec_clients = {
    "ALPACA": AlpacaExecutionClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
    ),
}
exec_factories = {"ALPACA": AlpacaExecutionClientFactory}
```

`AlpacaExecutionClientConfig` refuses `paper` other than `True`, any `url_override`, any other keyword, and
any value naming Alpaca's live host (`api.alpaca.markets`, from alpaca-py's `BaseURL`), with an error that
names the rule: paper trading only, AGENTS.md rule 8 and ADR 0009. NautilusTrader's own
`ExecutionClientConfig` drops unknown keywords silently, so the client refuses any other config class.
Whatever the config holds, the client builds alpaca-py's `TradingClient` and `TradingStream` with
`paper=True` and nothing else. The risk engine denies a market order while it has no price for the
instrument, so run the data client on the same node.

`load_all=True` loads every active US equity instead; its `filters` may set `exchange` or `attributes`
(the `GetAssetsRequest` fields), and `status` only to `active`. `filter_callable` is refused when the
provider is built: NautilusTrader v2's Python `InstrumentProvider` never applies it, and upstream's own
Binance adapter refuses it in v2. Node configuration itself belongs in `src/canon_trading/`, not here.

Credentials come from the environment: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, holding a **paper** key
pair. alpaca-py 0.45.0 reads no environment variable itself; these are the names Alpaca's own CLI and MCP
server read. They are read in the client's `_connect`, never stored in a config, and never logged. If they
are missing, the connect fails and the node log names both variables. On a Mac the pair is entered once
with `mise run paper:store`, kept in the login keychain, and put in these variables only for
`mise run paper:test` ([ADR 0029](../../docs/adr/0029-paper-keys-in-the-login-keychain.md)).

## Contract

From upstream's Python adapter guide (`developer_guide/python_adapters`):

* Subclass `DataClient` or `MarketDataClient`, and `ExecutionClient`, from `nautilus_trader.live.clients`.
* Provide `DataClientFactory` and `ExecutionClientFactory` subclasses. `create(*, name, config, cache, clock)`
  returns the client synchronously; execution factories also receive `trader_id`.
* Name classes `Alpaca<Role>`: `AlpacaDataClient`, `AlpacaDataClientConfig`, `AlpacaDataClientFactory`, and
  the same for execution. No `Live` prefix. The data client has no adapter config fields, so it uses a plain
  `DataClientConfig` and there is no `AlpacaDataClientConfig` yet. `AlpacaExecutionClientConfig` has none
  either; it exists to refuse what is not paper trading.
* Execution clients take `trader_id`, `venue`, `account_id`, `account_type` and `oms_type` at
  construction, implement the four reconciliation hooks, and emit events through `generate_*`. Here the
  account is `ALPACA-PAPER`, a `CASH` account with `NETTING` positions.
* Keep constructors free of tasks and network resources. Schedule background work with `self.create_task`
  and close resources in `_disconnect`. Here the alpaca-py REST clients are built in `_connect`; the data
  stream is built on the first subscription's feed and its thread started then, and the thread is
  stopped and joined in `_disconnect`. A REST client given to a client's constructor is the caller's:
  every connect uses it, and none is built from the environment in its place. A given stream serves one
  connect that runs it, since alpaca-py's stream keeps its subscriptions and websocket between runs; a
  later connect fails, naming why. A given stream's feed cannot be read, so it serves only subscriptions
  that name no feed. A worker call whose hook was cancelled has its outcome read when it ends, so
  its error is not left "never retrieved".
* Keep node configuration and strategies out of this package. They live in `src/canon_trading/`.

The class names, import paths and factory signatures above are checked against the installed pinned package
by `tests/test_upstream_contract.py`. If that test fails after a pin moves, re-read the guide first.

Known limits of custom Python clients upstream: no Redis or PostgreSQL cache backing; clients emit
`CustomData` only (no component `publish_message` or topic subscription).

## Known limits of this slice

Each is stated where it is implemented, with its source.

* **Whole shares only.** NautilusTrader's `Equity` cannot hold a fractional quantity (ADR 0030). Alpaca's
  `fractionable` flag is kept in `Equity.info`; a fractional size in market data is refused, not rounded.
* **One price increment, 0.0001.** Alpaca's assets endpoint gives no price increment for US equities, and
  Alpaca's order rule is price-dependent: at most 2 decimals at or above $1.00, 4 below. `Equity` takes one
  increment, so the finer one is used and market data is never rounded: a bar, quote or trade price
  finer than 0.0001 is refused, not rounded (`Instrument.make_price` would round it silently). The
  instrument does not stop a sub-penny order at or above $1.00; the execution client denies one (below).
* **Quotes need both sides.** A quote with a zero or negative bid or ask price is dropped, since a
  `QuoteTick` cannot hold a one-sided quote. A price that is not a finite number is not one-sided: that
  quote is dropped as malformed and logged as an error, as any other quote that cannot be converted. Outside market hours and on thin names such quotes can be
  most of them, so the first per symbol is logged as a warning with the reason, then the running count
  each time it reaches 10, 100, 1000 and so on, and the total on disconnect. The count restarts when the
  symbol is unsubscribed.
* **Quote sizes are in round lots**, as Alpaca's stream schema states, not shares. Alpaca gives no lot size
  to convert with.
* **Bars:** `LAST`, `EXTERNAL`, MINUTE, HOUR, DAY or WEEK (DAY and WEEK at step 1 only); stamped at the
  open by Alpaca, so `ts_event` is the open and `ts_init = ts_event + interval`, per NautilusTrader's bar
  timestamp convention, where `interval` is the bar's fixed length: 24 hours for a day, 168 for a week.
  `ts_init` is not the end of trading. Alpaca opens daily and weekly bars at midnight New York time
  (UNVERIFIED until the paper test runs), so a daily bar's `ts_init` is 24 hours later, hours after the
  16:00 close. Across a clock change that is not midnight: a bar spanning the autumn change ends at 23:00
  New York, one spanning the spring change at 01:00 the next day. US clocks change early on Sundays, so
  trading-day daily bars never span a change; weekly bars do.
* **Trades** carry no aggressor side (`NO_AGGRESSOR`). Alpaca's trade cancels, errors and corrections
  are logged as warnings naming the symbol and trade ID; a trade tick already emitted cannot be withdrawn
  or corrected.
* **Feed:** named in a subscription's or bars request's params, `{"feed": "boats"}`, the channel the
  adapter guide documents for adapter-specific options, as upstream's Databento (`schema`) and OKX
  (`rpi`) adapters choose a venue channel there; IEX when the params name none, so nothing that names
  none changes. Only `feed` is read; the engine's own keys (`update_catalog`) are left alone. A
  subscription may name `iex`, `sip`, `delayed_sip`, `boats` or `overnight` (Alpaca's stream URLs), a bars
  request `iex`, `otc`, `sip` or `boats` (the bars reference's `feed` enum); another name, or a value
  that is not a string (`None` too: only a missing key means IEX), is refused before anything is sent.
  The client runs one stream, built at its first subscription on that subscription's feed, and refuses a
  later subscription, quotes or trades, whose feed differs (one naming none counts as IEX), naming both;
  one already subscribed is checked too. An unsubscribe reads its feed the same way: one that names a feed
  Alpaca does not stream, or another than the stream's, names a subscription the client never made (it
  was refused), so nothing is sent and a warning says so, as the adapter guide has unsubscribe hooks
  tolerate members whose subscription failed. The engine passes an unsubscribe the params its subscription had (observed offline).
  NautilusTrader's data engine passes on only the first subscription to an instrument's quotes or trades
  (observed offline), so a second strategy naming another feed for the same one is never seen by the
  client and gets the first one's feed, unrefused.
  It never tries another feed. On paper on 2026-11-10 `boats` streamed overnight quotes, trades and bars
  and `iex` nothing after 16:00, and the pair also authenticated on `sip`; a Paper Only account is
  documented as entitled to IEX alone, so none of this is an entitlement (ADR 0055). No feed delivered
  in pre-market or after-hours on paper.
* **Timestamps** lose sub-microsecond precision: alpaca-py parses them into `datetime`.
* **No restart:** if alpaca-py's stream ends by itself, the client logs an error and does not restart it;
  a later subscription fails with an error saying so, until the client is reconnected. The end counts
  from the moment alpaca-py's `run()` returns, marked on the stream's thread before it is reported or
  the thread ends. Each quote and trade handler names the subscription it was made for, which an
  unsubscribe, a resubscribe and a reconnect each replace, so what a handler delivers after its
  subscription is gone (a message alpaca-py dispatched just before an unsubscribe, or one from a stream
  left running by a timed-out disconnect, below) is dropped. The stream's thread
  never holds the client, not even during a call, so the client is never freed on that thread.
  alpaca-py reports a refused authentication only through Python `logging`: on 409 "insufficient
  subscription" (a feed outside the account's plan) it ends the stream, and on any other refusal it
  retries without ending. The client passes alpaca-py's stream warnings and errors from its stream's
  thread to the node's log, naming the feed and the thread (a name unique in the process, so neither an
  abandoned stream nor another client's is taken for this one's), and the stream's end as an error
  naming the feed. The subscription itself has already returned by then: alpaca-py connects only after
  the first subscription and does not expose the outcome. Subscriptions and unsubscribes reach
  alpaca-py's stream one at a time, since it changes its handler maps without a lock of its own; one
  whose hook is cancelled still holds the next until alpaca-py returns, and what it did is recorded then.
* **A connect that stops part way** (failed or cancelled) drops what it built, so a subscription or bars
  request is refused as not connected; a stream thread that cannot start drops the stream built for it,
  with its feed.
* **Stream shutdown:** `_disconnect` waits `STREAM_JOIN_TIMEOUT_SECS` (10 s) for the stream thread. A
  connected alpaca-py stream sees the stop within its 5 s receive timeout and then closes the websocket,
  which `websockets` allows 10 s, so a slow close can exceed it; a stop during connect or auth waits for
  that to finish. When it is exceeded, the client clears its state and raises, the node logs the failed
  disconnect, and the daemon thread finishes on its own with its websocket still open until then. If
  the node cancels the disconnect first (a disconnection timeout under 10 s), the state is cleared all
  the same. A later connect builds a new stream; whether Alpaca accepts it while the old connection is still open is
  UNVERIFIED.
* **Bar windows:** Alpaca filters bars on their open time, inclusive at both `start` and `end` (taken from
  its documentation, https://docs.alpaca.markets/us/reference/stockbars.md; checked against the live API
  only by the paper test below). NautilusTrader keeps the bars whose `ts_init` lies in the request's
  `[start, end]` and applies no `limit` itself. So the adapter sends `start` and `end` one bar interval
  earlier, and a request returns exactly the bars whose `ts_init` (open plus one interval) lies in its
  window; the response keeps the bounds as requested. The window ends at the earlier of the requested
  `end` and the node clock's current time (`now` when there is no `end`; NautilusTrader's `request_bars`
  itself refuses an `end` after now), so the bar still forming, whose `ts_init` is in the future, is
  never returned. A request with no `start` starts at midnight New York of the day its window ends in,
  today unless it names an earlier `end`: Alpaca's reference gives "the beginning of the current day" as
  its own default but names no time zone, so the adapter supplies the start itself, and the window sent
  never ends before it starts. Both bounds move back in elapsed time, in UTC, so a clock change does not
  shift the window by an hour. A window that starts after now holds no finished bar: nothing is sent,
  and the response has no bars.
* **Choosing a window:** to get the bar for a day or week, ask for one that holds its open plus its
  length with hours to spare: from noon New York on the bar's last day to noon the next day works on
  every day, clock changes included. A window ending exactly at midnight misses a weekly bar that spans
  the spring change (its `ts_init` is 01:00), and a window inside a day's session returns no daily bar.
* **Bar requests:** `limit` goes to Alpaca as it is and returns the *earliest* bars of the window, since
  Alpaca sorts ascending by default. With no `start`, the window is today in New York so far: for MINUTE
  and HOUR, the first `limit` bars that closed since midnight; for DAY, the previous calendar day's bar,
  whose `ts_init` is this midnight, so none on a Monday or the day after a holiday; for WEEK, last
  week's bar on a Monday (from 01:00 after the spring change; none on the Monday after the autumn
  change, whose bar ends at 23:00 the Sunday before) and none on other days. alpaca-py's
  `sort` parameter (`Sort.DESC`) would return the latest instead; it is not adopted here. If
  any one bar in the response is refused (a fractional volume, a price finer than 0.0001), the whole
  request fails. A refused or failed request reaches the strategy as a response with a `failure_reason`
  and no bars. Requests are made one at a time, because alpaca-py's REST client holds one
  `requests.Session`; a waiting request holds an `asyncio.Lock` on the node's loop, not a worker thread,
  and the lock is released only when the HTTP call returns, so a request cancelled mid-call (as on
  shutdown) still keeps the next one waiting. A request the loop's executor refuses (one shut down)
  fails and releases the lock.
* **HTTP timeout:** alpaca-py sets none and offers no setting for one (`RESTClient._one_request` calls
  `requests.Session.request` without `timeout`). Every alpaca-py REST client the adapter builds, the
  provider's, the data client's and the execution client's, gets requests' own example timeout, 3.05 s to
  connect and 27 s between bytes, through a transport adapter mounted on alpaca-py's private
  `RESTClient._session` ([ADR 0039](../../docs/adr/0039-alpaca-http-timeout.md)); a client without that
  attribute is refused at start-up. A bar or asset request that stalls now fails with a timeout, a
  transport error, instead of waiting until the operating system ends the connection; at connect the
  node's own connection timeout (20 s in the paper tests) can fire first, and a response that keeps
  trickling bytes is not bounded, since the read timeout is the wait between bytes. alpaca-py does not
  repeat a timed-out request. Before this, on paper on 2026-11-10, a node stopped while an execution call
  hung reported "Failed to disconnect execution clients: ALPACA" and its run never ended.
* **Instrument loading:** only active assets load; an inactive asset, or an asset whose symbol is not the
  one requested, is refused with its ID named. Assets Alpaca marks not tradable still load, because their
  market data does not depend on it; `tradable` is kept in `Equity.info` for the execution slice. `load_ids`
  fetches one asset per request, in sequence, inside the connect, so a long list can exceed the node's
  connection timeout; every ID is attempted, network errors included, and the error names each that
  failed. `load_all` fails as a whole if alpaca-py cannot parse any one asset record (its models validate
  the full list).

## Known limits of the execution slice

Each is stated in `execution.py`'s header, with its source.

* **Order types:** market, limit, stop and stop-limit, `DAY` or `GTC`, whole shares, simple orders.
  Alpaca marks IOC, FOK, OPG and CLS for whole-share market and limit orders "Yes*", "contact the sales
  team", and for stop and stop-limit orders "No"
  (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Order Types vs Supported Time in Force"), so
  they are denied. So are trailing, post-only, reduce-only, quote-quantity and display quantity orders (a stop-market's
  display quantity included), a
  stop trigger type other than `DEFAULT` or `LAST_PRICE` (Alpaca elects a stop "on the consolidated
  print"), a limit or stop price at or above $1.00 with more than 2 decimals or below $1.00 with more than
  4 (the same page, "Sub-penny increments"), and an asset Alpaca marks not tradable. Alpaca sends no update
  when a stop is elected, so the client generates no `OrderTriggered`: an elected stop-limit stays
  `ACCEPTED` until it fills or ends. An open stop is reported with its stop price as trigger price and,
  since Alpaca's order has none, the trigger type of the node's order, or `DEFAULT` for one the node does
  not hold ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)). Denied orders never
  reach Alpaca. Not implemented: order lists.
* **Queries:** NautilusTrader's in-flight check sends `QueryOrder` for an order left `SUBMITTED`,
  `PENDING_CANCEL` or `PENDING_UPDATE` (on by default: from 10 s after start, every 2 s, for an order in
  flight 5 s, 5 retries), and its open-order check, off by default, for an accepted order Alpaca's open
  orders leave out. The client answers with the order's status report, as upstream's Binance spot,
  Bybit, Coinbase, Kraken spot and OKX adapters do; not found or a failed read sends nothing, and the
  engine asks again. Past the last retry the engine resolves an unanswered `SUBMITTED` order locally as
  rejected; before this answer it always did, which on paper rejected an order Alpaca then accepted.
* **Extended hours:** `submit_order(order, params={"extended_hours": True})`, the way upstream's adapters
  take venue order flags, sends a limit order, `DAY` or `GTC`, eligible for pre-market, after-hours and
  overnight; any other order so marked, an unknown params key and a non-boolean value are denied before
  Alpaca, which rejects them ("Orders at Alpaca", "Extended Hours Trading";
  [ADR 0038](../../docs/adr/0038-alpaca-extended-hours.md)). Overnight quotes reach a strategy through a
  data subscription that names the `boats` feed (ADR 0055).
* **Modify** replaces the order on Alpaca ("Replace Order by ID") and generates `OrderUpdated` only when
  Alpaca's stream sends `replaced` for it, with the replacement's ID, quantity and prices; a success answer
  alone does not, since Alpaca says it "does NOT guarantee" the replacement. The replacement gets a client
  order ID Alpaca makes, so its updates and reports are traced back to the node's order through its
  `replaces` links, cached by Alpaca's order ID (an order made outside the node can have an empty client
  order ID) and walked again after a restart. A working replacement whose links cannot be read fails
  start-up, naming it; a closed one is skipped with a warning. A replacement Alpaca rejects after
  sending `replaced` for the old order had taken over: the node's order is rejected, not put back to its
  old terms, and so is its report. A replacement's `rejected` that arrives
  before `replaced`, when Alpaca shows the old order replaced by it, ends the order the same way, and so
  does `replaced` whose replacement Alpaca already shows rejected, canceled or expired; a replacement
  replaced again before it was read is followed to the order now working; a rejected replacement the node
  holds is reported as the order's whatever a read of the order before it gives. Alpaca's refusal,
  `order_replace_rejected` and any other replacement's `rejected` are `OrderModifyRejected`; a modify
  without a venue order ID, to a fractional or zero quantity, to a price that is not positive or to a
  sub-penny price is denied before Alpaca. A report whose replacement Alpaca does not find is none, as for
  an order Alpaca does not find. What Alpaca's pages leave open was
  observed on paper and is asserted by `test_paper_replace.py`
  ([ADR 0042](../../docs/adr/0042-alpaca-adapter-modify.md)).
* **Cancel-all** cancels the node's open and in-flight orders for the command's instrument, and side if
  it names one, each by ID, one at a time, as a single cancel does; one already pending cancel is canceled
  again, after the others, since its first cancel may have had no answer;
  a refused one gets `OrderCancelRejected`, and after a refusal or a failure the rest go on. Alpaca's "Delete All Orders" cancels every open order of the
  account, so it is not called. A strategy sends the command only with `strategy_only=False`, and it then
  covers every strategy's orders; with the default, the strategy cancels its own orders one by one
  ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)).
* **Outcomes:** a 403 or 422 from "Create an Order" (https://docs.alpaca.markets/us/reference/postorder.md)
  rejects the order once Alpaca confirms it does not hold it (see Retries); any other failure leaves it `SUBMITTED`, an unknown outcome under NautilusTrader's
  policies (https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md, "Command outcomes"),
  for a trade update or reconciliation to resolve. A cancel answered 204 waits for Alpaca's `canceled`
  update; one answered 422 is a cancel rejection unless Alpaca shows the order pending cancel, when its
  `canceled` update confirms it, or canceled, when the order is canceled from that read if Alpaca shows
  no more filled than the node holds (else it is left, with a warning, for reconciliation), since a
  dropped stream never sends the update, as upstream's Bybit adapter cancels from an order it reads
  canceled (https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md). On paper, after hours,
  a second cancel of an order already canceled was answered 204, not the documented 422 (TC-E44,
  2026-11-10), so an order Alpaca canceled while the stream was down gets no `canceled` update; the
  in-flight check's `QueryOrder` then reads it canceled and the engine cancels it at Alpaca's time
  (offline). What the regular session answers is UNVERIFIED. An outcome found after an `await` (an
  answer, a lookup) is checked against what the stream applied meanwhile: an order that has ended gets
  no second rejection and no cancel rejection, and a cancel refused for an order Alpaca shows filled is
  settled by its fill. An acknowledgement that comes while a cancel or modify sent before it is pending
  is kept, gives a later cancel or modify Alpaca's order ID, and `OrderAccepted` follows a refusal of
  that cancel or modify unless a fill came first. An update for an order the engine has ended from a
  query's report is dropped, fills included, since the engine infers the fills that report shows.
* **Wash trades:** Alpaca rejects an order that "could potentially interact" with one of the account's
  own open orders on the other side, "with the HTTP status code 403 (Forbidden)", and says the protection
  "also applies to your paper trading account" ("User Protection", "Preventing Wash Trades",
  https://docs.alpaca.markets/us/docs/user-protection.md). Its table rejects a new order always when
  either order is a market or stop order, and a limit or stop-limit pair only when the buy limit price
  is at or above the sell limit price; bracket, OCO and trailing stop orders are excepted, and this slice
  sends none of them. The order is rejected like any 403 from "Create an Order", whose own page documents
  403 only as "Buying power or shares is not sufficient"; the strategy sees `OrderSubmitted`, then
  `OrderRejected` with Alpaca's message, on paper 403 "potential wash trade detected. use complex orders"
  (2026-11-10: a limit sell while the account's market buy was still open, and buys while the strategy's
  market sell was open; ADR 0037). The adapter does not check for a wash trade before sending, and
  neither the error code nor the message is on a page read.
* **Fills** come from the trade updates stream only, one per `fill` or `partial_fill` event, with the
  event's own `qty` and `price`; the node's order ends when its fills reach its quantity, on the client's
  own count, so a fill on an order since replaced that arrives after the replacement's is still applied,
  and a fill that cannot be applied ends nothing (https://docs.alpaca.markets/us/docs/websocket-streaming.md). The trade ID
  is Alpaca's `execution_id`, which that page shows only in its example; a fill without one is logged and
  not applied. Prices are kept at the precision Alpaca sends (the example has seven decimals). Alpaca
  reports no commission and no liquidity side, so fills carry `None` and `NO_LIQUIDITY_SIDE`.
* **Fill reports** come from Alpaca's `FILL` activities, `GET /v2/account/activities/FILL`, called through
  alpaca-py's generic `get` since 0.45.0's `TradingClient` has no method for it
  ([ADR 0024](../../docs/adr/0024-alpaca-fill-history.md)). One report per activity: the trade ID is the
  UUID after `::` in its `id`, which on paper is the trade update's `execution_id` for the same fill
  (`test_paper_fill_ids.py`), so a fill seen on the stream and in a report is one fill; the price exactly
  as Alpaca writes it; the time its `transaction_time`; commission zero dollars (Alpaca's fill has none;
  upstream's TC-E85 expects a zero from a stream that omits fees) and `NO_LIQUIDITY_SIDE`; no client order
  ID (an activity has none). A fill listed under an order since replaced is reported on the order that
  replaced it, as the stream applies it, when that order shows it counts it: its `filled_qty` equals the
  chain's listed fills and, if it lists fills of its own, its newest activity's `cum_qty` equals its
  `filled_qty`. Fill reports asked for one order cover its replace chain, as the bulk request reports
  them. Whether a replacement counts the fills before it is UNVERIFIED (ADR 0042): one that
  shows it counts only its own keeps its own fills and leaves the earlier ones to the engine, and
  anything else leaves the chain's fills to the engine. With no fill of its own listed, a `filled_qty`
  equal to the earlier fills is read as counting them, which a fill of its own not listed yet would
  also show (UNVERIFIED). An activity listed twice is one fill, counted and reported once, as
  NautilusTrader's order takes one fill per trade ID; one trade ID listed with two quantities or prices
  leaves that order's fills to the engine. Whether Alpaca ever repeats a row is UNVERIFIED. Alpaca
  returns activities newest first, at most 100 a page; the window and
  the instrument are applied here, on `transaction_time` and `symbol`, since what Alpaca's `after` and
  `until` compare for a fill is not documented and the endpoint has no symbol filter. Paging stops at the
  first fill before the window, or after 10 pages (1,000 fills, twice the 500 orders the order reports
  hold) with a warning. `sell_short` is a sell. A fill in a symbol with `/` (crypto), a fractional fill,
  an `id` not in `::` form, or a value that cannot be read is skipped with a warning. If the history
  cannot be read at all (an error, a body that is not JSON or not a list), the error is logged and no fill
  is reported, so the engine infers fills from the order reports, as before fill reports. A full page
  whose last activity has no readable `id`, which the next page needs as `page_token`, ends the read with
  a warning; what was read is kept.
* **Fill activities can lag the stream:** on paper, a node started about a second after an order's last
  fill found only the first of its two fills listed (2026-11-10). NautilusTrader then rebuilt the order as
  partially filled and added a synthetic order for the missing share ("Adjusting fills ... with position
  reports"). So an order's fills are reported only when they can be shown to be all of them: its newest
  listed activity leaves nothing to fill (`leaves_qty` 0) and the listed fills add up to its `cum_qty`;
  or else Alpaca's order, read from its orders endpoint, shows no more filled than they add up to. Listed
  fills short of `cum_qty`, an order that cannot be read or built, or more filled than listed: that
  order's fills are not reported, with a warning, and the engine infers them instead. This holds
  whatever the fills' age, since how long the lag lasts is UNVERIFIED. The cost
  falls on orders whose newest listed fill leaves shares to fill or lacks `cum_qty` or `leaves_qty` (an
  order filled in full and listed in full costs none): one `get_order_by_id` for one such order; for
  more, one `get_orders` for the account's newest 500 orders and one `get_order_by_id` for each order
  not among them, one at a time.
* **Reports:** every open order however old, up to 500 (more and the read is logged as possibly
  truncated), read first, on its own, with no window, as upstream's Kraken spot adapter reads its open
  orders before its history, and, unless only open orders are asked for, the
  orders submitted in the engine's window, since Alpaca's `after` and `until` filter on submission time
  and a read of every status returns only the newest; at most 500 orders per request, alpaca-py's
  documented maximum, newest first; a full page
  is logged as possibly truncated. An order seen working in the first read whose replacement is in the
  second is not reported twice: the replacement is, under the node's ID, unless Alpaca shows the order
  `pending_replace`, when the order is, at its own terms. An open order with a type, side, class, status, quantity, time in force
  or client order ID this slice does not map fails start-up, and its reason is logged; so does an open
  order in anything but US equities, and a fractional position (`Equity` is whole-share only). A closed
  order of that kind is skipped with a warning naming why. Positions in anything but US equities, or in an instrument the client has not loaded, are not
  reported (NautilusTrader fails start-up on the second, "instrument missing from cache", observed; as
  upstream's Coinbase adapter keeps its reports to what it loaded), and a report asked for one instrument
  reads no other's. A value that is not a number skips a closed order and fails an open one, as any
  value this slice cannot map. Alpaca does not say whether a short
  position's `qty` is negative, so the side comes from `side` and the size is its magnitude. Each loaded
  instrument Alpaca lists no position in is reported flat: Alpaca's positions endpoint lists only open
  positions, and NautilusTrader takes only "An explicit flat report" as evidence of flat. Without it,
  fills inferred from part of the order history left an `EXTERNAL` position Alpaca did not hold. With
  `load_all`, that is one flat report per US equity on each position report request. A flat report from
  a read taken just before a fill lands is wrong for that moment; whether a running node's periodic
  position check can act on that is UNVERIFIED.
* **Account:** a NautilusTrader cash account whose USD balance is Alpaca's `cash`, all free, read at
  connect and whenever a strategy calls `query_account`; Alpaca's stream pushes no account update, so the
  balance is as of the last read
  ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)); cash finer than a cent is reported to the cent, with a warning. Buying power, margin and
  shorting are not modelled; `equity`, `buying_power` and the
  account number are kept in the account state's `info`.
* **Stream:** alpaca-py does not expose the stream's `listening` acknowledgement, so updates sent before
  it is listening are missed. alpaca-py reconnects the stream by itself after a websocket or connection
  error, with backoff, logging only through Python `logging`, and updates sent while it was down are not
  replayed; none of upstream's adapters at the pin catches up on a reconnect either. So a default node
  re-checks only orders in flight (the in-flight check, through `QueryOrder`); an accepted order whose
  update was lost is re-checked only with the open-order check on, else at the next start. A node that
  holds orders for long enough to lose an update sets `LiveExecutionEngineConfig(open_check_interval_secs=...)`
  within upstream's recommended "5-10s" (how_to/configure_live_trading), as the paper node does at 10 s
  (`src/canon_trading/paper_node.py`); it costs one open-orders read per interval. Updates for orders the node does not hold are logged and
  dropped, and one alpaca-py hands over unparsed is logged as an error. Events this slice does not map
  (`done_for_day`, `stopped`, `suspended` and the rest) are logged as warnings. If alpaca-py's `run()`
  returns, the client logs an error and does not start the stream again, and shutdown waits `STREAM_JOIN_TIMEOUT_SECS` for the stream thread and, at the same time,
  the task applying updates: up to 1 s for an update being applied to finish, then up to 2 s after its
  cancel; each by a deadline on the node's loop with no worker thread (a wait for a thread through
  `asyncio.to_thread` needs a free worker of the executor every alpaca-py call runs in), as upstream's
  Coinbase adapter bounds its task drain (`TaskGroup::finish_shutdown`, 1 s and 2 s). A wait that runs
  out fails the disconnect, and the client logs why, which NautilusTrader's own message does not say.
* **Retries:** alpaca-py retries a request answered 429 or 504 up to three times, a submit or cancel
  included (`RESTClient._request`), and cannot be told not to. A repeated submit carries the same
  `client_order_id`; what Alpaca answers it is not documented on the pages read (UNVERIFIED). So before a
  403 or 422 rejects an order, the client asks Alpaca for it by client order ID and accepts it if Alpaca
  holds it; before a 422 rejects a cancel, it asks for the order and generates nothing if Alpaca shows it
  pending cancel, and `OrderCanceled` if canceled. A timeout (ADR 0039) is a transport error: a connect timeout on a call's
  only attempt proves nothing was sent, so a submit is rejected, a modify modify-rejected and a cancel cancel-rejected, each
  "not sent"; any other timeout leaves a submit `SUBMITTED` and a cancel as it was.

## Tests

`tests/` beside `src/`, run with pytest in `importlib` import mode from the repo root (`uv run --locked pytest`).
Upstream's template is covered by integration tests for factory registration, a market fill and shutdown.
The execution client has all three, offline. The data client has registration and shutdown; it places no
order, so it has no fill test, and its data delivery is tested instead. Each has paper tests:

| File | Covers | Network |
|---|---|---|
| `test_parsing.py` | Asset to `Equity`; bar, quote and trade conversion; bar type support; missing credentials | None |
| `test_provider.py` | Asset queries and filters, load by ID, failures named, other venues refused | None |
| `test_data_client.py` | A real `LiveNode`: registration through config and builder, owned and hosted launch, bars, quotes and trades into strategy and cache, hourly and daily bar windows, `limit`, open-ended requests, concurrent and cancelled bar requests, request failures as `failure_reason`, unsubscribes that never subscribed or failed, late data for an unsubscribed symbol dropped, late data from a stream left behind by a timed-out disconnect dropped after a reconnect, a resubscribe delivering again, trade cancels and corrections logged, one-sided quotes dropped and counted and a NaN price dropped as malformed, a TICK bar request refused with the adapter's reason, the feed named in params (the default IEX, a named feed on the stream and the bars request, other keys left alone, a second feed refused, a name or value Alpaca does not take refused before anything is sent, a feed Alpaca refuses logged naming it and later subscriptions refused), a repeat subscription after the stream ended refused, concurrent unsubscribes sending one, a reconnect waiting for a bar call a cancelled request left running, a given stream that never ran serving the next connect, windows with no `start` (an earlier `end`, either clock change), a request the executor refuses releasing the lock, a disconnect cancelled by the node clearing state, the stream thread holding no reference to the client inside a handler, the stream stand-in connecting only once something is subscribed, as alpaca-py's does, a stream ending by itself logged (and an abandoned one not) and a later subscription refused, clients given at construction kept across a reconnect, a cancelled bar call's later error read, shutdown joining the stream thread or clearing state when it does not stop, stream handlers not keeping the client alive, paper and IEX client construction; a cancelled stream call holding the next until alpaca-py returns and a refused one recording nothing, a subscription after the stream's `run()` returned refused while its thread winds down, a connect that fails or is cancelled part way and a stream thread that cannot start leaving nothing behind, `feed: None` refused, an unsubscribe's feed read as a subscription's (and the engine passing it the subscription's params), a window starting after now sending nothing, another client's abandoned stream's log lines not taken for this one's, a quote from before an unsubscribe dropped after a resubscribe, an unsubscribe after a subscription refused for its feed tolerated, a subscription while disconnecting refused as such, no hook waiting on a call stuck in an ended stream, a window inverted in the past still sent | None |
| `test_paper_feed.py` | ADR 0055 through a real node: a subscription naming `boats` streams on `v1beta1/boats` and its quotes reach the strategy (overnight only, from Alpaca's calendar); one naming none streams on `v2/iex`, as before (any hour). Shaped on TC-D20 and TC-D71, by a small strategy, since v2.0.0rc8's Python `DataTesterConfig` takes no `subscribe_params` | Paper only; places nothing; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_integration.py` | One asset and a week of daily bars from Alpaca's paper environment through a real node, from midday New York on a fixed trading day: the first bar must be the one that opened at that day's midnight, which Alpaca returns only because the adapter shifts the window | Paper only; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_execution_client.py` | A real `LiveNode` with both clients: registration through config and builder, owned and hosted launch, a market order that fills, partial fills, a limit order canceled, cancel-all by ID (side filter, a refused cancel, a failed cancel, nothing to cancel), rejections by HTTP status and by trade update, a cancel refused, a cancel refused for an order Alpaca canceled while the stream was down, a refusal whose lookup alpaca-py cannot build (submit, cancel, modify), a replacement rejected after `replaced`, fills listed before a replace, clients given at construction kept across a reconnect, a cancelled call's later error read, stop and stop-limit orders sent with their prices, modify (`OrderUpdated` only on Alpaca's `replaced`, a fill on the replacement reaching the node's order with the client's map full and emptied, two replaces in a row, reports of a replacement under the node's ID, refusals, a refused repeat, `order_replace_rejected`, a rejected replacement, local denials, a stop's trigger price, a replacement that cannot be read, a replacement whose prior order cannot be read, open or closed, two orders with an empty client order ID), orders denied before Alpaca (type, time in force, stop trigger type, sub-penny limit and stop prices, not tradable), an unknown submit outcome left `SUBMITTED`, an update for an unknown order dropped, start-up fills from Alpaca's fill activities (trade IDs and prices, paging, the window, one order, an activity it cannot map skipped, a page whose last activity has no `id`, a body that is not JSON, fills not all listed whatever their age or with no `cum_qty` and `leaves_qty`, the order read to check them and one alpaca-py cannot build, an activity listed twice), start-up reconciliation of an open order (older than 500 newer closed orders, or before an open-only check's window), a filled order, a position and open stop and stop-limit orders, an unmapped open order failing start-up, account state on connect, paper-only client construction, shutdown joining the trade stream thread; the config's paper-only refusals, and a node refusing a plain `ExecutionClientConfig`; HTTP timeouts (a local server that never answers: the call ends, the next is answered, the run ends; a connect timeout on the only attempt rejects a submit or cancel as not sent, a read timeout or a repeated attempt leaves it unknown; a session holding another timeout, or the adapter on HTTPS only, gets the package's on both schemes); `QueryOrder` from the engine's in-flight check (a submit Alpaca took with no answer, a cancel answered 204 for an order Alpaca canceled unseen), an acknowledgement during a pending cancel, a refused submit Alpaca holds closed, one rejection from the stream and the answer, no cancel rejection after a fill, a fill that cannot be applied, positions in another or an unloaded symbol, values that are not numbers, a late fill on a replaced order, a replacement rejected before `replaced`, replaced again before it was read, or adopted and then rejected, an order replaced between the two reads, one order's fill reports over its chain, a given client across a reconnect with a call still running, a given stream after a connect that failed early, a modify to zero, a held stop's trigger type | None |
| `test_paper_execution.py` | DAY limit buys of 1 AAPL far below the market on Alpaca's paper Trading API through a real node: one submitted, accepted and canceled; upstream's `ExecTester` running rows of its Execution Testing Spec: TC-E41, a limit buy and a stop buy canceled by `CancelAllOrders` on stop, TC-E20 and TC-E22, a stop and a stop-limit buy accepted at the prices sent and reported back the same, and TC-E30, whose one-tick amend the client denies as sub-penny; a $1.00 buy modified to $1.01 through a node, then canceled; TC-E84, an open order placed with alpaca-py loaded by a node's start-up reconciliation; and TC-E44's venue side, a canceled order canceled again | Paper only; cancels every order it places; each places nothing and fails if the paper account already holds an open AAPL order or position; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_fill_ids.py` | ADR 0024's test, with alpaca-py directly, not through a node: a market buy of 2 AAPL and a market sell of 2 on Alpaca's paper Trading API; every fill and partial fill update's `execution_id` must be the UUID after `::` in one of the order's `FILL` activity `id`s | Paper only; needs the regular session and skips outside it; stops if the account holds any open order or position; sells what it bought; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_matrix.py` | Upstream's `DataTester` and `ExecTester` through a real node on Alpaca's paper environment: the rows in "Upstream's test matrices against paper" | Paper only; needs the regular session for the stream and fill rows; stops if the account holds any open order or position; cancels and closes what its node opened; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_flat_reconciliation.py` | TC-E85 on the shared account's past orders, with no lookback: with Alpaca holding no position, start-up must leave none open, through the explicit flat reports (no `EXTERNAL` position) | Paper only; places nothing, so needs no session; stops, touching nothing, if the account holds any open order or position; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_fill_reports.py` | TC-E85 with fill reports: a market buy of 2 AAPL with alpaca-py, its trade updates recorded; a node's start-up reconciliation must give the order one fill per update, with that update's `execution_id` and price | Paper only; needs the regular session and skips outside it; stops if the account holds any open order or position; sells what it bought; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_replace.py` | Alpaca's replace through alpaca-py directly: a $1.00 buy replaced with $1.01; asserts what a paper observation showed and the modify rests on (a replacement may not keep the client order ID, the answer's new IDs and `replaces`, `replaced` carrying the old order with `replaced_by`, a second replace refused) | Paper only; needs the regular session; places nothing while the account holds an open AAPL order, cancels what it opened |
| `test_upstream_contract.py` | The upstream names this README quotes still exist in the pinned packages; the execution hooks and `generate_order_filled` signature; alpaca-py's trading clients default to and target paper; the native config drops unknown keywords; alpaca-py's own REST client raises the `APIError` fields the stand-in builds; the stream stand-ins refuse and keep what alpaca-py's `StockDataStream` and `TradingStream` do; the data stream stand-in, beside alpaca-py's real `StockDataStream` run on an in-memory socket, sends an unsubscribe only while running, stops one run at a time, and misses a subscribe made while it connects; the stand-ins' `APIError`s carry their HTTP status; alpaca-py's REST clients keep a `requests.Session` at `_session` and pass no timeout (ADR 0039) | None |

The offline tests use payloads hand-built from the examples in Alpaca's docs (sources in `tests/conftest.py`)
and stand-ins for the alpaca-py clients. To run the paper tests, enter the pair once with
`mise run paper:store` and then run `mise run paper:test` from the repo root; `mise run paper:check` says
whether the pair is there (root `README.md`, "Enter the Alpaca paper keys"). The first two passed on
2026-11-10 (ADR 0030); the TC-E41 test is ADR 0036's.
Fills and partial fills have come from Alpaca outside the adapter in `test_paper_fill_ids.py` (six runs
passed on 2026-11-10, ADR 0024); `mise run paper:test` runs it, and
`mise run paper:test -s -k test_paper_fill_activity_ids` runs it alone.
Through the adapter, `test_paper_matrix.py` runs upstream's test matrices (below). Not yet tested against
Alpaca: the asset list at `load_all` scale, and a partial fill through the adapter (each tester order is
one share).

### Upstream's test matrices against paper

NautilusTrader's Data Testing Spec and Execution Testing Spec
(https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md, `spec_exec_testing.md`)
define the rows an adapter must pass for what it supports, run with the built-in `DataTester` actor and
`ExecTester` strategy, which v2.0.0rc8 exposes through `nautilus_trader.testkit`. `test_paper_matrix.py`
runs them through a real node against Alpaca's paper environment, one share of AAPL, IEX feed, during the
regular session; each test skips outside it. `mise run paper:test` runs them with the other paper tests;
`mise run paper:test -s -k "test_paper_data_tester or test_paper_exec_tester"` runs them alone. The rows
below ran on 2026-11-10 between 13:59 and 14:44 New York time: the data rows six times (TC-D70 passed five
of them), the execution rows three or four times (TC-E11 three times with its final check, TC-E41 and
TC-E81 twice, after cancel-all was added), unless the row says otherwise. TC-D70 and TC-E83 were checked
in each run's log, not by an assertion: the test cannot read the node's log.

Tester settings that differ from upstream's defaults, and why: resting orders sit $50 from the touch
(`tob_offset_ticks=500_000`, since the instrument's price increment is 0.0001); one side per run, because
Alpaca refuses an opposite-side order while the tester's opening market order is open (403 "potential
wash trade detected"; see "Wash trades"); no
reduce-only on stop, which this slice lacks; start-up reconciliation looks back one
minute on the shared account (see the reconciliation finding below); and the node keeps a post-stop grace
period, since disconnection "discards queued commands" (python adapter guide, "Disconnect and drain
tasks"): with none, a queued unsubscribe was discarded and logged as an error once.

| Rows | Outcome |
|---|---|
| TC-D03 load an instrument | Passed |
| TC-D20 subscribe quotes | Passed: 256 to 595 quotes in 30 s; quotes with bid below ask and both sizes positive, none crossed |
| TC-D30 subscribe trades | Passed, except the aggressor side: 5 to 28 trades in 30 s; Alpaca sends no aggressor, so every trade is `NO_AGGRESSOR` |
| TC-D41 historical bars | Passed: 60 one-minute bars, valid OHLC, no duplicate timestamps |
| TC-D70 unsubscribe on stop | Passed with the grace period: quotes and trades unsubscribed, no tick after, no warning or error |
| TC-D01, D02, D21, D31, D40, D60, D61, D73 | Not in this slice: no instrument requests or subscriptions, no historical quotes or trades, no bar subscriptions, no status or close; D73 not run |
| TC-D10 to D14, D50 to D53, D62, D63, D71, D72, D74, D75 | Do not apply: no order book in Alpaca's stock stream, no derivatives or options here, no custom parameters, no DeFi |
| TC-E01 market buy, TC-E80 open on start | Passed: submitted, accepted, filled; the fill's trade ID is Alpaca's `execution_id` |
| TC-E06, TC-E82 close position on stop | Passed: a market sell, filled, position closed |
| TC-E10 GTC limit buy | Passed: accepted, then canceled on stop |
| TC-E11 GTC limit sell | Passed against the tester's long. While the opening market buy was still open, Alpaca refused each sell (403 "potential wash trade detected", 6 times in about 1 s); the first sell after the fill was accepted |
| TC-E12 limit buy and sell pair | Not run: the tester runs one side at a time. Alpaca's wash-trade table refuses a limit pair only when the buy limit is at or above the sell limit, so a pair resting $50 from the touch on each side should be accepted (UNVERIFIED on paper) |
| TC-E19 DAY limit | Passed: accepted, then canceled |
| TC-E40, TC-E42 cancel, one by one on stop | Passed |
| TC-E41, TC-E81 cancel all on stop | Passed with the cancel-all ADR 0036 added. Before it, `_cancel_all_orders` raised, the orders stayed open, and the closing sell was refused (403 "insufficient qty available") because the open limit sell held the share; the test's clean-up canceled both |
| TC-E72 unsupported order type, TC-E73 unsupported time in force | Passed: a market-if-touched order and an IOC limit are `DENIED` before Alpaca |
| Venue rejection (no upstream row) | Passed: a market buy of 1,000,000 shares, refused by Alpaca (403 "insufficient buying power"), ends `REJECTED`; nothing opens |
| TC-E83 unsubscribe on stop | Passed: the tester's quote and trade subscriptions are removed |
| TC-E85 reconcile filled orders | Passed twice (`test_paper_fill_reports.py`, after fill reports were added, 14:53 and 15:43): a market buy of 2 filled outside the node is reconciled `FILLED`, one fill per trade update, each with that update's `execution_id` and price, once Alpaca lists every fill. A first run, started before Alpaca listed the second fill, failed (see "Fill activities can lag the stream"); another hung after the node failed to disconnect its execution client, cause UNVERIFIED. The engine's log also says "Generated inferred fill" for orders it was given fill reports for; which path that line names is UNVERIFIED |
| TC-E84 reconcile open orders | Passed (`test_paper_execution.py`, 2026-11-10, after hours): a $1.00 buy placed with alpaca-py, as a prior session leaves it, is loaded at start-up with its venue order ID, `ACCEPTED`, price, quantity, side and type, as an `EXTERNAL` order |
| TC-E44 cancel already-canceled | Venue side only, through alpaca-py (`test_paper_execution.py`, 2026-11-10, after hours, twice): the second cancel is answered 204 and the order still reads `canceled`, the idempotent result the card allows. The client's 422 path for it runs offline only |
| TC-E02, E86, E87 | Not run: a market sell (a short from flat), and reconciliation of a prior session's positions |
| TC-E20, TC-E22 stop and stop-limit buy | Passed in `test_paper_execution.py` (ADR 0036): accepted at the prices sent, as Alpaca holds them, reported back the same, canceled on stop |
| TC-E30 modify limit buy | Ends `OrderModifyRejected` before Alpaca, as designed: the tester's one-tick amend is sub-penny at the 0.0001 increment (`test_paper_execution.py`, ADR 0042); a modify by a cent through a node passes there |
| TC-E21, E23, E31, E33 | Not run: sells, each a short from flat (E33 is also a cancel-replace) |
| TC-E32, E35 | Not run: cancel-replace, which needs no hook beyond cancel and submit (ADR 0042) |
| TC-E34 modify stop trigger | Offline only (`test_stop_trigger_is_modified`, ADR 0042) |
| TC-E03 to E05, E13 to E18, E24 to E27, E36, E43, E50 to E53, E60 to E63, E70, E71 | Not in this slice, or do not apply: IOC, FOK, quote quantity, GTD, if-touched orders, an adapter without modify, batch cancel, brackets, order flags |
| TC-E74 to E78 | Ambiguous submit and cancel failures run offline only (`test_execution_client.py`); a modify with no definitive answer is logged and left to the stream or reconciliation, not tested; batch does not apply |
| TC-E88, E89, TC-E90 to E101 | Do not apply: Alpaca reports no commission, and options are not supported |

**Reconciliation finding, fixed.** Without a lookback, start-up reconciliation reads the shared paper
account's last 500 orders and, with no fill reports, inferred one fill for each filled one. On 2026-11-10
those inferred fills netted to a short of 1 AAPL, and in another run to a long of 10 SPY, while Alpaca held
no position and reported none; the engine kept each as an `EXTERNAL` position (ADR 0037). Alpaca's
500-order cap, and orders this slice skips (such as `replaced`), can leave a fill without its counterpart.
Fill reports alone do not remove it, since the missing counterpart's fills belong to an order the node is
not given; the explicit flat report for each loaded instrument without a position does
(`test_execution_client.py`, with and without fill activities). On paper, 2026-11-10 at 17:14 New York
time (`test_paper_flat_reconciliation.py`, which places nothing): with the account flat, start-up with no
lookback read 425 orders, 1,000 fills (the page limit) and the explicit flat reports for AAPL and SPY,
inferred 71 fills, and left AAPL and SPY each at a net position of 0, with no position open (counts from
the run's log). The same at 17:27, after review changes.

## Rules

Paper credentials and paper endpoints only. See `.claude/rules/trading-safety.md`. The execution client
holds this rule in code: see `AlpacaExecutionClientConfig` above.
