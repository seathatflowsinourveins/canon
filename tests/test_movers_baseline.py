"""ADR 0087's nightly baseline writer, against temporary catalogs and no network.

The tiny market has two ALPACA equities. File intervals are the requested coverage, as history_load
writes them; bars are parsed by the adapter, including its open/end timestamps. Calendar dates are
explicit inputs: neither weekdays nor absent bars establish an exchange session.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tomllib
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time

import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.trading.models import Asset
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import history_load
from canon_trading import movers_rank
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.session_window import NEW_YORK
from canon_trading.session_window import SOURCE_ROOT

SESSION = date(2026, 11, 11)
DAYS = tuple(date(2026, 11, d) for d in (4, 5, 6, 9, 10))
CALENDAR = (date(2026, 11, 3), *DAYS, SESSION)


def writer():
    return importlib.import_module("canon_trading.movers_baseline")


def stamp(day: date, clock: time) -> int:
    return dt_to_unix_nanos(datetime.combine(day, clock, NEW_YORK))


def minute(equity, day: date, clock: time, high: float, low: float, close: float, volume: int):
    return parse_bar(
        AlpacaBar(
            symbol=equity.id.symbol.value,
            raw_data={
                "t": datetime.combine(day, clock, NEW_YORK).astimezone(UTC).isoformat(),
                "o": close,
                "h": high,
                "l": low,
                "c": close,
                "v": volume,
                "n": 1,
                "vw": 999,  # deliberately differs from the typical price the ranking uses
            },
        ),
        BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL"),
        equity,
    )


def market(tmp_path, alpaca_bars, *, feed="sip", missing=None, empty=None):
    root = tmp_path / feed
    root.mkdir()
    (root / FEED_FILE).write_text(feed + "\n", encoding="ascii")
    catalog = ParquetDataCatalog(str(root))
    equities = [alpaca_bars.equity(symbol) for symbol in ("AAPL", "QQQ")]
    catalog.write_instruments(equities)
    for equity in equities:
        for i, day in enumerate(DAYS, 1):
            if (equity.id.symbol.value, day) in _pairs(missing):
                continue
            # A 03:59 bar can prove a request was covered even when no trade occurred after 04:00.
            bars = [minute(equity, day, time(3, 59), 900, 900, 900, 100)]
            if (equity.id.symbol.value, day) != empty:
                bars.extend(
                    [
                        minute(equity, day, time(4), 12, 9, 9, 3 * i),
                        minute(equity, day, time(4, 14), 15, 9, 12, 2 * i),
                        minute(equity, day, time(4, 15), 18, 9, 18, i),
                        minute(equity, day, time(19, 59), 6, 3, 6, 3 * i),
                    ]
                )
            catalog.write_bars(bars, start=stamp(day, time(4)), end=stamp(day, time(20)))
    return root


def _pairs(given):
    if given is None:
        return set()
    return {given} if isinstance(given[0], str) else set(given)


def build(root, output, *, feed="sip", sessions=CALENDAR, session=SESSION, **kwargs):
    return writer().build_baseline(
        session=session,
        sessions=sessions,
        feed=feed,
        catalog_path=root,
        baseline_dir=output,
        **kwargs,
    )


def test_written_baselines_round_trip_for_the_coming_session(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars)
    path = build(root, tmp_path / "baselines")
    assert path == tmp_path / "baselines" / "sip.json"
    result = movers_rank.read_baselines(path, SESSION)
    assert result.problem is None
    assert set(result.baselines) == {"AAPL", "QQQ"}
    assert all(b.sessions == DAYS for b in result.baselines.values())
    assert movers_rank.read_baselines(path, date(2026, 11, 12)).problem is not None


def test_numbers_match_typical_price_cumulative_segments_by_hand(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars)
    path = build(root, tmp_path / "baselines")
    baseline = movers_rank.read_baselines(path, SESSION).baselines["AAPL"]
    # First segment: (12+9+9)/3*3 + (15+9+12)/3*2 = 54 per unit of i.
    # Second: + (18+9+18)/3 = +15; final minute: +(6+3+6)/3*3 = +15.
    # mean i = (1+2+3+4+5)/5 = 3; the 03:59 minute, before sip's hours, contributes nothing, and the
    # 32 overnight segments from 20:00 the evening before hold zero.
    assert baseline.cumulative == pytest.approx((*([0] * 32), 162, *([207] * 62), 252))
    assert movers_rank.baseline_at(baseline, time(4, 7, 30)) == pytest.approx(81)
    assert movers_rank.baseline_at(baseline, time(4, 22, 30)) == pytest.approx(184.5)
    assert movers_rank.baseline_at(baseline, time(20)) == pytest.approx(252)


def test_an_uncovered_name_is_left_out_and_named_while_the_others_are_written(
    tmp_path, alpaca_bars
):
    """history_load writes nothing for an empty answer, so a name with no trade is never covered: it is
    left out by name, not a stop for every other name (L6 review W1, coordinator's decision)."""
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[2]))
    path = build(root, tmp_path / "baselines")
    result = movers_rank.read_baselines(path, SESSION)
    assert result.problem is None
    assert set(result.baselines) == {"AAPL"}
    document = json.loads(path.read_text())
    assert set(document["excluded"]) == {"QQQ"}
    assert all(text in document["excluded"]["QQQ"] for text in ("sip", "2026-11-06"))
    assert document["excluded_share"] == pytest.approx(0.5)
    source = document["source"]
    assert source["feed"] == "sip"
    assert source["sessions"] == [d.isoformat() for d in DAYS]
    assert source["equities"] == 2
    assert source["catalog"] == str(root.resolve())


def test_a_sip_baseline_is_covered_and_counted_from_0400_and_says_it_holds_no_night(
    tmp_path, alpaca_bars
):
    """Version 2's part runs from 20:00 the evening before (ADR 0087), but sip has no overnight session. A
    sip session is covered when the root holds its minute ends 04:01-20:00, so an uncovered night does
    not leave a name out; a bar before 04:00 counts nowhere; and the header's `feeds` say sip only, so
    `read_baselines` reports no overnight dollars and the ranking counts its day ratio from 04:00 (the
    lead's rule, 2026-11-11)."""
    root = market(tmp_path, alpaca_bars)
    equity = alpaca_bars.equity("AAPL")
    evening = date(2026, 11, 9)  # 20:00 on 11-09 opens the part of DAYS[-1], 11-10
    ParquetDataCatalog(str(root)).write_bars(
        [minute(equity, evening, time(21), 50, 50, 50, 1000)],
        start=stamp(evening, time(20, 30)),
        end=stamp(evening, time(22)),
    )
    path = build(root, tmp_path / "baselines")
    document = json.loads(path.read_text())
    assert document["feeds"] == ["sip"]
    assert document["segments"] == movers_rank.SEGMENTS == 96
    read = movers_rank.read_baselines(path, SESSION)
    assert read.problem is None and read.overnight is False
    assert set(read.baselines) == {"AAPL", "QQQ"}
    aapl = read.baselines["AAPL"]
    assert aapl.cumulative[:32] == (0.0,) * 32
    assert aapl.cumulative == read.baselines["QQQ"].cumulative


def test_a_name_left_out_gets_no_rank(tmp_path, alpaca_bars):
    """A name left out is absent from what `read_baselines` gives the ranking; `rank` gives a name with
    no baseline no ratio and excludes it as `ratio:unknown`, which
    tests/test_movers_rank.py::test_a_name_with_no_ratio_says_so_and_is_not_eligible proves."""
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[2]))
    baselines = movers_rank.read_baselines(build(root, tmp_path / "baselines"), SESSION).baselines
    assert "QQQ" not in baselines
    assert (movers_rank.baseline_at(baselines["AAPL"], time(4, 7, 30)) or 0) > 0


def test_above_the_excluded_share_it_stops_by_name_and_keeps_the_previous_file(
    tmp_path, alpaca_bars
):
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[2]))
    output = tmp_path / "baselines"
    output.mkdir()
    path = output / "sip.json"
    path.write_text("previous baseline", encoding="utf-8")
    with pytest.raises(history_load.HistoryStopped) as exc:
        build(root, output, max_excluded_share=0.4)
    assert all(text in str(exc.value) for text in ("1 of 2", "sip", "QQQ", "2026-11-06"))
    assert path.read_text() == "previous baseline"


def test_no_covered_name_is_never_a_baseline(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars, missing=[("QQQ", DAYS[2]), ("AAPL", DAYS[0])])
    with pytest.raises(history_load.HistoryStopped, match="2 of 2"):
        build(root, tmp_path / "baselines", max_excluded_share=0.99)
    assert not (tmp_path / "baselines").exists()


def test_a_partial_session_is_missing_even_if_it_has_bars(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[-1]))
    equity = alpaca_bars.equity("QQQ")
    ParquetDataCatalog(str(root)).write_bars(
        [minute(equity, DAYS[-1], time(4), 10, 10, 10, 100)],
        start=stamp(DAYS[-1], time(4)),
        end=stamp(DAYS[-1], time(4, 15)),
    )
    path = build(root, tmp_path / "baselines")
    assert "2026-11-10" in json.loads(path.read_text())["excluded"]["QQQ"]


def test_a_covered_empty_session_counts_zero_in_the_five_day_mean(tmp_path, alpaca_bars):
    """The state history_load makes: one request over two sessions answered with bars on the first only
    is written with the request's bounds, so the second is covered and has no bar."""
    root = market(tmp_path, alpaca_bars, missing=[("AAPL", DAYS[-2]), ("AAPL", DAYS[-1])])
    equity = alpaca_bars.equity("AAPL")
    ParquetDataCatalog(str(root)).write_bars(
        [
            minute(equity, DAYS[-2], time(4), 12, 9, 9, 3 * 4),
            minute(equity, DAYS[-2], time(4, 14), 15, 9, 12, 2 * 4),
            minute(equity, DAYS[-2], time(4, 15), 18, 9, 18, 4),
            minute(equity, DAYS[-2], time(19, 59), 6, 3, 6, 3 * 4),
        ],
        start=stamp(DAYS[-2], time(4)),
        end=stamp(DAYS[-1], time(20)),
    )
    path = build(root, tmp_path / "baselines")
    baseline = movers_rank.read_baselines(path, SESSION).baselines["AAPL"]
    assert baseline.sessions == DAYS
    assert baseline.cumulative[32] == pytest.approx(108)  # 54*(1+2+3+4+0)/5, at 04:00-04:15
    assert baseline.cumulative[-1] == pytest.approx(168)


def test_the_sip_file_leaves_another_feeds_file_alone(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars, feed="sip")
    output = tmp_path / "baselines"
    output.mkdir()
    other = output / "boats.json"
    other.write_text("other feed", encoding="utf-8")
    path = build(root, output, feed="sip")
    assert path.name == "sip.json"
    assert movers_rank.read_baselines(path, SESSION).problem is None
    assert other.read_text() == "other feed"


def test_boats_is_refused_by_name_until_a_boats_history_loader_exists(tmp_path, alpaca_bars):
    """ADR 0087 now defines the overnight segments (20:00-04:00), but ADR 0093's loader reads sip only,
    so no boats root is loaded the way this writer needs: refused by name (the lead's ruling,
    2026-11-11)."""
    root = market(tmp_path, alpaca_bars, feed="boats")
    with pytest.raises(history_load.HistoryStopped, match="boats.*history loader"):
        build(root, tmp_path / "baselines", feed="boats")
    assert not (tmp_path / "baselines").exists()


@pytest.mark.parametrize("marker", [None, "iex", "boats"])
def test_unlabelled_or_other_feed_catalog_is_refused(tmp_path, alpaca_bars, marker):
    root = market(tmp_path, alpaca_bars)
    if marker is None:
        (root / FEED_FILE).unlink()
    else:
        (root / FEED_FILE).write_text(marker + "\n", encoding="ascii")
    with pytest.raises(history_load.HistoryStopped, match="feed"):
        build(root, tmp_path / "baselines")
    assert not (tmp_path / "baselines").exists()


@pytest.mark.parametrize(
    "sessions",
    [DAYS, (*DAYS[1:], SESSION), (*DAYS, DAYS[-1], SESSION), (*reversed(DAYS), SESSION)],
    ids=["target-not-in-calendar", "too-few", "duplicate", "out-of-order"],
)
def test_unusable_calendars_stop_instead_of_guessing_sessions(tmp_path, alpaca_bars, sessions):
    root = market(tmp_path, alpaca_bars)
    with pytest.raises(history_load.HistoryStopped, match="session|calendar"):
        build(root, tmp_path / "baselines", sessions=sessions)
    assert not (tmp_path / "baselines").exists()


def test_catalog_lock_excludes_history_writer_until_baseline_is_finished(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars)
    with (
        history_load._catalog_lock(root),
        pytest.raises(history_load.HistoryStopped, match="catalog"),
    ):
        build(root, tmp_path / "baselines")
    assert not (tmp_path / "baselines").exists()


def test_a_missing_catalog_is_named_without_creating_it(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(history_load.HistoryStopped, match="catalog"):
        build(root, tmp_path / "baselines")
    assert not root.exists()


def test_no_equities_is_not_a_whole_market_baseline(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / FEED_FILE).write_text("sip\n", encoding="ascii")
    with pytest.raises(history_load.HistoryStopped, match="equity|equities|instrument"):
        build(root, tmp_path / "baselines")


def test_cli_writes_from_explicit_calendar_without_any_client(tmp_path, alpaca_bars, capsys):
    root = market(tmp_path, alpaca_bars)
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("# Calendar supplied by operator\n" + "\n".join(map(str, CALENDAR)))
    output = tmp_path / "baselines"
    assert (
        writer().main(
            [
                "--session",
                str(SESSION),
                "--sessions-file",
                str(calendar),
                "--feed",
                "sip",
                "--catalog",
                str(root),
                "--baseline-dir",
                str(output),
            ]
        )
        == 0
    )
    assert movers_rank.read_baselines(output / "sip.json", SESSION).problem is None
    assert "sip.json" in capsys.readouterr().out


def test_cli_names_missing_history_and_returns_one(tmp_path, alpaca_bars, capsys):
    root = market(tmp_path, alpaca_bars, missing=("AAPL", DAYS[0]))
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("\n".join(map(str, CALENDAR)))
    assert (
        writer().main(
            [
                "--session",
                str(SESSION),
                "--sessions-file",
                str(calendar),
                "--catalog",
                str(root),
                "--baseline-dir",
                str(tmp_path / "baselines"),
                "--max-excluded-share",
                "0.4",
            ]
        )
        == 1
    )
    assert "2026-11-04" in capsys.readouterr().err


def test_cli_writes_and_counts_what_it_left_out(tmp_path, alpaca_bars, capsys):
    root = market(tmp_path, alpaca_bars, missing=("AAPL", DAYS[0]))
    calendar = tmp_path / "sessions.txt"
    calendar.write_text("\n".join(map(str, CALENDAR)))
    argv = ["--session", str(SESSION), "--sessions-file", str(calendar), "--catalog", str(root)]
    assert writer().main([*argv, "--baseline-dir", str(tmp_path / "baselines")]) == 0
    assert "1 equities excluded" in capsys.readouterr().out


def test_mise_baseline_mode_routes_before_credential_access(tmp_path):
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    sent = tmp_path / "sent"
    security = tmp_path / "security"
    security.write_text("#!/bin/sh\necho forbidden-credential-access >&2\nexit 99\n")
    security.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    timeout.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BASELINE_SENT"\n')
    uv.chmod(0o700)
    # Replace external process boundaries so even the pre-feature route cannot read a real key.
    script = (
        task["run"]
        .replace("/usr/bin/security", str(security))
        .replace("/usr/bin/timeout", str(timeout))
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{tmp_path}:{os.defpath}",
            "BASELINE_SENT": str(sent),
            "usage_account": "tests",
            "usage_mode": "baseline",
            "usage_session": str(SESSION),
            "usage_sessions_file": "/calendar.txt",
            "usage_feed": "boats",
            "usage_catalog": "/catalog/boats",
            "usage_baseline_dir": "/catalog/movers-baseline",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "credential" not in completed.stderr
    args = sent.read_text().splitlines()
    assert args[:6] == [
        "run",
        "--locked",
        "python",
        "-m",
        "canon_trading.movers_baseline",
        "--session",
    ]
    assert args[6:] == [
        str(SESSION),
        "--sessions-file",
        "/calendar.txt",
        "--feed",
        "boats",
        "--catalog",
        "/catalog/boats",
        "--baseline-dir",
        "/catalog/movers-baseline",
    ]


def test_mise_baseline_mode_defaults_the_catalog_to_the_feed_root(tmp_path):
    """W2: with no --catalog the baseline reads catalog/<feed>, never the catalog root."""
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    sent = tmp_path / "sent"
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    timeout.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BASELINE_SENT"\n')
    uv.chmod(0o700)
    for feed, share in (("boats", ""), ("sip", "0.3")):
        env = {
            "PATH": f"{tmp_path}:{os.defpath}",
            "BASELINE_SENT": str(sent),
            "usage_account": "tests",
            "usage_mode": "baseline",
            "usage_session": str(SESSION),
            "usage_sessions_file": "/calendar.txt",
            "usage_feed": feed,
        }
        if share:
            env["usage_max_excluded_share"] = share
        completed = subprocess.run(
            ["bash", "-c", task["run"].replace("/usr/bin/timeout", str(timeout))],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        args = sent.read_text().splitlines()
        assert args[args.index("--catalog") + 1] == f"catalog/{feed}"
        assert ("--max-excluded-share" in args) == bool(share)


def test_each_name_carries_the_sessions_it_traded_in_each_segment(tmp_path, alpaca_bars):
    """The lead's ADR 0087 definition: a segment's baseline is established only where the name traded
    in it in every lookback session, so the file carries that count per segment and the lookback."""
    root = market(tmp_path, alpaca_bars)
    document = json.loads(build(root, tmp_path / "baselines").read_text())
    assert document["lookback_sessions"] == movers_rank.BASELINE_SESSIONS == 5
    traded = document["symbols"]["AAPL"]["traded_sessions"]
    assert len(traded) == movers_rank.SEGMENTS
    # Bars open 04:00 and 04:14 (segment 32), 04:15 (segment 33) and 19:59 (segment 95) every session;
    # the 03:59 bar is before sip's hours and counts nowhere.
    assert traded[32] == traded[33] == traded[95] == 5
    assert sum(traded) == 15


def test_a_covered_session_with_no_trade_is_not_counted_as_traded(tmp_path, alpaca_bars):
    root = market(tmp_path, alpaca_bars, missing=[("AAPL", DAYS[-2]), ("AAPL", DAYS[-1])])
    equity = alpaca_bars.equity("AAPL")
    ParquetDataCatalog(str(root)).write_bars(
        [minute(equity, DAYS[-2], time(4), 12, 9, 9, 12)],
        start=stamp(DAYS[-2], time(4)),
        end=stamp(DAYS[-1], time(20)),
    )
    traded = json.loads(build(root, tmp_path / "baselines").read_text())["symbols"]["AAPL"]
    assert traded["traded_sessions"][32] == 4  # three full sessions plus 04:00 on the fourth
    assert traded["traded_sessions"][33] == traded["traded_sessions"][95] == 3
    assert movers_rank.segment_trade_counts({DAYS[0]: []}, SESSION) == (0,) * movers_rank.SEGMENTS


def test_a_session_not_yet_published_stops_before_reading(tmp_path, alpaca_bars, monkeypatch):
    root = market(tmp_path, alpaca_bars)
    last_end = datetime.combine(DAYS[-1], time(20), NEW_YORK).astimezone(UTC)
    monkeypatch.setattr(history_load, "utc_now", lambda: last_end)
    with pytest.raises(history_load.HistoryStopped, match="2026-11-10 is not complete"):
        build(root, tmp_path / "baselines")
    assert not (tmp_path / "baselines").exists()


def test_every_version_of_an_equity_counts_once(tmp_path, alpaca_bars):
    """Each asset-list load writes another version, and `instruments()` returns them all: the share
    left out is of distinct equities (Antigravity, baseline finding 2)."""
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[2]))
    later = [
        parse_equity(Asset.model_validate({**alpaca_bars.ASSET, "symbol": symbol}), 10**9)
        for symbol in ("AAPL", "QQQ")
    ]
    ParquetDataCatalog(str(root)).write_instruments(later)
    assert len(ParquetDataCatalog(str(root)).instruments()) == 4
    document = json.loads(build(root, tmp_path / "baselines").read_text())
    assert document["source"]["equities"] == 2
    assert document["excluded_share"] == pytest.approx(0.5)
    with pytest.raises(history_load.HistoryStopped, match="1 of 2"):
        build(root, tmp_path / "again", max_excluded_share=0.4)


def _route(tmp_path, security_answers=False, **usage):
    """Run the task's script with its process boundaries replaced. The keychain stand-in refuses, or
    with `security_answers` prints a placeholder that is no key, so a load reaches the `uv` stand-in."""
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    security = tmp_path / "security"
    security.write_text(
        "#!/bin/sh\necho placeholder\n"
        if security_answers
        else "#!/bin/sh\necho forbidden-credential-access >&2\nexit 99\n"
    )
    security.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    timeout.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BASELINE_SENT"\n')
    uv.chmod(0o700)
    script = (
        task["run"]
        .replace("/usr/bin/security", str(security))
        .replace("/usr/bin/timeout", str(timeout))
    )
    env = {
        "PATH": f"{tmp_path}:{os.defpath}",
        "BASELINE_SENT": str(tmp_path / "sent"),
        "usage_account": "tests",
        **usage,
    }
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "usage",
    [
        {"usage_sessions_file": "/calendar.txt", "usage_symbols": "AAPL"},
        {"usage_max_excluded_share": "0.3", "usage_symbols": "AAPL"},
    ],
    ids=["sessions-file", "excluded-share"],
)
def test_load_mode_refuses_a_baseline_flag_before_any_credential(tmp_path, usage):
    """A baseline flag in load mode is refused by name. `--feed boats` was one until history_load took
    the boats feed itself (Antigravity, baseline finding 3: it would have loaded sip into a boats
    root)."""
    completed = _route(tmp_path, **usage)
    assert completed.returncode == 3
    assert "baseline" in completed.stderr
    assert "credential" not in completed.stderr
    assert not (tmp_path / "sent").exists()


@pytest.mark.parametrize(
    "usage",
    [
        {"usage_symbols": "AAPL"},
        {"usage_start": "2026-11-10T04:00:00-05:00"},
        {"usage_bars": "1-DAY"},
    ],
    ids=["symbols", "start", "bars"],
)
def test_baseline_mode_refuses_a_load_flag(tmp_path, usage):
    completed = _route(
        tmp_path,
        usage_mode="baseline",
        usage_session=str(SESSION),
        usage_sessions_file="/calendar.txt",
        **usage,
    )
    assert completed.returncode == 3
    assert "load" in completed.stderr
    assert not (tmp_path / "sent").exists()


def test_baseline_mode_without_a_sessions_file_leaves_it_to_the_parser(tmp_path):
    """No `--sessions-file ""`, which would read the working directory as the calendar."""
    completed = _route(tmp_path, usage_mode="baseline", usage_session=str(SESSION))
    assert completed.returncode == 0, completed.stderr
    args = (tmp_path / "sent").read_text().splitlines()
    assert "--sessions-file" not in args
    assert "" not in args


def test_batched_reads_give_the_same_file_as_one_symbol_at_a_time(
    tmp_path, alpaca_bars, monkeypatch
):
    root = market(tmp_path, alpaca_bars, missing=("QQQ", DAYS[2]))
    together = json.loads(build(root, tmp_path / "together").read_text())
    monkeypatch.setattr(writer(), "QUERY_SYMBOLS", 1)
    alone = json.loads(build(root, tmp_path / "alone").read_text())
    for document in (together, alone):
        del document["source"]["written_at"]
    assert together == alone
    assert set(together["symbols"]) == {"AAPL"}


@pytest.mark.parametrize(
    ("data", "root"),
    [("quotes", "catalog/boats-quotes"), ("trades", "catalog/boats"), ("bars", "catalog/boats")],
)
def test_load_mode_sends_boats_to_the_loader_in_a_root_of_its_own(tmp_path, data, root):
    """history_load takes `--feed boats` for bars, trades and quotes; the task passes it on, with the
    boats root the module defaults to, and holds the module's symbol bound before any pair is read."""
    completed = _route(
        tmp_path,
        usage_feed="boats",
        usage_data=data,
        usage_symbols="AAPL,MSFT",
        usage_start="2026-11-12T20:00:00-05:00",
        usage_end="2026-11-13T04:00:00-05:00",
        security_answers=True,
    )
    assert completed.returncode == 0, completed.stderr
    args = (tmp_path / "sent").read_text().splitlines()
    assert args[args.index("--feed") + 1] == "boats"
    assert args[args.index("--catalog") + 1] == root
    assert args[args.index("--data") + 1] == data


@pytest.mark.parametrize(
    "usage",
    [
        {"usage_symbols_file": "/symbols.txt"},
        {"usage_symbols": ",".join(f"S{i}" for i in range(9))},
    ],
    ids=["symbols-file", "nine-symbols"],
)
@pytest.mark.parametrize("data", ["quotes", "trades", "bars"])
def test_load_mode_holds_the_boats_bound_before_any_credential(tmp_path, usage, data):
    completed = _route(tmp_path, usage_feed="boats", usage_data=data, **usage)
    assert completed.returncode == 3
    assert "boats loads take" in completed.stderr, completed.stderr
    assert "baseline" not in completed.stderr and "credential" not in completed.stderr
    assert not (tmp_path / "sent").exists()
