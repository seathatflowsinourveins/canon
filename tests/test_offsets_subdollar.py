"""The lead's sub-dollar offset contract (2026-11-13), through PRICE offsets.

The limit offset is on tick(entry); its spacing below the trigger is at least one
step and at most CHASE_CAP * (entry - trailing), rounded down to that step.
Every existing result satisfying both bounds is preserved, including its digits.
The trailing distance is unchanged, including S2's distance and a plan's least.
Where the cap, rounded down, is under one step, the spacing is one step (the
lead's ruling of 2026-11-13, 17:27): the old cent pair is never kept over the cap,
and a non-positive trigger is left to the Mover's no-room guards. An off-step S2
distance with no on-step limit between one step and the cap takes the largest
on-step limit within the cap, a positive spacing under one step (a named limit).

NautilusTrader d53849b216e2, crates/execution/src/trailing.rs:104-148,176-221,
uses the same offset type for both distances. Reuse test_mover's real backtest
venue; no network, credential, catalog, ledger or persistent state is used here.
F2 and tier have fixed fractions; OWN1/OWN3 below use a synthetic ATR/entry of
one percent with the registered one/three multiples, not new trading settings.
"""

from decimal import ROUND_FLOOR
from decimal import Decimal

import pytest
from nautilus_trader.model import Money
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.risk import FixedRiskSizer

from canon_trading import mover
from canon_trading.price_step import tick
from canon_trading.protect import PLANNER
from tests.test_mover import AAA
from tests.test_mover import USD
from tests.test_mover import Seam
from tests.test_mover import engine
from tests.test_mover import quote
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover import tape

D = Decimal
ROWS = (
    ("F2", D("0.02"), D("0.005")),
    ("tier", mover.TIER_TRAIL, mover.TIER_TRAIL / 4),
    ("OWN1", D("0.01"), D("0.0025")),
    ("OWN3", D("0.03"), D("0.0075")),
)
SUB_DOLLAR = (
    ("0.0420", ("0.0009", "0.0001", "0.0005", "0.0013"), "0.0017"),
    ("0.4200", ("0.0084", "0.0009", "0.0042", "0.0126"), "0.0169"),
    ("0.6123", ("0.0123", "0.0013", "0.0062", "0.0184"), "0.0246"),
    ("0.9999", ("0.0200", "0.0020", "0.0100", "0.0300"), "0.0401"),
)


@pytest.mark.parametrize(("entry", "trails", "least_trail"), SUB_DOLLAR)
def test_subdollar_limit_is_on_step_with_spacing_between_one_step_and_cap(
    entry, trails, least_trail
):
    price = D(entry)
    step = tick(price)
    failures = []
    for row, trail, through in ROWS:
        for mode, arguments in (
            ("fraction", {}),
            ("least", {"least": price * D("0.04") + D("0.00001")}),
            ("distance", {"distance": D("0.0073"), "least": price}),
        ):
            trailing, limit = mover.offsets(price, trail, through, **arguments)
            bound = ((price - trailing) * PLANNER.chase_cap).quantize(step, rounding=ROUND_FLOOR)
            spacing = limit - trailing
            if limit % step or not step <= spacing <= bound:
                failures.append((row, mode, trailing, limit, spacing, bound))
    assert not failures, failures


@pytest.mark.parametrize(("entry", "trails", "least_trail"), SUB_DOLLAR)
def test_every_trailing_distance_stays_at_its_base_value(entry, trails, least_trail):
    price = D(entry)
    for (_, trail, through), expected in zip(ROWS, trails, strict=True):
        assert str(mover.offsets(price, trail, through)[0]) == expected
        assert (
            str(mover.offsets(price, trail, through, least=price * D("0.04") + D("0.00001"))[0])
            == least_trail
        )
        assert (
            str(mover.offsets(price, trail, through, distance=D("0.0073"), least=price)[0])
            == "0.0073"
        )


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("1.00", (("0.02", "0.03"), ("0.01", "0.02"), ("0.01", "0.02"), ("0.03", "0.04"))),
        ("1.37", (("0.03", "0.04"), ("0.01", "0.02"), ("0.02", "0.03"), ("0.05", "0.07"))),
        ("4.99", (("0.10", "0.13"), ("0.01", "0.02"), ("0.05", "0.07"), ("0.15", "0.19"))),
        ("12.34", (("0.25", "0.32"), ("0.03", "0.04"), ("0.13", "0.17"), ("0.38", "0.48"))),
        ("250.00", (("5.00", "6.25"), ("0.50", "0.63"), ("2.50", "3.13"), ("7.50", "9.38"))),
    ],
)
def test_valid_cent_offsets_keep_every_digit_of_the_base_table(entry, expected):
    for (_, trail, through), pair in zip(ROWS, expected, strict=True):
        actual = mover.offsets(D(entry), trail, through)
        assert tuple(map(str, actual)) == pair


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("0.2965", ("0.0060", "0.01")),
        ("0.6123", ("0.0123", "0.02")),
        ("0.9500", ("0.0190", "0.03")),
    ],
)
def test_valid_subdollar_offsets_also_keep_every_digit(entry, expected):
    assert tuple(map(str, mover.offsets(D(entry), PLANNER.trail, PLANNER.exit_through))) == expected


@pytest.mark.parametrize("entry", [D("0.4200"), D("1.3700")])
def test_excessive_give_is_capped_at_the_bound_rounded_down(entry):
    trailing, limit = mover.offsets(entry, PLANNER.trail, D("0.50"))
    step = tick(entry)
    bound = ((entry - trailing) * PLANNER.chase_cap).quantize(step, rounding=ROUND_FLOOR)
    assert limit % step == 0
    assert limit - trailing == bound


def test_zero_give_still_leaves_one_whole_step_below_the_trigger():
    # Off-step S2 distance: a merely positive gap (the old cent rounding) is too small.
    trailing, limit = mover.offsets(D("1.37"), PLANNER.trail, D(0), distance=D("0.0199"))
    assert trailing == D("0.0199")
    assert limit == D("0.03")
    assert tick(D("1.37")) <= limit - trailing <= D("0.02")


@pytest.mark.parametrize(
    ("entry", "arguments", "expected"),
    [
        ("0.0001", {}, ("0.0001", "0.0002")),
        ("0.0050", {}, ("0.0001", "0.0002")),
        ("0.2000", {"least": D("0.30")}, ("0.3000", "0.3001")),
    ],
)
def test_a_cap_under_one_step_gives_one_step_never_the_old_cent_pair(entry, arguments, expected):
    """The lead's ruling (2026-11-13, 17:27): where the cap is under one step, the offset is one
    step; the old two cents is never kept where it exceeds the cap."""
    trailing, limit = mover.offsets(D(entry), PLANNER.trail, PLANNER.exit_through, **arguments)
    assert (str(trailing), str(limit)) == expected
    assert limit - trailing == tick(D(entry))


def test_an_off_step_distance_with_no_on_step_room_takes_the_largest_limit_within_the_cap():
    """A named limit: S2's 0.4901 at 1.00 leaves no cent limit between one step and the 0.01 cap; the
    largest within the cap is kept, a positive spacing under one step."""
    trailing, limit = mover.offsets(
        D("1.0000"), PLANNER.trail, PLANNER.exit_through, distance=D("0.4901")
    )
    assert (trailing, limit) == (D("0.4901"), D("0.50"))
    assert 0 < limit - trailing <= (D(1) - trailing) * PLANNER.chase_cap


def test_offsets_in_force_preserves_the_floor_and_sized_loss():
    seam = Seam("0.4200")
    slot = seam_slot(seam, bought="0.4200")
    slot.trail_plan = mover.TrailPlan(
        trail=PLANNER.trail,
        exit_through=PLANNER.exit_through,
        chase_cap=PLANNER.chase_cap,
        atr=None,
        spread=D("0.00425"),
        bound="spread floor",
    )
    assert mover.offsets_in_force(slot, D("0.4200"), PLANNER.trail, PLANNER.exit_through, D(2))[
        0
    ] == D("0.0085")
    slot.sized_loss = D("0.0073")
    assert mover.offsets_in_force(slot, D("0.4200"), PLANNER.trail, PLANNER.exit_through, D(2))[
        0
    ] == D("0.0073")


def test_mover_releases_a_subdollar_limit_on_step_and_inside_the_trigger_cap():
    eng = engine()
    s = strategy(max_notional=D(100), flow_share=D(1))
    try:
        orders = run(
            eng,
            s,
            ranker(rank((AAA, "0.25"))),
            [
                *tape(AAA, [0.38, 0.39, 0.40, 0.41, 0.4199], spread=0.0001),
                quote(302.1, 0.4200, 0.4201),
                quote(302.2, 0.4110, 0.4111),
                quote(304, 0.4110, 0.4111),
            ],
        )
        [buy] = [o for o in orders if o.side == OrderSide.BUY]
        assert buy.price.as_decimal() == D("0.4200")
        [sell] = [o for o in orders if o.side == OrderSide.SELL]
        cid = sell.client_order_id.value
        assert cid in s.released
        assert D(s.sale_rows[cid]["trailing_offset"]) == D("0.0084")
        trigger = D(s.sell_triggers[cid])
        released_limit = sell.price.as_decimal()
        step = tick(buy.price.as_decimal())
        assert released_limit % step == 0
        bound = (trigger * PLANNER.chase_cap).quantize(step, rounding=ROUND_FLOOR)
        assert step <= trigger - released_limit <= bound
    finally:
        eng.dispose()


def test_smaller_offset_raises_planned_stop_and_legacy_risk_size():
    # Base cf3eb736: limit offset 0.02, planned stop 0.3920, 357 shares at $10 risk.
    # Intended effect: limit offset 0.0105, stop 0.4013, 534 shares at the same risk.
    entry = D("0.4200")
    old_stop = D("0.3920")
    stop = mover.planned_stop(entry, PLANNER.trail, PLANNER.exit_through, PLANNER.chase_cap)
    assert stop == D("0.4013"), (old_stop, stop)
    eng = engine()
    try:
        sizer = FixedRiskSizer(eng.cache.instrument(AAA))
        before = sizer.calculate(
            Price.from_str(str(entry)), Price.from_str(str(old_stop)), Money(1000, USD), D("0.01")
        )
        after = sizer.calculate(
            Price.from_str(str(entry)), Price.from_str(str(stop)), Money(1000, USD), D("0.01")
        )
        assert before.as_decimal() == D(357)
        assert after.as_decimal() == D(534)
    finally:
        eng.dispose()
