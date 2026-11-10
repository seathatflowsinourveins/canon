# canon-alpaca

An out-of-tree NautilusTrader adapter for Alpaca, built on `alpaca-py`.

**Status: first slice, data only.** It loads US equities as instruments, serves historical bars on request
and streams quotes and trades, all against Alpaca's paper data. There is no execution client: nothing in
this package places, modifies or cancels an order. The offline tests pass; the paper integration test has
not run yet (see [Tests](#tests)). Nothing should depend on the adapter until that test and the execution
slice exist.

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
| `factories.py` | `AlpacaDataClientFactory` | Built (data only) |
| `execution.py` | Account state, reconciliation, order execution hooks | Not started |

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

`load_all=True` loads every active US equity instead; its `filters` may set `exchange` or `attributes`
(the `GetAssetsRequest` fields), and `status` only to `active`. `filter_callable` is refused when the
provider is built: NautilusTrader v2's Python `InstrumentProvider` never applies it, and upstream's own
Binance adapter refuses it in v2. Node configuration itself belongs in `src/canon_trading/`, not here.

Credentials come from the environment: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, holding a **paper** key
pair. alpaca-py 0.45.0 reads no environment variable itself; these are the names Alpaca's own CLI and MCP
server read. They are read in the client's `_connect`, never stored in a config, and never logged. If they
are missing, the connect fails and the node log names both variables.

## Contract

From upstream's Python adapter guide (`developer_guide/python_adapters`):

* Subclass `DataClient` or `MarketDataClient`, and `ExecutionClient`, from `nautilus_trader.live.clients`.
* Provide `DataClientFactory` and `ExecutionClientFactory` subclasses. `create(*, name, config, cache, clock)`
  returns the client synchronously; execution factories also receive `trader_id`.
* Name classes `Alpaca<Role>`: `AlpacaDataClient`, `AlpacaDataClientConfig`, `AlpacaDataClientFactory`, and
  the same for execution. No `Live` prefix. This slice has no adapter config fields, so it uses a plain
  `DataClientConfig` and there is no `AlpacaDataClientConfig` yet.
* Keep constructors free of tasks and network resources. Schedule background work with `self.create_task`
  and close resources in `_disconnect`. Here the alpaca-py clients are built in `_connect`; the stream
  thread starts on the first subscription and is stopped and joined in `_disconnect`.
* Keep node configuration and strategies out of this package. They live in `src/canon_trading/`.

The class names, import paths and factory signatures above are checked against the installed pinned package
by `tests/test_upstream_contract.py`. If that test fails after a pin moves, re-read the guide first.

Known limits of custom Python clients upstream: no Redis or PostgreSQL cache backing; clients emit
`CustomData` only (no component `publish_message` or topic subscription).

## Known limits of this slice

Each is stated where it is implemented, with its source.

* **Whole shares only.** NautilusTrader's `Equity` cannot hold a fractional quantity (ADR 0009). Alpaca's
  `fractionable` flag is kept in `Equity.info`; a fractional size in market data is refused, not rounded.
* **One price increment, 0.0001.** Alpaca's assets endpoint gives no price increment for US equities, and
  Alpaca's order rule is price-dependent: at most 2 decimals at or above $1.00, 4 below. `Equity` takes one
  increment, so the finer one is used and market data is never rounded: a bar, quote or trade price
  finer than 0.0001 is refused, not rounded (`Instrument.make_price` would round it silently). The
  instrument does not stop a sub-penny order at or above $1.00; the execution slice must.
* **Quotes need both sides.** A quote with a zero or negative bid or ask price is dropped, since a
  `QuoteTick` cannot hold a one-sided quote. Outside market hours and on thin names such quotes can be
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
* **Feed:** IEX only, the feed a paper-only account is entitled to.
* **Timestamps** lose sub-microsecond precision: alpaca-py parses them into `datetime`.
* **No restart:** if alpaca-py's stream ends by itself, the client logs an error and does not restart it.
  If the stream cannot authenticate, alpaca-py retries without ending and reports it only through Python
  `logging`, which the node does not show: subscriptions then deliver nothing, with no adapter error.
* **Stream shutdown:** `_disconnect` waits `STREAM_JOIN_TIMEOUT_SECS` (10 s) for the stream thread. A
  connected alpaca-py stream sees the stop within its 5 s receive timeout and then closes the websocket,
  which `websockets` allows 10 s, so a slow close can exceed it; a stop during connect or auth waits for
  that to finish. When it is exceeded, the client clears its state and raises, the node logs the failed
  disconnect, and the daemon thread finishes on its own with its websocket still open until then. A
  later connect builds a new stream; whether Alpaca accepts it while the old connection is still open is
  UNVERIFIED.
* **Bar windows:** Alpaca filters bars on their open time, inclusive at both `start` and `end` (taken from
  its documentation, https://docs.alpaca.markets/us/reference/stockbars.md; checked against the live API
  only by the paper test below). NautilusTrader keeps the bars whose `ts_init` lies in the request's
  `[start, end]` and applies no `limit` itself. So the adapter sends `start` and `end` one bar interval
  earlier, and a request returns exactly the bars whose `ts_init` (open plus one interval) lies in its
  window; the response keeps the bounds as requested. The window ends at the earlier of the requested
  `end` and the node clock's current time (`now` when there is no `end`; NautilusTrader's `request_bars`
  itself refuses an `end` after now), so the bar still forming, whose `ts_init` is in the future, is
  never returned. A request with no `start` starts at midnight New York of the current day: Alpaca's
  reference gives "the beginning of the current day" as its own default but names no time zone, so the
  adapter supplies the start itself, and the window sent never ends before it starts.
* **Choosing a window:** to get the bar for a day or week, ask for one that holds its open plus its
  length with hours to spare: from noon New York on the bar's last day to noon the next day works on
  every day, clock changes included. A window ending exactly at midnight misses a weekly bar that spans
  the spring change (its `ts_init` is 01:00), and a window inside a day's session returns no daily bar.
* **Bar requests:** `limit` goes to Alpaca as it is and returns the *earliest* bars of the window, since
  Alpaca sorts ascending by default. With no `start`, the window is today in New York so far: for MINUTE
  and HOUR, the first `limit` bars that closed since midnight; for DAY, the previous calendar day's bar,
  whose `ts_init` is this midnight, so none on a Monday or the day after a holiday; for WEEK, last
  week's bar on a Monday (from 01:00 after the spring change) and none on other days. alpaca-py's
  `sort` parameter (`Sort.DESC`) would return the latest instead; it is not adopted here. If
  any one bar in the response is refused (a fractional volume, a price finer than 0.0001), the whole
  request fails. A refused or failed request reaches the strategy as a response with a `failure_reason`
  and no bars. Requests are made one at a time, because alpaca-py's REST client holds one
  `requests.Session`; a waiting request holds an `asyncio.Lock` on the node's loop, not a worker thread,
  and the lock is released only when the HTTP call returns, so a request cancelled mid-call (as on
  shutdown) still keeps the next one waiting.
* **No HTTP timeout:** alpaca-py sets none and offers no setting for one (`RESTClient._one_request` calls
  `requests.Session.request` without `timeout`). A bar request whose HTTP call stalls never answers: the
  strategy gets no response, and every bar request queued behind it waits too, until the operating
  system ends the connection. Subscriptions and the stream's shutdown are not held up by it. A stalled
  asset request holds up the connect the same way, until the node's connection timeout.
* **Instrument loading:** only active assets load; an inactive asset, or an asset whose symbol is not the
  one requested, is refused with its ID named. Assets Alpaca marks not tradable still load, because their
  market data does not depend on it; `tradable` is kept in `Equity.info` for the execution slice. `load_ids`
  fetches one asset per request, in sequence, inside the connect, so a long list can exceed the node's
  connection timeout; every ID is attempted, network errors included, and the error names each that
  failed. `load_all` fails as a whole if alpaca-py cannot parse any one asset record (its models validate
  the full list).

## Tests

`tests/` beside `src/`, run with pytest in `importlib` import mode from the repo root (`uv run pytest`).
Upstream's template is covered by integration tests for factory registration, a market fill and shutdown.
This slice has the two that apply to a data client, offline, plus a paper test:

| File | Covers | Network |
|---|---|---|
| `test_parsing.py` | Asset to `Equity`; bar, quote and trade conversion; bar type support; missing credentials | None |
| `test_provider.py` | Asset queries and filters, load by ID, failures named, other venues refused | None |
| `test_data_client.py` | A real `LiveNode`: registration through config and builder, owned and hosted launch, bars, quotes and trades into strategy and cache, hourly and daily bar windows, `limit`, open-ended requests, concurrent and cancelled bar requests, request failures as `failure_reason`, unsubscribes that never subscribed or failed, late data for an unsubscribed symbol dropped, trade cancels and corrections logged, one-sided quotes dropped and counted, a stream ending by itself logged (and an abandoned one not), shutdown joining the stream thread or clearing state when it does not stop, stream handlers not keeping the client alive, paper and IEX client construction | None |
| `test_paper_integration.py` | One asset and a week of daily bars from Alpaca's paper environment through a real node, from midday New York on a fixed trading day: the first bar must be the one that opened at that day's midnight, which Alpaca returns only because the adapter shifts the window | Paper only; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_upstream_contract.py` | The upstream names this README quotes still exist in the pinned packages; the stream stand-in refuses and keeps what alpaca-py's `StockDataStream` does | None |

The offline tests use payloads hand-built from the examples in Alpaca's docs (sources in `tests/conftest.py`)
and stand-ins for the three alpaca-py clients. Not yet tested against Alpaca: the live quote and trade
stream, and the asset list at `load_all` scale.

## Rules

Paper credentials and paper endpoints only. See `.claude/rules/trading-safety.md`.
