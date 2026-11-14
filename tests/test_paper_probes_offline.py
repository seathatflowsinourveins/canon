"""Offline cases for the two paper probes `test_paper_trailing_stop_outside_session` and
`test_paper_qty_available`: what each sends, in what order it withdraws it, and what it writes, against
stand-ins for alpaca-py's `TradingClient`.

The probes themselves run only under `mise run paper:test`. These cases hold what must be true of the
first whatever Alpaca answers: one trailing stop, GTC, close-only, of the protect lineage, far under the
market; the plain sell beside it withdrawn first and the stop after it, in the same step, also when a
read or the other withdrawal fails; a cancel asked again while its order stands; a refused stop
passing as an answer; no order ID and no account number in any line or in what a failure raises. Of
the second: no further sell is sent while the venue still holds the share from the one before.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import TrailingStopOrderRequest

from canon_trading import custody
from tests import test_paper_qty_available as free
from tests import test_paper_trailing_stop_outside_session as probe

STOP_ID = UUID("11111111-1111-4111-8111-111111111111")
SELL_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT_NUMBER = "PA3EXAMPLE99"
NEAR = Decimal("200.00")


def refused(status: int, code: int, message: str) -> APIError:
    return APIError(
        f'{{"code": {code}, "message": "{message}"}}',
        SimpleNamespace(response=SimpleNamespace(status_code=status)),
    )


class Venue:
    """A stand-in for the calls the probe makes. `stop` and `sell` are what Alpaca answers each submit
    with: an order status word, or the `APIError` it raises (or a function of this venue that gives
    one). `cancels` maps an order kind to the answers its cancels get in turn: an error to raise, or
    the status the order takes; past the list a cancel ends the order."""

    def __init__(
        self,
        stop: Any = "new",
        sell: Any = "new",
        failing_read: int | None = None,
        cancels: dict[str, list[Any]] | None = None,
    ) -> None:
        self.answers = {"trailing_stop": stop, "limit": sell}
        self.failing_read = failing_read
        self.cancels = cancels or {}
        self.reads = 0
        self.calls: list[str] = []
        self.requests: list[Any] = []
        self.orders: dict[UUID, SimpleNamespace] = {}

    def submit_order(self, request: Any) -> SimpleNamespace:
        kind = "trailing_stop" if isinstance(request, TrailingStopOrderRequest) else "limit"
        self.calls.append(f"submit {kind}")
        self.requests.append(request)
        answer = self.answers[kind]
        if callable(answer):
            answer = answer(self)
        if isinstance(answer, APIError):
            raise answer
        order = SimpleNamespace(
            id=STOP_ID if kind == "trailing_stop" else SELL_ID,
            status=answer,
            filled_qty="0",
            hwm="200.100000000" if kind == "trailing_stop" else None,
            stop_price="120.10" if kind == "trailing_stop" else None,
            kind=kind,
        )
        self.orders[order.id] = order
        return order

    def get_order_by_id(self, order_id: UUID) -> SimpleNamespace:
        order = self.orders[order_id]
        if order.status == "pending_cancel":
            order.status = "canceled"
            return SimpleNamespace(**{**vars(order), "status": "pending_cancel"})
        return order

    def get_open_position(self, symbol: str) -> SimpleNamespace:
        self.reads += 1
        if self.failing_read is not None and self.reads >= self.failing_read:
            raise OSError(f"the read of order {STOP_ID} failed")
        open_sells = [o for o in self.orders.values() if o.status not in probe.ENDED]
        return SimpleNamespace(qty="1", qty_available="0" if open_sells else "1")

    def cancel_order_by_id(self, order_id: UUID) -> None:
        order = self.orders[order_id]
        self.calls.append(f"cancel {order.kind}")
        turn = self.cancels.get(order.kind, [])
        answer = turn.pop(0) if turn else "canceled"
        if isinstance(answer, APIError):
            raise answer
        order.status = answer


def run(venue: Venue) -> list[str]:
    lines: list[str] = []
    probe.ask(venue, NEAR, lines, sell_price="250.00", part="overnight", within=0.0, step=0.0)
    return lines


def failed(venue: Venue) -> tuple[list[str], str]:
    lines: list[str] = []
    with pytest.raises(probe.ProbeFailed) as raised:
        probe.ask(venue, NEAR, lines, sell_price="250.00", part="overnight", within=0.0, step=0.0)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    return lines, str(raised.value)


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
    # At least a third under the price: the stop price is the high-water mark less the trail.
    assert Decimal(str(stop.trail_price)) >= NEAR / 3
    assert Decimal(str(stop.trail_price)) < NEAR


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
    assert probe.left_standing(lines) == []


def test_a_refused_stop_sends_nothing_beside_it_and_passes_as_an_answer() -> None:
    venue = Venue(stop=refused(422, 42210000, "trailing stop orders are not accepted now"))
    lines = run(venue)
    assert venue.calls == ["submit trailing_stop"]
    assert any(
        "the stop: refused, 422 42210000 trailing stop orders are not accepted now" in line
        for line in lines
    ), lines
    assert probe.left_standing(lines) == [], "a refusal is an answer, not a failure"


def test_a_refused_plain_sell_is_recorded_and_the_stop_is_still_withdrawn() -> None:
    venue = Venue(sell=refused(403, 40310000, "insufficient qty available for order"))
    lines = run(venue)
    assert venue.calls == ["submit trailing_stop", "submit limit", "cancel trailing_stop"]
    assert any(
        "the plain sell beside it: refused, 403 40310000 insufficient qty available" in line
        for line in lines
    ), lines


@pytest.mark.parametrize("failing_read", [1, 2])
def test_a_read_that_fails_leaves_nothing_at_the_venue_and_loses_no_line(failing_read: int) -> None:
    """The first read fails before the plain sell exists; the second after Alpaca took it."""
    venue = Venue(failing_read=failing_read)
    lines, raised = failed(venue)
    withdrawals = [call for call in venue.calls if call.startswith("cancel")]
    assert withdrawals == ["cancel limit", "cancel trailing_stop"][2 - failing_read :]
    assert all(order.status == "canceled" for order in venue.orders.values())
    assert "a step failed: OSError: the read of order [id] failed" in raised
    assert any("the stop: accepted" in line for line in lines), lines
    assert any("a step failed" in line for line in lines), lines
    assert str(STOP_ID) not in raised + "\n".join(lines)


def test_a_withdrawal_that_fails_does_not_keep_the_stop_from_being_withdrawn() -> None:
    venue = Venue(cancels={"limit": [refused(500, 50010000, f"order {SELL_ID} failed")]})
    lines, raised = failed(venue)
    assert venue.calls[-2:] == ["cancel limit", "cancel trailing_stop"]
    assert venue.orders[STOP_ID].status == "canceled"
    assert "the plain sell could not be withdrawn: 500 50010000 order [id] failed" in raised
    assert str(SELL_ID) not in raised + "\n".join(lines)


def test_a_cancel_refused_while_the_order_stands_is_asked_again() -> None:
    venue = Venue(cancels={"trailing_stop": [refused(422, 42210000, "order is not cancelable")]})
    lines = run(venue)
    assert venue.calls[-2:] == ["cancel trailing_stop", "cancel trailing_stop"]
    assert any(
        "the stop: cancel refused, 422 42210000 order is not cancelable; then cancel taken; "
        "ended canceled, filled 0" in line
        for line in lines
    ), lines
    assert probe.left_standing(lines) == []


def test_a_cancel_that_is_pending_is_waited_for() -> None:
    venue = Venue(cancels={"trailing_stop": ["pending_cancel"]})
    lines = run(venue)
    assert any("the stop: cancel taken; ended canceled, filled 0" in line for line in lines), lines


def test_an_order_that_never_ends_is_named_as_left_standing(monkeypatch) -> None:
    monkeypatch.setattr(probe, "ENDED_WITHIN_SECS", 0.0)
    venue = Venue(cancels={"trailing_stop": [refused(422, 42210000, "not cancelable")] * 3})
    lines = run(venue)
    (left,) = probe.left_standing(lines)
    assert "ended new, filled 0" in left, left


def test_a_stop_that_is_only_queued_is_named_so_and_still_withdrawn() -> None:
    venue = Venue(stop="accepted")
    lines = run(venue)
    assert any("the stop: accepted, status accepted" in line for line in lines), lines
    assert venue.calls[-1] == "cancel trailing_stop"


def test_no_line_carries_an_order_id_a_client_order_id_or_an_account_number() -> None:
    def quoting(venue: Venue) -> APIError:
        ours = venue.requests[0].client_order_id
        return refused(
            403, 40310000, f"account {ACCOUNT_NUMBER}: order {STOP_ID} ({ours}) holds the shares"
        )

    venue = Venue(sell=quoting)
    lines = run(venue)
    text = "\n".join(lines)
    assert ACCOUNT_NUMBER not in text and str(STOP_ID) not in text and str(SELL_ID) not in text
    assert venue.requests[0].client_order_id not in text
    assert "403 40310000 account [account]: order [id] ([id]) holds the shares" in text, text


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("hwm 229.870000000, stop_price 137.92", "hwm 229.870000000, stop_price 137.92"),
        ("403 40310000 insufficient qty", "403 40310000 insufficient qty"),
        ("read at 2026-11-15T01:02:03.456+00:00", "read at 2026-11-15T01:02:03.456+00:00"),
        ("account 123456789 refused", "account [account] refused"),
        ("order canon-close-20261115T010203Z-1-0a1b2c3d", "order [id]"),
        ("order O-20261115-010203-001-000-1", "order [id]"),
        ("id 0123456789abcdef0123456789abcdef", "id [id]"),
    ],
)
def test_the_mask_hides_ids_and_account_numbers_and_leaves_codes_and_prices(text, shown) -> None:
    assert probe.masked(text) == shown


class Holder:
    """A stand-in for the free-quantity probe's calls: a one-share long whose `qty_available` is 0
    while a sell is open and, when `releases`, 1 again once it is canceled."""

    def __init__(self, releases: bool) -> None:
        self.releases = releases
        self.sent: list[Any] = []
        self.open = 0
        self.stuck = False

    def submit_order(self, request: Any) -> AlpacaOrder:
        self.sent.append(request)
        self.open += 1
        return AlpacaOrder.model_construct(id=SELL_ID)

    def cancel_order_by_id(self, order_id: UUID) -> None:
        self.open -= 1
        self.stuck = not self.releases

    def get_open_position(self, symbol: str) -> AlpacaPosition:
        held = self.open or self.stuck
        return AlpacaPosition.model_construct(qty="1", qty_available="0" if held else "1")

    def get_orders(self, request: Any) -> list[Any]:
        return [object()] * self.open


@pytest.mark.parametrize(("releases", "sells"), [(True, 2), (False, 1)])
def test_no_second_sell_is_sent_while_the_venue_still_holds_the_share(
    monkeypatch, releases: bool, sells: int
) -> None:
    monkeypatch.setattr(free, "WITHIN_SECS", 0.0)
    monkeypatch.setattr(free, "STEP_SECS", 0.0)
    venue = Holder(releases)
    reads: dict[str, tuple[str, str, str]] = {}
    changes: dict[str, tuple[str, float | None]] = {}
    free.measure(cast("Any", venue), "250.00", "overnight", reads, changes)
    assert len(venue.sent) == sells
    assert [request.position_intent for request in venue.sent] == [
        None,
        PositionIntent.SELL_TO_CLOSE,
    ][:sells]
    assert len(changes) == 2 * sells
    assert (changes["plain sell canceled (first read, secs to 1)"][1] is not None) == releases
