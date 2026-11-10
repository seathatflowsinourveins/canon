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

* `StockHistoricalDataClient.get_stock_bars(StockBarsRequest)` serves `_request_bars`.
* `StockDataStream` serves `_subscribe_quotes` and `_subscribe_trades`. Its `run()` drives its own asyncio
  loop, and once running its `subscribe_*` and `unsubscribe_*` wait on that loop, so the stream runs on a
  thread this client owns. The thread starts on the first subscription and is stopped (`stop()`, safe from
  any thread) and joined in `_disconnect`. The stream's handlers pass each message to the node's loop with
  `loop.call_soon_threadsafe`; conversion and output happen on the node's loop only, as the guide requires
  for cache and output access.
* alpaca-py's REST client is synchronous, so each call runs in a worker thread (`asyncio.to_thread`).

Timestamps: Alpaca stamps a bar at its open (alpaca-py `Bar.timestamp`, "The opening timestamp of the
bar"), so `ts_event` is the open and `ts_init = ts_event + interval`, the rule NautilusTrader gives for bars
timestamped at the open (concepts/backtesting/bar-execution, "Bar timestamp convention"). alpaca-py parses
timestamps into `datetime`, so sub-microsecond precision is lost.

Known limits:

* Quote sizes keep Alpaca's unit, round lots ("Real-time Stock Data" quote schema), not shares. Alpaca
  gives no lot size to convert with.
* Alpaca trades carry no aggressor side, so trade ticks use `NO_AGGRESSOR`.
* Bars: `LAST` price, `EXTERNAL` source, and MINUTE, HOUR, DAY or WEEK aggregation, at the steps both
  NautilusTrader's `BarType` and alpaca-py's `TimeFrame` accept (DAY and WEEK only at 1). MONTH is
  refused: it has no fixed interval to derive `ts_init` from.
* A request's `limit` is passed to Alpaca as it is; Alpaca returns bars in ascending time from `start`.
* The feed is fixed to IEX (`constants.FEED`).
* Sizes must be whole shares (`Equity` is whole-share only); a fractional size is refused, not rounded.
* alpaca-py's REST client has no `close()`; `_disconnect` drops the reference.
* If alpaca-py's stream ends on its own (for example after an insufficient-subscription error), the client
  logs an error and does not restart it.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Protocol
from typing import override

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import Trade
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
"""How long `_disconnect` waits for the stream thread. alpaca-py's `DataStream._consume` looks for a stop
request after each receive, which times out every 5 seconds; this allows that plus the websocket close."""

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


class QuoteTradeStream(Protocol):
    """The part of alpaca-py's `StockDataStream` this client uses."""

    def subscribe_quotes(self, handler: QuoteHandler, *symbols: str) -> None: ...

    def subscribe_trades(self, handler: TradeHandler, *symbols: str) -> None: ...

    def unsubscribe_quotes(self, *symbols: str) -> None: ...

    def unsubscribe_trades(self, *symbols: str) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


def locked_call[**P, R](
    lock: threading.Lock, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Call `fn` holding `lock`, for use in a worker thread.

    Kept at module level so the worker holds no reference to the client: NautilusTrader's `Logger` is a
    native object that must not be dropped on another thread, and a closure over the client could let a
    worker drop it.
    """
    with lock:
        return fn(*args, **kwargs)


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
    """Return the fixed interval of a time bar type in nanoseconds."""
    return (bar_type.spec.timedelta // timedelta(microseconds=1)) * 1_000


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
        open=instrument.make_price(bar.open),
        high=instrument.make_price(bar.high),
        low=instrument.make_price(bar.low),
        close=instrument.make_price(bar.close),
        volume=whole_shares(instrument, bar.volume, "volume"),
        ts_event=ts_event,
        ts_init=ts_event + bar_interval_ns(bar_type),
    )


def parse_quote(quote: Quote, instrument: Equity, ts_init: int) -> QuoteTick:
    """Convert one alpaca-py quote into a NautilusTrader quote. Sizes stay in round lots."""
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=instrument.make_price(quote.bid_price),
        ask_price=instrument.make_price(quote.ask_price),
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
        price=instrument.make_price(trade.price),
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
        # which is not documented as thread-safe, so calls on it are made one at a time.
        self._historical_lock = threading.Lock()
        self._stream = stream
        self._stream_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._quote_symbols: set[str] = set()
        self._trade_symbols: set[str] = set()
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
        if self._historical is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._historical is None:
                self._historical = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
            if self._stream is None:
                self._stream = StockDataStream(api_key=api_key, secret_key=secret_key, feed=FEED)
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
            if thread.is_alive():
                raise RuntimeError(
                    f"Alpaca stream thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
                )
        self._stream_thread = None
        self._stream = None
        self._historical = None
        self._quote_symbols.clear()
        self._trade_symbols.clear()

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
        await asyncio.to_thread(stream.subscribe_quotes, self._on_quote, *new)

    @override
    async def _unsubscribe_quotes(self, command: live.UnsubscribeQuotes) -> None:
        symbols = self._subscribed(command.instrument_ids, self._quote_symbols)
        if symbols and self._stream is not None:
            self._quote_symbols.difference_update(symbols)
            await asyncio.to_thread(self._stream.unsubscribe_quotes, *symbols)

    @override
    async def _subscribe_trades(self, command: live.SubscribeTrades) -> None:
        new = [s for s in self._serveable(command.instrument_ids) if s not in self._trade_symbols]
        if not new:
            return
        stream = self._started_stream()
        self._trade_symbols.update(new)  # kept if the call fails; see _subscribe_quotes
        await asyncio.to_thread(stream.subscribe_trades, self._on_trade, *new)

    @override
    async def _unsubscribe_trades(self, command: live.UnsubscribeTrades) -> None:
        symbols = self._subscribed(command.instrument_ids, self._trade_symbols)
        if symbols and self._stream is not None:
            self._trade_symbols.difference_update(symbols)
            await asyncio.to_thread(self._stream.unsubscribe_trades, *symbols)

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
        params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=bar_timeframe(bar_type),
            start=request.start,
            end=request.end,
            limit=request.limit,
            feed=FEED,
        )

        result = await asyncio.to_thread(
            locked_call,
            self._historical_lock,
            historical.get_stock_bars,
            params,
        )
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
                target=self._run_stream,
                args=(self._stream, self._owner_loop),
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

    def _on_stream_exit(self, error: BaseException | None) -> None:
        if not self._stopping:
            self._log.error(f"Alpaca stream ended while subscribed: {error!r}")

    # -- the stream's thread -----------------------------------------------------------------------

    def _run_stream(self, stream: QuoteTradeStream, loop: asyncio.AbstractEventLoop) -> None:
        try:
            stream.run()
        except BaseException as e:
            self._post(loop, self._on_stream_exit, e)
            raise
        self._post(loop, self._on_stream_exit, None)

    async def _on_quote(self, quote: Quote | RawData) -> None:
        if isinstance(quote, Quote) and self._owner_loop is not None:
            self._post(self._owner_loop, self._emit_quote, quote)

    async def _on_trade(self, trade: Trade | RawData) -> None:
        if isinstance(trade, Trade) and self._owner_loop is not None:
            self._post(self._owner_loop, self._emit_trade, trade)

    @staticmethod
    def _post[T](loop: asyncio.AbstractEventLoop, callback: Callable[[T], None], item: T) -> None:
        try:
            loop.call_soon_threadsafe(callback, item)
        except RuntimeError:
            # The node's loop is closed: the node has finished, and late stream messages have no reader.
            pass
