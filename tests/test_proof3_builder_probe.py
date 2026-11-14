"""Probe (untracked, L1): proof 3 through protector_node's own builder. The builder's Alpaca client
factories are replaced by the synthetic in-memory clients (no transport, no credential); everything
protector_node builds itself (the strategy's config) is kept. A reconciled EXTERNAL long arrives
before on_start, as on the live node. Neutral instrument, synthetic quotes only."""

from __future__ import annotations

import asyncio
import gc
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import ExecutionClientConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import OmsType
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Quantity
from nautilus_trader.model import Venue

from canon_trading import owner_protector
from canon_trading import paper_node
from canon_trading import protector_node
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import SyntheticData
from tests.test_owner_protector import SyntheticExecution
from tests.test_owner_protector import quote


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """This synthetic node needs no credential-environment reader or real stream lock."""


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


@pytest.mark.usefixtures("new_york_regular_hours")
def test_a_segment_through_the_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gc.collect()
    said: list[str] = []
    monkeypatch.setattr(
        owner_protector.OwnerProtector, "_say", lambda self, text: said.append(text)
    )

    class Data(SyntheticData):
        async def publish_tape(self) -> None:
            for bid in ("10.00", "10.10", "10.20", "10.30"):
                self._handle_data(quote(bid, self.clock.timestamp_ns()))
                await asyncio.sleep(0.3)

    class Execution(SyntheticExecution):
        async def _generate_position_status_reports(self, command):
            now = self.clock.timestamp_ns()
            return [
                PositionStatusReport(
                    AccountId("ALPACA-001"),
                    AAA,
                    PositionSide.LONG,
                    Quantity.from_int(10),
                    now - 3600 * SECOND,
                    now,
                    avg_px_open=Decimal("10.00"),
                )
            ]

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Execution(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )

    def builder(name, instruments, *, trader_id, **_kwargs):
        def build():
            return LiveNode.build(
                name,
                LiveNodeConfig(
                    trader_id=trader_id,
                    timeout_connection_secs=2,
                    timeout_reconciliation_secs=2,
                    timeout_portfolio_secs=2,
                    timeout_disconnection_secs=1,
                    delay_post_stop_secs=0,
                    data_clients={"ALPACA": DataClientConfig()},
                    exec_clients={"ALPACA": ExecutionClientConfig()},
                ),
                data_factories={"ALPACA": DataFactory},
                exec_factories={"ALPACA": ExecutionFactory},
            )

        return SimpleNamespace(build=build)

    monkeypatch.setattr(paper_node, "alpaca_paper_builder", builder)
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    run = protector_node.ProtectorRun(account="tests", row="OWN1", holds_dir=holds, inbox=inbox)
    node, strategy = protector_node.protector_node(run, (AAA,), feed="sip")

    async def bounded():
        await asyncio.sleep(3)
        strategy.shutdown_system("end of probe segment")

    async def go():
        stop = asyncio.create_task(bounded())
        try:
            async with asyncio.timeout(8):
                await node.run_async()
        finally:
            stop.cancel()

    try:
        asyncio.run(go())
    finally:
        node.dispose()
    print("settings:", strategy.settings.account_label, strategy.settings.level_book_path)
    print("said:", [n for n in said][:8])
    books = list(tmp_path.rglob("*.json"))
    print("books:", books, [json.loads(b.read_text()) for b in books][:1])
