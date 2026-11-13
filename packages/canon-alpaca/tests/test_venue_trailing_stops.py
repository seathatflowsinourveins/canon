"""Venue trailing stops on a real offline node; no request reaches Alpaca.

Contracts: Alpaca Orders, Trailing Stop Orders (read 2026-11-12); alpaca-py c42add7c0a70
TrailingStopOrderRequest and ReplaceOrderRequest; NautilusTrader d53849b216e2 order factory,
Python adapter output, and Interactive Brokers' params["trailing_offset"] modify contract.
"""

import asyncio
import threading
import time
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.requests import TrailingStopOrderRequest
from canon_alpaca import execution
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import replaces_as_asked
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType

from . import test_execution_client as execution_tests
from .test_execution_client import AAPL
from .test_execution_client import build_node
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - fixture
from .test_execution_client import logged
from .test_execution_client import provider_config
from .test_execution_client import run
from .test_execution_client import run_with_client
from .test_execution_client import until_idle
from .test_execution_client import venue as venue  # noqa: PLC0414 - fixture
from .test_external_orders import IdleTrader
from .test_external_orders import trailing_order as outside_trailing_order


class TrailingTrader(IdleTrader):
    def on_start(self):
        super().on_start()
        # The pinned risk engine values a DEFAULT trailing stop from a trade (risk/engine/mod.rs:
        # 2367-2397), whereas the market-order tests only need a quote.
        self.subscribe_trades(AAPL)


@pytest.fixture
def trailing_enabled(monkeypatch, venue):
    settings: dict[str, Any] = {"submit_trailing_stops": True}

    def config():
        return AlpacaExecutionClientConfig(instrument_provider=provider_config(), **settings)

    monkeypatch.setattr(execution_tests, "exec_config", config)
    venue.trading.script = "accept"
    venue.clock_reads = []

    def clock():
        now = datetime.now(UTC)
        venue.clock_reads.append(now)
        return AlpacaClock(
            timestamp=now, is_open=True, next_open=now, next_close=now + timedelta(hours=1)
        )

    monkeypatch.setattr(venue.trading, "get_clock", clock, raising=False)
    return settings


def trailing_order(strategy, **changes):
    fields = {
        "instrument_id": AAPL,
        "order_side": OrderSide.SELL,
        "quantity": Quantity.from_int(2),
        "trailing_offset": Decimal("1.25"),
        "trailing_offset_type": TrailingOffsetType.PRICE,
        "time_in_force": TimeInForce.GTC,
    }
    fields.update(changes)
    return strategy.order_factory.trailing_stop_market(**fields)


def modify(order, **changes):
    fields = {
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "quantity": None,
        "price": None,
        "trigger_price": None,
        "params": None,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_trailing_submit_setting_is_off_by_default_and_boolean():
    assert AlpacaExecutionClientConfig().submit_trailing_stops is False
    assert AlpacaExecutionClientConfig(submit_trailing_stops=True).submit_trailing_stops is True
    with pytest.raises(TypeError, match="submit_trailing_stops must be a bool"):
        AlpacaExecutionClientConfig(submit_trailing_stops=cast(Any, 1))


def test_with_the_setting_off_a_trailing_stop_is_denied_by_name_and_nothing_is_asked_or_sent(
    venue, trailing_enabled
):
    """The default: no trailing stop leaves the node, and Alpaca's clock is not even read for one."""
    trailing_enabled["submit_trailing_stops"] = False
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert "submit_trailing_stops is off" in strategy.events[-1].reason
        assert "OrderSubmitted" not in strategy.names()
        assert venue.trading.submitted == [] and venue.clock_reads == []

    run_with_client(body, strategy)


@pytest.mark.parametrize("tif", [TimeInForce.DAY, TimeInForce.GTC])
@pytest.mark.parametrize(
    "kind,offset,field,value",
    [
        (TrailingOffsetType.PRICE, "1.25", "trail_price", 1.25),
        (TrailingOffsetType.BASIS_POINTS, "125", "trail_percent", 1.25),
        (TrailingOffsetType.BASIS_POINTS, "12.5", "trail_percent", 0.125),
    ],
)
def test_submit_sends_exactly_one_venue_trail(
    venue, trailing_enabled, tif, kind, offset, field, value
):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(
            strategy, trailing_offset_type=kind, trailing_offset=Decimal(offset), time_in_force=tif
        )
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.ACCEPTED
        (request,) = venue.trading.submitted
        assert isinstance(request, TrailingStopOrderRequest)
        wire = request.to_request_fields()
        assert wire[field] == value
        assert ("trail_percent" if field == "trail_price" else "trail_price") not in wire
        assert wire["type"] == "trailing_stop" and wire["side"] == "sell"
        assert wire["time_in_force"] == tif.name.lower()
        assert wire["client_order_id"] == order.client_order_id.value and wire["qty"] == 2
        assert "extended_hours" not in wire

    run_with_client(body, strategy)


@pytest.mark.parametrize(
    "boundary", ["before_open", "at_close", "weekend", "stale_open", "slow_read"]
)
def test_trailing_submit_refuses_a_closed_regular_session(
    venue, trailing_enabled, monkeypatch, boundary
):
    now = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)
    timestamp = {
        "before_open": now.replace(hour=14, minute=29, second=59),
        "weekend": now.replace(day=14, hour=15),
    }.get(boundary, now)
    is_open = boundary in ("stale_open", "slow_read")

    def clock():
        if boundary == "slow_read":
            time.sleep(0.03)
        return AlpacaClock(
            timestamp=timestamp,
            is_open=is_open,
            next_open=timestamp + timedelta(days=1),
            next_close=timestamp + timedelta(milliseconds=10 if boundary == "slow_read" else 0),
        )

    monkeypatch.setattr(venue.trading, "get_clock", clock)
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert "regular session is not open" in strategy.events[-1].reason
        assert "OrderSubmitted" not in strategy.names() and venue.trading.submitted == []

    run_with_client(body, strategy)


@pytest.mark.parametrize("answer", ["error", "raw"])
def test_a_trailing_stop_is_not_sent_when_the_clock_cannot_be_verified(
    venue, trailing_enabled, monkeypatch, answer
):
    def clock():
        if answer == "error":
            raise OSError("offline clock failed")
        return {"is_open": True}

    monkeypatch.setattr(venue.trading, "get_clock", clock)
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert "trailing stop regular session could not be verified" in strategy.events[-1].reason
        assert "OrderSubmitted" not in strategy.names() and venue.trading.submitted == []

    run_with_client(body, strategy)


@pytest.mark.parametrize(
    "fields,params,reason",
    [
        ({"order_side": OrderSide.BUY}, None, "trailing stop BUY"),
        ({"activation_price": Price.from_str("150")}, None, "activation price"),
        ({"trigger_price": Price.from_str("149")}, None, "initial trigger price"),
        ({"trigger_type": TriggerType.LAST_PRICE}, None, "trigger type LAST_PRICE"),
        ({"trigger_type": TriggerType.BID_ASK}, None, "trigger type BID_ASK"),
        ({"trailing_offset_type": TrailingOffsetType.TICKS}, None, "trailing offset type TICKS"),
        ({"time_in_force": TimeInForce.IOC}, None, "time in force IOC"),
        ({}, {"trail_price": 1, "trail_percent": 1}, "order params"),
        ({}, {"extended_hours": True}, "extended_hours"),
    ],
)
def test_unsupported_trailing_submit_is_denied_by_name(
    venue, trailing_enabled, fields, params, reason
):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy, **fields)
        strategy.submit_order(order, params=params)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        denied = [event for event in strategy.events if type(event).__name__ == "OrderDenied"]
        assert len(denied) == 1 and reason in denied[0].reason
        assert venue.trading.submitted == []
        assert "OrderSubmitted" not in strategy.names()

    run_with_client(body, strategy)


@pytest.mark.parametrize("scope", [None, [OrderType.TRAILING_STOP_MARKET]])
def test_reduce_only_trailing_sell_uses_the_explicit_intent_scope(venue, trailing_enabled, scope):
    trailing_enabled["reduce_only_sells"] = True
    if scope is not None:
        trailing_enabled["sell_to_close_order_types"] = scope
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy, reduce_only=True)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        if scope is None:
            assert held.status == OrderStatus.DENIED
            assert (
                "reduce-only TRAILING_STOP_MARKET SELL is not supported"
                in strategy.events[-1].reason
            )
            assert venue.trading.submitted == []
        else:
            assert held.status == OrderStatus.ACCEPTED and held.is_reduce_only
            assert (
                venue.trading.submitted[0].to_request_fields()["position_intent"] == "sell_to_close"
            )

    run_with_client(body, strategy)


@pytest.mark.parametrize("kind", [TrailingOffsetType.PRICE, TrailingOffsetType.BASIS_POINTS])
@pytest.mark.parametrize("quantity_change", [False, True])
def test_trailing_offset_modify_uses_the_existing_replace_chain(
    venue, trailing_enabled, kind, quantity_change
):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy, trailing_offset_type=kind)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        old_id = held.venue_order_id
        offset = "2.5" if kind == TrailingOffsetType.PRICE else "250"
        params = {"trailing_offset": offset}
        if quantity_change:
            # Strategy.modify_order at this pin requires a quantity/price/trigger change too.
            strategy.modify_order(
                order.client_order_id, quantity=Quantity.from_int(3), params=params
            )
        else:
            # Test the documented adapter hook with an offset-only ModifyOrder payload.
            await client._modify_order(modify(held, params=params))
        await until_idle(client, venue)
        ((target, request),) = venue.trading.replace_requests
        assert target == old_id.value and request.trail == 2.5
        assert request.qty == (3 if quantity_change else None)
        assert request.limit_price is None and request.stop_price is None
        assert request.client_order_id is None
        updated = client.cache.order(order.client_order_id)
        assert updated.venue_order_id != old_id
        assert updated.quantity == Quantity.from_int(3 if quantity_change else 2)
        report = await client._order_report(
            venue.trading.get_order_by_id(updated.venue_order_id.value)
        )
        assert report.client_order_id == order.client_order_id
        assert report.order_type == OrderType.TRAILING_STOP_MARKET
        assert report.trailing_offset == Decimal(offset) and report.trailing_offset_type == kind
        assert strategy.names().count("OrderUpdated") == 1
        strategy.cancel_order(order.client_order_id)
        await until_idle(client, venue)
        assert venue.trading.cancels == [updated.venue_order_id.value]
        assert client.cache.order(order.client_order_id).status == OrderStatus.CANCELED

    run_with_client(body, strategy)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"params": {"trail_price": 2, "trail_percent": 2}}, "modify params"),
        (
            {"params": {"trailing_offset": "2", "trailing_offset_type": "BASIS_POINTS"}},
            "modify params",
        ),
        ({"params": {"trailing_offset": "NaN"}}, "trailing offset"),
        ({"params": {"trailing_offset": "Infinity"}}, "trailing offset"),
        ({"params": {"trailing_offset": "0"}}, "trailing offset"),
        ({"params": {"trailing_offset": "-1"}}, "trailing offset"),
        ({"params": {"trailing_offset": "1e-1000"}}, "trailing offset"),
        ({"price": Price.from_str("152")}, "no limit price"),
        ({"trigger_price": Price.from_str("149")}, "no stop price"),
        ({}, "trailing modify needs"),
    ],
)
def test_unsupported_trailing_modify_is_rejected_before_transport(
    venue, trailing_enabled, changes, reason
):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        await client._modify_order(modify(held, **changes))
        await until_idle(client, venue)
        rejected = [e for e in strategy.events if type(e).__name__ == "OrderModifyRejected"]
        assert len(rejected) == 1 and reason in rejected[0].reason
        assert venue.trading.replace_requests == []
        assert client.cache.order(order.client_order_id).status == OrderStatus.ACCEPTED

    run_with_client(body, strategy)


@pytest.mark.parametrize("field", ["trail_price", "trail_percent"])
def test_a_refused_replace_must_match_the_trail_before_being_confirmed(alpaca, field):
    order = SimpleNamespace(
        quantity=Quantity.from_int(2),
        trailing_offset_type=(
            TrailingOffsetType.PRICE if field == "trail_price" else TrailingOffsetType.BASIS_POINTS
        ),
    )
    raw = outside_trailing_order(alpaca, field)
    replacement = AlpacaOrder(**raw)
    assert not replaces_as_asked(replacement, ReplaceOrderRequest(trail=2.5), order)
    assert replaces_as_asked(replacement, ReplaceOrderRequest(trail=1.25), order)


def test_an_overflowing_basis_point_modify_gets_a_named_rejection(venue, trailing_enabled):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy, trailing_offset_type=TrailingOffsetType.BASIS_POINTS)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        await client._modify_order(modify(held, params={"trailing_offset": "1e1000002"}))
        await until_idle(client, venue)
        rejected = [e for e in strategy.events if type(e).__name__ == "OrderModifyRejected"]
        assert len(rejected) == 1 and "trailing offset" in rejected[0].reason
        assert venue.trading.replace_requests == []

    run_with_client(body, strategy)


def test_a_trailing_modify_also_requires_an_open_regular_session(
    venue, trailing_enabled, monkeypatch
):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        client._session_clock = None  # the open answer would be reused until its own close
        monkeypatch.setattr(venue.trading, "get_clock", closed_clock)
        held = client.cache.order(order.client_order_id)
        await client._modify_order(modify(held, params={"trailing_offset": "2"}))
        await until_idle(client, venue)
        rejected = [e for e in strategy.events if type(e).__name__ == "OrderModifyRejected"]
        assert len(rejected) == 1 and "regular session is not open" in rejected[0].reason
        assert venue.trading.replace_requests == []
        strategy.cancel_order(order.client_order_id)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.CANCELED

    run_with_client(body, strategy)


def closed_clock():
    now = datetime.now(UTC)
    return AlpacaClock(
        timestamp=now,
        is_open=False,
        next_open=now + timedelta(days=1),
        next_close=now + timedelta(days=1),
    )


def test_a_quantity_only_replace_of_a_resting_trailing_stop_needs_no_open_session(
    venue, trailing_enabled, monkeypatch
):
    """A GTC stop rests through the night; after part of the holding is sold outside the regular
    session its quantity must come down, or it oversells at the open. Only a trail change is gated."""
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy, quantity=Quantity.from_int(4))
        strategy.submit_order(order)
        await until_idle(client, venue)
        client._session_clock = None
        monkeypatch.setattr(venue.trading, "get_clock", closed_clock)
        strategy.modify_order(order.client_order_id, quantity=Quantity.from_int(2))
        await until_idle(client, venue)
        ((_, request),) = venue.trading.replace_requests
        assert request.qty == 2 and request.trail is None
        assert "OrderModifyRejected" not in strategy.names()
        assert client.cache.order(order.client_order_id).quantity == Quantity.from_int(2)

    run_with_client(body, strategy)


def test_with_submitting_off_only_the_trail_change_of_a_trailing_stop_is_refused(
    venue, trailing_enabled
):
    """The quantity replace of a trailing stop the node holds worked before this setting existed (an
    outside one, reported on request); the setting gates the trail parameter and nothing else."""
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        client._submit_trailing_stops = False
        quantity = modify(held, quantity=Quantity.from_int(1))
        assert client._modify_denial(held, quantity, held.venue_order_id) is None
        trail = modify(held, params={"trailing_offset": "2"})
        assert "submit_trailing_stops is off" in client._modify_denial(
            held, trail, held.venue_order_id
        )

    run_with_client(body, strategy)


def test_a_clock_answer_is_reused_until_its_own_boundary(venue, trailing_enabled, monkeypatch):
    """One read serves every trailing stop until the session it describes ends: a second submit asks
    nothing, and a closed answer refuses the next one without a second read."""
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        for _ in range(2):
            strategy.submit_order(trailing_order(strategy))
            await until_idle(client, venue)
        assert len(venue.trading.submitted) == 2 and len(venue.clock_reads) == 1
        client._session_clock = None
        reads = []

        def closed():
            reads.append(1)
            return closed_clock()

        monkeypatch.setattr(venue.trading, "get_clock", closed)
        for _ in range(2):
            order = trailing_order(strategy)
            strategy.submit_order(order)
            await until_idle(client, venue)
            assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert len(venue.trading.submitted) == 2 and reads == [1]

    run_with_client(body, strategy)


def test_a_kept_open_answer_is_not_trusted_past_its_close_on_the_wall_clock(
    venue, trailing_enabled, monkeypatch
):
    """A machine that slept: the monotonic clock stood still while the wall clock ran past the close
    the kept answer names. The next trailing stop asks Alpaca again, and is refused on its answer."""
    strategy = TrailingTrader()
    slept = {"secs": 0.0}
    monkeypatch.setattr(
        execution, "time", SimpleNamespace(time=lambda: time.time() + slept["secs"]), raising=False
    )

    async def body(client):
        await until_idle(client, venue)
        strategy.submit_order(trailing_order(strategy))
        await until_idle(client, venue)
        assert len(venue.trading.submitted) == 1 and len(venue.clock_reads) == 1
        slept["secs"] = 2 * 3600.0
        reads = []

        def closed():
            reads.append(1)
            return closed_clock()

        monkeypatch.setattr(venue.trading, "get_clock", closed)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert reads == [1]
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert len(venue.trading.submitted) == 1

    run_with_client(body, strategy)


def test_a_client_with_no_clock_denies_a_trailing_stop_by_name(
    venue, trailing_enabled, monkeypatch
):
    monkeypatch.delattr(venue.trading, "get_clock", raising=False)
    monkeypatch.setattr(
        type(venue.trading), "get_clock", property(lambda _self: missing()), raising=False
    )
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert "could not be verified" in strategy.events[-1].reason
        assert venue.trading.submitted == []

    run_with_client(body, strategy)


def missing():
    raise AttributeError("get_clock")


def test_a_cancel_sent_during_the_clock_read_is_applied_after_the_submit_and_ends_the_order(
    venue, trailing_enabled, monkeypatch, capfd
):
    """The clock read is awaited before the order is marked submitted. The node hands this client
    its commands one at a time, so a cancel sent in that window is not lost against an order
    Alpaca does not hold yet: it runs after the submit and cancels the accepted order by its ID."""
    release = threading.Event()
    entered = threading.Event()

    def slow_clock():
        entered.set()
        release.wait(5)
        now = datetime.now(UTC)
        return AlpacaClock(
            timestamp=now, is_open=True, next_open=now, next_close=now + timedelta(hours=1)
        )

    monkeypatch.setattr(venue.trading, "get_clock", slow_clock)
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        while not entered.is_set():
            await asyncio.sleep(0.005)
        strategy.cancel_order(order.client_order_id)
        await asyncio.sleep(0.05)
        assert venue.trading.submitted == [] and venue.trading.cancels == []
        release.set()
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        assert len(venue.trading.submitted) == 1
        assert venue.trading.cancels == [held.venue_order_id.value]
        assert held.status == OrderStatus.CANCELED

    run_with_client(body, strategy)
    assert "left as it is" not in logged(capfd)


@pytest.mark.parametrize("all_orders", [False, True])
def test_trailing_cancel_uses_the_existing_venue_cancel_path(venue, trailing_enabled, all_orders):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        venue_id = client.cache.order(order.client_order_id).venue_order_id
        if all_orders:
            strategy.cancel_all_orders(AAPL)
        else:
            strategy.cancel_order(order.client_order_id)
        await until_idle(client, venue)
        assert venue.trading.cancels == [venue_id.value]
        assert client.cache.order(order.client_order_id).status == OrderStatus.CANCELED
        assert strategy.names()[-2:] == ["OrderPendingCancel", "OrderCanceled"]

    run_with_client(body, strategy)


@pytest.mark.parametrize(
    "identity",
    [
        "same",
        "same_market",
        "child_replaces",
        "child_replaces_trailing",
    ],
)
def test_triggered_market_fills_stay_on_the_nodes_trailing_order(venue, trailing_enabled, identity):
    """The two forms the adapter's existing paths cover: the fill arrives under the order's own venue
    ID, whatever type Alpaca then reports, or under a new one that names the old in `replaces`."""
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        original_id = held.venue_order_id.value
        raw = dict(venue.trading._placed[original_id])
        if identity not in ("same", "child_replaces_trailing"):
            raw.update(type="market", order_type="market", trail_price=None, trail_percent=None)
        if identity.startswith("child"):
            raw["id"] = str(uuid.uuid4())
            if identity.startswith("child_replaces"):
                raw.update(client_order_id=str(uuid.uuid4()), replaces=original_id)
                venue.trading._placed[original_id].update(status="replaced", replaced_by=raw["id"])
        venue.trading._placed[raw["id"]] = raw
        first = venue.trading.update(
            raw["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
        )
        await client._apply_trade_update(TradeUpdate(**first))
        await client._apply_trade_update(TradeUpdate(**first))
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        assert held.filled_qty == Quantity.from_int(1)
        assert held.venue_order_id.value == raw["id"]
        # The pinned model explicitly rejects OrderTriggered for market-style stops
        # (model/src/orders/mod.rs:116-124, :874-878). Election is visible through fills.
        assert strategy.names().count("OrderTriggered") == 0
        assert strategy.names().count("OrderFilled") == 1
        last = venue.trading.update(
            raw["id"], "fill", 1, deliver=False, status="filled", filled_qty="2"
        )
        await client._apply_trade_update(TradeUpdate(**last))
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        assert held.status == OrderStatus.FILLED and held.filled_qty == Quantity.from_int(2)
        assert strategy.names().count("OrderTriggered") == 0
        assert strategy.names().count("OrderFilled") == 2
        assert {str(t) for t in held.trade_ids} == {first["execution_id"], last["execution_id"]}
        assert len(client.cache.orders()) == 1

    run_with_client(body, strategy)


def test_a_trailing_report_updates_the_venues_advisory_stop(venue, trailing_enabled):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        raw = venue.trading._placed[held.venue_order_id.value]
        raw.update(hwm="152.00", stop_price="150.75")
        report = await client._send_report(AlpacaOrder(**raw))
        await until_idle(client, venue)
        assert report is not None and report.trigger_price == Price.from_str("150.75")
        assert report.trailing_offset == Decimal("1.25")
        assert report.trailing_offset_type == TrailingOffsetType.PRICE
        assert client.cache.order(order.client_order_id).trigger_price == Price.from_str("150.75")
        assert "OrderUpdated" in strategy.names()

    run_with_client(body, strategy)


@pytest.mark.parametrize(
    "event,status",
    [
        ("expired", OrderStatus.EXPIRED),
        ("rejected", OrderStatus.REJECTED),
    ],
)
def test_trailing_terminal_updates_end_the_nodes_order(venue, trailing_enabled, event, status):
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        venue.trading.update(held.venue_order_id.value, event, status=event)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == status
        assert (
            strategy.names().count("OrderExpired" if event == "expired" else "OrderRejected") == 1
        )

    run_with_client(body, strategy)


@pytest.mark.parametrize("kind", ["market", "trailing_stop"])
def test_a_fill_under_a_new_venue_id_with_no_link_is_counted_and_the_order_keeps_its_own_id(
    venue, trailing_enabled, kind
):
    """Alpaca does not document which ID the order a trigger creates carries. If it were a new venue
    ID with the same client order ID and no `replaces`, the adapter adopts nothing: the existing rule
    for an unknown leg with no link counts its fills on the node's order, which keeps its own venue
    ID. UNVERIFIED as venue behaviour; the paper read settles it (ADR 0129)."""
    strategy = TrailingTrader()

    async def body(client):
        await until_idle(client, venue)
        order = trailing_order(strategy)
        strategy.submit_order(order)
        await until_idle(client, venue)
        original_id = client.cache.order(order.client_order_id).venue_order_id.value
        raw = dict(venue.trading._placed[original_id])
        raw["id"] = str(uuid.uuid4())
        if kind == "market":
            raw.update(type="market", order_type="market", trail_price=None, trail_percent=None)
        venue.trading._placed[raw["id"]] = raw
        first = venue.trading.update(
            raw["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
        )
        await client._apply_trade_update(TradeUpdate(**first))
        await client._apply_trade_update(TradeUpdate(**first))
        await until_idle(client, venue)
        held = client.cache.order(order.client_order_id)
        assert held.filled_qty == Quantity.from_int(1)
        assert held.venue_order_id.value == original_id
        assert strategy.names().count("OrderFilled") == 1
        assert len(client.cache.orders()) == 1

    run_with_client(body, strategy)


@pytest.mark.parametrize("replacement", [False, True])
def test_startup_reconciles_an_open_trailing_stop_to_the_claiming_strategy_when_reports_are_on(
    venue, alpaca, trailing_enabled, replacement
):
    """After a start with no cache nothing tells this node's open trailing stop from another's (the
    trader's tag is the same on every node here), so it is mapped only with
    `report_external_trailing_stops`, as any outside one is, and goes to the strategy that claims
    the instrument. A node that submits venue stops and may start cold turns both settings on."""
    trailing_enabled["report_external_trailing_stops"] = True
    strategy = execution_tests.trader(submit=False)
    root_id = "O-20261112-143000-001-000-9"
    raw = outside_trailing_order(alpaca, "trail_price")
    raw.update(client_order_id=root_id, hwm="148.5")
    if replacement:
        old = dict(raw)
        raw = {
            **raw,
            "id": str(uuid.uuid4()),
            "client_order_id": str(uuid.uuid4()),
            "replaces": old["id"],
        }
        old.update(status="replaced", replaced_by=raw["id"])
        venue.trading.orders = [old, raw]
    else:
        venue.trading.orders = [raw]
    node = build_node(strategy)
    strategy.set_external_order_instrument_ids([AAPL])

    run(node)

    held = node.cache.order(ClientOrderId(root_id))
    assert strategy.started and held is not None
    assert held.strategy_id == strategy.strategy_id
    assert held.order_type == OrderType.TRAILING_STOP_MARKET and held.status == OrderStatus.ACCEPTED
    assert held.venue_order_id.value == raw["id"]
    assert held.trailing_offset == Decimal("1.25")
    assert venue.trading.submitted == []


@pytest.mark.parametrize(
    "cid",
    [
        "placed-elsewhere",
        "O-20261112-143000-999-000-9",
        # another node on the same account: every node here is TESTER-001, so the trader's tag alone
        # says nothing about whose order it is
        "O-20261112-143000-001-007-3",
    ],
)
def test_submitting_opt_in_does_not_claim_an_outside_trailing_stop(
    venue, alpaca, trailing_enabled, capfd, cid
):
    raw = outside_trailing_order(alpaca, "trail_price")
    raw["client_order_id"] = cid
    venue.trading.orders = [raw]
    strategy = execution_tests.trader(submit=False)

    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(build_node(strategy))

    assert not strategy.started
    assert "is open with order type trailing_stop" in logged(capfd)
    assert venue.trading.submitted == []
