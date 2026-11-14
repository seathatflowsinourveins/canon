"""U-d: a released owner sale chases using main's price rule, in every session.

The command harness uses native orders/events; the final simulation uses the real core and
synthetic adapter clients. No credential, network, paper state, catalog or ledger is used.
"""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.core import UUID4
from nautilus_trader.model import AccountId
from nautilus_trader.model import OrderAccepted
from nautilus_trader.model import OrderCanceled
from nautilus_trader.model import OrderCancelRejected
from nautilus_trader.model import OrderExpired
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderRejected
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderSubmitted
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import VenueOrderId

from canon_trading import protect
from canon_trading.mover import FLOOR_WAIT_SECS
from canon_trading.mover import chase_floor
from canon_trading.price_step import exit_price
from tests import test_owner_protector as h

ROWS = protect.PLANNER
CHASE = ROWS.chase_secs * h.SECOND
WAIT = FLOOR_WAIT_SECS * h.SECOND
RELEASE = h.T0 + h.SECOND


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the credential-environment fixture for these offline cases."""


def accepted(order: Any, at: int) -> None:
    order.apply(
        OrderSubmitted(
            order.trader_id,
            order.strategy_id,
            h.AAA,
            order.client_order_id,
            AccountId("ALPACA-001"),
            UUID4(),
            at,
            at,
        )
    )
    order.apply(
        OrderAccepted(
            order.trader_id,
            order.strategy_id,
            h.AAA,
            order.client_order_id,
            VenueOrderId(str(order.client_order_id)),
            AccountId("ALPACA-001"),
            UUID4(),
            at,
            at,
            False,
        )
    )


def terminal(order: Any, at: int, *, expired: bool = False) -> None:
    event_type = OrderExpired if expired else OrderCanceled
    order.apply(
        event_type(
            order.trader_id,
            order.strategy_id,
            h.AAA,
            order.client_order_id,
            UUID4(),
            at,
            at,
            False,
        )
    )


def filled(strategy: h.Harness, order: Any, quantity: int, at: int) -> None:
    event = OrderFilled(
        order.trader_id,
        order.strategy_id,
        h.AAA,
        order.client_order_id,
        VenueOrderId(str(order.client_order_id)),
        AccountId("ALPACA-001"),
        TradeId(f"fill-{at}"),
        OrderSide.SELL,
        OrderType.LIMIT,
        Quantity.from_int(quantity),
        order.price,
        h.USD,
        h.LiquiditySide.TAKER,
        UUID4(),
        at,
        at,
        False,
    )
    order.apply(event)
    remaining = strategy.cached.held[0].signed_qty - quantity
    strategy.cached.held = [] if not remaining else [h.position(str(remaining))]
    strategy.test_clock.set_time(at)
    strategy.on_order_filled(event)


def released(at: int = RELEASE) -> tuple[h.Harness, Any]:
    strategy = h.Harness()
    strategy.test_clock.set_time(at - h.SECOND)
    strategy.cached.latest = h.quote("10.00", at - h.SECOND)
    strategy.adopt_holding(h.AAA, Price.from_str("9.95"))
    h.new_quote(strategy)
    trailing = strategy.sent[0]
    strategy.test_clock.set_time(at)
    # The emulator transforms a trailing order into a native limit with the same client ID.
    order = strategy.order_factory.limit(
        h.AAA,
        OrderSide.SELL,
        Quantity.from_int(10),
        Price.from_str("9.90"),
        time_in_force=TimeInForce.DAY,
        reduce_only=True,
        client_order_id=trailing.client_order_id,
    )
    event = OrderReleased(
        order.trader_id,
        order.strategy_id,
        h.AAA,
        order.client_order_id,
        Price.from_str("9.89"),
        UUID4(),
        at,
        at,
    )
    order.apply(event)
    strategy.cached.working = [order]
    strategy.on_order_released(event)
    accepted(order, at)
    strategy.observe("9.89", at)
    return strategy, order


def working(strategy: h.Harness) -> list[Any]:
    return [
        order
        for order in strategy.cached.working
        if order.strategy_id == h.PROTECTOR and not order.is_closed
    ]


def expected_price(bid: str, floor: Decimal | None) -> Decimal:
    """P3 follows main's price function; a later fast-sell rule changes only that import."""
    return exit_price(Decimal(bid), ROWS.exit_ticks, floor)


def records(strategy: h.Harness) -> list[dict[str, Any]]:
    return [
        json.loads(line.split("sale record ", 1)[1])
        for line in strategy.said
        if "sale record " in line
    ]


def test_p3_chase_is_paced_downward_and_keeps_the_first_floor() -> None:
    strategy, first = released()
    floor = chase_floor(first.price.as_decimal(), ROWS.chase_cap)
    strategy.observe("9.80", RELEASE + CHASE - 1)
    assert not strategy.canceled
    strategy.observe("9.80", RELEASE + CHASE + h.SECOND)
    assert strategy.canceled == [first.client_order_id], "P3: released sale must chase"
    assert working(strategy) == [first]
    terminal(first, RELEASE + CHASE + h.SECOND)
    resent_at = RELEASE + CHASE + 2 * h.SECOND
    strategy.observe("9.80", resent_at)
    second = strategy.sent[-1]
    assert second.order_type == OrderType.LIMIT
    assert second.price.as_decimal() == expected_price("9.80", floor)
    accepted(second, resent_at)
    strategy.observe("9.70", resent_at + CHASE - 1)
    assert len(strategy.canceled) == 1
    strategy.observe("9.80", resent_at + CHASE)
    assert len(strategy.canceled) == 1, "unchanged price sends nothing"
    strategy.observe("10.00", resent_at + CHASE + h.SECOND)
    assert len(strategy.canceled) == 1, "a recovery never chases upward"
    strategy.observe("9.70", resent_at + CHASE + 2 * h.SECOND)
    assert strategy.canceled[-1] == second.client_order_id
    terminal(second, resent_at + CHASE + 2 * h.SECOND)
    strategy.observe("9.70", resent_at + CHASE + 3 * h.SECOND)
    assert strategy.sent[-1].price.as_decimal() == expected_price("9.70", floor)
    assert len(working(strategy)) == 1


def test_p5_gap_through_floor_releases_after_wait_and_records_the_sale() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("8.00", at)
    assert strategy.canceled == [first.client_order_id], "P5: gap must reach the chase floor"
    terminal(first, at)
    at += h.SECOND
    strategy.observe("8.00", at)
    at_floor = strategy.sent[-1]
    floor = chase_floor(first.price.as_decimal(), ROWS.chase_cap)
    assert at_floor.price.as_decimal() == floor
    accepted(at_floor, at)
    strategy.observe("8.00", at)
    strategy.observe("8.00", at + WAIT - 1)
    assert len(strategy.canceled) == 1
    strategy.observe("8.00", at + WAIT)
    assert strategy.canceled == [first.client_order_id, at_floor.client_order_id]
    terminal(at_floor, at + WAIT)
    strategy.observe("8.00", at + WAIT + h.SECOND)
    final = strategy.sent[-1]
    assert final.price.as_decimal() == expected_price("8.00", None)
    assert final.price.as_decimal() < floor, "the exit beats the gain trigger and chase floor"
    accepted(final, at + WAIT + h.SECOND)
    assert not any(row["trigger_to_fill_ns"] is not None for row in records(strategy))
    fill_at = at + WAIT + 2 * h.SECOND
    filled(strategy, final, 10, fill_at)
    strategy.observe("8.00", fill_at + h.SECOND)
    record = records(strategy)[-1]
    assert record["reprices"] == 2
    assert record["trigger_to_fill_ns"] == fill_at - RELEASE
    assert record["floor_reached"] is True
    assert record["floor_wait_ns"] == WAIT
    assert Decimal(record["floor_release_price"]) == expected_price("8.00", None)
    assert not working(strategy) and not strategy.cached.held


@pytest.mark.parametrize("hour", [5, 11, 17, 22], ids=["pre", "regular", "after", "overnight"])
def test_p2_p7_expired_sale_returns_as_a_marked_day_limit_in_every_session(hour) -> None:
    at = (
        int(datetime(2026, 11, 12, hour, tzinfo=ZoneInfo("America/New_York")).timestamp())
        * h.SECOND
    )
    strategy, first = released(at)
    terminal(first, at + h.SECOND, expired=True)
    strategy.observe("9.80", at + 2 * h.SECOND)
    assert len(strategy.sent) == 2
    sale = strategy.sent[-1]
    assert sale.order_type == OrderType.LIMIT, "P7: an expired sale must not re-arm a trail"
    assert sale.time_in_force == TimeInForce.DAY and sale.is_reduce_only
    assert sale.side == OrderSide.SELL and sale.quantity == Quantity.from_int(10)
    assert strategy.params[-1] == {"extended_hours": True}
    assert len(working(strategy)) == 1
    assert not any(row["trigger_to_fill_ns"] is not None for row in records(strategy))


def test_p4_p6_cancel_waits_and_reads_the_remaining_long_after_a_partial_fill() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    assert strategy.canceled == [first.client_order_id]
    strategy.observe("9.70", at + CHASE)
    assert len(strategy.sent) == 1 and working(strategy) == [first]
    filled(strategy, first, 4, at + CHASE + h.SECOND)
    strategy.observe("9.70", at + CHASE + 2 * h.SECOND)
    assert len(strategy.sent) == 1
    terminal(first, at + CHASE + 3 * h.SECOND)
    strategy.observe("9.70", at + CHASE + 4 * h.SECOND)
    assert len(working(strategy)) == 1
    assert strategy.sent[-1].quantity == Quantity.from_int(6)
    assert not any(row["trigger_to_fill_ns"] is not None for row in records(strategy))


def test_p6_fill_while_cancel_is_pending_never_sends_another_sale() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    assert strategy.canceled == [first.client_order_id]
    filled(strategy, first, 10, at + h.SECOND)
    strategy.observe("9.70", at + 2 * h.SECOND)
    assert len(strategy.sent) == 1 and not working(strategy)
    assert records(strategy)[-1]["trigger_to_fill_ns"] == at + h.SECOND - RELEASE


def test_p6_late_fill_of_a_canceled_leg_finishes_the_record_and_pulls_the_replacement() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    filled(strategy, first, 4, at + h.SECOND)
    terminal(first, at + 2 * h.SECOND)
    strategy.observe("9.80", at + 3 * h.SECOND)
    second = strategy.sent[-1]
    assert second.quantity == Quantity.from_int(6)
    accepted(second, at + 3 * h.SECOND)
    # The cancel and a fill can cross on the wire. Only the real native order state is changed.
    fill_at = at + 4 * h.SECOND
    filled(strategy, first, 6, fill_at)
    strategy.observe("9.80", at + 5 * h.SECOND)
    assert strategy.canceled == [first.client_order_id, second.client_order_id]
    terminal(second, at + 6 * h.SECOND)
    strategy.observe("9.80", at + 7 * h.SECOND)
    assert not working(strategy) and not strategy.cached.held
    assert len(strategy.sent) == 2
    assert records(strategy)[-1]["trigger_to_fill_ns"] == fill_at - RELEASE


def test_p4_rejected_cancel_keeps_one_sale_and_retries_at_the_chase_interval() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    event = OrderCancelRejected(
        first.trader_id,
        first.strategy_id,
        h.AAA,
        first.client_order_id,
        "synthetic cancel rejection",
        UUID4(),
        at,
        at,
        False,
    )
    first.apply(event)
    strategy.on_order_cancel_rejected(event)
    strategy.observe("9.80", at + CHASE - 1)
    assert strategy.canceled == [first.client_order_id]
    assert len(strategy.sent) == 1 and working(strategy) == [first]
    strategy.observe("9.80", at + CHASE)
    assert strategy.canceled == [first.client_order_id, first.client_order_id]
    assert len(strategy.sent) == 1 and working(strategy) == [first]


def test_p6_late_fill_with_a_remake_waiting_for_a_quote_still_finishes_its_record() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    terminal(first, at)
    strategy.cached.latest = None
    strategy.venue_quotes.clear()  # U-h prices from the venue clock; both sources are absent.
    strategy._on_step(cast(Any, None))
    assert len(strategy.sent) == 1
    fill_at = at + h.SECOND
    filled(strategy, first, 10, fill_at)
    strategy._on_step(cast(Any, None))
    assert not working(strategy) and not strategy.cached.held
    assert records(strategy)[-1]["trigger_to_fill_ns"] == fill_at - RELEASE


def test_floor_wait_record_is_kept_when_the_node_stops_before_a_fill() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("8.00", at)
    terminal(first, at)
    at += h.SECOND
    strategy.observe("8.00", at)
    at_floor = strategy.sent[-1]
    accepted(at_floor, at)
    strategy.observe("8.00", at)
    strategy.test_clock.set_time(at + WAIT // 2)
    strategy.on_stop()
    record = records(strategy)[-1]
    assert record["floor_reached"] is True
    assert record["floor_wait_ns"] == WAIT // 2
    assert record["trigger_to_fill_ns"] is None
    assert strategy.canceled[-1] == at_floor.client_order_id


def test_p5_recovery_restarts_the_continuous_floor_wait() -> None:
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("8.00", at)
    terminal(first, at)
    at += h.SECOND
    strategy.observe("8.00", at)
    floor_order = strategy.sent[-1]
    accepted(floor_order, at)
    strategy.observe("8.00", at)
    strategy.observe("10.00", at + WAIT - h.SECOND)
    strategy.observe("8.00", at + WAIT)
    strategy.observe("8.00", at + 2 * WAIT - 1)
    assert len(strategy.canceled) == 1, "the prior, interrupted wait cannot release the floor"
    strategy.observe("8.00", at + 2 * WAIT)
    assert strategy.canceled[-1] == floor_order.client_order_id


@pytest.mark.parametrize("hour,minute", [(3, 59), (9, 29), (15, 59), (19, 59)])
def test_p7_session_boundary_does_not_reset_the_sale_to_a_trail(hour, minute) -> None:
    at = (
        int(
            datetime(
                2026, 11, 12, hour, minute, 58, tzinfo=ZoneInfo("America/New_York")
            ).timestamp()
        )
        * h.SECOND
    )
    strategy, first = released(at)
    terminal(first, at + h.SECOND, expired=True)
    strategy.observe("9.80", at + 3 * h.SECOND)
    sale = strategy.sent[-1]
    assert sale.order_type == OrderType.LIMIT and sale.is_reduce_only
    assert sale.time_in_force == TimeInForce.DAY
    assert strategy.params[-1] == {"extended_hours": True}
    assert sale.quantity == Quantity.from_int(10)


def test_p8_foreign_sell_freezes_by_name_and_only_our_id_is_canceled() -> None:
    strategy, first = released()
    foreign = h.working_sell("owner-foreign", qty="2", filled="0")
    strategy.cached.working.append(foreign)
    strategy.observe("9.80", RELEASE + CHASE + h.SECOND)
    assert strategy.canceled == [first.client_order_id]
    assert foreign.client_order_id not in strategy.canceled
    assert "foreign" in strategy.frozen[h.AAA]
    terminal(first, RELEASE + CHASE + 2 * h.SECOND)
    strategy.observe("9.70", RELEASE + 3 * CHASE)
    assert len(strategy.sent) == 1
    assert "foreign" in strategy.frozen[h.AAA]


def test_p9_an_unreleased_trail_is_not_chased() -> None:
    strategy = h.Harness()
    strategy.adopt_holding(h.AAA, Price.from_str("9.90"))
    h.new_quote(strategy)
    first = strategy.sent[0]
    strategy.observe("10.01", RELEASE + CHASE + WAIT)
    assert not strategy.canceled and strategy.sent == [first]
    assert first.order_type == OrderType.TRAILING_STOP_LIMIT
    assert not records(strategy)


def test_expired_sale_with_no_quote_names_the_stall_on_every_step() -> None:
    strategy, first = released()
    terminal(first, RELEASE + h.SECOND, expired=True)
    strategy.cached.latest = None
    strategy.venue_quotes.clear()  # Removing a cache arrival alone no longer removes the quote.
    for _ in range(2):
        before = len(strategy.said)
        strategy._on_step(cast(Any, None))
        assert len(strategy.sent) == 1
        assert any("sale" in line and "quote" in line for line in strategy.said[before:])


def test_real_core_chases_a_gapped_release_then_fills_the_external_long() -> None:
    """A synthetic adapter accepts the resting release, acknowledges its cancel, then fills
    only a marketable replacement; the execution engine alone updates the EXTERNAL position."""
    gc.collect()
    clients = []

    class Data(h.SyntheticData):
        async def publish_tape(self):
            for bid in [*(["10.00"] * 4), *(["9.70"] * 80)]:
                self._handle_data(h.quote(bid, self.clock.timestamp_ns()))
                await asyncio.sleep(0.1)

    class Execution(h.SyntheticExecution):
        async def _connect(self):
            await super()._connect()
            self.sells = []
            self.cancels = []

        async def _submit_order(self, command):
            order = command.order
            if order.side == OrderSide.BUY:
                await super()._submit_order(command)
                return
            tick = self.cache.quote(h.AAA)
            assert tick is not None
            assert order.order_type == OrderType.LIMIT
            assert order.time_in_force == TimeInForce.DAY and order.is_reduce_only
            assert command.params == {"extended_hours": True}
            self.sells.append(order)
            venue_id = VenueOrderId(str(order.client_order_id))
            self.generate_order_submitted(order)
            self.generate_order_accepted(order, venue_id, self.clock.timestamp_ns())
            if order.price <= tick.bid_price:
                self.generate_order_filled(
                    order,
                    venue_id,
                    None,
                    TradeId(f"T-{len(self.sells)}"),  # a TradeId holds at most 36 characters
                    order.quantity,
                    tick.bid_price,
                    h.USD,
                    h.Money.from_str("0 USD"),
                    h.LiquiditySide.TAKER,
                    self.clock.timestamp_ns(),
                )

        async def _cancel_order(self, command):
            order = self.cache.order(command.client_order_id)
            assert order is not None and order in self.sells
            self.cancels.append(order.client_order_id)
            self.generate_order_canceled(order, order.venue_order_id, self.clock.timestamp_ns())

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
                account_id=AccountId("ALPACA-001"),
                account_type=h.AccountType.CASH,
                oms_type=h.OmsType.NETTING,
            )
            clients.append(client)
            return client

    node = h.LiveNode.build(
        "OWNER-CHASE-TEST",
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
    protector = h.SimulatedProtector("9.90")
    protector.stop_on_result = True
    node.add_strategy(h.Owner())
    node.add_strategy(protector)

    async def run():
        async with asyncio.timeout(9):
            await node.run_async()

    try:
        asyncio.run(run())
        assert not protector.refusals
        assert len(clients[0].sells) == 2
        assert clients[0].cancels == [clients[0].sells[0].client_order_id]
        assert len(protector.fills) == 1
        assert not node.cache.positions_open(strategy_id=h.EXTERNAL)
        assert not node.cache.positions(strategy_id=protector.strategy_id)
    finally:
        node.dispose()


def refused(order: Any, at: int) -> None:
    order.apply(
        OrderSubmitted(
            order.trader_id,
            order.strategy_id,
            h.AAA,
            order.client_order_id,
            AccountId("ALPACA-001"),
            UUID4(),
            at,
            at,
        )
    )
    order.apply(
        OrderRejected(
            order.trader_id,
            order.strategy_id,
            h.AAA,
            order.client_order_id,
            AccountId("ALPACA-001"),
            "refused by the venue",
            UUID4(),
            at,
            at,
            False,
        )
    )


def test_review1_a_refused_leg_is_sent_again_after_the_chase_interval() -> None:
    """Review finding 1: a refused leg left the triggered long with no sale for good. Main's chase
    sends a refused sale again every chase_secs (mover.py:4002-4007)."""
    strategy, first = released()
    floor = chase_floor(first.price.as_decimal(), ROWS.chase_cap)
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    terminal(first, at)
    at += h.SECOND
    strategy.observe("9.80", at)
    second = strategy.sent[-1]
    refused(second, at)
    strategy.observe("9.80", at + h.SECOND)
    assert strategy.sent[-1] is second, "a refused leg is sent again at the chase pace, not at once"
    strategy.observe("9.80", at + CHASE)
    third = strategy.sent[-1]
    assert third is not second, "the refused sale was never sent again"
    assert third.order_type == OrderType.LIMIT and third.is_reduce_only
    assert third.time_in_force == TimeInForce.DAY
    assert third.price.as_decimal() == expected_price("9.80", floor)
    assert strategy.params[-1] == {"extended_hours": True}


def test_review2_the_floor_wait_runs_with_the_bid_at_the_floor() -> None:
    """Review finding 2: main starts the floor wait when the exit price is through the floor
    (mover.py:4695), so a sale resting at the floor with the bid at the floor is released too."""
    strategy, first = released()
    floor = chase_floor(first.price.as_decimal(), ROWS.chase_cap)
    bid = str(floor)
    at = RELEASE + CHASE + h.SECOND
    strategy.observe(bid, at)
    terminal(first, at)
    at += h.SECOND
    strategy.observe(bid, at)
    at_floor = strategy.sent[-1]
    assert at_floor.price.as_decimal() == floor
    accepted(at_floor, at)
    for step in range(1, FLOOR_WAIT_SECS + 2 * ROWS.chase_secs + 1):
        strategy.observe(bid, at + step * h.SECOND)
    assert strategy.canceled[-1] == at_floor.client_order_id, "the floor never released"


def test_review3_a_resend_at_the_floor_keeps_the_running_wait() -> None:
    """Review finding 3: main keeps floor_since across legs, so an expiry or a resize at the floor
    does not restart the 60 s wait."""
    strategy, first = released()
    floor = chase_floor(first.price.as_decimal(), ROWS.chase_cap)
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("8.00", at)
    terminal(first, at)
    at += h.SECOND
    strategy.observe("8.00", at)
    at_floor = strategy.sent[-1]
    accepted(at_floor, at)
    strategy.observe("8.00", at)
    expired_at = at + 50 * h.SECOND
    terminal(at_floor, expired_at, expired=True)
    strategy.observe("8.00", expired_at + h.SECOND)
    again = strategy.sent[-1]
    assert again is not at_floor and again.price.as_decimal() == floor
    accepted(again, expired_at + h.SECOND)
    strategy.observe("8.00", at + WAIT + h.SECOND)
    assert strategy.canceled[-1] == again.client_order_id, "the re-send restarted the floor wait"


def test_review10_a_sale_ended_by_the_owner_logs_its_final_record() -> None:
    """Review finding 10: a sale whose long leaves without our fill still logs its record."""
    strategy, first = released()
    at = RELEASE + CHASE + h.SECOND
    strategy.observe("9.80", at)
    terminal(first, at)
    strategy.observe("9.80", at + h.SECOND)
    leg = strategy.sent[-1]
    accepted(leg, at + h.SECOND)
    strategy.cached.held = []  # the owner sold the long by hand
    before = len(records(strategy))
    strategy.observe("9.80", at + 2 * h.SECOND)
    terminal(leg, at + 3 * h.SECOND)
    strategy.observe("9.80", at + 4 * h.SECOND)
    assert h.AAA not in strategy.slots
    assert len(records(strategy)) > before, "the ended sale left no final record"


def test_every_chase_leg_carries_the_protect_lineage_never_our_nodes() -> None:
    """A protective sale of the owner's shares is never one of our nodes' orders (ADR 0148): each
    re-sent leg is named by `_protect_id`, so `paper:close` freezes it rather than canceling it."""
    from canon_trading import custody

    strategy, first = released()
    strategy.observe("9.80", RELEASE + CHASE + h.SECOND)
    terminal(first, RELEASE + CHASE + h.SECOND)
    strategy.observe("9.80", RELEASE + CHASE + 2 * h.SECOND)
    legs = [order for order in strategy.sent if order.order_type == OrderType.LIMIT]
    assert legs, "the chase must re-send the sale"
    for leg in [strategy.sent[0], *legs]:
        assert custody.lineage(str(leg.client_order_id)) == custody.PROTECT, leg.client_order_id
