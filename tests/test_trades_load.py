"""Historical SIP prints into the real pinned catalog, with Alpaca's GET transport stood in.

Contract: Alpaca GET /v2/stocks/{symbol}/trades (inclusive nanosecond bounds, ascending pages and
next_page_token), via the adapter's fetch_trades and parse_trade; no network or real credentials.
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
from canon_alpaca import providers
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import parse_equity
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from requests.adapters import HTTPAdapter

from canon_trading import history_load
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.history_load import PACE
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import RateLimited
from canon_trading.session_window import SOURCE_ROOT

_spec = importlib.util.spec_from_file_location(
    "canon_alpaca_standins_prints", SOURCE_ROOT / "packages/canon-alpaca/tests/conftest.py"
)
assert _spec is not None and _spec.loader is not None
standins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standins)

START = datetime(2026, 11, 10, 21, tzinfo=UTC)
END = datetime(2026, 11, 11, 9, tzinfo=UTC)
NOW = datetime(2026, 11, 11, 18, tzinfo=UTC)
FIRST = "2026-11-10T21:00:00.000000001Z"
LAST = "2026-11-11T09:00:00.000000000Z"
SYMBOLS = ["AAPL", "MSFT"]


def trade(id_: int, timestamp: str, **changes) -> dict:
    return dict(standins.TRADE_AAPL, i=id_, t=timestamp, **changes)


@dataclass
class Venue:
    rows: list[dict] = field(
        default_factory=lambda: [trade(1, FIRST), trade(2, FIRST), trade(3, LAST)]
    )
    page_rows: int = 2
    answers: list[int] = field(default_factory=list)
    sent: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)

    def answer(self, request) -> tuple[int, dict]:
        url = urlsplit(request.url or "")
        query = parse_qs(url.query)
        self.sent.append((request.method, url.path, query))
        if self.answers:
            status = self.answers.pop(0)
            if status != 200:
                return status, {
                    "message": "subscription refused" if status == 403 else f"offline HTTP {status}"
                }
        assert url.hostname == "data.alpaca.markets"
        symbol = url.path.split("/")[-2]
        assert url.path == f"/v2/stocks/{symbol}/trades"
        lo, hi = dt_to_unix_nanos(query["start"][0]), dt_to_unix_nanos(query["end"][0])
        rows = [row for row in self.rows if lo <= dt_to_unix_nanos(row["t"]) <= hi]
        offset = int(query.get("page_token", ["0"])[0])
        page = rows[offset : offset + min(self.page_rows, int(query["limit"][0]))]
        after = offset + len(page)
        return 200, {
            "symbol": symbol,
            "trades": page,
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
def catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    root.mkdir()
    ParquetDataCatalog(str(root)).write_instruments(
        [parse_equity(standins.asset(symbol=s), 0) for s in SYMBOLS]
    )
    return root


@dataclass
class Clock:
    now: float = 1_000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs


def run(catalog: Path, **kwargs) -> int:
    clock = kwargs.pop("clock", Clock())
    return history_load.load_trades(
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


def ticks_in(catalog: Path, symbol="AAPL") -> list:
    return ParquetDataCatalog(str(catalog)).query_trade_ticks([f"{symbol}.ALPACA"])


def cli(catalog: Path, *extra: str) -> list[str]:
    return [
        "--data",
        "trades",
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
        "--catalog",
        str(catalog),
        *extra,
    ]


def test_prints_keep_ids_sizes_and_nanoseconds_through_the_trade_tick_writer(
    venue, catalog
) -> None:
    clock = Clock()
    assert run(catalog, clock=clock) == 6

    assert [path for _, path, _ in venue.sent] == [
        "/v2/stocks/AAPL/trades",
        "/v2/stocks/AAPL/trades",
        "/v2/stocks/MSFT/trades",
        "/v2/stocks/MSFT/trades",
    ]
    assert all(
        method == "GET" and q["feed"] == ["sip"] and q["sort"] == ["asc"]
        for method, _, q in venue.sent
    )
    assert clock.slept == [pytest.approx(60 / PACE.data_per_minute)] * 3
    for symbol in SYMBOLS:
        ticks = ticks_in(catalog, symbol)
        assert [str(t.trade_id) for t in ticks] == ["1", "2", "3"]
        assert [t.ts_init for t in ticks] == [dt_to_unix_nanos(FIRST)] * 2 + [
            dt_to_unix_nanos(LAST)
        ]
        assert all(t.ts_init == t.ts_event and str(t.size) == "1" for t in ticks)
        assert all(str(t.price) == "126.5500" for t in ticks)
        assert ParquetDataCatalog(str(catalog)).get_intervals(
            NautilusDataType.TradeTick, f"{symbol}.ALPACA"
        ) == [(dt_to_unix_nanos(START), dt_to_unix_nanos(END))]
    assert (catalog / FEED_FILE).read_text() == "sip\n"


def test_a_rerun_skips_covered_prints_and_an_extension_preserves_same_timestamp_prints(
    venue, catalog
) -> None:
    assert run(catalog, symbols=["AAPL"]) == 3
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"]) == 0
    assert venue.sent == []
    venue.rows.extend(
        [trade(4, "2026-11-11T09:00:00.000000001Z"), trade(5, "2026-11-11T09:00:00.000000001Z")]
    )
    assert run(catalog, symbols=["AAPL"], end=END + timedelta(minutes=1)) == 2
    assert venue.sent[0][2]["start"] == ["2026-11-11T09:00:00.000000001Z"]
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["1", "2", "3", "4", "5"]


def test_an_empty_answer_does_not_claim_feed_or_coverage(venue, catalog) -> None:
    venue.rows.clear()
    assert run(catalog) == 0
    assert not (catalog / FEED_FILE).exists()
    venue.sent.clear()
    assert run(catalog) == 0
    assert len(venue.sent) == 2


@pytest.mark.parametrize(
    ("status", "exception", "message"),
    [
        (429, RateLimited, "429"),
        (403, HistoryStopped, "HTTP 403"),
    ],
)
def test_a_429_or_refused_feed_stops_without_retry_or_fallback(
    venue, catalog, status, exception, message
) -> None:
    venue.answers = [status]
    with pytest.raises(exception, match=message):
        run(catalog)
    assert len(venue.sent) == 1
    assert venue.sent[0][2]["feed"] == ["sip"]
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


def test_a_later_page_429_leaves_no_incomplete_coverage_and_keeps_earlier_symbols(
    venue, catalog, monkeypatch
) -> None:
    original = venue.answer

    def limited(request):
        if "/MSFT/" in (request.url or "") and "page_token=" in (request.url or ""):
            venue.answers = [429]
        return original(request)

    monkeypatch.setattr(venue, "answer", limited)
    with pytest.raises(RateLimited, match="429"):
        run(catalog)
    assert len(venue.sent) == 4
    assert len(ticks_in(catalog)) == 3
    assert ticks_in(catalog, "MSFT") == []


@pytest.mark.parametrize("retry_wait", [0, 3])
def test_pages_and_504_retries_are_paced_from_each_actual_send(
    venue, catalog, monkeypatch, retry_wait
) -> None:
    venue.page_rows = 1
    venue.rows = [trade(i, FIRST) for i in range(1, 101)]
    # With SDK backoffs of 3 seconds, these seven retrying pages take 63 seconds before the
    # fast pages finish: the old pre-fetch schedule expired and admitted another burst.
    venue.answers = [504, 504, 504, 200] * 7
    clock = Clock()
    monkeypatch.setattr(alpaca_rest, "DEFAULT_RETRY_WAIT_SECONDS", retry_wait)
    monkeypatch.setattr(alpaca_rest.time, "sleep", clock.sleep)
    client = history_load.history_client()
    session = providers.session_of(client)
    assert session is not None
    mounted = dict(session.adapters)
    monkeypatch.setattr(history_load, "history_client", lambda: client)
    sent_at: list[float] = []
    original = venue.answer

    def timed(request):
        sent_at.append(clock())
        return original(request)

    monkeypatch.setattr(venue, "answer", timed)

    assert run(catalog, clock=clock) == 200

    assert len(venue.sent) == len(sent_at) == 221
    gap = 60 / PACE.data_per_minute
    assert all(b - a >= gap - 1e-8 for a, b in pairwise(sent_at))
    assert all(len(ticks_in(catalog, symbol)) == 100 for symbol in SYMBOLS)
    assert session.adapters == mounted


def test_exhausted_504_retries_stop_by_name_without_writing(venue, catalog, monkeypatch) -> None:
    venue.answers = [504] * 4
    clock = Clock()
    monkeypatch.setattr(alpaca_rest, "DEFAULT_RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(alpaca_rest.time, "sleep", clock.sleep)
    client = history_load.history_client()
    session = providers.session_of(client)
    assert session is not None
    mounted = dict(session.adapters)
    monkeypatch.setattr(history_load, "history_client", lambda: client)

    with pytest.raises(HistoryStopped, match=r"AAPL.*HTTP 504"):
        run(catalog, clock=clock)

    assert len(venue.sent) == 4
    assert [s for s in clock.slept if s > 0] == [pytest.approx(60 / PACE.data_per_minute)] * 3
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()
    assert session.adapters == mounted


def test_canceled_and_incorrect_prints_are_named_and_corrected_prints_are_kept(
    venue, catalog
) -> None:
    venue.rows = [
        trade(1, FIRST, u="canceled"),
        trade(2, FIRST, u="incorrect"),
        trade(3, FIRST, u="corrected"),
    ]
    lines: list[str] = []
    assert run(catalog, symbols=["AAPL"], out=lines.append) == 1
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["3"]
    assert any("canceled=1" in line and "incorrect=1" in line for line in lines)


def test_a_size_zero_print_is_skipped_and_named_and_the_load_goes_on(venue, catalog) -> None:
    """An after-hours print of size 0 (seen in a 2026-11-13 load) cannot become a TradeTick at the
    pin (its size must be positive); it is skipped and counted, never fatal to the symbol's load."""
    venue.rows = [trade(1, FIRST, s=0), trade(2, FIRST), trade(3, LAST, s=0)]
    lines: list[str] = []
    assert run(catalog, symbols=["AAPL"], out=lines.append) == 1
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["2"]
    assert any("Skipped 2 size-0" in line and "AAPL" in line for line in lines), lines


def test_a_cut_part_of_only_size_zero_prints_does_not_end_the_interval(venue, catalog) -> None:
    """Review of 37789e60: a part cut at the row bound whose rows before the cut are all size 0
    writes nothing, but the interval still goes on from the cut, and the skip is counted."""
    venue.rows = [trade(1, FIRST, s=0), trade(2, FIRST, s=0), trade(3, LAST)]
    lines: list[str] = []
    skipped: dict[str, int] = {}
    assert run(catalog, symbols=["AAPL"], max_rows=2, out=lines.append, skipped=skipped) == 1
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["3"]
    assert skipped == {"size-0 prints": 2}, (skipped, lines)
    assert any("coverage not recorded" in line for line in lines), lines


def test_size_zero_prints_at_a_cut_are_counted_once_across_parts(venue, catalog) -> None:
    """A cut part with written prints and size-0 prints before and at the cut: the ones at the cut
    are fetched again by the next part and counted there, not twice."""
    venue.rows = [trade(1, FIRST), trade(2, FIRST, s=0), trade(3, LAST, s=0), trade(4, LAST)]
    skipped: dict[str, int] = {}
    assert run(catalog, symbols=["AAPL"], max_rows=3, out=lambda _: None, skipped=skipped) == 2
    assert sorted(str(t.trade_id) for t in ticks_in(catalog)) == ["1", "4"]
    assert skipped == {"size-0 prints": 2}, skipped


def test_a_print_with_another_unusable_size_still_stops_by_name(venue, catalog) -> None:
    """Only size 0 is skipped; a negative size reaches parse_trade and stops the load by name."""
    venue.rows = [trade(1, FIRST, s=-100)]
    with pytest.raises(HistoryStopped, match=r"parsing trades of AAPL"):
        run(catalog, symbols=["AAPL"])


def test_the_done_line_counts_the_size_zero_prints_skipped(venue, catalog, monkeypatch, capsys):
    venue.rows = [trade(1, FIRST, s=0), trade(2, FIRST)]
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    assert history_load.main([*cli(catalog), "--symbols", "AAPL"]) == 0
    out = capsys.readouterr().out
    assert "done, 1 trades written" in out and ", 1 size-0 prints skipped" in out, out


def test_rounding_is_named_once_per_symbol_with_the_first_example(venue, catalog) -> None:
    venue.rows = [trade(1, FIRST, p=126.553488)]
    lines: list[str] = []
    assert run(catalog, symbols=["AAPL"], out=lines.append) == 1
    warnings = [line for line in lines if "Rounded" in line]
    assert len(warnings) == 1
    assert all(
        text in warnings[0]
        for text in (
            "AAPL",
            "feed sip",
            "1 of 1",
            "precision 4",
            "half to even",
            "126.553488",
        )
    )
    assert str(ticks_in(catalog)[0].price) == "126.5535"


def test_an_overfull_interval_is_loaded_in_parts_each_cut_before_its_last_timestamp(
    venue, catalog
) -> None:
    # Code review of the prints loader: an interval over the row bound stopped the whole run, so a
    # liquid symbol's session could not load. Each part now ends one nanosecond before the last
    # print returned, so no timestamp is split across parts and no coverage is claimed past a print.
    catalog_ = ParquetDataCatalog(str(catalog))
    assert run(catalog, symbols=["AAPL"], max_rows=2) == 3
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["1", "2", "3"]
    last = dt_to_unix_nanos(LAST)
    assert catalog_.get_intervals(NautilusDataType.TradeTick, "AAPL.ALPACA") == [
        (dt_to_unix_nanos(START), last - 1),
        (last, dt_to_unix_nanos(END)),
    ]
    starts = [q["start"][0] for _, _, q in venue.sent]
    assert list(dict.fromkeys(starts)) == ["2026-11-10T21:00:00.000000000Z", LAST]
    venue.sent.clear()
    assert run(catalog, symbols=["AAPL"], max_rows=2) == 0  # covered: nothing asked again
    assert venue.sent == []


def test_more_prints_at_one_timestamp_than_the_row_bound_stop_by_name(venue, catalog) -> None:
    with pytest.raises(HistoryStopped, match=r"AAPL.*more than 1 .*one timestamp"):
        run(catalog, symbols=["AAPL"], max_rows=1)
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


def test_prints_answered_out_of_order_are_written_in_time_order(venue, catalog) -> None:
    # Code review of the prints loader: the writer was handed prints in the answer's order.
    venue.rows = [trade(3, LAST), trade(1, FIRST), trade(2, FIRST)]
    venue.page_rows = 10
    assert run(catalog, symbols=["AAPL"]) == 3
    ticks = ticks_in(catalog)
    assert [t.ts_init for t in ticks] == sorted(t.ts_init for t in ticks)


def test_a_padded_symbol_is_read_as_its_name(venue, catalog) -> None:
    # Code review of the prints loader: only the CLI stripped a symbol, so a library caller's
    # " AAPL" was refused as having no instrument definition.
    assert run(catalog, symbols=[" AAPL "]) == 3
    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["1", "2", "3"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"symbols": []}, "symbol"),
        ({"start": START.replace(tzinfo=None)}, "time zone"),
        ({"end": END.replace(tzinfo=None)}, "time zone"),
        ({"end": START}, "not before"),
        ({"end": NOW - timedelta(minutes=14)}, "15"),
        ({"symbols": ["UNKNOWN"]}, "UNKNOWN"),
        ({"max_rows": 0}, "row bound"),
    ],
)
def test_invalid_inputs_are_refused_before_a_lock_or_request(
    venue, catalog, options, message
) -> None:
    with pytest.raises(HistoryStopped, match=message):
        run(catalog, **options)
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


@pytest.mark.parametrize("through_cli", [False, True])
def test_a_pre_epoch_start_is_refused_before_creating_a_lock(
    venue, catalog, monkeypatch, through_cli, capsys
) -> None:
    start = datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC)
    if through_cli:
        monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
        with pytest.raises(SystemExit) as refused:
            history_load.main(cli(catalog, "--symbols", "AAPL", "--start", start.isoformat()))
        assert refused.value.code == 2
        assert "1970" in capsys.readouterr().err
    else:
        with pytest.raises(HistoryStopped):
            run(catalog, start=start)
    assert venue.sent == []
    assert not (catalog / ".history-load.lock").exists()


@pytest.mark.parametrize("kind", ["missing", "ledger", "file", "feed"])
def test_an_invalid_root_is_refused_before_a_request(venue, catalog, tmp_path, kind) -> None:
    if kind == "feed":
        (catalog / FEED_FILE).write_text("iex\n")
        root = catalog
    elif kind == "file":
        root = tmp_path / "file"
        root.touch()
    else:
        root = tmp_path / kind / "catalog"
    with pytest.raises(HistoryStopped):
        run(root)
    assert venue.sent == []
    if kind in ("missing", "ledger"):
        assert not root.exists()


def test_bars_and_prints_use_the_same_catalog_lock(venue, catalog, monkeypatch) -> None:
    original = venue.answer

    def competing(request):
        with pytest.raises(HistoryStopped, match="another history load"):
            history_load.load_history(
                object(), ["AAPL"], ["1-MINUTE"], START, END, catalog, now=lambda: NOW
            )
        return original(request)

    monkeypatch.setattr(venue, "answer", competing)
    assert run(catalog, symbols=["AAPL"]) == 3


def test_a_changed_feed_during_the_request_is_refused_before_writing(
    venue, catalog, monkeypatch
) -> None:
    original = venue.answer

    def changed(request):
        (catalog / FEED_FILE).write_text("iex\n")
        return original(request)

    monkeypatch.setattr(venue, "answer", changed)
    with pytest.raises(HistoryStopped, match="iex"):
        run(catalog)
    assert ticks_in(catalog) == []
    assert (catalog / FEED_FILE).read_text() == "iex\n"


def test_a_parse_failure_names_the_symbol_and_does_not_claim_coverage(venue, catalog) -> None:
    venue.rows = [trade(1, FIRST, s=1.5)]
    with pytest.raises(HistoryStopped, match=r"AAPL.*fractional") as stopped:
        run(catalog)
    assert isinstance(stopped.value.__cause__, ValueError)
    assert ticks_in(catalog) == []
    assert not (catalog / FEED_FILE).exists()


@pytest.mark.parametrize("partial", [False, True])
def test_a_write_failure_names_the_symbol_and_cleans_only_an_unused_marker(
    venue, catalog, monkeypatch, partial
) -> None:
    original = history_load.ParquetDataCatalog

    class WriteFails:
        def __init__(self, path):
            self.catalog = original(path)

        def __getattr__(self, name):
            return getattr(self.catalog, name)

        def write_trade_ticks(self, *args, **kwargs):
            if partial:
                self.catalog.write_trade_ticks(*args, **kwargs)
            raise OSError("offline disk full")

    monkeypatch.setattr(history_load, "ParquetDataCatalog", WriteFails)
    with pytest.raises(HistoryStopped, match=r"AAPL.*offline disk full") as stopped:
        run(catalog)
    assert isinstance(stopped.value.__cause__, OSError)
    assert (catalog / FEED_FILE).exists() == partial


def test_a_transport_failure_is_named(venue, catalog, monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise requests.Timeout("offline stalled")

    monkeypatch.setattr(HTTPAdapter, "send", timeout)
    with pytest.raises(HistoryStopped, match=r"AAPL.*offline stalled") as stopped:
        run(catalog)
    assert isinstance(stopped.value.__cause__, requests.Timeout)


@pytest.mark.parametrize("file", [False, True])
def test_cli_loads_prints_from_symbols_or_a_file_without_daily_bar_restrictions(
    venue, catalog, monkeypatch, capsys, tmp_path, file
) -> None:
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    if file:
        symbols = tmp_path / "symbols.txt"
        symbols.write_text("  # one night\nAAPL\nAAPL\n\n")
        extra = ["--symbols-file", str(symbols)]
    else:
        extra = ["--symbols", "AAPL,AAPL"]
    assert history_load.main(cli(catalog, *extra)) == 0
    assert "3 trades written" in capsys.readouterr().out
    assert len(ticks_in(catalog)) == 3


@pytest.mark.parametrize(("status", "exit_code"), [(429, 4), (403, 1)])
def test_cli_reports_print_request_failure(venue, catalog, monkeypatch, capsys, status, exit_code):
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    venue.answers = [status]
    assert history_load.main(cli(catalog, "--symbols", "AAPL")) == exit_code
    assert str(status) in capsys.readouterr().err


def test_cli_keeps_nanosecond_start_and_end_bounds(venue, catalog, monkeypatch) -> None:
    monkeypatch.setattr(history_load, "utc_now", lambda: NOW)
    venue.rows = [trade(i, f"2026-11-10T21:00:00.00000000{i}Z") for i in range(1, 6)]
    start = "2026-11-10T21:00:00.000000002Z"
    end = "2026-11-10T21:00:00.000000004Z"

    assert history_load.main(cli(catalog, "--symbols", "AAPL", "--start", start, "--end", end)) == 0

    assert [str(t.trade_id) for t in ticks_in(catalog)] == ["2", "3", "4"]
    assert venue.sent[0][2]["start"] == [start]
    assert venue.sent[0][2]["end"] == [end]


def test_the_reused_client_still_refuses_non_get_requests(venue) -> None:
    client = history_load.history_client()
    with pytest.raises(MethodRefused):
        client.post("/stocks/AAPL/trades", {})
    assert venue.sent == []


def test_the_task_documents_the_implemented_prints_mode() -> None:
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    assert "trades is not built yet" not in task["usage"]
    assert "trades" in task["description"]


BOATS_END = START + timedelta(hours=1)


def test_boats_trades_are_asked_for_on_boats_and_claim_a_boats_root(venue, catalog) -> None:
    """The overnight feed: Alpaca's historical trades take `feed=boats`, "Blue Ocean ATS, overnight US
    trading data" (https://docs.alpaca.markets/us/reference/stocktrades-1.md, read 2026-11-13)."""
    from alpaca.data.enums import DataFeed

    assert run(catalog, end=BOATS_END, feed=DataFeed.BOATS) > 0
    assert {q["feed"][0] for _, _, q in venue.sent} == {"boats"}
    assert (catalog / FEED_FILE).read_text() == "boats\n"


@pytest.mark.parametrize(
    ("status", "exception", "message"),
    [(429, RateLimited, "429"), (403, HistoryStopped, r"feed boats.*HTTP 403")],
)
def test_a_429_or_refused_boats_feed_stops_by_name_and_asks_no_other_feed(
    venue, catalog, status, exception, message
) -> None:
    from alpaca.data.enums import DataFeed

    venue.answers = [status]
    with pytest.raises(exception, match=message):
        run(catalog, end=BOATS_END, feed=DataFeed.BOATS)
    assert [q["feed"] for _, _, q in venue.sent] == [["boats"]]
    assert not (catalog / FEED_FILE).exists()


def test_a_boats_trade_load_into_a_sip_root_is_refused_before_any_request(venue, catalog) -> None:
    from alpaca.data.enums import DataFeed

    assert run(catalog, end=BOATS_END) > 0
    venue.sent.clear()
    with pytest.raises(HistoryStopped, match="holds sip data and this load is boats"):
        run(catalog, end=BOATS_END, feed=DataFeed.BOATS)
    assert venue.sent == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"symbols": [f"S{i}" for i in range(9)]}, "BOATS_MAX_SYMBOLS"),
        ({"end": START + timedelta(hours=8, minutes=1)}, "BOATS_MAX_SPAN"),
    ],
    ids=["nine-symbols", "over-one-overnight-session"],
)
def test_a_boats_trade_load_over_its_bound_is_refused_before_any_request(
    venue, catalog, options, message
) -> None:
    from alpaca.data.enums import DataFeed

    with pytest.raises(HistoryStopped, match=message):
        run(catalog, feed=DataFeed.BOATS, **({"end": BOATS_END} | options))
    assert venue.sent == []
