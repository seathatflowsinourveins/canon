"""Historical IEX quotes: real pinned SDK response and catalog, offline HTTP transport.

Contract: alpaca-py get_stock_quotes pages /stocks/quotes into a QuoteSet, then the adapter's
parse_quote and NautilusTrader's write_quote_ticks; ADR 0066's feed, span and size-unit bounds.
"""

from __future__ import annotations

import importlib.util
import json
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
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from requests.adapters import HTTPAdapter

from canon_trading import history_load
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
        ({"feed": DataFeed.SIP}, "iex.*ADR 0066"),
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
