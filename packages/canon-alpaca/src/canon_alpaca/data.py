"""Market data client: historical bars, and live quotes and trades, for US equities on Alpaca.

Contract: `nautilus_trader.live.clients.MarketDataClient`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Data client hooks" and "Startup, scheduling, and shutdown"). Hooks are async and receive frozen commands.
Subscription hooks read `command.instrument_ids`, skip and log members they cannot serve, and keep
subscription state locally; unsubscribe hooks tolerate members that never subscribed. Historical
responses preserve the request ID, params and bounds, and a failed fetch raises. Streaming data goes out
through `_handle_data`, instruments through `_handle_instrument` before their data. Nothing is started in
the constructor; resources are made in `_connect` and adapter-owned work is joined in `_disconnect`. Hooks
not overridden here raise `NotImplementedError` from the base class.

Venue side, alpaca-py 0.45.0:

* `StockHistoricalDataClient.get_stock_bars(StockBarsRequest)` serves `_request_bars`. alpaca-py pages
  through the response until `limit` bars have arrived (`RESTClient._get_marketdata`).
* `StockDataStream` serves `_subscribe_quotes` and `_subscribe_trades`. Its `run()` drives its own asyncio
  loop, and once running its `subscribe_*` and `unsubscribe_*` wait on that loop, so the stream runs on a
  thread this client owns. The thread starts on the first subscription and is stopped (`stop()`, safe from
  any thread) and joined in `_disconnect`. The stream's handlers pass each message to the node's loop with
  `loop.call_soon_threadsafe`; conversion and output happen on the node's loop only, as the guide requires
  for cache and output access. Each handler is a coroutine function, which alpaca-py's `_subscribe`
  requires, and holds the client only weakly (`weak_coroutine`), as does the thread itself. Alpaca sends trade cancels and corrections with the trades channel; alpaca-py passes them to
  the handlers set with `register_trade_cancels` and `register_trade_corrections`, and this client logs
  them as warnings.
* alpaca-py's REST client is synchronous, so each call runs in a worker thread (`asyncio.to_thread`).

Timestamps: Alpaca stamps a bar at its open (alpaca-py `Bar.timestamp`, "The opening timestamp of the
bar"), so `ts_event` is the open and `ts_init = ts_event + interval`, the rule NautilusTrader gives for bars
timestamped at the open (concepts/backtesting/bar-execution, "Bar timestamp convention"). alpaca-py parses
timestamps into `datetime`, so sub-microsecond precision is lost.

Bar windows: a bar's `ts_init` is its open plus the bar's fixed length (`get_interval_ns`: 24 hours for a
day, 168 for a week), not the end of trading. Alpaca opens daily and weekly bars at midnight New York time
(UNVERIFIED: not checked against the live API), so a daily bar's `ts_init` is 24 hours after that, hours
after the 16:00 close. Across a clock change the fixed length does not land on midnight: a bar spanning the
autumn change ends at 23:00 New York, one spanning the spring change at 01:00 the next day. US clock
changes fall early on Sundays, so trading-day daily bars never span one, but weekly bars do. Alpaca's bars
endpoint takes an inclusive `start` and `end` compared with the open
(https://docs.alpaca.markets/us/reference/stockbars.md, read 2026-11-10; UNVERIFIED against the live API),
and NautilusTrader keeps the bars of a response whose `ts_init` lies in the request's `[start, end]` and
applies no `limit` of its own. `alpaca_bars_window` sends both bounds one interval earlier, so a request
returns exactly the bars whose `ts_init` lies in its window. It ends the window at the earlier of the
requested end and the node clock's now, so a bar still forming, whose `ts_init` is in the future, is never
returned; and with no `start` it starts the window at midnight New York of the current day. To get the
bar for day or week D, ask for a window holding its open plus its length with hours to spare, for example
from noon New York on the bar's last day to noon the next day; a window ending exactly at midnight misses
a weekly bar that spans the spring change, and a window inside D's trading hours returns no daily bar. The
response carries the bounds as requested. `limit` returns the earliest bars of the window, since Alpaca sorts ascending by
default; alpaca-py's `sort` parameter (`Sort.DESC`) would return the latest and is not used here.

A bar request runs one alpaca-py call at a time on the one HTTP session. The lock is released when the
worker thread's call returns, not when the awaiting hook ends, so a request cancelled mid-call (as on
shutdown) still keeps the next one waiting until the call it started has finished.

Known limits:

* Quote sizes keep Alpaca's unit, round lots ("Real-time Stock Data" quote schema), not shares. Alpaca
  gives no lot size to convert with.
* Alpaca trades carry no aggressor side, so trade ticks use `NO_AGGRESSOR`.
* Bars: `LAST` price, `EXTERNAL` source, and MINUTE, HOUR, DAY or WEEK aggregation, at the steps both
  NautilusTrader's `BarType` and alpaca-py's `TimeFrame` accept (DAY and WEEK only at 1). MONTH is
  refused: it has no fixed interval to derive `ts_init` from.
* A request's `limit` is passed to Alpaca as it is; Alpaca returns bars in ascending time from `start`.
* alpaca-py sets no HTTP timeout and has no setting for one (`RESTClient._one_request` calls
  `requests.Session.request` without `timeout`). The historical client gets one, 3.05 s to connect and
  27 s between bytes, from a transport adapter on alpaca-py's private `_session`
  (`providers.with_http_timeout`, ADR 0039); a stalled request fails with a timeout. A request whose first
  byte takes longer than 27 s fails too, and alpaca-py does not repeat it.
* A trade tick already emitted cannot be withdrawn or corrected: a cancel or correction is only logged.
* Prices must be representable at the instrument's precision; a finer price is refused, not rounded.
* The feed is fixed to IEX (`constants.FEED`).
* Sizes must be whole shares (`Equity` is whole-share only); a fractional size is refused, not rounded.
* alpaca-py's REST client has no `close()`; `_disconnect` drops the reference.
* If alpaca-py's stream ends on its own (for example after an insufficient-subscription error), the client
  logs an error and does not restart it.
"""

from __future__ import annotations

import asyncio
import math
import threading
import weakref
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Protocol
from typing import override
from zoneinfo import ZoneInfo

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import Trade
from alpaca.data.models import TradeCancel
from alpaca.data.models import TradeCorrection
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.timeframe import TimeFrameUnit
from nautilus_trader.common import Logger
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import BarsResponse
from nautilus_trader.live.clients import MarketDataClient
from nautilus_trader.model import AggregationSource
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarAggregation
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import PriceType
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import Symbol
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick

from .constants import FEED
from .constants import VENUE
from .providers import AlpacaInstrumentProvider
from .providers import credentials_from_env
from .providers import with_http_timeout

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterable

    from alpaca.common.types import RawData
    from alpaca.data.models import Bar as AlpacaBar
    from nautilus_trader import live
    from nautilus_trader.common import Clock
    from nautilus_trader.config import DataClientConfig
    from nautilus_trader.live import ClientCache

STREAM_JOIN_TIMEOUT_SECS = 10.0
"""How long `_disconnect` waits for the stream thread to return from `run()`.

While connected, alpaca-py's `DataStream._consume` sees a stop after its current receive, which it bounds
at 5 seconds, and then closes the websocket. `websockets` 17.4 waits up to its default `close_timeout`,
10 seconds, for the close (alpaca-py does not set it), so a connected stream can take about 15 seconds:
this timeout covers the usual case, not the worst one. A stop that arrives while alpaca-py is connecting
or authenticating is seen only once that finishes: the handshake is bounded by `websockets`' default
`open_timeout` (10 seconds), the welcome and auth replies only by its keepalive (alpaca-py sets
`ping_interval` 10 and `ping_timeout` 180 seconds). If the thread is still alive after this timeout,
`_disconnect` clears the client's state and raises; the daemon thread is left to finish on its own."""

NEW_YORK = ZoneInfo("America/New_York")
"""The US equity market's time zone, which sets the day a bar request's default start falls in."""

TIMEFRAME_UNITS = {
    BarAggregation.MINUTE: TimeFrameUnit.Minute,
    BarAggregation.HOUR: TimeFrameUnit.Hour,
    BarAggregation.DAY: TimeFrameUnit.Day,
    BarAggregation.WEEK: TimeFrameUnit.Week,
}


class BarsClient(Protocol):
    """The part of alpaca-py's `StockHistoricalDataClient` this client uses."""

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet | RawData: ...


type QuoteHandler = Callable[[Quote | RawData], Awaitable[None]]
type TradeHandler = Callable[[Trade | RawData], Awaitable[None]]


type TradeCancelHandler = Callable[[TradeCancel | RawData], Awaitable[None]]
type TradeCorrectionHandler = Callable[[TradeCorrection | RawData], Awaitable[None]]


class QuoteTradeStream(Protocol):
    """The part of alpaca-py's `StockDataStream` this client uses."""

    def subscribe_quotes(self, handler: QuoteHandler, *symbols: str) -> None: ...

    def subscribe_trades(self, handler: TradeHandler, *symbols: str) -> None: ...

    def register_trade_cancels(self, handler: TradeCancelHandler) -> None: ...

    def register_trade_corrections(self, handler: TradeCorrectionHandler) -> None: ...

    def unsubscribe_quotes(self, *symbols: str) -> None: ...

    def unsubscribe_trades(self, *symbols: str) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


def weak_coroutine[T](method: Callable[[T], Awaitable[None]]) -> Callable[[T], Awaitable[None]]:
    """Return a coroutine function that calls `method` while its object is alive, holding it weakly.

    The stream and its thread can outlive the client when `_disconnect` times out. NautilusTrader's
    `Logger` and `Clock` are native objects that must not be dropped on another thread, so nothing the
    stream thread holds may keep the client alive.
    """
    ref = weakref.WeakMethod(method)

    async def call(item: T) -> None:
        bound = ref()
        if bound is not None:
            await bound(item)

    return call


def weak_callback[T](method: Callable[[T], None]) -> Callable[[T], None]:
    """Return a function that calls `method` while its object is alive, holding it weakly."""
    ref = weakref.WeakMethod(method)

    def call(item: T) -> None:
        bound = ref()
        if bound is not None:
            bound(item)

    return call


def post[T](loop: asyncio.AbstractEventLoop, callback: Callable[[T], None], item: T) -> None:
    """Hand `item` to `callback` on the node's loop, from any thread."""
    try:
        loop.call_soon_threadsafe(callback, item)
    except RuntimeError:
        # The node's loop is closed: the node has finished, and late stream messages have no reader.
        pass


def release_after(
    loop: asyncio.AbstractEventLoop,
    lock: asyncio.Lock,
    fn: Callable[[StockBarsRequest], BarSet | RawData],
    arg: StockBarsRequest,
) -> BarSet | RawData:
    """In a worker thread: return `fn(arg)`, then release `lock` on the node's loop, however `fn` ends."""
    try:
        return fn(arg)
    finally:
        try:
            loop.call_soon_threadsafe(lock.release)
        except RuntimeError:
            pass  # the loop is closed, and the lock with it


type StreamExit = tuple[QuoteTradeStream, BaseException | None]


def run_stream(
    stream: QuoteTradeStream,
    loop: asyncio.AbstractEventLoop,
    on_exit: Callable[[StreamExit], None],
) -> None:
    """Run alpaca-py's stream on the calling thread and report, naming the stream, when it returns."""
    try:
        stream.run()
    except BaseException as e:
        post(loop, on_exit, (stream, e))
        raise
    post(loop, on_exit, (stream, None))


def bar_timeframe(bar_type: BarType) -> TimeFrame:
    """Return the alpaca-py timeframe for a bar type, or raise if Alpaca cannot serve it."""
    spec = bar_type.spec
    if bar_type.aggregation_source != AggregationSource.EXTERNAL:
        raise ValueError(f"{bar_type}: Alpaca serves EXTERNAL bars only")
    if spec.price_type != PriceType.LAST:
        raise ValueError(f"{bar_type}: Alpaca serves trade (LAST) bars only")
    unit = TIMEFRAME_UNITS.get(spec.aggregation)
    if unit is None:
        raise ValueError(f"{bar_type}: Alpaca bars are MINUTE, HOUR, DAY or WEEK here")
    return TimeFrame(spec.step, unit)


def bar_interval_ns(bar_type: BarType) -> int:
    """Return the fixed interval of a time bar type in nanoseconds (`BarSpecification.get_interval_ns`)."""
    return bar_type.spec.get_interval_ns()


def is_power_of_ten(n: int) -> bool:
    """Whether `n` is 10, 100, 1000, ..."""
    while n >= 10 and n % 10 == 0:
        n //= 10
    return n == 1


def start_of_day(now: datetime) -> datetime:
    """Midnight New York time on the day `now` falls in, New York time."""
    return datetime.combine(now.astimezone(NEW_YORK).date(), time(0), NEW_YORK)


def alpaca_bars_window(
    start: datetime | None, end: datetime | None, now: datetime, interval: timedelta
) -> tuple[datetime, datetime]:
    """Return the `start` and `end` to send Alpaca for bars whose `ts_init` lies in `[start, end]`.

    Alpaca filters on the open, inclusive at both ends, and `ts_init` is the open plus `interval`, so both
    bounds move back one interval. The end is the earlier of the requested end and `now`, so no bar whose
    `ts_init` is in the future (one still forming) is returned. With no start, the start is the beginning
    of the current day, Alpaca's own documented default ("Default: the beginning of the current day",
    https://docs.alpaca.markets/us/reference/stockbars.md), taken as midnight New York because the
    reference names no time zone; supplying it here keeps the window sent from ending before it starts.
    """
    end = now if end is None else min(end, now)
    start = start_of_day(now) if start is None else start
    return start - interval, end - interval


def exact_price(instrument: Equity, value: float, field: str) -> Price:
    """Return a price at the instrument's precision, refusing a value that would need rounding.

    `Instrument.make_price` rounds to the instrument's precision without saying so, so this checks first.
    `round(value, precision) == value` holds exactly when the float's shortest decimal form has at most
    `precision` decimals.
    """
    precision = instrument.price_precision
    if not math.isfinite(value) or round(value, precision) != value:
        raise ValueError(
            f"{instrument.id} {field} {value} is not representable at price precision {precision}",
        )
    return instrument.make_price(value)


def whole_shares(instrument: Equity, value: float, field: str) -> Quantity:
    """Return a whole-share quantity, refusing a fractional value instead of rounding it."""
    if not float(value).is_integer():
        raise ValueError(
            f"{instrument.id} {field} {value} is fractional; Equity is whole-share only"
        )
    return instrument.make_qty(value)


def parse_bar(bar: AlpacaBar, bar_type: BarType, instrument: Equity) -> Bar:
    """Convert one alpaca-py bar, timestamped at its open, into a NautilusTrader bar."""
    ts_event = dt_to_unix_nanos(bar.timestamp)
    return Bar(
        bar_type=bar_type,
        open=exact_price(instrument, bar.open, "open"),
        high=exact_price(instrument, bar.high, "high"),
        low=exact_price(instrument, bar.low, "low"),
        close=exact_price(instrument, bar.close, "close"),
        volume=whole_shares(instrument, bar.volume, "volume"),
        ts_event=ts_event,
        ts_init=ts_event + bar_interval_ns(bar_type),
    )


class OneSidedQuoteError(ValueError):
    """A quote with no price on one side, which a `QuoteTick` cannot hold."""


def parse_quote(quote: Quote, instrument: Equity, ts_init: int) -> QuoteTick:
    """Convert one alpaca-py quote into a NautilusTrader quote. Sizes stay in round lots.

    A side priced at zero or less is refused: a `QuoteTick` holds both sides, and a one-sided quote would
    otherwise carry a 0.0000 price for the missing side.
    """
    for side, price in (("bid", quote.bid_price), ("ask", quote.ask_price)):
        if not price > 0:
            raise OneSidedQuoteError(
                f"{instrument.id} quote at {quote.timestamp} has {side} price {price}"
            )
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=exact_price(instrument, quote.bid_price, "bid price"),
        ask_price=exact_price(instrument, quote.ask_price, "ask price"),
        bid_size=whole_shares(instrument, quote.bid_size, "bid size"),
        ask_size=whole_shares(instrument, quote.ask_size, "ask size"),
        ts_event=dt_to_unix_nanos(quote.timestamp),
        ts_init=ts_init,
    )


def parse_trade(trade: Trade, instrument: Equity, ts_init: int) -> TradeTick:
    """Convert one alpaca-py trade into a NautilusTrader trade."""
    if trade.id is None:
        raise ValueError(f"{instrument.id} trade at {trade.timestamp} has no trade ID")
    return TradeTick(
        instrument_id=instrument.id,
        price=exact_price(instrument, trade.price, "trade price"),
        size=whole_shares(instrument, trade.size, "trade size"),
        aggressor_side=AggressorSide.NO_AGGRESSOR,
        trade_id=TradeId(str(trade.id)),
        ts_event=dt_to_unix_nanos(trade.timestamp),
        ts_init=ts_init,
    )


class AlpacaDataClient(MarketDataClient):
    """Serve Alpaca US equity bars on request, and quotes and trades by subscription."""

    def __init__(
        self,
        *,
        name: str,
        config: DataClientConfig,
        cache: ClientCache,
        clock: Clock,
        instrument_provider: AlpacaInstrumentProvider,
        historical_client: BarsClient | None = None,
        stream: QuoteTradeStream | None = None,
    ) -> None:
        """Retain configuration and local state. The alpaca-py clients are built in `_connect`.

        The venue is always `constants.VENUE`: instruments are matched against it.
        """
        super().__init__(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            venue=VENUE,
            instrument_provider=instrument_provider,
        )
        self._provider = instrument_provider
        self._historical = historical_client
        # Historical requests run concurrently, but alpaca-py's REST client holds one requests.Session,
        # which is not documented as thread-safe, so calls on it are made one at a time. The lock is held
        # on the node's loop, so a waiting request occupies no worker thread. Rebuilt in `_connect`.
        self._historical_lock = asyncio.Lock()
        self._stream = stream
        self._stream_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._quote_symbols: set[str] = set()
        self._trade_symbols: set[str] = set()
        self._one_sided_quotes: dict[str, int] = {}  # dropped per symbol since connect
        self._log = Logger(type(self).__name__)

    # -- lifecycle ---------------------------------------------------------------------------------

    @override
    async def _connect(self) -> None:
        # NautilusTrader v2.0.0rc8 logs a failed connect as "connection failed" without the exception's
        # message (observed), so the reason is logged here before it propagates.
        try:
            await self._connect_venue()
        except Exception as e:
            self._log.error(f"Alpaca data client failed to connect: {e}")
            raise

    async def _connect_venue(self) -> None:
        self._owner_loop = self.loop
        self._stopping = False
        self._historical_lock = asyncio.Lock()
        if self._historical is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._historical is None:
                self._historical = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
            if self._stream is None:
                self._stream = StockDataStream(api_key=api_key, secret_key=secret_key, feed=FEED)
        with_http_timeout(
            self._historical
        )  # a client passed in too; refused without a session (ADR 0039)
        self._stream.register_trade_cancels(weak_coroutine(self._on_trade_cancel))
        self._stream.register_trade_corrections(weak_coroutine(self._on_trade_correction))
        await self._provider.initialize()
        for instrument in self._provider.list_all():
            self._handle_instrument(instrument)

    @override
    async def _disconnect(self) -> None:
        self._stopping = True
        thread = self._stream_thread
        if thread is not None and self._stream is not None:
            self._stream.stop()
            await asyncio.to_thread(thread.join, STREAM_JOIN_TIMEOUT_SECS)
        # Cleared whether or not the thread stopped, so a later connect builds a fresh stream.
        self._stream_thread = None
        self._stream = None
        self._historical = None
        self._quote_symbols.clear()
        self._trade_symbols.clear()
        for symbol, count in sorted(self._one_sided_quotes.items()):
            self._log.warning(
                f"Dropped {count} one-sided Alpaca quotes for {symbol} since subscribing"
            )
        self._one_sided_quotes.clear()
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                f"Alpaca stream thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
            )

    # -- subscriptions -----------------------------------------------------------------------------

    @override
    async def _subscribe_quotes(self, command: live.SubscribeQuotes) -> None:
        new = [s for s in self._serveable(command.instrument_ids) if s not in self._quote_symbols]
        if not new:
            return
        stream = self._started_stream()
        # Tracked before the call and kept if it fails: alpaca-py records the handler before it sends,
        # so it re-sends the subscription on reconnect, and unsubscribe must still be able to remove it.
        self._quote_symbols.update(new)
        await asyncio.to_thread(stream.subscribe_quotes, weak_coroutine(self._on_quote), *new)

    @override
    async def _unsubscribe_quotes(self, command: live.UnsubscribeQuotes) -> None:
        symbols = self._subscribed(command.instrument_ids, self._quote_symbols)
        if symbols and self._stream is not None:
            # alpaca-py sends the unsubscribe first and drops its handlers only if the send succeeds, so
            # the symbols are dropped here only after it returns: a failed unsubscribe can be retried.
            await asyncio.to_thread(self._stream.unsubscribe_quotes, *symbols)
            self._quote_symbols.difference_update(symbols)
            for symbol in symbols:
                self._one_sided_quotes.pop(symbol, None)  # counted afresh after a new subscription

    @override
    async def _subscribe_trades(self, command: live.SubscribeTrades) -> None:
        new = [s for s in self._serveable(command.instrument_ids) if s not in self._trade_symbols]
        if not new:
            return
        stream = self._started_stream()
        self._trade_symbols.update(new)  # kept if the call fails; see _subscribe_quotes
        await asyncio.to_thread(stream.subscribe_trades, weak_coroutine(self._on_trade), *new)

    @override
    async def _unsubscribe_trades(self, command: live.UnsubscribeTrades) -> None:
        symbols = self._subscribed(command.instrument_ids, self._trade_symbols)
        if symbols and self._stream is not None:
            await asyncio.to_thread(
                self._stream.unsubscribe_trades, *symbols
            )  # see _unsubscribe_quotes
            self._trade_symbols.difference_update(symbols)

    # -- historical requests -----------------------------------------------------------------------

    @override
    async def _request_bars(self, request: live.RequestBars) -> None:
        bar_types = request.bar_types
        if len(bar_types) != 1:
            raise ValueError(f"Alpaca bar requests take one bar type, got {len(bar_types)}")
        bar_type = bar_types[0]
        instrument = self._equity(bar_type.instrument_id)
        historical = self._historical
        if historical is None:
            raise RuntimeError("Alpaca data client is not connected")
        symbol = instrument.id.symbol.value
        interval = timedelta(microseconds=bar_interval_ns(bar_type) // 1_000)
        start, end = alpaca_bars_window(request.start, request.end, self.clock.utc_now(), interval)
        params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=bar_timeframe(bar_type),
            start=start,
            end=end,
            limit=request.limit,
            feed=FEED,
        )

        result = await self._one_historical_call(historical.get_stock_bars, params)
        if not isinstance(result, BarSet):
            raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
        bars = [parse_bar(bar, bar_type, instrument) for bar in result.data.get(symbol, [])]
        self._handle_response(
            BarsResponse(
                client_id=self.client_id,
                bar_type=bar_type,
                data=bars,
                correlation_id=request.request_id,
                ts_init=self.clock.timestamp_ns(),
                start=request.start_ns,
                end=request.end_ns,
                params=request.params,
            ),
        )

    async def _one_historical_call(
        self, fn: Callable[[StockBarsRequest], BarSet | RawData], arg: StockBarsRequest
    ) -> BarSet | RawData:
        """Run `fn(arg)` in a worker thread, one call at a time, releasing the lock when the call returns.

        The worker's future is shielded, so cancelling the awaiting hook leaves the call running and the
        lock held until it ends; the worker hands the release back to the node's loop. If the loop stops
        before a queued call starts, the lock stays held, and `_connect` builds a new one.
        """
        loop = asyncio.get_running_loop()
        lock = self._historical_lock
        await lock.acquire()
        try:
            call = asyncio.ensure_future(asyncio.to_thread(release_after, loop, lock, fn, arg))
        except BaseException:
            lock.release()
            raise
        return await asyncio.shield(call)

    # -- helpers on the node's loop ----------------------------------------------------------------

    def _equity(self, instrument_id: InstrumentId) -> Equity:
        instrument = self._provider.find(instrument_id) if instrument_id.venue == VENUE else None
        if instrument is None:
            raise ValueError(f"{instrument_id} is not a loaded {VENUE} instrument")
        if not isinstance(instrument, Equity):
            raise TypeError(f"{instrument_id} is not an Equity")
        return instrument

    def _serveable(self, instrument_ids: Iterable[InstrumentId]) -> list[str]:
        symbols = []
        for instrument_id in instrument_ids:
            try:
                symbols.append(self._equity(instrument_id).id.symbol.value)
            except (ValueError, TypeError) as e:
                self._log.error(f"Cannot subscribe: {e}")
        return symbols

    @staticmethod
    def _subscribed(instrument_ids: Iterable[InstrumentId], current: set[str]) -> list[str]:
        return [
            i.symbol.value for i in instrument_ids if i.venue == VENUE and i.symbol.value in current
        ]

    def _started_stream(self) -> QuoteTradeStream:
        if self._stream is None or self._owner_loop is None:
            raise RuntimeError("Alpaca data client is not connected")
        if self._stream_thread is None:
            self._stream_thread = threading.Thread(
                target=run_stream,
                args=(self._stream, self._owner_loop, weak_callback(self._on_stream_exit)),
                name=f"{self.client_id}-stream",
                daemon=True,
            )
            self._stream_thread.start()
        return self._stream

    def _emit_quote(self, quote: Quote) -> None:
        if self._stopping or quote.symbol not in self._quote_symbols:
            return
        try:
            instrument = self._equity(InstrumentId(Symbol(quote.symbol), VENUE))
            tick = parse_quote(quote, instrument, self.clock.timestamp_ns())
        except OneSidedQuoteError as e:
            # Common outside market hours and on thin names, so the first per symbol is logged with its
            # reason, then the running count each time it reaches a power of ten: a bounded number of
            # lines however long the node runs, with no timer.
            count = self._one_sided_quotes.get(quote.symbol, 0) + 1
            self._one_sided_quotes[quote.symbol] = count
            if count == 1:
                self._log.warning(
                    f"Dropped one-sided Alpaca quote: {e}; later ones for this symbol are counted",
                )
            elif is_power_of_ten(count):
                self._log.warning(
                    f"Dropped {count} one-sided Alpaca quotes for {quote.symbol} since subscribing",
                )
            return
        except (ValueError, TypeError) as e:
            self._log.error(f"Dropped Alpaca quote: {e}")
            return
        self._handle_data(tick)

    def _emit_trade(self, trade: Trade) -> None:
        if self._stopping or trade.symbol not in self._trade_symbols:
            return
        try:
            instrument = self._equity(InstrumentId(Symbol(trade.symbol), VENUE))
            tick = parse_trade(trade, instrument, self.clock.timestamp_ns())
        except (ValueError, TypeError) as e:
            self._log.error(f"Dropped Alpaca trade: {e}")
            return
        self._handle_data(tick)

    def _warn_trade_cancel(self, cancel: TradeCancel) -> None:
        if self._stopping or cancel.symbol not in self._trade_symbols:
            return
        action = "error" if cancel.action == "E" else "cancel"
        self._log.warning(
            f"Alpaca trade {action} for {cancel.symbol} trade {cancel.id} at {cancel.timestamp}; "
            "a trade tick already emitted is not withdrawn",
        )

    def _warn_trade_correction(self, correction: TradeCorrection) -> None:
        if self._stopping or correction.symbol not in self._trade_symbols:
            return
        self._log.warning(
            f"Alpaca trade correction for {correction.symbol} trade {correction.original_id} "
            f"(now trade {correction.corrected_id}, price {correction.corrected_price}, "
            f"size {correction.corrected_size}) at {correction.timestamp}; "
            "a trade tick already emitted is not corrected",
        )

    def _on_stream_exit(self, exit: StreamExit) -> None:
        stream, error = exit
        # Only the stream this client still runs counts: a thread left behind by a timed-out disconnect
        # can end after a reconnect, when `_stopping` is False again.
        if stream is self._stream and not self._stopping:
            self._log.error(f"Alpaca stream ended while subscribed: {error!r}")

    # -- the stream's thread: reached through `weak_coroutine`, never holding the client -------------

    async def _on_quote(self, quote: Quote | RawData) -> None:
        if isinstance(quote, Quote) and self._owner_loop is not None:
            post(self._owner_loop, self._emit_quote, quote)

    async def _on_trade(self, trade: Trade | RawData) -> None:
        if isinstance(trade, Trade) and self._owner_loop is not None:
            post(self._owner_loop, self._emit_trade, trade)

    async def _on_trade_cancel(self, cancel: TradeCancel | RawData) -> None:
        if isinstance(cancel, TradeCancel) and self._owner_loop is not None:
            post(self._owner_loop, self._warn_trade_cancel, cancel)

    async def _on_trade_correction(self, correction: TradeCorrection | RawData) -> None:
        if isinstance(correction, TradeCorrection) and self._owner_loop is not None:
            post(self._owner_loop, self._warn_trade_correction, correction)
