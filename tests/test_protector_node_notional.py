"""The protector node's risk limits never deny one protective sell of a large holding.

The lead's question before W2 (2026-11-13): what per-order notional cap and submit limits does
paper:protector's node carry, and does ONE protective sell of a holding worth about $300,000 pass
its risk engine? W2's longs on B are small and cannot show it. A cap that denied the sell would
leave the holding frozen with no exit under the defaults.

The node's limits are read from `protector_node` itself (`paper_node.risk_limits`, as
`alpaca_paper_builder` passes them to `LiveRiskEngineConfig`). They are then run through the pinned
engine: a real LiveNode with unit 10's in-memory adapter clients (no transport, no credential),
OwnerProtector self-arming a reconciled EXTERNAL long of 30,000 shares at $10 with $100,000 cash.
The risk engine's notional check denies with NOTIONAL_EXCEEDS_MAX_PER_ORDER (crates/risk/src/engine
at the pin; tests/test_risk_limits.py); the contrast case runs the same node under the paper
default cap (`MAX_NOTIONAL_PER_ORDER_USD`) and is denied, so this test can see a denial.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from alpaca.data.models import Quote
from alpaca.trading.models import Asset
from canon_alpaca.constants import PRICE_INCREMENT
from canon_alpaca.constants import PRICE_PRECISION
from canon_alpaca.data import parse_quote
from canon_alpaca.providers import parse_equity
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import ExecutionClientConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue

from canon_trading import paper_node
from canon_trading import protector_node as module
from canon_trading.owner_protector import OwnerProtector
from canon_trading.owner_protector import OwnerProtectorConfig
from canon_trading.price_step import tick
from tests.test_owner_protector import AAA
from tests.test_owner_protector import EXTERNAL
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import SyntheticData
from tests.test_owner_protector import SyntheticExecution
from tests.test_owner_protector import quote
from tests.test_protector_node import config

SHARES = 30_000  # about $300,000 at $10
ASSET_AAA = {
    "class": "us_equity",
    "exchange": "NASDAQ",
    "fractionable": False,
    "id": "00000000-0000-4000-8000-000000000001",
    "marginable": True,
    "name": "A sub-dollar name",
    "shortable": False,
    "status": "active",
    "symbol": "AAA",
    "tradable": True,
    "easy_to_borrow": False,
}
pytestmark = pytest.mark.usefixtures("new_york_regular_hours")

QUOTE_AAA = {"T": "q", "S": "AAA", "bx": "U", "bs": 50, "ax": "Q", "as": 50, "c": ["R"], "z": "C"}


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


@pytest.fixture
def controls(tmp_path):
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    return holds, inbox


def protector_limits(controls, monkeypatch) -> dict[str, Any]:
    """The risk limits paper:protector's own node is built with."""
    seen: list[dict[str, Any]] = []
    original = paper_node.risk_limits

    def capture(*args, **kwargs):
        seen.append(dict(original(*args, **kwargs)))
        return seen[-1]

    monkeypatch.setattr(paper_node, "risk_limits", capture)
    node, _strategy = module.protector_node(config(controls), (AAA,), feed="sip")
    node.dispose()
    assert len(seen) == 1
    return seen[0]


def test_the_protector_node_carries_no_notional_cap_and_the_paper_rates(controls, monkeypatch):
    limits = protector_limits(controls, monkeypatch)
    assert limits == {
        "bypass": False,
        "max_order_submit_rate": paper_node.MAX_ORDER_SUBMIT_RATE,
        "max_order_modify_rate": paper_node.MAX_ORDER_MODIFY_RATE,
        "max_notional_per_order": {},
    }


def run_large_holding(
    limits: dict[str, Any],
    *,
    shares: int = SHARES,
    entry: str = "10.00",
    data: type[SyntheticData] | None = None,
) -> tuple[Any, list[Any], list[Any]]:
    gc.collect()
    clients: list[SyntheticExecution] = []
    released: list[Any] = []

    class Recording(OwnerProtector):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.submits: list[Any] = []
            self.fills: list[Any] = []
            self.denied: list[Any] = []

        def _say(self, text):
            pass

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.submits.append(order)
            super().submit_order(order, position_id, client_id, params)

        def on_order_denied(self, event):
            self.denied.append(event)
            self.shutdown_system("protective sell denied")

        def on_order_filled(self, event):
            self.fills.append(event)
            self.shutdown_system("protective sell filled")

    class Data(SyntheticData):
        async def _connect(self):
            await super()._connect()
            self._handle_data(quote("10.00", self.clock.timestamp_ns()))

        async def publish_tape(self):
            for bid in [*(["10.00"] * 3), "9.78", "9.77", "9.76"]:
                self._handle_data(quote(bid, self.clock.timestamp_ns()))
                await asyncio.sleep(0.1)

    class Execution(SyntheticExecution):
        async def _submit_order(self, command) -> None:
            released.append(command.order)
            await super()._submit_order(command)

        async def _generate_position_status_reports(self, command):
            now = self.clock.timestamp_ns()
            return [
                PositionStatusReport(
                    AccountId("ALPACA-001"),
                    AAA,
                    PositionSide.LONG,
                    Quantity.from_int(shares),
                    now - 7 * 24 * 3600 * SECOND,
                    now,
                    avg_px_open=Decimal(entry),
                )
            ]

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return (data or Data)(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = Execution(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )
            clients.append(client)
            return client

    node = LiveNode.build(
        "OWNER-PROTECTION-NOTIONAL-TEST",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            risk_engine=LiveRiskEngineConfig(**limits),
            data_clients={"ALPACA": DataClientConfig()},
            exec_clients={"ALPACA": ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    protector = Recording(OwnerProtectorConfig(strategy_id=PROTECTOR, row="OWN1"))
    node.add_strategy(protector)

    async def bounded():
        await asyncio.sleep(5)
        protector.shutdown_system("end bounded notional test")

    async def run():
        finish = asyncio.create_task(bounded())
        try:
            async with asyncio.timeout(8):
                await node.run_async()
        finally:
            finish.cancel()
            await asyncio.gather(finish, return_exceptions=True)

    try:
        asyncio.run(run())
        external = [p for p in node.cache.positions() if p.strategy_id == EXTERNAL]
        return protector, external, released
    finally:
        node.dispose()


def test_one_protective_sell_of_a_300k_holding_passes_the_protector_nodes_risk_engine(
    controls, monkeypatch
):
    protector, external, _released = run_large_holding(protector_limits(controls, monkeypatch))
    assert not protector.denied, [str(d.reason) for d in protector.denied]
    assert len(protector.submits) == 1
    assert protector.submits[0].quantity == Quantity.from_int(SHARES)
    assert protector.submits[0].quantity.as_decimal() * protector.submits[
        0
    ].trigger_price.as_decimal() >= Decimal(290_000)
    assert len(protector.fills) == 1 and protector.fills[0].last_qty == Quantity.from_int(SHARES)
    assert all(p.is_closed for p in external)


def test_the_same_sell_under_the_paper_default_cap_is_denied(controls, monkeypatch):
    """The contrast: this harness sees a denial when a cap is there."""
    limits = protector_limits(controls, monkeypatch)
    capped = limits | {"max_notional_per_order": {str(AAA): paper_node.MAX_NOTIONAL_PER_ORDER_USD}}
    protector, _external, _released = run_large_holding(capped)
    assert protector.denied, "the harness did not see the cap"
    assert "NOTIONAL_EXCEEDS_MAX_PER_ORDER" in str(protector.denied[0].reason)
    assert not protector.fills


SUB_DOLLAR_SHARES = 500_000  # about $300,000 at $0.61, the case behind the finish line


class SubDollarData(SyntheticData):
    """The adapter's own instrument (`providers.parse_equity`, four decimals) and its own quote
    parser (`data.parse_quote`, which drops a quote finer than the instrument): every quote here is
    in four decimals, and each is taken. The bid steps down a tenth of a cent at a time."""

    async def _connect(self) -> None:
        self.instrument = parse_equity(Asset.model_validate(ASSET_AAA), 0)
        self._handle_instrument(self.instrument)
        self.publishing = False
        self.taken = 0
        self._handle_data(self.parsed(0.6123))

    def parsed(self, bid: float) -> QuoteTick:
        now = self.clock.timestamp_ns()
        raw = {
            **QUOTE_AAA,
            "bp": bid,
            "ap": round(bid + 0.0007, 4),
            "t": datetime.fromtimestamp(now / 1e9, UTC),
        }
        tick = parse_quote(Quote("AAA", raw), self.instrument, ts_init=now)
        self.taken += 1
        return tick

    async def publish_tape(self) -> None:
        bids = [0.6123] * 4 + [round(0.6113 - step * 0.0010, 4) for step in range(40)]
        for bid in bids:
            self._handle_data(self.parsed(bid))
            await asyncio.sleep(0.05)


def test_one_protective_sell_of_500k_sub_dollar_shares_is_on_the_step_and_passes(
    controls, monkeypatch
):
    """The lead's case (2026-11-13, 17:01): about 500,000 shares of a sub-dollar name quoted in
    four decimals. Its quotes are taken, not dropped as finer than the instrument; the level and the
    released limit sit on the instrument's step (Alpaca's 0.0001 under a dollar,
    `price_step.tick`); and the sale passes the protector node's risk engine and fills whole."""
    assert PRICE_PRECISION == 4 and PRICE_INCREMENT == Price.from_str("0.0001")
    protector, external, released = run_large_holding(
        protector_limits(controls, monkeypatch),
        shares=SUB_DOLLAR_SHARES,
        entry="0.6100",
        data=SubDollarData,
    )
    assert not protector.denied, [str(d.reason) for d in protector.denied]
    assert len(protector.submits) == 1
    sell = protector.submits[0]
    assert sell.quantity == Quantity.from_int(SUB_DOLLAR_SHARES)
    step = tick(sell.trigger_price.as_decimal())
    assert step == Decimal("0.0001")
    assert sell.trigger_price.precision == 4
    assert sell.trigger_price.as_decimal() % step == 0, "the level is off the instrument's step"
    assert sell.quantity.as_decimal() * sell.trigger_price.as_decimal() >= Decimal(290_000)
    limits = [order for order in released if order.order_type == OrderType.LIMIT]
    assert len(limits) == 1, released
    assert limits[0].price.precision == 4
    assert limits[0].price.as_decimal() % step == 0, "the released limit is off the step"
    assert len(protector.fills) == 1
    assert protector.fills[0].last_qty == Quantity.from_int(SUB_DOLLAR_SHARES)
    # Taken, not dropped: the level was planned on the four-decimal bid 0.6123 (OWN1's cold distance
    # 0.0123), and the sale filled at the four-decimal bid that elected it.
    assert sell.trigger_price == Price.from_str("0.6000")
    assert protector.fills[0].last_px == Price.from_str("0.5993")
    assert all(p.is_closed for p in external)
