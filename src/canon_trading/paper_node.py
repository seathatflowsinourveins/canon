"""A live node on Alpaca's paper environment running upstream's built-in `EmaCross`, under native risk limits.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveNode.add_builtin_strategy` and
`LiveRiskEngineConfig`, and the strategy `EmaCross` configured by `nautilus_trader.trading.EmaCrossConfig`.
Read 2026-11-10 (ADR 0037):

* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: register data and execution
  clients with `LiveNode.builder(...)`; the continuous reconciliation settings and their defaults.
* https://nautilustrader.io/docs/md/latest/concepts/execution/index.md ("Risk engine"): the engine sits on
  the submit and modify path; "Cancel and query commands route directly to other execution components and
  do not pass through the `RiskEngine`"; a failed submit check gives `OrderDenied`.
* `crates/trading/src/examples/strategies/ema_cross/` at the pinned commit (`d53849b216e2`): `EmaCross`
  subscribes to quotes, keeps a fast and a slow EMA of the mid, and on each cross sends one market order
  of `trade_size`, a buy when the fast crosses above, a sell when it crosses below. It has no position
  check and does not flatten on stop. `grid_mm/README.md` there: Python passes the config to
  `add_builtin_strategy` and "the strategy runs entirely in Rust".
* `crates/risk/src/python/config.rs` and `engine/mod.rs` at the same commit: a rate is
  `"limit/HH:MM:SS"`, default `100/00:00:01` for both; a submit over the limit is denied with reason
  `RATE_LIMIT_EXCEEDED`, a modify over it is rejected with "Exceeded MAX_ORDER_MODIFY_RATE".

The numbers and why each was chosen are ADR 0037's. This is an end-to-end check of the stack on paper,
not a strategy evaluation; ADR 0010 governs that, and nothing here ranks or recommends a strategy.

Paper only: the execution client is `canon_alpaca`'s, whose config refuses anything but paper trading.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from typing import TypedDict

from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TraderId
from nautilus_trader.trading import EmaCrossConfig

if TYPE_CHECKING:
    from nautilus_trader.model import InstrumentId

MAX_ORDER_SUBMIT_RATE = "50/00:01:00"
"""At most 50 submits in any minute: a quarter of Alpaca's 200 calls a minute (ADR 0037)."""
MAX_ORDER_MODIFY_RATE = "25/00:01:00"
"""At most 25 modifies in any minute (ADR 0037)."""
MAX_NOTIONAL_PER_ORDER_USD = Decimal(1000)
"""Per order, in the instrument's quote currency. Sized for SPY near $750 (ADR 0037): one share passes, two
do not. For a symbol priced far from that it caps nothing useful, or denies every order."""

EMA_CROSS = "EmaCross"
"""The type name `add_builtin_strategy` takes."""
EMA_CROSS_ID = StrategyId("EMA_CROSS-001")
TRADER_ID = TraderId("TESTER-001")
TRADE_SIZE = Quantity.from_int(1)
"""One share an order (the paper-run rule)."""


class RiskLimits(TypedDict):
    """The keywords `LiveRiskEngineConfig` and `nautilus_trader.risk.RiskEngineConfig` share."""

    bypass: bool
    max_order_submit_rate: str
    max_order_modify_rate: str
    max_notional_per_order: dict[str, Decimal]


def risk_limits(
    instrument_id: InstrumentId, *, max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE
) -> RiskLimits:
    """The risk engine's settings for a run on `instrument_id`, as keywords of either risk config class.

    `max_order_submit_rate` is the ceiling unless a run sets a tighter budget of its own, as the paper
    test of `EmaCross` does (ADR 0037).

    `LiveRiskEngineConfig` (the live node's) and `nautilus_trader.risk.RiskEngineConfig` (a backtest's)
    take the same keywords, so the offline proof of the throttle runs on exactly these values.
    """
    return {
        "bypass": False,
        "max_order_submit_rate": max_order_submit_rate,
        "max_order_modify_rate": MAX_ORDER_MODIFY_RATE,
        "max_notional_per_order": {str(instrument_id): MAX_NOTIONAL_PER_ORDER_USD},
    }


def ema_cross_config(instrument_id: InstrumentId) -> EmaCrossConfig:
    """`EmaCross` on `instrument_id`, one share an order, at upstream's default periods (10 and 50)."""
    return EmaCrossConfig(
        instrument_id=instrument_id,
        trade_size=TRADE_SIZE,
        strategy_id=EMA_CROSS_ID,
        order_id_tag="001",
    )


def ema_cross_paper_node(
    instrument_id: InstrumentId,
    *,
    name: str = "CANON-EMA-CROSS-PAPER",
    trader_id: TraderId = TRADER_ID,
    max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE,
) -> LiveNode:
    """Build a paper node with the Alpaca data and execution clients and `EmaCross` on `instrument_id`.

    The risk engine runs with `risk_limits` (with `max_order_submit_rate` in place of the ceiling when one
    is given), reconciliation left at upstream's default (on), and the node is not
    started: the caller runs it and owns its lifetime. The key pair is read from the environment by the
    clients when they connect (ADR 0029).
    """
    provider = InstrumentProviderConfig(load_ids=[str(instrument_id)])
    node = (
        LiveNode.builder(name, trader_id, Environment.SANDBOX)
        .with_risk_engine_config(
            LiveRiskEngineConfig(
                **risk_limits(instrument_id, max_order_submit_rate=max_order_submit_rate)
            )
        )
        .with_delay_post_stop_secs(5)
        .with_timeout_connection(20)
        .with_timeout_disconnection_secs(15)
        .add_data_client(
            "ALPACA", AlpacaDataClientFactory, DataClientConfig(instrument_provider=provider)
        )
        .add_exec_client(
            "ALPACA",
            AlpacaExecutionClientFactory,
            AlpacaExecutionClientConfig(instrument_provider=provider),
        )
        .build()
    )
    node.add_builtin_strategy(EMA_CROSS, ema_cross_config(instrument_id))
    return node
