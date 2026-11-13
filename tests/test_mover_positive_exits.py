"""Positive sell levels at the Mover/emulator boundary (r-comb-ag P1-1 and P2-1).

NautilusTrader d53849b216e2 seeds missing levels from the last delivered market price
and only raises a sell's levels thereafter (execution/src/trailing.rs:61-100).
The native backtest and order factory are test_mover's; no network or file writes.
"""

from collections import deque
from decimal import Decimal
from itertools import permutations
from typing import Any
from typing import cast

import pytest
from nautilus_trader.common import Clock
from nautilus_trader.common import OrderFactory
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import at
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover import tape

D = Decimal


def print_at(price: str, event: int, received: int) -> TradeTick:
    return TradeTick(
        AAA,
        Price.from_str(price),
        Quantity.from_int(100),
        AggressorSide.NO_AGGRESSOR,
        TradeId(f"T-{received}"),
        T0 + event,
        T0 + received,
    )


def quote_at(bid: str, ask: str, received: int) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(bid),
        Price.from_str(ask),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        T0 + received,
        T0 + received,
    )


def assert_positive_sells(orders: list[Any]) -> None:
    """Check initialized, updated, released and final levels, not only the final order."""
    for order in orders:
        if order.side == OrderSide.SELL:
            for state in [order, *order.events()]:
                for field in ("price", "trigger_price"):
                    level = getattr(state, field, None)
                    assert level is None or level.as_decimal() > 0, str(state)


@pytest.mark.parametrize("late", ["0.2000", "0.4100"])
def test_a_late_print_in_the_cache_never_seeds_or_releases_a_nonpositive_limit(late: str) -> None:
    """r-comb-ag/P1-1: the reader's tape, including the zero-limit boundary."""
    eng = engine()
    s = strategy(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        size_from_budget=True,
        loss_share=mover.PAPER_LOSS_SHARE,
        entry_ticks=0,
        trigger=TriggerType.LAST_PRICE,
    )
    data = [
        *tape(AAA, RISING[:5], spread=0.08),
        print_at("10.5000", 301 * SECOND + 100_000_000, 301 * SECOND + 100_000_000),
        print_at(late, 300 * SECOND, 301 * SECOND + 200_000_000),
        quote_at("10.3200", "10.4800", 303 * SECOND),
    ]
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        assert any(o.side == OrderSide.BUY and o.filled_qty.as_decimal() > 0 for o in orders)
        assert_positive_sells(orders)
        assert not s.refused
        assert s.slots[AAA].held_since is not None
    finally:
        eng.dispose()


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
def test_a_holding_with_no_safe_emulated_sell_is_sold_by_plain_limit(
    trigger: TriggerType,
) -> None:
    """No room in the delivered market: the bid meets the retained stop at once;
    LAST_PRICE waits on its held stop, then sells at the bounded wind-down.

    Both exits fill every share in the native backtest, without a denied sale.
    """
    eng = engine()
    s = strategy(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        size_from_budget=True,
        loss_share=mover.PAPER_LOSS_SHARE,
        entry_ticks=0,
        trigger=trigger,
        extended_hours=True,
        wind_down_at=at(T0 + 305 * SECOND),
    )
    data = tape(AAA, RISING[:5], spread=0.08)
    data += [
        print_at("10.5000", 301 * SECOND + 100_000_000, 301 * SECOND + 100_000_000),
        quote_at("0.1900", "0.2000", 301 * SECOND + 200_000_000),
        print_at("0.2000", 300 * SECOND, 301 * SECOND + 300_000_000),
        quote_at("0.1900", "0.2000", 303 * SECOND),
        quote_at("0.1900", "0.2000", 307 * SECOND),
    ]
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        (buy,) = [order for order in orders if order.side == OrderSide.BUY]
        assert buy.filled_qty.as_decimal() > 0
        sells = [order for order in orders if order.side == OrderSide.SELL]
        assert sells
        assert sum(order.filled_qty.as_decimal() for order in sells) == buy.filled_qty.as_decimal()
        assert_positive_sells(orders)
        assert not s.refused
        assert not eng.cache.positions_open(instrument_id=AAA)
        if trigger == TriggerType.LAST_PRICE:
            assert sells[0].init_event.ts_init >= T0 + 305 * SECOND
        else:
            assert sells[0].init_event.ts_init < T0 + 303 * SECOND
    finally:
        eng.dispose()


@pytest.mark.parametrize(
    ("kept", "old_limit"), [("0.1000", "0.0900"), ("0.1700", "0.0900"), ("0.1800", "0.00005")]
)
@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
def test_a_remade_trail_never_submits_a_nonpositive_kept_limit(
    kept: str, old_limit: str, trigger: TriggerType
) -> None:
    """r-comb-ag/P2-1, with zero and a positive cap that rounds down to zero."""
    host = cast(
        Any,
        Seam(
            "9.00",
            trigger=trigger,
            trail_mode="atr",
            trail_cap="own",
            size_from_budget=True,
            loss_share=mover.PAPER_LOSS_SHARE,
        ),
    )
    host.order_factory = OrderFactory(
        TraderId("TESTER-001"), StrategyId("Mover-001"), Clock.new_test()
    )
    host.cache.trade = lambda _: print_at("9.0000", SECOND, SECOND)
    slot = seam_slot(host, bought="8.02")
    slot.sized_loss = D("8.00")
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.08"), D("0.02"), D("0.08"), None, D("0.01"), "atr", floor=D("0.0002")
    )
    slot.trail_trigger = D(kept)
    slot.trail_limit = D(old_limit)
    host._protect(slot, D(10), T0)
    (order,) = host.sent
    assert order.price is None or order.price.as_decimal() > 0, str(order)
    assert order.trigger_price is None or order.trigger_price.as_decimal() > 0


@pytest.mark.parametrize("direct", [False, True])
def test_a_new_no_room_hold_sells_the_bid_that_already_met_its_stop(direct: bool) -> None:
    """Limit 9: the hold reads its current followed price, even through a direct caller."""
    host = cast(Any, Seam("0.20"))
    slot = seam_slot(host)
    slot.closes = deque([D("10.40")], maxlen=4)
    if direct:
        host._hold(slot, D(3), T0)
    else:
        host._protect(slot, D(3), T0)
    assert [order.kind for order in host.sent] == ["LIMIT"]
    assert slot.exit_trigger == D("9.80")
    assert slot.exiting


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
@pytest.mark.parametrize("known_start", [True, False])
def test_a_no_room_holds_first_price_is_not_lost_when_a_fill_raises_the_stop(
    trigger: TriggerType, known_start: bool
) -> None:
    """Limit 9: 0.25 is above the first stop (0.20), then below the refreshed 0.30.

    The current price recovers to 0.45 between steps. A print cannot be kept as
    the hold's low without a known holding start; a bid always can.
    """
    host = cast(
        Any,
        Seam(
            "0.25",
            trigger=trigger,
            trail_mode="atr",
            trail_cap="own",
            size_from_budget=True,
            loss_share=mover.PAPER_LOSS_SHARE,
        ),
    )
    slot = seam_slot(host, bought="0.50")
    slot.sized_loss = D("0.30")
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.6"), D("0.15"), D("0.6"), None, D("0.15"), "spread floor", floor=D("0.6")
    )
    slot.closes = deque([D("0.50")], maxlen=4)
    host.slots, host.trades = {AAA: slot}, 0
    if known_start:
        host.fills = [("E-1", "BUY", "3", "0.50", T0)]
    prints = [print_at("0.2500", SECOND, SECOND)]
    host.cache.trade = lambda _: prints[-1]
    host._protect(slot, D(3), T0 + SECOND)
    assert host.sent == [] and slot.held_stop == D("0.20")

    host.orders["E-1"].avg_px = 0.60
    host.bid = "0.45"
    prints.append(print_at("0.4500", 2 * SECOND, 2 * SECOND))
    if trigger == TriggerType.LAST_PRICE:
        host.on_trade(prints[-1])
    else:
        host.on_quote(quote_at("0.4500", "0.4700", 2 * SECOND))
    host._protect(slot, D(5), T0 + 3 * SECOND)

    should_sell = known_start or trigger == TriggerType.BID_ASK
    assert [order.kind for order in host.sent] == ["LIMIT" if should_sell else "TRAILING"]
    assert slot.exiting is should_sell
    if should_sell:
        assert slot.exit_trigger == D("0.30")
        assert slot.exit_record is not None
        assert slot.exit_record.trigger_price == D("0.25")
        assert slot.exit_record.held_stop_kept == D("0.30")


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
def test_a_gap_between_updates_in_one_step_never_reseeds_an_unsized_sell_below_zero(
    trigger: TriggerType,
) -> None:
    """r-comb-dsh/P2: the first trailing sell has no supplied trigger or limit.

    Both drop and recovery arrive before the next timer; all native order events
    must retain positive levels, and the triggered sale must fill on recovery.
    """
    eng = engine()
    s = strategy(max_notional=D(1000), entry_ticks=0, trigger=trigger)
    data = [
        *tape(AAA, RISING[:5]),
        print_at("10.5000", 301 * SECOND + 100_000_000, 301 * SECOND + 100_000_000),
        quote_at("10.5000", "10.5200", 302 * SECOND + 100_000_000),
    ]
    if trigger == TriggerType.BID_ASK:
        data += [
            quote_at("0.0100", "0.0200", 302 * SECOND + 200_000_000),
            quote_at("10.5000", "10.5200", 302 * SECOND + 300_000_000),
        ]
    else:
        data += [
            print_at("0.0100", 302 * SECOND + 200_000_000, 302 * SECOND + 200_000_000),
            print_at("10.5000", 302 * SECOND + 300_000_000, 302 * SECOND + 300_000_000),
        ]
    data.append(quote_at("10.5000", "10.5200", 304 * SECOND))
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        sells = [order for order in orders if order.side == OrderSide.SELL]
        assert sells and any(order.filled_qty.as_decimal() > 0 for order in sells)
        assert not s.refused
        assert_positive_sells(orders)
        assert not eng.cache.positions_open(instrument_id=AAA)
    finally:
        eng.dispose()


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
@pytest.mark.parametrize("arrival", list(permutations(("new", "late", "quote"))))
def test_positive_sell_levels_survive_each_print_and_quote_arrival_order(
    trigger: TriggerType, arrival: tuple[str, ...]
) -> None:
    """All six arrival orders, fixed venue stamps: the cache need not be venue-newest."""
    eng = engine()
    s = strategy(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        size_from_budget=True,
        entry_ticks=0,
        trigger=trigger,
    )
    data = tape(AAA, RISING[:5], spread=0.08)
    for index, event in enumerate(arrival):
        received = 301 * SECOND + (index + 1) * 100_000_000
        if event == "new":
            data.append(print_at("10.5000", 301 * SECOND + 50_000_000, received))
        elif event == "late":
            data.append(print_at("0.2000", 300 * SECOND, received))
        else:
            data.append(quote_at("10.3200", "10.4800", received))
    data.append(quote_at("10.3200", "10.4800", 303 * SECOND))
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        assert any(o.side == OrderSide.BUY and o.filled_qty.as_decimal() > 0 for o in orders)
        assert_positive_sells(orders)
        assert not s.refused
        slot = s.slots[AAA]
        assert slot.held_stop is None or slot.held_stop > 0
        assert s._order(slot.protect_id) is not None or slot.held_since is not None
    finally:
        eng.dispose()


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
def test_the_readers_wide_floor_entry_is_refused_before_any_sell(
    trigger: TriggerType,
) -> None:
    """r-comb-dsh's proposed 0.90 floor at a 1.225 ask is outside the sizing ceiling.

    The existing unsized-gap test exercises the emulator without a kept trigger;
    this control checks the reader's concrete tape without relaxing entry limits.
    """
    eng = engine()
    s = strategy(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        size_from_budget=True,
        entry_ticks=0,
        trigger=trigger,
    )
    data = [
        *tape(AAA, [0.92, 0.94, 0.96, 0.98, 1.00], spread=0.225),
        quote_at("1.2000", "1.2250", 301 * SECOND + 100_000_000),
        print_at("1.2250", 301 * SECOND + 200_000_000, 301 * SECOND + 200_000_000),
    ]
    for offset, price in (
        (302 * SECOND + 100_000_000, "1.3000"),
        (302 * SECOND + 200_000_000, "0.8500"),
        (302 * SECOND + 300_000_000, "1.2000"),
        (304 * SECOND + 100_000_000, "0.1000"),
        (304 * SECOND + 200_000_000, "1.3000"),
    ):
        data.append(quote_at(price, str(D(price) + D("0.01")), offset))
        data.append(print_at(price, offset + 1, offset + 1))
    data.append(quote_at("1.3000", "1.3100", 306 * SECOND))
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        assert orders == []
        assert any("distance_over_ceiling" in reason for reason in s.dropped)
        assert not eng.cache.positions_open(instrument_id=AAA)
    finally:
        eng.dispose()


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
@pytest.mark.parametrize("sized", [True, False])
def test_a_return_above_a_held_stop_keeps_its_level_and_record_when_the_average_moves(
    trigger: TriggerType, sized: bool
) -> None:
    """Limit 7 controls: a refreshed stop not met, and an unsized stop kept as before."""
    host = cast(Any, Seam("0", trigger=trigger))
    slot = seam_slot(host)
    slot.closes = deque([D("10.40")], maxlen=4)
    slot.sized_loss = D("0.20") if sized else None
    host.fills = [("E-1", "BUY", "3", "10.00", T0)]
    host._protect(slot, D(3), T0 + SECOND)
    assert slot.held_stop == D("9.80")
    host.orders["E-1"].avg_px = 10.20
    host.bid = "10.40"
    host.cache.trade = lambda _: print_at("10.4000", 2 * SECOND, 2 * SECOND)
    host._protect(slot, D(5), T0 + 3 * SECOND)
    (sale,) = host.sent
    expected = D("10.00") if sized else D("9.80")
    assert sale.kind == "TRAILING" and sale.trigger_price.as_decimal() == expected
    assert D(host.sell_triggers[sale.client_order_id.value]) == expected
    assert slot.exit_record is not None
    assert slot.exit_record.held_stop_kept == expected
    assert slot.exit_record.held_stop_from_average == D("10.20") - expected
    assert not slot.exiting


@pytest.mark.parametrize("trigger", [TriggerType.BID_ASK, TriggerType.LAST_PRICE])
def test_a_holding_below_its_floor_never_arms_a_nonpositive_stop_and_still_sends_its_exit(
    trigger: TriggerType,
) -> None:
    """The partial-fill state the native venue cannot price-improve: 0.20 under a 0.30 floor.

    BID_ASK sends the bounded unpriced exit; LAST_PRICE with two quoted sides
    waits for the window's wind-down. Both send all held shares at a positive limit.
    """
    host = cast(
        Any,
        Seam(
            "0.20",
            trigger=trigger,
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
    slot.closes = deque([D("0.20")], maxlen=4)
    slot.newest_print = mover.Print(T0 + SECOND, D("1.00"), T0 + SECOND)
    host.cache.quote = lambda _: quote_at("0.2000", "0.2100", SECOND)
    host.cache.trade = lambda _: print_at("0.2000", -SECOND, SECOND + 1)
    host.fills = [("E-1", "BUY", "5", "0.20", T0)]
    host._protect(slot, D(5), T0 + SECOND)
    assert host.sent == [] and slot.held_stop is None and slot.held_since == T0
    host._protect(slot, D(5), T0 + 61 * SECOND)
    if trigger == TriggerType.LAST_PRICE:
        assert host.sent == [] and not slot.exiting
        host._sell_out(slot, D(5), T0 + 62 * SECOND)
    else:
        assert slot.exit_record is not None
        assert slot.exit_record.trigger_reason == "unpriced_exit"
    (sale,) = host.sent
    assert sale.quantity.as_decimal() == D(5)
    assert sale.order_type.name == "LIMIT" and sale.price.as_decimal() > 0
    assert_positive_sells(host.sent)
