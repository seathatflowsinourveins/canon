"""Alpaca's documented price step through the pinned NautilusTrader tick schemes, offline."""

from decimal import Decimal

import pytest

from canon_trading import mover
from canon_trading import price_step


@pytest.mark.parametrize(
    ("kept", "expected"),
    [
        ("10.4237", "10.42"),
        ("0.4237", "0.4237"),
        ("10.29", "10.29"),
        ("1.00", "1.00"),
        # finer than the finest step, within the nudge of the tick above: still down
        ("10.429995", "10.42"),
        ("0.42369", "0.4236"),
        ("0.99999", "0.9999"),
        # above the scheme's last tick: a cent down, not the last tick (4999.99)
        ("6000.2371", "6000.23"),
        ("5000", "5000"),
        # at or below its lowest tick: the finest step down, not raised to 0.0001
        ("0.0001", "0.0001"),
        ("0.00005", "0"),
    ],
)
def test_a_long_stop_trigger_rounds_down_on_the_venue_step(kept: str, expected: str) -> None:
    assert price_step.stop_trigger_price(Decimal(kept)) == Decimal(expected)


@pytest.mark.parametrize(
    ("bid", "ticks", "expected"),
    [
        ("6000.237", 0, "6000.23"),
        ("6000.237", 2, "6000.21"),
        ("5000", 0, "5000.00"),
        ("5000.0001", 1, "4999.99"),
        # an offset that runs back into the expansion goes on there, across the dollar too
        ("6000.237", 200_000, "4000.23"),
        ("6000.237", 600_000, "0.9923"),
    ],
)
def test_a_sell_limit_above_the_schemes_top_is_on_the_cent_through_the_bid(
    bid: str, ticks: int, expected: str
) -> None:
    """The lead's ruling (ii), 2026-11-12: above the scheme's last tick (4999.99) a sell goes on at
    Alpaca's cent step; it is never clamped to that tick or to the lowest."""
    assert price_step.exit_price(Decimal(bid), ticks, None) == Decimal(expected)


def test_a_sell_limit_above_the_top_still_keeps_its_floor() -> None:
    assert price_step.exit_price(Decimal("6000.237"), 5, Decimal("6000.20")) == Decimal("6000.20")


@pytest.mark.parametrize(("ask", "ticks"), [("5000", 0), ("6000.237", 0), ("4999.99", 1)])
def test_an_entry_above_the_schemes_top_is_refused_by_that_name(ask: str, ticks: int) -> None:
    """The lead's ruling (ii), 2026-11-12: a buy whose limit would be above the scheme's last tick is
    refused, by a name that says so."""
    with pytest.raises(ValueError, match="above the top tier of ALPACA_US_EQUITY_LIMIT"):
        price_step.entry_price(Decimal(ask), ticks)


@pytest.mark.parametrize(
    ("price", "expected"),
    [("5880.2254", "5880.23"), ("6000", "6000"), ("4999.99", "4999.99"), ("10.0254", "10.03")],
)
def test_a_bound_on_a_sale_rounds_up_on_the_step_above_the_top_too(
    price: str, expected: str
) -> None:
    assert price_step.up_on_step(Decimal(price)) == Decimal(expected)


def test_a_chase_floor_above_the_top_is_under_the_first_sale() -> None:
    """code-review high on (c), finding 1: the floor of a sale first priced above 4999.99 was that price
    itself, so the sale was never chased."""
    assert mover.chase_floor(Decimal("6000.23"), Decimal("0.02")) == Decimal("5880.23")


@pytest.mark.parametrize("bid", ["6000.237", "4000"])
def test_a_negative_offset_is_refused_above_the_top_as_below_it(bid: str) -> None:
    with pytest.raises(ValueError, match="n must be >= 0"):
        price_step.exit_price(Decimal(bid), -1, None)


@pytest.mark.parametrize("bid", ["NaN", "Infinity", "-Infinity"])
def test_a_bid_that_is_not_a_number_is_refused_as_a_value_error(bid: str) -> None:
    with pytest.raises(ValueError, match="no price on Alpaca's step"):
        price_step.exit_price(Decimal(bid), 0, None)
