"""The first-observation guard over a table of clocks, reference stamps and deliveries.

The lead's condition on the stamp fix (2026-11-13, after the (e) review's P1-1): one table over the
grid, not only the scratch cases, and it is the guard's specification ("the next reviewer argues
with the table").

Axes:
- Host skew: the node's clock behind Alpaca's (every quote stamped ahead of its receipt), level, or
  ahead (stamped behind its receipt), each inside and beyond the adapter's 5 s
  `STAMP_AHEAD_MOST_NS` (canon_alpaca/data.py:1044).
- The reference quote's stamp, as its lead over the stream's own skew: at its receipt, ahead inside
  the bound, ahead beyond it, or absent (no quote cached at a supplied adoption).
- The delivery after the reference: the same quote again (its stamp, a later receipt), a late older
  quote (stamped a second before the reference was observed, received after it), or a newer quote.
- The phase whose guard the reference holds: a supplied level the bid is under (the delivery is the
  first observation, guarded), a supplied level the bid is over (the delivery places the sell), a
  self-armed level (the reference is the quote it was planned on; the delivery is the first
  observation, guarded, as for every fresh level), or the confirmation (the reference is the guarded
  observation; the delivery confirms the breach and sells).

Invariants:
- I1: the guard is spent (a first observation counted, or a sell sent) only by a quote received
  after the arming whose venue stamp is newer than the reference's. A reference stamped beyond the
  bound has no usable time, so the receipts order it (the lead's stall ruling); the quote received
  after it is the newer one.
- I2: when newer quotes arrive (one a second, at the stream's skew), the holding is not left waiting
  past the reference's receipt plus the bound plus two one-second observations, and every step
  before the sell names the holding unprotected.

The remaining limit is the xfail cell: a late older quote after a reference stamped beyond the bound
is ordered by its receipt, so it counts as newer.
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

from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness

SKEWS = {
    "behind-beyond": STAMP_AHEAD_MOST_NS + SECOND,
    "behind-inside": 2 * SECOND,
    "level": 0,
    "ahead-inside": -2 * SECOND,
    "ahead-beyond": -(STAMP_AHEAD_MOST_NS + SECOND),
}
REFERENCES = {
    "at-receipt": 0,
    "inside": 2 * SECOND,
    "beyond": STAMP_AHEAD_MOST_NS + SECOND,
    "absent": None,
}
PHASES = ("supplied-breached", "supplied-clear", "self-armed", "confirming")
DELIVERIES = ("same", "late-older", "newer")
BID = {
    "supplied-breached": "9.99",
    "supplied-clear": "10.00",
    "self-armed": "10.00",
    "confirming": "9.99",
}
LIMIT = (
    "Named limit (the lead, 2026-11-13): a reference stamped beyond the bound has no usable time, "
    "so receipts order it, and a late older quote received after it counts as newer."
)


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


def deliver(strategy: Harness, q: QuoteTick) -> None:
    strategy.cached.latest = q
    strategy.test_clock.set_time(max(q.ts_init, strategy.test_clock.timestamp_ns()))
    strategy.on_quote(q)


def step_named(strategy: Harness, at: int) -> None:
    """One timer step; with no sell yet, it must name the holding unprotected (I2)."""
    before = len(strategy.said)
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace()))
    if not strategy.sent:
        said = strategy.said[before:]
        assert any(line.startswith("AAA.ALPACA: unprotected") for line in said), (
            f"a step at +{(at - T0) / SECOND:.2f} s passed in silence with no sell"
        )


def spent(strategy: Harness, phase: str) -> bool:
    if phase in ("supplied-breached", "self-armed"):  # a self-armed level is guarded too
        assert not strategy.sent, "one observation elected"
        return strategy.slots[AAA].last_observation is not None
    return bool(strategy.sent)


def cases() -> list[Any]:
    out = []
    for skew in SKEWS:
        for phase in PHASES:
            for reference, lead in REFERENCES.items():
                if lead is None and not phase.startswith("supplied"):
                    continue  # self-arming plans on a quote; a confirmation follows one
                for delivery in DELIVERIES if lead is not None else ("newer",):
                    limit = reference == "beyond" and delivery == "late-older"
                    marks = [pytest.mark.xfail(strict=True, reason=LIMIT)] if limit else []
                    out.append(
                        pytest.param(
                            skew,
                            phase,
                            reference,
                            delivery,
                            id=f"{skew}-{phase}-{reference}-{delivery}",
                            marks=marks,
                        )
                    )
    return out


def arm(strategy: Harness, phase: str, skew: int, lead: int | None) -> tuple[int, int | None]:
    """Arm the phase and return the reference's receipt and stamp (no stamp when absent)."""
    if phase.startswith("supplied"):
        receipt = T0  # the adoption's clock: armed_at
        stamp = None if lead is None else receipt + skew + lead
        strategy.cached.latest = None if stamp is None else tick(BID[phase], stamp, receipt)
        strategy.adopt_holding(
            AAA, Price.from_str("10.00" if phase.endswith("breached") else "9.90")
        )
    elif phase == "self-armed":
        strategy.cached.latest = None
        strategy.on_start()
        receipt, stamp = T0 + SECOND, T0 + SECOND + skew + cast(int, lead)
        deliver(strategy, tick(BID[phase], stamp, receipt))
        assert strategy.slots[AAA].level is not None, "no level planned on the reference"
    else:
        strategy.cached.latest = tick("10.00", T0 + skew, T0)
        strategy.adopt_holding(AAA, Price.from_str("10.00"))
        receipt, stamp = T0 + SECOND, T0 + SECOND + skew + cast(int, lead)
        deliver(strategy, tick(BID[phase], stamp, receipt))  # the guarded observation
        assert strategy.slots[AAA].last_observation == (stamp, receipt), "the reference not guarded"
    assert not strategy.sent, "a sell went out on the reference itself"
    if phase != "confirming":
        assert strategy.slots[AAA].last_observation is None, "the reference spent the guard"
    return receipt, stamp


@pytest.mark.parametrize(("skew", "phase", "reference", "delivery"), cases())
def test_the_guard_over_the_table(skew: str, phase: str, reference: str, delivery: str) -> None:
    offset, lead = SKEWS[skew], REFERENCES[reference]
    strategy = Harness()
    receipt, stamp = arm(strategy, phase, offset, lead)
    bid = BID[phase]
    if delivery == "same":
        q = tick(bid, cast(int, stamp), receipt + SECOND)
    elif delivery == "late-older":
        q = tick(bid, receipt + offset - SECOND, receipt + SECOND)
    else:
        q = tick(bid, receipt + 3 * SECOND + offset, receipt + 3 * SECOND)
    deliver(strategy, q)
    assert spent(strategy, phase) == (delivery == "newer"), "I1: the guard's spending"

    # I2: newer quotes at the stream's skew; the sell goes out within the bound, every step named.
    step_named(strategy, q.ts_init + SECOND // 4)
    deadline = receipt + STAMP_AHEAD_MOST_NS + 2 * SECOND
    sent_at = None
    for n in range(4, 16):
        at = receipt + n * SECOND
        deliver(strategy, tick(bid, at + offset, at))
        if strategy.sent and sent_at is None:
            sent_at = at
        step_named(strategy, at + SECOND // 4)
    assert len(strategy.sent) == 1, "I2: no sell, or more than one"
    assert sent_at is not None and sent_at <= deadline, (
        f"I2: waited to +{(cast(int, sent_at) - receipt) / SECOND:.0f} s past the reference"
    )
