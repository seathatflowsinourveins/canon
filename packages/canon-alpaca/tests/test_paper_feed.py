"""Paper tests of the data feed a subscription names in its params (ADR 0055), through a real node.

Shaped on the Data Testing Spec's TC-D20 (subscribe quotes) and TC-D71 (adapter-specific subscribe params,
https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md). Upstream's `DataTester`
cannot be used for TC-D71 here: v2.0.0rc8's Python `DataTesterConfig` takes no `subscribe_params`
(`TypeError` when given one), so a small strategy subscribes instead.

* `test_paper_boats_quotes_reach_the_strategy`: `subscribe_quotes(AAPL, params={"feed": "boats"})`; the
  node's stream is alpaca-py's on `v1beta1/boats` and quotes reach the strategy. Overnight only (8:00pm
  to 4:00am New York, from Alpaca's calendar): the probe of 2026-11-10 saw `boats` send nothing outside
  it. That the paper pair is served `boats` at all is an observation, not an entitlement (ADR 0055).
* `test_paper_default_feed_is_iex`: a subscription with no params; the stream is on `v2/iex`, as before.
  Any hour: it checks where the stream authenticated, not that IEX sent anything.

No paper test shows a feed refused: on 2026-11-10 at 20:46 New York the paper pair authenticated on
`v2/sip` too, although "Paper Trading" (https://docs.alpaca.markets/us/docs/paper-trading.md) entitles a
Paper Only account to IEX alone (ADR 0055). A refusal is tested offline only.

Each places nothing and runs only with a paper key pair in the environment, through `mise run paper:test`,
which holds the paper lock. The stream's endpoint is read from alpaca-py's own log lines ("connected to
<url>", logged only after authenticating; `alpaca/data/live/websocket.py`).
"""

import asyncio
import logging
import os
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import ALPACA_STREAM_LOGGER
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
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
NEW_YORK = ZoneInfo("America/New_York")
STREAM_HOST = "wss://stream.data.alpaca.markets"
START_TIMEOUT_SECS = 60


def overnight_now() -> bool:
    """Whether now is in the overnight session: the 8 hours before a trading date's `session_open`, from
    Alpaca's calendar (`GET /v2/calendar`; 8:00pm-4:00am ET, following the NYSE holiday calendar, Alpaca's
    24/5 page), as `test_paper_execution.alpaca_session` reads it."""
    trading = with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )
    now = datetime.now(UTC).astimezone(NEW_YORK)
    days = trading.get_calendar(
        GetCalendarRequest(start=now.date(), end=(now + timedelta(days=5)).date())
    )
    assert isinstance(days, list)

    def session_open(day: date, hhmm: str | None, opens: datetime) -> datetime:
        if not hhmm:
            return opens.replace(tzinfo=NEW_YORK) if opens.tzinfo is None else opens
        return datetime.combine(day, dt_time(int(hhmm[:2]), int(hhmm[2:])), NEW_YORK)

    for day in days:
        starts = session_open(day.date, day.session_open, day.open)
        if starts - timedelta(hours=8) <= now < starts:
            return True
    return False


class Quotes(Strategy):
    """Subscribe to AAPL quotes with the given params and keep what arrives (TC-D20's shape)."""

    def __init__(self) -> None:
        super().__init__()
        self.params: dict | None = None
        self.quotes: list = []

    def on_start(self) -> None:
        self.subscribe_quotes(AAPL, params=self.params)

    def on_quote(self, quote) -> None:
        self.quotes.append(quote)


class StreamLog(logging.Handler):
    """alpaca-py's stream log lines, as `logging` records them."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def stream_log():
    logger = logging.getLogger(ALPACA_STREAM_LOGGER)
    handler, level = StreamLog(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def paper_node(strategy: Strategy) -> LiveNode:
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-FEED",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=2,  # lets the unsubscribe on stop reach the client
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
    )
    node.add_strategy(strategy)
    return node


def run_for(node: LiveNode, seconds: float) -> None:
    """Run the node for `seconds` once it is running, then stop it through its handle and let it finish,
    as `test_paper_matrix.run_for` does."""
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


def test_paper_boats_quotes_reach_the_strategy(stream_log) -> None:
    if not overnight_now():
        pytest.skip("boats sent nothing outside the overnight session on paper; this test needs it")
    strategy = Quotes()
    strategy.params = {"feed": "boats"}
    node = paper_node(strategy)

    run_for(node, 30)

    assert f"connected to {STREAM_HOST}/v1beta1/boats" in stream_log.lines, stream_log.lines
    assert strategy.quotes, "no AAPL quote from boats in 30 s of the overnight session"
    assert all(q.instrument_id == AAPL for q in strategy.quotes)
    assert all(q.bid_price.as_double() > 0 and q.ask_price.as_double() > 0 for q in strategy.quotes)
    crossed = sum(q.bid_price > q.ask_price for q in strategy.quotes)
    print(f"PAPER-FEED boats quotes={len(strategy.quotes)} crossed={crossed}")


def test_paper_default_feed_is_iex(stream_log) -> None:
    strategy = Quotes()
    node = paper_node(strategy)

    run_for(node, 10)

    assert f"connected to {STREAM_HOST}/v2/iex" in stream_log.lines, stream_log.lines
    assert not any("v1beta1" in line for line in stream_log.lines)
    print(f"PAPER-FEED iex quotes={len(strategy.quotes)}")
