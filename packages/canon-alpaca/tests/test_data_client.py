"""AlpacaDataClient inside a real LiveNode, offline: factory registration, data delivery and shutdown.

The shape follows upstream's template tests (`python/tests/integration/test_python_adapter_template.py` at
the pinned NautilusTrader commit): the node is built with the adapter's own factory, through both
registration paths the adapter guide documents, a strategy drives it, and the node shuts itself down, on
both the owned (`node.run()`) and hosted (`await node.run_async()`) launch modes. There is no fill test:
this slice has no execution client.

The three alpaca-py constructors the adapter calls are replaced by recorders that hand out the stand-ins
in conftest.py, so the real factory, `_connect` and credential lookup run. The environment holds
placeholder strings, not credentials.
"""

import asyncio
import threading
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import canon_alpaca.data
import canon_alpaca.providers
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrameUnit
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
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
TRADER = TraderId("TESTER-001")
PLACEHOLDER = "offline-placeholder"


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
        self.unsubscribe_on_stop = False
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
            self.request_id = self.request_bars(self.bar_type, start=START, end=END)

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

    def on_stop(self) -> None:
        if self.unsubscribe_on_stop:
            self.unsubscribe_quotes(AAPL)
            self.unsubscribe_trades(AAPL)  # never subscribed: the hook must tolerate it

    def _check(self) -> None:
        done = (
            (self.seen_quotes or not self.want_quotes)
            and (self.seen_trades or not self.want_trades)
            and (self.seen_responses or self.bar_type is None)
        )
        if done and not self.finished:
            self.finished = True
            self.shutdown_system("Alpaca data checked")


def consumer(**options: object) -> Consumer:
    strategy = Consumer()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


def build_node(strategy: Strategy, registration: str = "config", delay_post_stop_secs: int = 0):
    config = DataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"])
    )
    if registration == "config":
        node = LiveNode.build(
            "CANON-ALPACA",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TRADER,
                timeout_connection_secs=2,
                timeout_disconnection_secs=5,
                delay_post_stop_secs=delay_post_stop_secs,
                data_clients={"ALPACA": config},
            ),
            data_factories={"ALPACA": AlpacaDataClientFactory},
        )
    else:
        node = (
            LiveNode.builder("CANON-ALPACA", TRADER, Environment.SANDBOX)
            .with_delay_post_stop_secs(delay_post_stop_secs)
            .with_timeout_connection(2)
            .with_timeout_disconnection_secs(5)
            .add_data_client("ALPACA", AlpacaDataClientFactory, config)
            .build()
        )
    node.add_strategy(strategy)
    return node


async def run_hosted(node) -> None:
    async with asyncio.timeout(15):
        await node.run_async()


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
    assert request.start == START.replace(tzinfo=None)  # alpaca-py stores naive UTC
    assert request.end == END.replace(tzinfo=None)
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


def test_unsubscribe_on_stop_tolerates_members_never_subscribed(venue) -> None:
    strategy = consumer(want_trades=False, bar_type=None, unsubscribe_on_stop=True)

    run(build_node(strategy, delay_post_stop_secs=1), "hosted")

    assert venue.stream.unsubscribed == [("quotes", ("AAPL",))]
    assert venue.stream.stopped.is_set()


def test_stream_ending_while_subscribed_is_logged(venue, capfd) -> None:
    venue.stream.ends_on_its_own = True
    strategy = consumer(want_trades=False, bar_type=None)

    run(build_node(strategy), "hosted")

    assert len(strategy.seen_quotes) == 1
    assert "Alpaca stream ended while subscribed" in capfd.readouterr().err


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
    node = build_node(Consumer())

    with pytest.raises(RuntimeError, match="readiness timeout"):
        run(node, "hosted")

    assert built == []
    assert (
        f"set {ENV_API_KEY} and {ENV_SECRET_KEY}" in capfd.readouterr().err
    )  # ERROR goes to stderr
