"""`canon_trading.early_entry_study` on generated bars: the listing minute, the first-rising-run entry and
its refusals, the trailing exit walked open, high, low, close, ADR 0092's measures, and a catalog read.

Offline. The catalog tests write Alpaca-shaped minute bars through the adapter's `parse_bar` into a
`ParquetDataCatalog` in pytest's temporary directory, as `conftest.AlpacaBars` does for daily bars.
"""

from __future__ import annotations

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
    out = study.protect(after, entry, study.GROSS)
    assert out is not None
    assert (out.why, out.minute, out.raises) == ("trail", 601, 1)
    assert out.first_trigger == pytest.approx(entry * (1 - TRAIL))
    assert out.price == pytest.approx(10.5 - entry * TRAIL)


def test_an_open_under_the_trigger_sells_at_the_open_or_rests_at_the_chase_s_floor() -> None:
    entry, trigger = 10.0, 9.8
    limit = trigger - entry * EXIT_THROUGH
    floor = limit * (1 - CHASE_CAP)
    gap = study.protect([Minute(600, 9.7, 9.7, 9.6, 9.6, 1.0)], entry, study.GROSS)
    assert gap is not None and (gap.why, gap.price) == ("gap", 9.7)
    deep = [Minute(600, 9.0, 9.1, 8.9, 9.0, 1.0), Minute(601, 9.0, 9.7, 9.0, 9.6, 1.0)]
    rest = study.protect(deep, entry, study.GROSS)
    assert rest is not None and (rest.why, rest.minute) == ("floor", 602)
    assert rest.price == pytest.approx(floor)
    assert study.protect(deep[:1], entry, study.GROSS) is None  # still held at the window's end


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
    assert "watching from 09:34" in out


def test_the_arms_are_the_fifteen_the_record_registers() -> None:
    assert len(study.ARMS) == 15
    assert next(iter(study.ARMS.values())) is REGISTERED
    assert len(set(study.ARMS.values())) == 15
    for rows in study.ARMS.values():
        assert rows.max_move is None  # the no-chase cap from the reference is off in every arm
        replay_day(a_day(RISE), rows)
