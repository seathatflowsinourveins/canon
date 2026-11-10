"""Build the Alpaca data client and its instrument provider from node configuration.

Contract: `nautilus_trader.live.clients.DataClientFactory`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Define a factory"): `create(*, name, config, cache, clock)` returns the client synchronously, passes the
node's name, config, cache and clock through, and starts no task and opens no network resource. Register
the class under a client name with `LiveNodeBuilder.add_data_client` or
`LiveNode.build(..., data_factories=...)`.

The config is a plain `DataClientConfig`: this slice has no adapter fields (the feed is fixed in
`constants.FEED` and credentials come from the environment), so there is no `AlpacaDataClientConfig` yet.
Its `instrument_provider` decides what is loaded: `load_ids=["AAPL.ALPACA", ...]`, or `load_all=True` with
optional `filters` (`providers.FILTER_KEYS`).

No execution client factory: orders are not in this slice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.live.clients import DataClientFactory

from .data import AlpacaDataClient
from .providers import AlpacaInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.common import Clock
    from nautilus_trader.config import DataClientConfig
    from nautilus_trader.live import ClientCache


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
