"""L8 blind contract: validate every venue quantity before trusting it for custody.

Base: 9c33cf9e04c1c0e8505637428694530b3ee3a6fb. Contracts: the task's P1-P5 and
alpaca-py c42add7c0a70, alpaca/trading/models.py:186,201,282-283. Order counts
are optional strings/floats; Position.qty is required text and qty_available
is optional text. Missing optional fields below go through model_validate.

Only tests/test_custody.py's public offline stand-ins are reused. Its opened
wrapper exercises custody._opened; close exercises custody.close_ours. The
file-loader pattern is already used in tests/test_history_load.py and works
when this file alone is collected under pytest's importlib mode.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from uuid import uuid4

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order
from alpaca.trading.models import Position

from canon_trading import custody

if TYPE_CHECKING:
    from tests import test_custody as standins
else:
    spec = importlib.util.spec_from_file_location(
        "custody_quantity_standins", Path(__file__).with_name("test_custody.py")
    )
    assert spec is not None and spec.loader is not None
    standins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standins)


class Missing(Enum):
    FIELD = "omitted"


BAD_COUNTS = [
    pytest.param(None, id="none"),
    pytest.param(Missing.FIELD, id="omitted"),
    pytest.param("", id="empty"),
    pytest.param("many", id="many"),
    pytest.param("NaN", id="nan-string"),
    pytest.param("inf", id="inf-string"),
    pytest.param("-inf", id="minus-inf-string"),
    pytest.param("-1", id="minus-one"),
    pytest.param("-18", id="minus-eighteen"),
    pytest.param("-20", id="minus-twenty-string"),
    pytest.param(-20.0, id="minus-twenty-float"),
    pytest.param(float("nan"), id="nan-float"),
    pytest.param(float("inf"), id="inf-float"),
    pytest.param(float("-inf"), id="minus-inf-float"),
]
BAD_POSITION_COUNTS = [
    # Changed after the tester's hand, on the lead's ruling of 2026-11-13: an available quantity that
    # is not listed passes, since custody decides nothing on it and Alpaca does not require it; one
    # that is listed is still checked. The four cases this takes out are below, turned round.
    pytest.param(field, *case.values, id=f"{case.id}-{field}")
    for case in BAD_COUNTS
    for field in ("qty", "qty_available")
    if not (field == "qty_available" and case.values[0] in (None, Missing.FIELD))
]
GOOD_FILLS = [
    pytest.param("0", id="zero-string"),
    pytest.param(0, id="zero-number"),
    pytest.param("1.25", id="fraction-string"),
    pytest.param(1.25, id="fraction-float"),
]
GOOD_QTYS = [
    pytest.param("0", id="zero-string"),
    pytest.param(0, id="zero-number"),
    pytest.param("20", id="positive-string"),
    pytest.param(20.0, id="positive-float"),
]
ORDER_ROLES = (
    "own-single",
    "own-predecessor",
    "foreign-predecessor",
    "own-buy-successor",
    "own-sell-successor",
    "foreign-successor",
    "foreign-unrelated",
    "before-zero-rejected",
)


def order_count(order: Order, field: str, value: object) -> Order:
    """Use actual alpaca-py validation, including its None default on omission."""
    payload = order.model_dump()
    if value is Missing.FIELD:
        payload.pop(field)
    else:
        payload[field] = value
    return Order.model_validate(payload)


def position_count(position: Position, field: str, value: object) -> Position:
    """Strings and optional omissions are validated as real venue payloads.

    Position.qty has no default and neither Position count declares floats.
    Construct/copy those defensive cases explicitly; they are not claims about
    payloads alpaca-py accepts. The report distinguishes them from valid models.
    """
    payload = position.model_dump()
    if value is Missing.FIELD:
        payload.pop(field)
        if field == "qty":
            return Position.model_construct(**payload)
    else:
        payload[field] = value
        if isinstance(value, (int, float)) or (field == "qty" and value is None):
            return position.model_copy(update={field: value})
    return Position.model_validate(payload)


class QuantityVenue(standins.ReplacedNotListedOpen):
    """Keep the original simulator, supplying availability on well-formed rows.

    Its original position fixture omits qty_available. Valid controls here
    explicitly supply it so testing that omission does not taint every case.
    """

    def __init__(self) -> None:
        super().__init__(standins.REGULAR)
        self.position_override: tuple[str, object] | None = None

    def get_all_positions(self) -> list[Position]:
        found = [
            position.model_copy(update={"qty_available": position.qty})
            for position in super().get_all_positions()
        ]
        if self.position_override is not None:
            field, value = self.position_override
            found = [
                position_count(position, field, value) if position.symbol == "SPY" else position
                for position in found
            ]
        return found


def order_case(role: str) -> tuple[QuantityVenue, int, Decimal]:
    """One target row per role; all its other fields and its holdings are valid."""
    venue = QuantityVenue()
    if role == "own-single":
        standins.chain(venue, "18")
        at, held = 0, 18
    elif role == "own-predecessor":
        standins.chain(venue, "18", "20", by_hand=(1,))
        at, held = 0, 18
    elif role == "foreign-predecessor":
        standins.chain(venue, "0", "18", "20", by_hand=(1,))
        at, held = 1, 2
    elif role == "own-buy-successor":
        standins.chain(venue, "0", "18")
        at, held = 1, 18
    elif role == "own-sell-successor":
        venue.hold("SPY", 20)
        standins.chain(venue, "0", "18", side="sell", first="O-20261111-145500-001-001-2")
        at, held = 2, 2
    elif role == "foreign-successor":
        standins.chain(venue, "18", "20", by_hand=(1,))
        at, held = 1, 18
    elif role == "foreign-unrelated":
        standins.chain(venue, "18")
        venue.open(
            client_order_id=str(uuid4()),
            symbol="SPY",
            qty="20",
            filled_qty="18",
            status="filled",
            submitted_at=venue.now.isoformat(),
        )
        at, held = 1, 18
    elif role == "before-zero-rejected":
        standins.chain(venue, "18", "0", last="rejected")
        at, held = 0, 18
    else:
        raise AssertionError(role)
    venue.held["SPY"] = Decimal(held)
    return venue, at, Decimal(held)


def working_orders(venue: QuantityVenue) -> list[str]:
    """Ordinary own orders in both the malformed symbol and an independent one."""
    return [
        venue.open(
            client_order_id=f"O-20261111-145900-001-001-{n}",
            symbol=symbol,
            qty="1",
            filled_qty="0",
            status="new",
        ).client_order_id
        for n, symbol in enumerate(("SPY", "AAPL"), 90)
    ]


def assert_frozen(report: custody.CloseReport, symbol: str = "SPY") -> None:
    assert symbol in report.frozen, report.lines()
    assert report.frozen[symbol], report.lines()
    assert any(symbol in why for why in report.frozen[symbol]), report.lines()
    assert any(f"frozen {symbol}:" in line for line in report.lines()), report.lines()
    assert not report.failed, report.lines()


@pytest.mark.parametrize("role", ORDER_ROLES)
@pytest.mark.parametrize("value", GOOD_FILLS)
def test_p1_finite_nonnegative_fills_are_accepted(role: str, value: str | float) -> None:
    venue, at, _ = order_case(role)
    venue.history[at] = order_count(venue.history[at], "filled_qty", value)
    filled = Decimal(str(value))
    if role == "foreign-successor" and 0 < filled < 18:
        # Added by the owning lane after the tester's hand, on the lead's ruling of 2026-11-13: a
        # foreign replacement that shows fewer fills than our 18 before it leaves the count not
        # established. The tester's case required 18. Zero still counts nothing.
        count, why = standins.opened(venue)
        assert count is None and "not established" in why, (count, why)
        return
    if role in ("foreign-predecessor", "own-sell-successor"):
        expected = Decimal(20) - filled
    elif role in ("foreign-successor", "foreign-unrelated"):
        expected = Decimal(18)
    else:
        expected = filled
    assert standins.opened(venue) == (expected, "")


@pytest.mark.parametrize("role", ORDER_ROLES)
@pytest.mark.parametrize("value", GOOD_QTYS)
def test_p1_finite_nonnegative_order_qty_is_accepted(role: str, value: str | float) -> None:
    venue, at, _ = order_case(role)
    # Zero quantity must be accepted without inventing fills in a zero-size row.
    venue.history = [order_count(order, "filled_qty", "0") for order in venue.history]
    venue.history[at] = order_count(venue.history[at], "qty", value)
    assert standins.opened(venue) == (Decimal(0), "")


@pytest.mark.parametrize("role", ORDER_ROLES)
@pytest.mark.parametrize("field", ("filled_qty", "qty"))
@pytest.mark.parametrize("value", BAD_COUNTS)
def test_p2_bad_history_count_is_unknown_never_zero_or_credit(
    role: str, field: str, value: object
) -> None:
    venue, at, _ = order_case(role)
    venue.history[at] = order_count(venue.history[at], field, value)
    quantity, why = standins.opened(venue)
    assert quantity is None, (role, field, value, quantity, why)
    assert "SPY" in why, why


@pytest.mark.parametrize("role", ORDER_ROLES)
@pytest.mark.parametrize("field", ("filled_qty", "qty"))
@pytest.mark.parametrize("value", BAD_COUNTS)
def test_p2_p3_bad_history_cancels_ours_and_freezes_only_that_symbol(
    role: str, field: str, value: object
) -> None:
    venue, at, _ = order_case(role)
    venue.history[at] = order_count(venue.history[at], field, value)
    pending = working_orders(venue)
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    assert set(pending) <= set(report.canceled), report.lines()
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)
    assert "AAPL" not in report.frozen, report.lines()


@pytest.mark.parametrize("mine", (True, False), ids=("ours", "foreign"))
@pytest.mark.parametrize("field", ("filled_qty", "qty"))
@pytest.mark.parametrize("value", BAD_COUNTS)
def test_p2_p3_bad_open_order_cancels_ours_but_never_foreign(
    mine: bool, field: str, value: object
) -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 18)
    bad = venue.open(
        client_order_id="O-20261111-145800-001-001-80" if mine else str(uuid4()),
        symbol="SPY",
        qty="20",
        filled_qty="0",
    )
    venue.history[-1] = order_count(bad, field, value)
    pending = working_orders(venue)
    report = standins.close(venue)
    expected = [*pending, bad.client_order_id] if mine else pending
    assert set(expected) <= set(venue.canceled), report.lines()
    assert set(expected) <= set(report.canceled), report.lines()
    if not mine:
        assert bad.client_order_id not in venue.canceled
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)


@pytest.mark.parametrize("field", ("qty", "qty_available"))
@pytest.mark.parametrize("value", GOOD_FILLS)
def test_p1_finite_nonnegative_position_count_is_accepted(field: str, value: str | float) -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 18)
    venue.position_override = field, value
    if field == "qty":
        venue.history[0] = order_count(venue.history[0], "filled_qty", value)
    holdings = custody.read_holdings(cast(TradingClient, venue), venue.now)
    (holding,) = holdings
    assert holding.symbol == "SPY"
    assert holding.frozen == []
    assert holding.held == (Decimal(str(value)) if field == "qty" else Decimal(18))


@pytest.mark.parametrize("value", [None, Missing.FIELD], ids=["none", "omitted"])
def test_p1_an_available_quantity_that_is_not_listed_is_accepted(value: object) -> None:
    """Not the tester's: added with the change to `BAD_POSITION_COUNTS` above."""
    venue = QuantityVenue()
    venue.hold("SPY", 18)
    venue.position_override = "qty_available", value
    (holding,) = custody.read_holdings(cast(TradingClient, venue), venue.now)
    assert holding.symbol == "SPY"
    assert holding.frozen == []
    assert holding.held == Decimal(18)


@pytest.mark.parametrize(("field", "value"), BAD_POSITION_COUNTS)
def test_p2_bad_position_count_freezes_without_raising(field: str, value: object) -> None:
    venue = QuantityVenue()
    # Match abs(negative) to our entry: a mismatch must not mask accepting it.
    held = {"-1": 1, "-18": 18, "-20": 20, "-20.0": 20}.get(str(value), 18)
    venue.hold("SPY", held)
    venue.position_override = field, value
    (holding,) = custody.read_holdings(cast(TradingClient, venue), venue.now)
    assert holding.symbol == "SPY"
    assert holding.frozen
    assert any("SPY" in why for why in holding.frozen), holding.frozen


@pytest.mark.parametrize(("field", "value"), BAD_POSITION_COUNTS)
def test_p2_p3_bad_position_still_cancels_open_orders(field: str, value: object) -> None:
    venue = QuantityVenue()
    held = {"-1": 1, "-18": 18, "-20": 20, "-20.0": 20}.get(str(value), 18)
    venue.hold("SPY", held)
    venue.position_override = field, value
    pending = working_orders(venue)
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    assert set(pending) <= set(report.canceled), report.lines()
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)
    assert "AAPL" not in report.frozen, report.lines()


class CloseAnswerVenue(QuantityVenue):
    """Only a returned close lookup is malformed, after a valid simulated fill.

    Do not corrupt history: custody's last-shown read must check its own input,
    even when the later account/history reads happen to be well formed.
    """

    def __init__(self, shown: object) -> None:
        super().__init__()
        self.shown = shown

    def get_order_by_client_id(self, client_id: str) -> Order:
        order = super().get_order_by_client_id(client_id)
        if client_id.startswith(custody.HAND_CLOSE_PREFIX):
            return order_count(order, "filled_qty", self.shown)
        return order


@pytest.mark.parametrize("value", BAD_COUNTS)
def test_p2_last_shown_close_fill_is_checked_and_named(value: object) -> None:
    venue = CloseAnswerVenue(value)
    venue.hold("SPY", 1)
    pending = working_orders(venue)
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    # The malformed answer arrives after the one authorized close was sent.
    assert len(venue.sent) == 1, venue.sent
    assert_frozen(report)


@pytest.mark.parametrize("value", GOOD_FILLS)
def test_p1_last_shown_close_accepts_finite_nonnegative_fill(value: str | float) -> None:
    venue = CloseAnswerVenue(value)
    venue.hold("SPY", 2)
    report = standins.close(venue)
    assert len(venue.sent) == 1
    assert report.frozen == {}
    assert not report.failed


@pytest.mark.parametrize(
    ("scenario", "expected", "held", "sell"),
    [
        ("all-own-two", 20, 20, 20),
        ("all-own-three", 20, 20, 20),
        ("mixed", 18, 18, 18),
        ("mixed-excess", 18, 20, None),
        ("sell-chain", 12, 12, 12),
        ("zero-rejected", 18, 18, 18),
        ("foreign-excess", 20, 38, None),
        ("foreign-successor", 18, 20, None),
    ],
)
def test_p4_well_formed_chains_keep_the_base_result(
    scenario: str, expected: int, held: int, sell: int | None
) -> None:
    venue = QuantityVenue()
    if scenario in ("all-own-two", "foreign-excess"):
        standins.chain(venue, "18", "20")
    elif scenario == "all-own-three":
        standins.chain(venue, "16", "18", "20")
    elif scenario in ("mixed", "mixed-excess"):
        standins.chain(venue, "16", "18", "20", by_hand=(1,))
    elif scenario == "sell-chain":
        venue.hold("SPY", 20)
        standins.chain(venue, "5", "8", side="sell", first="O-20261111-145500-001-001-2")
    elif scenario == "zero-rejected":
        standins.chain(venue, "18", "0", last="rejected")
    elif scenario == "foreign-successor":
        standins.chain(venue, "18", "20", by_hand=(1,))
    else:
        raise AssertionError(scenario)
    venue.held["SPY"] = Decimal(held)
    if scenario == "sell-chain":
        # Turned by the owning lane after the tester's hand, on the lead's ruling of 2026-11-13: a
        # sell chain of ours with fills on two orders running (5, then 8) is not counted, since
        # whether the 8 carries the 5 is not established. The tester's case required 12 and a close.
        count, why = standins.opened(venue)
        assert count is None and "not established" in why, (count, why)
        sell = None
    else:
        assert standins.opened(venue) == (Decimal(expected), "")
    report = standins.close(venue)
    assert not report.failed, report.lines()
    if sell is None:
        assert venue.sent == []
        assert_frozen(report)
        assert venue.held["SPY"] == Decimal(held)
    else:
        assert report.frozen == {}
        assert [(request.symbol, request.qty) for request in venue.sent] == [("SPY", sell)]
        assert venue.held["SPY"] == 0
        assert report.flat, report.lines()


def test_p5_negative_foreign_predecessor_cannot_authorize_selling_38() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 38, client_order_id=None)
    standins.chain(venue, "0", "-18", "20", by_hand=(1,))
    quantity, why = standins.opened(venue)
    report = standins.close(venue)
    assert venue.sent == [], (quantity, why, venue.sent)
    assert quantity is None and "SPY" in why
    assert_frozen(report)


def test_p5_own_replaced_18_share_row_with_none_is_unknown() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 18, client_order_id=None)
    standins.chain(venue, "18", "20", by_hand=(1,))
    venue.history[0] = order_count(venue.history[0], "filled_qty", None)
    quantity, why = standins.opened(venue)
    report = standins.close(venue)
    assert quantity is None, (quantity, why)
    assert "SPY" in why
    assert venue.sent == []
    assert_frozen(report)


def test_p2_negative_own_sell_successor_cannot_add_twenty_to_holdings() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 20)
    venue.held["SPY"] = Decimal(40)
    standins.chain(venue, "0", "-20", side="sell", first="O-20261111-145500-001-001-2")
    quantity, why = standins.opened(venue)
    report = standins.close(venue)
    assert venue.sent == [], (quantity, why, venue.sent)
    assert quantity is None and "SPY" in why
    assert_frozen(report)


# -- Added after the tester's hand: the cases a reader of the fix found outside this matrix (its F1 to
# F4), each written here before its fix. A position's quantity is signed at Alpaca: one below zero is a
# short, never a malformed long, and a position labelled short is frozen as one whatever it shows.


class ListedPosition(QuantityVenue):
    """SPY's position as the venue holds it, listed with the test's own fields; `late` lists them
    only once a close has been sent, so only the run's last read sees them."""

    listed: dict[str, object]
    late: bool = False

    def get_all_positions(self) -> list[Position]:
        found = super().get_all_positions()
        if self.late and not self.sent:
            return found
        return [
            Position.model_validate({**position.model_dump(), **self.listed})
            if position.symbol == "SPY"
            else position
            for position in found
        ]


class UnreadablePositions(QuantityVenue):
    """A position whose required `qty` Alpaca left out: alpaca-py refuses the whole response
    (`TradingClient.get_all_positions` validates the list), so no row reaches custody."""

    def get_all_positions(self) -> list[Position]:
        payload = super().get_all_positions()[0].model_dump()
        payload.pop("qty")
        return [Position.model_validate(payload)]


@pytest.mark.parametrize(
    "listed",
    [{"qty": "many"}, {"qty": "NaN"}, {"qty_available": "many"}, {"qty_available": "-1"}],
    ids=["qty-many", "qty-nan", "available-many", "available-minus-one"],
)
def test_f1_a_position_unreadable_only_at_the_last_read_is_frozen_by_name(listed: dict) -> None:
    venue = ListedPosition()
    venue.now = standins.AFTER_HOURS
    venue.listed, venue.late = listed, True
    venue.hold("SPY", 1)
    report = standins.close(venue)
    assert venue.sent, report.lines()  # the close went out, and did not fill
    assert venue.held["SPY"] == 1
    assert "SPY" in report.frozen, report.lines()
    assert any("frozen SPY:" in line for line in report.lines()), report.lines()
    # ours when the run began and not shown closed: never "nothing of ours left"
    assert any("SPY" in left for left in report.left), report.lines()
    assert not report.flat


def test_f2_positions_that_cannot_be_read_still_have_our_open_orders_canceled() -> None:
    venue = UnreadablePositions()
    venue.hold("SPY", 18)
    pending = working_orders(venue)
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    assert set(pending) <= set(report.canceled), report.lines()
    assert venue.sent == []
    assert "the positions" in report.failed, report.lines()
    assert not report.flat


def test_f2_a_clock_that_cannot_be_read_still_has_our_open_orders_canceled() -> None:
    class NoClock(QuantityVenue):
        def get_clock(self):
            raise ConnectionError("no clock")

    venue = NoClock()
    venue.hold("SPY", 18)
    pending = working_orders(venue)
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    assert venue.sent == []
    assert report.failed and not report.flat, report.lines()


def test_f2_when_the_open_orders_cannot_be_read_either_none_is_canceled_and_it_is_said() -> None:
    class NothingReadable(UnreadablePositions):
        def get_orders(self, filter=None):
            raise ConnectionError("no orders")

    venue = NothingReadable()
    venue.hold("SPY", 18)
    working_orders(venue)
    report = standins.close(venue)
    assert venue.canceled == [] and venue.sent == []
    assert any("our open orders were not canceled" in e for e in report.errors), report.lines()
    assert "the positions" in report.failed, report.lines()


def test_f2_a_fill_count_too_large_to_sum_freezes_its_symbol_and_our_orders_are_canceled() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 18)
    venue.history[0] = venue.history[0].model_copy(
        update={"qty": "1e1000000", "filled_qty": "1e1000000"}
    )
    pending = working_orders(venue)
    quantity, why = standins.opened(venue)
    report = standins.close(venue)
    assert quantity is None and "SPY" in why
    assert set(pending) <= set(venue.canceled), report.lines()
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)


@pytest.mark.parametrize(
    "listed",
    [
        {"side": "short", "qty": "0", "qty_available": "-1"},
        {"side": "short", "qty": "0"},
        {"side": "short", "qty": "18"},
        {"side": "short", "qty": "-18", "qty_available": "-18"},
        {"side": "short", "qty": "-18", "qty_available": "many"},
        {"side": "long", "qty": "-18"},
    ],
    ids=[
        "zero-short",
        "zero-short-alone",
        "short-above-zero",
        "short",
        "short-available-unreadable",
        "long-below-zero",
    ],
)
def test_f3_a_short_by_its_label_or_its_sign_is_frozen_as_a_short_and_nothing_is_sent(
    listed: dict,
) -> None:
    venue = ListedPosition()
    venue.listed = listed
    venue.hold("SPY", 18)
    pending = working_orders(venue)
    (holding,) = [
        h for h in custody.read_holdings(cast(TradingClient, venue), venue.now) if h.symbol == "SPY"
    ]
    assert any("a short of" in why and "SPY" in why for why in holding.frozen), holding.frozen
    assert holding.held is not None and holding.held <= 0  # never read as a long
    report = standins.close(venue)
    assert set(pending) <= set(venue.canceled), report.lines()
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)


@pytest.mark.parametrize("qty", ["many", "NaN", "inf", ""])
def test_f3_a_short_whose_quantity_is_no_number_is_frozen_as_unknown(qty: str) -> None:
    venue = ListedPosition()
    venue.listed = {"side": "short", "qty": qty}
    venue.hold("SPY", 18)
    (holding,) = custody.read_holdings(cast(TradingClient, venue), venue.now)
    assert holding.held is None
    assert any("SPY" in why for why in holding.frozen), holding.frozen
    assert any("a short of" in why for why in holding.frozen), holding.frozen


def test_f4_an_empty_quantity_beside_a_notional_is_no_count() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 2)
    by_hand = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", filled_qty="0.5", status="canceled"
    )
    venue.history[-1] = by_hand.model_copy(update={"qty": "", "notional": "100"})
    quantity, why = standins.opened(venue)
    report = standins.close(venue)
    assert quantity is None and "SPY" in why
    assert venue.sent == []
    assert_frozen(report)


@pytest.mark.parametrize("notional", [None, "", "many", "0", "-100", "NaN"])
def test_f4_no_quantity_beside_no_positive_notional_is_no_count(notional: object) -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 2)
    by_hand = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", filled_qty="0.5", status="canceled"
    )
    venue.history[-1] = by_hand.model_copy(update={"qty": None, "notional": notional})
    quantity, why = standins.opened(venue)
    assert quantity is None and "SPY" in why


def test_f4_the_documented_notional_form_is_still_accepted() -> None:
    venue = QuantityVenue()
    venue.hold("SPY", 2)
    by_hand = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", filled_qty="0.5", status="canceled"
    )
    venue.history[-1] = by_hand.model_copy(update={"qty": None, "notional": "100"})
    assert standins.opened(venue) == (Decimal(2), "")


# -- Added after the tester's hand: what a second reader of the fix found (its R2-F1). A quantity
# that cannot be read at the run's first read froze its symbol, but the read after our orders were
# canceled replaced that read whole, so a clean second listing lost the freeze and a close went out.


class FirstListingBad(QuantityVenue):
    """SPY is listed with a quantity that cannot be read in the first answer only: the first
    positions answer (`bad_position`, the fields to list), the first open-orders answer (`bad_order`), or
    the first answer listing the symbol's orders of the lookback (`bad_history`). `clock_fails_at` makes
    the clock read of that number raise."""

    bad_position: dict[str, object] | None = None
    bad_order: bool = False
    bad_history: bool = False
    clock_fails_at: int = 0

    def __init__(self) -> None:
        super().__init__()
        self.position_reads = self.open_reads = self.history_reads = self.clock_reads = 0

    def get_clock(self):
        self.clock_reads += 1
        if self.clock_reads == self.clock_fails_at:
            raise ConnectionError("no clock")
        return super().get_clock()

    def get_all_positions(self) -> list[Position]:
        found = super().get_all_positions()
        self.position_reads += 1
        if self.bad_position is None or self.position_reads > 1:
            return found
        return [
            Position.model_validate({**p.model_dump(), **self.bad_position})
            if p.symbol == "SPY"
            else p
            for p in found
        ]

    def get_orders(self, filter=None):
        found = super().get_orders(filter)
        status = getattr(filter, "status", None)
        if self.bad_order and status == QueryOrderStatus.OPEN:
            self.open_reads += 1
            if self.open_reads == 1:
                return [
                    order_count(o, "filled_qty", None) if o.symbol == "SPY" else o for o in found
                ]
        if (
            self.bad_history
            and status == QueryOrderStatus.ALL
            and "SPY" in (getattr(filter, "symbols", None) or ())
        ):
            self.history_reads += 1
            if self.history_reads == 1:
                return [order_count(o, "filled_qty", None) for o in found]
        return found


def first_listing_bad(**bad: object) -> tuple[FirstListingBad, str]:
    """A long of ours in SPY with our open sell, beside a long of ours in AAPL with nothing open."""
    venue = FirstListingBad()
    for name, value in bad.items():
        setattr(venue, name, value)
    venue.hold("SPY", 1)
    venue.hold("AAPL", 1)
    venue.history[-1] = venue.history[-1].model_copy(
        update={"client_order_id": standins.OURS[1], "symbol": "AAPL"}
    )
    resting = venue.open(
        client_order_id=standins.OURS[2], symbol="SPY", side="sell", qty="1", limit_price="999"
    )
    return venue, str(resting.client_order_id)


FIRST_ONLY = [
    pytest.param({"bad_position": {"qty": "many"}}, id="position-many"),
    pytest.param({"bad_position": {"qty": "NaN"}}, id="position-nan"),
    pytest.param({"bad_position": {"qty_available": "many"}}, id="available-many"),
    pytest.param({"bad_order": True}, id="open-order-no-fill-count"),
    pytest.param({"bad_history": True}, id="history-row-no-fill-count"),
]


@pytest.mark.parametrize("bad", FIRST_ONLY)
def test_r2_a_quantity_unreadable_only_at_the_first_read_keeps_its_symbol_frozen(bad: dict) -> None:
    venue, resting = first_listing_bad(**bad)
    report = standins.close(venue)
    assert resting in venue.canceled, report.lines()  # our open order is still canceled
    assert not any(request.symbol == "SPY" for request in venue.sent), venue.sent
    assert_frozen(report)
    assert any("at the first read" in why for why in report.frozen["SPY"]), report.lines()
    # the symbol beside it is not held back
    assert [(request.symbol, request.qty) for request in venue.sent] == [("AAPL", 1)]
    assert "AAPL" not in report.frozen, report.lines()


@pytest.mark.parametrize("bad", FIRST_ONLY)
def test_r2_a_symbol_frozen_for_the_run_is_reported_frozen_and_never_as_ours_left(
    bad: dict,
) -> None:
    """The run's last read is clean, but the symbol is frozen for the run: its position is reported
    as frozen, never as ours left, and the task's exit is that of frozen symbols only."""
    venue, _ = first_listing_bad(**bad)
    report = standins.close(venue)
    assert not any("SPY" in left for left in report.left), report.lines()
    assert report.flat, report.lines()
    assert "SPY" in report.frozen


def test_r2_the_exit_of_a_run_with_a_symbol_frozen_at_its_first_read_is_two(capsys) -> None:
    venue, _ = first_listing_bad(bad_position={"qty": "many"})
    clocks = standins.Clocks()
    code = custody.close_task(
        cast(TradingClient, venue),
        standins.Quotes(),
        sleep=clocks.sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )
    assert code == 2, capsys.readouterr().out
    assert "frozen SPY: at the first read:" in capsys.readouterr().out


@pytest.mark.parametrize("bad", FIRST_ONLY)
def test_r2_a_run_that_fails_after_its_first_read_still_names_what_that_read_could_not_count(
    bad: dict,
) -> None:
    # the clock is read once before the first read of the account and again after our cancels
    venue, resting = first_listing_bad(**bad, clock_fails_at=2)
    report = standins.close(venue)
    assert resting in venue.canceled, report.lines()
    assert report.failed, report.lines()
    assert venue.sent == []
    assert "SPY" in report.frozen, report.lines()
    assert any("frozen SPY:" in line for line in report.lines()), report.lines()


def test_r2_a_symbol_with_nothing_listed_after_the_cancel_is_still_named() -> None:
    """Our open order shows no fill count once; after its cancel nothing is listed in the symbol.
    Nothing is sent, and the report still names the quantity it could not read."""
    venue = FirstListingBad()
    venue.bad_order = True
    resting = venue.open(
        client_order_id=standins.OURS[2], symbol="SPY", side="buy", qty="1", limit_price="1"
    )
    report = standins.close(venue)
    assert str(resting.client_order_id) in venue.canceled, report.lines()
    assert venue.sent == []
    assert any("at the first read" in why for why in report.frozen["SPY"]), report.lines()


def test_r2_two_orders_unreadable_alike_are_named_once() -> None:
    venue, _ = first_listing_bad(bad_order=True)
    venue.open(
        client_order_id="O-20261111-150428-001-001-77",
        symbol="SPY",
        side="sell",
        qty="1",
        limit_price="998",
    )
    report = standins.close(venue)
    carried = [why for why in report.frozen["SPY"] if why.startswith("at the first read")]
    assert len(carried) == len(set(carried)) == 1, report.lines()


def test_r2_two_orders_unreadable_alike_are_named_once_when_the_run_fails_next() -> None:
    venue, _ = first_listing_bad(bad_order=True, clock_fails_at=2)
    venue.open(
        client_order_id="O-20261111-150428-001-001-77",
        symbol="SPY",
        side="sell",
        qty="1",
        limit_price="998",
    )
    report = standins.close(venue)
    assert report.failed, report.lines()
    assert len(report.frozen["SPY"]) == len(set(report.frozen["SPY"])), report.lines()


def test_r2_a_first_read_that_is_clean_freezes_nothing() -> None:
    venue, resting = first_listing_bad()
    report = standins.close(venue)
    assert resting in venue.canceled, report.lines()
    assert report.frozen == {}
    assert sorted((request.symbol, request.qty) for request in venue.sent) == [
        ("AAPL", 1),
        ("SPY", 1),
    ]
