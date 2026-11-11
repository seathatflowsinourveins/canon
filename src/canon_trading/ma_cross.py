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
* The config refuses a keyword it does not have, with `TypeError` naming it. Upstream's
  `StrategyConfig.__new__` ignores an unknown keyword, and the `**_kwargs` that lets the base fields
  through would then keep it without a word, so `fast_perid=5` left `fast_period` at 10. Upstream has no
  strict option for `StrategyConfig`; its fixed config constructors raise `TypeError` for an unsupported
  keyword (https://nautilustrader.io/docs/md/latest/concepts/configuration.md, "Unknown-field handling
  depends on the construction path", read 2026-11-10), which `refuse_unknown_keywords` does for what
  `**_kwargs` holds beyond the base fields. The base fields are read from `StrategyConfig` by reflection,
  as `ledger` reads them, so a field upstream adds passes without a change here. Each own field also
  takes one type (`OWN_FIELDS`), as upstream's fixed constructors type their fields, so
  `fast_period=3.0` or a `Decimal` size is refused when built, not run as another value or met at start.
* A missing instrument raises in `on_start`. Upstream's handler example logs and calls `self.stop()`
  there; at v2.0.0rc8 that fails the trader's start with "Invalid state trigger STOPPED ->
  START_COMPLETED" (observed 2026-11-10), so the reason is raised instead and the node's start fails
  naming it.

Known limits:

* It decides from its open positions at each bar and does not track its own working orders. In a
  backtest an order sent from `on_bar` fills before the next bar (concepts/backtesting/bar-execution,
  "Order submission timing"). On a venue, a bar that arrives while an order is still working would send
  a second one, so this strategy is not run on paper: `SessionMaCross`, which has a working-order guard,
  is the one for that. A backtest with a latency model does the same: the order waits for a later bar,
  and on the test fixture's bars each cross was followed by a second order (6 fills and 1 rejected sell
  where 4 fills were meant, observed at v2.0.0rc8, ADR 0067). Adding a guard here would be a new strategy
  and so a new study, since the step 7 study keys on this one (ADR 0048).
* `on_stop` flattens, so stopping a node sells whatever this strategy holds.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import SimpleMovingAverage
from nautilus_trader.model import BarType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.trading import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model import Bar


BASE_FIELDS: frozenset[str] = frozenset(
    name for name, _ in inspect.getmembers(StrategyConfig, inspect.isgetsetdescriptor)
)
"""The base `StrategyConfig` fields at the pin: the pyo3 getters of the Rust struct."""


def refuse_unknown_keywords(config: StrategyConfig, kwargs: Mapping[str, object]) -> None:
    """Raise `TypeError` naming each keyword in `kwargs` that is not a base field.

    Call it from a `StrategyConfig` subclass's `__init__` with its `**_kwargs`: upstream's `__new__` has
    read the base fields from them and ignored the rest.
    """
    unknown = sorted(set(kwargs) - BASE_FIELDS)
    if unknown:
        raise TypeError(
            f"{type(config).__name__} got an unexpected keyword argument "
            + ", ".join(repr(name) for name in unknown)
            + ": NautilusTrader's StrategyConfig would drop it without a word",
        )


OWN_FIELDS: dict[str, type] = {
    "bar_type": BarType,
    "trade_size": Quantity,
    "fast_period": int,
    "slow_period": int,
}
"""`MaCrossConfig`'s own fields and the one type each takes."""


def refuse_another_type(
    config: StrategyConfig,
    fields: Mapping[str, object],
    types: Mapping[str, type | tuple[type, ...]],
    *,
    after: str = "",
) -> None:
    """Raise `TypeError` for a field of `types` whose value in `fields` is not of its type.

    An instance of the type, and a `bool` (an `int` subclass) only where `bool` is the type, so `True`
    is not a period and a `Decimal` is not a `Quantity`: such a value either runs as another (`3.0` as
    3, `"False"` as true) or fails only once the strategy starts, and its text form keys it apart from,
    or together with, the value it runs as. A numpy integer is not an `int` and is refused too. Not the
    exact type: at the pin a `BarType` is an instance of a variant subclass (observed:
    `BarType.from_str(...)` is a `Standard`). `after` is added to the message.
    """
    for name, expected in types.items():
        value = fields[name]
        if not isinstance(value, expected) or (isinstance(value, bool) and expected is not bool):
            wanted = (
                " or ".join(t.__name__ for t in expected)
                if isinstance(expected, tuple)
                else expected.__name__
            )
            raise TypeError(
                f"{type(config).__name__}'s {name} is {type(value).__name__} {value!r}, "
                f"where it takes {wanted}{after}",
            )


def refuse_what_cannot_run(
    fast_period: int, slow_period: int, trade_size: Quantity, *, after: str = ""
) -> None:
    """Raise `ValueError` unless 0 < `fast_period` < `slow_period` and `trade_size` is positive."""
    if not 0 < fast_period < slow_period:
        raise ValueError(
            f"MaCross needs 0 < fast_period < slow_period, got {fast_period} and {slow_period}"
            + after,
        )
    if not trade_size.is_positive():
        raise ValueError(f"MaCross needs a positive trade_size, got {trade_size}{after}")


def check_as_built(config: MaCrossConfig) -> None:
    """Raise unless `config`'s attributes are what `MaCrossConfig` builds: exactly `OWN_FIELDS`.

    `TypeError` for another set of attributes or a value of another type, `ValueError` for values the
    constructor refuses. `MaCrossConfig` builds no other attribute, so one added since (a base field's
    name in `vars(config)` shadows nothing the engine reads) is refused rather than read, and so is one
    changed to what the constructor would not take. A change to another value it would take is not told
    apart: the config then holds, and is keyed by, that value.
    """
    after = ", not as MaCrossConfig builds it"
    held = {name: value for name, value in vars(config).items() if not name.startswith("_")}
    if set(held) != set(OWN_FIELDS):
        raise TypeError(
            f"{type(config).__name__} holds {sorted(held)}, where MaCrossConfig builds "
            f"{sorted(OWN_FIELDS)}{after}",
        )
    refuse_another_type(config, held, OWN_FIELDS, after=after)
    refuse_what_cannot_run(config.fast_period, config.slow_period, config.trade_size, after=after)


class MaCrossConfig(StrategyConfig):
    """Parameters of `MaCross`.

    `bar_type` names the bars to subscribe to and, through its instrument ID, the instrument to trade.
    `trade_size` is the quantity of each entry order. `fast_period` and `slow_period` are the lengths, in
    bars, of the two simple moving averages; their defaults are the ones upstream's example uses. Any
    other keyword must be a base `StrategyConfig` field, and each of the four takes the type `OWN_FIELDS`
    gives it, or it raises `TypeError` naming it.
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
        refuse_unknown_keywords(self, _kwargs)
        refuse_another_type(
            self,
            {
                "bar_type": bar_type,
                "trade_size": trade_size,
                "fast_period": fast_period,
                "slow_period": slow_period,
            },
            OWN_FIELDS,
        )
        refuse_what_cannot_run(fast_period, slow_period, trade_size)
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
