"""Moving-average cross on bars: the first strategy here, and a vehicle for the plumbing only.

Contract: `nautilus_trader.trading.Strategy` configured by a `nautilus_trader.config.StrategyConfig`
subclass, NautilusTrader v2.0.0rc8. Read 2026-11-10:

* https://nautilustrader.io/docs/md/latest/concepts/strategies.md ("Strategy configuration": a config
  subclass declares its own fields keyword-only, accepts `**_kwargs` so the base fields pass through, and
  calls `super().__init__()` with no arguments; "Handlers": do system work in `on_start`, not in the
  constructor).
* https://nautilustrader.io/docs/md/latest/getting_started/quickstart.md (upstream's EMA cross, whose
  shape this follows: indicators registered for the bar type, `indicators_initialized()` before acting,
  market orders from the order factory, `close_all_positions` in `on_stop`).
* https://nautilustrader.io/docs/md/latest/concepts/accounting.md ("Simulated short-selling checks": a
  backtest refuses an equity sell on a cash account that exceeds the long inventory), which is why this
  strategy is long or flat and never short.

Where this differs from upstream's example, and why:

* The config holds no separate `instrument_id`. The instrument is the bar type's, so the two cannot
  disagree.
* It decides from its own open positions, read from the cache with its strategy ID
  (https://nautilustrader.io/docs/md/latest/concepts/cache.md, "Position access"), not from
  `portfolio.is_net_flat` and `is_net_long`. Those answer for every position in the instrument, whoever
  opened it, while `close_all_positions` closes only this strategy's. With a second strategy in the
  same instrument the example's logic never entered while the other was long, and its exit closed
  nothing (observed at v2.0.0rc8, 2026-11-10). A position the account already held on a venue would be
  expected to do the same (UNVERIFIED: not tried with the adapter's execution client).
* `trade_size` is a `Quantity` and goes to the order factory unchanged. `Instrument.make_qty` rounds to
  the instrument's size precision without saying so (observed at v2.0.0rc8: `make_qty(Decimal("1.5"))`
  on an `Equity` gives 2), and `Equity` is whole-share only (concepts/instruments/equity). A size finer
  than the instrument allows is refused in `on_start`.
* A missing instrument raises in `on_start`. Upstream's handler example logs and calls `self.stop()`
  there; at v2.0.0rc8 that fails the trader's start with "Invalid state trigger STOPPED ->
  START_COMPLETED" (observed 2026-11-10), so the reason is raised instead and the node's start fails
  naming it.

Known limits:

* It decides from its open positions at each bar and does not track its own working orders. In a
  backtest an order sent from `on_bar` fills before the next bar (concepts/backtesting/bar-execution,
  "Order submission timing"). On a venue, a bar that arrives while an order is still working would send
  a second one. This must be closed before a paper run.
* `on_stop` flattens, so stopping a node sells whatever this strategy holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import SimpleMovingAverage
from nautilus_trader.model import OrderSide
from nautilus_trader.trading import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model import Bar
    from nautilus_trader.model import BarType
    from nautilus_trader.model import Quantity


class MaCrossConfig(StrategyConfig):
    """Parameters of `MaCross`.

    `bar_type` names the bars to subscribe to and, through its instrument ID, the instrument to trade.
    `trade_size` is the quantity of each entry order. `fast_period` and `slow_period` are the lengths, in
    bars, of the two simple moving averages; their defaults are the ones upstream's example uses.
    """

    def __init__(
        self,
        *,
        bar_type: BarType,
        trade_size: Quantity,
        fast_period: int = 10,
        slow_period: int = 20,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        if not 0 < fast_period < slow_period:
            raise ValueError(
                f"MaCross needs 0 < fast_period < slow_period, got {fast_period} and {slow_period}",
            )
        if not trade_size.is_positive():
            raise ValueError(f"MaCross needs a positive trade_size, got {trade_size}")
        self.bar_type = bar_type
        self.trade_size = trade_size
        self.fast_period = fast_period
        self.slow_period = slow_period


class MaCross(Strategy):
    """Hold `trade_size` while the fast average of bar closes is above the slow one, else hold nothing.

    This is not a trading idea and makes no claim to an edge. It exists so that bars, orders, fills,
    positions and the account can be seen moving through NautilusTrader end to end, in a backtest now
    and on Alpaca's paper environment later. Its results say nothing about what to trade.

    On each bar, once both averages have a full window: with the fast above the slow and no open
    position of its own, it buys `trade_size` at market; with the fast below the slow and a position of
    its own, it closes that position. Equal averages change nothing. It never sells short, and it
    neither counts nor touches a position another strategy opened.
    """

    def __init__(self, config: MaCrossConfig) -> None:
        super().__init__(config)
        self.bar_type = config.bar_type
        self.instrument_id = config.bar_type.instrument_id
        self.trade_size = config.trade_size
        self.fast = SimpleMovingAverage(config.fast_period)
        self.slow = SimpleMovingAverage(config.slow_period)

    def on_start(self) -> None:
        """Check the instrument, then register the averages for the bars and subscribe to them."""
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            raise RuntimeError(
                f"MaCross cannot start: instrument {self.instrument_id} is not in the cache",
            )
        if self.trade_size.precision > instrument.size_precision:
            raise ValueError(
                f"MaCross cannot start: trade_size {self.trade_size} is finer than "
                f"{self.instrument_id} allows (size precision {instrument.size_precision})",
            )
        self.register_indicator_for_bars(self.bar_type, self.fast)
        self.register_indicator_for_bars(self.bar_type, self.slow)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Enter when the fast average is above the slow one and flat; exit when below and long.

        `bar` itself is not read: the registered averages have already been updated with it.
        """
        if not self.indicators_initialized():
            return
        # It only ever buys, so any open position of its own is long.
        holding = bool(
            self.cache.positions_open(
                instrument_id=self.instrument_id,
                strategy_id=self.strategy_id,
            ),
        )
        if self.fast.value > self.slow.value and not holding:
            order = self.order_factory.market(self.instrument_id, OrderSide.BUY, self.trade_size)
            self.submit_order(order)
        elif self.fast.value < self.slow.value and holding:
            self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        """Close this strategy's open position, if any, as upstream's example does."""
        self.close_all_positions(self.instrument_id)
