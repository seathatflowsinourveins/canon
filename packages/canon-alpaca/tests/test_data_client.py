"""AlpacaDataClient inside a real LiveNode, offline: factory registration, data delivery and shutdown.

The shape follows upstream's template tests (`python/tests/integration/test_python_adapter_template.py` at
the pinned NautilusTrader commit): the node is built with the adapter's own factory, through both
registration paths the adapter guide documents, a strategy drives it, and the node shuts itself down, on
both the owned (`node.run()`) and hosted (`await node.run_async()`) launch modes. The fill test is in
test_execution_client.py.

The three alpaca-py constructors the adapter calls are replaced by recorders that hand out the stand-ins
in conftest.py, so the real factory, `_connect` and credential lookup run. The environment holds
placeholder strings, not credentials.

Some tests call a hook directly on the client the node connected (`run_with_client`), for behaviour the
engine never drives on its own: an unsubscribe for an instrument never subscribed, a failed send, a stream
thread that does not stop.
"""

import asyncio
import gc
import threading
import weakref
from collections.abc import Awaitable
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Literal
from zoneinfo import ZoneInfo

import canon_alpaca.data
import canon_alpaca.providers
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrameUnit
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import AlpacaDataClient
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.common import Logger
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core import UUID4
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

AAPL = InstrumentId.from_str("AAPL.ALPACA")
BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")
START = datetime(2022, 1, 3, 9, tzinfo=UTC)
END = datetime(2022, 1, 3, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)
NEW_YORK = ZoneInfo("America/New_York")
TRADER = TraderId("TESTER-001")
PLACEHOLDER = "offline-placeholder"
LOG_PROBE_LINES = 20_000


@pytest.fixture
def venue(monkeypatch, alpaca) -> SimpleNamespace:
    """Replace alpaca-py's client constructors with recorders that return the stand-ins."""
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    ns = SimpleNamespace(
        trading=alpaca.TradingClient(),
        historical=alpaca.HistoricalClient(),
        stream=alpaca.Stream(),
        built={},
    )

    def recorder(name: str, instance: object):
        def build(**kwargs: object) -> object:
            ns.built[name] = kwargs
            return instance

        return build

    monkeypatch.setattr(canon_alpaca.providers, "TradingClient", recorder("trading", ns.trading))
    monkeypatch.setattr(
        canon_alpaca.data,
        "StockHistoricalDataClient",
        recorder("historical", ns.historical),
    )
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", recorder("stream", ns.stream))
    return ns


class Consumer(Strategy):
    """Subscribe and request as configured, record what arrives, and stop once all of it has.

    `Strategy` is a native class whose `__new__` takes no extra arguments, so `consumer()` sets the options.
    """

    def __init__(self) -> None:
        super().__init__()
        self.want_quotes = True
        self.want_trades = True
        self.bar_type: BarType | None = BAR_TYPE
        self.bars_start: datetime | None = START
        self.bars_end: datetime | None = END
        self.bars_limit: int | None = None
        self.bar_requests = 1
        self.auto_shutdown = True
        self.seen_quotes: list = []
        self.seen_trades: list = []
        self.seen_bars: list = []
        self.seen_responses: list = []
        self.request_id: str | None = None
        self.finished = False

    def on_start(self) -> None:
        if self.want_quotes:
            self.subscribe_quotes(AAPL)
        if self.want_trades:
            self.subscribe_trades(AAPL)
        if self.bar_type is not None:
            for _ in range(self.bar_requests):
                self.request_id = self.request_bars(
                    self.bar_type, start=self.bars_start, end=self.bars_end, limit=self.bars_limit
                )

    def on_quote(self, quote) -> None:
        self.seen_quotes.append(quote)
        self._check()

    def on_trade(self, trade) -> None:
        self.seen_trades.append(trade)
        self._check()

    def on_historical_bars(self, bars) -> None:
        self.seen_bars.extend(bars)

    def on_data_response(self, response) -> None:
        self.seen_responses.append((str(response.correlation_id), response.failure_reason))
        self._check()

    def _check(self) -> None:
        done = (
            (self.seen_quotes or not self.want_quotes)
            and (self.seen_trades or not self.want_trades)
            and (len(self.seen_responses) >= self.bar_requests or self.bar_type is None)
        )
        if done and self.auto_shutdown and not self.finished:
            self.finished = True
            self.shutdown_system("Alpaca data checked")


def consumer(**options: object) -> Consumer:
    strategy = Consumer()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


BUILT_NODES: list[LiveNode] = []


@pytest.fixture(autouse=True)
def dispose_nodes():
    """Dispose of every node a test built, so a failed test does not leave one alive for the next.

    NautilusTrader allows one `LiveNode` per thread, and a failing test's traceback keeps its node alive.
    """
    yield
    while BUILT_NODES:
        BUILT_NODES.pop().dispose()


def logged(capfd) -> str:
    """Everything the node logged, whichever of stdout and stderr each level goes to.

    The node's logger writes on its own thread, fed by a channel, so a line logged just before the node
    stopped, such as a summary from `_disconnect`, can still be queued when the run returns.
    `logging_sync_to_disk` queues a sync behind every line already sent and blocks until the writer thread
    has flushed stdout and stderr and acknowledged it (`crates/common/src/logging/logger.rs` at
    NautilusTrader v2.0.0rc8, `d53849b2`, `LogEvent::Sync`; its doc speaks only of file logs, the code flushes both
    streams first). `Logger.flush` and `logger_flush` only queue a flush and do not wait. It returns at once
    when the logger is not running; `test_logged_waits_for_lines_still_queued_after_a_run` fails if so.
    """
    logging_sync_to_disk()
    captured = capfd.readouterr()
    return captured.out + captured.err


def build_node(
    strategy: Strategy,
    registration: str = "config",
    timeout_connection_secs: int = 2,
    factory: type[AlpacaDataClientFactory] = AlpacaDataClientFactory,
):
    config = DataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"])
    )
    if registration == "config":
        node = LiveNode.build(
            "CANON-ALPACA",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TRADER,
                timeout_connection_secs=timeout_connection_secs,
                timeout_disconnection_secs=5,
                delay_post_stop_secs=0,
                data_clients={"ALPACA": config},
            ),
            data_factories={"ALPACA": factory},
        )
    else:
        node = (
            LiveNode.builder("CANON-ALPACA", TRADER, Environment.SANDBOX)
            .with_delay_post_stop_secs(0)
            .with_timeout_connection(timeout_connection_secs)
            .with_timeout_disconnection_secs(5)
            .add_data_client("ALPACA", factory, config)
            .build()
        )
    node.add_strategy(strategy)
    BUILT_NODES.append(node)
    return node


async def run_hosted(node) -> None:
    async with asyncio.timeout(15):
        await node.run_async()


def capturing_factory(clients: list) -> type[AlpacaDataClientFactory]:
    """The adapter's factory, recording each client it builds."""

    class Capturing(AlpacaDataClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaDataClientFactory.create(**kwargs)
            clients.append(client)
            return client

    return Capturing


async def wait_until(condition: Callable[[], object], timeout: float = 5.0) -> None:
    """Poll on the loop without a worker thread, so a test can watch the default executor."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def run_with_client(
    body: Callable[[AlpacaDataClient], Awaitable[None]],
    strategy: Strategy | None = None,
    max_workers: int | None = None,
):
    """Run a hosted node, await `body` with its connected Alpaca client, stop the node, return its cache.

    With `max_workers`, the loop's default executor, which `asyncio.to_thread` uses, has that many threads.
    The cache is taken before the run, as NautilusTrader requires for a node run by `run_async`.
    """
    clients: list[AlpacaDataClient] = []
    strategy = strategy or consumer(
        want_quotes=False, want_trades=False, bar_type=None, auto_shutdown=False
    )
    # Held in a list that is emptied on the way out, so a failure's traceback does not keep the node.
    nodes = [build_node(strategy, factory=capturing_factory(clients))]
    handle = nodes[0].handle()
    cache = nodes[0].cache

    async def main() -> None:
        if max_workers is not None:
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers))
        async with asyncio.timeout(15):
            task = asyncio.ensure_future(nodes[0].run_async())
            try:
                await wait_until(lambda: task.done() or (clients and clients[0].is_connected))
                if task.done():
                    task.result()
                await body(clients[0])
            finally:
                if not task.done():
                    handle.stop()
                await task

    try:
        asyncio.run(main())
    finally:
        nodes.clear()
    return cache


def command(*instrument_ids: InstrumentId) -> SimpleNamespace:
    """The one field the subscription hooks read from a command."""
    return SimpleNamespace(instrument_ids=list(instrument_ids))


def run(node, launch: str) -> None:
    if launch == "owned":
        node.run()
    else:
        asyncio.run(run_hosted(node))


def stream_threads_alive(stream) -> list[str]:
    return [t.name for t in threading.enumerate() if t.name in stream.run_threads]


@pytest.mark.parametrize("launch", ["owned", "hosted"])
@pytest.mark.parametrize("registration", ["config", "builder"])
def test_bars_quotes_and_trades_reach_strategy_and_cache(venue, registration, launch) -> None:
    strategy = Consumer()
    node = build_node(strategy, registration)

    run(node, launch)

    instrument = node.cache.instrument(AAPL)
    assert isinstance(instrument, Equity)

    (bar,) = strategy.seen_bars
    assert bar.bar_type == BAR_TYPE
    assert bar.close == Price.from_str("178.2100")
    assert bar.ts_init - bar.ts_event == 3_600_000_000_000
    assert strategy.seen_responses == [(strategy.request_id, None)]
    (request,) = venue.historical.requests
    assert request.symbol_or_symbols == "AAPL"
    assert (request.timeframe.amount, request.timeframe.unit) == (1, TimeFrameUnit.Hour)
    # One interval earlier, since Alpaca filters on the open; alpaca-py stores naive UTC.
    assert request.start == (START - HOUR).replace(tzinfo=None)
    assert request.end == (END - HOUR).replace(tzinfo=None)
    assert request.feed == DataFeed.IEX

    (quote,) = strategy.seen_quotes
    assert quote.bid_price == Price.from_str("87.6600")
    assert node.cache.quote(AAPL) == quote
    (trade,) = strategy.seen_trades
    assert trade.trade_id == TradeId("96921")


def test_alpaca_clients_are_built_for_paper_and_iex(venue) -> None:
    run(build_node(Consumer()), "hosted")

    assert venue.built["trading"] == {
        "api_key": PLACEHOLDER,
        "secret_key": PLACEHOLDER,
        "paper": True,
    }
    assert venue.built["historical"] == {"api_key": PLACEHOLDER, "secret_key": PLACEHOLDER}
    assert venue.built["stream"] == {
        "api_key": PLACEHOLDER,
        "secret_key": PLACEHOLDER,
        "feed": DataFeed.IEX,
    }


def test_shutdown_stops_and_joins_the_stream_thread(venue) -> None:
    run(build_node(Consumer()), "hosted")

    assert venue.stream.run_threads == ["ALPACA-stream"]
    assert venue.stream.stopped.is_set()
    assert stream_threads_alive(venue.stream) == []


def test_unsubscribe_of_a_never_subscribed_instrument_sends_nothing(venue) -> None:
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await client._unsubscribe_trades(command(AAPL))  # trades were never subscribed
        await client._unsubscribe_quotes(command(InstrumentId.from_str("MSFT.ALPACA")))

    run_with_client(body)

    assert venue.stream.unsubscribed == []
    assert list(venue.stream.quote_handlers) == ["AAPL"]


def test_failed_unsubscribe_can_be_retried(venue) -> None:
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        venue.stream.send_error = ConnectionError("send failed")
        with pytest.raises(ConnectionError, match="send failed"):
            await client._unsubscribe_quotes(command(AAPL))
        assert list(venue.stream.quote_handlers) == ["AAPL"]  # alpaca-py keeps it too

        venue.stream.send_error = None
        await client._unsubscribe_quotes(command(AAPL))

    run_with_client(body)

    assert venue.stream.unsubscribed == [("quotes", ("AAPL",))]
    assert venue.stream.quote_handlers == {}


@pytest.mark.parametrize(
    ("start", "end", "limit", "closes"),
    [
        (START, START + 2 * HOUR, None, [START, START + HOUR, START + 2 * HOUR]),
        (START, START + HOUR, 2, [START, START + HOUR]),
    ],
)
def test_bar_window_holds_the_bars_that_close_inside_it(venue, alpaca, start, end, limit, closes):
    venue.historical.bars = alpaca.hourly_bars(START - 2 * HOUR, 6)  # opens 07:00 to 12:00
    strategy = consumer(
        want_quotes=False, want_trades=False, bars_start=start, bars_end=end, bars_limit=limit
    )

    run(build_node(strategy), "hosted")

    assert strategy.seen_responses == [(strategy.request_id, None)]
    assert [b.ts_init for b in strategy.seen_bars] == [dt_to_unix_nanos(c) for c in closes]


FRI = datetime(2022, 1, 7, tzinfo=UTC)  # a Friday; New York is on EST (UTC-5) in January
MON = datetime(2022, 1, 10, tzinfo=UTC)
NY_MIDNIGHT = timedelta(hours=5)
DAILY_BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")


@pytest.mark.parametrize(
    ("start", "end", "opens"),
    [
        # Friday evening to Monday evening: only Friday's bar, stamped at Saturday midnight New York.
        (FRI + timedelta(hours=21), MON + timedelta(hours=21), [FRI + NY_MIDNIGHT]),
        # Inside Monday's session: Monday's bar is stamped at Tuesday midnight, so nothing.
        (MON + timedelta(hours=14, minutes=30), MON + timedelta(hours=21), []),
        # Monday noon to Tuesday noon New York: Monday's bar.
        (MON + timedelta(hours=17), MON + timedelta(days=1, hours=17), [MON + NY_MIDNIGHT]),
    ],
)
def test_daily_bar_window_selects_by_open_plus_24_hours(venue, alpaca, start, end, opens):
    days = [FRI - timedelta(days=1), FRI, MON, MON + timedelta(days=1)]  # Thursday to Tuesday
    venue.historical.bars = alpaca.bars_opening(*(d + NY_MIDNIGHT for d in days))
    strategy = consumer(
        want_quotes=False,
        want_trades=False,
        bar_type=DAILY_BAR_TYPE,
        bars_start=start,
        bars_end=end,
    )

    run(build_node(strategy), "hosted")

    assert strategy.seen_responses == [(strategy.request_id, None)]
    assert [b.ts_event for b in strategy.seen_bars] == [dt_to_unix_nanos(o) for o in opens]
    assert all(b.ts_init == b.ts_event + 86_400_000_000_000 for b in strategy.seen_bars)


def opens_received(venue, alpaca) -> list[int]:
    """`ts_event` of the stand-in's bars that fall in the window the adapter actually sent."""
    (request,) = venue.historical.requests
    start, end = request.start.replace(tzinfo=UTC), request.end.replace(tzinfo=UTC)
    opens = [datetime.fromisoformat(b["t"]) for b in venue.historical.bars]
    return [dt_to_unix_nanos(o) for o in sorted(opens) if start <= o <= end]


def sent_window(venue) -> tuple[datetime, datetime]:
    (request,) = venue.historical.requests
    return request.start.replace(tzinfo=UTC), request.end.replace(tzinfo=UTC)


def test_open_ended_bar_request_returns_no_bar_stamped_in_the_future(venue, alpaca) -> None:
    before = datetime.now(UTC)
    first_open = before.replace(minute=0, second=0, microsecond=0) - 3 * HOUR
    # Finished bars, the one forming now, and two that have not opened yet.
    venue.historical.bars = alpaca.hourly_bars(first_open, 6)
    strategy = consumer(want_quotes=False, want_trades=False, bars_start=first_open, bars_end=None)

    run(build_node(strategy), "hosted")
    after = datetime.now(UTC)

    # Bounds are read from what the adapter sent, so the test does not depend on where the hour falls.
    start_sent, end_sent = sent_window(venue)
    assert start_sent == first_open - HOUR
    assert before - HOUR <= end_sent <= after - HOUR  # the end sent is now, one interval back
    assert strategy.seen_responses == [(strategy.request_id, None)]
    assert [b.ts_event for b in strategy.seen_bars] == opens_received(venue, alpaca)
    assert all(b.ts_init <= dt_to_unix_nanos(after) for b in strategy.seen_bars)


def test_bar_request_ending_in_the_future_is_cut_at_now(venue, alpaca) -> None:
    # NautilusTrader's `request_bars` refuses an end after now ("end was > now"), so the hook is called
    # directly, as another caller of it could.
    times = {}

    async def body(client) -> None:
        times["before"] = datetime.now(UTC)
        await client._request_bars(bars_request(START, times["before"] + 3 * HOUR))
        times["after"] = datetime.now(UTC)

    run_with_client(body)

    _, end_sent = sent_window(venue)
    assert times["before"] - HOUR <= end_sent <= times["after"] - HOUR


def test_bar_request_with_no_bounds_starts_at_the_beginning_of_the_day(venue, alpaca) -> None:
    today = datetime.now(NEW_YORK).date()
    days = [today + timedelta(days=d) for d in range(-5, 1)]
    venue.historical.bars = alpaca.bars_opening(
        *(datetime.combine(d, datetime.min.time(), NEW_YORK) for d in days)
    )
    strategy = consumer(
        want_quotes=False,
        want_trades=False,
        bar_type=DAILY_BAR_TYPE,
        bars_start=None,
        bars_end=None,
    )

    run(build_node(strategy), "hosted")

    start_sent, end_sent = sent_window(venue)
    now = end_sent + DAY  # the node clock's time when the request was made
    midnight = datetime.combine(now.astimezone(NEW_YORK).date(), datetime.min.time(), NEW_YORK)
    assert start_sent == midnight - DAY
    assert start_sent <= end_sent
    assert strategy.seen_responses == [(strategy.request_id, None)]
    assert [b.ts_event for b in strategy.seen_bars] == opens_received(venue, alpaca)
    assert all(midnight <= unix_nanos_to_dt(b.ts_init) <= now for b in strategy.seen_bars)


WEEKLY_BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-WEEK-LAST-EXTERNAL")


def new_york(*args: int) -> datetime:
    return datetime(*args, tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    ("week_open", "start", "end", "ts_init"),
    [
        # Autumn change, Sunday 2026-11-01: the week's fixed 168 hours end at 23:00 that Sunday.
        (
            new_york(2026, 10, 26),
            new_york(2026, 11, 1, 12),
            new_york(2026, 11, 2, 12),
            new_york(2026, 11, 1, 23),
        ),
        # Spring change, Sunday 2026-03-08: they end at 01:00 on the Monday.
        (
            new_york(2026, 3, 2),
            new_york(2026, 3, 8, 12),
            new_york(2026, 3, 9, 12),
            new_york(2026, 3, 9, 1),
        ),
        # So a window ending at midnight on that Monday misses the spring week's bar.
        (new_york(2026, 3, 2), new_york(2026, 3, 8, 12), new_york(2026, 3, 9), None),
    ],
    ids=["autumn change", "spring change", "spring change, window ends at midnight"],
)
def test_weekly_bar_across_a_clock_change(venue, alpaca, week_open, start, end, ts_init) -> None:
    venue.historical.bars = alpaca.bars_opening(*(week_open + w * WEEK for w in (-1, 0, 1)))
    strategy = consumer(
        want_quotes=False,
        want_trades=False,
        bar_type=WEEKLY_BAR_TYPE,
        bars_start=start,
        bars_end=end,
    )

    run(build_node(strategy), "hosted")

    assert strategy.seen_responses == [(strategy.request_id, None)]
    expected = [] if ts_init is None else [dt_to_unix_nanos(ts_init)]
    assert [b.ts_init for b in strategy.seen_bars] == expected


def bars_request(start: datetime = START, end: datetime = END) -> SimpleNamespace:
    """The fields `_request_bars` reads from a `RequestBars`, for calling the hook directly."""
    return SimpleNamespace(
        bar_types=[BAR_TYPE],
        start=start,
        end=end,
        limit=None,
        request_id=UUID4(),
        start_ns=dt_to_unix_nanos(start),
        end_ns=dt_to_unix_nanos(end),
        params=None,
    )


class ObservedLock(asyncio.Lock):
    """An `asyncio.Lock` that counts calls to `acquire`."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def acquire(self) -> Literal[True]:
        self.attempts += 1
        return await super().acquire()


def test_waiting_bar_request_holds_no_worker_thread(venue) -> None:
    venue.historical.gate = threading.Event()

    async def body(client) -> None:
        lock = client._historical_lock = ObservedLock()
        requests = [asyncio.ensure_future(client._request_bars(bars_request())) for _ in range(2)]
        try:
            await wait_until(lambda: venue.historical.entered.is_set() and lock.attempts == 2)
            # The second request now waits for the first. With two worker threads, one held by the
            # first request, a third call must still run.
            await asyncio.wait_for(asyncio.to_thread(int), 1.0)
        finally:
            venue.historical.gate.set()
        await asyncio.gather(*requests)

    run_with_client(body, max_workers=2)

    assert len(venue.historical.requests) == 2
    assert venue.historical.max_active == 1


def test_cancelled_bar_request_keeps_the_next_waiting_until_its_call_returns(venue) -> None:
    venue.historical.gate = threading.Event()

    async def body(client) -> None:
        first = asyncio.ensure_future(client._request_bars(bars_request()))
        try:
            await wait_until(venue.historical.entered.is_set)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # The first call is still inside get_stock_bars, so the lock is still held.
            assert client._historical_lock.locked()
            second = asyncio.ensure_future(client._request_bars(bars_request()))
        finally:
            venue.historical.gate.set()
        await second

    run_with_client(body)

    assert len(venue.historical.requests) == 2
    assert venue.historical.max_active == 1


def test_trade_cancels_and_corrections_are_logged(venue, alpaca, capfd) -> None:
    # Delivered before the trade, so they are handled before the strategy sees it and stops.
    venue.stream.trades = []
    venue.stream.trade_events = [alpaca.trade_cancel, alpaca.trade_correction, alpaca.trade]
    strategy = consumer(want_quotes=False, bar_type=None)

    run(build_node(strategy), "hosted")

    assert len(strategy.seen_trades) == 1
    log = logged(capfd)
    assert "Alpaca trade cancel for AAPL trade 465" in log
    assert "Alpaca trade correction for AAPL trade 52983525033527" in log


def test_one_sided_quotes_are_dropped_and_logged_once_then_counted(venue, alpaca, capfd) -> None:
    one_sided = [lambda: alpaca.quote(bp=0.0, bs=0)] * 12
    venue.stream.quotes = [*one_sided, alpaca.quote]
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy), "hosted")

    (tick,) = strategy.seen_quotes
    assert tick.bid_price == Price.from_str("87.6600")
    log = logged(capfd)
    assert log.count("Dropped one-sided Alpaca quote: AAPL.ALPACA quote at") == 1
    assert "has bid price 0.0" in log
    assert log.count("one-sided Alpaca quotes for AAPL since subscribing") == 2
    assert "Dropped 10 one-sided Alpaca quotes for AAPL since subscribing" in log  # running count
    assert "Dropped 12 one-sided Alpaca quotes for AAPL since subscribing" in log  # on disconnect


def test_one_sided_quote_count_restarts_after_resubscribing(venue, alpaca, capfd) -> None:
    venue.stream.quotes = []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        for _ in range(2):
            client._emit_quote(alpaca.quote(bp=0.0, bs=0))
        await client._unsubscribe_quotes(command(AAPL))
        await client._subscribe_quotes(command(AAPL))
        client._emit_quote(alpaca.quote(bp=0.0, bs=0))

    run_with_client(body)

    log = logged(capfd)
    assert log.count("Dropped one-sided Alpaca quote: AAPL.ALPACA quote at") == 2
    assert "Dropped 1 one-sided Alpaca quotes for AAPL since subscribing" in log


def test_logged_waits_for_lines_still_queued_after_a_run(venue, capfd) -> None:
    # More lines than the writer thread drains before `readouterr` would run, queued after a node run, as
    # the tests above read the log: the last is captured only if `logged` waits for the writer thread.
    run_with_client(lambda client: asyncio.sleep(0))
    probe = Logger("LogProbe")
    for i in range(LOG_PROBE_LINES):
        probe.info(f"probe line {i}")

    assert f"probe line {LOG_PROBE_LINES - 1}" in logged(capfd)


def test_quote_for_a_symbol_not_subscribed_is_dropped(venue, alpaca) -> None:
    async def body(client) -> None:
        client._emit_quote(alpaca.quote())  # as a late message after an unsubscribe would arrive

    cache = run_with_client(body)

    assert cache.quote(AAPL) is None


def test_stream_handlers_do_not_keep_the_client_alive(venue) -> None:
    refs = []

    async def body(client) -> None:
        refs.append(weakref.ref(client))
        await client._subscribe_quotes(command(AAPL))
        await client._subscribe_trades(command(AAPL))

    run_with_client(body)
    while BUILT_NODES:
        BUILT_NODES.pop().dispose()
    gc.collect()

    assert venue.stream.quote_handlers and venue.stream.cancel_handlers  # the stand-in keeps them
    assert refs[0]() is None


def test_old_stream_ending_after_a_reconnect_is_not_reported(venue, alpaca, monkeypatch, capfd):
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)
    old = venue.stream

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(old.running.is_set)
        old.ignores_stop = True
        old_thread = client._stream_thread
        try:
            with pytest.raises(RuntimeError, match="did not stop"):
                await client._disconnect()
            monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: alpaca.Stream())
            await client._connect()
        finally:
            old.release.set()
        await wait_until(lambda: not old_thread.is_alive())
        for _ in range(3):  # let the exit report the old thread posted run on the loop
            await asyncio.sleep(0)

    run_with_client(body)

    assert "Alpaca stream ended while subscribed" not in logged(capfd)


def test_disconnect_clears_state_when_the_stream_thread_does_not_stop(venue, monkeypatch) -> None:
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        venue.stream.ignores_stop = True
        try:
            with pytest.raises(RuntimeError, match="did not stop within 0.05 s"):
                await client._disconnect()
            assert client._stream is None
            assert client._stream_thread is None
            assert client._historical is None
            assert client._quote_symbols == set()
        finally:
            venue.stream.release.set()

    run_with_client(body)

    assert venue.stream.finished.wait(2), "the released stream thread did not finish"


def test_stream_ending_while_subscribed_is_logged(venue, capfd) -> None:
    venue.stream.ends_on_its_own = True
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy), "hosted")

    assert len(strategy.seen_quotes) == 1
    assert "Alpaca stream ended while subscribed" in logged(capfd)


def test_bars_only_run_starts_no_stream_thread(venue) -> None:
    strategy = consumer(want_quotes=False, want_trades=False)

    run(build_node(strategy), "hosted")

    assert len(strategy.seen_bars) == 1
    assert venue.stream.run_threads == []


def test_failed_bars_fetch_becomes_the_failure_reason(venue) -> None:
    venue.historical.error = APIError('{"code": 42210000, "message": "invalid start"}')
    strategy = consumer(want_quotes=False, want_trades=False)

    run(build_node(strategy), "hosted")

    assert strategy.seen_bars == []
    ((request_id, reason),) = strategy.seen_responses
    assert request_id == strategy.request_id
    assert "invalid start" in reason


def test_unsupported_bar_type_becomes_the_failure_reason(venue) -> None:
    strategy = consumer(
        want_quotes=False,
        want_trades=False,
        bar_type=BarType.from_str("AAPL.ALPACA-1-MINUTE-BID-EXTERNAL"),
    )

    run(build_node(strategy), "hosted")

    ((_, reason),) = strategy.seen_responses
    assert "LAST" in reason
    assert venue.historical.requests == []


def test_missing_credentials_fail_the_connect_and_build_no_client(monkeypatch, capfd) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_SECRET_KEY, raising=False)
    built = []
    for module, name in [
        (canon_alpaca.providers, "TradingClient"),
        (canon_alpaca.data, "StockHistoricalDataClient"),
        (canon_alpaca.data, "StockDataStream"),
    ]:
        monkeypatch.setattr(module, name, lambda name=name, **_: built.append(name))
    node = build_node(Consumer(), timeout_connection_secs=1)

    with pytest.raises(RuntimeError, match="readiness timeout"):
        run(node, "hosted")

    assert built == []
    assert f"set {ENV_API_KEY} and {ENV_SECRET_KEY}" in logged(capfd)
