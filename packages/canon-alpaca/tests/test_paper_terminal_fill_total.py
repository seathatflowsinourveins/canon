"""Paper probe for ADR 0075's invariant that an order's filled total never goes down: does Alpaca ever show a
copy of an order, on the trade updates stream or in a read of it, with fewer shares filled than an earlier
copy, a terminal one above all?

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`, and
when `CANON_PAPER_PROBE_TERMINAL=1` asks for it: it is a one-time probe of about three minutes (at most
about twelve, if every order is slow), so a routine
`mise run paper:test` skips it. Run it with
`CANON_PAPER_PROBE_TERMINAL=1 mise run paper:test -- -s -k test_paper_terminal_fill_total_never_goes_down`,
which holds the paper account's lock. It needs the regular session, from 09:35 New York until ten minutes
before the close. Never write the keys to a file.

What it does, on Alpaca's paper Trading API only (`paper=True` throughout), with alpaca-py and no node:
`ORDERS` DAY market orders of AAPL, each a buy of `QTY` when Alpaca shows no AAPL position and a sale of
what it shows held otherwise, each followed at once by a cancel (refused when the order filled first), so some
may end canceled with part filled; a paper order "will receive partial fills for a random size 10% of the
time" ("Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md). For each order it keeps every
`filled_qty` Alpaca shows, in arrival order: each trade update's copy of the order, and a read of the order
every `POLL_SECS` until `READS_AFTER_END` reads after it is final. It prints one JSON line per order and a
summary, and fails if a terminal copy (filled, canceled, expired or rejected) shows fewer filled than an
earlier copy, naming it: that is the copy ADR 0075's invariant holds back. A lower non-terminal copy, as a
stream update for an earlier fill arriving after a read that showed more, is printed and not failed. It
also fails if an order is not final within `FINAL_WAIT_SECS` (placing no more) or if an order that filled got
no copy on the stream, and it skips, naming why, when no order ended canceled with part filled: the case
the invariant is about was then not produced, and the record says so. None seen is the probe's other answer (the record then says the path is
covered by test and unobserved at the venue).

The account is left as found: the test places nothing if the account holds any open order or an AAPL
position, and afterwards, pass or fail, cancels its orders still open and closes any AAPL position.
"""

import json
import os
import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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
from canon_alpaca.providers import with_http_timeout

PROBE_ENV = "CANON_PAPER_PROBE_TERMINAL"
pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
        reason=(
            f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
            "to run this integration test"
        ),
    ),
    pytest.mark.skipif(
        os.environ.get(PROBE_ENV) != "1",
        reason=f"a one-time paper probe; set {PROBE_ENV}=1 to run it",
    ),
]

SYMBOL = "AAPL"
NEW_YORK = ZoneInfo("America/New_York")
ORDERS = 10
QTY = 10
"""Several shares, so a partial fill can leave some unfilled for the cancel."""
POLL_SECS = 0.2
READS_AFTER_END = 10
"""Reads of a final order, about two seconds of copies after its end."""
FINAL_WAIT_SECS = 60.0
SPACING_SECS = 10.0
DONE = {"filled", "canceled", "expired", "rejected"}


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def aapl_shares(trading: TradingClient) -> int:
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return 0
        raise
    return int(Decimal(str(getattr(position, "qty", 0))))


def open_orders(trading: TradingClient) -> list[Order]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    assert isinstance(orders, list)
    return orders


def wait_until(done, what: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds:.0f} s waiting for {what}")
        time.sleep(0.5)


def why_not_now(trading: TradingClient) -> str | None:
    """Why not to place an order now, or None, all from Alpaca's clock."""
    clock = trading.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    local = clock.timestamp.astimezone(NEW_YORK)
    if not clock.is_open:
        return "the regular session is closed"
    if (local.hour, local.minute) < (9, 35):
        return "before 09:35 New York"
    if clock.next_close - clock.timestamp < timedelta(minutes=10):
        return "the session closes within ten minutes"
    return None


def decreases(copies: list[tuple[str, str, float]]) -> list[dict]:
    """Each copy showing fewer filled than the most any earlier copy showed."""
    most, found = 0.0, []
    for source, status, filled in copies:
        if filled < most:
            found.append({"source": source, "status": status, "filled": filled, "after": most})
        most = max(most, filled)
    return found


def test_paper_terminal_fill_total_never_goes_down() -> None:
    trading = paper_trading()
    why = why_not_now(trading)
    if why is not None:
        pytest.skip(why)
    assert open_orders(trading) == [], "the paper account holds open orders; placing nothing"
    assert aapl_shares(trading) == 0, "the paper account holds AAPL; placing nothing"

    copies: dict[str, list[tuple[str, str, float]]] = {}
    lock = threading.Lock()

    async def on_update(update: TradeUpdate) -> None:
        cid = str(update.order.client_order_id)
        with lock:
            if cid in copies:
                copies[cid].append(
                    (
                        f"stream:{getattr(update.event, 'value', update.event)}",
                        getattr(update.order.status, "value", str(update.order.status)),
                        float(update.order.filled_qty or 0),
                    )
                )

    stream = TradingStream(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    stream.subscribe_trade_updates(on_update)
    thread = threading.Thread(target=stream.run, name="terminal-probe-stream", daemon=True)
    thread.start()
    observed: list[dict] = []
    stopped: list[str] = []
    try:
        # As test_paper_replace.py: alpaca-py sets `_running` once it has connected and authenticated;
        # the `listen` that follows is not acknowledged through its interface, so a short margin.
        wait_until(lambda: bool(getattr(stream, "_running", False)), "the trade updates stream", 30)
        time.sleep(2)
        assert thread.is_alive(), "the trade updates stream ended before any order was placed"
        for _ in range(ORDERS):
            if why_not_now(trading) is not None:
                break
            held = aapl_shares(trading)
            side, qty = (AlpacaSide.SELL, held) if held > 0 else (AlpacaSide.BUY, QTY)
            cid = f"canon-terminal-probe-{uuid.uuid4()}"
            with lock:
                copies[cid] = []
            placed = trading.submit_order(
                MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty,
                    side=side,
                    time_in_force=AlpacaTif.DAY,
                    client_order_id=cid,
                )
            )
            assert isinstance(placed, Order)
            try:
                trading.cancel_order_by_id(placed.id)
            except APIError as e:
                if e.status_code != 422:  # 422: filled before the cancel
                    raise
            after_end, deadline = 0, time.monotonic() + FINAL_WAIT_SECS
            while after_end < READS_AFTER_END and time.monotonic() < deadline:
                read = trading.get_order_by_id(placed.id)
                assert isinstance(read, Order)
                status = getattr(read.status, "value", str(read.status))
                with lock:
                    copies[cid].append(("read", status, float(read.filled_qty or 0)))
                if status in DONE:
                    after_end += 1
                time.sleep(POLL_SECS)
            time.sleep(1.0)  # the stream's last updates for it
            with lock:
                seen = list(copies[cid])
            ends = [c for c in seen if c[1] in DONE]
            final = ends[-1] if ends else (seen[-1] if seen else ("none", "none", 0.0))
            entry = {
                "client_order_id": cid,
                "side": side.value,
                "qty": qty,
                "final": final[1],
                "filled": final[2],
                "copies": len(seen),
                "stream_copies": sum(1 for c in seen if c[0].startswith("stream:")),
                "decreases": decreases(seen),
            }
            observed.append(entry)
            print("terminal-probe observation:", json.dumps(entry))
            if not ends:
                stopped.append(f"{cid} not final after {FINAL_WAIT_SECS:.0f} s")
                break  # its side and size would be wrong for the next order
            time.sleep(SPACING_SECS)
    finally:
        try:
            stream.stop()
            thread.join(timeout=15)
        finally:
            try:
                for order in open_orders(trading):
                    try:
                        trading.cancel_order_by_id(order.id)
                    except APIError as e:
                        if (
                            e.status_code != 422
                        ):  # 422: it closed between the listing and the cancel
                            raise
                wait_until(lambda: not open_orders(trading), "the canceled orders to close", 30)
            finally:
                if aapl_shares(trading) > 0:
                    trading.close_position(SYMBOL)
                    wait_until(lambda: aapl_shares(trading) == 0, "the AAPL position to close", 60)

    partial = [e for e in observed if e["final"] == "canceled" and 0 < e["filled"] < e["qty"]]
    found = [d for e in observed for d in e["decreases"]]
    terminal = [d for d in found if d["status"] in DONE]
    unstreamed = [
        e["client_order_id"] for e in observed if e["filled"] > 0 and not e["stream_copies"]
    ]
    print(
        "terminal-probe summary:",
        json.dumps(
            {
                "orders": len(observed),
                "canceled_partly_filled": len(partial),
                "copies": sum(e["copies"] for e in observed),
                "decreases": found,
                "terminal_decreases": terminal,
                "orders_without_stream_copies": unstreamed,
                "stopped": stopped,
            }
        ),
    )
    assert observed, "no order was placed"
    assert not terminal, f"Alpaca showed a terminal copy with a lower filled total: {terminal}"
    assert not stopped, stopped
    assert not unstreamed, f"orders that filled with no copy on the stream: {unstreamed}"
    if not partial:
        pytest.skip(
            f"no order of {len(observed)} ended canceled with part filled, so the case the invariant is "
            "about was not produced; no lower terminal copy was seen"
        )
