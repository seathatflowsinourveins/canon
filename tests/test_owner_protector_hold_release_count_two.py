"""The count-two counterparts of rd-owner-protector F1 and F2 (a second opinion, not the proof).

At a count above one the hold acts through the sale table (`_hold_counted`), and every order event
of a leg we sent this run is queued by `on_order_event` (admitted by `sent_ids`, not by the current
sale) and judged at the next step or quote. These cases ask the two questions of that table:
F1: does a sale canceled under the hold get sent again when the cancel lands and the hold is
removed before the next step? F2: is a late fill on the canceled held leg still judged as ours,
and is a later leg working for more than is held withdrawn for size?

Offline: the stand-in of tests/test_owner_protector_sale_table.py; synthetic holdings and orders.
"""

from __future__ import annotations

from pathlib import Path

from canon_trading import sale_table
from canon_trading import symbol_hold
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c
from tests.test_owner_protector_symbol_hold_count_two import alpaca_stream_lock  # noqa: F401
from tests.test_owner_protector_symbol_hold_count_two import isolated_level_books  # noqa: F401
from tests.test_owner_protector_symbol_hold_count_two import step
from tests.test_owner_protector_symbol_hold_count_two import two_quote
from tests.test_owner_protector_symbol_hold_count_two import under

State = sale_table.State


def test_count_two_a_cancel_landed_before_a_quick_release_counts_again_and_resends_nothing(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    assert strategy.canceled == [leg.client_order_id]
    strategy.deliver("Canceled")  # stored only: no step judges it before the release
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    assert strategy.sent == [leg], "the canceled sale is not sent again by the release"
    # The stored cancel is judged in the release's own call (T0), so RL meets a stopped row there
    # and the count starts again at the next call: one step late, nothing sent.
    assert c.row(strategy) in (State("T0"), State("G", 0)), c.row(strategy)
    step(strategy, 5)
    assert c.row(strategy) == State("G", 0), (c.row(strategy), strategy.said[-6:])
    assert strategy.sent == [leg], "the canceled sale is not sent again by the release"
    under(strategy, 6, "9.70")
    assert strategy.sent == [leg], "one counted quote cannot sell"


def test_count_two_a_late_fill_on_the_held_leg_is_ours_and_sizes_the_next_leg(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    strategy.deliver_then_step("Canceled")
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    assert c.row(strategy) == State("G", 0), c.row(strategy)
    strategy.deliver("Filled", quantity=4, leg=strategy.cache.order(leg.client_order_id))
    step(strategy, 5)
    assert strategy.cached.held[0].signed_qty == 6
    under(strategy, 6, "9.70")
    under(strategy, 7, "9.69")
    assert len(strategy.sent) == 2, (c.row(strategy), strategy.said[-6:])
    assert strategy.sent[-1].quantity == h.Quantity.from_int(6)


def test_count_two_a_late_fill_on_the_held_leg_withdraws_a_later_leg_working_for_more(
    tmp_path: Path,
) -> None:
    strategy = two_quote(tmp_path)
    c.first_leg(strategy)
    (leg,) = strategy.sent
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, 3)
    strategy.deliver_then_step("Canceled")
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, 4)
    under(strategy, 5, "9.70")
    under(strategy, 6, "9.69")
    assert len(strategy.sent) == 2
    second = strategy.sent[-1]
    assert second.quantity == h.Quantity.from_int(10)
    strategy.deliver_then_step("Submitted", leg=strategy.cache.order(second.client_order_id))
    strategy.deliver_then_step("Accepted", leg=strategy.cache.order(second.client_order_id))
    strategy.deliver("Filled", quantity=4, leg=strategy.cache.order(leg.client_order_id))
    step(strategy, 7)
    assert strategy.cached.held[0].signed_qty == 6
    assert second.client_order_id in strategy.canceled, (
        "a later leg for 10 left working against 6 held",
        c.row(strategy),
        strategy.said[-6:],
    )
