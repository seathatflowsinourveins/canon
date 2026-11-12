"""`canon_trading.early_entry_study` on generated bars: the listing minute, the first-rising-run entry and
its refusals, the trailing exit walked open, high, low, close, ADR 0092's measures, and a catalog read.

Offline. The catalog tests write Alpaca-shaped minute bars through the adapter's `parse_bar` into a
`ParquetDataCatalog` in pytest's temporary directory, as `conftest.AlpacaBars` does for daily bars.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from alpaca.data.models import Bar as AlpacaBar
from canon_alpaca.data import parse_bar
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import early_entry_study as study
from canon_trading.early_entry_study import CHASE_CAP
from canon_trading.early_entry_study import EXIT_THROUGH
from canon_trading.early_entry_study import REGISTERED
from canon_trading.early_entry_study import TRAIL
from canon_trading.early_entry_study import Costs
from canon_trading.early_entry_study import Day
from canon_trading.early_entry_study import Minute
from canon_trading.early_entry_study import replay as replay_day
from canon_trading.movers_stream import normal

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import AlpacaBars

DAY = date(2026, 11, 11)
OPEN = study.REGULAR_OPEN


def bars(
    start: int, closes: list[float], volume: float = 1000.0, first: float = 10.0
) -> list[Minute]:
    """Consecutive minute bars from `start`, each opening at the close before it."""
    out, previous = [], first
    for i, close in enumerate(closes):
        out.append(
            Minute(start + i, previous, max(previous, close), min(previous, close), close, volume)
        )
        previous = close
    return out


QUIET = tuple((Minute(study.PART_START, 10.0, 10.0, 10.0, 10.0, 100.0),) for _ in range(5))
"""Five earlier sessions with $1,000 traded in the first minute and nothing after."""


def a_day(today: list[Minute], *, earlier: tuple = QUIET, reference: float | None = 10.0) -> Day:
    return Day("TEST", DAY, tuple(today), earlier, reference)


RISE = [
    Minute(OPEN, 10.0, 10.1, 10.0, 10.1, 100.0),  # +1%, ratio 1.01: not listed
    *bars(OPEN + 1, [10.4, 10.5, 10.6, 10.7, 10.9, 10.8], first=10.1),
]
"""Listed at the second bar's close (+4%, ratio over 11); the run of three completes at 10.7."""


def test_a_name_is_listed_at_the_first_close_that_meets_both_thresholds() -> None:
    seen = study.listing(a_day(RISE), REGISTERED)
    assert [s.listed for s in seen] == [False, True, True, True, True, True, True]
    assert seen[0].ratio == pytest.approx(1.01)
    assert seen[1].minute == OPEN + 2
    assert seen[1].move == pytest.approx(0.04)


def test_the_entry_is_the_close_of_the_run_s_last_bar_and_the_measures_follow() -> None:
    run = replay_day(a_day(RISE), REGISTERED)
    assert run.ignition == run.listed == OPEN + 2
    assert run.entry_minute == OPEN + 5
    assert run.entry == pytest.approx(10.7)
    assert run.lead == 3
    assert (run.high, run.high_minute) == (10.9, OPEN + 6)
    assert run.done_at_entry == pytest.approx(0.7 / 0.9)
    assert run.ahead == pytest.approx(0.2 / 0.9)
    assert run.late
    assert run.exit is None  # 10.8 is above the trigger, 10.9 less 2% of the entry
    assert run.managed == run.hold == pytest.approx(10.8 / 10.7 - 1)


def test_bars_before_the_listing_do_not_count_towards_the_run() -> None:
    thin = [Minute(OPEN + i, 10.0, c, 10.0, c, 1.0) for i, c in enumerate([10.31, 10.32, 10.33])]
    today = [*thin, *bars(OPEN + 3, [10.4, 10.5, 10.6, 10.7, 10.8], first=10.33)]
    run = replay_day(a_day(today), REGISTERED)
    assert run.listed == OPEN + 4  # the first bar with volume
    assert run.entry_minute == OPEN + 7  # three rises after the listed bar, not after 10.31


def test_a_repeated_close_restarts_the_run() -> None:
    today = [RISE[0], *bars(OPEN + 1, [10.4, 10.5, 10.5, 10.6, 10.7, 10.8], first=10.1)]
    run = replay_day(a_day(today), REGISTERED)
    assert run.entry == pytest.approx(10.8)
    assert run.entry_minute == OPEN + 7


def test_a_name_that_leaves_the_listing_loses_its_bars() -> None:
    today = [
        RISE[0],
        *bars(OPEN + 1, [10.4, 10.5, 10.2, 10.45, 10.5, 10.6, 10.7], first=10.1),
    ]  # 10.2 is +2%: unlisted for one bar
    run = replay_day(a_day(today), REGISTERED)
    assert run.entry == pytest.approx(10.7)


def test_shared_entry_runs_keep_the_listing_clock_and_clear_unlisted_bars() -> None:
    today = bars(OPEN, [10.4, 10.5, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8])
    candidates = list(study.entry_runs(a_day(today), REGISTERED))
    assert [(index, listed_at) for index, _, listed_at in candidates] == [
        (6, OPEN + 1),
        (7, OPEN + 1),
    ]
    # Starting to watch late never lets historical rises warm the entry.
    assert list(study.entry_runs(a_day(today), REGISTERED, start=OPEN + 5)) == []


def test_shared_extension_refusal_is_independent_of_the_diagnostics_spread_model() -> None:
    day = a_day(bars(OPEN, [10.4] * 17 + [10.5, 10.6, 10.7, 10.8]))
    rows = study.ARMS["window-15"]
    index, run, listed_at = next(study.entry_runs(day, rows))
    assert index == 19 and listed_at == OPEN + 1
    assert "past the first-leg window of 15" in (
        study.entry_refusal(day, rows, index, run, listed_at) or ""
    )
    assert study.entry_refusal(day, REGISTERED, index, run, listed_at) is None
    assert study.refusal(day, REGISTERED, Costs(0.03, 0.03), index, run, listed_at) == (
        "its bid is under the planned stop"
    )


def test_a_name_with_no_normal_is_never_listed_and_says_why() -> None:
    assert replay_day(a_day(RISE, earlier=()), REGISTERED).never_listed == (
        "no earlier session to take a normal from"
    )
    assert "volume ratio never reached" in (
        replay_day(a_day(bars(OPEN, [10.4, 10.5], volume=1.0)), REGISTERED).never_listed or ""
    )
    assert "move never reached" in (
        replay_day(a_day(bars(OPEN, [10.1, 10.2])), REGISTERED).never_listed or ""
    )
    assert replay_day(a_day(RISE, reference=None), REGISTERED).never_listed == "no reference close"


def test_a_missing_name_is_kept_and_counted() -> None:
    gone = Day("GONE", DAY, (), (), None, missing="no minute bars in the catalog")
    run = replay_day(gone, REGISTERED)
    assert run.entry is None and run.late
    assert "missing: 1" in study.summary([run, replay_day(a_day(RISE), REGISTERED)])[0]


def test_the_no_chase_cap_refuses_by_name_and_a_later_run_is_decided_again() -> None:
    capped = replace(REGISTERED, max_move=0.06)
    run = replay_day(a_day(RISE), capped)
    assert run.entry is None
    assert [m for m, _ in run.refusals] == [OPEN + 5, OPEN + 6]
    assert "over the no-chase cap of 6%" in run.refusals[0][1]


def test_a_run_past_the_first_leg_window_is_extended_unless_it_pulled_back() -> None:
    late = [
        RISE[0],
        *bars(OPEN + 1, [10.4, 10.8, 10.3, 10.35, 10.4, 10.45, 10.5], first=10.1),
    ]
    windowed = replace(REGISTERED, first_leg_minutes=3)
    run = replay_day(a_day(late), windowed)
    assert run.entry is None
    assert "past the first-leg window of 3" in run.refusals[0][1]
    admitted = replay_day(a_day(late), replace(windowed, pullback=0.04))
    assert admitted.entry == pytest.approx(10.45)  # 10.3 is over 4% under the 10.8 close
    refused = replay_day(a_day(late), replace(windowed, pullback=0.10))
    assert "no pullback of 10%" in refused.refusals[0][1]


def test_a_run_too_far_above_the_quiet_price_is_extended() -> None:
    rows = replace(REGISTERED, quiet_minutes=30, max_above_quiet=0.02)
    run = replay_day(a_day(RISE), rows)
    assert "above the quiet price 10.1000" in run.refusals[0][1]  # the one bar before the run
    assert replay_day(a_day(RISE), replace(rows, max_above_quiet=0.10)).entry is not None
    with pytest.raises(ValueError, match="together"):
        replace(REGISTERED, quiet_minutes=30)


def test_the_trigger_rises_on_a_bar_s_high_before_its_low_is_tested() -> None:
    entry = 10.0
    after = [
        Minute(600, 10.0, 10.5, 10.1, 10.4, 1.0),  # trigger to 10.3; the low 10.1 reaches it
    ]
    out = study.protect(after, entry, entry, study.GROSS)
    assert out is not None
    assert (out.why, out.minute, out.raises) == ("trail", 601, 1)
    assert out.first_trigger == pytest.approx(entry * (1 - TRAIL))
    assert out.price == pytest.approx(10.5 - entry * TRAIL)


def test_an_open_under_the_trigger_sells_at_the_open_or_rests_at_the_chase_s_floor() -> None:
    entry, trigger = 10.0, 9.8
    limit = trigger - entry * EXIT_THROUGH
    floor = limit * (1 - CHASE_CAP)
    gap = study.protect([Minute(600, 9.7, 9.7, 9.6, 9.6, 1.0)], entry, entry, study.GROSS)
    assert gap is not None and (gap.why, gap.price) == ("gap", 9.7)
    deep = [Minute(600, 9.0, 9.1, 8.9, 9.0, 1.0), Minute(601, 9.0, 9.7, 9.0, 9.6, 1.0)]
    rest = study.protect(deep, entry, entry, study.GROSS)
    assert rest is not None and (rest.why, rest.minute) == ("floor", 602)
    assert rest.price == pytest.approx(floor)
    assert (
        study.protect(deep[:1], entry, entry, study.GROSS) is None
    )  # still held at the window's end


def test_costs_move_the_entry_to_the_ask_and_the_sale_to_the_bid() -> None:
    costs = Costs(premarket=0.01, regular=0.001)
    run = replay_day(a_day(RISE), REGISTERED, costs)
    assert run.entry == pytest.approx(10.7 * 1.001)
    assert run.hold == pytest.approx(10.8 * 0.999 / (10.7 * 1.001) - 1)
    assert costs.half(study.PART_START) == 0.01 and costs.times(2).regular == 0.002
    wide = replay_day(a_day(RISE), REGISTERED, Costs(0.03, 0.03))
    assert wide.entry is None and wide.refusals[0][1] == "its bid is under the planned stop"


def test_a_window_that_starts_late_sees_only_what_is_left() -> None:
    run = replay_day(a_day(RISE), REGISTERED, start=OPEN + 4)
    assert run.ignition == OPEN + 2  # ignition is the tape's, not the window's
    assert run.done_at_start == pytest.approx(0.6 / 0.9)
    assert run.entry is None  # 10.7, 10.9, 10.8: no run of three is left


# --- the catalog -----------------------------------------------------------------------------------


def minute_payloads(day: date, start: int, closes: list[float], volume: int) -> list[dict]:
    at = datetime.combine(day, datetime.min.time(), study.NEW_YORK) + timedelta(minutes=start)
    out, previous = [], closes[0]
    for i, close in enumerate(closes):
        opened = (at + timedelta(minutes=i)).astimezone(UTC)
        out.append(
            {
                "t": opened.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": previous,
                "h": max(previous, close),
                "l": min(previous, close),
                "c": close,
                "v": volume,
                "n": 10,
                "vw": close,
            }
        )
        previous = close
    return out


def write_minutes(
    root: Path, alpaca_bars: AlpacaBars, symbol: str, days: dict[date, list[dict]], until: int
) -> None:
    """Write `days`' minute bars as one covered range, from the first day's 00:00 to `until` on DAY."""
    root.mkdir(exist_ok=True)
    equity = alpaca_bars.equity(symbol)
    bar_type = BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL")
    series = [
        parse_bar(AlpacaBar(symbol, payload), bar_type, equity)
        for day in sorted(days)
        for payload in days[day]
    ]
    ParquetDataCatalog(str(root)).write_bars(
        series, start=study._ns(min(days), 0), end=study._ns(DAY, until)
    )


def test_a_day_is_read_from_the_catalog_with_its_reference_from_the_last_regular_minute_bar(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    before = date(2026, 11, 10)
    days = {
        before: minute_payloads(before, study.PART_START, [10.0], 100)
        + minute_payloads(before, 15 * 60 + 59, [10.0], 1),
        DAY: minute_payloads(DAY, OPEN, [10.1, 10.4, 10.5, 10.6, 10.7, 10.9, 10.8], 1000),
    }
    write_minutes(tmp_path, alpaca_bars, "TEST", days, OPEN + 30)
    day = study.load_day(tmp_path, "TEST", DAY)
    assert day.missing is None
    assert day.reference == 10.0
    assert day.reference_from == "the 15:59 minute bar of 2026-11-10"
    assert day.covered_to == OPEN + 30
    assert [m.start for m in day.bars][:2] == [OPEN, OPEN + 1]
    assert len(day.earlier) == 1 and len(day.earlier[0]) == 2
    run = replay_day(day, REGISTERED)
    assert run.entry_minute == OPEN + 5
    assert study.load_day(tmp_path, "NONE", DAY).missing == "no minute bars in the catalog"
    assert "does not cover" in (study.load_day(tmp_path, "TEST", date(2026, 11, 12)).missing or "")


def test_the_command_prints_a_section_a_name_and_a_summary(
    tmp_path: Path, alpaca_bars: AlpacaBars, capsys: pytest.CaptureFixture[str]
) -> None:
    before = date(2026, 11, 10)
    days = {
        before: minute_payloads(before, study.PART_START, [10.0], 100)
        + minute_payloads(before, 15 * 60 + 59, [10.0], 1),
        DAY: minute_payloads(DAY, OPEN, [10.1, 10.4, 10.5, 10.6, 10.7, 10.9, 10.8], 1000),
    }
    write_minutes(tmp_path, alpaca_bars, "TEST", days, study.WINDOW_END)
    argv = ["--catalog", str(tmp_path), "--day", "2026-11-11", "--symbols", "test,none"]
    assert study.main(argv) == 0
    out = capsys.readouterr().out
    assert "### TEST, 2026-11-11 (DIAGNOSTIC)" in out
    assert "entry 10.7000 at 09:35, lead 3 min" in out
    assert "* missing: no minute bars in the catalog" in out
    assert "names: 2; missing: 1; entered: 1" in out
    assert "costs x1" not in out
    assert study.main([*argv, "--half-spread-bps", "50", "10", "--from", "09:34"]) == 0
    out = capsys.readouterr().out
    assert "**Summary, costs x2**" in out
    assert "watching from 09:34: the price then stood" in out


def test_the_arms_are_the_fifteen_the_record_registers() -> None:
    assert len(study.ARMS) == 15
    assert next(iter(study.ARMS.values())) is REGISTERED
    assert len(set(study.ARMS.values())) == 15
    for rows in study.ARMS.values():
        assert rows.max_move is None  # the no-chase cap from the reference is off in every arm
        replay_day(a_day(RISE), rows)


# --- the list, the split and the walk-forward ------------------------------------------------------

DATES = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]


@pytest.fixture(autouse=True)
def isolated_holdout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(study, "default_holdout_root", lambda: tmp_path / "holdouts")


SLOW = [
    RISE[0],
    *bars(OPEN + 1, [10.4, 10.5, 10.5, 10.6, 10.6, 10.7, 10.7, 10.8, 10.9], first=10.1),
]
"""Listed, but never three rises in a row: only an arm with a shorter run enters it."""


def on(day: date, today: list[Minute]) -> Day:
    return Day("TEST", day, tuple(today), QUIET, 10.0)


def write_list(path: Path, lines: list[str]) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_list_is_refused_unless_it_is_the_one_registered(tmp_path: Path) -> None:
    path = tmp_path / "days.csv"
    digest = write_list(path, ["# a comment", "2026-09-01,test", "2026-09-02, OTHER  # late"])
    assert study.read_days(path, digest) == [(DATES[0], "TEST"), (DATES[1], "OTHER")]
    with pytest.raises(ValueError, match="not the list registered"):
        study.read_days(path, "0" * 64)
    for bad, why in (
        ("2026-09-01", "line 1"),
        ("yesterday,TEST", "line 1"),
        ("2026-09-01,TEST\n2026-09-01,test", "repeats"),
    ):
        with pytest.raises(ValueError, match=why):
            study.read_days(path, write_list(path, [bad]))


def test_the_dates_split_in_order_and_the_folds_never_judge_a_date_they_chose_on() -> None:
    development, holdout = study.split_dates(reversed([*DATES, DATES[0]]))
    assert (development, holdout) == (DATES[:7], DATES[7:])
    with pytest.raises(ValueError, match="walk-forward needs 5"):
        study.split_dates(DATES[:7])
    ninety = [date(2026, 1, 1) + timedelta(days=i) for i in range(90)]
    assert len(study.split_dates(ninety)[0]) == 63  # 90 x 0.7 in floating point is under 63
    folds = study.folds(DATES)
    assert [(len(train), test) for train, test in folds] == [
        (2, DATES[2:4]),
        (4, DATES[4:6]),
        (6, DATES[6:8]),
        (8, DATES[8:10]),
    ]
    assert all(max(train) < min(test) for train, test in folds)


def test_an_arm_is_chosen_on_earlier_dates_and_judged_on_later_ones() -> None:
    days = [on(d, SLOW) for d in DATES]
    quiet = on(DATES[0], bars(OPEN, [10.1, 10.2]))  # never listed under any arm
    assert study.choose([quiet]) == "registered"  # every arm ties: the first listed
    assert study.choose(days[:2]) == "run-1"
    assert study.early_share([replay_day(d, REGISTERED) for d in days]) == (0.0, 0.0)
    walked = study.walk_forward(days, DATES)
    assert [fold.arm for fold in walked] == ["run-1"] * 4
    assert [fold.judged for fold in walked] == [tuple(test) for _, test in study.folds(DATES)]
    assert all(run.entry is not None for fold in walked for run in fold.runs)


def test_the_holdout_opens_once(tmp_path: Path) -> None:
    path = tmp_path / "days.csv"
    path.write_text("2026-09-01,TEST\n", encoding="utf-8")
    marker = study.open_holdout(path)
    assert marker.name == "days.csv.holdout-opened" and marker.exists()
    with pytest.raises(PermissionError, match="opened once"):
        study.open_holdout(path)


def test_shared_dates_holdout_is_consumed_only_after_success(tmp_path: Path) -> None:
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text("{}")
    second.write_text("{}")
    held = tuple(DATES[-3:])
    root = tmp_path / "holdouts"

    def failure():
        raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError, match="replay failed"):
        study.open_holdout(first, dates=held, root=root, replay=failure)
    assert not list(root.glob("*.holdout-opened"))
    marker = study.open_holdout(second, dates=held, root=root, replay=lambda: None)
    assert sorted(path.name for path in marker.glob("*.holdout-opened")) == [
        f"{day.isoformat()}.holdout-opened" for day in held
    ]
    with pytest.raises(PermissionError, match="opened once"):
        study.open_holdout(first, dates=held, root=root, replay=lambda: None)


def test_the_command_walks_the_list_forward_and_opens_the_holdout_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "days.csv"
    digest = write_list(path, [f"{d},TEST" for d in DATES])
    loaded: list[date] = []

    def load(catalog: Path, symbol: str, day: date) -> Day:
        loaded.append(day)
        return on(day, SLOW)

    monkeypatch.setattr(study, "load_day", load)
    argv = ["--catalog", str(tmp_path), "--days-file", str(path), "--sha256", digest]
    assert study.main(argv) == 0
    out = capsys.readouterr().out
    assert "10 name-days on 10 dates, 7 development, 3 holdout; gross" in out
    assert out.count("**Fold ") == 6  # seven dates, blocks of one: each later date is judged
    assert "**Chosen on all development dates**: `run-1`" in out
    assert "Holdout" not in out and loaded == DATES[:7]  # the holdout's days are not even read
    assert study.main([*argv, "--open-holdout"]) == 0
    assert "**Holdout, opened once" in capsys.readouterr().out
    with pytest.raises(PermissionError):
        study.main([*argv, "--open-holdout"])
    for wrong in (["--day", "2026-09-01"], ["--day", "2026-09-01", "--symbols", "A", *argv[2:]]):
        with pytest.raises(SystemExit):
            study.main(["--catalog", str(tmp_path), *wrong])


def test_every_arm_is_printed_for_a_name_and_for_the_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(study, "load_day", lambda catalog, symbol, day: on(day, SLOW))
    argv = ["--catalog", str(tmp_path), "--day", "2026-09-01", "--symbols", "TEST", "--arms"]
    assert study.main(argv) == 0
    out = capsys.readouterr().out
    assert "| run-1 | 09:32 | 09:33 | 10.5000 | 1 |" in out
    assert "| registered | 09:32 | - | - | - |" in out
    assert "| run-1 | 1 of 1 | " in out


def test_a_top_up_s_own_interval_is_joined_to_the_one_before_it(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    before = date(2026, 11, 10)
    first = {
        before: minute_payloads(before, study.PART_START, [10.0], 100)
        + minute_payloads(before, 15 * 60 + 59, [10.0], 1),
        DAY: minute_payloads(DAY, OPEN, [10.1, 10.4, 10.5], 1000),
    }
    write_minutes(tmp_path, alpaca_bars, "TEST", first, OPEN + 3)
    equity = alpaca_bars.equity("TEST")
    bar_type = BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL")
    more = [
        parse_bar(AlpacaBar("TEST", payload), bar_type, equity)
        for payload in minute_payloads(DAY, OPEN + 3, [10.6, 10.7, 10.9, 10.8], 1000)
    ]
    ParquetDataCatalog(
        str(tmp_path)
    ).write_bars(  # as a later load writes it: the next nanosecond on
        more, start=study._ns(DAY, OPEN + 3) + 1, end=study._ns(DAY, OPEN + 30)
    )
    day = study.load_day(tmp_path, "TEST", DAY)
    assert day.covered_to == OPEN + 30
    assert len(day.bars) == 7


# --- what the review of 2026-11-11 found -----------------------------------------------------------


def test_the_running_normal_is_movers_stream_s_normal() -> None:
    earlier = tuple(
        tuple(bars(OPEN + i, [10.0, 10.2, 10.1], volume=100.0 * (i + 1))) for i in range(5)
    )
    day = a_day(RISE, earlier=earlier)
    sessions = [study.minute_dollars(s) for s in earlier]
    for seen in study.listing(day, REGISTERED):
        assert seen.normal == pytest.approx(normal(sessions, seen.minute - study.PART_START))
    assert study.listing(a_day(RISE), REGISTERED)[0].normal == pytest.approx(1000.0)


def test_the_first_trigger_is_under_the_entry_s_own_bid_across_the_open() -> None:
    costs = Costs(premarket=0.005, regular=0.001)
    early = study.REGULAR_OPEN - 10
    today = [
        Minute(early - 1, 10.0, 10.1, 10.0, 10.1, 100.0),
        *bars(early, [10.4, 10.5, 10.6, 10.7], first=10.1),
        Minute(OPEN + 1, 10.7, 10.7, 10.6, 10.65, 1000.0),  # the next bar is after the open
    ]
    run = replay_day(a_day(today), REGISTERED, costs)
    assert run.entry is not None and run.entry == pytest.approx(10.7 * 1.005)
    assert (
        run.exit is None
    )  # the low's bid, 10.6 x 0.999, is above 10.7 x 0.995 less 2% of the entry
    out = study.protect(today[-1:], run.entry, 10.7 * 0.995, costs)
    assert out is None


def test_a_limit_resting_at_the_floor_sells_at_a_later_open_above_it() -> None:
    entry = 10.0
    floor = (9.8 - entry * EXIT_THROUGH) * (1 - CHASE_CAP)
    deep = [Minute(600, 9.0, 9.1, 8.9, 9.0, 1.0), Minute(601, 9.9, 10.0, 9.9, 10.0, 1.0)]
    out = study.protect(deep, entry, entry, study.GROSS)
    assert out is not None and (out.why, out.price) == ("floor", 9.9)
    assert out.price > floor


def test_a_name_not_entered_counts_zero_in_the_tie_break() -> None:
    entered = replay_day(on(DATES[0], RISE), REGISTERED)
    never = replay_day(on(DATES[1], SLOW), REGISTERED)
    assert study.early_share([entered, never, never]) == (0.0, 0.0)
    assert study.early_share([entered])[1] == pytest.approx(0.2 / 0.9)


def test_a_part_segment_is_set_against_the_same_minutes_of_earlier_sessions() -> None:
    earlier = (
        tuple(Minute(study.PART_START + i, 10.0, 10.0, 10.0, 10.0, 10.0) for i in range(30)),
    )
    today = Day(
        "TEST", DAY, tuple(earlier[0][:20]), earlier, 10.0, covered_to=study.PART_START + 20
    )
    assert study.segments(today, 5) == [
        (study.PART_START, study.PART_START + 15, 1500.0, 1500.0),
        (study.PART_START + 15, study.PART_START + 20, 500.0, 500.0),
    ]
    assert "| 04:15 to 04:20 | 500 | 500 | 1.0 | 1.0 |" in study.section(
        today, REGISTERED, None, 240
    )


def test_no_reference_is_taken_from_a_session_not_covered_to_its_close(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    before = date(2026, 11, 10)
    equity = alpaca_bars.equity("TEST")
    bar_type = BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL")
    tmp_path.mkdir(exist_ok=True)
    catalog = ParquetDataCatalog(str(tmp_path))
    for day, start, until in ((before, 13 * 60, 13 * 60 + 22), (DAY, OPEN, OPEN + 30)):
        series = [
            parse_bar(AlpacaBar("TEST", payload), bar_type, equity)
            for payload in minute_payloads(day, start, [10.0, 10.1], 100)
        ]
        catalog.write_bars(series, start=study._ns(day, 0), end=study._ns(day, until))
    assert study.load_day(tmp_path, "TEST", DAY).missing == (
        "no daily bar for 2026-11-10, and its minute bars are not covered to 16:00"
    )


def test_a_holdout_that_cannot_be_replayed_stays_unopened_and_one_that_can_is_priced_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "days.csv"
    digest = write_list(path, [f"{d},TEST" for d in DATES])
    gone = Day("TEST", DAY, (), (), None, missing="no minute bars in the catalog")
    monkeypatch.setattr(
        study, "load_day", lambda catalog, symbol, day: on(day, SLOW) if day in DATES[:7] else gone
    )
    argv = ["--catalog", str(tmp_path), "--days-file", str(path), "--sha256", digest]
    with pytest.raises(LookupError, match="stays unopened"):
        study.main([*argv, "--open-holdout"])
    assert not study.opened_holdout(path)
    monkeypatch.setattr(study, "load_day", lambda catalog, symbol, day: on(day, SLOW))
    capsys.readouterr()
    assert study.main([*argv, "--open-holdout", "--half-spread-bps", "20", "10"]) == 0
    out = capsys.readouterr().out
    holdout = out[out.index("**Holdout, opened once") :]
    assert all(label in holdout for label in ("*gross*", "*costs x1*", "*costs x2*"))
    assert study.opened_holdout(path) == [d.isoformat() for d in DATES[7:]]
    with pytest.raises(SystemExit):
        study.main([*argv, "--from", "09:40"])


def test_an_overlapping_holdout_is_refused_by_each_date_already_opened(tmp_path: Path) -> None:
    """code-review-high-n2b/holdout-per-date: a subset, superset or overlap of dates already opened
    is refused; the once-only key is each held date, not a digest of one date set."""
    root = tmp_path / "holdouts"
    study.open_holdout(tmp_path / "a.csv", dates=DATES[-3:], root=root, replay=lambda: None)
    for held in (DATES[-2:], DATES[-4:-1], DATES[-4:]):
        with pytest.raises(PermissionError, match=DATES[-2].isoformat()):
            study.open_holdout(tmp_path / "b.csv", dates=held, root=root, replay=lambda: None)
    study.open_holdout(tmp_path / "c.csv", dates=DATES[:2], root=root, replay=lambda: None)


def test_an_arm_pending_at_an_opening_replays_once_at_its_commit_and_nothing_else_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lead's ruling of 2026-11-12 (ADR 0100): an opening writes a manifest of its arms, each with
    its registration commit, and may list one as registered with its replay pending. Only such an arm,
    at that commit, on exactly those dates, may replay later, once; anything else goes to the later
    holdout, and a replay that fails leaves the arm pending."""
    root, held = tmp_path / "holdouts", DATES[-3:]
    c1, c2, c3 = checkout(tmp_path / "repo", monkeypatch)
    arms = {
        "F-all": {"commit": c1, "status": study.REPLAYED},
        "move": {"commit": c2, "status": study.PENDING},
    }
    study.open_holdout(tmp_path / "a.csv", dates=held, root=root, replay=lambda: None, arms=arms)
    (manifest,) = root.glob("*.manifest.json")
    assert json.loads(manifest.read_text())["arms"] == arms

    def refused(arm: str, commit: str, dates=held) -> None:
        with pytest.raises(PermissionError, match="later holdout"):
            study.replay_pending_arm(
                arm, commit=commit, dates=dates, root=root, replay=lambda: None
            )

    refused("move", c3)  # a commit the checkout does not hold
    refused("move", c1)  # another commit the checkout holds
    refused("F-all", c1)  # replayed at the opening
    refused("rdb5", c2)  # never named
    refused("move", c2, DATES[-2:])  # not exactly the opening's dates

    def failure():
        raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError, match="replay failed"):
        study.replay_pending_arm("move", commit=c2, dates=held, root=root, replay=failure)
    ran = []
    study.replay_pending_arm(
        "move", commit=c2[:12], dates=held, root=root, replay=lambda: ran.append(1)
    )
    assert ran == [1]
    assert json.loads(manifest.read_text())["arms"]["move"]["status"].startswith("replayed later")
    refused("move", c2)  # once


def test_an_opening_manifest_refuses_an_arm_with_no_commit_or_an_unknown_status(
    tmp_path: Path,
) -> None:
    root, ran = tmp_path / "holdouts", []
    for bad in ({"status": study.PENDING}, {"commit": "c1", "status": "waiting"}):
        with pytest.raises(ValueError, match="arm move"):
            study.open_holdout(
                tmp_path / "a.csv",
                dates=DATES[-3:],
                root=root,
                replay=lambda: ran.append(1),
                arms={"move": bad},
            )
    assert ran == [] and not list(root.glob("*"))


def test_a_holdout_opened_beside_its_list_by_the_old_code_stays_opened(tmp_path: Path) -> None:
    """code-review-high-n2b/legacy-marker: a `<list>.holdout-opened` file from before per-date
    markers still refuses a second opening of that list's holdout."""
    path = tmp_path / "days.csv"
    write_list(path, [f"{d},TEST" for d in DATES])
    path.with_name(path.name + ".holdout-opened").write_text("opened earlier\n")
    _, held = study.split_dates(DATES)
    with pytest.raises(PermissionError, match="days.csv.holdout-opened"):
        study.open_holdout(path, dates=held, replay=lambda: None)
    assert study.opened_holdout(path, dates=held) == ["days.csv.holdout-opened"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def checkout(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """A checkout whose HEAD is its second commit, and a third commit on a branch HEAD does not hold;
    the study reads it as its own checkout."""
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    shas = []
    for name in ("first", "second"):
        git(repo, "commit", "-q", "--allow-empty", "-m", name)
        shas.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "commit", "-q", "--allow-empty", "-m", "side")
    shas.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(study, "REPOSITORY", repo)
    return shas[0], shas[1], shas[2]


def test_the_command_lists_pending_arms_at_commits_the_checkout_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lead's ruling of 2026-11-12 (ADR 0100): `--open-holdout` takes the arms registered with their
    replay pending, each at a commit that is the checkout's HEAD or an ancestor of it, and writes them
    with the chosen arm, replayed at HEAD, into the opening's manifest."""
    first, second, side = checkout(tmp_path / "repo", monkeypatch)
    monkeypatch.setattr(study, "load_day", lambda catalog, symbol, day: on(day, SLOW))

    def argv(name: str) -> list[str]:
        path = tmp_path / f"{name}.csv"
        digest = write_list(path, [f"{d},TEST" for d in DATES])
        return ["--catalog", str(tmp_path), "--days-file", str(path), "--sha256", digest]

    for pending, why in (
        (f"move={side}", "not the checkout's HEAD or an ancestor"),
        ("move=0000000", "names no commit"),
        (f"run-1={first}", "run-1 is the arm this opening replays"),
    ):
        with pytest.raises(ValueError, match=why):
            study.main([*argv("refused"), "--open-holdout", "--pending-arm", pending])
        assert not study.opened_holdout(tmp_path / "refused.csv")
    for wrong in (["--open-holdout", "--pending-arm", "move"], ["--pending-arm", f"move={first}"]):
        with pytest.raises(SystemExit):
            study.main([*argv("refused"), *wrong])
    capsys.readouterr()
    assert study.main(
        [*argv("days"), "--open-holdout", "--pending-arm", f"move={first[:10]}",
         "--pending-arm", "F-all=HEAD"]
    ) == 0  # fmt: skip
    assert "manifest" in capsys.readouterr().out
    (manifest,) = study.default_holdout_root().glob("*.manifest.json")
    assert json.loads(manifest.read_text())["arms"] == {
        "run-1": {"commit": second, "status": study.REPLAYED},
        "move": {"commit": first, "status": study.PENDING},
        "F-all": {"commit": second, "status": study.PENDING},
    }


def test_an_opening_whose_markers_failed_can_be_opened_again_with_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code-review-high-n2c/2: a manifest written before a date marker failed made every later
    attempt raise FileExistsError, so the holdout could never be marked opened."""
    root, held = tmp_path / "holdouts", DATES[-3:]
    _, head, _ = checkout(tmp_path / "repo", monkeypatch)
    arms = {"move": {"commit": head, "status": study.PENDING}}
    real = study._date_marker
    monkeypatch.setattr(
        study, "_date_marker", lambda base, day: tmp_path / "gone" / day.isoformat()
    )
    with pytest.raises(FileNotFoundError):
        study.open_holdout(
            tmp_path / "a.csv", dates=held, root=root, replay=lambda: None, arms=arms
        )
    monkeypatch.setattr(study, "_date_marker", real)
    study.open_holdout(tmp_path / "a.csv", dates=held, root=root, replay=lambda: None, arms=arms)
    (manifest,) = root.glob("*.manifest.json")
    assert json.loads(manifest.read_text())["arms"] == arms


def test_a_pending_replay_where_nothing_was_ever_opened_goes_to_the_later_holdout(
    tmp_path: Path,
) -> None:
    """code-review-high-n2c/8: a root no opening made raised FileNotFoundError, not the refusal."""
    with pytest.raises(PermissionError, match="later holdout"):
        study.replay_pending_arm(
            "move", commit="HEAD", dates=DATES[-3:], root=tmp_path / "none", replay=lambda: None
        )


def frozen_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A checkout holding a holdout list and an amendment at HEAD, and the commit before them."""
    repo = tmp_path / "repo"
    first, _, _ = checkout(repo, monkeypatch)
    (repo / "canon").mkdir()
    (repo / "canon" / "holdout.txt").write_text("# ADR 0092's held dates\n2026-10-16\n2026-11-10\n")
    (repo / "amend.md").write_text("the opening is a freeze, then one read\n")
    git(repo, "add", "canon/holdout.txt", "amend.md")
    git(repo, "commit", "-q", "-m", "list and amendment")
    return repo, first, git(repo, "rev-parse", "HEAD")


def freeze_argv(repo: Path, out: Path, first: str, bars: Path) -> list[str]:
    bars.write_text("the nightly baseline read held-date bars for its baselines\n")
    return [
        "--freeze", str(out),
        "--holdout-list", str(repo / "canon" / "holdout.txt"),
        "--amendments", str(repo / "amend.md"),
        "--population-rule", "ADR 0092's measure and window over 60 sessions",
        "--admitted", f"run-1={first}",
        "--pending-arm", f"F-all={first}",
        "--pending-arm", "F-move=HEAD",
        "--bars-read", str(bars),
        "--not-admitted", "BV1=registered, not ready at the opening (ADR 0096:63-64)",
        "--reason", "pending=ADR 0100's arms, replay pending at their registration",
    ]  # fmt: skip


def test_a_freeze_names_the_admitted_arms_and_hashes_without_a_list_or_a_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lead's decision of 2026-11-12 (05:4x, the 09:30 freeze): the opening freezes which arms are
    admitted, with HEAD's commit, the holdout list's and the amendment's hashes, the population rule by
    citation and what read holdout bars; it takes no --days-file and reads no catalog, and is written
    once."""
    repo, first, head = frozen_checkout(tmp_path, monkeypatch)

    def no_catalog(*_: object) -> None:
        raise AssertionError("a freeze reads no catalog")

    monkeypatch.setattr(study, "load_day", no_catalog)
    out = tmp_path / "freeze.json"
    argv = freeze_argv(repo, out, first, tmp_path / "bars.txt")
    assert study.main(argv) == 0
    assert "frozen" in capsys.readouterr().out
    record = json.loads(out.read_text())
    listed = (repo / "canon" / "holdout.txt").read_bytes()
    assert record["commit"] == head
    assert record["holdout_list"] == {
        "path": "canon/holdout.txt",
        "sha256": hashlib.sha256(listed).hexdigest(),
    }
    assert record["amendments"]["path"] == "amend.md"
    assert record["population_rule"] == "ADR 0092's measure and window over 60 sessions"
    pending = "ADR 0100's arms, replay pending at their registration"
    assert record["arms"] == {
        "run-1": {"commit": first, "status": study.ADMITTED},
        "F-all": {"commit": first, "status": study.PENDING, "reason": pending},
        "F-move": {"commit": head, "status": study.PENDING, "reason": pending},
        "BV1": {
            "status": study.NOT_ADMITTED,
            "reason": "registered, not ready at the opening (ADR 0096:63-64)",
        },
    }
    assert record["nothing_read"] == study.NOTHING_READ
    assert record["bars_read"] == ["the nightly baseline read held-date bars for its baselines"]
    assert (
        record["bars_read_sha256"]
        == hashlib.sha256((tmp_path / "bars.txt").read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        study.main(argv)  # written once
    for wrong in (
        ["--admitted", f"F-all={first}"],  # admitted and pending at once
        ["--not-admitted", "F-move=late"],  # pending and left out at once
        ["--reason", "other=text"],
        ["--days-file", str(out), "--sha256", "0" * 64],
        ["--catalog", str(tmp_path)],
        ["--from", "05:00"],  # review of the freeze, finding 5: taken and ignored
        ["--half-spread-bps", "1", "2"],
    ):
        with pytest.raises((SystemExit, ValueError)):
            study.main([*freeze_argv(repo, tmp_path / "other.json", first, tmp_path / "b"), *wrong])
    assert not (tmp_path / "other.json").exists()
    (repo / "amend.md").write_text("changed after its commit\n")
    with pytest.raises(ValueError, match="amend.md differs from its form at"):
        study.main(freeze_argv(repo, tmp_path / "third.json", first, tmp_path / "b"))


def test_the_later_read_verifies_the_freezes_hashes_and_refuses_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lead's decision of 2026-11-12 (the 09:30 freeze): the one read of holdout outcomes verifies
    the freeze first and refuses on any mismatch: the freeze must be tracked, unchanged and committed
    once, its commit a full SHA the checkout holds, every arm of a known status, and the holdout list
    must hash as frozen both at the commit and now; the amendment is checked at the commit, since its
    record may be edited later (review of the freeze, findings 1, 3, 4 and 6)."""
    repo, first, _ = frozen_checkout(tmp_path, monkeypatch)
    out = repo / "canon" / "freeze.json"
    assert study.main(freeze_argv(repo, out, first, tmp_path / "bars.txt")) == 0
    with pytest.raises(PermissionError, match="not tracked"):
        study.verify_freeze(out)
    git(repo, "add", "canon/freeze.json")
    git(repo, "commit", "-q", "-m", "the freeze")
    assert study.verify_freeze(out)["arms"]["F-all"]["status"] == study.PENDING
    (repo / "amend.md").write_text("a later routine edit of the amended record\n")
    assert study.verify_freeze(out)["commit"]  # the amendment is checked at the commit
    (repo / "amend.md").write_text("the opening is a freeze, then one read\n")
    kept = out.read_text()

    def refused(record: dict, match: str) -> None:
        out.write_text(json.dumps(record))
        with pytest.raises(PermissionError, match=match):
            study.verify_freeze(out)
        out.write_text(kept)

    record = json.loads(kept)
    refused({**record, "arms": {**record["arms"], "BV1": {"status": "admitted", "commit": first}}},
            "differs from its committed form")  # fmt: skip
    out.write_text(json.dumps({**record, "population_rule": "another"}))
    git(repo, "commit", "-q", "-am", "an edited freeze")
    with pytest.raises(PermissionError, match="committed more than once"):
        study.verify_freeze(out)
    before = git(repo, "rev-parse", "HEAD~2")  # the list and amendment, before the freeze
    for change, match in (
        ({"commit": "HEAD"}, "not a full SHA"),
        ({"arms": {"X": {"status": "maybe", "commit": first}}}, "unknown status"),
        ({"holdout_list": None}, "malformed"),
        ({"arms": []}, "malformed"),  # second read of the freeze fixes, 1
        ({"holdout_list": {"path": 5, "sha256": "0" * 64}}, "malformed"),  # second read, 2
        ({"amendments": {**record["amendments"], "sha256": "0" * 64}}, "amend.md"),
    ):
        git(repo, "reset", "-q", "--hard", before)
        out.write_text(json.dumps({**record, **change}))
        git(repo, "add", "canon/freeze.json")
        git(repo, "commit", "-q", "-m", "a malformed freeze")
        with pytest.raises(PermissionError, match=match):
            study.verify_freeze(out)
    git(repo, "reset", "-q", "--hard", before)
    out.write_text(kept)
    git(repo, "add", "canon/freeze.json")
    git(repo, "commit", "-q", "-m", "the freeze")
    assert study.verify_freeze(out)
    listed = repo / "canon" / "holdout.txt"
    listed.write_text(listed.read_text() + "2026-11-11\n")
    with pytest.raises(PermissionError, match="canon/holdout.txt in the checkout"):
        study.verify_freeze(out)
    listed.write_text("# ADR 0092's held dates\n2026-10-16\n2026-11-10\n")
    # second read of the freeze fixes, 3: a path git would read as a glob counts only its own commits
    globbed = repo / "canon" / "freeze[1].json"
    globbed.write_text(kept)
    git(repo, "add", "canon/freeze[1].json")
    git(repo, "commit", "-q", "-m", "a freeze whose name has brackets")
    (repo / "canon" / "freeze1.json").write_text("another file\n")
    git(repo, "add", "canon/freeze1.json")
    git(repo, "commit", "-q", "-m", "a file the glob would match")
    assert study.verify_freeze(globbed)
