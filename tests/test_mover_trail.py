"""T1/T2: synthetic minute bars and quotes, using rc8's ATR and real order emulator.

The existing test_mover fixtures provide an offline cash venue; no credential or network is read.
"""

from collections import deque
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import mover
from canon_trading.price_step import down_on_step
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover import tape

D = Decimal


def quote(spread="0.005"):
    return SimpleNamespace(
        bid_price=Price.from_str(str(D(10) - D(spread))), ask_price=Price.from_str("10.0000")
    )


def warmed(mode="atr", *, width="0.04", spread="0.005", baseline=None, **rows):
    s = strategy(trail_mode=mode, atr_n=3, atr_k=D(1), **rows)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(s.settings.atr_n)
    s.slots[AAA] = slot
    if baseline is not None:
        s.baseline_dollar_volume_per_minute[AAA] = D(baseline)
    for i in range(3):
        bar = Bar(
            bar_type(AAA),
            Price.from_str("10.0000"),
            Price.from_str(str(D(10) + D(width) / 2)),
            Price.from_str(str(D(10) - D(width) / 2)),
            Price.from_str("10.0000"),
            Quantity.from_int(1000),
            T0 + i * MINUTE,
            T0 + (i + 1) * MINUTE,
        )
        s.on_bar(bar)
    return s, slot, quote(spread)


@pytest.mark.parametrize(
    ("mode", "width", "spread", "baseline", "fraction", "bound"),
    [
        ("fixed", "0.04", "0.005", None, "0.02", "fixed"),
        ("atr", "0.04", "0.005", None, "0.004", "atr"),
        ("atr", "0.001", "0.02", None, "0.004", "spread floor"),
        ("atr", "0.5", "0.005", None, "0.02", "0.02 cap"),
        ("tier", "0.04", "0.005", "1000000", "0.002", "tier"),
        ("tier", "0.04", "0.04", "1000000", "0.008", "spread floor"),
        ("tier", "0.04", "0.005", "999999", "0.004", "atr"),
        ("ratchet", "0.04", "0.005", None, "0.004", "atr"),
    ],
)
def test_trail_arms_name_the_bound(mode, width, spread, baseline, fraction, bound):
    s, slot, latest = warmed(mode, width=width, spread=spread, baseline=baseline)
    plan = s._entry_trail(slot, D(10), latest)
    assert plan.trail == pytest.approx(D(fraction))
    assert plan.bound == bound
    assert plan.spread == D(spread)
    if mode != "fixed":
        assert plan.atr == pytest.approx(D(width))
        assert plan.exit_through == plan.trail / 4
        assert plan.chase_cap == max(plan.trail, s.settings.chase_cap)


@pytest.mark.parametrize("bid", [None, "0", "10", "10.01"])
@pytest.mark.parametrize("mode", ["atr", "tier", "ratchet"])
def test_unusable_entry_spread_selects_and_names_the_fixed_fallback(bid, mode):
    s, slot, _ = warmed(mode)
    latest = SimpleNamespace(ask_price=Price.from_str("10.0000"))
    if bid is not None:
        latest.bid_price = Price.from_str(bid)
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound, plan.spread) == (D("0.02"), "fixed", None)
    assert "spread" in plan.note and "fixed" in plan.note


def test_no_quote_still_refuses_entry_in_every_arm():
    for mode in ("fixed", "atr", "tier", "ratchet"):
        eng = engine()
        s = strategy(trail_mode=mode, atr_n=3)
        data = [item for item in tape(AAA, RISING) if isinstance(item, Bar)]
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        assert orders == [] and s.record()["entries"] == []
        assert any("no quote yet" in why for why in s.dropped)
        eng.dispose()


def test_locked_entry_quote_records_the_fixed_fallback():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0))
    entry = s.record()["entries"][0]
    assert entry["trail"] == "0.02" and entry["trail_bound"] == "fixed"
    assert entry["spread"] is None
    assert any("spread" in note and "fixed" in note for note in entry["notes"])
    eng.dispose()


def test_zero_bid_refusal_keeps_the_fixed_fallback_in_the_records_notes():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3)
    data = []
    for item in tape(AAA, RISING):
        if isinstance(item, QuoteTick):
            item = QuoteTick(
                AAA,
                Price.from_str("0.0000"),
                item.ask_price,
                item.bid_size,
                item.ask_size,
                item.ts_event,
                item.ts_init,
            )
        data.append(item)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    assert orders == []  # the existing planned-stop bid guard still holds
    notes = " ".join(s.record()["notes"].get(str(AAA), []))
    assert "fixed trail fallback" in notes
    eng.dispose()


def test_a_cold_atr_enters_on_the_fixed_plan_by_name_and_a_spread_floor_over_the_cap_refuses():
    """Coordinator's (ii), 2026-11-12: a cold ATR no longer delays entry by `atr_n` live minutes."""
    s, slot, latest = warmed()
    slot.atr.reset()
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound) == (s.settings.trail, "fixed")
    assert "ATR" in plan.note and "3" in plan.note and "fixed" in plan.note
    assert plan.source == "cold_atr_fallback"
    s, slot, latest = warmed(spread="0.11")
    with pytest.raises(ValueError, match="spread floor.*0.02 cap"):
        s._entry_trail(slot, D(10), latest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trail_mode", "guess"),
        ("atr_k", D(0)),
        ("atr_n", 0),
        ("spread_floor_m", D(0)),
        ("liquidity_tier_cut", D(0)),
        ("ratchet_r", D(0)),
        ("ratchet_g", D(0)),
        ("ratchet_g", D(1)),
        ("ratchet_base", "fixed"),
        ("spread_floor_m", D("0.5")),
    ],
)
def test_trail_settings_cannot_disable_the_bounds(field, value):
    with pytest.raises(ValueError, match=field):
        strategy(**{field: value})


def test_size_stand_in_reads_the_rankings_per_minute_normal():
    record = {"inputs": {"dvol": 12000000, "dvol_ratio": 2}, "sample": {"elapsed_minutes": 3}}
    assert mover.baseline_per_minute(record) == D(2000000)
    assert mover.baseline_per_minute({"inputs": record["inputs"], "elapsed_minutes": 3}) == D(
        2000000
    )
    assert mover.baseline_per_minute({}) is None


@pytest.mark.parametrize(
    ("gain", "spread", "expected", "bound"),
    [
        ("0.0799", "0.005", None, None),
        ("0.08", "0.005", "0.002", "ratchet"),
        ("0.08", "0.015", "0.003", "spread floor"),
        ("0.08", "0.03", None, None),
    ],
)
def test_ratchet_threshold_and_spread_floor(gain, spread, expected, bound):
    s, slot, latest = warmed("ratchet")
    initial = s._entry_trail(slot, D(10), latest)
    tightened = mover.ratchet_trail(initial, D(10), D(10) + D(gain), D(spread), s.settings)
    if expected is None:
        assert tightened is None
    else:
        assert tightened is not None
        assert tightened.trail == pytest.approx(D(expected))
        assert tightened.bound == bound
        assert tightened.exit_through == tightened.trail / 4
        assert tightened.chase_cap == max(tightened.trail, s.settings.chase_cap)


@pytest.mark.parametrize(
    ("baseline", "bound", "trail"),
    [
        ("1000000", "tier", "0.002"),
        ("999999", "atr", "0.004"),
    ],
)
def test_ratchet_can_start_from_the_tier_plan(baseline, bound, trail):
    s, slot, latest = warmed("ratchet", baseline=baseline, ratchet_base="tier")
    plan = s._entry_trail(slot, D(10), latest)
    assert plan.bound == bound and plan.trail == pytest.approx(D(trail))


def test_each_name_uses_its_own_bar_atr_and_spread():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3, atr_k=D(1), slots=2, watch=2)
    run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.20"))),
        tape(AAA, RISING),
        tape(BBB, [20 + 0.01 * i for i in range(8)], spread=0.001),
    )
    records = {r["instrument_id"]: r for r in s.record()["entries"]}
    assert D(records[str(AAA)]["trail"]) > D(records[str(BBB)]["trail"])
    assert D(records[str(AAA)]["atr"]) > D(records[str(BBB)]["atr"])
    assert records[str(AAA)]["spread"] != records[str(BBB)]["spread"]
    eng.dispose()


def test_ratchet_over_tier_is_paired_with_the_plain_tier_entry():
    entries = []
    for mode in ("tier", "ratchet"):
        eng = engine()
        s = strategy(trail_mode=mode, ratchet_base="tier", atr_n=3, ratchet_r=D(2))
        s.baseline_dollar_volume_per_minute[AAA] = D(1_000_000)
        run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0.001))
        entries.append(s.record()["entries"][0])
        eng.dispose()
    assert entries[0]["trail"] == entries[1]["trail"] == "0.002"
    assert entries[0]["ratchet"]["tightened"] is False
    assert entries[1]["ratchet"]["tightened"] is True
    assert D(entries[1]["ratchet"]["to"]) == D("0.001")


def test_atr_entries_record_the_frozen_offsets_and_paper_limit():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3, atr_k=D("0.5"))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    entry = s.record()["entries"][0]
    protection = next(o for o in orders if o.side == OrderSide.SELL)
    fraction = D(entry["trail"])
    assert entry["trail_bound"] == "atr" and D(entry["atr"]) > 0
    assert entry["spread"] == "0.0200"
    assert fraction < D("0.02")
    trailing, limit = mover.offsets(D(str(orders[0].avg_px)), fraction, fraction / 4)
    assert protection.trailing_offset == trailing and protection.limit_offset == limit
    notes = " ".join(entry["notes"])
    assert "quoted size" in notes and "impact" in notes and "UNCALIBRATED" in notes
    assert "lead, no primary source" in notes and "ATR multiple" in notes and "spread" in notes
    eng.dispose()


@pytest.mark.parametrize("average", ["10.00", None, "0", "NaN", "Infinity"])
def test_missing_entry_price_protects_from_cache_average_then_bid_and_counts_each_use(average):
    seam = Seam("10.50")
    slot = seam_slot(seam, bought=None)
    if average is not None:
        seam.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D(average))]
    source = "position average price" if average == "10.00" else "current bid"
    anchor = D("10.00") if average == "10.00" else D("10.50")
    seam._protect(slot, D(5), T0)
    assert len(seam.sent) == 1 and seam.sent[0].is_emulated
    assert seam.sent[0].trailing_offset == mover.offsets(anchor, D("0.02"), D("0.005"))[0]
    seam.refuse(seam.sent[0])
    seam._protect(slot, D(5), T0 + 5 * SECOND)
    assert len(seam.sent) == 2
    assert seam.protection_fallbacks[source] == 2
    assert sum(seam.protection_fallbacks.values()) == 2


def test_a_fill_with_no_known_entry_still_has_a_working_sale(monkeypatch):
    eng = engine()
    s = strategy()
    trail = s._trail

    def lose_entry(slot, held, now):
        slot.entry_id = None
        trail(slot, held, now)

    monkeypatch.setattr(s, "_trail", lose_entry)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    buy = next(o for o in orders if o.side == OrderSide.BUY)
    sales = [o for o in orders if o.side == OrderSide.SELL]
    assert buy.filled_qty.is_positive()
    assert len(sales) == 1 and sales[0].quantity == buy.filled_qty
    assert s.record()["protection_fallbacks"] == {"position average price": 1, "current bid": 0}
    eng.dispose()


def test_missing_trade_for_a_held_position_holds_a_stop_instead_of_selling_at_once():
    """The lead's (b): no followed price makes a held stop from the cache average, not a sale."""
    seam = Seam("10.50", trigger=mover.TriggerType.LAST_PRICE)
    seam.cache.trade = lambda _: None
    seam.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.00"))]
    slot = seam_slot(seam, bought=None)
    seam._protect(slot, D(5), T0)
    assert seam.sent == [] and not slot.exiting
    assert slot.held_stop == D("10.00") - mover.offsets(D("10.00"), D("0.02"), D("0.005"))[0]


def test_missing_quote_for_a_held_position_sells_from_its_cache_average_after_60_s():
    seam = Seam("10.50")
    seam.cache.quote = lambda _: None
    seam.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.00"))]
    slot = seam_slot(seam, bought=None)
    seam._protect(slot, D(5), T0)
    seam._protect(slot, D(5), T0 + 59 * SECOND)
    assert seam.sent == []
    seam._protect(slot, D(5), T0 + 60 * SECOND)
    assert len(seam.sent) == 1 and seam.sent[0].is_open
    assert seam.sent[0].price == Price.from_str("9.9900")
    seam.refuse(seam.sent[0])
    seam._protect(slot, D(5), T0 + 65 * SECOND)
    assert len(seam.sent) == 2 and seam.sent[1].is_open


def test_cached_position_anchor_is_weighted_and_excludes_other_strategies():
    seam = Seam("10.50")
    requests = []

    def positions(**kwargs):
        requests.append(kwargs)
        return [
            SimpleNamespace(signed_qty=D(2), avg_px_open=D(10)),
            SimpleNamespace(signed_qty=D(3), avg_px_open=D(11)),
        ]

    seam.cache.positions_open = positions
    slot = seam_slot(seam, bought=None)
    seam._protect(slot, D(5), T0)
    assert seam.sent[0].trailing_offset == mover.offsets(D("10.6"), D("0.02"), D("0.005"))[0]
    assert requests == [{"instrument_id": AAA, "strategy_id": seam.strategy_id}]


@pytest.mark.parametrize(
    ("baseline", "bound", "fraction"), [(1000000, "tier", "0.002"), (999999, "atr", "0.004")]
)
def test_default_tier_uses_the_owners_tight_fraction_or_the_smallest_atr_multiple(
    baseline, bound, fraction
):
    s, slot, latest = warmed("tier", baseline=baseline)
    s.settings.atr_k = mover.MoverConfig(max_notional=D(100), max_move=D(5)).atr_k
    plan = s._entry_trail(slot, D(10), latest)
    assert plan.bound == bound and plan.trail == pytest.approx(D(fraction))
    assert plan.exit_through == plan.trail / 4
    assert plan.chase_cap == max(plan.trail, s.settings.chase_cap)


def test_window_record_notes_state_both_uncalibrated_limits_even_without_an_entry():
    notes = " ".join(strategy().record()["trail_notes"])
    assert "quoted size" in notes and "impact" in notes
    assert "tight stops look better on paper than live" in notes
    assert "no outside source" in notes and "ATR multiple" in notes and "spread" in notes
    assert "lead, no primary source" in notes and "UNCALIBRATED" in notes


def test_exit_record_uses_release_event_and_all_partial_fills_across_reprices():
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    slot.entry_record = {"client_order_id": "BUY-1", "stopped_out": False}
    s._ensure_exit(slot, D(5))
    slot.protect_id = ClientOrderId("SELL-1")
    s.on_quote(SimpleNamespace(instrument_id=AAA, bid_price=Price.from_str("11.0000")))
    s.on_order_released(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=slot.protect_id,
            released_price=Price.from_str("10.0000"),
            ts_event=T0,
        )
    )
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=slot.protect_id,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(2),
            last_px=Price.from_str("9.9000"),
            ts_event=T0 + SECOND,
        )
    )
    partial = s.record()["exits"][0]
    assert partial["final_fill_ns"] is None and partial["trigger_to_final_fill_ms"] is None
    # A later chase order completes this same exit; the original trigger must survive it.
    slot.protect_id = ClientOrderId("SELL-2")
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=slot.protect_id,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(3),
            last_px=Price.from_str("9.8000"),
            ts_event=T0 + 7 * SECOND,
        )
    )
    (row,) = s.record()["exits"]
    assert row["trigger_ns"] == T0 and D(row["trigger_price"]) == D(10)
    assert row["final_fill_ns"] == T0 + 7 * SECOND and row["trigger_to_final_fill_ms"] == 7000
    assert D(row["fill_vwap"]) == D("9.84")
    assert D(row["slip_fraction"]) == D("0.016") and D(row["slip_dollars"]) == D("0.80")
    assert D(row["peak_giveback_dollars"]) == D("5.80")
    assert slot.entry_record["stopped_out"] is True


def test_exit_timing_is_recorded_when_the_emulator_releases_and_fills_between_steps():
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.46, 10.0, 9.9]))
    (row,) = s.record()["exits"]
    fills = [f for f in s.fills if f[1] == "SELL"]
    assert row["trigger_ns"] == T0 + 9 * MINUTE - 1
    assert row["final_fill_ns"] == max(f[4] for f in fills)
    assert row["trigger_to_final_fill_ms"] == 0
    assert D(row["trigger_price"]) == D("10.45")
    quantity = sum((D(f[2]) for f in fills), D(0))
    vwap = sum((D(f[2]) * D(f[3]) for f in fills), D(0)) / quantity
    assert D(row["fill_vwap"]) == vwap
    assert D(row["peak_price"]) == D("10.69")
    assert D(row["peak_giveback_dollars"]) == (D("10.69") - vwap) * quantity
    assert s.record()["entries"][0]["stopped_out"] is True
    eng.dispose()


def test_exit_record_retains_a_zero_trigger_observation_without_dividing_by_zero():
    row = mover.ExitRecord(
        AAA, "BUY-1", D(1), filled=D(1), notional=D(10), trigger_ns=T0, trigger_price=D(0)
    )
    record = row.record()
    assert record["trigger_price"] == "0" and record["slip_fraction"] is None


def test_chase_uses_the_entry_plans_scaled_cap():
    s, warmed_slot, latest = warmed()
    plan = s._entry_trail(warmed_slot, D(10), latest)
    seam = Seam("9.90")
    slot = seam_slot(seam)
    slot.trail_plan, slot.exiting = plan, True
    sale = seam._limit(AAA, OrderSide.SELL, Quantity.from_int(1), Price.from_str("10.00"))
    seam.orders[sale.client_order_id.value] = sale
    slot.protect_id = sale.client_order_id
    seam._protect(slot, D(1), 100 * SECOND)
    assert slot.floor is not None
    assert plan.trail < seam.settings.chase_cap
    # Coordinator's (ii), 2026-11-12: a narrow trail keeps the configured chase depth, so a gap
    # through it is still chased to a fill rather than resting above the market.
    assert plan.chase_cap == seam.settings.chase_cap
    assert slot.floor == mover.chase_floor(D(10), seam.settings.chase_cap)


@pytest.mark.parametrize("sized_loss", ["0.02", "0.01"], ids=["equal-under-l", "wider-than-l"])
def test_a_ratchet_is_not_spent_unless_it_tightens_the_offsets_l_put_in_force(sized_loss: str):
    """The landing review of the fix on S2, finding 5: an entry sized for a loss L trails L under the
    fill until a ratchet, so the offsets in force are L's, not the initial plan's. A candidate of 0.002
    (offsets 0.02 and 0.03) tightens the plan's 0.04 and 0.05, but equals L = 0.02's offsets and is
    wider than L = 0.01's (0.01 and 0.02); neither tightens the sale, so the ratchet is kept."""
    seam, slot = ratchet_seam("10.0800", "10.0850")
    slot.sized_loss = D(sized_loss)
    assert mover.Mover._ratchet(seam, slot, T0) is False
    assert slot.ratcheted is False and "ratchet" not in (slot.entry_record or {})
    slot.sized_loss = D("0.03")
    assert mover.Mover._ratchet(seam, slot, T0 + SECOND) is True
    assert slot.ratcheted is True


def test_ratchet_remakes_the_emulated_sale_and_records_when_and_from_what():
    eng = engine()
    s = strategy(trail_mode="ratchet", atr_n=3, atr_k=D(1), ratchet_r=D(2), ratchet_g=D("0.5"))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.8, 10.9]))
    record = s.record()["entries"][0]
    ratchet = record["ratchet"]
    sells = [o for o in orders if o.side == OrderSide.SELL]
    assert ratchet["tightened"] is True
    assert ratchet["when_ns"] > record["submitted_ns"]
    assert D(ratchet["to"]) == D(ratchet["from"]) / 2
    assert len(sells) == 2 and sells[0].status.name == "CANCELED"
    assert sells[1].trailing_offset < sells[0].trailing_offset
    assert all(a <= b for a, b in zip(s.triggers, s.triggers[1:]))
    # the real emulator takes the remade sale with the limit the canceled one had reached, and
    # never moves it lower (Astra second read of #316, finding 1)
    assert sells[1].price is not None and sells[1].price >= sells[0].price
    eng.dispose()


def test_flow_alternate_and_shipped_atr_default_are_registered_without_changing_defaults():
    rows = mover.TRAIL_SETTINGS
    assert rows["flow_share"]["default"] == D("0.05")
    assert D("0.01") in rows["flow_share"]["alternates"]
    assert "Lean" in rows["flow_share"]["source"]
    notes = " ".join(strategy().record()["trail_notes"])
    assert "backtesting.py" in notes and "6 ATR" in notes and "bar frequency" in notes
    assert "note" in notes and "row" in notes
    assert mover.MoverConfig(max_notional=D(100), max_move=D(5)).atr_k == D(1)


# --- fix round: the lead's held stop (b) and the high review's findings ----------------------------


def held(bid="0", **rows):
    """A Seam whose entry filled at 10.00 and whose quote is one-sided (bid 0)."""
    seam = Seam(bid, **rows)
    seam.cache.trade = lambda _: None
    slot = seam_slot(seam)
    slot.entry_record = {"client_order_id": "E-1", "stopped_out": False}
    return seam, slot


def test_one_sided_quote_at_fill_makes_no_sale_and_leaves_a_working_held_stop():
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    distance = mover.offsets(D("10.00"), D("0.02"), D("0.005"))[0]
    assert seam.sent == [] and not slot.exiting
    assert slot.held_stop == D("10.00") - distance
    assert slot.entry_record["protect_path"] == "held_stop"


def test_a_print_through_the_held_stop_sells_at_once_and_names_its_source():
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("9.7900"))
    seam._protect(slot, D(5), T0 + 5 * SECOND)
    assert len(seam.sent) == 1 and seam.sent[0].price == Price.from_str("9.7800")
    assert slot.exiting and slot.exit_record.trigger_reason == "held stop (last trade)"
    assert slot.entry_record["protect_path"] == "held_stop"
    assert slot.entry_record["held_stop_secs"] == 5
    assert slot.entry_record["held_stop_trigger"] == "last trade"


def test_with_no_trade_feed_the_held_stop_is_checked_on_the_minute_close():
    seam, slot = held()
    slot.closes = deque([D("9.90")], maxlen=4)
    seam._protect(slot, D(5), T0)
    assert seam.sent == []
    slot.closes.append(D("9.70"))
    seam._protect(slot, D(5), T0 + MINUTE)
    assert len(seam.sent) == 1 and slot.exit_record.trigger_reason == "held stop (minute close)"


def test_no_two_sided_quote_within_60_s_sells_by_limit_and_counts_an_unpriced_exit():
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    seam._protect(slot, D(5), T0 + 59 * SECOND)
    assert seam.sent == []
    seam._protect(slot, D(5), T0 + 60 * SECOND)
    assert len(seam.sent) == 1 and seam.sent[0].is_open
    assert slot.exit_record.trigger_reason == "unpriced_exit"
    assert slot.entry_record["protect_path"] == "unpriced_exit"
    assert slot.entry_record["held_stop_secs"] == 60
    assert seam.protect_paths["unpriced_exit"] == 1


def test_a_two_sided_quote_releases_the_held_stop_into_the_normal_trail():
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    seam.bid = "10.10"
    seam._protect(slot, D(5), T0 + 10 * SECOND)
    assert len(seam.sent) == 1 and seam.sent[0].kind == "TRAILING"
    assert slot.held_stop is None
    assert slot.entry_record["protect_path"] == "trail"
    assert slot.entry_record["held_stop_secs"] == 10


def test_a_trail_with_no_price_at_all_is_noted_and_retried_not_raised():
    """High review: an anchorless trail raised from the step timer and stopped every slot."""
    seam = Seam("0", trigger=mover.TriggerType.LAST_PRICE)
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("10.0000"))
    slot = seam_slot(seam, bought=None)
    seam._protect(slot, D(5), T0)
    assert seam.sent == []
    assert any("no entry, position average price" in n for n in seam.dropped)


def test_ratchet_tightens_from_the_protection_anchor_when_the_entry_price_is_unknown():
    seam = Seam("10.50", trail_mode="ratchet")
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    slot = seam_slot(seam, bought=None)
    slot.protection_anchor = D("10.00")
    slot.initial_trail = mover.TrailPlan(D("0.02"), D("0.005"), D("0.02"), None, None, "atr", None)
    assert mover.Mover._ratchet(cast(Any, seam), slot, T0) is True
    assert slot.trail_plan is not None and slot.trail_plan.trail < D("0.02")


class RatchetSeam(Seam):
    """A Seam whose protection may ratchet, and whose cancel of an emulated sale is seen at once."""

    _ratchet = mover.Mover._ratchet

    def cancel_order(self, client_order_id: Any) -> None:
        super().cancel_order(client_order_id)
        self.orders[client_order_id.value].is_closed = True


def ratchet_seam(bid: str, ask: str) -> tuple[Any, mover.Slot]:
    """Entry filled at 10.00 on an ATR plan of 0.004 (offsets 0.04 and 0.05), with the quote given."""
    seam = RatchetSeam(bid, trail_mode="ratchet")
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str(bid), ask_price=Price.from_str(ask)
    )
    slot = seam_slot(seam)
    plan = mover.TrailPlan(D("0.004"), D("0.001"), D("0.02"), None, None, "atr", None)
    slot.initial_trail = slot.trail_plan = plan
    slot.entry_record = {"client_order_id": "E-1", "stopped_out": False}
    return seam, slot


def test_a_ratchet_remake_after_a_pullback_keeps_the_release_limit_the_sale_had_reached():
    """Astra second read of #316, finding 1: the bid reached 10.14 and pulled back to 10.11 between
    steps, so the working sale holds trigger 10.10 and limit 10.09. The ratchet remakes it with offsets
    0.02 and 0.03. The emulator seeds a trailing limit from the order's own price
    (`crates/execution/src/trailing.rs:61-65` at the pin) and moves it only to a better one, so a remake
    with no price would release at 10.11 - 0.03 = 10.08, under the 10.09 already reached."""
    seam, slot = ratchet_seam("10.1100", "10.1200")
    working = seam._new(
        "TRAILING",
        quantity=Quantity.from_int(5),
        trigger_price=Price.from_str("10.1000"),
        price=Price.from_str("10.0900"),
        trailing_offset=D("0.04"),
        limit_offset=D("0.05"),
    )
    seam.orders[working.client_order_id.value] = working
    slot.protect_id = working.client_order_id
    seam._protect(slot, D(5), T0)
    assert seam.canceled == [working.client_order_id.value]
    remade = seam.sent[-1]
    assert (remade.kind, remade.trailing_offset, remade.limit_offset) == (
        "TRAILING",
        D("0.02"),
        D("0.03"),
    )
    assert remade.trigger_price == Price.from_str("10.1000")
    assert remade.price == Price.from_str("10.0900")


def test_a_kept_release_limit_is_never_above_the_kept_trigger_on_alpacas_step():
    """The landing review of the fix (high), finding 1: a sale bought under $1 has a sub-cent trail, so
    its reached limit can sit within a cent of its trigger. Once the price is over $1 the remade
    trigger is rounded down to Alpaca's step (1.0599 to 1.05); the kept limit 1.0536 would sit above
    it and release a sale over the bid. The kept limit is capped at the kept trigger."""
    seam = Seam("1.0560")
    slot = seam_slot(seam, bought="0.9000")
    slot.trail_trigger, slot.trail_limit = D("1.0599"), D("1.0536")
    seam._protect(slot, D(5), T0)
    remade = seam.sent[-1]
    assert remade.kind == "TRAILING" and remade.trigger_price == Price.from_str("1.0500")
    assert remade.price is not None and remade.price <= remade.trigger_price


def test_a_kept_release_limit_is_on_alpacas_step():
    """The landing review of the cap, finding 1: the kept trigger is on whole cents over $1, but a
    sale bought under $1 has a sub-cent trailing offset, so the trigger less the spread between the
    offsets (1.05 - (0.03 - 0.0180) = 1.0380) is off Alpaca's step. The adapter floors a sell limit,
    so it would release at 1.03 unseen. The kept limit is rounded down to the step where it is made."""
    seam = Seam("1.0560")
    slot = seam_slot(seam, bought="0.9000")
    slot.trail_trigger, slot.trail_limit = D("1.0599"), D("1.0536")
    seam._protect(slot, D(5), T0)
    remade = seam.sent[-1]
    assert remade.price is not None
    price = remade.price.as_decimal()
    assert price == down_on_step(price) == D("1.03")


def test_a_ratchet_whose_rounded_offsets_are_unchanged_is_not_spent():
    """Astra second read of #316, finding 2: a gain of 0.08 over a 0.019 spread gives a candidate trail
    of 0.0038, under 0.004, but both round to offsets 0.04 and 0.05. Nothing tightens, so the one
    ratchet is not spent or recorded, and a later narrower spread still tightens the sale."""
    seam, slot = ratchet_seam("10.0800", "10.0990")
    assert mover.offsets(D(10), D("0.0038"), D("0.00095")) == mover.offsets(
        D(10), D("0.004"), D("0.001")
    )
    assert mover.Mover._ratchet(seam, slot, T0) is False
    assert slot.ratcheted is False and "ratchet" not in (slot.entry_record or {})
    assert slot.trail_plan is not None and slot.trail_plan.trail == D("0.004")
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.0800"), ask_price=Price.from_str("10.0850")
    )
    assert mover.Mover._ratchet(seam, slot, T0 + SECOND) is True
    assert slot.ratcheted is True
    assert slot.trail_plan is not None and slot.trail_plan.trail == D("0.002")


def test_unusable_entry_spread_falls_back_to_the_configured_fixed_plan():
    s, slot, _ = warmed("atr", trail=D("0.03"), exit_through=D("0.01"), chase_cap=D("0.03"))
    plan = s._entry_trail(slot, D(10), SimpleNamespace(ask_price=Price.from_str("10.0000")))
    assert (plan.trail, plan.exit_through, plan.chase_cap) == (D("0.03"), D("0.01"), D("0.03"))
    assert plan.bound == "fixed"


def test_trail_rows_reach_the_mover_through_its_validated_constructor():
    from canon_trading import mover_window
    from tests.test_mover import at

    def parts(rows, bar_spec=mover_window.LIVE_BARS):
        return mover_window.mover_universe_parts(
            {AAA: D(20)},
            extended=False,
            feed="sip",
            wind_down_at=at(T0 + 20 * MINUTE),
            market_exit_interval_ms=1000,
            market_exit_max_attempts=60,
            slots=1,
            bar_spec=bar_spec,
            trail_rows=rows,
        )

    s, _ = parts({"trail_mode": "atr", "atr_k": D(3)})
    assert (s.settings.trail_mode, s.settings.atr_k) == ("atr", D(3))
    with pytest.raises(ValueError, match="1-MINUTE-LAST"):
        parts({"trail_mode": "atr"}, "1-MINUTE-BID-EXTERNAL")


def test_a_replay_expected_to_enter_does_enter_under_the_default_rows():
    """A step-timer error is swallowed by the engine; a replay that should enter must show its entry."""
    eng = engine()
    s = strategy(trail_mode="tier", atr_n=3)
    s.baseline_dollar_volume_per_minute[AAA] = D(2_000_000)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].filled_qty.is_positive()
    assert len(s.record()["entries"]) == 1
    eng.dispose()


def test_exit_record_states_the_slip_beyond_the_trail_level_in_percent():
    """The lead, 2026-11-12: per exit, the fill against the trigger level, in percent."""
    row = mover.ExitRecord(
        AAA, "BUY-1", D(10), filled=D(10), notional=D(98), trigger_ns=T0, trigger_price=D(10)
    )
    assert D(row.record()["slip_beyond_trail_pct"]) == D(2)
    row.trigger_price = None
    assert row.record()["slip_beyond_trail_pct"] is None


def test_a_cold_atr_entry_records_its_trail_source_and_a_warm_one_its_bound():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=30)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    [entry] = s.record()["entries"]
    assert entry["trail_source"] == "cold_atr_fallback" and entry["trail"] == "0.02"
    eng.dispose()
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    [entry] = s.record()["entries"]
    assert entry["trail_source"] == entry["trail_bound"] == "atr"
    eng.dispose()


# --- the landing review of P0a (/code-review high) -------------------------------------------------


def cold(mode, **rows):
    """A slot whose ATR has no complete minute yet."""
    s = strategy(trail_mode=mode, atr_n=3, **rows)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    return s, slot


def test_a_cold_atr_tier_entry_keeps_the_unknown_baseline_note():
    s, slot = cold("tier")
    plan = s._entry_trail(slot, D(10), quote())
    assert (plan.bound, plan.source) == ("fixed", "cold_atr_fallback")
    assert plan.note is not None and "fewer than 3" in plan.note
    assert "baseline dollar volume per minute is unknown" in plan.note


def test_the_ratchet_leaves_a_fixed_fallback_plan_alone():
    s, slot = cold("ratchet", exit_through=D("0.01"))
    initial = s._entry_trail(slot, D(10), quote())
    assert initial.bound == "fixed"
    assert mover.ratchet_trail(initial, D(10), D(11), D("0.005"), s.settings) is None
    s, slot, _ = warmed("ratchet", exit_through=D("0.01"))
    unpriced = s._entry_trail(slot, D(10), SimpleNamespace(ask_price=Price.from_str("10.0000")))
    assert unpriced.bound == "fixed"
    assert mover.ratchet_trail(unpriced, D(10), D(11), D("0.005"), s.settings) is None


def test_the_startup_baseline_reads_a_record_whose_sample_or_inputs_are_null():
    record = {"inputs": {"dvol": 600, "dvol_ratio": 2}, "sample": None, "elapsed_minutes": 3}
    assert mover.baseline_per_minute(record) == D(100)
    assert mover.baseline_per_minute({"inputs": None, "sample": None}) is None


def test_an_exit_still_unfilled_at_the_wind_down_records_that_it_waited_and_for_how_long():
    """Coordinator, 2026-11-12: a gap deeper than the chase floor waits for the wind-down; say so."""
    s = strategy()
    waiting, untriggered, sold = (
        mover.ExitRecord(AAA, "BUY-1", D(10), trigger_ns=T0, trigger_reason="trailing stop"),
        mover.ExitRecord(BBB, "BUY-2", D(10)),
        mover.ExitRecord(AAA, "BUY-3", D(10), filled=D(10), trigger_ns=T0, final_fill_ns=T0 + 1),
    )
    for instrument, row in ((AAA, waiting), (BBB, untriggered)):
        slot = mover.Slot(instrument, bar_type(instrument), deque(maxlen=4), deque(maxlen=5))
        slot.exit_record = row
        s.slots[instrument] = slot
    s.exits.extend([waiting, untriggered, sold])
    s._wind_down(cast(Any, SimpleNamespace(ts_event=T0 + 5 * MINUTE)))
    assert s.winding_down
    record = waiting.record()
    assert record["waited_for_wind_down"] is True and record["wind_down_wait_minutes"] == 5
    for row in (untriggered, sold):
        assert row.record()["waited_for_wind_down"] is False
        assert row.record()["wind_down_wait_minutes"] is None


def test_a_sale_resting_at_its_floor_for_60_s_drops_the_floor_and_follows_the_bid():
    """The lead's (b), 2026-11-12: a gap deeper than the chase floor no longer waits for the wind-down."""
    seam = Seam("9.50")
    slot = seam_slot(seam)
    slot.exiting, slot.floor = True, D("9.80")
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(5), trigger_ns=T0, trigger_price=D("9.95"))
    sale = seam._limit(AAA, OrderSide.SELL, Quantity.from_int(5), Price.from_str("9.80"))
    seam.orders[sale.client_order_id.value] = sale
    slot.protect_id = sale.client_order_id
    seam._protect(slot, D(5), T0 + 100 * SECOND)
    seam._protect(slot, D(5), T0 + 159 * SECOND)
    assert seam.canceled == []
    seam._protect(slot, D(5), T0 + 160 * SECOND)
    assert seam.canceled == [sale.client_order_id.value]
    sale.is_closed, sale.is_open = True, False
    seam._protect(slot, D(5), T0 + 161 * SECOND)
    assert seam.sent[-1].price == Price.from_str("9.4900") and slot.floor is None
    record = slot.exit_record.record()
    assert record["floor_released"] is True and record["floor_wait_secs"] == 60


def test_a_chased_sale_with_no_bid_is_remade_from_the_last_price_never_back_up_at_the_entry():
    """Fix-round review: with no positive bid the remake fell back to the entry's average fill."""
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("9.7900"))
    seam._protect(slot, D(5), T0 + 5 * SECOND)  # through the held stop: sold from 9.79
    first = seam.sent[-1]
    assert first.price == Price.from_str("9.7800")
    seam.refuse(first)
    seam.cache.trade = lambda _: None
    seam._protect(slot, D(5), T0 + 11 * SECOND)  # resent with no bid and no trade
    assert len(seam.sent) == 2
    assert seam.sent[-1].price.as_decimal() <= first.price.as_decimal()


def test_a_hold_that_begins_after_the_holding_was_trailed_counts_its_60_s_from_its_own_start():
    """Fix-round review: a quote gap long after the fill sold at once as an unpriced exit."""
    seam, slot = held(bid="10.10")
    seam.fills.append(("E-1", OrderSide.BUY.name, "5", "10.00", T0))
    slot.entry_id = SimpleNamespace(value="E-1")
    seam.orders["E-1"].client_order_id = slot.entry_id
    seam._protect(slot, D(5), T0)
    assert seam.sent[-1].kind == "TRAILING"
    seam.refuse(seam.sent[-1])
    seam.bid = "0"
    seam._protect(slot, D(5), T0 + 10 * MINUTE)
    assert len(seam.sent) == 1 and not slot.exiting
    seam._protect(slot, D(5), T0 + 10 * MINUTE + 59 * SECOND)
    assert len(seam.sent) == 1
    seam._protect(slot, D(5), T0 + 11 * MINUTE)
    assert len(seam.sent) == 2 and slot.exit_record.trigger_reason == "unpriced_exit"


def test_the_floor_wait_restarts_when_the_bid_recovers_above_the_floor():
    """Fix-round review: a later dip released the floor without 60 s at it."""
    seam = Seam("9.50")
    slot = seam_slot(seam)
    slot.exiting, slot.floor = True, D("9.80")
    sale = seam._limit(AAA, OrderSide.SELL, Quantity.from_int(5), Price.from_str("9.80"))
    seam.orders[sale.client_order_id.value] = sale
    slot.protect_id = sale.client_order_id
    seam._protect(slot, D(5), T0 + 100 * SECOND)  # at the floor, bid through it
    seam.bid = "9.95"
    seam._protect(slot, D(5), T0 + 130 * SECOND)  # recovered: no longer through
    seam.bid = "9.50"
    seam._protect(slot, D(5), T0 + 165 * SECOND)  # 65 s after the first sighting, 0 s after this
    assert seam.canceled == []


def test_a_held_stop_sale_marks_the_entry_stopped_out():
    seam, slot = held()
    seam._protect(slot, D(5), T0)
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("9.7900"))
    seam._protect(slot, D(5), T0 + 5 * SECOND)
    assert slot.entry_record["stopped_out"] is True


# --- the landing review of P0b (high) --------------------------------------------------------------


def test_a_held_stop_after_a_reached_trigger_holds_at_that_trigger_on_alpacas_step():
    """A trailed holding whose sale was canceled at a reached 11.765 trigger, then held on a one-sided
    quote, keeps that trigger (rounded down to 11.76, main's price-step rule), not the fill less the
    trail."""
    seam, slot = held()
    slot.trailed, slot.trail_trigger = True, D("11.765")
    seam._protect(slot, D(5), T0)
    assert seam.sent == [] and slot.held_stop == D("11.76")
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("11.7000"))
    seam._protect(slot, D(5), T0 + 5 * SECOND)
    assert len(seam.sent) == 1 and slot.exit_record.trigger_reason == "held stop (last trade)"


def test_a_sale_is_not_chased_and_its_floor_not_released_while_the_bid_is_zero():
    seam = Seam("0")
    slot = seam_slot(seam)
    slot.exiting, slot.floor = True, D("9.80")
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(5), trigger_ns=T0, trigger_price=D("9.95"))
    sale = seam._limit(AAA, OrderSide.SELL, Quantity.from_int(5), Price.from_str("9.90"))
    seam.orders[sale.client_order_id.value] = sale
    slot.protect_id = sale.client_order_id
    for secs in (100, 160, 230):
        seam._protect(slot, D(5), T0 + secs * SECOND)
    assert seam.canceled == []
    sale.price = Price.from_str("9.80")
    for secs in (300, 361, 430):
        seam._protect(slot, D(5), T0 + secs * SECOND)
    assert seam.canceled == [] and slot.floor == D("9.80") and not slot.floor_released


def test_a_one_sided_quote_leaves_the_last_positive_bid():
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.on_quote(SimpleNamespace(instrument_id=AAA, bid_price=Price.from_str("10.0000")))
    s.on_quote(SimpleNamespace(instrument_id=AAA, bid_price=Price.from_str("0")))
    assert slot.last_bid == D("10.0000")


def test_the_trail_constants_and_slot_fields_carry_their_own_docstrings():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mover))
    body = tree.body
    documented = {}
    for i, node in enumerate(body[:-1]):
        nxt = body[i + 1]
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            is_doc = isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant)
            documented[node.targets[0].id] = nxt.value.value if is_doc else None
    assert "held stop" in str(documented["HOLD_MOST_SECS"])
    assert "chase floor" in str(documented["FLOOR_WAIT_SECS"])
    assert documented["TRAIL_CAP"] is not None

    def is_text(stmt):
        return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)

    doubled = [
        (cls.name, after.lineno)
        for cls in body
        if isinstance(cls, ast.ClassDef) and cls.name in ("Slot", "ExitRecord")
        for before, after in zip(cls.body, cls.body[1:], strict=False)
        if is_text(before) and is_text(after)
    ]
    assert doubled == []


def test_a_sized_entrys_held_stop_is_l_under_the_fill_and_its_sale_keeps_that_trigger():
    """S2 under #320's held stop: an entry sized for a loss per share L holds its stop L under the fill,
    rounded up onto the step (`mover.fill_trigger`), the level its trailing sell would start from, not
    the trail's fraction of the fill. The sale a print through it sends has that level beside it in the
    record (`sell_triggers`), as every other exit's sale does."""
    seam, slot = held()
    slot.sized_loss = D("0.30")
    seam._protect(slot, D(5), T0)
    assert seam.sent == [] and slot.held_stop == mover.fill_trigger(D("10.00"), D("0.30")) == D(
        "9.70"
    )
    seam.cache.trade = lambda _: SimpleNamespace(price=Price.from_str("9.6900"))
    seam._protect(slot, D(5), T0 + 5 * SECOND)
    assert len(seam.sent) == 1 and slot.exit_record.trigger_reason == "held stop (last trade)"
    assert D(seam.sell_triggers[seam.sent[0].client_order_id.value]) == D("9.70")


def test_a_sized_entrys_held_stop_follows_l_under_the_average_as_the_entry_fills_further():
    """Review of S2 over #320, finding 1: a later fill of the same entry moves the average, and the held
    stop follows L under it, as a trailing sell remade for all that is held would; the exit row's
    distance is L, the distance the held stop holds (finding 2)."""
    seam, slot = held()
    seam.orders["E-1"].avg_px = 9.90
    slot.sized_loss = D("0.30")
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(5))
    seam._protect(slot, D(2), T0)
    assert slot.held_stop == D("9.60")
    seam.orders["E-1"].avg_px = 9.95
    seam._protect(slot, D(5), T0 + SECOND)
    assert seam.sent == [] and slot.held_stop == D("9.65")
    assert slot.exit_record.trail_distance == D("0.30")


def test_l_met_when_the_first_trailing_sell_is_armed_is_a_stop_not_a_retained_trigger():
    """Review of S2 over #320, finding 3: with nothing reached, a bid already at L under the fill when the
    sale is first armed is the stop L, not a retained trigger, and the entry is stopped out."""
    seam = Seam("9.69")
    slot = seam_slot(seam)
    slot.sized_loss = D("0.30")
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(3))
    slot.entry_record = {"client_order_id": "E-1", "stopped_out": False}
    seam._protect(slot, D(3), T0)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    reason = slot.exit_record.trigger_reason
    assert reason is not None and reason.startswith("trailing stop"), reason
    assert slot.entry_record["stopped_out"] is True
