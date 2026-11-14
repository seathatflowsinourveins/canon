"""The extracted protection seam and the Mover's unchanged order command ownership.

Uses the 13 in-memory golden replays; no file, venue or account state is written.
"""

from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price

from canon_trading.mover import TrailPlan
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import seam_slot
from tests.test_mover_adopt import quote
from tests.test_mover_adopt import replay
from tests.test_protection_golden import SCENARIOS
from tests.test_protection_golden import RecordingMover


def test_protection_has_the_strategy_callback_contract() -> None:
    module = importlib.import_module("canon_trading.protection")
    protection = module.Protection(object(), module.ProtectionConfig())
    for name in (
        "on_position_opened",
        "on_position_changed",
        "on_position_closed",
        "on_quote",
    ):
        assert callable(getattr(protection, name))


def inside_protection() -> bool:
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get("__name__") == "canon_trading.protection":
            return True
        frame = frame.f_back
    return False


@pytest.mark.parametrize("scenario", [name for name in SCENARIOS if name != "time_out"])
def test_every_mover_protective_command_passes_through_protection(
    monkeypatch, capfd, scenario
) -> None:
    outside: list[str] = []
    counted: list[str] = []

    def record(action: str) -> None:
        counted.append(action)
        if not inside_protection():
            outside.append(action)

    submit = RecordingMover.submit_order
    cancel = RecordingMover.cancel_order
    cancel_all = RecordingMover.cancel_all_orders
    market_exit = RecordingMover.market_exit

    def send(self, order, *args: Any, **kwargs: Any):
        if order.side == OrderSide.SELL:
            record("submit sell")
        return submit(self, order, *args, **kwargs)

    def withdraw(self, client_order_id, *args: Any, **kwargs: Any):
        order = self.cache.order(client_order_id)
        if order is not None and order.side == OrderSide.SELL:
            record("cancel sell")
        return cancel(self, client_order_id, *args, **kwargs)

    def withdraw_all(self, *args: Any, **kwargs: Any):
        record("cancel all")
        return cancel_all(self, *args, **kwargs)

    def exit_market(self, *args: Any, **kwargs: Any):
        record("market exit")
        return market_exit(self, *args, **kwargs)

    monkeypatch.setattr(RecordingMover, "submit_order", send)
    monkeypatch.setattr(RecordingMover, "cancel_order", withdraw)
    monkeypatch.setattr(RecordingMover, "cancel_all_orders", withdraw_all)
    monkeypatch.setattr(RecordingMover, "market_exit", exit_market)
    SCENARIOS[scenario]()
    capfd.readouterr()
    assert counted, "the tape must exercise a protective command"
    assert not outside, f"protective commands outside Protection: {outside}"


def test_mover_delegates_the_adoption_guard_and_activation_price():
    strategy = replay("10.0037", quote("10.0020", 5 * SECOND + SECOND // 2))
    (armed,) = strategy.initial["working"]
    assert strategy.initial["held"] == Decimal(7)
    assert not strategy.initial["released"]
    assert armed.init_event.trigger_price == Price.from_str("10.0100")
    assert armed.init_event.activation_price == Price.from_str("10.0200")
    assert strategy.steps[-1]["held"] == 0


def test_mover_delegates_reduce_only_on_an_adopted_limit_sale():
    strategy = replay("10.0037", quote("10.0020", 5 * SECOND + SECOND // 2))
    sales = [order for order in strategy.steps[-1]["sells"] if order.order_type == OrderType.LIMIT]
    assert sales, "the confirmed breach must reach the limit-sale path"
    assert all(order.is_reduce_only for order in sales)


def test_mover_delegates_the_plan_chase_cap_to_the_sale_spacing():
    host: Any = Seam("10.50")
    slot = seam_slot(host)
    slot.trail_plan = TrailPlan(
        trail=Decimal("0.02"),
        exit_through=Decimal("0.02"),
        chase_cap=Decimal("0.001"),
        atr=None,
        spread=None,
        bound="fixed",
    )
    host._trail(slot, Decimal(3), T0)
    (sale,) = host.sent
    spacing = sale.limit_offset - sale.trailing_offset
    bound = max(Decimal("0.01"), (Decimal(10) - sale.trailing_offset) * Decimal("0.001"))
    assert spacing > 0
    assert spacing <= bound, "the plan's cap, with the one-step minimum, bounds the sale"
