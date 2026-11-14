"""Blind reproductions of #338 C3-C5 and dsh F-1 at 7715cb6f, without fixes.

Contracts: ADR 0106's default-arm window records and MoverPart.bridge_reason. Offline
fixtures reuse tests.test_session_window's account stand-ins. C1, C2 and C6 are Mover's
and are not here. A run that never became a window (a parser refusal, a gap skip, nothing
to run or a startup failure) preserves the previous window record, by the lead's ruling. A
signal before the window writes a failed record naming the arm instead (main's contract, which
the lead kept): that record is itself unsettled, so the next start must still read flat.
No account client, credential, paper task or network is used.
"""

from __future__ import annotations

import json
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from canon_trading import session_window as window
from tests.test_session_window import Account
from tests.test_session_window import Evening


@pytest.fixture
def offline_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "EXTENDED_READY", True)
    monkeypatch.setattr(window, "_ABANDONED", [])
    monkeypatch.setattr(window, "paper_trading", lambda: Account())

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("offline test unexpectedly reached a client or node builder")

    for name in ("window_data", "window_actions", "window_screener", "window_node"):
        monkeypatch.setattr(window, name, forbidden)


def cli(tmp_path: Path, *flags: str, session: str = "extended") -> list[str]:
    return [
        "--session",
        session,
        "--mover",
        "--strategy",
        "Mover",
        "--log-dir",
        str(tmp_path),
        *flags,
    ]


def latest(tmp_path: Path, result: str, arm: str | None, default: bool) -> dict[str, Any]:
    path = tmp_path / "latest.json"
    assert path.is_file(), f"{result} window left no latest.json naming arm {arm}"
    record = json.loads(path.read_text())
    assert record["result"].startswith(result), record
    assert (record["arm"], record["arm_default"]) == (arm, default), record
    return record


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize(
    ("flags", "named", "arm"),
    [
        (
            ["--arm", "F2", "--profile-k", "3", "--trail-profile-path", "/profiles/today.json"],
            "profile",
            "F2",
        ),
        (
            ["--arm", "OWN1", "--trail-mode", "atr", "--trail-cap", "own", "--profile-k", "3"],
            "profile",
            "OWN1",
        ),
        (["--trail-profile-path", "/missing.json"], "trail_profile_path", None),
        (["--profile-k", "3"], "profile_k", None),
        (["--atr-k", "3"], "atr_k", None),
    ],
    ids=["f2-profile", "own1-profile", "profile-path-only", "profile-k-only", "atr-k-fixed"],
)
def test_c3_ineffective_flags_are_refused_by_name_and_recorded(
    monkeypatch, tmp_path, capsys, flags, named, arm
) -> None:
    ran: list[window.WindowConfig] = []

    def would_run(config, **_kwargs):
        ran.append(config)
        raise window.WindowSkipped("offline boundary: invalid flags reached run_window")

    monkeypatch.setattr(window, "run_window", would_run)
    code = window.main(cli(tmp_path, *flags))
    captured = capsys.readouterr()
    assert not ran, f"ineffective flags {flags} reached run_window with {ran[0]}"
    assert code != 0
    assert named in (captured.out + captured.err).replace("-", "_")
    latest(tmp_path, "refused", arm, False)


@pytest.mark.usefixtures("offline_window")
def test_dsh_f1_contradictory_own1_rows_write_a_refused_arm_record(tmp_path, capsys) -> None:
    assert window.main(cli(tmp_path, "--arm", "OWN1")) == 1
    assert "arm OWN1" in capsys.readouterr().err
    latest(tmp_path, "refused", "OWN1", False)


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize(
    ("flags", "session", "named", "arm", "default"),
    [
        (["--arm", "F2", "--trail-mode", "atr"], "extended", "F2", "F2", False),
        (["--reentry-arm", "vwap"], "extended", "reentry_arm", "OWN1", True),
        (["--opening-arm", "on"], "regular", "opening_arm", "OWN1", True),
        # 2 minutes, not 5: a window is refused at or under its 120 s wind-down, and 5 minutes is valid
        (["--minutes", "2"], "extended", "wind-down", "OWN1", True),
    ],
    ids=["f2-conflict", "vwap", "opening", "short-window"],
)
def test_c4_config_refusals_write_an_arm_record(
    tmp_path, capsys, flags, session, named, arm, default
) -> None:
    assert window.main(cli(tmp_path, *flags, session=session)) == 1
    assert named in capsys.readouterr().err
    latest(tmp_path, "refused", arm, default)


def previous_window(tmp_path: Path, result: str | None) -> bytes | None:
    if result is None:
        return None
    body = json.dumps(
        {
            "result": result,
            "started_epoch": 1,
            "stale_after": 1,
            "arm": "F2",
            "arm_default": False,
            "mover": {"run": {"entries": [{"instrument_id": "OLD.ALPACA"}]}},
        },
        indent=2,
    ).encode()
    (tmp_path / "latest.json").write_bytes(body)
    return body


def unchanged_window(tmp_path: Path, previous: bytes | None) -> None:
    path = tmp_path / "latest.json"
    if previous is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == previous


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize("previous_result", [None, "running", "stopping: SIGTERM received"])
@pytest.mark.parametrize("where", ["auto-clock", "config", "window-clock"])
def test_c4_failure_before_report_preserves_the_previous_window(
    monkeypatch, tmp_path, capsys, previous_result, where
) -> None:
    previous = previous_window(tmp_path, previous_result)

    def clock_failed(*_args):
        raise RuntimeError("offline clock unavailable")

    monkeypatch.setattr(
        window, "window_config" if where == "config" else "session_now", clock_failed
    )
    session = "auto" if where == "auto-clock" else "extended"
    assert window.main(cli(tmp_path, session=session)) == 1
    assert "offline clock unavailable" in capsys.readouterr().err
    heartbeat = json.loads((tmp_path / "last-firing.json").read_text())["outcome"]
    assert heartbeat.startswith("failed: RuntimeError: offline clock unavailable")
    if where != "auto-clock":
        assert "arm OWN1 (the default)" in heartbeat
    unchanged_window(tmp_path, previous)


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize("previous_result", [None, "ok", "running", "stopping: SIGTERM received"])
@pytest.mark.parametrize("where", ["config", "run_window"])
def test_c4_startup_signal_writes_a_failed_record_naming_the_arm(
    monkeypatch, tmp_path, previous_result, where
) -> None:
    previous_window(tmp_path, previous_result)

    def signalled(*_args, **_kwargs):
        window._SIGNALS.handle(signal.SIGTERM, None)

    monkeypatch.setattr(window, "window_config" if where == "config" else "run_window", signalled)
    assert window.main(cli(tmp_path)) == 128 + signal.SIGTERM
    heartbeat = json.loads((tmp_path / "last-firing.json").read_text())["outcome"]
    assert heartbeat.startswith("failed: WindowSignalled: stopped by SIGTERM"), heartbeat
    assert "arm OWN1 (the default)" in heartbeat
    record = json.loads((window.records_dir(tmp_path) / "latest.json").read_text())
    assert record["result"] == "failed: WindowSignalled: stopped by SIGTERM"
    assert (record["arm"], record["arm_default"]) == ("OWN1", True)


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize("flags, arm", [((), "OWN1 (the default)"), (("--arm", "F2"), "F2")])
def test_c4_parser_refusal_preserves_the_previous_window(tmp_path, flags, arm) -> None:
    """A refusal by argparse stays as it is (exit 2): it writes neither the heartbeat nor a record."""
    previous = previous_window(tmp_path, "running")
    heartbeat = tmp_path / "last-firing.json"
    before = heartbeat.read_bytes() if heartbeat.exists() else None
    with pytest.raises(SystemExit) as error:
        window.main(cli(tmp_path, *flags, "--regular-strategy", "Mover"))
    assert error.value.code == 2
    assert (heartbeat.read_bytes() if heartbeat.exists() else None) == before
    unchanged_window(tmp_path, previous)


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize("flags, arm", [((), "OWN1 (the default)"), (("--arm", "F2"), "F2")])
def test_c4_gap_skip_preserves_the_previous_window(tmp_path, flags, arm) -> None:
    previous = previous_window(tmp_path, "stopping: SIGTERM received")
    (tmp_path / "last-window-end").write_text("recent offline window")
    assert window.main(cli(tmp_path, *flags)) == 0
    heartbeat = json.loads((tmp_path / "last-firing.json").read_text())["outcome"]
    assert heartbeat.startswith("skipped: the last window ended under")
    assert f"arm {arm}" in heartbeat
    unchanged_window(tmp_path, previous)


@pytest.mark.usefixtures("offline_window")
@pytest.mark.parametrize("flags, arm", [((), "OWN1 (the default)"), (("--arm", "F2"), "F2")])
def test_c4_auto_nothing_to_run_preserves_the_previous_window(
    monkeypatch, tmp_path, flags, arm
) -> None:
    previous = previous_window(tmp_path, "running")
    monkeypatch.setattr(window, "resolve_session", lambda *_args, **_kw: (None, "offline closed"))
    assert window.main(cli(tmp_path, *flags, session="auto")) == 0
    heartbeat = json.loads((tmp_path / "last-firing.json").read_text())["outcome"]
    assert heartbeat.startswith("nothing to run: offline closed")
    assert f"arm {arm}" in heartbeat
    unchanged_window(tmp_path, previous)


@pytest.mark.usefixtures("offline_window")
def test_c5_default_arm_can_reach_the_documented_bridge(monkeypatch, tmp_path) -> None:
    config = window.window_config(window._args(cli(tmp_path)), "extended")
    monkeypatch.setitem(
        window.MOVER_PARTS,
        "after-hours",
        replace(window.MOVER_PARTS["after-hours"], bridge_reason="offline bridge requested"),
    )
    monkeypatch.setattr(window, "paper_trading", lambda: Evening(17))

    # a private exception: run_window turns a RuntimeError (WindowSkipped among them) raised by
    # warm_up_start into WindowRefused, so the boundary must not be one
    class AfterBridge(Exception):
        pass

    def after_bridge(*_args, **_kwargs):
        raise AfterBridge("offline boundary: bridge configuration succeeded")

    monkeypatch.setattr(window, "warm_up_start", after_bridge)
    with pytest.raises(AfterBridge, match="bridge configuration succeeded"):
        window.run_window(config)
