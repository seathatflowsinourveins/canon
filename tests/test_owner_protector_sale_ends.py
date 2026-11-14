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
    strategy.deliver("CancelRejected", leg=strategy.cache.order(leg.client_order_id))
    step(strategy, 6)
    step(strategy, 7)
    assert strategy.canceled == [leg.client_order_id] * 2, (
        "the reopened holding is at least as large as our leg, so no mismatch ever asks: the "
        "holding's end, seen again, does"
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
