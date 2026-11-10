"""Instrument provider: US equities from Alpaca's assets endpoint.

Contract: `nautilus_trader.live.providers.InstrumentProvider`, NautilusTrader v2.0.0rc8 (Python adapter
guide, "Instrument providers"): override `load_all_async` and `load_ids_async`, store instruments with
`add`, and leave `initialize` and the synchronous loaders to the base class. The data client sends the
loaded instruments to the core.

Venue side: alpaca-py 0.45.0 `TradingClient.get_all_assets(GetAssetsRequest)` and
`TradingClient.get_asset(symbol)`, which call Alpaca's `/v2/assets` endpoints ("Get Assets" and "Get an
Asset by ID or Symbol", https://docs.alpaca.markets/us/reference/get-v2-assets-1.md). `TradingClient` is
the only alpaca-py client for those endpoints. It is built with `paper=True`, the pinned default, and this
module calls nothing on it but the two asset reads.

Known limits:

* NautilusTrader's `Equity` is whole-share only (nautilus_trader issue 5462, ADR 0009). Alpaca's
  `fractionable` flag is kept in `Equity.info`, but a fractional position cannot be represented.
* One price increment for every equity: see `constants.PRICE_INCREMENT`.
* The assets endpoint carries no timestamp, so `ts_event` and `ts_init` are the time of the load.
* alpaca-py's REST clients are synchronous, so each call runs in a worker thread (`asyncio.to_thread`)
  to keep the node's event loop free.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING
from typing import Protocol
from typing import override

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import AssetStatus
from alpaca.trading.models import Asset
from alpaca.trading.requests import GetAssetsRequest
from nautilus_trader.live.providers import InstrumentProvider
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Symbol

from .constants import ENV_API_KEY
from .constants import ENV_SECRET_KEY
from .constants import PRICE_INCREMENT
from .constants import PRICE_PRECISION
from .constants import USD
from .constants import VENUE

if TYPE_CHECKING:
    from uuid import UUID

    from alpaca.common.types import RawData
    from nautilus_trader.config import InstrumentProviderConfig

FILTER_KEYS = frozenset({"status", "exchange", "attributes"})
"""`InstrumentProviderConfig.filters` keys passed to `GetAssetsRequest`. Its fourth field, `asset_class`,
is fixed to `us_equity` by this provider."""


class AssetsClient(Protocol):
    """The part of alpaca-py's `TradingClient` this provider uses (signatures as in alpaca-py 0.45.0)."""

    def get_all_assets(self, filter: GetAssetsRequest | None = None) -> list[Asset] | RawData: ...

    def get_asset(self, symbol_or_asset_id: UUID | str) -> Asset | RawData: ...


def credentials_from_env() -> tuple[str, str]:
    """Return the paper key pair from the environment, or raise naming the variables to set.

    The values are never logged or stored outside the alpaca-py client they are passed to.
    """
    api_key = os.environ.get(ENV_API_KEY)
    secret_key = os.environ.get(ENV_SECRET_KEY)
    if not api_key or not secret_key:
        raise RuntimeError(
            f"Alpaca paper credentials are not in the environment: set {ENV_API_KEY} and "
            f"{ENV_SECRET_KEY}",
        )
    return api_key, secret_key


def assets_request(filters: dict | None) -> GetAssetsRequest:
    """Build the assets query for active US equities, narrowed by the provider config's filters."""
    filters = dict(filters or {})
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise ValueError(
            f"Unsupported Alpaca asset filters {sorted(unknown)}; supported: {sorted(FILTER_KEYS)}",
        )
    return GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=filters.get("status", AssetStatus.ACTIVE),
        exchange=filters.get("exchange"),
        attributes=filters.get("attributes"),
    )


def parse_equity(asset: Asset, ts_init: int) -> Equity:
    """Convert one Alpaca asset into a NautilusTrader `Equity` keyed on the Alpaca venue."""
    if asset.asset_class != AssetClass.US_EQUITY:
        raise ValueError(f"{asset.symbol} is {asset.asset_class.value}, not us_equity")
    symbol = Symbol(asset.symbol)
    return Equity(
        instrument_id=InstrumentId(symbol, VENUE),
        raw_symbol=symbol,
        currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=PRICE_INCREMENT,
        ts_event=ts_init,
        ts_init=ts_init,
        info={
            "asset_id": str(asset.id),
            "name": asset.name,
            "exchange": asset.exchange.value,
            "status": asset.status.value,
            "tradable": asset.tradable,
            "fractionable": asset.fractionable,
            "marginable": asset.marginable,
            "shortable": asset.shortable,
            "borrow_status": asset.borrow_status.value if asset.borrow_status else None,
            "attributes": list(asset.attributes or []),
            "cusip": asset.cusip,
        },
    )


class AlpacaInstrumentProvider(InstrumentProvider):
    """Load US equities from Alpaca's assets endpoint into adapter-local storage."""

    def __init__(
        self,
        config: InstrumentProviderConfig | None = None,
        *,
        assets_client: AssetsClient | None = None,
    ) -> None:
        """Retain configuration only. The alpaca-py client is built on the first load."""
        super().__init__(config)
        self._assets_client = assets_client

    def _client(self) -> AssetsClient:
        if self._assets_client is None:
            api_key, secret_key = credentials_from_env()
            self._assets_client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        return self._assets_client

    @override
    async def load_all_async(self, filters: dict | None = None) -> None:
        """Load every asset matching `filters` (default: active US equities)."""
        request = assets_request(filters)
        assets = await asyncio.to_thread(self._client().get_all_assets, request)
        if not isinstance(assets, list):
            raise TypeError("alpaca-py returned raw asset data; the client must use raw_data=False")
        ts_init = time.time_ns()
        for asset in assets:
            self.add(parse_equity(asset, ts_init))

    @override
    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """Load each requested equity by symbol, keeping instruments loaded earlier.

        Every ID is attempted; if any cannot be loaded, the error names all of them.
        """
        if filters:
            raise ValueError("Alpaca asset filters apply to load_all only, not to load_ids")
        if not instrument_ids:
            return
        foreign = [str(i) for i in instrument_ids if i.venue != VENUE]
        if foreign:
            raise ValueError(f"Not {VENUE} instruments: {foreign}")

        client = self._client()
        failures = []
        for instrument_id in instrument_ids:
            try:
                asset = await asyncio.to_thread(client.get_asset, instrument_id.symbol.value)
                if not isinstance(asset, Asset):
                    raise TypeError("alpaca-py returned raw asset data")
                self.add(parse_equity(asset, time.time_ns()))
            except (APIError, ValueError, TypeError) as e:
                failures.append(f"{instrument_id}: {e}")
        if failures:
            raise RuntimeError(f"Could not load Alpaca instruments: {'; '.join(failures)}")
