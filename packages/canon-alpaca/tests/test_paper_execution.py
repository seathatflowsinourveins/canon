"""Integration tests against Alpaca's paper Trading API: limit orders far below the market, through a real
node, accepted and then canceled, one by `CancelOrder` and two by one `CancelAllOrders`.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`
(`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Otherwise pytest reports it as skipped with that reason. Never
write the keys to a file.

What it calls: Alpaca's paper Trading API and its trade updates stream, through `TradingClient(paper=True)`
and `TradingStream(paper=True)` ("Paper Trading", https://docs.alpaca.markets/us/docs/paper-trading.md).
The first places one paper order: a DAY limit buy of 1 AAPL at $1.00, a price far below the market, so it
is not expected to fill, and cancels it once Alpaca has accepted it. The second is TC-E41 of upstream's
Execution Testing Spec, driven by upstream's `ExecTester`: a limit buy $50 below the best bid, canceled by
`CancelAllOrders` on stop (ADR 0036). That command reaches every AAPL order
the node holds, so the test places nothing, and fails, if the paper account already holds an open AAPL
order; it needs the regular session for quotes, and in a `finally` cancels any order of its own left open. Startup reconciliation reads the paper
account's orders and positions first; an open order there that this slice does not map fails the start.
Whether Alpaca accepts a limit price this far from the market, and how it treats a DAY order outside
market hours, are UNVERIFIED until this runs; Alpaca documents that a DAY order submitted after the
close "is queued and submitted the following trading day" (alpaca-py `TimeInForce`).
"""

import asyncio
import os
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.constants import ACCOUNT_ID
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import ClientId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.testkit import ExecTesterConfig
from nautilus_trader.trading import Strategy

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

AAPL = InstrumentId.from_str("AAPL.ALPACA")
TESTER = StrategyId("ExecTester-000")
"""The tester's strategy ID; the cache also holds every AAPL order reconciliation loaded from the account."""
TOB_OFFSET_TICKS = 500_000
"""$50 at the adapter's 0.0001 increment: far from the market, so nothing fills; the spec's default, 500
ticks, would be five cents."""


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


def paper_node() -> LiveNode:
    return LiveNode.build(
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


def paper_trading() -> TradingClient:
    return TradingClient(
        api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
    )


def open_aapl_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=["AAPL"]))
    assert isinstance(orders, list)
    return orders


def holds_aapl(trading: TradingClient) -> bool:
    try:
        trading.get_open_position("AAPL")
    except APIError as e:
        if e.status_code == 404:
            return False
        raise
    return True


@contextmanager
def aapl_left_as_found() -> Generator[dict[str, int]]:
    """Refuse to start while the paper account holds an open AAPL order or an AAPL position, and leave it so:
    afterwards, whatever happened, cancel every open AAPL order and close any AAPL position. The account
    held none at the start and the paper lock keeps other sessions out, so whatever is found is this
    test's. What had to be cleaned up is counted, for the test to fail on."""
    trading = paper_trading()
    held = open_aapl_orders(trading)
    assert held == [], (
        "the paper account already holds open AAPL orders; placing nothing: "
        + ", ".join(
            f"{o.client_order_id} ({getattr(o.side, 'value', o.side)} {o.qty} at {o.limit_price}, "
            f"submitted {o.submitted_at})"
            for o in held
        )
    )
    assert not holds_aapl(trading), (
        "the paper account already holds an AAPL position; placing nothing"
    )
    cleaned = {"orders": 0, "positions": 0}
    try:
        yield cleaned
    finally:
        for order in open_aapl_orders(trading):
            trading.cancel_order_by_id(order.id)
            cleaned["orders"] += 1
        if holds_aapl(trading):
            trading.close_position("AAPL")
            cleaned["positions"] += 1


def test_paper_limit_order_is_accepted_and_canceled() -> None:
    with aapl_left_as_found() as cleaned:
        node = paper_node()
        strategy = AcceptThenCancel()
        node.add_strategy(strategy)
        try:
            asyncio.run(run_hosted(node))
            has_account = node.cache.account(ACCOUNT_ID) is not None
        finally:
            # Disposed here, so a failure's traceback does not keep the node and fail the next paper test.
            node.dispose()

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert has_account
    assert strategy.events == [
        "OrderSubmitted",
        "OrderAccepted",
        "OrderPendingCancel",
        "OrderCanceled",
    ]
    assert strategy.venue_order_id


def test_paper_cancel_all_on_stop_tc_e41() -> None:
    """TC-E41, "Cancel all on stop", of upstream's Execution Testing Spec, driven by upstream's `ExecTester`
    with the spec's node setup (risk engine bypassed, reconciliation on). The tester places a limit buy
    `TOB_OFFSET_TICKS` below the best bid; on stop it sends `CancelAllOrders` (`strategy_only=false`,
    `crates/testkit/src/testers/exec/strategy.rs` at the pinned commit), which the client cancels order by
        order. Pass: each order ends `OrderCanceled`, and nothing was left for the cleanup. The row's limit
    sell is left out: from a flat account Alpaca refuses it, 403 "cannot open a short sell while a long
    buy order is open" (observed 2026-11-10), and the tester resubmits it on every quote."""
    with aapl_left_as_found() as cleaned:
        events = run_exec_tester(
            ExecTesterConfig(
                instrument_id=AAPL,
                client_id=ClientId("ALPACA"),
                order_qty=Quantity.from_int(1),
                enable_limit_buys=True,
                enable_limit_sells=False,  # see the docstring
                tob_offset_ticks=TOB_OFFSET_TICKS,
                limit_time_in_force=TimeInForce.DAY,
            ),
            count=1,
        )

    assert cleaned == {"orders": 0, "positions": 0}, cleaned
    assert len(events) == 1, events
    # The card's sequence is `OrderPendingCancel` then `OrderCanceled`; for `CancelAllOrders` the engine
    # emits no `OrderPendingCancel` (observed) and the binding has no method for a client to, so the last
    # event is checked.
    for names in events.values():
        assert names[-1] == "OrderCanceled", names


def run_exec_tester(config: ExecTesterConfig, count: int) -> dict[str, list[str]]:
    """Run upstream's `ExecTester` on a node with the spec's setup until it has `orders` orders accepted,
    then stop it (its stop cancels them), and return each of its orders' event names."""
    provider = InstrumentProviderConfig(load_ids=[str(AAPL)])
    node = (
        LiveNode.builder("CANON-ALPACA-PAPER-TESTER", TraderId("TESTER-001"), Environment.SANDBOX)
        .with_risk_engine_config(LiveRiskEngineConfig(bypass=True))
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=True))
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
    node.add_builtin_strategy("ExecTester", config)
    events: dict[str, list[str]] = {}
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run

    async def main() -> None:
        # Wait for the accepts with a deadline of its own, then stop the node and let it finish: a timeout
        # around the whole run would cancel the node's own shutdown.
        task = asyncio.ensure_future(node.run_async())
        deadline = asyncio.get_running_loop().time() + 60
        try:
            while not task.done() and asyncio.get_running_loop().time() < deadline:
                orders = cache.orders(instrument_id=AAPL, strategy_id=TESTER)
                if len(orders) == count and all(o.status == OrderStatus.ACCEPTED for o in orders):
                    break
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                node.handle().stop()
            await task

    try:
        asyncio.run(main())
        for order in cache.orders(instrument_id=AAPL, strategy_id=TESTER):
            events[order.client_order_id.value] = [type(e).__name__ for e in order.events()]
    finally:
        node.dispose()
    return events
