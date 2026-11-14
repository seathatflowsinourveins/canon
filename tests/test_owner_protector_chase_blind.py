"""Blind tests for U-d: the protector's sale chases, in every session.

The properties are `specs/w6/U-d-properties.md`'s P1-P9 and the lead's added P10 and P11. They are
asserted on order events and the cache only, through `tests/test_owner_protector.py`'s `Harness`:
after the release the cached order is the DAY `LIMIT` the pinned emulator leaves behind, with the
released limit's price, and the protector's next `cancel_order`/`submit_order` are the events.

Citations: `docs/handbook/practices.md:43` (a cancel, then a new limit; no order is modified),
`docs/handbook/rows.md:49-50` (`CHASE_CAP` 0.02, `CHASE_SECS` 5) and `:90` (`FLOOR_WAIT_SECS` 60);
`price_step.exit_price` and `mover.chase_floor` are imported as the price reference, as the lead
ruled (2026-11-13 16:29): `max(bid, ...)` stepped onto Alpaca's step, one step of tolerance.

At unit 10's base this file should be red on the chase properties (P2-P7 and P11) and green on the
ownership and not-released guards (P8, P9). P1 and P10 hold at the base only vacuously, through the
released order the test itself puts in the cache; the REPORT says so. Synthetic throughout: no file,
paper state, catalog, ledger, credential or network service is touched.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.core import UUID4
from nautilus_trader.model import AccountId
from nautilus_trader.model import OrderAccepted
from nautilus_trader.model import OrderCanceled
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderSubmitted
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId

from canon_trading.mover import FLOOR_WAIT_SECS
from canon_trading.mover import chase_floor
from canon_trading.price_step import exit_price
from canon_trading.price_step import tick
from tests.test_owner_protector import AAA
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import Harness
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture
from tests.test_owner_protector import position
from tests.test_owner_protector import working_sell

NEW_YORK = ZoneInfo("America/New_York")
CHASE_CAP = Decimal("0.02")  # rows.md:49, `MoverConfig.chase_cap`'s default
CHASE_SECS = 5  # rows.md:50, `MoverConfig.chase_secs`'s default
SESSIONS = {"pre-market": 5, "regular": 11, "after-hours": 17, "overnight": 22}
LEVEL = "9.90"
HELD = Decimal(10)


def session_ns(hour: int) -> int:
    """A quote's timestamp in the named New York session on 2025-11-13."""
    return int(datetime(2025, 11, 13, hour, tzinfo=NEW_YORK).timestamp()) * SECOND


class ChaseHarness(Harness):
    """Unit 10's Harness, with the venue's cancel acknowledgement made observable.

    `answer_cancels` False leaves a cancel unanswered until the test answers it, so P4 can see the
    seriation; True answers it at once, as a venue would for a cancel that lands before the next
    quote. A modify still fails loudly: the lead ruled cancel-then-new and ADR 0088 refuses modify.
    """

    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self.answer_cancels = True

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        super().submit_order(order, position_id, client_id, params)
        if order.order_type != OrderType.LIMIT:
            return  # The trailing sell is emulated and reaches no venue until it is released.
        # The venue's submit and accept acknowledgements, as `answer_cancels` answers a cancel: a
        # sale whose acceptance never comes is not open (orders/mod.rs:477-490), and main's chase
        # waits for it (mover.py:4678).
        at = self.clock.timestamp_ns()
        account = AccountId("ALPACA-001")
        order.apply(
            OrderSubmitted(
                order.trader_id,
                order.strategy_id,
                AAA,
                order.client_order_id,
                account,
                UUID4(),
                at,
                at,
            )
        )
        order.apply(
            OrderAccepted(
                order.trader_id,
                order.strategy_id,
                AAA,
                order.client_order_id,
                VenueOrderId(str(order.client_order_id)),
                account,
                UUID4(),
                at,
                at,
                False,  # reconciliation
            )
        )

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        super().cancel_order(client_order_id, client_id, params)
        if not self.answer_cancels:
            return
        for order in self.cached.working:
            if order.client_order_id != client_order_id:
                continue
            if isinstance(order, SimpleNamespace):  # the test's released limit
                order.status = OrderStatus.CANCELED
                order.is_closed = True
            elif order.is_open:  # a limit the protector sent: the venue's terminal cancel
                at = self.clock.timestamp_ns()
                order.apply(
                    OrderCanceled(
                        order.trader_id,
                        order.strategy_id,
                        AAA,
                        order.client_order_id,
                        UUID4(),
                        at,
                        at,
                        False,
                    )
                )


class ReleasedLimit(SimpleNamespace):
    """The released LIMIT's status flags as the pinned native order derives them from its status
    (crates/model/src/orders/mod.rs:477-525; a released order has no emulation trigger)."""

    @property
    def is_open(self) -> bool:
        return self.status in (
            OrderStatus.ACCEPTED,
            OrderStatus.TRIGGERED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.PENDING_UPDATE,
            OrderStatus.PARTIALLY_FILLED,
        )

    @property
    def is_pending_cancel(self) -> bool:
        return self.status == OrderStatus.PENDING_CANCEL

    @property
    def is_pending_update(self) -> bool:
        return self.status == OrderStatus.PENDING_UPDATE


def released_sale(
    first: Any,
    *,
    price: str = LEVEL,
    qty: str = "10",
    filled: str = "0",
    status: OrderStatus = OrderStatus.ACCEPTED,
    closed: bool = False,
    released_at: int,
) -> Any:
    """The sale the emulator releases: a DAY reduce-only LIMIT with the retained price, keeping the
    trail's events and its OrderReleased (emulator.rs:1394-1405, :1559-1580)."""
    level = Price.from_str(price)
    release = OrderReleased(
        first.trader_id,
        first.strategy_id,
        AAA,
        first.client_order_id,
        level,
        UUID4(),
        released_at,
        released_at,
    )
    return ReleasedLimit(
        **vars(
            working_sell(
                str(first.client_order_id),
                qty=qty,
                filled=filled,
                strategy_id=PROTECTOR,
                status=status,
                is_closed=closed,
                price=level,
                trigger_price=level,
                time_in_force=TimeInForce.DAY,
                is_reduce_only=True,
                events=lambda: [SimpleNamespace(trigger_price=level), release],
            )
        )
    )


def arm_and_release(
    strategy: ChaseHarness, *, start: int | None = None, **changes: Any
) -> tuple[Any, int]:
    """Adopt at 9.90, send the protective stop, then leave its released sale in the cache."""
    at = strategy.clock.timestamp_ns() if start is None else start
    strategy.test_clock.set_time(at)
    strategy.adopt_holding(AAA, Price.from_str(LEVEL))
    strategy.observe("10.00", at + SECOND // 2)
    first = strategy.sent[0]
    strategy.cached.working = [released_sale(first, released_at=at + SECOND, **changes)]
    return first, at + SECOND


def chase(strategy: ChaseHarness, bid: str, at: int) -> None:
    """One chase step: the quote at `at`, then the quote that sees the cancel answered."""
    strategy.observe(bid, at)
    strategy.observe(bid, at + SECOND // 2)


def steps(strategy: ChaseHarness, start: int, end: int) -> None:
    """The protector's one-second step timer (owner_protector.py:533), fired over [start, end) as
    unit 10's tests fire it; the quotes in between hold the last bid in the cache."""
    for at in range(start, end, SECOND):
        strategy.test_clock.set_time(at)
        strategy._on_step(cast(Any, SimpleNamespace(ts_event=at)))


def own_working(strategy: ChaseHarness) -> list[Any]:
    """Every SELL of the protector's own lineage that is still working, from the cache."""
    return [
        order
        for order in strategy.cached.working
        if order.strategy_id == PROTECTOR
        and order.side == OrderSide.SELL
        and order.instrument_id == AAA
        and not order.is_closed
    ]


def chase_price(bid: str, *, at_floor: bool = True) -> Decimal:
    """Main's built chase price; the one place the price rule lives.

    `price_step.exit_price(bid, exit_ticks, floor)` with `exit_ticks` 1 and `floor =
    mover.chase_floor(first released price, chase_cap)`; `at_floor` False is after the floor wait.
    """
    floor = chase_floor(Decimal(LEVEL), CHASE_CAP) if at_floor else None
    return exit_price(Decimal(bid), 1, floor)


def assert_price_within_step(order: Any, expected: Decimal) -> None:
    assert order.price is not None, "the chased sale carries no limit price"
    step = tick(expected)
    assert abs(order.price.as_decimal() - expected) <= step, (
        f"chased sale at {order.price} is not within one step of {expected} "
        f"(exit_price(bid, 1, chase_floor(first, {CHASE_CAP})))"
    )


def test_p1_at_most_one_working_sale_of_the_protectors_lineage() -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(strategy)
    for step, bid in enumerate(("9.80", "9.70", "9.60"), start=1):
        chase(strategy, bid, released_at + step * (CHASE_SECS + 1) * SECOND)
        assert len(own_working(strategy)) <= 1, (
            f"the protector has two working sells after bid {bid}"
        )


@pytest.mark.parametrize("session", tuple(SESSIONS), ids=tuple(SESSIONS))
def test_p2_every_sale_it_sends_is_a_day_limit_for_the_held_shares(session: str) -> None:
    strategy = ChaseHarness()
    first, released_at = arm_and_release(strategy, start=session_ns(SESSIONS[session]))
    assert first.order_type == OrderType.TRAILING_STOP_LIMIT
    assert first.emulation_trigger == TriggerType.BID_ASK
    chase(strategy, "9.80", released_at + (CHASE_SECS + 1) * SECOND)
    assert len(strategy.sent) > 1, (
        f"{session}: no sale was sent after the released limit, so there is no chase form to read"
    )
    for index, sale in enumerate(strategy.sent):
        if index == 0:  # the protective trailing stop-limit, not the released sale
            continue
        assert sale.side == OrderSide.SELL
        assert sale.order_type == OrderType.LIMIT, f"{session}: a sale that is not a limit"
        assert sale.time_in_force == TimeInForce.DAY, f"{session}: a sale that is not DAY"
        assert sale.is_reduce_only, f"{session}: a sale that is not reduce-only"
        assert sale.emulation_trigger is None, f"{session}: a sale left as an emulated stop"
        assert strategy.params[index] == {"extended_hours": True}, (
            f"{session}: a sale not marked extended_hours"
        )
        assert sale.quantity.as_decimal() <= HELD, f"{session}: a sale larger than the holding"


def test_p3_the_released_sale_is_canceled_and_rechased_through_the_bid() -> None:
    strategy = ChaseHarness()
    first, released_at = arm_and_release(strategy)
    chase(strategy, "9.80", released_at + (CHASE_SECS + 1) * SECOND)
    assert strategy.canceled == [first.client_order_id], (
        "the released sale was not canceled when the bid fell 0.10 below its price"
    )
    assert len(strategy.sent) == 2, "no replacement limit sale followed the cancel"
    replacement = strategy.sent[1]
    assert replacement.side == OrderSide.SELL
    assert replacement.order_type == OrderType.LIMIT
    assert replacement.time_in_force == TimeInForce.DAY
    assert replacement.is_reduce_only
    assert strategy.params[1] == {"extended_hours": True}
    assert replacement.quantity == Quantity.from_int(10)
    assert_price_within_step(replacement, chase_price("9.80"))


def test_p4_no_new_sale_while_the_cancel_is_unanswered() -> None:
    strategy = ChaseHarness()
    strategy.answer_cancels = False
    first, released_at = arm_and_release(strategy)
    chase(strategy, "9.80", released_at + (CHASE_SECS + 1) * SECOND)
    assert strategy.canceled == [first.client_order_id], (
        "the released sale was not canceled, so the seriation cannot be observed"
    )
    assert len(strategy.sent) == 1, "a new sale was sent before the cancel was terminal"
    # The cancel answers only now, after the holding has shrunk to 6: the remainder must be read
    # after the terminal status, not before it.
    strategy.cached.held = [position("6")]
    for order in strategy.cached.working:
        if order.client_order_id == first.client_order_id:
            order.status, order.is_closed = OrderStatus.CANCELED, True
    strategy.answer_cancels = True
    strategy.observe("9.80", released_at + (2 * CHASE_SECS + 2) * SECOND)
    assert len(strategy.sent) == 2, "the substitute sale was not sent after the cancel was terminal"
    assert strategy.sent[1].order_type == OrderType.LIMIT
    assert strategy.sent[1].quantity == Quantity.from_int(6), (
        "the substitute was sized before the cancel was terminal"
    )


def test_p5_the_floor_is_released_after_the_floor_wait() -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(strategy)
    floor = chase_floor(Decimal(LEVEL), CHASE_CAP)
    chase(strategy, "9.60", released_at + (CHASE_SECS + 1) * SECOND)
    assert strategy.canceled, "the sale resting at the floor was never re-priced"
    at_floor = own_working(strategy)
    assert len(at_floor) == 1, "no single working sale at the chase floor"
    assert_price_within_step(at_floor[0], chase_price("9.60"))
    steps(
        strategy,
        released_at + (CHASE_SECS + 2) * SECOND,
        released_at + (CHASE_SECS + FLOOR_WAIT_SECS + 3) * SECOND,
    )
    chase(strategy, "9.60", released_at + (CHASE_SECS + FLOOR_WAIT_SECS + 3) * SECOND)
    following = own_working(strategy)
    assert len(following) == 1, "the floor release left the holding without a working sale"
    assert_price_within_step(following[0], chase_price("9.60", at_floor=False))
    assert following[0].price.as_decimal() < floor, (
        "after the floor wait the sale did not follow the bid past the floor"
    )


def test_p6_a_partial_fill_leaves_exactly_the_remainder_chased() -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(
        strategy, qty="10", filled="4", status=OrderStatus.PARTIALLY_FILLED
    )
    strategy.cached.held = [position("6")]
    chase(strategy, "9.80", released_at + (CHASE_SECS + 1) * SECOND)
    assert len(strategy.sent) == 2, "the 6-share remainder of the partial fill was not chased"
    replacement = strategy.sent[1]
    assert replacement.order_type == OrderType.LIMIT
    assert replacement.quantity == Quantity.from_int(6), (
        "the chased sale is not exactly the remainder the external long still holds"
    )


def test_p7_an_expired_day_sale_is_sent_again_as_a_limit() -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(strategy, status=OrderStatus.EXPIRED, closed=True)
    strategy.observe("10.00", released_at + SECOND)
    assert len(strategy.sent) == 2, "the expired DAY sale was not sent again on the next quote"
    renewal = strategy.sent[1]
    assert renewal.order_type == OrderType.LIMIT, (
        "the expired sale was replaced by a new stop, not by a limit sale"
    )
    assert renewal.quantity == Quantity.from_int(10)
    assert renewal.time_in_force == TimeInForce.DAY
    assert renewal.is_reduce_only
    assert strategy.params[-1] == {"extended_hours": True}


def test_p8_ownership_guard_foreign_sell_freezes_and_only_own_ids_are_canceled() -> None:
    frozen = ChaseHarness()
    frozen.cached.working = [working_sell("owner-limit", qty="1", filled="0")]
    frozen.adopt_holding(AAA, Price.from_str(LEVEL))
    frozen.observe("10.00", frozen.clock.timestamp_ns() + SECOND)
    assert not frozen.sent
    assert "foreign" in frozen.frozen[AAA]
    assert not frozen.canceled

    strategy = ChaseHarness()
    strategy.adopt_holding(AAA, Price.from_str(LEVEL))
    strategy.observe("10.00", strategy.clock.timestamp_ns() + SECOND)
    own = strategy.sent[0]
    strategy.cached.working.append(working_sell("owner-limit", qty="1", filled="0"))
    strategy.observe("10.00", strategy.clock.timestamp_ns() + SECOND)
    assert "foreign" in strategy.frozen[AAA]
    assert own.client_order_id in strategy.canceled
    assert all(str(cid) != "owner-limit" for cid in strategy.canceled)


def test_p9_not_released_guard_nothing_is_chased_before_the_trigger() -> None:
    strategy = ChaseHarness()
    strategy.adopt_holding(AAA, Price.from_str(LEVEL))
    strategy.observe("10.00", strategy.clock.timestamp_ns() + SECOND)
    first = strategy.sent[0]
    for bid in ("10.00", "10.10", "10.20"):
        strategy.observe(bid, strategy.clock.timestamp_ns() + (CHASE_SECS + 1) * SECOND)
    assert len(strategy.sent) == 1
    assert not strategy.canceled
    assert first.order_type == OrderType.TRAILING_STOP_LIMIT
    assert first.trigger_price == Price.from_str(LEVEL)
    assert first.emulation_trigger == TriggerType.BID_ASK


@pytest.mark.parametrize("session", tuple(SESSIONS), ids=tuple(SESSIONS))
def test_p10_after_the_trigger_a_working_sale_or_a_named_reason(session: str) -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(strategy, start=session_ns(SESSIONS[session]))
    chase(strategy, "9.80", released_at + (CHASE_SECS + 1) * SECOND)
    named = [
        line
        for line in strategy.said
        if AAA.symbol.value in line
        and any(word in line for word in ("frozen", "refused", "no sale", "unprotected"))
    ]
    assert len(own_working(strategy)) == 1 or named, (
        f"{session}: after the trigger the holding had no working sale and no named reason"
    )


def test_p11_a_gap_through_the_chase_floor_still_sells() -> None:
    strategy = ChaseHarness()
    _, released_at = arm_and_release(strategy)
    floor = chase_floor(Decimal(LEVEL), CHASE_CAP)
    chase(strategy, "9.40", released_at + (CHASE_SECS + 1) * SECOND)
    assert strategy.canceled, "a gap straight through the floor left the released sale un-repriced"
    at_floor = own_working(strategy)
    assert len(at_floor) == 1, "the gap left the holding without a working sale"
    assert_price_within_step(at_floor[0], chase_price("9.40"))
    steps(
        strategy,
        released_at + (CHASE_SECS + 2) * SECOND,
        released_at + (CHASE_SECS + FLOOR_WAIT_SECS + 3) * SECOND,
    )
    chase(strategy, "9.40", released_at + (CHASE_SECS + FLOOR_WAIT_SECS + 3) * SECOND)
    following = own_working(strategy)
    assert len(following) == 1, "the floor release left the holding without a working sale"
    assert_price_within_step(following[0], chase_price("9.40", at_floor=False))
    assert following[0].price.as_decimal() < floor, (
        "a gap through the floor never sold below the first sale's chase floor"
    )
