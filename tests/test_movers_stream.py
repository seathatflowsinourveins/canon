"""The movers probe's stream (ADR 0070, its addition of 2026-11-11): data-only, every method but GET refused
with nothing sent, no trading client, under the stream lock, bars then trades, and the ranking score's
arithmetic. Offline: requests' transport is replaced by a recorder and the stream by a stand-in."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
import threading
import tomllib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import alpaca.trading.client
import pytest
import requests
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models.bars import BarSet
from alpaca.trading.client import TradingClient
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import timeout_adapter
from requests.adapters import HTTPAdapter

from canon_trading import movers_stream
from canon_trading.movers_stream import NEW_YORK
from canon_trading.session_window import SOURCE_ROOT
from canon_trading.session_window import START_UP_SECS

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from alpaca.data.models.snapshots import Snapshot

PLACEHOLDER = "offline-placeholder"


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Data-only mode with placeholder keys; every request that reaches requests' transport is recorded
    and answered offline with `{}`."""
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    recorded: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        recorded.append((request.method, request.url))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return recorded


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_a_method_other_than_get_is_refused_with_nothing_sent(sent, method) -> None:
    """The module's client, as `main` builds it: every method but GET refused before requests' transport
    sees it; a GET goes out."""
    client = movers_stream.data_client()
    assert isinstance(client, StockHistoricalDataClient)
    with pytest.raises(MethodRefused, match="nothing was sent"):
        getattr(client, method)("/stocks/snapshots", {})
    assert sent == []
    adapter = timeout_adapter(client)
    assert adapter is not None and adapter.get_only
    client.get("/stocks/snapshots")
    assert [m for m, _ in sent] == ["GET"]


def test_nothing_is_built_outside_data_only_mode(monkeypatch) -> None:
    monkeypatch.setenv(DATA_ONLY_ENV, "0")  # recorded, so teardown restores what was there
    monkeypatch.delenv(DATA_ONLY_ENV)
    for build in (movers_stream.data_client, movers_stream.sip_stream):
        with pytest.raises(movers_stream.DataOnlyRefused, match="nothing was built"):
            build()


def test_main_sets_data_only_builds_no_trading_client_and_streams_sip(
    sent, monkeypatch, tmp_path
) -> None:
    """`main`: data-only set by the module itself, the refusing data client, alpaca-py's stream on `sip`
    with raw messages, and no `TradingClient` built or alive; the run itself is replaced."""

    def refused(self, *args, **kwargs):
        raise AssertionError("a TradingClient was built")

    monkeypatch.setattr(alpaca.trading.client.TradingClient, "__init__", refused)
    monkeypatch.setenv(DATA_ONLY_ENV, "0")
    seen = {}

    def ran(stream, client, minutes, trades_after, log, **kwargs) -> int:
        seen.update(
            stream=stream, client=client, minutes=minutes, trades_after=trades_after, log=log
        )
        return 0

    monkeypatch.setattr(movers_stream, "run", ran)
    assert movers_stream.main(["--minutes", "6", "--log-dir", str(tmp_path)]) == 0
    assert isinstance(seen["stream"], StockDataStream)
    assert seen["stream"]._endpoint.endswith("/v2/sip") and seen["stream"]._raw_data is True
    adapter = timeout_adapter(seen["client"])
    assert adapter is not None and adapter.get_only
    assert seen["minutes"] == 6 and seen["trades_after"] == 3
    assert seen["log"].parent == tmp_path and seen["log"].name.startswith("movers-stream-")
    gc.collect()
    assert not [o for o in gc.get_objects() if isinstance(o, TradingClient)]


@pytest.mark.parametrize(
    "argv",
    [
        ["--minutes", "0", "--log-dir", "/tmp/x"],
        ["--minutes", "61", "--log-dir", "/tmp/x"],
        ["--minutes", "1", "--log-dir", "relative"],
        ["--minutes", "2", "--trades-after", "-1", "--log-dir", "/tmp/x"],
        ["--minutes", "1"],
    ],
)
def test_a_run_is_1_to_60_minutes_into_an_absolute_folder(argv) -> None:
    with pytest.raises(SystemExit):
        movers_stream.main(argv)


def test_paper_data_runs_the_stream_under_the_stream_lock_inside_its_deadline() -> None:
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:data"]
    run = task["run"]
    branch = re.search(r"^  movers-stream\) (.*)$", run, re.MULTILINE)
    assert branch is not None
    assert "module=canon_trading.movers_stream" in branch.group(
        1
    ) and "streams=0" not in branch.group(1)
    assert re.search(r"^streams=1; logs=0$", run, re.MULTILINE)
    bound = {
        k: int(v) for k, v in re.findall(r"^(deadline|sync|lockwait)=(\d+)$", run, re.MULTILINE)
    }
    assert bound["deadline"] >= (
        bound["lockwait"]
        + bound["sync"]
        + START_UP_SECS
        + movers_stream.MAX_MINUTES * 60
        + movers_stream.STOP_SECS
    )


# ---------- the ranking score's arithmetic ----------


def daily(day: str, close: float) -> SimpleNamespace:
    return SimpleNamespace(timestamp=datetime.fromisoformat(f"{day}T05:00:00+00:00"), close=close)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-11-11T09:30:00+00:00", 10.0),  # 04:30 New York: today's bar, if any, is not done
        ("2026-11-11T18:00:00+00:00", 10.0),  # 13:00 New York
        ("2026-11-11T21:30:00+00:00", 12.0),  # 16:30 New York: today's close is the last completed
    ],
)
def test_the_reference_is_the_last_completed_sessions_close(now, expected) -> None:
    snap = SimpleNamespace(
        daily_bar=daily("2026-11-11", 12.0), previous_daily_bar=daily("2026-11-10", 10.0)
    )
    assert (
        movers_stream.reference_close(cast("Snapshot", snap), datetime.fromisoformat(now))
        == expected
    )


def test_before_the_open_a_daily_bar_of_yesterday_is_the_reference() -> None:
    """In pre-market the snapshot's daily bar may still be the last session's; it is the latest done."""
    snap = SimpleNamespace(
        daily_bar=daily("2026-11-10", 11.0), previous_daily_bar=daily("2026-11-09", 9.0)
    )
    now = datetime.fromisoformat("2026-11-11T09:30:00+00:00")
    assert movers_stream.reference_close(cast("Snapshot", snap), now) == 11.0
    empty = SimpleNamespace(daily_bar=None, previous_daily_bar=None)
    assert movers_stream.reference_close(cast("Snapshot", empty), now) is None


def test_the_normal_is_the_mean_of_the_same_minutes_and_zero_is_unknown() -> None:
    sessions = [[10.0, 20.0, 30.0], [30.0, 0.0, 0.0]]
    assert movers_stream.normal(sessions, 2) == (30.0 + 30.0) / 2
    assert movers_stream.normal(sessions, 0) is None
    assert movers_stream.normal([], 2) is None
    assert movers_stream.normal([[0.0, 0.0]], 2) is None


def test_the_rank_is_move_times_dollar_volume_ratio_risers_only_unknowns_left_out_ties_by_symbol() -> (
    None
):
    close = {"AAA": 12.0, "BBB": 15.0, "CCC": 12.0, "DDD": 50.0, "EEE": 12.0, "FFF": 8.0}
    references = {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0, "DDD": None, "EEE": 10.0, "FFF": 10.0}
    dvol = {"AAA": 400.0, "BBB": 100.0, "CCC": 400.0, "DDD": 1e9, "EEE": 1.0, "FFF": 1e9}
    normals = {
        "AAA": [[100.0]],
        "BBB": [[100.0]],
        "CCC": [[100.0]],
        "DDD": [[1.0]],
        "EEE": [[0.0]],
        "FFF": [[1.0]],
    }
    ranked = movers_stream.rank(close, dvol, references, normals, elapsed=1)
    assert [s.symbol for s in ranked] == ["AAA", "CCC", "BBB"]
    assert ranked[0].move == pytest.approx(0.2) and ranked[0].dvol_ratio == pytest.approx(4.0)
    assert ranked[0].score == pytest.approx(0.8) and ranked[2].score == pytest.approx(0.5)
    assert len(movers_stream.rank(close, dvol, references, normals, elapsed=1, top=1)) == 1


def test_normals_are_read_per_prior_weekday_on_the_runs_clock_minutes_skipping_a_holiday() -> None:
    """One minute-bars read per weekday before the run's day, from the run's New York start for its
    minutes, on sip; a weekday with no bar for any symbol is skipped. alpaca-py's request keeps its times
    as naive UTC (`BaseTimeseriesDataRequest`), so they are read back as UTC."""
    start = datetime(2026, 11, 11, 14, 30, tzinfo=UTC)  # 09:30 New York
    requests_seen = []

    class Client:
        def get_stock_snapshot(self, request_params):
            raise AssertionError("no snapshot is read for a normal")

        def get_stock_bars(self, request_params):
            requests_seen.append(request_params)
            day = request_params.start.date()
            if day.isoformat() == "2026-11-06":
                return BarSet({})
            t = request_params.start.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
            bar = {"t": t, "o": 1, "h": 1, "l": 1, "c": 2.0, "v": 5, "n": 1, "vw": 2.0}
            return BarSet({"AAA": [bar]})

    got = movers_stream.read_normals(Client(), ["AAA", "BBB"], start, 3)
    days = [r.start.date().isoformat() for r in requests_seen]
    assert days == [
        "2026-11-10",
        "2026-11-09",
        "2026-11-06",
        "2026-11-05",
        "2026-11-04",
        "2026-11-03",
    ]
    assert all(
        r.start.replace(tzinfo=UTC).astimezone(NEW_YORK).strftime("%H:%M") == "09:30"
        for r in requests_seen
    )
    assert all((r.end - r.start).total_seconds() == 180 for r in requests_seen)
    assert all(r.feed.value == "sip" and str(r.timeframe) == "1Min" for r in requests_seen)
    assert got["AAA"] == [[10.0, 0.0, 0.0]] * movers_stream.NORMAL_SESSIONS
    assert got["BBB"] == [[0.0, 0.0, 0.0]] * movers_stream.NORMAL_SESSIONS


# ---------- a run, against a stand-in stream ----------


class Stream:
    """A stand-in `StockDataStream`: `run` hands `messages` to the bar handler, then blocks until `stop`;
    `subscribe_trades` is recorded by the run's minute."""

    def __init__(
        self, messages: list[dict], *, ends_early: bool = False, error: str | None = None
    ) -> None:
        self.messages = messages
        self.ends_early = ends_early
        self.error = error
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self.delivered = threading.Event()
        self.stopped = threading.Event()

    def subscribe_bars(self, handler, *symbols) -> None:
        assert symbols == ("*",)
        self.handlers["bars"] = handler

    def subscribe_trades(self, handler, *symbols) -> None:
        assert symbols == ("*",)
        self.handlers["trades"] = handler

    def run(self) -> None:
        for msg in self.messages:
            asyncio.run(self.handlers["bars"](msg))
        if "trades" in self.handlers:
            asyncio.run(self.handlers["trades"]({"T": "t", "S": "AAA"}))
        if self.error:
            logging.getLogger(movers_stream.STREAM_LOGGER).error(self.error)
        self.delivered.set()
        if not self.ends_early:
            self.stopped.wait(10)

    def stop(self) -> None:
        self.stopped.set()


class DataClient:
    """A stand-in `StockHistoricalDataClient`: AAA closed at 10, its normal is 100 dollars a minute."""

    def __init__(self) -> None:
        self.snapshots = 0
        self.bars = 0

    def get_stock_snapshot(self, request_params):
        self.snapshots += 1
        prev = SimpleNamespace(timestamp=datetime(2020, 1, 2, 5, tzinfo=UTC), close=10.0)
        return {
            s: SimpleNamespace(daily_bar=None, previous_daily_bar=prev)
            for s in request_params.symbol_or_symbols
        }

    def get_stock_bars(self, request_params):
        self.bars += 1
        t = request_params.start.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        return BarSet(
            {
                s: [{"t": t, "o": 1, "h": 1, "l": 1, "c": 10.0, "v": 10, "n": 1, "vw": 1}]
                for s in request_params.symbol_or_symbols
            }
        )


def fake_time(start: float, stream: Stream):
    now = [start]

    def sleep(secs: float) -> None:
        stream.delivered.wait(5)
        now[0] += secs

    return (lambda: now[0]), sleep


def bar(symbol: str, t: float, close: float, volume: float) -> dict:
    return {"T": "b", "S": symbol, "t": datetime.fromtimestamp(t, UTC), "c": close, "v": volume}


def test_a_run_streams_bars_then_trades_and_logs_rates_memory_and_the_top(tmp_path) -> None:
    start = 1_762_872_000.0  # a minute's start
    stream = Stream([bar("AAA", start, 12.0, 100), bar("BBB", start, 10.0, 1)])
    clock, sleep = fake_time(start, stream)
    log = tmp_path / "s.jsonl"
    client = DataClient()
    status = movers_stream.run(stream, client, 3, 1, log, clock=clock, sleep=sleep)
    assert status == 0
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    minutes, summary = lines[:3], lines[3]
    assert [m["minute"] for m in minutes] == [1, 2, 3]
    assert minutes[0]["messages"]["bars"] == 2 and minutes[0]["trades_subscribed"] is True
    assert minutes[0]["peak_rss_mib"] > 0 and minutes[0]["stream_alive"] is True
    top = minutes[0]["top"]
    assert top[0]["symbol"] == "AAA"
    assert top[0]["move"] == pytest.approx(0.2) and top[0]["dvol_ratio"] == pytest.approx(12.0)
    assert [t["symbol"] for t in top] == ["AAA"]  # BBB did not move
    assert summary["summary"] is True and summary["failures"] == []
    assert client.snapshots == 1 and stream.stopped.is_set()


def test_trades_are_not_subscribed_before_their_minute(tmp_path) -> None:
    start = 1_762_872_000.0
    stream = Stream([])
    clock, sleep = fake_time(start, stream)
    log = tmp_path / "s.jsonl"
    assert movers_stream.run(stream, DataClient(), 2, 5, log, clock=clock, sleep=sleep) == 0
    assert "trades" not in stream.handlers


@pytest.mark.parametrize(
    ("stream", "why"),
    [
        (Stream([], ends_early=True), "stopped before the run's end"),
        (Stream([], error="insufficient subscription (409)"), "logged 1 error"),
    ],
)
def test_a_stream_that_ends_early_or_logs_an_error_fails_the_run(
    tmp_path, stream, why, capsys
) -> None:
    clock, sleep = fake_time(1_762_872_000.0, stream)
    assert (
        movers_stream.run(
            stream, DataClient(), 2, 0, tmp_path / "s.jsonl", clock=clock, sleep=sleep
        )
        == 1
    )
    assert why in capsys.readouterr().err


def test_a_refused_method_ends_the_run_and_stops_the_stream(tmp_path) -> None:
    class Refusing(DataClient):
        def get_stock_snapshot(self, request_params):
            raise MethodRefused("refused")

    stream = Stream([bar("AAA", 1_762_872_000.0, 12.0, 1)])
    clock, sleep = fake_time(1_762_872_000.0, stream)
    with pytest.raises(MethodRefused):
        movers_stream.run(stream, Refusing(), 3, 0, tmp_path / "s.jsonl", clock=clock, sleep=sleep)
    assert stream.stopped.is_set()


def test_the_lookback_is_ten_weekdays() -> None:
    days = list(movers_stream.prior_weekdays(datetime(2026, 11, 11).date()))
    assert len(days) == movers_stream.LOOKBACK_WEEKDAYS and all(d.weekday() < 5 for d in days)
    assert days[0].isoformat() == "2026-11-10" and days[-1].isoformat() == "2026-10-28"


def test_a_failed_batch_leaves_its_symbols_unknown_and_is_not_read_again() -> None:
    """One bad batch cannot hold up the rest, minute after minute; references are as of the run's start."""

    class Failing(DataClient):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[datetime] = []

        def get_stock_snapshot(self, request_params):
            self.snapshots += 1
            raise OSError("down")

        def get_stock_bars(self, request_params):
            self.bars += 1
            raise OSError("down")

    reads = movers_stream.Reads()
    client = Failing()
    start = datetime(2026, 11, 11, 20, 50, tzinfo=UTC)
    close = {"AAA": 12.0}
    for _ in range(2):
        movers_stream.refresh(reads, client, close, start, 30, lambda: 0.0, 1.0)
    assert client.snapshots == 1 and reads.references == {"AAA": None}
    reads.references["AAA"] = 10.0
    for _ in range(2):
        movers_stream.refresh(reads, client, close, start, 30, lambda: 0.0, 1.0)
    assert client.bars == 1 and reads.normals == {"AAA": []}
    assert len(reads.failed) == 2


def test_no_read_starts_at_or_after_its_bound() -> None:
    reads = movers_stream.Reads()
    client = DataClient()
    start = datetime(2026, 11, 11, 20, 50, tzinfo=UTC)
    assert movers_stream.refresh(reads, client, {"AAA": 12.0}, start, 30, lambda: 5.0, 5.0) == 0
    assert client.snapshots == 0 and client.bars == 0 and reads.references == {}
