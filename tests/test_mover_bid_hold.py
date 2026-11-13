"""#320's held stop under `BID_ASK`, main's default (the lead's H1-H3, 2026-11-12; the BID_ASK twin of
the xr3 fast reader's SF1 on the print trigger). A holding with no bid is held at its fill less the
trail. Before this, when a bid returned the hold ended and the trail was armed from that bid with no
trigger: a bid through the held stop did not sell, and the protection started at the bid less the
trail, under the held stop. The hold's end is one rule for both triggers (`Mover._trail`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from typing import cast

from nautilus_trader.model import OrderSide

from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import seam_slot


def test_under_the_bid_a_returning_bid_through_the_held_stop_sells() -> None:
    """Filled 3 at 10.00 with no bid: held at 9.80. The bid returns at 9.70, through the held stop:
    the hold's sale by limit, its reason the held stop (H1), not a trail from 9.70 protecting at 9.50."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert slot.held_stop == Decimal("9.80") and seam.sent == []
    seam.bid = "9.70"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"


def test_under_the_bid_a_returning_bid_above_the_held_stop_arms_the_trail_from_it() -> None:
    """The same hold; the bid returns at 9.90, above the 9.80 stop: the trail goes out with its trigger
    at the held stop (H2, H3), not at 9.90 less the trail."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, Decimal(3), filled + SECOND)
    seam.bid = "9.90"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING"] and not slot.exiting
    assert seam.sent[0].trigger_price is not None
    assert Decimal(str(seam.sent[0].trigger_price)) == Decimal("9.80")
