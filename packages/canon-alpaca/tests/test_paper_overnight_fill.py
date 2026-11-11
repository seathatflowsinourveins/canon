"""A paper test of an overnight fill through a real node: an extended-hours limit order priced from the boats
quote the node itself receives, filled, and the share sold back the same way (ADR 0038, ADR 0055).

* `test_paper_overnight_limit_fills_against_boats_and_is_closed`: the node subscribes AAPL quotes with
  `params={"feed": "boats"}`. On the first two-sided quote that is not crossed it submits a DAY limit buy of
  1 AAPL marked `extended_hours`, priced 0.2% through the ask; once that fills, a DAY limit sell of the share
  marked the same way, priced 0.2% through the latest bid. Overnight only (8:00pm-4:00am New York, from
  Alpaca's calendar), and only while AAPL's asset lists `overnight_tradable` in its `attributes` and not
  `overnight_halted`: the raw `GET /v2/assets/AAPL` carries no top-level `overnight_tradable` key, its
  `attributes` list does (paper, 2026-11-10).

Venue rules, from Alpaca's "24/5 Trading" page (https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md):
"Only `limit` orders are supported during the overnight session", time in force `day` or `gtc`; eligible
assets carry `overnight_tradable`, halted ones `overnight_halted`; boats is the overnight feed ("Blue Ocean,
overnight US trading data", the `feed` enum of https://docs.alpaca.markets/us/reference/stocklatestquotes-1.md).

Upstream's Execution Testing Spec (https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md)
has no row for this case. Its nearest, TC-E13 ("Limit IOC aggressive fill"), sends IOC, which the overnight
session does not take, and the spec gives no position-closing limit order (TC-E06, TC-E61 and TC-E82 close
with market orders). Upstream's `ExecTester` cannot run it either: v2.0.0rc8's `ExecTesterConfig` takes
`order_params` but no subscription params, so its quotes come from the default feed, IEX, which sends nothing
overnight. A small strategy subscribes instead, as `test_paper_feed.py` does for the Data Testing Spec's
TC-D71, and the order flow follows TC-E13's shape with a DAY limit.

If an order rests its whole wait unfilled, what Alpaca held for it is recorded and the test is reported
`xfail` with that record: an unfilled overnight order is a result, not a failure to hide. Anything else that
ends the round trip early fails, and so does an order Alpaca filled while the node ran whose fill never
reached the node.

It places nothing, and fails, if the paper account already holds an open AAPL order or an AAPL position, and
afterwards cancels every open AAPL order and closes any AAPL position with a limit order marked
`extended_hours`, priced 0.5% through the latest boats bid from Alpaca's market data API, or through the last
boats quote the node received when that call gives none (the record names which). It runs only with a paper
key pair in the environment, through `mise run paper:test`, which holds the paper lock.
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
from alpaca.trading.enums import PositionSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Asset as AlpacaAsset
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
        reason=(
            f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
            "to run this integration test"
        ),
    ),
    # each test opens a market-data stream, so it holds the login's stream lock (conftest.py, ADR 0069)
    pytest.mark.alpaca_stream,
]

AAPL = InstrumentId.from_str("AAPL.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
BOATS = {"feed": "boats"}
START_TIMEOUT_SECS = 60
QUOTE_WAIT_SECS = 30
"""How long the node waits for its first usable boats quote before the test fails."""
FILL_WAIT_SECS = 60
"""How long each order may rest before the node is stopped and what Alpaca held is recorded."""
QUOTE_MAX_AGE_SECS = 300
"""The oldest quote the cleanup prices a closing order from."""
THROUGH = Decimal("0.002")
CENT = Decimal("0.01")


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def overnight_now(trading: TradingClient) -> bool:
    """Whether now is in the overnight session: the 8 hours before a trading date's `session_open`, from
    Alpaca's calendar (`GET /v2/calendar`), as `test_paper_feed.overnight_now` reads it. A day with no
    `session_open` fails the test, naming it: this test trades, so it assumes no bound."""
    now = datetime.now(UTC).astimezone(NEW_YORK)
    days = trading.get_calendar(
        GetCalendarRequest(start=now.date(), end=(now + timedelta(days=5)).date())
    )
    assert isinstance(days, list)

    def session_open(day: date, hhmm: str | None) -> datetime:
        if not hhmm:
            pytest.fail(f"Alpaca's calendar gives {day} no session_open; nothing was placed")
        return datetime.combine(day, dt_time(int(hhmm[:2]), int(hhmm[2:])), NEW_YORK)

    for day in days:
        starts = session_open(day.date, day.session_open)
        if starts - timedelta(hours=8) <= now < starts:
            return True
    return False


def overnight_attributes(trading: TradingClient) -> list[str]:
    """AAPL's asset `attributes` (`GET /v2/assets/AAPL`), where `overnight_tradable` and `overnight_halted`
    appear."""
    asset = trading.get_asset("AAPL")
    assert isinstance(asset, AlpacaAsset)
    return list(asset.attributes or [])


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


def latest_boats_quote() -> tuple[Decimal, Decimal] | None:
    """The latest boats bid and ask for AAPL from Alpaca's market data API, or None when either side is
    missing, the quote is older than five minutes, or the call is refused."""
    client = with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    )
    try:
        quotes = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols="AAPL", feed=DataFeed.BOATS)
        )
    except APIError:
        return None
    assert isinstance(quotes, dict)
    q = quotes["AAPL"]
    bid, ask = Decimal(str(q.bid_price)), Decimal(str(q.ask_price))
    if not (bid > 0 and ask > 0) or datetime.now(UTC) - q.timestamp > timedelta(
        seconds=QUOTE_MAX_AGE_SECS
    ):
        return None
    return bid, ask


class OvernightRoundTrip(Strategy):
    """Subscribe to AAPL's boats quotes; buy 1 AAPL through the first usable ask, then sell it through the
    latest bid, each a DAY limit marked `extended_hours`. Records every order event and the quote each order
    was priced from; `done` once the sell fills or an order ends without filling."""

    def __init__(self) -> None:
        super().__init__()
        self.quotes = 0
        self.last: tuple[Decimal, Decimal] | None = None
        self.last_at: float | None = None
        self.priced: list[dict[str, Any]] = []
        self.events: list = []
        self.submitted_at: float | None = None
        self.done = False

    def on_start(self) -> None:
        self.subscribe_quotes(AAPL, params=BOATS)

    def on_quote(self, quote) -> None:
        self.quotes += 1
        bid, ask = Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
        if not (bid > 0 and ask > 0) or bid > ask:
            return
        self.last, self.last_at = (bid, ask), time.monotonic()
        if not self.priced:
            self._submit(OrderSide.BUY, (ask * (1 + THROUGH)).quantize(CENT))

    def _submit(self, side: OrderSide, price: Decimal) -> None:
        assert self.last is not None
        self.priced.append(
            {"side": side.name, "price": price, "bid": self.last[0], "ask": self.last[1]}
        )
        order = self.order_factory.limit(
            AAPL,
            side,
            Quantity.from_int(1),
            Price.from_str(str(price)),
            time_in_force=TimeInForce.DAY,
        )
        self.submitted_at = time.monotonic()
        self.submit_order(order, params={"extended_hours": True})

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(event)
        if name == "OrderFilled" and event.order_side == OrderSide.BUY:
            assert self.last is not None
            self._submit(OrderSide.SELL, (self.last[0] * (1 - THROUGH)).quantize(CENT))
        elif name in {
            "OrderFilled",
            "OrderCanceled",
            "OrderExpired",
            "OrderRejected",
            "OrderDenied",
        }:
            # The sell filled, or an order ended without filling.
            self.done = True

    def names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]

    def fills(self) -> list[dict[str, Any]]:
        return [
            {
                "side": e.order_side.name,
                "last_px": Decimal(str(e.last_px)),
                "last_qty": Decimal(str(e.last_qty)),
                "venue_order_id": e.venue_order_id.value,
            }
            for e in self.events
            if type(e).__name__ == "OrderFilled"
        ]

    def recent_quote(self) -> tuple[Decimal, Decimal] | None:
        """The last usable quote if it is under five minutes old, as `latest_boats_quote` bounds its own."""
        if self.last_at is None or time.monotonic() - self.last_at > QUOTE_MAX_AGE_SECS:
            return None
        return self.last

    def venue_order_ids(self) -> list[str]:
        ids: list[str] = []
        for e in self.events:
            venue = getattr(e, "venue_order_id", None)
            if venue is not None and venue.value not in ids:
                ids.append(venue.value)
        return ids


def paper_node(strategy: Strategy) -> LiveNode:
    provider = InstrumentProviderConfig(load_ids=[str(AAPL)])
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-OVERNIGHT",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=2,  # lets the unsubscribe on stop reach the client
            data_clients={"ALPACA": DataClientConfig(instrument_provider=provider)},
            exec_clients={"ALPACA": AlpacaExecutionClientConfig(instrument_provider=provider)},
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
    node.add_strategy(strategy)
    return node


def run_round_trip(node: LiveNode, strategy: OvernightRoundTrip) -> str:
    """Run the node until the strategy is done, it has had no usable quote for `QUOTE_WAIT_SECS`, or an
    order has rested `FILL_WAIT_SECS`; then stop it through its handle. Returns why it stopped."""
    handle = node.handle()
    why = "done"

    async def main() -> None:
        nonlocal why
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        try:
            started = loop.time() + START_TIMEOUT_SECS
            while not task.done() and not handle.is_running and loop.time() < started:
                await asyncio.sleep(0.25)
            assert handle.is_running or task.done(), (
                f"the node did not start in {START_TIMEOUT_SECS} s"
            )
            quote_by = loop.time() + QUOTE_WAIT_SECS
            while not task.done() and not strategy.done:
                if not strategy.priced and loop.time() > quote_by:
                    why = (
                        f"no usable boats quote in {QUOTE_WAIT_SECS} s ({strategy.quotes} received)"
                    )
                    break
                rested = strategy.submitted_at
                if rested is not None and time.monotonic() - rested > FILL_WAIT_SECS:
                    why = f"the {strategy.priced[-1]['side']} rested {FILL_WAIT_SECS} s unfilled"
                    break
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                handle.stop()
            await task

    asyncio.run(main())
    return why


def close_aapl(trading: TradingClient, fallback: tuple[Decimal, Decimal] | None) -> str:
    """Close the AAPL position with a DAY limit marked `extended_hours`, priced 0.5% through the latest boats
    quote under five minutes old; returns where that quote came from."""
    position = trading.get_open_position("AAPL")
    assert isinstance(position, AlpacaPosition)
    quote, source = latest_boats_quote(), "market data API, feed boats"
    if quote is None:
        quote, source = fallback, "the node's last boats quote"
    assert quote is not None, (
        "no boats quote under five minutes old to price the closing order with"
    )
    bid, ask = quote
    long = position.side == PositionSide.LONG
    price = (bid * Decimal("0.995") if long else ask * Decimal("1.005")).quantize(CENT)
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
    return f"{source}, at {price}"


@contextmanager
def aapl_left_as_found(
    trading: TradingClient, strategy: OvernightRoundTrip
) -> Generator[dict[str, Any]]:
    """Refuse to start while the paper account holds an open AAPL order or an AAPL position, and leave it so:
    afterwards, whatever happened, cancel every open AAPL order and close any AAPL position. The paper lock
    keeps other sessions out, so whatever is found is this test's. What had to be cleaned up is counted.
    Each step runs even when one before it failed; what failed is raised once all have run. A closing order
    that has not filled in 60 s is canceled, so nothing is left working, and the position still held is
    named in the failure."""
    held = open_aapl_orders(trading)
    assert held == [], (
        f"the paper account already holds {len(held)} open AAPL order(s); placing nothing"
    )
    assert not holds_aapl(trading), (
        "the paper account already holds an AAPL position; placing nothing"
    )
    cleaned: dict[str, Any] = {"orders": 0, "positions": 0}
    try:
        yield cleaned
    finally:
        failures: list[str] = []

        def cancel_open() -> None:
            for order in open_aapl_orders(trading):
                try:
                    trading.cancel_order_by_id(order.id)
                except APIError as e:
                    # 422 "no longer cancelable": it closed between the listing and the cancel.
                    if e.status_code != 422:
                        failures.append(f"cancel of {order.id}: {e}")
            try:
                wait_until(lambda: not open_aapl_orders(trading), "the canceled orders to close")
            except AssertionError as e:
                failures.append(str(e))

        cleaned["orders"] = len(open_aapl_orders(trading))
        cancel_open()
        if holds_aapl(trading):
            cleaned["positions"] += 1
            try:
                cleaned["closed_with"] = close_aapl(trading, strategy.recent_quote())
                wait_until(lambda: not holds_aapl(trading), "the AAPL position to close", 60.0)
            except AssertionError as e:
                failures.append(str(e))
                cancel_open()
        cleaned["flat"] = not holds_aapl(trading) and not open_aapl_orders(trading)
        if failures or not cleaned["flat"]:
            raise AssertionError(f"cleanup left the account {cleaned}: {failures}")


def alpaca_record(trading: TradingClient, venue_order_id: str) -> dict[str, Any]:
    """What Alpaca holds for an order, as `GET /v2/orders/{id}` answers."""
    order = trading.get_order_by_id(venue_order_id)
    assert isinstance(order, AlpacaOrder)
    return {
        "id": venue_order_id,
        "side": getattr(order.side, "value", order.side),
        "status": getattr(order.status, "value", order.status),
        "limit_price": order.limit_price,
        "filled_qty": order.filled_qty,
        "filled_avg_price": order.filled_avg_price,
        "extended_hours": order.extended_hours,
        "submitted_at": order.submitted_at,
        "filled_at": order.filled_at,
        "canceled_at": order.canceled_at,
    }


def test_paper_overnight_limit_fills_against_boats_and_is_closed() -> None:
    trading = paper_trading()
    if not overnight_now(trading):
        pytest.skip(
            "the overnight session is closed now (8:00pm-4:00am New York, Alpaca's calendar)"
        )
    attributes = overnight_attributes(trading)
    if "overnight_tradable" not in attributes or "overnight_halted" in attributes:
        pytest.skip(f"AAPL is not overnight tradable now: asset attributes {attributes}")

    strategy = OvernightRoundTrip()
    with aapl_left_as_found(trading, strategy) as cleaned:
        node = paper_node(strategy)
        try:
            why = run_round_trip(node, strategy)
        finally:
            stopped = datetime.now(UTC)
            node.dispose()

    record = {
        "stopped": why,
        "events": strategy.names(),
        "priced": strategy.priced,
        "fills": strategy.fills(),
        "alpaca": [alpaca_record(trading, i) for i in strategy.venue_order_ids()],
        "cleaned": cleaned,
    }
    print(f"PAPER-OVERNIGHT {record}")

    assert cleaned["flat"], record
    assert strategy.priced, record
    assert "OrderRejected" not in record["events"] and "OrderDenied" not in record["events"], record
    assert all(a["extended_hours"] is True for a in record["alpaca"]), record
    # Alpaca's own record against the node's: an order Alpaca filled before the node stopped must have
    # reached the node as a fill, or the adapter lost it.
    reached = {f["venue_order_id"] for f in record["fills"]}
    lost = [
        a
        for a in record["alpaca"]
        if a["filled_at"] is not None and a["filled_at"] < stopped and a["id"] not in reached
    ]
    assert not lost, ("Alpaca filled these and the node saw no fill", lost, record)
    fills = record["fills"]
    for side, index in (("BUY", 0), ("SELL", 1)):
        if any(f["side"] == side for f in fills):
            continue
        # Only an order that rested its whole wait unfilled is a result; anything else that stopped the
        # round trip (a cancel or expiry from Alpaca, the node ending early) fails, with the record.
        rested = len(strategy.priced) == index + 1 and why.endswith("unfilled")
        assert rested, (f"the {side} neither filled nor rested its wait", record)
        pytest.xfail(f"the overnight {side} rested {FILL_WAIT_SECS} s unfilled: {record}")
    buy = next(f for f in fills if f["side"] == "BUY")
    assert buy["last_px"] <= strategy.priced[0]["price"], record
    sell = next(f for f in fills if f["side"] == "SELL")
    assert sell["last_px"] >= strategy.priced[1]["price"], record
    assert cleaned["orders"] == 0 and cleaned["positions"] == 0, record
