"""Gain-lock requirements: synthetic holdings and the pinned native emulator, no broker I/O.

The command harness applies native OrderEmulated/OrderUpdated events (nt@d53849b216e2,
model/__init__.pyi:4283-4293,5048-5067). The replay exercises the real emulation path; the
cash matcher's known EXTERNAL inventory limitation is observed as release, not a claimed fill.
No paper state, catalog, ledger, credential or external file is read or written.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderEmulated
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import OrderUpdated
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue

from tests.test_owner_protector import AAA
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import USD
from tests.test_owner_protector import Harness
from tests.test_owner_protector import Owner
from tests.test_owner_protector import SimulatedProtector
from tests.test_owner_protector import equity
from tests.test_owner_protector import minute_bars
from tests.test_owner_protector import new_quote
from tests.test_owner_protector import quote
from tests.test_owner_protector import working_sell


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared credential-environment reader for these offline cases."""


class GainHarness(Harness):
    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        super().submit_order(order, position_id, client_id, params)
        order.apply(
            OrderEmulated(
                order.trader_id,
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                UUID4(),
                self.clock.timestamp_ns(),
                self.clock.timestamp_ns(),
            )
        )

    def modify_order(
        self,
        client_order_id,
        quantity=None,
        price=None,
        trigger_price=None,
        client_id=None,
        params=None,
    ) -> None:
        order = self.cache.order(client_order_id)
        assert order is not None and order.is_emulated
        assert quantity is None, "gain locking must never change the quantity"
        order.apply(
            OrderUpdated(
                order.trader_id,
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                order.quantity,
                UUID4(),
                self.clock.timestamp_ns(),
                self.clock.timestamp_ns(),
                False,
                price=price if price is not None else order.price,
                trigger_price=trigger_price if trigger_price is not None else order.trigger_price,
            )
        )


def holding(entry: Any = Decimal("10.00"), bid: str = "10.00") -> GainHarness:
    strategy = GainHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = entry
    strategy.cached.latest = quote(bid)
    strategy.cached.history = minute_bars("0.20")
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    new_quote(strategy, "10.00")  # the first received quote, under the arming point
    assert strategy.sent[0].trailing_offset == Decimal("0.20")
    return strategy


def records(strategy) -> list[dict[str, Any]]:
    return getattr(strategy, "gain_lock_events", [])


def observed_quote(
    strategy,
    *,
    bid="10.10",
    ask="10.12",
    event_at=T0 + SECOND,
    received_at=T0 + SECOND,
    now=T0 + SECOND,
) -> None:
    strategy.test_clock.set_time(now)
    tick = QuoteTick(
        AAA,
        Price.from_str(bid),
        Price.from_str(ask),
        Quantity.from_int(100),
        Quantity.from_int(100),
        event_at,
        received_at,
    )
    strategy.cached.latest = tick
    strategy.on_quote(tick)


def test_f1_higher_average_defers_unexecutable_floor_and_keeps_working_stop() -> None:
    strategy = holding()
    strategy.observe("10.10", T0 + SECOND)
    order = strategy.sent[0]
    previous = (order.trigger_price, order.price)
    strategy.cached.held[0].avg_px_open = Decimal("11.00")
    strategy.observe("10.10", T0 + 2 * SECOND)
    assert (order.trigger_price, order.price) == previous, (
        "a floor above the bid must wait, keeping the existing protective stop"
    )
    assert order.is_emulated and not strategy.canceled
    assert any("floor deferred" in line and "bid" in line for line in strategy.said)
    assert strategy.slots[AAA].gain_floor == Price.from_str("11.00")
    # A lower average while waiting cannot undo the raised latch.
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.observe("11.20", T0 + 3 * SECOND)
    assert order.price == Price.from_str("11.00")
    assert order.trigger_price == Price.from_str("11.05")


def test_f2_floor_keeps_configured_spacing_between_trigger_and_limit() -> None:
    strategy = holding()
    strategy.observe("10.10", T0 + SECOND)
    order = strategy.sent[0]
    # This registered row plans a 0.20 trail and a 0.25 limit offset: 0.05 spacing.
    assert order.trailing_offset == Decimal("0.20")
    assert order.limit_offset == Decimal("0.25")
    assert order.price == Price.from_str("10.00")
    assert order.trigger_price == Price.from_str("10.05")


@pytest.mark.parametrize("ask", ["10.00", "9.99"], ids=["locked", "crossed"])
def test_f3_nonpositive_spread_waits_named_then_fixes_positive_cost(ask) -> None:
    strategy = holding()
    observed_quote(strategy, bid="10.00", ask=ask)
    assert records(strategy) == [], "a quote with no positive spread never arms the lock"
    assert strategy.sent[0].trigger_price == Price.from_str("9.80")
    assert any("positive spread" in line for line in strategy.said)
    strategy.observe("10.10", T0 + 2 * SECOND)
    assert records(strategy)[0]["round_trip_cost"] == "0"
    assert records(strategy)[0]["noise_floor"] == "0.04"


@pytest.mark.parametrize("ahead", [5_000_000, 100_000_000], ids=["5ms", "bound-100ms"])
@pytest.mark.parametrize("stamp", ["event", "receipt", "both"])
def test_f4_small_forward_clock_skew_can_arm(stamp, ahead) -> None:
    strategy = holding()
    at = T0 + SECOND
    observed_quote(
        strategy,
        event_at=at + (ahead if stamp in ("event", "both") else 0),
        received_at=at + (ahead if stamp in ("receipt", "both") else 0),
    )
    assert records(strategy), "a bounded forward timestamp must not be treated as unknown"
    assert records(strategy)[0]["event"] == "armed"
    assert records(strategy)[0]["round_trip_cost"] == "0"


@pytest.mark.parametrize("stamp", ["event", "receipt", "both"])
@pytest.mark.parametrize("ahead", [100_000_001, 120 * SECOND], ids=["past-bound", "120s"])
def test_f4_large_forward_skew_is_named_and_does_not_poison_later_quotes(stamp, ahead) -> None:
    """Moved by C0's venue clock (the lead, 2026-11-13 17:36): the clock is the running maximum of
    sane stamps and a quote counts only if it raises it, so with both stamps ahead the next quote
    that can arm is the first stamped after that maximum."""
    strategy = holding()
    at = T0 + SECOND
    observed_quote(
        strategy,
        event_at=at + (ahead if stamp in ("event", "both") else 0),
        received_at=at + (ahead if stamp in ("receipt", "both") else 0),
    )
    assert records(strategy) == []
    assert any("GAIN_QUOTE_FORWARD_SKEW" in line for line in strategy.said)
    # C0's venue clock (the lead's definition, 2026-11-13 17:36) is the running maximum of sane
    # stamps, and a quote counts only if it raises it. With both stamps ahead the quote is sane and
    # sets that maximum (a receipt is the node's own clock, so this input cannot arise live); the
    # next quote that counts is the first stamped after it.
    later = at + SECOND + (ahead if stamp == "both" else 0)
    strategy.observe("10.10", later)
    assert records(strategy), "an invalid future stamp must not become the observation watermark"


def test_f4_invalid_cached_future_quote_does_not_poison_startup() -> None:
    """Moved by C0's venue clock (the lead, 2026-11-13 17:36): with no maximum yet, the first sane
    quote establishes the clock and does not count, so the second newer quote arms."""
    strategy = GainHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    # Stamped 120 s ahead of its receipt; a receipt is the node's own clock, never ahead of it.
    ahead = quote("10.00", T0 + 120 * SECOND)
    strategy.cached.latest = QuoteTick(
        AAA, ahead.bid_price, ahead.ask_price, ahead.bid_size, ahead.ask_size, ahead.ts_event, T0
    )
    strategy.cached.history = minute_bars("0.20")
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    assert records(strategy) == []
    # Under C0's venue clock the refused future stamp sets nothing, the first sane quote establishes
    # the clock and does not count, and the next newer one arms.
    strategy.observe("10.10", T0 + SECOND)
    assert records(strategy) == []
    strategy.observe("10.10", T0 + 2 * SECOND)
    assert records(strategy), "startup must not retain an invalid future observation watermark"


@pytest.mark.parametrize("event_at", [T0 - SECOND, T0], ids=["backlog", "start-boundary"])
def test_f5_backlog_received_after_start_never_arms(event_at) -> None:
    strategy = GainHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.cached.latest = quote("10.00", T0 - 30 * SECOND)
    strategy.cached.history = minute_bars("0.20")
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    observed_quote(strategy, event_at=event_at)
    assert records(strategy) == [], "receipt after start cannot make a pre-start event fresh"
    assert strategy.sent[0].is_emulated
    assert strategy.sent[0].trigger_price == Price.from_str("9.80")
    assert any("protection began" in line for line in strategy.said)
    strategy.observe("10.10", T0 + 2 * SECOND)
    assert records(strategy)[0]["event"] == "armed"


def test_f5_post_start_quote_still_must_meet_quote_age_bound() -> None:
    strategy = holding()
    observed_quote(
        strategy,
        event_at=T0 + SECOND,
        received_at=T0 + 62 * SECOND,
        now=T0 + 62 * SECOND,
    )
    assert records(strategy) == []
    assert any("stale" in line for line in strategy.said)
    assert strategy.sent[0].is_emulated and not strategy.canceled


@contextmanager
def native_gain_replay(owner=None):
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("TESTER-001"), bypass_logging=True)
    )
    engine.add_venue(
        Venue("ALPACA"),
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    engine.add_instrument(equity())
    protector = SimulatedProtector("9.80")
    protector.settings.gain_lock = True  # the lock is opt-in (the lead, 2026-11-13 22:47)
    engine.add_strategy(Owner() if owner is None else owner)
    engine.add_strategy(protector)
    engine.add_data(sorted(minute_bars("0.20"), key=lambda bar: bar.ts_init))
    engine.add_data([quote("9.98", T0), quote("10.00", T0 + SECOND)])
    try:
        engine.run(streaming=True)
        engine.clear_data()
        engine.add_data([quote("10.10", T0 + 2 * SECOND)])
        engine.run(streaming=True)
        yield engine, protector
    finally:
        engine.end()
        engine.dispose()


class RebuyingOwner(Owner):
    def on_quote(self, quote) -> None:
        super().on_quote(quote)
        if quote.ts_init == T0 + 3 * SECOND:
            self.submit_order(self.order_factory.market(AAA, OrderSide.BUY, Quantity.from_int(10)))


def test_f1_native_average_raise_does_not_release_unfillable_limit_on_resize() -> None:
    with native_gain_replay(RebuyingOwner()) as (engine, protector):
        original_id = protector.slots[AAA].protect_id
        engine.clear_data()
        at = T0 + 3 * SECOND
        engine.add_data(
            [
                QuoteTick(
                    AAA,
                    Price.from_str("10.10"),
                    Price.from_str("12.00"),
                    Quantity.from_int(100),
                    Quantity.from_int(100),
                    at,
                    at,
                ),
                quote("10.10", T0 + 4 * SECOND),
            ]
        )
        engine.run(streaming=True)
        positions = engine.cache.positions_open(instrument_id=AAA)
        assert len(positions) == 1 and positions[0].avg_px_open == Decimal("11.00")
        original = engine.cache.order(original_id)
        assert original is not None
        assert not any(type(event).__name__ == "OrderReleased" for event in original.events())
        order = engine.cache.order(protector.slots[AAA].protect_id)
        assert order is not None and order.is_emulated
        assert order.quantity == Quantity.from_int(20)
        assert order.price == Price.from_str("10.00")
        assert order.trigger_price == Price.from_str("10.05")


def test_f2_native_gap_through_trigger_releases_marketable_floor_limit() -> None:
    with native_gain_replay() as (engine, protector):
        protect_id = protector.slots[AAA].protect_id
        engine.clear_data()
        engine.add_data([quote("10.04", T0 + 3 * SECOND)])
        engine.run(streaming=True)
        order = engine.cache.order(protect_id)
        assert order is not None
        assert any(type(event).__name__ == "OrderReleased" for event in order.events())
        assert order.price <= Price.from_str("10.04"), "the released sale must reach the bid"


def test_r3_gap_below_cost_floor_still_releases_the_whole_sale_in_one_step() -> None:
    """The exit beats the lock (the lead's R3, 2026-11-13): the gain lock moves the trigger up and
    never withholds the sale. A gap through the floor releases a SELL limit for every protected
    share on that quote, its first limit at the applied floor; re-pricing it down through the
    floor is the chase's (unit U-d), not the lock's. The cash matcher rejects the released
    EXTERNAL sale (see the module notes), so the release and its submit are asserted, not a fill."""
    with native_gain_replay() as (engine, protector):
        protect_id = protector.slots[AAA].protect_id
        engine.clear_data()
        engine.add_data([quote("9.99", T0 + 3 * SECOND)])
        engine.run(streaming=True)
        order = engine.cache.order(protect_id)
        assert order is not None
        names = [type(event).__name__ for event in order.events()]
        assert "OrderReleased" in names and "OrderSubmitted" in names
        assert order.side == OrderSide.SELL and order.order_type == OrderType.LIMIT
        assert order.quantity == Quantity.from_int(10), "every protected share is offered"
        assert order.price == Price.from_str("10.00"), "first limit at the applied floor"


def test_arms_at_entry_plus_cost_plus_noise_only_on_a_post_protection_quote() -> None:
    # Removing the fresh-quote gate or using > instead of >= breaks this boundary. On paper the
    # cost is 0 (fees, ADR 0147), so a 0.02 spread arms at entry 10.00 + noise 0.04 = 10.04.
    strategy = holding(bid="10.06")
    assert records(strategy) == []
    strategy.observe("10.08", T0)  # Same observation cannot confirm the cached high.
    strategy._manage(AAA)  # Nor can a timer, even with a qualifying cached bid.
    assert strategy.sent[0].trigger_price == Price.from_str("9.80")
    strategy.observe("10.03", T0 + SECOND)
    assert records(strategy) == []
    strategy.observe("10.06", T0 + 2 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("10.05")
    assert [event["event"] for event in records(strategy)] == ["armed"]
    strategy.observe("9.90", T0 + 3 * SECOND)
    strategy.cached.held[0].avg_px_open = None
    strategy.cached.latest = None
    strategy._manage(AAA)
    assert strategy.sent[0].trigger_price == Price.from_str("10.05")
    assert len(records(strategy)) == 1, "arming is latched across falling or unknown inputs"


def test_cost_and_noise_are_measured_at_the_arming_quote() -> None:
    strategy = holding()
    at = T0 + SECOND
    strategy.test_clock.set_time(at)
    wide = QuoteTick(
        AAA,
        Price.from_str("10.07"),
        Price.from_str("10.11"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        at,
        at,
    )
    strategy.cached.latest = wide
    strategy.on_quote(wide)  # threshold 10.00 + 0 + 2 x 0.04 = 10.08
    assert records(strategy) == []
    strategy.observe("10.06", at + SECOND)  # narrow spread now qualifies
    assert strategy.sent[0].trigger_price == Price.from_str("10.05")
    assert records(strategy)[0]["round_trip_cost"] == "0"
    assert records(strategy)[0]["noise_floor"] == "0.04"


def test_gain_floor_keeps_the_higher_trail_and_survives_resize_and_expiry() -> None:
    strategy = holding()
    strategy.observe("10.06", T0 + SECOND)
    first = strategy.sent[0]
    assert first.trigger_price == Price.from_str("10.05")
    # Simulate the native trail reaching a higher high before a resize acknowledgement.
    strategy.modify_order(first.client_order_id, trigger_price=Price.from_str("10.30"))
    strategy.observe("10.50", T0 + 2 * SECOND)
    assert first.trigger_price == Price.from_str("10.30")
    strategy.cached.held[0].signed_qty = Decimal(6)
    strategy.observe("10.40", T0 + 3 * SECOND)
    assert strategy.canceled == [first.client_order_id]
    assert len(strategy.sent) == 1
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=Price.from_str("10.30"),
        )
    ]
    strategy.observe("10.35", T0 + 4 * SECOND)
    second = strategy.sent[-1]
    assert second.quantity == Quantity.from_int(6)
    assert second.trigger_price == Price.from_str("10.30")
    strategy.cached.working = [
        working_sell(
            str(second.client_order_id),
            qty="6",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.EXPIRED,
            is_closed=True,
            trigger_price=Price.from_str("10.30"),
        )
    ]
    strategy.observe("10.20", T0 + 5 * SECOND)
    assert len(strategy.sent) == 3
    assert strategy.sent[-1].trigger_price == Price.from_str("10.30")
    assert strategy.sent[-1].price >= Price.from_str("10.00")


@pytest.mark.parametrize("entry", [None, Decimal(0), Decimal(-1), Decimal("NaN"), "unknown"])
def test_unknown_cost_basis_names_unarmed_every_step_and_keeps_plain_protection(entry) -> None:
    strategy = holding(entry)
    for second in (1, 2, 3):
        before = len(strategy.said)
        strategy.observe("10.50", T0 + second * SECOND)
        assert strategy.sent[0].trigger_price == Price.from_str("9.80")
        assert not strategy.canceled
        assert any(
            "gain lock unarmed" in line and "cost basis" in line for line in strategy.said[before:]
        )
    assert records(strategy) == []


@pytest.mark.parametrize("unknown", ["missing", "stale", "crossed", "empty"])
def test_unknown_quote_names_unarmed_every_step_and_keeps_plain_protection(unknown) -> None:
    strategy = holding()
    for second in (1, 2, 3):
        before = len(strategy.said)
        at = T0 + (60 + second) * SECOND
        strategy.test_clock.set_time(at)
        tick = (
            None
            if unknown == "missing"
            else QuoteTick(
                AAA,
                Price.from_str("10.50"),
                Price.from_str("10.49" if unknown == "crossed" else "10.52"),
                Quantity.from_int(0 if unknown == "empty" else 100),
                Quantity.from_int(100),
                T0 if unknown == "stale" else at,
                at,
            )
        )
        strategy.cached.latest = tick
        strategy._manage(AAA, tick)
        assert strategy.sent[0].trigger_price == Price.from_str("9.80")
        assert not strategy.canceled
        assert any(
            "gain lock unarmed" in line and "quote" in line for line in strategy.said[before:]
        )
    assert records(strategy) == []


def test_arming_and_each_floor_raise_are_logged_and_recorded_with_the_inputs() -> None:
    strategy = holding()
    strategy.observe("10.06", T0 + SECOND)
    strategy.cached.held[0].avg_px_open = Decimal("10.03")
    strategy.observe("10.09", T0 + 2 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("10.08")
    strategy.cached.held[0].avg_px_open = Decimal("9.90")
    strategy.observe("10.06", T0 + 3 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("10.08")
    events = records(strategy)
    assert [event["event"] for event in events] == ["armed", "raised"]
    assert [event["entry"] for event in events] == ["10.00", "10.03"]
    assert [event["floor"] for event in events] == ["10.00", "10.03"]
    for event in events:
        assert event["round_trip_cost"] == "0"
        assert event["noise_floor"] == "0.04"
        assert event["armed_at"] == T0 + SECOND
        assert any(
            "gain lock" in line
            and event["event"] in line
            and f"entry={event['entry']}" in line
            and "round_trip_cost=0," in line
            and "noise_floor=0.04" in line
            and f"armed_at={T0 + SECOND}" in line
            for line in strategy.said
        )


@pytest.mark.parametrize("high,release", [("10.10", "10.05"), ("10.03", "9.83")])
def test_todays_shape_releases_at_cost_floor_and_below_threshold_keeps_plain_trail(high, release):
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("TESTER-001"), bypass_logging=True)
    )
    engine.add_venue(
        Venue("ALPACA"),
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    engine.add_instrument(equity())
    protector = SimulatedProtector("9.80")
    protector.settings.gain_lock = True  # the lock is opt-in (the lead, 2026-11-13 22:47)
    engine.add_strategy(Owner())
    engine.add_strategy(protector)
    # Entry ask is 10.00, and the warm ATR fixes the trail at 0.20.
    engine.add_data(sorted(minute_bars("0.20"), key=lambda bar: bar.ts_init))
    engine.add_data([quote("9.98", T0), quote("10.00", T0 + SECOND)])
    try:
        engine.run(streaming=True)
        distance = protector.slots[AAA].distance
        assert distance is not None and distance.distance == Decimal("0.20")
        engine.clear_data()
        engine.add_data([quote(high, T0 + 2 * SECOND), quote(high, T0 + 3 * SECOND)])
        engine.run(streaming=True)
        protect_id = protector.slots[AAA].protect_id
        assert protect_id is not None
        order = engine.cache.order(protect_id)
        assert order is not None and order.is_emulated
        assert order.trigger_price == Price.from_str(release)
        if high == "10.10":
            assert order.price >= Price.from_str("10.00")
        engine.clear_data()
        engine.add_data([quote(release, T0 + 4 * SECOND)])
        engine.run(streaming=True)
        ended = engine.cache.order(order.client_order_id)
        assert ended is not None
        assert any(type(event).__name__ == "OrderReleased" for event in ended.events())
        # The ordinary trail for 10.10 would still wait until 9.90; the lock releases earlier.
        if high == "10.10":
            assert Decimal(records(protector)[0]["entry"]) == Decimal("10.00")
        else:
            assert records(protector) == [], "the below-threshold guard must stay unarmed"
    finally:
        engine.end()
        engine.dispose()


# The U-b review's findings (the owning lane's fix-forward, 2026-11-13), each red before its fix.


@pytest.mark.parametrize("bid", ["10.04", "10.05", "10.06", "10.07"])
def test_review4_arming_never_puts_the_trigger_at_or_over_the_bid(bid) -> None:
    """The emulator matches a modified trigger at once (emulator.rs:814-928): a lock whose trigger
    is at or over the bid would sell on arming. The floor waits, named, until the bid clears it."""
    strategy = holding()
    observed_quote(strategy, bid=bid, ask=str(Decimal(bid) + Decimal("0.02")))
    trigger = strategy.sent[0].trigger_price
    assert trigger.as_decimal() < Decimal(bid), f"trigger {trigger} at or over the bid {bid}"
    # Entry 10.00, cost 0, noise 2 x 0.02: 10.04 arms by the threshold, but the floor 10.00 sets the
    # trigger 10.05, so the lock arms only on a bid above 10.05, never armed-but-unapplied.
    if Decimal(bid) <= Decimal("10.05"):
        assert records(strategy) == [], "armed with its floor unapplied"
        assert any("gain lock waits" in line for line in strategy.said)
    else:
        (armed,) = records(strategy)
        assert armed["applied_floor"] == "10.00" and trigger == Price.from_str("10.05")


def test_review5_a_redelivered_quote_from_before_the_arming_never_arms() -> None:
    strategy = GainHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.cached.history = minute_bars("0.20")
    # Received 10 ms before adoption, stamped 80 ms after it (the venue clock ahead).
    early = QuoteTick(
        AAA,
        Price.from_str("10.10"),
        Price.from_str("10.12"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        T0 + 80_000_000,
        T0 - 10_000_000,
    )
    strategy.cached.latest = early
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    armed = strategy.slots[AAA].armed_at
    assert armed is not None
    observed_quote(
        strategy,
        bid="10.10",
        ask="10.12",
        event_at=T0 + 80_000_000,
        received_at=armed + 1,
        now=armed + 1,
    )
    assert records(strategy) == [], "the same observation, re-delivered, armed the lock"


class CountingHarness(GainHarness):
    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self.modified: list[Any] = []

    def modify_order(self, client_order_id, *args, **kwargs) -> None:
        self.modified.append(client_order_id)
        super().modify_order(client_order_id, *args, **kwargs)


def test_review6_no_modify_in_the_step_that_cancels_for_size() -> None:
    strategy = CountingHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.cached.latest = quote("10.00")
    strategy.cached.history = minute_bars("0.20")
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    new_quote(strategy, "10.00")
    strategy.observe("10.10", T0 + SECOND)
    first = strategy.sent[0]
    assert first.trigger_price == Price.from_str("10.05")
    before = len(strategy.modified)
    # The owner buys more at a higher price: the average rises and the holding grows.
    strategy.cached.held[0].avg_px_open = Decimal("10.03")
    strategy.cached.held[0].signed_qty = Decimal(12)
    strategy.observe("10.20", T0 + 2 * SECOND)
    assert strategy.canceled == [first.client_order_id]
    assert strategy.modified[before:] == [], "the sale was modified in the step that cancels it"


def test_review7_an_ordinary_trail_raise_is_not_a_gain_lock_event() -> None:
    strategy = holding()
    strategy.observe("10.10", T0 + SECOND)
    first = strategy.sent[0]
    assert [event["event"] for event in records(strategy)] == ["armed"]
    strategy.modify_order(first.client_order_id, trigger_price=Price.from_str("10.30"))
    for second in (2, 3, 4):
        strategy.observe("10.50", T0 + second * SECOND)
    assert [event["event"] for event in records(strategy)] == ["armed"], (
        "the engine's trail moved the level, not the lock: no floor changed"
    )


def test_review8_unarmed_is_named_at_most_once_a_second_per_holding() -> None:
    strategy = holding(None)
    before = len(strategy.said)
    for millis in (100, 200, 300, 400):
        strategy.observe("10.50", T0 + SECOND + millis * 1_000_000)
    unarmed = [line for line in strategy.said[before:] if "gain lock unarmed" in line]
    assert len(unarmed) == 1, f"{len(unarmed)} unarmed lines within one second"


def test_measure1_a_sale_the_lock_protected_records_its_result_and_spread() -> None:
    """The lead's measure 1 (2026-11-13): every sale the lock triggers records its realised result
    against the entry and the spread at its trigger, for a later slippage study (ADR 0147)."""
    strategy = holding()
    strategy.observe("10.10", T0 + SECOND)
    sale = strategy.sent[0]
    # Synthetic events with the fields the handlers read; the cash matcher rejects released
    # EXTERNAL sales, so no native fill exists (header).
    released = cast(
        Any,
        SimpleNamespace(
            strategy_id=PROTECTOR,
            client_order_id=sale.client_order_id,
            instrument_id=AAA,
            ts_event=T0 + 2 * SECOND,
        ),
    )
    strategy.observe("10.04", T0 + 2 * SECOND)
    strategy.on_order_released(released)
    filled = cast(
        Any,
        SimpleNamespace(
            strategy_id=PROTECTOR,
            client_order_id=sale.client_order_id,
            instrument_id=AAA,
            last_px=Price.from_str("10.03"),
            last_qty=Quantity.from_int(10),
            ts_event=T0 + 3 * SECOND,
        ),
    )
    strategy.on_order_filled(filled)
    sales = getattr(strategy, "gain_lock_sales", [])
    assert len(sales) == 1
    assert sales[0]["entry"] == "10.00"
    assert sales[0]["fill_price"] == "10.03"
    assert sales[0]["result_per_share"] == "0.03"
    assert sales[0]["spread_at_release"] == "0.02"


def test_the_lock_never_arms_before_the_plan_so_planning_cannot_lift_it_to_the_bid() -> None:
    """With the plan refused, the spacing is unknown, and a floor applied at a spacing of 0 is lifted
    by the later plan to floor plus spacing, at or over the bid: the next quote then sends a trail
    whose trigger is the bid, which the emulator releases at once. The lock alone must never sell."""
    strategy = GainHarness(gain_lock=True)
    strategy.cached.held[0].avg_px_open = Decimal("10.00")
    strategy.cached.latest = None
    strategy.cached.history = minute_bars("0.20")
    plan = strategy._plan_holding
    refused = [2]  # a planner refusal on the first two quotes, as a refused row gives

    def refusing(*args: Any, **kwargs: Any) -> bool:
        if refused[0]:
            refused[0] -= 1
            return False
        return plan(*args, **kwargs)

    strategy._plan_holding = refusing
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    for second in (1, 2, 3, 4):
        strategy.observe("10.05", T0 + second * SECOND)
    sent = [order for order in strategy.sent if order.order_type == OrderType.TRAILING_STOP_LIMIT]
    assert sent, "the trail is placed once planned"
    assert all(order.trigger_price.as_decimal() < Decimal("10.05") for order in sent), [
        order.trigger_price for order in sent
    ]
