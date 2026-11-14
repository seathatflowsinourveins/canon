"""The unit 10 fix-delta review's three P1 reproductions, kept as tests (P1 3 a named limit)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import StrategyId

from canon_trading.price_step import up_on_step
from tests.test_owner_protector import AAA
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import position
from tests.test_owner_protector import task_distance
from tests.test_owner_protector import working_sell


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline reproductions."""


def test_p1_1_rebuy_before_our_fill_does_not_rearm_at_the_elected_trigger() -> None:
    strategy = Harness()
    strategy.cached.held = [position("10")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    strategy.observe("10.00", T0 + SECOND)  # Second observation: armed.
    assert len(strategy.sent) == 1
    first = strategy.sent[0]
    # The owner buys 10 more first (position 20, same ts_opened); then our 10-share sell
    # fills at its raised 12.00 trigger: the position is 10, never flat, same ts_opened.
    strategy.cached.held = [position("10")]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="10",
            strategy_id=PROTECTOR,
            status=OrderStatus.FILLED,
            is_closed=True,
            events=lambda: [SimpleNamespace(trigger_price=Price.from_str("12.00"))],
        )
    ]
    strategy.observe("11.00", T0 + 3 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert len(strategy.sent) == 1, (
        f"re-armed at {strategy.sent[-1].trigger_price} on a quote at 11.00: "
        "the re-bought shares are sold on one observation"
    )


def test_p1_2_level_supplied_with_no_long_does_not_arm_a_much_later_holding() -> None:
    strategy = Harness()
    strategy.cached.held = []
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    for _ in range(3):
        strategy._on_step(cast(Any, SimpleNamespace()))
    later = position("10")
    later.ts_opened = T0 + 3600 * SECOND  # Bought an hour later, at about 8.00.
    strategy.cached.held = [later]
    strategy.cached.latest = None  # U-h permits an old quote in this part; this case starts empty.
    strategy.test_clock.set_time(T0 + 3600 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))  # The one-second timer adopts it.
    strategy.observe("8.00", T0 + 3601 * SECOND)
    distance, _ = task_distance(strategy, "OWN1")
    expected = up_on_step(Decimal("8.00") - distance)
    assert not strategy.sent, "a new holding still needs its first-observation guard"
    strategy.observe("7.99", T0 + 3602 * SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe("7.98", T0 + 3603 * SECOND)
    assert len(strategy.sent) == 1, "the later holding self-arms without another supplied level"
    assert strategy.sent[0].trigger_price.as_decimal() == expected
    assert strategy.sent[0].trigger_price != Price.from_str("10.00")


@pytest.mark.xfail(
    strict=True, reason="Lead ruling: P1 3 is a named limit; Mover cover can cancel our sell"
)
def test_p1_3_mover_entering_the_symbol_does_not_pull_our_armed_sell() -> None:
    mover = StrategyId("Mover-001")
    strategy = Harness()
    strategy.cached.held = [position("100")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    strategy.observe("10.00", T0 + SECOND)
    assert len(strategy.sent) == 1
    own = strategy.sent[0]
    strategy.cached.held = [position("100"), position("50", mover)]
    strategy.cached.working = [
        own,
        working_sell("O-20261113-190000-001-001-1", qty="50", filled="0", strategy_id=mover),
    ]
    strategy.observe("10.01", T0 + 2 * SECOND)
    assert not strategy.canceled, (
        "the Mover's own 50 on its own 50 cannot oversell; our armed sell on the owner's "
        "100 was canceled and the owner's long is left without protection"
    )
