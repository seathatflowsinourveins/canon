"""Unit 10i: the carrying node's startup and stop, entirely offline.

All writes are under pytest's tmp_path. The node is built but never connected; runtime tests use
the real OwnerProtector with unit 10's synthetic cache/transport and stand-in account reads.
"""

from __future__ import annotations

import fcntl
import importlib
import importlib.util
import signal
import subprocess
import tomllib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from canon_alpaca.execution import AlpacaExecutionClient
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window as window
from canon_trading.owner_protector import OwnerProtector
from tests.test_owner_protector import Harness
from tests.test_owner_protector import new_quote
from tests.test_owner_protector import working_sell

ROOT = Path(__file__).resolve().parents[1]
AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline tests."""


def subject() -> Any:
    assert importlib.util.find_spec("canon_trading.protector_node") is not None, (
        "the owner protector runner is not built"
    )
    return importlib.import_module("canon_trading.protector_node")


@pytest.fixture
def controls(tmp_path):
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    return holds, inbox


def config(controls, **changes):
    values = {"account": "windows", "row": "OWN1", "holds_dir": controls[0], "inbox": controls[1]}
    return subject().ProtectorRun(**(values | changes))


def market(at: str):
    now = datetime.fromisoformat(at).replace(tzinfo=window.NEW_YORK)
    days = [
        Calendar(
            date=d,
            open="09:30",
            close="16:00",
            session_open="0400",
            session_close="2000",
        )
        for d in ("2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17")
    ]
    clock = Clock(
        timestamp=now,
        is_open=now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0),
        next_open=now.replace(hour=9, minute=30),
        next_close=now.replace(hour=16, minute=0),
    )
    return clock, days


def test_task_runs_the_protector_module_with_required_row_and_simulation_limit():
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"].get("paper:protector")
    assert task is not None, "paper:protector is not registered"
    assert "canon_trading.protector_node" in task["run"]
    assert 'flag "--row <row>" required=#true' in task["usage"]
    assert "SIMULATION ONLY" in task["description"]
    assert "new fill" in task["description"]
    assert "paper:protector" in (ROOT / "AGENTS.md").read_text()


def test_one_real_node_has_only_owner_protector_and_no_external_claim(controls, monkeypatch):
    added = []
    build = paper_node.alpaca_paper_builder

    class Node:
        def __init__(self, node):
            self.node = node

        def add_strategy(self, strategy):
            added.append(strategy)
            self.node.add_strategy(strategy)

        def dispose(self):
            self.node.dispose()

    def builder(*args, **kwargs):
        real = build(*args, **kwargs)
        return SimpleNamespace(build=lambda: Node(real.build()))

    monkeypatch.setattr(paper_node, "alpaca_paper_builder", builder)
    node, strategy = subject().protector_node(config(controls), (AAA, BBB), feed="sip")
    try:
        assert type(strategy) is OwnerProtector
        assert added == [strategy]
        assert strategy.trader_id == paper_node.TRADER_ID
        assert strategy.settings.external_order_instrument_ids is None
        assert strategy.settings.manage_stop is False
        assert strategy.settings.levels == {}
    finally:
        node.dispose()


def test_every_held_instrument_is_loaded_before_reconciliation(controls, monkeypatch):
    seen = {}
    original = paper_node.exec_engine_config

    def reconciliation(instruments):
        result = original(instruments)
        seen["reconciliation"] = result.reconciliation_instrument_ids
        return result

    monkeypatch.setattr(paper_node, "exec_engine_config", reconciliation)
    for name, factory in (
        ("data", paper_node.AlpacaDataClientFactory),
        ("execution", paper_node.AlpacaExecutionClientFactory),
    ):
        create = factory.create

        def capture(*, _name=name, _create=create, **kwargs):
            seen[_name] = kwargs["config"]
            return _create(**kwargs)

        monkeypatch.setattr(factory, "create", staticmethod(capture))
    node, _strategy = subject().protector_node(config(controls), (AAA, BBB), feed="sip")
    try:
        assert seen["reconciliation"] == [str(AAA), str(BBB)]
        assert seen["data"].instrument_provider.load_ids == [str(AAA), str(BBB)]
        assert seen["execution"].instrument_provider.load_ids == [str(AAA), str(BBB)]
        assert seen["execution"].report_external_orders is True
        assert seen["execution"].report_external_trailing_stops is True
        assert seen["execution"].reduce_only_sells is True
    finally:
        node.dispose()


def test_client_order_ids_are_of_the_protect_lineage(controls):
    """The lead's ruling (16:04:49): the protector's sells are PROTECT by custody's one rule."""
    node, strategy = subject().protector_node(config(controls), (AAA,), feed="sip")
    try:
        cid = str(strategy._protect_id())
        assert custody.lineage(cid) == custody.PROTECT
        assert custody.lineage(cid) != custody.OURS
    finally:
        node.dispose()


@pytest.mark.parametrize("row", [None, "", "F2", "typo"])
def test_row_is_explicit_and_f2_is_refused_by_name(controls, row):
    module = subject()
    with pytest.raises((ValueError, TypeError), match="row|F2"):
        if row is None:
            module.ProtectorRun(account="tests", holds_dir=controls[0], inbox=controls[1])
        else:
            config(controls, row=row)


@pytest.mark.parametrize("text", ["AAA=0", "AAA=-1", "AAA=NaN", "AAA=1,AAA=2", "AAA", "=1"])
def test_bad_levels_are_refused_without_reading_account(text):
    module = subject()
    with pytest.raises(ValueError):
        module.parse_levels(text)


def test_levels_are_optional_and_reach_only_named_holdings(controls):
    module = subject()
    assert module.parse_levels("") == {}
    levels = module.parse_levels("AAA=9.50,BBB=11.25")
    node, strategy = module.protector_node(config(controls, levels=levels), (AAA, BBB), feed="sip")
    try:
        assert strategy.settings.levels == {
            AAA: Price.from_str("9.50"),
            BBB: Price.from_str("11.25"),
        }
        assert strategy.settings.external_order_instrument_ids is None
    finally:
        node.dispose()


def test_a_level_cannot_bind_to_an_instrument_absent_at_start(controls):
    module = subject()
    with pytest.raises(ValueError, match="BBB.*holding"):
        module.protector_node(
            config(controls, levels={BBB: Price.from_str("8.00")}), (AAA,), feed="sip"
        )


@pytest.mark.parametrize(
    ("at", "minutes", "expected_feed", "end"),
    [
        ("2026-11-13T04:00:00", 960, "sip", "2026-11-13T20:00:00"),
        ("2026-11-13T10:00:00", 120, "sip", "2026-11-13T12:00:00"),
        ("2026-11-13T15:59:00", 960, "sip", "2026-11-13T20:00:00"),
        ("2026-11-13T19:59:00", 30, "sip", "2026-11-13T20:00:00"),
        ("2026-11-16T20:00:00", 960, "boats", "2026-11-17T04:00:00"),
        ("2026-11-13T02:00:00", 960, "boats", "2026-11-13T04:00:00"),
    ],
)
def test_carrying_bound_is_one_feed_span(at, minutes, expected_feed, end):
    subject()
    until, _part, feed = window.protection_span(*market(at), minutes=minutes)
    assert feed == expected_feed
    assert until == datetime.fromisoformat(end).replace(tzinfo=window.NEW_YORK)


@pytest.mark.parametrize("minutes", [0, -1, 961])
def test_carrying_bound_refuses_invalid_minutes(controls, minutes):
    subject()
    with pytest.raises(ValueError, match="minutes"):
        config(controls, minutes=minutes)


@pytest.mark.parametrize("feed", ["iex", "boats"])
def test_wrong_feed_is_refused_by_name_in_regular_session(feed):
    subject()
    with pytest.raises(window.WindowRefused, match="IEX|boats"):
        window.protection_span(*market("2026-11-13T10:00:00"), minutes=30, feed=feed)


def test_weekend_has_no_invented_session():
    subject()
    with pytest.raises(window.WindowRefused, match="trading day|session"):
        window.protection_span(*market("2026-11-14T10:00:00"), minutes=30)


@pytest.mark.parametrize("blocking", ["holds", "inbox", "both"])
def test_start_reads_holds_and_inbox_together_and_refuses_either(controls, tmp_path, blocking):
    subject()
    if blocking in ("holds", "both"):
        (controls[0] / "pause.txt").write_text("stop this run")
    if blocking in ("inbox", "both"):
        controls[1].write_text("unread lead message")
    with (
        pytest.raises(window.WindowRefused) as caught,
        window.paper_start("windows", holds_dir=controls[0], inbox=controls[1], lock_dir=tmp_path),
    ):
        pytest.fail("a held start reached the body")
    if blocking in ("holds", "both"):
        assert "holds" in str(caught.value)
    if blocking in ("inbox", "both"):
        assert "inbox" in str(caught.value)


def test_missing_start_controls_are_a_refusal(controls, tmp_path):
    subject()
    controls[1].unlink()
    with (
        pytest.raises(window.WindowRefused, match="inbox"),
        window.paper_start("windows", holds_dir=controls[0], inbox=controls[1], lock_dir=tmp_path),
    ):
        pytest.fail("an unread start reached the body")


@pytest.mark.parametrize(
    ("account", "lock_name"),
    [
        ("tests", "canon-paper-test.lock"),
        ("windows", "canon-paper-windows.lock"),
        ("protect", "canon-paper-protect.lock"),
        ("windows", "canon-alpaca-stream-windows.lock"),
        ("tests", "canon-alpaca-stream-tests.lock"),
    ],
)
def test_start_refuses_account_or_stream_lock_without_waiting(
    controls, tmp_path, account, lock_name
):
    subject()
    with (tmp_path / lock_name).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(window.WindowRefused, match="lock.*held"),
            window.paper_start(
                account, holds_dir=controls[0], inbox=controls[1], lock_dir=tmp_path
            ),
        ):
            pytest.fail("held lock admitted the node")


def test_start_holds_both_locks_for_whole_run_then_releases_them(controls, tmp_path):
    subject()
    names = ("canon-paper-windows.lock", "canon-alpaca-stream-windows.lock")
    with window.paper_start("windows", holds_dir=controls[0], inbox=controls[1], lock_dir=tmp_path):
        for name in names:
            with (tmp_path / name).open("a") as competing, pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for name in names:
        with (tmp_path / name).open("a") as competing:
            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("go", [None, "tests OWN1", "protect OWN3", "protect", "go"])
def test_account_c_needs_go_naming_account_and_row_before_reads(
    controls, tmp_path, monkeypatch, go
):
    module = subject()
    monkeypatch.setattr(window, "paper_trading", lambda: pytest.fail("read before C's go"))
    with pytest.raises(window.WindowRefused, match="lead.*go.*protect.*OWN1"):
        module.run_protector(config(controls, account="protect", lead_go=go), lock_dir=tmp_path)


def runtime(monkeypatch, controls, *, account="windows", lead_go=None):
    module = subject()
    strategy = Harness(row="OWN1", order_id_tag="001")
    foreign = working_sell("owner-order", qty="10", filled="0", instrument_id=BBB)
    strategy.cached.working.append(foreign)
    strategy.adopt_holding(AAA, Price.from_str("9.50"))
    new_quote(strategy)  # placement waits one quote received after the adoption (L6's round 2)
    reads = []
    built = []
    node = SimpleNamespace(dispose=lambda: reads.append("dispose"))

    def positions(_trading):
        reads.append("positions")
        return {"AAA": Decimal(10), "BBB": Decimal(2)}

    monkeypatch.setattr(window, "positions", positions)
    monkeypatch.setattr(window, "open_orders", lambda _trading: [])
    clock, calendar = market("2026-11-13T10:00:00")
    monkeypatch.setattr(
        window, "session_now", lambda _trading: (clock, calendar, window.time.monotonic())
    )

    def build(settings, instruments, *, feed):
        built.append((settings, instruments, feed))
        return node, strategy

    monkeypatch.setattr(module, "protector_node", build)
    monkeypatch.setattr(window, "_run_node", lambda node, ends: strategy.on_stop())
    settings = config(controls, account=account, lead_go=lead_go)
    return module, settings, strategy, foreign, reads, built


def test_stop_sells_nothing_and_cancels_no_owner_order_and_names_each_left_long(
    controls, tmp_path, monkeypatch, capsys
):
    module, settings, strategy, foreign, reads, built = runtime(monkeypatch, controls)
    before = list(strategy.sent)
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert strategy.sent == before
    assert strategy.canceled == [before[0].client_order_id]
    assert foreign in strategy.cached.working
    assert foreign.client_order_id not in strategy.canceled
    assert strategy.cached.held[0].signed_qty == Decimal(10)
    assert reads[0] == "positions"
    assert len(built) == 1
    assert set(built[0][1]) == {AAA, BBB}
    said = capsys.readouterr().out
    assert "AAA" in said and "BBB" in said and "unprotected" in said


def test_each_released_sell_has_close_only_intent_and_adapter_opt_in(controls, monkeypatch):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    assert module is not None and settings.row == "OWN1"
    order = strategy.sent[0]
    assert order.is_reduce_only
    # The emulator releases a LIMIT; the adapter takes the reduce-only bit into its request.
    limit = strategy.factory.limit(
        AAA, OrderSide.SELL, order.quantity, Price.from_str("9.50"), reduce_only=True
    )
    request = AlpacaExecutionClient._order_request(limit, extended_hours=True)
    assert request.position_intent is not None
    assert request.position_intent.value == "sell_to_close"
    assert request.extended_hours is True
    assert strategy.params == [{"extended_hours": True}]


def test_account_c_named_go_reaches_node(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, _reads, built = runtime(
        monkeypatch, controls, account="protect", lead_go="protect OWN1"
    )
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert len(built) == 1


def test_account_a_stream_lock_refuses_before_any_account_read(controls, tmp_path, monkeypatch):
    """Outside the recorder's hours, a held stream lock on A refuses before any account read. Only
    Alpaca's clock is read first, for the recorder's hours (the lead's ruling, 16:04:49)."""
    module = subject()
    clock, calendar = market("2026-11-12T20:30:00")
    monkeypatch.setattr(window, "paper_trading", lambda: "trading")
    monkeypatch.setattr(
        window, "session_now", lambda _trading: (clock, calendar, window.time.monotonic())
    )
    monkeypatch.setattr(window, "positions", lambda _trading: pytest.fail("read behind the lock"))
    with (tmp_path / "canon-alpaca-stream-tests.lock").open("a") as recorder:
        fcntl.flock(recorder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(window.WindowRefused, match="stream.*lock.*held"):
            module.run_protector(config(controls, account="tests"), lock_dir=tmp_path)


def test_hold_arriving_during_account_read_refuses_node(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, _reads, built = runtime(monkeypatch, controls)

    def held(_trading):
        (controls[0] / "new-hold").write_text("stop")
        return {"AAA": Decimal(10)}

    monkeypatch.setattr(window, "positions", held)
    with pytest.raises(window.WindowRefused, match="holds"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert not built


def test_cli_requires_row_and_controls_and_refuses_iex(controls, capsys):
    module = subject()
    with pytest.raises(SystemExit):
        module.parse_args(["windows"])
    assert "--row" in capsys.readouterr().err
    argv = [
        "windows",
        "--row",
        "OWN1",
        "--holds-dir",
        str(controls[0]),
        "--inbox",
        str(controls[1]),
    ]
    args = module.parse_args(argv)
    assert args.levels == {} and args.row == "OWN1"
    with pytest.raises(SystemExit):
        module.parse_args([*argv, "--feed", "iex"])
    assert "IEX" in capsys.readouterr().err


def test_signalled_main_exits_as_the_window_signal(controls, monkeypatch):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))

    def stopped(_config):
        raise window.WindowSignalled("stopped by SIGTERM")

    monkeypatch.setattr(module, "run_protector", stopped)
    assert module.main([]) == 128 + signal.SIGTERM


def test_failed_end_read_names_last_seen_holdings(controls, tmp_path, monkeypatch, capsys):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    first = window.positions
    calls = []

    def positions(trading):
        calls.append(None)
        if len(calls) > 1:
            raise OSError("end read unavailable")
        return first(trading)

    monkeypatch.setattr(window, "positions", positions)
    before = list(strategy.sent)
    with pytest.raises(RuntimeError, match="end"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    said = capsys.readouterr().out
    assert "AAA" in said and "BBB" in said and "last seen" in said
    assert strategy.sent == before


def test_failed_disposal_still_names_holdings_and_never_sells(
    controls, tmp_path, monkeypatch, capsys
):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)

    def dispose():
        raise OSError("dispose failed")

    monkeypatch.setattr(
        module,
        "protector_node",
        lambda *_args, **_kwargs: (SimpleNamespace(dispose=dispose), strategy),
    )
    before = list(strategy.sent)
    with pytest.raises(RuntimeError, match="dispose"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    said = capsys.readouterr().out
    assert "AAA" in said and "BBB" in said
    assert strategy.sent == before


def test_failed_end_read_does_not_mask_the_node_failure(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)

    def failed_node(_node, _ends):
        def failed_read(_trading):
            raise OSError("end read unavailable")

        monkeypatch.setattr(window, "positions", failed_read)
        raise RuntimeError("primary node failure")

    monkeypatch.setattr(window, "_run_node", failed_node)
    with pytest.raises(RuntimeError, match="primary node failure"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)


def test_signal_during_end_read_still_names_holdings(controls, tmp_path, monkeypatch, capsys):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    before = window.positions

    def stop(_node, _ends):
        strategy.on_stop()
        window._SIGNALS.left_node()

        def signalled(trading):
            # Exercise the handler directly, never deliver an OS signal to pytest.
            window._SIGNALS.handle(signal.SIGTERM, None)
            return before(trading)

        monkeypatch.setattr(window, "positions", signalled)

    monkeypatch.setattr(window, "_run_node", stop)
    with pytest.raises(window.WindowSignalled, match="stopped by SIGTERM"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    said = capsys.readouterr().out
    assert "AAA" in said and "BBB" in said and "unprotected" in said


def test_abandoned_node_forces_process_exit_even_on_base_exception(controls, monkeypatch):
    module = subject()
    exited = []
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(window, "_ABANDONED", [object()])

    def aborted(_config):
        raise SystemExit(7)

    monkeypatch.setattr(module, "run_protector", aborted)
    monkeypatch.setattr(module.os, "_exit", exited.append)
    with pytest.raises(SystemExit):
        module.main([])
    assert exited == [1]


# The lead's ruling (2026-11-13, 16:04:49) on the composed review's P1: the movers recorder streams on
# account A's pair on weekdays 04:00-20:00 New York and holds no stream lock, so the protector
# refuses A by name in those hours, by Alpaca's clock, before any account read.
@pytest.mark.parametrize(
    "at", ["2026-11-13T04:00:00", "2026-11-13T10:00:00", "2026-11-13T19:59:00"]
)
def test_account_a_is_refused_in_the_recorders_hours_with_no_lock_held(
    controls, tmp_path, monkeypatch, at
):
    module, settings, _strategy, _foreign, reads, built = runtime(
        monkeypatch, controls, account="tests"
    )
    clock, calendar = market(at)
    monkeypatch.setattr(
        window, "session_now", lambda _trading: (clock, calendar, window.time.monotonic())
    )
    locks = tmp_path / "locks"
    locks.mkdir()
    # A paper test holds A's lock: the refusal still names the recorder, so it came before any lock.
    with (locks / "canon-paper-test.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(window.WindowRefused, match="recorder"):
            module.run_protector(settings, trading=object(), lock_dir=locks)
    assert not built, "a node was built on A's pair in the recorder's hours"
    assert "positions" not in reads, "the refusal must come before any account read"
    with (locks / "canon-paper-test.lock").open("a") as free:
        fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB)  # and no lock was left taken


@pytest.mark.parametrize("at", ["2026-11-12T20:30:00", "2026-11-16T03:00:00"])
def test_account_a_outside_the_recorders_hours_is_not_refused_for_it(
    controls, tmp_path, monkeypatch, at
):
    module, settings, _strategy, _foreign, _reads, built = runtime(
        monkeypatch, controls, account="tests"
    )
    clock, calendar = market(at)
    monkeypatch.setattr(
        window, "session_now", lambda _trading: (clock, calendar, window.time.monotonic())
    )
    locks = tmp_path / "locks"
    locks.mkdir()
    module.run_protector(settings, trading=object(), lock_dir=locks)
    assert built


def git_checkout(path: Path, *, branch: str | None, tag_main: bool = False) -> Path:
    """A repository's main checkout, on `branch` (None: detached)."""
    path.mkdir()
    run = lambda *args: subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    if tag_main:
        run("tag", "main")
    if branch is None:
        run("checkout", "-q", "--detach")
    elif branch != "main":
        run("checkout", "-q", "-b", branch)
    return path


def linked_worktree(repo: Path, path: Path, *, branch: str | None, existing: bool = False) -> Path:
    """A checkout of its own: a linked worktree at the repository's commit."""
    args = ["worktree", "add", "-q"]
    if branch is None:
        args += ["--detach", str(path)]
    else:
        args += [str(path), branch] if existing else ["-b", branch, str(path)]
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return path


# The lead's rule 2a (2026-11-13, 16:04:49): a protector never runs from the main checkout, which
# the maintenance agent fast-forwards; it runs from a checkout of its own at a commit.
def test_a_run_from_a_checkout_on_main_is_refused_before_anything(
    controls, tmp_path, monkeypatch, capsys
):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("started on main"))
    assert module.main([], checkout=git_checkout(tmp_path / "main", branch="main")) == 3
    assert "main checkout" in capsys.readouterr().err


# The pnode fix review's P2: the main checkout is refused by what it is, not only by its branch.
@pytest.mark.parametrize(
    ("branch", "tag_main"), [("protector-run", False), (None, False), ("protector-run", True)]
)
def test_the_main_checkout_off_main_or_detached_is_refused(
    controls, tmp_path, monkeypatch, capsys, branch, tag_main
):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("main checkout"))
    checkout = git_checkout(tmp_path / "main", branch=branch, tag_main=tag_main)
    assert module.main([], checkout=checkout) == 3
    assert "main checkout" in capsys.readouterr().err


def test_a_linked_worktree_on_the_branch_main_is_refused(controls, tmp_path, monkeypatch):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("on main"))
    repo = git_checkout(tmp_path / "repo", branch="elsewhere")
    assert (
        module.main(
            [], checkout=linked_worktree(repo, tmp_path / "own", branch="main", existing=True)
        )
        == 3
    )


@pytest.mark.parametrize("branch", [None, "protector-run"])
def test_a_run_from_a_checkout_of_its_own_starts(controls, tmp_path, monkeypatch, branch):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    started = []
    monkeypatch.setattr(module, "run_protector", started.append)
    repo = git_checkout(tmp_path / "repo", branch="main", tag_main=True)
    assert module.main([], checkout=linked_worktree(repo, tmp_path / "own", branch=branch)) == 0
    assert started


def test_a_checkout_that_cannot_be_read_is_refused(controls, tmp_path, monkeypatch):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("started unchecked"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    (tmp_path / "plain").mkdir()
    assert module.main([], checkout=tmp_path / "plain") == 3


def test_the_task_entry_point_checks_its_own_checkout():
    source = (ROOT / "src/canon_trading/protector_node.py").read_text()
    assert "raise SystemExit(main(checkout=CHECKOUT))" in source


# The lead's ruling (d)2 (2026-11-13): before proof 4, on account C the task refuses a tree that is
# not clean at a commit on main; a dirty tree on B stays a named limit.
def committed(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", name], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", name], check=True, capture_output=True)


def own_tree_on_main(tmp_path: Path) -> Path:
    repo = git_checkout(tmp_path / "repo", branch="main")
    committed(repo, "tracked.txt", "main\n")
    return linked_worktree(repo, tmp_path / "own", branch=None)


def c_go(controls) -> Any:
    return config(controls, account="protect", lead_go="protect OWN1")


@pytest.mark.parametrize("dirt", ["modified", "untracked", "staged"])
def test_on_c_a_tree_not_clean_is_refused(controls, tmp_path, monkeypatch, capsys, dirt):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: c_go(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("dirty tree on C"))
    own = own_tree_on_main(tmp_path)
    if dirt == "modified":
        (own / "tracked.txt").write_text("changed\n")
    elif dirt == "untracked":
        (own / "extra.py").write_text("x = 1\n")
    else:
        (own / "tracked.txt").write_text("staged\n")
        subprocess.run(["git", "-C", str(own), "add", "tracked.txt"], check=True)
    assert module.main([], checkout=own) == 3
    assert "not clean" in capsys.readouterr().err


def test_on_c_a_commit_not_on_main_is_refused(controls, tmp_path, monkeypatch, capsys):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: c_go(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("off main on C"))
    repo = git_checkout(tmp_path / "repo", branch="main")
    own = linked_worktree(repo, tmp_path / "own", branch="unreviewed")
    committed(own, "change.txt", "unreviewed\n")
    assert module.main([], checkout=own) == 3
    assert "not a commit on main" in capsys.readouterr().err


def test_on_c_the_origin_main_decides_when_there_is_one(controls, tmp_path, monkeypatch, capsys):
    """A local main ahead of origin/main is not reviewed: origin's main decides."""
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: c_go(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("ahead of origin"))
    origin = git_checkout(tmp_path / "origin", branch="main")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    committed(repo, "local.txt", "not on origin\n")
    own = linked_worktree(repo, tmp_path / "own", branch=None)
    assert module.main([], checkout=own) == 3
    assert "not a commit on main" in capsys.readouterr().err


def test_on_c_a_clean_tree_at_a_commit_on_main_starts(controls, tmp_path, monkeypatch):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: c_go(controls))
    started = []
    monkeypatch.setattr(module, "run_protector", started.append)
    assert module.main([], checkout=own_tree_on_main(tmp_path)) == 0
    assert started


@pytest.mark.parametrize("account", ["windows", "tests"])
def test_off_c_a_dirty_tree_is_a_named_limit_and_starts(controls, tmp_path, monkeypatch, account):
    module = subject()
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls, account=account))
    started = []
    monkeypatch.setattr(module, "run_protector", started.append)
    own = own_tree_on_main(tmp_path)
    (own / "tracked.txt").write_text("changed\n")
    assert module.main([], checkout=own) == 0
    assert started
