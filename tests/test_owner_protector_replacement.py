"""F8: a terminal cancel or DAY expiry must not leave a holding waiting for a good quote.

Synthetic holdings and native orders use the existing command harness. No network, paper state,
catalog or ledger is read or written. A bad newest quote cannot erase an older usable sale price.
"""

from __future__ import annotations

import gc
import re
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TimeInForce

from canon_trading.owner_protector import OwnerProtector
from tests import test_owner_protector as h
from tests.test_owner_protector_chase import CHASE
from tests.test_owner_protector_chase import RELEASE
from tests.test_owner_protector_chase import released
from tests.test_owner_protector_chase import terminal
from tests.test_owner_protector_chase import working
from tests.test_owner_protector_growth import acknowledge_cancel
from tests.test_owner_protector_growth import armed_one_share
from tests.test_owner_protector_growth import grows_to
from tests.test_owner_protector_growth import step


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment fixture for these offline cases."""


def unusable(at: int, kind: str) -> QuoteTick:
    return QuoteTick(
        h.AAA,
        Price.from_str("9.70"),
        Price.from_str("9.65" if kind == "crossed" else "9.72"),
        Quantity.from_int(0 if kind == "zero-bid-size" else 100),
        Quantity.from_int(0 if kind == "zero-ask-size" else 100),
        at,
        at,
    )


def receive(strategy: h.Harness, tick: QuoteTick) -> None:
    strategy.cached.latest = tick
    strategy.test_clock.set_time(max(strategy.clock.timestamp_ns(), tick.ts_init))
    strategy.on_quote(tick)


@pytest.mark.parametrize("kind", ["crossed", "zero-bid-size", "zero-ask-size"])
def test_growth_replaces_whole_holding_after_cancel_despite_bad_newest_quote(kind: str) -> None:
    strategy = armed_one_share()
    first = strategy.sent[0]
    grows_to(strategy, "10")
    # The last usable quote is an hour old and has left the cache's arrival history.
    at = h.T0 + 3600 * h.SECOND
    receive(strategy, unusable(at, kind))
    assert strategy.canceled == [first.client_order_id]
    assert strategy.sent == [first], "the terminal acknowledgement must come first"
    acknowledge_cancel(strategy, first, "9.95")
    step(strategy, at + h.SECOND)

    assert len(strategy.sent) == 2, "F8: size cancel left the holding without its working sell"
    replacement = strategy.sent[-1]
    assert replacement.quantity == Quantity.from_int(10)
    assert replacement.trigger_price == Price.from_str("9.95")
    assert replacement.order_type == OrderType.TRAILING_STOP_LIMIT
    assert len(working(strategy)) == 1
    assert h.AAA not in strategy.frozen


@pytest.mark.parametrize("kind", ["crossed", "zero-bid-size", "zero-ask-size"])
def test_day_expired_trail_replaces_on_the_first_step_with_an_old_usable_quote(kind: str) -> None:
    strategy = armed_one_share()
    first = strategy.sent[0]
    at = h.T0 + 3600 * h.SECOND
    receive(strategy, unusable(at, kind))
    strategy.cached.working = [
        h.working_sell(
            str(first.client_order_id),
            qty="1",
            filled="0",
            strategy_id=h.PROTECTOR,
            status=OrderStatus.EXPIRED,
            is_closed=True,
            trigger_price=Price.from_str("9.95"),
        )
    ]
    step(strategy, at + h.SECOND)

    assert len(strategy.sent) == 2, "F8: DAY expiry left the holding without its working sell"
    replacement = strategy.sent[-1]
    assert replacement.quantity == Quantity.from_int(1)
    assert replacement.trigger_price == Price.from_str("9.95")
    assert replacement.time_in_force == TimeInForce.DAY
    assert replacement.is_reduce_only
    assert strategy.params[-1] == {"extended_hours": True}
    assert len(working(strategy)) == 1


@pytest.mark.parametrize("expired", [False, True], ids=["own-reprice-cancel", "day-expiry"])
@pytest.mark.parametrize("kind", ["crossed", "zero-bid-size", "zero-ask-size"])
def test_sale_replacement_uses_newest_usable_venue_stamp_even_after_bad_and_late_quotes(
    kind: str, expired: bool
) -> None:
    strategy, first = released()
    at = RELEASE + (h.SECOND if expired else CHASE + h.SECOND)
    strategy.observe("9.85", at)
    assert strategy.canceled == ([] if expired else [first.client_order_id])
    strategy.observe("9.80", at + h.SECOND)
    # A late arrival is usable but older: the replacement must use 9.80, not 9.70 or 9.85.
    receive(strategy, h.quote("9.70", at - h.SECOND))
    receive(strategy, unusable(at + 3600 * h.SECOND, kind))
    assert len(strategy.sent) == 1, "no overlapping sale before the original leg ends"
    terminal(first, at + 3600 * h.SECOND, expired=expired)
    step(strategy, at + 3601 * h.SECOND)

    assert len(strategy.sent) == 2, "F8: terminal sale leg was not replaced at the next step"
    replacement = strategy.sent[-1]
    assert replacement.order_type == OrderType.LIMIT
    assert replacement.price == Price.from_str("9.79"), "one tick through the newest usable bid"
    assert replacement.quantity == Quantity.from_int(10)
    assert replacement.time_in_force == TimeInForce.DAY and replacement.is_reduce_only
    assert strategy.params[-1] == {"extended_hours": True}
    assert len(working(strategy)) == 1
    assert h.AAA not in strategy.frozen


class LoggedHarness(h.Harness):
    """Keep the production severity routing and capture only the logger's output boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.logged: list[tuple[str, str]] = []

    def _say(self, text: str) -> None:
        super()._say(text)
        log = SimpleNamespace(
            error=lambda line: self.logged.append(("ERROR", line)),
            info=lambda line: self.logged.append(("INFO", line)),
        )
        OwnerProtector._say(cast(Any, SimpleNamespace(log=log)), text)


@pytest.mark.parametrize("kind", ["absent", "crossed", "zero-bid-size", "zero-ask-size"])
def test_no_usable_quote_ever_names_the_unprotected_holding_at_error_until_quotes_arrive(
    kind: str,
) -> None:
    strategy = LoggedHarness()
    strategy.cached.latest = None if kind == "absent" else unusable(h.T0, kind)
    strategy.adopt_holding(h.AAA, Price.from_str("9.90"))
    for second in (1, 2):
        before = len(strategy.logged)
        step(strategy, h.T0 + second * h.SECOND)
        assert not strategy.sent
        assert any(
            severity == "ERROR"
            and "AAA.ALPACA" in line
            and "quote" in line
            and ("unprotected" in line or "without" in line)
            for severity, line in strategy.logged[before:]
        ), "F8: a holding without any usable quote must be named at ERROR on each step"
    strategy.observe("10.00", h.T0 + 3 * h.SECOND)
    strategy.observe("10.00", h.T0 + 4 * h.SECOND)
    strategy.observe("10.00", h.T0 + 5 * h.SECOND)
    assert len(working(strategy)) == 1
    assert h.AAA not in strategy.frozen


def test_engine_replaces_size_canceled_trail_while_crossed_quotes_continue(capfd) -> None:
    """The real emulator acknowledges the size cancel; no transport or event stand-in does it."""
    gc.collect()

    class GrowingOwner(h.Owner):
        def on_quote(self, quote) -> None:
            if not self.bought:
                self.bought = True
                quantity = 1
            elif quote.ts_event == h.T0 + 3 * h.SECOND:
                quantity = 9
            else:
                return
            self.submit_order(
                self.order_factory.market(h.AAA, h.OrderSide.BUY, Quantity.from_int(quantity))
            )

    class ObservedProtector(h.SimulatedProtector):
        def __init__(self, level: str) -> None:
            super().__init__(level)
            self.last_cover = Decimal(0)

        def on_quote(self, quote) -> None:
            super().on_quote(quote)
            self.last_cover = sum(
                (
                    order.leaves_qty.as_decimal()
                    for order in self.cache.orders(strategy_id=self.strategy_id)
                    if not order.is_closed
                ),
                Decimal(0),
            )

    engine = h.BacktestEngine(h.BacktestEngineConfig(trader_id=h.TraderId("TESTER-001")))
    engine.add_venue(
        h.Venue("ALPACA"),
        h.OmsType.NETTING,
        h.AccountType.CASH,
        [h.Money(100_000, h.USD)],
        fee_model=h.MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    engine.add_instrument(h.equity())
    owner = GrowingOwner()
    protector = ObservedProtector("9.00")
    engine.add_strategy(owner)
    engine.add_strategy(protector)
    engine.add_data(
        [h.quote("9.60", h.T0 + second * h.SECOND) for second in range(3)]
        + [unusable(h.T0 + second * h.SECOND, "crossed") for second in range(3, 7)]
    )
    try:
        engine.run()
        output = capfd.readouterr()
        assert not re.search(r"Python \w+ failed", output.out + output.err)
        assert protector.last_cover == Decimal(10), "F8: emulator cancel left the holding uncovered"
        assert [order.quantity for order, _ in protector.submits] == [
            Quantity.from_int(1),
            Quantity.from_int(10),
        ]
    finally:
        engine.dispose()
