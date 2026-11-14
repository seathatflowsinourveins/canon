"""L6's frozen gain-lock brief: plain trail by default, explicit opt-in preserves the lock.

Contract: the lead's ruling of 2026-11-13, and the lead's ruling of 2026-11-14 01:31 that a
setting never lowers a level in the book (the last case). The native replay uses NautilusTrader
d53849b216e2's trailing.rs:61-102,129-143 and emulator.rs:814-928. Its prices and
holdings are synthetic, with the existing offline owner/protector fixtures.
All state is isolated under pytest's temporary tree; no broker or credential read.
"""

from __future__ import annotations

import asyncio
import gc
import tomllib
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue

from canon_trading import paper_node
from canon_trading import protector_node
from canon_trading.owner_protector import OwnerProtector
from canon_trading.owner_protector import OwnerProtectorConfig
from canon_trading.start_level import LevelBook
from canon_trading.start_level import book_path
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import USD
from tests.test_owner_protector import Owner
from tests.test_owner_protector import SimulatedProtector
from tests.test_owner_protector import SyntheticData
from tests.test_owner_protector import SyntheticExecution
from tests.test_owner_protector import VenueOrders
from tests.test_owner_protector import equity
from tests.test_owner_protector import minute_bars
from tests.test_owner_protector import quote
from tests.test_protector_node import config as node_config
from tests.test_protector_node import runtime
from tests.test_protector_node import subject as node_subject

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared credential-environment reader for these offline cases."""


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)
    monkeypatch.delenv(protector_node.LEVEL_BOOK_ENV, raising=False)


@pytest.fixture
def controls(tmp_path: Path) -> tuple[Path, Path]:
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    return holds, inbox


@contextmanager
def two_spread_replay(*, gain_lock: bool = False):
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("TESTER-001"), bypass_logging=True)
    )
    try:
        engine.add_venue(
            Venue("ALPACA"),
            OmsType.NETTING,
            AccountType.CASH,
            [Money(100_000, USD)],
            fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
            bar_execution=False,
        )
        engine.add_instrument(equity())
        strategy = SimulatedProtector("9.80")
        if gain_lock:
            # SimulatedProtector builds its own config, so the option is set on it before the
            # engine starts; this case also ran on the always-on base. The parser and builder
            # cases prove the real flag reaches the constructed strategy.
            strategy.settings.gain_lock = True
        engine.add_strategy(Owner())
        engine.add_strategy(strategy)
        engine.add_data(sorted(minute_bars("0.08"), key=lambda bar: bar.ts_init))
        # The owner buys at the synthetic ask of 10.00; the trail is armed before the gain.
        engine.add_data(
            [quote("9.98", T0), quote("10.00", T0 + SECOND), quote("10.00", T0 + 2 * SECOND)]
        )
        engine.run(streaming=True)
        slot = strategy.slots[AAA]
        assert slot.protect_id is not None
        order = engine.cache.order(slot.protect_id)
        assert order is not None and order.is_emulated
        assert order.trailing_offset > Decimal("0.04")
        assert 0 < order.limit_offset - order.trailing_offset < Decimal("0.04")
        assert strategy.gain_lock_events == []
        engine.clear_data()
        engine.add_data([quote("10.04", T0 + 3 * SECOND)])
        engine.run(streaming=True)
        yield engine, strategy
    finally:
        engine.end()
        engine.dispose()


def test_default_at_two_spreads_keeps_the_native_trail_without_a_gain_trigger() -> None:
    with two_spread_replay() as (engine, strategy):
        slot = strategy.slots[AAA]
        order = engine.cache.order(slot.protect_id)
        assert strategy.gain_lock_events == [], "default settings armed the gain lock"
        assert slot.gain_armed_at is None
        assert slot.gain_floor is None and slot.gain_applied_floor is None
        assert order is not None and order.is_emulated
        trail = Decimal("10.04") - order.trailing_offset
        assert order.trigger_price.as_decimal() == trail
        assert order.price.as_decimal() == Decimal("10.04") - order.limit_offset
        assert not any(type(event).__name__ == "OrderReleased" for event in order.events())
        # A return to entry remains above the ordinary trail; it must not sell near entry.
        engine.clear_data()
        engine.add_data([quote("10.00", T0 + 4 * SECOND)])
        engine.run(streaming=True)
        order = engine.cache.order(slot.protect_id)
        assert order is not None and order.is_emulated
        assert order.trigger_price.as_decimal() == trail
        assert strategy.gain_lock_events == [] and strategy.gain_lock_sales == []


def test_explicit_gain_lock_at_two_spreads_preserves_the_existing_cost_floor() -> None:
    with two_spread_replay(gain_lock=True) as (engine, strategy):
        slot = strategy.slots[AAA]
        order = engine.cache.order(slot.protect_id)
        (armed,) = strategy.gain_lock_events
        assert armed["event"] == "armed"
        assert Decimal(armed["entry"]) == Decimal("10.00")
        assert Decimal(armed["round_trip_cost"]) == 0
        assert Decimal(armed["noise_floor"]) == Decimal("0.04")
        assert slot.gain_floor == slot.gain_applied_floor == Price.from_str("10.00")
        assert order is not None and order.is_emulated
        assert order.trigger_price.as_decimal() == (
            Decimal("10.00") + order.limit_offset - order.trailing_offset
        )
        assert order.price == Price.from_str("10.00")


def test_strategy_configuration_defaults_gain_lock_off() -> None:
    assert OwnerProtectorConfig(row="OWN1").gain_lock is False
    assert OwnerProtectorConfig(row="OWN1", gain_lock=True).gain_lock is True


@pytest.mark.parametrize("value", ["false", "off", 1, None], ids=repr)
def test_a_gain_lock_that_is_not_true_or_false_is_refused(controls, monkeypatch, value) -> None:
    """Review of this unit: a truthy non-bool (text from a file, say) would turn the lock on, so
    the strategy's config and the node's run settings both refuse anything but True or False."""
    with pytest.raises(ValueError, match="gain_lock must be True or False"):
        OwnerProtectorConfig(row="OWN1", gain_lock=value)
    _module, settings, _strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    with pytest.raises(ValueError, match="gain_lock must be True or False"):
        replace(settings, gain_lock=value)


@pytest.mark.parametrize("enabled", [False, True], ids=["default-off", "flag-on"])
def test_cli_setting_reaches_the_constructed_strategy(controls, monkeypatch, enabled) -> None:
    argv = [
        "windows",
        "--row",
        "OWN1",
        "--holds-dir",
        str(controls[0]),
        "--inbox",
        str(controls[1]),
    ]
    if enabled:
        argv.append("--gain-lock")
    settings = protector_node.parse_args(argv)
    added = []
    node = SimpleNamespace(add_strategy=added.append, dispose=lambda: None)
    monkeypatch.setattr(
        paper_node,
        "alpaca_paper_builder",
        lambda *args, **kwargs: SimpleNamespace(build=lambda: node),
    )
    built, strategy = protector_node.protector_node(settings, (AAA,), feed="sip")
    assert built is node and added == [strategy]
    assert settings.gain_lock is enabled
    assert strategy.settings.gain_lock is enabled


def test_task_help_and_argument_array_expose_only_an_explicit_gain_lock_opt_in() -> None:
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:protector"]
    lines = [line for line in task["usage"].splitlines() if 'flag "--gain-lock"' in line]
    assert len(lines) == 1, "the task help has no named gain-lock flag"
    assert "default off" in lines[0].lower()
    assert "default=#true" not in lines[0]
    assert '[ "${usage_gain_lock:-false}" = true ] && args+=(--gain-lock)' in task["run"]


def test_python_help_names_gain_lock_and_its_default(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        protector_node.parse_args(["--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "--gain-lock" in help_text
    assert "default off" in help_text.lower()


@pytest.mark.parametrize("enabled", [False, True], ids=["default-off", "flag-on"])
def test_start_line_names_gain_lock_and_its_value(
    controls, tmp_path, monkeypatch, capsys, enabled
) -> None:
    module, settings, _strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)
    if enabled:
        settings = replace(settings, gain_lock=True)
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    # The start line is the one the runner parses, ending "(epoch N)" (scripts/automation);
    # the node prints other "paper:protector:" lines before it, such as the symbol-holds note.
    start = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("paper:protector:") and "(epoch " in line
    )
    assert f"gain-lock={'on' if enabled else 'off'}" in start


def test_handbook_names_the_default_and_pending_replay_proof() -> None:
    rows = (ROOT / "docs/handbook/rows.md").read_text()
    assert "--gain-lock" in rows
    assert "not a default until its replay proof" in rows


@pytest.mark.usefixtures("new_york_regular_hours")
def test_a_lock_off_segment_starts_at_a_book_level_above_the_plain_trail(
    controls, tmp_path, monkeypatch
) -> None:
    """The lead's ruling of 2026-11-14 01:31: a setting never lowers a level in the book.

    A segment with the lock on writes the book; its level is then raised above what the plain
    trail computes, as a lock-raised trigger would be, and a segment with the lock off starts at
    that level, raises nothing of the lock's own, and writes the level back unlowered. Real
    LiveNodes with the synthetic client factories of
    tests/test_protector_node.py::test_two_builder_segments_carry_the_level_distance_and_offset.
    """
    module = node_subject()  # isolated_state already points the book under tmp_path
    notes: list[str] = []
    monkeypatch.setattr(OwnerProtector, "_say", lambda self, text: notes.append(text))
    venue = VenueOrders()
    runs: list[str] = []
    starts: list[int] = []  # where each segment's notes begin
    snapshots: list[tuple[Decimal, Decimal, Decimal]] = []
    sells: list[tuple[Decimal | None, Price | None, Price | None]] = []
    gain_events: list[list[dict]] = []
    path = book_path("windows")
    raised = Decimal("10.25")

    class Data(SyntheticData):
        async def publish_tape(self):
            # The same bid until the segment stops: arming takes the first, and the sell is
            # placed only on an observation newer than the guarded one.
            for _ in range(100):
                self._handle_data(quote("10.30", self.clock.timestamp_ns()))
                published.set()
                await asyncio.sleep(0.1)

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
        while slot.protect_id is None or strategy.cache.order(slot.protect_id) is None:
            await asyncio.sleep(0.05)
        order = strategy.cache.order(slot.protect_id)
        sells.append((order.trigger_price.as_decimal(), slot.gain_floor, slot.gain_applied_floor))
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
    for gain_lock in (True, False):
        gc.collect()
        published = asyncio.Event()
        node, strategy = module.protector_node(
            node_config(controls, gain_lock=gain_lock),
            (AAA,),
            feed="sip",
            book={"account_label": "windows"},
            trading_reader=lambda: venue,
        )
        runs.append(strategy.settings.run)
        starts.append(len(notes))
        try:
            asyncio.run(run_segment(node, strategy, published))
        finally:
            node.dispose()
            del node
        if gain_lock:
            saved = LevelBook.read(path)[("windows", AAA)]
            # The plain trail on this tape: bid 10.30 less its distance, below the raised level.
            assert saved.last_level == snapshots[0][0] < raised
            with LevelBook.locked(path):
                LevelBook.write(
                    path,
                    {("windows", AAA): replace(saved, last_level=raised)},
                    last_step_ns=LevelBook.last_steps(path),
                )
    (on, off) = snapshots
    assert off == (raised, on[1], on[2]), "the lock-off segment must start at the book's level"
    assert gain_events[1] == [], "the lock-off segment armed the gain lock"
    assert sells[1] == (raised, None, None), "the lock-off sell is not at the book's level"
    after = LevelBook.read(path)[("windows", AAA)]
    assert after.last_level == raised and after.run == runs[1], "a setting lowered the level"
    second = notes[starts[1] :]
    assert any(f"carried level {raised}" in note and runs[0] in note for note in second)
    assert not any("no carried level" in note or "not carried" in note for note in second)
