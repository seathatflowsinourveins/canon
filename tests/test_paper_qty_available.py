"""Paper probe, on account A: what Alpaca's position field `qty_available` subtracts, and how soon it shows it.

Contract: Alpaca's Position object, `qty_available`: "Total number of shares available minus open orders /
locked for options covered call" (https://docs.alpaca.markets/us/reference/getallopenpositions.md and
getopenposition-1.md, read 2026-11-14), alpaca-py's `Position.qty_available` (`alpaca/trading/models.py:176`,
201 at the pin c42add7c0a70, `Optional[str]`). The docs do not say which open orders are subtracted, or
whether the field is current on paper. Unit (ii) of the protector caps a sale at
min(the engine's uncovered quantity, `qty_available` plus our own open remainder from the same read). This
probe measures the figure that cap rests on.

What is measured, on a long of one share opened by a plain order of foreign lineage
(`test_paper_protect_seam.buy_foreign`):

* `qty` and `qty_available` with no open order;
* the same while a plain limit SELL for the share rests far over the ask (no `position_intent`);
* the same while a limit SELL marked `position_intent=sell_to_close` rests, the intent the protector's
  close-only sells carry (ADR 0111);
* after each cancel, the figure again.

For each change, the first read after Alpaca answers the submit or the cancel is printed with its time,
then the seconds until the figure shows the change, reading every `STEP_SECS` for at most `WITHIN_SECS`.
A field Alpaca leaves out is printed as UNKNOWN.

It asserts only what the docs state: with one share held and a sell for it open, `qty_available` is 0, and it
is 1 again once the sell is canceled, each within `WITHIN_SECS`. Everything else is printed for the
record. Bracket, OCO and OTO legs are not placed here: Alpaca takes them only in the regular session
(orders-at-alpaca.md, "Bracket Orders": "Extended hours are not supported"), which is Monday's cell on B.

Run under `mise run paper:test -k qty_available`, which holds account A's lock, in any of Alpaca's sessions
(`open_part`). It places nothing while the account holds an open order or a position in the symbol, and
cancels and closes what it opened (`left_as_found`). It opens no market-data stream.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from decimal import Decimal

from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import LimitOrderRequest

from tests import test_paper_protect_seam as seam

pytestmark = seam.pytestmark

SYMBOL = seam.SYMBOL
STEP_SECS = 0.25
WITHIN_SECS = 10.0
"""How long the figure is given to show an order's submit or cancel."""


def figures(trading: seam.TradingClient) -> tuple[str, str, str]:
    """`qty`, `qty_available` (UNKNOWN when Alpaca leaves it out) and the read's time, from one read."""
    position = trading.get_open_position(SYMBOL)
    read_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    assert isinstance(position, AlpacaPosition)
    available = position.qty_available
    return str(position.qty), "UNKNOWN" if available is None else str(available), read_at


def shows(available: str, expected: str) -> bool:
    return available != "UNKNOWN" and Decimal(available) == Decimal(expected)


def until_available(trading: seam.TradingClient, expected: str) -> tuple[str, float | None]:
    """The first read's `qty_available`, and the seconds until a read shows `expected` (None: not within
    `WITHIN_SECS`)."""
    started = time.monotonic()
    first = available = figures(trading)[1]
    while not shows(available, expected):
        if time.monotonic() - started > WITHIN_SECS:
            return first, None
        time.sleep(STEP_SECS)
        available = figures(trading)[1]
    return first, round(time.monotonic() - started, 2)


def resting_sell(
    trading: seam.TradingClient, price: str, part: str, close_only: bool
) -> AlpacaOrder:
    """One share, a DAY limit SELL far over the ask, marked extended hours outside the regular session,
    of foreign lineage; `sell_to_close` when `close_only`."""
    order = trading.submit_order(
        LimitOrderRequest(
            symbol=SYMBOL,
            qty=1,
            side=AlpacaOrderSide.SELL,
            time_in_force=AlpacaTimeInForce.DAY,
            limit_price=float(price),
            extended_hours=None if part == "regular" else True,
            position_intent=PositionIntent.SELL_TO_CLOSE if close_only else None,
        )
    )
    assert isinstance(order, AlpacaOrder)
    return order


def test_paper_qty_available_subtracts_a_resting_sell_and_a_close_only_sell() -> None:
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    reads: dict[str, tuple[str, str, str]] = {}
    changes: dict[str, tuple[str, float | None]] = {}
    with seam.left_as_found(part) as cleaned:
        seam.buy_foreign(trading, data, part)
        reads["held, no open order"] = figures(trading)
        price = seam.resting_price(data, part)
        for name, close_only in (("plain sell", False), ("sell_to_close sell", True)):
            order = resting_sell(trading, price, part, close_only)
            changes[f"{name} open (first read, secs to 0)"] = until_available(trading, "0")
            reads[f"{name} open"] = figures(trading)
            trading.cancel_order_by_id(order.id)
            seam.wait_until(
                lambda: not seam.open_orders(trading), f"the {name} to close", WITHIN_SECS
            )
            changes[f"{name} canceled (first read, secs to 1)"] = until_available(trading, "1")
    print(f"\n[{part}] qty_available on a long of 1 {SYMBOL}")
    for name, (qty, available, read_at) in reads.items():
        print(f"  {name}: qty {qty}, qty_available {available}, read at {read_at}")
    for name, (first, secs) in changes.items():
        print(f"  {name}: {first}, {'not within ' + str(WITHIN_SECS) if secs is None else secs}")
    assert shows(reads["held, no open order"][1], "1"), reads
    assert all(secs is not None for _, secs in changes.values()), changes
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned
