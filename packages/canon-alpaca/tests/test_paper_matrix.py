"""Paper tests: rows of NautilusTrader's Data Testing Spec and Execution Testing Spec against Alpaca's paper
environment, driven by upstream's own built-in testers.

Upstream: NautilusTrader v2.0.0rc8 exposes the Rust `DataTester` actor and `ExecTester` strategy to Python
through `nautilus_trader.testkit.DataTesterConfig` and `ExecTesterConfig`, added with
`LiveNode.add_builtin_actor("DataTester", ...)` and `add_builtin_strategy("ExecTester", ...)`
(https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md and
`spec_exec_testing.md`, "Python node setup"). The execution spec asks for the risk engine bypassed and
reconciliation on; both are set here. Each test runs one node for a fixed time and then stops it the way an
operator would, through the node's handle, so the testers' own `on_stop` (unsubscribe, cancel, close) runs.
The rows each test covers are named in its docstring; the adapter README's matrix records the outcomes.

They run only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports them as skipped with that reason. The stream rows and every row that needs a fill
need the regular session and skip, with that reason, when Alpaca's clock says it is closed or closes within
ten minutes. Paper endpoints only: the clients are built with `paper=True` by the adapter, and the account
reads and clean-up here use `TradingClient(paper=True)`.

The account is left as found. Before a node starts, a test reads the open orders and positions and stops,
touching nothing, if there are any. Afterwards, pass or fail, it cancels any order its node placed that is
still open at Alpaca and sells or buys back whatever position in the test symbol is left, which can only be
its own since it found none and the paper lock keeps other runs out. Smallest size: one share, except the
rejection row, which asks for more than the account can buy so that Alpaca refuses it.
"""

import asyncio
import os
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTif
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveNodeConfig
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.testkit import DataTesterConfig
from nautilus_trader.testkit import ExecTesterConfig

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)

SYMBOL = "AAPL"
AAPL = InstrumentId.from_str(f"{SYMBOL}.ALPACA")
MINUTE = BarType.from_str(f"{SYMBOL}.ALPACA-1-MINUTE-LAST-EXTERNAL")
ALPACA = ClientId("ALPACA")
START_TIMEOUT_SECS = 60
TESTER = StrategyId("ExecTester-000")
DONE = {"filled", "canceled", "expired", "rejected", "replaced"}


def trading_client() -> TradingClient:
    return with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )


def require_session(client: TradingClient) -> None:
    clock = client.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    if not clock.is_open or clock.next_close - clock.timestamp < timedelta(minutes=10):
        pytest.skip("the regular session is closed or closes within ten minutes; this row needs it")


def require_empty_account(client: TradingClient) -> None:
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    held = client.get_all_positions()
    if open_orders or held:
        pytest.fail(
            f"the paper account holds {len(open_orders)} open order(s) and {len(held)} position(s) "
            "this test did not open; it started nothing and touched nothing"
        )


def build_node(name: str, *, execution: bool) -> LiveNode:
    provider = InstrumentProviderConfig(load_ids=[str(AAPL)])
    exec_clients = (
        {"ALPACA": AlpacaExecutionClientConfig(instrument_provider=provider)} if execution else {}
    )
    return LiveNode.build(
        name,
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=15,
            # The post-stop grace runs before disconnection, which "discards queued commands" (python
            # adapter guide, "Disconnect and drain tasks"): the testers' unsubscribes, cancels and closing
            # orders need it to reach the clients.
            delay_post_stop_secs=10 if execution else 2,
            risk_engine=LiveRiskEngineConfig(bypass=True),
            # The paper account is shared and has a long order history; start-up reconciliation reads
            # only the last minute's orders plus every open one, so earlier runs' fills are not replayed.
            exec_engine=LiveExecutionEngineConfig(
                reconciliation=True, reconciliation_lookback_mins=1
            ),
            data_clients={"ALPACA": DataClientConfig(instrument_provider=provider)},
            exec_clients=exec_clients,
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
        exec_factories={"ALPACA": AlpacaExecutionClientFactory} if execution else None,
    )


def run_for(node: LiveNode, seconds: float) -> None:
    """Run the node for `seconds` once it is running, then stop it through its handle and let it finish.

    As in `test_paper_execution.py`: the wait has a deadline of its own, since a timeout around the whole
    run would cancel the node's own shutdown, where the testers unsubscribe, cancel and close.
    """
    handle = node.handle()

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        try:
            started = loop.time() + START_TIMEOUT_SECS
            while not task.done() and not handle.is_running and loop.time() < started:
                await asyncio.sleep(0.25)
            assert handle.is_running or task.done(), (
                f"the node did not start in {START_TIMEOUT_SECS} s"
            )
            ends = loop.time() + seconds
            while not task.done() and loop.time() < ends:
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                handle.stop()
            await task

    asyncio.run(main())


def positions(client: TradingClient) -> list[Position]:
    held = client.get_all_positions()
    assert isinstance(held, list), f"Alpaca answered {held!r} for the positions"
    return [p for p in held if isinstance(p, Position)]


def leave_as_found(client: TradingClient, client_order_ids: list[str]) -> list[str]:
    """Cancel this node's orders still open at Alpaca and close what is left in the test symbol."""
    actions = []
    for cid in client_order_ids:
        try:
            order = client.get_order_by_client_id(cid)
        except APIError as error:
            if error.status_code != 404:
                raise
            print(f"cleanup: Alpaca holds no order {cid} (denied or refused before Alpaca took it)")
            continue
        assert isinstance(order, Order), f"Alpaca answered {order!r} for order {cid}"
        if order.status.value not in DONE:
            client.cancel_order_by_id(order.id)
            actions.append(f"canceled {cid}")
    deadline = time.monotonic() + 30
    while client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])):
        assert time.monotonic() < deadline, "this test's orders are still open at Alpaca after 30 s"
        time.sleep(1)
    for position in positions(client):
        if position.symbol != SYMBOL:
            continue
        qty = abs(int(float(position.qty)))
        side = AlpacaSide.SELL if float(position.qty) > 0 else AlpacaSide.BUY
        close = client.submit_order(
            MarketOrderRequest(symbol=SYMBOL, qty=qty, side=side, time_in_force=AlpacaTif.DAY)
        )
        assert isinstance(close, Order), f"Alpaca answered {close!r} to the closing order"
        actions.append(f"closed {position.qty} {SYMBOL} with {close.id}")
    deadline = time.monotonic() + 60
    while client.get_all_positions():
        assert time.monotonic() < deadline, "the position this test opened is still open after 60 s"
        time.sleep(1)
    return actions


def run_exec_tester(name: str, config: ExecTesterConfig, seconds: float) -> dict:
    """Run one ExecTester node against paper and return what its cache and Alpaca held afterwards.

    Only the tester's own orders and position are reported and cleaned up: start-up reconciliation can
    also bring in the last minute's closed orders from other runs, as external orders, and those are
    never touched.
    """
    client = trading_client()
    require_empty_account(client)
    node = build_node(name, execution=True)
    node.add_builtin_strategy("ExecTester", config)
    cache = node.cache  # a node run by `run_async` is read through the cache taken before the run
    result: dict = {"orders": []}
    cids: list[str] = []
    try:
        run_for(node, seconds)
        orders = sorted(cache.orders(strategy_id=TESTER), key=lambda o: o.ts_init)
        cids = [o.client_order_id.value for o in orders]
        result["orders"] = [
            {
                "id": o.client_order_id.value,
                "type": o.order_type,
                "side": o.side,
                "tif": o.time_in_force,
                "status": o.status,
                "filled": o.filled_qty.as_double(),
                "events": [type(e).__name__ for e in o.events()],
            }
            for o in orders
        ]
        result["positions_open"] = len(cache.positions_open(strategy_id=TESTER))
        result["account"] = cache.account_for_venue(AAPL.venue) is not None
        print(f"{name}:", result)
    finally:
        try:
            if not cids:
                cids = [o.client_order_id.value for o in cache.orders(strategy_id=TESTER)]
        finally:
            try:
                node.dispose()
            finally:
                # Runs even when the test or the node failed; a clean-up error is chained to that one.
                result["cleanup"] = leave_as_found(client, cids)
                print(f"{name} cleanup:", result["cleanup"])
    return result


def tester(**changes: Any) -> ExecTesterConfig:
    """The tester for one row: one share, resting orders $50 from the touch (500,000 ticks of the
    instrument's 0.0001 price increment), one side only (Alpaca refuses an opposite-side order while one
    is open: "potential wash trade detected"), and no reduce-only on stop (this slice denies it). Open
    orders are canceled on stop with cancel-all, upstream's default, unless a row asks otherwise.
    """
    config: dict[str, Any] = {
        "instrument_id": AAPL,
        "client_id": ALPACA,
        "order_qty": Quantity.from_int(1),
        "tob_offset_ticks": 500_000,
        "enable_limit_buys": False,
        "enable_limit_sells": False,
        "reduce_only_on_stop": False,
    }
    return ExecTesterConfig(**{**config, **changes})


def of_type(result: dict, order_type: OrderType) -> list[dict]:
    return [o for o in result["orders"] if o["type"] == order_type]


def test_paper_data_tester_quotes_trades_and_bars() -> None:
    """TC-D03 (load one instrument), TC-D20 (quotes), TC-D30 (trades), TC-D41 (historical bars) and
    TC-D70 (unsubscribe on stop)."""
    require_session(trading_client())
    node = build_node("CANON-ALPACA-PAPER-DATA-TESTER", execution=False)
    node.add_builtin_actor(
        "DataTester",
        DataTesterConfig(
            client_id=ALPACA,
            instrument_ids=[AAPL],
            bar_types=[MINUTE],
            subscribe_quotes=True,
            subscribe_trades=True,
            request_bars=True,
            can_unsubscribe=True,
        ),
    )
    cache = node.cache  # read through the cache taken before the run, as above
    try:
        run_for(node, 30)
        instrument = cache.instrument(AAPL)
        quotes = cache.quotes(AAPL) or []
        trades = cache.trades(AAPL) or []
        bars = cache.bars(MINUTE) or []
    finally:
        node.dispose()
    print(f"data tester: {len(quotes)} quotes, {len(trades)} trades, {len(bars)} minute bars")

    assert instrument is not None  # TC-D03
    assert quotes, "TC-D20: no QuoteTick in 30 s of the regular session"
    # TC-D20: at least one quote with bid below ask and both sizes positive, and none crossed. A locked
    # quote (bid equal to ask) can occur; one-sided quotes are dropped by the client.
    assert any(
        q.bid_price.as_double() < q.ask_price.as_double()
        and q.bid_size.as_double() > 0
        and q.ask_size.as_double() > 0
        for q in quotes
    ), "TC-D20: no quote with bid below ask and both sizes positive"
    crossed = [q for q in quotes if q.bid_price.as_double() > q.ask_price.as_double()]
    assert not crossed, f"TC-D20: crossed quotes {crossed[:3]}"
    assert trades, "TC-D30: no TradeTick in 30 s of the regular session"
    assert all(t.price.as_double() > 0 and t.size.as_double() > 0 for t in trades)
    assert bars, "TC-D41: no historical minute bar"
    for b in bars:
        low, high = b.low.as_double(), b.high.as_double()
        assert low <= b.open.as_double() <= high and low <= b.close.as_double() <= high, b
    ts = sorted(b.ts_event for b in bars)
    assert len(set(ts)) == len(ts), "TC-D41: duplicate bar timestamps"


def test_paper_exec_tester_market_and_limit_buy() -> None:
    """TC-E01 (market buy fills, position long), TC-E10 (GTC limit buy accepted), TC-E41 and TC-E81
    (canceled on stop by cancel-all), TC-E06 and TC-E82 (position closed with a market sell on stop)."""
    require_session(trading_client())
    result = run_exec_tester(
        "CANON-ALPACA-PAPER-EXEC-BUY",
        tester(open_position_on_start_qty=Decimal(1), enable_limit_buys=True),
        seconds=25,
    )
    markets, limits = of_type(result, OrderType.MARKET), of_type(result, OrderType.LIMIT)
    assert [o["side"] for o in markets] == [OrderSide.BUY, OrderSide.SELL], markets
    assert all(o["status"] == OrderStatus.FILLED and o["filled"] == 1 for o in markets), markets
    assert limits and all(o["side"] == OrderSide.BUY for o in limits), limits
    assert all("OrderAccepted" in o["events"] for o in limits), limits
    assert all(o["status"] == OrderStatus.CANCELED for o in limits), limits
    assert result["positions_open"] == 0
    assert result["account"]
    assert result["cleanup"] == [], f"the tester left something at Alpaca: {result['cleanup']}"


def test_paper_exec_tester_limit_sell_against_a_long() -> None:
    """TC-E11 (GTC limit sell accepted), held against the long the tester opens so it is not a short
    sale; TC-E41 and TC-E82 as above. The tester sends its sell as soon as it has a quote, and while the
    opening market buy is still open Alpaca refuses it: 403 "potential wash trade detected". Those
    refusals are allowed here; once the buy has filled, a sell must be accepted and then canceled."""
    require_session(trading_client())
    result = run_exec_tester(
        "CANON-ALPACA-PAPER-EXEC-SELL",
        tester(open_position_on_start_qty=Decimal(1), enable_limit_sells=True),
        seconds=25,
    )
    limits = of_type(result, OrderType.LIMIT)
    assert limits and all(o["side"] == OrderSide.SELL for o in limits), limits
    accepted = [o for o in limits if "OrderAccepted" in o["events"]]
    assert accepted, f"TC-E11: no limit sell was accepted: {limits}"
    assert all(o["status"] == OrderStatus.CANCELED for o in accepted), accepted
    assert all(o["status"] == OrderStatus.REJECTED for o in limits if o not in accepted), limits
    markets = of_type(result, OrderType.MARKET)
    assert all(o["status"] == OrderStatus.FILLED for o in markets), markets
    assert result["positions_open"] == 0
    assert result["cleanup"] == [], f"the tester left something at Alpaca: {result['cleanup']}"


def test_paper_exec_tester_day_limit() -> None:
    """TC-E19 (DAY limit buy accepted) and TC-E42 (canceled on stop, one by one)."""
    require_session(trading_client())  # the tester prices its limit off a live quote
    result = run_exec_tester(
        "CANON-ALPACA-PAPER-EXEC-DAY",
        tester(
            enable_limit_buys=True,
            limit_time_in_force=TimeInForce.DAY,
            use_individual_cancels_on_stop=True,
        ),
        seconds=20,
    )
    limits = of_type(result, OrderType.LIMIT)
    assert limits and all(o["tif"] == TimeInForce.DAY for o in limits), limits
    assert all("OrderAccepted" in o["events"] for o in limits), limits
    assert all(o["status"] == OrderStatus.CANCELED for o in limits), limits
    assert result["cleanup"] == [], f"the tester left something at Alpaca: {result['cleanup']}"


def test_paper_exec_tester_unsupported_denied() -> None:
    """TC-E72 (an order type this slice does not send: market-if-touched) and TC-E73 (a time in force
    it does not send: IOC) are denied before they reach Alpaca."""
    require_session(trading_client())  # the tester prices both orders off a live quote
    result = run_exec_tester(
        "CANON-ALPACA-PAPER-EXEC-DENIED",
        tester(
            enable_limit_buys=True,
            limit_time_in_force=TimeInForce.IOC,
            enable_stop_buys=True,
            stop_order_type=OrderType.MARKET_IF_TOUCHED,
        ),
        seconds=20,
    )
    assert of_type(result, OrderType.LIMIT), "TC-E73: no IOC limit was tried"
    assert of_type(result, OrderType.MARKET_IF_TOUCHED), "TC-E72: no MIT order was tried"
    for o in result["orders"]:
        assert o["status"] == OrderStatus.DENIED, o
        assert "OrderSubmitted" not in o["events"], o
    assert result["cleanup"] == []


def test_paper_exec_tester_rejection() -> None:
    """A venue rejection: a market buy larger than the paper account can pay for, which Alpaca refuses
    ("Create an Order", 403), so the order ends `REJECTED` and nothing opens."""
    require_session(trading_client())
    result = run_exec_tester(
        "CANON-ALPACA-PAPER-EXEC-REJECT",
        tester(open_position_on_start_qty=Decimal(1_000_000)),
        seconds=15,
    )
    markets = of_type(result, OrderType.MARKET)
    assert markets and markets[0]["status"] == OrderStatus.REJECTED, markets
    assert result["positions_open"] == 0
    assert result["cleanup"] == []
