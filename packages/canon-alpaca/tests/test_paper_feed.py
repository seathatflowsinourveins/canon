"""Paper tests of the data feed a subscription names in its params (ADR 0055), through a real node.

Shaped on the Data Testing Spec's TC-D20 (subscribe quotes), TC-D30 (subscribe trades) and TC-D71
(adapter-specific subscribe params, https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md).
Upstream's `DataTester` cannot be used for TC-D71 here: v2.0.0rc8's Python `DataTesterConfig` takes no
`subscribe_params` (`TypeError` when given one), so a small strategy subscribes instead.

* `test_paper_boats_quotes_reach_the_strategy`: `subscribe_quotes(AAPL, params={"feed": "boats"})` and
  `subscribe_trades` with the same params (TC-D30, subscribe trades); the node's stream is alpaca-py's on
  `v1beta1/boats`, and quotes and trades reach the strategy. It runs until both have arrived, at most
  `BOATS_WAIT_SECS`: boats trades are sparse overnight (at 23:21 New York on 2026-11-10, 24 AAPL quotes
  and one trade in a run of 54 s, the node's start-up included). With quotes and no trade in that time it
  skips, naming the counts: a quiet market, not a defect, once Alpaca has confirmed the trade
  subscription. It fails when no quote arrives, the subscription is not confirmed, or the boats stream logs a
  warning or an error (a reconnect, a refusal). Overnight only
  (8:00pm to 4:00am New York, from Alpaca's calendar): the probe of 2026-11-10 saw `boats` send nothing
  outside it. That the paper pair is served `boats` at all is an observation, not an entitlement (ADR 0055).
* `test_paper_default_feed_is_iex`: a subscription with no params; the stream is on `v2/iex`, as before.
  Any hour: it checks where the stream authenticated, not that IEX sent anything.
* `test_paper_sip_trade_prices_are_kept_at_four_decimals_and_named_when_rounded`:
  `subscribe_trades(AAPL, params={"feed": "sip"})` and quotes on the same feed, for `SIP_SECS`, while
  Alpaca's clock says the market is open. The consolidated tape carries prices finer than the
  instrument's four decimals; the client rounds a trade's as upstream rounds and names it, and drops a
  quote that fine, naming it (ADR 0055). It fails when no trade arrives or the client drops a trade; it
  reports how many prices were rounded, which a quiet minute may leave at none.

* `test_paper_wildcard_bars_and_statuses_reach_the_strategy_for_listed_symbols` (ADR 0090): minute bars
  and trading statuses for `SYMBOLS` with `params={"feed": "sip", "wildcard": True}`. Alpaca must confirm
  `"*"` on both channels; minute bars reach the strategy for listed symbols only, each the stream's one
  bar type. It runs `WILDCARD_SECS`, two minute marks at least, while sip is in session (between the
  day's session open and close on Alpaca's calendar). It fails when Alpaca does not confirm `"*"`, no bar
  arrives, or a bar arrives for a symbol not listed; it reports the bars per minute, the symbols seen and
  each status received, which a calm market may leave at none.

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
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock
from alpaca.trading.requests import GetCalendarRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import ALPACA_STREAM_LOGGER
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.providers import with_http_timeout
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

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
        reason=(
            f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
            "to run this integration test"
        ),
    ),
    # each test opens a market-data stream, so it holds account A's stream lock (conftest.py, ADR 0069)
    pytest.mark.alpaca_stream,
]

AAPL = InstrumentId.from_str("AAPL.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
STREAM_HOST = "wss://stream.data.alpaca.markets"
START_TIMEOUT_SECS = 60
BOATS_WAIT_SECS = 120
SIP_SECS = 60
"""How long the sip test runs: in the regular session sip sends AAPL trades many times a second."""
"""How long the boats test waits for both a quote and a trade."""


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
    """Subscribe to AAPL quotes with the given params, and trades too with `with_trades`, and keep what
    arrives (TC-D20's and TC-D30's shape)."""

    def __init__(self) -> None:
        super().__init__()
        self.params: dict | None = None
        self.with_trades = False
        self.quotes: list = []
        self.trades: list = []
        self.subscribed_at: float | None = None
        self.first_trade_at: float | None = None

    def on_start(self) -> None:
        self.subscribed_at = time.monotonic()
        self.subscribe_quotes(AAPL, params=self.params)
        if self.with_trades:
            self.subscribe_trades(AAPL, params=self.params)

    def on_quote(self, quote) -> None:
        self.quotes.append(quote)

    def on_trade(self, trade) -> None:
        if self.first_trade_at is None:
            self.first_trade_at = time.monotonic()
        self.trades.append(trade)


class StreamLog(logging.Handler):
    """alpaca-py's stream log lines, as `logging` records them, each with the thread that logged it."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.threadName or "", record.levelno, record.getMessage()))

    @property
    def lines(self) -> list[str]:
        return [message for _, _, message in self.records]

    def of_thread_that_logged(self, line: str) -> list[tuple[int, str]]:
        """Every line of the thread that logged `line`: one stream's own, since the data client runs each
        stream on a thread named for it alone, and a stream another test left running keeps its own."""
        threads = {thread for thread, _, message in self.records if message == line}
        return [(level, message) for thread, level, message in self.records if thread in threads]


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


def run_for(node: LiveNode, seconds: float, until: Callable[[], bool] | None = None) -> None:
    """Run the node for `seconds` once it is running, or until `until` holds, then stop it through its
    handle and let it finish, as `test_paper_matrix.run_for` does."""
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
            while not task.done() and loop.time() < ends and not (until and until()):
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
    strategy.with_trades = True
    node = paper_node(strategy)

    run_for(node, BOATS_WAIT_SECS, until=lambda: bool(strategy.quotes and strategy.trades))
    stopped_at = time.monotonic()

    subscribed = strategy.subscribed_at
    waited = stopped_at - subscribed if subscribed is not None else 0.0
    first = strategy.first_trade_at
    first_trade = f"{first - subscribed:.0f} s" if first is not None and subscribed else "none"
    crossed = sum(q.bid_price > q.ask_price for q in strategy.quotes)
    print(
        f"PAPER-FEED boats quotes={len(strategy.quotes)} crossed={crossed} "
        f"trades={len(strategy.trades)} first trade after {first_trade}, {waited:.0f} s subscribed"
    )
    connected = f"connected to {STREAM_HOST}/v1beta1/boats"
    assert connected in stream_log.lines, stream_log.lines
    # This stream's own lines: a warning is a reconnect ("data websocket error, restarting connection"),
    # an error a refusal or a failed run (alpaca-py 0.45.0, `alpaca/data/live/websocket.py`).
    boats = stream_log.of_thread_that_logged(connected)
    troubles = [message for level, message in boats if level >= logging.WARNING]
    assert strategy.quotes, (
        f"no AAPL quote from boats in {BOATS_WAIT_SECS} s of the overnight session"
    )
    assert all(q.instrument_id == AAPL for q in strategy.quotes)
    assert all(q.bid_price.as_double() > 0 and q.ask_price.as_double() > 0 for q in strategy.quotes)
    assert not troubles, troubles
    if not strategy.trades:
        # A quiet market, not a defect, but only once Alpaca has confirmed the trade subscription: alpaca-py
        # logs each confirmation, "subscribed to trades: ['AAPL'], quotes: ['AAPL']" or the like.
        confirmed = any(m.startswith("subscribed to") and "trades: ['AAPL']" in m for _, m in boats)
        assert confirmed, ("Alpaca never confirmed the boats trade subscription", boats)
        pytest.skip(
            f"no boats trade for AAPL in {waited:.0f} s subscribed; "
            f"{len(strategy.quotes)} quotes arrived"
        )
    assert all(t.instrument_id == AAPL for t in strategy.trades)
    assert all(t.price.as_double() > 0 and t.size.as_double() > 0 for t in strategy.trades)


def test_paper_default_feed_is_iex(stream_log) -> None:
    strategy = Quotes()
    node = paper_node(strategy)

    run_for(node, 10)

    assert f"connected to {STREAM_HOST}/v2/iex" in stream_log.lines, stream_log.lines
    assert not any("v1beta1" in line for line in stream_log.lines)
    print(f"PAPER-FEED iex quotes={len(strategy.quotes)}")


def test_paper_sip_trade_prices_are_kept_at_four_decimals_and_named_when_rounded(
    stream_log, capfd
) -> None:
    trading = with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )
    clock = trading.get_clock()
    assert isinstance(clock, Clock), f"Alpaca answered {clock!r} for its clock"
    if not clock.is_open:
        pytest.skip("the regular session is closed; this test reads sip while it is open")
    strategy = Quotes()
    strategy.params = {"feed": "sip"}
    strategy.with_trades = True
    node = paper_node(strategy)

    run_for(node, SIP_SECS)

    assert f"connected to {STREAM_HOST}/v2/sip" in stream_log.lines, stream_log.lines
    logging_sync_to_disk()
    log = capfd.readouterr()
    lines = (log.out + log.err).splitlines()
    dropped = [line for line in lines if "Dropped Alpaca trade" in line]
    rounded = [line for line in lines if "Alpaca stream on feed sip: rounded AAPL" in line]
    fine = [line for line in lines if "Alpaca stream on feed sip: dropped AAPL" in line]
    print(
        f"PAPER-FEED sip trades={len(strategy.trades)} quotes={len(strategy.quotes)} "
        f"rounded_lines={len(rounded)} fine_quote_lines={len(fine)}"
    )
    for line in rounded + fine:
        print(f"PAPER-FEED {line}")
    # the node's own log was captured, so no line above means no such price, not a missed log
    assert any("Subscribed AAPL.ALPACA trades" in line for line in lines), lines[-20:]
    assert strategy.trades, f"no AAPL trade from sip in {SIP_SECS} s of the regular session"
    assert not dropped, dropped
    assert all(t.price.precision == 4 for t in strategy.trades)


SYMBOLS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "AMZN")
WILDCARD_SECS = 150
"""How long the test runs: two minute marks at least, so each listed symbol has a chance to trade."""
PARAMS = {"feed": "sip", "wildcard": True}


class Market(Strategy):
    """Subscribe minute bars and statuses for `SYMBOLS` by wildcard, and record what arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.bars: list = []
        self.statuses: list = []

    def on_start(self) -> None:
        ids = [InstrumentId.from_str(f"{s}.ALPACA") for s in SYMBOLS]
        bar_types = [BarType.from_str(f"{i}-1-MINUTE-LAST-EXTERNAL") for i in ids]
        self.subscribe_bars(bar_types, params=PARAMS)
        self.subscribe_instrument_status(ids, params=PARAMS)

    def on_bar(self, bar) -> None:
        self.bars.append(bar)

    def on_instrument_status(self, status) -> None:
        self.statuses.append(status)


def sip_in_session() -> bool:
    """Whether sip is in session now, between the day's `session_open` and `session_close` on Alpaca's
    calendar (pre-market open to after-hours close, New York time)."""
    trading = with_http_timeout(
        TradingClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], paper=True)
    )
    now = datetime.now(UTC).astimezone(NEW_YORK)
    days = trading.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    assert isinstance(days, list)
    if not days or not (days[0].session_open and days[0].session_close):
        return False
    return days[0].session_open <= now.strftime("%H%M") < days[0].session_close


def test_paper_wildcard_bars_and_statuses_reach_the_strategy_for_listed_symbols(stream_log) -> None:
    if not sip_in_session():
        pytest.skip("sip is not in session (Alpaca's calendar: no session open now)")
    strategy = Market()
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-WILDCARD",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=2,
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(
                        load_ids=[f"{s}.ALPACA" for s in SYMBOLS]
                    ),
                ),
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
    )
    node.add_strategy(strategy)
    started = datetime.now(UTC)

    run_for(node, WILDCARD_SECS)

    assert f"connected to {STREAM_HOST}/v2/sip" in stream_log.lines, stream_log.lines
    confirmed = [line for line in stream_log.lines if line.startswith("subscribed to")]
    print(f"PAPER-WILDCARD confirmed={confirmed}")
    assert any("bars: ['*']" in line and "statuses: ['*']" in line for line in confirmed), confirmed
    per_minute = Counter(
        datetime.fromtimestamp(b.ts_event / 1e9, UTC).strftime("%H:%M") for b in strategy.bars
    )
    seen = sorted({b.bar_type.instrument_id.symbol.value for b in strategy.bars})
    print(
        f"PAPER-WILDCARD from={started:%H:%M:%S}Z secs={WILDCARD_SECS} bars={len(strategy.bars)} "
        f"per_minute={dict(sorted(per_minute.items()))} symbols_seen={seen} "
        f"statuses={len(strategy.statuses)}"
    )
    for status in strategy.statuses:
        print(f"PAPER-WILDCARD status {status}")
    assert strategy.bars, f"no minute bar for {SYMBOLS} in {WILDCARD_SECS} s of sip's session"
    assert set(seen) <= set(SYMBOLS), seen
    assert all(str(b.bar_type).endswith("-1-MINUTE-LAST-EXTERNAL") for b in strategy.bars)
    assert all(b.ts_init > b.ts_event for b in strategy.bars)  # stamped at its open, arrived after
    assert datetime.now(UTC) - started < timedelta(seconds=WILDCARD_SECS + 120)
