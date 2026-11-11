"""Offline: `canon_trading.bars_list` reads its list, cuts a window to `load_bars`' margin, and counts by day.

Bars are built the way a paper load gets them, an Alpaca bar payload through `canon_alpaca.data.parse_bar`
(the `alpaca_bars` fixture's instrument), and written to a temporary catalog as `load_bars` writes them.
Nothing here reaches Alpaca.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import canon_alpaca.data
import canon_alpaca.providers
import pytest
from alpaca.data.models import Bar as AlpacaBar
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import NEW_YORK
from canon_alpaca.data import parse_bar
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import bars_list

if TYPE_CHECKING:
    from pathlib import Path

MINUTE = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
DAILY = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")

STANDINS_PATH = bars_list.REPO / "packages" / "canon-alpaca" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("canon_bars_list_standins", STANDINS_PATH)
assert _spec is not None and _spec.loader is not None
standins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standins)
collect_on_main_thread = standins.collect_on_main_thread


def write_list(path: Path, loads: list[dict[str, str]], report: Path, **extra: str) -> Path:
    path.write_text(json.dumps({"report": str(report), "loads": loads, **extra}), encoding="utf-8")
    return path


def test_the_list_is_read_as_new_york_windows_on_sip_by_default(tmp_path: Path) -> None:
    listed = write_list(
        tmp_path / "list.json",
        [
            {
                "symbol": "AAPL",
                "bar": "1-MINUTE",
                "start": "2026-11-02T04:00",
                "end": "2026-11-03T20:00",
            }
        ],
        tmp_path / "report.json",
    )
    feed, report, entries = bars_list.read_list(listed)
    assert feed == "sip"
    assert report == (tmp_path / "report.json").resolve()
    assert entries == [
        (
            MINUTE,
            datetime(2026, 11, 2, 4, tzinfo=NEW_YORK),
            datetime(2026, 11, 3, 20, tzinfo=NEW_YORK),
        )
    ]


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"symbol": "aapl"}, "not a US equity symbol"),
        ({"bar": "5-MINUTE"}, "not one of"),
    ],
)
def test_a_malformed_entry_is_refused(tmp_path: Path, change: dict[str, str], refusal: str) -> None:
    entry = {
        "symbol": "AAPL",
        "bar": "1-MINUTE",
        "start": "2026-11-02T04:00",
        "end": "2026-11-02T20:00",
    }
    listed = write_list(tmp_path / "list.json", [entry | change], tmp_path / "report.json")
    with pytest.raises(ValueError, match=refusal):
        bars_list.read_list(listed)


def test_a_feed_a_bars_request_does_not_take_is_refused(tmp_path: Path) -> None:
    listed = write_list(tmp_path / "list.json", [], tmp_path / "r.json", feed="crypto")
    with pytest.raises(ValueError, match="not one a bars request takes"):
        bars_list.read_list(listed)


def test_a_list_or_report_inside_the_repository_is_refused(tmp_path: Path) -> None:
    inside = bars_list.REPO / "catalog" / "report.json"
    listed = write_list(tmp_path / "list.json", [], inside)
    with pytest.raises(ValueError, match="inside the repository"):
        bars_list.read_list(listed)


def test_an_end_too_near_now_is_cut_to_the_margin_less_a_minute() -> None:
    start = datetime(2026, 11, 11, 4, tzinfo=NEW_YORK)
    end = datetime(2026, 11, 11, 20, tzinfo=NEW_YORK)
    now = datetime(2026, 11, 11, 13, 30, 40, tzinfo=NEW_YORK)
    assert bars_list.cut(MINUTE, start, end, now) == datetime(2026, 11, 11, 13, 14, tzinfo=NEW_YORK)
    # a daily bar keeps one day's margin, so nothing of a window ending today is left
    assert bars_list.cut(DAILY, start, end, now) is None
    # an end already far enough back is kept
    early = datetime(2026, 11, 11, 9, tzinfo=NEW_YORK)
    assert bars_list.cut(MINUTE, start, early, now) == early


def test_a_day_is_in_the_window_when_one_of_its_bars_can_be() -> None:
    start = datetime(2026, 10, 13, 12, tzinfo=NEW_YORK)
    end = datetime(2026, 11, 10, 13, 29, tzinfo=NEW_YORK)
    # a daily bar's ts_init is the midnight after its day
    assert not bars_list.in_window(date(2026, 10, 12), False, start, end)
    assert bars_list.in_window(date(2026, 10, 13), False, start, end)
    assert bars_list.in_window(date(2026, 11, 9), False, start, end)
    assert not bars_list.in_window(date(2026, 11, 10), False, start, end)
    # minute bars: 04:01 to 20:00 New York
    assert bars_list.in_window(date(2026, 11, 10), True, start, end)
    assert not bars_list.in_window(date(2026, 11, 11), True, start, end)


def test_bars_are_counted_by_day_and_session_part_and_an_empty_weekday_is_named(
    tmp_path: Path, alpaca_bars
) -> None:
    instrument = alpaca_bars.equity()
    opens = [
        datetime(2026, 11, 2, 4, 0, tzinfo=NEW_YORK),  # Monday: pre-market
        datetime(2026, 11, 2, 9, 29, tzinfo=NEW_YORK),
        datetime(2026, 11, 2, 9, 30, tzinfo=NEW_YORK),  # regular
        datetime(2026, 11, 2, 16, 0, tzinfo=NEW_YORK),  # post-market
        datetime(2026, 11, 4, 10, 0, tzinfo=NEW_YORK),  # Wednesday; none on Tuesday
    ]
    bars = [
        parse_bar(
            AlpacaBar(
                "AAPL",
                {
                    "t": o.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": 10.0,
                    "h": 10.0,
                    "l": 10.0,
                    "c": 10.0,
                    "v": 100,
                    "n": 1,
                    "vw": 10.0,
                },
            ),
            MINUTE,
            instrument,
        )
        for o in opens
    ]
    start = datetime(2026, 11, 2, 4, tzinfo=NEW_YORK)
    end = datetime(2026, 11, 4, 20, tzinfo=NEW_YORK)
    ParquetDataCatalog(str(tmp_path)).write_bars(
        bars, start=dt_to_unix_nanos(start), end=dt_to_unix_nanos(end)
    )
    counted = bars_list.by_day(tmp_path, MINUTE, start, end)
    assert counted["bars"] == 5
    assert counted["missing_days"] == ["2026-11-03"]
    assert counted["days"]["2026-11-02"] == {
        "bars": 4,
        "pre": 2,
        "regular": 1,
        "post": 1,
        "first_pre": "04:00",
        "last_pre": "09:29",
    }
    assert counted["days"]["2026-11-04"]["first_pre"] is None
    assert timedelta(minutes=1).total_seconds() * 1e9 == bars[0].ts_init - bars[0].ts_event


def test_a_rerun_of_a_covered_window_reports_the_joined_day_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the real loader, node and catalog; only Alpaca's clients use the adapter's stand-ins,
    # as in test_bars_to_catalog.py. A partial load followed by a wider one leaves two data files.
    historical = standins.FakeHistoricalClient(
        standins.bars_opening(
            datetime(2026, 11, 2, 4, tzinfo=NEW_YORK),
            datetime(2026, 11, 2, 9, 29, tzinfo=NEW_YORK),
            datetime(2026, 11, 2, 9, 30, tzinfo=NEW_YORK),
            datetime(2026, 11, 2, 16, tzinfo=NEW_YORK),
            datetime(2026, 11, 4, 4, tzinfo=NEW_YORK),
            datetime(2026, 11, 4, 8, tzinfo=NEW_YORK),
            datetime(2026, 11, 4, 10, tzinfo=NEW_YORK),
        )
    )
    monkeypatch.setenv(ENV_API_KEY, "offline-placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "offline-placeholder")
    monkeypatch.setattr(
        canon_alpaca.providers, "TradingClient", lambda **_: standins.FakeTradingClient()
    )
    monkeypatch.setattr(canon_alpaca.data, "StockHistoricalDataClient", lambda **_: historical)
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: standins.FakeStream())
    monkeypatch.setattr(bars_list, "ROOTS", tmp_path / "catalog")
    report_path = tmp_path / "report.json"
    entry = {
        "symbol": "AAPL",
        "bar": "1-MINUTE",
        "start": "2026-11-02T04:00",
        "end": "2026-11-02T12:00",
    }
    listed = write_list(tmp_path / "list.json", [entry], report_path)
    assert bars_list.run(listed, 1) == 0
    write_list(listed, [entry | {"end": "2026-11-04T20:00"}], report_path)
    assert bars_list.run(listed, 1) == 0
    report_path.unlink()  # the covered rerun must write its own report
    assert bars_list.run(listed, 1) == 0
    assert report_path.is_file()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["loads"] == 1
    assert report["failed"] == report["missing"] == []
    [row] = report["rows"]
    assert row["status"] == "loaded"
    assert row["bars"] == 7
    assert row["missing_days"] == ["2026-11-03"]
    assert row["days"] == {
        "2026-11-02": {
            "bars": 4,
            "pre": 2,
            "regular": 1,
            "post": 1,
            "first_pre": "04:00",
            "last_pre": "09:29",
        },
        "2026-11-04": {
            "bars": 3,
            "pre": 2,
            "regular": 1,
            "post": 0,
            "first_pre": "04:00",
            "last_pre": "08:00",
        },
    }
    assert len(historical.requests) == 2  # the covered rerun makes no third request


ENTRY = {
    "symbol": "AAPL",
    "bar": "1-MINUTE",
    "start": "2026-11-02T04:00",
    "end": "2026-11-02T20:00",
}


@pytest.mark.parametrize("given", ["2026-11-02T09:00Z", "2026-11-02T04:00-05:00"])
def test_a_time_with_its_own_offset_is_refused(tmp_path: Path, given: str) -> None:
    """Times are New York wall clock; one that names an offset is refused rather than overwritten."""
    listed = write_list(tmp_path / "list.json", [ENTRY | {"start": given}], tmp_path / "r.json")
    with pytest.raises(ValueError, match="New York wall clock"):
        bars_list.read_list(listed)


def test_a_relative_report_is_read_beside_the_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(bars_list.REPO)
    listed = tmp_path / "list.json"
    listed.write_text(json.dumps({"report": "out.json", "loads": []}), encoding="utf-8")
    assert bars_list.read_list(listed)[1] == (tmp_path / "out.json").resolve()


def test_a_failed_count_marks_its_row_failed_and_the_rest_still_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(bars_list, "ROOTS", tmp_path / "roots")
    monkeypatch.setattr(bars_list, "LOADS_PER_MINUTE", 10**6)
    monkeypatch.setattr(bars_list, "load_bars", lambda *a, **k: None)
    counts = iter([OSError("damaged parquet"), {"bars": 3}])

    def by_day(*_: object) -> dict[str, int]:
        got = next(counts)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(bars_list, "by_day", by_day)
    report_path = tmp_path / "report.json"
    listed = write_list(tmp_path / "list.json", [ENTRY, ENTRY | {"symbol": "MSFT"}], report_path)
    assert bars_list.run(listed, 1) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [r["status"] for r in report["rows"]] == ["failed", "loaded"]
    assert report["failed"] == ["AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL"]


def test_the_report_is_written_after_each_load_so_a_killed_run_keeps_the_ones_done(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(bars_list, "ROOTS", tmp_path / "roots")
    monkeypatch.setattr(bars_list, "LOADS_PER_MINUTE", 10**6)
    monkeypatch.setattr(bars_list, "by_day", lambda *_: {"bars": 3})
    calls = []

    def load_bars(*_: object, **__: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt  # stands in for the task's kill partway through the list

    monkeypatch.setattr(bars_list, "load_bars", load_bars)
    report_path = tmp_path / "report.json"
    listed = write_list(tmp_path / "list.json", [ENTRY, ENTRY | {"symbol": "MSFT"}], report_path)
    with pytest.raises(KeyboardInterrupt):
        bars_list.run(listed, 1)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["loads"] == 1
    assert report["rows"][0]["status"] == "loaded"


def test_paper_data_runs_the_bars_list_without_the_stream_lock_inside_its_deadline() -> None:
    """The task (mise.toml): bars-list is on the list, help and refusal name it, it takes no stream lock,
    and its start-up, the last start `--minutes` allows and that load's own timeout fit the deadline."""
    import re
    import tomllib

    from canon_trading.session_window import START_UP_SECS

    task = tomllib.loads((bars_list.REPO / "mise.toml").read_text())["tasks"]["paper:data"]
    run = task["run"]
    branch = re.search(r"^  bars-list\) (.*)$", run, re.MULTILINE)
    assert branch is not None
    assert "module=canon_trading.bars_list" in branch.group(1) and "streams=0" in branch.group(1)
    assert "bars-list:" in task["usage"] and "1 to 60 for bars-list" in task["usage"]
    assert "movers-screener, quotes and bars-list" in run
    bound = {k: int(v) for k, v in re.findall(r"^(deadline|sync)=(\d+)$", run, re.MULTILINE)}
    assert bound["deadline"] >= (
        bound["sync"] + START_UP_SECS + bars_list.MAX_MINUTES * 60 + bars_list.LOAD_TIMEOUT_SECS
    )
