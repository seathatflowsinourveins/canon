"""Integration test against Alpaca's paper Trading API: a limit order far below the market, through a real
node, accepted and then canceled.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`
(`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Otherwise pytest reports it as skipped with that reason. Never
write the keys to a file.

What it calls: Alpaca's paper Trading API and its trade updates stream, through `TradingClient(paper=True)`
and `TradingStream(paper=True)` ("Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md).
It places one paper order: a DAY limit buy of 1 AAPL at $1.00, a price far below the market, so it is
not expected to fill, and cancels it once Alpaca has accepted it. Startup reconciliation reads the paper
account's orders and positions first; an open order there that this slice does not map fails the start.
Whether Alpaca accepts a limit price this far from the market, and how it treats a DAY order outside
market hours, are UNVERIFIED until this runs; Alpaca documents that a DAY order submitted after the
close "is queued and submitted the following trading day" (alpaca-py `TimeInForce`).
"""

import asyncio
import os

import pytest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

AAPL = InstrumentId.from_str("AAPL.ALPACA")


class AcceptThenCancel(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.venue_order_id: str | None = None

    def on_start(self) -> None:
        order = self.order_factory.limit(
            AAPL,
            OrderSide.BUY,
            Quantity.from_int(1),
            Price.from_str("1.00"),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)

    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name == "OrderInitialized":
            return
        self.events.append(name)
        if name == "OrderAccepted":
            self.venue_order_id = event.venue_order_id.value
            self.cancel_order(event.client_order_id)
        if name in {"OrderCanceled", "OrderRejected", "OrderDenied", "OrderCancelRejected"}:
            self.shutdown_system(f"Paper order ended with {name}")


async def run_hosted(node) -> None:
    async with asyncio.timeout(60):
        await node.run_async()


def test_paper_limit_order_is_accepted_and_canceled() -> None:
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-EXEC",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
    strategy = AcceptThenCancel()
    node.add_strategy(strategy)
    try:
        asyncio.run(run_hosted(node))
        has_account = node.cache.account(ACCOUNT_ID) is not None
    finally:
        # Disposed here, so a failure's traceback does not keep the node and fail the next paper test.
        node.dispose()

    assert has_account
    assert strategy.events == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    assert strategy.venue_order_id
