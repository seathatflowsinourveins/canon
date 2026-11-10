"""Upstream's built-in `EmaCross` trading one share of SPY in real time on Alpaca's paper environment, under the
risk limits of `canon_trading.paper_node` (ADR 0037).

It runs only when a paper key pair is in the environment (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`); otherwise
pytest reports it skipped with that reason. Run it with `mise run paper:test`, which holds the paper
account's lock. Never write the keys to a file.

What it calls: Alpaca's paper Trading API and trade updates stream, and the IEX quote stream, through the
adapter (`TradingClient(paper=True)`, "Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md).

The run. The node is `ema_cross_paper_node(SPY)`: the adapter's data and execution clients and `EmaCross`
at one share an order, with the risk engine on and this test's own submit budget, `RUN_SUBMIT_RATE`, so
one order reaches Alpaca and the engine denies the rest with `RATE_LIMIT_EXCEEDED`. A probe strategy beside it sends one order on its first
quote that the notional cap must deny before Alpaca sees it: a sell limit of one share at $1,000.01, over
the $1,000 cap; were the cap not applied it would rest about $250 above the market and could not fill. The
test stops the node at the first of: an `EmaCross` order filled, `DEADLINE_SECS` gone, or the strategy's
net position over one share, which fails the test, as does Alpaca holding more than one share at stop.
`EmaCross` does not flatten on stop, so a filled order leaves one share, long or short, which the clean-up
closes outside the node. In the regular session a fill is required; outside it the market order waits at
Alpaca for the open, is canceled by the clean-up, and the run says the session was closed.

The account is left as it was found: the test places nothing, and fails, if the account holds an open SPY
order or a SPY position; afterwards, whatever happened, it cancels every open SPY order and closes any SPY
position, waiting for the cancels before the close, then fails naming whatever is left. Startup reconciliation reads the account's orders and
positions first; an open order there that the adapter does not map fails the start.

What it prints, for ADR 0037's Confirmation: orders placed, fills, denials and their reasons, seconds run.
"""

import asyncio
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from canon_trading.paper_node import EMA_CROSS_ID
from canon_trading.paper_node import MAX_NOTIONAL_PER_ORDER_USD
from canon_trading.paper_node import ema_cross_paper_node

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

SPY = InstrumentId.from_str("SPY.ALPACA")
SYMBOL = "SPY"
DEADLINE_SECS = 240
"""The hard limit on the run, start-up included."""
RUN_SUBMIT_RATE = "1/00:05:00"
"""This run's own submit budget, tighter than the ceiling: one order reaches Alpaca, so the position is at
most one share (ADR 0037). Upstream's `EmaCross` has no position check and assumes its orders alternate;
the first run without this budget built a 4-share short in under two seconds."""
PROBE_PRICE = Price.from_str(f"{MAX_NOTIONAL_PER_ORDER_USD + Decimal('0.01')}")
"""One cent over the cap for one share. The cap is sized for SPY near $750 (ADR 0037): one share passes,
two do not, and the probe rests far above the market."""


class NotionalProbe(Strategy):
    """On its first quote, send the one order the notional cap must deny, and keep its events."""

    def __init__(self) -> None:
        super().__init__(
            StrategyConfig(strategy_id=StrategyId("NotionalProbe-002"), order_id_tag="002")
        )
        self.sent = False
        self.ask: Decimal | None = None
        self.events: list[str] = []
        self.denied_reason: str | None = None

    def on_start(self) -> None:
        self.subscribe_quotes(SPY)

    def on_quote(self, quote) -> None:
        if self.sent:
            return
        self.sent = True
        # The probe is safe only while it is not marketable: were the cap not applied, it must rest.
        self.ask = quote.ask_price.as_decimal()
        if self.ask >= PROBE_PRICE.as_decimal():
            self.events.append("not sent: the ask is at or over the probe's price")
            return
        order = self.order_factory.limit(
            SPY, OrderSide.SELL, Quantity.from_int(1), PROBE_PRICE, time_in_force=TimeInForce.DAY
        )
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(name)
        if name == "OrderDenied":
            self.denied_reason = str(event.reason)

    def on_stop(self) -> None:
        self.unsubscribe_quotes(SPY)


def paper_trading() -> TradingClient:
    return TradingClient(
        api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
    )


def open_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
    assert isinstance(orders, list)
    return orders


def position_qty(trading: TradingClient) -> int:
    """The signed SPY quantity Alpaca holds, 0 when it holds none (404); `side` gives the sign."""
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return 0
        raise
    assert isinstance(position, AlpacaPosition)
    qty = abs(int(Decimal(str(position.qty))))
    return -qty if getattr(position.side, "value", position.side) == "short" else qty


def settled(trading: TradingClient, done, seconds: float = 30) -> bool:
    """Poll Alpaca until `done(trading)` or `seconds` have gone."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done(trading):
            return True
        time.sleep(0.5)
    return done(trading)


@dataclass
class Found:
    """What the clean-up found at Alpaca after the run, and what it did."""

    position_at_stop: int = 0
    orders_canceled: int = 0
    closed: bool = False


@contextmanager
def left_as_found() -> Generator[Found]:
    """Refuse to start while the account holds an open SPY order or a SPY position; afterwards, whatever
    happened, read the position the run left at Alpaca, cancel every open SPY order and wait until Alpaca
    holds none, close any SPY position, outside the node and its risk budget, and wait for the close. A
    close that does not fill in time (outside the session it waits for the open) is canceled too, and the
    test fails naming what is left. The paper lock keeps other sessions out, so whatever is found then is
    this run's. Every step runs even if an earlier one raised."""
    trading = paper_trading()
    assert open_orders(trading) == [], (
        "the paper account already holds open SPY orders; placing nothing"
    )
    assert position_qty(trading) == 0, (
        "the paper account already holds a SPY position; placing nothing"
    )
    found = Found()
    try:
        yield found
    finally:
        errors: list[str] = []
        try:
            found.position_at_stop = position_qty(trading)
        except APIError as e:
            errors.append(f"reading the position: {e}")
        for order in open_orders(trading):
            try:
                trading.cancel_order_by_id(order.id)
                found.orders_canceled += 1
            except APIError as e:  # an order that filled or ended meanwhile
                errors.append(f"canceling {order.client_order_id}: {e}")
        # A held order keeps its shares from a close (403), so the cancels settle first.
        settled(trading, lambda t: not open_orders(t))
        try:
            if position_qty(trading) != 0:
                trading.close_position(SYMBOL)
                found.closed = True
                settled(trading, lambda t: position_qty(t) == 0 and not open_orders(t))
        except APIError as e:
            errors.append(f"closing the position: {e}")
        for order in open_orders(trading):  # a close that could not fill
            trading.cancel_order_by_id(order.id)
            errors.append(f"canceled the unfilled close {order.client_order_id}")
        left = [f"open order {o.client_order_id}" for o in open_orders(trading)]
        if qty := position_qty(trading):
            left.append(f"position of {qty} {SYMBOL}")
        assert not left, f"the paper account was not left as found; still there: {left}; {errors}"


def net_position(cache) -> int:
    return sum(
        int(p.signed_qty) for p in cache.positions_open(instrument_id=SPY, strategy_id=EMA_CROSS_ID)
    )


def test_paper_ema_cross_trades_one_share_under_the_risk_limits() -> None:
    node = ema_cross_paper_node(SPY, max_order_submit_rate=RUN_SUBMIT_RATE)
    probe = NotionalProbe()
    node.add_strategy(probe)
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run
    stopped_by = "deadline"
    peak = 0

    async def main() -> None:
        nonlocal stopped_by, peak
        task = asyncio.ensure_future(node.run_async())
        deadline = asyncio.get_running_loop().time() + DEADLINE_SECS
        try:
            while not task.done() and asyncio.get_running_loop().time() < deadline:
                peak = max(peak, abs(net_position(cache)))
                if peak > 1:
                    stopped_by = "position over one share"
                    break
                orders = cache.orders(instrument_id=SPY, strategy_id=EMA_CROSS_ID)
                if any(o.status == OrderStatus.FILLED for o in orders):
                    stopped_by = "first fill"
                    break
                await asyncio.sleep(0.05)
            else:
                if task.done():
                    stopped_by = "the node ended by itself"
        finally:
            if not task.done():
                node.handle().stop()
            await task
            peak = max(peak, abs(net_position(cache)))

    session_open = bool(getattr(paper_trading().get_clock(), "is_open", False))
    started = time.monotonic()
    with left_as_found() as found:
        try:
            asyncio.run(main())
            seconds = time.monotonic() - started
            orders = cache.orders(instrument_id=SPY, strategy_id=EMA_CROSS_ID)
            events = {
                o.client_order_id.value: [type(e).__name__ for e in o.events()] for o in orders
            }
            reasons = {
                o.client_order_id.value: str(e.reason)
                for o in cache.orders(instrument_id=SPY)
                for e in o.events()
                if type(e).__name__ in {"OrderDenied", "OrderRejected"}
            }
        finally:
            node.dispose()

    reached = [cid for cid, names in events.items() if "OrderSubmitted" in names]
    fills = sum(names.count("OrderFilled") for names in events.values())
    print(
        f"\nEmaCross paper run (regular session {'open' if session_open else 'closed'}): stopped by {stopped_by} after {seconds:.0f} s; "
        f"{len(orders)} orders, {len(reached)} sent to Alpaca, {fills} fills, peak position {peak}; "
        f"denials and rejections {reasons}; probe {probe.events}; account {found}; events {events}"
    )
    assert peak <= 1 and abs(found.position_at_stop) <= 1, (stopped_by, found, events)
    # The cap the running node applies is the configured one, and it stops the order before Alpaca.
    assert probe.events == ["OrderDenied"], probe.events
    assert probe.denied_reason is not None
    assert f"max={MAX_NOTIONAL_PER_ORDER_USD:.2f} USD," in probe.denied_reason, probe.denied_reason
    assert probe.denied_reason.startswith("NOTIONAL_EXCEEDS_MAX_PER_ORDER")
    # The run's budget lets one order reach Alpaca; the engine denies the rest for the rate.
    assert orders, f"EmaCross placed no order in {DEADLINE_SECS} s"
    assert len(reached) == 1, events
    # A fill needs the regular session; outside it the market order waits at Alpaca and is canceled.
    if session_open:
        assert stopped_by == "first fill" and fills == 1, (stopped_by, events)
    for cid, names in events.items():
        if cid not in reached:
            assert names == ["OrderInitialized", "OrderDenied"], events
            assert reasons[cid] == "RATE_LIMIT_EXCEEDED", reasons
