"""Against Alpaca's Market Data API with the paper pair: where SPY's and QQQ's quote history begins, by feed.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped. `mise run paper:test -- -k quote_history` runs it alone under the
paper lock. Never write the keys to a file.

ADR 0066 sizes a quote load from these dates, so a change in what Alpaca serves fails here first. What it
calls: alpaca-py's `StockHistoricalDataClient`, with the adapter's HTTP timeout mounted
(`canon_alpaca.providers.with_http_timeout`, ADR 0039), one page at a time through its `RESTClient.get`,
which returns the parsed JSON, on `/stocks/quotes`, the endpoint `get_stock_quotes` pages through
(`get_stock_quotes` itself would collect every page), with the parameters of Alpaca's
"Historical quotes" reference (https://docs.alpaca.markets/us/reference/stockquotes-1.md): `feed`
(`sip`, `iex`, `boats`, `otc`), `start`, `end`, `limit` and `sort`. One quote is asked for, from
2000-01-01 in ascending order, on each feed: twelve calls. It places, modifies and cancels nothing and
opens no position, so it leaves the account as it found it. A refusal is reported with its HTTP status,
so an outage or a credential error does not read as a feed withdrawn.
"""

from __future__ import annotations

import os
from datetime import date
from datetime import timedelta

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import with_http_timeout

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

# The first quote's date on each feed the reference names, observed on the paper pair on 2026-11-10.
FIRST_QUOTE = {
    "SPY": {"iex": "2020-07-27", "sip": "2016-01-04", "boats": "2024-09-16"},
    "QQQ": {"iex": "2020-07-28", "sip": "2016-01-04", "boats": "2024-09-16"},
}
# The reference's fourth feed, and alpaca-py's two `DataFeed` members the reference does not list for
# history, with the message Alpaca refused each with.
REFUSED = {
    "otc": "subscription does not permit querying OTC data",
    "delayed_sip": "invalid feed: delayed_sip",
    "overnight": "invalid feed: overnight",
}


def _first(client: StockHistoricalDataClient, symbol: str, feed: str) -> str:
    """The first quote's UTC timestamp on `feed`, or `refused <HTTP status>: <message>`."""
    params: dict[str, object] = {
        "symbols": symbol,
        "feed": feed,
        "start": "2000-01-01",
        "end": (date.today() - timedelta(days=1)).isoformat(),
        "limit": 1,
        "sort": "asc",
    }
    try:
        page = client.get("/stocks/quotes", params)
    except APIError as e:
        return f"refused {e.status_code}: {e}"
    assert isinstance(page, dict), page
    quotes = (page.get("quotes") or {}).get(symbol) or []
    return quotes[0]["t"] if quotes else "none"


def test_paper_quote_history_begins_where_adr_0066_says() -> None:
    client = with_http_timeout(
        StockHistoricalDataClient(os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY])
    )
    for symbol, feeds in FIRST_QUOTE.items():
        for feed, day in feeds.items():
            first = _first(client, symbol, feed)
            assert first.startswith(day), (symbol, feed, first)
        for feed, message in REFUSED.items():
            refused = _first(client, symbol, feed)
            assert refused.startswith("refused") and message in refused, (symbol, feed, refused)
