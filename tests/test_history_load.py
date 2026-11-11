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
import tomllib
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
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
        t = start
        while t <= end:
            rows.append((symbol, t))
            t += step
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
        out=lambda line: None,
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


def test_a_symbol_with_no_instrument_in_the_catalog_is_refused_before_any_request(
    venue, catalog
) -> None:
    with pytest.raises(HistoryStopped, match="NOPE"):
        run(catalog, symbols=["AAPL", "NOPE"])

    assert venue.sent == []


def test_a_root_holding_another_feed_is_refused_before_any_request(venue, catalog) -> None:
    (catalog / FEED_FILE).write_text("iex\n")

    with pytest.raises(HistoryStopped, match="iex"):
        run(catalog)

    assert venue.sent == []


def test_outside_data_only_mode_no_client_is_built(monkeypatch) -> None:
    monkeypatch.delenv(DATA_ONLY_ENV, raising=False)
    with pytest.raises(HistoryStopped, match="data-only"):
        history_client()


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
                "trades",
            ],
            "_request_trades",
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
