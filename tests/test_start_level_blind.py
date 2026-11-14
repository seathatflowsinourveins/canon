"""Blind tests for U-e, "a restart never lowers a protection level".

Written from the handoff's Facts and Part 1 and from the public base in this tree; no builder
implementation of ``canon_trading.start_level`` was read. The module does not exist at this base, so
each case imports it inside the case: every case is red on its own ``ModuleNotFoundError``. The
protector cases pass the unit's new config fields through ``BOOK_CONFIG``, the single place where a
field name can be renamed.

Citations: ``src/canon_trading/rebuilt_level.py:47-66`` (the start is a floor; the candidate is
floored to the step); ``src/canon_trading/owner_protector.py:127-182`` (the base config and its
refusals), ``:398-438`` (adoption and the first-observation guard), ``:473-478`` (``on_start``),
``:562-680`` (planning and sending), ``:713-726`` (``stop_instrument`` and ``on_stop``);
``src/canon_trading/rank_shared.py:13-23`` (``write_atomically``); ``src/canon_trading/session_window.py:3927-3954``
(``overnight_eligibility``: the raw asset, or its ``attributes``);
``packages/canon-alpaca/src/canon_alpaca/providers.py:544-565`` (the asset ``attributes`` kept in
``Equity.info``); NautilusTrader d53849b216e2 ``crates/execution/src/trailing.rs:61-100`` (a sell's
trigger is seeded from the order and only moved by the better side's comparison).

Every fixture is synthetic: ``SHAPE.ALPACA``, scaled prices, a book under ``tmp_path``, and
``CLAUDE_CONFIG_DIR`` pointed at ``tmp_path``. No file outside ``tmp_path`` is written.
"""

from __future__ import annotations

import os
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import AccountId
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Symbol

# landing lane, 2026-11-13: the self-arm floor case (floor A); see FIXROUND-L1.md §2
from canon_trading.price_step import up_on_step
from canon_trading.rebuilt_level import rebuilt_level
from tests import test_owner_protector as base

SHAPE = InstrumentId.from_str("SHAPE.ALPACA")
T0 = base.T0
SECOND = base.SECOND
MINUTE = 60_000_000_000
START = T0 - 20 * MINUTE
ACCOUNT = "neutral-account"
"""The account label the book's key carries; a neutral string, never an Alpaca account number."""
CARRY = Price.from_str("1.70")
"""A carried level above every rebuilt level these tapes can give, so a carried case is visible."""
BOOK_CONFIG = {
    "book": "book_path",
    "account": "account_label",
    "overnight": "overnight",
}
"""The unit's three config fields the tests need, by purpose. Rename a field here and nowhere else."""


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


@pytest.fixture(autouse=True)
def neutral_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The book's default folder resolves under ``CLAUDE_CONFIG_DIR``, a temp folder here."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


class ShapeHarness(base.Harness):
    """The base command harness, with its quotes on the neutral ``SHAPE.ALPACA`` instrument."""

    def observe(self, bid: str, at: int) -> None:
        tick = base.quote(bid, at, SHAPE)
        self.cached.latest = tick
        self.test_clock.set_time(max(at, self.test_clock.timestamp_ns()))
        self.on_quote(tick)


def start_level_module() -> Any:
    """Import the module under test inside the case, so each case is red on its own."""
    return import_module("canon_trading.start_level")


def holding(quantity: str = "10", *, start: int = START) -> Any:
    """One neutral EXTERNAL long whose protection start is ``start``."""
    return SimpleNamespace(
        id=PositionId(f"{SHAPE}-EXTERNAL"),
        instrument_id=SHAPE,
        strategy_id=base.EXTERNAL,
        signed_qty=Decimal(quantity),
        quantity=Quantity.from_str(quantity),
        account_id=AccountId("NEUTRAL-000"),
        is_long=Decimal(quantity) > 0,
        is_open=Decimal(quantity) != 0,
        ts_opened=start,
        ts_init=start,
    )


def minute_bar(close: str, minute: int, *, high: str | None = None) -> Bar:
    """One complete one-minute trade bar, ``minute`` minutes before ``T0``."""
    price = Price.from_str(close)
    return Bar(
        BarType.from_str(f"{SHAPE}-1-MINUTE-LAST-EXTERNAL"),
        price,
        price if high is None else Price.from_str(high),
        price,
        price,
        Quantity.from_int(100),
        T0 - minute * MINUTE,
        T0 - (minute - 1) * MINUTE,
    )


def climbing_tape() -> list[Bar]:
    """Sixteen complete closes from 1.40 to 1.54; the newest sixteen are 1.40 then 1.54."""
    closes = (
        "1.54",
        "1.52",
        "1.50",
        "1.48",
        "1.46",
        "1.44",
        "1.42",
        "1.40",
        "1.42",
        "1.44",
        "1.46",
        "1.48",
        "1.50",
        "1.52",
        "1.50",
        "1.48",
    )
    return [minute_bar(close, minute) for minute, close in enumerate(closes, start=2)]


def falling_tape() -> list[Bar]:
    """Sixteen complete closes near 1.20: about a fifth under ``CARRY`` and its rebuilt level."""
    closes = (
        "1.18",
        "1.19",
        "1.20",
        "1.21",
        "1.20",
        "1.19",
        "1.18",
        "1.17",
        "1.18",
        "1.19",
        "1.20",
        "1.21",
        "1.20",
        "1.19",
        "1.18",
        "1.17",
    )
    return [minute_bar(close, minute) for minute, close in enumerate(closes, start=2)]


def book_key(
    *, account: str = ACCOUNT, instrument: InstrumentId = SHAPE, start: int = START
) -> Any:
    """The holding's key: account label, instrument id, protection start in ns."""
    # landing lane, 2026-11-13: U-e A keys a holding by account and instrument; the protection
    # start is the entry's own field (item 1: ts_opened is the reconciliation clock); see FIXROUND-L1.md §U-e
    return (account, instrument)


def entry(
    level: str,
    *,
    start: int = START,
    quantity: str = "10",
    written_ns: int = T0,
    run: str = "earlier-run",
    distance: str | None = "0.04",
    reading: bool = True,
) -> Any:
    """The book's entry for one holding, constructed through the module's public name.

    DeepSeek P2-2: an entry carries the venue's reading of its fills, here at its write;
    reading=False is an entry written before readings were kept, never the same holding."""
    # landing lane, 2026-11-13: review F9: an entry carries the distance it was armed with (the
    # 17:43 ruling), so the carry cases take the carried-distance path; distance=None is an entry
    # written before distances were kept (ruling (a)); see FIXROUND-L1.md §Review fix round
    return start_level_module().Entry(
        last_level=Decimal(level),
        protection_start_ns=start,
        quantity=Decimal(quantity),
        written_ns=written_ns,
        run=run,
        distance=None if distance is None else Decimal(distance),
        horizon=start_level_module().Horizon(written_ns) if reading else None,
    )


def entry_fields(found: Any) -> tuple[Any, Any, Any, Any, Any]:
    """Every field Part 1 gives an Entry, in one comparable tuple."""
    return (
        found.last_level,
        found.protection_start_ns,
        found.quantity,
        found.written_ns,
        found.run,
    )


def seed_book(path: Path, entries: dict[Any, Any]) -> None:
    start_level_module().LevelBook.write(path, entries)


def read_book(path: Path) -> Any:
    return start_level_module().LevelBook.read(path)


def protector(
    tmp_path: Path,
    *,
    book: Path | None = None,
    overnight: bool = False,
    levels: dict[InstrumentId, Price] | None = None,
    held: list[Any] | None = None,
    history: list[Bar] | None = None,
    bid: str = "1.10",
) -> ShapeHarness:
    """A command harness on SHAPE with the unit's config fields, one neutral holding and a tape."""
    # The fix round's one edit to this file: the brief left the config's field names open, so the
    # build's names are mapped here (book_path -> level_book_path, overnight -> session_part).
    fields: dict[str, Any] = {
        "level_book_path": tmp_path / "levels.json" if book is None else book,
        BOOK_CONFIG["account"]: ACCOUNT,
        "session_part": "overnight" if overnight else "regular",
        # landing lane, 2026-11-13: U-e B reads the venue's sells through the same reader; this
        # venue sold none since the entry, so the entry's holding is the one held; see FIXROUND-L1.md §U-e
        "trading": base.VenueOrders(),
    }
    if levels is not None:
        fields["levels"] = levels
    strategy = ShapeHarness(**fields)
    strategy.cached.held = [holding()] if held is None else list(held)
    strategy.cached.history = list(history or [])
    strategy.cached.latest = base.quote(bid, T0, SHAPE)
    return strategy


def two_quotes(strategy: ShapeHarness) -> None:
    """Two observations: a fresh adoption guards the first and sends on the second."""
    base.new_quote(strategy)
    latest = strategy.cached.latest
    assert latest is not None
    bid = str(latest.bid_price)
    strategy.observe(bid, strategy.clock.timestamp_ns() + SECOND // 2)


def row_distance(tmp_path: Path, bars: list[Bar], bid: str) -> Decimal:
    """The row's trailing distance in dollars, measured through a supplied-level run's order."""
    measured = protector(
        tmp_path,
        book=tmp_path / "measure-levels.json",
        levels={SHAPE: Price.from_str("0.90")},
        history=bars,
        bid=bid,
    )
    measured.on_start()
    two_quotes(measured)
    assert len(measured.sent) == 1, measured.said
    return measured.sent[0].trailing_offset


def rebuilt_expected(
    tmp_path: Path, bars: list[Bar], bid: str, *, previous: Decimal | None = None
) -> Decimal:
    """The highest close since the protection start less the row distance, floored to the step."""
    distance = row_distance(tmp_path, bars, bid)
    closes = [bar.close.as_decimal() for bar in bars if bar.ts_event >= START]
    rebuilt = rebuilt_level(closes, Decimal("0.01"), distance)
    return rebuilt if previous is None else max(previous, rebuilt)


def set_instrument(strategy: ShapeHarness, instrument: Any) -> None:
    """The cache's instrument for SHAPE, carrying an asset's ``attributes`` in its ``info``."""
    cast(Any, strategy.cached).instrument = lambda _instrument_id: instrument
    # landing lane, 2026-11-13: the build reads eligibility through OwnerProtectorConfig.trading,
    # given the same attributes as Alpaca's raw asset; the unreadable case keeps no reader
    # (seam C); see FIXROUND-L1.md §2
    attributes = instrument.info["attributes"]
    if attributes is not None:
        asset = {"attributes": attributes}
        # landing lane, 2026-11-13: the reader also answers U-e B's GET /orders; see FIXROUND-L1.md §U-e
        strategy.settings.trading = base.VenueOrders(asset=asset)


def shape_equity(attributes: Any) -> Equity:
    """The adapter's shape: the asset's raw ``attributes`` kept in the instrument's ``info``."""
    return Equity(
        instrument_id=SHAPE,
        raw_symbol=Symbol("SHAPE"),
        currency=base.USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        ts_event=0,
        ts_init=0,
        info={"attributes": attributes},
    )


# P1: never lower.  ``start_level(p, r) == max(p, r)``; ``previous=None`` gives ``r``; an off-step
# value stays as it is.


@pytest.mark.parametrize(
    ("previous", "rebuilt", "expected"),
    [
        (None, "1.02", "1.02"),
        ("1.10", "1.02", "1.10"),
        ("1.02", "1.10", "1.10"),
        ("1.10", "1.10", "1.10"),
        ("1.2345", "1.10", "1.2345"),
        ("1.10", "1.234567", "1.234567"),
    ],
    ids=(
        "no-previous-takes-the-rebuilt",
        "previous-wins",
        "rebuilt-wins",
        "equal",
        "off-step-previous-is-not-rounded",
        "off-step-rebuilt-is-not-rounded",
    ),
)
def test_start_level_is_the_higher_level_and_never_rounds_it(
    previous: str | None, rebuilt: str, expected: str
) -> None:
    module = start_level_module()
    reb = Decimal(rebuilt)
    raised = module.start_level(None if previous is None else Decimal(previous), reb)
    assert isinstance(raised, Decimal)
    assert raised == Decimal(expected)
    assert raised >= reb
    if previous is not None:
        assert raised >= Decimal(previous)
        assert raised == max(Decimal(previous), reb)


# P1's LevelBook and entry fields: the write and the read round-trip every field Part 1 names, with
# ``last_level`` kept as decimal text.


def test_level_book_round_trips_every_entry_field(tmp_path: Path) -> None:
    module = start_level_module()
    path = tmp_path / "levels.json"
    made = entry("1.2345", quantity="10", written_ns=T0, run="earlier-run")
    module.LevelBook.write(path, {book_key(): made})
    found = module.LevelBook.read(path)
    assert len(found) == 1
    found_key, found_entry = next(iter(found.items()))
    assert tuple(found_key) == book_key()
    assert entry_fields(found_entry) == entry_fields(made)
    assert "1.2345" in path.read_text()


# P5, the atomic write: a write that fails leaves the previous file whole, and the reader still
# sees the older entry.


def test_a_failed_level_book_write_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = start_level_module()
    path = tmp_path / "levels.json"
    module.LevelBook.write(path, {book_key(): entry("1.30")})
    before = path.read_text()

    def interrupted(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("neutral interruption")

    monkeypatch.setattr(os, "replace", interrupted)
    try:
        module.LevelBook.write(path, {book_key(): entry("1.40")})
    except OSError:
        pass
    assert path.read_text() == before
    assert module.LevelBook.read(path)[book_key()].last_level == Decimal("1.30")


# P2: the rebuilt argument is the highest close since the protection start less the offset.  A close
# before the start and a minute's high must not lift it.


def rebuilt_tape(kind: str) -> list[Bar]:
    bars = climbing_tape()
    if kind == "pre-start-close":
        bars.append(minute_bar("1.80", 21))  # one minute before START, inside the ATR lookback
    else:
        bars.append(minute_bar("1.03", 18, high="1.70"))  # a high above every close, after START
    return bars


@pytest.mark.parametrize("kind", ["pre-start-close", "minute-high"])
# landing lane, 2026-11-13: review F9 renamed this case, which no longer rebuilds from closes (the
# 17:43 ruling): it checks that neither a close before the start nor a minute's high moves the
# carried level; see FIXROUND-L1.md §Review fix round
def test_no_close_or_minute_high_moves_the_carried_level(kind: str, tmp_path: Path) -> None:
    start_level_module()
    bars = rebuilt_tape(kind)
    seed_book(tmp_path / "levels.json", {book_key(): entry("0.90")})
    strategy = protector(tmp_path, history=bars, bid="1.10")
    strategy.on_start()
    two_quotes(strategy)
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid
    expected = Decimal("0.90")
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price.as_decimal() == expected, (
        "a close before the protection start or a minute's high moved the carried level"
    )


# P3: same holding only.  A matching key is carried; another account, instrument or protection
# start is not; a larger book quantity still counts; a smaller one counts and names added shares.


def test_a_previous_level_is_carried_for_the_same_holding(tmp_path: Path) -> None:
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY))})
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.10")
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price == CARRY


@pytest.mark.parametrize(
    ("other_key", "named"),
    [
        (("another-account", SHAPE, START), False),
        ((ACCOUNT, base.BBB, START), False),
        ((ACCOUNT, SHAPE, START - 5 * MINUTE), True),
    ],
    ids=("another-account", "another-instrument", "older-protection-start"),
)
def test_a_previous_entry_for_another_holding_is_not_carried(
    other_key: tuple[str, InstrumentId, int], named: bool, tmp_path: Path
) -> None:
    start_level_module()
    # landing lane, 2026-11-13: U-e A keys by account and instrument (the start is the entry's);
    # see FIXROUND-L1.md §U-e
    seed_book(tmp_path / "levels.json", {other_key[:2]: entry(str(CARRY), start=other_key[2])})
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.10")
    if named:
        # landing lane, 2026-11-13: another holding of the same name is one the venue sold out
        # since the entry was written (U-e B); see FIXROUND-L1.md §U-e
        strategy.settings.trading = base.VenueOrders([base.venue_fill("sell", "10", "SHAPE")])
    strategy.on_start()
    two_quotes(strategy)
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid
    # (point 5: no carried level self-arms as unit 10 does, bid less the row distance)
    expected = up_on_step(Decimal("1.10") - row_distance(tmp_path, climbing_tape(), "1.10"))
    assert len(strategy.sent) == 1, strategy.said
    trigger = strategy.sent[0].trigger_price.as_decimal()
    assert trigger == expected
    assert trigger < CARRY.as_decimal(), "an entry for another holding lowered the level"
    if named:
        assert any("previous level not carried: a new holding" in note for note in strategy.said), (
            strategy.said
        )


def test_a_previous_entry_with_a_larger_quantity_still_counts(tmp_path: Path) -> None:
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY), quantity="15")})
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.10")
    # landing lane, 2026-11-13: review F4 carries a changed quantity only where the venue's fills
    # explain it: 15 to 10 by a sale of 5; see FIXROUND-L1.md §Review fix round
    strategy.settings.trading = base.VenueOrders([base.venue_fill("sell", "5", "SHAPE")])
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price == CARRY, "a partial sale lowered the level"


def test_a_previous_entry_with_a_smaller_quantity_counts_and_names_added_shares(
    tmp_path: Path,
) -> None:
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY), quantity="5")})
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.10")
    # landing lane, 2026-11-13: review F4 carries a changed quantity only where the venue's fills
    # explain it: 5 to 10 by a buy of 5; see FIXROUND-L1.md §Review fix round
    strategy.settings.trading = base.VenueOrders([base.venue_fill("buy", "5", "SHAPE")])
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price == CARRY
    assert any("added" in note.lower() and "carried" in note.lower() for note in strategy.said), (
        strategy.said
    )


# landing lane, 2026-11-13: the floor A case at bid 1.75; see FIXROUND-L1.md §2
def test_a_carried_level_under_the_self_arm_floor_arms_at_the_carried_level(tmp_path: Path) -> None:
    """At bid 1.75 unit 10's self-arm floor, bid less the row distance rounded up (1.71), is above
    the carried 1.70 and the rebuilt level. Since the lead's ruling of 2026-11-13 17:43 the armed
    trigger is the carried 1.70: nothing re-derives a carried level, and the first bid raises it
    only through the emulator once armed."""
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY))})
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.75")
    strategy.on_start()
    two_quotes(strategy)
    distance = row_distance(tmp_path, climbing_tape(), "1.75")
    rebuilt = rebuilt_expected(tmp_path, climbing_tape(), "1.75")
    floor = up_on_step(Decimal("1.75") - distance)
    assert len(strategy.sent) == 1, strategy.said
    trigger = strategy.sent[0].trigger_price.as_decimal()
    rule = (
        "the armed level is the highest of the book's level and the bid less the row's distance, "
        "and never under the book's"
    )
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid
    # (point 2: neither the self-arm floor 1.71 nor the level rebuilt from closes re-derives it)
    assert floor == Decimal("1.71") and rebuilt < floor, rule
    assert trigger == CARRY.as_decimal() == Decimal("1.70"), (
        "the armed level is the carried level (the lead's ruling, 2026-11-13 17:43)"
    )


# P4: a carried level at or above the first bid does not sell on one observation.  Unit 10's guard
# holds exactly as it does for a supplied level (the control below), and the sale may follow later.


def test_a_carried_level_at_or_above_the_first_bid_does_not_sell_on_one_observation(
    tmp_path: Path,
) -> None:
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY))})
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.05")
    strategy.on_start()
    strategy.observe("1.05", T0 + SECOND // 2)
    assert not strategy.sent, "the first quote since the arming released the sell"
    strategy.observe("1.04", T0 + SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == CARRY

    control = ShapeHarness(levels={SHAPE: CARRY})
    control.cached.held = [holding()]
    control.cached.latest = base.quote("1.05", T0, SHAPE)
    control.on_start()
    control.observe("1.05", T0 + SECOND // 2)
    assert not control.sent, "unit 10's guard regressed for a supplied level"
    control.observe("1.04", T0 + SECOND)
    assert len(control.sent) == 1
    assert control.sent[0].trigger_price == CARRY


# P5: the run writes what it ended with.  The emulated sell's trigger, or the retained level when
# none was armed; a write that fails is named and the stop still ends the run.


def test_on_stop_writes_the_armed_trigger_and_every_entry_field(tmp_path: Path) -> None:
    module = start_level_module()
    path = tmp_path / "levels.json"
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.75")
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    armed = strategy.sent[0].trigger_price
    strategy.on_stop()
    found = module.LevelBook.read(path)
    assert len(found) == 1
    found_key, written = next(iter(found.items()))
    assert tuple(found_key) == book_key()
    assert written.last_level == armed.as_decimal()
    assert written.protection_start_ns == START
    assert written.quantity == Decimal(10)
    assert written.written_ns > 0
    assert isinstance(written.run, str) and written.run


def test_a_retained_level_is_written_when_no_sell_was_armed(tmp_path: Path) -> None:
    module = start_level_module()
    strategy = protector(tmp_path, levels={SHAPE: CARRY}, history=climbing_tape(), bid="1.20")
    strategy.on_start()
    assert not strategy.sent
    strategy.on_stop()
    found = module.LevelBook.read(tmp_path / "levels.json")
    assert len(found) == 1
    _found_key, written = next(iter(found.items()))
    assert written.last_level == CARRY.as_decimal()


def test_a_failed_book_write_is_named_and_the_stop_stands(tmp_path: Path) -> None:
    start_level_module()
    path = tmp_path / "levels.json"
    strategy = protector(tmp_path, levels={SHAPE: CARRY}, history=climbing_tape(), bid="1.20")
    strategy.on_start()
    assert not strategy.sent
    # landing lane, 2026-11-13: P10 wrote the book at start (seam B); see FIXROUND-L1.md §2
    path.unlink(missing_ok=True)
    path.mkdir()  # the configured book path exists, and cannot be replaced by a file
    strategy.on_stop()
    assert strategy.stopping is True
    assert any("levels.json" in note for note in strategy.said), strategy.said


# P6: a missing or unreadable book means the previous level is None for the holding, is never a
# refusal to protect, and the rebuilt level still arms.


@pytest.mark.parametrize("book_kind", ["missing", "malformed"])
def test_a_missing_or_malformed_book_still_arms_the_rebuilt_level(
    book_kind: str, tmp_path: Path
) -> None:
    start_level_module()
    path = tmp_path / "levels.json"
    if book_kind == "malformed":
        path.write_text("{ this is not a level book")
    strategy = protector(tmp_path, book=path, history=climbing_tape(), bid="1.10")
    strategy.on_start()
    two_quotes(strategy)
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid
    # (point 5: no carried level self-arms as unit 10 does, bid less the row distance)
    expected = up_on_step(Decimal("1.10") - row_distance(tmp_path, climbing_tape(), "1.10"))
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price.as_decimal() == expected
    assert any(
        "SHAPE.ALPACA" in note and ("book" in note.lower() or "previous" in note.lower())
        for note in strategy.said
    ), strategy.said


# P7: overnight eligibility.  An eligible holding is armed as above; an ineligible or halted one
# gets no order, is named "uncoverable overnight: <reason>" on every run, and its book entry is
# kept unchanged; an unreadable eligibility counts as ineligible.


def test_an_eligible_holding_is_armed_overnight(tmp_path: Path) -> None:
    start_level_module()
    seed_book(tmp_path / "levels.json", {book_key(): entry(str(CARRY))})
    # landing lane, 2026-11-13: bid under the carried and rebuilt levels plus the row distance (floor A); see FIXROUND-L1.md §2
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.10", overnight=True)
    set_instrument(strategy, shape_equity(["overnight_tradable"]))
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    assert strategy.sent[0].trigger_price == CARRY
    assert not any("uncoverable overnight" in note for note in strategy.said)


@pytest.mark.parametrize(
    "attributes",
    [[], ["overnight_tradable", "overnight_halted"], None],
    ids=("ineligible", "halted", "unreadable"),
)
def test_an_ineligible_holding_gets_no_order_on_any_overnight_run(
    attributes: list[str] | None, tmp_path: Path
) -> None:
    module = start_level_module()
    path = tmp_path / "levels.json"
    held = {book_key(): entry(str(CARRY), written_ns=T0 - SECOND, run="older-run")}
    module.LevelBook.write(path, held)
    before = entry_fields(held[book_key()])
    for _run_number in range(2):
        strategy = protector(tmp_path, history=climbing_tape(), bid="1.75", overnight=True)
        set_instrument(strategy, shape_equity(attributes))
        strategy.on_start()
        two_quotes(strategy)
        assert not strategy.sent, strategy.said
        assert any(
            "SHAPE.ALPACA" in note and "uncoverable overnight" in note for note in strategy.said
        ), strategy.said
        strategy.on_stop()
        found = module.LevelBook.read(path)
        assert entry_fields(found[book_key()]) == before


# P8: back to back.  The second run, after the first wrote its book, arms at least the first's last
# trigger even after a fall, not at bid less the distance, and the sale follows as a marked limit.


def test_two_runs_in_sequence_never_lower_the_armed_trigger(tmp_path: Path) -> None:
    start_level_module()
    path = tmp_path / "levels.json"
    first = protector(tmp_path, history=climbing_tape(), bid="1.75")
    first.on_start()
    two_quotes(first)
    assert len(first.sent) == 1, first.said
    first_level = first.sent[0].trigger_price
    first.on_stop()

    assert path.exists(), "the first run did not write its book"
    second = protector(tmp_path, history=falling_tape(), bid="1.18")
    second.on_start()
    second.observe("1.18", T0 + SECOND // 2)
    assert not second.sent, "the first quote after the fall released a sale on one observation"
    second.observe("1.18", T0 + SECOND)
    assert len(second.sent) == 1, second.said
    sale = second.sent[0]
    assert sale.trigger_price >= first_level
    assert sale.trigger_price == first_level
    assert sale.trigger_price.as_decimal() > Decimal("1.18"), (
        "armed at bid less the distance, not carried"
    )
    assert sale.order_type == OrderType.TRAILING_STOP_LIMIT
    assert sale.is_reduce_only
    assert second.params[0] == {"extended_hours": True}


# P9: no owner data.  The book a neutral run writes carries only the neutral instrument, the neutral
# account label and scaled prices; no credential or real path enters it.


def test_the_book_carries_only_neutral_instrument_and_scaled_data(tmp_path: Path) -> None:
    start_level_module()
    path = tmp_path / "levels.json"
    strategy = protector(tmp_path, history=climbing_tape(), bid="1.75")
    strategy.on_start()
    two_quotes(strategy)
    assert len(strategy.sent) == 1, strategy.said
    strategy.on_stop()
    body = path.read_text()
    assert "SHAPE" in body
    assert ACCOUNT in body
    assert "APCA_" not in body, "a credential environment name entered the book"
    assert "ALPACA-001" not in body, "an account id entered the book"
    assert path.parent == tmp_path
