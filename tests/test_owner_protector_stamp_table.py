"""The first-observation guard over a table of host clocks, references, deliveries and phases.

The specification is the lead's guard definition, V1 to V4 (2026-11-13, after the stamp fix's
second P1). Every cell's expected result is derived from it in `expect`, and from nothing else:
- V1: a quote stamped more than the adapter's `STAMP_AHEAD_MOST_NS` (canon_alpaca/data.py:1044)
  ahead of its own receipt is not an observation and is never a reference. It is named.
- V2: arming fixes its level on a sane quote no older than `protect.QUOTE_MOST`, aged from the
  earlier of its stamp and its receipt, and that quote is the reference. With none, it waits named.
- V3: a quote counts only if its venue stamp is strictly greater than the reference's and the last
  counted one's. Receipt order never decides: the same stamp never counts, however late; an older
  stamp never counts; a newer one counts, however late. A stamp V1 refused is the same observation
  when it comes again within the bound, so it never counts either.
- V4: every step names a holding that has no sell yet.

Axes:
- Host clock: level; behind the venue's (every quote stamped ahead of its receipt) or ahead of it,
  each inside and beyond the bound. Host skew moves every quote's stamp against its receipt, so it
  decides V1 and nothing else. Behind beyond the bound, no quote on time is sane: that is the named
  limit (1), and those cells assert the naming at every step (e1).
- The reference: stamped at its receipt, ahead inside the bound, ahead beyond it (each relative to
  the stream), absent (no cached quote at a supplied adoption), or stale (received 120 s before
  the arming, over `QUOTE_MOST`; e2 for a self-armed level).
- The delivery after the reference: the same stamp again, an older stamp (a second before the
  reference's venue time), or a newer stamp (three seconds after it). Each arrives promptly (just
  after the reference), late inside the bound (4 s after its venue time) or late beyond it (7 s).
- The phase whose guard the reference holds: a supplied level the bid is under (the delivery is
  the first observation, guarded), a supplied level the bid is over (the delivery places the
  sell), a self-armed level (the reference is the quote it was planned on; guarded, as for every
  fresh level), or the confirmation (the reference is the guarded observation; the delivery
  confirms the breach).

Invariants, in every cell:
- I1: no sell on one observation, and the guard is spent exactly as V1-V3 say. A same stamp never
  counts; a stamp older than the reference never places a clear level or spends the guard.
- I2: once newer quotes arrive, one a second at the host's skew, the sell goes out by the later of
  the reference's receipt plus the bound (plus the host's lead over the venue, since V1 measures a
  reference against its own receipt) and the delivery's receipt, plus one second for each counting
  quote still owed: the observations, and a reference when none is usable yet (V2's cold case: an
  absent, stale or refused reference). Every step before it, from the arming on, names the holding
  unprotected. A stopped instrument keeps no wait record (e6).

Named cells: F1 (the arming quote re-delivered late after the guard), F2 (a quote older than the
arming quote, late beyond the bound, at a breached supplied level), F3 (the same at a clear level),
e1 (the host behind beyond the bound), e2 (a self-armed level on a stale quote) and e3 (a cached
quote stamped beyond the bound, re-delivered promptly).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from canon_alpaca.data import STAMP_AHEAD_MOST_NS
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import protect
from canon_trading.owner_protector import WAITING
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness

BOUND = STAMP_AHEAD_MOST_NS
STALE = 120 * SECOND
assert STALE > protect.QUOTE_MOST.total_seconds() * SECOND

SKEWS = {
    "level": 0,
    "behind-inside": 2 * SECOND,
    "behind-beyond": BOUND + SECOND,
    "ahead-inside": -2 * SECOND,
    "ahead-beyond": -(BOUND + SECOND),
}
REFERENCES = ("at-receipt", "inside", "beyond", "absent", "stale")
LEAD = {"at-receipt": 0, "inside": 2 * SECOND, "beyond": BOUND + SECOND}
PHASES = ("supplied-breached", "supplied-clear", "self-armed", "confirming")
KINDS = ("same", "older", "newer")
LATENESS = {"prompt": None, "inside": 4 * SECOND, "beyond": 7 * SECOND}
BID = {
    "supplied-breached": "9.99",
    "supplied-clear": "10.00",
    "self-armed": "10.00",
    "confirming": "9.99",
}
NAMED = {
    "level-confirming-at-receipt-older-beyond": "F1",
    "level-supplied-breached-at-receipt-older-beyond": "F2",
    "level-supplied-clear-at-receipt-older-beyond": "F3",
    "level-supplied-breached-beyond-same-prompt": "e3",
}


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def tick(bid: str, ts_event: int, ts_init: int) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(bid),
        Price.from_str(str(Decimal(bid) + Decimal("0.02"))),
        Quantity.from_int(100),
        Quantity.from_int(100),
        ts_event,
        ts_init,
    )


def sane(stamp: int, receipt: int) -> bool:
    """V1."""
    return stamp - receipt <= BOUND


def deliver(strategy: Harness, q: QuoteTick) -> None:
    strategy.cached.latest = q
    strategy.test_clock.set_time(max(q.ts_init, strategy.test_clock.timestamp_ns()))
    strategy.on_quote(q)


def step_named(strategy: Harness, at: int) -> None:
    """One timer step; with no sell yet, it must name the holding unprotected (V4, I2)."""
    before = len(strategy.said)
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace()))
    if not strategy.sent:
        said = strategy.said[before:]
        assert any(line.startswith("AAA.ALPACA: unprotected") for line in said), (
            f"a step at +{(at - T0) / SECOND:.2f} s passed in silence with no sell"
        )


def cases() -> list[Any]:
    out = []
    for skew in SKEWS:
        for phase in PHASES:
            for reference in REFERENCES:
                if reference == "absent" and not phase.startswith("supplied"):
                    continue  # self-arming plans on a quote; a confirmation follows one
                if reference == "stale" and phase == "confirming":
                    continue  # the guarded observation is a delivered quote, never a cached one
                for kind in KINDS:
                    if reference == "absent" and kind != "newer":
                        continue  # no stamp to repeat or precede
                    for late in LATENESS:
                        if reference == "stale" and kind != "newer" and late != "beyond":
                            continue  # a stale stamp comes again only long after it was made
                        cell = f"{skew}-{phase}-{reference}-{kind}-{late}"
                        name = NAMED.get(cell, "")
                        if skew == "behind-beyond":
                            name = "e1"
                        elif phase == "self-armed" and reference == "stale":
                            name = "e2"
                        out.append(
                            pytest.param(
                                skew,
                                phase,
                                reference,
                                kind,
                                late,
                                id=f"{cell}-{name}" if name else cell,
                            )
                        )
    return out


def arm(strategy: Harness, phase: str, skew: int, reference: str) -> dict[str, Any]:
    """Arm the phase. Returns the base B (the reference's receipt, or the arming for a stale or
    absent one), the reference's stamp S and receipt, and the guarded quote G for a confirmation."""
    lead = LEAD.get(reference)
    if phase.startswith("supplied"):
        if reference == "absent":
            strategy.cached.latest = None
            stamp = receipt = None
        elif reference == "stale":
            receipt = T0 - STALE
            stamp = receipt + skew
        else:
            receipt = T0
            stamp = receipt + skew + cast(int, lead)
        if stamp is not None and receipt is not None:
            strategy.cached.latest = tick(BID[phase], stamp, receipt)
        strategy.adopt_holding(
            AAA, Price.from_str("10.00" if phase.endswith("breached") else "9.90")
        )
        return {"B": T0, "S": stamp, "R": receipt, "G": None}
    if phase == "self-armed":
        if reference == "stale":
            receipt = T0 - STALE
            stamp = receipt + skew
            strategy.cached.latest = tick(BID[phase], stamp, receipt)
            strategy.on_start()
            return {"B": T0, "S": stamp, "R": receipt, "G": None}
        strategy.cached.latest = None
        strategy.on_start()
        receipt = T0 + SECOND
        stamp = receipt + skew + cast(int, lead)
        deliver(strategy, tick(BID[phase], stamp, receipt))
        return {"B": receipt, "S": stamp, "R": receipt, "G": None}
    strategy.cached.latest = tick("10.00", T0 + skew, T0)  # the arming quote, Q0
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    receipt = T0 + SECOND
    stamp = receipt + skew + cast(int, lead)
    deliver(strategy, tick(BID[phase], stamp, receipt))  # the guarded observation, G
    return {"B": receipt, "S": stamp, "R": receipt, "G": (stamp, receipt), "Q0": T0 + skew}


def delivery(kind: str, late: str, skew: int, ref: dict[str, Any]) -> tuple[int, int]:
    """The delivery's stamp and receipt. Its lateness is its receipt less its venue time."""
    base, stamp_ref = ref["B"], ref["S"]
    venue_ref = ref["R"] if ref["R"] is not None else base  # the reference's own venue time
    stamp = {
        "same": stamp_ref,
        "older": min(
            cast(int, stamp_ref) if stamp_ref is not None else base + skew, venue_ref + skew
        )
        - SECOND,
        "newer": venue_ref + skew + 3 * SECOND
        if ref["R"] is not None and ref["R"] >= T0
        else base + skew + 3 * SECOND,
    }[kind]
    lateness = LATENESS[late]
    if lateness is None:
        receipt = base + (3 * SECOND if kind == "newer" else SECOND)
    else:
        receipt = stamp - skew + lateness
    return stamp, max(receipt, base + SECOND)


def expect(phase: str, ref: dict[str, Any], d: tuple[int, int]) -> dict[str, Any]:
    """V1-V3, and V2's cold case, applied to this cell, in the order the quotes arrive."""
    most = int(protect.QUOTE_MOST.total_seconds()) * SECOND
    refused: set[int] = set()
    reference: int | None = None
    last: int | None = None

    def usable(stamp: int, receipt: int, now: int) -> bool:  # V1 and V2
        return sane(stamp, receipt) and stamp not in refused and now - min(stamp, receipt) <= most

    def see(stamp: int, receipt: int) -> None:
        if not sane(stamp, receipt):
            refused.add(stamp)

    stamp_ref, receipt_ref = ref["S"], ref["R"]
    if phase == "confirming":
        see(ref["Q0"], T0)
        if usable(ref["Q0"], T0, T0):
            reference = ref["Q0"]
        see(stamp_ref, receipt_ref)
        if sane(stamp_ref, receipt_ref):
            if reference is None:
                reference = stamp_ref  # V2: the first sane quote after the arming
            elif stamp_ref > reference:
                last = stamp_ref  # the guarded observation
    elif stamp_ref is not None:
        see(stamp_ref, receipt_ref)
        if usable(stamp_ref, receipt_ref, ref["B"]):
            reference = stamp_ref
    planned = phase != "self-armed" or reference is not None
    stamp, receipt = d
    counted = became = False
    see(stamp, receipt)
    if reference is None:
        if usable(stamp, receipt, receipt):
            reference, became = stamp, True  # V2: arms a self-armed level, or is the reference
    elif sane(stamp, receipt) and stamp not in refused:
        counted = stamp > max(reference, last if last is not None else reference)
    observations = (1 if phase == "supplied-clear" else 2) - (last is not None) - counted
    return {
        "planned": planned,
        "counted": counted,
        "became": became,
        "sent": counted and observations == 0,
        "needed": observations + (reference is None),
    }


@pytest.mark.parametrize(("skew", "phase", "reference", "kind", "late"), cases())
def test_the_guard_over_the_table(
    skew: str, phase: str, reference: str, kind: str, late: str
) -> None:
    offset = SKEWS[skew]
    strategy = Harness()
    ref = arm(strategy, phase, offset, reference)
    assert not strategy.sent, "I1: a sell went out on the reference itself"
    stamp, receipt = delivery(kind, late, offset, ref)
    want = expect(phase, ref, (stamp, receipt))
    if phase == "self-armed":
        planned = strategy.slots[AAA].level is not None
        assert planned == want["planned"], "V1/V2: arming on the reference"
    bid = BID[phase]
    step_named(strategy, ref["B"] + SECOND // 4)  # I2: the step before the delivery is named too
    before = strategy.slots[AAA].last_observation
    deliver(strategy, tick(bid, stamp, receipt))

    # I1: the delivery spends the guard, or sells, exactly as V1-V3 say; never one observation.
    slot = strategy.slots[AAA]
    assert bool(strategy.sent) or slot.last_observation != before or not want["counted"], (
        "I1: a quote that counts was not counted"
    )
    assert (bool(strategy.sent) or slot.last_observation != before) == want["counted"], (
        "I1: the guard's spending"
    )
    assert bool(strategy.sent) == want["sent"], "I1: the sell"
    if want["became"]:
        assert slot.armed_event == stamp, "V2: the first sane quote is the reference"

    # I2: newer quotes at the host's skew; every step before the sell is named.
    step_named(strategy, receipt + SECOND // 4)
    start = (receipt - ref["B"]) // SECOND + 1
    host_ahead = max(0, -offset)
    deadline = max(ref["B"] + BOUND + host_ahead, receipt) + want["needed"] * SECOND
    sent_at = receipt if strategy.sent else None
    for n in range(start, start + 16):
        at = ref["B"] + n * SECOND
        deliver(strategy, tick(bid, at + offset, at))
        if strategy.sent and sent_at is None:
            sent_at = at
        step_named(strategy, at + SECOND // 4)
    if skew == "behind-beyond":
        # Named limit (1): no quote on time is sane, so nothing more is placed, and it is said.
        assert len(strategy.sent) == int(want["sent"]), "e1: placed on a quote V1 refuses"
        if not strategy.sent:
            record = strategy.frozen[AAA]
            assert record.startswith(WAITING) and "ahead of its receipt" in record, (
                f"e1: the limit is not named: {record}"
            )
    else:
        assert len(strategy.sent) == 1, "I2: no sell, or more than one"
        assert sent_at is not None and sent_at <= deadline, (
            f"I2: sold at +{(cast(int, sent_at) - ref['B']) / SECOND:.0f} s, "
            f"past +{(deadline - ref['B']) / SECOND:.0f} s"
        )
    strategy.stop_instrument(AAA)
    assert not strategy.frozen.get(AAA, "").startswith(WAITING), "e6: a stopped wait record"
