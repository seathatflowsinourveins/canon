"""Venue identity and the venue facts the other modules share.

Contract: NautilusTrader's Python adapter template keeps shared venue identity in `constants.py`
(`examples/live/_template/README.md` at the pinned commit `d53849b216e2`, tag `v2.0.0rc8`). Every value
below names the upstream document it is taken from; none is chosen freely.
"""

from alpaca.data.enums import DataFeed
from nautilus_trader.model import AccountId
from nautilus_trader.model import Currency
from nautilus_trader.model import Price
from nautilus_trader.model import Venue

VENUE = Venue("ALPACA")
"""Instruments from this adapter are keyed `<symbol>.ALPACA`, for example `AAPL.ALPACA`."""

USD = Currency.from_str("USD")
"""US equities on Alpaca are quoted in US dollars (Get Assets reference: class `us_equity`)."""

ACCOUNT_ID = AccountId(f"{VENUE}-PAPER")
"""The execution client's account, `ALPACA-PAPER`.

NautilusTrader's `ExecutionClient` takes its account ID at construction (Python adapter guide, "Construct
the client"), before Alpaca's account number can be read, so the ID is fixed; the number is kept in the
account state's `info`. It names the paper account because no other is ever connected (ADR 0009)."""

ENV_API_KEY = "ALPACA_API_KEY"
ENV_SECRET_KEY = "ALPACA_SECRET_KEY"
"""Environment variables holding the paper key pair.

alpaca-py 0.45.0 reads no environment variable: `RESTClient._validate_credentials` uses its arguments
only, whatever its docstring says. These are the names Alpaca's own tools read: the alpaca CLI v0.0.15
(`internal/config/config.go`, `resolveCredentials`) and alpaca-mcp-server v2.4.1 (`README.md`), both
pinned in `canon/upstreams.json`.
"""

FEED = DataFeed.IEX
"""Market data feed for historical bars and the live stream.

An Alpaca Paper Only account "is only entitled to receive and make use of IEX market data"
(https://docs.alpaca.markets/us/docs/paper-trading.md). IEX is also alpaca-py's `StockDataStream`
default.
"""

PRICE_PRECISION = 4
PRICE_INCREMENT = Price.from_str("0.0001")
"""Price increment for every US equity this adapter loads.

Alpaca's assets endpoint gives `price_increment` for crypto only (Get Assets reference,
https://docs.alpaca.markets/us/reference/get-v2-assets-1.md). For US equity orders Alpaca allows at most
2 decimals at or above $1.00 and 4 decimals below (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md,
"Sub-penny increments"). NautilusTrader's `Equity` takes one increment, and its precision must equal
`price_precision`, so this slice uses the finer one: market data is never rounded, but the instrument does
not by itself stop a sub-penny order price at or above $1.00. The execution client denies such an order.
"""
