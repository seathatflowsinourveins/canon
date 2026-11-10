"""Paper test for ADR 0024: does an account activity's `id` carry the trade update's `execution_id`?

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped with that reason. It needs the regular session, since a market
order fills only then, and skips with that reason when Alpaca's clock says the market is closed or
closes within ten minutes. Never write the keys to a file.

What it calls, all on Alpaca's paper Trading API through alpaca-py 0.45.0 with `paper=True`:

* `TradingStream.subscribe_trade_updates`, for each `fill` and `partial_fill` update's `execution_id`
  ("Websocket Streaming", https://docs.alpaca.markets/us/docs/websocket-streaming.md).
* `TradingClient.submit_order` with a `MarketOrderRequest`: a DAY market buy of 2 AAPL, then a DAY market
  sell of the quantity bought. Two shares, not one, because a paper order "will receive partial fills for a
  random size 10% of the time" ("Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md),
  which one share cannot show.
* `TradingClient.get("/account/activities/FILL", {"order_id": ..., "direction": "asc"})`, the call ADR 0024
  names: "Retrieve Account Activities of Specific Type", whose `order_id` filter is "Useful for retrieving
  the fills that make up a completely filled order"
  (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md).

ADR 0024's rule: compare every activity `id`, and the UUID after its `::`, with the `execution_id`s the
stream sent for the same order. The test passes only if, for both orders, the UUIDs after `::` are exactly
the stream's `execution_id`s, one activity per fill update, and each matched pair has the same quantity,
price and kind (`fill` or `partial_fill`). Each order's observations are printed as one
JSON line (run with `-s` to see them); they hold order and execution IDs and prices, no credential.

The account is left as found. Before placing anything the test reads the open orders and positions and
stops, touching nothing, if there are any, since they are not its own. Afterwards, pass or fail, it cancels
any of its orders still open and sells whatever of its buy is still held. The stream's subscription is not
acknowledged through alpaca-py's interface, so the test waits a few seconds after starting it; if an update
is missed anyway, the run fails as incomplete rather than as a mismatch.
"""

import json
import math
import os
import threading
import time
import uuid
from datetime import timedelta

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.stream import TradingStream
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

SYMBOL = "AAPL"
QTY = 2
STREAM_SETTLE_SECS = 5.0
FILL_TIMEOUT_SECS = 60.0
ACTIVITY_TIMEOUT_SECS = 60.0
DONE = {"filled", "canceled", "expired", "rejected", "replaced"}


class Recorder:
    """Collects the stream's fill updates by Alpaca order ID, on the stream's own thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fills: dict[str, list[TradeUpdate]] = {}

    async def on_update(self, update: TradeUpdate) -> None:
        event = getattr(update.event, "value", update.event)
        if event in ("fill", "partial_fill"):
            with self.lock:
                self.fills.setdefault(str(update.order.id), []).append(update)

    def fills_for(self, order_id: str) -> list[TradeUpdate]:
        with self.lock:
            return list(self.fills.get(order_id, []))


def wait_for(predicate, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def order(client: TradingClient, order_id: str) -> Order:
    found = client.get_order_by_id(order_id)
    assert isinstance(found, Order), f"Alpaca answered {found!r} for order {order_id}"
    return found


def order_done(client: TradingClient, order_id: str) -> Order:
    def done():
        found = order(client, order_id)
        return found if found.status.value in DONE else None

    final = wait_for(done, FILL_TIMEOUT_SECS)
    assert final, f"order {order_id} did not reach a final status in {FILL_TIMEOUT_SECS:.0f} s"
    return final


def submit(client: TradingClient, side: OrderSide, qty: int, placed: list[str]) -> Order:
    """Submit a market order, its client order ID recorded first so clean-up finds it even if the
    answer is lost."""
    client_order_id = f"canon-fill-ids-{uuid.uuid4()}"
    placed.append(client_order_id)
    placed_order = client.submit_order(
        MarketOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    )
    assert isinstance(placed_order, Order), f"Alpaca answered {placed_order!r} to a submit"
    return placed_order


def fill_activities(client: TradingClient, order_id: str, qty: float) -> list[dict]:
    """The order's FILL activities, read until their quantities add up to what the order filled; if they
    never do, the last page read, so the observation shows what Alpaca did return."""
    last: list[dict] = []

    def read():
        nonlocal last
        page = client.get("/account/activities/FILL", {"order_id": order_id, "direction": "asc"})
        assert isinstance(page, list), f"activities answered {type(page).__name__}, not a list"
        last = page
        return page if abs(sum(float(a["qty"]) for a in page) - qty) < 1e-9 else None

    return wait_for(read, ACTIVITY_TIMEOUT_SECS, interval=1.0) or last


def observe(client: TradingClient, recorder: Recorder, order: Order) -> dict:
    """Wait for the order to finish, then gather its stream fills and its FILL activities."""
    order_id = str(order.id)
    final = order_done(client, order_id)
    assert final.status.value == "filled", (
        f"order {order_id} ended {final.status.value}, not filled"
    )
    filled = float(final.filled_qty or 0)

    def all_updates():
        updates = recorder.fills_for(order_id)
        return updates if abs(sum(float(u.qty or 0) for u in updates) - filled) < 1e-9 else None

    updates = wait_for(all_updates, FILL_TIMEOUT_SECS) or recorder.fills_for(order_id)
    activities = fill_activities(client, order_id, filled)
    observation = {
        "order_id": order_id,
        "side": "buy" if final.side == OrderSide.BUY else "sell",
        "filled_qty": final.filled_qty,
        "updates": [
            {
                "event": getattr(u.event, "value", u.event),
                "execution_id": str(u.execution_id) if u.execution_id else None,
                "qty": u.qty,
                "price": u.price,
                "timestamp": u.timestamp.isoformat(),
            }
            for u in updates
        ],
        "activities": [
            {
                k: a.get(k)
                for k in ("id", "type", "qty", "price", "cum_qty", "leaves_qty", "transaction_time")
            }
            for a in activities
        ],
    }
    print("fill-ids observation:", json.dumps(observation, sort_keys=True))
    return observation


def compare(observation: dict) -> None:
    """ADR 0024's comparison for one order: every activity `id`, and the UUID after `::`."""
    order_id = observation["order_id"]
    execution_ids = [u["execution_id"] for u in observation["updates"]]
    ids = [a["id"] for a in observation["activities"]]
    filled = float(observation["filled_qty"])
    streamed = sum(float(u["qty"]) for u in observation["updates"])
    assert abs(streamed - filled) < 1e-9, (
        f"incomplete run: the stream's {len(execution_ids)} fill update(s) for {order_id} add up to "
        f"{streamed:g}, not its {observation['filled_qty']} filled"
    )
    assert len(set(execution_ids)) == len(execution_ids), (
        f"a repeated execution_id: {execution_ids}"
    )
    assert len(set(ids)) == len(ids), f"a repeated activity id: {ids}"
    assert ids, f"incomplete run: no FILL activity for {order_id} in {ACTIVITY_TIMEOUT_SECS:.0f} s"
    assert all(e is not None for e in execution_ids), (
        f"a fill update for {order_id} has no execution_id"
    )
    assert not set(ids) & set(execution_ids), (
        f"an activity id equals an execution_id outright: {ids}"
    )
    assert all(i.count("::") == 1 for i in ids), f"an activity id is not in `::` form: {ids}"
    suffixes = [i.split("::", 1)[1] for i in ids]
    assert sorted(suffixes) == sorted(execution_ids), (
        f"no match for {order_id}: UUIDs after '::' {suffixes}, execution_ids {execution_ids}"
    )
    # Each matched pair is the same fill: same quantity, price and kind.
    updates = {u["execution_id"]: u for u in observation["updates"]}
    for activity, suffix in zip(observation["activities"], suffixes, strict=True):
        update = updates[suffix]
        assert math.isclose(float(activity["qty"]), update["qty"], abs_tol=1e-9), (activity, update)
        assert math.isclose(float(activity["price"]), update["price"], abs_tol=1e-6), (
            activity,
            update,
        )
        assert activity["type"] == update["event"], (activity, update)


def test_paper_fill_activity_ids_carry_the_execution_ids() -> None:
    client = TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)

    clock = client.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    if not clock.is_open or clock.next_close - clock.timestamp < timedelta(minutes=10):
        pytest.skip(
            "the regular session is closed or closes within ten minutes; a market order would not "
            "fill, so the IDs cannot be compared now"
        )
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    positions = client.get_all_positions()
    if open_orders or positions:
        pytest.fail(
            f"the paper account holds {len(open_orders)} open order(s) and {len(positions)} position(s) "
            "this test did not open; it placed nothing and touched nothing"
        )

    recorder = Recorder()
    stream = TradingStream(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    stream.subscribe_trade_updates(recorder.on_update)
    thread = threading.Thread(target=stream.run, name="fill-ids-stream", daemon=True)
    thread.start()
    placed: list[str] = []  # client order IDs
    try:
        try:
            time.sleep(STREAM_SETTLE_SECS)
            assert thread.is_alive(), "the trade updates stream ended before any order was placed"
            buy = submit(client, OrderSide.BUY, QTY, placed)
            bought = observe(client, recorder, buy)
            sell = submit(client, OrderSide.SELL, int(float(bought["filled_qty"])), placed)
            sold = observe(client, recorder, sell)
            compare(bought)
            compare(sold)
        finally:
            _leave_as_found(client, placed)
    finally:
        stream.stop()
        thread.join(timeout=15)


def _leave_as_found(client: TradingClient, placed: list[str]) -> None:
    """Cancel this test's open orders and sell what is left of its buy; touch nothing else."""
    held = 0.0
    for client_order_id in list(placed):
        try:
            found = client.get_order_by_client_id(client_order_id)
        except APIError as error:  # never taken by Alpaca: nothing to clean up
            print(f"fill-ids cleanup: Alpaca holds no order {client_order_id}: {error}")
            continue
        assert isinstance(found, Order), f"Alpaca answered {found!r} for {client_order_id}"
        order_id = str(found.id)
        mine = found
        if mine.status.value not in DONE:
            client.cancel_order_by_id(order_id)
            mine = order_done(client, order_id)
        filled = float(mine.filled_qty or 0)
        held += filled if mine.side == OrderSide.BUY else -filled
    if held > 0:
        close = submit(client, OrderSide.SELL, int(held), placed)
        final = order_done(client, str(close.id))
        assert final.status.value == "filled", (
            f"could not close the {held:g} {SYMBOL} this test bought (order {close.id} ended "
            f"{final.status.value}); the paper account still holds them"
        )
    print(f"fill-ids cleanup: {len(placed)} order(s) placed, {held:g} share(s) closed")
