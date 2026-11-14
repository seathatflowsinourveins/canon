"""Blind tests for `scripts/exit-monitor-notify` at `d75db684`, where the script does not exist.

The unit is the carrier for the exit monitor's alert outbox. Its frozen brief is
`lead-archive/2026-11-12/specs/handoff/L2-exit-monitor-carrier.md`, properties N1-N6, and the line it
carries is `exit_monitor._notice`'s (`src/canon_trading/exit_monitor.py:808-819`), the watchdog's shape
(`home/.claude/canon/lanes-watch.sh`). N5 (the `paper:exit-monitor` mise task) is out of this file's
scope: it changes `mise.toml`, not the carrier. N1, N2, N3, N4 and N6 are each a case or more below.
Every case is expected RED at this base: the script is absent, so `subprocess.run` raises
`FileNotFoundError` before any property can hold.

Call shape this file fixes, because a blind file must fix one: `scripts/exit-monitor-notify <A|B>` is one
carrier pass for that account, with `CLAUDE_CONFIG_DIR` replacing `~/.claude`, as
`python -m canon_trading.exit_monitor <A|B>` is one monitor pass (`exit_monitor.py:1341-1349`). The brief
names the seams `CANON_EXITMON_OSASCRIPT` and `CANON_EXITMON_NO_NOTIFY`; it names no seam that shortens
the 30 s notification bound, so the hang case waits for the real bound, and `REPORT.md` says so.

Each case writes a stand-in for `/usr/bin/osascript` into `tmp_path`, which records its argv and its
environment and can fail or hang. `CLAUDE_CONFIG_DIR` and `HOME` both point under `tmp_path`, so a carrier
that ignored the first still cannot write the real `~/.claude` paper state. No case reads a credential,
makes a request, or writes the catalog or the ledger; the invented `ALPACA_*` values exist only to prove
they do not reach the stand-in.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from canon_trading import exit_monitor

ROOT = Path(__file__).resolve().parents[1]
"""This checkout: the carrier is a sibling of the other `scripts/` entries."""
CARRIER = ROOT / "scripts" / "exit-monitor-notify"
"""The unit's one public seam. Absent at `d75db684`, so every case is red."""
TITLE = "Canon exit monitor"
"""N1's notification title."""
ACCOUNT = "A"
"""One account label, never a number."""


class StandIn:
    """A stand-in for osascript: records its argv and environment, then exits or hangs on request."""

    def __init__(self, tmp_path: Path) -> None:
        self.log = tmp_path / "osa.log"
        self.environment = tmp_path / "osa-env.log"
        self.exit_code = tmp_path / "osa-rc"
        self.hang_flag = tmp_path / "osa-hang"
        self.path = tmp_path / "osa"
        self.exit_code.write_text("0")
        log = shlex.quote(str(self.log))
        environment = shlex.quote(str(self.environment))
        hang = shlex.quote(str(self.hang_flag))
        exit_code = shlex.quote(str(self.exit_code))
        self.path.write_text(
            "#!/bin/bash\n"
            f"printf 'INVOCATION\\n' >> {log}\n"
            f'for argument in "$@"; do printf \'%s\\n\' "$argument" >> {log}; done\n'
            f"/usr/bin/env > {environment}\n"
            f"if [ -f {hang} ]; then exec /bin/sleep 60; fi\n"
            f'exit "$(cat {exit_code})"\n'
        )
        self.path.chmod(0o755)

    def fail(self) -> None:
        self.exit_code.write_text("1")

    def succeed(self) -> None:
        self.exit_code.write_text("0")

    def hang(self) -> None:
        self.hang_flag.write_text("")

    def release(self) -> None:
        self.hang_flag.unlink(missing_ok=True)

    def invocations(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls: list[list[str]] = []
        current: list[str] | None = None
        for row in self.log.read_text().splitlines():
            if row == "INVOCATION":
                current = []
                calls.append(current)
            elif current is not None:
                current.append(row)
        return calls

    def environment_text(self) -> str:
        return self.environment.read_text() if self.environment.exists() else ""


def alert(
    key: str,
    flag: str,
    state: str = "uncovered",
    *,
    account: str = ACCOUNT,
    lane: str | None = None,
) -> dict[str, Any]:
    """One outbox line in `_notice`'s shape, with a caller-chosen key fragment and flag."""
    return {
        "at": "2026-11-13T18:00:00+00:00",
        "key": f"exit-monitor:{account}:{key}",
        "sid": f"exit-monitor:{account}",
        "lane": lane or f"exit monitor {account}",
        "flag": flag,
        "state": state,
        "quiet_min": 0,
        "fail": 0,
    }


def folder(config: Path, account: str = ACCOUNT) -> Path:
    """`<CLAUDE_CONFIG_DIR>/canon/exit-monitor/<A|B>`: the monitor's own folder (`exit_monitor.py:470`)."""
    return config / "canon" / "exit-monitor" / account


def write_outbox(config: Path, lines: list[dict[str, Any]], account: str = ACCOUNT) -> Path:
    path = folder(config, account) / "alerts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def append_outbox(config: Path, line: dict[str, Any], account: str = ACCOUNT) -> None:
    path = folder(config, account) / "alerts.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps(line) + "\n")


def run_carrier(
    config: Path,
    standin: StandIn | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one carrier pass for account A with every inherited `ALPACA_*` name removed."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("ALPACA_")}
    env["CLAUDE_CONFIG_DIR"] = str(config)
    env["HOME"] = str(config / "home")
    if standin is not None:
        env["CANON_EXITMON_OSASCRIPT"] = str(standin.path)
    env.update(environment or {})
    return subprocess.run(
        [str(CARRIER), ACCOUNT],
        capture_output=True,
        text=True,
        cwd=str(config),
        env=env,
        timeout=120,
        check=False,
    )


def only_invocation(standin: StandIn) -> list[str]:
    calls = standin.invocations()
    assert len(calls) == 1, calls
    return calls[0]


def script_arguments(argv: list[str]) -> list[str]:
    """The AppleScript source this run handed osascript, one entry per `-e`."""
    return [
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "-e" and index + 1 < len(argv)
    ]


def payload(argv: list[str]) -> list[str]:
    """The arguments that are not the AppleScript source: the run's own text travels here."""
    source = script_arguments(argv)
    return [
        argument
        for argument in argv
        if argument not in source and "display notification" not in argument
    ]


def spoken_text(argv: list[str]) -> str:
    return "\n".join(payload(argv))


def read_status(config: Path, account: str = ACCOUNT) -> dict[str, Any]:
    value = json.loads((folder(config, account) / "notify-status.json").read_text())
    assert isinstance(value, dict)
    return value


def recorded(path: Path) -> set[str]:
    """Every string a delivered record carries, whether a key, a list entry or an object key."""
    if not path.exists():
        return set()
    return strings(json.loads(path.read_text()))


def strings(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found.add(str(key))
            found |= strings(item)
    elif isinstance(value, list):
        for item in value:
            found |= strings(item)
    return found


def delivered(config: Path, account: str = ACCOUNT) -> set[str]:
    return recorded(folder(config, account) / "delivered.json")


def test_one_notification_carries_the_title_the_lane_and_the_flag(tmp_path: Path) -> None:
    """N1: `display notification` with title "Canon exit monitor", subtitle the lane, text the flag."""
    standin = StandIn(tmp_path)
    flag = "SYN quantity 100 uncovered; frozen by the protector; next act no armed exit"
    write_outbox(tmp_path, [alert("one", flag)])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    argv = only_invocation(standin)
    assert any(TITLE in argument for argument in argv)
    spoken = spoken_text(argv)
    assert "exit monitor A" in spoken
    assert flag in spoken
    assert all(flag not in argument for argument in script_arguments(argv))


def test_one_run_names_four_flags_with_the_count_and_uncovered_first(tmp_path: Path) -> None:
    """N1: one notification for the run, up to 4 flags, the count, `uncovered` first, all keys sent."""
    standin = StandIn(tmp_path)
    flags = [
        ("b1", "BOUNDARY-ONE next act no armed exit", "boundary"),
        ("u1", "UNCOVERED-ONE frozen by the protector", "uncovered"),
        ("b2", "BOUNDARY-TWO next act no armed exit", "boundary"),
        ("u2", "UNCOVERED-TWO frozen by the protector", "uncovered"),
        ("b3", "BOUNDARY-THREE next act no armed exit", "boundary"),
    ]
    lines = [alert(key, flag, state) for key, flag, state in flags]
    write_outbox(tmp_path, lines)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    spoken = spoken_text(only_invocation(standin))
    named = [flag for _, flag, _ in flags if flag in spoken]
    assert len(named) == 4, spoken
    uncovered = [flags[1][1], flags[3][1]]
    boundary = [flag for flag in named if flag.startswith("BOUNDARY")]
    assert all(flag in named for flag in uncovered)
    assert max(spoken.index(flag) for flag in uncovered) < min(
        spoken.index(flag) for flag in boundary
    )
    assert re.search(r"(?<!\d)5(?!\d)|\bfive\b", spoken, re.IGNORECASE), spoken
    assert {line["key"] for line in lines} <= delivered(tmp_path)


def test_a_later_run_names_only_the_new_key(tmp_path: Path) -> None:
    """N1: each run notifies the keys that are still undelivered, not the ones already sent."""
    standin = StandIn(tmp_path)
    first = alert("one", "FIRST-FLAG frozen by the protector")
    second = alert("two", "SECOND-FLAG frozen by the protector")
    write_outbox(tmp_path, [first])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    append_outbox(tmp_path, second)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    calls = standin.invocations()
    assert len(calls) == 2, calls
    spoken = spoken_text(calls[1])
    assert "SECOND-FLAG frozen by the protector" in spoken
    assert "FIRST-FLAG frozen by the protector" not in spoken
    assert {first["key"], second["key"]} <= delivered(tmp_path)


def test_a_sent_key_is_never_sent_again(tmp_path: Path) -> None:
    """N2: a key whose notification succeeded is recorded in delivered.json and never sent again."""
    standin = StandIn(tmp_path)
    line = alert("one", "SYN quantity 100 uncovered; frozen by the protector")
    write_outbox(tmp_path, [line])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert line["key"] in delivered(tmp_path)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 1
    status = read_status(tmp_path)
    assert status["sent_now"] == 0
    assert status["pending"] == 0


def test_a_failed_notification_keeps_the_key_pending_and_the_next_run_sends_it(
    tmp_path: Path,
) -> None:
    """N2: a non-zero osascript exit sends nothing, keeps the key, and the next run sends it."""
    standin = StandIn(tmp_path)
    line = alert("one", "SYN quantity 100 uncovered; frozen by the protector")
    write_outbox(tmp_path, [line])
    standin.fail()
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 1
    assert line["key"] not in delivered(tmp_path)
    status = read_status(tmp_path)
    assert status["notified"] is False
    assert status["pending"] == 1
    assert status["sent_now"] == 0
    standin.succeed()
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 2
    assert line["key"] in delivered(tmp_path)
    status = read_status(tmp_path)
    assert status["notified"] is True
    assert status["pending"] == 0
    assert status["sent_now"] == 1


def test_a_hang_at_the_thirty_second_bound_keeps_the_key_pending_and_the_next_run_sends_it(
    tmp_path: Path,
) -> None:
    """N2: a notification past 30 s counts as not sent; the key is kept for the next run."""
    standin = StandIn(tmp_path)
    line = alert("one", "SYN quantity 100 uncovered; frozen by the protector")
    write_outbox(tmp_path, [line])
    standin.hang()
    started = time.monotonic()
    completed = run_carrier(tmp_path, standin)
    elapsed = time.monotonic() - started
    assert completed.returncode == 0, completed.stderr
    assert 20 <= elapsed <= 55, elapsed
    assert len(standin.invocations()) == 1
    assert line["key"] not in delivered(tmp_path)
    status = read_status(tmp_path)
    assert status["notified"] is False
    assert status["pending"] == 1
    standin.release()
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 2
    assert line["key"] in delivered(tmp_path)


def test_the_outbox_is_never_written(tmp_path: Path) -> None:
    """N3: the carrier reads alerts.jsonl and leaves it untouched, bytes and inode alike."""
    standin = StandIn(tmp_path)
    path = write_outbox(tmp_path, [alert("one", "SYN quantity 100 uncovered")])
    before = path.read_bytes()
    inode = path.stat().st_ino
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 1
    assert path.read_bytes() == before
    assert path.stat().st_ino == inode


def test_a_bad_line_is_skipped_and_a_good_line_is_sent(tmp_path: Path) -> None:
    """N3: a line that does not parse, or has no string key, is skipped; the rest is sent."""
    standin = StandIn(tmp_path)
    good = alert("good", "GOOD-FLAG frozen by the protector")
    path = write_outbox(tmp_path, [])
    path.write_text(
        "this is not json\n"
        + json.dumps({"at": "2026-11-13T18:00:00+00:00", "flag": "NO-KEY-FLAG"})
        + "\n"
        + json.dumps(good)
        + "\n"
    )
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    spoken = spoken_text(only_invocation(standin))
    assert "GOOD-FLAG frozen by the protector" in spoken
    assert "NO-KEY-FLAG" not in spoken
    assert good["key"] in delivered(tmp_path)
    assert read_status(tmp_path)["sent_now"] == 1


def test_only_the_last_two_thousand_lines_are_read(tmp_path: Path) -> None:
    """N3: the outbox read is bounded to its last 2000 lines; older keys are not delivered."""
    standin = StandIn(tmp_path)
    old = [alert(f"old-{number:03d}", f"OLD-FLAG-{number:03d}") for number in range(100)]
    new = [alert(f"new-{number:04d}", f"NEW-FLAG-{number:04d}") for number in range(2000)]
    write_outbox(tmp_path, old + new)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    spoken = spoken_text(only_invocation(standin))
    assert "OLD-FLAG-099" not in spoken
    keys = delivered(tmp_path)
    assert new[0]["key"] in keys
    assert old[-1]["key"] not in keys


def test_no_alpaca_variable_reaches_the_standin(tmp_path: Path) -> None:
    """N3: even when the caller sets `ALPACA_*`, the notifier inherits none of them."""
    standin = StandIn(tmp_path)
    write_outbox(tmp_path, [alert("one", "SYN quantity 100 uncovered")])
    completed = run_carrier(
        tmp_path,
        standin,
        environment={
            "ALPACA_API_KEY": "invented-key-value",
            "ALPACA_SECRET_KEY": "invented-secret-value",
            "ALPACA_BASE_URL": "https://example.invalid",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 1
    environment = standin.environment_text()
    assert "ALPACA_" not in environment
    assert "invented-key-value" not in environment
    assert "invented-secret-value" not in environment


def test_status_is_written_when_nothing_is_due(tmp_path: Path) -> None:
    """N4: every run rewrites notify-status.json; `notified` is null when nothing was due."""
    standin = StandIn(tmp_path)
    write_outbox(tmp_path, [])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert standin.invocations() == []
    status = read_status(tmp_path)
    assert {"checked_at", "pending", "sent_now", "notified"} <= set(status)
    assert status["pending"] == 0
    assert status["sent_now"] == 0
    assert status["notified"] is None
    datetime.fromisoformat(status["checked_at"])


def test_status_says_one_was_sent(tmp_path: Path) -> None:
    """N4: `notified` true and `sent_now` one when a run sends one key."""
    standin = StandIn(tmp_path)
    write_outbox(tmp_path, [alert("one", "SYN quantity 100 uncovered")])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    status = read_status(tmp_path)
    assert status["notified"] is True
    assert status["sent_now"] == 1
    assert status["pending"] == 0
    datetime.fromisoformat(status["checked_at"])


def test_status_says_a_failure_keeps_one_pending(tmp_path: Path) -> None:
    """N4: `notified` false, `sent_now` zero and `pending` one after a failed notification."""
    standin = StandIn(tmp_path)
    write_outbox(tmp_path, [alert("one", "SYN quantity 100 uncovered")])
    standin.fail()
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    status = read_status(tmp_path)
    assert status["notified"] is False
    assert status["sent_now"] == 0
    assert status["pending"] == 1


def test_no_notify_keeps_the_status_and_sends_nothing(tmp_path: Path) -> None:
    """N4: `CANON_EXITMON_NO_NOTIFY=1` sends nothing, keeps the key and still writes its status."""
    standin = StandIn(tmp_path)
    line = alert("one", "SYN quantity 100 uncovered")
    write_outbox(tmp_path, [line])
    completed = run_carrier(tmp_path, standin, environment={"CANON_EXITMON_NO_NOTIFY": "1"})
    assert completed.returncode == 0, completed.stderr
    assert standin.invocations() == []
    status = read_status(tmp_path)
    assert "notified" in status
    assert status["sent_now"] == 0
    assert status["pending"] == 1
    assert line["key"] not in delivered(tmp_path)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert len(standin.invocations()) == 1
    assert line["key"] in delivered(tmp_path)


def test_a_status_file_that_cannot_be_written_exits_non_zero(tmp_path: Path) -> None:
    """N4: a run that cannot write its own status file exits non-zero."""
    standin = StandIn(tmp_path)
    write_outbox(tmp_path, [alert("one", "SYN quantity 100 uncovered")])
    (folder(tmp_path) / "notify-status.json").mkdir()
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode != 0


def test_a_frozen_notice_from_the_base_reaches_the_standin_and_repeats_once(
    tmp_path: Path,
) -> None:
    """N6: a frozen line written by the base `_notice` is sent, and its repeat alert_count once more."""
    standin = StandIn(tmp_path)
    first = exit_monitor._notice(
        ACCOUNT,
        datetime(2026, 11, 13, 18, 0, tzinfo=UTC),
        "SYN:2026-11-13T12:00:00+00:00:uncovered:0",
        "SYN quantity 100 uncovered, remainder 0, age 120s since first seen; "
        "frozen by the protector; next act no armed exit",
        "uncovered",
    )
    assert "frozen by the protector" in first["flag"]
    write_outbox(tmp_path, [first])
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    assert first["flag"] in spoken_text(only_invocation(standin))
    assert first["key"] in delivered(tmp_path)
    repeat = exit_monitor._notice(
        ACCOUNT,
        datetime(2026, 11, 13, 18, 15, tzinfo=UTC),
        "SYN:2026-11-13T12:00:00+00:00:uncovered:1",
        "SYN quantity 100 uncovered, remainder 0, age 1020s since first seen; "
        "frozen by the protector; next act no armed exit",
        "uncovered",
    )
    append_outbox(tmp_path, repeat)
    completed = run_carrier(tmp_path, standin)
    assert completed.returncode == 0, completed.stderr
    calls = standin.invocations()
    assert len(calls) == 2, calls
    spoken = spoken_text(calls[1])
    assert repeat["flag"] in spoken
    assert first["flag"] not in spoken
    assert {first["key"], repeat["key"]} <= delivered(tmp_path)
