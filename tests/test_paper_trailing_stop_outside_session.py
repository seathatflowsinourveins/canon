"""Paper probe, on account A: what Alpaca does with a GTC trailing stop sent outside the regular session.

The question: the standing protector is to run beside stops Alpaca holds, and its proof needs one such
stop standing while the regular session is closed. Does Alpaca accept one then, does it stand as an
open order, and does it hold the shares it would sell?

Contract, read 2026-11-14 through a summarising fetch (the wording as that fetch gave it):

* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Trailing Stop Orders": "Valid
  time-in-force values for trailing stop are "day" and "gtc""; "Trailing stop will not trigger outside
  of the regular market hours". The same page: "Orders not eligible for extended hours submitted after
  4:00pm ET will be queued up for release the next trading day."
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md: "Only `limit` orders are
  supported during the overnight session."
* https://docs.alpaca.markets/us/reference/getopenposition-1.md: `qty_available`, "Total number of
  shares available minus open orders / locked for options covered call".
* https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md: a cancel of an order that is no
  longer cancelable "will be rejected with status 422".
* alpaca-py at the pin: `TrailingStopOrderRequest` (`alpaca/trading/requests.py`), sent as the protect
  task sends its stop (`canon_trading.protect`): SELL, GTC, a dollar trail through
  `canon_alpaca.execution.venue_trail`, `position_intent=sell_to_close`, a client order ID of the
  protect lineage (`custody.PROTECT_PREFIX`).

The docs do not say which status a stop queued for the next session shows, whether it holds the
shares, or whether its high-water mark moves before the open. This probe records those three.

What it does, on a long of one share opened by a plain order of foreign lineage
(`test_paper_protect_seam.buy_foreign`):

1. sends ONE trailing stop for the share, its trail `FAR` of the bid, so its stop price is far under
   the market;
2. if Alpaca takes it, reads the order (status, `hwm`, `stop_price`) and the position (`qty`,
   `qty_available`) every `STEP_SECS` for `WITHIN_SECS`;
3. if it still stands, sends one plain DAY limit SELL for the same share far over the ask, marked
   `sell_to_close`: refused, the stop holds the share; accepted, it does not;
4. cancels what Alpaca took, the plain sell first and then the stop, in the same step, also when a
   read fails, and reads each ended with nothing filled.

It asserts only what must hold whatever Alpaca answers: everything it sent that Alpaca took ended
canceled with nothing filled, and the share is still held when the probe hands back to the cleaning.
The answer itself is printed for the record: no order ID and no account number is written (`masked`).

Run under `mise run paper:test -k trailing_stop_outside_session`, which holds account A's lock, in any
of Alpaca's sessions but the regular one, where it skips by name: the question is for the other parts.
It places nothing while the account holds an open order or a position in the symbol, and cancels and
closes what it opened (`left_as_found`). It opens no market-data stream and changes no account setting.
"""

from __future__ import annotations

import re
import time
from datetime import UTC
from datetime import datetime
from decimal import ROUND_UP
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import TrailingStopOrderRequest
from canon_alpaca.execution import venue_trail
from nautilus_trader.model import TrailingOffsetType

from canon_trading import session_window
from canon_trading.custody import PROTECT_PREFIX
from tests import test_paper_protect_seam as seam

pytestmark = seam.pytestmark

SYMBOL = seam.SYMBOL
FAR = Decimal("0.40")
"""The trail as a share of the bid: the stop price starts two fifths under the market."""
STEP_SECS = 0.25
WITHIN_SECS = 10.0
"""How long the stop is read after Alpaca takes it, and how long each cancel is given to end it."""
STANDING = ("new", "accepted", "pending_new", "held", "partially_filled")
"""The status words under which an order Alpaca took has not ended."""
ENDED_WITHIN_SECS = 30.0

UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
OUR_ID = re.compile(rf"{re.escape(PROTECT_PREFIX)}[0-9A-Za-z-]+")
ACCOUNT_TEXT = re.compile(r"\b(?:PA[0-9A-Z]{8,}|[0-9]{9,})\b")
"""A paper account number, or a run of nine digits or more; Alpaca's own codes have eight."""


def masked(text: object) -> str:
    """`text` with every order ID, client order ID of ours and account number replaced."""
    found = UUID_TEXT.sub("[id]", str(text))
    found = OUR_ID.sub("[id]", found)
    return ACCOUNT_TEXT.sub("[account]", found)


def word(field: object) -> str:
    return str(getattr(field, "value", field))


def far_trail(bid: Decimal) -> Decimal:
    """A whole-cent dollar trail of `FAR` of the bid, rounded up."""
    return (bid * FAR).quantize(Decimal("0.01"), rounding=ROUND_UP)


def stop_request(bid: Decimal) -> TrailingStopOrderRequest:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return TrailingStopOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=AlpacaOrderSide.SELL,
        time_in_force=AlpacaTimeInForce.GTC,
        trail_price=venue_trail(far_trail(bid), TrailingOffsetType.PRICE),
        position_intent=PositionIntent.SELL_TO_CLOSE,
        client_order_id=f"{PROTECT_PREFIX}{stamp}-1-{uuid4().hex[:8]}",
    )


def sell_request(price: str, part: str) -> LimitOrderRequest:
    return LimitOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=AlpacaOrderSide.SELL,
        time_in_force=AlpacaTimeInForce.DAY,
        limit_price=float(price),
        extended_hours=None if part == "regular" else True,
        position_intent=PositionIntent.SELL_TO_CLOSE,
    )


def submitted(trading: Any, request: Any) -> tuple[Any, str]:
    """The order Alpaca took and `accepted, status <word>`, or None and `refused, <status code words>`."""
    try:
        order = trading.submit_order(request)
    except APIError as e:
        return None, masked(f"refused, {e.status_code} {e.code} {e.message}")
    return order, f"accepted, status {word(order.status)}"


def read(trading: Any, order: Any) -> tuple[str, str]:
    """One read of the stop and of the position, and its time."""
    found = trading.get_order_by_id(order.id)
    position = trading.get_open_position(SYMBOL)
    available = position.qty_available
    shown = (
        f"status {word(found.status)}, hwm {found.hwm}, stop_price {found.stop_price}, "
        f"qty {position.qty}, qty_available {'UNKNOWN' if available is None else available}"
    )
    return shown, datetime.now(UTC).isoformat(timespec="milliseconds")


def withdrawn(trading: Any, order: Any, within: float, step: float) -> str:
    """Cancel `order` and say how it ended: its last status and what filled. A cancel Alpaca refuses as
    no longer cancelable (422) is read like any other: the order's own status says how it ended."""
    try:
        trading.cancel_order_by_id(order.id)
        asked = "cancel taken"
    except APIError as e:
        if e.status_code != 422:
            raise
        asked = masked(f"cancel refused, {e.status_code} {e.code} {e.message}")
    deadline = time.monotonic() + within
    found = trading.get_order_by_id(order.id)
    while word(found.status) in (*STANDING, "pending_cancel") and time.monotonic() < deadline:
        time.sleep(step)
        found = trading.get_order_by_id(order.id)
    return f"{asked}; ended {word(found.status)}, filled {found.filled_qty}"


def ask(
    trading: Any,
    bid: Decimal,
    *,
    sell_price: str,
    part: str,
    within: float = WITHIN_SECS,
    step: float = STEP_SECS,
) -> list[str]:
    """Send the stop, read it, send the plain sell beside it, and withdraw both, the plain sell first.
    The lines say what Alpaca answered. Whatever a read or a submit raises, what Alpaca took is
    withdrawn before this returns or raises."""
    lines: list[str] = []
    stop, answer = submitted(trading, stop_request(bid))
    lines.append(f"the stop: {answer}; trail {far_trail(bid)} on a bid of {bid}")
    if stop is None:
        return lines
    sell = None
    try:
        deadline = time.monotonic() + within
        shown, at = read(trading, stop)
        lines.append(f"the stop, first read: {shown}, read at {at}")
        while time.monotonic() < deadline:
            time.sleep(step)
            latest, at = read(trading, stop)
            if latest != shown:
                shown = latest
                lines.append(f"the stop, changed: {shown}, read at {at}")
        found = trading.get_order_by_id(stop.id)
        if word(found.status) in STANDING:
            sell, answer = submitted(trading, sell_request(sell_price, part))
            lines.append(f"the plain sell beside it: {answer}")
            if sell is not None:
                shown, at = read(trading, stop)
                lines.append(f"the stop, beside the plain sell: {shown}, read at {at}")
        else:
            lines.append(
                f"the stop no longer stands ({word(found.status)}): no sell sent beside it"
            )
    finally:
        if sell is not None:
            lines.append(f"the plain sell: {withdrawn(trading, sell, ENDED_WITHIN_SECS, step)}")
        lines.append(f"the stop: {withdrawn(trading, stop, ENDED_WITHIN_SECS, step)}")
    return [masked(line) for line in lines]


def test_paper_a_gtc_trailing_stop_outside_the_regular_session() -> None:
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    if part == "regular":
        pytest.skip("the regular session is open: the question is for the other parts")
    with seam.left_as_found(part) as cleaned:
        seam.buy_foreign(trading, data, part)
        offered, _ = session_window.close_limit(data, SYMBOL, "sell", part)
        lines = ask(trading, offered, sell_price=seam.resting_price(data, part), part=part)
        still_held = seam.held(trading)
        left_open = len(seam.open_orders(trading))
    print(f"\n[{part}] a GTC trailing stop for 1 {SYMBOL}, sent outside the regular session")
    for line in lines:
        print(f"  {line}")
    ended = [line for line in lines if "; ended " in line]
    assert ended and all("ended canceled, filled 0" in line for line in ended), lines
    assert (still_held, left_open) == (Decimal(1), 0), (still_held, left_open)
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned
