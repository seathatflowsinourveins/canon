"""H1/H3 from the lead (2026-11-12; restated 2026-11-13 00:12), tested at 1fab3d0e.

Contract: the held stop survives cheaper partial fills and remembers a breach between steps;
the sale that ends it records its level. These are shaped cache/event sequences, not market data.
Reuse the existing holding/Seam fixtures and Mover methods. NautilusTrader's pinned contract
(d53849b216e2, python/nautilus_trader/{trading,model}/__init__.pyi) supplies on_trade/on_quote,
TradeTick/QuoteTick, and their event times. No future production names are assumed.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TriggerType

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover_breach import print_at
from tests.test_mover_breach import quote_at
from tests.test_mover_print_breach import holding

D = Decimal
TRIGGERS = [TriggerType.LAST_PRICE, TriggerType.BID_ASK]


def budget_hold(trigger):
    """Three of six shares filled at 10.00; L=0.20; no followed price yet.

    LAST_PRICE has a two-sided quote but no print; BID_ASK has no positive bid.
    The mutable clock follows test_mover.OneAccount's now_ns seam. Callback counters and
    slots are Mover's existing plain attributes, absent from the protection-only Seam.
    """
    bid, ask = ("9.99", "10.01") if trigger == TriggerType.LAST_PRICE else ("0", "0")
    seam, slot, filled, last = holding(trigger, bid=bid, ask=ask)
    seam.settings = mover.MoverConfig(
        max_notional=D("60"),
        max_move=D("5"),
        size_from_budget=True,
        planned_loss=D("1.20"),
        trigger=trigger,
        breach_count=1,
        high_count=1,
    )
    seam.slots = {AAA: slot}
    seam.trades = seam.quotes = 0
    seam.now_ns = filled
    seam._now_ns = lambda: seam.now_ns
    seam.clock = SimpleNamespace(timestamp_ns=seam._now_ns)
    seam.orders["E-1"].is_closed = False
    slot.sized_loss = D("0.20")
    slot.entry_record = {"client_order_id": "E-1", "stopped_out": False}
    step = filled + SECOND
    protect(seam, slot, 3, step)
    assert seam.settings.size_from_budget and not seam.persistence
    assert seam.sent == [] and not slot.exiting
    assert slot.held_stop == D("9.80")
    return seam, slot, step, last


def protect(seam, slot, quantity, now):
    """Advance the synthetic clock and run the existing protection step."""
    seam.now_ns = now
    seam._protect(slot, D(quantity), now)


def cheaper_fill(seam, now):
    """Cache after the remaining three fill at 9.80: (3*10.00 + 3*9.80)/6 = 9.90.

    As in the existing held-stop tests, supply the cached average and fill history directly;
    no execution adapter or fill simulator is involved.
    """
    seam.fills.append(("E-1", OrderSide.BUY.name, "3", "9.80", now))
    seam.orders["E-1"].avg_px = 9.90
    seam.orders["E-1"].is_closed = True


def followed_price(seam, last, trigger, price, now):
    """Update the cache before delivering real tick objects to the existing callbacks.

    Every print is after the entry, strictly newer than its predecessor, and at its quote's bid,
    with matching venue/receipt times. BID_ASK receives quotes only.
    """
    seam.now_ns = now
    quote = quote_at(now - T0, price, str(D(price) + D("0.02")))
    seam.cache.quote = lambda _id: quote
    mover.Mover.on_quote(seam, quote)
    if trigger == TriggerType.LAST_PRICE:
        trade = print_at(now - T0, price)
        last.append(trade)
        mover.Mover.on_trade(seam, trade)


@pytest.mark.parametrize("trigger", TRIGGERS, ids=["last_price", "bid_ask"])
def test_cheaper_partial_fill_never_lowers_the_budget_sized_held_stop(trigger):
    """H3: no sale and no higher trigger occurred, so the 9.80 hold must remain 9.80."""
    seam, slot, step, _ = budget_hold(trigger)
    cheaper_fill(seam, step + SECOND // 2)
    protect(seam, slot, 6, step + SECOND)
    assert seam.sent == [] and not slot.exiting
    assert slot.held_stop == D("9.80")


@pytest.mark.parametrize("trigger", TRIGGERS, ids=["last_price", "bid_ask"])
def test_first_price_after_cheaper_partial_fill_sells_through_the_original_hold(trigger):
    """H3's consequence: 9.75 crosses 9.80 even though it is above 9.90 minus L (9.70)."""
    seam, slot, step, last = budget_hold(trigger)
    cheaper_fill(seam, step + SECOND // 2)
    protect(seam, slot, 6, step + SECOND)
    assert seam.sent == [] and not slot.exiting
    followed_price(seam, last, trigger, "9.75", step + SECOND + SECOND // 2)
    protect(seam, slot, 6, step + 2 * SECOND)
    assert [order.kind for order in seam.sent] == ["LIMIT"]
    assert slot.exiting and slot.held_stop is None
    assert seam.sent[0].quantity.as_decimal() == D(6)


@pytest.mark.parametrize("trigger", TRIGGERS, ids=["last_price", "bid_ask"])
def test_breach_then_recovery_between_steps_still_sells_the_held_stop(trigger):
    """H1: with both counts at one, a 9.70 breach cannot be erased by the later 9.90."""
    seam, slot, step, last = budget_hold(trigger)
    followed_price(seam, last, trigger, "9.70", step + SECOND // 5)
    followed_price(seam, last, trigger, "9.90", step + 4 * SECOND // 5)
    assert seam._followed(slot) == D("9.90")
    protect(seam, slot, 3, step + SECOND)
    assert [order.kind for order in seam.sent] == ["LIMIT"]
    assert slot.exiting and slot.held_stop is None
    assert seam.sent[0].quantity.as_decimal() == D(3)


@pytest.mark.parametrize("trigger", TRIGGERS, ids=["last_price", "bid_ask"])
def test_the_sale_ending_a_hold_records_that_stops_level_in_sell_triggers(trigger):
    """Record: the 9.75 observation elects the sale, whose stop level is still 9.80."""
    seam, slot, step, last = budget_hold(trigger)
    followed_price(seam, last, trigger, "9.75", step + SECOND // 5)
    protect(seam, slot, 3, step + SECOND)
    assert [order.kind for order in seam.sent] == ["LIMIT"] and slot.exiting
    sale_id = seam.sent[0].client_order_id.value
    recorded_level = seam.sell_triggers.get(sale_id)
    assert recorded_level is not None, f"sale {sale_id} omitted its held stop: {seam.sell_triggers}"
    assert D(recorded_level) == D("9.80")
