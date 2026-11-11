"""Integration tests against Alpaca's paper Trading API through a real node: limit and stop buys far from
the market, accepted and then canceled.

They run only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`
(`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`); otherwise pytest reports them skipped with that reason. Never write
the keys to a file. Run them with `mise run paper:test`, which holds the paper account's lock.

What they call: Alpaca's paper Trading API and its trade updates stream, through `TradingClient(paper=True)`
and `TradingStream(paper=True)` ("Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md).
The first places a DAY limit buy of 1 AAPL at $1.00 and cancels it once Alpaca has accepted it. The others
run rows of upstream's Execution Testing Spec through upstream's `ExecTester` (ADR 0036): TC-E41, a limit
buy $50 below the bid and a stop buy $50 above the ask, canceled by `CancelAllOrders` on stop; TC-E20 and
TC-E22, a stop and a stop-limit buy $50 above the ask, accepted at the prices sent and canceled on stop.
Modify (ADR 0042): TC-E30 through the tester, whose one-tick amend the client denies as sub-penny, and a
$1.00 buy modified to $1.01 through a node, then canceled.
Two run with any session closed and need no node event for the order: TC-E84, a $1.00 buy placed with
alpaca-py, as a prior session would leave it, which a node's start-up reconciliation must load open; and
TC-E44's venue side, a $1.00 buy canceled twice through alpaca-py (ADR 0036).
Two test extended hours (ADR 0038): a $1.00 buy marked eligible, accepted and canceled in any open session;
and, in pre-market or after-hours only, a buy of 1 AAPL priced through the latest IEX ask (read from
Alpaca's market data API), filled and sold back through the bid: that one trades a real paper share.
The tester's rows need the regular session for quotes. Each test places nothing, and fails, if the paper
account already holds an open AAPL order or an AAPL position, and afterwards cancels every open AAPL order
and closes any AAPL position. Startup reconciliation reads the account's orders and positions first; an
open order there that this slice does not map fails the start.
"""

import asyncio
import os
import time
from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import PositionSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import order_status_report
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import ClientId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType
from nautilus_trader.testkit import ExecTesterConfig
from nautilus_trader.trading import Strategy

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

AAPL = InstrumentId.from_str("AAPL.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
TESTER = StrategyId("ExecTester-000")
"""The tester's strategy ID; the cache also holds every AAPL order reconciliation loaded from the account."""
TOB_OFFSET_TICKS = 500_000
"""$50 at the adapter's 0.0001 increment: far from the market, so nothing fills; the spec's default, 500
ticks, would be five cents."""
STOP_LIMIT_OFFSET_TICKS = 500
"""Five cents between a stop-limit's trigger and its limit."""


class AcceptThenCancel(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.venue_order_id: str | None = None

    def on_start(self) -> None:
        order = self.order_factory.limit(
            AAPL,
            OrderSide.BUY,
            Quantity.from_int(1),
            Price.from_str("1.00"),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(name)
        if name == "OrderAccepted":
            self.venue_order_id = event.venue_order_id.value
            self.cancel_order(event.client_order_id)
        if name in {"OrderCanceled", "OrderRejected", "OrderDenied", "OrderCancelRejected"}:
            self.shutdown_system(f"Paper order ended with {name}")


class AcceptModifyCancel(Strategy):
    """A DAY limit buy of 1 AAPL at $1.00, modified to $1.01 once accepted, canceled once updated."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list = []

    def on_start(self) -> None:
        order = self.order_factory.limit(
            AAPL,
            OrderSide.BUY,
            Quantity.from_int(1),
            Price.from_str("1.00"),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderAccepted":
            self.modify_order(event.client_order_id, price=Price.from_str("1.01"))
        elif name in {"OrderUpdated", "OrderModifyRejected"}:
            # A refused modify leaves the order working: it is canceled too, so the run ends as it began.
            self.cancel_order(event.client_order_id)
        elif name in {"OrderCanceled", "OrderRejected", "OrderDenied", "OrderCancelRejected"}:
            self.shutdown_system(f"Paper order ended with {name}")

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


class ExtendedHoursOrders(Strategy):
    """Submit each `(side, price)` in `orders` in turn as a DAY limit order of 1 AAPL with
    `params={"extended_hours": True}`, the next once the last has filled; or, with `cancel`, cancel each
    once Alpaca accepts it. Stops when the last order ends, or on a rejection or denial."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: list[tuple[OrderSide, str]] = []
        self.cancel = False
        self.events: list = []

    def on_start(self) -> None:
        self._next()

    def _next(self) -> None:
        if not self.orders:
            self.shutdown_system("extended-hours orders done")
            return
        side, price = self.orders.pop(0)
        order = self.order_factory.limit(
            AAPL, side, Quantity.from_int(1), Price.from_str(price), time_in_force=TimeInForce.DAY
        )
        self.submit_order(order, params={"extended_hours": True})

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderAccepted" and self.cancel:
            self.cancel_order(event.client_order_id)
        elif (name == "OrderCanceled" and self.cancel) or (
            name == "OrderFilled" and not self.cancel
        ):
            self._next()
        elif (
            name in {"OrderCanceled", "OrderExpired", "OrderRejected", "OrderDenied"}
            or name == "OrderCancelRejected"
        ):
            self.shutdown_system(f"extended-hours order ended with {name}")

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


def alpaca_session(trading: TradingClient, now: datetime | None = None) -> str:
    """The session now, from Alpaca's own calendar (`GET /v2/calendar`): each trading date's
    `session_open`, `open`, `close` and `session_close` give "pre-market", "regular" and "after-hours";
    the 8 hours before a trading date's `session_open` are "overnight" (8:00pm-4:00am ET, "Extended Hours
    Trading"; the overnight session follows the NYSE holiday calendar, Alpaca's 24/5 page); anything else is
    "closed". Holidays and early closes come with the calendar."""
    now = (now or datetime.now(UTC)).astimezone(NEW_YORK)
    days = trading.get_calendar(
        GetCalendarRequest(
            start=(now - timedelta(days=1)).date(), end=(now + timedelta(days=5)).date()
        )
    )
    assert isinstance(days, list)

    def local(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=NEW_YORK) if value.tzinfo is None else value.astimezone(NEW_YORK)
        )

    def at(day: date, hhmm: str | None, default: datetime) -> datetime:
        # alpaca-py keeps `session_open` and `session_close` as "HHMM" strings for the trading date.
        if not hhmm:
            return default
        return datetime.combine(day, dt_time(int(hhmm[:2]), int(hhmm[2:])), NEW_YORK)

    for day in days:
        opens, closes = local(day.open), local(day.close)
        session_open = at(day.date, day.session_open, opens)
        session_close = at(day.date, day.session_close, closes)
        if session_open <= now < opens:
            return "pre-market"
        if opens <= now < closes:
            return "regular"
        if closes <= now < session_close:
            return "after-hours"
        if session_open - timedelta(hours=8) <= now < session_open:
            return "overnight"
    return "closed"


def latest_iex_quote(max_age: timedelta) -> tuple[Decimal, Decimal] | None:
    """The latest IEX bid and ask for AAPL, or None if either side is missing or older than `max_age`."""
    quote = with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    ).get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="AAPL", feed=DataFeed.IEX))
    assert isinstance(quote, dict)
    q = quote["AAPL"]
    bid, ask = Decimal(str(q.bid_price)), Decimal(str(q.ask_price))
    if not (bid > 0 and ask > 0) or datetime.now(UTC) - q.timestamp > max_age:
        return None
    return bid, ask


async def run_hosted(node) -> None:
    async with asyncio.timeout(60):
        await node.run_async()


def paper_node(exec_engine: LiveExecutionEngineConfig | None = None) -> LiveNode:
    return LiveNode.build(
        "CANON-ALPACA-PAPER-EXEC",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            exec_engine=exec_engine,
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )


def test_paper_limit_order_is_modified_through_a_node() -> None:
    """One modify through a real node (ADR 0042): `OrderUpdated` arrives with Alpaca's `replaced`, under the
    replacement's order ID and the new price, and the cancel after it reaches the replacement. It needs the
    regular session: Alpaca does not replace an order it has not routed (`accepted`)."""
    if alpaca_session(paper_trading()) != "regular":
        pytest.skip("needs the regular session: Alpaca replaces only an order it has routed")
    with aapl_left_as_found() as cleaned:
        node = paper_node()
        strategy = AcceptModifyCancel()
        node.add_strategy(strategy)
        try:
            asyncio.run(run_hosted(node))
        finally:
            node.dispose()

    # The events first: when they differ, they say why the clean-up had something to do.
    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingUpdate",
        "OrderUpdated",
        "OrderPendingCancel",
        "OrderCanceled",
    ], (strategy.names(), cleaned)
    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    accepted, updated, canceled = (strategy.events[i] for i in (1, 3, 5))
    assert updated.venue_order_id != accepted.venue_order_id
    assert updated.price == Price.from_str("1.0100")
    assert canceled.venue_order_id == updated.venue_order_id


def test_paper_modify_limit_buy_tc_e30() -> None:
    """TC-E30 ("Modify limit BUY price") through `ExecTester`, as the card configures it: the one-shot amend
    moves the limit "one tick more passive". The adapter's tick is 0.0001 (ADR 0030), so above $1.00 that
    price breaks Alpaca's sub-penny rule and the client denies the modify before Alpaca:
    `OrderModifyRejected`, not `OrderUpdated`. The order is then canceled on stop."""
    reasons: list[str] = []

    def modify_rejected(orders: list) -> bool:
        reasons[:] = [
            str(e.reason)
            for o in orders
            for e in o.events()
            if type(e).__name__ == "OrderModifyRejected"
        ]
        return bool(reasons)

    with aapl_left_as_found() as cleaned:
        events = run_exec_tester(
            ExecTesterConfig(
                instrument_id=AAPL,
                client_id=ClientId("ALPACA"),
                order_qty=Quantity.from_int(1),
                enable_limit_buys=True,
                enable_limit_sells=False,
                tob_offset_ticks=TOB_OFFSET_TICKS,
                limit_time_in_force=TimeInForce.DAY,
                modify_orders_to_maintain_tob_offset=True,
                trigger_limit_order_maintenance_once=True,
            ),
            count=1,
            settle=modify_rejected,
        )

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    (names,) = events.values()
    assert "OrderModifyRejected" in names, names
    assert any("has more than 2 decimals" in r for r in reasons), reasons
    assert names[-1] == "OrderCanceled", names


def test_paper_extended_hours_limit_order_rests_and_is_canceled() -> None:
    """A DAY limit buy of 1 AAPL at $1.00 marked `extended_hours` (ADR 0038), through a node: accepted and
    canceled, and Alpaca holds it as extended-hours eligible. Any session but a closed one."""
    trading = paper_trading()
    session = alpaca_session(trading)
    if session == "closed":
        pytest.skip("Alpaca's sessions are closed now")
    with aapl_left_as_found() as cleaned:
        node = paper_node()
        strategy = ExtendedHoursOrders()
        strategy.orders = [(OrderSide.BUY, "1.00")]
        strategy.cancel = True
        node.add_strategy(strategy)
        try:
            asyncio.run(run_hosted(node))
        finally:
            node.dispose()

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert strategy.names() == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ], (session, strategy.names())
    held = trading.get_order_by_id(strategy.events[1].venue_order_id.value)
    assert isinstance(held, AlpacaOrder)
    assert held.extended_hours is True, session


def test_paper_extended_hours_order_fills_and_is_closed() -> None:
    """In pre-market or after-hours: a DAY limit buy of 1 AAPL marked `extended_hours`, priced 0.2% through
    Alpaca's latest IEX ask so it fills, then a sale of that share priced 0.2% through the bid, both
    through a node (ADR 0038). Skipped in other sessions; overnight quotes come from another feed."""
    session = alpaca_session(paper_trading())
    if session not in ("pre-market", "after-hours"):
        pytest.skip(f"needs pre-market or after-hours; now {session}")
    quote = latest_iex_quote(timedelta(seconds=60))
    if quote is None:
        pytest.skip("no two-sided IEX quote from the last minute")
    bid, ask = quote
    cent = Decimal("0.01")
    buy = (ask * Decimal("1.002")).quantize(cent)
    sell = (bid * Decimal("0.998")).quantize(cent)
    with aapl_left_as_found() as cleaned:
        node = paper_node()
        strategy = ExtendedHoursOrders()
        strategy.orders = [(OrderSide.BUY, str(buy)), (OrderSide.SELL, str(sell))]
        node.add_strategy(strategy)
        try:
            asyncio.run(run_hosted(node))
        finally:
            node.dispose()

    assert cleaned == {"orders": 0, "positions": 0}, (session, cleaned, strategy.names())
    assert strategy.names().count("OrderFilled") >= 2, (session, strategy.names())
    assert "OrderRejected" not in strategy.names() and "OrderDenied" not in strategy.names()


def test_paper_account_query_matches_alpacas_cash() -> None:
    """A strategy's `query_account` through a node (ADR 0036): the client reads Alpaca's account again and the
    node's USD balance is Alpaca's `cash` to the cent, as `get_account` gives it directly while the node runs.
    Places nothing, and runs only while the account holds no AAPL order or position, as the others do."""
    with aapl_left_as_found() as cleaned:
        run_account_query()
    assert cleaned == {"orders": 0, "positions": 0}, cleaned


def run_account_query() -> None:

    class Idle(Strategy):
        def __init__(self) -> None:
            super().__init__()
            self.started = False

        def on_start(self) -> None:
            self.started = True

    node = paper_node()
    strategy = Idle()
    node.add_strategy(strategy)
    cache = node.cache
    totals: list[str] = []
    cash: list[Decimal] = []

    def node_account() -> Any:
        held = cache.account(ACCOUNT_ID)
        assert held is not None, "the node holds no ALPACA-PAPER account"
        return held

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())

        async def until(done: Callable[[], bool]) -> None:
            while not done():
                if task.done():
                    task.result()  # the node's own error, if it stopped with one
                    raise AssertionError("the node stopped before the account query was answered")
                await asyncio.sleep(0.1)

        try:
            async with asyncio.timeout(60):
                await until(lambda: strategy.started)
                before = len(node_account().events)
                strategy.query_account(ACCOUNT_ID, ClientId("ALPACA"))
                await until(lambda: len(node_account().events) > before)
                totals.append(str(node_account().balance().total))
                # Alpaca's account, read directly while the node still runs.
                direct = await asyncio.to_thread(paper_trading().get_account)
                assert isinstance(direct, TradeAccount)
                cash.append(Decimal(str(direct.cash)).quantize(Decimal("0.01")))
        finally:
            if not task.done():
                node.handle().stop()
            await asyncio.wait_for(asyncio.shield(task), 45)  # the node's stop is bounded too

    try:
        asyncio.run(main())
    finally:
        node.dispose()

    assert totals == [f"{cash[0]} USD"], (totals, cash)


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def open_aapl_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=["AAPL"]))
    assert isinstance(orders, list)
    return orders


def holds_aapl(trading: TradingClient) -> bool:
    try:
        trading.get_open_position("AAPL")
    except APIError as e:
        if e.status_code == 404:
            return False
        raise
    return True


def wait_until(done: Callable[[], bool], what: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds} s waiting for {what}")
        time.sleep(0.5)


def close_aapl(trading: TradingClient) -> None:
    """Close the AAPL position: Alpaca's close (a market order) in the regular session; outside it, a
    limit order marked `extended_hours`, priced 0.5% through the IEX quote, since a market order is held
    for the next regular session ("Orders not designated for extended hours execution will be queued")."""
    position = trading.get_open_position("AAPL")
    assert isinstance(position, AlpacaPosition)
    if alpaca_session(trading) == "regular":
        trading.close_position("AAPL")
        return
    quote = latest_iex_quote(timedelta(minutes=5))
    assert quote is not None, "no IEX quote to price the closing order with"
    bid, ask = quote
    long = position.side == PositionSide.LONG
    price = (bid * Decimal("0.995") if long else ask * Decimal("1.005")).quantize(Decimal("0.01"))
    trading.submit_order(
        LimitOrderRequest(
            symbol="AAPL",
            qty=abs(float(position.qty)),
            side=AlpacaOrderSide.SELL if long else AlpacaOrderSide.BUY,
            time_in_force=AlpacaTimeInForce.DAY,
            limit_price=float(price),
            extended_hours=True,
        )
    )


@contextmanager
def aapl_left_as_found() -> Generator[dict[str, int]]:
    """Refuse to start while the paper account holds an open AAPL order or an AAPL position, and leave it so:
    afterwards, whatever happened, cancel every open AAPL order and close any AAPL position. The account
    held none at the start and the paper lock keeps other sessions out, so whatever is found is this
    test's. What had to be cleaned up is counted, for the test to fail on."""
    trading = paper_trading()
    held = open_aapl_orders(trading)
    assert held == [], (
        "the paper account already holds open AAPL orders; placing nothing: "
        + ", ".join(
            f"{o.client_order_id} ({getattr(o.side, 'value', o.side)} {o.qty} at {o.limit_price}, "
            f"submitted {o.submitted_at})"
            for o in held
        )
    )
    assert not holds_aapl(trading), (
        "the paper account already holds an AAPL position; placing nothing"
    )
    cleaned = {"orders": 0, "positions": 0}
    try:
        yield cleaned
    finally:
        for order in open_aapl_orders(trading):
            cleaned["orders"] += 1
            try:
                trading.cancel_order_by_id(order.id)
            except APIError as e:
                # 422 "no longer cancelable": it closed between the listing and the cancel (the listing
                # can lag the stream). Anything else is raised once the rest is cleaned up.
                if e.status_code != 422:
                    raise
        wait_until(lambda: not open_aapl_orders(trading), "the canceled orders to close")
        if holds_aapl(trading):
            cleaned["positions"] += 1
            close_aapl(trading)
            wait_until(lambda: not holds_aapl(trading), "the AAPL position to close")


def test_paper_limit_order_is_accepted_and_canceled() -> None:
    with aapl_left_as_found() as cleaned:
        node = paper_node()
        strategy = AcceptThenCancel()
        node.add_strategy(strategy)
        try:
            asyncio.run(run_hosted(node))
            has_account = node.cache.account(ACCOUNT_ID) is not None
        finally:
            node.dispose()

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert has_account
    assert strategy.events == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    assert strategy.venue_order_id


def resting_buy(trading: TradingClient) -> AlpacaOrder:
    """A DAY limit buy of 1 AAPL at $1.00 placed with alpaca-py, once Alpaca lists it open."""
    placed = trading.submit_order(
        LimitOrderRequest(
            symbol="AAPL",
            qty=1,
            side=AlpacaOrderSide.BUY,
            time_in_force=AlpacaTimeInForce.DAY,
            limit_price=1.00,
        )
    )
    assert isinstance(placed, AlpacaOrder)
    order_id = placed.id
    wait_until(
        lambda: any(o.id == order_id for o in open_aapl_orders(trading)),
        "Alpaca to list the order open",
    )
    return placed


def test_paper_open_order_from_a_prior_session_is_reconciled_tc_e84() -> None:
    """TC-E84, "Reconcile open orders", of upstream's Execution Testing Spec: an open limit order left by a
    prior session, here placed with alpaca-py, and a node started with reconciliation on and no lookback.
    Pass: the order is loaded into the cache with its venue order ID, `ACCEPTED`, its price, quantity,
    side and type. The client reads open orders on their own, with no window, so neither a lookback nor
    the account's newest 500 orders can leave one out (ADR 0030). No strategy claims it, so it is
    `EXTERNAL`, as the card says; the clean-up cancels it, the one order it finds."""

    class StopOnStart(Strategy):
        def on_start(self) -> None:
            self.shutdown_system("reconciled")

    with aapl_left_as_found() as cleaned:
        placed = resting_buy(paper_trading())
        node = paper_node(LiveExecutionEngineConfig(reconciliation=True))
        cache = node.cache  # read through the cache taken before the run, as the flat test does
        node.add_strategy(StopOnStart())
        try:
            asyncio.run(run_hosted(node))
            loaded = [
                (o.status, o.price.as_decimal(), o.quantity.as_double(), o.side, o.order_type)
                for o in cache.orders()
                if o.venue_order_id is not None and o.venue_order_id.value == str(placed.id)
            ]
        finally:
            node.dispose()

    assert cleaned == {"orders": 1, "positions": 0}, cleaned
    assert loaded == [(OrderStatus.ACCEPTED, Decimal(1), 1.0, OrderSide.BUY, OrderType.LIMIT)]


def test_paper_cancel_of_a_canceled_order_tc_e44() -> None:
    """TC-E44, "Cancel already-canceled order", on the venue's side, through alpaca-py as the card's
    adapter-focused form allows (`Strategy.cancel_order` drops a cancel for a locally closed order): a
    $1.00 buy canceled, then canceled again once Alpaca shows it canceled. Alpaca's "Delete Order by ID"
    documents 422 for an order not cancelable; on paper on 2026-11-10, after hours, the second cancel was
    answered with no error (204), as the card allows "a documented idempotent result". Pass: the second
    cancel is answered 204 or refused 422, and the order still reads `canceled`. The client handles both:
    a 204 waits for the `canceled` update, a 422 cancels the order from Alpaca's read (ADR 0036)."""
    with aapl_left_as_found() as cleaned:
        trading = paper_trading()
        placed = resting_buy(trading)
        trading.cancel_order_by_id(placed.id)

        def status() -> object:
            held = trading.get_order_by_id(placed.id)
            assert isinstance(held, AlpacaOrder)
            return held.status

        wait_until(lambda: status() == AlpacaOrderStatus.CANCELED, "Alpaca to show it canceled")
        try:
            trading.cancel_order_by_id(placed.id)
            second: int | None = None
        except APIError as e:
            second = e.status_code
        after = status()

    print(f"TC-E44: the second cancel was answered {second or 204}")  # shown with -s
    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert second in (None, 422), second
    assert after == AlpacaOrderStatus.CANCELED


def test_paper_cancel_all_on_stop_tc_e41() -> None:
    """TC-E41, "Cancel all on stop", of upstream's Execution Testing Spec, driven by upstream's `ExecTester`
    with the spec's node setup (risk engine bypassed, reconciliation on). The tester places a limit buy
    `TOB_OFFSET_TICKS` below the best bid; on stop it sends `CancelAllOrders` (`strategy_only=false`,
    `crates/testkit/src/testers/exec/strategy.rs` at the pinned commit), which the client cancels order by
        order. Pass: each order ends `OrderCanceled`, and nothing was left for the cleanup. The row's limit
    sell is left out: from a flat account Alpaca refuses it, 403 "cannot open a short sell while a long
    buy order is open" (observed 2026-11-10), and the tester resubmits it on every quote. A stop buy
    `TOB_OFFSET_TICKS` above the ask is the second order instead."""
    with aapl_left_as_found() as cleaned:
        events = run_exec_tester(
            ExecTesterConfig(
                instrument_id=AAPL,
                client_id=ClientId("ALPACA"),
                order_qty=Quantity.from_int(1),
                enable_limit_buys=True,
                enable_limit_sells=False,  # see the docstring
                enable_stop_buys=True,  # the second order, in place of the sell
                stop_order_type=OrderType.STOP_MARKET,
                stop_offset_ticks=TOB_OFFSET_TICKS,
                stop_time_in_force=TimeInForce.DAY,
                tob_offset_ticks=TOB_OFFSET_TICKS,
                limit_time_in_force=TimeInForce.DAY,
            ),
            count=2,
        )

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert len(events) == 2, events
    # The card's sequence is `OrderPendingCancel` then `OrderCanceled`; for `CancelAllOrders` the engine
    # emits no `OrderPendingCancel` (observed) and the binding has no method for a client to, so the last
    # event is checked.
    for names in events.values():
        assert names[-1] == "OrderCanceled", names


@pytest.mark.parametrize(
    ("row", "order_type"),
    [("TC-E20", OrderType.STOP_MARKET), ("TC-E22", OrderType.STOP_LIMIT)],
)
def test_paper_stop_buy_is_accepted_with_its_prices(row: str, order_type: OrderType) -> None:
    """TC-E20 ("StopMarket BUY") and TC-E22 ("StopLimit BUY") of upstream's Execution Testing Spec, through
    `ExecTester`: a stop buy `TOB_OFFSET_TICKS` above the ask (a stop-limit's limit 5 cents above its
    trigger), which must not trigger. Pass: accepted with the trigger and limit prices the node sent, as
    Alpaca holds them; `order_status_report` of Alpaca's order, as for an order the node does not hold (a
    prior session's), has the same type and prices and trigger type `DEFAULT`, since Alpaca's order
    carries none; then canceled on stop. A stop the node holds is reported with the node's own trigger
    type, offline only (`test_a_held_stop_is_reported_with_the_trigger_type_the_node_sent`). A trigger
    and fill need the market to move $50 and are not tested (the card: "may not happen during the
    test")."""
    trading = paper_trading()
    seen: list[tuple[Any, AlpacaOrder]] = []

    def check(orders: list) -> None:
        for order in orders:
            held = trading.get_order_by_client_id(order.client_order_id.value)
            assert isinstance(held, AlpacaOrder)
            seen.append((order, held))

    with aapl_left_as_found() as cleaned:
        events = run_exec_tester(
            ExecTesterConfig(
                instrument_id=AAPL,
                client_id=ClientId("ALPACA"),
                order_qty=Quantity.from_int(1),
                enable_limit_buys=False,
                enable_limit_sells=False,
                enable_stop_buys=True,
                enable_stop_sells=False,
                stop_order_type=order_type,
                stop_offset_ticks=TOB_OFFSET_TICKS,
                stop_limit_offset_ticks=STOP_LIMIT_OFFSET_TICKS,
                stop_time_in_force=TimeInForce.DAY,
            ),
            count=1,
            check=check,
        )

    assert cleaned == {"orders": 0, "positions": 0}, (row, cleaned)
    assert len(events) == 1, (row, events)
    ((order, held),) = seen
    assert Decimal(str(held.stop_price)) == Decimal(str(order.trigger_price)), row
    report = order_status_report(held, 0)  # no trigger type given: an order the node does not hold
    assert report is not None
    assert (report.order_type, report.trigger_type) == (order_type, TriggerType.DEFAULT), row
    assert report.trigger_price == order.trigger_price, row
    if order_type == OrderType.STOP_LIMIT:
        assert Decimal(str(held.limit_price)) == Decimal(str(order.price)), row
        assert report.price == order.price, row
    for names in events.values():
        assert names[-1] == "OrderCanceled", (row, names)


def run_exec_tester(
    config: ExecTesterConfig,
    count: int,
    check: Callable[[list], None] | None = None,
    settle: Callable[[list], bool] | None = None,
) -> dict[str, list[str]]:
    """Run upstream's `ExecTester` on a node with the spec's setup until it has `count` orders accepted,
    pass them to `check` while they rest, then stop it (its stop cancels them), and return each of its
    orders' event names."""
    provider = InstrumentProviderConfig(load_ids=[str(AAPL)])
    node = (
        LiveNode.builder("CANON-ALPACA-PAPER-TESTER", TraderId("TESTER-001"), Environment.SANDBOX)
        .with_risk_engine_config(LiveRiskEngineConfig(bypass=True))
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=True))
        .with_delay_post_stop_secs(5)
        .with_timeout_connection(20)
        .with_timeout_disconnection_secs(15)
        .add_data_client(
            "ALPACA", AlpacaDataClientFactory, DataClientConfig(instrument_provider=provider)
        )
        .add_exec_client(
            "ALPACA",
            AlpacaExecutionClientFactory,
            AlpacaExecutionClientConfig(instrument_provider=provider),
        )
        .build()
    )
    node.add_builtin_strategy("ExecTester", config)
    events: dict[str, list[str]] = {}
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run
    reached: list[bool] = []

    async def main() -> None:
        # Wait for the accepts with a deadline of its own, then stop the node and let it finish: a timeout
        # around the whole run would cancel the node's own shutdown.
        task = asyncio.ensure_future(node.run_async())
        deadline = asyncio.get_running_loop().time() + 60
        try:
            while not task.done() and asyncio.get_running_loop().time() < deadline:
                orders = cache.orders(instrument_id=AAPL, strategy_id=TESTER)
                accepted = len(orders) == count and all(
                    o.status == OrderStatus.ACCEPTED for o in orders
                )
                if accepted and (settle is None or settle(orders)):
                    reached.append(True)
                    if check is not None:
                        check(orders)
                    break
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                node.handle().stop()
            await task

    try:
        asyncio.run(main())
        for order in cache.orders(instrument_id=AAPL, strategy_id=TESTER):
            events[order.client_order_id.value] = [type(e).__name__ for e in order.events()]
    finally:
        node.dispose()
    # The deadline running out is a failure, not a pass on whatever the stop then canceled.
    assert reached, f"the tester's {count} orders were not all accepted within 60 s: {events}"
    return events
