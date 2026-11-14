"""Unit 12's failing test first: a second template gets protection automatically.

Unit 12 is the last row of the 24/5 plan's Units table: unit 11 extracts a `Protection` part from
the Mover, and this unit's second template is NautilusTrader's own EMA-cross shape, arming on the
position's opening event (`plan-24-5-protection-2026-11-13.md:118`, `:154-162`; decision D at
`:88-98`, and FINISH LINE F5 at `:30-32`).

THE SEAM this file holds (named by the coordinator; unit 11's builder is held to it):
`canon_trading.protection`, with `Protection` and `ProtectionConfig`. The part is built inside the
strategy as `Protection(strategy, ProtectionConfig())` and is driven by that strategy's own
callbacks, which it must accept: `on_position_opened`, `on_position_changed`,
`on_position_closed`, `on_quote`. It submits through `strategy.submit_order(order, params=...)` and
removes a sell through `strategy.cancel_order(...)`. `ProtectionConfig()` needs no arguments: its
defaults are the protection, a level taken from the position's average fill, a second observation
required before that level may elect, and every protective sell reduce-only and marked
`extended_hours`.

Contract at the pin (`nautilus_trader@d53849b216e2`, v2.0.0rc8), unless named otherwise:
- arming on the position's opening event: v1's
  `nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:379-385` arms in `on_event` on
  `PositionOpened | PositionChanged`; the pin's `python/tests/strategies/acceptance.py:578-588`
  arms on the entry fill instead, and `:631-657` submits the trailing sell there. The plan's
  correction at `:154-162` reads the position event as the shape to follow.
- `reduce_only=True` on that sell: `acceptance.py:653`; `OrderFactory.trailing_stop_limit` takes
  it, `python/nautilus_trader/common/__init__.pyi:1523-1545`.
- `submit_order(order, params=...)`: `python/nautilus_trader/trading/__init__.pyi:538-544`. The
  adapter's extended-hours mark is `params={"extended_hours": True}` on a limit DAY or GTC order:
  `packages/canon-alpaca/src/canon_alpaca/execution.py:257-258` and `:2545-2546` (ADR 0038).
- position callbacks are dispatched to `on_position_opened/changed/closed` at the pin:
  `crates/trading/src/python/strategy.rs:608-639`, `python/nautilus_trader/trading/__init__.pyi:470-473`.
- the pinned emulator checks a sell stop as `bid <= trigger` on each quote
  (`crates/execution/src/matching.rs:555-579`), and a trailing trigger only rises
  (`crates/execution/src/trailing.rs:61-65`). A part that leaves election to the emulator
  therefore fails property 2's first case, which is the property: one observation may not elect.

At this base the seam does not exist, so every case fails on
`ModuleNotFoundError: No module named 'canon_trading.protection'` -- the expected ImportError. The
file collects, so each case is reported separately. Nothing here writes the paper state, the
catalog or the ledger, and no venue or credential is touched: the engine is in memory.

UNVERIFIED: the constructor order `Protection(strategy, config)` and the four callback names above
are this harness's contract, not a read of unit 11's implementation; none is in this tree and none
was looked for.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionChanged
from nautilus_trader.model import PositionClosed
from nautilus_trader.model import PositionOpened
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from tests.test_mover import AAA
from tests.test_mover import engine
from tests.test_mover import quote

TEMPLATE_ID = StrategyId("Template-001")
LIMIT_ORDERS = (OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT)
EXTENDED_HOURS_TIFS = (TimeInForce.DAY, TimeInForce.GTC)


def seam() -> Any:
    """The named seam, imported when a case runs so this file still collects before it exists."""
    return importlib.import_module("canon_trading.protection")


@dataclass(frozen=True)
class Submission:
    """What the template handed the engine, recorded before the engine saw it: a later remake
    cannot rewrite what was sent."""

    client_order_id: str
    side: Any
    order_type: Any
    quantity: Decimal
    reduce_only: bool
    time_in_force: Any
    params: dict | None


class TemplateConfig(StrategyConfig):
    """One config object, as every NautilusTrader strategy is built: the template's entry and
    close are its own test knobs, not the protection's."""

    def __init__(
        self,
        *,
        quantity: int = 10,
        entry_limit: str | None = None,
        flatten_on_quote: int | None = None,
        **_kwargs: Any,
    ) -> None:
        super().__init__()
        self.quantity = quantity
        self.entry_limit = entry_limit
        self.flatten_on_quote = flatten_on_quote


class Template(Strategy):
    """NautilusTrader's EMA-cross shape at the pin, cut to what protection needs: one entry, then
    every position and quote event forwarded to the one `Protection` part. The part, not this
    template, decides and sends every sale.

    Arming rides the position's opening event, as v1's example does
    (`ema_cross_trailing_stop.py:379-385`), not the entry's fill (`acceptance.py:578-588`).
    """

    def __init__(self, config: TemplateConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str("AAA.ALPACA")
        self.quantity = config.quantity
        self.entry_limit = config.entry_limit
        self.flatten_on_quote = config.flatten_on_quote
        self.quotes = 0
        self.submitted: list[Submission] = []
        self.canceled: list[str] = []
        self.protection = seam().Protection(self, seam().ProtectionConfig())

    def on_start(self) -> None:
        self.subscribe_quotes(self.instrument_id)
        if self.entry_limit is None:
            order = self.order_factory.market(
                self.instrument_id, OrderSide.BUY, Quantity.from_int(self.quantity)
            )
        else:
            order = self.order_factory.limit(
                self.instrument_id,
                OrderSide.BUY,
                Quantity.from_int(self.quantity),
                Price.from_str(self.entry_limit),
                time_in_force=TimeInForce.DAY,
            )
        self.submit_order(order)

    def on_quote(self, quote: QuoteTick) -> None:
        self.protection.on_quote(quote)
        self.quotes += 1
        if self.flatten_on_quote is not None and self.quotes == self.flatten_on_quote:
            self._flatten()

    def on_position_opened(self, event: PositionOpened) -> None:
        self.protection.on_position_opened(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        self.protection.on_position_changed(event)

    def on_position_closed(self, event: PositionClosed) -> None:
        self.protection.on_position_closed(event)

    def _flatten(self) -> None:
        held = self.portfolio.net_position(self.instrument_id)
        if held > 0:
            self.submit_order(
                self.order_factory.market(
                    self.instrument_id, OrderSide.SELL, Quantity.from_int(int(held))
                )
            )

    def submit_order(
        self,
        order: Any,
        position_id: Any = None,
        client_id: Any = None,
        params: dict | None = None,
    ) -> None:
        self.submitted.append(
            Submission(
                client_order_id=str(order.client_order_id.value),
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity.as_decimal(),
                reduce_only=bool(order.is_reduce_only),
                time_in_force=order.time_in_force,
                params=None if params is None else dict(params),
            )
        )
        super().submit_order(order, position_id, client_id, params)

    def cancel_order(
        self,
        client_order_id: Any,
        client_id: Any = None,
        params: dict | None = None,
    ) -> None:
        self.canceled.append(str(getattr(client_order_id, "value", client_order_id)))
        super().cancel_order(client_order_id, client_id, params)


def template(**options: Any) -> Template:
    """The second template with a named strategy ID, so its orders are found in the cache.

    At the pin the strategy ID is composed with the order ID tag (`strategy/core.rs:159-166`,
    `strategy_id_with_order_id_tag`), so passing only an ID whose tag is whole ("-001") keeps the
    ID the tests query.
    """
    return Template(TemplateConfig(strategy_id=TEMPLATE_ID, **options))


def sells(strategy: Template) -> list[Submission]:
    return [sent for sent in strategy.submitted if sent.side == OrderSide.SELL]


def buys(strategy: Template) -> list[Submission]:
    return [sent for sent in strategy.submitted if sent.side == OrderSide.BUY]


def cache_orders(eng: BacktestEngine) -> list[Any]:
    return list(eng.cache.orders(strategy_id=TEMPLATE_ID))


def working_sells(eng: BacktestEngine) -> list[Any]:
    return [
        order for order in cache_orders(eng) if order.side == OrderSide.SELL and not order.is_closed
    ]


def order_with(eng: BacktestEngine, client_order_id: str) -> Any:
    for order in cache_orders(eng):
        if order.client_order_id.value == client_order_id:
            return order
    raise AssertionError(f"no order {client_order_id} in the engine cache")


def sold(eng: BacktestEngine) -> Decimal:
    return sum(
        (
            order.filled_qty.as_decimal()
            for order in cache_orders(eng)
            if order.side == OrderSide.SELL
        ),
        Decimal(0),
    )


def marked_for_extended_hours(sent: Submission) -> bool:
    return sent.params is not None and sent.params.get("extended_hours") is True


@pytest.fixture
def drive() -> Iterator[Callable[..., BacktestEngine]]:
    """Run any strategy over hand-built quote ticks and hand back the engine, disposed after the
    case. Fills and quotes are the real backtest engine's; nothing is read from a venue or disk."""

    engines: list[BacktestEngine] = []

    def run(strategy: Strategy, data: list[Any], *, liquid: bool = False) -> BacktestEngine:
        eng = engine(liquid=liquid)
        engines.append(eng)
        eng.add_data(data)
        eng.add_strategy(strategy)
        eng.run()
        return eng

    yield run
    for eng in engines:
        eng.dispose()


# --- Property 1: an entry's fill arms exactly one protective sell -------------------------------


def test_an_entry_fill_arms_one_reduce_only_extended_hours_sell_for_the_filled_quantity(drive):
    """The entry fills 10 at 10.01; the part sends one sell of 10, reduce-only, marked extended
    hours, with a limit order type and a time in force the adapter accepts that mark on."""
    template_ = template()
    eng = drive(
        template_,
        [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01), quote(2, 9.99, 10.01)],
    )
    assert [sent.quantity for sent in buys(template_)] == [Decimal(10)]
    armed = sells(template_)
    assert len(armed) == 1, f"one protective sell, got {[sent.quantity for sent in armed]}"
    sent = armed[0]
    assert sent.quantity == Decimal(10)
    assert sent.reduce_only
    assert marked_for_extended_hours(sent)
    assert sent.order_type in LIMIT_ORDERS
    assert sent.time_in_force in EXTENDED_HOURS_TIFS
    (working,) = working_sells(eng)
    assert working.quantity.as_decimal() == Decimal(10)


# --- Property 2: one observation never elects; a further one is needed --------------------------


@pytest.mark.xfail(
    strict=True,
    reason="unit 13: the breach follows arming and a healthy quote, so it needs a later-step guard",
)
def test_one_quote_at_the_level_does_not_elect_the_stop(drive):
    """A bid far under the level is one observation: the stop is not released, filled, canceled or
    replaced, and the long is still held.

    The level is the armed sell's own -- any positive trail under the 10.01 fill -- so a bid of
    0.99 is at or below it whatever the part's default distance. The pinned emulator checks a sell
    stop on every quote (`matching.rs:555-579`): a part that leaves election to it fails here,
    which is the property.
    """
    template_ = template()
    eng = drive(
        template_,
        [quote(0, 9.99, 10.01), quote(1, 9.99, 10.01), quote(2, 0.99, 1.01)],
    )
    armed = sells(template_)
    assert len(armed) == 1
    order = order_with(eng, armed[0].client_order_id)
    assert order.filled_qty.as_decimal() == 0
    assert not any(isinstance(event, OrderReleased) for event in order.events())
    assert template_.canceled == []
    assert len(sells(template_)) == 1
    assert eng.cache.positions_open(strategy_id=TEMPLATE_ID)
    assert sold(eng) == 0


@pytest.mark.xfail(
    strict=True,
    reason="unit 13: two breaches after an established trail need later-step confirmation",
)
def test_a_second_quote_at_the_level_elects_the_stop(drive):
    """The further observation elects it and the sale goes out: the long is flat and 10 shares were
    sold, every sell the template sent being reduce-only and marked extended hours."""
    template_ = template()
    eng = drive(
        template_,
        [
            quote(0, 9.99, 10.01),
            quote(1, 9.99, 10.01),
            quote(2, 0.99, 1.01),
            quote(3, 0.99, 1.01),
        ],
    )
    assert not eng.cache.positions_open(strategy_id=TEMPLATE_ID)
    assert sold(eng) == Decimal(10)
    assert sells(template_)
    assert all(sent.reduce_only for sent in sells(template_))
    assert all(marked_for_extended_hours(sent) for sent in sells(template_))


# --- Property 3: no second sell; a partial fill protects what filled and grows -------------------


def test_a_partial_entry_fill_protects_only_the_shares_that_filled(drive):
    """Four of the ten shares fill against a thin ask; the one protective sell is for four, and
    the four are what the position holds."""
    template_ = template(entry_limit="10.02")
    eng = drive(
        template_,
        [quote(0, 9.99, 10.01, ask_size=4), quote(1, 10.09, 10.10)],
        liquid=True,
    )
    armed = sells(template_)
    assert len(armed) == 1
    assert armed[0].quantity == Decimal(4)
    (working,) = working_sells(eng)
    assert working.quantity.as_decimal() == Decimal(4)
    (position,) = eng.cache.positions_open(strategy_id=TEMPLATE_ID)
    assert position.quantity.as_decimal() == Decimal(4)


def test_a_later_fill_grows_the_one_protective_sell_without_overselling(drive):
    """The next six shares fill; one working sell covers all ten -- never a four and a ten -- and
    the first cover was made for the four that had filled."""
    template_ = template(entry_limit="10.02")
    eng = drive(
        template_,
        [
            quote(0, 9.99, 10.01, ask_size=4),
            quote(1, 9.99, 10.01),
            quote(2, 10.09, 10.10),
        ],
        liquid=True,
    )
    working = working_sells(eng)
    assert len(working) == 1
    assert working[0].quantity.as_decimal() == Decimal(10)
    assert sum((order.quantity.as_decimal() for order in working), Decimal(0)) == Decimal(10)
    assert sells(template_)[0].quantity == Decimal(4)
    assert all(sent.reduce_only for sent in sells(template_))
    assert all(marked_for_extended_hours(sent) for sent in sells(template_))


# --- Property 4: the protective sell is gone when the holding closes ----------------------------


def test_the_protective_sell_is_gone_when_the_holding_closes(drive):
    """A take-profit market sell closes the long at 10.99; the part cancels its sell, so nothing
    is left working to sell another ten shares into a short."""
    template_ = template(flatten_on_quote=3)
    eng = drive(
        template_,
        [
            quote(0, 9.99, 10.01),
            quote(1, 10.49, 10.51),
            quote(2, 10.99, 11.01),
            quote(3, 10.99, 11.01),
        ],
    )
    assert not eng.cache.positions_open(strategy_id=TEMPLATE_ID)
    assert eng.portfolio.net_position(AAA) == 0
    assert working_sells(eng) == []
    first = sells(template_)[0]
    assert order_with(eng, first.client_order_id).is_closed
    assert first.client_order_id in template_.canceled
    take_profit = [sent for sent in sells(template_) if sent.order_type == OrderType.MARKET]
    assert [sent.quantity for sent in take_profit] == [Decimal(10)]
    assert sold(eng) == Decimal(10)  # the take-profit sold the long; the stop sold nothing
