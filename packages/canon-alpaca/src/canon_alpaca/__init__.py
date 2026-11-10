"""Out-of-tree NautilusTrader adapter for Alpaca, built on alpaca-py.

First slice: instruments and market data for US equities, paper data only. The modules follow upstream's
adapter template at the pinned NautilusTrader commit:

* `constants` - venue identity and the venue facts the other modules share
* `providers` - `AlpacaInstrumentProvider`, US equities from Alpaca's assets endpoint
* `data` - `AlpacaDataClient`, historical bars and live quotes and trades
* `factories` - `AlpacaDataClientFactory`

There is no execution client yet (`execution.py` comes with the order slice). See this package's README.md
and docs/adr/0009-trading-foundation.md.
"""
