"""Paper test: start-up reconciliation of the shared account's past orders leaves no position when Alpaca
holds none, the Execution Testing Spec's TC-E85 "Reconcile filled orders"
(https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md) on whatever filled orders
earlier runs left, with no lookback.

Upstream: NautilusTrader's reconciliation page at the pinned commit takes "An explicit flat report" as the
only evidence of flat ("Missing reports do not mean flat"); the execution client sends one for each loaded
instrument Alpaca lists no position in (package README, "Reports"). Without it, fills inferred from part of
the account's order history were kept as an `EXTERNAL` position Alpaca did not hold (2026-11-10, a short of
1 AAPL and a long of 10 SPY; ADR 0037). This test checks the outcome on paper, that no position is open;
that the flat reports are what produce it is shown offline (`test_execution_client.py`), since the test
cannot read the node's reports or log. It skips if start-up reconciled no filled order in AAPL or SPY,
which would leave nothing to show.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped with that reason. It places, modifies and cancels nothing, so it
needs no session. It stops, touching nothing, if the account holds an open order or a position: then the
account is not flat and the test has nothing to show. Paper endpoints only: the adapter builds its clients
with `paper=True`, and the account reads here use `TradingClient(paper=True)`.
"""

import asyncio
import os

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveNodeConfig
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import TraderId

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

INSTRUMENTS = ["AAPL.ALPACA", "SPY.ALPACA"]  # the two seen kept as EXTERNAL positions
START_SECS = 60.0
# Start-up, reconciliation and stop; each Alpaca request ends within its timeout.
RUN_LIMIT_SECS = 120.0


def test_paper_startup_with_past_filled_orders_and_a_flat_account_holds_no_position() -> None:
    client = with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    held = client.get_all_positions()
    if open_orders or held:
        pytest.fail(
            f"the paper account holds {len(open_orders)} open order(s) and {len(held)} position(s); "
            "it is not flat, and this test touched nothing"
        )
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-FLAT",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=30,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            exec_engine=LiveExecutionEngineConfig(reconciliation=True),  # no lookback
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=INSTRUMENTS),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run
    handle = node.handle()
    started: list[bool] = []

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        try:
            deadline = loop.time() + START_SECS
            while not task.done() and not handle.is_running and loop.time() < deadline:
                await asyncio.sleep(0.25)
            started.append(handle.is_running)
        finally:
            if not task.done():
                handle.stop()
            await task

    async def bounded() -> None:
        async with asyncio.timeout(RUN_LIMIT_SECS):
            await main()

    try:
        asyncio.run(bounded())
        positions = [
            (str(p.instrument_id), str(p.side), str(p.quantity)) for p in cache.positions_open()
        ]
        filled = [
            o
            for o in cache.orders()
            if o.status == OrderStatus.FILLED and o.instrument_id.value in INSTRUMENTS
        ]
        print(
            "flat-reconciliation observation:",
            {"orders": len(cache.orders()), "filled": len(filled), "positions_open": positions},
        )
    finally:
        node.dispose()

    assert started == [True], "the node did not start: start-up reconciliation failed"
    if not filled:
        pytest.skip(
            "start-up reconciled no filled AAPL or SPY order; there was nothing to net to flat"
        )
    assert positions == [], f"start-up left positions Alpaca does not hold: {positions}"
    after = (
        client.get_all_positions(),
        client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)),
    )
    assert after == ([], []), "the account changed during a test that places nothing"
