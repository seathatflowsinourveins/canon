"""The request counter on `TimeoutAdapter` (`providers.request_counts`), offline.

Real alpaca-py clients built on placeholder keys, with requests' transport (`HTTPAdapter.send`) answered
here, as `test_data_only.py` does: every request alpaca-py makes, its own 429 retry included, passes
through the adapter this package mounts (`with_http_timeout`). The counter's clock is a stand-in.
"""

from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from canon_alpaca import providers
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import RateLimited
from canon_alpaca.providers import RequestCount
from canon_alpaca.providers import RequestCounter
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import with_http_timeout
from requests.adapters import HTTPAdapter

KEY_A = "offline-placeholder-a"
KEY_B = "offline-placeholder-b"
T0 = datetime(2026, 11, 11, 15, 30, tzinfo=UTC).timestamp()
"""A wall-clock minute's start, 10:30 in New York."""


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """A fresh process-wide counter on a stand-in clock."""
    clock = Clock()
    monkeypatch.setattr(providers, "REQUESTS", RequestCounter(clock=clock))
    return clock


@pytest.fixture
def answers(monkeypatch) -> list[int]:
    """Requests' transport, answered offline: each request gets the next status in the list (200 once
    it is empty) and `{}`. The adapter this package mounts calls it after counting."""
    statuses: list[int] = []

    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = statuses.pop(0) if statuses else 200
        response.reason = {200: "OK", 429: "Too Many Requests"}.get(response.status_code, "")
        response._content = b"{}"
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return statuses


def trading(key: str = KEY_A) -> TradingClient:
    client = with_http_timeout(TradingClient(api_key=key, secret_key=key, paper=True))
    client._retry_wait = 0  # alpaca-py's wait between its 429 retries; the retries themselves stay
    return client


def one(api: str, account: int = 1) -> RequestCount:
    (count,) = [c for c in request_counts() if c.api == api and c.account == account]
    return count


def test_counts_each_wall_clock_minute_and_keeps_the_peak(clock, answers) -> None:
    client = trading()
    for _ in range(3):
        client.get("/clock")
    clock.now = T0 + 59.9
    client.get("/clock")  # same minute: 4
    clock.now = T0 + 60.0
    client.get("/clock")  # next minute: 1
    clock.now = T0 + 185.0
    for _ in range(2):
        client.get("/account")  # 15:33: 2

    count = one("trading")
    assert count.total == 7
    assert count.peak == 4
    assert count.peak_minute == datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
    assert count.rate_limited == () and count.rate_limited_total == 0


def test_a_later_minute_that_passes_the_peak_takes_it(clock, answers) -> None:
    client = trading()
    client.get("/clock")
    clock.now = T0 + 120
    client.get("/clock")
    client.get("/clock")
    assert (one("trading").peak, one("trading").peak_minute) == (
        2,
        datetime(2026, 11, 11, 15, 32, tzinfo=UTC),
    )


def test_a_burst_across_a_minute_end_shows_in_the_60_second_peak(clock, answers) -> None:
    """A wall-clock minute splits a burst that a limiter on any 60 seconds would see whole."""
    client = trading()
    clock.now = T0 + 59.0
    for _ in range(3):
        client.get("/clock")
    clock.now = T0 + 60.5
    for _ in range(3):
        client.get("/clock")
    clock.now = T0 + 125.0
    client.get("/clock")  # the burst is out of this one's 60 seconds

    count = one("trading")
    assert (count.peak, count.peak_60s) == (3, 6)


def test_a_clock_stepped_back_never_moves_the_minute_back(clock, answers) -> None:
    client = trading()
    clock.now = T0 + 61.0
    client.get("/clock")
    clock.now = T0 + 59.0  # the wall clock stepped back across the minute's start
    client.get("/clock")
    clock.now = T0 + 62.0
    client.get("/clock")

    count = one("trading")
    assert count.total == 3
    assert (count.peak, count.peak_minute) == (3, datetime(2026, 11, 11, 15, 31, tzinfo=UTC))
    assert count.peak_60s == 3


def test_a_429_is_never_stamped_before_the_send_it_answers(clock, answers) -> None:
    client = trading()
    client._retry = 0
    clock.now = T0 + 61.0
    client.get("/clock")
    answers.append(429)
    clock.now = T0 + 59.0  # stepped back again before the next send and its answer
    with pytest.raises(APIError):
        client.get("/clock")
    (limited,) = one("trading").rate_limited
    assert limited.at == datetime.fromtimestamp(T0 + 61.0, UTC)


def test_a_data_host_written_with_its_final_dot_is_the_data_api(clock) -> None:
    request = requests.Request("GET", "https://data.alpaca.markets./v2/stocks/AAPL/bars").prepare()
    request.headers["APCA-API-KEY-ID"] = KEY_A
    providers.REQUESTS.sent(request)
    assert [c.api for c in request_counts()] == ["data"]


def test_concurrent_sends_are_all_counted(clock, answers) -> None:
    client = trading()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: client.get("/clock"), range(200)))
    count = one("trading")
    assert (count.total, count.peak, count.peak_60s) == (200, 200, 200)


def test_trading_and_data_requests_are_counted_apart(clock, answers) -> None:
    trading().get("/clock")
    data = with_http_timeout(StockHistoricalDataClient(api_key=KEY_A, secret_key=KEY_A))
    data.get("/stocks/AAPL/bars", {"timeframe": "1Min"})
    data.get("/stocks/AAPL/bars", {"timeframe": "1Min"})

    assert one("trading").total == 1
    assert one("data").total == 2 and one("data").peak == 2
    assert {c.api for c in request_counts()} == {"trading", "data"}


def test_each_key_pair_is_counted_as_its_own_account(clock, answers) -> None:
    a, b = trading(KEY_A), trading(KEY_B)
    a.get("/clock")
    b.get("/clock")
    b.get("/clock")
    trading(KEY_A).get("/account")  # a second client on account A's pair adds to account A

    assert [(c.account, c.api, c.total) for c in request_counts()] == [
        (1, "trading", 2),
        (2, "trading", 2),
    ]


def test_no_key_value_reaches_the_counts(clock, answers) -> None:
    trading().get("/clock")
    assert KEY_A not in repr(request_counts())
    assert KEY_A not in repr(vars(providers.REQUESTS))
    (kept,) = providers.REQUESTS._accounts
    assert (
        kept != hashlib.sha256(KEY_A.encode()).digest()
    )  # keyed: not matchable outside the process


def test_an_authorization_header_is_an_account_and_is_not_kept(clock) -> None:
    """alpaca-py's other schemes send `Authorization` (Basic key:secret, or Bearer) in place of
    `APCA-API-KEY-ID` (`RESTClient._get_auth_headers`); each such header is its own account."""
    counter = providers.REQUESTS
    headers = [
        "Basic " + base64.b64encode(f"{KEY_A}:{KEY_A}".encode()).decode(),
        "Basic " + base64.b64encode(f"{KEY_B}:{KEY_B}".encode()).decode(),
    ]
    for header in [*headers, headers[0]]:
        request = requests.Request(
            "GET", "https://paper-api.alpaca.markets/v2/clock", headers={"Authorization": header}
        ).prepare()
        counter.sent(request)

    assert [(c.account, c.total) for c in request_counts()] == [(1, 2), (2, 1)]
    for header in headers:
        assert header not in repr(vars(counter))
        assert hashlib.sha256(header.encode()).digest() not in counter._accounts


def test_a_429_is_recorded_by_name_time_and_path_and_alpaca_py_still_retries(
    clock, answers
) -> None:
    """alpaca-py repeats a request answered 429 (`RESTClient._request`); every attempt is a request sent,
    and each 429 is recorded with the path only, no query string."""
    client = trading()
    client._retry = 1
    answers.extend([429, 429])
    clock.now = T0 + 12.5
    with pytest.raises(APIError):
        client.get("/orders", {"status": "open", "limit": 5})

    count = one("trading")
    assert count.total == 2 and count.peak == 2
    assert count.rate_limited_total == 2
    expected = RateLimited(
        name="429 Too Many Requests",
        at=datetime(2026, 11, 11, 15, 30, 12, 500000, tzinfo=UTC),
        path="/v2/orders",
    )
    assert count.rate_limited == (expected, expected)


def test_a_429_then_success_is_counted_twice_and_recorded_once(clock, answers) -> None:
    client = trading()
    answers.append(429)
    assert client.get("/clock") == {}
    count = one("trading")
    assert (count.total, count.rate_limited_total) == (2, 1)


def test_only_the_latest_429s_are_kept_and_all_are_counted(clock, answers) -> None:
    client = trading()
    client._retry = 0
    keep = providers.RATE_LIMITED_KEPT
    answers.extend([429] * (keep + 3))
    for i in range(keep + 3):
        clock.now = T0 + i
        with pytest.raises(APIError):
            client.get("/clock")
    count = one("trading")
    assert count.rate_limited_total == keep + 3
    assert len(count.rate_limited) == keep
    assert count.rate_limited[-1].at == datetime.fromtimestamp(T0 + keep + 2, UTC)


def test_a_refused_data_only_request_is_not_counted(clock, answers, monkeypatch) -> None:
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, KEY_A)
    monkeypatch.setenv(ENV_SECRET_KEY, KEY_A)
    client = trading()
    with pytest.raises(MethodRefused, match="nothing was sent"):
        client.close_position("SPY")
    assert request_counts() == ()
    client.get("/clock")
    assert one("trading").total == 1


def test_a_request_that_fails_to_connect_is_still_counted(clock, monkeypatch) -> None:
    """Counted when handed to requests' transport: an upper bound on what reached Alpaca."""

    def send(self, request, **kwargs):
        raise requests.ConnectTimeout("offline")

    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(requests.ConnectTimeout):
        trading().get("/clock")
    assert one("trading").total == 1


def test_counts_are_snapshots(clock, answers) -> None:
    client = trading()
    client.get("/clock")
    before = request_counts()
    client.get("/clock")
    assert before[0].total == 1 and one("trading").total == 2
    with pytest.raises(AttributeError):
        before[0].total = 5  # ty: ignore[invalid-assignment] - frozen


def test_an_endpoint_folds_symbols_and_ids_and_drops_the_query() -> None:
    assert providers.endpoint("get", "https://paper-api.alpaca.markets/v2/assets/AAPL") == (
        "GET paper-api.alpaca.markets/v2/assets/{symbol}"
    )
    assert providers.endpoint(
        "DELETE",
        "https://paper-api.alpaca.markets/v2/orders/0b5c6f0e-1d2a-4c3b-9e8f-7a6b5c4d3e2f",
    ) == ("DELETE paper-api.alpaca.markets/v2/orders/{id}")
    assert providers.endpoint(
        "GET", "https://data.alpaca.markets/v2/stocks/BRK.B/quotes/latest?feed=sip"
    ) == ("GET data.alpaca.markets/v2/stocks/{symbol}/quotes/latest")
    assert providers.endpoint("GET", "https://data.alpaca.markets/v2/stocks/bars?symbols=A,B") == (
        "GET data.alpaca.markets/v2/stocks/bars"
    )


def test_each_request_the_adapter_sends_is_counted_by_endpoint(clock, answers) -> None:
    """One tally: the adapter's own, so the endpoint counts and `request_counts` agree, a retry
    counting as the attempt it is (code-review-high/universe-10)."""
    client = trading()
    client.get("/assets/AAPL")
    client.get("/assets/BRK.B")
    answers.append(429)
    client.get("/clock")  # alpaca-py retries the 429: two attempts
    client.delete("/orders/0b5c6f0e-1d2a-4c3b-9e8f-7a6b5c4d3e2f")
    assert providers.endpoint_counts() == {
        "DELETE paper-api.alpaca.markets/v2/orders/{id}": 1,
        "GET paper-api.alpaca.markets/v2/assets/{symbol}": 2,
        "GET paper-api.alpaca.markets/v2/clock": 2,
    }
    assert sum(providers.endpoint_counts().values()) == one("trading").total


def test_a_rate_limited_path_is_folded_like_its_endpoint(clock, answers) -> None:
    """A 429 keeps its endpoint's folded path, not the order ID it was sent for
    (code-review-high/universe-2-4)."""
    client = trading()
    answers.append(429)
    client.delete("/orders/0b5c6f0e-1d2a-4c3b-9e8f-7a6b5c4d3e2f")
    (limited,) = one("trading").rate_limited
    assert limited.path == "/v2/orders/{id}"


def test_an_endpoints_host_and_encoded_symbol_fold_to_one_key() -> None:
    """The host is normalized as the API is (`_key`'s final dot) and an encoded symbol folds too
    (code-review-high/universe-2-5, -6)."""
    assert providers.endpoint("GET", "https://data.alpaca.markets./v2/stocks/BTC%2FUSD/bars") == (
        "GET data.alpaca.markets/v2/stocks/{symbol}/bars"
    )
    assert providers.endpoint("GET", "/v2/clock") == "GET unknown-host/v2/clock"
