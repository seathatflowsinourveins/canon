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

`load_all=True` loads every active US equity instead; its `filters` may set `status`, `exchange` or
`attributes` (the `GetAssetsRequest` fields). Node configuration itself belongs in `src/canon_trading/`,
not here.

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
  increment, so the finer one is used and market data is never rounded. The instrument does not stop a
  sub-penny order at or above $1.00; the execution slice must.
* **Quote sizes are in round lots**, as Alpaca's stream schema states, not shares. Alpaca gives no lot size
  to convert with.
* **Bars:** `LAST`, `EXTERNAL`, MINUTE, HOUR, DAY or WEEK (DAY and WEEK at step 1 only); stamped at the
  open by Alpaca, so `ts_init = ts_event + interval`, per NautilusTrader's bar timestamp convention.
* **Trades** carry no aggressor side (`NO_AGGRESSOR`).
* **Feed:** IEX only, the feed a paper-only account is entitled to.
* **Timestamps** lose sub-microsecond precision: alpaca-py parses them into `datetime`.
* **No restart:** if alpaca-py's stream ends by itself, the client logs an error and does not restart it.
  If the stream cannot authenticate, alpaca-py retries without ending and reports it only through Python
  `logging`, which the node does not show: subscriptions then deliver nothing, with no adapter error.
* **Bar requests:** `limit` goes to Alpaca as it is, so with no `start` Alpaca returns the first `limit`
  bars of the current day, not the latest. A window ending now may include the bar still forming, passed
  on as Alpaca returns it (UNVERIFIED whether Alpaca returns it). Requests are made one at a time, because
  alpaca-py's REST client holds one `requests.Session`.
* **Instrument loading:** `load_ids` fetches one asset per request, in sequence, inside the connect, so a
  long list can exceed the node's connection timeout. `load_all` fails as a whole if alpaca-py cannot
  parse any one asset record (its models validate the full list).

## Tests

`tests/` beside `src/`, run with pytest in `importlib` import mode from the repo root (`uv run pytest`).
Upstream's template is covered by integration tests for factory registration, a market fill and shutdown.
This slice has the two that apply to a data client, offline, plus a paper test:

| File | Covers | Network |
|---|---|---|
| `test_parsing.py` | Asset to `Equity`; bar, quote and trade conversion; bar type support; missing credentials | None |
| `test_provider.py` | Asset queries and filters, load by ID, failures named, other venues refused | None |
| `test_data_client.py` | A real `LiveNode`: registration through config and builder, owned and hosted launch, bars, quotes and trades into strategy and cache, request failures as `failure_reason`, unsubscribe on stop, a stream ending by itself logged, shutdown joining the stream thread, paper and IEX client construction | None |
| `test_paper_integration.py` | One asset and 14 days of daily bars from Alpaca's paper environment through a real node | Paper only; skipped without `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` |
| `test_upstream_contract.py` | The upstream names this README quotes still exist in the pinned packages | None |

The offline tests use payloads hand-built from the examples in Alpaca's docs (sources in `tests/conftest.py`)
and stand-ins for the three alpaca-py clients. Not yet tested against Alpaca: the live quote and trade
stream, and the asset list at `load_all` scale.

## Rules

Paper credentials and paper endpoints only. See `.claude/rules/trading-safety.md`.
