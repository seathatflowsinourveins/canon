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
import concurrent.futures
import contextlib
import functools
import gc
import json
import logging
import re
import threading
import weakref
from collections.abc import Awaitable
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from typing import cast
from zoneinfo import ZoneInfo

import canon_alpaca.data
import canon_alpaca.factories
import canon_alpaca.providers
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockTradesRequest
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.client import TradingClient
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import VENUE
from canon_alpaca.data import STAMP_AHEAD_MOST_NS
from canon_alpaca.data import AlpacaDataClient
from canon_alpaca.data import AlpacaDataClientConfig
from canon_alpaca.data import TradeSequence
from canon_alpaca.data import late_ms
from canon_alpaca.data import late_prints_set_aside
from canon_alpaca.data import trade_stamp
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.trade_conditions import CONDITIONS_PATH
from canon_alpaca.trade_conditions import TABLE
from canon_alpaca.trade_conditions import condition_counts
from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
from nautilus_trader.common import Clock
from nautilus_trader.common import Environment
from nautilus_trader.common import Logger
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core import UUID4
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live import InstrumentResponse
from nautilus_trader.live import LiveNode
from nautilus_trader.live import TradesResponse
from nautilus_trader.model import AccountType
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import BarType
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import MarketStatusAction
from nautilus_trader.model import Money
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OmsType
from nautilus_trader.model import Price
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick
from nautilus_trader.persistence import DataCatalogConfig
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.trading import Strategy
from requests.adapters import HTTPAdapter

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
        self.subscribe_params: dict | None = None
        self.bars_params: dict | None = None
        self.auto_shutdown = True
        self.seen_quotes: list = []
        self.seen_trades: list = []
        self.seen_bars: list = []
        self.seen_responses: list = []
        self.request_id: str | None = None
        self.finished = False

    def on_start(self) -> None:
        if self.want_quotes:
            self.subscribe_quotes(AAPL, params=self.subscribe_params)
        if self.want_trades:
            self.subscribe_trades(AAPL, params=self.subscribe_params)
        if self.bar_type is not None:
            for _ in range(self.bar_requests):
                self.request_id = self.request_bars(
                    self.bar_type,
                    start=self.bars_start,
                    end=self.bars_end,
                    limit=self.bars_limit,
                    params=self.bars_params,
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
    catalog: Path | None = None,
    config: DataClientConfig | None = None,
):
    if config is None:
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
                catalogs=[] if catalog is None else [DataCatalogConfig(path=str(catalog))],
            ),
            data_factories={"ALPACA": factory},
        )
    else:
        assert catalog is None, "the builder path is not used with a catalog here"
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
    executor: ThreadPoolExecutor | None = None,
    config: DataClientConfig | None = None,
):
    """Run a hosted node, await `body` with its connected Alpaca client, stop the node, return its cache.

    With `max_workers`, the loop's default executor, which `asyncio.to_thread` uses, has that many threads;
    with `executor`, it is that one. With `config`, the data client is built from it. The cache is taken
    before the run, as NautilusTrader requires for a node run by `run_async`.
    """
    clients: list[AlpacaDataClient] = []
    strategy = strategy or consumer(
        want_quotes=False, want_trades=False, bar_type=None, auto_shutdown=False
    )
    # Held in a list that is emptied on the way out, so a failure's traceback does not keep the node.
    nodes = [build_node(strategy, factory=capturing_factory(clients), config=config)]
    handle = nodes[0].handle()
    cache = nodes[0].cache

    async def main() -> None:
        if max_workers is not None:
            executor_used = ThreadPoolExecutor(max_workers)
        else:
            executor_used = executor
        if executor_used is not None:
            asyncio.get_running_loop().set_default_executor(executor_used)
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


def command(*instrument_ids: InstrumentId, params: dict | None = None) -> SimpleNamespace:
    """The fields the subscription hooks read from a command."""
    return SimpleNamespace(instrument_ids=list(instrument_ids), params=params)


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
        "data_timeout": canon_alpaca.data.STREAM_DATA_TIMEOUT_SECS,
    }


MSFT = InstrumentId.from_str("MSFT.ALPACA")


class InstrumentRequester(Strategy):
    """Request a name outside the startup roster and stop after the routed response."""

    def __init__(self) -> None:
        super().__init__()
        self.request_id: str | None = None
        self.responses: list[tuple[str, str | None]] = []

    def on_start(self) -> None:
        self.request_id = self.request_instrument(MSFT)

    def on_data_response(self, response) -> None:
        self.responses.append((str(response.correlation_id), response.failure_reason))
        self.shutdown_system("Alpaca instrument request checked")


def test_instrument_request_reaches_a_name_outside_the_startup_roster(venue, alpaca) -> None:
    venue.trading.assets.append({**alpaca.ASSET_AAPL, "symbol": "MSFT"})
    strategy = InstrumentRequester()
    node = build_node(strategy)
    assert node.cache.instrument(MSFT) is None

    run(node, "hosted")

    assert strategy.responses == [(strategy.request_id, None)]
    assert isinstance(node.cache.instrument(MSFT), Equity)
    assert venue.trading.symbol_requests == ["AAPL", "MSFT"]
    assert venue.trading.all_requests == []


def test_on_demand_asset_gets_enter_the_existing_trading_request_tally(venue, alpaca, monkeypatch):
    # The real alpaca-py GET and TimeoutAdapter run; only requests' final transport is answered here.
    counter = canon_alpaca.providers.RequestCounter()
    monkeypatch.setattr(canon_alpaca.providers, "REQUESTS", counter)
    assets = TradingClient(api_key=PLACEHOLDER, secret_key=PLACEHOLDER, paper=True)
    monkeypatch.setattr(canon_alpaca.providers, "TradingClient", lambda **kwargs: assets)
    sent: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        symbol = request.url.rsplit("/", 1)[-1]
        sent.append((request.method, symbol))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({**alpaca.ASSET_AAPL, "symbol": symbol}).encode()
        response.request, response.url = request, request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    strategy = InstrumentRequester()
    run(build_node(strategy), "hosted")

    assert strategy.responses == [(strategy.request_id, None)]
    assert sent == [("GET", "AAPL"), ("GET", "MSFT")]
    (count,) = canon_alpaca.providers.request_counts()
    assert count.api == canon_alpaca.providers.TRADING_API and count.total == count.peak_60s == 2


def instrument_request(instrument_id: InstrumentId = MSFT) -> SimpleNamespace:
    """The documented single-instrument request fields, including response metadata."""
    return SimpleNamespace(
        instrument_id=instrument_id,
        request_id=UUID4(),
        start_ns=dt_to_unix_nanos(START),
        end_ns=dt_to_unix_nanos(END),
        params={"probe": "instrument-response"},
    )


def test_instrument_request_publishes_before_answering_and_keeps_query_metadata(
    venue, alpaca, monkeypatch
) -> None:
    venue.trading.assets.append({**alpaca.ASSET_AAPL, "symbol": "MSFT"})
    request = instrument_request()
    delivered: list[tuple[str, object]] = []

    async def body(client) -> None:
        monkeypatch.setattr(
            client,
            "_handle_instrument",
            lambda instrument: delivered.append(("instrument", instrument)),
        )
        monkeypatch.setattr(
            client, "_handle_response", lambda response: delivered.append(("response", response))
        )
        await client._request_instrument(request)
        await client._request_instrument(request)  # the provider keeps a definition already loaded

    run_with_client(body)

    assert [kind for kind, _ in delivered] == ["instrument", "response"] * 2
    instrument = delivered[0][1]
    response = delivered[1][1]
    assert isinstance(instrument, Equity)
    assert isinstance(response, InstrumentResponse)
    assert response.data == instrument
    assert response.instrument_id == MSFT
    assert str(response.client_id) == "ALPACA"
    assert response.correlation_id == request.request_id
    assert (response.start, response.end, response.params) == (
        request.start_ns,
        request.end_ns,
        request.params,
    )
    assert venue.trading.symbol_requests == ["AAPL", "MSFT"]


@pytest.mark.parametrize("failure", ["unknown", "inactive", "transport", "foreign"])
def test_instrument_request_refusal_names_the_symbol_without_raising(
    venue, alpaca, monkeypatch, failure
) -> None:
    instrument_id = InstrumentId.from_str("MSFT.OTHER") if failure == "foreign" else MSFT
    request = instrument_request(instrument_id)
    if failure == "inactive":
        venue.trading.assets.append({**alpaca.ASSET_AAPL, "symbol": "MSFT", "status": "inactive"})
    elif failure == "transport":
        venue.trading.errors["MSFT"] = OSError("offline asset transport failed")
    failures: list[tuple[UUID4, str]] = []

    async def body(client) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(
                client,
                "_output",
                SimpleNamespace(request_failed=lambda *args: failures.append(args)),
            )
            await client._request_instrument(request)
        assert client._provider.find(instrument_id) is None

    cache = run_with_client(body)

    assert len(failures) == 1
    assert failures[0][0] == request.request_id
    assert str(instrument_id) in failures[0][1]
    assert cache.instrument(instrument_id) is None
    assert venue.trading.symbol_requests == (["AAPL"] if failure == "foreign" else ["AAPL", "MSFT"])


@pytest.mark.parametrize("failure", ["unknown", "inactive", "transport"])
def test_instrument_request_refusal_reaches_the_requester(venue, alpaca, failure) -> None:
    if failure == "inactive":
        venue.trading.assets.append({**alpaca.ASSET_AAPL, "symbol": "MSFT", "status": "inactive"})
    elif failure == "transport":
        venue.trading.errors["MSFT"] = OSError("offline asset transport failed")
    strategy = InstrumentRequester()
    node = build_node(strategy)

    run(node, "hosted")

    ((request_id, reason),) = strategy.responses
    assert request_id == strategy.request_id
    assert reason is not None and str(MSFT) in reason
    assert node.cache.instrument(MSFT) is None


def test_shutdown_stops_and_joins_the_stream_thread(venue) -> None:
    run(build_node(Consumer()), "hosted")

    (name,) = venue.stream.run_threads
    assert re.fullmatch(r"ALPACA-stream-\d+", name)
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
        # Three bars close inside the window; `limit` keeps the earliest two.
        (START, START + 2 * HOUR, 2, [START, START + HOUR]),
    ],
    ids=["no limit", "limit cuts the window"],
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
        # From Monday's open, midnight New York, to the evening: the window holds the open, but Monday's
        # bar is stamped at Tuesday midnight, so nothing. Alpaca is asked for Sunday, which has no bar.
        (MON + NY_MIDNIGHT, MON + timedelta(hours=21), []),
        # Monday noon to Tuesday noon New York: Monday's bar.
        (MON + timedelta(hours=17), MON + timedelta(days=1, hours=17), [MON + NY_MIDNIGHT]),
    ],
    ids=["friday to monday", "inside monday's session", "from monday's open", "noon to noon"],
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
    # NautilusTrader drops a bar whose `ts_init` is outside the request, so an empty answer alone would
    # also pass with no shift; the window sent shows the shift.
    assert sent_window(venue) == (start - DAY, end - DAY)
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
    assert start_sent == midnight.astimezone(UTC) - DAY  # 24 elapsed hours, as the client moves it
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
            # While the first call is held at the gate, the second must not reach the venue.
            await asyncio.sleep(0.2)
            assert len(venue.historical.requests) == 1
            assert not second.done()
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
            client._emit_quote(client._quote_symbols["AAPL"], alpaca.quote(bp=0.0, bs=0))
        await client._unsubscribe_quotes(command(AAPL))
        await client._subscribe_quotes(command(AAPL))
        client._emit_quote(client._quote_symbols["AAPL"], alpaca.quote(bp=0.0, bs=0))

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
    venue.stream.quotes = []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        handler = venue.stream.quote_handlers["AAPL"]
        await client._unsubscribe_quotes(command(AAPL))
        assert client._stream is not None  # the stream still runs; only the symbol is gone
        # As a late message after an unsubscribe would arrive, on the stream's thread.
        await asyncio.to_thread(asyncio.run, handler(alpaca.quote()))
        for _ in range(5):
            await asyncio.sleep(0.01)

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

    assert "ended by itself while subscribed" not in logged(capfd)


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
            assert client._quote_symbols == {}
        finally:
            venue.stream.release.set()

    run_with_client(body)

    assert venue.stream.finished.wait(2), "the released stream thread did not finish"


def test_stream_ending_while_subscribed_is_logged(venue, capfd) -> None:
    # The node stops only after the stream's thread has ended and its quote has reached the strategy. Its
    # exit report reaches the loop after its one quote, so a strategy stopping the node on that quote
    # could disconnect first, and the client drops a report from a stream it no longer runs (`_current`);
    # and the quote passes through the data engine's queue, so a stop as the thread ends could beat it.
    # Both failed under load.
    venue.stream.ends_on_its_own = True
    strategy = consumer(want_trades=False, bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(
            lambda: (
                client._stream_thread is not None
                and not client._stream_thread.is_alive()
                and strategy.seen_quotes
            )
        )
        await asyncio.sleep(0)  # the report, posted before the thread ended, runs on this loop

    run_with_client(body, strategy)

    assert len(strategy.seen_quotes) == 1
    assert "ended by itself while subscribed" in logged(capfd)


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


def test_a_cancelled_bar_call_whose_worker_fails_later_leaves_no_unretrieved_error(venue) -> None:
    # As in the execution client: the shielded call goes on after its hook is cancelled, and its later
    # error must not reach the loop as "never retrieved".
    import gc
    import threading

    messages: list[str] = []

    async def body(client) -> None:
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: messages.append(str(context.get("message"))))
        release = threading.Event()

        def fails_later(request) -> None:
            release.wait(5)
            raise OSError("read timed out")

        try:
            waiting = asyncio.ensure_future(client._one_historical_call(fails_later, None))
            await asyncio.sleep(0.05)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            release.set()
            await wait_until(lambda: not client._historical_lock.locked())
            await asyncio.sleep(0.05)
            gc.collect()
            await asyncio.sleep(0.05)
        finally:
            loop.set_exception_handler(handler)

    run_with_client(body)

    assert not [m for m in messages if "never retrieved" in m], messages


def test_subscription_after_the_stream_ended_by_itself_is_refused(venue) -> None:
    # The client does not restart a stream that ended by itself (README, "No restart"): a later
    # subscription must fail, naming that, rather than be recorded on a stream that is not running.
    venue.stream.ends_on_its_own = True

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(
            lambda: client._stream_thread is not None and not client._stream_thread.is_alive()
        )
        with pytest.raises(RuntimeError, match="ended by itself"):
            await client._subscribe_trades(command(AAPL))
        assert client._trade_symbols == {}

    run_with_client(body)

    assert len(venue.stream.run_threads) == 1


def test_a_client_given_at_construction_is_kept_across_a_reconnect(venue, monkeypatch) -> None:
    import functools

    from canon_alpaca.data import AlpacaDataClient

    def refuse(**kwargs: object) -> object:
        raise AssertionError("built from the environment in place of the given client")

    monkeypatch.setattr(canon_alpaca.data, "StockHistoricalDataClient", refuse)
    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaDataClient",
        functools.partial(AlpacaDataClient, historical_client=venue.historical),
    )
    seen: list = []

    async def body(client) -> None:
        await client._disconnect()
        seen.append(client._historical)
        await client._connect()
        seen.append(client._historical)

    run_with_client(body)

    assert seen == [None, venue.historical]


def test_a_stream_given_at_construction_serves_one_connect(venue, monkeypatch) -> None:
    import functools

    from canon_alpaca.data import AlpacaDataClient

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaDataClient",
        functools.partial(AlpacaDataClient, stream=venue.stream),
    )

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))  # the stream runs
        await client._disconnect()
        with pytest.raises(RuntimeError, match="serves one connect"):
            await client._connect()

    run_with_client(body)


# -- the data client's defects from the 2026-11-10 reviewer benchmark triage -----------------------------


@pytest.mark.parametrize(
    ("end", "now", "interval", "window"),
    [
        # No start, and an end before today: the window is the end's day so far, not today's.
        (
            datetime(2024, 12, 31, 23, tzinfo=UTC),
            datetime(2026, 11, 10, 17, tzinfo=UTC),
            HOUR,
            (datetime(2024, 12, 31, 4, tzinfo=UTC), datetime(2024, 12, 31, 22, tzinfo=UTC)),
        ),
        # A week back from midnight 2026-11-02 EST is 168 hours, which lands at 01:00 EDT on 10-26.
        (
            None,
            datetime(2026, 11, 2, 17, tzinfo=UTC),
            WEEK,
            (datetime(2026, 10, 26, 5, tzinfo=UTC), datetime(2026, 10, 26, 17, tzinfo=UTC)),
        ),
        # Half an hour into the Monday after the spring change: a day back from midnight EDT is 23:00
        # EST on the Saturday, so the window is half an hour long, not inverted.
        (
            None,
            datetime(2026, 3, 9, 4, 30, tzinfo=UTC),
            DAY,
            (datetime(2026, 3, 8, 4, tzinfo=UTC), datetime(2026, 3, 8, 4, 30, tzinfo=UTC)),
        ),
    ],
    ids=["end before today", "week across the autumn change", "day after the spring change"],
)
def test_bars_window_with_no_start_is_the_end_days_window_in_elapsed_time(
    end, now, interval, window
):
    sent = canon_alpaca.data.alpaca_bars_window(None, end, now, interval)

    assert sent is not None
    start_sent, end_sent = sent
    assert sent == window
    assert start_sent <= end_sent


def test_bar_request_with_no_start_and_a_past_end_returns_that_days_bars(venue, alpaca) -> None:
    venue.historical.bars = alpaca.hourly_bars(START - 2 * HOUR, 6)  # opens 07:00 to 12:00 UTC
    strategy = consumer(want_quotes=False, want_trades=False, bars_start=None, bars_end=END)

    run(build_node(strategy), "hosted")

    assert strategy.seen_responses == [(strategy.request_id, None)]
    # From midnight New York (05:00 UTC) to END, 10:00 UTC: the bars that closed at 08:00, 09:00, 10:00.
    assert [b.ts_init for b in strategy.seen_bars] == [
        dt_to_unix_nanos(START - HOUR + h * HOUR) for h in range(3)
    ]


def test_old_streams_messages_after_a_reconnect_are_dropped(venue, alpaca, monkeypatch, capfd):
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)
    old = venue.stream
    old.quotes, old.trades = [], []  # the old stream delivers only what the test hands its handlers
    new = alpaca.Stream()
    new.quotes, new.trades = [], []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await client._subscribe_trades(command(AAPL))
        await wait_until(old.running.is_set)
        old.ignores_stop = True
        handlers = [
            (old.quote_handlers["AAPL"], alpaca.quote()),
            (old.trade_handlers["AAPL"], alpaca.trade()),
            (old.cancel_handlers["*"], alpaca.trade_cancel()),
        ]
        try:
            with pytest.raises(RuntimeError, match="did not stop"):
                await client._disconnect()
            monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: new)
            await client._connect()
            await client._subscribe_quotes(command(AAPL))
            await client._subscribe_trades(command(AAPL))
            for handler, message in handlers:  # late messages on the old stream's thread
                await asyncio.to_thread(asyncio.run, handler(message))
            for _ in range(5):
                await asyncio.sleep(0.01)
        finally:
            old.release.set()

    cache = run_with_client(body)

    assert cache.quote(AAPL) is None
    assert cache.trade(AAPL) is None
    assert "Alpaca trade cancel" not in logged(capfd)


class RefusingExecutor(ThreadPoolExecutor):
    """A default executor that, once told to, refuses a bar call's work as a shut-down executor does.

    Only the call `asyncio.to_thread` and the client submit, `context.run(release_after, ...)`, is
    refused, so the node's other work goes on."""

    refuse = False

    def submit(self, fn, /, *args, **kwargs):
        bar_call = getattr(fn, "args", ())[:1] == (canon_alpaca.data.release_after,)
        if self.refuse and bar_call:
            raise RuntimeError("cannot schedule new futures after shutdown")
        return super().submit(fn, *args, **kwargs)


def test_a_bar_call_the_executor_refuses_releases_the_request_lock(venue) -> None:
    executor = RefusingExecutor()

    async def body(client) -> None:
        executor.refuse = True
        try:
            with pytest.raises(RuntimeError, match="cannot schedule"):
                await client._request_bars(bars_request())
        finally:
            executor.refuse = False
        assert not client._historical_lock.locked()
        await asyncio.wait_for(client._request_bars(bars_request()), 2)  # the next is served

    run_with_client(body, executor=executor)

    assert len(venue.historical.requests) == 1


def test_the_stream_thread_holds_no_strong_reference_to_the_client_inside_a_handler(
    venue, alpaca, monkeypatch
) -> None:
    from alpaca.data.models import Quote

    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)
    inside, release = threading.Event(), threading.Event()
    real_post = canon_alpaca.data.post

    def post_held_on_the_stream_thread(loop, callback, item) -> None:
        if isinstance(item, Quote) and threading.current_thread().name in venue.stream.run_threads:
            inside.set()
            release.wait(5)  # the stream thread is preempted here, inside the handler
        real_post(loop, callback, item)

    monkeypatch.setattr(canon_alpaca.data, "post", post_held_on_the_stream_thread)
    refs = []

    async def body(client) -> None:
        refs.append(weakref.ref(client))
        await client._subscribe_quotes(command(AAPL))
        await wait_until(inside.is_set)
        with pytest.raises(RuntimeError, match="did not stop"):
            await client._disconnect()

    try:
        run_with_client(body)
        while BUILT_NODES:
            BUILT_NODES.pop().dispose()
        gc.collect()
        assert refs[0]() is None
    finally:
        release.set()
        assert venue.stream.finished.wait(2)


def test_a_disconnect_cancelled_by_the_node_still_clears_the_clients_state(venue, capfd) -> None:
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        client._one_sided_quotes["AAPL"] = 3
        venue.stream.ignores_stop = True
        try:
            # As a node whose disconnection timeout is shorter than STREAM_JOIN_TIMEOUT_SECS cancels it.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client._disconnect(), 0.2)
            assert client._stream is None
            assert client._stream_thread is None
            assert client._historical is None
            assert client._quote_symbols == {}
            assert client._one_sided_quotes == {}
        finally:
            venue.stream.release.set()

    run_with_client(body)

    assert "Dropped 3 one-sided Alpaca quotes for AAPL since subscribing" in logged(capfd)
    assert venue.stream.finished.wait(2)


def test_tick_bar_request_is_refused_with_the_adapters_reason(venue) -> None:
    strategy = consumer(
        want_quotes=False,
        want_trades=False,
        bar_type=BarType.from_str("AAPL.ALPACA-100-TICK-LAST-EXTERNAL"),
    )

    run(build_node(strategy), "hosted")

    ((_, reason),) = strategy.seen_responses
    assert "MINUTE, HOUR, DAY or WEEK" in reason
    assert venue.historical.requests == []


def test_a_quote_with_a_nan_price_is_dropped_as_an_error_not_counted_one_sided(
    venue, alpaca, capfd
) -> None:
    venue.stream.quotes = [
        lambda: alpaca.quote(bp=float("nan")),
        lambda: alpaca.quote(
            bp=0.0, ap=float("nan")
        ),  # the other side NaN: malformed, not one-sided
        alpaca.quote,
    ]
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy), "hosted")

    (tick,) = strategy.seen_quotes
    assert tick.bid_price == Price.from_str("87.6600")
    log = logged(capfd)
    assert "one-sided" not in log
    assert "Dropped Alpaca quote: AAPL.ALPACA bid price nan is not a finite number" in log
    assert "Dropped Alpaca quote: AAPL.ALPACA ask price nan is not a finite number" in log


def test_resubscribing_delivers_again(venue) -> None:
    # The strategy's own subscription stays in the engine, so each quote the client emits reaches it.
    strategy = consumer(want_trades=False, bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(lambda: len(strategy.seen_quotes) == 1)
        await client._unsubscribe_quotes(command(AAPL))
        await client._subscribe_quotes(command(AAPL))
        await wait_until(lambda: len(strategy.seen_quotes) == 2)

    run_with_client(body, strategy=strategy)


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_a_running_stream_connects_only_once_something_is_subscribed(make, alpaca) -> None:
    # alpaca-py 0.45.0's `_drive_run` waits for a market subscription before it connects, and sets
    # `_running`, which gates every send, only after connecting. Nothing is subscribed here, so the
    # real stream opens no connection.
    from alpaca.data.live.stock import StockDataStream

    if make == "real":
        stream = StockDataStream(api_key=PLACEHOLDER, secret_key=PLACEHOLDER)
        connected = lambda: stream._running
    else:
        stream = alpaca.Stream()
        connected = stream.running.is_set
    thread = threading.Thread(target=stream.run, daemon=True)
    thread.start()
    try:
        thread.join(0.2)
        assert thread.is_alive()
        assert not connected()
    finally:
        stream.stop()
        thread.join(2)
    assert not thread.is_alive()


# -- three claims from the second readers of #99 and #107 ------------------------------------------------


def test_a_repeat_subscription_after_the_stream_ended_by_itself_is_refused(venue) -> None:
    venue.stream.ends_on_its_own = True

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(
            lambda: client._stream_thread is not None and not client._stream_thread.is_alive()
        )
        with pytest.raises(RuntimeError, match="ended by itself"):
            await client._subscribe_quotes(command(AAPL))  # already in the client's symbols

    run_with_client(body)


def test_concurrent_unsubscribes_of_one_symbol_send_one(venue) -> None:
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        await asyncio.gather(
            client._unsubscribe_quotes(command(AAPL)), client._unsubscribe_quotes(command(AAPL))
        )

    run_with_client(body)

    assert venue.stream.unsubscribed == [("quotes", ("AAPL",))]


def test_a_reconnect_waits_for_a_bar_call_a_cancelled_request_left_running(
    venue, monkeypatch
) -> None:
    import functools

    from canon_alpaca.data import AlpacaDataClient

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaDataClient",
        functools.partial(AlpacaDataClient, historical_client=venue.historical),
    )
    venue.historical.gate = threading.Event()

    async def body(client) -> None:
        first = asyncio.ensure_future(client._request_bars(bars_request()))
        second = None
        try:
            await wait_until(venue.historical.entered.is_set)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await client._disconnect()
            await client._connect()
            second = asyncio.ensure_future(client._request_bars(bars_request()))
            await asyncio.sleep(0.2)
            # The given client's one session is still inside the first call.
            assert len(venue.historical.requests) == 1
        finally:
            venue.historical.gate.set()
        assert second is not None
        await second

    run_with_client(body)

    assert venue.historical.max_active == 1


def test_a_given_stream_that_never_ran_serves_the_next_connect(venue, monkeypatch) -> None:
    import functools

    from canon_alpaca.data import AlpacaDataClient

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaDataClient",
        functools.partial(AlpacaDataClient, stream=venue.stream),
    )

    async def body(client) -> None:
        await client._disconnect()  # nothing was subscribed, so the stream never ran
        await client._connect()
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)

    run_with_client(body)


# -- a feed named in params (ADR 0055) -----------------------------------------------------------------


def test_a_named_feed_reaches_the_stream_and_the_bars_request(venue) -> None:
    strategy = consumer(subscribe_params={"feed": "boats"}, bars_params={"feed": "boats"})

    run(build_node(strategy), "hosted")

    assert venue.built["stream"]["feed"] == DataFeed.BOATS
    (request,) = venue.historical.requests
    assert request.feed == DataFeed.BOATS
    assert len(strategy.seen_quotes) == 1
    assert len(strategy.seen_trades) == 1
    assert strategy.seen_responses == [(strategy.request_id, None)]


def test_other_params_keys_are_left_to_the_engine_and_the_feed_stays_iex(venue) -> None:
    strategy = consumer(subscribe_params={"time_zone": "UTC"}, bars_params={"update": 1})

    run(build_node(strategy), "hosted")

    assert venue.built["stream"]["feed"] == DataFeed.IEX
    (request,) = venue.historical.requests
    assert request.feed == DataFeed.IEX


def test_a_subscription_on_another_feed_than_the_stream_is_refused(venue, capfd) -> None:
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL, params={"feed": "boats"}))
        with pytest.raises(
            ValueError, match=r"one stream, on feed boats .* asks for iex \(named none"
        ):
            await client._subscribe_trades(command(AAPL))
        with pytest.raises(ValueError, match="one stream, on feed boats .* asks for sip"):
            await client._subscribe_trades(command(AAPL, params={"feed": "sip"}))
        with pytest.raises(ValueError, match="asks for overnight"):  # already subscribed, on boats
            await client._subscribe_quotes(command(AAPL, params={"feed": "overnight"}))
        assert client._trade_symbols == {}
        await client._subscribe_trades(command(AAPL, params={"feed": "boats"}))
        assert list(client._trade_symbols) == ["AAPL"]

    run_with_client(body)

    assert list(venue.stream.trade_handlers) == ["AAPL"]
    assert venue.built["stream"]["feed"] == DataFeed.BOATS
    assert "Cannot subscribe: this client runs one stream, on feed boats" in logged(capfd)


@pytest.mark.parametrize(
    ("feed", "error", "match"),
    [
        ("otc", ValueError, "subscriptions take feed iex, sip, delayed_sip, boats, overnight"),
        ("BOATS", ValueError, "params named 'BOATS'"),
        (1, TypeError, "must name an Alpaca feed"),
        (None, TypeError, "must name an Alpaca feed .* got None"),
    ],
)
def test_a_subscription_naming_a_feed_alpaca_does_not_stream_sends_nothing(
    venue, feed, error, match
) -> None:
    async def body(client) -> None:
        with pytest.raises(error, match=match):
            await client._subscribe_quotes(command(AAPL, params={"feed": feed}))
        assert client._stream is None

    run_with_client(body)

    assert "stream" not in venue.built
    assert venue.stream.run_threads == []


def test_a_bars_request_naming_a_feed_the_bars_endpoint_lacks_is_refused(venue) -> None:
    strategy = consumer(want_quotes=False, want_trades=False, bars_params={"feed": "overnight"})

    run(build_node(strategy), "hosted")

    ((_, reason),) = strategy.seen_responses
    assert "bar requests take feed iex, otc, sip, boats" in reason
    assert venue.historical.requests == []


def test_a_feed_alpaca_refuses_fails_loudly_naming_it(venue, capfd) -> None:
    # As alpaca-py 0.45.0 does on 409 "insufficient subscription" at authentication: it logs the
    # error on its stream's thread and the run returns.
    venue.stream.refused = "insufficient subscription"

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL, params={"feed": "sip"}))
        await wait_until(
            lambda: client._stream_thread is not None and not client._stream_thread.is_alive()
        )
        for _ in range(5):
            await asyncio.sleep(0.01)  # the posted log line and exit report
        with pytest.raises(RuntimeError, match="feed sip ended by itself"):
            await client._subscribe_trades(command(AAPL, params={"feed": "sip"}))

    cache = run_with_client(body)

    assert cache.quote(AAPL) is None
    log = logged(capfd)
    assert re.search(
        r"Alpaca stream on feed sip \(ALPACA-stream-\d+\): error during websocket "
        "communication: insufficient subscription",
        log,
    )
    assert "Alpaca stream on feed sip ended by itself while subscribed" in log


# -- the second reader of the feed change ----------------------------------------------------------------


def test_the_relay_reads_alpaca_pys_own_stream_logger() -> None:
    import alpaca.data.live.websocket

    assert alpaca.data.live.websocket.log.name == canon_alpaca.data.ALPACA_STREAM_LOGGER


def alpaca_log_on_thread(name: str, message: str, *args: object) -> None:
    """Log a warning on alpaca-py's stream logger from a thread called `name`, as its stream would."""
    logger = logging.getLogger(canon_alpaca.data.ALPACA_STREAM_LOGGER)
    thread = threading.Thread(target=logger.warning, args=(message, *args), name=name)
    thread.start()
    thread.join(2)


def test_an_old_stream_threads_log_lines_are_not_taken_for_the_new_streams(
    venue, alpaca, monkeypatch, capfd
) -> None:
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)
    old, new = venue.stream, alpaca.Stream()

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(old.running.is_set)
        old.ignores_stop = True
        old_name = client._stream_thread.name
        try:
            with pytest.raises(RuntimeError, match="did not stop"):
                await client._disconnect()
            monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: new)
            await client._connect()
            await client._subscribe_quotes(command(AAPL, params={"feed": "boats"}))
            assert client._stream_thread.name != old_name
            alpaca_log_on_thread(old_name, "data websocket error, restarting connection: old")
            alpaca_log_on_thread(client._stream_thread.name, "websocket error: new")
            for _ in range(5):
                await asyncio.sleep(0.01)
        finally:
            old.release.set()

    run_with_client(body)

    log = logged(capfd)
    assert re.search(
        r"Alpaca stream on feed boats \(ALPACA-stream-\d+\): websocket error: new", log
    )
    assert "restarting connection: old" not in log


def test_a_log_record_that_cannot_be_formatted_is_not_raised_into_the_stream(
    venue, capfd, monkeypatch
) -> None:
    # Kept from pytest's own capture handler at the root, which fails a test on a bad record.
    logger = logging.getLogger(canon_alpaca.data.ALPACA_STREAM_LOGGER)
    monkeypatch.setattr(logger, "propagate", False)

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        alpaca_log_on_thread(client._stream_thread.name, "%s and %s", "one")  # one argument short
        alpaca_log_on_thread(client._stream_thread.name, "after it")
        for _ in range(5):
            await asyncio.sleep(0.01)

    run_with_client(body)

    assert re.search(r"Alpaca stream on feed iex \(ALPACA-stream-\d+\): after it", logged(capfd))


def test_a_stream_thread_that_cannot_start_leaves_nothing_recorded(venue, monkeypatch) -> None:
    real_start = threading.Thread.start
    refused = []

    def start(self) -> None:
        if "-stream-" in self.name and not refused:
            refused.append(self.name)
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    logger = logging.getLogger(canon_alpaca.data.ALPACA_STREAM_LOGGER)

    async def body(client) -> None:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            await client._subscribe_quotes(command(AAPL))
        assert client._stream_thread is None
        assert not [h for h in logger.handlers if isinstance(h, canon_alpaca.data.StreamLogRelay)]
        await client._subscribe_quotes(command(AAPL))  # not "ended by itself"
        await wait_until(venue.stream.running.is_set)

    run_with_client(body)


def test_a_subscription_the_client_cannot_serve_after_the_stream_ended_is_only_logged(venue, capfd):
    venue.stream.ends_on_its_own = True

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(
            lambda: client._stream_thread is not None and not client._stream_thread.is_alive()
        )
        await client._subscribe_quotes(command(InstrumentId.from_str("MSFT.ALPACA")))

    run_with_client(body)

    assert "Cannot subscribe: MSFT.ALPACA is not a loaded ALPACA instrument" in logged(capfd)


def test_a_subscribe_stuck_in_one_connect_does_not_hold_up_the_next(venue) -> None:
    async def body(client) -> None:
        await client._stream_calls.acquire()  # as a subscribe whose alpaca-py call never returns
        await client._disconnect()
        await client._connect()
        await asyncio.wait_for(client._subscribe_quotes(command(AAPL)), 2)

    run_with_client(body)


def test_the_engine_passes_on_only_the_first_subscription_to_an_instrument(venue, monkeypatch):
    # ADR 0055's stated limit: a second strategy naming another feed for the same instrument's quotes is
    # never seen by the client, so it cannot be refused there. Fails if NautilusTrader starts passing it.
    calls: list = []
    original = AlpacaDataClient._subscribe_quotes

    async def recorded(self, command) -> None:
        calls.append(dict(command.params or {}).get("feed"))
        await original(self, command)

    monkeypatch.setattr(AlpacaDataClient, "_subscribe_quotes", recorded)
    first = consumer(want_trades=False, bar_type=None, auto_shutdown=False)
    first.subscribe_params = {"feed": "boats"}
    second = consumer(want_trades=False, bar_type=None, subscribe_params={"feed": "sip"})
    node = build_node(first)
    node.add_strategy(second)

    run(node, "hosted")

    assert calls == ["boats"]
    assert venue.built["stream"]["feed"] == DataFeed.BOATS


# -- the second readers of #113 and #122 ----------------------------------------------------------------


def stream_call_name(fn: object) -> str:
    """The alpaca-py stream method a submitted worker call makes first, through the partials around it
    and `call_each`, which makes one call per channel."""
    target = (getattr(fn, "args", ()) or (None,))[0]
    if isinstance(target, functools.partial) and target.func is canon_alpaca.data.call_each:
        target = target.args[0][0]
    target = getattr(target, "func", target)
    return getattr(target, "__name__", "")


class RefusingStreamExecutor(ThreadPoolExecutor):
    """A default executor that, once told to, refuses a stream subscribe or unsubscribe call, as a
    shut-down executor does, and runs the node's other work."""

    refuse = False

    def submit(self, fn, /, *args, **kwargs):
        if self.refuse and stream_call_name(fn).startswith(("subscribe_", "unsubscribe_")):
            raise RuntimeError("cannot schedule new futures after shutdown")
        return super().submit(fn, *args, **kwargs)


def test_a_cancelled_unsubscribe_keeps_the_next_stream_call_out_until_alpaca_py_returns(venue):
    # Codex and Antigravity: a cancelled worker call let the next one into alpaca-py's unlocked handler
    # maps, and the client never dropped the symbol alpaca-py did, so a resubscribe sent nothing.
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        gate = venue.stream.send_gate = threading.Event()
        second = None
        try:
            first = asyncio.ensure_future(client._unsubscribe_quotes(command(AAPL)))
            await wait_until(venue.stream.sending.is_set)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert client._stream_calls.locked()  # alpaca-py is still inside the unsubscribe
            second = asyncio.ensure_future(client._subscribe_trades(command(AAPL)))
            await asyncio.sleep(0.2)
            assert venue.stream.trade_handlers == {}  # kept out until the first call returns
        finally:
            venue.stream.send_gate = None
            gate.set()
        assert second is not None
        await second
        assert "AAPL" not in client._quote_symbols  # dropped as alpaca-py dropped it
        await client._subscribe_quotes(command(AAPL))
        assert list(venue.stream.quote_handlers) == ["AAPL"]

    run_with_client(body)

    assert venue.stream.max_active == 1


def test_a_subscribe_the_executor_refuses_records_nothing(venue) -> None:
    # Antigravity: the symbols were recorded before the call, so a refused call left them recorded with
    # no handler in alpaca-py, and every later subscribe of them sent nothing.
    executor = RefusingStreamExecutor()

    async def body(client) -> None:
        executor.refuse = True
        try:
            with pytest.raises(RuntimeError, match="cannot schedule"):
                await client._subscribe_quotes(command(AAPL))
        finally:
            executor.refuse = False
        assert not client._quote_symbols
        assert not client._stream_calls.locked()
        await client._subscribe_quotes(command(AAPL))
        assert list(venue.stream.quote_handlers) == ["AAPL"]

    run_with_client(body, executor=executor)


def test_a_subscription_once_the_stream_has_ended_is_refused_while_its_thread_winds_down(
    venue, monkeypatch
) -> None:
    # Antigravity and Muse: the end was known from the thread alone, which outlives `run()` by
    # alpaca-py's loop teardown, so a subscription made after the end was reported still succeeded.
    venue.stream.ends_on_its_own = True
    real_post = canon_alpaca.data.post
    reported, wind_down = threading.Event(), threading.Event()

    def post(loop, callback, item) -> None:
        real_post(loop, callback, item)
        if isinstance(item, tuple) and len(item) == 2 and item[0] is venue.stream:
            reported.set()
            wind_down.wait(5)  # the thread is still alive after its end is reported

    monkeypatch.setattr(canon_alpaca.data, "post", post)

    async def body(client) -> None:
        try:
            await client._subscribe_quotes(command(AAPL))
            await wait_until(reported.is_set)
            for _ in range(5):
                await asyncio.sleep(0.01)  # the end has reached the client
            assert client._stream_thread is not None and client._stream_thread.is_alive()
            with pytest.raises(RuntimeError, match="ended by itself"):
                await client._subscribe_trades(command(AAPL))
            assert not client._trade_symbols
        finally:
            wind_down.set()

    run_with_client(body)


@pytest.mark.parametrize("how", ["fails", "is cancelled"])
def test_a_connect_that_stops_part_way_leaves_nothing_a_hook_can_use(venue, how) -> None:
    # Antigravity: the clients a connect built stayed set when loading instruments failed, so a later
    # subscription started a stream on a client that never connected.
    async def body(client) -> None:
        await client._disconnect()
        provider = client._provider

        async def initialize(*_: object, **__: object) -> None:
            if how == "fails":
                raise ConnectionError("assets request failed")
            await asyncio.sleep(10)

        provider.initialize = initialize
        connecting = asyncio.ensure_future(client._connect())
        if how == "fails":
            with pytest.raises(ConnectionError):
                await connecting
        else:
            await asyncio.sleep(0.05)
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
        del provider.initialize
        with pytest.raises(RuntimeError, match="not connected"):
            await client._subscribe_quotes(command(AAPL))
        with pytest.raises(RuntimeError, match="not connected"):
            await client._request_bars(bars_request())
        await client._connect()  # a connect that completes serves again
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)

    run_with_client(body)

    assert len(venue.stream.run_threads) == 1


def test_a_stream_thread_that_cannot_start_leaves_no_feed_behind(venue, monkeypatch) -> None:
    # Antigravity: the stream built for the first subscription stayed, with its feed, so the next
    # subscription on another feed was refused although no stream had ever run.
    real_start = threading.Thread.start
    refused = []

    def start(self) -> None:
        if "-stream-" in self.name and not refused:
            refused.append(self.name)
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)

    async def body(client) -> None:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            await client._subscribe_quotes(command(AAPL, params={"feed": "boats"}))
        assert client._stream is None
        await client._subscribe_quotes(
            command(AAPL)
        )  # the default feed, not refused as "not boats"
        await wait_until(venue.stream.running.is_set)

    run_with_client(body)

    assert venue.built["stream"]["feed"] == DataFeed.IEX


@pytest.mark.parametrize("call", ["subscriptions", "bar requests"])
def test_a_feed_given_as_none_is_refused_not_read_as_the_default(call) -> None:
    # Codex and Antigravity: ADR 0055 refuses a value that is not a string; only an absent key is the
    # default.
    from canon_alpaca.constants import BARS_FEEDS
    from canon_alpaca.constants import STREAM_FEEDS

    feeds = STREAM_FEEDS if call == "subscriptions" else BARS_FEEDS
    with pytest.raises(TypeError, match="got None"):
        canon_alpaca.data.named_feed({"feed": None, "start_ns": None}, feeds, call)
    assert canon_alpaca.data.named_feed({"start_ns": None}, feeds, call) == (DataFeed.IEX, False)


def test_a_bars_request_naming_feed_none_becomes_the_failure_reason(venue) -> None:
    strategy = consumer(want_quotes=False, want_trades=False, bars_params={"feed": None})

    run(build_node(strategy), "hosted")

    ((_, reason),) = strategy.seen_responses
    assert "must name an Alpaca feed" in reason
    assert venue.historical.requests == []


def test_an_unsubscribe_reads_its_feed_as_a_subscription_does(venue, capfd) -> None:
    # Muse and Antigravity: an unsubscribe naming another feed, or one Alpaca does not stream, removed
    # the subscription on the stream's feed. The engine passes an unsubscribe the params its
    # subscription had (observed offline), so one from a strategy names the stream's feed.
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL, params={"feed": "boats"}))
        await wait_until(venue.stream.running.is_set)
        # Each names a subscription the client never made: a refused one's params come back on its
        # unsubscribe, so each is tolerated.
        await client._unsubscribe_quotes(command(AAPL, params={"feed": 1}))
        await client._unsubscribe_quotes(command(AAPL, params={"feed": "otc"}))
        await client._unsubscribe_quotes(command(AAPL, params={"feed": "sip"}))
        await client._unsubscribe_quotes(command(AAPL))  # the default feed, iex
        assert list(client._quote_symbols) == ["AAPL"]
        assert venue.stream.unsubscribed == []
        await client._unsubscribe_quotes(command(AAPL, params={"feed": "boats"}))
        assert not client._quote_symbols

    run_with_client(body)

    assert venue.stream.unsubscribed == [("quotes", ("AAPL",))]
    log = logged(capfd)
    assert "Not unsubscribing quotes: params 'feed' must name an Alpaca feed" in log
    assert "params named 'otc'; nothing was subscribed on it" in log
    assert "Not unsubscribing quotes for AAPL: the unsubscribe names feed sip" in log
    assert "names feed iex (named none, so the default)" in log


def test_an_unsubscribe_from_the_engine_names_the_subscriptions_feed(venue, monkeypatch) -> None:
    # What the test above relies on: a strategy that subscribed on `boats` and unsubscribes with no
    # params reaches the hook with `boats`. Fails if NautilusTrader stops passing them on.
    calls: list = []
    original = AlpacaDataClient._unsubscribe_quotes

    async def recorded(self, command) -> None:
        calls.append(dict(command.params or {}).get("feed"))
        await original(self, command)

    monkeypatch.setattr(AlpacaDataClient, "_unsubscribe_quotes", recorded)

    class Unsubscribes(Consumer):
        def on_quote(self, quote) -> None:
            self.seen_quotes.append(quote)
            if len(self.seen_quotes) == 1:
                self.unsubscribe_quotes(AAPL)
                self.shutdown_system("unsubscribed")

    strategy = Unsubscribes()
    strategy.want_trades, strategy.bar_type, strategy.auto_shutdown = False, None, False
    strategy.subscribe_params = {"feed": "boats"}

    run(build_node(strategy), "hosted")

    assert calls == ["boats"]
    assert venue.stream.unsubscribed == [("quotes", ("AAPL",))]


def test_a_bar_request_starting_after_now_sends_nothing_and_answers_no_bars(venue) -> None:
    # Muse and Antigravity: the end was cut to now and the start was not, so Alpaca was asked for an
    # inverted window and the request failed on its 422.
    async def body(client) -> None:
        now = datetime.now(UTC)
        await client._request_bars(bars_request(now + HOUR, now + 2 * HOUR))

    run_with_client(body)

    assert venue.historical.requests == []


def test_a_window_starting_after_now_holds_nothing_to_ask_for() -> None:
    now = datetime(2026, 11, 10, 15, tzinfo=UTC)
    window = canon_alpaca.data.alpaca_bars_window(now + HOUR, now + 2 * HOUR, now, HOUR)

    assert window is None
    assert canon_alpaca.data.alpaca_bars_window(now - HOUR, now + HOUR, now, HOUR) == (
        now - 2 * HOUR,
        now - HOUR,
    )


def test_a_stream_another_client_with_the_same_id_left_behind_is_not_taken_for_this_ones(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # Codex: thread names were unique only within one client, so a fresh node's client logged an
    # abandoned stream's lines as its own stream's, on its own feed.
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.05)
    old, new = venue.stream, alpaca.Stream()
    names: list[str] = []

    async def abandon(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(old.running.is_set)
        old.ignores_stop = True
        names.append(client._stream_thread.name)
        with pytest.raises(RuntimeError, match="did not stop"):
            await client._disconnect()

    async def fresh(client) -> None:
        await client._subscribe_quotes(command(AAPL, params={"feed": "boats"}))
        assert client._stream_thread.name != names[0]
        alpaca_log_on_thread(names[0], "from the abandoned stream")
        for _ in range(5):
            await asyncio.sleep(0.01)

    try:
        run_with_client(abandon)
        while BUILT_NODES:  # one node per thread: the first goes before the fresh one is built
            BUILT_NODES.pop().dispose()
        monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: new)
        run_with_client(fresh)
    finally:
        old.release.set()

    assert "from the abandoned stream" not in logged(capfd)


def test_a_stopping_streams_own_lines_are_kept_and_name_its_thread(venue, monkeypatch, capfd):
    # Antigravity read a line logged while the stream stops as misattributed. It is that stream's own,
    # carries its feed and thread, and says why a stop is slow, so it is kept.
    monkeypatch.setattr(canon_alpaca.data, "STREAM_JOIN_TIMEOUT_SECS", 0.3)

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        venue.stream.ignores_stop = True
        name = client._stream_thread.name
        try:
            stopping = asyncio.ensure_future(client._disconnect())
            await asyncio.sleep(0.05)
            alpaca_log_on_thread(name, "websocket error while stopping")
            with pytest.raises(RuntimeError, match="did not stop"):
                await stopping
        finally:
            venue.stream.release.set()

    run_with_client(body)

    assert re.search(
        r"Alpaca stream on feed iex \(ALPACA-stream-\d+\): websocket error while stopping",
        logged(capfd),
    )


def test_a_quote_from_before_an_unsubscribe_is_dropped_after_a_resubscribe(venue, alpaca) -> None:
    # Muse: a quote alpaca-py dispatched before an unsubscribe and the node's loop handled after the
    # symbol was subscribed again passed both the stream and the symbol check, with a fresh `ts_init`.
    venue.stream.quotes = []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        handler = venue.stream.quote_handlers["AAPL"]
        await client._unsubscribe_quotes(command(AAPL))
        await client._subscribe_quotes(command(AAPL))
        await asyncio.to_thread(asyncio.run, handler(alpaca.quote()))  # the earlier subscription's
        for _ in range(5):
            await asyncio.sleep(0.01)

    cache = run_with_client(body)

    assert cache.quote(AAPL) is None


def test_an_unsubscribe_after_a_refused_subscription_on_its_feed_is_tolerated(venue, capfd) -> None:
    # The code review: the engine passes an unsubscribe its subscription's params, so a subscription
    # refused for its feed comes back as an unsubscribe naming that feed, which must not fail again.
    async def body(client) -> None:
        with pytest.raises(ValueError, match="params named 'otc'"):
            await client._subscribe_quotes(command(AAPL, params={"feed": "otc"}))
        await client._unsubscribe_quotes(command(AAPL, params={"feed": "otc"}))

    run_with_client(body)

    assert "Not unsubscribing quotes" in logged(capfd)


def test_a_subscription_while_disconnecting_says_so_not_that_the_stream_ended(venue) -> None:
    # The code review: the stop `_disconnect` asks for also ends the stream's run.
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.running.is_set)
        venue.stream.ignores_stop = True
        try:
            stopping = asyncio.ensure_future(client._disconnect())
            await asyncio.sleep(0.05)
            with pytest.raises(RuntimeError, match="disconnecting"):
                await client._subscribe_trades(command(AAPL))
            await client._unsubscribe_quotes(command(AAPL))  # forgotten, nothing sent
            assert venue.stream.unsubscribed == []
        finally:
            venue.stream.release.set()
        await stopping

    run_with_client(body)


def test_no_stream_call_waits_on_a_call_stuck_in_a_stream_that_has_ended(venue) -> None:
    # The code review: a call alpaca-py posts to its loop as the run ends can wait for good, holding
    # the client's stream lock. Once the run has ended, hooks make no call and wait for none.
    venue.stream.ends_on_its_own = True

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(lambda: client._stream_ended is not None and client._stream_ended.is_set())
        await client._stream_calls.acquire()  # as a call stuck in the ended stream
        with pytest.raises(RuntimeError, match="ended by itself"):
            await asyncio.wait_for(client._subscribe_trades(command(AAPL)), 1)
        await asyncio.wait_for(client._unsubscribe_quotes(command(AAPL)), 1)
        assert not client._quote_symbols

    run_with_client(body)

    assert venue.stream.unsubscribed == []


def test_a_window_inverted_in_the_past_is_still_sent(venue) -> None:
    # The code review: only a window starting after now is answered with no bars unasked; one
    # inverted as requested goes to Alpaca, whose refusal becomes the failure reason.
    async def body(client) -> None:
        with pytest.raises(APIError, match="start is after end"):
            await client._request_bars(bars_request(END, START))

    run_with_client(body)

    assert len(venue.historical.requests) == 1


# -- a subscribe made while alpaca-py connects (Codex) ----------------------------------------------------


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_a_subscription_lost_while_the_stream_connects_is_sent_on_its_first_message(
    venue, alpaca, monkeypatch, make
) -> None:
    # alpaca-py 0.45.0 sends its handler maps when it connects and only then counts as running, so a
    # subscribe made between the two is never sent. The client sends its subscriptions again once the
    # stream's first quote or trade shows it is running.
    if make == "real":
        stream, socket = alpaca.memory_stream(monkeypatch)
        gate, held, wire = threading.Event(), socket.holding, lambda: socket.wire
        socket.gate = gate
        running = lambda: stream._running
    else:
        stream = venue.stream
        stream.trades = []
        gate = stream.snapshot_gate = threading.Event()
        held, wire, running = stream.snapshot_taken, lambda: stream.wire, stream.running.is_set
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: stream)

    async def body(client) -> None:
        try:
            await client._subscribe_quotes(command(AAPL))
            await wait_until(held.is_set)
            await client._subscribe_trades(command(AAPL))  # in the window: recorded, not sent
        finally:
            gate.set()
        await wait_until(running)
        if make == "real":
            await asyncio.sleep(0.2)
            assert wire()["trades"] == set()  # quiet: nothing has arrived, so nothing is resent yet
            socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: wire()["trades"] == {"AAPL"})

    cache = run_with_client(body)

    assert cache.quote(AAPL) is not None


def test_a_stream_with_one_subscription_sends_nothing_again(venue) -> None:
    # The subscribe that starts the run is always sent: alpaca-py waits for it before connecting.
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy), "hosted")

    assert len(strategy.seen_quotes) == 1
    assert venue.stream.subscribes_sent == 0


def test_a_failed_resend_is_logged_and_tried_again_on_the_next_message(
    venue, alpaca, capfd
) -> None:
    stream = venue.stream
    gate = stream.snapshot_gate = threading.Event()

    async def body(client) -> None:
        try:
            await client._subscribe_quotes(command(AAPL))
            await wait_until(stream.snapshot_taken.is_set)
            await client._subscribe_trades(command(AAPL))  # in the window
            stream.send_error = ConnectionError("send failed")  # the first resend's send fails
        finally:
            gate.set()
        await wait_until(lambda: client._resend_tries == 1 and not client._stream_heard)
        assert stream.wire["trades"] == set()
        stream.send_error = None
        handler = stream.quote_handlers["AAPL"]
        await asyncio.to_thread(asyncio.run, handler(alpaca.quote()))  # the next message
        await wait_until(lambda: stream.wire["trades"] == {"AAPL"})
        await asyncio.to_thread(asyncio.run, handler(alpaca.quote()))  # sent: nothing more to do
        await asyncio.sleep(0.05)
        assert client._resend_tries == 2
        assert stream.subscribes_sent == 1

    run_with_client(body)

    log = logged(capfd)
    assert log.count("Alpaca stream: sending the subscriptions again failed: send failed") == 1
    assert "the next message tries again (2 left)" in log


def test_a_stream_call_waiting_through_a_reconnect_takes_the_new_connects_lock(venue) -> None:
    # The code review: a call that waited on the old connect's lock must not then run on the new
    # connect's stream outside its lock.
    async def body(client) -> None:
        old = client._stream_calls
        await old.acquire()  # as a call stuck in the old connect's stream
        waiting = asyncio.ensure_future(client._subscribe_quotes(command(AAPL)))
        await asyncio.sleep(0.05)
        await client._disconnect()
        await client._connect()
        new = client._stream_calls
        assert new is not old
        await new.acquire()  # a call in the new connect
        old.release()
        await asyncio.sleep(0.1)
        assert venue.stream.quote_handlers == {}  # still waiting, now on the new connect's lock
        new.release()
        await asyncio.wait_for(waiting, 2)
        assert list(venue.stream.quote_handlers) == ["AAPL"]

    run_with_client(body)


class InlineStreamExecutor(ThreadPoolExecutor):
    """A default executor that runs a stream subscribe or unsubscribe call at once and hands back its
    finished future, as when a worker returns before `run_in_executor` has chained the future
    (CPython 3.14 `futures._chain_future` then copies the state at once). The node's other work runs
    in threads as usual."""

    def submit(self, fn, /, *args, **kwargs):
        if not stream_call_name(fn).startswith(("subscribe_", "unsubscribe_")):
            return super().submit(fn, *args, **kwargs)
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 - handed to the caller, as a worker would
            future.set_exception(e)
        return future


def test_a_stream_call_that_returns_at_once_is_recorded_before_its_hook_returns(venue) -> None:
    # Under load, `test_an_unsubscribe_reads_its_feed_as_a_subscription_does` found the hook back
    # before the unsubscribe was recorded: a call already done when the hook awaited it.
    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        assert list(client._quote_symbols) == ["AAPL"]
        await wait_until(venue.stream.running.is_set)
        await client._unsubscribe_quotes(command(AAPL))
        assert not client._quote_symbols  # recorded by the time the hook returns
        assert not client._stream_calls.locked()

    run_with_client(body, executor=InlineStreamExecutor())


# -- historical trades: the single-symbol HTTP endpoint --------------------------------------------


@pytest.fixture
def trades_http(venue, monkeypatch) -> SimpleNamespace:
    """Real alpaca-py GET handling, with its HTTP session answered entirely offline.

    Alpaca documents `trades` and `next_page_token` for GET /v2/stocks/{symbol}/trades:
    https://docs.alpaca.markets/us/reference/stocktradesingle-1.
    """
    historical = StockHistoricalDataClient(PLACEHOLDER, PLACEHOLDER)
    ns = SimpleNamespace(
        pages=[],
        calls=[],
        historical=historical,
        url="https://data.alpaca.markets/v2/stocks/AAPL/trades",
    )

    def answer(method: str, url: str, **kwargs) -> requests.Response:
        assert method == "GET"
        if url.endswith(CONDITIONS_PATH):  # the trade-condition filter's connect: the venue's map
            response = requests.Response()
            response.status_code = 200
            codes = TABLE[kwargs["params"]["tape"]]
            response._content = json.dumps(dict.fromkeys(codes, "name")).encode()
            return response
        assert url == ns.url
        ns.calls.append(dict(kwargs["params"]))
        assert ns.pages, "unexpected HTTP request"
        page = ns.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(page).encode()
        return response

    monkeypatch.setattr(historical._session, "request", answer)
    monkeypatch.setattr(canon_alpaca.data, "StockHistoricalDataClient", lambda **_: historical)
    return ns


def historical_trade(second: int, **changes) -> dict:
    return {
        "t": (START + timedelta(seconds=second)).isoformat(),
        "p": 182.12,
        "s": 100,
        "i": second + 1000,
        "x": "V",
        "c": ["@"],
        "z": "C",
        **changes,
    }


def trade_page(trades: list | None, token: str | None = None, symbol: str = "AAPL") -> dict:
    """The endpoint's required fields, including its required response symbol."""
    return {"symbol": symbol, "trades": trades, "next_page_token": token}


class TradeRequester(Strategy):
    """Drive the real request/response bridge and record its historical trade callback."""

    def __init__(self) -> None:
        super().__init__()
        self.params: dict | None = None
        self.limit: int | None = None
        self.trades_start: datetime | None = START
        self.trades_end: datetime | None = END
        self.instrument_ids = [AAPL]
        self.seen: list = []
        self.responses: list = []
        self.request_id = None

    def on_start(self) -> None:
        ids = self.instrument_ids[0] if len(self.instrument_ids) == 1 else self.instrument_ids
        self.request_id = self.request_trades(
            ids, start=self.trades_start, end=self.trades_end, limit=self.limit, params=self.params
        )

    def on_historical_trades(self, trades) -> None:
        self.seen.extend(trades)

    def on_data_response(self, response) -> None:
        if response.failure_reason or response.completed:
            self.responses.append(response)
            self.shutdown_system("Alpaca historical trades checked")


def test_historical_trades_follow_every_page_and_reach_the_strategy(trades_http) -> None:
    trades_http.pages = [
        trade_page([historical_trade(0)], "second"),
        trade_page([], "third"),
        trade_page([historical_trade(3600)]),
    ]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert str(strategy.responses[0].correlation_id) == str(strategy.request_id)
    assert [t.trade_id for t in strategy.seen] == [TradeId("1000"), TradeId("4600")]
    assert [t.ts_event for t in strategy.seen] == [dt_to_unix_nanos(START), dt_to_unix_nanos(END)]
    assert [t.ts_init for t in strategy.seen] == [t.ts_event for t in strategy.seen]
    first = strategy.seen[0]
    assert first.instrument_id == AAPL
    assert first.price == Price.from_str("182.1200")
    assert int(first.size) == 100
    assert first.aggressor_side == AggressorSide.NO_AGGRESSOR
    assert [c.get("page_token") for c in trades_http.calls] == [None, "second", "third"]
    for call in trades_http.calls:
        assert call["feed"] == "iex"
        assert call["sort"] == "asc"
        assert call["limit"] == 10_000
        assert datetime.fromisoformat(call["start"]) == START
        assert datetime.fromisoformat(call["end"]) == END
        assert "symbols" not in call


def test_a_trade_limit_counts_across_pages_and_stops_without_an_extra_fetch(trades_http):
    trades_http.pages = [
        trade_page([historical_trade(0), historical_trade(1)], "second"),
        trade_page([historical_trade(2)], "unneeded"),
    ]
    strategy = TradeRequester()
    strategy.limit = 3

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert [t.trade_id for t in strategy.seen] == [TradeId(str(n)) for n in (1000, 1001, 1002)]
    assert [c["limit"] for c in trades_http.calls] == [3, 1]


@pytest.mark.parametrize("feed", ["iex", "sip", "boats", "otc"])
def test_trade_history_sends_the_named_feed_and_preserves_response_metadata(
    venue, trades_http, monkeypatch, feed
):
    trades_http.pages = [trade_page([historical_trade(1)])]
    responses = []
    request = quotes_request(AAPL, start=START, end=END)
    request.params = {"feed": feed, "update_catalog": True}
    # Response bounds retain the exact nanoseconds, not the datetime's truncation.
    request.start_ns += 17
    request.end_ns += 23

    async def body(client) -> None:
        monkeypatch.setattr(client, "_handle_response", responses.append)
        await client._request_trades(request)

    run_with_client(body)

    (response,) = responses
    assert isinstance(response, TradesResponse)
    assert response.client_id.value == "ALPACA"
    assert response.instrument_id == AAPL
    assert response.correlation_id == request.request_id
    assert (response.start, response.end) == (request.start_ns, request.end_ns)
    assert response.params == request.params == {"feed": feed, "update_catalog": True}
    assert len(response.data) == 1
    assert trades_http.calls[0]["feed"] == feed


@pytest.mark.parametrize("feed", ["overnight", "delayed_sip", "other", None, 1])
def test_trade_history_refuses_an_unsupported_feed_before_http(trades_http, feed):
    strategy = TradeRequester()
    strategy.params = {"feed": feed}

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None and "feed" in response.failure_reason
    assert strategy.seen == []
    assert trades_http.calls == []


@pytest.mark.parametrize("trades", [[], None])
def test_empty_trade_history_completes_with_one_empty_response(trades_http, trades):
    trades_http.pages = [trade_page(trades)]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert strategy.seen == []
    assert len(trades_http.calls) == 1


def test_a_later_trade_page_failure_does_not_emit_partial_history(trades_http):
    trades_http.pages = [
        trade_page([historical_trade(1)], "second"),
        APIError('{"code": 42210000, "message": "trade page failed"}'),
    ]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None and "trade page failed" in response.failure_reason
    assert strategy.seen == []
    assert len(trades_http.calls) == 2


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"i": None}, "no trade ID"),
        ({"s": 1.5}, "fractional"),
        ({"p": float("nan")}, "not a finite number"),
        ({"p": 0.00001}, "rounds to zero"),
    ],
)
def test_an_unconvertible_historical_trade_fails_the_whole_request(trades_http, changes, reason):
    trades_http.pages = [trade_page([historical_trade(1), historical_trade(2, **changes)])]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None and reason in response.failure_reason
    assert strategy.seen == []


def test_historical_trade_rounding_is_counted_and_named(trades_http, capfd):
    trades_http.pages = [trade_page([historical_trade(1, p=0.12345), historical_trade(2)])]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert [t.price for t in strategy.seen] == [
        Price.from_str("0.1234"),
        Price.from_str("182.1200"),
    ]
    log = logged(capfd)
    assert "Rounded 1 of 2 Alpaca trades for AAPL" in log
    assert "0.12345" in log


def test_trade_history_refuses_multiple_instruments_before_http(venue, trades_http):
    async def body(client) -> None:
        with pytest.raises(ValueError, match="one instrument, got 2"):
            await client._request_trades(quotes_request(AAPL, AAPL, start=START, end=END))

    run_with_client(body)

    assert trades_http.calls == []


def test_trade_history_caps_each_page_at_the_documented_maximum(trades_http):
    trades_http.pages = [trade_page([historical_trade(1)])]
    strategy = TradeRequester()
    strategy.limit = 10_001

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert len(strategy.seen) == 1
    assert trades_http.calls[0]["limit"] == 10_000


def test_trade_history_starting_after_now_completes_without_http(venue, trades_http, monkeypatch):
    responses = []

    async def body(client) -> None:
        now = datetime.now(UTC)
        monkeypatch.setattr(client, "_handle_response", responses.append)
        await client._request_trades(quotes_request(AAPL, start=now + HOUR, end=now + 2 * HOUR))

    run_with_client(body)

    (response,) = responses
    assert isinstance(response, TradesResponse) and response.data == []
    assert trades_http.calls == []


def test_historical_trades_let_the_node_loop_run_between_conversion_slices(
    venue, trades_http, monkeypatch
):
    trades_http.pages = [trade_page([historical_trade(n) for n in range(4)])]
    # Set without requiring the constant to exist yet, so the unimplemented hook is the before failure.
    monkeypatch.setattr(canon_alpaca.data, "TRADES_PER_SLICE", 2, raising=False)
    turns = [0]
    seen_at: list[int] = []
    parse = canon_alpaca.data.parse_trade

    def counting_parse(*args, **kwargs):
        seen_at.append(turns[0])
        return parse(*args, **kwargs)

    monkeypatch.setattr(canon_alpaca.data, "parse_trade", counting_parse)

    async def body(client) -> None:
        async def count() -> None:
            while True:
                turns[0] += 1
                await asyncio.sleep(0)

        counter = asyncio.ensure_future(count())
        try:
            await client._request_trades(quotes_request(AAPL, start=START, end=END))
        finally:
            counter.cancel()

    run_with_client(body)

    assert len(seen_at) == 4
    assert seen_at[0] == seen_at[1] < seen_at[2] == seen_at[3]


def test_trade_history_filters_canceled_and_incorrect_prints_before_the_sdk_drops_u(
    trades_http, capfd
):
    trades_http.pages = [
        trade_page(
            [historical_trade(0, u="canceled"), historical_trade(1, u="incorrect")], "valid"
        ),
        trade_page([historical_trade(2, c=["I"]), historical_trade(3, u="corrected")]),
    ]
    strategy = TradeRequester()
    strategy.limit = 2

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert [t.trade_id for t in strategy.seen] == [TradeId("1002"), TradeId("1003")]
    assert [c["limit"] for c in trades_http.calls] == [2, 2]
    log = logged(capfd)
    assert "Dropped 2 invalid Alpaca trades for AAPL" in log
    assert "canceled=1, incorrect=1" in log


@pytest.mark.parametrize("tokens", [["same", "same"], ["a", "b", "a"], [""]])
def test_trade_history_refuses_empty_or_repeated_page_tokens(trades_http, tokens):
    # A final page stops the unfixed implementation too, so a before run cannot spin forever.
    trades_http.pages = [trade_page([], token) for token in tokens] + [trade_page([])]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None
    assert "AAPL" in response.failure_reason and "page token" in response.failure_reason
    assert len(trades_http.calls) == len(tokens)
    assert strategy.seen == []


def test_a_repeated_trade_token_is_refused_even_when_the_page_fills_the_limit(trades_http):
    trades_http.pages = [
        trade_page([historical_trade(0)], "same"),
        trade_page([historical_trade(1)], "same"),
    ]
    strategy = TradeRequester()
    strategy.limit = 2

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None and "repeated page token" in response.failure_reason
    assert strategy.seen == []


def test_trade_history_page_cap_fails_without_partial_data_and_releases_the_http_lock(
    venue, trades_http, monkeypatch
):
    monkeypatch.setattr(canon_alpaca.data, "TRADES_MAX_PAGES", 2, raising=False)
    trades_http.pages = [
        trade_page([historical_trade(0)], "a"),
        trade_page([historical_trade(1)], "b"),
        trade_page([historical_trade(2)]),
    ]
    responses = []

    async def body(client) -> None:
        monkeypatch.setattr(client, "_handle_response", responses.append)
        request = quotes_request(AAPL, start=START, end=END)
        with pytest.raises(ValueError, match="AAPL.*exceeded 2 pages"):
            await client._request_trades(request)
        assert len(trades_http.calls) == 2
        assert responses == []
        await asyncio.wait_for(client._request_trades(request), 2)

    run_with_client(body)

    (response,) = responses
    assert [t.trade_id for t in response.data] == [TradeId("1002")]
    assert len(trades_http.calls) == 3


@pytest.mark.parametrize("first_page_size", [1, 3])
def test_trade_history_enforces_its_total_limit_even_when_a_page_over_returns(
    trades_http, first_page_size
):
    trades_http.pages = [
        trade_page([historical_trade(n) for n in range(first_page_size)], "second"),
        trade_page([historical_trade(3), historical_trade(4)]),
    ]
    strategy = TradeRequester()
    strategy.limit = 2

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    expected_ids = ("1000", "1003") if first_page_size == 1 else ("1000", "1001")
    assert [t.trade_id for t in strategy.seen] == [TradeId(i) for i in expected_ids]


@pytest.mark.parametrize(
    ("symbol", "encoded"),
    [("BRK/B", "BRK%2FB"), ("A?B", "A%3FB"), ("A#B", "A%23B"), ("A%B", "A%25B")],
)
def test_trade_history_quotes_the_symbol_as_one_url_path_segment(trades_http, symbol, encoded):
    trades_http.url = f"https://data.alpaca.markets/v2/stocks/{encoded}/trades"
    trades_http.pages = [trade_page([], symbol=symbol)]

    canon_alpaca.data.fetch_trades(
        trades_http.historical, StockTradesRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    )

    assert len(trades_http.calls) == 1


def test_trade_history_refuses_a_page_for_another_symbol(trades_http):
    trades_http.pages = [trade_page([historical_trade(0)], symbol="MSFT")]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None
    assert "AAPL" in response.failure_reason and "MSFT" in response.failure_reason
    assert strategy.seen == []


@pytest.mark.parametrize(
    "page",
    [
        None,
        [],
        {"symbol": "AAPL", "next_page_token": None},
        {"trades": [], "next_page_token": None},
        {"symbol": "AAPL", "trades": []},
        {**trade_page([]), "trades": {}},
        {**trade_page([]), "trades": "not an array"},
        trade_page([None]),
        trade_page(["not an object"]),
        trade_page([{}]),
        trade_page([historical_trade(0, u="unknown")]),
        {**trade_page([]), "next_page_token": 123},
    ],
)
def test_malformed_trade_pages_fail_with_the_symbol_and_page_context(trades_http, page):
    trades_http.pages = [page]
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None
    assert "AAPL" in response.failure_reason and "page 1" in response.failure_reason
    assert strategy.seen == []


@pytest.mark.parametrize("params", [{"asof": "2022-01-01"}, {"currency": "EUR"}])
def test_trade_history_refuses_unsupported_mapping_and_currency_params_before_http(
    trades_http, params
):
    trades_http.pages = [trade_page([historical_trade(0)])]
    strategy = TradeRequester()
    strategy.params = params

    run(build_node(strategy), "hosted")

    (response,) = strategy.responses
    assert response.failure_reason is not None
    assert next(iter(params)) in response.failure_reason
    assert "not supported" in response.failure_reason
    assert trades_http.calls == []
    assert strategy.seen == []


def test_the_shared_historical_stand_in_can_answer_an_empty_trade_request(venue):
    strategy = TradeRequester()

    run(build_node(strategy), "hosted")

    assert [r.failure_reason for r in strategy.responses] == [None]
    assert strategy.seen == []


def test_trade_history_preserves_nanosecond_bounds_and_raw_trade_timestamps(
    venue, trades_http, monkeypatch
):
    trades_http.pages = [
        trade_page([historical_trade(0, t="2022-01-03T09:00:00.000000500Z")], "unneeded")
    ]
    request = quotes_request(AAPL, start=START, end=START)
    request.start_ns += 17
    request.end_ns += 800
    request.limit = 1
    responses = []

    async def body(client) -> None:
        monkeypatch.setattr(client, "_handle_response", responses.append)
        await client._request_trades(request)

    run_with_client(body)

    (sent,) = trades_http.calls
    assert dt_to_unix_nanos(sent["start"]) == request.start_ns
    assert dt_to_unix_nanos(sent["end"]) == request.end_ns
    (response,) = responses
    (trade,) = response.data
    assert trade.ts_event == trade.ts_init == dt_to_unix_nanos(START) + 500
    assert (response.start, response.end) == (request.start_ns, request.end_ns)


def test_out_of_window_trade_rows_do_not_consume_the_exact_window_limit(
    venue, trades_http, monkeypatch
):
    trades_http.pages = [
        trade_page([historical_trade(0)], "inside"),
        trade_page([historical_trade(1, t="2022-01-03T09:00:00.000000500Z")]),
    ]
    request = quotes_request(AAPL, start=START, end=START)
    request.start_ns += 17
    request.end_ns += 800
    request.limit = 1
    responses = []

    async def body(client) -> None:
        monkeypatch.setattr(client, "_handle_response", responses.append)
        await client._request_trades(request)

    run_with_client(body)

    (response,) = responses
    assert [t.trade_id for t in response.data] == [TradeId("1001")]
    assert [c["limit"] for c in trades_http.calls] == [1, 1]


def test_a_trade_catalog_gap_starting_one_nanosecond_after_coverage_keeps_its_first_trade(
    trades_http, tmp_path
):
    boundary = START + timedelta(seconds=1)
    trades_http.pages = [trade_page([historical_trade(1)])]
    first = TradeRequester()
    first.trades_end = boundary
    first.params = {"update_catalog": True}

    run(build_node(first, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in first.responses] == [None]
    assert [t.trade_id for t in first.seen] == [TradeId("1001")]
    BUILT_NODES.pop().dispose()
    trades_http.pages = [trade_page([historical_trade(2, t="2022-01-03T09:00:01.000000500Z")])]
    second = TradeRequester()
    second.trades_end = START + timedelta(seconds=2)
    second.params = {"update_catalog": True}

    run(build_node(second, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in second.responses] == [None]
    assert [t.trade_id for t in second.seen] == [TradeId("1001"), TradeId("1002")]
    assert [t.ts_init for t in second.seen] == [
        dt_to_unix_nanos(boundary),
        dt_to_unix_nanos(boundary) + 500,
    ]
    assert dt_to_unix_nanos(trades_http.calls[1]["start"]) == dt_to_unix_nanos(boundary) + 1
    catalog = ParquetDataCatalog(str(tmp_path))
    assert [t.ts_init for t in catalog.query_trade_ticks([str(AAPL)])] == [
        t.ts_init for t in second.seen
    ]
    BUILT_NODES.pop().dispose()
    again = TradeRequester()
    again.trades_end = second.trades_end
    again.params = {"update_catalog": True}

    run(build_node(again, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in again.responses] == [None]
    assert [t.ts_init for t in again.seen] == [t.ts_init for t in second.seen]
    assert len(trades_http.calls) == 2


def test_trade_catalog_reads_ignore_feed_unless_skip_catalog_data_is_set(trades_http, tmp_path):
    trades_http.pages = [trade_page([historical_trade(0)])]
    first = TradeRequester()
    first.params = {"feed": "iex", "update_catalog": True}

    run(build_node(first, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in first.responses] == [None]
    BUILT_NODES.pop().dispose()
    trades_http.pages = [trade_page([historical_trade(1, p=999)])]
    catalog_read = TradeRequester()
    catalog_read.params = {"feed": "sip"}  # no write-back: the registered catalog still answers

    run(build_node(catalog_read, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in catalog_read.responses] == [None]
    assert [t.trade_id for t in catalog_read.seen] == [TradeId("1000")]
    assert len(trades_http.calls) == 1
    BUILT_NODES.pop().dispose()
    bypass = TradeRequester()
    bypass.params = {"feed": "sip", "skip_catalog_data": True}

    run(build_node(bypass, catalog=tmp_path), "hosted")

    assert [r.failure_reason for r in bypass.responses] == [None]
    assert [t.price for t in bypass.seen] == [Price.from_str("999.0000")]
    assert len(trades_http.calls) == 2
    assert trades_http.calls[1]["feed"] == "sip"
    assert dt_to_unix_nanos(trades_http.calls[1]["start"]) == dt_to_unix_nanos(START)
    assert dt_to_unix_nanos(trades_http.calls[1]["end"]) == dt_to_unix_nanos(END)


# -- historical quotes (ADR 0066) ------------------------------------------------------------------

Q_START = datetime(2022, 1, 3, 15, tzinfo=UTC)
Q_END = Q_START + timedelta(minutes=1)
WRITE_BACK = {"update_catalog": True}


class QuoteRequester(Strategy):
    """Request AAPL's quotes over each window in turn, recording what arrives, then stop the node.

    `Strategy` is a native class whose `__new__` takes no extra arguments, so the caller sets the options."""

    def __init__(self) -> None:
        super().__init__()
        self.windows: list[tuple[datetime | None, datetime | None]] = [(Q_START, Q_END)]
        self.params: dict | None = dict(WRITE_BACK)
        self.limit: int | None = None
        self.instrument_ids: list[InstrumentId] = [AAPL]
        self.seen: list = []
        self.failures: list[str | None] = []

    def on_start(self) -> None:
        self._request_next()

    def _request_next(self) -> None:
        start, end = self.windows[len(self.failures)]
        ids = self.instrument_ids[0] if len(self.instrument_ids) == 1 else self.instrument_ids
        self.request_quotes(ids, start=start, end=end, limit=self.limit, params=self.params)

    def on_historical_quotes(self, quotes) -> None:
        self.seen.extend(quotes)

    def on_data_response(self, response) -> None:
        if not (response.failure_reason or response.completed):
            return
        self.failures.append(response.failure_reason)
        if len(self.failures) < len(self.windows):
            self._request_next()
        else:
            self.shutdown_system("Alpaca quotes checked")


def quote_requester(**options: object) -> QuoteRequester:
    strategy = QuoteRequester()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


def quote_seconds(*seconds: float) -> list[datetime]:
    return [Q_START + timedelta(seconds=s) for s in seconds]


def stored_quotes(catalog: Path) -> list:
    return ParquetDataCatalog(str(catalog)).query_quote_ticks([str(AAPL)])


def quote_intervals(catalog: Path) -> list[tuple[int, int]]:
    return sorted(
        ParquetDataCatalog(str(catalog)).get_intervals(NautilusDataType.QuoteTick, str(AAPL))
    )


def catalog_files(catalog: Path) -> list[str]:
    return sorted(str(p.relative_to(catalog)) for p in catalog.rglob("*") if p.is_file())


def test_quotes_reach_the_strategy_stamped_when_alpaca_stamped_them(venue, alpaca) -> None:
    times = quote_seconds(0, 0.5, 30, 60, 61)
    venue.historical.quotes = alpaca.quotes_at(*times)
    strategy = quote_requester(params=None)

    run(build_node(strategy), "hosted")

    assert strategy.failures == [None]
    # Alpaca's window is inclusive at both ends (the quotes reference), so the quote at the end is kept.
    expected = [dt_to_unix_nanos(t) for t in times[:4]]
    assert [q.ts_event for q in strategy.seen] == expected
    assert [q.ts_init for q in strategy.seen] == expected
    first = strategy.seen[0]
    quote = alpaca.HISTORICAL_QUOTE_AAPL
    assert (first.bid_price, first.ask_price) == (
        Price.from_str("387.6700"),
        Price.from_str("387.7000"),
    )
    assert (int(first.bid_size), int(first.ask_size)) == (quote["bs"], quote["as"])
    (sent,) = venue.historical.requests
    assert sent.symbol_or_symbols == "AAPL"
    assert sent.feed is DataFeed.IEX
    assert (sent.start, sent.end) == (Q_START.replace(tzinfo=None), Q_END.replace(tzinfo=None))
    assert sent.limit is None


def test_a_quote_request_writes_back_and_a_covered_one_asks_alpaca_nothing(
    venue, alpaca, tmp_path
) -> None:
    """ADR 0066's first point offline: write-back works for quotes at the pin, as ADR 0040 found for bars."""
    venue.historical.quotes = alpaca.quotes_at(*quote_seconds(1, 2, 3))
    first = quote_requester()
    run(build_node(first, catalog=tmp_path), "hosted")

    assert first.failures == [None]
    assert quote_intervals(tmp_path) == [(dt_to_unix_nanos(Q_START), dt_to_unix_nanos(Q_END))]
    files = catalog_files(tmp_path)
    assert files == [
        "data/quotes/AAPL.ALPACA/2022-01-03T15-00-00-000000000Z_2022-01-03T15-01-00-000000000Z.parquet"
    ]
    assert [(q.ts_event, q.ts_init) for q in stored_quotes(tmp_path)] == [
        (q.ts_event, q.ts_init) for q in first.seen
    ]

    BUILT_NODES.pop().dispose()
    again = quote_requester()
    run(build_node(again, catalog=tmp_path), "hosted")

    assert again.failures == [None]
    assert len(venue.historical.requests) == 1  # served from the catalog
    assert [q.ts_event for q in again.seen] == [q.ts_event for q in first.seen]
    assert catalog_files(tmp_path) == files


def test_an_empty_quote_response_records_nothing_and_is_asked_for_again(
    venue, tmp_path, capfd
) -> None:
    """ADR 0066's second point offline: the Parquet catalog records no known-empty coverage."""
    strategy = quote_requester(windows=[(Q_START, Q_END), (Q_START, Q_END)])

    run(build_node(strategy, catalog=tmp_path), "hosted")

    assert strategy.failures == [None, None]
    assert strategy.seen == []
    assert len(venue.historical.requests) == 2
    assert quote_intervals(tmp_path) == []
    assert catalog_files(tmp_path) == []
    assert "Received empty QuoteTick response" in logged(capfd)


def test_a_staging_file_a_killed_write_leaves_is_not_read_and_the_window_is_written_again(
    venue, alpaca, tmp_path
) -> None:
    """ADR 0066's third point offline. The catalog encodes a file whole, writes it as `<name>#<n>` and
    renames it into place (observed at the pin), so a process killed in between leaves only that staging
    file, which is cut short here to stand for one killed mid-write."""
    name = "2022-01-03T15-00-00-000000000Z_2022-01-03T15-01-00-000000000Z.parquet"
    staged = tmp_path / "data" / "quotes" / str(AAPL) / f"{name}#1"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"PAR1" + bytes(1000))
    assert quote_intervals(tmp_path) == []
    assert stored_quotes(tmp_path) == []

    venue.historical.quotes = alpaca.quotes_at(*quote_seconds(1, 2, 3))
    strategy = quote_requester()
    run(build_node(strategy, catalog=tmp_path), "hosted")

    assert strategy.failures == [None]
    assert len(venue.historical.requests) == 1
    assert quote_intervals(tmp_path) == [(dt_to_unix_nanos(Q_START), dt_to_unix_nanos(Q_END))]
    assert len(stored_quotes(tmp_path)) == 3
    assert catalog_files(tmp_path) == [
        f"data/quotes/AAPL.ALPACA/{name}",
        f"data/quotes/AAPL.ALPACA/{name}#1",
    ]


def test_a_quote_request_with_a_limit_is_answered_and_not_written_back(venue, alpaca, tmp_path):
    """The catalog guide: write-back refuses a request with a record limit. At the pin the request is
    answered and nothing is written or recorded (observed)."""
    venue.historical.quotes = alpaca.quotes_at(*quote_seconds(1, 2, 3))
    strategy = quote_requester(limit=2)

    run(build_node(strategy, catalog=tmp_path), "hosted")

    assert strategy.failures == [None]
    assert len(strategy.seen) == 2
    assert venue.historical.requests[0].limit == 2
    assert quote_intervals(tmp_path) == []
    assert catalog_files(tmp_path) == []


def test_one_sided_historical_quotes_are_dropped_and_counted(venue, alpaca, capfd) -> None:
    venue.historical.quotes = [
        *alpaca.quotes_at(*quote_seconds(1)),
        {**alpaca.quotes_at(*quote_seconds(2))[0], "bp": 0},
        {**alpaca.quotes_at(*quote_seconds(3))[0], "ap": 0},
    ]
    strategy = quote_requester(params=None)

    run(build_node(strategy), "hosted")

    assert strategy.failures == [None]
    assert [q.ts_event for q in strategy.seen] == [dt_to_unix_nanos(quote_seconds(1)[0])]
    assert "Dropped 2 one-sided Alpaca quotes of 3 for AAPL" in logged(capfd)


def test_a_historical_quote_finer_than_the_precision_is_dropped_and_counted(
    venue, alpaca, capfd
) -> None:
    """A lit book quotes at the tick, so a finer quote price is a fault: dropped, counted and named,
    and the rest of the request is kept, as one-sided quotes are."""
    venue.historical.quotes = [
        {**alpaca.quotes_at(*quote_seconds(1))[0], "bp": 387.67001},
        *alpaca.quotes_at(*quote_seconds(2)),
    ]
    strategy = quote_requester(params=None)

    run(build_node(strategy), "hosted")

    assert strategy.failures == [None]
    assert [q.ts_event for q in strategy.seen] == [dt_to_unix_nanos(quote_seconds(2)[0])]
    log = logged(capfd)
    assert "Dropped 1 Alpaca quotes of 2 for AAPL" in log
    assert "bid price 387.67001 is finer than price precision 4" in log


def test_a_historical_quote_that_is_not_a_number_still_fails_the_request(venue, alpaca) -> None:
    venue.historical.quotes = [{**alpaca.quotes_at(*quote_seconds(1))[0], "bp": float("nan")}]
    strategy = quote_requester(params=None)

    run(build_node(strategy), "hosted")

    (reason,) = strategy.failures
    assert reason is not None and "not a finite number" in reason
    assert strategy.seen == []


@pytest.mark.parametrize("feed", ["sip", "boats", "otc", "overnight"])
def test_a_quote_request_naming_another_feed_than_iex_is_refused_before_sending(venue, feed):
    strategy = quote_requester(params={"feed": feed, **WRITE_BACK})

    run(build_node(strategy), "hosted")

    (reason,) = strategy.failures
    assert reason is not None and "quote requests take feed iex" in reason
    assert venue.historical.requests == []


def test_a_quote_request_naming_iex_is_sent_on_iex(venue) -> None:
    strategy = quote_requester(params={"feed": "iex"})

    run(build_node(strategy), "hosted")

    assert strategy.failures == [None]
    assert venue.historical.requests[0].feed is DataFeed.IEX


def test_a_failed_quotes_fetch_becomes_the_failure_reason(venue, tmp_path) -> None:
    venue.historical.error = APIError('{"code": 42210000, "message": "invalid start"}')
    strategy = quote_requester()

    run(build_node(strategy, catalog=tmp_path), "hosted")

    (reason,) = strategy.failures
    assert reason is not None and "invalid start" in reason
    assert catalog_files(tmp_path) == []


def quotes_request(
    *instrument_ids: InstrumentId, start: datetime, end: datetime
) -> SimpleNamespace:
    """The fields `_request_quotes` reads from a `RequestQuotes`, for calling the hook directly."""
    return SimpleNamespace(
        instrument_ids=list(instrument_ids),
        start=start,
        end=end,
        limit=None,
        request_id=UUID4(),
        start_ns=dt_to_unix_nanos(start),
        end_ns=dt_to_unix_nanos(end),
        params=None,
    )


def test_a_quote_request_for_two_instruments_is_refused(venue) -> None:
    async def body(client) -> None:
        with pytest.raises(ValueError, match="one instrument, got 2"):
            await client._request_quotes(quotes_request(AAPL, AAPL, start=Q_START, end=Q_END))

    run_with_client(body)
    assert venue.historical.requests == []


def test_a_quote_request_starting_after_now_sends_nothing(venue) -> None:
    # NautilusTrader's `request_quotes` refuses an end after now ("end was > now"), so the hook is called
    # directly, as another caller of it could.
    async def body(client) -> None:
        now = datetime.now(UTC)
        await client._request_quotes(quotes_request(AAPL, start=now + HOUR, end=now + 2 * HOUR))

    run_with_client(body)

    assert venue.historical.requests == []


def test_a_quote_request_ending_after_now_is_cut_at_now(venue) -> None:
    times = {}

    async def body(client) -> None:
        times["before"] = datetime.now(UTC)
        await client._request_quotes(
            quotes_request(AAPL, start=times["before"] - HOUR, end=times["before"] + HOUR)
        )
        times["after"] = datetime.now(UTC)

    run_with_client(body)

    (sent,) = venue.historical.requests
    assert sent.start == (times["before"] - HOUR).astimezone(UTC).replace(tzinfo=None)
    assert times["before"].replace(tzinfo=None) <= sent.end <= times["after"].replace(tzinfo=None)


def test_historical_quotes_are_converted_in_slices_that_let_the_loop_run(
    venue, alpaca, monkeypatch
) -> None:
    """An hour of IEX quotes takes seconds to convert on the node's loop, so the hook lets it run every
    `QUOTES_PER_SLICE` quotes: a task counting loop turns sees the count move between slices."""
    venue.historical.quotes = alpaca.quotes_at(*quote_seconds(1, 2, 3, 4))
    monkeypatch.setattr(canon_alpaca.data, "QUOTES_PER_SLICE", 2)
    turns = [0]
    seen_at: list[int] = []
    parse = canon_alpaca.data.parse_quote

    def counting_parse(*args, **kwargs):
        seen_at.append(turns[0])
        return parse(*args, **kwargs)

    monkeypatch.setattr(canon_alpaca.data, "parse_quote", counting_parse)

    async def body(client) -> None:
        async def count() -> None:
            while True:
                turns[0] += 1
                await asyncio.sleep(0)

        counter = asyncio.ensure_future(count())
        try:
            await client._request_quotes(quotes_request(AAPL, start=Q_START, end=Q_END))
        finally:
            counter.cancel()

    run_with_client(body)

    assert len(seen_at) == 4
    assert seen_at[0] == seen_at[1] < seen_at[2] == seen_at[3]


# -- a connected stream that sends nothing: alpaca-py's data_timeout and the client's mute limit ----------


def data_config(
    stream_data_timeout_secs: float | None = canon_alpaca.data.STREAM_DATA_TIMEOUT_SECS,
    stream_mute_limit_secs: float | None = canon_alpaca.data.STREAM_MUTE_LIMIT_SECS,
) -> AlpacaDataClientConfig:
    return AlpacaDataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
        stream_data_timeout_secs=stream_data_timeout_secs,
        stream_mute_limit_secs=stream_mute_limit_secs,
    )


QUIETEST_HEALTHY_SILENCE_SECS = 513
"""The longest silence seen on a stream that then delivered: `iex` subscribed to QQQ alone, pre-market
2026-11-11, one trade and no quote in 600 s, the trade 513 s after the subscription (README, "Mute
stream")."""


def test_the_stream_takes_its_data_timeout_from_the_config(venue) -> None:
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy, config=data_config(stream_data_timeout_secs=120.0)), "hosted")

    assert len(strategy.seen_quotes) == 1
    assert venue.built["stream"]["data_timeout"] == 120.0


def test_no_data_timeout_leaves_alpaca_py_without_one(venue) -> None:
    strategy = consumer(want_trades=False, bar_type=None)
    config = data_config(stream_data_timeout_secs=None, stream_mute_limit_secs=None)

    run(build_node(strategy, config=config), "hosted")

    assert venue.built["stream"]["data_timeout"] is None


def test_the_defaults_outlast_the_quietest_feed_seen_delivering() -> None:
    config = AlpacaDataClientConfig()

    timeout, limit = config.stream_data_timeout_secs, config.stream_mute_limit_secs
    assert timeout == canon_alpaca.data.STREAM_DATA_TIMEOUT_SECS
    assert limit == canon_alpaca.data.STREAM_MUTE_LIMIT_SECS
    assert timeout is not None and limit is not None
    assert timeout > QUIETEST_HEALTHY_SILENCE_SECS
    assert limit > timeout
    assert config.dict()["stream_data_timeout_secs"] == timeout  # serialised, as the guide has it


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"stream_data_timeout_secs": 0}, "stream_data_timeout_secs must be"),
        ({"stream_data_timeout_secs": -1.0}, "stream_data_timeout_secs must be"),
        ({"stream_data_timeout_secs": float("nan")}, "stream_data_timeout_secs must be"),
        ({"stream_data_timeout_secs": float("inf")}, "stream_data_timeout_secs must be"),
        ({"stream_data_timeout_secs": True}, "stream_data_timeout_secs must be"),
        ({"stream_data_timeout_secs": "900"}, "stream_data_timeout_secs must be"),
        ({"stream_mute_limit_secs": 0}, "stream_mute_limit_secs must be"),
        (
            {"stream_data_timeout_secs": 600.0, "stream_mute_limit_secs": 600.0},
            "longer than stream_data_timeout_secs",
        ),
        ({"feed": "sip"}, "no setting 'feed'"),
        ({"trade_condition_filter": "yes"}, "trade_condition_filter must be True or False"),
        ({"validate_trade_sequence": 1}, "validate_trade_sequence must be True or False"),
    ],
)
def test_the_config_refuses_a_bound_it_cannot_use(fields, error) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        AlpacaDataClientConfig(**fields)


def test_alpaca_py_reconnects_a_mute_stream_and_sends_its_subscriptions_again(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # alpaca-py 0.45.0's own path, on its real stream: `_consume` gives up on a connection that sends no
    # market data for `data_timeout` seconds, and `_drive_run` connects again and sends every handler map.
    stream, socket = alpaca.memory_stream(monkeypatch)
    stream._reconnect_min_backoff = 0.01  # alpaca-py waits 0.5 to 1 s first; shortened here

    def build(**kwargs: object) -> object:
        # As `StockDataStream(data_timeout=...)` sets it.
        stream._data_timeout = kwargs["data_timeout"]
        return stream

    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", build)

    def subscribes() -> list[dict]:
        return [m for m in socket.sent if m["action"] == "subscribe"]

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(lambda: len(subscribes()) >= 2)
        socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: client._stream_heard)

    config = data_config(stream_data_timeout_secs=0.1, stream_mute_limit_secs=None)
    cache = run_with_client(body, config=config)

    assert all(m["quotes"] == ["AAPL"] for m in subscribes())
    assert cache.quote(AAPL) is not None
    lines: list[str] = []

    def reconnect_logged() -> bool:
        lines.extend(line for line in logged(capfd).splitlines() if "no data received" in line)
        return bool(lines)

    asyncio.run(wait_until(reconnect_logged))
    assert re.search(r"Alpaca stream on feed iex \(ALPACA-stream-\d+\): no data received", lines[0])
    assert "[ERROR]" not in lines[0]  # a reconnect is a warning: a quiet feed fails nothing


def test_a_subscribe_made_before_a_late_reconnect_warning_is_sent_on_the_next_message(
    venue, alpaca, monkeypatch
) -> None:
    # Found by a second reader of the change: the node loop may run a subscribe hook after alpaca-py has
    # dropped a mute connection and before the relayed warning reaches the loop. The hook still sees the
    # old connection heard from, so marks nothing, and alpaca-py records the subscribe without sending it
    # while it reconnects (`_subscribe` sends only while `_running`). The warning, processed later, must
    # still have the next message send the client's subscriptions again.
    stream, socket = alpaca.memory_stream(monkeypatch)  # alpaca-py's own backoff, 0.5 to 1 s

    def build(**kwargs: object) -> object:
        stream._data_timeout = kwargs["data_timeout"]
        return stream

    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", build)
    gate = threading.Event()

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: client._stream_heard)
        socket.gate = gate
        try:
            # Blocking this loop stands in for a busy node: the reconnect's first send is held after
            # alpaca-py snapshotted its maps, with `_running` still false, and the warning that came
            # before it waits behind this coroutine.
            assert socket.holding.wait(5), "alpaca-py did not reconnect"
            # No second reconnect, whose replay of every handler map would send the trades by itself
            # (`_consume` reads `_data_timeout` on each pass); set while the reconnect is held.
            stream._data_timeout = None
            assert client._stream_heard
            await client._subscribe_trades(command(AAPL))
        finally:
            gate.set()
        await wait_until(lambda: stream._running)
        assert "AAPL" in stream._handlers["trades"]
        assert socket.wire["trades"] == set()  # the subscribe was recorded and not sent
        socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: socket.wire["trades"] == {"AAPL"})

    config = data_config(stream_data_timeout_secs=0.2, stream_mute_limit_secs=None)
    run_with_client(body, config=config)


def test_a_resend_planned_before_a_reconnect_warning_leaves_the_next_connection_owed_one(
    venue, alpaca
) -> None:
    # Found by the change's code review: a resend still waiting for the call ahead of it when alpaca-py
    # drops the connection may reach alpaca-py while it reconnects, recorded and not sent. Finishing it
    # must not clear what the reconnect warning marked.
    stream = venue.stream
    stream.trades = []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(lambda: client._stream_heard)
        await client._stream_calls.acquire()  # as a subscribe still inside alpaca-py
        try:
            client._stream_heard, client._resend_needed = False, True  # as after a lost subscribe
            handler = stream.quote_handlers["AAPL"]
            await asyncio.to_thread(
                asyncio.run, handler(alpaca.quote())
            )  # plans the resend; it waits
            await wait_until(lambda: client._resend_tries == 1)
            assert client._stream_thread is not None
            warning = "no data received on stock data stream for 900.0s, reconnecting"
            client._on_stream_log((client._stream_thread.name, "iex", logging.WARNING, warning))
        finally:
            client._stream_calls.release()
        await wait_until(
            lambda: stream.subscribes_sent == 1
        )  # the resend planned before the warning
        await asyncio.sleep(0.05)
        assert client._resend_needed  # the next connection's first message sends them again

    run_with_client(body)


def test_a_stream_mute_past_the_limit_is_stopped_and_named(venue, capfd) -> None:
    venue.stream.quotes, venue.stream.trades = [], []  # connected, subscribed, and sending nothing

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(venue.stream.stopped.is_set)
        await wait_until(venue.stream.finished.is_set)
        with pytest.raises(RuntimeError, match=r"Alpaca stream on feed iex went mute: .*reconnect"):
            await client._subscribe_trades(command(AAPL))

    config = data_config(stream_data_timeout_secs=0.05, stream_mute_limit_secs=0.2)
    run_with_client(body, config=config)

    log = logged(capfd)
    (line,) = [line for line in log.splitlines() if "went mute" in line and "[ERROR]" in line][:1]
    # What a session window reads as its stream's error: a line that begins with the feed.
    assert "Alpaca stream on feed iex went mute: no market data for" in line
    assert "stream_mute_limit_secs" in line
    assert "ended by itself" not in log


def test_the_mute_limit_counts_from_the_last_quote(venue, alpaca, monkeypatch, capfd) -> None:
    stream, socket = alpaca.memory_stream(monkeypatch)
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: stream)

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        for _ in range(8):  # a quote every 0.1 s for 0.8 s, past the limit four times over
            socket.deliver(alpaca.wire_quote())
            await asyncio.sleep(0.1)
        assert client._stream_failure is None
        await wait_until(lambda: client._stream_failure is not None)  # then nothing comes

    run_with_client(
        body, config=data_config(stream_data_timeout_secs=None, stream_mute_limit_secs=0.2)
    )

    assert "went mute" in logged(capfd)


def test_a_stream_holding_no_subscription_is_not_mute(venue, capfd) -> None:
    venue.stream.trades = []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await wait_until(lambda: client._stream_heard)
        await client._unsubscribe_quotes(command(AAPL))
        await asyncio.sleep(0.6)  # three limits with nothing subscribed
        assert client._stream_failure is None
        assert not venue.stream.stopped.is_set()

    run_with_client(
        body, config=data_config(stream_data_timeout_secs=None, stream_mute_limit_secs=0.2)
    )

    assert "went mute" not in logged(capfd)


def test_a_disconnect_cancels_the_mute_check(venue, capfd) -> None:
    venue.stream.quotes, venue.stream.trades = [], []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await client._disconnect()
        await asyncio.sleep(0.4)  # twice the limit after the disconnect

    run_with_client(
        body, config=data_config(stream_data_timeout_secs=None, stream_mute_limit_secs=0.2)
    )

    assert "went mute" not in logged(capfd)


def test_a_stopped_node_cannot_run_again_so_a_client_serves_one_run(venue) -> None:
    # The client forgets its subscriptions at disconnect. The engine keeps its own record of them until a
    # reset (`DataClientAdapter::clear_subscription_state`) and skips a subscribe it holds already, so a
    # connect again under one engine would leave them unsent. NautilusTrader v2.0.0rc8 gives a client to one
    # node run (the adapter guide, "A client instance and its bound provider belong to one node run"), and a
    # stopped node refuses to run again: this fails if upstream allows that.
    node = build_node(consumer(want_trades=False, bar_type=None))
    run(node, "hosted")

    with pytest.raises(RuntimeError, match="build a new node to run again"):
        run(node, "hosted")


def test_a_subscription_after_a_spell_with_none_starts_the_mute_count_afresh(venue, monkeypatch):
    # The code review: silence while nothing was subscribed is not owed by a later subscription.
    venue.stream.quotes, venue.stream.trades = [], []

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        await client._unsubscribe_quotes(command(AAPL))
        client._last_heard -= 10.0  # ten seconds with nothing subscribed and no check in between
        await client._subscribe_trades(command(AAPL))
        client._check_mute()
        assert client._stream_failure is None

    run_with_client(
        body, config=data_config(stream_data_timeout_secs=1.0, stream_mute_limit_secs=5.0)
    )


def test_a_subscription_lost_in_a_reconnect_alpaca_py_makes_is_sent_on_the_next_message(
    venue, alpaca, monkeypatch
) -> None:
    # The code review: alpaca-py's `data_timeout` reconnect sends its handler maps and only then counts as
    # running, the same window as its start; its warning shows the client a new connection is coming, so a
    # subscribe made before that connection's first message is sent again on it.
    stream, socket = alpaca.memory_stream(monkeypatch)
    stream._reconnect_min_backoff = 0.01

    def build(**kwargs: object) -> object:
        stream._data_timeout = kwargs["data_timeout"]
        return stream

    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", build)
    gate = threading.Event()

    async def body(client) -> None:
        await client._subscribe_quotes(command(AAPL))
        socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: client._stream_heard)
        socket.gate = gate  # holds the reconnect's send of the handler maps
        try:
            await wait_until(socket.holding.is_set)
            await wait_until(lambda: not client._stream_heard)  # the reconnect warning was seen
            await client._subscribe_trades(command(AAPL))  # in the window: recorded, not sent
        finally:
            gate.set()
        await wait_until(lambda: stream._running)
        assert socket.wire["trades"] == set()
        socket.deliver(alpaca.wire_quote())
        await wait_until(lambda: socket.wire["trades"] == {"AAPL"})

    run_with_client(
        body, config=data_config(stream_data_timeout_secs=0.2, stream_mute_limit_secs=None)
    )


def test_a_bound_too_large_for_a_float_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="stream_mute_limit_secs must be"):
        AlpacaDataClientConfig(stream_mute_limit_secs=10**400)


# -- prices finer than the instrument's precision (sip) -------------------------------------------------

SIP_PRICE = 747.086488
"""A consolidated-tape price finer than the instrument's four decimals, as the study's loader met it in a
SPY bar's high."""


def stream_lines(log: str, mark: str) -> list[str]:
    return [line for line in log.splitlines() if "Alpaca stream on feed " in line and mark in line]


def test_a_sip_trade_finer_than_the_precision_is_rounded_counted_and_the_stream_goes_on(
    venue, alpaca, capfd
) -> None:
    venue.stream.trades = [
        lambda: alpaca.trade(p=SIP_PRICE),
        lambda: alpaca.trade(p=SIP_PRICE, i=96922),
        lambda: alpaca.trade(i=96923),
        lambda: alpaca.trade(p=0.12345, i=96924),  # under a dollar, as extreme movers often are
    ]
    strategy = consumer(want_quotes=False, bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(lambda: len(strategy.seen_trades) == 4)

    run_with_client(body, strategy)

    prices = [str(t.price) for t in strategy.seen_trades]
    assert prices == ["747.0865", "747.0865", "126.5500", "0.1234"]
    log = logged(capfd)
    first = stream_lines(log, "rounded AAPL trade price")
    assert len(first) == 1, first  # the first per symbol, then counts at powers of ten
    assert "747.086488 to 747.0865" in first[0]
    assert "[ERROR]" not in first[0]  # never an error: a session window fails on one
    (summary,) = stream_lines(log, "rounded 3 AAPL trade prices")
    assert "[ERROR]" not in summary


def test_a_quote_finer_than_the_precision_is_dropped_and_named_and_the_stream_goes_on(
    venue, alpaca, capfd
) -> None:
    venue.stream.quotes = [lambda: alpaca.quote(ap=SIP_PRICE), alpaca.quote]
    strategy = consumer(want_trades=False, bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(lambda: len(strategy.seen_quotes) == 1)

    run_with_client(body, strategy)

    (tick,) = strategy.seen_quotes
    assert tick.ask_price == Price.from_str("87.6800")
    log = logged(capfd)
    (first,) = stream_lines(log, "dropped AAPL quote")
    assert "ask price 747.086488 is finer than price precision 4" in first
    assert "[ERROR]" not in first
    (summary,) = stream_lines(log, "dropped 1 AAPL quotes")
    assert "[ERROR]" not in summary
    assert "Dropped Alpaca quote" not in log


def test_a_sip_bar_finer_than_the_precision_is_rounded_and_counted(venue, alpaca, capfd) -> None:
    venue.historical.bars = [
        {**alpaca.BAR_AAPL, "o": 747.05, "h": SIP_PRICE, "l": 747.0, "c": 747.08},
        alpaca.BAR_AAPL,
    ]
    strategy = consumer(want_quotes=False, want_trades=False)

    run(build_node(strategy), "hosted")

    assert [str(b.high) for b in strategy.seen_bars] == ["747.0865", "178.2600"]
    ((_, reason),) = strategy.seen_responses
    assert reason is None
    log = logged(capfd)
    assert "Rounded 1 of 2 Alpaca bars for AAPL" in log
    assert "high 747.086488 to 747.0865" in log


def test_each_price_count_is_logged_at_its_own_powers_of_ten(venue, alpaca, capfd) -> None:
    # A quote dropped once, then ten trades rounded: the tenth logs the trades' count, not the quotes'.
    venue.stream.quotes = [lambda: alpaca.quote(ap=SIP_PRICE), alpaca.quote]
    venue.stream.trades = [lambda i=i: alpaca.trade(p=SIP_PRICE, i=97000 + i) for i in range(10)]
    strategy = consumer(bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(lambda: strategy.seen_quotes and len(strategy.seen_trades) == 10)

    run_with_client(body, strategy)

    log = logged(capfd)
    assert len(stream_lines(log, "rounded 10 AAPL trade prices")) == 2  # at ten, and at disconnect
    assert len(stream_lines(log, "dropped 1 AAPL quotes")) == 1  # at disconnect only


def test_unsubscribing_quotes_logs_the_quote_count_and_leaves_the_trade_count(
    venue, alpaca, capfd
) -> None:
    venue.stream.quotes = [lambda: alpaca.quote(ap=SIP_PRICE), alpaca.quote]
    venue.stream.trades = [
        lambda: alpaca.trade(p=SIP_PRICE),
        lambda: alpaca.trade(p=SIP_PRICE, i=96922),
    ]
    strategy = consumer(bar_type=None, auto_shutdown=False)

    async def body(client) -> None:
        await wait_until(lambda: strategy.seen_quotes and len(strategy.seen_trades) == 2)
        await client._unsubscribe_quotes(command(AAPL))
        await wait_until(lambda: "AAPL" not in client._quote_symbols)  # settled

    run_with_client(body, strategy)

    log = logged(capfd)
    assert len(stream_lines(log, "dropped 1 AAPL quotes")) == 1  # at the unsubscribe, not again
    assert len(stream_lines(log, "rounded 2 AAPL trade prices")) == 1  # at disconnect only


# -- whole-market minute bars and trading statuses on the stream (ADR 0090) -----------------------------

MINUTE = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
MSFT = InstrumentId.from_str("MSFT.ALPACA")
MSFT_MINUTE = BarType.from_str("MSFT.ALPACA-1-MINUTE-LAST-EXTERNAL")


class Watcher(Strategy):
    """Subscribe bars and statuses as configured, and record what arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.bar_types: list[BarType] = []
        self.status_ids: list[InstrumentId] = []
        self.params: dict | None = None
        self.seen_bars: list = []
        self.seen_statuses: list = []

    def on_start(self) -> None:
        if self.bar_types:
            self.subscribe_bars(self.bar_types, params=self.params)
        if self.status_ids:
            self.subscribe_instrument_status(self.status_ids, params=self.params)

    def on_bar(self, bar) -> None:
        self.seen_bars.append(bar)

    def on_instrument_status(self, status) -> None:
        self.seen_statuses.append(status)


def watcher(**options: object) -> Watcher:
    strategy = Watcher()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


@pytest.fixture
def market(venue, alpaca, monkeypatch) -> SimpleNamespace:
    """alpaca-py's real stream on an in-memory socket, with AAPL and MSFT both loaded."""
    stream, socket = alpaca.memory_stream(monkeypatch)
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: stream)
    msft = {**alpaca.ASSET_AAPL, "symbol": "MSFT", "id": "4ce9353c-66d1-46c2-898f-fce867ab0247"}
    venue.trading.assets = [alpaca.ASSET_AAPL, msft]
    config = AlpacaDataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA", "MSFT.ALPACA"])
    )
    return SimpleNamespace(stream=stream, socket=socket, config=config)


def test_a_minute_bar_subscription_hands_the_engine_the_streamed_bar(market, alpaca) -> None:
    strategy = watcher(bar_types=[MINUTE])

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"AAPL"})
        market.socket.deliver(alpaca.wire_bar())
        await wait_until(lambda: strategy.seen_bars)

    run_with_client(body, strategy, config=market.config)

    (bar,) = strategy.seen_bars
    assert bar.bar_type == MINUTE
    assert str(bar.open) == "178.2600" and str(bar.close) == "178.2100"
    assert bar.ts_event == dt_to_unix_nanos(datetime(2022, 1, 3, 9, tzinfo=UTC))  # at its open


def test_consecutive_streamed_bars_keep_receipt_times_separate_from_interval_ends(
    market, alpaca, monkeypatch
) -> None:
    # Arrival is 100 ms after each minute ends. The next minute still opens at
    # the previous interval's end, before its receipt; ts_init cannot be used as that boundary.
    strategy = watcher(bar_types=[MINUTE])
    clock = Clock.new_test()
    opens = [
        datetime(2022, 1, 3, 14, 30, tzinfo=UTC),
        datetime(2022, 1, 3, 14, 31, tzinfo=UTC),
    ]
    receipts = [
        dt_to_unix_nanos(datetime(2022, 1, 3, 14, 31, 0, 100_000, tzinfo=UTC)),
        dt_to_unix_nanos(datetime(2022, 1, 3, 14, 32, 0, 100_000, tzinfo=UTC)),
    ]

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"AAPL"})
        # Keep the real stream, handler and engine; restore the adapter's live clock before shutdown.
        with monkeypatch.context() as stamp:
            stamp.setattr(client, "clock", clock)
            for count, (opened, received) in enumerate(zip(opens, receipts, strict=True), start=1):
                clock.set_time(received)
                message = alpaca.wire_bar()
                message["t"] = SimpleNamespace(to_datetime=lambda at=opened: at)
                market.socket.deliver(message)
                await wait_until(lambda count=count: len(strategy.seen_bars) == count)

    cache = run_with_client(body, strategy, config=market.config)

    bars = strategy.seen_bars
    assert len(bars) == 2
    assert [bar.ts_event for bar in bars] == [dt_to_unix_nanos(opened) for opened in opens]
    assert [bar.ts_init for bar in bars] == receipts
    assert bars[0].ts_event + 60_000_000_000 == bars[1].ts_event < bars[0].ts_init
    cached = cache.bar(MINUTE)
    assert cached is not None and cached.ts_init == receipts[1]


def test_a_wildcard_bar_subscription_sends_star_and_hands_on_only_listed_symbols(
    market, alpaca
) -> None:
    strategy = watcher(bar_types=[MINUTE], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"*"})
        market.socket.deliver(alpaca.wire_bar(symbol="MSFT"), alpaca.wire_bar(symbol="ZZZZ"))
        market.socket.deliver(alpaca.wire_bar())
        await wait_until(lambda: strategy.seen_bars)
        await asyncio.sleep(0.1)

    cache = run_with_client(body, strategy, config=market.config)

    assert [b.bar_type for b in strategy.seen_bars] == [MINUTE]
    assert not cache.has_bars(MSFT_MINUTE)  # loaded, but not listed: nobody subscribed to it


def test_a_bar_type_the_stream_does_not_carry_is_refused_by_name(market, capfd) -> None:
    hourly = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")
    strategy = watcher(bar_types=[hourly])

    async def body(client) -> None:
        await asyncio.sleep(0.2)

    run_with_client(body, strategy, config=market.config)

    assert market.socket.held("bars") == set()
    log = logged(capfd)
    assert "Cannot subscribe: AAPL.ALPACA-1-HOUR-LAST-EXTERNAL" in log
    assert "1-MINUTE-LAST-EXTERNAL" in log


@pytest.mark.parametrize("asked", [False, True])
def test_updated_bars_are_handed_on_only_when_asked(market, alpaca, asked) -> None:
    strategy = watcher(bar_types=[MINUTE], params={"updated_bars": True} if asked else None)

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"AAPL"})
        assert market.socket.held("updatedBars") == ({"AAPL"} if asked else set())
        market.socket.deliver(alpaca.wire_bar())
        await wait_until(lambda: strategy.seen_bars)
        market.socket.deliver(alpaca.wire_bar("u", c=178.3, h=178.3))
        if asked:
            await wait_until(lambda: len(strategy.seen_bars) == 2)
        else:
            await asyncio.sleep(0.1)

    run_with_client(body, strategy, config=market.config)

    closes = [str(b.close) for b in strategy.seen_bars]
    assert closes == (["178.2100", "178.3000"] if asked else ["178.2100"])


@pytest.mark.parametrize(
    ("code", "action", "trading", "quoting", "restricted"),
    [
        ("2", MarketStatusAction.HALT, False, None, None),
        ("H", MarketStatusAction.HALT, False, None, None),
        ("P", MarketStatusAction.PAUSE, False, None, None),
        ("3", MarketStatusAction.TRADING, True, None, None),
        ("T", MarketStatusAction.TRADING, True, None, None),
        ("Q", MarketStatusAction.QUOTING, False, True, None),
        ("5", MarketStatusAction.NEW_PRICE_INDICATION, None, None, None),
        # Alpaca names the code only "Short Sale Restriction"; CTA keeps whether it starts or ends apart
        ("E", MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE, None, None, None),
        ("7", MarketStatusAction.NONE, None, None, None),
        ("F", MarketStatusAction.NONE, None, None, None),
    ],
)
def test_each_alpaca_status_code_becomes_an_instrument_status(
    market, alpaca, code, action, trading, quoting, restricted
) -> None:
    strategy = watcher(status_ids=[AAPL])

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("statuses") == {"AAPL"})
        market.socket.deliver(alpaca.wire_status(code, "T1"))
        await wait_until(lambda: strategy.seen_statuses)

    run_with_client(body, strategy, config=market.config)

    (status,) = strategy.seen_statuses
    assert status.instrument_id == AAPL
    assert status.action == action
    assert (status.is_trading, status.is_quoting, status.is_short_sell_restricted) == (
        trading,
        quoting,
        restricted,
    )
    assert status.trading_event == f"{code} status {code}"
    assert status.reason == "T1 reason T1"
    assert status.ts_event == dt_to_unix_nanos(datetime(2022, 1, 3, 15, 30, tzinfo=UTC))


def test_a_wildcard_status_subscription_hands_on_only_listed_symbols(market, alpaca) -> None:
    strategy = watcher(status_ids=[AAPL], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("statuses") == {"*"})
        market.socket.deliver(alpaca.wire_status("H", symbol="MSFT"))
        market.socket.deliver(alpaca.wire_status("H"))
        await wait_until(lambda: strategy.seen_statuses)
        await asyncio.sleep(0.1)

    run_with_client(body, strategy, config=market.config)

    assert [s.instrument_id for s in strategy.seen_statuses] == [AAPL]


def test_unsubscribing_the_last_listed_symbol_unsubscribes_the_wildcard(market) -> None:
    strategy = watcher(bar_types=[MINUTE, MSFT_MINUTE], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"*"})
        strategy.unsubscribe_bars(MINUTE, params={"wildcard": True})
        await asyncio.sleep(0.1)
        assert market.socket.held("bars") == {"*"}  # MSFT is still listed
        strategy.unsubscribe_bars(MSFT_MINUTE, params={"wildcard": True})
        await wait_until(lambda: market.socket.held("bars") == set())

    run_with_client(body, strategy, config=market.config)


def test_a_reconnect_sends_the_wildcard_again(market, alpaca, monkeypatch) -> None:
    market.stream._reconnect_min_backoff = 0.01  # alpaca-py waits 0.5 to 1 s first; shortened here

    def build(**kwargs: object) -> object:
        market.stream._data_timeout = kwargs["data_timeout"]
        return market.stream

    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", build)
    strategy = watcher(bar_types=[MINUTE], status_ids=[AAPL], params={"wildcard": True})

    def sent(channel: str) -> list:
        return [
            m[channel] for m in market.socket.sent if m["action"] == "subscribe" and channel in m
        ]

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("statuses") == {"*"})
        await wait_until(lambda: len(sent("bars")) >= 3)  # a reconnect replays both maps

    config = AlpacaDataClientConfig(
        instrument_provider=market.config.instrument_provider,
        stream_data_timeout_secs=0.1,
        stream_mute_limit_secs=None,
    )
    run_with_client(body, strategy, config=config)

    assert all(symbols == ["*"] for symbols in sent("bars"))


def test_a_resend_under_the_wildcard_sends_the_wildcard_and_no_listed_symbol(
    market, alpaca
) -> None:
    # Code review of M-a: the resend took a channel's first key, a listed symbol, which alpaca-py then
    # held, and Alpaca with it, apart from `"*"`.
    strategy = watcher(bar_types=[MINUTE], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"*"})
        sent = len(market.socket.sent)
        client._stream_heard, client._resend_needed = False, True  # as after a lost subscribe
        market.socket.deliver(alpaca.wire_bar())
        await wait_until(lambda: not client._resend_needed)
        assert len(market.socket.sent) > sent  # the resend went out
        assert set(market.stream._handlers["bars"]) == {"*"}

    run_with_client(body, strategy, config=market.config)

    subscribed = [
        m["bars"] for m in market.socket.sent if m["action"] == "subscribe" and "bars" in m
    ]
    assert all(symbols == ["*"] for symbols in subscribed), subscribed


def test_the_mute_limit_counts_the_whole_markets_messages_under_the_wildcard(
    market, alpaca, capfd
) -> None:
    # Code review of M-a: only a listed symbol's message reset the mute clock, so a busy wildcard
    # stream with a quiet listed symbol was stopped as mute.
    strategy = watcher(status_ids=[AAPL], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("statuses") == {"*"})
        # an unlisted status every 0.1 s for 0.8 s, past the limit four times over
        for _ in range(8):
            market.socket.deliver(alpaca.wire_status("H", symbol="MSFT"))
            await asyncio.sleep(0.1)
        assert client._stream_failure is None

    config = AlpacaDataClientConfig(
        instrument_provider=market.config.instrument_provider,
        stream_data_timeout_secs=None,
        stream_mute_limit_secs=0.2,
    )
    run_with_client(body, strategy, config=config)

    assert "went mute" not in logged(capfd)
    assert not strategy.seen_statuses


def test_a_channel_whose_unsubscribe_was_sent_is_forgotten_when_a_later_one_fails(
    market, monkeypatch, capfd
) -> None:
    # Code review of M-a: one command unsubscribes bars and updated bars; when the second failed,
    # both stayed recorded, so every retry asked alpaca-py to drop bars it no longer held (KeyError).
    strategy = watcher(bar_types=[MINUTE], params={"updated_bars": True})
    unsubscribe = SimpleNamespace(bar_types=[MINUTE], params=None)

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("updatedBars") == {"AAPL"})
        real = market.stream.unsubscribe_updated_bars

        def fail_once(*symbols: str) -> None:
            monkeypatch.setattr(market.stream, "unsubscribe_updated_bars", real)
            raise OSError("the socket closed")

        monkeypatch.setattr(market.stream, "unsubscribe_updated_bars", fail_once)
        with pytest.raises(OSError, match="the socket closed"):
            await client._unsubscribe_bars(unsubscribe)
        assert market.socket.held("bars") == set()
        await client._unsubscribe_bars(unsubscribe)  # the retry
        assert market.socket.held("updatedBars") == set()
        assert not client._bars.symbols and not client._updated_bars.symbols

    run_with_client(body, strategy, config=market.config)

    assert "KeyError" not in logged(capfd)


def test_a_listed_symbol_dropped_under_a_kept_wildcard_has_its_count_logged_and_reset(
    market, capfd
) -> None:
    # Code review of M-a: a symbol forgotten under a `"*"` others keep skipped the count's last line
    # and its reset, so a later subscription went on from the old total.
    strategy = watcher(bar_types=[MINUTE, MSFT_MINUTE], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("bars") == {"*"})
        client._rounded_bars["AAPL"] = 3
        await client._unsubscribe_bars(SimpleNamespace(bar_types=[MINUTE], params=None))
        assert "AAPL" not in client._rounded_bars
        assert market.socket.held("bars") == {"*"}  # MSFT still listed

    run_with_client(body, strategy, config=market.config)

    assert "rounded 3 AAPL bars to the instrument's price precision" in logged(capfd)


def test_a_mute_streams_error_counts_a_long_listing_rather_than_naming_every_symbol(
    market, capfd
) -> None:
    # Code review of M-a: the error named every listed symbol, one line of thousands of names with a
    # whole-market listing.
    strategy = watcher(status_ids=[AAPL], params={"wildcard": True})

    async def body(client) -> None:
        await wait_until(lambda: market.socket.held("statuses") == {"*"})
        star = client._statuses.symbols["*"]
        client._statuses.symbols.update({f"S{n:04d}": star for n in range(2000)})
        await wait_until(lambda: client._stream_failure is not None)
        for n in range(2000):  # not subscribed by the engine, so not unsubscribed at the stop
            del client._statuses.symbols[f"S{n:04d}"]

    config = AlpacaDataClientConfig(
        instrument_provider=market.config.instrument_provider,
        stream_data_timeout_secs=None,
        stream_mute_limit_secs=0.2,
    )
    run_with_client(body, strategy, config=config)

    lines = [line for line in logged(capfd).splitlines() if "went mute" in line]
    line = next(line for line in lines if "[ERROR]" in line and "no market data for" in line)
    assert all(len(line) < 2000 for line in lines)
    assert "statuses 2002 symbols" in line


# -- the trade-condition filter: the venue's bars rule (canon_alpaca.trade_conditions) ------------------


class LastTrade(Consumer):
    """Records every trade and stops at the one whose trade ID is `LAST_ID`."""

    def on_trade(self, trade) -> None:
        self.seen_trades.append(trade)
        if str(trade.trade_id) == LAST_ID and not self.finished:
            self.finished = True
            self.shutdown_system("the last trade arrived")


LAST_ID = "999"


def filtered_config(*, trade_condition_filter: bool = False) -> AlpacaDataClientConfig:
    return AlpacaDataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
        trade_condition_filter=trade_condition_filter,
    )


def last_trade() -> LastTrade:
    strategy = LastTrade()
    strategy.want_quotes, strategy.bar_type, strategy.auto_shutdown = False, None, False
    return strategy


def prints(alpaca, *changes: dict) -> list:
    return [functools.partial(alpaca.trade, **c) for c in changes]


USD = Currency.from_str("USD")
SIP = {"feed": "sip"}
"""The filter is measured on SIP prints only, so a filtered client's trades come from the sip feed."""


def sip_trades() -> LastTrade:
    strategy = last_trade()
    strategy.subscribe_params = SIP
    return strategy


def grown(before: dict[str, dict[str, int]], source: str) -> dict[str, int]:
    """What `condition_counts` gained for `source` since `before` (the tally is the process's)."""
    after = condition_counts().get(source, {})
    was = before.get(source, {})
    return {k: v - was.get(k, 0) for k, v in after.items() if v != was.get(k, 0)}


def test_the_trade_condition_filter_is_named_and_off_by_default(venue, alpaca) -> None:
    assert AlpacaDataClientConfig().trade_condition_filter is False
    assert AlpacaDataClientConfig().dict()["trade_condition_filter"] is False
    venue.stream.trades = prints(alpaca, {"c": ["@", "I"], "z": "C", "i": int(LAST_ID)})
    strategy = last_trade()
    run(build_node(strategy, config=filtered_config()), "hosted")
    assert [str(t.trade_id) for t in strategy.seen_trades] == [LAST_ID], "off: an odd lot is a tick"


def test_with_the_filter_on_only_prints_the_minute_bars_take_become_ticks(
    venue, alpaca, capfd
) -> None:
    venue.stream.trades = prints(
        alpaca,
        {"c": ["@", "I"], "z": "C", "i": 1},  # odd lot
        {"c": ["B"], "z": "A", "i": 2},  # average price, tape A
        {"c": ["@", "W"], "z": "C", "i": 3},  # average price, tape C
        {"c": ["@", "Z"], "z": "C", "i": 4},  # sold out of sequence
        {"c": ["@"], "z": "C", "i": 5},  # regular
        {"c": ["T"], "z": "A", "i": 6},  # extended hours, tape A
        {"c": ["@", "T"], "z": "C", "i": 7},  # extended hours, tape C
        {"c": [" "], "z": "B", "i": int(LAST_ID)},
    )
    strategy = sip_trades()
    node = build_node(strategy, config=filtered_config(trade_condition_filter=True))
    run(node, "hosted")
    assert [str(t.trade_id) for t in strategy.seen_trades] == ["5", "6", "7", LAST_ID]
    log = logged(capfd)
    assert "trade-condition filter: AAPL print with I (tape C) is no tick" in log


def test_with_the_filter_on_dropped_prints_are_counted_by_code(venue, alpaca) -> None:
    venue.stream.trades = prints(
        alpaca,
        {"c": ["@", "I"], "z": "C", "i": 1},
        {"c": ["@", "I"], "z": "C", "i": 2},
        {"c": ["@", "4", "I"], "z": "C", "i": 3},
        {"c": ["@", "?"], "z": "C", "i": 4},
        {"c": ["@"], "z": "C", "i": None},  # judged, but parse_trade makes no tick of it
        {"c": ["@"], "z": "C", "i": int(LAST_ID)},
    )
    strategy = sip_trades()
    before = condition_counts()
    config = filtered_config(trade_condition_filter=True)
    run(build_node(strategy, config=config), "hosted")
    assert [str(t.trade_id) for t in strategy.seen_trades] == [LAST_ID]
    assert grown(before, "stream") == {"kept": 1, "I": 3, "4": 1, "unknown:?": 1}
    assert grown(before, "request") == {}


def test_with_the_filter_on_a_code_the_venue_shows_that_the_table_does_not_know_refuses_the_connect(
    venue, alpaca, capfd
) -> None:
    venue.historical.conditions["C"]["?"] = "Something New"
    strategy = StartedLastTrade()
    strategy.want_quotes, strategy.bar_type, strategy.auto_shutdown = False, None, False
    strategy.subscribe_params = SIP
    node = filtered_node_with_execution(strategy)
    with pytest.raises(RuntimeError, match="readiness timeout"):
        run(node, "hosted")
    log = logged(capfd)
    assert "tape C '?' (Something New)" in log
    assert [p for p, _ in venue.historical.gets] == ["/stocks/meta/conditions/trade"] * 3
    assert strategy.started == [], "the strategy never started"
    assert venue.stream.wire == {"quotes": set(), "trades": set()}, "nothing was subscribed"


def test_with_the_filter_off_the_connect_asks_nothing_of_the_conditions_map(venue, alpaca) -> None:
    venue.stream.trades = prints(alpaca, {"c": ["@"], "z": "C", "i": int(LAST_ID)})
    strategy = last_trade()
    run(build_node(strategy, config=filtered_config()), "hosted")
    assert venue.historical.gets == []


class StartedLastTrade(LastTrade):
    """`LastTrade` that records each start, so a test can show the node never started it."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[bool] = []

    def on_start(self) -> None:
        self.started.append(True)
        super().on_start()


def test_a_started_strategy_records_its_start(venue, alpaca) -> None:
    """The recorder the refusal test relies on does record a start."""
    venue.stream.trades = prints(alpaca, {"c": ["@"], "z": "C", "i": int(LAST_ID)})
    strategy = StartedLastTrade()
    strategy.want_quotes, strategy.bar_type, strategy.auto_shutdown = False, None, False
    strategy.subscribe_params = SIP
    run(build_node(strategy, config=filtered_config(trade_condition_filter=True)), "hosted")
    assert strategy.started == [True]


def filtered_node_with_execution(strategy: Strategy) -> LiveNode:
    """A node like the sandbox node: the filtered data client and upstream's sandbox execution client."""
    node = (
        LiveNode.builder("CANON-ALPACA", TRADER, Environment.SANDBOX)
        .with_delay_post_stop_secs(0)
        .with_timeout_connection(2)
        .with_timeout_disconnection_secs(5)
        .add_data_client(
            "ALPACA", AlpacaDataClientFactory, filtered_config(trade_condition_filter=True)
        )
        .add_simulated_exec_client(
            "ALPACA",
            SandboxExecutionClientFactory(),
            SandboxExecutionClientConfig(
                venue=VENUE,
                starting_balances=[Money(100_000, USD)],
                base_currency=USD,
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
            ),
        )
        .build()
    )
    node.add_strategy(strategy)
    BUILT_NODES.append(node)
    return node


@pytest.mark.parametrize("feed", ["iex", None])
def test_with_the_filter_on_a_trade_subscription_on_any_feed_but_sip_is_refused(
    venue, alpaca, capfd, feed
) -> None:
    venue.stream.trades = prints(alpaca, {"c": ["@"], "z": "C", "i": int(LAST_ID)})
    strategy = last_trade()
    strategy.subscribe_params = None if feed is None else {"feed": feed}
    strategy.auto_shutdown = True
    with contextlib.suppress(Exception):
        run(build_node(strategy, config=filtered_config(trade_condition_filter=True)), "hosted")
    assert "trade_condition_filter is measured on the sip feed only" in logged(capfd)
    assert venue.stream.wire["trades"] == set()
    assert strategy.seen_trades == []


def test_with_the_filter_on_a_trade_request_keeps_only_prints_the_bars_take(trades_http) -> None:
    trades_http.pages = [
        trade_page(
            [
                historical_trade(0, c=["@", "I"]),  # odd lot
                historical_trade(1, c=["@", "W"]),  # average price
                historical_trade(2, c=["@"]),  # regular
            ],
            "second",
        ),
        trade_page([historical_trade(3, c=["@", "T"]), historical_trade(4, c=["@", "Z"])]),
    ]
    strategy = TradeRequester()
    strategy.params = SIP
    before = condition_counts()
    run(build_node(strategy, config=filtered_config(trade_condition_filter=True)), "hosted")
    assert [r.failure_reason for r in strategy.responses] == [None]
    assert [t.trade_id for t in strategy.seen] == [TradeId("1002"), TradeId("1003")]
    assert grown(before, "request") == {"kept": 2, "I": 1, "W": 1, "Z": 1}
    assert grown(before, "stream") == {}


def test_with_the_filter_on_a_left_out_print_uses_none_of_a_requests_limit(trades_http) -> None:
    trades_http.pages = [
        trade_page(
            [
                historical_trade(0, c=["@", "I"]),
                historical_trade(1),
                historical_trade(2, c=["@", "I"]),
            ],
            "second",
        ),
        trade_page([historical_trade(3)], "unneeded"),
    ]
    strategy = TradeRequester()
    strategy.params, strategy.limit = SIP, 2
    run(build_node(strategy, config=filtered_config(trade_condition_filter=True)), "hosted")
    assert [r.failure_reason for r in strategy.responses] == [None]
    assert [t.trade_id for t in strategy.seen] == [TradeId("1001"), TradeId("1003")]
    assert [c["limit"] for c in trades_http.calls] == [2, 1]


@pytest.mark.parametrize(
    ("params", "refusal"),
    [
        ({"feed": "iex"}, "measured on the sip feed only"),
        ({"feed": "sip", "update_catalog": True}, "request with update_catalog off"),
    ],
)
def test_with_the_filter_on_a_trade_request_off_sip_or_into_the_catalog_is_refused(
    trades_http, params, refusal
) -> None:
    strategy = TradeRequester()
    strategy.params = params
    run(build_node(strategy, config=filtered_config(trade_condition_filter=True)), "hosted")
    assert [refusal in (r.failure_reason or "") for r in strategy.responses] == [True]
    assert trades_http.calls == [], "no trade was asked for"


# -- late streamed prints set aside (`validate_trade_sequence`) ------------------------------------------

PRINT_BASE = datetime(2021, 2, 22, 15, 51, 44, tzinfo=UTC)
SEQUENCE_LINE = "Alpaca trade sequence on feed "


def at(ms: int, trade_id: int, **changes: object) -> dict:
    """A streamed print's fields: stamped by Alpaca `ms` after `PRINT_BASE`, with its own trade ID."""
    return {"t": PRINT_BASE + timedelta(milliseconds=ms), "i": trade_id, **changes}


def sequenced_config(*, trade_condition_filter: bool = False) -> AlpacaDataClientConfig:
    return AlpacaDataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
        validate_trade_sequence=True,
        trade_condition_filter=trade_condition_filter,
    )


def emit_trades(client, alpaca, *fields: dict) -> None:
    """Hand the client streamed prints of AAPL in this order, as its subscription's handler does."""
    for changes in fields:
        client._emit_trade(client._trade_symbols["AAPL"], alpaca.trade(**changes))


def sequence_lines(log: str, mark: str) -> list[str]:
    return [line for line in log.splitlines() if SEQUENCE_LINE in line and mark in line]


def trades_only() -> Consumer:
    return consumer(want_quotes=False, bar_type=None, auto_shutdown=False)


@pytest.fixture(autouse=True)
def own_late_tally(monkeypatch) -> None:
    """Each test starts with an empty tally of prints set aside, and leaves none to the next: the tally
    is the process's, and a window's record takes whatever it holds."""
    monkeypatch.setattr(canon_alpaca.data, "_LATE_PRINTS", {})


def test_the_trade_sequence_check_is_named_and_off_by_default(venue, alpaca, capfd) -> None:
    assert AlpacaDataClientConfig().validate_trade_sequence is False
    assert AlpacaDataClientConfig().dict()["validate_trade_sequence"] is False
    venue.stream.trades = []
    strategy = trades_only()

    async def body(client) -> None:
        await wait_until(lambda: "AAPL" in client._trade_symbols)
        emit_trades(client, alpaca, at(2000, 1), at(1000, 2))
        await wait_until(lambda: len(strategy.seen_trades) == 2)

    run_with_client(body, strategy)

    # off: every print is a tick in the order it arrived, as NautilusTrader's data engine takes trades
    assert [str(t.trade_id) for t in strategy.seen_trades] == ["1", "2"]
    assert late_prints_set_aside() == {}
    assert sequence_lines(logged(capfd), "") == []


def test_with_the_check_on_a_print_stamped_before_the_newest_passed_on_is_set_aside_and_counted(
    venue, alpaca, capfd
) -> None:
    venue.stream.trades = []
    strategy = trades_only()

    async def body(client) -> None:
        await wait_until(lambda: "AAPL" in client._trade_symbols)
        emit_trades(
            client,
            alpaca,
            at(0, 1),
            at(2000, 2),
            at(750, 3, s=300),  # 1250 ms behind the newest passed on
            at(2000, 4),  # the newest's own time: passed on
            at(1999, 5, s=25),  # 1 ms behind
            at(3000, 6),
        )
        await wait_until(lambda: len(strategy.seen_trades) == 4)

    cache = run_with_client(body, strategy, config=sequenced_config())

    assert [str(t.trade_id) for t in strategy.seen_trades] == ["1", "2", "4", "6"]
    # nothing downstream saw the two: not the strategy, and not the cache the emulator and Mover read
    assert sorted(str(t.trade_id) for t in cache.trades(AAPL)) == ["1", "2", "4", "6"]
    assert str(cache.trade(AAPL).trade_id) == "6"
    # by name, with the lateness, and with the shares a bar built from ticks will not hold
    assert late_prints_set_aside() == {
        "AAPL": {"count": 2, "most_late_ms": 1250, "shares": 325, "stamped_ahead": 0}
    }
    log = logged(capfd)
    (first,) = sequence_lines(log, "set aside a late AAPL print")
    assert "1250 ms before the newest passed on" in first
    assert "[ERROR]" not in first  # never an error: a session window fails on one
    (summary,) = sequence_lines(log, "2 late AAPL prints set aside")  # at disconnect
    assert "at most 1250 ms" in summary
    assert "[ERROR]" not in summary
    # not among the lines a session window keeps about the stream: it keeps twenty, and looks for the
    # stream's own errors only in those
    assert stream_lines(log, "late AAPL print") == []


def test_the_late_print_count_restarts_with_the_subscription_and_the_newest_stamp_does_not(
    venue, alpaca, capfd
) -> None:
    venue.stream.trades = []

    async def body(client) -> None:
        await client._subscribe_trades(command(AAPL))
        emit_trades(client, alpaca, at(5000, 1), *[at(n, 10 + n) for n in range(10)])
        await client._unsubscribe_trades(command(AAPL))
        await wait_until(lambda: "AAPL" not in client._trade_symbols)
        await client._subscribe_trades(command(AAPL))
        # stamped before what was passed on under the first subscription: still late
        emit_trades(client, alpaca, at(4000, 30), at(6000, 31))

    run_with_client(body, config=sequenced_config())

    assert late_prints_set_aside()["AAPL"]["count"] == 11
    log = logged(capfd)
    assert (
        len(sequence_lines(log, "set aside a late AAPL print")) == 2
    )  # the first of each subscription
    assert (
        len(sequence_lines(log, "10 late AAPL prints set aside")) == 2
    )  # at ten, at the unsubscribe
    assert len(sequence_lines(log, " 1 late AAPL prints set aside")) == 1  # at disconnect, afresh


def test_prints_are_ordered_by_their_own_time_when_the_clock_runs_behind_alpacas(
    venue, alpaca
) -> None:
    """A node clock behind Alpaca's stamps puts every print's own time ahead of its receipt. Ordered by
    the earlier of the two, prints would be ordered by receipt, which only rises, and none would ever
    be late."""
    venue.stream.trades = []
    strategy = trades_only()

    async def body(client) -> None:
        await wait_until(lambda: "AAPL" in client._trade_symbols)
        now = datetime.now(UTC)
        emit_trades(
            client,
            alpaca,
            {"t": now + timedelta(milliseconds=800), "i": 1},
            {
                "t": now + timedelta(milliseconds=300),
                "i": 2,
            },  # older by its own time, received later
            {"t": now + timedelta(milliseconds=900), "i": 3},
        )
        await wait_until(lambda: len(strategy.seen_trades) == 2)

    run_with_client(body, strategy, config=sequenced_config())

    assert [str(t.trade_id) for t in strategy.seen_trades] == ["1", "3"]
    assert late_prints_set_aside()["AAPL"] == {
        "count": 1,
        "most_late_ms": 500,
        "shares": 1,
        "stamped_ahead": 0,
    }


def test_a_print_stamped_far_ahead_of_the_clock_is_passed_on_counted_and_never_the_newest(
    venue, alpaca, capfd
) -> None:
    venue.stream.trades = []
    strategy = trades_only()

    async def body(client) -> None:
        await wait_until(lambda: "AAPL" in client._trade_symbols)
        in_flight = datetime.now(UTC)  # a print made before the next one is received
        emit_trades(client, alpaca, {"t": datetime(2099, 1, 1, tzinfo=UTC), "i": 1})
        emit_trades(client, alpaca, {"t": in_flight, "i": 2})
        await asyncio.sleep(0.02)
        emit_trades(client, alpaca, {"t": datetime.now(UTC), "i": 3})
        await wait_until(lambda: len(strategy.seen_trades) == 3)

    run_with_client(body, strategy, config=sequenced_config())

    # it has no usable time: it goes out as with the check off, and holds back neither the print that
    # was already on its way nor the ones after
    assert [str(t.trade_id) for t in strategy.seen_trades] == ["1", "2", "3"]
    assert late_prints_set_aside() == {
        "AAPL": {"count": 0, "most_late_ms": 0, "shares": 0, "stamped_ahead": 1}
    }
    log = logged(capfd)
    (line,) = sequence_lines(log, "ahead of its receipt")
    assert "AAPL" in line
    assert "[ERROR]" not in line
    (summary,) = sequence_lines(log, "1 stamped ahead of the clock")  # at disconnect
    assert "0 late AAPL prints set aside" in summary


def test_a_print_the_condition_filter_leaves_out_does_not_move_the_newest_stamp(
    venue, alpaca
) -> None:
    venue.stream.trades = prints(
        alpaca,
        at(
            5000, 1, c=["@", "I"]
        ),  # an odd lot: no tick with the filter on, so nothing was passed on
        at(1000, 2, c=["@"]),
        at(2000, int(LAST_ID), c=["@"]),
    )
    strategy = sip_trades()

    run(build_node(strategy, config=sequenced_config(trade_condition_filter=True)), "hosted")

    assert [str(t.trade_id) for t in strategy.seen_trades] == ["2", LAST_ID]
    assert late_prints_set_aside() == {}


def test_the_condition_tally_counts_as_kept_only_the_prints_that_became_ticks(
    venue, alpaca
) -> None:
    before = condition_counts()
    venue.stream.trades = prints(
        alpaca,
        at(2000, 1, c=["@"]),
        at(1000, 2, c=["@"]),  # the filter takes it, the sequence check sets it aside: no tick
        at(3000, int(LAST_ID), c=["@"]),
    )
    strategy = sip_trades()

    run(build_node(strategy, config=sequenced_config(trade_condition_filter=True)), "hosted")

    assert [str(t.trade_id) for t in strategy.seen_trades] == ["1", LAST_ID]
    assert grown(before, "stream") == {"kept": 2}
    assert late_prints_set_aside()["AAPL"]["count"] == 1


def test_prints_are_late_only_against_their_own_symbol() -> None:
    sequence = TradeSequence()
    assert sequence.late_by("AAPL", 5_000_000) is None
    assert sequence.late_by("MSFT", 3_000_000) is None  # older than AAPL's newest, and MSFT's first
    assert sequence.late_by("AAPL", 4_999_999) == 1
    assert sequence.late_by("AAPL", 5_000_000) is None  # the newest's own stamp
    assert sequence.late_by("MSFT", 2_000_000) == 1_000_000
    assert sequence.late_by("AAPL", 4_000_000) == 1_000_000  # a late one never becomes the newest


@pytest.mark.parametrize(
    ("ts_event", "ts_init", "stamp"),
    [
        (10, 20, 10),
        (20, 10, 20),
        (7, 7, 7),
        (10 + STAMP_AHEAD_MOST_NS, 10, 10 + STAMP_AHEAD_MOST_NS),
        (11 + STAMP_AHEAD_MOST_NS, 10, None),
    ],
    ids=[
        "its own time",
        "its own time, when the clock runs behind Alpaca's",
        "either",
        "its own time at the bound",
        "none beyond the bound",
    ],
)
def test_a_prints_stamp_is_its_own_time_unless_that_is_far_ahead_of_its_receipt(
    ts_event, ts_init, stamp
) -> None:
    tick = cast("TradeTick", SimpleNamespace(ts_event=ts_event, ts_init=ts_init))
    assert trade_stamp(tick) == stamp


def test_the_tally_shows_a_lateness_under_a_millisecond_as_one() -> None:
    assert late_ms(1) == 1
    assert late_ms(1_000_000) == 1
    assert late_ms(1_000_001) == 2
    assert late_ms(1_250_000_000) == 1250
