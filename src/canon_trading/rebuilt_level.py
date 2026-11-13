"""Rebuild a carried long's stop from the tape: a node that starts holding a long re-arms its
trailing sell from the level the minute closes since the protection start allow, not from state
kept across the restart. The caller resolves the trail row to a price offset (``mover.offsets``,
:774-804); only minute closes lift the level, never a bar's high, so one stray print cannot raise
it (ADR 0119:12-14). The cost: the level can be looser than the bid-high the live engine reached.

NautilusTrader v2.0.0rc8, d53849b216e20f2c122ca01b4dd0da377638b186:
``crates/execution/src/trailing.rs:61-65`` seeds the existing trigger; :81-102 moves a
sell's trigger only upward, and :182-200 subtracts a PRICE offset from its basis.
``mover.py:4256-4263`` passes the retained trigger when arming the emulated sell.
Venue rounding reuses ``price_step.down_on_step`` (:83-98), which follows the pinned
``crates/model/src/instruments/tick_scheme.rs:525-552`` for the sell-side tick.

The Bar wrapper reads only the bar type, event time and close, through the pin's
``crates/model/src/python/data/bar.rs:401-405,409-411,478-482,502-518`` (a specification's text
form is ``step-aggregation-price_type``, ``crates/model/src/data/bar.rs:708-711``) and
``crates/model/src/python/types/price.rs:518-522`` (``Price.as_decimal``).
It does not fetch bars or resolve a row; those belong to the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_FLOOR
from decimal import Context
from decimal import Decimal
from decimal import localcontext
from typing import TYPE_CHECKING

from .price_step import down_on_step

if TYPE_CHECKING:
    from nautilus_trader.model import Bar


MINUTE_CLOSES = "1-MINUTE-LAST"
"""The one bar specification whose closes the level is rebuilt from: one-minute trade bars."""


def _positive_finite(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, not {type(value).__name__}: {value}")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive and finite: {value}")


def rebuilt_level(closes: Sequence[Decimal], start: Decimal, offset: Decimal) -> Decimal:
    """Return the highest close less the price offset, floored to Alpaca's step, or start.

    Every input must be positive and finite, including start and offset with no closes.
    Start is an immutable floor: if it is off-step, it is preserved rather than rounded
    below itself. Only an increase is rounded. No closes returns start unchanged.
    """
    _positive_finite(start, "start")
    _positive_finite(offset, "offset")
    highest = start
    for index, close in enumerate(closes):
        _positive_finite(close, f"close[{index}]")
        highest = max(highest, close)

    # Keep at least four fractional places even above the tick scheme's expansion.
    # Floor the subtraction too: a tiny offset must not round back up to its close.
    # A private context keeps the caller's rounding, precision, traps and flags out.
    with localcontext(Context(prec=max(28, highest.adjusted() + 5), rounding=ROUND_FLOOR)):
        candidate = highest - offset
        return start if candidate <= start else max(start, down_on_step(candidate))


def rebuilt_level_from_bars(bars: Sequence[Bar], start: Decimal, offset: Decimal) -> Decimal:
    """Rebuild from one instrument's one-minute trade bars in nondecreasing event-time order.

    Receipt times do not order the tape. A bar of another bar type (another name, a day or
    quote series, another source) is refused. A minute delivered twice is read once; two
    different closes for one minute are refused, since which is the minute's is not known.
    Nothing sorts or mutates the input.
    """
    closes: list[Decimal] = []
    bar_type = None
    previous: tuple[int, Decimal] | None = None
    for index, bar in enumerate(bars):
        if bar_type is None:
            bar_type = bar.bar_type
            if str(bar_type.spec) != MINUTE_CLOSES:
                raise ValueError(f"bars[{index}] is not a {MINUTE_CLOSES} bar: {bar_type}")
        elif bar.bar_type.instrument_id != bar_type.instrument_id:
            raise ValueError(f"bars[{index}] has a mixed instrument: {bar.bar_type.instrument_id}")
        elif bar.bar_type != bar_type:
            raise ValueError(f"bars[{index}] is of another bar type: {bar.bar_type}")

        close = bar.close.as_decimal()
        if previous is not None:
            if bar.ts_event < previous[0]:
                raise ValueError(f"bars[{index}] is out of time order by ts_event")
            if bar.ts_event == previous[0]:
                if close != previous[1]:
                    raise ValueError(f"bars[{index}] gives its minute a second close: {close}")
                continue
        previous = bar.ts_event, close
        closes.append(close)

    return rebuilt_level(closes, start, offset)
