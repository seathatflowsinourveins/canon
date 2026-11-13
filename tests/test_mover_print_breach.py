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
