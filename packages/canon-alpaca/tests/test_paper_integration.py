"""Integration test against Alpaca's paper environment: one asset and its daily bars, through a real node.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`
(`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`: the names Alpaca's CLI and MCP server read; alpaca-py 0.45.0 reads
none). Otherwise pytest reports it as skipped with that reason. Never write the keys to a file.

What it calls: the asset lookup goes to Alpaca's paper Trading API through `TradingClient(paper=True)`, and
the bars to Alpaca's Market Data API, which paper accounts share ("Authentication",
https://docs.alpaca.markets/us/docs/authentication.md). It places no order. The bars are historical, so the
result does not depend on market hours. The live quote and trade stream is not exercised here.
"""

import asyncio
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
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
DAILY = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
DAY_NS = 86_400_000_000_000


class DailyBars(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self.bars: list = []
        self.responses: list = []
        self.request_id: str | None = None

    def on_start(self) -> None:
        end = datetime.now(UTC) - timedelta(days=1)
        self.request_id = self.request_bars(DAILY, start=end - timedelta(days=14), end=end)

    def on_historical_bars(self, bars) -> None:
        self.bars.extend(bars)

    def on_data_response(self, response) -> None:
        self.responses.append((str(response.correlation_id), response.failure_reason))
        self.shutdown_system("Paper bars received")


async def run_hosted(node) -> None:
    async with asyncio.timeout(60):
        await node.run_async()


def test_paper_asset_and_daily_bars_reach_the_strategy() -> None:
    node = LiveNode.build(
        "CANON-ALPACA-PAPER",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
    )
    strategy = DailyBars()
    node.add_strategy(strategy)

    asyncio.run(run_hosted(node))

    instrument = node.cache.instrument(AAPL)
    assert isinstance(instrument, Equity)
    assert instrument.info["exchange"] == "NASDAQ"
    assert strategy.responses == [(strategy.request_id, None)]
    assert strategy.bars, "14 calendar days of daily bars should hold at least one trading day"
    ts_events = [bar.ts_event for bar in strategy.bars]
    assert ts_events == sorted(ts_events)
    assert all(bar.ts_init - bar.ts_event == DAY_NS for bar in strategy.bars)
    assert all(
        bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high
        for bar in strategy.bars
    )
