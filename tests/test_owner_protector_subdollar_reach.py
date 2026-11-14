"""Case (d) of the sub-dollar offset unit (L5's brief, 2026-11-13 17:09): a sub-dollar EXTERNAL long
the owner protector adopts has its trailing stop-limit's trigger-to-limit spacing on 0.0001 and no
more than CHASE_CAP of the trigger. The protector plans its offsets with `mover.offsets`
(`OwnerProtector._plan_holding`), so this is the reach of that unit's fix. This base carries the
old cent rounding, so the case is a strict expected failure until both are on one base: its pass
then fails the mark here and the mark is removed in that landing.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR
from decimal import Decimal

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading.protect import PLANNER
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture
from tests.test_owner_protector import position

STEP = Decimal("0.0001")


def subdollar_bars(mid: Decimal, width: Decimal, count: int = 14) -> list[Bar]:
    return [
        Bar(
            BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"),
            Price.from_str(str(mid)),
            Price.from_str(str(mid + width / 2)),
            Price.from_str(str(mid - width / 2)),
            Price.from_str(str(mid)),
            Quantity.from_int(10_000),
            T0 - minute * 60 * SECOND,
            T0 - (minute - 1) * 60 * SECOND,
        )
        for minute in range(1, count + 1)  # Cache returns newest first.
    ]


def subdollar_quote(bid: Decimal, at: int) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(str(bid)),
        Price.from_str(str(bid + STEP)),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        at,
        at,
    )


OLD_RULE = pytest.mark.xfail(
    strict=True,
    reason="the sub-dollar offsets (mover.offsets on tick(entry)) are not on this base",
)


@pytest.mark.parametrize(
    ("bid", "row", "width"),
    [
        # At 0.6123 these rows' distances (0.0073 and 0.0072) take a 0.01 limit offset, which already
        # fits: the brief's 0.5923 release came from another distance (UNVERIFIED).
        (Decimal("0.6123"), "OWN1", Decimal("0.0073")),
        (Decimal("0.6123"), "OWN3", Decimal("0.0024")),
        # At 0.42 a distance of 0.0084 takes 0.02 under the old rule: 0.0116 of spacing against a cap
        # of 0.0082 at a 0.41 trigger.
        pytest.param(Decimal("0.4200"), "OWN1", Decimal("0.0084"), marks=OLD_RULE),
        pytest.param(Decimal("0.4200"), "OWN3", Decimal("0.0028"), marks=OLD_RULE),
    ],
)
def test_a_subdollar_adopted_long_has_its_spacing_on_the_step_and_within_the_chase_cap(
    bid, row, width
) -> None:
    strategy = Harness(row=row)
    held = position("1000")
    held.avg_px_open = bid.quantize(Decimal("0.01")) - Decimal("0.01")
    strategy.cached.held = [held]
    strategy.cached.history = subdollar_bars(bid, width)
    strategy.cached.latest = subdollar_quote(bid, T0)
    # The harness instrument takes a supplied level in cents.
    strategy.adopt_holding(
        AAA, Price.from_str(str(bid.quantize(Decimal("0.01")) - Decimal("0.01")))
    )
    strategy.test_clock.set_time(T0 + SECOND)
    strategy.observe(str(bid), T0 + SECOND)
    strategy.test_clock.set_time(T0 + 2 * SECOND)
    strategy.observe(str(bid), T0 + 2 * SECOND)
    assert strategy.sent, strategy.said
    order = strategy.sent[0]
    spacing = order.limit_offset - order.trailing_offset
    trigger = order.trigger_price.as_decimal()
    assert spacing % STEP == 0, spacing
    assert STEP <= spacing <= (trigger * PLANNER.chase_cap).quantize(STEP, rounding=ROUND_FLOOR), (
        spacing,
        trigger,
    )
