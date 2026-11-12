"""The owner's T5 profile contract, using synthetic minute bars and local temporary catalogs only."""

from __future__ import annotations

import importlib
import json
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import history_load
from canon_trading import movers_rank
from canon_trading.bars_to_catalog import FEED_FILE

DAY = date(2026, 10, 15)
AS_OF = date(2026, 10, 16)
FAR = date(2026, 12, 24)  # a holdout date outside every test span


def writer():
    return importlib.import_module("canon_trading.trail_profile")


def minute(equity, day, clock, high, low, close):
    start = movers_rank.ny_ns(day, clock)
    return Bar(
        bar_type=BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL"),
        open=equity.make_price(close),
        high=equity.make_price(high),
        low=equity.make_price(low),
        close=equity.make_price(close),
        volume=equity.make_qty(100),
        ts_event=start,
        ts_init=start + 60_000_000_000,
    )


def bars(equity, prices, day=DAY, first=time(4)):
    start = datetime.combine(day, first)
    return [
        minute(
            equity,
            (start + timedelta(minutes=i)).date(),
            (start + timedelta(minutes=i)).time(),
            *values,
        )
        for i, values in enumerate(prices)
    ]


def test_true_range_uses_previous_close_including_gaps(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    sample = bars(equity, [(100, 100, 100), (112, 108, 110), (102, 98, 100)])
    result = p.segment_profile({DAY: sample}, as_of=AS_OF)[32]
    assert result["bars"] == 3 and result["sessions"] == 1
    assert result["true_range"]["count"] == 2  # first bar has no previous close
    assert p.true_range_fraction(sample[1], 100) == pytest.approx(0.12)
    assert p.true_range_fraction(sample[2], 110) == pytest.approx(12 / 110)


def test_percentiles_are_inclusive_linear_interpolation():
    assert writer().percentiles([1, 2, 3, 4, 5]) == {
        "median": 3,
        "p75": 4,
        "p90": 4.6,
    }


def test_completed_rising_run_counts_its_largest_close_to_low_dip(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    # Anchor 100, rise to 110, dip to 103/close 105, then a high above 111.
    sample = bars(equity, [(100, 100, 100), (111, 100, 110), (109, 103, 105), (112, 106, 111)])
    result = p.segment_profile({DAY: sample}, as_of=AS_OF)[32]
    assert result["pullback"]["count"] == 1
    assert p.pullback_samples(sample) == [(3, pytest.approx(7 / 110))]


@pytest.mark.parametrize("last", [(110, 104, 108), (109, 98, 99)])
def test_a_dip_without_a_new_high_is_not_counted(alpaca_bars, last):
    p = writer()
    sample = bars(alpaca_bars.equity(), [(100, 100, 100), (111, 100, 110), (109, 103, 105), last])
    assert p.pullback_samples(sample) == []


def test_pullback_uses_running_close_peak_and_previous_close_basis(alpaca_bars):
    sample = bars(
        alpaca_bars.equity(),
        [(100, 100, 100), (111, 100, 110), (109, 103, 105), (108, 102, 104), (112, 106, 111)],
    )
    assert writer().pullback_samples(sample) == [(4, pytest.approx(8 / 105))]


def test_thin_has_no_borrowed_or_smoothed_values(alpaca_bars):
    p = writer()
    sample = bars(alpaca_bars.equity(), [(100, 100, 100), (102, 99, 101)])
    rows = p.segment_profile({DAY: sample}, as_of=AS_OF)
    assert p.MIN_SAMPLES > 2
    assert rows[32]["thin"] is True
    assert rows[32]["true_range"] == {
        "count": 1,
        "thin": True,
        "median": None,
        "p75": None,
        "p90": None,
    }
    assert rows[33]["bars"] == rows[33]["sessions"] == 0
    assert rows[33]["true_range"]["median"] is None


def test_statistics_appear_at_the_named_minimum(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    days = [DAY - timedelta(days=i) for i in range(p.MIN_SAMPLES)]
    sample = {day: bars(equity, [(100, 100, 100), (102, 99, 101)], day) for day in days}
    row = p.segment_profile(sample, as_of=AS_OF)[32]
    assert row["true_range"]["thin"] is False
    assert row["true_range"]["median"] == pytest.approx(0.03)
    assert row["sessions"] == p.MIN_SAMPLES


def test_clock_has_96_segments_and_a_new_high_belongs_to_its_confirmation(alpaca_bars):
    p = writer()
    sample = bars(
        alpaca_bars.equity(),
        [(100, 100, 100), (111, 100, 110), (109, 103, 105), (112, 106, 111)],
        first=time(4, 12),
    )
    rows = p.segment_profile({DAY: sample}, as_of=AS_OF)
    assert len(rows) == 96
    assert rows[32]["pullback"]["count"] == 0
    assert rows[33]["pullback"]["count"] == 1
    assert rows[32]["bars"] == 3 and rows[33]["bars"] == 1


def test_sessions_reset_previous_close_and_runs(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    earlier = DAY - timedelta(days=1)
    history = {
        earlier: bars(equity, [(100, 100, 100), (111, 100, 110), (109, 103, 105)], earlier),
        DAY: bars(equity, [(112, 106, 111)]),
    }
    row = p.segment_profile(history, as_of=AS_OF)[32]
    assert row["bars"] == 4 and row["sessions"] == 2
    assert row["true_range"]["count"] == 2 and row["pullback"]["count"] == 0


def test_holdout_date_outside_recent_window_is_refused_before_opening_catalog(tmp_path):
    p = writer()
    days = [date(2026, 10, 16) + timedelta(days=i) for i in range(p.RECENT_SESSIONS + 1)]
    with pytest.raises(history_load.HistoryStopped, match="holdout.*2026-10-16"):
        p.build_profile(
            sessions=days,
            holdout_file=holdout_file(tmp_path, FAR),
            as_of=days[-1] + timedelta(days=1),
            recent_root=tmp_path / "absent",
            long_root=tmp_path / "absent-dev",
            overnight_root=tmp_path / "absent-boats",
            baseline_dir=tmp_path / "out",
        )
    assert not (tmp_path / "absent").exists()


def test_recent_window_exception_is_explicit_in_both_sets():
    p = writer()
    days = [DAY, AS_OF, date(2026, 10, 19)]
    selected = p.select_sessions(days, as_of=date(2026, 10, 20))
    assert selected == {"recent": tuple(days), "long": tuple(days)}


def test_as_of_excludes_its_date_and_all_later_sessions(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    days = [DAY, AS_OF, date(2026, 10, 19)]
    selected = p.select_sessions(days, as_of=AS_OF)
    assert selected == {"recent": (DAY,), "long": (DAY,)}
    row = p.segment_profile(
        {day: bars(equity, [(100, 100, 100), (102, 99, 101)], day) for day in days}, as_of=AS_OF
    )[32]
    assert row["sessions"] == 1 and row["bars"] == 2


def catalogs(tmp_path, alpaca_bars):
    equity = alpaca_bars.equity()
    roots = {}
    for name, feed in (("recent", "sip"), ("long", "sip"), ("overnight", "boats")):
        root = tmp_path / name
        root.mkdir()
        (root / FEED_FILE).write_text(feed + "\n", encoding="ascii")
        catalog = ParquetDataCatalog(str(root))
        catalog.write_instruments([equity])
        for day in (DAY, AS_OF):
            sample = bars(equity, [(100, 100, 100), (102, 99, 101)], day)
            if name == "overnight":
                sample = bars(equity, [(99, 99, 99)], day - timedelta(days=1), time(20))
            catalog.write_bars(sample)
        roots[name] = root
    return roots


def build(tmp_path, alpaca_bars):
    roots = catalogs(tmp_path, alpaca_bars)
    return writer().build_profile(
        sessions=[DAY, AS_OF],
        holdout_file=holdout_file(tmp_path, FAR),
        as_of=AS_OF,
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )


def test_catalog_round_trip_header_and_no_double_count(tmp_path, alpaca_bars):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    assert path == tmp_path / "out" / "trail-profile-v1.json"
    doc = p.read_profile(path, as_of=AS_OF)
    assert doc["format"] == "canon-trail-profile/1"
    assert doc["as_of"] == "2026-10-16"
    assert doc["sessions"] == {"recent": ["2026-10-15"], "long": ["2026-10-15"]}
    assert doc["feeds"] == ["sip", "boats"]
    assert doc["roots"]["recent"] == [str(tmp_path / "recent"), str(tmp_path / "overnight")]
    assert doc["roots"]["long"] == [
        str(tmp_path / "long"),
        str(tmp_path / "recent"),
        str(tmp_path / "overnight"),
    ]
    assert doc["price_basis"] == "previous close"
    assert doc["true_range_definition"] == p.TRUE_RANGE_DEFINITION
    assert doc["pullback_definition"] == p.PULLBACK_DEFINITION
    assert doc["thin_rule"]["minimum_samples"] == p.MIN_SAMPLES
    assert doc["thin_rule"]["calibration"] == "UNCALIBRATED"
    assert doc["segments"] == 96 and doc["segment_minutes"] == 15
    assert doc["part_start"] == "20:00" and doc["timezone"] == "America/New_York"
    assert datetime.fromisoformat(doc["written_at"]).tzinfo is not None
    for name in ("recent", "long"):
        rows = doc["symbols"]["AAPL"][name]
        assert sum(row["bars"] for row in rows) == 3
        assert rows[0]["bars"] == 1
        assert rows[32]["bars"] == 2 and rows[32]["true_range"]["count"] == 2


def test_reader_refuses_unmarked_file_by_name(tmp_path):
    path = tmp_path / "unmarked.json"
    path.write_text('{"symbols": {}}')
    with pytest.raises(history_load.HistoryStopped, match="unmarked.json.*header"):
        writer().read_profile(path, as_of=AS_OF)


@pytest.mark.parametrize(
    "key",
    [
        "format",
        "as_of",
        "written_at",
        "sessions",
        "roots",
        "feeds",
        "price_basis",
        "true_range_definition",
        "pullback_definition",
        "thin_rule",
        "segments",
        "segment_minutes",
        "part_start",
        "timezone",
        "holdout_rule",
        "percentile_method",
    ],
)
def test_reader_requires_each_header_field(tmp_path, alpaca_bars, key):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    del doc[key]
    path.write_text(json.dumps(doc))
    with pytest.raises(history_load.HistoryStopped, match="header"):
        p.read_profile(path, as_of=AS_OF)


def test_reader_refuses_a_profile_from_the_replays_future(tmp_path, alpaca_bars):
    path = build(tmp_path, alpaca_bars)
    with pytest.raises(history_load.HistoryStopped, match="as_of.*2026-10-16"):
        writer().read_profile(path, as_of=DAY)


def test_cli_uses_supplied_roots_sessions_and_as_of(tmp_path, alpaca_bars):
    roots = catalogs(tmp_path, alpaca_bars)
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("# explicit calendar\n2026-10-15\n2026-10-16\n")
    assert (
        writer().main(
            [
                "--sessions-file",
                str(calendar),
                "--as-of",
                str(AS_OF),
                "--recent-root",
                str(roots["recent"]),
                "--long-root",
                str(roots["long"]),
                "--overnight-root",
                str(roots["overnight"]),
                "--baseline-dir",
                str(tmp_path / "out"),
                "--holdout-file",
                str(holdout_file(tmp_path, FAR)),
            ]
        )
        == 0
    )
    assert writer().read_profile(tmp_path / "out" / "trail-profile-v1.json", as_of=AS_OF)


def test_default_as_of_is_next_unfinished_supplied_session():
    p = writer()
    sessions = [DAY, AS_OF, date(2026, 10, 19)]
    now = datetime(2026, 10, 16, 21, tzinfo=movers_rank.NEW_YORK)
    assert p.coming_session(sessions, now=now) == date(2026, 10, 19)
    now = datetime(2026, 10, 16, 3, tzinfo=movers_rank.NEW_YORK)
    assert p.coming_session(sessions, now=now) == AS_OF


def test_recent_never_borrows_a_bar_from_dev(tmp_path, alpaca_bars):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    equity = alpaca_bars.equity()
    # An extra dev bar within recent's dates is legitimate in long alone.
    ParquetDataCatalog(str(roots["long"])).write_bars(
        [minute(equity, DAY, time(4, 2), 103, 100, 102)]
    )
    path = p.build_profile(
        sessions=[DAY, AS_OF],
        holdout_file=holdout_file(tmp_path, FAR),
        as_of=AS_OF,
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    symbol = p.read_profile(path, as_of=AS_OF)["symbols"]["AAPL"]
    assert symbol["recent"][32]["bars"] == 2
    assert symbol["long"][32]["bars"] == 3


def test_queries_never_reach_as_of_or_dev_holdout(tmp_path, alpaca_bars, monkeypatch):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    original = p.ParquetDataCatalog
    original_read = p._read_bars
    seen = []
    paths = {}

    def catalog(root):
        instance = original(root)
        paths[id(instance)] = root
        return instance

    def read(instance, identifiers, days, feed, recent):
        seen.extend((paths[id(instance)], day, feed) for day in days)
        return original_read(instance, identifiers, days, feed, recent)

    monkeypatch.setattr(p, "ParquetDataCatalog", catalog)
    monkeypatch.setattr(p, "_read_bars", read)
    target = date(2026, 10, 19)
    path = p.build_profile(
        sessions=[DAY, AS_OF, target],
        holdout_file=holdout_file(tmp_path, FAR),
        as_of=target,
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    assert all(day < target for _, day, _ in seen)
    assert all(day <= DAY for root, day, _ in seen if root == str(roots["long"]))
    assert (str(roots["recent"]), AS_OF, "sip") in seen
    assert p.read_profile(path, as_of=target)["sessions"]["long"] == [str(DAY), str(AS_OF)]


def test_atomic_replace_failure_keeps_previous_profile(tmp_path, alpaca_bars, monkeypatch):
    from canon_trading import rank_shared

    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    output = tmp_path / "out"
    output.mkdir()
    path = output / "trail-profile-v1.json"
    path.write_text("previous")

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(rank_shared.os, "replace", fail)
    with pytest.raises(history_load.HistoryStopped, match="synthetic replace failure"):
        p.build_profile(
            sessions=[DAY, AS_OF],
            holdout_file=holdout_file(tmp_path, FAR),
            as_of=AS_OF,
            recent_root=roots["recent"],
            long_root=roots["long"],
            overnight_root=roots["overnight"],
            baseline_dir=output,
        )
    assert path.read_text() == "previous"
    assert list(output.iterdir()) == [path]


def test_pullback_statistics_at_minimum_are_raw_depths(alpaca_bars):
    p = writer()
    equity = alpaca_bars.equity()
    history = {
        day: bars(equity, [(100, 100, 100), (111, 100, 110), (109, 103, 105), (112, 106, 111)], day)
        for day in (DAY - timedelta(days=i) for i in range(p.MIN_SAMPLES))
    }
    row = p.segment_profile(history, as_of=AS_OF)[32]
    assert row["thin"] is False
    assert row["pullback"]["count"] == p.MIN_SAMPLES
    for key in ("median", "p75", "p90"):
        assert row["pullback"][key] == pytest.approx(7 / 110)


@pytest.mark.parametrize(
    "corruption", ["future-session", "borrowed-value", "wrong-basis", "bad-thin"]
)
def test_reader_checks_values_not_only_presence(tmp_path, alpaca_bars, corruption):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    row = doc["symbols"]["AAPL"]["recent"][32]
    if corruption == "future-session":
        doc["sessions"]["long"].append(str(AS_OF))
    elif corruption == "borrowed-value":
        row["true_range"]["median"] = 0.1
    elif corruption == "wrong-basis":
        doc["price_basis"] = "current close"
    else:
        row["thin"] = False
    path.write_text(json.dumps(doc))
    with pytest.raises(history_load.HistoryStopped, match="header/data refused"):
        p.read_profile(path, as_of=AS_OF)


def build_recent_only(tmp_path, alpaca_bars, **kwargs):
    roots = catalogs(tmp_path, alpaca_bars)
    path = writer().build_profile(
        sessions=[DAY, AS_OF],
        holdout_file=holdout_file(tmp_path, FAR),
        as_of=AS_OF,
        recent_root=roots["recent"],
        long_root=None,
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
        **kwargs,
    )
    return path, roots


def test_an_absent_long_is_written_and_read_as_recent_only(tmp_path, alpaca_bars):
    p = writer()
    path, roots = build_recent_only(tmp_path, alpaca_bars)
    doc = p.read_profile(path, as_of=AS_OF)
    assert doc["windows"] == "recent only; long absent"
    assert doc["sessions"] == {"recent": ["2026-10-15"]}
    assert doc["roots"] == {"recent": [str(roots["recent"]), str(roots["overnight"])]}
    assert doc["symbols"]["AAPL"]["long"] is None
    rows = doc["symbols"]["AAPL"]["recent"]
    assert sum(row["bars"] for row in rows) == 3 and rows[32]["true_range"]["count"] == 2


def test_an_absent_long_never_takes_or_reads_the_dev_root(tmp_path, alpaca_bars, monkeypatch):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    locked = []
    original = history_load._catalog_lock

    def lock(path):
        locked.append(path)
        return original(path)

    monkeypatch.setattr(history_load, "_catalog_lock", lock)
    with original(roots["long"]):  # dev held by another process: an absent long must not wait on it
        p.build_profile(
            sessions=[DAY, AS_OF],
            holdout_file=holdout_file(tmp_path, FAR),
            as_of=AS_OF,
            recent_root=roots["recent"],
            long_root=None,
            overnight_root=roots["overnight"],
            baseline_dir=tmp_path / "out",
        )
    assert locked == [roots["recent"], roots["overnight"]]


def test_the_cli_writes_an_absent_long_only_when_asked(tmp_path, alpaca_bars):
    roots = catalogs(tmp_path, alpaca_bars)
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("2026-10-15\n2026-10-16\n")
    common = ["--sessions-file", str(calendar), "--as-of", str(AS_OF)]
    common += ["--recent-root", str(roots["recent"]), "--overnight-root", str(roots["overnight"])]
    common += ["--baseline-dir", str(tmp_path / "out")]
    common += ["--holdout-file", str(holdout_file(tmp_path, FAR))]
    assert writer().main([*common, "--long-absent"]) == 0
    doc = writer().read_profile(tmp_path / "out" / "trail-profile-v1.json", as_of=AS_OF)
    assert doc["windows"] == "recent only; long absent"
    with pytest.raises(SystemExit):
        writer().main([*common, "--long-absent", "--long-root", str(roots["long"])])


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("full-header-null-long", "invalid symbol sets for AAPL"),
        ("absent-header-with-long-rows", "invalid symbol sets for AAPL"),
        ("absent-header-with-long-sessions", "header sessions must name recent alone"),
        ("absent-header-with-long-roots", "header roots must name the windows present"),
        ("other-windows-value", "header windows must be"),
        ("absent-header-over-20-sessions", "header sessions violate"),
    ],
)
def test_the_reader_refuses_every_mixed_long_shape(tmp_path, alpaca_bars, corruption, reason):
    p = writer()
    (tmp_path / "full").mkdir()
    (tmp_path / "absent").mkdir()
    full = json.loads(build(tmp_path / "full", alpaca_bars).read_text())
    path, _ = build_recent_only(tmp_path / "absent", alpaca_bars)
    doc = json.loads(path.read_text())
    if corruption == "full-header-null-long":
        doc = full
        doc["symbols"]["AAPL"]["long"] = None
    elif corruption == "absent-header-with-long-rows":
        doc["symbols"]["AAPL"]["long"] = full["symbols"]["AAPL"]["long"]
    elif corruption == "absent-header-with-long-sessions":
        doc["sessions"]["long"] = full["sessions"]["long"]
    elif corruption == "absent-header-with-long-roots":
        doc["roots"]["long"] = full["roots"]["long"]
    elif corruption == "other-windows-value":
        doc["windows"] = "recent and long"
    else:
        doc["sessions"]["recent"] = [
            str(DAY - timedelta(days=i)) for i in range(p.RECENT_SESSIONS, -1, -1)
        ]
    path.write_text(json.dumps(doc))
    with pytest.raises(history_load.HistoryStopped, match=f"header/data refused: {reason}"):
        p.read_profile(path, as_of=AS_OF)


def test_the_full_shape_has_no_windows_field_and_still_needs_long(tmp_path, alpaca_bars):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    assert "windows" not in doc
    assert set(doc["sessions"]) == set(doc["roots"]) == {"recent", "long"}
    del doc["sessions"]["long"]
    path.write_text(json.dumps(doc))
    with pytest.raises(
        history_load.HistoryStopped, match="header sessions must name recent and long"
    ):
        p.read_profile(path, as_of=AS_OF)


def holdout_file(tmp_path, *days):
    path = tmp_path / "holdout.txt"
    path.write_text("# registered holdout\n" + "".join(f"{day}\n" for day in days))
    return path


def counting_catalogs(p, monkeypatch):
    """Count catalog.query_bars calls per root."""
    original = p.ParquetDataCatalog
    calls: dict[str, int] = {}

    class Counting:
        def __init__(self, root):
            self.root, self.catalog = root, original(root)

        def __getattr__(self, name):
            return getattr(self.catalog, name)

        def query_bars(self, *args, **kwargs):
            calls[self.root] = calls.get(self.root, 0) + 1
            return self.catalog.query_bars(*args, **kwargs)

    def make(root):
        return Counting(root)

    monkeypatch.setattr(p, "ParquetDataCatalog", make)
    return calls


def test_each_root_is_read_once_per_batch_over_its_span(tmp_path, alpaca_bars, monkeypatch):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    calls = counting_catalogs(p, monkeypatch)
    target = date(2026, 10, 19)
    p.build_profile(
        sessions=[DAY, AS_OF, target],
        as_of=target,
        holdout_file=holdout_file(tmp_path, date(2026, 12, 24)),
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    assert calls == {str(roots[name]): 1 for name in ("recent", "long", "overnight")}


def test_a_bar_on_a_date_not_supplied_is_never_counted(tmp_path, alpaca_bars):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    equity = alpaca_bars.equity()
    between = date(2026, 10, 17)  # inside the recent root's span, not a supplied session
    ParquetDataCatalog(str(roots["recent"])).write_bars(
        bars(equity, [(100, 100, 100), (150, 90, 140)], between)
    )
    monday, target = date(2026, 10, 19), date(2026, 10, 20)
    path = p.build_profile(
        sessions=[DAY, monday, target],  # AS_OF (10-16) and 10-17 lie inside the span, not supplied
        as_of=target,
        holdout_file=holdout_file(tmp_path, date(2026, 12, 24)),
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    rows = p.read_profile(path, as_of=target)["symbols"]["AAPL"]["recent"]
    assert sum(row["bars"] for row in rows) == 3  # DAY's two sip bars and its night's one, only


def test_a_span_containing_a_holdout_date_is_refused_before_any_query(
    tmp_path, alpaca_bars, monkeypatch
):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    calls = counting_catalogs(p, monkeypatch)
    target = date(2026, 10, 19)
    with pytest.raises(history_load.HistoryStopped, match="holdout date 2026-10-16.*--live-file"):
        p.build_profile(
            sessions=[DAY, AS_OF, target],
            as_of=target,
            holdout_file=holdout_file(tmp_path, AS_OF),
            recent_root=roots["recent"],
            long_root=roots["long"],
            overnight_root=roots["overnight"],
            baseline_dir=tmp_path / "out",
        )
    assert calls == {}
    assert not (tmp_path / "out").exists()


def test_only_the_live_builder_reads_holdout_dates_and_its_header_says_so(tmp_path, alpaca_bars):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    registered = holdout_file(tmp_path, AS_OF)
    target = date(2026, 10, 19)
    path = p.build_live_profile(
        sessions=[DAY, AS_OF, target],
        as_of=target,
        holdout_file=registered,
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    doc = p.read_profile(path, as_of=target)
    assert doc["live_file"] is True
    assert doc["holdout"] == {
        "path": str(registered),
        "sha256": __import__("hashlib").sha256(registered.read_bytes()).hexdigest(),
        "dates_sha256": __import__("hashlib").sha256(f"{AS_OF}\n".encode()).hexdigest(),
        "dates_read": [str(AS_OF)],
    }
    assert doc["sessions"]["recent"] == [str(DAY), str(AS_OF)]


def test_the_replay_builder_cannot_be_given_the_live_flag(tmp_path):
    p = writer()
    with pytest.raises(TypeError, match="live_file"):
        p.build_profile(sessions=[DAY, AS_OF], holdout_file=tmp_path / "h.txt", live_file=True)


def test_a_default_build_records_no_live_flag_and_reads_no_holdout(tmp_path, alpaca_bars):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = p.read_profile(path, as_of=AS_OF)
    assert doc["live_file"] is False
    assert doc["holdout"]["dates_read"] == []


def test_the_cli_needs_a_holdout_file_and_takes_live_file(tmp_path, alpaca_bars):
    roots = catalogs(tmp_path, alpaca_bars)
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("2026-10-15\n2026-10-16\n2026-10-19\n")
    common = ["--sessions-file", str(calendar), "--as-of", "2026-10-19"]
    common += ["--recent-root", str(roots["recent"]), "--long-root", str(roots["long"])]
    common += ["--overnight-root", str(roots["overnight"]), "--baseline-dir", str(tmp_path / "out")]
    with pytest.raises(SystemExit):
        writer().main(common)
    registered = holdout_file(tmp_path, AS_OF)
    assert writer().main([*common, "--holdout-file", str(registered)]) == 1
    assert writer().main([*common, "--holdout-file", str(registered), "--live-file"]) == 0


def test_the_header_carries_the_holdout_dates_hash_beside_the_whole_file_hash(
    tmp_path, alpaca_bars
):
    import hashlib

    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = p.read_profile(path, as_of=AS_OF)
    dates_only = "".join(f"{day}\n" for day in [FAR]).encode()
    assert doc["holdout"]["dates_sha256"] == hashlib.sha256(dates_only).hexdigest()


def test_a_file_written_before_the_dates_hash_still_reads(tmp_path, alpaca_bars):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    del doc["holdout"]["dates_sha256"]
    path.write_text(json.dumps(doc))
    assert "dates_sha256" not in p.read_profile(path, as_of=AS_OF)["holdout"]


def test_no_query_spans_the_excluded_dates_between_dev_and_recent(
    tmp_path, alpaca_bars, monkeypatch
):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    monkeypatch.setattr(p, "RECENT_SESSIONS", 1)
    original = p.ParquetDataCatalog
    windows = []

    class Recording:
        def __init__(self, root):
            self.catalog = original(root)

        def __getattr__(self, name):
            return getattr(self.catalog, name)

        def query_bars(self, *args, **kwargs):
            windows.append((kwargs["start"], kwargs["end"]))
            return self.catalog.query_bars(*args, **kwargs)

    monkeypatch.setattr(p, "ParquetDataCatalog", Recording)
    recent, target = date(2026, 10, 27), date(2026, 10, 28)
    p.build_profile(
        sessions=[DAY, recent, target],  # long = DAY and 10-27; 10-16..10-26 are excluded from long
        as_of=target,
        holdout_file=holdout_file(tmp_path, FAR),
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    excluded = movers_rank.ny_ns(date(2026, 10, 20), time(0))
    assert windows and not any(start <= excluded <= end for start, end in windows)


@pytest.mark.parametrize(
    ("root", "clock", "feed"),
    [
        ("recent", time(21), "sip"),  # a sip bar in boats hours
        ("recent", time(20), "sip"),  # a sip bar opening at 20:00, as Alpaca's sip serves one
        ("overnight", time(10), "boats"),  # a boats bar in sip hours
    ],
)
def test_a_bar_outside_its_feeds_hours_is_left_out_and_counted(
    tmp_path, alpaca_bars, monkeypatch, root, clock, feed
):
    """The per-session read bounded each query to the feed's hours, so such a bar never came back; the
    span read leaves it out the same way and counts it in the header, instead of stopping the build."""
    p = writer()
    monkeypatch.setattr(p, "MAX_OUTSIDE_SHARE", 1.0)
    roots = catalogs(tmp_path, alpaca_bars)
    equity = alpaca_bars.equity()
    sessions = [DAY, AS_OF, date(2026, 10, 19)]
    kwargs = {
        "as_of": date(2026, 10, 19),
        "holdout_file": holdout_file(tmp_path, FAR),
        "recent_root": roots["recent"],
        "long_root": roots["long"],
        "overnight_root": roots["overnight"],
    }
    clean = json.loads(
        p.build_profile(sessions=sessions, baseline_dir=tmp_path / "clean", **kwargs).read_text()
    )
    # Inside the read span, in a supplied session, outside the feed's own hours.
    ParquetDataCatalog(str(roots[root])).write_bars([minute(equity, DAY, clock, 101, 99, 100)])
    path = p.build_profile(sessions=sessions, baseline_dir=tmp_path / "out", **kwargs)
    doc = json.loads(path.read_text())
    for name in ("sip", "boats"):
        before, after = clean["outside_feed_hours"][name], doc["outside_feed_hours"][name]
        assert before["left_out"] == 0
        assert after["left_out"] == (1 if name == feed else 0)
        assert after["read"] == before["read"] + (1 if name == feed else 0)
    assert doc["symbols"] == clean["symbols"]
    p.read_profile(path, as_of=date(2026, 10, 19))


def test_too_many_bars_outside_their_feeds_hours_stop_the_build(tmp_path, alpaca_bars):
    """A shifted clock would push a share of every session out of hours: above the bound, stop."""
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    equity = alpaca_bars.equity()
    ParquetDataCatalog(str(roots["recent"])).write_bars(
        [minute(equity, DAY, time(21, m), 101, 99, 100) for m in range(30)]
    )
    out = tmp_path / "out"
    with pytest.raises(
        history_load.HistoryStopped, match="sip bars read fall outside.*clock suspect"
    ):
        p.build_profile(
            sessions=[DAY, AS_OF, date(2026, 10, 19)],
            as_of=date(2026, 10, 19),
            holdout_file=holdout_file(tmp_path, FAR),
            recent_root=roots["recent"],
            long_root=roots["long"],
            overnight_root=roots["overnight"],
            baseline_dir=out,
        )
    assert not (out / "trail-profile-v1.json").exists()


@pytest.mark.parametrize(
    "value",
    [
        {"sip": {"left_out": -1, "read": 5}, "boats": {"left_out": 0, "read": 5}},
        {"sip": {"left_out": 0, "read": 5}},
        {"sip": {"left_out": 0, "read": 5}, "boats": {"left_out": 0}},
        {"sip": {"left_out": True, "read": 5}, "boats": {"left_out": 0, "read": 5}},
        {"sip": {"left_out": 5, "read": 5}, "boats": {"left_out": 0, "read": 5}},
        {"sip": 0, "boats": 0},
        [],
    ],
)
def test_the_reader_checks_the_outside_hours_count(tmp_path, alpaca_bars, value):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    doc["outside_feed_hours"] = value
    path.write_text(json.dumps(doc))
    with pytest.raises(
        history_load.HistoryStopped, match="header/data refused: header outside_feed_hours"
    ):
        p.read_profile(path, as_of=AS_OF)


@pytest.mark.parametrize(
    ("written_at", "reads"),
    [("2026-11-12T11:00:45+00:00", True), ("2026-11-12T16:00:00+00:00", False)],
)
def test_only_a_file_written_before_the_count_may_lack_it(tmp_path, alpaca_bars, written_at, reads):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    del doc["outside_feed_hours"]
    if written_at:
        doc["written_at"] = written_at
    path.write_text(json.dumps(doc))
    if reads:
        p.read_profile(path, as_of=AS_OF)
    else:
        with pytest.raises(history_load.HistoryStopped, match="outside_feed_hours"):
            p.read_profile(path, as_of=AS_OF)


@pytest.mark.parametrize("content", [None, "2026-13-01\n", "# only a comment\n"])
def test_a_bad_holdout_file_is_refused_by_name(tmp_path, content):
    p = writer()
    path = tmp_path / "holdout.txt"
    if content is not None:
        path.write_text(content)
    with pytest.raises(history_load.HistoryStopped, match="holdout file .*holdout.txt"):
        p.build_profile(sessions=[DAY, AS_OF], as_of=AS_OF, holdout_file=path)


@pytest.mark.parametrize("corruption", ["dates-read-not-dates", "sha256-not-hex", "path-not-text"])
def test_the_reader_checks_the_holdout_fields_values(tmp_path, alpaca_bars, corruption):
    p = writer()
    path = build(tmp_path, alpaca_bars)
    doc = json.loads(path.read_text())
    if corruption == "dates-read-not-dates":
        doc["live_file"] = True
        doc["holdout"]["dates_read"] = [42]
    elif corruption == "sha256-not-hex":
        doc["holdout"]["sha256"] = None
    else:
        doc["holdout"]["path"] = 7
    path.write_text(json.dumps(doc))
    with pytest.raises(history_load.HistoryStopped, match="header/data refused: header holdout"):
        p.read_profile(path, as_of=AS_OF)


def test_a_live_file_never_reaches_a_replay_of_a_holdout_date(tmp_path, alpaca_bars):
    p = writer()
    roots = catalogs(tmp_path, alpaca_bars)
    target = date(2026, 10, 19)
    path = p.build_live_profile(
        sessions=[DAY, AS_OF, target],
        as_of=target,
        holdout_file=holdout_file(tmp_path, AS_OF),
        recent_root=roots["recent"],
        long_root=roots["long"],
        overnight_root=roots["overnight"],
        baseline_dir=tmp_path / "out",
    )
    with pytest.raises(history_load.HistoryStopped, match="as_of 2026-10-19 is later"):
        p.read_profile(path, as_of=AS_OF)  # replaying the holdout date itself


def test_the_default_roots_are_relative_to_the_checkout() -> None:
    """A default root under someone's home would publish that path and fail on every other machine."""
    trail_profile = writer()
    for root in (
        trail_profile.DEFAULT_RECENT_ROOT,
        trail_profile.DEFAULT_LONG_ROOT,
        trail_profile.DEFAULT_OVERNIGHT_ROOT,
    ):
        assert not root.is_absolute(), root
        assert root.parts[0] == "catalog", root
