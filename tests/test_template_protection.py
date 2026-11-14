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
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from canon_trading.mover import MoverConfig
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
        log=SimpleNamespace(info=lambda _: None),
        cache=SimpleNamespace(
            positions_open=lambda **kw: eng.cache.positions_open(**kw) if held[0] else [],
            instrument=eng.cache.instrument,
            quote=eng.cache.quote,
            order=eng.cache.order,
            orders=lambda **kw: list(active.values()),
        ),
        cancel_all_orders=cancel_all,
        cancel_order=lambda cid: active.pop(cid, None),
        close_all_positions=close_all,
        modify_order=modify,
        submit_order=submit,
    )
    module().EMACrossProtected.on_stop(host)
    assert held[0] == 0 or original.client_order_id in active, (
        "stop canceled protection without a close accepted by the adapter",
        [denial for _, _, denial in submissions],
    )
    if submissions:
        assert all(denial is None for _, _, denial in submissions)
        assert all(order.client_order_id == original.client_order_id for order, _, _ in submissions)
    assert len(submissions) == (0 if pending_modify else 1)
