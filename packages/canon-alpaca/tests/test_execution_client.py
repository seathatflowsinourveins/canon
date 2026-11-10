"""AlpacaExecutionClient inside a real LiveNode, offline: factory registration, fills, cancels, rejections,
reconciliation, the paper-only refusals and shutdown.

The shape follows upstream's template tests (`python/tests/integration/test_python_adapter_template.py` at
the pinned NautilusTrader commit): the node is built with the adapter's own factories through both
registration paths the adapter guide documents, a strategy submits an order on start, and the node shuts
itself down once the order's last event has arrived, on both the owned and hosted launch modes.

The node also runs the adapter's data client: NautilusTrader's risk engine denies a market order while it
has no price for the instrument (`MARKET_PRICE_UNAVAILABLE`, observed), so the strategy subscribes to
quotes and submits on the first one, as a strategy on a real node would. The alpaca-py constructors the
adapter calls are replaced by recorders that hand out the stand-ins in conftest.py, so the real factories,
`_connect`, credential lookup and reconciliation run. The environment holds placeholder strings, not
credentials.
"""

import asyncio
import socket
import threading
import time
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import canon_alpaca.data
import canon_alpaca.execution
import canon_alpaca.providers
import pytest
import requests
from alpaca.common.rest import RESTClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.requests import StopOrderRequest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import USD
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import fill_report
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.common import Logger
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
from nautilus_trader.model import AccountId
from nautilus_trader.model import ClientId
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId
from nautilus_trader.trading import Strategy

AAPL = InstrumentId.from_str("AAPL.ALPACA")
TRADER = TraderId("TESTER-001")
FINAL_EVENTS = {"OrderDenied", "OrderRejected", "OrderCanceled", "OrderExpired"}
PLACEHOLDER = "offline-placeholder"
LOG_PROBE_LINES = 20_000


@pytest.fixture
def venue(monkeypatch, alpaca) -> SimpleNamespace:
    """Replace alpaca-py's trading constructors with recorders that return the stand-ins."""
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    trading = alpaca.OrdersClient()
    stream = alpaca.TradeStream()
    trading.stream = stream
    ns = SimpleNamespace(
        trading=trading,
        stream=stream,
        historical=alpaca.HistoricalClient(),
        quotes=alpaca.Stream(),
        built={},
    )

    def recorder(name: str, instance: object):
        def build(**kwargs: object) -> object:
            ns.built[name] = kwargs
            return instance

        return build

    monkeypatch.setattr(canon_alpaca.providers, "TradingClient", recorder("assets", trading))
    monkeypatch.setattr(canon_alpaca.execution, "TradingClient", recorder("trading", trading))
    monkeypatch.setattr(canon_alpaca.execution, "TradingStream", recorder("stream", stream))
    monkeypatch.setattr(
        canon_alpaca.data, "StockHistoricalDataClient", recorder("historical", ns.historical)
    )
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", recorder("quotes", ns.quotes))
    return ns


class Trader(Strategy):
    """Submit one order on the first quote (and cancel it on `cancel_on`, if set), record every order
    event, and shut the node down when the order reaches a final state.

    `Strategy` is a native class whose `__new__` takes no extra arguments, so `trader()` sets the options.
    """

    def __init__(self) -> None:
        super().__init__()
        self.order_type = "market"
        self.side = OrderSide.BUY
        self.quantity = 2
        self.price = "150.00"
        self.time_in_force = TimeInForce.GTC
        self.trigger_price = "200.00"  # for stop and stop-limit orders
        self.trigger_type: TriggerType | None = None  # None leaves the factory's default
        self.post_only = False
        self.params: dict | None = (
            None  # the submit's params, as `Strategy.submit_order` takes them
        )
        self.cancel_on: str | None = None  # the event name on which to cancel the order
        self.submit = True
        self.idle = False  # neither submits nor stops: the test stops the node
        self.started = False  # set in on_start, after the node's startup reconciliation
        self.events: list = []
        self.client_order_id: ClientOrderId | None = None
        self.stopped_for: str | None = None
        self.filled = 0.0

    def on_start(self) -> None:
        self.started = True
        if self.idle:
            return
        if not self.submit:
            self.shutdown_system("nothing to submit")
            return
        self.subscribe_quotes(AAPL)

    def on_quote(self, quote) -> None:
        if self.client_order_id is not None:
            return
        factory = self.order_factory
        if self.order_type == "market":
            order = factory.market(
                AAPL, self.side, Quantity.from_int(self.quantity), time_in_force=self.time_in_force
            )
        elif self.order_type == "limit":
            order = factory.limit(
                AAPL,
                self.side,
                Quantity.from_int(self.quantity),
                Price.from_str(self.price),
                time_in_force=self.time_in_force,
            )
        elif self.order_type == "stop":
            order = factory.stop_market(
                AAPL,
                self.side,
                Quantity.from_int(self.quantity),
                Price.from_str(self.trigger_price),
                trigger_type=self.trigger_type,
                time_in_force=self.time_in_force,
            )
        elif self.order_type == "stop_limit":
            order = factory.stop_limit(
                AAPL,
                self.side,
                Quantity.from_int(self.quantity),
                Price.from_str(self.price),
                Price.from_str(self.trigger_price),
                trigger_type=self.trigger_type,
                time_in_force=self.time_in_force,
                post_only=self.post_only,
            )
        else:
            order = factory.trailing_stop_market(
                AAPL,
                self.side,
                Quantity.from_int(self.quantity),
                trailing_offset=Decimal("1.00"),
                trailing_offset_type=TrailingOffsetType.PRICE,
                trigger_price=Price.from_str(self.trigger_price),
            )
        self.client_order_id = order.client_order_id
        self.submit_order(order, params=self.params)

    def on_order_event(self, event) -> None:
        # The cache is borrowed while order events are dispatched, so the order's end is read from them.
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderFilled":
            self.filled += event.last_qty.as_double()
        if name == self.cancel_on:
            self.cancel_order(event.client_order_id)
        finished = name in FINAL_EVENTS or (name == "OrderFilled" and self.filled >= self.quantity)
        if finished and self.stopped_for is None:
            self.stopped_for = name
            self.shutdown_system(f"order closed with {name}")

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


def trader(**options: object) -> Trader:
    strategy = Trader()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


BUILT_NODES: list[LiveNode] = []


@pytest.fixture(autouse=True)
def dispose_nodes():
    """Dispose of every node a test built (one `LiveNode` per thread; see test_data_client.py)."""
    yield
    while BUILT_NODES:
        BUILT_NODES.pop().dispose()


def provider_config() -> InstrumentProviderConfig:
    return InstrumentProviderConfig(load_ids=[str(AAPL)])


def exec_config() -> AlpacaExecutionClientConfig:
    return AlpacaExecutionClientConfig(instrument_provider=provider_config())


def build_node(
    strategy: Strategy,
    registration: str = "config",
    exec_factory: type[AlpacaExecutionClientFactory] = AlpacaExecutionClientFactory,
) -> LiveNode:
    if registration == "config":
        node = LiveNode.build(
            "CANON-ALPACA-EXEC",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TRADER,
                timeout_connection_secs=2,
                timeout_reconciliation_secs=5,
                timeout_disconnection_secs=5,
                delay_post_stop_secs=0,
                data_clients={"ALPACA": DataClientConfig(instrument_provider=provider_config())},
                exec_clients={"ALPACA": exec_config()},
            ),
            data_factories={"ALPACA": AlpacaDataClientFactory},
            exec_factories={"ALPACA": exec_factory},
        )
    else:
        node = (
            LiveNode.builder("CANON-ALPACA-EXEC", TRADER, Environment.SANDBOX)
            .with_delay_post_stop_secs(0)
            .with_timeout_connection(2)
            .with_timeout_disconnection_secs(5)
            .add_data_client(
                "ALPACA",
                AlpacaDataClientFactory,
                DataClientConfig(instrument_provider=provider_config()),
            )
            .add_exec_client("ALPACA", exec_factory, exec_config())
            .build()
        )
    node.add_strategy(strategy)
    BUILT_NODES.append(node)
    return node


async def run_hosted(node: LiveNode) -> None:
    async with asyncio.timeout(15):
        await node.run_async()


def run(node: LiveNode, launch: str = "hosted") -> None:
    if launch == "owned":
        node.run()
    else:
        asyncio.run(run_hosted(node))


def run_with_client(
    body: Callable[[AlpacaExecutionClient], Awaitable[None]],
    strategy: Trader | CancelAll | Modifier | None = None,
) -> None:
    """Run a hosted node, await `body` with its connected execution client once the node has started its
    strategy, then stop the node.

    The node connects the client and then runs its own startup reconciliation, which asks the client for
    order, fill and position reports; `body` would race it. A strategy's `on_start` comes after that
    reconciliation: the reconciliation page at the pinned NautilusTrader commit
    (`docs/concepts/execution/reconciliation.md`, "Startup reconciliation" and "Startup position
    validation") has a failed startup stop "before actor or strategy `on_start`", and the order is
    observed in these tests. The default strategy is idle; with another, `body` can wait for what the
    strategy set off.
    """
    clients: list[AlpacaExecutionClient] = []
    strategy = strategy or trader(idle=True)

    class Capturing(AlpacaExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaExecutionClientFactory.create(**kwargs)
            clients.append(client)
            return client

    node = build_node(strategy, exec_factory=Capturing)

    async def main() -> None:
        async with asyncio.timeout(15):
            task = asyncio.ensure_future(node.run_async())
            try:
                while not (
                    task.done() or (clients and clients[0].is_connected and strategy.started)
                ):
                    await asyncio.sleep(0.005)
                if task.done():
                    task.result()
                await body(clients[0])
            finally:
                if not task.done():
                    node.handle().stop()
                await task

    asyncio.run(main())


async def until_idle(client: AlpacaExecutionClient, venue: SimpleNamespace) -> None:
    """Wait until the venue stand-in has answered every call and sent every update, and the node's loop
    has run what they handed it."""
    async with asyncio.timeout(10):
        while (
            client._rest_lock.locked()
            or not venue.stream._messages.empty()
            or not client._updates.empty()
            or client._applying
        ):
            await asyncio.sleep(0.01)
        for _ in range(
            20
        ):  # the stream thread's last hand-off, and the hook after the worker returns
            await asyncio.sleep(0.01)


def logged(capfd) -> str:
    """Everything the node logged, whichever of stdout and stderr each level goes to.

    The node's logger writes on its own thread, fed by a channel, so a line logged just before the node
    stopped can still be queued when the run returns. `logging_sync_to_disk` queues a sync behind every
    line already sent and blocks until the writer thread has flushed stdout and stderr and acknowledged it
    (`crates/common/src/logging/logger.rs` at NautilusTrader v2.0.0rc8, `d53849b2`, `LogEvent::Sync`), as
    `test_data_client.py`'s `logged` does; `Logger.flush` only queues a flush. It returns at once when the
    logger is not running; `test_logged_waits_for_lines_still_queued_after_a_run` fails if so.
    """
    logging_sync_to_disk()
    captured = capfd.readouterr()
    return captured.out + captured.err


def test_logged_waits_for_lines_still_queued_after_a_run(venue, capfd) -> None:
    # As in test_data_client.py: lines queued after a node run are captured only if `logged` waits.
    run_with_client(lambda client: asyncio.sleep(0))
    probe = Logger("LogProbe")
    for i in range(LOG_PROBE_LINES):
        probe.info(f"probe line {i}")

    assert f"probe line {LOG_PROBE_LINES - 1}" in logged(capfd)


@pytest.mark.parametrize("launch", ["owned", "hosted"])
@pytest.mark.parametrize("registration", ["config", "builder"])
def test_market_order_fills(venue, registration, launch) -> None:
    strategy = trader()
    node = build_node(strategy, registration)

    run(node, launch)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
    filled = strategy.events[-1]
    assert filled.account_id == ACCOUNT_ID
    assert filled.last_qty == Quantity.from_int(2)
    assert filled.last_px == Price.from_str("105.8988475")  # the docs' fill price, kept exactly
    assert strategy.client_order_id is not None
    order = node.cache.order(strategy.client_order_id)
    assert order is not None
    assert order.status == OrderStatus.FILLED
    (request,) = venue.trading.submitted
    assert isinstance(request, MarketOrderRequest)
    assert (request.symbol, request.qty, request.side) == ("AAPL", 2.0, AlpacaOrderSide.BUY)
    assert request.time_in_force == AlpacaTimeInForce.GTC
    assert request.client_order_id == strategy.client_order_id.value


def test_limit_order_is_canceled(venue) -> None:
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", price="150.25", cancel_on="OrderAccepted")

    run(build_node(strategy))

    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    (request,) = venue.trading.submitted
    assert isinstance(request, LimitOrderRequest)
    assert request.limit_price == 150.25
    accepted = strategy.events[1]
    assert venue.trading.cancels == [accepted.venue_order_id.value]
    assert strategy.events[-1].venue_order_id == accepted.venue_order_id


def test_order_refused_by_alpaca_is_rejected(venue, alpaca) -> None:
    venue.trading.submit_error = alpaca.api_error(403, 40310000, "insufficient buying power")
    strategy = trader()

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderRejected"]
    assert strategy.events[-1].reason == "Alpaca refused the order (403): insufficient buying power"


def test_order_rejected_on_the_stream_is_rejected(venue) -> None:
    venue.trading.script = "reject"
    strategy = trader(order_type="limit")

    run(build_node(strategy))

    # Accepted once, by whichever of the REST answer and the stream's `new` comes first, then rejected.
    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderRejected"]
    assert "Alpaca rejected the order" in strategy.events[-1].reason


def test_partial_fills_are_reported_per_fill(venue) -> None:
    venue.trading.script = "partial"
    strategy = trader(quantity=3)

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled", "OrderFilled"]
    assert [e.last_qty for e in strategy.events[2:]] == [Quantity.from_int(1), Quantity.from_int(2)]
    assert len({e.trade_id for e in strategy.events[2:]}) == 2


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"order_type": "trailing"}, "order type TRAILING_STOP_MARKET is not supported"),
        ({"time_in_force": TimeInForce.IOC}, "time in force IOC is not supported"),
        (
            {"order_type": "stop", "time_in_force": TimeInForce.FOK},
            "time in force FOK is not supported",
        ),
        (
            {"order_type": "stop", "trigger_type": TriggerType.BID_ASK},
            "trigger type BID_ASK is not offered: Alpaca elects a stop on the last trade",
        ),
        (
            {"order_type": "stop", "trigger_type": TriggerType.MARK_PRICE},
            "trigger type MARK_PRICE is not offered",
        ),
        ({"order_type": "stop", "trigger_price": "200.125"}, "stop price 200.125 has more than 2"),
        (
            {
                "order_type": "stop_limit",
                "price": "200.50",
                "trigger_price": "200.00",
                "post_only": True,
            },
            "post-only is not supported",
        ),
        (
            {"order_type": "stop_limit", "price": "201.001", "trigger_price": "200.00"},
            "limit price 201.001 has more than 2 decimals",
        ),
        (
            {"order_type": "limit", "price": "150.123"},
            "limit price 150.123 has more than 2 decimals",
        ),
    ],
)
def test_unsupported_order_is_denied_before_alpaca(venue, options, reason) -> None:
    strategy = trader(**options)

    run(build_node(strategy))

    assert strategy.names() == ["OrderDenied"]
    assert reason in strategy.events[0].reason
    assert venue.trading.submitted == []


@pytest.mark.parametrize("trigger_type", [None, TriggerType.DEFAULT, TriggerType.LAST_PRICE])
def test_stop_order_is_sent_with_its_stop_price_and_fills(venue, trigger_type) -> None:
    strategy = trader(order_type="stop", trigger_price="200.00", trigger_type=trigger_type)

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
    (request,) = venue.trading.submitted
    assert isinstance(request, StopOrderRequest)
    assert (request.stop_price, request.time_in_force) == (200.0, AlpacaTimeInForce.GTC)
    assert request.extended_hours is None  # never sent: Alpaca takes no stop in extended hours


def test_stop_limit_order_is_sent_with_both_prices(venue) -> None:
    venue.trading.script = "accept"
    strategy = trader(
        order_type="stop_limit",
        price="200.50",
        trigger_price="200.00",
        time_in_force=TimeInForce.DAY,
        cancel_on="OrderAccepted",
    )

    run(build_node(strategy))

    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    (request,) = venue.trading.submitted
    assert isinstance(request, StopLimitOrderRequest)
    assert (request.stop_price, request.limit_price) == (200.0, 200.5)
    assert request.time_in_force == AlpacaTimeInForce.DAY


def test_extended_hours_limit_order_is_sent_eligible(venue) -> None:
    strategy = trader(
        order_type="limit", time_in_force=TimeInForce.DAY, params={"extended_hours": True}
    )

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
    (request,) = venue.trading.submitted
    assert isinstance(request, LimitOrderRequest)
    assert (request.extended_hours, request.time_in_force) == (True, AlpacaTimeInForce.DAY)


def test_order_without_the_flag_is_not_sent_eligible(venue) -> None:
    strategy = trader(order_type="limit", params={"extended_hours": False})

    run(build_node(strategy))

    (request,) = venue.trading.submitted
    assert request.extended_hours is None


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"order_type": "market"}, "extended_hours is for limit orders only at Alpaca, not MARKET"),
        (
            {"order_type": "stop"},
            "extended_hours is for limit orders only at Alpaca, not STOP_MARKET",
        ),
        (
            {"order_type": "limit", "params": {"extended_hours": "yes"}},
            "extended_hours must be true or false, got 'yes'",
        ),
        (
            {"order_type": "limit", "params": {"outside_rth": True}},
            "order params ['outside_rth'] are not taken; only 'extended_hours'",
        ),
    ],
)
def test_extended_hours_order_alpaca_rejects_is_denied_before_alpaca(
    venue, options, reason
) -> None:
    options = {"params": {"extended_hours": True}, **options}
    strategy = trader(**options)

    run(build_node(strategy))

    assert strategy.names() == ["OrderDenied"]
    assert reason in strategy.events[0].reason
    assert venue.trading.submitted == []


def test_untradable_asset_is_denied(venue, alpaca) -> None:
    venue.trading.assets = [{**alpaca.ASSET_AAPL, "tradable": False}]
    strategy = trader(order_type="limit")

    run(build_node(strategy))

    assert strategy.names() == ["OrderDenied"]
    assert "not tradable on Alpaca" in strategy.events[0].reason


def test_submit_without_an_answer_stays_submitted(venue, capfd) -> None:
    venue.trading.submit_error = ConnectionError("connection reset")
    strategy = trader(order_type="limit")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.submitted:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted"]
    assert "stays SUBMITTED until a trade update or reconciliation resolves it" in logged(capfd)


def test_submit_whose_connection_timed_out_on_its_only_attempt_is_rejected(venue) -> None:
    # A connect timeout proves the request was not sent (ADR 0039), so the submit is proven unsuccessful.
    venue.trading.submit_error = requests.ConnectTimeout("connect timed out")
    strategy = trader(order_type="limit")

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderRejected"]
    assert strategy.events[-1].reason.startswith(
        "not sent: Alpaca's API did not accept the connection"
    )


@pytest.mark.parametrize(
    ("error", "sends"),
    [
        (requests.ReadTimeout("read timed out"), 1),  # sent; the answer did not come
        (
            requests.ConnectTimeout("connect timed out"),
            2,
        ),  # a repeat of a 504, which may have arrived
    ],
)
def test_submit_that_may_have_reached_alpaca_stays_submitted(venue, capfd, error, sends) -> None:
    venue.trading.submit_error = error
    venue.trading.sends_per_call = sends
    strategy = trader(order_type="limit")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.submitted:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted"]
    assert "stays SUBMITTED until a trade update or reconciliation resolves it" in logged(capfd)


def test_cancel_whose_connection_timed_out_is_a_cancel_rejection(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.cancel_error = requests.ConnectTimeout("connect timed out")
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")
    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run(build_node(strategy))
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert strategy.names()[-1] == "OrderCancelRejected"
    assert strategy.events[-1].reason.startswith(
        "not sent: Alpaca's API did not accept the connection"
    )


def test_cancel_whose_lookup_connection_timed_out_is_a_cancel_rejection(venue) -> None:
    # The order has no venue ID yet, so the cancel first looks it up; that lookup's connection times out,
    # so neither the lookup nor the cancel reached Alpaca.
    venue.trading.script = "silent"
    venue.trading.lookup_error = requests.ConnectTimeout("connect timed out")
    strategy = trader(order_type="limit", cancel_on="OrderSubmitted")
    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run(build_node(strategy))
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert strategy.names()[-1] == "OrderCancelRejected"
    assert strategy.events[-1].reason.startswith(
        "not sent: Alpaca's API did not accept the connection"
    )
    assert venue.trading.cancels == []


def test_cancel_whose_answer_timed_out_leaves_the_order(venue, capfd) -> None:
    venue.trading.script = "accept"
    venue.trading.cancel_error = requests.ReadTimeout("read timed out")
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.cancels:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert "OrderCancelRejected" not in strategy.names()
    assert "got no answer" in logged(capfd)


class SilentServer:
    """A local HTTP server that accepts each connection and, for `/hang`, never answers."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.socket.settimeout(0.1)  # so the accept loop sees `stopped`
        self.port = self.socket.getsockname()[1]
        self.stopped = threading.Event()
        self.held: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            connection.settimeout(None)
            request = b""
            while b"\r\n\r\n" not in request:  # the request line and headers, however they arrive
                chunk = connection.recv(65536)
                if not chunk:
                    break
                request += chunk
            if b"/hang " in request:
                self.held.append(connection)  # read, never answered
                continue
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\n{}"
            )
            connection.close()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(5)
        self.socket.close()
        for connection in self.held:
            connection.close()


def test_a_request_alpaca_never_answers_ends_at_the_read_timeout_and_the_run_ends(
    venue, monkeypatch
) -> None:
    # On paper a node stopped while a call hung failed to disconnect and its run never ended: alpaca-py
    # sets no HTTP timeout. Here alpaca-py's own REST client, given the adapter's timeout (ADR 0039), asks
    # a local server that never answers; the call ends at the read timeout, the next call is answered,
    # and the node's run ends at once.
    monkeypatch.setattr(canon_alpaca.providers, "CONNECT_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr(canon_alpaca.providers, "READ_TIMEOUT_SECS", 0.5)
    server = SilentServer()
    rest = with_http_timeout(
        RESTClient(
            base_url=f"http://127.0.0.1:{server.port}",
            api_version="v2",
            api_key=PLACEHOLDER,
            secret_key=PLACEHOLDER,
        )
    )
    outcomes: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        started = time.monotonic()
        try:
            await client._call(rest.get, "/hang")
        except requests.ReadTimeout as e:
            outcomes.append((type(e).__name__, time.monotonic() - started))
        outcomes.append(await client._call(rest.get, "/ok"))

    started = time.monotonic()
    try:
        run_with_client(body)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert len(outcomes) == 2, outcomes
    timed_out, waited = cast("tuple[str, float]", outcomes[0])
    answer = outcomes[1]
    assert timed_out == "ReadTimeout"
    assert waited < 5
    assert answer == {}
    assert elapsed < 20, f"the node's run took {elapsed:.1f} s"  # it never ended before the timeout


def test_a_client_without_a_requests_session_is_refused() -> None:
    with pytest.raises(TypeError, match="refusing to run without an HTTP timeout"):
        with_http_timeout(SimpleNamespace())


def test_fill_without_an_execution_id_is_logged_not_applied(venue, capfd) -> None:
    venue.trading.execution_ids = False
    strategy = trader()

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted"]
    assert "has no execution_id to use as the trade ID" in logged(capfd)


def test_refused_repeat_of_a_submit_alpaca_took_is_not_a_rejection(venue, alpaca, capfd) -> None:
    # alpaca-py repeats a submit answered 504; the repeat is refused, but Alpaca holds the first one.
    # No trade update has arrived yet, so only the lookup can tell.
    venue.trading.script = "silent"
    venue.trading.submit_error = alpaca.api_error(422, 40010001, "client_order_id must be unique")
    venue.trading.took_before_error = True
    strategy = trader(order_type="limit")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.submitted:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted"]
    assert "but holds the order: an earlier attempt reached it" in logged(capfd)


def test_refused_repeat_of_a_cancel_alpaca_took_is_not_a_rejection(venue, alpaca) -> None:
    # The repeat of a cancel is refused 422 while Alpaca shows the order pending cancel.
    venue.trading.script = "accept"
    venue.trading.cancel_error = alpaca.api_error(422, 42210000, "order is not cancelable")
    venue.trading.took_before_error = True
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.cancels:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderPendingCancel"]


def test_cancel_while_the_submit_is_in_flight(venue) -> None:
    # The cancel is sent while Alpaca's answer to the submit is held back and no update has arrived:
    # the late answer must not move the order out of PENDING_CANCEL, and the cancel finds the order by
    # its client order ID.
    venue.trading.script = "silent"
    venue.trading.submit_delay = 0.3
    strategy = trader(order_type="limit", cancel_on="OrderSubmitted")

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderPendingCancel", "OrderCanceled"]
    assert len(venue.trading.cancels) == 1


def test_cancel_refused_by_alpaca_is_a_cancel_rejection(venue, alpaca) -> None:
    venue.trading.script = "accept"
    venue.trading.cancel_error = alpaca.api_error(422, 42210000, "order is not cancelable")
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")
    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run(build_node(strategy))
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert strategy.names()[-1] == "OrderCancelRejected"
    assert strategy.events[-1].reason == "Alpaca refused the cancel (422): order is not cancelable"


class CancelAll(Strategy):
    """Place one limit order per entry in `orders` on the first quote and, once Alpaca has accepted all of
    them, send one `CancelAllOrders` for AAPL (`strategy_only=False`, the form that reaches the client)."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: list[tuple[OrderSide, str]] = []
        self.side: OrderSide | None = None  # the cancel-all's order side, None for both
        self.started = False
        self.placed: list[ClientOrderId] = []
        self.accepted: set[ClientOrderId] = set()
        self.sent = False
        self.events: list = []

    def on_start(self) -> None:
        self.started = True
        self.subscribe_quotes(AAPL)

    def on_quote(self, quote) -> None:
        if self.placed:
            return
        for side, price in self.orders:
            order = self.order_factory.limit(
                AAPL, side, Quantity.from_int(1), Price.from_str(price)
            )
            self.placed.append(order.client_order_id)
            self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderAccepted":
            self.accepted.add(event.client_order_id)
        if not self.sent and self.placed and self.accepted >= set(self.placed):
            self.sent = True
            self.cancel_all_orders(AAPL, order_side=self.side, strategy_only=False)

    def final(self) -> dict[str, str]:
        """Each order's last event, by client order ID."""
        return {e.client_order_id.value: type(e).__name__ for e in self.events}


def cancel_all(**options: object) -> CancelAll:
    strategy = CancelAll()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


def run_until(strategy: CancelAll, done: Callable[[], bool], venue: SimpleNamespace) -> None:
    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not done():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)


def test_cancel_all_cancels_each_open_order_of_the_instrument_by_id(venue, alpaca) -> None:
    # One order the node reconciled at start-up, placed before it by no strategy of this node, and two
    # this strategy places: all three are AAPL's, so all three are canceled, each by its own ID.
    venue.trading.script = "accept"
    venue.trading.orders = [alpaca.alpaca_order(status="new")]  # GTC limit buy of 2 at 150
    venue.trading.positions = [alpaca.POSITION_AAPL]  # long 2, so the sell is not a short
    strategy = cancel_all(orders=[(OrderSide.BUY, "140.00"), (OrderSide.SELL, "190.00")])
    external = alpaca.ORDER_AAPL["client_order_id"]

    run_until(
        strategy,
        lambda: (
            len(venue.trading.cancels) == 3
            and len([n for n in strategy.final().values() if n == "OrderCanceled"]) == 2
        ),
        venue,
    )

    placed = {c.value for c in strategy.placed}
    assert {k: v for k, v in strategy.final().items() if k in placed} == dict.fromkeys(
        placed, "OrderCanceled"
    )
    accepted = {
        e.client_order_id.value: e.venue_order_id.value
        for e in strategy.events
        if type(e).__name__ == "OrderAccepted"
    }
    assert sorted(venue.trading.cancels) == sorted([*accepted.values(), alpaca.ORDER_AAPL["id"]])
    assert venue.trading.get_order_by_client_id(external).status.value == "canceled"


def test_cancel_all_with_a_side_cancels_only_that_side(venue, alpaca) -> None:
    venue.trading.script = "accept"
    venue.trading.positions = [alpaca.POSITION_AAPL]
    strategy = cancel_all(
        orders=[(OrderSide.BUY, "140.00"), (OrderSide.SELL, "190.00")], side=OrderSide.BUY
    )

    run_until(strategy, lambda: "OrderCanceled" in strategy.final().values(), venue)

    buy, sell = (c.value for c in strategy.placed)
    assert strategy.final() == {buy: "OrderCanceled", sell: "OrderAccepted"}
    assert len(venue.trading.cancels) == 1


def test_cancel_all_rejects_the_cancel_alpaca_refuses_and_cancels_the_rest(venue, alpaca) -> None:
    venue.trading.script = "accept"
    refusal = alpaca.api_error(422, 42210000, "order is not cancelable")
    venue.trading.refuse_cancel = lambda order: refusal if order["limit_price"] == "140.0" else None
    strategy = cancel_all(orders=[(OrderSide.BUY, "140.00"), (OrderSide.BUY, "141.00")])

    run_until(
        strategy,
        lambda: sorted(strategy.final().values()) == ["OrderCancelRejected", "OrderCanceled"],
        venue,
    )

    first, second = (c.value for c in strategy.placed)
    assert strategy.final() == {first: "OrderCancelRejected", second: "OrderCanceled"}
    rejection = next(e for e in strategy.events if type(e).__name__ == "OrderCancelRejected")
    assert rejection.reason == "Alpaca refused the cancel (422): order is not cancelable"
    assert len(venue.trading.cancels) == 2


def test_cancel_all_goes_on_after_an_unexpected_failure(venue, capfd) -> None:
    # An answer alpaca-py cannot parse raises a ValueError (pydantic's ValidationError is one).
    venue.trading.script = "accept"
    broken = ValueError("unparsable answer")
    venue.trading.refuse_cancel = lambda order: broken if order["limit_price"] == "140.0" else None
    strategy = cancel_all(orders=[(OrderSide.BUY, "140.00"), (OrderSide.BUY, "141.00")])

    run_until(strategy, lambda: "OrderCanceled" in strategy.final().values(), venue)

    first, second = (c.value for c in strategy.placed)
    assert strategy.final() == {first: "OrderAccepted", second: "OrderCanceled"}
    assert f"Alpaca cancel of {first} failed (ValueError('unparsable answer'))" in logged(capfd)


def test_cancel_all_with_no_matching_order_sends_nothing(venue, capfd) -> None:
    async def body(client) -> None:
        # On the node's loop, where the command queue runs the hook; the cache holds no AAPL order.
        await client._cancel_all_orders(SimpleNamespace(instrument_id=AAPL, order_side=None))

    run_with_client(body)

    assert venue.trading.cancels == []
    assert "No open or in-flight AAPL.ALPACA orders to cancel" in logged(capfd)


class Modifier(Strategy):
    """Place one limit (or stop) buy on the first quote and, once Alpaca accepts it, modify it to each
    price in `prices` in turn, each after the last `OrderUpdated`."""

    def __init__(self) -> None:
        super().__init__()
        self.order_type = "limit"
        self.prices: list[str] = ["150.50"]
        self.field = "price"  # or "trigger_price", or "quantity"
        self.started = False
        self.client_order_id: ClientOrderId | None = None
        self.events: list = []

    def on_start(self) -> None:
        self.started = True
        self.subscribe_quotes(AAPL)

    def on_quote(self, quote) -> None:
        if self.client_order_id is not None:
            return
        factory = self.order_factory
        if self.order_type == "stop":
            order = factory.stop_market(
                AAPL, OrderSide.BUY, Quantity.from_int(2), Price.from_str("200.00")
            )
        else:
            order = factory.limit(
                AAPL, OrderSide.BUY, Quantity.from_int(2), Price.from_str("150.00")
            )
        self.client_order_id = order.client_order_id
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name in ("OrderAccepted", "OrderUpdated") and self.prices:
            if name == "OrderAccepted" and self.names().count("OrderAccepted") > 1:
                return
            value = self.prices.pop(0)
            assert self.client_order_id is not None
            if self.field == "quantity":
                self.modify_order(self.client_order_id, quantity=Quantity.from_str(value))
            elif self.field == "trigger_price":
                self.modify_order(self.client_order_id, trigger_price=Price.from_str(value))
            else:
                self.modify_order(self.client_order_id, price=Price.from_str(value))

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]

    def last(self, name: str):
        return next(e for e in reversed(self.events) if type(e).__name__ == name)


def modifier(**options: object) -> Modifier:
    strategy = Modifier()
    for name, value in options.items():
        assert hasattr(strategy, name), name
        setattr(strategy, name, value)
    return strategy


def run_modify(
    venue: SimpleNamespace,
    strategy: Modifier,
    done: Callable[[], bool],
    then: Callable[[AlpacaExecutionClient], Awaitable[None]] | None = None,
) -> None:
    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not done():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        if then is not None:
            await then(client)
            await until_idle(client, venue)

    run_with_client(body, strategy)


def alpaca_order_model(raw: dict):
    from alpaca.trading.models import Order

    return Order(**raw)


def placed_order(venue: SimpleNamespace, index: int = 0) -> dict:
    return list(venue.trading._placed.values())[index]


def test_modify_is_updated_when_alpacas_stream_confirms_the_replace(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingUpdate",
        "OrderUpdated",
    ]
    old, new = placed_order(venue, 0), placed_order(venue, 1)
    updated = strategy.last("OrderUpdated")
    assert updated.venue_order_id.value == new["id"]
    assert updated.price == Price.from_str("150.5000")
    assert updated.quantity == Quantity.from_int(2)
    ((replaced_id, request),) = venue.trading.replace_requests
    assert replaced_id == old["id"]
    assert isinstance(request, ReplaceOrderRequest)
    # The current price is sent for a limit order; no client order ID (Alpaca refuses one kept).
    assert (request.limit_price, request.qty, request.client_order_id) == (150.5, None, None)


def test_no_update_comes_from_the_patch_answer_alone(venue) -> None:
    # Alpaca: a success "does NOT guarantee the existing open order has been replaced".
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def confirm(client) -> None:
        assert "OrderUpdated" not in strategy.names()
        venue.trading.confirm_replace(placed_order(venue, 0)["id"])
        async with asyncio.timeout(10):
            while "OrderUpdated" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), confirm)

    assert strategy.names()[-2:] == ["OrderPendingUpdate", "OrderUpdated"]
    assert strategy.last("OrderUpdated").venue_order_id.value == placed_order(venue, 1)["id"]


@pytest.mark.parametrize("cold", [False, True], ids=["mapped", "after-restart"])
def test_fill_on_the_replacement_reaches_the_nodes_order(venue, cold) -> None:
    # The replacement's updates carry a client order ID Alpaca made; "after-restart" empties the
    # client's map first, so the fill is traced back through `replaces`.
    venue.trading.script = "accept"
    strategy = modifier()

    async def fill(client) -> None:
        if cold:
            client._replacements.clear()
        new = placed_order(venue, 1)
        venue.trading.update(new["id"], "fill", 2, status="filled", filled_qty="2")
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), fill)

    filled = strategy.last("OrderFilled")
    assert filled.client_order_id == strategy.client_order_id
    assert filled.venue_order_id.value == placed_order(venue, 1)["id"]


def test_two_replaces_in_a_row_and_a_fill_traced_back_through_both(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier(prices=["150.50", "151.00"])

    async def fill(client) -> None:
        client._replacements.clear()
        newest = placed_order(venue, 2)
        venue.trading.update(newest["id"], "fill", 2, status="filled", filled_qty="2")
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: strategy.names().count("OrderUpdated") == 2, fill)

    first, second = (e for e in strategy.events if type(e).__name__ == "OrderUpdated")
    assert first.venue_order_id.value == placed_order(venue, 1)["id"]
    assert second.venue_order_id.value == placed_order(venue, 2)["id"]
    assert second.price == Price.from_str("151.0000")
    assert [old for old, _ in venue.trading.replace_requests] == [
        placed_order(venue, 0)["id"],
        placed_order(venue, 1)["id"],
    ]
    assert strategy.last("OrderFilled").client_order_id == strategy.client_order_id


def test_reconciliation_reports_the_replacement_under_the_nodes_id(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier()
    reports: list = []

    async def report(client) -> None:
        client._replacements.clear()
        venue.trading.orders = list(venue.trading._placed.values())
        window = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)
        reports.extend(await client._generate_order_status_reports(window))
        by_id = SimpleNamespace(venue_order_id=None, client_order_id=strategy.client_order_id)
        reports.append(await client._generate_order_status_report(by_id))

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), report)

    new = placed_order(venue, 1)
    assert [(r.venue_order_id.value, r.client_order_id) for r in reports] == [
        (new["id"], strategy.client_order_id),
        (new["id"], strategy.client_order_id),
    ]


def test_modify_alpaca_refuses_is_modify_rejected(venue, alpaca) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = alpaca.api_error(422, 42210000, "order is not replaceable")
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert strategy.names()[-2:] == ["OrderPendingUpdate", "OrderModifyRejected"]
    reason = strategy.last("OrderModifyRejected").reason
    assert reason == "Alpaca refused the replace (422): order is not replaceable"


def test_modify_whose_connection_timed_out_is_modify_rejected(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = requests.ConnectTimeout("connect timed out")
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert strategy.names()[-2:] == ["OrderPendingUpdate", "OrderModifyRejected"]
    reason = strategy.last("OrderModifyRejected").reason
    assert reason.startswith("not sent: Alpaca's API did not accept the connection")


def test_modify_whose_answer_timed_out_is_not_rejected(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = requests.ReadTimeout("read timed out")
    strategy = modifier()

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests))

    assert "OrderModifyRejected" not in strategy.names()


def test_refused_repeat_of_a_replace_alpaca_took_is_not_a_rejection(venue, alpaca, capfd) -> None:
    # alpaca-py repeats a replace answered 504; the repeat gets 422 "order already replaced" (observed).
    venue.trading.script = "accept"
    venue.trading.replace_error = alpaca.api_error(422, 42210000, "order already replaced")
    venue.trading.took_before_error = True
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    assert "OrderModifyRejected" not in strategy.names()
    assert strategy.last("OrderUpdated").venue_order_id.value == placed_order(venue, 1)["id"]
    assert "but shows the order replaced as asked: an earlier attempt reached it" in logged(capfd)


def test_replace_rejected_on_the_stream_is_modify_rejected(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "reject"
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert "OrderUpdated" not in strategy.names()
    assert "OrderModifyRejected" in strategy.names()


def test_rejected_replacement_is_modify_rejected_not_order_rejected(venue) -> None:
    # Alpaca: if the old order fills before the replacement reaches the venue, the replacement is
    # rejected. That rejection must not reject the node's order.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def race(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old["status"] = "new"
        venue.trading.update(old["id"], "fill", 2, status="filled", filled_qty="2")
        venue.trading.update(new["id"], "rejected", status="rejected")
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), race)

    assert "OrderRejected" not in strategy.names()
    assert "OrderFilled" in strategy.names()


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"prices": ["150.505"]}, "limit price 150.505 has more than 2 decimals"),
        (
            {"order_type": "stop", "field": "trigger_price", "prices": ["210.125"]},
            "stop price 210.125 has more than 2 decimals",
        ),
    ],
)
def test_modify_the_client_can_prove_alpaca_refuses_is_rejected_before_alpaca(
    venue, options, reason
) -> None:
    venue.trading.script = "accept"
    strategy = modifier(**options)

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert reason in strategy.last("OrderModifyRejected").reason
    assert venue.trading.replace_requests == []


def test_stop_trigger_is_modified(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier(order_type="stop", field="trigger_price", prices=["210.00"])

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    updated = strategy.last("OrderUpdated")
    assert updated.trigger_price == Price.from_str("210.0000")
    ((_, request),) = venue.trading.replace_requests
    assert (request.stop_price, request.limit_price) == (210.0, None)


def test_replacement_that_cannot_be_read_gives_no_update(venue, alpaca, capfd) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def confirm(client) -> None:
        old = placed_order(venue, 0)
        client._answers.clear()  # the PATCH answer is lost
        venue.trading.orders = []
        del venue.trading._placed[old["replaced_by"]]  # and the replacement cannot be read
        venue.trading.confirm_replace(old["id"])

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), confirm)

    assert "OrderUpdated" not in strategy.names()
    assert "no OrderUpdated, reconciliation settles it" in logged(capfd)


def test_filled_replacement_is_reported_when_replaced_was_missed(venue) -> None:
    # The replace is answered, Alpaca's `replaced` never arrives, and the replacement fills unseen: the
    # node's order must still be reported filled, through the replacement, or its fill is lost.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    reports: list = []

    async def reconcile(client) -> None:
        new = placed_order(venue, 1)
        new.update(status="filled", filled_qty="2", filled_avg_price="150.5")
        venue.trading.orders = list(venue.trading._placed.values())
        client._replacements.clear()
        window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
        reports.extend(await client._generate_order_status_reports(window))

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), reconcile)

    new = placed_order(venue, 1)
    mine = [r for r in reports if r.client_order_id == strategy.client_order_id]
    assert [(r.venue_order_id.value, r.order_status) for r in mine] == [
        (new["id"], OrderStatus.FILLED)
    ]


def test_replace_rejected_twice_over_is_one_modify_rejection(venue) -> None:
    # Alpaca refuses the replace on the HTTP answer and on the stream (`order_replace_rejected`).
    venue.trading.script = "accept"
    venue.trading.replace_script = "reject"
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert strategy.names().count("OrderModifyRejected") == 1, strategy.names()


def test_replaced_delivered_twice_is_one_update(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier()

    async def again(client) -> None:
        venue.trading.confirm_replace(placed_order(venue, 0)["id"])

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), again)

    assert strategy.names().count("OrderUpdated") == 1, strategy.names()


def test_rejected_replacement_after_the_old_order_filled_settles_the_modify(venue, capfd) -> None:
    # The old order fills, then Alpaca rejects the replacement. NautilusTrader drops a modify rejection
    # for a filled order (observed), so the fill is the outcome, the rejection is logged, and the modify
    # is settled: a later refusal is not taken for this one.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def race(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old["status"] = "new"
        venue.trading.update(old["id"], "fill", 2, status="filled", filled_qty="2")
        venue.trading.update(new["id"], "rejected", status="rejected")
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        assert strategy.client_order_id not in client._modifying.values()

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), race)

    assert "OrderFilled" in strategy.names() and "OrderRejected" not in strategy.names()
    assert "which had filled first; the fill is the outcome" in logged(capfd)


@pytest.mark.parametrize("quantity", ["5", "1"])
def test_fill_on_a_replacement_before_its_replaced_applies_the_replacement_first(
    venue, quantity
) -> None:
    # The replacement's fill arrives before Alpaca's `replaced`: the node's order must take the
    # replacement's quantity first, or a larger fill is dropped as an overfill.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(field="quantity", prices=[quantity])

    async def fill(client) -> None:
        new = placed_order(venue, 1)
        q = int(quantity)
        venue.trading.update(new["id"], "fill", q, status="filled", filled_qty=str(q))
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), fill)

    names = strategy.names()
    assert names.index("OrderUpdated") < names.index("OrderFilled"), names
    filled = strategy.last("OrderFilled")
    assert filled.last_qty == Quantity.from_int(int(quantity))
    assert filled.venue_order_id.value == placed_order(venue, 1)["id"]


def test_late_cancel_of_the_replaced_order_does_not_cancel_the_replacement(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier()

    async def late(client) -> None:
        old = placed_order(venue, 0)
        venue.trading.update(old["id"], "canceled", status="canceled")

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), late)

    assert "OrderCanceled" not in strategy.names(), strategy.names()


def test_open_order_pending_replace_at_startup_is_reported(venue, alpaca) -> None:
    venue.trading.orders = [alpaca.alpaca_order(status="pending_replace")]
    node = build_node(trader(submit=False))

    run(node)

    (working,) = node.cache.orders_open()
    assert working.venue_order_id.value == alpaca.ORDER_AAPL["id"]


@pytest.mark.parametrize("status", ["pending_replace", "replaced"])
def test_replaced_whose_replacement_has_moved_on_still_updates(venue, status) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def moved(client) -> None:
        client._answers.clear()
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        if status == "replaced":
            newer = {**new, "id": "9c0f6b2e-1d4a-4e8b-a3f5-7b6c5d4e3f21", "replaces": new["id"]}
            newer["client_order_id"] = "newer-" + new["client_order_id"]
            venue.trading._placed[newer["id"]] = newer
            new.update(status="replaced", replaced_by=newer["id"])
        else:
            new["status"] = "pending_replace"
        venue.trading.confirm_replace(old["id"])
        async with asyncio.timeout(10):
            while "OrderUpdated" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), moved)

    assert "OrderUpdated" in strategy.names()


def test_second_modify_refused_after_the_first_replaced_is_rejected(venue) -> None:
    # Modify A (qty 5, $151) is answered but its `replaced` not yet seen; modify B ($151, quantity
    # unchanged at 2) reaches the old order and is refused "order already replaced". B asked for
    # other terms than the replacement holds, so B is rejected, not taken for alpaca-py's retry.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(field="quantity", prices=["5"])

    async def second(client) -> None:
        cached = client.cache.order(strategy.client_order_id)
        command = SimpleNamespace(
            client_order_id=strategy.client_order_id,
            venue_order_id=cached.venue_order_id,
            quantity=None,
            price=Price.from_str("150.00"),
            trigger_price=None,
        )
        await client._modify_order(command)
        async with asyncio.timeout(10):
            while "OrderModifyRejected" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), second)

    assert "OrderModifyRejected" in strategy.names()


def test_answer_after_replaced_is_not_kept(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_delay = 0.3  # `replaced` is applied before the answer returns
    strategy = modifier()
    left: list = []

    async def check(client) -> None:
        left.extend(client._answers)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), check)

    assert left == []


def test_replacement_seen_before_the_node_holds_its_original_is_traced_later(venue) -> None:
    # The state a trace in the start-up window leaves (the original not yet in the node's cache): the
    # replacement was traced to no node order. Once the node holds the original, it must trace to it.
    venue.trading.script = "accept"
    strategy = modifier()
    traced: list = []

    async def trace(client) -> None:
        new = placed_order(venue, 1)
        client._replacements.clear()
        # What a trace in the window records: the chain's root, the original's client order ID, which
        # the node did not hold then (before the fix, the miss was recorded as tracing to nothing).
        client._replacements[new["client_order_id"]] = ClientOrderId(
            placed_order(venue, 0)["client_order_id"]
        )
        traced.append(await client._node_order_id(alpaca_order_model(new)))

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), trace)

    assert traced == [strategy.client_order_id]


def test_replacement_new_then_rejected_is_not_an_order_rejection(venue) -> None:
    # A replacement's `new` does not adopt it (it may still be rejected); its rejection is the modify's.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def race(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.update(new["id"], "new", status="new")
        old["status"] = "new"
        venue.trading.update(old["id"], "fill", 2, status="filled", filled_qty="2")
        venue.trading.update(new["id"], "rejected", status="rejected")
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), race)

    assert "OrderRejected" not in strategy.names() and "OrderUpdated" not in strategy.names()


def test_rejected_replacement_is_not_reported_after_a_restart(venue) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    reports: list = []

    async def restart(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old.update(status="filled", filled_qty="2", filled_avg_price="150")
        new["status"] = "rejected"
        venue.trading.orders = list(venue.trading._placed.values())
        client._replacements.clear()
        window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
        reports.extend(await client._generate_order_status_reports(window))

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), restart)

    mine = [r for r in reports if r.client_order_id == strategy.client_order_id]
    assert [(r.venue_order_id.value, r.order_status) for r in mine] == [
        (placed_order(venue, 0)["id"], OrderStatus.FILLED)
    ]


def test_pending_replace_with_fills_is_reported_partly_filled(alpaca) -> None:
    from alpaca.trading.models import Order
    from canon_alpaca.execution import order_status_report

    raw = alpaca.alpaca_order(status="pending_replace", filled_qty="1", filled_avg_price="150")
    report = order_status_report(Order(**raw), 0)
    assert report is not None
    assert report.order_status == OrderStatus.PARTIALLY_FILLED


def test_update_for_an_order_the_node_does_not_hold_is_dropped(venue, alpaca, capfd) -> None:
    other = alpaca.alpaca_order(client_order_id="placed-elsewhere", status="new")
    venue.stream.send({"event": "new", "order": other, "timestamp": other["updated_at"]})
    strategy = trader()

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
    assert "which this node does not hold; reconciliation reports it" in logged(capfd)


def test_startup_reconciliation_reports_orders_and_positions(venue, alpaca) -> None:
    open_limit = alpaca.alpaca_order()  # the docs' accepted GTC limit buy of 2 at 150
    filled_market = alpaca.alpaca_order(
        id="0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
        client_order_id="filled-before-start",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty="2",
        filled_avg_price="174.78",
    )
    venue.trading.orders = [open_limit, filled_market]
    venue.trading.positions = [alpaca.POSITION_AAPL]  # long 2 at 174.78
    node = build_node(trader(submit=False))

    run(node)

    # Every order in the engine's window, and every open order however old.
    statuses = [q.status for q in venue.trading.order_queries]
    assert statuses in ([QueryOrderStatus.ALL], [QueryOrderStatus.ALL, QueryOrderStatus.OPEN])
    (working,) = node.cache.orders_open()
    assert working.venue_order_id.value == open_limit["id"]
    assert working.status == OrderStatus.ACCEPTED
    assert working.price == Price.from_str("150.0000")
    (position,) = node.cache.positions_open()
    assert position.quantity == Quantity.from_int(2)
    assert Decimal(str(position.avg_px_open)) == Decimal("174.78")


def test_startup_reconciliation_takes_fills_from_alpacas_fill_activities(venue, alpaca) -> None:
    # A market buy of 2 filled before start in two parts, at prices other than its average: the fills come
    # from Alpaca's FILL activities, each with the UUID after `::` as its trade ID (ADR 0024), not inferred.
    filled = alpaca.alpaca_order(
        id="0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
        client_order_id="filled-before-start",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty="2",
        filled_avg_price="174.78",
    )
    first = datetime(2026, 11, 10, 18, 52, 26, 415_106, tzinfo=UTC)
    second = datetime(2026, 11, 10, 18, 52, 28, 283_360, tzinfo=UTC)
    venue.trading.orders = [filled]
    venue.trading.positions = [alpaca.POSITION_AAPL]  # long 2
    venue.trading.activities = [
        alpaca.fill_activity(
            filled, "d7c568d4-4cf3-4326-8362-8dd41d44da80", 1, "174.7", first, "partial_fill", 1, 1
        ),
        alpaca.fill_activity(
            filled, "d83ed35d-fb7a-4b11-a888-0a5ba5298620", 1, "174.86", second, cum_qty=2
        ),
    ]
    node = build_node(trader(submit=False))

    run(node)

    order = node.cache.order(ClientOrderId("filled-before-start"))
    assert order is not None
    assert order.status == OrderStatus.FILLED
    fills = [e for e in order.events() if type(e).__name__ == "OrderFilled"]
    assert [f.trade_id.value for f in fills] == [
        "d7c568d4-4cf3-4326-8362-8dd41d44da80",
        "d83ed35d-fb7a-4b11-a888-0a5ba5298620",
    ]
    assert [f.last_px for f in fills] == [Price.from_str("174.7"), Price.from_str("174.86")]
    assert [f.ts_event for f in fills] == [dt_to_unix_nanos(first), dt_to_unix_nanos(second)]
    assert venue.trading.activity_requests[0]["direction"] == "desc"


@pytest.mark.parametrize("with_fill_activities", [False, True])
def test_older_filled_orders_and_no_position_at_alpaca_end_start_up_flat(
    venue, alpaca, with_fill_activities
) -> None:
    # Seen on paper: a filled buy whose closing sell is older than Alpaca's page of orders netted to a long
    # Alpaca did not hold, kept as an EXTERNAL position. Fill reports alone do not remove it, since the
    # sell's fills belong to an order the node is not given; the explicit flat report does.
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    bought = alpaca.alpaca_order(
        id="0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
        client_order_id="older-buy",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        qty="10",
        filled_qty="10",
        filled_avg_price="174.78",
    )
    sold = alpaca.alpaca_order(
        id="1d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
        client_order_id="older-sell",
        side="sell",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        qty="10",
        filled_qty="10",
        filled_avg_price="175",
    )
    venue.trading.orders = [bought]  # the sell is older than Alpaca's page of orders
    venue.trading.positions = []
    if with_fill_activities:
        venue.trading.activities = [
            alpaca.fill_activity(bought, str(uuid.UUID(int=1)), 10, "174.78", at),
            alpaca.fill_activity(sold, str(uuid.UUID(int=2)), 10, "175", at + timedelta(minutes=1)),
        ]
    node = build_node(trader(submit=False))

    run(node)

    assert node.cache.positions_open() == []
    order = node.cache.order(ClientOrderId("older-buy"))
    assert order is not None and order.status == OrderStatus.FILLED


def test_fills_alpaca_has_not_all_listed_are_left_to_the_engine(venue, alpaca) -> None:
    # Seen on paper: a node started a second after the last fill found only the first of two listed.
    filled = alpaca.alpaca_order(
        id="0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
        client_order_id="filled-before-start",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty="2",
        filled_avg_price="174.78",
    )
    a_second_ago = datetime.now(UTC) - timedelta(seconds=1)
    first = alpaca.fill_activity(
        filled,
        str(uuid.UUID(int=1)),
        1,
        "174.7",
        a_second_ago,
        "partial_fill",
        cum_qty=1,
        leaves_qty=1,
    )
    venue.trading.orders = [filled]
    venue.trading.positions = [alpaca.POSITION_AAPL]  # long 2
    venue.trading.activities = [first]
    node = build_node(trader(submit=False))

    run(node)

    order = node.cache.order(ClientOrderId("filled-before-start"))
    assert order is not None and order.status == OrderStatus.FILLED
    fills = [e for e in order.events() if type(e).__name__ == "OrderFilled"]
    assert str(uuid.UUID(int=1)) not in [f.trade_id.value for f in fills]
    assert sum(f.last_qty.as_double() for f in fills) == 2


def test_fill_history_that_cannot_be_read_leaves_fills_inferred(venue, alpaca, capfd) -> None:
    filled = alpaca.alpaca_order(
        client_order_id="filled-before-start",
        type="market",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty="2",
        filled_avg_price="174.78",
    )
    venue.trading.orders = [filled]
    venue.trading.positions = [alpaca.POSITION_AAPL]
    venue.trading.activities_error = alpaca.api_error(500, 50010000, "internal server error")
    node = build_node(trader(submit=False))

    run(node)

    order = node.cache.order(ClientOrderId("filled-before-start"))
    assert order is not None and order.status == OrderStatus.FILLED
    assert "fills are inferred from the order reports" in logged(capfd)


@pytest.mark.parametrize("change", [{"price": None}, {"qty": "not a number"}])
def test_fill_reports_skip_an_activity_with_a_value_they_cannot_read(venue, alpaca, change) -> None:
    order = alpaca.alpaca_order(status="filled", filled_qty="2")
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(order, str(uuid.UUID(int=1)), 1, "174.7", at),
        {**alpaca.fill_activity(order, str(uuid.UUID(int=2)), 1, "174.8", at), **change},
    ]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=1))]


def test_fill_report_maps_a_short_sale_to_a_sell(alpaca) -> None:
    assert (
        fill_report({**alpaca.FILL_ACTIVITY, "side": "sell_short"}, 0).order_side == OrderSide.SELL
    )


def test_an_order_whose_older_fills_fall_before_the_window_gets_no_fill_reports(
    venue, alpaca
) -> None:
    order = alpaca.alpaca_order(status="filled", filled_qty="2")
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(order, str(uuid.UUID(int=1)), 1, "174.7", at, "partial_fill", 1, 1),
        alpaca.fill_activity(
            order, str(uuid.UUID(int=2)), 1, "174.8", at + timedelta(minutes=2), cum_qty=2
        ),
    ]
    window_start = dt_to_unix_nanos(
        at + timedelta(minutes=1)
    )  # only the second fill is in the window
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command(start=window_start)))

    run_with_client(body)

    assert reports == []  # its cum_qty, 2, shows a fill the window left out


def test_fill_reports_for_one_order_keep_only_that_orders_fills(venue, alpaca) -> None:
    mine = alpaca.alpaca_order(
        id="0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f", status="filled", filled_qty="1"
    )
    other = alpaca.alpaca_order(
        id="1d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f", status="filled", filled_qty="1"
    )
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(mine, str(uuid.UUID(int=1)), 1, "174.7", at),
        alpaca.fill_activity(other, str(uuid.UUID(int=2)), 1, "174.8", at),
    ]
    venue.trading.ignores_order_filter = True  # as if Alpaca ignored `order_id`
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        command = fills_command(venue_order_id=VenueOrderId(mine["id"]))
        reports.extend(await client._fill_reports(command))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=1))]


def test_fill_report_maps_the_docs_fill_activity(alpaca) -> None:
    report = fill_report(alpaca.FILL_ACTIVITY, 7)

    assert report.trade_id.value == "2d7be4ff-d1f3-43e9-856a-0f5cf5c5088e"
    assert report.venue_order_id.value == "b5abe576-6a8a-49f3-a353-46b72c1ccae9"
    assert report.instrument_id == InstrumentId.from_str("AAPL.ALPACA")
    assert report.order_side == OrderSide.BUY
    assert report.last_qty == Quantity.from_int(2)
    assert report.last_px == Price.from_str("174.78")
    assert report.commission == Money(0, USD)
    assert report.liquidity_side == LiquiditySide.NO_LIQUIDITY_SIDE
    assert report.ts_event == dt_to_unix_nanos(datetime(2022, 2, 2, 18, 55, 9, 981_482, tzinfo=UTC))
    assert report.ts_init == 7
    assert report.account_id == ACCOUNT_ID


@pytest.mark.parametrize(
    "change",
    [
        {"id": "2d7be4ff-d1f3-43e9-856a-0f5cf5c5088e"},  # no `::`
        {"side": "short"},
        {"symbol": "BTC/USD"},
        {"qty": "0.5"},
    ],
)
def test_fill_report_refuses_an_activity_this_slice_cannot_map(alpaca, change) -> None:
    with pytest.raises(ValueError):
        fill_report({**alpaca.FILL_ACTIVITY, **change}, 0)


def fills_command(**fields: object) -> Any:
    """What `_fill_reports` reads of a `GenerateFillReports`, which Python cannot construct."""
    return SimpleNamespace(
        **({"venue_order_id": None, "instrument_id": None, "start": None, "end": None} | fields)
    )


def many_fills(alpaca, count: int) -> list[dict]:
    order = alpaca.alpaca_order(status="filled", filled_qty=str(count))
    start = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    return [
        alpaca.fill_activity(
            order, str(uuid.UUID(int=i)), 1, "174.78", start + timedelta(seconds=i)
        )
        for i in range(count)
    ]


def test_fill_reports_page_through_alpacas_activities(venue, alpaca) -> None:
    venue.trading.activities = many_fills(alpaca, 150)
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        venue.trading.activity_requests.clear()
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert len(reports) == 150
    first, second = venue.trading.activity_requests
    assert first == {"direction": "desc", "page_size": 100}
    assert second["page_token"].endswith(str(uuid.UUID(int=50)))  # the 100th, newest first


def test_fill_reports_stop_paging_before_the_window(venue, alpaca) -> None:
    venue.trading.activities = many_fills(alpaca, 150)
    window_start = dt_to_unix_nanos(datetime(2026, 11, 10, 15, 2, 0, tzinfo=UTC))  # the newest 30
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        venue.trading.activity_requests.clear()
        reports.extend(await client._fill_reports(fills_command(start=window_start)))

    run_with_client(body)

    assert len(reports) == 30
    assert len(venue.trading.activity_requests) == 1


def test_fill_reports_ask_alpaca_for_one_order_and_skip_what_they_cannot_map(venue, alpaca) -> None:
    order = alpaca.alpaca_order(status="filled", filled_qty="2")
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(order, "d7c568d4-4cf3-4326-8362-8dd41d44da80", 1, "174.7", at),
        {
            **alpaca.fill_activity(order, "d83ed35d-fb7a-4b11-a888-0a5ba5298620", 1, "174.8", at),
            "qty": "0.5",
        },
    ]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        venue.trading.activity_requests.clear()
        reports.extend(
            await client._fill_reports(fills_command(venue_order_id=VenueOrderId(order["id"])))
        )

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == ["d7c568d4-4cf3-4326-8362-8dd41d44da80"]
    assert venue.trading.activity_requests[0]["order_id"] == order["id"]


def test_open_order_placed_before_the_window_is_reported(venue, alpaca) -> None:
    venue.trading.orders = [alpaca.alpaca_order()]  # open, submitted on 2023-12-12
    reports: list = []
    queries: list = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    window = SimpleNamespace(  # the fields the hook reads from a GenerateOrderStatusReports command
        open_only=False,
        instrument_id=None,
        start=dt_to_unix_nanos(start),
        end=None,
    )

    async def body(client) -> None:
        # The node's startup reconciliation has made its own order query by now (see run_with_client).
        # Its default config lists no orders after that: open_check_interval_secs and
        # position_check_interval_secs are None, and the in-flight check has no order to look at. Only
        # the queries this call makes are taken.
        before = len(venue.trading.order_queries)
        reports.extend(await client._generate_order_status_reports(window))
        queries.extend(venue.trading.order_queries[before:])

    run_with_client(body)

    windowed, open_now = queries
    assert (windowed.status, windowed.after) == (QueryOrderStatus.ALL, start)
    assert (open_now.status, open_now.after, open_now.until) == (QueryOrderStatus.OPEN, None, None)
    assert [r.venue_order_id.value for r in reports] == [alpaca.ORDER_AAPL["id"]]


def test_open_order_outside_this_slice_fails_startup(venue, alpaca, capfd) -> None:
    venue.trading.orders = [
        alpaca.alpaca_order(
            type="trailing_stop", order_type="trailing_stop", trail_price="1", limit_price=None
        )
    ]
    node = build_node(trader(submit=False))

    with pytest.raises(Exception):  # noqa: B017 - the node's own startup failure type
        run(node)

    assert "is open with order type trailing_stop" in logged(capfd)


def test_startup_reconciliation_reports_open_stop_orders(venue, alpaca) -> None:
    stop = alpaca.alpaca_order(
        id="5d1f3c2a-0b7e-4c8f-9a6d-2e4b8c1f7a90",
        client_order_id="stop-before-start",
        type="stop",
        order_type="stop",
        limit_price=None,
        stop_price="140.5",
    )
    stop_limit = alpaca.alpaca_order(
        id="8a2e6f4b-3c1d-4e9a-b7f0-6d5c4b3a2918",
        client_order_id="stop-limit-before-start",
        type="stop_limit",
        order_type="stop_limit",
        limit_price="141",
        stop_price="140.5",
    )
    venue.trading.orders = [stop, stop_limit]
    node = build_node(trader(submit=False))

    run(node)

    working = {o.venue_order_id.value: o for o in node.cache.orders_open()}
    assert working[stop["id"]].order_type == OrderType.STOP_MARKET
    assert working[stop["id"]].trigger_price == Price.from_str("140.5000")
    assert working[stop["id"]].trigger_type == TriggerType.DEFAULT
    assert working[stop_limit["id"]].order_type == OrderType.STOP_LIMIT
    assert working[stop_limit["id"]].price == Price.from_str("141.0000")
    assert working[stop_limit["id"]].trigger_price == Price.from_str("140.5000")


def test_account_state_is_reported_on_connect(venue) -> None:
    node = build_node(trader(submit=False))

    run(node)

    account = node.cache.account(ACCOUNT_ID)
    assert account is not None
    balance = account.balance()
    assert str(balance.total) == "122086.50 USD"
    assert str(balance.free) == "122086.50 USD"


def test_query_account_reports_alpacas_account_again(venue) -> None:
    # Alpaca's stream pushes no account update: a strategy's query reads the account and reports it.
    strategy = trader(idle=True)
    balances: list[str] = []

    async def body(client) -> None:
        venue.trading.account = {**venue.trading.account, "cash": "100000"}
        strategy.query_account(ACCOUNT_ID, ClientId("ALPACA"))

        def total() -> str:
            account = client.cache.account(ACCOUNT_ID)
            assert account is not None
            return str(account.balance().total)

        async with asyncio.timeout(10):
            while total() != "100000.00 USD":
                await asyncio.sleep(0.01)
        balances.append(total())

    run_with_client(body, strategy)

    assert balances == ["100000.00 USD"]


@pytest.mark.parametrize(
    ("failure", "logged_as"),
    [
        (ConnectionError("connection reset"), "ConnectionError('connection reset')"),
        (None, "InvalidOperation"),  # an unparsable `cash`
    ],
)
def test_failed_account_query_leaves_the_state_as_it_was(venue, capfd, failure, logged_as) -> None:
    strategy = trader(idle=True)
    seen: list = []

    async def body(client) -> None:
        def state() -> tuple[str, int]:
            account = client.cache.account(ACCOUNT_ID)
            assert account is not None
            return str(account.balance().total), len(account.events)

        before = state()
        if failure is not None:

            def refuse() -> None:
                raise failure

            venue.trading.get_account = refuse
        else:
            venue.trading.account = {**venue.trading.account, "cash": "not a number"}
        strategy.query_account(ACCOUNT_ID, ClientId("ALPACA"))
        await until_idle(client, venue)
        seen.append((before, state()))

    run_with_client(body, strategy)

    ((before, after),) = seen
    assert after == before
    text = logged(capfd)
    assert "Alpaca account query failed:" in text
    assert logged_as in text


def test_query_for_another_account_is_refused(venue, capfd) -> None:
    async def body(client) -> None:
        await client._query_account(SimpleNamespace(account_id=AccountId("OTHER-001")))

    run_with_client(body)

    assert "Account query for OTHER-001: this client holds ALPACA-PAPER only" in logged(capfd)


def test_alpaca_trading_clients_are_built_for_paper(venue) -> None:
    run(build_node(trader(submit=False)))

    paper = {"api_key": PLACEHOLDER, "secret_key": PLACEHOLDER, "paper": True}
    assert venue.built["trading"] == paper
    assert venue.built["stream"] == paper
    assert venue.built["assets"] == paper


def test_shutdown_stops_and_joins_the_trade_stream_thread(venue) -> None:
    run(build_node(trader()))

    assert venue.stream.run_threads == ["ALPACA-trade-updates"]
    assert venue.stream.stopped.is_set()
    alive = [t.name for t in threading.enumerate() if t.name in venue.stream.run_threads]
    assert alive == []


# -- the paper-only check, without a node ------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"paper": False}, "refuses paper=False: paper trading only"),
        ({"paper": "true"}, "refuses paper='true': paper trading only"),
        (
            {"url_override": "https://paper-api.alpaca.markets"},
            "refuses url_override='https://paper-api.alpaca.markets' (a URL)",
        ),
        (
            {"url_override": "https://api.alpaca.markets"},
            "(Alpaca's live trading host): the client connects to Alpaca's paper Trading API only",
        ),
        (
            {"base_url": "wss://api.alpaca.markets/stream"},
            "refuses base_url='wss://api.alpaca.markets/stream', Alpaca's live trading host",
        ),
    ],
)
def test_config_refuses_anything_but_paper(settings, message) -> None:
    from canon_alpaca.execution import PaperOnlyError

    with pytest.raises(PaperOnlyError) as refused:
        AlpacaExecutionClientConfig(**settings)

    assert message in str(refused.value)
    assert "(AGENTS.md rule 8, ADR 0009)" in str(refused.value)


def test_config_refuses_a_setting_it_does_not_have() -> None:
    with pytest.raises(TypeError, match="has no setting 'feed'"):
        AlpacaExecutionClientConfig(feed="sip")


def test_config_accepts_paper_and_keeps_the_common_fields() -> None:
    config = AlpacaExecutionClientConfig(paper=True, instrument_provider=provider_config())

    assert config.instrument_provider.load_ids == [str(AAPL)]
    assert "paper" not in config.dict()
    assert "url_override" not in config.dict()


def test_node_refuses_an_exec_config_that_skipped_the_check(venue) -> None:
    from nautilus_trader.config import ExecutionClientConfig

    # A plain ExecutionClientConfig drops unknown keywords without a word, paper=False among them, so the
    # client takes only the config that refuses it. The node builder re-raises as RuntimeError.
    plain = ExecutionClientConfig(paper=False)
    config = LiveNodeConfig(
        environment=Environment.SANDBOX, trader_id=TRADER, exec_clients={"ALPACA": plain}
    )
    with pytest.raises(
        RuntimeError, match="takes an AlpacaExecutionClientConfig.*paper trading only"
    ):
        LiveNode.build(
            "CANON-ALPACA-EXEC", config, exec_factories={"ALPACA": AlpacaExecutionClientFactory}
        )
