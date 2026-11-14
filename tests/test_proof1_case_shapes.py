"""Proof 1: two case shapes for OwnerProtector's protective sale on the pinned native emulator.

Shape A, late lots then a fall on thin quotes, proves: the gain lock arms on the lots' weighted
average; from arming on no working sell's trigger is under entry plus round-trip cost; the sale is
released as a limit; every sell is submitted with {"extended_hours": True}; and the limit at release
is at or above entry less the Mover's chase cap (an expected outcome on this shape, not a floor the
protector enforces).

Shape B, a gap straight through both floors, proves the Mover's chase applied to the released sale
(R3, the lead's 2026-11-13 ruling: the exit beats the lock): after `chase_secs` it is re-priced down
to `mover.chase_floor` of its first sale price, and after `FLOOR_WAIT_SECS` resting there with the
bid through it, it follows the bid; at every step exactly one working sale covers the held quantity.
The chase is the Mover's (src/canon_trading/mover.py header :120-127, :179-182; `chase_floor`;
`_chase`'s FLOOR_WAIT_SECS branch), with its rows CHASE_CAP, CHASE_SECS and FLOOR_WAIT_SECS read from
MoverConfig's defaults and mover.py (docs/handbook/rows.md); no number of its own.

Harness and its known limits (tests/test_gain_lock.py:1-6). The cash matcher does not fill against an
EXTERNAL long, so release and the working sale's price are observed, not fills. At the pin
(nt@d53849b216e2) every backtest venue refuses this protector's released reduce-only sale at
submission, because the matcher looks up the order's own strategy's NETTING position
(crates/execution/src/matching_engine/mod.rs:5545-5559) and the protector owns none: a CASH equity
venue rejects it as a short sale (:3626-3667), a MARGIN venue as a reduce-only increase (:3583-3617),
and a venue without reduce-only support refuses the flag (:3518-3522). Shape A runs on the CASH venue
unchanged, so its release is followed by that rejection. Shape B needs a sale that rests, so it runs
on a MARGIN venue with `use_reduce_only=False` (backtest/__init__.pyi:466), and the protector's order
factory is wrapped to record the reduce_only it asked for and hand the venue a copy without it. A
sale that becomes marketable there fills against the book into a protector-owned short, not the
EXTERNAL long, so Shape B observes sales only up to the first sale priced at or under the bid.
Everything else is the pinned engine: the emulator's trailing and release, the risk and execution
engines, the venue's acceptance, cancel and terminal OrderCanceled. Instrument AAA, prices and times
are scaled shapes; no account symbol, price or time. No file, catalog, ledger or credential is used.

Each shape runs at two scales. The dollar case is the instrument the gain-lock tests use (price
precision 2). The sub-dollar case is a name under $1.00 quoted in four decimals, on the adapter's own
instrument: canon_alpaca.constants.PRICE_PRECISION (4) and PRICE_INCREMENT (0.0001), which
providers.equity_from_asset sets for every US equity (providers.py:548-549), and Alpaca's step
under a dollar (price_step.tick, "Sub-penny increments"). It holds 500,000 shares on an entry off
the step (0.42238). Beyond Shapes A and B it proves that every price the protector sends (trigger,
limit, modify, chase price), and every released limit, is on the increment and on Alpaca's step,
and that from arming on no trigger, no floor applied by modify, and neither of the lock's floors is
under entry plus cost. A chase's plain limit may be (R3).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from typing import Self

import pytest
from canon_alpaca.constants import PRICE_INCREMENT
from canon_alpaca.constants import PRICE_PRECISION
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue

from canon_trading.mover import FLOOR_WAIT_SECS
from canon_trading.mover import STEP_SECS
from canon_trading.mover import MoverConfig
from canon_trading.mover import chase_floor
from canon_trading.price_step import tick
from tests.test_owner_protector import AAA
from tests.test_owner_protector import EXTERNAL
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import USD
from tests.test_owner_protector import Owner
from tests.test_owner_protector import SimulatedProtector
from tests.test_owner_protector import equity
from tests.test_owner_protector import minute_bars
from tests.test_owner_protector import quote

_MOVER = inspect.signature(MoverConfig.__init__).parameters
CHASE_CAP: Decimal = _MOVER["chase_cap"].default
"""Row CHASE_CAP (docs/handbook/rows.md): MoverConfig's default, 0.02 under the first sale price."""
CHASE_SECS: int = _MOVER["chase_secs"].default
"""Row CHASE_SECS: MoverConfig's default, 5 seconds a released sale rests before it is re-priced."""
RESEND_GAP_NS = STEP_SECS * SECOND
"""The Mover's cancel-to-resend gap: the cancel is seen terminal at a later step, then the new limit
goes out (mover.py:121-127), so a step with no working sale lasts at most one STEP_SECS."""
PACE_NS = CHASE_SECS * SECOND + 2 * STEP_SECS * SECOND
"""How late a chase action may come after it is due: one CHASE_SECS pacing and a cancel/resend."""
GAP_SECS = 3 * FLOOR_WAIT_SECS
"""How long Shape B's gap bid is held: long enough for a chase, its floor wait and a follow."""


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared credential-environment reader for these offline cases."""


def two_sided(bid: str, ask: str, at: int, bid_size: int = 100, ask_size: int = 100) -> QuoteTick:
    return QuoteTick(
        AAA,
        Price.from_str(bid),
        Price.from_str(ask),
        Quantity.from_int(bid_size),
        Quantity.from_int(ask_size),
        at,
        at,
    )


def thin(bid: str, at: int, spread: str = "0.06", bid_size: int = 300, ask_size: int = 200):
    """A wide quote with a few hundred shares a side."""
    return two_sided(bid, str(Decimal(bid) + Decimal(spread)), at, bid_size, ask_size)


class LateLots(Owner):
    """The owner's buys: one lot at each scheduled quote, market, under EXTERNAL ownership."""

    def __new__(cls, lots: dict[int, int]) -> Self:
        return super().__new__(cls)

    def __init__(self, lots: dict[int, int]) -> None:
        super().__init__()
        self.lots = dict(lots)

    def on_quote(self, quote) -> None:
        shares = self.lots.pop(quote.ts_init, None)
        if shares:
            self.submit_order(
                self.order_factory.market(AAA, OrderSide.BUY, Quantity.from_int(shares))
            )


class VenueCopy:
    """Records the reduce_only the protector asked for, and gives the venue a copy without it."""

    def __init__(self, factory: Any, asked: dict[str, Any]) -> None:
        self.factory = factory
        self.asked = asked

    def __getattr__(self, name: str) -> Any:
        make = getattr(self.factory, name)
        if not callable(make):
            return make

        def made(*args: Any, **kwargs: Any) -> Any:
            asked = kwargs.get("reduce_only", False)
            kwargs["reduce_only"] = False
            order = make(*args, **kwargs)
            self.asked[str(order.client_order_id)] = asked
            return order

        return made


class Watched(SimulatedProtector):
    """The unit's protector, with a snapshot of its working sells after each of its own callbacks
    and a record of every price it sends (submit and modify)."""

    def __new__(cls, level: str, *, venue_copy: bool = False) -> Self:
        return super().__new__(cls, level)

    def __init__(self, level: str, *, venue_copy: bool = False) -> None:
        super().__init__(level)
        # Both shapes prove the gain lock, which is opt-in (the lead, 2026-11-13 22:47).
        self.settings.gain_lock = True
        self.asked: dict[str, Any] = {}
        self.venue_copy = venue_copy
        self.snapshots: list[tuple[int, Decimal | None, list[Any]]] = []
        self.cancels: list[tuple[int, str]] = []
        self.prices_sent: list[tuple[int, str, str, Decimal]] = []

    @property
    def order_factory(self) -> Any:
        factory = super().order_factory
        return VenueCopy(factory, self.asked) if self.venue_copy else factory

    def working(self) -> list[Any]:
        return [
            order
            for order in self.cache.orders(instrument_id=AAA, strategy_id=self.strategy_id)
            if order.side == OrderSide.SELL and not order.is_closed
        ]

    def snap(self) -> None:
        latest = self.cache.quote(AAA)
        self.snapshots.append(
            (
                self.clock.timestamp_ns(),
                None if latest is None else latest.bid_price.as_decimal(),
                [
                    (
                        str(o.client_order_id),
                        o.order_type,
                        o.status,
                        None if getattr(o, "price", None) is None else o.price.as_decimal(),
                        o.leaves_qty.as_decimal(),
                        None
                        if getattr(o, "trigger_price", None) is None
                        else o.trigger_price.as_decimal(),
                    )
                    for o in self.working()
                ],
            )
        )

    def _sent(self, kind: str, price: Any, trigger: Any) -> None:
        now = self.clock.timestamp_ns()
        for name, value in (("limit", price), ("trigger", trigger)):
            if value is not None:
                self.prices_sent.append((now, kind, name, value.as_decimal()))

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self._sent("submit", getattr(order, "price", None), getattr(order, "trigger_price", None))
        super().submit_order(order, position_id, client_id, params)

    def modify_order(
        self,
        client_order_id,
        quantity=None,
        price=None,
        trigger_price=None,
        client_id=None,
        params=None,
    ) -> None:
        self._sent("modify", price, trigger_price)
        super().modify_order(client_order_id, quantity, price, trigger_price, client_id, params)

    def on_quote(self, quote) -> None:
        super().on_quote(quote)
        self.snap()

    def _on_step(self, event) -> None:
        super()._on_step(event)
        self.snap()

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.cancels.append((self.clock.timestamp_ns(), str(client_order_id)))
        super().cancel_order(client_order_id, client_id, params)


@dataclass(frozen=True)
class Case:
    """One scale of both shapes: the instrument, the owner's lots and the quotes."""

    name: str
    instrument: Equity
    balance: int
    bars: list[Bar]
    level: str
    a_lots: dict[int, int]
    a_entry: Decimal
    a_quotes: Callable[[], list[QuoteTick]]
    b_lots: dict[int, int]
    b_warm: Callable[[], list[QuoteTick]]
    b_gap_bid: Decimal
    b_gap: Callable[[int], QuoteTick]

    @property
    def a_held(self) -> Decimal:
        return Decimal(sum(self.a_lots.values()))

    @property
    def b_held(self) -> Decimal:
        return Decimal(sum(self.b_lots.values()))


@contextmanager
def replay(case: Case, owner: Owner, protector: Watched, *, account, use_reduce_only: bool):
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("TESTER-001"), bypass_logging=True)
    )
    engine.add_venue(
        Venue("ALPACA"),
        OmsType.NETTING,
        account,
        [Money(case.balance, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
        use_reduce_only=use_reduce_only,
    )
    engine.add_instrument(case.instrument)
    engine.add_strategy(owner)
    engine.add_strategy(protector)
    engine.add_data(sorted(case.bars, key=lambda bar: bar.ts_init))
    try:
        yield engine
    finally:
        engine.end()
        engine.dispose()


def feed(engine: BacktestEngine, ticks: list[QuoteTick]) -> None:
    engine.add_data(ticks)
    engine.run(streaming=True)
    engine.clear_data()


def released(engine: BacktestEngine, protector: Watched) -> list[Any]:
    return [
        order
        for order in engine.cache.orders(instrument_id=AAA, strategy_id=protector.strategy_id)
        if any(type(event).__name__ == "OrderReleased" for event in order.events())
    ]


def release_time(order: Any) -> int:
    return next(e.ts_event for e in order.events() if type(e).__name__ == "OrderReleased")


# The dollar case. Shape A: lots at asks 10.00, 10.04 and 10.06 (10, 10 and 20 shares) average
# 10.04; the bid stays under that average's arming threshold until the last lot, climbs a cent at a
# time to about +10% (11.05), then falls 30% over 15 minutes on quotes six cents wide with a few
# hundred shares a side. Shape B: one lot of 10 at 10.00; the lock arms (floor 10.02, trigger 10.07),
# then the next quote's bid is 7.50, 25% under entry, far under both floors, and it stays there.
PEAK = Decimal("11.05")


def dollar_a_quotes() -> list[QuoteTick]:
    ticks = [quote("9.98", T0)]
    ticks += [quote("10.00", T0 + s * SECOND) for s in range(5, 60, 5)]
    ticks += [quote("10.02", T0 + s * SECOND) for s in range(60, 120, 5)]
    ticks += [quote("10.04", T0 + s * SECOND) for s in range(120, 130, 5)]
    cents = int((PEAK - Decimal("10.04")) * 100)
    for i in range(1, cents + 1):  # one cent every 2 s, as a bid climbs, so each cent is quoted
        ticks.append(quote(str(Decimal("10.04") + Decimal(i) / 100), T0 + (128 + 2 * i) * SECOND))
    start = T0 + (140 + 2 * cents) * SECOND
    for i in range(91):  # every 10 s for 900 s
        bid = PEAK * (1 - Decimal("0.30") * i / 90)
        size = 200 + 100 * (i % 3)
        ticks.append(
            thin(str(bid.quantize(Decimal("0.01"))), start + i * 10 * SECOND, bid_size=size)
        )
    return ticks


DOLLAR = Case(
    name="dollar",
    instrument=equity(),
    balance=100_000,
    bars=minute_bars("0.20"),
    level="9.80",
    a_lots={T0: 10, T0 + 60 * SECOND: 10, T0 + 120 * SECOND: 20},
    a_entry=Decimal("10.04"),
    a_quotes=dollar_a_quotes,
    b_lots={T0: 10},
    b_warm=lambda: [
        quote("9.98", T0),
        quote("10.00", T0 + SECOND),
        quote("10.10", T0 + 2 * SECOND),
    ],
    b_gap_bid=Decimal("7.50"),
    b_gap=lambda at: quote("7.50", at),
)


# The sub-dollar case, quoted in four decimals on the adapter's instrument: price precision
# canon_alpaca.constants.PRICE_PRECISION (4) and increment PRICE_INCREMENT (0.0001), which
# canon_alpaca.providers.equity_from_asset (providers.py:548-549) sets for every US equity, and
# under a dollar Alpaca's own step (price_step.tick: 0.0001; "Sub-penny increments", Orders at
# Alpaca). 500,000 shares. Shape A: lots of 100,000, 150,000 and 250,000 at asks 0.4213, 0.4219 and
# 0.4231 average 0.42238, off the step; the bid climbs a tick a second to about +10% (0.4646), then
# falls 30% over 15 minutes on quotes 25 ticks wide with a few hundred shares a side. Shape B: one
# lot of 500,000 at 0.4213; the lock arms on a bid of 0.4400, over its level (the row's whole-cent
# limit offset makes the level 0.0115 over the floor), then the bid is 0.3160, 25% under entry.
SUB = Decimal("0.0001")
SUB_PEAK = Decimal("0.4646")


def sub_bars(width: str = "0.0084", count: int = 14) -> list[Bar]:
    mid = Decimal("0.4200")
    return [
        Bar(
            BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"),
            Price.from_str(str(mid)),
            Price.from_str(str(mid + Decimal(width) / 2)),
            Price.from_str(str(mid - Decimal(width) / 2)),
            Price.from_str(str(mid)),
            Quantity.from_int(100_000),
            T0 - minute * 60 * SECOND,
            T0 - (minute - 1) * 60 * SECOND,
        )
        for minute in range(1, count + 1)
    ]


def sub_quote(bid: Decimal, at: int, spread: Decimal = Decimal("0.0006"), size: int = 1_000_000):
    return two_sided(f"{bid:.4f}", f"{bid + spread:.4f}", at, size, size)


def sub_a_quotes() -> list[QuoteTick]:
    ticks = [sub_quote(Decimal("0.4207"), T0)]
    ticks += [sub_quote(Decimal("0.4209"), T0 + s * SECOND) for s in range(5, 60, 5)]
    ticks += [sub_quote(Decimal("0.4213"), T0 + s * SECOND) for s in range(60, 120, 5)]
    ticks += [sub_quote(Decimal("0.4225"), T0 + s * SECOND) for s in range(120, 130, 5)]
    steps = int((SUB_PEAK - Decimal("0.4225")) / SUB)
    for i in range(1, steps + 1):  # one tick a second, so each four-decimal bid is quoted
        ticks.append(sub_quote(Decimal("0.4225") + i * SUB, T0 + (129 + i) * SECOND))
    start = T0 + (140 + steps) * SECOND
    for i in range(91):  # every 10 s for 900 s
        bid = (SUB_PEAK * (1 - Decimal("0.30") * i / 90)).quantize(SUB)
        size = 200 + 100 * (i % 3)
        ticks.append(
            two_sided(f"{bid:.4f}", f"{bid + 25 * SUB:.4f}", start + i * 10 * SECOND, size, 200)
        )
    return ticks


SUB_DOLLAR = Case(
    name="sub-dollar",
    instrument=Equity(
        instrument_id=AAA,
        raw_symbol=AAA.symbol,
        currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=PRICE_INCREMENT,
        ts_event=0,
        ts_init=0,
    ),
    balance=1_000_000,
    bars=sub_bars(),
    level="0.4129",
    a_lots={T0: 100_000, T0 + 60 * SECOND: 150_000, T0 + 120 * SECOND: 250_000},
    a_entry=Decimal("0.42238"),
    a_quotes=sub_a_quotes,
    b_lots={T0: 500_000},
    b_warm=lambda: [
        sub_quote(Decimal("0.4207"), T0),
        sub_quote(Decimal("0.4210"), T0 + SECOND),
        sub_quote(Decimal("0.4400"), T0 + 2 * SECOND),
    ],
    b_gap_bid=Decimal("0.3160"),
    b_gap=lambda at: sub_quote(Decimal("0.3160"), at),
)


# Shape A


@contextmanager
def shape_a(case: Case):
    protector = Watched(case.level)
    with replay(
        case, LateLots(case.a_lots), protector, account=AccountType.CASH, use_reduce_only=True
    ) as engine:
        feed(engine, case.a_quotes())
        yield engine, protector


def check_a_arms_on_weighted_average(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        held = engine.cache.positions_open(instrument_id=AAA, strategy_id=EXTERNAL)
        assert len(held) == 1 and held[0].quantity.as_decimal() == case.a_held
        assert Decimal(str(held[0].avg_px_open)) == case.a_entry
        armed = [e for e in protector.gain_lock_events if e["event"] == "armed"]
        assert len(armed) == 1, protector.gain_lock_events
        assert Decimal(armed[0]["entry"]) == case.a_entry
        assert armed[0]["armed_at"] > max(case.a_lots), "the lock armed before the last lot"


def check_a_no_trigger_under_entry_plus_cost(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        armed = protector.gain_lock_events[0]
        armed_at = armed["armed_at"]
        floor = Decimal(armed["entry"]) + Decimal(armed["round_trip_cost"])
        checked = 0
        for order in engine.cache.orders(instrument_id=AAA, strategy_id=protector.strategy_id):
            assert order.order_type in (OrderType.TRAILING_STOP_LIMIT, OrderType.LIMIT)
            # The trigger in force after each instant: the last event at that time sets it.
            in_force: dict[int, Decimal] = {}
            for event in order.events():
                trigger = getattr(event, "trigger_price", None)
                if trigger is not None and event.ts_event >= armed_at:
                    in_force[event.ts_event] = trigger.as_decimal()
            for at, trigger in in_force.items():
                checked += 1
                assert trigger >= floor, (
                    f"{order.client_order_id} trigger {trigger} at {at} under {floor}"
                )
        for at, _, working in protector.snapshots:
            if at < armed_at:
                continue
            for cid, _, _, _, _, trigger in working:
                if trigger is not None:
                    checked += 1
                    assert trigger >= floor, (cid, at, trigger)
        assert checked, "no working trigger was observed after arming"


def check_a_released_as_limit_extended_hours(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        out = released(engine, protector)
        assert len(out) == 1, "the shape must release the protective sale"
        assert out[0].order_type == OrderType.LIMIT and out[0].side == OrderSide.SELL
        assert out[0].is_reduce_only
        assert protector.submits, "the protector sent no sell"
        for order, params in protector.submits:
            assert order.side == OrderSide.SELL and order.is_reduce_only
            assert params == {"extended_hours": True}, (order.client_order_id, params)


def check_a_released_at_or_above_entry_less_cap(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        (order,) = released(engine, protector)
        at = release_time(order)
        # The released limit's price as released; the CASH venue then rejects it (harness limit).
        released_price = order.price.as_decimal()
        assert at >= protector.gain_lock_events[0]["armed_at"]
        assert released_price >= case.a_entry * (1 - CHASE_CAP), (
            f"released at {released_price}, under entry {case.a_entry} less the chase cap "
            f"{CHASE_CAP}"
        )


def check_a_arming_quote_does_not_release(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        (order,) = released(engine, protector)
        armed = protector.gain_lock_events[0]
        spacing = Decimal(armed["level"]) - Decimal(armed["applied_floor"])
        assert release_time(order) > armed["armed_at"], (
            f"the sale was released by the arming quote itself: level {armed['level']} = applied "
            f"floor {armed['applied_floor']} + spacing {spacing} is above the arming bid, since "
            f"the spacing exceeds the noise floor {armed['noise_floor']}"
        )


def check_prices_on_step(case: Case, engine: BacktestEngine, protector: Watched) -> None:
    """Every price the protector sent, and every released limit, is on the instrument's increment
    and on Alpaca's step at that price (price_step.tick); from arming on no trigger it sends, no
    floor it applies by modify, and neither of the lock's floors is under entry plus cost (never
    rounded down). A chase's plain limit is exempt: under R3 the exit beats the lock."""
    increment = case.instrument.price_increment.as_decimal()
    sent = list(protector.prices_sent)
    sent += [
        (release_time(o), "release", "limit", o.price.as_decimal())
        for o in released(engine, protector)
    ]
    assert sent, "the protector sent no price"
    for at, kind, name, price in sent:
        assert price % increment == 0, f"{kind} {name} {price} is off the increment {increment}"
        assert price % tick(price) == 0, f"{kind} {name} {price} is off Alpaca's step {tick(price)}"
    for event in protector.gain_lock_events:
        least = Decimal(event["entry"]) + Decimal(event["round_trip_cost"])
        for key in ("floor", "applied_floor"):
            assert Decimal(event[key]) >= least, (
                f"lock {key} {event[key]} rounded under entry {event['entry']} + cost "
                f"{event['round_trip_cost']} = {least}"
            )
        armed_at = event["armed_at"]
        for at, kind, name, price in sent:
            # A trigger, and the lock's floor applied by modify, never under entry plus cost. A
            # plain limit the chase sends may be: the exit beats the lock (R3).
            if at >= armed_at and (name == "trigger" or kind == "modify"):
                assert price >= least, (
                    f"{kind} {name} {price} after arming is under entry plus cost {least}"
                )


def check_a_prices_on_step(case: Case) -> None:
    with shape_a(case) as (engine, protector):
        assert protector.gain_lock_events, "the lock never armed"
        check_prices_on_step(case, engine, protector)


def test_shape_a_lock_arms_on_the_late_lots_weighted_average() -> None:
    check_a_arms_on_weighted_average(DOLLAR)


def test_shape_a_no_working_trigger_under_entry_plus_cost_from_arming_on() -> None:
    check_a_no_trigger_under_entry_plus_cost(DOLLAR)


def test_shape_a_sale_released_as_limit_and_every_sell_extended_hours() -> None:
    check_a_released_as_limit_extended_hours(DOLLAR)


def test_shape_a_released_limit_is_at_or_above_entry_less_chase_cap() -> None:
    check_a_released_at_or_above_entry_less_cap(DOLLAR)


def test_shape_a_arming_quote_does_not_release_the_sale() -> None:
    """Shape A's fall, not the arming quote, must release the sale (found by this shape).

    The lock's level is the applied floor plus the row's trigger-to-limit spacing
    (owner_protector.py `_gain_level`), and its arming threshold is entry + cost + noise. When the
    spacing exceeds the noise floor, a bid that arms at the threshold sits under the new level, and
    the pinned emulator rematches the modify at once (emulator.rs:814-928), releasing the sale.
    """
    check_a_arming_quote_does_not_release(DOLLAR)


def test_subdollar_shape_a_lock_arms_on_the_late_lots_weighted_average() -> None:
    check_a_arms_on_weighted_average(SUB_DOLLAR)


def test_subdollar_shape_a_no_working_trigger_under_entry_plus_cost_from_arming_on() -> None:
    check_a_no_trigger_under_entry_plus_cost(SUB_DOLLAR)


def test_subdollar_shape_a_sale_released_as_limit_and_every_sell_extended_hours() -> None:
    check_a_released_as_limit_extended_hours(SUB_DOLLAR)


def test_subdollar_shape_a_released_limit_is_at_or_above_entry_less_chase_cap() -> None:
    check_a_released_at_or_above_entry_less_cap(SUB_DOLLAR)


def test_subdollar_shape_a_arming_quote_does_not_release_the_sale() -> None:
    check_a_arming_quote_does_not_release(SUB_DOLLAR)


def test_subdollar_shape_a_every_price_sent_is_on_step_and_never_under_entry_plus_cost() -> None:
    check_a_prices_on_step(SUB_DOLLAR)


# Shape B


@contextmanager
def shape_b(case: Case):
    protector = Watched(case.level, venue_copy=True)
    with replay(
        case, LateLots(case.b_lots), protector, account=AccountType.MARGIN, use_reduce_only=False
    ) as engine:
        feed(engine, case.b_warm())
        assert [e["event"] for e in protector.gain_lock_events] == ["armed"]
        gap_at = T0 + 3 * SECOND
        feed(engine, [case.b_gap(gap_at + s * SECOND) for s in range(GAP_SECS)])
        yield engine, protector, gap_at


def sales(engine: BacktestEngine, protector: Watched) -> list[tuple[int, Decimal, str]]:
    """Each sale the venue held, by the time it was released or submitted, with its limit."""
    out = []
    for order in engine.cache.orders(instrument_id=AAA, strategy_id=protector.strategy_id):
        if order.order_type != OrderType.LIMIT:
            continue
        submitted = next(
            (e.ts_event for e in order.events() if type(e).__name__ == "OrderSubmitted"), None
        )
        if submitted is not None:
            out.append((submitted, order.price.as_decimal(), str(order.client_order_id)))
    return sorted(out)


MISSING = "the released sale is not re-priced through the floors after the wait"


def check_b_gap_releases(case: Case) -> None:
    with shape_b(case) as (engine, protector, gap_at):
        out = released(engine, protector)
        assert len(out) >= 1
        first = min(out, key=release_time)
        assert release_time(first) == gap_at
        assert first.order_type == OrderType.LIMIT
        assert first.price.as_decimal() > case.b_gap_bid, (
            "the gap leaves the released limit over the bid"
        )
        assert all(protector.asked[cid] is True for cid in protector.asked), protector.asked
        assert all(params == {"extended_hours": True} for _, params in protector.submits)


def check_b_repriced_to_chase_floor(case: Case) -> None:
    with shape_b(case) as (engine, protector, gap_at):
        first_price = min(released(engine, protector), key=release_time).price.as_decimal()
        floor = chase_floor(first_price, CHASE_CAP)
        repriced = [s for s in sales(engine, protector) if s[1] < first_price]
        assert repriced, (
            f"{MISSING}: after CHASE_SECS={CHASE_SECS} the sale released at {first_price} still "
            f"rests over a bid of {case.b_gap_bid}; no sale at the chase floor {floor} was sent "
            f"(mover.chase_floor, CHASE_CAP={CHASE_CAP})"
        )
        at, price, _ = repriced[0]
        assert gap_at + CHASE_SECS * SECOND <= at <= gap_at + PACE_NS, (
            f"first re-price at {(at - gap_at) / SECOND} s after release, due after {CHASE_SECS} s"
        )
        assert price == floor, f"{MISSING}: first re-price {price}, chase floor {floor}"


def check_b_follows_after_floor_wait(case: Case) -> None:
    with shape_b(case) as (engine, protector, gap_at):
        first_price = min(released(engine, protector), key=release_time).price.as_decimal()
        floor = chase_floor(first_price, CHASE_CAP)
        all_sales = sales(engine, protector)
        at_floor = [s for s in all_sales if s[1] == floor]
        through = [s for s in all_sales if s[1] <= case.b_gap_bid]
        assert at_floor and through, (
            f"{MISSING}: after FLOOR_WAIT_SECS={FLOOR_WAIT_SECS} resting at the chase floor "
            f"{floor} with the bid {case.b_gap_bid} through it, no sale at or under the bid was "
            f"sent; sales sent (s after release, limit): "
            f"{[((t - gap_at) / SECOND, str(p)) for t, p, _ in all_sales]}"
        )
        floor_at, follow_at = at_floor[0][0], through[0][0]
        assert all(p >= floor for t, p, _ in all_sales if t < follow_at), (
            "a sale went under the chase floor before the floor was released"
        )
        assert floor_at + FLOOR_WAIT_SECS * SECOND <= follow_at, (
            f"the floor was dropped {(follow_at - floor_at) / SECOND} s after the first sale at "
            f"it, before FLOOR_WAIT_SECS={FLOOR_WAIT_SECS}"
        )
        assert follow_at <= floor_at + FLOOR_WAIT_SECS * SECOND + 2 * PACE_NS, (
            f"the sale followed the bid {(follow_at - floor_at) / SECOND} s after reaching the "
            f"floor, over FLOOR_WAIT_SECS={FLOOR_WAIT_SECS} and two chase paces"
        )


def check_b_exactly_one_working_sale(case: Case) -> None:
    with shape_b(case) as (engine, protector, gap_at):
        held = case.b_held
        follow = [t for t, p, _ in sales(engine, protector) if p <= case.b_gap_bid]
        end = follow[0] if follow else None
        ours = {cid for _, cid in protector.cancels}
        empty_since: int | None = None
        steps = 0
        for at, _, working in protector.snapshots:
            if at < gap_at or (end is not None and at > end):
                continue
            steps += 1
            assert len(working) <= 1, f"two working sales at {(at - gap_at) / SECOND} s: {working}"
            if not working:
                last = [
                    o
                    for o in engine.cache.orders(
                        instrument_id=AAA, strategy_id=protector.strategy_id
                    )
                    if o.is_closed and o.ts_last <= at
                ]
                assert last and last[-1].status == OrderStatus.CANCELED, (
                    f"no working sale at {(at - gap_at) / SECOND} s, and not after our cancel"
                )
                assert str(last[-1].client_order_id) in ours
                empty_since = at if empty_since is None else empty_since
                assert at - empty_since <= RESEND_GAP_NS, (
                    f"no working sale for {(at - empty_since) / SECOND} s, over the Mover's "
                    f"cancel-to-resend gap of STEP_SECS={STEP_SECS}"
                )
                continue
            empty_since = None
            leaves = working[0][4]
            assert leaves == held, f"working sale for {leaves}, holding {held}"
        window = ((gap_at + GAP_SECS * SECOND) if end is None else end) - gap_at
        assert steps >= window // (STEP_SECS * SECOND), (
            f"only {steps} steps were observed in {window / SECOND} s after the gap"
        )


def check_b_prices_on_step(case: Case) -> None:
    with shape_b(case) as (engine, protector, _):
        check_prices_on_step(case, engine, protector)
        for at, price, cid in sales(engine, protector):
            assert price % tick(price) == 0, f"sale {cid} at {price} is off Alpaca's step"


def test_shape_b_gap_releases_the_sale_at_the_lock_floor() -> None:
    check_b_gap_releases(DOLLAR)


def test_shape_b_released_sale_is_repriced_to_the_chase_floor_after_chase_secs() -> None:
    check_b_repriced_to_chase_floor(DOLLAR)


def test_shape_b_sale_follows_the_bid_after_floor_wait_secs_at_the_chase_floor() -> None:
    check_b_follows_after_floor_wait(DOLLAR)


def test_shape_b_exactly_one_working_sale_for_the_held_quantity_at_every_step() -> None:
    check_b_exactly_one_working_sale(DOLLAR)


def test_subdollar_shape_b_gap_releases_the_sale_at_the_lock_floor() -> None:
    check_b_gap_releases(SUB_DOLLAR)


def test_subdollar_shape_b_released_sale_is_repriced_to_the_chase_floor_after_chase_secs() -> None:
    check_b_repriced_to_chase_floor(SUB_DOLLAR)


def test_subdollar_shape_b_sale_follows_the_bid_after_floor_wait_secs_at_the_chase_floor() -> None:
    check_b_follows_after_floor_wait(SUB_DOLLAR)


def test_subdollar_shape_b_exactly_one_working_sale_for_the_held_quantity_at_every_step() -> None:
    check_b_exactly_one_working_sale(SUB_DOLLAR)


def test_subdollar_shape_b_every_price_sent_is_on_step_and_never_under_entry_plus_cost() -> None:
    check_b_prices_on_step(SUB_DOLLAR)
