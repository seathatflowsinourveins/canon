"""Out-of-tree NautilusTrader adapter for Alpaca, built on alpaca-py.

Instruments, market data and paper execution for US equities. The modules follow upstream's adapter
template at the pinned NautilusTrader commit:

* `constants` - venue identity and the venue facts the other modules share
* `providers` - `AlpacaInstrumentProvider`, US equities from Alpaca's assets endpoint
* `data` - `AlpacaDataClient`, historical bars and live quotes and trades, and `AlpacaDataClientConfig`,
  the stream's silence bounds
* `execution` - `AlpacaExecutionClient` and `AlpacaExecutionClientConfig`, market and limit orders on
  Alpaca's paper Trading API only
* `factories` - `AlpacaDataClientFactory`, `AlpacaExecutionClientFactory`

See this package's README.md, docs/adr/0009-trading-foundation.md and docs/adr/0030-alpaca-adapter.md.
"""
