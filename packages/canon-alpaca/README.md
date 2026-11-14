# canon-alpaca

An out-of-tree NautilusTrader adapter for Alpaca, built on `alpaca-py`.

**Status: data slice and first execution slice, paper only.** It loads US equities as instruments, serves
historical bars, quotes and trades on request and streams quotes and trades, all against Alpaca's paper data. Its execution
client sends market, limit, stop and stop-limit orders, and opt-in venue trailing-stop SELLs, for US
equities to Alpaca's **paper** Trading API, cancels them one at a time or all of an instrument's at once,
turns Alpaca's trade updates into order events and fills, and
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
| `data.py` | `AlpacaDataClient` (`MarketDataClient`): `_request_bars`, `_request_quotes`, `_request_trades`, `_subscribe_quotes`, `_subscribe_trades` and their unsubscribes; `AlpacaDataClientConfig`, the stream's silence bounds | Built |
| `factories.py` | `AlpacaDataClientFactory`, `AlpacaExecutionClientFactory` | Built |
| `execution.py` | `AlpacaExecutionClient` (`ExecutionClient`): account state on connect, `_submit_order`, `_modify_order`, `_cancel_order` and `_cancel_all_orders` for market, limit, stop, stop-limit and opt-in trailing-stop-market SELLs, trade updates as order events and fills, order and position status reports; `AlpacaExecutionClientConfig`, which refuses anything but paper trading | Built (venue trailing-stop paper proof pending) |

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

A plain `DataClientConfig` gives the stream its default silence bounds. `AlpacaDataClientConfig`, from
`canon_alpaca.data`, takes the same fields and sets them: `stream_data_timeout_secs` (900 s), after which
alpaca-py reconnects a connection that has sent no quote or trade, and `stream_mute_limit_secs` (3,600 s),
after which the client stops a stream still silent and says so; `None` turns either off, and any other
keyword is refused (Known limits, "Mute stream").

A subscription or bars request names its Alpaca feed in its params, and gets IEX when it names none
([ADR 0055](../../docs/adr/0055-alpaca-data-feed-named-in-params.md)):

```python
self.subscribe_quotes(instrument_id, params={"feed": "boats"})  # in a strategy
self.request_bars(bar_type, start=start, end=end, params={"feed": "boats"})
```

A quotes request is served on IEX only, and with a catalog registered on the node it can be written back
into it, as ADR 0040 does for bars
([ADR 0066](../../docs/adr/0066-alpaca-quotes-into-the-catalog.md)):

```python
self.request_quotes(instrument_id, start=start, end=end, params={"update_catalog": True})
```

Historical trades use the same default IEX feed as bars and subscriptions. A request may name `iex`,
`sip`, `otc` or `boats`, the feeds Alpaca documents for its
[single-symbol historical trades endpoint](https://docs.alpaca.markets/us/reference/stocktradesingle-1):

```python
self.request_trades(instrument_id, start=start, end=end, params={"feed": "iex"})
```

Only `feed` is supported as a venue option on trade requests. `asof` and `currency` in params are
refused explicitly: Alpaca's current-day symbol mapping and default USD prices apply. The engine's
own params, such as `update_catalog`, are preserved; they are not sent to Alpaca.

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
  the same for execution. No `Live` prefix. `AlpacaDataClientConfig` has two adapter fields, the stream's
  silence bounds (Known limits, "Mute stream"); a plain `DataClientConfig` gets their defaults.
  `AlpacaExecutionClientConfig` has `report_external_orders`, `report_external_trailing_stops`,
  `submit_trailing_stops`, `sell_to_close_on_long_exits` and `reduce_only_sells`, all off by default, with
  `sell_to_close_order_types` scoping the last two, and refuses what is not paper trading. With
  `report_external_orders` true, trade updates report outside orders and their real trade-ID
  fills through `_handle_report(report, fills)` (the same guide's typed output contract).
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

Reduce-only follows upstream's [send-or-reject contract](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/adapters.md#reduce-only-execution-contract),
which permits an equivalent enforcing venue instruction. Alpaca's [order schema](https://docs.alpaca.markets/us/reference/postorder.md)
has a `position_intent` field. By default every reduce-only order is denied before transport with
`reduce-only is not supported`. With `AlpacaExecutionClientConfig(reduce_only_sells=True)`, a reduce-only
SELL carries `position_intent=sell_to_close` and a reduce-only BUY is denied before transport. The field is
present at the pin in [OrderRequest, requests.py:388](https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/trading/requests.py#L388),
inherited by all supported request classes, with [PositionIntent.SELL_TO_CLOSE](https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/trading/enums.py#L598).
No account configuration is read or cached, at connect, on a query or on any order.

`AlpacaExecutionClientConfig(sell_to_close_on_long_exits=True)` also adds the field to an ordinary SELL
when the cached net position for its account and instrument is long (the sum of the matching open
positions' `signed_qty` is positive). It is off by default and independent of `reduce_only_sells`.

Both settings act only on the order types in `sell_to_close_order_types`, by default a limit, a stop and
a stop-limit: the types for which a paper run shows Alpaca refusing the field when the sell would open a
short (ADR 0111). A market order is outside it until that is shown: with `reduce_only_sells` a reduce-only
market SELL is denied before transport, naming the order type, and with `sell_to_close_on_long_exits` a
market SELL of a long is a plain sell. A type the adapter does not submit is refused when the
configuration is built.
A BUY or a SELL from cached flat/short is
unchanged by this setting; the venue resolves any position change after that cached view. The adapter
never sets the order's Nautilus `reduce_only` flag or changes a strategy's exit settings. No adapter
setting is enabled in a node by this change.

`AlpacaExecutionClientConfig(submit_trailing_stops=True)` opts into Alpaca's own trailing stop for a
whole-share `TRAILING_STOP_MARKET` SELL, `DAY` or `GTC`, `DEFAULT` trigger, with no activation price or
initial trigger price. A PRICE offset sends `trail_price`; BASIS_POINTS sends `trail_percent` after
division by 100 (125 basis points sends 1.25 percent). Only one is sent. The default denies submission
by name, independently of `report_external_trailing_stops`. With the setting off two things still
differ from before: `sell_to_close_order_types` accepts the trailing stop where it raised, and a modify
carrying `trailing_offset` is refused where the parameter was ignored. BUY, other trigger and offset types, and
extended-hours eligibility are refused before transport. The regular-session risk check needs a
cached trade for a DEFAULT trailing stop at the Nautilus pin (`crates/risk/src/engine/mod.rs:2367-2397`).

A trailing stop can be named explicitly in `sell_to_close_order_types`; the default scope stays limit,
stop and stop-limit. A reduce-only trailing SELL needs both `reduce_only_sells=True` and that explicit
scope, and sends `position_intent=sell_to_close` through the existing path. The request inherits
`OrderRequest.position_intent` ([alpaca-py requests.py:624-644](https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/trading/requests.py#L624)).
Whether Alpaca enforces it on the market order created on election is **UNVERIFIED** until paper proof.

Submission and a change of the trail each need alpaca-py's `TradingClient.get_clock`, the same
regular session source the node uses, to say the regular session is open. One answer is reused until
the boundary it names (`next_close` while open, `next_open` while closed): one request a session, not
one a stop. The answer's age is the longer of the monotonic clock's and the wall clock's, because the
first stops while a machine sleeps: an open answer kept across a sleep is not trusted past its close. A closed clock, an elapsed `next_close`, an unreadable clock, raw data or a client with no
clock refuses the command by name before transport, and a failed read is not kept; no extended-hours
flag is set. The check uses the broker's time and close, including early closes. A close during the
subsequent network request can still queue an order for the next session; that boundary remains
**UNVERIFIED** on paper. A quantity-only replacement and a cancel work outside the regular session.

At the adapter hook, a `ModifyOrder` with `params={"trailing_offset": "250"}` changes a BASIS_POINTS
trail to 2.5 percent through `ReplaceOrderRequest(trail=2.5)`. This parameter follows the pinned
[Interactive Brokers convention](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/integrations/interactive_brokers.md#commands-and-order-attributes).
The offset kind stays fixed. Only the trail change needs `submit_trailing_stops`; the quantity
replacement is the existing path, for a reported outside trailing stop too, including its refusal to
increase a reduce-only SELL; limit/stop prices, changing offset kind and unknown params are rejected.
Updates and cancellation follow the existing replacement ID chain. A native `Strategy.modify_order`
at this pin requires a quantity, price or trigger change too, so a params-only strategy call fails
before reaching the adapter. Combining a genuine quantity change with the trail is tested; no dummy
price or quantity change is inserted.

Accepted, updated, filled, canceled, expired and rejected orders use the existing event/report paths.
The pin rejects `OrderTriggered` for market-style stops, so election appears as a fill. Alpaca does
not document which ID the order a trigger creates carries. Two forms are covered offline, both by the
existing paths: the order's own venue ID, and a new one naming the old in `replaces`, with partial and
duplicate fills. A new venue ID with no link is not adopted: its fills are counted by the existing
rule for an unknown leg and the node's order keeps its own ID. The venue's actual behaviour is
**UNVERIFIED**; that form is built only if a paper read shows it (ADR 0129).
The report carries Alpaca's `stop_price` as the advisory trigger and its current trail offset/type.
The pinned `OrderStatusReport` has no `hwm` field, and `OrderUpdated` cannot change a cached order's
trailing offset: the venue report is the current offset after replacement. These are verified pin
limits, rather than fields this adapter fabricates.

At startup, cached own trailing orders map without opting into outside orders. With no cache nothing
tells this node's open trailing stop from another's (a generated client ID carries the trader's tag,
the same on every node here), so an uncached one maps only with `report_external_trailing_stops`, and
otherwise fails startup by name. A node that submits venue stops and may start without its cache turns
both settings on. The native engine assigns an uncached order to its strategy through the node's
explicit instrument claim; without one it uses `EXTERNAL`.

## Known limits of this slice

Each is stated where it is implemented, with its source.

* **No algorithmic or direct-market order types.** Alpaca offers DMA, VWAP and TWAP orders only on its
  Elite Smart Router route; the adapter does not implement them and none is planned (ADR 0036).

* **Whole shares only.** NautilusTrader's `Equity` cannot hold a fractional quantity (ADR 0030). Alpaca's
  `fractionable` flag is kept in `Equity.info`; a fractional size in market data is refused, not rounded.
* **One price increment, 0.0001.** Alpaca's assets endpoint gives no price increment for US equities, and
  Alpaca's order rule is price-dependent: at most 2 decimals at or above $1.00, 4 below. `Equity` takes one
  increment, so the finer one is used. The instrument does not stop a sub-penny order at or above $1.00;
  the execution client denies one (below).
* **Prices finer than 0.0001 (2026-11-11, ADR 0055).** The consolidated tape (`sip`) carries trade and
  bar prices finer than four decimals (a SPY bar high of 747.086488), and sub-dollar names often do. A
  trade or bar price is rounded half to even, as upstream rounds a venue's value: `Instrument.make_price`
  (`try_make_price`, `MidpointNearestEven`, in `crates/model/src/instruments/mod.rs`) and the Deribit and
  Bybit parsers (`Price::from_decimal_dp`) at NautilusTrader `d53849b2`; 747.086488 becomes 747.0865 and
  0.12345 becomes 0.1234. Each rounding is named, never silent: on the stream the first per symbol, then
  the count at each power of ten, and the total at unsubscribe or disconnect, as warnings that begin
  `Alpaca stream on feed `, which a session window keeps in its record and does not fail on; for a bars
  request one warning with the count and the first. A quote finer than four decimals is dropped and
  counted the same way, not rounded, since a lit book quotes at the tick; a historical quotes request
  drops and counts it and keeps the rest. A single such print never ends a stream or fails a request.
  A positive price that rounds to zero is refused (no US equity trades below the 0.0001 tick).
* **Quotes need both sides.** A quote with a zero or negative bid or ask price is dropped, since a
  `QuoteTick` cannot hold a one-sided quote. A price that is not a finite number is not one-sided: that
  quote is dropped as malformed and logged as an error, as any other quote that cannot be converted. Outside market hours and on thin names such quotes can be
  most of them, so the first per symbol is logged as a warning with the reason, then the running count
  each time it reaches 10, 100, 1000 and so on, and the total on disconnect. The count restarts when the
  symbol is unsubscribed.
* **Quote sizes keep Alpaca's unit:** round lots on the stream, as its schema states, not shares. Alpaca gives no lot size
  to convert with. Its historical quotes reference says otherwise for history: "shares (round lots prior
  to November 3, 2025)". The two pages disagree, and nothing here converts a size, so quotes stored from
  history may change unit at that date.
* **Quote history** (`_request_quotes`, ADR 0066): one instrument per request, IEX only (a `feed` other
  than `iex` is refused before anything is sent: the catalog keys a quote by instrument, not by feed),
  answered with one response, so the catalog can write it back. The window is Alpaca's inclusive
  `[start, end]`, cut at now, with no `start` meaning the start of the end's day in New York. Each
  quote's `ts_init` is its own `ts_event`, as upstream's Databento client stamps a historical range. A
  one-sided quote, and one finer than four decimals, is dropped and the request's count logged; any
  other quote that cannot be converted fails the request. alpaca-py gathers every page of a request in memory before it returns and nothing
  here bounds a window, so a long one is asked for in pieces (ADR 0066: an hour at a time); a quote fetch
  holds the HTTP session's lock, so a bar request waits behind it. The quotes are converted on the node's
  loop 10,000 at a time, letting it run between. A write-back whose end is at or near now records as
  covered quotes Alpaca may not have published yet; `load_bars` keeps 15 minutes' margin for bars, and
  a caller of quote write-back keeps it itself. Write-back, as observed at the pin (offline and on paper, ADR 0066):
  the window is recorded as covered and one file written under `data/quotes/<instrument>/`; an empty
  response writes nothing and records no coverage, so the window is asked for again; a request with a
  `limit` is answered and not written back. The catalog encodes a file whole, writes it as
  `<name>#<n>` and renames it into place, so a process killed while writing leaves either no data file
  or the complete one, and at most that staging file, which the catalog does not read.
  A latest snapshot uses `request_quotes(id, limit=1, params={"feed": feed, "latest": True,
  "skip_catalog_data": True, "update_catalog": False})` on a named non-IEX feed matching the
  established live stream, returns at most one receipt-stamped quote without catalog reads or writes,
  and makes one SDK call under the client's historical lock (alpaca-py may retry 429/504 three times,
  3 s apart). Its sizes are in shares, not the stream's round lots (ADR 0066's amendment of 2026-11-13).
* **Historical bars:** `LAST`, `EXTERNAL`, MINUTE, HOUR, DAY or WEEK (DAY and WEEK at step 1 only);
  stamped at the open by Alpaca, so `ts_event` is the open and `ts_init = ts_event + interval`, per
  NautilusTrader's bar execution-simulation convention, where `interval` is the bar's fixed length:
  24 hours for a day, 168 for a week.
  `ts_init` is not the end of trading. Alpaca opens daily and weekly bars at midnight New York time
  (UNVERIFIED until the paper test runs), so a daily bar's `ts_init` is 24 hours later, hours after the
  16:00 close. Across a clock change that is not midnight: a bar spanning the autumn change ends at 23:00
  New York, one spanning the spring change at 01:00 the next day. US clocks change early on Sundays, so
  trading-day daily bars never span a change; weekly bars do.
* **Streamed bars and trading statuses (2026-11-11, ADR 0090):** `subscribe_bars` takes
  `<SYMBOL>.ALPACA-1-MINUTE-LAST-EXTERNAL`, the one bar Alpaca's stream carries, and refuses any other bar
  type with `Cannot subscribe:`. A streamed bar's `ts_event` is its open and `ts_init` its local
  receipt/normalization time, following NautilusTrader's pinned
  [timestamp semantics](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/data/index.md#timestamps).
  Its interval end is `ts_event + bar_type.spec.get_interval_ns()`; consumers must derive that boundary
  rather than compare the next open with the preceding receipt time. Its prices are rounded and counted
  as a trade's are. `params={"updated_bars": True}`
  also hands on Alpaca's `updatedBars`, sent after a late trade, as a second bar of the same type for that
  minute; off by default. `subscribe_instrument_status` hands on one `InstrumentStatus` per Alpaca status,
  its code mapped to a `MarketStatusAction` (`STATUS_ACTIONS`: halt, pause, resume, quotation resumption,
  price indication, short sale restriction; any other code `NONE`). `params={"wildcard": True}` on either
  subscribes `"*"` once per channel and hands on only the listed symbols; the last listed symbol's
  unsubscribe unsubscribes `"*"`. Quotes and trades refuse it. Bars carry no trade count or VWAP, since
  NautilusTrader's `Bar` has none; whether the paper data subscription serves `statuses` is `UNVERIFIED`.
* **Trades** carry no aggressor side (`NO_AGGRESSOR`). Alpaca's trade cancels, errors and corrections
  are logged as warnings naming the symbol and trade ID; a trade tick already emitted cannot be withdrawn
  or corrected.
* **Trade history** (`_request_trades`): one loaded instrument per request, using
  `GET /v2/stocks/{symbol}/trades` through alpaca-py's existing HTTP session. Every page is read,
  including a short or empty page with a `next_page_token`, until the token ends or the request's total
  `limit` of valid trades is reached; each page asks for at most 10,000 records and the remaining limit,
  which is enforced even if the server over-returns. An empty string, malformed token or repeated
  page token fails the request. A collection is capped at 1,000 pages, the pinned NautilusTrader Derive client's collection
  cap; if more history remains it fails, and the caller shortens the window. This bounds page count,
  with the existing HTTP timeouts still applying to each call. The response symbol must match the
  request, and malformed pages or rows fail with the symbol, page number and token named. Symbols
  are quoted as one URL path segment. Alpaca's documented `u` field is checked before the SDK drops it:
  `canceled` and `incorrect` prints are excluded, counted and logged, while ordinary trades and
  `corrected` replacements are kept; condition flags alone do not invalidate a print. With
  `trade_condition_filter` on, a print, streamed or requested, becomes a tick only if Alpaca's minute
  bars take its price, by the per-tape table in `canon_alpaca/trade_conditions.py`; the rest are counted
  by code and use none of a request's limit, and a code Alpaca's map shows that the table does not know
  refuses the connect (ADR 0118). It is for the `sip` feed only, and refuses a trade request with
  `update_catalog`. The setting is off by default. With `validate_trade_sequence` on
  (ADR 0134), a streamed print whose own time is before the newest one passed on for its symbol is set
  aside and is no tick, so the cache, a strategy and the order emulator do not act on an older print
  that arrived late; a print with the newest's own time passes. It is the first comparison of the bar
  check NautilusTrader's `validate_data_sequence` turns on, which the data engine has for bars only
  (`crates/data/src/engine/dispatch.rs:117-128`, `:444-446`, `:471-496` at the pin). A print stamped
  more than five seconds ahead of its receipt has no usable time: it is passed on as with the setting
  off and never becomes the newest. Each is counted by name: on log lines that begin
  `Alpaca trade sequence on feed `, and for the process in `canon_alpaca.data.late_prints_set_aside`
  (per symbol the count, the most any was behind in milliseconds, their shares, and the prints passed
  on unordered), which a Mover window's record carries as `late_prints_set_aside_by_the_adapter`. A
  bar built from ticks does not hold a print set aside; Alpaca's own bars are not affected. A request's
  trades are not checked. Off by default; the paper and sandbox node builders accept
  `validate_trade_sequence=True`, and a session window passes it only when run with
  `--validate-trade-sequence`, which is refused together with `--trigger last_price`: under a print
  trigger Mover judges a late print itself (one counts toward a held stop's low) and must see every
  print, and this check sets a late one aside before any strategy sees it. Its bound for a print
  stamped ahead is against receipt, where Mover's is against its name's quotes; that difference is a
  limit until this bound moves to the same reference (ADR 0134).
  Corrections and cancels are not applied to ticks already sent. Results are
  requested in ascending timestamp order and delivered in one `TradesResponse`, preserving the request
  ID, bounds and params. The inclusive window is cut at now, and a missing start uses midnight New York
  of the end's day, as quotes do. Bounds and raw trade timestamps retain nanoseconds, including the
  `+1 ns` legs the catalog creates after existing coverage; only in-window valid trades consume a
  request's limit. Each tick's `ts_init` equals its own `ts_event`; trade IDs and whole-share
  sizes are preserved. Prices are rounded half to even and any rounding counted and named, as on the
  trade stream. A failed page or unconvertible trade fails the whole request; no partial response is
  emitted. The HTTP lock covers every page and history is gathered in memory, so callers keep windows
  small; tick conversion yields to the node's loop every 10,000 trades. Catalog keys do not distinguish
  feeds, so callers must keep one feed per instrument for both reading and writing a registered
  catalog. Covered `request_trades` ranges are read from that catalog regardless of `params["feed"]`,
  even without `update_catalog`; an uncovered range may therefore merge stored IEX prints with
  newly requested SIP prints. `params={"feed": "sip", "skip_catalog_data": True}` bypasses catalog
  reads and requests the full window from SIP, but a write-back still uses the same instrument key.
  Offline HTTP and node
  tests verify this path; its response from the paper data API remains **UNVERIFIED**.
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
  documented as entitled to IEX alone, so none of this is an entitlement (ADR 0055). In pre-market
  `iex` and `boats` delivered nothing before 05:00 on 2026-11-11; the sip feed authenticated on paper on
  2026-11-10, and authenticated and delivered on 2026-11-11, from 08:23 in that pre-market (ADR 0055).
* **Timestamps** lose sub-microsecond precision where alpaca-py parses them into `datetime`.
  Trade history preserves the raw timestamp's nanoseconds separately, along with exact request bounds.
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
* **A subscribe lost while the stream connects:** a narrow start-up race in alpaca-py 0.45.0,
  reproduced offline by holding the first send, not observed on a live stream. alpaca-py sends its
  handler maps when it connects and only then counts as running, and sends a subscribe only while
  running; the three statements run with no lock around them (`_drive_run`, `_subscribe`, the same on
  upstream's master). So a subscribe made from another thread while that first message is being sent is
  recorded and not sent. The usual pattern, subscribe and then run, cannot meet it. The contract test
  `test_stream_stand_in_misses_a_subscribe_made_while_it_connects` forces the window on alpaca-py's real
  stream by holding the send, and fails if upstream changes the path. The subscribe that starts a stream
  is always sent, since alpaca-py waits for it before connecting. If another was made before anything
  arrived, the client
  sends its subscriptions again, once, on the first quote or trade of that stream run, which a running
  stream is the only one to deliver: one subscribe of a
  symbol it holds, with its own handler, after which alpaca-py sends every map whole and Alpaca answers
  with the full list it holds. That covers a start whose subscriptions deliver something; it does not
  cover a start where everything sent stays quiet (the lost one is sent with the next subscribe, or not
  at all), nor an unsubscribe made in the same window, which alpaca-py does not send either and a
  subscribe does not undo. A reconnect alpaca-py makes by itself opens the same window, and alpaca-py
  logs a warning before it (after `data_timeout`, a websocket error or a failed connect). That warning
  reaches the node's loop from alpaca-py's thread, so the loop can run a subscribe after the connection
  dropped and before the warning arrives; the client therefore marks every held subscription when the
  warning arrives, and sends them again on the next message, one subscribe per reconnect (corrected
  2026-11-11 after a second reader's finding). A resend planned before such a warning and finished
  after it leaves the mark set, since it may have reached alpaca-py while it reconnected. Marking
  every held subscription, rather than counting subscribes since the last message, also covers a
  subscribe the loop ran while a message from the old connection still waited behind it.
* **Mute stream:** a connection that is up but sends nothing is closed and made again by alpaca-py's
  own `data_timeout` (`StockDataStream(data_timeout=...)`, seconds, `None` by default, which disables it).
  At the pin (`alpaca/data/live/websocket.py`, alpaca-py `c42add7c0a70`): only market data resets the
  clock, not subscription acknowledgements or errors (lines 52-76, 287-299); on expiry `_consume` logs
  "no data received on data stream for N s, reconnecting", closes the socket and returns (272-281); and
  `_drive_run` waits a backoff that starts at 0.5 to 1 s and doubles to 30 s while no data comes
  (525-535, 120-121, `alpaca/common/utils.py` 47-76), connects and authenticates again and sends every
  handler map whole (519-524, 412-424), so every subscription the client holds is sent again. The client
  sets it from `AlpacaDataClientConfig.stream_data_timeout_secs`, 900 s by default: above the longest
  silence seen on a feed that then delivered (513 s: `iex` subscribed to QQQ alone in pre-market,
  2026-11-11, one trade and no quote in 600 s), while the parts a window runs in are far busier (SPY on
  `iex` in the regular session, hundreds of quotes a minute; on `boats` overnight, 1,552 to 3,700 quotes
  a window, ADR 0044). On a feed that sends nothing for hours (`iex` before about 08:00, a part ADR 0044
  skips), it reconnects every 15 minutes with nothing to lose. A reconnect is relayed as a warning naming
  the feed, so a quiet feed fails nothing. alpaca-py reconnects without end, so the client also counts the
  silence itself: once no quote, trade, bar or status has arrived for `stream_mute_limit_secs` (3,600 s by default,
  longer than the data timeout, which the config enforces) while it holds a subscription, it stops the
  stream, logs an error that begins "Alpaca stream on feed <feed> went mute", which a session window
  reads as its stream's error, and refuses later subscriptions, naming why, until it reconnects: the
  pattern of an ended stream (No restart) and of upstream's Databento feed handler, which gives up
  reconnecting after `reconnect_timeout_mins` with an error. Silence while nothing is subscribed is not
  counted. A node spanning a part no feed serves (pre-market and after-hours) sets the limit longer than
  that part, or `None`; the limit outlasts every window this repository runs (an hour at most), where
  the window's own no-data check speaks first, so it matters to longer nodes. With nothing subscribed, a
  running stream is still made again after each `data_timeout`, with a warning each time. A stream given
  to the constructor keeps its own `data_timeout`. UNVERIFIED: how
  long a healthy feed can stay silent beyond the one 10-minute sample; a mute connection on a live stream
  (forced offline only, on alpaca-py's real stream over an in-memory socket).
* **A stream that never connects:** alpaca-py retries a refused connection without end (406 "connection
  limit exceeded", for one, as when an abandoned stream still holds the connection). The subscription
  has returned; the node logs alpaca-py's retry warnings, naming the feed and thread, and no data
  arrives. alpaca-py keeps its connection state private; the mute limit, which counts from the stream's
  start, ends it and says so.
* **Nothing reaches the engine but the log:** NautilusTrader v2.0.0rc8 gives a Python data client no
  way to say its data has stopped (`is_connected` is the runtime's, read-only); upstream's Databento
  client, when it gives up reconnecting, logs the error and goes no further. A stream that ended, never
  connected or went mute shows in the node's log and in the refusal of later subscriptions.
* **Subscriptions across a reconnect:** a reconnect alpaca-py makes sends every subscription again (Mute
  stream). The client forgets its subscriptions at `_disconnect`, while NautilusTrader's engine keeps its
  own record of them until a reset (`DataClientAdapter::clear_subscription_state`, `crates/data/src/
  client.rs`) and skips a subscribe it already holds, so connecting the same client again under one engine
  would leave them unsent. At the pin that cannot happen: "A client instance and its bound provider belong
  to one node run" (the adapter guide), and a stopped `LiveNode` refuses to run again
  (`test_a_stopped_node_cannot_run_again_so_a_client_serves_one_run`, which fails if upstream allows it).
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
* **Data-only mode** ([ADR 0070](../../docs/adr/0070-sandbox-execution-on-alpacas-live-data-and-a-data-only-task.md)):
  with `CANON_ALPACA_DATA_ONLY=1`, which `mise run paper:data` sets, the same `TimeoutAdapter` refuses every
  method but GET with `MethodRefused` before anything is sent, so no REST client the adapter builds can
  submit, replace, cancel or close. It is a mode of the one adapter, not a second one, because
  `with_http_timeout` replaces any other adapter each time it runs; once refusing, it stays so, and an
  adapter mounted for any other prefix is replaced. The data client refuses to connect, and so the node to
  start, if its historical client or the provider's client has an adapter that would send another method
  (`require_get_only`). It guards the adapter's clients only, not other code holding the pair, and does
  not cover the streams, which are websockets.
* **Request count:** the same `TimeoutAdapter` counts every REST request this package's clients send, per
  key pair and per API (trading or market data, by alpaca-py's `BaseURL` hosts), with the total, the
  busiest UTC wall-clock minute, the most in any 60 seconds, the number of 429s and the latest 100 of
  them by name, time and path; `providers.request_counts()` returns
  them for a node or a window to read at its end. It counts attempts handed to requests' transport, so
  alpaca-py's 429 and 504 retries count and so does a request that never connected: an upper bound on
  what Alpaca received. A data-only refusal is not counted. It counts and does not throttle:
  NautilusTrader's rate limiter (`crates/network/src/ratelimiter`) is Rust-only at the pin, and alpaca-py
  has none. Streams are not counted.
* **Instrument loading:** only active assets load; an inactive asset, or an asset whose symbol is not the
  one requested, is refused with its ID named. Assets Alpaca marks not tradable still load, because their
  market data does not depend on it; `tradable` is kept in `Equity.info` for the execution slice. `load_ids`
  fetches one asset per request, in sequence, inside the connect, so a long list can exceed the node's
  connection timeout; every ID is attempted, network errors included, and the error names each that
  failed. `load_all` fails as a whole if alpaca-py cannot parse any one asset record (its models validate
  the full list).

## Known limits of the execution slice

Each is stated in `execution.py`'s header, with its source.

* **Instrument load and the command queue:** a node that loads more than 1,024 instruments at its start
  (`load_all`, the whole market) logs one error for each instrument past the first 1,024, `Client ALPACA
  instrument update failed: RuntimeError: Client ALPACA command queue is full`. It is an upstream limit,
  not a load that failed. NautilusTrader gives a Python client one "FIFO queue with capacity for 1,024
  waiting operations", says "A full queue rejects further admission", and admits each published
  instrument's notification, `_on_instrument`, to the execution client's queue
  (`docs/developer_guide/python_adapters.md:168-170`, `:408`; `crates/live/src/python/runtime.rs:39`,
  `:187-192`; `crates/live/src/python/client/mod.rs:1960-1972`, at the pinned `d53849b216e2`). The node's
  start connects the data clients, then puts every instrument they buffered into the cache in one
  synchronous loop, then connects the execution clients (`crates/live/src/node/mod.rs:35-48`,
  `:484-493`; `crates/live/src/runner.rs:375-411`); nothing can drain the queue inside that loop, so no
  pacing in an adapter lifts the limit, and upstream documents no setting for the capacity (the base
  client builds its runtime with the default, `nautilus_trader/live/clients.py:160`). v2.0.0rc9 and
  upstream's development branch on 2026-11-12 have the same code.
  What is refused is only the notification, a hook this client does not override. The data engine
  adds an instrument to the cache before it publishes it (`crates/data/src/engine/dispatch.rs:29-43`),
  so every instrument is in the cache, and the queue is empty again, when a strategy starts; and an
  order sent at the start is admitted and fills. The execution client hands the node only the
  instruments the cache does not hold, on upstream's order of start ("so that instruments are in the
  cache before execution clients read them"), so an instrument both clients load is refused once, not
  twice. Each of the two bursts, the data clients' and what the execution client still hands on, is
  refused past 1,024 by itself: where the data client's load covers the execution client's, as in a
  paper node with a ranking (`canon_trading.paper_node`, `data_load_all`), the refusals are the
  instruments loaded less 1,024. `test_instrument_load.py` holds each of these on a real node, in that
  shape and with both clients loading 3,079, and counts the refusals exactly by the text
  `execution.INSTRUMENT_REFUSAL`, so a change upstream, in the limit or in its wording, fails a test by
  name. A window's record counts the lines by the same text in `instrument_refusals`, apart from its
  stream lines and errors (`canon_trading.session_window`). Those tests on a real node, offline, are
  this change's simulation run (ADR 0074). Limits: the record's number is the whole window's, so a
  notification refused later, while the queue held real commands, is in it and is told apart only by
  comparing it with the instruments loaded less 1,024; a provider loads once
  (`InstrumentProvider.initialize` returns at once when it has loaded,
  `python/nautilus_trader/live/providers.py:62-68`), so a reconnect hands on the definitions of the
  start, and the execution client none of those the cache holds; and a whole-market start on paper has
  not been run on this change, so that the node's log file carries the line as the tests read it off
  the console is UNVERIFIED until a window's record shows the count.
* **Order types:** market, limit, stop and stop-limit, plus the opt-in trailing-stop-market SELL above;
  `DAY` or `GTC`, whole shares, simple orders.
  Alpaca marks IOC, FOK, OPG and CLS for whole-share market and limit orders "Yes*", "contact the sales
  team", and for stop and stop-limit orders "No"
  (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Order Types vs Supported Time in Force"), so
  they are denied. So are post-only, quote-quantity and display quantity orders (a stop-market's
  display quantity included), a
  stop trigger type other than `DEFAULT` or `LAST_PRICE` (Alpaca elects a stop "on the consolidated
  print"), a stop price at or above $1.00 with more than 2 decimals or below $1.00 with more than
  4 (the same page, "Sub-penny increments"), and an asset Alpaca marks not tradable. A limit price finer
  than that increment is sent on it instead, a buy rounded up and a sell down, on a submit and a modify,
  and `OrderUpdated` just before `OrderAccepted` carries the price sent
  ([ADR 0030](../../docs/adr/0030-alpaca-adapter.md), 2026-11-11). Alpaca sends no update
  when a stop is elected, so the client generates no `OrderTriggered`: an elected stop-limit stays
  `ACCEPTED` until it fills or ends. An open stop is reported with its stop price as trigger price and,
  since Alpaca's order has none, the trigger type of the node's order, or `DEFAULT` for one the node does
  not hold ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)). Denied orders never
  reach Alpaca. Not implemented: order lists.
  With `report_external_trailing_stops` off, the default, an open `trailing_stop` placed outside the
  node fails the start by name, `order type trailing_stop`, as every unmapped order type does. With it
  on, an outside `trailing_stop` is mapped for reports and reconciliation only: its `trail_price` is a
  `PRICE` offset, or its `trail_percent` multiplied by 100 is a `BASIS_POINTS` offset (100 means 1%);
  its `stop_price` is the advisory current trigger, absent when Alpaca gives none or the value is not
  representable at the instrument's four decimals (one warning), with `DEFAULT` trigger type
  unless the cached order supplies one. This uses [Alpaca's trailing-stop fields](https://docs.alpaca.markets/us/docs/orders-at-alpaca.md#trailing-stop-orders)
  and the pinned [trailing model](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/orders/trailing_stop_market.md)
  and [offset units](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/orders/index.md#trailing-offset-type).
  No high-water mark or activation price is required. Exactly one positive finite offset must be
  present, otherwise an open order still fails by name. Trailing submission uses its separate opt-in
  above. There is no local trailing logic; updates of Alpaca's moving trigger between reports remain
  unavailable on its stream.
* **Reduce-only:** all reduce-only orders are denied with `reduce-only is not supported` by default.
  With `reduce_only_sells=True`, a SELL of an order type in `sell_to_close_order_types` carries
  `position_intent=sell_to_close`; a reduce-only market SELL (outside the default scope) and every
  reduce-only BUY are denied. A modify that raises the quantity of a resting reduce-only SELL is
  rejected before transport: Alpaca's replace carries no position intent, and what it checks when such
  an order grows is not shown on paper; a smaller quantity or another price is sent. The scope is kept
  on the config as the order types in the order of their names, so the config serialises.
  Every other order check still applies. The independent optional long-exit setting above adds this
  intent to ordinary closing SELLs and changes no Nautilus flag. A 422 `position intent mismatch` with
  body code 42210000 reaches the strategy
  as `OrderRejected` with the code and text once Alpaca confirms it does not hold the order. Venue
  enforcement of position_intent is shown on paper only for the cases ADR 0111's Confirmation lists; every
  other session and order-type combination is **UNVERIFIED**. There is no account-configuration read or cache.
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
  Alpaca's stream shows the replace took effect (`replaced`, carrying the old order with `replaced_by`),
  with the replacement's ID, quantity and prices; a success answer alone does not, since Alpaca says it
  "does NOT guarantee" the replacement. Alpaca's refusal and `order_replace_rejected` are
  `OrderModifyRejected`; a modify without a venue order ID, to a fractional or zero quantity, to a price
  that is not positive or to a sub-penny stop price is denied before Alpaca, and so is one whose limit,
  on Alpaca's increment, is the order's own price with nothing else changed; a sub-penny limit price is
  sent on Alpaca's increment. What Alpaca's pages leave open was
  observed on paper and is asserted by `test_paper_replace.py`
  ([ADR 0042](../../docs/adr/0042-alpaca-adapter-modify.md)).
* **The replace chain** is one record per node order, after upstream's Binance spot adapter, the one
  pinned adapter that modifies by cancel-replace (`crates/adapters/binance/src/common/dispatch.rs`,
  `OrderBinding`): the Alpaca orders the node's order has been, the one it is on now and the ones it has
  left, the replaces awaiting an outcome, and the client's own count of its fills and their trade IDs.
  It is built from the node's cache when first needed (the order's venue order IDs, its fills, its trade
  IDs), as upstream's `restore_order` rebuilds a binding, so a reconnect or a new client keeps it; it is
  dropped when the order ends. A replacement is sent a new client order ID, the order's own with a
  clock stamp after its last field (`replacement_client_order_id`; Alpaca's reference gives the replace
  a `client_order_id` of at most 128 characters, and one kept is refused), so an account read by client
  order ID still shows it as the node's; Alpaca makes the ID where the order's own does not end in a
  number. Neither ID is read for what it replaces: a replacement is known as the
  node's through the client's index or the node's cache, and only on a miss by walking its `replaces`
  links, at most 16 reads, from the orders already read first. A walk that would need more reads, or meets
  an order Alpaca does not find, cannot be read: a working replacement whose chain cannot be read fails
  start-up, naming it, and a closed one is skipped with a warning. On the stream:
  * a fill on any order of the chain is applied once, known by its trade ID or by a status report that
    showed it (see Fills), and the order ends when its fills reach its quantity;
  * a replacement is adopted once, by `replaced`, by its own update once the record shows the order it
    replaces replaced by it, or by a fill, which proves it live; its `new` alone does not adopt it;
  * a late update of an order the record has left is ignored, but for its fills;
  * a replacement's `rejected` took over only if the order it replaces is replaced by it, as the record
    shows or one read of that order does. Then nothing works at Alpaca and the node's order ends:
    rejected, or, if partly filled, canceled with its fills, as upstream's `reject_replace` emits the old
    order's `OrderCanceled` (NautilusTrader drops `OrderRejected` for a partly filled order). A
    replacement rejected without taking over is the modify's rejection; one a later replace left behind
    changes nothing; one rejected after the order filled leaves the fill as the outcome.

  Reports apply the same rules to Alpaca's reads: a replacement is reported under the client order ID its
  chain starts from, a rejected one only if it took over, then as the order's end (canceled once it has
  fills); `pending_replace` is open at the order's own terms. A report whose replacement Alpaca does not
  find is none, as for an order Alpaca does not find. A report asked for by client order ID starts from
  the Alpaca order the node's order is on now. What a replacement's `qty` and `filled_qty`, and its
  activities' `cum_qty` and `leaves_qty`, count once the order before it had fills is not on Alpaca's
  pages. On paper, overnight, 2 of 2 runs (ADR 0042), the replacement carried the order on: its `qty` the
  order's, its `filled_qty` and `cum_qty` counting the fills before it, each fill event's `qty` its own.
  That reading, `chain`, is in force; whether the regular session answers the same is UNVERIFIED. The
  other, `own` (an order for what is left), stays written in `FilledReading` and tested, and the
  client's accounting turns on the reading in one place (`chain_filled`, `chain_quantity`).
* **Cancel-all** cancels the node's open and in-flight orders for the command's instrument, and side if
  it names one, each by ID, one at a time, as a single cancel does; one already pending cancel is canceled
  again, after the others, since its first cancel may have had no answer;
  a refused one gets `OrderCancelRejected`, and after a refusal or a failure the rest go on. Alpaca's "Delete All Orders" cancels every open order of the
  account, so it is not called. A strategy sends the command only with `strategy_only=False`, and it then
  covers every strategy's orders; with the default, the strategy cancels its own orders one by one
  ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)).
* **Outcomes:** a 403 or 422 from "Create an Order" (https://docs.alpaca.markets/us/reference/postorder.md)
  rejects the order once Alpaca confirms it does not hold it (see Retries); any other failure, a 504 or
  429 included, leaves it `SUBMITTED`, an unknown outcome under NautilusTrader's
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
  settled by its fill. If the order was never acknowledged, the filled read is passed through the
  existing report/catch-up path to book its actual trade-ID fills: a retained submit has no further
  automatic query budget. An acknowledgement that comes while a cancel or modify sent before it is pending
  is kept, gives a later cancel or modify Alpaca's order ID, and `OrderAccepted` follows a refusal of
  that cancel or modify unless a fill came first. An update for an order the engine has ended from a
  query's report is dropped, fills included, since that report reaches the engine only once the node's
  order holds every fill it shows (ADR 0075).
* **Refusal reasons:** when Alpaca's error body includes a code, the bounded venue text keeps it before
  the message, for example `Alpaca refused the order (403): 40310000: insufficient qty available ...`.
  Submit, modify and cancel use the same formatter, reading the pinned alpaca-py's `APIError.code`
  and `APIError.message`; a body with no code keeps its previous message or raw-text fallback. The HTTP
  status and the rest of the reason's wording are unchanged.
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

  **OBSERVED 2026-11-13, 18:26-18:27 New York, post-market:** one share of a liquid index ETF
  held on a long-only paper account, REST only. Each accepted order was canceled and read canceled
  before the next. The owning lane keeps the run's log outside the repository.
  The new sells were (a) a DAY limit sell marked for extended hours, 5% over the ask;
  (b) the same with `position_intent=sell_to_close`; (c) a plain DAY market sell;
  (d) a GTC trailing stop sell, 5%, `sell_to_close`. Every refusal was HTTP 403, code
  **40310000**, message **"potential wash trade detected. use complex orders"**.

  | Resting order | (a) | (b) | (c) | (d) |
  |---|---|---|---|---|
  | 0. None (baseline) | accepted | accepted | accepted (queued) | accepted |
  | 1. Market buy, GTC, not marked for extended hours, resting as `accepted` (queued for the next open) | REFUSED | REFUSED | REFUSED | accepted |
  | 2. DAY limit buy marked for extended hours, 5% under the bid (live) | accepted | accepted | accepted (queued) | accepted |
  | 3. GTC limit buy not marked for extended hours (queued), 1.00 over the sell's limit | REFUSED | REFUSED | REFUSED | accepted |

  Reversed: with sell (a) resting, a new market buy (GTC, not extended) was ACCEPTED (queued),
  and a new DAY limit buy marked for extended hours, 5% under the bid, was ACCEPTED.

  The one differing cell from the page's wording above is **row 2, cell (c)**: the market sell
  was accepted beside a resting live limit buy priced under the market. This is recorded as
  observed, without inferring a general venue rule.
  **Not shown:** the overnight session (a second run is owed, to be appended by a later edit);
  a stop or stop-limit sell; whether a trailing stop elects and sells beside a resting buy;
  any size but one share; an account that allows shorting; the regular session.
* **Fills** come from the trade updates stream, one per `fill` or `partial_fill` event, with the event's
  own `qty` and `price`, and from Alpaca's fill activities when an Alpaca read shows more filled than the
  node holds; every fill is known by Alpaca's trade ID alone and applied once per trade ID, whichever
  source brings it first (an update delivered twice is applied once, a reconnect included;
  [ADR 0075](../../docs/adr/0075-alpaca-fill-identity-by-trade-id.md)). The engine never infers a fill for
  an order the node holds: it would infer one from a status report showing more filled than the order,
  under an ID of its own that no later source of that fill can be matched to by trade ID (NautilusTrader
  "treats the reports as distinct" when one fill arrives with different IDs,
  `docs/concepts/execution/index.md`, "Overfills", at the pin). So a stream update, a `QueryOrder` answer,
  a refused submit's read or an open-order check showing more filled than the node holds first has the
  client read the fill activities of the order's replace chain and apply the missing fills, oldest first,
  each under its own trade ID, price and time; a status report reaches the engine only once the node's
  cached order shows every fill it shows, and is held back meanwhile (a report returned to the engine can
  reach it before fills generated just before it, observed), so the engine's next check sends it. While
  the activities do not list the fills, the report is held back with a warning; for a stream update the
  activities are read again, walking the replace chain at Alpaca, 3 times in all, 3 s apart (alpaca-py's
  own wait between attempts of a read; the count a choice, since Alpaca states no lag), and then the
  order is left with an error; an update showing more while they wait gives them their reads again. A fill on an Alpaca order of the chain the node's order never held is
  applied under the order it is on now, as NautilusTrader's `reconcile_fill_report` does, so it cannot
  move the order back to an order Alpaca replaced; an order Alpaca shows replaced is never adopted. Before
  a fill on an order the record is not on counts, the order is placed in the replace chain: behind, its
  fill counts on the order now working; ahead, it is adopted first, so its quantity applies before its
  fills, as upstream amends before recovered fills; unplaceable (a link unreadable), the fill waits for the
  later reads. A
  `canceled` or `expired` update or report showing more filled than the node holds brings the missing
  fills first and then ends the order, waiting with the later reads while the activities lag; after
  them it ends with what the node holds, with an error. Ending an order removes its later-read target.
  An outstanding read remains tracked until it returns, and a sleeping retry exits when it wakes,
  without another read or crediting a stream fill as caught up on that read; disconnect cancels any
  still outstanding. A read that itself brought the ending fill retains its catch-up log.

  An already finished order waits for actual cache closure at three sites: `_settled_report` for
  single and bulk return hooks (including mass/open-order reports), `_query_order` for `QueryOrder`,
  and `_send_report` for stream snapshots and companion fills. They share `_hold_report` and its
  `HELD_REPORT_TIMEOUT_SECS` (0.5 s, an adapter choice). That bounds mapping inside the send hold,
  cache closure, chain orders, activity pages, newer snapshot reads and the cache application of
  recovered fills needed by return hooks. A continuous report's delivery ends its hold immediately;
  no later cache wait can label that delivery failed. Only an actual cache wait logs an INFO
  held/released pair, naming the order and reason. Initial query/bulk reads and mapping precede
  settlement, outside the stream consumer. Earlier identification of an unknown replacement keeps
  its existing chain lookup. A canceled REST await releases the consumer at the bound; its shielded
  worker retains the REST lock until the actual call ends.

  Expiry raises a named error. `_report_hold_failures` keeps the entry cache state, a monotonic retry
  time, a log deadline and the error. `HELD_REPORT_RETRY_SECS` (5 s, an adapter choice) spaces retries
  after a timeout on an unchanged closed order: a later update, query or report tries again after
  that spacing even if no fills have reached the cache. Cache closure/progress can unblock it sooner.
  A burst inside the spacing is refused immediately; the timeout names the omission and spacing,
  and repeated refusals share that one line. If the cache never closes, no more holds start, and
  omissions are logged at most once per spacing. Successful settlement drops the failure; disconnect
  replaces the state so an old hold cannot restore it; completed chain reads also check their
  session before writing the index, and settlement checks before delivery.

  A stream update of a finished order whose hold fails, by a timeout, because Alpaca's activities
  do not list the fills it shows yet, or because a read for its mapping failed (an `APIError` or an
  `OSError`), is read again by the client itself: Alpaca sends an ended
  order no further update, and the engine's open-order check reads open orders only by default, so
  nothing else would. `_reread_report_later` reads the order afresh `HELD_REPORT_REREADS` (3) times
  at most, `HELD_REPORT_RETRY_SECS` apart, through the same hold and the same delivery; one task per
  order, canceled at disconnect. Past the last read it logs an error that leaves the order to the
  next start-up. A re-read and a later update can each queue the same fills before the cache shows
  them; the engine's bundle path drops a fill whose trade ID the order already holds
  (`reconciliation/orders.rs:785-791`, called per fill at `engine/mod.rs:1769`, at `d53849b216e2`).
  Limits: whether start-up reconciliation then brings such a fill is UNVERIFIED; an
  update that fails while a re-read waits adds no reads; a `QueryOrder` or report hook whose hold
  fails arms none, since the engine asks again on its own schedule; and each re-read's first
  request to Alpaca is outside the hold's bound, in its own task, off the stream consumer.

  Bulk reports omit each held order by name and return the reports available, including `[]` when
  every candidate is held or awaiting catch-up. Listing failures still raise. The engine follows a
  missing bulk entry with a targeted lookup (`crates/live/src/execution/manager.rs:2457-2472` at
  `d53849b216e2`); that lookup raises for a held order, marking coverage incomplete
  (`reconciliation.rs:530-556`). The manager then logs "Deferring" and keeps that order unresolved
  (`manager.rs:2590-2597`), without suppressing reconciliation of every other missing order as a
  failed bulk listing would (`manager.rs:2076-2079,2457-2466`). `QueryOrder` and a refused submit's
  report consume an already logged hold error; the latter also logs an API mapping error once,
  without raising it to the command worker.

  `_finished` alone does not prove an end reached the engine. Additional filled shares come by real
  trade ID from companion fills or the chain's activities. When those fills exceed a snapshot's
  total, the order is read again; the resulting report must account for every new fill delivered.
  If the snapshot is ahead of the activities, or remains behind them after that read, the request
  fails by name without sending an incomplete bundle or inferring a fill. Continuous send/query
  paths deliver the report and new fills once through `_handle_report(report, fills)`; a late
  update showing no new shares is not forwarded to the engine. Single/bulk
  return hooks send only the individual FillReports, wait for their cache application, then return
  the status once. The continuous bundle path applies fills and then an authoritative snapshot,
  which can void fills if its total is older (`crates/execution/src/engine/mod.rs:1679-1800` and
  `crates/execution/src/reconciliation/orders.rs:117-157,596-670` at `d53849b216e2`). The model preserves
  cancellation/expiry for a late partial fill below the order quantity
  (`crates/model/src/orders/mod.rs:1355-1376`). The bound's suitability under paper load is
  **UNVERIFIED**. A request that starts catch-up on an unfinished order keeps the earlier
  suppression behavior above, even when that request itself generates the ending fill; its missing-order
  limits below remain.

  `_finished` can be populated before startup mass status when the cache already contains orders:
  connect starts the stream consumer and resumes waiting ends before the node requests mass status
  (`_connect_venue`, `_resume_waiting_ends`; upstream `crates/live/src/node/mod.rs:502-535,945-982`).
  This is a source-based reachability inference, not a fresh empty-cache startup observation.
  Startup reconciliation uses `is_snapshot=true` (`manager.rs:980-988`), so a return hook refreshes
  a snapshot below the cache's filled total and refuses it if it remains older. It also checks the
  total again after waiting for recovered fills; it never returns a finished snapshot below the
  cache's then-current total.

  At start-up there is no exit to inference for
  an open order the node does not hold that shows fills: its chain's fills, at any age and its older
  ones outside the engine's window included, are reported by trade ID when they add up to what its
  status report counts, or the fill reports fail by name after 3 reads (each fill reports request
  reads the open orders for this, one request more). Known limits: an order still `SUBMITTED`, `PENDING_UPDATE` or
  `PENDING_CANCEL` whose fills the activities do not list through all the engine's in-flight queries is
  resolved locally by the engine under upstream's default (`INFLIGHT_TIMEOUT` for a submit, canceled for
  the others), and a fill Alpaca makes on it afterwards is booked to an `EXTERNAL` order (the session
  window's claim 10, ADR 0044); the paper nodes' engine retains a submit instead (`RETAIN_UNRESOLVED`),
  and a fill report for an Alpaca order the node does not know carries the client order ID Alpaca's
  read of it shows when that is an order in flight the node holds with no Alpaca order ID, of the same
  instrument and side (`_unanswered_named`, as upstream's OKX adapter sets the one its venue echoes), so
  the fill reaches the order. The window makes no whole-instrument claim: proven foreign orders and
  fills stay EXTERNAL. A retained order whose read fails, whose ancestor trace fails, or whose fill activities lag is
  asked again on each check until it is named or the node stops: an unread order's fill reports are
  withheld, never booked `EXTERNAL`, and the fill request reports its coverage incomplete so the engine
  does not infer a position in their place, while the identified fills are delivered; a refused cancel
  that reads the retained order FILLED keeps reading its activities past `FILL_READ_TRIES`. Each failed
  naming read is counted and logged by class and status/code rather than raw exception text. Without
  confirmed ownership the retained submit can still fail shutdown. With `open_check_open_only` off, an open order whose report is held back past
  `open_check_missing_retries` and a targeted query is resolved locally too (an accepted one rejected
  `NOT_FOUND_AT_VENUE`, a partly filled one canceled). A fill report the engine has not applied yet
  when the stream's update for the same fill arrives is counted once: the record adds a fill to the
  larger of its own count and the node's order's, never their sum, and the engine's order refuses the
  repeat ("enforces one quantity-bearing fill per `trade_id`"). The node's order ends when its fills reach its quantity, on the client's own count, so a
  fill on an order since replaced that arrives after the replacement's is still applied, and a fill that
  cannot be applied ends nothing (https://docs.alpaca.markets/us/docs/websocket-streaming.md). The trade ID
  is Alpaca's `execution_id`, which that page shows only in its example; a fill without one is logged, and
  the order's fill activities bring it. Prices are kept at the precision Alpaca sends (the example has seven decimals). Alpaca
  reports no commission and no liquidity side, so fills carry `None` and `NO_LIQUIDITY_SIDE`.
* **Fill reports** come from Alpaca's `FILL` activities, `GET /v2/account/activities/FILL`, called through
  alpaca-py's generic `get` since 0.45.0's `TradingClient` has no method for it
  ([ADR 0024](../../docs/adr/0024-alpaca-fill-history.md)). One report per activity: the trade ID is the
  UUID after `::` in its `id`, which on paper is the trade update's `execution_id` for the same fill
  (`test_paper_fill_ids.py`), so a fill seen on the stream and in a report is one fill; the price exactly
  as Alpaca writes it; the time its `transaction_time`; commission zero dollars (Alpaca's fill has none;
  upstream's TC-E85 expects a zero from a stream that omits fees) and `NO_LIQUIDITY_SIDE`; no client order
  ID (an activity has none). A fill listed under an order since replaced is reported on the order that
  replaced it, as the stream applies it. A chain's fills are judged together, every order of it read, two
  replaces or more: they are kept when the live order shows they are all of them under the reading in
  force (The replace chain, above). Under `chain`, the live order's `filled_qty` must equal the chain's
  listed fills and, if it lists fills of its own, its newest activity's `cum_qty` must equal its
  `filled_qty`; under `own`, each order's listed fills must equal its own `filled_qty`. Fill reports asked
  for one order cover its replace chain, as the bulk request reports them. A live order that shows it
  counts only its own fills, which paper did not show, keeps them and leaves the earlier ones to the
  engine; anything else that does not add up leaves the chain's fills to the engine. With no fill of its own listed, a `filled_qty`
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
  or else Alpaca's order, read from its orders endpoint, shows no more filled than they add up to. An
  order that cannot be read or built, or more filled than listed (listed fills short of `cum_qty`, for an
  order not in a replace chain): that order's fills are not reported, with a warning, and the engine
  infers them instead, for a closed order the node does not hold (an open one has its fills reported
  at any age, or fails start-up, above). This holds whatever the fills' age, since how long the
  lag lasts is UNVERIFIED.
  The cost falls on orders whose newest listed fill leaves shares to fill, lacks `cum_qty` or
  `leaves_qty`, or is short of its `cum_qty`, which in a replace chain can count the chain's fills (an
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
  position's `qty` is negative (UNVERIFIED), so a position is read by custody's one rule: its `qty` must
  be a finite number, read before the sign is taken off, else the report fails naming it, and with it the node's
  start-up, as for any position this slice cannot represent (below); it is a short
  by its `side` or by a `qty` below zero, never reported long; the size is the magnitude. A position
  labelled long with a `qty` below zero is reported a short and named in a warning with both fields, once per
  symbol for the client's life and only for a report it makes; a refusal names the symbol. Each loaded
  instrument Alpaca lists no position in is reported flat: Alpaca's positions endpoint lists only open
  positions, and NautilusTrader takes only "An explicit flat report" as evidence of flat. Without it,
  fills inferred from part of the order history left an `EXTERNAL` position Alpaca did not hold. With
  `load_all`, that is one flat report per US equity on each position report request. A flat report from
  a read taken just before a fill lands is wrong for that moment; whether a running node's periodic
  position check can act on that is UNVERIFIED.
* **Account:** a NautilusTrader cash account whose USD balance is Alpaca's `cash`, all free, read at
  connect and whenever a strategy calls `query_account`; Alpaca's stream pushes no account update, so the
  balance is as of the last read
  ([ADR 0036](../../docs/adr/0036-alpaca-adapter-order-management.md)); cash finer than a cent is reported to the cent, with a warning. An answer
  without `cash` or `currency` is refused naming the field: a connect fails, a query leaves the state as
  it was. Buying power, margin and
  shorting are not modelled; the account number, `status`, `equity`, `buying_power`,
  `non_marginable_buying_power` (as Alpaca sends it, or `None`) and `trading_blocked` are kept in the
  account state's `info`.
* **Stream:** alpaca-py does not expose the stream's `listening` acknowledgement, so updates sent before
  it is listening are missed. alpaca-py reconnects the stream by itself after a websocket or connection
  error, with backoff, logging only through Python `logging`, and updates sent while it was down are not
  replayed; none of upstream's adapters at the pin catches up on a reconnect either. So a default node
  re-checks only orders in flight (the in-flight check, through `QueryOrder`); an accepted order whose
  update was lost is re-checked only with the open-order check on, else at the next start. A node that
  holds orders for long enough to lose an update sets `LiveExecutionEngineConfig(open_check_interval_secs=...)`
  within upstream's recommended "5-10s" (how_to/configure_live_trading), as the paper node does at 10 s
  (`src/canon_trading/paper_node.py`); it costs one open-orders read per interval. Updates for orders the
  node does not hold are logged and dropped by default, naming the symbol and event without the venue
  order ID. `AlpacaExecutionClientConfig(report_external_orders=True)` instead sends a mapped status
  report with its stream fills, each under Alpaca's trade ID, through the same report output as queries.
  The pinned runtime [creates an external order and applies companion fills before any residual inference](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/execution/src/engine/mod.rs#L1679),
  assigning the strategy's active [external instrument claim](https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/reconciliation.md#external-order-creation).
  Before creation, the client keeps observed fills by trade ID and sends a snapshot only when those
  fills account for every filled share it shows: no remainder can be inferred. This also covers two
  updates queued before the engine creates the order, including observed fills from its traced
  replace chain, reported on the replacement. The pinned runtime can retain the earlier cached venue
  order ID when two outside replacement reports are queued before creation and no quantity or price
  changes; the real trade IDs and filled state still apply. Once the node holds it, later updates use the
  ordinary event path and deduplicate against the cache and record. The complete fills stay bundled
  with their status even if the order materializes during a mapping await, without a REST fill read.
  Completed closed orders release their stored fills immediately, and pending fills move off
  superseded legs onto their replacement. An unmapped type, status or client order ID is warned about
  and not stored or reported. An unloaded instrument is skipped with one warning per symbol.
  A missing trade ID, failed trace or incomplete stream fill set leaves the order to reconciliation:
  one warning names its symbol, side, type and client lineage, stored fills are dropped, and later
  outside updates send no partial fill set and repeat no warning. Outside-path messages omit venue
  order IDs. This path makes no extra reads to fill that gap. Its request cost is
  `_send_report`'s existing mapping cost: zero REST requests for a simple hand-placed market fill, with
  replace-chain reads only when that existing mapping needs them. The field stays off on the A and B
  windows unless their configuration changes. An update alpaca-py hands over unparsed is logged as an
  error. Events this slice does not map
  (`done_for_day`, `stopped`, `suspended` and the rest) are logged as warnings. If alpaca-py's `run()`
  returns, the client logs an error and does not start the stream again, and shutdown waits `STREAM_JOIN_TIMEOUT_SECS` for the stream thread and, at the same time,
  the task applying updates: up to 1 s for an update being applied to finish, then up to 2 s after its
  cancel; each by a deadline on the node's loop with no worker thread (a wait for a thread through
  `asyncio.to_thread` needs a free worker of the executor every alpaca-py call runs in), as upstream's
  Coinbase adapter bounds its task drain (`TaskGroup::finish_shutdown`, 1 s and 2 s). A wait that runs
  out fails the disconnect, and the client logs why, which NautilusTrader's own message does not say.
* **`report_external_orders` is not to be turned on yet.** The second review of that path (2026-11-12)
  found, from the code and from two scratch tests, defects that its tests do not cover; they are open:
  - an update for a replacement of an outside order the node has since materialized is dropped with no
    log, so its fills wait for the open-order or position check;
  - a report that is skipped (a late update on a replaced leg, a rejected replacement that did not take
    over) discards the chain's stored trade-ID fills, and the chain is then left to reconciliation
    although the stream delivered every fill;
  - one failed trace, or one update in a status this slice does not map, leaves the chain out of stream
    reports for the whole connection, with no retry; that includes a leg of an order the node holds,
    replaced twice outside it, whose later updates are then dropped without a line where `main` logged
    an error for each (the third review, 2026-11-12);
  - a normal `replaced` event is logged at warning level, and the sets that remember left chains and
    warned triggers only grow until disconnect.
  No node sets it; a node that adopts outside positions uses the engine's position check instead.
  It needs these fixed, each with a test, and a paper run before it is anyone's setting.
* **A failed trace or report names its cause sparsely.** When an order's replace chain cannot be traced
  or reported, the log names Alpaca's HTTP status and numeric body code, or the adapter's own named
  limit (`ChainTooLong`), and for any other error its class only. The error's text can carry a venue
  order ID, so it is left out, and with it the adapter's own words for a `ValueError` or `TypeError` of
  its making, which `main` printed. A refused submit, cancel or replace is unchanged: Alpaca's code
  and words.
* **Retries:** alpaca-py retries a request answered 429 or 504 up to three times, a submit, replace or
  cancel included (`RESTClient._request`), and `TradingClient` cannot be told not to. The client sends
  each order command once, as NautilusTrader's policies do ("Once, unless the safety test holds") and
  Alpaca's FAQ asks of an order that timed out ("You should not attempt to resend the order"): a requests
  response hook on alpaca-py's session ends a submit, replace or cancel at Alpaca's first error answer
  with alpaca-py's own `APIError` (`send_order_commands_once` at connect, `without_resend` per command, [ADR 0039](../../docs/adr/0039-alpaca-http-timeout.md)).
  Reads keep alpaca-py's retry. A 504 or 429 on an order command leaves the order as it was (NautilusTrader
  counts a 5xx and a rate limit as unknown unless the venue's semantics prove otherwise, and Alpaca's
  pages read state none for the Trading API), for the
  stream, the engine's in-flight `QueryOrder` (by client order ID when there is no venue order ID yet) or
  reconciliation. Alpaca refuses a repeated `client_order_id` with 422 "client_order_id must be unique"
  (paper, 2026-11-10), so a refusal does not prove an order absent: before a 403 or 422 rejects an order,
  the client asks Alpaca for it by client order ID and accepts it if Alpaca holds it; before a 422 rejects a
  cancel, it asks for the order and generates nothing if Alpaca shows it
  pending cancel, and `OrderCanceled` if canceled. A timeout (ADR 0039) is a transport error: a connect timeout on a call's
  only attempt proves nothing was sent, so a submit is rejected, a modify modify-rejected and a cancel cancel-rejected, each
  "not sent"; any other timeout leaves a submit `SUBMITTED` and a cancel as it was.

## Tests

`tests/` beside `src/`, run with pytest in `importlib` import mode from the repo root (`uv run --locked pytest`).
Upstream's template is covered by integration tests for factory registration, a market fill and shutdown.
The execution client has all three, offline. The data client has registration and shutdown; it places no
order, so it has no fill test, and its data delivery is tested instead. Each has paper tests:

`test_instrument_load.py` runs a real node on the stand-ins with more instruments than the execution
client's command queue holds (Known limits of the execution slice, "Instrument load and the command
queue"): a paper node's shape, the data client loading everything and the execution client one
instrument, with the refusals equal to the instruments less the capacity, the only error lines, and an
order at the start filled; both clients loading everything, each instrument refused once and the queue
empty when the strategy starts; a load of exactly the capacity notified in full with no refusal; the
execution client handing on what the data client did not load, and its own burst past the capacity
refused in turn.

`test_external_orders.py` checks the default reduce-only denial for BUY and SELL on all four supported
types, the opt-in sell_to_close requests and BUY refusal, strict settings and their named errors,
ordinary requests unchanged with the setting off, opt-in intent on ordinary long-closing
SELLs without changing their flags, and no configuration read at connect, on queries or on any order.
It also checks opt-in outside reports and real trade-ID fills,
instrument-claim position adoption, updates queued before cache creation, duplicates and replace-chain
fills, pending-fill cleanup, cache materialization during mapping, unmapped and unloaded instruments,
and incomplete evidence left to reconciliation once, all without extra fill reads. Venue refusal
messages and enforcement on that path still need a paper run on account A.
It checks that an outside trailing stop fails the start by name while `report_external_trailing_stops`
is off and, with it on, trailing stop reports by price and percent, their startup materialization with and without
Alpaca's current stop price (including a fine advisory trigger reported as None), unchanged trailing
submission refusal, and the remaining class/time-in-force
and malformed-offset failures by name.

| File | Covers | Network |
|---|---|---|
| `test_parsing.py` | Asset to `Equity`; bar, quote and trade conversion; bar type support; missing credentials | None |
| `test_provider.py` | Asset queries and filters, load by ID, failures named, other venues refused | None |
| `test_data_client.py` | A real `LiveNode`: registration through config and builder, owned and hosted launch, bars, quotes and trades into strategy and cache, hourly and daily bar windows, `limit`, open-ended requests, concurrent and cancelled bar requests, request failures as `failure_reason`, unsubscribes that never subscribed or failed, late data for an unsubscribed symbol dropped, late data from a stream left behind by a timed-out disconnect dropped after a reconnect, a resubscribe delivering again, trade cancels and corrections logged, one-sided quotes dropped and counted and a NaN price dropped as malformed, a TICK bar request refused with the adapter's reason, the feed named in params (the default IEX, a named feed on the stream and the bars request, other keys left alone, a second feed refused, a name or value Alpaca does not take refused before anything is sent, a feed Alpaca refuses logged naming it and later subscriptions refused), a repeat subscription after the stream ended refused, concurrent unsubscribes sending one, a reconnect waiting for a bar call a cancelled request left running, a given stream that never ran serving the next connect, windows with no `start` (an earlier `end`, either clock change), a request the executor refuses releasing the lock, a disconnect cancelled by the node clearing state, the stream thread holding no reference to the client inside a handler, the stream stand-in connecting only once something is subscribed, as alpaca-py's does, a stream ending by itself logged (and an abandoned one not) and a later subscription refused, clients given at construction kept across a reconnect, a cancelled bar call's later error read, shutdown joining the stream thread or clearing state when it does not stop, stream handlers not keeping the client alive, paper and IEX client construction; a cancelled stream call holding the next until alpaca-py returns and a refused one recording nothing, a subscription after the stream's `run()` returned refused while its thread winds down, a connect that fails or is cancelled part way and a stream thread that cannot start leaving nothing behind, `feed: None` refused, an unsubscribe's feed read as a subscription's (and the engine passing it the subscription's params), a window starting after now sending nothing, another client's abandoned stream's log lines not taken for this one's, a quote from before an unsubscribe dropped after a resubscribe, an unsubscribe after a subscription refused for its feed tolerated, a subscription while disconnecting refused as such, no hook waiting on a call stuck in an ended stream, a window inverted in the past still sent, a subscription lost while the stream connects sent on its first message (on alpaca-py's real stream and the stand-in, and one the node made before a reconnect warning reached it, a resend finished after that warning leaving the next connection owed one), once per run and only when a later subscribe could have been lost, a failed resend logged and tried again on the next message, a stream call waiting through a reconnect taking the new connect's lock, a stream call that returns at once recorded before its hook returns; historical quotes (ADR 0066): stamped when Alpaca stamped them in an inclusive window sent on IEX, written back into a catalog and a covered request served from it, an empty response recording nothing and asked for again, a staging file a killed write leaves not read and the window written again, a request with a `limit` answered and not written back, one-sided quotes and quotes finer than four decimals dropped and counted, a NaN quote failing the request, sip trade and bar prices finer than four decimals rounded and counted (747.086488 and 0.12345) and such a quote dropped and named, each on a warning the session window keeps, streamed minute bars and trading statuses (ADR 0090: a stream subscription and a wildcard one handing on only listed symbols, another bar type refused, updated bars only when asked, each status code's action and flags, the last listed symbol's unsubscribe unsubscribing the wildcard, a reconnect sending it again), a feed other than `iex` refused before sending, a fetch failure as `failure_reason`, two instruments refused, a window after now sending nothing and one ending after now cut at now | None |
| `test_paper_feed.py` | ADR 0055 through a real node: a subscription naming `boats` streams on `v1beta1/boats` and its quotes and trades reach the strategy (overnight only, from Alpaca's calendar; it waits up to 120 s for a trade, and skips, naming the counts, when quotes came and Alpaca confirmed the trade subscription but no trade came); while the market is open, trades and quotes naming `sip` stream on `v2/sip` for 60 s, no trade is dropped, every price keeps four decimals, and the rounded prices and dropped quotes the client names are counted; one naming none streams on `v2/iex`, as before (any hour). ADR 0090: minute bars and trading statuses for eight listed symbols subscribed with `params={"feed": "sip", "wildcard": True}` for 150 s while sip is in session on Alpaca's calendar; Alpaca must confirm `"*"` on both channels, a bar must arrive and every bar must be a listed symbol's one-minute last bar, and the bars per minute, symbols seen and statuses received are printed. Shaped on TC-D20, TC-D30 and TC-D71, by a small strategy, since v2.0.0rc8's Python `DataTesterConfig` takes no `subscribe_params` | Paper only; places nothing; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_sip_bars.py` | ADR 0055, prices finer than four decimals, through a real node: SPY bars on `sip`, one-minute for three days and daily for a year to 20 minutes ago, each request answered with every price at four decimals and the bars the client rounded counted from its warning (TC-D50's shape, a small strategy); any hour, and no stream lock, since a bars request opens no stream | Paper only; places nothing; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_integration.py` | One asset and a week of daily bars from Alpaca's paper environment through a real node, from midday New York on a fixed trading day: the first bar must be the one that opened at that day's midnight, which Alpaca returns only because the adapter shifts the window | Paper only; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_execution_client.py` | A real `LiveNode` with both clients: registration through config and builder, owned and hosted launch, a market order that fills, partial fills, a limit order canceled, cancel-all by ID (side filter, a refused cancel, a failed cancel, nothing to cancel), rejections by HTTP status and by trade update, a cancel refused, a cancel refused for an order Alpaca canceled while the stream was down, a refusal whose lookup alpaca-py cannot build (submit, cancel, modify), a replacement rejected after `replaced`, fills listed before a replace, clients given at construction kept across a reconnect, a cancelled call's later error read, stop and stop-limit orders sent with their prices, modify (`OrderUpdated` only on Alpaca's `replaced`, a fill on the replacement reaching the node's order with the client's record full and dropped, two replaces in a row, reports of a replacement under the node's ID, refusals, a refused repeat, `order_replace_rejected`, a rejected replacement, local denials, a stop's trigger price, a replacement that cannot be read, a replacement whose prior order cannot be read, open or closed, two orders with an empty client order ID), orders denied before Alpaca (type, time in force, stop trigger type, sub-penny stop prices, not tradable), a limit finer than Alpaca's increment sent on it (a buy up, a sell down, a submit and a modify, a stop-limit's limit) with `OrderUpdated` carrying the price sent and a status report agreeing, an unknown submit outcome left `SUBMITTED`, an update for an unknown order dropped, start-up fills from Alpaca's fill activities (trade IDs and prices, paging, the window, one order, an activity it cannot map skipped, a page whose last activity has no `id`, a body that is not JSON, fills not all listed whatever their age or with no `cum_qty` and `leaves_qty`, the order read to check them and one alpaca-py cannot build, an activity listed twice), start-up reconciliation of an open order (older than 500 newer closed orders, or before an open-only check's window), a filled order, a position and open stop and stop-limit orders, an unmapped open order failing start-up, account state on connect (and an answer without `cash` or `currency` refused naming it, at connect and on a query), order commands answered 504 or meeting a connect or read timeout sent once by alpaca-py's own `TradingClient` over a stand-in network (submit, replace, cancel; a read still repeated, as alpaca-py repeats a request unguarded), the trading client's transport retrying nothing, and a POST that never gets an answer reaching a local server once, paper-only client construction, shutdown joining the trade stream thread; the config's paper-only refusals, and a node refusing a plain `ExecutionClientConfig`; HTTP timeouts (a local server that never answers: the call ends, the next is answered, the run ends; a connect timeout on the only attempt rejects a submit or cancel as not sent, a read timeout or a repeated attempt leaves it unknown; a session holding another timeout, or the adapter on HTTPS only, gets the package's on both schemes); `QueryOrder` from the engine's in-flight check (a submit Alpaca took with no answer, a cancel answered 204 for an order Alpaca canceled unseen), an acknowledgement during a pending cancel, a refused submit Alpaca holds closed, one rejection from the stream and the answer, no cancel rejection after a fill, a fill that cannot be applied, positions in another or an unloaded symbol, values that are not numbers, a late fill on a replaced order, a replacement rejected before `replaced`, replaced again before it was read, or adopted and then rejected, an order replaced between the two reads, one order's fill reports over its chain, a given client across a reconnect with a call still running, a given stream after a connect that failed early, a modify to zero, a held stop's trigger type; the replace chain as one record (a takeover rejected after a fill ending canceled, on the stream and at start-up; a late rejection of a replacement a later replace left behind; fills across two replaces, in bulk and asked for by each order; a reconnect, after which a fill on the replacement needs no read and a late cancel of the replaced order is ignored; a rejected replacement of an outside order with no client order ID; chains past 16 links read or traced, and a trace needing more reads failing by name; `order_replace_rejected` after a successful answer; a fill delivered twice; a fill a status report showed (a `QueryOrder` answer, on the same connect and after a reconnect, and an order since replaced) brought by its trade ID and not counted again from the stream, and the order's next fill still counted; a stream fill after a fill report for a later share, and a `partial_fill` whose order shows the count before it, each counted; a fill report and stream fills interleaved, each counted once; one identity by trade ID (ADR 0075: a report and its fill's update in one loop turn, a larger fill after a smaller one, two prices out of order, a lost update under the paper node's checks with no `EXTERNAL` order, a report held back while the activities lag, a fill on a chain order the node never held, a replacement's fill after a reconnect under either reading, a full-history check, and an open order whose fills are not listed failing start-up by name or listed on a second read); both readings of a replacement's quantities, on the stream and at start-up, and the partly filled replace paper showed); a submit never answered that Alpaca fills later (the session window's claim 10, ADR 0044: an entry and a closing sell, the update delivered or lost, with the engine retaining the submit and the strategy claiming its instrument, each alone and neither; a lost update's fill reaching the retained order by its client order ID; a submit Alpaca never placed, retained or resolved locally); a failed test not failing the next at `build_node` | None |
| `test_paper_execution.py` | DAY limit buys of 1 AAPL far below the market on Alpaca's paper Trading API through a real node: one submitted, accepted and canceled; upstream's `ExecTester` running rows of its Execution Testing Spec: TC-E41, a limit buy and a stop buy canceled by `CancelAllOrders` on stop, TC-E20 and TC-E22, a stop and a stop-limit buy accepted at the prices sent and reported back the same, and TC-E30, whose one-tick amend the client denies as sub-penny; a $1.00 buy modified to $1.01 through a node, then canceled; TC-E84, an open order placed with alpaca-py loaded by a node's start-up reconciliation; TC-E44's venue side, a canceled order canceled again; and a resting buy's `client_order_id` submitted again, refused 422 with one order held | Paper only; cancels every order it places; each places nothing and fails if the paper account already holds an open AAPL order or position; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_overnight_fill.py` | ADR 0038 and ADR 0055 overnight through a real node with both clients: a DAY limit buy of 1 AAPL marked `extended_hours`, priced 0.2% through the ask of the `boats` quote the node receives, filled, and its share sold 0.2% through the bid; overnight only, and only while AAPL's asset `attributes` list `overnight_tradable` and not `overnight_halted`. No row of the Execution Testing Spec covers it (TC-E13 is IOC, which the overnight session does not take) and `ExecTesterConfig` names no feed, so a small strategy runs it; an unfilled order is reported `xfail` with what Alpaca held | Paper only; trades a real paper share; places nothing and fails if the account holds an open AAPL order or position; cancels what it opened and closes the position with an eligible limit priced from `boats`; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_fill_ids.py` | ADR 0024's test, with alpaca-py directly, not through a node: a market buy of 2 AAPL and a market sell of 2 on Alpaca's paper Trading API; every fill and partial fill update's `execution_id` must be the UUID after `::` in one of the order's `FILL` activity `id`s | Paper only; needs the regular session and skips outside it; stops if the account holds any open order or position; sells what it bought; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_matrix.py` | Upstream's `DataTester` and `ExecTester` through a real node on Alpaca's paper environment: the rows in "Upstream's test matrices against paper" | Paper only; needs the regular session for the stream and fill rows; stops if the account holds any open order or position; cancels and closes what its node opened; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_flat_reconciliation.py` | TC-E85 on the shared account's past orders, with no lookback: with Alpaca holding no position, start-up must leave none open, through the explicit flat reports (no `EXTERNAL` position) | Paper only; places nothing, so needs no session; stops, touching nothing, if the account holds any open order or position; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_fill_reports.py` | TC-E85 with fill reports: a market buy of 2 AAPL with alpaca-py, its trade updates recorded; a node's start-up reconciliation must give the order one fill per update, with that update's `execution_id` and price | Paper only; needs the regular session and skips outside it; stops if the account holds any open order or position; sells what it bought; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_paper_replace.py` | Alpaca's replace through alpaca-py directly: a $1.00 buy replaced with $1.01; asserts what a paper observation showed and the modify rests on (a replacement may not keep the client order ID, the answer's new IDs and `replaces`, `replaced` carrying the old order with `replaced_by`, a second replace refused), and since 2026-11-12 what its replace now sends (a replace naming a new client order ID is taken and the replacement carries it; not yet run on paper) | Paper only; needs the regular session; places nothing while the account holds an open AAPL order, cancels what it opened |
| `test_paper_quotes_write_back.py` | ADR 0066's first condition through a real node: SPY's IEX quotes for 10:00 to 10:03 New York on 2026-11-09 written back into a temporary catalog (never `catalog/`) and a covered request writing nothing; an empty Sunday minute recording nothing; the same window requested in a child process killed with SIGKILL when its catalog logs "Writing", what it left checked (no data file and no coverage, or the complete file) and the window then filled. As ADR 0040's bars paper test; no row of the Data Testing Spec covers write-back | Paper only; reads only, places nothing; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_request_counter.py` | Offline, real alpaca-py clients on placeholder keys and requests' transport answered here: requests per UTC minute and the peak minute, trading and data apart, two key pairs as two accounts with no key value in the counts, a 429 recorded by name, time and path with alpaca-py's retry kept and every attempt counted, the latest 429s kept and all counted, a data-only refusal not counted, a failed connect counted, a burst across a minute's end in the 60-second peak, a clock stepped back, concurrent sends, an `Authorization` header as an account and kept only as a keyed digest | None |
| `test_paper_request_count.py` | One asset read on the paper Trading API and one IEX latest quote on the Market Data API, each counted under one account and its own API; prints every count of what the process sent before it, so the tests to be counted go before it in the run | Paper Trading API and Market Data API; places nothing |
| `test_data_only.py` | ADR 0070 offline: `submit_order`, `replace_order_by_id`, `cancel_order_by_id` and `close_position` through the provider's client, fetched twice, each refused with nothing reaching requests' transport, while a GET goes out; the refusing adapter kept when `with_http_timeout` runs again and restored over one mounted for a longer prefix; a node in data-only mode whose historical or provider client sends on a session without it does not start, and with both clients refusing it does; every alpaca-py `Session`, `_session` assignment and REST client class, and every REST client this repository builds, enumerated, each passed to `with_http_timeout` | None |
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

A limit, not reproduced: on 2026-11-11, under a machine load of about 60, one full run spun for 22 minutes
in `test_execution_client.py` near `test_a_report_whose_replacement_alpaca_does_not_find_is_none`. The
report path under that test is bounded: the replace chain walk by `MAX_REPLACE_CHAIN`, alpaca-py's
retries by `DEFAULT_RETRY_ATTEMPTS`, each read by the package's HTTP timeout (ADR 0039), and every test's
run by `run_with_client`'s 15 s timeout. The one wait the harness leaves unbounded is the node's own end
after that timeout fires and the node is stopped (`await task`); a stop sent while the node starts is
kept, not lost (`test_handle_stop_blocks_running_transition` in `crates/live/src/node/mod.rs` at the
pin), and whether every stop ends the run in bounded time is UNVERIFIED.
Rerun with the test and its neighbours at a load of about 27, under `-o faulthandler_timeout=120` (which
dumps every thread's stack if a test outlives it), it passed in 3 s. If it spins again, that dump names
the wait.

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
reduce-only on stop, which the adapter refused during these earlier runs; start-up reconciliation looks back one
minute on the shared account (see the reconciliation finding below); and the node keeps a post-stop grace
period, since disconnection "discards queued commands" (python adapter guide, "Disconnect and drain
tasks"): with none, a queued unsubscribe was discarded and logged as an error once.

| Rows | Outcome |
|---|---|
| TC-D03 load an instrument | Passed |
| TC-D20 subscribe quotes | Passed: 256 to 595 quotes in 30 s; quotes with bid below ask and both sizes positive, none crossed |
| TC-D21 historical quotes | To run at the regular session's open (`test_paper_data_tester_historical_quotes`); not yet run against paper. The quotes path is shown on paper by `test_paper_quotes_write_back.py` (ADR 0066) |
| TC-D30 subscribe trades | Passed, except the aggressor side: 5 to 28 trades in 30 s; Alpaca sends no aggressor, so every trade is `NO_AGGRESSOR` |
| TC-D41 historical bars | Passed: 60 one-minute bars, valid OHLC, no duplicate timestamps |
| TC-D70 unsubscribe on stop | Passed with the grace period: quotes and trades unsubscribed, no tick after, no warning or error |
| TC-D31 historical trades | Implemented and tested offline through a real node and fake HTTP pages (`test_data_client.py`): pagination, limits, feed selection, timestamps, IDs, whole shares, rounding, response metadata and failure propagation; paper API response UNVERIFIED |
| TC-D01, D02, D40, D60, D61, D73 | Not in this slice: no instrument requests or subscriptions, no bar subscriptions, no status or close; D73 not run |
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
| TC-E30 modify limit buy | Ended `OrderModifyRejected` before Alpaca while sub-penny limits were denied (2026-11-10, ADR 0042). Since 2026-11-11 the amend is rounded up to the cent (ADR 0030), which is the order's own price, and a replace that changes nothing is refused before Alpaca, still `OrderModifyRejected`; UNVERIFIED on paper until `paper:test` runs in a regular session |
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
