"""S2: the Mover sizes each budget entry through `sizing.plan_for_loss` at one call site, and the stop
it was sized from is the trigger its first trailing sell is sent with (the lead's requirements of
2026-11-12). Offline: the backtest venue of `tests.test_mover`, no credential or network."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType

from canon_trading import mover
from canon_trading import session_window
from canon_trading.mover import PAPER_LOSS_SHARE
from canon_trading.mover import MoverConfig
from canon_trading.mover import share_loss
from canon_trading.price_step import stop_trigger_price
from canon_trading.price_step import up_on_step
from canon_trading.session_window import MoverPick
from canon_trading.session_window import WindowConfig
from canon_trading.sizing import REFUSALS
from canon_trading.sizing import sized_stop
from tests.test_mover import AAA
from tests.test_mover import FALL
from tests.test_mover import RISING
from tests.test_mover import Recording
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover_day import liquid_replay  # noqa: F401 (the partial-fill engine, a fixture)
from tests.test_mover_trail import warmed

D = Decimal


class Sent(Recording):
    """`Recording` that also keeps each trailing sell's trigger as it was submitted."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.sent_triggers: list[Decimal | None] = []

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        if order.order_type == OrderType.TRAILING_STOP_LIMIT:
            trigger = order.trigger_price
            self.sent_triggers.append(None if trigger is None else trigger.as_decimal())
        super().submit_order(order, position_id, client_id, params)


def sent(**changes: object) -> Sent:
    base = strategy(**changes)
    return Sent(base.settings)


def first_buy(orders):
    return next(o for o in orders if o.side == OrderSide.BUY)


def test_a_fill_under_the_limit_puts_the_first_trigger_l_under_the_fill() -> None:
    """The lead's S2 property (a) and (c), 2026-11-12 17:21: the entry is sized for a loss per share L,
    the planned entry less its stepped stop, and the first trigger is L under the fill, not under the
    limit; a share never loses more than L at it."""
    eng = engine()
    s = sent(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), entry_ticks=2)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING + FALL))
    eng.dispose()
    buy = first_buy(orders)
    limit, fill = buy.price.as_decimal(), D(str(buy.avg_px))
    assert fill < limit, (fill, limit)
    sizing = s.entries_sent[0]["sizing"]
    loss = limit - D(sizing["stop"])
    assert D(sizing["stop"]) == sized_stop(limit, D("0.02"))
    assert s.sent_triggers, s.said
    first = s.sent_triggers[0]
    assert first is not None
    assert first == up_on_step(fill - loss)
    assert first < D(sizing["stop"])  # under the limit's stop: the fill is lower
    assert buy.filled_qty.as_decimal() * (fill - first) <= D(sizing["planned_loss"])
    assert D(sizing["loss_per_share"]) == loss
    assert any(f"{sizing['loss_per_share']} under the fill {fill}" in line for line in s.said), (
        s.said
    )


def test_each_exit_keeps_its_trigger_beside_its_fill_so_a_gap_past_l_is_measured() -> None:
    """The lead's ruling of 2026-11-12 17:43: L bounds the loss at the trigger, not through a gap, and
    each exit's record carries its trigger beside its fill, so the loss past L can be measured per exit.
    The emulator releases a triggered sell as a limit without its trigger, so the Mover keeps it."""
    eng = engine()
    s = sent(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), entry_ticks=2)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING + FALL))
    eng.dispose()
    record = s.record()
    sells = [o for o in orders if o.side == OrderSide.SELL and o.filled_qty.as_decimal() > 0]
    assert sells, s.said
    first = s.sent_triggers[0]
    assert first is not None
    for sell in sells:
        cid = sell.client_order_id.value
        assert cid in record["sell_triggers"], (cid, record["sell_triggers"])
        assert [f for f in record["fills"] if f[0] == cid], (cid, record["fills"])
        # the trail only rose from the first trigger, which was L under the fill
        assert D(record["sell_triggers"][cid]) >= first


def test_a_gapped_exit_chased_to_its_fill_keeps_the_released_trigger_beside_that_fill() -> None:
    """The bid gaps through the trigger: the released limit rests, is chased, and the re-sent limit
    fills under the trigger. The fill that closes the exit has the exit's trigger beside it, the one the
    emulator released the sell at, so the gap's loss past L is in the record (code review of the
    trigger record, 2026-11-12)."""
    eng = engine()
    s = sent(
        max_notional=D(10_000), size_from_budget=True, flow_share=D(1), entry_ticks=2, chase_secs=5
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of([*RISING, 10.30, 10.30]))
    eng.dispose()
    record = s.record()
    sells = [o for o in orders if o.side == OrderSide.SELL]
    assert [o.status.name for o in sells] == ["CANCELED", "FILLED"], [str(o) for o in sells]
    released, chased = (o.client_order_id.value for o in sells)
    assert released in record["released"]
    trigger = record["sell_triggers"].get(chased)
    assert trigger is not None, record["sell_triggers"]
    assert trigger == record["sell_triggers"][released]
    fill = [D(f[3]) for f in record["fills"] if f[0] == chased]
    assert fill and max(fill) < D(trigger), (fill, trigger)


def test_a_cent_step_name_whose_give_equals_the_trail_keeps_its_trigger_l_under_the_fill() -> None:
    """The lead's S2 property (b): at $2 on the cent step, a give of four cents through the ask is the
    2% trail; a trigger L under the limit would sit at the bid and sell at once."""
    from tests.test_mover import tape

    closes = [2.00, 2.02, 2.04, 2.06, 2.08, 2.10, 2.12, 2.14]
    eng = engine()
    s = sent(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), entry_ticks=4)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    eng.dispose()
    buy = first_buy(orders)
    limit, fill = buy.price.as_decimal(), D(str(buy.avg_px))
    loss = limit - D(s.entries_sent[0]["sizing"]["stop"])
    assert limit - fill == D("0.04") and loss >= D("0.04"), (limit, fill, loss)
    first = s.sent_triggers[0]
    assert first is not None and first == up_on_step(fill - loss)
    assert first < fill - D("0.01")  # under the bid, so it is not met at once
    assert not [o for o in orders if o.side == OrderSide.SELL and o.order_type == OrderType.LIMIT]


def tape_of(closes):
    from tests.test_mover import tape

    return tape(AAA, closes)


def test_a_two_percent_trail_sizes_to_the_budget_on_the_step() -> None:
    # The lead's ruling (2) of 2026-11-12: the planned loss is the budget's shares at the entry times
    # what each loses at the stepped 2% stop, so the fixed 2% trail buys the budget's shares exactly.
    eng = engine()
    s = strategy(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), protect=False)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING))
    eng.dispose()
    [buy] = [o for o in orders if o.side == OrderSide.BUY]
    price = buy.price.as_decimal()
    sizing = s.entries_sent[0]["sizing"]
    assert buy.quantity.as_decimal() == D(10_000) // price
    assert D(sizing["planned_loss"]) == share_loss(price, D(10_000), PAPER_LOSS_SHARE)
    assert D(sizing["loss_at_stop"]) <= D(sizing["planned_loss"])
    assert sizing["held_by"] == "entry_notional"
    assert s.record()["sizing_bindings"] == {"entry_notional": 1}


def test_a_wider_trail_sizes_down_by_the_planned_loss_and_is_counted() -> None:
    eng = engine()
    s = strategy(
        max_notional=D(10_000),
        size_from_budget=True,
        flow_share=D(1),
        trail=D("0.05"),
        protect=False,
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING))
    eng.dispose()
    [buy] = [o for o in orders if o.side == OrderSide.BUY]
    price = buy.price.as_decimal()
    sizing = s.entries_sent[0]["sizing"]
    stop = D(sizing["stop"])
    assert stop == stop_trigger_price(price * D("0.95"))
    assert buy.quantity.as_decimal() == D(sizing["planned_loss"]) // (price - stop)
    assert buy.quantity.as_decimal() < D(10_000) // price
    assert (sizing["binding"], sizing["held_by"]) == ("planned_loss", "planned_loss")
    assert s.record()["sizing_bindings"] == {"planned_loss": 1}


@pytest.mark.parametrize(
    ("changes", "name"),
    [
        ({"trail": D("0.15")}, "distance_over_ceiling"),
        ({"loss_share": None, "planned_loss": D("0.01")}, "loss_under_one_share"),
        # review finding 1: a figure under one share prices no loss, and no share is what refused
        ({"max_notional": D(1)}, "size_zero"),
    ],
)
def test_each_sizing_refusal_is_counted_by_its_own_name(changes, name) -> None:
    eng = engine()
    s = strategy(
        **{
            "max_notional": D(10_000),
            "size_from_budget": True,
            "flow_share": D(1),
            "protect": False,
        }
        | changes
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING))
    eng.dispose()
    assert not [o for o in orders if o.side == OrderSide.BUY]
    counts = s.record()["sizing_refusals"]
    assert set(counts) == {
        *REFUSALS,
        mover.DISTANCE_UNDER_STEP,
        mover.DISTANCE_UNDER_FLOOR,
        mover.BID_AT_STOP,
    }
    assert counts[name] >= 1
    assert all(count == 0 for other, count in counts.items() if other != name)
    assert any(f"sizing refused: {name}" in why for why in s.dropped), s.dropped


def test_budget_sizing_has_no_default_planned_loss() -> None:
    with pytest.raises(ValueError, match="planned_loss or loss_share is required"):
        MoverConfig(max_notional=D(1000), max_move=D(1), size_from_budget=True)
    with pytest.raises(ValueError, match="not both"):
        MoverConfig(
            max_notional=D(1000),
            max_move=D(1),
            planned_loss=D(10),
            loss_share=D("0.02"),
        )


def test_the_clamp_arm_is_mains_and_the_default() -> None:
    s, slot, latest = warmed(width="0.5")
    assert s.settings.trail_cap == "clamp"
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound, plan.measure) == (D("0.02"), "0.02 cap", D("0.05"))


def test_the_own_arm_keeps_the_names_distance_over_the_cap() -> None:
    s, slot, latest = warmed(
        width="0.5", trail_cap="own", size_from_budget=True, loss_share=PAPER_LOSS_SHARE
    )
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound) == (D("0.05"), "atr")
    assert plan.floor == D(2) * D("0.005") / D(10)
    assert plan.measure == D("0.05")
    assert plan.exit_through == D("0.05") / 4
    assert plan.chase_cap == D("0.05")


def test_the_own_arm_takes_a_spread_floor_over_the_cap_instead_of_refusing() -> None:
    s, slot, latest = warmed(
        spread="0.11", trail_cap="own", size_from_budget=True, loss_share=PAPER_LOSS_SHARE
    )
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound) == (D("0.022"), "spread floor")
    slot.atr.reset()  # cold: the fixed fallback is raised to the floor, not left inside the spread
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound, plan.source) == (
        D("0.022"),
        "spread floor",
        "cold_atr_fallback",
    )


def test_the_refuse_arm_refuses_a_trail_over_the_cap_by_name() -> None:
    s, slot, latest = warmed(width="0.5", trail_cap="refuse")
    with pytest.raises(ValueError, match="over the 0.02 cap; refused"):
        s._entry_trail(slot, D(10), latest)
    s, slot, latest = warmed(width="0.04", trail_cap="refuse")
    assert s._entry_trail(slot, D(10), latest).trail == pytest.approx(D("0.004"))


OWN = {"size_from_budget": True, "loss_share": PAPER_LOSS_SHARE}
"""The `own` arm as a paper window runs it: budget-sized, so the size carries a distance over the cap."""


@pytest.mark.parametrize(("cap", "rows"), [("clamp", {}), ("refuse", {}), ("own", OWN)])
@pytest.mark.parametrize("arm", ["opening", None])
def test_a_cold_atr_entry_trails_at_least_the_spread_floor_at_its_step_in_every_cap_arm(
    cap, rows, arm
) -> None:
    """The lead's O1 of 2026-11-12: at every entry of every arm that tightens, the trail is at least
    the spread floor at that step. A 0.01 fixed trail on a quote 0.15 wide at 10.00 is inside the
    0.015 floor (2 spreads), which the cap allows, so a cold-ATR entry, the opening's included, is
    raised to the floor whatever the cap arm."""
    s, slot, latest = warmed(spread="0.075", trail=D("0.01"), trail_cap=cap, **rows)
    slot.atr.reset()
    slot.run_arm = arm
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound, plan.source) == (
        D("0.015"),
        "spread floor",
        "cold_atr_fallback",
    )
    assert plan.floor == D("0.015")
    assert (plan.exit_through, plan.chase_cap) == (D("0.015") / 4, s.settings.chase_cap)


@pytest.mark.parametrize(
    ("bid", "ask", "trail"),
    [
        ("19.7500", "20.0000", "0.025"),  # #338's second reader (Antigravity), P1-2
        ("9.8500", "10.0000", "0.03"),  # #340's Codex reader, P1-2, and its dsh reader, F1
        ("9.8400", "10.0000", "0.032"),  # #340's Antigravity reader, P1-2
    ],
)
def test_a_cold_opening_entry_takes_the_floor_over_the_fixed_plan(bid, ask, trail) -> None:
    """The second reader of #338 (Antigravity, P1-2), at the lead's inputs of 2026-11-13: at 09:35 New
    York with the ATR cold, the opening arm on, cap own and budget sizing, a 19.75 x 20.00 quote puts
    the spread floor at 0.025, over the fixed 0.02. The opening's cold entry takes the larger, so the
    loss-bound shares are sized for 0.025, not oversized by 25% at 0.02. The readers of #340 gave two
    more such quotes at 10.00."""
    from types import SimpleNamespace

    from nautilus_trader.model import Price

    s, slot, _ = warmed(trail_cap="own", **OWN)
    assert s.settings.trail == D("0.02")
    slot.atr.reset()
    slot.run_arm = "opening"
    quote = SimpleNamespace(bid_price=Price.from_str(bid), ask_price=Price.from_str(ask))
    plan = s._entry_trail(slot, D(ask), quote)
    assert (plan.trail, plan.bound, plan.source) == (
        D(trail),
        "spread floor",
        "cold_atr_fallback",
    )


def stepping_to(last: float, step: float) -> list[float]:
    """Closes rising by `step` to `last`, the close the entry is made at, then on and back down."""
    rising = [round(last - step * (4 - i), 4) for i in range(5)]
    return rising + [round(last + step * i, 4) for i in (1, 2, 3)] + [1.00, 0.97]


@pytest.mark.parametrize(
    ("closes", "spread", "trail", "price", "least", "held"),
    [
        # #338's second reader (Antigravity), P1-1: an ask of 1.0000, the bid 0.9920
        (stepping_to(0.996, 0.01), 0.004, "0.01", "1.0000", "0.016", "0.02"),
        # #340's Codex reader, P1-1: the bid 0.9940, a floor of 0.012
        (stepping_to(0.997, 0.01), 0.003, "0.01", "1.0000", "0.012", "0.02"),
        # #340's Antigravity reader, P1-1: the bid 0.9910, a floor of 0.018
        (stepping_to(0.9955, 0.01), 0.0045, "0.01", "1.0000", "0.018", "0.02"),
        # the same reader's second input: a 0.008 floor whose stop, 0.9920, rounded down to nothing
        (stepping_to(0.998, 0.008), 0.002, "0.005", "1.0000", "0.008", "0.01"),
        # the A3+O reader's P2: at 1.03 the fraction 0.04 / 1.03 is not exact in a Decimal
        (stepping_to(1.022, 0.01), 0.008, "0.01", "1.0300", "0.032", "0.04"),
    ],
)
def test_an_entry_whose_stop_crosses_a_dollar_holds_at_least_the_spread_floor_and_its_loss(
    closes, spread, trail, price, least, held
) -> None:
    """The second reader of #338 (Antigravity, P1-1), the lead's cross-dollar property of 2026-11-13:
    at an ask of 1.0000 with the bid 0.9920 the spread floor is 0.016 a share. The stop sized from it,
    0.9840, is on the sub-dollar step, and L rounded down onto the entry's cent step was 0.01, under
    the floor. L on the entry's step is now at or above the floor, 0.02, and the size is taken for it,
    so the loss at the stop stays within the plan. The readers of #340 gave three more such inputs.
    The stop the size is taken again at is L under the entry exactly, so the record's stop and its loss
    per share agree (the A3+O reader, P2: at 1.03 the stop was 0.9899 for an L of 0.04)."""
    from tests.test_mover import tape

    eng = engine()
    s = sent(
        max_notional=D(1000),
        flow_share=D(1),
        trail_mode="atr",
        trail_cap="own",
        trail=D(trail),
        entry_ticks=0,
        **OWN,
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes, spread=spread))
    eng.dispose()
    buy = first_buy(orders)
    assert buy.price.as_decimal() == D(price)
    price = D(price)
    entry = s.entries_sent[0]
    floor = D(entry["spread_floor"]) * price
    assert floor == D(least)
    sizing = entry["sizing"]
    loss = D(sizing["loss_per_share"])
    assert loss >= floor
    assert loss == price - D(sizing["stop"]) == D(held)
    # the record names the fraction the size was taken at, which gives its stop (the high review, nit 2)
    assert D(sizing["stop"]) == sized_stop(price, D(sizing["sized_trail"]))
    assert D(sizing["sized_trail"]) > D(sizing["trail"])
    assert D(sizing["loss_at_stop"]) <= D(sizing["planned_loss"])
    assert buy.quantity.as_decimal() * loss <= D(sizing["planned_loss"])
    first = s.sent_triggers[0]
    assert first is not None and D(str(buy.avg_px)) - first >= floor


def test_the_fixed_arm_keeps_its_trail_inside_the_spread_floor() -> None:
    """O1 covers the arms that select a distance; the fixed arm is the default and does not move."""
    s, slot, latest = warmed("fixed", spread="0.075", trail=D("0.01"))
    plan = s._entry_trail(slot, D(10), latest)
    assert (plan.trail, plan.bound) == (D("0.01"), "fixed")


@pytest.mark.parametrize(
    ("cap", "rows", "width", "spread", "cause", "distance"),
    [
        ("clamp", {}, "0.04", "0.11", "floor_over_cap", "0.022"),
        ("refuse", {}, "0.04", "0.11", "floor_over_cap", "0.022"),
        ("own", {}, "0.04", "0.11", "floor_over_cap", "0.022"),
        ("refuse", {}, "0.5", "0.005", "distance_over_cap", "0.05"),
        ("own", {}, "0.5", "0.005", "distance_over_cap", "0.05"),
    ],
)
def test_an_entry_the_cap_forbids_is_refused_with_its_cause_and_distance(
    cap, rows, width, spread, cause, distance
) -> None:
    """The lead's O2 of 2026-11-12: where the cap forbids the distance, the entry is refused by name,
    and the refusal carries its cause and the distance the cap forbade, for the run's record."""
    s, slot, latest = warmed(width=width, spread=spread, trail_cap=cap, **rows)
    with pytest.raises(mover.DistanceRefusal) as refused:
        s._entry_trail(slot, D(10), latest)
    assert (refused.value.cause, refused.value.distance) == (cause, D(distance))
    slot.atr.reset()  # cold, the same floor is refused
    if cause == "floor_over_cap":
        with pytest.raises(mover.DistanceRefusal, match="spread floor exceeds the 0.02 cap"):
            s._entry_trail(slot, D(10), latest)


def test_a_refused_entry_is_written_once_in_the_runs_refusals_with_its_numbers() -> None:
    """The lead's record rule 1 of 2026-11-12: an entry refused for its distance is one row of the
    run's `refusals`, with its cause, its distance, the cap and its time, and no line of `dropped`.
    Written once: a name refused again at each later decision keeps its one row for that cause, which
    counts the decisions and names the last, as batch B's `reentry_max` row does.
    A quote 0.24 wide at about 10.20 puts the spread floor near 0.047, over the 0.02 cap."""
    from tests.test_mover import tape

    eng = engine()
    s = strategy(trail_mode="atr", atr_n=3)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0.12))
    eng.dispose()
    assert not [o for o in orders if o.side == OrderSide.BUY]
    record = s.record()
    refusals = record["refusals"]
    assert len(refusals) == 1, refusals
    (row,) = refusals
    assert set(row) == {
        "when_ns",
        "instrument_id",
        "reason",
        "distance",
        "cap",
        "decisions",
        "last_ns",
    }
    assert (row["instrument_id"], row["reason"], row["cap"]) == (str(AAA), "floor_over_cap", "0.02")
    assert D(row["distance"]) > D("0.02") and isinstance(row["when_ns"], int)
    assert row["decisions"] > 1 and row["last_ns"] > row["when_ns"]
    assert not [why for why in record["dropped"] if "cap" in why], record["dropped"]


def test_an_unknown_cap_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="trail_cap"):
        strategy(trail_cap="widen")


def test_an_entry_whose_bid_is_at_the_sized_stop_is_refused_by_name() -> None:
    # A 2% trail on a quote 30 cents wide (about 2.8%): the bid is over the planned sale but already
    # under the stop the entry would be sized for and sent with, which would sell it at once.
    from tests.test_mover import tape

    eng = engine()
    s = strategy(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), protect=False)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0.15))
    eng.dispose()
    assert not [o for o in orders if o.side == OrderSide.BUY]
    assert any("under the ask" in why and "wider than the trail" in why for why in s.dropped)
    # review finding 6: counted with the sizing refusals, by its own name
    counts = s.record()["sizing_refusals"]
    assert counts[mover.BID_AT_STOP] >= 1
    assert all(count == 0 for name, count in counts.items() if name != mover.BID_AT_STOP)


def test_an_error_that_is_not_a_sizing_refusal_propagates_and_is_not_counted(monkeypatch) -> None:
    """Review finding 3: any ValueError's text became a key of the refusal counts."""

    def upstream(**_kwargs):
        raise ValueError("an upstream fault, not a refusal")

    monkeypatch.setattr(mover, "plan_for_loss", upstream)
    eng = engine()
    s = strategy(max_notional=D(10_000), size_from_budget=True, flow_share=D(1), protect=False)
    # named, not counted as a refusal, and the step goes on (third review finding 5)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape_of(RISING))
    eng.dispose()
    assert not [o for o in orders if o.side == OrderSide.BUY]
    assert not any("sizing refused" in why for why in s.dropped)
    assert any(
        "sizing failed, not a refusal: ValueError: an upstream fault" in why for why in s.dropped
    ), s.dropped
    assert set(s.sizing_refusals) == {
        *REFUSALS,
        mover.DISTANCE_UNDER_STEP,
        mover.DISTANCE_UNDER_FLOOR,
        mover.BID_AT_STOP,
    }
    assert not any(s.sizing_refusals.values())


def test_the_planned_loss_and_the_sized_stop_are_one_stop_computation() -> None:
    """Review finding 9: `share_loss` priced its stop with its own formula beside `plan_for_loss`'s."""
    from canon_trading.sizing import sized_stop

    for entry in (D("0.7263"), D("10.42"), D("123.45"), D("4999.99")):
        shares = D(1000) // entry
        assert share_loss(entry, D(1000), D("0.02")) == shares * (
            entry - sized_stop(entry, D("0.02"))
        )


@pytest.mark.parametrize("account", ["windows", "tests"])
def test_paper_a_and_b_measure_the_planned_loss_from_two_percent(account) -> None:
    assert session_window.window_loss_share(account) == D("0.02")


@pytest.mark.parametrize("account", ["unknown", "protect", "live"])
def test_any_other_account_refuses_the_window_with_no_default_loss(account) -> None:
    with pytest.raises(session_window.WindowRefused, match="owner's figure"):
        session_window.window_loss_share(account)


def test_the_window_flags_reach_the_window_config() -> None:
    args = session_window._args(
        [
            "--mover",
            "--session",
            "extended",
            "--trail-mode",
            "atr",
            "--trail-cap",
            "own",
            "--atr-k",
            "3",
        ]
    )
    assert (args.trail_mode, args.trail_cap, args.atr_k) == ("atr", "own", D(3))
    plain = session_window._args(["--mover", "--session", "extended"])
    assert (plain.trail_mode, plain.trail_cap, plain.atr_k, plain.arm) == (None, None, None, None)
    assert session_window._mover_trail(plain) == {
        **session_window.TRAIL_ARMS["OWN1"],
        "arm": "OWN1",
        "arm_default": True,
    }


def test_a_bare_window_namespace_does_not_show_the_fixed_rows_it_will_not_run() -> None:
    """Review finding 3 (/code-review high, F-1): `_args` filled the fixed rows into a bare Mover
    window's namespace, which then runs OWN1; a flag left out stays unset, resolved by `_mover_trail`."""
    plain = session_window._args(["--mover", "--session", "extended"])
    assert (plain.trail_mode, plain.trail_cap, plain.atr_k) == (None, None, None)


@pytest.mark.parametrize(
    "flags",
    [
        ["--trail-mode", "atr"],
        ["--trail-cap", "own"],
        ["--atr-k", "3"],
        ["--trail-mode", "fixed"],
        ["--trail-cap", "clamp"],
        ["--atr-k", "1"],
    ],
)
def test_a_trail_flag_without_mover_is_refused(flags) -> None:
    """Review finding 2 (/code-review high, F-1): a trail flag given at its fixed row opts a Mover
    window out of `DEFAULT_ARM`, so it is given like any other value, and refused without Mover."""
    with pytest.raises(SystemExit):
        session_window._args(["--session", "regular", *flags])


def test_an_atr_multiple_that_is_not_registered_is_refused() -> None:
    with pytest.raises(SystemExit):
        session_window._args(["--mover", "--session", "extended", "--atr-k", "4"])


def test_the_window_passes_its_trail_rows_and_loss_share_to_the_mover(tmp_path) -> None:
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        D(1500),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        D(1500),
    )
    node, s, _ = session_window.window_node(
        WindowConfig(
            symbol="LLL",
            session="extended",
            strategy="Mover",
            mover=True,
            log_dir=tmp_path,
            trail_mode="atr",
            trail_cap="own",
            atr_k=D(3),
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="rows",
        feed=session_window.MOVER_PARTS["after-hours"].feed,
        part="after-hours",
        pick=pick,
        ends=ends,
        entry_notional=D(20000),
        loss_share=PAPER_LOSS_SHARE,
    )
    try:
        assert isinstance(s, mover.Mover)
        settings = s.settings
        assert (settings.trail_mode, settings.trail_cap, settings.atr_k) == ("atr", "own", D(3))
        assert (settings.loss_share, settings.planned_loss) == (D("0.02"), None)
        rows = s.record()["rows"]
        assert (rows["trail_mode"], rows["trail_cap"], rows["atr_k"]) == ("atr", "own", "3")
    finally:
        node.dispose()


def test_a_mover_window_without_a_loss_share_refuses_before_the_builder(monkeypatch, tmp_path):
    monkeypatch.setattr(
        session_window, "alpaca_paper_builder", lambda *a, **k: pytest.fail("builder called")
    )
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick("LLL", D(1500), {"at": ends.isoformat(), "inputs": {"move": 2}}, D(1500))
    with pytest.raises(RuntimeError, match="loss share"):
        session_window.window_node(
            WindowConfig(symbol="LLL", session="extended", strategy="Mover", mover=True,
                         log_dir=tmp_path),
            wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
            log_name="none",
            feed=session_window.MOVER_PARTS["after-hours"].feed,
            part="after-hours",
            pick=pick,
            ends=ends,
            entry_notional=D(20000),
        )  # fmt: skip


def test_sizing_rows_reach_the_record_without_an_entry() -> None:
    s = strategy(max_notional=D(1000), size_from_budget=True)
    record = s.record()
    assert record["sizing_refusals"] == dict.fromkeys(
        (*REFUSALS, mover.DISTANCE_UNDER_STEP, mover.DISTANCE_UNDER_FLOOR, mover.BID_AT_STOP), 0
    )
    assert record["sizing_bindings"] == {}
    assert record["rows"]["loss_share"] == "0.02"


@pytest.mark.parametrize(
    ("arm", "flags"),
    [
        ("F2", []),
        ("OWN1", ["--trail-mode", "atr", "--trail-cap", "own"]),
        ("OWN3", ["--trail-mode", "atr", "--trail-cap", "own", "--atr-k", "3"]),
    ],
)
def test_each_registered_arm_is_named_with_its_rows(arm, flags, tmp_path) -> None:
    args = session_window._args(["--mover", "--session", "extended", "--arm", arm, *flags])
    config = WindowConfig(
        symbol="LLL",
        session="extended",
        strategy="Mover",
        mover=True,
        log_dir=tmp_path,
        **session_window._mover_trail(args),
    )
    assert config.arm == arm
    assert session_window.WindowReport("extended", "LLL", started="arm", arm=config.arm).arm == arm


@pytest.mark.parametrize(
    ("arm", "rows"),
    [
        ("OWN3", {"trail_mode": "atr", "trail_cap": "own"}),
        ("OWN1", {"trail_mode": "atr", "trail_cap": "clamp"}),
        ("F2", {"trail_mode": "atr", "trail_cap": "own"}),
    ],
)
def test_an_arm_whose_rows_are_not_the_windows_is_refused_by_name(arm, rows, tmp_path) -> None:
    with pytest.raises(ValueError, match=f"arm {arm} does not match"):
        WindowConfig(
            symbol="LLL", session="extended", strategy="Mover", mover=True, log_dir=tmp_path,
            arm=arm, **rows,
        )  # fmt: skip


def test_an_arm_is_mover_only_and_registered(tmp_path) -> None:
    with pytest.raises(ValueError, match="arm must be one of"):
        WindowConfig(symbol="LLL", session="extended", strategy="Mover", mover=True,
                     log_dir=tmp_path, arm="OWN5")  # fmt: skip
    with pytest.raises(SystemExit):
        session_window._args(["--session", "regular", "--arm", "F2"])


@pytest.mark.usefixtures("liquid_replay")
def test_the_trigger_never_falls_and_holds_l_under_the_average_fill_across_partial_fills() -> None:
    """The lead's S2 property (d), with L4's note 4: a partial entry fill is protected L under its fill,
    and the remake for the rest is L under the average fill or the trigger reached, whichever is
    higher; no trigger falls, and no share loses more than L at its trigger."""
    from tests.test_mover_day import RISING as DAY_RISING
    from tests.test_mover_day import generated_instrument
    from tests.test_mover_day import generated_quote
    from tests.test_mover_day import generated_tape
    from tests.test_mover_day import replay
    from tests.test_mover_day import stage_one

    data = generated_tape(DAY_RISING[:5])
    data[-1] = generated_quote(300 - 1e-9, "10.39", "10.41", ask_size=1)
    data += [
        generated_quote(302, "10.40", "10.42"),
        generated_quote(304, "10.40", "10.43", ask_size=2),
        generated_quote(306, "10.39", "10.60"),
        generated_quote(308, "10.17", "10.60"),
        generated_quote(310, "10.00", "10.60"),
    ]
    result = replay(
        generated_instrument(),
        data,
        D(8),
        wind_down_at=None,
        max_notional=D("31.5"),
        size_from_budget=True,
        loss_share=PAPER_LOSS_SHARE,
        flow_share=D(1),
        entry_ticks=2,
    )
    assert not [b for b in stage_one(result) if "trigger fell" in b], result.report()
    [buy] = [o for o in result.orders if o.side == "BUY"]
    assert buy.price is not None
    loss = buy.price - sized_stop(buy.price, D("0.02"))
    bought: list[tuple[D, D]] = []
    sells = [o for o in result.orders if o.side == "SELL" and result.triggers.get(o.id)]
    assert len(sells) >= 2, result.report()  # a partial entry, then a remake for all that is held
    for sell in sells:
        filled = [
            (D(q), D(p)) for i, side, q, p, t in result.fills if side == "BUY" and t <= sell.ts_init
        ]
        held = sum((q for q, _ in filled), D(0))
        average = sum((q * p for q, p in filled), D(0)) / held
        first = result.triggers[sell.id][0]
        assert first >= up_on_step(average - loss), (sell.id, first, average, loss)
        assert held * (average - first) <= held * loss, result.report()
        bought.append((held, first))
    assert [h for h, _ in bought] == sorted(h for h, _ in bought)
    assert [f for _, f in bought] == sorted(f for _, f in bought)


def test_the_own_arm_without_budget_sizing_refuses_a_trail_over_the_cap() -> None:
    """Second review finding 6: off budget sizing nothing carries a trail over the cap, and nothing
    applies the 0.10 ceiling, so `own` refuses there by name."""
    s, slot, latest = warmed(width="0.5", trail_cap="own")
    with pytest.raises(ValueError, match="trail_cap own needs size_from_budget"):
        s._entry_trail(slot, D(10), latest)
    s, slot, latest = warmed(spread="0.11", trail_cap="own")
    with pytest.raises(ValueError, match="spread floor exceeds the 0.02 cap"):
        s._entry_trail(slot, D(10), latest)
    slot.atr.reset()  # the cold fallback is not raised to a floor over the cap either
    with pytest.raises(ValueError, match="spread floor exceeds the 0.02 cap"):
        s._entry_trail(slot, D(10), latest)


def test_a_cold_fallback_raised_to_the_spread_floor_is_not_ratcheted() -> None:
    """Second review finding 7: the own arm's cold fallback is bound by the spread floor, and the
    ratchet, which leaves a fallback's offsets alone, tightened it."""
    s, slot, latest = warmed(
        "ratchet",
        spread="0.11",
        trail_cap="own",
        size_from_budget=True,
        loss_share=PAPER_LOSS_SHARE,
    )
    slot.atr.reset()
    plan = s._entry_trail(slot, D(10), latest)
    assert plan.source == "cold_atr_fallback"
    assert mover.ratchet_trail(plan, D(10), D(20), D("0.01"), s.settings) is None


def test_a_fill_with_no_room_for_l_under_it_trails_from_the_fill() -> None:
    """Second review finding 5: with no room for L under the fill there is no anchor, and the trailing
    offset is the trail of the fill, never L at or over the bid."""
    assert mover.fill_trigger(D("0.0003"), D("0.0004")) is None
    assert mover.fill_trigger(D("10.03"), D("0.21")) == D("9.82")
    assert mover.fill_trigger(D("10.0233"), D("0.21")) == D(
        "9.82"
    )  # up onto the step: <= L a share


def test_l_is_held_on_the_entrys_step_where_the_stop_crosses_a_dollar() -> None:
    """Third review finding 2: an entry at $1.01 with a 2% trail is sized against 0.9898, a sub-dollar
    stop, so L was 0.0202, off the cent step a bid above $1 trails by; a remake then stepped the reached
    trigger down. L is held on the entry's step, rounded down, so the loss stays within the plan."""
    from canon_trading.price_step import tick

    price = D("1.01")
    stop = sized_stop(price, D("0.02"))
    assert stop == D("0.9898")
    assert (price - stop).quantize(tick(price), rounding="ROUND_FLOOR") == D("0.02")
    eng = engine()
    from tests.test_mover import tape

    closes = [0.97, 0.98, 0.99, 1.00, 1.01, 1.01, 1.01, 1.01]
    s = sent(max_notional=D(10_000), size_from_budget=True, flow_share=D(1))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes, spread=0.005))
    eng.dispose()
    buy = first_buy(orders)
    sizing = s.entries_sent[0]["sizing"]
    loss = D(sizing["loss_per_share"])
    assert loss == loss.quantize(tick(buy.price.as_decimal()))
    assert D(sizing["stop"]) < 1 <= buy.price.as_decimal(), (sizing["stop"], buy.price)
    assert loss < buy.price.as_decimal() - D(sizing["stop"])


def test_the_bars_replay_refuses_budget_sizing_by_name() -> None:
    """Third review finding 4: the replay's fixed rows clear the planned loss, so a budget-sized trial
    failed on MoverConfig's own check; it is refused by name instead."""
    from canon_trading.tuning_contract import check_bars_replay_modes

    settings = MoverConfig(
        max_notional=D(1000), max_move=D(1), size_from_budget=True, loss_share=PAPER_LOSS_SHARE
    )
    with pytest.raises(ValueError, match="bars replay refuses size_from_budget"):
        check_bars_replay_modes(settings)


@pytest.mark.parametrize("arm", [{"breach_count": 2}, {"high_count": 2}])
def test_persistence_with_budget_sizing_is_refused_by_name(arm: dict[str, int]) -> None:
    """Persistence (ADR 0119) holds its level at the fixed trail's distance from the fill, not at the L
    the budget sized for, so the two are refused together rather than size for one distance and stop at
    another (the lead's S2 property of 2026-11-12, 17:21)."""
    from nautilus_trader.model import TriggerType

    with pytest.raises(ValueError, match="size_from_budget is refused with persistence"):
        strategy(
            size_from_budget=True,
            loss_share=PAPER_LOSS_SHARE,
            trigger=TriggerType.LAST_PRICE,
            confirm_gap_ms=100,
            **arm,
        )


def test_a_sale_for_a_met_trigger_and_its_chased_resend_keep_that_trigger() -> None:
    """A trailing sell is remade from a trigger of 10.38 and denied; the bid is then 10.20, under it,
    so the next step sells by limit for the met trigger. That sale, and the limit re-sent after the bid
    falls and the chase cancels it, each have 10.38 beside them in the record (code review of the
    trigger record, 2026-11-12)."""
    from typing import Any
    from typing import cast

    from tests.test_mover import SECOND
    from tests.test_mover import T0
    from tests.test_mover import Seam
    from tests.test_mover import seam_slot

    seam = Seam("10.50")
    cast(Any, seam).recent = 0
    slot = seam_slot(seam)
    slot.trail_trigger = D("10.38")
    cast(Any, seam)._protect(slot, D(3), T0)
    remade = seam.sent[-1]
    seam.refuse(remade)
    seam.bid = "10.20"
    cast(Any, seam)._protect(slot, D(3), T0 + 5 * SECOND)
    met = seam.sent[-1]
    assert met.kind == "LIMIT" and slot.exiting
    assert D(seam.sell_triggers[met.client_order_id.value]) == D("10.38")
    cast(Any, seam)._protect(
        slot, D(3), T0 + 6 * SECOND
    )  # seen working: its floor, the chase's clock
    seam.bid = "10.10"
    cast(Any, seam)._protect(slot, D(3), T0 + 12 * SECOND)
    assert seam.canceled == [met.client_order_id.value]
    met.is_closed, met.is_open = True, False
    cast(Any, seam)._protect(slot, D(3), T0 + 13 * SECOND)
    chased = seam.sent[-1]
    assert chased is not met and chased.kind == "LIMIT"
    assert D(seam.sell_triggers[chased.client_order_id.value]) == D("10.38")


def test_a_trailing_sell_canceled_to_be_remade_records_the_trigger_it_had_reached() -> None:
    """More is held than the trailing sell covers, so it is canceled to be made again for all of it.
    The trigger the emulator had moved it to is kept beside its client order ID."""
    from typing import Any
    from typing import cast

    from nautilus_trader.model import Price

    from tests.test_mover import T0
    from tests.test_mover import Seam
    from tests.test_mover import seam_slot

    seam = Seam("10.50")
    slot = seam_slot(seam)
    cast(Any, seam)._protect(slot, D(3), T0)
    trailing = seam.sent[-1]
    assert trailing.kind == "TRAILING"
    trailing.trigger_price = Price.from_str("10.4000")  # where the emulator had moved it
    cast(Any, seam)._protect(slot, D(5), T0 + 1)
    assert seam.canceled == [trailing.client_order_id.value]
    assert D(seam.sell_triggers[trailing.client_order_id.value]) == D("10.40")


@pytest.mark.parametrize("exit_trigger", [D("10.38"), None])
def test_a_sale_the_mover_did_not_send_takes_the_exits_trigger_at_its_fill(exit_trigger) -> None:
    """Upstream's market exit at the wind-down or the managed stop sells with an order the Mover never
    sent. Its fill takes the exit's trigger when the exit was decided at one, and nothing when no
    trigger was crossed; the handler reads plain attributes only."""
    from types import SimpleNamespace
    from typing import Any
    from typing import cast

    from nautilus_trader.model import Price
    from nautilus_trader.model import Quantity

    from canon_trading.mover import Mover
    from tests.test_mover import T0
    from tests.test_mover import Seam
    from tests.test_mover import seam_slot

    seam = Seam("10.50")
    slot = seam_slot(seam)
    slot.exit_trigger = exit_trigger
    host = cast(Any, seam)
    host.slots, host.fills, host.fills_to_report = {AAA: slot}, [], []
    fill = SimpleNamespace(
        client_order_id=SimpleNamespace(value="MX-1"),
        order_side=OrderSide.SELL,
        instrument_id=AAA,
        last_qty=Quantity.from_int(3),
        last_px=Price.from_str("10.05"),
        ts_event=T0,
    )
    Mover.on_order_filled(host, fill)
    assert seam.sell_triggers.get("MX-1") == (None if exit_trigger is None else str(exit_trigger))
    assert [f[0] for f in host.fills] == ["MX-1"]
