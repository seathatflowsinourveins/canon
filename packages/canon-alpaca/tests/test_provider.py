"""AlpacaInstrumentProvider against a stand-in for alpaca-py's TradingClient. Offline.

The provider is unbound here, so its async loaders are awaited directly, as the adapter guide prescribes
for an unbound provider inside an active event loop.
"""

import asyncio

import pytest
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import AssetExchange
from alpaca.trading.enums import AssetStatus
from canon_alpaca.providers import AlpacaInstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model import InstrumentId

AAPL = InstrumentId.from_str("AAPL.ALPACA")
MSFT = InstrumentId.from_str("MSFT.ALPACA")


def test_load_all_asks_for_active_us_equities(alpaca) -> None:
    client = alpaca.TradingClient()
    provider = AlpacaInstrumentProvider(assets_client=client)

    asyncio.run(provider.load_all_async())

    (request,) = client.all_requests
    assert request.asset_class == AssetClass.US_EQUITY
    assert request.status == AssetStatus.ACTIVE
    assert request.exchange is None
    assert [i.id for i in provider.list_all()] == [AAPL]


def test_load_all_passes_supported_filters(alpaca) -> None:
    client = alpaca.TradingClient()
    provider = AlpacaInstrumentProvider(assets_client=client)

    asyncio.run(provider.load_all_async({"exchange": "NASDAQ", "attributes": "has_options"}))

    (request,) = client.all_requests
    assert request.exchange == AssetExchange.NASDAQ
    assert request.attributes == "has_options"
    assert request.asset_class == AssetClass.US_EQUITY


def test_load_all_refuses_unknown_filters(alpaca) -> None:
    provider = AlpacaInstrumentProvider(assets_client=alpaca.TradingClient())
    with pytest.raises(ValueError, match="asset_class"):
        asyncio.run(provider.load_all_async({"asset_class": "crypto"}))


def test_initialize_loads_configured_ids_by_symbol(alpaca) -> None:
    msft = {**alpaca.ASSET_AAPL, "symbol": "MSFT", "name": "Microsoft Corporation Common Stock"}
    client = alpaca.TradingClient([alpaca.ASSET_AAPL, msft])
    provider = AlpacaInstrumentProvider(
        InstrumentProviderConfig(load_ids=["MSFT.ALPACA"]),
        assets_client=client,
    )

    asyncio.run(provider.initialize())

    assert client.all_requests == []
    assert client.symbol_requests == ["MSFT"]
    assert [i.id for i in provider.list_all()] == [MSFT]


def test_load_ids_keeps_earlier_instruments(alpaca) -> None:
    msft = {**alpaca.ASSET_AAPL, "symbol": "MSFT"}
    provider = AlpacaInstrumentProvider(
        assets_client=alpaca.TradingClient([alpaca.ASSET_AAPL, msft]),
    )

    asyncio.run(provider.load_ids_async([AAPL]))
    asyncio.run(provider.load_ids_async([MSFT]))

    assert {i.id for i in provider.list_all()} == {AAPL, MSFT}


def test_load_ids_names_every_failure_and_keeps_the_rest(alpaca) -> None:
    provider = AlpacaInstrumentProvider(assets_client=alpaca.TradingClient())
    missing = [InstrumentId.from_str("NOPE.ALPACA"), InstrumentId.from_str("ALSO.ALPACA")]

    with pytest.raises(RuntimeError, match="NOPE.ALPACA.*ALSO.ALPACA"):
        asyncio.run(provider.load_ids_async([missing[0], AAPL, missing[1]]))

    assert [i.id for i in provider.list_all()] == [AAPL]


def test_load_ids_refuses_other_venues(alpaca) -> None:
    client = alpaca.TradingClient()
    provider = AlpacaInstrumentProvider(assets_client=client)

    with pytest.raises(ValueError, match="AAPL.XNAS"):
        asyncio.run(provider.load_ids_async([InstrumentId.from_str("AAPL.XNAS")]))
    assert client.symbol_requests == []


def test_load_ids_refuses_filters(alpaca) -> None:
    provider = AlpacaInstrumentProvider(assets_client=alpaca.TradingClient())
    with pytest.raises(ValueError, match="load_all only"):
        asyncio.run(provider.load_ids_async([AAPL], {"exchange": "NASDAQ"}))


def test_constructor_builds_no_client() -> None:
    provider = AlpacaInstrumentProvider()
    assert provider._assets_client is None
