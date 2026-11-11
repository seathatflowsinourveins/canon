"""A hard cap on the account's net position in one instrument, checked on a timer inside the node.

Contract: a `nautilus_trader.trading.Strategy` that places no order, with the clock's `set_timer`, the
portfolio's `net_position` and `shutdown_system`, NautilusTrader v2.0.0rc8 (`trading/__init__.pyi`,
`portfolio/__init__.pyi`, `common/__init__.pyi`). It is a `Strategy` rather than a `DataActor` because at
the pin only a strategy has the portfolio from Python. Read 2026-11-10.

No native setting caps a position: the risk engine bounds each order's notional and the submit rate, not
the position they add up to (concepts/execution "Risk engine"; ADR 0037, where a strategy built a 4-share
short under both). So this checks, every `interval_secs`, the portfolio's net position for the instrument:
every strategy's, `EXTERNAL` included, which is what the venue holds once reconciliation has aligned it.
Alignment with Alpaca's own position is the execution engine's position check, `position_check_interval_secs`
(ADR 0044): it compares the cache with the adapter's position reports and corrects it, and this reads the
result, with no second client and no keys of its own.

Over `max_position` in either direction it logs an error naming the quantity and calls
`shutdown_system`, which stops the node. The cap sends no order, and flattens only through the strategies'
own stop: `SessionMaCross` in a session window runs upstream's market exit (`manage_stop`) and the window
then checks the account and closes what is left; upstream's `EmaCross` does not flatten on stop, so its
caller closes the position (`paper_node.ema_cross_paper_node`). It also trips on a position that
reconciliation inferred and the venue does not hold, failing closed. The first check runs at start.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import StrategyId
from nautilus_trader.trading import Strategy

from .ma_cross import refuse_another_type
from .ma_cross import refuse_unknown_keywords

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent

CAP_ID = StrategyId("PositionCap-CAP")


class PositionCapConfig(StrategyConfig):
    """`instrument_id` to watch, `max_position` in units of it (one share), `interval_secs` between checks.

    Build it with `position_cap_config`, which also gives the base fields (`strategy_id`, `order_id_tag`):
    at the pin they reach a `StrategyConfig` subclass only as constructor keywords (concepts/strategies,
    "Strategy configuration"). Any other keyword raises `TypeError` naming it
    (`ma_cross.refuse_unknown_keywords`), since upstream's `__new__` would drop it, and each field takes
    one type (`ma_cross.refuse_another_type`).
    """

    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        max_position: Decimal = Decimal(1),
        interval_secs: int = 1,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        refuse_another_type(
            self,
            {
                "instrument_id": instrument_id,
                "max_position": max_position,
                "interval_secs": interval_secs,
            },
            {"instrument_id": InstrumentId, "max_position": Decimal, "interval_secs": int},
        )
        if max_position < 0:
            raise ValueError(f"max_position must not be negative, got {max_position}")
        if interval_secs <= 0:
            raise ValueError(f"interval_secs must be positive, got {interval_secs}")
        self.instrument_id = instrument_id
        self.max_position = max_position
        self.interval_secs = interval_secs


def position_cap_config(
    instrument_id: InstrumentId, *, max_position: Decimal = Decimal(1), interval_secs: int = 1
) -> PositionCapConfig:
    """The cap's config under its own strategy ID and order tag."""
    return PositionCapConfig(
        instrument_id=instrument_id,
        max_position=max_position,
        interval_secs=interval_secs,
        strategy_id=CAP_ID,
        order_id_tag="CAP",
    )


class PositionCap(Strategy):
    """Stop the node when the net position in the instrument exceeds the cap. It never trades."""

    def __init__(self, config: PositionCapConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.max_position = config.max_position
        self.interval_secs = config.interval_secs
        self.checks = 0
        self.peak = Decimal(0)
        self.tripped: str | None = None

    def on_start(self) -> None:
        self.check()
        self.clock.set_timer(
            "position-cap", timedelta(seconds=self.interval_secs), callback=self._on_timer
        )

    def on_stop(self) -> None:
        self.clock.cancel_timer("position-cap")

    def _on_timer(self, event: TimeEvent) -> None:  # the timer only times the check
        self.check()

    def check(self) -> None:
        """Read the net position; over the cap, record why and shut the system down (once)."""
        self.checks += 1
        net = Decimal(self.portfolio.net_position(self.instrument_id))
        self.peak = max(self.peak, abs(net))
        if abs(net) > self.max_position and self.tripped is None:
            self.tripped = (
                f"net position {net} in {self.instrument_id} is over the cap of {self.max_position}"
            )
            self.log.error(f"PositionCap: {self.tripped}; shutting the system down")
            self.shutdown_system(self.tripped)
