"""`MaCross` for a session run on a live venue: no second order while one is working, limits outside the session.

Contract: `canon_trading.ma_cross.MaCross`, subclassed and left unchanged, so recorded backtests keep their
meaning (ADR 0044); `nautilus_trader.trading.Strategy.submit_order(order, params=...)`, `cancel_all_orders`
and the clock's `set_time_alert`, NautilusTrader v2.0.0rc8 (`trading/__init__.pyi`, `common/__init__.pyi`).
Read 2026-11-10:

* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading": an order is eligible
  outside the regular session only with `extended_hours` true, only as a limit order with time in force
  `day` or `gtc`; "Orders not eligible for extended hours submitted after 4:00pm ET will be queued up for
  release the next trading day"; extended hours carry "less liquidity". "Sub-penny increments": at or above
  $1.00 a limit price has at most two decimals.
* The flag reaches the adapter in the submit command's params, `params={"extended_hours": True}`, sent
  only on a DAY or GTC limit (`canon_alpaca.execution`, ADR 0038); `session_window` refuses an extended
  window until one has passed on paper (ADR 0044).

Two departures from `MaCross`, both for live use:

* **Working-order guard.** `MaCross` decides from its open position at each bar and does not track its
  working orders, so a bar that arrives while an order is still working sends a second one (its own
  docstring). Here nothing is sent while one of this strategy's orders is open or in flight: not on a bar,
  not at wind-down, not on stop. That is a defect of `MaCross` for live use; it is not fixed there now
  because recorded backtests ran its code.
* **Outside the regular session** (`extended_hours=True`, set by the run's configuration, never from the
  clock) every order is a marked DAY limit, priced `limit_offset` through the touch from the latest quote:
  a buy at the ask plus the offset, a sell at the bid minus it, rounded to the cent away from the market's
  side. A limit not filled within `limit_timeout_secs` is canceled and its signal dropped, never chased.
  With no quote yet the signal is dropped.

* **Closes are not reduce-only.** `MaCross` exits with `close_all_positions`, which sends a reduce-only
  market order (observed at v2.0.0rc8); the adapter denies reduce-only orders (ADR 0036), so that exit
  would never reach Alpaca. Here a close is a plain DAY market order in the session.

`wind_down_at`, when set, is when the run's flatten starts, with no new entries after it. In the session it
is upstream's `market_exit()` (concepts/strategies, "Market exit": cancel the strategy's orders, close its
positions at market, re-check every `market_exit_interval_ms` and close again once nothing works), and
`manage_stop` makes `stop()` do the same; the run sets both `market_exit` keys so the closing orders are
DAY and not reduce-only. Outside the session a market close would only queue for the next session, so the
wind-down cancels this strategy's working orders and, once none is working, closes with a marked limit,
again on each later bar while it is still held. A close is sized and sided from the signed position, so a
short is bought back, not added to.

Known limits: the averages are built from this window's bars only, so the first `slow_period` bars send
nothing; the strategy does not know the session calendar (the run checks it before it starts).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce

from .ma_cross import MaCross
from .ma_cross import MaCrossConfig

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar
    from nautilus_trader.model import BarType
    from nautilus_trader.model import Quantity

EXTENDED_HOURS = {"extended_hours": True}
"""The submit params that mark an order as extended-hours eligible for the adapter."""
LIMIT_OFFSET = Decimal("0.05")
"""Five ticks of Alpaca's $0.01 increment through the touch: outside the session the book is thinner and a
limit at the touch may never fill; five cents bounds the price paid at a fraction of a basis point on SPY."""
LIMIT_TIMEOUT_SECS = 30
CENT = Decimal("0.01")


class SessionMaCrossConfig(MaCrossConfig):
    """`MaCrossConfig` plus the session settings described in the module header.

    `wind_down_at` is an aware UTC `datetime`. The base fields (`strategy_id`, `order_id_tag`) are given as
    constructor keywords, as for any `StrategyConfig` subclass at the pin (concepts/strategies).
    """

    def __init__(
        self,
        *,
        bar_type: BarType,
        trade_size: Quantity,
        fast_period: int = 10,
        slow_period: int = 20,
        extended_hours: bool = False,
        limit_offset: Decimal = LIMIT_OFFSET,
        limit_timeout_secs: int = LIMIT_TIMEOUT_SECS,
        wind_down_at: datetime | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            bar_type=bar_type,
            trade_size=trade_size,
            fast_period=fast_period,
            slow_period=slow_period,
            **kwargs,
        )
        if limit_offset < 0 or limit_offset != limit_offset.quantize(CENT):
            raise ValueError(
                f"limit_offset must be whole cents and not negative, got {limit_offset}"
            )
        if limit_timeout_secs <= 0:
            raise ValueError(f"limit_timeout_secs must be positive, got {limit_timeout_secs}")
        if wind_down_at is not None and wind_down_at.tzinfo is None:
            raise ValueError("wind_down_at must be an aware datetime")
        self.extended_hours = extended_hours
        self.limit_offset = limit_offset
        self.limit_timeout_secs = limit_timeout_secs
        self.wind_down_at = wind_down_at


def through_the_touch(side: OrderSide, bid: Decimal, ask: Decimal, offset: Decimal) -> Decimal:
    """The marketable limit price: ask plus `offset` for a buy, bid minus it for a sell, in whole cents."""
    if side == OrderSide.BUY:
        return (ask + offset).quantize(CENT, rounding=ROUND_CEILING)
    return (bid - offset).quantize(CENT, rounding=ROUND_FLOOR)


class SessionMaCross(MaCross):
    """`MaCross`'s decision, with the working-order guard, session order forms and wind-down above."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.extended_hours = config.extended_hours
        self.limit_offset = config.limit_offset
        self.limit_timeout_secs = config.limit_timeout_secs
        self.wind_down_at = config.wind_down_at
        self.manage_stop = config.manage_stop
        self.winding_down = False
        self.dropped: list[str] = []
        """Why each dropped signal was dropped, for the run's report."""

    def on_start(self) -> None:
        super().on_start()
        if self.extended_hours:
            self.subscribe_quotes(self.instrument_id)
        if self.wind_down_at is not None:
            self.clock.set_time_alert(
                "wind-down", self.wind_down_at, self._wind_down, allow_past=True
            )

    def working(self) -> bool:
        """Whether one of this strategy's orders for the instrument is not closed: initialized and not yet
        submitted (the adapter submits on a task of its own), in flight, or open."""
        return any(
            not order.is_closed
            for order in self.cache.orders(
                instrument_id=self.instrument_id, strategy_id=self.strategy_id
            )
        )

    def held(self) -> Decimal:
        """This strategy's signed open quantity in the instrument: positive long, negative short."""
        return sum(
            (
                p.signed_qty
                for p in self.cache.positions_open(
                    instrument_id=self.instrument_id, strategy_id=self.strategy_id
                )
            ),
            Decimal(0),
        )

    def on_bar(self, bar: Bar) -> None:
        if self.working() or self.is_exiting():
            return
        held = self.held()
        if self.winding_down:
            if held:
                self._wind_down_close()
            return
        if held < 0:  # it never sells short; a short is closed at once
            self._close()
            return
        if not self.indicators_initialized():
            return
        if self.fast.value > self.slow.value and not held:
            self._enter()
        elif self.fast.value < self.slow.value and held > 0:
            self._close()

    def on_stop(self) -> None:
        """Cancel this strategy's working orders; with none working, close its position.

        A close is never sent while an order works, so a stop cannot add a second exit to one in flight;
        what is left then is the window's to find and report. With `manage_stop` the market exit has
        already run before this is called, and nothing more is sent.
        """
        if self.manage_stop:
            return
        if self.working():
            self.cancel_all_orders(self.instrument_id)
        elif self.held():
            self._close()

    def _wind_down(self, event: TimeEvent) -> None:  # the alert only times it
        self.winding_down = True
        self.log.info("Winding down: no new entries; closing what is held once nothing works")
        if self.working():
            self.cancel_all_orders(self.instrument_id)  # the next bar closes, once the cancels land
        elif self.held():
            self._wind_down_close()

    def _wind_down_close(self) -> None:
        """In the session upstream's market exit, outside it a marked limit; only once nothing works, so
        a second close never joins one in flight."""
        if self.extended_hours:
            self._close()
        else:
            self.market_exit()

    def _enter(self) -> None:
        if not self.extended_hours:
            self.submit_order(
                self.order_factory.market(self.instrument_id, OrderSide.BUY, self.trade_size)
            )
            return
        self._limit(OrderSide.BUY, self.trade_size.as_decimal())

    def _close(self) -> None:
        held = self.held()
        if not self.extended_hours:
            self.close_all_positions(
                self.instrument_id, time_in_force=TimeInForce.DAY, reduce_only=False
            )
            return
        self._limit(OrderSide.SELL if held > 0 else OrderSide.BUY, abs(held))

    def _limit(self, side: OrderSide, quantity: Decimal) -> None:
        quote = self.cache.quote(self.instrument_id)
        if quote is None:
            self.dropped.append(f"{side.name}: no quote yet")
            self.log.warning(f"{side.name} signal dropped: no quote for {self.instrument_id} yet")
            return
        price = through_the_touch(
            side, quote.bid_price.as_decimal(), quote.ask_price.as_decimal(), self.limit_offset
        )
        instrument = self.cache.instrument(self.instrument_id)
        assert instrument is not None  # checked in `MaCross.on_start`
        order = self.order_factory.limit(
            self.instrument_id,
            side,
            instrument.make_qty(quantity),
            Price.from_str(f"{price:.{instrument.price_precision}f}"),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order, params=dict(EXTENDED_HOURS))
        self.clock.set_time_alert(
            f"limit-timeout-{order.client_order_id}",
            self.clock.utc_now() + timedelta(seconds=self.limit_timeout_secs),
            self._on_limit_timeout,
        )

    def _on_limit_timeout(self, event: TimeEvent) -> None:
        """Cancel the limit if it is still open; if Alpaca has not acknowledged it yet, look again after
        another timeout rather than leave it unwatched."""
        order = self.cache.order(ClientOrderId(event.name.removeprefix("limit-timeout-")))
        if order is not None and not order.is_closed:
            client_order_id = order.client_order_id.value
            if order.is_open:
                self.dropped.append(f"{order.side.name}: not filled in {self.limit_timeout_secs} s")
                self.log.warning(
                    f"{client_order_id} not filled in {self.limit_timeout_secs} s; canceled, "
                    "signal dropped"
                )
                self.cancel_order(order.client_order_id)
            else:
                self.clock.set_time_alert(
                    event.name,
                    self.clock.utc_now() + timedelta(seconds=self.limit_timeout_secs),
                    self._on_limit_timeout,
                )
