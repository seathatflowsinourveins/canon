"""Contract tests: the upstream names this package's README quotes still exist in the pinned packages.

They import and inspect only. No network, no credentials, and no node is built or run.

If one fails after a pin moves, the upstream interface changed: re-read the adapter guide
(https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md) before touching the adapter.

Sources of the quoted names: NautilusTrader's Python adapter guide and examples/live/_template/README.md
at the pinned commit; alpaca-py's TradingClient.
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
]


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


def test_sandbox_environment_exists() -> None:
    from nautilus_trader.common import Environment

    assert hasattr(Environment, "SANDBOX")


def test_alpaca_trading_client_defaults_to_paper() -> None:
    from alpaca.trading.client import TradingClient

    assert inspect.signature(TradingClient.__init__).parameters["paper"].default is True


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
