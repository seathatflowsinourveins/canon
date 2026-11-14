"""Feed-boundary scheduling, runner bounds and offline stream hand-over checks.

All record writes use tmp_path. No client is constructed, no stream or node is started,
and no test reads the real paper state, a credential, the catalog or the ledger.
The stream cases run stand-in processes behind the task's real stream lock. They
do not replace a paper boundary proof. The overlap branch is refused by name until
the stream-limit measurement settles it; its red case is kept outside this file.
"""

import json
import re
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock

from canon_trading import session_window as window

ROOT = Path(__file__).resolve().parents[1]


def market(at: str) -> tuple[Clock, list[Calendar]]:
    now = datetime.fromisoformat(at).replace(tzinfo=window.NEW_YORK)
    return (
        Clock(
            timestamp=now,
            is_open=False,
            next_open=datetime(2026, 11, 16, 9, 30, tzinfo=window.NEW_YORK),
            next_close=datetime(2026, 11, 16, 16, tzinfo=window.NEW_YORK),
        ),
        [
            Calendar(
                date=day,
                open="09:30",
                close="16:00",
                session_open="0400",
                session_close="2000",
            )
            for day in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-13", "2026-11-16")
        ],
    )


@pytest.mark.parametrize("at", ["2026-11-09T19:59:30", "2026-11-10T03:59:30"])
def test_auto_skips_preflight_when_the_feed_boundary_leaves_no_window(at: str) -> None:
    clock, calendar = market(at)
    selected, why = window.resolve_session(clock, calendar, mover=True)
    assert selected is None
    assert "left in the session" in why


@pytest.mark.parametrize("at", ["2026-11-09T19:59:30", "2026-11-10T03:59:30"])
def test_an_explicit_window_skips_before_account_or_ranking_reads(
    at: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, calendar = market(at)

    def unexpected_read(*args: object, **kwargs: object) -> None:
        pytest.fail("the closing feed entered preflight before rejecting its unusable window")

    monkeypatch.setattr(window, "leftovers", unexpected_read)
    monkeypatch.setattr(window, "choose_mover", unexpected_read)
    with pytest.raises(window.WindowSkipped, match="left in the session"):
        window.run_window(
            window.WindowConfig(session="extended", mover=True, log_dir=tmp_path),
            trading=cast(TradingClient, object()),
            market=(clock, calendar),
        )


@pytest.mark.parametrize("at", ["2026-11-09T19:59:30", "2026-11-10T03:59:30"])
def test_auto_cli_leaves_the_next_firing_free_at_the_boundary(
    at: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock, calendar = market(at)
    monkeypatch.setattr(window, "paper_trading", lambda: object())
    monkeypatch.setattr(window, "session_now", lambda _: (clock, calendar, 0.0))

    def unexpected_window(*args: object, **kwargs: object) -> None:
        pytest.fail("auto started a window whose preflight would run past the feed boundary")

    monkeypatch.setattr(window, "run_window", unexpected_window)
    assert window.main(["--session", "auto", "--mover", "--log-dir", str(tmp_path)]) == 0
    assert "nothing to run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("at", "feed"),
    [
        ("2026-11-09T20:00:00", "boats"),
        ("2026-11-09T20:01:10", "boats"),
        ("2026-11-10T04:00:00", "sip"),
        ("2026-11-10T04:01:10", "sip"),
        ("2026-11-13T20:00:00", None),
        ("2026-11-14T04:00:00", None),
        ("2026-11-14T20:00:00", None),
        ("2026-11-15T04:00:00", None),
        ("2026-11-15T19:59:30", None),
        ("2026-11-15T20:00:00", "boats"),
    ],
)
def test_the_next_firing_selects_the_boundary_feed_or_nothing(at: str, feed: str | None) -> None:
    clock, calendar = market(at)
    selected, _ = window.resolve_session(clock, calendar, mover=True)
    if feed is None:
        assert selected is None
    else:
        assert selected == "extended"
        _, part = window.session_part(selected, clock, calendar)
        assert window.MOVER_PARTS[part].feed == feed


@pytest.mark.parametrize(
    ("at", "boundary"),
    [
        ("2026-11-09T19:30:00", "2026-11-09T20:00:00"),
        ("2026-11-10T03:30:00", "2026-11-10T04:00:00"),
    ],
)
def test_a_bounded_window_ends_before_the_feed_boundary(at: str, boundary: str) -> None:
    clock, calendar = market(at)
    ends, _ = window.session_part("extended", clock, calendar)
    assert ends == datetime.fromisoformat(boundary).replace(tzinfo=window.NEW_YORK)
    seconds, _ = window.window_seconds(60, (ends - clock.timestamp).total_seconds())
    assert 0 < seconds <= (ends - clock.timestamp).total_seconds() - window.END_MARGIN_SECS


def test_sunday_overnight_belongs_to_mondays_trade_date() -> None:
    clock, calendar = market("2026-11-15T20:00:00")
    ends, part = window.session_part("extended", clock, calendar)
    assert part == "overnight"
    assert ends == datetime(2026, 11, 16, 4, tzinfo=window.NEW_YORK)


def test_no_overnight_window_before_a_calendar_holiday() -> None:
    clock, calendar = market("2026-11-09T20:00:00")
    calendar = [day for day in calendar if day.date.isoformat() != "2026-11-10"]
    assert window.resolve_session(clock, calendar, mover=True)[0] is None


@pytest.mark.parametrize("policy", ["overlap", "typo"])
def test_an_unimplemented_or_invalid_policy_refuses_before_client_work(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    monkeypatch.setenv("CANON_FEED_HANDOVER", policy)
    monkeypatch.setattr(window, "_main", lambda _: called.append("client work") or 0)
    assert window.main(["--log-dir", str(tmp_path)]) == 1
    assert called == []


def test_paper_plist_names_both_feed_boundaries_and_keeps_the_interval(tmp_path: Path) -> None:
    import plistlib

    source = (ROOT / "scripts/automation").read_text()
    function = re.search(r"^write_paper_plist\(\) \{\n.*?^\}", source, re.MULTILINE | re.DOTALL)
    assert function is not None
    plist = tmp_path / "paper.plist"
    # Extract only the writer. No install, launchctl, real HOME or paper task can run here.
    script = (
        "xml() { printf '%s' \"$1\"; }\n"
        f"CANON_HOME={shlex.quote(str(tmp_path))}\n"
        f"PAPER_PLIST={shlex.quote(str(plist))}\n"
        f"PAPER_CHECKOUT={shlex.quote(str(tmp_path / 'checkout'))}\n"
        "PAPER_LABEL=local.canon.paper-session\nPAPER_INTERVAL=70\nPAPER_MINUTES=25\n"
        "PAPER_REGULAR_STRATEGY=Mover\n"
        + function[0]
        + "\nwrite_paper_plist /unused/mise /unused/origin\n"
    )
    subprocess.run(["/bin/bash", "-c", script], env={}, check=True)
    written = plistlib.loads(plist.read_bytes())
    expected = [
        {"Weekday": day, "Hour": hour, "Minute": 0}
        for hour, days in ((4, range(1, 6)), (20, range(5)))
        for day in days
    ]
    assert written.get("StartCalendarInterval") == expected
    assert written["StartInterval"] == 70
    assert written["RunAtLoad"] is False
    assert written["ProgramArguments"][7:9] == ["--minutes", "25"]
    assert "--carry" not in written["ProgramArguments"]


_STREAM_WORKER = r"""
import logging
import sys
import time
from pathlib import Path
from canon_trading import session_window as window

root, feed = Path(sys.argv[1]), sys.argv[2]
logger = logging.getLogger("alpaca.data.live.websocket")
logger.setLevel(logging.INFO)

def offline_main(args):
    # The pinned stream logs this after authentication. No stream/client is built.
    version = "v2" if feed == "sip" else "v1beta1"
    logger.info("connected to wss://stream.data.alpaca.markets/%s/%s", version, feed)
    (root / ("opened-" + feed)).touch()
    if feed == "sip":
        end = time.monotonic() + 20
        while not (root / "release-sip").exists():
            if time.monotonic() >= end:
                return 1
            time.sleep(0.01)
    logger.info("data stream stopped")
    return 0

window._main = offline_main
raise SystemExit(window.main(["--log-dir", str(root)]))
"""


def stream_process(tmp_path: Path, feed: str, *, policy: str | None = None) -> subprocess.Popen:
    """The task's real stream-lock functions, with a completely offline Python body.

    Every file, including the descriptor lock and state, is inside tmp_path.
    The environment is built from scratch: no keychain, credential or Alpaca call.
    """
    if not Path("/usr/bin/lockf").is_file():
        pytest.skip("the paper task uses macOS /usr/bin/lockf")
    run = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:session"]["run"]
    functions = []
    for name in ("lock_wait", "stream"):
        function = re.search(rf"^{name}\(\) \{{\n.*?^\}}", run, re.MULTILINE | re.DOTALL)
        assert function is not None
        functions.append(function[0])
    script = (
        "\n".join(functions)
        + f"\ntmp={shlex.quote(str(tmp_path) + '/')}\nlockwait=15\nwaited=0\n"
        + 'stream\nexec "$@"\n'
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    if policy is not None:
        env["CANON_FEED_HANDOVER"] = policy
    return subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            script,
            "offline-stream",
            sys.executable,
            "-c",
            _STREAM_WORKER,
            str(tmp_path),
            feed,
        ],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def await_file(path: Path, process: subprocess.Popen, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


def test_serial_branch_waits_for_the_old_stream_and_logs_the_measured_gap(tmp_path: Path) -> None:
    old = stream_process(tmp_path, "sip")
    new = None
    try:
        assert await_file(tmp_path / "opened-sip", old), "the offline old stream did not start"
        new = stream_process(tmp_path, "boats")  # setting absent: branch (b)
        assert not await_file(tmp_path / "opened-boats", new, 0.25)
        (tmp_path / "release-sip").touch()
        old_output = old.communicate(timeout=15)[0]
        new_output = new.communicate(timeout=15)[0]
        assert old.returncode == 0, old_output
        assert new.returncode == 0, new_output
        record = tmp_path / "feed-stream-gap.json"
        assert record.exists(), "serial hand-over did not measure or log the gap"
        measured = json.loads(record.read_text())
        assert measured["policy"] == "serial"
        assert (measured["from_feed"], measured["to_feed"]) == ("sip", "boats")
        assert measured["metric"] == "stream_stopped_to_stream_authenticated"
        assert measured["gap_seconds"] >= 0
        assert "feed-stream-gap: sip -> boats policy=serial gap_seconds=" in new_output
    finally:
        (tmp_path / "release-sip").touch()
        for process in (old, new):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=15)


def _offline_stream(feed: str, *, stop: bool = True):
    """An offline `_main` that logs the pinned stream's lifecycle lines and returns 0."""
    import logging

    def offline_main(_args) -> int:
        logger = logging.getLogger("alpaca.data.live.websocket")
        version = "v2" if feed == "sip" else "v1beta1"
        logger.info("connected to wss://stream.data.alpaca.markets/%s/%s", version, feed)
        if stop:
            logger.info("data stream stopped")
        return 0

    return offline_main


def test_a_measurement_failure_never_changes_the_windows_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream-gap record is observation only: a window that ended 0 still exits 0 when the
    record cannot be read or written, and the failure is said on stderr."""
    (tmp_path / "feed-stream-state.json").mkdir()  # unreadable as a file and unwritable over
    monkeypatch.delenv("CANON_FEED_HANDOVER", raising=False)
    monkeypatch.setattr(window, "_main", _offline_stream("boats"))
    assert window.main(["--log-dir", str(tmp_path)]) == 0


def test_a_bad_state_file_is_overwritten_and_does_not_fail_later_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "feed-stream-state.json"
    state.write_text("not json")
    monkeypatch.delenv("CANON_FEED_HANDOVER", raising=False)
    monkeypatch.setattr(window, "_main", _offline_stream("sip"))
    assert window.main(["--log-dir", str(tmp_path)]) == 0
    written = json.loads(state.read_text())
    assert (written["feed"], written["state"]) == ("sip", "closed")
    monkeypatch.setattr(window, "_main", _offline_stream("boats"))
    assert window.main(["--log-dir", str(tmp_path)]) == 0
    gap = json.loads((tmp_path / "feed-stream-gap.json").read_text())
    assert (gap["from_feed"], gap["to_feed"], gap["status"]) == ("sip", "boats", "measured")


def test_the_stream_gap_record_says_what_it_does_not_measure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It times one data stream's stop to the next one's authentication, which is not the seconds
    a holding is without an armed stop (the lead's property), and says so in the record and the log."""
    monkeypatch.delenv("CANON_FEED_HANDOVER", raising=False)
    for feed in ("sip", "boats"):
        monkeypatch.setattr(window, "_main", _offline_stream(feed))
        assert window.main(["--log-dir", str(tmp_path)]) == 0
    gap = json.loads((tmp_path / "feed-stream-gap.json").read_text())
    assert gap["metric"] == "stream_stopped_to_stream_authenticated"
    assert gap["not_measured"] == "seconds a holding is without an armed stop"
    assert not (tmp_path / "feed-handover.json").exists()
    out = capsys.readouterr().out
    assert "feed-stream-gap: sip -> boats policy=serial gap_seconds=" in out
    assert "not the seconds a holding is without an armed stop" in out
