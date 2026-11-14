"""Every sell the owner protector releases is of the protect lineage, by custody.py's one rule.

The lead's ruling (2026-11-13, 16:04:49, after ADR 0148): a protective sale of the owner's shares
is never one of our nodes' orders. Its client order ID satisfies `custody.PROTECT_CLIENT_ORDER_ID`,
so custody does not count it as ours, paper:close never cancels it, and paper:account names it.
NautilusTrader d53849b216e2 lets a strategy name the ID: OrderFactory.trailing_stop_limit takes
`client_order_id` (crates/common/src/factories/order.rs:950-973), and the emulator's released LIMIT
keeps it (crates/execution/src/order_emulator/emulator.rs:5094).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price

from canon_trading import custody
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


def test_a_sell_for_a_supplied_level_is_of_the_protect_lineage() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert len(strategy.sent) == 1
    cid = str(strategy.sent[0].client_order_id)
    assert cid.startswith(custody.PROTECT_PREFIX)
    assert custody.lineage(cid) == custody.PROTECT


def test_every_later_sell_has_its_own_protect_id() -> None:
    """A residual holding's sell after our fill gets a new ID of the same lineage."""
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="10",
            strategy_id=PROTECTOR,
            status=OrderStatus.FILLED,
            is_closed=True,
            trigger_price=Price.from_str("12.00"),
        )
    ]
    strategy.cached.held = [position("5")]
    for n in (1, 2, 3):
        strategy.observe("11.00", T0 + n * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert len(strategy.sent) == 2
    ids = [str(order.client_order_id) for order in strategy.sent]
    assert len(set(ids)) == 2
    assert all(custody.lineage(cid) == custody.PROTECT for cid in ids)
