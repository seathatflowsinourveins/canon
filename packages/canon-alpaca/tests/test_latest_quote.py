"""Frozen latest-quote contract on the native node, with offline Alpaca transports.

Contracts: NautilusTrader d53849b216e2 request_quotes, QuotesResponse and request_failed;
alpaca-py c42add7c0a70 StockLatestQuoteRequest and get_stock_latest_quote. The HTTP retry
tests retain the real SDK retry loop and its sleeps. All catalogs use pytest's tmp_path;
no test reads credentials, opens a socket, or writes paper state, catalog/ or ledger/.
"""

import asyncio
import json
import threading
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import canon_alpaca.data
import pytest
import requests
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from canon_alpaca.data import AlpacaDataClient
from canon_alpaca.data import parse_quote
from canon_alpaca.providers import AlpacaInstrumentProvider
from canon_alpaca.providers import parse_equity
from nautilus_trader.common import Environment
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import TraderId
from nautilus_trader.persistence import DataCatalogConfig
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.trading import Strategy

AAPL = InstrumentId.from_str("AAPL.ALPACA")
MSFT = InstrumentId.from_str("MSFT.ALPACA")
SEED_PARAMS = {
    "feed": "sip",
    "latest": True,
    "skip_catalog_data": True,
    "update_catalog": False,
}


@pytest.fixture
def venue(alpaca, monkeypatch):
    """Inject clients; replace only stream construction after the real offline connect.

    The original probe's constructor-injected stream has no known feed. Giving the
    connected client a fake stream factory lets its real _started_stream establish
    the feed on subscription, without an environment lookup or a real transport.
    """
    state = SimpleNamespace(
        historical=alpaca.HistoricalClient(),
        stream=alpaca.Stream(),
        latest_result={"AAPL": alpaca.quote()},
        latest_error=None,
        latest_calls=[],
        latest_threads=[],
        delay=0,
        entered=threading.Event(),
        ended=threading.Event(),
        requests=[],
        responses=[],
        order=[],
    )

    def refuse(*args, **kwargs):
        raise AssertionError("latest-quote tests allow no network or credential lookup")

    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(canon_alpaca.data, "credentials_from_env", refuse)

    def latest(params):
        state.latest_calls.append(params)
        state.latest_threads.append(threading.get_ident())
        state.entered.set()
        try:
            if state.delay:
                time.sleep(state.delay)
            if state.latest_error is not None:
                raise state.latest_error
            return state.latest_result
        finally:
            state.ended.set()

    state.historical.get_stock_latest_quote = latest
    connect = AlpacaDataClient._connect
    request_quotes = AlpacaDataClient._request_quotes
    handle_response = AlpacaDataClient._handle_response
    started_stream = AlpacaDataClient._started_stream

    async def connected(client):
        await connect(client)
        client._stream = None
        client._new_stream = lambda **kwargs: state.stream

    async def requested(client, request):
        state.requests.append(request)
        state.order.append(("request", client._stream_feed))
        await request_quotes(client, request)

    def responded(client, response):
        state.responses.append(response)
        handle_response(client, response)

    def started(client, feed):
        result = started_stream(client, feed)
        state.order.append(("started", client._stream_feed))
        return result

    monkeypatch.setattr(AlpacaDataClient, "_connect", connected)
    monkeypatch.setattr(AlpacaDataClient, "_request_quotes", requested)
    monkeypatch.setattr(AlpacaDataClient, "_handle_response", responded)
    monkeypatch.setattr(AlpacaDataClient, "_started_stream", started)
    yield state
    state.historical._session.close()


@pytest.fixture
def build_node(alpaca, venue):
    """Retain the native-node probe fixture, with injected offline clients only."""
    nodes = []

    class OfflineFactory(DataClientFactory):
        @staticmethod
        def create(*, name, config, cache, clock):
            return AlpacaDataClient(
                name=name,
                config=config,
                cache=cache,
                clock=clock,
                instrument_provider=AlpacaInstrumentProvider(
                    config.instrument_provider, assets_client=alpaca.TradingClient()
                ),
                historical_client=venue.historical,
                stream=venue.stream,
            )

    def build(strategy, catalog=None):
        node = LiveNode.build(
            "SEED-CONTRACT",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TraderId("TESTER-001"),
                timeout_connection_secs=2,
                timeout_disconnection_secs=5,
                delay_post_stop_secs=0,
                data_clients={
                    "ALPACA": DataClientConfig(
                        instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)])
                    )
                },
                catalogs=[] if catalog is None else [DataCatalogConfig(path=str(catalog))],
            ),
            data_factories={"ALPACA": OfflineFactory},
        )
        nodes.append(node)
        node.add_strategy(strategy)
        return node

    yield build
    for node in nodes:
        node.dispose()


def run(node):
    async def main():
        async with asyncio.timeout(25):
            await node.run_async()

    asyncio.run(main())


class SeedRequester(Strategy):
    """Read ticks, terminal envelope and ID callback on their native delivery surfaces."""

    def __init__(self) -> None:
        super().__init__()
        self.request_id: str | None = None
        self.params = dict(SEED_PARAMS)
        self.limit: int | None = 1
        self.instrument_ids: InstrumentId | list[InstrumentId] = AAPL
        self.stream_feed: str | None = "sip"
        self.immediate = False
        self.request_start: datetime | None = None
        self.request_end: datetime | None = None
        self.callbacks: list = []
        self.responses: list = []
        self.quotes: list = []
        self.events: list[str] = []
        self.sent_at = 0
        self.completed_at = 0
        self.cached = None
        self.busy = None
        self.timer_during_call: list[int] = []

    def on_start(self) -> None:
        if self.busy is not None:
            self.clock.set_timer(
                "seed-loop-probe", timedelta(milliseconds=100), callback=self.on_timer
            )
        if self.stream_feed is not None:
            self.subscribe_quotes(AAPL, params={"feed": self.stream_feed})
        if self.immediate or self.stream_feed is None:
            self.send_request()

    def on_quote(self, quote) -> None:
        if self.request_id is None:
            self.send_request()

    def send_request(self) -> None:
        self.sent_at = self.clock.timestamp_ns()
        self.request_id = self.request_quotes(
            self.instrument_ids,
            start=self.request_start,
            end=self.request_end,
            limit=self.limit,
            params=self.params,
            callback=self.completed,
        )

    def on_timer(self, event) -> None:
        assert self.busy is not None
        if self.busy.entered.is_set() and not self.busy.ended.is_set():
            self.timer_during_call.append(self.clock.timestamp_ns())

    def on_historical_quotes(self, quotes) -> None:
        self.events.append("quotes")
        self.quotes.extend(quotes)

    def on_data_response(self, response) -> None:
        self.events.append("response")
        self.responses.append(response)

    def completed(self, request_id) -> None:
        self.events.append("callback")
        self.callbacks.append(request_id)
        self.completed_at = self.clock.timestamp_ns()
        self.cached = self.cache.quote(AAPL)
        self.shutdown_system("Seed contract probe complete")


def assert_completed(strategy, *, failure=None):
    (response,) = strategy.responses
    assert str(response.correlation_id) == strategy.request_id
    assert strategy.callbacks == [strategy.request_id]
    assert isinstance(strategy.callbacks[0], str)
    if failure is None:
        assert response.failure_reason is None
        assert strategy.events == ["quotes", "response", "callback"]
    else:
        assert response.failure_reason is not None
        assert "AAPL: latest quote refused:" in response.failure_reason
        assert failure in response.failure_reason
        assert response.data is None
        assert strategy.quotes == []
        assert strategy.events == ["response", "callback"]


@pytest.mark.parametrize("feed", ["sip", "delayed_sip", "boats", "overnight"])
def test_a1_a2_a4_one_sdk_call_on_the_stream_feed_stamps_receipt(build_node, venue, feed):
    strategy = SeedRequester()
    strategy.stream_feed = strategy.params["feed"] = feed

    run(build_node(strategy))

    assert_completed(strategy)
    (sent,) = venue.latest_calls
    assert isinstance(sent, StockLatestQuoteRequest)
    assert sent.symbol_or_symbols == "AAPL"
    assert sent.feed == DataFeed(feed)
    assert sent.to_request_fields() == {"symbols": "AAPL", "feed": feed}
    assert venue.historical.requests == []
    (tick,) = strategy.quotes
    assert tick.ts_event == dt_to_unix_nanos(venue.latest_result["AAPL"].timestamp)
    assert strategy.sent_at <= tick.ts_init <= strategy.completed_at
    assert tick.ts_init > tick.ts_event
    assert strategy.cached == tick
    assert len(venue.latest_threads) == 1
    assert venue.latest_threads[0] != threading.get_ident()


@pytest.mark.parametrize(
    ("feed", "stream_feed", "reason"),
    [
        ("iex", "iex", "IEX"),
        ("boats", "sip", "stream feed"),
        ("sip", None, "no stream feed yet"),
        ("otc", "sip", "otc"),
        ("unknown", "sip", "unknown"),
        (None, "sip", "feed"),
        (7, "sip", "feed"),
        ("missing", "sip", "feed"),
    ],
)
def test_a2_a5_feed_refusal_is_named_and_sends_nothing(
    build_node, venue, feed, stream_feed, reason
):
    strategy = SeedRequester()
    strategy.stream_feed = stream_feed
    strategy.params["feed"] = feed
    if feed == "missing":
        del strategy.params["feed"]

    run(build_node(strategy))

    assert_completed(strategy, failure=reason)
    assert venue.latest_calls == venue.historical.requests == []


@pytest.mark.parametrize(
    ("option", "value", "reason"),
    [
        ("limit", 2, "limit"),
        ("skip_catalog_data", False, "skip_catalog_data"),
        ("skip_catalog_data", None, "skip_catalog_data"),
        ("skip_catalog_data", 1, "skip_catalog_data"),
        ("update_catalog", True, "update_catalog"),
        ("update_catalog", None, "update_catalog"),
        ("update_catalog", 0, "update_catalog"),
        ("instrument_ids", [AAPL, MSFT], "one instrument"),
    ],
)
def test_a3_a5_invalid_shape_completes_without_transport(build_node, venue, option, value, reason):
    strategy = SeedRequester()
    if option in ("limit", "instrument_ids"):
        setattr(strategy, option, value)
        if option == "instrument_ids":
            # engine/mod.rs:1581-1584 rejects a multi-instrument request with a limit
            # before dispatch; None reaches the adapter's own one-instrument refusal.
            strategy.limit = None
    elif value is None:
        del strategy.params[option]
    else:
        strategy.params[option] = value

    run(build_node(strategy))

    assert_completed(strategy, failure=reason)
    assert venue.latest_calls == venue.historical.requests == []


def test_a3_bounds_are_ignored_and_none_limit_is_allowed(build_node, venue):
    strategy = SeedRequester()
    strategy.limit = None
    strategy.request_start = datetime(2020, 1, 1, tzinfo=UTC)
    strategy.request_end = datetime(2020, 1, 2, tzinfo=UTC)

    run(build_node(strategy))

    assert_completed(strategy)
    assert len(strategy.quotes) == len(venue.latest_calls) == 1
    (response,) = venue.responses
    assert response.start is response.end is None
    assert venue.latest_calls[0].to_request_fields() == {"symbols": "AAPL", "feed": "sip"}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (None, None),
        ({"bp": 0}, "OneSidedQuoteError"),
        ({"ap": 0}, "OneSidedQuoteError"),
        ({"bp": 1.12345}, "FinePriceError"),
        ({"ap": 1.12345}, "FinePriceError"),
    ],
)
def test_a4_absent_or_unusable_quote_returns_zero_ticks(
    build_node, venue, alpaca, capfd, changes, reason
):
    venue.latest_result = {} if changes is None else {"AAPL": alpaca.quote(**changes)}
    strategy = SeedRequester()

    run(build_node(strategy))

    assert_completed(strategy)
    assert strategy.quotes == []
    assert len(venue.latest_calls) == 1
    if reason is not None:
        logging_sync_to_disk()
        captured = capfd.readouterr()
        assert reason in captured.out + captured.err


@pytest.mark.parametrize("problem", ["http", "timeout", "sdk", "raw", "list", "none", "object"])
def test_a1_a5_errors_complete_once_with_the_named_failure(build_node, venue, alpaca, problem):
    if problem == "http":
        venue.latest_error = alpaca.api_error(403, 40310000, "offline feed denied")
        reason = "APIError"
    elif problem == "timeout":
        venue.latest_error = requests.Timeout("offline latest timeout")
        reason = "Timeout"
    elif problem == "sdk":
        venue.latest_error = RuntimeError("offline SDK failure")
        reason = "RuntimeError"
    else:
        venue.latest_result = {
            "raw": {"AAPL": {"bp": 100}},
            "list": [alpaca.quote()],
            "none": {"AAPL": None},
            "object": {"AAPL": object()},
        }[problem]
        reason = "TypeError"
    strategy = SeedRequester()

    run(build_node(strategy))

    assert_completed(strategy, failure=reason)
    assert len(venue.latest_calls) == 1
    assert venue.historical.requests == []
    assert venue.responses == []


@pytest.mark.parametrize("catalog_kind", ["none", "empty", "iex"])
def test_a6_native_engine_delivers_only_seed_without_catalog_writes(
    build_node, venue, alpaca, tmp_path, catalog_kind
):
    catalog = None if catalog_kind == "none" else tmp_path
    stored = None
    if catalog_kind == "iex":
        stored = parse_quote(alpaca.quote(bp=10, ap=11), parse_equity(alpaca.asset(), 0))
        ParquetDataCatalog(str(tmp_path)).write_quote_ticks([stored])
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    strategy = SeedRequester()

    run(build_node(strategy, catalog=catalog))

    assert_completed(strategy)
    (tick,) = strategy.quotes
    assert tick.ts_event == dt_to_unix_nanos(venue.latest_result["AAPL"].timestamp)
    assert tick.ts_init >= strategy.sent_at
    assert strategy.cached == tick
    assert tick != stored
    assert len(venue.latest_calls) == 1
    (request,) = venue.requests
    assert request.params == SEED_PARAMS
    if catalog is not None:
        assert request.start is not None and request.end is not None
        assert tick.ts_init > request.end_ns
    (response,) = venue.responses
    assert response.start is response.end is None
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    if stored is None:
        assert list((tmp_path / "data" / "quotes").rglob("*")) == []
    else:
        assert ParquetDataCatalog(str(tmp_path)).query_quote_ticks([str(AAPL)]) == [stored]


def test_a2_subscription_then_request_in_same_handler_has_a_named_outcome(build_node, venue, capfd):
    strategy = SeedRequester()
    strategy.immediate = True

    run(build_node(strategy))

    request_entry = next(entry for entry in venue.order if entry[0] == "request")
    if request_entry[1] is None:
        assert_completed(strategy, failure="no stream feed yet")
        assert venue.latest_calls == []
    else:
        assert venue.order.index(("started", DataFeed.SIP)) < venue.order.index(request_entry)
        assert_completed(strategy)
        assert len(strategy.quotes) == len(venue.latest_calls) == 1
    with capfd.disabled():
        print(
            f"Native subscribe/request order: {venue.order}; "
            f"failure={strategy.responses[0].failure_reason}"
        )


def test_a8_node_timer_runs_during_two_second_sdk_call(build_node, venue):
    venue.delay = 2
    strategy = SeedRequester()
    strategy.busy = venue

    run(build_node(strategy))

    assert_completed(strategy)
    assert len(venue.latest_calls) == len(strategy.quotes) == 1
    assert len(strategy.timer_during_call) >= 5
    assert strategy.timer_during_call[-1] - strategy.timer_during_call[0] >= 400_000_000
    assert venue.latest_threads[0] != threading.get_ident()


def test_a7_history_after_a_seed_keeps_its_feed_bounds_and_event_stamp(build_node, venue, alpaca):
    class SeedThenHistory(SeedRequester):
        def __init__(self) -> None:
            super().__init__()
            self.seed_response = None
            self.seed_quotes = []

        def completed(self, request_id) -> None:
            if self.seed_response is None:
                self.seed_response = self.responses[0]
                self.seed_quotes = list(self.quotes)
                self.responses.clear()
                self.quotes.clear()
                self.events.clear()
                self.params = {}
                self.limit = None
                self.request_start = datetime(2021, 2, 22, 15, 51, tzinfo=UTC)
                self.request_end = self.request_start + timedelta(minutes=1)
                self.send_request()
            else:
                super().completed(request_id)

    venue.historical.quotes = alpaca.quotes_at(venue.latest_result["AAPL"].timestamp)
    strategy = SeedThenHistory()

    run(build_node(strategy))

    assert strategy.seed_response is not None
    assert strategy.seed_response.failure_reason is None
    assert len(strategy.seed_quotes) == len(venue.latest_calls) == 1
    assert_completed(strategy)
    (tick,) = strategy.quotes
    assert tick.ts_init == tick.ts_event == dt_to_unix_nanos(venue.latest_result["AAPL"].timestamp)
    (sent,) = venue.historical.requests
    assert sent.feed is DataFeed.IEX
    assert strategy.request_start is not None and strategy.request_end is not None
    assert sent.start == strategy.request_start.replace(tzinfo=None)
    assert sent.end == strategy.request_end.replace(tzinfo=None)
    assert sent.limit is None
    response = venue.responses[-1]
    assert response.start == dt_to_unix_nanos(strategy.request_start)
    assert response.end == dt_to_unix_nanos(strategy.request_end)


@pytest.mark.parametrize("status", [429, 504])
@pytest.mark.parametrize("exhausted", [False, True])
def test_a1_a5_real_sdk_retry_is_one_call_and_does_not_block_node(
    build_node, venue, monkeypatch, status, exhausted
):
    historical = StockHistoricalDataClient("offline-placeholder", "offline-placeholder")
    venue.historical._session.close()
    venue.historical = historical
    http_calls = []
    original = historical.get_stock_latest_quote

    def latest(params):
        venue.latest_calls.append(params)
        venue.latest_threads.append(threading.get_ident())
        venue.entered.set()
        try:
            return original(params)
        finally:
            venue.ended.set()

    def answer(method, url, **kwargs):
        http_calls.append((method, url, dict(kwargs["params"]), time.monotonic()))
        response = requests.Response()
        response.status_code = status if exhausted or len(http_calls) == 1 else 200
        body = (
            {"code": status * 10000, "message": "offline retryable failure"}
            if response.status_code != 200
            else {
                "quotes": {
                    "AAPL": {
                        "t": "2026-11-13T20:00:00Z",
                        "bp": 100,
                        "bs": 1,
                        "ap": 100.01,
                        "as": 1,
                    }
                }
            }
        )
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(historical, "get_stock_latest_quote", latest)
    monkeypatch.setattr(historical._session, "request", answer)
    strategy = SeedRequester()
    strategy.busy = venue

    run(build_node(strategy))

    assert_completed(strategy, failure="APIError" if exhausted else None)
    assert len(venue.latest_calls) == 1
    assert len(strategy.quotes) == (0 if exhausted else 1)
    assert len(http_calls) == (4 if exhausted else 2)
    assert all(
        method == "GET"
        and url.endswith("/v2/stocks/quotes/latest")
        and params == {"symbols": "AAPL", "feed": "sip", "limit": None, "page_token": None}
        for method, url, params, _ in http_calls
    )
    assert http_calls[-1][3] - http_calls[0][3] >= (9 if exhausted else 3)
    assert len(strategy.timer_during_call) >= 5
    assert venue.latest_threads[0] != threading.get_ident()
