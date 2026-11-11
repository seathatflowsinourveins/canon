"""Paper test of the request counter (`providers.request_counts`): the trading and market data requests of
a real run, counted per account and per API, printed at the end.

It runs only when a paper key pair is in the environment (`mise run paper:test`); otherwise pytest reports
it as skipped. It places, modifies and cancels nothing: one asset read on Alpaca's paper Trading API
through the provider's `TradingClient(paper=True)` ("Get an Asset by ID or Symbol",
https://docs.alpaca.markets/us/reference/get-v2-assets-1.md), and one latest-quote read on the Market Data
API's IEX feed through `StockHistoricalDataClient`, each built by this package and passed to
`with_http_timeout`. The counter is process-wide, so the numbers it prints cover everything the process
sent before it; a test collected after it is not in them.
"""

import asyncio
import os

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_API
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import AlpacaInstrumentProvider
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.model import InstrumentId

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this integration test"
    ),
)


def test_paper_requests_are_counted_per_account_and_api(capsys) -> None:
    def totals() -> dict[tuple[int, str], int]:
        return {(c.account, c.api): c.total for c in request_counts()}

    before = totals()
    provider = AlpacaInstrumentProvider()
    asyncio.run(provider.load_ids_async([InstrumentId.from_str("AAPL.ALPACA")]))
    data = with_http_timeout(
        StockHistoricalDataClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY])
    )
    data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols="AAPL", feed=DataFeed.IEX)
    )
    after = totals()

    # Each read is one request at least (alpaca-py repeats a 429), both under the paper pair's one account,
    # whatever number earlier tests in the process left it.
    grew = {key for key, total in after.items() if total > before.get(key, 0)}
    (account,) = {a for a, _ in grew}
    assert grew == {(account, TRADING_API), (account, DATA_API)}
    with capsys.disabled():
        for c in request_counts():
            limited = [(r.name, r.at.isoformat(), r.path) for r in c.rate_limited]
            print(
                f"\nrequest count: account {c.account} {c.api}: total {c.total}, peak {c.peak} in the "
                f"minute from {c.peak_minute.isoformat() if c.peak_minute else None}, peak in any 60 s "
                f"{c.peak_60s}, 429s {c.rate_limited_total} {limited}"
            )
