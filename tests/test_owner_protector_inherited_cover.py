"""A start that finds a working sell of the protect lineage counts it as that holding's cover.

The lead's ruling (d)1 (2026-11-13, before proof W2): a start that finds a working sell of the protect
lineage (`custody.PROTECT_CLIENT_ORDER_ID`, `canon-protect-`) for a holding counts it as that
holding's cover, and arms NO second sell for those shares. Two working sells over one holding are an
oversell, and on account C one opens a short. Such a sell is the one a previous protector node left
working (its released LIMIT), or one paper:protect placed for Alpaca to hold (a trailing stop); after a
restart, reconciliation gives it the EXTERNAL strategy ID (reconciliation.md:305-378, at the pin).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price

from canon_trading import custody
from tests.test_owner_protector import AAA
from tests.test_owner_protector import CANON_SELL
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import position
from tests.test_owner_protector import working_sell


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def inherited(qty: str, kind: OrderType) -> Any:
    """A protect-lineage sell left working at Alpaca, reconciled as EXTERNAL at this start."""
    assert custody.lineage(CANON_SELL) == custody.PROTECT
    return working_sell(
        CANON_SELL,
        qty=qty,
        filled="0",
        status=OrderStatus.ACCEPTED,
        order_type=kind,
        trigger_price=None if kind == OrderType.LIMIT else Price.from_str("9.80"),
    )


def run(strategy: Harness, seconds: int = 6) -> None:
    for n in range(1, seconds + 1):
        strategy.observe("10.00", T0 + n * SECOND)
        strategy.test_clock.set_time(T0 + n * SECOND + SECOND // 2)
        strategy._on_step(cast(Any, SimpleNamespace()))


def working_over(strategy: Harness) -> Decimal:
    return sum(
        (
            Decimal(str(order.quantity)) - Decimal(str(order.filled_qty))
            for order in strategy.cached.working
            if order.side.name == "SELL" and not order.is_closed
        ),
        Decimal(0),
    )


KINDS = [OrderType.TRAILING_STOP_MARKET, OrderType.LIMIT]


@pytest.mark.parametrize("kind", KINDS, ids=["alpaca-trailing-stop", "released-limit"])
@pytest.mark.parametrize("supplied", [True, False], ids=["supplied-level", "self-armed"])
def test_a_whole_holding_covered_by_a_protect_sell_gets_no_second_sell(kind, supplied) -> None:
    strategy = Harness(levels={AAA: Price.from_str("9.90")} if supplied else {})
    strategy.cached.working = [inherited("10", kind)]
    strategy.on_start()
    run(strategy)
    assert not strategy.sent, "a second sell was armed over a protect-lineage cover"
    assert not strategy.canceled, "the protect-lineage sell is not ours to cancel"
    assert working_over(strategy) == Decimal(10)


@pytest.mark.parametrize("kind", KINDS, ids=["alpaca-trailing-stop", "released-limit"])
def test_a_holding_covered_in_part_by_a_protect_sell_gets_a_sell_for_the_rest_only(kind) -> None:
    strategy = Harness(levels={AAA: Price.from_str("9.90")})
    strategy.cached.working = [inherited("4", kind)]
    strategy.on_start()
    run(strategy)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].quantity.as_decimal() == Decimal(6)
    assert working_over(strategy) == Decimal(10), "the working sells exceed the holding"


def test_growth_over_a_protect_cover_arms_only_the_new_shares() -> None:
    strategy = Harness(levels={AAA: Price.from_str("9.90")})
    strategy.cached.working = [inherited("10", OrderType.TRAILING_STOP_MARKET)]
    strategy.on_start()
    run(strategy, 3)
    assert not strategy.sent
    strategy.cached.held = [position("15")]
    for n in (4, 5, 6):
        strategy.observe("10.00", T0 + n * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].quantity.as_decimal() == Decimal(5)
    assert working_over(strategy) == Decimal(15), "the working sells exceed the holding"
