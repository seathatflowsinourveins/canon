"""Blind L5 properties at 1afe6c91, through the base's offline Mover seams.

Contract: the lead's P1-P4 dispatch of 2026-11-13. NautilusTrader d53849b216e2
defines Price.from_str/as_decimal and OrderFactory.trailing_stop_limit in
python/nautilus_trader/{model,common}/__init__.pyi; a sell's unseeded trailing
trigger follows bid minus its price offset (crates/execution/src/trailing.rs).
The existing test helpers supply the cache, synthetic venue and order observations.
No paper task, credential, catalog, ledger or real paper state is used.
"""

from collections import deque
from decimal import ROUND_CEILING
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import tape
from tests.test_mover_profile import held_up_to_10_92
from tests.test_mover_sizing import sent
from tests.test_mover_trail import sized_cold_holding

D = Decimal


@pytest.mark.parametrize("at_level", [False, True], ids=["below_level", "at_level"])
def test_p1_last_held_second_fill_sells_at_the_warmed_level(at_level: bool) -> None:
    """A fill after the last quote-less decision must reach the next decision's held level.

    The warmed plan is stepped up to cents at the later 11.00 average.
    A bid between L's 10.80 and that updated stop, including equality at the
    stop, must release a limit immediately, covering all fifteen shares.
    """
    seam, slot = sized_cold_holding("0.20", "0.15")
    seam._protect(slot, D(5), T0 + SECOND)
    assert slot.tightened is True
    assert D(0) < seam.sent[-1].trailing_offset < D("0.20")
    distance = (D(11) * slot.trail_plan.trail).quantize(D("0.01"), rounding=ROUND_CEILING)
    expected_level = D(11) - distance
    returning_bid = expected_level if at_level else expected_level - D("0.005")
    assert D(11) - slot.sized_loss < returning_bid <= expected_level
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str(seam.bid),
        ask_price=Price.from_str(str(D(seam.bid) + D("0.01"))),
    )
    seam.bid = "0"
    slot.closes = deque([D("11.20")], maxlen=4)
    seam.orders["E-1"].avg_px = 10.80
    held_from = T0 + 2 * SECOND
    seam._protect(slot, D(10), held_from)
    seam._protect(slot, D(10), held_from + 58 * SECOND)
    assert slot.held_stop is not None and slot.held_stop < returning_bid
    count = len(seam.sent)

    # Five more shares at 11.40 arrive in the last held second: the cache now
    # reports (10 * 10.80 + 5 * 11.40) / 15 = 11.00. No bid has returned yet.
    seam.orders["E-1"].avg_px = 11.00
    seam.fills.append(("E-1", "BUY", "5", "11.40", held_from + 59 * SECOND))
    assert seam.bid == "0"
    seam.bid = str(returning_bid)
    seam._protect(slot, D(15), held_from + 59 * SECOND + SECOND // 2)

    sales = seam.sent[count:]
    assert [order.kind for order in sales] == ["LIMIT"], sales
    assert sales[0].quantity.as_decimal() == D(15)
    assert sales[0].price.as_decimal() <= returning_bid
    assert slot.exiting is True
    assert slot.exit_trigger == expected_level
    assert D(seam.sell_triggers[sales[0].client_order_id.value]) == expected_level


@pytest.mark.parametrize("returning_bid", ["10.85", "11.20"])
def test_p2_venue_refused_remake_retains_the_held_level(returning_bid: str) -> None:
    """The venue refuses the 10.92 remake; both a breach and a recovery retain 10.92."""
    seam, slot = held_up_to_10_92()
    seam.bid = "11.20"
    remade_at = T0 + 3 * MINUTE + 2 * SECOND
    seam._protect(slot, D(15), remade_at)
    refused = seam.sent[-1]
    assert (refused.kind, refused.trigger_price.as_decimal()) == ("TRAILING", D("10.92"))
    seam.refuse(refused)
    refused.status = OrderStatus.REJECTED  # the venue, rather than a risk denial
    count = len(seam.sent)
    seam.bid = returning_bid
    seam._protect(slot, D(15), remade_at + seam.settings.chase_secs * SECOND)

    sales = seam.sent[count:]
    assert len(sales) == 1, sales
    assert sales[0].quantity.as_decimal() == D(15)
    if D(returning_bid) <= D("10.92"):
        assert sales[0].kind == "LIMIT", sales[0]
        assert slot.exiting is True
        assert slot.exit_trigger == D("10.92")
    else:
        assert sales[0].kind == "TRAILING", sales[0]
        assert sales[0].trigger_price.as_decimal() >= D("10.92")
    assert D(seam.sell_triggers[sales[0].client_order_id.value]) >= D("10.92")


@pytest.mark.parametrize("floor", ["0.20", "0.30"])
@pytest.mark.parametrize(
    "path",
    [
        "held_fill",
        "direct_fill",
        pytest.param(
            "direct_bid",
            marks=pytest.mark.xfail(
                strict=True,
                reason="judged: a followed bid of 0.20 is through the 10.00 entry's held stop, so the "
                "sale goes at once through that known price (the held stop's followed-price check, "
                "the lead's H1-H3), not by the unpriced exit kept for a missing price",
            ),
        ),
    ],
)
def test_p3_no_nonpositive_level_and_a_bounded_unpriced_exit(path: str, floor: str) -> None:
    """A frozen 10.00 plan meets a 0.20 fill or arming bid, at/eclipsed by its floor.

    Observe each step, not just the eventual exit: timing out an invalid held
    level sixty seconds later does not satisfy the no-nonpositive-level rule.
    """
    bid = "0" if path == "held_fill" else "0.20"
    seam = cast(Any, Seam(bid, trail_mode="atr"))
    slot = cast(Any, seam_slot(seam, bought="10.00" if path == "direct_bid" else "0.20"))
    distance = D(floor)
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        trail=distance / D(10),
        exit_through=distance / D(40),
        chase_cap=D("0.03"),
        atr=distance,
        spread=distance / D(2),
        bound="spread floor",
        floor=distance / D(10),
    )
    slot.entry_record = {"client_order_id": "E-1", "trail_changes": []}
    levels: list[tuple[int, str, Decimal]] = []
    count = 0
    for elapsed in (0, 59, 60):
        seam._protect(slot, D(5), T0 + elapsed * SECOND)
        for name in ("held_stop", "trail_trigger", "exit_trigger"):
            level = getattr(slot, name)
            if level is not None:
                levels.append((elapsed, name, level))
        for order in seam.sent[count:]:
            if order.trigger_price is not None:
                levels.append((elapsed, "sent trigger", order.trigger_price.as_decimal()))
            elif order.kind == "TRAILING":
                levels.append((elapsed, "bid minus offset", D(bid) - order.trailing_offset))
        count = len(seam.sent)

    assert all(level > 0 for _, _, level in levels), levels
    assert [order.kind for order in seam.sent] == ["LIMIT"], seam.sent
    assert seam.sent[0].quantity.as_decimal() == D(5)
    assert seam.sent[0].price.as_decimal() > 0
    assert slot.exiting is True
    assert slot.exit_record.trigger_reason == "unpriced_exit"
    assert slot.entry_record["protect_path"] == "unpriced_exit"
    assert seam.protect_paths["unpriced_exit"] == 1


@pytest.mark.parametrize("floor_fraction", ["1", "1.1"])
def test_p4_legacy_sizing_refuses_a_floor_at_or_above_entry(
    floor_fraction: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inject the frozen plan at the sizing boundary; the planner is outside P4.

    Keep the old fraction small so the legacy planned-stop check alone would
    admit it. The price floor, not just that fraction, must refuse the entry.
    """
    eng = engine()
    strategy = sent(
        size_from_budget=False,
        trail_mode="atr",
        max_notional=D(1000),
        flow_share=D(1),
        entry_ticks=0,
        protect=False,
        arming_path=str(tmp_path / "arming.json"),
    )
    attempts: list[tuple[Decimal, Decimal]] = []

    def frozen_plan(
        slot: mover.Slot, price: Decimal, quote: Any, *, now: int | None = None
    ) -> mover.TrailPlan:
        floor_at_entry = price * D(floor_fraction)
        attempts.append((price, floor_at_entry))
        return mover.TrailPlan(
            trail=D("0.02"),
            exit_through=D("0.005"),
            chase_cap=D("0.02"),
            atr=D("0.01"),
            spread=floor_at_entry / D(2),
            bound="spread floor",
            floor=D(floor_fraction),
        )

    monkeypatch.setattr(strategy, "_entry_trail", frozen_plan)
    try:
        orders = run(eng, strategy, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        record = strategy.record()
        assert attempts and all(floor >= price for price, floor in attempts)
        assert not [order for order in orders if order.side == OrderSide.BUY]
        assert record["entries"] == []
        assert any(
            "floor" in reason.lower() and ("siz" in reason.lower() or "room" in reason.lower())
            for reason in strategy.dropped
        ), strategy.dropped
    finally:
        eng.dispose()
