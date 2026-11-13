"""Unit 6: protective LIMIT sells keep extended-hours eligibility across the close.

NautilusTrader d53849b216e2, read at the pin: Strategy.submit_order accepts params
(python/nautilus_trader/trading/__init__.pyi:538-544); the emulator retains them when
releasing its LIMIT (crates/execution/src/order_emulator/emulator.rs:1368-1376,
:1531, :1590-1594). The boundary case runs the real emulator, records the original
submit's mark and checks the released order through the adapter's pure request
builder. It makes no claim about an Alpaca acceptance or fill.

The method seams reuse test_mover's in-memory driver; no catalog, ledger, paper
state or network is used. All behavior under test is Mover's own.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import extended_hours_denial
from canon_alpaca.execution import extended_hours_param
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TriggerType

from canon_trading import mover
from tests import test_mover as base
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import Seam
from tests.test_mover import StoppedMidway
from tests.test_mover import seam_slot

NEW_YORK = ZoneInfo("America/New_York")
START = int(datetime(2026, 11, 13, 15, 54, tzinfo=NEW_YORK).timestamp()) * SECOND
ARM = START + 5 * MINUTE  # 15:59 New York
RELEASE = START + 7 * MINUTE  # 16:01 New York


class RecordedMover(StoppedMidway):
    """Observe submission and release without changing their routing or parameters."""

    def __init__(self, config: mover.MoverConfig) -> None:
        super().__init__(config)
        self.submissions: dict[str, tuple[int, dict | None]] = {}
        self.release_times: dict[str, int] = {}

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submissions[order.client_order_id.value] = (
            self.clock.timestamp_ns(),
            None if params is None else dict(params),
        )
        super().submit_order(order, position_id, client_id, params)

    def on_order_released(self, event) -> None:
        self.release_times[event.client_order_id.value] = event.ts_event
        super().on_order_released(event)


class RecordedSeam(Seam):
    def __init__(self, bid: str, **rows: Any) -> None:
        super().__init__(bid, **rows)
        # Match Mover.__init__: the base seam leaves this unset even for extended nodes.
        self.submit_params = dict(mover.EXTENDED_HOURS) if self.settings.extended_hours else None
        self.params: list[dict | None] = []

    def submit_order(self, order: SimpleNamespace, params: Any = None) -> None:
        self.params.append(None if params is None else dict(params))
        super().submit_order(order, params)

    def cancel_order(self, client_order_id: Any) -> None:
        super().cancel_order(client_order_id)
        order = self.orders[client_order_id.value]
        if order.is_emulated:
            # The pinned emulator cancels its own held order synchronously.
            order.is_closed, order.is_open, order.is_emulated = True, False, False
            order.status = OrderStatus.CANCELED


def recording(**changes: Any) -> RecordedMover:
    return RecordedMover(base.strategy(**changes).settings)


def seam(bid: str, **rows: Any) -> Any:
    # The existing driver borrows Mover methods without inheriting native Strategy.
    return cast(Any, RecordedSeam(bid, **rows))


def quote_at(now: int, bid: str) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(f"{D(bid):.4f}"),
        Price.from_str(f"{D(bid) + D('0.02'):.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        now,
        now,
    )


@pytest.mark.parametrize("extended_hours", [False, True])
def test_trailing_sell_armed_at_1559_released_at_1601_is_marked(
    monkeypatch, capfd, extended_hours: bool
) -> None:
    monkeypatch.setattr(base, "T0", START)
    eng = base.engine()
    s = recording(extended_hours=extended_hours)
    try:
        orders = base.run(
            eng,
            s,
            base.ranker(base.rank((AAA, "0.25"))),
            base.tape(AAA, base.RISING[:6]),
            [quote_at(RELEASE, "10.28"), quote_at(RELEASE + SECOND, "10.28")],
        )
        buy, sell = orders
        armed_ns, params = s.submissions[sell.client_order_id.value]
        released_ns = s.release_times[sell.client_order_id.value]
        extended, denial = extended_hours_param(params)
        request = AlpacaExecutionClient._order_request(sell, extended)
        buy_params = s.submissions[buy.client_order_id.value][1]
    finally:
        eng.dispose()
        capfd.readouterr()

    assert ARM <= armed_ns < ARM + MINUTE
    assert released_ns == RELEASE
    assert (sell.side, sell.order_type.name, sell.time_in_force) == (
        OrderSide.SELL,
        "LIMIT",
        TimeInForce.DAY,
    )
    assert sell.status == OrderStatus.FILLED
    assert buy_params == ({"extended_hours": True} if extended_hours else None)
    assert denial is None and extended_hours_denial(sell) is None
    assert (params, request.extended_hours) == ({"extended_hours": True}, True)


@pytest.mark.parametrize("extended_hours", [False, True])
@pytest.mark.parametrize("source", ["followed price", "last trade"])
def test_held_stop_limit_is_marked(extended_hours: bool, source: str) -> None:
    host = seam("0", extended_hours=extended_hours)
    slot = seam_slot(host)
    host._protect(slot, D(5), ARM)
    assert host.sent == [] and slot.held_stop == D("9.80")

    if source == "followed price":
        host.bid = "9.79"
    else:
        host.cache.trade = lambda _: SimpleNamespace(
            price=Price.from_str("9.7900"), ts_event=ARM + SECOND, ts_init=ARM + SECOND
        )
    host._protect(slot, D(5), ARM + SECOND)

    assert [o.kind for o in host.sent] == ["LIMIT"]
    assert slot.exit_record is not None
    assert slot.exit_record.trigger_reason == f"held stop ({source})"
    assert host.params == [{"extended_hours": True}]


@pytest.mark.parametrize("extended_hours", [False, True])
def test_persistence_breach_limit_is_marked(extended_hours: bool) -> None:
    host = seam(
        "9.79", extended_hours=extended_hours, trigger=TriggerType.LAST_PRICE, breach_count=2
    )
    slot = seam_slot(host)
    host._protect(slot, D(5), ARM)
    assert host.sent == [] and slot.trail_trigger == D("9.80")
    slot.observations.extend([(ARM + SECOND, D("9.79")), (ARM + 2 * SECOND, D("9.78"))])
    host._protect(slot, D(5), ARM + 2 * SECOND)

    assert slot.exiting and [o.kind for o in host.sent] == ["LIMIT"]
    assert slot.exit_record is not None and slot.exit_record.trigger_reason is not None
    assert "breach confirmed by 2 prints" in slot.exit_record.trigger_reason
    assert host.params == [{"extended_hours": True}]


@pytest.mark.parametrize("extended_hours", [False, True])
def test_retained_trigger_limit_is_marked(extended_hours: bool) -> None:
    host = seam("10.20", extended_hours=extended_hours)
    slot = seam_slot(host)
    slot.trail_trigger = D("10.38")
    host._protect(slot, D(5), ARM)

    assert [o.kind for o in host.sent] == ["LIMIT"]
    assert slot.exit_record is not None
    assert slot.exit_record.trigger_reason == "retained trigger"
    assert host.params == [{"extended_hours": True}]


@pytest.mark.parametrize("extended_hours", [False, True])
def test_holding_average_crossing_limit_is_marked(extended_hours: bool) -> None:
    host = seam("10.20", extended_hours=extended_hours)
    slot = seam_slot(host)
    slot.sized_loss, slot.protection_anchor, slot.trail_trigger = D("0.07"), D("10.40"), D("10.33")
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.20"))]
    host._protect(slot, D(5), ARM)

    assert [o.kind for o in host.sent] == ["LIMIT"]
    assert slot.exit_record is not None
    assert slot.exit_record.trigger_reason == "held stop (holding average)"
    assert host.params == [{"extended_hours": True}]


@pytest.mark.parametrize("extended_hours", [False, True])
def test_unpriced_timeout_limit_is_marked(extended_hours: bool) -> None:
    host = seam("0", extended_hours=extended_hours)
    slot = seam_slot(host)
    host._protect(slot, D(5), ARM)
    assert host.sent == []
    host._protect(slot, D(5), ARM + mover.HOLD_MOST_SECS * SECOND)

    assert [o.kind for o in host.sent] == ["LIMIT"]
    assert slot.exit_record is not None
    assert slot.exit_record.trigger_reason == "unpriced_exit"
    assert host.params == [{"extended_hours": True}]


@pytest.mark.parametrize("extended_hours", [False, True])
def test_chase_resend_is_marked_after_cancel_acknowledgement(extended_hours: bool) -> None:
    host = seam("10.20", extended_hours=extended_hours, chase_secs=5)
    slot = seam_slot(host)
    slot.trail_trigger = D("10.38")
    host._protect(slot, D(5), ARM)
    first = host.sent[-1]
    host._protect(slot, D(5), ARM + SECOND)
    host.bid = "10.10"
    host._protect(slot, D(5), ARM + 6 * SECOND)
    assert host.canceled == [first.client_order_id.value] and len(host.sent) == 1
    host._protect(slot, D(5), ARM + 7 * SECOND)
    assert len(host.sent) == 1  # No new sell before the cancellation is terminal.
    first.is_closed, first.is_open, first.status = True, False, OrderStatus.CANCELED
    host._protect(slot, D(5), ARM + 8 * SECOND)

    assert [o.kind for o in host.sent] == ["LIMIT", "LIMIT"]
    assert host.sent[-1].price < first.price
    assert host.params == [{"extended_hours": True}] * 2


@pytest.mark.parametrize("extended_hours", [False, True])
@pytest.mark.parametrize("cause", ["quantity", "refusal"])
def test_remade_trailing_sell_is_marked(extended_hours: bool, cause: str) -> None:
    host = seam("10.50", extended_hours=extended_hours)
    slot = seam_slot(host)
    host._protect(slot, D(5), ARM)
    first = host.sent[-1]
    if cause == "refusal":
        host.refuse(first)
        host._protect(slot, D(5), ARM + 6 * SECOND)
    else:
        host._protect(slot, D(6), ARM + SECOND)

    assert [o.kind for o in host.sent] == ["TRAILING", "TRAILING"]
    assert host.sent[-1].quantity == Quantity.from_int(5 if cause == "refusal" else 6)
    assert host.params == [{"extended_hours": True}] * 2


def test_extended_wind_down_limit_keeps_its_mark(capfd) -> None:
    eng = base.engine()
    s = recording(extended_hours=True, wind_down_at=base.at(base.T0 + 6 * MINUTE))
    try:
        orders = base.run(
            eng, s, base.ranker(base.rank((AAA, "0.25"))), base.tape(AAA, base.RISING)
        )
        sells = [o for o in orders if o.side == OrderSide.SELL]
        params = [s.submissions[o.client_order_id.value][1] for o in sells]
    finally:
        eng.dispose()
        capfd.readouterr()

    assert [(o.order_type.name, o.status.name) for o in sells] == [
        ("TRAILING_STOP_LIMIT", "CANCELED"),
        ("LIMIT", "FILLED"),
    ]
    assert params == [{"extended_hours": True}] * 2


@pytest.mark.parametrize("managed_stop", [False, True])
def test_regular_market_exits_keep_the_native_route(capfd, managed_stop: bool) -> None:
    eng = base.engine()
    end = base.at(base.T0 + 6 * MINUTE)
    s = recording(
        manage_stop=managed_stop,
        wind_down_at=None if managed_stop else end,
        market_exit_reduce_only=False,
        market_exit_time_in_force=TimeInForce.DAY,
    )
    if managed_stop:
        s.stop_at = end
    try:
        orders = base.run(
            eng, s, base.ranker(base.rank((AAA, "0.25"))), base.tape(AAA, base.RISING)
        )
        markets = [o for o in orders if o.order_type.name == "MARKET"]
    finally:
        eng.dispose()
        capfd.readouterr()

    assert markets and all(o.side == OrderSide.SELL for o in markets)
    assert all(
        o.status == OrderStatus.FILLED and list(o.tags or []) == ["MARKET_EXIT"] for o in markets
    )
    # Native market_exit bypasses the Python submit hook. Its params are None at
    # the pin (crates/trading/src/strategy/mod.rs:1815-1824, :1946-1955).
    assert all(o.client_order_id.value not in s.submissions for o in markets)
