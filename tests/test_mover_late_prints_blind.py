"""Blind L6 properties on the supplied base; all prices and timestamps are SHAPED.

Contract: NautilusTrader d53849b216e2 Cache.trade/trades return reverse insertion order
(crates/common/src/cache/mod.rs:6218,6364); dispatch caches a TradeTick before publishing it
(crates/data/src/engine/dispatch.rs:117). TradeTick.ts_event is venue time, ts_init receipt.
The existing test_mover.Seam captures orders without an emulator, whose delivery policy is L8.
Only cache/clock/subscriptions/order transport are doubles: all decision methods belong to Mover.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from types import MethodType
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from canon_trading.mover import Mover
from canon_trading.mover import Slot
from tests import test_mover_breach as breach_cases
from tests import test_mover_trail as trail_cases
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam

MS = 1_000_000
ARMS = [(1, 1), (2, 1), (1, 2), (2, 2)]
ARM_NAMES = ["both-off", "breach-only", "high-only", "both-on"]
READERS = ["_followed", "_last_known", "_last_print"]


def trade(
    price: str,
    event: int,
    received: int,
    instrument: InstrumentId = AAA,
) -> TradeTick:
    return TradeTick(
        instrument,
        Price.from_str(f"{Decimal(price):.4f}"),
        Quantity.from_int(100),
        AggressorSide.SELL,
        TradeId(f"B-{instrument.symbol.value}-{event:x}-{received:x}"),
        T0 + event,
        T0 + received,
    )


class PrintSeam(Seam):
    """The existing sale seam, with Mover's real initialized state and insertion-ordered trades.

    Resolve additional Mover methods dynamically so a future shared reader need not be copied into
    the test. Do not implement any newest-print selection or counters in this double.
    """

    def __init__(self, *, arms: tuple[int, int] = (1, 1), bid: str = "10.00") -> None:
        super().__init__(
            bid,
            trigger=TriggerType.LAST_PRICE,
            breach_count=arms[0],
            high_count=arms[1],
            confirm_gap_ms=100,
        )
        for name, value in vars(Mover(self.settings)).items():
            self.__dict__.setdefault(name, value)
        self.received: dict[InstrumentId, list[TradeTick]] = {}
        self.now = T0 + 30 * SECOND
        self.clock = SimpleNamespace(timestamp_ns=lambda: self.now)
        self.cache.trade = self.cached_trade
        self.cache.trades = self.cached_trades
        self.cache.bars = lambda _: None

    def __getattr__(self, name: str) -> Any:
        return MethodType(getattr(Mover, name), self)

    def _quote(self, _instrument_id: InstrumentId) -> SimpleNamespace:
        return SimpleNamespace(
            bid_price=Price.from_str(self.bid),
            ask_price=Price.from_str(f"{Decimal(self.bid) + Decimal('0.01'):.4f}"),
        )

    def cached_trade(self, instrument: InstrumentId) -> TradeTick | None:
        prints = self.received.get(instrument, [])
        return prints[0] if prints else None

    def cached_trades(self, instrument: InstrumentId) -> list[TradeTick] | None:
        prints = self.received.get(instrument)
        return None if prints is None else list(prints)

    def cache_only(self, tick: TradeTick) -> None:
        self.received.setdefault(tick.instrument_id, []).insert(0, tick)

    def deliver(self, tick: TradeTick) -> None:
        self.cache_only(tick)
        Mover.on_trade(cast(Any, self), tick)

    def open_slot(self, instrument: InstrumentId = AAA) -> Slot:
        Mover._open_slot(cast(Any, self), instrument)
        slot = self.slots[instrument]
        cid = ClientOrderId(f"blind-entry-{instrument.symbol.value}")
        slot.entry_id = cid
        self.orders[cid.value] = SimpleNamespace(client_order_id=cid, avg_px=10.0, is_closed=True)
        self.fills.append((cid.value, "BUY", "5", "10.00", T0))
        return slot

    def subscribe_quotes(self, *args: Any, **kwargs: Any) -> None:
        pass

    def subscribe_trades(self, *args: Any, **kwargs: Any) -> None:
        pass

    def subscribe_bars(self, *args: Any, **kwargs: Any) -> None:
        pass


def read(seam: PrintSeam, slot: Slot, name: str) -> Decimal | None:
    result = getattr(seam, name)(slot)
    return result[0] if name == "_last_known" else result


def symbol_value(record: dict[str, Any], name: str, instrument: InstrumentId) -> Any:
    """P3/P5 name symbols, without specifying whether keys retain the venue suffix."""
    rows = record[name]
    matching = set(rows) & {str(instrument), instrument.symbol.value}
    assert len(matching) == 1, (name, rows, instrument)
    return rows[matching.pop()]


@pytest.mark.parametrize("arms", ARMS, ids=ARM_NAMES)
@pytest.mark.parametrize("reader", READERS)
def test_p1_every_reader_keeps_the_venue_newest(arms: tuple[int, int], reader: str) -> None:
    seam = PrintSeam(arms=arms)
    slot = seam.open_slot()
    seam.deliver(trade("10", 5 * SECOND, 5 * SECOND))
    late = trade("9", SECOND, 6 * SECOND)
    seam.deliver(late)
    assert seam.cached_trade(AAA) == late  # adversarial cache really is last-received
    assert read(seam, slot, reader) == Decimal(10)


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("arms", [(1, 1), (2, 2)], ids=["both-off", "both-on"])
def test_p1_venue_order_does_not_use_the_earlier_of_event_and_receipt(
    arms: tuple[int, int], reader: str
) -> None:
    seam = PrintSeam(arms=arms)
    slot = seam.open_slot()
    # Both timestamps are usable: the first venue timestamp is only two seconds ahead.
    seam.deliver(trade("10", 5 * SECOND, 3 * SECOND))
    seam.deliver(trade("9", 4 * SECOND, 6 * SECOND))
    assert read(seam, slot, reader) == Decimal(10)


@pytest.mark.parametrize("arms", ARMS, ids=ARM_NAMES)
def test_p2_late_print_does_not_cross_a_held_stop(arms: tuple[int, int]) -> None:
    # The lead's ruling of 2026-11-13 (09:27, F1 (b)) replaced P2: a late print of the holding counts
    # toward the hold's low, which the hold's end reads (`_protect`; tests/test_mover_newest_print.py).
    # `_hold` reads the last price, which a late print never becomes, so this case holds as it reads.
    seam = PrintSeam(arms=arms, bid="0")
    slot = seam.open_slot()
    slot.held_since, slot.held_stop = T0, Decimal("9.80")
    seam.deliver(trade("10", 5 * SECOND, 5 * SECOND))
    seam.deliver(trade("9", SECOND, 6 * SECOND))
    seam._hold(slot, Decimal(5), T0 + 6 * SECOND)
    assert seam.sent == [], "a late print went through the held stop"
    assert not slot.exiting and slot.held_stop == Decimal("9.80")


def test_p2_late_print_does_not_meet_the_kept_trigger() -> None:
    seam = PrintSeam()
    slot = seam.open_slot()
    slot.trail_trigger = Decimal("9.80")
    seam.deliver(trade("10", 5 * SECOND, 5 * SECOND))
    seam.deliver(trade("9", SECOND, 6 * SECOND))
    seam._protect(slot, Decimal(5), T0 + 6 * SECOND)
    assert not slot.exiting, "the Mover treated the late print as a reached trigger"
    assert [order.kind for order in seam.sent] == ["TRAILING"]


@pytest.mark.parametrize("newest,late,expected", [("11", "10", True), ("10", "11", False)])
def test_p1_ratchet_uses_the_venue_newest(newest: str, late: str, expected: bool) -> None:
    seam = PrintSeam(bid="10.50")
    slot = seam.open_slot()
    warmed, warm_slot, quote = trail_cases.warmed(mode="ratchet", trigger=TriggerType.LAST_PRICE)
    seam.settings = warmed.settings
    slot.initial_trail = slot.trail_plan = warmed._entry_trail(warm_slot, Decimal(10), quote)
    seam.deliver(trade(newest, 5 * SECOND, 5 * SECOND))
    seam.deliver(trade(late, SECOND, 6 * SECOND))
    assert seam._ratchet(slot, T0 + 6 * SECOND) is expected
    assert slot.ratcheted is expected


@pytest.mark.parametrize("arms", ARMS, ids=ARM_NAMES)
def test_p1_sale_without_bid_uses_the_venue_newest(arms: tuple[int, int]) -> None:
    seam = PrintSeam(arms=arms, bid="0")
    slot = seam.open_slot()
    seam.deliver(trade("10", 5 * SECOND, 3 * SECOND))
    seam.deliver(trade("9", 4 * SECOND, 6 * SECOND))
    seam._limit_sell(slot, Decimal(5), T0 + 6 * SECOND, None)
    assert seam.sent[-1].price.as_decimal() == Decimal("9.99")


@pytest.mark.parametrize("lateness,rounded_ms", [(1, 1), (MS - 1, 1), (MS, 1), (MS + 1, 2)])
def test_p3_lateness_is_rounded_up_to_whole_milliseconds(lateness: int, rounded_ms: int) -> None:
    seam = PrintSeam()
    seam.open_slot()
    seam.deliver(trade("10", 5 * SECOND, 5 * SECOND))
    seam.deliver(trade("9", 5 * SECOND - lateness, 6 * SECOND))
    assert symbol_value(seam.record(), "late_prints_set_aside", AAA) == {
        "count": 1,
        "most_late_ms": rounded_ms,
    }


def test_p3_counts_and_maximum_are_independent_by_symbol() -> None:
    seam = PrintSeam(arms=(2, 2))
    seam.open_slot(AAA)
    seam.open_slot(BBB)
    seam.deliver(trade("10", 5 * SECOND, 5 * SECOND, AAA))
    seam.deliver(trade("20", 7 * SECOND, 7 * SECOND, BBB))
    for index, lateness in enumerate((1, MS + 1, MS)):
        seam.deliver(trade("9", 5 * SECOND - lateness, (8 + index) * SECOND, AAA))
    seam.deliver(trade("19", 7 * SECOND - 1, 11 * SECOND, BBB))
    # A later usable print, a tie, and repeated reads are not extra set-aside prints.
    seam.deliver(trade("11", 12 * SECOND, 12 * SECOND, AAA))
    seam.deliver(trade("11.01", 12 * SECOND, 13 * SECOND, AAA))
    for reader in READERS:
        read(seam, seam.slots[AAA], reader)
    record = seam.record()
    assert symbol_value(record, "late_prints_set_aside", AAA) == {"count": 3, "most_late_ms": 2}
    assert symbol_value(record, "late_prints_set_aside", BBB) == {"count": 1, "most_late_ms": 1}


@pytest.mark.parametrize("arms", ARMS, ids=ARM_NAMES)
@pytest.mark.parametrize("ahead_in_cache", [False, True])
def test_p4_open_slot_seeds_from_all_cached_prints(
    arms: tuple[int, int], ahead_in_cache: bool
) -> None:
    seam = PrintSeam(arms=arms)
    seam.cache_only(trade("10", 5 * SECOND, 5 * SECOND))
    seam.cache_only(trade("9", SECOND, 6 * SECOND))
    if ahead_in_cache:
        seam.cache_only(trade("99", 20 * SECOND, 7 * SECOND))
    slot = seam.open_slot()
    # Another late delivery must not erase the seed either.
    seam.deliver(trade("8", 2 * SECOND, 8 * SECOND))
    assert tuple(read(seam, slot, reader) for reader in READERS) == (Decimal(10),) * 3


@pytest.mark.parametrize("arms", ARMS, ids=ARM_NAMES)
@pytest.mark.parametrize("reader", READERS)
def test_p5_over_five_seconds_ahead_is_not_a_usable_price(
    arms: tuple[int, int], reader: str
) -> None:
    seam = PrintSeam(arms=arms)
    slot = seam.open_slot()
    seam.deliver(trade("10", SECOND, SECOND))
    seam.deliver(trade("99", 7 * SECOND + 1, 2 * SECOND))
    assert read(seam, slot, reader) == Decimal(10)


def test_p5_exactly_five_seconds_ahead_is_usable_and_orders_by_venue_time() -> None:
    seam = PrintSeam(arms=(2, 2))
    slot = seam.open_slot()
    seam.deliver(trade("10", 6 * SECOND, SECOND))
    seam.deliver(trade("9", 2 * SECOND, 2 * SECOND))
    assert tuple(read(seam, slot, reader) for reader in READERS) == (Decimal(10),) * 3


def test_p5_ahead_counts_are_separate_from_lateness_and_per_symbol() -> None:
    seam = PrintSeam()
    seam.open_slot(AAA)
    seam.open_slot(BBB)
    for instrument in (AAA, BBB):
        seam.deliver(trade("10", SECOND, SECOND, instrument))
        seam.deliver(trade("99", 7 * SECOND + 1, 2 * SECOND, instrument))
        seam.deliver(trade("11", 3 * SECOND, 3 * SECOND, instrument))
    seam.deliver(trade("98", 10 * SECOND, 4 * SECOND, AAA))
    record = seam.record()
    for instrument, expected in ((AAA, 2), (BBB, 1)):
        counted = symbol_value(record, "prints_stamped_ahead", instrument)
        # P5 specifies a count per symbol, but does not prescribe its scalar/row representation.
        assert (counted if isinstance(counted, int) else counted["count"]) == expected
    late = record.get("late_prints_set_aside", {})
    assert all(row["count"] == 0 for row in late.values()), late


@pytest.mark.parametrize("ahead", [5 * SECOND, 5 * SECOND + 1], ids=["usable", "unusable"])
def test_p2_p5_persistence_keeps_its_receipt_clamped_watermark(ahead: int) -> None:
    seam = PrintSeam(arms=(2, 2))
    slot = seam.open_slot()
    seam._persistent_trail(slot, T0)
    seam.deliver(trade("10", SECOND + ahead, SECOND))
    seam.deliver(trade("9.70", 2 * SECOND, 2 * SECOND))
    seam.deliver(trade("9.60", 2 * SECOND + 200 * MS, 2 * SECOND + 200 * MS))
    seam._persistent_trail(slot, T0 + 3 * SECOND)
    assert slot.exiting and slot.breach_price == Decimal("9.60")
    assert slot.observed_ns == T0 + 2 * SECOND + 200 * MS


@pytest.mark.parametrize(
    "event,received,usable",
    [(3 * SECOND, SECOND, True), (SECOND, 3 * SECOND, False)],
    ids=["venue-after-fill-receipt-before", "venue-before-fill-receipt-after"],
)
def test_p6_holding_trade_compares_venue_time_to_the_entry_fill(
    event: int, received: int, usable: bool
) -> None:
    seam = PrintSeam(arms=(2, 2))
    slot = seam.open_slot()
    assert slot.entry_id is not None
    seam.fills[:] = [(slot.entry_id.value, "BUY", "5", "10.00", T0 + 2 * SECOND)]
    seam.deliver(trade("10.25", event, received))
    # P6 explicitly names this seam; absence at the base is an intentional failing assertion path.
    found = seam._holding_trade(slot)
    assert (found is not None) is usable


def test_p7_ordinary_prints_ties_and_no_print_minute_fallback() -> None:
    seam = PrintSeam()
    slot = seam.open_slot()
    slot.closes.extend([Decimal("8.50"), Decimal("9.50")])
    # the trade-print trigger unit (code-review-high-succ/3): under a print trigger, with no print, a
    # minute close is no price of this holding, so neither the held stop nor the no-bid sale reads one;
    # the minute-close fallback under the bid is in test_mover_newest_print.py
    assert seam._last_known(slot) == (None, "none")
    assert seam._last_print(slot) is None
    for price, event, received in (("10", SECOND, SECOND), ("11", 2 * SECOND, 2 * SECOND)):
        seam.deliver(trade(price, event, received))
        assert tuple(read(seam, slot, reader) for reader in READERS) == (Decimal(price),) * 3
    seam.deliver(trade("11.01", 2 * SECOND, 3 * SECOND))
    assert tuple(read(seam, slot, reader) for reader in READERS) == (Decimal("11.01"),) * 3


@pytest.mark.parametrize(
    "existing_case",
    [
        trail_cases.test_a_print_through_the_held_stop_sells_at_once_and_names_its_source,
        breach_cases.test_both_off_submit_the_original_emulated_orders,
        trail_cases.test_ratchet_remakes_the_emulated_sale_and_records_when_and_from_what,
    ],
    ids=["reader", "breach", "ratchet"],
)
def test_p7_existing_cases_unchanged(existing_case: Callable[[], None]) -> None:
    existing_case()


def test_p8_prints_leave_f2_atr_history_counts_and_warm_up_record_unchanged() -> None:
    strategy, slot, _ = trail_cases.warmed()
    assert slot.atr is not None and slot.atr.initialized
    before_atr = (slot.atr.value, slot.atr.initialized, slot.atr_warm_bars)
    before_history = {
        key: deepcopy(value) for key, value in vars(strategy).items() if key.startswith("history_")
    }
    before_warm_up = deepcopy(strategy.record()["warm_up"])
    for tick in (
        trade("10", 5 * SECOND, 5 * SECOND),
        trade("9", SECOND, 6 * SECOND),
        trade("99", 20 * SECOND, 7 * SECOND),
    ):
        strategy.on_trade(tick)
    assert (slot.atr.value, slot.atr.initialized, slot.atr_warm_bars) == before_atr
    assert {key: value for key, value in vars(strategy).items() if key.startswith("history_")} == (
        before_history
    )
    assert strategy.record()["warm_up"] == before_warm_up
