"""A refused supplied level is dropped at once, and no quote from before an arming counts.

The lead's ruling on unit 10's composed review (2026-11-13, 15:55):
- P1-1: a supplied level refused for its precision, or because the instrument is not yet in the
  cache, is dropped at the refusal and named EVERY time. The holding it was supplied for is named
  unprotected at every step. A later long on that symbol self-arms or is named unprotected; no
  symbol is skipped in silence.
- P2 (ruling 6, "no election on one observation"): the first-step guard requires a newer venue
  stamp after arming. U-h Q1/Q4 replaces the old QUOTE_MOST planning limit: the current maximum
  may plan at any age within the open session part, with its age named; an earlier-part quote
  waits for this part's first quote and says so.
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

from canon_trading import protect
from canon_trading.price_step import up_on_step
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import equity
from tests.test_owner_protector import position
from tests.test_owner_protector import quote
from tests.test_owner_protector import task_distance


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def step(strategy: Harness) -> None:
    strategy._on_step(cast(Any, SimpleNamespace()))


def unprotected_lines(strategy: Harness) -> list[str]:
    return [line for line in strategy.said if "AAA.ALPACA" in line and "unprotected" in line]


def refusing(cause: str, monkeypatch) -> tuple[Harness, dict[str, bool]]:
    """A Harness whose supplied AAA level is refused at start, for `cause`, and a switch that
    loads the instrument into the cache."""
    loaded: dict[str, bool] = {"yet": True}
    if cause == "off_precision":
        # The instrument is in cents.
        return Harness(levels={AAA: Price.from_str("9.9017")}), loaded
    loaded["yet"] = False
    strategy = Harness(levels={AAA: Price.from_str("9.90")})
    monkeypatch.setattr(
        strategy.cached, "instrument", lambda iid: equity(iid) if loaded["yet"] else None
    )
    return strategy, loaded


@pytest.mark.parametrize("cause", ["off_precision", "instrument_missing"])
def test_a_refused_supplied_level_is_dropped_and_its_holding_named_every_step(
    cause, monkeypatch
) -> None:
    strategy, loaded = refusing(cause, monkeypatch)
    strategy.on_start()
    assert AAA not in strategy.levels, "a refused level must be dropped at its refusal"
    assert AAA not in strategy.slots and not strategy.sent
    loaded["yet"] = True
    first = len(unprotected_lines(strategy))
    assert first >= 1, "the refusal must be named at once"
    for n in (1, 2, 3):
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy.cached.latest = quote("10.00", T0 + n * SECOND)
        step(strategy)
        named = unprotected_lines(strategy)
        assert len(named) == first + n, "the holding must be named unprotected at every step"
        assert "supplied level" in strategy.frozen[AAA]
    assert not strategy.sent, "the holding whose level was refused is not re-armed from the row"


@pytest.mark.parametrize("cause", ["off_precision", "instrument_missing"])
def test_a_later_holding_on_a_refused_symbol_self_arms(cause, monkeypatch) -> None:
    strategy, loaded = refusing(cause, monkeypatch)
    strategy.on_start()
    loaded["yet"] = True
    strategy.cached.held = []  # the owner sells the first holding
    strategy.test_clock.set_time(T0 + 10 * SECOND)
    step(strategy)
    later = position("10")
    later.ts_opened = later.ts_init = T0 + 3600 * SECOND
    strategy.cached.held = [later]
    strategy.test_clock.set_time(T0 + 3600 * SECOND)
    strategy.cached.latest = quote("10.00", T0 + 3600 * SECOND)
    step(strategy)
    assert AAA in strategy.slots, f"the later holding was skipped: frozen={strategy.frozen}"
    strategy.observe("10.00", T0 + 3601 * SECOND)
    strategy.observe("10.00", T0 + 3602 * SECOND)
    assert len(strategy.sent) == 1


def test_a_self_armed_guard_is_not_spent_on_the_quote_it_was_armed_from() -> None:
    """Armed at T0+59 s from a quote stamped T0; the first quote received after it cannot elect."""
    strategy = Harness()
    strategy.cached.held = []
    strategy.on_start()
    found = position("10")
    found.ts_opened = found.ts_init = T0 + 59 * SECOND
    strategy.cached.held = [found]
    strategy.cached.latest = quote("10.00", T0)
    strategy.test_clock.set_time(T0 + 59 * SECOND)
    step(strategy)
    level = strategy.slots[AAA].level
    assert level is not None and not strategy.sent
    step(strategy)  # the timer again, on the same cached quote
    assert not strategy.sent
    strategy.observe("9.50", T0 + 60 * SECOND)  # the first quote received after the arming
    assert not strategy.sent, "one observation after the arming elected"
    strategy.observe("9.50", T0 + 61 * SECOND)
    assert len(strategy.sent) == 1, "a second observation at or under the level confirms"
    assert strategy.sent[0].trigger_price == level


def test_a_supplied_level_guard_is_not_spent_on_a_quote_cached_before_its_adoption() -> None:
    """At or under the supplied level, the cached quote spends no guard; two later ones confirm."""
    strategy = Harness()
    strategy.cached.latest = quote("9.90", T0)
    strategy.test_clock.set_time(T0 + 30 * SECOND)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert not strategy.sent
    strategy.observe("9.89", T0 + 31 * SECOND)
    assert not strategy.sent, "a quote cached before the adoption spent the guard"
    strategy.observe("9.88", T0 + 32 * SECOND)
    assert len(strategy.sent) == 1


def test_a_prior_part_quote_waits_named_then_arms_from_this_parts_first_quote() -> None:
    strategy = Harness()
    strategy.cached.latest = quote("12.00", T0)
    now = T0 + 6 * 3600 * SECOND  # 17:00 New York; the cached 11:00 quote is before this part.
    strategy.test_clock.set_time(now)
    strategy.on_start()
    assert strategy.slots[AAA].level is None, "a level was planned from the previous part"
    assert any("before the open session part" in line for line in unprotected_lines(strategy))
    strategy.observe("10.00", now + SECOND)
    distance, _ = task_distance(strategy, "OWN1")
    level = strategy.slots[AAA].level
    assert level is not None
    assert level.as_decimal() == up_on_step(Decimal("10.00") - distance)
    assert not strategy.sent, "the quote the level was armed from is not an observation"
    strategy.observe("10.00", now + 2 * SECOND)
    assert not strategy.sent, "the first quote after the arming is the guarded one"
    strategy.observe("10.00", now + 3 * SECOND)
    assert len(strategy.sent) == 1
    assert protect.QUOTE_MOST.total_seconds() == 60


# The lead's ruling (2026-11-13, 16:13:47) on the fix review: a quote spends the guard, or sends,
# only if it was received after the arming AND stamped by the venue after the arming quote; nothing
# is sent on the cached quote, so a supplied level under the cached bid waits for such a quote too.
def tick(bid: str, ts_event: int, ts_init: int) -> QuoteTick:
    """A quote with its own venue time and receipt time, as the live stream gives them."""
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


def test_a_redelivered_cached_quote_spends_no_guard() -> None:
    strategy = Harness()
    strategy.cached.latest = tick("9.99", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0, T0 + SECOND // 2))  # the same venue observation again
    deliver(strategy, tick("9.98", T0 + SECOND, T0 + SECOND))  # the first new one: guarded
    assert not strategy.sent, "the first new venue observation after the adoption elected"
    deliver(strategy, tick("9.97", T0 + 2 * SECOND, T0 + 2 * SECOND))
    assert len(strategy.sent) == 1


def test_a_late_older_quote_spends_no_self_arm_guard() -> None:
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    deliver(strategy, tick("10.00", T0 + SECOND, T0 + SECOND))  # the level is planned on this
    level = strategy.slots[AAA].level
    assert level is not None and not strategy.sent
    under = str(level.as_decimal() - Decimal("0.01"))
    deliver(strategy, tick(under, T0 + SECOND // 2, T0 + SECOND + SECOND // 5))  # older, late
    deliver(strategy, tick(under, T0 + 2 * SECOND, T0 + 2 * SECOND))  # newer: guarded
    assert not strategy.sent, "the first observation newer than the arming quote elected"
    deliver(strategy, tick(under, T0 + 3 * SECOND, T0 + 3 * SECOND))
    assert len(strategy.sent) == 1


def test_a_duplicate_venue_observation_received_later_is_not_a_confirmation() -> None:
    strategy = Harness()
    strategy.cached.latest = tick("10.00", T0, T0)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + SECOND))  # guarded
    deliver(strategy, tick("9.99", T0 + SECOND, T0 + 2 * SECOND))  # the same one, again
    assert not strategy.sent, "a duplicate venue observation confirmed the breach"
    deliver(strategy, tick("9.98", T0 + 3 * SECOND, T0 + 3 * SECOND))
    assert len(strategy.sent) == 1


def test_a_supplied_level_under_the_cached_bid_waits_for_a_new_quote() -> None:
    """One quote of delay before placement: nothing is sent on the quote cached at adoption."""
    strategy = Harness()
    strategy.cached.latest = tick("10.50", T0, T0)
    strategy.test_clock.set_time(T0 + 30 * SECOND)
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert not strategy.sent, "sent on a quote received before the adoption"
    deliver(strategy, tick("10.50", T0 + 31 * SECOND, T0 + 31 * SECOND))
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == Price.from_str("10.00")
