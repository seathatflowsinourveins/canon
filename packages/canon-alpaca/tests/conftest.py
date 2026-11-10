"""Offline fixtures: Alpaca payloads and stand-ins for the three alpaca-py clients the adapter uses.

Every payload is hand-built from an example in Alpaca's docs, read 2026-11-09, and parsed with the pinned
alpaca-py's own models, so the adapter's conversions are tested against what alpaca-py hands it:

* asset: "Get Assets", schema example (https://docs.alpaca.markets/us/reference/get-v2-assets-1.md)
* bar: "Historical bars", response example (https://docs.alpaca.markets/us/reference/stockbars.md)
* trade and quote: "Real-time Stock Data", schema examples
  (https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md). The stream sends `t` as a msgpack
  timestamp, and alpaca-py's `DataStream._cast` converts it with `Timestamp.to_datetime()`, a UTC
  `datetime` with microsecond precision, before building the model. The fixtures write that `datetime`
  directly, so the tests do not import msgpack (not in `canon/upstreams.json`).

The stand-ins implement only the methods the adapter calls, with alpaca-py 0.45.0's signatures. No test
here opens a network connection or reads a credential.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import Trade
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.models import Asset

type Handler = Callable[[object], Awaitable[None]]

ASSET_AAPL = {
    "borrow_status": "easy_to_borrow",
    "class": "us_equity",
    "easy_to_borrow": True,
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "name": "Apple Inc. Common Stock",
    "shortable": True,
    "status": "active",
    "symbol": "AAPL",
    "tradable": True,
}

BAR_AAPL = {
    "c": 178.21,
    "h": 178.26,
    "l": 178.21,
    "n": 65,
    "o": 178.26,
    "t": "2022-01-03T09:00:00Z",
    "v": 1118,
    "vw": 178.235733,
}

TRADE_AAPL = {
    "T": "t",
    "i": 96921,
    "S": "AAPL",
    "x": "D",
    "p": 126.55,
    "s": 1,
    "t": datetime(2021, 2, 22, 15, 51, 44, 208_000, tzinfo=UTC),  # "2021-02-22T15:51:44.208Z"
    "c": ["@", "I"],
    "z": "C",
}

QUOTE_AAPL = {
    "T": "q",
    "S": "AAPL",  # the docs' example is AMD; the symbol is changed so one instrument serves all tests
    "bx": "U",
    "bp": 87.66,
    "bs": 1,
    "ax": "Q",
    "ap": 87.68,
    "as": 4,
    # The docs' "2021-02-22T15:51:45.335689322Z", as to_datetime() gives it: the last 322 ns are dropped.
    "t": datetime(2021, 2, 22, 15, 51, 45, 335_689, tzinfo=UTC),
    "c": ["R"],
    "z": "C",
}


def asset(**changes: object) -> Asset:
    return Asset.model_validate({**ASSET_AAPL, **changes})


def quote() -> Quote:
    return Quote(QUOTE_AAPL["S"], dict(QUOTE_AAPL))


def trade() -> Trade:
    return Trade(TRADE_AAPL["S"], dict(TRADE_AAPL))


class FakeTradingClient:
    """Stands in for `TradingClient`'s two asset reads."""

    def __init__(self, assets: list[dict] | None = None) -> None:
        self.assets = assets if assets is not None else [ASSET_AAPL]
        self.all_requests: list[object] = []
        self.symbol_requests: list[object] = []

    def get_all_assets(self, filter: object = None) -> list[Asset]:
        self.all_requests.append(filter)
        return [Asset.model_validate(a) for a in self.assets]

    def get_asset(self, symbol_or_asset_id: object) -> Asset:
        self.symbol_requests.append(symbol_or_asset_id)
        for a in self.assets:
            if a["symbol"] == symbol_or_asset_id:
                return Asset.model_validate(a)
        raise APIError('{"code": 40410000, "message": "asset not found"}')


class FakeHistoricalClient:
    """Stands in for `StockHistoricalDataClient.get_stock_bars`."""

    def __init__(self, bars: list[dict] | None = None, error: Exception | None = None) -> None:
        self.bars = bars if bars is not None else [BAR_AAPL]
        self.error = error
        self.requests: list[object] = []

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet:
        self.requests.append(request_params)
        if self.error is not None:
            raise self.error
        symbol = request_params.symbol_or_symbols
        assert isinstance(symbol, str)
        return BarSet({symbol: list(self.bars)})


class FakeStream:
    """Stands in for `StockDataStream`: `run()` blocks on its own asyncio loop until `stop()`.

    While running, it sends the recorded quote and trade once to each handler subscribed for AAPL, the way
    alpaca-py's stream awaits a coroutine handler per message. With `ends_on_its_own`, `run()` returns
    after its first delivery without a `stop()`, as alpaca-py's does after an insufficient-subscription
    error.
    """

    def __init__(self) -> None:
        self.ends_on_its_own = False
        self.quote_handlers: dict[str, Handler] = {}
        self.trade_handlers: dict[str, Handler] = {}
        self.unsubscribed: list[tuple[str, tuple[str, ...]]] = []
        self.run_threads: list[str] = []
        self.stopped = threading.Event()
        self._changed = threading.Event()

    def subscribe_quotes(self, handler: Handler, *symbols: str) -> None:
        self.quote_handlers.update(dict.fromkeys(symbols, handler))
        self._changed.set()

    def subscribe_trades(self, handler: Handler, *symbols: str) -> None:
        self.trade_handlers.update(dict.fromkeys(symbols, handler))
        self._changed.set()

    def unsubscribe_quotes(self, *symbols: str) -> None:
        self.unsubscribed.append(("quotes", symbols))
        for s in symbols:
            del self.quote_handlers[s]  # alpaca-py raises KeyError for a symbol never subscribed

    def unsubscribe_trades(self, *symbols: str) -> None:
        self.unsubscribed.append(("trades", symbols))
        for s in symbols:
            del self.trade_handlers[s]

    def run(self) -> None:
        self.run_threads.append(threading.current_thread().name)
        asyncio.run(self._serve())

    def stop(self) -> None:
        self.stopped.set()
        self._changed.set()

    async def _serve(self) -> None:
        sent: set[tuple[str, str]] = set()
        while not self.stopped.is_set():
            for channel, handlers, message in (
                ("quotes", self.quote_handlers, quote),
                ("trades", self.trade_handlers, trade),
            ):
                for symbol, handler in list(handlers.items()):
                    if (channel, symbol) not in sent:
                        sent.add((channel, symbol))
                        await handler(message())
            if self.ends_on_its_own and sent:
                return
            self._changed.clear()
            await asyncio.to_thread(self._changed.wait, 0.05)


@pytest.fixture
def alpaca() -> SimpleNamespace:
    """The payload builders and stand-ins, for tests to construct as they need."""
    return SimpleNamespace(
        ASSET_AAPL=ASSET_AAPL,
        BAR_AAPL=BAR_AAPL,
        asset=asset,
        quote=quote,
        trade=trade,
        TradingClient=FakeTradingClient,
        HistoricalClient=FakeHistoricalClient,
        Stream=FakeStream,
    )
