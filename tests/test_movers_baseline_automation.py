"""Offline checks of ADR 0087's evening automation; every write is under tmp_path.

The shell harness sources only automation's definitions. Git, mise, checkout preparation,
descriptor locks and launchctl are stand-ins; no paper task or installer is executed.
"""

from __future__ import annotations

import json
import plistlib
import runpy
import shlex
import subprocess
import tomllib
from datetime import date
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / "scripts/automation"
INPUTS = ROOT / "scripts/movers-baseline-inputs.py"
COMMIT = "8bf8e947ea8309327c1abb110aa8aed70a8f63e3"


def shell(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    definitions = AUTOMATION.read_text().rsplit('\ncase "${1:-}" in', 1)[0]
    config = tmp_path / "config"
    (config / "canon/watch").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents").mkdir(exist_ok=True)
    program = tmp_path / "automation-fixture.sh"
    program.write_text(
        f"git() {{ echo {COMMIT}; }}\n" + definitions + f"\nROOT={shlex.quote(str(ROOT))}\n" + body
    )
    return subprocess.run(
        ["/bin/bash", str(program)],
        check=False,
        env={
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(config),
            "CANON_LAUNCHAGENTS_DIR": str(tmp_path / "agents"),
            "CANON_GETCONF": "/usr/bin/false",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_evening_plist_schedule_and_arguments(tmp_path):
    output = tmp_path / "evening.plist"
    result = shell(
        tmp_path,
        f'write_baseline_plist /fake/mise https://example.com/canon.git "{output}"\n',
    )
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(output.read_bytes())
    canon = tmp_path / "config/canon"
    assert plist["Label"] == "local.canon.movers-baseline"
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(canon / "movers-baseline.sh"),
        "build-movers-baseline",
        "/fake/mise",
        str(canon / "movers-baseline-checkout"),
        "https://example.com/canon.git",
    ]
    assert plist["StartInterval"] == 900
    assert plist["RunAtLoad"] is False
    assert plist["WorkingDirectory"] == str(canon)
    assert plist["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")


@pytest.mark.parametrize("outcome,label", [("ok", "ok"), ("failed", "FAILED"), (None, "UNKNOWN")])
def test_evening_status(tmp_path, outcome, label):
    watch = tmp_path / "config/canon/watch"
    watch.mkdir(parents=True)
    if outcome:
        (watch / "movers-baseline.json").write_text(
            json.dumps(
                {
                    "at_epoch": 1794619200,
                    "outcome": outcome,
                    "exit_code": 4 if outcome == "failed" else 0,
                    "note": "history_failed: exit 4" if outcome == "failed" else "sip 2026-11-16",
                    "commit": COMMIT,
                }
            )
        )
    result = shell(
        tmp_path,
        """
lc() { if [ "$1" = print ]; then printf '\tstate = waiting\n\tlast exit code = 0\n'; fi; }
cmp() { return 0; }
date() { echo 1794619200; }
bad=0
show_baseline_status
exit "$bad"
""",
    )
    assert "movers baseline    " in result.stdout
    assert label in result.stdout
    assert result.returncode == (0 if label == "ok" else 1), result.stderr
    if outcome == "failed":
        assert "history_failed" in result.stdout
    elif outcome is None:
        assert "no firing recorded" in result.stdout


@pytest.fixture
def inputs():
    assert INPUTS.is_file(), "the scheduled task needs a calendar and universe preparation helper"
    return runpy.run_path(str(INPUTS))


@pytest.fixture
def calendar():
    # Thanksgiving is absent; Friday has an early close. Dates, not missing bars, decide sessions.
    return [
        SimpleNamespace(date=date.fromisoformat(day))
        for day in (
            "2026-11-12",
            "2026-11-13",
            "2026-11-16",
            "2026-11-17",
            "2026-11-18",
            "2026-11-19",
            "2026-11-20",
            "2026-11-23",
            "2026-11-24",
            "2026-11-25",
            "2026-11-27",
            "2026-11-30",
        )
    ]


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-11-24T19:59:00-05:00", "2026-11-24"),
        ("2026-11-24T20:00:00-05:00", "2026-11-25"),
        ("2026-11-24T20:01:00-05:00", "2026-11-25"),
        ("2026-11-20T20:01:00-05:00", "2026-11-23"),
        ("2026-11-22T20:01:00-05:00", "2026-11-23"),
        ("2026-11-25T20:01:00-05:00", "2026-11-27"),
        ("2026-11-26T19:59:00-05:00", "2026-11-27"),
        ("2026-11-25T01:01:00+00:00", "2026-11-25"),
    ],
)
def test_trade_date_uses_new_york_rollover_and_calendar(inputs, calendar, stamp, expected):
    target, previous = inputs["select_sessions"](datetime.fromisoformat(stamp), calendar)
    assert target == date.fromisoformat(expected)
    assert len(previous) == 5
    assert previous == tuple(day.date for day in calendar if day.date < target)[-5:]


def test_calendar_cannot_infer_missing_dates(inputs):
    with pytest.raises(ValueError, match="calendar"):
        inputs["select_sessions"](datetime.fromisoformat("2026-11-13T20:01:00-05:00"), [])


def test_inputs_seed_catalog_and_write_the_complete_plan(inputs, calendar, tmp_path, monkeypatch):
    calls = []

    class Client:
        def get_calendar(self, request):
            calls.append("calendar")
            assert request.start < date(2026, 11, 12)
            assert request.end > date(2026, 11, 30)
            return calendar

        def get_all_assets(self, request):
            calls.append("assets")
            assert request.asset_class.value == "us_equity"
            return [SimpleNamespace(symbol="AAA")]

    class Catalog:
        def __init__(self, path):
            assert Path(path) == tmp_path / "catalog"

        def write_instruments(self, instruments):
            calls.append("instruments")
            assert instruments == ["equity"]

    prepare = inputs["prepare_inputs"]
    monkeypatch.setitem(prepare.__globals__, "ParquetDataCatalog", Catalog)
    monkeypatch.setitem(prepare.__globals__, "parse_equity", lambda asset, stamp: "equity")
    plan = prepare(
        tmp_path / "inputs",
        tmp_path / "catalog",
        tmp_path / "baselines",
        now=datetime.fromisoformat("2026-11-25T20:16:00-05:00"),
        client=Client(),
    )
    assert calls == ["calendar", "assets", "instruments"]
    assert plan["session"] == "2026-11-27"
    assert plan["start"] == "2026-11-19T04:01:00-05:00"
    assert plan["end"] == "2026-11-25T20:00:00-05:00"
    assert plan["deadline_epoch"] == int(
        datetime.fromisoformat("2026-11-27T04:00:00-05:00").timestamp()
    )
    assert (tmp_path / "inputs/symbols.txt").read_text() == "AAA\n"
    assert (tmp_path / "inputs/sessions.txt").read_text().splitlines() == [
        "2026-11-19",
        "2026-11-20",
        "2026-11-23",
        "2026-11-24",
        "2026-11-25",
        "2026-11-27",
    ]
    assert json.loads((tmp_path / "inputs/plan.json").read_text()) == plan


def test_inputs_refuse_unpublished_session_before_asset_read(inputs, calendar, tmp_path):
    client = SimpleNamespace(get_calendar=lambda request: calendar)
    with pytest.raises(ValueError, match="published"):
        inputs["prepare_inputs"](
            tmp_path / "inputs",
            tmp_path / "catalog",
            tmp_path / "baselines",
            now=datetime.fromisoformat("2026-11-25T20:01:00-05:00"),
            client=client,
        )
    assert not (tmp_path / "inputs/plan.json").exists()
    assert not (tmp_path / "catalog").exists()


def test_completed_date_needs_no_asset_read_or_history(inputs, calendar, tmp_path, monkeypatch):
    output = tmp_path / "baselines/2026-11-27/sip.json"
    output.parent.mkdir(parents=True)
    output.write_text("complete baseline stand-in")
    prepare = inputs["prepare_inputs"]
    monkeypatch.setattr(
        prepare.__globals__["movers_rank"],
        "read_baselines",
        lambda path, session: SimpleNamespace(problem=None),
    )
    plan = prepare(
        tmp_path / "inputs",
        tmp_path / "catalog",
        tmp_path / "baselines",
        now=datetime.fromisoformat("2026-11-25T20:16:00-05:00"),
        client=SimpleNamespace(get_calendar=lambda request: calendar),
    )
    assert plan["complete"] is True
    assert not (tmp_path / "catalog").exists()
    assert not (tmp_path / "inputs/symbols.txt").exists()
    assert output.read_text() == "complete baseline stand-in"


def test_inputs_refuse_client_without_data_only_before_any_pair_read(inputs, monkeypatch):
    factory = inputs["calendar_client"]
    monkeypatch.setitem(factory.__globals__, "data_only", lambda: False)

    def forbidden():
        pytest.fail("a pair was read outside data-only mode")

    monkeypatch.setitem(factory.__globals__, "credentials_from_env", forbidden)
    with pytest.raises(ValueError, match="DATA_ONLY"):
        factory()


def test_task_has_a_preparation_flag_without_changing_baseline_mode():
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    assert 'flag "--baseline-inputs <dir>"' in task["usage"]
    assert "scripts/movers-baseline-inputs.py" in task["run"]
    baseline = task["run"].split("  baseline)", 1)[1].split("    ;;", 1)[0]
    assert "canon_trading.movers_baseline" in baseline
    assert "security" not in baseline


def run_evening(
    tmp_path: Path,
    *,
    fail: str = "",
    clock: str = "2016",
    expired: bool = False,
    lock_held: bool = False,
    late: bool = False,
    before: str = "",
):
    canon = tmp_path / "config/canon"
    canon.mkdir(parents=True, exist_ok=True)
    helper = canon / "movers-baseline-checkout.sh"
    helper.write_text('#!/bin/bash\nmkdir -p "$3"\n')
    mise = tmp_path / "mise"
    mise.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/calls"\n'
        f'{{ true >&9; }} 2>/dev/null && echo fd9 >> "{tmp_path}/inherited"\n'
        'while [ "$1" != paper:history ]; do shift; done\nshift 2\n'
        'case "$1" in\n'
        f'--baseline-inputs) mkdir -p "$2"; printf "%s" \'{{"session":"2026-11-16",'
        '"start":"2026-11-09T04:01:00-05:00","end":"2026-11-13T20:00:00-05:00",'
        f'"deadline_epoch":{1794619199 if expired else 1794870000}}}'
        '\' > "$2/plan.json"; echo 2026-11-16 > "$2/sessions.txt"; echo AAA > "$2/symbols.txt" ;;\n'
        f'baseline) while [ "$1" != --baseline-dir ]; do shift; done; mkdir -p "$2"; '
        'printf "%s" \'{"session":"2026-11-16","feeds":["sip"]}\' > "$2/sip.json"; '
        + (f': > "{tmp_path}/late"; ' if late else "")
        + f"exit {6 if fail == 'baseline' else 0} ;;\n"
        f"*) exit {4 if fail == 'history' else 0} ;;\nesac\n"
    )
    mise.chmod(0o755)
    return shell(
        tmp_path,
        f"""
date() {{ case "${{!#}}" in +%H%M) echo {clock} ;;
  *) if [ -e "{tmp_path}/late" ]; then echo 1794870000; else echo 1794619200; fi ;; esac; }}
lock_stub() {{ [ "{int(lock_held)}" = 1 ] && [ "$3" = 9 ] && return 75; return 0; }}
LOCKF=lock_stub
{before}
do_baseline_movers "{mise}" "$BASELINE_CHECKOUT" https://example.com/canon.git
""",
    )


@pytest.mark.parametrize("failure", ["history", "baseline"])
def test_failed_evening_leaves_no_baseline(tmp_path, failure):
    result = run_evening(tmp_path, fail=failure)
    assert result.returncode != 0
    final = tmp_path / "config/canon/movers-baseline/2026-11-16/sip.json"
    assert not final.exists()
    status = json.loads((tmp_path / "config/canon/watch/movers-baseline.json").read_text())
    assert status["outcome"] == "failed"
    assert f"{failure}_failed" in status["note"]
    calls = (tmp_path / "calls").read_text()
    if failure == "history":
        assert " tests baseline " not in calls


def test_evening_runs_existing_tasks_in_its_own_clone(tmp_path):
    result = run_evening(tmp_path)
    assert result.returncode == 0, result.stderr
    canon = tmp_path / "config/canon"
    assert (canon / "movers-baseline/2026-11-16/sip.json").is_file()
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 3
    assert all("paper:history tests " in line for line in calls)
    assert "--bars 1-MINUTE" in calls[1]
    assert f"--catalog {canon}/movers-baseline-catalog/sip" in calls[1]
    assert f"--baseline-dir {canon}/movers-baseline/2026-11-16" in calls[2]
    assert "--session 2026-11-16" in calls[2]
    assert "--feed sip" in calls[2]
    assert json.loads((canon / "watch/movers-baseline.json").read_text())["outcome"] == "ok"


@pytest.mark.parametrize("clock", ["1959", "2001", "0400", "1200"])
def test_evening_waits_for_publication_and_stays_out_of_the_day(tmp_path, clock):
    result = run_evening(tmp_path, clock=clock)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()


def test_firing_past_the_sip_open_deadline_fails_without_history_or_output(tmp_path):
    result = run_evening(tmp_path, expired=True)
    assert result.returncode != 0
    canon = tmp_path / "config/canon"
    assert not (canon / "movers-baseline/2026-11-16/sip.json").exists()
    assert len((tmp_path / "calls").read_text().splitlines()) == 1
    assert (
        "deadline_failed" in json.loads((canon / "watch/movers-baseline.json").read_text())["note"]
    )


def test_automation_lifecycle_includes_baseline():
    text = AUTOMATION.read_text()
    assert "  install_baseline || ok=1" in text
    assert "  show_baseline_status" in text
    assert 'write_baseline_plist "$m" "$url" "$t/b"' in text
    assert '"$BASELINE_LABEL") fs=' in text


def test_tasks_do_not_inherit_the_firing_lock(tmp_path):
    result = run_evening(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "inherited").exists()


def test_a_held_firing_lock_leaves_the_last_status(tmp_path):
    status = tmp_path / "config/canon/watch/movers-baseline.json"
    status.parent.mkdir(parents=True)
    kept = '{"at_epoch":1794619000,"outcome":"ok","exit_code":0,"note":"sip 2026-11-16 complete"}'
    status.write_text(kept)
    result = run_evening(tmp_path, lock_held=True)
    assert result.returncode == 0, result.stderr
    assert status.read_text() == kept
    assert not (tmp_path / "calls").exists()


def test_a_date_completed_this_evening_is_not_prepared_again(tmp_path):
    canon = tmp_path / "config/canon"
    (canon / "watch").mkdir(parents=True)
    (canon / "movers-baseline/2026-11-16").mkdir(parents=True)
    (canon / "movers-baseline/2026-11-16/sip.json").write_text("{}")
    kept = '{"at_epoch":1794618000,"outcome":"ok","exit_code":0,"note":"sip 2026-11-16 complete"}'
    (canon / "watch/movers-baseline.json").write_text(kept)
    result = run_evening(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()
    assert (canon / "watch/movers-baseline.json").read_text() == kept


def test_a_completion_after_the_opening_keeps_the_written_file(tmp_path):
    result = run_evening(tmp_path, late=True)
    assert result.returncode != 0
    canon = tmp_path / "config/canon"
    assert (canon / "movers-baseline/2026-11-16/sip.json").is_file()
    note = json.loads((canon / "watch/movers-baseline.json").read_text())["note"]
    assert "deadline_failed" in note
    assert "kept" in note


def test_a_stopped_firing_stops_its_task(tmp_path):
    """The firing's subshell holds the traps (the function body is a subshell under the `&` fork)."""
    pidfile = tmp_path / "task.pid"
    slow = tmp_path / "slow-mise"
    slow.write_text(
        "#!/bin/bash\n"
        'while [ "$1" != paper:history ]; do shift; done\nshift 2\n'
        'case "$1" in\n'
        '--baseline-inputs) mkdir -p "$2"; printf "%s" \'{"session":"2026-11-16",'
        '"start":"2026-11-09T04:01:00-05:00","end":"2026-11-13T20:00:00-05:00",'
        '"deadline_epoch":1794870000}\' > "$2/plan.json"; echo AAA > "$2/symbols.txt" ;;\n'
        f'*) echo $$ > "{pidfile}"; sleep 30 ;;\nesac\n'
    )
    slow.chmod(0o755)
    canon = tmp_path / "config/canon"
    canon.mkdir(parents=True)
    (canon / "movers-baseline-checkout.sh").write_text('#!/bin/bash\nmkdir -p "$3"\n')
    result = shell(
        tmp_path,
        f"""
date() {{ case "${{!#}}" in +%H%M) echo 2016 ;; *) echo 1794619200 ;; esac; }}
lock_stub() {{ return 0; }}
LOCKF=lock_stub
do_baseline_movers "{slow}" "$BASELINE_CHECKOUT" https://example.com/canon.git &
firing=$!
for _ in $(seq 100); do [ -s "{pidfile}" ] && break; sleep 0.1; done
pkill -TERM -P "$firing"; wait "$firing"
sleep 0.5
if kill -0 "$(cat "{pidfile}")" 2>/dev/null; then echo task-alive; kill "$(cat "{pidfile}")"; fi
""",
    )
    assert pidfile.is_file(), result.stderr
    assert "task-alive" not in result.stdout
    status = json.loads((canon / "watch/movers-baseline.json").read_text())
    assert "interrupted" in status["note"]
