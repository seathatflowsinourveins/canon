"""T1/T2: synthetic minute bars and quotes, using rc8's ATR and real order emulator.

The existing test_mover fixtures provide an offline cash venue; no credential or network is read.
"""

from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import mover
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
