"""Golden order streams for the Mover's protective path offline (Unit 11, 24/5 trailing-stop plan).

Drives `canon_trading.mover.Mover` through a real `BacktestEngine` over fixed, shaped offline tapes
(bars, quotes, trades, and venue fills; no network, no credential, no write outside pytest's temp
directory) and compares every order command the strategy emits (`submit`, `modify`, `cancel`, in
exact call order) against golden streams captured from `main`. Unit 11 extracts a `Protection` part
from `Mover` and delegates to it with identical orders; any drift in action, side, order type,
quantity, limit/offset prices, trigger fields, time in force, `extended_hours`, `reduce_only`, or
ordering fails this suite.

Upstream contracts and pinned citations:
- `nautilus-trader-v1` (`27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`):
  - `nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:376-385`: protective trailing
    stop armed from the entry's fill / position event.
- `nautilus-trader` (`d53849b216e20f2c122ca01b4dd0da377638b186`):
  - `python/tests/strategies/acceptance.py:514-589`: `_EMACrossTrailingWorkflow` submitting a
    trailing stop from `on_order_filled` (`event.last_px`, `event.position_id`).
  - `python/nautilus_trader/trading/__init__.pyi:512, 538-612`: public `Strategy` order command
    seams (`submit_order`, `submit_order_list`, `modify_order`, `modify_orders`, `cancel_order`,
    `cancel_orders`, `cancel_all_orders`, `close_position`, `close_all_positions`, `market_exit`).
  - `crates/trading/src/strategy/mod.rs:1055-1119, 1749-1827`: `cancel_all_orders` and
    `market_exit` route cancellations and closing market orders in Rust without re-entering the
    Python `Strategy` subclass methods.
  - `crates/execution/src/trailing.rs:40-165`: `trailing_stop_calculate` seeds from the order's
    `trigger_price` / `price` when set and moves sell levels monotonically upward.
  - `crates/execution/src/order_emulator/emulator.rs:679-699, 1395-1435, 1522-1536`: local
    `OrderEmulator` immediate-match check, release transform from `TrailingStopLimitOrder` to
    `LimitOrder`, and preservation of `SubmitOrder` `params` (including `extended_hours`) on
    release.
  - `crates/model/src/orders/mod.rs:247, 295`: `Canceled` and `Expired` orders may still transition
    to `Filled` on a late venue fill.

Regenerating the golden streams on `main` (for the owning lane after units 5, 6, and 7 land), from
the repository root, which must be on the path for `tests.test_mover`; then format the block:
    PYTHONPATH=. uv run --locked python tests/test_protection_golden.py --regenerate
    uv run --locked ruff format tests/test_protection_golden.py
"""

from __future__ import annotations

import copy
import pprint
import sys
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.execution import StaticLatencyModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import ClientId
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Position
from nautilus_trader.model import PositionId
from nautilus_trader.model import PositionSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy

from canon_trading.mover import Mover
from canon_trading.mover import MoverConfig
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import FALL
from tests.test_mover import MINUTE
from tests.test_mover import MOVER_ID
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import USD
from tests.test_mover import VENUE
from tests.test_mover import bar_type
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import tape

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Sequence

    from nautilus_trader.common import TimeEvent

type StreamEntry = dict[str, Any]
type OrderStream = list[StreamEntry]


def _fmt_dec(value: Any) -> str | None:
    if value is None:
        return None
    dec = value.as_decimal() if hasattr(value, "as_decimal") else Decimal(str(value))
    text = f"{dec.normalize():f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = value.name if hasattr(value, "name") else str(value)
    if name in ("None", "NO_TRIGGER", "NO_TRAILING_OFFSET"):
        return None
    return name


def _snapshot_order(
    action: str,
    order: Any,
    *,
    extended_hours: bool,
    quantity_override: Quantity | None = None,
    price_override: Price | None = None,
    trigger_override: Price | None = None,
) -> StreamEntry:
    qty = quantity_override if quantity_override is not None else order.quantity
    raw_price = (
        price_override
        if price_override is not None
        else (getattr(order, "price", None) if getattr(order, "has_price", False) else None)
    )
    raw_trigger = (
        trigger_override
        if trigger_override is not None
        else (
            getattr(order, "trigger_price", None)
            if getattr(order, "has_trigger_price", False)
            else None
        )
    )
    return {
        "action": action,
        "side": _enum_name(order.side),
        "type": _enum_name(order.order_type),
        "quantity": _fmt_dec(qty),
        "prices": {
            "price": _fmt_dec(raw_price),
            "limit_offset": _fmt_dec(getattr(order, "limit_offset", None)),
            "trailing_offset": _fmt_dec(getattr(order, "trailing_offset", None)),
            "trailing_offset_type": _enum_name(getattr(order, "trailing_offset_type", None)),
        },
        "trigger": {
            "trigger_price": _fmt_dec(raw_trigger),
            "activation_price": _fmt_dec(getattr(order, "activation_price", None)),
            "trigger_type": _enum_name(getattr(order, "trigger_type", None)),
            "emulation_trigger": _enum_name(getattr(order, "emulation_trigger", None)),
        },
        "time_in_force": _enum_name(order.time_in_force),
        "extended_hours": extended_hours,
        "reduce_only": bool(order.is_reduce_only),
    }


def _order_seq_key(order: Any) -> tuple[int, str]:
    value = order.client_order_id.value
    tail = value.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else 0, value)


class RecordingMover(Mover):
    """Captures every order command `Mover` emits on `Strategy`'s public order seams, in order."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.order_stream: OrderStream = []
        self._ext_by_id: dict[ClientOrderId, bool] = {}

    def _record_submit(self, order: Any, params: dict | None) -> None:
        ext = bool(params and params.get("extended_hours"))
        self._ext_by_id[order.client_order_id] = ext
        self.order_stream.append(_snapshot_order("submit", order, extended_hours=ext))

    def _record_cancel(self, order: Any, params: dict | None = None) -> None:
        ext = self._ext_by_id.get(
            order.client_order_id,
            bool(params and params.get("extended_hours")),
        )
        self.order_stream.append(_snapshot_order("cancel", order, extended_hours=ext))

    def _active_orders_for(
        self,
        instrument_id: InstrumentId | None,
        *,
        order_side: OrderSide | None = None,
        strategy_only: bool = True,
    ) -> list[Any]:
        sid = self.strategy_id if strategy_only else None
        seen: dict[str, Any] = {}
        for batch in (
            self.cache.orders_open(instrument_id=instrument_id, strategy_id=sid, side=order_side),
            self.cache.orders_emulated(
                instrument_id=instrument_id, strategy_id=sid, side=order_side
            ),
            self.cache.orders_inflight(
                instrument_id=instrument_id, strategy_id=sid, side=order_side
            ),
        ):
            for order in batch:
                cid = order.client_order_id
                if (
                    not order.is_closed
                    and not order.is_pending_cancel
                    and not self.cache.is_order_pending_cancel_local(cid)
                ):
                    seen[cid.value] = order
        return [seen[key] for key in sorted(seen)]

    def submit_order(
        self,
        order: Any,
        position_id: PositionId | None = None,
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        self._record_submit(order, params)
        super().submit_order(order, position_id=position_id, client_id=client_id, params=params)

    def submit_order_list(
        self,
        order_list: Any,
        position_id: PositionId | None = None,
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        for order in order_list.orders:
            self._record_submit(order, params)
        super().submit_order_list(
            order_list, position_id=position_id, client_id=client_id, params=params
        )

    def modify_order(
        self,
        client_order_id: ClientOrderId,
        quantity: Quantity | None = None,
        price: Price | None = None,
        trigger_price: Price | None = None,
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        order = self.cache.order(client_order_id)
        if order is not None:
            ext = self._ext_by_id.get(
                client_order_id,
                bool(params and params.get("extended_hours")),
            )
            self.order_stream.append(
                _snapshot_order(
                    "modify",
                    order,
                    extended_hours=ext,
                    quantity_override=quantity,
                    price_override=price,
                    trigger_override=trigger_price,
                )
            )
        super().modify_order(
            client_order_id,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            client_id=client_id,
            params=params,
        )

    def modify_orders(
        self,
        updates: Sequence[tuple[ClientOrderId, Quantity | None, Price | None, Price | None]] = (),
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        for cid, quantity, price, trigger_price in updates:
            order = self.cache.order(cid)
            if order is not None:
                ext = self._ext_by_id.get(cid, bool(params and params.get("extended_hours")))
                self.order_stream.append(
                    _snapshot_order(
                        "modify",
                        order,
                        extended_hours=ext,
                        quantity_override=quantity,
                        price_override=price,
                        trigger_override=trigger_price,
                    )
                )
        super().modify_orders(updates, client_id=client_id, params=params)

    def cancel_order(
        self,
        client_order_id: ClientOrderId,
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        order = self.cache.order(client_order_id)
        if order is not None:
            self._record_cancel(order, params)
        super().cancel_order(client_order_id, client_id=client_id, params=params)

    def cancel_orders(
        self,
        client_order_ids: Sequence[ClientOrderId],
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        for cid in client_order_ids:
            order = self.cache.order(cid)
            if order is not None:
                self._record_cancel(order, params)
        super().cancel_orders(client_order_ids, client_id=client_id, params=params)

    def cancel_all_orders(
        self,
        instrument_id: InstrumentId,
        order_side: OrderSide | None = None,
        client_id: ClientId | None = None,
        strategy_only: bool = True,
        params: dict | None = None,
    ) -> None:
        for order in self._active_orders_for(
            instrument_id, order_side=order_side, strategy_only=strategy_only
        ):
            self._record_cancel(order, params)
        super().cancel_all_orders(
            instrument_id,
            order_side=order_side,
            client_id=client_id,
            strategy_only=strategy_only,
            params=params,
        )

    def close_position(
        self,
        position: Position,
        client_id: ClientId | None = None,
        tags: Sequence[str] | None = None,
        time_in_force: TimeInForce | None = None,
        reduce_only: bool | None = None,
        quote_quantity: bool | None = None,
        params: dict | None = None,
    ) -> None:
        before = {o.client_order_id for o in self.cache.orders(strategy_id=self.strategy_id)}
        super().close_position(
            position,
            client_id=client_id,
            tags=tags,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            quote_quantity=quote_quantity,
            params=params,
        )
        created = [
            o
            for o in self.cache.orders(strategy_id=self.strategy_id)
            if o.client_order_id not in before
        ]
        for order in sorted(created, key=_order_seq_key):
            self._record_submit(order, params)

    def close_all_positions(
        self,
        instrument_id: InstrumentId,
        position_side: PositionSide | None = None,
        client_id: ClientId | None = None,
        tags: Sequence[str] | None = None,
        time_in_force: TimeInForce | None = None,
        reduce_only: bool | None = None,
        quote_quantity: bool | None = None,
        params: dict | None = None,
    ) -> None:
        before = {o.client_order_id for o in self.cache.orders(strategy_id=self.strategy_id)}
        super().close_all_positions(
            instrument_id,
            position_side=position_side,
            client_id=client_id,
            tags=tags,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            quote_quantity=quote_quantity,
            params=params,
        )
        created = [
            o
            for o in self.cache.orders(strategy_id=self.strategy_id)
            if o.client_order_id not in before
        ]
        for order in sorted(created, key=_order_seq_key):
            self._record_submit(order, params)

    def market_exit(self) -> None:
        if not self.is_running() or self.is_exiting():
            super().market_exit()
            return
        instruments = sorted(
            {
                *(o.instrument_id for o in self.cache.orders_open(strategy_id=self.strategy_id)),
                *(
                    o.instrument_id
                    for o in self.cache.orders_inflight(strategy_id=self.strategy_id)
                ),
                *(p.instrument_id for p in self.cache.positions_open(strategy_id=self.strategy_id)),
            },
            key=str,
        )
        for instrument_id in instruments:
            for order in self._active_orders_for(instrument_id, strategy_only=True):
                self._record_cancel(order, None)
        before = {o.client_order_id for o in self.cache.orders(strategy_id=self.strategy_id)}
        super().market_exit()
        created = [
            o
            for o in self.cache.orders(strategy_id=self.strategy_id)
            if o.client_order_id not in before
        ]
        for order in sorted(created, key=_order_seq_key):
            self._record_submit(order, None)


def _engine(
    *,
    liquid: bool = False,
    trade_execution: bool = True,
    latency_model: Any | None = None,
    cash: Decimal = Decimal(100_000),
) -> BacktestEngine:
    eng = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("TESTER-001"), risk_engine=RiskEngineConfig())
    )
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(float(cash), USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        latency_model=latency_model,
        bar_execution=False,
        trade_execution=trade_execution,
        liquidity_consumption=liquid,
    )
    for instrument_id in (AAA, BBB):
        eng.add_instrument(
            Equity(
                instrument_id=instrument_id,
                raw_symbol=Symbol(instrument_id.symbol.value),
                currency=USD,
                price_precision=4,
                price_increment=Price.from_str("0.0001"),
                ts_event=0,
                ts_init=0,
                info={"name": "Example Inc. Common Stock", "status": "active", "tradable": True},
            )
        )
    return eng


def _golden_strategy(**changes: Any) -> RecordingMover:
    settings: dict[str, Any] = {
        "max_notional": Decimal(50_000),
        "max_move": Decimal(5),
        "max_shares": 10,
        "entry_arm": "bridge",
        "strategy_id": MOVER_ID,
        "order_id_tag": "001",
    }
    settings.update(changes)
    return RecordingMover(MoverConfig(**settings))


def _quote_at(
    ts_ns: int,
    bid: float,
    ask: float,
    *,
    bid_size: int = 10_000,
    ask_size: int = 10_000,
) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price(bid, 4),
        Price(ask, 4),
        Quantity.from_int(bid_size),
        Quantity.from_int(ask_size),
        ts_ns,
        ts_ns,
    )


def _bar_at(
    minute_index: int,
    opened: float,
    high: float,
    low: float,
    close: float,
    *,
    volume: int = 100_000,
) -> Bar:
    ts_open = T0 + minute_index * MINUTE
    return Bar(
        bar_type(AAA),
        Price(opened, 4),
        Price(high, 4),
        Price(low, 4),
        Price(close, 4),
        Quantity.from_int(volume),
        ts_open,
        ts_open + MINUTE,
    )


def _trade_at(ts_ns: int, price: float, *, size: int = 100, tag: str = "T-1") -> TradeTick:
    return TradeTick(
        AAA,
        Price(price, 4),
        Quantity.from_int(size),
        AggressorSide.BUY,
        TradeId(tag),
        ts_ns,
        ts_ns,
    )


def _run_replay(
    eng: BacktestEngine,
    strategy: RecordingMover,
    *tapes: list[Any],
    extra_strategies: Sequence[Strategy] = (),
) -> tuple[OrderStream, list[Any], bool]:
    for items in tapes:
        eng.add_data(items)
    eng.add_strategy(strategy)
    eng.add_strategy(ranker(rank((AAA, "0.25"))))
    for extra in extra_strategies:
        eng.add_strategy(extra)
    eng.run()
    cached_orders = sorted(
        eng.cache.orders(strategy_id=MOVER_ID),
        key=_order_seq_key,
    )
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    # every order the Mover holds was recorded as a submit: one sent by a path the recorder does not
    # wrap (a close Rust retries on its own timer, say) fails here, not silently in the stream
    submits = [e for e in strategy.order_stream if e["action"] == "submit"]
    assert len(submits) == len(cached_orders), (len(submits), len(cached_orders))
    return list(strategy.order_stream), cached_orders, flat


class _LateSaleFillConfig(StrategyConfig):
    def __init__(self, **_kwargs: object) -> None:
        super().__init__()


class _LateSaleFillDriver(Strategy):
    """Delivers one late `OrderFilled` on a canceled sale leg after the holding is already flat."""

    target: RecordingMover | None = None
    fire_ns: int = 0
    # a timer callback's exception is not raised out of the engine, so the scenario reads these
    delivered: int = 0
    error: BaseException | None = None

    def on_start(self) -> None:
        self.clock.set_time_alert(
            "late-sale-fill",
            datetime.fromtimestamp(self.fire_ns / 1e9, tz=UTC),
            self._deliver,
        )

    def _deliver(self, event: TimeEvent) -> None:
        try:
            self._deliver_late_fill(event)
        except BaseException as error:  # held for the scenario's assert
            self.error = error
            raise
        self.delivered += 1

    def _deliver_late_fill(self, event: TimeEvent) -> None:
        assert self.target is not None
        canceled_sales = [
            o
            for o in self.cache.orders(strategy_id=MOVER_ID)
            if o.side == OrderSide.SELL and o.is_canceled
        ]
        assert len(canceled_sales) == 1
        sale = canceled_sales[0]
        assert sale.venue_order_id is not None
        assert sale.account_id is not None
        assert sale.price is not None
        late_fill = OrderFilled(
            trader_id=sale.trader_id,
            strategy_id=sale.strategy_id,
            instrument_id=sale.instrument_id,
            client_order_id=sale.client_order_id,
            venue_order_id=sale.venue_order_id,
            account_id=sale.account_id,
            trade_id=TradeId("LATE-SELL-1"),
            order_side=sale.side,
            order_type=sale.order_type,
            last_qty=sale.quantity,
            last_px=sale.price,
            currency=USD,
            liquidity_side=LiquiditySide.MAKER,
            event_id=UUID4(),
            ts_event=event.ts_event,
            ts_init=event.ts_event,
            reconciliation=False,
        )
        self.target.on_order_filled(late_fill)


# --- Scenario runners ----------------------------------------------------------------------------


def _scenario_entry_fill_then_trail_arm() -> OrderStream:
    eng = _engine()
    s = _golden_strategy()
    stream, _, flat = _run_replay(eng, s, tape(AAA, RISING + FALL))
    eng.dispose()
    assert flat
    return stream


def _scenario_entry_fill_then_trail_arm_trades() -> OrderStream:
    eng = _engine(trade_execution=False)
    s = _golden_strategy(trigger=TriggerType.LAST_PRICE)
    closes = RISING + FALL
    items = list(tape(AAA, closes))
    for i, close in enumerate(closes):
        ts_close = T0 + (i + 1) * MINUTE
        items.append(_trade_at(ts_close - 2, close, tag=f"T-{i}"))
    # A print of the holding after the 301s entry fill and before the 302s arming step.
    items.append(_trade_at(T0 + 5 * MINUTE + SECOND + SECOND // 2, 10.41, tag="T-HOLD"))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_ratchet() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(
        trail_mode="ratchet",
        ratchet_base="tier",
        atr_n=3,
        ratchet_r=Decimal(2),
        ratchet_g=Decimal("0.5"),
    )
    s.baseline_dollar_volume_per_minute[AAA] = Decimal(1_000_000)
    closes = [100.00, 100.20, 100.40, 100.60, 100.80, 102.00, 101.86, 100.00]
    stream, _, flat = _run_replay(eng, s, tape(AAA, closes))
    eng.dispose()
    assert flat
    return stream


def _scenario_warm_up_tighten() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(trail_mode="atr", atr_n=6, atr_k=Decimal(1))
    rows = [
        (0, 99.00, 99.25, 99.00, 99.00, 98.98, 99.00),
        (1, 99.00, 99.25, 99.00, 99.25, 99.23, 99.25),
        (2, 99.25, 99.50, 99.25, 99.50, 99.48, 99.50),
        (3, 99.50, 99.75, 99.50, 99.75, 99.73, 99.75),
        (4, 99.75, 100.00, 99.75, 100.00, 99.98, 100.00),
        (5, 100.00, 100.25, 100.00, 100.25, 100.24, 100.26),
        (6, 100.25, 100.25, 99.95, 99.95, 99.94, 99.96),
        (7, 99.95, 99.95, 99.50, 99.50, 99.48, 99.50),
    ]
    items: list[Any] = []
    for idx, opened, high, low, close, bid, ask in rows:
        items.append(_bar_at(idx, opened, high, low, close))
        items.append(_quote_at(T0 + (idx + 1) * MINUTE - 1, bid, ask))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_chase() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(chase_secs=5)
    stream, _, flat = _run_replay(eng, s, tape(AAA, [*RISING, 10.30, 10.30]))
    eng.dispose()
    assert flat
    return stream


def _scenario_extended_hours_release() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(extended_hours=True)
    stream, orders, flat = _run_replay(eng, s, tape(AAA, RISING + FALL))
    released = orders[1]
    had_release = any(isinstance(ev, OrderReleased) for ev in released.events())
    final_shape = (released.order_type.name, released.status.name)
    eng.dispose()
    assert flat
    assert had_release and final_shape == ("LIMIT", "FILLED")
    return stream


def _scenario_time_out() -> OrderStream:
    eng = _engine(latency_model=StaticLatencyModel(insert_latency_nanos=100_000_000))
    s = _golden_strategy(entry_timeout_secs=10)
    items = list(tape(AAA, [*RISING[:5], 10.35, 10.30]))
    # Ask steps away while the 301s entry is in flight, so the limit rests open and times out.
    items.append(_quote_at(T0 + 5 * MINUTE + SECOND + 50_000_000, 10.49, 10.51))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_late_fill_after_a_full_sale() -> OrderStream:
    eng = _engine(
        liquid=True,
        latency_model=StaticLatencyModel(cancel_latency_nanos=15 * SECOND),
    )
    s = _golden_strategy(max_notional=Decimal("31.5"), max_shares=None)
    items = list(tape(AAA, [10.00, 10.10, 10.20, 10.30]))
    # Minute 5 close (10.40) offers only 2 shares on the ask; the 3-share entry fills 2 and rests 1.
    items.append(_bar_at(4, 10.30, 10.40, 10.30, 10.40))
    items.append(_quote_at(T0 + 5 * MINUTE - 1, 10.39, 10.41, ask_size=2))
    # Bid drops through the 2-share trailing stop while ask stays above 10.41: full sale of the 2
    # held shares, and the next step cancels the still-resting 1-share buy tail.
    items.append(_quote_at(T0 + 5 * MINUTE + 4 * SECOND, 10.14, 10.60))
    # While that cancel is in flight, 1 share fills late at 10.41; the exiting slot sells it flat.
    items.append(_quote_at(T0 + 5 * MINUTE + 7 * SECOND, 10.39, 10.41, ask_size=1))
    items.append(_bar_at(5, 10.40, 10.40, 10.35, 10.35))
    items.append(_quote_at(T0 + 6 * MINUTE - 1, 10.34, 10.36))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_late_reentry_fill_after_timeout_cancel() -> OrderStream:
    eng = _engine(
        liquid=True,
        latency_model=StaticLatencyModel(
            insert_latency_nanos=100_000_000,
            cancel_latency_nanos=15 * SECOND,
        ),
    )
    s = _golden_strategy(
        max_notional=Decimal("32.5"),
        max_shares=None,
        cooldown_secs=10,
        entry_timeout_secs=5,
    )
    closes = [10.00, 10.10, 10.20, 10.30, 10.40, 10.15, 10.50, 10.60, 10.80, 10.65, 10.60]
    items = list(tape(AAA, closes))
    # Second entry (at 541s) rests open when the ask steps away, times out at 546s, and fills 2
    # shares at 548s while its cancel is in flight; Mover arms a 2-share trail and sells flat.
    items.append(_quote_at(T0 + 9 * MINUTE + SECOND + 50_000_000, 10.89, 10.91))
    items.append(_quote_at(T0 + 9 * MINUTE + 8 * SECOND, 10.79, 10.81, ask_size=2))
    items.append(_quote_at(T0 + 9 * MINUTE + 8 * SECOND + 100_000_000, 10.89, 10.91))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_late_sell_fill_after_full_sale() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(chase_secs=5)
    driver = _LateSaleFillDriver(
        _LateSaleFillConfig(strategy_id=StrategyId("LateFill-001"), order_id_tag="LTF")
    )
    driver.target = s
    driver.fire_ns = T0 + 9 * MINUTE + 10 * SECOND
    stream, _, flat = _run_replay(
        eng,
        s,
        tape(AAA, [*RISING, 10.30, 10.30, 10.25]),
        extra_strategies=(driver,),
    )
    exits = s.record()["exits"]
    eng.dispose()
    assert driver.error is None, driver.error
    assert driver.delivered == 1
    assert flat
    assert len(exits) == 1 and Decimal(exits[0]["filled_quantity"]) == Decimal(10)
    return stream


def _scenario_quantity_change() -> OrderStream:
    eng = _engine(liquid=True)
    s = _golden_strategy(max_notional=Decimal("31.5"), max_shares=None)
    items = list(tape(AAA, [10.00, 10.10, 10.20, 10.30]))
    # Minute 5 close offers 1 share on the ask: 3-share entry fills 1 share, arming a 1-share trail.
    items.append(_bar_at(4, 10.30, 10.40, 10.30, 10.40))
    items.append(_quote_at(T0 + 5 * MINUTE - 1, 10.39, 10.41, ask_size=1))
    # Bid rises to 10.59 so the 1-share trail ratchets up to trigger 10.38 / limit 10.32.
    items.append(_quote_at(T0 + 5 * MINUTE + 2 * SECOND + SECOND // 2, 10.59, 10.61))
    # Ask returns to 10.41 with 2 shares while bid (10.39) stays above 10.38: remaining 2 shares
    # fill, and Mover cancels the 1-share trail and remakes a 3-share trail keeping 10.38 / 10.32.
    items.append(_quote_at(T0 + 5 * MINUTE + 4 * SECOND, 10.39, 10.41, ask_size=2))
    # Bid then crosses 10.38, releasing and filling all 3 shares cleanly.
    items.append(_quote_at(T0 + 5 * MINUTE + 6 * SECOND, 10.35, 10.37))
    items.append(_bar_at(5, 10.40, 10.40, 10.35, 10.36))
    items.append(_quote_at(T0 + 6 * MINUTE - 1, 10.35, 10.37))
    stream, _, flat = _run_replay(eng, s, items)
    eng.dispose()
    assert flat
    return stream


def _scenario_wind_down_exit_extended() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(
        extended_hours=True,
        wind_down_at=datetime.fromtimestamp((T0 + 6 * MINUTE) / 1e9, tz=UTC),
    )
    stream, _, flat = _run_replay(eng, s, tape(AAA, RISING))
    eng.dispose()
    assert flat
    return stream


def _scenario_wind_down_exit_regular() -> OrderStream:
    eng = _engine()
    s = _golden_strategy(
        wind_down_at=datetime.fromtimestamp((T0 + 6 * MINUTE) / 1e9, tz=UTC),
        market_exit_reduce_only=False,
        market_exit_time_in_force=TimeInForce.DAY,
    )
    stream, _, flat = _run_replay(eng, s, tape(AAA, RISING))
    eng.dispose()
    assert flat
    return stream


SCENARIOS: dict[str, Callable[[], OrderStream]] = {
    "entry_fill_then_trail_arm": _scenario_entry_fill_then_trail_arm,
    "entry_fill_then_trail_arm_trades": _scenario_entry_fill_then_trail_arm_trades,
    "ratchet": _scenario_ratchet,
    "warm_up_tighten": _scenario_warm_up_tighten,
    "chase": _scenario_chase,
    "extended_hours_release": _scenario_extended_hours_release,
    "time_out": _scenario_time_out,
    "late_fill_after_a_full_sale": _scenario_late_fill_after_a_full_sale,
    "late_reentry_fill_after_timeout_cancel": _scenario_late_reentry_fill_after_timeout_cancel,
    "late_sell_fill_after_full_sale": _scenario_late_sell_fill_after_full_sale,
    "quantity_change": _scenario_quantity_change,
    "wind_down_exit_extended": _scenario_wind_down_exit_extended,
    "wind_down_exit_regular": _scenario_wind_down_exit_regular,
}


# --- Golden order streams on main ----------------------------------------------------------------

# BEGIN GOLDEN_STREAMS
GOLDEN_STREAMS: dict[str, OrderStream] = {
    "entry_fill_then_trail_arm": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "entry_fill_then_trail_arm_trades": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "LAST_PRICE",
                "emulation_trigger": "LAST_PRICE",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "ratchet": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "100.81",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "101.72",
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "101.78",
                "activation_price": "100.79",
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "101.72",
                "limit_offset": "0.14",
                "trailing_offset": "0.11",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "101.78",
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "warm_up_tighten": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "100",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "2.5",
                "trailing_offset": "2",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "97.74",
                "limit_offset": "2.5",
                "trailing_offset": "2",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "98.24",
                "activation_price": "99.98",
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "97.74",
                "limit_offset": "0.32",
                "trailing_offset": "0.25",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "98.24",
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "chase": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.42",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.28",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "extended_hours_release": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "time_out": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
    ],
    "late_fill_after_a_full_sale": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "2",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "1",
            "prices": {
                "price": "10.38",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "late_reentry_fill_after_timeout_cancel": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "3",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.81",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.81",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "2",
            "prices": {
                "price": None,
                "limit_offset": "0.28",
                "trailing_offset": "0.22",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "late_sell_fill_after_full_sale": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.42",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.28",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "quantity_change": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "1",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "1",
            "prices": {
                "price": "10.32",
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "10.38",
                "activation_price": "10.39",
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "3",
            "prices": {
                "price": "10.32",
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "10.38",
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "wind_down_exit_extended": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.22",
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "10.28",
                "activation_price": "10.39",
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.48",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
    ],
    "wind_down_exit_regular": [
        {
            "action": "submit",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.41",
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "cancel",
            "side": "SELL",
            "type": "TRAILING_STOP_LIMIT",
            "quantity": "10",
            "prices": {
                "price": "10.22",
                "limit_offset": "0.27",
                "trailing_offset": "0.21",
                "trailing_offset_type": "PRICE",
            },
            "trigger": {
                "trigger_price": "10.28",
                "activation_price": "10.39",
                "trigger_type": "BID_ASK",
                "emulation_trigger": "BID_ASK",
            },
            "time_in_force": "DAY",
            "extended_hours": True,
            "reduce_only": False,
        },
        {
            "action": "submit",
            "side": "SELL",
            "type": "MARKET",
            "quantity": "10",
            "prices": {
                "price": None,
                "limit_offset": None,
                "trailing_offset": None,
                "trailing_offset_type": None,
            },
            "trigger": {
                "trigger_price": None,
                "activation_price": None,
                "trigger_type": None,
                "emulation_trigger": None,
            },
            "time_in_force": "DAY",
            "extended_hours": False,
            "reduce_only": False,
        },
    ],
}
# END GOLDEN_STREAMS


def _assert_golden_stream(name: str, actual: OrderStream, expected: OrderStream) -> None:
    if len(actual) != len(expected):
        raise AssertionError(
            f"{name}: stream length {len(actual)} != golden {len(expected)}; "
            f"actual={actual!r}, expected={expected!r}"
        )
    for idx, (got, want) in enumerate(zip(actual, expected, strict=True)):
        if got != want:
            raise AssertionError(f"{name}[{idx}] diverged: actual={got!r} != golden={want!r}")


# --- Golden order stream tests -------------------------------------------------------------------


def test_golden_entry_fill_then_trail_arm() -> None:
    _assert_golden_stream(
        "entry_fill_then_trail_arm",
        _scenario_entry_fill_then_trail_arm(),
        GOLDEN_STREAMS["entry_fill_then_trail_arm"],
    )


def test_golden_entry_fill_then_trail_arm_trades() -> None:
    _assert_golden_stream(
        "entry_fill_then_trail_arm_trades",
        _scenario_entry_fill_then_trail_arm_trades(),
        GOLDEN_STREAMS["entry_fill_then_trail_arm_trades"],
    )


def test_golden_ratchet() -> None:
    _assert_golden_stream(
        "ratchet",
        _scenario_ratchet(),
        GOLDEN_STREAMS["ratchet"],
    )


def test_golden_warm_up_tighten() -> None:
    _assert_golden_stream(
        "warm_up_tighten",
        _scenario_warm_up_tighten(),
        GOLDEN_STREAMS["warm_up_tighten"],
    )


def test_golden_chase() -> None:
    _assert_golden_stream(
        "chase",
        _scenario_chase(),
        GOLDEN_STREAMS["chase"],
    )


def test_golden_extended_hours_release() -> None:
    _assert_golden_stream(
        "extended_hours_release",
        _scenario_extended_hours_release(),
        GOLDEN_STREAMS["extended_hours_release"],
    )


def test_golden_time_out() -> None:
    _assert_golden_stream(
        "time_out",
        _scenario_time_out(),
        GOLDEN_STREAMS["time_out"],
    )


def test_golden_late_fill_after_a_full_sale() -> None:
    _assert_golden_stream(
        "late_fill_after_a_full_sale",
        _scenario_late_fill_after_a_full_sale(),
        GOLDEN_STREAMS["late_fill_after_a_full_sale"],
    )


def test_golden_late_reentry_fill_after_timeout_cancel() -> None:
    _assert_golden_stream(
        "late_reentry_fill_after_timeout_cancel",
        _scenario_late_reentry_fill_after_timeout_cancel(),
        GOLDEN_STREAMS["late_reentry_fill_after_timeout_cancel"],
    )


def test_golden_late_sell_fill_after_full_sale() -> None:
    _assert_golden_stream(
        "late_sell_fill_after_full_sale",
        _scenario_late_sell_fill_after_full_sale(),
        GOLDEN_STREAMS["late_sell_fill_after_full_sale"],
    )


def test_golden_quantity_change() -> None:
    stream = _scenario_quantity_change()
    assert all(entry["action"] != "modify" for entry in stream)
    _assert_golden_stream(
        "quantity_change",
        stream,
        GOLDEN_STREAMS["quantity_change"],
    )


def test_golden_wind_down_exit_extended() -> None:
    _assert_golden_stream(
        "wind_down_exit_extended",
        _scenario_wind_down_exit_extended(),
        GOLDEN_STREAMS["wind_down_exit_extended"],
    )


def test_golden_wind_down_exit_regular() -> None:
    _assert_golden_stream(
        "wind_down_exit_regular",
        _scenario_wind_down_exit_regular(),
        GOLDEN_STREAMS["wind_down_exit_regular"],
    )


def test_every_scenario_has_a_golden_copy_and_any_stream_mutation_fails() -> None:
    assert set(SCENARIOS) == set(GOLDEN_STREAMS)
    baseline = copy.deepcopy(GOLDEN_STREAMS["ratchet"])
    _assert_golden_stream("ratchet", baseline, GOLDEN_STREAMS["ratchet"])

    top_mutations: list[tuple[int, str, Any]] = [
        (1, "action", "modify"),
        (1, "side", "BUY"),
        (1, "type", "LIMIT"),
        (1, "quantity", "9"),
        (1, "time_in_force", "GTC"),
        (1, "extended_hours", not GOLDEN_STREAMS["ratchet"][1]["extended_hours"]),
        (1, "reduce_only", True),
    ]
    for idx, key, value in top_mutations:
        mutated = copy.deepcopy(baseline)
        mutated[idx][key] = value
        with pytest.raises(AssertionError):
            _assert_golden_stream("ratchet", mutated, GOLDEN_STREAMS["ratchet"])

    nested_mutations: list[tuple[int, str, str, Any]] = [
        (3, "prices", "price", "101.71"),
        (3, "prices", "limit_offset", "0.15"),
        (3, "prices", "trailing_offset", "0.12"),
        (3, "prices", "trailing_offset_type", "BASIS_POINTS"),
        (3, "trigger", "trigger_price", "101.77"),
        (3, "trigger", "activation_price", "102"),
        (3, "trigger", "trigger_type", "LAST_PRICE"),
        (3, "trigger", "emulation_trigger", None),
    ]
    for idx, group, key, value in nested_mutations:
        mutated = copy.deepcopy(baseline)
        mutated[idx][group][key] = value
        with pytest.raises(AssertionError):
            _assert_golden_stream("ratchet", mutated, GOLDEN_STREAMS["ratchet"])

    reordered = [baseline[0], baseline[1], baseline[3], baseline[2]]
    with pytest.raises(AssertionError):
        _assert_golden_stream("ratchet", reordered, GOLDEN_STREAMS["ratchet"])

    with pytest.raises(AssertionError):
        _assert_golden_stream("ratchet", baseline[:-1], GOLDEN_STREAMS["ratchet"])

    with pytest.raises(AssertionError):
        _assert_golden_stream("ratchet", [*baseline, baseline[-1]], GOLDEN_STREAMS["ratchet"])


def _regenerate_golden_streams() -> None:
    computed = {name: runner() for name, runner in SCENARIOS.items()}
    path = Path(__file__).resolve()
    source = path.read_text(encoding="utf-8")
    begin = "# BEGIN GOLDEN_STREAMS\n"
    end = "# END GOLDEN_STREAMS\n"
    if begin not in source or end not in source:
        raise RuntimeError("GOLDEN_STREAMS markers not found in test_protection_golden.py")
    prefix, rest = source.split(begin, maxsplit=1)
    _, suffix = rest.split(end, maxsplit=1)
    formatted = pprint.pformat(computed, width=96, sort_dicts=False)
    block = f"GOLDEN_STREAMS: dict[str, OrderStream] = {formatted}\n"
    path.write_text(f"{prefix}{begin}{block}{end}{suffix}", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] != ["--regenerate"]:
        raise SystemExit("Usage: python tests/test_protection_golden.py --regenerate")
    _regenerate_golden_streams()
