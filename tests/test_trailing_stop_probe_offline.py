"""Offline cases for the paper probe `test_paper_trailing_stop_outside_session`: what it sends, in what
order it withdraws it, and what it writes, against a stand-in for alpaca-py's `TradingClient`.

The probe itself runs only under `mise run paper:test`. These cases hold what must be true of it
whatever Alpaca answers: one trailing stop, GTC, close-only, of the protect lineage, far under the
market; the plain sell beside it canceled first and the stop after it, in the same step, also when a
read fails; no order ID and no account number in any line it returns.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import TrailingStopOrderRequest

from canon_trading import custody
from tests import test_paper_trailing_stop_outside_session as probe

STOP_ID = UUID("11111111-1111-4111-8111-111111111111")
SELL_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT_NUMBER = "PA3EXAMPLE99"
BID = Decimal("200.00")


def refused(status: int, code: int, message: str) -> APIError:
    return APIError(
        f'{{"code": {code}, "message": "{message}"}}',
        SimpleNamespace(response=SimpleNamespace(status_code=status)),
    )


class Venue:
    """A stand-in for the calls the probe makes. `stop` and `sell` are what Alpaca answers each submit
    with: an order status word, or the `APIError` it raises."""

    def __init__(self, stop: Any = "new", sell: Any = "new", failing_read: bool = False) -> None:
        self.answers = {"trailing_stop": stop, "limit": sell}
        self.failing_read = failing_read
        self.calls: list[str] = []
        self.requests: list[Any] = []
        self.orders: dict[UUID, SimpleNamespace] = {}

    def submit_order(self, request: Any) -> SimpleNamespace:
        kind = "trailing_stop" if isinstance(request, TrailingStopOrderRequest) else "limit"
        self.calls.append(f"submit {kind}")
        self.requests.append(request)
        answer = self.answers[kind]
        if isinstance(answer, APIError):
            raise answer
        order = SimpleNamespace(
            id=STOP_ID if kind == "trailing_stop" else SELL_ID,
            status=answer,
            filled_qty="0",
            hwm="200.10" if kind == "trailing_stop" else None,
            stop_price="120.10" if kind == "trailing_stop" else None,
            kind=kind,
        )
        self.orders[order.id] = order
        return order

    def get_order_by_id(self, order_id: UUID) -> SimpleNamespace:
        return self.orders[order_id]

    def get_open_position(self, symbol: str) -> SimpleNamespace:
        if self.failing_read:
            raise OSError("the read failed")
        open_sells = [o for o in self.orders.values() if o.status in probe.STANDING]
        return SimpleNamespace(qty="1", qty_available="0" if open_sells else "1")

    def cancel_order_by_id(self, order_id: UUID) -> None:
        order = self.orders[order_id]
        self.calls.append(f"cancel {order.kind}")
        order.status = "canceled"


def run(venue: Venue) -> list[str]:
    return probe.ask(venue, BID, sell_price="250.00", part="overnight", within=0.0, step=0.0)


def test_the_stop_is_one_gtc_close_only_share_of_the_protect_lineage_far_under_the_market() -> None:
    venue = Venue()
    run(venue)
    stop = venue.requests[0]
    assert isinstance(stop, TrailingStopOrderRequest)
    assert (stop.qty, stop.side, stop.time_in_force) == (
        1,
        AlpacaOrderSide.SELL,
        AlpacaTimeInForce.GTC,
    )
    assert stop.position_intent == PositionIntent.SELL_TO_CLOSE
    assert stop.trail_percent is None
    assert custody.lineage(stop.client_order_id) == custody.PROTECT, stop.client_order_id
    # At least a third under the bid: the stop price is the high-water mark less the trail.
    assert Decimal(str(stop.trail_price)) >= BID / 3
    assert Decimal(str(stop.trail_price)) < BID


def test_the_plain_sell_beside_it_is_close_only_and_is_withdrawn_before_the_stop() -> None:
    venue = Venue()
    lines = run(venue)
    sell = venue.requests[1]
    assert isinstance(sell, LimitOrderRequest)
    assert (sell.qty, sell.side, sell.limit_price) == (1, AlpacaOrderSide.SELL, 250.0)
    assert sell.position_intent == PositionIntent.SELL_TO_CLOSE and sell.extended_hours is True
    assert venue.calls == [
        "submit trailing_stop",
        "submit limit",
        "cancel limit",
        "cancel trailing_stop",
    ]
    assert all(order.status == "canceled" for order in venue.orders.values())
    assert any("the stop: accepted, status new" in line for line in lines), lines
    assert any("qty_available 0" in line for line in lines), lines
    assert any("the plain sell beside it: accepted" in line for line in lines), lines


def test_a_refused_stop_sends_nothing_beside_it_and_its_words_are_recorded() -> None:
    venue = Venue(stop=refused(422, 42210000, "trailing stop orders are not accepted now"))
    lines = run(venue)
    assert venue.calls == ["submit trailing_stop"]
    assert any(
        "the stop: refused, 422 42210000 trailing stop orders are not accepted now" in line
        for line in lines
    ), lines


def test_a_refused_plain_sell_is_recorded_and_the_stop_is_still_withdrawn() -> None:
    venue = Venue(sell=refused(403, 40310000, "insufficient qty available for order"))
    lines = run(venue)
    assert venue.calls == ["submit trailing_stop", "submit limit", "cancel trailing_stop"]
    assert any(
        "the plain sell beside it: refused, 403 40310000 insufficient qty available" in line
        for line in lines
    ), lines


def test_a_read_that_fails_does_not_leave_the_stop_at_the_venue() -> None:
    venue = Venue(failing_read=True)
    with pytest.raises(OSError, match="the read failed"):
        run(venue)
    assert venue.calls[-1] == "cancel trailing_stop"
    assert venue.orders[STOP_ID].status == "canceled"


def test_a_stop_that_is_only_queued_is_named_so_and_still_withdrawn() -> None:
    venue = Venue(stop="accepted")
    lines = run(venue)
    assert any("the stop: accepted, status accepted" in line for line in lines), lines
    assert venue.calls[-1] == "cancel trailing_stop"


def test_no_line_carries_an_order_id_or_an_account_number() -> None:
    venue = Venue(
        sell=refused(403, 40310000, f"account {ACCOUNT_NUMBER}: order {STOP_ID} holds the shares")
    )
    lines = run(venue)
    text = "\n".join(lines)
    assert ACCOUNT_NUMBER not in text and str(STOP_ID) not in text and str(SELL_ID) not in text
    assert venue.requests[0].client_order_id not in text
    assert "403 40310000 account [account]: order [id] holds the shares" in text, text
