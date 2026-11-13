"""Blind late-fill properties through the existing offline Mover event/cache seams.

Contract supplied by the task: NautilusTrader d53849b216e2, orders/mod.rs:247,295
permits fills after cancellation. The pinned source copy was unavailable this session.
No network, account, catalog, ledger, or paper-state path is used by this driver.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import strategy

D = Decimal
ARMS = ("clock", "reclaim", "turn", "vwap")


def event(cid: Any, at: int, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(instrument_id=AAA, client_order_id=cid, ts_event=at, **fields)


class LateFillSeam(Seam):
    """Real Mover methods and state, with the existing Seam's offline cache and venue.

    The extra engine boundaries supply subscriptions, a clock, and a calculated
    account. Order events are explicit; only a test's fill changes its position.
    """

    def __init__(self, **rows: Any) -> None:
        super().__init__("10.50", **rows)
        # The real constructor supplies bookkeeping, including future plain state;
        # Seam supplies the engine attributes, which are not in this dictionary.
        self.__dict__.update(strategy(extended_hours=True, cooldown_secs=0, **rows).__dict__)
        self.submitted: list[Any] = []
        self.rank: list[Any] = []
        self.rank_ns: int = 0
        self.now = T0
        self.clock = SimpleNamespace(timestamp_ns=lambda: self.now)
        self.cache.order = self._order
        self.cache.instrument = self._instrument
        self.cache.trades = (
            lambda _: []
        )  # no cached print: a slot seeds from none (`_newest_cached`)
        self.cache.quote = lambda _: SimpleNamespace(
            bid_price=Price.from_str(self.bid),
            ask_price=Price.from_str(str(D(self.bid) + D("0.01"))),
            # a quote carries its venue and receipt times (`mover.quote_lag`)
            ts_event=self.now,
            ts_init=self.now,
        )

    def __getattr__(self, name: str) -> Any:
        # As in Seam's explicit aliases, every strategy method under test is real.
        return getattr(mover.Mover, name).__get__(self, type(self))

    def _subscription(self, *args: Any, **kwargs: Any) -> None:
        pass

    subscribe_bars = unsubscribe_bars = _subscription
    subscribe_quotes = unsubscribe_quotes = _subscription
    subscribe_trades = unsubscribe_trades = _subscription

    def is_exiting(self) -> bool:
        return False

    def _trading_account(self, venue: Any) -> SimpleNamespace:
        return SimpleNamespace(calculate_account_state=True)

    def _new(self, kind: str, **fields: Any) -> SimpleNamespace:
        return super()._new(
            kind,
            avg_px=None,
            filled_qty=Quantity.from_int(0),
            instrument_id=AAA,
            side=OrderSide.SELL,
            ts_init=self.now,
            **fields,
        )

    def cancel_order(self, client_order_id: Any) -> None:
        super().cancel_order(client_order_id)
        order = self._order(client_order_id)
        assert order is not None
        order.is_closed, order.is_open, order.is_emulated = True, False, False
        self.on_order_canceled(event(client_order_id, self.now))

    def submit_order(self, order: Any, params: Any = None) -> None:
        self.orders[order.client_order_id.value] = order
        self.submitted.append(order)

    def step(self, at: int) -> None:
        self.now = at
        self._on_step(SimpleNamespace(ts_event=at))

    def fill(self, order: Any, qty: int, px: str, at: int) -> None:
        """Publish an explicit venue fill, after its cached order/position update."""
        before = order.filled_qty.as_decimal()
        notional = before * D(str(order.avg_px or 0)) + D(qty) * D(px)
        order.filled_qty = Quantity.from_int(int(before) + qty)
        order.avg_px = notional / (before + qty)
        if order.filled_qty == order.quantity:
            order.is_closed, order.is_open, order.is_emulated = True, False, False
        held = sum((p.signed_qty for p in self.positions), D(0))
        held += D(qty) if order.side == OrderSide.BUY else -D(qty)
        self.positions = [SimpleNamespace(signed_qty=held, avg_px_open=D("10"))] if held else []
        self.on_order_filled(
            event(
                order.client_order_id,
                at,
                order_side=order.side,
                last_qty=Quantity.from_int(qty),
                last_px=Price.from_str(px),
            )
        )


def entry(host: LateFillSeam) -> tuple[Any, Any]:
    host._open_slot(AAA)
    slot = host.slots[AAA]
    order = SimpleNamespace(
        client_order_id=ClientOrderId("BUY-LATE"),
        instrument_id=AAA,
        side=OrderSide.BUY,
        quantity=Quantity.from_int(5),
        filled_qty=Quantity.from_int(0),
        price=Price.from_str("10.0000"),
        avg_px=None,
        is_closed=False,
        is_open=True,
        is_emulated=False,
        is_pending_cancel=False,
        is_pending_update=False,
        ts_init=T0,
        status=None,
    )
    host.orders[order.client_order_id.value] = order
    slot.entry_id = order.client_order_id
    slot.entry_record = {
        "client_order_id": order.client_order_id.value,
        "instrument_id": str(AAA),
        "entry_price": None,
        "stopped_out": False,
    }
    host.entries_sent.append(slot.entry_record)
    return slot, order


def orphan_buy(cause: str) -> LateFillSeam:
    host = LateFillSeam()
    _, buy = entry(host)
    host.cancel_order(buy.client_order_id)
    host.rank, host.rank_ns = [], T0
    if cause == "wind_down":
        host._wind_down(SimpleNamespace(ts_event=T0 + SECOND))
    host.step(T0 + SECOND)
    assert AAA not in host.slots, "precondition: the canceled entry's slot really closed"
    host.fill(buy, 5, "10.0000", T0 + 2 * SECOND)
    host.step(T0 + 3 * SECOND)
    return host


@pytest.mark.parametrize("cause", ["unranked", "wind_down"])
def test_p1_orphan_buy_gets_protection_or_a_sale(cause: str) -> None:
    host = orphan_buy(cause)
    slot = host.slots.get(AAA)
    protected = (
        slot is not None
        and slot.exit_record is not None
        and (
            slot.held_stop is not None
            or any(o.is_emulated and o.quantity.as_decimal() >= 5 for o in host.submitted)
        )
    )
    selling = any(o.is_open and o.quantity.as_decimal() >= 5 for o in host.submitted)
    assert protected or selling, "five late-bought shares have neither protection nor a sale"


@pytest.mark.parametrize("cause", ["unranked", "wind_down"])
def test_p1_window_close_includes_the_orphan_buy(cause: str) -> None:
    host = orphan_buy(cause)
    host._wind_down(SimpleNamespace(ts_event=T0 + 10 * SECOND))
    # Give cancellation and its replacement separate steps, and explicitly fill
    # each marketable sale. No synthetic fill is created unless Mover sent it.
    for second in (11, 17, 23):
        host.step(T0 + second * SECOND)
        for sale in list(host.submitted):
            if sale.is_open:
                host.fill(sale, int(sale.quantity.as_decimal()), "10.5000", host.now)
    held = sum((p.signed_qty for p in host.positions), D(0))
    assert held == 0, "wind-down omitted the symbol whose canceled entry filled late"
    assert sum(D(f[2]) for f in host.fills if f[1] == "SELL") == D(5)


def sale_legs(arm: str = "clock") -> tuple[Any, Any, Any, Any]:
    host = LateFillSeam(reentry_arm=arm)
    slot, buy = entry(host)
    host.fill(buy, 5, "10.0000", T0)
    slot.was_held = True
    host._protect(slot, D(5), T0)
    (old,) = host.submitted
    host.now = T0 + SECOND
    host.cancel_order(old.client_order_id)
    host._protect(slot, D(5), T0 + SECOND)
    old, replacement = host.submitted
    return host, slot, old, replacement


def late_sale(arm: str, *, clear_before_fill: bool = False) -> tuple[Any, Any]:
    host, slot, old, replacement = sale_legs(arm)
    host.cancel_order(replacement.client_order_id)
    if clear_before_fill:
        # A flat cache report can precede the delayed order callbacks. Exercise
        # the same cleared-record seam as test_mover_trail's late-sale cases.
        host.positions = []
        host._flat(slot, T0 + SECOND)
        host._close_slot(slot)
        host.on_order_filled(
            event(
                old.client_order_id,
                T0 + 2 * SECOND,
                order_side=OrderSide.SELL,
                last_qty=Quantity.from_int(2),
                last_px=Price.from_str("10.0000"),
            )
        )
        host.on_order_filled(
            event(
                old.client_order_id,
                T0 + 3 * SECOND,
                order_side=OrderSide.SELL,
                last_qty=Quantity.from_int(3),
                last_px=Price.from_str("10.5000"),
            )
        )
    else:
        host.fill(old, 2, "10.0000", T0 + 2 * SECOND)
        host.fill(old, 3, "10.5000", T0 + 3 * SECOND)
        host._flat(slot, T0 + 4 * SECOND)
    return host, slot


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("clear_before_fill", [False, True], ids=["slot_present", "slot_closed"])
def test_p2_late_sale_keeps_its_weighted_price(arm: str, clear_before_fill: bool) -> None:
    host, _ = late_sale(arm, clear_before_fill=clear_before_fill)
    # (2 * 10.00 + 3 * 10.50) / 5 = 10.30; the unfilled replacement has no average.
    assert host.sold[AAA][1] == D("10.30"), "the late sale's VWAP was lost during flat cleanup"
    assert host.reentry_states[AAA].sale_price == D("10.30")


@pytest.mark.parametrize(
    "arm",
    [
        pytest.param(
            arm,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "L5's judgment against main's rules: the turn and vwap arms re-enter on a turn "
                    "from the low or a VWAP cross after the sale, with no above-sale price rule; "
                    "the brief's every-arm wording was the deputy's, not main's"
                ),
            ),
        )
        if arm in ("turn", "vwap")
        else arm
        for arm in ARMS
    ],
)
@pytest.mark.parametrize("close", ["10.29", "10.30"], ids=["below", "equal"])
def test_p2_late_sale_refuses_a_run_at_or_below_its_price(arm: str, close: str) -> None:
    host, slot = late_sale(arm)
    now = T0 + MINUTE
    host.rank, host.rank_ns = [(AAA, D("0.25"))], now
    slot.run_seen_ns = now
    slot.run_closed_ns = now  # the run's interval ended after the sale (`bar_closed_ns`)
    # Make each state arm otherwise eligible, so an unrelated missing turn or
    # VWAP observation cannot conceal loss of the sale-price boundary. The base
    # deliberately admits these arms below a sale; REPORT.md names that conflict
    # with the task's literal P2 rather than treating it as a late-fill-only bug.
    state = host.reentry_states[AAA]
    if arm == "turn":
        state.low, state.previous, state.rises = D("10.00"), D("10.20"), 2
    elif arm == "vwap":
        state.vwap, state.crossed_vwap = D("10.10"), True
    why = host._refusal(slot, D(close), {AAA: (0, D("0.25"))}, 0, now)
    assert why is not None and "not above the sale" in why, (
        f"a {close} run must not re-enter after selling at 10.30: {why!r}"
    )


def triggered_legs() -> tuple[Any, Any, Any, Any]:
    host, slot, old, replacement = sale_legs()
    host.on_quote(
        SimpleNamespace(
            instrument_id=AAA, bid_price=Price.from_str("11.0000"), ts_event=T0, ts_init=T0
        )
    )
    host.on_order_released(
        event(replacement.client_order_id, T0 + SECOND, released_price=Price.from_str("10.0000"))
    )
    return host, slot, old, replacement


@pytest.mark.parametrize("gone", ["current", "exit_cleared", "slot_closed"])
@pytest.mark.parametrize("field", ["quantity", "final_time", "slippage", "giveback"])
def test_p3_earlier_canceled_leg_cannot_recount_a_completed_exit(gone: str, field: str) -> None:
    host, slot, old, replacement = triggered_legs()
    row = slot.exit_record
    host.fill(replacement, 5, "9.8000", T0 + 5 * SECOND)
    before = row.record()
    assert D(before["filled_quantity"]) == D(5)
    assert D(before["slip_dollars"]) == D("1.00")
    assert D(before["peak_giveback_dollars"]) == D("6.00")
    if gone != "current":
        host._flat(slot, T0 + 6 * SECOND)
        if gone == "slot_closed":
            host._close_slot(slot)
    # The earlier canceled order's own row must still receive this report. It
    # must not sell the same five-share holding a second time in its exit totals.
    host.on_order_filled(
        event(
            old.client_order_id,
            T0 + 2 * SECOND,
            order_side=OrderSide.SELL,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("9.8000"),
        )
    )
    after = row.record()
    if field == "quantity":
        assert D(after["filled_quantity"]) <= D(5), after
        assert D(after["quantity"]) == D(5)
    elif field == "final_time":
        assert after["final_fill_ns"] == T0 + 5 * SECOND, after
    elif field == "slippage":
        assert D(after["slip_dollars"]) == D("1.00"), after
    else:
        assert D(after["peak_giveback_dollars"]) == D("6.00"), after
    assert D(host.sale_rows[old.client_order_id.value]["filled_quantity"]) == D(5)


def test_p3_out_of_order_partial_fills_keep_the_latest_execution_time() -> None:
    host, slot, old, replacement = triggered_legs()
    host.fill(replacement, 3, "9.8000", T0 + 5 * SECOND)
    host.fill(old, 2, "9.9000", T0 + 2 * SECOND)
    row = slot.exit_record.record()
    assert D(row["filled_quantity"]) == D(5)
    assert D(row["fill_vwap"]) == D("9.84")
    assert D(row["slip_dollars"]) == D("0.80")
    assert D(row["peak_giveback_dollars"]) == D("5.80")
    assert row["final_fill_ns"] == T0 + 5 * SECOND
    assert row["trigger_to_final_fill_ms"] == 4000


def test_control_buy_before_slot_cleanup_is_protected_and_closed() -> None:
    """Driver control: this ordinary order of callbacks already holds at the base."""
    host = LateFillSeam()
    slot, buy = entry(host)
    host.cancel_order(buy.client_order_id)
    host.fill(buy, 5, "10.0000", T0 + SECOND)
    host.rank, host.rank_ns = [], T0
    host.step(T0 + 2 * SECOND)
    assert slot.exit_record is not None
    assert slot.held_stop is not None or any(o.is_emulated for o in host.submitted)
    host._wind_down(SimpleNamespace(ts_event=T0 + 3 * SECOND))
    host.step(T0 + 4 * SECOND)
    host.step(T0 + 10 * SECOND)
    sales = [o for o in host.submitted if o.is_open]
    assert len(sales) == 1 and sales[0].quantity == Quantity.from_int(5)
    host.fill(sales[0], 5, "10.5000", T0 + 11 * SECOND)
    assert host.positions == []


@pytest.mark.parametrize("lifecycle", ["kept", "closed", "reopened"])
def test_recovered_late_buy_keeps_receipt_for_unpriced_hold(lifecycle: str) -> None:
    # A late buy's hold is counted from this node's receipt of its fill, whether its slot was kept,
    # closed, or reopened by the ranking before the fill (r-pair-codex, P1).
    host = LateFillSeam()
    original, buy = entry(host)
    host.cancel_order(buy.client_order_id)
    if lifecycle != "kept":
        host._close_slot(original)
        if lifecycle == "reopened":
            host._open_slot(AAA)

    venue_fill = T0 + 2 * SECOND
    received = venue_fill + 120 * SECOND
    buy.avg_px = Decimal(10)
    buy.filled_qty = Quantity.from_int(5)
    buy.is_closed, buy.is_open, buy.is_emulated = True, False, False
    host.positions = [SimpleNamespace(signed_qty=Decimal(5), avg_px_open=Decimal(10))]
    host.cache.quote = lambda _: None
    host.now = received
    host.on_order_filled(
        event(
            buy.client_order_id,
            venue_fill,
            ts_init=received,
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str("10.0000"),
        )
    )

    host.now = received + SECOND
    host._reopen_late_buys(host.now)
    slot = host.slots[AAA]
    host._protect(slot, Decimal(5), host.now)

    assert host.submitted == [], "one second since receipt must not send an unpriced exit"
    assert slot.entry_received_ns == received
    assert slot.held_since == received
    assert host.fills[-1][-1] == venue_fill

    host.now = received + mover.HOLD_MOST_SECS * SECOND
    host._protect(slot, Decimal(5), host.now)
    assert len(host.submitted) == 1
    assert host.submitted[0].quantity == Quantity.from_int(5)
    assert slot.exit_record.trigger_reason == "unpriced_exit"
