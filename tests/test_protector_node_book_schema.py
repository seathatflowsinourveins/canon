"""The node reads the level book the protector writes (aproof-build's P1, the deputy 20:08).

OwnerProtector._save_levels writes through start_level.LevelBook.write, schema 2. The next
segment's node start reads that same file with protector_node._book_levels before it builds
anything. Before this, the node accepted schema 1 alone, so every restart after a written book
was refused ("level book unreadable ... nothing started").
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price

from canon_trading import start_level
from canon_trading.protector_node import _book_levels
from canon_trading.start_level import Entry
from canon_trading.start_level import LevelBook

AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")


def test_an_empty_book_the_protector_wrote_is_read_by_the_next_start(tmp_path):
    path = tmp_path / "levels.json"
    LevelBook.write(path, {})
    assert json.loads(path.read_text())["schema"] == start_level.SCHEMA
    assert _book_levels(path, "tests") == {}


def test_a_written_level_reaches_the_next_start_and_a_waiting_holding_carries_none(
    tmp_path, capsys
):
    path = tmp_path / "levels.json"
    LevelBook.write(
        path,
        {
            ("tests", AAA): Entry(Decimal("9.75"), 1, Decimal(10), 5, "r1", "armed"),
            ("tests", BBB): Entry(
                None, 2, Decimal(3), 6, "r1", "waiting", reason="no two-sided quote"
            ),
            ("other", AAA): Entry(Decimal(8), 1, Decimal(10), 7, "r1", "armed"),
        },
        last_step_ns={"tests": 6},
    )
    assert _book_levels(path, "tests") == {AAA: Price.from_str("9.75")}
    # A holding written with no level is not a malformed row: nothing is skipped by name.
    assert "skipped" not in capsys.readouterr().out


def test_the_protectors_own_saved_book_passes_the_next_segments_real_start(tmp_path, monkeypatch):
    """The lead's term (2026-11-13 20:08): the writer's real output through the node's real start.

    Segment 1 is an OwnerProtector holding an armed level, whose stop saves the book through
    `_save_levels`. Segment 2 is `run_protector` with the runner's book variable set, which reads
    the book at its start before it builds the node. The tape cases never reached this read.
    """
    from tests.test_owner_protector import Harness
    from tests.test_protector_node import booked
    from tests.test_protector_node import config

    book = tmp_path / "owner-levels.json"
    first = Harness(row="OWN1", order_id_tag="001")
    first.settings.account_label = "windows"
    first.settings.level_book_path = book
    first.settings.run = "segment-1"
    # on_start sets these two from the settings; this segment is saved without a start.
    first.level_book_path = book
    first.run = first.settings.run
    first.adopt_holding(AAA, Price.from_str("9.50"))
    first._save_levels(final=True)
    assert json.loads(book.read_text())["schema"] == start_level.SCHEMA

    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    controls = (holds, inbox)
    module, settings, _strategy, _foreign, _reads, built = booked(monkeypatch, controls)
    monkeypatch.setenv("CANON_PROTECTOR_LEVEL_BOOK", str(book))
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    assert built, "the second segment's start was refused before the node was built"
    assert built[0][3] == {"account_label": "windows", "level_book_path": str(book)}
    assert config(controls).account == "windows"


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Synthetic only: never inspect a credential environment or a paper lock."""
