"""Blind C2/P5 tests at 2c7d9a3d: print timing follows the venue, with receipt as fallback.

Contract: NautilusTrader d53849b216e2 TradeTick/QuoteTick.ts_event is event time and
ts_init is initialization time (crates/model/src/data/{trade,quote}.rs). Its data engine
caches each tick before publishing it (crates/data/src/engine/dispatch.rs:102-128);
Cache.quote/trade return the last insertion (crates/common/src/cache/mod.rs:6343-6373).
The lead's supplied properties specify the five-second bound and no-quote fallback.

All times and prices are SHAPED. The existing PrintSeam supplies Mover's real initialized
state and methods; this extension only supplies timestamped quotes and the host clock.
No paper state, catalog, ledger, network, or order transport is used.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeTick

from canon_trading.mover import Mover
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover_late_prints_blind import PrintSeam
from tests.test_mover_late_prints_blind import trade

MINUTE_OFFSETS = [-60 * SECOND, 60 * SECOND]
MINUTE_NAMES = ["host-60s-behind", "host-60s-ahead"]


class VenueClockSeam(PrintSeam):
    """Cache real quote ticks by name, before dispatch, without choosing any print time."""

    def __init__(self, offset: int = 0) -> None:
        super().__init__()
        self.offset = offset
        self.now = T0 + offset
        self.stream_quotes: dict[InstrumentId, QuoteTick] = {}
        self.cache.quote = self.stream_quotes.get

    def quote(self, event: int, instrument: InstrumentId = AAA) -> None:
        tick = QuoteTick(
            instrument,
            Price.from_str("10.0000"),
            Price.from_str("10.0100"),
            Quantity.from_int(100),
            Quantity.from_int(100),
            T0 + event,
            T0 + event + self.offset,
        )
        self.now = tick.ts_init
        self.stream_quotes[instrument] = tick
        Mover.on_quote(cast(Any, self), tick)

    def deliver(self, tick: TradeTick) -> None:
        self.now = tick.ts_init
        super().deliver(tick)


def receipt_basis_named(value: object, path: str = "") -> bool:
    """Recognize a positive receipt-basis diagnostic without inventing a record field name.

    The supplied property specifies no schema: a note, a basis enum, or a named positive
    counter can state it. Require print/trade and receipt/ts_init in the same leaf path;
    a zero counter, an empty mapping, or the ahead count alone does not state fallback.
    """
    if isinstance(value, dict):
        return any(receipt_basis_named(child, f"{path} {key}") for key, child in value.items())
    if isinstance(value, list):
        return any(receipt_basis_named(child, path) for child in value)
    if value is None or value is False or value == 0:
        return False
    text = f"{path} {value}".casefold()
    return any(word in text for word in ("print", "trade")) and any(
        word in text for word in ("receipt", "received", "ts_init", "host_clock", "host clock")
    )


@pytest.mark.parametrize(
    "offset",
    [-86_400 * SECOND, *MINUTE_OFFSETS, 86_400 * SECOND],
    ids=["host-1d-behind", *MINUTE_NAMES, "host-1d-ahead"],
)
def test_c2_normal_print_order_ignores_host_offset(offset: int) -> None:
    """Case (a): every normal quote and print has the same ts_init - ts_event offset."""
    seam = VenueClockSeam(offset)
    slot = seam.open_slot()
    for event, price in ((10 * SECOND, "10.00"), (11 * SECOND, "10.20"), (11 * SECOND, "10.21")):
        seam.quote(event)
        seam.deliver(trade(price, event, event + offset))
        kept = slot.newest_print
        assert kept is not None, "an ordinary print lost its usable venue time to host offset"
        assert (kept.event_ns, kept.price) == (T0 + event, Decimal(price))
        holding = seam._holding_trade(slot)
        assert holding is not None and holding.price == Decimal(price)
        assert seam._followed(slot) == Decimal(price)
        assert seam.record()["prints_stamped_ahead"] == {}
        assert seam.record()["late_prints_set_aside"] == {}


@pytest.mark.parametrize("offset", MINUTE_OFFSETS, ids=MINUTE_NAMES)
def test_c2_host_offset_does_not_lose_the_held_low(offset: int) -> None:
    """A usable low through the held stop must survive the following rebound print."""
    seam = VenueClockSeam(offset)
    slot = seam.open_slot()
    slot.held_since, slot.held_stop = T0 + offset, Decimal("9.80")
    for event, price in ((10 * SECOND, "9.70"), (11 * SECOND, "10.10")):
        seam.quote(event)
        seam.deliver(trade(price, event, event + offset))
    assert slot.held_low == Decimal("9.70"), "host offset erased the held stop's low print"
    holding = seam._holding_trade(slot)
    assert holding is not None and holding.price == Decimal("10.10")
    assert seam.record()["prints_stamped_ahead"] == {}


@pytest.mark.parametrize("offset", MINUTE_OFFSETS, ids=MINUTE_NAMES)
@pytest.mark.parametrize("lead", [5 * SECOND + 1, 6 * SECOND], ids=["5s-plus-1ns", "6s"])
def test_p5_ahead_is_measured_from_quotes_not_receipt(offset: int, lead: int) -> None:
    """Case (b): a corrupt event stamp is ahead of the simultaneous quote by over five seconds."""
    seam = VenueClockSeam(offset)
    slot = seam.open_slot()
    slot.held_since, slot.held_stop = T0 + offset, Decimal("9.80")
    venue_now = 10 * SECOND
    seam.quote(venue_now)
    # Receipt is the same host instant as the quote; only the print's event stamp is corrupted.
    seam.deliver(trade("9.70", venue_now + lead, venue_now + offset))
    assert slot.newest_print is None, "a future print acquired a usable ordering timestamp"
    assert slot.held_low is None, "a future print crossed the held stop"
    assert seam.record()["prints_stamped_ahead"] == {str(AAA): 1}
    assert seam.record()["late_prints_set_aside"] == {}


@pytest.mark.parametrize("offset", MINUTE_OFFSETS, ids=MINUTE_NAMES)
def test_p5_ahead_print_cannot_replace_the_holding_price_or_poison_later_prints(
    offset: int,
) -> None:
    """A six-second future print cannot replace a valid print, latch its low, or hide recovery."""
    seam = VenueClockSeam(offset)
    slot = seam.open_slot()
    slot.held_since, slot.held_stop = T0 + offset, Decimal("9.80")
    seam.quote(10 * SECOND)
    seam.deliver(trade("10.00", 10 * SECOND, 10 * SECOND + offset))
    seam.quote(11 * SECOND)
    seam.deliver(trade("9.70", 17 * SECOND, 11 * SECOND + offset))
    holding = seam._holding_trade(slot)
    assert holding is not None and holding.price == Decimal("10.00")
    assert slot.held_low == Decimal("10.00")
    seam.quote(12 * SECOND)
    seam.deliver(trade("10.20", 12 * SECOND, 12 * SECOND + offset))
    holding = seam._holding_trade(slot)
    assert holding is not None and holding.price == Decimal("10.20")
    assert slot.newest_print is not None and slot.newest_print.event_ns == T0 + 12 * SECOND
    assert slot.held_low == Decimal("10.00")
    assert seam.record()["prints_stamped_ahead"] == {str(AAA): 1}
    assert seam.record()["late_prints_set_aside"] == {}


@pytest.mark.parametrize("offset", MINUTE_OFFSETS, ids=MINUTE_NAMES)
def test_p5_exactly_five_seconds_ahead_of_the_venue_is_usable(offset: int) -> None:
    """The bound is strictly more than five seconds, on the same venue-time basis."""
    seam = VenueClockSeam(offset)
    slot = seam.open_slot()
    seam.quote(10 * SECOND)
    seam.deliver(trade("10.20", 15 * SECOND, 10 * SECOND + offset))
    assert slot.newest_print is not None
    assert slot.newest_print.event_ns == T0 + 15 * SECOND
    holding = seam._holding_trade(slot)
    assert holding is not None and holding.price == Decimal("10.20")
    assert seam.record()["prints_stamped_ahead"] == {}


@pytest.mark.parametrize("foreign_quote", [False, True], ids=["no-quotes", "other-name-only"])
@pytest.mark.parametrize(
    "lead,usable",
    [(5 * SECOND, True), (5 * SECOND + 1, False), (6 * SECOND, False)],
    ids=["exactly-5s", "5s-plus-1ns", "6s"],
)
def test_no_quote_for_this_name_keeps_the_receipt_bound(
    foreign_quote: bool, lead: int, usable: bool
) -> None:
    """Case (c): another symbol's quote cannot replace this symbol's absent venue clock."""
    seam = VenueClockSeam()
    slot = seam.open_slot()
    if foreign_quote:
        seam.quote(100 * SECOND, BBB)
    assert seam.cache.quote(AAA) is None
    seam.deliver(trade("10.20", 10 * SECOND + lead, 10 * SECOND))
    assert (slot.newest_print is not None) is usable
    assert seam.record()["prints_stamped_ahead"] == ({} if usable else {str(AAA): 1})
    assert seam.record()["late_prints_set_aside"] == {}


@pytest.mark.parametrize("foreign_quote", [False, True], ids=["no-quotes", "other-name-only"])
def test_no_quote_receipt_fallback_is_named_in_the_run_record(foreign_quote: bool) -> None:
    """Case (c) requires the fallback to be visible even when no print is rejected as ahead."""
    seam = VenueClockSeam()
    seam.open_slot()
    if foreign_quote:
        seam.quote(100 * SECOND, BBB)
    seam.deliver(trade("10.00", 10 * SECOND, 10 * SECOND))
    assert seam.record()["prints_stamped_ahead"] == {}
    record = seam.record()
    assert receipt_basis_named(record), (
        "the run record does not name receipt/ts_init as the print-time basis without a quote",
        record,
    )
