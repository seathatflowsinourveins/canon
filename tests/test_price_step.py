"""Alpaca's documented price step through the pinned NautilusTrader tick schemes, offline."""

from decimal import Decimal

import pytest

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
