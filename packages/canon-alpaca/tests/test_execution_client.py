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
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import canon_alpaca.data
import canon_alpaca.execution
import canon_alpaca.providers
import pytest
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

AAPL = InstrumentId.from_str("AAPL.ALPACA")
TRADER = TraderId("TESTER-001")
FINAL_EVENTS = {"OrderDenied", "OrderRejected", "OrderCanceled", "OrderExpired"}
PLACEHOLDER = "offline-placeholder"


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
        self.cancel_on: str | None = None  # the event name on which to cancel the order
        self.submit = True
        self.idle = False  # neither submits nor stops: the test stops the node
        self.events: list = []
        self.client_order_id: ClientOrderId | None = None
        self.stopped_for: str | None = None
        self.filled = 0.0

    def on_start(self) -> None:
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
        else:
            order = factory.stop_market(
                AAPL, self.side, Quantity.from_int(self.quantity), Price.from_str(self.price)
            )
        self.client_order_id = order.client_order_id
        self.submit_order(order)

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
    body: Callable[[AlpacaExecutionClient], Awaitable[None]], strategy: Strategy | None = None
) -> None:
    """Run a hosted node, await `body` with its connected execution client, then stop the node.

    The default strategy is idle; with another, `body` can wait for what the strategy set off.
    """
    clients: list[AlpacaExecutionClient] = []

    class Capturing(AlpacaExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaExecutionClientFactory.create(**kwargs)
            clients.append(client)
            return client

    node = build_node(strategy or trader(idle=True), exec_factory=Capturing)

    async def main() -> None:
        async with asyncio.timeout(15):
            task = asyncio.ensure_future(node.run_async())
            try:
                while not (task.done() or (clients and clients[0].is_connected)):
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
        while client._rest_lock.locked() or not venue.stream._messages.empty():
            await asyncio.sleep(0.01)
        for _ in range(
            20
        ):  # the stream thread's last hand-off, and the hook after the worker returns
            await asyncio.sleep(0.01)


def logged(capfd) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


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
        ({"order_type": "stop", "price": "200.00"}, "order type STOP_MARKET is not supported"),
        ({"time_in_force": TimeInForce.IOC}, "time in force IOC is not supported"),
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


def test_open_order_placed_before_the_window_is_reported(venue, alpaca) -> None:
    venue.trading.orders = [alpaca.alpaca_order()]  # open, submitted on 2023-12-12
    reports: list = []
    window = SimpleNamespace(  # the fields the hook reads from a GenerateOrderStatusReports command
        open_only=False,
        instrument_id=None,
        start=dt_to_unix_nanos(datetime(2024, 1, 1, tzinfo=UTC)),
        end=None,
    )

    async def body(client) -> None:
        reports.extend(await client._generate_order_status_reports(window))

    run_with_client(body)

    windowed, open_now = venue.trading.order_queries[-2:]
    assert (windowed.status, open_now.status) == (QueryOrderStatus.ALL, QueryOrderStatus.OPEN)
    assert open_now.after is None
    assert [r.venue_order_id.value for r in reports] == [alpaca.ORDER_AAPL["id"]]


def test_open_order_outside_this_slice_fails_startup(venue, alpaca, capfd) -> None:
    venue.trading.orders = [alpaca.alpaca_order(type="stop", order_type="stop", stop_price="140")]
    node = build_node(trader(submit=False))

    with pytest.raises(Exception):  # noqa: B017 - the node's own startup failure type
        run(node)

    assert "is open with order type stop" in logged(capfd)


def test_account_state_is_reported_on_connect(venue) -> None:
    node = build_node(trader(submit=False))

    run(node)

    account = node.cache.account(ACCOUNT_ID)
    assert account is not None
    balance = account.balance()
    assert str(balance.total) == "122086.50 USD"
    assert str(balance.free) == "122086.50 USD"


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
