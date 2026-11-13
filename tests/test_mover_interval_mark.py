"""The Mover reads each complete interval once, even when an entry has consumed its run.

Offline bars use the pinned Bar contract and the existing simulated-venue helpers in test_mover;
no network, credential, paper state, catalog or ledger is read or written by these cases.
"""

from collections import deque
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import bar_type
from tests.test_mover import entry_day
from tests.test_mover import strategy
from tests.test_mover_trail import handling
from tests.test_mover_trail import reclaim_after_a_sale
from tests.test_mover_trail import sale_event
from tests.test_mover_trail import sized_cold_holding
from tests.test_mover_trail import streamed_minute

D = Decimal


@pytest.mark.parametrize("entry_arm", ["bridge", "registered"])
@pytest.mark.parametrize("reentry_arm", ["turn", "vwap"])
@pytest.mark.parametrize("minute", [11, 12], ids=["older", "repeated"])
def test_an_unaccepted_interval_changes_no_observation_or_candidate(entry_arm, reentry_arm, minute):
    s = strategy(entry_arm=entry_arm, reentry_arm=reentry_arm, turn_k=2)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(3)
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE + SECOND // 2
    s.sold[AAA] = (filled, D("10.40"))
    s.sold_fill_ns[AAA] = filled
    state = s.reentry_states[AAA] = mover.ReentryState(filled, D("10.40"), D("0.10"))
    s.entry_contexts[AAA] = replace(entry_day([10.0] * 13), reference=8.0)
    received = T0 + 13 * MINUTE + SECOND
    s.session_vwaps[AAA] = {received: D("10.1"), received + SECOND: D("10.1")}
    s.on_bar(streamed_minute(T0 + 12 * MINUTE, received, "10.0000"))
    closes, dollars, observed = list(slot.closes), list(slot.dollars), replace(state)
    context = s.entry_contexts[AAA]
    candidate = (
        slot.run_ns,
        slot.run_close,
        slot.run_seen_ns,
        slot.run_closed_ns,
        slot.run_open_ns,
    )
    live_bars, atr = s.live_bars, slot.atr.value

    s.on_bar(streamed_minute(T0 + minute * MINUTE, received + SECOND, "10.2000"))

    assert list(slot.closes) == closes
    assert list(slot.dollars) == dollars
    assert state == observed
    assert s.entry_contexts[AAA] == context
    assert (
        slot.run_ns,
        slot.run_close,
        slot.run_seen_ns,
        slot.run_closed_ns,
        slot.run_open_ns,
    ) == candidate
    assert s.live_bars == live_bars
    assert slot.atr.value == atr


def test_consuming_a_reclaim_does_not_allow_its_interval_to_be_read_again(monkeypatch):
    filled = T0 + 10 * MINUTE + SECOND // 2
    s, slot, bought = reclaim_after_a_sale(monkeypatch, filled)
    received = T0 + 11 * MINUTE + SECOND
    s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
    s.on_bar(streamed_minute(T0 + 10 * MINUTE, received, "10.1000"))
    s._enter(received, 0)
    assert len(bought) == 1 and slot.run_ns is None

    s.on_bar(streamed_minute(T0 + 10 * MINUTE, received + SECOND, "10.2000"))
    s._enter(received + SECOND, 0)
    assert len(bought) == 1, "a consumed interval was admitted again"
    assert list(slot.closes) == [D("9.90"), D("10.1000")]

    s.on_bar(streamed_minute(T0 + 11 * MINUTE, received + MINUTE, "10.2000"))
    s.rank_ns = received + MINUTE
    s._enter(received + MINUTE, 0)
    assert len(bought) == 2, "the next chronological close must still qualify"


@pytest.mark.parametrize("repeat", [False, True], ids=["in_order", "with_repeat"])
def test_in_order_turn_enters_on_its_second_rise_even_with_a_repeated_interval(monkeypatch, repeat):
    s = strategy(reentry_arm="turn", turn_k=2, turn_d=D(1))
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE + SECOND // 2
    s.sold[AAA] = (filled, D("10.40"))
    s.sold_fill_ns[AAA] = filled
    s.reentry_states[AAA] = mover.ReentryState(filled, D("10.40"), D("0.10"))
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    for minute, close, count in [(10, "10.0000", 0), (11, "10.1000", 0), (12, "10.2000", 1)]:
        received = T0 + (minute + 1) * MINUTE + SECOND
        s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
        s.on_bar(streamed_minute(T0 + minute * MINUTE, received, close))
        s._enter(received, 0)
        assert len(bought) == count
        if repeat and minute == 11:
            s.on_bar(streamed_minute(T0 + minute * MINUTE, received + SECOND, "10.1500"))
            s._enter(received + SECOND, 0)
            assert not bought, "the repeated interval must not replace the second real rise"


@pytest.mark.parametrize("arm", ["reclaim", "turn", "vwap"])
@pytest.mark.parametrize("seen", [None, T0 + 11 * MINUTE + SECOND])
def test_each_state_arm_names_missing_interval_evidence_in_record_refusals(monkeypatch, arm, seen):
    s = strategy(reentry_arm=arm)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE + SECOND // 2
    s.sold[AAA] = (filled, D("10.00"))
    s.sold_fill_ns[AAA] = filled
    s.reentry_states[AAA] = mover.ReentryState(
        filled,
        D("10.00"),
        D("0.10"),
        low=D("9.80"),
        previous=D("10.10"),
        rises=2,
        vwap=D("10.00"),
        crossed_vwap=True,
    )
    now = T0 + 11 * MINUTE + 2 * SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    slot.run_ns, slot.run_close, slot.run_seen_ns = T0 + 9 * MINUTE, D("10.1000"), seen
    s._enter(now, 0)
    assert not bought
    assert slot.run_ns is None
    assert any(
        row["instrument_id"] == str(AAA)
        and row["reason"] == "qualifying close interval end unavailable"
        for row in s.record()["refusals"]
    )


@pytest.mark.parametrize("arm", ["turn", "vwap"])
@pytest.mark.parametrize("fill_first", [True, False])
def test_post_sale_state_keeps_a_close_delivered_before_the_fill_callback(
    monkeypatch, arm, fill_first
):
    """The sr-c-355 P2 reader's case, identical to the owning lane's probe."""
    s = strategy(reentry_arm=arm, turn_k=1, run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    slot.closes.append(D("10.00"))
    sale = SimpleNamespace(
        is_closed=True,
        avg_px=10.5,
        client_order_id=ClientOrderId("S-1"),
    )
    slot.protect_id = sale.client_order_id
    slot.was_held = True
    slot.exit_record = mover.ExitRecord(AAA, "B-1", D(5), trail_distance=D("0.20"))
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))

    filled = T0 + 10 * MINUTE - SECOND // 2
    first_received = T0 + 10 * MINUTE + SECOND // 10
    second_received = T0 + 11 * MINUTE + SECOND // 10
    s.session_vwaps[AAA] = {
        first_received: D("10.20"),
        second_received: D("10.20"),
    }
    fill = SimpleNamespace(
        instrument_id=AAA,
        client_order_id=sale.client_order_id,
        order_side=OrderSide.SELL,
        last_qty=Quantity.from_int(5),
        last_px=Price.from_str("10.5000"),
        ts_event=filled,
    )

    # Both callbacks precede the first step; only their delivery order differs.
    if fill_first:
        s.on_order_filled(fill)
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, first_received, "10.1000"))
    if not fill_first:
        s.on_order_filled(fill)

    step = T0 + 10 * MINUTE + SECOND // 5
    s.rank_ns, s.rank = step, [(AAA, D("0.25"))]
    s._flat(slot, step)
    assert s.sold_fill_ns[AAA] == filled
    s._enter(step, 0)
    assert not bought  # The low / below-VWAP close alone is not an entry.

    s.rank_ns = second_received
    s.on_bar(streamed_minute(T0 + 10 * MINUTE, second_received, "10.3000"))
    s._enter(second_received + SECOND // 10, 0)
    assert len(bought) == 1, s.dropped


@pytest.mark.parametrize("source", ["EXTERNAL", "INTERNAL"])
@pytest.mark.parametrize("interval", ["100-TICK", "100-VOLUME", "100-VALUE", "1-MONTH", "1-YEAR"])
def test_bridge_refuses_bar_specs_without_a_fixed_time_interval_by_name(interval, source):
    bad_spec = f"{interval}-LAST-{source}"
    with pytest.raises(ValueError, match="bar_spec.*fixed time interval") as refused:
        strategy(bar_spec=bad_spec)
    assert bad_spec in str(refused.value)


@pytest.mark.parametrize("source", ["EXTERNAL", "INTERNAL"])
@pytest.mark.parametrize(
    "interval", ["1-MILLISECOND", "1-SECOND", "1-MINUTE", "5-MINUTE", "1-HOUR", "1-DAY", "1-WEEK"]
)
def test_bridge_keeps_fixed_time_bar_specs(interval, source):
    spec = f"{interval}-LAST-{source}"
    assert strategy(bar_spec=spec).settings.bar_spec == spec


def pending_sale(monkeypatch, arm, *, previous_sale=False):
    """An offline current holding whose sale's fill callback has not arrived."""
    s = strategy(reentry_arm=arm, turn_k=2, run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    slot.closes.append(D("10.00"))
    slot.was_held = True
    slot.protect_id = ClientOrderId("S-2")
    slot.exit_record = mover.ExitRecord(AAA, "B-2", D(5), trail_distance=D("0.20"))
    sale = SimpleNamespace(is_closed=True, avg_px=10.5, client_order_id=slot.protect_id)
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    if previous_sale:
        s.sold[AAA] = (T0, D("9.00"))
        s.sold_fill_ns[AAA] = T0
        s.reentry_states[AAA] = mover.ReentryState(
            T0, D("9.00"), D("0.10"), low=D("8.00"), previous=D("8.50"), rises=2
        )
    fill = SimpleNamespace(
        instrument_id=AAA,
        client_order_id=slot.protect_id,
        order_side=OrderSide.SELL,
        last_qty=Quantity.from_int(5),
        last_px=Price.from_str("10.5000"),
        ts_event=T0 + 10 * MINUTE - SECOND // 2,
    )
    return s, slot, fill, bought


@pytest.mark.parametrize("arm", ["turn", "vwap"])
@pytest.mark.parametrize("previous_sale", [False, True])
def test_pending_closes_replay_once_after_the_final_partial_fill(monkeypatch, arm, previous_sale):
    s, slot, fill, bought = pending_sale(monkeypatch, arm, previous_sale=previous_sale)
    old = replace(s.reentry_states[AAA]) if previous_sale else None
    s.on_order_filled(
        SimpleNamespace(
            **{**vars(fill), "last_qty": Quantity.from_int(2), "ts_event": T0 + 8 * MINUTE}
        )
    )
    s.session_vwaps[AAA] = {}
    for minute, close in [(8, "10.0000"), (9, "10.1000"), (10, "10.1500"), (11, "10.3000")]:
        received = T0 + (minute + 1) * MINUTE + SECOND // 10
        s.session_vwaps[AAA][received] = D("10.20")
        s.on_bar(streamed_minute(T0 + minute * MINUTE, received, close))
    # An older or repeated interval must not enter the pending observations either.
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, received + SECOND, "10.0000"))
    s.on_bar(streamed_minute(T0 + 11 * MINUTE, received + 2 * SECOND, "10.0000"))
    assert s.reentry_states.get(AAA) == old
    s.session_vwaps.clear()  # Replay uses each bar's snapshot, never a later tape lookup.
    fill.last_qty = Quantity.from_int(3)
    s.on_order_filled(fill)
    state = s.reentry_states[AAA]
    assert (state.low, state.previous, state.rises) == (D("10.10"), D("10.30"), 2)
    assert state.vwap == D("10.20") and state.crossed_vwap
    observed = replace(state)

    step = received + 3 * SECOND
    s.rank_ns, s.rank = step, [(AAA, D("0.25"))]
    s._flat(slot, step)
    s._flat(slot, step + SECOND)
    assert s.reentry_states[AAA] == observed, "flattening must not replay the closes twice"
    s._enter(step, 0)
    assert len(bought) == 1, s.dropped


@pytest.mark.parametrize("offset", [-SECOND // 2, 0, SECOND // 2], ids=["after", "equal", "before"])
def test_pending_observations_use_the_interval_end_strictly_after_the_final_fill(
    monkeypatch, offset
):
    s, _slot, fill, _bought = pending_sale(monkeypatch, "turn")
    fill.ts_event = T0 + 10 * MINUTE + offset
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 11 * MINUTE, "10.1000"))
    s.on_order_filled(fill)
    assert s.reentry_states[AAA].previous == (D("10.10") if offset < 0 else None)


@pytest.mark.parametrize("next_day", [False, True])
@pytest.mark.parametrize("missing_vwap", [False, True])
def test_pending_vwap_keeps_the_exact_bar_value_and_session_date(
    monkeypatch, next_day, missing_vwap
):
    s, _slot, fill, _bought = pending_sale(monkeypatch, "vwap")
    first_open = T0 + 9 * MINUTE
    second_open = first_open + (24 * 60 * MINUTE if next_day else MINUTE)
    first_received = first_open + MINUTE + SECOND
    second_received = second_open + MINUTE + SECOND
    s.session_vwaps[AAA] = {first_received: D("10.20")}
    if not missing_vwap:
        s.session_vwaps[AAA][second_received] = D("10.20")
    s.on_bar(streamed_minute(first_open, first_received, "10.1000"))
    s.on_bar(streamed_minute(second_open, second_received, "10.3000"))
    s.session_vwaps[AAA] = {first_received: D("9.00"), second_received: D("9.00")}
    s.on_order_filled(fill)
    state = s.reentry_states[AAA]
    assert state.previous == D("10.30")
    assert state.vwap == (None if missing_vwap else D("10.20"))
    assert state.crossed_vwap is (not next_day and not missing_vwap)
    assert state.vwap_day == datetime.fromtimestamp(second_open / SECOND, mover.NEW_YORK).date()


@pytest.mark.parametrize("arm", ["turn", "vwap"])
@pytest.mark.parametrize("lost_after_sale", [False, True])
def test_pending_history_overflow_refuses_only_when_a_post_sale_close_was_lost(
    monkeypatch, arm, lost_after_sale
):
    s, slot, fill, bought = pending_sale(monkeypatch, arm)
    fill.ts_event = T0 if lost_after_sale else T0 + MINUTE
    s.session_vwaps[AAA] = {}
    for minute in range(65):
        close = "10.3000" if minute == 64 else "10.1500" if minute == 63 else "10.1000"
        received = T0 + (minute + 1) * MINUTE + SECOND
        s.session_vwaps[AAA][received] = D("10.20")
        s.on_bar(streamed_minute(T0 + minute * MINUTE, received, close))
    row = slot.exit_record
    s.on_order_filled(fill)
    step = received + SECOND
    s.rank_ns, s.rank = step, [(AAA, D("0.25"))]
    s._flat(slot, step)
    s._enter(step, 0)
    if lost_after_sale:
        assert not bought
        assert any(
            entry["reason"] == "post-sale close history exceeded 64 pending intervals"
            for entry in s.record()["refusals"]
        )
    else:
        assert len(bought) == 1, s.dropped
    assert row is not None
    assert row.pending_reentry_closes.maxlen == 64
    assert not row.pending_reentry_closes, "the fill consumes the bounded history"


def test_reclaim_reentry_row_keeps_the_close_received_before_flattening(monkeypatch):
    """P2b's reclaim control, using the record's existing Decimal string precision."""
    s = strategy(reentry_arm="reclaim", run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(5), trail_distance=D("0.20"))
    s.slots[AAA] = slot
    filled = T0 + 10 * MINUTE - SECOND // 2
    sale = SimpleNamespace(is_closed=True, avg_px=10.0, client_order_id=ClientOrderId("S-1"))
    slot.protect_id, slot.was_held = sale.client_order_id, True
    s.fills.append(("S-1", OrderSide.SELL.name, "5", "10.0000", filled))
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    slot.closes.append(D("9.90"))
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 10 * MINUTE, "10.1000"))
    step = T0 + 10 * MINUTE + SECOND // 5
    s._flat(slot, step)
    order = SimpleNamespace(init_event=SimpleNamespace(ts_init=step))
    row = s._reentry_row(slot, None, slot.run_close, order)
    assert row is not None
    assert (row["low"], row["fall_in_distances"], row["rise_in_distances"]) == (
        "10.1000",
        "-0.50",
        "0.00",
    )


@pytest.mark.parametrize("delivery", ["fill_first", "bar_first", "flat_only"])
@pytest.mark.parametrize("previous_sale", [False, True])
def test_clock_reentry_row_keeps_post_sale_closes_received_before_flattening(
    monkeypatch, delivery, previous_sale
):
    s, slot, fill, _bought = pending_sale(monkeypatch, "clock", previous_sale=previous_sale)
    s.on_bar(streamed_minute(T0 + 8 * MINUTE, T0 + 9 * MINUTE, "10.0000"))
    if delivery == "fill_first":
        s.on_order_filled(fill)
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, T0 + 10 * MINUTE, "10.1000"))
    s.on_bar(streamed_minute(T0 + 10 * MINUTE, T0 + 11 * MINUTE, "10.3000"))
    if delivery == "bar_first":
        s.on_order_filled(fill)
    elif delivery == "flat_only":
        s.fills.append((fill.client_order_id.value, "SELL", "5", "10.5000", fill.ts_event))
    step = T0 + 11 * MINUTE + SECOND // 5
    s._flat(slot, step)
    assert s.sold[AAA] == (step, D("10.5")), "clock cooldown still starts at flattening"
    assert slot.run_ns is None, "clock flattening still clears the pending run"
    observed = replace(s.reentry_states[AAA])
    s._flat(slot, step + SECOND)
    assert s.reentry_states[AAA] == observed

    # The next entry's record must include the earlier low, not just this latest close.
    received = T0 + 12 * MINUTE
    s.on_bar(streamed_minute(T0 + 11 * MINUTE, received, "10.4000"))
    order = SimpleNamespace(init_event=SimpleNamespace(ts_init=received))
    row = s._reentry_row(slot, None, slot.run_close, order)
    assert row is not None
    assert row["sale_ns"] == fill.ts_event
    assert (row["low"], row["fall_in_distances"], row["rise_in_distances"]) == (
        "10.1000",
        "2.00",
        "1.50",
    )


@pytest.mark.parametrize("flatten", [True, False], ids=["before_flat", "already_flat"])
@pytest.mark.parametrize(
    "later_closes",
    [("9.9600",), ("10.2000",), ("9.9600", "10.1000")],
    ids=["falling", "equal", "rebuilding"],
)
def test_a_later_unqualified_bar_clears_the_undecided_post_sale_bridge_run(
    monkeypatch, flatten, later_closes
):
    """sr-a-355 P3: a newer bar replaces the pending decision, including across _flat."""
    wanted = 2 if len(later_closes) == 2 else 1
    s = strategy(reentry_arm="reclaim", run_bars=wanted)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    filled = T0 + 9 * MINUTE - SECOND // 2
    sale = SimpleNamespace(is_closed=True, avg_px=10.0, client_order_id=ClientOrderId("S-1"))
    slot.protect_id, slot.was_held = sale.client_order_id, True
    s.fills.append(("S-1", OrderSide.SELL.name, "5", "10.0000", filled))
    monkeypatch.setattr(s, "_order", lambda cid: sale if cid is not None else None)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    if not flatten:
        s._flat(slot, filled + SECOND // 10)
    slot.closes.extend([D("9.80"), D("9.90")])
    received = T0 + 12 * MINUTE
    s.on_bar(streamed_minute(T0 + 8 * MINUTE, received, "10.2000"))
    assert slot.run_close == D("10.2000")
    for offset, close in enumerate(later_closes, start=1):
        s.on_bar(
            streamed_minute(T0 + (8 + offset) * MINUTE, received + offset * SECOND // 10, close)
        )
    pending = (slot.run_ns, slot.run_close, slot.run_seen_ns, slot.run_closed_ns)
    step = received + SECOND
    s.rank_ns, s.rank = step, [(AAA, D("0.25"))]
    if flatten:
        s._flat(slot, step)
    s._enter(step, 0)
    assert not bought, "a newer unqualified close must invalidate the previous bridge candidate"
    assert pending == (None,) * 4

    # The reset must still admit the next genuine rising run.
    for offset in range(wanted):
        opened = T0 + (9 + len(later_closes) + offset) * MINUTE
        close = "10.3000" if offset == 0 else "10.4000"
        s.on_bar(streamed_minute(opened, received + (offset + 1) * MINUTE, close))
    s.rank_ns = received + wanted * MINUTE
    s._enter(s.rank_ns, 0)
    assert len(bought) == 1, s.dropped


def test_reentry_judges_against_the_holdings_latest_sale_fill_whatever_order_its_fills_arrive_in(
    monkeypatch,
):
    s = strategy(reentry_arm="reclaim", run_bars=1)
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.exit_record = mover.ExitRecord(AAA, "E-1", D(10), trail_distance=D("0.20"))
    s.slots[AAA] = slot
    slot.closes.append(D("9.90"))
    later_fill = T0 + 10 * MINUTE + SECOND // 2
    earlier_fill = T0 + 10 * MINUTE - SECOND // 2
    for at in (later_fill, earlier_fill):
        s.on_order_filled(
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=ClientOrderId("S-1"),
                order_side=OrderSide.SELL,
                last_qty=Quantity.from_int(5),
                last_px=Price.from_str("10.0000"),
                ts_event=at,
            )
        )
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    bought: list = []
    monkeypatch.setattr(s, "_buy", lambda *args: bought.append(args))
    received = T0 + 10 * MINUTE + 3 * SECOND // 2
    s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
    s.on_bar(streamed_minute(T0 + 9 * MINUTE, received, "10.1000"))
    s._enter(received + SECOND // 2, 0)
    assert not bought and any("preceded the sale fill" in why for why in s.dropped), s.dropped
    assert slot.exit_record.final_fill_ns == later_fill
    assert s.sold_fill_ns[AAA] == later_fill
    assert s.reentry_states[AAA].previous is None


@pytest.mark.parametrize("arm", ["turn", "vwap"])
@pytest.mark.parametrize("in_order", [False, True], ids=["out_of_order", "in_order"])
def test_presale_eviction_between_partial_fill_times_keeps_a_valid_reentry(
    monkeypatch, arm, in_order
):
    """r-dsh-intmark P1: no post-sale interval was lost from this 74-bar holding."""
    s, slot, fill, bought = pending_sale(monkeypatch, arm)
    row = slot.exit_record
    assert row is not None
    earlier = T0 + 10 * MINUTE - SECOND // 2
    latest = T0 + 10 * MINUTE + SECOND // 2
    fill.last_qty = Quantity.from_int(2)
    fill.ts_event = earlier if in_order else latest
    s.on_order_filled(fill)
    assert row.final_fill_ns is None
    s.session_vwaps[AAA] = {}
    for minute in range(74):
        close = "10.3000" if minute == 73 else "10.1500" if minute == 72 else "10.1000"
        received = T0 + (minute + 1) * MINUTE + SECOND
        s.session_vwaps[AAA][received] = D("10.20")
        s.on_bar(streamed_minute(T0 + minute * MINUTE, received, close))
    assert row.lost_reentry_close_ns == T0 + 10 * MINUTE
    assert earlier < row.lost_reentry_close_ns < latest
    assert len(row.pending_reentry_closes) == 64

    fill.last_qty = Quantity.from_int(3)
    fill.ts_event = latest if in_order else earlier
    s.on_order_filled(fill)
    step = received + SECOND
    s.rank_ns, s.rank = step, [(AAA, D("0.25"))]
    s._flat(slot, step)
    s._enter(step, 0)
    assert len(bought) == 1, s.record()["refusals"]
    assert row.final_fill_ns == s.sold_fill_ns[AAA] == latest
    state = s.reentry_states[AAA]
    assert state.sale_ns == latest and state.history_complete
    assert state.rises == 2 and state.crossed_vwap
    assert not row.pending_reentry_closes


@pytest.mark.parametrize("gone", [None, "exit_record", "slot"])
@pytest.mark.parametrize("in_order", [False, True], ids=["out_of_order", "in_order"])
def test_sale_order_and_holding_records_end_at_the_latest_partial_fill(gone, in_order):
    """The sale row and its holding keep the same event clock, even after the slot is cleared."""
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    row = slot.exit_record
    row.trigger_ns = T0
    earlier, latest = T0 + SECOND, T0 + 2 * SECOND
    first, second = (earlier, latest) if in_order else (latest, earlier)
    sold = {"order_side": OrderSide.SELL, "last_px": Price.from_str("10.3000")}
    mover.Mover.on_order_filled(
        host, sale_event(sale, first, last_qty=Quantity.from_int(2), **sold)
    )
    assert row.final_fill_ns is None
    if gone == "slot":
        del host.slots[AAA]
    elif gone == "exit_record":
        slot.exit_record = None
    mover.Mover.on_order_filled(
        host, sale_event(sale, second, last_qty=Quantity.from_int(3), **sold)
    )
    record = row.record()
    assert record["final_fill_ns"] == latest
    assert record["trigger_to_final_fill_ms"] == 2000
    (kept,) = record["sales"]
    assert (kept["ended"], kept["ended_ns"], kept["filled_quantity"]) == ("filled", latest, "5")


@pytest.mark.parametrize("late_buy", [False, True])
def test_completion_uses_every_sale_order_applied_to_the_current_holding(late_buy):
    """An old sale's late fill sells the current holding; a late entry adds to that same row."""
    s = strategy(reentry_arm="reclaim")
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    row = slot.exit_record = mover.ExitRecord(AAA, "B-2", D(5), trail_distance=D("0.20"))
    old = s.sale_owner["S-1"] = mover.ExitRecord(AAA, "B-1", D(5))
    earlier, latest = T0 + SECOND, T0 + 2 * SECOND
    fill = SimpleNamespace(
        instrument_id=AAA,
        client_order_id=ClientOrderId("S-1"),
        order_side=OrderSide.SELL,
        last_qty=Quantity.from_int(2),
        last_px=Price.from_str("10.0000"),
        ts_event=latest,
    )
    s.on_order_filled(fill)
    if late_buy:
        s.on_order_filled(
            SimpleNamespace(
                **{
                    **vars(fill),
                    "client_order_id": ClientOrderId("B-2"),
                    "order_side": OrderSide.BUY,
                    "last_qty": Quantity.from_int(5),
                    "ts_event": T0,
                }
            )
        )
    assert row.final_fill_ns is None
    fill.client_order_id = ClientOrderId("S-2")
    fill.last_qty = Quantity.from_int(8 if late_buy else 3)
    fill.ts_event = earlier
    s.on_order_filled(fill)
    assert row.final_fill_ns == latest
    assert s.sold[AAA] == (latest, D("10.00"))
    assert s.sold_fill_ns[AAA] == s.reentry_states[AAA].sale_ns == latest
    assert (old.filled, old.notional, old.final_fill_ns) == (D(0), D(0), None)


@pytest.mark.parametrize("ended", ["canceled", "expired"])
def test_a_completed_sale_row_uses_its_latest_fill_not_its_later_cancel_or_expiry(ended):
    seam, slot = sized_cold_holding("0.20", "0.15")
    host = handling(seam, slot)
    (sale,) = seam.sent
    mover.Mover.on_order_filled(
        host,
        sale_event(
            sale,
            T0 + 2 * SECOND,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(2),
            last_px=Price.from_str("10.3000"),
        ),
    )
    getattr(mover.Mover, f"on_order_{ended}")(host, sale_event(sale, T0 + 3 * SECOND))
    kept = host.sale_rows[sale.client_order_id.value]
    assert (kept["ended"], kept["ended_ns"]) == (ended, T0 + 3 * SECOND)
    mover.Mover.on_order_filled(
        host,
        sale_event(
            sale,
            T0 + SECOND,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(3),
            last_px=Price.from_str("10.3000"),
        ),
    )
    assert (kept["ended"], kept["ended_ns"]) == ("filled", T0 + 2 * SECOND)
