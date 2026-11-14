"""The same-holding verdict on one clock (DeepSeek P2-2; the lead's ruling, 2026-11-13 21:23).

Whether a book entry's holding is the one held now is judged on the venue's clock alone: the
entry keeps the venue's reading of its fills (`start_level.Horizon`), and a fill the node had not
applied when it saved is counted at the next start whatever the host's clock said. The lead's
properties of 21:32: (1) the reading at discovery is a venue stamp, and a restart with no fill
between keeps the carried level; (2) a book the composed commit wrote is read, never refused,
and its entries answer unknown once, by name, until the first save writes the new form.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model import Price

from canon_trading import start_level
from canon_trading.protector_node import _book_levels
from canon_trading.start_level import Applied
from canon_trading.start_level import Entry
from canon_trading.start_level import Horizon
from canon_trading.start_level import leg_key
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import VenueOrders
from tests.test_owner_protector import iso
from tests.test_owner_protector import quote
from tests.test_owner_protector import restart_harness
from tests.test_owner_protector import venue_fill

MS = SECOND // 1000
READ = T0 + 179 * SECOND
"""The venue's clock when the first run read the holding (one second behind the host's)."""

COMPOSED_BOOK = {
    # Written by the composed commit's own LevelBook.write (41b411d9), unchanged.
    "schema": 2,
    "entries": [
        {
            "account_label": "shape",
            "instrument_id": "AAA.ALPACA",
            "protection_start_ns": 1762790400000000000,
            "last_level": "1.10",
            "quantity": "10",
            "written_ns": 1762790401000000000,
            "run": "shape-prior",
            "state": "armed",
            "distance": "0.04",
            "limit_offset": "0.05",
        }
    ],
    "last_step_ns": {"shape": 1762790401000000000},
}


def first_run(path: Path, venue: VenueOrders | None = None):
    """A run that finds the owner's long with no previous entry, self-arms at 1.01 (the bid
    1.05 less OWN1's 0.04) and saves at its stop, at the host's T0 + 180 s."""
    strategy = restart_harness(path, trading=venue or VenueOrders(clock=iso(READ)))
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01"), strategy.said
    strategy.on_stop()
    return strategy


def next_run(path: Path, venue: VenueOrders):
    """The next start, with the bid fallen to 1.00: carried, it arms at 1.01; cold, at 0.96."""
    strategy = restart_harness(path, trading=venue)
    strategy.cached.latest = quote("1.00", T0 + 180 * SECOND)
    strategy.on_start()
    return strategy


def test_p22_a_round_trip_stamped_before_the_hosts_write_is_a_new_holding(tmp_path: Path) -> None:
    """DeepSeek P2-2, reproduced on 41b411d9: the owner sold all 10 and bought 10 again after
    the venue's reading, and the venue stamps both before the host's write. The node had not
    applied either when it saved, so the next start must not carry the old holding's level."""
    path = tmp_path / "levels.json"
    first_run(path)
    venue = VenueOrders(
        [
            venue_fill("sell", "10", submitted=T0 + 179_800 * MS, filled=T0 + 179_900 * MS),
            venue_fill("buy", "10", submitted=T0 + 179_920 * MS, filled=T0 + 179_950 * MS),
        ]
    )
    strategy = next_run(path, venue)
    assert strategy.slots[AAA].level == Price.from_str("0.96"), strategy.said
    assert any(
        "previous level not carried: a new holding: sold out since the venue's "
        f"{iso(READ)} (10 of 10 sold)" in line
        for line in strategy.said
    ), strategy.said


def test_the_first_reading_is_the_venues_clock_and_its_source_is_named(tmp_path: Path) -> None:
    """The lead's property 1: the reading at discovery is a venue stamp, Alpaca's clock, never
    the host's (here one second ahead)."""
    path = tmp_path / "levels.json"
    venue = VenueOrders(clock=iso(READ))
    strategy = first_run(path, venue)
    assert [route for route, _ in venue.reads][:1] == ["/clock"], venue.reads
    (row,) = json.loads(path.read_text())["entries"]
    assert row["horizon_ns"] == READ != row["written_ns"], row
    assert any(
        line == f"AAA.ALPACA: venue reading of its fills at the venue's {iso(READ)}, "
        "0 recent orders kept by identity"
        for line in strategy.said
    ), strategy.said


def test_a_restart_with_no_fill_between_keeps_the_carried_level_twice(tmp_path: Path) -> None:
    """The lead's property 1, the case of every holding on account C: a long the node knows only
    through the position report, restarted with no fill between, is the same holding, and the
    same-holding reading is carried into the next entry, so a third start carries too."""
    path = tmp_path / "levels.json"
    first_run(path)
    second = next_run(path, VenueOrders(clock=iso(T0 + 300 * SECOND)))
    assert second.slots[AAA].level == Price.from_str("1.01"), second.said
    second.on_stop()
    (row,) = json.loads(path.read_text())["entries"]
    assert row["horizon_ns"] == READ and row["run"] == "shape-next", row
    third = next_run(path, VenueOrders())
    assert third.slots[AAA].level == Price.from_str("1.01"), third.said


def test_a_book_the_composed_commit_wrote_is_read_once_unknown_then_carried(
    tmp_path: Path,
) -> None:
    """The lead's property 2: the schema-2 book #443 writes is read by both readers, never
    refused; its entry answers unknown once, by name; the first save writes schema 3 with the
    venue's reading, and the start after it carries."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps(COMPOSED_BOOK, indent=2) + "\n")
    assert _book_levels(path, "shape") == {AAA: Price.from_str("1.10")}
    first = restart_harness(path, trading=VenueOrders(clock=iso(READ)))
    first.on_start()
    assert first.slots[AAA].level == Price.from_str("1.01"), first.said
    assert any(
        line.startswith(
            "AAA.ALPACA: no carried level (previous level not carried: the entry "
            "holds no venue reading of its fills"
        )
        for line in first.said
    ), first.said
    first.on_stop()
    book = json.loads(path.read_text())
    assert book["schema"] == start_level.SCHEMA == 3
    (row,) = book["entries"]
    assert (row["last_level"], row["horizon_ns"]) == ("1.01", READ), row
    assert _book_levels(path, "shape") == {AAA: Price.from_str("1.01")}
    second = next_run(path, VenueOrders())
    assert second.slots[AAA].level == Price.from_str("1.01"), second.said


def test_a_venue_quantity_that_is_not_the_nodes_gives_no_reading_and_a_cold_restart(
    tmp_path: Path,
) -> None:
    """A reading holds only when the venue's quantity after the walk is the node's: any other
    is named, and the entry it saves is never the same holding."""
    path = tmp_path / "levels.json"
    first = first_run(path, VenueOrders(clock=iso(READ), held="7"))
    assert any(
        "AAA.ALPACA: no venue reading of its fills yet (FillsUnknown: the venue holds 7 and the "
        "node 10)" in line
        for line in first.said
    ), first.said
    (row,) = json.loads(path.read_text())["entries"]
    assert "horizon_ns" not in row, row
    second = next_run(path, VenueOrders())
    assert second.slots[AAA].level == Price.from_str("0.96"), second.said


def entry(quantity: str, *applied: Applied) -> Entry:
    return Entry(
        Decimal("1.10"),
        T0,
        Decimal(quantity),
        T0 + 180 * SECOND,
        "shape-prior",
        horizon=Horizon(READ, applied),
    )


def order(oid: str, side: str, filled: str, at: int, **fields: object) -> dict[str, object]:
    return {
        "id": oid,
        "symbol": "AAA",
        "side": side,
        "filled_qty": filled,
        "status": "filled",
        "submitted_at": iso(at - SECOND),
        "filled_at": iso(at),
        **fields,
    }


def test_a_second_execution_at_the_same_microsecond_on_an_applied_order_is_counted() -> None:
    """The node applied 4 of a sale; the venue shows 10 at the same stamp as the reading, and the
    owner bought 4 back: the 6 unapplied shares sell out the entry's 6, a new holding."""
    sale = Applied("sell", (leg_key("v-sale"),), Decimal(4))
    venue = VenueOrders(
        [order("v-sale", "sell", "10", READ), order("v-buy", "buy", "4", READ + SECOND)]
    )
    kind, reason = start_level.holding_verdict(venue, "AAA", entry("6", sale), Decimal(4))
    assert kind == start_level.NEW, reason


def test_an_applied_order_the_venue_does_not_list_adds_nothing() -> None:
    """The pin's position-reconciliation order (a venue order ID the venue never made) and an
    order older than the walk are no venue order: a holding known only through the position
    report is the same holding when nothing filled since its reading."""
    synthetic = Applied("buy", (leg_key("5c1f0a2e-0000-5000-8000-000000000000"),), Decimal(10))
    reading = start_level.holding_reading(VenueOrders(), "AAA", entry("10", synthetic), Decimal(10))
    assert reading.kind == start_level.SAME, reading.reason
    assert reading.horizon == Horizon(READ, ()), reading.horizon


def test_fewer_shares_at_the_venue_than_the_node_applied_is_unknown() -> None:
    applied = Applied("buy", (leg_key("v-buy"),), Decimal(10))
    venue = VenueOrders([order("v-buy", "buy", "6", READ)])
    kind, reason = start_level.holding_verdict(venue, "AAA", entry("10", applied), Decimal(10))
    assert kind == start_level.UNKNOWN and reason is not None
    assert "the venue shows 6 filled on an order of which 10 were applied" in reason


def test_an_order_the_venue_last_stamped_before_the_reading_is_held_and_a_later_one_is_not() -> (
    None
):
    """An order not kept by identity counts only when the venue stamped it after the reading:
    its fill time, or a later update (a fill that moved an old order on)."""
    old = order("v-old", "buy", "10", READ - SECOND)
    assert start_level.holding_verdict(VenueOrders([old]), "AAA", entry("10"), Decimal(10)) == (
        start_level.SAME,
        None,
    )
    moved = dict(old, updated_at=iso(READ + SECOND))
    kind, reason = start_level.holding_verdict(
        VenueOrders([moved]), "AAA", entry("10"), Decimal(10)
    )
    assert kind == start_level.UNKNOWN and reason is not None, reason
    assert "10 bought since the venue's" in reason


def test_an_entry_with_no_reading_is_never_the_same_holding() -> None:
    bare = Entry(Decimal("1.10"), T0, Decimal(10), T0 + SECOND, "shape-prior")
    kind, reason = start_level.holding_verdict(VenueOrders(), "AAA", bare, Decimal(10))
    assert kind == start_level.UNKNOWN and reason is not None
    assert reason.startswith("the entry holds no venue reading of its fills"), reason
