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
  requires (`relay`). Neither a handler nor the thread holds the client, even during a call. A quote or
  trade handler names the subscription it was made for (`Subscription`), the adapter-local subscription
  state the guide asks for ("Track venue subscription acknowledgments and reconnect replay in
  adapter-local state"), and a trade event handler its stream, so what a handler delivers once its
  subscription or stream is gone is dropped. Alpaca sends
  trade cancels and corrections with the trades channel; alpaca-py passes them to the handlers set with
  `register_trade_cancels` and `register_trade_corrections`, and this client logs them as warnings.
* alpaca-py's REST client is synchronous, so each call runs in a worker thread (`asyncio.to_thread`), and
  so does each stream subscribe and unsubscribe, which blocks until sent. A stream call holds the next
  until it returns, even when its hook is cancelled, and what it did is recorded then
  (`_one_stream_call`); the hook awaits that record, shielded, as asyncio's `shield` documents an inner
  task goes on.
* A connect that fails or is cancelled part way drops what it built ("Create loop-dependent sessions,
  locks, and network clients inside that async hook", "Startup, scheduling, and shutdown"), as
  `_disconnect` drops them, so no hook is served by a client that never connected.

Feeds (ADR 0055): a subscription or bars request names its feed in its params, `{"feed": "boats"}`, the
channel the adapter guide documents for adapter-specific options ("Commands preserve native identifiers
and params"), as upstream's Databento adapter takes `params={"schema": ...}` on `subscribe_quotes` and
OKX `params={"rpi": True}` for its `books-rpi` channel. With none, the feed is IEX (`constants.FEED`), as
before. The key is read and the rest of the dict left alone, since the engine shares it (`update_catalog`,
ADR 0040). A name Alpaca does not document for the call is refused before anything is sent, as Databento
refuses a schema: `constants.STREAM_FEEDS` for a subscription, `constants.BARS_FEEDS` for bars. The client
builds its one stream at its first subscription, on that subscription's feed, and refuses one whose feed
differs (Alpaca allows most users one stream connection, "Streaming Market Data", 406). alpaca-py reports
a stream Alpaca refuses only through Python `logging` (`DataStream._auth` raises the server's message,
`_drive_run` ends the run on "insufficient subscription" and retries on anything else), so its warnings
and errors from this client's stream thread are passed to the node's log, naming the feed, by a
`logging.Handler`, the way Python's logging HOWTO has an application receive a library's records. No
other feed is ever tried. An unsubscribe reads its feed as a subscription does. One that names a feed
Alpaca does not stream, or another feed than the stream's, names a subscription the client never made:
the engine passes an unsubscribe its subscription's params, and such a subscription was refused. So
nothing is sent and a warning says why, as the guide has unsubscribe hooks "tolerate members whose
subscription failed" ("Queue admission and failures").

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
returned; and with no `start` it starts the window at midnight New York of the day the window ends in.
A window that starts after now is answered with no bars and nothing is sent: of any answer,
NautilusTrader would keep only bars whose `ts_init` lies in the window, and none can yet.
Both bounds move back in elapsed time (UTC), not on New York's wall clock. To get the
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
* One feed per stream, the first subscription's; a subscription naming another is refused. A refusal by
  Alpaca at authentication shows in the log once the stream has started, not as a failed subscription:
  alpaca-py connects only after its first subscription and does not expose the outcome.
* Sizes must be whole shares (`Equity` is whole-share only); a fractional size is refused, not rounded.
* alpaca-py's REST client has no `close()`; `_disconnect` drops the reference.
* If alpaca-py's stream ends on its own (for example after an insufficient-subscription error), the client
  logs an error and does not restart it; later subscriptions are refused until it reconnects.
  alpaca-py itself reconnects and sends every handler map again, without limit, after a websocket error
  and after any error while it connects; an error once connected (an `OSError`, a timeout, any other)
  is logged and the connection kept.
  NautilusTrader v2.0.0rc8 gives a data client no way to tell the engine its data has stopped:
  `is_connected` is the runtime's and read-only, and upstream's Databento client, when it gives up
  reconnecting, logs the error ("Feed handler error", `crates/adapters/databento/src/data.rs`) and goes
  no further. So a strategy sees no more data, and the node's log says why.
* A stream alpaca-py never gets connected (406 "connection limit exceeded", for example while a stream
  abandoned at a timed-out disconnect holds the one connection) is retried by alpaca-py without end.
  The subscription has returned, the node logs alpaca-py's retry warnings naming the feed and thread,
  and no data arrives. Its connection state is private to alpaca-py, so the client sets no bound on it.
* A subscribe made while alpaca-py connects, after it has sent the handler maps and before it counts as
  running, is not sent by alpaca-py 0.45.0 (`_drive_run`, `_subscribe`). The client sends its
  subscriptions again on the first quote or trade of each stream run (`_resend_subscriptions`), which
  covers the start but not a stream whose sent subscriptions stay quiet, a reconnect alpaca-py makes by
  itself, or an unsubscribe made in the same window, which alpaca-py does not send either.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import logging
import math
import threading
import weakref
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Protocol
from typing import override
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
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

from .constants import BARS_FEEDS
from .constants import FEED
from .constants import FEED_PARAM
from .constants import STREAM_FEEDS
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

RESEND_TRIES = 3
"""How many times a stream run's subscriptions are sent again before the client gives up, each try on
the next message after a failed one (`AlpacaDataClient._heard`)."""

JOIN_POLL_SECS = 0.05
"""How often `join_within` looks whether a stream thread has ended."""

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
    """Return a coroutine function that calls `method` while its object is alive, holding it weakly
    between calls.

    During a call the stream's thread holds the object, so if every other reference goes meanwhile, it
    is freed on that thread when the call ends; `relay` does not have that window, and the data client
    uses it. The execution client's trade update handler still uses this one.
    """
    ref = weakref.WeakMethod(method)

    async def call(item: T) -> None:
        bound = ref()
        if bound is not None:
            await bound(item)

    return call


def relay[S, T](
    loop: asyncio.AbstractEventLoop,
    source: S,
    kind: type[T],
    method: Callable[[S, T], None],
) -> Callable[[object], Awaitable[None]]:
    """Return a coroutine handler for alpaca-py's stream that hands each message of type `kind` to
    `method` on the node's loop, with `source`: the `Subscription` a quote or trade handler was made for,
    or the stream a trade event handler serves.

    The stream and its thread can outlive the client when `_disconnect` times out. NautilusTrader's
    `Logger` and `Clock` are native objects that must not be dropped on another thread, so the stream's
    thread never holds the client, not even for the length of a call: the handler holds the loop and weak
    references to `method` and `source`, read only on the node's loop. `source` lets the client drop what
    a handler delivers once its subscription or stream is gone; it is held weakly because the stream
    holds the handler.
    """
    ref = weakref.WeakMethod(method)
    source_ref = weakref.ref(source)

    def deliver(item: T) -> None:  # on the node's loop
        bound, stream = ref(), source_ref()
        if bound is not None and stream is not None:
            bound(stream, item)

    async def handle(item: object) -> None:  # on the stream's thread
        if isinstance(item, kind):
            post(loop, deliver, item)

    return handle


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

ALPACA_STREAM_LOGGER = "alpaca.data.live.websocket"
"""The `logging` logger alpaca-py 0.45.0's data streams write to (`log = logging.getLogger(__name__)`)."""


def named_feed(
    params: dict | None, allowed: tuple[DataFeed, ...], call: str
) -> tuple[DataFeed, bool]:
    """Return the feed a command's params name and whether they name one; `FEED` when they do not.

    Only `FEED_PARAM` is read. A value that is not a string, `None` included, or a feed Alpaca does not
    document for `call`, is refused, naming the feeds it takes (ADR 0055): only a key that is absent
    means the default."""
    given = params or {}
    if FEED_PARAM not in given:
        return FEED, False
    value = given[FEED_PARAM]
    names = ", ".join(f.value for f in allowed)
    if not isinstance(value, str):
        raise TypeError(f"params {FEED_PARAM!r} must name an Alpaca feed ({names}), got {value!r}")
    for feed in allowed:
        if feed.value == value:
            return feed, True
    raise ValueError(f"Alpaca {call} take feed {names}; params named {value!r}")


class StreamLogRelay(logging.Handler):
    """Pass alpaca-py's stream warnings and errors from one thread to the node's loop.

    alpaca-py reports a refused authentication, a reconnect and the reason a run ended only through
    `logging`, which the node does not show. The handler holds the loop and a weak callback, never the
    client, and takes only records from the named thread, a name unique in the process (`stream_name`),
    so another stream's lines, another client's included, are not reported as this one's; each line
    carries that name and the feed this stream runs on, so a line a stream logs while it stops is told
    from the next one's. As the standard library's handlers do, a record that cannot be formatted goes
    to `handleError`, so it is never raised into alpaca-py's run."""

    def __init__(
        self,
        thread_name: str,
        feed: str,
        loop: asyncio.AbstractEventLoop,
        callback: Callable[[tuple[str, str, int, str]], None],
    ) -> None:
        super().__init__(logging.WARNING)
        self._thread_name = thread_name
        self._feed = feed
        self._loop = loop
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        if record.threadName != self._thread_name:
            return
        try:
            entry = (self._thread_name, self._feed, record.levelno, record.getMessage())
            post(self._loop, self._callback, entry)
        except Exception:  # noqa: BLE001 - `logging.StreamHandler.emit`'s own pattern
            self.handleError(record)


def retrieved(task: asyncio.Future) -> None:
    """Read a finished task's outcome, so an error it ended with is not left "never retrieved".

    A shielded call whose awaiting hook was cancelled has no other reader: asyncio's own `shield`
    documents that the inner task goes on, and a task's error nobody reads is reported as "Task
    exception was never retrieved" when it is collected."""
    if not task.cancelled():
        task.exception()


class Subscription:
    """What one subscribe call's handler names as the source of what it delivers (`relay`).

    The client maps each symbol it subscribed to the `Subscription` of the call that subscribed it; an
    unsubscribe drops it and a new subscription makes another. A message whose handler was made for a
    `Subscription` the client no longer maps, as one alpaca-py dispatched just before an unsubscribe and
    the node's loop runs after a resubscribe, is dropped. Held weakly by its handler, as a stream is."""

    __slots__ = ("__weakref__", "handler")

    def __init__(self) -> None:
        self.handler: Callable[[object], Awaitable[None]] | None = None
        """The handler made for it, kept to send its subscription again (`_resend_subscriptions`)."""


@dataclass(frozen=True, slots=True)
class StreamCall:
    """One alpaca-py stream call the client has planned (`AlpacaDataClient._one_stream_call`)."""

    call: Callable[[], None]
    """Run in a worker thread: alpaca-py's subscribe and unsubscribe block until sent."""
    started: Callable[[], None]
    """Run on the node's loop once the worker has the call, before anything it delivers is handled."""
    settle: Callable[[BaseException | None], None]
    """Run on the node's loop when the call has returned, with what it raised."""


def nothing() -> None:
    """A `StreamCall.started` with nothing to record."""


def ignore_outcome(_: BaseException | None) -> None:
    """A `StreamCall.settle` for a call whose outcome changes nothing the client records."""


async def logged_failure[R](log: Logger, what: str, step: Awaitable[R]) -> R:
    """Await `step`, logging what failed and why before the error propagates: NautilusTrader v2.0.0rc8
    logs a failed connect as "connection failed", a failed disconnect as "Failed to disconnect" and a
    failed report request as "Failed to get mass status", each without the exception's message
    (observed)."""
    try:
        return await step
    except Exception as e:
        log.error(f"Alpaca {what}: {e}")
        raise


async def join_within(thread: threading.Thread, timeout: float) -> bool:
    """Wait on the calling loop until `thread` has ended or `timeout` seconds have passed, and return
    whether it ended.

    Polled every `JOIN_POLL_SECS` rather than `Thread.join` in `asyncio.to_thread`, which needs a free
    worker of the loop's default executor, where every alpaca-py call also runs: with each worker held
    by a call that has not come back, that wait would not start, and its timeout would not run.
    NautilusTrader's own adapters bound each wait at shutdown by a deadline in the same way
    (`TaskGroup::finish_shutdown` in `crates/live/src/task.rs` at the pinned commit, which the Coinbase
    adapter's disconnect calls).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while thread.is_alive() and loop.time() < deadline:
        await asyncio.sleep(min(JOIN_POLL_SECS, max(0.0, deadline - loop.time())))
    return not thread.is_alive()


STREAM_RUNS = itertools.count(1)
"""Numbers the stream threads of every client in the process, as `threading` numbers "Thread-N" from one
module-wide counter (CPython 3.14, `threading._newname`): two clients with one ID never share a name."""


def stream_name(client_id: object) -> str:
    """A stream thread's name, unique in the process."""
    return f"{client_id}-stream-{next(STREAM_RUNS)}"


def run_stream(
    stream: QuoteTradeStream,
    loop: asyncio.AbstractEventLoop,
    on_exit: Callable[[StreamExit], None],
    ended: threading.Event,
) -> None:
    """Run alpaca-py's stream on the calling thread; when it returns, set `ended` and report it, naming
    the stream.

    `ended` is set on this thread as soon as `run()` returns, before the report reaches the node's loop
    and before the thread ends, so a subscription made meanwhile is refused (`_check_stream`)."""
    error: BaseException | None = None
    try:
        stream.run()
    except BaseException as e:
        error = e
        raise
    finally:
        ended.set()
        post(loop, on_exit, (stream, error))


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


def start_of_day(moment: datetime) -> datetime:
    """Midnight New York time on the day `moment` falls in, in UTC."""
    return datetime.combine(moment.astimezone(NEW_YORK).date(), time(0), NEW_YORK).astimezone(UTC)


def alpaca_bars_window(
    start: datetime | None, end: datetime | None, now: datetime, interval: timedelta
) -> tuple[datetime, datetime] | None:
    """Return the `start` and `end` to send Alpaca for bars whose `ts_init` lies in `[start, end]`.

    Alpaca filters on the open, inclusive at both ends, and `ts_init` is the open plus `interval`, so both
    bounds move back one interval. The end is the earlier of the requested end and `now`, so no bar whose
    `ts_init` is in the future (one still forming) is returned. With no start, the start is the beginning
    of the day the window ends in: today with no end or a later one, Alpaca's own documented default
    ("Default: the beginning of the current day", https://docs.alpaca.markets/us/reference/stockbars.md),
    taken as midnight New York because the reference names no time zone; with an end before today, that
    end's day, so the window sent never ends before it starts. The bounds move back in elapsed time, in
    UTC: on a New York time a `timedelta` moves the wall clock, which across a clock change is an hour
    off the bar's fixed length.

    Returns None for a window that starts after `now`: no bar's `ts_init` can lie in it yet, so nothing
    is sent, where cutting its end to `now` would ask Alpaca for an inverted window. A window inverted
    as requested, in the past, is sent as it is, for Alpaca to refuse.
    """
    if start is not None and start > now:
        return None
    end = now if end is None else min(end, now)
    start = start_of_day(end) if start is None else start
    return start.astimezone(UTC) - interval, end.astimezone(UTC) - interval


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

    A side priced at zero or less is refused as one-sided: a `QuoteTick` holds both sides, and a one-sided
    quote would otherwise carry a 0.0000 price for the missing side. A price that is not a finite number
    is refused as malformed, not as one-sided.
    """
    sides = (("bid", quote.bid_price), ("ask", quote.ask_price))
    for side, price in sides:  # both sides first: a malformed quote is not counted as one-sided
        if not math.isfinite(price):
            raise ValueError(f"{instrument.id} {side} price {price} is not a finite number")
    for side, price in sides:
        if price <= 0:
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
        # Clients given here are the caller's: used on every connect, never replaced by ones built from
        # the environment, and not dropped at disconnect (the adapter guide closes what the adapter
        # creates in `_connect`). The live fields are set in `_connect` and cleared on disconnect.
        self._given_historical = historical_client
        self._given_stream = stream
        # A given stream serves one connect that runs it: alpaca-py's stream keeps its subscriptions
        # and its websocket between runs, and a run abandoned at a timed-out disconnect may still be
        # using it. A connect that never subscribed never ran it.
        self._given_stream_used = False
        self._historical: BarsClient | None = None
        # Historical requests run concurrently, but alpaca-py's REST client holds one requests.Session,
        # which is not documented as thread-safe, so calls on it are made one at a time. The lock is held
        # on the node's loop, so a waiting request occupies no worker thread. Kept across a reconnect on
        # the same loop, so a call a cancelled request left running still holds it (a given client keeps
        # its one session); rebuilt only for a new loop, which an asyncio lock cannot cross.
        self._historical_lock = asyncio.Lock()
        # One alpaca-py stream call at a time (`_one_stream_call`); rebuilt at each connect.
        self._stream_calls = asyncio.Lock()
        self._locks_loop: asyncio.AbstractEventLoop | None = None
        self._stream: QuoteTradeStream | None = None
        # The feed of the stream this client runs: set when the client builds the stream, at its first
        # subscription; None for a given stream, whose feed the client cannot read.
        self._stream_feed: DataFeed | None = None
        # Builds the stream on a feed, from the key pair read at connect; None with a given stream.
        self._new_stream: Callable[..., QuoteTradeStream] | None = None
        self._stream_log: StreamLogRelay | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_ended: threading.Event | None = None  # set once the stream's `run()` returned
        self._stream_heard = False  # a quote or trade has arrived from this run of the stream
        # A subscribe was made on this run after its thread started and before anything arrived, so
        # alpaca-py may not have sent it (`_resend_subscriptions`).
        self._resend_needed = False
        self._resend_tries = 0  # resends tried on this run (`RESEND_TRIES`)
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        # Each subscribed symbol and the `Subscription` that subscribed it, per channel.
        self._quote_symbols: dict[str, Subscription] = {}
        self._trade_symbols: dict[str, Subscription] = {}
        self._one_sided_quotes: dict[str, int] = {}  # dropped per symbol since connect
        self._log = Logger(type(self).__name__)

    # -- lifecycle ---------------------------------------------------------------------------------

    @override
    async def _connect(self) -> None:
        await logged_failure(self._log, "data client failed to connect", self._connect_venue())

    async def _connect_venue(self) -> None:
        loop = self.loop
        if loop is None:
            raise RuntimeError("Alpaca data client has no event loop to connect on")
        try:
            await self._connect_on(loop)
        except BaseException:
            # A connect that fails or is cancelled part way leaves nothing a hook could use: the clients
            # it built are dropped, so a subscription or bars request is refused as not connected rather
            # than served by a client whose instruments were never loaded. The adapter guide makes
            # resources in `_connect`; what a failed one made is dropped as `_disconnect` drops it.
            self._clear_stream_state()
            raise

    async def _connect_on(self, loop: asyncio.AbstractEventLoop) -> None:
        self._owner_loop = loop
        self._stopping = False
        if self._locks_loop is not loop:
            self._historical_lock, self._locks_loop = asyncio.Lock(), loop
        # Per stream, and each connect has a new one: a subscribe stuck in the old stream does not
        # hold up the next connect's.
        self._stream_calls = asyncio.Lock()
        if self._given_stream is not None and self._given_stream_used:
            raise RuntimeError(
                "a market data stream given to the constructor serves one connect that runs it; build "
                "a new client to connect again"
            )
        self._historical, self._stream = self._given_historical, self._given_stream
        self._stream_feed, self._new_stream = None, None
        if self._historical is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._historical is None:
                self._historical = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
            if self._stream is None:
                # Built at the first subscription, on its feed (ADR 0055).
                self._new_stream = functools.partial(
                    StockDataStream, api_key=api_key, secret_key=secret_key
                )
        with_http_timeout(
            self._historical
        )  # a client passed in too; refused without a session (ADR 0039)
        if self._stream is not None:
            self._register_trade_events(self._stream, loop)
        await self._provider.initialize()
        for instrument in self._provider.list_all():
            self._handle_instrument(instrument)

    @override
    async def _disconnect(self) -> None:
        await logged_failure(
            self._log, "data client failed to disconnect", self._disconnect_stream()
        )

    async def _disconnect_stream(self) -> None:
        self._stopping = True
        thread = self._stream_thread
        try:
            if thread is not None and self._stream is not None:
                self._stream.stop()
                await join_within(thread, STREAM_JOIN_TIMEOUT_SECS)
        finally:
            # Cleared whether or not the thread stopped, and also when the wait is cancelled (a node
            # whose disconnection timeout is shorter than STREAM_JOIN_TIMEOUT_SECS), so a later connect
            # builds a fresh stream (a given one serves one connect).
            self._clear_stream_state()
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                f"Alpaca stream thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
            )

    def _register_trade_events(
        self, stream: QuoteTradeStream, loop: asyncio.AbstractEventLoop
    ) -> None:
        stream.register_trade_cancels(relay(loop, stream, TradeCancel, self._warn_trade_cancel))
        stream.register_trade_corrections(
            relay(loop, stream, TradeCorrection, self._warn_trade_correction)
        )

    def _clear_stream_state(self) -> None:
        if self._stream_log is not None:
            logging.getLogger(ALPACA_STREAM_LOGGER).removeHandler(self._stream_log)
            self._stream_log = None
        self._stream_thread = None
        self._stream_ended = None
        self._stream_heard = self._resend_needed = False
        self._resend_tries = 0
        self._stream = None
        self._stream_feed = None
        self._new_stream = None
        self._historical = None
        self._quote_symbols.clear()
        self._trade_symbols.clear()
        for symbol, count in sorted(self._one_sided_quotes.items()):
            self._log.warning(
                f"Dropped {count} one-sided Alpaca quotes for {symbol} since subscribing"
            )
        self._one_sided_quotes.clear()

    # -- subscriptions -----------------------------------------------------------------------------

    @override
    async def _subscribe_quotes(self, command: live.SubscribeQuotes) -> None:
        await self._one_stream_call(
            lambda: self._subscription(command, self._quote_symbols, Quote, self._emit_quote)
        )

    @override
    async def _unsubscribe_quotes(self, command: live.UnsubscribeQuotes) -> None:
        await self._one_stream_call(
            lambda: self._unsubscription(command, self._quote_symbols, Quote)
        )

    @override
    async def _subscribe_trades(self, command: live.SubscribeTrades) -> None:
        await self._one_stream_call(
            lambda: self._subscription(command, self._trade_symbols, Trade, self._emit_trade)
        )

    @override
    async def _unsubscribe_trades(self, command: live.UnsubscribeTrades) -> None:
        await self._one_stream_call(
            lambda: self._unsubscription(command, self._trade_symbols, Trade)
        )

    async def _one_stream_call(self, plan: Callable[[], StreamCall | None]) -> None:
        """Plan one alpaca-py stream call and make it in a worker thread, one call at a time.

        alpaca-py's `DataStream` changes and reads its handler maps from the calling thread with no lock
        of its own (`_subscribe`, `_unsubscribe`), so `_stream_calls` is held from the plan until the
        worker's call returns, not until the awaiting hook ends: a hook cancelled mid-call does not let
        the next call into alpaca-py while its own is still there. What the call did to alpaca-py's maps
        is recorded on the node's loop when it returns (`StreamCall.settle`), whether or not the hook
        still waits, so the client's symbols and alpaca-py's handlers agree. The hook awaits, shielded, a
        future `finished` resolves after recording (`settled`), not the worker's future, and reads the
        call's outcome from it. An executor that refuses the call raises here, and nothing is recorded.

        With no live stream (not connected, disconnecting, or its `run()` returned, and alpaca-py's loop
        with it), no call is made and no earlier one waited for: a call into alpaca-py then could wait for
        that loop for good (`run_coroutine_threadsafe(...).result()`). A subscription is refused by its
        plan; an unsubscription forgets its symbols, as alpaca-py's stream, which sends nothing when not
        running, forgets its handlers. A call already stuck that way still holds a hook that queued
        behind it before the stream ended or the client reconnected."""
        if not self._stream_live():
            self._settle_locally(plan)
            return
        loop = asyncio.get_running_loop()
        while True:  # the lock of the connect this call runs in, which a reconnect replaces
            lock = self._stream_calls
            await lock.acquire()
            if lock is self._stream_calls:
                break
            lock.release()
        try:
            if not self._stream_live():  # it ended while this call waited for the one before
                lock.release()
                self._settle_locally(plan)
                return
            step = plan()
            if step is None:
                lock.release()
                return
            context = contextvars.copy_context()
            call = loop.run_in_executor(None, functools.partial(context.run, step.call))
        except BaseException:
            lock.release()
            raise

        # The hook awaits `settled`, which `finished` resolves once the outcome is recorded and the lock
        # released, not the call itself: a call that returns before `run_in_executor` has chained its
        # future (CPython 3.14, `futures._chain_future`, which then copies the state at once) is done
        # already, and `shield` hands back a done future, so awaiting it would resume the hook before
        # `finished` had run.
        settled = loop.create_future()
        settled.add_done_callback(retrieved)  # read if the hook awaiting it was cancelled meanwhile

        def finished(done: asyncio.Future) -> None:
            error = asyncio.CancelledError() if done.cancelled() else done.exception()
            failed: BaseException | None = None
            try:
                step.settle(error)
            except Exception as e:  # noqa: BLE001 - handed to the hook below, not lost in a callback
                failed = e
            finally:
                lock.release()
            if failed is not None:  # the client's record is not what alpaca-py did: say so
                settled.set_exception(failed)
            elif error is None:
                settled.set_result(None)
            elif done.cancelled():
                settled.cancel()
            else:
                settled.set_exception(error)

        # Added before `started`, so the lock is released when the call returns even if `started` raises.
        call.add_done_callback(finished)
        step.started()
        await asyncio.shield(settled)

    def _stream_live(self) -> bool:
        """Whether a stream call can reach alpaca-py: connected, not disconnecting, and the stream, if
        built, not ended."""
        connected = self._stream is not None or self._new_stream is not None
        ended = self._stream_ended is not None and self._stream_ended.is_set()
        return connected and not self._stopping and not ended

    @staticmethod
    def _settle_locally(plan: Callable[[], StreamCall | None]) -> None:
        """Apply a plan with no alpaca-py call, as if the call had sent nothing and succeeded."""
        step = plan()
        if step is not None:
            step.started()
            step.settle(None)

    def _subscription[T](
        self,
        command: live.SubscribeQuotes | live.SubscribeTrades,
        symbols: dict[str, Subscription],
        kind: type[T],
        emit: Callable[[Subscription, T], None],
    ) -> StreamCall | None:
        """Plan subscribing the command's new symbols on the stream, starting it on the command's feed.

        A command whose symbols are all subscribed already still has its feed and the stream checked, so
        it is refused as a new one would be."""
        feed, named = self._subscription_feed(command.params)
        serveable = self._serveable(command.instrument_ids)  # those it cannot serve are logged
        if not serveable:
            return None
        self._check_stream(feed, named)
        new = [s for s in serveable if s not in symbols]
        if not new:
            return None
        # A run already started may still be connecting, when alpaca-py records a subscribe and sends
        # nothing (`_resend_subscriptions`).
        may_be_lost = self._stream_thread is not None and not self._stream_heard
        stream, loop = self._started_stream(feed)
        subscription = Subscription()
        handler = subscription.handler = relay(loop, subscription, kind, emit)
        subscribe = stream.subscribe_quotes if kind is Quote else stream.subscribe_trades

        def started() -> None:
            # Recorded once the worker has the call, before anything it delivers reaches this loop, and
            # kept if the call fails: alpaca-py records the handler before it sends, so it re-sends the
            # subscription on reconnect, and unsubscribe must still be able to remove it.
            symbols.update(dict.fromkeys(new, subscription))
            if may_be_lost:
                self._resend_needed = True

        return StreamCall(functools.partial(subscribe, handler, *new), started, ignore_outcome)

    def _unsubscription(
        self,
        command: live.UnsubscribeQuotes | live.UnsubscribeTrades,
        symbols: dict[str, Subscription],
        kind: type,
    ) -> StreamCall | None:
        """Plan unsubscribing the command's subscribed symbols from the stream.

        The command's feed is read as a subscription's is. A value Alpaca does not stream, or a feed other
        than the stream's, names a subscription this client never made (the engine passes an unsubscribe
        the params of its subscription, which was refused if it named such a feed), so it is tolerated, as
        the adapter guide has unsubscribe hooks "tolerate members whose subscription failed": nothing is
        sent, and a warning says why."""
        channel = "quotes" if kind is Quote else "trades"
        try:
            feed, named = named_feed(command.params, STREAM_FEEDS, "subscriptions")
        except (TypeError, ValueError) as e:
            self._log.warning(f"Not unsubscribing {channel}: {e}; nothing was subscribed on it")
            return None
        found = self._subscribed(command.instrument_ids, symbols)
        stream = self._stream
        if not found or stream is None:
            return None
        if not self._serves_feed(feed, named):
            self._log.warning(
                f"Not unsubscribing {channel} for {', '.join(found)}: the unsubscribe names feed "
                f"{feed.value}{'' if named else ' (named none, so the default)'} and this client's "
                f"stream runs on {self._feed_name()}, where they are subscribed"
            )
            return None
        made = {s: symbols[s] for s in found}
        unsubscribe = stream.unsubscribe_quotes if kind is Quote else stream.unsubscribe_trades

        def settle(error: BaseException | None) -> None:
            # alpaca-py sends the unsubscribe first and drops its handlers only if the send succeeds,
            # so the symbols are dropped only then, and a failed unsubscribe can be retried. A symbol
            # subscribed afresh meanwhile, as after a reconnect, has another `Subscription` and stays.
            if error is not None:
                return
            for symbol, subscription in made.items():
                if symbols.get(symbol) is subscription:
                    del symbols[symbol]
                    if kind is Quote:  # counted afresh after a new subscription
                        self._one_sided_quotes.pop(symbol, None)

        return StreamCall(functools.partial(unsubscribe, *found), nothing, settle)

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
        timeframe = bar_timeframe(bar_type)  # first: it names what Alpaca serves (not a TICK bar)
        feed, _ = named_feed(request.params, BARS_FEEDS, "bar requests")
        interval = timedelta(microseconds=bar_interval_ns(bar_type) // 1_000)
        window = alpaca_bars_window(request.start, request.end, self.clock.utc_now(), interval)
        if window is None:  # a window starting after now holds no finished bar: nothing to ask for
            self._handle_response(self._bars_response(request, bar_type, []))
            return
        start, end = window
        params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=request.limit,
            feed=feed,
        )

        result = await self._one_historical_call(historical.get_stock_bars, params)
        if not isinstance(result, BarSet):
            raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
        bars = [parse_bar(bar, bar_type, instrument) for bar in result.data.get(symbol, [])]
        self._handle_response(self._bars_response(request, bar_type, bars))

    def _bars_response(
        self, request: live.RequestBars, bar_type: BarType, bars: list[Bar]
    ) -> BarsResponse:
        """The response to `request`, with its ID, bounds and params as requested."""
        return BarsResponse(
            client_id=self.client_id,
            bar_type=bar_type,
            data=bars,
            correlation_id=request.request_id,
            ts_init=self.clock.timestamp_ns(),
            start=request.start_ns,
            end=request.end_ns,
            params=request.params,
        )

    async def _one_historical_call(
        self, fn: Callable[[StockBarsRequest], BarSet | RawData], arg: StockBarsRequest
    ) -> BarSet | RawData:
        """Run `fn(arg)` in a worker thread, one call at a time, releasing the lock when the call returns.

        The worker's future is shielded, so cancelling the awaiting hook leaves the call running and the
        lock held until it ends; the worker hands the release back to the node's loop. An executor that
        refuses the work (one shut down) raises here, and the lock is released. If the loop stops before
        a queued call starts, the lock stays held, and `_connect` on a new loop builds a new one.
        """
        loop = asyncio.get_running_loop()
        lock = self._historical_lock
        await lock.acquire()
        try:
            # What `asyncio.to_thread` does (CPython 3.14, `asyncio/threads.py`), called here so that an
            # executor refusing the work raises before anything could release the lock.
            context = contextvars.copy_context()
            work = functools.partial(context.run, release_after, loop, lock, fn, arg)
            call = loop.run_in_executor(None, work)
        except BaseException:
            lock.release()
            raise
        call.add_done_callback(retrieved)  # read if the hook awaiting it was cancelled meanwhile
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
    def _subscribed(
        instrument_ids: Iterable[InstrumentId], current: dict[str, Subscription]
    ) -> list[str]:
        return [
            i.symbol.value for i in instrument_ids if i.venue == VENUE and i.symbol.value in current
        ]

    def _subscription_feed(self, params: dict | None) -> tuple[DataFeed, bool]:
        try:
            return named_feed(params, STREAM_FEEDS, "subscriptions")
        except (TypeError, ValueError) as e:
            self._log.error(f"Cannot subscribe: {e}")
            raise

    def _feed_name(self) -> str:
        if self._stream_feed is None:
            return "unknown (a stream given to the constructor)"
        return self._stream_feed.value

    def _serves_feed(self, feed: DataFeed, named: bool) -> bool:
        """Whether a command on `feed` is for the stream the client runs, or would build: there is none
        yet, it is the stream's feed, or it names none on a given stream, whose feed is unknown."""
        given = self._stream_feed is None
        return self._stream is None or (given and not named) or feed == self._stream_feed

    def _refuse_other_feed(self, feed: DataFeed, named: bool) -> None:
        """Refuse a subscription whose feed is not the running stream's: the client runs one stream."""
        if self._serves_feed(feed, named):
            return
        if self._stream_feed is None:
            reason = (
                "this client's stream was given to its constructor, so it cannot tell that stream's "
                f"feed, and serves only subscriptions that name none; one named {feed.value}"
            )
        else:
            asked = feed.value if named else f"{feed.value} (named none, so the default)"
            reason = (
                f"this client runs one stream, on feed {self._feed_name()} from its first "
                f"subscription; this subscription asks for {asked}; use another data client for it"
            )
        self._log.error(f"Cannot subscribe: {reason}")
        raise ValueError(reason)

    def _check_stream(self, feed: DataFeed, named: bool) -> None:
        """Refuse a subscription the stream cannot take: not connected, ended, or on another feed."""
        if self._owner_loop is None or (self._stream is None and self._new_stream is None):
            raise RuntimeError("Alpaca data client is not connected")
        if self._stopping:  # its stream is being stopped on purpose: not "ended by itself"
            raise RuntimeError("Alpaca data client is disconnecting")
        if self._stream_ended is not None and self._stream_ended.is_set():
            # The stream's `run()` returned by itself (`_on_stream_exit`), which `run_stream` marks on
            # the stream's thread before it reports it or ends, and is not restarted (README, "No
            # restart"): a subscription recorded on it would deliver nothing, so it fails, naming why.
            raise RuntimeError(
                f"Alpaca stream on feed {self._feed_name()} ended by itself and this client does not "
                "restart it or try another feed; reconnect the data client to subscribe again"
            )
        self._refuse_other_feed(feed, named)

    def _started_stream(self, feed: DataFeed) -> tuple[QuoteTradeStream, asyncio.AbstractEventLoop]:
        """The stream, built on `feed` if there is none yet, with its thread running. `_check_stream`
        has passed. If the thread cannot start, a stream built here is dropped with its feed, so the
        next subscription starts afresh on its own feed."""
        loop = self._owner_loop
        assert loop is not None
        built = self._stream is None
        if self._stream is None:
            assert self._new_stream is not None
            self._stream, self._stream_feed = self._new_stream(feed=feed), feed
            self._register_trade_events(self._stream, loop)
        if self._stream_thread is None:
            # A name unique in the process: a thread left behind by a timed-out disconnect, or another
            # client's, keeps its own, so its log lines are not taken for this stream's.
            name = stream_name(self.client_id)
            relay_log = StreamLogRelay(
                name, self._feed_name(), loop, weak_callback(self._on_stream_log)
            )
            ended = threading.Event()
            thread = threading.Thread(
                target=run_stream,
                args=(self._stream, loop, weak_callback(self._on_stream_exit), ended),
                name=name,
                daemon=True,
            )
            logger = logging.getLogger(ALPACA_STREAM_LOGGER)
            logger.addHandler(relay_log)
            try:
                thread.start()
            except BaseException:
                logger.removeHandler(relay_log)  # nothing runs, so nothing is recorded as running
                if built:
                    self._stream, self._stream_feed = None, None
                raise
            self._stream_log, self._stream_thread, self._stream_ended = relay_log, thread, ended
            self._stream_heard = self._resend_needed = False
            self._resend_tries = 0
            # A given stream has now run: alpaca-py keeps its subscriptions and websocket between runs.
            self._given_stream_used = self._stream is self._given_stream
        return self._stream, loop

    def _current(self, stream: QuoteTradeStream) -> bool:
        """Whether a message from `stream` is still wanted: the client runs it and is not stopping.

        A stream left running by a timed-out disconnect delivers until its thread ends, and after a
        reconnect the symbol maps hold the new stream's subscriptions."""
        return stream is self._stream and not self._stopping

    def _wanted(self, subscription: Subscription, symbol: str, current: dict) -> bool:
        """Whether a quote or trade is still wanted: its handler's subscription is the one the client
        holds for its symbol, which a reconnect, an unsubscribe and a resubscribe each replace, and the
        client is not stopping."""
        return not self._stopping and symbol in current and current[symbol] is subscription

    def _heard(self) -> None:
        """A message has arrived from the stream's current run. Once per run, if a subscribe was
        recorded after the run started and before this, send the client's subscriptions again
        (`_resend_subscriptions`). A resend that fails, or cannot be scheduled, is logged and tried
        again on the next message, `RESEND_TRIES` times in all; the message itself is handled either
        way."""
        if self._stream_heard:
            return
        self._stream_heard = True  # also while a resend is on its way, so one is made at a time
        if not self._resend_needed or self._resend_tries >= RESEND_TRIES:
            return
        self._resend_tries += 1
        resend = self._resend_subscriptions(self._stream)
        try:
            self.create_task(resend, name="alpaca-stream-resend")
        except Exception as e:  # noqa: BLE001 - logged; the message is still handled
            resend.close()
            self._resend_failed(f"cannot be scheduled: {e}")

    def _resend_failed(self, why: str) -> None:
        """Log a failed resend, and let the next message try again while tries remain."""
        left = RESEND_TRIES - self._resend_tries
        then = (
            f"the next message tries again ({left} left)"
            if left
            else "the client gives up; a subscription made while the stream connected may never be "
            "sent, until the next subscribe"
        )
        self._log.error(f"Alpaca stream: sending the subscriptions again {why}; {then}")
        self._stream_heard = False

    async def _resend_subscriptions(self, stream: QuoteTradeStream | None) -> None:
        """Send the client's subscriptions once more on `stream`, now that it is known to be running.

        alpaca-py 0.45.0's `_drive_run` sends its handler maps when it connects and only then sets
        `_running`, and `_subscribe` sends only while `_running`, so a subscribe made between the two is
        recorded and never sent (`test_stream_stand_in_misses_a_subscribe_made_while_it_connects`). The
        subscribe that starts the run is always sent, since the run waits for it before connecting.
        alpaca-py dispatches a message only from `_consume`, after `_running` is set, so once one has
        arrived a subscribe is sent; and a subscribe while running sends every handler map whole
        (`_send_subscribe_msg`). So one subscribe of a symbol already held, with its own handler, sends
        whatever that window lost and changes nothing else: alpaca-py keeps the same handler, and Alpaca
        answers a subscribe with the full list of subscriptions it now holds ("Streaming Market Data").
        It narrows the window rather than closing it: nothing arrives to trigger it while every sent
        subscription is quiet; the same window opens at each reconnect alpaca-py makes by itself, which
        the client cannot see; and an unsubscribe lost in it, which alpaca-py does not send either, is not
        undone by a subscribe. Only `stream`, the one heard from, is resubscribed, on its own connect."""
        try:
            await self._one_stream_call(lambda: self._resend_plan(stream))
        except Exception as e:  # noqa: BLE001 - background work: logged, as the guide logs hook failures
            if stream is self._stream:
                self._resend_failed(f"failed: {e}")
        else:
            if stream is self._stream:
                self._resend_needed = False

    def _resend_plan(self, stream: QuoteTradeStream | None) -> StreamCall | None:
        if stream is None or stream is not self._stream:
            return None
        for symbols, subscribe in (
            (self._quote_symbols, stream.subscribe_quotes),
            (self._trade_symbols, stream.subscribe_trades),
        ):
            for symbol, subscription in symbols.items():  # the first held symbol is enough
                assert subscription.handler is not None  # set where it is made (`_subscription`)
                call = functools.partial(subscribe, subscription.handler, symbol)
                return StreamCall(call, nothing, ignore_outcome)
        return None

    def _emit_quote(self, subscription: Subscription, quote: Quote) -> None:
        if not self._wanted(subscription, quote.symbol, self._quote_symbols):
            return
        self._heard()
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

    def _emit_trade(self, subscription: Subscription, trade: Trade) -> None:
        if not self._wanted(subscription, trade.symbol, self._trade_symbols):
            return
        self._heard()
        try:
            instrument = self._equity(InstrumentId(Symbol(trade.symbol), VENUE))
            tick = parse_trade(trade, instrument, self.clock.timestamp_ns())
        except (ValueError, TypeError) as e:
            self._log.error(f"Dropped Alpaca trade: {e}")
            return
        self._handle_data(tick)

    def _warn_trade_cancel(self, stream: QuoteTradeStream, cancel: TradeCancel) -> None:
        if not self._current(stream):
            return
        self._heard()  # a running stream's message as much as a trade is
        if cancel.symbol not in self._trade_symbols:
            return
        action = "error" if cancel.action == "E" else "cancel"
        self._log.warning(
            f"Alpaca trade {action} for {cancel.symbol} trade {cancel.id} at {cancel.timestamp}; "
            "a trade tick already emitted is not withdrawn",
        )

    def _warn_trade_correction(self, stream: QuoteTradeStream, correction: TradeCorrection) -> None:
        if not self._current(stream):
            return
        self._heard()
        if correction.symbol not in self._trade_symbols:
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
        if self._current(stream):
            self._log.error(
                f"Alpaca stream on feed {self._feed_name()} ended by itself while subscribed "
                f"({error!r}); alpaca-py ends a stream Alpaca refuses at authentication, such as a feed "
                "outside the account's plan (409 insufficient subscription), with the reason logged "
                "above; this client does not restart it or try another feed"
            )

    def _on_stream_log(self, entry: tuple[str, str, int, str]) -> None:
        # Kept while the stream stops, as its own lines say why a stop is slow; named by its thread.
        thread, feed, level, message = entry
        line = f"Alpaca stream on feed {feed} ({thread}): {message}"
        if level >= logging.ERROR:
            self._log.error(line)
        else:
            self._log.warning(line)
