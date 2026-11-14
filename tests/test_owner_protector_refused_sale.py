"""U-c2 refusal, silence, gap and chase-cap contract on synthetic holdings.

All orders and holdings are synthetic. The level book is under pytest's temp
directory; native engines use only synthetic clients, with no external venue,
credential or real paper state.
"""

import asyncio
import gc
import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Self

import pytest
from nautilus_trader.model import OrderReleased
from nautilus_trader.risk import RiskEngineConfig

from canon_trading import sale_table
from canon_trading.mover import chase_floor
from canon_trading.start_level import LevelBook
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c

engine_checks = c.engine_checks


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the credential-environment fixture for this offline case."""


@pytest.fixture(autouse=True)
def isolated_level_books(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


def counted(**settings):
    settings.setdefault("confirm_gap_ms", 0)
    return c.counted(**settings)


def sent(strategy):
    strategy.observe("9.89", h.T0 + h.SECOND)
    strategy.observe("9.88", h.T0 + 2 * h.SECOND)
    assert len(strategy.sent) == 1


def alerts(strategy):
    return [line for line in strategy.said if line.startswith("ALERT:")]


def refuse(strategy, *, reason="synthetic venue refusal"):
    return strategy.deliver("Rejected", reason=reason, ts_event=strategy.clock.timestamp_ns())


def records(strategy):
    return [
        json.loads(line.split("sale record ", 1)[1])
        for line in strategy.said
        if "sale record " in line
    ]


def test_t1_refusal_told_survives_a_new_first_leg_then_acceptance_resets_it():
    strategy = counted()
    sent(strategy)
    refuse(strategy, reason="reason " + "x" * 300)
    assert c.row(strategy).row == "RW"
    assert len(alerts(strategy)) == 1
    assert "venue refused our sale" in alerts(strategy)[0]
    assert "REJECTED" in alerts(strategy)[0]
    assert "counted quote under the level after 60 s" in alerts(strategy)[0]
    assert str(strategy.sent[0].client_order_id) not in alerts(strategy)[0]
    strategy.observe("10.00", h.T0 + 3 * h.SECOND)
    assert c.row(strategy) == sale_table.State("G", 0)
    strategy.observe("9.89", h.T0 + 62 * h.SECOND)
    strategy.observe("9.88", h.T0 + 63 * h.SECOND)
    assert len(strategy.sent) == 2
    refuse(strategy)
    assert len(alerts(strategy)) == 1
    assert any("refusal 2" in line and "ALERT" not in line for line in strategy.said)
    strategy.observe("9.87", h.T0 + 123 * h.SECOND)
    strategy.deliver("Accepted")
    assert any("our sale was accepted after 2 refusals since " in line for line in strategy.said)
    refuse(strategy)
    assert len(alerts(strategy)) == 2


@pytest.mark.parametrize("quantity", [1, 10])
def test_t1_fill_also_clears_the_holdings_refusal(quantity):
    strategy = counted()
    sent(strategy)
    refuse(strategy)
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    strategy.deliver("Filled", quantity=quantity)
    slot = strategy.slots[h.AAA]
    assert not slot.refusal_told and slot.refusals == 0
    if quantity == 1:
        strategy.deliver("Accepted")  # Native PF -> Accepted makes Rejected reachable.
        refuse(strategy)
        assert len(alerts(strategy)) == 2


@pytest.mark.parametrize("crosses", [False, True])
def test_t2_interval_guards_waiting_and_a_recount_not_timers(crosses):
    strategy = counted()
    sent(strategy)
    refuse(strategy)
    assert c.row(strategy).row == "RW"
    if crosses:
        strategy.observe("10.00", h.T0 + 3 * h.SECOND)
        strategy.observe("9.89", h.T0 + 4 * h.SECOND)
    strategy.observe("9.88", h.T0 + 61 * h.SECOND)
    assert len(strategy.sent) == 1
    before = c.row(strategy)
    strategy.test_clock.set_time(h.T0 + 62 * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))
    assert c.row(strategy) == before and len(strategy.sent) == 1
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[-1].price.as_decimal() <= Decimal("9.87")


def test_t2_partial_fill_remainder_keeps_the_episode_floor_and_chases_after_acceptance():
    strategy = counted()
    sent(strategy)
    strategy.deliver("Accepted")
    strategy.deliver("Filled", quantity=3)
    strategy.deliver("Accepted")
    sale = strategy.slots[h.AAA].sale
    assert sale is not None
    floor = sale.floor
    refuse(strategy)
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    assert len(strategy.sent) == 2 and strategy.sent[-1].quantity.as_decimal() == 7
    assert strategy.slots[h.AAA].sale is sale and sale.floor == floor
    strategy.deliver("Accepted")
    strategy.observe("9.80", h.T0 + 122 * h.SECOND)
    assert c.row(strategy).row == "PCR" and len(strategy.canceled) == 1


def test_t2_reprice_ack_after_an_above_level_quote_still_sends_below_level():
    strategy = counted()
    sent(strategy)
    strategy.deliver("Accepted")
    strategy.observe("9.80", h.T0 + 62 * h.SECOND)
    assert c.row(strategy).row == "PCR"
    strategy.observe("10.50", h.T0 + 63 * h.SECOND)
    strategy.deliver("Canceled")
    assert len(strategy.sent) == 2
    c.assert_sale_below_level(strategy, strategy.sent[-1])


@pytest.mark.parametrize("change", ["shrink", "foreign", "whole", "empty"])
def test_t2_waiting_rereads_holdings_and_foreign_cover_before_any_send(change):
    strategy = counted()
    sent(strategy)
    refuse(strategy)
    assert c.row(strategy).row == "RW"
    held = strategy.cached.held[0]
    if change in {"shrink", "empty"}:
        held.signed_qty = Decimal(4 if change == "shrink" else 0)
        held.quantity = h.Quantity.from_int(int(held.signed_qty))
    else:
        strategy.cached.working.append(
            h.working_sell(qty="10" if change == "whole" else "6", filled="0")
        )
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    strategy.observe("9.86", h.T0 + 63 * h.SECOND)
    strategy.observe("9.85", h.T0 + 64 * h.SECOND)
    if change in {"whole", "empty"}:
        assert len(strategy.sent) == 1
    else:
        assert len(strategy.sent) == 2 and strategy.sent[-1].quantity.as_decimal() == 4


def test_t3_real_save_and_restart_from_waiting_retains_level_only(tmp_path):
    path = tmp_path / "levels.json"
    strategy = counted(account_label="example", level_book_path=path)
    strategy.level_book_path, strategy.run = path, "before"
    sent(strategy)
    refuse(strategy)
    strategy._save_levels()
    saved = LevelBook.read(path)["example", h.AAA]
    # Nothing of ours works at the venue in the waiting row, so the book must not say `armed`
    # (a reader takes that for a working sell): it is the entry counting gives, at zero.
    assert (saved.state, saved.count, saved.reason) == ("counting", 0, None)
    text = path.read_text()
    assert "refusal" not in text and "RW" not in text
    restarted = c.Counted(
        breach_count=2,
        confirm_gap_ms=0,
        account_label="example",
        level_book_path=path,
        trading=h.VenueOrders(),
    )
    restarted.test_clock.set_time(h.T0 + 180 * h.SECOND)
    restarted.cached.latest = h.quote("10.00", h.T0 + 180 * h.SECOND)
    restarted.on_start()
    slot = restarted.slots[h.AAA]
    assert slot.level is not None and saved.last_level is not None
    assert slot.level.as_decimal() >= saved.last_level
    assert c.row(restarted) == sale_table.State("G", 0)
    assert not slot.refusal_told and slot.refusals == 0
    next_level = LevelBook.read(path)["example", h.AAA].last_level
    assert next_level is not None and next_level >= saved.last_level
    restarted.observe("9.89", h.T0 + 181 * h.SECOND)
    restarted.observe("9.88", h.T0 + 182 * h.SECOND)
    refuse(restarted)
    assert len(alerts(restarted)) == 1
    restarted.on_stop()


def test_t4_record_and_end_carry_reason_first_event_time_and_count():
    strategy = counted()
    sent(strategy)
    first = h.T0 + h.SECOND  # Event time differs from receipt; the record uses the event.
    strategy.deliver("Rejected", reason="synthetic reason", ts_event=first)
    record = records(strategy)[-1]
    iso = datetime.fromtimestamp(first / h.SECOND, UTC).isoformat()
    assert (record["refusal_reason"], record["refusal_first"], record["refusals"]) == (
        "synthetic reason",
        iso,
        1,
    )
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    refuse(strategy, reason="second reason")
    assert records(strategy)[-1]["refusals"] == 2
    strategy.on_stop()
    end = next(line for line in strategy.said if ": END " in line)
    assert "refusals=2" in end and f"refusal_first={iso}" in end


def test_t4_acceptance_logs_cleared_record():
    strategy = counted()
    sent(strategy)
    refuse(strategy)
    strategy.observe("9.87", h.T0 + 62 * h.SECOND)
    strategy.deliver("Accepted")
    record = records(strategy)[-1]
    assert (record["refusal_reason"], record["refusal_first"], record["refusals"]) == (
        None,
        None,
        0,
    )


@pytest.mark.parametrize("bid", ["9.87", "10.00"])
def test_t5_unanswered_leg_is_told_once_on_quotes_never_on_timer(bid):
    strategy = counted()
    sent(strategy)
    strategy.deliver("Submitted")
    strategy.observe(bid, h.T0 + 61 * h.SECOND)
    assert not alerts(strategy)
    strategy.test_clock.set_time(h.T0 + 62 * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))
    assert not alerts(strategy)
    for second in (62, 122, 3602):
        strategy.observe(bid, h.T0 + second * h.SECOND)
    assert len(alerts(strategy)) == 1 and "no answer" in alerts(strategy)[0]
    assert c.row(strategy).row == "FL"
    assert len(strategy.sent) == 1 and not strategy.canceled


def test_t6_gap_is_required_by_name_above_one():
    with pytest.raises(ValueError, match="confirm_gap_ms.*required"):
        h.OwnerProtectorConfig(row="OWN1", breach_count=2)
    assert h.OwnerProtectorConfig(row="OWN1", breach_count=2, confirm_gap_ms=0).confirm_gap_ms == 0


def test_t6_a_quote_that_changes_no_count_writes_no_count_line():
    """A holding above its level all day would otherwise write one line for every quote."""
    strategy = counted(confirm_gap_ms=1000)
    for n in range(1, 6):
        strategy.observe("10.00", h.T0 + n * h.SECOND)
    assert not [line for line in strategy.said if "counted quote count=" in line]
    strategy.observe("9.89", h.T0 + 7 * h.SECOND)
    assert [line for line in strategy.said if "counted quote count=" in line] == [
        f"{h.AAA}: counted quote count=1; confirm_gap_ms=1000"
    ]


def test_t6_gap_is_in_arming_count_lines_and_sale_record():
    strategy = counted(confirm_gap_ms=1000)
    sent(strategy)
    lines = [line for line in strategy.said if "armed distance" in line or "count=" in line]
    assert len(lines) >= 3 and all("confirm_gap_ms=1000" in line for line in lines)
    strategy.deliver("Rejected")
    assert records(strategy)[-1]["confirm_gap_ms"] == 1000


@pytest.mark.parametrize(
    "distance,cap_source", [("0.40", "holding plan"), ("0.04", "registered row")]
)
def test_t7_counted_carried_floor_takes_the_larger_cap(tmp_path, distance, cap_source):
    path = tmp_path / "levels.json"
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "entries": [
                    h.restart_record(account_label="example", last_level="10.10", distance=distance)
                ],
            }
        )
    )
    strategy = c.Counted(
        breach_count=2,
        confirm_gap_ms=0,
        account_label="example",
        level_book_path=path,
        trading=h.VenueOrders(),
    )
    strategy.cached.latest = h.quote("10.00", h.T0 + 180 * h.SECOND)
    strategy.test_clock.set_time(h.T0 + 180 * h.SECOND)
    strategy.on_start()
    strategy.observe("10.00", h.T0 + 181 * h.SECOND)
    strategy.observe("9.99", h.T0 + 182 * h.SECOND)
    slot = strategy.slots[h.AAA]
    assert slot.distance is not None and slot.sale is not None
    cap = max(slot.distance.plan.chase_cap, h.protect.PLANNER.chase_cap)
    assert slot.sale.floor == chase_floor(strategy.sent[0].price.as_decimal(), cap)
    assert any(f"cap={cap}" in line and cap_source in line for line in strategy.said)


def test_t7_planned_counted_holding_uses_its_wide_trail_cap():
    strategy = c.Counted(breach_count=2, confirm_gap_ms=0)
    strategy.cached.history = h.minute_bars("1.00")
    strategy.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    sent(strategy)
    slot = strategy.slots[h.AAA]
    assert slot.distance is not None and slot.sale is not None
    cap = slot.distance.plan.chase_cap
    assert cap > h.protect.PLANNER.chase_cap
    assert slot.sale.floor == chase_floor(strategy.sent[0].price.as_decimal(), cap)


@pytest.mark.parametrize("answer", ["none", "Accepted", "Rejected"])
def test_t5_real_engine_unanswered_submit_then_late_answer(answer):
    """Gain lock ON; native cache/dispatch and synthetic venue, logical strategy clock only."""
    gc.collect()
    failures, completed, clients = [], [], []

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
            self.test_clock = h.Clock.new_test()
            self.said = []

        @property
        def clock(self):
            return self.test_clock

        def _say(self, text):
            self.said.append(text)

        def on_start(self):
            self.subscribe_quotes(h.AAA)

        def on_quote(self, quote):
            if h.AAA not in self.slots and self.cache.positions_open(
                instrument_id=h.AAA, strategy_id=h.EXTERNAL
            ):
                self.adopt_holding(h.AAA, h.Price.from_str("9.90"))
            super().on_quote(quote)

    probe = Probe()

    class Execution(h.SyntheticExecution):
        async def _submit_order(self, command):
            if command.order.side == h.OrderSide.BUY:
                await super()._submit_order(command)
            else:
                self.generate_order_submitted(command.order)
                # No answer whatsoever from the simulated venue until the tape says so.

        async def _cancel_order(self, command):
            order = self.cache.order(command.client_order_id)
            assert order is not None
            self.generate_order_canceled(order, order.venue_order_id, self.clock.timestamp_ns())

    class Data(h.SyntheticData):
        async def publish_tape(self):
            try:
                await self.walk()
            except Exception as exc:  # noqa: BLE001 - report the task failure to pytest.
                failures.append(exc)
            finally:
                probe.shutdown_system("synthetic silence finished")

        async def publish(self, bid, at):
            probe.test_clock.set_time(at)
            self._handle_data(h.quote(bid, at))
            await asyncio.sleep(0.02)

        async def walk(self):
            start = self.clock.timestamp_ns()
            for i, bid in enumerate(("10.00", "10.00", "9.89", "9.88")):
                await self.publish(bid, start + i * h.SECOND)
            orders = self.cache.orders(strategy_id=h.PROTECTOR)
            assert len(orders) == 1
            cid = orders[0].client_order_id
            sent_at = probe.clock.timestamp_ns()
            await self.publish("9.87", sent_at + 59 * h.SECOND)
            assert not alerts(probe)
            for seconds, bid in ((60, "10.00"), (61, "9.87"), (3600, "9.87")):
                await self.publish(bid, sent_at + seconds * h.SECOND)
                assert c.row(probe).row == "FL"
                assert len(self.cache.orders(strategy_id=h.PROTECTOR)) == 1
            assert len(alerts(probe)) == 1 and "no answer" in alerts(probe)[0]
            order = self.cache.order(cid)
            at = probe.clock.timestamp_ns()
            if answer == "Accepted":
                clients[0].generate_order_accepted(order, h.VenueOrderId(str(cid)), at)
                await asyncio.sleep(0.02)
                assert c.row(probe).row == "WK"
            elif answer == "Rejected":
                clients[0].generate_order_rejected(order, "synthetic refusal", at, False)
                await asyncio.sleep(0.02)
                assert c.row(probe).row == "RW" and len(alerts(probe)) == 2
                await self.publish("9.87", at + 59 * h.SECOND)
                assert len(self.cache.orders(strategy_id=h.PROTECTOR)) == 1
                await self.publish("9.86", at + 60 * h.SECOND)
                assert len(self.cache.orders(strategy_id=h.PROTECTOR)) == 2
            completed.append(answer)

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
        "REFUSED-SALE-TEST",
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
        assert not failures, failures
        assert completed == [answer]
    finally:
        node.dispose()


def run_backtest_probe(*, crash=False):
    """Gain lock ON; measure when the native risk denial arrives relative to submit_order."""
    gc.collect()

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
            self.inside_submit = False
            self.denials = []
            self.said = []
            self.did_raise = False

        def _say(self, text):
            self.said.append(text)

        def on_start(self):
            self.subscribe_quotes(h.AAA)

        def on_quote(self, quote):
            if crash and not self.did_raise:
                self.did_raise = True
                raise RuntimeError("deliberate handler failure for the shared log check")
            if h.AAA not in self.slots and self.cache.positions_open(
                instrument_id=h.AAA, strategy_id=h.EXTERNAL
            ):
                self.adopt_holding(h.AAA, h.Price.from_str("9.90"))
            super().on_quote(quote)

        def submit_order(self, order, *args, **kwargs):
            self.inside_submit = True
            try:
                return super().submit_order(order, *args, **kwargs)
            finally:
                self.inside_submit = False

        def on_order_event(self, event):
            if type(event).__name__ == "OrderDenied":
                self.denials.append((self.inside_submit, c.row(self).row))
            super().on_order_event(event)

    engine = h.BacktestEngine(
        h.BacktestEngineConfig(
            trader_id=h.TraderId("TESTER-001"),
            bypass_logging=False,
            risk_engine=RiskEngineConfig(max_notional_per_order={str(h.AAA): Decimal(50)}),
        )
    )
    engine.add_venue(
        h.Venue("ALPACA"),
        h.OmsType.NETTING,
        h.AccountType.CASH,
        [h.Money(100_000, h.USD)],
        fee_model=h.MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    engine.add_instrument(h.equity())
    probe = Probe()
    engine.add_strategy(h.Owner())
    engine.add_strategy(probe)
    engine.add_data(
        [
            h.quote(bid, h.T0 + second * h.SECOND)
            for second, bid in (
                (0, "1.00"),
                (1, "10.00"),
                (2, "9.89"),
                (3, "9.88"),
                (70, "9.87"),
            )
        ]
    )
    try:
        engine.run()
        orders = engine.cache.orders(strategy_id=h.PROTECTOR)
        return probe, orders
    finally:
        engine.dispose()


def test_t8_a_risk_denial_arrives_after_native_submit_returns_and_freezes_without_handler_error():
    # The pin routes a strategy's submit through a queued endpoint so that a risk denial is NOT
    # dispatched back into a strategy that is still borrowed (nautilus-trader d53849b216e2,
    # crates/risk/src/engine/mod.rs:145-166; crates/trading/src/strategy/mod.rs:2470; the backtest
    # engine installs the queue, crates/backtest/src/engine.rs:2181-2208). So the denial reaches
    # the handler after submit has returned, in the in-flight row. Pinned as observed: a pin that
    # delivers it inside the submit fails this case by name.
    probe, orders = run_backtest_probe()
    assert len(orders) == 1 and orders[0].status == h.OrderStatus.DENIED
    assert len(alerts(probe)) == 1
    assert c.row(probe).row in {"FZ", "T0"}  # Engine.run has stopped the strategy.
    assert probe.denials == [(False, "FL")]


def test_t8_shared_log_check_alone_catches_a_raised_handler(engine_checks):
    probe, _ = run_backtest_probe(crash=True)
    assert probe.did_raise
    with pytest.raises(AssertionError, match="engine handler errors:.*") as failure:
        engine_checks()
    assert str(failure.value).count("Python on_quote failed") == 1
    assert "Already mutably borrowed" not in str(failure.value)


def test_t2_every_send_price_assertion_rejects_an_old_price_above_a_lower_level():
    strategy = counted()
    order = SimpleNamespace(instrument_id=h.AAA, price=h.Price.from_str("10.00"))
    with pytest.raises(AssertionError, match="sale limit exceeds"):
        c.assert_sale_below_level(strategy, order)


def test_t7_count_one_carried_sale_floor_uses_its_plan_cap(tmp_path):
    """Same carried holding as the chase test of a carried level far above the bid."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [h.restart_record(last_level="1.10")]}))
    strategy = h.restart_harness(path)
    strategy.cached.latest = h.quote("1.00", h.T0 + 180 * h.SECOND)
    strategy.on_start()
    slot = strategy.slots[h.AAA]
    assert slot.distance is not None
    cap = slot.distance.plan.chase_cap
    assert cap == Decimal("0.04") > h.protect.PLANNER.chase_cap
    strategy.observe("1.00", h.T0 + 181 * h.SECOND)
    strategy.observe("0.99", h.T0 + 182 * h.SECOND)
    trailing = strategy.sent[0]
    at = h.T0 + 183 * h.SECOND
    strategy.test_clock.set_time(at)
    first = trailing.trigger_price.as_decimal() - Decimal(str(trailing.limit_offset))
    order = strategy.order_factory.limit(
        h.AAA,
        h.OrderSide.SELL,
        trailing.quantity,
        h.Price.from_str(str(first)),
        time_in_force=h.TimeInForce.DAY,
        reduce_only=True,
        client_order_id=trailing.client_order_id,
    )
    released = OrderReleased(
        order.trader_id,
        order.strategy_id,
        h.AAA,
        order.client_order_id,
        h.Price.from_str("0.99"),
        h.UUID4(),
        at,
        at,
    )
    order.apply(released)
    strategy.cached.working = [order]
    strategy.on_order_released(released)
    assert slot.sale is not None
    assert slot.sale.floor == chase_floor(first, cap)
