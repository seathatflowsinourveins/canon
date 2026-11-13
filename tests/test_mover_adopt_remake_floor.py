"""A remake of an adopted slot keeps main's floor: only the SUPPLIED level goes up onto Alpaca's step.

The lead's ruling (2026-11-13, 15:48, on unit 5's round-2 review): rounding every remake trigger of an
adopted slot up onto the step sold a holding whose bid never met the trigger the emulator had reached,
at once, on one observation. A remake rounds down onto the step (`price_step.stop_trigger_price`), as
main does for every slot; the supplied level alone is rounded up (`price_step.up_on_step`).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import strategy
from tests.test_mover_adopt import Adopting
from tests.test_mover_adopt import quote
from tests.test_mover_adopt import replay


class MoreShares(Adopting):
    def before_adopt(self) -> None:
        self.add_price = Price.from_str("10.2000")


def test_a_remake_with_the_bid_above_the_reached_trigger_does_not_sell() -> None:
    """The first arm activates at 10.1737 with a 0.21 trail, so the emulator's trigger is 9.9637;
    the bid then dips to 9.9650, above it, and more shares force a remake. Rounded up, the remake's
    trigger would be 9.97, met by 9.9650; floored as main, it is 9.96 and nothing sells."""
    s = replay(
        "9.9555",
        quote("10.1737", 2 * SECOND + SECOND // 2),
        quote("9.9650", 2 * SECOND + 3 * SECOND // 4),
        add_shares=True,
        kind=MoreShares,
    )
    assert s.steps[-1]["held"] == Decimal(10), (
        "a remake sold a holding whose bid never met its reached trigger"
    )


def cent_quote(bid: str, offset: int, *, instrument=AAA) -> QuoteTick:
    """A quote on the cent above $1 (0.0001 under), a cent or 0.001 wide."""
    b = Decimal(bid)
    ask = b + (Decimal("0.0010") if b < 1 else Decimal("0.01"))
    return QuoteTick(
        instrument,
        Price.from_str(bid),
        Price.from_str(f"{ask:.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        T0 + offset,
        T0 + offset,
    )


class Cheap(Adopting):
    """Bought at 0.97, so the trailing offset is on the 0.0001 step; the price then crosses $1."""

    def on_start(self) -> None:
        super(Adopting, self).on_start()
        self.subscribe_quotes(AAA)
        order = self.order_factory.limit(
            AAA, OrderSide.BUY, Quantity.from_int(7), Price.from_str("0.9700")
        )
        self.seed_id = order.client_order_id
        self.submit_order(order)

    def before_adopt(self) -> None:
        self.add_price = Price.from_str("1.3000")


@pytest.mark.parametrize(
    "arm",
    [{}, {"trail_mode": "atr", "trail_cap": "own", "atr_k": Decimal(1)}],
    ids=["fixed", "OWN1"],
)
def test_cent_bids_a_remake_does_not_sell_above_the_reached_trigger(arm) -> None:
    """Every bid on the cent: the bid rises to 1.20, the emulator's trigger is off the cent at 1.1809,
    the bid dips to 1.19 and more shares force a remake. Floored, the remake's trigger is 1.18."""
    eng = engine()
    s = Cheap(strategy(entries=1, extended_hours=True, **arm).settings)
    s.level, s.add_shares = Price.from_str("0.9000"), True
    eng.add_strategy(s)
    eng.add_data(
        [
            cent_quote("0.9500", 0),
            cent_quote("0.9500", SECOND),
            cent_quote("1.2000", 2 * SECOND + SECOND // 2),
            cent_quote("1.1900", 2 * SECOND + 3 * SECOND // 4),
            cent_quote("20.0000", 8 * SECOND, instrument=BBB),
        ]
    )
    try:
        eng.run()
        assert not s.errors, s.errors
        assert s.steps[-1]["held"] == Decimal(10), "sold above the reached trigger"
    finally:
        eng.dispose()
