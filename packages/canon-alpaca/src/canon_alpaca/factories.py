"""Build the Alpaca data and execution clients and their instrument providers from node configuration.

Contract: `nautilus_trader.live.clients.DataClientFactory` and `ExecutionClientFactory`, NautilusTrader
v2.0.0rc8 (Python adapter guide, "Define a factory",
https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md): `create(*, name, config,
cache, clock)` returns the client synchronously, and execution factories also receive `trader_id`. Each
passes the node's values through and starts no task and opens no network resource. Register the classes
under a client name with `LiveNodeBuilder.add_data_client` and `add_exec_client`, or
`LiveNode.build(..., data_factories=..., exec_factories=...)`.

The config is a plain `DataClientConfig`: this slice has no adapter fields (the feed is fixed in
`constants.FEED` and credentials come from the environment), so there is no `AlpacaDataClientConfig` yet.
Its `instrument_provider` decides what is loaded: `load_ids=["AAPL.ALPACA", ...]`, or `load_all=True` with
optional `filters` (`providers.FILTER_KEYS`).

The execution config is an `AlpacaExecutionClientConfig`, which refuses anything but paper trading; the
client refuses any other config class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClientFactory

from .data import AlpacaDataClient
from .execution import AlpacaExecutionClient
from .providers import AlpacaInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.common import Clock
    from nautilus_trader.config import DataClientConfig
    from nautilus_trader.config import ExecutionClientConfig
    from nautilus_trader.live import ClientCache
    from nautilus_trader.model import TraderId


class AlpacaDataClientFactory(DataClientFactory):
    """Build a fresh Alpaca data client for one node run."""

    @staticmethod
    def create(
        *,
        name: str,
        config: DataClientConfig,
        cache: ClientCache,
        clock: Clock,
    ) -> AlpacaDataClient:
        """Build the client and provider. Credentials are read later, in the client's `_connect`."""
        return AlpacaDataClient(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            instrument_provider=AlpacaInstrumentProvider(config.instrument_provider),
        )


class AlpacaExecutionClientFactory(ExecutionClientFactory):
    """Build a fresh Alpaca paper execution client for one node run."""

    @staticmethod
    def create(
        *,
        name: str,
        config: ExecutionClientConfig,
        cache: ClientCache,
        clock: Clock,
        trader_id: TraderId,
    ) -> AlpacaExecutionClient:
        """Build the client and provider. Credentials are read later, in the client's `_connect`."""
        return AlpacaExecutionClient(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            trader_id=trader_id,
            instrument_provider=AlpacaInstrumentProvider(config.instrument_provider),
        )
