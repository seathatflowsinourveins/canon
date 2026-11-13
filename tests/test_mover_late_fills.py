"""A fill that arrives after its order's cancel is still the Mover's (the Antigravity reader of #351,
P1-1, P1-2 and P2-1). A canceled order may still fill: the pinned model lets Canceled and Expired go to
Filled (NautilusTrader d53849b216e2, crates/model/src/orders/mod.rs:247, :295). A late buy leaves its name
in a slot with its stop placed, its exit recorded and the wind-down selling it; a late sell records its
sale with its price, so no buy goes out under it; one arriving after the holding is flat counts nothing
twice. The seams and engines are test_mover's and test_mover_trail's."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import strategy
from tests.test_mover_trail import handling
from tests.test_mover_trail import sale_event
from tests.test_mover_trail import sized_cold_holding

D = Decimal


def late_buy(*, winding_down: bool) -> Any:
    """A Mover whose entry E-1 was canceled and its slot closed, and whose late fill bought 5 at 10."""
    eng = engine()
    s = strategy()
    eng.add_strategy(s)
    for name in ("subscribe_quotes", "subscribe_trades", "subscribe_bars"):
        setattr(s, name, cast(Any, lambda *args, **kwargs: None))  # no engine run, so no feed
    s.entries_sent.append({"client_order_id": "E-1", "entry_price": None})
    s.winding_down = winding_down
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=ClientOrderId("E-1"),
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.0000"),
            ts_event=T0 + 32 * SECOND,
            ts_init=T0 + 32 * SECOND,
        )
    )
    s._held = lambda slot: D(5) if slot.instrument_id == AAA else D(0)
    s._ranked = lambda now: {}
    return s


def test_a_late_buy_fill_after_its_slot_closed_reopens_protection_and_winds_down() -> None:
    """w1-a-351, P1-1: an entry canceled and its slot closed; its late fill leaves shares held. The
    name is in a slot again with its exit recorded, and the wind-down's market exit sells it."""
    s = late_buy(winding_down=True)
    exited: list[bool] = []
    s.market_exit = lambda: exited.append(True)
    s._watch(T0 + 33 * SECOND)
    s._end()
    assert AAA in s.slots
    assert len(s.exits) == 1
    assert exited == [True]


def test_a_late_buy_fill_outside_the_wind_down_has_its_stop_placed_at_the_next_step() -> None:
    """P1-1, the same late fill while the run goes on: the step that opens the slot again protects
    the 5 held, as any holding's step does, with the entry's own record."""
    s = late_buy(winding_down=False)
    protected: list[Decimal] = []
    s._protect = lambda _slot, held, _now: protected.append(held)
    s._watch(T0 + 33 * SECOND)
    slot = s.slots[AAA]
    s._manage(slot, T0 + 33 * SECOND)
    assert protected == [D(5)]
    assert slot.entry_record == {"client_order_id": "E-1", "entry_price": "10.0000"}
    assert slot.exit_record is not None and slot.exit_record.quantity == D(5)
    assert any("came after its slot closed" in reason for reason in s.dropped)


def test_a_late_sale_fill_in_the_default_clock_arm_keeps_the_sale_price_and_refuses_lower_reentry() -> (
    None
):
    """w1-a-351, P1-2: a late fill of a canceled sale sells the holding under the default clock arm;
    the sale's price is recorded, so a run that closes under it is not bought."""
    seam, slot = sized_cold_holding("0.20", "0.15")  # default reentry_arm == "clock"
    host = handling(seam, slot)
    host.sold, host.sold_fill_ns, host.reentry_states = {}, {}, {}
    (sale,) = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(sale, T0 + SECOND))
    slot.exit_record = None
    current = seam._ensure_exit(slot, D(5))
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 2 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    assert (current.filled, current.notional) == (D(5), D("51.5"))
    slot.protect_id, slot.was_held = None, True
    host._working = lambda _slot: False
    mover.Mover._flat(host, slot, T0 + 3 * SECOND)
    assert host.sold[AAA] == (T0 + 3 * SECOND, D("10.3"))
    s = strategy()
    s.rank_ns = T0 + 304 * SECOND
    s.rank = [(AAA, D("0.25"))]
    s.sold = dict(host.sold)
    slot.run_ns, slot.run_close, slot.run_seen_ns = (
        T0 + 304 * SECOND,
        D("9.50"),
        T0 + 304 * SECOND,
    )
    assert s._refusal(slot, D("9.50"), s._ranked(s.rank_ns), 0, s.rank_ns) == (
        "its run closed at 9.50, not above the sale at 10.3000"
    )


def test_a_late_fill_of_a_canceled_chase_leg_does_not_double_count_a_completed_exit_record() -> (
    None
):
    """w1-a-351, P2-1 (the reader's drop-in): the chase's second sale sold all 5 and the holding is
    flat; the canceled first sale's late fill of 5 adds nothing to the holding's record, which keeps
    its 5, its last fill time and its dollars. The order's own row and the fills keep the full fill."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (first,) = seam.sent
    row = slot.exit_record
    row.peak = D("10.50")
    row.trigger_ns, row.trigger_price, row.trigger_reason = T0, D("10.30"), "trailing stop"
    mover.Mover.on_order_canceled(host, sale_event(first, T0 + 5 * SECOND))
    seam._limit_sell(slot, D(5), T0 + 5 * SECOND, None)
    second = seam.sent[-1]
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.2000")}
    mover.Mover.on_order_filled(
        host, sale_event(second, T0 + 6 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    slot.exit_record = None  # _flat cleared the completed 5-share holding
    mover.Mover.on_order_filled(
        host, sale_event(first, T0 + 8 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    record = row.record()
    assert record["filled_quantity"] == "5"
    assert record["final_fill_ns"] == T0 + 6 * SECOND
    assert (D(record["slip_dollars"]), D(record["peak_giveback_dollars"])) == (
        D("0.50"),
        D("1.50"),
    )
    assert host.sale_rows[first.client_order_id.value]["filled_quantity"] == "5"


def test_a_late_buy_for_a_name_ranked_again_in_the_same_step_keeps_its_entry() -> None:
    """The landing review of this unit, P2: the step after the late fill ranks the name again, so a
    slot is opened for it as a ranked name; that slot is still the late entry's, with its order and
    record, which the persistence arm's protection reads (`_persistent_trail`)."""
    s = late_buy(winding_down=False)
    s._ranked = lambda now: {AAA: (0, D("0.25"))}
    s._watch(T0 + 33 * SECOND)
    slot = s.slots[AAA]
    assert slot.entry_id is not None and slot.entry_id.value == "E-1"
    assert slot.entry_record == {"client_order_id": "E-1", "entry_price": "10.0000"}


def test_reader_late_buy_after_ranking_reopened_slot_arms_persistence() -> None:
    from nautilus_trader.model import TriggerType

    eng = engine()
    s = strategy(breach_count=2, trigger=TriggerType.LAST_PRICE)
    eng.add_strategy(s)
    s.subscribe_quotes = lambda *args, **kwargs: None
    s.subscribe_trades = lambda *args, **kwargs: None
    s.subscribe_bars = lambda *args, **kwargs: None
    s.entries_sent.append({"client_order_id": "E-1", "entry_price": None})
    s._ranked = lambda now: {AAA: (0, D("0.25"))}
    s._held = lambda slot: D(0)
    s._watch(T0 + 31 * SECOND)
    slot = s.slots[AAA]
    assert slot.entry_id is None

    entry_id = ClientOrderId("E-1")
    entry = SimpleNamespace(client_order_id=entry_id, avg_px=10.0, is_closed=True)
    s._order = lambda client_order_id: entry if client_order_id == entry_id else None
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=entry_id,
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.0000"),
            ts_event=T0 + 32 * SECOND,
            ts_init=T0 + 32 * SECOND,
        )
    )
    s._held = lambda slot: D(5)
    s._watch(T0 + 33 * SECOND)
    s._manage(s.slots[AAA], T0 + 33 * SECOND)

    assert s.slots[AAA].trail_trigger is not None
    assert s.slots[AAA].entry_id == entry_id


def test_reader_reopened_late_buy_keeps_the_loss_distance_used_for_sizing() -> None:
    eng = engine()
    s = strategy(trail_mode="atr", size_from_budget=True, loss_share=D("0.02"))
    eng.add_strategy(s)
    s.subscribe_quotes = lambda *args, **kwargs: None
    s.subscribe_trades = lambda *args, **kwargs: None
    s.subscribe_bars = lambda *args, **kwargs: None
    s._request_history = lambda slot: None
    s.entries_sent.append(
        {
            "client_order_id": "E-1",
            "entry_price": None,
            "trail": "0.005",
            "exit_through": "0.00125",
            "chase_cap": "0.02",
            "trail_source": "atr",
            "trail_bound": "atr",
            "sizing": {"loss_per_share": "0.05"},
        }
    )
    entry_id = ClientOrderId("E-1")
    entry = SimpleNamespace(client_order_id=entry_id, avg_px=10.0, is_closed=True)
    s._order = lambda client_order_id: entry if client_order_id == entry_id else None
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=entry_id,
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.0000"),
            ts_event=T0 + 32 * SECOND,
            ts_init=T0 + 32 * SECOND,
        )
    )
    s._held = lambda slot: D(5)
    s._ranked = lambda now: {}
    s._watch(T0 + 33 * SECOND)
    slot = s.slots[AAA]
    s._manage(slot, T0 + 33 * SECOND)

    assert (slot.sized_loss, slot.held_stop) == (D("0.05"), D("9.95"))


def test_reader_clock_uses_all_sale_legs_when_a_late_leg_flattens() -> None:
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    host.sold, host.sold_fill_ns, host.reentry_states = {}, {}, {}
    (first,) = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(first, T0 + SECOND))
    seam._limit_sell(slot, D(5), T0 + SECOND, None)
    second = seam.sent[-1]
    mover.Mover.on_order_filled(
        host,
        sale_event(
            second,
            T0 + 2 * SECOND,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(1),
            last_px=Price.from_str("10.0000"),
        ),
    )
    mover.Mover.on_order_canceled(host, sale_event(second, T0 + 3 * SECOND))
    second.is_closed, second.is_open, second.avg_px = True, False, 10.0
    mover.Mover.on_order_filled(
        host,
        sale_event(
            first,
            T0 + 4 * SECOND,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(4),
            last_px=Price.from_str("12.0000"),
        ),
    )
    assert (slot.exit_record.filled, slot.exit_record.notional) == (D(5), D(58))
    slot.was_held = True
    host._working = lambda slot: False
    mover.Mover._flat(host, slot, T0 + 5 * SECOND)

    s = strategy()
    now = T0 + 306 * SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    s.sold = dict(host.sold)
    slot.run_ns, slot.run_close, slot.run_seen_ns = now, D("11.0"), now
    why = s._refusal(slot, D("11.0"), s._ranked(now), 0, now)
    assert (host.sold[AAA][1], why) == (
        D("11.6"),
        "its run closed at 11.0, not above the sale at 11.6000",
    )


def test_reader_late_round_trip_before_next_step_keeps_its_new_sale_price() -> None:
    # The drop-in uses this file's late_buy helper: its BUY has arrived, with no step yet.
    s = late_buy(winding_down=False)
    s.unsubscribe_bars = lambda *args, **kwargs: None
    s.unsubscribe_trades = lambda *args, **kwargs: None
    s.unsubscribe_quotes = lambda *args, **kwargs: None
    earlier = mover.ExitRecord(AAA, "E-old", D(5), filled=D(5), notional=D(40), final_fill_ns=T0)
    s.exits.append(earlier)
    s.sale_owner["S-old"] = earlier
    s.sale_rows["S-old"] = {
        "quantity": "5",
        "filled_quantity": "0",
        "ended": "canceled",
        "ended_ns": T0,
    }
    s.sold[AAA] = (T0, D(8))
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=ClientOrderId("S-old"),
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("12.0000"),
            ts_event=T0 + 32 * SECOND + SECOND // 2,
            ts_init=T0 + 32 * SECOND + SECOND // 2,
        )
    )
    s._held = lambda slot: D(0)
    s.rank_ns = T0 + 33 * SECOND
    s._watch(T0 + 33 * SECOND)

    assert s.sold[AAA][1] == D(12)
    assert any(row.entry_client_order_id == "E-1" and row.filled == D(5) for row in s.exits)


def test_late_buy_with_a_new_unfilled_entry_keeps_it_cancelable_and_arms_persistence() -> None:
    from nautilus_trader.model import TriggerType

    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    host = LateFillSeam(breach_count=2, trigger=TriggerType.LAST_PRICE)
    _, old = entry(host)
    host.cancel_order(old.client_order_id)
    host.rank_ns = T0
    host.step(T0 + SECOND)
    assert AAA not in host.slots
    host._open_slot(AAA)
    slot = host.slots[AAA]
    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-new")
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders[newer.client_order_id.value] = newer
    slot.entry_id = newer.client_order_id
    slot.entry_record = {"client_order_id": "E-new", "entry_price": None}
    host.entries_sent.append(slot.entry_record)

    host.fill(old, 5, "10.0000", T0 + 3 * SECOND)
    host.step(T0 + 4 * SECOND)

    assert slot.trail_trigger == D("9.80")
    assert slot.exit_record.entry_client_order_id == old.client_order_id.value
    assert slot.entry_id == newer.client_order_id
    host._wind_down(SimpleNamespace(ts_event=T0 + 5 * SECOND))
    host.step(T0 + 6 * SECOND)
    assert newer.client_order_id.value in host.canceled


def test_unsized_late_buy_restores_its_entry_trail_plan() -> None:
    s = late_buy(winding_down=False)
    s.entries_sent[0].update(
        trail="0.005", exit_through="0.00125", chase_cap="0.01", trail_bound="atr"
    )
    entry_id = ClientOrderId("E-1")
    entry = SimpleNamespace(client_order_id=entry_id, avg_px=10.0, is_closed=True)
    s._order = lambda cid: entry if cid == entry_id else None
    s._watch(T0 + 33 * SECOND)
    slot = s.slots[AAA]
    s._manage(slot, T0 + 33 * SECOND)

    assert slot.held_stop == D("9.95")
    assert slot.trail_plan == slot.initial_trail
    assert slot.trail_plan is not None
    assert (slot.trail_plan.exit_through, slot.trail_plan.chase_cap) == (
        D("0.00125"),
        D("0.01"),
    )


def test_late_buy_partly_sold_before_recovery_keeps_both_fill_totals() -> None:
    s = late_buy(winding_down=False)
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=ClientOrderId("S-old"),
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(2),
            last_px=Price.from_str("12.0000"),
            ts_event=T0 + 32 * SECOND + SECOND // 2,
        )
    )
    s._held = lambda slot: D(3)
    s._watch(T0 + 33 * SECOND)
    row = s.slots[AAA].exit_record
    assert row is not None
    assert (row.quantity, row.bought_notional, row.filled, row.notional) == (
        D(5),
        D(50),
        D(2),
        D(24),
    )


def test_a_newer_entry_filled_before_the_step_keeps_its_own_plan_and_sizing() -> None:
    """A late E-1 buy and the newer E-2's own fill both arrive before the step. The slot keeps E-2,
    its record and the loss its entry was sized for; the late buy's row is attached, not its plan
    (code-review-l5-late-fills-fix/P2). An order's average is unset until it fills (NautilusTrader
    d53849b216e2, crates/model/src/orders/limit.rs:414)."""
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    host = LateFillSeam()
    _, old = entry(host)
    host.cancel_order(old.client_order_id)
    host.rank_ns = T0
    host.step(T0 + SECOND)
    assert AAA not in host.slots
    host._open_slot(AAA)
    slot = host.slots[AAA]
    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-new")
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders[newer.client_order_id.value] = newer
    slot.entry_id = newer.client_order_id
    slot.sized_loss = D("0.07")
    newer_record = {
        "client_order_id": "E-new",
        "entry_price": None,
        "sizing": {"loss_per_share": "0.07"},
    }
    slot.entry_record = newer_record
    host.entries_sent.append(newer_record)

    host.fill(old, 5, "10.0000", T0 + 3 * SECOND)
    host.fill(newer, 5, "10.4000", T0 + 3 * SECOND + 1)
    host.step(T0 + 4 * SECOND)

    assert slot.entry_id == newer.client_order_id
    assert slot.entry_record is newer_record
    assert slot.sized_loss == D("0.07")
    assert any(row.entry_client_order_id == old.client_order_id.value for row in host.exits)


def test_wind_down_late_buy_without_positive_bid_still_sends_a_sale() -> None:
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    host = LateFillSeam()
    _, buy = entry(host)
    host.cancel_order(buy.client_order_id)
    host.rank, host.rank_ns = [], T0
    host._wind_down(SimpleNamespace(ts_event=T0 + SECOND))
    host.step(T0 + SECOND)
    assert AAA not in host.slots

    host.bid = "0.0000"
    host.fill(buy, 5, "10.0000", T0 + 2 * SECOND)
    host.step(T0 + 3 * SECOND)

    slot = host.slots.get(AAA)
    protected = (
        slot is not None
        and slot.exit_record is not None
        and (
            slot.held_stop is not None
            or any(o.is_emulated and o.quantity.as_decimal() >= 5 for o in host.submitted)
        )
    )
    selling = any(o.is_open and o.quantity.as_decimal() >= 5 for o in host.submitted)
    assert protected or selling, "five late-bought shares have neither protection nor a sale"


def test_a_newer_entry_filled_after_late_buy_recovery_keeps_its_own_plan_and_sizing() -> None:
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    host = LateFillSeam()
    _, old = entry(host)
    host.entries_sent[-1].update(
        trail="0.02",
        exit_through="0.005",
        chase_cap="0.02",
        trail_bound="fixed",
        sizing={"loss_per_share": "0.20"},
    )
    host.cancel_order(old.client_order_id)
    host.rank_ns = T0
    host.step(T0 + SECOND)
    assert AAA not in host.slots

    host._open_slot(AAA)
    slot = host.slots[AAA]
    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-new")
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders[newer.client_order_id.value] = newer
    slot.entry_id = newer.client_order_id
    slot.sized_loss = D("0.07")
    newer_plan = mover.TrailPlan(
        trail=D("0.007"),
        exit_through=D("0.002"),
        chase_cap=D("0.02"),
        atr=D("0.14"),
        spread=D("0.01"),
        bound="atr",
        source="atr",
    )
    slot.trail_plan = slot.initial_trail = newer_plan
    newer_record = {
        "client_order_id": "E-new",
        "entry_price": None,
        "trail": "0.007",
        "exit_through": "0.002",
        "chase_cap": "0.02",
        "atr": "0.14",
        "spread": "0.01",
        "trail_bound": "atr",
        "trail_source": "atr",
        "sizing": {"loss_per_share": "0.07"},
    }
    slot.entry_record = newer_record
    host.entries_sent.append(newer_record)

    host.fill(old, 5, "10.0000", T0 + 3 * SECOND)
    host.step(T0 + 4 * SECOND)
    host.fill(newer, 5, "10.4000", T0 + 5 * SECOND)
    # the position's average over both fills, as the pinned NautilusTrader keeps it
    # (`crates/model/src/position.rs:1062-1063`: cost open over the quantity open); the blind
    # driver fixes it at 10
    host.positions = [SimpleNamespace(signed_qty=D(10), avg_px_open=D("10.20"))]
    host.step(T0 + 6 * SECOND)

    assert slot.entry_id == newer.client_order_id
    assert slot.entry_record is newer_record
    assert slot.sized_loss == D("0.07")
    assert slot.trail_plan == newer_plan
    # one holding, one stop: L under the whole holding's average, 10.20 less 0.07 (the lead,
    # 2026-11-13, heldnext (a)), not under the newer entry's own 10.40
    assert host.submitted[-1].trigger_price == Price.from_str("10.1300")


def test_a_late_buy_reported_after_a_newer_entry_was_stopped_sells_through_the_crossing() -> None:
    """The other order of the same shape: the newer entry fills and is stopped L under its own
    10.40 first, then the canceled entry's late fill at 10.00 is reported. The stop is not lowered;
    the holding's average 10.20 is under it, so the crossing rule sells at once. That sale is at a
    gain over the average, the limit the lead named at 11:39."""
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    host = LateFillSeam()
    _, old = entry(host)
    host.cancel_order(old.client_order_id)
    host.rank_ns = T0
    host.step(T0 + SECOND)
    assert AAA not in host.slots

    host._open_slot(AAA)
    slot = host.slots[AAA]
    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-new")
    newer.filled_qty, newer.avg_px = Quantity.from_int(0), None
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders[newer.client_order_id.value] = newer
    slot.entry_id = newer.client_order_id
    slot.sized_loss = D("0.07")
    slot.entry_record = {"client_order_id": "E-new", "sizing": {"loss_per_share": "0.07"}}
    host.entries_sent.append(slot.entry_record)

    host.fill(newer, 5, "10.4000", T0 + 3 * SECOND)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.40"))]
    host.step(T0 + 4 * SECOND)
    stops = [o for o in host.submitted if o.side == OrderSide.SELL]
    assert stops, "the newer entry's holding has its stop"
    assert stops[-1].trigger_price == Price.from_str("10.3300")
    before = len(host.submitted)

    host.fill(old, 5, "10.0000", T0 + 5 * SECOND)
    # (`crates/model/src/position.rs:1062-1063`) the average over both fills
    host.positions = [SimpleNamespace(signed_qty=D(10), avg_px_open=D("10.20"))]
    host.step(T0 + 6 * SECOND)

    sent = [o for o in host.submitted[before:] if o.side == OrderSide.SELL]
    # the crossing's sale is a plain limit for the whole holding, sent in this step; a remade
    # trailing sell, at any trigger, would be the stop kept rather than the crossing's sale
    assert slot.exiting, "the average 10.20 under the kept 10.33 sells at once"
    assert slot.exit_trigger == D("10.33")
    assert [(o.kind, o.quantity) for o in sent] == [("LIMIT", Quantity.from_int(10))]


def test_the_clock_arm_holds_reentry_above_the_sale_on_the_sales_trade_date_only() -> None:
    """The lead's ruling (2026-11-13): every sale now has a price, so the default clock arm's
    above-sale rule is scoped to the sale's trade date, as the opening arm's `SOLD_THIS_TRADE_DATE`
    is (the 20:00 rollover included). A Mover that runs a replay's whole sample then buys a later
    session's run under an earlier session's sale, as a window's own Mover, which lives inside one
    session, always could."""
    slot = sized_cold_holding("0.20", "0.15")[1]
    s = strategy()
    day = 86_400 * SECOND
    hour = 3_600 * SECOND
    # T0 is 11:00 New York; (the sale's fill, its mark, the step that reads it)
    cases = [
        (T0, T0 + SECOND, T0 + 304 * SECOND, True),  # the same session
        (T0, T0 + SECOND, T0 + day, False),  # the next session
        (
            T0 + 8 * hour + 50 * 60 * SECOND,
            T0 + 8 * hour + 3_000 * SECOND,
            T0 + 8 * hour + 59 * 60 * SECOND,
            True,
        ),  # 19:50, 19:59
        (
            T0 + 8 * hour + 50 * 60 * SECOND,
            T0 + 8 * hour + 3_000 * SECOND,
            T0 + 9 * hour + 5 * 60 * SECOND,
            False,
        ),  # 19:50, 20:05
        (  # filled 19:59, marked 20:01, read 20:07: the sale is dated by its fill
            T0 + 8 * hour + 59 * 60 * SECOND,
            T0 + 9 * hour + 60 * SECOND,
            T0 + 9 * hour + 7 * 60 * SECOND,
            False,
        ),
    ]
    for filled, marked, now, refused in cases:
        s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
        s.sold, s.sold_fill_ns = {}, {AAA: filled}
        s.sold[AAA] = (marked, D("10.3"))
        slot.run_ns, slot.run_close, slot.run_seen_ns = now, D("9.50"), now
        why = s._refusal(slot, D("9.50"), s._ranked(now), 0, now)
        assert (why == "its run closed at 9.50, not above the sale at 10.3") is refused, (
            filled,
            now,
            why,
        )
        if not refused:
            assert why is None, (filled, now, why)


def test_late_buy_after_completed_exit_before_flat_step_does_not_inherit_closed_exit_or_floor() -> (
    None
):
    """P1-1: Under main's defaults (OWN1: trail_mode='atr', trail_cap='own', size_from_budget=True,
    reentry_arm='clock', chase='resend', TriggerType.BID_ASK), E-1 (5 @ 10.00, L=0.20) times out
    and is canceled; E-2 later buys 5 @ 10.20 (L=0.20), trails up to trigger 10.80 / limit 10.74,
    releases, and fills all 5 @ 10.79 between steps. Before the next 1s timer step runs _flat,
    canceled E-1 fills late (+5 @ 10.00). Neither an immediate liquidation above E-1's 9.80 stop
    nor E-2's stale 10.53 chase floor clamping a gapped-down stop exit above the market may carry
    into the new holding."""
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry
    from tests.test_mover_late_fill_blind import event

    def scenario(next_bid: str) -> tuple[LateFillSeam, Any, list[Any]]:
        host = LateFillSeam(
            trail_mode="atr",
            trail_cap="own",
            size_from_budget=True,
            loss_share=D("0.02"),
        )
        host.cache.bars = lambda _: None
        vars(host)["_request_history"] = lambda _: None  # the seam has no history request
        _, old = entry(host)
        host.entries_sent[-1].update(
            trail="0.02",
            exit_through="0.005",
            chase_cap="0.02",
            trail_bound="fixed",
            trail_source="cold_atr_fallback",
            sizing={"loss_per_share": "0.20"},
        )
        host.cancel_order(old.client_order_id)
        host.rank_ns = T0
        host.step(T0 + SECOND)
        assert AAA not in host.slots

        host._open_slot(AAA)
        slot = host.slots[AAA]
        newer = SimpleNamespace(**vars(old))
        newer.client_order_id = ClientOrderId("E-2")
        newer.price = Price.from_str("10.2000")
        newer.filled_qty, newer.avg_px = Quantity.from_int(0), None
        newer.is_closed, newer.is_open = False, True
        newer.ts_init = T0 + 2 * SECOND
        host.orders[newer.client_order_id.value] = newer
        slot.entry_id = newer.client_order_id
        slot.sized_loss = D("0.20")
        newer_plan = mover.TrailPlan(
            trail=D("0.02"),
            exit_through=D("0.005"),
            chase_cap=D("0.02"),
            atr=None,
            spread=D("0.01"),
            bound="fixed",
            source="cold_atr_fallback",
        )
        slot.trail_plan = slot.initial_trail = newer_plan
        slot.entry_record = {
            "client_order_id": "E-2",
            "instrument_id": str(AAA),
            "entry_price": None,
            "stopped_out": False,
            "trail": "0.02",
            "exit_through": "0.005",
            "chase_cap": "0.02",
            "trail_bound": "fixed",
            "trail_source": "cold_atr_fallback",
            "sizing": {"loss_per_share": "0.20"},
        }
        host.entries_sent.append(slot.entry_record)

        host.bid = "10.25"
        host.fill(newer, 5, "10.2000", T0 + 3 * SECOND)
        host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.20"))]
        host.step(T0 + 4 * SECOND)
        sale = host._order(slot.protect_id)
        assert sale is not None and sale.kind == "TRAILING"

        sale.trigger_price = Price.from_str("10.8000")
        sale.price = Price.from_str("10.7400")
        sale.is_emulated, sale.is_open, sale.has_trigger_price = False, True, False
        sale.events = lambda: [SimpleNamespace(trigger_price=Price.from_str("10.8000"))]
        host.on_order_released(
            event(sale.client_order_id, T0 + 4 * SECOND + 100_000_000, released_price=sale.price)
        )
        host.fill(sale, 5, "10.7900", T0 + 4 * SECOND + 200_000_000)
        assert host.positions == []

        before = len(host.submitted)
        host.fill(old, 5, "10.0000", T0 + 4 * SECOND + 500_000_000)
        host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.00"))]
        host.bid = next_bid
        host.step(T0 + 5 * SECOND)
        sent = [o for o in host.submitted[before:] if o.side == OrderSide.SELL]
        return host, slot, sent

    # Wrong order: unsolicited immediate liquidation while $0.99 above E-1's 9.80 stop.
    host_up, slot_up, sent_up = scenario("10.79")
    assert not slot_up.exiting, "new 10.00 holding at bid 10.79 must arm trailing stop, not exit"
    assert [o.kind for o in sent_up] == ["TRAILING"]
    assert AAA in host_up.sold and host_up.sold[AAA][1] == D("10.79")

    # Wrong order + missed sale: bid gaps down to 9.70 (below E-1's 9.80 stop), and E-2's stale
    # 10.53 chase floor clamps the stop exit $0.83 above the market.
    _, _, sent_gap = scenario("9.70")
    assert len(sent_gap) == 1 and sent_gap[0].kind == "LIMIT"
    assert sent_gap[0].price.as_decimal() <= D("9.70"), (
        f"stop exit at bid 9.70 was clamped above the market to {sent_gap[0].price}"
    )
