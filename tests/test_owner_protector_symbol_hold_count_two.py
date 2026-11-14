"""The owner's word for one symbol, at a count of two (the two-quote sale's table).

The properties are those of tests/test_owner_protector_symbol_hold.py, restated for a path that
rests no sell of ours at the venue until two counted quotes confirm a breach:

2. a held symbol is never counted and never sold; a new hold cancels only our own working sell of
   that symbol within one step and sends nothing; every other holding goes on counting;
3. the hold is named in the END record and in the book's state;
4. removing the hold starts the count again from zero at the kept level, never lowered, once
   nothing of ours is open, and a breach during the hold is acted on only under the count rule;
5. the hold never touches an order the owner placed.

A hold is the table's stop of that instrument (its ST column); the release is a start for that
holding. A holding frozen by a fault of ours stays frozen through a hold and its release.

Offline: the stand-in of tests/test_owner_protector_sale_table.py, synthetic holdings and orders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from canon_trading import sale_table
from canon_trading import symbol_hold
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c
from tests import test_owner_protector_symbol_hold as s

HELD = symbol_hold.HELD
State = sale_table.State


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the credential-environment fixture for this offline case."""


@pytest.fixture(autouse=True)
def isolated_level_books(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


def two_quote(folder: Path):
    return c.counted(symbol_holds_dir=folder)


def step(strategy, seconds: int) -> None:
    s.step(strategy, h.T0 + seconds * h.SECOND)
    c.flush_events(strategy)


def under(strategy, seconds: int, bid: str = "9.80") -> None:
    strategy.observe(bid, h.T0 + seconds * h.SECOND)


def test_a_symbol_held_at_the_start_is_never_counted_or_sold(tmp_path: Path) -> None:
    symbol_hold.write_hold(tmp_path, "AAA")
    strategy = two_quote(tmp_path)
    for n in range(1, 5):
        step(strategy, n)
        under(strategy, n)
    assert strategy.sent == [], "a held symbol is sold by nothing"
    assert c.row(strategy) == State("T0"), c.row(strategy)
    assert any(f"{h.AAA}: {HELD}" in line for line in strategy.said), strategy.said


def test_a_new_hold_while_counting_stops_the_count(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1)
    assert c.row(strategy) == State("G", 1)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 2)
    for n in range(3, 7):
        under(strategy, n)
        step(strategy, n)
    assert strategy.sent == [], "the count never completes under a hold"
    assert c.row(strategy) == State("T0"), c.row(strategy)


def test_a_new_hold_cancels_our_working_sell_within_one_step_and_sends_nothing(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert strategy.canceled == [leg.client_order_id], "within one step, our own sell only"
    strategy.deliver_then_step("Canceled")
    assert c.row(strategy) == State("T0"), c.row(strategy)
    for n in range(4, 8):
        under(strategy, n, "9.70")
        step(strategy, n)
    assert strategy.sent == [leg], "nothing new is sent for a held symbol"


def test_removing_the_hold_counts_again_from_zero_at_the_kept_level(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1)
    kept = strategy.slots[h.AAA].level
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 2)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert c.row(strategy) == State("G", 0), "the release is a start: the count is zero"
    assert strategy.slots[h.AAA].level >= kept, "never lowered"
    assert strategy.sent == [], "a release sends nothing by itself"
    under(strategy, 4)
    assert strategy.sent == [], "one counted quote cannot sell"
    under(strategy, 5)
    assert len(strategy.sent) == 1, "the second counted quote after the release sends"
    assert strategy.sent[0].price <= kept


def test_a_breach_during_the_hold_is_acted_on_only_under_the_count_rule(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 1)
    for n in range(2, 6):
        under(strategy, n, "9.60")  # through the level, during the hold
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 6)
    assert strategy.sent == [], "quotes seen during the hold count for nothing"
    under(strategy, 7, "9.59")
    assert strategy.sent == []
    under(strategy, 8, "9.58")
    assert len(strategy.sent) == 1


def test_the_release_waits_while_our_sell_is_still_open(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert c.row(strategy) == State("PCT"), "our cancel is asked and not yet answered"
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    under(strategy, 5, "9.70")
    under(strategy, 6, "9.69")
    assert strategy.sent == [leg], "no second sell beside one whose cancel is not answered"
    assert c.row(strategy) == State("PCT"), c.row(strategy)
    strategy.deliver_then_step("Canceled")
    step(strategy, 7)
    assert c.row(strategy) == State("G", 0), "counting again once nothing of ours is open"
    under(strategy, 8, "9.68")
    under(strategy, 9, "9.67")
    assert len(strategy.sent) == 2


def test_the_book_and_the_end_record_name_the_hold(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    slot = strategy.slots.get(h.AAA) or strategy.stopped_sales[h.AAA]
    assert strategy._holding_state(h.AAA, slot)[0] != "held", "our sell still rests, cancel asked"
    strategy.deliver_then_step("Canceled")
    step(strategy, 4)
    slot = strategy.slots.get(h.AAA) or strategy.stopped_sales[h.AAA]
    state, _, reason = strategy._holding_state(h.AAA, slot)
    assert state == "held" and reason is not None and HELD in reason, (state, reason)
    strategy.on_stop()
    (end,) = [line for line in strategy.said if line.startswith(f"{h.AAA}: END ")]
    assert f"{HELD} at end" in end and "owner holds=1" in end, end


def test_a_freeze_by_a_fault_of_ours_outlasts_a_hold_and_its_release(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1, "9.89")
    under(strategy, 2, "9.88")
    strategy.deliver_then_step("Denied")
    assert c.row(strategy) == State("FZ"), c.row(strategy)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    for n in range(5, 9):
        under(strategy, n, "9.70")
        step(strategy, n)
    assert len(strategy.sent) == 1, "a frozen holding sends nothing after a hold is removed"
    assert c.row(strategy) == State("FZ"), "the freeze holds to the next start"
    slot = strategy.slots.get(h.AAA) or strategy.stopped_sales[h.AAA]
    assert strategy._holding_state(h.AAA, slot)[0] == "frozen"


def test_a_hold_with_our_leg_in_flight_is_told_and_cancels_at_the_acknowledgement(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1, "9.89")
    under(strategy, 2, "9.88")
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert c.row(strategy) == State("TW"), "nothing can be asked before the acknowledgement"
    assert strategy.canceled == []
    assert any(line.startswith("ALERT:") and "FL x ST" in line for line in strategy.said)
    strategy.deliver_then_step("Submitted")
    strategy.deliver_then_step("Accepted")
    assert strategy.canceled == [leg.client_order_id], "asked when the leg can take a cancel"
    strategy.deliver_then_step("Canceled")
    assert c.row(strategy) == State("T0"), c.row(strategy)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    assert c.row(strategy) == State("G", 0) and strategy.sent == [leg]


def test_a_holding_frozen_with_its_sale_open_stays_stopped_after_the_release(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    strategy.deliver_then_step(
        "Unrecognized", leg=strategy.cache.order(strategy.slots[h.AAA].protect_id)
    )
    assert c.row(strategy) == State("FZW"), c.row(strategy)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert strategy.canceled == [leg.client_order_id], "the hold cancels our sell, frozen or not"
    strategy.deliver_then_step("Canceled")
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    for n in range(5, 9):
        under(strategy, n, "9.70")
        step(strategy, n)
    assert strategy.sent == [leg], "a fault's freeze is not lifted by a hold and its release"
    assert c.row(strategy) == State("FZ"), "the release reads the recorded fault: frozen again"
    assert strategy._holding_state(h.AAA, strategy.slots[h.AAA])[0] == "frozen"
    told = [line for line in strategy.said if line.startswith("ALERT:") and "T0 x RL" in line]
    assert len(told) == 1, told


def test_a_definitive_refusal_during_the_hold_is_not_cured_by_the_release(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1, "9.89")
    under(strategy, 2, "9.88")
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert c.row(strategy) == State("TW"), c.row(strategy)
    strategy.deliver_then_step("Denied")
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    for n in range(5, 9):
        under(strategy, n, "9.70")
        step(strategy, n)
    assert strategy.sent == [leg], "a definitive refusal is not cured by a hold and its release"
    assert c.row(strategy) == State("FZ"), c.row(strategy)
    assert strategy._holding_state(h.AAA, strategy.slots[h.AAA])[0] == "frozen"
    assert [line for line in strategy.said if line.startswith("ALERT:") and "T0 x RL" in line]


def test_a_refusal_during_the_hold_still_binds_the_first_leg_after_the_release(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1, "9.89")
    under(strategy, 2, "9.88")
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    strategy.deliver_then_step("Rejected", reason="synthetic venue refusal")
    assert c.row(strategy) == State("T0"), c.row(strategy)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    assert c.row(strategy) == State("G", 0), c.row(strategy)
    under(strategy, 5, "9.70")
    under(strategy, 6, "9.69")
    assert strategy.sent == [leg], "the count is complete, but the refusal's wait still runs"
    under(strategy, 9, "9.68")
    assert len(strategy.sent) == 2, "sent once the refusal's wait has passed"


def test_stored_order_events_are_judged_on_a_quote_under_the_hold(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    under(strategy, 1, "9.89")
    under(strategy, 2, "9.88")
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    strategy.deliver("Submitted")
    strategy.deliver("Accepted")
    assert strategy.canceled == [], "stored, not yet judged"
    under(strategy, 3, "9.87")
    assert strategy.canceled == [leg.client_order_id], (
        "the owed cancel is asked at the next quote, not only at the next timer step"
    )


def test_a_hold_arriving_with_a_reprice_cancel_answered_sends_no_next_leg(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    strategy.test_clock.set_time(h.T0 + 60 * h.SECOND)
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    assert c.row(strategy) == State("PCR"), c.row(strategy)
    strategy.deliver("Canceled")
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 62)
    assert strategy.sent == [leg], "the stop is judged before the cancel's answer: no next leg"
    assert c.row(strategy) == State("T0"), c.row(strategy)
    assert not [line for line in strategy.said if "not by us" in line], (
        "the hold's cancel is not told as another hand's"
    )


def test_a_symbol_held_at_the_start_is_not_sold_before_any_step(tmp_path: Path) -> None:
    symbol_hold.write_hold(tmp_path, "AAA")
    strategy = two_quote(tmp_path)
    strategy.on_start()
    for n in range(1, 5):
        under(strategy, n)
    assert strategy.sent == [], "the start's own read holds the symbol before the first step"


def test_every_other_holding_goes_on_counting(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    strategy.cached = s.TwoCache()
    strategy.adopt_holding(s.BBB, h.Price.from_str("19.80"))
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 1)
    s.tick(strategy, h.AAA, "9.80", h.T0 + 2 * h.SECOND)
    s.tick(strategy, s.BBB, "19.70", h.T0 + 2 * h.SECOND)
    assert c.row(strategy) == State("T0"), "the held symbol is stopped"
    assert strategy.slots[s.BBB].sale_state == State("G", 1), "the other holding counts"


def test_the_hold_never_cancels_an_order_the_owner_placed(tmp_path: Path) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    owner = h.working_sell("owner-order", qty="3", filled="0")
    strategy.cached.working.insert(0, owner)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    strategy.on_stop()
    sent = {order.client_order_id for order in strategy.sent}
    assert set(strategy.canceled) <= sent, "only IDs this instance sent are ever canceled"
    assert owner.client_order_id not in strategy.canceled


@pytest.mark.xfail(
    strict=True,
    reason="count-two follow-up: node-report (under the hold the status hides a recorded fault "
    "and says that removing the hold re-arms the holding)",
)
def test_the_status_under_a_hold_names_a_recorded_fault(tmp_path: Path) -> None:
    """A fault of ours froze the holding, then the owner held the symbol. The release will freeze
    it again, so the status must not say that removing the hold re-arms it."""
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    strategy.deliver_then_step(
        "Unrecognized", leg=strategy.cache.order(strategy.slots[h.AAA].protect_id)
    )
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    strategy.deliver_then_step("Canceled")
    step(strategy, 4)
    assert c.row(strategy) == State("T0"), c.row(strategy)
    state, _, reason = strategy._holding_state(h.AAA, strategy.slots[h.AAA])
    assert "re-arms" not in (reason or ""), reason
    assert state == "frozen", (state, reason)
