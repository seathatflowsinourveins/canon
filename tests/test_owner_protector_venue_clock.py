"""The lead's venue clock (2026-11-13 17:36), including the high review's r1, r1b and r2.

All quotes and holdings are synthetic; the shared Harness uses the real order factory without
transport. No file, paper state, catalog, ledger, credential or network service is used.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector_stamp_table import deliver
from tests.test_owner_protector_stamp_table import step_named

MS = SECOND // 1000


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def tick(bid: str, stamp: int, receipt: int, spread: str = "0.02") -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(bid),
        Price.from_str(str(Decimal(bid) + Decimal(spread))),
        Quantity.from_int(100),
        Quantity.from_int(100),
        stamp,
        receipt,
    )


def cold_breach() -> Harness:
    strategy = Harness()
    strategy.cached.latest = None
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    return strategy


def test_r1_redelivery_of_a_spread_refused_quote_never_spends_the_guard() -> None:
    """A moves the clock despite its spread; B is older; A again cannot count; C is guarded.

    The scratch reproduction asserted that B became the reference, a pre-fix implementation
    detail which contradicts the new definition. Keep its trace and sale assertion instead.
    """
    strategy = cold_breach()
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND, "1.50"))
    assert strategy.slots[AAA].distance is None, "precondition: the planner refused A"
    deliver(strategy, tick("9.99", T0 + 200 * MS, T0 + 1500 * MS))
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + 1800 * MS, "1.50"))
    deliver(strategy, tick("9.99", T0 + 2 * SECOND, T0 + 2 * SECOND))
    assert not strategy.sent, "sold on A's re-delivery plus one new quote"
    step_named(strategy, T0 + 2250 * MS)
    deliver(strategy, tick("9.99", T0 + 3 * SECOND, T0 + 3 * SECOND))
    assert len(strategy.sent) == 1


def test_r1b_a_quote_older_than_a_seen_spread_refusal_never_spends_the_guard() -> None:
    strategy = cold_breach()
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND, "1.50"))
    deliver(strategy, tick("9.99", T0 + 200 * MS, T0 + 1500 * MS))
    deliver(strategy, tick("9.99", T0 + 500 * MS, T0 + 1800 * MS))
    deliver(strategy, tick("9.99", T0 + 2 * SECOND, T0 + 2 * SECOND))
    assert not strategy.sent, "M, older than A already seen, spent the guard; C sold"
    deliver(strategy, tick("9.99", T0 + 3 * SECOND, T0 + 3 * SECOND))
    assert len(strategy.sent) == 1


@pytest.mark.parametrize("old_clock", ["stamp", "receipt"])
def test_r2_age_is_named_from_the_earlier_clock_and_an_ahead_stamp_stays_refused(
    old_clock: str,
) -> None:
    strategy = Harness()
    stamp, receipt = T0 - 90 * SECOND, T0 - SECOND
    if old_clock == "receipt":
        stamp, receipt = receipt, stamp
    strategy.cached.latest = tick("10.00", stamp, receipt)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    slot = strategy.slots[AAA]
    assert slot.armed_event is None, "a stale quote became the arming reference"
    if old_clock == "stamp":
        assert slot.distance is not None, "age alone within this part no longer refuses the plan"
        assert slot.distance.quote_age.total_seconds() == 90
        assert any("quote 90 s old" in line for line in strategy.said)
    else:
        assert slot.distance is None, "a stamp 89 s ahead of receipt is still refused"
        assert "ahead of its receipt" in strategy.frozen[AAA]
    assert not strategy.sent


@pytest.mark.parametrize("self_armed", [False, True], ids=["supplied", "self-armed"])
@pytest.mark.parametrize("prior", ["callback", "cache"], ids=["before-holding", "warm-cache"])
def test_an_older_last_arrival_cannot_plan_a_level(self_armed: bool, prior: str) -> None:
    strategy = Harness()
    newest = tick("9.99", T0 + SECOND, T0 + SECOND, "1.50")
    older = tick("9.99", T0 + 200 * MS, T0 + 1500 * MS)
    if prior == "callback":
        deliver(strategy, newest)
        deliver(strategy, older)
    else:
        strategy.cached.earlier_quotes = [newest]
        strategy.cached.latest = older
        strategy.test_clock.set_time(older.ts_init)
    if self_armed:
        strategy.on_start()
    else:
        strategy.adopt_holding(AAA, Price.from_str("10.00"))
    assert strategy.slots[AAA].distance is None, "planned on the older last arrival"
    for at in (T0 + 1600 * MS, T0 + 1700 * MS):
        step_named(strategy, at)
        assert strategy.slots[AAA].distance is None
    deliver(strategy, tick("9.99", T0 + 2 * SECOND, T0 + 2 * SECOND))
    assert strategy.slots[AAA].distance is not None
    assert not strategy.sent, "planning cannot bypass the first observation guard"


def test_a_spread_refusal_after_planning_also_moves_the_clock() -> None:
    """Even when coverage blocks management, a received sane quote moves the clock."""
    from tests.test_owner_protector import working_sell

    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy.cached.working = [working_sell("owner-limit", qty="10", filled="0")]
    deliver(strategy, tick("9.99", T0 + 2 * SECOND, T0 + 2 * SECOND, "1.50"))
    strategy.cached.working = []
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + 2500 * MS))
    deliver(strategy, tick("9.99", T0 + 3 * SECOND, T0 + 3 * SECOND))
    assert not strategy.sent, "a quote seen while covered was forgotten when cover ended"
    deliver(strategy, tick("9.99", T0 + 4 * SECOND, T0 + 4 * SECOND))
    assert len(strategy.sent) == 1
