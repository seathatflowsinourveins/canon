"""The gain lock's arithmetic, shared by the owner protector and the Mover (rows ROUND_TRIP_COST,
NOISE_FLOOR; handbook/rows.md).

Contract: the owner's requirement, ADR 0083:29 and handbook/philosophy.md:29 ("Once the gain covers
its costs, a gain lock holds it"); handbook/practices.md:38 (arming only above entry plus round-trip
cost plus the noise floor; an unknown cost basis or quote leaves it unarmed); the lead's rulings of
2026-11-13: R3 (the lock moves the trigger up and never withholds or floors the sale) and the cost
ruling. The entry's fill paid the ask and the lock reads the bid, so the spread is already inside
both prices and is not counted again. The round-trip cost is fees per share: zero on paper, until
the fee record (ADR 0147) is on main, a named limit. The noise floor is SPREAD_FLOOR_M times the
arming quote's spread. Both are fixed at arming. Pure arithmetic: no clock, cache or order here.
"""

from __future__ import annotations

from decimal import Decimal

from canon_trading import protect
from canon_trading.price_step import up_on_step

FEES_PER_SHARE = Decimal(0)
"""ROUND_TRIP_COST on paper: no fee is charged; fees enter with the fee record (ADR 0147)."""


def arming_measures(bid: Decimal, ask: Decimal) -> tuple[Decimal, Decimal] | None:
    """(round-trip cost, noise floor) per share at a two-sided quote, or None with no positive spread.

    The cost is FEES_PER_SHARE (the spread is already inside the fill and the bid). The noise floor
    is SPREAD_FLOOR_M times the quoted spread, the distance's own floor measure
    (protect.PLANNER.spread_floor_m).
    """
    if bid <= 0 or ask <= bid:
        return None
    return FEES_PER_SHARE, protect.PLANNER.spread_floor_m * (ask - bid)


def arms(bid: Decimal, entry: Decimal, cost: Decimal, noise: Decimal) -> bool:
    """A quote arms the lock when its bid is at or above entry plus cost plus the noise floor."""
    return bid >= entry + cost + noise


def cost_floor(entry: Decimal, cost: Decimal) -> Decimal:
    """Entry plus the round-trip cost, rounded up onto the price step: the trigger never goes under it,
    and the first sale is never priced under the fill."""
    return up_on_step(entry + cost)
