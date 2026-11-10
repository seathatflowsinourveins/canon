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
    """The adapter reads `APIError.status_code`; the stand-in builds it the way alpaca-py's REST client does."""
    error = alpaca.api_error(422, 42210000, "order is not cancelable")

    assert (error.status_code, error.code, error.message) == (
        422,
        42210000,
        "order is not cancelable",
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
