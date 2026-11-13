"""Persistence step 1 (the lead's ruling of 2026-11-12, 11:26): a breach is a trade print at or under the
trail's level, never a quote. That is upstream's `LAST_PRICE` election (the pinned emulator sets the
matching core's last price from each `TradeTick` and iterates its held orders,
`crates/execution/src/order_emulator/emulator.rs:1093-1110` at d53849b2), which `MoverConfig.trigger`
already accepts. The default, `BID_ASK`, sets the bid from every `QuoteTick` and iterates
(`emulator.rs:1076-1084`), so one quote under the level sells.

The tape is SHAPED, not recorded: an invented instrument, times as offsets from its start, and prices
and sizes chosen for the geometry of an exit seen on paper. A run-up is bought at 1.97. Under
`LAST_PRICE` the trail's four cents follow the highest print, 1.97, so the sale is held at 1.93; under
`BID_ASK` they follow the highest bid, 1.96, so it is held at 1.92. Prints sweep 1.94 within about a
millisecond, and a few more follow over four seconds; none is at or under 1.93. One bid at 1.91,
under both levels, stands for 1 ms just after the sweep, then the bid recovers."""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TriggerType

from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover import trade_tape
from tests.test_mover_breach import MS
from tests.test_mover_breach import print_at
from tests.test_mover_breach import quote_at

US = SECOND // 1_000_000

RISE = [1.80, 1.83, 1.86, 1.89, 1.92, 1.94, 1.96, 1.97]
"""The entry's minutes: a run up to 1.97, bought at the last minute's ask."""

LEVEL = Decimal("1.93")
"""The highest print, 1.97, less the trail's four cents: where `LAST_PRICE` holds the sale."""

LONE_BID = "1.91"
"""The one bid under the level, standing for 1 ms."""

SWEEP_US = [n * 40 for n in range(30)]
"""Thirty prints a cent above the level, 40 us apart: the level's neighbour swept in about 1.2 ms."""

AFTER = [(1_000_000, "1.95"), (2_000_000, "1.94"), (2_200_000, "1.94"), (4_000_000, "1.94")]
"""The prints after the sweep, as microseconds after its first print, and their prices."""


def quote(offset: int, bid: str, ask: str):
    """`test_mover_breach.quote_at`, a thousand shares a side."""
    return quote_at(offset, bid, ask, size=1000)


def prints(start: int) -> list:
    """The sweep and the prints after it, `start` an offset from T0 (`test_mover_breach.print_at`)."""
    return [print_at(start + us * US, "1.94") for us in SWEEP_US] + [
        print_at(start + us * US, price) for us, price in AFTER
    ]


def shaped_tape(*, level_print: bool = False) -> list:
    """The entry, a few quotes either side, the sweep, and one bid under the level for 1 ms; with
    `level_print`, one print at the level a second after the sweep."""
    data = trade_tape(AAA, RISE, size=100_000)
    entered = len(RISE) * MINUTE  # offsets from T0, as `print_at` and `quote_at` take them
    data += [quote(entered + 5 * SECOND, "1.96", "1.97"), quote(entered + MINUTE, "1.94", "1.95")]
    sweep = entered + 2 * MINUTE + 30 * SECOND
    data += [quote(sweep - 30 * SECOND, "1.94", "1.95")]
    data += prints(sweep)
    data += [quote(sweep + 2 * MS, LONE_BID, "1.95"), quote(sweep + 3 * MS, "1.94", "1.95")]
    data += [quote(sweep + 30 * SECOND, "1.94", "1.95"), quote(sweep + 75 * SECOND, "1.94", "1.95")]
    if level_print:
        data += [print_at(sweep + SECOND + 1, str(LEVEL))]
    return sorted(data, key=lambda d: d.ts_init)


def sells(trigger: TriggerType, *, level_print: bool = False) -> tuple[list, list[Decimal]]:
    """The sales filled, and every trigger the held trailing sell carried while emulated."""
    eng = engine(cash=Decimal(100_000))
    s = strategy(max_notional=Decimal(30_000), trigger=trigger, bar_spec="1-MINUTE-LAST-INTERNAL")
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), shaped_tape(level_print=level_print))
    bought = [o for o in orders if o.side == OrderSide.BUY and o.filled_qty.as_double() > 0]
    sold = [o for o in orders if o.side == OrderSide.SELL and o.filled_qty.as_double() > 0]
    eng.dispose()
    assert bought, "the entry must fill for the tape to test its trail"
    return sold, s.triggers


def test_one_bid_under_the_level_sells_on_the_bid_but_no_print_does_on_the_last_trade() -> None:
    """Under `BID_ASK` the one bid under the level sells; under `LAST_PRICE` the prints, none at or
    under the level, sell nothing while the trailing sell is held at the level."""
    sold, _ = sells(TriggerType.BID_ASK)
    assert sold, "the bid election is the tape's premise"
    sold, triggers = sells(TriggerType.LAST_PRICE)
    assert sold == []
    assert triggers and max(triggers) == LEVEL


def test_a_print_at_the_level_sells_on_the_last_trade() -> None:
    """The control: the same tape with one print at the level sells under `LAST_PRICE`, so the
    nothing sold above is the election's and not a sale that could never trigger
    (code-review-high-print/2)."""
    sold, triggers = sells(TriggerType.LAST_PRICE, level_print=True)
    assert sold
    assert max(triggers) == LEVEL


def test_a_cached_print_older_than_the_entry_is_not_followed_at_a_remake() -> None:
    """A trailing sell made again from a reached 10.38 under `LAST_PRICE` reads the cache's last trade.
    One stamped before the entry filled is not of this holding: it is not followed, as before the
    first print, so its 10.20 under the kept trigger sells nothing; a print after the fill at 10.20
    does. The rule is #322's for the persistence path, where a print cached before arming is never
    the level (code-review-high-trig/3)."""
    seam = Seam("10.50", trigger=TriggerType.LAST_PRICE)
    slot = seam_slot(seam)
    filled = T0 + 10 * SECOND
    cast(Any, seam).fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id  # what #320's held stop reads the fill by
    last: list = []
    seam.cache = SimpleNamespace(quote=seam._quote, trade=lambda _id: last[-1] if last else None)
    slot.trail_trigger = Decimal("10.38")
    last.append(
        SimpleNamespace(price=Price.from_str("10.2000"), ts_event=filled - 1, ts_init=filled)
    )
    cast(Any, seam)._protect(slot, Decimal(3), T0 + 20 * SECOND)
    # not followed, so #320's held stop: it does not read that print as the last known price either
    assert seam.sent == [] and not slot.exiting
    last.append(
        SimpleNamespace(price=Price.from_str("10.2000"), ts_event=filled, ts_init=filled + 1)
    )
    cast(Any, seam)._protect(slot, Decimal(3), T0 + 21 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting


def holding(trigger: TriggerType, bid: str = "0", ask: str = "0") -> tuple[Any, Any, int, list]:
    """A Seam whose entry E-1 filled 3 at 10.00, its quote `bid`/`ask`, and a cache whose last trade is
    the last of the returned list (none at first)."""
    seam = cast(Any, Seam(bid, trigger=trigger))
    slot = seam_slot(seam)
    filled = T0 + 10 * SECOND
    seam.fills = [("E-1", OrderSide.BUY.name, "3", "10.00", filled)]
    seam.orders["E-1"].client_order_id = slot.entry_id
    last: list = []
    quote = SimpleNamespace(bid_price=Price.from_str(bid), ask_price=Price.from_str(ask))
    seam.cache = SimpleNamespace(
        quote=lambda _id: quote, trade=lambda _id: last[-1] if last else None
    )
    return seam, slot, filled, last


def stamped(price: str, event: int, received: int) -> SimpleNamespace:
    return SimpleNamespace(price=Price.from_str(price), ts_event=event, ts_init=received)


def test_under_the_bid_the_held_stop_reads_the_last_trade_as_on_main() -> None:
    """A `bid_ask` window is as on main (#320): its held stop reads the cache's last trade, whatever its
    stamp, and a trade at 9.70 under the held stop sells at once (code-review-high-trig-win/1)."""
    seam, slot, filled, last = holding(TriggerType.BID_ASK)
    last.append(stamped("9.7000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (last trade)"


def test_under_a_print_trigger_a_close_older_than_a_stale_print_is_no_price_either() -> None:
    """Under `last_price` the cache's print is from before the entry, so no price of this holding is
    known: a minute close, no newer than that print, does not sell through the held stop either
    (code-review-high-trig-win/1)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE)
    slot.closes = deque([Decimal("9.70")], maxlen=4)
    last.append(stamped("9.9900", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert seam.sent == [] and not slot.exiting and slot.held_stop is not None


def test_under_a_print_trigger_a_two_sided_quote_holds_past_60_s_without_a_print() -> None:
    """Under `last_price` no print since the fill is no followed price, but the quote is two-sided:
    #320's `unpriced_exit` is for a holding with no two-sided quote, so 61 s on nothing is sold; it
    waits for a print or the wind-down, as the handbook says (code-review-high-trig-win/2)."""
    seam, slot, filled, _ = holding(TriggerType.LAST_PRICE, bid="10.05", ask="10.07")
    seam._protect(slot, Decimal(3), filled + SECOND)
    seam._protect(slot, Decimal(3), filled + 61 * SECOND)
    assert seam.sent == [] and not slot.exiting


def test_under_a_print_trigger_a_one_sided_quote_still_sells_after_60_s() -> None:
    """The control: with no print and a one-sided quote, #320's `unpriced_exit` still sells at 60 s."""
    seam, slot, filled, _ = holding(TriggerType.LAST_PRICE)
    seam._protect(slot, Decimal(3), filled + SECOND)
    seam._protect(slot, Decimal(3), filled + 61 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"]
    assert slot.exit_record.trigger_reason == "unpriced_exit"


def test_a_print_after_the_fill_by_the_venues_clock_is_followed_whatever_its_receipt() -> None:
    """A print the venue stamped 200 ms after the fill is of this holding even if this host received it
    at a time before the fill on its own clock: the boundary compares venue times only
    (code-review-high-trig-win/5)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="10.05", ask="10.07")
    last.append(stamped("10.0500", filled + 200 * MS, filled - 300 * MS))
    assert seam._followed(slot) == Decimal("10.05")


def test_under_a_print_trigger_a_quote_turning_one_sided_mid_hold_sells_at_the_next_step() -> None:
    """The one bound short of the wind-down on a print trigger's hold: two-sided with no print, the
    holding is held past 60 s; once its cached quote reads one-sided it sells as `unpriced_exit` at the
    next step (the lead's counted limit, 2026-11-12). This tests the cached quote only: the adapter
    discards a quote with a side at or under zero (canon_alpaca.data `parse_quote`), so on a live feed
    the cache keeps the last two-sided quote and the holding waits for a print or the wind-down, as
    `PRINT_TRIGGER_FINDINGS` names (xr3 fast reader, note 2)."""
    seam, slot, filled, _ = holding(TriggerType.LAST_PRICE, bid="10.05", ask="10.07")
    seam._protect(slot, Decimal(3), filled + SECOND)
    seam._protect(slot, Decimal(3), filled + 61 * SECOND)
    assert seam.sent == [] and not slot.exiting
    one_sided = SimpleNamespace(bid_price=Price.from_str("0"), ask_price=Price.from_str("10.07"))
    seam.cache.quote = lambda _id: one_sided
    seam._protect(slot, Decimal(3), filled + 62 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "unpriced_exit"


def test_under_a_print_trigger_the_first_print_through_the_held_stop_sells() -> None:
    """A fresh holding whose cached print predates its fill is held at 9.80 (10.00 less the 2% trail).
    The first print of its own, at 9.70, is through that stop: it sells by limit as the held stop, and
    is not armed as a trail from 9.70 with no trigger, which would protect it at 9.50 (xr3 fast
    reader, SF1)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.69", ask="9.71")
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert slot.held_stop == Decimal("9.80") and seam.sent == []
    last.append(stamped("9.7000", filled + 2 * SECOND, filled + 2 * SECOND))
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"
    assert slot.entry_record is None or slot.entry_record.get("protect_path") == "held_stop"


def test_under_a_print_trigger_a_held_stop_with_no_print_at_all_reads_no_minute_close() -> None:
    """Under `last_price` with no print cached at all, the minute close is from before the entry, so it is
    no price of this holding either and does not sell through the held stop (code-review-high-succ/3)."""
    seam, slot, filled, _last = holding(TriggerType.LAST_PRICE)
    slot.closes = deque([Decimal("9.70")], maxlen=4)
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert seam.sent == [] and not slot.exiting and slot.held_stop is not None


def test_under_a_print_trigger_a_print_between_the_step_and_an_off_step_held_stop_sells() -> None:
    """A held stop off Alpaca's step (9.8049) and a first print of the holding's own at 9.802, under it
    but over the stop rounded down: the hold's end sells as `_hold` would, not arming a trail at 9.80
    under the holding's own held stop (code-review-high-succ/6)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.79", ask="9.81")
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    slot.held_stop = Decimal("9.8049")
    last.append(stamped("9.8020", filled + 2 * SECOND, filled + 2 * SECOND))
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"


def test_under_a_print_trigger_a_cheaper_fill_in_the_hold_does_not_lower_the_held_stop() -> None:
    """Filled 3 at 10.00 for L 0.20: held at 9.80. A further 3 fill at 9.80 (average 9.90) would put L
    under the average at 9.70; the held stop stays 9.80, and the first own print at 9.75 sells through
    it rather than arming a trail at 9.70 (the xr4 reader's F1; the lead's H3)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.74", ask="9.76")
    slot.sized_loss = Decimal("0.20")
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    assert slot.held_stop == Decimal("9.80")
    seam.fills.append(("E-1", OrderSide.BUY.name, "3", "9.80", filled + 2 * SECOND))
    seam.orders["E-1"].avg_px = 9.90
    seam._protect(slot, Decimal(6), filled + 3 * SECOND)
    assert slot.held_stop == Decimal("9.80") and seam.sent == []
    last.append(stamped("9.7500", filled + 4 * SECOND, filled + 4 * SECOND))
    seam._protect(slot, Decimal(6), filled + 5 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting


def test_under_a_print_trigger_a_print_through_the_held_stop_and_back_before_the_step_sells() -> (
    None
):
    """Held at 9.80; between two steps the holding prints 9.70, through the stop, then 9.90. The step's
    cached print is 9.90, and the hold's end still sells through the stop (the xr4 reader's F2)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    seam.slots, seam.trades = {slot.instrument_id: slot}, 0
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    for price, at in (("9.7000", filled + 2 * SECOND), ("9.9000", filled + 2 * SECOND + 1)):
        trade = SimpleNamespace(
            instrument_id=slot.instrument_id, price=Price.from_str(price), ts_event=at, ts_init=at
        )
        last.append(trade)
        seam.on_trade(trade)
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"


def test_under_a_print_trigger_a_first_print_above_the_held_stop_arms_the_trail_from_it() -> None:
    """The same hold, its first own print at 9.90, above the 9.80 stop: the trail is armed with its
    trigger at the held stop on Alpaca's step, so the protection does not fall to 9.90 less the trail
    (xr3 fast reader, SF1)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, Decimal(3), filled + SECOND)
    last.append(stamped("9.9000", filled + 2 * SECOND, filled + 2 * SECOND))
    seam._protect(slot, Decimal(3), filled + 3 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING"] and not slot.exiting
    assert seam.sent[0].trigger_price is not None
    assert Decimal(str(seam.sent[0].trigger_price)) == Decimal("9.80")
