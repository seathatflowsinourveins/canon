"""Blind H1-H6 tests of the lead's held-stop refresh contract, at aadc93af.

LEVEL follows NautilusTrader d53849b216e2, crates/execution/src/trailing.rs:98-101:
a sell's trigger only improves upward. All orders use test_mover_profile's offline seam.
"""

from collections import deque
from decimal import Decimal

import pytest
from nautilus_trader.model import Price

from tests.test_mover_profile import holding
from tests.test_mover_trail import MINUTE
from tests.test_mover_trail import SECOND
from tests.test_mover_trail import T0

D = Decimal


def test_returning_bid_refreshes_a_held_stop_after_a_fill_since_the_last_step():
    """H1: the reader's drop-in case, with no protection step between fill and quote."""
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.tightened is True
    assert slot.trail_plan.trail == D("0.007")

    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([D("11.20")], maxlen=4)
    seam._protect(slot, D(10), T0 + 3 * MINUTE)
    assert slot.held_stop == D("10.72")

    # A fill arrives while the bid is absent, then the bid returns, all
    # before the next protection step. Do not insert another _protect here.
    seam.orders["E-1"].avg_px = 11.00
    seam.bid = "10.85"
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == D("10.92")
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) == D("10.92")


def tightened_hold(*, reached=None):
    """Stop before the later fill: 10 shares at 10.80, tightened to 0.007, no bid.

    A supplied reached trigger models an already-ratcheted sale; this helper does not
    test the ratchet's decision to tighten. It lets the real protection step retain it.
    """
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.tightened is True
    assert slot.trail_plan.trail == D("0.007")
    if reached is not None:
        slot.ratcheted = True
        seam.sent[-1].trigger_price = Price.from_str(reached)

    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([D("11.20")], maxlen=4)
    seam._protect(slot, D(10), T0 + 3 * MINUTE)
    expected = D("10.72") if reached is None else max(D("10.72"), D(reached))
    assert slot.held_stop == expected
    assert slot.exiting is False
    return seam, slot


@pytest.mark.parametrize("bid", ["10.70", "10.92"], ids=["below-old-stop", "at-new-stop"])
def test_h1_the_first_quote_comparison_uses_the_refreshed_tightened_stop(bid):
    """H1: even a bid through the old stop must record the refreshed exit level."""
    seam, slot = tightened_hold()
    seam.orders["E-1"].avg_px = 11.00
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == D("10.92")
    assert str(remade[0].quantity) == "15"
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) == D("10.92")


def test_h1_a_still_unquoted_holding_checks_the_refreshed_stop_on_the_hold_path():
    """H1 control: with the bid still absent, the next close crosses the refreshed stop."""
    seam, slot = tightened_hold()
    seam.orders["E-1"].avg_px = 11.00
    slot.closes = deque([D("10.85")], maxlen=4)
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == D("10.92")
    assert str(remade[0].quantity) == "15"
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) == D("10.92")


@pytest.mark.parametrize(
    ("bid", "kind"),
    [("10.55", "LIMIT"), ("10.70", "LIMIT"), ("10.79", "LIMIT"), ("11.05", "TRAILING")],
    ids=["below-old-stop", "between-stops", "at-new-stop", "above-new-stop"],
)
def test_h2_an_untightened_holding_refreshes_at_l_before_using_the_returning_bid(bid, kind):
    """H2: 10.80 - L = 10.59 becomes 11.00 - L = 10.79 before the bid is judged."""
    seam, slot = holding(sized_loss="0.21")
    seam.trail_profile = None  # Keep the entry's untightened plan in force for this case.
    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([D("11.20")], maxlen=4)
    seam._protect(slot, D(10), T0 + SECOND)
    assert slot.tightened is False
    assert slot.held_stop == D("10.59")

    seam.orders["E-1"].avg_px = 11.00
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 2 * SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == [kind]
    if kind == "LIMIT":
        assert slot.exiting is True
        assert slot.exit_trigger == D("10.79")
    else:
        assert slot.exiting is False
        assert remade[0].trigger_price == Price.from_str("10.7900")
        assert remade[0].trailing_offset == D("0.21")
    assert str(remade[0].quantity) == "15"
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) == D("10.79")


@pytest.mark.parametrize(
    ("reached", "bid", "kind", "expected"),
    [
        ("10.80", "10.85", "LIMIT", "10.92"),
        ("11.05", "11.00", "LIMIT", "11.05"),
        ("11.05", "11.10", "TRAILING", "11.05"),
    ],
    ids=["refresh-above-reached", "reached-stop-crossed", "reached-stop-remade"],
)
def test_h3_a_ratcheted_hold_keeps_the_higher_of_the_refreshed_stop_and_reached_level(
    reached, bid, kind, expected
):
    """H3: LEVEL forbids lowering a reached sell trigger when a later fill refreshes the stop."""
    seam, slot = tightened_hold(reached=reached)
    assert slot.ratcheted is True
    seam.orders["E-1"].avg_px = 11.00
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == [kind]
    if kind == "LIMIT":
        assert slot.exiting is True
        assert slot.exit_trigger == D(expected)
    else:
        assert slot.exiting is False
        assert remade[0].trigger_price == Price.from_str(expected)
    level = D(seam.sell_triggers[remade[0].client_order_id.value])
    assert level == D(expected)
    assert level >= D(reached)
    assert str(remade[0].quantity) == "15"


@pytest.mark.parametrize(
    ("bid", "kind"),
    [("0", None), ("10.70", "LIMIT"), ("10.75", "TRAILING")],
    ids=["still-held", "return-crosses-kept-stop", "return-above-kept-stop"],
)
def test_h4_no_room_for_l_preserves_the_tightened_holdings_existing_stop(bid, kind):
    """H4: an average of 0.20 has no room for L = 0.21; the held 10.72 stays in force."""
    seam, slot = tightened_hold()
    seam.orders["E-1"].avg_px = 0.20
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    if kind is None:
        assert remade == []
        assert slot.exiting is False
        assert slot.held_stop == D("10.72")
        return
    assert [order.kind for order in remade] == [kind]
    if kind == "LIMIT":
        assert slot.exiting is True
        assert slot.exit_trigger == D("10.72")
    else:
        assert slot.exiting is False
        assert remade[0].trigger_price == Price.from_str("10.7200")
    assert str(remade[0].quantity) == "15"
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) == D("10.72")


@pytest.mark.parametrize(
    ("average", "bid", "kind"),
    [
        ("10.60", "10.70", "LIMIT"),
        ("10.60", "10.90", "TRAILING"),
        ("9.90", "10.70", "LIMIT"),
        ("9.90", "10.90", "TRAILING"),
    ],
    ids=["small-fall-crossed", "small-fall-remade", "large-fall-crossed", "large-fall-remade"],
)
def test_h5_a_lower_average_never_widens_the_stop_beyond_l(average, bid, kind):
    """H5: a cheaper fill cannot relax the held level, including when it exceeds the average."""
    seam, slot = tightened_hold()
    seam.orders["E-1"].avg_px = float(average)
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == [kind]
    if kind == "LIMIT":
        assert slot.exiting is True
        assert slot.exit_trigger == D("10.72")
    else:
        assert slot.exiting is False
        assert remade[0].trigger_price == Price.from_str("10.7200")
        assert remade[0].trailing_offset <= D("0.21")
    level = D(seam.sell_triggers[remade[0].client_order_id.value])
    assert level == D("10.72")
    assert D(average) - level <= D("0.21")
    assert str(remade[0].quantity) == "15"


@pytest.mark.parametrize("bid", ["10.93", "11.20"], ids=["just-above-stop", "well-above-stop"])
def test_h6_a_return_above_the_refreshed_stop_remakes_from_that_level(bid):
    """H6: the next TRAILING sale must be seeded at least at 10.92, with no interim hold step."""
    seam, slot = tightened_hold()
    seam.orders["E-1"].avg_px = 11.00
    seam.bid = bid
    sent = len(seam.sent)
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["TRAILING"]
    assert remade[0].trigger_price >= Price.from_str("10.9200")
    assert D(seam.sell_triggers[remade[0].client_order_id.value]) >= D("10.92")
    assert remade[0].trailing_offset == D("0.08")
    assert str(remade[0].quantity) == "15"
    assert slot.exiting is False
