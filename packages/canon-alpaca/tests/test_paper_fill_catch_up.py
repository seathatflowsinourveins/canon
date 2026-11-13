"""Paper test for ADR 0075: how many reads of Alpaca's fill activities the client's catch-up needs, and how
long after a fill's `transaction_time` the first read lists it.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`, and
when `CANON_PAPER_MEASURE_CATCH_UP=1` asks for it: it takes up to `ORDERS` orders and about ten minutes, so a
routine `mise run paper:test` skips it. Run it with
`CANON_PAPER_MEASURE_CATCH_UP=1 mise run paper:test -- -s -k test_paper_fill_catch_up_reads_are_counted`,
which holds the paper account's lock. It needs the regular session, from 09:35 New York (at the open on
2026-11-11 paper did not make market orders final within 60 s) until `LATEST_BEFORE_CLOSE` before the close,
checked before each order. Never write the keys to a file.

What it does, on Alpaca's paper Trading API only (`paper=True` throughout): a node with the execution client
places `ORDERS` DAY limit orders of 1 AAPL priced to fill, `SPACING_SECS` apart, each a buy when Alpaca shows
no AAPL position and a sell of the one share when it shows one, so the account is never short. Each is
priced `THROUGH` past the latest IEX quote, read once per order from Alpaca's market data API with alpaca-py's
`StockHistoricalDataClient` (a GET, no stream): the node has no data client, and NautilusTrader's risk engine
denies a market order it has no price for (`MARKET_PRICE_UNAVAILABLE`, seen on paper 2026-11-13), which a
limit order's own price avoids. For each fill, a hook
in this test hands the client the stream's trade update without its `execution_id`: the client cannot apply
the fill from the stream ("a fill without one is logged, and the order's fill activities bring it", package
README), the update's order shows more filled than the node holds, and the client's catch-up reads Alpaca's
real fill activities (`_catch_up`, then `_catch_up_later`, `FILL_READ_TRIES` reads `FILL_READ_RETRY_SECS`
apart). Nothing in the adapter changes; the hook, the read log and the counts live here.

Also recorded per order: each time a report on it went through the client's hold for an ended order
(`_hold_report`), how that hold ended, and each report settled there (`_finished_report`) with what it showed
filled and how many fills came with it. The summary counts the orders with a hold and with a held report that
showed a fill: whether a fill is reported through the hold on the real venue is an observation, not asserted.

Recorded per order, and printed as one JSON line each (run with `-s`): the read that brought the fill (its
"caught up on read n of 3" line, so n - 1 extra reads), or that the reads ran out; and the seconds from the
fill's `transaction_time` to the start of the first read of the activities that listed its trade ID (for a
fill whose reads ran out, from the stream's timestamp, which paper showed equal to `transaction_time`, to the
last read, a lower bound). A summary line gives the distribution: read 1, 2 or 3, and ran out. The test fails
if an order does not fill at Alpaca (and places no more), if the window closes before the last order, if a
fill gets no outcome, or if a fill caught up is not held under Alpaca's trade ID; reads that ran out are
counted and printed, not failed, since they are the measurement (ADR 0075's Confirmation; a run with any
goes to the lead before the constant changes).

The account is left as found: the test places nothing if the account holds any open order or an AAPL
position, and afterwards, pass or fail, cancels its orders still open and sells any AAPL share it holds.
"""

import asyncio
import json
import os
import time
from collections import Counter
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import canon_alpaca.execution
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Clock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

MEASURE_ENV = "CANON_PAPER_MEASURE_CATCH_UP"
pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
        reason=(
            f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
            "to run this integration test"
        ),
    ),
    pytest.mark.skipif(
        os.environ.get(MEASURE_ENV) != "1",
        reason=f"a measurement of up to ten minutes; set {MEASURE_ENV}=1 to run it",
    ),
]

SYMBOL = "AAPL"
AAPL = InstrumentId.from_str(f"{SYMBOL}.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
ORDERS = 20
"""The lead's figure (2026-11-11): twenty one-share orders give a distribution of the reads."""
SPACING_SECS = 15.0
"""Between one order's outcome and the next order, so the fills are spread over the session's minutes."""
FILL_WAIT_SECS = 60.0
"""For Alpaca to show an order filled: the fill tests' own wait, not lengthened (2026-11-11)."""
OUTCOME_WAIT_SECS = 30.0
"""For the client's catch-up to bring the fill or run out: its reads take
`FILL_READ_TRIES * FILL_READ_RETRY_SECS` (9 s at 3 and 3 s) and each read Alpaca's answer time."""
THROUGH = Decimal("0.005")
"""How far past the quote an order is priced so that it fills: a buy over the ask, a sell under the bid."""
QUOTE_MOST = timedelta(minutes=1)
"""The oldest IEX quote an order is priced on."""
EARLIEST = (9, 35)
LATEST_BEFORE_CLOSE = timedelta(minutes=10)
"""Checked before each order: one order's fill wait, outcome wait and spacing are under two minutes."""


class Placer(Strategy):
    """Places what the test asks, and keeps every order event."""

    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.events: list = []

    def on_start(self) -> None:
        self.started = True

    def place(self, side: OrderSide, price: str) -> ClientOrderId:
        order = self.order_factory.limit(
            AAPL, side, Quantity.from_int(1), Price.from_str(price), time_in_force=TimeInForce.DAY
        )
        self.submit_order(order)
        return order.client_order_id

    def on_order_event(self, event) -> None:
        self.events.append(event)


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def priced_to_fill(side: OrderSide) -> str | None:
    """A limit price in cents `THROUGH` past the latest IEX quote for AAPL, or None when a side of the
    quote is missing or it is older than `QUOTE_MOST`. One GET of Alpaca's market data API; no stream."""
    quote = with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    ).get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=SYMBOL, feed=DataFeed.IEX))
    assert isinstance(quote, dict)
    q = quote[SYMBOL]
    bid, ask = Decimal(str(q.bid_price)), Decimal(str(q.ask_price))
    if not (bid > 0 and ask > 0) or datetime.now(UTC) - q.timestamp > QUOTE_MOST:
        return None
    cent = Decimal("0.01")
    if side == OrderSide.BUY:
        return str((ask * (1 + THROUGH)).quantize(cent, rounding=ROUND_CEILING))
    return str((bid * (1 - THROUGH)).quantize(cent, rounding=ROUND_FLOOR))


def aapl_shares(trading: TradingClient) -> int:
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return 0
        raise
    return int(Decimal(str(getattr(position, "qty", 0))))


def open_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    assert isinstance(orders, list)
    return orders


def wait_until(done, what: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds:.0f} s waiting for {what}")
        time.sleep(0.5)


def in_window(trading: TradingClient) -> str | None:
    """Why the test cannot run now, or None: Alpaca's clock must show the regular session open, from 09:35
    New York, and its close at least `LATEST_BEFORE_CLOSE` away."""
    clock = trading.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    now = datetime.now(UTC)
    local = now.astimezone(NEW_YORK)
    if not clock.is_open:
        return "the regular session is closed"
    if (local.hour, local.minute) < EARLIEST:
        return "before 09:35 New York, when paper did not make market orders final in time"
    if clock.next_close - now < LATEST_BEFORE_CLOSE:
        return f"the session closes within {LATEST_BEFORE_CLOSE}"
    return None


def build_node(strategy: Placer, clients: list[AlpacaExecutionClient]) -> LiveNode:
    class Capturing(AlpacaExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaExecutionClientFactory.create(**kwargs)
            clients.append(client)
            return client

    node = LiveNode.build(
        "CANON-ALPACA-PAPER-CATCH-UP",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        exec_factories={"ALPACA": Capturing},
    )
    node.add_strategy(strategy)
    return node


def instrument(client: AlpacaExecutionClient, measured: dict[str, dict[str, Any]]) -> None:
    """Hand the client each fill update of the test's orders without its `execution_id`, and record every
    read of the fill activities and the client's catch-up outcome. Instance attributes only: the adapter's
    code is unchanged."""
    apply, read = client._apply, client._fill_activities
    caught, ran_out = client._caught_up, client._ran_out
    hold, finished = client._hold_report, client._finished_report

    async def logged_hold(client_order_id, request):
        entry = measured.get(str(client_order_id))
        if entry is not None:
            entry["holds"] = entry.get("holds", 0) + 1
        try:
            return await hold(client_order_id, request)
        except BaseException as e:
            if entry is not None:
                entry.setdefault("hold_errors", []).append(type(e).__name__)
            raise

    async def logged_finished(report, companion_fills=None, *, deliver=False):
        entry = measured.get(str(report.client_order_id))
        if entry is not None:
            entry.setdefault("held_reports", []).append(
                {
                    "shown_filled": str(report.filled_qty),
                    "fills_with_it": len(companion_fills or ()),
                    "deliver": deliver,
                }
            )
        return await finished(report, companion_fills, deliver=deliver)

    async def withheld(update: TradeUpdate) -> None:
        event = getattr(update.event, "value", update.event)
        cid = str(update.order.client_order_id)
        if cid in measured and event in ("fill", "partial_fill") and update.execution_id:
            entry = measured[cid]
            entry["order_id"] = str(update.order.id)
            trade_ids = entry.setdefault("trade_ids", [])
            if str(update.execution_id) not in trade_ids:  # a redelivered update is one fill
                trade_ids.append(str(update.execution_id))
            entry.setdefault("stream_at", update.timestamp.astimezone(UTC).isoformat())
            update = update.model_copy(update={"execution_id": None})
        await apply(update)

    async def logged_read(start: int | None, order_ids: set[str] | None = None) -> list:
        at = datetime.now(UTC)  # the read's request, not its last page
        activities = await read(start, order_ids)
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            trade_id = str(activity.get("id", "")).partition("::")[2]
            for entry in measured.values():
                if trade_id in entry.get("trade_ids", ()) and "listed_at" not in entry:
                    entry["listed_at"] = at.isoformat()
                    entry["transaction_time"] = activity.get("transaction_time")
        for order_id in order_ids or ():
            for entry in measured.values():
                if entry.get("order_id") == order_id:
                    entry["reads"] = entry.get("reads", 0) + 1
                    entry["last_read_at"] = at.isoformat()
        return activities

    def logged_caught(client_order_id, read, shown, held) -> None:
        entry = measured.get(str(client_order_id))
        if entry is not None:
            entry["outcome"] = f"read {read}"
        caught(client_order_id, read, shown, held)

    def logged_ran_out(client_order_id, shown, held, follows) -> None:
        entry = measured.get(str(client_order_id))
        if entry is not None:
            entry["outcome"] = "ran out"
        ran_out(client_order_id, shown, held, follows)

    client._apply = withheld
    client._fill_activities = logged_read
    client._caught_up = logged_caught
    client._ran_out = logged_ran_out
    client._hold_report = logged_hold
    client._finished_report = logged_finished


def seconds_between(earlier: str | None, later: str | None) -> float | None:
    if not earlier or not later:
        return None
    first = datetime.fromisoformat(earlier)
    second = datetime.fromisoformat(later)
    return round((second - first).total_seconds(), 3)


def test_paper_fill_catch_up_reads_are_counted() -> None:
    trading = paper_trading()
    why = in_window(trading)
    if why is not None:
        pytest.skip(why)
    assert open_orders(trading) == [], "the paper account holds open orders; placing nothing"
    assert aapl_shares(trading) == 0, "the paper account holds AAPL; placing nothing"

    strategy = Placer()
    clients: list[AlpacaExecutionClient] = []
    measured: dict[str, dict[str, Any]] = {}
    stopped: list[str] = []
    node = build_node(strategy, clients)

    async def measure() -> None:
        task = asyncio.ensure_future(node.run_async())
        try:
            async with asyncio.timeout(60):
                while not (
                    task.done() or (clients and clients[0].is_connected and strategy.started)
                ):
                    await asyncio.sleep(0.05)
            if task.done():
                task.result()
            client = clients[0]
            instrument(client, measured)
            for _ in range(ORDERS):
                why = await asyncio.to_thread(in_window, trading)
                if why is not None:
                    stopped.append(f"stopped after {len(measured)} orders: {why}")
                    return
                held = await asyncio.to_thread(aapl_shares, trading)
                side = OrderSide.SELL if held > 0 else OrderSide.BUY
                price = await asyncio.to_thread(priced_to_fill, side)
                if price is None:
                    stopped.append(
                        f"stopped after {len(measured)} orders: no two-sided IEX quote from the "
                        "last minute to price an order on"
                    )
                    return
                cid = strategy.place(side, price)
                entry = measured.setdefault(str(cid), {"side": side.name})
                deadline = time.monotonic() + FILL_WAIT_SECS
                while time.monotonic() < deadline:
                    try:
                        order = await asyncio.to_thread(trading.get_order_by_client_id, str(cid))
                    except APIError as e:
                        if (
                            e.status_code != 404
                        ):  # 404: the node's submit has not reached Alpaca yet
                            raise
                        order = None
                    if isinstance(order, AlpacaOrder):
                        entry["order_id"] = str(order.id)
                        entry["status"] = getattr(order.status, "value", order.status)
                        if entry["status"] in ("filled", "canceled", "expired", "rejected"):
                            break
                    await asyncio.sleep(0.5)
                if entry.get("status") != "filled":
                    stopped.append(
                        f"order {cid} is {entry.get('status')} after {FILL_WAIT_SECS:.0f} s"
                    )
                    return  # its side would be wrong for the next order
                deadline = time.monotonic() + OUTCOME_WAIT_SECS
                while "outcome" not in entry and time.monotonic() < deadline:
                    await asyncio.sleep(0.25)
                deadline = (
                    time.monotonic() + 5
                )  # the engine applies the generated fill from its queue
                while (
                    str(entry.get("outcome", "")).startswith("read ")
                    and time.monotonic() < deadline
                ):
                    cached = client.cache.order(cid)
                    if cached is not None and len(cached.trade_ids) >= len(entry["trade_ids"]):
                        break
                    await asyncio.sleep(0.1)
                cached = client.cache.order(cid)
                entry["node_trade_ids"] = (
                    [str(t) for t in cached.trade_ids] if cached is not None else None
                )
                entry["lag_secs"] = seconds_between(
                    entry.get("transaction_time"), entry.get("listed_at")
                )
                if entry.get("outcome") == "ran out":
                    entry["lag_secs_at_least"] = seconds_between(
                        entry.get("stream_at"), entry.get("last_read_at")
                    )
                print("catch-up observation:", json.dumps({"client_order_id": str(cid), **entry}))
                await asyncio.sleep(SPACING_SECS)
        finally:
            if not task.done():
                node.handle().stop()
            await task

    try:
        asyncio.run(measure())
    finally:
        cleanup_errors: list[str] = []
        try:
            node.dispose()
        except Exception as e:  # noqa: BLE001 - the account is still cleaned up; reported below
            cleanup_errors.append(f"dispose: {e!r}")
        try:
            for order in open_orders(trading):
                try:
                    trading.cancel_order_by_id(order.id)
                except APIError as e:
                    if e.status_code != 422:  # 422: it closed between the listing and the cancel
                        raise
            wait_until(lambda: not open_orders(trading), "the canceled orders to close", 30)
        except Exception as e:  # noqa: BLE001 - the share is still closed; reported below
            cleanup_errors.append(f"orders: {e!r}")
        try:
            if aapl_shares(trading) > 0:
                trading.close_position(SYMBOL)
                wait_until(lambda: aapl_shares(trading) == 0, "the AAPL share to close", 60)
        except Exception as e:  # noqa: BLE001 - reported below
            cleanup_errors.append(f"position: {e!r}")
        if cleanup_errors:
            print("catch-up cleanup failed:", "; ".join(cleanup_errors))

    outcomes = Counter(entry.get("outcome", "none") for entry in measured.values())
    lags = sorted(e["lag_secs"] for e in measured.values() if e.get("lag_secs") is not None)
    print(
        "catch-up distribution:",
        json.dumps(
            {
                "orders": len(measured),
                "outcomes": dict(outcomes),
                "lag_secs": lags,
                "reads_allowed": canon_alpaca.execution.FILL_READ_TRIES,
                "retry_secs": canon_alpaca.execution.FILL_READ_RETRY_SECS,
                "orders_with_a_hold": sum(1 for e in measured.values() if e.get("holds")),
                "orders_with_a_held_report_showing_a_fill": sum(
                    1
                    for e in measured.values()
                    if any(Decimal(r["shown_filled"]) > 0 for r in e.get("held_reports", ()))
                ),
                "hold_errors": sorted(
                    {name for e in measured.values() for name in e.get("hold_errors", ())}
                ),
            }
        ),
    )
    assert not stopped, (stopped, measured)
    assert len(measured) == ORDERS, measured
    assert all(e.get("status") == "filled" for e in measured.values()), measured
    assert outcomes["none"] == 0, measured
    for entry in measured.values():
        if str(entry.get("outcome", "")).startswith("read "):
            assert sorted(entry["node_trade_ids"]) == sorted(entry["trade_ids"]), entry
