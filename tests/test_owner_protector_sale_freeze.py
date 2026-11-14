"""A freeze at a count above one, against what the count-one path does with the same account.

Contract: sale_table.json's Words (MO: our sale's remaining quantity exceeds the long less foreign
sells; a cancel, never a send) and owner_protector's count-one `_freeze` (it pulls our armed sell
where an oversell is possible, and a freeze ends when the cover reads again). Reproducers for the
landing review's three findings on the two-quote path; the stand-ins are test_owner_protector's.
No broker, credential, paper state, catalog or ledger is read or written.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from canon_trading import owner_protector
from canon_trading import sale_table
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c
from tests.test_owner_protector_sale_table import counted
from tests.test_owner_protector_sale_table import first_leg
from tests.test_owner_protector_sale_table import row

FOREIGN = "external-order-not-ours"
engine_checks = c.engine_checks
isolated_level_books = c.isolated_level_books
alpaca_stream_lock = c.alpaca_stream_lock


def alerts(strategy):
    return [line for line in strategy.said if line.startswith("ALERT:")]


def records(strategy):
    return [
        json.loads(line.split("sale record ", 1)[1])
        for line in strategy.said
        if "sale record " in line
    ]


def step(strategy, seconds):
    strategy.test_clock.set_time(h.T0 + seconds * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))


def test_a_foreign_sell_placed_beside_our_working_leg_gets_our_leg_canceled():
    """Ten held, our leg works for ten, a foreign sell of five appears: fifteen against ten."""
    strategy = counted()
    first_leg(strategy)
    ours = strategy.sent[-1].client_order_id
    strategy.cached.working.append(h.working_sell(FOREIGN, qty="5", filled="0"))
    step(strategy, 3)
    strategy.observe("9.87", h.T0 + 4 * h.SECOND)
    assert ours in strategy.canceled, (row(strategy), strategy.said[-3:])
    assert len(strategy.sent) == 1


@pytest.mark.parametrize("status", ["PENDING_CANCEL", "PENDING_UPDATE"])
def test_a_foreign_sell_whose_cover_cannot_be_summed_gets_our_leg_canceled(status):
    strategy = counted()
    first_leg(strategy)
    ours = strategy.sent[-1].client_order_id
    strategy.cached.working.append(
        h.working_sell(FOREIGN, qty="5", filled="0", status=getattr(h.OrderStatus, status))
    )
    step(strategy, 3)
    assert ours in strategy.canceled, (row(strategy), strategy.said[-3:])


def test_a_holding_taken_short_while_our_leg_works_gets_our_leg_canceled_and_said():
    """The owner's sells take the long to a short under our working leg: it is canceled, with one ALERT."""
    strategy = counted()
    first_leg(strategy)
    ours = strategy.sent[-1].client_order_id
    held = strategy.cached.held[0]
    held.signed_qty, held.quantity, held.is_long = Decimal(-2), h.Quantity.from_int(2), False
    step(strategy, 3)
    assert ours in strategy.canceled, (row(strategy), strategy.said[-3:])
    assert row(strategy).row in owner_protector.SALE_COVER_ROWS, row(strategy)
    assert len(alerts(strategy)) == 1, alerts(strategy)
    step(strategy, 4)
    assert len(alerts(strategy)) == 1 and len(strategy.sent) == 1, alerts(strategy)


def test_a_cover_that_reads_again_ends_the_freeze_and_a_new_count_sells():
    """A foreign sell covers part of the long for a moment, then is gone; the bid then falls."""
    strategy = counted()
    foreign = h.working_sell(FOREIGN, qty="5", filled="0")
    strategy.cached.working.append(foreign)
    step(strategy, 1)
    assert row(strategy).row == "FC" and h.AAA in strategy.frozen
    strategy.cached.working.remove(foreign)
    step(strategy, 2)
    strategy.observe("9.89", h.T0 + 3 * h.SECOND)
    strategy.observe("9.88", h.T0 + 4 * h.SECOND)
    assert h.AAA not in strategy.frozen, strategy.frozen
    assert len(strategy.sent) == 1, (row(strategy), strategy.said[-3:])
    assert strategy.sent[-1].quantity.as_decimal() == 10


def test_a_table_ordered_freeze_is_not_ended_by_a_cover_that_reads():
    """A leg our own engine denied stays frozen: only a freeze by the cover's read may end."""
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    strategy.deliver("Denied")
    assert row(strategy).row == "FZ"
    step(strategy, 3)
    strategy.observe("9.87", h.T0 + 4 * h.SECOND)
    strategy.observe("9.86", h.T0 + 5 * h.SECOND)
    assert row(strategy).row == "FZ" and len(strategy.sent) == 1


def test_an_error_after_a_send_in_one_step_keeps_the_new_leg_as_ours(monkeypatch):
    """Reached by a direct call with two events only: no caller of the driver builds this step."""
    strategy = counted()
    first_leg(strategy)
    strategy.test_clock.set_time(h.T0 + 60 * h.SECOND)
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    assert row(strategy).row == "PCR"
    leg = strategy.cached.working[-1]
    leg.status, leg.is_closed, leg.is_open = h.OrderStatus.CANCELED, True, False
    original = sale_table.transition

    def second_raises(state, n, code, facts):
        if code == "NQ":
            raise sale_table.NoMatchingBranch("synthetic contract failure")
        return original(state, n, code, facts)

    monkeypatch.setattr(sale_table, "transition", second_raises)
    strategy._sale_step(h.AAA, ["oCXL", "NQ"])
    assert len(strategy.sent) == 2
    new = strategy.sent[-1].client_order_id
    monkeypatch.setattr(sale_table, "transition", original)
    step(strategy, 63)
    slot = strategy.slots[h.AAA]
    assert slot.protect_id == new, (row(strategy), slot.protect_id)
    assert row(strategy).row == "FZW"


def test_summable_predecessor_and_ours_within_holding_neither_freeze_nor_cancel():
    strategy = counted()
    strategy.cached.working.append(h.working_sell(qty="4", filled="0"))
    first_leg(strategy)
    assert strategy.sent[-1].quantity.as_decimal() == 6
    step(strategy, 3)
    assert row(strategy).row == "WK" and h.AAA not in strategy.frozen
    assert not strategy.canceled


@pytest.mark.parametrize("status", ["ACCEPTED", "PENDING_CANCEL"])
def test_cover_recovery_is_named_once_and_next_unread_cover_alerts_again(status):
    strategy = counted()
    foreign = h.working_sell(FOREIGN, qty="5", filled="0", status=getattr(h.OrderStatus, status))
    strategy.cached.working.append(foreign)
    step(strategy, 1)
    assert row(strategy).row == "FC"
    strategy.cached.working.remove(foreign)
    step(strategy, 2)
    step(strategy, 3)
    assert row(strategy) == sale_table.State("G", 0) and h.AAA not in strategy.frozen
    assert sum("cover reads again" in line for line in strategy.said) == 1
    strategy.cached.working.append(foreign)
    step(strategy, 4)
    assert len(alerts(strategy)) == 2
    strategy.cached.working.remove(foreign)
    step(strategy, 5)
    strategy.observe("9.89", h.T0 + 6 * h.SECOND)
    strategy.observe("9.88", h.T0 + 7 * h.SECOND)
    assert strategy.sent[-1].quantity.as_decimal() == 10


def test_fault_while_cover_unread_keeps_reason_after_recovery():
    strategy = counted()
    first_leg(strategy)
    # An unread cover whose total is safe must leave our leg working.
    foreign = h.working_sell(FOREIGN, qty="1", filled="0", status=h.OrderStatus.PENDING_CANCEL)
    strategy.cached.held[0].signed_qty = Decimal(20)
    strategy.cached.working.append(foreign)
    step(strategy, 3)
    assert row(strategy).row == "FCW" and not strategy.canceled
    strategy.deliver("Unrecognized", leg=strategy.cache.order(strategy.slots[h.AAA].protect_id))
    assert row(strategy).row == "FZW"
    reason = strategy.frozen[h.AAA]
    strategy.cached.working.remove(foreign)
    step(strategy, 4)
    assert row(strategy).row == "FZW" and strategy.frozen[h.AAA] == reason
    assert "cover reads again" not in " ".join(strategy.said)


@pytest.mark.parametrize("source", ["G", "RW", "PCR"])
def test_unformable_leg_retries_without_building_an_order_or_repeating_alert(source, monkeypatch):
    strategy = counted()
    if source != "G":
        first_leg(strategy)
        if source == "RW":
            strategy.deliver("Rejected")
        else:
            strategy.observe("9.80", h.T0 + 62 * h.SECOND)
    instrument = strategy.cached.instrument
    monkeypatch.setattr(strategy.cached, "instrument", lambda _: None)
    if source == "PCR":
        strategy.deliver("Canceled")
        assert row(strategy).row == "RW"
    before = len(strategy.sent)
    strategy.observe("9.79", h.T0 + 123 * h.SECOND)
    strategy.observe("9.78", h.T0 + 124 * h.SECOND)
    strategy.observe("9.77", h.T0 + 125 * h.SECOND)
    assert len(strategy.sent) == before and h.AAA not in strategy.frozen
    assert len(alerts(strategy)) == 1
    assert strategy.slots[h.AAA].refusal_told
    monkeypatch.setattr(strategy.cached, "instrument", instrument)
    strategy.observe("9.76", h.T0 + 126 * h.SECOND)
    assert len(strategy.sent) == before + 1


def test_external_cancel_alert_interval_and_fresh_quantity():
    strategy = counted()
    first_leg(strategy)
    strategy.deliver("Canceled", reason="x" * 300)
    assert row(strategy) == sale_table.State("G", 0)
    assert len(alerts(strategy)) == 1
    from canon_trading.owner_protector import HOLD_ONE_SYMBOL

    alert = alerts(strategy)[0]
    assert "canceled by another hand" in alert and "60 s" in alert
    assert "full new count" in alert and HOLD_ONE_SYMBOL in alert
    strategy.observe("9.87", h.T0 + 3 * h.SECOND)
    strategy.observe("9.86", h.T0 + 4 * h.SECOND)
    assert len(strategy.sent) == 1
    # Both clocks guard every first leg; a later refusal cannot shorten a cancel interval.
    strategy.cached.held[0].signed_qty = Decimal(7)
    step(strategy, 62)
    strategy.observe("9.85", h.T0 + 63 * h.SECOND)
    strategy.observe("9.84", h.T0 + 64 * h.SECOND)
    assert len(strategy.sent) == 2 and strategy.sent[-1].quantity.as_decimal() == 7


@pytest.mark.parametrize("kind,venue", [("Denied", "engine limit"), ("Unrecognized", None)])
def test_alert_keeps_cell_reason_then_event_reason(kind, venue):
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    strategy.deliver(kind, reason=venue)
    alert = alerts(strategy)[-1]
    note = sale_table.TABLE.cells["FL", sale_table.order_event_column(kind)].note
    label = "FL x oDEN: " if kind == "Denied" else "FL x oOTH: UNEXPECTED; "
    assert alert.split(": ", 2)[2] == (label + note + (f"; {venue}" if venue else ""))[:160]
    assert "None" not in alert
    if venue:
        assert venue in alert and alert.index("FL x ") < alert.index(venue)
    assert strategy.frozen[h.AAA]


@pytest.mark.parametrize(
    "kind", ["Expired", "Canceled", "Denied", "Filled", "stop", "cover", "end"]
)
def test_each_sale_end_writes_one_final_record(kind):
    strategy = counted()
    if kind == "Denied":
        strategy.observe("9.89", h.T0 + h.SECOND)
        strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    else:
        first_leg(strategy)
    before = len(records(strategy))
    if kind == "stop":
        strategy.stop_instrument(h.AAA)
    elif kind == "cover":
        strategy.cached.working.append(h.working_sell(FOREIGN, qty="5", filled="0"))
        step(strategy, 3)
    elif kind == "end":
        strategy.cached.held[0].signed_qty = Decimal(0)
        step(strategy, 3)
    else:
        strategy.deliver(kind, quantity=10 if kind == "Filled" else 0)
    final = records(strategy)[before:]
    assert len(final) == 1
    assert {"refusal_reason", "refusal_first", "refusals", "confirm_gap_ms"} <= final[0].keys()
    if kind == "Filled":
        assert final[0]["trigger_to_fill_ns"] is not None
    step(strategy, 4)
    assert len(records(strategy)) == before + 1


def test_gain_lock_measure_on_counted_sale_fill():
    strategy = counted()
    slot = strategy.slots[h.AAA]
    slot.gain_armed_at, slot.gain_entry = h.T0, Decimal("9.00")
    slot.gain_floor = h.Price.from_str("9.50")
    first_leg(strategy)
    filled = strategy.deliver("Filled", quantity=3)
    # The pin calls the specific handler before the generic handler. This stand-in
    # exercises the same specific entry explicitly while the sale is still open.
    strategy.on_order_filled(filled)
    assert len(strategy.gain_lock_sales) == 1
    assert strategy.gain_lock_sales[0]["quantity"] == "3"
    assert strategy.gain_lock_sales[0]["spread_at_release"] is not None


@pytest.mark.parametrize("target", ["GC", "FZ", "FZW", "FZP", "FC", "FCW", "FCP"])
@pytest.mark.parametrize("planned", [False, True])
def test_reported_cover_and_freeze_states_have_reasons(target, planned):
    strategy = counted()
    slot = strategy.slots[h.AAA]
    slot.sale_state = sale_table.State(target)
    if not planned:
        slot.level = None
        slot.distance = None
    if target != "GC":
        strategy.frozen[h.AAA] = "synthetic freeze cause"
    state, count, reason = strategy._holding_state(h.AAA, slot)
    assert count is None
    if target == "GC":
        assert state == "waiting" and "foreign sells cover the whole long" in reason
        assert "no count" in reason
    else:
        assert (state, reason) == ("frozen", "synthetic freeze cause")


def test_quotes_that_send_nothing_use_no_order_ids(monkeypatch):
    strategy = counted(confirm_gap_ms=1000)
    ids = []
    native = strategy._protect_id
    monkeypatch.setattr(strategy, "_protect_id", lambda: ids.append(native()) or ids[-1])
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + h.SECOND + 1)
    strategy.observe("10.00", h.T0 + 2 * h.SECOND)
    strategy.observe("9.89", h.T0 + 3 * h.SECOND)
    assert ids == [] and not strategy.sent
    strategy.observe("9.88", h.T0 + 4 * h.SECOND)
    assert len(ids) == len(strategy.sent) == 1


def test_one_step_says_each_cover_line_once():
    strategy = counted()
    strategy.cached.working.append(h.working_sell(qty="10", filled="0"))
    before = len(strategy.said)
    step(strategy, 1)
    lines = [line for line in strategy.said[before:] if "fully covered" in line]
    assert len(lines) == 1


def test_unread_cover_with_unknown_own_remainder_uses_base_oversell_check(monkeypatch):
    strategy = counted()
    first_leg(strategy)
    ours = strategy.cache.order(strategy.slots[h.AAA].protect_id)
    ours.leaves_qty = None
    strategy.cached.working.append(h.working_sell(FOREIGN, qty="1", filled="0"))
    called = []
    original = strategy._oversell_possible
    monkeypatch.setattr(
        strategy, "_oversell_possible", lambda iid: called.append(iid) or original(iid)
    )
    step(strategy, 3)
    assert called and ours.client_order_id in strategy.canceled


def test_cover_freeze_retries_refused_oversell_cancel_on_timer():
    strategy = counted()
    first_leg(strategy)
    ours = strategy.cache.order(strategy.slots[h.AAA].protect_id)
    strategy.cached.working.append(h.working_sell(FOREIGN, qty="1", filled="0"))
    step(strategy, 3)
    assert row(strategy).row == "FCP" and len(strategy.canceled) == 1
    strategy.deliver("CancelRejected", leg=ours)
    assert row(strategy).row == "FCW"
    step(strategy, 4)
    assert len(strategy.canceled) == 2 and len(strategy.sent) == 1


@pytest.mark.parametrize("target", ["PCR", "PCO"])
def test_refusal_of_cancel_pending_leg_keeps_the_ruled_result(target):
    strategy = counted()
    first_leg(strategy)
    if target == "PCR":
        strategy.observe("9.80", h.T0 + 62 * h.SECOND)
    else:
        strategy.cached.held[0].signed_qty = Decimal(7)
        step(strategy, 3)
    assert row(strategy).row == target
    strategy.deliver("Rejected", reason="synthetic venue refusal")
    assert h.AAA not in strategy.frozen
    if target == "PCR":
        assert row(strategy).row == "RW" and strategy.slots[h.AAA].refusals == 1
        strategy.observe("9.79", h.T0 + 63 * h.SECOND)
        assert len(strategy.sent) == 1
        strategy.observe("9.78", h.T0 + 122 * h.SECOND)
        assert len(strategy.sent) == 2
    else:
        assert row(strategy) == sale_table.State("G", 0)
        assert strategy.slots[h.AAA].protect_id is None


def test_instrument_missing_when_level_is_raised_is_a_recoverable_cover_freeze(monkeypatch):
    strategy = counted()
    instrument = strategy.cached.instrument
    monkeypatch.setattr(strategy.cached, "instrument", lambda _: None)
    strategy.observe("10.50", h.T0 + h.SECOND)
    assert row(strategy).row == "FC" and "instrument" in strategy.frozen[h.AAA]
    step(strategy, 2)
    assert row(strategy).row == "FC"
    monkeypatch.setattr(strategy.cached, "instrument", instrument)
    step(strategy, 3)
    assert row(strategy) == sale_table.State("G", 0) and h.AAA not in strategy.frozen


@pytest.mark.parametrize("state", ["flight", "working", "partial", "cancel"])
def test_cover_reads_again_uses_current_leg_state(state):
    strategy = counted()
    first_leg(strategy)
    slot = strategy.slots[h.AAA]
    ours = strategy.cache.order(slot.protect_id)
    if state == "flight":
        ours.status = h.OrderStatus.SUBMITTED
    elif state == "partial":
        strategy.deliver("Filled", quantity=1, leg=ours)
    elif state == "cancel":
        strategy.observe("9.80", h.T0 + 62 * h.SECOND)
    # Another strategy's position makes the read ambiguous without an oversell.
    other = h.position("1", h.StrategyId("OTHER-001"))
    strategy.cached.held.append(other)
    step(strategy, 63)
    assert row(strategy).row == ("FCP" if state == "cancel" else "FCW")
    strategy.cached.held.remove(other)
    step(strategy, 64)
    assert (
        row(strategy).row
        == {"flight": "FL", "working": "WK", "partial": "PF", "cancel": "FCP"}[state]
    )
    if state == "cancel":
        strategy.deliver("Canceled", leg=ours)
        assert row(strategy).row == "FC"
        step(strategy, 65)
        assert row(strategy) == sale_table.State("G", 0)
    assert h.AAA not in strategy.frozen


@pytest.mark.parametrize("target", ["GC", "FC", "FZ"])
def test_unplanned_cover_and_fault_book_round_trip(target, tmp_path):
    path = tmp_path / "book"
    strategy = counted(account_label="example", level_book_path=path)
    strategy.level_book_path, strategy.run = path, "synthetic"
    slot = strategy.slots[h.AAA]
    slot.sale_state, slot.level, slot.distance = sale_table.State(target), None, None
    strategy.frozen[h.AAA] = "unread synthetic position"
    strategy._save_levels()
    notes = []
    saved = c.LevelBook.read(path, report=notes.append)["example", h.AAA]
    assert not notes
    assert saved.state == ("waiting" if target == "GC" else "frozen")
    assert saved.reason and saved.count is None


@pytest.mark.parametrize("later", ["refusal", "cancel"])
def test_both_refusal_and_external_cancel_clocks_guard_a_send(later):
    strategy = counted()
    slot = strategy.slots[h.AAA]
    slot.refusal_last_ns = h.T0 + (30 if later == "refusal" else 1) * h.SECOND
    slot.external_cancel_ns = h.T0 + (30 if later == "cancel" else 1) * h.SECOND
    strategy.observe("9.89", h.T0 + 62 * h.SECOND)
    strategy.observe("9.88", h.T0 + 63 * h.SECOND)
    assert not strategy.sent
    strategy.observe("9.87", h.T0 + 90 * h.SECOND)
    assert len(strategy.sent) == 1


@pytest.mark.parametrize("cause", ["ambiguous", "other-strategy", "short"])
def test_unread_position_is_not_a_holding_end_and_can_recover(cause):
    strategy = counted()
    step(strategy, 1)
    held = strategy.cached.held[0]
    if cause == "ambiguous":
        strategy.cached.held.append(h.position("1"))
    elif cause == "other-strategy":
        strategy.cached.held = [h.position("10", h.StrategyId("OTHER-001"))]
    else:
        held.signed_qty = Decimal(-1)
    step(strategy, 2)
    assert row(strategy).row == "FC" and not strategy.slots[h.AAA].ended
    held.signed_qty = Decimal(10)
    strategy.cached.held = [held]
    step(strategy, 3)
    assert row(strategy) == sale_table.State("G", 0) and h.AAA not in strategy.frozen
    strategy.observe("9.89", h.T0 + 4 * h.SECOND)
    strategy.observe("9.88", h.T0 + 5 * h.SECOND)
    assert strategy.sent[-1].quantity.as_decimal() == 10
