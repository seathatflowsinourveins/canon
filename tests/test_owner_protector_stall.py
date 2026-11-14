"""No quote stamped far ahead of its receipt stalls protection, and every wait is said at every step.

The lead's ruling on unit 10 (2026-11-13, 16:3x, on the round-2 review's P2-1), as the lead's guard
definition V1-V4 (2026-11-13, 17:0x) now states it:
- The stall: a quote whose venue stamp runs ahead of the stream, taken as the arming quote (or as the
  last observation), made every later quote fail the "stamped after" rule, so the protector sent
  nothing and named nothing. Under V1 a quote stamped more than the adapter's `STAMP_AHEAD_MOST_NS`
  ahead of its own receipt (canon_alpaca/data.py:1044, `trade_stamp` for prints) is no observation
  and never a reference, so it cannot stall; its stamp, delivered again within the bound, is still
  the same observation and never counts (V3 and the refused-stamp record).
- A host clock more than the bound behind the venue's is the named limit (1): nothing is placed, and
  it is said at every step.
- The general property: a protector that cannot act on its quotes says so at every step, never in
  silence.
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
from tests.test_owner_protector import position

HOUR = 3600 * SECOND


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


def step(strategy: Harness, at: int) -> None:
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace()))


def waits_said(strategy: Harness) -> int:
    return sum(1 for line in strategy.said if line.startswith("AAA.ALPACA: unprotected: waiting"))


def test_an_arming_quote_stamped_an_hour_ahead_does_not_stall_protection() -> None:
    """The review's s1: the cached quote is stamped an hour ahead of its receipt. The first normal
    quote after the adoption, above the level, sends."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0 + HOUR, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    for n in (1, 2):
        deliver(strategy, tick("10.00", T0 + n * SECOND, T0 + n * SECOND))
        step(strategy, T0 + n * SECOND + SECOND // 4)
    assert len(strategy.sent) == 1, "a quote stamped an hour ahead stalled the protection"


def test_a_self_arming_quote_stamped_ahead_does_not_stall_protection() -> None:
    """V1: a quote stamped an hour ahead plans no level and is named; the next sane quote is the
    reference (V2), the one after it the guarded observation, and the third sells."""
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    deliver(strategy, tick("10.00", T0 + HOUR, T0 + SECOND))  # refused: no level on this
    assert strategy.slots[AAA].level is None and not strategy.sent
    assert "ahead of its receipt" in strategy.frozen[AAA]
    deliver(strategy, tick("10.00", T0 + 2 * SECOND, T0 + 2 * SECOND))  # the reference
    assert strategy.slots[AAA].level is not None and not strategy.sent
    deliver(strategy, tick("10.00", T0 + 3 * SECOND, T0 + 3 * SECOND))  # guarded
    assert not strategy.sent
    deliver(strategy, tick("10.00", T0 + 4 * SECOND, T0 + 4 * SECOND))
    assert len(strategy.sent) == 1, "a self-arming quote stamped ahead stalled the protection"


def test_a_guarded_quote_stamped_ahead_does_not_stall_the_confirmation() -> None:
    """At or under the level: a quote stamped an hour ahead of its receipt is no observation (V1),
    so it neither spends the guard nor holds back the two sane quotes that follow."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 + HOUR, T0 + SECOND))  # V1: no observation
    assert not strategy.sent and strategy.slots[AAA].last_observation is None
    deliver(strategy, tick("9.99", T0 + HOUR, T0 + 2 * SECOND))  # the same refused quote again
    assert not strategy.sent, "a re-delivered quote confirmed the breach"
    deliver(strategy, tick("9.97", T0 + 3 * SECOND, T0 + 3 * SECOND))  # the guarded observation
    assert not strategy.sent, "sold on one observation"
    deliver(strategy, tick("9.96", T0 + 4 * SECOND, T0 + 4 * SECOND))
    assert len(strategy.sent) == 1, "a quote stamped ahead held back the confirmation"


def test_a_quote_stamped_within_the_bound_still_orders() -> None:
    """Stamped exactly `STAMP_AHEAD_MOST_NS` ahead of its receipt: a usable time, as for prints."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 + SECOND + STAMP_AHEAD_MOST_NS, T0 + SECOND))
    assert not strategy.sent
    deliver(strategy, tick("9.98", T0 + 2 * SECOND, T0 + 2 * SECOND))
    assert not strategy.sent, "an earlier-stamped quote is not newer than the guarded one"
    deliver(strategy, tick("9.97", T0 + 2 * SECOND + STAMP_AHEAD_MOST_NS, T0 + 3 * SECOND))
    assert len(strategy.sent) == 1


def test_waiting_for_a_quote_after_the_arming_is_said_at_every_step() -> None:
    """The review's s3: a supplied level, a cached quote, then a quiet name. Each step says the
    holding is unprotected and what it waits for."""
    strategy = Harness()
    strategy.cached.latest = tick("10.50", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    for n in range(1, 6):
        step(strategy, T0 + n * SECOND)
        assert waits_said(strategy) == n, f"step {n} passed in silence"
    assert "waiting" in strategy.frozen[AAA]


def test_waiting_for_a_confirming_observation_is_said_at_every_step() -> None:
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND))  # guarded
    before = waits_said(strategy)
    for n in range(1, 4):
        step(strategy, T0 + SECOND + n * SECOND)
        assert waits_said(strategy) == before + n, f"step {n} passed in silence"


@pytest.mark.parametrize("behind", [6 * SECOND, HOUR], ids=["6s", "1h"])
def test_a_node_clock_behind_alpacas_stamps_is_the_named_limit(behind) -> None:
    """The (e) review's e1, now the named limit (1): every quote is stamped `behind` ahead of its
    receipt (the node's clock runs behind Alpaca's by more than the bound). V1 refuses each, so
    nothing is placed, and every step names the holding and why."""
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0 + behind, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    for n in range(1, 6):
        deliver(strategy, tick("10.00", T0 + behind + n * SECOND, T0 + n * SECOND))
        before = len(strategy.said)
        step(strategy, T0 + n * SECOND + SECOND // 4)
        said = strategy.said[before:]
        assert any(
            line.startswith("AAA.ALPACA: unprotected: waiting") and "ahead of its receipt" in line
            for line in said
        ), f"step {n} did not name the limit"
    assert not strategy.sent, "placed on quotes V1 refuses"


def test_a_redelivered_cached_quote_stamped_ahead_does_not_spend_the_guard() -> None:
    """The (e) review's P1-1 (e3): the cached quote is stamped 6 s ahead of its receipt, so V1
    refuses it and there is no reference. Re-delivered 2 s later, now within the bound, it carries
    a refused stamp and never counts; the first new quote is the reference (V2), the second the
    guarded observation, and the third sells."""
    strategy = Harness()
    strategy.cached.latest = tick("9.80", T0 + 6 * SECOND, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))  # breached: guard applies
    deliver(strategy, tick("9.80", T0 + 6 * SECOND, T0 + 2 * SECOND))  # the cached quote again
    deliver(strategy, tick("9.79", T0 + 7 * SECOND, T0 + 7 * SECOND))  # one new quote
    assert not strategy.sent, "sold on the cached quote's re-delivery plus one new quote"
    deliver(strategy, tick("9.78", T0 + 8 * SECOND, T0 + 8 * SECOND))
    assert not strategy.sent, "sold on one observation"
    deliver(strategy, tick("9.77", T0 + 9 * SECOND, T0 + 9 * SECOND))
    assert len(strategy.sent) == 1


def test_a_self_arming_quote_redelivered_does_not_spend_the_guard() -> None:
    """A quote stamped 6 s ahead plans no level (V1). Re-delivered within the bound it carries a
    refused stamp, so it plans none either (V3 and the record); the next quote is the reference."""
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    deliver(strategy, tick("10.00", T0 + 7 * SECOND, T0 + SECOND))  # refused
    assert strategy.slots[AAA].level is None and not strategy.sent
    deliver(strategy, tick("10.00", T0 + 7 * SECOND, T0 + 3 * SECOND))  # the same, re-delivered
    assert strategy.slots[AAA].level is None, "a level planned on a refused stamp"
    deliver(strategy, tick("10.00", T0 + 8 * SECOND, T0 + 8 * SECOND))  # the reference
    assert strategy.slots[AAA].level is not None
    deliver(strategy, tick("10.00", T0 + 9 * SECOND, T0 + 9 * SECOND))  # guarded: fresh level
    assert not strategy.sent, "a re-delivered arming quote spent the self-arm guard"
    deliver(strategy, tick("10.00", T0 + 10 * SECOND, T0 + 10 * SECOND))
    assert len(strategy.sent) == 1


def test_a_level_is_never_planned_on_a_quote_stamped_an_hour_ahead_of_receipt() -> None:
    """U-h permits old quotes inside the part; this quote still violates V1's stamp bound."""
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    old = tick("12.00", T0 + 3600 * SECOND, T0)
    strategy.cached.latest = old
    strategy.on_quote(old)
    step(strategy, T0 + 1800 * SECOND)
    assert strategy.slots[AAA].level is None, "a level was planned on a 30-minute-old quote"
    assert any("ahead of its receipt" in line for line in strategy.said)


def test_a_stopped_instrument_keeps_no_wait_record() -> None:
    """The (e) review's e6."""
    strategy = Harness()
    strategy.cached.latest = tick("10.50", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    step(strategy, T0 + SECOND)
    assert strategy.frozen[AAA].startswith("unprotected: waiting")
    strategy.stop_instrument(AAA)
    assert AAA not in strategy.frozen


def test_a_working_sell_is_not_a_wait() -> None:
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    deliver(strategy, tick("10.00", T0 + SECOND, T0 + SECOND))
    assert len(strategy.sent) == 1
    said = waits_said(strategy)
    for n in range(2, 5):
        step(strategy, T0 + n * SECOND)
    assert waits_said(strategy) == said
    assert AAA not in strategy.frozen


def test_a_placed_holding_says_no_wait_while_its_sell_works() -> None:
    strategy = Harness()
    strategy.cached.held = [position("1")]
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    deliver(strategy, tick("10.00", T0 + SECOND, T0 + SECOND))
    assert len(strategy.sent) == 1
    for n in range(2, 5):
        step(strategy, T0 + n * SECOND)
    assert waits_said(strategy) == 0
