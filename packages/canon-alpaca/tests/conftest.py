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
* trade cancel and trade correction: the same page, schema examples, read 2026-11-10. The symbol is
  changed to AAPL and the timestamp written as `to_datetime()` gives it.

The stand-ins implement only the methods the adapter calls, with alpaca-py 0.45.0's signatures, and are as
strict as alpaca-py where the adapter depends on it: the bars stand-in filters the way Alpaca's bars
endpoint documents, and the stream stand-in checks handlers and unsubscribes the way
`alpaca/data/live/websocket.py` does. No test here opens a network connection or reads a credential.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import threading
import traceback
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import Trade
from alpaca.data.models import TradeCancel
from alpaca.data.models import TradeCorrection
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.models import Asset

type Handler = Callable[[object], Awaitable[None]]
type Message = Quote | Trade | TradeCancel | TradeCorrection

NEW_YORK = ZoneInfo("America/New_York")

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


TRADE_CANCEL_AAPL = {
    "T": "x",
    "S": "AAPL",  # the docs' example is GOOGL
    "i": 465,
    "x": "D",
    "p": 105.31,
    "s": 300,
    "a": "C",
    "z": "C",
    "t": datetime(2023, 4, 6, 13, 15, 42, 835_409, tzinfo=UTC),  # "2023-04-06T13:15:42.83540958Z"
}

TRADE_CORRECTION_AAPL = {
    "T": "c",
    "S": "AAPL",  # the docs' example is EEM
    "x": "M",
    "oi": 52983525033527,
    "op": 39.1582,
    "os": 440000,
    "oc": [" ", "7"],
    "ci": 52983525034326,
    "cp": 39.1809,
    "cs": 440000,
    "cc": [" ", "7"],
    "z": "B",
    "t": datetime(2023, 4, 6, 14, 25, 6, 542_305, tzinfo=UTC),  # "2023-04-06T14:25:06.542305024Z"
}


def asset(**changes: object) -> Asset:
    return Asset.model_validate({**ASSET_AAPL, **changes})


def quote(**changes: object) -> Quote:
    return Quote(QUOTE_AAPL["S"], {**QUOTE_AAPL, **changes})


def trade(**changes: object) -> Trade:
    return Trade(TRADE_AAPL["S"], {**TRADE_AAPL, **changes})


def trade_cancel() -> TradeCancel:
    return TradeCancel("AAPL", dict(TRADE_CANCEL_AAPL))


def trade_correction() -> TradeCorrection:
    return TradeCorrection("AAPL", dict(TRADE_CORRECTION_AAPL))


def bars_opening(*opens: datetime) -> list[dict]:
    """Copies of the docs' bar, one opening at each UTC time given."""
    return [{**BAR_AAPL, "t": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")} for t in opens]


def hourly_bars(first_open: datetime, count: int) -> list[dict]:
    """`count` copies of the docs' bar, opening one hour apart from `first_open`."""
    return bars_opening(*(first_open + timedelta(hours=i) for i in range(count)))


class FakeTradingClient:
    """Stands in for `TradingClient`'s two asset reads."""

    def __init__(self, assets: list[dict] | None = None) -> None:
        self.assets = assets if assets is not None else [ASSET_AAPL]
        self.errors: dict[str, Exception] = {}  # raised by get_asset for that symbol
        self.all_requests: list[object] = []
        self.symbol_requests: list[object] = []

    def get_all_assets(self, filter: object = None) -> list[Asset]:
        self.all_requests.append(filter)
        return [Asset.model_validate(a) for a in self.assets]

    def get_asset(self, symbol_or_asset_id: object) -> Asset:
        self.symbol_requests.append(symbol_or_asset_id)
        if isinstance(symbol_or_asset_id, str) and symbol_or_asset_id in self.errors:
            raise self.errors[symbol_or_asset_id]
        for a in self.assets:
            if a["symbol"] == symbol_or_asset_id:
                return Asset.model_validate(a)
        raise APIError('{"code": 40410000, "message": "asset not found"}')


class FakeHistoricalClient:
    """Stands in for `StockHistoricalDataClient.get_stock_bars`.

    It answers the way Alpaca's bars endpoint documents (https://docs.alpaca.markets/us/reference/stockbars.md):
    the bars whose timestamp, the open, lies in the inclusive `[start, end]`, in ascending time, at most
    `limit` of them. alpaca-py has already made `start` and `end` naive UTC. With `gate` set, each call
    sets `entered` and then waits for `gate`.
    """

    def __init__(self, bars: list[dict] | None = None, error: Exception | None = None) -> None:
        self.bars = bars if bars is not None else [BAR_AAPL]
        self.error = error
        self.requests: list[StockBarsRequest] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.max_active = 0  # the most calls that were inside `get_stock_bars` at once
        self._active = 0
        self._count_lock = threading.Lock()

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet:
        with self._count_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            return self._answer(request_params)
        finally:
            with self._count_lock:
                self._active -= 1

    def _answer(self, request_params: StockBarsRequest) -> BarSet:
        self.requests.append(request_params)
        if self.gate is not None:
            self.entered.set()
            assert self.gate.wait(10), "gate never opened"
        if self.error is not None:
            raise self.error
        symbol = request_params.symbol_or_symbols
        assert isinstance(symbol, str)
        start, end, limit = request_params.start, request_params.end, request_params.limit
        assert start is None or start.tzinfo is None
        assert end is None or end.tzinfo is None
        # Alpaca's documented defaults: "the beginning of the current day" and "the current time" (for
        # a feed with real-time access). The reference names no time zone; midnight New York is assumed.
        now = datetime.now(UTC)
        if start is None:
            day = now.astimezone(NEW_YORK).date()
            start = datetime.combine(day, time(0), NEW_YORK).astimezone(UTC).replace(tzinfo=None)
        if end is None:
            end = now.replace(tzinfo=None)
        if start > end:
            # The reference does not say what Alpaca answers; refusing makes an inverted window visible.
            raise APIError('{"code": 42210000, "message": "start is after end (stand-in)"}')
        selected = []
        for raw in sorted(self.bars, key=lambda b: b["t"]):
            opened = datetime.fromisoformat(raw["t"]).replace(tzinfo=None)
            if start <= opened <= end:
                selected.append(raw)
        return BarSet({symbol: selected[:limit] if limit else selected})


class FakeStream:
    """Stands in for `StockDataStream`: `run()` blocks on its own asyncio loop until `stop()`.

    While running, it sends each message in `quotes` to the handler subscribed for that message's symbol,
    and each in `trades` (then, after them, those in `trade_events`, cancels and corrections) to the trade
    handler, once per subscription, the way alpaca-py's stream awaits a coroutine handler per message.
    Cancels and corrections go to the handlers set with `register_trade_cancels` and
    `register_trade_corrections`, which alpaca-py keys under "*". With `ends_on_its_own`, `run()` returns
    after its first delivery without a `stop()`, as alpaca-py's does after an insufficient-subscription
    error. With `ignores_stop`, `run()` keeps going after `stop()` until `release` is set.

    As in alpaca-py 0.45.0 (`DataStream._subscribe`, `_unsubscribe`, `_ensure_coroutine`): a handler that is
    not a coroutine function raises `ValueError`; a subscribe records its handlers and then sends; an
    unsubscribe sends first and deletes its handlers only if the send succeeded, and deleting a symbol never
    subscribed raises `KeyError`. Sends are made only while running. With `send_error` set, every send
    raises it.
    """

    def __init__(self) -> None:
        self.ends_on_its_own = False
        self.ignores_stop = False
        self.release = threading.Event()
        self.send_error: Exception | None = None
        self.quotes: list[Callable[[], Message]] = [quote]
        self.trades: list[Callable[[], Message]] = [trade]
        self.trade_events: list[Callable[[], Message]] = []
        self.quote_handlers: dict[str, Handler] = {}
        self.trade_handlers: dict[str, Handler] = {}
        self.cancel_handlers: dict[str, Handler] = {}
        self.correction_handlers: dict[str, Handler] = {}
        self.unsubscribed: list[tuple[str, tuple[str, ...]]] = []
        self.run_threads: list[str] = []
        self.running = threading.Event()
        self.finished = threading.Event()
        self.stopped = threading.Event()
        self._changed = threading.Event()

    def _send(self) -> None:
        if self.running.is_set() and self.send_error is not None:
            raise self.send_error

    def _subscribe(self, handler: Handler, symbols: tuple[str, ...], handlers: dict) -> None:
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("handler must be a coroutine function")
        handlers.update(dict.fromkeys(symbols, handler))
        self._changed.set()
        self._send()

    def _unsubscribe(self, channel: str, symbols: tuple[str, ...], handlers: dict) -> None:
        self._send()
        self.unsubscribed.append((channel, symbols))
        for s in symbols:
            del handlers[s]

    def subscribe_quotes(self, handler: Handler, *symbols: str) -> None:
        self._subscribe(handler, symbols, self.quote_handlers)

    def subscribe_trades(self, handler: Handler, *symbols: str) -> None:
        self._subscribe(handler, symbols, self.trade_handlers)

    def register_trade_cancels(self, handler: Handler) -> None:
        self.cancel_handlers = {"*": handler}

    def register_trade_corrections(self, handler: Handler) -> None:
        self.correction_handlers = {"*": handler}

    def unsubscribe_quotes(self, *symbols: str) -> None:
        self._unsubscribe("quotes", symbols, self.quote_handlers)

    def unsubscribe_trades(self, *symbols: str) -> None:
        self._unsubscribe("trades", symbols, self.trade_handlers)

    def run(self) -> None:
        self.run_threads.append(threading.current_thread().name)
        self.running.set()
        try:
            asyncio.run(self._serve())
        finally:
            self.running.clear()
            self.finished.set()

    def stop(self) -> None:
        self.stopped.set()
        self._changed.set()

    def _handler_for(self, message: Message) -> Handler | None:
        channel = {
            TradeCancel: self.cancel_handlers,
            TradeCorrection: self.correction_handlers,
            Trade: self.trade_handlers,
            Quote: self.quote_handlers,
        }[type(message)]
        symbol = message.symbol
        return channel.get(symbol, channel.get("*"))

    async def _serve(self) -> None:
        sent: set[tuple[str, str]] = set()
        while not self.stopped.is_set() or (self.ignores_stop and not self.release.is_set()):
            for channel, handlers, builders in (
                ("quotes", self.quote_handlers, self.quotes),
                ("trades", self.trade_handlers, self.trades + self.trade_events),
            ):
                for symbol in list(handlers):
                    if (channel, symbol) in sent:
                        continue
                    sent.add((channel, symbol))
                    for build in builders:
                        message = build()
                        handler = self._handler_for(message)
                        if message.symbol == symbol and handler is not None:
                            await handler(message)
            if self.ends_on_its_own and sent:
                return
            self._changed.clear()
            await asyncio.to_thread(self._changed.wait, 0.05)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Once a failure's report is written, drop the local variables its traceback keeps.

    NautilusTrader allows one `LiveNode` per thread, and a failed test's traceback would keep its node
    alive, so every later test would fail to build one.
    """
    report = yield
    if call.excinfo is not None:
        traceback.clear_frames(call.excinfo.tb)
    return report


@pytest.fixture(autouse=True)
def collect_on_main_thread():
    """Collect each test's garbage on the main thread before the next test starts threads.

    A client and its NautilusTrader `ClientRuntime` refer to each other, so they are freed by Python's
    cyclic collector, which runs on whichever thread happens to allocate. Their native `Logger` and
    `Clock` raise if dropped off the thread that made them, so a later test's worker or stream thread
    must not be the one that collects them. What that raise produces is an "unraisable exception"
    warning, log noise rather than a crash; collecting here keeps it out of the test output.
    """
    yield
    gc.collect()


@pytest.fixture
def alpaca() -> SimpleNamespace:
    """The payload builders and stand-ins, for tests to construct as they need."""
    return SimpleNamespace(
        ASSET_AAPL=ASSET_AAPL,
        BAR_AAPL=BAR_AAPL,
        asset=asset,
        quote=quote,
        trade=trade,
        trade_cancel=trade_cancel,
        trade_correction=trade_correction,
        hourly_bars=hourly_bars,
        bars_opening=bars_opening,
        TradingClient=FakeTradingClient,
        HistoricalClient=FakeHistoricalClient,
        Stream=FakeStream,
    )
