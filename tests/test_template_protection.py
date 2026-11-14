"""Unit 12: the shipped EMA-cross template forwards position events to shared protection.

All replays use the in-memory engine and invented quotes. No account or disk state is used.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from canon_alpaca.execution import SELL_TO_CLOSE_PROVEN
from canon_alpaca.execution import AlpacaExecutionClient
from nautilus_trader.core import UUID4
from nautilus_trader.model import AccountId
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderAccepted
from nautilus_trader.model import OrderPendingCancel
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderSubmitted
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId

from canon_trading.mover import MoverConfig
from canon_trading.mover import TrailPlan
from canon_trading.mover import fill_trigger
from canon_trading.mover import offsets
from canon_trading.price_step import tick
from canon_trading.session_window import DEFAULT_ARM
from canon_trading.session_window import TRAIL_ARMS
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import MOVER_ID
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import quote
from tests.test_mover import tape

SID = StrategyId("Protected-001")


def module() -> Any:
    return importlib.import_module("canon_trading.ema_cross_protected")


def template(*, entry_limit=None, flatten_on_quote=None, start_entry=True, arm_on_quote=None):
    implementation = module()

    class Probe(implementation.EMACrossProtected):
        def __init__(self, config):
            super().__init__(config)
            self.sent = []
            self.canceled = []
            self.coverage = []
            self.quotes = 0
            self.pending_open = None
            self.stop_calls = []

        def cancel_all_orders(self, instrument_id, *args, **kwargs):
            self.stop_calls.append(("cancel", instrument_id))
            super().cancel_all_orders(instrument_id, *args, **kwargs)

        def close_all_positions(self, instrument_id, *args, **kwargs):
            self.stop_calls.append(("close", instrument_id))
            super().close_all_positions(instrument_id, *args, **kwargs)

        def modify_order(self, client_order_id, *args, **kwargs):
            if not self.is_running():
                order = self.cache.order(client_order_id)
                self.stop_calls.append(("close", order.instrument_id))
            super().modify_order(client_order_id, *args, **kwargs)

        def on_start(self):
            super().on_start()
            if not start_entry:
                return
            if entry_limit is None:
                order = self.order_factory.market(AAA, OrderSide.BUY, Quantity.from_int(10))
            else:
                order = self.order_factory.limit(
                    AAA,
                    OrderSide.BUY,
                    Quantity.from_int(10),
                    Price.from_str(entry_limit),
                    time_in_force=TimeInForce.DAY,
                )
            self.submit_order(order)

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.sent.append(
                {
                    "id": order.client_order_id,
                    "side": order.side,
                    "quantity": order.quantity.as_decimal(),
                    "reduce_only": order.is_reduce_only,
                    "params": params,
                    "type": order.order_type,
                }
            )
            super().submit_order(order, position_id, client_id, params)
            active = working(self.cache)
            self.coverage.append(
                (
                    len(active),
                    sum((o.leaves_qty.as_decimal() for o in active), Decimal(0)),
                    self.portfolio.net_position(AAA),
                )
            )

        def cancel_order(self, client_order_id, client_id=None, params=None):
            self.canceled.append(client_order_id)
            super().cancel_order(client_order_id, client_id, params)

        def on_quote(self, tick):
            if self.quotes + 1 == arm_on_quote:
                super().on_position_opened(self.pending_open)
            super().on_quote(tick)
            self.quotes += 1
            if self.quotes == flatten_on_quote:
                self.submit_order(
                    self.order_factory.market(
                        AAA, OrderSide.SELL, Quantity.from_int(10), reduce_only=True
                    )
                )

        def on_position_opened(self, event):
            if arm_on_quote is None:
                super().on_position_opened(event)
            else:
                self.pending_open = event

    return Probe(
        implementation.EMACrossProtectedConfig(
            instrument_id=AAA,
            bar_type=bar_type(AAA),
            trade_size=Decimal(10),
            fast_ema_period=2,
            slow_ema_period=3,
            strategy_id=SID,
        )
    )


def working(cache):
    return [
        order
        for order in cache.orders(strategy_id=SID)
        if order.side == OrderSide.SELL and not order.is_closed
    ]


def protective(strategy):
    return [
        sent
        for sent in strategy.sent
        if sent["side"] == OrderSide.SELL and sent["type"] != OrderType.MARKET
    ]


@pytest.fixture
def drive(capfd):
    engines = []

    def run(strategy, data, *, liquid=False):
        eng = engine(liquid=liquid)
        engines.append(eng)
        eng.add_data(data)
        eng.add_strategy(strategy)
        eng.run(streaming=True)
        capfd.readouterr()
        return eng

    yield run
    for eng in engines:
        eng.end()
        eng.dispose()


def test_entry_fill_arms_one_marked_reduce_only_sell_from_the_average(drive):
    strategy = template()
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01)])
    (sent,) = protective(strategy)
    assert sent["quantity"] == Decimal(10)
    assert sent["reduce_only"]
    assert sent["params"] == {"extended_hours": True}
    (order,) = working(eng.cache)
    defaults = MoverConfig(
        max_notional=Decimal(100), max_move=Decimal(1), **TRAIL_ARMS[DEFAULT_ARM]
    )
    distance = offsets(Decimal("10.01"), defaults.trail, defaults.exit_through)[0]
    assert order.trigger_price.as_decimal() == fill_trigger(Decimal("10.01"), distance)


@pytest.mark.parametrize("observations", [1, 2])
def test_only_the_first_arming_step_requires_a_second_observation(drive, observations):
    # A delayed opening callback first arms into a gap. The spread remains usable,
    # so OWN1's spread floor does not mask the breach of the level under the fill.
    strategy = template(arm_on_quote=2)
    data = [quote(0, 9.99, 10.01), quote(1, 9.00, 9.02)]
    if observations == 2:
        data.append(quote(2, 9.00, 9.02))
    eng = drive(strategy, data)
    expected = Decimal(10) if observations == 1 else Decimal(0)
    assert eng.portfolio.net_position(AAA) == expected
    filled = sum(
        (
            order.filled_qty.as_decimal()
            for order in eng.cache.orders(strategy_id=SID)
            if order.side == OrderSide.SELL
        ),
        Decimal(0),
    )
    assert filled == Decimal(10) - expected
    assert all(sent["reduce_only"] for sent in protective(strategy))
    assert all(sent["params"] == {"extended_hours": True} for sent in protective(strategy))


def test_a_partial_entry_has_one_sell_for_only_its_filled_part(drive):
    strategy = template(entry_limit="10.02")
    eng = drive(strategy, [quote(0, 9.99, 10.01, ask_size=4), quote(1, 10.09, 10.10)], liquid=True)
    (order,) = working(eng.cache)
    assert order.quantity.as_decimal() == Decimal(4)
    assert [sent["quantity"] for sent in protective(strategy)] == [Decimal(4)]


def test_later_fills_grow_cover_without_a_second_working_sell_at_any_submit(drive):
    strategy = template(entry_limit="10.02")
    eng = drive(
        strategy,
        [quote(0, 9.99, 10.01, ask_size=4), quote(1, 9.99, 10.01), quote(2, 10.09, 10.10)],
        liquid=True,
    )
    (order,) = working(eng.cache)
    assert order.quantity.as_decimal() == Decimal(10)
    assert protective(strategy)[0]["quantity"] == Decimal(4)
    assert all(count <= 1 and covered <= held for count, covered, held in strategy.coverage)


def test_closing_the_holding_removes_its_protection(drive):
    strategy = template(flatten_on_quote=3)
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 10.49, 10.51), quote(2, 10.99, 11.01)])
    assert eng.portfolio.net_position(AAA) == 0
    assert working(eng.cache) == []
    assert protective(strategy)[0]["id"] in strategy.canceled


def test_ema_entry_uses_the_same_default_protection(drive):
    strategy = template(start_entry=False)
    eng = drive(strategy, tape(AAA, [10.0, 10.1, 10.2, 10.3, 10.4]))
    assert len([sent for sent in strategy.sent if sent["side"] == OrderSide.BUY]) == 1
    assert len(protective(strategy)) == 1
    assert len(working(eng.cache)) == 1
    defaults = MoverConfig(
        max_notional=Decimal(100), max_move=Decimal(1), **TRAIL_ARMS[DEFAULT_ARM]
    )
    assert DEFAULT_ARM == "OWN1"
    for name in (
        "trail",
        "trail_mode",
        "trail_cap",
        "atr_k",
        "exit_through",
        "chase_cap",
        "chase_secs",
        "exit_ticks",
        "trigger",
    ):
        assert getattr(strategy.protection.config, name) == getattr(defaults, name)
    assert "acceptance.py:514-589" in module().__doc__
    assert "ema_cross_trailing_stop.py:376-385" in module().__doc__


def test_default_arm_uses_warm_atr_instead_of_the_fixed_two_percent_row(drive):
    strategy = template(start_entry=False)
    eng = drive(strategy, tape(AAA, [10.0 - i / 10 for i in range(16)] + [9.0, 9.1, 9.2]))
    (order,) = working(eng.cache)
    (position,) = eng.cache.positions_open(strategy_id=SID)
    assert order.trailing_offset < Decimal(str(position.avg_px_open)) * Decimal("0.02")
    slot = strategy.protection._context.slots[AAA]
    assert slot.trail_plan.bound == "atr"
    assert slot.trail_plan.source != "cold_atr_fallback"


def test_opening_before_a_quote_holds_the_stop_then_arms_the_default_plan():
    implementation = importlib.import_module("canon_trading.protection")
    host: Any = Seam("9.99")
    host.cache.order = host._order
    host.cache.instrument = host._instrument
    host.cache.bars = lambda _: []
    host.cache.quote = lambda _: None
    host.log = SimpleNamespace(info=host.said.append)
    host.positions = [SimpleNamespace(signed_qty=Decimal(10), avg_px_open=Decimal("10.01"))]
    part = implementation.Protection(host, implementation.ProtectionConfig())
    part.on_position_opened(
        SimpleNamespace(
            strategy_id=MOVER_ID,
            instrument_id=AAA,
            signed_qty=Decimal(10),
            opening_order_id=ClientOrderId("ENTRY"),
            avg_px_open=Decimal("10.01"),
            ts_init=T0,
        )
    )
    assert host.sent == []
    assert part._context.slots[AAA].held_stop is not None
    tick = quote(1, 9.99, 10.01)
    assert tick.ts_init == T0 + SECOND
    host.cache.quote = lambda _: tick
    part.on_quote(tick)
    assert len(host.sent) == 1
    assert host.sent[0].quantity.as_decimal() == Decimal(10)
    assert part._context.slots[AAA].trail_plan.source == "cold_atr_fallback"


def holding_part(*, pending_cancel=False):
    """Venue-cancel timing stood in; the production part owns every decision."""
    implementation = importlib.import_module("canon_trading.protection")
    host: Any = Seam("9.99")
    host.tick = quote(0, 9.99, 10.01)
    host.bars = []
    host.cache.order = host._order
    host.cache.instrument = host._instrument
    host.cache.bars = lambda _: host.bars
    host.cache.quote = lambda _: host.tick
    host.log = SimpleNamespace(info=host.said.append)

    def cancel(cid):
        host.canceled.append(cid.value)
        order = host._order(cid)
        order.is_pending_cancel = pending_cancel
        order.is_closed = not pending_cancel

    trailing = host.order_factory.trailing_stop_limit

    def arm(*args, **kwargs):
        order = trailing(*args, **kwargs)
        order.events = list
        order.side = OrderSide.SELL
        order.avg_px = None
        order.filled_qty = Quantity.from_int(0)
        return order

    host.cancel_order = cancel
    host.order_factory.trailing_stop_limit = arm
    return implementation.Protection(host, implementation.ProtectionConfig()), host


def opened(host, *, average="10.01", at=T0, entry="ENTRY"):
    host.positions = [SimpleNamespace(signed_qty=Decimal(10), avg_px_open=Decimal(average))]
    return SimpleNamespace(
        strategy_id=MOVER_ID,
        instrument_id=AAA,
        signed_qty=Decimal(10),
        opening_order_id=ClientOrderId(entry),
        avg_px_open=Decimal(average),
        ts_init=at,
    )


@pytest.mark.parametrize("source", ["spread", "atr", "rounding", "minimum_step"])
def test_r2_s3_a_filled_holding_over_the_ceiling_is_covered_and_named(source):
    part, host = holding_part()
    event = opened(host)
    if source == "spread":
        host.tick = quote(0, 9.0, 10.01)
    elif source == "atr":
        slot = part._position_slot(AAA, T0)
        slot.atr = SimpleNamespace(initialized=True, value=2)
    elif source == "rounding":
        event = opened(host, average="1.01")
        host.tick = quote(0, 1.01, 1.02)
        slot = part._position_slot(AAA, T0)
        slot.atr = SimpleNamespace(initialized=True, value=0.1001)
    else:
        event = opened(host, average="0.0001")
        host.tick = quote(0, 0.0001, 0.0001)
    part.on_position_opened(event)
    assert host.sent, "an already filled long must have a protective sell"
    assert any("distance_over_ceiling" in message for message in host.said)
    slot = part._context.slots[AAA]
    average = Decimal(str(event.avg_px_open))
    distance = slot.sized_loss
    assert distance <= average * Decimal("0.10")
    assert distance % tick(average) == 0
    if host.sent[-1].kind == "TRAILING":
        assert host.sent[-1].trailing_offset == distance
    else:
        assert host.sent[-1].kind == "LIMIT"
    assert host.sent[-1].quantity.as_decimal() == 10


@pytest.mark.parametrize("pending_cancel", [False, True])
def test_r3_reentry_has_a_fresh_plan_exit_and_first_observation_guard(pending_cancel):
    part, host = holding_part(pending_cancel=pending_cancel)
    first = opened(host)
    part.on_position_opened(first)
    old = part._context.slots[AAA]
    previous = host.sent[0]
    old.trail_trigger = Decimal(19)
    old.exit_trigger = Decimal(19)
    old.exit_record.trigger_reason = "previous holding"
    old_plan, old_record = old.trail_plan, old.exit_record
    host.positions = []
    part.on_position_closed(first)
    # A bar between holdings must not make the old slot eligible for reuse.
    part.on_bar(minute_bar(0))
    host.tick = quote(61, 19.0, 19.02)
    part.on_position_opened(opened(host, average="20", at=T0 + 61 * SECOND, entry="NEXT"))
    fresh = part._context.slots[AAA]
    assert fresh is not old
    assert fresh.trail_plan is not old_plan
    assert fresh.trail_trigger is None and fresh.exit_trigger is None
    assert fresh.exit_record is None or fresh.exit_record is not old_record
    assert fresh.entry_id == ClientOrderId("NEXT")
    assert len(host.sent) == 1, "a new holding must get its own first-step guard"
    previous.is_closed = True
    previous.is_pending_cancel = False
    if pending_cancel:
        host.tick = quote(62, 19.0, 19.02)
        part.on_quote(host.tick)
        assert len(host.sent) == 1, "the first step after the old cancel is still guarded"
    host.tick = quote(63, 19.0, 19.02)
    part.on_quote(host.tick)
    assert len(host.sent) == 2
    assert host.sent[-1].kind == "LIMIT"
    assert fresh.exit_record is not old_record
    assert fresh.trail_plan.trail != old_plan.trail or fresh.sized_loss != old.sized_loss


def minute_bar(index, *, internal=False, close="10.0000"):
    kind = BarType.from_str(f"{AAA}-1-MINUTE-LAST-{'INTERNAL' if internal else 'EXTERNAL'}")
    end = T0 + (index + 1) * MINUTE
    return Bar(
        kind,
        Price.from_str(close),
        Price.from_str(str(Decimal(close) + Decimal("0.01"))),
        Price.from_str(str(Decimal(close) - Decimal("0.01"))),
        Price.from_str(close),
        Quantity.from_int(1000),
        end if internal else end - MINUTE,
        end,
    )


def test_r4_the_first_partial_internal_minute_does_not_warm_the_atr():
    part, host = holding_part()
    slot = part._position_slot(AAA, T0 + 10 * SECOND)
    slot.subscribed_ns = T0 + 10 * SECOND
    part.on_bar(minute_bar(0, internal=True))
    assert slot.atr.count == 0
    assert list(slot.closes) == []
    assert any("partial INTERNAL minute" in message for message in host.said)
    part.on_bar(minute_bar(1, internal=True))
    assert slot.atr.count == 1
    assert list(slot.closes) == [Decimal(10)]


@pytest.mark.parametrize(
    "spec",
    ["1-MINUTE-LAST-INTERNAL", "5-MINUTE-LAST-EXTERNAL", "1-MINUTE-BID-EXTERNAL"],
)
def test_r5_a_template_without_external_minute_bars_is_refused_by_name(spec):
    with pytest.raises(ValueError, match="1-MINUTE-LAST-EXTERNAL"):
        module().EMACrossProtectedConfig(
            instrument_id=AAA,
            bar_type=BarType.from_str(f"{AAA}-{spec}"),
            trade_size=Decimal(10),
        )


def test_r6_stopping_cancels_orders_then_closes_positions(drive):
    strategy = template()
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01)])
    assert eng.portfolio.net_position(AAA) == 10
    eng.end()
    assert strategy.stop_calls == [("cancel", AAA), ("close", AAA)]
    assert working(eng.cache) == []
    assert eng.portfolio.net_position(AAA) == 0
    (sale,) = [order for order in eng.cache.orders(strategy_id=SID) if order.side == OrderSide.SELL]
    assert sale.order_type == OrderType.LIMIT
    assert sale.time_in_force == TimeInForce.DAY and sale.is_reduce_only
    adapter: Any = SimpleNamespace(
        _provider=SimpleNamespace(find=eng.cache.instrument),
        cache=eng.cache,
        _reduce_only_sells=True,
        _sell_to_close_order_types=SELL_TO_CLOSE_PROVEN,
    )
    assert AlpacaExecutionClient._denial(adapter, sale) is None


def test_r7_a_first_live_bar_already_in_the_cache_keeps_its_close_once():
    part, host = holding_part()
    first = minute_bar(0)
    host.bars = [first]
    part.on_bar(first)
    slot = part._context.slots[AAA]
    assert slot.atr.count == 1
    assert list(slot.closes) == [Decimal(10)]
    part.on_bar(first)
    assert slot.atr.count == 1
    assert list(slot.closes) == [Decimal(10)]
    part.on_bar(minute_bar(1, close="10.1000"))
    assert slot.atr.count == 2
    assert list(slot.closes) == [Decimal(10), Decimal("10.1")]


def test_r8_a_stop_already_held_sells_on_the_first_followed_breach():
    part, host = holding_part()
    host.tick = None
    part.on_position_opened(opened(host))
    slot = part._context.slots[AAA]
    assert slot.held_since == T0
    assert slot.held_stop is not None and host.sent == []
    host.tick = quote(1, 9.00, 9.02)
    part.on_quote(host.tick)
    assert [order.kind for order in host.sent] == ["LIMIT"]
    assert slot.exiting
    assert slot.exit_record.trigger_reason.startswith("held stop")


def entry_host(*, own_long=False, foreign_quantity=0):
    """Only cache/indicator inputs are stood in; entry and planning run production methods."""
    part, host = holding_part()
    host.protection = part
    host.settings = module().EMACrossProtectedConfig(
        instrument_id=AAA, bar_type=bar_type(AAA), trade_size=Decimal(10)
    )
    host.entry_id = None
    host.fast_ema = SimpleNamespace(value=11)
    host.slow_ema = SimpleNamespace(value=10)
    host.indicators_initialized = lambda: True
    host.portfolio = SimpleNamespace(
        is_net_flat=lambda _: (10 if own_long else 0) + foreign_quantity == 0
    )
    positions = []
    if own_long:
        positions.append(SimpleNamespace(strategy_id=MOVER_ID, signed_qty=Decimal(10)))
    if foreign_quantity:
        positions.append(
            SimpleNamespace(strategy_id=StrategyId("Other-001"), signed_qty=foreign_quantity)
        )
    host.cache.positions_open = lambda **kw: [
        position for position in positions if position.strategy_id == kw.get("strategy_id")
    ]
    host.order_factory.market = lambda instrument_id, side, quantity: host._new(
        "MARKET", quantity=quantity, side=side
    )
    return host


@pytest.mark.parametrize("source", ["spread", "atr", "rounding"])
def test_s3_entry_over_the_ceiling_is_refused_before_sending(source):
    host = entry_host()
    slot = host.protection._position_slot(AAA, T0)
    if source == "spread":
        host.tick = quote(0, 9.0, 10.01)
    elif source == "atr":
        slot.atr = SimpleNamespace(initialized=True, value=2, handle_bar=lambda _: None)
    else:
        host.tick = quote(0, 1.00, 1.01)
        slot.atr = SimpleNamespace(initialized=True, value=0.1001, handle_bar=lambda _: None)
    module().EMACrossProtected.on_bar(host, minute_bar(0))
    assert host.sent == [], "the entry itself must be refused, before a long exists"
    assert any("distance_over_ceiling" in message for message in host.said)


@pytest.mark.parametrize("own_long,foreign_quantity,entries", [(False, 10, 1), (True, -10, 0)])
def test_s4_entry_uses_only_its_own_open_positions(own_long, foreign_quantity, entries):
    host = entry_host(own_long=own_long, foreign_quantity=foreign_quantity)
    module().EMACrossProtected.on_bar(host, minute_bar(0))
    assert len(host.sent) == entries


@pytest.mark.parametrize("size", ["1.5", "0.4", "0", "-1", "NaN", "Infinity"])
def test_s5_trade_size_must_be_finite_positive_whole_shares(size):
    with pytest.raises(ValueError, match="trade_size"):
        module().EMACrossProtectedConfig(
            instrument_id=AAA, bar_type=bar_type(AAA), trade_size=Decimal(size)
        )


@pytest.mark.parametrize("size", [1, 1.0, True, "1"])
def test_s5_trade_size_refuses_other_types_by_name(size):
    with pytest.raises(TypeError, match="trade_size"):
        module().EMACrossProtectedConfig(instrument_id=AAA, bar_type=bar_type(AAA), trade_size=size)


def test_s6_missing_instrument_raises_its_name_without_stopping():
    stopped = []
    host = SimpleNamespace(
        settings=SimpleNamespace(instrument_id=AAA),
        cache=SimpleNamespace(instrument=lambda _: None),
        log=SimpleNamespace(error=lambda _: None),
        stop=lambda: stopped.append(True),
    )
    with pytest.raises(RuntimeError, match="AAA.ALPACA"):
        module().EMACrossProtected.on_start(host)
    assert stopped == []


@pytest.mark.parametrize("fast,slow", [(20, 10), (10, 10), (0, 10), (-1, 10), (1, 0)])
def test_s8_ema_periods_must_be_positive_and_ordered(fast, slow):
    with pytest.raises(ValueError, match="fast_period.*slow_period"):
        module().EMACrossProtectedConfig(
            instrument_id=AAA,
            bar_type=bar_type(AAA),
            trade_size=Decimal(10),
            fast_ema_period=fast,
            slow_ema_period=slow,
        )


@pytest.mark.parametrize("fast,slow", [(3.0, 10), (2, 10.0), (True, 10), (2, False)])
def test_s8_ema_periods_refuse_other_types_by_name(fast, slow):
    with pytest.raises(TypeError, match="ema_period"):
        module().EMACrossProtectedConfig(
            instrument_id=AAA,
            bar_type=bar_type(AAA),
            trade_size=Decimal(10),
            fast_ema_period=fast,
            slow_ema_period=slow,
        )


@pytest.mark.parametrize("pending_modify", [False, True])
def test_s2_stop_closes_through_adapter_or_preserves_cover(drive, pending_modify):
    """Run the actual pre-transport check, execution.py:2557-2636, without a client/transport.

    Acknowledged and pending modifications are stood in; the real engine case above
    exercises shutdown and checks its released order against the adapter too.
    """
    strategy = template()
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01)])
    (original,) = working(eng.cache)
    active = {original.client_order_id: original}
    submissions = []
    held = [Decimal(10)]
    adapter: Any = SimpleNamespace(
        _provider=SimpleNamespace(find=eng.cache.instrument),
        cache=eng.cache,
        _reduce_only_sells=True,
        _sell_to_close_order_types=SELL_TO_CLOSE_PROVEN,
    )

    def submit(order, position_id=None, client_id=None, params=None):
        denial = AlpacaExecutionClient._denial(adapter, order)
        submissions.append((order, params, denial))
        if denial is None:
            held[0] = Decimal(0)

    def cancel_all(instrument_id, order_side=None, **kwargs):
        for cid, order in list(active.items()):
            if order_side is None or order.side == order_side:
                del active[cid]

    def close_all(instrument_id, **kwargs):
        submit(
            strategy.order_factory.market(
                instrument_id,
                OrderSide.SELL,
                Quantity.from_int(10),
                time_in_force=kwargs.get("time_in_force", TimeInForce.GTC),
                reduce_only=kwargs.get("reduce_only", True),
            )
        )

    def modify(cid, *, quantity=None, price=None, trigger_price=None, **kwargs):
        assert cid == original.client_order_id
        assert trigger_price is not None
        assert trigger_price.as_decimal() >= eng.cache.quote(AAA).bid_price.as_decimal()
        if pending_modify:
            return
        # The pin's emulator.rs:814-917 matches immediately after ModifyOrder;
        # :1450-1487,1522-1536 transforms to LIMIT, retaining its ID/params.
        released = strategy.order_factory.limit(
            AAA,
            OrderSide.SELL,
            original.quantity if quantity is None else quantity,
            price,
            time_in_force=original.time_in_force,
            reduce_only=original.is_reduce_only,
            client_order_id=cid,
        )
        submit(released, params={"extended_hours": True})
        if held[0] == 0:
            active.pop(cid)

    host = SimpleNamespace(
        settings=strategy.settings,
        strategy_id=SID,
        order_factory=strategy.order_factory,
        protection=strategy.protection,
        log=SimpleNamespace(info=lambda _: None, warning=lambda _: None),
        clock=strategy.clock,
        cache=SimpleNamespace(
            positions_open=lambda **kw: eng.cache.positions_open(**kw) if held[0] else [],
            instrument=eng.cache.instrument,
            quote=eng.cache.quote,
            order=eng.cache.order,
            orders=lambda **kw: list(active.values()),
            bars=eng.cache.bars,
        ),
        cancel_all_orders=cancel_all,
        cancel_order=lambda cid: active.pop(cid, None),
        close_all_positions=close_all,
        modify_order=modify,
        submit_order=submit,
    )
    protection = importlib.import_module("canon_trading.protection")
    host.protection = protection.Protection(host, protection.ProtectionConfig())
    module().EMACrossProtected.on_stop(host)
    assert held[0] == 0 or original.client_order_id in active, (
        "stop canceled protection without a close accepted by the adapter",
        [denial for _, _, denial in submissions],
    )
    assert len(submissions) == 1
    ((sale, _, denial),) = submissions
    assert denial is None
    if pending_modify:
        # A trail the emulator did not release ends with the node (delta review of 93a862f3,
        # finding 2): a venue sale of its shares is sent first, and only then is it canceled.
        assert sale.client_order_id != original.client_order_id
        assert sale.order_type == OrderType.LIMIT and sale.is_reduce_only
        assert original.client_order_id not in active
    else:
        assert sale.client_order_id == original.client_order_id


def stop_host(
    drive,
    *,
    cover=10,
    emulated=False,
    pending=None,
    filled=0,
    fill_on_modify=False,
    release_on_modify=True,
):
    """Real factory/adapter check, with venue acknowledgements and share reservations stood in.

    The pin's strategy/mod.rs:400-405 refuses a pending cancel's modification;
    :777-778 permits a pending update. emulator.rs:814-917 releases a locally
    modified trail synchronously. A pending venue cancel still reserves shares
    until its acknowledgement, as the adapter README's TC-E41/81 proof records.
    `release_on_modify=False` stands in for a trail the emulator does not release
    (emulator.rs:877-900); an emulated order's cancel is local and settles at once
    (emulator.rs:1202-1240).
    """
    strategy = template()
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01)])
    (original,) = working(eng.cache)
    held = [Decimal(10)]
    active = {}
    messages, submissions, modifications, released, canceled = [], [], [], [], []
    adapter: Any = SimpleNamespace(
        _provider=SimpleNamespace(find=eng.cache.instrument),
        cache=eng.cache,
        _reduce_only_sells=True,
        _sell_to_close_order_types=SELL_TO_CLOSE_PROVEN,
    )
    if cover:
        active[original.client_order_id] = SimpleNamespace(
            client_order_id=original.client_order_id,
            side=OrderSide.SELL,
            order_type=OrderType.TRAILING_STOP_LIMIT if emulated else OrderType.LIMIT,
            quantity=Quantity.from_int(cover + filled),
            leaves_qty=Quantity.from_int(cover),
            filled_qty=Quantity.from_int(filled),
            price=Price.from_str("10.10"),
            trigger_price=Price.from_str("9.80") if emulated else None,
            is_emulated=emulated,
            emulation_trigger=TriggerType.BID_ASK if emulated else None,
            is_open=not emulated,
            is_inflight=pending is not None,
            is_closed=False,
            is_pending_cancel=pending == "cancel",
            is_pending_update=pending == "update",
            time_in_force=TimeInForce.DAY,
            is_reduce_only=True,
        )

    def positions(**kwargs):
        assert kwargs == {"instrument_id": AAA, "strategy_id": SID}
        return [SimpleNamespace(signed_qty=held[0], avg_px_open=10.01)] if held[0] else []

    def orders(**kwargs):
        assert kwargs == {"instrument_id": AAA, "strategy_id": SID}
        return list(active.values())

    def submit(order, position_id=None, client_id=None, params=None):
        denial = AlpacaExecutionClient._denial(adapter, order)
        # Alpaca holds shares for a venue sell; an emulated one is local and holds none
        reserved = sum(
            (
                o.leaves_qty.as_decimal()
                for o in active.values()
                if not o.is_closed and not o.is_emulated
            ),
            Decimal(0),
        )
        if denial is None and reserved + order.quantity.as_decimal() > held[0]:
            denial = "insufficient qty available: existing sell still reserves shares"
        submissions.append((order, params, denial))
        if denial is None:
            active[order.client_order_id] = order

    def modify(cid, *, quantity=None, price=None, trigger_price=None, **kwargs):
        order = active[cid]
        modifications.append(cid)
        if order.is_pending_cancel:  # native Strategy refuses before routing a command
            return
        if order.is_emulated and not release_on_modify:
            return
        if order.is_emulated:
            assert trigger_price is not None
            assert trigger_price.as_decimal() >= Decimal("9.99")
            sale = strategy.order_factory.limit(
                AAA,
                OrderSide.SELL,
                order.quantity if quantity is None else quantity,
                price,
                time_in_force=order.time_in_force,
                reduce_only=order.is_reduce_only,
                client_order_id=cid,
            )
            assert AlpacaExecutionClient._denial(adapter, sale) is None
            released.append(sale)
            order.order_type = OrderType.LIMIT
            order.is_emulated, order.is_open = False, True
            order.emulation_trigger = None
        order.is_pending_update = False
        order.price = price
        if fill_on_modify:
            held[0] -= order.leaves_qty.as_decimal()
            order.is_closed = True

    def cancel(cid, *args, **kwargs):
        order = active[cid]
        canceled.append(cid)
        assert order.is_emulated, "only a still-emulated sell is canceled at stop"
        order.is_emulated, order.is_closed = False, True
        order.emulation_trigger = None

    def cancel_all(instrument_id, order_side=None):
        assert instrument_id == AAA
        assert order_side == OrderSide.BUY or held[0] == 0

    host = SimpleNamespace(
        settings=strategy.settings,
        strategy_id=SID,
        order_factory=strategy.order_factory,
        clock=strategy.clock,
        log=SimpleNamespace(
            info=lambda message: messages.append(("info", message)),
            warning=lambda message: messages.append(("warning", message)),
            error=lambda message: messages.append(("error", message)),
        ),
        cache=SimpleNamespace(
            positions_open=positions,
            orders=orders,
            order=active.get,
            instrument=eng.cache.instrument,
            quote=eng.cache.quote,
            trade=eng.cache.trade,
            bars=eng.cache.bars,
        ),
        cancel_all_orders=cancel_all,
        modify_order=modify,
        cancel_order=cancel,
        submit_order=submit,
    )
    protection = importlib.import_module("canon_trading.protection")
    host.protection = protection.Protection(host, protection.ProtectionConfig())
    return host, SimpleNamespace(
        held=held,
        active=active,
        messages=messages,
        submissions=submissions,
        modifications=modifications,
        released=released,
        canceled=canceled,
    )


def test_f1_pending_update_can_reprice_the_same_venue_sell(drive):
    host, state = stop_host(drive, pending="update")
    module().EMACrossProtected.on_stop(host)
    (sale,) = state.active.values()
    assert sale.price.as_decimal() == Decimal("9.98")
    assert state.submissions == []
    assert sale.leaves_qty.as_decimal() == 10


def test_f1_pending_cancel_is_named_and_its_reserved_shares_are_not_sold_twice(drive):
    host, state = stop_host(drive, pending="cancel")
    module().EMACrossProtected.on_stop(host)
    assert state.submissions == []
    assert any(
        level in {"warning", "error"}
        and "AAA.ALPACA" in text
        and "uncovered" in text
        and "pending cancel" in text
        for level, text in state.messages
    )
    # The lead's ruling (a), 2026-11-13 21:13: the order is named in an ALERT that says the long may
    # be left without a working sell if that cancel succeeds; no second sell of its shares is sent.
    (pending,) = [o for o in state.active.values() if o.is_pending_cancel]
    assert any(
        level == "error"
        and text.startswith("ALERT")
        and str(pending.client_order_id) in text
        and "may be left without a working sell if that cancel succeeds" in text
        for level, text in state.messages
    ), state.messages


@pytest.mark.xfail(
    strict=True,
    reason=(
        "unit 12's named limit (the lead, 2026-11-13 21:13): a stopped strategy gets no order events "
        "(pin crates/trading/src/strategy/mod.rs:1395-1399) and a sell pending cancel cannot be "
        "modified (:400-405), so a cancel in flight at stop cannot be settled here; the quiesce unit "
        "(the node's stop waits for it, or alerts) carries it"
    ),
)
def test_f1_pending_cancel_must_finish_with_durable_full_cover(drive):
    host, state = stop_host(drive, pending="cancel")
    # Native order state, not flags invented by this seam: the venue has
    # accepted the sell and its cancellation has not yet been acknowledged.
    sale = host.order_factory.limit(
        AAA,
        OrderSide.SELL,
        Quantity.from_int(10),
        Price.from_str("10.10"),
        time_in_force=TimeInForce.DAY,
        reduce_only=True,
    )
    identity = {
        "trader_id": sale.trader_id,
        "strategy_id": sale.strategy_id,
        "instrument_id": AAA,
        "client_order_id": sale.client_order_id,
        "account_id": AccountId("ALPACA-001"),
        "ts_event": T0,
        "ts_init": T0,
    }
    sale.apply(OrderSubmitted(**identity, event_id=UUID4()))
    sale.apply(
        OrderAccepted(
            **identity,
            venue_order_id=VenueOrderId("STOP-1"),
            event_id=UUID4(),
            reconciliation=False,
        )
    )
    sale.apply(
        OrderPendingCancel(
            **identity,
            venue_order_id=VenueOrderId("STOP-1"),
            event_id=UUID4(),
            reconciliation=False,
        )
    )
    assert sale.is_pending_cancel and sale.is_open and not sale.is_emulated
    state.active.clear()
    state.active[sale.client_order_id] = sale
    module().EMACrossProtected.on_stop(host)
    durable = sum(
        (
            order.leaves_qty.as_decimal()
            for order in state.active.values()
            if not order.is_closed and not order.is_pending_cancel and not order.is_emulated
        ),
        Decimal(0),
    )
    assert durable == state.held[0], "a pending cancel cannot cover the long after shutdown"


@pytest.mark.parametrize(
    "emulated,filled,fill_on_modify", [(True, 0, False), (True, 0, True), (False, 3, False)]
)
def test_f3_stop_covers_only_the_unreserved_remainder(drive, emulated, filled, fill_on_modify):
    host, state = stop_host(
        drive, cover=4, emulated=emulated, filled=filled, fill_on_modify=fill_on_modify
    )
    module().EMACrossProtected.on_stop(host)
    assert len(state.submissions) == 1, "six of the ten held shares still need a sale"
    sale, params, denial = state.submissions[0]
    assert denial is None
    assert sale.quantity.as_decimal() == 6
    assert sale.order_type == OrderType.LIMIT and sale.time_in_force == TimeInForce.DAY
    assert sale.is_reduce_only and params == {"extended_hours": True}
    assert (
        sum(
            (o.leaves_qty.as_decimal() for o in state.active.values() if not o.is_closed),
            Decimal(0),
        )
        == state.held[0]
    )


def test_f3_native_stop_releases_four_and_sells_the_remaining_six(drive):
    strategy = template()
    eng = drive(strategy, [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01)])
    (original,) = working(eng.cache)
    strategy.modify_order(original.client_order_id, quantity=Quantity.from_int(4))
    eng.end()
    assert eng.portfolio.net_position(AAA) == 0
    sales = [order for order in eng.cache.orders(strategy_id=SID) if order.side == OrderSide.SELL]
    assert sorted(order.quantity.as_decimal() for order in sales) == [Decimal(4), Decimal(6)]
    assert original.client_order_id in {order.client_order_id for order in sales}
    adapter: Any = SimpleNamespace(
        _provider=SimpleNamespace(find=eng.cache.instrument),
        cache=eng.cache,
        _reduce_only_sells=True,
        _sell_to_close_order_types=SELL_TO_CLOSE_PROVEN,
    )
    assert all(AlpacaExecutionClient._denial(adapter, order) is None for order in sales)


def test_stop_hook_covers_a_non_template_host_and_records_its_sale(drive):
    host, state = stop_host(drive, cover=0)
    host.protection.on_stop(AAA)
    (submission,) = state.submissions
    sale, params, denial = submission
    assert sale.quantity.as_decimal() == 10 and denial is None
    assert sale.is_reduce_only and params == {"extended_hours": True}
    assert sale.client_order_id.value in host.protection._context.sale_rows


STOP_COVER_CASES = [
    (0, False, None, 10),
    (10, True, None, 10),
    (4, False, None, 6),
    (10, False, "cancel", 10),
    (10, False, None, 0),
]


@pytest.mark.parametrize("cover,emulated,pending,uncovered", STOP_COVER_CASES)
def test_f6_stop_reports_only_durable_whole_holding_cover(
    drive, cover, emulated, pending, uncovered
):
    host, state = stop_host(drive, cover=cover, emulated=emulated, pending=pending)
    host.cache.instrument = lambda _: None
    module().EMACrossProtected.on_stop(host)
    assert state.submissions == []
    if uncovered:
        assert not any("retains protection" in text for _, text in state.messages)
        assert any(
            level in {"warning", "error"}
            and "AAA.ALPACA" in text
            and "long 10" in text
            and f"uncovered {uncovered}" in text
            for level, text in state.messages
        )
    else:
        assert any("retains protection" in text for _, text in state.messages)
        assert not any(level in {"warning", "error"} for level, _ in state.messages)


@pytest.mark.parametrize("missing", ["quote", "positive_bid"])
@pytest.mark.parametrize("cover,emulated,pending,uncovered", STOP_COVER_CASES)
def test_stop_with_no_usable_bid_still_sells_what_no_venue_sell_reserves(
    drive, missing, cover, emulated, pending, uncovered
):
    """Delta review of 93a862f3, finding 1: with an instrument but no usable bid the stop still sends
    the reduce-only DAY limit `_limit_sell` prices from its no-bid fallback, for the shares no venue
    sell reserves; a still-emulated sell is canceled first, and a pending cancel's shares are not sold."""
    host, state = stop_host(drive, cover=cover, emulated=emulated, pending=pending)
    last = TradeTick(
        AAA,
        Price.from_str("10.00"),
        Quantity.from_int(100),
        AggressorSide.BUY,
        TradeId("T-STOP"),
        T0 + 2 * SECOND,
        T0 + 2 * SECOND,
    )
    host.cache.trade = lambda _: last
    if missing == "quote":
        host.cache.quote = lambda _: None
    else:
        host.cache.quote = lambda _: SimpleNamespace(bid_price=Price.from_str("0"))
    module().EMACrossProtected.on_stop(host)
    sold = 0 if pending == "cancel" else uncovered
    assert [s.quantity.as_decimal() for s, _, _ in state.submissions] == ([sold] if sold else [])
    for sale, params, denial in state.submissions:
        assert denial is None
        assert sale.order_type == OrderType.LIMIT and sale.time_in_force == TimeInForce.DAY
        assert sale.is_reduce_only and params == {"extended_hours": True}
    assert bool(state.canceled) == emulated
    live = sum(
        (o.leaves_qty.as_decimal() for o in state.active.values() if not o.is_closed), Decimal(0)
    )
    assert live <= state.held[0], "more sent than held"
    if pending != "cancel":
        assert live == state.held[0], "a long left bare"


def test_stop_cancels_a_trail_the_emulator_did_not_release_and_sells_its_shares(drive):
    """Delta review of 93a862f3, finding 2: a trail still emulated after the modify ends with the node,
    so it is canceled locally and its shares are sold at the venue, never sold beside it."""
    host, state = stop_host(drive, cover=10, emulated=True, release_on_modify=False)
    module().EMACrossProtected.on_stop(host)
    assert state.canceled and not state.released
    (submission,) = state.submissions
    sale, params, denial = submission
    assert denial is None and sale.quantity.as_decimal() == 10
    assert sale.is_reduce_only and params == {"extended_hours": True}
    live = [o for o in state.active.values() if not o.is_closed]
    assert [o.client_order_id for o in live] == [sale.client_order_id]
    assert any("retains protection" in text for _, text in state.messages)


@pytest.mark.parametrize("context_floor,expected", [(1, "0.20"), (3, "0.60")])
def test_f9_plan_distance_uses_the_injected_context_spread_floor(context_floor, expected):
    protection = importlib.import_module("canon_trading.protection")
    context = SimpleNamespace(settings=SimpleNamespace(spread_floor_m=Decimal(context_floor)))
    part = protection.Protection(object(), protection.ProtectionConfig(), context=context)
    plan = TrailPlan(
        trail=Decimal("0.01"),
        exit_through=Decimal("0.005"),
        chase_cap=Decimal("0.02"),
        atr=None,
        spread=Decimal("0.20"),
        bound="test",
    )
    assert part._plan_distance(plan, Decimal(10)) == Decimal(expected)
