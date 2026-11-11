"""Contract tests: the upstream names this package's README quotes still exist in the pinned packages.

They import and inspect only. No network, no credentials, and no node is built or run.

If one fails after a pin moves, the upstream interface changed: re-read the adapter guide
(https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md) before touching the adapter.

Sources of the quoted names: NautilusTrader's Python adapter guide and examples/live/_template/README.md
at the pinned commit; alpaca-py's TradingClient and TradingStream. The private attributes read below
(`_base_url`, `_endpoint`) are read only here, to show where the clients the adapter builds connect.
"""

import importlib
import inspect

import pytest

QUOTED = [
    ("nautilus_trader.live.clients", "DataClient"),
    ("nautilus_trader.live.clients", "MarketDataClient"),
    ("nautilus_trader.live.clients", "ExecutionClient"),
    ("nautilus_trader.live.clients", "DataClientFactory"),
    ("nautilus_trader.live.clients", "ExecutionClientFactory"),
    ("nautilus_trader.live", "LiveNode"),
    ("nautilus_trader.live", "LiveNodeBuilder"),
    ("nautilus_trader.config", "DataClientConfig"),
    ("nautilus_trader.config", "ExecutionClientConfig"),
    ("nautilus_trader.config", "InstrumentProviderConfig"),
    ("nautilus_trader.config", "LiveNodeConfig"),
    ("nautilus_trader.common", "Environment"),
    ("nautilus_trader.model", "TraderId"),
    ("nautilus_trader.model", "AccountBalance"),
    ("nautilus_trader.model", "OrderStatusReport"),
    ("nautilus_trader.model", "FillReport"),
    ("nautilus_trader.model", "PositionStatusReport"),
    ("nautilus_trader.live", "SubmitOrder"),
    ("nautilus_trader.live", "CancelOrder"),
    ("nautilus_trader.live", "CancelAllOrders"),
    ("nautilus_trader.live", "GenerateOrderStatusReport"),
    ("nautilus_trader.live", "GenerateOrderStatusReports"),
    ("nautilus_trader.live", "GenerateFillReports"),
    ("nautilus_trader.live", "GeneratePositionStatusReports"),
]

EXECUTION_HOOKS = [
    "_submit_order",
    "_cancel_order",
    "_cancel_all_orders",
    "_batch_cancel_orders",
    "_generate_order_status_report",
    "_generate_order_status_reports",
    "_generate_fill_reports",
    "_generate_position_status_reports",
    "_generate_mass_status",
]
"""The hooks the guide's "Execution client hooks" names that `AlpacaExecutionClient` overrides or keeps."""


@pytest.mark.parametrize(("module", "name"), QUOTED)
def test_quoted_upstream_name_exists(module: str, name: str) -> None:
    assert hasattr(importlib.import_module(module), name)


def test_data_factory_signature() -> None:
    from nautilus_trader.live.clients import DataClientFactory

    params = list(inspect.signature(DataClientFactory.create).parameters)
    assert params == ["name", "config", "cache", "clock"]


def test_execution_factory_signature() -> None:
    from nautilus_trader.live.clients import ExecutionClientFactory

    params = list(inspect.signature(ExecutionClientFactory.create).parameters)
    assert params == ["name", "config", "cache", "clock", "trader_id"]


@pytest.mark.parametrize("hook", EXECUTION_HOOKS)
def test_execution_hook_exists(hook: str) -> None:
    from nautilus_trader.live.clients import ExecutionClient

    assert inspect.iscoroutinefunction(getattr(ExecutionClient, hook))


def test_cancel_all_reads_what_the_client_uses() -> None:
    """`_cancel_all_orders` reads the command's instrument and side and selects with the cache's `side=`."""
    from nautilus_trader.live import CancelAllOrders
    from nautilus_trader.live import ClientCache

    assert {"instrument_id", "order_side"} <= set(dir(CancelAllOrders))
    for method in (ClientCache.orders_open, ClientCache.orders_inflight):
        assert {"instrument_id", "side"} <= set(inspect.signature(method).parameters)


def test_batch_cancel_default_calls_the_single_cancel_hook_in_order() -> None:
    """The guide's default, kept by the client: "ordered calls of the corresponding single-order hook"."""
    import asyncio
    from types import SimpleNamespace

    from nautilus_trader.live.clients import ExecutionClient

    called: list[object] = []

    class Recorder:
        async def _cancel_order(self, cancel: object) -> None:
            called.append(cancel)

    batch = SimpleNamespace(cancels=["first", "second", "third"])
    asyncio.run(ExecutionClient._batch_cancel_orders(Recorder(), batch))  # ty: ignore[invalid-argument-type]

    assert called == ["first", "second", "third"]


def test_execution_client_constructor_takes_the_documented_identity() -> None:
    from nautilus_trader.live.clients import ExecutionClient

    params = inspect.signature(ExecutionClient.__init__).parameters
    for name in (
        "trader_id",
        "venue",
        "account_id",
        "account_type",
        "oms_type",
        "instrument_provider",
    ):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_generate_order_filled_signature() -> None:
    from nautilus_trader.live.clients import ExecutionClient

    params = list(inspect.signature(ExecutionClient.generate_order_filled).parameters)
    assert params == [
        "self",
        "order",
        "venue_order_id",
        "venue_position_id",
        "trade_id",
        "last_qty",
        "last_px",
        "quote_currency",
        "commission",
        "liquidity_side",
        "ts_event",
    ]


def test_plain_execution_config_drops_unknown_keywords() -> None:
    """Why `AlpacaExecutionClientConfig` refuses keywords itself: the native config keeps none of them."""
    from nautilus_trader.config import ExecutionClientConfig

    assert set(ExecutionClientConfig(paper=False, url_override="x").dict()) == {
        "instrument_provider",
        "routing",
    }


def test_sandbox_environment_exists() -> None:
    from nautilus_trader.common import Environment

    assert hasattr(Environment, "SANDBOX")


def test_alpaca_trading_client_defaults_to_paper() -> None:
    from alpaca.trading.client import TradingClient

    assert inspect.signature(TradingClient.__init__).parameters["paper"].default is True


def test_alpaca_trading_stream_defaults_to_paper() -> None:
    from alpaca.trading.stream import TradingStream

    assert inspect.signature(TradingStream.__init__).parameters["paper"].default is True


def test_clients_built_as_the_adapter_builds_them_target_paper() -> None:
    """`TradingClient(..., paper=True)` and `TradingStream(..., paper=True)`, built offline."""
    from alpaca.common.enums import BaseURL
    from alpaca.trading.client import TradingClient
    from alpaca.trading.stream import TradingStream

    key = secret = "offline-placeholder"
    client = TradingClient(api_key=key, secret_key=secret, paper=True)
    stream = TradingStream(api_key=key, secret_key=secret, paper=True)
    assert client._base_url == BaseURL.TRADING_PAPER
    assert stream._endpoint == BaseURL.TRADING_STREAM_PAPER


def test_alpaca_py_rest_clients_keep_a_requests_session_where_the_timeout_is_mounted() -> None:
    """ADR 0039: the HTTP timeout is a transport adapter mounted on `RESTClient._session`, a private
    attribute of alpaca-py 0.45.0. A pin move that renames or replaces it fails here, not silently."""
    import requests
    from alpaca.common.rest import RESTClient
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import CONNECT_TIMEOUT_SECS
    from canon_alpaca.providers import READ_TIMEOUT_SECS
    from canon_alpaca.providers import TimeoutAdapter
    from canon_alpaca.providers import with_http_timeout

    key = secret = "offline-placeholder"
    for client in (
        TradingClient(api_key=key, secret_key=secret, paper=True),
        StockHistoricalDataClient(api_key=key, secret_key=secret),
    ):
        assert isinstance(client, RESTClient)
        assert isinstance(client._session, requests.Session)
        with_http_timeout(client)
        for prefix in ("https://", "http://"):
            adapter = client._session.adapters[prefix]
            assert isinstance(adapter, TimeoutAdapter)
            assert adapter.timeout == (CONNECT_TIMEOUT_SECS, READ_TIMEOUT_SECS)
    # alpaca-py sends every request through `self._session.request` and passes no timeout of its own.
    source = inspect.getsource(RESTClient._one_request)
    assert "self._session.request(method, url, **opts)" in source
    assert "timeout" not in inspect.getsource(RESTClient._request)
    assert "timeout" not in inspect.getsource(RESTClient._one_request)


def test_live_hosts_are_alpaca_py_live_urls() -> None:
    from canon_alpaca.execution import LIVE_HOSTS
    from canon_alpaca.execution import names_live_host

    assert LIVE_HOSTS == {"api.alpaca.markets"}
    assert not names_live_host("https://paper-api.alpaca.markets")
    assert not names_live_host("wss://paper-api.alpaca.markets/stream")
    assert names_live_host("API.alpaca.markets")


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_trade_stream_stand_in_matches_alpaca_py(make, alpaca) -> None:
    """The conftest stand-in refuses what alpaca-py's `TradingStream` refuses, before it runs."""
    from alpaca.trading.stream import TradingStream

    stream = (
        TradingStream(api_key="offline-placeholder", secret_key="offline-placeholder")
        if make == "real"
        else alpaca.TradeStream()
    )

    async def handler(_: object) -> None: ...

    with pytest.raises(ValueError, match="coroutine"):
        stream.subscribe_trade_updates(lambda _: None)
    stream.subscribe_trade_updates(handler)


def test_api_error_status_comes_from_the_http_error(alpaca) -> None:
    """The adapter reads `APIError.status_code`, `.code` and `.message`: alpaca-py's own REST client raises
    one for a 422, from a transport adapter that answers in place of the network, and the stand-in's has
    the same fields."""
    import json

    import requests
    import requests.adapters
    from alpaca.common.exceptions import APIError
    from alpaca.trading.client import TradingClient

    body = {"code": 42210000, "message": "order is not cancelable"}

    class Answers422(requests.adapters.BaseAdapter):
        def send(
            self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None
        ) -> requests.Response:
            response = requests.Response()
            response.status_code = 422
            response._content = json.dumps(body).encode()
            response.url = request.url
            response.request = request
            return response

        def close(self) -> None:
            pass

    client = TradingClient("offline-placeholder", "offline-placeholder", paper=True)
    client._session.mount("https://", Answers422())  # nothing reaches the network
    with pytest.raises(APIError) as raised:
        client.cancel_order_by_id("61e69015-8549-4bfd-b9c3-01e75843f47d")
    real = raised.value
    stand_in = alpaca.api_error(422, body["code"], body["message"])

    assert (real.status_code, real.code, real.message) == (422, 42210000, "order is not cancelable")
    assert (stand_in.status_code, stand_in.code, stand_in.message) == (
        real.status_code,
        real.code,
        real.message,
    )


def stock_data_stream():
    """alpaca-py's real stream, built offline: it connects only once `run()` is called."""
    from alpaca.data.live.stock import StockDataStream

    return StockDataStream(api_key="offline-placeholder", secret_key="offline-placeholder")


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_stream_stand_in_matches_alpaca_py(make, alpaca) -> None:
    """The conftest stand-in refuses and keeps what alpaca-py's `StockDataStream` does, before it runs."""
    stream = stock_data_stream() if make == "real" else alpaca.Stream()

    async def handler(_: object) -> None: ...

    with pytest.raises(ValueError, match="coroutine"):
        stream.subscribe_quotes(lambda _: None, "AAPL")
    with pytest.raises(KeyError):
        stream.unsubscribe_trades("AAPL")  # never subscribed
    stream.subscribe_trades(handler, "AAPL")
    stream.register_trade_cancels(handler)
    stream.register_trade_corrections(handler)
    stream.unsubscribe_trades("AAPL")


def running_stream(make: str, alpaca, monkeypatch):
    """A stream to drive on a thread of its own: alpaca-py's real one on a `MemorySocket`, or the
    stand-in, with what each shows of the venue side and whether it counts as running."""
    if make == "real":
        stream, socket = alpaca.memory_stream(monkeypatch)
        return stream, (lambda: socket.wire), (lambda: stream._running), socket
    stream = alpaca.Stream()
    stream.quotes, stream.trades = [], []
    return stream, (lambda: stream.wire), stream.running.is_set, None


def wait_for(condition, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_stream_stand_in_sends_an_unsubscribe_only_while_running(make, alpaca, monkeypatch) -> None:
    """alpaca-py's `_unsubscribe` sends only while running; before, it only deletes the handlers."""
    stream, wire, _, socket = running_stream(make, alpaca, monkeypatch)

    async def handler(_: object) -> None: ...

    stream.subscribe_quotes(handler, "AAPL")
    stream.unsubscribe_quotes("AAPL")

    sent = socket.sent if socket is not None else stream.unsubscribed
    assert sent == []
    assert wire() == {"quotes": set(), "trades": set()}


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_stream_stand_in_stops_one_run_at_a_time(make, alpaca, monkeypatch) -> None:
    """alpaca-py's `stop()` before any run keeps the first from starting; between runs it does nothing,
    and the next `run()` starts and waits for a subscription."""
    import threading

    stream, _, _, _ = running_stream(make, alpaca, monkeypatch)
    stream.stop()  # before the first run
    first = threading.Thread(target=stream.run, daemon=True)
    first.start()
    first.join(2)
    assert not first.is_alive()

    stream.stop()  # between runs
    second = threading.Thread(target=stream.run, daemon=True)
    second.start()
    try:
        second.join(0.2)
        assert second.is_alive()  # not stopped by the stop before it
    finally:
        stream.stop()
        second.join(2)
    assert not second.is_alive()


@pytest.mark.parametrize("make", ["real", "stand-in"])
def test_stream_stand_in_misses_a_subscribe_made_while_it_connects(
    make, alpaca, monkeypatch
) -> None:
    """alpaca-py's `_drive_run` sends the handler maps as they are when it connects and only then counts
    as running, and `_subscribe` sends only while running: a subscribe made in between is not sent
    until a later subscribe sends the maps again."""
    import threading

    stream, wire, running, socket = running_stream(make, alpaca, monkeypatch)
    gate = threading.Event()
    if socket is not None:
        socket.gate, held = gate, socket.holding
    else:
        stream.snapshot_gate, held = gate, stream.snapshot_taken

    async def handler(_: object) -> None: ...

    thread = threading.Thread(target=stream.run, daemon=True)
    thread.start()
    try:
        stream.subscribe_quotes(handler, "AAPL")
        wait_for(held.is_set)
        stream.subscribe_trades(handler, "AAPL")  # after the maps were taken, before running
        gate.set()
        wait_for(running)
        assert wire() == {"quotes": {"AAPL"}, "trades": set()}
        stream.subscribe_quotes(handler, "MSFT")  # running: the maps are sent whole
        assert wire() == {"quotes": {"AAPL", "MSFT"}, "trades": {"AAPL"}}
    finally:
        gate.set()
        stream.stop()
        thread.join(7)
    assert not thread.is_alive()


def test_stand_in_api_errors_carry_their_http_status(alpaca) -> None:
    """The stand-ins raise `APIError` with the status alpaca-py reads from the HTTP error, as
    `test_api_error_status_comes_from_the_http_error` shows the real client does: 404 for an asset not
    found ("Get an Asset"), and a 422 for the bars stand-in's inverted window."""
    from datetime import UTC
    from datetime import datetime

    from alpaca.common.exceptions import APIError
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    with pytest.raises(APIError) as missing:
        alpaca.TradingClient().get_asset("MSFT")
    assert (missing.value.status_code, missing.value.code) == (404, 40410000)

    inverted = StockBarsRequest(
        symbol_or_symbols="AAPL",
        timeframe=TimeFrame.Hour,
        start=datetime(2026, 1, 2, tzinfo=UTC),
        end=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(APIError) as refused:
        alpaca.HistoricalClient().get_stock_bars(inverted)
    assert (refused.value.status_code, refused.value.code) == (422, 42210000)
