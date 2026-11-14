"""Unit 10i: the carrying node's startup and stop, entirely offline.

All writes are under pytest's tmp_path. The node is built but never connected; runtime tests use
the real OwnerProtector with unit 10's synthetic cache/transport and stand-in account reads.
"""

from __future__ import annotations

import asyncio
import fcntl
import fnmatch
import gc
import importlib
import importlib.util
import json
import re
import signal
import subprocess
import threading
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
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Venue

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window as window
from canon_trading.owner_protector import OwnerProtector
from canon_trading.start_level import Entry
from canon_trading.start_level import LevelBook
from canon_trading.start_level import book_path
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import Harness
from tests.test_owner_protector import SyntheticData
from tests.test_owner_protector import SyntheticExecution
from tests.test_owner_protector import VenueOrders
from tests.test_owner_protector import new_quote
from tests.test_owner_protector import quote
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


def test_the_protector_has_only_the_alpaca_data_client(controls, monkeypatch):
    """No other quote source can bypass the Alpaca one-sided-quote drop."""
    added = []
    original = paper_node.LiveNode.builder

    class Builder:
        def __init__(self, native):
            self.native = native

        def add_data_client(self, name, factory, settings):
            added.append((name, factory))
            self.native = self.native.add_data_client(name, factory, settings)
            return self

        def __getattr__(self, name):
            def call(*args, **kwargs):
                result = getattr(self.native, name)(*args, **kwargs)
                if name == "build":
                    return result
                self.native = result
                return self

            return call

    monkeypatch.setattr(
        paper_node,
        "LiveNode",
        SimpleNamespace(
            builder=lambda *args, **kwargs: Builder(original(*args, **kwargs)),
        ),
    )
    node, _ = subject().protector_node(config(controls), (AAA,), feed="sip")
    try:
        assert added == [("ALPACA", paper_node.AlpacaDataClientFactory)]
    finally:
        node.dispose()


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
        return {"AAA": Decimal(10), "BBB": Decimal(2)}, {}

    monkeypatch.setattr(window, "position_reads", positions)
    monkeypatch.setattr(window, "open_orders", lambda _trading: [])
    clock, calendar = market("2026-11-13T10:00:00")
    monkeypatch.setattr(
        window, "session_now", lambda _trading: (clock, calendar, window.time.monotonic())
    )

    def build(settings, instruments, *, feed, book=None, **_kwargs):
        built.append((settings, instruments, feed))
        return node, strategy

    monkeypatch.setattr(module, "protector_node", build)
    monkeypatch.setattr(window, "_run_node", lambda node, ends: strategy.on_stop())
    # Every start hands the protector its GET-only reader, book or none (Codex's P1-a); offline
    # here it is a stand-in, and booked() replaces it where a case names its reader.
    monkeypatch.setattr(module, "_trading_reader", lambda: "reader")
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
    monkeypatch.setattr(
        window, "position_reads", lambda _trading: pytest.fail("read behind the lock")
    )
    with (tmp_path / "canon-alpaca-stream-tests.lock").open("a") as recorder:
        fcntl.flock(recorder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(window.WindowRefused, match="stream.*lock.*held"):
            module.run_protector(config(controls, account="tests"), lock_dir=tmp_path)


def test_hold_arriving_during_account_read_refuses_node(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, _reads, built = runtime(monkeypatch, controls)

    def held(_trading):
        (controls[0] / "new-hold").write_text("stop")
        return {"AAA": Decimal(10)}, {}

    monkeypatch.setattr(window, "position_reads", held)
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
        "--symbol-holds-dir",
        str(controls[0]),
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
    first = window.position_reads
    calls = []

    def positions(trading):
        calls.append(None)
        if len(calls) > 1:
            raise OSError("end read unavailable")
        return first(trading)

    monkeypatch.setattr(window, "position_reads", positions)
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

        monkeypatch.setattr(window, "position_reads", failed_read)
        raise RuntimeError("primary node failure")

    monkeypatch.setattr(window, "_run_node", failed_node)
    with pytest.raises(RuntimeError, match="primary node failure"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)


def test_signal_during_end_read_still_names_holdings(controls, tmp_path, monkeypatch, capsys):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    before = window.position_reads

    def stop(_node, _ends):
        strategy.on_stop()
        window._SIGNALS.left_node()

        def signalled(trading):
            # Exercise the handler directly, never deliver an OS signal to pytest.
            window._SIGNALS.handle(signal.SIGTERM, None)
            return before(trading)

        monkeypatch.setattr(window, "position_reads", signalled)

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
    # --basetemp may be inside our worktree; the plain folder must not discover that parent.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(module, "parse_args", lambda _argv: config(controls))
    monkeypatch.setattr(module, "run_protector", lambda _config: pytest.fail("started unchecked"))
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


# U-g: the successor's bounded wait on the account's locks (the lead's ruling (d)4, 2026-11-13).


def held_lock(tmp_path: Path, name: str = "canon-paper-windows.lock"):
    """A second open file description of the lock: flock(2) conflicts with it in this process too."""
    held = (tmp_path / name).open("a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return held


def test_lock_wait_takes_the_lock_once_released_and_reads_nothing_before(
    controls, tmp_path, monkeypatch
):
    module, _settings, _strategy, _foreign, reads, _built = runtime(monkeypatch, controls)
    settings = config(controls, lock_wait=5)
    held = held_lock(tmp_path)
    released: list[float] = []

    def release() -> None:
        released.append(window.time.monotonic())
        held.close()

    first_read: list[float] = []
    positions = window.position_reads

    def stamped(trading):
        first_read.append(window.time.monotonic())
        return positions(trading)

    monkeypatch.setattr(window, "position_reads", stamped)
    timer = threading.Timer(0.6, release)
    timer.start()
    try:
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    finally:
        timer.cancel()
        held.close()
    assert released and first_read and first_read[0] >= released[0]
    assert reads[0] == "positions"


def test_lock_wait_refuses_by_name_past_its_bound_with_no_read(controls, tmp_path, monkeypatch):
    module, _settings, _strategy, _foreign, reads, built = runtime(monkeypatch, controls)
    held = held_lock(tmp_path)
    started = window.time.monotonic()
    try:
        with pytest.raises(window.WindowRefused, match=r"lock is held after waiting 1 s"):
            module.run_protector(config(controls, lock_wait=1), trading=object(), lock_dir=tmp_path)
    finally:
        held.close()
    assert 1 <= window.time.monotonic() - started < 3
    assert reads == [] and built == []


def test_lock_wait_never_retries_around_a_hold(controls, tmp_path, monkeypatch):
    module, _settings, _strategy, _foreign, reads, built = runtime(monkeypatch, controls)
    (controls[0] / "owner-hold").write_text("stop")
    started = window.time.monotonic()
    with pytest.raises(window.WindowRefused, match="holds folder is not empty"):
        module.run_protector(config(controls, lock_wait=5), trading=object(), lock_dir=tmp_path)
    assert window.time.monotonic() - started < 1
    assert reads == [] and built == []


def test_no_lock_wait_still_refuses_at_once(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, reads, _built = runtime(monkeypatch, controls)
    held = held_lock(tmp_path)
    started = window.time.monotonic()
    try:
        with pytest.raises(window.WindowRefused, match="lock is held; nothing started"):
            module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    finally:
        held.close()
    assert window.time.monotonic() - started < 1 and reads == []


@pytest.mark.parametrize("value", ["-1", "301", "x"])
def test_lock_wait_is_bounded(controls, value):
    with pytest.raises(SystemExit):
        subject().parse_args(
            ["windows", "--row", "OWN1", "--holds-dir", str(controls[0]), "--inbox"]
            + [str(controls[1]), "--symbol-holds-dir", str(controls[0]), "--lock-wait", value]
        )


def test_lock_wait_defaults_to_none_and_parses(controls):
    base = [
        "windows",
        "--row",
        "OWN1",
        "--holds-dir",
        str(controls[0]),
        "--inbox",
        str(controls[1]),
        "--symbol-holds-dir",
        str(controls[0]),
    ]
    assert subject().parse_args(base).lock_wait == 0
    assert subject().parse_args([*base, "--lock-wait", "90"]).lock_wait == 90


def test_start_line_names_its_end_as_an_epoch_for_the_runner(
    controls, tmp_path, monkeypatch, capsys
):
    module, settings, *_rest = runtime(monkeypatch, controls)
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    line = next(x for x in capsys.readouterr().out.splitlines() if "; ends " in x)
    found = re.search(r"; ends (\S+) \(epoch (\d+)\)$", line)
    assert found is not None, line
    assert int(datetime.fromisoformat(found.group(1)).timestamp()) == int(found.group(2))


def test_task_passes_the_bounded_lock_wait():
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:protector"]
    assert 'flag "--lock-wait <lock_wait>"' in task["usage"]
    assert 'args+=(--lock-wait "$usage_lock_wait")' in task["run"]


def test_runner_retry_bound_is_the_stop_grace_and_a_minute():
    text = (ROOT / "scripts" / "automation").read_text()
    grace = re.search(r"^PROTECTOR_STOP_GRACE=(\d+)", text, re.MULTILINE)
    assert grace is not None, "scripts/automation has no PROTECTOR_STOP_GRACE"
    assert int(grace.group(1)) == window.STOP_GRACE_SECS
    assert (
        "PROTECTOR_RETRY_BOUND=${CANON_PROTECTOR_RETRY_BOUND:-$((PROTECTOR_STOP_GRACE + 60))}"
        in text
    )


def runner_arm(message: str) -> str:
    """The first arm of the runner's refusal `case` (scripts/automation, do_run_protector) that the
    node's printed refusal matches, read from the script itself, with bash's glob read as fnmatch's."""
    text = (Path(__file__).parents[1] / "scripts" / "automation").read_text()
    block = text.split("    err=$(grep -E 'paper:protector: (refused|failed)'", 1)[1]
    block = block.split('    case "$err" in\n', 1)[1].split("    esac", 1)[0]
    line = f"paper:protector: refused: {message}"
    for arm in re.findall(r"^      (\*[^)]*|'')\)", block, flags=re.MULTILINE):
        for pattern in arm.split("|"):
            if pattern != "''" and fnmatch.fnmatchcase(line, pattern.replace('"', "")):
                return arm
    return "*"


def test_the_runner_classes_each_node_refusal_by_its_own_words(controls, tmp_path, monkeypatch):
    """The runner reads only the node's printed words: each refusal it handles apart must match its arm."""
    module, settings, *_ = runtime(monkeypatch, controls)
    held = held_lock(tmp_path)
    try:
        with pytest.raises(window.WindowRefused) as now:
            module.run_protector(settings, trading=object(), lock_dir=tmp_path)
        with pytest.raises(window.WindowRefused) as waited:
            module.run_protector(config(controls, lock_wait=1), trading=object(), lock_dir=tmp_path)
    finally:
        held.close()
    assert runner_arm(str(waited.value)) == '*"lock is held after waiting"*'
    assert runner_arm(str(now.value)) == '*"lock is held"*'
    (controls[0] / "owner-hold").write_text("stop")
    with pytest.raises(window.WindowRefused) as hold:
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert runner_arm(str(hold.value)) == '*"holds folder"*|*"inbox"*'
    (controls[0] / "owner-hold").unlink()
    controls[1].write_text("unread")
    with pytest.raises(window.WindowRefused) as inbox:
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert runner_arm(str(inbox.value)) == '*"holds folder"*|*"inbox"*'


# The lead's ruling (2026-11-13, 17:10): the level book is read only once the account's locks are held.


def write_book(path: Path, *rows: tuple[str, str, str, int]) -> None:
    entries = [
        {
            "account_label": account,
            "instrument_id": instrument,
            "protection_start_ns": 1,
            "last_level": level,
            "quantity": "10",
            "written_ns": written,
            "run": "r",
        }
        for account, instrument, level, written in rows
    ]
    path.write_text(json.dumps({"schema": 1, "entries": entries}))


def booked(monkeypatch, controls, *, takes_book=True, takes_reader=False, reader=None):
    """runtime(), with the node builder recording the book settings it is handed."""
    parts = runtime(monkeypatch, controls)
    module, _settings, strategy, _foreign, _reads, built = parts
    node = SimpleNamespace(dispose=lambda: None)

    def build(settings, instruments, *, feed, book=None, **_kwargs):
        built.append((settings, instruments, feed, book))
        return node, strategy

    monkeypatch.setattr(module, "protector_node", build)
    monkeypatch.setattr(module, "_protector_takes_book", lambda: takes_book)
    monkeypatch.setattr(module, "_protector_takes_reader", lambda: takes_reader)
    monkeypatch.setattr(module, "_trading_reader", lambda: reader)
    return parts


def test_book_is_read_only_once_the_lock_is_held(controls, tmp_path, monkeypatch):
    module, _settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    book = tmp_path / "owner-levels.json"
    write_book(book, ("windows", "AAA.ALPACA", "9.5", 5))
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    held = held_lock(tmp_path)
    released: list[float] = []

    def release() -> None:
        # The predecessor's stop writes the book while it still holds the lock, then exits.
        write_book(book, ("windows", "AAA.ALPACA", "9.75", 6))
        released.append(window.time.monotonic())
        held.close()

    read: list[tuple[float, dict]] = []
    book_levels = module._book_levels

    def stamped(path, account):
        found = book_levels(path, account)
        read.append((window.time.monotonic(), found))
        return found

    monkeypatch.setattr(module, "_book_levels", stamped)
    timer = threading.Timer(0.6, release)
    timer.start()
    try:
        module.run_protector(config(controls, lock_wait=5), trading=object(), lock_dir=tmp_path)
    finally:
        timer.cancel()
        held.close()
    assert released and read and read[0][0] >= released[0]
    assert read[0][1] == {AAA: Price.from_str("9.75")}
    assert built[0][3] == {"account_label": "windows", "level_book_path": str(book)}


def test_every_book_level_of_this_account_reaches_the_protector_unfiltered(
    controls, tmp_path, monkeypatch
):
    """The lead's P11 ruling (16:55, narrowed 17:08): a level for a symbol not held at the start is
    kept and handed on; the protector, not this node, decides whether it binds."""
    module, settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    book = tmp_path / "owner-levels.json"
    write_book(
        book,
        ("windows", "AAA.ALPACA", "9.5", 5),
        ("windows", "AAA.ALPACA", "9.6", 7),
        ("protect", "BBB.ALPACA", "1.5", 9),
        ("windows", "ZZZ.ALPACA", "3", 9),
    )
    assert module._book_levels(book, "windows") == {
        AAA: Price.from_str("9.6"),
        InstrumentId.from_str("ZZZ.ALPACA"): Price.from_str("3"),
    }
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built[0][0].levels == {}
    assert built[0][3] == {"account_label": "windows", "level_book_path": str(book)}


def test_a_book_with_a_protector_that_reads_none_refuses_by_name(controls, tmp_path, monkeypatch):
    module, settings, _strategy, _foreign, _reads, built = booked(
        monkeypatch, controls, takes_book=False
    )
    book = tmp_path / "owner-levels.json"
    write_book(book, ("windows", "AAA.ALPACA", "9.5", 5))
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    with pytest.raises(window.WindowRefused, match="reads no level book"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built == []


def test_the_protector_gets_the_accounts_own_get_only_reader_under_the_lock(
    controls, tmp_path, monkeypatch, capsys
):
    """U-e's fix round (the deputy, 17:26): the protector decides carry from the venue's fills, read
    through a trading reader the runner supplies; this node hands it, built only under the locks."""
    reader = object()
    module, settings, _strategy, _foreign, _reads, built = booked(
        monkeypatch, controls, takes_reader=True, reader=reader
    )
    book = tmp_path / "owner-levels.json"
    write_book(book, ("windows", "AAA.ALPACA", "9.5", 5))
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built[0][3] == {
        "account_label": "windows",
        "level_book_path": str(book),
        "trading": reader,
    }
    assert "no trading reader" not in capsys.readouterr().out


def test_a_protector_with_no_reader_slot_is_named(controls, tmp_path, monkeypatch, capsys):
    module, settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    book = tmp_path / "owner-levels.json"
    write_book(book, ("windows", "AAA.ALPACA", "9.5", 5))
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert "trading" not in built[0][3]
    assert "takes no trading reader" in capsys.readouterr().out


def test_the_trading_reader_refuses_every_method_but_get_before_sending(monkeypatch):
    """Stand-in key values only (no credential is read): the reader's session has only GET-only
    adapters, so a POST is refused before anything is sent."""
    from canon_alpaca.providers import MethodRefused
    from canon_alpaca.providers import TimeoutAdapter
    from canon_alpaca.providers import session_of

    monkeypatch.setenv(window.ENV_API_KEY, "stand-in")
    monkeypatch.setenv(window.ENV_SECRET_KEY, "stand-in")
    reader = subject()._trading_reader()
    session = session_of(reader)
    assert session is not None
    adapters = list(session.adapters.values())
    assert adapters and all(isinstance(a, TimeoutAdapter) and a.get_only for a in adapters)
    with pytest.raises(MethodRefused):
        session.post("https://paper-api.alpaca.markets/v2/orders", json={})
    with pytest.raises(MethodRefused):
        session.delete("https://paper-api.alpaca.markets/v2/orders")


def test_this_trees_protector_takes_the_reader():
    """U-e is composed (L6, 2026-11-13): its OwnerProtector takes the GET-only `trading` reader."""
    assert subject()._protector_takes_reader() is True


def test_this_trees_protector_reads_the_book():
    """U-e is composed: its OwnerProtector takes `account_label` and `level_book_path`."""
    assert subject()._protector_takes_book() is True


@pytest.mark.parametrize("text", [None, "not json", '{"schema": 3, "entries": []}'])
def test_an_unreadable_book_refuses_the_start_by_name(controls, tmp_path, monkeypatch, text):
    module, settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    book = tmp_path / "owner-levels.json"
    if text is not None:
        book.write_text(text)
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    with pytest.raises(window.WindowRefused, match="level book unreadable"):
        module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built == []


# Composition (L6, 2026-11-13): U-e's protector checks overnight eligibility only when told the run is
# overnight (OwnerProtectorConfig.session_part, default "regular"); the node knows the part from
# window.protection_span and must hand it over, or an overnight run skips that check.
def test_run_protector_hands_the_span_part_to_the_node_builder(controls, tmp_path, monkeypatch):
    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    seen: list[dict[str, Any]] = []

    def build(settings, instruments, *, feed, book=None, **kwargs):
        seen.append({"feed": feed, **kwargs})
        return SimpleNamespace(dispose=lambda: None), strategy

    monkeypatch.setattr(module, "protector_node", build)
    span = window.protection_span

    def overnight(clock, calendar, *, minutes, feed):
        ends, _part, _feed = span(clock, calendar, minutes=minutes, feed=None)
        return ends, "overnight", "boats"

    monkeypatch.setattr(window, "protection_span", overnight)
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert seen == [{"feed": "boats", "session_part": "overnight"}]


def test_the_node_builder_puts_the_part_in_the_protectors_settings(controls, monkeypatch):
    run = config(controls)  # subject() reloads the module: patch only after it
    module = subject()
    made: list[Any] = []

    class Stop(Exception):
        pass

    def builder(*_args, **_kwargs):
        raise Stop

    real = module.OwnerProtectorConfig

    def recorded(**kwargs):
        made.append(real(**kwargs))
        return made[-1]

    monkeypatch.setattr(module, "OwnerProtectorConfig", recorded)
    monkeypatch.setattr(module.paper_node, "alpaca_paper_builder", builder)
    with pytest.raises(Stop):
        module.protector_node(run, (AAA,), feed="boats", session_part="overnight")
    assert made[0].session_part == "overnight"


def test_a_label_resolves_the_book_without_the_runner_environment(controls, tmp_path, monkeypatch):
    module = subject()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(module.LEVEL_BOOK_ENV, raising=False)
    supplied = {"account_label": "windows", "trading": VenueOrders()}
    node, strategy = module.protector_node(config(controls), (AAA,), feed="sip", book=supplied)
    try:
        assert strategy.settings.level_book_path == book_path("windows")
        assert strategy.settings.trading is supplied["trading"]
        assert set(supplied) == {"account_label", "trading"}, "the caller's book was mutated"
    finally:
        node.dispose()
        del node


def test_each_labelled_build_has_a_distinct_run_name(controls, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    runs = []
    for _ in range(2):
        gc.collect()
        node, strategy = subject().protector_node(
            config(controls), (AAA,), feed="sip", book={"account_label": "windows"}
        )
        try:
            runs.append(strategy.settings.run)
        finally:
            node.dispose()
            del node
    assert all(runs), "each start needs a name before it reaches the strategy"
    assert runs[0] != runs[1]


def test_the_identity_reader_is_injectable_at_the_builder(controls, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    venue = VenueOrders()
    made = []

    def reader():
        made.append(venue)
        return venue

    node, strategy = subject().protector_node(
        config(controls),
        (AAA,),
        feed="sip",
        book={"account_label": "windows"},
        trading_reader=reader,
    )
    try:
        assert strategy.settings.trading is venue
        assert made == [venue]
        assert venue.reads == [], "building must not ask the venue about holdings"
    finally:
        node.dispose()


def test_an_explicit_reader_is_not_replaced_by_the_factory(controls, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    venue = VenueOrders()

    def unused():
        pytest.fail("an explicit reader must not be replaced")

    node, strategy = subject().protector_node(
        config(controls),
        (AAA,),
        feed="sip",
        book={"account_label": "windows", "trading": venue},
        trading_reader=unused,
    )
    try:
        assert strategy.settings.trading is venue
    finally:
        node.dispose()


def test_an_unlabelled_build_does_not_construct_a_reader(controls):
    def unused():
        pytest.fail("an unlabelled build must not construct a reader")

    node, strategy = subject().protector_node(
        config(controls), (AAA,), feed="sip", trading_reader=unused
    )
    try:
        assert strategy.settings.account_label is None
        assert strategy.settings.level_book_path is None
        assert strategy.settings.run == ""
        assert strategy.settings.trading is None
    finally:
        node.dispose()


def test_the_runner_accepts_the_schema_the_composed_strategy_writes(
    controls, tmp_path, monkeypatch
):
    module, settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    path = tmp_path / "levels.json"
    LevelBook.write(
        path,
        {
            ("windows", AAA): Entry(
                Decimal("9.75"),
                1,
                Decimal(10),
                2,
                "earlier",
                distance=Decimal("0.20"),
                limit_offset=Decimal("0.05"),
            ),
        },
    )
    monkeypatch.setenv(module.LEVEL_BOOK_ENV, str(path))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built[0][3] == {"account_label": "windows", "level_book_path": str(path)}
    assert module._book_levels(path, "windows") == {AAA: Price.from_str("9.75")}


@pytest.mark.usefixtures("new_york_regular_hours")
def test_two_builder_segments_carry_the_level_distance_and_offset(controls, tmp_path, monkeypatch):
    """Real LiveNodes with synthetic client factories; the builder and strategy stay intact.

    Pin: live/src/python/node.rs:846-856,1326-1370 installs Python factories and the constructed
    strategy; execution/src/trailing.rs:61-102 keeps a sell's existing trigger unless it rises.
    All book and lock writes use the account's book_path under this test's CLAUDE_CONFIG_DIR.
    """
    module = subject()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(module.LEVEL_BOOK_ENV, raising=False)
    notes = []
    monkeypatch.setattr(OwnerProtector, "_say", lambda self, text: notes.append(text))
    venue = VenueOrders()
    tapes = [("10.30", "10.50"), ("10.30",)]
    snapshots = []
    requests = []
    runs = []
    gain_events = []
    path = book_path("windows")

    class Data(SyntheticData):
        async def _request_bars(self, request):
            requests.append(("bars", request.params))
            await super()._request_bars(request)

        async def _request_quotes(self, request):
            requests.append(("quotes", request.params))
            await super()._request_quotes(request)

        async def publish_tape(self):
            for bid in tapes[len(runs) - 1]:
                self._handle_data(quote(bid, self.clock.timestamp_ns()))
                await asyncio.sleep(0.1)
            published.set()

    class Execution(SyntheticExecution):
        async def _generate_position_status_reports(self, command):
            now = self.clock.timestamp_ns()
            return [
                PositionStatusReport(
                    AccountId("ALPACA-001"),
                    AAA,
                    PositionSide.LONG,
                    Quantity.from_int(10),
                    now - 3600 * SECOND,
                    now,
                    avg_px_open=Decimal("10.00"),
                )
            ]

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Execution(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )

    async def stop_when_armed(strategy, published):
        await published.wait()
        while AAA not in strategy.slots or strategy.slots[AAA].distance is None:
            await asyncio.sleep(0.05)
        slot = strategy.slots[AAA]
        snapshots.append((slot.level.as_decimal(), slot.distance.distance, slot.limit_offset))
        gain_events.append(list(strategy.gain_lock_events))
        strategy.shutdown_system("builder segment observed")

    async def run_segment(node, strategy, published):
        stop = asyncio.create_task(stop_when_armed(strategy, published))
        try:
            async with asyncio.timeout(20):
                await node.run_async()
            await stop
        finally:
            stop.cancel()

    monkeypatch.setattr(paper_node, "AlpacaDataClientFactory", DataFactory)
    monkeypatch.setattr(paper_node, "AlpacaExecutionClientFactory", ExecutionFactory)
    for _ in range(2):
        gc.collect()
        published = asyncio.Event()
        node, strategy = module.protector_node(
            config(controls, gain_lock=True),
            (AAA,),
            feed="sip",
            book={"account_label": "windows"},
            trading_reader=lambda: venue,
        )
        runs.append(strategy.settings.run)
        try:
            asyncio.run(run_segment(node, strategy, published))
        finally:
            node.dispose()
            del node
        saved = LevelBook.read(path)[("windows", AAA)]
        assert (saved.last_level, saved.distance, saved.limit_offset) == snapshots[-1]
        assert saved.run == runs[-1]
    assert snapshots[0] == snapshots[1], "a restart must carry all three exit values unchanged"
    assert runs[0] and runs[1] and runs[0] != runs[1]
    assert notes.count("level book enabled for account windows") == 2
    assert not any("level book disabled" in note for note in notes)
    assert any("carried level" in note and runs[0] in note for note in notes)
    (armed,) = gain_events[0]
    assert armed["event"] == "armed"
    assert Decimal(armed["entry"]) == Decimal("10.00")
    assert Decimal(armed["round_trip_cost"]) == 0
    assert Decimal(armed["noise_floor"]) == Decimal("0.04")  # Two synthetic 0.02 spreads.
    assert Decimal(armed["applied_floor"]) == Decimal("10.00")
    assert armed["armed_at"] > armed["level_armed_at"]
    assert [kind for kind, _params in requests].count("bars") == 2
    assert [kind for kind, _params in requests].count("quotes") == 2
    assert all(params["feed"] == "sip" for _kind, params in requests)
    assert all(
        params["latest"] and params["skip_catalog_data"] and not params["update_catalog"]
        for kind, params in requests
        if kind == "quotes"
    )
    assert [query["side"] for path, query in venue.reads if path == "/orders" and query] == [
        "sell",
        "buy",
    ]


def test_unbooked_overnight_start_can_select_an_eligible_long(controls, tmp_path, monkeypatch):
    reader = VenueOrders(asset={"attributes": ["overnight_tradable"]})
    module, settings, _, _, _, built = booked(
        monkeypatch, controls, takes_reader=True, reader=reader
    )
    monkeypatch.delenv(module.LEVEL_BOOK_ENV, raising=False)
    clock, calendar = market("2026-11-12T21:00:00")
    monkeypatch.setattr(window, "session_now", lambda _: (clock, calendar, window.time.monotonic()))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built[0][2] == "boats"
    probe = Harness(feed="boats", session_part="overnight", **(built[0][3] or {}))
    probe.on_start()
    assert AAA in probe.slots, probe.said


REAL_POSITION_READS = window.position_reads
"""The real per-symbol reader, kept before any test patches it, so the cases below run it unpatched."""


def listed(symbol: str, qty: str, side: str = "long") -> Any:
    """One position as alpaca-py models Alpaca's listing (`alpaca.trading.models.Position`)."""
    from alpaca.trading.models import Position as AlpacaPosition

    return AlpacaPosition.model_validate(
        {
            "asset_id": "64bbff51-59d6-4b3c-9351-13ad85e3c752",
            "symbol": symbol,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "10.00",
            "qty": qty,
            "side": side,
            "cost_basis": "100",
        }
    )


class Listing:
    """An account whose only read here is its positions, as alpaca-py's `get_all_positions` lists them."""

    def __init__(self, *positions: Any) -> None:
        self.listed = list(positions)

    def get_all_positions(self) -> list:
        return list(self.listed)


def real_reader(monkeypatch, controls):
    """`runtime`, with the real per-symbol reader between run_protector and alpaca-py's Positions."""
    found = runtime(monkeypatch, controls)
    monkeypatch.setattr(window, "position_reads", REAL_POSITION_READS)
    return found


def test_one_unreadable_position_at_the_start_is_skipped_by_name_and_every_other_long_is_covered(
    controls, tmp_path, monkeypatch, capsys
):
    """The reader follow-up's red case (2026-11-13): with the account listing AAA long 10 and BBB with a
    quantity that is no number, the start read raised PositionUnread and no node was built, so AAA got
    no protection. Each position is read by itself: BBB is named with an ALERT and left out of the
    node, and AAA is covered (the lead, 2026-11-13 21:27, 21:35 and 21:39)."""
    module, settings, _strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    trading = Listing(listed("AAA", "10"), listed("BBB", "NaN"))
    module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert len(built) == 1
    assert set(built[0][1]) == {AAA}
    said = capsys.readouterr().out
    start, end = [line for line in said.splitlines() if line.startswith("ALERT BBB:")]
    assert "position in BBB" in start and "skipped, nothing sent" in start, start
    assert "position in BBB" in end and "left unprotected by this node" in end, end


def test_a_level_for_an_unreadable_position_is_dropped_and_the_rest_start(
    controls, tmp_path, monkeypatch, capsys
):
    """A level supplied for a holding that cannot be read is left out with it, never a refusal of the
    whole start."""
    module, settings, _strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    settings = config(controls, levels={AAA: Price.from_str("9.50"), BBB: Price.from_str("1.50")})
    trading = Listing(listed("AAA", "10"), listed("BBB", "Infinity"))
    module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert len(built) == 1
    assert set(built[0][0].levels) == {AAA}
    assert set(built[0][1]) == {AAA}
    assert "ALERT BBB:" in capsys.readouterr().out


@pytest.mark.parametrize(("qty", "side"), [("-3", "long"), ("3", "short"), ("-3", "short")])
def test_a_short_by_its_label_or_its_sign_is_never_read_as_a_long_at_the_start(
    controls, tmp_path, monkeypatch, qty, side
):
    """Through the real reader a short, by its label or by its sign, is below zero: a level supplied
    for it finds no starting long and nothing starts."""
    module, _settings, _strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    settings = config(controls, levels={BBB: Price.from_str("1.50")})
    trading = Listing(listed("AAA", "10"), listed("BBB", qty, side))
    with pytest.raises(window.WindowRefused, match="no starting long holding"):
        module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert not built


def test_a_position_unreadable_at_the_end_is_named_with_its_count_last_seen_at_the_start(
    controls, tmp_path, monkeypatch, capsys
):
    """The end read goes through the same per-symbol reader: a position readable at the start and not
    at the end is named with an ALERT and its count as last seen, the other holdings as read now, and
    the run is not failed for it."""
    module, settings, strategy, _foreign, _reads, _built = real_reader(monkeypatch, controls)
    trading = Listing(listed("AAA", "10"), listed("BBB", "2"))

    def stop(_node, _ends):
        strategy.on_stop()
        trading.listed = [listed("AAA", "10"), listed("BBB", "NaN")]

    monkeypatch.setattr(window, "_run_node", stop)
    before = list(strategy.sent)
    module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    said = capsys.readouterr().out
    assert "AAA: 10 held, left unprotected by this node" in said, said
    (alert,) = [line for line in said.splitlines() if line.startswith("ALERT BBB:")]
    assert "2 held, last seen at start" in alert and "left unprotected" in alert, alert
    assert strategy.sent == before


def test_a_position_unreadable_at_the_start_is_named_at_the_end_when_the_end_read_fails(
    controls, tmp_path, monkeypatch, capsys
):
    """A symbol skipped as unreadable at the start is named in the end report when the end read itself
    fails: what the start could not read is not forgotten by the stop."""
    module, settings, strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    trading = Listing(listed("AAA", "10"), listed("BBB", "NaN"))

    def failing() -> list:
        raise ConnectionError("positions offline")

    def stop(_node, _ends):
        strategy.on_stop()
        trading.get_all_positions = failing

    monkeypatch.setattr(window, "_run_node", stop)
    with pytest.raises(RuntimeError, match="end positions read failed"):
        module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert set(built[0][1]) == {AAA}
    said = capsys.readouterr().out
    alerts = [line for line in said.splitlines() if line.startswith("ALERT BBB:")]
    assert len(alerts) == 2, said
    assert "left unprotected by this node" in alerts[1], alerts[1]


@pytest.mark.parametrize(("qty", "side"), [("0", "long"), ("0", "short"), ("-0", "short")])
def test_a_holding_with_a_zero_quantity_is_skipped_by_name_and_every_other_long_is_covered(
    controls, tmp_path, monkeypatch, capsys, qty, side
):
    """A position listed with a zero quantity is no holding the node can protect: it is named with an
    ALERT and left out, a level for it with it, and every other long is covered, never a refusal of the
    whole node (the lead, 2026-11-13 21:27)."""
    module, _settings, _strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    settings = config(controls, levels={AAA: Price.from_str("9.50"), BBB: Price.from_str("1.50")})
    trading = Listing(listed("AAA", "10"), listed("BBB", qty, side))
    module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert len(built) == 1
    assert set(built[0][1]) == {AAA}
    assert set(built[0][0].levels) == {AAA}
    start = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("ALERT BBB:")
    )
    assert "skipped, nothing sent" in start, start
