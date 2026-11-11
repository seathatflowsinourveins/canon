"""A paper test of bars on the consolidated tape (`sip`) whose prices are finer than the instrument's
four decimals, through a real node (ADR 0055, "Prices finer than the instrument's precision").

Shaped on the Data Testing Spec's TC-D50 (request bars,
https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md) with a small strategy, as
`test_paper_integration.py` is: SPY bars on `sip`, one-minute for the three days and daily for the year to
20 minutes ago. SPY is where the consolidated tape gave a bar high of 747.086488. Each request is
answered, every price keeps four decimals, and the bars the client rounded (half to even, as
`Instrument.make_price` rounds) are counted from its warning. Any hour.

It places nothing and opens no market-data stream (the client builds one only at a subscription), so it
does not take account A's stream lock and does not wait behind another stream on A's pair. It runs only with a
paper key pair in the environment, through `mise run paper:test`, which holds the paper lock.
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
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

SPY = InstrumentId.from_str("SPY.ALPACA")
ANSWER_SECS = 60


class SipBars(Strategy):
    """Request `bar_type` on `sip` for `days` to 20 minutes ago, and keep the answer. Both are set after
    construction: the v2 `Strategy` takes no constructor arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.bar_type: BarType | None = None
        self.days = 0
        self.bars: list = []
        self.answered = False
        self.failure: str | None = None

    def on_start(self) -> None:
        assert self.bar_type is not None
        end = datetime.now(UTC) - timedelta(minutes=20)
        self.request_bars(
            self.bar_type, start=end - timedelta(days=self.days), end=end, params={"feed": "sip"}
        )

    def on_historical_bars(self, bars) -> None:
        self.bars.extend(bars)

    def on_data_response(self, response) -> None:
        self.failure = response.failure_reason
        self.answered = True


def run_until_answered(strategy: SipBars) -> None:
    """Run a node with the adapter's data client until the request is answered or `ANSWER_SECS` pass."""
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-SIP-BARS",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(SPY)]),
                ),
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
    )
    node.add_strategy(strategy)
    handle = node.handle()

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        try:
            ends = loop.time() + ANSWER_SECS
            while not task.done() and not strategy.answered and loop.time() < ends:
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                handle.stop()
            await task

    asyncio.run(main())


@pytest.mark.parametrize(
    ("bar_type", "days"),
    [("SPY.ALPACA-1-MINUTE-LAST-EXTERNAL", 3), ("SPY.ALPACA-1-DAY-LAST-EXTERNAL", 365)],
)
def test_paper_sip_bars_finer_than_four_decimals_are_rounded_and_named(
    capfd, bar_type, days
) -> None:
    strategy = SipBars()
    strategy.bar_type, strategy.days = BarType.from_str(bar_type), days

    run_until_answered(strategy)

    logging_sync_to_disk()
    log = capfd.readouterr()
    lines = (log.out + log.err).splitlines()
    rounded = [line for line in lines if "Alpaca bars for SPY" in line and "Rounded " in line]
    print(f"PAPER-SIP-BARS {bar_type} bars={len(strategy.bars)} failure={strategy.failure!r}")
    for line in rounded:
        print(f"PAPER-SIP-BARS {line}")
    assert strategy.answered, f"the sip bars request was not answered in {ANSWER_SECS} s"
    assert strategy.failure is None, strategy.failure
    assert strategy.bars, f"Alpaca returned no sip {bar_type} bar in {days} days"
    assert all(
        price.precision == 4
        for bar in strategy.bars
        for price in (bar.open, bar.high, bar.low, bar.close)
    )
