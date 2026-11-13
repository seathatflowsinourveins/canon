"""The F-1 follow-up's properties P1-P11, through the base's CLI, window and record contracts.

The node, market/account reads and strategy record are offline stand-ins. P2 and P3
delegate a monkeypatched run_window to the real window layer; the other CLI cases
stop at that seam. No credential, Alpaca request or paper task is used.

Upstream contracts: argparse's type conversion and add_argument_group at
https://docs.python.org/3/library/argparse.html#type and #argument-groups;
pytest's monkeypatch and parametrize how-to guides. P12, the paper windows' evidence in
ADR 0106, is not a claim these synthetic records can establish.
"""

import json
import re
import signal
import socket
import time
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import NoReturn

import pytest

from canon_trading import movers_rank
from canon_trading import session_window as sw

BASE = "7715cb6f89878c46afea41100836ffc7286ea5ee"
NO_SPREAD = "no usable quoted spread (missing/zero bid, crossed or locked): fixed trail fallback"
COLD_ATR = "ATR has fewer than 14 complete one-minute bars: fixed trail fallback"
F2_FLAGS = ("--arm", "F2", "--trail-mode", "fixed", "--trail-cap", "clamp", "--atr-k", "1")
OWN1_FLAGS = ("--arm", "OWN1", "--trail-mode", "atr", "--trail-cap", "own")
OWN3_FLAGS = (*OWN1_FLAGS[:1], "OWN3", *OWN1_FLAGS[2:], "--atr-k", "3")
ARM_CASES = [
    pytest.param((), "OWN1", True, id="default-OWN1"),
    pytest.param(F2_FLAGS, "F2", False, id="explicit-F2"),
]
PROFILE_CASES = [
    pytest.param(("--profile-k", "2"), id="profile-k-at-row"),
    pytest.param(("--profile-k", "3"), id="profile-k-away"),
    pytest.param(("--profile-pctl", "p75"), id="profile-pctl-at-row"),
    pytest.param(("--profile-pctl", "p90"), id="profile-pctl-away"),
    pytest.param(("--profile-floor", "median"), id="profile-floor-at-row"),
    pytest.param(("--profile-floor", "off"), id="profile-floor-away"),
    pytest.param(("--profile-multipliers", "1", "1", "1"), id="profile-multipliers"),
    pytest.param(("--trail-profile-path", "unused-profile.json"), id="trail-profile-path"),
]


def _forbidden_io(*_args: Any, **_kwargs: Any) -> NoReturn:
    pytest.fail("blind test reached an unstubbed account, credential, network or node seam")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared paper fixture so this offline file never inspects key variables."""


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _forbidden_io)
    for name in ("paper_trading", "window_data", "window_screener", "window_node", "run_window"):
        monkeypatch.setattr(sw, name, _forbidden_io)
    monkeypatch.setattr(sw, "EXTENDED_READY", True)
    monkeypatch.setattr(sw, "paper_account", lambda: "windows")
    monkeypatch.setenv(sw.ACCOUNT_ENV, "windows")  # an alias, never a key or account number
    monkeypatch.setattr(sw, "running_commit", lambda: BASE)
    monkeypatch.setattr(sw, "request_counts", lambda: ())
    monkeypatch.setattr(sw, "endpoint_counts", dict)
    monkeypatch.setattr(sw, "condition_counts", dict)
    monkeypatch.setattr(sw, "_ABANDONED", [])
    monkeypatch.setattr(sw, "_SIGNALS", sw._WindowSignals())


# Capture the base's public runner before the autouse guard replaces it.
REAL_RUN_WINDOW = sw.run_window


def _argv(log_dir: Path, *flags: str) -> list[str]:
    return [*flags, "--log-dir", str(log_dir)]


def _read(log_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((sw.records_dir(log_dir) / name).read_text())


def _hear(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    written: list[str] = []
    write = sw.write_heartbeat

    def capture(log_dir: Path, outcome: str) -> None:
        write(log_dir, outcome)
        written.append(_read(log_dir, "last-firing.json")["outcome"])

    monkeypatch.setattr(sw, "write_heartbeat", capture)
    return written


def _assert_arm(outcome: str, arm: str | None, default: bool) -> None:
    if arm is None:
        assert "arm " not in outcome
    else:
        assert f"arm {arm}" in outcome, outcome
    assert ("(the default)" in outcome) is default, outcome


def _finished(config: sw.WindowConfig) -> sw.WindowReport:
    return sw.WindowReport(
        config.session,
        config.symbol,
        started="20261113T120000Z",
        ended="20261113T120001Z",
        result="ok",
        strategy=config.strategy,
        arm=config.arm,
        arm_default=config.arm_default,
        commit=BASE,
        account="windows",
    )


def _market(monkeypatch: pytest.MonkeyPatch, *, regular: bool = False) -> None:
    at = datetime(2026, 11, 10, 11 if regular else 17, tzinfo=sw.NEW_YORK)
    days = [
        SimpleNamespace(
            date=(at - timedelta(days=offset)).date(),
            open=(at - timedelta(days=offset)).replace(hour=9, minute=30),
            close=(at - timedelta(days=offset)).replace(hour=16, minute=0),
        )
        for offset in (1, 0)
    ]
    clock = SimpleNamespace(timestamp=at, is_open=regular)
    monkeypatch.setattr(sw, "paper_trading", object)
    monkeypatch.setattr(sw, "session_now", lambda _trading: (clock, days, time.monotonic()))
    monkeypatch.setattr(
        sw,
        "session_part",
        lambda *_args: (at + timedelta(hours=2), "regular" if regular else "after-hours"),
    )
    monkeypatch.setattr(sw, "warm_up_start", lambda *_args, **_kw: at - timedelta(hours=1))


class _RecordedMover(SimpleNamespace):
    """Only the strategy's public record/telemetry seam, never a trading strategy."""

    def record(self) -> dict[str, Any]:
        return self.run_report


def _offline_window(
    monkeypatch: pytest.MonkeyPatch,
    notes: tuple[tuple[str, ...], ...] = (),
) -> list[sw.WindowConfig]:
    """Run real window configuration and record handling around a synchronous fake node."""
    _market(monkeypatch)
    monkeypatch.setattr(sw, "window_data", object)
    monkeypatch.setattr(sw, "leftovers", lambda _trading: [])
    monkeypatch.setattr(sw, "venue_no_shorting", lambda _trading: True)
    monkeypatch.setattr(sw, "share_price", lambda *_args: Decimal(10))
    monkeypatch.setattr(sw, "mover_entry_limit", lambda *_args, **_kw: Decimal(10))
    monkeypatch.setattr(
        movers_rank, "read_baselines", lambda *_args: movers_rank.BaselineRead({}, None)
    )
    monkeypatch.setattr(
        sw,
        "mover_allocation",
        lambda _trading, slots: sw.MoverAllocation(
            Decimal(7000), Decimal(7000), slots, Decimal(1000)
        ),
    )
    monkeypatch.setattr(sw, "start_reference_read", lambda *_args: (None, None))
    monkeypatch.setattr(sw, "references_before_node", lambda *_args: {"synthetic": True})

    def pick(*_args: Any, **_kwargs: Any) -> sw.MoverPick:
        return sw.MoverPick(
            "BLND",
            Decimal(10),
            {"source": "stream", "symbol": "BLND", "inputs": {"move": 2}, "passed_over": {}},
            Decimal(10),
        )

    monkeypatch.setattr(sw, "choose_mover", pick)
    entries = [
        {"client_order_id": f"blind-{i}", "instrument_id": "BLND.ALPACA", "notes": list(said)}
        for i, said in enumerate(notes)
    ]
    run = {
        "entries": entries,
        # These are not entry rows and must not inflate either counter.
        "notes": {"WATCHED.ALPACA": [NO_SPREAD, COLD_ATR]},
        "trail_notes": [NO_SPREAD, COLD_ATR],
        "ranking_records": [{"published": True}],
        "opened_slots": ["BLND.ALPACA"] if entries else [],
        "fills": [],
    }
    telemetry = {
        "dropped": [],
        "history_bars": 0,
        "history_dropped": 0,
        "quotes": 1,
        "trades": 1,
        "live_bars": 0,
        "remakes_under_floor": 0,
    }
    strategy = _RecordedMover(
        **telemetry,
        opened_slots=(),
        settings=SimpleNamespace(max_notional=Decimal(1000)),
        clock=SimpleNamespace(timestamp_ns=lambda: 0),
        run_report=run,
    )
    monkeypatch.setattr(sw, "Mover", _RecordedMover)
    seen: list[sw.WindowConfig] = []

    def node(config: sw.WindowConfig, **kwargs: Any) -> tuple[Any, Any, Any]:
        seen.append(config)
        (config.log_dir / f"{kwargs['log_name']}.log").write_text("")
        cache = SimpleNamespace(orders=lambda **_kw: [], bars=lambda *_args: [])
        cap = SimpleNamespace(
            checks=1,
            peak=Decimal(0),
            tripped=None,
            instrument_id=config.instrument_id,
            max_position=Decimal(1),
        )
        observed = strategy if config.strategy == sw.MOVER else SimpleNamespace(**telemetry)
        return SimpleNamespace(cache=cache, dispose=lambda: None), observed, cap

    monkeypatch.setattr(sw, "window_node", node)
    monkeypatch.setattr(sw, "_run_node", lambda *_args: None)
    monkeypatch.setattr(sw, "_flatten_after_stop", lambda *_args, **_kw: [])

    def window(config: sw.WindowConfig, **kwargs: Any) -> sw.WindowReport:
        return REAL_RUN_WINDOW(config, **kwargs)

    monkeypatch.setattr(sw, "run_window", window)
    return seen


@pytest.mark.parametrize(
    ("error_type", "code", "prefix"),
    [
        pytest.param(sw.WindowSkipped, 0, "skipped:", id="skipped"),
        pytest.param(sw.WindowRefused, 1, "refused:", id="refused"),
        pytest.param(RuntimeError, 1, "failed: RuntimeError:", id="runtime-error"),
        pytest.param(LookupError, None, "failed: LookupError:", id="other-exception"),
    ],
)
@pytest.mark.parametrize(
    ("flags", "arm", "default"),
    [*ARM_CASES, pytest.param(None, None, False, id="SessionMaCross")],
)
def test_terminal_heartbeats_keep_the_start_arm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flags: tuple[str, ...] | None,
    arm: str | None,
    default: bool,
    error_type: type[Exception],
    code: int | None,
    prefix: str,
) -> None:
    """P1/P11: each later heartbeat keeps the start's arm; a skip writes no latest record."""
    heard = _hear(monkeypatch)

    def window(config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        assert (config.arm, config.arm_default) == (arm, default)
        raise error_type("blind terminal outcome")

    monkeypatch.setattr(sw, "run_window", window)
    selected = () if flags is None else ("--mover", *flags)
    args = sw._args(_argv(tmp_path, "--session", "extended", *selected))
    if code is None:
        with pytest.raises(error_type, match="blind terminal outcome"):
            sw._main(args)
    else:
        assert sw._main(args) == code
    assert len(heard) == 2
    assert heard[-1].startswith(prefix)
    assert _read(tmp_path, "last-firing.json")["outcome"] == heard[-1]
    for outcome in heard:
        _assert_arm(outcome, arm, default)
    if error_type is sw.WindowSkipped:
        assert not (sw.records_dir(tmp_path) / "latest.json").exists()


def test_auto_bridge_clears_the_default_arm_and_uses_fixed_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2: inject the currently unreachable bridge row and inspect the downstream node config."""
    assert all(not row.bridge_reason for row in sw.MOVER_PARTS.values())
    parts = dict(sw.MOVER_PARTS)
    parts["after-hours"] = replace(parts["after-hours"], bridge_reason="blind bridge fixture")
    monkeypatch.setattr(sw, "MOVER_PARTS", parts)
    seen = _offline_window(monkeypatch)
    args = sw._args(_argv(tmp_path, "--session", "auto", "--mover"))
    assert sw._main(args) == 0
    assert len(seen) == 1
    config = seen[0]
    assert config.strategy == sw.DEFAULT_STRATEGY == "SessionMaCross"
    assert (config.arm, config.arm_default) == (None, False)
    assert {name: getattr(config, name) for name in sw.TRAIL_FLAGS} == sw.TRAIL_FLAGS
    assert config.trail_mode == "fixed"


@pytest.mark.parametrize("output", ["latest", "printed"])
@pytest.mark.parametrize(
    ("notes", "strategy", "spread", "cold"),
    [
        pytest.param(((NO_SPREAD,), (NO_SPREAD,)), sw.MOVER, 2, 0, id="spread-only"),
        pytest.param(((COLD_ATR,), (COLD_ATR,)), sw.MOVER, 0, 2, id="cold-only"),
        pytest.param(
            (
                (NO_SPREAD, NO_SPREAD),
                (NO_SPREAD,),
                (COLD_ATR, COLD_ATR),
                (
                    (
                        "ATR has fewer than 21 complete one-minute bars: fixed trail fallback, "
                        "raised to the spread floor"
                    ),
                ),
                ("ordinary entry note",),
                (),
            ),
            sw.MOVER,
            2,
            2,
            id="mixed-count-entries-once",
        ),
        pytest.param((("ordinary entry note",), ()), sw.MOVER, 0, 0, id="no-fallback"),
        pytest.param(
            (("ATR has fewer than 14 complete one-minute bars: opening uses the fixed trail",),),
            sw.MOVER,
            0,
            0,
            id="opening-is-not-the-cold-fallback",
        ),
        pytest.param((), sw.MOVER, 0, 0, id="no-entries"),
        pytest.param(((NO_SPREAD,), (COLD_ATR,)), sw.DEFAULT_STRATEGY, 0, 0, id="non-Mover"),
    ],
)
def test_window_records_count_entry_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    notes: tuple[tuple[str, ...], ...],
    strategy: str,
    spread: int,
    cold: int,
    output: str,
) -> None:
    """P3: integer counts on disk and stdout, by entry, independent of other run notes."""
    seen = _offline_window(monkeypatch, notes)
    flags = ("--mover",) if strategy == sw.MOVER else ()
    assert sw._main(sw._args(_argv(tmp_path, "--session", "extended", *flags))) == 0
    assert len(seen) == 1
    printed = json.loads(capsys.readouterr().out)
    record = _read(tmp_path, "latest.json") if output == "latest" else printed
    assert record["result"] == "ok"
    for name, expected in (("entries_no_usable_spread", spread), ("entries_cold_fallback", cold)):
        assert name in record, f"{output} report lacks {name}"
        assert type(record[name]) is int
        assert record[name] == expected


def test_nonnumeric_atr_k_is_a_named_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P4: Decimal conversion failures must follow argparse's documented type= contract."""
    with pytest.raises(SystemExit) as stopped:
        sw._args(_argv(tmp_path, "--session", "extended", "--mover", "--atr-k", "abc"))
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert "usage:" in output.err and "--atr-k" in output.err
    assert "Traceback" not in output.err + output.out
    assert not (sw.records_dir(tmp_path) / "latest.json").exists()


@pytest.mark.parametrize("value", ["1", "2", "3"])
def test_registered_atr_multiples_still_parse(tmp_path: Path, value: str) -> None:
    """P4 compatibility: these already parse at the base."""
    args = sw._args(
        _argv(tmp_path, "--session", "extended", "--mover", "--trail-mode", "atr", "--atr-k", value)
    )
    assert args.atr_k == Decimal(value)


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(("--trail-mode", "atr"), id="atr"),
        pytest.param(("--profile-k", "3"), id="profile-k"),
        pytest.param(("--profile-pctl", "p90"), id="profile-pctl"),
        pytest.param(("--profile-floor", "off"), id="profile-floor"),
        pytest.param(("--profile-multipliers", "1", "1.25", "1.5"), id="profile-multipliers"),
    ],
)
def test_non_mover_prints_one_named_notice_and_keeps_it_in_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
) -> None:
    """P5: an auto's regular SessionMaCross part says an unused trail once, by flag."""
    _market(monkeypatch, regular=True)
    heard = _hear(monkeypatch)

    def window(config: sw.WindowConfig, **_kwargs: Any) -> sw.WindowReport:
        assert config.strategy == "SessionMaCross"
        return _finished(config)

    monkeypatch.setattr(sw, "run_window", window)
    args = sw._args(_argv(tmp_path, "--session", "auto", "--mover", *flags))
    assert sw._main(args) == 0
    output = capsys.readouterr()
    notices = [
        line
        for line in (output.out + output.err).splitlines()
        if "not run" in line or "keeps the fixed trail" in line
    ]
    assert len(notices) == 1, notices
    assert f"{' '.join(flags)} not run:" in notices[0]
    assert f"{' '.join(flags)} not run" in heard[-1]
    _assert_arm(heard[-1], None, False)


def test_trail_flag_defaults_equal_f2_rows() -> None:
    """P6: equality is observable; constructing one from the other needs code review."""
    assert sw.TRAIL_FLAGS == {
        name: sw.TRAIL_ARMS["F2"][name] for name in ("trail_mode", "trail_cap", "atr_k")
    }


def test_help_groups_all_mover_trail_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """P7: rendered help places all nine flags together under a Mover trail title."""
    with pytest.raises(SystemExit) as stopped:
        sw._parser().parse_args(["--help"])
    assert stopped.value.code == 0
    heading = ""
    groups: dict[str, str] = {}
    for line in capsys.readouterr().out.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            heading = line[:-1]
        if match := re.match(r"\s{2,}(--[a-z-]+)(?:\s|$)", line):
            groups[match.group(1)] = heading
    flags = (
        "--trail-mode",
        "--trail-cap",
        "--atr-k",
        "--arm",
        "--profile-k",
        "--profile-pctl",
        "--profile-floor",
        "--profile-multipliers",
        "--trail-profile-path",
    )
    assert all(flag in groups for flag in flags), groups
    titles = {groups[flag] for flag in flags}
    assert len(titles) == 1, groups
    title = titles.pop().lower()
    assert "mover" in title and "trail" in title, title


def test_trail_cli_defaults_and_choices_are_preserved() -> None:
    """P7 compatibility: grouping help must leave flag spellings, rows and None defaults intact."""
    parser = sw._parser()
    defaults = parser.parse_args([])
    values = {
        "trail_mode": ("fixed", "atr", "tier", "ratchet", "profile"),
        "trail_cap": ("clamp", "refuse", "own"),
        "atr_k": ("1", "2", "3"),
        "arm": ("F2", "OWN1", "OWN3"),
        "profile_k": ("1", "2", "3"),
        "profile_pctl": ("median", "p75", "p90"),
        "profile_floor": ("median", "p75", "off"),
    }
    for name, choices in values.items():
        assert getattr(defaults, name) is None
        flag = "--" + name.replace("_", "-")
        for value in choices:
            assert str(getattr(parser.parse_args([flag, value]), name)) == value
        with pytest.raises(SystemExit) as stopped:
            parser.parse_args([flag, "999"])
        assert stopped.value.code == 2
    assert defaults.profile_multipliers is None and defaults.trail_profile_path is None
    assert parser.parse_args(["--profile-multipliers", "1", "1.25", "1.5"]).profile_multipliers == [
        Decimal(1),
        Decimal("1.25"),
        Decimal("1.5"),
    ]
    assert parser.parse_args(["--trail-profile-path", "example.json"]).trail_profile_path == Path(
        "example.json"
    )


@pytest.mark.parametrize(("flags", "arm", "default"), ARM_CASES)
@pytest.mark.parametrize(
    "change_namespace", [False, True], ids=["unchanged", "mutated-after-build"]
)
def test_refusal_records_the_built_configs_arm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flags: tuple[str, ...],
    arm: str,
    default: bool,
    change_namespace: bool,
) -> None:
    """P8: the Namespace can change after construction; the refused config remains authoritative."""
    args = sw._args(_argv(tmp_path, "--session", "extended", "--mover", *flags))

    def window(config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        assert (config.arm, config.arm_default) == (arm, default)
        if change_namespace:
            args.arm = "OWN3" if arm == "F2" else "F2"
            for name, value in sw.TRAIL_ARMS[args.arm].items():
                setattr(args, name, value)
        raise sw.WindowRefused("blind refusal after build")

    monkeypatch.setattr(sw, "run_window", window)
    assert sw._main(args) == 1
    latest = _read(tmp_path, "latest.json")
    assert latest["result"] == "refused: blind refusal after build"
    assert (latest["arm"], latest["arm_default"]) == (arm, default)


def _named_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    named: str,
) -> None:
    ran: list[sw.WindowConfig] = []

    def window(config: sw.WindowConfig, **_kwargs: Any) -> sw.WindowReport:
        ran.append(config)
        return _finished(config)

    monkeypatch.setattr(sw, "run_window", window)
    try:
        code = sw._main(sw._args(_argv(tmp_path, "--session", "extended", "--mover", *flags)))
    except SystemExit as stopped:
        code = stopped.code
    output = capsys.readouterr()
    assert not ran, f"{named} reached run_window under {ran[0].trail_mode}"
    assert code in (1, 2)
    assert named in output.out + output.err
    assert "Traceback" not in output.out + output.err
    if code == 1:
        assert _read(tmp_path, "last-firing.json")["outcome"].startswith("refused:")


@pytest.mark.parametrize("profile", PROFILE_CASES)
@pytest.mark.parametrize(
    "arm_flags",
    [
        pytest.param((), id="alone"),
        pytest.param(F2_FLAGS, id="F2"),
        pytest.param(OWN1_FLAGS, id="OWN1"),
        pytest.param(OWN3_FLAGS, id="OWN3"),
    ],
)
def test_profile_flags_are_refused_by_name_outside_profile_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    profile: tuple[str, ...],
    arm_flags: tuple[str, ...],
) -> None:
    """P9: presence, even at the profile's default row, must not silently run another trail."""
    _named_refusal(monkeypatch, tmp_path, capsys, (*arm_flags, *profile), profile[0])


@pytest.mark.parametrize("value", ["2", "3"])
@pytest.mark.parametrize("arm_flags", [(), ("--arm", "F2")], ids=["unarmed", "F2"])
def test_nondefault_atr_k_is_refused_by_name_in_fixed_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    value: str,
    arm_flags: tuple[str, ...],
) -> None:
    """P9: a nondefault ATR multiple cannot be silently ignored by the fixed plan."""
    _named_refusal(
        monkeypatch,
        tmp_path,
        capsys,
        (*arm_flags, "--trail-mode", "fixed", "--atr-k", value),
        "--atr-k",
    )


@pytest.mark.parametrize("profile", PROFILE_CASES)
def test_profile_mode_keeps_its_valid_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: tuple[str, ...]
) -> None:
    """P9 compatibility: the new refusal must allow profile flags in profile mode."""
    ran: list[sw.WindowConfig] = []

    def window(config: sw.WindowConfig, **_kwargs: Any) -> sw.WindowReport:
        ran.append(config)
        return _finished(config)

    monkeypatch.setattr(sw, "run_window", window)
    args = sw._args(
        _argv(tmp_path, "--session", "extended", "--mover", "--trail-mode", "profile", *profile)
    )
    assert sw._main(args) == 0
    assert len(ran) == 1
    assert (ran[0].trail_mode, ran[0].arm, ran[0].arm_default) == ("profile", None, False)


def test_no_trail_flags_still_runs_default_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P9 compatibility: refusing inappropriate flags must not change the no-flag default."""
    ran: list[sw.WindowConfig] = []

    def window(config: sw.WindowConfig, **_kwargs: Any) -> sw.WindowReport:
        ran.append(config)
        return _finished(config)

    monkeypatch.setattr(sw, "run_window", window)
    assert sw._main(sw._args(_argv(tmp_path, "--session", "extended", "--mover"))) == 0
    assert len(ran) == 1
    assert (ran[0].arm, ran[0].arm_default) == (sw.DEFAULT_ARM, True)


@pytest.mark.parametrize(
    ("flags", "arm", "default", "reason"),
    [
        pytest.param(("--arm", "OWN1"), "OWN1", False, "OWN1", id="contradicted-OWN1"),
        pytest.param(
            ("--arm", "OWN3", "--trail-mode", "atr"), "OWN3", False, "OWN3", id="contradicted-OWN3"
        ),
        pytest.param(
            ("--arm", "F2", "--trail-mode", "atr"), "F2", False, "F2", id="contradicted-F2"
        ),
        pytest.param(("--minutes", "0"), "OWN1", True, "minutes", id="default-bad-minutes"),
        pytest.param(("--spares", "-1"), "OWN1", True, "spares", id="default-bad-spares"),
        pytest.param(("--slots", "0"), "OWN1", True, "slots", id="default-bad-slots"),
        pytest.param(
            ("--arm", "F2", "--minutes", "0"), "F2", False, "minutes", id="F2-bad-minutes"
        ),
        pytest.param(
            ("--trail-mode", "fixed", "--minutes", "0"),
            None,
            False,
            "minutes",
            id="unarmed-bad-minutes",
        ),
    ],
)
def test_config_validation_refusals_write_arm_records(
    tmp_path: Path, flags: tuple[str, ...], arm: str | None, default: bool, reason: str
) -> None:
    """P10: configuration ValueErrors are refusals, with the requested arm on disk."""
    assert sw._main(sw._args(_argv(tmp_path, "--session", "extended", "--mover", *flags))) == 1
    latest_path = sw.records_dir(tmp_path) / "latest.json"
    assert latest_path.exists(), "configuration refusal wrote no latest.json"
    latest = json.loads(latest_path.read_text())
    assert latest["result"].startswith("refused:")
    assert reason in latest["result"]
    assert latest["strategy"] == sw.MOVER
    assert (latest["arm"], latest["arm_default"]) == (arm, default)
    assert _read(tmp_path, "last-firing.json")["outcome"].startswith("refused:")


@pytest.mark.parametrize("open_result", ["running", "stopping: SIGTERM"])
def test_a_refusal_leaves_a_record_that_says_the_account_may_not_be_flat(
    tmp_path: Path, open_result: str
) -> None:
    """A record whose window may have died (`running` or `stopping`, which `scripts/automation status`
    reads as DIED, the account may not be flat) is never replaced by a run that did not establish the
    account is flat: a configuration refusal leaves it as it is and is heard in the heartbeat."""
    records = sw.records_dir(tmp_path)
    records.mkdir(parents=True, exist_ok=True)
    died = {
        "session": "extended",
        "strategy": sw.MOVER,
        "result": open_result,
        "started_epoch": 1.0,
        "started": "20261113T010000Z",
        "stale_after": 60,
    }
    (records / "latest.json").write_text(json.dumps(died))
    before = (records / "latest.json").read_bytes()
    argv = _argv(tmp_path, "--session", "extended", "--mover", "--minutes", "0")
    assert sw._main(sw._args(argv)) == 1
    assert (records / "latest.json").read_bytes() == before
    assert _read(tmp_path, "last-firing.json")["outcome"].startswith("refused:")


@pytest.mark.parametrize("prior", ["ok", "refused: an earlier refusal"])
def test_a_refusal_replaces_a_closed_record(tmp_path: Path, prior: str) -> None:
    """A record of a window that ended ok, or of an earlier refusal, is replaced by the refusal's."""
    records = sw.records_dir(tmp_path)
    records.mkdir(parents=True, exist_ok=True)
    (records / "latest.json").write_text(json.dumps({"result": prior, "started_epoch": 1.0}))
    argv = _argv(tmp_path, "--session", "extended", "--mover", "--minutes", "0")
    assert sw._main(sw._args(argv)) == 1
    assert json.loads((records / "latest.json").read_text())["result"].startswith("refused:")


def test_a_refusal_leaves_a_record_that_says_the_account_was_not_left_flat(tmp_path: Path) -> None:
    """A closed record that names holdings left (`left`, which `scripts/automation status` prints) says
    the account may not be flat as plainly as a DIED one; a configuration refusal, which read no
    account, leaves it as it is."""
    records = sw.records_dir(tmp_path)
    records.mkdir(parents=True, exist_ok=True)
    unflat = {
        "result": "failed: not left flat: ['BLND 5']",
        "left": ["BLND 5"],
        "started_epoch": 1.0,
    }
    (records / "latest.json").write_text(json.dumps(unflat))
    before = (records / "latest.json").read_bytes()
    argv = _argv(tmp_path, "--session", "extended", "--mover", "--minutes", "0")
    assert sw._main(sw._args(argv)) == 1
    assert (records / "latest.json").read_bytes() == before
    assert _read(tmp_path, "last-firing.json")["outcome"].startswith("refused:")


def test_a_refusal_keeps_a_failed_record_whose_account_was_not_read(tmp_path: Path) -> None:
    """A window whose account check after the stop raised ends `failed` with nothing in `left`: the
    account was never read, so a refusal that read none either leaves that record as it is."""
    records = sw.records_dir(tmp_path)
    records.mkdir(parents=True, exist_ok=True)
    failed = {
        "result": "failed: RuntimeError: the account could not be read in 60 s after the stop",
        "left": [],
        "started_epoch": 1.0,
    }
    (records / "latest.json").write_text(json.dumps(failed))
    before = (records / "latest.json").read_bytes()
    argv = _argv(tmp_path, "--session", "extended", "--mover", "--minutes", "0")
    assert sw._main(sw._args(argv)) == 1
    assert (records / "latest.json").read_bytes() == before


def test_a_bridged_configuration_error_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bridged part's configuration that WindowConfig refuses is `refused`, with its record, as
    every configuration error is."""
    monkeypatch.setitem(
        sw.MOVER_PARTS,
        "after-hours",
        replace(sw.MOVER_PARTS["after-hours"], bridge_reason="blind bridge fixture"),
    )
    _offline_window(monkeypatch)
    args = sw._args(_argv(tmp_path, "--session", "auto", "--mover"))
    monkeypatch.setattr(sw, "DEFAULT_STRATEGY", "NoSuchStrategy")
    assert sw._main(args) == 1
    assert _read(tmp_path, "last-firing.json")["outcome"].startswith("refused: strategy must be")
    assert _read(tmp_path, "latest.json")["result"].startswith("refused: strategy must be")


@pytest.mark.parametrize(
    "prior",
    [{"result": "running", "started_epoch": 1.0}, {"result": "failed: x", "left": ["BLND 5"]}],
    ids=["died", "not-left-flat"],
)
def test_a_refusal_after_the_flat_check_replaces_a_record_that_may_not_be_flat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: dict[str, Any]
) -> None:
    """Once the window has read the account flat, the older record's warning is out of date: a refusal
    after that check (here, an account that allows shorting) writes its own record."""
    _offline_window(monkeypatch)
    monkeypatch.setattr(sw, "venue_no_shorting", lambda _trading: False)
    records = sw.records_dir(tmp_path)
    records.mkdir(parents=True, exist_ok=True)
    (records / "latest.json").write_text(json.dumps(prior))
    assert sw._main(sw._args(_argv(tmp_path, "--session", "extended", "--mover"))) == 1
    record = _read(tmp_path, "latest.json")
    assert record["result"].startswith("refused: the paper account allows shorting"), record
    assert not record.get("left")


@pytest.mark.parametrize(
    "error", [RuntimeError("not left flat"), KeyboardInterrupt()], ids=["failure", "raised"]
)
def test_a_failure_after_a_signal_names_the_signal_in_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    """A signal, then another failure: the heartbeat names both, so the stop is not read as the
    failure's alone."""
    monkeypatch.setattr(sw._SIGNALS, "first", None)

    def window(_config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        sw._SIGNALS.first = "SIGTERM"
        raise error

    monkeypatch.setattr(sw, "run_window", window)
    argv = sw._args(_argv(tmp_path, "--session", "extended", "--mover"))
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            sw._main(argv)
    else:
        assert sw._main(argv) == 1
    outcome = _read(tmp_path, "last-firing.json")["outcome"]
    assert outcome.startswith(f"failed: {type(error).__name__}"), outcome
    assert outcome.endswith(", arm OWN1 (the default); SIGTERM received during the run"), outcome


@pytest.mark.parametrize(
    "argv",
    [
        ["--session", "extended", "--arm", "F2"],
        ["--session", "extended", "--regular-strategy", "Mover"],
        ["--profile-multipliers", "9", "9", "9"],
    ],
)
def test_a_parser_refusal_writes_nothing_in_the_default_records_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    """With no `--log-dir`, a refusal while the command line is parsed never writes in
    `default_log_dir()`, the folder `scripts/automation status` reads: it is argparse's exit 2 and
    nothing else, so a test (or a mistyped command) cannot replace a running window's heartbeat."""
    real = tmp_path / "default-records"
    monkeypatch.setattr(sw, "default_log_dir", lambda: real)
    with pytest.raises(SystemExit) as error:
        sw._args(argv)
    assert error.value.code == 2
    assert not real.exists()


def test_value_error_after_build_remains_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P10 boundary: only config-time errors are reclassified as refusals."""

    def window(_config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        raise ValueError("blind runtime fault")

    monkeypatch.setattr(sw, "run_window", window)
    assert sw._main(sw._args(_argv(tmp_path, "--session", "extended", "--mover"))) == 1
    assert _read(tmp_path, "last-firing.json")["outcome"].startswith(
        "failed: ValueError: blind runtime fault"
    )


class _ProcessExit(BaseException):
    """Intercept os._exit without ending pytest or running any abandoned task."""


@pytest.mark.parametrize(("flags", "arm", "default"), ARM_CASES)
@pytest.mark.parametrize("abandoned", [False, True], ids=["not-abandoned", "abandoned"])
def test_signalled_window_updates_heartbeat_even_without_abandonment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flags: tuple[str, ...],
    arm: str,
    default: bool,
    abandoned: bool,
) -> None:
    """P11: signal completion must replace the running heartbeat in both exit paths."""
    heard = _hear(monkeypatch)
    expected_code = 128 + signal.SIGTERM

    def window(_config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        if abandoned:
            sw._ABANDONED.append(object())
        raise sw.WindowSignalled("stopped by SIGTERM")

    def exit_process(code: int) -> NoReturn:
        assert code == expected_code
        raise _ProcessExit

    monkeypatch.setattr(sw, "run_window", window)
    monkeypatch.setattr(sw.os, "_exit", exit_process)
    argv = _argv(tmp_path, "--session", "extended", "--mover", *flags)
    if abandoned:
        with pytest.raises(_ProcessExit):
            sw.main(argv)
    else:
        assert sw.main(argv) == expected_code
    assert heard[-1].startswith("failed: WindowSignalled: stopped by SIGTERM"), heard
    _assert_arm(heard[-1], arm, default)
    assert _read(tmp_path, "last-firing.json")["outcome"] == heard[-1]


@pytest.mark.parametrize(
    ("flags", "arm", "default"),
    [*ARM_CASES, pytest.param(None, None, False, id="SessionMaCross")],
)
def test_nothing_to_run_names_the_intended_arm_without_a_window_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flags: tuple[str, ...] | None,
    arm: str | None,
    default: bool,
) -> None:
    """P11: an auto's idle firing names the selected trail and still creates no window record."""
    _market(monkeypatch)
    monkeypatch.setattr(sw, "resolve_session", lambda *_args, **_kw: (None, "blind closed session"))
    selected = () if flags is None else ("--mover", *flags)
    assert sw._main(sw._args(_argv(tmp_path, "--session", "auto", *selected))) == 0
    outcome = _read(tmp_path, "last-firing.json")["outcome"]
    assert outcome.startswith("nothing to run:")
    assert not (sw.records_dir(tmp_path) / "latest.json").exists()
    _assert_arm(outcome, arm, default)


@pytest.mark.parametrize(("flags", "arm", "default"), ARM_CASES)
def test_a_signal_during_wind_down_closes_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flags, arm, default
) -> None:
    """H1: the real runner returns its failed report after a wind-down signal."""
    _offline_window(monkeypatch)
    heard = _hear(monkeypatch)

    def flatten(*_args: Any, **_kwargs: Any) -> list:
        sw._SIGNALS.handle(signal.SIGTERM, None)
        return []

    monkeypatch.setattr(sw, "_flatten_after_stop", flatten)
    assert sw.main(_argv(tmp_path, "--session", "extended", "--mover", *flags)) == 143
    report = _read(tmp_path, "latest.json")
    assert report["result"].startswith("failed: WindowSignalled: SIGTERM after the node stopped")
    assert heard[-1].startswith(report["result"])
    _assert_arm(heard[-1], arm, default)
    assert _read(tmp_path, "last-firing.json")["outcome"] == heard[-1]


def test_a_refusal_after_the_first_record_preserves_the_windows_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H2/H6: the CLI must not replace a real window record with a thin refusal."""
    _offline_window(monkeypatch)

    def refused(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise sw.WindowRefused("offline refusal after the first record")

    monkeypatch.setattr(sw, "window_node", refused)
    assert sw.main(_argv(tmp_path, "--session", "extended", "--mover")) == 1
    record = _read(tmp_path, "latest.json")
    assert record["result"] == "failed: WindowRefused: offline refusal after the first record"
    assert record["part"] == "after-hours"
    assert record["planned_seconds"] > 0
    assert record["ended"]
    assert (tmp_path / "latest.json").read_bytes() == (
        tmp_path / f"window-{record['started']}.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "flags",
    [("--reentry-arm", "turn"), ("--slots", "3"), ("--arm", "F2")],
)
def test_a_bridge_resets_every_mover_field_and_names_its_unused_flag_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, flags
) -> None:
    """H3: every bridge uses SessionMaCross's defaults, including explicit Mover rows."""
    assert all(not row.bridge_reason for row in sw.MOVER_PARTS.values())
    monkeypatch.setitem(
        sw.MOVER_PARTS,
        "after-hours",
        replace(sw.MOVER_PARTS["after-hours"], bridge_reason="blind bridge fixture"),
    )
    seen = _offline_window(monkeypatch)
    assert sw.main(_argv(tmp_path, "--session", "auto", "--mover", *flags)) == 0
    assert len(seen) == 1
    config = seen[0]
    defaults = sw.WindowConfig(log_dir=tmp_path)
    assert config.strategy == sw.DEFAULT_STRATEGY
    for name in (
        *sw.TRAIL_SETTINGS,
        "trail_cap",
        "slots",
        "spares",
        "baseline_path",
        "trail_profile_path",
        "arm",
        "arm_default",
    ):
        assert getattr(config, name) == getattr(defaults, name), name
    output = capsys.readouterr()
    notices = [line for line in (output.out + output.err).splitlines() if "not run" in line]
    assert len(notices) == 1, notices
    assert " ".join(flags) in notices[0]
    assert "SessionMaCross" in notices[0]
    record = _read(tmp_path, "latest.json")
    assert record["strategy"] == sw.DEFAULT_STRATEGY
    assert (record["arm"], record["arm_default"]) == (None, False)


def test_an_auto_failure_before_the_clock_names_the_part_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H4: before the clock, this auto invocation has no selected strategy or arm."""
    _market(monkeypatch)

    def failed_clock(_trading: Any) -> NoReturn:
        raise RuntimeError("offline clock failed")

    monkeypatch.setattr(sw, "session_now", failed_clock)
    assert (
        sw.main(
            _argv(tmp_path, "--session", "auto", "--mover", "--regular-strategy", "SessionMaCross")
        )
        == 1
    )
    outcome = _read(tmp_path, "last-firing.json")["outcome"]
    assert outcome.startswith("failed: RuntimeError: offline clock failed")
    assert "part unresolved" in outcome
    assert "Mover" not in outcome
    _assert_arm(outcome, None, False)
    assert not (tmp_path / "latest.json").exists()


@pytest.mark.parametrize("regular", [False, True])
def test_an_auto_failure_after_the_clock_names_the_resolved_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, regular: bool
) -> None:
    """H4: once resolved, the same mixed auto invocation names the actual part's arm."""
    _market(monkeypatch, regular=regular)

    def failed_window(_config: sw.WindowConfig, **_kwargs: Any) -> NoReturn:
        raise RuntimeError("offline window failed")

    monkeypatch.setattr(sw, "run_window", failed_window)
    assert (
        sw.main(
            _argv(tmp_path, "--session", "auto", "--mover", "--regular-strategy", "SessionMaCross")
        )
        == 1
    )
    outcome = _read(tmp_path, "last-firing.json")["outcome"]
    assert "part unresolved" not in outcome
    _assert_arm(outcome, None if regular else "OWN1", not regular)
