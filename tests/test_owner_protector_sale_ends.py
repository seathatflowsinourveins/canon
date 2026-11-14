"""A two-quote sale's holding that ends, from any row, ends its slot (the table's ninth form).

Contract: sale_table.json. An echo of our own send is judged in the step after the send, so a cover
that goes unread in between meets the echo in FCW, where it is still only an echo (FCW x oINI). A
holding that ends ends its slot from every row that can see the end: a frozen holding's working sale
is asked canceled as a working sale's is (FZW x HE, FCW x HE), an ended holding's open sale is asked
canceled again whenever it can take a cancel (OX x HE), and a stopped holding that ends has ended
(T0 x HE), so nothing of it reaches a later holding of the instrument.
The stand-ins are test_owner_protector's. No broker, credential, paper state, catalog or ledger.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from canon_trading import symbol_hold
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c

engine_checks = c.engine_checks
isolated_level_books = c.isolated_level_books
alpaca_stream_lock = c.alpaca_stream_lock


def step(strategy, seconds: int | None = None) -> None:
    if seconds is not None:
        strategy.test_clock.set_time(h.T0 + seconds * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))


def reopen(strategy, seconds: int, quantity: int | None = None) -> None:
    """The netting position closes and reopens under its ID: new stamps, no flat moment read."""
    held = strategy.cached.held[0]
    if quantity is not None:
        held.signed_qty, held.quantity = Decimal(quantity), h.Quantity.from_int(quantity)
    held.ts_opened = held.ts_init = h.T0 + seconds * h.SECOND


def frozen_by_cover(strategy) -> None:
    """Our leg of 10 works against a long of 12; a foreign sell of 1 leaves the cover unread."""
    c.first_leg(strategy)
    reopen_quantity = strategy.cached.held[0]
    reopen_quantity.signed_qty, reopen_quantity.quantity = Decimal(12), h.Quantity.from_int(12)
    step(strategy, 5)
    strategy.cached.working.append(
        h.working_sell("owner-sell-1", qty="1", filled="0", status=h.OrderStatus.ACCEPTED)
    )
    step(strategy, 6)
    assert c.row(strategy).row == "FCW", c.row(strategy)


def test_a_cover_unread_in_the_step_after_a_send_is_not_a_fault_of_ours() -> None:
    strategy = c.counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert c.row(strategy).row == "FL"
    foreign = h.working_sell("external-order-not-ours", qty="5", filled="0")
    strategy.cached.working.append(foreign)
    step(strategy, 3)
    assert c.row(strategy).row == "FCW", "the send's echo, judged after the freeze, is only an echo"
    strategy.cached.working.remove(foreign)
    for seconds in range(4, 8):
        step(strategy, seconds)
    assert c.row(strategy).row not in {"FZ", "FZW", "FZP"}, c.row(strategy)
    assert not [line for line in strategy.said if "a fault of ours" in line], strategy.said


def test_a_frozen_holding_that_ends_cancels_our_working_sale_and_ends_its_slot() -> None:
    strategy = c.counted()
    frozen_by_cover(strategy)
    old = strategy.slots[h.AAA]
    (leg,) = strategy.sent
    reopen(strategy, 7)
    step(strategy, 7)
    assert strategy.canceled == [leg.client_order_id], "asked canceled in the step the end is seen"
    assert c.row(strategy).row == "PCX", c.row(strategy)
    strategy.deliver("Canceled", leg=strategy.cache.order(leg.client_order_id))
    step(strategy, 8)
    assert c.row(strategy).row == "X", c.row(strategy)
    step(strategy, 9)
    assert strategy.slots[h.AAA] is not old, "the later holding has a slot of its own"


def test_an_ended_holdings_sale_is_asked_canceled_again_after_a_refusal() -> None:
    strategy = c.counted()
    c.first_leg(strategy)
    (leg,) = strategy.sent
    reopen(strategy, 5, 15)
    step(strategy, 5)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCX"
    strategy.deliver(
        "CancelRejected",
        leg=strategy.cache.order(leg.client_order_id),
        ts_event=h.T0 + 6 * h.SECOND,
    )
    for seconds in range(6, 11):
        step(strategy, seconds)
    assert strategy.canceled == [leg.client_order_id], "not before the refusal's wait has passed"
    assert c.row(strategy).row == "OX", c.row(strategy)
    step(strategy, 11)
    assert strategy.canceled == [leg.client_order_id] * 2, (
        "the reopened holding is at least as large as our leg, so no mismatch ever asks: the "
        "holding's end, seen again, does, once the wait has passed"
    )
    assert c.row(strategy).row == "PCX", c.row(strategy)


def test_a_held_holding_that_ends_does_not_lend_its_slot_to_the_next_one(tmp_path: Path) -> None:
    strategy = c.counted(symbol_holds_dir=tmp_path)
    strategy.observe("9.95", h.T0 + h.SECOND)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 2)
    old = strategy.slots[h.AAA]
    assert c.row(strategy).row == "T0", c.row(strategy)
    reopen(strategy, 3)
    step(strategy, 3)
    assert c.row(strategy).row == "X", "a stopped holding that ends has ended"
    step(strategy, 4)
    assert strategy.slots[h.AAA] is not old, "the holding bought later is not judged by the old one"
    assert c.row(strategy).row == "T0", "and it is held as the old one was"
    assert strategy.sent == []


def test_a_stopped_sale_whose_cancel_was_refused_is_asked_again_when_its_holding_ends(
    tmp_path: Path,
) -> None:
    """A hold with our leg in flight (TW), the cancel asked at the acknowledgement and refused
    (back to TW); the owner then sells and buys again at least our leg's size. The old sell must
    not rest on the new holding with nothing asking it canceled."""
    strategy = c.counted(symbol_holds_dir=tmp_path)
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert c.row(strategy).row == "TW", c.row(strategy)
    ours = strategy.cache.order(leg.client_order_id)
    strategy.deliver("Submitted", leg=ours)
    strategy.deliver("Accepted", leg=ours)
    step(strategy, 4)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCT"
    strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + 5 * h.SECOND)
    step(strategy, 5)
    assert c.row(strategy).row == "TW", c.row(strategy)
    reopen(strategy, 6, 15)
    step(strategy, 6)
    assert strategy.canceled == [leg.client_order_id] * 2, (
        "asked again at the holding's end, a new cause: the stop's refused cancel does not hold it"
    )
    assert c.row(strategy).row == "PCT", c.row(strategy)


def test_a_holding_that_goes_flat_while_its_stopped_sale_is_open_has_ended(tmp_path: Path) -> None:
    """A hold with our leg in flight, the cancel asked at the acknowledgement (PCT); the owner then
    sells the whole long. When our leg's cancel is acknowledged nothing of the holding is left: it
    has ended, and the hold's release must find no level and start no count for it."""
    strategy = c.counted(symbol_holds_dir=tmp_path)
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    ours = strategy.cache.order(leg.client_order_id)
    strategy.deliver("Submitted", leg=ours)
    strategy.deliver("Accepted", leg=ours)
    step(strategy, 4)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCT"
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity = Decimal(0), h.Quantity.from_int(0)
    strategy.cached.held.clear()
    step(strategy, 5)
    assert c.row(strategy).row == "PCT", "the cancel is still owed its answer"
    strategy.deliver("Canceled", leg=ours)
    step(strategy, 6)
    assert c.row(strategy).row == "X", c.row(strategy)
    assert h.AAA not in strategy.levels, "an ended holding keeps no level"
    symbol_hold.remove_hold(tmp_path, "AAA")
    said = len(strategy.said)
    for seconds in (7, 8, 9):
        step(strategy, seconds)
    later = strategy.said[said:]
    assert not [line for line in later if "count starts again" in line], later
    assert h.AAA not in strategy.levels
    slot = strategy.slots.get(h.AAA)
    assert slot is None or slot.ended, "nothing of the ended holding is judged again"


def refuse_each_cancel(strategy, leg, seconds: range, pending: str) -> None:
    """One step a second; whenever our cancel is pending the venue refuses it before the step."""
    ours = strategy.cache.order(leg.client_order_id)
    for second in seconds:
        if c.row(strategy).row == pending:
            strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + second * h.SECOND)
        step(strategy, second)


def cancel_alerts(strategy) -> list[str]:
    return [line for line in strategy.said if line.startswith("ALERT:") and "cancel" in line]


def test_a_refused_cancel_at_a_holdings_end_is_asked_again_at_a_pace() -> None:
    """The owner sold and bought again at least our leg's size, so the end is seen at every step
    and no mismatch ever shows. A venue that refuses our cancel each time is asked again at the
    refused sale's own pace (5, 10, 20 s), never once a second and never never, and told once."""
    strategy = c.counted()
    c.first_leg(strategy)
    (leg,) = strategy.sent
    reopen(strategy, 5, 15)
    step(strategy, 5)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCX"
    refuse_each_cancel(strategy, leg, range(6, 45), "PCX")
    asked = len(strategy.canceled)
    assert asked == 4, ("asked at 5 s, then 5, 10 and 20 s after each refusal", asked)
    assert len(cancel_alerts(strategy)) == 1, cancel_alerts(strategy)


def test_a_refused_cancel_of_a_flat_holdings_sale_is_asked_again_at_a_pace() -> None:
    """The holding went flat with our leg working: the mismatch asks the cancel. Refused each time,
    it is asked again at the pace, not at every timer step."""
    strategy = c.counted()
    c.first_leg(strategy)
    (leg,) = strategy.sent
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity = Decimal(0), h.Quantity.from_int(0)
    strategy.cached.held.clear()
    step(strategy, 5)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCX"
    refuse_each_cancel(strategy, leg, range(6, 45), "PCX")
    assert len(strategy.canceled) == 4, len(strategy.canceled)
    assert len(cancel_alerts(strategy)) == 1, cancel_alerts(strategy)


def test_a_held_symbols_refused_cancel_is_asked_again_while_the_hold_stands(tmp_path: Path) -> None:
    """A hold with our leg in flight; the cancel asked at the acknowledgement is refused. The long
    remains and the hold stands, so nothing ends the holding: the cancel must still be asked again,
    at the pace, or the held symbol's sell rests at the venue and can fill."""
    strategy = c.counted(symbol_holds_dir=tmp_path)
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    ours = strategy.cache.order(leg.client_order_id)
    strategy.deliver("Submitted", leg=ours)
    strategy.deliver("Accepted", leg=ours)
    step(strategy, 4)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCT"
    refuse_each_cancel(strategy, leg, range(5, 44), "PCT")
    assert len(strategy.canceled) == 4, len(strategy.canceled)
    assert len(cancel_alerts(strategy)) == 1, cancel_alerts(strategy)
    assert strategy.sent == [leg], "a hold never sells"


def test_a_new_leg_starts_the_refused_cancels_pace_and_its_alert_again() -> None:
    """The pace and the one ALERT belong to the leg: a later leg's first refused cancel is told."""
    strategy = c.counted()
    c.first_leg(strategy)
    slot = strategy.slots[h.AAA]
    slot.cancel_refusals, slot.cancel_refused_ns, slot.cancel_refusal_told = 3, h.T0, True
    strategy.observe("9.70", h.T0 + 60 * h.SECOND)
    assert c.row(strategy).row == "PCR", c.row(strategy)
    strategy.deliver("Canceled", ts_event=h.T0 + 61 * h.SECOND)
    step(strategy, 61)
    assert c.row(strategy).row == "FL" and len(strategy.sent) == 2
    assert (slot.cancel_refusals, slot.cancel_refused_ns, slot.cancel_refusal_told) == (
        0,
        None,
        False,
    )


def held_with_a_refused_cancel(strategy, tmp_path: Path):
    """A hold with our leg in flight; the cancel asked at the acknowledgement is refused at 5 s."""
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    ours = strategy.cache.order(leg.client_order_id)
    strategy.deliver("Submitted", leg=ours)
    strategy.deliver("Accepted", leg=ours)
    step(strategy, 4)
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "PCT"
    strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + 5 * h.SECOND)
    step(strategy, 5)
    assert c.row(strategy).row == "TW", c.row(strategy)
    return leg, ours


def test_the_first_ask_for_a_new_cause_never_waits_a_refusals_pace(tmp_path: Path) -> None:
    """The stop's cancel was refused and its wait runs. The owner then sells the whole long: the
    holding's end is a new cause to ask, and our sell must not rest beside a flat account for the
    rest of a wait that another cause started."""
    strategy = c.counted(symbol_holds_dir=tmp_path)
    leg, _ours = held_with_a_refused_cancel(strategy, tmp_path)
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity = Decimal(0), h.Quantity.from_int(0)
    strategy.cached.held.clear()
    step(strategy, 6)
    assert strategy.canceled == [leg.client_order_id] * 2, "asked in the step the end is seen"
    assert c.row(strategy).row == "PCT", c.row(strategy)


def test_part_fills_after_a_refused_cancel_do_not_ask_at_every_fill() -> None:
    """An ended holding's sale fills in parts while the venue refuses its cancel: a part fill
    asks the cancel the end already owes, so it waits the end's pace and does not lengthen it."""
    strategy = c.counted()
    c.first_leg(strategy)
    (leg,) = strategy.sent
    ours = strategy.cache.order(leg.client_order_id)
    reopen(strategy, 5, 15)
    step(strategy, 5)
    strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + 6 * h.SECOND)
    step(strategy, 6)
    for seconds in (7, 8, 9):
        strategy.deliver("Filled", quantity=1, leg=ours)
        step(strategy, seconds)
    assert len(strategy.canceled) == 1, "part fills inside the wait ask nothing"
    assert strategy.slots[h.AAA].cancel_refusals == 1
    step(strategy, 11)
    assert len(strategy.canceled) == 2, "asked again when the wait has passed"


def test_a_refusal_stamped_ahead_of_the_clock_does_not_postpone_the_next_ask() -> None:
    """The wait runs from no later than the node's own clock when the refusal is judged."""
    strategy = c.counted()
    c.first_leg(strategy)
    (leg,) = strategy.sent
    ours = strategy.cache.order(leg.client_order_id)
    reopen(strategy, 5, 15)
    step(strategy, 5)
    strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + 3600 * h.SECOND)
    for seconds in range(6, 12):
        step(strategy, seconds)
    assert len(strategy.canceled) == 2, "asked again five seconds after the refusal was judged"


def test_each_refused_cancel_after_the_first_is_counted_in_a_line(tmp_path: Path) -> None:
    strategy = c.counted(symbol_holds_dir=tmp_path)
    _leg, ours = held_with_a_refused_cancel(strategy, tmp_path)
    for seconds in range(6, 30):
        if c.row(strategy).row == "PCT":
            strategy.deliver("CancelRejected", leg=ours, ts_event=h.T0 + seconds * h.SECOND)
        step(strategy, seconds)
    counted = [
        line
        for line in strategy.said
        if "cancel of our sale refused" in line and not line.startswith("ALERT:")
    ]
    assert [line.split("refused ")[1].split(" ")[0] for line in counted] == ["1", "2", "3"], counted
    assert "asked again after 5 s" in counted[0] and "asked again after 20 s" in counted[2]
    assert len(cancel_alerts(strategy)) == 1
    assert "asked again after 5 s" in cancel_alerts(strategy)[0], cancel_alerts(strategy)


def test_a_stored_order_event_is_judged_before_the_standing_stop_asks(tmp_path: Path) -> None:
    """The stop that still stands is raised only in a step with no stored order event of the
    instrument: an Updated stored while the wait ran is judged in the resting row, where it changes
    nothing, and is never read as a second refusal of a cancel asked in the same step."""
    strategy = c.counted(symbol_holds_dir=tmp_path)
    leg, ours = held_with_a_refused_cancel(strategy, tmp_path)
    strategy.deliver("Updated", leg=ours, ts_event=h.T0 + 11 * h.SECOND)
    step(strategy, 11)
    slot = strategy.stopped_sales.get(h.AAA) or strategy.slots[h.AAA]
    assert slot.cancel_refusals == 1, "the stored update is not a refusal"
    assert strategy.canceled == [leg.client_order_id] and c.row(strategy).row == "TW"
    step(strategy, 12)
    assert strategy.canceled == [leg.client_order_id] * 2 and c.row(strategy).row == "PCT"
