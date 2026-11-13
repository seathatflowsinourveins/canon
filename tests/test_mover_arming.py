"""The Mover's entry switch: the arming file, read at each buy decision; sells never read it.

The lead's decisions of 2026-11-12, with the format and the reader from
`canon_trading.arming`, which the protector shares. A paper window names no file, so it behaves as
before; an absent file is the deployment's (`arming_absent_is_on`).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderSide

from canon_trading import arming
from canon_trading import mover_window
from canon_trading.mover import MoverConfig
from tests.test_mover import AAA
from tests.test_mover import FALL
from tests.test_mover import MOVER_ID
from tests.test_mover import RECLAIM
from tests.test_mover import RISING
from tests.test_mover import Recording
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover import tape


def fire(s):
    eng = engine()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    eng.dispose()
    return orders


def buys(orders):
    return [o for o in orders if o.side == OrderSide.BUY]


def sells(orders):
    return [o for o in orders if o.side == OrderSide.SELL]


def test_with_the_file_off_a_qualifying_entry_is_not_submitted_and_is_recorded(tmp_path):
    path = tmp_path / "arming"
    path.write_text("off\nheld for the review\n")
    s = strategy(arming_path=str(path))
    orders = fire(s)
    assert buys(orders) == []
    record = s.record()
    assert record["entries"] == []
    stopped = record["entries_off"]
    assert stopped and all(row["instrument_id"] == str(AAA) for row in stopped)
    assert all(row["read"] == arming.OFF and row["kind"] == "entry" for row in stopped)
    assert all(isinstance(row["when_ns"], int) for row in stopped)
    assert record["arming"] == {"path": str(path), "absent_is_on": True}


class OffAfterTheBuyFills(Recording):
    """Turns the switch off once the entry has filled, so what follows is the holding's own exit."""

    path: Path

    def on_order_filled(self, event) -> None:
        super().on_order_filled(event)
        if event.order_side == OrderSide.BUY:
            self.path.write_text("off\n")


def test_with_the_file_off_a_due_protective_sell_still_goes_out(tmp_path):
    path = tmp_path / "arming"
    path.write_text("on\n")
    control = fire(strategy())
    OffAfterTheBuyFills.path = path
    s = OffAfterTheBuyFills(
        MoverConfig(
            max_notional=strategy().settings.max_notional,
            max_move=strategy().settings.max_move,
            entry_arm="bridge",
            trail_mode="fixed",
            cooldown_secs=300,
            strategy_id=MOVER_ID,
            order_id_tag="001",
            arming_path=str(path),
        )
    )
    orders = fire(s)
    assert len(buys(orders)) == 1 and sells(control)
    assert [(o.order_type, o.status, o.quantity) for o in sells(orders)] == [
        (o.order_type, o.status, o.quantity) for o in sells(control)
    ]
    assert s.record()["entries"][0]["arming"] == arming.ON


def test_an_absent_file_is_the_deployments_and_an_unusable_one_is_off_by_name(tmp_path):
    absent = tmp_path / "absent"
    paper = strategy(arming_path=str(absent))
    assert len(buys(fire(paper))) == 1
    assert paper.record()["entries"][0]["arming"] == "on: the arming file is absent"
    live = strategy(arming_path=str(absent), arming_absent_is_on=False)
    assert buys(fire(live)) == []
    assert live.record()["entries_off"][0]["read"] == "off: the arming file is absent"
    empty = tmp_path / "empty"
    empty.write_text("")
    wrong = tmp_path / "wrong"
    wrong.write_text("yes\n")
    for path, value in ((empty, ""), (wrong, "yes")):
        for absent_is_on in (True, False):
            s = strategy(arming_path=str(path), arming_absent_is_on=absent_is_on)
            assert buys(fire(s)) == []
            assert s.record()["entries_off"][0]["read"] == (
                f"off: the arming file's first line is neither on nor off: {value!r}"
            )


class Flips(Recording):
    """Writes `then` to the file after the first read, so the next decision reads the new value."""

    path: Path
    then: str

    def _arming_read(self):
        read = super()._arming_read()
        self.path.write_text(f"{self.then}\n")
        return read


def flipping(path, first, then):
    path.write_text(f"{first}\n")
    Flips.path, Flips.then = path, then
    return Flips(
        MoverConfig(
            max_notional=strategy().settings.max_notional,
            max_move=strategy().settings.max_move,
            entry_arm="bridge",
            trail_mode="fixed",
            cooldown_secs=300,
            strategy_id=MOVER_ID,
            order_id_tag="001",
            arming_path=str(path),
        )
    )


def test_the_file_is_read_at_each_decision_not_once(tmp_path):
    s = flipping(tmp_path / "arming", "off", "on")
    orders = fire(s)
    record = s.record()
    assert len(record["entries_off"]) == 1 and len(buys(orders)) == 1
    assert record["entries"][0]["arming"] == arming.ON
    assert record["entries_off"][0]["when_ns"] < record["entries"][0]["submitted_ns"]


def test_with_no_file_set_the_run_is_as_before_and_says_so():
    before = fire(strategy())
    s = strategy()
    after = fire(s)
    assert [(o.side, o.order_type, o.status, o.quantity) for o in after] == [
        (o.side, o.order_type, o.status, o.quantity) for o in before
    ]
    record = s.record()
    assert record["entries"][0]["arming"] == "on: no arming file is set"
    assert record["entries_off"] == []
    assert record["arming"] == {"path": None, "absent_is_on": True}


def test_a_stopped_buy_leaves_the_share_cap_and_the_position_monitor_as_they_were(tmp_path):
    path = tmp_path / "arming"
    path.write_text("off\n")
    s = strategy(arming_path=str(path), size_from_budget=True, share_caps={AAA: 7})
    (monitor,) = mover_window.mover_position_caps({AAA: Decimal(10)}, Decimal(1000))
    original_cap = monitor.max_position
    s.position_caps = {AAA: monitor}
    assert buys(fire(s)) == [] and s.record()["entries_off"]
    assert s.settings.share_caps == {AAA: 7}
    assert monitor.max_position == original_cap and monitor.tripped is None
    assert not any("size budget" in line for line in s.said)


@pytest.mark.parametrize("path", ["", "canon/live-arming", "/tmp/arm\x00ing"])
def test_an_arming_path_must_be_absolute_and_whole(path):
    with pytest.raises(ValueError, match="arming_path"):
        strategy(arming_path=path)


def test_the_warning_is_given_again_when_what_the_switch_reads_changes(tmp_path):
    path = tmp_path / "arming"
    s = flipping(path, "off", "maybe")
    fire(s)
    reads = [read for _, read in s.arming_warnings]
    assert reads[:2] == [
        arming.OFF,
        "off: the arming file's first line is neither on nor off: 'maybe'",
    ]
    assert len(s.record()["entries_off"]) >= 3  # a third stop happened, with a read seen before
    assert len(reads) == 2  # the same reason again is not warned again


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing-folder", "the arming file's folder is missing"),
        ("dangling-link", "the arming file is a link to nothing"),
        ("directory", "the arming file could not be read (IsADirectoryError)"),
        ("invalid-utf8", "the arming file could not be read (UnicodeDecodeError)"),
    ],
)
def test_main_reader_keeps_an_unusable_path_off_by_name(tmp_path, case, reason):
    path = tmp_path / "arming"
    if case == "missing-folder":
        path = tmp_path / "missing" / "arming"
    elif case == "dangling-link":
        path.symlink_to(tmp_path / "missing")
    elif case == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff")
    s = strategy(arming_path=str(path))
    assert buys(fire(s)) == []
    assert s.record()["entries_off"][0]["read"] == f"off: {reason}"


class RefusesASellRead(OffAfterTheBuyFills):
    def _arming_read(self):
        assert self.path.read_text() == "on\n", "a sell read the entry switch"
        return super()._arming_read()


class CountsReads(OffAfterTheBuyFills):
    """Counts each read of the switch, so a read after the buy fills shows without an exception."""

    def __init__(self, config):
        super().__init__(config)
        self.reads_at_fill: int | None = None
        self.reads = 0

    def _arming_read(self):
        self.reads += 1
        return super()._arming_read()

    def on_order_filled(self, event) -> None:
        super().on_order_filled(event)
        if event.order_side == OrderSide.BUY:
            self.reads_at_fill = self.reads


def test_a_protective_sell_never_calls_the_arming_reader(tmp_path):
    path = tmp_path / "arming"
    path.write_text("on\n")
    RefusesASellRead.path = path
    s = RefusesASellRead(strategy(arming_path=str(path)).settings)
    orders = fire(s)
    assert len(buys(orders)) == 1 and sells(orders)
    assert path.read_text() == "off\n"
    CountsReads.path = tmp_path / "counted"
    CountsReads.path.write_text("on\n")
    counted = CountsReads(strategy(arming_path=str(CountsReads.path), entries=1).settings)
    orders = fire(counted)
    assert len(buys(orders)) == 1 and sells(orders)
    # with one entry allowed, no buy decision follows the fill: any later read is a sale's
    assert counted.reads_at_fill is not None and counted.reads == counted.reads_at_fill


def test_a_name_is_warned_again_after_the_switch_was_on_in_between(tmp_path):
    """An armed read anywhere ends the off period, even for a name that did not buy in it."""
    eng = engine()
    s = strategy(arming_path=str(tmp_path / "arming"))
    eng.add_strategy(s)  # registered, for the clock the record reads
    try:
        slot = cast(Any, SimpleNamespace(instrument_id=AAA, run_arm=None))
        off = arming.Arming(False, arming.OFF)
        s._switch_off(slot, off)
        s._switch_armed()  # another name's buy read on
        s._switch_off(slot, off)
        assert [read for _, read in s.arming_warnings] == [arming.OFF, arming.OFF]
    finally:
        eng.dispose()


def test_no_file_can_default_to_off_without_changing_the_paper_default():
    s = strategy(arming_absent_is_on=False)
    assert buys(fire(s)) == []
    assert s.record()["entries_off"][0]["read"] == "off: no arming file is set"
    assert strategy().settings.arming_absent_is_on is True


def test_arming_settings_are_recorded_outside_the_trial_rows():
    record = strategy().record()
    assert record["arming"] == {"path": None, "absent_is_on": True}
    assert "arming_path" not in record["rows"]
    assert "arming_absent_is_on" not in record["rows"]


class OffAfterTheSaleFills(Recording):
    path: Path

    def on_order_filled(self, event) -> None:
        super().on_order_filled(event)
        if event.order_side == OrderSide.SELL:
            self.path.write_text("off\n")


def test_a_qualifying_reentry_reads_the_file_again_and_is_recorded_as_a_reentry(tmp_path):
    path = tmp_path / "arming"
    path.write_text("on\n")
    OffAfterTheSaleFills.path = path
    control = strategy(cooldown_secs=60)
    s = OffAfterTheSaleFills(strategy(arming_path=str(path), cooldown_secs=60).settings)
    results = []
    for candidate in (control, s):
        eng = engine()
        orders = run(
            eng, candidate, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + RECLAIM)
        )
        eng.dispose()
        results.append(orders)
    assert len(buys(results[0])) == 2 and len(buys(results[1])) == 1
    stopped = s.record()["entries_off"]
    assert stopped and all(
        row["kind"] == "reentry" and row["read"] == arming.OFF for row in stopped
    )


def test_the_existing_mover_trial_key_excludes_the_default_arming_settings():
    from canon_trading.ledger import config_key
    from canon_trading.ledger import trial_config
    from canon_trading.tuning_contract import BARS_REPLAY_FIXED_ROWS
    from tests.test_tuning_study import p0_trial

    assert {
        name: BARS_REPLAY_FIXED_ROWS[name] for name in ("arming_path", "arming_absent_is_on")
    } == {
        "arming_path": None,
        "arming_absent_is_on": True,
    }
    config = trial_config([p0_trial()], starting_balances=["100000 USD"])
    assert config_key(config) == "213b37b6d3bba04c68c7c7d5b8bb91b73e74a8476c6f5a46179c18b0eec2cf88"
    assert all(
        name not in config["strategies"][0]["config"]
        for name in ("arming_path", "arming_absent_is_on")
    )


@pytest.mark.parametrize("name", ["arming_path", "arming_absent_is_on"])
def test_bars_replay_and_trials_refuse_nondefault_arming_by_name(tmp_path, name):
    from canon_trading.ledger import trial_config
    from canon_trading.tuning_study import BarsReplayMover
    from tests.test_tuning_study import p0_trial

    cfg = p0_trial()
    value = str(tmp_path / "arming") if name == "arming_path" else False
    settings = strategy(max_notional=cfg.settings.max_notional, **{name: value}).settings
    for invoke in (
        lambda: BarsReplayMover(settings),
        lambda: trial_config([replace(cfg, settings=settings)], starting_balances=["100000 USD"]),
    ):
        with pytest.raises(ValueError, match=name):
            invoke()
