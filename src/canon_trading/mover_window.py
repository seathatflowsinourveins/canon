"""What a session window adds to run `Mover` (ADR 0088): the run's rows, and the ranking on the bus.

Nothing here is a strategy. `session_window` builds the node and runs it; this module holds the three
things it adds for `--strategy Mover`:

* **The rows of a window's run**, each UNCALIBRATED (ADR 0088): the largest entry notional
  (`ENTRY_NOTIONAL_USD`), the risk engine's per-order cap as an outer bound at a multiple of it
  (`OUTER_BOUND_MULTIPLE`), one slot, one entry a window (`ENTRIES`), and a no-chase cap set so that it
  does not bind (`NO_CHASE_CAP`). The risk engine's per-order notional cap holds for every order whatever
  its side (`nautilus-trader@d53849b216e2`, `crates/risk/src/engine/mod.rs:2491-2507`), so a cap at the
  entry's own notional would deny the sale of a position that rose. The entry is therefore bounded where
  it is sized, in the strategy, and the engine's cap only stops a runaway order.
* **`FixedRank`**, a `DataActor` (upstream's class for a component that publishes, concepts/actors) that
  publishes one ranking on the `movers-rank` signal (`DataActor.publish_signal`,
  `python/nautilus_trader/common/__init__.pyi:720`). It is the bridge until the ranking actor exists
  (ADR 0087): the window chooses one name before the node is built, and this actor repeats that choice
  with the time it was computed as the signal's `ts_event`, never a later one, so the strategy's staleness
  row measures the choice's real age. A signal is not replayed to a later subscriber at the pin, hence the
  repeat. The staleness row of such a run reaches from the choice to the window's end (`mover_parts`),
  and the window's record says so.
* **`mover_parts`**, which builds the strategy, the actor and the position cap for one name.
"""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.common import DataActor
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .mover import RANK_SIGNAL
from .mover import Mover
from .mover import MoverConfig
from .mover import format_rank
from .position_cap import PositionCap
from .position_cap import position_cap_config

if TYPE_CHECKING:
    from datetime import datetime

    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import InstrumentId

MOVER = "Mover"
"""The name `--strategy` takes."""
MOVER_ID = StrategyId("Mover-001")
ENTRY_NOTIONAL_USD = Decimal(500)
"""The largest notional of an entry in a window (UNCALIBRATED; the first run's row, 2026-11-11)."""
OUTER_BOUND_MULTIPLE = 10
"""The risk engine's per-order notional cap for a mover node, as a multiple of `ENTRY_NOTIONAL_USD`
(UNCALIBRATED): an outer bound against a runaway order, which a sale meets only once the position is
worth this many times its entry."""
OUTER_BOUND_USD = ENTRY_NOTIONAL_USD * OUTER_BOUND_MULTIPLE
NO_CHASE_CAP = Decimal(100)
"""`MoverConfig.max_move` for a window: 100 times the last close, above any move the selection has shown,
so the row is stated and does not bind until a measurement sets it (UNCALIBRATED)."""
ENTRIES = 1
"""Entries a window may fill: stage one is one slot and one trade (ADR 0088)."""
CHASE = "resend"
"""How a window re-prices a resting sale: cancel, see the cancel's terminal state at the next step, then
send the new limit for what is held. No order of a mover node is modified at the venue (the lead's
decision of 2026-11-11, until the adapter's replace chain has passed two clean outside reviews); the cost
is two requests a re-price and a moment with no working sale."""
LIVE_BARS = "1-MINUTE-LAST-INTERNAL"
"""One-minute bars the data engine builds from the trade stream: the adapter streams no bars."""
RANK_REPEAT_SECS = 15
"""How often `FixedRank` repeats its ranking."""
REPEAT_TIMER = "fixed-rank"


class FixedRank(DataActor):
    """Publish one ranking, computed before the node was built, every `RANK_REPEAT_SECS`."""

    def __init__(self) -> None:
        # `DataActor` is a native class whose `__new__` takes only a config, so `mover_parts` sets the
        # ranking after construction (as `bars_to_catalog._RequestBars` is built).
        super().__init__()
        self.value = ""
        self.computed_ns = 0
        self.published = 0

    def on_start(self) -> None:
        self.clock.set_timer(
            REPEAT_TIMER, timedelta(seconds=RANK_REPEAT_SECS), callback=self._publish
        )

    def _publish(self, event: TimeEvent) -> None:
        self.publish_signal(RANK_SIGNAL, self.value, ts_event=self.computed_ns)
        self.published += 1

    def on_stop(self) -> None:
        if self.clock.timer_names().count(REPEAT_TIMER):
            self.clock.cancel_timer(REPEAT_TIMER)


def max_shares(price: Decimal) -> int:
    """The shares the largest entry buys at `price`, the name's price when the window chose it: the
    strategy's bound on an entry in shares, and the position cap's bound."""
    if price <= 0:
        raise ValueError(f"the price must be positive, got {price}")
    return int(ENTRY_NOTIONAL_USD // price)


def mover_parts(
    instrument_id: InstrumentId,
    *,
    price: Decimal,
    move: Decimal,
    computed_at: datetime,
    ends: datetime,
    extended: bool,
    feed: str | None,
    wind_down_at: datetime,
    market_exit_interval_ms: int,
    market_exit_max_attempts: int,
) -> tuple[Mover, FixedRank, PositionCap]:
    """`Mover` on the one name a window chose, the actor that publishes that choice as the ranking, and
    the position cap at the shares the largest entry buys at `price`. `move` is the name's move from its
    last close at `computed_at`, as the choice measured it; the staleness row reaches from then to
    `ends`, the window's end, since the choice is made once. In the session the wind-down and the stop are upstream's market
    exit with DAY orders that Alpaca takes; outside it every order is a marked limit and the stop
    cancels."""
    shares = max_shares(price)
    if shares < 1:
        raise ValueError(
            f"one share of {instrument_id} at {price} USD is over the largest entry of "
            f"{ENTRY_NOTIONAL_USD} USD"
        )
    strategy = Mover(
        MoverConfig(
            max_notional=ENTRY_NOTIONAL_USD,
            max_move=NO_CHASE_CAP,
            max_shares=shares,
            entries=ENTRIES,
            slots=1,
            watch=1,
            bar_spec=LIVE_BARS,
            chase=CHASE,
            rank_max_age_secs=max(1, math.ceil((ends - computed_at).total_seconds())),
            extended_hours=extended,
            feed=feed,
            wind_down_at=wind_down_at,
            strategy_id=MOVER_ID,
            order_id_tag="001",
            manage_stop=not extended,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
            market_exit_interval_ms=market_exit_interval_ms,
            market_exit_max_attempts=market_exit_max_attempts,
        )
    )
    ranking = FixedRank()
    ranking.value = format_rank([(instrument_id, move)])
    ranking.computed_ns = dt_to_unix_nanos(computed_at)
    cap = PositionCap(position_cap_config(instrument_id, max_position=Decimal(shares)))
    return strategy, ranking, cap
