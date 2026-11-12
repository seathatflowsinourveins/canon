"""`canon_trading.history_load`, the module `mise run paper:history` runs (ADR 0093), offline.

A real alpaca-py `StockHistoricalDataClient` with the adapter's timeout adapter and request counter, and
requests' transport replaced by a stand-in for Alpaca's bars endpoint that answers here (the screener
tests' pattern), so nothing reaches Alpaca and the key pair is a placeholder. The stand-in answers as
https://docs.alpaca.markets/us/reference/stockbars.md documents: bars whose open lies in the inclusive
`[start, end]`, sorted by symbol then time, in pages that carry `next_page_token` ("The API may return
less, even if there are more available data points"), and a 429 or 403 with Alpaca's JSON error body.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests
from canon_alpaca import providers
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import parse_equity
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog
from requests.adapters import HTTPAdapter

from canon_trading import history_load
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.history_load import PACE
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import RateLimited
from canon_trading.history_load import history_client
from canon_trading.history_load import load_history
from canon_trading.session_window import SOURCE_ROOT

STANDINS_PATH = SOURCE_ROOT / "packages" / "canon-alpaca" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("canon_alpaca_standins_history", STANDINS_PATH)
assert _spec is not None and _spec.loader is not None
standins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standins)

PLACEHOLDER = "offline-placeholder"
NOW = datetime(2026, 11, 11, 18, tzinfo=UTC)
MIN_START = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
MIN_END = MIN_START + timedelta(minutes=10)
DAY_START = datetime(2026, 11, 3, 5, tzinfo=UTC)
DAY_END = datetime(2026, 11, 7, 5, tzinfo=UTC)
SYMBOLS = ["AAPL", "MSFT", "SPY"]
STEPS = {"1Min": timedelta(minutes=1), "1Day": timedelta(days=1)}


@dataclass
class Venue:
    """The stand-in bars endpoint: what was sent, and how it answers."""

    page_rows: int = 10_000
    answers: list[int] = field(default_factory=list)  # statuses to answer, in order, before 200s
    sent: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)
    opens: list[datetime] | None = None
    rows_per_call: list[int] = field(default_factory=list)

    def bars_sent(self) -> list[dict[str, list[str]]]:
        return [q for _, url, q in self.sent if urlsplit(url).path == "/v2/stocks/bars"]


def utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def answer(venue: Venue, request: requests.PreparedRequest) -> tuple[int, dict]:
    url = urlsplit(request.url or "")
    query = parse_qs(url.query)
    venue.sent.append((request.method or "", request.url or "", query))
    if venue.answers:
        status = venue.answers.pop(0)
        if status == 429:
            return status, {"code": 42910000, "message": "too many requests."}
        if status == 403:
            return status, {"message": "subscription does not permit querying recent SIP data"}
        return status, {"message": "error"}
    if url.hostname != "data.alpaca.markets" or url.path != "/v2/stocks/bars":
        return 404, {"message": "not found"}
    step = STEPS[query["timeframe"][0]]
    start, end = utc(query["start"][0]), utc(query["end"][0])
    rows = []
    for symbol in sorted(query["symbols"][0].split(",")):
        if venue.opens is not None:
            rows.extend((symbol, t) for t in venue.opens if start <= t <= end)
        else:
            t = start
            while t <= end:
                rows.append((symbol, t))
                t += step
    if "page_token" not in query:
        venue.rows_per_call.append(len(rows))
    offset = int(query.get("page_token", ["0"])[0])
    page = rows[offset : offset + venue.page_rows]
    bars: dict[str, list[dict]] = {}
    for symbol, t in page:
        bar = dict(standins.BAR_AAPL, t=t.isoformat().replace("+00:00", "Z"))
        bars.setdefault(symbol, []).append(bar)
    after = offset + len(page)
    token = str(after) if after < len(rows) else None
    return 200, {"bars": bars, "next_page_token": token}


@pytest.fixture
def venue(monkeypatch) -> Venue:
    """Data-only mode with placeholder keys; every request reaching requests' transport is answered by
    the stand-in."""
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    # A counter of this test's own: the process's one counter is what a later test's window reads for
    # its request budget, and a 429 the stand-in answers here is not that window's (ADR 0076's check).
    monkeypatch.setattr(providers, "REQUESTS", providers.RequestCounter())
    stand_in = Venue()

    def send(self, request, **kwargs):
        status, body = answer(stand_in, request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return stand_in


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    """A catalog root holding the definitions of `SYMBOLS`, as the asset list load writes them."""
    root = tmp_path / "catalog"
    root.mkdir()
    ParquetDataCatalog(str(root)).write_instruments(
        [parse_equity(standins.asset(symbol=s), 0) for s in SYMBOLS]
    )
    return root


@dataclass
class Clock:
    """A monotonic clock that moves only when the run sleeps or a call takes `per_call` seconds."""

    now: float = 1_000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs


def run(catalog: Path, symbols=SYMBOLS, specs=("1-MINUTE",), start=MIN_START, end=MIN_END, **kw):
    clock = kw.pop("clock", Clock())
    return load_history(
        history_client(),
        symbols,
        specs,
        start,
        end,
        catalog,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
        out=kw.pop("out", lambda line: None),
        **kw,
    )


def bars_in(catalog: Path, symbol: str, spec: str) -> list:
    return ParquetDataCatalog(str(catalog)).query_bars([f"{symbol}.ALPACA-{spec}-LAST-EXTERNAL"])


def test_many_symbols_go_in_one_call_on_sip_and_land_in_the_catalog(venue, catalog) -> None:
    run(catalog)

    sent = venue.bars_sent()
    assert [q["timeframe"] for q in sent] == [["1Min"]]
    [q] = sent
    assert q["symbols"] == [",".join(SYMBOLS)]
    assert q["feed"] == ["sip"]
    assert all(method == "GET" for method, _, _ in venue.sent)
    assert {urlsplit(url).hostname for _, url, _ in venue.sent} == {"data.alpaca.markets"}
    for symbol in SYMBOLS:
        bars = bars_in(catalog, symbol, "1-MINUTE")
        # ts_init is the bar's close, as the adapter stamps it, and lies in [start, end]
        assert [b.ts_init for b in bars] == [
            int((MIN_START + timedelta(minutes=i)).timestamp() * 1e9) for i in range(11)
        ]
    assert (catalog / FEED_FILE).read_text() == "sip\n"


def test_day_bars_are_asked_for_on_their_own_timeframe(venue, catalog) -> None:
    run(catalog, specs=("1-DAY",), start=DAY_START, end=DAY_END)

    [q] = venue.bars_sent()
    assert q["timeframe"] == ["1Day"] and q["feed"] == ["sip"]
    assert len(bars_in(catalog, "SPY", "1-DAY")) == 5


def test_every_page_is_followed_and_the_pace_follows_the_gets_each_call_sent(
    venue, catalog
) -> None:
    venue.page_rows = 4  # 11 bars a symbol: 3 pages for one symbol, 9 for three
    clock = Clock()
    run(catalog, symbols_per_call=1, clock=clock)

    sent = venue.bars_sent()
    assert [q["symbols"] for q in sent if "page_token" not in q] == [[s] for s in SYMBOLS]
    assert len(sent) == 9
    # three GETs a call at PACE.data_per_minute: the next call waits until they fit in a minute's share
    gap = 3 * 60 / PACE.data_per_minute
    assert clock.slept == [pytest.approx(gap), pytest.approx(gap)]
    assert all(len(bars_in(catalog, s, "1-MINUTE")) == 11 for s in SYMBOLS)


def test_a_429_stops_the_run_at_once_by_name_and_alpaca_py_does_not_send_it_again(
    venue, catalog
) -> None:
    run(catalog, symbols=["AAPL"])
    venue.sent.clear()
    venue.answers = [429]

    with pytest.raises(RateLimited, match="429 Too Many Requests"):
        run(catalog, symbols=["MSFT", "SPY"], symbols_per_call=1)

    assert len(venue.sent) == 1  # no retry by alpaca-py, and no next call
    assert len(bars_in(catalog, "AAPL", "1-MINUTE")) == 11  # what an earlier run wrote stays


def test_a_refused_sip_request_fails_by_name_and_nothing_asks_iex(venue, catalog) -> None:
    venue.answers = [403]

    with pytest.raises(HistoryStopped, match=r"feed sip.*403"):
        run(catalog)

    assert [q["feed"] for q in venue.bars_sent()] == [["sip"]]
    assert not (catalog / FEED_FILE).exists()


def test_a_covered_range_is_not_asked_for_again(venue, catalog) -> None:
    run(catalog)
    venue.sent.clear()

    run(catalog)

    assert venue.sent == []


def test_an_overlapping_extension_writes_each_bar_once_within_its_file_bounds(
    venue, catalog
) -> None:
    run(catalog, symbols=["AAPL"])

    written = run(catalog, symbols=["AAPL"], end=MIN_END + timedelta(minutes=10))

    bars = bars_in(catalog, "AAPL", "1-MINUTE")
    assert written == 10
    assert len(bars) == len({b.ts_init for b in bars}) == 21
    store = ParquetDataCatalog(str(catalog))
    bar_type = "AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL"
    intervals = store.get_intervals(history_load.NautilusDataType.Bar, bar_type)
    assert len(intervals) == 2
    for lo, hi in intervals:
        files = store.query_files(history_load.NautilusDataType.Bar, [bar_type], lo, hi)
        contents = store.query(
            history_load.NautilusDataType.Bar, files=files, optimize_file_loading=False
        )
        assert contents and all(lo <= b.ts_init <= hi for b in contents)


def test_a_symbol_with_no_instrument_in_the_catalog_is_refused_before_any_request(
    venue, catalog
) -> None:
    with pytest.raises(HistoryStopped, match="NOPE"):
        run(catalog, symbols=["AAPL", "NOPE"])

    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


def test_a_root_holding_another_feed_is_refused_before_any_request(venue, catalog) -> None:
    (catalog / FEED_FILE).write_text("iex\n")

    with pytest.raises(HistoryStopped, match="iex"):
        run(catalog)

    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"start": MIN_START.replace(tzinfo=None)}, "time zone"),
        ({"end": MIN_END.replace(tzinfo=None)}, "time zone"),
        ({"start": MIN_END, "end": MIN_START}, "not before"),
        ({"end": NOW}, "before now"),
        ({"specs": []}, "bar specification"),
        ({"specs": ["1-HOUR"]}, "bar specification"),
        ({"symbols": []}, "symbol"),
    ],
)
def test_invalid_load_inputs_do_not_create_a_catalog_lock(venue, catalog, options, message) -> None:
    with pytest.raises(HistoryStopped, match=message):
        run(catalog, **options)

    assert not (catalog / ".history-load.lock").exists()
    assert venue.sent == []


def test_a_missing_catalog_is_refused_without_creating_it(venue, tmp_path) -> None:
    catalog = tmp_path / "typo" / "catalog"

    with pytest.raises(HistoryStopped, match="existing directory"):
        run(catalog)

    assert not (tmp_path / "typo").exists()
    assert venue.sent == []


def test_a_catalog_path_that_is_a_file_is_refused(venue, tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.write_text("keep me")

    with pytest.raises(HistoryStopped, match="existing directory"):
        run(catalog)

    assert catalog.read_text() == "keep me"
    assert venue.sent == []


@pytest.mark.parametrize("kind", ["root", "descendant", "named"])
def test_direct_load_refuses_ledger_paths_without_creating_them(
    venue, tmp_path, monkeypatch, kind
) -> None:
    ledger = tmp_path / "trial-store"
    monkeypatch.setattr(history_load, "LEDGER", ledger)
    catalog = {
        "root": ledger,
        "descendant": ledger / "history",
        "named": tmp_path / "ledger" / "history",
    }[kind]

    with pytest.raises(HistoryStopped, match="trial ledger"):
        run(catalog)

    assert not ledger.exists() and not (tmp_path / "ledger").exists()
    assert venue.sent == []


def test_another_history_load_cannot_enter_the_same_catalog_during_a_request(
    venue, catalog, monkeypatch
) -> None:
    original = history_load.StockHistoricalDataClient.get_stock_bars

    def concurrent(client, request):
        with monkeypatch.context() as other:
            other.setattr(history_load.StockHistoricalDataClient, "get_stock_bars", original)
            with pytest.raises(HistoryStopped, match="another history load.*catalog"):
                run(catalog, symbols=["MSFT"])
        return original(client, request)

    monkeypatch.setattr(history_load.StockHistoricalDataClient, "get_stock_bars", concurrent)
    assert run(catalog, symbols=["AAPL"]) == 11
    assert len(venue.sent) == 1
    assert bars_in(catalog, "MSFT", "1-MINUTE") == []
    # The lock is released on return; the other symbol can now load.
    monkeypatch.setattr(history_load.StockHistoricalDataClient, "get_stock_bars", original)
    assert run(catalog, symbols=["MSFT"]) == 11


def test_a_feed_changed_while_the_request_runs_is_refused_before_writing(
    venue, catalog, monkeypatch
) -> None:
    original = history_load.StockHistoricalDataClient.get_stock_bars

    def changed(client, request):
        result = original(client, request)
        (catalog / FEED_FILE).write_text("iex\n")
        return result

    monkeypatch.setattr(history_load.StockHistoricalDataClient, "get_stock_bars", changed)
    with pytest.raises(HistoryStopped, match="iex"):
        run(catalog, symbols=["AAPL"])

    assert bars_in(catalog, "AAPL", "1-MINUTE") == []
    assert (catalog / FEED_FILE).read_text() == "iex\n"


def test_a_parse_failure_names_the_symbol_and_preserves_the_cause(
    venue, catalog, monkeypatch
) -> None:
    monkeypatch.setitem(standins.BAR_AAPL, "v", 1.5)

    with pytest.raises(HistoryStopped, match=r"AAPL.*volume 1.5 is fractional") as stopped:
        run(catalog, symbols=["AAPL"])

    assert isinstance(stopped.value.__cause__, ValueError)
    assert bars_in(catalog, "AAPL", "1-MINUTE") == []


@pytest.mark.parametrize("state", ["empty", "preclaimed", "partial", "changed"])
def test_a_write_failure_names_the_bar_type_and_preserves_the_cause(
    venue, catalog, monkeypatch, state
) -> None:
    original = history_load.ParquetDataCatalog
    if state == "preclaimed":
        (catalog / FEED_FILE).write_text("sip\n")

    class WriteFails:
        def __init__(self, path):
            self.catalog = original(path)

        def __getattr__(self, name):
            return getattr(self.catalog, name)

        def write_bars(self, *args, **kwargs):
            if state == "partial":
                self.catalog.write_bars(*args, **kwargs)
            elif state == "changed":
                (catalog / FEED_FILE).write_text("iex\n")
            raise OSError("offline disk full")

    monkeypatch.setattr(history_load, "ParquetDataCatalog", WriteFails)
    with pytest.raises(HistoryStopped, match=r"AAPL.*offline disk full") as stopped:
        run(catalog, symbols=["AAPL"])

    assert isinstance(stopped.value.__cause__, OSError)
    assert len(bars_in(catalog, "AAPL", "1-MINUTE")) == (11 if state == "partial" else 0)
    if state == "empty":
        assert not (catalog / FEED_FILE).exists()
    else:
        assert (catalog / FEED_FILE).read_text() == ("iex\n" if state == "changed" else "sip\n")


def test_a_transport_failure_names_the_request_and_preserves_the_cause(
    venue, catalog, monkeypatch
) -> None:
    def timeout(*args, **kwargs):
        raise requests.Timeout("offline connection stalled")

    monkeypatch.setattr(HTTPAdapter, "send", timeout)
    with pytest.raises(HistoryStopped, match=r"AAPL.*offline connection stalled") as stopped:
        run(catalog, symbols=["AAPL"])

    assert isinstance(stopped.value.__cause__, requests.Timeout)


def test_main_reports_a_parse_failure_by_name(venue, catalog, monkeypatch, capsys) -> None:
    monkeypatch.setitem(standins.BAR_AAPL, "v", 1.5)

    result = history_load.main(
        [
            "--symbols",
            "AAPL",
            "--bars",
            "1-MINUTE",
            "--start",
            MIN_START.isoformat(),
            "--end",
            MIN_END.isoformat(),
            "--catalog",
            str(catalog),
        ]
    )

    assert result == 1
    assert "AAPL" in capsys.readouterr().err


def test_sip_rounding_names_the_count_and_first_price(venue, catalog, monkeypatch) -> None:
    monkeypatch.setitem(standins.BAR_AAPL, "h", 178.263488)
    lines: list[str] = []

    assert run(catalog, symbols=["AAPL"], out=lines.append) == 11

    warnings = [line for line in lines if "Rounded" in line]
    assert len(warnings) == 1
    [warning] = warnings
    assert "11 of 11" in warning
    assert "AAPL" in warning and "feed sip" in warning
    assert "price precision 4" in warning and "half to even" in warning
    assert "high 178.263488 to 178.2635" in warning
    assert str(bars_in(catalog, "AAPL", "1-MINUTE")[0].high) == "178.2635"


def test_outside_data_only_mode_no_client_is_built(monkeypatch) -> None:
    monkeypatch.delenv(DATA_ONLY_ENV, raising=False)
    with pytest.raises(HistoryStopped, match="data-only"):
        history_client()


@pytest.mark.parametrize(
    ("stage", "cause"),
    [("credentials", RuntimeError), ("timeout", TypeError), ("get-only", MethodRefused)],
)
def test_client_setup_failures_stop_by_name(venue, monkeypatch, stage, cause) -> None:
    if stage == "credentials":
        monkeypatch.delenv(ENV_API_KEY)
        monkeypatch.delenv(ENV_SECRET_KEY)
    else:

        def refused(*args, **kwargs):
            raise cause(f"offline {stage} validation failed")

        name = "with_http_timeout" if stage == "timeout" else "require_get_only"
        monkeypatch.setattr(history_load, name, refused)

    with pytest.raises(HistoryStopped, match="history client") as stopped:
        history_client()

    assert isinstance(stopped.value.__cause__, cause)
    assert venue.sent == []


def test_main_names_missing_credentials_and_returns_one(
    venue, catalog, monkeypatch, capsys
) -> None:
    monkeypatch.delenv(ENV_API_KEY)
    monkeypatch.delenv(ENV_SECRET_KEY)

    result = history_load.main(
        [
            "--symbols",
            "AAPL",
            "--bars",
            "1-MINUTE",
            "--start",
            MIN_START.isoformat(),
            "--end",
            MIN_END.isoformat(),
            "--catalog",
            str(catalog),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "history client" in error and "credentials" in error
    assert not (catalog / ".history-load.lock").exists()
    assert venue.sent == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--symbols", "AAPL", "--start", "2026-11-10T14:30:00+00:00"], "--end"),
        (["--symbols", "AAPL", "--end", "2026-11-10T14:30:00+00:00"], "--start"),
        (
            ["--symbols", "AAPL", "--start", "2026-11-10T14:30:00", "--end", "2026-11-10T15:00:00"],
            "time zone",
        ),
        (
            [
                "--symbols",
                "AAPL",
                "--start",
                "2026-11-10T15:00:00Z",
                "--end",
                "2026-11-10T14:00:00Z",
            ],
            "not before",
        ),
        (
            [
                "--symbols",
                "AAPL",
                "--start",
                "2026-11-11T12:00:00Z",
                "--end",
                "2026-11-11T12:30:00Z",
            ],
            "1-DAY",
        ),
        (
            [
                "--symbols",
                "AAPL",
                "--symbols-file",
                "x",
                "--start",
                "2026-11-10T14:00:00Z",
                "--end",
                "2026-11-10T15:00:00Z",
            ],
            "not allowed with",
        ),
        (["--start", "2026-11-10T14:00:00Z", "--end", "2026-11-10T15:00:00Z"], "--symbols"),
        (
            [
                "--symbols",
                "AAPL",
                "--start",
                "2026-11-10T14:00:00Z",
                "--end",
                "2026-11-10T15:00:00Z",
                "--catalog",
                "ledger/x",
            ],
            "ledger",
        ),
        (
            [
                "--symbols",
                "AAPL",
                "--start",
                "2026-11-10T14:00:00Z",
                "--end",
                "2026-11-10T15:00:00Z",
                "--data",
                "news",
            ],
            "invalid choice",
        ),
    ],
)
def test_the_arguments_refuse_an_open_or_unplaced_range_and_what_is_not_built(
    argv, message, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    with pytest.raises(SystemExit) as exit_:
        history_load.main(argv)
    assert exit_.value.code == 2
    assert message in capsys.readouterr().err


def test_the_pace_is_the_records_row() -> None:
    """PACE cites ADR 0093's table; the two agree, and the row says it is not yet calibrated."""
    [record] = (SOURCE_ROOT / "docs" / "adr").glob("0093-*.md")
    row = re.search(
        r"^ *\| `paper:history` \| (\d+) \| (\d+) \| UNCALIBRATED", record.read_text(), re.MULTILINE
    )
    assert row is not None
    assert (int(row.group(1)), int(row.group(2))) == (PACE.data_per_minute, PACE.trading_per_minute)


def test_paper_history_reads_one_named_pair_takes_no_lock_and_runs_data_only() -> None:
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    run_ = task["run"]
    assert re.search(r"^\s*tests\) svc=canon-alpaca-paper ;;$", run_, re.MULTILINE)
    assert re.search(r"^\s*windows\) svc=canon-alpaca-paper-windows ;;$", run_, re.MULTILINE)
    assert "lockf" not in run_ and ".lock" not in run_
    assert "CANON_ALPACA_DATA_ONLY=1" in run_
    assert "python -m canon_trading.history_load" in run_
    assert '"tests" "windows"' in task["usage"]
    assert '"bars" "trades"' in task["usage"]


def test_bar_types_are_the_adapters() -> None:
    """The two bar specifications are ones the adapter serves (`bar_timeframe` refuses others)."""
    from canon_alpaca.data import bar_timeframe

    for spec in history_load.BAR_SPECS.values():
        bar_timeframe(BarType.from_str(f"AAPL.ALPACA-{spec}"))


def test_a_request_is_bounded_by_the_most_rows_its_symbols_could_have(venue, catalog) -> None:
    """`MAX_ROWS_PER_CALL` bounds every request: symbols are batched by the most bars one could have in the
    window, and a window longer than the bound for one symbol is cut into segments, each written over its
    own bounds."""
    run(catalog, max_rows=25)  # 11 bars a symbol at most: two symbols a request, then one

    assert [q["symbols"] for q in venue.bars_sent()] == [["AAPL,MSFT"], ["SPY"]]
    venue.sent.clear()

    run(
        catalog,
        symbols=["AAPL"],
        start=MIN_START - timedelta(minutes=30),
        end=MIN_START - timedelta(minutes=1),
        max_rows=10,
    )

    starts = [utc(q["start"][0]) for q in venue.bars_sent()]
    assert len(starts) == 3  # 30 possible bars, at most 10 a request
    assert len(bars_in(catalog, "AAPL", "1-MINUTE")) == 30 + 11


@pytest.mark.parametrize("max_rows", [10, 20])
def test_daily_requests_stay_bounded_across_spring_dst(venue, catalog, max_rows) -> None:
    # Eleven New York midnights span 239 hours; a fixed 24-hour count gives only ten.
    eastern = ZoneInfo("America/New_York")
    venue.opens = [datetime(2026, 3, day, tzinfo=eastern) for day in range(6, 17)]
    start = datetime(2026, 3, 7, 5, tzinfo=UTC)
    end = datetime(2026, 3, 17, 4, tzinfo=UTC)

    assert (
        run(
            catalog,
            symbols=["AAPL", "MSFT"],
            specs=("1-DAY",),
            start=start,
            end=end,
            max_rows=max_rows,
        )
        == 22
    )

    assert max(venue.rows_per_call) <= max_rows
    for symbol in ("AAPL", "MSFT"):
        bars = bars_in(catalog, symbol, "1-DAY")
        assert len(bars) == len({b.ts_init for b in bars}) == 11


def test_a_gap_boundary_returned_again_still_counts_against_the_request_bound(
    venue, catalog
) -> None:
    run(catalog, symbols=["AAPL"])
    venue.rows_per_call.clear()

    assert run(catalog, symbols=["AAPL"], end=MIN_END + timedelta(minutes=10), max_rows=10) == 10

    assert max(venue.rows_per_call) <= 10
    assert len(bars_in(catalog, "AAPL", "1-MINUTE")) == 21


def test_an_overfull_response_stops_before_any_of_its_bars_are_written(
    venue, catalog, monkeypatch
) -> None:
    original = answer

    def overfull(stand_in, request):
        status, body = original(stand_in, request)
        bars = body["bars"]["AAPL"]
        bars[:0] = [bars[0], bars[0]]  # 13 returned, against a planned bound of 12
        return status, body

    monkeypatch.setattr(sys.modules[__name__], "answer", overfull)
    with pytest.raises(HistoryStopped, match=r"13.*12"):
        run(catalog, symbols=["AAPL"], max_rows=12)

    assert bars_in(catalog, "AAPL", "1-MINUTE") == []
    assert not (catalog / FEED_FILE).exists()


def test_an_indented_comment_in_a_symbols_file_is_not_a_symbol(monkeypatch, tmp_path) -> None:
    """Code review, 2026-11-11: a comment line is skipped wherever its `#` starts, so an indented note
    is not loaded as a symbol and does not refuse the whole run."""
    listed = tmp_path / "symbols.txt"
    listed.write_text("  # large caps\nAAPL\n\n", encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(history_load, "history_client", lambda: object())
    monkeypatch.setattr(
        history_load, "load_history", lambda client, symbols, *a, **k: seen.append(symbols) or 0
    )
    assert (
        history_load.main(
            [
                "--symbols-file",
                str(listed),
                "--start",
                "2026-11-10T14:00:00Z",
                "--end",
                "2026-11-10T15:00:00Z",
                "--catalog",
                str(tmp_path / "catalog"),
            ]
        )
        == 0
    )
    assert seen == [["AAPL"]]
