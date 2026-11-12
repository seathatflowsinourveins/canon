"""Offline tests of `canon_trading.nightly_study` (ADR 0089): one firing studies the latest recorded
night once, on 1-minute bars shaped as the recorder writes them, and records what the gate said.

The bars are built as the adapter builds them (`parse_bar`: `ts_event` at the minute's open,
`ts_init` at its close) on the fixture's instrument. The first night runs from 14:00 to 20:00 New
York on 2026-11-11, as the recorder's first day did, so it spans a UTC midnight and holds two UTC
session days; the second night runs from 09:30 to 20:00 on 2026-11-12.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

import numpy as np
import optuna
import pytest
from alpaca.data.models import Bar as AlpacaBar
from canon_alpaca.data import NEW_YORK
from canon_alpaca.data import parse_bar
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from skfolio import measures

from canon_trading import nightly_study
from canon_trading.ledger import Sample
from canon_trading.ledger import open_study
from canon_trading.ledger import samples
from canon_trading.nightly_study import ATTEMPTS
from canon_trading.nightly_study import DRY_RUN
from canon_trading.nightly_study import FAILED
from canon_trading.nightly_study import GATE_FIX_FILE
from canon_trading.nightly_study import GATE_FIX_TEST
from canon_trading.nightly_study import MIN_SESSIONS
from canon_trading.nightly_study import NOT_YET
from canon_trading.nightly_study import NOTHING_NEW
from canon_trading.nightly_study import PASSED
from canon_trading.nightly_study import RUNNING
from canon_trading.nightly_study import WAITING
from canon_trading.nightly_study import Night
from canon_trading.nightly_study import entry
from canon_trading.nightly_study import last_close
from canon_trading.nightly_study import main
from canon_trading.nightly_study import run_nightly
from canon_trading.nightly_study import verdict
from canon_trading.nightly_study import windows_to
from canon_trading.ranking import RankingReport
from canon_trading.ranking import RankingStopped
from canon_trading.variant_study import PERIODS
from canon_trading.variant_study import SYMBOLS
from canon_trading.variant_study import key_of

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import AlpacaBars

optuna.logging.set_verbosity(optuna.logging.WARNING)

PLAN = replace(DRY_RUN, study="night", periods=((2, 5), (3, 8)))
FIRST_OPEN = datetime(2026, 11, 11, 14, 0, tzinfo=NEW_YORK)
FIRST_CLOSE = datetime(2026, 11, 11, 20, 0, tzinfo=NEW_YORK)
SECOND_OPEN = datetime(2026, 11, 12, 9, 30, tzinfo=NEW_YORK)
SECOND_CLOSE = datetime(2026, 11, 12, 20, 0, tzinfo=NEW_YORK)
AFTER_FIRST = datetime(2026, 11, 11, 20, 20, tzinfo=NEW_YORK)
AFTER_SECOND = datetime(2026, 11, 12, 20, 20, tzinfo=NEW_YORK)


def closes(count: int, wave: int) -> list[float]:
    """A zigzag of `count` closes around 100 with a period of `wave` minutes, in cents."""
    return [round(100 + abs(i % wave - wave / 2) * 0.1, 2) for i in range(count)]


def write_minutes(
    alpaca_bars: AlpacaBars, path: Path, opened: datetime, closed: datetime, *, joined: bool = True
) -> None:
    """Write each symbol's minute bars from `opened` to `closed` to the catalog at `path`.

    `joined` records the bars as covering from the catalog's last bar on, the way the recorder's
    consolidation leaves a night; otherwise from their own first bar, a hole behind them.
    """
    path.mkdir(exist_ok=True)
    catalog = ParquetDataCatalog(str(path))
    count = int((closed - opened) / timedelta(minutes=1))
    for symbol, wave in zip(SYMBOLS, (40, 26), strict=True):
        listed = alpaca_bars.equity(symbol)
        bar_type = PLAN.bar_type(symbol)
        bars, previous = [], 100.0
        for i, close in enumerate(closes(count, wave)):
            at = (opened + timedelta(minutes=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {"t": at, "o": previous, "h": max(previous, close), "l": min(previous, close)}
            payload.update(c=close, v=10_000, n=100, vw=close)
            bars.append(parse_bar(AlpacaBar(symbol, payload), bar_type, listed))
            previous = close
        held = catalog.get_intervals(NautilusDataType.Bar, str(bar_type))
        if not held:
            catalog.write_instruments([listed])
        start = max(hi for _, hi in held) + 1 if held and joined else None
        catalog.write_bars(bars, start=start)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout that holds the test of the gate's fix, as `main` does once the fix has landed."""
    root = tmp_path / "checkout"
    (root / GATE_FIX_FILE).parent.mkdir(parents=True)
    (root / GATE_FIX_FILE).write_text(f"def {GATE_FIX_TEST}(flag: bool) -> None: ...\n")
    return root


def fire(tmp_path: Path, checkout: Path, now: datetime, **kwargs: Any) -> Night:
    return run_nightly(
        PLAN,
        catalog_path=tmp_path / "catalog",
        ledger_path=tmp_path / "ledger" / "trials.db",
        now=now,
        checkout=checkout,
        **kwargs,
    )


def report(**changes: Any) -> RankingReport:
    fields: dict[str, Any] = {
        "study": "night",
        "sample": "sample 0",
        "ranked_sample": Sample(0, 1, 2),
        "records": 4,
        "all_records": 4,
        "first_trial": 0,
        "last_trial": 3,
        "first_recorded": "2026-11-11",
        "last_recorded": "2026-11-11",
        "n": 4,
        "t": 40,
        "v": 0.01,
        "first_day": "2026-11-11",
        "last_day": "2026-12-31",
        "pick": key_of(PLAN.configs()[0]),
        "tied": (),
        "pick_trials": (0,),
        "sharpe": 0.4,
        "dsr": 0.97,
        "passed": True,
        "n_hat": None,
        "dsr_at_n_hat": None,
    }
    return RankingReport(**{**fields, **changes})


@pytest.mark.parametrize(
    ("now", "close"),
    [
        (datetime(2026, 11, 11, 20, 4, 59, tzinfo=NEW_YORK), datetime(2026, 11, 10, 20, 0)),
        (datetime(2026, 11, 11, 20, 5, tzinfo=NEW_YORK), datetime(2026, 11, 11, 20, 0)),
        (datetime(2026, 11, 12, 3, 0, tzinfo=NEW_YORK), datetime(2026, 11, 11, 20, 0)),
        (datetime(2026, 11, 12, 1, 5, tzinfo=UTC), datetime(2026, 11, 11, 20, 0)),
        # Across the change of clocks on 2026-11-01 the close is still 20:00 on the wall.
        (datetime(2026, 11, 1, 12, 0, tzinfo=NEW_YORK), datetime(2026, 10, 31, 20, 0)),
    ],
)
def test_the_cutoff_is_the_latest_2000_new_york_five_minutes_old(
    now: datetime, close: datetime
) -> None:
    assert last_close(now) == close.replace(tzinfo=NEW_YORK)
    assert last_close(now).utcoffset() == close.replace(tzinfo=NEW_YORK).utcoffset()


def test_the_dry_run_is_the_plan_of_adr_0089() -> None:
    configs = DRY_RUN.configs()

    assert DRY_RUN.study == "nightly-dry-run-ma-cross-1-minute-spy-qqq"
    assert (DRY_RUN.symbols, DRY_RUN.periods) == (SYMBOLS, PERIODS)
    assert [(str(c.bar_type), c.fast_period, c.slow_period) for c in configs] == [
        (f"{symbol}.ALPACA-1-MINUTE-LAST-EXTERNAL", fast, slow)
        for symbol in ("SPY", "QQQ")
        for fast, slow in ((10, 20), (5, 20), (10, 50), (20, 100))
    ]
    assert len({key_of(c) for c in configs}) == 8
    assert DRY_RUN.first.isoformat() == "2026-11-11T00:00:00-05:00"
    assert DRY_RUN.adopts is False
    assert MIN_SESSIONS == 5


def test_the_first_night_creates_the_study_on_the_recorded_range_and_is_not_yet(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)

    night = fire(tmp_path, checkout, AFTER_FIRST)

    assert night.status == NOT_YET, night.summary
    assert (night.day, night.planned, night.recorded) == ("2026-11-11", 4, 4)
    # The night spans a UTC midnight, so T is 2, and on two sessions skfolio refuses the moments the
    # gate would compute with (ADR 0080): the gate stops, which the night records as not yet, with
    # the gate's own words.
    # One New York day of bars is under the floor of sessions, which is said first.
    assert night.why.startswith(
        f"too few sessions: 1 New York day of bars is below the floor of {MIN_SESSIONS}; the "
        "ranking stopped: The ranking of study 'night' (N = 4) stopped"
    )
    assert "so the gate cannot compute with them (ADR 0080)" in night.summary
    assert (night.sample, night.n, night.t, night.dsr, night.sessions) == (0, None, None, None, 1)
    assert night.adopts is False
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        [sample] = samples(study)
        assert len(study.trials) == 4
    first_bar_closes = dt_to_unix_nanos(FIRST_OPEN + timedelta(minutes=1))
    assert (sample.start, sample.end) == (first_bar_closes, dt_to_unix_nanos(FIRST_CLOSE))
    out = tmp_path / "ledger" / "nightly"
    latest = json.loads((out / "latest.json").read_text())
    assert latest == json.loads((out / "night" / "2026-11-11.json").read_text())
    assert (latest["status"], latest["planned"], latest["sample"]) == (NOT_YET, 4, 0)
    assert latest["line"].startswith(
        "nightly study 2026-11-11 (night): 4 configurations planned, not yet: too few sessions: 1 "
    )
    heartbeat = json.loads((out / "last-firing.json").read_text())
    assert (heartbeat["status"], heartbeat["line"]) == (NOT_YET, night.line)
    # The heartbeat is stamped when it is written, by the clock, not with the firing's `now`.
    assert abs(heartbeat["epoch"] - time.time()) < 120
    written = (out / "night" / "2026-11-11.md").read_text()
    assert written == entry(night)
    assert written.startswith("## 2026-11-11, night\n")
    assert (
        "4 configurations planned, 1 New York day of bars. Verdict: not yet: too few sessions: 1 "
        "New York day of bars is below the floor"
    ) in written
    assert "**Rows changed.** none." in written
    assert str(tmp_path) not in written


def test_a_second_firing_on_a_studied_night_does_nothing(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    first = fire(tmp_path, checkout, AFTER_FIRST)
    latest = (tmp_path / "ledger" / "nightly" / "latest.json").read_text()

    again = fire(tmp_path, checkout, AFTER_FIRST + timedelta(minutes=30))

    assert again.status == NOTHING_NEW
    assert again.line == f"{first.line} (nothing new since)"
    assert (tmp_path / "ledger" / "nightly" / "latest.json").read_text() == latest
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        assert (len(samples(study)), len(study.trials)) == (1, 4)
    heartbeat = json.loads((tmp_path / "ledger" / "nightly" / "last-firing.json").read_text())
    assert heartbeat["status"] == NOTHING_NEW


def test_the_full_dry_run_cycle_uses_a_temporary_ledger_and_adopts_nothing(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_path = tmp_path / "catalog"
    ledger_path = tmp_path / "rehearsal" / "trials.db"
    # Five trading days, each with the winter after-hours hour across UTC midnight: seven UTC
    # days in all (11, 12, 13, 14, 16, 17, 18). Every run, trial and gate calculation is real.
    for day in (11, 12, 13, 16, 17):
        opened = datetime(2026, 11, day, 14, 0, tzinfo=NEW_YORK)
        write_minutes(alpaca_bars, catalog_path, opened, opened + timedelta(hours=6))
    windows = tmp_path / "windows"
    windows.mkdir()
    (windows / "window-20261117T150000Z.json").write_text(json.dumps({"result": "ok", "fills": 2}))

    def no_default_ledger() -> Path:
        pytest.fail("the dry-run test must never resolve the default ledger")

    monkeypatch.setattr(nightly_study, "default_ledger_path", no_default_ledger)
    now = datetime(2026, 11, 17, 20, 20, tzinfo=NEW_YORK)

    night = run_nightly(
        DRY_RUN,
        catalog_path=catalog_path,
        ledger_path=ledger_path,
        windows_dir=windows,
        now=now,
        checkout=checkout,
    )

    assert night.status in (PASSED, NOT_YET), night.summary
    assert (night.day, night.planned, night.recorded, night.already_run) == (
        "2026-11-17",
        8,
        8,
        0,
    )
    assert (night.n, night.t, night.sessions, night.sample) == (8, 7, 5, 0)
    assert night.run_failures == []
    assert night.dsr is not None and math.isfinite(night.dsr)
    assert night.returns["count"] == 7.0
    assert (night.adopts, night.rows_changed) == (False, [])
    assert (night.paper["ran"], night.paper["ok"], night.paper["fills"]) == (1, 1, 2)
    # Reading the saved window is part of the cycle; fill prices are still a stated unwired limit.
    assert night.paper["fill_gap"] == nightly_study.NO_FILL_PRICES
    with open_study(DRY_RUN.study, ledger_path) as study:
        [sample] = samples(study)
        assert len(study.trials) == 8
        assert len({t.user_attrs["config_key"] for t in study.trials}) == 8
        assert [t.state.name for t in study.trials] == ["COMPLETE"] * 8
    assert (sample.start, sample.end) == (
        dt_to_unix_nanos(FIRST_OPEN + timedelta(minutes=1)),
        dt_to_unix_nanos(datetime(2026, 11, 17, 20, 0, tzinfo=NEW_YORK)),
    )
    out = ledger_path.parent / "nightly"
    latest = (out / "latest.json").read_text()
    saved = json.loads(latest)
    assert saved == json.loads((out / DRY_RUN.study / "2026-11-17.json").read_text())
    assert (saved["n"], saved["t"], saved["adopts"], saved["rows_changed"]) == (8, 7, False, [])
    written = (out / DRY_RUN.study / "2026-11-17.md").read_text()
    assert "N = 8 counted from the ledger" in written
    assert "5 New York days of bars" in written
    assert "**Rows changed.** none." in written
    assert json.loads((out / "last-firing.json").read_text())["status"] == night.status

    again = run_nightly(
        DRY_RUN,
        catalog_path=catalog_path,
        ledger_path=ledger_path,
        windows_dir=windows,
        now=now + timedelta(minutes=30),
        checkout=checkout,
    )

    assert again.status == NOTHING_NEW
    assert (out / "latest.json").read_text() == latest
    with open_study(DRY_RUN.study, ledger_path) as study:
        assert (len(samples(study)), len(study.trials)) == (1, 8)
    assert json.loads((out / "last-firing.json").read_text())["status"] == NOTHING_NEW


def test_the_next_night_moves_the_sample_and_runs_every_configuration_again(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    fire(tmp_path, checkout, AFTER_FIRST)
    write_minutes(alpaca_bars, tmp_path / "catalog", SECOND_OPEN, SECOND_CLOSE)

    # Before 20:05 New York the second day's bars are not read: the first night is still the last.
    early = fire(tmp_path, checkout, datetime(2026, 11, 12, 19, 0, tzinfo=NEW_YORK))
    night = fire(tmp_path, checkout, AFTER_SECOND)

    assert early.status == NOTHING_NEW
    assert night.status == NOT_YET, night.summary
    assert (night.day, night.sample, night.recorded, night.already_run) == ("2026-11-12", 1, 4, 0)
    assert (night.n, night.t) == (4, 3)  # N is the four keys, not the eight records
    # Two New York days are three UTC session days, since the first night spans a UTC midnight,
    # and on those the gate itself passes the pick (DSR 0.9536 on generated bars): the floor of
    # sessions, counted in New York days, is what makes the night not yet. The pick's Sharpe ratio
    # is shown with the count and spread of the returns it was taken of, beside its DSR.
    assert night.gate_passed is True
    assert night.dsr == pytest.approx(0.9536, abs=5e-5)
    assert night.sessions == 2
    assert night.why == (
        f"too few sessions: 2 New York days of bars are below the floor of {MIN_SESSIONS}; the "
        "gate's T is 3 UTC days and its DSR 0.9536, which would pass at 0.95"
    )
    assert "T=3 over 2 New York days" in night.line
    assert night.returns["count"] == 3.0
    assert night.returns["least"] <= night.returns["mean"] <= night.returns["greatest"]
    assert night.sharpe == pytest.approx(night.returns["mean"] / night.returns["sd"])
    shown = f"Sharpe {night.sharpe:.4g} per session (3 session returns from "
    assert shown in night.line
    assert f"DSR {night.dsr:.4f}, not yet: " in night.line
    assert shown.replace("Sharpe", "Sharpe ratio") in entry(night)
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        first, second = samples(study)
        assert len(study.trials) == 8
    assert (second.start, second.end) == (first.start, dt_to_unix_nanos(SECOND_CLOSE))
    out = tmp_path / "ledger" / "nightly" / "night"
    assert sorted(p.name for p in out.iterdir()) == [
        "2026-11-11.json",
        "2026-11-11.md",
        "2026-11-12.json",
        "2026-11-12.md",
    ]


def test_a_night_the_catalog_does_not_cover_yet_waits_and_moves_nothing(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    fire(tmp_path, checkout, AFTER_FIRST)
    latest = (tmp_path / "ledger" / "nightly" / "latest.json").read_text()
    write_minutes(alpaca_bars, tmp_path / "catalog", SECOND_OPEN, SECOND_CLOSE, joined=False)

    night = fire(tmp_path, checkout, AFTER_SECOND)

    assert night.status == WAITING
    assert "does not cover SPY.ALPACA-1-MINUTE-LAST-EXTERNAL" in night.why
    assert "the recorder has not closed the night's holes" in night.why
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        assert (len(samples(study)), len(study.trials)) == (1, 4)
    # The last studied night stays the latest record; the heartbeat says what this firing found.
    assert (tmp_path / "ledger" / "nightly" / "latest.json").read_text() == latest
    heartbeat = json.loads((tmp_path / "ledger" / "nightly" / "last-firing.json").read_text())
    assert heartbeat["status"] == WAITING

    # The recorder's consolidation closes the night's hole (ADR 0041 term 1), and the night runs.
    catalog = ParquetDataCatalog(str(tmp_path / "catalog"))
    for symbol in SYMBOLS:
        catalog.consolidate_data(NautilusDataType.Bar, str(PLAN.bar_type(symbol)))
    assert fire(tmp_path, checkout, AFTER_SECOND + timedelta(minutes=30)).status == NOT_YET


def test_a_chosen_cutoff_is_for_a_rehearsal_on_a_temporary_ledger_only(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    mid_session = datetime(2026, 11, 11, 16, 0, tzinfo=NEW_YORK)

    night = fire(tmp_path, checkout, mid_session, cutoff=mid_session)

    # Without a cutoff, 16:00 on the first day reads up to the evening before: no bar, so it waits.
    assert night.status == NOT_YET, night.summary
    assert (night.day, night.end, night.sessions) == ("2026-11-11", "2026-11-11 21:00 UTC", 1)
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        [sample] = samples(study)
    assert sample.end == dt_to_unix_nanos(mid_session)
    waits = run_nightly(
        PLAN,
        catalog_path=tmp_path / "catalog",
        ledger_path=tmp_path / "other" / "trials.db",
        now=mid_session,
        checkout=checkout,
    )
    assert waits.status == WAITING

    # On the default ledger a chosen cutoff is refused before anything is read or written: its
    # samples end at a session's close.
    monkeypatch.setattr(
        nightly_study, "default_ledger_path", lambda: tmp_path / "real" / "trials.db"
    )
    for ledger in (None, tmp_path / "real" / "trials.db"):
        with pytest.raises(
            ValueError, match="a chosen cutoff is for a rehearsal on a temporary ledger"
        ):
            run_nightly(
                PLAN,
                catalog_path=tmp_path / "catalog",
                ledger_path=ledger,
                now=mid_session,
                cutoff=mid_session,
                checkout=checkout,
            )
    assert not (tmp_path / "real").exists()


def test_a_chosen_cutoff_is_refused_on_the_default_ledger_reached_through_a_link(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    mid_session = datetime(2026, 11, 11, 16, 0, tzinfo=NEW_YORK)
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    # The default ledger as `ledger.default_ledger_path` gives it, unresolved: a clone whose path
    # passes through a link (/tmp, a linked home) names it by the link.
    monkeypatch.setattr(
        nightly_study, "default_ledger_path", lambda: tmp_path / "link" / "trials.db"
    )

    with pytest.raises(
        ValueError, match="a chosen cutoff is for a rehearsal on a temporary ledger"
    ):
        run_nightly(
            PLAN,
            catalog_path=tmp_path / "catalog",
            now=mid_session,
            cutoff=mid_session,
            checkout=checkout,
        )
    assert list((tmp_path / "real").iterdir()) == []


def test_the_paper_windows_are_counted_to_the_night_s_close_not_to_its_last_bar(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    # The last common bar closes at 19:50 New York; the night's stretch still ends at 20:00.
    write_minutes(
        alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE - timedelta(minutes=10)
    )
    records = tmp_path / "windows"
    records.mkdir()
    for stamp in (
        "20261112T005500Z",  # 19:55 on the 11th: this night's
        "20261111T005500Z",  # 19:55 on the 10th: the night before's
    ):
        (records / f"window-{stamp}.json").write_text(json.dumps({"result": "ok", "fills": 1}))

    night = fire(tmp_path, checkout, AFTER_FIRST, windows_dir=records)

    assert night.day == "2026-11-11", night.summary
    assert night.paper is not None
    assert (night.paper["to"], night.paper["ran"]) == ("2026-11-11 20:00 New York", 1)


def test_no_catalog_or_no_bars_waits_and_creates_no_ledger(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    nothing = fire(tmp_path, checkout, AFTER_FIRST)
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    too_early = fire(tmp_path, checkout, FIRST_CLOSE)  # 20:00, before the five minutes

    assert (nothing.status, too_early.status) == (WAITING, WAITING)
    assert nothing.why.startswith("there is no catalog at ")
    # A firing that failed before it knew its night says so without a date.
    undated = Night(study="night", status=FAILED, why="OSError: unreadable", planned=4)
    assert nightly_study._line(undated) == (
        "nightly study (night): 4 configurations planned, failed: OSError: unreadable"
    )
    assert "holds no SPY.ALPACA-1-MINUTE-LAST-EXTERNAL bars" in too_early.why
    assert not (tmp_path / "ledger" / "trials.db").exists()
    latest = json.loads((tmp_path / "ledger" / "nightly" / "latest.json").read_text())
    assert latest["status"] == WAITING
    assert latest["line"].startswith("nightly study (night): waiting: ")


def test_without_the_gate_s_fix_in_the_checkout_nothing_is_ranked(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    (checkout / GATE_FIX_FILE).write_text("def test_another() -> None: ...\n")

    night = fire(tmp_path, checkout, AFTER_FIRST)

    assert night.status == WAITING
    assert GATE_FIX_TEST in night.why
    assert not (tmp_path / "ledger" / "trials.db").exists()
    assert fire(tmp_path, tmp_path / "no-checkout", AFTER_FIRST).status == WAITING


@pytest.mark.parametrize("studied", [False, True])
def test_an_unreadable_gate_prerequisite_fails_by_name_instead_of_waiting(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    studied: bool,
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    out = tmp_path / "ledger" / "nightly"
    if studied:
        assert fire(tmp_path, checkout, AFTER_FIRST).status == NOT_YET
    latest = (out / "latest.json").read_text() if studied else None
    read_text = type(checkout).read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == checkout / GATE_FIX_FILE:
            raise PermissionError("the gate prerequisite cannot be read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(checkout), "read_text", unreadable)

    night = fire(tmp_path, checkout, AFTER_FIRST)

    assert night.status == FAILED
    assert "PermissionError" in night.why
    assert str(GATE_FIX_FILE) in night.why
    assert "the gate prerequisite cannot be read" in night.why
    assert "has no" not in night.why
    heartbeat = json.loads((out / "last-firing.json").read_text())
    assert (heartbeat["status"], heartbeat["line"]) == (FAILED, night.line)
    if studied:
        assert (out / "latest.json").read_text() == latest
        with open_study("night", tmp_path / "ledger" / "trials.db") as study:
            assert len(study.trials) == 4
    else:
        assert not (tmp_path / "ledger" / "trials.db").exists()
        assert json.loads((out / "latest.json").read_text())["status"] == FAILED


def test_the_verdict_only_ever_narrows_the_gate_s() -> None:
    adopting = replace(PLAN, adopts=True)

    assert verdict(adopting, report(), 40) == (PASSED, "the gate passes the pick")
    status, why = verdict(PLAN, report(), 40)
    assert (status, PLAN.read_as in why) == (PASSED, True)
    assert verdict(adopting, report(passed=False, dsr=0.9338), 40) == (
        NOT_YET,
        "the pick's DSR 0.9338 is below 0.95",
    )
    # Too few sessions is said first, whatever the gate's figure, which is shown beside it. The
    # sessions are New York days of bars: the gate's T counts UTC days, and a winter day leaves an
    # hour on the next one, so T reaches the floor before the days do.
    few = MIN_SESSIONS - 1
    too_few = f"too few sessions: {few} New York days of bars are below the floor of {MIN_SESSIONS}"
    assert verdict(adopting, report(t=2 * few, dsr=1.0), few) == (
        NOT_YET,
        f"{too_few}; the gate's T is {2 * few} UTC days and its DSR 1.0000, which would pass at 0.95",
    )
    assert verdict(adopting, report(t=few, passed=False, dsr=0.41), few) == (
        NOT_YET,
        f"{too_few}; the gate's T is {few} UTC days and its DSR 0.4100, which rejects at 0.95",
    )
    stopped = RankingStopped("The ranking stopped:\n* k: never traded")
    assert verdict(adopting, stopped, few) == (
        NOT_YET,
        f"{too_few}; the ranking stopped: The ranking stopped:",
    )
    # The gate's own T under the floor is too few as well, however many days hold bars.
    assert verdict(adopting, report(t=few), 40)[1].startswith(
        f"too few sessions: the gate's T is {few} UTC days, below the floor of {MIN_SESSIONS}"
    )
    assert verdict(adopting, report(t=MIN_SESSIONS), MIN_SESSIONS)[0] == PASSED
    # A pass for a pick that does not beat holding flat is a defect of the gate, never a pass.
    for sharpe in (0.0, -0.3, float("nan")):
        for sessions in (few, 40):
            status, why = verdict(adopting, report(sharpe=sharpe, dsr=1.0, t=sessions), sessions)
            assert status == FAILED
            assert "not above zero" in why
    # A rejected pick with such a ratio is the gate working: not yet, never failed.
    assert verdict(adopting, report(sharpe=-0.3, passed=False, dsr=0.01), 40)[0] == NOT_YET
    assert verdict(adopting, stopped, 40) == (NOT_YET, "the ranking stopped: The ranking stopped:")


def test_a_failed_night_is_tried_again_and_then_left(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    calls = []

    def raises(*args: object, **kwargs: object) -> object:
        calls.append(kwargs["create"])
        # While the pass runs, the night's own record says a firing began, with its attempt.
        began = json.loads(
            (tmp_path / "ledger" / "nightly" / "night" / "2026-11-11.json").read_text()
        )
        assert (began["status"], began["attempt"]) == (RUNNING, len(calls))
        raise RuntimeError(f"the engine stopped at {tmp_path / 'home' / 'catalog'}\nand more")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with monkeypatch.context() as patched:
        patched.setattr(nightly_study.variant_study, "run_study", raises)
        nights = [
            fire(tmp_path, checkout, AFTER_FIRST + timedelta(minutes=30 * i))
            for i in range(ATTEMPTS + 1)
        ]

    assert [n.status for n in nights] == [FAILED] * ATTEMPTS + [NOTHING_NEW]
    assert [n.attempt for n in nights[:ATTEMPTS]] == list(range(1, ATTEMPTS + 1))
    # The home folder reads as ~ in what the status row and the session brief print.
    assert nights[0].why == "RuntimeError: the engine stopped at ~/catalog"
    assert str(tmp_path) not in nights[0].line
    assert "Traceback" in nights[0].summary
    assert f"failed {ATTEMPTS} times" in nights[-1].line
    assert len(calls) == ATTEMPTS
    out = tmp_path / "ledger" / "nightly"
    assert json.loads((out / "latest.json").read_text())["status"] == FAILED
    assert not (out / "night" / "2026-11-11.md").exists()  # a failed night has no entry to land


@pytest.mark.parametrize(
    ("damage", "cause"),
    [
        ("malformed", "JSONDecodeError"),
        ("non-object", "JSON object"),
        ("permission", "PermissionError"),
        ("missing-attempt", "attempt"),
        ("invalid-attempt", "attempt"),
        ("missing-range", "end_ns"),
    ],
)
def test_an_unreadable_attempt_record_fails_without_resetting_or_overwriting_it(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    cause: str,
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    record = tmp_path / "ledger" / "nightly" / "night" / "2026-11-11.json"
    record.parent.mkdir(parents=True)
    # An interrupted third attempt is already the bound, even with no ledger created yet.
    previous: dict[str, Any] = {
        "end_ns": dt_to_unix_nanos(FIRST_CLOSE),
        "status": RUNNING,
        "attempt": 3,
    }
    if damage == "missing-attempt":
        del previous["attempt"]
    elif damage == "invalid-attempt":
        previous["attempt"] = "unreadable"
    elif damage == "missing-range":
        del previous["end_ns"]
    broken = {"malformed": "{", "non-object": "[]"}.get(damage, json.dumps(previous))
    record.write_text(broken)
    read_text = type(record).read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == record and damage == "permission":
            raise PermissionError("the attempt record cannot be read")
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(type(record), "read_text", unreadable)
        nights = [fire(tmp_path, checkout, AFTER_FIRST) for _ in range(2)]

    for night in nights:
        assert night.status == FAILED
        assert record.name in night.why
        assert cause in night.why
    assert record.read_text() == broken
    assert not (tmp_path / "ledger" / "trials.db").exists()
    out = tmp_path / "ledger" / "nightly"
    heartbeat = json.loads((out / "last-firing.json").read_text())
    assert (heartbeat["status"], heartbeat["line"]) == (FAILED, nights[-1].line)


@pytest.mark.parametrize("broken", ["{", "[]"])
def test_an_unreadable_latest_record_fails_without_overwriting_it(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, broken: str
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    latest = tmp_path / "ledger" / "nightly" / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(broken)

    night = fire(tmp_path, checkout, AFTER_FIRST)

    assert night.status == FAILED
    assert latest.name in night.why
    assert latest.read_text() == broken
    assert not (tmp_path / "ledger" / "trials.db").exists()
    assert json.loads((latest.parent / "last-firing.json").read_text())["status"] == FAILED


def test_a_run_that_raised_leaves_the_night_failed_so_the_next_firing_runs_it(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    real = nightly_study.variant_study.run_study

    def one_failed(*args: Any, **kwargs: Any) -> Any:
        recorded, _ = real(*args, **kwargs)
        assert recorded is not None
        failed = replace(recorded, failed=("SPY 2/5: RuntimeError('engine')",))
        return failed, RankingStopped("The ranking stopped:\n* k: its latest record failed")

    with monkeypatch.context() as patched:
        patched.setattr(nightly_study.variant_study, "run_study", one_failed)
        night = fire(tmp_path, checkout, AFTER_FIRST)
    again = fire(tmp_path, checkout, AFTER_FIRST + timedelta(minutes=30))

    assert night.status == FAILED
    assert night.why == "1 runs raised: SPY 2/5: RuntimeError('engine')"
    assert (again.status, again.attempt, again.already_run) == (NOT_YET, 2, 4)


def test_a_catalog_that_lost_bars_is_refused_even_back_to_a_night_already_studied(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    fire(tmp_path, checkout, AFTER_FIRST)
    first_night = tmp_path / "first-night"
    write_minutes(alpaca_bars, first_night, FIRST_OPEN, FIRST_CLOSE)
    write_minutes(alpaca_bars, tmp_path / "catalog", SECOND_OPEN, SECOND_CLOSE)
    fire(tmp_path, checkout, AFTER_SECOND)
    out = tmp_path / "ledger" / "nightly"
    kept = {p.name: p.read_text() for p in [out / "latest.json", *(out / "night").iterdir()]}
    shorter = tmp_path / "shorter"
    write_minutes(alpaca_bars, shorter, FIRST_OPEN, FIRST_CLOSE - timedelta(minutes=30))

    for lost in (first_night, shorter):  # back to the first night's end, and to before any night's
        night = run_nightly(
            PLAN,
            catalog_path=lost,
            ledger_path=tmp_path / "ledger" / "trials.db",
            now=AFTER_SECOND,
            checkout=checkout,
        )

        assert night.status == FAILED
        assert "the catalog lost bars" in night.why
        # No night's record is touched: the last night studied stays the latest.
        assert {
            p.name: p.read_text() for p in [out / "latest.json", *(out / "night").iterdir()]
        } == kept
        heartbeat = json.loads((out / "last-firing.json").read_text())
        assert (heartbeat["status"], "the catalog lost bars" in heartbeat["line"]) == (FAILED, True)
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        assert len(samples(study)) == 2


@pytest.mark.parametrize("remaining", ["no-catalog", "no-bars", "one-bar", "lost-start"])
def test_loss_of_the_studied_catalog_fails_before_it_can_be_reported_as_waiting(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, remaining: str
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    assert fire(tmp_path, checkout, AFTER_FIRST).status == NOT_YET
    out = tmp_path / "ledger" / "nightly"
    kept = {p.name: p.read_text() for p in [out / "latest.json", *(out / "night").iterdir()]}
    lost = tmp_path / "lost"
    if remaining == "no-bars":
        lost.mkdir()
    elif remaining == "one-bar":
        write_minutes(alpaca_bars, lost, FIRST_OPEN, FIRST_OPEN + timedelta(minutes=1))
    elif remaining == "lost-start":
        write_minutes(alpaca_bars, lost, FIRST_OPEN + timedelta(hours=1), FIRST_CLOSE)

    night = run_nightly(
        PLAN,
        catalog_path=lost,
        ledger_path=tmp_path / "ledger" / "trials.db",
        now=AFTER_FIRST + timedelta(minutes=30),
        checkout=checkout,
    )

    assert night.status == FAILED
    assert "the catalog lost bars" in night.why
    assert "catalog" in night.why
    assert {
        p.name: p.read_text() for p in [out / "latest.json", *(out / "night").iterdir()]
    } == kept
    heartbeat = json.loads((out / "last-firing.json").read_text())
    assert (heartbeat["status"], heartbeat["line"]) == (FAILED, night.line)
    with open_study("night", tmp_path / "ledger" / "trials.db") as study:
        [sample] = samples(study)
        assert (sample.number, sample.end, len(study.trials)) == (
            0,
            dt_to_unix_nanos(FIRST_CLOSE),
            4,
        )


def test_a_firing_killed_in_its_pass_is_counted_as_an_attempt(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)

    def killed(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt  # as a timeout's signal ends the process: nothing after it runs

    with monkeypatch.context() as patched:
        patched.setattr(nightly_study.variant_study, "run_study", killed)
        for _ in range(ATTEMPTS):
            with pytest.raises(KeyboardInterrupt):
                fire(tmp_path, checkout, AFTER_FIRST)
        left = fire(tmp_path, checkout, AFTER_FIRST)
        began = json.loads(
            (tmp_path / "ledger" / "nightly" / "night" / "2026-11-11.json").read_text()
        )

    assert (began["status"], began["attempt"]) == (RUNNING, ATTEMPTS)
    assert left.status == NOTHING_NEW
    assert f"failed {ATTEMPTS} times" in left.line
    assert not (tmp_path / "ledger" / "nightly" / "latest.json").exists()


def test_a_failure_before_the_night_is_known_leaves_the_last_night_s_record(
    alpaca_bars: AlpacaBars, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minutes(alpaca_bars, tmp_path / "catalog", FIRST_OPEN, FIRST_CLOSE)
    out = tmp_path / "ledger" / "nightly"

    def unreadable(*args: object, **kwargs: object) -> object:
        raise OSError("a file the recorder is writing")

    with monkeypatch.context() as patched:
        patched.setattr(nightly_study, "recorded_range", unreadable)
        before_any = fire(tmp_path, checkout, AFTER_FIRST)
        undated = json.loads((out / "latest.json").read_text())
    studied = fire(tmp_path, checkout, AFTER_FIRST)
    latest = (out / "latest.json").read_text()
    with monkeypatch.context() as patched:
        patched.setattr(nightly_study, "recorded_range", unreadable)
        after = fire(tmp_path, checkout, AFTER_FIRST + timedelta(minutes=30))

    # With no night studied yet, the failure is the latest record; a studied night then replaces it.
    assert (before_any.status, undated["status"], undated["day"]) == (FAILED, FAILED, "")
    assert studied.status == NOT_YET
    # Once a night is studied, a failure that names no night leaves its record: the firing's exit
    # code and its heartbeat say what failed, and the next firing that works clears both.
    assert after.status == FAILED
    assert after.line == (
        "nightly study (night): 4 configurations planned, failed: OSError: a file the recorder "
        "is writing"
    )
    assert (out / "latest.json").read_text() == latest
    heartbeat = json.loads((out / "last-firing.json").read_text())
    assert (heartbeat["status"], heartbeat["line"]) == (FAILED, after.line)


def false_passes(
    lengths: tuple[int, ...], rounds: int, trials: int = 8, seed: int = 20261111
) -> dict[int, tuple[float, float]]:
    """For each T of `lengths`, in order and from one generator: the share of `rounds` in which the
    pick of `trials` series of T independent standard normal returns with no edge reaches a
    deflated Sharpe ratio of 0.95, and the share in which skfolio's value is not finite.

    The statistic is taken as `canon_trading.ranking.rank` takes it: each Sharpe ratio the mean
    over the sample standard deviation, the pick the highest, and
    `skfolio.measures.deflated_sharpe_ratio(pick's returns, every ratio)`. ADR 0089's table is
    `false_passes((2, 3, 4, 5, 10, 20, 30, 60, 250), 40_000)`.
    """
    rng = np.random.default_rng(seed)
    found = {}
    for length in lengths:
        passed = unreadable = 0
        for _ in range(rounds):
            returns = rng.standard_normal((trials, length)) * 0.01
            sharpes = [float(series.mean() / series.std(ddof=1)) for series in returns]
            pick = [float(value) for value in returns[int(np.argmax(sharpes))]]
            dsr = float(measures.deflated_sharpe_ratio(pick, sharpes))
            unreadable += not math.isfinite(dsr)
            passed += math.isfinite(dsr) and dsr >= nightly_study.THRESHOLD
        found[length] = (passed / rounds, unreadable / rounds)
    return found


def test_the_floor_of_sessions_sits_where_the_measured_false_pass_rate_is_under_the_nominal() -> (
    None
):
    found = false_passes((2, 3, 4, MIN_SESSIONS), rounds=2_000)

    # On two returns skfolio's statistic is never a number: the gate stops, it does not pass.
    assert found[2] == (0.0, 1.0)
    # On three, the pick of eight series with no edge reaches 0.95 more often than the nominal 5%.
    assert 0.06 < found[3][0] < 0.12
    # At the floor the rate is under it, and every value is a number from three sessions on.
    assert 0.01 < found[MIN_SESSIONS][0] < 0.045
    assert [found[length][1] for length in (3, 4, MIN_SESSIONS)] == [0.0, 0.0, 0.0]


def test_the_paper_windows_of_the_24_hours_to_the_night_s_end_are_counted(tmp_path: Path) -> None:
    records = tmp_path / "windows"
    records.mkdir()
    for stamp, result, fills in (
        ("20261111T143000Z", "ok", 2),  # 09:30 New York on the 11th
        ("20261111T040000Z", "ok", 8),  # 23:00 on the 10th: the overnight session before the 11th
        ("20261112T005500Z", "running", 0),  # 19:55 on the 11th, not yet ended
        (
            "20261111T010000Z",
            "failed: not left flat: ['SPY']",
            1,
        ),  # 20:00 on the 10th: the night before
        ("20261112T010000Z", "failed: no market data", 1),  # 20:00 on the 11th exactly: counted
        ("20261112T020000Z", "ok", 4),  # 21:00 on the 11th: the next night's
        ("20261110T143000Z", "ok", 16),  # the 10th
    ):
        (records / f"window-{stamp}.json").write_text(
            json.dumps({"result": result, "fills": fills})
        )
    (records / "window-20261111T150000Z.json").write_text("{")
    (records / "latest.json").write_text("{}")

    found = windows_to(records, FIRST_CLOSE)

    assert found == {
        "to": "2026-11-11 20:00 New York",
        "ran": 4,
        "ok": 2,
        "failed": 1,
        "running": 1,
        "fills": 11,
        "unreadable": 1,
        "fill_gap_bps": None,
        "fill_gap": nightly_study.NO_FILL_PRICES,
    }
    assert windows_to(None, FIRST_CLOSE)["fill_gap"].startswith("not read")


def test_main_prints_the_night_s_line_and_exits_by_its_status(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fired(**kwargs: Any) -> Night:
        seen.update(kwargs)
        return Night(
            study="night", status=seen.get("status", NOT_YET), line="the line", summary="s"
        )

    monkeypatch.setattr(nightly_study, "run_nightly", fired)
    ledger = tmp_path / "main" / "ledger" / "trials.db"

    assert main(["--ledger", str(ledger), "--windows", str(tmp_path / "w")]) == 0
    assert capsys.readouterr().out == "the line\n"
    # The recorder's catalog is `catalog/sip` of the checkout that holds the ledger.
    assert seen["catalog_path"] == tmp_path / "main" / "catalog" / "sip"
    assert (seen["ledger_path"], seen["windows_dir"]) == (ledger, tmp_path / "w")
    seen["status"] = FAILED
    assert main(["--ledger", str(ledger), "--catalog", str(tmp_path / "c")]) == 1
    assert seen["catalog_path"] == tmp_path / "c"
    assert capsys.readouterr().err == "s\n"
