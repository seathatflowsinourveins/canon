"""R2 opening entries on generated minute tapes through rc8's real order emulator.

Rule: lead: the owner's observation; UNCALIBRATED, no primary source.
The existing test_mover cash venue supplies sizing, fills and protection without a credential.
"""

import json
from collections import deque
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

import pytest
from nautilus_trader.core import UUID4
from nautilus_trader.model import AccountId
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderCanceled
from nautilus_trader.model import OrderExpired
from nautilus_trader.model import OrderRejected
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick

from canon_trading import mover
from canon_trading import session_window
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import FALL
from tests.test_mover import MINUTE
from tests.test_mover import MOVER_ID
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import Recording
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover import tape

D = Decimal
OPEN = int(datetime(2025, 11, 10, 9, 30, tzinfo=mover.NEW_YORK).timestamp()) * SECOND
FALL_TURN = [9.80, 9.50, 9.60, 9.70, 9.80, 9.80]


def price(value):
    # rc8 matching_engine/mod.rs:1904 rejects a quote whose precision differs from the instrument.
    return Price.from_str(f"{D(str(value)):.4f}")


def opening_tape(closes, *, start=OPEN, opened="10", lows=None, volume=100_000):
    data = [
        QuoteTick(
            AAA,
            price(D(opened) - D("0.001")),
            price(D(opened) + D("0.001")),
            Quantity.from_int(10_000),
            Quantity.from_int(10_000),
            start,
            start,
        )
    ]
    for i, value in enumerate(closes):
        close = D(str(value))
        previous = D(str(closes[i - 1])) if i else D(opened)
        low = min(previous, close) if lows is None else D(str(lows[i]))
        end = start + (i + 1) * MINUTE
        data.extend(
            [
                Bar(
                    bar_type(AAA),
                    price(previous),
                    price(max(previous, close)),
                    price(low),
                    price(close),
                    Quantity.from_int(volume),
                    end - MINUTE,
                    end,
                ),
                QuoteTick(
                    AAA,
                    price(close - D("0.001")),
                    price(close + D("0.001")),
                    Quantity.from_int(10_000),
                    Quantity.from_int(10_000),
                    end - 1,
                    end - 1,
                ),
                QuoteTick(
                    AAA,
                    price(close - D("0.001")),
                    price(close + D("0.001")),
                    Quantity.from_int(10_000),
                    Quantity.from_int(10_000),
                    end + SECOND,
                    end + SECOND,
                ),
            ]
        )
    return data


def opening_strategy(*, baseline="1000000", **changes):
    rows = {
        "opening_arm": "on",
        "trail_mode": "tier",
        "run_bars": 50,
        "size_from_budget": True,
        "reentry_arm": "reclaim",  # the source default; main keeps clock
    }
    rows.update(changes)
    if rows.get("entry_arm") == "registered":
        rows["run_bars"] = 3
    s = strategy(**rows)
    if baseline is not None:
        s.baseline_dollar_volume_per_minute[AAA] = D(baseline)
    return s


def simulate(closes=FALL_TURN, *, data=None, **rows):
    eng = engine()
    s = opening_strategy(**rows)
    orders = run(
        eng, s, ranker(rank((AAA, "0.25"))), opening_tape(closes) if data is None else data
    )
    eng.dispose()
    return eng, s, orders


def test_opening_settings_register_only_the_owners_rows_and_forward_to_windows():
    config = mover.MoverConfig(max_notional=D(100), max_move=D(5))
    assert (config.opening_arm, config.opening_minutes, config.opening_f) == ("off", 30, D(1))
    rows = mover.TRAIL_SETTINGS
    for name in ("opening_arm", "opening_minutes", "opening_f"):
        assert rows[name]["status"] == "UNCALIBRATED"
        assert rows[name]["source"] == "lead: the owner's observation, no primary source"
    assert rows["opening_minutes"]["arms"] == (15, 30)
    assert rows["opening_f"]["arms"] == (D(1), D(2))
    window = session_window.WindowConfig(
        strategy="Mover", mover=True, opening_minutes=15, opening_f=D(2)
    )
    assert (window.opening_arm, window.opening_minutes, window.opening_f) == ("off", 15, D(2))
    # the arm itself is refused by a window until the opening reads Alpaca's bars from 09:30


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("opening_arm", "yes"),
        ("opening_minutes", 14),
        ("opening_minutes", 31),
        ("opening_f", D(0)),
        ("opening_f", D(3)),
        ("opening_f", D("NaN")),
    ],
)
def test_opening_settings_refuse_unregistered_values(name, value):
    with pytest.raises(ValueError, match=name):
        strategy(**{name: value})


def test_an_opening_entry_cannot_select_the_unprotected_hold_to_close_row():
    with pytest.raises(ValueError, match="opening_arm.*protect=True"):
        opening_strategy(protect=False)


@pytest.mark.parametrize("arm", ["bridge", "registered"])
def test_a_deep_opening_fall_enters_once_at_the_turn_with_the_existing_protection(arm):
    _, s, orders = simulate(entry_arm=arm)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].status.name == "FILLED"
    assert buys[0].init_event.ts_init == OPEN + 4 * MINUTE
    row = s.record()["entries"][0]
    assert row["arm"] == "opening" and row["reentry"] is None
    opening = row["opening"]
    assert D(opening["open"]) == D(10)
    assert D(opening["low"]) == D("9.5") and opening["low_minute"] == 571
    assert opening["turn_minute"] == 573 and opening["turn_ns"] == OPEN + 4 * MINUTE
    assert (opening["M"], opening["f"], opening["k"]) == (30, "1", 2) and "d" not in opening
    assert D(opening["entry_price"]) == D(str(buys[0].avg_px))
    assert D(opening["fall_in_trail_distances"]) == D("0.5") / D(opening["trail_distance"])
    sales = [o for o in orders if o.side == OrderSide.SELL]
    assert len(sales) == 1 and sales[0].order_type.name == "TRAILING_STOP_LIMIT"
    assert sales[0].quantity == buys[0].quantity and s.triggers
    notes = " ".join(row["notes"])
    assert "lead: the owner's observation" in notes and "UNCALIBRATED" in notes
    assert "no primary source" in notes


@pytest.mark.parametrize("mode", ["tier", "atr", "fixed"])
def test_opening_uses_the_existing_per_name_trail_plan(mode):
    _, s, _ = simulate(trail_mode=mode, atr_n=3)
    row = s.record()["entries"][0]
    opening = row["opening"]
    expected = mover.offsets(D(opening["entry_limit"]), D(row["trail"]), D(row["exit_through"]))[0]
    assert D(opening["trail_distance"]) == expected
    # As every entry records it on this tree: the cold fallback by name, else the bound that set it.
    assert opening["trail_source"] == row["trail_source"] == row["trail_bound"]
    assert row["trail_bound"] in (mode, "spread floor", "0.02 cap")


@pytest.mark.parametrize("mode", ["tier", "atr", "ratchet"])
def test_a_cold_ATR_opening_uses_and_records_the_shared_fixed_fallback(mode):
    _, s, orders = simulate(trail_mode=mode, atr_n=14)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].status.name == "FILLED"
    row = s.record()["entries"][0]
    opening = row["opening"]
    assert row["atr"] is None and D(row["trail"]) == D("0.02")
    assert opening["trail_source"] == row["trail_source"] == "cold_atr_fallback"
    distance = mover.offsets(D(opening["entry_limit"]), D("0.02"), D("0.005"))[0]
    assert D(opening["trail_distance"]) == distance
    assert D(opening["fall_in_trail_distances"]) == D("0.5") / distance
    assert any(o.side == OrderSide.SELL for o in orders)


@pytest.mark.parametrize(("factor", "count"), [(D(1), 1), (D(2), 0)])
def test_the_opening_fall_gate_uses_the_cold_fixed_distance(factor, count):
    _, s, _ = simulate([9.8, 9.7, 9.72, 9.74, 9.74], trail_mode="atr", atr_n=14, opening_f=factor)
    assert len(s.record()["entries"]) == count


class C0BuyReplay(Recording):
    """The four-argument override contract verified in C0 3a8613a's Mover._buy."""

    def _buy(self, slot, move, seen=None, run_close=None):
        return super()._buy(slot, move, seen, run_close)


class C0RefusalReplay(Recording):
    """Keep C0's refusal override signature too; timer errors cannot make this test empty/pass."""

    def _refusal(self, slot, run_close, ranked, recent, now):
        return super()._refusal(slot, run_close, ranked, recent, now)


@pytest.mark.parametrize("replay", [C0BuyReplay, C0RefusalReplay])
@pytest.mark.parametrize("opening", [False, True])
def test_a_C0_compatible_replay_override_actually_enters(replay, opening):
    template = opening_strategy() if opening else strategy()
    s = replay(template.settings)
    s.baseline_dollar_volume_per_minute = dict(template.baseline_dollar_volume_per_minute)
    eng = engine()
    data = opening_tape(FALL_TURN) if opening else tape(AAA, RISING + FALL)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].status.name == "FILLED"
    assert len(s.record()["entries"]) == 1
    if opening:
        assert s.record()["entries"][0]["arm"] == "opening"
    eng.dispose()


@pytest.mark.parametrize(("factor", "count"), [(D(1), 1), (D(2), 0)])
def test_the_fall_must_cover_the_selected_number_of_current_trail_distances(factor, count):
    _, s, _ = simulate([9.98, 9.97, 9.98, 9.99, 10.0], opening_f=factor, atr_n=3)
    assert len(s.record()["entries"]) == count


def test_a_fall_smaller_than_one_distance_has_no_opening_entry():
    _, s, _ = simulate([9.995, 9.99, 10.00, 10.01, 10.02])
    assert s.record()["entries"] == []


@pytest.mark.parametrize(("k", "minute"), [(1, 3), (2, 4), (3, 5)])
def test_opening_reuses_the_registered_turn_k(k, minute):
    _, s, orders = simulate(turn_k=k)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].init_event.ts_init == OPEN + minute * MINUTE
    assert s.record()["entries"][0]["opening"]["k"] == k


def test_the_reentry_turn_depth_does_not_gate_the_opening():
    # Was test_opening_reuses_the_registered_turn_depth: the lead's ruling on finding (3) reverses it.
    _, s, _ = simulate([9.98, 9.97, 9.98, 9.99, 10.0], turn_d=D(2), atr_n=3)
    assert [row["arm"] for row in s.record()["entries"]] == ["opening"]


@pytest.mark.parametrize(("window", "count"), [(15, 0), (30, 1)])
def test_a_turn_after_M_is_not_an_opening_entry(window, count):
    _, s, _ = simulate([9.8] * 14 + FALL_TURN, opening_minutes=window)
    assert len(s.record()["entries"]) == count


@pytest.mark.parametrize("baseline", ["999999", None])
def test_a_name_outside_the_top_tier_has_no_opening_entry(baseline):
    _, s, _ = simulate(baseline=baseline, trail_mode="fixed")
    assert s.record()["entries"] == []


@pytest.mark.parametrize("hour", [4, 8, 16, 20, 23])
@pytest.mark.parametrize("marked_extended", [False, True])
def test_premarket_after_hours_and_overnight_never_trigger(hour, marked_extended):
    start = int(datetime(2025, 11, 10, hour, tzinfo=mover.NEW_YORK).timestamp()) * SECOND
    if marked_extended:  # refused at construction since L4's fix round
        with pytest.raises(ValueError, match="extended"):
            simulate(data=opening_tape(FALL_TURN, start=start), extended_hours=True)
        return
    _, s, _ = simulate(data=opening_tape(FALL_TURN, start=start))
    assert s.record()["entries"] == []


def test_a_late_first_bar_cannot_invent_the_0930_open():
    _, s, _ = simulate(data=opening_tape(FALL_TURN, start=OPEN + MINUTE))
    assert s.record()["entries"] == []


def test_the_opening_window_excludes_a_turn_at_the_exact_M_minute_boundary():
    _, s, _ = simulate([9.8] * 11 + FALL_TURN, opening_minutes=15)
    assert s.record()["entries"] == []


def test_a_gap_in_the_opening_tape_cannot_invent_a_running_low():
    data = opening_tape(FALL_TURN)
    missing = [item for item in data if not isinstance(item, Bar) or item.ts_event != OPEN + MINUTE]
    _, s, _ = simulate(data=missing)
    assert s.record()["entries"] == []
    assert any("missing minute coverage" in why for why in s.dropped)


def test_a_fall_equal_to_one_distance_qualifies():
    closes = [9.99, 9.984, 9.985, 9.986, 9.986]
    lows = [9.99, 9.98, 9.984, 9.985, 9.986]
    _, s, _ = simulate(data=opening_tape(closes, lows=lows), atr_n=3)
    row = s.record()["entries"][0]["opening"]
    assert D(row["fall_in_trail_distances"]) == D(1)


@pytest.mark.parametrize(("cut", "count"), [(D(500_000), 1), (D(2_000_000), 0)])
def test_opening_uses_T1s_selected_top_tier_cut(cut, count):
    _, s, _ = simulate(liquidity_tier_cut=cut, trail_mode="fixed")
    assert len(s.record()["entries"]) == count


def test_the_opening_low_is_the_bar_low_even_when_its_close_is_higher():
    lows = [9.8, 9.4, 9.5, 9.6, 9.7, 9.8]
    _, s, _ = simulate(data=opening_tape(FALL_TURN, lows=lows))
    row = s.record()["entries"][0]["opening"]
    assert D(row["low"]) == D("9.4") and row["low_minute"] == 571
    assert row["turn_minute"] == 573


def test_a_second_opening_turn_after_an_exit_uses_the_reentry_arm():
    closes = FALL_TURN + [9.775, 9.40, 9.30, 9.40, 9.50, 9.60, 9.60]
    _, s, orders = simulate(closes, reentry_arm="turn", atr_n=3)
    assert len([o for o in orders if o.side == OrderSide.BUY]) == 2
    rows = s.record()["entries"]
    assert rows[0]["arm"] == "opening" and rows[1]["reentry"]["arm"] == "turn"
    assert sum(row.get("arm") == "opening" for row in rows) == 1


def test_opening_uses_the_existing_flow_cap():
    _, s, orders = simulate(data=opening_tape(FALL_TURN, volume=1))
    assert orders == [] and s.record()["entries"] == []
    # S2 sizes through `sizing.plan_for_loss`, the flow cap its hard limit: a cap of no share is size_zero
    assert s.record()["sizing_refusals"]["size_zero"] > 0
    assert any("size_zero" in why and "share cap 0," in why for why in s.dropped)


def test_the_flow_cap_can_reduce_an_opening_entry_without_removing_its_protection():
    _, s, orders = simulate(data=opening_tape(FALL_TURN, volume=1000))
    sizing = s.record()["entries"][0]["sizing"]
    assert sizing["binding"] == "flow"
    assert D(sizing["quantity"]) == D(sizing["flow_shares"]) < D(sizing["budget_shares"])
    buy = next(o for o in orders if o.side == OrderSide.BUY)
    sale = next(o for o in orders if o.side == OrderSide.SELL)
    assert buy.status.name == "FILLED" and sale.quantity == buy.quantity


def registered(s):
    """`s`, registered with an engine that has not run: `on_bar` reads the node's cache (L4)."""
    engine().add_strategy(s)
    return s


def observed_strategy(**rows):
    s = registered(opening_strategy(**rows))
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=51), deque(maxlen=5))
    s.slots[AAA] = slot
    for item in opening_tape(FALL_TURN[:4]):
        if isinstance(item, Bar):
            s.on_bar(item)
    return s, slot


def test_opening_still_requires_ranking_a_free_slot_and_the_request_budget():
    s, slot = observed_strategy()
    now = OPEN + 4 * MINUTE
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    ranked = {AAA: (0, D("0.25"))}
    assert slot.run_arm == "opening"
    assert s._refusal(slot, D("9.7"), ranked, 0, now) is None
    unranked = s._refusal(slot, D("9.7"), {}, 0, now)
    assert unranked is not None and "no longer ranked" in unranked
    capped = s._refusal(slot, D("9.7"), ranked, s.settings.soft_line, now)
    assert capped is not None and "soft line" in capped
    s.slots[BBB] = mover.Slot(BBB, bar_type(BBB), deque(), deque(), was_held=True)
    full = s._refusal(slot, D("9.7"), ranked, 0, now)
    assert full is not None and "no slot is free" in full


def test_the_opening_use_survives_slot_removal_and_resets_on_the_next_session():
    s, slot = observed_strategy()
    state = s.opening_states[AAA]
    state.used = True
    del s.slots[AAA]
    s.slots[AAA] = mover.Slot(AAA, bar_type(AAA), deque(maxlen=51), deque(maxlen=5))
    assert s._opening_refusal(slot, OPEN + 5 * MINUTE) == "opening entry already used this session"
    next_open = OPEN + 24 * 60 * MINUTE
    for item in opening_tape(FALL_TURN[:4], start=next_open, opened="11"):
        if isinstance(item, Bar):
            s.on_bar(item)
    assert s.opening_states[AAA].used is False
    assert s.opening_states[AAA].day != state.day and s.opening_states[AAA].open == D(11)


def test_internal_bars_use_the_0930_open_regardless_of_event_timestamp():
    s = registered(opening_strategy())
    kind = BarType.from_str(f"{AAA}-1-MINUTE-LAST-INTERNAL")
    slot = mover.Slot(AAA, kind, deque(maxlen=51), deque(maxlen=5), subscribed_ns=OPEN)
    s.slots[AAA] = slot
    for item in opening_tape(FALL_TURN):
        if isinstance(item, Bar):
            s.on_bar(
                Bar(
                    kind,
                    item.open,
                    item.high,
                    item.low,
                    item.close,
                    item.volume,
                    item.ts_init,
                    item.ts_init,
                )
            )
    state = s.opening_states[AAA]
    assert state.open == D(10) and state.low == D("9.5")
    assert state.low_minute == 571


ADDED_SINCE_P1 = {
    "run": (
        "arming",
        "entries_off",
        "persistence_arms",
        "sell_triggers",
        "sizing_refusals",
        "sizing_bindings",
        "history_requests",  # F-2's warm-up: under the fixed trail none is asked
        "history_request_limit",
        "warm_up",
        "entries_by_trail_source",
        "order_refusals",
        "refusals",
        "remakes_under_floor",
    ),
    "entry": ("profile", "arming", "trail_cap", "spread_floor", "own_measure", "entry_price"),
    "exit": ("sales", "held_stop_kept", "held_stop_from_average"),
    "rows": (
        "profile_k",
        "profile_pctl",
        "profile_floor",
        "profile_multipliers",
        "breach_count",
        "high_count",
        "confirm_gap_ms",
        "trail_cap",
        "planned_loss",
        "loss_share",
    ),
}
"""Record keys later pieces added on purpose (the trail profile's rows, the entry switch, main's
persistence arms, ADR 0119, main's S2 sizing: its exit triggers, refusal and binding counts, cap
arm, spread floor and own measure, and the D3 terms' keys: each refusal's cause, each entry's price and
each exit's sale rows); and the re-flooring ruling's
count of remakes under the floor; each is checked by its own tests and taken out here, so the rest of the record is still compared byte for byte with P1's."""


def as_of_p1(record):
    record = {k: v for k, v in record.items() if k not in ADDED_SINCE_P1["run"]}
    record["rows"] = {k: v for k, v in record["rows"].items() if k not in ADDED_SINCE_P1["rows"]}
    record["entries"] = [
        {k: v for k, v in entry.items() if k not in ADDED_SINCE_P1["entry"]}
        for entry in record["entries"]
    ]
    record["exits"] = [
        {k: v for k, v in row.items() if k not in ADDED_SINCE_P1["exit"]} for row in record["exits"]
    ]
    return record


def legacy_fixture_bytes(**rows):
    eng = engine()
    s = strategy(**rows)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    payload = {
        "record": as_of_p1(s.record()),
        "orders": [
            [o.side.name, o.order_type.name, o.status.name, str(o.price), str(o.quantity)]
            for o in orders
        ],
    }
    eng.dispose()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_opening_off_preserves_the_existing_fixture_byte_for_byte():
    legacy = legacy_fixture_bytes()
    assert (
        sha256(legacy).hexdigest()
        == "e5135a0fac10851ba8c6cb177f95cf0647921ab18c8a680f3171f028ba7f2de7"  # P1 stacked, R2 absent
    )
    assert legacy_fixture_bytes(opening_arm="off") == legacy
    assert legacy_fixture_bytes(opening_arm="off", opening_minutes=15, opening_f=D(2)) == legacy


# --- L4's fix round on the merge with 1a568ec ---------------------------------------------------------


def test_a_slot_opened_after_0930_seeds_its_opening_from_the_cached_bars():
    # The live window starts after 09:30 and its slots open when the ranking names them; the 09:30 bar
    # is then never one of the slot's own. The rank actor's cached bars carry the minutes before.
    eng = engine()
    s = opening_strategy()
    late = ranker(rank(), values={2: rank((AAA, "0.25"))})
    orders = run(eng, s, late, opening_tape(FALL_TURN))
    eng.dispose()
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and s.record()["entries"][0]["arm"] == "opening"
    state = s.opening_states[AAA]
    assert state.open == D(10) and state.low == D("9.5") and state.complete


def test_the_low_minute_names_the_minute_the_turn_is_anchored_to():
    closes = [9.80, 9.60, 9.70, 9.50, 9.60, 9.70]
    lows = [9.80, 9.50, 9.60, 9.50, 9.50, 9.60]
    _, s, _ = simulate(closes, data=opening_tape(closes, lows=lows))
    assert s.opening_states[AAA].low_minute == 573


def test_opening_on_is_refused_with_extended_hours():
    with pytest.raises(ValueError, match="extended"):
        mover.MoverConfig(max_notional=D(100), max_move=D(5), opening_arm="on", extended_hours=True)


def test_an_undecided_opening_run_is_not_handed_to_the_bridge_arm_by_the_next_bar():
    s, slot = observed_strategy()
    assert slot.run_arm == "opening" and slot.run_ns is not None
    fifth = [item for item in opening_tape(FALL_TURN[:5]) if isinstance(item, Bar)][4]
    s.on_bar(fifth)
    assert slot.run_ns is None or slot.run_arm == "opening"


def test_an_opening_entry_of_a_name_sold_the_evening_before_records_its_rule_as_a_reentry():
    # A sale before 20:00 the evening before is the previous trade date: it does not block the opening.
    eng = engine()
    s = opening_strategy()
    sale_ns = OPEN - 14 * 60 * MINUTE  # 19:30 the evening before
    s.sold[AAA] = (sale_ns, D("10.40"))
    s.sold_fill_ns[AAA] = sale_ns
    run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(FALL_TURN))
    eng.dispose()
    entry = s.record()["entries"][0]
    row = entry["reentry"]
    assert entry["arm"] == "opening" and row["rule"] == "opening" and row["fall_from"] == "open"
    assert D(row["fall_in_distances"]) == D(entry["opening"]["fall_in_trail_distances"])
    distance = D(row["distance"])
    assert D(row["rise_in_distances"]) == (D(row["run_close"]) - D("9.5")) / distance
    assert row["secs_since_stop_out"] == pytest.approx((entry["submitted_ns"] - sale_ns) / SECOND)


@pytest.mark.parametrize("before_open", [9 * 60 + 31, 9 * 60 + 29])  # 23:59 and 00:01
def test_a_name_sold_on_the_opening_trade_date_is_refused_the_opening_and_left_to_turn(before_open):
    """The lead (2026-11-12): the opening refuses a name sold on its trade date (20:00 rollover), the
    evening before as well as after midnight, by name; the turn arm still decides it."""
    eng = engine()
    s = opening_strategy(reentry_arm="turn")
    sale_ns = OPEN - before_open * MINUTE
    s.sold[AAA] = (sale_ns, D("10.40"))
    s.sold_fill_ns[AAA] = sale_ns
    s.reentry_states[AAA] = mover.ReentryState(sale_ns, D("10.40"), D("0.05"))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(FALL_TURN))
    eng.dispose()
    record = s.record()
    assert record["opening_refused"] == {str(AAA): "sold_this_trade_date"}
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert buys and all(entry.get("arm") != "opening" for entry in record["entries"])
    assert record["entries"][0]["reentry"]["rule"] == "turn"


@pytest.mark.parametrize("before_open", [9 * 60 + 31, 9 * 60 + 29])  # 23:59 and 00:01
def test_a_name_sold_at_23_59_the_evening_before_does_not_take_the_opening_under_reclaim(
    before_open,
):
    """A calendar-day rule would admit the 23:59 sale to the opening; the trade date refuses both."""
    eng = engine()
    s = opening_strategy()
    sale_ns = OPEN - before_open * MINUTE
    s.sold[AAA] = (sale_ns, D("10.40"))
    s.sold_fill_ns[AAA] = sale_ns
    run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(FALL_TURN))
    eng.dispose()
    record = s.record()
    assert all(entry.get("arm") != "opening" for entry in record["entries"])
    assert record["opening_refused"] == {str(AAA): "sold_this_trade_date"}


def test_a_sold_name_outside_the_top_tier_is_refused_by_its_tier_not_named_as_a_sale_refusal():
    eng = engine()
    s = opening_strategy(baseline="10", reentry_arm="turn")
    sale_ns = OPEN - 60 * MINUTE
    s.sold[AAA] = (sale_ns, D("10.40"))
    s.sold_fill_ns[AAA] = sale_ns
    s.reentry_states[AAA] = mover.ReentryState(sale_ns, D("10.40"), D("0.05"))
    run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(FALL_TURN))
    eng.dispose()
    assert s.record()["opening_refused"] == {}


def test_a_window_refuses_the_opening_arm_at_its_start_until_it_reads_alpacas_bars_from_0930(
    tmp_path,
):
    """The lead's condition on P4's split (2026-11-12): the arm lands off and cannot be switched on
    until the opening is judged on Alpaca's minute bars from 09:30, which this tree does not request.
    The Mover keeps the arm, so its tests still drive it; a paper window refuses it by name."""
    with pytest.raises(ValueError, match="opening_arm 'on' is refused"):
        session_window.WindowConfig(strategy="Mover", mover=True, opening_arm="on")
    args = session_window._args(
        [
            "--session",
            "auto",
            "--regular-strategy",
            "Mover",
            "--mover",
            "--opening-arm",
            "on",
            "--log-dir",
            str(tmp_path),
        ]
    )
    args.strategy = "Mover"
    with pytest.raises(ValueError, match="opening_arm 'on' is refused"):
        session_window.window_config(args, "regular")
    assert (
        mover.MoverConfig(max_notional=D(100), max_move=D(5), opening_arm="on").opening_arm == "on"
    )


def test_opening_defaults_keep_main_clock_reentry_and_extended_windows_refuse_opening():
    assert strategy().settings.reentry_arm == "clock"
    assert session_window.WindowConfig().reentry_arm == "clock"
    with pytest.raises(ValueError, match="opening_arm.*extended"):
        session_window.WindowConfig(
            strategy="Mover", mover=True, session="extended", opening_arm="on"
        )


def test_opening_rows_do_not_move_the_existing_mover_trial_key():
    from dataclasses import replace

    from canon_trading.ledger import config_key
    from canon_trading.ledger import trial_config
    from canon_trading.tuning_contract import BARS_REPLAY_FIXED_ROWS
    from canon_trading.tuning_study import BarsReplayMover
    from tests.test_tuning_study import p0_trial

    cfg = p0_trial()
    original = trial_config([cfg], starting_balances=["100000 USD"])
    assert config_key(original) == (
        "213b37b6d3bba04c68c7c7d5b8bb91b73e74a8476c6f5a46179c18b0eec2cf88"
    )
    assert {name: BARS_REPLAY_FIXED_ROWS[name] for name in mover.OPENING_FIELDS} == {
        "opening_arm": "off",
        "opening_minutes": 30,
        "opening_f": D(1),
    }
    settings = mover.MoverConfig(
        max_notional=cfg.settings.max_notional,
        max_move=cfg.settings.max_move,
        run_bars=cfg.settings.run_bars,
        protect=cfg.settings.protect,
        entry_arm=cfg.settings.entry_arm,
        opening_arm="off",
        opening_minutes=15,
        opening_f=D(2),
    )
    assert (
        trial_config([replace(cfg, settings=settings)], starting_balances=["100000 USD"])
        == original
    )
    replay = BarsReplayMover(settings)
    assert (
        replay.settings.opening_arm,
        replay.settings.opening_minutes,
        replay.settings.opening_f,
    ) == ("off", 30, D(1))
    assert (settings.opening_minutes, settings.opening_f) == (15, D(2))


def test_bars_replay_and_trial_refuse_an_active_opening_by_name():
    from dataclasses import replace

    from canon_trading.ledger import trial_config
    from canon_trading.tuning_study import BarsReplayMover
    from tests.test_tuning_study import p0_trial

    cfg = p0_trial()
    settings = mover.MoverConfig(max_notional=D(1000), max_move=D(1000), opening_arm="on")
    for check in (
        lambda: BarsReplayMover(settings),
        lambda: trial_config([replace(cfg, settings=settings)], starting_balances=["100000 USD"]),
    ):
        with pytest.raises(ValueError, match="opening_arm.*on"):
            check()


def test_an_opening_entry_the_switch_stops_is_recorded_as_the_opening_kind(tmp_path):
    """P8's entry switch after P4: a stopped opening buy is named as the opening, not an entry."""
    path = tmp_path / "arming"
    path.write_text("off\n")
    _, s, orders = simulate(arming_path=str(path))
    assert [o for o in orders if o.side == OrderSide.BUY] == []
    stopped = s.record()["entries_off"]
    assert stopped and {row["kind"] for row in stopped} == {"opening"}


# --- Fix round 1 on P4: the lead's rulings on review findings (8), (2) and (3) -------------------------

SECOND_TURN = [*FALL_TURN, 9.40, 9.30, 9.40, 9.50, 9.60, 9.60]
"""The opening's turn at 09:34, then a fresh low at 09:38 and a second turn at 09:40, both inside M."""


def test_a_denied_opening_order_is_counted_and_leaves_the_opening_available():
    # Finding (8): the risk engine's $1 cap denies every entry. A denied order spends nothing, so each
    # later bar that still has k rising closes from the low is offered the opening again: 09:34 and
    # 09:35 on the first turn, 09:40 and 09:41 on the second. Each failure is a counted outcome.
    eng = engine(order_cap=D(1))
    s = opening_strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(SECOND_TURN))
    eng.dispose()
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert [o.status.name for o in buys] == ["DENIED"] * 4
    assert [o.init_event.ts_init for o in buys] == [OPEN + m * MINUTE for m in (4, 5, 10, 11)]
    assert [row["arm"] for row in s.record()["entries"]] == ["opening"] * 4
    assert s.opening_states[AAA].used is False
    assert s.record()["opening_outcomes"] == {"denied": 4}


def ended(kind, order_id):
    """A real `OrderRejected`, `OrderExpired` or `OrderCanceled` for `order_id` on AAA."""
    ids = (TraderId("TESTER-001"), MOVER_ID, AAA, order_id)
    if kind == "rejected":
        return OrderRejected(*ids, AccountId("ALPACA-001"), "test", UUID4(), OPEN, OPEN, False)
    made = OrderExpired if kind == "expired" else OrderCanceled
    return made(*ids, UUID4(), OPEN, OPEN, False)


@pytest.mark.parametrize("kind", ["rejected", "expired", "canceled"])
def test_an_opening_order_that_ends_unfilled_is_counted_and_frees_the_opening(kind):
    # Finding (8): rejected, expired, or canceled (the entry's time-out) with nothing filled.
    s, slot = observed_strategy()
    order_id = ClientOrderId("O-opening-1")
    state = s.opening_states[AAA]
    state.used, state.order_id = True, order_id
    getattr(s, f"on_order_{kind}")(ended(kind, order_id))
    assert state.used is False and state.order_id is None
    assert s.record()["opening_outcomes"] == {kind: 1}
    assert s._opening_refusal(slot, OPEN + 5 * MINUTE) is None


def test_a_filled_opening_order_spends_the_opening_even_if_its_rest_is_canceled():
    eng = engine()
    s = opening_strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), opening_tape(SECOND_TURN))
    eng.dispose()
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].status.name == "FILLED"
    state = s.opening_states[AAA]
    assert state.used is True and state.order_id is None
    s.on_order_canceled(ended("canceled", buys[0].client_order_id))
    assert state.used is True and s.record()["opening_outcomes"] == {"filled": 1}


def internal_trades(closes):
    """Trades for the data engine to build INTERNAL minute bars: one at the 09:30 open, then one at each
    minute's close, as `test_mover.trade_tape` does."""
    trades = []
    for i, close in enumerate(closes):
        at = OPEN + 1 if i == 0 else OPEN + i * MINUTE - 2
        trades.append(
            TradeTick(
                AAA,
                price(close),
                Quantity.from_int(100_000),
                AggressorSide.BUY,
                TradeId(f"T-{i}"),
                at,
                at,
            )
        )
    return trades


def test_the_opening_turn_is_read_from_the_slots_own_series_only():
    # Finding (2): the slot trades INTERNAL bars, built here from trades as live; the rank actor's
    # EXTERNAL bar for each minute reaches the cache a nanosecond before the INTERNAL bar closes. The
    # EXTERNAL minutes may not stand in for the slot's own, so the turn is the INTERNAL series' turn.
    internal = internal_trades([10.00, *FALL_TURN])
    early = OPEN - 2 * MINUTE  # the ranking opens the slot before 09:30, so its first bar is whole
    quote = QuoteTick(
        AAA,
        price("9.999"),
        price("10.001"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        early,
        early,
    )
    external = [
        Bar(
            item.bar_type,
            item.open,
            item.high,
            item.low,
            item.close,
            item.volume,
            item.ts_event,
            item.ts_init - 1,
        )
        if isinstance(item, Bar)
        else item
        for item in opening_tape(FALL_TURN)
    ]
    eng = engine()
    s = opening_strategy(bar_spec="1-MINUTE-LAST-INTERNAL")
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), [quote, *internal, *external])
    eng.dispose()
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].init_event.ts_init == OPEN + 4 * MINUTE
    opening = s.record()["entries"][0]["opening"]
    assert opening["turn_ns"] == OPEN + 4 * MINUTE and opening["turn_minute"] == 573
    assert D(opening["open"]) == D(10) and D(opening["low"]) == D("9.5")


def test_the_opening_reads_its_own_fall_row_not_the_reentry_turn_depth():
    # Finding (3): a fixed 4% trail makes the fall (0.5 from the 10.00 open) between one and two trail
    # distances. The opening's own row, opening_f=1, admits it; the re-entry row's turn_d=2 would have
    # refused it, since both were checked and the threshold in effect was max(turn_d, opening_f).
    _, s, orders = simulate(trail_mode="fixed", trail=D("0.04"), opening_f=D(1), turn_d=D(2))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].status.name == "FILLED"
    opening = s.record()["entries"][0]["opening"]
    assert 1 <= D(opening["fall_in_trail_distances"]) < 2
    assert "d" not in opening


def test_a_partial_first_internal_minute_cannot_invent_the_0930_open():
    # Finding (2)'s guard: with the opening read from the slot's own cached INTERNAL bars, a slot
    # subscribed after 09:30:00 has only part of the 09:30 minute, which `on_bar` skips too. Its cached
    # partial bar is not the 09:30 open, so the slot has no opening coverage and no opening entry.
    eng = engine()
    s = opening_strategy(bar_spec="1-MINUTE-LAST-INTERNAL")
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), internal_trades([10.00, *FALL_TURN]))
    eng.dispose()
    assert s.slots[AAA].subscribed_ns > OPEN
    assert [o for o in orders if o.side == OrderSide.BUY] == []
    assert AAA not in s.opening_states


@pytest.mark.parametrize(
    ("moves", "why"),
    [
        ({"values": {6: rank((AAA, "6"))}}, "moved 6.0, over the no-chase cap of 5"),
        ({"until_minute": 5}, "the ranking is stale"),
    ],
    ids=["no-chase cap", "stale ranking"],
)
def test_a_later_attempt_after_a_freed_opening_meets_a_fresh_entrys_refusals(moves, why):
    # Finding (8) as built in round 1: a denied opening frees it, and the next attempt is refused by
    # whatever refuses a fresh entry. Here the ranking's move passes the no-chase cap, or the ranking
    # goes stale, after the first turn's two denials and before the second turn (09:40 and 09:41).
    eng = engine(order_cap=D(1))
    s = opening_strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25")), **moves), opening_tape(SECOND_TURN))
    eng.dispose()
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert [o.status.name for o in buys] == ["DENIED"] * 2
    assert [o.init_event.ts_init for o in buys] == [OPEN + m * MINUTE for m in (4, 5)]
    assert s.record()["opening_outcomes"] == {"denied": 2}
    assert s.opening_states[AAA].used is False
    assert [w for w in s.dropped if w.startswith(f"{AAA}: ") and why in w] == [f"{AAA}: {why}"] * 2
