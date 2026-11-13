"""No print is set aside because of this host's clock offset, whatever its size.

The lead's ruling of 2026-11-13 (08:18, term 3; restated 08:49): a holding's exit never depends on this
host's clock agreeing with the venue's. "Ahead" is measured on the venue's time as the stream shows
it: a print is compared with the newest quote of its name, each taken as venue time less receipt on
this host, so the host's offset cancels (`print_time`). A print more than 5 s ahead of the stream has
no usable time (P5). With no quote of the name yet, the old 5 s test against receipt applies, and the
record counts each such print. NautilusTrader at the pin corrects no clock offset (see `print_time`);
the rule is ours.
"""

from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import TriggerType

from canon_trading import session_window
from canon_trading.mover import PRINT_AHEAD_MOST_NS
from canon_trading.mover import Mover
from canon_trading.mover import quote_lag
from tests.test_mover import SECOND
from tests.test_mover import strategy
from tests.test_mover_breach import MS
from tests.test_mover_print_breach import holding

BBB = InstrumentId.from_str("BBB.ALPACA")
LATENCY = 20 * MS
OFFSETS = [-60 * SECOND, 60 * SECOND]
"""The venue's clock less this host's: positive when this host's clock is behind."""
OFFSET_IDS = ["host-ahead-60s", "host-behind-60s"]


def received(price: str, venue: int, skew: int, instrument: Any = BBB) -> SimpleNamespace:
    """A print the venue stamped `venue`, received `LATENCY` later by this host's clock."""
    return SimpleNamespace(
        instrument_id=instrument,
        price=Price.from_str(price),
        ts_event=venue,
        ts_init=venue - skew + LATENCY,
    )


def quoted(venue: int, skew: int, instrument: Any = BBB, bid: str = "9.89", ask: str = "9.91"):
    """A quote the venue stamped `venue`, received `LATENCY` later by this host's clock."""
    return SimpleNamespace(
        instrument_id=instrument,
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        ts_event=venue,
        ts_init=venue - skew + LATENCY,
    )


def held(skew: int, quote: bool = True) -> tuple[Any, Any, int, list]:
    """A holding filled at 10.00, held at 9.80 under a print trigger with a two-sided quote; this
    host's clock is `skew` behind the venue's throughout. With `quote`, the stream has shown a quote
    of the name; its last print is from before the entry."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    seam.slots, seam.trades = {slot.instrument_id: slot}, 0
    if quote:
        seam.on_quote(quoted(filled - 40 * SECOND, skew, slot.instrument_id))
    before = received("10.0000", filled - 30 * SECOND, skew, slot.instrument_id)
    seam.on_trade(before)
    last.append(before)
    seam._protect(slot, D(3), filled + SECOND - skew)
    assert slot.held_stop == D("9.80") and not seam.sent
    return seam, slot, filled, last


def deliver(seam: Any, last: list, trade: Any) -> None:
    last.append(trade)
    seam.on_trade(trade)


@pytest.mark.parametrize("skew", OFFSETS, ids=OFFSET_IDS)
def test_a_minute_off_prints_order_normally_and_none_is_ahead(skew: int) -> None:
    """(a) With this host's clock a minute behind or ahead, each print is ordered by the venue's time:
    the newer one is the price every reader follows, an older one reported late moves nothing and is
    counted late, and none is classed as ahead."""
    seam, slot, filled, last = held(skew)
    name = str(slot.instrument_id)
    assert slot.newest_print is not None and slot.newest_print.price == D("10.00")
    deliver(seam, last, received("10.2000", filled + 2 * SECOND, skew, slot.instrument_id))
    assert seam._followed(slot) == D("10.20") and seam._last_print(slot) == D("10.20")
    late = received("9.0000", filled + SECOND, skew, slot.instrument_id)
    late.ts_init += 2 * SECOND
    deliver(seam, last, late)
    assert seam._followed(slot) == D("10.20")
    assert seam.late_prints == {name: {"count": 1, "most_late_ms": 1000}}
    assert seam.prints_ahead == {}
    assert seam.prints_on_receipt == {}


@pytest.mark.parametrize("skew", OFFSETS, ids=OFFSET_IDS)
def test_a_minute_off_a_breached_hold_sells_within_its_hold(skew: int) -> None:
    """(a) The holding's first print is 9.70, through the held stop: the next step sells, as it does
    with the clocks agreeing."""
    seam, slot, filled, last = held(skew)
    deliver(seam, last, received("9.7000", filled + 2 * SECOND, skew, slot.instrument_id))
    seam._protect(slot, D(3), filled + 3 * SECOND - skew)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason.startswith("held stop")


@pytest.mark.parametrize("skew", OFFSETS, ids=OFFSET_IDS)
def test_a_print_six_seconds_ahead_of_the_stream_has_no_usable_time(skew: int) -> None:
    """(b) Under the same offsets, a print stamped 6 s ahead of the stream (its venue time less its
    receipt, 6 s past the quote's) is neither the newest nor late, and is counted ahead; through the
    held stop it sells nothing."""
    seam, slot, filled, last = held(skew)
    kept = slot.newest_print
    ahead = received("9.7000", filled + 2 * SECOND, skew, slot.instrument_id)
    ahead.ts_init -= 6 * SECOND
    deliver(seam, last, ahead)
    assert slot.newest_print == kept
    assert seam.prints_ahead == {str(slot.instrument_id): 1}
    assert seam.prints_on_receipt == {}
    seam._protect(slot, D(3), filled + 3 * SECOND - skew)
    assert not seam.sent and not slot.exiting


def test_with_no_quote_the_receipt_test_applies_and_the_record_says_so() -> None:
    """(c) With no quote of the name yet, the venue's time cannot be had: the old 5 s test against
    receipt applies. A print 4 s ahead of its receipt orders; one past 5 s has no usable time. The
    record counts each print so judged, by symbol."""
    seam, slot, filled, last = held(0, quote=False)
    name = str(slot.instrument_id)
    usable = received("10.1000", filled + 2 * SECOND, 0, slot.instrument_id)
    usable.ts_init -= 4 * SECOND
    deliver(seam, last, usable)
    assert slot.newest_print is not None and slot.newest_print.price == D("10.10")
    past = received("10.3000", filled + 3 * SECOND, 0, slot.instrument_id)
    past.ts_init -= PRINT_AHEAD_MOST_NS + SECOND
    deliver(seam, last, past)
    assert slot.newest_print.price == D("10.10")
    assert seam.prints_ahead == {name: 1}
    assert seam.prints_on_receipt == {name: 3}
    s = strategy(trigger=TriggerType.LAST_PRICE)
    s.prints_on_receipt[name] = 3
    assert s.record()["prints_timed_on_receipt"] == {name: 3}


def test_a_quote_seen_before_the_slot_opened_sets_its_reference() -> None:
    """A slot that opens after its name's quote reads the quote's venue time from the cache, so its
    first print is not judged on receipt."""
    quote = quoted(1_000 * SECOND, 60 * SECOND)
    assert quote_lag(quote) == 60 * SECOND - LATENCY
    assert quote_lag(None) is None


@pytest.mark.parametrize("skew", [0, 30 * SECOND], ids=["clocks-agree", "host-behind-30s"])
def test_persistence_stamps_a_print_on_the_venue_s_clock(skew: int) -> None:
    """Persistence's stamp is the earlier of a print's venue time and its receipt, the receipt read on
    the venue's clock through the quote: so with this host's clock behind, a print older at the venue
    reported late stamps below the newest, as with the clocks agreeing, and counts toward no breach
    (code-review-high-host-clock-v3/1)."""
    seam, slot, filled, last = held(skew)
    deliver(seam, last, received("10.0000", filled + 5 * SECOND, skew, slot.instrument_id))
    assert slot.stamp_seen == filled + 5 * SECOND
    late = received("9.7000", filled + 2 * SECOND, skew, slot.instrument_id)
    late.ts_init += 4 * SECOND
    deliver(seam, last, late)
    assert slot.stamp_seen == filled + 5 * SECOND


@pytest.mark.parametrize("skew", [-60 * SECOND, -6 * SECOND, 6 * SECOND, 60 * SECOND])
def test_the_run_record_counts_the_difference_between_the_clocks(skew: int) -> None:
    """The record carries the venue's time less this host's receipt of the latest quotes, in
    milliseconds: their median, least and most. That is the clocks' difference less the feed's
    latency. Quotes, not prints: a print reported late would read below it."""
    s = strategy(trigger=TriggerType.LAST_PRICE)
    assert s.record()["host_clock"] == {
        "venue_minus_receipt_ms": None,
        "median_of": 0,
        "quotes": 0,
    }
    for k in range(8):
        Mover.on_quote(s, quoted(k * 100 * MS, skew))
    Mover.on_trade(s, received("20.00", SECOND, skew - 10 * SECOND))
    ms = (skew - LATENCY) // MS
    assert s.record()["host_clock"] == {
        "venue_minus_receipt_ms": {"median": ms, "least": ms, "most": ms},
        "median_of": 8,
        "quotes": 8,
    }
    noted = session_window.clock_notes(s.record())
    assert ("host_clock" in noted) is (abs(ms) > session_window.CLOCK_NOTE_MOST_MS)


@pytest.mark.parametrize(
    ("median", "noted"), [(2_000, False), (2_001, True), (-2_000, False), (-2_001, True)]
)
def test_the_window_names_the_clocks_difference_past_two_seconds(median: int, noted: bool) -> None:
    """The lead's rule of 2026-11-13, 08:40: a window whose clocks are more than 2 s apart goes to
    the lead, so its difference is named beside the Mover's record, either way."""
    clock = {"venue_minus_receipt_ms": {"median": median, "least": median, "most": median}}
    run = {"prints_stamped_ahead": {}, "host_clock": clock}
    assert session_window.clock_notes(run) == ({"host_clock": clock} if noted else {})


def test_one_quote_reported_late_moves_no_print_into_ahead() -> None:
    """A name's reference is the median of its latest quotes, so one quote reported 6 s late does not
    class the prints after it as ahead (code-review-high-host-clock-v4/2)."""
    seam, slot, filled, last = held(60 * SECOND)
    for k in range(3):
        seam.on_quote(quoted(filled + k * 100 * MS, 60 * SECOND, slot.instrument_id))
    late = quoted(filled + 300 * MS, 60 * SECOND, slot.instrument_id)
    late.ts_init += 6 * SECOND
    seam.on_quote(late)
    deliver(seam, last, received("10.2000", filled + 2 * SECOND, 60 * SECOND, slot.instrument_id))
    assert slot.newest_print is not None and slot.newest_print.price == D("10.20")
    assert seam.prints_ahead == {}


def test_with_the_clocks_agreeing_a_late_quote_moves_no_persistence_stamp() -> None:
    """With the clocks agreeing, a print's persistence stamp is its venue time, as on main, whatever
    its name's quotes' delays (code-review-high-host-clock-v4/1)."""
    seam, slot, filled, last = held(0)
    for k in range(5):
        late = quoted(filled + k * 100 * MS, 0, slot.instrument_id)
        late.ts_init += 150 * MS
        seam.on_quote(late)
    deliver(seam, last, received("10.1000", filled + 2 * SECOND, 0, slot.instrument_id))
    assert slot.stamp_seen == filled + 2 * SECOND


@pytest.mark.parametrize("skew", OFFSETS, ids=OFFSET_IDS)
def test_after_one_timely_quote_a_late_one_moves_no_print_into_ahead(skew: int) -> None:
    """A quote reported late only lowers its venue time less receipt, so the reference is the highest
    of the name's latest quotes: with one timely quote seen, a second reported 6 s late does not class
    the next print as ahead (code-review-high-host-clock-v5/2)."""
    seam, slot, filled, last = held(skew)
    late = quoted(filled + 100 * MS, skew, slot.instrument_id)
    late.ts_init += 6 * SECOND
    seam.on_quote(late)
    deliver(seam, last, received("10.2000", filled + 2 * SECOND, skew, slot.instrument_id))
    assert slot.newest_print is not None and slot.newest_print.price == D("10.20")
    assert seam.prints_ahead == {}


def test_one_quote_stamped_far_ahead_does_not_switch_off_the_ahead_bound() -> None:
    """The reference is the upper median of the name's latest quotes, so one quote whose venue time
    runs 30 s ahead does not let a print 20 s ahead of the stream order (code-review-high-combined/4)."""
    seam, slot, filled, last = held(60 * SECOND)
    for k in range(4):
        seam.on_quote(quoted(filled + k * 100 * MS, 60 * SECOND, slot.instrument_id))
    bad = quoted(filled + 500 * MS, 60 * SECOND, slot.instrument_id)
    bad.ts_event += 30 * SECOND
    seam.on_quote(bad)
    ahead = received("9.7000", filled + 2 * SECOND, 60 * SECOND, slot.instrument_id)
    ahead.ts_init -= 20 * SECOND
    deliver(seam, last, ahead)
    assert seam.prints_ahead == {str(slot.instrument_id): 1}
