"""On-demand entry history uses the roster loader, with counted, bounded transport attempts.

No network or real state paths: every HTTP send is replaced below, and the only data is synthetic.
The requests transport adapter and alpaca-py pagination contracts are pinned in session_window.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from canon_alpaca import providers
from nautilus_trader.model import InstrumentId
from requests.adapters import HTTPAdapter

from canon_trading import session_window
from canon_trading.early_entry_study import Day

BBB = InstrumentId.from_str("BBB.ALPACA")
AT = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)
AT_NS = int(AT.timestamp()) * 1_000_000_000
CALENDAR: Any = [SimpleNamespace(date=datetime(2026, 11, d).date()) for d in (3, 4, 5, 6, 9, 10)]


def test_the_reader_uses_the_roster_loader_and_preserves_the_exact_instant(monkeypatch) -> None:
    data = SimpleNamespace(_session=requests.Session())
    seen = []
    expected = Day("BBB", AT.date(), (), (), 10.0)

    def contexts(client, symbols, at, calendar, *, feed):
        seen.append((client, symbols, at, calendar, feed))
        return {BBB: expected}

    monkeypatch.setattr(session_window, "mover_entry_contexts", contexts)
    reader = session_window.on_demand_context_reader(cast(Any, data), CALENDAR, feed="sip")
    assert reader(BBB, AT_NS - 1) == expected
    assert seen == [(data, ["BBB"], AT - timedelta(microseconds=1), CALENDAR, "sip")]


def offline_client(
    monkeypatch, *, pages: bool = False, after_send=None, status: int = 200, clock=lambda: 10.0
):
    """Real SDK pagination and counter, with HTTPAdapter.send replaced before any connection."""
    calls = []
    counter = providers.RequestCounter(clock=clock)
    monkeypatch.setattr(providers, "REQUESTS", counter)

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(
            {"bars": {}, "next_page_token": str(len(calls)) if pages and len(calls) % 200 else None}
        ).encode()
        response.request = request
        if after_send is not None:
            after_send()
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    # Fixed test values, never an environment/keychain read; transport above cannot reach a host.
    client = providers.with_http_timeout(StockHistoricalDataClient("offline", "offline"))
    client._retry_wait = 0
    return client, calls, counter


def test_context_pages_are_counted_and_the_151st_attempt_is_refused_by_name(monkeypatch) -> None:
    now = [10.0]
    client, calls, counter = offline_client(monkeypatch, pages=True, clock=lambda: now[0])
    reader = session_window.on_demand_context_reader(client, CALENDAR, feed="sip")
    reader.clock = lambda: now[0]
    with pytest.raises(RuntimeError, match=r"BBB.ALPACA.*data.*budget.*150"):
        reader(BBB, AT_NS)
    assert len(calls) == 150
    counts = counter.counts()
    assert len(counts) == 1 and counts[0].api == providers.DATA_API
    assert counts[0].total == counts[0].peak_60s == 150
    report = session_window.WindowReport("regular", "BBB", started="20261110T160000Z")
    session_window._snapshot_requests(report)
    assert report.requests_by_api[0]["total"] == 150
    assert reader.record()["attempts"] == 150
    assert reader.record()["limit_per_minute"] == 150
    assert reader.record()["refused"][str(BBB)] == 1
    # Same minute and the boundary immediately before it cannot admit another attempt.
    now[0] = 69.999
    with pytest.raises(RuntimeError, match="budget"):
        reader(BBB, AT_NS)
    assert len(calls) == 150
    now[0] = 70.0
    with pytest.raises(RuntimeError, match="budget"):
        reader(BBB, AT_NS)
    assert len(calls) == 300
    assert counter.counts()[0].peak_60s == 150


def test_a_burst_of_names_shares_one_budget_across_both_data_calls(monkeypatch) -> None:
    client, calls, _counter = offline_client(monkeypatch)
    reader = session_window.on_demand_context_reader(client, CALENDAR, feed="sip")
    reader.clock = lambda: 10.0
    for index in range(75):
        reader(InstrumentId.from_str(f"LATE{index}.ALPACA"), AT_NS)
    assert len(calls) == 150
    with pytest.raises(RuntimeError, match="budget"):
        reader(InstrumentId.from_str("CCC.ALPACA"), AT_NS)
    assert len(calls) == 150
    assert reader.record()["refused"] == {"CCC.ALPACA": 1}


def test_sdk_retries_are_counted_and_refused_before_the_budget_can_be_exceeded(monkeypatch) -> None:
    client, calls, counter = offline_client(monkeypatch, status=504)
    reader = session_window.on_demand_context_reader(client, CALENDAR, feed="sip")
    reader.clock = lambda: 10.0
    for _ in range(37):
        with pytest.raises(APIError):
            reader(BBB, AT_NS)
    assert len(calls) == 148  # one attempt plus the pinned SDK's three retries
    with pytest.raises(RuntimeError, match=r"BBB.ALPACA.*budget.*150"):
        reader(BBB, AT_NS)
    assert len(calls) == counter.counts()[0].total == 150


def test_stopping_the_reader_refuses_the_next_page_without_sending_it(monkeypatch) -> None:
    stopped = []
    client, calls, _counter = offline_client(
        monkeypatch, pages=True, after_send=lambda: stopped[0]()
    )
    reader = session_window.on_demand_context_reader(client, CALENDAR, feed="sip")
    stopped.append(reader.stop)
    with pytest.raises(RuntimeError, match=r"BBB.ALPACA.*stopped"):
        reader(BBB, AT_NS)
    assert len(calls) == 1


def test_an_uncounted_client_is_refused_instead_of_reading_unbounded_history() -> None:
    reader = session_window.on_demand_context_reader(cast(Any, object()), CALENDAR, feed="sip")
    with pytest.raises(RuntimeError, match=r"BBB.ALPACA.*requests.Session"):
        reader(BBB, AT_NS)
