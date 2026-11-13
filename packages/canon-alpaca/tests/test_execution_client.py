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
import contextvars
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
from urllib.parse import urlsplit

import alpaca.common.rest as alpaca_rest
import canon_alpaca.data
import canon_alpaca.execution
import canon_alpaca.factories
import canon_alpaca.providers
import pytest
import requests
import requests.adapters
from alpaca.common.exceptions import APIError
from alpaca.common.rest import RESTClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.requests import StopOrderRequest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import USD
from canon_alpaca.execution import REPLACEMENT_STAMP_DIGITS
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import FilledReading
from canon_alpaca.execution import fill_report
from canon_alpaca.execution import replacement_client_order_id
from canon_alpaca.execution import send_order_commands_once
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
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import SubmissionRecoveryPolicy
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
    strategy: Trader | CancelAll | Modifier | Claimant | None = None,
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
    assert strategy.events[-1].reason == (
        "Alpaca refused the order (403): 40310000: insufficient buying power"
    )


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
            {"order_type": "stop_limit", "price": "201.001", "trigger_price": "200.005"},
            "stop price 200.005 has more than 2 decimals",
        ),
    ],
)
def test_unsupported_order_is_denied_before_alpaca(venue, options, reason) -> None:
    strategy = trader(**options)

    run(build_node(strategy))

    assert strategy.names() == ["OrderDenied"]
    assert reason in strategy.events[0].reason
    assert venue.trading.submitted == []


@pytest.mark.parametrize(
    ("side", "price", "sent"),
    [
        # The emulated trailing exit's release (cx-m-c-composition's REPORT.md, "ORB cross-check"): the
        # emulator rounds to the instrument's 0.0001 increment, which Alpaca rejects at or above $1.
        (OrderSide.SELL, "10.4237", "10.42"),
        (OrderSide.BUY, "10.4237", "10.43"),
        (OrderSide.SELL, "1.0001", "1.00"),
    ],
)
def test_a_limit_finer_than_alpacas_increment_is_sent_on_it_never_less_marketable(
    venue, side, price, sent
) -> None:
    strategy = trader(order_type="limit", side=side, price=price)

    run(build_node(strategy))

    (request,) = venue.trading.submitted
    assert isinstance(request, LimitOrderRequest)
    assert Decimal(str(request.limit_price)) == Decimal(sent)
    # The price sent is the order's before Alpaca's acknowledgement, so a strategy that acts on
    # `OrderAccepted` sees it (the second reader's finding 1, 2026-11-11).
    assert strategy.names() == ["OrderSubmitted", "OrderUpdated", "OrderAccepted", "OrderFilled"]
    updated = strategy.events[1]
    assert updated.price.as_decimal() == Decimal(sent)
    assert updated.venue_order_id == strategy.events[2].venue_order_id


@pytest.mark.parametrize("price", ["0.7212", "10.42"])
def test_a_limit_on_alpacas_increment_is_sent_as_it_is_with_no_update(venue, price) -> None:
    strategy = trader(order_type="limit", side=OrderSide.SELL, price=price)

    run(build_node(strategy))

    (request,) = venue.trading.submitted
    assert Decimal(str(request.limit_price)) == Decimal(price)
    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]


def test_a_stop_limits_limit_is_sent_on_alpacas_increment(venue) -> None:
    venue.trading.script = "accept"
    strategy = trader(
        order_type="stop_limit",
        side=OrderSide.SELL,
        price="199.996",
        trigger_price="200.00",
        cancel_on="OrderAccepted",
    )

    run(build_node(strategy))

    (request,) = venue.trading.submitted
    assert (request.stop_price, request.limit_price) == (200.0, 199.99)
    assert strategy.names()[:3] == ["OrderSubmitted", "OrderUpdated", "OrderAccepted"]


def test_a_rounded_limit_reconciles_with_alpacas_order_unchanged(venue) -> None:
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", side=OrderSide.SELL, price="10.4237")
    seen: list = []

    async def report(client) -> None:
        async with asyncio.timeout(10):
            while "OrderUpdated" not in strategy.names():
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        by_id = SimpleNamespace(venue_order_id=None, client_order_id=strategy.client_order_id)
        reported = await client._generate_order_status_report(by_id)
        seen.append((reported.price, client.cache.order(strategy.client_order_id).price))

    run_with_client(report, strategy)

    ((reported, cached),) = seen
    assert reported == cached == Price.from_str("10.4200")


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


MSFT = InstrumentId.from_str("MSFT.ALPACA")


class OnDemandTrader(Trader):
    """Load a new name through the data client, then submit on the next timer tick."""

    def __init__(self) -> None:
        super().__init__()
        self.quantity = 1
        self.definition_ready = False

    def on_start(self) -> None:
        self.started = True
        self.request_instrument(MSFT)
        self.clock.set_timer(
            "on-demand-entry", timedelta(milliseconds=10), callback=self._enter_loaded
        )

    def on_data_response(self, response) -> None:
        self.definition_ready = response.completed and response.failure_reason is None

    def _enter_loaded(self, event) -> None:
        if not self.definition_ready or self.client_order_id is not None:
            return
        order = self.order_factory.limit(
            MSFT,
            OrderSide.BUY,
            Quantity.from_int(1),
            Price.from_str("10.00"),
            time_in_force=TimeInForce.DAY,
        )
        self.client_order_id = order.client_order_id
        self.submit_order(order)


def test_an_equity_loaded_only_by_the_data_client_can_be_submitted(venue, alpaca) -> None:
    venue.trading.assets.append({**alpaca.ASSET_AAPL, "symbol": "MSFT"})
    venue.trading.fill_price = "10.00"
    clients: list[AlpacaExecutionClient] = []

    class Capturing(AlpacaExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaExecutionClientFactory.create(**kwargs)
            clients.append(client)
            return client

    strategy = OnDemandTrader()
    node = build_node(strategy, exec_factory=Capturing)
    assert node.cache.instrument(MSFT) is None

    run(node)

    assert clients[0]._provider.find(MSFT) is None  # the providers were not merged
    assert node.cache.instrument(MSFT) is not None
    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
    assert len(venue.trading.submitted) == 1 and venue.trading.submitted[0].symbol == "MSFT"


@pytest.mark.parametrize("case", ["non-equity", "wrong-venue", "non-tradable", "absent"])
def test_shared_cache_instrument_lookup_keeps_every_asset_refusal(venue, alpaca, monkeypatch, case):
    instrument_id = InstrumentId.from_str("MSFT.OTHER") if case == "wrong-venue" else MSFT
    cached: Any = canon_alpaca.providers.parse_equity(
        alpaca.asset(symbol="MSFT", tradable=case != "non-tradable"), 0
    )
    if case == "non-equity":
        cached = object()
    elif case == "absent":
        cached = None
    strategy = trader(idle=True)
    lookups: list[InstrumentId] = []

    async def body(client):
        assert client._provider.find(instrument_id) is None
        order = strategy.order_factory.limit(
            instrument_id,
            OrderSide.BUY,
            Quantity.from_int(1),
            Price.from_str("10.00"),
            time_in_force=TimeInForce.DAY,
        )

        def instrument(name):
            lookups.append(name)
            return cached

        with monkeypatch.context() as patch:
            patch.setattr(client, "cache", SimpleNamespace(instrument=instrument))
            reason = client._denial(order)
        if case == "non-tradable":
            assert reason == f"{instrument_id} is not tradable on Alpaca (asset tradable=false)"
        else:
            assert reason == f"{instrument_id} is not a loaded ALPACA equity"

    run_with_client(body, strategy)
    assert venue.trading.submitted == []
    assert lookups == ([] if case == "wrong-venue" else [instrument_id])


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
        ),  # a second attempt, which the first may have reached
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


class GatewayTimeouts:
    """The network under requests' `HTTPAdapter`, standing in for Alpaca behind an intermediary that gives
    up waiting: every request is recorded and answered 504 with a gateway's page, or, given `error`, met
    with that transport error. alpaca-py's own `TradingClient` and `RESTClient` run above it unchanged, its
    retry included; only its wait between attempts is made instant."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, error: Callable[[], Exception] | None = None
    ) -> None:
        self.sent: list[tuple[str, str]] = []

        def send(
            adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None
        ):
            self.sent.append((request.method, urlsplit(request.url).path))
            if error is not None:
                raise error()
            response = requests.Response()
            response.status_code = 504
            response.reason = "Gateway Timeout"
            response._content = b"<html><body>504 Gateway Time-out</body></html>"
            response.url = request.url
            response.request = request
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
        monkeypatch.setattr(alpaca_rest, "time", SimpleNamespace(sleep=lambda _seconds: None))

    def count(self, method: str) -> int:
        return sum(1 for sent, _ in self.sent if sent == method)


def alpaca_py_trading_client() -> TradingClient:
    """alpaca-py's own paper `TradingClient`, prepared as the execution client prepares its own at connect:
    the adapter's timeout and its hook for order commands (ADR 0039)."""
    return send_order_commands_once(
        with_http_timeout(TradingClient(api_key=PLACEHOLDER, secret_key=PLACEHOLDER, paper=True))
    )


def test_connect_prepares_the_trading_client_to_send_order_commands_once(venue) -> None:
    hooks: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        hooks.extend(venue.trading._session.hooks["response"])
        send_order_commands_once(venue.trading)  # a second connect adds nothing
        hooks.append(len(venue.trading._session.hooks["response"]))

    run_with_client(body)

    assert hooks == [canon_alpaca.execution.end_at_first_error, 1]


def test_a_client_without_a_requests_session_is_refused_its_order_commands() -> None:
    with pytest.raises(
        TypeError, match="refusing to run with alpaca-py's resend of order commands"
    ):
        send_order_commands_once(SimpleNamespace())


def test_alpaca_py_resends_a_request_answered_504(monkeypatch) -> None:
    # The upstream behaviour the adapter guards against: alpaca-py 0.45.0 sends a request answered 504
    # four times (`RESTClient._request`, `DEFAULT_RETRY_ATTEMPTS = 3`), an order command included. If this
    # fails, alpaca-py no longer resends, and `execution.send_order_commands_once` and its hook can go.
    wire = GatewayTimeouts(monkeypatch)
    trading = TradingClient(api_key=PLACEHOLDER, secret_key=PLACEHOLDER, paper=True)
    with pytest.raises(APIError) as raised:
        trading.cancel_order_by_id(str(uuid.uuid4()))
    assert raised.value.status_code == 504
    assert wire.count("DELETE") == 4


def test_a_submit_answered_504_is_sent_once_and_stays_submitted(venue, monkeypatch, capfd) -> None:
    # NautilusTrader's policies send a submit "once, unless the safety test holds", and Alpaca's FAQ says
    # not to resend an order that timed out; alpaca-py would resend it three times.
    wire = GatewayTimeouts(monkeypatch)
    real = alpaca_py_trading_client()
    venue.trading.submit_order = real.submit_order
    strategy = trader(order_type="limit")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not wire.count("POST"):
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert wire.count("POST") == 1, wire.sent
    assert strategy.names() == ["OrderSubmitted"]
    text = logged(capfd)
    assert "HTTP 504" in text
    assert "stays SUBMITTED until a trade update or reconciliation resolves it" in text


def test_a_replace_answered_504_is_sent_once_and_left_unknown(venue, monkeypatch) -> None:
    venue.trading.script = "accept"
    wire = GatewayTimeouts(monkeypatch)
    real = alpaca_py_trading_client()
    venue.trading.replace_order_by_id = real.replace_order_by_id
    strategy = modifier()

    run_modify(venue, strategy, lambda: bool(wire.count("PATCH")))

    assert wire.count("PATCH") == 1, wire.sent
    assert "OrderModifyRejected" not in strategy.names()
    assert "OrderUpdated" not in strategy.names()


def test_a_cancel_answered_504_is_sent_once_and_left_unknown(venue, monkeypatch, capfd) -> None:
    venue.trading.script = "accept"
    wire = GatewayTimeouts(monkeypatch)
    real = alpaca_py_trading_client()
    venue.trading.cancel_order_by_id = real.cancel_order_by_id
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not wire.count("DELETE"):
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    run_with_client(body, strategy)

    assert wire.count("DELETE") == 1, wire.sent
    assert "OrderCancelRejected" not in strategy.names()
    assert "OrderCanceled" not in strategy.names()
    assert "got no definitive answer (HTTP 504" in logged(capfd)


TIMEOUTS = {
    "connect": lambda: requests.ConnectTimeout("connect timed out"),
    "read": lambda: requests.ReadTimeout("read timed out"),
}


@pytest.mark.parametrize("timeout", ["connect", "read"])
@pytest.mark.parametrize("command", ["submit", "replace", "cancel"])
def test_an_order_command_that_times_out_is_sent_once(venue, monkeypatch, command, timeout) -> None:
    # alpaca-py resends only on its own `RetryException`, raised for a status in its retry codes; a
    # transport error propagates (`RESTClient._request`), and requests' `HTTPAdapter`, which the
    # adapter's `TimeoutAdapter` subclasses, retries nothing by default (`DEFAULT_RETRIES = 0`). Counted at
    # the transport, each command is sent once. A connect timeout on the only attempt proves nothing was
    # sent, so the command is rejected "not sent"; a read timeout leaves its outcome unknown (ADR 0039).
    method, rejection = {
        "submit": ("POST", "OrderRejected"),
        "replace": ("PATCH", "OrderModifyRejected"),
        "cancel": ("DELETE", "OrderCancelRejected"),
    }[command]
    if command != "submit":
        venue.trading.script = "accept"
    wire = GatewayTimeouts(monkeypatch, TIMEOUTS[timeout])
    real = alpaca_py_trading_client()
    if command == "submit":
        venue.trading.submit_order = real.submit_order
        strategy: Trader | Modifier = trader(order_type="limit")
    elif command == "replace":
        venue.trading.replace_order_by_id = real.replace_order_by_id
        strategy = modifier()
    else:
        venue.trading.cancel_order_by_id = real.cancel_order_by_id
        strategy = trader(order_type="limit", cancel_on="OrderAccepted")

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not wire.count(method):
                await asyncio.sleep(0.01)
        await until_idle(client, venue)

    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run_with_client(body, strategy)
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert wire.count(method) == 1, wire.sent
    if timeout == "connect":
        assert strategy.names()[-1] == rejection, strategy.names()
        (event,) = [e for e in strategy.events if type(e).__name__ == rejection]
        assert event.reason.startswith("not sent: "), event.reason
    else:
        assert rejection not in strategy.names(), strategy.names()


def test_the_trading_clients_transport_retries_nothing(venue) -> None:
    # The second place a resend could come from: the transport adapter on the trading client's session.
    # requests' `HTTPAdapter` defaults to `Retry(0, read=False)`; the adapter's `TimeoutAdapter` must keep
    # that, so a later change to it cannot add a retry unseen.
    adapters: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        session = venue.trading._session
        adapters.extend(session.adapters.get(scheme) for scheme in ("https://", "http://"))

    run_with_client(body)

    for adapter in adapters:
        assert isinstance(adapter, TimeoutAdapter)
        retries = adapter.max_retries
        assert retries.total == 0 and retries.read is False, retries
        assert not retries.connect and not retries.status and not retries.other, retries


def test_a_post_that_times_out_on_the_wire_is_sent_once(venue, monkeypatch) -> None:
    # The same through the real requests and urllib3 stack, no stand-in under it: a local server that
    # reads each request and never answers counts what reaches it.
    monkeypatch.setattr(canon_alpaca.providers, "CONNECT_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr(canon_alpaca.providers, "READ_TIMEOUT_SECS", 0.5)
    server = SilentServer()
    rest = send_order_commands_once(
        with_http_timeout(
            RESTClient(
                base_url=f"http://127.0.0.1:{server.port}",
                api_version="v2",
                api_key=PLACEHOLDER,
                secret_key=PLACEHOLDER,
            )
        )
    )
    outcomes: list[str] = []

    async def body(client: AlpacaExecutionClient) -> None:
        try:
            await client._call(rest.post, "/hang", {}, once=True)
        except requests.ReadTimeout as e:
            outcomes.append(type(e).__name__)

    try:
        run_with_client(body)
        held = len(server.held)
    finally:
        server.close()

    assert outcomes == ["ReadTimeout"]
    assert held == 1


def test_a_read_answered_504_keeps_alpaca_pys_retry(venue, monkeypatch) -> None:
    # A read changes nothing at Alpaca, so a repeat is safe ("Read or other idempotent request": "On a
    # transient failure", NautilusTrader's policies); only order commands are sent once.
    wire = GatewayTimeouts(monkeypatch)
    real = alpaca_py_trading_client()
    outcomes: list[object] = []

    async def body(client: AlpacaExecutionClient) -> None:
        try:
            await client._call(real.get_order_by_client_id, "some-order")
        except APIError as e:
            outcomes.append(e.status_code)

    run_with_client(body)

    assert outcomes == [504]
    assert wire.count("GET") == 4, wire.sent


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
        # `get` is all the fill history uses, and no open order is read (ADR 0075).
        client._trading = SimpleNamespace(get=rest.get, get_orders=lambda filter=None: [])  # ty: ignore[invalid-assignment]
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
    # Alpaca refuses a submit whose client order ID it holds, 422 "client_order_id must be unique" (paper,
    # 2026-11-10), so a refusal can come while Alpaca holds the order. No trade update has arrived yet,
    # so only the lookup can tell.
    venue.trading.script = "silent"
    venue.trading.submit_error = alpaca.api_error(422, 42210000, "client_order_id must be unique")
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
    # A cancel is refused 422 while Alpaca shows the order pending cancel from a cancel before it.
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
    assert strategy.events[-1].reason == (
        "Alpaca refused the cancel (422): 42210000: order is not cancelable"
    )


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
    assert rejection.reason == "Alpaca refused the cancel (422): 42210000: order is not cancelable"
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
    price in `prices` in turn, each after the `OrderUpdated` that answers the last modify."""

    def __init__(self) -> None:
        super().__init__()
        self.order_type = "limit"
        self.price = "150.00"  # the limit buy's price
        self.prices: list[str] = ["150.50"]
        self.field = "price"  # or "trigger_price", or "quantity"
        self.awaiting = False  # a modify sent, its `OrderUpdated` not yet seen
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
                AAPL, OrderSide.BUY, Quantity.from_int(2), Price.from_str(self.price)
            )
        self.client_order_id = order.client_order_id
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderUpdated" and not self.awaiting:
            return  # not the answer to a modify of ours
        if name in ("OrderAccepted", "OrderUpdated") and self.prices:
            if name == "OrderAccepted" and self.names().count("OrderAccepted") > 1:
                return
            value = self.prices.pop(0)
            self.awaiting = True
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
    # The current price is sent for a limit order, and a new client order ID of the order's own form
    # (Alpaca refuses one kept): the order's own with a clock stamp after its count.
    assert (request.limit_price, request.qty) == (150.5, None)
    own = old["client_order_id"]
    assert request.client_order_id == replacement_client_order_id(
        own, int(request.client_order_id[-REPLACEMENT_STAMP_DIGITS:])
    )
    assert new["client_order_id"] == request.client_order_id != own


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
    # The replacement's updates carry a client order ID that is not the node's own; "after-restart"
    # empties the client's map first, so the fill is traced back through `replaces`.
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
    assert reason == "Alpaca refused the replace (422): 42210000: order is not replaceable"


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
    # A second replace of an order already replaced gets 422 "order already replaced" (observed).
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


def test_a_modify_finer_than_alpacas_increment_is_sent_on_it_as_a_submit_is(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier(prices=["150.505"])  # a buy: rounded up, never less marketable

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    ((_, request),) = venue.trading.replace_requests
    assert Decimal(str(request.limit_price)) == Decimal("150.51")
    assert strategy.last("OrderUpdated").price == Price.from_str("150.5100")


def test_a_modify_on_acceptance_is_answered_by_its_own_replace_not_the_rounding(venue) -> None:
    """The second reader's finding 1 (2026-11-11): a strategy that modifies on `OrderAccepted`, as
    upstream's `ExecTester` does, must not get the rounded price's `OrderUpdated` as its modify's
    answer: that put the order back to `ACCEPTED` mid-replace, and a second modify went to the replaced
    order and was lost."""
    venue.trading.script = "accept"
    strategy = modifier(price="150.0001", prices=["150.50", "150.60"])

    run_modify(venue, strategy, lambda: strategy.names().count("OrderUpdated") >= 3)

    updates = [(type(e).__name__, getattr(e, "price", None)) for e in strategy.events]
    assert updates == [
        ("OrderSubmitted", None),
        ("OrderUpdated", Price.from_str("150.0100")),
        ("OrderAccepted", None),
        ("OrderPendingUpdate", None),
        ("OrderUpdated", Price.from_str("150.5000")),
        ("OrderPendingUpdate", None),
        ("OrderUpdated", Price.from_str("150.6000")),
    ]
    (first, second) = [order_id for order_id, _ in venue.trading.replace_requests]
    assert second == placed_order(venue, 1)["id"] != first


def test_a_modify_whose_price_rounds_to_the_orders_own_is_refused_before_alpaca(venue) -> None:
    """The second reader's finding 2 (2026-11-11): `ExecTester`'s one-tick amend of a whole-cent buy
    (TC-E30) rounds back up to the price the order has, so the replace would change nothing."""
    venue.trading.script = "accept"
    strategy = modifier(prices=["149.9999"])

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    reason = strategy.last("OrderModifyRejected").reason
    assert "149.9999 on Alpaca's increment is 150.0000, the order's own" in reason
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


def test_filled_replacement_is_reported_when_replaced_was_missed(venue, alpaca) -> None:
    # The replace is answered, Alpaca's `replaced` never arrives, and the replacement fills unseen: the
    # node's order must still be reported filled, through the replacement, or its fill is lost.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    reports: list = []

    async def reconcile(client) -> None:
        new = placed_order(venue, 1)
        new.update(status="filled", filled_qty="2", filled_avg_price="150.5")
        venue.trading.activities.append(
            alpaca.fill_activity(new, str(uuid.uuid4()), 2, "150.5", datetime.now(UTC))
        )
        venue.trading.orders = list(venue.trading._placed.values())
        forget(client)
        window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
        # The first read brings the fill by its trade ID and holds the report back until the node's
        # order has applied it (ADR 0075); the engine's next check gets the report.
        assert not await client._generate_order_status_reports(window)
        await asyncio.sleep(0.2)
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


def test_rejected_replacement_is_not_reported_after_a_restart(venue, alpaca) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    reports: list = []

    async def restart(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old.update(status="filled", filled_qty="2", filled_avg_price="150")
        venue.trading.activities.append(
            alpaca.fill_activity(old, str(uuid.uuid4()), 2, "150", datetime.now(UTC))
        )
        new["status"] = "rejected"
        venue.trading.orders = list(venue.trading._placed.values())
        forget(client)
        window = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
        # As above: the fill first, by its trade ID, then the report (ADR 0075).
        assert not await client._generate_order_status_reports(window)
        await asyncio.sleep(0.2)
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
        "Alpaca order for AAPL buy limit (client lineage '') is open with a client order ID NautilusTrader "
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
    line = next(
        line for line in logged(capfd).splitlines() if "which this node does not hold" in line
    )
    assert "which this node does not hold; reconciliation reports it" in line
    assert other["id"] not in line


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
    # The order reports' two reads, then the fill reports' own read of the open orders (ADR 0075).
    assert statuses == [QueryOrderStatus.OPEN, QueryOrderStatus.ALL, QueryOrderStatus.OPEN]
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
    # Besides the fill reports' own read of the open orders (ADR 0075).
    (query,) = [q for q in venue.trading.order_queries if q.status != QueryOrderStatus.OPEN]
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

        def listed(filter: Any = None) -> list:
            if filter is not None and filter.status == QueryOrderStatus.OPEN:
                return []  # the fill reports' read of the open orders (ADR 0075): none
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


def test_an_open_trailing_stop_placed_outside_the_node_fails_startup_by_name(
    venue, alpaca, capfd
) -> None:
    """With every setting off an outside order is never taken on unasked (custody's rule; the lead,
    2026-11-12): the node's start fails by name, as before `report_external_trailing_stops` existed."""
    venue.trading.orders = [
        alpaca.alpaca_order(
            type="trailing_stop", order_type="trailing_stop", trail_price="1", limit_price=None
        )
    ]
    node = build_node(trader(submit=False))

    with pytest.raises(Exception):  # noqa: B017 - the node's own startup failure type
        run(node)

    assert "is open with order type trailing_stop" in logged(capfd)


def test_open_order_outside_this_slice_fails_startup(venue, alpaca, capfd) -> None:
    venue.trading.orders = [alpaca.alpaca_order(type=None, order_type=None)]
    node = build_node(trader(submit=False))

    with pytest.raises(Exception):  # noqa: B017 - the node's own startup failure type
        run(node)

    assert "is open with order type None" in logged(capfd)


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


@pytest.mark.parametrize("available", ["321.45", None])
def test_account_report_carries_non_marginable_buying_power(venue, available) -> None:
    """The account's `info` carries Alpaca's `non_marginable_buying_power` unchanged, at connect and on a query.

    alpaca-py c42add7c `alpaca/trading/models.py:626` declares the field as an optional string;
    NautilusTrader d53849b2 `crates/model/src/events/account/state.rs:29-32,63-65` keeps venue fields in `info`.
    """
    venue.trading.account = {**venue.trading.account, "non_marginable_buying_power": available}
    strategy = trader(idle=True)
    seen: list[object] = []

    async def body(client) -> None:
        def info() -> dict:
            account = client.cache.account(ACCOUNT_ID)
            assert account is not None
            return account.last_event.info

        seen.append(info()["non_marginable_buying_power"])
        venue.trading.account = {**venue.trading.account, "non_marginable_buying_power": "12.34"}
        strategy.query_account(ACCOUNT_ID, ClientId("ALPACA"))
        async with asyncio.timeout(10):
            while info()["non_marginable_buying_power"] != "12.34":
                await asyncio.sleep(0.01)
        seen.append(info()["non_marginable_buying_power"])

    run_with_client(body, strategy)

    assert seen == [available, "12.34"]


@pytest.mark.parametrize(
    ("failure", "logged_as"),
    [
        (ConnectionError("connection reset"), "ConnectionError('connection reset')"),
        (None, "cash 'not a number' is not a number"),  # an unparsable `cash`, named
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


@pytest.mark.parametrize("field", ["cash", "currency"])
def test_an_account_answer_without_cash_or_currency_is_refused_by_name(venue, capfd, field) -> None:
    # Found by two independent readers' sweeps (DeepSeek Harness, finding 3; Muse): a missing `cash` read
    # as $0 and a missing `currency` as USD, with no log. Both are optional in alpaca-py's `TradeAccount`.
    strategy = trader(idle=True)
    seen: list = []

    async def body(client) -> None:
        def state() -> tuple[str, int]:
            account = client.cache.account(ACCOUNT_ID)
            assert account is not None
            return str(account.balance().total), len(account.events)

        before = state()
        venue.trading.account = {k: v for k, v in venue.trading.account.items() if k != field}
        strategy.query_account(ACCOUNT_ID, ClientId("ALPACA"))
        await until_idle(client, venue)
        seen.append((before, state()))

    run_with_client(body, strategy)

    ((before, after),) = seen
    assert after == before
    text = logged(capfd)
    assert "Alpaca account query failed:" in text
    assert f"Alpaca's account answer has no {field}" in text


def test_a_connect_whose_account_answer_has_no_cash_fails_naming_it(venue, capfd) -> None:
    venue.trading.account = {k: v for k, v in venue.trading.account.items() if k != "cash"}
    node = build_node(trader(submit=False))

    with pytest.raises(
        RuntimeError, match="readiness timeout while waiting for engine connections"
    ):
        run(node)

    assert node.cache.account(ACCOUNT_ID) is None
    assert "failed to connect: Alpaca's account answer has no cash" in logged(capfd)


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

    log = logged(capfd)
    assert (
        "Alpaca order for AAPL buy limit (client lineage 'alpaca-made-2') is open and tracing it failed"
        in log
    )
    assert replacement["id"] not in log


@pytest.mark.parametrize("error", TRACE_FAILURES, ids=TRACE_FAILURE_IDS)
def test_a_closed_replacement_whose_trace_fails_is_skipped(venue, alpaca, capfd, error) -> None:
    replacement = replacement_of_an_unread_order(alpaca, venue, "canceled", error)
    node = build_node(trader(submit=False))

    run(node)

    assert node.cache.orders_open() == []
    log = logged(capfd)
    assert (
        "Alpaca order for AAPL buy limit (client lineage 'alpaca-made-2') is not reported: tracing it failed"
        in log
    )
    assert replacement["id"] not in log


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
            if venue.trading.orders[0]["status"] != "filled":
                venue.trading.orders[0].update(
                    status="filled", filled_qty="2", filled_avg_price="150"
                )
                venue.trading.activities.append(
                    alpaca.fill_activity(
                        venue.trading.orders[0], str(uuid.uuid4()), 2, "150", datetime.now(UTC)
                    )
                )
            return answer

        venue.trading.get_orders = fills_after_the_first_read
        # The fill first, by its trade ID, then the report, on the engine's next check (ADR 0075).
        assert not await client._generate_order_status_reports(window)
        await asyncio.sleep(0.2)
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
        "Alpaca order for AAPL buy limit (client lineage '') (canceled) is not reported: a client order ID "
        "NautilusTrader cannot hold (invalid string for 'value', was empty)"
    ) in log
    assert order["id"] not in log
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
    venue.trading.submit_error = alpaca.api_error(422, 42210000, "client_order_id must be unique")
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


async def until_true(check: Callable[[], bool], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


async def until_named(
    strategy: Trader | Modifier | Claimant, *names: str, timeout: float = 10
) -> None:
    await until_true(lambda: any(name in strategy.names() for name in names), timeout)


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
    # 204, the in-flight check's query reads it canceled with the share filled, and the client brings
    # that fill by its trade ID before the report (ADR 0075). The stream's late `partial_fill` is then
    # dropped, not booked a second time.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        (placed,) = venue.trading._placed.values()
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
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
        venue.stream.send(late)
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
    assert (
        "Alpaca order for AAPL buy limit" in failure and "is open and tracing it failed" in failure
    )
    # the reason start-up failed is in the message, not only the exception's class
    assert f"replace chain needs more than {MAX_REPLACE_CHAIN} reads to trace" in failure
    assert chain[-1]["id"] not in failure


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


# -- one fill, one identity, whichever source brings it ---------------------------------------------


@pytest.mark.parametrize("reconnected", [False, True], ids=["same-connect", "after-reconnect"])
def test_a_fill_a_query_report_shows_is_not_counted_again_from_the_stream(
    venue, reconnected
) -> None:
    # Found by two independent readers (Muse, finding 1; Antigravity, finding 1): an open order's status
    # report showing a fill reached the engine before the stream's update for that share; the engine
    # inferred the fill under a trade ID of its own, the stream's `execution_id` then counted it again, and
    # a buy of 2 ended FILLED on one share at Alpaca. A fill keeps Alpaca's execution ID from every source
    # ("Stable `trade_id` values let the engine deduplicate the same fill across both channels").
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await client._query_order(
            SimpleNamespace(
                venue_order_id=VenueOrderId(placed["id"]),
                client_order_id=strategy.client_order_id,
            )
        )
        await settled(client, venue)
        if reconnected:  # the client keeps nothing of the report; the node's order holds its fill
            await reconnect(client, venue)
        venue.stream.send(late)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), len(cached.trade_ids)))
        # The order's second share, past what the report showed: a fill of its own.
        venue.trading.update(placed["id"], "fill", 1, status="filled", filled_qty="2")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), len(cached.trade_ids)))

    run_with_client(body, strategy)

    assert held == [
        (OrderStatus.PARTIALLY_FILLED, 1.0, 1),
        (OrderStatus.FILLED, 2.0, 2),
    ], strategy.names()


def test_a_stream_fill_before_a_later_fills_report_is_its_own_fill(venue) -> None:
    # The code review of the rule: a fill report for the order's second share (B) reached the engine
    # before the stream's update for its first (A). A rule on quantity alone took A, whose `filled_qty` the
    # order had applied, for B and dropped it. B's ID is Alpaca's, so nothing was inferred: A counts.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        trade_a = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        trade_b = venue.trading.update(
            placed["id"], "fill", 1, deliver=False, status="filled", filled_qty="2"
        )
        reports = await client._fill_reports(
            fills_command(venue_order_id=VenueOrderId(placed["id"]))
        )
        client._handle_report(
            next(r for r in reports if str(r.trade_id) == trade_b["execution_id"])
        )
        await settled(client, venue)
        venue.stream.send(trade_a)
        await settled(client, venue)
        venue.stream.send(trade_b)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), len(cached.trade_ids)))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.FILLED, 2.0, 2)], strategy.names()


def test_a_partial_fill_whose_order_shows_the_count_before_it_is_counted(venue) -> None:
    # The code review of the rule: whether a `partial_fill`'s order shows `filled_qty` after the fill is
    # not on Alpaca's page. One that showed it before, with nothing inferred, must still count.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        update = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        update["order"] = {**update["order"], "filled_qty": "0"}
        venue.stream.send(update)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.PARTIALLY_FILLED, 1.0)], strategy.names()


def test_a_late_fill_of_a_replaced_order_a_report_showed_is_not_counted_again(venue) -> None:
    # The code review of the rule: an order's fill reached the engine through a `QueryOrder` report, the
    # order was then replaced, and the stream's update for that fill arrived on the order replaced. A rule
    # kept to the order now working counted it a second time.
    venue.trading.script = "accept"
    strategy = modifier(prices=[])
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await client._query_order(
            SimpleNamespace(
                venue_order_id=VenueOrderId(placed["id"]),
                client_order_id=strategy.client_order_id,
            )
        )
        await settled(client, venue)
        assert strategy.client_order_id is not None
        strategy.modify_order(strategy.client_order_id, price=Price.from_str("149.00"))
        await until_named(strategy, "OrderUpdated")
        await settled(client, venue)
        venue.stream.send(late)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.filled_qty.as_double(), len(cached.trade_ids)))

    run_with_client(body, strategy)

    assert held == [(1.0, 1)], strategy.names()


def test_a_fill_report_and_stream_fills_interleaved_are_each_counted_once(venue, alpaca) -> None:
    # Found by an independent reader (Codex, finding 1): trade A reaches the engine as a fill report, B
    # through the stream, then A again through the stream. The record consulted the order's trade IDs
    # only while the order showed more filled than its own count, so after B it counted A a second time
    # (the engine refused it), C ended the order, and D was dropped: PARTIALLY_FILLED 3 with 4 filled at
    # Alpaca.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=4)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        trade_a = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        reports = await client._fill_reports(
            fills_command(venue_order_id=VenueOrderId(placed["id"]))
        )
        client._handle_report(
            next(r for r in reports if str(r.trade_id) == trade_a["execution_id"])
        )
        await settled(client, venue)
        venue.trading.update(
            placed["id"], "partial_fill", 1, status="partially_filled", filled_qty="2"
        )
        await settled(client, venue)
        venue.stream.send(trade_a)
        await settled(client, venue)
        venue.trading.update(
            placed["id"], "partial_fill", 1, status="partially_filled", filled_qty="3"
        )
        await settled(client, venue)
        venue.trading.update(placed["id"], "fill", 1, status="filled", filled_qty="4")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), len(cached.trade_ids)))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.FILLED, 4.0, 4)], strategy.names()


# -- one identity for a fill: Alpaca's trade ID, and no inferred fill for an order the node holds (ADR 0075)


async def query(client: AlpacaExecutionClient, strategy: Trader | Modifier, placed: dict) -> None:
    """Answer the engine's `QueryOrder` for the strategy's order, as the in-flight and open-order checks
    send it."""
    command = SimpleNamespace(
        venue_order_id=VenueOrderId(placed["id"]), client_order_id=strategy.client_order_id
    )
    await client._query_order(cast(Any, command))


def trade_ids(order: Any) -> list[str]:
    return [e.trade_id.value for e in order.events() if type(e).__name__ == "OrderFilled"]


def test_a_query_report_and_the_stream_fill_it_shows_in_one_loop_turn_count_it_once(venue) -> None:
    # Found by an independent reader (Antigravity, round 1 on the fill-identity model, finding 3): a
    # `QueryOrder` report showing a fill was handed to the engine, and on the same loop turn the stream's
    # update for that fill was judged against the node's cache, which had not applied the engine's
    # inferred fill yet. Both counted: FILLED 2 on one share at Alpaca.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await query(client, strategy, placed)
        await client._apply(TradeUpdate(**late))  # no await between: the engine has not run
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(late["execution_id"])

    run_with_client(body, strategy)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.PARTIALLY_FILLED, 1.0, [execution_id])


@pytest.mark.parametrize("quantity", [3, 4])
def test_a_larger_fill_after_a_smaller_one_a_query_report_showed_is_counted_once(
    venue, quantity
) -> None:
    # Antigravity, finding 2: A, 2 shares, reached the engine as a share count from a `QueryOrder` report
    # before its stream update; B, 1 share, arrived on the stream first and was taken for one of A's
    # shares; A's own update then counted both of its shares again. With 4 shares the order ended FILLED
    # on 3 at Alpaca; with 3 the engine refused A as an overfill while the client ended the order.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=quantity)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        trade_a = venue.trading.update(
            placed["id"],
            "partial_fill",
            2,
            deliver=False,
            status="partially_filled",
            filled_qty="2",
        )
        await query(client, strategy, placed)
        await settled(client, venue)
        trade_b = venue.trading.update(
            placed["id"],
            "fill" if quantity == 3 else "partial_fill",
            1,
            deliver=False,
            status="filled" if quantity == 3 else "partially_filled",
            filled_qty="3",
        )
        trade_b["order"] = {**trade_b["order"], "filled_qty": "2"}  # shown before the fill
        venue.stream.send(trade_b)
        await settled(client, venue)
        venue.stream.send(trade_a)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.filled_qty.as_double(), sorted(trade_ids(cached))))
        held.append(sorted([trade_a["execution_id"], trade_b["execution_id"]]))

    run_with_client(body, strategy)

    (filled, trades), alpacas = held
    assert (filled, trades) == (3.0, alpacas), strategy.names()


def test_fills_at_two_prices_delivered_out_of_order_keep_the_average_price(venue) -> None:
    # Antigravity, finding 7: share 1 at $150 reached the engine through a `QueryOrder` report before its
    # stream update; share 2 at $140, its order shown before the fill, was taken for share 1, and share
    # 1's own update was counted as a new fill. The order ended with an average of $150, not $145.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        venue.trading.fill_price = "150"
        first = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await query(client, strategy, placed)
        await settled(client, venue)
        venue.trading.fill_price = "140"
        second = venue.trading.update(
            placed["id"], "fill", 1, deliver=False, status="filled", filled_qty="2"
        )
        second["order"] = {**second["order"], "filled_qty": "1"}  # shown before the fill
        venue.stream.send(second)
        await settled(client, venue)
        venue.stream.send(first)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.avg_px))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.FILLED, 145.0)], strategy.names()


PAPER_CHECKS = LiveExecutionEngineConfig(
    reconciliation_startup_delay_secs=0,
    open_check_interval_secs=0.25,
    open_check_threshold_ms=200,
    position_check_interval_secs=0.4,
)
"""The paper node's open-order and position checks (`paper_node.exec_engine_config`, open orders only by
the engine's default), run every fraction of a second so a test sees several cycles."""


@pytest.mark.parametrize("closed", [False, True], ids=["open", "closed"])
def test_a_lost_fill_update_is_brought_by_its_trade_id_and_books_no_external_order(
    venue, alpaca, closed
) -> None:
    # Antigravity, finding 1: the stream lost share 1's update and the engine inferred it from the
    # open-order check, under an ID of its own. While the order stayed open, every later update showing
    # the count before its fill was taken for that share; once Alpaca closed it, the engine never read it
    # again, the position check refused Alpaca's real fills as overlapping the inferred one and booked the
    # missing share to an EXTERNAL order, and the strategy's order stayed PARTIALLY_FILLED.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        lost = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        venue.trading.positions = [
            {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
        ]
        await asyncio.sleep(0.8)  # open-order checks
        second = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="2",
        )
        second["order"] = {**second["order"], "filled_qty": "1"}  # shown before the fill
        venue.stream.send(second)
        await settled(client, venue)
        third = venue.trading.update(
            placed["id"],
            "fill" if closed else "partial_fill",
            1,
            deliver=not closed,
            status="filled" if closed else "partially_filled",
            filled_qty="3",
        )
        if closed:
            venue.trading.positions[0].update(qty="3", qty_available="3")
            third["order"] = {**third["order"], "filled_qty": "2"}  # shown before the fill
            venue.stream.send(third)
        else:
            venue.trading.positions[0].update(qty="3", qty_available="3")
        await asyncio.sleep(1.2)  # open-order and position checks
        cached = client.cache.order(strategy.client_order_id)
        orders = [(o.strategy_id.value, o.filled_qty.as_double()) for o in client.cache.orders()]
        held.append((cached.filled_qty.as_double(), sorted(trade_ids(cached)), orders))
        held.append(sorted(u["execution_id"] for u in (lost, second, third)))

    run_with_client(body, strategy, exec_engine=PAPER_CHECKS)

    (filled, trades, orders), alpacas = held
    assert (filled, trades) == (3.0, alpacas), strategy.names()
    assert [filled for strategy_id, filled in orders if strategy_id != "EXTERNAL"] == [3.0]
    assert len(orders) == 1, orders


def test_a_report_showing_a_fill_the_activities_do_not_list_yet_is_held_back(venue, capfd) -> None:
    # The model's own case (ADR 0075): a `QueryOrder` report shows a fill whose stream update and fill
    # activity have not come. The engine would infer it under an ID of its own; the report is held back,
    # and the fill counts once, under Alpaca's ID, when its update comes.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        listed = venue.trading.activities.pop()  # the activities lag
        await query(client, strategy, placed)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append(cached.filled_qty.as_double())
        venue.trading.activities.append(listed)
        venue.stream.send(late)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.filled_qty.as_double(), trade_ids(cached) == [late["execution_id"]]))

    run_with_client(body, strategy)

    assert held == [0.0, (1.0, True)], strategy.names()
    assert "not reported until Alpaca lists them" in logged(capfd)


def test_a_fill_on_an_order_of_the_chain_the_node_never_held_keeps_the_working_order(
    venue, alpaca
) -> None:
    # Antigravity, finding 4: an open replace chain reconciled at start-up holds only the working
    # replacement; a fill on the order it replaced was applied under that order's ID, which moved the
    # node's order back to it. A modify then went to the dead order, and after a reconnect the working
    # order's own end was ignored. NautilusTrader applies a fill under an ID the order never held to the
    # order's own (`reconcile_fill_report`, `crates/execution/src/reconciliation/orders.rs` at the pin).
    original = alpaca.alpaca_order(
        id=REPLACED_ID, client_order_id="my-order-1", status="replaced", replaced_by=REPLACEMENT_ID
    )
    replacement = alpaca.alpaca_order(
        id=REPLACEMENT_ID, client_order_id="alpaca-made-8", replaces=REPLACED_ID, status="new"
    )
    venue.trading.orders = [original, replacement]
    node_order = ClientOrderId("my-order-1")
    held: list = []

    async def body(client) -> None:
        venue.stream.send(
            {
                "event": "partial_fill",
                "order": {**original, "filled_qty": "1"},
                "timestamp": datetime.now(UTC).isoformat(),
                "price": "150",
                "qty": "1",
                "position_qty": "1",
                "execution_id": str(uuid.UUID(int=3)),
            }
        )
        await settled(client, venue)
        cached = client.cache.order(node_order)
        held.append((cached.venue_order_id.value, cached.filled_qty.as_double()))
        await reconnect(client, venue)
        venue.trading.orders[1] = {**replacement, "status": "canceled", "filled_qty": "1"}
        venue.stream.send(
            {
                "event": "canceled",
                "order": venue.trading.orders[1],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await settled(client, venue)
        held.append(client.cache.order(node_order).status)

    run_with_client(body)

    assert held == [(REPLACEMENT_ID, 1.0), OrderStatus.CANCELED]


@pytest.mark.parametrize("reading", READINGS)
def test_a_replacements_fill_after_a_reconnect_counts_under_either_reading(
    venue, monkeypatch, reading
) -> None:
    # Antigravity, finding 5: under the own reading, a record rebuilt after a reconnect knows nothing of
    # what the replaced order filled, so a real fill on the replacement was taken for a share the engine
    # had inferred from a `QueryOrder` report, and a filled buy of 2 stayed PARTIALLY_FILLED.
    monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading(reading))
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    held: list = []

    async def body(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        old["status"] = "new"
        venue.trading.update(
            old["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
        )
        await query(client, strategy, old)
        await settled(client, venue)
        new.update(replacement_as_read(reading, 0))
        client._records[strategy.client_order_id].answers.clear()
        venue.trading.confirm_replace(old["id"])
        await until_named(strategy, "OrderUpdated")
        await settled(client, venue)
        await reconnect(client, venue)
        venue.trading.update(
            new["id"],
            "fill",
            1,
            status="filled",
            filled_qty=replacement_as_read(reading, 1)["filled_qty"],
        )
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), body)

    assert held == [(OrderStatus.FILLED, 2.0)], strategy.names()


@pytest.mark.parametrize("streamed", [False, True], ids=["never-streamed", "streamed"])
def test_a_full_history_check_after_a_query_report_counts_each_share_once(
    venue, alpaca, streamed
) -> None:
    # Found by an independent reader (Codex, round 1 on the fill-identity model): a buy of 3 filled A at
    # Alpaca, the engine inferred A from a `QueryOrder` report (and, in one case, A's stream update was
    # then taken for that share), Alpaca filled B and canceled the rest, and a full-history open-order
    # check (`open_check_open_only=False`) read the fill history: A and B both reached the engine as new
    # fills, FILLED 3 where Alpaca shows 2. The intervals are short only so the run is bounded.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    observed: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late_a = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await query(client, strategy, placed)
        await settled(client, venue)
        if streamed:
            venue.stream.send(late_a)
            await settled(client, venue)
        venue.trading.positions = [
            {
                **alpaca.POSITION_AAPL,
                "qty": "2",
                "qty_available": "2",
                "avg_entry_price": str(venue.trading.fill_price),
            }
        ]
        venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="2",
        )
        venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        await asyncio.sleep(4)
        order = client.cache.order(strategy.client_order_id)
        observed.append((order.status, order.filled_qty.as_double()))

    run_with_client(
        body,
        strategy,
        exec_engine=LiveExecutionEngineConfig(
            reconciliation_startup_delay_secs=0,
            open_check_interval_secs=0.25,
            open_check_threshold_ms=2000,
            open_check_open_only=False,
            open_check_missing_retries=1,
            single_order_query_delay_ms=0,
        ),
    )

    assert observed == [(OrderStatus.CANCELED, 2.0)], strategy.names()


def open_order_filled_one_of_two(alpaca, venue) -> dict:
    """An open buy of 2 filled 1 before start-up, its fill activity not listed, and the long it left."""
    order = alpaca.alpaca_order(status="partially_filled", filled_qty="1", filled_avg_price="150")
    venue.trading.orders = [order]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    return order


def test_an_open_order_whose_fills_alpaca_does_not_list_fails_startup_by_name(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # The start-up form of finding 1 (Muse, Antigravity): an open order's fill not yet in Alpaca's fill
    # activities was inferred by the engine under an ID of its own, so the stream's update for that share,
    # or a later fill report, would count it again. Start-up reads the activities again a bounded number
    # of times and then fails, naming the order.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05, raising=False)
    order = open_order_filled_one_of_two(alpaca, venue)
    node = build_node(trader(submit=False))

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(node)

    assert f"open order {order['id']}" in logged(capfd)
    reads = [r for r in venue.trading.activity_requests if r.get("order_id") == order["id"]]
    assert len(reads) == canon_alpaca.execution.FILL_READ_TRIES


def test_an_open_orders_fill_listed_on_a_second_read_reaches_startup_under_its_trade_id(
    venue, alpaca, monkeypatch
) -> None:
    # The same order whose activity Alpaca lists by the second read of its own: start-up takes it under
    # its trade ID, and the stream's update for that share then counts nothing again. Muse (outside round
    # 1 on ADR 0075, finding 4) showed it listed on the first such read, so it passed with no retry; it is
    # now listed only from the second, and the reads are counted.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05, raising=False)
    order = open_order_filled_one_of_two(alpaca, venue)
    execution_id = str(uuid.UUID(int=21))
    activity = alpaca.fill_activity(
        order,
        execution_id,
        1,
        "150",
        datetime.now(UTC) - timedelta(seconds=5),
        "partial_fill",
        1,
        1,
    )
    answer = venue.trading.get

    def own_reads() -> list[dict]:
        return [r for r in venue.trading.activity_requests if r.get("order_id") == order["id"]]

    def lagging(path: str, data: dict | None = None) -> list[dict]:
        if own_reads() and activity not in venue.trading.activities:
            venue.trading.activities.append(activity)
        return answer(path, data)

    venue.trading.get = lagging
    external = ClientOrderId(order["client_order_id"])
    held: list = []

    async def body(client) -> None:
        venue.stream.send(
            {
                "event": "partial_fill",
                "order": dict(order),
                "timestamp": datetime.now(UTC).isoformat(),
                "price": "150",
                "qty": "1",
                "position_qty": "1",
                "execution_id": execution_id,
            }
        )
        await settled(client, venue)
        cached = client.cache.order(external)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(len(own_reads()))

    run_with_client(body)

    assert held == [(OrderStatus.PARTIALLY_FILLED, 1.0, [execution_id]), 2]


STEP_ORDERS = ["report-then-update", "update-then-report", "another-fill-between"]


@pytest.mark.parametrize("step", STEP_ORDERS)
def test_a_fill_report_and_its_stream_update_in_one_engine_step_count_it_once(
    venue, alpaca, step
) -> None:
    # Asked by the lead before landing: a fill report the engine has not applied yet, and the stream's
    # update for the same fill in the same engine step. The update counts it once on the record, which
    # adds a fill to the larger of its own count and the cached order's, never their sum; the engine
    # refuses the repeat. A guard: it passed before the question too.
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=4)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        trade_a = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        reports = await client._fill_reports(
            fills_command(venue_order_id=VenueOrderId(placed["id"]))
        )
        report = next(r for r in reports if str(r.trade_id) == trade_a["execution_id"])
        if step == "update-then-report":
            await client._apply(TradeUpdate(**trade_a))
            client._handle_report(report)
        else:
            client._handle_report(report)  # no await before the updates: the engine has not run
            if step == "another-fill-between":
                trade_b = venue.trading.update(
                    placed["id"],
                    "partial_fill",
                    2,
                    deliver=False,
                    status="partially_filled",
                    filled_qty="3",
                )
                await client._apply(TradeUpdate(**trade_b))
            await client._apply(TradeUpdate(**trade_a))
        await settled(client, venue)
        if step != "another-fill-between":
            venue.trading.update(
                placed["id"], "partial_fill", 2, status="partially_filled", filled_qty="3"
            )
            await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))
        venue.trading.update(placed["id"], "fill", 1, status="filled", filled_qty="4")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), len(set(trade_ids(cached)))))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.PARTIALLY_FILLED, 3.0), (OrderStatus.FILLED, 4.0, 3)], (
        strategy.names()
    )


# -- the code review of the model (ADR 0075), round 1 ----------------------------------------------

SHORT_WINDOW = LiveExecutionEngineConfig(reconciliation_lookback_mins=1)
"""Start-up reconciliation of the last minute only, as the paper node's (`RECONCILIATION_LOOKBACK_MINS`)."""


def test_an_open_orders_fill_older_than_the_startup_window_is_counted_once(venue, alpaca) -> None:
    # The code review of the model: an open order the node did not hold, filled 1 of 3 before the engine's
    # window, had its fill dropped from the fill reports and inferred by the engine under an ID of its
    # own; a later report showing a second fill then had the client bring every listed fill, the first
    # again under its real ID. Start-up now reports an open order's fills at any age.
    order = alpaca.alpaca_order(
        qty="3", status="partially_filled", filled_qty="1", filled_avg_price="150"
    )
    venue.trading.orders = [order]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=31)),
            1,
            "150",
            datetime.now(UTC) - timedelta(minutes=10),
            "partial_fill",
            leaves_qty=2,
        )
    ]
    external = ClientOrderId(order["client_order_id"])
    held: list = []

    async def body(client) -> None:
        cached = client.cache.order(external)
        held.append(trade_ids(cached))
        order.update(filled_qty="2")
        venue.trading.activities.append(
            alpaca.fill_activity(
                order, str(uuid.UUID(int=32)), 1, "150", datetime.now(UTC), "partial_fill", 2, 1
            )
        )
        command = SimpleNamespace(
            venue_order_id=VenueOrderId(order["id"]), client_order_id=external
        )
        await client._query_order(cast(Any, command))
        await settled(client, venue)
        cached = client.cache.order(external)
        held.append((cached.filled_qty.as_double(), sorted(trade_ids(cached))))

    run_with_client(body, exec_engine=SHORT_WINDOW)

    assert held == [
        [str(uuid.UUID(int=31))],
        (2.0, [str(uuid.UUID(int=31)), str(uuid.UUID(int=32))]),
    ]


def test_an_open_replacement_whose_earlier_orders_are_outside_the_window_starts_up(
    venue, alpaca
) -> None:
    # The code review of the model: the check of an open order's fills followed its replace chain only
    # through the orders already read, so a replacement whose earlier orders fell outside the window
    # read none of their fills and failed start-up although Alpaca lists them all.
    long_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    first = alpaca.alpaca_order(
        id="9a000000-0000-4000-8000-000000000001",
        client_order_id="outside-1",
        status="replaced",
        replaced_by="9a000000-0000-4000-8000-000000000002",
        filled_qty="1",
        filled_avg_price="150",
        submitted_at=long_ago,
        created_at=long_ago,
    )
    middle = alpaca.alpaca_order(
        id="9a000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-21",
        status="replaced",
        replaces=first["id"],
        replaced_by="9a000000-0000-4000-8000-000000000003",
        filled_qty="1",
        filled_avg_price="150",
        submitted_at=long_ago,
        created_at=long_ago,
    )
    working = alpaca.alpaca_order(
        id="9a000000-0000-4000-8000-000000000003",
        client_order_id="alpaca-made-22",
        status="partially_filled",
        replaces=middle["id"],
        filled_qty="1",
        filled_avg_price="150",
    )
    venue.trading.orders = [first, middle, working]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    venue.trading.activities = [
        alpaca.fill_activity(
            first,
            str(uuid.UUID(int=41)),
            1,
            "150",
            datetime.now(UTC) - timedelta(hours=2),
            "partial_fill",
            leaves_qty=1,
        )
    ]
    node = build_node(trader(submit=False), exec_engine=SHORT_WINDOW)

    run(node)

    (order,) = [o for o in node.cache.orders() if o.client_order_id.value == "outside-1"]
    assert (order.filled_qty.as_double(), trade_ids(order)) == (1.0, [str(uuid.UUID(int=41))])


def test_a_later_update_showing_more_fills_widens_the_reads_already_waiting(
    venue, monkeypatch
) -> None:
    # The code review of the model: while later reads of the activities waited for fills one update
    # showed, a second update showing more did not widen them, so they stopped at the first's count and
    # the rest were never read or reported.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 1.0)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=5)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        trading = venue.trading

        def fill(shares: int, deliver: bool = True) -> dict:
            done = shares == 5
            return trading.update(
                placed["id"],
                "fill" if done else "partial_fill",
                1,
                deliver=deliver,
                status="filled" if done else "partially_filled",
                filled_qty=str(shares),
            )

        fill(1)
        await until_idle(client, venue)
        fill(2, deliver=False)  # lost, and not listed yet
        second = trading.activities.pop()
        fill(3)  # the reads start, waiting for 3
        await until_idle(client, venue)
        fill(4, deliver=False)
        fourth = trading.activities.pop()
        fill(5)  # shows 5 while they wait
        await until_idle(client, venue)
        trading.activities.append(second)  # listed by the first later read
        await asyncio.sleep(1.0)
        trading.activities.append(fourth)  # listed only by the second
        await asyncio.sleep(1.0)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.FILLED, 5.0)], strategy.names()


# -- the code review of the model (ADR 0075), round 2 ----------------------------------------------


def test_an_update_showing_more_during_the_last_later_read_gets_reads_of_its_own(
    venue, monkeypatch, capfd
) -> None:
    # The code review, round 2: a larger count shown while the last later read was out met a loop that
    # had no reads left, and the order was left with an error naming the smaller count.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=5)
    strategy.stopped_for = "held open"
    reads: list[Decimal] = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        client_order_id = strategy.client_order_id
        assert client_order_id is not None

        async def reading(order, record, shown, venue_order_id, walk=True, logged=True) -> bool:
            reads.append(shown)
            if len(reads) == canon_alpaca.execution.FILL_READ_TRIES - 1:  # the last read for 2
                client._catch_up_later(client_order_id, Decimal(4), venue_order_id)
                return True
            return len(reads) >= canon_alpaca.execution.FILL_READ_TRIES + 1

        client._catch_up = reading
        client._catch_up_later(client_order_id, Decimal(2), VenueOrderId(placed_order(venue)["id"]))
        await asyncio.sleep(0.5)

    run_with_client(body, strategy)

    # 2 read once by the later reads (the update's own read was the first), then 4 its own reads.
    assert reads == [Decimal(2), Decimal(2), Decimal(4), Decimal(4)], reads
    assert "ran out after" not in logged(capfd)


def test_an_open_orders_old_fills_are_not_reported_again_once_the_node_holds_it(
    venue, alpaca
) -> None:
    # The code review, rounds 2 to 4: an open order's fills read at start-up were added to later fill
    # reports requests, outside their window, from a read long past. Each request reads the open orders
    # itself, and an order the node holds since is not one the engine could infer fills for.
    order = alpaca.alpaca_order(
        qty="3", status="partially_filled", filled_qty="1", filled_avg_price="150"
    )
    venue.trading.orders = [order]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    venue.trading.activities = [
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=51)),
            1,
            "150",
            datetime.now(UTC) - timedelta(minutes=10),
            "partial_fill",
            leaves_qty=2,
        )
    ]
    later: list = []

    async def body(client) -> None:
        start = dt_to_unix_nanos(datetime.now(UTC) - timedelta(minutes=1))
        later.append(await client._fill_reports(fills_command(start=start)))

    run_with_client(body, exec_engine=SHORT_WINDOW)

    assert later == [[]]


def test_under_the_own_reading_an_open_replacements_unlisted_fills_fail_startup(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # Raised by the code review, round 2, and not reproduced: under the own reading the earlier order's
    # listed fills could seem to stand for an open replacement's own. The replacement's report counts
    # the chain's fills under either reading (`_order_report`), so its own unlisted fill still fails
    # start-up. Kept as a guard; it passed before the round's fixes too.
    monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading.OWN)
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    replaced_after_one_fill(
        alpaca,
        venue,
        {"status": "partially_filled", "qty": "1", "filled_qty": "1", "filled_avg_price": "150"},
    )
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "2", "qty_available": "2", "avg_entry_price": "150"}
    ]
    node = build_node(trader(submit=False))

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(node)

    assert f"open order {REPLACEMENT_ID}" in logged(capfd)


def test_an_open_orders_fill_listed_with_two_prices_fails_startup_naming_why(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # The code review, round 2: one trade ID listed at two prices was taken as no fill listed, and
    # start-up failed saying the activities did not list the order's fills.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    order = open_order_filled_one_of_two(alpaca, venue)
    at = datetime.now(UTC) - timedelta(seconds=5)
    listed = alpaca.fill_activity(order, str(uuid.UUID(int=61)), 1, "150", at, "partial_fill", 1, 1)
    venue.trading.activities = [listed, {**listed, "price": "151"}]
    node = build_node(trader(submit=False))

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(node)

    assert "a trade ID listed with two quantities, prices or times" in logged(capfd)


# -- the code review of the model (ADR 0075), round 3 ----------------------------------------------


def outside_order_partly_filled_long_ago(alpaca, venue) -> dict:
    """An open order the node does not hold, placed after start-up outside it, filled 1 of 3 ten minutes
    ago, its fill listed."""
    order = alpaca.alpaca_order(
        id="6c000000-0000-4000-8000-000000000001",
        client_order_id="outside-later",
        qty="3",
        status="partially_filled",
        filled_qty="1",
        filled_avg_price="150",
    )
    venue.trading.orders.append(order)
    venue.trading.activities.append(
        alpaca.fill_activity(
            order,
            str(uuid.UUID(int=71)),
            1,
            "150",
            datetime.now(UTC) - timedelta(minutes=10),
            "partial_fill",
            leaves_qty=2,
        )
    )
    return order


def test_a_fills_request_for_another_order_does_not_take_an_open_orders_old_fills(
    venue, alpaca
) -> None:
    # The code review, round 3: a fill reports request for one order took the open orders' fills an
    # earlier read had kept, so the bulk request after it went without them. Each request now reads the
    # open orders itself (round 4), so the bulk request after one for another order still reports them.
    reports: list = []

    async def body(client) -> None:
        outside_order_partly_filled_long_ago(alpaca, venue)
        other = await client._fill_reports(
            fills_command(venue_order_id=VenueOrderId(str(uuid.uuid4())))
        )
        start = dt_to_unix_nanos(datetime.now(UTC) - timedelta(minutes=1))
        reports.append((other, await client._fill_reports(fills_command(start=start))))

    run_with_client(body)

    ((other, bulk),) = reports
    assert other == []
    assert [r.trade_id.value for r in bulk] == [str(uuid.UUID(int=71))]


def test_an_outside_orders_old_fills_are_reported_only_while_it_is_open(venue, alpaca) -> None:
    # The code review, rounds 3 and 4: open-order fills kept from an earlier read reached later fill
    # reports after the order had closed. Each request now reads the open orders itself.
    reports: list = []

    async def body(client) -> None:
        order = outside_order_partly_filled_long_ago(alpaca, venue)
        start = dt_to_unix_nanos(datetime.now(UTC) - timedelta(minutes=1))
        reports.append(await client._fill_reports(fills_command(start=start)))
        order.update(status="canceled")
        reports.append(await client._fill_reports(fills_command(start=start)))

    run_with_client(body)

    assert [[r.trade_id.value for r in batch] for batch in reports] == [
        [str(uuid.UUID(int=71))],
        [],
    ]


def test_a_chain_walk_cut_short_on_one_read_is_walked_again(venue, alpaca, monkeypatch) -> None:
    # The code review, round 3: the check of an open order's fills kept its replace chain from the first
    # walk, so a walk a failed read cut short stayed short, and start-up failed although Alpaca lists
    # every fill.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    long_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    first = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000001",
        client_order_id="outside-2",
        status="replaced",
        replaced_by="9b000000-0000-4000-8000-000000000002",
        filled_qty="1",
        filled_avg_price="150",
        submitted_at=long_ago,
        created_at=long_ago,
    )
    middle = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-31",
        status="replaced",
        replaces=first["id"],
        replaced_by="9b000000-0000-4000-8000-000000000003",
        filled_qty="1",
        filled_avg_price="150",
        submitted_at=long_ago,
        created_at=long_ago,
    )
    working = alpaca.alpaca_order(
        id="9b000000-0000-4000-8000-000000000003",
        client_order_id="alpaca-made-32",
        status="partially_filled",
        replaces=middle["id"],
        filled_qty="1",
        filled_avg_price="150",
    )
    venue.trading.orders = [first, middle, working]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    ]
    venue.trading.activities = [
        alpaca.fill_activity(
            first,
            str(uuid.UUID(int=81)),
            1,
            "150",
            datetime.now(UTC) - timedelta(hours=2),
            "partial_fill",
            leaves_qty=1,
        )
    ]
    reads: list[str] = []
    failed: list[str] = []
    # The injected failure is scoped to the open-order check's own chain walk: a read is marked only
    # while `_open_order_fills` walks a chain (`_chain_ids` within it), and the first marked read of the
    # middle order fails. Start-up's order and fill report requests run concurrently and other paths read
    # that order too, so a count of every read would fail whichever path came second. The markers are
    # context variables: `_call` starts each read in a task (`asyncio.ensure_future`), which copies the
    # context, and `asyncio.to_thread` copies it on to the worker thread.
    checking = contextvars.ContextVar("checking", default=False)
    walking = contextvars.ContextVar("walking", default=False)
    check, walk = AlpacaExecutionClient._open_order_fills, AlpacaExecutionClient._chain_ids

    async def marked_check(self, command, chain):
        token = checking.set(True)
        try:
            return await check(self, command, chain)
        finally:
            checking.reset(token)

    async def marked_walk(self, venue_order_id):
        token = walking.set(True)
        try:
            return await walk(self, venue_order_id)
        finally:
            walking.reset(token)

    def once(order_id: str) -> Exception | None:
        if order_id == middle["id"] and checking.get() and walking.get():
            reads.append(order_id)
            if len(reads) == 1:
                failed.append(order_id)
                return OSError("connection reset")
        return None

    monkeypatch.setattr(AlpacaExecutionClient, "_open_order_fills", marked_check)
    monkeypatch.setattr(AlpacaExecutionClient, "_chain_ids", marked_walk)
    venue.trading.order_lookup_error = once
    node = build_node(trader(submit=False), exec_engine=SHORT_WINDOW)

    run(node)

    (order,) = [o for o in node.cache.orders() if o.client_order_id.value == "outside-2"]
    assert trade_ids(order) == [str(uuid.UUID(int=81))]
    # The check's walk was cut short once, and walked again.
    assert failed == [middle["id"]] and len(reads) >= 2, (failed, reads)


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


# -- what paper showed of a replace (ADR 0042, 2026-11-10 New York, overnight) ----------------------


def test_a_partly_filled_order_replaced_as_paper_shows_it_ends_filled_on_the_stream(venue) -> None:
    # Paper, 2 of 2 runs: one share of a buy of 2 fills, the order is replaced, and the answer is the
    # replacement `partially_filled`, with the order's `qty` and the fill before it; Alpaca then sends
    # `replaced` for the old order and the replacement's `fill`, with no `new` before it, its order's
    # `filled_qty` the chain's. The node's order keeps its quantity and ends filled with 2.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(prices=[])  # the test sends the modify, after the fill
    held: list = []

    async def partly_filled_replaced_filled(client) -> None:
        old = placed_order(venue, 0)
        venue.trading.update(
            old["id"], "partial_fill", 1, status="partially_filled", filled_qty="1"
        )
        await until_named(strategy, "OrderFilled")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        await client._modify_order(
            SimpleNamespace(
                client_order_id=strategy.client_order_id,
                venue_order_id=cached.venue_order_id,
                quantity=None,
                price=Price.from_str("150.50"),
                trigger_price=None,
            )
        )
        new = placed_order(venue, 1)
        held.append((new["status"], new["qty"], new["filled_qty"]))  # the answer, as paper gave it
        venue.trading.confirm_replace(old["id"])
        venue.trading.update(new["id"], "fill", 1, status="filled", filled_qty="2")
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.quantity.as_double(), cached.filled_qty.as_double()))

    run_modify(
        venue, strategy, lambda: "OrderAccepted" in strategy.names(), partly_filled_replaced_filled
    )

    assert held == [("partially_filled", "2", "1"), (OrderStatus.FILLED, 2.0, 2.0)]
    assert strategy.names()[-2:] == ["OrderUpdated", "OrderFilled"], strategy.names()


# -- Codex, outside round 1 on ADR 0075 ------------------------------------------------------------


@pytest.mark.parametrize("failing", ["once", "always"])
def test_an_outside_open_orders_fills_shown_whole_are_not_dropped_by_a_later_failed_read(
    venue, alpaca, monkeypatch, failing
) -> None:
    # Found by an independent reader (Codex, outside round 1 on ADR 0075, finding 1): an open order the
    # node does not hold had its fills shown all listed, and then the completeness filter read the order
    # again, failed once, and dropped them, so start-up inferred the fill under an ID of its own and a
    # later report counted the share again. Its fills are reported, or the request fails by name.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    reports: list = []

    async def body(client) -> None:
        order = outside_order_partly_filled_long_ago(alpaca, venue)
        lookups: list[str] = []

        def lookup(order_id: str) -> Exception | None:
            if order_id != order["id"]:
                return None
            lookups.append(order_id)
            if len(lookups) == 2 or (failing == "always" and len(lookups) > 1):
                return OSError("connection reset by peer")
            return None

        venue.trading.order_lookup_error = lookup
        start = dt_to_unix_nanos(datetime.now(UTC) - timedelta(minutes=1))
        try:
            reports.append(
                [r.trade_id.value for r in await client._fill_reports(fills_command(start=start))]
            )
        except ValueError as e:
            reports.append(str(e))

    run_with_client(body)

    # Its fills are judged by the one check; no later read of the order can leave them to inference.
    assert reports == [[str(uuid.UUID(int=71))]]


def hold_fill_retry(client, monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Gate only this client's later-read sleep; other loop users keep asyncio's real sleep.

    Contract: Python b703b2c5e704, asyncio/locks.py's Event; tasks.py's current_task.
    The caller releases the gate in finally and joins the actual retry task before checking it.
    """
    sleeping, release = asyncio.Event(), asyncio.Event()
    original = asyncio.sleep

    async def sleep(delay, result=None):
        if asyncio.current_task() in client._catching_up.values():
            sleeping.set()
            await release.wait()
            return result
        return await original(delay, result)

    monkeypatch.setattr(canon_alpaca.execution.asyncio, "sleep", sleep)
    return sleeping, release


@pytest.mark.parametrize(
    "fill",
    [
        "listed",
        "listed-later",
        "update-after",
        "update-during-read",
    ],
)
def test_a_cancel_showing_a_fill_the_node_lost_ends_the_order_with_it(
    venue, monkeypatch, capfd, fill
) -> None:
    # Found by an independent reader (Codex, outside round 1 on ADR 0075, finding 2): the stream lost a
    # partial fill and then delivered `canceled` showing it filled. The cancel ended the order and dropped
    # its record, so the fill was never brought, nor applied when its update came late: CANCELED with 0
    # filled where Alpaca shows 1. The fill is brought by its trade ID first, and the cancel follows it.
    #
    # Regression pinned by `update-during-read`: a later read that is OUT when the fill's own update
    # ends the order comes back, sees the order ended, and logs itself as the read that brought the
    # fill, which the stream brought. Stopping an ended order's later reads prevents that false credit.
    #
    # Gate both the retry sleep and its activity read. The stream ends update-after while the retry
    # sleeps, and update-during-read while its read is out. Both retries are joined before checking
    # their result and logs; neither a background exception nor a false catch-up can pass unnoticed.
    from .test_adapter_held_report_blind import StreamProbe
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until(lambda: "OrderAccepted" in strategy.names(), "the order was not accepted")
        stream = StreamProbe(client, monkeypatch)
        sleeping, wake = hold_fill_retry(client, monkeypatch)
        placed = placed_order(venue)
        reading, release = asyncio.Event(), asyncio.Event()
        read_activities = client._fill_activities
        reads = 0

        async def held_read(start, order_ids=None):
            nonlocal reads
            if start is not None or not order_ids or placed["id"] not in order_ids:
                return await read_activities(start, order_ids)  # not a catch-up read of this order
            reads += 1
            if reads > 1:
                reading.set()
                await release.wait()  # every bounded wait below releases this in finally
            return await read_activities(start, order_ids)

        monkeypatch.setattr(client, "_fill_activities", held_read)
        lost = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        listed = venue.trading.activities.pop() if fill != "listed" else None
        cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        try:
            await stream.send(venue, cancel)
            pending = client._catching_up.get(strategy.client_order_id)
            assert reads == 1
            if fill == "listed":
                assert pending is None
            else:
                assert pending is not None
                await until(sleeping.is_set, "the retry did not enter its sleep")
                if fill == "listed-later":
                    venue.trading.activities.append(listed)
                    wake.set()
                    release.set()
                else:
                    if fill == "update-during-read":
                        wake.set()
                        await until(reading.is_set, "the later activity read did not start")
                    await stream.send(venue, lost)
            await until(lambda: "OrderCanceled" in strategy.names(), "the order did not cancel")
        finally:
            release.set()
            wake.set()
        if pending is not None:
            await until(pending.done, "the later-read task did not finish")
            assert pending.result() is None  # re-raise every task failure, including cancellation
        assert strategy.client_order_id not in client._catching_up
        assert strategy.client_order_id not in client._catch_up_targets
        assert (
            reads
            == {"listed": 1, "listed-later": 2, "update-after": 1, "update-during-read": 2}[fill]
        )
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(lost["execution_id"])

    run_with_client(body, strategy)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 1.0, [execution_id]), strategy.names()
    assert strategy.names()[-2:] == ["OrderFilled", "OrderCanceled"], strategy.names()
    # The line a paper run counts (the lead, 2026-11-11): which read brought the fill.
    # One brought by its own late update logs no read.
    read = {"listed": 1, "listed-later": 2}
    prefix = f"Alpaca fill catch-up for {strategy.client_order_id}:"
    lines = [line for line in logged(capfd).splitlines() if prefix in line]
    if fill in read:
        assert len(lines) == 1, lines
        assert f"{prefix} caught up on read {read[fill]} of 3 (1 shown, 1 held)" in lines[0], lines
    else:
        assert lines == [], lines


@pytest.mark.parametrize("ending", ["missing", "stream", "engine"])
def test_a_fill_retry_waking_after_its_order_is_gone_does_not_read_or_log(
    venue, monkeypatch, ending
) -> None:
    # NautilusTrader d53849b216e2, live/clients.py: cache is the client's assigned view;
    # generate_order_canceled queues an engine event. Only the missing case substitutes that view,
    # without removing anything from the native cache. Stream and engine closure use real outputs.
    from .test_adapter_held_report_blind import LogProbe
    from .test_adapter_held_report_blind import StreamProbe
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    strategy = holding_strategy(venue)

    async def body(client) -> None:
        await until(lambda: "OrderAccepted" in strategy.names(), "the order was not accepted")
        client_order_id = strategy.client_order_id
        placed = placed_order(venue)
        venue_order_id = VenueOrderId(placed["id"])
        cache = client.cache
        stream = StreamProbe(client, monkeypatch)
        log = LogProbe(client._log)
        monkeypatch.setattr(client, "_log", log)
        sleeping, wake = hold_fill_retry(client, monkeypatch)
        reads: list = []

        async def read(*args, **kwargs):
            reads.append((args, kwargs))
            return False

        monkeypatch.setattr(client, "_catch_up", read)
        client._catch_up_later(client_order_id, Decimal(1), venue_order_id)
        pending = client._catching_up[client_order_id]
        try:
            await until(sleeping.is_set, "the retry did not enter its sleep")
            assert not pending.done()
            if ending == "missing":
                monkeypatch.setattr(client, "cache", SimpleNamespace(order=lambda _id: None))
            elif ending == "stream":
                cancel = venue.trading.update(
                    placed["id"], "canceled", deliver=False, status="canceled"
                )
                await stream.send(venue, cancel)
                assert client_order_id in client._finished
            else:
                client.generate_order_canceled(
                    cache.order(client_order_id), venue_order_id, client.clock.timestamp_ns()
                )
                assert client_order_id not in client._finished
            if ending != "missing":
                await until(
                    lambda: cache.order(client_order_id).is_closed,
                    "the ending event did not close the cache",
                )
            wake.set()
            await until(pending.done, "the retry did not finish after waking")
            assert pending.result() is None
            assert reads == [], "a retry read after its order was gone or closed"
            assert not [line for line in log.lines if "fill catch-up" in line], log.lines
            assert client_order_id not in client._catching_up
            assert client_order_id not in client._catch_up_targets
        finally:
            wake.set()
            monkeypatch.setattr(client, "cache", cache)
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif not pending.cancelled():
                pending.result()

    run_with_client(body, strategy)


@pytest.mark.parametrize("error", [RuntimeError, AssertionError, asyncio.CancelledError])
def test_a_later_read_that_raises_fails_the_cancel_case(venue, monkeypatch, capfd, error) -> None:
    # Python b703b2c5e704, asyncio/tasks.py: gather(return_exceptions=True) returns failures
    # as values. The case driving the real later() task must raise them, including cancellation.
    original = AlpacaExecutionClient._catch_up

    async def broken_read(self, order, *args, **kwargs):
        later = self._catching_up.get(order.client_order_id) is asyncio.current_task()
        result = await original(self, order, *args, **kwargs)
        if later:
            raise error("stand-in later read failed")
        return result

    monkeypatch.setattr(AlpacaExecutionClient, "_catch_up", broken_read)
    with pytest.raises(error, match="stand-in later read failed"):
        test_a_cancel_showing_a_fill_the_node_lost_ends_the_order_with_it(
            venue, monkeypatch, capfd, "update-during-read"
        )


@pytest.mark.parametrize(
    "source", ["query", "single", "bulk", "send", "send-with-fills", "open-check", "targeted-check"]
)
def test_an_older_canceled_report_is_held_back_once_the_client_has_ended_the_order(
    venue, monkeypatch, source
) -> None:
    # F1, xr7-r-l8-adapter: a stale cancel must not overtake the queued real fill and end.
    # Exercise every report consumer, including the native open-order check that can resolve a
    # successful empty answer as NOT_FOUND_AT_VENUE. Reuse the blind harness's output boundary.
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)
    checks = (
        LiveExecutionEngineConfig(
            reconciliation_startup_delay_secs=0,
            open_check_interval_secs=0.05,
            open_check_open_only=False,
            open_check_threshold_ms=0,
            open_check_missing_retries=1,
            single_order_query_delay_ms=0,
        )
        if source in {"open-check", "targeted-check"}
        else None
    )

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            case.placed["filled_qty"] = "0"
            task = None
            if source == "targeted-check":
                # A bulk response omits the order; the real targeted lookup must retain its held
                # snapshot rather than complete with None and let the engine reject it as missing.
                monkeypatch.setattr(venue.trading, "get_orders", lambda _request: [])
            if source in {"open-check", "targeted-check"}:
                await until(
                    lambda: bool(case.hold_lines()), "the native check did not hold the report"
                )
            elif source in {"send", "send-with-fills"}:
                task = asyncio.create_task(
                    client._send_report(
                        AlpacaOrder(**case.placed), [] if source == "send-with-fills" else None
                    )
                )
                case.tasks.append(task)
                await until(case.mapped.is_set, "the stream snapshot was not mapped")
            else:
                task = await case.query(source)
            if task is not None:
                assert not task.done(), "the held request completed before the queued fill and end"
            assert cached(client, strategy).status == OrderStatus.ACCEPTED
            assert not case.reports
            # Return hooks must refresh an older snapshot before mass-status reconciliation.
            case.placed["filled_qty"] = "1"
            await case.close_cache()
            if task is not None:
                async with asyncio.timeout(3):
                    result = await task
                if source in {"single", "send", "send-with-fills"}:
                    assert result is not None
                    assert result.client_order_id == strategy.client_order_id
                    if source == "single":
                        assert result.filled_qty.as_double() == 1
                elif source == "bulk":
                    assert any(r.client_order_id == strategy.client_order_id for r in result)
                    assert all(r.filled_qty.as_double() == 1 for r in result)
            await until(lambda: bool(case.release_lines()), "no named release after cache close")
            order = cached(client, strategy)
            assert order.status == OrderStatus.CANCELED
            assert trade_ids(order) == [case.first_fill["execution_id"]]
            assert strategy.names()[-2:] == ["OrderFilled", "OrderCanceled"]
            assert "OrderRejected" not in strategy.names()
        finally:
            await case.cleanup()

    run_with_client(body, strategy, exec_engine=checks)


@pytest.mark.parametrize("kind", ["single"])
def test_a_held_report_without_cache_close_errors_at_its_named_bound(
    venue, monkeypatch, kind
) -> None:
    # _finished is not proof the engine received an end. A held request must fail explicitly
    # when the cache never closes, rather than successfully returning None to a targeted query.
    # G2 gives bulk omission its own tests below; an empty bulk answer is safe there.
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05, raising=False)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            task = await case.query(kind, allow_deferred_error=True)
            with pytest.raises(RuntimeError, match=f"{strategy.client_order_id}.*timed out"):
                async with asyncio.timeout(3):
                    await task
            assert not cached(client, strategy).is_closed
            assert not case.reports
            assert len(case.hold_lines()) == len(case.release_lines()) == 1
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


async def single_leg_for_activity_deadline(venue_order_id):
    """Keep setup before the blocked activities read synchronous; chain deadlines have their own case."""
    return {venue_order_id.value}


async def ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch):
    """Lose both stream fills and all three activity reads, then restore the activity listing."""
    from .test_adapter_held_report_blind import LogProbe
    from .test_adapter_held_report_blind import StreamProbe
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import until

    await until(lambda: "OrderAccepted" in strategy.names(), "the order was not accepted")
    stream = StreamProbe(client, monkeypatch)
    log = LogProbe(client._log)
    monkeypatch.setattr(client, "_log", log)
    placed = placed_order(venue)
    first = venue.trading.update(
        placed["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
    )
    second = venue.trading.update(
        placed["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="2"
    )
    activities = list(venue.trading.activities)
    venue.trading.activities.clear()
    cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
    await stream.send(venue, cancel)
    await until(
        lambda: (
            cached(client, strategy).is_closed
            and strategy.client_order_id not in client._catching_up
        ),
        "the three empty reads did not end the order",
    )
    assert cached(client, strategy).filled_qty.as_double() == 0
    assert any("ran out after 3 reads" in line for line in log.lines)
    venue.trading.activities.extend(activities)
    return SimpleNamespace(
        stream=stream, log=log, placed=placed, first=first, second=second, activities=activities
    )


@pytest.mark.parametrize("source", ["stream", "send-with-fills"])
def test_a_late_snapshot_cannot_void_newer_listed_fills(venue, monkeypatch, source) -> None:
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        if source == "stream":
            await case.stream.send(venue, case.first)
        else:
            fills = [fill_report(a, client.clock.timestamp_ns()) for a in case.activities]
            await client._send_report(AlpacaOrder(**case.first["order"]), fills)
        await until(
            lambda: strategy.names().count("OrderFilled") == 2,
            "both listed fills must reach the engine despite the older snapshot",
        )
        order = cached(client, strategy)
        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty.as_double() == 2
        assert trade_ids(order) == [case.first["execution_id"], case.second["execution_id"]]
        assert "OrderFillVoided" not in strategy.names()
        await case.stream.send(venue, case.second)
        assert strategy.names().count("OrderFilled") == 2

    run_with_client(body, strategy)


def test_a_snapshot_ahead_of_activities_never_infers_the_missing_fill(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        venue.trading.activities.pop()
        await case.stream.send(venue, case.second)
        assert cached(client, strategy).filled_qty.as_double() == 0
        assert "OrderFilled" not in strategy.names()
        assert any("additional fills unread" in line for line in case.log.lines)
        venue.trading.activities.append(case.activities[1])
        await case.stream.send(venue, case.second)
        await until(lambda: cached(client, strategy).filled_qty.as_double() == 2, "fills lost")
        assert trade_ids(cached(client, strategy)) == [
            case.first["execution_id"],
            case.second["execution_id"],
        ]
        assert "OrderFillVoided" not in strategy.names()

    run_with_client(body, strategy)


def test_a_late_fill_the_activities_list_only_later_is_read_again_without_another_update(
    venue, monkeypatch
) -> None:
    """A finished order's update shows fills Alpaca's activities do not list yet, and the hold ends
    unread. No further update comes for an ended order, and the engine's open-order check reads open
    orders only by default, so nothing else would read it again: the client does, by itself and
    spaced, and brings the fills by their own trade IDs once Alpaca lists them (the third review's
    P2-1)."""
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        venue.trading.activities.clear()
        await case.stream.send(venue, case.second)
        assert cached(client, strategy).filled_qty.as_double() == 0
        assert any("additional fills unread" in line for line in case.log.lines)
        venue.trading.activities.extend(case.activities)  # Alpaca lists them now; no update follows
        await until(
            lambda: cached(client, strategy).filled_qty.as_double() == 2,
            "the fills Alpaca listed later were never read again",
        )
        order = cached(client, strategy)
        assert order.status == OrderStatus.CANCELED
        assert trade_ids(order) == [case.first["execution_id"], case.second["execution_id"]]
        assert "OrderFillVoided" not in strategy.names()
        await until(
            lambda: strategy.client_order_id not in client._report_rereads,
            "the timed re-read stayed armed after it settled the order",
        )
        assert strategy.names().count("OrderFilled") == 2

    run_with_client(body, strategy)


def test_a_timed_out_stream_hold_is_read_again_by_itself_after_its_spacing(
    venue, monkeypatch
) -> None:
    """A stream hold that times out leaves its retry to a later request, and the spacing refuses the
    update that follows within it; with no update after that, both fills were lost for the session.
    The client reads the order again itself once the spacing has passed (the third review's P2-2)."""
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 0.3)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        monkeypatch.setattr(client, "_chain_ids", single_leg_for_activity_deadline)
        entered, release = asyncio.Event(), asyncio.Event()
        original = client._fill_activities

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "_fill_activities", slow)
        await case.stream.send(venue, case.first)  # its activities read outlasts the hold's bound
        assert entered.is_set()
        assert any("held report" in x and "timed out" in x for x in case.log.lines)
        await case.stream.send(venue, case.second)  # refused inside the spacing, or timed out too
        assert cached(client, strategy).filled_qty.as_double() == 0
        # The re-read's own hold gets room: only the update's hold is meant to time out here, and a
        # loaded machine must not time the re-reads out as well.
        monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
        release.set()  # Alpaca answers again; no further update comes
        await until(
            lambda: cached(client, strategy).filled_qty.as_double() == 2,
            "a timed-out hold was never read again once its spacing had passed",
        )
        order = cached(client, strategy)
        assert order.status == OrderStatus.CANCELED
        assert trade_ids(order) == [case.first["execution_id"], case.second["execution_id"]]
        assert "OrderFillVoided" not in strategy.names()

    run_with_client(body, strategy)


@pytest.mark.parametrize("kind", ["os", "api"])
def test_a_finished_orders_update_whose_mapping_read_fails_is_read_again_too(
    venue, alpaca, monkeypatch, kind
) -> None:
    """A read for the snapshot's own mapping can fail before the hold settles anything, and that is
    no hold error: the update is named as failed, and the order is read again all the same."""
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 0.05)
    strategy = holding_strategy(venue)
    error = OSError("stand-in") if kind == "os" else alpaca.api_error(503, 50310000, "stand-in")

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        original = client._order_report
        failed = []

        async def once(*args, **kwargs):
            if not failed:
                failed.append(args)
                raise error
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "_order_report", once)
        await case.stream.send(venue, case.second)
        assert failed and cached(client, strategy).filled_qty.as_double() == 0
        await until(
            lambda: cached(client, strategy).filled_qty.as_double() == 2,
            "an update whose mapping read failed was never read again",
        )
        order = cached(client, strategy)
        assert order.status == OrderStatus.CANCELED
        assert trade_ids(order) == [case.first["execution_id"], case.second["execution_id"]]
        assert "OrderFillVoided" not in strategy.names()

    run_with_client(body, strategy)


def test_the_timed_re_reads_of_a_finished_order_end_after_their_count_and_say_so(
    venue, monkeypatch
) -> None:
    """The re-reads are bounded: `HELD_REPORT_REREADS` of them, then one error line that leaves the
    order to the next start-up. Nothing is inferred for the fills Alpaca never listed."""
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 0.01)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        venue.trading.activities.clear()
        reads = []
        original = client._order_status_report_by

        async def counted(*args, **kwargs):
            reads.append(args)
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "_order_status_report_by", counted)
        await case.stream.send(venue, case.second)
        assert strategy.client_order_id in client._report_rereads
        await until(
            lambda: strategy.client_order_id not in client._report_rereads,
            "the timed re-reads never ended",
        )
        assert len(reads) == canon_alpaca.execution.HELD_REPORT_REREADS
        left = [line for line in case.log.lines if "left to the next start-up" in line]
        assert len(left) == 1 and str(strategy.client_order_id) in left[0]
        assert cached(client, strategy).filled_qty.as_double() == 0
        assert "OrderFilled" not in strategy.names()
        # a later update arms them again: the count is per failed update, not for the session
        await case.stream.send(venue, case.second)
        assert strategy.client_order_id in client._report_rereads

    run_with_client(body, strategy)


def test_a_timed_re_read_still_waiting_is_canceled_at_disconnect(venue, monkeypatch) -> None:
    """The disconnect itself cancels a waiting re-read and awaits it: when it returns, the task has
    ended and the client keeps none, so no read of the old session can follow into the next."""
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        venue.trading.activities.clear()
        await case.stream.send(venue, case.second)
        waiting = client._report_rereads[strategy.client_order_id]
        assert not waiting.done()
        await client._disconnect_venue()
        assert waiting.cancelled(), "the disconnect returned with a re-read still waiting"
        assert client._report_rereads == {}

    run_with_client(body, strategy)


@pytest.mark.parametrize("source", ["query", "single", "bulk", "send", "send-with-fills"])
def test_a_finished_report_is_delivered_only_once(venue, monkeypatch, source) -> None:
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        reports = []
        original = client._handle_report

        def delivered(report, fills=None):
            if isinstance(report, canon_alpaca.execution.OrderStatusReport):
                reports.append(report)
            original(report, fills)

        monkeypatch.setattr(client, "_handle_report", delivered)
        command = SimpleNamespace(
            client_order_id=strategy.client_order_id,
            venue_order_id=VenueOrderId(case.placed["id"]),
            instrument_id=None,
            start=None,
            end=None,
            open_only=False,
        )
        if source in {"send", "send-with-fills"}:
            fills = (
                [fill_report(a, client.clock.timestamp_ns()) for a in case.activities]
                if source == "send-with-fills"
                else None
            )
            await client._send_report(AlpacaOrder(**case.placed), fills)
        elif source == "query":
            await client._query_order(command)
        elif source == "single":
            reports.append(await client._generate_order_status_report(command))
        else:
            reports.extend(await client._generate_order_status_reports(command))
        assert len(reports) == 1, "a bundled report must not be delivered again by its caller"

    run_with_client(body, strategy)


def test_a_timed_out_hold_does_not_fail_other_bulk_reports(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            other = dict(
                case.placed,
                id=str(uuid.UUID(int=818)),
                client_order_id="OTHER-OPEN",
                status="new",
                filled_qty="0",
            )
            venue.trading.orders.append(other)
            result = await client._generate_order_status_reports(case.command())
            assert [r.client_order_id.value for r in result] == ["OTHER-OPEN"]
            assert len(case.release_lines()) == 1
            assert "timed out" in case.release_lines()[0]
            assert str(strategy.client_order_id) in case.release_lines()[0]
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


def test_an_unchanged_timed_out_hold_fails_fast_until_cache_progress(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            for _ in range(2):
                with pytest.raises(canon_alpaca.execution.HeldReportError):
                    await client._generate_order_status_report(case.command())
            assert len(case.hold_lines()) == 1, "unchanged cache must not start a second hold"
            assert len(case.release_lines()) == 1
            await case.close_cache()
            report = await client._generate_order_status_report(case.command())
            assert report is not None
            assert report.client_order_id == strategy.client_order_id
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


def test_a_query_logs_its_hold_timeout_without_raising_to_the_command_worker(venue, monkeypatch):
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            async with asyncio.timeout(3):
                await client._query_order(case.command())
            assert len(case.hold_lines()) == len(case.release_lines()) == 1
            assert not case.reports
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


def test_disconnect_clears_timed_out_report_state(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)
    clients = []

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            with pytest.raises(canon_alpaca.execution.HeldReportError):
                await client._generate_order_status_report(case.command())
            assert strategy.client_order_id in client._report_hold_failures
            clients.append(client)
        finally:
            await case.cleanup()

    run_with_client(body, strategy)
    assert clients[0]._report_hold_failures == {}


@pytest.mark.parametrize("stage", ["activities", "chain", "root", "earlier-filled"])
def test_the_whole_stream_hold_including_rest_reads_has_one_deadline(
    venue, monkeypatch, stage
) -> None:
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        method = {
            "activities": "_fill_activities",
            "chain": "_chain_ids",
            "root": "_root_id",
            "earlier-filled": "_earlier_filled",
        }[stage]
        original = getattr(client, method)
        if stage == "activities":
            monkeypatch.setattr(client, "_chain_ids", single_leg_for_activity_deadline)

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, method, slow)
        snapshot = dict(case.second["order"])
        if stage == "earlier-filled":
            monkeypatch.setattr(canon_alpaca.execution, "FILLED_READING", FilledReading.OWN)
            snapshot["replaces"] = str(uuid.UUID(int=819))
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(client._send_report(AlpacaOrder(**snapshot)))
        try:
            await until(entered.is_set, "the selected REST stage was not reached")
            # The test owns the deadline; two seconds of scheduler room is not a production SLA.
            bound = canon_alpaca.execution.HELD_REPORT_TIMEOUT_SECS
            await until(task.done, "a REST read escaped the whole hold deadline", bound + 2.0)
            with pytest.raises(canon_alpaca.execution.HeldReportError, match="timed out"):
                task.result()
            assert asyncio.get_running_loop().time() - started <= bound + 2.0
            assert sum("held report" in x and "timed out" in x for x in case.log.lines) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run_with_client(body, strategy)


def test_another_orders_stream_update_is_applied_within_the_hold_bound(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        other = strategy.order_factory.limit(
            AAPL, OrderSide.BUY, Quantity.from_int(4), Price.from_str("150.00")
        )
        strategy.submit_order(other)
        await until(
            lambda: (
                client.cache.order(other.client_order_id) is not None
                and client.cache.order(other.client_order_id).status == OrderStatus.ACCEPTED
            ),
            "second order was not accepted",
        )
        second_placed = placed_order(venue, 1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = client._fill_activities

        async def slow(start, order_ids=None):
            if order_ids and case.placed["id"] in order_ids:
                entered.set()
                await release.wait()
            return await original(start, order_ids)

        monkeypatch.setattr(client, "_fill_activities", slow)
        monkeypatch.setattr(client, "_chain_ids", single_leg_for_activity_deadline)
        try:
            case.stream.post(venue, case.second)
            await until(entered.is_set, "held order never started its activities read")
            start = asyncio.get_running_loop().time()
            venue.trading.update(
                second_placed["id"], "partial_fill", 1, status="partially_filled", filled_qty="1"
            )
            bound = canon_alpaca.execution.HELD_REPORT_TIMEOUT_SECS
            await until(
                lambda: client.cache.order(other.client_order_id).filled_qty.as_double() == 1,
                "another order was blocked beyond the whole hold bound",
                bound + 2.0,
            )
            assert asyncio.get_running_loop().time() - start <= bound + 2.0
        finally:
            release.set()

    run_with_client(body, strategy)


def test_a_report_cannot_reach_the_engine_before_its_queued_end(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            extra = venue.trading.update(
                case.placed["id"],
                "partial_fill",
                1,
                deliver=False,
                status="partially_filled",
                filled_qty="2",
            )
            task = await case.query("query")
            await until(lambda: bool(case.hold_lines()) or task.done(), "request never settled")
            assert not case.reports, "report overtook the generated fill and cancel"
            assert cached(client, strategy).status == OrderStatus.ACCEPTED
            await case.close_cache()
            await task
            assert len(case.reports) == 1 and case.reports[0][1]
            await until(
                lambda: cached(client, strategy).filled_qty.as_double() == 2,
                "the late fill did not reach the cache",
            )
            assert trade_ids(cached(client, strategy)) == [
                case.first_fill["execution_id"],
                extra["execution_id"],
            ]
            assert cached(client, strategy).status == OrderStatus.CANCELED
            assert "OrderFillVoided" not in strategy.names()
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("source", ["stream", "query", "single", "bulk"])
def test_a_closed_order_retries_its_timed_out_fill_read_after_spacing(
    venue, monkeypatch, source
) -> None:
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 30.0, raising=False)
    now = [100.0]
    monkeypatch.setattr(canon_alpaca.execution, "monotonic", lambda: now[0], raising=False)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        original = client._fill_activities
        reads = 0

        async def slow_once(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                await asyncio.Event().wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "_fill_activities", slow_once)
        monkeypatch.setattr(client, "_chain_ids", single_leg_for_activity_deadline)
        await case.stream.send(venue, case.first)
        assert reads == 1 and cached(client, strategy).filled_qty.as_double() == 0
        assert any("timed out" in line for line in case.log.lines)
        now[0] += 31.0  # advance only the retry clock, never the asyncio scheduler
        monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
        command = SimpleNamespace(
            client_order_id=strategy.client_order_id,
            venue_order_id=VenueOrderId(case.placed["id"]),
            instrument_id=None,
            start=None,
            end=None,
            open_only=False,
        )
        if source == "stream":
            await case.stream.send(venue, case.first)
        elif source == "query":
            await client._query_order(command)
        elif source == "single":
            assert await client._generate_order_status_report(command) is not None
        else:
            assert len(await client._generate_order_status_reports(command)) == 1
        await until(
            lambda: cached(client, strategy).filled_qty.as_double() == 2, "retry lost fills"
        )
        assert reads == 2
        assert cached(client, strategy).status == OrderStatus.CANCELED
        assert trade_ids(cached(client, strategy)) == [
            case.first["execution_id"],
            case.second["execution_id"],
        ]
        assert "OrderFillVoided" not in strategy.names()

    run_with_client(body, strategy)


def test_a_burst_after_timeout_costs_one_attempt_and_one_named_omission(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 30.0, raising=False)
    monkeypatch.setattr(canon_alpaca.execution, "monotonic", lambda: 100.0, raising=False)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        reads = 0

        async def slow(*args, **kwargs):
            nonlocal reads
            reads += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_fill_activities", slow)
        monkeypatch.setattr(client, "_chain_ids", single_leg_for_activity_deadline)
        case.log.lines.clear()
        for _ in range(5):
            await case.stream.send(venue, case.first)
        assert reads == 1
        # Timeout itself names the omission and spacing; repeated refusals share that one line.
        assert len(case.log.lines) == 1, case.log.lines
        assert str(strategy.client_order_id) in case.log.lines[0]
        assert "HELD_REPORT_RETRY_SECS" in case.log.lines[0]

    run_with_client(body, strategy)


@pytest.mark.parametrize("other", ["absent", "catching-up"])
def test_bulk_returns_empty_when_every_candidate_is_held(venue, monkeypatch, other) -> None:
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_RETRY_SECS", 30.0, raising=False)
    now = [100.0]
    monkeypatch.setattr(canon_alpaca.execution, "monotonic", lambda: now[0], raising=False)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            if other == "catching-up":
                second = strategy.order_factory.limit(
                    AAPL, OrderSide.BUY, Quantity.from_int(4), Price.from_str("150.00")
                )
                strategy.submit_order(second)
                await until_idle(client, venue)
                placed = placed_order(venue, 1)
                venue.trading.update(
                    placed["id"],
                    "partial_fill",
                    1,
                    deliver=False,
                    status="partially_filled",
                    filled_qty="1",
                )
                venue.trading.activities.pop()
            assert await client._generate_order_status_reports(case.command()) == []
            case.log.lines.clear()
            now[0] += 31.0
            for _ in range(3):
                assert await client._generate_order_status_reports(case.command()) == []
            omissions = [
                line for line in case.named_lines() if "omitted" in line and "report" in line
            ]
            assert len(omissions) == 1, case.named_lines()
            assert "HELD_REPORT_RETRY_SECS" in omissions[0]
            assert not case.reports
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("failure", ["hold", "mapping"])
def test_a_refused_submit_consumes_its_report_error_once(venue, alpaca, monkeypatch, failure):
    from .test_adapter_held_report_blind import LogProbe
    from .test_adapter_held_report_blind import until

    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=4)
    strategy.stopped_for = "test owns shutdown"
    refusal = alpaca.api_error(422, 42210000, "client_order_id must be unique")
    venue.trading.submit_error = refusal
    reported = []
    log = []
    original = AlpacaExecutionClient._send_report

    async def report_error(client, held, fills=None):
        probe = LogProbe(client._log)
        log.append(probe)
        monkeypatch.setattr(client, "_log", probe)
        try:
            if failure == "hold":
                # A hold logs its own error before raising; its caller must consume it.
                probe.error(f"Alpaca held report for {held.client_order_id} timed out")
                raise canon_alpaca.execution.HeldReportError("already logged")
            raise alpaca.api_error(500, 50010000, "offline mapping read failed")
        finally:
            reported.append(held)

    monkeypatch.setattr(AlpacaExecutionClient, "_send_report", report_error)

    # Let the real refused-submit path find a venue order without an acceptance update.
    def held_order(client_id):
        return AlpacaOrder(**alpaca.alpaca_order(client_order_id=client_id, status="canceled"))

    monkeypatch.setattr(venue.trading, "get_order_by_client_id", held_order)

    async def body(client) -> None:
        await until(lambda: bool(reported), "refused submit did not attempt its report")
        # Exercise the hook directly too: no runtime may swallow the escaped exception for this check.
        monkeypatch.setattr(AlpacaExecutionClient, "_send_report", original)
        order = client.cache.order(strategy.client_order_id)
        client._accepted.discard(strategy.client_order_id)
        monkeypatch.setattr(client, "_send_report", lambda held: report_error(client, held))
        await client._refused(order, refusal)
        assert len(log[-1].lines) == 1, log[-1].lines

    run_with_client(body, strategy)


def test_a_delivered_report_has_no_post_delivery_timeout(venue, monkeypatch) -> None:
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_with_two_unlisted_fills(client, strategy, venue, monkeypatch)
        original = client._wait_report_cache
        waits = 0

        async def wait(*args):
            nonlocal waits
            waits += 1
            if waits > 1:
                # Deterministically expire only the post-delivery wait, regardless of REST latency.
                raise TimeoutError
            return await original(*args)

        monkeypatch.setattr(client, "_wait_report_cache", wait)
        case.log.lines.clear()
        async with asyncio.timeout(3):
            assert await client._send_report(AlpacaOrder(**case.second["order"])) is not None
        assert not client._report_hold_failures
        assert not any("failed" in line for line in case.log.lines)

    run_with_client(body, strategy)


def test_a_report_hold_ending_after_disconnect_cannot_restore_failure_state(venue, monkeypatch):
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def finish_after_disconnect(*args):
            entered.set()
            await release.wait()
            raise TimeoutError

        monkeypatch.setattr(client, "_wait_report_cache", finish_after_disconnect)
        task = asyncio.create_task(client._generate_order_status_report(case.command()))
        case.tasks.append(task)
        try:
            await until(entered.is_set, "hold did not reach its cache wait")
            await client._disconnect_venue()
            assert not client._report_hold_failures
            release.set()
            with pytest.raises(canon_alpaca.execution.HeldReportError):
                await task
            assert not client._report_hold_failures
            assert not case.reports
        finally:
            release.set()
            await case.cleanup()

    run_with_client(body, strategy)


def test_a_mapping_hold_ending_after_disconnect_cannot_repopulate_indexes(venue, monkeypatch):
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original = client._call
        prior_id = str(uuid.UUID(int=821))

        async def read_prior(method, *args, **kwargs):
            if args == (prior_id,):
                entered.set()
                await release.wait()
                return AlpacaOrder(**case.placed)
            return await original(method, *args, **kwargs)

        monkeypatch.setattr(client, "_call", read_prior)
        snapshot = AlpacaOrder(**dict(case.placed, id=str(uuid.UUID(int=822)), replaces=prior_id))
        task = asyncio.create_task(client._send_report(snapshot))
        case.tasks.append(task)
        try:
            await until(entered.is_set, "mapping did not start its prior-order read")
            await client._disconnect_venue()
            assert not client._index
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert not client._index, "a late mapping repopulated disconnected state"
            assert not client._report_hold_failures
            assert not case.reports
        finally:
            release.set()
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("status", ["new", "canceled", "partially_filled"])
def test_a_finished_update_without_new_shares_is_not_forwarded(venue, monkeypatch, status):
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            await case.close_cache()
            raw = dict(case.cancel)
            raw["order"] = dict(case.placed, status=status)
            case.log.lines.clear()
            await case.stream.send(venue, raw)
            assert not case.reports
            assert not case.log.lines, "a hold that never waits must not emit INFO"
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("source", ["single", "bulk"])
def test_a_returned_finished_snapshot_cannot_decrease_cached_fills(venue, monkeypatch, source):
    from .test_adapter_held_report_blind import cached
    from .test_adapter_held_report_blind import ended_ahead_of_cache
    from .test_adapter_held_report_blind import holding_strategy

    monkeypatch.setattr(canon_alpaca.execution, "HELD_REPORT_TIMEOUT_SECS", 30.0)
    strategy = holding_strategy(venue)

    async def body(client) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            await case.close_cache()
            case.placed["filled_qty"] = "0"
            if source == "single":
                with pytest.raises(canon_alpaca.execution.HeldReportError):
                    await client._generate_order_status_report(case.command())
            else:
                assert await client._generate_order_status_reports(case.command()) == []
            assert cached(client, strategy).filled_qty.as_double() == 1
            assert not case.reports
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


def test_a_cancel_whose_lost_fill_is_never_listed_ends_with_an_error_naming_the_shares(
    venue, monkeypatch, capfd
) -> None:
    # ADR 0075's recorded limit, on the lead's terms (2026-11-11): when the reads run out, the order ends
    # with what the node holds, and an ERROR names the order, the shares Alpaca shows and those held.
    from .test_adapter_held_report_blind import StreamProbe
    from .test_adapter_held_report_blind import until

    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.0)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until(lambda: "OrderAccepted" in strategy.names(), "the order was not accepted")
        stream = StreamProbe(client, monkeypatch)
        sleeping, wake = hold_fill_retry(client, monkeypatch)
        placed = placed_order(venue)
        venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        venue.trading.activities.pop()  # never listed
        cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        try:
            await stream.send(venue, cancel)
            pending = client._catching_up.get(strategy.client_order_id)
            assert pending is not None
            await until(sleeping.is_set, "the retry did not enter its sleep")
        finally:
            wake.set()
        await until(pending.done, "the three empty reads did not finish")
        assert pending.result() is None
        await until(lambda: "OrderCanceled" in strategy.names(), "the order did not cancel")
        assert strategy.client_order_id not in client._catching_up
        assert strategy.client_order_id not in client._catch_up_targets
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_with_client(body, strategy)

    assert held == [(OrderStatus.CANCELED, 0.0)], strategy.names()
    error = (
        f"AlpacaExecutionClient: Alpaca fill catch-up for {strategy.client_order_id}: ran out after 3 "
        "reads (1 shown, 0 held); the order ends canceled with what the node holds"
    )
    # The node's logger is set up once per process, under the first node's trader ID, so the line is
    # matched from its level on.
    prefix = f"Alpaca fill catch-up for {strategy.client_order_id}:"
    lines = [line for line in logged(capfd).splitlines() if prefix in line]
    assert len(lines) == 1, lines
    assert error in lines[0] and "[ERROR] " in lines[0], lines


@pytest.mark.parametrize("activities", ["listed", "listed-later"])
def test_a_canceled_query_report_held_for_its_fill_ends_the_order(
    venue, monkeypatch, activities
) -> None:
    # Found by an independent reader (Codex, outside round 1 on ADR 0075, finding 3): the stream lost a
    # partial fill and the cancel after it; the engine's `QueryOrder` read the order canceled with 1
    # filled, the client brought the fill and held the report back for the engine to ask again, and
    # under the paper node's checks, which read open orders only, nothing asked: PARTIALLY_FILLED where
    # Alpaca shows the order canceled. The client ends the order after its fills.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.5)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        lost = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        listed = venue.trading.activities.pop() if activities != "listed" else None
        venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        await query(client, strategy, placed)
        await settled(client, venue)
        if listed is not None:
            venue.trading.activities.append(listed)
        await asyncio.sleep(1.0)  # open-order and position checks
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(lost["execution_id"])

    run_with_client(body, strategy, exec_engine=PAPER_CHECKS)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 1.0, [execution_id]), strategy.names()
    assert strategy.names()[-2:] == ["OrderFilled", "OrderCanceled"], strategy.names()


MIDDLE_CHAIN = [f"7d000000-0000-4000-8000-00000000000{n}" for n in range(1, 4)]


@pytest.mark.parametrize("copy", ["as-filled", "as-replaced"])
def test_a_fill_on_the_middle_order_of_a_chain_the_node_never_held_keeps_the_working_order(
    venue, alpaca, copy
) -> None:
    # Found by an independent reader (Muse, outside round 1 on ADR 0075, finding 2): an outside chain of
    # three orders reconciled at start-up holds only the working one; a fill on the middle order, which
    # has a `replaces` link of its own, was taken for a replacement taking over, moved the node's order
    # back to that replaced order, and the working order's cancel was then ignored. An order Alpaca shows
    # replaced is never adopted; its fill is applied under the order the node's order is on now.
    first_id, middle_id, working_id = MIDDLE_CHAIN
    first = alpaca.alpaca_order(
        id=first_id,
        client_order_id="outside-mid",
        qty="3",
        status="replaced",
        replaced_by=middle_id,
    )
    middle = alpaca.alpaca_order(
        id=middle_id,
        client_order_id="alpaca-made-31",
        qty="3",
        status="replaced",
        replaces=first_id,
        replaced_by=working_id,
    )
    working = alpaca.alpaca_order(
        id=working_id, client_order_id="alpaca-made-32", qty="3", status="new", replaces=middle_id
    )
    venue.trading.orders = [first, middle, working]
    node_order = ClientOrderId("outside-mid")
    held: list = []

    async def body(client) -> None:
        execution_id = str(uuid.UUID(int=81))
        shown = (
            {**middle, "status": "partially_filled", "replaced_by": None, "filled_qty": "1"}
            if copy == "as-filled"
            else {**middle, "filled_qty": "1"}
        )
        middle["filled_qty"] = "1"
        working.update(filled_qty="1", status="partially_filled")
        venue.trading.activities.append(
            alpaca.fill_activity(
                middle, execution_id, 1, "150", datetime.now(UTC), "partial_fill", 1, 2
            )
        )
        venue.stream.send(
            {
                "event": "partial_fill",
                "order": shown,
                "timestamp": datetime.now(UTC).isoformat(),
                "price": "150",
                "qty": "1",
                "position_qty": "1",
                "execution_id": execution_id,
            }
        )
        await settled(client, venue)
        cached = client.cache.order(node_order)
        held.append((cached.venue_order_id.value, cached.filled_qty.as_double()))
        working.update(status="canceled")
        venue.stream.send(
            {
                "event": "canceled",
                "order": dict(working),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await settled(client, venue)
        held.append(client.cache.order(node_order).status)

    run_with_client(body)

    assert held == [(working_id, 1.0), OrderStatus.CANCELED]


@pytest.mark.parametrize("cum", ["absent", "own"])
def test_an_outside_open_chains_fills_are_reported_whatever_its_newest_activity_counts(
    venue, alpaca, cum
) -> None:
    # Found by an independent reader (Muse, outside round 1 on ADR 0075, finding 3): an outside open
    # replacement whose chain's fills Alpaca lists in full had them all left to the engine to infer when
    # its newest activity had no `cum_qty`, or counted its own fills only, so start-up neither reported
    # them by trade ID nor failed. With Codex's finding 1, one family: an open order the node does not
    # hold has its fills reported by trade ID, or start-up fails by name.
    first_id, _, working_id = MIDDLE_CHAIN
    first = alpaca.alpaca_order(
        id=first_id,
        client_order_id="outside-cum",
        qty="3",
        status="replaced",
        replaced_by=working_id,
        filled_qty="1",
        filled_avg_price="150",
    )
    working = alpaca.alpaca_order(
        id=working_id,
        client_order_id="alpaca-made-33",
        qty="3",
        status="partially_filled",
        replaces=first_id,
        filled_qty="2",
        filled_avg_price="150",
    )
    venue.trading.orders = [first, working]
    venue.trading.positions = [
        {**alpaca.POSITION_AAPL, "qty": "2", "qty_available": "2", "avg_entry_price": "150"}
    ]
    newest = alpaca.fill_activity(
        working, str(uuid.UUID(int=92)), 1, "150", datetime.now(UTC), "partial_fill", 2, 1
    )
    if cum == "absent":
        del newest["cum_qty"]
    else:
        newest["cum_qty"] = "1"
    venue.trading.activities = [
        newest,
        alpaca.fill_activity(
            first,
            str(uuid.UUID(int=91)),
            1,
            "150",
            datetime.now(UTC) - timedelta(seconds=30),
            "partial_fill",
            1,
            2,
        ),
    ]
    held: list = []

    async def body(client) -> None:
        cached = client.cache.order(ClientOrderId("outside-cum"))
        held.append((cached.filled_qty.as_double(), sorted(trade_ids(cached))))

    run_with_client(body)

    assert held == [(2.0, [str(uuid.UUID(int=91)), str(uuid.UUID(int=92))])]


def test_fills_inside_a_window_whose_end_cuts_off_a_later_fill_are_reported(venue, alpaca) -> None:
    # Found by an independent reader (Antigravity, an upstream-conformance read of ADR 0075, item 6),
    # next to Codex's finding 1: the window's end was applied before the check that an order's listed
    # fills are all of them, so an order filled again after the end, as the engine's position check asks
    # (`plan_position_fill_reports` sets an end), had its fills inside the window dropped. The check
    # judges every listed fill, and the end is applied after it.
    order = alpaca.alpaca_order(
        id="6e000000-0000-4000-8000-000000000001",
        client_order_id="outside-ended",
        qty="2",
        status="filled",
        filled_qty="2",
        filled_avg_price="150",
    )
    now = datetime.now(UTC)
    venue.trading.orders = [order]
    reports: list = []

    async def body(client) -> None:
        venue.trading.activities[:] = [
            alpaca.fill_activity(
                order, str(uuid.UUID(int=102)), 1, "150", now - timedelta(seconds=5), "fill", 2, 0
            ),
            alpaca.fill_activity(
                order,
                str(uuid.UUID(int=101)),
                1,
                "150",
                now - timedelta(seconds=30),
                "partial_fill",
                1,
                1,
            ),
        ]
        command = fills_command(
            start=dt_to_unix_nanos(now - timedelta(minutes=1)),
            end=dt_to_unix_nanos(now - timedelta(seconds=15)),
        )
        reports.append([r.trade_id.value for r in await client._fill_reports(command)])

    run_with_client(body)

    assert reports == [[str(uuid.UUID(int=101))]]


# -- the code review of the round-1 fixes (ADR 0075) -----------------------------------------------


def test_a_fill_on_a_raised_replacement_before_its_replaced_counts_against_its_quantity(
    venue,
) -> None:
    # The code review of the round-1 fixes, finding 1: a fill on a replacement not yet adopted, whose read
    # failed, was applied without adopting it, so a modify raising the quantity from 2 to 4 still had the
    # record's quantity at 2, and a 2-share fill ended the order FILLED while Alpaca works 2 more. A
    # replacement whose `replaces` reaches the order the record is on is adopted first, with no read.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(field="quantity", prices=["4"])
    held: list = []

    async def fill(client) -> None:
        new = placed_order(venue, 1)
        venue.trading.order_lookup_error = lambda order_id: (
            OSError("connection reset by peer") if order_id == new["id"] else None
        )
        venue.trading.update(
            new["id"], "partial_fill", 2, status="partially_filled", filled_qty="2"
        )
        async with asyncio.timeout(10):
            while "OrderFilled" not in strategy.names():
                await asyncio.sleep(0.01)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.quantity.as_double(), cached.filled_qty.as_double()))

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), fill)

    assert held == [(OrderStatus.PARTIALLY_FILLED, 4.0, 2.0)], strategy.names()


def test_a_cancel_waiting_for_its_fill_across_a_reconnect_still_ends_the_order(
    venue, monkeypatch
) -> None:
    # The code review of the round-1 fixes, finding 5: a disconnect dropped the record of an order whose
    # cancel was waiting for a fill the activities did not list yet, so the order never ended: Alpaca no
    # longer lists it open, and the paper node's checks never ask again. The end is resumed at the
    # next connect.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.5)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        lost = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        listed = venue.trading.activities.pop()
        venue.trading.update(placed["id"], "canceled", status="canceled")
        await settled(client, venue)
        await reconnect(client, venue)
        venue.trading.activities.append(listed)
        await asyncio.sleep(1.5)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(lost["execution_id"])

    run_with_client(body, strategy)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 1.0, [execution_id]), strategy.names()


def test_an_outside_order_pending_replace_has_its_fills_reported_on_the_order_reported(
    venue, alpaca
) -> None:
    # The code review of the round-1 fixes, finding 6: an outside order pending replace and its new
    # replacement, both open, the replacement read first: the chain's fills were moved to the
    # replacement, while the order reports name the order pending replace.
    long_ago = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    pending = alpaca.alpaca_order(
        id="6f000000-0000-4000-8000-000000000001",
        client_order_id="outside-pending",
        qty="3",
        status="pending_replace",
        filled_qty="1",
        filled_avg_price="150",
        submitted_at=long_ago,
        created_at=long_ago,
    )
    replacement = alpaca.alpaca_order(
        id="6f000000-0000-4000-8000-000000000002",
        client_order_id="alpaca-made-61",
        qty="3",
        status="new",
        replaces=pending["id"],
        filled_qty="1",
        filled_avg_price="150",
    )
    reports: list = []

    async def body(client) -> None:
        venue.trading.orders[:] = [pending, replacement]
        venue.trading.activities[:] = [
            alpaca.fill_activity(
                pending, str(uuid.UUID(int=111)), 1, "150", datetime.now(UTC), "partial_fill", 1, 2
            )
        ]
        named = [
            r.venue_order_id.value
            for r in await client._order_status_reports(
                SimpleNamespace(
                    instrument_id=None, start=None, end=None, open_only=True, log_receipt_level=None
                )
            )
        ]
        fills = [r.venue_order_id.value for r in await client._fill_reports(fills_command())]
        reports.append((named, fills))

    run_with_client(body)

    ((named, fills),) = reports
    assert fills == [pending["id"]] and pending["id"] in named, (named, fills)


# -- a submit never answered that Alpaca fills (the session window's claim 10, ADR 0044, ADR 0075) ----

LOST_SUBMIT_CHECKS: dict[str, Any] = {
    "reconciliation_startup_delay_secs": 0,
    "inflight_check_interval_ms": 100,
    "inflight_check_threshold_ms": 200,
    "open_check_interval_secs": 0.25,
    "open_check_threshold_ms": 200,
    "position_check_interval_secs": 0.4,
    "position_check_threshold_ms": 500,
}
"""The engine's in-flight check (`FAST_INFLIGHT`) and the paper node's open-order and position checks
(`PAPER_CHECKS`), with the position check's grace after an order's last activity (one minute by default,
`position_check_threshold_ns` in `crates/live/src/execution/config.rs` at the pin) cut to half a second,
so a test sees the engine give up on the submit and then book what Alpaca shows."""


class Claimant(Strategy):
    """Submits nothing itself, records its order events and reads its own position, as `SessionMaCross`
    does (`held`, by strategy ID); built with a `StrategyConfig`, so it can claim its instrument."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        super().__init__(config)
        self.started = False
        self.events: list = []

    def on_start(self) -> None:
        self.started = True

    def on_order_event(self, event) -> None:
        if type(event).__name__ != "OrderInitialized":
            self.events.append(event)

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]

    def held(self) -> Decimal:
        return sum(
            (
                p.signed_qty
                for p in self.cache.positions_open(instrument_id=AAPL, strategy_id=self.strategy_id)
            ),
            Decimal(0),
        )


EXHAUSTED = "Submission recovery exhausted for {}"
"""What the engine logs when it retains a submit its in-flight check has given up on
(`exhaust_submission`, `crates/live/src/execution/manager.rs` at the pin)."""


def lost_submit_filled(
    venue,
    alpaca,
    monkeypatch,
    capfd,
    *,
    side: str,
    delivered: bool,
    retain: bool,
    claim: bool,
    took: bool = True,
    order_unread: bool = False,
    unread_reads: int = 0,
    trace_unread_reads: int = 0,
) -> dict:
    """One limit order of one share whose submit Alpaca takes and answers 500, every read of the order
    failing meanwhile (by client order ID, by ID, and the open orders), until the engine's in-flight
    check has run out of queries; then Alpaca fills it, shows the position it made, and its reads are
    answered again. `side` is `entry` (a buy from flat) or `close` (a sell of the one share a filled buy
    left). With `delivered` the fill's trade update reaches the client; without, the stream lost it.
    `retain` and `claim` are the two upstream settings: the engine's `RETAIN_UNRESOLVED` and the
    strategy's claim on the instrument. Without `took`, Alpaca never placed the order, and nothing fills.
    With `order_unread`, Alpaca's order can still not be read by its ID once the fill is made, so the fill
    report cannot be named. With `trace_unread_reads`, it fills on a replacement whose ancestor read
    fails that many times before answering. Returns what the node holds once it has settled, and the
    error the node's run ended with, if any."""
    reads_lost = {"on": False}
    queries: list[str] = []
    get_orders = venue.trading.get_orders
    by_client_id = venue.trading.get_order_by_client_id

    def lost_orders(filter=None):
        if reads_lost["on"]:
            raise ConnectionError("connection reset")
        return get_orders(filter)

    def lost_lookup(client_id: str):
        if reads_lost["on"]:
            queries.append(client_id)
            raise ConnectionError("connection reset")
        return by_client_id(client_id)

    monkeypatch.setattr(venue.trading, "get_orders", lost_orders)
    monkeypatch.setattr(venue.trading, "get_order_by_client_id", lost_lookup)
    venue.trading.order_lookup_error = lambda _id: (
        ConnectionError("connection reset") if reads_lost["on"] else None
    )
    one_share = {**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1", "avg_entry_price": "150"}
    strategy = Claimant(StrategyConfig(external_order_instrument_ids=[AAPL]) if claim else None)
    engine = LiveExecutionEngineConfig(
        **LOST_SUBMIT_CHECKS,
        submission_recovery_policy=(
            SubmissionRecoveryPolicy.RETAIN_UNRESOLVED
            if retain
            else SubmissionRecoveryPolicy.RESOLVE_LOCALLY
        ),
    )
    alpaca_holds = Decimal(1) if side == "entry" else Decimal(0)
    seen: dict = {}

    def limit(order_side: OrderSide):
        return strategy.order_factory.limit(
            AAPL, order_side, Quantity.from_int(1), Price.from_str("150.00")
        )

    def external_fills() -> list:
        return [o for o in strategy.cache.orders() if o.strategy_id.value == "EXTERNAL"]

    async def body(client) -> None:
        if side == "close":
            venue.trading.script = "fill"
            strategy.submit_order(limit(OrderSide.BUY))
            await until_named(strategy, "OrderFilled")
            venue.trading.positions = [one_share]
            await settled(client, venue)
        venue.trading.script = "silent"
        venue.trading.took_before_error = took
        venue.trading.submit_error = alpaca.api_error(500, 50010000, "internal server error")
        reads_lost["on"] = True
        order = limit(OrderSide.BUY if side == "entry" else OrderSide.SELL)
        strategy.submit_order(order)

        def own() -> Any:
            cached = strategy.cache.order(order.client_order_id)
            assert cached is not None
            return cached

        # A limit of 5 in-flight checks permits 4 queries before the fifth gives up
        # (reconciliation.md, "Recovery limits", at the pin): retained, the engine says so; resolved
        # locally, the order is rejected.
        await until_true(lambda: len(queries) >= 4)
        log: list[str] = []

        def given_up() -> bool:
            if not retain:
                return own().status == OrderStatus.REJECTED
            logging_sync_to_disk()
            captured = capfd.readouterr()
            log.append(captured.out + captured.err)
            return EXHAUSTED.format(order.client_order_id) in "".join(log)

        await until_true(given_up, timeout=4)
        cached = own()
        seen["given_up"] = (cached.status, cached.is_closed, strategy.held())
        venue.trading.submit_error = None
        venue.trading.took_before_error = False
        reads_lost["on"] = False
        if order_unread:
            seen["unread_order_reads"] = 0

            def never_read(_id):
                seen["unread_order_reads"] += 1
                return ConnectionError("connection reset")

            venue.trading.order_lookup_error = never_read
        if unread_reads:
            left = {"n": unread_reads}

            def once(_id):
                if left["n"] > 0:
                    left["n"] -= 1
                    return ConnectionError("connection reset")
                return None

            venue.trading.order_lookup_error = once
        seen["client_order_id"] = order.client_order_id.value
        if not took:
            await asyncio.sleep(1.0)  # every check runs again, with the reads answered
            seen.update(order=(own().status, []), submits=len(venue.trading.submitted))
            return
        (placed,) = [
            p
            for p in venue.trading._placed.values()
            if p["client_order_id"] == order.client_order_id.value
        ]
        if trace_unread_reads:
            ancestor = placed
            placed = {
                **ancestor,
                "id": str(uuid.UUID(int=2342)),
                "client_order_id": "alpaca-made-trace-replacement",
                "replaces": ancestor["id"],
            }
            ancestor.update(status="replaced", replaced_by=placed["id"])
            venue.trading._placed[placed["id"]] = placed
            seen["ancestor_reads"] = 0

            def trace_once(order_id):
                if order_id == ancestor["id"]:
                    seen["ancestor_reads"] += 1
                    if seen["ancestor_reads"] <= trace_unread_reads:
                        return ConnectionError("ancestor connection reset")
                return None

            venue.trading.order_lookup_error = trace_once
        update = venue.trading.update(
            placed["id"], "fill", 1, deliver=delivered, status="filled", filled_qty="1"
        )
        venue.trading.positions = [one_share] if side == "entry" else []
        await until_true(
            lambda: (
                (strategy.held() == alpaca_holds and (not delivered or own().is_closed))
                or bool(external_fills())
                or (order_unread and seen.get("unread_order_reads", 0) >= 3)
            ),
            timeout=5,
        )
        await asyncio.sleep(1.0)  # every check runs again: nothing more is booked
        cached = own()
        seen.update(
            order=(cached.status, sorted(trade_ids(cached))),
            trade_id=update["execution_id"],
            held=strategy.held(),
            alpaca_holds=alpaca_holds,
            external=[(o.side, o.filled_qty.as_double()) for o in external_fills()],
            submits=len(venue.trading.submitted),
        )

    try:
        run_with_client(body, strategy, exec_engine=engine)
    except RuntimeError as e:
        seen["run"] = str(e)
    return seen


@pytest.mark.parametrize("side", ["entry", "close"])
@pytest.mark.parametrize("delivered", [True, False], ids=["delivered", "lost"])
def test_a_submit_never_answered_and_filled_later_is_the_strategys_own_with_both_settings(
    venue, alpaca, monkeypatch, capfd, side, delivered
) -> None:
    # The session window's claim 10: the node rejected such an order itself (`INFLIGHT_TIMEOUT`), and the
    # fill Alpaca then made was booked to an `EXTERNAL` order, which `SessionMaCross` does not read: after
    # a closing sell it sold a share it no longer held, after an entry it bought a second. With the paper
    # nodes' two settings the order stays open at the node, so a strategy that sends nothing while one
    # works sends nothing more, and what the node books in the instrument is the strategy's own.
    seen = lost_submit_filled(
        venue, alpaca, monkeypatch, capfd, side=side, delivered=delivered, retain=True, claim=True
    )

    status, closed, _ = seen["given_up"]
    assert (status, closed) == (OrderStatus.SUBMITTED, False), seen
    assert seen["held"] == seen["alpaca_holds"], seen
    assert seen["external"] == [], seen
    assert seen["submits"] == (1 if side == "entry" else 2), seen  # nothing sent again
    # Lost, the position check's fill report names the order by the client order ID Alpaca's read of it
    # shows, so the fill reaches the order itself, under Alpaca's trade ID, and nothing is left unresolved
    # at the stop.
    assert seen["order"] == (OrderStatus.FILLED, [seen["trade_id"]]), seen
    assert "run" not in seen, seen


@pytest.mark.parametrize("side", ["entry", "close"])
def test_a_submit_never_answered_and_filled_later_was_booked_to_external_before(
    venue, alpaca, monkeypatch, capfd, side
) -> None:
    # The claim as the review found it, on the paper nodes' settings before this change (upstream's
    # defaults): the strategy's own position is not Alpaca's, and an `EXTERNAL` order holds the fill.
    seen = lost_submit_filled(
        venue, alpaca, monkeypatch, capfd, side=side, delivered=False, retain=False, claim=False
    )

    status, closed, _ = seen["given_up"]
    assert (status, closed) == (OrderStatus.REJECTED, True), seen
    assert seen["held"] != seen["alpaca_holds"], seen
    # The fill report, under Alpaca's order ID the node never heard, and the position check's own order
    # (`RECONCILIATION`), each an `EXTERNAL` one on the order's side.
    assert seen["external"], seen
    assert {s for s, _ in seen["external"]} == {
        OrderSide.BUY if side == "entry" else OrderSide.SELL
    }, seen
    assert "run" not in seen, seen


def test_the_claim_alone_leaves_the_order_closed_while_alpaca_may_still_fill_it(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # Why the engine retains the submit: claimed but resolved locally, the order is closed at the node
    # once the in-flight check gives up, with nothing held, while Alpaca holds it; a strategy that sends
    # nothing while one of its orders works would send again before the position check books the fill
    # (a minute after the order's last activity, by default).
    seen = lost_submit_filled(
        venue, alpaca, monkeypatch, capfd, side="entry", delivered=True, retain=False, claim=True
    )

    assert seen["given_up"] == (OrderStatus.REJECTED, True, Decimal(0)), seen
    # Alpaca's later fill is not applied to it.
    assert seen["order"][0] == OrderStatus.REJECTED, seen
    assert "run" not in seen, seen


@pytest.mark.parametrize("side", ["entry", "close"])
def test_retained_a_lost_fill_reaches_the_order_by_its_client_order_id(
    venue, alpaca, monkeypatch, capfd, side
) -> None:
    # Retained, the order stays submitted; with its trade update lost, the position check's fill report
    # carried only Alpaca's order ID, which the node never heard for an order never answered, so the
    # engine booked the fill to `EXTERNAL` and the order stayed submitted until the stop. The report now
    # carries the client order ID Alpaca's read of the order shows, as upstream's OKX adapter carries
    # the one the venue echoes, and the engine matches the fill to the order by it, with no claim.
    seen = lost_submit_filled(
        venue, alpaca, monkeypatch, capfd, side=side, delivered=False, retain=True, claim=False
    )

    assert seen["given_up"][:2] == (OrderStatus.SUBMITTED, False), seen
    assert seen["order"] == (OrderStatus.FILLED, [seen["trade_id"]]), seen
    assert seen["held"] == seen["alpaca_holds"], seen
    assert seen["external"] == [], seen
    assert "run" not in seen, seen


@pytest.mark.parametrize("retain", [True, False], ids=["retained", "resolved locally"])
def test_a_submit_alpaca_never_placed_is_retained_too_and_nothing_is_sent_again(
    venue, alpaca, monkeypatch, capfd, retain
) -> None:
    # What the retention costs: a submit answered 500 that Alpaca did not place is retained just the
    # same, since a read that finds no order is no outcome (`_query_order` sends nothing); Alpaca asks
    # that a timed-out order not be marked canceled "until confirmed" (working-with-orders FAQ). The order
    # stays submitted, so a strategy that sends nothing while one works sends nothing more for the rest
    # of the run, and the run ends with the error naming it. Resolved locally, it was rejected.
    seen = lost_submit_filled(
        venue,
        alpaca,
        monkeypatch,
        capfd,
        side="entry",
        delivered=False,
        retain=retain,
        claim=True,
        took=False,
    )

    assert seen["submits"] == 1, seen
    if retain:
        assert seen["order"][0] == OrderStatus.SUBMITTED, seen
        assert seen["run"].startswith("Submission recovery incomplete at shutdown"), seen
        assert seen["client_order_id"] in seen["run"], seen
    else:
        assert seen["order"][0] == OrderStatus.REJECTED, seen
        assert "run" not in seen, seen


# -- the code review of the fill report's client order ID (`_unanswered_named`) --------------------


def naming(venue, alpaca, monkeypatch, check) -> None:
    """Run `check(client, strategy, waiting, placed_id, reads)` once the strategy holds a buy whose submit
    Alpaca took and answered 500, so the node holds it `SUBMITTED` with no Alpaca order ID; `reads`
    lists every `get_order_by_id`."""
    strategy = Claimant()
    reads: list[str] = []
    by_id = venue.trading.get_order_by_id

    def counted(order_id):
        reads.append(str(order_id))
        return by_id(order_id)

    monkeypatch.setattr(venue.trading, "get_order_by_id", counted)

    async def body(client) -> None:
        venue.trading.script = "silent"
        venue.trading.took_before_error = True
        venue.trading.submit_error = alpaca.api_error(500, 50010000, "internal server error")
        order = strategy.order_factory.limit(
            AAPL, OrderSide.BUY, Quantity.from_int(1), Price.from_str("150.00")
        )
        strategy.submit_order(order)
        await until_true(lambda: bool(venue.trading._placed))
        await until_true(lambda: "OrderSubmitted" in strategy.names())
        (placed,) = venue.trading._placed.values()
        await check(
            client, strategy, strategy.cache.order(order.client_order_id), placed["id"], reads
        )

    run_with_client(body, strategy)


def fill_of(alpaca, order_id: str, side: str = "buy", symbol: str = "AAPL"):
    return fill_report(
        {**alpaca.FILL_ACTIVITY, "order_id": order_id, "side": side, "symbol": symbol}, 0
    )


def test_a_fill_of_a_submit_never_answered_is_named_and_other_fills_are_not(
    venue, alpaca, monkeypatch
) -> None:
    named: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        own = fill_of(alpaca, placed_id)
        other_side = fill_of(alpaca, placed_id, side="sell")
        already = canon_alpaca.execution.report_with(
            own, client_order_id=ClientOrderId("O-elsewhere")
        )
        named.extend(await client._unanswered_named([own, other_side, already]))
        named.append(list(reads))

    naming(venue, alpaca, monkeypatch, check)

    own, other_side, already, reads = named
    assert own.client_order_id is not None and own.client_order_id.value.startswith("O-"), own
    assert other_side.client_order_id is None  # not the waiting order's side: not read for it
    assert already.client_order_id == ClientOrderId("O-elsewhere")
    assert len(reads) == 1, reads


def test_no_order_is_read_while_the_node_holds_no_submit_never_answered(
    venue, alpaca, monkeypatch
) -> None:
    seen: list = []

    async def body(client) -> None:
        report = fill_of(alpaca, "b5abe576-6a8a-49f3-a353-46b72c1ccae9")
        seen.append(await client._unanswered_named([report]))

    reads: list[str] = []
    monkeypatch.setattr(venue.trading, "get_order_by_id", lambda order_id: reads.append(order_id))
    run_with_client(body)

    ((report,),) = seen
    assert report.client_order_id is None and reads == []


def test_an_order_that_cannot_be_read_is_withheld_and_read_again(
    venue, alpaca, monkeypatch
) -> None:
    named: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        venue.trading.order_lookup_error = lambda _id: ConnectionError("connection reset")
        first = await client._unanswered_named([fill_of(alpaca, placed_id)])
        venue.trading.order_lookup_error = None
        (second,) = await client._unanswered_named([fill_of(alpaca, placed_id)])
        named.extend([first, second, list(reads)])

    naming(venue, alpaca, monkeypatch, check)

    first, second, reads = named
    assert first == []
    assert second.client_order_id is not None
    assert len(reads) == 2, reads


def test_an_order_not_the_nodes_is_read_once(venue, alpaca, monkeypatch) -> None:
    outside = alpaca.alpaca_order(
        id="c0ffee00-0000-4000-8000-000000000001", client_order_id="theirs-1"
    )
    named: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        venue.trading.orders.append(outside)  # after start-up, which would have made it the node's
        for _ in range(2):
            (report,) = await client._unanswered_named([fill_of(alpaca, outside["id"])])
            named.append(report)
        named.append(list(reads))

    naming(venue, alpaca, monkeypatch, check)

    first, second, reads = named
    assert first.client_order_id is None and second.client_order_id is None
    assert reads == [outside["id"]], reads


def test_a_submit_answered_while_its_order_is_read_is_not_named(venue, alpaca, monkeypatch) -> None:
    # The node's orders waiting on an answer were taken before the read: an order rejected meanwhile
    # (the in-flight check giving up under upstream's default, or a refusal) was named, and the engine
    # given a fill for a closed order rather than booking it.
    named: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        read = client._orders_read

        async def answered_meanwhile(order_ids):
            found = await read(order_ids)
            client.generate_order_rejected(
                waiting, "refused while read", client.clock.timestamp_ns(), False
            )
            await until_true(lambda: strategy.cache.order(waiting.client_order_id).is_closed)
            return found

        monkeypatch.setattr(client, "_orders_read", answered_meanwhile)
        named.extend(await client._unanswered_named([fill_of(alpaca, placed_id)]))

    naming(venue, alpaca, monkeypatch, check)

    (report,) = named
    assert report.client_order_id is None


@pytest.mark.parametrize("claim", [True, False], ids=["claimed", "unclaimed"])
def test_a_lost_fill_whose_order_cannot_be_read_keeps_retrying_without_external_booking(
    venue, alpaca, monkeypatch, capfd, claim
) -> None:
    # A failed identity read is not evidence of an outside order, even with an instrument claim.
    # Keep asking until shutdown; the retained order still makes shutdown fail without its identity.
    seen = lost_submit_filled(
        venue,
        alpaca,
        monkeypatch,
        capfd,
        side="entry",
        delivered=False,
        retain=True,
        claim=claim,
        order_unread=True,
    )

    assert seen["order"][0] == OrderStatus.SUBMITTED, seen
    assert seen["run"].startswith("Submission recovery incomplete at shutdown"), seen
    assert seen["held"] == Decimal(0), seen
    assert seen["external"] == [], seen
    assert seen["unread_order_reads"] >= 3, seen


# -- the replace chain's item, closed by ADR 0075's rules: its readers' reproductions, kept as guards -----


@pytest.mark.parametrize("end", ["canceled", "expired"])
def test_an_end_showing_fills_not_yet_applied_waits_for_them(venue, end) -> None:
    # Found by an independent reader (Codex, finding 3, on the replace chain's record), closed by ADR
    # 0075's end after its fills and kept as a guard: a `canceled` or `expired` whose order showed a
    # fill the stream had not delivered yet ended the node's order at once, so the late fill was dropped:
    # closed with 0 filled while Alpaca had filled 1. NautilusTrader holds a terminal report with
    # unaccounted fills back ("waiting for fill reports", `reconcile_order_report` at the pin).
    venue.trading.script = "accept"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        late = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        venue.trading.update(placed["id"], end, status=end, filled_qty="1")
        await settled(client, venue)
        venue.stream.send(late)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double()))

    run_with_client(body, strategy)

    expected = OrderStatus.CANCELED if end == "canceled" else OrderStatus.EXPIRED
    assert held == [(expected, 1.0)], strategy.names()
    assert strategy.names()[-2:] == [
        "OrderFilled",
        "OrderCanceled" if end == "canceled" else "OrderExpired",
    ]


def test_a_late_fill_of_a_middle_replacement_after_a_reconnect_does_not_move_the_order_back(
    venue,
) -> None:
    # Found by an independent reader (Codex, finding 2, on the replace chain's record), closed by ADR
    # 0075's rule that an order the record has moved past is never adopted, and kept as a guard: A was
    # replaced by B and B by C, the first PATCH
    # answer was lost, so the node held only A and C. After a reconnect the record knew nothing of B, a
    # delayed fill on B was taken as proof B was working, the node's order moved back from C to B at B's
    # older price, and C's own `canceled` was then ignored. An Alpaca order the order now working replaces,
    # directly or through others, is one the node's order has left.
    from alpaca.trading.requests import ReplaceOrderRequest

    venue.trading.script = "accept"
    venue.trading.replace_error = requests.ReadTimeout("read timed out")
    venue.trading.took_before_error = True
    venue.trading.replace_script = "silent"
    strategy = modifier()
    seen: list = []

    async def body(client) -> None:
        await until_idle(client, venue)
        async with asyncio.timeout(10):
            while len(venue.trading._placed) < 2:
                await asyncio.sleep(0.01)
        old = placed_order(venue, 0)
        middle = placed_order(venue, 1)
        middle.update(filled_qty="1", filled_avg_price="150", status="partially_filled")
        delayed = {
            "event": "partial_fill",
            "order": dict(middle),
            "timestamp": datetime.now(UTC).isoformat(),
            "qty": "1",
            "price": "150",
            "execution_id": str(uuid.uuid4()),
        }
        venue.trading.replace_error = None
        venue.trading.replace_order_by_id(middle["id"], ReplaceOrderRequest(limit_price=151.0))
        final = placed_order(venue, 2)
        venue.trading.confirm_replace(old["id"])
        await settled(client, venue)
        assert client.cache.order(strategy.client_order_id).venue_order_id.value == final["id"]
        await reconnect(client, venue)
        venue.stream.send(delayed)
        await settled(client, venue)
        after = client.cache.order(strategy.client_order_id)
        venue.trading.update(final["id"], "canceled", status="canceled")
        await settled(client, venue)
        ended = client.cache.order(strategy.client_order_id)
        seen.append(
            (after.venue_order_id.value == final["id"], ended.status, ended.filled_qty.as_double())
        )

    run_with_client(body, strategy)

    assert seen == [(True, OrderStatus.CANCELED, 1.0)], strategy.names()


@pytest.mark.parametrize("activity", ["listed", "lagging"])
def test_a_replacements_cancel_before_a_late_fill_on_the_order_it_replaced_keeps_the_fill(
    venue, monkeypatch, activity
) -> None:
    # Found by an independent reader (Antigravity, round 1 on the fill-identity model, finding 6): an
    # order replaced after a fill whose stream update was late, then the replacement's `canceled`,
    # showing the chain's fill, before that update: the cancel ended the node's order with 0 filled and
    # the late fill was dropped. ADR 0075's end after its fills brings it from the activities first, or
    # waits for the late update while they lag.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 1.0)
    venue.trading.script = "accept"
    strategy = modifier()
    held: list = []

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while "OrderUpdated" not in strategy.names():
                await asyncio.sleep(0.01)
        await settled(client, venue)
        first, second = placed_order(venue, 0), placed_order(venue, 1)
        late = venue.trading.update(
            first["id"], "partial_fill", 1, deliver=False, status="replaced", filled_qty="1"
        )
        if activity == "lagging":
            venue.trading.activities.pop()
        second.update(filled_qty="1")
        venue.trading.update(second["id"], "canceled", status="canceled", filled_qty="1")
        await settled(client, venue)
        venue.stream.send(late)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(late["execution_id"])

    run_with_client(body, strategy)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 1.0, [execution_id]), strategy.names()
    assert strategy.names()[-2:] == ["OrderFilled", "OrderCanceled"], strategy.names()


# -- Codex, outside round 2 on ADR 0075 (at 1c3d671) ------------------------------------------------


def test_a_query_bringing_a_raised_replacements_fill_adopts_its_quantity_first(venue) -> None:
    # Found by an independent reader (Codex, outside round 2 on ADR 0075, finding 1): a modify raised the
    # quantity from 2 to 4, the `replaced` and the replacement's 2-share fill were lost, and a
    # `QueryOrder` on the original brought the fill by its trade ID against the old quantity: FILLED 2 of
    # 2 while Alpaca works 2 more. Upstream applies a quantity amendment before recovered fills
    # (`crates/execution/src/reconciliation/orders.rs:45`, `crates/live/src/execution/manager.rs:4522`).
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(field="quantity", prices=["4"])
    held: list = []

    async def body(client) -> None:
        old = placed_order(venue, 0)
        new = placed_order(venue, 1)
        lost = venue.trading.update(
            new["id"], "partial_fill", 2, deliver=False, status="partially_filled", filled_qty="2"
        )
        await query(client, strategy, old)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append(
            (
                cached.status,
                cached.quantity.as_double(),
                cached.filled_qty.as_double(),
                trade_ids(cached),
            )
        )
        held.append(lost["execution_id"])

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), body)

    (status, quantity, filled, trades), execution_id = held
    assert (status, quantity, filled, trades) == (
        OrderStatus.PARTIALLY_FILLED,
        4.0,
        2.0,
        [execution_id],
    ), strategy.names()


def test_a_replaced_read_finding_the_replacement_canceled_with_a_lost_fill_ends_the_order(
    venue, alpaca, monkeypatch
) -> None:
    # Found by an independent reader (Codex, outside round 2 on ADR 0075, finding 2): the PATCH answer,
    # the replacement's fill and its `canceled` were lost; `replaced` came, its read showed the
    # replacement canceled with 1 filled, and the client waited for the replacement's own updates, which
    # never came. Under the paper node's checks, which read open orders only, the order stayed
    # PARTIALLY_FILLED. The end after its fills now covers it.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    venue.trading.replace_error = requests.ReadTimeout("lost replacement answer")
    venue.trading.took_before_error = True
    strategy = modifier()
    held: list = []

    async def body(client) -> None:
        async with asyncio.timeout(10):
            while not venue.trading.replace_requests:
                await asyncio.sleep(0.01)
        await until_idle(client, venue)
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        lost = venue.trading.update(
            new["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
        )
        venue.trading.update(
            new["id"],
            "canceled",
            deliver=False,
            status="canceled",
            canceled_at=datetime.now(UTC).isoformat(),
        )
        venue.trading.positions[:] = [{**alpaca.POSITION_AAPL, "qty": "1", "qty_available": "1"}]
        venue.trading.confirm_replace(old["id"])
        await settled(client, venue)
        await asyncio.sleep(1.5)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), trade_ids(cached)))
        held.append(lost["execution_id"])

    run_with_client(body, strategy, exec_engine=PAPER_CHECKS)

    (status, filled, trades), execution_id = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 1.0, [execution_id]), strategy.names()


def test_a_stale_terminal_report_does_not_end_an_order_whose_end_waits_for_more_fills(
    venue, monkeypatch
) -> None:
    # Found by an independent reader (Codex, outside round 2 on ADR 0075, finding 3): a `canceled`
    # showing 2 filled waited for its second fill's activity; an older canceled snapshot showing 1 then
    # reached the engine, which closed the order at 1, and the second fill, listed within the reads, was
    # lost without the run-out error. Whether Alpaca sends such copies is UNVERIFIED; a report showing no
    # more than an end already waiting is held back, and the client ends the order itself.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.5)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        first = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        stale = dict(placed, status="canceled", canceled_at=datetime.now(UTC).isoformat())
        second = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="2",
        )
        second_activity = venue.trading.activities.pop()
        venue.trading.update(
            placed["id"], "canceled", status="canceled", canceled_at=datetime.now(UTC).isoformat()
        )
        await settled(client, venue)
        report = await client._order_report(AlpacaOrder(**stale))
        assert report is not None
        settled_report = await client._settled_report(report)
        if settled_report is not None:
            client._handle_report(settled_report)
        await settled(client, venue)
        venue.trading.activities.append(second_activity)
        await asyncio.sleep(0.8)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), sorted(trade_ids(cached))))
        held.append(sorted([first["execution_id"], second["execution_id"]]))

    run_with_client(body, strategy)

    (status, filled, trades), both = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 2.0, both), strategy.names()


# -- Stream acknowledgement time (Nautilus OrderAccepted / Alpaca Order.submitted_at) ---------------


@pytest.fixture
def submit_answer_held(venue, monkeypatch):
    """Let the stand-in place the order, but hold its HTTP answer until the test releases it."""
    venue.trading.script = "silent"
    ready = threading.Event()
    release = threading.Event()
    submit = venue.trading.submit_order

    def held(order_data):
        answer = submit(order_data)
        ready.set()
        assert release.wait(10), "the test did not release the submit answer"
        return answer

    monkeypatch.setattr(venue.trading, "submit_order", held)
    try:
        yield ready, release
    finally:
        release.set()


@pytest.mark.parametrize(
    ("event", "status", "quantity", "last_event"),
    [
        ("new", "new", None, "OrderAccepted"),
        ("accepted", "accepted", None, "OrderAccepted"),
        ("pending_new", "pending_new", None, "OrderAccepted"),
        ("partial_fill", "partially_filled", 1, "OrderFilled"),
        ("fill", "filled", 2, "OrderFilled"),
        ("canceled", "canceled", None, "OrderCanceled"),
        ("expired", "expired", None, "OrderExpired"),
    ],
)
def test_stream_acceptance_time_before_the_http_answer(
    venue,
    submit_answer_held,
    generated,
    event,
    status,
    quantity,
    last_event,
) -> None:
    # OrderAccepted.ts_event becomes the cached ts_accepted (Nautilus d53849b216e2,
    # crates/model/src/orders/mod.rs:1042-1046). The fill's own time must stay later.
    ready, release = submit_answer_held
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        try:
            async with asyncio.timeout(5):
                while not ready.is_set():
                    await asyncio.sleep(0.005)
            await until_named(strategy, "OrderSubmitted")
            assert strategy.client_order_id is not None
            placed = placed_order(venue)
            submitted_at = datetime.fromisoformat(placed["submitted_at"])
            update_at = submitted_at + timedelta(seconds=5, microseconds=123456)
            raw = venue.trading.update(
                placed["id"],
                event,
                quantity,
                deliver=False,
                status=status,
                filled_qty=str(quantity or 0),
            )
            update = TradeUpdate.model_validate({**raw, "timestamp": update_at})
            expected = dt_to_unix_nanos(submitted_at)
            names = ["OrderSubmitted", "OrderAccepted"]
            if last_event != "OrderAccepted":
                names.append(last_event)

            await client._apply(update)
            await until_named(strategy, last_event)
            assert strategy.names() == names
            assert strategy.events[1].ts_event == expected
            assert client.cache.order(strategy.client_order_id).ts_accepted == expected
            if last_event != "OrderAccepted":
                assert strategy.events[-1].ts_event == dt_to_unix_nanos(update_at)
            if quantity is not None:
                fill = strategy.events[-1]
                assert fill.trade_id.value == str(update.execution_id)
                assert fill.last_qty == Quantity.from_int(quantity)
                assert fill.last_px == Price.from_str(str(raw["price"]))
                assert trade_ids(client.cache.order(strategy.client_order_id)) == [
                    str(update.execution_id)
                ]

            await client._apply(update)  # a duplicate update must not accept or fill twice
            release.set()
            await until_idle(client, venue)
            await client._apply(update)  # nor does the same update after the HTTP answer
            await until_idle(client, venue)
            assert strategy.names() == names
            assert client.cache.order(strategy.client_order_id).ts_accepted == expected
            assert generated.count("accepted") == 1
        finally:
            release.set()

    run_with_client(body, strategy)


def test_stream_acceptance_time_survives_a_pending_cancel(
    venue, submit_answer_held, generated
) -> None:
    ready, release = submit_answer_held
    strategy = trader(order_type="limit", cancel_on="OrderSubmitted")
    strategy.stopped_for = "held open"
    # Once released, the queued HTTP cancel adds no further stream update.
    venue.trading.cancel_order_by_id = lambda order_id: None

    async def body(client) -> None:
        try:
            async with asyncio.timeout(5):
                while not ready.is_set():
                    await asyncio.sleep(0.005)
            await until_named(strategy, "OrderPendingCancel")
            assert strategy.client_order_id is not None
            placed = placed_order(venue)
            submitted_at = datetime.fromisoformat(placed["submitted_at"])
            update_at = submitted_at + timedelta(seconds=5, microseconds=123456)
            raw = venue.trading.update(placed["id"], "new", deliver=False, status="new")
            update = TradeUpdate.model_validate({**raw, "timestamp": update_at})
            expected = dt_to_unix_nanos(submitted_at)
            await client._apply(update)
            assert "OrderAccepted" not in strategy.names()
            assert client._acknowledged[strategy.client_order_id][1] == expected

            refused_at = update_at + timedelta(seconds=1)
            refused = TradeUpdate.model_validate(
                {**raw, "event": "order_cancel_rejected", "timestamp": refused_at}
            )
            await client._apply(refused)
            await until_named(strategy, "OrderAccepted")
            release.set()
            await until_idle(client, venue)
            assert strategy.names() == [
                "OrderSubmitted",
                "OrderPendingCancel",
                "OrderCancelRejected",
                "OrderAccepted",
            ]
            assert strategy.events[-2].ts_event == dt_to_unix_nanos(refused_at)
            assert strategy.events[-1].ts_event == expected
            assert client.cache.order(strategy.client_order_id).ts_accepted == expected
            assert generated.count("accepted") == 1
        finally:
            release.set()

    run_with_client(body, strategy)


def test_stream_acceptance_time_keeps_an_http_first_acceptance(venue, generated) -> None:
    venue.trading.script = "silent"
    strategy = trader(order_type="limit")
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        submitted_at = datetime.fromisoformat(placed["submitted_at"])
        expected = dt_to_unix_nanos(submitted_at)
        assert strategy.events[1].ts_event == expected
        assert client.cache.order(strategy.client_order_id).ts_accepted == expected
        raw = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        update_at = submitted_at + timedelta(seconds=5, microseconds=123456)
        update = TradeUpdate.model_validate({**raw, "timestamp": update_at})
        await client._apply(update)
        await until_idle(client, venue)
        assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]
        assert client.cache.order(strategy.client_order_id).ts_accepted == expected
        assert strategy.events[-1].ts_event == dt_to_unix_nanos(update_at)
        assert strategy.events[-1].trade_id.value == str(update.execution_id)
        assert generated.count("accepted") == 1

    run_with_client(body, strategy)


@pytest.mark.parametrize("cancel", [False, True], ids=["fill", "then-cancel"])
def test_a_fill_on_an_order_whose_chain_cannot_be_read_waits_rather_than_moving_the_order(
    venue, alpaca, monkeypatch, cancel
) -> None:
    # Found by an independent reader (Muse, outside round 2 on ADR 0075): a fill on the middle order of
    # an outside chain of three, whose first order's read failed, was taken as on an order ahead and
    # adopted: the node's order moved back to the middle order, and the working order's `canceled` left
    # it PARTIALLY_FILLED. An order that cannot be placed in the chain is neither adopted nor counted
    # behind; its fill waits for the later reads, which place it and bring the fill by its trade ID.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.2)
    first_id, middle_id, working_id = MIDDLE_CHAIN
    first = alpaca.alpaca_order(
        id=first_id,
        client_order_id="outside-mid2",
        qty="3",
        status="replaced",
        replaced_by=middle_id,
    )
    middle = alpaca.alpaca_order(
        id=middle_id,
        client_order_id="alpaca-made-31",
        qty="3",
        status="replaced",
        replaces=first_id,
        replaced_by=working_id,
    )
    working = alpaca.alpaca_order(
        id=working_id, client_order_id="alpaca-made-32", qty="3", status="new", replaces=middle_id
    )
    venue.trading.orders = [first, middle, working]
    node_order = ClientOrderId("outside-mid2")
    held: list = []

    async def body(client) -> None:
        reads = {"first": 0}

        def second_read_fails(order_id: str) -> Exception | None:
            if order_id == first_id:
                reads["first"] += 1
                if reads["first"] == 2:
                    return OSError("connection reset by peer")
            return None

        venue.trading.order_lookup_error = second_read_fails
        execution_id = str(uuid.UUID(int=82))
        shown = {**middle, "status": "partially_filled", "replaced_by": None, "filled_qty": "1"}
        middle["filled_qty"] = "1"
        working.update(filled_qty="1", status="partially_filled")
        venue.trading.activities.append(
            alpaca.fill_activity(
                middle, execution_id, 1, "150", datetime.now(UTC), "partial_fill", 1, 2
            )
        )
        venue.stream.send(
            {
                "event": "partial_fill",
                "order": shown,
                "timestamp": datetime.now(UTC).isoformat(),
                "price": "150",
                "qty": "1",
                "position_qty": "1",
                "execution_id": execution_id,
            }
        )
        await settled(client, venue)
        await asyncio.sleep(0.6)  # the later reads
        await settled(client, venue)
        cached = client.cache.order(node_order)
        held.append((cached.venue_order_id.value, cached.filled_qty.as_double(), trade_ids(cached)))
        if cancel:
            working.update(status="canceled")
            venue.stream.send(
                {
                    "event": "canceled",
                    "order": dict(working),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
            await settled(client, venue)
            held.append(client.cache.order(node_order).status)
        held.append(execution_id)

    run_with_client(body)

    if cancel:
        (venue_id, filled, trades), status, execution_id = held
        assert status == OrderStatus.CANCELED, held
    else:
        (venue_id, filled, trades), execution_id = held
    assert (venue_id, filled, trades) == (working_id, 1.0, [execution_id]), held


def test_a_catch_up_on_the_working_order_places_a_raised_replacements_fills_first(venue) -> None:
    # The code review of round 2's model: the catch-up placed only the order its read was asked for, and
    # counted the listed fills of every order of the chain. A catch-up on the original order listing a
    # quantity-raising replacement's fill counted it against the old quantity: FILLED 2 of 2 while
    # Alpaca works 2 more. Each order whose fills are counted is placed first.
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier(field="quantity", prices=["4"])
    held: list = []

    async def body(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        lost = venue.trading.update(
            new["id"], "partial_fill", 2, deliver=False, status="partially_filled", filled_qty="2"
        )
        order = client.cache.order(strategy.client_order_id)
        await client._catch_up(order, client._record(order), Decimal(2), VenueOrderId(old["id"]))
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append(
            (
                cached.status,
                cached.quantity.as_double(),
                cached.filled_qty.as_double(),
                trade_ids(cached),
            )
        )
        held.append(lost["execution_id"])

    run_modify(venue, strategy, lambda: bool(venue.trading.replace_requests), body)

    (status, quantity, filled, trades), execution_id = held
    assert (status, quantity, filled, trades) == (
        OrderStatus.PARTIALLY_FILLED,
        4.0,
        2.0,
        [execution_id],
    ), strategy.names()


def test_an_older_canceled_report_held_back_while_fills_are_caught_up_still_ends_the_order(
    venue, monkeypatch
) -> None:
    # The code review of round 2's model: a canceled report showing fewer fills than the client was
    # catching up to was held back, with no end waiting, so nothing ended the order once the fills came,
    # and the paper node's checks never ask for a closed order again. The held report's end now waits on
    # the record, and the client sends it after the fills.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.5)
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    held: list = []

    async def body(client) -> None:
        await until_named(strategy, "OrderAccepted")
        await until_idle(client, venue)
        placed = placed_order(venue)
        first = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        stale = dict(placed, status="canceled", canceled_at=datetime.now(UTC).isoformat())
        second = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="2",
        )
        second_activity = venue.trading.activities.pop()
        venue.stream.send({**first, "order": {**first["order"], "filled_qty": "2"}})
        await settled(client, venue)
        report = await client._order_report(AlpacaOrder(**stale))
        assert report is not None
        assert await client._settled_report(report) is None
        venue.trading.activities.append(second_activity)
        await asyncio.sleep(1.2)
        await settled(client, venue)
        cached = client.cache.order(strategy.client_order_id)
        held.append((cached.status, cached.filled_qty.as_double(), sorted(trade_ids(cached))))
        held.append(sorted([first["execution_id"], second["execution_id"]]))

    run_with_client(body, strategy)

    (status, filled, trades), both = held
    assert (status, filled, trades) == (OrderStatus.CANCELED, 2.0, both), strategy.names()


def test_window_does_not_claim_a_fill_for_a_client_id_it_never_sent(
    venue, alpaca, tmp_path
) -> None:
    from canon_trading.session_ma_cross import SessionMaCross
    from canon_trading.session_window import WindowConfig
    from canon_trading.session_window import window_node

    node, window_strategy, cap = window_node(
        WindowConfig(symbol="AAPL", log_dir=tmp_path),
        wind_down_at=datetime.now(UTC) + timedelta(minutes=5),
        log_name="foreign-fill-claim",
        feed="iex",
    )
    try:
        assert isinstance(window_strategy, SessionMaCross)
        assert window_strategy.config is not None
        claims = window_strategy.config.external_order_instrument_ids
    finally:
        node.dispose()
        del node, window_strategy, cap

    strategy = Claimant(StrategyConfig(external_order_instrument_ids=claims))
    seen = []

    async def body(client) -> None:
        # This appears after startup, so the preflight/startup checks saw flat.
        foreign = alpaca.alpaca_order(
            id="c0ffee00-0000-4000-8000-000000000001",
            client_order_id="manual-foreign-1",
            symbol="AAPL",
            side="buy",
            qty="1",
            filled_qty="1",
            filled_avg_price="150",
            status="filled",
        )
        venue.trading.orders.append(foreign)
        venue.trading.activities = [
            alpaca.fill_activity(foreign, str(uuid.UUID(int=901)), 1, "150", datetime.now(UTC))
        ]
        reports = await client._fill_reports(fills_command(instrument_id=AAPL))
        assert len(reports) == 1
        assert reports[0].client_order_id is None
        assert venue.trading.submitted == []
        client._handle_report(reports[0])
        venue_id = VenueOrderId(foreign["id"])
        await until_true(lambda: client.cache.client_order_id(venue_id) is not None)
        order = client.cache.order(client.cache.client_order_id(venue_id))
        seen.append(order.strategy_id.value)

    run_with_client(body, strategy)
    assert seen == ["EXTERNAL"], seen


def test_an_unread_foreign_parent_does_not_discard_a_readable_own_fill(
    venue, alpaca, monkeypatch
) -> None:
    foreign = alpaca.alpaca_order(
        id="c0ffee00-0000-4000-8000-000000000002",
        client_order_id="manual-replacement-1",
        replaces="c0ffee00-0000-4000-8000-000000000003",
        symbol="AAPL",
        side="buy",
        qty="1",
        filled_qty="1",
        filled_avg_price="150",
        status="filled",
    )
    named = []
    owner = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        venue.trading.orders.append(foreign)
        # The foreign parent is deliberately absent: get_order_by_id returns 404.
        now = datetime.now(UTC)
        own_report = fill_report(
            alpaca.fill_activity(
                venue.trading._placed[placed_id],
                str(uuid.UUID(int=902)),
                1,
                "150",
                now,
            ),
            0,
        )
        foreign_report = fill_report(
            alpaca.fill_activity(foreign, str(uuid.UUID(int=903)), 1, "150", now),
            0,
        )
        owner.append(waiting.client_order_id)
        named.extend(await client._unanswered_named([own_report, foreign_report]))

    naming(venue, alpaca, monkeypatch, check)
    assert len(named) == 1
    assert named[0].client_order_id == owner[0]


def test_an_unread_foreign_parent_defers_reconciliation_but_delivers_the_readable_own_fill(
    venue, alpaca, monkeypatch
) -> None:
    # The ancestor-trace case: the same setup, driven through `_fill_reports`
    # (which exists on main too) instead of `_unanswered_named` (the port's own helper), so the batch
    # property "the readable own fill survives an unrelated failed trace" can be compared on main
    # and on the port. The untraceable fill must wait, lest it be booked as an outside order.
    foreign = alpaca.alpaca_order(
        id="c0ffee00-0000-4000-8000-000000000002",
        client_order_id="manual-replacement-1",
        replaces="c0ffee00-0000-4000-8000-000000000003",
        symbol="AAPL",
        side="buy",
        qty="1",
        filled_qty="1",
        filled_avg_price="150",
        status="filled",
    )
    got: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        venue.trading.orders.append(foreign)
        now = datetime.now(UTC)
        venue.trading.activities = [
            alpaca.fill_activity(
                venue.trading._placed[placed_id], str(uuid.UUID(int=902)), 1, "150", now
            ),
            alpaca.fill_activity(foreign, str(uuid.UUID(int=903)), 1, "150", now),
        ]
        with pytest.raises(RuntimeError, match="fill identity is unread"):
            await client._fill_reports(fills_command(instrument_id=AAPL))
        await until_true(
            lambda: strategy.cache.order(waiting.client_order_id).status == OrderStatus.FILLED
        )
        got.append(strategy.cache.order(waiting.client_order_id))

    naming(venue, alpaca, monkeypatch, check)
    assert len(got) == 1, got
    assert got[0].client_order_id is not None, got
    assert trade_ids(got[0]) == [str(uuid.UUID(int=902))], got


def test_an_outside_replacement_whose_chain_cannot_be_read_does_not_drop_a_waiting_orders_fill(
    venue, alpaca, monkeypatch
) -> None:
    missing_prior = "00000000-0000-4000-8000-000000000099"
    outside = alpaca.alpaca_order(
        id="00000000-0000-4000-8000-000000000001",
        client_order_id="alpaca-made-1",
        replaces=missing_prior,
    )
    named: list = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        venue.trading.orders.append(outside)
        venue.trading.order_lookup_error = lambda order_id: (
            ConnectionError("connection reset") if str(order_id) == missing_prior else None
        )
        reports = await client._unanswered_named(
            [fill_of(alpaca, outside["id"]), fill_of(alpaca, placed_id)]
        )
        named.extend(reports)

    naming(venue, alpaca, monkeypatch, check)

    (own_report,) = named
    assert own_report.client_order_id == ClientOrderId(own_report.client_order_id.value)


def test_canceling_a_retained_submit_that_filled_at_alpaca_settles_it_from_the_read_refused(
    venue, alpaca, monkeypatch, capfd
) -> None:
    reads_lost = {"on": True}
    queries: list[str] = []
    by_client_id = venue.trading.get_order_by_client_id

    def lost_lookup(client_id: str):
        if reads_lost["on"]:
            queries.append(client_id)
            raise ConnectionError("connection reset")
        return by_client_id(client_id)

    monkeypatch.setattr(venue.trading, "get_order_by_client_id", lost_lookup)
    strategy = Claimant(StrategyConfig(external_order_instrument_ids=[AAPL]))
    engine = LiveExecutionEngineConfig(
        reconciliation_startup_delay_secs=0,
        inflight_check_interval_ms=100,
        inflight_check_threshold_ms=200,
        submission_recovery_policy=SubmissionRecoveryPolicy.RETAIN_UNRESOLVED,
    )
    seen: dict[str, Any] = {}

    async def body(client) -> None:
        venue.trading.script = "silent"
        venue.trading.took_before_error = True
        venue.trading.submit_error = alpaca.api_error(500, 50010000, "internal server error")
        order = strategy.order_factory.limit(
            AAPL, OrderSide.BUY, Quantity.from_int(1), Price.from_str("150.00")
        )
        strategy.submit_order(order)
        await until_true(lambda: len(queries) >= 4)
        log: list[str] = []

        def exhausted() -> bool:
            logging_sync_to_disk()
            captured = capfd.readouterr()
            log.append(captured.out + captured.err)
            return EXHAUSTED.format(order.client_order_id) in "".join(log)

        await until_true(exhausted, timeout=4)
        reads_lost["on"] = False
        (placed,) = venue.trading._placed.values()
        update = venue.trading.update(
            placed["id"], "fill", 1, deliver=False, status="filled", filled_qty="1"
        )
        # Reproduction harness: Alpaca answers 422 to a cancel of a filled order;
        # the stand-in otherwise answers 204 and streams `canceled` for it.
        venue.trading.refuse_cancel = lambda raw: (
            alpaca.api_error(422, 42210000, "order is not cancelable")
            if raw["status"] == "filled"
            else None
        )
        # This pin takes a ClientOrderId, not an order.
        strategy.cancel_order(order.client_order_id)
        await settled(client, venue)

        def own():
            cached = strategy.cache.order(order.client_order_id)
            assert cached is not None
            return cached

        await until_true(
            lambda: own().status == OrderStatus.FILLED,
            timeout=2,
        )
        cached = own()
        seen.update(
            status=cached.status,
            trades=trade_ids(cached),
            trade_id=update["execution_id"],
            held=strategy.held(),
        )

    run_with_client(body, strategy, exec_engine=engine)

    assert seen == {
        "status": OrderStatus.FILLED,
        "trades": [seen["trade_id"]],
        "trade_id": seen["trade_id"],
        "held": Decimal(1),
    }


def test_window_does_not_claim_an_order_for_a_client_id_it_never_sent(
    venue, alpaca, tmp_path
) -> None:
    from canon_trading.session_ma_cross import SessionMaCross
    from canon_trading.session_window import WindowConfig
    from canon_trading.session_window import window_node

    node, window_strategy, cap = window_node(
        WindowConfig(symbol="AAPL", log_dir=tmp_path),
        wind_down_at=datetime.now(UTC) + timedelta(minutes=5),
        log_name="foreign-order-claim",
        feed="iex",
    )
    try:
        assert isinstance(window_strategy, SessionMaCross)
        assert window_strategy.config is not None
        claims = window_strategy.config.external_order_instrument_ids
    finally:
        node.dispose()
        del node, window_strategy, cap

    strategy = Claimant(StrategyConfig(external_order_instrument_ids=claims))
    seen = []

    async def body(client) -> None:
        foreign = AlpacaOrder(
            **alpaca.alpaca_order(
                id="c0ffee00-0000-4000-8000-000000000004",
                client_order_id="manual-foreign-order",
                symbol="AAPL",
                side="buy",
                qty="1",
                status="new",
            )
        )
        await client._send_report(foreign)
        venue_id = VenueOrderId(str(foreign.id))
        await until_true(lambda: client.cache.client_order_id(venue_id) is not None)
        order = client.cache.order(client.cache.client_order_id(venue_id))
        seen.append((order.strategy_id.value, order.status))
        assert venue.trading.submitted == []

    run_with_client(body, strategy)
    assert seen == [("EXTERNAL", OrderStatus.ACCEPTED)], seen


@pytest.mark.parametrize("stage", ["order", "ancestor", "batch"])
def test_unanswered_fill_read_failures_are_private_and_retryable(
    venue, alpaca, monkeypatch, capfd, stage
) -> None:
    secret_text = "account=offline-account-id order=offline-order-id"
    seen = []

    async def check(client, strategy, waiting, placed_id, reads) -> None:
        order_id = placed_id
        if stage == "ancestor":
            replacement = alpaca.alpaca_order(
                id="c0ffee00-0000-4000-8000-000000000005",
                client_order_id="alpaca-made-replacement",
                replaces=placed_id,
                symbol="AAPL",
                side="buy",
            )
            venue.trading.orders.append(replacement)
            order_id = replacement["id"]
        report = fill_of(alpaca, order_id)
        reports = [report]
        get_orders = venue.trading.get_orders
        if stage == "batch":
            # More than one unknown order makes the naming pass try the bulk read first.
            foreign = alpaca.alpaca_order(
                id="c0ffee00-0000-4000-8000-000000000006",
                client_order_id="manual-foreign-batch",
            )
            venue.trading.orders.append(foreign)
            reports.append(fill_of(alpaca, foreign["id"]))

            def unread_orders(filter=None):
                raise ConnectionError(secret_text)

            monkeypatch.setattr(venue.trading, "get_orders", unread_orders)
        venue.trading.order_lookup_error = lambda order_id: (
            ConnectionError(secret_text) if str(order_id) == placed_id else None
        )
        first = await client._unanswered_named(reports)
        repeated = await client._unanswered_named(reports)
        venue.trading.order_lookup_error = None
        monkeypatch.setattr(venue.trading, "get_orders", get_orders)
        second = await client._unanswered_named(reports)
        seen.append((second[0].client_order_id, waiting.client_order_id))
        assert len(first) == len(repeated) == len(reports) - 1
        assert len(second) == len(reports)
        if stage == "batch":
            assert first[0].client_order_id is None and second[1].client_order_id is None

    naming(venue, alpaca, monkeypatch, check)
    second_id, own_id = seen[0]
    assert second_id == own_id
    output = logged(capfd)
    assert "ConnectionError" in output
    assert secret_text not in output
    assert "fill identity read 1 failed" in output
    assert "fill identity read 2 failed" in output


@pytest.mark.parametrize("side", ["entry", "close"])
def test_a_lost_fill_whose_order_read_fails_once_is_named_on_the_next_read(
    venue, alpaca, monkeypatch, capfd, side
) -> None:
    # The read of the retained order's Alpaca order fails once, then
    # answers. "Left unnamed and read again" holds only if the engine is not first given the unnamed
    # report, which it books as an external order under the venue order ID.
    seen = lost_submit_filled(
        venue,
        alpaca,
        monkeypatch,
        capfd,
        side=side,
        delivered=False,
        retain=True,
        claim=False,
        unread_reads=1,
    )

    assert "run" not in seen, seen
    assert seen["order"][0] == OrderStatus.FILLED, seen
    assert seen["held"] == seen["alpaca_holds"], seen
    assert seen["external"] == [], seen


@pytest.mark.parametrize("claim", [True, False], ids=["claimed", "unclaimed"])
def test_a_refused_cancel_of_a_filled_retained_submit_settles_after_the_activities_lag(
    venue, alpaca, monkeypatch, capfd, claim
) -> None:
    # The fill activity is listed only 0.5 s after the refused cancel
    reads_lost = {"on": True}
    queries: list[str] = []
    by_client_id = venue.trading.get_order_by_client_id

    def lost_lookup(client_id: str):
        if reads_lost["on"]:
            queries.append(client_id)
            raise ConnectionError("connection reset")
        return by_client_id(client_id)

    monkeypatch.setattr(venue.trading, "get_order_by_client_id", lost_lookup)
    strategy = Claimant(StrategyConfig(external_order_instrument_ids=[AAPL]) if claim else None)
    engine = LiveExecutionEngineConfig(
        reconciliation_startup_delay_secs=0,
        inflight_check_interval_ms=100,
        inflight_check_threshold_ms=200,
        submission_recovery_policy=SubmissionRecoveryPolicy.RETAIN_UNRESOLVED,
    )
    seen: dict[str, Any] = {}

    async def body(client) -> None:
        venue.trading.script = "silent"
        venue.trading.took_before_error = True
        venue.trading.submit_error = alpaca.api_error(500, 50010000, "internal server error")
        order = strategy.order_factory.limit(
            AAPL, OrderSide.BUY, Quantity.from_int(1), Price.from_str("150.00")
        )
        strategy.submit_order(order)
        await until_true(lambda: len(queries) >= 4)
        log: list[str] = []

        def exhausted() -> bool:
            logging_sync_to_disk()
            captured = capfd.readouterr()
            log.append(captured.out + captured.err)
            return EXHAUSTED.format(order.client_order_id) in "".join(log)

        await until_true(exhausted, timeout=4)
        reads_lost["on"] = False
        (placed,) = venue.trading._placed.values()
        update = venue.trading.update(
            placed["id"], "fill", 1, deliver=False, status="filled", filled_qty="1"
        )
        # Reproduction harness: Alpaca answers 422 to a cancel of a filled order;
        # the stand-in otherwise answers 204 and streams `canceled` for it.
        lagged = list(venue.trading.activities)
        venue.trading.activities.clear()
        asyncio.get_running_loop().call_later(0.5, venue.trading.activities.extend, lagged)
        venue.trading.refuse_cancel = lambda raw: (
            alpaca.api_error(422, 42210000, "order is not cancelable")
            if raw["status"] == "filled"
            else None
        )
        # This pin takes a ClientOrderId, not an order.
        strategy.cancel_order(order.client_order_id)
        await settled(client, venue)

        def own():
            cached = strategy.cache.order(order.client_order_id)
            assert cached is not None
            return cached

        await until_true(
            lambda: own().status == OrderStatus.FILLED,
            timeout=4,
        )
        cached = own()
        seen.update(
            status=cached.status,
            trades=trade_ids(cached),
            trade_id=update["execution_id"],
            held=strategy.held(),
        )

    run_with_client(body, strategy, exec_engine=engine)

    assert seen == {
        "status": OrderStatus.FILLED,
        "trades": [seen["trade_id"]],
        "trade_id": seen["trade_id"],
        "held": Decimal(1),
    }


def test_a_lost_replacement_fill_whose_ancestor_read_fails_once_is_named_on_the_next_read(
    venue, alpaca, monkeypatch, capfd
) -> None:
    seen = lost_submit_filled(
        venue,
        alpaca,
        monkeypatch,
        capfd,
        side="entry",
        delivered=False,
        retain=True,
        claim=False,
        trace_unread_reads=1,
    )

    assert "run" not in seen, seen
    assert seen["order"][0] == OrderStatus.FILLED, seen
    assert seen["held"] == seen["alpaca_holds"], seen
    assert seen["external"] == [], seen
    assert seen["ancestor_reads"] >= 2, seen


def test_a_retained_filled_submit_keeps_reading_past_the_fill_retry_budget(
    venue, alpaca, monkeypatch, capfd
) -> None:
    # Use the same native-node reproduction, with the activities still absent after all ordinary
    # catch-up reads. Retention must keep asking until they arrive or the node stops.
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_TRIES", 2)
    monkeypatch.setattr(canon_alpaca.execution, "FILL_READ_RETRY_SECS", 0.05)
    test_a_refused_cancel_of_a_filled_retained_submit_settles_after_the_activities_lag(
        venue, alpaca, monkeypatch, capfd, claim=False
    )
    output = logged(capfd)
    assert "retained submit fill read 1 failed" in output
    assert "retained submit fill read 2 failed" in output
    assert "retained submit fill read 3 failed" in output
    assert "ran out after 2 reads" not in output
