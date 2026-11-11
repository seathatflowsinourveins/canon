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
import concurrent.futures
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
import canon_alpaca.factories
import canon_alpaca.providers
import pytest
import requests
from alpaca.common.rest import RESTClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
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
from canon_alpaca.execution import FilledReading
from canon_alpaca.execution import fill_report
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import TimeoutAdapter
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.common import Logger
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveExecutionEngineConfig
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
        self.display_qty: int | None = None  # a stop-market order's display quantity, if set
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
                display_qty=Quantity.from_int(self.display_qty)
                if self.display_qty is not None
                else None,
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
    exec_engine: LiveExecutionEngineConfig | None = None,
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
                exec_engine=exec_engine,
                data_clients={"ALPACA": DataClientConfig(instrument_provider=provider_config())},
                exec_clients={"ALPACA": exec_config()},
            ),
            data_factories={"ALPACA": AlpacaDataClientFactory},
            exec_factories={"ALPACA": exec_factory},
        )
    else:
        assert exec_engine is None, "the builder path here takes the engine's defaults"
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
    exec_engine: LiveExecutionEngineConfig | None = None,
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

    node = build_node(strategy, exec_factory=Capturing, exec_engine=exec_engine)

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
            or not client._idle.is_set()
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
        ({"order_type": "stop", "display_qty": 1}, "display quantity is not supported"),
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
    # The submit's answer timed out, so the order has no venue ID and the cancel first looks it up; that
    # lookup's connection times out, so neither the lookup nor the cancel reached Alpaca.
    venue.trading.script = "silent"
    venue.trading.submit_error = requests.ReadTimeout("read timed out")
    venue.trading.lookup_error = requests.ConnectTimeout("connect timed out")
    strategy = trader(order_type="limit", cancel_on="OrderSubmitted")
    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run(build_node(strategy))
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert strategy.names()[-1] == "OrderCancelRejected", strategy.names()
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
    """A local HTTP server that accepts each connection and, for `/hang`, never answers; Alpaca's fill
    activities path it answers 200 with a body that is not JSON, as an intermediary's error page might."""

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
            if b"/account/activities/FILL" in request:
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 13\r\n"
                    b"Connection: close\r\n\r\n<html></html>"
                )
                connection.close()
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


def test_fill_history_answered_with_a_body_that_is_not_json_leaves_fills_inferred(
    venue, capfd
) -> None:
    # alpaca-py's `get` returns `response.json()` for a 200; requests raises its `JSONDecodeError`,
    # which is both an `OSError` and a `ValueError`, so the history counts as unread.
    server = SilentServer()
    rest = with_http_timeout(
        RESTClient(
            base_url=f"http://127.0.0.1:{server.port}",
            api_version="v2",
            api_key=PLACEHOLDER,
            secret_key=PLACEHOLDER,
        )
    )
    reports: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        trading = client._trading
        client._trading = rest  # ty: ignore[invalid-assignment] - `get` is all the fill history uses
        try:
            reports.append(await client._fill_reports(fills_command()))
        finally:
            client._trading = trading

    try:
        run_with_client(body)
    finally:
        server.close()

    assert reports == [[]]
    assert "fills are inferred from the order reports" in logged(capfd)


def test_with_http_timeout_mounts_its_own_timeout_on_both_schemes() -> None:
    # A session holding this package's adapter with another timeout, or on HTTPS only, gets the
    # (3.05, 27) adapter on both schemes (ADR 0039); one already holding it keeps the same adapter.
    session = requests.Session()
    session.mount("https://", TimeoutAdapter((999.0, 999.0)))
    client = with_http_timeout(SimpleNamespace(_session=session))
    expected = (
        canon_alpaca.providers.CONNECT_TIMEOUT_SECS,
        canon_alpaca.providers.READ_TIMEOUT_SECS,
    )
    adapter = session.adapters["https://"]
    assert isinstance(adapter, TimeoutAdapter) and adapter.timeout == expected
    assert session.adapters["http://"] is adapter

    with_http_timeout(client)
    assert session.adapters["https://"] is adapter and session.adapters["http://"] is adapter

    session = requests.Session()
    session.mount("https://", TimeoutAdapter(expected))
    with_http_timeout(SimpleNamespace(_session=session))
    assert isinstance(session.adapters["http://"], TimeoutAdapter)
    assert session.adapters["http://"] is session.adapters["https://"]


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


def forget(client: AlpacaExecutionClient) -> None:
    """Drop what the client keeps of each order's replaces, as a new client after a restart has none: its
    records and its index of Alpaca order IDs. The node's cache still holds the orders."""
    client._records.clear()
    client._index.clear()


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
            forget(client)
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
        forget(client)
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
        forget(client)
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
        client._records[strategy.client_order_id].answers.clear()  # the PATCH answer is lost
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
        forget(client)
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
        assert strategy.client_order_id not in client._records  # its pending modify with it

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
        client._records[strategy.client_order_id].answers.clear()
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
        left.extend(a for record in client._records.values() for a in record.answers)

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
        forget(client)
        # What a trace in the window records: the chain's root, the original's client order ID, which
        # the node did not hold then (before the fix, the miss was recorded as tracing to nothing).
        client._index[new["id"]] = ClientOrderId(placed_order(venue, 0)["client_order_id"])
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
        forget(client)
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


@pytest.mark.parametrize("client_order_id", ["", "\u8ba2\u5355-1"])
def test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped(
    venue, alpaca, client_order_id
) -> None:
    # An order made outside the node (Alpaca's dashboard, say) with an empty or non-ASCII client order ID:
    # a closed one is skipped with a warning; start-up reconciliation goes on.
    venue.trading.orders = [
        alpaca.alpaca_order(
            id="3e1f7c2a-5b9d-4a6e-8c0f-1d2b3c4e5f60",
            client_order_id=client_order_id,
            status="canceled",
        )
    ]
    node = build_node(trader(submit=False))

    run(node)

    assert node.cache.orders_open() == []


def test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name(
    venue, alpaca, capfd
) -> None:
    venue.trading.orders = [alpaca.alpaca_order(client_order_id="", status="new")]
    node = build_node(trader(submit=False))

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(node)

    assert (
        f"Alpaca order {alpaca.ORDER_AAPL['id']} (AAPL) is open with a client order ID NautilusTrader "
        "cannot hold (invalid string"
    ) in logged(capfd)


def test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own(
    venue, alpaca
) -> None:
    # A dashboard order with a non-ASCII client order ID, replaced: the open replacement has an ID the node
    # can hold and is reconciled under it; the trace stops at the unholdable original.
    original = alpaca.alpaca_order(
        id="6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        client_order_id="\u8ba2\u5355-1",
        status="replaced",
        replaced_by="7b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
    )
    replacement = alpaca.alpaca_order(
        id="7b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
        client_order_id="alpaca-made-1",
        status="new",
        replaces=original["id"],
    )
    venue.trading.orders = [original, replacement]
    node = build_node(trader(submit=False))

    run(node)

    (working,) = node.cache.orders_open()
    assert working.client_order_id == ClientOrderId("alpaca-made-1")


def test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer(venue, alpaca) -> None:
    # A cancel that got no answer leaves the order PENDING_CANCEL while it still works at Alpaca; a later
    # cancel-all must send it again, as upstream's Kraken spot, Binance spot, Bybit and Interactive Brokers
    # adapters include every open order.
    venue.trading.script = "accept"
    strategy = cancel_all(orders=[(OrderSide.BUY, "140.00")])
    first_cancel: list = []

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not strategy.accepted:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        venue.trading.cancel_error = ConnectionError(
            "read timed out"
        )  # the first cancel: no answer
        client_order_id = strategy.placed[0]
        strategy.cancel_order(client_order_id)
        await until_idle(client, venue)
        first_cancel.append(len(venue.trading.cancels))
        venue.trading.cancel_error = None
        await client._cancel_all_orders(SimpleNamespace(instrument_id=AAPL, order_side=None))
        await until_idle(client, venue)

    strategy.sent = True  # the strategy sends no cancel-all itself; the test does
    run_with_client(body, strategy)

    assert first_cancel == [1]
    assert len(venue.trading.cancels) == 2, venue.trading.cancels
    assert "OrderCanceled" in strategy.final().values()


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

    # Every open order however old, read first, and every order in the engine's window.
    statuses = [q.status for q in venue.trading.order_queries]
    assert statuses == [QueryOrderStatus.OPEN, QueryOrderStatus.ALL]
    (working,) = node.cache.orders_open()
    assert working.venue_order_id.value == open_limit["id"]
    assert working.status == OrderStatus.ACCEPTED
    assert working.price == Price.from_str("150.0000")
    (position,) = node.cache.positions_open()
    assert position.quantity == Quantity.from_int(2)
    assert Decimal(str(position.avg_px_open)) == Decimal("174.78")
    # The filled order's report, not only the count: filled in full at Alpaca's average price, long.
    (filled,) = [o for o in node.cache.orders() if o.venue_order_id.value == filled_market["id"]]
    assert (filled.status, filled.filled_qty) == (OrderStatus.FILLED, Quantity.from_int(2))
    assert Decimal(str(filled.avg_px)) == Decimal("174.78")
    assert position.side.name == "LONG"


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


FILLED_TWO = {
    "id": "0d5e6a3b-6f3c-4c1e-9d3e-1a2b3c4d5e6f",
    "client_order_id": "filled-before-start",
    "type": "market",
    "order_type": "market",
    "limit_price": None,
    "status": "filled",
    "filled_qty": "2",
    "filled_avg_price": "174.78",
}
"""A market buy of 2, filled before start, as Alpaca's order endpoint shows it."""


@pytest.mark.parametrize(
    ("age", "fields"),
    [
        (timedelta(seconds=1), {}),  # seen on paper: a node started a second after the last fill
        (timedelta(seconds=120), {}),  # a lag longer than a minute
        (timedelta(days=1), {}),
        (timedelta(seconds=1), {"cum_qty": None, "leaves_qty": None}),  # the two fields absent
    ],
    ids=["1s", "120s", "1d", "no-cum-or-leaves"],
)
def test_fills_alpaca_has_not_all_listed_are_left_to_the_engine(venue, alpaca, age, fields) -> None:
    # Only the first of two fills is listed; Alpaca's order shows both. Whatever the listed fill's age,
    # and whether or not it carries `cum_qty` and `leaves_qty`, its report is dropped and the engine
    # infers the order's fills, rather than taking one listed fill and inferring the rest.
    filled = alpaca.alpaca_order(**FILLED_TWO)
    first = alpaca.fill_activity(
        filled,
        str(uuid.UUID(int=1)),
        1,
        "174.7",
        datetime.now(UTC) - age,
        "partial_fill",
        cum_qty=1,
        leaves_qty=1,
    )
    for name, value in fields.items():
        if value is None:
            del first[name]
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


def test_an_order_whose_listed_fills_alpaca_shows_as_all_its_fills_keeps_them(
    venue, alpaca
) -> None:
    # Filled 1 of 2 and then canceled, a day ago: its one fill leaves a share, and Alpaca's order shows 1
    # filled, so the listed fill is all of them and is reported.
    order = alpaca.alpaca_order(
        **{**FILLED_TWO, "status": "canceled", "filled_qty": "1", "filled_avg_price": "174.7"}
    )
    venue.trading.orders = [order]
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=1)),
            1,
            "174.7",
            datetime.now(UTC) - timedelta(days=1),
            "partial_fill",
            cum_qty=1,
            leaves_qty=1,
        )
    ]
    reports: list = []
    lookups: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        get_order_by_id = venue.trading.get_order_by_id

        def counted_lookup(order_id: object):
            lookups.append(order_id)
            return get_order_by_id(order_id)

        venue.trading.get_order_by_id = counted_lookup
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=1))]
    assert lookups == [order["id"]]


def test_an_order_whose_last_fill_is_listed_costs_no_order_lookup(venue, alpaca) -> None:
    # The cost of the completeness check: an order whose newest listed fill leaves nothing to fill, and
    # whose listed fills add up to its `cum_qty`, is shown complete by the activities alone.
    filled = alpaca.alpaca_order(**FILLED_TWO)
    at = datetime.now(UTC) - timedelta(days=1)
    venue.trading.activities = [
        alpaca.fill_activity(
            filled, str(uuid.UUID(int=1)), 1, "174.7", at, "partial_fill", cum_qty=1, leaves_qty=1
        ),
        alpaca.fill_activity(
            filled, str(uuid.UUID(int=2)), 1, "174.86", at + timedelta(seconds=2), cum_qty=2
        ),
    ]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        def no_lookup(order_id: object):
            raise AssertionError(f"looked up {order_id}")

        venue.trading.get_order_by_id = no_lookup
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=2)), str(uuid.UUID(int=1))]


def test_several_orders_to_check_are_read_in_one_request(venue, alpaca) -> None:
    # Two orders, each filled 1 of 2 and then canceled: their fills are checked against Alpaca's newest
    # orders in one `get_orders`, not one `get_order_by_id` each.
    at = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    orders = [
        alpaca.alpaca_order(
            **{
                **FILLED_TWO,
                "status": "canceled",
                "filled_qty": "1",
                "id": str(uuid.UUID(int=100 + i)),
                "client_order_id": f"canceled-{i}",
            }
        )
        for i in range(2)
    ]
    venue.trading.orders = orders
    venue.trading.activities = [
        alpaca.fill_activity(order, str(uuid.UUID(int=i + 1)), 1, "174.7", at, "partial_fill", 1, 1)
        for i, order in enumerate(orders)
    ]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        def no_lookup(order_id: object):
            raise AssertionError(f"looked up {order_id}")

        venue.trading.get_order_by_id = no_lookup
        venue.trading.order_queries.clear()
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert sorted(r.trade_id.value for r in reports) == [
        str(uuid.UUID(int=1)),
        str(uuid.UUID(int=2)),
    ]
    (query,) = venue.trading.order_queries
    assert query.status == QueryOrderStatus.ALL
    assert query.symbols is None


@pytest.mark.parametrize("listing", ["fails", "misses-one"])
def test_orders_the_batch_read_does_not_give_are_read_one_by_one(venue, alpaca, listing) -> None:
    at = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    orders = [
        alpaca.alpaca_order(
            **{
                **FILLED_TWO,
                "status": "canceled",
                "filled_qty": "1",
                "id": str(uuid.UUID(int=100 + i)),
                "client_order_id": f"canceled-{i}",
            }
        )
        for i in range(2)
    ]
    venue.trading.orders = orders
    venue.trading.activities = [
        alpaca.fill_activity(order, str(uuid.UUID(int=i + 1)), 1, "174.7", at, "partial_fill", 1, 1)
        for i, order in enumerate(orders)
    ]
    reports: list = []
    lookups: list[str] = []

    async def body(client: AlpacaExecutionClient) -> None:
        get_order_by_id = venue.trading.get_order_by_id

        def counted_lookup(order_id: object):
            lookups.append(str(order_id))
            return get_order_by_id(order_id)

        def listed(filter: object = None) -> list:
            if listing == "fails":
                raise requests.ReadTimeout("read timed out")
            return [AlpacaOrder(**orders[0])]

        venue.trading.get_order_by_id = counted_lookup
        venue.trading.get_orders = listed
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert sorted(r.trade_id.value for r in reports) == [
        str(uuid.UUID(int=1)),
        str(uuid.UUID(int=2)),
    ]
    expected = [o["id"] for o in orders] if listing == "fails" else [orders[1]["id"]]
    assert lookups == expected


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "a status alpaca-py does not know"},  # pydantic's ValidationError, a ValueError
        None,  # an empty body: alpaca-py returns None and builds `Order(**None)`, a TypeError
    ],
    ids=["unknown-value", "empty-body"],
)
def test_an_order_alpaca_answers_in_a_shape_alpaca_py_refuses_leaves_fills_inferred(
    venue, alpaca, capfd, answer
) -> None:
    # alpaca-py's `get_order_by_id` returns `Order(**response)`: an order Alpaca answers in a shape
    # 0.45.0 cannot build must not fail start-up from the fill check.
    filled = alpaca.alpaca_order(**FILLED_TWO)
    venue.trading.orders = [filled]
    venue.trading.positions = [alpaca.POSITION_AAPL]  # long 2
    venue.trading.activities = [
        alpaca.fill_activity(
            filled,
            str(uuid.UUID(int=1)),
            1,
            "174.7",
            datetime.now(UTC) - timedelta(seconds=1),
            "partial_fill",
            cum_qty=1,
            leaves_qty=1,
        )
    ]

    def unreadable(order_id: object):
        if answer is None:
            return AlpacaOrder(**cast("dict", answer))
        return AlpacaOrder(**{**filled, **answer})

    venue.trading.get_order_by_id = unreadable
    node = build_node(trader(submit=False))

    run(node)

    order = node.cache.order(ClientOrderId("filled-before-start"))
    assert order is not None and order.status == OrderStatus.FILLED
    fills = [e for e in order.events() if type(e).__name__ == "OrderFilled"]
    assert str(uuid.UUID(int=1)) not in [f.trade_id.value for f in fills]
    assert "not read to check its fills" in logged(capfd)


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
def test_fill_reports_skip_an_activity_with_a_value_they_cannot_read(
    venue, alpaca, capfd, change
) -> None:
    # Two one-share fills of a two-share order, as Alpaca lists them (`cum_qty` 1 then 2), the second
    # unreadable: it is skipped with a warning, and the order's one readable fill cannot be shown to be
    # all of them, so it is left to the engine. Another order's complete fill is still reported.
    order = alpaca.alpaca_order(status="filled", filled_qty="2")
    other = alpaca.alpaca_order(
        id="7f000000-0000-4000-8000-000000000001",
        client_order_id="other-filled",
        status="filled",
        qty="1",
        filled_qty="1",
    )
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=1)),
            1,
            "174.7",
            at - timedelta(seconds=1),
            "partial_fill",
            cum_qty=1,
            leaves_qty=1,
        ),
        {
            **alpaca.fill_activity(
                order, str(uuid.UUID(int=2)), 1, "174.8", at, cum_qty=2, leaves_qty=0
            ),
            **change,
        },
        alpaca.fill_activity(other, str(uuid.UUID(int=3)), 1, "174.9", at - timedelta(seconds=2)),
    ]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=3))]
    log = logged(capfd)
    assert f"Alpaca fill activity {venue.trading.activities[1]['id']} is not reported" in log
    assert f"Alpaca lists only some fills of order {order['id']}" in log


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


def test_a_full_page_whose_last_activity_has_no_id_keeps_the_pages_fills(
    venue, alpaca, capfd
) -> None:
    # Paging needs the last activity's `id` as `page_token`; without it the read stops, and the page's
    # readable fills are still reported. 150 orders, one fill each, newest first.
    start = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    activities = []
    for i in range(150):
        order = alpaca.alpaca_order(
            id=str(uuid.UUID(int=1000 + i)), status="filled", filled_qty="1"
        )
        activities.append(
            alpaca.fill_activity(
                order, str(uuid.UUID(int=i)), 1, "174.78", start + timedelta(seconds=i)
            )
        )
    del activities[50]["id"]  # the 100th newest, the last of the first page
    venue.trading.activities = activities
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        venue.trading.activity_requests.clear()
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert len(reports) == 99
    assert len(venue.trading.activity_requests) == 1
    assert "older fills, if any, are not reported" in logged(capfd)


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
    # One order's fills, asked for by its ID; its second fill is fractional, which this slice cannot map,
    # so the first alone cannot be shown to be all of them and none is reported.
    order = alpaca.alpaca_order(status="filled", filled_qty="2")
    at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            "d7c568d4-4cf3-4326-8362-8dd41d44da80",
            1,
            "174.7",
            at - timedelta(seconds=1),
            "partial_fill",
            cum_qty=1,
            leaves_qty=1,
        ),
        {
            **alpaca.fill_activity(
                order,
                "d83ed35d-fb7a-4b11-a888-0a5ba5298620",
                1,
                "174.8",
                at,
                cum_qty=2,
                leaves_qty=0,
            ),
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

    assert reports == []
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

    open_now, windowed = queries
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


def timed_stop(
    prepare: Callable[[AlpacaExecutionClient], None],
) -> tuple[float, BaseException | None]:
    """Start a hosted node, call `prepare` with its connected execution client, stop the node, and return
    how long its run took to end after the stop, and what the run raised."""
    clients: list[AlpacaExecutionClient] = []

    class Capturing(AlpacaExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaExecutionClientFactory.create(**kwargs)
            clients.append(client)
            return client

    node = build_node(trader(idle=True), exec_factory=Capturing)
    outcome: list[tuple[float, BaseException | None]] = []

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        handle = node.handle()
        async with asyncio.timeout(10):
            while not handle.is_running:
                await asyncio.sleep(0.01)
        prepare(clients[0])
        handle.stop()
        stopped = time.monotonic()
        try:
            await task
            raised = None
        except RuntimeError as e:  # how a hosted run reports a failed disconnect
            raised = e
        outcome.append((time.monotonic() - stopped, raised))

    asyncio.run(main())
    return outcome[0]


def test_a_trade_update_task_that_does_not_end_when_cancelled_does_not_hold_the_disconnect(
    venue, monkeypatch, capfd
) -> None:
    # The disconnect lets the task applying trade updates finish, then cancels it and waits for it; both
    # waits are bounded, as NautilusTrader's own adapters bound their task drains
    # (`TaskGroup::finish_shutdown`).
    monkeypatch.setattr(canon_alpaca.execution, "TASK_ABORT_SECS", 0.2)
    released = threading.Event()
    timer = threading.Timer(8, released.set)

    def prepare(client: AlpacaExecutionClient) -> None:
        async def stubborn() -> None:
            while not released.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    continue  # stands for an update whose application does not end at once

        assert client._consumer is not None
        client._consumer.cancel()
        # On the loop directly: a task made with the client's `create_task` is also drained by
        # NautilusTrader's own disconnect, within the node's `timeout_disconnection_secs`.
        client._consumer = asyncio.get_running_loop().create_task(stubborn())
        timer.start()

    try:
        took, raised = timed_stop(prepare)
    finally:
        released.set()
        timer.cancel()

    # The client gives up after its bound and says why; the node then waits out its own
    # `timeout_disconnection_secs` (5 s here) for the engine, as it does for any failed disconnect.
    # Unbounded, the client's wait held the disconnect until the node's timeout cut it off instead.
    assert raised is not None and str(raised).startswith("Failed to disconnect execution clients")
    assert "trade updates task did not end within 1.2 s of the disconnect" in logged(capfd)
    assert took < 7, f"the run ended {took:.1f} s after the stop"


@pytest.mark.parametrize(("apply_secs", "finished"), [(0.3, True), (5.0, False)])
def test_an_update_being_applied_at_disconnect_gets_a_grace_period(
    venue, monkeypatch, apply_secs, finished
) -> None:
    # Upstream's drain: the update being applied may finish within the grace bound (1 s here, as
    # upstream's Coinbase adapter); past it, the task is cancelled.
    outcome: list[str] = []

    def prepare(client: AlpacaExecutionClient) -> None:
        async def apply(update: object) -> None:
            try:
                await asyncio.sleep(apply_secs)
                outcome.append("finished")
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        monkeypatch.setattr(client, "_apply_trade_update", apply)
        client._updates.put_nowait(cast("Any", SimpleNamespace(event="fill")))

    took, raised = timed_stop(prepare)

    assert outcome == (["finished"] if finished else ["cancelled"])
    assert raised is None
    assert took < 4, f"the run ended {took:.1f} s after the stop"


def test_a_busy_default_executor_does_not_hold_the_disconnect(venue) -> None:
    # Every alpaca-py call runs in the node loop's default executor; a disconnect that needed a worker of
    # its own to wait for the stream thread would wait for the executor first.
    released = threading.Event()

    def prepare(client: AlpacaExecutionClient) -> None:
        loop = client._owner_loop
        assert loop is not None
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        loop.run_in_executor(None, released.wait, 8)  # a call that has not come back

    try:
        took, raised = timed_stop(prepare)
    finally:
        released.set()

    assert raised is None
    assert took < 4, f"the run ended {took:.1f} s after the stop"


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
            {"url_override": "https://api.alpaca.markets./v2"},
            "refuses url_override='https://api.alpaca.markets./v2' (Alpaca's live trading host)",
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


# -- Second reader's findings on start-up reports, tracing and cancel (each failed before its fix) -----


def test_report_request_with_no_window_still_reads_every_open_order(venue, alpaca) -> None:
    # 500 closed orders newer than an open one: Alpaca's newest 500 of every status miss the open order,
    # so the open orders are read on their own whatever the window, as upstream's Kraken spot reads its
    # open orders before its order history.
    base = datetime(2024, 1, 1, tzinfo=UTC)
    venue.trading.orders = [alpaca.alpaca_order()] + [  # open, submitted on 2023-12-12
        alpaca.alpaca_order(
            id=str(uuid.UUID(int=n + 1)),
            client_order_id=f"closed-{n}",
            status="canceled",
            submitted_at=(base + timedelta(minutes=n)).isoformat(),
        )
        for n in range(500)
    ]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert alpaca.ORDER_AAPL["id"] in [r.venue_order_id.value for r in reports]


EMPTY_ID_ORDERS = {
    "node": "1a000000-0000-4000-8000-000000000001",
    "replacement": "1a000000-0000-4000-8000-000000000002",
    "unrelated": "1a000000-0000-4000-8000-000000000003",
}


@pytest.mark.parametrize("blank", ["", " "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("first", ["replacement", "unrelated"])
def test_orders_with_an_empty_client_id_are_each_traced_on_their_own(
    venue, alpaca, first, blank
) -> None:
    # Two orders made outside the node, both with an empty (or all-whitespace) client order ID, which
    # NautilusTrader refuses: one replaces the node's order, the other is unrelated. Neither trace may
    # stand for the other, in either order.
    ids = EMPTY_ID_ORDERS
    venue.trading.orders = [
        alpaca.alpaca_order(
            id=ids["node"],
            client_order_id="node-1",
            status="replaced",
            replaced_by=ids["replacement"],
        )
    ]
    orders = {
        "replacement": AlpacaOrder(
            **alpaca.alpaca_order(
                id=ids["replacement"], client_order_id=blank, status="new", replaces=ids["node"]
            )
        ),
        "unrelated": AlpacaOrder(
            **alpaca.alpaca_order(
                id=ids["unrelated"],
                client_order_id=blank,
                status="filled",
                filled_qty="2",
                filled_avg_price="150",
            )
        ),
    }
    traced: dict[str, ClientOrderId | None] = {}

    async def body(client) -> None:
        forget(client)
        for name in sorted(orders, key=lambda n: n != first):
            traced[name] = await client._root_id(orders[name])

    run_with_client(body)

    assert traced == {"replacement": ClientOrderId("node-1"), "unrelated": None}


def test_an_update_for_an_unrelated_order_with_an_empty_client_id_leaves_the_node_order(
    venue, alpaca
) -> None:
    # The stream path: once a replacement with an empty client order ID has been traced to the node's
    # order, an unrelated outside order with an empty one is still an order the node does not hold.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"  # the strategy does not stop the node; the test does

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        replacement = alpaca.alpaca_order(
            id="4c000000-0000-4000-8000-000000000001",
            client_order_id="",
            status="new",
            replaces=placed["id"],
        )
        assert await client._node_order_id(AlpacaOrder(**replacement)) == strategy.client_order_id
        other = alpaca.alpaca_order(
            id="4c000000-0000-4000-8000-000000000002", client_order_id="", status="canceled"
        )
        venue.stream.send({"event": "canceled", "order": other, "timestamp": other["updated_at"]})
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted"]


TRACE_FAILURES = [
    requests.ReadTimeout("read timed out"),
    500,  # an HTTP status is built in the test: an `APIError` needs the fixture
    404,  # an order Alpaca names in `replaces` and does not find is a chain that cannot be read
    ValueError("a body alpaca-py's Order model refuses"),
    TypeError("Order() argument after ** must be a mapping"),
]
TRACE_FAILURE_IDS = ["timeout", "http-500", "http-404", "value", "type"]


def replacement_of_an_unread_order(alpaca, venue, status: str, error: Exception | int) -> dict:
    """A replacement whose prior order is not among the orders listed and cannot be read."""
    prior = "2b000000-0000-4000-8000-000000000001"
    replacement = alpaca.alpaca_order(
        id="2b000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-2",
        status=status,
        replaces=prior,
    )
    venue.trading.orders = [replacement]
    failure = (
        alpaca.api_error(error, error * 100000, "order not found" if error == 404 else "internal")
        if isinstance(error, int)
        else error
    )
    venue.trading.order_lookup_error = lambda order_id: failure if order_id == prior else None
    return replacement


@pytest.mark.parametrize("error", TRACE_FAILURES, ids=TRACE_FAILURE_IDS)
def test_a_working_replacement_whose_trace_fails_fails_startup_by_name(
    venue, alpaca, capfd, error
) -> None:
    # The node cannot tell whose order a working replacement is: start-up fails, as for any open order this
    # slice cannot represent, rather than going on without it.
    replacement = replacement_of_an_unread_order(alpaca, venue, "new", error)
    node = build_node(trader(submit=False))

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(node)

    assert f"Alpaca order {replacement['id']} (AAPL) is open and tracing it failed" in logged(capfd)


@pytest.mark.parametrize("error", TRACE_FAILURES, ids=TRACE_FAILURE_IDS)
def test_a_closed_replacement_whose_trace_fails_is_skipped(venue, alpaca, capfd, error) -> None:
    replacement = replacement_of_an_unread_order(alpaca, venue, "canceled", error)
    node = build_node(trader(submit=False))

    run(node)

    assert node.cache.orders_open() == []
    assert f"Alpaca order {replacement['id']} is not reported: tracing it failed" in logged(capfd)


def test_cancel_refused_for_an_order_alpaca_canceled_unseen_cancels_it(venue, alpaca) -> None:
    # The stream dropped Alpaca's `canceled`: the node's cancel is refused 422 and the order reads canceled,
    # so the order is canceled, as upstream's Bybit adapter emits `OrderCanceled` from an order it reads
    # canceled; the late `canceled` update adds nothing.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"  # the strategy does not stop the node; the test does
    canceled_at: list[datetime] = []

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        canceled_at.append(datetime.now(UTC))
        placed.update(status="canceled", canceled_at=canceled_at[0].isoformat())  # no update sent
        venue.trading.cancel_error = alpaca.api_error(422, 42210000, "order is not cancelable")
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        async with asyncio.timeout(5):
            while "OrderCanceled" not in strategy.names():
                await asyncio.sleep(0.01)
        venue.trading.update(placed["id"], "canceled", status="canceled")
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    assert strategy.events[-1].ts_event == dt_to_unix_nanos(
        canceled_at[0]
    )  # Alpaca's `canceled_at`


def cancel_after_alpaca_moved(
    venue, alpaca, move: Callable[[dict], None]
) -> tuple[Trader, Callable[[AlpacaExecutionClient], Awaitable[None]]]:
    """A limit buy, accepted; then `move` changes Alpaca's order with no update sent, and the node's
    cancel is refused 422."""
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"  # the strategy does not stop the node; the test does

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        move(placed)
        venue.trading.cancel_error = alpaca.api_error(422, 42210000, "order is not cancelable")
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        await until_idle(client, venue)

    return strategy, body


def test_cancel_refused_for_an_order_alpaca_canceled_after_an_unseen_fill_is_left_open(
    venue, alpaca, capfd
) -> None:
    # Alpaca filled one share and then canceled, both while the stream was down: canceling here would end
    # the order with the fill missing, and every later update for it would be dropped. It is left for
    # reconciliation, which infers the fill.
    strategy, body = cancel_after_alpaca_moved(
        venue,
        alpaca,
        lambda placed: placed.update(
            status="canceled", filled_qty="1", canceled_at=datetime.now(UTC).isoformat()
        ),
    )

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderPendingCancel"]
    assert "canceled with 1 filled, where" in logged(capfd)


def test_cancel_redirected_to_a_replacement_alpaca_canceled_unseen_cancels_the_order(
    venue, alpaca
) -> None:
    # The node's order was replaced and the replacement canceled, none of it seen: the cancel of the old
    # order is redirected to the replacement, refused 422, and the replacement, traced to the node's order
    # through `replaces`, reads canceled.
    replacement_id = "5d000000-0000-4000-8000-000000000001"

    def replaced_and_canceled(placed: dict) -> None:
        venue.trading.orders.append(
            {
                **placed,
                "id": replacement_id,
                "client_order_id": "alpaca-made-5",
                "replaces": placed["id"],
                "status": "canceled",
                "canceled_at": datetime.now(UTC).isoformat(),
            }
        )
        placed.update(status="replaced", replaced_by=replacement_id)

    strategy, body = cancel_after_alpaca_moved(venue, alpaca, replaced_and_canceled)

    run_with_client(body, strategy)

    assert strategy.names()[-1] == "OrderCanceled"
    assert strategy.events[-1].venue_order_id == VenueOrderId(replacement_id)


def test_an_order_that_closes_between_the_two_order_reads_is_reported_closed(venue, alpaca) -> None:
    # The open orders are read first and the windowed history second, as Kraken spot reads them, and the
    # later copy is kept: an order filled between the reads is reported filled.
    venue.trading.orders = [alpaca.alpaca_order()]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        get_orders = venue.trading.get_orders

        def fills_after_the_first_read(filter=None):
            answer = get_orders(filter)
            venue.trading.orders[0].update(status="filled", filled_qty="2", filled_avg_price="150")
            return answer

        venue.trading.get_orders = fills_after_the_first_read
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert [(r.venue_order_id.value, r.order_status) for r in reports] == [
        (alpaca.ORDER_AAPL["id"], OrderStatus.FILLED)
    ]


@pytest.mark.parametrize(
    ("listed", "kept"),
    [
        (["last", "last"], []),  # the first fill missing: one fill of two, listed twice
        (["last", "last", "first"], [2, 1]),  # all listed, one twice: each fill once
        (["last", "last-restated", "first"], []),  # one trade ID with two quantities
        (["last", "last-later", "first"], []),  # one trade ID at two times
    ],
    ids=[
        "repeat-hides-a-missing-fill",
        "repeat-of-a-full-list",
        "conflicting-repeat",
        "repeat-at-another-time",
    ],
)
def test_a_fill_activity_listed_twice_counts_once(venue, alpaca, listed, kept) -> None:
    # NautilusTrader's order takes "one quantity-bearing fill per `trade_id`", so a repeated activity is
    # one fill: it neither makes the fills look complete nor is reported twice.
    filled = alpaca.alpaca_order(**FILLED_TWO)
    at = datetime.now(UTC) - timedelta(days=1)
    rows = {
        "first": alpaca.fill_activity(
            filled,
            str(uuid.UUID(int=1)),
            1,
            "174.7",
            at - timedelta(seconds=1),
            "partial_fill",
            cum_qty=1,
            leaves_qty=1,
        ),
        "last": alpaca.fill_activity(
            filled, str(uuid.UUID(int=2)), 1, "174.8", at, "fill", cum_qty=2, leaves_qty=0
        ),
    }
    rows["last-restated"] = {**rows["last"], "qty": "2"}
    rows["last-later"] = {
        **rows["last"],
        "transaction_time": (at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    }
    venue.trading.orders = [filled]
    venue.trading.activities = [dict(rows[name]) for name in listed]
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert [r.trade_id.value for r in reports] == [str(uuid.UUID(int=n)) for n in kept]


def test_closed_order_skipped_for_its_client_id_is_logged_with_that_reason(
    venue, alpaca, capfd
) -> None:
    order = alpaca.alpaca_order(
        id="3e1f7c2a-5b9d-4a6e-8c0f-1d2b3c4e5f60", client_order_id="", status="canceled"
    )
    venue.trading.orders = [order]

    run(build_node(trader(submit=False)))

    log = logged(capfd)
    assert (
        f"Alpaca order {order['id']} (AAPL, canceled) is not reported: a client order ID "
        "NautilusTrader cannot hold (invalid string for 'value', was empty)"
    ) in log
    assert "its type, side, class, quantity or time in force" not in log


def test_open_only_report_request_reads_open_orders_with_no_window(venue, alpaca) -> None:
    # A check for open orders with a lookback (`open_check_lookback_mins`) must still see a working order
    # submitted before it: Alpaca's `after` filters on submission time.
    venue.trading.orders = [alpaca.alpaca_order()]  # open, submitted on 2023-12-12
    reports: list = []
    queries: list = []
    window = SimpleNamespace(
        open_only=True,
        instrument_id=None,
        start=dt_to_unix_nanos(datetime(2024, 1, 1, tzinfo=UTC)),
        end=None,
    )

    async def body(client) -> None:
        before = len(venue.trading.order_queries)
        reports.extend(await client._generate_order_status_reports(window))
        queries.extend(venue.trading.order_queries[before:])

    run_with_client(body)

    (open_now,) = queries
    assert (open_now.status, open_now.after, open_now.until) == (QueryOrderStatus.OPEN, None, None)
    assert [r.venue_order_id.value for r in reports] == [alpaca.ORDER_AAPL["id"]]


# -- A third reader's findings on the same changes (each failed before its fix) ------------------------

UNBUILDABLE = [
    ValueError("a body alpaca-py's Order model refuses"),
    TypeError("Order() argument after ** must be a mapping"),
]
UNBUILDABLE_IDS = ["value", "type"]


@pytest.mark.parametrize("error", UNBUILDABLE, ids=UNBUILDABLE_IDS)
def test_submit_refused_whose_lookup_cannot_be_built_stays_submitted(
    venue, alpaca, capfd, error
) -> None:
    # A 422 on the submit, then a lookup answered with a body alpaca-py cannot build: the outcome is
    # unknown, logged as such, as `_filled_quantities` takes the same two classes.
    venue.trading.script = "silent"
    venue.trading.submit_error = alpaca.api_error(422, 40010001, "client_order_id must be unique")
    venue.trading.lookup_error = error
    strategy = trader(order_type="limit")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.submitted:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted"]
    assert "refused (422), then the lookup failed" in logged(capfd)


@pytest.mark.parametrize("error", UNBUILDABLE, ids=UNBUILDABLE_IDS)
def test_cancel_refused_whose_lookup_cannot_be_built_leaves_the_order(
    venue, alpaca, capfd, error
) -> None:
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"  # the strategy does not stop the node; the test does

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        venue.trading.cancel_error = alpaca.api_error(422, 42210000, "order is not cancelable")
        venue.trading.order_lookup_error = lambda order_id: error
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderPendingCancel"]
    assert "Alpaca refused the cancel of" in logged(capfd)


@pytest.mark.parametrize("error", UNBUILDABLE, ids=UNBUILDABLE_IDS)
def test_modify_refused_whose_lookup_cannot_be_built_is_an_unknown_outcome(
    venue, alpaca, capfd, error
) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = alpaca.api_error(422, 42210000, "order is not replaceable")
    venue.trading.order_lookup_error = lambda order_id: (
        error
    )  # nothing else reads an order by ID here
    strategy = modifier()

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests))

    assert strategy.names()[-1] == "OrderPendingUpdate", strategy.names()
    assert "refused (422), then the lookup failed" in logged(capfd)


def test_rejected_replacement_after_replaced_whose_read_failed_ends_the_order(
    venue, alpaca
) -> None:
    # Alpaca sent `replaced` for the node's order, the replacement could not be read then, and the
    # replacement is then rejected: it had taken over, so its rejection ends the node's order, which is
    # not put back to its old terms by an `OrderModifyRejected` while nothing works at Alpaca.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    venue.trading.replace_error = requests.ReadTimeout("read timed out")
    venue.trading.took_before_error = True
    strategy = modifier()

    async def replaced_then_rejected(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.order_lookup_error = lambda order_id: (
            requests.ReadTimeout("read timed out") if order_id == new["id"] else None
        )
        venue.trading.confirm_replace(old["id"])
        await until_idle(client, venue)
        venue.trading.order_lookup_error = None
        venue.trading.update(new["id"], "rejected", status="rejected")

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), replaced_then_rejected
    )

    assert "OrderModifyRejected" not in strategy.names(), strategy.names()
    assert strategy.names()[-1] == "OrderRejected", strategy.names()


def test_rejected_replacement_of_a_replaced_order_is_reported(venue, alpaca) -> None:
    # Alpaca shows the node's order replaced and its replacement rejected: the replacement had taken
    # over, so it is the node's order's outcome, reported under its ID, unlike a replacement rejected
    # because the old order filled first, which leaves the old order's report.
    old = alpaca.alpaca_order(
        id="6e000000-0000-4000-8000-000000000001",
        client_order_id="node-6",
        status="replaced",
        replaced_by="6e000000-0000-4000-8000-000000000002",
    )
    new = alpaca.alpaca_order(
        id="6e000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-6",
        status="rejected",
        replaces=old["id"],
    )
    venue.trading.orders = [old, new]
    reports: list = []
    by_id = SimpleNamespace(venue_order_id=VenueOrderId(old["id"]), client_order_id=None)
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        reports.append(await client._generate_order_status_report(by_id))
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert [(r.client_order_id, r.venue_order_id.value, r.order_status) for r in reports] == [
        (ClientOrderId("node-6"), new["id"], OrderStatus.REJECTED)
    ] * 2


def test_a_cancelled_call_whose_worker_fails_later_leaves_no_unretrieved_error(venue) -> None:
    # A hook cancelled while its alpaca-py call runs (as the drain at disconnect cancels the update task):
    # the shielded call goes on, and its later error must not reach the loop as "never retrieved".
    import gc

    messages: list[str] = []

    async def body(client) -> None:
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: messages.append(str(context.get("message"))))
        release = threading.Event()

        def fails_later() -> None:
            release.wait(5)
            raise OSError("read timed out")

        try:
            waiting = asyncio.ensure_future(client._call(fails_later))
            await asyncio.sleep(0.05)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            release.set()
            async with asyncio.timeout(5):
                while client._rest_lock.locked():
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            gc.collect()
            await asyncio.sleep(0.05)
        finally:
            loop.set_exception_handler(handler)

    run_with_client(body)

    assert not [m for m in messages if "never retrieved" in m], messages


def test_a_client_given_at_construction_is_kept_across_a_reconnect(venue, monkeypatch) -> None:
    # A trading client given to the constructor is the caller's: a reconnect uses it again rather than
    # building one from the environment in its place.
    import functools

    def refuse(**kwargs: object) -> object:
        raise AssertionError("built from the environment in place of the given client")

    monkeypatch.setattr(canon_alpaca.execution, "TradingClient", refuse)
    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaExecutionClient",
        functools.partial(AlpacaExecutionClient, trading_client=venue.trading),
    )
    seen: list = []

    async def body(client) -> None:
        await client._disconnect()
        seen.append(client._trading)
        await client._connect()
        seen.append(client._trading)

    run_with_client(body)

    assert seen == [None, venue.trading]


def test_a_stream_given_at_construction_serves_one_connect(venue, monkeypatch) -> None:
    # alpaca-py's stream keeps its handlers and websocket between runs, and a run abandoned at a timed-out
    # disconnect may still use it: a second connect with a given stream fails, naming why.
    import functools

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaExecutionClient",
        functools.partial(AlpacaExecutionClient, stream=venue.stream),
    )

    async def body(client) -> None:
        await client._disconnect()
        with pytest.raises(RuntimeError, match="serves one connect"):
            await client._connect()

    run_with_client(body)


REPLACED_ID = "8a000000-0000-4000-8000-000000000001"
REPLACEMENT_ID = "8a000000-0000-4000-8000-000000000002"


def replaced_after_one_fill(
    alpaca, venue, replacement: dict[str, str], last_fill: bool = False
) -> None:
    """The node's buy of 2 at 150, filled 1 and then replaced; the replacement as `replacement` gives it,
    with Alpaca's activities for the fill before the replace and, with `last_fill`, one on the
    replacement."""
    at = datetime.now(UTC) - timedelta(minutes=5)
    old = alpaca.alpaca_order(
        id=REPLACED_ID,
        client_order_id="my-order-1",
        status="replaced",
        replaced_by=REPLACEMENT_ID,
        filled_qty="1",
        filled_avg_price="150",
    )
    new = alpaca.alpaca_order(
        id=REPLACEMENT_ID,
        client_order_id="alpaca-made-8",
        replaces=REPLACED_ID,
        **replacement,
    )
    venue.trading.orders = [old, new]
    venue.trading.activities = [
        alpaca.fill_activity(
            old, str(uuid.UUID(int=1)), 1, "150", at, "partial_fill", cum_qty=1, leaves_qty=1
        )
    ]
    if last_fill:
        venue.trading.activities.append(
            alpaca.fill_activity(
                new, str(uuid.UUID(int=2)), 1, "150", at + timedelta(minutes=1), cum_qty=2
            )
        )


@pytest.mark.parametrize(
    ("replacement", "last_fill", "status", "held"),
    [
        (
            {"status": "new", "filled_qty": "1", "filled_avg_price": "150"},
            False,
            OrderStatus.PARTIALLY_FILLED,
            "1",
        ),
        (
            {"status": "filled", "filled_qty": "2", "filled_avg_price": "150"},
            True,
            OrderStatus.FILLED,
            "2",
        ),
    ],
    ids=["open", "filled"],
)
def test_a_fill_before_a_replace_is_reported_on_the_node_order_at_startup(
    venue, alpaca, replacement, last_fill, status, held
) -> None:
    # Alpaca lists the fill before the replace under the replaced order, which is not reported (its
    # replacement is). The fill belongs to the node's order, now the replacement, as on the stream:
    # reported there, it is not taken for an outside order's.
    replaced_after_one_fill(alpaca, venue, replacement, last_fill)
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": held, "qty_available": held, "avg_entry_price": "150"}
    ]
    node = build_node(trader(submit=False))

    run(node)

    orders = node.cache.orders()
    assert [o.client_order_id.value for o in orders] == ["my-order-1"], orders
    (order,) = orders
    assert (order.venue_order_id.value, order.status) == (REPLACEMENT_ID, status)
    trades = [e.trade_id.value for e in order.events() if type(e).__name__ == "OrderFilled"]
    assert str(uuid.UUID(int=1)) in trades


def test_fills_before_a_replace_the_replacement_does_not_count_are_left_to_the_engine(
    venue, alpaca, capfd
) -> None:
    # Whether a replacement's `filled_qty` counts the fills before it is UNVERIFIED (ADR 0042): when it
    # does not, the fills listed across the chain do not add up to it, and they are not reported.
    replaced_after_one_fill(alpaca, venue, {"status": "new", "qty": "1", "filled_qty": "0"})
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert reports == []
    assert f"Alpaca lists fills of order {REPLACED_ID}, replaced by {REPLACEMENT_ID}" in logged(
        capfd
    )


def test_a_rejected_replacement_of_an_order_replaced_by_another_is_not_reported(
    venue, alpaca
) -> None:
    # A replacement Alpaca rejected while its old order stayed open, which a later modify then replaced
    # with another: only the working replacement is the node's order.
    old = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000001",
        client_order_id="node-9",
        status="replaced",
        replaced_by="9b000000-0000-4000-8000-000000000003",
    )
    rejected = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-9a",
        status="rejected",
        replaces=old["id"],
    )
    working = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000003",
        client_order_id="alpaca-made-9b",
        status="new",
        replaces=old["id"],
    )
    venue.trading.orders = [old, rejected, working]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert [(r.client_order_id.value, r.venue_order_id.value) for r in reports] == [
        ("node-9", working["id"])
    ]


def chain_fill_reports(venue, alpaca, replacement: dict[str, str], own: list[tuple[int, int]]):
    """The node's buy of 2, filled 1 and replaced; `own` lists the replacement's activities as (UUID
    number, `cum_qty`), one share each. Returns the fill reports' trade ID numbers, newest first."""
    replaced_after_one_fill(alpaca, venue, replacement)
    new = venue.trading.orders[1]
    at = datetime.now(UTC) - timedelta(minutes=4)
    for n, (number, cum) in enumerate(own):
        venue.trading.activities.append(
            alpaca.fill_activity(
                new,
                str(uuid.UUID(int=number)),
                1,
                "150",
                at + timedelta(seconds=n),
                cum_qty=cum,
                leaves_qty=int(new["qty"]) - cum,
            )
        )
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)
    return [uuid.UUID(r.trade_id.value).int for r in reports]


def test_a_chain_whose_live_order_has_an_unlisted_fill_is_left_to_the_engine(venue, alpaca) -> None:
    # The replacement shows 2 filled, counting only its own, and one of its two fills is not listed yet:
    # the chain's listed fills add up to 2 by coincidence. Its newest activity's `cum_qty` (1) shows a
    # fill after it, so nothing of the chain is reported.
    kept = chain_fill_reports(
        venue, alpaca, {"status": "filled", "filled_qty": "2", "filled_avg_price": "150"}, [(2, 1)]
    )

    assert kept == []


def test_a_replacement_that_counts_only_its_own_fills_keeps_them(venue, alpaca, capfd) -> None:
    # The replacement shows 1 filled, its newest activity's `cum_qty` 1, and lists that fill: it counts
    # only its own. Its fill is reported; the one before the replace cannot be shown on it and is left.
    kept = chain_fill_reports(
        venue,
        alpaca,
        {"status": "filled", "qty": "1", "filled_qty": "1", "filled_avg_price": "150"},
        [(2, 1)],
    )

    assert kept == [2]
    assert "it does not count the earlier fills" in logged(capfd)


def test_a_replaced_order_whose_replacement_cannot_be_read_warns(venue, alpaca, capfd) -> None:
    # Alpaca answers the replacement's read with raw data: the replaced order's fills are left to the
    # engine, and the log says why.
    replaced_after_one_fill(alpaca, venue, {"status": "new", "filled_qty": "1"})
    get_order_by_id = venue.trading.get_order_by_id
    venue.trading.get_order_by_id = lambda order_id: (
        {} if str(order_id) == REPLACEMENT_ID else get_order_by_id(order_id)
    )
    reports: list = []

    async def body(client: AlpacaExecutionClient) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert reports == []
    assert (
        f"Alpaca order {REPLACED_ID} was replaced and the order replacing it could not be read"
        in (logged(capfd))
    )


@pytest.mark.parametrize("error", UNBUILDABLE, ids=UNBUILDABLE_IDS)
def test_replaced_whose_replacement_cannot_be_built_is_left_to_reconciliation(
    venue, alpaca, capfd, error
) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    venue.trading.replace_error = requests.ReadTimeout("read timed out")
    venue.trading.took_before_error = True
    strategy = modifier()

    async def replaced(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.order_lookup_error = lambda order_id: error if order_id == new["id"] else None
        venue.trading.confirm_replace(old["id"])

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), replaced)

    assert "which could not be read" in logged(capfd)
    assert "OrderUpdated" not in strategy.names()


# -- the reviewer benchmark's triage and the second readers of #99 and #107 ------------------------

FAST_INFLIGHT = LiveExecutionEngineConfig(
    reconciliation_startup_delay_secs=0,
    inflight_check_interval_ms=100,
    inflight_check_threshold_ms=200,
)
"""The engine's in-flight check, on by default (from 10 s after start-up, every 2 s, for an order in
flight 5 s, 5 retries), made faster so a test sees it query and, with no answer, resolve locally within a
second or two."""


@pytest.fixture
def generated(monkeypatch) -> list[str]:
    """The name of every order event the client generates that answers a command or ends an order."""
    names: list[str] = []
    for method in (
        "generate_order_accepted",
        "generate_order_rejected",
        "generate_order_canceled",
        "generate_order_cancel_rejected",
        "generate_order_expired",
        "generate_order_updated",
        "generate_order_modify_rejected",
    ):
        original = getattr(AlpacaExecutionClient, method)

        def recording(self, *args, _method=method, _original=original, **kwargs):
            names.append(_method.removeprefix("generate_order_"))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(AlpacaExecutionClient, method, recording)
    return names


async def until_named(strategy: Trader | Modifier, *names: str, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not any(name in strategy.names() for name in names):
            await asyncio.sleep(0.01)


def test_a_submit_alpaca_took_with_no_answer_is_settled_by_the_engines_order_query(
    venue, alpaca
) -> None:
    # The in-flight check sends `QueryOrder` for an order left `SUBMITTED`; without an answer it resolves
    # the order locally as rejected, which on paper rejected an order Alpaca then accepted. The query
    # reads Alpaca's order and hands its report to the engine, as upstream's adapters answer it.
    venue.trading.script = "silent"
    venue.trading.took_before_error = True
    venue.trading.submit_error = alpaca.api_error(500, 50010000, "internal server error")
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted", "OrderRejected")
        await until_idle(client, venue)

    run_with_client(body, strategy, exec_engine=FAST_INFLIGHT)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted"], strategy.names()
    assert strategy.events[-1].venue_order_id.value == placed_order(venue)["id"]


def test_a_cancel_answered_204_for_an_order_alpaca_canceled_unseen_is_settled_by_the_order_query(
    venue,
) -> None:
    # On paper a second cancel of a canceled order was answered 204, not the documented 422 (TC-E44): an
    # order Alpaca canceled while the stream was down then gets no `canceled` update. The in-flight check's
    # query reads it canceled, and the engine cancels it from Alpaca's read, at Alpaca's time.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    canceled_at = (datetime.now(UTC) - timedelta(seconds=30)).replace(microsecond=0)

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        placed.update(
            status="canceled",
            canceled_at=canceled_at.isoformat(),
            updated_at=canceled_at.isoformat(),
        )
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        await until_named(strategy, "OrderCanceled")

    run_with_client(body, strategy, exec_engine=FAST_INFLIGHT)

    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    assert venue.trading.cancels == [placed_order(venue)["id"]]  # answered 204, no update
    assert strategy.events[-1].ts_event == dt_to_unix_nanos(canceled_at)  # from Alpaca's read


def test_an_order_alpaca_acknowledged_while_its_cancel_was_pending_is_accepted(
    venue, alpaca
) -> None:
    # Alpaca's `new` arrives while the node's order is `PENDING_CANCEL`, and the cancel is then refused:
    # the order must still get `OrderAccepted` (NautilusTrader's order takes it from `PENDING_CANCEL`,
    # "Allow failed cancel requests"), or it stays `SUBMITTED` for the in-flight check to reject.
    venue.trading.script = "accept"
    venue.trading.refuse_cancel = lambda raw: alpaca.api_error(
        422, 42210000, "order is not cancelable"
    )
    strategy = trader(order_type="limit", cancel_on="OrderSubmitted")
    strategy.stopped_for = "held open"
    statuses: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderCancelRejected")
        await until_idle(client, venue)
        assert strategy.client_order_id is not None
        statuses.append(client.cache.order(strategy.client_order_id).status)

    run_with_client(body, strategy)

    assert "OrderAccepted" in strategy.names(), strategy.names()
    assert statuses == [OrderStatus.ACCEPTED]


@pytest.mark.parametrize(
    ("held", "names"),
    [
        ("rejected", ["OrderSubmitted", "OrderRejected"]),
        ("canceled", ["OrderSubmitted", "OrderAccepted", "OrderCanceled"]),
    ],
)
def test_a_refused_submit_alpaca_holds_closed_ends_as_alpaca_holds_it(
    venue, alpaca, monkeypatch, held, names
) -> None:
    # A 403 answering alpaca-py's retry of a submit Alpaca took: the lookup finds the order, which Alpaca
    # may hold rejected or already closed. It ends as Alpaca holds it, not accepted and left open.
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

    venue.trading.script = "silent"
    venue.trading.took_before_error = True
    venue.trading.submit_error = alpaca.api_error(403, 40310000, "insufficient buying power")
    lookup = venue.trading.get_order_by_client_id

    def held_closed(client_id: str) -> AlpacaOrder:
        return lookup(client_id).model_copy(update={"status": AlpacaOrderStatus(held)})

    monkeypatch.setattr(venue.trading, "get_order_by_client_id", held_closed)
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, *names[-1:])
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert strategy.names() == names


def test_a_rejection_on_the_stream_and_in_the_submit_answer_is_one_rejection(
    venue, monkeypatch, generated
) -> None:
    # The stream's `rejected` is applied while the submit's answer is on its way, and the answer shows the
    # order rejected too: one `OrderRejected`, not a second for an order already closed.
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

    venue.trading.script = "reject"
    submit = venue.trading.submit_order

    def answered_rejected(order_data):
        answer = submit(order_data)
        time.sleep(0.3)  # the stream's update is applied first
        return answer.model_copy(update={"status": AlpacaOrderStatus.REJECTED})

    monkeypatch.setattr(venue.trading, "submit_order", answered_rejected)
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderRejected")
        await asyncio.sleep(0.5)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert generated.count("rejected") == 1, generated


def test_a_cancel_refused_while_the_order_filled_is_not_a_cancel_rejection(
    venue, alpaca, monkeypatch, generated
) -> None:
    # The cancel is refused 422 and, while the client reads the order, the stream's `fill` is applied:
    # the order has ended, so no `OrderCancelRejected` follows its fill.
    venue.trading.script = "accept"
    venue.trading.refuse_cancel = lambda raw: alpaca.api_error(
        422, 42210000, "order is not cancelable"
    )
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        lookup = venue.trading.get_order_by_id

        def filled_meanwhile(order_id: object) -> AlpacaOrder:
            if not client._finished:
                venue.trading.update(placed["id"], "fill", 2, status="filled", filled_qty="2")
                deadline = time.monotonic() + 2
                while not client._finished and time.monotonic() < deadline:
                    time.sleep(0.01)
            return lookup(order_id)

        monkeypatch.setattr(venue.trading, "get_order_by_id", filled_meanwhile)
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        await until_named(strategy, "OrderFilled")
        await asyncio.sleep(0.2)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert "cancel_rejected" not in generated, generated
    assert strategy.names()[-1] == "OrderFilled", strategy.names()


def test_a_fill_that_cannot_be_applied_does_not_end_the_order(venue) -> None:
    # A `fill` with no `execution_id` is logged and not applied; the order has not ended, so a later
    # update for it, here the fill sent again with its ID, is still applied.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        venue.trading.execution_ids = False
        venue.trading.update(placed["id"], "fill", 2, status="filled", filled_qty="2")
        await until_idle(client, venue)
        venue.trading.execution_ids = True
        venue.trading.update(placed["id"], "fill", 2, status="filled", filled_qty="2")
        await until_named(strategy, "OrderFilled", timeout=5)

    run_with_client(body, strategy)

    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]


MSFT_POSITION_ID = "64bbff51-59d6-4b3c-9351-13ad85e3c752"


def test_a_position_report_for_one_instrument_ignores_another_symbols_fractional_position(
    venue, alpaca
) -> None:
    # Alpaca allows fractional shares; a fractional MSFT position must not fail a report asked for AAPL,
    # nor start-up, which asks for every position: MSFT is not loaded here.
    msft = {
        **alpaca.POSITION_AAPL,
        "asset_id": MSFT_POSITION_ID,
        "symbol": "MSFT",
        "qty": "0.5",
        "qty_available": "0.5",
    }
    venue.trading.positions = [alpaca.POSITION_AAPL, msft]
    reports: list = []

    async def body(client) -> None:
        reports.extend(
            await client._generate_position_status_reports(SimpleNamespace(instrument_id=AAPL))
        )

    run_with_client(body)

    assert [(r.instrument_id, r.quantity) for r in reports] == [(AAPL, Quantity.from_int(2))]


def test_a_position_in_an_instrument_not_loaded_does_not_fail_startup(venue, alpaca) -> None:
    # As upstream's Coinbase adapter keeps its reports to the instruments it loaded: a whole-share MSFT
    # position, MSFT not loaded, is not reported, and start-up goes on with AAPL's.
    msft = {
        **alpaca.POSITION_AAPL,
        "asset_id": MSFT_POSITION_ID,
        "symbol": "MSFT",
        "qty": "3",
        "qty_available": "3",
    }
    venue.trading.positions = [alpaca.POSITION_AAPL, msft]
    node = build_node(trader(submit=False))

    run(node)

    assert [p.instrument_id for p in node.cache.positions_open()] == [AAPL]


@pytest.mark.parametrize(
    "change",
    [{"filled_avg_price": "n/a"}, {"qty": "two"}, {"filled_qty": "1e"}],
    ids=["price", "quantity", "filled"],
)
def test_a_closed_order_with_a_value_that_is_not_a_number_is_skipped(alpaca, change) -> None:
    from canon_alpaca.execution import order_status_report

    order = AlpacaOrder(
        **alpaca.alpaca_order(
            **{"status": "filled", "filled_qty": "2", "filled_avg_price": "150", **change}
        )
    )
    reasons: list[str] = []

    assert order_status_report(order, 0, skipped=reasons.append) is None
    assert reasons and "not a number" in reasons[0], reasons


def test_a_late_fill_on_the_replaced_order_after_the_replacement_filled_is_applied(venue) -> None:
    # The node's buy of 2 fills one share on its old order and is replaced; the replacement fills the
    # other and its `fill` arrives before the old order's delayed `partial_fill`. The node's order has one
    # share until the second arrives, so it has not ended and the late fill is applied: two shares, as
    # Alpaca holds. This rests on the node's own count of its fills, whichever way Alpaca counts a
    # replacement's `filled_qty` (UNVERIFIED, ADR 0042).
    venue.trading.script = "accept"
    strategy = modifier()

    async def fills(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.update(new["id"], "fill", 1, status="filled", filled_qty="2")
        await until_idle(client, venue)
        venue.trading.update(old["id"], "partial_fill", 1, filled_qty="1")
        async with asyncio.timeout(5):
            while strategy.names().count("OrderFilled") < 2:
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), fills)

    assert strategy.names().count("OrderFilled") == 2, strategy.names()
    assert (
        sum(e.last_qty.as_double() for e in strategy.events if type(e).__name__ == "OrderFilled")
        == 2
    )


def test_a_replacement_rejected_before_its_replaced_arrives_ends_the_order(venue) -> None:
    # Alpaca replaced the node's order and then rejected the replacement, and the stream delivers the
    # rejection before `replaced`. Alpaca shows the old order replaced by it, so it had taken over: the
    # node's order is rejected, not modify-rejected and then moved to the rejected replacement's terms.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def rejected_first(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.update(new["id"], "rejected", status="rejected")
        await until_idle(client, venue)
        venue.trading.confirm_replace(old["id"])

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), rejected_first)

    assert "OrderModifyRejected" not in strategy.names(), strategy.names()
    assert strategy.names()[-1] == "OrderRejected", strategy.names()


def test_a_replacement_replaced_again_before_it_was_read_is_adopted_at_its_end(venue) -> None:
    # `replaced` for the node's order could not read the replacement; the replacement is then replaced in
    # turn, and its own `replaced` is its first update. The node's order moves to the order now working,
    # rather than failing to adopt a replaced one.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def replaced_twice(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        venue.trading.order_lookup_error = lambda order_id: (
            requests.ReadTimeout("read timed out") if order_id == new["id"] else None
        )
        venue.trading.confirm_replace(old["id"])
        await until_idle(client, venue)
        venue.trading.order_lookup_error = None
        venue.trading.replace_script = "replaced"
        venue.trading.replace_order_by_id(new["id"], ReplaceOrderRequest(limit_price=150.75))
        async with asyncio.timeout(5):
            while "OrderUpdated" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), replaced_twice)

    updated = strategy.last("OrderUpdated")
    assert updated.venue_order_id.value == placed_order(venue, 2)["id"]
    assert updated.price == Price.from_str("150.7500")


def test_a_rejected_replacement_the_node_adopted_is_reported_when_its_prior_cannot_be_read(
    venue,
) -> None:
    # The node moved its order to the replacement, which Alpaca then rejected; the order it replaced
    # cannot be read. The node holds the replacement, so it took over: its report is the rejection.
    venue.trading.script = "accept"
    strategy = modifier()
    reports: list = []

    async def query(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        new.update(status="rejected")  # no update reaches the node
        venue.trading.order_lookup_error = lambda order_id: (
            requests.ReadTimeout("read timed out") if order_id == old["id"] else None
        )
        by_id = SimpleNamespace(venue_order_id=VenueOrderId(new["id"]), client_order_id=None)
        reports.append(await client._generate_order_status_report(by_id))

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), query)

    (report,) = reports
    assert report is not None
    assert (report.client_order_id, report.order_status) == (
        strategy.client_order_id,
        OrderStatus.REJECTED,
    )


def test_an_order_replaced_between_the_two_order_reads_is_reported_once(
    venue, alpaca, monkeypatch
) -> None:
    # The open read sees the node's order working; Alpaca then replaces it, and the windowed history holds
    # the replacement but not the order it replaced. One report, the replacement's, under the node's ID.
    old = alpaca.alpaca_order(id=REPLACED_ID, client_order_id="my-order-3")
    new = alpaca.alpaca_order(
        id=REPLACEMENT_ID, client_order_id="alpaca-made-3", replaces=REPLACED_ID, limit_price="151"
    )
    venue.trading.orders = [{**old, "status": "replaced", "replaced_by": REPLACEMENT_ID}, new]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        answers = iter([[AlpacaOrder(**old)], [AlpacaOrder(**new)]])
        monkeypatch.setattr(venue.trading, "get_orders", lambda filter=None: next(answers))
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert [(r.client_order_id.value, r.venue_order_id.value) for r in reports] == [
        ("my-order-3", REPLACEMENT_ID)
    ]


def test_fill_reports_for_one_order_of_a_replace_chain_are_the_chains(venue, alpaca) -> None:
    # The bulk request reports the fill before the replace on the order that replaced it; a request for
    # that order alone must report the same fills, not none because the chain was cut before assembly.
    replaced_after_one_fill(
        alpaca,
        venue,
        {"status": "filled", "filled_qty": "2", "filled_avg_price": "150"},
        last_fill=True,
    )
    bulk: list = []
    single: list = []

    async def body(client) -> None:
        bulk.extend(await client._fill_reports(fills_command()))
        single.extend(
            await client._fill_reports(fills_command(venue_order_id=VenueOrderId(REPLACEMENT_ID)))
        )

    run_with_client(body)

    def terms(reports):
        return sorted((r.venue_order_id.value, r.trade_id.value) for r in reports)

    assert len(bulk) == 2
    assert terms(single) == terms(bulk)


def test_a_call_left_running_by_a_cancelled_hook_holds_the_given_client_across_a_reconnect(
    venue, monkeypatch
) -> None:
    # A given trading client is used again by the next connect; a call a cancelled hook left running on
    # it keeps it until the call returns, so no two calls share its `requests.Session` at once.
    import functools

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaExecutionClient",
        functools.partial(AlpacaExecutionClient, trading_client=venue.trading),
    )
    running = 0
    overlapped: list[bool] = []
    release = threading.Event()
    guard = threading.Lock()

    def tracked(wait: bool) -> None:
        nonlocal running
        with guard:
            running += 1
            overlapped.append(running > 1)
        try:
            if wait:
                release.wait(5)
        finally:
            with guard:
                running -= 1

    async def body(client) -> None:
        waiting = asyncio.ensure_future(client._call(tracked, True))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await client._disconnect()
        await client._connect()
        second = asyncio.ensure_future(client._call(tracked, False))
        await asyncio.sleep(0.1)
        release.set()
        await second

    run_with_client(body)

    assert overlapped == [False, False]


def test_a_given_stream_a_failed_connect_never_ran_serves_the_next_connect(
    venue, alpaca, monkeypatch
) -> None:
    # A connect that fails before its stream starts (here the account read) has not used the given
    # stream, so the next connect runs it rather than refusing it as used.
    import functools

    monkeypatch.setattr(
        canon_alpaca.factories,
        "AlpacaExecutionClient",
        functools.partial(AlpacaExecutionClient, stream=venue.stream),
    )
    account = venue.trading.get_account

    async def body(client) -> None:
        await client._disconnect()
        client._given_stream_used = False  # as if this client had never connected
        venue.stream.stopped.clear()

        def fails() -> object:
            raise OSError("connection reset")

        monkeypatch.setattr(venue.trading, "get_account", fails)
        with pytest.raises(OSError):
            await client._connect()
        monkeypatch.setattr(venue.trading, "get_account", account)
        await client._connect()
        assert client.is_connected or client._stream_thread is not None

    run_with_client(body)


def test_the_orders_stand_in_lists_the_orders_placed_through_it(venue) -> None:
    # As Alpaca lists an account's orders: one submitted through the stand-in is in its open orders.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    reports: list = []
    window = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body, strategy)

    assert [r.venue_order_id.value for r in reports] == [placed_order(venue)["id"]]


def test_a_held_stop_is_reported_with_the_trigger_type_the_node_sent(venue) -> None:
    # Alpaca's order has no trigger type: a stop the node holds is reported with its own (`LAST_PRICE`
    # here), and only one the node does not hold with `DEFAULT`.
    venue.trading.script = "accept"
    strategy = trader(order_type="stop", trigger_type=TriggerType.LAST_PRICE)
    strategy.stopped_for = "held open"
    reports: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        by_id = SimpleNamespace(
            venue_order_id=VenueOrderId(placed_order(venue)["id"]), client_order_id=None
        )
        reports.append(await client._generate_order_status_report(by_id))

    run_with_client(body, strategy)

    (report,) = reports
    assert (report.order_type, report.trigger_type) == (
        OrderType.STOP_MARKET,
        TriggerType.LAST_PRICE,
    )


def test_a_trade_update_alpaca_py_hands_over_raw_is_logged(venue, capfd) -> None:
    async def body(client) -> None:
        await client._on_trade_update({"event": "new"})
        await until_idle(client, venue)

    run_with_client(body)

    assert "Alpaca trade update not parsed" in logged(capfd)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    # A trigger or limit price of 0 is refused by NautilusTrader's risk engine first
    # (`PRICE_NOT_POSITIVE`, observed); a quantity of 0 reaches the client.
    [("quantity", "0", "quantity 0 is not positive")],
)
def test_a_modify_to_a_value_that_is_not_positive_is_rejected_before_alpaca(
    venue, field, value, reason
) -> None:
    # alpaca-py's `ReplaceOrderRequest` raises for these before anything is sent, which left the modify
    # with no outcome.
    venue.trading.script = "accept"
    strategy = modifier(
        order_type="stop" if field == "trigger_price" else "limit", field=field, prices=[value]
    )

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert reason in strategy.last("OrderModifyRejected").reason
    assert venue.trading.replace_requests == []


def test_a_report_whose_replacement_alpaca_does_not_find_is_none(venue, alpaca) -> None:
    # A 404 on the replacement is answered as a 404 on the order asked for: no report, not an error.
    old = alpaca.alpaca_order(
        id=REPLACED_ID, client_order_id="my-order-9", status="replaced", replaced_by=REPLACEMENT_ID
    )
    venue.trading.orders = [old]
    reports: list = []
    by_id = SimpleNamespace(venue_order_id=VenueOrderId(REPLACED_ID), client_order_id=None)

    async def body(client) -> None:
        reports.append(await client._order_status_report(by_id))

    run_with_client(body)

    assert reports == [None]


# -- the code review of the change above --------------------------------------------------------


def test_replaced_whose_replacement_reads_canceled_with_a_fill_still_applies_the_fill(
    venue,
) -> None:
    # `replaced` reads the replacement canceled with a share filled that the node has not seen; its
    # `partial_fill` is still queued, so the order is not ended from the read and the fill is applied.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    venue.trading.replace_error = requests.ReadTimeout("read timed out")
    venue.trading.took_before_error = True  # no answer kept: `replaced` reads the replacement
    strategy = modifier()

    async def canceled_after_a_fill(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        new.update(status="canceled", filled_qty="1", canceled_at=datetime.now(UTC).isoformat())
        venue.trading.confirm_replace(old["id"])
        venue.trading.update(new["id"], "partial_fill", 1, status="canceled", filled_qty="1")
        venue.trading.update(new["id"], "canceled", status="canceled")
        async with asyncio.timeout(5):
            while "OrderCanceled" not in strategy.names():
                await asyncio.sleep(0.01)

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), canceled_after_a_fill)

    names = strategy.names()
    assert "OrderFilled" in names and names.index("OrderFilled") < names.index("OrderCanceled"), (
        names
    )


def test_a_fill_for_an_order_the_engine_closed_from_a_query_report_is_not_applied_again(
    venue, alpaca
) -> None:
    # Alpaca filled a share and canceled the order while the stream was down; the cancel is answered
    # 204, the in-flight check's query reads it canceled with the share filled, and the engine infers
    # that fill. The stream's late `partial_fill` is then dropped, not booked a second time.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        placed.update(  # no update sent
            status="canceled",
            filled_qty="1",
            filled_avg_price="150",
            canceled_at=datetime.now(UTC).isoformat(),
        )
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)  # answered 204, as paper answered it
        await until_named(strategy, "OrderCanceled")
        await until_idle(client, venue)
        venue.trading.update(placed["id"], "partial_fill", 1, status="canceled", filled_qty="1")
        await asyncio.sleep(0.3)
        await until_idle(client, venue)

    run_with_client(body, strategy, exec_engine=FAST_INFLIGHT)

    filled = [e for e in strategy.events if type(e).__name__ == "OrderFilled"]
    assert sum(e.last_qty.as_double() for e in filled) == 1, strategy.names()


def test_an_acknowledgement_kept_during_a_cancel_is_not_generated_after_a_fill(
    venue, generated
) -> None:
    # A fill after the kept acknowledgement has moved the order on: the refusal of the cancel then
    # generates no `OrderAccepted` for it.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        order = client.cache.order(strategy.client_order_id)
        client._accepted.discard(order.client_order_id)
        client._acknowledged[order.client_order_id] = (order.venue_order_id, 1)
        client._record(order).filled = Decimal(1)
        generated.clear()
        client._refusal_settled(order)
        seen.extend(generated)

    seen: list[str] = []
    run_with_client(body, strategy)

    assert seen == [], seen


def test_a_replacement_not_yet_working_is_not_reported_over_the_order_pending_replace(
    venue, alpaca, monkeypatch
) -> None:
    # Alpaca shows the node's order `pending_replace` and its replacement `pending_new`: the replace has
    # not taken effect, so the order is reported at its own terms and the replacement is not.
    old = alpaca.alpaca_order(
        id=REPLACED_ID, client_order_id="my-order-4", status="pending_replace"
    )
    new = alpaca.alpaca_order(
        id=REPLACEMENT_ID,
        client_order_id="alpaca-made-4",
        replaces=REPLACED_ID,
        status="pending_new",
        limit_price="151",
    )
    venue.trading.orders = [old, new]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert [(r.client_order_id.value, r.venue_order_id.value) for r in reports] == [
        ("my-order-4", REPLACED_ID)
    ]


def test_a_kept_acknowledgement_gives_the_next_command_alpacas_order_id(venue) -> None:
    # A cancel or modify while an acknowledgement is kept uses Alpaca's order ID from it.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    found: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        order = client.cache.order(strategy.client_order_id)
        kept = VenueOrderId("ab000000-0000-4000-8000-000000000001")
        client._acknowledged[order.client_order_id] = (kept, 1)
        found.append(client._known_venue_id(order.client_order_id, None, None))

    run_with_client(body, strategy)

    assert found == [VenueOrderId("ab000000-0000-4000-8000-000000000001")]


# -- one record per node order: the replace chain as one model (ADR 0042) ---------------------------


async def reconnect(client: AlpacaExecutionClient, venue: SimpleNamespace) -> None:
    """Disconnect the client and connect it again, as a dropped session does: what it kept of each
    order's replaces is gone, and the node's cache still holds the orders."""
    await client._disconnect()
    venue.stream.stopped.clear()
    await client._connect()


async def settled(client: AlpacaExecutionClient, venue: SimpleNamespace) -> None:
    """Every update applied, and the engine given time to apply what they generated."""
    await until_idle(client, venue)
    await asyncio.sleep(0.2)


@pytest.mark.parametrize("first", ["replaced", "rejected"])
def test_a_rejected_takeover_of_a_partly_filled_order_ends_canceled(venue, first) -> None:
    # One of two shares fills, the order is replaced, and Alpaca rejects the replacement after it took
    # over: nothing works at Alpaca. NautilusTrader drops `OrderRejected` for a partly filled order, which
    # left it open in the cache; the order ends canceled with its fill, as upstream's Binance spot emits
    # the old order's cancel when its cancel-replace is rejected (`reject_replace`).
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    held: list = []

    async def partly_filled_then_rejected(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old["status"] = "new"
        venue.trading.update(
            old["id"], "partial_fill", 1, status="partially_filled", filled_qty="1"
        )
        await until_named(strategy, "OrderFilled")
        await until_idle(client, venue)
        if first == "replaced":
            venue.trading.confirm_replace(old["id"])
            await until_named(strategy, "OrderUpdated")
            venue.trading.update(new["id"], "rejected", status="rejected")
        else:
            old["status"] = "replaced"  # as Alpaca shows it; its `replaced` is delivered second
            venue.trading.update(new["id"], "rejected", status="rejected")
            await until_idle(client, venue)
            venue.trading.confirm_replace(old["id"])
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), partly_filled_then_rejected
    )

    assert "OrderRejected" not in strategy.names(), strategy.names()
    assert strategy.names()[-1] == "OrderCanceled", strategy.names()
    assert held == [(OrderStatus.CANCELED, 1.0)]


def test_a_rejected_takeover_of_a_partly_filled_order_is_reported_canceled_with_its_fill(
    venue, alpaca
) -> None:
    # The same state read at start-up: reported `REJECTED`, the engine dropped the order's fill report
    # and held it rejected with nothing filled. It is reported canceled, its fill on it.
    replaced_after_one_fill(
        alpaca, venue, {"status": "rejected", "filled_qty": "1", "filled_avg_price": "150"}
    )
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    node = build_node(trader(submit=False))

    run(node)

    orders = node.cache.orders()
    assert [o.client_order_id.value for o in orders] == ["my-order-1"], orders
    (order,) = orders
    assert (order.venue_order_id.value, order.status, order.filled_qty.as_double()) == (
        REPLACEMENT_ID,
        OrderStatus.CANCELED,
        1.0,
    )
    trades = [e.trade_id.value for e in order.events() if type(e).__name__ == "OrderFilled"]
    assert trades == [str(uuid.UUID(int=1))]


def test_a_late_rejection_of_an_earlier_replacement_leaves_the_working_one(venue) -> None:
    # Modify 1's replacement is rejected while the old order stays open, and its `rejected` is delayed;
    # modify 2 replaces the old order with a working replacement. The late `rejected` names a replacement
    # the old order was not replaced by: it must not move the node's order to it and reject it.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    held: list = []

    async def second_modify_then_late_rejection(client) -> None:
        old, failed = placed_order(venue, 0), placed_order(venue, 1)
        old.update(
            status="new", replaced_by=None
        )  # Alpaca rejected the replacement; the old one works
        failed["status"] = "rejected"
        venue.trading.replace_script = "replaced"
        cached = client.cache.order(strategy.client_order_id)
        await client._modify_order(
            SimpleNamespace(
                client_order_id=strategy.client_order_id,
                venue_order_id=cached.venue_order_id,
                quantity=None,
                price=Price.from_str("151.00"),
                trigger_price=None,
            )
        )
        await until_named(strategy, "OrderUpdated")
        venue.trading.update(failed["id"], "rejected", status="rejected")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.venue_order_id.value, cached.is_open))

    run_modify(
        venue,
        strategy,
        lambda: bool(venue.trading.replace_requests),
        second_modify_then_late_rejection,
    )

    working = placed_order(venue, 2)
    assert "OrderRejected" not in strategy.names(), strategy.names()
    assert strategy.names().count("OrderUpdated") == 1, strategy.names()
    assert held == [(working["id"], True)]


CHAIN_IDS = [f"7c000000-0000-4000-8000-00000000000{n}" for n in range(1, 4)]


def replaced_twice_with_a_fill_on_each(alpaca, venue) -> None:
    """The node's buy of 3, one share filled on the order and on each of its two replacements, as
    Alpaca lists them while a replacement counts the fills before it: `cum_qty` 1, 2 and 3."""
    first, second, third = CHAIN_IDS
    at = datetime.now(UTC) - timedelta(minutes=10)
    common = {"qty": "3", "filled_avg_price": "174.6"}
    orders = [
        alpaca.alpaca_order(
            **common,
            id=first,
            client_order_id="my-order-7",
            status="replaced",
            replaced_by=second,
            filled_qty="1",
        ),
        alpaca.alpaca_order(
            **common,
            id=second,
            client_order_id="alpaca-made-7a",
            replaces=first,
            status="replaced",
            replaced_by=third,
            filled_qty="2",
        ),
        alpaca.alpaca_order(
            **common,
            id=third,
            client_order_id="alpaca-made-7b",
            replaces=second,
            status="filled",
            filled_qty="3",
        ),
    ]
    venue.trading.orders = orders
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=n + 1)),
            1,
            "174.6",
            at + timedelta(minutes=n),
            "fill" if n == 2 else "partial_fill",
            cum_qty=n + 1,
            leaves_qty=2 - n,
        )
        for n, order in enumerate(orders)
    ]


@pytest.mark.parametrize("asked", [None, 0, 1, 2], ids=["bulk", "first", "middle", "last"])
def test_fills_across_two_replaces_are_reported_on_the_order_now_working(
    venue, alpaca, asked
) -> None:
    # Three orders in a chain, a fill on each. The middle one's listed fill is short of its `cum_qty`
    # (which counts the first's), so it was set aside before the chain was assembled and the whole
    # chain's fills were dropped. Every fill is reported on the last order, whichever order is asked for,
    # and the order's status report agrees.
    replaced_twice_with_a_fill_on_each(alpaca, venue)
    fills: list = []
    statuses: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
    by = {} if asked is None else {"venue_order_id": VenueOrderId(CHAIN_IDS[asked])}

    async def body(client) -> None:
        fills.extend(await client._fill_reports(fills_command(**by)))
        statuses.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert sorted((r.venue_order_id.value, uuid.UUID(r.trade_id.value).int) for r in fills) == [
        (CHAIN_IDS[2], 1),
        (CHAIN_IDS[2], 2),
        (CHAIN_IDS[2], 3),
    ]
    assert [
        (r.client_order_id.value, r.venue_order_id.value, r.order_status, r.filled_qty.as_double())
        for r in statuses
    ] == [("my-order-7", CHAIN_IDS[2], OrderStatus.FILLED, 3.0)]


def test_a_fill_on_the_replacement_after_a_reconnect_needs_no_read_of_the_order_before_it(
    venue,
) -> None:
    # After a reconnect the client has kept nothing of the replace, but the node's cache holds the order
    # under the replacement's ID: the fill is the node's order's without walking `replaces` over HTTP,
    # so a read that times out cannot lose it.
    venue.trading.script = "accept"
    strategy = modifier()

    async def fill_after_reconnect(client) -> None:
        await reconnect(client, venue)
        venue.trading.order_lookup_error = lambda order_id: requests.ReadTimeout("read timed out")
        new = placed_order(venue, 1)
        venue.trading.update(new["id"], "fill", 2, status="filled", filled_qty="2")
        await settled(client, venue)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), fill_after_reconnect)

    assert strategy.names()[-1] == "OrderFilled", strategy.names()
    assert strategy.last("OrderFilled").venue_order_id.value == placed_order(venue, 1)["id"]


def test_a_late_cancel_of_the_replaced_order_after_a_reconnect_is_ignored(venue) -> None:
    # The same late `canceled` as before a reconnect: the node's order names the replacement, so an
    # update of the order it left does not cancel it.
    venue.trading.script = "accept"
    strategy = modifier()
    held: list = []

    async def late_cancel(client) -> None:
        await reconnect(client, venue)
        old = placed_order(venue, 0)
        venue.trading.update(old["id"], "canceled", status="canceled")
        await settled(client, venue)
        held.append(client.cache.order(strategy.client_order_id).is_open)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), late_cancel)

    assert "OrderCanceled" not in strategy.names(), strategy.names()
    assert held == [True]


def test_a_rejected_replacement_of_an_outside_order_with_no_client_id_is_not_reported(
    venue, alpaca
) -> None:
    # An order made outside the node with an empty client order ID fills, and its replacement, with an
    # ID Alpaca made, is rejected without taking over. The replacement's own ID is where its trace ends,
    # which was taken for "not a replacement": it was reported rejected, as an order of its own.
    outside = alpaca.alpaca_order(
        id="5f000000-0000-4000-8000-000000000001",
        client_order_id="",
        status="filled",
        filled_qty="2",
        filled_avg_price="150",
    )
    rejected = alpaca.alpaca_order(
        id="5f000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-501",
        status="rejected",
        replaces=outside["id"],
    )
    venue.trading.orders = [outside, rejected]
    reports: list = []
    window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)

    async def body(client) -> None:
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    assert reports == []


def long_chain(alpaca, links: int) -> list[dict]:
    """An order "node-long" replaced `links` times, the last replacement working."""
    ids = [str(uuid.UUID(int=0xA000 + n)) for n in range(links + 1)]
    return [
        alpaca.alpaca_order(
            id=ids[n],
            client_order_id="node-long" if n == 0 else f"alpaca-made-{n}",
            replaces=ids[n - 1] if n else None,
            replaced_by=ids[n + 1] if n < links else None,
            status="replaced" if n < links else "new",
        )
        for n in range(links + 1)
    ]


def test_a_chain_longer_than_the_lookup_limit_is_traced_through_the_orders_already_read(
    venue, alpaca
) -> None:
    # Seventeen replaces, every order of the chain in the orders start-up reads: the working order is
    # the first order's, "node-long". The limit is on lookups, and none is needed; counted in links, it
    # named the order sixteen links back, one with an ID Alpaca made.
    from canon_alpaca.execution import MAX_REPLACE_CHAIN

    venue.trading.orders = long_chain(alpaca, MAX_REPLACE_CHAIN + 1)
    node = build_node(trader(submit=False))

    run(node)

    (working,) = node.cache.orders_open()
    assert working.client_order_id == ClientOrderId("node-long")


def test_a_chain_that_needs_more_lookups_than_the_limit_is_not_traced_to_another_order(
    venue, alpaca
) -> None:
    # The same chain when only its working order is among the orders read: tracing it takes seventeen
    # lookups, one past the limit. It is an open order whose chain cannot be read, which fails the
    # report by name, not an order reported under the ID sixteen links back.
    from canon_alpaca.execution import MAX_REPLACE_CHAIN

    chain = long_chain(alpaca, MAX_REPLACE_CHAIN + 1)
    venue.trading.orders = chain
    open_only = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)
    failures: list[str] = []

    async def body(client) -> None:
        with pytest.raises(ValueError) as failure:
            await client._generate_order_status_reports(open_only)
        failures.append(str(failure.value))

    venue.trading.orders = []  # start-up reads nothing
    strategy = trader(idle=True)

    async def with_the_chain(client) -> None:
        venue.trading.orders = chain
        await body(client)

    run_with_client(with_the_chain, strategy)

    (failure,) = failures
    assert f"Alpaca order {chain[-1]['id']} (AAPL) is open and tracing it failed" in failure


def test_an_order_the_node_holds_is_its_own_however_often_it_was_replaced(venue) -> None:
    # Seventeen modifies through the node, then a reconnect: the node's cache names the working order,
    # so its fill, its report among the open orders and its report by client order ID are the node's
    # order's with no walk at all. Walked, the chain ended sixteen links back, at an ID the node does not
    # hold: the fill was dropped and the reports named another order or none.
    from canon_alpaca.execution import MAX_REPLACE_CHAIN

    replaces = MAX_REPLACE_CHAIN + 1
    venue.trading.script = "accept"
    strategy = modifier(prices=[f"{150 + n / 100:.2f}" for n in range(1, replaces + 1)])
    reports: list = []
    open_only = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)

    async def after_a_reconnect(client) -> None:
        await reconnect(client, venue)
        reports.extend(await client._generate_order_status_reports(open_only))
        by_id = SimpleNamespace(venue_order_id=None, client_order_id=strategy.client_order_id)
        reports.append(await client._generate_order_status_report(by_id))
        venue.trading.update(
            placed_order(venue, replaces)["id"], "fill", 2, status="filled", filled_qty="2"
        )
        await settled(client, venue)

    run_modify(
        venue,
        strategy,
        lambda: strategy.names().count("OrderUpdated") == replaces,
        after_a_reconnect,
    )

    working = placed_order(venue, replaces)["id"]
    assert [(r.client_order_id, r.venue_order_id.value) for r in reports if r is not None] == [
        (strategy.client_order_id, working)
    ] * 2
    assert strategy.names()[-1] == "OrderFilled", strategy.names()


def test_a_replace_alpaca_answers_and_then_rejects_on_the_stream_is_modify_rejected(venue) -> None:
    # The PATCH is answered with a replacement and Alpaca then sends `order_replace_rejected` for the
    # old order: only the stream says the modify failed. The modify is rejected once, the old order still
    # works, and its fill is applied under its own ID. (The stand-in's "reject" script also refuses the
    # PATCH, so a test on it passes with the stream's rejection ignored.)
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def rejected_on_the_stream(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        del venue.trading._placed[new["id"]]  # the replacement never worked
        old.update(status="new", replaced_by=None)
        venue.trading.update(old["id"], "order_replace_rejected")
        await until_named(strategy, "OrderModifyRejected", timeout=5)
        venue.trading.update(old["id"], "fill", 2, status="filled", filled_qty="2")
        await until_named(strategy, "OrderFilled", timeout=5)
        await settled(client, venue)

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), rejected_on_the_stream
    )

    assert strategy.names()[-3:] == ["OrderPendingUpdate", "OrderModifyRejected", "OrderFilled"]
    assert (
        strategy.last("OrderModifyRejected").reason == "Alpaca rejected the replace (trade update)"
    )
    assert strategy.last("OrderFilled").venue_order_id.value == placed_order(venue, 0)["id"]
    assert "OrderUpdated" not in strategy.names()


READINGS = ["chain", "own"]
"""Both readings of a replacement's `qty` and `filled_qty` (`FilledReading`, UNVERIFIED, ADR 0042)."""


def replacement_as_read(reading: str, filled: int) -> dict[str, str]:
    """The replacement of a buy of 2 filled 1 before the replace, with `filled` shares of its own, as
    Alpaca would show it under `reading`: carrying the order on, or an order for what is left."""
    if reading == "chain":
        return {"qty": "2", "filled_qty": str(1 + filled)}
    return {"qty": "1", "filled_qty": str(filled)}


@pytest.mark.parametrize("reading", READINGS)
def test_a_partly_filled_order_replaced_and_filled_reconciles_whole_under_either_reading(
    venue, alpaca, monkeypatch, reading
) -> None:
    # The seam: with Alpaca's data shaped as each reading says, start-up reconciles the node's order
    # filled 2 of 2 on the replacement, both trade IDs on it, whichever reading the client is set to.
    monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading(reading))
    shown = replacement_as_read(reading, 1)
    replaced_after_one_fill(alpaca, venue, {"status": "filled", "filled_avg_price": "150", **shown})
    new = venue.trading.orders[1]
    venue.trading.activities.append(
        alpaca.fill_activity(
            new,
            str(uuid.UUID(int=2)),
            1,
            "150",
            datetime.now(UTC) - timedelta(minutes=4),
            cum_qty=int(shown["filled_qty"]),
            leaves_qty=0,
        )
    )
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "2", "qty_available": "2", "avg_entry_price": "150"}
    ]
    node = build_node(trader(submit=False))

    run(node)

    (order,) = node.cache.orders()
    assert (
        order.venue_order_id.value,
        order.status,
        order.quantity.as_double(),
        order.filled_qty.as_double(),
    ) == (REPLACEMENT_ID, OrderStatus.FILLED, 2.0, 2.0)
    trades = sorted(
        uuid.UUID(e.trade_id.value).int for e in order.events() if type(e).__name__ == "OrderFilled"
    )
    assert trades == [1, 2]


@pytest.mark.parametrize("reading", READINGS)
def test_a_partly_filled_order_replaced_on_the_stream_keeps_its_quantity_under_either_reading(
    venue, monkeypatch, reading
) -> None:
    # The same chain on the stream: one share fills, the order is replaced, the replacement fills the
    # other. `OrderUpdated` keeps the order's quantity of 2 and the order ends filled with 2, under each
    # reading with Alpaca's data shaped as it says.
    monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading(reading))
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    held: list = []

    async def partly_filled_replaced_filled(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old["status"] = "new"
        venue.trading.update(
            old["id"], "partial_fill", 1, status="partially_filled", filled_qty="1"
        )
        await until_named(strategy, "OrderFilled")
        await until_idle(client, venue)
        new.update(replacement_as_read(reading, 0))
        client._records[strategy.client_order_id].answers.clear()  # read from Alpaca, as shaped
        venue.trading.confirm_replace(old["id"])
        await until_named(strategy, "OrderUpdated")
        venue.trading.update(
            new["id"],
            "fill",
            1,
            status="filled",
            filled_qty=replacement_as_read(reading, 1)["filled_qty"],
        )
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.quantity.as_double(), cached.filled_qty.as_double()))

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), partly_filled_replaced_filled
    )

    assert strategy.last("OrderUpdated").quantity == Quantity.from_int(2)
    assert held == [(OrderStatus.FILLED, 2.0, 2.0)]


@pytest.mark.parametrize("reconnected", [False, True], ids=["same-connect", "after-reconnect"])
def test_a_fill_delivered_twice_is_applied_once(venue, reconnected) -> None:
    # One trade ID delivered twice counted two shares on the client's own count: a buy of 2 ended on its
    # first share and dropped the second. Applied once per trade ID, as NautilusTrader's order takes one
    # fill per trade ID; after a reconnect the record is rebuilt from the node's cache, trade IDs with it.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        placed.update(status="partially_filled", filled_qty="1")
        message = {
            "event": "partial_fill",
            "order": dict(placed),
            "timestamp": datetime.now(UTC).isoformat(),
            "price": venue.trading.fill_price,
            "qty": "1",
            "position_qty": "1",
            "execution_id": str(uuid.UUID(int=7)),
        }
        venue.stream.send(message)
        await until_named(strategy, "OrderFilled")
        await settled(client, venue)
        if reconnected:
            await reconnect(client, venue)
        venue.stream.send(dict(message))
        await settled(client, venue)
        venue.trading.update(placed["id"], "fill", 1, status="filled", filled_qty="2")
        await settled(client, venue)

    run_with_client(body, strategy)

    filled = [e for e in strategy.events if type(e).__name__ == "OrderFilled"]
    assert len({e.trade_id for e in filled}) == len(filled) == 2, strategy.names()
    assert sum(e.last_qty.as_double() for e in filled) == 2


def test_a_fill_the_engine_applied_from_a_report_is_not_counted_again_from_the_stream(
    venue, alpaca
) -> None:
    # A fill report handed to the engine applies one share under its trade ID; the stream then delivers the
    # same fill. The record's own count took it a second time and ended a buy of 2 on one share, dropping
    # the real second fill. A trade ID the node's order holds is applied once, whoever applied it.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        execution_id = str(uuid.UUID(int=11))
        activity = alpaca.fill_activity(
            placed, execution_id, 1, "150", datetime.now(UTC), "partial_fill", leaves_qty=1
        )
        client._handle_report(fill_report(activity, client.clock.timestamp_ns()))
        await until_named(strategy, "OrderFilled")
        await settled(client, venue)
        placed.update(status="partially_filled", filled_qty="1")
        venue.stream.send(
            {
                "event": "partial_fill",
                "order": dict(placed),
                "timestamp": datetime.now(UTC).isoformat(),
                "price": "150",
                "qty": "1",
                "position_qty": "1",
                "execution_id": execution_id,
            }
        )
        await settled(client, venue)
        venue.trading.update(placed["id"], "fill", 1, status="filled", filled_qty="2")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.FILLED, 2.0)], strategy.names()


# -- the code review of the record -------------------------------------------------------------------


def test_a_rejected_takeover_alpaca_shows_unfilled_after_a_fill_is_reported_canceled_with_it(
    venue, alpaca
) -> None:
    # The replacement took over a partly filled order and was rejected, and Alpaca's rejected order shows
    # nothing filled. The order before it shows the fill: the report is canceled with it, not rejected
    # with none, which NautilusTrader would leave open.
    replaced_after_one_fill(alpaca, venue, {"status": "rejected", "filled_qty": "0"})
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    node = build_node(trader(submit=False))

    run(node)

    orders = node.cache.orders()
    assert [(o.client_order_id.value, o.status, o.filled_qty.as_double()) for o in orders] == [
        ("my-order-1", OrderStatus.CANCELED, 1.0)
    ]


def test_two_modifies_sent_to_one_order_each_get_their_rejection(venue, generated) -> None:
    # Two replaces sent to the order before either has an outcome, both replacements rejected while the
    # order stays open: each modify gets its own rejection, where one entry per Alpaca order held both.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    seen: list[str] = []

    async def second_then_both_rejected(client) -> None:
        old, first = placed_order(venue, 0), placed_order(venue, 1)
        old.update(status="new", replaced_by=None)
        cached = client.cache.order(strategy.client_order_id)
        await client._modify_order(
            SimpleNamespace(
                client_order_id=strategy.client_order_id,
                venue_order_id=cached.venue_order_id,
                quantity=None,
                price=Price.from_str("151.00"),
                trigger_price=None,
            )
        )
        second = placed_order(venue, 2)
        old.update(status="new", replaced_by=None)
        generated.clear()
        for replacement in (first, second):
            venue.trading.update(replacement["id"], "rejected", status="rejected")
        await settled(client, venue)
        seen.extend(generated)

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), second_then_both_rejected
    )

    assert seen == ["modify_rejected", "modify_rejected"], seen


def test_a_cancel_refused_on_a_replacement_not_yet_adopted_is_a_cancel_rejection(
    venue, monkeypatch
) -> None:
    # A cancel is answered 204 and Alpaca refuses it on the replacement's stream before its `replaced`
    # for the order it replaces is applied: the node's pending cancel gets its refusal.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()

    async def refused_on_the_replacement(client) -> None:
        new = placed_order(venue, 1)
        monkeypatch.setattr(venue.trading, "cancel_order_by_id", lambda order_id: None)
        assert strategy.client_order_id is not None
        strategy.cancel_order(strategy.client_order_id)
        await settled(client, venue)
        venue.trading.update(new["id"], "order_cancel_rejected")
        await until_named(strategy, "OrderCancelRejected", timeout=5)

    run_modify(
        venue, strategy, lambda: bool(venue.trading.replace_requests), refused_on_the_replacement
    )

    assert "OrderCancelRejected" in strategy.names(), strategy.names()


def test_an_ended_order_leaves_nothing_in_the_index(venue) -> None:
    # The index holds what the client traced while an order lives; once it ends, the node's cache knows
    # its Alpaca orders, so a long session does not keep one entry per order it ever placed.
    venue.trading.script = "accept"
    strategy = modifier()
    left: list = []

    async def fill(client) -> None:
        new = placed_order(venue, 1)
        venue.trading.update(new["id"], "fill", 2, status="filled", filled_qty="2")
        await until_named(strategy, "OrderFilled")
        await settled(client, venue)
        left.extend(k for k, v in client._index.items() if v == strategy.client_order_id)

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names(), fill)

    assert left == []


def test_under_the_own_reading_a_chain_with_an_unlisted_middle_fill_is_left_to_the_engine(
    venue, alpaca, monkeypatch
) -> None:
    # Three orders, each counting its own fills: the first's and the last's are listed, the middle one's
    # are not. The middle order is read on the way to the last, and its fills are missing: nothing of
    # the chain is reported, rather than two fills taken for all of them.
    monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading.OWN)
    first, second, third = CHAIN_IDS
    at = datetime.now(UTC) - timedelta(minutes=10)
    orders = [
        alpaca.alpaca_order(
            id=first,
            client_order_id="my-order-7",
            qty="3",
            status="replaced",
            replaced_by=second,
            filled_qty="1",
            filled_avg_price="174.6",
        ),
        alpaca.alpaca_order(
            id=second,
            client_order_id="alpaca-made-7a",
            qty="2",
            replaces=first,
            status="replaced",
            replaced_by=third,
            filled_qty="1",
            filled_avg_price="174.6",
        ),
        alpaca.alpaca_order(
            id=third,
            client_order_id="alpaca-made-7b",
            qty="1",
            replaces=second,
            status="filled",
            filled_qty="1",
            filled_avg_price="174.6",
        ),
    ]
    venue.trading.orders = orders
    venue.trading.activities = [
        alpaca.fill_activity(
            orders[0],
            str(uuid.UUID(int=1)),
            1,
            "174.6",
            at,
            "partial_fill",
            cum_qty=1,
            leaves_qty=2,
        ),
        alpaca.fill_activity(
            orders[2],
            str(uuid.UUID(int=3)),
            1,
            "174.6",
            at + timedelta(minutes=2),
            cum_qty=1,
            leaves_qty=0,
        ),
    ]
    reports: list = []

    async def body(client) -> None:
        reports.extend(await client._fill_reports(fills_command()))

    run_with_client(body)

    assert reports == []
