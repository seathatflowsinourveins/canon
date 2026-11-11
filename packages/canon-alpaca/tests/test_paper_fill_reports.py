"""Paper test: start-up reconciliation takes an order's fills from Alpaca's fill activities (ADR 0024), the
Execution Testing Spec's TC-E85 "Reconcile filled orders"
(https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md).

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped with that reason. It needs the regular session, since a market
order fills only then, and skips with that reason outside it. Never write the keys to a file.

Run it after 09:35 New York. At the open on 2026-11-11 Alpaca paper did not make a 2-share market order
final within 60 s in the first three minutes, and the test failed there with the account left flat; a run
at 09:33 passed. The 60 s wait is not lengthened to cover the open.

What it does, on Alpaca's paper Trading API only (`paper=True` throughout): with alpaca-py, outside any
node, a DAY market buy of 2 AAPL (two shares, so Alpaca may fill it in parts), recording every `fill` and
`partial_fill` trade update's `execution_id` and price from `TradingStream`. Then a node with the
execution client starts, its start-up reconciliation looking back two minutes; the order it reconciles
must carry one `OrderFilled` per trade update, each with that update's `execution_id` as its trade ID and
its price. That is the report path; an inferred fill would carry one fill at the average price under an
ID of the engine's own. After the node stops, the test sells the 2 shares with alpaca-py.

The account is left as found: the test places nothing if the account holds any open order or position,
and afterwards, pass or fail, cancels its orders still open and sells what it holds of its buy.
"""

import asyncio
import math
import os
import threading
import time
import uuid
from datetime import timedelta

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTif
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.stream import TradingStream
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveNodeConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import TraderId

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

SYMBOL = "AAPL"
AAPL = InstrumentId.from_str(f"{SYMBOL}.ALPACA")
QTY = 2
DONE = {"filled", "canceled", "expired", "rejected", "replaced"}
WAIT_SECS = 60.0
ACTIVITIES = "/account/activities/FILL"
SHUTDOWN_LIMIT_SECS = 60  # each Alpaca request now ends within its timeout (ADR 0039)


def wait_for(predicate, timeout: float = WAIT_SECS, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def final_order(client: TradingClient, client_order_id: str) -> Order:
    def done():
        found = client.get_order_by_client_id(client_order_id)
        assert isinstance(found, Order), f"Alpaca answered {found!r} for {client_order_id}"
        return found if found.status.value in DONE else None

    found = wait_for(done)
    assert found, f"order {client_order_id} did not reach a final status in {WAIT_SECS:.0f} s"
    return found


def submit(client: TradingClient, side: AlpacaSide, qty: int, placed: list[str]) -> str:
    """Submit a market order, its client order ID recorded first so clean-up finds it."""
    client_order_id = f"canon-fill-reports-{uuid.uuid4()}"
    placed.append(client_order_id)
    client.submit_order(
        MarketOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            side=side,
            time_in_force=AlpacaTif.DAY,
            client_order_id=client_order_id,
        )
    )
    return client_order_id


def reconciled_fills(venue_order_id: str) -> tuple[OrderStatus | None, list]:
    """Start a node, let start-up reconciliation run, and return the order's status and fills."""
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-FILL-REPORTS",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            exec_engine=LiveExecutionEngineConfig(
                reconciliation=True, reconciliation_lookback_mins=2
            ),
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run
    handle = node.handle()

    async def bounded() -> None:
        # A hard limit on the whole run, so it cannot hold the paper lock without end; the test's own
        # clean-up runs afterwards in its `finally` either way.
        async with asyncio.timeout(WAIT_SECS + SHUTDOWN_LIMIT_SECS):
            await main()

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        try:
            deadline = loop.time() + WAIT_SECS
            while not task.done() and not handle.is_running and loop.time() < deadline:
                await asyncio.sleep(0.25)
            assert handle.is_running or task.done(), "the node did not start"
        finally:
            if not task.done():
                handle.stop()
            await task

    try:
        asyncio.run(bounded())
        for order in cache.orders(instrument_id=AAPL):
            if order.venue_order_id is not None and order.venue_order_id.value == venue_order_id:
                fills = [e for e in order.events() if type(e).__name__ == "OrderFilled"]
                return order.status, fills
        return None, []
    finally:
        node.dispose()


def test_paper_startup_reconciliation_reports_alpacas_fills() -> None:
    client = (
        with_http_timeout(  # so no request of the test's own can hold the paper lock (ADR 0039)
            TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
        )
    )
    clock = client.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    if not clock.is_open or clock.next_close - clock.timestamp < timedelta(minutes=10):
        pytest.skip(
            "the regular session is closed or closes within ten minutes; a market order would not fill"
        )
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    held = client.get_all_positions()
    if open_orders or held:
        pytest.fail(
            f"the paper account holds {len(open_orders)} open order(s) and {len(held)} position(s) "
            "this test did not open; it placed nothing and touched nothing"
        )

    updates: list[TradeUpdate] = []
    lock = threading.Lock()

    async def on_update(update: TradeUpdate) -> None:
        if getattr(update.event, "value", update.event) in ("fill", "partial_fill"):
            with lock:
                updates.append(update)

    stream = TradingStream(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    stream.subscribe_trade_updates(on_update)
    thread = threading.Thread(target=stream.run, name="fill-reports-stream", daemon=True)
    thread.start()
    placed: list[str] = []
    try:
        try:
            time.sleep(5)  # the subscription is not acknowledged through alpaca-py's interface
            assert thread.is_alive(), "the trade updates stream ended before any order was placed"
            buy = final_order(client, submit(client, AlpacaSide.BUY, QTY, placed))
            assert buy.status.value == "filled", f"the buy ended {buy.status.value}"

            def streamed():
                with lock:
                    mine = [u for u in updates if str(u.order.id) == str(buy.id)]
                return mine if sum(float(u.qty or 0) for u in mine) == QTY else None

            fills_seen = wait_for(streamed)
            assert fills_seen, "incomplete run: the stream did not send every fill of the buy"

            # TC-E85 reconciles fills "from a prior session": wait until Alpaca lists every fill of the
            # buy. A node started within a second of the last fill once found only the first listed.
            def listed():
                page = client.get(ACTIVITIES, {"order_id": str(buy.id)})
                assert isinstance(page, list), f"activities answered {type(page).__name__}"
                return page if sum(float(a["qty"]) for a in page) == QTY else None

            assert wait_for(listed, interval=1.0), "Alpaca did not list every fill of the buy"
            status, fills = reconciled_fills(str(buy.id))
        finally:
            stream.stop()
            thread.join(timeout=15)
        print(
            "fill-reports observation:",
            [
                (getattr(u.event, "value", u.event), str(u.execution_id), u.qty, u.price)
                for u in fills_seen
            ],
            [(f.trade_id.value, f.last_qty.as_double(), f.last_px.as_double()) for f in fills],
        )
        assert status == OrderStatus.FILLED, f"the reconciled order is {status}"
        assert sorted(f.trade_id.value for f in fills) == sorted(
            str(u.execution_id) for u in fills_seen
        )
        by_id = {str(u.execution_id): u for u in fills_seen}
        for fill in fills:
            update = by_id[fill.trade_id.value]
            assert math.isclose(fill.last_qty.as_double(), float(update.qty or 0))
            assert math.isclose(fill.last_px.as_double(), float(update.price or 0), abs_tol=1e-6)
    finally:
        leave_as_found(client, placed)


def leave_as_found(client: TradingClient, placed: list[str]) -> None:
    """Cancel this test's open orders and sell what is left of its buy; touch nothing else."""
    held = 0.0
    for client_order_id in list(placed):
        try:
            found = client.get_order_by_client_id(client_order_id)
        except APIError as error:
            if error.status_code != 404:
                raise
            continue  # never taken by Alpaca
        assert isinstance(found, Order), f"Alpaca answered {found!r} for {client_order_id}"
        if found.status.value not in DONE:
            client.cancel_order_by_id(found.id)
            found = final_order(client, client_order_id)
        filled = float(found.filled_qty or 0)
        held += filled if found.side == AlpacaSide.BUY else -filled
    if held > 0:
        close = final_order(client, submit(client, AlpacaSide.SELL, int(held), placed))
        assert close.status.value == "filled", (
            f"could not close the {held:g} {SYMBOL} this test bought (it ended {close.status.value}); "
            "the paper account still holds them"
        )
    print(f"fill-reports cleanup: {len(placed)} order(s) placed, {held:g} share(s) closed")
