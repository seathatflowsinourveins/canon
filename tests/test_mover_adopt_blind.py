"""Unit 5: blind contracts for Mover.adopt_holding, using the real offline engine.

The task supplies the new public seam; no implementation is supplied or substituted.
Pinned NautilusTrader d53849b216e2 contracts read for this driver:
  python/nautilus_trader/common/__init__.pyi:1388 (OrderFactory.limit);
  python/nautilus_trader/trading/__init__.pyi:538,1021 (submit_order, StrategyConfig);
  crates/backtest/src/python/engine.rs:487,530 (streaming run and clear_data);
  python/nautilus_trader/model/__init__.pyi:4436,6365,7107 (fills, quotes, orders);
  crates/execution/src/trailing.rs:61,100 (initial trigger and upward-only sell trail).
The existing test_mover engine, Recording and strategy helpers supply the venue and timer.
All prices and holdings are invented; only in-memory cache/engine state is written.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any
from typing import cast

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TriggerType
from nautilus_trader.trading import Strategy
from nautilus_trader.trading import StrategyConfig

from canon_trading.mover import MoverConfig
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MOVER_ID
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import engine
from tests.test_mover import strategy

LEVEL = Price.from_str("9.95")
QUARTER = SECOND // 4


class AdoptionRecording(Recording):
    """Observe public order commands without replacing execution or Mover decisions."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.commands: list[tuple[str, Any]] = []
        self.submissions: list[tuple[OrderSide, OrderType]] = []
        self.seed_quantity: int | None = None
        self.seeding = True

    def on_start(self) -> None:
        super().on_start()
        if self.seed_quantity is not None:
            seed_long(self, self.seed_quantity)

    def on_order_filled(self, event) -> None:
        # Seed fills belong to the simulated earlier run. This run receives its
        # position through the cache, without an entry-fill callback to open a slot.
        if not self.seeding:
            super().on_order_filled(event)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.commands.append(("submit", order.client_order_id))
        self.submissions.append((order.side, order.order_type))
        super().submit_order(order, position_id, client_id, params)

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.commands.append(("cancel", client_order_id))
        super().cancel_order(client_order_id, client_id, params)

    def cancel_all_orders(
        self, instrument_id, order_side=None, client_id=None, strategy_only=True, params=None
    ) -> None:
        self.commands.append(("cancel_all", instrument_id))
        super().cancel_all_orders(instrument_id, order_side, client_id, strategy_only, params)


def seed_long(seeder: Strategy, quantity: int) -> None:
    """Fixture input: an actual simulated fill, before measuring adoption commands."""
    order = seeder.order_factory.limit(
        AAA,
        OrderSide.BUY,
        Quantity.from_int(quantity),
        Price.from_str("10.0200"),
        time_in_force=TimeInForce.DAY,
    )
    Strategy.submit_order(seeder, order)


class OtherHolding(Strategy):
    """A fixture owner, including the engine's EXTERNAL strategy identifier."""

    quantity = 7

    def on_start(self) -> None:
        seed_long(self, self.quantity)


def quote(instrument_id: InstrumentId, offset: int, bid: str = "10.00") -> QuoteTick:
    return QuoteTick(
        instrument_id,
        Price.from_str(f"{Decimal(bid):.4f}"),
        Price.from_str(f"{Decimal(bid) + Decimal('0.02'):.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        T0 + offset,
        T0 + offset,
    )


@dataclass
class AdoptionNode:
    eng: BacktestEngine
    mover: AdoptionRecording
    seed_ids: set[ClientOrderId] = field(default_factory=set)

    def stream(self, *quotes: QuoteTick) -> None:
        """Advance actual timers without ending the run and canceling its protection."""
        self.eng.clear_data()
        self.eng.add_data(list(quotes))
        self.eng.run(streaming=True)

    def start(self, *, owner: StrategyId = MOVER_ID, quantity: int = 7, bid="10.00") -> None:
        if owner == MOVER_ID:
            self.mover.seed_quantity = quantity
        else:
            # An explicit tag rewrites the strategy ID at registration. Keep the
            # supplied ID exact, especially the reserved EXTERNAL owner.
            seeder = OtherHolding(StrategyConfig(strategy_id=owner))
            seeder.quantity = quantity
            self.eng.add_strategy(seeder)
        self.stream(quote(AAA, 0), quote(AAA, QUARTER // 2, bid), quote(BBB, QUARTER))
        self.mover.seeding = False
        (seed,) = self.eng.cache.orders()
        assert seed.side == OrderSide.BUY and seed.filled_qty.as_decimal() == Decimal(quantity)
        self.seed_ids.add(seed.client_order_id)
        (cached,) = self.eng.cache.positions_open(instrument_id=AAA, strategy_id=owner)
        assert cached.is_long and cached.signed_qty == Decimal(quantity)
        assert not self.mover.slots and not self.orders() and not self.mover.commands

    def adopt(self) -> None:
        # The cast keeps the file type-checkable at the blind base; a missing
        # method is an ordinary AttributeError, never a skip or an expected failure.
        assert cast(Any, self.mover).adopt_holding(AAA, LEVEL) is None

    def orders(self) -> list[Any]:
        return [
            order
            for order in self.eng.cache.orders(strategy_id=MOVER_ID)
            if order.client_order_id not in self.seed_ids
        ]

    def held(self, owner: StrategyId = MOVER_ID) -> Decimal:
        return sum(
            (
                p.signed_qty
                for p in self.eng.cache.positions_open(instrument_id=AAA, strategy_id=owner)
            ),
            Decimal(0),
        )


@pytest.fixture
def node() -> Iterator[AdoptionNode]:
    eng = engine()
    # Adoption must cover the holding, even when it exceeds the entry share cap.
    mover = AdoptionRecording(strategy(max_shares=2).settings)
    eng.add_strategy(mover)
    try:
        yield AdoptionNode(eng, mover)
    finally:
        eng.dispose()


def assert_one_protective_sell(node: AdoptionNode, quantity: int) -> Any:
    orders = node.orders()
    (order,) = orders
    assert order.instrument_id == AAA and order.strategy_id == MOVER_ID
    assert order.side == OrderSide.SELL and order.order_type == OrderType.TRAILING_STOP_LIMIT
    assert order.is_emulated and not order.is_closed
    assert order.emulation_trigger == TriggerType.BID_ASK
    assert order.quantity.as_decimal() == Decimal(quantity)
    assert order.is_reduce_only
    assert order.trigger_price == LEVEL
    assert order.filled_qty.as_decimal() == 0
    return order.client_order_id


def assert_no_sale(node: AdoptionNode) -> None:
    """An emulated protective sell may be armed, but no executable sale may be sent."""
    assert node.held() == Decimal(7)
    assert all(kind == OrderType.TRAILING_STOP_LIMIT for _, kind in node.mover.submissions)
    assert all(order.order_type == OrderType.TRAILING_STOP_LIMIT for order in node.orders())
    assert all(order.filled_qty.as_decimal() == 0 for order in node.orders())
    assert not any(
        isinstance(event, OrderReleased) for order in node.orders() for event in order.events()
    )


@pytest.mark.parametrize("quantity", [1, 13])
def test_p1_adoption_arms_one_reduce_only_sell_for_the_whole_holding(node, quantity) -> None:
    node.start(quantity=quantity)
    node.adopt()
    assert_one_protective_sell(node, quantity)
    assert node.held() == Decimal(quantity)
    # A timer step must preserve the supplied 9.95 level, not seed a fresh lower trail.
    node.stream(quote(BBB, SECOND + QUARTER))
    assert_one_protective_sell(node, quantity)


@pytest.mark.parametrize("bid", ["9.95", "9.94"], ids=["at-level", "below-level"])
def test_p2_one_cached_breach_never_sells_but_a_later_quote_can(node, bid) -> None:
    node.start(bid=bid)
    node.adopt()
    assert_no_sale(node)
    node.stream(quote(BBB, SECOND + QUARTER))
    assert_no_sale(node)  # first timer step, still only the quote present at adoption
    node.stream(quote(BBB, 4 * SECOND + QUARTER))
    assert_no_sale(node)  # multiple later steps and another instrument are not observations

    confirming = quote(AAA, 5 * SECOND + QUARTER, bid)
    node.stream(confirming, quote(BBB, 6 * SECOND + QUARTER))
    sales = [order for order in node.orders() if order.order_type == OrderType.LIMIT]
    (sale,) = sales
    assert sale.side == OrderSide.SELL and sale.quantity.as_decimal() == Decimal(7)
    assert sale.is_open or sale.filled_qty.as_decimal() > 0
    releases = [event for event in sale.events() if isinstance(event, OrderReleased)]
    # Either the emulator releases its sell or Mover sends its usual direct limit.
    sent_at = releases[0].ts_init if releases else sale.ts_init
    assert sent_at >= confirming.ts_init


def test_p3_adoption_and_later_steps_never_submit_an_entry(node) -> None:
    node.start()
    node.adopt()
    node.stream(quote(AAA, SECOND + QUARTER), quote(BBB, 3 * SECOND + QUARTER))
    assert node.mover.submissions, "the holding must actually receive protection"
    assert all(side == OrderSide.SELL for side, _ in node.mover.submissions)
    assert all(order.side == OrderSide.SELL for order in node.orders())
    assert node.held() == Decimal(7)


@pytest.mark.parametrize("owner", ["Other-002", "EXTERNAL"])
def test_p4_foreign_and_external_longs_are_refused_by_name_without_changes(node, owner) -> None:
    owner_id = StrategyId(owner)
    node.start(owner=owner_id)
    before = [p.to_dict() for p in node.eng.cache.positions_open(instrument_id=AAA)]
    orders_before = [order.to_dict() for order in node.eng.cache.orders()]
    slots = set(node.mover.slots)
    opened = set(node.mover.opened_slots)
    reasons: list[str] = []
    try:
        node.adopt()
    except (ValueError, RuntimeError) as error:
        # The contract specifies a named refusal, not an exception type. A logged
        # refusal is equally acceptable; AttributeError still fails at the base.
        reasons.append(str(error))
    reasons.extend(node.mover.said)
    reasons.extend(node.mover.dropped)
    assert any(str(AAA) in reason for reason in reasons), reasons
    assert any(
        word in " ".join(reasons).lower()
        for word in ("own", "foreign", "external", "strategy", "belong")
    ), reasons
    assert node.mover.commands == []
    assert set(node.mover.slots) == slots and set(node.mover.opened_slots) == opened
    assert [order.to_dict() for order in node.eng.cache.orders()] == orders_before
    assert [p.to_dict() for p in node.eng.cache.positions_open(instrument_id=AAA)] == before
    assert node.held(owner_id) == Decimal(7) and node.held() == 0
    # A refused adoption must not leave a slot that sends something on the next step.
    node.stream(quote(BBB, 2 * SECOND + QUARTER))
    assert node.mover.commands == [] and set(node.mover.slots) == slots
    assert [p.to_dict() for p in node.eng.cache.positions_open(instrument_id=AAA)] == before


@pytest.mark.parametrize("after_step", [False, True], ids=["immediate", "after-step"])
def test_p5_repeated_adoption_does_not_arm_another_sell(node, after_step) -> None:
    node.start()
    node.adopt()
    first_id = assert_one_protective_sell(node, 7)
    if after_step:
        node.stream(quote(BBB, 2 * SECOND + QUARTER))
    commands = list(node.mover.commands)
    try:
        node.adopt()
    except (ValueError, RuntimeError) as error:
        assert str(AAA) in str(error), "a duplicate refusal must identify its instrument"
    assert node.mover.commands == commands, "a duplicate must not cancel and replace its sell"
    assert assert_one_protective_sell(node, 7) == first_id
    node.stream(quote(BBB, 4 * SECOND + QUARTER))
    assert assert_one_protective_sell(node, 7) == first_id
    assert len(node.mover.submissions) == 1
