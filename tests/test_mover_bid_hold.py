"""#320's held stop under `BID_ASK`, main's default (the lead's H1-H3, 2026-11-12; the BID_ASK twin of
the xr3 fast reader's SF1 on the print trigger). A holding with no bid is held at its fill less the
trail. Before this, when a bid returned the hold ended and the trail was armed from that bid with no
trigger: a bid through the held stop did not sell, and the protection started at the bid less the
trail, under the held stop. The hold's end is one rule for both triggers (`Mover._trail`)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price

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


def test_under_the_bid_an_off_step_held_stop_never_falls_when_trailing_resumes() -> None:
    """The BID_ASK twin of xr7-cx-succ/F3: ending the 9.8049 hold remakes the trigger at 9.81."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam, bought="10.0149"))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.0149", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    assert slot.sized_loss is None
    seam._protect(slot, Decimal(3), filled + SECOND)
    old_level = slot.held_stop
    assert old_level == Decimal("9.81") and seam.sent == []

    seam.bid = "9.90"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING"]
    assert seam.sent[0].trigger_price.as_decimal() >= old_level
    assert seam.sent[0].trigger_price.as_decimal() == Decimal("9.81")
    assert slot.held_stop is None and slot.held_since is None and not slot.exiting


@pytest.mark.parametrize("bid", ["9.8050", "9.8100"])
def test_under_the_bid_a_bid_meeting_the_rounded_up_held_stop_sells_by_limit(bid: str) -> None:
    """A bid above 9.8049 but at or below the rounded 9.81 meets the replacement immediately."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam, bought="10.0149"))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.0149", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert slot.held_stop == Decimal("9.81") and seam.sent == []

    seam.bid = bid
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"]
    assert slot.exiting and slot.held_stop is None and slot.held_since is None
    assert slot.trail_trigger is None and slot.trail_limit is None
    assert slot.exit_trigger == Decimal("9.81")
    assert seam.sent[0].quantity.as_decimal() == Decimal(3)
    assert Decimal(seam.sell_triggers[seam.sent[0].client_order_id.value]) == Decimal("9.81")


def test_a_sale_through_the_held_stop_at_the_holds_end_records_its_trigger_and_drops_the_trail() -> (
    None
):
    """The hold's end sells through the held stop as `_hold` does: the stop it sold through is kept
    beside the sale (`sell_triggers`), and a trigger an earlier trailed sale had reached no longer
    stands beside a sale under way (code-review-high-succ/1 and /10)."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, Decimal(3), filled + SECOND)
    slot.trail_trigger, slot.trail_limit = Decimal("9.60"), Decimal("9.50")
    seam.bid = "9.70"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert seam.sell_triggers == {seam.sent[0].client_order_id.value: "9.80"}
    assert slot.trail_trigger is None and slot.trail_limit is None


def test_a_held_stop_is_met_by_the_followed_price_when_the_anchor_cannot_be_read() -> None:
    """The anchor read raises (no entry price, position average or bid it accepts): the followed price
    through the held stop still sells, and the hold is not ended unprotected (code-review-high-succ/5)."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, Decimal(3), filled + SECOND)

    def unreadable(_slot: Any) -> Decimal:
        raise RuntimeError("no anchor")

    seam._trail_anchor = unreadable
    seam.bid = "9.90"
    seam._protect(slot, Decimal(3), filled + 2 * SECOND)
    assert seam.sent == [] and slot.held_since is not None and slot.held_stop == Decimal("9.80")
    seam.bid = "9.70"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"


def test_under_the_bid_a_bid_through_the_held_stop_and_back_before_the_step_sells() -> None:
    """The same hold; between two steps the bid returns at 9.70, through the 9.80 stop, then 9.90. The
    step sees 9.90, and the hold's end still sells through the stop the bid went through (the xr4
    reader's F2), not arming a trail at 9.80 under a price that has already met it."""
    seam = cast(Any, Seam("0"))
    slot = cast(Any, seam_slot(seam))
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam.slots, seam.quotes = {slot.instrument_id: slot}, 0
    seam._protect(slot, Decimal(3), filled + SECOND)
    seam.on_quote(
        SimpleNamespace(
            instrument_id=slot.instrument_id,
            bid_price=Price.from_str("9.70"),
            ts_event=filled + 2 * SECOND,
            ts_init=filled + 2 * SECOND,
        )
    )
    seam.bid = "9.90"
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"
