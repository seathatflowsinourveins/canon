"""A long-only EMA-cross template with the Mover's protection applied on position events.

Contract: nautilus-trader@d53849b216e2 python/tests/strategies/acceptance.py:514-589
(_EMACrossTrailingWorkflow), with its market-entry implementation at :723-740;
nautilus-trader-v1@27a8e54e7ac3
nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:376-385 arms protection
on PositionOpened/Changed. The EMA indicators and registration are rc8
python/nautilus_trader/indicators/__init__.pyi:379-405 and
python/nautilus_trader/trading/__init__.pyi:528-537. Period defaults 10/20 follow
v1's example :89-90. Position callbacks and order commands belong to this strategy.
The node's Alpaca execution client must set reduce_only_sells=True: Protection's
sells are reduce-only, denied otherwise (packages/canon-alpaca/src/canon_alpaca/
execution.py:90-102,2597-2609). Its default sell-to-close scope accepts LIMIT,
not the reduce-only MARKET made by close_all_positions (pin crates/trading/src/
strategy/mod.rs:1131-1175). Stop cancels BUY entries and delegates to Protection.on_stop;
that shared method releases/reprices existing sells and covers unreserved shares.
The side filter is at python/nautilus_trader/trading/__init__.pyi:581-589.

Entry selection lives here. Protection owns normal trailing sells, stop-time closing,
quantity changes and cancellation on closing, using the default Mover arm.
This is a template for offline and paper evaluation, with no claim to an edge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import PositionChanged
from nautilus_trader.model import PositionClosed
from nautilus_trader.model import PositionOpened
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.trading import Strategy

from .ma_cross import refuse_another_type
from .ma_cross import refuse_unknown_keywords
from .ma_cross import refuse_what_cannot_run
from .protection import Protection
from .protection import ProtectionConfig


class EMACrossProtectedConfig(StrategyConfig):
    """The upstream EMA entry parameters; protection has no separate template settings."""

    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        bar_type: BarType,
        trade_size: Decimal,
        fast_ema_period: int = 10,
        slow_ema_period: int = 20,
        **_kwargs: Any,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        refuse_another_type(
            self,
            {
                "trade_size": trade_size,
                "fast_ema_period": fast_ema_period,
                "slow_ema_period": slow_ema_period,
            },
            {"trade_size": Decimal, "fast_ema_period": int, "slow_ema_period": int},
        )
        if (
            not trade_size.is_finite()
            or trade_size <= 0
            or trade_size != trade_size.to_integral_value()
        ):
            raise ValueError(
                f"trade_size must be a positive whole number of shares, got {trade_size}"
            )
        refuse_what_cannot_run(fast_ema_period, slow_ema_period, Quantity.from_int(int(trade_size)))
        expected = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
        if bar_type != expected:
            raise ValueError(
                f"{instrument_id}: protection requires {expected}; received {bar_type}"
            )
        self.instrument_id = instrument_id
        self.bar_type = bar_type
        self.trade_size = trade_size
        self.fast_ema_period = fast_ema_period
        self.slow_ema_period = slow_ema_period


class EMACrossProtected(Strategy):
    """Enter a flat long when the warmed fast EMA is at or above the slow EMA."""

    def __init__(self, config: EMACrossProtectedConfig) -> None:
        super().__init__(config)
        self.settings = config
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self.entry_id: ClientOrderId | None = None
        self.protection = Protection(self, ProtectionConfig())

    def on_start(self) -> None:
        instrument = self.cache.instrument(self.settings.instrument_id)
        if instrument is None:
            raise RuntimeError(
                f"EMACrossProtected cannot start: instrument {self.settings.instrument_id} "
                "is not in the cache"
            )
        self.register_indicator_for_bars(self.settings.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.settings.bar_type, self.slow_ema)
        self.subscribe_bars(self.settings.bar_type)
        self.subscribe_quotes(self.settings.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type != self.settings.bar_type:
            return
        self.protection.on_bar(bar)
        if not self.indicators_initialized():
            return
        instrument_id = self.settings.instrument_id
        if self.cache.positions_open(instrument_id=instrument_id, strategy_id=self.strategy_id):
            return
        entry = None if self.entry_id is None else self.cache.order(self.entry_id)
        if entry is not None and not entry.is_closed:
            return
        if self.fast_ema.value < self.slow_ema.value:
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return
        if not self.protection.entry_allowed(instrument_id, bar.ts_init):
            return
        order = self.order_factory.market(
            instrument_id, OrderSide.BUY, instrument.make_qty(self.settings.trade_size)
        )
        self.entry_id = order.client_order_id
        self.submit_order(order)

    def on_quote(self, quote: QuoteTick) -> None:
        self.protection.on_quote(quote)

    def on_stop(self) -> None:
        instrument_id = self.settings.instrument_id
        self.cancel_all_orders(instrument_id, order_side=OrderSide.BUY)
        self.protection.on_stop(instrument_id)

    def on_position_opened(self, event: PositionOpened) -> None:
        self.protection.on_position_opened(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        self.protection.on_position_changed(event)

    def on_position_closed(self, event: PositionClosed) -> None:
        self.protection.on_position_closed(event)
