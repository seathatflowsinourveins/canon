"""A paper test of historical trades through a real node: upstream's Data Testing Spec TC-D31
(request historical trades, https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md),
whose pass criteria are trades received through `on_historical_trades` with valid timestamps, prices,
sizes and trade IDs.

A small strategy, as `test_paper_sip_bars.py` has, requests AAPL's trades on each stock feed the paper pair
reads (`iex` and `sip`) from the latest session's open on Alpaca's calendar (pre-market, New York time) to
20 minutes ago, at most `LIMIT` of them: on a trading day, that day's session. Each request is
answered with no failure and at least one trade; every trade is AAPL's, in the window, in time order,
stamped `ts_init == ts_event`, priced at four decimals, sized in whole shares, and carries a trade ID.
Any hour: a GET of history needs no session.

It places nothing and opens no market-data stream (the client builds one only at a subscription), so it
does not take account A's stream lock. It runs only with a paper key pair in the environment, through
`mise run paper:test`, which holds the paper lock.
"""

import asyncio
import itertools
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
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

AAPL = InstrumentId.from_str("AAPL.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
ANSWER_SECS = 60
LIMIT = 1_000
"""At most this many trades per request: one page of Alpaca's, enough to check every field."""


class TradesHistory(Strategy):
    """Request AAPL's trades on `feed` from `trades_start` to `trades_end`, at most `LIMIT`, and keep
    the answer. Set after construction: the v2 `Strategy` takes no constructor arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.feed = ""
        self.trades_start: datetime | None = None
        self.trades_end: datetime | None = None
        self.trades: list = []
        self.answered = False
        self.failure: str | None = None

    def on_start(self) -> None:
        self.request_trades(
            AAPL,
            start=self.trades_start,
            end=self.trades_end,
            limit=LIMIT,
            params={"feed": self.feed},
        )

    def on_historical_trades(self, trades) -> None:
        self.trades.extend(trades)

    def on_data_response(self, response) -> None:
        self.failure = response.failure_reason
        self.answered = True


def latest_session_open(before: datetime) -> datetime:
    """The latest session open on Alpaca's calendar (`GET /v2/calendar`, `session_open`, pre-market) at or
    before `before`, within the week before it."""
    trading = with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )
    local = before.astimezone(NEW_YORK)
    days = trading.get_calendar(
        GetCalendarRequest(start=(local - timedelta(days=7)).date(), end=local.date())
    )
    assert isinstance(days, list)
    opens = [
        datetime.combine(
            day.date, datetime.strptime(day.session_open, "%H%M").time(), NEW_YORK
        ).astimezone(UTC)
        for day in days
        if day.session_open
    ]
    opens = [at for at in opens if at <= before]
    assert opens, f"no session open on Alpaca's calendar in the week to {before}"
    return max(opens)


def run_until_answered(strategy: TradesHistory) -> None:
    """Run a node with the adapter's data client until the request is answered or `ANSWER_SECS` pass."""
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-TRADES-HISTORY",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
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


@pytest.mark.parametrize("feed", ["iex", "sip"])
def test_paper_historical_trades_reach_the_strategy_with_valid_fields(feed) -> None:
    strategy = TradesHistory()
    end = datetime.now(UTC) - timedelta(minutes=20)
    start = latest_session_open(end)
    strategy.feed, strategy.trades_start, strategy.trades_end = feed, start, end

    run_until_answered(strategy)

    trades = strategy.trades
    print(
        f"PAPER-TRADES-HISTORY {feed} from={start:%Y-%m-%dT%H:%M}Z to={end:%Y-%m-%dT%H:%M}Z "
        f"trades={len(trades)} failure={strategy.failure!r} "
        f"first={trades[0].ts_event if trades else None} last={trades[-1].ts_event if trades else None}"
    )
    assert strategy.answered, f"the {feed} trades request was not answered in {ANSWER_SECS} s"
    assert strategy.failure is None, strategy.failure
    assert trades, f"Alpaca returned no {feed} AAPL trade from {start} to {end}"
    assert len(trades) <= LIMIT
    low, high = dt_to_unix_nanos(strategy.trades_start), dt_to_unix_nanos(end)
    assert all(t.instrument_id == AAPL for t in trades)
    assert all(low <= t.ts_event <= high for t in trades)
    assert all(a.ts_event <= b.ts_event for a, b in itertools.pairwise(trades))
    assert all(t.ts_init == t.ts_event for t in trades)
    assert all(t.price.precision == 4 and t.price > 0 for t in trades)
    assert all(t.size.precision == 0 and t.size > 0 for t in trades)
    assert all(str(t.trade_id) for t in trades)
