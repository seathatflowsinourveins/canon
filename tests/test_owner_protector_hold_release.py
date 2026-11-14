"""A released hold re-arms the trail, and a late fill on a held sale is still ours (count one).

The Codex second reader's findings on main 1b052804 (rd-owner-protector F1 and F2), held as the lead's
ruling of 2026-11-14 10:15:

F1. When the owner removes a hold, the holding is re-armed with a trailing stop. A sale canceled
    under the hold is never sent again, whatever the order of cancel-processed, quote-recovered and
    hold-removed.
F2. A fill that arrives late on a sale canceled under a hold is still recognised as ours, and in that
    same event a replacement working for more than is now held is withdrawn, as a late fill on an
    ended leg already is (test_owner_protector_chase.py::
    test_a_late_fill_on_an_ended_leg_withdraws_the_working_leg_in_that_event).

The first case of each is the reader's own probe, its assertions kept. Offline: the Harness of
tests/test_owner_protector.py, the native order factory and the native Canceled and
Canceled -> Filled transitions (nt@d53849b216e2 crates/model/src/orders/mod.rs:247). All prices and
quantities are synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

from nautilus_trader.core import UUID4
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce

from canon_trading import symbol_hold
from tests import test_owner_protector as h
from tests.test_owner_protector_chase import accepted
from tests.test_owner_protector_chase import filled
from tests.test_owner_protector_chase import released
from tests.test_owner_protector_chase import terminal
from tests.test_owner_protector_growth import step


def test_quick_hold_release_must_not_resume_the_canceled_sale(tmp_path):
    """F1, the reader's probe: the cancel lands and the hold is removed before any held step."""
    strategy, sale = released()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 2 * h.SECOND)
    assert sale.client_order_id in strategy.canceled
    # Recovery while held: no further breach exists.
    strategy.observe("10.00", h.T0 + 3 * h.SECOND)
    # The cancel and the owner's release both arrive before the next management pass.
    terminal(sale, h.T0 + 4 * h.SECOND)
    symbol_hold.remove_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 5 * h.SECOND)
    new_orders = strategy.sent[1:]
    assert new_orders, "the recovered holding should have a protective trail"
    assert new_orders[-1].order_type == OrderType.TRAILING_STOP_LIMIT, (
        "hold release sent an immediate LIMIT sale above the retained level",
        new_orders[-1].order_type,
        strategy.slots[h.AAA].level,
        new_orders[-1].price,
        strategy.said[-10:],
    )


def test_a_hold_removed_before_its_cancel_lands_re_arms_the_trail_once_it_lands(tmp_path):
    """F1, the other order: the hold is removed while our cancel is still pending, the bid has
    recovered, and the cancel lands after the release. The sale is not sent again: the trail is."""
    strategy, sale = released()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 2 * h.SECOND)
    assert sale.client_order_id in strategy.canceled
    symbol_hold.remove_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 3 * h.SECOND)
    assert len(strategy.sent) == 1, "nothing is sent while our cancel is pending"
    strategy.observe("10.00", h.T0 + 4 * h.SECOND)
    terminal(sale, h.T0 + 5 * h.SECOND)
    step(strategy, h.T0 + 6 * h.SECOND)
    new_orders = strategy.sent[1:]
    assert all(o.order_type != OrderType.LIMIT for o in new_orders), (
        "the sale canceled under the hold was sent again after its release",
        [(o.order_type, o.price) for o in new_orders],
        strategy.said[-10:],
    )
    assert new_orders and new_orders[-1].order_type == OrderType.TRAILING_STOP_LIMIT, strategy.said[
        -10:
    ]
    assert new_orders[-1].quantity == Quantity.from_int(10)
    assert strategy.slots[h.AAA].sale is None


def test_hold_must_keep_old_sale_ids_for_late_fill_withdrawal(tmp_path):
    """F2, the reader's probe: the cancel is processed at a held step, then a late fill on it."""
    strategy, sale = released()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 2 * h.SECOND)
    terminal(sale, h.T0 + 3 * h.SECOND)
    step(strategy, h.T0 + 4 * h.SECOND)
    assert strategy.slots[h.AAA].sale is None
    strategy.observe("10.00", h.T0 + 5 * h.SECOND)
    symbol_hold.remove_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 6 * h.SECOND)
    new = strategy.sent[-1]
    assert new.order_type == OrderType.TRAILING_STOP_LIMIT
    assert new.quantity == Quantity.from_int(10)
    # Exact native Canceled -> Filled transition used by the suite's late-fill proof.
    filled(strategy, sale, 4, h.T0 + 7 * h.SECOND)
    assert strategy.cached.held[0].signed_qty == 6
    assert new.client_order_id in strategy.canceled, (
        "late fill left a protective order for 10 working against 6 held",
        new.quantity,
        strategy.cached.held[0].signed_qty,
    )


def test_a_late_fill_on_a_sale_whose_cancel_landed_after_the_release_withdraws_the_trail(tmp_path):
    """F2 on F1's path: the hold is removed before the cancel lands, the trail is re-armed for the
    whole holding, and a late fill on the canceled sale withdraws it in the fill's own event."""
    strategy, sale = released()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 2 * h.SECOND)
    strategy.observe("10.00", h.T0 + 3 * h.SECOND)
    terminal(sale, h.T0 + 4 * h.SECOND)
    symbol_hold.remove_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 5 * h.SECOND)
    new = strategy.sent[-1]
    assert new.order_type == OrderType.TRAILING_STOP_LIMIT, strategy.said[-10:]
    assert new.quantity == Quantity.from_int(10)
    filled(strategy, sale, 4, h.T0 + 6 * h.SECOND)
    assert strategy.cached.held[0].signed_qty == 6
    assert new.client_order_id in strategy.canceled, (
        "late fill left a protective order for 10 working against 6 held",
        strategy.said[-10:],
    )
    assert len(strategy.sent) == 2, "nothing new is sent until the trail's cancel is final"


def test_a_rejected_hold_cancel_leaves_the_leg_working_and_a_held_step_cancels_it_again(tmp_path):
    """The mark that ends a held sale names only a cancel the venue has not refused: a rejected
    cancel clears it, so the chase's own later cancel of that leg is not read as the hold's."""
    strategy, sale = released()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 2 * h.SECOND)
    assert strategy.slots[h.AAA].held_cancel == sale.client_order_id
    rejected = SimpleNamespace(
        instrument_id=h.AAA, client_order_id=sale.client_order_id, reason="synthetic"
    )
    strategy.on_order_cancel_rejected(cast(Any, rejected))
    assert strategy.slots[h.AAA].held_cancel is None
    assert strategy.slots[h.AAA].sale is not None, "the leg works on: its sale is not ended"
    step(strategy, h.T0 + 3 * h.SECOND)
    assert strategy.canceled.count(sale.client_order_id) == 2, "a held step cancels it again"
    assert strategy.slots[h.AAA].held_cancel == sale.client_order_id
    assert len(strategy.sent) == 1, "nothing is sent while held"


def test_a_trail_held_before_its_release_and_canceled_as_a_limit_is_not_sent_again(tmp_path):
    """F1 with the hold first: the hold cancels our trail before it is released, the emulator
    releases it into a limit (same client ID) before the cancel lands, and the cancel lands and the
    hold is removed with no pass between. The release re-arms the trail; the limit is not resent.
    Whether the pinned emulator can release an order whose cancel is pending is UNVERIFIED."""
    strategy = h.Harness()
    strategy.settings.symbol_holds_dir = tmp_path / "holds"
    strategy.test_clock.set_time(h.T0)
    strategy.cached.latest = h.quote("10.00", h.T0)
    strategy.adopt_holding(h.AAA, Price.from_str("9.95"))
    h.new_quote(strategy)
    trailing = strategy.sent[0]
    symbol_hold.write_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + h.SECOND)
    assert trailing.client_order_id in strategy.canceled
    strategy.observe("10.00", h.T0 + h.SECOND + h.SECOND // 2)  # the bid, still held
    at = h.T0 + 2 * h.SECOND
    strategy.test_clock.set_time(at)
    sale = strategy.order_factory.limit(
        h.AAA,
        OrderSide.SELL,
        Quantity.from_int(10),
        Price.from_str("9.90"),
        time_in_force=TimeInForce.DAY,
        reduce_only=True,
        client_order_id=trailing.client_order_id,
    )
    event = OrderReleased(
        sale.trader_id,
        sale.strategy_id,
        h.AAA,
        sale.client_order_id,
        Price.from_str("9.89"),
        UUID4(),
        at,
        at,
    )
    sale.apply(event)
    strategy.cached.working = [sale]
    strategy.on_order_released(event)
    accepted(sale, at)
    # No pass runs between the release and the cancel landing, nor between that and the owner's
    # release of the hold: no held pass ever sees the released limit.
    terminal(sale, h.T0 + 3 * h.SECOND)
    symbol_hold.remove_hold(strategy.settings.symbol_holds_dir, "AAA")
    step(strategy, h.T0 + 4 * h.SECOND)
    new_orders = strategy.sent[1:]
    assert all(o.order_type != OrderType.LIMIT for o in new_orders), (
        "the limit canceled under the hold was sent again after its release",
        [(o.order_type, o.price) for o in new_orders],
        strategy.said[-10:],
    )
    assert new_orders and new_orders[-1].order_type == OrderType.TRAILING_STOP_LIMIT, strategy.said[
        -10:
    ]
    assert strategy.slots[h.AAA].sale is None
