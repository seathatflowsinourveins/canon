"""Unit 10 blind properties at 05905849; no owner-protector implementation was read.

Contracts read at NautilusTrader d53849b216e2 (paths relative to that pinned source):
* crates/backtest/src/engine.rs:753-840: `run_impl` starts the trader and all registered
  strategies only on its first iteration (:832); a later streaming run does not start a
  strategy added afterwards, and `BacktestEngine` exposes no trader handle. So every
  strategy here is registered before the engine's first run and started by the engine.
* crates/system/src/trader.rs:823-835,867,988: a strategy may be registered while the
  trader runs, but registration is not a start; `start_components`/`start_component` is
  the path that runs `on_start` with a usable clock, while a manual `Strategy.start()`
  raises "Already mutably borrowed" from `clock.set_timer` (observed).
* docs/concepts/execution/reconciliation.md:305-335: reduce EXTERNAL under NETTING,
  preserving its ownership; :370-386: instrument claims change order ownership.
* crates/model/src/identifiers/strategy_id.rs:110-128 and
  crates/trading/src/strategy/core.rs:409-420: the EXTERNAL tag must remain EXTERNAL.
* python/nautilus_trader/trading/__init__.pyi:482-650,1021-1049: Strategy lifecycle,
  cache, submit/cancel/modify/claim seams, callbacks and StrategyConfig.
* python/nautilus_trader/common/__init__.pyi:438-485,558-585,629-634,1375-1410:
  cache queries, clock and order factory; model/__init__.pyi:2639-2805,5810-5895,
  6185-6230,6315-6420: order/position observations, prices, quantities and quotes.

The engine/instruments are tests.test_mover's existing offline harness. A seed strategy is
registered before OwnerProtector; its simulated fill creates the EXTERNAL long and its
working sell (also EXTERNAL) is real cached state. All tested orders pass through the real
engine; there is no adapter, paper state, catalog, ledger, credential or network access.

The given level is supplied through `OwnerProtectorConfig`'s public `levels` constructor
field (read from the seam's signature, not from the implementation). Every case still
judges behaviour, and P3 reads the trigger from the order it captures. No other unknown
config name is guessed.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from importlib import import_module
from typing import Any

import pytest
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import FALL
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import tape

EXTERNAL = StrategyId("EXTERNAL")
PARTIAL_FOREIGN_LIMIT = pytest.mark.xfail(
    strict=True, reason="Lead ruling: A, a long covered in part by a foreign sell, is a named limit"
)


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override conftest's credential-environment reader for this entirely offline file."""


def quote(second: int, bid: str = "10.00", *, instrument_id=AAA, size: int = 10_000):
    timestamp = T0 + second * SECOND
    return QuoteTick(
        instrument_id,
        Price.from_str(f"{Decimal(bid):.4f}"),
        Price.from_str(f"{Decimal(bid) + Decimal('0.01'):.4f}"),
        Quantity.from_int(size),
        Quantity.from_int(size),
        timestamp,
        timestamp,
    )


class ExternalInventory(Strategy):
    """Create the already-held long and working owner sells using simulated fills."""

    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        self.held = 0
        self.covers: tuple[int, ...] = ()
        self.entered = False
        self.cover_ids: list = []

    def on_start(self):
        self.subscribe_quotes(AAA)

    def on_quote(self, quote):
        if self.entered or not self.held:
            return
        self.entered = True
        self.submit_order(
            self.order_factory.limit(
                AAA,
                OrderSide.BUY,
                Quantity.from_int(self.held),
                quote.ask_price,
                time_in_force=TimeInForce.GTC,
            )
        )

    def on_order_filled(self, event):
        if event.order_side != OrderSide.BUY:
            return
        for quantity in self.covers:
            order = self.order_factory.limit(
                AAA,
                OrderSide.SELL,
                Quantity.from_int(quantity),
                Price.from_str("11.0000"),
                time_in_force=TimeInForce.GTC,
            )
            self.cover_ids.append(order.client_order_id)
            self.submit_order(order)


class UnreadableQuantity:
    """A working order whose share coverage is unavailable, with its identity intact."""

    def __init__(self, order):
        self.order = order

    @property
    def leaves_qty(self):
        return None

    @property
    def quantity(self):
        return None

    def __getattr__(self, name):
        return getattr(self.order, name)


class UnknownCover:
    """Stand in only for the documented cache queries that return working orders."""

    def __init__(self, native, covered_strategy):
        self.native = native
        self.covered_strategy = covered_strategy

    def __getattr__(self, name):
        member = getattr(self.native, name)
        if name not in {"orders", "orders_open", "orders_inflight", "orders_emulated", "order"}:
            return member

        def query(*args, **kwargs):
            result = member(*args, **kwargs)

            def obscure(order):
                if order is not None and order.strategy_id == self.covered_strategy:
                    return UnreadableQuantity(order)
                return order

            return (
                [obscure(order) for order in result]
                if isinstance(result, list)
                else obscure(result)
            )

        return query


def owner_type():
    """Import inside each case, so missing the named seam fails each property separately."""
    module = import_module("canon_trading.owner_protector")
    assert issubclass(module.OwnerProtector, Strategy)

    class Recording(module.OwnerProtector):
        def __init__(self, config):
            self.sent: list[dict[str, Any]] = []
            self.claims: list = []
            self.changes: list = []
            self.opaque_orders = False
            self.supplied_for_holding = False
            super().__init__(config)

        def on_quote(self, quote):
            # The seed fills after node start. Supply a level only once its holding exists;
            # the new lifecycle contract correctly drops a level supplied while flat.
            if not self.supplied_for_holding and self.cache.positions_open(
                instrument_id=AAA, strategy_id=EXTERNAL
            ):
                self.supplied_for_holding = True
                assert self.config is not None
                self.adopt_holding(AAA, self.config.levels[AAA])
            super().on_quote(quote)

        def on_start(self):
            super().on_start()
            assert self.config is not None
            self.subscribe_quotes(AAA, params={"feed": self.config.feed})

        @property
        def cache(self):
            native = super().cache
            return native if not self.opaque_orders else UnknownCover(native, EXTERNAL)

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.sent.append(
                {
                    "id": order.client_order_id,
                    "side": order.side,
                    "quantity": order.quantity.as_decimal(),
                    "reduce_only": order.is_reduce_only,
                    "trigger": getattr(order, "trigger_price", None),
                    "params": dict(params or {}),
                }
            )
            super().submit_order(order, position_id, client_id, params)

        def set_external_order_instrument_ids(self, instrument_ids):
            self.claims.append(tuple(instrument_ids))
            super().set_external_order_instrument_ids(instrument_ids)

        def cancel_order(self, client_order_id, *args, **kwargs):
            self.changes.append(client_order_id)
            super().cancel_order(client_order_id, *args, **kwargs)

        def modify_order(self, client_order_id, *args, **kwargs):
            self.changes.append(client_order_id)
            super().modify_order(client_order_id, *args, **kwargs)

    def configure(**changes):
        # P3's "given level": the public OwnerProtectorConfig seam takes it as `levels`.
        settings: dict[Any, Any] = {"row": "OWN1", "levels": {AAA: Price.from_str("9.90")}}
        settings.update(changes)
        return module.OwnerProtectorConfig(**settings)

    return configure, Recording


class Replay:
    def __init__(self, eng, seed):
        self.engine = eng
        self.seed = seed
        self.second = 4
        self.closed = False

    def close(self):
        """Stop through the engine, which owns the strategies' clocks and on_stop path."""
        if not self.closed:
            self.engine.end()
            self.closed = True

    def drive(self, *bids: str, instrument_id=AAA, size: int = 10_000):
        data = []
        for bid in bids:
            self.second += 1
            data.append(quote(self.second, bid, instrument_id=instrument_id, size=size))
        self.engine.clear_data()
        self.engine.add_data(data)
        self.engine.run(streaming=True)

    def foreign(self):
        return [self.engine.cache.order(cid) for cid in self.seed.cover_ids]

    def external_quantity(self):
        return sum(
            (
                position.signed_qty
                for position in self.engine.cache.positions_open(
                    instrument_id=AAA, strategy_id=EXTERNAL
                )
            ),
            Decimal(0),
        )


@contextmanager
def replay(*, held: int = 10, covers: tuple[int, ...] = (), strategies: tuple[Any, ...] = ()):
    eng = engine(liquid=True)
    harness = None
    try:
        seed = ExternalInventory(StrategyConfig(strategy_id=EXTERNAL))
        seed.held, seed.covers = held, covers
        # Every strategy is registered before the engine's first run: only its own start
        # (BacktestEngine -> Trader.start_trader, crates/backtest/src/engine.rs:832) gives a
        # strategy a clock it can set timers on. A manual Strategy.start() hits a borrowed clock.
        eng.add_strategy(seed)
        for strategy in strategies:
            eng.add_strategy(strategy)
        eng.add_data([quote(second) for second in range(5)])
        eng.run(streaming=True)
        harness = Replay(eng, seed)
        assert harness.external_quantity() == held, "setup must create real EXTERNAL inventory"
        assert len(harness.foreign()) == len(covers)
        assert all(order.is_open for order in harness.foreign())
        yield harness
    finally:
        if harness is not None:
            harness.close()
        else:
            eng.end()
        eng.dispose()


def fingerprint(order):
    return (
        order.strategy_id,
        order.client_order_id,
        order.venue_order_id,
        order.quantity,
        order.leaves_qty,
        order.price,
        order.status,
        order.event_count,
    )


def protective_orders(harness, strategy):
    return harness.engine.cache.orders(strategy_id=strategy.strategy_id, side=OrderSide.SELL)


def elected(harness, strategy):
    return any(
        order.filled_qty.as_decimal() > 0
        or order.order_type in {OrderType.LIMIT, OrderType.MARKET}
        or any(type(event).__name__ == "OrderReleased" for event in order.events())
        for order in protective_orders(harness, strategy)
    )


def test_harness_really_has_external_inventory_and_a_partially_filled_foreign_sell():
    """Control: pre-existing upstream behaviour, not a new Unit 10 property."""
    probe = Strategy(StrategyConfig(strategy_id=StrategyId("Probe-001")))
    with replay(held=12, covers=(6,), strategies=(probe,)) as harness:
        harness.drive("11.00", size=2)
        harness.drive("10.00")
        assert harness.external_quantity() == 10
        (foreign,) = harness.foreign()
        assert foreign.strategy_id == EXTERNAL
        assert foreign.quantity.as_decimal() == 6
        assert foreign.leaves_qty.as_decimal() == 4
        assert probe.is_running(), "the engine starts a strategy added before its first run"


@PARTIAL_FOREIGN_LIMIT
def test_never_claims_external_inventory_or_changes_another_lineages_working_sell():
    """P1: protect the remainder while the owner's working order survives start/stop."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    assert not strategy.config.external_order_instrument_ids
    with replay(covers=(4,), strategies=(strategy,)) as harness:
        before = [fingerprint(order) for order in harness.foreign()]
        harness.drive(*(["10.00"] * 10), "9.00", "9.00", "9.00", "9.00")
        harness.close()
        assert strategy.sent, "must actually protect the known uncovered remainder"
        assert not any(strategy.claims)
        assert not strategy.config.external_order_instrument_ids
        assert not set(harness.seed.cover_ids).intersection(strategy.changes)
        assert [fingerprint(order) for order in harness.foreign()] == before
        assert all(
            position.strategy_id == EXTERNAL
            for position in harness.engine.cache.positions(instrument_id=AAA)
        )


@pytest.mark.parametrize(
    ("covers", "expected"),
    [
        ((), 10),
        pytest.param((4,), 6, marks=PARTIAL_FOREIGN_LIMIT),
        pytest.param((2, 3), 5, marks=PARTIAL_FOREIGN_LIMIT),
        ((10,), 0),
    ],
    ids=["uncovered", "partial-cover", "two-sells", "fully-covered"],
)
def test_protects_only_the_quantity_working_sells_leave_uncovered(covers, expected):
    """P2: an owner's ordinary working sells reserve shares without being modified."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    with replay(covers=covers, strategies=(strategy,)) as harness:
        harness.drive(*(["10.00"] * 10))
        if expected == 0:
            assert strategy.sent == []
        else:
            assert strategy.sent, "uncovered EXTERNAL shares need a protective order"
            assert strategy.sent[0]["quantity"] == expected
            assert all(row["quantity"] <= expected for row in strategy.sent)
            assert (
                sum(
                    order.leaves_qty.as_decimal()
                    for order in protective_orders(harness, strategy)
                    if not order.is_closed
                )
                == expected
            )


@PARTIAL_FOREIGN_LIMIT
def test_partial_fill_uses_foreign_leaves_quantity_not_original_order_quantity():
    """P2: 12 bought, 2 sold, 4 still working leaves 6 of the current 10 uncovered."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    with replay(held=12, covers=(6,), strategies=(strategy,)) as harness:
        harness.drive("11.00", size=2)
        harness.drive("10.00")
        assert harness.external_quantity() == 10
        before = fingerprint(harness.foreign()[0])
        harness.drive(*(["10.00"] * 10))
        assert strategy.sent and strategy.sent[0]["quantity"] == 6
        assert fingerprint(harness.foreign()[0]) == before


def test_unknown_foreign_cover_sends_nothing_for_the_instrument():
    """P2: unavailable share quantities must not be interpreted as zero coverage."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    strategy.opaque_orders = True
    with replay(covers=(4,), strategies=(strategy,)) as harness:
        before = fingerprint(harness.foreign()[0])
        harness.drive(*(["10.00"] * 10), "9.00", "9.00", "9.00")
        harness.close()
        assert strategy.sent == []
        assert fingerprint(harness.foreign()[0]) == before
        assert harness.external_quantity() == 10


@pytest.mark.parametrize("feed", ["sip", "boats"])
def test_protective_sells_are_reduce_only_and_extended_hours(feed):
    """P3: inspect the actual submission, without guessing flag config names."""
    config, recording = owner_type()
    strategy = recording(config(feed=feed))
    with replay(strategies=(strategy,)) as harness:
        harness.drive(*(["10.00"] * 10), *(["9.00"] * 10))
        assert strategy.sent
        assert all(row["side"] == OrderSide.SELL for row in strategy.sent)
        assert all(row["reduce_only"] is True for row in strategy.sent)
        assert all(row["params"].get("extended_hours") is True for row in strategy.sent)


@pytest.mark.parametrize("below", [Decimal(0), Decimal("0.01")], ids=["at-level", "below-level"])
@pytest.mark.xfail(
    strict=True, reason="Lead ruling: B, election after arming on one quote, is unit 13"
)
def test_one_quote_through_the_armed_level_cannot_elect_the_protective_sell(below):
    """P3: timers and another instrument cannot turn one breach into confirmation."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    with replay(strategies=(strategy,)) as harness:
        harness.drive(*(["10.00"] * 10))
        armed = [row for row in strategy.sent if row["trigger"] is not None]
        assert armed, "observe a protective trigger, without inventing a level config key"
        level = armed[-1]["trigger"].as_decimal()
        assert 0 < level < Decimal("10.00")
        assert not elected(harness, strategy)
        breach = str(level - below)
        harness.drive(breach)
        harness.drive(*(["10.00"] * 5), instrument_id=BBB)
        assert not elected(harness, strategy), "one AAA quote must not release or fill a sell"
        assert harness.external_quantity() == 10
        harness.drive(*([breach] * 10))
        assert elected(harness, strategy), "continued independent breaches must eventually elect"
        assert harness.external_quantity() < 10
        assert not harness.engine.cache.positions(strategy_id=strategy.strategy_id)


@pytest.mark.parametrize("held", [0, 10], ids=["flat", "owner-long"])
def test_never_buys_on_rising_ranked_prices_or_after_a_protective_exit(held):
    """P4: the Mover's qualifying tape and ranking cannot turn the protector into an entry."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip"))
    ranking = ranker(rank((AAA, "0.25")))
    with replay(held=held, strategies=(strategy, ranking)) as harness:
        harness.engine.clear_data()
        harness.engine.add_data(tape(AAA, RISING + FALL + RISING + FALL))
        harness.engine.run(streaming=True)
        harness.close()
        assert all(row["side"] != OrderSide.BUY for row in strategy.sent)
        assert not harness.engine.cache.orders(strategy_id=strategy.strategy_id, side=OrderSide.BUY)
        if held:
            assert strategy.sent, "the no-buy assertion must accompany actual protection"


def test_iex_configuration_refuses_to_run():
    """P5: constructor rejection or an explicit stopped/faulted start are both refusals."""
    config, recording = owner_type()
    try:
        strategy = recording(config(feed="iex"))
    except ValueError as refused:
        assert "iex" in str(refused).lower() or "feed" in str(refused).lower()
        return
    try:
        with replay(strategies=(strategy,)):
            pass
    except ValueError as refused:
        assert "iex" in str(refused).lower() or "feed" in str(refused).lower()
    else:
        assert not strategy.is_running(), "IEX must be refused at start, before the tape"
    assert strategy.sent == []
