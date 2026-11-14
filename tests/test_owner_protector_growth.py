"""A holding that grows is covered whole within one step, and its level never falls for the growth.

The lead's property (e) for unit 10 (2026-11-13, 16:31, from account C's case, where nine tenths of
the holding was bought in its last 106 seconds): when the owner buys more, the protector covers the
whole holding within one step, and its level never falls because of the growth.

At the pin (NautilusTrader d53849b216e2) a fill that adds to an open position keeps its `ts_opened`
and `ts_init` (crates/model/src/position.rs: only `reset_cycle`, :717, and a rebuild from the first
event, :410, set them), so a grown holding is the same holding: the protector cancels its own sell
for the old quantity, and once the cancel is acknowledged it sends one sell for the whole holding at
the level it held, never re-planned from the bid. A step here is the protector's one-second timer.

The gain lock's entry (the account's average entry as Alpaca returns it) is the gain-lock unit's
part of the property, composed onto this file by its own lane; unit 10 has no gain lock.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from tests.test_owner_protector import AAA
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import new_quote
from tests.test_owner_protector import position
from tests.test_owner_protector import working_sell


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def step(strategy: Harness, at: int) -> None:
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace()))


def acknowledge_cancel(strategy: Harness, order: Any, reached: str) -> None:
    """Alpaca acknowledges our cancel; the trailing trigger had reached `reached`."""
    strategy.cached.working = [
        working_sell(
            str(order.client_order_id),
            qty=str(order.quantity),
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=Price.from_str(reached),
        )
    ]


def grows_to(strategy: Harness, qty: str) -> None:
    """The owner buys more: the same NETTING position, larger (ts_opened and ts_init unchanged)."""
    strategy.cached.held = [position(qty)]


def armed_one_share() -> Harness:
    strategy = Harness()
    strategy.cached.held = [position("1")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert len(strategy.sent) == 1 and strategy.sent[0].quantity == Quantity.from_int(1)
    return strategy


def test_growth_in_three_buys_over_106_seconds_is_covered_whole_and_the_level_never_falls() -> None:
    """C's shape: 1 share held, then 3, 6 and 9 more within 106 s; quotes every second, the bid
    falling under the level between buys. Each growth is covered whole by the step after our
    cancel is acknowledged, at a level at or over every level before it."""
    strategy = armed_one_share()
    levels = [strategy.sent[0].trigger_price]
    reached = ["9.95", "9.97", "9.98"]
    at = T0 + SECOND
    for buy, (qty, high) in enumerate(zip(("4", "7", "10"), reached, strict=True)):
        grows_to(strategy, qty)
        strategy.observe("10.00", at)
        step(strategy, at + SECOND // 2)
        own = strategy.sent[-1]
        assert strategy.canceled[-1] == own.client_order_id, f"buy {buy}: our sell not canceled"
        acknowledge_cancel(strategy, own, high)
        strategy.observe("9.80", at + SECOND)  # under every level: a level must not follow it
        step(strategy, at + SECOND + SECOND // 2)
        replacement = strategy.sent[-1]
        assert replacement.quantity == Quantity.from_str(qty), f"buy {buy}: not covered whole"
        assert replacement.trigger_price >= max(levels), f"buy {buy}: the level fell"
        assert replacement.trigger_price == Price.from_str(high)
        levels.append(replacement.trigger_price)
        at += 35 * SECOND  # three buys within 106 s, as in C's case
    assert len(strategy.sent) == 4


def test_a_grown_holding_is_covered_whole_at_the_first_step_after_the_cancel_with_no_new_quote() -> (
    None
):
    """A quiet name: the owner's buy, our cancel and its acknowledgement come between quotes. The
    whole holding is covered at the first step after the acknowledgement, on the level held; no
    new quote is needed to re-place coverage of a holding whose guard was already spent."""
    strategy = armed_one_share()
    first = strategy.sent[0]
    grows_to(strategy, "10")
    step(strategy, T0 + SECOND)
    assert strategy.canceled == [first.client_order_id]
    acknowledge_cancel(strategy, first, "9.95")
    step(strategy, T0 + 2 * SECOND)
    assert len(strategy.sent) == 2, "the grown holding was left uncovered for want of a new quote"
    assert strategy.sent[1].quantity == Quantity.from_int(10)
    assert strategy.sent[1].trigger_price == Price.from_str("9.95")


def test_growth_never_re_plans_the_level_from_the_bid() -> None:
    """After growth the level is the one held (or the trigger our sell reached), never the bid
    less the row distance, even when the bid has risen far above it."""
    strategy = armed_one_share()
    first = strategy.sent[0]
    grows_to(strategy, "10")
    strategy.observe("12.00", T0 + SECOND)
    acknowledge_cancel(strategy, first, "9.90")
    strategy.observe("12.00", T0 + 2 * SECOND)
    assert strategy.sent[-1].quantity == Quantity.from_int(10)
    assert strategy.sent[-1].trigger_price == Price.from_str("9.90")
