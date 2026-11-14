"""Red cases for the two-quote sale's price rule, leg size and floor (the table's seventh form).

Contract: sale_table.json's `price_rule` (a leg is priced from the newest usable quote stored at its
send, never under its floor, never above the level, never above the leg before it), its predicate
`leg_positive` (every leg is the uncovered quantity read at its send) and its properties
`no_leg_rests_above_a_falling_market_in_silence` and `a_leg_and_its_floor_agree`.
The stand-ins are test_owner_protector's. No broker, credential, paper state, catalog or ledger.
Each case steps the timer after an order event, so it holds whether the event is judged in its
handler or in the next step.
"""

from __future__ import annotations

import gc
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Self

import pytest

from canon_trading import sale_table
from canon_trading.owner_protector import FLIGHT_SECS
from canon_trading.owner_protector import STEP_NS
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c
from tests.test_owner_protector_chase import expected_price

engine_checks = c.engine_checks
isolated_level_books = c.isolated_level_books
alpaca_stream_lock = c.alpaca_stream_lock


def step(strategy):
    """One timer step at the clock's present time."""
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))


def deliver(strategy, kind, **values):
    strategy.deliver(kind, **values)
    step(strategy)


def one_sided(strategy, bid, at):
    """A quote with no size on its ask: not usable, so it neither counts nor prices."""
    tick = h.QuoteTick(
        h.AAA,
        h.Price.from_str(bid),
        h.Price.from_str(str(Decimal(bid) + Decimal("0.02"))),
        h.Quantity.from_int(100),
        h.Quantity.from_int(0),
        at,
        at,
    )
    strategy.cached.latest = tick
    strategy.test_clock.set_time(max(at, strategy.test_clock.timestamp_ns()))
    strategy.on_quote(tick)


def ruled(strategy, bid, previous):
    """The price rule's answer for a later leg, from the holding's own level and floor."""
    slot = strategy.slots[h.AAA]
    price = expected_price(bid, slot.sale.floor)
    return min(price, slot.level.as_decimal(), previous)


def repricing(strategy):
    """Our first leg works and a counted quote under the level has asked its re-price cancel."""
    c.first_leg(strategy)
    first = strategy.sent[-1].price.as_decimal()
    strategy.test_clock.set_time(h.T0 + 60 * h.SECOND)
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    assert c.row(strategy).row == "PCR", c.row(strategy)
    return first


def test_a_next_leg_is_priced_from_usable_quotes_inside_the_gap():
    """A quote above the level is counted while the re-price cancel is pending; the market then
    falls inside the gap, where quotes are usable and not counted. The next leg follows them."""
    strategy = c.counted(confirm_gap_ms=1000)
    first = repricing(strategy)
    strategy.observe("10.40", h.T0 + 62 * h.SECOND)
    for n, bid in enumerate(("9.70", "9.60", "9.50"), start=1):
        strategy.observe(bid, h.T0 + 62 * h.SECOND + n * 200 * h.SECOND // 1000)
    deliver(strategy, "Canceled")
    assert c.row(strategy).row == "FL" and len(strategy.sent) == 2
    price = strategy.sent[-1].price.as_decimal()
    assert price == ruled(strategy, "9.50", first), (price, strategy.slots[h.AAA].sale.floor)
    assert price < first


def test_a_next_leg_after_only_one_sided_quotes_is_never_above_the_leg_before_it():
    """The same pending re-price; after the counted quote above the level only one-sided quotes
    arrive. None is usable, so the newest usable quote is the one above the level: the next leg is
    held at the leg it replaces, never above it and never above the level."""
    strategy = c.counted()
    first = repricing(strategy)
    strategy.observe("10.40", h.T0 + 62 * h.SECOND)
    for n, bid in enumerate(("9.70", "9.60", "9.50"), start=1):
        one_sided(strategy, bid, h.T0 + (62 + n) * h.SECOND)
    deliver(strategy, "Canceled")
    assert c.row(strategy).row == "FL" and len(strategy.sent) == 2
    price = strategy.sent[-1].price.as_decimal()
    level = strategy.slots[h.AAA].level.as_decimal()
    assert price == first and price <= level, (price, first, level)


def test_a_reprice_answered_before_the_next_quote_steps_down_to_the_quote_that_asked_it():
    strategy = c.counted()
    first = repricing(strategy)
    deliver(strategy, "Canceled")
    price = strategy.sent[-1].price.as_decimal()
    assert price == ruled(strategy, "9.80", first) and price < first, (price, first)


def test_shares_added_while_our_leg_works_are_in_the_next_leg():
    strategy = c.counted()
    c.first_leg(strategy)
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity = Decimal(15), h.Quantity.from_int(15)
    strategy.test_clock.set_time(h.T0 + 3 * h.SECOND)
    step(strategy)
    strategy.test_clock.set_time(h.T0 + 60 * h.SECOND)
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    assert c.row(strategy).row == "PCR", c.row(strategy)
    deliver(strategy, "Canceled")
    assert strategy.sent[-1].quantity.as_decimal() == 15, strategy.sent[-1].quantity


def test_a_leg_sent_again_under_a_fallen_bid_takes_the_floor_and_is_then_repriced():
    """The venue refuses the first leg; the bid falls under the episode's floor during the wait.
    The leg sent again rests at the floor, the floor's wait runs and releases, and the leg then
    follows the market down: it never rests above a falling market for good."""
    strategy = c.counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    first = strategy.sent[-1].price.as_decimal()
    deliver(
        strategy,
        "Rejected",
        reason="synthetic venue refusal",
        ts_event=strategy.clock.timestamp_ns(),
    )
    assert c.row(strategy).row == "RW", c.row(strategy)
    strategy.observe("8.90", h.T0 + 62 * h.SECOND)
    step(strategy)
    assert len(strategy.sent) == 2, c.row(strategy)
    floor = strategy.slots[h.AAA].sale.floor
    second = strategy.sent[-1].price.as_decimal()
    assert floor is not None and second == floor < first, (second, floor)
    deliver(strategy, "Accepted")
    at = h.T0 + 62 * h.SECOND
    for bid in ("8.80", "8.50", "8.00", "7.50", "7.00", "6.50", "6.00"):
        at += 120 * h.SECOND
        strategy.observe(bid, at)
        step(strategy)
        if strategy.cached.working[-1].status == h.OrderStatus.PENDING_CANCEL:
            deliver(strategy, "Canceled")
            deliver(strategy, "Accepted")
    assert strategy.canceled, "the leg at the floor was never re-priced"
    assert strategy.sent[-1].price.as_decimal() < second


def alerts(strategy):
    return [line for line in strategy.said if line.startswith("ALERT:")]


def records(strategy):
    return [
        json.loads(line.split("sale record ", 1)[1])
        for line in strategy.said
        if "sale record " in line
    ]


def grow(strategy, quantity=15):
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity = Decimal(quantity), h.Quantity.from_int(quantity)


def test_added_shares_less_foreign_cover_are_all_in_the_next_leg():
    strategy = c.counted()
    c.first_leg(strategy)
    grow(strategy)
    strategy.cached.working.append(h.working_sell(qty="3", filled="0"))
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    # A newly placed foreign sell invalidates the chase; its cancel needs a new count.
    deliver(strategy, "Canceled", leg=strategy.cached.working[0])
    strategy.observe("9.79", h.T0 + 62 * h.SECOND)
    strategy.observe("9.78", h.T0 + 63 * h.SECOND)
    assert strategy.sent[-1].quantity.as_decimal() == 12
    deliver(strategy, "Accepted", leg=strategy.cached.working[-1])
    grow(strategy, 18)
    strategy.observe("9.70", h.T0 + 124 * h.SECOND)
    deliver(strategy, "Canceled", leg=strategy.cached.working[-1])
    assert strategy.sent[-1].quantity.as_decimal() == 15


@pytest.mark.parametrize("quantity", [10, 15])
def test_floor_wait_names_added_shares_once_and_only_when_uncovered(quantity):
    strategy = c.counted()
    c.first_leg(strategy)
    strategy.observe("8.90", h.T0 + 61 * h.SECOND)
    deliver(strategy, "Canceled")
    deliver(strategy, "Accepted")
    grow(strategy, quantity)
    strategy.said.clear()
    for second in (62, 63, 64):
        strategy.observe("8.80", h.T0 + second * h.SECOND)
    costs = [line for line in alerts(strategy) if "next leg" in line]
    assert len(costs) == (1 if quantity == 15 else 0), strategy.said
    if costs:
        assert "15" in costs[0] and "10" in costs[0]
    assert strategy.slots[h.AAA].sale.floor_since is not None


def test_an_unanswered_cancel_is_told_once_then_ack_sends_in_a_step():
    strategy = c.counted()
    repricing(strategy)
    strategy.said.clear()
    for second in (122, 123, 124):
        strategy.test_clock.set_time(h.T0 + second * h.SECOND)
        step(strategy)
    assert len(alerts(strategy)) == 1 and "cancel" in alerts(strategy)[0]
    assert c.row(strategy).row == "PCR" and len(strategy.sent) == 1
    strategy.deliver("Canceled", ts_event=h.T0 + 124 * h.SECOND)
    assert c.row(strategy).row == "PCR" and len(strategy.sent) == 1
    strategy.test_clock.set_time(h.T0 + 124 * h.SECOND + STEP_NS // 2)
    step(strategy)
    assert c.row(strategy).row == "FL" and len(strategy.sent) == 2
    line = next(line for line in strategy.said if "ack_to_send_ms=" in line)
    assert "ack_to_send_ms=500" in line
    assert all(key in line for key in ("limit=", "level=", "previous=", "floor="))


def test_a_part_fill_is_no_answer_to_our_cancel():
    """A fill of part of the leg while our cancel is pending says nothing of the cancel: its silence
    is still told once."""
    strategy = c.counted()
    repricing(strategy)
    strategy.deliver("Filled", quantity=1, ts_event=h.T0 + 62 * h.SECOND)
    strategy.test_clock.set_time(h.T0 + 62 * h.SECOND)
    step(strategy)
    assert c.row(strategy).row == "PCR", c.row(strategy)
    strategy.said.clear()
    for second in (122, 123, 124):
        strategy.test_clock.set_time(h.T0 + second * h.SECOND)
        step(strategy)
    assert len(alerts(strategy)) == 1 and "cancel" in alerts(strategy)[0], strategy.said[-4:]
    assert c.row(strategy).row == "PCR" and len(strategy.sent) == 1


def test_unanswered_submit_is_told_on_timer_despite_only_uncounted_quotes():
    strategy = c.counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    for second in (FLIGHT_SECS + 3, FLIGHT_SECS + 4):
        one_sided(strategy, "9.50", h.T0 + second * h.SECOND)
        step(strategy)
    assert len(alerts(strategy)) == 1
    assert c.row(strategy).row == "FL" and len(strategy.sent) == 1
    deliver(strategy, "Accepted")
    assert c.row(strategy).row == "WK"


def test_holding_end_cancels_before_a_larger_reopened_holding_is_adopted():
    strategy = c.counted()
    c.first_leg(strategy)
    step(strategy)
    old = strategy.slots[h.AAA]
    held = strategy.cached.held[0]
    grow(strategy, 15)
    held.ts_opened += h.SECOND
    held.ts_init += h.SECOND
    step(strategy)
    assert c.row(strategy).row == "PCX" and len(strategy.canceled) == 1
    assert strategy.slots[h.AAA] is old
    deliver(strategy, "Canceled")
    assert c.row(strategy).row == "X"
    step(strategy)
    assert strategy.slots[h.AAA] is not old and c.row(strategy).row == "G"


@pytest.mark.parametrize("kind", c.TABLE_DATA["order_event_inventory"]["kinds"])
def test_every_order_handler_stores_without_clock_cache_or_actions(kind, monkeypatch):
    strategy = c.counted()
    c.first_leg(strategy)
    step(strategy)
    item = c.event(kind, strategy.cached.working[-1], ts_event=h.T0 + h.SECOND)
    strategy.slots[h.AAA].gain_armed_at = h.T0
    strategy.slots[h.AAA].gain_entry = Decimal("9.00")
    before = c.row(strategy), list(strategy.effects), list(strategy.said)

    def forbidden(*args, **kwargs):
        raise AssertionError("order handler read clock/cache or issued a command")

    with monkeypatch.context() as patch:
        patch.setattr(type(strategy), "cache", property(forbidden))
        patch.setattr(type(strategy), "clock", property(forbidden))
        patch.setattr(strategy, "submit_order", forbidden)
        patch.setattr(strategy, "cancel_order", forbidden)
        specific = {
            "CancelRejected": strategy.on_order_cancel_rejected,
            "Released": strategy.on_order_released,
            "Filled": strategy.on_order_filled,
        }.get(kind)
        if specific is not None:
            specific(item)
        strategy.on_order_event(item)
    assert (c.row(strategy), strategy.effects, strategy.said) == before


PAIRS = [
    (("Submitted", 0), ("Accepted", 0)),
    (("Accepted", 0), ("Filled", 3)),
    (("Accepted", 0), ("Filled", 10)),
    (("Filled", 3), ("Filled", 7)),
    (("Accepted", 0), ("Rejected", 0)),
    (("PendingCancel", 0), ("Canceled", 0)),
    (("Filled", 3), ("Canceled", 0)),
    (("Canceled", 0), ("Filled", 3)),
]
# All pairs from the plain order's initial/submitted and working states that these
# payloads can express (orders/mod.rs:207-302), plus terminal-order late fills.
# A full fill cannot be followed by Canceled: the pin drops that invalid transition.
PAIRS += [
    (first, second)
    for first, seconds in [
        (
            ("Submitted", 0),
            [
                ("Rejected", 0),
                ("Canceled", 0),
                ("PendingCancel", 0),
                ("Updated", 0),
                ("Filled", 3),
                ("Filled", 10),
                ("CancelRejected", 0),
            ],
        ),
        (
            ("Accepted", 0),
            [
                ("Canceled", 0),
                ("Expired", 0),
                ("PendingCancel", 0),
                ("Updated", 0),
                ("CancelRejected", 0),
            ],
        ),
        (
            ("Filled", 3),
            [
                ("Expired", 0),
                ("PendingCancel", 0),
                ("CancelRejected", 0),
                ("Accepted", 0),
                ("Updated", 0),
                ("Filled", 2),
            ],
        ),
        (
            ("PendingCancel", 0),
            [
                ("Expired", 0),
                ("Accepted", 0),
                ("Rejected", 0),
                ("Filled", 3),
                ("Filled", 10),
                ("CancelRejected", 0),
                ("Updated", 0),
                ("PendingCancel", 0),
            ],
        ),
        (("Canceled", 0), [("Filled", 10), ("Updated", 0)]),
        (("Expired", 0), [("Filled", 3), ("Filled", 10), ("Updated", 0)]),
        (
            ("Updated", 0),
            [
                ("Submitted", 0),
                ("Accepted", 0),
                ("Rejected", 0),
                ("Canceled", 0),
                ("Expired", 0),
                ("Updated", 0),
            ],
        ),
        (
            ("CancelRejected", 0),
            [
                ("Rejected", 0),
                ("Canceled", 0),
                ("Expired", 0),
                ("PendingCancel", 0),
                ("CancelRejected", 0),
                ("Updated", 0),
                ("Filled", 3),
                ("Filled", 10),
            ],
        ),
    ]
    for second in seconds
]


@pytest.mark.parametrize("pair", PAIRS)
def test_order_pairs_queued_together_match_one_per_step(pair, monkeypatch):
    results = []
    native = sale_table.transition
    for together in (False, True):
        strategy = c.counted()
        strategy.observe("9.89", h.T0 + h.SECOND)
        strategy.observe("9.88", h.T0 + 2 * h.SECOND)
        step(strategy)
        if pair[0][0] in {"Filled", "Canceled", "PendingCancel", "CancelRejected"}:
            deliver(strategy, "Accepted")
        if pair[0][0] == "PendingCancel":
            strategy.observe("9.80", h.T0 + 61 * h.SECOND)
            step(strategy)
        traced = []

        def trace(state, n, code, facts, traced=traced):
            result = native(state, n, code, facts)
            if code.startswith("o"):
                traced.append((state, code, result))
            return result

        monkeypatch.setattr(sale_table, "transition", trace)
        strategy.said.clear()
        leg = strategy.cached.working[-1]
        for kind, quantity in pair:
            before = c.row(strategy), len(strategy.effects), len(traced)
            strategy.deliver(
                kind, quantity=quantity, leg=leg, ts_event=strategy.clock.timestamp_ns()
            )
            assert (c.row(strategy), len(strategy.effects), len(traced)) == before
            if not together:
                step(strategy)
        if together:
            step(strategy)
        results.append(
            (c.row(strategy), list(strategy.effects), alerts(strategy), records(strategy), traced)
        )
        monkeypatch.setattr(sale_table, "transition", native)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "kind,field", [("Rejected", "refusal_last_ns"), ("Canceled", "external_cancel_ns")]
)
def test_order_interval_uses_event_time_not_handler_or_step_time(kind, field):
    strategy = c.counted()
    c.first_leg(strategy)
    step(strategy)
    event_time = h.T0 + 4 * h.SECOND
    strategy.test_clock.set_time(h.T0 + 20 * h.SECOND)
    strategy.deliver(kind, ts_event=event_time)
    strategy.test_clock.set_time(h.T0 + 30 * h.SECOND)
    step(strategy)
    assert getattr(strategy.slots[h.AAA], field) == event_time


def test_refused_cancel_waits_the_chase_interval_from_its_last_request():
    strategy = c.counted()
    repricing(strategy)
    deliver(strategy, "CancelRejected")
    strategy.said.clear()
    for second in (62, 63, 64):
        strategy.observe("9.70", h.T0 + second * h.SECOND)
    assert len(strategy.canceled) == 1 and not alerts(strategy)
    strategy.observe("9.70", h.T0 + 122 * h.SECOND)
    assert len(strategy.canceled) == 2


def test_no_quote_to_plan_from_is_named_at_every_step():
    strategy = c.Counted(breach_count=2, confirm_gap_ms=0)
    strategy.cached.latest = None
    strategy.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    strategy.said.clear()
    for second in range(1, 61):
        strategy.test_clock.set_time(h.T0 + second * h.SECOND)
        step(strategy)
    assert (
        sum(
            "unprotected: row OWN1 distance refused:" in line and "two-sided quote" in line
            for line in strategy.said
        )
        == 60
    )


def test_oversized_leg_is_cut_on_the_first_quote_after_a_refused_cancel():
    strategy = c.counted()
    c.first_leg(strategy)
    step(strategy)
    grow(strategy, 5)
    step(strategy)
    strategy.deliver("CancelRejected")
    # No new holding edge remains; this quote must still derive MO.
    strategy.observe("9.80", h.T0 + 4 * h.SECOND)
    assert len(strategy.canceled) == 2 and c.row(strategy).row == "PCO"


def test_resends_and_chase_reprices_have_separate_record_counts():
    strategy = c.counted()
    c.first_leg(strategy)
    deliver(strategy, "Rejected", ts_event=h.T0 + 2 * h.SECOND)
    strategy.observe("9.80", h.T0 + 62 * h.SECOND)
    assert records(strategy)[-1]["reprices"] == 0
    assert records(strategy)[-1]["resends"] == 1
    deliver(strategy, "Accepted")
    strategy.observe("9.70", h.T0 + 123 * h.SECOND)
    deliver(strategy, "Canceled")
    assert records(strategy)[-1]["reprices"] == 1
    assert records(strategy)[-1]["resends"] == 1


def test_only_a_first_send_can_log_a_complete_count(monkeypatch):
    strategy = c.counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    snapshot = strategy._sale_snapshot

    def covered(*args):
        position, held, _, order, readable = snapshot(*args)
        return position, held, Decimal(0), order, readable

    # The size guard closes a cover read at the completing quote itself.
    monkeypatch.setattr(strategy, "_sale_snapshot", covered)
    strategy.slots[h.AAA].observed_quantities = (Decimal(10), Decimal(10))
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert not strategy.sent and c.row(strategy).row == "GC"
    assert not any("count=2" in line for line in strategy.said)


def test_refusal_record_uses_the_alerts_scrub_and_length_bound():
    strategy = c.counted()
    c.first_leg(strategy)
    leg = strategy.cached.working[-1]
    leg.venue_order_id = h.VenueOrderId("synthetic-venue-identity")
    reason = f"{leg.client_order_id} {leg.venue_order_id} " + "x" * 300
    deliver(strategy, "Rejected", reason=reason)
    record = records(strategy)[-1]["refusal_reason"]
    assert len(record) <= 160
    assert str(leg.client_order_id) not in record and str(leg.venue_order_id) not in record


def test_external_cancel_names_no_hand():
    strategy = c.counted()
    c.first_leg(strategy)
    deliver(strategy, "Canceled")
    assert "canceled, and not by us (the venue or another hand)" in alerts(strategy)[-1]


def test_alert_markers_follow_the_chosen_branch(monkeypatch):
    strategy = c.counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    step(strategy)
    original = sale_table.transition

    def branch(state, n, code, facts):
        if state.row == "FL" and code == "QU":
            return sale_table.Transition(state, (sale_table.Action.ALERT,), "not refusal_told")
        return original(state, n, code, facts)

    monkeypatch.setattr(sale_table, "transition", branch)
    strategy.observe("9.87", h.T0 + 3 * h.SECOND)
    assert strategy.slots[h.AAA].refusal_told
    assert not strategy.slots[h.AAA].flight_told


def gain_floor_replay(count):
    """Gain lock ON: the pinned engine's trail and plain-limit sends on the same tape."""
    gc.collect()

    def settings():
        if count == 1:
            return h.OwnerProtectorConfig(strategy_id=h.PROTECTOR, row="OWN1")
        return h.OwnerProtectorConfig(
            strategy_id=h.PROTECTOR, row="OWN1", breach_count=count, confirm_gap_ms=0
        )

    class Probe(h.EngineProtector):
        def __new__(cls) -> Self:
            return super().__new__(cls, settings())  # ty: ignore[too-many-positional-arguments]

        def __init__(self):
            super().__init__(settings())
            self.said = []
            self.floor = None
            self.adopted = False

        def _say(self, text):
            self.said.append(text)

        def on_start(self):
            self.subscribe_quotes(h.AAA)

        def on_quote(self, quote):
            if not self.adopted and self.cache.positions_open(
                instrument_id=h.AAA, strategy_id=h.EXTERNAL
            ):
                self.adopted = True
                self.adopt_holding(h.AAA, h.Price.from_str("9.80"))
            super().on_quote(quote)
            slot = self.slots.get(h.AAA)
            if slot is not None and slot.gain_applied_floor is not None:
                self.floor = slot.gain_applied_floor.as_decimal()

    engine = h.BacktestEngine(
        h.BacktestEngineConfig(trader_id=h.TraderId("TESTER-001"), bypass_logging=False)
    )
    engine.add_venue(
        h.Venue("ALPACA"),
        h.OmsType.NETTING,
        h.AccountType.CASH,
        [h.Money(100_000, h.USD)],
        fee_model=h.MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    engine.add_instrument(h.equity())
    probe = Probe()
    engine.add_strategy(h.Owner())
    engine.add_strategy(probe)
    engine.add_data(sorted(h.minute_bars("0.20"), key=lambda bar: bar.ts_init))
    engine.add_data(
        [
            h.quote(bid, h.T0 + second * h.SECOND)
            for second, bid in enumerate(
                ("9.98", "10.00", "10.10", "10.10", "9.95", "9.94", "9.93")
            )
        ]
    )
    try:
        engine.run()
        orders = engine.cache.orders(strategy_id=h.PROTECTOR)
        assert orders and probe.floor is not None, probe.said
        return orders[0].price.as_decimal(), probe.floor
    finally:
        engine.dispose()


def test_first_leg_honors_gain_lock_beside_count_one_on_the_real_engine():
    one, one_floor = gain_floor_replay(1)
    two, two_floor = gain_floor_replay(2)
    assert one_floor == two_floor == Decimal("10.00")
    assert one == two == one_floor
