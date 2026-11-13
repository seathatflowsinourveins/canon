"""Amendment 10's registered profile arithmetic, on explicit synthetic profile rows."""

import importlib
import json
from copy import deepcopy
from datetime import date
from datetime import time
from datetime import timedelta
from decimal import Decimal

import pytest

from canon_trading import movers_rank
from canon_trading import trail_profile

D = Decimal
BASE = {
    "profile_k": D(2),
    "profile_pctl": "p75",
    "profile_floor": "median",
    "profile_multipliers": (D(1), D(1), D(1)),
}


def arithmetic():
    return importlib.import_module("canon_trading.trail")


def profile_document():
    """A fully marked read_profile input; every segment has its own independent dictionaries."""
    dates = [date(2025, 10, 20) + timedelta(days=i) for i in range(5)]
    row = {
        "sessions": 5,
        "bars": 20,
        "thin": False,
        "true_range": {"count": 20, "thin": False, "median": 0.002, "p75": 0.003, "p90": 0.004},
        "pullback": {"count": 20, "thin": False, "median": 0.007, "p75": 0.009, "p90": 0.011},
    }
    return {
        "format": trail_profile.FORMAT,
        "as_of": "2025-11-10",
        "written_at": "2025-11-09T23:00:00+00:00",
        "live_file": False,
        "holdout": {"path": "/holdout", "sha256": "0" * 64, "dates_read": []},
        "sessions": {name: [str(day) for day in dates] for name in ("recent", "long")},
        "roots": {"recent": ["/recent", "/overnight"], "long": ["/long", "/recent", "/overnight"]},
        "feeds": ["sip", "boats"],
        "price_basis": "previous close",
        "true_range_definition": trail_profile.TRUE_RANGE_DEFINITION,
        "pullback_definition": trail_profile.PULLBACK_DEFINITION,
        "holdout_rule": trail_profile.HOLDOUT_RULE,
        "thin_rule": trail_profile._thin_rule(),
        "percentile_method": trail_profile.PERCENTILE_METHOD,
        "part_start": "20:00",
        "segment_minutes": movers_rank.SEGMENT_MINUTES,
        "segments": movers_rank.SEGMENTS,
        "timezone": "America/New_York",
        "symbols": {
            "AAA": {name: [deepcopy(row) for _ in range(96)] for name in ("recent", "long")}
        },
    }


def distance(
    doc=None, *, symbol="AAA", segment=54, atr=Decimal("0.001"), ratio=Decimal(10), **rows
):
    return arithmetic().profile_distance(
        profile_document() if doc is None else doc,
        symbol,
        segment,
        atr,
        ratio,
        **(BASE | rows),
    )


def test_profile_rows_name_only_the_registered_bases_and_alternates():
    rows = arithmetic().PROFILE_SETTINGS
    assert set(rows) == set(BASE)
    for name, base in BASE.items():
        assert rows[name]["default"] == base
        assert rows[name]["status"] == "UNCALIBRATED"
        assert rows[name]["source"] == "lead, no primary source"
    assert rows["profile_k"]["alternates"] == (D(1), D(3))
    assert rows["profile_pctl"]["arms"] == ("median", "p75", "p90")
    assert rows["profile_floor"]["arms"] == ("median", "p75", "off")
    assert rows["profile_multipliers"]["alternates"] == ((D(1), D("1.25"), D("1.5")),)


@pytest.mark.parametrize(
    ("day", "clock", "expected"),
    [
        (date(2026, 11, 11), time(20), 0),
        (date(2026, 11, 12), time(3, 59), 31),
        (date(2026, 11, 12), time(4), 32),
        (date(2026, 11, 12), time(9, 29), 53),
        (date(2026, 11, 12), time(9, 30), 54),
        (date(2026, 11, 12), time(15, 59), 79),
        (date(2026, 11, 12), time(16), 80),
        (date(2026, 11, 12), time(19, 59), 95),
        (date(2026, 11, 12), time(20), 0),
    ],
)
def test_segment_reuses_the_ranking_clock_across_both_dates(day, clock, expected):
    assert arithmetic().profile_segment(movers_rank.ny_ns(day, clock)) == expected


def test_base_formula_names_every_measured_input():
    result = distance()
    assert result == {
        "symbol": "AAA",
        "segment": 54,
        "K": D(2),
        "pctl": "p75",
        "R": D("0.003"),
        "r_window": "recent",
        "ATR": D("0.001"),
        "floor_setting": "median",
        "floor": "median",
        "F": D("0.007"),
        "f_window": "recent",
        "ratio": D(10),
        "bucket": "10 and over",
        "V": D(1),
        "multipliers": (D(1), D(1), D(1)),
        "distance": D("0.007"),
        "reason": None,
    }


@pytest.mark.parametrize(
    ("setting", "value", "input_name", "expected"),
    [
        ("profile_k", D(1), "K", "0.007"),
        ("profile_k", D(3), "K", "0.009"),
        ("profile_pctl", "median", "pctl", "0.007"),
        ("profile_pctl", "p90", "pctl", "0.008"),
        ("profile_floor", "p75", "floor", "0.009"),
        ("profile_floor", "off", "floor", "0.006"),
        ("profile_multipliers", (D(1), D("1.25"), D("1.5")), "multipliers", "0.0105"),
    ],
)
def test_seven_alternates_change_one_registered_input(setting, value, input_name, expected):
    result = distance(**{setting: value})
    assert result["distance"] == D(expected)
    assert result[input_name] == value
    assert sum(result[key] != distance()[key] for key in ("K", "pctl", "floor", "multipliers")) == 1


@pytest.mark.parametrize(("atr", "expected"), [(None, "0.006"), (D("0.005"), "0.010")])
def test_the_profile_is_the_cold_prior_and_a_larger_warm_atr_wins(atr, expected):
    result = distance(atr=atr, profile_floor="off")
    assert result["distance"] == D(expected) and result["ATR"] == atr
    assert result["reason"] is None and result["F"] is None and result["floor"] == "off"


@pytest.mark.parametrize(
    ("ratio", "bucket", "v"),
    [
        ("2.99", "under 3", "1"),
        ("3", "3 to under 10", "1.25"),
        ("9.99", "3 to under 10", "1.25"),
        ("10", "10 and over", "1.5"),
    ],
)
def test_bucket_edges_are_constants(ratio, bucket, v):
    result = distance(ratio=D(ratio), profile_multipliers=(D(1), D("1.25"), D("1.5")))
    assert (result["bucket"], result["V"]) == (bucket, D(v))
    assert result["distance"] == D("0.007") * D(v)


@pytest.mark.parametrize(
    "ratio", [None, D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), D(0), D(-1), float("nan")]
)
def test_unknown_ratio_is_named_and_uses_the_first_multiplier(ratio):
    result = distance(ratio=ratio, profile_multipliers=(D(1), D("1.25"), D("1.5")))
    assert (result["ratio"], result["bucket"], result["V"]) == ("unknown", "under 3", D(1))


@pytest.mark.parametrize("missing", ["true_range", "pullback"])
def test_recent_before_long_is_chosen_separately_for_each_statistic(missing):
    doc = profile_document()
    for key in ("median", "p75", "p90"):
        doc["symbols"]["AAA"]["recent"][54][missing][key] = None
    result = distance(doc)
    assert result["r_window"] == ("long" if missing == "true_range" else "recent")
    assert result["f_window"] == ("long" if missing == "pullback" else "recent")


def test_missing_pullback_is_an_absent_floor_and_does_not_make_the_symbol_thin():
    doc = profile_document()
    for window in ("recent", "long"):
        doc["symbols"]["AAA"][window][54]["pullback"] = dict.fromkeys(("median", "p75", "p90"))
    result = distance(doc)
    assert result["distance"] == D("0.006") and result["reason"] is None
    assert result["floor"] == "absent: no pullback statistic"
    assert result["F"] is result["f_window"] is None
    assert result["floor_setting"] == "median"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("absent", "not in the profile"),
        ("sessions", "4 recent sessions"),
        ("range", "no true_range statistic"),
    ],
)
def test_thin_is_named_without_borrowing_another_symbol_or_segment(kind, reason):
    doc = profile_document()
    if kind == "absent":
        doc["symbols"]["BBB"] = doc["symbols"].pop("AAA")
    elif kind == "sessions":
        doc["symbols"]["AAA"]["recent"][54]["sessions"] = 4
    else:
        for window in ("recent", "long"):
            doc["symbols"]["AAA"][window][54]["true_range"]["p75"] = None
    result = distance(doc)
    assert result["distance"] is None and result["reason"] == reason


def test_five_recent_sessions_is_not_thin():
    assert distance()["reason"] is None


def test_file_floats_are_converted_through_their_text():
    doc = profile_document()
    value = 0.1 + 0.2
    doc["symbols"]["AAA"]["recent"][54]["true_range"]["p75"] = value
    assert distance(doc)["R"] == D(str(value))


@pytest.mark.parametrize("sessions", [4, 5])
def test_the_ordered_thin_rule_precedes_long_fallback_for_sample_thin_recent_rows(
    tmp_path, sessions
):
    doc = profile_document()
    recent = doc["symbols"]["AAA"]["recent"][54]
    recent["sessions"], recent["thin"] = sessions, True
    for metric in ("true_range", "pullback"):
        recent[metric] = {
            "count": trail_profile.MIN_SAMPLES - 1,
            "thin": True,
            "median": None,
            "p75": None,
            "p90": None,
        }
    path = tmp_path / "ordered-thin.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    checked = trail_profile.read_profile(path, as_of=date(2025, 11, 10))
    result = distance(checked)
    if sessions == 4:
        assert result["reason"] == "4 recent sessions" and result["distance"] is None
        assert result["r_window"] is result["f_window"] is None
    else:
        assert result["reason"] is None and result["distance"] == D("0.007")
        assert result["r_window"] == result["f_window"] == "long"


def absent_long_document():
    """The recent-only file as the builder writes it and the strict reader accepts it (the lead's change
    of 2026-11-12): the header names the windows present, sessions and roots carry recent alone, and
    every symbol's long rows are null."""
    doc = profile_document()
    doc["windows"] = "recent only; long absent"
    doc["sessions"] = {"recent": doc["sessions"]["recent"]}
    doc["roots"] = {"recent": doc["roots"]["recent"]}
    doc["symbols"]["AAA"]["long"] = None
    return doc


def test_the_absent_long_document_is_the_shape_the_strict_reader_accepts(tmp_path):
    path = tmp_path / "trail-profile.json"
    path.write_text(json.dumps(absent_long_document()), encoding="utf-8")
    read = trail_profile.read_profile(path, as_of=date(2025, 11, 10))
    assert read == absent_long_document()
    assert distance(read) == distance()


def test_an_absent_long_window_leaves_a_present_recent_statistic_as_it_was():
    result = distance(absent_long_document())
    assert result == distance()
    assert (result["r_window"], result["f_window"]) == ("recent", "recent")


def test_an_absent_long_window_with_no_recent_range_is_thin_by_name_not_an_error():
    doc = absent_long_document()
    doc["symbols"]["AAA"]["recent"][54]["true_range"]["p75"] = None
    result = distance(doc)
    assert result["distance"] is None and result["reason"] == "no true_range statistic"
    assert result["R"] is result["r_window"] is None


def test_an_absent_long_window_with_no_recent_pullback_is_an_absent_floor():
    doc = absent_long_document()
    doc["symbols"]["AAA"]["recent"][54]["pullback"]["median"] = None
    result = distance(doc)
    assert result["distance"] == D("0.006") and result["reason"] is None
    assert result["floor"] == "absent: no pullback statistic"
    assert result["F"] is result["f_window"] is None


def test_a_symbol_with_no_recent_rows_is_thin_by_name():
    doc = profile_document()
    doc["symbols"]["AAA"]["recent"] = None
    result = distance(doc)
    assert result["distance"] is None and result["reason"] == "no recent rows"
