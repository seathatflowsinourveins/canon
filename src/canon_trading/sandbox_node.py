"""A sandbox node: Alpaca's live market data, with orders matched locally by NautilusTrader's sandbox
execution client and never sent to Alpaca (ADR 0070).

Contract: NautilusTrader's `examples/live/sandbox/exec_tester.py` at the pinned commit (`d53849b216e2`),
configured only: `LiveNode.builder(..., Environment.SANDBOX)`, `with_reconciliation(reconciliation=False)`,
`LiveRiskEngineConfig(bypass=True)`, a venue's live data client through `add_data_client`, and
`add_simulated_exec_client(venue, SandboxExecutionClientFactory(), SandboxExecutionClientConfig(venue=...,
starting_balances=..., account_id=..., fee_model=...))`, then the built-in `ExecTester` through
`add_builtin_strategy` (v2.0.0rc8, `nautilus_trader.adapters.sandbox` and `nautilus_trader.testkit`). Here
the data client is `canon_alpaca`'s, on venue `ALPACA`, so the sandbox fills on Alpaca's quotes and trades.

How it is kept from trading (ADR 0070, terms 3 and 6):

* It runs in data-only mode, which `mise run paper:data` sets and `main` sets again: every alpaca-py REST
  client `canon_alpaca` builds refuses every method but GET before sending, and the data client refuses to
  connect if one does not (`canon_alpaca.providers.require_get_only`).
* After the build it checks that no `AlpacaExecutionClient` exists, and stops if one does.

What it models: the sandbox's own matching, on a simulated `CASH` account with `NETTING`, $100,000 and zero
fees (ADR 0070). Alpaca's rules are not modelled (the adapter's denials, the wash-trade refusal, paper's
partial fills), so a run is comparable to paper only for orders the adapter would pass. A built-in
strategy subscribes with no params, so it streams IEX, which carries nothing overnight (ADR 0055).

Evidence and limits: the sandbox with Alpaca's data client ran once on paper at the rc8 pin, 2026-11-10
(ADR 0070, Confirmation). NautilusTrader v2.0.0rc9 fixes the sandbox's open-order check rejecting resting
orders and GTD fills after expiry, so running this unattended waits for that pin move (ADR 0068); a person
runs it with `mise run paper:data sandbox`. Not investment advice: the tester's orders exercise the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import sys
from decimal import Decimal
from typing import TYPE_CHECKING

from canon_alpaca.constants import VENUE
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import data_only
from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import ClientId
from nautilus_trader.model import Currency
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.testkit import ExecTesterConfig

from .paper_node import DELAY_POST_STOP_SECS
from .paper_node import TIMEOUT_CONNECTION_SECS
from .paper_node import TIMEOUT_DISCONNECTION_SECS
from .paper_node import TIMEOUT_SHUTDOWN_SECS
from .paper_node import TRADER_ID

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nautilus_trader.common import Cache

ALPACA = VENUE.value
ACCOUNT_ID = AccountId("ALPACA-SANDBOX-001")
"""The simulated account, as upstream's example names one (`BINANCE-SANDBOX-001`)."""
STARTING_BALANCE = Money(100_000.0, Currency.from_str("USD"))
INSTRUMENT_ID = InstrumentId.from_str("SPY.ALPACA")
STRATEGY_ID = StrategyId("EXEC_TESTER-001")
ORDER_QTY = Quantity.from_int(1)
TOB_OFFSET_TICKS = 500_000
"""Resting orders $50 from the touch at the adapter's 0.0001 price increment, as the paper matrix rests
them (`test_paper_matrix.tester`)."""
MAX_MINUTES = 60
"""A run is at most an hour, as a session window is (ADR 0044)."""
STOP_TIMEOUT_SECS = DELAY_POST_STOP_SECS + TIMEOUT_DISCONNECTION_SECS + TIMEOUT_SHUTDOWN_SECS + 30
"""How long a stop may take before the run is reported failed: the node's own post-stop delay,
disconnection and shutdown bounds, and 30 s for the loop."""


class DataOnlyRefused(RuntimeError):
    """The node would hold a client that can trade at Alpaca, or the process is not in data-only mode."""


def sandbox_node(name: str = "CANON-SANDBOX") -> LiveNode:
    """The node of upstream's example with Alpaca's data client, the sandbox execution client on venue
    `ALPACA` and `ExecTester` on SPY. Refused unless the process is in data-only mode, and checked after
    the build by `refuse_alpaca_execution`."""
    if not data_only():
        raise DataOnlyRefused(
            f"the sandbox node runs in data-only mode only ({DATA_ONLY_ENV}=1, ADR 0070); nothing was built"
        )
    provider = InstrumentProviderConfig(load_ids=[str(INSTRUMENT_ID)])
    node = (
        LiveNode.builder(name, TRADER_ID, Environment.SANDBOX)
        .with_reconciliation(reconciliation=False)
        .with_risk_engine_config(LiveRiskEngineConfig(bypass=True))
        .with_delay_post_stop_secs(DELAY_POST_STOP_SECS)
        .with_timeout_connection(TIMEOUT_CONNECTION_SECS)
        .with_timeout_disconnection_secs(TIMEOUT_DISCONNECTION_SECS)
        .with_delay_shutdown_secs(TIMEOUT_SHUTDOWN_SECS)
        .add_data_client(
            ALPACA, AlpacaDataClientFactory, DataClientConfig(instrument_provider=provider)
        )
        .add_simulated_exec_client(
            ALPACA,
            SandboxExecutionClientFactory(),
            SandboxExecutionClientConfig(
                venue=VENUE,
                starting_balances=[STARTING_BALANCE],
                account_id=ACCOUNT_ID,
                base_currency=STARTING_BALANCE.currency,
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
            ),
        )
        .build()
    )
    refuse_alpaca_execution(node)
    node.add_builtin_strategy("ExecTester", exec_tester_config())
    return node


def exec_tester_config() -> ExecTesterConfig:
    """Upstream's example's tester on SPY, one share: a position opened on the first quote, post-only limit
    orders resting on both sides, and on stop every order canceled and the position closed. No book: the
    adapter serves quotes and trades only."""
    return ExecTesterConfig(
        strategy_id=STRATEGY_ID,
        instrument_id=INSTRUMENT_ID,
        client_id=ClientId(ALPACA),
        external_order_instrument_ids=[INSTRUMENT_ID],
        order_qty=ORDER_QTY,
        subscribe_book=False,
        subscribe_quotes=True,
        subscribe_trades=True,
        open_position_on_start_qty=Decimal(1),
        open_position_on_first_quote=True,
        open_position_time_in_force=TimeInForce.IOC,
        enable_limit_buys=True,
        enable_limit_sells=True,
        tob_offset_ticks=TOB_OFFSET_TICKS,
        use_post_only=True,
        cancel_orders_on_stop=True,
        close_positions_on_stop=True,
        reduce_only_on_stop=False,
        dry_run=False,
        log_data=False,
    )


def alpaca_execution_clients() -> list[AlpacaExecutionClient]:
    """Every `AlpacaExecutionClient` alive in this process. The node keeps its clients in Rust and lists
    none (v2.0.0rc8), and a factory builds its client at `build()`, so the objects are read from the
    collector, as the rc8 proof's probe did (ADR 0070), after a collection so a client kept only by a cycle
    of an earlier build is not counted. The sandbox module builds one node a process, so any found is its."""
    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, AlpacaExecutionClient)]


def refuse_alpaca_execution(node: LiveNode) -> None:
    """Raise `DataOnlyRefused`, disposing of `node`, if an Alpaca execution client exists (ADR 0070)."""
    found = alpaca_execution_clients()
    if found:
        node.dispose()
        raise DataOnlyRefused(
            f"{len(found)} AlpacaExecutionClient(s) exist after the build; a sandbox run holds none "
            "(ADR 0070); nothing was run"
        )


async def run_for(node: LiveNode, seconds: float) -> None:
    """Run `node` for `seconds`, stop it and wait at most `STOP_TIMEOUT_SECS` for the stop."""
    task = asyncio.ensure_future(node.run_async())
    try:
        await asyncio.wait({task}, timeout=seconds)
    finally:
        if not task.done():
            node.handle().stop()
        await asyncio.wait({task}, timeout=STOP_TIMEOUT_SECS)
    if not task.done():
        raise RuntimeError(f"the sandbox node did not stop within {STOP_TIMEOUT_SECS} s")
    task.result()


def summary(cache: Cache) -> dict[str, object]:
    """What the run did, from the node's cache: the simulated account's orders, those with any fill, and
    what is left open."""
    orders = cache.orders(strategy_id=STRATEGY_ID)
    return {
        "orders": len(orders),
        "filled": sum(1 for o in orders if o.filled_qty.as_double() > 0),
        "open_orders": len(cache.orders_open(strategy_id=STRATEGY_ID)),
        "positions_open": len(cache.positions_open(strategy_id=STRATEGY_ID)),
        "account": str(a.id) if (a := cache.account_for_venue(VENUE)) is not None else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.sandbox_node", description=__doc__
    )
    parser.add_argument("--minutes", type=int, default=10, help=f"run length, 1 to {MAX_MINUTES}")
    args = parser.parse_args(argv)
    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be 1 to {MAX_MINUTES}")
    os.environ[DATA_ONLY_ENV] = (
        "1"  # the task sets it too; the module does not rely on that (ADR 0070)
    )
    node = sandbox_node()
    cache = (
        node.cache
    )  # taken before the run, as NautilusTrader requires for a node run by `run_async`
    try:
        asyncio.run(run_for(node, args.minutes * 60))
        done = summary(cache)
        print("sandbox run:", done, flush=True)
    finally:
        node.dispose()
    if done["open_orders"] or done["positions_open"]:
        print(
            "sandbox run did not end flat: the tester's stop left an order or a position open",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
