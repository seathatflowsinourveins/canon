"""A long-only EMA-cross template with the Mover's protection applied on position events.

Contract: nautilus-trader@d53849b216e2 python/tests/strategies/acceptance.py:514-589
(_EMACrossTrailingWorkflow), with its market-entry implementation at :723-740;
nautilus-trader-v1@27a8e54e7ac3
nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:376-385 arms protection
on PositionOpened/Changed. The EMA indicators and registration are rc8
python/nautilus_trader/indicators/__init__.pyi:379-405 and
python/nautilus_trader/trading/__init__.pyi:528-537. Period defaults 10/20 follow
v1's example :89-90. Position callbacks and order commands belong to this strategy.
Stopping follows the pinned workflow's :609-614: cancel orders, then close positions.

Only entry selection lives here. Protection owns every trailing sell, quantity
change and cancellation on closing, using its unchanged default Mover arm.
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
from nautilus_trader.model import QuoteTick
from nautilus_trader.trading import Strategy

from .ma_cross import refuse_unknown_keywords
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
            self.log.error(f"Could not find instrument for {self.settings.instrument_id}")
            self.stop()
            return
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
        if not self.portfolio.is_net_flat(instrument_id):
            return
        entry = None if self.entry_id is None else self.cache.order(self.entry_id)
        if entry is not None and not entry.is_closed:
            return
        if self.fast_ema.value < self.slow_ema.value:
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return
        order = self.order_factory.market(
            instrument_id, OrderSide.BUY, instrument.make_qty(self.settings.trade_size)
        )
        self.entry_id = order.client_order_id
        self.submit_order(order)

    def on_quote(self, quote: QuoteTick) -> None:
        self.protection.on_quote(quote)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.settings.instrument_id)
        self.close_all_positions(self.settings.instrument_id)

    def on_position_opened(self, event: PositionOpened) -> None:
        self.protection.on_position_opened(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        self.protection.on_position_changed(event)

    def on_position_closed(self, event: PositionClosed) -> None:
        self.protection.on_position_closed(event)
