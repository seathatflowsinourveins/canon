"""The book's write keeps what it cannot read (#443's max review F13; the lead, 2026-11-13 22:47).

The protector's locked re-read before each write discarded every reader ALERT and wrote back this
run's start-time copy of any row it rejected: another account's 6.00, rewritten in a shape this
build rejects, came back as the 5.00 read at the start, and a book overwritten with bad JSON was
recreated, all with nothing logged. The properties: a row this build cannot read is carried through
a write unchanged; a level is never lowered across a write; a reader's ALERT survives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from canon_trading import start_level
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import VenueOrders
from tests.test_owner_protector import iso
from tests.test_owner_protector import restart_harness

READ = T0 + 179 * SECOND


def other(level: str, **fields: Any) -> dict[str, Any]:
    """Another account's armed row, as this build writes one."""
    return {
        "account_label": "other",
        "instrument_id": "BBB.ALPACA",
        "protection_start_ns": T0,
        "last_level": level,
        "quantity": "5",
        "written_ns": T0 + SECOND,
        "run": "other-run",
        "state": "armed",
        "distance": "0.04",
        "limit_offset": "0.05",
        "horizon_ns": T0 + SECOND,
        "applied": [],
        **fields,
    }


def book(*rows: dict[str, Any]) -> str:
    return json.dumps({"schema": start_level.SCHEMA, "entries": list(rows)}, indent=2) + "\n"


def rows(path: Path) -> dict[str, dict[str, Any]]:
    return {row["account_label"]: row for row in json.loads(path.read_text())["entries"]}


def started(path: Path):
    strategy = restart_harness(path, trading=VenueOrders(clock=iso(READ)))
    strategy.on_start()
    return strategy


def test_a_row_this_build_cannot_read_is_carried_through_a_write_unchanged(tmp_path: Path) -> None:
    """F13's case: read at 5.00 at the start; another writer then rewrote the row at 6.00 in a shape
    this build rejects. This run's write keeps that row as it is, never its own older 5.00."""
    path = tmp_path / "levels.json"
    path.write_text(book(other("5.00")))
    strategy = started(path)
    newer = other("6.00", state="parked")  # a state this build does not know
    path.write_text(book(newer))
    strategy.on_stop()
    assert rows(path)["other"] == newer, path.read_text()
    assert rows(path)["shape"]["last_level"] == "1.01"


def test_the_rereads_alert_on_a_rejected_row_is_said(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    path.write_text(book(other("5.00")))
    strategy = started(path)
    path.write_text(book(other("6.00", state="parked")))
    strategy.on_stop()
    alerts = [
        line
        for line in strategy.said
        if line.startswith("BBB.ALPACA: ALERT: previous level unavailable")
    ]
    assert len(alerts) == 1, strategy.said
    assert "'parked'" in alerts[0], alerts


def test_a_book_overwritten_with_what_no_build_reads_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    path.write_text(book(other("5.00")))
    strategy = started(path)
    path.write_text("{not json")
    strategy.on_stop()
    assert path.read_text() == "{not json"
    alerts = [line for line in strategy.said if "ALERT:" in line and "not overwritten" in line]
    assert len(alerts) == 1, strategy.said


def test_another_runs_higher_level_for_the_same_holding_is_never_lowered(tmp_path: Path) -> None:
    """A successor of this account wrote a higher level for the same holding (the same protection
    start) while this run still held its own: this run's write keeps the higher level."""
    path = tmp_path / "levels.json"
    strategy = started(path)
    strategy._save_levels(final=True)
    (mine,) = json.loads(path.read_text())["entries"]
    successor = dict(
        mine, last_level="1.03", run="shape-successor", written_ns=mine["written_ns"] + 1
    )
    path.write_text(book(successor))
    strategy.on_stop()
    assert rows(path)["shape"]["last_level"] == "1.03", path.read_text()
