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
  side. A limit not filled within `limit_timeout_secs` is canceled (again if the cancel is rejected) and its
  signal dropped, never chased (one that filled in part drops nothing):
  a dropped entry is not sent again until the fast average has gone below the slow, nor a dropped close
  until it has gone above (a bar with the two equal is no cross), seen on any bar, the dropped limit's
  cancel still pending or not; the next cross takes its own. A short's
  buy-back and the wind-down's close are not signals and are not held back. With no quote yet the signal is dropped for that bar.

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

**Warm-up** (`warm_up=True`). A window starts with no state from the last (no cache backing for a Python
client, ADR 0047), so the averages would need `slow_period` live bars before any decision. Instead, as
upstream's EMA cross does in `on_start` (concepts/strategies, "Handler example": register the indicators,
`request_bars`, subscribe; "Registered indicators receive the bars from a request response before
`on_historical_bars()` runs, so a request hydrates them"), the `EXTERNAL` form of the live bar
type, the bars the adapter serves, is requested before the live subscription, at least an hour of it, and
from `warm_up_start` when that is earlier: the run sets it where the window's own part of the session is too
thin for the slow average (pre-market, `session_window.warm_up_start`), so the request reaches back into the
prior session. `Strategy.request_bars(bar_type, start=..., end=None, ...)` takes any `start`
(`nautilus_trader/trading/__init__.pyi` line 993 at v2.0.0rc8; `crates/common/src/python/actor.rs` line 2956
at `d53849b216e2`), sent as a span with no `limit`, since alpaca-py 0.45.0's bar request sorts ascending by
default (`alpaca/data/requests.py` line 39 at `c42add7c0a70`) and a limit would keep the oldest bars; the
adapter pages Alpaca's bars from `start` until every page has arrived (`canon_alpaca.data`), and the averages
keep the last bars fed.
One departure: the averages are registered for the live bar type only, and `on_historical_bars` feeds the
history to them, oldest first, only while no live bar has reached them; a later answer (after a live bar,
or after `WARM_UP_TIMEOUT_SECS`) is dropped and counted, so older bars never follow newer ones. Decisions
wait until the request completes or times out (`hydrated`). The live bars stay the `INTERNAL` ones built
from trades.

**Feed** (`feed`). The quote and trade subscriptions and the warm-up request name it in their params,
`{"feed": ...}`, Alpaca's own parameter name, as the adapter takes it (ADR 0055); with none they name
none and the adapter streams IEX. Quotes and trades are subscribed before the bars, since the data engine
hands the client only an instrument's first subscription, so the trades the bars are built from come on
the same feed. Each quote and trade that arrives is counted (`quotes`, `trades`) for the run's report.

Known limit: the strategy does not know the session calendar (the run checks it before it starts).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from types import NoneType
from typing import TYPE_CHECKING
from typing import Any

from nautilus_trader.model import AggregationSource
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce

from .ma_cross import MaCross
from .ma_cross import MaCrossConfig
from .ma_cross import refuse_another_type

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar
    from nautilus_trader.model import Quantity

EXTENDED_HOURS = {"extended_hours": True}
"""The submit params that mark an order as extended-hours eligible for the adapter."""
LIMIT_OFFSET = Decimal("0.05")
"""Five ticks of Alpaca's $0.01 increment through the touch: outside the session the book is thinner and a
limit at the touch may never fill; five cents bounds the price paid at a fraction of a basis point on SPY."""
LIMIT_TIMEOUT_SECS = 30
CENT = Decimal("0.01")
WARM_UP_SPARE_BARS = 5
"""Bars requested beyond `slow_period`, for minutes the feed had no trade in."""
WARM_UP_TIMEOUT_SECS = 60
"""How long decisions wait for the warm-up's answer before going on with live bars alone."""
WARM_UP_SPAN = timedelta(hours=1)
"""At least this much history is asked for: on a thin feed (pre-market, after-hours) the last
`slow_period` minutes can hold fewer than `slow_period` bars; the averages keep the last ones fed."""
WARM_UP_ALERT = "warm-up-timeout"


class SessionMaCrossConfig(MaCrossConfig):
    """`MaCrossConfig` plus the session settings described in the module header.

    `wind_down_at` is an aware UTC `datetime`. The base fields (`strategy_id`, `order_id_tag`) are given as
    constructor keywords, as for any `StrategyConfig` subclass at the pin (concepts/strategies). Any
    other keyword reaches `MaCrossConfig`, which raises `TypeError` naming it, and each session setting
    takes one type (`ma_cross.refuse_another_type`), so `extended_hours="False"` is refused, not run as
    true.
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
        warm_up: bool = False,
        warm_up_start: datetime | None = None,
        feed: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            bar_type=bar_type,
            trade_size=trade_size,
            fast_period=fast_period,
            slow_period=slow_period,
            **kwargs,
        )
        refuse_another_type(
            self,
            {
                "extended_hours": extended_hours,
                "limit_offset": limit_offset,
                "limit_timeout_secs": limit_timeout_secs,
                "wind_down_at": wind_down_at,
                "warm_up": warm_up,
                "warm_up_start": warm_up_start,
                "feed": feed,
            },
            {
                "extended_hours": bool,
                "limit_offset": Decimal,
                "limit_timeout_secs": int,
                "wind_down_at": (datetime, NoneType),
                "warm_up": bool,
                "warm_up_start": (datetime, NoneType),
                "feed": (str, NoneType),
            },
        )
        if limit_offset < 0 or limit_offset != limit_offset.quantize(CENT):
            raise ValueError(
                f"limit_offset must be whole cents and not negative, got {limit_offset}"
            )
        if limit_timeout_secs <= 0:
            raise ValueError(f"limit_timeout_secs must be positive, got {limit_timeout_secs}")
        if wind_down_at is not None and wind_down_at.tzinfo is None:
            raise ValueError("wind_down_at must be an aware datetime")
        if warm_up_start is not None and warm_up_start.tzinfo is None:
            raise ValueError("warm_up_start must be an aware datetime")
        if warm_up_start is not None and not warm_up:
            raise ValueError("warm_up_start needs warm_up=True; without it nothing is requested")
        self.extended_hours = extended_hours
        self.limit_offset = limit_offset
        self.limit_timeout_secs = limit_timeout_secs
        self.wind_down_at = wind_down_at
        if warm_up and not bar_type.spec.is_time_aggregated():
            raise ValueError(f"warm_up needs time bars to size its request, got {bar_type}")
        self.warm_up = warm_up
        self.warm_up_start = warm_up_start
        self.feed = feed


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
        self.warm_up = config.warm_up
        self.warm_up_start = config.warm_up_start
        self.feed_params = {"feed": config.feed} if config.feed else None
        """The params naming the feed in each subscription and the warm-up request (ADR 0055)."""
        self.quotes = 0
        self.trades = 0
        """The quotes and trades that reached the strategy, for the run's report."""
        self.history_bar_type = BarType(
            self.bar_type.instrument_id, self.bar_type.spec, AggregationSource.EXTERNAL
        )
        self.hydrated = not config.warm_up
        """Whether the warm-up response has arrived, or no warm-up was asked for."""
        self.history_bars = 0
        self.history_dropped = 0
        self.live_bars = 0
        self.winding_down = False
        self.dropped: list[str] = []
        """Why each dropped signal was dropped, for the run's report."""
        self.dropped_side: OrderSide | None = None
        """The side of the last signal limit dropped at its timeout, held until the opposite cross."""
        self.signal_orders: set[str] = set()
        """The client order IDs of limits sent on a cross (an entry, or a close on the cross below)."""
        self.timed_out: set[str] = set()
        """The client order IDs of limits canceled at their timeout and not yet closed."""

    def on_start(self) -> None:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None or self.trade_size.precision > instrument.size_precision:
            super().on_start()  # `MaCross`'s own checks raise, before anything is requested
            return
        if (
            self.warm_up
        ):  # requested before the live subscription, as upstream's handler example does
            self.hydrated = False
            self.history_bars = 0
            self.history_dropped = 0
            self.live_bars = 0
            self.request_bars(
                self.history_bar_type,
                start=self._warm_up_from(),
                callback=self._on_history,
                params=self.feed_params,
            )
            self.clock.set_time_alert(
                WARM_UP_ALERT,
                self.clock.utc_now() + timedelta(seconds=WARM_UP_TIMEOUT_SECS),
                self._on_history_timeout,
            )
        # Quotes and trades first, on the feed: the data engine hands the client only an instrument's
        # first subscription (ADR 0055), so the trades the bars are built from come on this feed too.
        # Quotes price the limits outside the session, and both show the stream delivered (the report).
        self.subscribe_quotes(self.instrument_id, params=self.feed_params)
        self.subscribe_trades(self.instrument_id, params=self.feed_params)
        super().on_start()
        if self.wind_down_at is not None:
            self.clock.set_time_alert(
                "wind-down", self.wind_down_at, self._wind_down, allow_past=True
            )

    def on_quote(self, quote) -> None:
        self.quotes += 1

    def on_trade(self, trade) -> None:
        self.trades += 1

    def _warm_up_from(self) -> datetime:
        """The warm-up request's start: `_warm_up_span` before now, or `warm_up_start` if that is
        earlier."""
        start = self.clock.utc_now() - self._warm_up_span()
        if self.warm_up_start is not None:
            start = min(start, self.warm_up_start.astimezone(UTC))
        return start

    def _warm_up_span(self) -> timedelta:
        """`WARM_UP_SPAN`, or `slow_period` plus `WARM_UP_SPARE_BARS` bars if that is longer."""
        bars = (self.slow.period + WARM_UP_SPARE_BARS) * self.bar_type.spec.timedelta
        return max(WARM_UP_SPAN, bars)

    def on_historical_bars(self, bars) -> None:
        """Feed the requested bars to the averages, oldest first, only while no live bar has reached
        them: the averages are registered for the live bar type alone, so a late answer (after a live
        bar, or after the timeout) is dropped rather than applied after newer bars."""
        if self.live_bars or self.hydrated:
            self.history_dropped += len(bars)
            self.log.warning(f"Warm-up: {len(bars)} bars arrived after live bars; dropped")
            return
        for bar in sorted(bars, key=lambda b: b.ts_init):
            self.fast.handle_bar(bar)
            self.slow.handle_bar(bar)
        self.history_bars += len(bars)

    def _on_history_timeout(self, event: TimeEvent) -> None:  # the alert only times it
        if not self.hydrated:
            self.log.warning(
                f"Warm-up: no answer in {WARM_UP_TIMEOUT_SECS} s; deciding on live bars"
            )
            self.hydrated = True

    def _on_history(self, request_id: str) -> None:
        """The request's completion, whether served or failed (both observed at the pin)."""
        if not self.hydrated:
            self.hydrated = True
            self.log.info(
                f"Warm-up: {self.history_bars} bars of {self.history_bar_type}; "
                f"averages initialized: {self.indicators_initialized()}"
            )
        if WARM_UP_ALERT in self.clock.timer_names():
            self.clock.cancel_timer(WARM_UP_ALERT)

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
        if bar.bar_type == self.bar_type:
            self.live_bars += 1  # the averages have already taken this bar
        decides = self.hydrated and self.indicators_initialized()
        above = below = False
        if decides:  # an opposite cross clears a dropped signal on any bar, an order working or not
            above, below = self.fast.value > self.slow.value, self.fast.value < self.slow.value
            self._clear_dropped(above=above, below=below)
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
        if not decides:
            return
        if above and not held and self.dropped_side != OrderSide.BUY:
            self._signal(self._enter())
        elif below and held > 0 and self.dropped_side != OrderSide.SELL:
            self._signal(self._close())

    def _clear_dropped(self, *, above: bool, below: bool) -> None:
        """A dropped entry is held until the fast average is below the slow, a dropped close until it is
        above: the opposite cross, not a bar on which the two are equal."""
        if (self.dropped_side == OrderSide.BUY and below) or (
            self.dropped_side == OrderSide.SELL and above
        ):
            self.dropped_side = None

    def _signal(self, client_order_id: str | None) -> None:
        if client_order_id is not None:
            self.signal_orders.add(client_order_id)

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

    def _enter(self) -> str | None:
        """Send the entry; return the limit's client order ID, if one was sent."""
        if not self.extended_hours:
            self.submit_order(
                self.order_factory.market(self.instrument_id, OrderSide.BUY, self.trade_size)
            )
            return None
        return self._limit(OrderSide.BUY, self.trade_size.as_decimal())

    def _close(self) -> str | None:
        """Close the signed position; return the limit's client order ID, if one was sent."""
        held = self.held()
        if not self.extended_hours:
            self.close_all_positions(
                self.instrument_id, time_in_force=TimeInForce.DAY, reduce_only=False
            )
            return None
        return self._limit(OrderSide.SELL if held > 0 else OrderSide.BUY, abs(held))

    def _limit(self, side: OrderSide, quantity: Decimal) -> str | None:
        quote = self.cache.quote(self.instrument_id)
        if quote is None:
            self.dropped.append(f"{side.name}: no quote yet")
            self.log.warning(f"{side.name} signal dropped: no quote for {self.instrument_id} yet")
            return None
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
        return order.client_order_id.value

    def _order(self, client_order_id: ClientOrderId) -> Any | None:
        """The cached order (`Cache.order` is typed `Any | None` at the pin): one seam, so a test can give
        the timeout an order without a venue."""
        return self.cache.order(client_order_id)

    def _on_limit_timeout(self, event: TimeEvent) -> None:
        """Cancel the limit if it is still open, and look at it again after each further timeout until it
        is closed: one Alpaca has not acknowledged yet is looked at again, one whose cancel was rejected
        (open again) is canceled again, and its signal is dropped once. One whose cancel went unanswered
        stays pending cancel, which the strategy cannot cancel again (`mark_order_pending_cancel` at the
        pin): the node's in-flight check (which queries it, answered by the adapter's `_query_order`), its
        open-order check (every 10 s) and the window's account check after the stop are what reconcile
        and cancel it. A limit that filled in part, by its timeout or by its close, is no dropped signal:
        what it left is the next cross's."""
        name = event.name.removeprefix("limit-timeout-")
        order = self._order(ClientOrderId(name))
        if order is None or order.is_closed:
            if (
                order is not None
                and name in self.timed_out
                and order.filled_qty.as_decimal()
                and self.dropped_side == order.side
            ):
                self.dropped_side = None  # it filled in part while its cancel was on its way
            self.signal_orders.discard(name)
            self.timed_out.discard(name)
            return
        client_order_id = order.client_order_id.value
        if order.is_pending_cancel:
            self.log.warning(f"{client_order_id} still pending cancel; looking again")
        elif order.is_open:
            if client_order_id not in self.timed_out:
                self.timed_out.add(client_order_id)
                filled = bool(order.filled_qty.as_decimal())
                self.dropped.append(
                    f"{order.side.name}: {'filled in part' if filled else 'not filled'} in "
                    f"{self.limit_timeout_secs} s"
                )
                # not a short's buy-back or a wind-down, and nothing filled
                if client_order_id in self.signal_orders and not filled:
                    self.dropped_side = order.side
                self.signal_orders.discard(client_order_id)
                self.log.warning(
                    f"{client_order_id} {'filled in part' if filled else 'not filled'} in "
                    f"{self.limit_timeout_secs} s; canceled"
                    + ("; the rest is the next cross's" if filled else ", signal dropped")
                )
            else:
                self.log.warning(f"{client_order_id} still open after its cancel; canceling again")
            self.cancel_order(order.client_order_id)
        self._rearm(event.name)

    def _rearm(self, name: str) -> None:
        """Look at the limit again after another timeout (one seam, so a test can see it re-armed)."""
        self.clock.set_time_alert(
            name,
            self.clock.utc_now() + timedelta(seconds=self.limit_timeout_secs),
            self._on_limit_timeout,
        )
