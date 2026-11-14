"""A paper read of one latest quote through the adapter's `latest` request (ADR 0066's amendment of
2026-11-13), on the feed of the stream the client has established.

Shaped as `test_paper_sip_bars.py` is, on the Data Testing Spec's request cases
(https://nautilustrader.io/docs/md/latest/developer_guide/spec_data_testing.md), with a small strategy on a
real node: it subscribes to SPY's quotes on `sip`, which builds the client's stream on that feed, and two
seconds later, whether or not a stream quote has arrived, sends one
`request_quotes(SPY, limit=1, params={"feed": "sip", "latest": True, "skip_catalog_data": True,
"update_catalog": False})`. The request is answered once, with no failure, and with at most one quote,
which keeps four decimals and is stamped on receipt (`ts_init` no earlier than the request was sent). Any
hour: outside a session Alpaca still answers the last quote it has, and the test does not wait for a
stream quote.

What it calls: the asset lookup goes to Alpaca's paper Trading API and the latest quote to the Market Data
API's `/v2/stocks/quotes/latest`, both GET, through the adapter; the subscription opens a market-data
stream on account A's pair, so the test is marked `alpaca_stream` and holds A's stream lock. It places,
modifies and cancels nothing. It runs only with a paper key pair in the environment, through
`mise run paper:test -- -s -k latest_quote_seed`, which holds the paper lock.
"""

import asyncio
import os
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
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import TraderId
from nautilus_trader.trading import Strategy

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
        reason=(
            f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
            "to run this paper test"
        ),
    ),
    pytest.mark.alpaca_stream,
]

SPY = InstrumentId.from_str("SPY.ALPACA")
FEED = "sip"
SEED_PARAMS = {"feed": FEED, "latest": True, "skip_catalog_data": True, "update_catalog": False}
REQUEST_AFTER = timedelta(seconds=2)
ANSWER_SECS = 60


class LatestQuoteSeed(Strategy):
    """Subscribe to SPY's quotes on `FEED`, then send one latest request on a timer and keep its answer."""

    def __init__(self) -> None:
        super().__init__()
        self.request_id: str | None = None
        self.sent_at = 0
        self.quotes: list = []
        self.responses: list = []
        self.callbacks: list = []
        self.stream_quotes_before_request = 0

    def on_start(self) -> None:
        self.subscribe_quotes(SPY, params={"feed": FEED})
        self.clock.set_time_alert(
            "latest-quote-seed", self.clock.utc_now() + REQUEST_AFTER, callback=self.send_request
        )

    def on_quote(self, quote) -> None:
        if self.request_id is None:
            self.stream_quotes_before_request += 1

    def send_request(self, event) -> None:
        self.sent_at = self.clock.timestamp_ns()
        self.request_id = self.request_quotes(
            SPY, limit=1, params=dict(SEED_PARAMS), callback=self.completed
        )

    def on_historical_quotes(self, quotes) -> None:
        self.quotes.extend(quotes)

    def on_data_response(self, response) -> None:
        self.responses.append(response)

    def completed(self, request_id) -> None:
        self.callbacks.append(request_id)
        self.shutdown_system("latest quote seed answered")


def run_until_answered(strategy: LatestQuoteSeed) -> None:
    """Run a node with the adapter's data client until the request is answered or `ANSWER_SECS` pass."""
    node = LiveNode.build(
        "CANON-ALPACA-PAPER-LATEST-QUOTE-SEED",
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
            while not task.done() and not strategy.callbacks and loop.time() < ends:
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                handle.stop()
            await task

    asyncio.run(main())


def test_paper_latest_quote_seed_answers_one_snapshot_on_the_stream_feed() -> None:
    strategy = LatestQuoteSeed()

    run_until_answered(strategy)

    failures = [response.failure_reason for response in strategy.responses]
    print(
        f"PAPER-LATEST-QUOTE-SEED feed={FEED} responses={len(strategy.responses)} "
        f"failures={failures!r} quotes={len(strategy.quotes)} "
        f"stream_quotes_before_request={strategy.stream_quotes_before_request}"
    )
    for quote in strategy.quotes:
        print(
            f"PAPER-LATEST-QUOTE-SEED bid={quote.bid_price} ask={quote.ask_price} "
            f"ts_event={quote.ts_event} ts_init={quote.ts_init} sent_at={strategy.sent_at}"
        )
    assert strategy.request_id is not None, "the latest quote request was never sent"
    assert strategy.callbacks == [strategy.request_id], (
        f"the latest quote request was not answered once in {ANSWER_SECS} s"
    )
    (response,) = strategy.responses
    assert str(response.correlation_id) == strategy.request_id
    assert response.failure_reason is None, response.failure_reason
    assert len(strategy.quotes) <= 1
    for quote in strategy.quotes:
        assert quote.instrument_id == SPY
        assert quote.bid_price.precision == quote.ask_price.precision == 4
        assert quote.ts_init >= strategy.sent_at
