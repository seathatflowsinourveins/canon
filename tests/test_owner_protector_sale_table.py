"""The plain-limit table must admit the lifecycle emitted by the pinned engine.

NautilusTrader d53849b216e2: trading/src/strategy/mod.rs:818-865 publishes
PendingCancel; :2448-2452 publishes Initialized; :1445-1465 dispatches both.
The synthetic clients below follow test_owner_protector's real-core harness.
No broker, credential, paper state, catalog or ledger is read or written.
"""

from __future__ import annotations

import asyncio
import gc
import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Self

import pytest
from nautilus_trader import model
from nautilus_trader.common import TimeEvent
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.model import OrderPendingCancel
from nautilus_trader.model import OrderUpdated

from canon_trading import sale_table
from canon_trading.owner_protector import FLIGHT_SECS
from canon_trading.owner_protector import OUTSIDE_CANCEL_WAIT_SECS
from canon_trading.owner_protector import REFUSAL_RETRY_MOST_SECS
from canon_trading.owner_protector import Sale
from canon_trading.price_step import exit_price
from canon_trading.start_level import LevelBook
from tests import test_owner_protector as h


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the credential-environment fixture for this offline test."""


@pytest.fixture(autouse=True)
def isolated_level_books(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


def check_engine_log(text):
    """Pin python/strategy.rs:1138-1235; logger.rs:1136-1148 syncs the writer first."""
    errors = [
        line
        for line in text.splitlines()
        if re.search(r"Python \w+ failed|Already mutably borrowed", line)
    ]
    if errors:
        raise AssertionError("engine handler errors:\n" + "\n".join(errors))


def assert_sale_below_level(strategy, order):
    if strategy.settings.breach_count > 1:
        slot = (
            strategy.slots.get(order.instrument_id) or strategy.stopped_sales[order.instrument_id]
        )
        assert order.price.as_decimal() <= slot.level.as_decimal(), (
            "sale limit exceeds its holding's level"
        )
        prices = getattr(strategy, "checked_episode_prices", {})
        episode, previous = prices.get(order.instrument_id, (None, None))
        if episode is not slot.sale:
            previous = None
        if previous is not None:
            assert order.price.as_decimal() <= previous, "sale limit rose within its episode"
            if slot.sale.floor is not None:
                assert order.price.as_decimal() >= slot.sale.floor, "sale limit under its floor"
        floor = (
            slot.sale.floor
            if previous is not None
            else None
            if slot.gain_applied_floor is None
            else slot.gain_applied_floor.as_decimal()
        )
        expected = sale_table.leg_limit(
            None
            if slot.usable_quote is None
            else exit_price(
                slot.usable_quote.bid_price.as_decimal(), h.protect.PLANNER.exit_ticks, None
            ),
            level=slot.level.as_decimal(),
            previous=previous,
            floor=floor,
        )
        assert order.price.as_decimal() == expected, "sale limit differs from the one price rule"
        prices[order.instrument_id] = slot.sale, order.price.as_decimal()
        strategy.checked_episode_prices = prices


@pytest.fixture(autouse=True)
def engine_checks(capfd, monkeypatch):
    """Shared by every engine case here and in the refusal file, including native histories."""
    native = h.OwnerProtector.submit_order
    harness = h.Harness.submit_order

    def submit(strategy, order, *args, **kwargs):
        assert_sale_below_level(strategy, order)
        return native(strategy, order, *args, **kwargs)

    def command(strategy, order, *args, **kwargs):
        assert_sale_below_level(strategy, order)
        return harness(strategy, order, *args, **kwargs)

    monkeypatch.setattr(h.OwnerProtector, "submit_order", submit)
    monkeypatch.setattr(h.Harness, "submit_order", command)

    def check():
        logging_sync_to_disk()
        captured = capfd.readouterr()
        check_engine_log(captured.out + captured.err)

    yield check
    check()


def plain_limit_callbacks() -> list[str]:
    """Read actual strategy callbacks for a plain sale and its native cancel.

    This probe deliberately does not implement the proposed glue: it establishes
    the engine input that the frozen table and its catch-all must accommodate.
    """
    gc.collect()

    class Probe(h.OwnerProtector):
        def __new__(cls) -> Self:
            return super().__new__(cls, h.OwnerProtectorConfig(strategy_id=h.PROTECTOR, row="OWN1"))  # ty: ignore[too-many-positional-arguments]

        def __init__(self) -> None:
            super().__init__(h.OwnerProtectorConfig(strategy_id=h.PROTECTOR, row="OWN1"))
            self.leg: Any = None
            self.callbacks: list[str] = []
            self.cancel_sent = False

        def on_start(self) -> None:
            self.subscribe_quotes(h.AAA)

        def on_quote(self, quote) -> None:
            if self.leg is None:
                if not self.cache.positions_open(instrument_id=h.AAA, strategy_id=h.EXTERNAL):
                    return
                self.leg = self.order_factory.limit(
                    h.AAA,
                    h.OrderSide.SELL,
                    h.Quantity.from_int(10),
                    h.Price.from_str("9.90"),
                    time_in_force=h.TimeInForce.DAY,
                    reduce_only=True,
                    client_order_id=self._protect_id(),
                )
                self.submit_order(self.leg, params={"extended_hours": True})
                return
            current = self.cache.order(self.leg.client_order_id)
            assert current is not None
            if current.status == h.OrderStatus.ACCEPTED and not self.cancel_sent:
                self.cancel_sent = True
                self.cancel_order(current.client_order_id)

        def on_order_event(self, event) -> None:
            self.callbacks.append(type(event).__name__)
            if type(event).__name__ == "OrderCanceled":
                self.shutdown_system("synthetic lifecycle observed")

        def on_stop(self) -> None:
            pass

    class Execution(h.SyntheticExecution):
        async def _submit_order(self, command) -> None:
            order = command.order
            if order.side == h.OrderSide.BUY:
                await super()._submit_order(command)
                return
            assert order.order_type == h.OrderType.LIMIT
            assert order.time_in_force == h.TimeInForce.DAY and order.is_reduce_only
            assert command.params == {"extended_hours": True}
            self.generate_order_submitted(order)
            self.generate_order_accepted(
                order, h.VenueOrderId(str(order.client_order_id)), self.clock.timestamp_ns()
            )

        async def _cancel_order(self, command) -> None:
            order = self.cache.order(command.client_order_id)
            assert order is not None
            self.generate_order_canceled(order, order.venue_order_id, self.clock.timestamp_ns())

    class DataFactory(h.DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return h.SyntheticData(**kwargs, venue=h.Venue("ALPACA"))

    class ExecutionFactory(h.ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Execution(
                **kwargs,
                venue=h.Venue("ALPACA"),
                account_id=h.AccountId("ALPACA-001"),
                account_type=h.AccountType.CASH,
                oms_type=h.OmsType.NETTING,
            )

    node = h.LiveNode.build(
        "SALE-TABLE-LIFECYCLE-TEST",
        h.LiveNodeConfig(
            environment=h.Environment.SANDBOX,
            trader_id=h.TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": h.DataClientConfig()},
            exec_clients={"ALPACA": h.ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    probe = Probe()
    node.add_strategy(h.Owner())
    node.add_strategy(probe)

    async def run() -> None:
        async with asyncio.timeout(5):
            await node.run_async()

    try:
        asyncio.run(run())
        assert probe.callbacks == [
            "OrderInitialized",
            "OrderSubmitted",
            "OrderAccepted",
            "OrderPendingCancel",
            "OrderCanceled",
        ]
        return probe.callbacks
    finally:
        node.dispose()


@pytest.mark.parametrize(
    "native_event,row",
    [("OrderInitialized", "FL"), ("OrderPendingCancel", "PCR")],
)
def test_plain_limit_local_lifecycle_must_not_freeze_the_sale(native_event, row) -> None:
    """The regenerated inventory maps the two engine echoes to their own columns."""
    assert native_event in plain_limit_callbacks()
    state = sale_table.State(row)
    result = sale_table.transition(state, 2, sale_table.order_event_column(native_event), {})
    assert (result.state, result.actions, result.named) == (state, (), None)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2", None])
def test_count_is_validated_by_name(value):
    with pytest.raises((TypeError, ValueError), match="breach_count"):
        h.OwnerProtectorConfig(row="OWN1", breach_count=value)


@pytest.mark.parametrize("value", [-1, True, 0.5, "0", None])
def test_gap_is_validated_by_name(value):
    with pytest.raises((TypeError, ValueError), match="confirm_gap_ms"):
        h.OwnerProtectorConfig(row="OWN1", breach_count=2, confirm_gap_ms=value)


def test_count_one_default_and_unknown_span():
    assert h.OwnerProtectorConfig(row="OWN1").breach_count == 1
    with pytest.raises((TypeError, ValueError), match="confirm_gap_ms"):
        h.OwnerProtectorConfig(row="OWN1", confirm_gap_ms=0)
    with pytest.raises((TypeError, ValueError), match="confirm_span_ms"):
        h.OwnerProtectorConfig(row="OWN1", breach_count=2, confirm_span_ms=1000)


def test_count_too_large_for_level_book_is_refused(tmp_path):
    with pytest.raises((TypeError, ValueError), match="breach_count.*level book"):
        h.OwnerProtectorConfig(
            row="OWN1",
            breach_count=3,
            confirm_gap_ms=0,
            account_label="example",
            level_book_path=tmp_path / "book",
        )
    assert h.OwnerProtectorConfig(row="OWN1", breach_count=3, confirm_gap_ms=0).breach_count == 3


def event(kind, order, **values):
    # Native payloads let the driver replay the pin's own status machine at a step.
    # The table's impossible kinds still use a bare payload when no native shape is needed.
    if kind == "Initialized" and order.events():
        return order.events()[0]
    if kind in {
        "Submitted",
        "Accepted",
        "Rejected",
        "Denied",
        "Canceled",
        "Expired",
        "PendingCancel",
        "CancelRejected",
        "Updated",
        "Filled",
    }:
        at = values.pop("ts_event", h.T0)
        reason = values.pop("reason", "synthetic refusal")
        kwargs = {
            "trader_id": h.TraderId("TESTER-001"),
            "strategy_id": h.PROTECTOR,
            "instrument_id": h.AAA,
            "client_order_id": order.client_order_id,
            "event_id": h.UUID4(),
            "ts_event": at,
            "ts_init": values.pop("ts_init", at),
        }
        if kind != "Denied":
            kwargs["account_id"] = h.AccountId("ALPACA-001")
        if kind not in {"Denied", "Submitted"}:
            kwargs["reconciliation"] = kind == "Updated"
        if kind in {
            "Accepted",
            "Canceled",
            "Expired",
            "PendingCancel",
            "CancelRejected",
            "Updated",
            "Filled",
        }:
            kwargs["venue_order_id"] = h.VenueOrderId(str(order.client_order_id))
        if kind in {"Rejected", "Denied", "Canceled", "CancelRejected"}:
            kwargs["reason"] = reason
        if kind == "Updated":
            kwargs.update(quantity=order.quantity, price=order.price)
        if kind == "Filled":
            kwargs.update(
                trade_id=h.TradeId(str(h.UUID4())),
                order_side=h.OrderSide.SELL,
                order_type=h.OrderType.LIMIT,
                currency=h.USD,
                liquidity_side=h.LiquiditySide.TAKER,
                last_qty=h.Quantity.from_int(1),
                last_px=order.price,
            )
        kwargs.update(values)
        return getattr(model, "Order" + kind)(**kwargs)
    item: Any = type("Order" + kind, (), {})()
    item.instrument_id = h.AAA
    item.client_order_id = order.client_order_id
    item.strategy_id = h.PROTECTOR
    item.ts_event = h.T0
    item.reason = "synthetic refusal"
    item.__dict__.update(values)
    return item


class Counted(h.Harness):
    """The base's stand-in; local echoes arrive synchronously inside the action."""

    def __init__(self, **settings):
        super().__init__(**settings)
        self.effects = []

    def submit_order(self, order, position_id=None, client_id=None, params=None):
        self.effects.append(sale_table.Action.SEND)
        super().submit_order(order, position_id, client_id, params)
        initialized = event("Initialized", order)
        leg = h.working_sell(
            str(order.client_order_id),
            qty=str(order.quantity),
            filled="0",
            strategy_id=self.strategy_id,
            price=order.price,
            status=h.OrderStatus.INITIALIZED,
            is_open=False,
            is_pending_cancel=False,
            is_pending_update=False,
            is_emulated=False,
            events=lambda: [initialized],
        )
        self.cached.working[-1] = leg
        self.on_order_event(initialized)

    def cancel_order(self, client_order_id, client_id=None, params=None):
        self.effects.append(sale_table.Action.CANCEL)
        super().cancel_order(client_order_id, client_id, params)
        self.deliver("PendingCancel", leg=self.cache.order(client_order_id))

    def deliver(self, kind, *, quantity=0, leg=None, **values):
        leg = leg or self.cached.working[-1]
        values.setdefault("ts_event", self.clock.timestamp_ns())
        if kind == "Filled":
            values.update(last_qty=h.Quantity.from_str(str(quantity)), last_px=leg.price)
        item = event(kind, leg, **values)
        history = leg.events()
        if kind == "Filled":
            leg.filled_qty = h.Quantity.from_str(str(leg.filled_qty.as_decimal() + quantity))
            leg.leaves_qty = h.Quantity.from_str(
                str(leg.quantity.as_decimal() - leg.filled_qty.as_decimal())
            )
            self.cached.held[0].signed_qty -= Decimal(quantity)
            self.cached.held[0].quantity = h.Quantity.from_str(
                str(max(0, self.cached.held[0].signed_qty))
            )
            status = "FILLED" if leg.leaves_qty.as_decimal() == 0 else "PARTIALLY_FILLED"
            if (
                leg.status in (h.OrderStatus.CANCELED, h.OrderStatus.EXPIRED)
                and leg.leaves_qty.as_decimal() > 0
            ):
                status = leg.status.name
        else:
            status = {
                "Submitted": "SUBMITTED",
                "Accepted": "ACCEPTED",
                "Rejected": "REJECTED",
                "Denied": "DENIED",
                "Canceled": "CANCELED",
                "Expired": "EXPIRED",
                "PendingCancel": "PENDING_CANCEL",
                "CancelRejected": "ACCEPTED",
            }.get(kind, leg.status.name)
        leg.status = getattr(h.OrderStatus, status)
        leg.is_closed = status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "DENIED"}
        leg.is_pending_cancel = status == "PENDING_CANCEL"
        leg.is_open = status in {"ACCEPTED", "PARTIALLY_FILLED", "SUBMITTED", "PENDING_CANCEL"}
        leg.events = lambda: [*history, item]
        self.on_order_event(item)
        return item

    def deliver_then_step(self, kind, **values):
        item = self.deliver(kind, **values)
        self._sale_step(h.AAA, ["NQ"])
        flush_events(self)
        return item


def flush_events(strategy):
    """Each queued arrival, including an action's echo, needs a subsequent timer step."""
    for _ in range(4):
        if not strategy._sale_events.get(h.AAA):
            return
        strategy._sale_step(h.AAA, ["NQ"])
    assert not strategy._sale_events.get(h.AAA), "event batches did not settle"


def counted(**settings):
    settings.setdefault("confirm_gap_ms", 0)
    strategy = Counted(breach_count=2, **settings)
    strategy.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    return strategy


def row(strategy):
    slot = strategy.slots.get(h.AAA) or strategy.stopped_sales.get(h.AAA)
    return sale_table.State("T0") if slot is None else slot.sale_state


def first_leg(strategy):
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert len(strategy.sent) == 1
    strategy.deliver_then_step("Submitted")
    strategy.deliver_then_step("Accepted")
    strategy._sale_step(h.AAA, ["NQ"])


def test_counted_quotes_send_one_plain_leg_and_echo_sees_new_row():
    strategy = counted()
    assert row(strategy) == sale_table.State("G", 0)
    strategy.observe("9.89", h.T0 + h.SECOND)
    assert row(strategy) == sale_table.State("G", 1) and not strategy.sent
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert row(strategy) == sale_table.State("FL")
    order = strategy.sent[0]
    assert (
        order.order_type == h.OrderType.LIMIT
        and order.emulation_trigger == h.TriggerType.NO_TRIGGER
    )
    assert order.is_reduce_only and order.time_in_force == h.TimeInForce.DAY
    assert str(order.client_order_id).startswith("canon-protect-")
    assert strategy.params == [{"extended_hours": True}]
    assert not any(line.startswith("ALERT") for line in strategy.said)


def test_reprice_only_on_counted_under_quote_and_ack_sends_remainder():
    strategy = counted()
    first_leg(strategy)
    strategy.deliver_then_step("Filled", quantity=3)
    strategy.test_clock.set_time(h.T0 + 60 * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))
    assert not strategy.canceled
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    assert row(strategy).row == "PCR" and len(strategy.canceled) == 1
    strategy.observe("9.79", h.T0 + 62 * h.SECOND)
    strategy.deliver_then_step("Canceled")
    assert row(strategy).row == "FL" and len(strategy.sent) == 2
    assert strategy.sent[-1].quantity.as_decimal() == 7
    assert strategy.sent[-1].price.as_decimal() <= Decimal("9.79")
    assert not any(line.startswith("ALERT") for line in strategy.said)


def test_seed_quote_never_counts_reprices_or_prices_next_leg():
    strategy = counted()
    strategy.on_historical_quotes([h.quote("9.89", h.T0 + h.SECOND)])
    assert row(strategy).count == 0 and not strategy.sent
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    strategy.observe("9.87", h.T0 + 3 * h.SECOND)
    strategy.deliver_then_step("Accepted")
    strategy.on_historical_quotes([h.quote("9.70", h.T0 + 60 * h.SECOND)])
    assert not strategy.canceled
    strategy.observe("9.80", h.T0 + 61 * h.SECOND)
    strategy.on_historical_quotes([h.quote("9.50", h.T0 + 62 * h.SECOND)])
    newest = strategy.slots[h.AAA].counted_quote
    strategy.deliver_then_step("Canceled")
    assert newest.bid_price.as_decimal() == Decimal("9.80")
    assert strategy.sent[-1].price.as_decimal() > Decimal("9.50")


def test_gap_and_bad_quotes_do_not_count_clear_or_raise():
    strategy = counted(confirm_gap_ms=1000)
    strategy.observe("9.89", h.T0 + h.SECOND)
    before = row(strategy), strategy.slots[h.AAA].level
    strategy.observe("9.88", h.T0 + h.SECOND + h.SECOND // 2)
    strategy.observe("10.50", h.T0 + h.SECOND)
    bad = h.QuoteTick(
        h.AAA,
        h.Price.from_str("11.00"),
        h.Price.from_str("10.00"),
        h.Quantity.from_int(1),
        h.Quantity.from_int(1),
        h.T0 + 2 * h.SECOND,
        h.T0 + 2 * h.SECOND,
    )
    strategy.on_quote(bad)
    assert (row(strategy), strategy.slots[h.AAA].level) == before
    assert not strategy.sent
    assert any("gap" in text for text in strategy.said)


def test_above_level_raises_without_order_and_clears_count():
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("12.00", h.T0 + 2 * h.SECOND)
    assert row(strategy).count == 0 and not strategy.sent
    assert strategy.slots[h.AAA].level.as_decimal() > Decimal("9.90")


@pytest.mark.parametrize("target", sale_table.TABLE.rows)
def test_timer_is_inert_in_every_row(target):
    strategy = counted()
    if target in {"FC", "FCW", "FCP"}:
        strategy.cached.working.append(h.working_sell("foreign", qty="1", filled="0"))
    strategy.slots[h.AAA].sale_state = sale_table.State(target, 1 if target == "G" else None)
    before = row(strategy)
    strategy._sale_step(h.AAA, ["NQ"])
    assert row(strategy) == before and not strategy.effects


@pytest.mark.parametrize(
    "error",
    [
        sale_table.NoMatchingBranch,
        sale_table.AmbiguousBranch,
        sale_table.MissingPredicate,
        sale_table.InvalidFacts,
        sale_table.UnknownEvent,
    ],
)
def test_module_failure_freezes_and_names_cell_without_send(error, monkeypatch):
    strategy = counted()

    def broken(*args, **kwargs):
        raise error("synthetic contract failure")

    monkeypatch.setattr(sale_table, "transition", broken)
    strategy.observe("9.89", h.T0 + h.SECOND)
    assert h.AAA in strategy.frozen and not strategy.effects
    assert any(text.startswith("ALERT") and "G x QU" in text for text in strategy.said)


def test_stop_keeps_flight_state_until_acceptance_and_blocks_readoption():
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    strategy.stop_instrument(h.AAA)
    assert not strategy.canceled
    strategy.adopt_holding(h.AAA, h.Price.from_str("10.00"))
    strategy.adopt_holding(h.AAA, h.Price.from_str("10.00"))
    assert sum("previous sale" in text for text in strategy.said) == 1
    strategy.deliver_then_step("Accepted")
    assert len(strategy.canceled) == 1 and len(strategy.sent) == 1
    strategy.deliver_then_step("Canceled")
    assert h.AAA not in strategy.stopped_sales


@pytest.mark.parametrize("target", sale_table.TABLE.rows)
def test_every_row_round_trips_real_level_book(target, tmp_path):
    LevelBook.write(tmp_path / "book", {})
    strategy = counted(account_label="example", level_book_path=tmp_path / "book")
    strategy.level_book_path = tmp_path / "book"
    strategy.run = "example-run"
    slot = strategy.slots[h.AAA]
    slot.sale_state = sale_table.State(target, 1 if target == "G" else None)
    slot.ended = target == "X"
    written = strategy._current_levels()
    strategy._save_levels()
    notes = []
    read = LevelBook.read(tmp_path / "book", report=notes.append)
    assert not notes
    assert read == written
    if target != "X":
        assert read["example", h.AAA].last_level == slot.level.as_decimal()
        entry = read["example", h.AAA]
        if target == "G":
            assert (entry.state, entry.count) == ("counting", 1)
        elif target == "RW":
            assert (entry.state, entry.count) == ("counting", 0)
        elif target in {"FL", "WK", "PF"}:
            assert entry.state == "armed"
        elif target in {"FZ", "FZW", "FZP", "FC", "FCW", "FCP"}:
            assert entry.state == "frozen" and entry.reason
        else:
            assert entry.state in {"waiting", "frozen"} and entry.reason


@pytest.mark.parametrize("target", ["G", "GC", "WK", "PCR", "FZ", "RW", "FC", "FCW", "FCP"])
def test_real_start_keeps_saved_level_and_resets_count(target, tmp_path):
    path = tmp_path / "book"
    previous = counted(account_label="example", level_book_path=path)
    previous.level_book_path, previous.run = path, "previous"
    slot = previous.slots[h.AAA]
    slot.level = h.Price.from_str("12.00")
    slot.sale_state = sale_table.State(target, 1 if target == "G" else None)
    previous._save_levels()
    saved = LevelBook.read(path)["example", h.AAA]
    assert (
        saved.state
        == {
            "G": "counting",
            "GC": "waiting",
            "WK": "armed",
            "PCR": "waiting",
            "FZ": "frozen",
            "RW": "counting",
            "FC": "frozen",
            "FCW": "frozen",
            "FCP": "frozen",
        }[target]
    )
    assert saved.count == {"G": 1, "RW": 0}.get(target)
    strategy = Counted(
        breach_count=2,
        confirm_gap_ms=0,
        account_label="example",
        level_book_path=path,
        trading=h.VenueOrders(),
    )
    strategy.test_clock.set_time(h.T0 + 180 * h.SECOND)
    strategy.cached.latest = h.quote("10.00", h.T0 + 180 * h.SECOND)
    strategy.on_start()
    level = strategy.slots[h.AAA].level
    assert level is not None and level.as_decimal() >= Decimal("12.00")
    assert row(strategy).count == 0
    assert not any("previous level unavailable" in text for text in strategy.said)
    assert any("count" in text and "zero" in text for text in strategy.said)
    read = LevelBook.read(path)
    level_read = read["example", h.AAA].last_level
    assert level_read is not None and level_read >= Decimal("12.00")
    strategy.on_stop()


HISTORIES = json.loads(Path(__file__).with_name("sale_table_histories.json").read_text())
CASES = [
    (group, name, history) for group, rows in HISTORIES.items() for name, history in rows.items()
]
TABLE_DATA = json.loads(Path(sale_table.__file__).with_suffix(".json").read_text())


def history_time(now, slot, when):
    """Supply the history's elapsed time, never a replacement predicate."""
    at = now + h.SECOND
    if when == "reprice_due":
        at += 60 * h.SECOND
    if "resend_due" in when and "not resend_due" not in when and slot.refusal_last_ns is not None:
        at = max(at, slot.refusal_last_ns + REFUSAL_RETRY_MOST_SECS * h.SECOND)
    if (
        "resend_due" in when
        and "not resend_due" not in when
        and getattr(slot, "external_cancel_ns", None) is not None
    ):
        at = max(at, slot.external_cancel_ns + OUTSIDE_CANCEL_WAIT_SECS * h.SECOND)
    if when == "flight_alert_due":
        at = max(at, slot.flight_sent_ns + FLIGHT_SECS * h.SECOND)
    return at


def history_observation(strategy, step):
    """Synthetic cache observations, never replacement transition logic."""
    code, when = step["event"], step["when"]
    if code in {"oINI", "oPCN"} and strategy._sale_events.get(h.AAA):
        flush_events(strategy)
        return
    slot = strategy.slots.get(h.AAA) or strategy.stopped_sales.get(h.AAA)
    assert slot is not None
    at = history_time(strategy.clock.timestamp_ns(), slot, when)
    strategy.test_clock.set_time(at)
    held = strategy.cached.held[0]
    if code in {"QU", "QA"}:
        bid = slot.level.as_decimal() + (Decimal(1) if code == "QA" else Decimal("-0.10"))
        if "not formable" in when:
            bid = Decimal("0.001")  # Positive quote, but the exit price cannot be formed.
        if when == "reprice_due":
            bid -= Decimal("0.10")
        strategy.observe(str(bid), at)
    elif code in {"HS", "HG", "HE"}:
        held.signed_qty = 0 if code == "HE" else held.signed_qty + (1 if code == "HG" else -1)
        held.quantity = h.Quantity.from_str(str(held.signed_qty))
        strategy.on_position_event(SimpleNamespace(instrument_id=h.AAA))
    elif code in {"CP", "CL"}:
        strategy.cached.working = [
            o for o in strategy.cached.working if o.client_order_id in strategy.sent_ids
        ]
        if code == "CP":
            # A predecessor is foreign to the table and summable by the unchanged _uncovered.
            quantity = (
                held.signed_qty
                if when == "whole_cover" or (when == "always" and step["row"] == "WK")
                else held.signed_qty - 1
            )
            strategy.cached.working.append(h.working_sell(qty=str(quantity), filled="0"))
        strategy._sale_step(h.AAA, [])
    elif code == "ST":
        strategy.stop_instrument(h.AAA)
    elif code == "FRZ":
        foreign = next(
            (o for o in strategy.cached.working if getattr(o, "history_cover", False)), None
        )
        if foreign is None:
            foreign = h.working_sell("unread-cover", qty="1", filled="0")
            strategy.cached.working.append(foreign)
        else:
            foreign.status = h.OrderStatus.PENDING_CANCEL
        strategy._sale_step(h.AAA, [])
    elif code == "UFZ":
        for order in strategy.cached.working:
            if getattr(order, "history_cover", False):
                order.status = h.OrderStatus.ACCEPTED
        strategy.cached.working = [
            o for o in strategy.cached.working if str(o.client_order_id) != "unread-cover"
        ]
        strategy._sale_step(h.AAA, [])
    elif code == "NQ":
        strategy._sale_step(h.AAA, ["NQ"])
    else:
        own = [o for o in strategy.cached.working if o.client_order_id in strategy.sent_ids]
        leg = own[-1]
        if code == "oLF":
            leg = next(o for o in own if o.is_closed)
        if code in {"oPF", "oFF", "oLF"}:
            quantity = leg.leaves_qty.as_decimal() if code == "oFF" else Decimal(1)
            if code == "oLF" and when == "not long_remains":
                quantity = held.signed_qty
            if "whole_cover" == when or when.endswith("and whole_cover"):
                foreign = sum(
                    (
                        o.leaves_qty.as_decimal()
                        for o in strategy.cached.working
                        if o.client_order_id not in strategy.sent_ids
                    ),
                    Decimal(0),
                )
                quantity = held.signed_qty - foreign
            strategy.deliver_then_step("Filled", quantity=quantity, leg=leg)
        else:
            kind = next(
                (
                    k
                    for k, spec in TABLE_DATA["order_event_inventory"]["kinds"].items()
                    if spec["columns"] == [code]
                ),
                "Triggered",
            )
            if code == "oUPD" and "ends_filled" in when and "not ends_filled" not in when:
                leg.quantity = leg.filled_qty
                leg.leaves_qty = h.Quantity.from_int(0)
                leg.status = h.OrderStatus.FILLED
            strategy.deliver_then_step(kind, leg=leg)


@pytest.mark.parametrize("group,name,history", CASES, ids=[name for _, name, _ in CASES])
def test_tracked_history_through_glue(group, name, history, monkeypatch):
    strategy = counted()
    if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
        strategy.cached.working.append(h.working_sell("unread-cover", qty="1", filled="0"))
        # This is a predecessor's summable sell until its cancel becomes pending.
        strategy.cached.working[-1].client_order_id = h.ClientOrderId(
            "canon-protect-20260101T000000Z-99-12345678"
        )
        strategy.cached.working[-1].history_cover = True
        strategy._sale_step(h.AAA, [])
    if name in {"expired-late-fill-covered", "refused-then-covered"}:
        strategy.cached.working.append(
            h.working_sell(qty="9" if name == "refused-then-covered" else "1", filled="0")
        )
        strategy._sale_step(h.AAA, [])
    stack = []
    transitions = []
    transition = sale_table.transition

    def traced(state, n, code, facts):
        result = transition(state, n, code, facts)
        transitions.append((state, code, result))
        return result

    monkeypatch.setattr(sale_table, "transition", traced)
    action = strategy._sale_action

    def authorized(*args):
        assert args[1].sale_state == transitions[-1][2].state
        stack.append(args[2])
        try:
            return action(*args)
        finally:
            stack.pop()

    send, cancel = strategy.submit_order, strategy.cancel_order

    def sent(*args, **kwargs):
        assert stack and stack[-1] == sale_table.Action.SEND
        return send(*args, **kwargs)

    def canceled(*args, **kwargs):
        assert stack and stack[-1] == sale_table.Action.CANCEL
        return cancel(*args, **kwargs)

    monkeypatch.setattr(strategy, "_sale_action", authorized)
    monkeypatch.setattr(strategy, "submit_order", sent)
    monkeypatch.setattr(strategy, "cancel_order", canceled)
    steps = history["steps"]
    index = 0
    while index < len(steps):
        step = steps[index]
        before = row(strategy)
        assert before.row == step["row"], (group, name, step)
        previous_effects = len(strategy.effects)
        transitions.clear()
        history_observation(strategy, step)
        expected = []
        for state, code, result in transitions:
            if (
                code == "NQ"
                and not result.actions
                and (index >= len(steps) or steps[index]["event"] != code)
            ):
                assert result.state == state
                continue
            # Engine echoes occur inside the action. Some recorded paths spell them out;
            # every other path has the same inert echoes implicitly.
            if (
                code in {"oINI", "oPCN", "FRZ", "UFZ"}
                and not result.actions
                and not result.named
                and (index >= len(steps) or steps[index]["event"] != code)
            ):
                continue
            wanted = steps[index]
            assert (state.row, code, result.when) == (
                wanted["row"],
                wanted["event"],
                wanted["when"],
            ), (name, wanted, transitions)
            branch = next(
                b
                for b in TABLE_DATA["cells"][state.row][code]["branches"]
                if b["when"] == wanted["when"]
            )
            assert result.state.row == branch["next"]
            expected.extend(
                a
                for key, a in (
                    ("send", sale_table.Action.SEND),
                    ("cancel", sale_table.Action.CANCEL),
                )
                if branch[key]
            )
            index += 1
        assert strategy.effects[previous_effects:] == expected, (name, step, strategy.said)
        assert row(strategy) == transitions[-1][2].state
    assert row(strategy).row == history["ends_in"]
    assert len(strategy.sent) == history["sends"]


@pytest.mark.parametrize("group,name,history", CASES, ids=[name for _, name, _ in CASES])
def test_no_action_transition_cannot_send_or_cancel_in_any_history(
    group, name, history, monkeypatch
):
    strategy = counted()
    if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
        strategy.cached.working.append(h.working_sell(qty="1", filled="0"))
        strategy.cached.working[-1].history_cover = True
        strategy._sale_step(h.AAA, [])
    original = sale_table.transition
    observed = []
    if name in {"expired-late-fill-covered", "refused-then-covered"}:
        strategy.cached.working.append(
            h.working_sell(qty="9" if name == "refused-then-covered" else "1", filled="0")
        )
        strategy._sale_step(h.AAA, [])

    def no_actions(state, n, code, facts):
        result = original(state, n, code, facts)
        observed.append((state, code, result))
        return replace(result, actions=())

    monkeypatch.setattr(sale_table, "transition", no_actions)
    steps, index, supplied = history["steps"], 0, 0
    while index < len(steps):
        observed.clear()
        history_observation(strategy, steps[index])
        assert observed
        for state, code, result in observed:
            if (
                code == "NQ"
                and not result.actions
                and (index >= len(steps) or steps[index]["event"] != code)
            ):
                assert result.state == state
                continue
            if (
                code in {"FRZ", "UFZ"}
                and result.state == state
                and not result.actions
                and (index >= len(steps) or steps[index]["event"] != code)
            ):
                continue
            wanted = steps[index]
            assert (state.row, code, result.when) == (
                wanted["row"],
                wanted["event"],
                wanted["when"],
            ), (name, wanted, observed)
            index += 1
            if sale_table.Action.SEND in result.actions:
                # The tape supplies the cache that the next observation describes.
                # This never calls the executor, submit_order or cancel_order.
                supplied += 1
                slot = strategy.slots[h.AAA]
                quantity = strategy.cached.held[0].signed_qty
                if name in {"expired-late-fill-covered", "refused-then-covered"}:
                    quantity -= 9 if name == "refused-then-covered" else 1
                if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
                    quantity -= 1
                if state.row == "G":
                    slot.opening_quantity = quantity
                    price = slot.counted_quote.bid_price.as_decimal()
                    slot.sale = Sale(strategy.clock.timestamp_ns(), price, None, 0, price)
                slot.sale.priced_ns = strategy.clock.timestamp_ns()
                cid = h.ClientOrderId(f"canon-protect-20260101T000000Z-{supplied}-12345678")
                slot.protect_id = cid
                slot.flight_sent_ns = strategy.clock.timestamp_ns()
                slot.flight_told = slot.flight_answered = False
                slot.sale.order_ids.add(cid)
                strategy.sent_ids.add(cid)
                strategy._sale_orders[cid] = strategy.order_factory.limit(
                    h.AAA,
                    h.OrderSide.SELL,
                    h.Quantity.from_str(str(quantity)),
                    slot.counted_quote.bid_price,
                    client_order_id=cid,
                )
                strategy.cached.working.append(
                    h.working_sell(
                        str(cid),
                        qty=str(quantity),
                        filled="0",
                        strategy_id=h.PROTECTOR,
                        status=h.OrderStatus.INITIALIZED,
                        is_open=False,
                        price=slot.counted_quote.bid_price,
                        is_pending_cancel=False,
                        is_pending_update=False,
                        is_emulated=False,
                        events=list,
                    )
                )
            elif sale_table.Action.ALERT in result.actions:
                slot = strategy.slots.get(h.AAA) or strategy.stopped_sales.get(h.AAA)
                if result.state.row == "RW" or "not formable" in result.when:
                    slot.refusal_told = True
                elif "flight_alert_due" in result.when.split(" and "):
                    slot.flight_told = True
            elif sale_table.Action.CANCEL in result.actions:
                leg = strategy.cache.order(
                    strategy.slots.get(h.AAA, strategy.stopped_sales.get(h.AAA)).protect_id
                )
                leg.status = h.OrderStatus.PENDING_CANCEL
                leg.is_pending_cancel = True
                slot = strategy.slots.get(h.AAA) or strategy.stopped_sales[h.AAA]
                slot.flight_sent_ns = strategy.clock.timestamp_ns()
                slot.flight_told = slot.flight_answered = False
                slot.flight_command = "cancel"
        assert row(strategy) == observed[-1][2].state
    assert not strategy.sent and not strategy.canceled, (group, name)


def test_level_book_round_trip_assertion_has_teeth(tmp_path, monkeypatch):
    strategy = counted(account_label="example", level_book_path=tmp_path / "book")
    strategy.level_book_path, strategy.run = tmp_path / "book", "example"
    original = strategy._holding_state
    monkeypatch.setattr(strategy, "_holding_state", lambda *args: ("counting", 2, None))
    strategy._save_levels()
    assert strategy.write_failures
    monkeypatch.setattr(strategy, "_holding_state", original)
    strategy._save_levels()
    notes = []
    assert LevelBook.read(tmp_path / "book", report=notes.append)
    assert not notes


@pytest.mark.parametrize(
    "scenario", ["first", "reprice", "refused-expired", "stop-ack", "flight-stop", "filled"]
)
def test_sale_through_real_engine(scenario, monkeypatch):
    """The pinned native cache, dispatch, local echoes and execution engine; no transport."""
    gc.collect()
    monkeypatch.setattr(h.protect.PLANNER, "chase_secs", 0)
    sends = []
    cancels = []
    timers = set()
    quotes = []

    class Probe(h.EngineProtector):
        def __new__(cls) -> Self:
            config = h.OwnerProtectorConfig(
                strategy_id=h.PROTECTOR, row="OWN1", breach_count=2, confirm_gap_ms=0
            )
            return super().__new__(cls, config)  # ty: ignore[too-many-positional-arguments]

        def __init__(self):
            super().__init__(
                h.OwnerProtectorConfig(
                    strategy_id=h.PROTECTOR, row="OWN1", breach_count=2, confirm_gap_ms=0
                )
            )
            self.notes = []
            self.callbacks = []
            self.finished = False
            self.result = None

        def _say(self, text):
            self.notes.append(text)

        def on_start(self):
            super().on_start()
            self.subscribe_quotes(h.AAA)

        def check_timer(self):
            if getattr(self, "stop_requested", False):
                self.stop_requested = False
                self.stop_instrument(h.AAA)
            slot = self.slots.get(h.AAA) or self.stopped_sales.get(h.AAA)
            if slot is None or slot.sale_state is None:
                return
            timers.add(slot.sale_state.row)
            flush_events(self)
            before = slot.sale_state, len(sends), len(cancels)
            self._sale_step(h.AAA, ["NQ"])
            assert (slot.sale_state, len(sends), len(cancels)) == before
            timers.add(slot.sale_state.row)

        def on_quote(self, quote):
            if (
                h.AAA not in self.slots
                and h.AAA not in self.stopped_instruments
                and self.cache.positions_open(instrument_id=h.AAA, strategy_id=h.EXTERNAL)
            ):
                self.adopt_holding(h.AAA, h.Price.from_str("9.90"))
                if scenario == "filled":
                    slot = self.slots[h.AAA]
                    slot.gain_armed_at = self.clock.timestamp_ns()
                    slot.gain_entry = Decimal("9.00")
                    slot.gain_floor = h.Price.from_str("9.50")
            super().on_quote(quote)
            quotes.append(quote)
            self.check_timer()

        def on_order_event(self, event):
            if scenario == "stop-ack" and type(event).__name__ == "OrderCanceled":
                self.stop_requested = True
            super().on_order_event(event)
            self.callbacks.append(type(event).__name__)

    probe = Probe()

    class Data(h.SyntheticData):
        async def publish_tape(self):
            started = self.clock.timestamp_ns()
            for index, bid in enumerate(["10.00", "10.00", "9.89", "9.88", "9.80", "9.79"]):
                self._handle_data(h.quote(bid, started + index * h.SECOND))
                await asyncio.sleep(0.04)
                if probe.finished:
                    break
            probe.check_timer()
            probe.check_timer()
            probe.result = (len(sends), len(cancels), row(probe))
            probe.shutdown_system("synthetic sale complete")

    class Execution(h.SyntheticExecution):
        async def _submit_order(self, command):
            order = command.order
            if order.side == h.OrderSide.BUY:
                await super()._submit_order(command)
                return
            sends.append(order)
            assert order.order_type == h.OrderType.LIMIT
            assert order.is_reduce_only and order.time_in_force == h.TimeInForce.DAY
            assert command.params == {"extended_hours": True}
            if scenario == "flight-stop":
                probe.stop_instrument(h.AAA)
            self.generate_order_submitted(order)
            venue = h.VenueOrderId(str(order.client_order_id))
            self.generate_order_accepted(order, venue, self.clock.timestamp_ns())
            if scenario in {"reprice", "filled"} and len(sends) == 1:
                self.generate_order_filled(
                    order,
                    venue,
                    None,
                    h.TradeId("synthetic-fill"),
                    h.Quantity.from_int(10 if scenario == "filled" else 3),
                    order.price,
                    h.USD,
                    h.Money.from_str("0 USD"),
                    h.LiquiditySide.TAKER,
                    self.clock.timestamp_ns(),
                )
            if scenario in {"first", "filled"} or len(sends) == 2:
                probe.finished = True

        async def _cancel_order(self, command):
            cancels.append(command)
            order = self.cache.order(command.client_order_id)
            assert order is not None
            if scenario == "refused-expired":
                self.generate_order_cancel_rejected(
                    order, order.venue_order_id, "synthetic refusal", self.clock.timestamp_ns()
                )
                self.generate_order_expired(order, order.venue_order_id, self.clock.timestamp_ns())
            else:
                self.generate_order_canceled(order, order.venue_order_id, self.clock.timestamp_ns())
            if scenario in {"refused-expired", "stop-ack", "flight-stop"}:
                probe.finished = True

    class DataFactory(h.DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=h.Venue("ALPACA"))

    class ExecutionFactory(h.ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Execution(
                **kwargs,
                venue=h.Venue("ALPACA"),
                account_id=h.AccountId("ALPACA-001"),
                account_type=h.AccountType.CASH,
                oms_type=h.OmsType.NETTING,
            )

    node = h.LiveNode.build(
        "COUNTED-SALE-TEST",
        h.LiveNodeConfig(
            environment=h.Environment.SANDBOX,
            trader_id=h.TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": h.DataClientConfig()},
            exec_clients={"ALPACA": h.ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    node.add_strategy(h.Owner())
    node.add_strategy(probe)

    async def run():
        async with asyncio.timeout(5):
            await node.run_async()

    try:
        asyncio.run(run())
        assert probe.result is not None
        sent_count, cancel_count, last = probe.result
        assert sent_count == (2 if scenario == "reprice" else 1), probe.notes
        assert cancel_count == (0 if scenario in {"first", "filled"} else 1)
        assert "OrderInitialized" in probe.callbacks
        assert not {"OrderEmulated", "OrderReleased"}.intersection(probe.callbacks)
        if scenario == "reprice":
            assert sends[1].quantity.as_decimal() == 7
            # The next quote is the step that judges the acknowledgement, and prices the send.
            assert sends[1].price.as_decimal() == Decimal("9.78")
            line = next(note for note in probe.notes if "ack_to_send_ms=" in note)
            assert all(key in line for key in ("limit=9.78", "level=", "previous=", "floor="))
            delay = Decimal(line.split("ack_to_send_ms=", 1)[1])
            assert 0 <= delay <= 1000
        if scenario in {"stop-ack", "flight-stop"}:
            assert last.row == "T0"
        if scenario == "refused-expired":
            assert last == sale_table.State("G", 0)
        if scenario == "filled":
            assert last.row == "X"
            assert len(probe.gain_lock_sales) == 1
            assert probe.gain_lock_sales[0]["quantity"] == "10"
            assert probe.gain_lock_sales[0]["spread_at_release"] is not None
            ended_records = [
                json.loads(note.split("sale record ", 1)[1])
                for note in probe.notes
                if "sale record " in note
            ]
            assert sum(record["trigger_to_fill_ns"] is not None for record in ended_records) == 1
        if scenario in {"first", "reprice"}:
            assert not any(note.startswith("ALERT") for note in probe.notes), probe.notes
        assert "G" in timers and "FL" in timers
    finally:
        node.dispose()


def extra_engine_history(prefix, extra, ends_in):
    return {
        "steps": [*prefix, *[{"row": r, "event": e, "when": w} for r, e, w in extra]],
        "ends_in": ends_in,
        "sends": 1,
    }


FIRST = HISTORIES["named_paths"]["two-legs-complete"]["steps"]
ENGINE_CASES = [
    *CASES,
    (
        "timer",
        "flight-ends",
        extra_engine_history(
            FIRST[:2],
            [
                ("FL", "HE", "always"),
                ("OX", "oACC", "always"),
                ("PCX", "oCXL", "always"),
            ],
            "X",
        ),
    ),
    (
        "timer",
        "flight-stops",
        extra_engine_history(
            FIRST[:2],
            [
                ("FL", "ST", "always"),
                ("TW", "oACC", "always"),
                ("PCT", "oCXL", "always"),
            ],
            "T0",
        ),
    ),
    (
        "timer",
        "pending-freezes",
        extra_engine_history(
            FIRST[:5],
            [
                ("PCR", "FRZ", "always"),
                ("FCP", "oCXL", "always"),
            ],
            "FC",
        ),
    ),
    (
        "timer",
        "pending-fault",
        extra_engine_history(
            FIRST[:5],
            [("PCR", "oOTH", "always"), ("FZP", "oCXL", "always")],
            "FZ",
        ),
    ),
    (
        "cover",
        "pending-predecessor-new-count",
        HISTORIES["named_paths"]["unread-cover-new-count"],
    ),
]
ENGINE_TIMERS = set()


@pytest.mark.parametrize("group,name,history", ENGINE_CASES, ids=[c[1] for c in ENGINE_CASES])
def test_tracked_history_through_native_cache_and_dispatch(group, name, history, monkeypatch):
    """Gain lock ON; logical clock drives the history's minute intervals, native events do the rest."""
    gc.collect()
    transitions, effects, stack = [], [], []
    clients = []
    failures = []
    completed = []
    timers = set()
    original_transition = sale_table.transition

    def traced(state, n, code, facts):
        result = original_transition(state, n, code, facts)
        transitions.append((state, code, result))
        return result

    monkeypatch.setattr(sale_table, "transition", traced)

    class Probe(h.EngineProtector):
        def __new__(cls) -> Self:
            config = h.OwnerProtectorConfig(
                strategy_id=h.PROTECTOR, row="OWN1", breach_count=2, confirm_gap_ms=0
            )
            return super().__new__(cls, config)  # ty: ignore[too-many-positional-arguments]

        def __init__(self):
            super().__init__(
                h.OwnerProtectorConfig(
                    strategy_id=h.PROTECTOR, row="OWN1", breach_count=2, confirm_gap_ms=0
                )
            )
            self.notes = []
            self.legs = []
            self.history_clock = h.Clock.new_test()

        @property
        def clock(self):
            return self.history_clock

        def _say(self, text):
            self.notes.append(text)

        def on_start(self):
            self.history_clock.set_time(super().clock.timestamp_ns())
            self.subscribe_quotes(h.AAA)

        def _sale_action(self, instrument_id, slot, action, candidate, before, reason):
            assert slot.sale_state == transitions[-1][2].state
            stack.append(action)
            try:
                return super()._sale_action(instrument_id, slot, action, candidate, before, reason)
            finally:
                stack.pop()

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            assert stack[-1] == sale_table.Action.SEND
            effects.append(stack[-1])
            self.legs.append(order.client_order_id)
            return super().submit_order(order, position_id, client_id, params)

        def cancel_order(self, *args, **kwargs):
            assert stack[-1] == sale_table.Action.CANCEL
            effects.append(stack[-1])
            return super().cancel_order(*args, **kwargs)

        def check_timer(self):
            flush_events(self)
            slot = self.slots.get(h.AAA) or self.stopped_sales.get(h.AAA)
            at = self.clock.timestamp_ns()
            step = TimeEvent("test-step", h.UUID4(), at, at)
            if slot is None:
                before = list(effects)
                self._on_step(step)
                assert effects == before
                timers.add("T0")
                return
            before = slot.sale_state, list(effects)
            observed_before = len(transitions)
            self._on_step(step)
            assert (slot.sale_state, effects) == before
            assert len(transitions) > observed_before and transitions[-1][1] == "NQ"
            assert slot.sale_state is not None
            timers.add(slot.sale_state.row)

    probe, owner = Probe(), h.Owner()

    class Execution(h.SyntheticExecution):
        async def _submit_order(self, command):
            order = command.order
            if order.strategy_id == h.PROTECTOR:
                assert order.order_type == h.OrderType.LIMIT
                assert order.time_in_force == h.TimeInForce.DAY and order.is_reduce_only
                assert command.params == {"extended_hours": True}
                return  # The tape delivers each recorded event, including submissions.
            self.generate_order_submitted(order)
            venue = h.VenueOrderId(str(order.client_order_id))
            self.generate_order_accepted(order, venue, self.clock.timestamp_ns())
            if order.order_type == h.OrderType.MARKET:
                self.fill(order, order.quantity.as_decimal())

        def fill(self, order, quantity):
            self.generate_order_filled(
                order,
                h.VenueOrderId(str(order.client_order_id)),
                None,
                h.TradeId(f"fill-{self.clock.timestamp_ns()}"),
                h.Quantity.from_str(str(quantity)),
                h.Price.from_str("9.80"),
                h.USD,
                h.Money.from_str("0 USD"),
                h.LiquiditySide.TAKER,
                self.clock.timestamp_ns(),
            )

        async def _cancel_order(self, command):
            pass  # The tape supplies the acknowledgement or refusal.

    class Data(h.SyntheticData):
        async def publish_tape(self):
            try:
                await self.walk()
            except Exception as exc:  # noqa: BLE001 - every task failure must reach pytest.
                failures.append(exc)
            finally:
                probe.shutdown_system("synthetic history complete")

        async def pause(self):
            await asyncio.sleep(0.02)

        def held(self):
            positions = self.cache.positions_open(instrument_id=h.AAA, strategy_id=h.EXTERNAL)
            return sum((p.signed_qty for p in positions), Decimal(0))

        def cached_order(self, cid) -> Any:
            order = self.cache.order(cid)
            assert order is not None
            return order

        async def cover(self, quantity, *, foreign=False):
            order = owner.order_factory.limit(
                h.AAA,
                h.OrderSide.SELL,
                h.Quantity.from_str(str(quantity)),
                h.Price.from_str("20.00"),
                client_order_id=h.ClientOrderId(
                    f"foreign-{len(self.covers)}"
                    if foreign
                    else f"canon-protect-20260101T000000Z-{len(self.covers)}-12345678"
                ),
            )
            self.covers.append(order.client_order_id)
            owner.submit_order(order)
            await self.pause()

        async def observe(self, step):
            code, when = step["event"], step["when"]
            slot = probe.slots.get(h.AAA) or probe.stopped_sales.get(h.AAA)
            assert slot is not None
            client = clients[0]
            now = history_time(probe.clock.timestamp_ns(), slot, when)
            probe.history_clock.set_time(now)
            if code in {"QU", "QA"}:
                assert slot.level is not None
                bid = slot.level.as_decimal() + (Decimal(1) if code == "QA" else Decimal("-0.10"))
                if "not formable" in when:
                    bid = Decimal("0.001")
                if when == "reprice_due":
                    bid -= Decimal("0.10")
                self._handle_data(h.quote(str(bid), now))
            elif code in {"CP", "CL"}:
                if code == "CP":
                    whole = when == "whole_cover" or (when == "always" and step["row"] == "WK")
                    await self.cover(self.held() if whole else self.held() - 1)
                else:
                    for cid in self.covers:
                        order = self.cached_order(cid)
                        if not order.is_closed:
                            client.generate_order_canceled(order, order.venue_order_id, now)
                    await self.pause()
                probe._sale_step(h.AAA, [])
            elif code in {"HE", "HS", "HG"}:
                quantity = self.held() if code == "HE" else Decimal(1)
                owner.submit_order(
                    owner.order_factory.market(
                        h.AAA,
                        h.OrderSide.BUY if code == "HG" else h.OrderSide.SELL,
                        h.Quantity.from_str(str(quantity)),
                    )
                )
                await self.pause()
                probe._sale_step(h.AAA, [])
            elif code == "ST":
                probe.stop_instrument(h.AAA)
            elif code == "FRZ":
                if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
                    cover = self.cached_order(self.covers[-1])
                    client._handle_event(
                        OrderPendingCancel(
                            cover.trader_id,
                            cover.strategy_id,
                            h.AAA,
                            cover.client_order_id,
                            cover.account_id,
                            h.UUID4(),
                            now,
                            now,
                            False,
                            cover.venue_order_id,
                        )
                    )
                    await self.pause()
                else:
                    await self.cover(1, foreign=name != "pending-predecessor-new-count")
                    if name == "pending-predecessor-new-count":
                        cover = self.cached_order(self.covers[-1])
                        client._handle_event(
                            OrderPendingCancel(
                                cover.trader_id,
                                cover.strategy_id,
                                h.AAA,
                                cover.client_order_id,
                                cover.account_id,
                                h.UUID4(),
                                now,
                                now,
                                False,
                                cover.venue_order_id,
                            )
                        )
                        await self.pause()
                probe._sale_step(h.AAA, [])
            elif code == "UFZ":
                cover = self.cached_order(self.covers[-1])
                if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
                    client.generate_order_accepted(cover, cover.venue_order_id, now)
                else:
                    client.generate_order_canceled(cover, cover.venue_order_id, now)
                await self.pause()
                probe._sale_step(h.AAA, [])
            elif code == "NQ":
                probe._sale_step(h.AAA, ["NQ"])
            else:
                orders = [self.cached_order(cid) for cid in probe.legs]
                order = next(o for o in orders if o.is_closed) if code == "oLF" else orders[-1]
                venue = h.VenueOrderId(str(order.client_order_id))
                if code in {"oPF", "oFF", "oLF"}:
                    quantity = order.leaves_qty.as_decimal() if code == "oFF" else Decimal(1)
                    if code == "oLF" and when == "not long_remains":
                        quantity = self.held()
                    if when == "whole_cover" or when.endswith("and whole_cover"):
                        quantity = self.held() - sum(
                            (self.cached_order(cid).leaves_qty.as_decimal() for cid in self.covers),
                            Decimal(0),
                        )
                    client.fill(order, quantity)
                elif code == "oSUB":
                    client.generate_order_submitted(order)
                elif code == "oACC":
                    client.generate_order_accepted(order, venue, now)
                elif code == "oCXL":
                    client.generate_order_canceled(order, venue, now)
                elif code == "oEXP":
                    client.generate_order_expired(order, venue, now)
                elif code == "oCRJ":
                    client.generate_order_cancel_rejected(order, venue, "synthetic refusal", now)
                elif code == "oREJ":
                    client.generate_order_rejected(order, "synthetic refusal", now, False)
                elif code == "oDEN":
                    client.generate_order_denied(order, "synthetic engine denial")
                elif code == "oUPD":
                    # Only reconciliation can mark an updated quantity as filled
                    # (orders/mod.rs:1322-1325); ordinary updates preserve its status.
                    client._handle_event(
                        OrderUpdated(
                            order.trader_id,
                            order.strategy_id,
                            h.AAA,
                            order.client_order_id,
                            order.filled_qty,
                            h.UUID4(),
                            now,
                            now,
                            True,
                            venue_order_id=venue,
                            account_id=order.account_id,
                            price=order.price,
                        )
                    )
                elif code == "oOTH":
                    client.generate_order_modify_rejected(order, venue, "unasked update", now)
                elif code == "oPCN":
                    client._handle_event(
                        OrderPendingCancel(
                            order.trader_id,
                            order.strategy_id,
                            h.AAA,
                            order.client_order_id,
                            order.account_id,
                            h.UUID4(),
                            now,
                            now,
                            False,
                            venue,
                        )
                    )
                elif code == "oINI":
                    flush_events(probe)
                else:
                    raise AssertionError(f"unconsumed local echo: {code}")
            await self.pause()
            flush_events(probe)

        async def walk(self):
            self.covers = []
            self._handle_data(h.quote("10.00", self.clock.timestamp_ns()))
            await self.pause()
            if name in {"expired-late-fill-covered", "refused-then-covered"}:
                await self.cover(9 if name == "refused-then-covered" else 1)
            if name in {"cover-restores-working-sale", "fault-during-unread-cover"}:
                await self.cover(1)
            probe.adopt_holding(h.AAA, h.Price.from_str("9.90"))
            await self.pause()
            index = 0
            steps = history["steps"]
            while index < len(steps):
                transitions.clear()
                previous = len(effects)
                await self.observe(steps[index])
                assert transitions, (name, steps[index], probe.notes)
                expected = []
                for state, code, result in transitions:
                    if (
                        code == "NQ"
                        and not result.actions
                        and (index >= len(steps) or steps[index]["event"] != code)
                    ):
                        assert result.state == state
                        continue
                    if (
                        code in {"oINI", "oPCN", "FRZ", "UFZ"}
                        and not result.actions
                        and not result.named
                        and (index >= len(steps) or steps[index]["event"] != code)
                    ):
                        continue
                    wanted = steps[index]
                    assert (state.row, code, result.when) == (
                        wanted["row"],
                        wanted["event"],
                        wanted["when"],
                    ), (name, wanted, transitions, probe.notes)
                    branch = next(
                        b
                        for b in TABLE_DATA["cells"][state.row][code]["branches"]
                        if b["when"] == wanted["when"]
                    )
                    assert result.state.row == branch["next"]
                    expected.extend(
                        a
                        for key, a in (
                            ("send", sale_table.Action.SEND),
                            ("cancel", sale_table.Action.CANCEL),
                        )
                        if branch[key]
                    )
                    index += 1
                assert effects[previous:] == expected
                assert row(probe) == transitions[-1][2].state
                if name == "unread-cover-cuts-working-sale" and steps[index - 1]["event"] == "oCXL":
                    assert len(probe.legs) == 1
                    assert self.cached_order(probe.legs[0]).status == h.OrderStatus.CANCELED
                    assert self.cached_order(self.covers[0]).status == h.OrderStatus.ACCEPTED
                probe.check_timer()
            assert row(probe).row == history["ends_in"]
            assert len(probe.legs) == history["sends"]
            if name in {"unread-cover-new-count", "pending-predecessor-new-count"}:
                assert self.cached_order(probe.legs[-1]).quantity.as_decimal() == 10
            if name == "fault-during-unread-cover":
                reason = probe.frozen[h.AAA]
                cover = self.cached_order(self.covers[-1])
                clients[0].generate_order_accepted(
                    cover, cover.venue_order_id, probe.clock.timestamp_ns()
                )
                await self.pause()
                probe._sale_step(h.AAA, ["NQ"])
                assert row(probe).row == "FZW" and probe.frozen[h.AAA] == reason
            completed.append((name, sorted(timers)))
            ENGINE_TIMERS.update(timers)

    class DataFactory(h.DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=h.Venue("ALPACA"))

    class ExecutionFactory(h.ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = Execution(
                **kwargs,
                venue=h.Venue("ALPACA"),
                account_id=h.AccountId("ALPACA-001"),
                account_type=h.AccountType.CASH,
                oms_type=h.OmsType.NETTING,
            )
            clients.append(client)
            return client

    node = h.LiveNode.build(
        "COUNTED-HISTORY-TEST",
        h.LiveNodeConfig(
            environment=h.Environment.SANDBOX,
            trader_id=h.TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": h.DataClientConfig()},
            exec_clients={"ALPACA": h.ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    node.add_strategy(owner)
    node.add_strategy(probe)

    async def run():
        async with asyncio.timeout(5):
            await node.run_async()

    try:
        asyncio.run(run())
        assert not failures, failures
        assert completed, probe.notes
    finally:
        node.dispose()


def test_native_timer_coverage_reaches_every_table_row():
    assert ENGINE_TIMERS == set(sale_table.TABLE.rows)


def test_driver_keeps_each_order_event_even_when_two_have_the_same_column(monkeypatch):
    strategy = counted()
    first_leg(strategy)
    seen = []
    original = sale_table.transition

    def record(state, n, code, facts):
        seen.append(code)
        return original(state, n, code, facts)

    monkeypatch.setattr(sale_table, "transition", record)
    strategy._sale_step(h.AAA, ["oACC", "oACC"])
    assert seen == ["oACC", "oACC"]


def test_whole_node_flight_stop_names_leg_and_consequence_in_one_alert():
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    leg = strategy.cached.working[-1]
    strategy.said.clear()
    strategy.on_stop()
    alerts = [line for line in strategy.said if line.startswith("ALERT")]
    assert len(alerts) == 1
    line = alerts[0]
    assert str(h.AAA) in line and "SELL" in line
    assert str(leg.quantity) in line and str(leg.price) in line
    assert "NO EFFECTIVE EXIT" in line and "session part" in line and "cover" in line
    assert str(leg.client_order_id) not in line and not strategy.canceled
    assert len(line.split(": ", 1)[1]) <= 160


def test_alert_redacts_venue_id_and_bounds_reason():
    strategy = counted()
    first_leg(strategy)
    leg = strategy.cached.working[-1]
    leg.venue_order_id = h.VenueOrderId("synthetic-private-order")
    strategy.deliver_then_step(
        "Rejected",
        reason=f"{leg.client_order_id} {leg.venue_order_id} " + "reason " * 50 + "\nsecond line",
    )
    alerts = [line for line in strategy.said if line.startswith("ALERT")]
    assert len(alerts) == 1
    assert "synthetic-private-order" not in alerts[0]
    assert str(leg.client_order_id) not in alerts[0]
    # "ALERT: <instrument>: <reason>": the form the protector logs as an error.
    assert alerts[0].startswith("ALERT: ")
    assert len(alerts[0].split(": ", 2)[2]) <= 160
    assert "\n" not in alerts[0]


def test_unformable_native_quantity_is_a_table_refusal(monkeypatch):
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    instrument = strategy.cache.instrument(h.AAA)

    def refused(_quantity):
        raise ValueError("quantity cannot be formed")

    monkeypatch.setattr(
        strategy.cached,
        "instrument",
        lambda _: SimpleNamespace(
            make_qty=refused,
            make_price=instrument.make_price,
            price_increment=instrument.price_increment,
        ),
    )
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert row(strategy).row == "G" and not strategy.sent
    assert any(line.startswith("ALERT") and "G x QU" in line for line in strategy.said)


@pytest.mark.parametrize(
    "defect", ["zero-bid", "bid-size", "ask-size", "crossed", "ahead", "duplicate", "late"]
)
def test_refused_stream_quote_changes_neither_count_level_nor_price(defect):
    strategy = counted()
    strategy.observe("9.89", h.T0 + h.SECOND)
    slot = strategy.slots[h.AAA]
    before = row(strategy), slot.level, slot.counted_quote
    at = h.T0 + 2 * h.SECOND
    bid, ask, bid_size, ask_size, stamp = "11.00", "11.01", 1, 1, at
    if defect == "zero-bid":
        bid = "0.00"
    elif defect == "bid-size":
        bid_size = 0
    elif defect == "ask-size":
        ask_size = 0
    elif defect == "crossed":
        ask = "10.00"
    elif defect == "ahead":
        stamp += 60 * h.SECOND
    elif defect == "duplicate":
        stamp = h.T0 + h.SECOND
    else:
        stamp = h.T0
    strategy.test_clock.set_time(at)
    strategy.on_quote(
        h.QuoteTick(
            h.AAA,
            h.Price.from_str(bid),
            h.Price.from_str(ask),
            h.Quantity.from_int(bid_size),
            h.Quantity.from_int(ask_size),
            stamp,
            at,
        )
    )
    assert (row(strategy), slot.level, slot.counted_quote) == before
    assert not strategy.effects


def test_gain_lock_raises_local_level_and_keeps_it_without_an_emulated_order():
    strategy = counted()
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.observe("12.00", h.T0 + h.SECOND)
    slot = strategy.slots[h.AAA]
    assert slot.gain_applied_floor is not None
    assert slot.level >= strategy._gain_trigger(
        strategy.cache.instrument(h.AAA),
        slot,
        slot.gain_applied_floor,
    )
    raised = slot.level
    strategy.observe("11.95", h.T0 + 2 * h.SECOND)
    assert slot.level >= raised and not strategy.sent


def test_new_position_cycle_after_ended_row_gets_its_own_count():
    strategy = counted()
    first_leg(strategy)
    old = strategy.slots[h.AAA]
    strategy.deliver_then_step("Filled", quantity=10)
    assert row(strategy).row == "X"
    position = strategy.cached.held[0]
    position.signed_qty = Decimal(10)
    position.quantity = h.Quantity.from_int(10)
    position.ts_opened = position.ts_init = h.T0 + 3 * h.SECOND
    strategy.observe("10.00", h.T0 + 4 * h.SECOND)
    assert strategy.slots[h.AAA] is not old
    assert row(strategy) == sale_table.State("G", 0)
    assert len(strategy.sent) == 1


def test_episode_floor_wait_is_judged_only_by_counted_stream_quotes():
    strategy = counted()
    first_leg(strategy)
    strategy.observe("9.00", h.T0 + 10 * h.SECOND)
    strategy.deliver_then_step("Canceled")
    strategy.deliver_then_step("Accepted")
    slot = strategy.slots[h.AAA]
    floor = slot.sale.floor
    assert strategy.sent[-1].price.as_decimal() == floor
    strategy.observe("8.99", h.T0 + 20 * h.SECOND)
    since = slot.sale.floor_since
    assert since is not None
    strategy.test_clock.set_time(h.T0 + 120 * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))
    strategy.on_historical_quotes([h.quote("8.00", h.T0 + 121 * h.SECOND)])
    assert slot.sale.floor == floor and slot.sale.floor_since == since
    strategy.observe("8.50", h.T0 + 122 * h.SECOND)
    assert slot.sale.floor is None and row(strategy).row == "PCR"
    strategy.deliver_then_step("Canceled")
    assert strategy.sent[-1].price.as_decimal() < floor


def test_predecessor_cover_never_becomes_this_instances_chased_or_canceled_leg():
    strategy = Counted(breach_count=2, confirm_gap_ms=0)
    predecessor = h.working_sell(qty="10", filled="0")
    strategy.cached.working.append(predecessor)
    strategy.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert row(strategy).row == "GC" and not strategy.sent
    strategy.stop_instrument(h.AAA)
    assert not strategy.canceled


def test_ended_holding_retires_its_earlier_book_level(tmp_path):
    path = tmp_path / "book"
    strategy = counted(account_label="example", level_book_path=path)
    strategy.level_book_path, strategy.run = path, "example"
    first_leg(strategy)
    assert LevelBook.read(path)["example", h.AAA].last_level is not None
    strategy.deliver_then_step("Filled", quantity=10)
    strategy._save_levels()
    saved = LevelBook.read(path)["example", h.AAA]
    assert saved.last_level is None and saved.reason == "holding ended"


def test_snapshot_freeze_alert_names_the_unchanged_cover_refusal():
    strategy = counted()
    strategy.cached.working.append(h.working_sell("external-order-not-ours", qty="1", filled="0"))
    strategy._on_step(SimpleNamespace(ts_event=h.T0 + h.SECOND))
    alerts = [line for line in strategy.said if line.startswith("ALERT")]
    assert row(strategy).row == "FC" and not strategy.effects
    assert len(alerts) == 1 and "covered in part by a foreign sell" in alerts[0]


def test_timer_still_observes_a_stopped_sale_after_cancel_refusal():
    strategy = counted()
    first_leg(strategy)
    strategy.stop_instrument(h.AAA)
    strategy.deliver_then_step("CancelRejected")
    assert row(strategy).row == "TW" and len(strategy.canceled) == 1
    strategy.cached.held[0].signed_qty = Decimal(5)
    strategy.cached.held[0].quantity = h.Quantity.from_int(5)
    strategy._on_step(SimpleNamespace(ts_event=h.T0 + 60 * h.SECOND))
    assert row(strategy).row == "PCT" and len(strategy.canceled) == 2
