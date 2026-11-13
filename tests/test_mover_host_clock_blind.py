"""Blind C1-C4 at 5f259317: venue ordering and holding exits survive host clock offsets.

All prices, fills and timestamps are SHAPED. Offsets mean host minus venue, with no feed
delay except the explicitly delayed pre-entry seed and the deliberately late print.
Only clock, cache and order transport are doubles; decisions use test_mover.Seam's Mover
methods. Window assembly uses test_session_window.quiet_window and pytest's tmp_path.

Contracts checked at NautilusTrader d53849b216e2:
* crates/model/src/data/trade.rs:44-58,71-89: ts_event is event time, ts_init creation time;
  the test supplies host receipt time as creation time, independently of venue event time.
* python/nautilus_trader/common/__init__.pyi:625-634 and
  crates/common/src/python/clock.rs:61-64,91-101,363-366: Clock.new_test/set_time/timestamp_ns.
* crates/common/src/cache/mod.rs:6362-6373: last added, not venue-sorted, cached trade.
* crates/data/src/engine/dispatch.rs:117-128: cache the trade before its callback.

C3 names no new diagnostic schema. Its magnitude test compares otherwise identical window
run records at two offsets in the same ahead-count bucket, requiring a distinguishing named
field without inventing its spelling or units. The separate ahead test interprets "stamped
ahead" literally as ts_event > ts_init, including the -1 s host offset.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from pathlib import Path
from types import MethodType
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.common import Clock
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from canon_trading import session_window
from canon_trading.mover import HOLD_MOST_SECS
from canon_trading.mover import Mover
from tests.test_mover import AAA
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import seam_slot
from tests.test_session_window import Evening
from tests.test_session_window import Quoted
from tests.test_session_window import a_pick
from tests.test_session_window import quiet_window

OFFSETS = [-60, -6, -1, 0, 1, 6, 60]
OFFSET_IDS = [f"host{seconds:+d}s" for seconds in OFFSETS]
FILLED = T0 + 120 * SECOND
HELD = Decimal(3)
STOP = Decimal("9.80")


class ClockSeam(Seam):
    """Existing sale seam with real initial Mover state and a pinned virtual host clock."""

    def __init__(
        self,
        offset: int,
        *,
        trigger: TriggerType | None = TriggerType.LAST_PRICE,
        bid: str = "9.99",
        ask: str = "10.01",
    ) -> None:
        rows: dict[str, Any] = {} if trigger is None else {"trigger": trigger}
        super().__init__(bid, **rows)
        for name, value in vars(Mover(self.settings)).items():
            self.__dict__.setdefault(name, value)
        self.offset_ns = offset * SECOND
        self.clock = Clock.new_test()
        self.clock.set_time(T0 + self.offset_ns)
        self.received: list[TradeTick] = []
        self.sent_ns: list[int] = []
        self.slot = seam_slot(self)
        self.slot.closes = deque()
        self.slot.dollars = deque()
        self.slots = {AAA: self.slot}
        self.fills = [("E-1", OrderSide.BUY.name, str(HELD), "10.00", FILLED)]
        self.orders["E-1"].client_order_id = self.slot.entry_id
        self.orders["E-1"].ts_accepted = FILLED
        self.cache.trade = lambda _id: self.received[0] if self.received else None
        self.cache.trades = lambda _id: list(self.received)
        self.cache.bars = lambda _id: None
        self.latest_quote = self.quote_tick(bid, ask, T0)
        # Not the blind tester's: the builder (L6) delivers this quote through the strategy's quote
        # handler, as the stream would. Without a quote of the name the venue's time cannot be had,
        # and the old test against receipt applies by the unit's contract (case c).
        Mover.on_quote(cast(Any, self), self.latest_quote)

    def __getattr__(self, name: str) -> Any:
        # Future helpers still resolve to Mover, rather than being reimplemented in the test.
        return MethodType(getattr(Mover, name), self)

    def _quote(self, _instrument_id: InstrumentId) -> Any:
        return self.latest_quote

    def quote_tick(self, bid: str, ask: str, venue_ns: int) -> QuoteTick:
        return QuoteTick(
            AAA,
            Price.from_str(bid),
            Price.from_str(ask),
            Quantity.from_int(1000),
            Quantity.from_int(1000),
            venue_ns,
            venue_ns + self.offset_ns,
        )

    def quote(self, bid: str, ask: str, venue_ns: int) -> None:
        self.clock.set_time(venue_ns + self.offset_ns)
        self.latest_quote = self.quote_tick(bid, ask, venue_ns)
        Mover.on_quote(cast(Any, self), self.latest_quote)

    def deliver(self, price: str, event: int, arrival: int | None = None) -> TradeTick:
        """`arrival` is venue time when received; ts_init and clock use the offset host."""
        received = (event if arrival is None else arrival) + self.offset_ns
        self.clock.set_time(received)
        trade = TradeTick(
            AAA,
            Price.from_str(price),
            Quantity.from_int(100),
            AggressorSide.SELL,
            TradeId(f"clock-{len(self.received)}"),
            event,
            received,
        )
        self.received.insert(0, trade)
        Mover.on_trade(cast(Any, self), trade)
        return trade

    def seed_before_entry(self) -> None:
        # A delayed pre-entry print is usable even at -60 s. This prevents the base's
        # no-kept-print cache fallback from concealing its stale-kept-print failure.
        self.deliver("10.00", T0, FILLED - SECOND)

    def protect(self, venue_ns: int) -> None:
        self.clock.set_time(venue_ns + self.offset_ns)
        self._protect(self.slot, HELD, self.clock.timestamp_ns())

    def _limit(self, _id: Any, _side: Any, qty: Any, price: Any, **_kw: Any) -> SimpleNamespace:
        order = super()._limit(_id, _side, qty, price, **_kw)
        order.side = _side
        return order

    def submit_order(self, order: SimpleNamespace, params: Any = None) -> None:
        super().submit_order(order, params)
        self.sent_ns.append(self.clock.timestamp_ns())

    def record(self) -> dict[str, Any]:
        return Mover.record(cast(Any, self))


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
@pytest.mark.parametrize("recovers", [False, True], ids=["stays-breached", "recovers-before-step"])
def test_c1_last_price_breach_sells_within_the_hold(offset: int, recovers: bool) -> None:
    seam = ClockSeam(offset)
    seam.seed_before_entry()
    seam.protect(FILLED + SECOND)
    started = seam.clock.timestamp_ns()
    assert seam.slot.held_stop == STOP and not seam.sent

    seam.quote("9.69", "9.71", FILLED + 2 * SECOND)
    seam.deliver("9.70", FILLED + 2 * SECOND)
    if recovers:
        seam.quote("9.89", "9.91", FILLED + 2 * SECOND + SECOND // 2)
        seam.deliver("9.90", FILLED + 2 * SECOND + SECOND // 2)

    # Drive protection steps through the hold's own bound; never invoke the wind-down.
    for elapsed in range(2, HOLD_MOST_SECS + 1):
        if seam.sent:
            break
        seam.protect(FILLED + SECOND + elapsed * SECOND)

    assert seam.sent, f"host offset {offset}s left a breached holding unsold for its hold bound"
    assert seam.sent[0].kind == "LIMIT" and seam.sent[0].side == OrderSide.SELL
    assert seam.sent[0].quantity.as_decimal() == HELD
    assert started <= seam.sent_ns[0] <= started + HOLD_MOST_SECS * SECOND
    assert seam.slot.exiting and not seam.winding_down
    assert seam.slot.exit_record is not None
    reason = seam.slot.exit_record.trigger_reason
    assert reason is not None and "held stop" in reason


def read_price(seam: ClockSeam, reader: str) -> Decimal | None:
    result = getattr(seam, reader)(seam.slot)
    return result[0] if reader == "_last_known" else result


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
@pytest.mark.parametrize("reader", ["_followed", "_last_known", "_last_print"])
def test_c2_new_prints_and_late_prints_are_ordered_only_by_venue_time(
    offset: int, reader: str
) -> None:
    seam = ClockSeam(offset)
    seam.seed_before_entry()
    seam.deliver("10.20", FILLED + 2 * SECOND)
    assert read_price(seam, reader) == Decimal("10.20"), "host offset rejected a newer print"

    late = seam.deliver("9.00", FILLED + SECOND, FILLED + 3 * SECOND)
    assert seam.cache.trade(AAA) == late, "the adversarial cache must hold the late print"
    assert read_price(seam, reader) == Decimal("10.20"), "a venue-older print moved a reader"
    assert seam.record()["late_prints_set_aside"][str(AAA)] == {
        "count": 1,
        "most_late_ms": 1000,
    }

    # Equal venue stamps retain the existing arrival-order tie rule.
    seam.deliver("10.30", FILLED + 2 * SECOND, FILLED + 4 * SECOND)
    assert read_price(seam, reader) == Decimal("10.30")
    seam.deliver("10.40", FILLED + 5 * SECOND)
    assert read_price(seam, reader) == Decimal("10.40")
    assert not seam.sent


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
def test_c2_a_venue_older_breach_of_the_holding_sells_a_hold_at_any_offset(offset: int) -> None:
    # Amended for the lead's ruling of 2026-11-13 (09:27, F1 (b)), which replaced the spec line this
    # case was written from: a print of the holding set aside as late counts toward the hold's low,
    # so the hold's end sells, at every offset alike. It never becomes the newest price: the
    # followed price stays the 9.90. The tester asserted a trail at the stop.
    seam = ClockSeam(offset, bid="9.89", ask="9.91")
    seam.slot.held_since = FILLED + SECOND + seam.offset_ns
    seam.slot.held_stop = STOP
    seam.deliver("9.90", FILLED + 3 * SECOND)
    seam.deliver("9.70", FILLED + 2 * SECOND, FILLED + 4 * SECOND)
    assert seam.slot.newest_print is not None
    assert seam.slot.newest_print.price == Decimal("9.90")
    seam.protect(FILLED + 5 * SECOND)
    assert seam.slot.exiting
    assert [order.kind for order in seam.sent] == ["LIMIT"]
    row = seam.slot.exit_record
    assert row is not None and row.trigger_reason == "held stop (followed price)"


def recorded_window(monkeypatch: pytest.MonkeyPatch, root: Path, offset: int) -> dict[str, Any]:
    """Real window JSON assembly, using only the existing offline window transport seam."""
    root.mkdir()
    seam = ClockSeam(offset)
    seam.deliver("10.00", FILLED + 2 * SECOND)
    seam.deliver("10.10", FILLED + 3 * SECOND)
    # Match quiet_window's published-generation premise in the state whose record is emitted.
    seam.ranking_records.append({"published": True, "value": ""})

    class ReportingMover(Mover):
        def record(self) -> dict[str, Any]:
            return seam.record()

    def unexpected_client(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("the offline window unexpectedly requested an external client")

    monkeypatch.setenv(session_window.ACCOUNT_ENV, "windows")
    monkeypatch.delenv(session_window.BY_HAND_ENV, raising=False)
    monkeypatch.setattr(session_window, "Mover", ReportingMover)
    monkeypatch.setattr(session_window, "choose_mover", lambda *a, **k: a_pick())
    monkeypatch.setattr(session_window, "window_data", lambda: Quoted())
    monkeypatch.setattr(session_window, "window_screener", unexpected_client)
    monkeypatch.setattr(session_window, "window_stream", unexpected_client)
    monkeypatch.setattr(session_window, "window_actions", unexpected_client)
    outcome, latest, _ = quiet_window(
        monkeypatch,
        root,
        quotes=2,
        trades=2,
        account=Evening(17),
        session="extended",
        strategy="Mover",
        mover=True,
        trigger="last_price",
    )
    assert not isinstance(outcome, Exception), outcome
    assert latest["result"] == "ok"
    assert (root / "latest.json").is_file()
    return latest["mover"]


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
def test_c3_window_records_the_count_of_prints_stamped_ahead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, references_in: None, offset: int
) -> None:
    # Not the blind tester's: amended by the builder (L6) to the unit's contract. Within the minute
    # the lead allowed, no print is "ahead" (no usable time) at all, and the window names the clocks'
    # difference beside the run once it passes 2 s (the lead's rule, 08:40). The tester had counted
    # every print with ts_event > ts_init as ahead, and named that reading UNVERIFIED in its report.
    window = recorded_window(monkeypatch, tmp_path / "window", offset)
    assert window["run"]["prints_stamped_ahead"].get(str(AAA), 0) == 0
    assert "prints_stamped_ahead" not in window
    seen = window["run"]["host_clock"]["venue_minus_receipt_ms"]
    assert seen["median"] == -offset * 1000
    assert ("host_clock" in window) is (abs(offset) > 2)


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
def test_c3_window_names_observed_clock_difference_beyond_an_ahead_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, references_in: None, offset: int
) -> None:
    # Same ahead-count category, different magnitude; no raw-price/order/fill difference.
    other_offset = offset - 1 if offset < 0 else offset + 1
    first = recorded_window(monkeypatch, tmp_path / "first", offset)["run"]
    second = recorded_window(monkeypatch, tmp_path / "second", other_offset)["run"]
    count_fields = {"prints_stamped_ahead", "late_prints_set_aside"}
    differing_fields = {
        name
        for name in first.keys() | second.keys()
        if name not in count_fields and first.get(name) != second.get(name)
    }
    assert differing_fields, (
        f"window records cannot distinguish observed host offsets {offset}s and {other_offset}s; "
        "C3 requires a named observation of their difference from venue stamps"
    )


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
def test_c4_default_bid_ask_still_arms_from_the_quote(offset: int) -> None:
    seam = ClockSeam(offset, trigger=None)
    assert seam.settings.trigger == TriggerType.BID_ASK
    seam.seed_before_entry()
    seam.deliver("9.00", FILLED + 2 * SECOND)
    assert seam._followed(seam.slot) == Decimal("9.99")
    seam.protect(FILLED + 3 * SECOND)
    assert [order.kind for order in seam.sent] == ["TRAILING"]
    assert seam.sent[0].quantity.as_decimal() == HELD
    assert not seam.slot.exiting and not seam.winding_down


@pytest.mark.parametrize("offset", OFFSETS, ids=OFFSET_IDS)
def test_c4_default_bid_ask_keeps_a_quote_breach_between_steps(offset: int) -> None:
    seam = ClockSeam(offset, trigger=None)
    assert seam.settings.trigger == TriggerType.BID_ASK
    seam.slot.held_since = FILLED + SECOND + seam.offset_ns
    seam.slot.held_stop = STOP
    seam.quote("9.70", "9.72", FILLED + 2 * SECOND)
    seam.quote("9.90", "9.92", FILLED + 2 * SECOND + SECOND // 2)
    seam.protect(FILLED + 3 * SECOND)
    assert [order.kind for order in seam.sent] == ["LIMIT"]
    assert seam.sent[0].side == OrderSide.SELL
    assert seam.sent[0].quantity.as_decimal() == HELD
    assert seam.slot.exiting and not seam.winding_down
    assert seam.slot.exit_record is not None
    reason = seam.slot.exit_record.trigger_reason
    assert reason is not None and "held stop" in reason
