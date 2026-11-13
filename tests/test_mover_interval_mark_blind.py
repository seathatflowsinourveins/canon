"""Blind R1-R3 tests of Mover's interval acceptance contract at base 4e2dc0bb.

Uses Mover.on_bar, Slot, ReentryState, record, and the existing offline entry seam from
tests/test_mover_trail.py. Bars follow that file's streamed_minute contract: EXTERNAL bars
stamp their open at ts_event and their receipt at ts_init; INTERNAL minute bars end at
ts_init. Calls arrive in the order written here, with no engine sorting in between.
"""

from collections import deque
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import entry_day
from tests.test_mover import strategy

D = Decimal
ARMS = ("clock", "reclaim", "turn", "vwap")
STATE_ARMS = ("reclaim", "turn", "vwap")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared fixture so these offline cases never inspect a credential."""


def _bar(
    opened: int,
    received: int,
    close: str,
    *,
    instrument: InstrumentId = AAA,
    spec: str = "1-MINUTE-LAST-EXTERNAL",
) -> Bar:
    price = Price.from_str(close)
    kind = BarType.from_str(f"{instrument}-{spec}")
    event = received if kind.is_internally_aggregated() else opened
    return Bar(kind, price, price, price, price, Quantity.from_int(1000), event, received)


def _after_sale(
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    *,
    entry_arm: str = "bridge",
    spec: str = "1-MINUTE-LAST-EXTERNAL",
) -> tuple[Recording, mover.Slot, list[tuple[Any, ...]]]:
    s = strategy(
        reentry_arm=arm,
        entry_arm=entry_arm,
        run_bars=1 if entry_arm == "bridge" else 3,
        turn_k=2,
        cooldown_secs=0,
        bar_spec=spec,
    )
    slot = mover.Slot(AAA, BarType.from_str(f"{AAA}-{spec}"), deque(maxlen=4), deque(maxlen=5))
    s.slots[AAA] = slot
    s.sold[AAA] = (T0 - SECOND, D("10.00"))
    s.sold_fill_ns[AAA] = T0 - SECOND
    s.reentry_states[AAA] = mover.ReentryState(T0 - SECOND, D("10.00"), D("0.10"))
    s.entry_contexts[AAA] = replace(entry_day([], 1000), bars=(), reference=8.0)
    monkeypatch.setattr(s, "_working", lambda _slot: False)
    monkeypatch.setattr(s, "_held", lambda _slot: D(0))
    buys: list[tuple[Any, ...]] = []
    monkeypatch.setattr(s, "_buy", lambda *args: buys.append(args))
    return s, slot, buys


def _pending(slot: mover.Slot) -> tuple[Any, ...]:
    return (
        slot.run_open_ns,
        slot.run_ns,
        slot.run_close,
        slot.run_seen_ns,
        slot.run_closed_ns,
        slot.run_arm,
    )


def _snapshot(s: Recording, slot: mover.Slot) -> dict[str, Any]:
    """Copies ensure an in-place mutation cannot also rewrite the expected value."""
    name = slot.instrument_id
    return deepcopy(
        {
            "closes": tuple(slot.closes),
            "turn_and_vwap": s.reentry_states[name],
            "context": s.entry_contexts[name],
            "pending_run": _pending(slot),
        }
    )


def _feed_rises(s: Recording, *, spec: str = "1-MINUTE-LAST-EXTERNAL") -> Bar:
    latest = _bar(T0, T0 + MINUTE, "9.8000", spec=spec)
    for minute, close in enumerate(("9.8000", "9.9000", "10.0000", "10.1000")):
        received = T0 + (minute + 1) * MINUTE
        s.session_vwaps.setdefault(AAA, {})[received] = D("10.05")
        latest = _bar(T0 + minute * MINUTE, received, close, spec=spec)
        s.on_bar(latest)
    return latest


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("entry_arm", ("bridge", "registered"))
@pytest.mark.parametrize("delivery", ("duplicate", "corrected_duplicate", "older"))
def test_r1_stale_external_interval_changes_no_reentry_input(
    monkeypatch: pytest.MonkeyPatch, arm: str, entry_arm: str, delivery: str
) -> None:
    s, slot, _buys = _after_sale(monkeypatch, arm, entry_arm=entry_arm)
    latest = _feed_rises(s)
    assert slot.run_ns is not None  # the rejected bar must preserve a real pending run
    assert s.reentry_states[AAA].crossed_vwap
    before = _snapshot(s, slot)
    if delivery == "duplicate":
        stale = latest
    else:
        minute = 3 if delivery == "corrected_duplicate" else 1
        close = "9.7000" if delivery == "corrected_duplicate" else "10.2000"
        # A later receipt does not make this an interval newer than minute 3's.
        stale = _bar(T0 + minute * MINUTE, T0 + 10 * MINUTE + SECOND, close)
    s.on_bar(stale)
    assert _snapshot(s, slot) == before


@pytest.mark.parametrize("arm", ARMS)
def test_r1_stale_bar_cannot_recreate_a_run_after_its_entry_decision(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    s, slot, buys = _after_sale(monkeypatch, arm)
    _feed_rises(s)
    now = T0 + 4 * MINUTE + SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    s._enter(now, 0)
    assert len(buys) == 1
    assert (slot.run_ns, slot.run_close, slot.run_seen_ns, slot.run_closed_ns) == (None,) * 4
    before = _snapshot(s, slot)
    s.on_bar(_bar(T0 + MINUTE, T0 + 5 * MINUTE + SECOND, "10.2000"))
    assert _snapshot(s, slot) == before


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("minute", (1, 3), ids=("older", "duplicate_end"))
def test_r1_internal_interval_changes_no_reentry_input(
    monkeypatch: pytest.MonkeyPatch, arm: str, minute: int
) -> None:
    spec = "1-MINUTE-LAST-INTERNAL"
    s, slot, _buys = _after_sale(monkeypatch, arm, entry_arm="registered", spec=spec)
    _feed_rises(s, spec=spec)
    assert slot.run_ns is not None
    before = _snapshot(s, slot)
    # Internal bars use ts_init for the interval end, even when delivered out of order.
    s.on_bar(_bar(T0 + minute * MINUTE, T0 + (minute + 1) * MINUTE, "10.2000", spec=spec))
    assert _snapshot(s, slot) == before


@pytest.mark.parametrize("arm", STATE_ARMS)
@pytest.mark.parametrize("receipt_present", (True, False), ids=("receipt", "no_receipt"))
def test_r2_missing_interval_end_is_named_in_refusals_and_never_buys(
    monkeypatch: pytest.MonkeyPatch, arm: str, receipt_present: bool
) -> None:
    s, slot, buys = _after_sale(monkeypatch, arm)
    _feed_rises(s)
    assert slot.run_closed_ns == T0 + 4 * MINUTE
    slot.run_closed_ns = None
    if not receipt_present:
        slot.run_seen_ns = None
    now = T0 + 4 * MINUTE + SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    s._enter(now, 0)
    refusals = s.record()["refusals"]
    reasons = [
        str(row["reason"]).lower() for row in refusals if row.get("instrument_id") == str(AAA)
    ]
    # The property requires a named absence, not a particular new reason-code spelling.
    named = any(
        ("interval" in reason or "close" in reason)
        and ("end" in reason or "closed" in reason)
        and any(word in reason for word in ("missing", "unavailable", "unknown", "absent", "no "))
        for reason in reasons
    )
    assert (len(buys), named) == (0, True), {"buy_calls": len(buys), "refusals": refusals}


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("entry_arm", ("bridge", "registered"))
def test_r3_in_order_bars_keep_the_base_state_context_and_entry(
    monkeypatch: pytest.MonkeyPatch, arm: str, entry_arm: str
) -> None:
    s, slot, buys = _after_sale(monkeypatch, arm, entry_arm=entry_arm)
    original_context = deepcopy(s.entry_contexts[AAA])
    _feed_rises(s)
    assert tuple(slot.closes) == tuple(map(D, ("9.8", "9.9", "10.0", "10.1")))
    state = s.reentry_states[AAA]
    assert (state.low, state.previous, state.rises) == (D("9.8"), D("10.1"), 3)
    assert (state.vwap, state.below_vwap, state.crossed_vwap) == (D("10.05"), False, True)
    assert _pending(slot) == (
        T0 + 3 * MINUTE,
        T0 + 3 * MINUTE,
        D("10.1"),
        T0 + 4 * MINUTE,
        T0 + 4 * MINUTE,
        None,
    )
    context = s.entry_contexts[AAA]
    if entry_arm == "registered":
        assert tuple(bar.close for bar in context.bars) == (9.8, 9.9, 10.0, 10.1)
        assert context.covered_to == context.bars[-1].start + 1
    else:
        assert context == original_context
    now = T0 + 4 * MINUTE + SECOND
    s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
    s._enter(now, 0)
    assert buys == [(slot, D("0.25"), T0 + 4 * MINUTE, D("10.1"))]
    assert s.record()["refusals"] == []


@pytest.mark.parametrize("arm", ARMS)
def test_r3_interval_acceptance_is_per_slot(monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    s, first, _buys = _after_sale(monkeypatch, arm)
    _feed_rises(s)  # AAA has accepted through minute 4 before BBB receives minute 1.
    before = _snapshot(s, first)
    second = mover.Slot(
        BBB, BarType.from_str(f"{BBB}-1-MINUTE-LAST-EXTERNAL"), deque(maxlen=4), deque(maxlen=5)
    )
    s.slots[BBB] = second
    s.sold[BBB] = (T0 - SECOND, D("10.00"))
    s.sold_fill_ns[BBB] = T0 - SECOND
    s.reentry_states[BBB] = mover.ReentryState(T0 - SECOND, D("10.00"), D("0.10"))
    second.closes.append(D("9.80"))
    s.on_bar(_bar(T0, T0 + 6 * MINUTE, "10.1000", instrument=BBB))
    assert tuple(second.closes) == (D("9.80"), D("10.1000"))
    assert s.reentry_states[BBB].previous == D("10.1000")
    assert second.run_closed_ns == T0 + MINUTE
    assert _snapshot(s, first) == before


def test_r3_in_order_five_minute_bar_keeps_its_own_interval_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = "5-MINUTE-LAST-EXTERNAL"
    s, slot, buys = _after_sale(monkeypatch, "reclaim", spec=spec)
    filled = T0 + 8 * MINUTE
    s.sold[AAA] = (filled, D("10.00"))
    s.sold_fill_ns[AAA] = filled
    s.reentry_states[AAA] = mover.ReentryState(filled, D("10.00"), D("0.10"))
    slot.closes.append(D("9.90"))
    received = T0 + 11 * MINUTE + SECOND
    s.on_bar(_bar(T0 + 6 * MINUTE, received, "10.1000", spec=spec))
    assert slot.run_closed_ns == T0 + 11 * MINUTE
    s.rank_ns, s.rank = received, [(AAA, D("0.25"))]
    s._enter(received, 0)
    assert buys == [(slot, D("0.25"), received, D("10.1000"))]
