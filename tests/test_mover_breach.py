"""SHAPED persistence tapes on the real BacktestEngine: invented AAA.ALPACA prices and T0 offsets.

Contract: nt@d53849b216e2 Strategy.on_trade, Clock timers, Cache.trade, TradeTick.ts_event/ts_init,
and OrderFactory.limit. The shared engine, strategy, ranking and tape helpers are test_mover's.
The on row is N=2 for each arm and G=100 ms, UNCALIBRATED; these tapes establish behavior only.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from canon_trading.ledger import _strategy_fields
from canon_trading.mover import STEP_SECS
from canon_trading.mover import Mover
from canon_trading.mover import chase_floor
from canon_trading.mover import offsets
from canon_trading.price_step import exit_price
from canon_trading.tuning_study import BarsReplayMover
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import MOVER_ID
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Borrowed
from tests.test_mover import Recording
from tests.test_mover import Seam
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover import tape
from tests.test_mover import trade_tape

MS = 1_000_000
START = 4 * MINUTE + 4 * SECOND
LEVEL = Decimal("10.29")
HIGH = Decimal("10.50")
CONFIRMED = 4 * MINUTE + 2 * SECOND
"""When the first of the prefix's three prints that confirm `HIGH` is received."""


def print_at(offset: int, price: str, *, received: int | None = None, size: int = 100) -> TradeTick:
    return TradeTick(
        AAA,
        Price.from_str(f"{Decimal(price):.4f}"),
        Quantity.from_int(size),
        AggressorSide.SELL,
        TradeId(f"shaped-{offset}-{received}"),
        T0 + offset,
        T0 + (offset if received is None else received),
    )


def quote_at(
    offset: int,
    bid: str = "10.34",
    ask: str = "10.36",
    *,
    size: int = 10_000,
    received: int | None = None,
):
    return QuoteTick(
        AAA,
        Price.from_str(f"{Decimal(bid):.4f}"),
        Price.from_str(f"{Decimal(ask):.4f}"),
        Quantity.from_int(size),
        Quantity.from_int(size),
        T0 + offset,
        T0 + (offset if received is None else received),
    )


def prefix(skew: int = 0, *, cached: str = "10.50", confirm: bool = True) -> list:
    """Four rising closes buy ten shares at 10.31, and the level is armed from that fill. Three prints
    at 10.50, 200 ms apart and after the arming step, then confirm the 10.50 high for any count up to
    three, which puts the level at 10.29. The print cached before arming is never the level. `skew`
    moves each confirming print's event time against its receipt."""
    closes = [10.0, 10.1, 10.2, 10.3]
    rows = [
        quote_at(0, "9.99", "10.01"),
        *tape(AAA, closes),
        *trade_tape(AAA, closes),
        quote_at(4 * MINUTE + 250 * MS, "10.29", "10.31"),
        quote_at(4 * MINUTE + SECOND + 250 * MS, "10.29", "10.31"),
        print_at(4 * MINUTE + 500 * MS, cached),
    ]
    if confirm:
        rows += [
            print_at(CONFIRMED + k * 200 * MS + skew, "10.50", received=CONFIRMED + k * 200 * MS)
            for k in range(3)
        ]
    rows.append(quote_at(4 * MINUTE + 3 * SECOND))
    return rows


def plain_state(slot) -> tuple:
    return (
        slot.trail_high,
        slot.trail_trigger,
        slot.exiting,
        tuple(slot.high_observations),
        tuple(slot.breach_observations),
        slot.observed_ns,
    )


class ObservedMover(Recording):
    """Observe the strategy's real steps, submissions and plain handler state without replacing them."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self.states: list[dict] = []
        self.submissions: list[tuple] = []
        self.deliveries: list[tuple] = []
        self.exit_steps: list[int] = []
        self.exit_step_sends: list[int] = []
        self.stop_offset: int | None = None
        self.stop_exiting = False
        self.stop_requested_at: int | None = None
        self.flat_at_stop = False

    def on_start(self) -> None:
        super().on_start()
        if self.stop_offset is not None:
            self.clock.set_time_alert("shaped-window-end", at(self.stop_offset), self._request_stop)

    def _request_stop(self, event) -> None:
        self.stop_requested_at = event.ts_event
        self.flat_at_stop = not self.cache.positions_open(strategy_id=MOVER_ID)
        self.stop()
        self.stop_exiting = self.is_exiting()

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submissions.append(
            (
                order.order_type.name,
                order.side.name,
                order.quantity.as_decimal(),
                None if order.price is None else order.price.as_decimal(),
                getattr(order, "trailing_offset", None),
                getattr(order, "limit_offset", None),
                order.emulation_trigger,
                order.time_in_force,
                params,
            )
        )
        super().submit_order(order, position_id, client_id, params)

    def on_trade(self, trade) -> None:
        slot = self.slots.get(trade.instrument_id)
        watching = slot is not None and getattr(slot, "trail_high", None) is not None
        before = plain_state(slot) if watching else None
        super().on_trade(trade)
        if watching:
            self.deliveries.append(
                (trade.ts_event, before, plain_state(slot), tuple(slot.observations))
            )

    def _on_step(self, event) -> None:
        exiting = self.is_exiting()
        before = len(self.submissions)
        if exiting:
            self.exit_steps.append(event.ts_event)
        super()._on_step(event)
        if exiting:
            self.exit_step_sends.append(len(self.submissions) - before)
        for slot in self.slots.values():
            self.states.append(
                {
                    "stamp": event.ts_event,
                    "level": slot.trail_trigger,
                    "high": getattr(slot, "trail_high", None),
                    "floor": slot.floor,
                    "breach_price": getattr(slot, "breach_price", None),
                    "breaches": tuple(getattr(slot, "breach_observations", ())),
                    "highs": tuple(getattr(slot, "high_observations", ())),
                    "queued": tuple(getattr(slot, "observations", ())),
                    "watermark": getattr(slot, "observed_ns", None),
                }
            )


def at(offset: int) -> datetime:
    return datetime.fromtimestamp((T0 + offset) / SECOND, tz=UTC)


def replay(
    data: list,
    *,
    end: int = START + 4 * SECOND,
    skew: int = 0,
    cached: str = "10.50",
    confirm: bool = True,
    stop_offset: int | None = None,
    liquid: bool = False,
    persistence: bool = True,
    **changes: Any,
) -> tuple[ObservedMover, list, Decimal]:
    rows: dict[str, Any] = {
        "max_shares": 10,
        "entries": 1,
        "watch": 1,
        "trigger": TriggerType.LAST_PRICE,
    }
    if persistence:
        rows.update(breach_count=2, high_count=2, confirm_gap_ms=100)
    rows.update(changes)
    s = ObservedMover(strategy(**rows).settings)
    s.stop_offset = stop_offset
    eng = engine(liquid=liquid)
    try:
        orders = run(
            eng,
            s,
            ranker(rank((AAA, "0.1"))),
            prefix(skew, cached=cached, confirm=confirm),
            data,
            [quote_at(end)],
        )
        held = sum(
            (p.signed_qty for p in eng.cache.positions_open(strategy_id=MOVER_ID)), Decimal(0)
        )
        buys = [o for o in orders if o.side == OrderSide.BUY]
        assert len(buys) == 1 and buys[0].filled_qty.as_decimal() == 10
        assert Decimal(str(buys[0].avg_px)) == Decimal("10.31")
        return s, orders, held
    finally:
        eng.dispose()


def sells(orders: list) -> list:
    """Every sale, including canceled, denied and unfilled orders."""
    return [o for o in orders if o.side == OrderSide.SELL]


def state_at(s: ObservedMover, offset: int) -> dict:
    return [state for state in s.states if state["stamp"] <= T0 + offset][-1]


def test_both_off_submit_the_original_emulated_orders() -> None:
    data = [
        print_at(START + 100 * MS, "10.60"),
        quote_at(START + 150 * MS, "10.59", "10.61"),
        print_at(START + SECOND + 100 * MS, "10.25"),
        quote_at(START + SECOND + 150 * MS, "10.24", "10.26"),
        quote_at(START + 2 * SECOND, "10.24", "10.26"),
    ]
    explicit, orders, _ = replay(data, breach_count=1, high_count=1, trigger=TriggerType.BID_ASK)
    baseline, original, _ = replay(data, persistence=False, trigger=TriggerType.BID_ASK)
    assert explicit.submissions == baseline.submissions
    assert [(o.order_type.name, o.status.name, o.price) for o in orders] == [
        (o.order_type.name, o.status.name, o.price) for o in original
    ]
    assert [row[:2] for row in explicit.submissions] == [
        ("LIMIT", "BUY"),
        ("TRAILING_STOP_LIMIT", "SELL"),
    ]
    assert explicit.submissions[1][4:8] == (
        Decimal("0.21"),
        Decimal("0.27"),
        TriggerType.BID_ASK,
        TimeInForce.DAY,
    )
    assert orders[-1].order_type.name == "LIMIT" and orders[-1].filled_qty.as_decimal() == 10


@pytest.mark.parametrize("count", [2, 3])
def test_only_the_nth_counted_breach_sends_a_sale(count: int) -> None:
    first = START + 100 * MS
    data = [print_at(first + i * SECOND, "10.29") for i in range(count)]
    s, orders, held = replay(data, breach_count=count, end=first + (count + 1) * SECOND)
    confirming = T0 + first + (count - 1) * SECOND
    assert held == 0 and sells(orders)
    assert all(o.ts_init > confirming for o in sells(orders))
    assert state_at(s, first + (count - 1) * SECOND)["breaches"] == tuple(
        T0 + first + i * SECOND for i in range(count - 1)
    )


def test_prints_closer_than_the_gap_count_once_then_the_gap_endpoint_confirms() -> None:
    first = START + 100 * MS
    near = [print_at(first, "10.28"), print_at(first + 99 * MS, "10.28")]
    _, early, held = replay(near)
    assert held == 10 and sells(early) == []
    _, orders, held = replay([*near, print_at(first + 100 * MS, "10.28")])
    assert held == 0 and sells(orders)
    assert all(o.ts_init > T0 + first + 100 * MS for o in sells(orders))


def test_zero_gap_counts_two_prints_a_microsecond_apart() -> None:
    first = START + 100 * MS
    _, orders, held = replay(
        [print_at(first, "10.28"), print_at(first + 1_000, "10.28")], confirm_gap_ms=0
    )
    assert held == 0 and sells(orders)


@pytest.mark.parametrize("reset_ms", [50, 150], ids=["uncounted-recovery", "spaced-recovery"])
def test_any_recovery_resets_the_breach_even_inside_the_gap(reset_ms: int) -> None:
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.28"),
            print_at(first + reset_ms * MS, "10.35"),
            print_at(first + 200 * MS, "10.28"),
        ]
    )
    assert held == 10 and sells(orders) == []
    assert s.states[-1]["breaches"] == (T0 + first + 200 * MS,)


def test_an_odd_lot_counts_and_resets_like_any_other_print() -> None:
    """One counted set for both directions (the lead, 2026-11-12): the count reads no size, so a
    one-share print confirms a breach and a one-share print above the level resets it."""
    first = START + 100 * MS
    _, orders, held = replay(
        [print_at(first, "10.28", size=1), print_at(first + 100 * MS, "10.28", size=1)]
    )
    assert sells(orders) and held == 0
    s, orders, held = replay(
        [
            print_at(first, "10.28"),
            print_at(first + 50 * MS, "10.35", size=1),
            print_at(first + 100 * MS, "10.28"),
        ]
    )
    assert sells(orders) == [] and held == 10
    assert s.states[-1]["breaches"] == (T0 + first + 100 * MS,)


def test_two_breaches_sixty_seconds_apart_have_no_expiry_and_sell_next_step() -> None:
    first = START + 100 * MS
    confirming = first + 60 * SECOND
    _, orders, held = replay(
        [print_at(first, "10.28"), print_at(confirming, "10.28")],
        end=confirming + 2 * SECOND,
    )
    assert held == 0 and sells(orders)
    assert min(o.ts_init for o in sells(orders)) - (T0 + confirming) == 900 * MS


@pytest.mark.parametrize("shape", ["one", "sweep", "confirmed"])
def test_a_new_high_needs_spaced_prints_and_uses_the_lowest(shape: str) -> None:
    first = START + 100 * MS
    data = [print_at(first, "10.70")]
    expected = LEVEL
    if shape == "sweep":
        data = [
            print_at(first + k * 40_000, str(Decimal("10.70") + k * Decimal("0.001")))
            for k in range(28)
        ]
    elif shape == "confirmed":
        data.append(print_at(first + 100 * MS, "10.6037"))
        expected = Decimal("10.39")
    s, orders, held = replay(data)
    assert held == 10 and sells(orders) == []
    assert s.states[-1]["level"] == expected
    assert all(
        state["level"] == LEVEL
        for state in s.states
        if T0 + 4 * MINUTE + 3 * SECOND <= state["stamp"] <= T0 + first
    )
    if shape == "confirmed":
        assert s.states[-1]["high"] == Decimal("10.6037")


@pytest.mark.parametrize("reset_ms", [50, 150])
def test_a_print_at_the_confirmed_high_resets_the_high_arm(reset_ms: int) -> None:
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.70"),
            print_at(first + reset_ms * MS, "10.50"),
            print_at(first + 200 * MS, "10.60"),
        ]
    )
    assert held == 10 and sells(orders) == []
    assert s.states[-1]["high"] == HIGH and s.states[-1]["level"] == LEVEL


def test_the_high_arm_has_no_expiry_and_never_lowers_the_level() -> None:
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.70"),
            print_at(first + 60 * SECOND, "10.60"),
            print_at(first + 61 * SECOND, "10.55"),
        ],
        end=first + 63 * SECOND,
    )
    assert held == 10 and sells(orders) == []
    levels = [state["level"] for state in s.states if state["level"] is not None]
    assert levels == sorted(levels) and levels[-1] == Decimal("10.39")


def test_the_off_high_arm_follows_each_newer_high_when_only_breach_is_on() -> None:
    first = START + 100 * MS
    s, _, held = replay([print_at(first, "10.60"), print_at(first + 1_000, "10.70")], high_count=1)
    assert held == 10 and s.states[-1]["level"] == Decimal("10.49")


@pytest.mark.parametrize("bid", ["10.10", "10.90"], ids=["bid-lower", "bid-recovers"])
def test_confirmation_latches_and_the_floor_uses_the_lower_of_print_and_bid(bid: str) -> None:
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.28"),
            print_at(first + 100 * MS, "10.27"),
            print_at(first + 200 * MS, "10.90"),
            quote_at(first + 250 * MS, bid, str(Decimal(bid) + Decimal("0.02")), size=0),
            quote_at(START + 2 * SECOND, "10.27", "10.29"),
            quote_at(START + 8 * SECOND, "10.27", "10.29"),
        ],
        liquid=True,
        end=START + 12 * SECOND,
    )
    expected = chase_floor(
        exit_price(min(Decimal("10.27"), Decimal(bid)), 1, None), s.settings.chase_cap
    )
    floors = [state["floor"] for state in s.states if state["floor"] is not None]
    assert sells(orders) and held == 0 and floors and set(floors) == {expected}
    confirmed = [state for state in s.states if state["breach_price"] is not None]
    assert confirmed and all(state["breach_price"] == Decimal("10.27") for state in confirmed)


def one_sided_at(offset: int, ask: str = "10.36") -> QuoteTick:
    """A quote with no bid: a zero price and no size on that side, beside a real ask."""
    return QuoteTick(
        AAA,
        Price.from_str("0.0000"),
        Price.from_str(f"{Decimal(ask):.4f}"),
        Quantity.from_int(0),
        Quantity.from_int(100),
        T0 + offset,
        T0 + offset,
    )


def test_a_one_sided_quotes_zero_bid_is_no_price_for_the_first_sale_or_its_floor() -> None:
    """Two prints, 10.28 and 10.27, confirm the breach while the cached quote is one-sided. A zero bid
    is no price: the first sale is priced from the confirming print, `exit_ticks` through 10.27, and
    the floor is `chase_cap` under that sale, never under one priced from zero."""
    first = START + 100 * MS
    s, orders, _ = replay(
        [
            one_sided_at(START + 50 * MS),
            print_at(first, "10.28"),
            print_at(first + 100 * MS, "10.27"),
            quote_at(START + 2 * SECOND, "10.27", "10.29"),
        ],
        end=START + 4 * SECOND,
    )
    sale = exit_price(Decimal("10.27"), 1, None)
    expected = chase_floor(sale, s.settings.chase_cap)
    floors = [state["floor"] for state in s.states if state["floor"] is not None]
    assert floors and set(floors) == {expected}
    sales = sells(orders)
    assert sales and sales[0].order_type.name == "LIMIT"
    assert sales[0].price.as_decimal() == sale == Decimal("10.26")
    assert all(order.price.as_decimal() >= expected for order in sales)


def confirmed(bid: str, last: str | None) -> tuple[Any, Any]:
    """One step of the sale path after two prints confirmed a breach at 10.27: the cached quote's bid
    (`"0"` for a one-sided quote) and the cache's last trade."""
    seam = cast(Any, Seam(bid, breach_count=2, trigger=TriggerType.LAST_PRICE))
    seam.last, seam.last_ns = last, T0
    seam.cache.trade = lambda _: (
        None
        if seam.last is None
        else SimpleNamespace(
            price=Price.from_str(seam.last), ts_event=seam.last_ns, ts_init=seam.last_ns
        )
    )
    slot = seam_slot(seam)
    slot.exiting, slot.breach_price = True, Decimal("10.27")
    return seam, slot


def test_with_no_bid_a_recovery_after_the_confirming_print_does_not_lift_the_sale() -> None:
    """The last trade is back at 10.90 and the quote has no bid. The sale and its floor are priced from
    the confirming print, 10.27, never from the recovery."""
    seam, slot = confirmed("0", "10.90")
    seam._protect(slot, Decimal(5), T0)
    assert seam.sent[-1].price == Price.from_str("10.2600")
    assert slot.floor == chase_floor(Decimal("10.26"), seam.settings.chase_cap)


def test_with_no_bid_the_sale_and_its_floor_follow_a_fall_under_the_confirming_print() -> None:
    """Prints after the confirming one took the last trade to 9.90 in the same step, and the quote has
    no bid. The sale goes `exit_ticks` through 9.90 with its floor under that, as it would through a
    bid of 9.90: it does not rest at 10.26 above the market."""
    seam, slot = confirmed("0", "9.90")
    seam._protect(slot, Decimal(5), T0)
    assert seam.sent[-1].price == Price.from_str("9.8900")
    assert slot.floor == chase_floor(Decimal("9.89"), seam.settings.chase_cap)


def test_with_no_bid_a_remade_sale_follows_the_last_trade_down_and_never_back_up() -> None:
    """The first sale, 10.26 from the confirming print, is refused. Trades print at 10.10: the remake
    is 10.09, from the newest trade and not from the old print. The trades recover to 10.50: the next
    remake stays at 10.09."""
    seam, slot = confirmed("0", "10.27")
    seam._protect(slot, Decimal(5), T0)
    first = seam.sent[-1]
    assert first.price == Price.from_str("10.2600")
    seam.refuse(first)
    seam.last = "10.10"
    seam._protect(slot, Decimal(5), T0 + 6 * SECOND)
    second = seam.sent[-1]
    assert second is not first and second.price == Price.from_str("10.0900")
    seam.refuse(second)
    seam.last = "10.50"
    seam._protect(slot, Decimal(5), T0 + 12 * SECOND)
    assert seam.sent[-1] is not second and seam.sent[-1].price == Price.from_str("10.0900")


def no_bid_sale(*later: TradeTick) -> tuple[Any, list]:
    """Two prints, 10.28 and 10.27, confirm the breach under a one-sided quote, then `later`."""
    first = START + 100 * MS
    s, orders, _ = replay(
        [
            one_sided_at(START + 50 * MS),
            print_at(first, "10.28"),
            print_at(first + 100 * MS, "10.27"),
            *later,
        ]
    )
    return s, sells(orders)


def test_with_no_bid_an_older_print_that_arrives_late_is_not_a_fall_after_the_confirmation() -> (
    None
):
    """A second reader, 2026-11-12: a print at 9.90 whose own time is before the confirming print
    arrives after it. It is history, not a fall: the sale is priced from the confirming print,
    10.26, and not from the last trade received, 9.89."""
    first = START + 100 * MS
    s, sales = no_bid_sale(print_at(first + 50 * MS, "9.90", received=first + 200 * MS))
    assert sales and sales[0].price.as_decimal() == Decimal("10.26")
    floor = chase_floor(Decimal("10.26"), s.settings.chase_cap)
    assert all(order.price.as_decimal() >= floor for order in sales)


def test_with_no_bid_a_late_older_print_does_not_lift_the_sale_over_a_newer_fall_either() -> None:
    """The other way round: trades fell to 9.90 after the confirmation, and a print from before the
    fall, 10.40, arrives last. The newest trade by its own time is still 9.90: the sale is 9.89."""
    first = START + 100 * MS
    _, sales = no_bid_sale(
        print_at(first + 200 * MS, "9.90"),
        print_at(first + 150 * MS, "10.40", received=first + 250 * MS),
    )
    assert sales and sales[0].price.as_decimal() == Decimal("9.89")


def test_with_no_bid_two_prints_of_one_time_are_read_in_the_order_they_arrived() -> None:
    """Prints that share a time, as a sweep's do, cannot be ordered by it: the later arrival is the
    newer, as the cache has it."""
    first = START + 100 * MS
    _, sales = no_bid_sale(
        print_at(first + 200 * MS, "9.90"),
        print_at(first + 200 * MS, "9.95", received=first + 250 * MS),
    )
    assert sales and sales[0].price.as_decimal() == Decimal("9.94")


def test_with_no_bid_a_trade_the_cache_holds_from_before_the_slots_first_print_still_counts() -> (
    None
):
    """The slot's first print can itself be a late report, older than a trade the cache already
    holds: the newer of the two by its own time stands in for the bid."""
    seam, slot = confirmed("0", "9.90")
    slot.newest_print = (T0 - SECOND, Decimal("10.40"))
    seam._protect(slot, Decimal(5), T0)
    assert seam.sent[-1].price == Price.from_str("9.8900")


def test_with_no_bid_a_sale_remade_without_an_anchor_reads_the_same_newest_trade() -> None:
    """The wind-down remakes a sale through the plain limit with no anchor. With the arm on it reads
    the trade newest by its own time too, not the one received last: a late report of an older 9.90
    does not take a sale from 10.26 to 9.89."""
    seam, slot = confirmed("0", "9.90")
    seam.last_ns = T0 - SECOND
    slot.newest_print = (T0, Decimal("10.27"))
    seam._limit_sell(slot, Decimal(5), T0, None)
    assert seam.sent[-1].price == Price.from_str("10.2600")


def test_with_no_print_kept_the_plain_sale_reads_the_cache_as_before() -> None:
    """Nothing is kept with the arm off, and nothing before a slot's first print: the plain sale then
    prices from the cache's last trade, as it did."""
    seam, slot = confirmed("0", "9.90")
    assert slot.newest_print is None
    seam._limit_sell(slot, Decimal(5), T0, None)
    assert seam.sent[-1].price == Price.from_str("9.8900")


def test_a_floor_released_after_its_wait_is_not_put_back_by_the_persistence_arm() -> None:
    """The sale rested at its floor for the wait and the floor was released: later sales follow the
    bid. The persistence arm's own floor is set once, for the first sale, and not again."""
    seam, slot = confirmed("9.50", "9.50")
    slot.floor, slot.floor_released = None, True
    seam._protect(slot, Decimal(5), T0)
    assert seam.sent[-1].price == Price.from_str("9.4900")
    assert slot.floor is None and slot.floor_released


def test_a_breach_confirmed_by_prints_marks_the_exit_record_at_the_confirming_print() -> None:
    """The exit's record carries the decision to sell: the confirming print's own time and price, and a
    reason that counts the holding as stopped out."""
    first = START + 100 * MS
    s, _, held = replay(
        [
            print_at(first, "10.28"),
            print_at(first + 100 * MS, "10.27"),
            quote_at(START + 2 * SECOND, "10.27", "10.29"),
        ],
        liquid=True,
        end=START + 6 * SECOND,
    )
    assert held == 0 and len(s.exits) == 1
    row = s.exits[0]
    assert row.trigger_reason == "trailing stop (breach confirmed by prints)"
    assert row.trigger_price == Decimal("10.27") and row.trigger_ns == T0 + first + 100 * MS
    assert [entry["stopped_out"] for entry in s.entries_sent] == [True]


def test_a_gap_through_the_level_sells_on_the_first_limit_priced_through_the_bid() -> None:
    """The market gaps from 10.35 to 10.00, far through the 10.29 level. The first sale is a limit
    `exit_ticks` through the bid of that step, 9.98 under a 9.99 bid, so it never rests above the
    market, and it is the only sale: no chase is needed."""
    first = START + 100 * MS
    confirming = first + 200 * MS
    _, orders, held = replay(
        [
            print_at(first, "10.35"),
            quote_at(first + 50 * MS, "9.99", "10.01"),
            print_at(first + 100 * MS, "10.00"),
            print_at(confirming, "10.00"),
            quote_at(START + SECOND + 100 * MS, "9.99", "10.01"),
        ],
        end=first + 3 * SECOND,
    )
    sales = sells(orders)
    assert held == 0 and len(sales) == 1
    sale = sales[0]
    assert sale.order_type.name == "LIMIT" and sale.status.name == "FILLED"
    assert sale.price.as_decimal() == exit_price(Decimal("9.99"), 1, None) == Decimal("9.98")
    assert sale.price.as_decimal() < Decimal("9.99") < LEVEL
    assert sale.ts_init - (T0 + confirming) == 700 * MS


@pytest.mark.parametrize("skew", [-20 * SECOND, 20 * SECOND], ids=["delayed", "ahead-of-clock"])
def test_watermark_and_gap_read_each_print_at_the_earlier_of_its_event_and_receipt(
    skew: int,
) -> None:
    """A venue clock 20 s behind or ahead of this node's: every print carries the same skew, and the
    count runs on one clock either way, the print's own when it is behind and the receipt's when it
    is ahead."""
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first + skew, "10.28", received=first),
            print_at(first + 100 * MS + skew, "10.28", received=first + 100 * MS),
            # the simulated venue's book follows event time, so its quote carries the skew ahead too
            quote_at(START + SECOND + 100 * MS + max(skew, 0), received=START + SECOND + 100 * MS),
        ],
        skew=skew,
    )
    last_confirming = CONFIRMED + 400 * MS + min(skew, 0)
    assert state_at(s, 4 * MINUTE + 3 * SECOND)["watermark"] == T0 + last_confirming
    assert sells(orders) and held == 0
    assert min(o.ts_init for o in sells(orders)) == T0 + START + SECOND


def test_a_print_stamped_ahead_of_its_receipt_does_not_hide_the_prints_after_it() -> None:
    """One print whose event time is 20 s ahead of its receipt, above the level, then a real breach:
    read by its event time alone it would put the watermark 20 s ahead and drop every later print."""
    first = START + 100 * MS
    _, orders, held = replay(
        [
            print_at(first + 20 * SECOND, "10.35", received=first),
            print_at(first + 100 * MS, "10.28"),
            print_at(first + 200 * MS, "10.28"),
        ]
    )
    assert sells(orders) and held == 0


def test_a_stray_print_cached_before_arming_does_not_set_the_level() -> None:
    """The last print before the arming step is 10.90, far over the 10.31 fill. Taken as the high it
    would put the level at 10.69, and two ordinary prints at 10.35 would sell a holding that never
    fell. The level is armed from the fill instead, and those two prints confirm 10.35 as the high."""
    first = START + 100 * MS
    s, orders, held = replay(
        [print_at(first, "10.35"), print_at(first + 100 * MS, "10.35")],
        cached="10.90",
        confirm=False,
    )
    armed = state_at(s, 4 * MINUTE + 3 * SECOND)
    assert armed["high"] == Decimal("10.31") and armed["level"] < Decimal("10.31")
    assert sells(orders) == [] and held == 10
    assert s.states[-1]["high"] == Decimal("10.35")


def test_a_print_sharing_the_last_time_resets_but_never_counts_and_an_older_one_does_neither() -> (
    None
):
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.28"),
            # the same time again under the level: not a second observation, even with no gap
            print_at(first, "10.28", received=first + 50 * MS),
            # an older time above the level, a late report: it neither counts nor resets
            print_at(first - 1_000, "10.35", received=first + 100 * MS),
        ],
        confirm_gap_ms=0,
    )
    assert sells(orders) == [] and held == 10
    assert s.states[-1]["breaches"] == (T0 + first,)
    s, orders, held = replay(
        [
            print_at(first, "10.28"),
            # the same time above the level, as a sweep's prints can be stamped: the recovery resets
            print_at(first, "10.35", received=first + 50 * MS),
            print_at(first + 1_000, "10.28", received=first + 100 * MS),
        ],
        confirm_gap_ms=0,
    )
    assert sells(orders) == [] and held == 10
    assert s.states[-1]["breaches"] == (T0 + first + 1_000,)


def test_trade_handlers_queue_plain_pairs_and_only_the_step_confirms() -> None:
    first = START + 100 * MS
    s, orders, _ = replay([print_at(first, "10.28"), print_at(first + 100 * MS, "10.28")])
    delivered = [row for row in s.deliveries if row[0] >= T0 + first]
    assert len(delivered) == 2 and all(before == after for _, before, after, _ in delivered)
    assert delivered[-1][-1] == (
        (T0 + first, Decimal("10.28")),
        (T0 + first + 100 * MS, Decimal("10.28")),
    )
    assert min(o.ts_init for o in sells(orders)) == T0 + START + SECOND
    borrowed: Any = Borrowed()
    borrowed.persistence = True
    slot = s.slots[AAA]
    slot.trail_trigger = LEVEL
    borrowed.slots[AAA] = slot
    trade = print_at(first, "10.28")
    Mover.on_trade(borrowed, trade)
    Mover.on_quote(borrowed, quote_at(first))
    assert tuple(slot.observations) == ((trade.ts_event, Decimal("10.28")),)
    assert borrowed.trades == borrowed.quotes == 1


def test_nothing_is_sent_while_the_managed_market_exit_owns_the_holding() -> None:
    first = START + 100 * MS
    s, orders, held = replay(
        [
            quote_at(first - 50 * MS, "10.27", "10.29", size=0),
            print_at(first, "10.28"),
            print_at(first + 100 * MS, "10.28"),
            quote_at(START + 225 * MS, "10.27", "10.29", size=0),
            quote_at(START + SECOND + 100 * MS, "10.27", "10.29"),
        ],
        stop_offset=START + 250 * MS,
        liquid=True,
        manage_stop=True,
        market_exit_time_in_force=TimeInForce.DAY,
        market_exit_reduce_only=False,
    )
    assert s.stop_exiting and s.exit_steps and held == 0
    assert sells(orders) and all(o.order_type.name == "MARKET" for o in sells(orders))
    assert s.exit_step_sends and set(s.exit_step_sends) == {0}


@pytest.mark.parametrize("extended", [False, True], ids=["regular", "extended"])
def test_one_unconfirmed_breach_flattens_at_wind_down_before_window_end(extended: bool) -> None:
    wind_down, end = START + 2 * SECOND + 250 * MS, START + 5 * SECOND
    s, orders, held = replay(
        [
            print_at(START + 100 * MS, "10.28"),
            quote_at(wind_down - 100 * MS, "10.27", "10.29"),
            quote_at(wind_down + SECOND, "10.27", "10.29"),
        ],
        end=end + 3 * SECOND,
        stop_offset=end,
        wind_down_at=at(wind_down),
        extended_hours=extended,
        manage_stop=True,
        market_exit_time_in_force=TimeInForce.DAY,
        market_exit_reduce_only=False,
    )
    assert held == 0 and all(o.is_closed for o in orders)
    sale = sells(orders)
    assert len(sale) == 1 and sale[0].filled_qty.as_decimal() == 10
    assert sale[0].order_type.name == ("LIMIT" if extended else "MARKET")
    assert T0 + wind_down < sale[0].ts_init < T0 + wind_down + STEP_SECS * SECOND
    assert s.stop_requested_at == T0 + end and s.flat_at_stop
    assert all(o.ts_init <= T0 + end for o in orders)
    if extended:
        assert all(row[-1] == {"extended_hours": True} for row in s.submissions)


def test_flat_clears_every_piece_of_the_slots_persistence_state() -> None:
    first = START + 100 * MS
    s, orders, held = replay([print_at(first, "10.28"), print_at(first + 100 * MS, "10.28")])
    assert held == 0 and sells(orders)
    last = s.states[-1]
    assert last["level"] is last["high"] is last["floor"] is last["breach_price"] is None
    assert last["breaches"] == last["highs"] == last["queued"] == ()
    assert last["watermark"] == 0


@pytest.mark.parametrize(("breach", "high"), [(1, 1), (1, 2), (2, 1), (2, 2)])
def test_record_names_each_arm_and_its_count_and_shared_gap(breach: int, high: int) -> None:
    s = strategy(
        breach_count=breach,
        high_count=high,
        confirm_gap_ms=100,
        trigger=TriggerType.LAST_PRICE,
    )
    record = s.record()
    assert record["persistence_arms"] == {
        "breach": {"state": "on" if breach > 1 else "off", "count": breach, "gap_ms": 100},
        "high": {"state": "on" if high > 1 else "off", "count": high, "gap_ms": 100},
    }
    assert record["rows"]["breach_count"] == str(breach)
    assert record["rows"]["high_count"] == str(high)
    assert record["rows"]["confirm_gap_ms"] == "100"
    defaults = strategy().settings
    assert (defaults.breach_count, defaults.high_count, defaults.confirm_gap_ms) == (1, 1, 100)
    if breach > 1 or high > 1:
        first = START + 100 * MS
        observed, orders, held = replay(
            [print_at(first + k * 100 * MS, "10.28") for k in range(breach)],
            breach_count=breach,
            high_count=high,
        )
        assert held == 0 and sells(orders)
        assert all(row[0] == "LIMIT" for row in observed.submissions)


@pytest.mark.parametrize("name", ["breach_count", "high_count"])
def test_counts_must_be_positive_integers(name: str) -> None:
    assert getattr(strategy().settings, name) == 1
    for value in (0, -1):
        with pytest.raises(ValueError, match=name):
            strategy(**{name: value}, trigger=TriggerType.LAST_PRICE)
    for value in (True, 1.5, "2", None):
        with pytest.raises(TypeError, match=name):
            strategy(**{name: value}, trigger=TriggerType.LAST_PRICE)


def test_the_shared_gap_must_be_a_non_negative_integer() -> None:
    assert strategy().settings.confirm_gap_ms == 100
    assert strategy(confirm_gap_ms=0).settings.confirm_gap_ms == 0
    with pytest.raises(ValueError, match="confirm_gap_ms"):
        strategy(confirm_gap_ms=-1)
    for value in (True, 1.5, "100", None):
        with pytest.raises(TypeError, match="confirm_gap_ms"):
            strategy(confirm_gap_ms=value)


@pytest.mark.parametrize(("breach", "high"), [(2, 1), (1, 2), (2, 2)])
def test_bid_ask_is_refused_by_name_when_either_persistence_arm_is_on(breach, high) -> None:
    with pytest.raises(ValueError, match="BID_ASK.*breach_count.*high_count"):
        strategy(breach_count=breach, high_count=high, trigger=TriggerType.BID_ASK)


@pytest.mark.parametrize("mode", ["atr", "tier", "ratchet"])
def test_persistence_is_refused_by_name_under_any_trail_mode_but_fixed(mode: str) -> None:
    """The strategy-held level takes its distance from the fixed trail. A trail plan's distance, its
    tighten and its chase cap are not carried into it, so such a row is refused, not half-applied."""
    for rows in ({"breach_count": 2}, {"high_count": 2}):
        with pytest.raises(ValueError, match=f"trail_mode={mode!r} is refused with persistence"):
            strategy(trigger=TriggerType.LAST_PRICE, trail_mode=mode, **rows)
    assert strategy(trigger=TriggerType.LAST_PRICE, trail_mode=mode).settings.persistence is False


def test_persistence_is_refused_by_name_when_protection_is_off() -> None:
    """A hold-to-close row has no protective sale for an arm to confirm; it is not recorded as on."""
    for rows in ({"breach_count": 2}, {"high_count": 2}):
        with pytest.raises(ValueError, match="protect=False is refused with persistence"):
            strategy(trigger=TriggerType.LAST_PRICE, protect=False, **rows)
    assert strategy(trigger=TriggerType.LAST_PRICE, protect=False).settings.persistence is False


def test_the_bars_replay_refuses_a_persistence_arm_by_name() -> None:
    """Bars deliver no trade print, and with an arm on the replay's own trailing sell is never
    reached: such a trial would hold unprotected and be recorded as if the rows had been measured."""
    for rows in ({"breach_count": 2}, {"high_count": 2}):
        with pytest.raises(ValueError, match="bars replay cannot run a persistence arm"):
            BarsReplayMover(strategy(trigger=TriggerType.LAST_PRICE, **rows).settings)
    BarsReplayMover(strategy(trigger=TriggerType.LAST_PRICE).settings)


def test_off_arms_leave_the_persistence_rows_out_of_a_trials_key() -> None:
    """With both arms off no run reads the rows: the key is what it was before they existed, and an
    unread gap does not make a second trial. With an arm on all three are keyed."""
    from canon_trading.mover import PERSISTENCE_FIELDS

    off = _strategy_fields(strategy().settings)
    assert not set(PERSISTENCE_FIELDS) & set(off)
    assert _strategy_fields(strategy(confirm_gap_ms=250).settings) == off
    on = _strategy_fields(strategy(breach_count=2, trigger=TriggerType.LAST_PRICE).settings)
    assert {name: on[name] for name in PERSISTENCE_FIELDS} == {
        "breach_count": 2,
        "high_count": 1,
        "confirm_gap_ms": 100,
    }
    assert {name: value for name, value in on.items() if name not in PERSISTENCE_FIELDS} == {
        **off,
        "trigger": on["trigger"],
    }


def test_a_sweep_one_price_step_above_the_level_never_sells() -> None:
    first = START + 100 * MS
    data = [print_at(first + k * 40_000, "10.30") for k in range(28)]
    data.append(print_at(first + SECOND, "10.35"))
    assert len(data) == 29 and data[27].ts_event - data[0].ts_event < 1_200_000
    assert all(t.price.as_decimal() > LEVEL for t in data)
    _, orders, held = replay(data)
    assert held == 10 and sells(orders) == []


def test_stray_two_sided_quotes_below_every_print_never_count() -> None:
    first = START + 100 * MS
    # Both quote sides remain valid; the bid is 2.3% below every market print.
    bid = Decimal("10.30") * Decimal("0.977")
    data = [
        quote_at(first, str(bid), "10.36"),
        quote_at(first + MS),
        quote_at(first + SECOND, str(bid), "10.36"),
        quote_at(first + 4 * SECOND),
        *(print_at(first + k * 100 * MS + 1_000, "10.30") for k in range(51)),
    ]
    s, orders, held = replay(data, end=first + 6 * SECOND)
    assert bid < LEVEL and s.quotes > 4 and s.trades > 50
    assert held == 10 and sells(orders) == []
    assert s.states[-1]["level"] == LEVEL


def test_a_large_stray_print_and_a_second_within_four_ms_recover_without_selling() -> None:
    first = START + 100 * MS
    stray = Decimal("10.35") * (1 - Decimal("0.0094"))
    assert stray < LEVEL
    data = [
        print_at(first, str(stray), size=50_000),
        print_at(first + 300_000, "10.35"),
        print_at(first + 700_000, "10.35"),
        print_at(first + 4 * MS, str(stray)),
        *(print_at(first + k * 15 * MS, "10.35") for k in range(1, 100)),
    ]
    _, orders, held = replay(data)
    assert held == 10 and sells(orders) == []


def test_a_sweep_through_the_level_sells_at_zero_gap_and_holds_at_the_row() -> None:
    first = START + 100 * MS
    data = [print_at(first + k * 40_000, "10.28") for k in range(28)]
    data.append(print_at(first + SECOND, "10.35"))
    assert data[27].ts_event - data[0].ts_event < 1_200_000
    assert all(t.price.as_decimal() <= LEVEL for t in data[:28])
    _, no_gap, held_without_gap = replay(data, confirm_gap_ms=0)
    assert sells(no_gap) and held_without_gap == 0
    _, row, held_at_row = replay(data)
    assert sells(row) == [] and held_at_row == 10


def test_a_sustained_drop_sends_a_day_limit_within_one_step_of_confirmation() -> None:
    first = START + 100 * MS
    confirming = first + 2 * 100 * MS
    data = [
        print_at(first, "10.35"),
        print_at(first + 100 * MS, "10.29"),
        print_at(confirming, "10.28"),
        *(print_at(first + k * 100 * MS, "10.27") for k in range(3, 41)),
        quote_at(START + SECOND + 100 * MS, "10.27", "10.29"),
    ]
    s, orders, held = replay(data, end=first + 5 * SECOND, extended_hours=True)
    sales = sells(orders)
    assert held == 0 and sales
    assert all(o.order_type.name == "LIMIT" and o.time_in_force == TimeInForce.DAY for o in sales)
    # Include canceled/unfilled sales: a later fill cannot hide an early submission.
    assert all(o.ts_init > T0 + confirming for o in sales)
    lag = min(o.ts_init for o in sales) - (T0 + confirming)
    assert lag == 700 * MS  # This tape confirms 300 ms into the step: exactly 700 ms to submit.
    assert lag <= STEP_SECS * SECOND
    assert all(row[-1] == {"extended_hours": True} for row in s.submissions)
    trailing, _ = offsets(Decimal("10.31"), s.settings.trail, s.settings.exit_through)
    assert HIGH - trailing == LEVEL


def test_a_breach_sale_keeps_the_confirmed_level_beside_its_fill() -> None:
    """The lead's ruling of 2026-11-12 17:43 under persistence: the sale a confirmed breach sends has
    the level it crossed beside its fill in the record (`sell_triggers`), so a gap's loss past the
    level can be read per exit (code review of the trigger record, 2026-11-12)."""
    first = START + 100 * MS
    s, orders, held = replay(
        [
            print_at(first, "10.35"),
            quote_at(first + 50 * MS, "9.99", "10.01"),
            print_at(first + 100 * MS, "10.00"),
            print_at(first + 200 * MS, "10.00"),
            quote_at(START + SECOND + 100 * MS, "9.99", "10.01"),
        ],
        end=first + 3 * SECOND,
    )
    sales = sells(orders)
    assert held == 0 and len(sales) == 1
    record = s.record()
    trigger = record["sell_triggers"].get(sales[0].client_order_id.value)
    assert trigger is not None and Decimal(trigger) == LEVEL, record["sell_triggers"]
