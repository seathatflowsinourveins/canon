"""A canceled buy after a complete sale starts a holding of its own (r-l5comp-ag/P1-1, P1-4).

NautilusTrader d53849b216e2 permits canceled orders to fill (orders/mod.rs:247) and opens a
new position after the previous one closed (execution/src/engine/mod.rs:4722-4767). These
offline event/cache seams keep the sale record, fresh protection and the fill receipt clock.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover_late_fill_blind import LateFillSeam
from tests.test_mover_late_fill_blind import entry
from tests.test_mover_late_fill_blind import event

D = Decimal


class ReceiptLateFillSeam(LateFillSeam):
    """The existing offline driver with a distinct host receipt on its fill events."""

    def on_order_filled(self, fill: Any) -> None:
        mover.Mover.on_order_filled(
            cast(Any, self), SimpleNamespace(**vars(fill), ts_init=self.now)
        )


def protected_newer(host: ReceiptLateFillSeam) -> tuple[Any, Any, dict[str, Any], Any]:
    """A canceled entry's slot closes; a newer entry of the name fills and is protected by a
    trailing stop. Returns the slot, the canceled entry, its record and the stop."""
    _, old = entry(host)
    old.price = Price.from_str("10.5000")
    old_record = host.entries_sent[-1]
    old_record.update(
        trail="0.007",
        exit_through="0.002",
        chase_cap="0.02",
        trail_bound="fixed",
        sizing={"loss_per_share": "0.07"},
    )
    host.cancel_order(old.client_order_id)
    host.rank_ns = T0
    host.step(T0 + SECOND)
    assert AAA not in host.slots

    host._open_slot(AAA)
    slot = host.slots[AAA]
    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-new")
    newer.price = Price.from_str("10.4000")
    newer.filled_qty, newer.avg_px = Quantity.from_int(0), None
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders[newer.client_order_id.value] = newer
    slot.entry_id = newer.client_order_id
    slot.sized_loss = D("0.07")
    slot.entry_record = {"client_order_id": "E-new", "sizing": {"loss_per_share": "0.07"}}
    host.entries_sent.append(slot.entry_record)

    host.bid = "10.4500"
    host.now = T0 + 3 * SECOND
    host.fill(newer, 5, "10.4000", host.now)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.40"))]
    host.step(T0 + 4 * SECOND)
    stop = host.submitted[-1]
    assert stop.kind == "TRAILING"
    assert stop.trigger_price == Price.from_str("10.3300")
    return slot, old, old_record, stop


def test_late_buy_after_active_slot_sold_in_full_before_step_arms_stop_instead_of_dumping() -> None:
    host = ReceiptLateFillSeam()
    slot, old, old_record, stop = protected_newer(host)
    completed = slot.exit_record

    # Release and sell the newer holding in full before the next timer can flatten it.
    stop.is_emulated, stop.is_open = False, True
    stop.price = Price.from_str("10.3200")
    stop.events = lambda: [SimpleNamespace(trigger_price=Price.from_str("10.3300"))]
    host.on_order_released(
        event(
            stop.client_order_id,
            T0 + 4 * SECOND + 100_000_000,
            released_price=Price.from_str("10.3200"),
        )
    )
    sold_at = T0 + 4 * SECOND + 200_000_000
    host.fill(stop, 5, "10.3200", sold_at)
    assert host.positions == []
    assert completed is not None and completed.final_fill_ns == sold_at

    # The canceled buy arrives after the sale, with a receipt later than its venue time.
    host.bid = "10.6000"
    received = host.now = T0 + 4 * SECOND + 700_000_000
    host.fill(old, 5, "10.5000", T0 + 4 * SECOND + 500_000_000)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.50"))]

    before = len(host.submitted)
    host.step(T0 + 5 * SECOND)
    sent = host.submitted[before:]

    assert not slot.exiting, "new late-bought holding above stop must not inherit closed exit"
    assert [(o.kind, o.quantity, o.trigger_price) for o in sent] == [
        ("TRAILING", Quantity.from_int(5), Price.from_str("10.4300"))
    ]
    assert len(host.exits) == 2
    assert host.exits[0] is completed
    assert (completed.quantity, completed.filled, completed.notional, completed.final_fill_ns) == (
        D(5),
        D(5),
        D("51.60"),
        sold_at,
    )
    assert host.sold_rows[AAA] is completed
    assert host.sold[AAA][1] == D("10.32")
    assert host.sold_fill_ns[AAA] == sold_at
    assert host.reentry_states[AAA].sale_ns == sold_at
    assert host.reentry_states[AAA].sale_price == D("10.32")
    assert slot.exit_record is host.exits[1]
    assert slot.exit_record.entry_client_order_id == old.client_order_id.value
    assert (slot.exit_record.quantity, slot.exit_record.bought_notional) == (D(5), D("52.50"))
    assert slot.exit_record.filled == 0 and slot.exit_record.final_fill_ns is None
    assert slot.exit_record.trigger_ns is None
    assert slot.entry_id == old.client_order_id
    assert slot.entry_record is old_record
    assert slot.entry_received_ns == received
    assert slot.sized_loss == D("0.07")
    assert slot.trail_plan == slot.initial_trail
    assert slot.trail_plan is not None and slot.trail_plan.trail == D("0.007")


def test_cold_fallback_late_buy_recovered_after_slot_warmed_and_flattened_tightens_on_warm_atr() -> (
    None
):
    # MoverConfig is an upstream StrategyConfig, not a dataclass: set the arm at construction.
    host: Any = LateFillSeam(trail_mode="atr")
    host.cache.bars = lambda _: []
    host._request_history = lambda slot: None  # this case supplies the cold/warm ATR explicitly
    host.rank, host.rank_ns = [(AAA, D("0.25"))], T0
    slot, old = entry(host)
    slot.atr = SimpleNamespace(initialized=False, value=0.0)
    host.entries_sent[-1].update(
        trail="0.02",
        exit_through="0.005",
        chase_cap="0.02",
        trail_bound="fixed",
        trail_source="cold_atr_fallback",
    )
    host.cancel_order(old.client_order_id)
    host.step(T0 + SECOND)
    assert AAA in host.slots

    newer = SimpleNamespace(**vars(old))
    newer.client_order_id = ClientOrderId("E-1")
    newer.filled_qty, newer.avg_px = Quantity.from_int(0), None
    newer.is_closed, newer.is_open = False, True
    newer.ts_init = T0 + 2 * SECOND
    host.orders["E-1"] = newer
    slot.entry_id = newer.client_order_id
    slot.trail_plan = slot.initial_trail = host._cold_plan("cold", D("0.01"), D("0.002"), None)
    slot.entry_record = {
        "client_order_id": "E-1",
        "trail": "0.02",
        "exit_through": "0.005",
        "chase_cap": "0.02",
        "trail_bound": "fixed",
        "trail_source": "cold_atr_fallback",
    }
    host.entries_sent.append(slot.entry_record)

    host.fill(newer, 5, "10.0000", T0 + 3 * SECOND)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.00"))]
    host.rank_ns = T0 + 4 * SECOND
    host.step(T0 + 4 * SECOND)
    assert host.submitted[-1].trailing_offset == D("0.20")

    slot.atr = SimpleNamespace(initialized=True, value=0.05)
    host.rank_ns = T0 + 5 * SECOND
    host.step(T0 + 5 * SECOND)
    warmed_stop = host.submitted[-1]
    assert warmed_stop.trailing_offset == D("0.05")
    assert slot.warm_checked is True

    host.fill(warmed_stop, 5, "10.4500", T0 + 6 * SECOND)
    assert host.positions == []
    host.rank_ns = T0 + 7 * SECOND
    host.step(T0 + 7 * SECOND)
    assert not slot.was_held

    host.fill(old, 5, "10.0000", T0 + 8 * SECOND)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.00"))]
    host.rank_ns = T0 + 9 * SECOND
    host.step(T0 + 9 * SECOND)
    assert host.submitted[-1].trailing_offset == D("0.20")

    host.rank_ns = T0 + 10 * SECOND
    host.step(T0 + 10 * SECOND)
    assert host.submitted[-1].trailing_offset == D("0.05")


class PendingCancelSeam(ReceiptLateFillSeam):
    """The driver above, where a cancel of a named order stays pending until the test closes it,
    as a venue's acknowledgement would."""

    def __init__(self, **rows: Any) -> None:
        super().__init__(**rows)
        self.held_cancels: set[str] = set()

    def cancel_order(self, client_order_id: Any) -> None:
        if client_order_id.value not in self.held_cancels:
            super().cancel_order(client_order_id)
            return
        self.canceled.append(client_order_id.value)
        order = self._order(client_order_id)
        assert order is not None
        order.is_pending_cancel = True

    def close_canceled(self, order: Any) -> None:
        self.held_cancels.discard(order.client_order_id.value)
        order.is_pending_cancel = False
        order.is_closed, order.is_open, order.is_emulated = True, False, False
        self.on_order_canceled(event(order.client_order_id, self.now))


def replaced_stop(host: ReceiptLateFillSeam, stop: Any) -> Any:
    """Cancel the newer holding's stop; the next step replaces it. Returns the replacement."""
    host.now = T0 + 4 * SECOND + 50_000_000
    host.cancel_order(stop.client_order_id)
    host.step(T0 + 4 * SECOND + 60_000_000)
    replacement = host.submitted[-1]
    assert replacement is not stop and replacement.kind == "TRAILING"
    return replacement


def release(host: ReceiptLateFillSeam, order: Any, at: int) -> None:
    """The emulator releases a trailing stop to its limit at 10.32."""
    order.is_emulated, order.is_open = False, True
    order.price = Price.from_str("10.3200")
    order.events = lambda: [SimpleNamespace(trigger_price=Price.from_str("10.3300"))]
    host.on_order_released(event(order.client_order_id, at, released_price=order.price))


def test_late_buy_waits_for_the_sold_holdings_open_leg_before_its_own_record_and_stop() -> None:
    host = PendingCancelSeam()
    slot, old, old_record, stop = protected_newer(host)
    completed = slot.exit_record
    replacement = replaced_stop(host, stop)
    assert slot.protect_id == replacement.client_order_id
    release(host, replacement, T0 + 4 * SECOND + 100_000_000)

    # The canceled first leg sells the holding in full; the released replacement is still open.
    sold_at = T0 + 4 * SECOND + 200_000_000
    host.fill(stop, 5, "10.3200", sold_at)
    assert host.positions == []
    assert completed is not None and completed.final_fill_ns == sold_at
    assert not replacement.is_closed

    # The canceled buy fills after the sale, while that leg is still open.
    host.held_cancels.add(replacement.client_order_id.value)
    host.bid = "10.6000"
    received = host.now = T0 + 4 * SECOND + 700_000_000
    host.fill(old, 5, "10.5000", T0 + 4 * SECOND + 500_000_000)
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.50"))]

    before, canceled = len(host.submitted), len(host.canceled)
    host.step(T0 + 5 * SECOND)
    assert host.submitted[before:] == [], "nothing is sold or protected through the old exit"
    assert replacement.client_order_id.value in host.canceled[canceled:]
    assert replacement.is_pending_cancel and not replacement.is_closed
    assert slot.exit_record is completed
    # The late shares are not added to the sold holding by managing it (`_ensure_exit`).
    assert (completed.quantity, completed.filled, completed.final_fill_ns) == (D(5), D(5), sold_at)
    assert host.late_buys[AAA].entry_client_order_id == old.client_order_id.value
    assert AAA not in host.sold_rows

    host.now = T0 + 5 * SECOND + 500_000_000
    host.close_canceled(replacement)
    host.step(T0 + 6 * SECOND)
    sent = host.submitted[before:]
    assert [(o.kind, o.quantity, o.trigger_price) for o in sent] == [
        ("TRAILING", Quantity.from_int(5), Price.from_str("10.4300"))
    ]
    assert not slot.exiting
    assert slot.protect_id == sent[0].client_order_id
    assert host.sold_rows[AAA] is completed
    assert (completed.quantity, completed.filled, completed.final_fill_ns) == (D(5), D(5), sold_at)
    assert host.sold[AAA][1] == D("10.32")
    assert host.sold_fill_ns[AAA] == sold_at
    assert host.reentry_states[AAA].sale_ns == sold_at
    assert AAA not in host.late_buys
    assert slot.exit_record is not completed and slot.exit_record is host.exits[-1]
    assert slot.exit_record.entry_client_order_id == old.client_order_id.value
    assert (slot.exit_record.quantity, slot.exit_record.bought_notional) == (D(5), D("52.50"))
    assert slot.exit_record.filled == 0 and slot.exit_record.final_fill_ns is None
    assert slot.entry_id == old.client_order_id
    assert slot.entry_record is old_record
    assert slot.entry_received_ns == received
    assert slot.trail_plan is not None and slot.trail_plan.trail == D("0.007")


def test_remainder_of_the_slots_own_entry_after_its_first_part_sold_joins_the_slots_holding() -> (
    None
):
    host = ReceiptLateFillSeam()
    host.rank_ns = T0
    slot, buy = entry(host)
    host.now = T0 + SECOND
    host.fill(buy, 2, "10.0000", host.now)
    host.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.00"))]
    host.step(T0 + 2 * SECOND)
    stop = host.submitted[-1]
    assert stop.kind == "TRAILING" and stop.quantity == Quantity.from_int(2)
    row = slot.exit_record
    assert row is not None and row.quantity == D(2)

    release(host, stop, T0 + 2 * SECOND + 100_000_000)
    host.fill(stop, 2, "10.3200", T0 + 2 * SECOND + 200_000_000)
    assert row.final_fill_ns is not None and row.filled == row.quantity

    # The rest of the slot's own entry fills before the next step cancels it.
    host.now = T0 + 2 * SECOND + 500_000_000
    host.fill(buy, 3, "10.0000", host.now)

    assert AAA not in host.late_buys
    assert slot.exit_record is row
    assert row.entry_client_order_id == buy.client_order_id.value
    assert (row.quantity, row.filled, row.final_fill_ns) == (D(5), D(2), None)
    assert row.bought_notional == D("50.0000")


def test_late_fill_of_the_sold_holdings_canceled_leg_is_charged_to_that_holding() -> None:
    host = ReceiptLateFillSeam()
    slot, old, _, stop = protected_newer(host)
    completed = slot.exit_record
    replacement = replaced_stop(host, stop)
    release(host, replacement, T0 + 4 * SECOND + 100_000_000)
    sold_at = T0 + 4 * SECOND + 200_000_000
    host.fill(replacement, 5, "10.3200", sold_at)
    assert completed is not None and completed.final_fill_ns == sold_at

    host.bid = "10.6000"
    host.now = T0 + 4 * SECOND + 700_000_000
    host.fill(old, 5, "10.5000", T0 + 4 * SECOND + 500_000_000)
    late = host.late_buys[AAA]

    # The first, canceled leg of the sold holding fills afterwards.
    host.on_order_filled(
        event(
            stop.client_order_id,
            T0 + 4 * SECOND + 300_000_000,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.3000"),
        )
    )
    assert late.filled == 0 and late.final_fill_ns is None
    assert (completed.filled, completed.notional, completed.final_fill_ns) == (
        D(5),
        D("51.60"),
        sold_at,
    )
    assert f"{AAA}: {stop.client_order_id} sold 5 beyond its holding's 5" in host.dropped


def test_remainder_of_the_slots_own_entry_joins_a_waiting_late_buy_not_the_sold_holding() -> None:
    """code-review-l5-latebuy2/F1: once a late buy waits behind a sold holding, the slot's own
    entry's remainder joins it, so the sold holding stays sold and is recorded by `_flat`."""
    host = ReceiptLateFillSeam()
    host.rank_ns = T0
    slot, buy = entry(host)
    early = SimpleNamespace(**vars(buy))
    early.client_order_id = ClientOrderId("E-early")
    early.quantity = Quantity.from_int(4)
    early.is_closed, early.is_open = True, False
    host.orders[early.client_order_id.value] = early
    early_record = {
        "client_order_id": "E-early",
        "instrument_id": str(AAA),
        "entry_price": None,
        "stopped_out": False,
        "trail": "0.007",
        "exit_through": "0.002",
        "chase_cap": "0.02",
        "trail_bound": "fixed",
        "sizing": {"loss_per_share": "0.07"},
    }
    host.entries_sent.append(early_record)

    host.now = T0 + SECOND
    host.fill(buy, 2, "10.0000", host.now)
    host.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.00"))]
    host.step(T0 + 2 * SECOND)
    stop = host.submitted[-1]
    row = slot.exit_record
    assert row is not None and stop.kind == "TRAILING"
    release(host, stop, T0 + 2 * SECOND + 100_000_000)
    sold_at = T0 + 2 * SECOND + 200_000_000
    host.fill(stop, 2, "10.3200", sold_at)
    assert row.final_fill_ns == sold_at and row.filled == row.quantity

    # A canceled earlier entry fills late, then the rest of the slot's own entry.
    host.bid = "10.6000"
    host.now = T0 + 2 * SECOND + 400_000_000
    host.fill(early, 4, "10.1000", host.now)
    late = host.late_buys[AAA]
    host.now = T0 + 2 * SECOND + 500_000_000
    host.fill(buy, 3, "10.0000", host.now)

    assert host.late_buys[AAA] is late
    assert (late.quantity, late.bought_notional) == (D(7), D("70.4000"))
    assert (row.quantity, row.filled, row.final_fill_ns) == (D(2), D(2), sold_at)

    host.positions = [SimpleNamespace(signed_qty=D(7), avg_px_open=D("70.4") / 7)]
    host.rank_ns = T0 + 3 * SECOND
    before = len(host.submitted)
    host.step(T0 + 3 * SECOND)
    sent = host.submitted[before:]
    assert [(o.kind, o.quantity) for o in sent] == [("TRAILING", Quantity.from_int(7))]
    assert AAA not in host.late_buys
    assert host.sold_rows[AAA] is row
    assert host.sold_fill_ns[AAA] == sold_at
    assert slot.exit_record is late and slot.exit_record is host.exits[-1]
    assert not slot.exiting


def test_a_sold_row_no_step_held_gives_the_slot_to_the_late_buy_at_once() -> None:
    """code-review-l5-latebuy2/F5: a sold-out record with `was_held` unset has no sale for `_flat`
    to record; the late buy is attached and protected, not put back on every step."""
    host = ReceiptLateFillSeam()
    slot, old, _, stop = protected_newer(host)
    stale = slot.exit_record
    assert stale is not None
    host.cancel_order(stop.client_order_id)
    stop.is_closed, stop.is_open, stop.is_emulated = True, False, False
    stale.filled, stale.notional = stale.quantity, D("51.60")
    stale.final_fill_ns = T0 + 4 * SECOND + 200_000_000
    slot.was_held, slot.protect_id = False, None

    host.bid = "10.6000"
    host.now = T0 + 4 * SECOND + 700_000_000
    host.fill(old, 5, "10.5000", T0 + 4 * SECOND + 500_000_000)
    late = host.late_buys[AAA]
    host.positions = [SimpleNamespace(signed_qty=D(5), avg_px_open=D("10.50"))]

    before = len(host.submitted)
    host.step(T0 + 5 * SECOND)
    sent = host.submitted[before:]
    assert [(o.kind, o.quantity, o.trigger_price) for o in sent] == [
        ("TRAILING", Quantity.from_int(5), Price.from_str("10.4300"))
    ]
    assert AAA not in host.late_buys
    assert slot.exit_record is late
