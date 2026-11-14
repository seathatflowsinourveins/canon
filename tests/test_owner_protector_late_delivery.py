"""Late and repeated deliveries against the guard (unit 10's guard, the lead's V1-V4, 2026-11-13).

F1-F5 are the stamp-fix review's scratch cases, kept (the review's P1-1 and P2-2): F1-F3 were red on
the stamp fix, where a quote late by more than the bound counted as newer than a prompt reference.
The last two cases are the named limit (2) and the refused-stamp record's bound.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness

MS = SECOND // 1000


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Offline."""


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


def step(strategy: Harness, at: int) -> None:
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace()))


def test_f1_arming_quote_redelivered_six_seconds_late_does_not_confirm() -> None:
    """Every stamp is at its receipt (no skew, no stamp ahead). The cached quote Q0 is the arming
    quote; G is the one guarded observation. Q0 re-delivered 6 s after its stamp is the same,
    older observation and must not confirm: one observation cannot elect."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))  # bid at the level: breached
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND))  # G: guarded
    assert not strategy.sent
    deliver(strategy, tick("10.00", T0, T0 + 6 * SECOND))  # Q0 again, 6 s late
    assert not strategy.sent, "the arming quote, re-delivered late, confirmed the breach"


def test_f2_a_quote_older_than_the_arming_quote_delivered_late_does_not_spend_the_guard() -> None:
    """No skew. A quote stamped 1 s before the arming quote, delivered 6 s after its stamp, is a
    pre-arming observation (I1); then one new quote under the level must not sell."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))  # breached
    deliver(strategy, tick("9.99", T0 - SECOND, T0 + 5 * SECOND))  # older than Q0, late
    assert strategy.slots[AAA].last_observation is None, "a pre-arming quote spent the guard"
    deliver(strategy, tick("9.98", T0 + 6 * SECOND, T0 + 6 * SECOND))
    assert not strategy.sent, "sold on one observation newer than the arming quote"


def test_f2_contrast_inside_the_bound_late_older_is_refused() -> None:
    """The table's own late-older cell, 2 s late: refused, as I1 says."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 - SECOND, T0 + SECOND))
    assert strategy.slots[AAA].last_observation is None


def test_f3_supplied_clear_late_older_places_on_a_pre_arming_quote() -> None:
    """Level clear of the bid: the first post-arming observation places the sell. A quote older
    than the arming quote, 6 s late, is no post-arming observation and must not place it."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    deliver(strategy, tick("10.00", T0 - SECOND, T0 + 5 * SECOND))
    assert not strategy.sent, "placed on a quote stamped before the arming quote"


@pytest.mark.parametrize("lead_ms", [4900, 5000])
def test_f4_i2_at_the_bounds_edge(lead_ms) -> None:
    """The guarded observation is stamped just inside the bound ahead of a level stream; the sell
    goes out by the table's deadline (receipt + bound + 2 s), every step named."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    receipt = T0 + SECOND
    deliver(strategy, tick("9.99", receipt + lead_ms * MS, receipt))
    sent_at = None
    for n in range(2, 12):
        at = receipt + n * SECOND
        deliver(strategy, tick("9.99", at, at))
        if strategy.sent and sent_at is None:
            sent_at = at
        step(strategy, at + SECOND // 4)
    assert sent_at is not None and sent_at <= receipt + 7 * SECOND, sent_at


def test_f5_reference_late_beyond_the_bound_then_stream() -> None:
    """A reference received 6 s after its stamp (a negative lead, absent from the table), then a
    level stream: no stall, and the reference re-delivered never spends."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0 - 6 * SECOND, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("10.00", T0 - 6 * SECOND, T0 + SECOND))
    assert strategy.slots[AAA].last_observation is None
    for n in (2, 3):
        deliver(strategy, tick("9.99", T0 + n * SECOND, T0 + n * SECOND))
    assert len(strategy.sent) == 1


def test_limit_2_a_second_quote_with_the_same_stamp_waits_for_the_next_greater_stamp() -> None:
    """Named limit (2): two different quotes with one stamp are one observation. The second (a
    different bid) does not confirm; the next greater stamp does."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND))  # guarded
    deliver(strategy, tick("9.95", T0 + SECOND, T0 + SECOND + MS))  # another quote, same stamp
    assert not strategy.sent, "a second quote with the guarded stamp confirmed"
    deliver(strategy, tick("9.95", T0 + SECOND + MS, T0 + 2 * SECOND))
    assert len(strategy.sent) == 1


def test_the_refused_stamp_record_drops_its_oldest_entry_when_full() -> None:
    """The record keeps the newest `REFUSED_KEPT` stamps V1 refused. A refused stamp re-delivered
    within the bound never counts while it is kept; once pushed out, it is judged by V3 alone."""
    from canon_trading.owner_protector import REFUSED_KEPT

    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))  # clear: the first observation places
    first = T0 + 10 * SECOND
    deliver(strategy, tick("10.00", first, T0 + MS))  # 10 s ahead: refused
    deliver(strategy, tick("10.00", first, first - 4 * SECOND))  # the same stamp, now sane
    assert not strategy.sent, "a refused stamp counted on its re-delivery"
    for n in range(1, REFUSED_KEPT + 1):  # push it out with other refused stamps, 14 s ahead
        deliver(strategy, tick("10.00", T0 + 20 * SECOND + n * MS, T0 + 6 * SECOND + n * MS))
    assert first not in strategy.refused_stamps[AAA]
    assert len(strategy.refused_stamps[AAA]) == REFUSED_KEPT
    assert not strategy.sent
    deliver(strategy, tick("10.00", first, T0 + 7 * SECOND))  # judged by V3 alone now
    assert len(strategy.sent) == 1, "the dropped entry still held"
