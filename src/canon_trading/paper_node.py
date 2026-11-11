"""A live node on Alpaca's paper environment running upstream's built-in `EmaCross`, under native risk limits.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveNode.add_builtin_strategy` and
`LiveRiskEngineConfig`, and the strategy `EmaCross` configured by `nautilus_trader.trading.EmaCrossConfig`.
Read 2026-11-10 (ADR 0037, ADR 0049):

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

The numbers and why each was chosen are ADR 0049's. This is an end-to-end check of the stack on paper,
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
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TraderId
from nautilus_trader.trading import EmaCrossConfig

from .position_cap import PositionCap
from .position_cap import position_cap_config

if TYPE_CHECKING:
    from nautilus_trader.live import LiveNodeBuilder
    from nautilus_trader.model import InstrumentId

MAX_ORDER_SUBMIT_RATE = "50/00:01:00"
"""At most 50 submits in any minute: a quarter of Alpaca's 200 calls a minute (ADR 0049)."""
MAX_ORDER_MODIFY_RATE = "25/00:01:00"
"""At most 25 modifies in any minute (ADR 0049)."""
MAX_NOTIONAL_PER_ORDER_USD = Decimal(1000)
"""Per order, in the instrument's quote currency (ADR 0049), first sized for SPY near $750: one share passes,
two do not. A session window runs on any symbol whose one share fits it, read from the latest sip trade
before the node is built (`session_window.one_share_fits`); on a cheaper symbol it bounds an order at more
than one share's worth."""

EMA_CROSS = "EmaCross"
"""The type name `add_builtin_strategy` takes."""
EMA_CROSS_ID = StrategyId("EMA_CROSS-001")
TRADER_ID = TraderId("TESTER-001")
TRADE_SIZE = Quantity.from_int(1)
"""One share an order (the paper-run rule)."""

OPEN_CHECK_INTERVAL_SECS = 10.0
POSITION_CHECK_INTERVAL_SECS = 60.0
"""The native runtime checks, off by default, turned on at the slow end of upstream's recommended ranges
("5-10s" and "30-60s", how_to/configure_live_trading): about six and one Alpaca calls a minute (ADR 0044)."""
DELAY_POST_STOP_SECS = 5
TIMEOUT_CONNECTION_SECS = 20
TIMEOUT_DISCONNECTION_SECS = 15
TIMEOUT_SHUTDOWN_SECS = 5
"""The node's own shutdown bounds (`crates/live/src/node/config.rs` at the pin): residual events after the
trader stops, the clients' disconnect, then pending tasks; `with_delay_shutdown_secs` sets
`timeout_shutdown` (`node/builder.rs`), here at its default. A session window bounds its wait on the stop
from these (`session_window.STOP_TIMEOUT_SECS`), a regular one with a longer post-stop delay for its managed
stop's market exit (`session_window.MANAGED_DELAY_POST_STOP_SECS`)."""

RECONCILIATION_LOOKBACK_MINS = 1
"""A minute of order history at start-up, the value the adapter's own paper tests use
(`packages/canon-alpaca/tests/test_paper_matrix.py`). Every run here starts only once it has checked the
account flat in its symbol, so history can only mislead: left unset, as the docs' tip allows, the adapter
is given everything and reconciliation infers fills from old filled orders into a position Alpaca does
not hold (ADR 0037); and any window as long as a run (upstream's Lighter tutorial uses 60 minutes) can
hold the last run's exit without its entry. Open orders and positions are reported however old."""


def exec_engine_config(instrument_id: InstrumentId) -> LiveExecutionEngineConfig:
    """The execution engine with its open-order and position checks against the venue turned on, and
    start-up reconciliation scoped to the instrument, as upstream's Lighter tutorial scopes it to its
    target, and to `RECONCILIATION_LOOKBACK_MINS` of history."""
    return LiveExecutionEngineConfig(
        reconciliation=True,
        reconciliation_instrument_ids=[str(instrument_id)],
        reconciliation_lookback_mins=RECONCILIATION_LOOKBACK_MINS,
        open_check_interval_secs=OPEN_CHECK_INTERVAL_SECS,
        position_check_interval_secs=POSITION_CHECK_INTERVAL_SECS,
    )


class RiskLimits(TypedDict):
    """The keywords `LiveRiskEngineConfig` and `nautilus_trader.risk.RiskEngineConfig` share."""

    bypass: bool
    max_order_submit_rate: str
    max_order_modify_rate: str
    max_notional_per_order: dict[str, Decimal]


def risk_limits(
    instrument_id: InstrumentId,
    *,
    max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE,
    max_notional_per_order: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
) -> RiskLimits:
    """The risk engine's settings for a run on `instrument_id`, as keywords of either risk config class.

    `max_order_submit_rate` is the ceiling unless a run sets a tighter budget of its own, as the paper
    test of `EmaCross` does (ADR 0037). The notional cap is one value for any symbol, which a
    window checks one share against before it starts (`session_window.one_share_fits`). A run whose
    strategy sizes its own entries gives `max_notional_per_order` as an outer bound above them, since
    the engine holds a sale to the cap as it does a buy (`mover_window`, ADR 0088).

    `LiveRiskEngineConfig` (the live node's) and `nautilus_trader.risk.RiskEngineConfig` (a backtest's)
    take the same keywords, so the offline proof of the throttle runs on exactly these values.
    """
    return {
        "bypass": False,
        "max_order_submit_rate": max_order_submit_rate,
        "max_order_modify_rate": MAX_ORDER_MODIFY_RATE,
        "max_notional_per_order": {str(instrument_id): max_notional_per_order},
    }


def ema_cross_config(instrument_id: InstrumentId) -> EmaCrossConfig:
    """`EmaCross` on `instrument_id`, one share an order, at upstream's default periods (10 and 50)."""
    return EmaCrossConfig(
        instrument_id=instrument_id,
        trade_size=TRADE_SIZE,
        strategy_id=EMA_CROSS_ID,
        order_id_tag="001",
    )


def alpaca_paper_builder(
    name: str,
    instrument_id: InstrumentId,
    *,
    trader_id: TraderId = TRADER_ID,
    max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE,
    max_notional_per_order: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
    delay_post_stop_secs: int = DELAY_POST_STOP_SECS,
) -> LiveNodeBuilder:
    """A builder with the Alpaca data and execution clients for `instrument_id`, `risk_limits`,
    `exec_engine_config` and the timeouts every paper node here uses; the caller adds logging and builds.
    `delay_post_stop_secs` is longer for a strategy whose managed stop runs a market exit, which the node
    runs only inside that delay (`session_window.MANAGED_DELAY_POST_STOP_SECS`).
    The calls are the ones upstream's live examples make (`examples/live/sandbox/exec_tester.py`)."""
    provider = InstrumentProviderConfig(load_ids=[str(instrument_id)])
    return (
        LiveNode.builder(name, trader_id, Environment.SANDBOX)
        .with_risk_engine_config(
            LiveRiskEngineConfig(
                **risk_limits(
                    instrument_id,
                    max_order_submit_rate=max_order_submit_rate,
                    max_notional_per_order=max_notional_per_order,
                )
            )
        )
        .with_exec_engine_config(exec_engine_config(instrument_id))
        .with_delay_post_stop_secs(delay_post_stop_secs)
        .with_timeout_connection(TIMEOUT_CONNECTION_SECS)
        .with_timeout_disconnection_secs(TIMEOUT_DISCONNECTION_SECS)
        .with_delay_shutdown_secs(TIMEOUT_SHUTDOWN_SECS)
        .add_data_client(
            "ALPACA", AlpacaDataClientFactory, DataClientConfig(instrument_provider=provider)
        )
        .add_exec_client(
            "ALPACA",
            AlpacaExecutionClientFactory,
            AlpacaExecutionClientConfig(instrument_provider=provider),
        )
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
    is given), the execution engine with `exec_engine_config`, and `PositionCap` beside the strategy: no
    native setting caps a position, and `EmaCross` stacks one when Alpaca refuses an opposite order (ADR
    0037). When the cap trips it stops the node and nothing flattens: `EmaCross` does not on stop, and its
    config at the pin exposes neither `manage_stop` nor the market-exit keys, whose default exit is
    reduce-only, which the adapter denies (ADR 0036). The caller closes what is left, as the paper test's
    `left_as_found` does. The node is not started: the caller runs it and owns its lifetime. The key pair
    is read from the environment by the clients when they connect (ADR 0029).
    """
    node = alpaca_paper_builder(
        name, instrument_id, trader_id=trader_id, max_order_submit_rate=max_order_submit_rate
    ).build()
    node.add_builtin_strategy(EMA_CROSS, ema_cross_config(instrument_id))
    node.add_strategy(PositionCap(position_cap_config(instrument_id)))
    return node
