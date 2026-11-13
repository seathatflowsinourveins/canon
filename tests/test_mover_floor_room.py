"""The readers' cases for one Mover unit: a held stop follows a fill after its last hold step, no sell is
sent or held with a trigger at or under zero, and the sizing row names one distance. Each case is the
reader's own input; the seams and engines are test_mover's, test_mover_profile's and test_mover_sizing's.
The pinned emulator seeds a sell's trailing trigger at the followed price less the offset and moves it
only up (NautilusTrader d53849b216e2, crates/execution/src/trailing.rs:61, :100)."""

from collections import deque
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.common import Clock
from nautilus_trader.common import OrderFactory
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType

from canon_trading import mover
from canon_trading.sizing import LossPlan
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import tape
from tests.test_mover_profile import holding
from tests.test_mover_sizing import OWN
from tests.test_mover_sizing import sent
from tests.test_mover_sizing import stepping_to
from tests.test_mover_trail import sized_cold_holding

D = Decimal


# --- the DeepSeek reader of #343, F1: a fill after the last hold step ---------------------------------


def test_a_fill_after_the_last_hold_step_is_in_the_retained_stop() -> None:
    """dsh-reader-343, F1: a tightened holding's stop held at 10.72 under a 10.80 average; a later fill
    raises the average to 11.00 with no hold step after it, and the bid returns at 10.85. The stop the
    returning price meets is the one at the distance in force under 11.00, 10.92: a sale at once."""
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)  # tightens the 0.02 plan to 0.007
    assert slot.tightened is True
    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([D("11.20")], maxlen=4)
    seam._protect(slot, D(10), T0 + 3 * MINUTE)
    assert slot.held_stop == D("10.72")
    seam.orders["E-1"].avg_px = 11.00  # later fill; no held step after it
    seam.bid = "10.85"
    seam._protect(slot, D(15), T0 + 3 * MINUTE + SECOND)
    assert [(o.kind, o.trigger_price) for o in seam.sent[-1:]] == [("LIMIT", None)]
    assert slot.exit_trigger == D("10.92")


# --- the Codex reader of A3+O (xr7-r-a3o-1afe), P2: no trigger at or under zero ------------------------


def native_holding() -> tuple[Any, Any]:
    """The reader's setup: an OWN holding planned at 10.00 with a 0.30 floor in price terms, filling at
    0.20, with the pinned native OrderFactory in place of the seam's order constructor."""
    host = cast(
        Any,
        Seam(
            "0",
            trail_mode="atr",
            trail_cap="own",
            size_from_budget=True,
            loss_share=mover.PAPER_LOSS_SHARE,
        ),
    )
    host.order_factory = OrderFactory(
        TraderId("TESTER-001"), StrategyId("Mover-001"), Clock.new_test()
    )
    slot = seam_slot(host, bought="0.20")
    slot.sized_loss = D("0.30")
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.03"), D("0.0075"), D("0.03"), None, D("0.15"), "spread floor", floor=D("0.03")
    )
    slot.entry_record = {"client_order_id": "E-1"}
    return host, slot


def positive(host: Any) -> bool:
    """Every trigger sent is over zero, and every trailing sell sent without one starts over zero."""
    for order in host.sent:
        trigger = getattr(order, "trigger_price", None)
        if trigger is not None and trigger.as_decimal() <= 0:
            return False
        offset = getattr(order, "trailing_offset", None)
        if trigger is None and offset is not None and D(host.bid) - D(str(offset)) <= 0:
            return False
    return True


def test_a_first_hold_with_no_room_for_the_floor_never_holds_or_sends_a_trigger_at_or_under_zero() -> (
    None
):
    host, slot = native_holding()
    slot.closes = deque([D("0.20")], maxlen=4)
    host._protect(slot, D(5), T0)
    assert slot.held_stop is None or slot.held_stop > 0
    host.orders["E-1"].avg_px = 0.19
    slot.closes = deque([D("0.19")], maxlen=4)
    host._protect(slot, D(10), T0 + SECOND)
    assert slot.held_stop is None or slot.held_stop > 0
    host.bid = "0.19"
    host._protect(slot, D(10), T0 + 2 * SECOND)
    assert positive(host)
    assert slot.held_stop is None or slot.held_stop > 0


def test_a_direct_trail_with_no_room_for_the_floor_never_seeds_a_trigger_at_or_under_zero() -> None:
    host, slot = native_holding()
    host.bid = "0.20"
    host._protect(slot, D(5), T0)
    assert positive(host)


# --- the DeepSeek reader of #351, P2 and its P3 pins ----------------------------------------------------


def test_a_sub_dollar_stop_that_already_clears_the_floor_keeps_the_records_stop_and_loss_one(
    monkeypatch: Any,
) -> None:
    """w1-d-351, P2: the record's stop and its loss per share are one distance even when the stop
    crosses $1, L on the entry's cent step already clears the floor, and no re-size runs."""
    eng = engine()
    s = sent(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        trail=D("0.01"),
        entry_ticks=0,
        **OWN,
    )
    real = mover.plan_for_loss
    monkeypatch.setattr(
        mover, "plan_for_loss", lambda **kw: LossPlan(real(**kw).quantity, D("0.9705"))
    )
    orders = run(
        eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, stepping_to(0.995, 0.01), spread=0.005)
    )
    eng.dispose()
    (buy,) = [o for o in orders if o.side == OrderSide.BUY]
    price = buy.price.as_decimal()
    assert price == D("1.0000")
    sizing = s.entries_sent[0]["sizing"]
    assert D(sizing["stop"]) == D("0.9705")
    # the row names each distance once: L, which the D3 check reads as the opening distance, and the
    # sizing stop's own, at which the loss is recorded (L5's ruling on the reader's fix)
    assert D(sizing["loss_per_share"]) == D("0.02")
    assert D(sizing["stop_loss_per_share"]) == price - D(sizing["stop"])
    assert D(sizing["loss_at_stop"]) == buy.quantity.as_decimal() * (price - D(sizing["stop"]))


def test_the_second_sizing_under_the_floor_is_counted_and_not_sent(monkeypatch: Any) -> None:
    """w1-d-351, P3: the cross-dollar guard's second sizing still under the floor is counted by name,
    not sent; the branch is defensive (no input reaches it by rounding alone)."""
    eng = engine()
    s = sent(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        trail=D("0.01"),
        entry_ticks=0,
        **OWN,
    )
    calls: list[object] = []
    real = mover.plan_for_loss

    def stopping_lower(**kw: Any) -> LossPlan:
        calls.append(kw["distance"])
        return LossPlan(real(**kw).quantity, D("0.9950") if len(calls) > 1 else D("0.9905"))

    monkeypatch.setattr(mover, "plan_for_loss", stopping_lower)
    orders = run(
        eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, stepping_to(0.995, 0.01), spread=0.005)
    )
    eng.dispose()
    # the step at 1.0000 is refused by name; a later step at 1.02 clears the floor and may buy (the
    # reader's `not any BUY` read the whole tape)
    assert not [
        o for o in orders if o.side == OrderSide.BUY and o.price.as_decimal() == D("1.0000")
    ]
    assert s.record()["sizing_refusals"]["distance_under_floor"] >= 1
    assert any("is under the spread floor" in reason for reason in s.dropped)


def test_quoted_spread_treats_a_missing_ask_as_no_spread() -> None:
    """w1-d-351, P3: a quote with no ask has no spread."""
    quote = SimpleNamespace(bid_price=Price.from_str("10.00"), ask_price=None)
    assert mover.quoted_spread(quote) is None


# --- the Antigravity reader of #351, P3-1 -----------------------------------------------------------------


def test_warm_tighten_cap_refusal_spends_the_one_shot_check_without_noting_dropped() -> None:
    """w1-a-351, P3-1 (the reader's drop-in): a warmed ATR the cap refuses spends the one comparison;
    it is not retried each step nor written to dropped as not applied yet."""
    seam, slot = sized_cold_holding("0.20", "0.30", trail_cap="refuse")
    seam._protect(slot, D(5), T0 + SECOND)
    assert slot.warm_checked is True
    assert not any("warmed ATR not applied yet" in note for note in seam.dropped)


# --- the Codex second reader of #343, P1 ----------------------------------------------------------------


def test_returning_bid_refreshes_a_held_stop_after_a_fill_since_the_last_step() -> None:
    """sr-c-343, P1 (the reader's drop-in): a later fill and the returning bid arrive before the same
    step; the remade sale reads the held stop at the new average, not the one of the last hold step."""
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, Decimal(5), T0 + 2 * MINUTE)
    assert slot.tightened is True
    assert slot.trail_plan.trail == Decimal("0.007")

    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([Decimal("11.20")], maxlen=4)
    seam._protect(slot, Decimal(10), T0 + 3 * MINUTE)
    assert slot.held_stop == Decimal("10.72")

    seam.orders["E-1"].avg_px = 11.00
    seam.bid = "10.85"
    sent = len(seam.sent)
    seam._protect(slot, Decimal(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == Decimal("10.92")
    assert Decimal(seam.sell_triggers[remade[0].client_order_id.value]) == Decimal("10.92")


# --- the landing review of this unit, P2 ----------------------------------------------------------------


def test_a_crash_through_a_reached_trigger_under_the_floor_sells_at_once() -> None:
    """code-review-l5-floor, P2 (the review's drop-in): a followed bid through the kept trigger and
    under the offsets sells at once by limit, as main does; the no-room hold is for a sell not yet due."""
    seam = Seam("10.50")
    slot = seam_slot(seam)
    slot.trailed = True
    slot.trail_trigger = Decimal("10.38")
    slot.closes = deque([Decimal("10.40")], maxlen=4)  # the last minute close, before the crash
    seam.bid = "0.05"  # through 10.38 and under the limit offset
    cast(Any, seam)._protect(slot, Decimal(3), T0)
    # at the step that first reads the price through the trigger, not a held step later
    assert [o.kind for o in seam.sent] == ["LIMIT"]
    assert slot.exiting
    assert slot.held_since is None


# --- the held-stop refresh blind file (t-heldrefresh), H1 and H2 ----------------------------------------


def test_a_bid_through_the_old_held_stop_sells_at_the_refreshed_stop() -> None:
    """t-heldrefresh, H1 below the old stop: the followed-price check of the held stop reads it at
    the new average too, so the sale records the refreshed 10.92, not the 10.72 of the last step."""
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, Decimal(5), T0 + 2 * MINUTE)
    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([Decimal("11.20")], maxlen=4)
    seam._protect(slot, Decimal(10), T0 + 3 * MINUTE)
    assert slot.held_stop == Decimal("10.72")

    seam.orders["E-1"].avg_px = 11.00
    seam.bid = "10.70"
    sent = len(seam.sent)
    seam._protect(slot, Decimal(15), T0 + 3 * MINUTE + SECOND)

    remade = seam.sent[sent:]
    assert [order.kind for order in remade] == ["LIMIT"]
    assert slot.exit_trigger == Decimal("10.92")
    assert Decimal(seam.sell_triggers[remade[0].client_order_id.value]) == Decimal("10.92")


def test_a_refreshed_held_stop_sold_through_is_the_level_the_exit_record_keeps() -> None:
    """The combined landing's code review, finding 1: the held record writes the kept level from
    the hold (`_record_held_stop`), and the floor refreshes the held stop in `_trail` too. A sale
    through the refreshed stop must leave the record naming that stop, measured from the average it
    was refreshed at, not the last hold step's 10.72 from 10.80."""
    seam, slot = holding(sized_loss="0.21")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, Decimal(5), T0 + 2 * MINUTE)
    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "0"
    slot.closes = deque([Decimal("11.20")], maxlen=4)
    seam._protect(slot, Decimal(10), T0 + 3 * MINUTE)
    row = slot.exit_record
    assert row is not None
    assert row.held_stop_kept == Decimal("10.72")

    seam.orders["E-1"].avg_px = 11.00
    seam.bid = "10.70"
    seam._protect(slot, Decimal(15), T0 + 3 * MINUTE + SECOND)

    assert slot.exit_trigger == Decimal("10.92")
    assert row.held_stop_kept == Decimal("10.92")
    assert row.held_stop_from_average == Decimal("0.08")
    assert slot.held_average == Decimal("11.00")


@pytest.mark.parametrize("recover_before_next_step", [False, True])
def test_a_new_no_room_hold_does_not_lose_its_already_breached_bid(
    recover_before_next_step: bool,
) -> None:
    """A no-room hold set up from a step whose followed bid is already through the stop it sets up
    sells by limit in that step, and a bid that recovers before the next step does not hide it (the
    combined landing's Codex second reader, P1; its drop-in case)."""
    host = cast(Any, Seam("0.20", trail_mode="atr", size_from_budget=False))
    slot = seam_slot(host, bought="10.00")
    host.slots = {slot.instrument_id: slot}
    slot.entry_received_ns = T0
    slot.closes = deque([Decimal("10.40")], maxlen=4)
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        trail=Decimal("0.02"),
        exit_through=Decimal("0.005"),
        chase_cap=Decimal("0.03"),
        atr=Decimal("0.10"),
        spread=Decimal("0.10"),
        bound="spread floor",
        floor=Decimal("0.02"),
    )

    def quote(at: int) -> SimpleNamespace:
        return SimpleNamespace(
            instrument_id=slot.instrument_id,
            bid_price=Price.from_str(host.bid),
            ask_price=Price.from_str(str(Decimal(host.bid) + Decimal("0.01"))),
            ts_event=at,
            ts_init=at,
        )

    host.cache.quote = lambda _instrument_id: quote(T0 + SECOND)
    # the quote is delivered before the step sets up its no-room hold
    host.on_quote(quote(T0 + SECOND))
    host._protect(slot, Decimal(5), T0 + SECOND)

    if recover_before_next_step:
        host.bid = "10.10"
        host.on_quote(quote(T0 + SECOND + SECOND // 2))
        host._protect(slot, Decimal(5), T0 + 2 * SECOND)

    assert [order.kind for order in host.sent] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == Decimal("9.80")
    assert host.sent[0].quantity.as_decimal() == Decimal(5)
    assert Decimal(0) < host.sent[0].price.as_decimal() <= Decimal("0.20")


def test_a_no_room_hold_keeps_no_print_low_with_the_holding_start_unknown() -> None:
    """Under a print trigger with no entry fill on record the holding's start cannot be read, so no
    print counts toward the hold's low (`on_trade`): the price a no-room hold is handed then is not
    kept as its low either, since one from before the entry would stay there once the fill is recorded
    (the delta's code review, finding 1)."""
    host = cast(Any, Seam("0.20", trigger=TriggerType.LAST_PRICE, size_from_budget=False))
    slot = seam_slot(host, bought="10.00")
    host.slots = {slot.instrument_id: slot}
    slot.entry_received_ns = T0
    slot.closes = deque([Decimal("10.40")], maxlen=4)
    assert host._holding_since(slot) is None

    # a print above the 9.80 stop the hold sets up: no sale, and nothing kept as the hold's low
    host._hold(slot, Decimal(5), T0 + SECOND, followed=Decimal("9.90"))

    assert host.sent == []
    assert slot.held_stop == Decimal("9.80")
    assert slot.held_low is None
