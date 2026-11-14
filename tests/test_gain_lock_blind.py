"""Blind tests for the gain lock on OwnerProtector and the Mover.

No implementation of `src/canon_trading/owner_protector.py`, `mover.py`,
`trail.py`, `trail_profile.py` or `protect.py` was opened. The seams are the
`OwnerProtector` / `OwnerProtectorConfig` constructor and `adopt_holding`, and
the Mover's config with the orders it sends; both run through the existing
offline `BacktestEngine` harnesses (`tests.test_owner_protector_blind` and
`tests.test_mover`). Every order under test is a real emulated order.

Read under the blind rules: `docs/handbook/practices.md:38` and `:41`,
`docs/adr/0088-the-mover-strategy-class.md:140`,
`docs/adr/0083-strategy-focus-extreme-movers-from-upstream-parts.md:29-31`,
`docs/roadmap.md:15`, and the fixtures/helpers of the three named test files.

The requirement states the gain lock without numbers. `ROUND_TRIP_COST`,
`NOISE_FLOOR` and `CONFIRMATION_BARS` below are named placeholders: each case
asserts the property (the lock arms above entry plus cost plus noise; once
armed no protective sale is below entry plus cost; an unknown input leaves it
unarmed), never that an implementation chose these values.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import strategy
from tests.test_mover import tape
from tests.test_owner_protector_blind import EXTERNAL
from tests.test_owner_protector_blind import owner_type
from tests.test_owner_protector_blind import replay

# Placeholders for the numbers the requirement does not state. They are shared
# by every case below and are never asserted as the implementation's choice.
# ROUND_TRIP_COST follows the lead's cost ruling of 2026-11-13: fees per share, zero on paper (the
# spread is already inside the fill and the bid).
ROUND_TRIP_COST = Decimal(0)
NOISE_FLOOR = Decimal("0.0025")
ARMING_GAIN = ROUND_TRIP_COST + NOISE_FLOOR
CONFIRMATION_BARS = 2

CENT = Decimal("0.01")
STEP = Decimal("0.001")
SPREAD = Decimal("0.01")
PROTECTOR_LEVEL = Price.from_str("9.99")
PROTECTOR_BAR_TYPE = BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override conftest's credential-environment reader for this offline file."""


def tick(ts_ns: int, bid: Decimal, *, iid: Any = AAA) -> QuoteTick:
    return QuoteTick(
        iid,
        Price.from_str(f"{bid:.4f}"),
        Price.from_str(f"{bid + SPREAD:.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        ts_ns,
        ts_ns,
    )


def minute_bar(ts_open: int, price: Decimal, bar_type_: BarType) -> Bar:
    half = Decimal("0.04") / 2
    return Bar(
        bar_type_,
        Price.from_str(f"{price:.4f}"),
        Price.from_str(f"{price + half:.4f}"),
        Price.from_str(f"{price - half:.4f}"),
        Price.from_str(f"{price:.4f}"),
        Quantity.from_int(1_000),
        ts_open,
        ts_open + MINUTE,
    )


class Session:
    """Streams quotes and completed bars into one already-started engine."""

    def __init__(
        self,
        eng: Any,
        seller_id: Any,
        position_id: Any,
        cursor_ns: int,
        bar_type_: BarType,
        subject: Any = None,
    ) -> None:
        self.engine = eng
        self.seller_id = seller_id
        self.position_id = position_id
        self.cursor_ns = cursor_ns
        self.bar_type = bar_type_
        self.subject = subject

    def feed(self, items: Iterable[Any]) -> None:
        data = sorted(items, key=lambda item: item.ts_init)
        assert data, "a session step needs at least one item"
        self.engine.clear_data()
        self.engine.add_data(data)
        self.engine.run(streaming=True)
        self.cursor_ns = max(self.cursor_ns, data[-1].ts_init)

    def quotes(self, bids: Iterable[Decimal], *, repeats: int = 2) -> None:
        items: list[Any] = []
        for bid in bids:
            for _ in range(repeats):
                self.cursor_ns += SECOND
                items.append(tick(self.cursor_ns, bid))
        self.feed(items)

    def confirm(self, price: Decimal, *, bars: int = CONFIRMATION_BARS) -> None:
        items: list[Any] = []
        minute_open = ((self.cursor_ns // MINUTE) + 1) * MINUTE
        for index in range(bars):
            start = minute_open + index * MINUTE
            for second in (5, 25, 45):
                items.append(tick(start + second * SECOND, price))
            items.append(minute_bar(start, price, self.bar_type))
        self.cursor_ns = minute_open + bars * MINUTE + SECOND
        items.append(tick(self.cursor_ns, price))
        self.feed(items)

    def descend(
        self,
        start: Decimal,
        stop: Decimal,
        *,
        step: Decimal = STEP,
        repeats: int = 2,
    ) -> None:
        bids: list[Decimal] = []
        current = start
        while current >= stop:
            bids.append(current)
            current -= step
        assert bids, "the descent must start at or above its stop"
        self.quotes(bids, repeats=repeats)

    def all_sells(self) -> list[Any]:
        return list(self.engine.cache.orders(strategy_id=self.seller_id, side=OrderSide.SELL))

    def open_sells(self) -> list[Any]:
        # An emulated order is not `is_open` at the pin (`Order::is_open` returns
        # false whenever an emulation trigger is set), so "working" is `not closed`.
        return [order for order in self.all_sells() if not order.is_closed]

    def entry(self) -> Decimal:
        positions = self.engine.cache.positions(instrument_id=AAA, strategy_id=self.position_id)
        assert positions, "expected the held position in the cache"
        return Decimal(str(positions[0].avg_px_open))

    def level(self) -> Decimal:
        sells = self.open_sells()
        assert sells, "expected an open protective sell"
        trigger = getattr(sells[-1], "trigger_price", None)
        assert trigger is not None, "expected the protective sell to carry a trigger"
        return trigger.as_decimal()

    def trail(self) -> Decimal:
        sells = self.open_sells()
        assert sells, "expected an open protective sell"
        offset = getattr(sells[-1], "trailing_offset", None)
        assert offset is not None, "expected the protective sell to be a trailing stop"
        return Decimal(str(offset))

    def elections(self) -> list[Any]:
        return [
            order
            for order in self.all_sells()
            if order.filled_qty.as_decimal() > 0
            or order.order_type in {OrderType.LIMIT, OrderType.MARKET}
            or any(type(event).__name__ == "OrderReleased" for event in order.events())
        ]

    def fill_prices(self) -> list[Decimal]:
        return [
            Decimal(str(order.avg_px))
            for order in self.all_sells()
            if order.filled_qty.as_decimal() > 0
        ]


class OpaquePosition:
    """A position whose cost basis cannot be read, with its identity intact."""

    def __init__(self, native: Any) -> None:
        self._native = native

    @property
    def avg_px_open(self) -> Decimal:
        return Decimal(0)

    @property
    def last_event(self) -> None:
        return None

    def events(self) -> list[Any]:
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)


class OpaqueCache:
    """Hides the EXTERNAL cost basis or the instrument quotes on documented queries."""

    def __init__(
        self,
        native: Any,
        *,
        hide_cost_basis: bool = False,
        hide_quotes: bool = False,
    ) -> None:
        self._native = native
        self.hide_cost_basis = hide_cost_basis
        self.hide_quotes = hide_quotes

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._native, name)
        if self.hide_quotes and name in {"quote", "quotes"}:
            if name == "quote":
                return lambda *_args, **_kwargs: None
            return lambda *_args, **_kwargs: []
        if self.hide_cost_basis and name in {
            "position",
            "position_for_order",
            "positions",
            "positions_open",
        }:

            def query_positions(*args: Any, **kwargs: Any) -> Any:
                result = member(*args, **kwargs)

                def wrap(position: Any) -> Any:
                    if position is not None and position.strategy_id == EXTERNAL:
                        return OpaquePosition(position)
                    return position

                if isinstance(result, list):
                    return [wrap(position) for position in result]
                return wrap(result)

            return query_positions
        if self.hide_cost_basis and name in {"orders", "orders_closed", "order"}:

            def query_orders(*args: Any, **kwargs: Any) -> Any:
                result = member(*args, **kwargs)
                if isinstance(result, list):
                    return [order for order in result if order.strategy_id != EXTERNAL]
                if result is not None and result.strategy_id == EXTERNAL:
                    return None
                return result

            return query_orders
        return member


def protector_type() -> tuple[Any, Any]:
    configure, base = owner_type()

    class Observable(base):
        """The blind harness protector, with toggles for the unknown-input cases."""

        def __init__(self, config: Any) -> None:
            self.hide_cost_basis = False
            self.hide_quotes = False
            self.suppress_quotes = False
            super().__init__(config)

        @property
        def cache(self) -> Any:
            native = super().cache
            if not self.hide_cost_basis and not self.hide_quotes:
                return native
            return OpaqueCache(
                native,
                hide_cost_basis=self.hide_cost_basis,
                hide_quotes=self.hide_quotes,
            )

        def on_quote(self, quote: QuoteTick) -> None:
            if self.suppress_quotes:
                return
            super().on_quote(quote)

    return configure, Observable


@contextmanager
def protector_session(*, hide_cost_basis: bool = False):
    configure, observable = protector_type()
    config = configure(feed="sip", levels={AAA: PROTECTOR_LEVEL}, gain_lock=True)
    subject = observable(config)
    subject.hide_cost_basis = hide_cost_basis
    with replay(strategies=(subject,)) as harness:
        session = Session(
            harness.engine,
            subject.strategy_id,
            EXTERNAL,
            T0 + 5 * SECOND,
            PROTECTOR_BAR_TYPE,
            subject,
        )
        assert session.open_sells(), "setup must arm an initial protective sell"
        yield session


@contextmanager
def mover_session():
    eng = engine()
    subject = strategy()
    ranking = ranker(rank((AAA, "0.25")))
    eng.add_strategy(subject)
    eng.add_strategy(ranking)
    try:
        items = sorted(tape(AAA, RISING), key=lambda item: item.ts_init)
        cursor_ns = T0
        index = 0
        found = False
        while index < len(items):
            ts_ns = items[index].ts_init
            batch: list[Any] = []
            while index < len(items) and items[index].ts_init == ts_ns:
                batch.append(items[index])
                index += 1
            cursor_ns = ts_ns
            eng.clear_data()
            eng.add_data(batch)
            eng.run(streaming=True)
            positions = eng.cache.positions_open(instrument_id=AAA, strategy_id=subject.strategy_id)
            sells = [
                order
                for order in eng.cache.orders(strategy_id=subject.strategy_id, side=OrderSide.SELL)
                if not order.is_closed
            ]
            if positions and sells:
                found = True
                break
        assert found, "setup must enter and arm an initial protective sell"
        session = Session(
            eng,
            subject.strategy_id,
            subject.strategy_id,
            cursor_ns,
            bar_type(AAA),
            subject,
        )
        assert session.open_sells(), "setup must arm an initial protective sell"
        yield session
    finally:
        eng.end()
        eng.dispose()


@contextmanager
def subject_session(subject: str):
    if subject == "protector":
        with protector_session() as session:
            yield session
    elif subject == "mover":
        with mover_session() as session:
            yield session
    else:
        raise ValueError(f"unknown subject: {subject}")


def arm_high(session: Session) -> Decimal:
    """Drive a completed confirmation above entry plus cost plus the noise floor."""
    entry = session.entry()
    trail = session.trail()
    assert trail > ARMING_GAIN, "the trail distance must exceed the arming placeholders"
    gain = (ARMING_GAIN + trail) / 2
    high = entry + gain
    session.confirm(high)
    return high


def sale_level(order: Any) -> Decimal | None:
    """The released sale's limit price, else its trigger, else None."""
    for name in ("price", "trigger_price"):
        value = getattr(order, name, None)
        if value is not None:
            return value.as_decimal()
    return None


def assert_no_sale_below_floor(session: Session) -> None:
    """Once armed, a fall must not permit a protective sale below entry plus cost."""
    initial = session.level()
    peak = arm_high(session)
    entry = session.entry()
    session.descend(peak, initial - Decimal("0.05"))
    released = session.elections()
    assert released, "the fall must release the protective stop"
    floor = entry + ROUND_TRIP_COST
    # The lead's R3 (2026-11-13): the lock sets the trigger and the first released limit; the
    # chase that follows may go under it ("the exit beats the lock"). So the floor binds the first
    # sale the fall releases, and any fill of that sale, not the re-priced legs after it.
    first = min(released, key=lambda order: order.ts_init)
    limits = [level for level in (sale_level(order) for order in [first]) if level is not None]
    assert all(level >= floor for level in limits), (
        f"protective sale limit(s) {limits} below entry plus round-trip cost {floor}"
    )
    prices = [Decimal(str(first.avg_px))] if first.filled_qty.as_decimal() > 0 else []
    assert all(price >= floor for price in prices), (
        f"protective sale filled at {prices}, below entry plus round-trip cost {floor}"
    )


def assert_stop_follows_gain(session: Session) -> None:
    """The armed level sits above cost and rises with a higher high, never falling."""
    entry = session.entry()
    trail = session.trail()
    assert trail > ARMING_GAIN, "the trail distance must exceed the arming placeholders"
    gain = (ARMING_GAIN + trail) / 2
    floor = entry + ROUND_TRIP_COST

    first_high = entry + gain
    session.confirm(first_high)
    level_at_first_high = session.level()
    assert level_at_first_high >= floor, (
        f"armed gain-lock level {level_at_first_high} is below entry plus cost {floor}"
    )

    second_high = entry + trail + gain
    session.confirm(second_high)
    level_at_second_high = session.level()
    assert level_at_second_high > level_at_first_high, (
        f"protective level did not rise with the high: {level_at_second_high} "
        f"is not above {level_at_first_high}"
    )

    dip = second_high - trail / 4
    session.quotes([dip])
    assert session.level() >= level_at_second_high, (
        f"protective level fell to {session.level()} from {level_at_second_high}"
    )


def assert_known_input_arms_lock() -> None:
    """Matched control: known cost basis and quotes arm the gain lock."""
    with protector_session() as session:
        entry = session.entry()
        trail = session.trail()
        gain = (ARMING_GAIN + trail) / 2
        session.confirm(entry + gain)
        floor = entry + ROUND_TRIP_COST
        level = session.level()
        assert level >= floor, (
            f"control: known cost basis and quote must arm the lock; level {level} is below {floor}"
        )


def test_gain_lock_protector_sale_never_below_entry_plus_cost() -> None:
    """Case 1 (OwnerProtector): in gain by less than one trail, then falls.

    Rests on `docs/adr/0083-strategy-focus-extreme-movers-from-upstream-parts.md:29-31`
    and `docs/handbook/practices.md:38,41`: once in gain past the arming point, no
    protective sale is below entry plus round-trip cost. The cost, noise floor and
    confirmation count are placeholders (the requirement states no numbers).
    """
    with protector_session() as session:
        assert_no_sale_below_floor(session)


@pytest.mark.xfail(strict=True, reason="the Mover's gain lock is its own unit (mgl-build2)")
def test_gain_lock_mover_sale_never_below_entry_plus_cost() -> None:
    """Case 1 (Mover): in gain by less than one trail, then falls.

    Rests on `docs/adr/0083-strategy-focus-extreme-movers-from-upstream-parts.md:29-31`,
    `docs/adr/0088-the-mover-strategy-class.md:140` and
    `docs/handbook/practices.md:38,41`: once in gain past the arming point, no
    protective sale is below entry plus round-trip cost. The cost, noise floor and
    confirmation count are placeholders (the requirement states no numbers).
    """
    with mover_session() as session:
        assert_no_sale_below_floor(session)


@pytest.mark.parametrize(
    "subject",
    [
        "protector",
        pytest.param(
            "mover",
            marks=pytest.mark.xfail(
                strict=True, reason="the Mover's gain lock is its own unit (mgl-build2)"
            ),
        ),
    ],
)
def test_gain_lock_stop_follows_gain_and_never_falls(subject: str) -> None:
    """Case 2: as the high rises the protective level rises, and never falls.

    Rests on `docs/handbook/practices.md:41` ("Every holding in gain has protection
    that follows the gain") and `docs/adr/0088-the-mover-strategy-class.md:140`.
    The cost, noise floor and confirmation count are placeholders (the requirement
    states no numbers).
    """
    with subject_session(subject) as session:
        assert_stop_follows_gain(session)


@pytest.mark.parametrize("subject", ["protector", "mover"])
def test_gain_lock_guard_no_gain_keeps_plain_trail(subject: str) -> None:
    """GUARD (Case 3): never in gain past the arming point, so today's trail stands.

    A guard expected to pass at the base. Rests on `docs/handbook/practices.md:38`:
    the lock arms only on a confirmation above entry plus cost plus the noise floor.
    The cost, noise floor and confirmation count are placeholders (the requirement
    states no numbers).
    """
    with subject_session(subject) as session:
        entry = session.entry()
        initial = session.level()
        assert initial < entry, "setup must start on a below-entry plain trail"
        session.quotes([entry - CENT])
        session.descend(initial + CENT, initial - 2 * CENT)
        released = session.elections()
        assert released, "today's plain trail must still release"
        levels = [level for level in (sale_level(order) for order in released) if level is not None]
        assert levels and min(levels) < entry, (
            "a holding never in gain must keep the below-entry plain trail"
        )


def test_gain_lock_unknown_cost_basis_stays_unarmed() -> None:
    """Case 4: an unknown cost basis leaves the lock unarmed, never armed at a guess.

    Rests on `docs/handbook/practices.md:38`: "Arming only on a completed
    confirmation above entry plus round-trip cost plus the noise floor, with an
    unknown cost basis or quote leaving it unarmed". The cost, noise floor and
    confirmation count are placeholders (the requirement states no numbers). The
    matched known-input control fails at the base because the lock is not built.
    """
    with protector_session(hide_cost_basis=True) as session:
        entry = session.entry()
        trail = session.trail()
        gain = (ARMING_GAIN + trail) / 2
        floor = entry + ROUND_TRIP_COST
        probe = floor - ROUND_TRIP_COST / 2
        session.confirm(entry + gain)
        assert probe > session.level(), "the probe must sit above the unarmed plain trail"
        session.quotes([probe])
        assert session.elections() == [], "an unknown cost basis must leave the gain lock unarmed"
    assert_known_input_arms_lock()


def test_gain_lock_unknown_quote_stays_unarmed() -> None:
    """Case 4: an unknown quote leaves the lock unarmed, never armed at a guess.

    Rests on `docs/handbook/practices.md:38`: "Arming only on a completed
    confirmation above entry plus round-trip cost plus the noise floor, with an
    unknown cost basis or quote leaving it unarmed". The cost, noise floor and
    confirmation count are placeholders (the requirement states no numbers). The
    matched known-input control fails at the base because the lock is not built.
    """
    with protector_session() as session:
        entry = session.entry()
        trail = session.trail()
        gain = (ARMING_GAIN + trail) / 2
        floor = entry + ROUND_TRIP_COST
        probe = floor - ROUND_TRIP_COST / 2
        session.subject.suppress_quotes = True
        session.subject.hide_quotes = True
        session.quotes([entry + gain] * 3)
        session.subject.suppress_quotes = False
        session.subject.hide_quotes = False
        assert probe > session.level(), "the probe must sit above the unarmed plain trail"
        session.quotes([probe])
        assert session.elections() == [], "an unknown quote must leave the gain lock unarmed"
    assert_known_input_arms_lock()
