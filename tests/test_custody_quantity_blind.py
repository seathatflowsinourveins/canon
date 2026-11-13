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


@pytest.mark.parametrize("field", ("qty", "qty_available"))
@pytest.mark.parametrize("value", BAD_COUNTS)
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


@pytest.mark.parametrize("field", ("qty", "qty_available"))
@pytest.mark.parametrize("value", BAD_COUNTS)
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
