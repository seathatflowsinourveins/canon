"""T1/T2: synthetic minute bars and quotes, using rc8's ATR and real order emulator.

The existing test_mover fixtures provide an offline cash venue; no credential or network is read.
"""

from collections import deque
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from canon_alpaca.execution import not_sent_reason
from canon_alpaca.execution import sub_penny_denial
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import mover
from canon_trading import session_window
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
from tests.test_mover import entry_day
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


def test_rows_register_defaults_and_two_alternates_as_uncalibrated():
    rows = mover.TRAIL_SETTINGS
    settings = mover.MoverConfig(max_notional=D(100), max_move=D(5))
    assert settings.trail_mode == "fixed" and settings.cooldown_secs == 300
    assert settings.reentry_arm == "clock" and settings.turn_k == 2 and settings.turn_d == D(1)
    assert settings.atr_n == 14 and settings.atr_k == D(1)
    assert settings.chase_secs == 5 and settings.exit_ticks == 1
    assert rows["trail_mode"]["arms"] == ("fixed", "atr", "tier", "ratchet", "profile")
    assert rows["cooldown_secs"]["default"] == 300
    assert rows["reentry_arm"]["arms"] == ("clock", "reclaim", "turn", "vwap")
    assert rows["turn_k"]["arms"] == (1, 2, 3) and rows["turn_d"]["arms"] == (D(1), D(2))
    assert all(
        rows[name]["source"] == "lead, no primary source"
        for name in ("reentry_arm", "turn_k", "turn_d")
    )
    assert rows["atr_k"]["source"] == rows["spread_floor_m"]["source"] == "lead, no primary source"
    for field in (
        "atr_k",
        "atr_n",
        "spread_floor_m",
        "liquidity_tier_cut",
        "ratchet_r",
        "ratchet_g",
    ):
        assert rows[field]["status"] == "UNCALIBRATED"
        assert len(rows[field]["alternates"]) == 2
        assert getattr(settings, field) == rows[field]["default"]


def test_window_settings_select_the_registered_rows():
    default = session_window.WindowConfig()
    assert default.trail_mode == "fixed" and default.cooldown_secs == 300
    assert default.reentry_arm == "clock" and default.turn_k == 2 and default.turn_d == D(1)
    assert default.atr_n == 14 and default.atr_k == D(1)
    # The trail rows are Mover's: P0a refuses them by name for another strategy.
    config = session_window.WindowConfig(
        strategy="Mover", mover=True, trail_mode="ratchet", cooldown_secs=0
    )
    assert config.trail_mode == "ratchet" and config.cooldown_secs == 0
    with pytest.raises(ValueError, match="spread_floor_m"):
        session_window.WindowConfig(strategy="Mover", mover=True, spread_floor_m=D(0))


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
    [change] = entries[1]["trail_changes"]
    assert (D(change["from"]), D(change["to"]), change["why"]) == (
        D("0.002"),
        D("0.001"),
        "ratchet",
    )


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
    s.on_quote(
        SimpleNamespace(
            instrument_id=AAA, bid_price=Price.from_str("11.0000"), ts_event=T0, ts_init=T0
        )
    )
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


@pytest.mark.parametrize("cooldown", [0, 60, 300, 3600])
def test_legacy_cooldown_setting_does_not_delay_the_first_eligible_reclaiming_close(cooldown):
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=cooldown, run_bars=1)
    closes = [10.0, 10.1, 10.2, 10.3, 10.4, 10.0, 10.01, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 2
    assert (buys[1].ts_init - T0) // MINUTE == 7
    assert any("minute close" in note for note in s.record()["entries"][1]["notes"])
    eng.dispose()


def test_reclaim_can_reenter_on_the_first_close_after_its_sale_even_in_the_same_minute():
    """The minute's intrabar sale fills early; its later rising close already reclaims the sale."""
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=0, run_bars=1)
    closes = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
    data = tape(AAA, closes)
    # Reuse the tape's QuoteTick class: insert a stop-triggering quote during minute 7,
    # then recover before that minute closes. State, rather than elapsed time, admits that close.
    latest = next(x for x in data if hasattr(x, "bid_price"))
    data.append(
        type(latest)(
            AAA,
            Price.from_str("10.0100"),
            Price.from_str("10.0300"),
            Quantity.from_int(10000),
            Quantity.from_int(10000),
            T0 + 6 * MINUTE + 10 * SECOND,
            T0 + 6 * MINUTE + 10 * SECOND,
        )
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 2
    assert (buys[1].ts_init - T0) // MINUTE == 7
    reentry = s.record()["entries"][1]["reentry"]
    assert reentry["arm"] == "reclaim" and D(reentry["reentry_price"]) > D(reentry["sale_price"])
    eng.dispose()


@pytest.mark.parametrize(("k", "minute"), [(1, 11), (2, 12), (3, 13)])
def test_turn_reenters_on_its_first_k_rises_from_a_deep_low_below_the_sale(k, minute):
    eng = engine()
    s = strategy(reentry_arm="turn", turn_k=k, turn_d=D(1), cooldown_secs=3600)
    closes = RISING + [10.46, 10.0, 10.05, 10.1, 10.15, 10.2, 10.25]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 2 and (buys[1].ts_init - T0) // MINUTE == minute
    row = s.record()["entries"][1]["reentry"]
    assert row["arm"] == "turn" and D(row["low"]) == D("10.0")
    assert D(row["reentry_price"]) == D(str(buys[1].avg_px)) < D(row["sale_price"])
    assert row["minutes_between"] == pytest.approx(
        (buys[1].ts_last - (T0 + 9 * MINUTE - 1)) / MINUTE
    )
    # The lead's 09:30 rows: the rule, its fall/rise in the name's distance, and elapsed seconds.
    distance = D(row["distance"])
    assert row["rule"] == "turn" and row["fall_from"] == "sale" and distance > 0
    assert D(row["fall_in_distances"]) == (D(row["sale_price"]) - D("10.0")) / distance
    assert D(row["rise_in_distances"]) == (D(row["run_close"]) - D("10.0")) / distance
    entry = s.record()["entries"][1]
    assert row["secs_since_stop_out"] == pytest.approx(
        (entry["submitted_ns"] - row["sale_ns"]) / SECOND
    )
    eng.dispose()


@pytest.mark.parametrize(("depth", "count"), [(D(1), 2), (D(2), 1)])
def test_turn_depth_is_measured_in_the_sold_positions_actual_trail_distance(depth, count):
    eng = engine()
    s = strategy(reentry_arm="turn", turn_k=2, turn_d=depth)
    orders = run(
        eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.46, 10.1, 10.15, 10.2, 10.25])
    )
    assert len([o for o in orders if o.side == OrderSide.BUY]) == count
    if count == 1:
        assert any("turn depth" in why for why in s.dropped)
    eng.dispose()


def test_turn_low_resets_the_rising_count_and_the_next_sale_resets_the_state():
    eng = engine()
    s = strategy(reentry_arm="turn", turn_k=2, turn_d=D(1))
    closes = RISING + [10.46, 10.0, 10.1, 9.9, 10.0, 10.1, 10.2, 9.8, 9.5, 9.6, 9.7, 9.8]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert [(o.ts_init - T0) // MINUTE for o in buys] == [5, 14, 19]
    rows = [r["reentry"] for r in s.record()["entries"][1:]]
    assert [D(r["low"]) for r in rows] == [D("9.9"), D("9.5")]
    assert rows[0]["sale_price"] != rows[1]["sale_price"]
    eng.dispose()


def test_turn_still_yields_to_rank_eligibility_and_the_request_budget():
    s = strategy(reentry_arm="turn", turn_k=2)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.sold[AAA] = (T0, D("10.45"))
    s.sold_fill_ns[AAA] = T0
    s.reentry_states[AAA] = mover.ReentryState(
        T0, D("10.45"), D("0.21"), low=D(10), previous=D("10.1"), rises=2
    )
    slot.run_closed_ns = T0 + MINUTE
    s.rank_ns, s.rank = T0 + MINUTE, [(AAA, D("0.25"))]
    ranked = {AAA: (0, D("0.25"))}
    assert s._refusal(slot, D("10.1"), ranked, 0, T0 + MINUTE) is None
    unranked = s._refusal(slot, D("10.1"), {}, 0, T0 + MINUTE)
    capped = s._refusal(slot, D("10.1"), ranked, s.settings.soft_line, T0 + MINUTE)
    assert unranked is not None and "no longer ranked" in unranked
    assert capped is not None and "soft line" in capped


def test_vwap_reentry_refuses_live_bars_by_name_without_fabricating_a_vwap():
    eng = engine()
    s = strategy(reentry_arm="vwap")
    orders = run(
        eng,
        s,
        ranker(rank((AAA, "0.25"))),
        tape(AAA, RISING + [10.46, 10.0, 10.1, 10.2, 10.6, 10.7]),
    )
    assert len([o for o in orders if o.side == OrderSide.BUY]) == 1
    assert any("vwap unavailable" in why for why in s.dropped)
    eng.dispose()


@pytest.mark.parametrize("entry_arm", ["bridge", "registered"])
def test_vwap_tape_reentry_requires_a_close_below_then_the_first_close_above(entry_arm):
    eng = engine()
    s = strategy(entry_arm=entry_arm, reentry_arm="vwap", cooldown_secs=3600)
    closes = RISING + [10.46, 10.0, 9.95, 9.9, 10.0, 10.1]
    if entry_arm == "registered":
        s.entry_contexts[AAA] = replace(entry_day(closes, 1000), reference=8.0)
    # Exact session VWAPs supplied by a tape, keyed by that completed bar's receipt time.
    # A falling close crosses when the tape's session VWAP falls farther; no rising-run delay.
    s.session_vwaps[AAA] = {
        T0 + 10 * MINUTE: D("9.9"),
        T0 + 11 * MINUTE: D("10.0"),
        T0 + 12 * MINUTE: D("9.8"),
    }
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 2 and (buys[1].ts_init - T0) // MINUTE == 12
    assert s.record()["entries"][1]["reentry"]["arm"] == "vwap"
    eng.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reentry_arm", "guess"),
        ("turn_k", 0),
        ("turn_k", 4),
        ("turn_d", D(0)),
        ("turn_d", D(3)),
        ("turn_d", D("NaN")),
    ],
)
def test_reentry_rows_only_select_registered_state_arms(field, value):
    with pytest.raises(ValueError, match=field):
        strategy(**{field: value})


def test_flow_alternate_and_shipped_atr_default_are_registered_without_changing_defaults():
    rows = mover.TRAIL_SETTINGS
    assert rows["flow_share"]["default"] == D("0.05")
    assert D("0.01") in rows["flow_share"]["alternates"]
    assert "Lean" in rows["flow_share"]["source"]
    notes = " ".join(strategy().record()["trail_notes"])
    assert "backtesting.py" in notes and "6 ATR" in notes and "bar frequency" in notes
    assert "note" in notes and "row" in notes
    assert mover.MoverConfig(max_notional=D(100), max_move=D(5)).atr_k == D(1)


@pytest.mark.parametrize("k", [1, 2])
def test_registered_entry_keeps_listing_eligibility_but_turn_uses_its_own_k(k):
    eng = engine()
    s = strategy(entry_arm="registered", reentry_arm="turn", turn_k=k)
    closes = RISING + [10.46, 10.0, 10.05, 10.1, 10.15, 10.2]
    s.entry_contexts[AAA] = replace(entry_day(closes, 1000), reference=8.0)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 2 and (buys[1].ts_init - T0) // MINUTE == 10 + k, s.dropped
    eng.dispose()


def test_vwap_cannot_reclaim_a_below_close_from_an_earlier_session():
    state = mover.ReentryState(T0, D(10), D("0.21"))
    state.observe(D("9.9"), D(10), date(2025, 11, 10))
    state.observe(D("10.1"), D(10), date(2025, 11, 11))
    assert state.crossed_vwap is False
    state.observe(D("9.9"), D(10), date(2025, 11, 11))
    state.observe(D("10.1"), None, date(2025, 11, 11))
    state.observe(D("10.2"), D(10), date(2025, 11, 11))
    assert state.crossed_vwap is False


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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("9.7900"), ts_event=T0, ts_init=T0
    )
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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("10.0000"), ts_event=T0, ts_init=T0
    )
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


def test_a_run_seen_before_the_sale_fill_is_refused_by_the_enter_step(monkeypatch):
    s = strategy(reentry_arm="reclaim", run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.sold[AAA] = (T0 + MINUTE, D("10.00"))
    s.sold_fill_ns[AAA] = T0 + MINUTE
    s.rank_ns, s.rank = T0 + MINUTE, [(AAA, D("0.25"))]
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    slot.run_ns, slot.run_close, slot.run_seen_ns = T0, D("10.50"), T0 + MINUTE - SECOND
    slot.run_closed_ns = T0 + MINUTE
    s._enter(T0 + MINUTE + SECOND, 0)
    assert any("preceded the sale fill" in why for why in s.dropped)


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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("9.7900"), ts_event=T0, ts_init=T0
    )
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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("9.7900"), ts_event=T0, ts_init=T0
    )
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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("11.7000"), ts_event=T0, ts_init=T0
    )
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
    s.on_quote(
        SimpleNamespace(
            instrument_id=AAA, bid_price=Price.from_str("10.0000"), ts_event=T0, ts_init=T0
        )
    )
    s.on_quote(
        SimpleNamespace(instrument_id=AAA, bid_price=Price.from_str("0"), ts_event=T0, ts_init=T0)
    )
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
    seam.cache.trade = lambda _: SimpleNamespace(
        price=Price.from_str("9.6900"), ts_event=T0, ts_init=T0
    )
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


def test_a_reopened_slot_starts_its_turn_and_crossing_from_the_next_observed_close():
    # A name unwatched after its sale misses the closes in between: no rise or VWAP side spans the gap.
    eng = engine()
    s = strategy(reentry_arm="turn", turn_k=2)
    eng.add_strategy(s)
    for name in ("subscribe_quotes", "subscribe_trades", "subscribe_bars"):
        setattr(s, name, cast(Any, lambda *args, **kwargs: None))  # no engine run, so no feed
    state = s.reentry_states[AAA] = mover.ReentryState(
        T0,
        D("10.45"),
        D("0.21"),
        low=D(10),
        previous=D("10.1"),
        rises=1,
        vwap=D("10.05"),
        below_vwap=True,
        crossed_vwap=True,
        vwap_day=date(2026, 11, 12),
    )
    s._open_slot(AAA)
    assert (state.low, state.previous, state.rises) == (None, None, 0)
    assert (state.vwap, state.below_vwap, state.crossed_vwap) == (None, False, False)
    assert (state.sale_ns, state.sale_price, state.trail_distance) == (T0, D("10.45"), D("0.21"))
    state.observe(D("10.2"), None, date(2026, 11, 12))
    assert (state.low, state.rises) == (D("10.2"), 0)
    eng.dispose()


@pytest.mark.parametrize(("arm", "noted"), [("clock", False), ("turn", True)])
def test_the_state_reentry_note_is_recorded_only_when_a_state_arm_runs(arm, noted):
    s = strategy(reentry_arm=arm)
    assert (mover.RECLAIM_LIMIT in s.record()["trail_notes"]) is noted
    eng = engine()
    s = strategy(reentry_arm=arm)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert (mover.RECLAIM_LIMIT in s.record()["entries"][0]["notes"]) is noted
    eng.dispose()


def test_a_paper_window_refuses_the_vwap_arm_by_name():
    # The tape supplies session VWAPs; a paper window has no live source, so the arm cannot run.
    with pytest.raises(ValueError, match="reentry_arm vwap"):
        session_window.WindowConfig(strategy="Mover", mover=True, reentry_arm="vwap")
    config = session_window.WindowConfig(strategy="Mover", mover=True, reentry_arm="turn")
    assert config.reentry_arm == "turn"
    assert strategy(reentry_arm="vwap").settings.reentry_arm == "vwap"  # the tape's replay keeps it


def test_the_cooldown_row_names_the_clock_arm():
    rows = (Path(__file__).parents[1] / "docs/handbook/rows.md").read_text()
    line = next(row for row in rows.splitlines() if row.startswith("| `COOLDOWN_SECS`"))
    assert "clock" in line


def test_seven_slots_chasing_over_the_soft_line_each_re_price_their_due_sale_without_waiting():
    """The lead, 2026-11-12: the soft line slows entries only."""
    seam = Seam("9.90")
    seam.recent = seam.settings.soft_line + 7  # the count the soft line held chases at, before
    slots = []
    for _ in range(7):
        slot = seam_slot(seam)
        slot.exiting, slot.floor = True, D("9.50")
        sale = seam._limit(AAA, OrderSide.SELL, Quantity.from_int(5), Price.from_str("10.00"))
        seam.orders[sale.client_order_id.value] = sale
        slot.protect_id = sale.client_order_id
        slots.append(slot)
    for slot in slots:
        seam._protect(slot, D(5), T0 + 100 * SECOND)
    assert len(seam.canceled) == 7
    assert not any("soft line" in note for note in seam.dropped)


# --- the lead's follow-ups, 2026-11-12: ATR warmed from the cache, tightened once when it warms ---


def minute_bars(closes, *, width="0.04"):
    bars = []
    for i, close in enumerate(closes):
        bars.append(
            Bar(
                bar_type(AAA),
                Price.from_str(f"{close:.4f}"),
                Price.from_str(str(D(f"{close:.4f}") + D(width) / 2)),
                Price.from_str(str(D(f"{close:.4f}") - D(width) / 2)),
                Price.from_str(f"{close:.4f}"),
                Quantity.from_int(1000),
                T0 + i * MINUTE,
                T0 + (i + 1) * MINUTE,
            )
        )
    return bars


def test_a_slot_warms_its_atr_from_the_cached_minute_bars_newest_first():
    s = strategy(trail_mode="atr", atr_n=3)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(3)
    bars = minute_bars([10.0, 10.1, 10.2, 10.3, 10.4])
    s._warm_atr(slot, list(reversed(bars)), T0 + 5 * MINUTE)
    reference = AverageTrueRange(3)
    for bar in bars[-4:]:
        reference.handle_bar(bar)
    assert slot.atr.initialized and slot.atr.value == pytest.approx(reference.value)
    assert any("ATR warmed from 4 cached" in note for note in s.notes[AAA])


def test_too_few_cached_bars_leave_the_atr_cold_and_bars_after_now_are_not_used():
    s = strategy(trail_mode="atr", atr_n=3)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(3)
    bars = minute_bars([10.0, 10.1, 10.2, 10.3])
    s._warm_atr(slot, list(reversed(bars)), T0 + 2 * MINUTE)
    assert not slot.atr.initialized


def test_a_streamed_bar_received_before_its_interval_ends_cannot_warm_the_atr():
    # Main's parse_bar preserves stream receipt time in ts_init; receipt alone is not a close.
    s = strategy(trail_mode="atr", atr_n=3)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(3)
    bars = [
        Bar(
            bar.bar_type,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.ts_event,
            bar.ts_event + SECOND,
        )
        for bar in minute_bars([10.0, 10.1, 10.2])
    ]
    assert s._warm_atr(slot, list(reversed(bars)), T0 + 2 * MINUTE + 30 * SECOND) is False
    assert not slot.atr.initialized


def test_a_slot_opened_in_the_engine_warms_from_bars_the_cache_already_holds():
    # The name is ranked only from minute 6, after six bars are in the cache; one live bar later it
    # enters. Without the warm the ATR would hold one live bar, and the entry would be cold.
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3, run_bars=1)
    late = ranker(rank(), values={6: rank((AAA, "0.25"))})
    orders = run(
        eng, s, late, tape(AAA, [10.0, 10.1, 10.0, 10.1, 10.0, 10.1, 10.0, 10.2, 10.3, 10.4, 10.5])
    )
    assert orders, "the warmed name entered"
    entry = s.record()["entries"][0]
    assert entry["trail_source"] != "cold_atr_fallback" and entry["atr"] is not None
    assert entry["atr_warm_bars"] == 4
    assert any("ATR warmed from 4 cached" in note for note in s.notes[AAA])
    eng.dispose()


def test_a_cold_fallback_holding_tightens_once_to_the_warmed_atr_and_records_the_change():
    eng = engine()
    s = strategy(trail_mode="atr", atr_n=6, atr_k=D(1))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.8, 10.9, 11.0, 11.1]))
    entry = s.record()["entries"][0]
    assert entry["trail_source"] == "cold_atr_fallback"
    [change] = entry["trail_changes"]
    assert D(change["to"]) < D(change["from"]) == D("0.02")
    assert change["when_ns"] > entry["submitted_ns"] and change["bound"] in ("atr", "spread floor")
    sells = [o for o in orders if o.side == OrderSide.SELL]
    assert sells[0].status.name == "CANCELED"
    assert sells[1].trailing_offset < sells[0].trailing_offset
    eng.dispose()


def test_a_warmed_atr_wider_than_the_fallback_never_widens_the_trail():
    seam = Seam("10.50", trail_mode="atr")
    slot = seam_slot(seam)
    slot.trail_plan = mover.TrailPlan(
        D("0.02"), D("0.005"), D("0.02"), None, D("0.01"), "fixed", None, "cold_atr_fallback"
    )
    slot.atr = AverageTrueRange(3)
    for bar in minute_bars([10.0, 10.5, 10.0, 10.5], width="1.0"):
        slot.atr.handle_bar(bar)
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0) is False
    assert slot.trail_plan.trail == D("0.02")


def test_a_failed_warm_check_is_retried_on_the_next_step_not_given_up():
    seam = Seam("10.50", trail_mode="atr")
    slot = seam_slot(seam)
    slot.entry_record = {"trail_changes": []}
    slot.trail_plan = mover.TrailPlan(
        D("0.02"), D("0.005"), D("0.02"), None, D("0.01"), "fixed", None, "cold_atr_fallback"
    )
    slot.atr = AverageTrueRange(3)
    for bar in minute_bars([10.0, 10.02, 10.0, 10.02], width="0.02"):
        slot.atr.handle_bar(bar)
    crossed = SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.4900")
    )
    seam.cache.quote = lambda _: crossed
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0) is False
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0 + SECOND) is True
    assert slot.trail_plan.trail < D("0.02")
    assert slot.entry_record is not None
    [change] = slot.entry_record["trail_changes"]
    assert change["when_ns"] == T0 + SECOND


def test_cached_bars_from_an_earlier_session_part_do_not_warm_the_atr():
    # T0 is 11:00 New York; bars opening before the regular part's 09:30 start are another part's.
    s = strategy(trail_mode="atr", atr_n=3)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(3)
    early = T0 - 92 * MINUTE  # 09:28 New York
    bars = [replace_bar(bar, early) for bar in minute_bars([10.0, 10.1, 10.2, 10.3])]
    s._warm_atr(slot, list(reversed(bars)), T0)
    assert not slot.atr.initialized


def replace_bar(bar, start):
    """`bar` moved so the first of `minute_bars` opens at `start`."""
    shift = start - T0
    return Bar(
        bar.bar_type,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event + shift,
        bar.ts_init + shift,
    )


def test_a_warmed_plan_never_sells_further_through_or_chases_deeper_than_the_cold_plan():
    # Review (high) of P1: the warmed trail is tighter, so its sale's offset and chase floor may not widen.
    seam = Seam("10.50", trail_mode="atr", exit_through=D("0.001"), chase_cap=D("0.005"))
    slot = seam_slot(seam)
    slot.entry_record = {"trail_changes": []}
    slot.trail_plan = mover.TrailPlan(
        D("0.02"), D("0.001"), D("0.005"), None, D("0.01"), "fixed", None, "cold_atr_fallback"
    )
    slot.atr = AverageTrueRange(3)
    for bar in minute_bars([10.0, 10.1, 10.0, 10.1], width="0.15"):
        slot.atr.handle_bar(bar)
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0) is True
    plan = slot.trail_plan
    assert D("0.004") < plan.trail < D("0.02")
    assert plan.exit_through <= D("0.001") and plan.chase_cap <= D("0.005")


def test_a_tier_plan_reselected_after_the_warm_is_not_applied_as_the_warmed_atr():
    # Review (high) of P1: a baseline that arrived after entry makes the re-selection tier, not ATR.
    seam = Seam("10.50", trail_mode="tier")
    seam.baseline_dollar_volume_per_minute[AAA] = D(2_000_000)
    slot = seam_slot(seam)
    slot.entry_record = {"trail_changes": []}
    cold = mover.TrailPlan(
        D("0.02"), D("0.005"), D("0.02"), None, D("0.01"), "fixed", None, "cold_atr_fallback"
    )
    slot.trail_plan = cold
    slot.atr = AverageTrueRange(3)
    for bar in minute_bars([10.0, 10.02, 10.0, 10.02], width="0.02"):
        slot.atr.handle_bar(bar)
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0) is False
    assert slot.trail_plan is cold and slot.entry_record["trail_changes"] == []
    assert any("tier" in note for note in seam.dropped)


def test_a_non_finite_entry_average_falls_back_to_the_protection_anchor():
    # Review (high) of P1: as `_trail_anchor`, so a NaN average cannot raise in the step timer.
    seam = Seam("10.50", trail_mode="atr")
    slot = seam_slot(seam)
    slot.protection_anchor = D("10.40")
    cast(Any, seam)._order = lambda _: SimpleNamespace(avg_px=float("nan"))
    assert mover.Mover._bought(cast(Any, seam), slot) == D("10.40")
    cast(Any, seam)._order = lambda _: SimpleNamespace(avg_px=0.0)
    assert mover.Mover._bought(cast(Any, seam), slot) == D("10.40")


# --- the lead's ruling of 2026-11-12, 21:01: a recorded tightening hands the trigger over ---------------


def sized_cold_holding(sized_loss: str, width: str, **rows: Any) -> tuple[Any, Any]:
    """A budget-sized entry filled at 10.00 on the cold fallback's 0.02 plan, trailing by L, and an
    ATR warmed from four minutes `width` wide, on a quote 0.01 wide."""
    seam = RatchetSeam(
        "10.5000",
        trail_mode="atr",
        size_from_budget=True,
        loss_share=mover.PAPER_LOSS_SHARE,
        **rows,
    )
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5100")
    )
    slot = seam_slot(seam)
    slot.entry_record = {"client_order_id": "E-1", "trail_changes": []}
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.02"), D("0.005"), D("0.02"), None, D("0.01"), "fixed", None, "cold_atr_fallback"
    )
    slot.sized_loss = D(sized_loss)
    seam._protect(slot, D(5), T0)
    assert seam.sent[0].trailing_offset == D(sized_loss)
    slot.atr = AverageTrueRange(3)
    for bar in minute_bars([10.0, 10.1, 10.0, 10.1], width=width):
        slot.atr.handle_bar(bar)
    return seam, slot


def test_a_warm_tighten_on_a_sized_entry_remakes_the_sale_at_the_warmed_offset_not_l():
    """The lead's ruling of 2026-11-12, 21:01: after every remake the working sale trails by the
    distance the record says. The warmed ATR tightens a sized cold entry's 0.02 plan; the remade sale
    trails by the warmed plan's offset, not by the 0.20 L it was sized for."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    seam._protect(slot, D(5), T0 + SECOND)
    [change] = slot.entry_record["trail_changes"]
    plan = slot.trail_plan
    assert D(change["to"]) == plan.trail < D("0.02")
    assert len(seam.sent) == 2 and seam.canceled == [seam.sent[0].client_order_id.value]
    remade = seam.sent[1]
    assert remade.trailing_offset == mover.offsets(D(10), plan.trail, plan.exit_through)[0]
    assert remade.trailing_offset < D("0.20")
    assert slot.exit_record.trail_distance == remade.trailing_offset


def test_a_warm_tighten_wider_than_l_in_force_leaves_the_sized_sale_alone():
    """The lead's ruling of 2026-11-12, 21:01: a tightening only moves the trigger closer. The warmed
    plan is narrower than the 0.02 fallback but its offset is wider than the 0.05 L in force, so the
    sale is not remade and nothing is recorded as a change; the check, made once, is spent."""
    seam, slot = sized_cold_holding("0.05", "0.15")
    seam._protect(slot, D(5), T0 + SECOND)
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.trail_plan.trail == D("0.02") and slot.entry_record["trail_changes"] == []
    assert slot.warm_checked is True
    assert any("not tighter than L" in note for note in seam.dropped), seam.dropped


def test_a_ratchet_on_a_sized_entry_remakes_the_sale_at_the_ratchets_offset_not_l():
    """The lead's ruling of 2026-11-12, 21:01, for the ratchet S2 already hands over: a sized entry on
    a 0.004 plan trailing by L = 0.03 ratchets to 0.002, and the remade sale trails by 0.02."""
    seam, slot = ratchet_seam("10.1100", "10.1200")
    slot = cast(Any, slot)
    slot.sized_loss = D("0.03")
    seam._protect(slot, D(5), T0)
    assert seam.sent[0].trailing_offset == D("0.03")
    seam._protect(slot, D(5), T0 + SECOND)
    assert slot.ratcheted is True and slot.trail_plan.trail == D("0.002")
    assert len(seam.sent) == 2
    assert (seam.sent[1].trailing_offset, seam.sent[1].limit_offset) == (D("0.02"), D("0.03"))
    assert slot.exit_record.trail_distance == D("0.02")


def test_a_sized_entrys_fill_records_the_distance_l_puts_in_force_not_the_plans():
    """The high read of the ruling's fix, item 2: the BUY fill records the exit row's trail distance
    from the offsets in force. A 0.03 plan on a 10.00 fill is 0.30, but the entry sized for L = 0.50
    trails by L, which the re-entry turn then reads."""
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.03"), D("0.0075"), D("0.03"), None, None, "atr"
    )
    slot.sized_loss = D("0.50")
    slot.entry_id = ClientOrderId("BUY-1")
    s.on_order_filled(
        SimpleNamespace(
            instrument_id=AAA,
            client_order_id=slot.entry_id,
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.0000"),
            ts_event=T0,
        )
    )
    assert slot.exit_record is not None
    assert slot.exit_record.trail_distance == D("0.50")


def sale_event(order: Any, at: int, **fields: Any) -> SimpleNamespace:
    """An order event of one sale, in the plain attributes the Mover's handlers read."""
    return SimpleNamespace(
        instrument_id=AAA, client_order_id=order.client_order_id, ts_event=at, **fields
    )


def handling(seam: Any, slot: Any) -> Any:
    """The Seam with the state the Mover's order-event handlers write to."""
    seam.slots, seam.fills_to_report, seam.refused = {AAA: slot}, [], []
    seam.order_refusals = []
    seam.opening_states, seam.opening_outcomes = {}, {}
    return seam


def test_each_sale_sent_is_a_row_with_the_offsets_on_the_order_and_the_distance_the_record_gives():
    """The lead's D3 terms of 2026-11-12, A3: after every remake the offset on the order sent equals
    the distance the record gives. Each sale order the Mover sends is one row of its holding's exit
    record, with the offsets read off the order, the record's distance at that moment, the trigger
    kept and the spread at that step, so one window's record shows the term for every remake."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    seam._protect(slot, D(5), T0 + SECOND)
    rows = slot.exit_record.record()["sales"]
    assert [row["client_order_id"] for row in rows] == [o.client_order_id.value for o in seam.sent]
    assert [row["sent_ns"] for row in rows] == [T0, T0 + SECOND]
    for row, order in zip(rows, seam.sent, strict=True):
        assert row["kind"] == "trailing_stop_limit" and row["quantity"] == "5"
        assert D(row["trailing_offset"]) == order.trailing_offset
        assert D(row["limit_offset"]) == order.limit_offset
        assert D(row["distance_in_force"]) == order.trailing_offset
        assert D(row["spread"]) == D("0.01")
        assert (row["kept_trigger"] is None) == (order.trigger_price is None)
        assert row["kept_trigger"] is None or D(row["kept_trigger"]) == D(str(order.trigger_price))
        assert (row["ended"], row["ended_ns"], row["reason"]) == (None, None, None)
    assert D(rows[0]["distance_in_force"]) == D("0.20") > D(rows[1]["distance_in_force"])
    assert D(rows[0]["kept_trigger"]) == D("9.80")


def test_a_sale_rows_end_and_its_refusal_reason_come_from_its_order_events():
    """A3 and A7 of the D3 terms: a sale row ends by its own order's events, canceled, expired, filled
    in full across partial fills, or refused with the venue's or the risk engine's reason, and a limit
    sale names its price and the exit's trigger kept beside it."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    seam._protect(slot, D(5), T0 + SECOND)
    host = handling(seam, slot)
    first, remade = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(first, T0 + 2 * SECOND))
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(remade, T0 + 3 * SECOND, last_qty=Quantity.from_int(2), **sold)
    )
    rows = slot.exit_record.record()["sales"]
    assert (rows[0]["ended"], rows[0]["ended_ns"], rows[0]["reason"]) == (
        "canceled",
        T0 + 2 * SECOND,
        None,
    )
    assert rows[1]["ended"] is None and rows[1]["filled_quantity"] == "2"
    mover.Mover.on_order_filled(
        host, sale_event(remade, T0 + 4 * SECOND, last_qty=Quantity.from_int(3), **sold)
    )
    row = slot.exit_record.record()["sales"][1]
    assert (row["ended"], row["ended_ns"], row["filled_quantity"]) == (
        "filled",
        T0 + 4 * SECOND,
        "5",
    )
    slot.exit_trigger = D("9.80")
    for at, handler, reason in (
        (5, mover.Mover.on_order_rejected, "potential wash trade detected"),
        (7, mover.Mover.on_order_denied, "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE"),
        (9, mover.Mover.on_order_expired, None),
    ):
        seam._limit_sell(slot, D(5), T0 + at * SECOND, None)
        limit = seam.sent[-1]
        fields = {} if reason is None else {"reason": reason}
        handler(host, sale_event(limit, T0 + (at + 1) * SECOND, **fields))
        row = slot.exit_record.record()["sales"][-1]
        assert row["kind"] == "limit" and row["trailing_offset"] is None
        assert D(row["limit_price"]) == D(str(limit.price)) and D(row["kept_trigger"]) == D("9.80")
        ended = handler.__name__.removeprefix("on_order_")
        assert (row["ended"], row["ended_ns"], row["reason"]) == (
            ended,
            T0 + (at + 1) * SECOND,
            reason,
        )


def test_a_windows_sale_rows_end_by_the_simulated_venues_own_events():
    """A3 on the real emulator and simulated venue: every sale the Mover sent is a row; each trailing
    sale's offset equals the distance the record gave when it was sent; each row ends, and the rows'
    filled quantities add up to the exit's."""
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.46, 10.0, 9.9]))
    (row,) = s.record()["exits"]
    sales = row["sales"]
    assert sales, row
    for sale in sales:
        if sale["kind"] == "trailing_stop_limit":
            assert D(sale["trailing_offset"]) == D(sale["distance_in_force"])
    assert [sale["ended"] for sale in sales][-1] == "filled"
    assert sum(D(sale["filled_quantity"]) for sale in sales) == D(row["filled_quantity"])
    eng.dispose()


def test_each_entry_names_its_average_fill_price_as_it_fills():
    """The lead's D3 terms of 2026-11-12, A1: a trail is checked against the spread floor in price
    terms, so each entry names its own average fill price, updated by every BUY fill, and stays null
    until the first."""
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + [10.46, 10.0, 9.9]))
    (entry,) = s.record()["entries"]
    buys = [f for f in s.fills if f[1] == "BUY" and f[0] == entry["client_order_id"]]
    assert buys
    quantity = sum((D(f[2]) for f in buys), D(0))
    assert D(entry["entry_price"]) == sum((D(f[2]) * D(f[3]) for f in buys), D(0)) / quantity
    eng.dispose()
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    slot.entry_id = ClientOrderId("BUY-1")
    record: dict[str, Any] = {"client_order_id": "BUY-1", "entry_price": None}
    slot.entry_record = record
    s.entries_sent.append(record)  # as `_enter` registers each entry it sends
    for qty, px, at in ((2, "10.0000", T0), (3, "10.5000", T0 + SECOND)):
        s.on_order_filled(
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=slot.entry_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_int(qty),
                last_px=Price.from_str(px),
                ts_event=at,
            )
        )
    price = record["entry_price"]
    assert isinstance(price, str) and D(price) == D("10.3")


@pytest.mark.parametrize(
    ("handler", "reason", "by", "cause"),
    [
        (
            "on_order_rejected",
            "Alpaca refused the order (403): potential wash trade detected. use complex orders",
            "venue",
            "wash_trade",
        ),
        (
            "on_order_denied",
            sub_penny_denial(Price.from_str("10.005"), "stop price"),
            "before_venue",
            "off_step",
        ),
        (
            # Alpaca's own rejection of a price off its step, as "Orders at Alpaca" gives it
            "on_order_rejected",
            (
                "invalid limit_price 290.123. sub-penny increment does not fulfill minimum "
                "pricing criteria"
            ),
            "venue",
            "off_step",
        ),
        ("on_order_rejected", "Alpaca refused the order (403): insufficient qty", "venue", "other"),
        ("on_order_rejected", not_sent_reason(TimeoutError("connect")), "before_venue", "other"),
        ("on_order_denied", "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE", "before_venue", "other"),
    ],
)
def test_each_refusal_names_who_refused_it_and_its_cause(handler, reason, by, cause):
    """The lead's D3 terms of 2026-11-12, A7: Alpaca refuses no order for a cause of ours. Each refusal
    names who refused it (the venue, or the node before the venue, which a rejection the adapter
    proves was never sent is: `not_sent_reason`, the high read's finding 1) and its cause: a wash
    trade (the paper text the adapter README records, "Wash trades"), a price off Alpaca's step (the
    adapter's `sub_penny_denial` text, or Alpaca's own as "Orders at Alpaca" gives it, the evidence
    reader's finding), or other; a sale's row carries the same cause."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    getattr(mover.Mover, handler)(host, sale_event(sale, T0 + SECOND, reason=reason))
    assert host.refused == [(sale.client_order_id.value, reason)]
    assert host.order_refusals == [
        {"client_order_id": sale.client_order_id.value, "by": by, "cause": cause, "reason": reason}
    ]
    (row,) = slot.exit_record.record()["sales"]
    assert (row["reason"], row["cause"]) == (reason, cause)


@pytest.mark.parametrize("end", ["on_order_canceled", "on_order_expired"])
def test_a_sale_filled_after_its_cancel_or_expiry_ends_filled(end):
    """The high read of the sale rows, finding 1: the pinned order state machine lets a canceled or
    expired order fill ("Real world possibility", `crates/model/src/orders/mod.rs:247`, `:295`), as a
    chase's cancel crossing a fill at Alpaca does. A row filled in full after such an end reads
    filled, at its fill's time; a part fill leaves the earlier end."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    getattr(mover.Mover, end)(host, sale_event(sale, T0 + SECOND))
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 2 * SECOND, last_qty=Quantity.from_int(2), **sold)
    )
    row = slot.exit_record.record()["sales"][0]
    assert (row["ended"], row["ended_ns"]) == (end.removeprefix("on_order_"), T0 + SECOND)
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 3 * SECOND, last_qty=Quantity.from_int(3), **sold)
    )
    row = slot.exit_record.record()["sales"][0]
    assert (row["ended"], row["ended_ns"], row["filled_quantity"]) == (
        "filled",
        T0 + 3 * SECOND,
        "5",
    )


def test_an_entrys_average_fill_price_counts_its_own_fills_only():
    """The pre-landing reader of A3 with O (2026-11-13), P2-1: an entry's price is its own fills'
    average, read by its client order ID. A late fill of an earlier entry in the same name goes to
    that entry's record, and none of it to the current one's."""
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    old: dict[str, Any] = {"client_order_id": "E-old", "entry_price": None}
    new: dict[str, Any] = {"client_order_id": "E-new", "entry_price": None}
    s.entries_sent.extend([old, new])
    slot.entry_id, slot.entry_record = ClientOrderId("E-new"), new
    for cid, px, at in (("E-old", "8.0000", T0), ("E-new", "10.0000", T0 + SECOND)):
        s.on_order_filled(
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=ClientOrderId(cid),
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_int(1),
                last_px=Price.from_str(px),
                ts_event=at,
            )
        )
    assert [D(str(row["entry_price"])) for row in (new, old)] == [D("10"), D("8")]


@pytest.mark.parametrize("gone", ["exit_record", "slot"])
def test_a_sale_filled_after_its_holding_is_cleared_still_ends_filled(gone):
    """The pre-landing reader of A3 with O (2026-11-13), P2-2: a sale row is found by its client order
    ID, so a fill that arrives after the holding's exit record is cleared (`_flat`), or after its
    slot is removed (`_close_slot`), still reaches the row the record keeps."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(sale, T0 + SECOND))
    row = slot.exit_record
    if gone == "slot":
        del host.slots[AAA]
    else:
        slot.exit_record = None
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 2 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    (kept,) = row.record()["sales"]
    assert (kept["ended"], kept["ended_ns"], kept["filled_quantity"]) == (
        "filled",
        T0 + 2 * SECOND,
        "5",
    )


def test_an_entrys_opening_and_reentry_rows_take_its_own_fills_only():
    """The high review of the reader fixes (2026-11-13), should-fix 1: an entry's opening and
    re-entry rows name its own average fill and its own first fill's time, like its `entry_price`. A
    late fill of an earlier entry goes to that entry's rows, and none of it to the current one's."""
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    old_reentry: dict[str, Any] = {"reentry_price": None, "minutes_between": None, "sale_ns": T0}
    new_reentry: dict[str, Any] = {
        "reentry_price": None,
        "minutes_between": None,
        "sale_ns": T0 + MINUTE,
    }
    opening: dict[str, Any] = {"entry_price": None}
    old = {"client_order_id": "E-old", "entry_price": None, "reentry": old_reentry}
    new = {
        "client_order_id": "E-new",
        "entry_price": None,
        "reentry": new_reentry,
        "opening": opening,
    }
    s.entries_sent.extend([old, new])
    slot.entry_id, slot.entry_record = ClientOrderId("E-new"), new
    for cid, px, at in (("E-old", "8.0000", T0 + MINUTE), ("E-new", "10.0000", T0 + 3 * MINUTE)):
        s.on_order_filled(
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=ClientOrderId(cid),
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_int(1),
                last_px=Price.from_str(px),
                ts_event=at,
            )
        )
    assert D(str(opening["entry_price"])) == D("10")
    assert (D(str(new_reentry["reentry_price"])), new_reentry["minutes_between"]) == (D("10"), 2.0)
    assert (D(str(old_reentry["reentry_price"])), old_reentry["minutes_between"]) == (D("8"), 1.0)


def test_a_late_fill_of_an_earlier_holdings_sale_sells_the_current_holding_and_keeps_its_price():
    """The pre-landing reader of the a3fix commit (xr7-r-a3fix, 2026-11-13), P1: the shares in a name
    are one position, so a late fill of an earlier holding's sale sells the current holding. It counts
    toward the current holding's sold quantity, notional and final fill, and so toward the re-entry
    state, as before the record fix; under reclaim `_flat` then keeps the sale price 10.30 a reclaim
    is judged against, not None. The sale's own row reads filled, and the earlier holding's totals
    do not count it a second time."""
    seam, slot = sized_cold_holding("0.20", "0.15", reentry_arm="reclaim")
    host = handling(seam, slot)
    host.sold, host.sold_fill_ns, host.reentry_states = {}, {}, {}
    (sale,) = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(sale, T0 + SECOND))
    earlier = slot.exit_record
    slot.exit_record = None
    current = seam._ensure_exit(slot, D(5))  # the name held again: a new holding's record
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 2 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    assert earlier.record()["sales"][0]["filled_quantity"] == "5"
    assert (earlier.filled, earlier.notional, earlier.final_fill_ns) == (D(0), D(0), None)
    assert (current.filled, current.notional, current.final_fill_ns) == (
        D(5),
        D("51.5"),
        T0 + 2 * SECOND,
    )
    assert host.sold[AAA] == (T0 + 2 * SECOND, D("10.3"))
    slot.protect_id, slot.was_held = None, True  # its own protection gone unfilled
    host._working = lambda _slot: False
    mover.Mover._flat(host, slot, T0 + 3 * SECOND)
    assert host.sold[AAA] == (T0 + 3 * SECOND, D("10.3"))
    state = host.reentry_states[AAA]
    assert (state.sale_ns, state.sale_price) == (T0 + 2 * SECOND, D("10.3"))


@pytest.mark.parametrize("gone", ["exit_record", "slot"])
def test_a_late_sale_with_no_current_holding_completes_its_own_holdings_totals(gone):
    """xr7-r-a3fix, P2: with no current holding in the name, a late fill of a sale completes the
    totals of the holding that sent it (sold quantity, notional, final fill), as its sale row does,
    and writes no trading state: the re-entry state belongs to a holding the slot still has."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    mover.Mover.on_order_canceled(host, sale_event(sale, T0 + SECOND))
    row = slot.exit_record
    if gone == "slot":
        del host.slots[AAA]
    else:
        slot.exit_record = None
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, T0 + 2 * SECOND, last_qty=Quantity.from_int(5), **sold)
    )
    assert (row.filled, row.notional, row.final_fill_ns) == (D(5), D("51.5"), T0 + 2 * SECOND)
    record = row.record()
    assert (record["filled_quantity"], record["final_fill_ns"]) == ("5", T0 + 2 * SECOND)
    assert not hasattr(host, "sold")  # the Seam has no trading state to write


def test_minutes_between_runs_to_the_entrys_earliest_fill_whatever_order_its_fills_arrive_in():
    """xr7-r-a3fix, P2: an earlier-stamped part fill that arrives second still sets the re-entry
    row's `minutes_between`, from the sale to the entry's earliest own fill."""
    s = strategy()
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    reentry: dict[str, Any] = {"reentry_price": None, "minutes_between": None, "sale_ns": T0}
    entry = {"client_order_id": "E-1", "entry_price": None, "reentry": reentry}
    s.entries_sent.append(entry)
    slot.entry_id, slot.entry_record = ClientOrderId("E-1"), entry
    for qty, px, at in ((2, "10.0000", T0 + 3 * MINUTE), (3, "10.5000", T0 + MINUTE)):
        s.on_order_filled(
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=ClientOrderId("E-1"),
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_int(qty),
                last_px=Price.from_str(px),
                ts_event=at,
            )
        )
    assert (D(str(reentry["reentry_price"])), reentry["minutes_between"]) == (D("10.3"), 1.0)


# --- the batch A head reader (2026-11-13), A-2 and A-3: a close counts from when its minute ended ---


def streamed_minute(opened_ns: int, received_ns: int, close: str, spec: str = "1-MINUTE") -> Bar:
    """An adapter EXTERNAL bar: stamped at its open, received at `received_ns`
    (`canon_alpaca.data.parse_bar`), so its close is the last trade before its interval ended."""
    price = Price.from_str(close)
    low = Price.from_str("9.9500")
    kind = BarType.from_str(f"{AAA}-{spec}-LAST-EXTERNAL")
    return Bar(kind, low, price, low, price, Quantity.from_int(1000), opened_ns, received_ns)


def reclaim_after_a_sale(
    monkeypatch, filled_ns: int, spec: str = "1-MINUTE"
) -> tuple[Any, Any, list]:
    """A reclaim Mover whose sale of AAA at 10.00 filled at `filled_ns`, nothing held or working, its
    last close 9.90, ranked; `_buy` only records that it was asked."""
    s = strategy(reentry_arm="reclaim", run_bars=1, bar_spec=f"{spec}-LAST-EXTERNAL")
    kind = BarType.from_str(f"{AAA}-{spec}-LAST-EXTERNAL")
    slot = mover.Slot(AAA, kind, deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.sold[AAA] = (filled_ns, Decimal("10.00"))
    s.sold_fill_ns[AAA] = filled_ns
    slot.closes.append(Decimal("9.90"))
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: Decimal(0))
    bought: list = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    return s, slot, bought


@pytest.mark.parametrize("minute_ends", ["before", "after"])
def test_a_reentry_close_counts_from_when_its_minute_ended_not_when_it_was_received(
    monkeypatch, minute_ends
):
    """A-2 (P1): the sale filled at 11:10:00.500. The 11:09 minute ended at 11:10:00, before the
    fill, so its close of 10.10, received at 11:10:01.500, is not a close after the sale: no
    re-entry. The 11:10 minute's close, ended after the fill, qualifies."""
    filled = T0 + 10 * MINUTE + SECOND // 2
    s, _slot, bought = reclaim_after_a_sale(monkeypatch, filled)
    opened = T0 + (9 if minute_ends == "before" else 10) * MINUTE
    received = opened + MINUTE + 3 * SECOND // 2
    s.rank_ns, s.rank = received, [(AAA, Decimal("0.25"))]
    s.on_bar(streamed_minute(opened, received, "10.1000"))
    s._enter(received + SECOND // 2, 0)
    if minute_ends == "before":
        assert not bought and any("preceded the sale fill" in why for why in s.dropped), s.dropped
    else:
        assert len(bought) == 1, s.dropped


def test_a_close_whose_minute_ended_before_the_sale_is_not_observed_by_its_reentry_state():
    """A-2 (P1), the state arms' half: the re-entry state observes only complete closes after the
    sale, so the 11:09 close received after an 11:10:00.500 fill is not one of them."""
    s = strategy(reentry_arm="turn", turn_k=2)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE + SECOND // 2
    state = mover.ReentryState(filled, Decimal("10.00"), Decimal("0.10"))
    s.reentry_states[AAA] = state
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 10 * MINUTE + 3 * SECOND // 2, "10.1000"))
    assert state.previous is None
    s.on_bar(streamed_minute(T0 + 10 * MINUTE, T0 + 11 * MINUTE + SECOND, "10.2000"))
    assert state.previous == Decimal("10.2000")


def test_a_run_whose_minute_ends_after_the_final_fill_survives_the_step_that_flattens(monkeypatch):
    """A-3 (P1): the sale filled in full at 11:09:59.500; the 11:09 minute, ended at 11:10:00, closes
    rising and arrives then; the step at 11:10:00.200 finds the name flat. Its run is a close after
    the sale, so the flattening keeps it for the entry check that follows in the same step."""
    s = strategy(reentry_arm="reclaim", run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE - SECOND // 2
    sale = SimpleNamespace(is_closed=True, avg_px=10.0, client_order_id=ClientOrderId("S-1"))
    slot.protect_id, slot.was_held = sale.client_order_id, True
    s.fills.append(("S-1", OrderSide.SELL.name, "5", "10.0000", filled))
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    slot.closes.append(Decimal("9.90"))
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 10 * MINUTE, "10.1000"))
    assert slot.run_ns is not None
    monkeypatch.setattr(s, "_held", lambda _slot: Decimal(0))
    bought: list = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    s.rank_ns, s.rank = T0 + 10 * MINUTE, [(AAA, Decimal("0.25"))]
    step = T0 + 10 * MINUTE + SECOND // 5
    s._flat(slot, step)
    assert s.sold_fill_ns[AAA] == filled
    assert (slot.run_close, slot.run_seen_ns) == (Decimal("10.1000"), T0 + 10 * MINUTE)
    s._enter(step, 0)
    assert len(bought) == 1, s.dropped


def test_a_reentry_close_of_a_longer_bar_counts_from_when_its_interval_ended(monkeypatch):
    """The A-2 fix's high review (should-fix 1): a 5-minute EXTERNAL bar opened at 11:06 ends at
    11:11, so its close, received after that, is after a sale filled at 11:08 and re-enters; a bar's
    end is its open plus its own interval (`canon_alpaca.data.bar_interval_ns`), not a minute."""
    s, _slot, bought = reclaim_after_a_sale(monkeypatch, T0 + 8 * MINUTE, "5-MINUTE")
    received = T0 + 11 * MINUTE + SECOND
    s.rank_ns, s.rank = received, [(AAA, Decimal("0.25"))]
    s.on_bar(streamed_minute(T0 + 6 * MINUTE, received, "10.1000", "5-MINUTE"))
    s._enter(received + SECOND // 2, 0)
    assert len(bought) == 1, s.dropped


def test_the_clock_arms_flattening_clears_a_run_as_before(monkeypatch):
    """The A-3 fix's high review (nit 3): the clock arm makes no fill comparison, so the step that
    finds the name flat clears its run whatever its minute's end; the keep is the state arms' only."""
    s = strategy(reentry_arm="clock", cooldown_secs=0, run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE - SECOND // 2
    sale = SimpleNamespace(is_closed=True, avg_px=10.0, client_order_id=ClientOrderId("S-1"))
    slot.protect_id, slot.was_held = sale.client_order_id, True
    s.fills.append(("S-1", OrderSide.SELL.name, "5", "10.0000", filled))
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    slot.closes.append(Decimal("9.90"))
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 10 * MINUTE, "10.1000"))
    assert slot.run_ns is not None
    s._flat(slot, T0 + 10 * MINUTE + SECOND // 5)
    assert (slot.run_ns, slot.run_close, slot.run_seen_ns, slot.run_closed_ns) == (None,) * 4


# --- w1-r-a2a3 P2/P3: late and repeated intervals, and missing interval evidence ---


def test_delayed_older_intervals_cannot_manufacture_a_turn(monkeypatch):
    filled = T0 + 10 * MINUTE + SECOND // 2
    s = strategy(reentry_arm="turn", turn_k=2, turn_d=D(1))
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.sold[AAA] = (filled, D("10.40"))
    s.sold_fill_ns[AAA] = filled
    s.reentry_states[AAA] = mover.ReentryState(filled, D("10.40"), D("0.10"))
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    received = T0 + 13 * MINUTE + SECOND
    s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
    for offset, (minute, close) in enumerate([(12, "10.0000"), (10, "10.1000"), (11, "10.2000")]):
        s.on_bar(
            streamed_minute(
                T0 + minute * MINUTE,
                received + offset * SECOND // 10,
                close,
            )
        )
    s._enter(received + SECOND // 2, 0)
    assert not bought, "older intervals manufactured two rises after the latest low"


def test_a_delayed_presale_bar_cannot_erase_a_valid_reclaim(monkeypatch):
    filled = T0 + 10 * MINUTE + SECOND // 2
    s, _slot, bought = reclaim_after_a_sale(monkeypatch, filled)
    received = T0 + 11 * MINUTE + SECOND
    s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
    s.on_bar(streamed_minute(T0 + 10 * MINUTE, received, "10.1000"))
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, received + SECOND // 10, "10.2000"))
    s._enter(received + SECOND // 2, 0)
    assert len(bought) == 1, s.dropped


@pytest.mark.parametrize("seen", [None, T0 + 11 * MINUTE + SECOND])
def test_a_reclaim_requires_its_interval_end_even_when_received_after_sale(monkeypatch, seen):
    filled = T0 + 10 * MINUTE + SECOND // 2
    s, slot, bought = reclaim_after_a_sale(monkeypatch, filled)
    now = T0 + 11 * MINUTE + 2 * SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    slot.run_ns = T0 + 9 * MINUTE
    slot.run_close = D("10.1000")
    slot.run_seen_ns = seen
    slot.run_closed_ns = None
    s._enter(now, 0)
    assert not bought, "a missing interval end is not evidence of a post-sale close"
