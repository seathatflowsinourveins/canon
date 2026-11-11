"""Alpaca's replace on its paper Trading API, through alpaca-py directly: what the client's modify rests on.

`AlpacaExecutionClient._modify_order` (ADR 0042) relies on what a paper observation of 2026-11-10 showed in 3
of 3 runs and Alpaca's pages do not state: "Replace Order by ID"
(https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md) refuses a replacement that keeps the
original `client_order_id` with 422 "client_order_id must be unique"; its answer is the replacement, with a
new ID, an Alpaca-made client order ID and `replaces`; the stream's `replaced`
(https://docs.alpaca.markets/us/docs/websocket-streaming.md) carries the old order, with `replaced_by`; and
a second replace of the old order is refused with 422 "order already replaced". This test asserts each, so
a change on Alpaca's side fails it. It uses alpaca-py's `TradingClient` and `TradingStream` (`paper=True`,
`raw_data=True`, so every field Alpaca sends is seen) as alpaca-py's own `replace_order_by_id` is called.

It places a DAY limit buy of 1 AAPL at $1.00, far below the market, waits for Alpaca's `new`, replaces it
with $1.01 asking to keep its client order ID, then without one, waits for `replaced`, replaces the old
order again with $1.02, and cancels what is open. It needs the regular session: an order not yet routed
(`accepted`) cannot be replaced. It places nothing if the account holds an open AAPL order or an AAPL
position, and in a `finally` cancels every open AAPL order and sells any share that filled, read from
the position.
"""

import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from decimal import Decimal

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.stream import TradingStream
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import with_http_timeout

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

SYMBOL = "AAPL"
RUNS = 1
WAIT_SECS = 30.0
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_replace"}


class Recorder:
    """Every trade update the stream hands over, as Alpaca sent it, with when it arrived."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self._lock = threading.Lock()

    async def on_update(self, data: dict) -> None:
        with self._lock:
            self.updates.append({"received": datetime.now(UTC).isoformat(), **data})

    def for_orders(self, ids: set[str]) -> list[dict]:
        with self._lock:
            return [u for u in self.updates if _order(u).get("id") in ids]


def _order(update: dict) -> dict:
    data = update.get("data", update)
    return data.get("order", {}) if isinstance(data, dict) else {}


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {WAIT_SECS} s waiting for {what}")
        time.sleep(0.25)


def error(e: APIError) -> dict:
    return {"status": e.status_code, "body": str(e)}


def raw(value: object) -> dict:
    """An answer of the `raw_data=True` client: Alpaca's JSON as a dict."""
    assert isinstance(value, dict), type(value)
    return value


def status(trading: TradingClient, order_id: str) -> str:
    return raw(trading.get_order_by_id(order_id))["status"]


def observe(trading: TradingClient, created: list[str], run: int) -> dict:
    client_order_id = f"canon-replace-{uuid.uuid4().hex[:12]}"
    placed = raw(
        trading.submit_order(
            LimitOrderRequest(
                symbol=SYMBOL,
                qty=1,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=1.00,
                client_order_id=client_order_id,
            )
        )
    )
    old_id = placed["id"]
    created.append(old_id)
    seen: dict = {"run": run, "placed": placed}
    wait_for(lambda: status(trading, old_id) == "new", f"order {old_id} to be new")

    try:
        answer = raw(
            trading.replace_order_by_id(
                old_id, ReplaceOrderRequest(limit_price=1.01, client_order_id=client_order_id)
            )
        )
        # Kept, which paper refused each time it was observed: recorded, and the assertions say so.
        seen["replace_keeping_client_order_id"] = {"answer": answer}
        seen["replace"] = {"answer": answer}
    except APIError as e:
        seen["replace_keeping_client_order_id"] = {"error": error(e)}
        answer = raw(trading.replace_order_by_id(old_id, ReplaceOrderRequest(limit_price=1.01)))
        seen["replace"] = {"answer": answer}
    new_id = answer["id"]
    created.append(new_id)

    wait_for(lambda: status(trading, old_id) == "replaced", f"order {old_id} to be replaced")
    seen["old_after_replace"] = raw(trading.get_order_by_id(old_id))
    seen["new_after_replace"] = raw(trading.get_order_by_id(new_id))

    try:
        again = raw(trading.replace_order_by_id(old_id, ReplaceOrderRequest(limit_price=1.02)))
        seen["second_replace_of_old"] = {"answer": again}
        created.append(again["id"])
    except APIError as e:
        seen["second_replace_of_old"] = {"error": error(e)}

    cancel_open(trading, created)
    seen["old_at_end"] = raw(trading.get_order_by_id(old_id))
    seen["new_at_end"] = raw(trading.get_order_by_id(new_id))
    return seen


def held_shares(trading: TradingClient) -> int:
    """The shares of `SYMBOL` the account holds, 0 with no position."""
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return 0
        raise
    qty = Decimal(str(position["qty"] if isinstance(position, dict) else position.qty))
    if qty < 0 or qty != qty.to_integral_value():
        raise AssertionError(f"{SYMBOL} position of {qty} is not one this test's buys can leave")
    return int(qty)


def cancel_open(trading: TradingClient, ids: list[str]) -> None:
    for order_id in ids:
        if status(trading, order_id) in OPEN_STATUSES:
            try:
                trading.cancel_order_by_id(order_id)
            except APIError:
                pass  # no longer cancelable; the wait below shows where it ended
        wait_for(
            lambda order_id=order_id: (
                status(trading, order_id) not in OPEN_STATUSES | {"pending_cancel"}
            ),
            f"order {order_id} to close",
        )


def test_paper_replace_answers_as_observed() -> None:
    api_key, secret_key = os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY]
    trading = with_http_timeout(
        TradingClient(api_key=api_key, secret_key=secret_key, paper=True, raw_data=True)
    )
    clock = trading.get_clock()  # raw data: Alpaca's clock as a mapping
    if not (clock["is_open"] if isinstance(clock, dict) else clock.is_open):
        # An order Alpaca has not routed yet is `accepted`, which "cannot be replaced".
        pytest.skip("the regular session is closed; Alpaca replaces only an order it has routed")
    held = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
    assert held == [], f"the paper account already holds {len(held)} open {SYMBOL} order(s)"
    # Flat at the start, so a position at the end is this test's own and is the one closed.
    assert held_shares(trading) == 0, f"the paper account already holds {SYMBOL}; placing nothing"

    recorder = Recorder()
    stream = TradingStream(api_key=api_key, secret_key=secret_key, paper=True, raw_data=True)
    stream.subscribe_trade_updates(recorder.on_update)
    thread = threading.Thread(target=stream.run, name="replace-observation", daemon=True)
    thread.start()
    created: list[str] = []
    runs: list[dict] = []
    filled = 0
    cleanup_errors: list[BaseException] = []
    try:
        # alpaca-py sets `_running` once it has connected, authenticated and sent `listen`; Alpaca's
        # `listening` acknowledgement is not passed on, so a short margin follows.
        wait_for(lambda: bool(stream._running), "the trade updates stream to connect")
        time.sleep(2)
        for run in range(RUNS):
            runs.append(observe(trading, created, run))
    finally:
        try:
            cancel_open(trading, created)
            # Then every other open order in the symbol: the account held none at the start, and a
            # replace whose answer was lost leaves its replacement untracked.
            listed = trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
            )
            untracked = [
                str(order["id"] if isinstance(order, dict) else order.id)
                for order in (listed if isinstance(listed, list) else [])
                if str(order["id"] if isinstance(order, dict) else order.id) not in created
            ]
            created.extend(untracked)
            cancel_open(trading, untracked)
        finally:
            time.sleep(3)  # updates still on their way
            stream.stop()
            thread.join(15)
            updates = recorder.for_orders(set(created))
            # What filled is read from the position Alpaca holds, not from the updates the stream
            # delivered, which can miss one; the account was flat at the start and the test only buys.
            try:
                filled = held_shares(trading)
                if filled > 0:
                    trading.submit_order(
                        MarketOrderRequest(
                            symbol=SYMBOL,
                            qty=filled,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                    )
                    wait_for(lambda: held_shares(trading) == 0, f"the {SYMBOL} position to close")
            except (APIError, AssertionError) as e:
                # Not raised from here, where it would hide the test's own failure; reported below.
                cleanup_errors.append(e)

    assert cleanup_errors == [], cleanup_errors
    assert all(status(trading, order_id) not in OPEN_STATUSES for order_id in created)
    assert filled == 0, f"{filled} share(s) filled and were sold back"
    assert len(runs) == RUNS
    for seen in runs:
        old, new = seen["placed"], seen["replace"]["answer"]
        # 2. The replacement may not keep the original client order ID.
        kept = seen["replace_keeping_client_order_id"]
        assert "error" in kept, f"Alpaca accepted a replacement keeping the client order ID: {kept}"
        assert kept["error"]["status"] == 422, kept
        assert "client_order_id must be unique" in kept["error"]["body"], kept
        # 3. The answer is the replacement: a new ID, an Alpaca-made client order ID, `replaces`.
        assert new["id"] != old["id"] and new["client_order_id"] != old["client_order_id"], new
        assert str(new["replaces"]) == str(old["id"]), new
        # 4. `replaced` carries the old order, with `replaced_by`.
        replaced = [
            u["data"]["order"]
            for u in updates
            if u["data"]["event"] == "replaced" and str(u["data"]["order"]["id"]) == str(old["id"])
        ]
        assert len(replaced) == 1, replaced
        assert replaced[0]["client_order_id"] == old["client_order_id"]
        assert str(replaced[0]["replaced_by"]) == str(new["id"])
        # 6. A second replace of the old order is refused.
        again = seen["second_replace_of_old"]
        assert again["error"]["status"] == 422, again
        assert "order already replaced" in again["error"]["body"], again
