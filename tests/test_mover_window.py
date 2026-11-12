"""The mover window's session rows and native node build, without connecting (ADR 0091)."""

from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from canon_trading import session_window
from canon_trading.mover import Mover
from canon_trading.session_window import MoverPick
from canon_trading.session_window import WindowConfig


@pytest.mark.parametrize(
    ("part", "feed"),
    [("after-hours", "sip"), ("overnight", "boats"), ("pre-market", "sip")],
)
def test_each_extended_part_builds_mover_with_one_configured_slot(tmp_path, part, feed) -> None:
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        Decimal("1.5"),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        Decimal("1.5"),
    )
    node, strategy, cap = session_window.window_node(
        WindowConfig(
            symbol="LLL", session="extended", strategy="Mover", mover=True, log_dir=tmp_path
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="m",
        feed=feed,
        part=part,
        pick=pick,
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover)
        rows = strategy.settings
        assert rows.slots == rows.watch == rows.entries == 1
        assert rows.extended_hours is True and rows.manage_stop is False
        assert rows.feed == feed and rows.max_notional == Decimal(500)
        assert cap.max_position == Decimal(333)
    finally:
        node.dispose()


def test_an_extended_parts_slot_count_is_a_setting(monkeypatch, tmp_path) -> None:
    """The shipped row is one slot; changing the row reaches MoverConfig instead of a literal cap."""
    row = session_window.MOVER_PARTS["after-hours"]
    monkeypatch.setitem(session_window.MOVER_PARTS, "after-hours", replace(row, slots=2))
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        Decimal("1.5"),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        Decimal("1.5"),
    )
    node, strategy, _ = session_window.window_node(
        WindowConfig(
            symbol="LLL", session="extended", strategy="Mover", mover=True, log_dir=tmp_path
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="m",
        feed="sip",
        part="after-hours",
        pick=pick,
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover)
        assert strategy.settings.slots == strategy.settings.watch == 2
    finally:
        node.dispose()
