"""Historical IEX quotes: real pinned SDK response and catalog, offline HTTP transport.

Contract: alpaca-py get_stock_quotes pages /stocks/quotes into a QuoteSet, then the adapter's
parse_quote and NautilusTrader's write_quote_ticks; ADR 0066's feed, span and size-unit bounds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
from alpaca.common import rest as alpaca_rest
from alpaca.data.enums import DataFeed
from alpaca.data.models import Quote
from canon_alpaca import providers
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import parse_quote
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import parse_equity
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog
from requests.adapters import HTTPAdapter

from canon_trading import history_load
from canon_trading.bars_to_catalog import DEFAULT_CATALOG
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import RateLimited
from canon_trading.session_window import SOURCE_ROOT

_spec = importlib.util.spec_from_file_location(
    "canon_alpaca_standins_quotes", SOURCE_ROOT / "packages/canon-alpaca/tests/conftest.py"
)
assert _spec is not None and _spec.loader is not None
standins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standins)

START = datetime(2026, 11, 10, 21, tzinfo=UTC)
END = START + timedelta(minutes=3)
NOW = datetime(2026, 11, 11, 18, tzinfo=UTC)
FIRST = "2026-11-10T21:00:00.000001999Z"
LAST = "2026-11-10T21:03:00Z"
SYMBOLS = ["AAPL", "MSFT"]


def quote(timestamp=FIRST, **changes) -> dict:
    return {**standins.HISTORICAL_QUOTE_AAPL, "t": timestamp, **changes}


@dataclass
class Venue:
    rows: list[dict] = field(default_factory=lambda: [quote(), quote(LAST)])
    page_rows: int = 1
    answers: list[int] = field(default_factory=list)
    end_microseconds: bool = False
    """A server reading `end` to the microsecond only (code-review-high-quotes2/2)."""
    sent: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)

    def answer(self, request) -> tuple[int, dict]:
        url = urlsplit(request.url or "")
        query = parse_qs(url.query)
        self.sent.append((request.method, query))
        assert url.hostname == "data.alpaca.markets"
        assert url.path == "/v2/stocks/quotes"
        if self.answers:
            status = self.answers.pop(0)
            if status != 200:
                return status, {"message": f"offline HTTP {status}, subscription refused"}
        symbol = query["symbols"][0]
        lo, hi = dt_to_unix_nanos(query["start"][0]), dt_to_unix_nanos(query["end"][0])
        if self.end_microseconds:
            hi = hi // 1000 * 1000
        rows = [r for r in self.rows if lo <= dt_to_unix_nanos(r["t"]) <= hi]
        offset = int(query.get("page_token", ["0"])[0])
        page = rows[offset : offset + min(self.page_rows, int(query["limit"][0]))]
        after = offset + len(page)
        return 200, {
            "quotes": {symbol: page},
            "next_page_token": str(after) if after < len(rows) else None,
        }


@pytest.fixture
def venue(monkeypatch) -> Venue:
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, "offline-placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "offline-placeholder")
    monkeypatch.setattr(providers, "REQUESTS", providers.RequestCounter())
    venue = Venue()

    def send(self, request, **kwargs):
        status, body = venue.answer(request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return venue


@pytest.fixture
def catalog(tmp_path) -> Path:
    root = tmp_path / "catalog"
    root.mkdir()
    ParquetDataCatalog(str(root)).write_instruments(
        [parse_equity(standins.asset(symbol=s), 0) for s in SYMBOLS]
    )
    return root


@dataclass
class Clock:
    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs
        self.slept.append(secs)


def run(catalog, **kwargs) -> int:
    clock = kwargs.pop("clock", Clock())
    return history_load.load_quotes(
        history_load.history_client(),
        kwargs.pop("symbols", SYMBOLS),
        kwargs.pop("start", START),
        kwargs.pop("end", END),
        catalog,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
        out=kwargs.pop("out", lambda line: None),
        **kwargs,
    )


def ticks_in(catalog, symbol="AAPL") -> list:
    return ParquetDataCatalog(str(catalog)).query_quote_ticks([f"{symbol}.ALPACA"])


def cli(catalog, *extra) -> list[str]:
    return [
        "--data",
        "quotes",
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
        "--catalog",
        str(catalog),
        *extra,
    ]


def test_quotes_use_iex_and_the_adapter_conversion_and_typed_catalog_writer(venue, catalog):
    assert run(catalog) == 4
    for symbol in SYMBOLS:
        ticks = ticks_in(catalog, symbol)
        assert len(ticks) == 2
        assert all(t.ts_init == t.ts_event for t in ticks)
        # SDK Quote datetime truncates the raw sub-microsecond digits, as in the adapter.
        assert ticks[0].ts_event == dt_to_unix_nanos("2026-11-10T21:00:00.000001Z")
        assert str(ticks[0].bid_price) == f"{venue.rows[0]['bp']:.4f}"
        assert str(ticks[0].ask_price) == f"{venue.rows[0]['ap']:.4f}"
        assert float(ticks[0].bid_size) == venue.rows[0]["bs"]
        assert float(ticks[0].ask_size) == venue.rows[0]["as"]
        assert ParquetDataCatalog(str(catalog)).get_intervals(
            NautilusDataType.QuoteTick, f"{symbol}.ALPACA"
        ) == [(dt_to_unix_nanos(START), dt_to_unix_nanos(END) + 999)]
    assert all(m == "GET" and q["feed"] == ["iex"] and q["sort"] == ["asc"] for m, q in venue.sent)
    assert (catalog / FEED_FILE).read_text() == "iex\n"


def test_incremental_extensions_keep_submicrosecond_boundary_quotes(venue, catalog):
    venue.rows.extend(
        [
            quote("2026-11-10T21:03:00.000000001Z"),
            quote("2026-11-10T21:03:00.000000999Z"),
            quote("2026-11-10T21:03:00.000001001Z"),
        ]
    )
    assert run(catalog, symbols=["AAPL"]) == 4
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"]) == 0
    assert venue.sent == []
    assert run(catalog, symbols=["AAPL"], end=END + timedelta(minutes=1)) == 1
    assert dt_to_unix_nanos(venue.sent[0][1]["start"][0]) == dt_to_unix_nanos(END) + 1000
    assert len(ticks_in(catalog)) == 5
    assert sum(t.ts_init == dt_to_unix_nanos(END) for t in ticks_in(catalog)) == 3
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"], end=END + timedelta(minutes=1)) == 0
    assert venue.sent == []


def test_requests_are_at_most_one_hour_and_keep_inclusive_coverage(venue, catalog):
    assert run(catalog, symbols=["AAPL"], end=START + timedelta(hours=2)) == 2
    windows = sorted({(q["start"][0], q["end"][0]) for _, q in venue.sent})
    # two hours, the last holding the inclusive endpoint's microsecond (code-review-high-quotes2/1);
    # each asks to the first nanosecond after its last bucket (code-review-high-quotes2/2)
    assert len(windows) == 2
    assert [dt_to_unix_nanos(hi) - dt_to_unix_nanos(lo) for lo, hi in windows] == [
        3_600_000_000_000,
        3_600_000_001_000,
    ]
    assert all(
        dt_to_unix_nanos(lo) % 1000 == 0 and dt_to_unix_nanos(hi) % 1000 == 0 for lo, hi in windows
    )


def test_a_whole_hour_window_is_one_piece_and_its_rerun_sends_nothing(venue, catalog):
    # code-review-high-quotes2/1: the endpoint's microsecond is no separate, always-empty piece
    end = START + timedelta(hours=1)
    venue.page_rows = 10
    assert run(catalog, symbols=["AAPL"], end=end) == 2
    assert len(venue.sent) == 1
    assert run(catalog, symbols=["AAPL"], end=end) == 0
    assert len(venue.sent) == 1


def test_a_piece_asks_past_its_last_microsecond_so_a_microsecond_end_loses_no_quote(venue, catalog):
    # code-review-high-quotes2/2: a quote in the last nanoseconds of an hour's last microsecond
    venue.end_microseconds = True
    venue.rows = [quote("2026-11-10T21:59:59.999999500Z"), quote("2026-11-10T22:30:00Z")]
    assert run(catalog, symbols=["AAPL"], end=START + timedelta(hours=2)) == 2
    assert [t.ts_init for t in ticks_in(catalog)] == [
        dt_to_unix_nanos(START + timedelta(hours=1)) - 1000,
        dt_to_unix_nanos(START + timedelta(hours=1, minutes=30)),
    ]
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"], end=START + timedelta(hours=2)) == 0
    assert venue.sent == []


@pytest.mark.parametrize("retry_wait", [0, 3])
def test_every_page_and_504_retry_is_paced_and_transport_restored(
    venue, catalog, monkeypatch, retry_wait
):
    venue.answers = [504, 504, 200]
    clock = Clock()
    monkeypatch.setattr(alpaca_rest, "DEFAULT_RETRY_WAIT_SECONDS", retry_wait)
    monkeypatch.setattr(alpaca_rest.time, "sleep", clock.sleep)
    client = history_load.history_client()
    session = providers.session_of(client)
    assert session is not None
    mounted = dict(session.adapters)
    monkeypatch.setattr(history_load, "history_client", lambda: client)
    sent_at = []
    original = venue.answer

    def timed(request):
        sent_at.append(clock())
        return original(request)

    monkeypatch.setattr(venue, "answer", timed)
    assert run(catalog, clock=clock) == 4
    assert len(sent_at) == 6
    assert all(b - a >= 60 / history_load.PACE.data_per_minute - 1e-8 for a, b in pairwise(sent_at))
    assert session.adapters == mounted


@pytest.mark.parametrize(
    "status,exception,message",
    [
        (429, RateLimited, "429"),
        (403, HistoryStopped, "HTTP 403"),
    ],
)
def test_429_and_refused_feed_stop_without_retry_fallback_or_coverage(
    venue, catalog, status, exception, message
):
    venue.answers = [status]
    with pytest.raises(exception, match=message):
        run(catalog)
    assert len(venue.sent) == 1
    assert venue.sent[0][1]["feed"] == ["iex"]
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


def test_an_overfull_quote_piece_is_loaded_in_parts_each_cut_before_its_last_microsecond(
    venue, catalog
):
    """code-review-high-quotes/overfull-hour: a liquid hour holds far more than the row bound
    (ADR 0066 measured SPY near a million IEX quotes an hour), so an overfull answer is written up
    to the microsecond before its last, covered to that bucket's end, and the next part starts at
    that microsecond; the whole window ends covered and a rerun sends nothing."""
    venue.rows = [
        quote("2026-11-10T21:00:00.000001Z"),
        quote("2026-11-10T21:01:00.000002Z"),
        quote("2026-11-10T21:02:00.000003Z"),
    ]
    lines: list[str] = []
    assert run(catalog, symbols=["AAPL"], max_rows=1, out=lines.append) == 3
    assert [t.ts_event for t in ticks_in(catalog)] == [dt_to_unix_nanos(r["t"]) for r in venue.rows]
    covered = ParquetDataCatalog(str(catalog)).get_intervals(
        NautilusDataType.QuoteTick, "AAPL.ALPACA"
    )
    # one interval per part, each ending where the next begins
    assert covered[0][0] == dt_to_unix_nanos(START)
    assert covered[-1][1] == dt_to_unix_nanos(END) + 999
    assert all(b[0] == a[1] + 1 for a, b in pairwise(covered))
    assert [b[0] for b in covered[1:]] == [dt_to_unix_nanos(r["t"]) for r in venue.rows[1:]]
    starts = [dt_to_unix_nanos(q["start"][0]) for _, q in venue.sent if "page_token" not in q]
    assert starts == [dt_to_unix_nanos(START)] + [dt_to_unix_nanos(r["t"]) for r in venue.rows[1:]]
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"], max_rows=1) == 0
    assert venue.sent == []


def test_more_quotes_in_one_microsecond_than_the_row_bound_stop_by_name(venue, catalog):
    venue.rows = [quote("2026-11-10T21:00:00.000001000Z"), quote("2026-11-10T21:00:00.000001500Z")]
    with pytest.raises(HistoryStopped, match=r"more than 1 quotes at one microsecond"):
        run(catalog, symbols=["AAPL"], max_rows=1)
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


def test_a_padded_symbol_is_read_as_its_name(venue, catalog):
    """code-review-high-quotes/padded-symbol: as the prints loader reads it."""
    assert run(catalog, symbols=[" AAPL "]) == 2
    assert len(ticks_in(catalog)) == 2


def test_a_quote_end_inside_the_publication_margin_is_refused(venue, catalog):
    """code-review-high-quotes/end-at-now: coverage up to now would mark quotes the historical
    store has not yet published as covered for good."""
    end = NOW - timedelta(minutes=1)
    with pytest.raises(HistoryStopped, match=r"before now"):
        run(catalog, start=end - timedelta(minutes=1), end=end)
    assert venue.sent == []


@pytest.mark.parametrize("rows", [[], [quote(bp=0)], [quote(ap=123.000001)]])
def test_empty_or_dropped_only_history_writes_no_coverage_and_names_drops(venue, catalog, rows):
    venue.rows = rows
    lines = []
    assert run(catalog, symbols=["AAPL"], out=lines.append) == 0
    assert not (catalog / FEED_FILE).exists()
    assert not ParquetDataCatalog(str(catalog)).get_intervals(
        NautilusDataType.QuoteTick, "AAPL.ALPACA"
    )
    assert len(venue.sent) == 1
    # code-review-high-quotes2/5: a piece that covered nothing does not log coverage
    assert any(line.endswith(": 0 quotes, 1 GET(s)") for line in lines)
    assert not any(" quotes to " in line for line in lines)
    assert run(catalog, symbols=["AAPL"]) == 0
    assert len(venue.sent) == 2
    if rows:
        assert any("Dropped 1" in line and "AAPL" in line for line in lines)


@pytest.mark.parametrize(
    "start",
    [
        datetime(2025, 10, 15, tzinfo=UTC),
        datetime(2025, 11, 2, 23, tzinfo=UTC),
        datetime(2025, 11, 3, tzinfo=UTC),
        datetime(2025, 11, 3, 23, 59, tzinfo=UTC),
        # still 2025-11-03 in New York (code-review-high-quotes/cutover-day)
        datetime(2025, 11, 4, 0, 30, tzinfo=UTC),
        datetime(2025, 11, 4, 4, 59, tzinfo=UTC),
    ],
)
def test_pre_cutover_and_cutover_day_refused_without_guessing_units(venue, catalog, start):
    with pytest.raises(HistoryStopped, match=r"2025-11-04.*UNVERIFIED"):
        run(
            catalog,
            start=start,
            end=start + timedelta(seconds=1),
        )
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


def test_two_runs_cannot_add_unverified_units_to_a_share_root(venue, catalog):
    assert run(catalog, symbols=["AAPL"]) == 2
    venue.sent.clear()
    with pytest.raises(HistoryStopped, match="2025-11-04"):
        run(
            catalog,
            start=datetime(2025, 10, 15, tzinfo=UTC),
            end=datetime(2025, 10, 15, 0, 1, tzinfo=UTC),
        )
    assert venue.sent == []
    assert len(ticks_in(catalog)) == 2


@pytest.mark.parametrize("old_units", [False, True])
def test_legacy_quote_roots_are_refused_before_fetch_even_for_other_symbols(
    venue, catalog, old_units
):
    timestamp = datetime(2025, 10, 15, tzinfo=UTC) if old_units else START
    instrument = parse_equity(standins.asset(symbol="MSFT"), 0)
    tick = parse_quote(Quote("MSFT", quote(timestamp.isoformat())), instrument)
    lo = dt_to_unix_nanos(timestamp)
    ParquetDataCatalog(str(catalog)).write_quote_ticks(
        [tick], start=lo, end=lo + 999 if old_units else lo
    )
    (catalog / FEED_FILE).write_text("iex\n")
    with pytest.raises(HistoryStopped, match="legacy quote coverage"):
        run(catalog, symbols=["AAPL"])
    assert venue.sent == []
    assert ticks_in(catalog) == []


# code-review-high-quotes2/3: the repeat case stops below the page bound, under its own message
@pytest.mark.parametrize(
    "repeats,max_pages,message", [(False, 2, "page bound"), (True, 5, "repeated page token")]
)
def test_unbounded_or_repeating_pages_stop_by_name_and_restore_hooks(
    venue, catalog, monkeypatch, repeats, max_pages, message
):
    monkeypatch.setattr(history_load, "QUOTES_MAX_PAGES", max_pages, raising=False)
    client = history_load.history_client()
    session = providers.session_of(client)
    assert session is not None
    hooks = list(session.hooks["response"])
    monkeypatch.setattr(history_load, "history_client", lambda: client)
    original = venue.answer

    def empty_pages(request):
        original(request)
        return 200, {"quotes": {}, "next_page_token": "1" if repeats else str(len(venue.sent))}

    monkeypatch.setattr(venue, "answer", empty_pages)

    # Bound a broken implementation's test run without letting it issue an unbounded loop.
    def bounded(request):
        if len(venue.sent) >= 3:
            raise AssertionError("loader did not stop bad pagination")
        return empty_pages(request)

    monkeypatch.setattr(venue, "answer", bounded)
    with pytest.raises(HistoryStopped, match=rf"AAPL.*{message}"):
        run(catalog, symbols=["AAPL"])
    assert len(venue.sent) == 2
    assert ticks_in(catalog) == []
    assert session.hooks["response"] == hooks


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z"),
        ("2025-11-03T00:00:00Z", "2025-11-03T01:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-05-01T00:00:00Z"),
        ("2026-11-10T00:00:00Z", "2026-11-12T00:00:00Z"),
    ],
)
def test_cli_rejects_quote_bounds_before_building_a_client(catalog, monkeypatch, start, end):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)

    def forbidden():
        pytest.fail("invalid CLI bounds reached history_client")

    monkeypatch.setattr(history_load, "history_client", forbidden)
    with pytest.raises(SystemExit) as stopped:
        history_load.main(cli(catalog, "--symbols", "AAPL", "--start", start, "--end", end))
    assert stopped.value.code == 2


@pytest.mark.parametrize(
    "options,message",
    [
        ({"symbols": []}, "symbol"),
        ({"symbols": ["UNKNOWN"]}, "UNKNOWN"),
        ({"start": START.replace(tzinfo=None)}, "time zone"),
        ({"end": START}, "not before"),
        ({"end": NOW + timedelta(days=1)}, "now"),
        ({"start": datetime(2024, 12, 31, tzinfo=UTC)}, "2025"),
        ({"start": START - timedelta(days=91)}, "90.*ADR 0066"),
        ({"feed": DataFeed.DELAYED_SIP}, "iex.*ADR 0151"),
        ({"feed": DataFeed.BOATS}, "iex.*ADR 0151"),
        ({"max_rows": 0}, "row bound"),
    ],
)
def test_invalid_inputs_refused_before_lock_and_request(venue, catalog, options, message):
    with pytest.raises(HistoryStopped, match=message):
        run(catalog, **options)
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


@pytest.mark.parametrize("kind", ["missing", "ledger", "feed"])
def test_invalid_roots_refused_before_requests(venue, catalog, tmp_path, kind):
    if kind == "feed":
        (catalog / FEED_FILE).write_text("sip\n")
        root = catalog
    else:
        root = tmp_path / kind / "catalog"
    with pytest.raises(HistoryStopped):
        run(root)
    assert venue.sent == []


def test_quotes_share_the_catalog_lock_with_prints(venue, catalog, monkeypatch):
    original = venue.answer

    def competing(request):
        # Root is still definitions-only, so SIP preflight passes before the common lock refuses.
        with pytest.raises(HistoryStopped, match="another history load"):
            history_load.load_trades(object(), ["AAPL"], START, END, catalog, now=lambda: NOW)
        return original(request)

    monkeypatch.setattr(venue, "answer", competing)
    assert run(catalog, symbols=["AAPL"]) == 2


def test_changed_feed_is_refused_before_write(venue, catalog, monkeypatch):
    original = venue.answer

    def changed(request):
        (catalog / FEED_FILE).write_text("sip\n")
        return original(request)

    monkeypatch.setattr(venue, "answer", changed)
    with pytest.raises(HistoryStopped, match="sip"):
        run(catalog)
    assert ticks_in(catalog) == []
    assert (catalog / FEED_FILE).read_text() == "sip\n"


@pytest.mark.parametrize("partial", [False, True])
def test_write_failure_names_symbol_and_cleans_only_unused_marker(
    venue, catalog, monkeypatch, partial
):
    original = history_load.ParquetDataCatalog

    class WriteFails:
        def __init__(self, path):
            self.catalog = original(path)

        def __getattr__(self, name):
            return getattr(self.catalog, name)

        def write_quote_ticks(self, *args, **kwargs):
            if partial:
                self.catalog.write_quote_ticks(*args, **kwargs)
            raise OSError("offline disk full")

    monkeypatch.setattr(history_load, "ParquetDataCatalog", WriteFails)
    with pytest.raises(HistoryStopped, match=r"AAPL.*offline disk full"):
        run(catalog)
    assert (catalog / FEED_FILE).exists() == partial


def test_malformed_quote_stops_by_name_without_coverage(venue, catalog):
    venue.rows = [quote(bs=1.5)]
    with pytest.raises(HistoryStopped, match=r"AAPL.*fractional"):
        run(catalog)
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


@pytest.mark.parametrize("file", [False, True])
def test_cli_accepts_quotes_from_symbols_or_file(venue, catalog, monkeypatch, tmp_path, file):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    if file:
        path = tmp_path / "symbols.txt"
        path.write_text("# quotes\nAAPL\nAAPL\n")
        extra = ["--symbols-file", str(path)]
    else:
        extra = ["--symbols", "AAPL,AAPL"]
    assert history_load.main(cli(catalog, *extra)) == 0
    assert len(ticks_in(catalog)) == 2


def test_mise_exposes_quotes_mode():
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    assert 'choices "bars" "trades" "quotes"' in task["usage"]
    assert "IEX" in task["description"]


# SIP quotes, bounded: named symbols, at most `SIP_QUOTES_MAX_SYMBOLS`, at most `SIP_QUOTES_MAX_SPAN`.


def test_the_sip_quote_bounds_are_eight_symbols_and_sixty_minutes():
    assert history_load.SIP_QUOTES_MAX_SYMBOLS == 8
    assert history_load.SIP_QUOTES_MAX_SPAN == timedelta(minutes=60)


def test_a_sip_quote_load_within_the_bound_names_sip_and_writes_to_the_root(venue, catalog):
    assert run(catalog, feed=DataFeed.SIP) == 4
    assert venue.sent
    assert all(m == "GET" and q["feed"] == ["sip"] for m, q in venue.sent)
    assert (catalog / FEED_FILE).read_text() == "sip\n"
    assert len(ticks_in(catalog)) == 2 and len(ticks_in(catalog, "MSFT")) == 2


def test_a_sip_quote_load_of_exactly_sixty_minutes_is_accepted(venue, catalog):
    venue.rows = [quote(), quote("2026-11-10T21:59:59Z")]
    end = START + timedelta(minutes=60)
    assert run(catalog, symbols=["AAPL"], end=end, feed=DataFeed.SIP) == 2
    assert all(q["feed"] == ["sip"] for _, q in venue.sent)


@pytest.mark.parametrize(
    "options,message",
    [
        ({"end": START + timedelta(minutes=60, microseconds=1)}, "SIP_QUOTES_MAX_SPAN"),
        ({"end": START + timedelta(hours=2)}, "SIP_QUOTES_MAX_SPAN"),
        ({"symbols": [f"S{i}" for i in range(9)]}, "SIP_QUOTES_MAX_SYMBOLS"),
    ],
    ids=["one-microsecond-over", "two-hours", "nine-symbols"],
)
def test_a_sip_quote_load_over_the_bound_is_refused_by_name_before_any_request(
    venue, catalog, options, message
):
    with pytest.raises(HistoryStopped, match=message):
        run(catalog, feed=DataFeed.SIP, **options)
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()
    assert not (catalog / FEED_FILE).exists()


def test_eight_sip_symbols_pass_the_bound_and_meet_the_definition_check(venue, catalog):
    """Eight is inside the bound: what refuses them is the root's missing definitions, not the count."""
    symbols = [*SYMBOLS, *(f"S{i}" for i in range(6))]
    with pytest.raises(HistoryStopped, match="no instrument definition"):
        run(catalog, symbols=symbols, feed=DataFeed.SIP)
    assert venue.sent == []


def test_repeated_sip_symbols_count_once_toward_the_bound(venue, catalog):
    assert run(catalog, symbols=["AAPL"] * 9, feed=DataFeed.SIP) == 2


@pytest.mark.parametrize("first,second", [("iex", "sip"), ("sip", "iex")])
def test_sip_and_iex_quotes_never_share_a_root(venue, catalog, first, second):
    assert run(catalog, symbols=["AAPL"], feed=DataFeed(first)) == 2
    sent = len(venue.sent)
    with pytest.raises(HistoryStopped, match=f"holds {first} data and this load is {second}"):
        run(catalog, symbols=["MSFT"], feed=DataFeed(second))
    assert len(venue.sent) == sent
    assert (catalog / FEED_FILE).read_text() == f"{first}\n"
    assert ticks_in(catalog, "MSFT") == []


def test_a_sip_quote_load_into_a_legacy_iex_root_is_refused(venue, catalog):
    """A root holding data with no marker is IEX (`bars_to_catalog.catalog_feed`)."""
    assert run(catalog, symbols=["AAPL"]) == 2
    (catalog / FEED_FILE).unlink()
    with pytest.raises(HistoryStopped, match="holds iex"):
        run(catalog, symbols=["MSFT"], feed=DataFeed.SIP)


def test_the_quote_feed_defaults_to_iex():
    import inspect

    assert inspect.signature(history_load.load_quotes).parameters["feed"].default is DataFeed.IEX


def _forbid_client(monkeypatch):
    def forbidden():
        pytest.fail("a refused CLI reached history_client")

    monkeypatch.setattr(history_load, "history_client", forbidden)


@pytest.mark.parametrize("feed", [None, "iex", "sip"])
def test_cli_quotes_take_feed_iex_by_default_or_sip(venue, catalog, monkeypatch, feed):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    extra = [] if feed is None else ["--feed", feed]
    assert history_load.main(cli(catalog, "--symbols", "AAPL", *extra)) == 0
    assert {q["feed"][0] for _, q in venue.sent} == {feed or "iex"}
    assert (catalog / FEED_FILE).read_text() == f"{feed or 'iex'}\n"


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--symbols", ",".join(f"S{i}" for i in range(9))], "SIP_QUOTES_MAX_SYMBOLS"),
        (
            ["--symbols", "AAPL", "--end", (START + timedelta(minutes=61)).isoformat()],
            "SIP_QUOTES_MAX_SPAN",
        ),
        (["--symbols-file", "FILE"], "sip quotes take --symbols"),
    ],
    ids=["nine-symbols", "sixty-one-minutes", "symbols-file"],
)
def test_cli_refuses_sip_quotes_outside_the_bound_before_building_a_client(
    catalog, monkeypatch, capsys, tmp_path, extra, message
):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    _forbid_client(monkeypatch)
    path = tmp_path / "symbols.txt"
    path.write_text("AAPL\n")
    extra = [str(path) if arg == "FILE" else arg for arg in extra]
    with pytest.raises(SystemExit) as stopped:
        history_load.main(cli(catalog, "--feed", "sip", *extra))
    assert stopped.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("data", ["trades", "bars"])
def test_cli_refuses_feed_iex_for_trades_and_bars_by_name(catalog, monkeypatch, capsys, data):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    _forbid_client(monkeypatch)
    argv = cli(catalog, "--symbols", "AAPL", "--feed", "iex")
    argv[argv.index("quotes")] = data
    with pytest.raises(SystemExit) as stopped:
        history_load.main(argv)
    assert stopped.value.code == 2
    assert "--feed iex is for --data quotes only" in capsys.readouterr().err


@pytest.mark.parametrize("data", ["trades", "bars"])
def test_cli_keeps_sip_for_trades_and_bars_when_feed_sip_is_named(catalog, monkeypatch, data):
    """`--feed sip` names what bars and trades already load; the parser lets it through to the client."""
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)

    def reached():
        raise HistoryStopped("offline: the client is not built in this test")

    monkeypatch.setattr(history_load, "history_client", reached)
    argv = cli(catalog, "--symbols", "AAPL", "--feed", "sip", "--bars", "1-MINUTE")
    argv[argv.index("quotes")] = data
    assert history_load.main(argv) == 1


def _route(tmp_path, **usage) -> tuple[subprocess.CompletedProcess, Path]:
    """The task's run script with its process boundaries replaced: no keychain, no uv, no network."""
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    security = bin_ / "security"
    security.write_text('#!/bin/sh\ntouch "$ROUTE_SECURITY"\necho offline-placeholder\n')
    timeout = bin_ / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    sent = tmp_path / "sent"
    uv = bin_ / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ROUTE_SENT"\n')
    for stub in (security, timeout, uv):
        stub.chmod(0o700)
    script = (
        task["run"]
        .replace("/usr/bin/security", str(security))
        .replace("/usr/bin/timeout", str(timeout))
    )
    env = {
        "PATH": f"{bin_}:{os.defpath}",
        "ROUTE_SENT": str(sent),
        "ROUTE_SECURITY": str(tmp_path / "security-called"),
        "usage_account": "tests",
        "usage_symbols": "AAPL",
        "usage_start": START.isoformat(),
        "usage_end": END.isoformat(),
        **usage,
    }
    usage = {k: v for k, v in env.items() if v is not None}
    completed = subprocess.run(
        ["bash", "-c", script], env=usage, capture_output=True, text=True, check=False
    )
    return completed, sent


@pytest.mark.parametrize("feed", ["sip", "iex"])
def test_mise_load_mode_passes_the_quote_feed_to_the_module(tmp_path, feed):
    completed, sent = _route(tmp_path, usage_data="quotes", usage_feed=feed)
    assert completed.returncode == 0, completed.stderr
    args = sent.read_text().splitlines()
    assert args[args.index("--feed") + 1] == feed
    assert args[args.index("--data") + 1] == "quotes"


def test_mise_load_mode_without_feed_passes_none(tmp_path):
    completed, sent = _route(tmp_path, usage_data="quotes")
    assert completed.returncode == 0, completed.stderr
    assert "--feed" not in sent.read_text().splitlines()


def test_mise_help_names_the_sip_quote_bound():
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    assert 'choices "sip" "boats" "iex"' in task["usage"]
    assert "60 minutes" in task["usage"] and "8 symbols" in task["usage"]
    assert "SIP quotes" in task["description"]
    assert f'"$count" -le {history_load.SIP_QUOTES_MAX_SYMBOLS} ]' in task["run"]


# Round 1 (code review, high): a SIP quote root of its own, shell refusals before any pair, the bound's count.


def _sip_bars_root(catalog: Path) -> None:
    """A root that holds SIP bars, as `load_history` leaves one: one minute bar and the `sip` marker."""
    bar_type = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
    price = Price.from_str("10.00")
    ts = dt_to_unix_nanos(START)
    ParquetDataCatalog(str(catalog)).write_bars(
        [Bar(bar_type, price, price, price, price, Quantity.from_int(5), ts, ts)], start=ts, end=ts
    )
    (catalog / FEED_FILE).write_text("sip\n")


def test_a_sip_quote_load_into_a_root_with_sip_bars_is_refused_before_any_request(venue, catalog):
    _sip_bars_root(catalog)
    with pytest.raises(HistoryStopped, match=r"holds bars.*sip quotes.*root of their own"):
        run(catalog, feed=DataFeed.SIP)
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


@pytest.mark.parametrize("feed", [DataFeed.SIP, DataFeed.IEX])
@pytest.mark.parametrize("kind", ["bars", "trades"])
def test_a_bars_or_trades_load_into_a_root_with_quotes_is_refused_whatever_the_feed(
    venue, catalog, kind, feed
):
    assert run(catalog, symbols=["AAPL"], feed=feed) == 2
    sent = len(venue.sent)
    with pytest.raises(HistoryStopped, match=r"holds quotes.*no bars or trades"):
        if kind == "bars":
            history_load.load_history(
                object(), ["AAPL"], ["1-MINUTE"], START, END, catalog, now=lambda: NOW
            )
        else:
            history_load.load_trades(object(), ["AAPL"], START, END, catalog, now=lambda: NOW)
    assert len(venue.sent) == sent


def test_the_default_sip_quote_root_is_catalog_sip_quotes_and_iex_keeps_catalog(monkeypatch):
    assert history_load.SIP_QUOTES_CATALOG == DEFAULT_CATALOG / "sip-quotes"
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    monkeypatch.setattr(history_load, "history_client", object)
    roots = []

    def load_quotes(client, symbols, start, end, catalog_path, **kwargs):
        roots.append((kwargs["feed"], catalog_path))
        return 0

    monkeypatch.setattr(history_load, "load_quotes", load_quotes)
    argv = ["--data", "quotes", "--symbols", "AAPL", "--start", START.isoformat()]
    argv += ["--end", END.isoformat()]
    assert history_load.main([*argv, "--feed", "sip"]) == 0
    assert history_load.main(argv) == 0
    assert roots == [
        (DataFeed.SIP, DEFAULT_CATALOG / "sip-quotes"),
        (DataFeed.IEX, DEFAULT_CATALOG),
    ]


@pytest.mark.parametrize("feed,root", [("sip", "catalog/sip-quotes"), ("iex", "catalog")])
def test_mise_defaults_the_quote_root_by_feed(tmp_path, feed, root):
    completed, sent = _route(tmp_path, usage_data="quotes", usage_feed=feed)
    assert completed.returncode == 0, completed.stderr
    args = sent.read_text().splitlines()
    assert args[args.index("--catalog") + 1] == root


@pytest.mark.parametrize(
    "usage,message",
    [
        ({"usage_data": "trades", "usage_feed": "iex"}, "--feed iex is for --data quotes only"),
        ({"usage_data": "bars", "usage_feed": "iex"}, "--feed iex is for --data quotes only"),
        ({"usage_feed": "iex"}, "--feed iex is for --data quotes only"),
        (
            {"usage_data": "quotes", "usage_feed": "sip", "usage_symbols": None},
            "sip quotes take --symbols",
        ),
        (
            {
                "usage_data": "quotes",
                "usage_feed": "sip",
                "usage_symbols": None,
                "usage_symbols_file": "/symbols.txt",
            },
            "sip quotes take --symbols",
        ),
        (
            {"usage_data": "quotes", "usage_feed": "sip", "usage_symbols": "A,B,C,D,E,F,G,H,I"},
            "at most 8 symbols",
        ),
    ],
    ids=["iex-trades", "iex-bars", "iex-default-bars", "sip-no-symbols", "sip-file", "sip-nine"],
)
def test_mise_load_mode_refuses_a_feed_misuse_before_any_pair_is_read(tmp_path, usage, message):
    completed, sent = _route(tmp_path, **usage)
    assert completed.returncode == 3
    assert message in completed.stderr and "nothing was read" in completed.stderr
    assert not (tmp_path / "security-called").exists()
    assert not sent.exists()


def test_mise_counts_sip_symbols_after_strip_and_upper_case(tmp_path):
    nine_strings = "AAPL,aapl, MSFT,S0,S1,S2,S3,S4,S5"
    completed, sent = _route(
        tmp_path, usage_data="quotes", usage_feed="sip", usage_symbols=nine_strings
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "security-called").exists()
    args = sent.read_text().splitlines()
    assert args[args.index("--symbols") + 1] == nine_strings


@pytest.mark.parametrize(
    "usage",
    [
        {
            "usage_mode": "baseline",
            "usage_session": "2026-11-12",
            "usage_start": None,
            "usage_end": None,
            "usage_symbols": None,
        },
        {"usage_mode": "conditions", "usage_start": None, "usage_end": None, "usage_symbols": None},
    ],
    ids=["baseline", "conditions"],
)
def test_feed_iex_is_refused_outside_load_mode_before_any_pair_is_read(tmp_path, usage):
    completed, sent = _route(tmp_path, usage_feed="iex", **usage)
    assert completed.returncode == 3
    assert "iex" in completed.stderr and "nothing was read" in completed.stderr
    assert not (tmp_path / "security-called").exists()
    assert not sent.exists()


def test_the_sip_bound_counts_distinct_symbols_after_strip_and_upper_case(venue, catalog):
    """Nine strings that are eight symbols pass the bound; what stops them is the missing definitions."""
    symbols = ["AAPL", "aapl", " MSFT", *(f"S{i}" for i in range(6))]
    with pytest.raises(HistoryStopped, match="no instrument definition"):
        run(catalog, symbols=symbols, feed=DataFeed.SIP)
    assert venue.sent == []


def test_a_blank_symbol_gets_the_empty_symbol_message_before_the_sip_bound(venue, catalog):
    with pytest.raises(HistoryStopped, match="nonempty symbol"):
        run(catalog, symbols=["", *(f"S{i}" for i in range(9))], feed=DataFeed.SIP)
    assert venue.sent == []


@pytest.mark.parametrize("data", ["bars", "trades"])
def test_a_bars_or_trades_run_builds_no_feed_value(catalog, monkeypatch, data):
    """The quote feed exists only on the quotes path: bars and trades load sip, never an iex value."""
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    built = []

    class Guard:
        SIP = DataFeed.SIP
        IEX = DataFeed.IEX

        def __new__(cls, value):
            built.append(value)
            return DataFeed(value)

    def reached():
        raise HistoryStopped("offline: the client is not built in this test")

    monkeypatch.setattr(history_load, "DataFeed", Guard)
    monkeypatch.setattr(history_load, "history_client", reached)
    argv = cli(catalog, "--symbols", "AAPL", "--bars", "1-MINUTE")
    argv[argv.index("quotes")] = data
    assert history_load.main(argv) == 1
    assert built == []


@pytest.mark.parametrize(
    "options",
    [{"end": START + timedelta(hours=2)}, {"symbols": [f"S{i}" for i in range(9)]}],
    ids=["span", "symbols"],
)
def test_a_sip_bound_refusal_cites_the_amended_record(venue, catalog, options):
    with pytest.raises(HistoryStopped, match="ADR 0151"):
        run(catalog, feed=DataFeed.SIP, **options)
