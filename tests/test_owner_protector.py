"""Unit 10: EXTERNAL custody, uncovered shares and a two-observation adoption guard.

All positions, orders and quotes are synthetic. The stand-in records commands using the real
upstream order factory; the final cases run the pinned BacktestEngine and its emulator. No file,
paper state, catalog, ledger, credential or network service is used by this file.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import Self
from typing import cast

import pytest
from alpaca.data.models import BarSet
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import Clock
from nautilus_trader.common import Environment
from nautilus_trader.common import OrderFactory
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import ExecutionClientConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live import InstrumentProviderConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import RoutingConfig
from nautilus_trader.live.clients import DataClientFactory
from nautilus_trader.live.clients import ExecutionClient
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.live.clients import MarketDataClient
from nautilus_trader.model import AccountBalance
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import ContingencyType
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderCancelRejected
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionId
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType
from nautilus_trader.model import Venue
from nautilus_trader.model import VenueOrderId
from nautilus_trader.trading import Strategy

from canon_trading import protect
from canon_trading.owner_protector import OwnerProtector
from canon_trading.owner_protector import OwnerProtectorConfig
from canon_trading.owner_protector import owner_execution_config
from canon_trading.price_step import tick
from canon_trading.price_step import up_on_step

AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")
EXTERNAL = StrategyId("EXTERNAL")
PROTECTOR = StrategyId("OwnerProtector-001")
USD = Currency.from_str("USD")
T0 = 1_762_790_400_000_000_000
SECOND = 1_000_000_000
CANON_SELL = "canon-protect-20261113T190000Z-1-0123abcd"


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline simulations."""


def equity(iid: InstrumentId = AAA, *, precision: int = 2) -> Equity:
    return Equity(
        instrument_id=iid,
        raw_symbol=Symbol(iid.symbol.value),
        currency=USD,
        price_precision=precision,
        price_increment=Price.from_str("0.0001" if precision == 4 else "0.01"),
        ts_event=0,
        ts_init=0,
    )


def quote(bid: str = "10.00", at: int = T0, iid: InstrumentId = AAA) -> QuoteTick:
    return QuoteTick(
        iid,
        Price.from_str(bid),
        Price.from_str(str(Decimal(bid) + Decimal("0.02"))),
        Quantity.from_int(100),
        Quantity.from_int(100),
        at,
        at,
    )


def position(qty: str = "10", strategy_id: StrategyId = EXTERNAL) -> SimpleNamespace:
    return SimpleNamespace(
        id=PositionId("AAA.ALPACA-EXTERNAL"),
        instrument_id=AAA,
        strategy_id=strategy_id,
        signed_qty=Decimal(qty),
        quantity=Quantity.from_str(str(abs(Decimal(qty)))),
        account_id=AccountId("ALPACA-001"),
        is_long=Decimal(qty) > 0,
        is_open=Decimal(qty) != 0,
        ts_opened=T0,
        ts_init=T0,
    )


def working_sell(
    cid: str = CANON_SELL, *, qty: str = "5", filled: str = "2", **changes: Any
) -> SimpleNamespace:
    fields = {
        "instrument_id": AAA,
        "strategy_id": EXTERNAL,
        "account_id": AccountId("ALPACA-001"),
        "client_order_id": ClientOrderId(cid),
        "side": OrderSide.SELL,
        "order_type": OrderType.LIMIT,
        "status": OrderStatus.PARTIALLY_FILLED,
        "quantity": Quantity.from_str(qty),
        "filled_qty": Quantity.from_str(filled),
        "leaves_qty": Quantity.from_str(str(Decimal(qty) - Decimal(filled))),
        "is_quote_quantity": False,
        "contingency_type": ContingencyType.NO_CONTINGENCY,
        "linked_order_ids": None,
        "is_closed": False,
        "trigger_price": None,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


class Cache:
    def __init__(self) -> None:
        self.held: list[Any] = [position()]
        self.working: list[Any] = []
        self.latest: QuoteTick | None = quote()
        self.history: list[Bar] = []

    def bars(self, bar_type):
        return [bar for bar in self.history if bar.bar_type == bar_type]

    def positions_open(self, instrument_id=None, strategy_id=None, **_kwargs):
        return [
            p
            for p in self.held
            if (instrument_id is None or p.instrument_id == instrument_id)
            and (strategy_id is None or p.strategy_id == strategy_id)
            and p.is_open
        ]

    def orders(self, instrument_id=None, **_kwargs):
        return [
            o for o in self.working if instrument_id is None or o.instrument_id == instrument_id
        ]

    def order(self, cid):
        return next((o for o in self.working if o.client_order_id == cid), None)

    def quote(self, iid):
        return self.latest if self.latest is not None and self.latest.instrument_id == iid else None

    def instrument(self, iid):
        return equity(iid)


class Harness(OwnerProtector):
    """Real factory, stand-in cache and transport, like the Mover's command harnesses."""

    def __new__(cls, **settings: Any) -> Self:
        # The stub omits Strategy.__new__(config); python/strategy.rs:1683-1690 defines it.
        settings.setdefault("row", "OWN1")
        return super().__new__(cls, OwnerProtectorConfig(strategy_id=PROTECTOR, **settings))  # ty: ignore[too-many-positional-arguments]

    def __init__(self, **settings: Any) -> None:
        settings.setdefault("row", "OWN1")
        super().__init__(OwnerProtectorConfig(strategy_id=PROTECTOR, **settings))
        self.cached = Cache()
        self.test_clock = Clock.new_test()
        self.test_clock.set_time(T0)
        self.factory = OrderFactory(TraderId("TESTER-001"), self.strategy_id, self.test_clock)
        self.sent: list[Any] = []
        self.params: list[dict | None] = []
        self.canceled: list[ClientOrderId] = []
        self.said: list[str] = []
        self.subscriptions: list[tuple[InstrumentId, dict | None]] = []
        self.bar_subscriptions: list[tuple[BarType, dict | None]] = []

    @property
    def cache(self) -> Any:
        return self.cached

    @property
    def clock(self) -> Any:
        return self.test_clock

    @property
    def order_factory(self) -> Any:
        return self.factory

    def _say(self, text: str) -> None:
        self.said.append(text)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        assert position_id is None  # Explicit EXTERNAL IDs are denied under NETTING at the pin.
        self.sent.append(order)
        self.params.append(params)
        self.cached.working.append(order)

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.canceled.append(client_order_id)

    def cancel_all_orders(self, *args, **kwargs) -> None:
        pytest.fail("OwnerProtector must never issue cancel_all_orders")

    def modify_order(self, *args, **kwargs) -> None:
        pytest.fail("OwnerProtector must never resize or replace an order")

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.subscriptions.append((instrument_id, params))

    def subscribe_bars(self, bar_type, client_id=None, params=None) -> None:
        self.bar_subscriptions.append((bar_type, params))

    def observe(self, bid: str, at: int) -> None:
        tick = quote(bid, at)
        self.cached.latest = tick
        self.test_clock.set_time(max(at, self.test_clock.timestamp_ns()))
        self.on_quote(tick)


def new_quote(strategy: Harness, bid: str | None = None) -> None:
    """The first quote received after an adoption, half a second on, at the cached bid unless
    given. Nothing is sent on the quote cached at adoption (the lead's ruling, 2026-11-13,
    16:13:47): placement waits one quote."""
    cached = strategy.cached.latest
    if bid is None:
        bid = "10.00" if cached is None else str(cached.bid_price)
    strategy.observe(bid, strategy.clock.timestamp_ns() + SECOND // 2)


def test_external_long_gets_one_reduce_only_emulated_sell_without_claim_or_entry() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert len(strategy.sent) == 1
    order = strategy.sent[0]
    assert order.side == OrderSide.SELL
    assert order.is_reduce_only
    assert order.quantity == Quantity.from_int(10)
    assert order.order_type == OrderType.TRAILING_STOP_LIMIT
    assert order.trigger_price == Price.from_str("9.90")
    assert order.emulation_trigger == TriggerType.BID_ASK
    assert strategy.settings.oms_type == OmsType.NETTING
    assert not strategy.settings.external_order_instrument_ids
    assert strategy.cached.held[0].strategy_id == EXTERNAL


@pytest.mark.parametrize("qty,owner", [("-10", EXTERNAL), ("0", EXTERNAL), ("10", PROTECTOR)])
def test_only_external_longs_are_protected(qty, owner) -> None:
    strategy = Harness()
    strategy.cached.held = [position(qty, owner)]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert not strategy.sent
    assert any("AAA.ALPACA" in note and "EXTERNAL" in note for note in strategy.said)


def test_ambiguous_external_positions_and_own_position_freeze() -> None:
    for holdings in ([position(), position()], [position(), position("2", PROTECTOR)]):
        strategy = Harness()
        strategy.cached.held = holdings
        strategy.adopt_holding(AAA, Price.from_str("9.90"))
        assert not strategy.sent
        assert AAA in strategy.frozen


def test_uncovered_quantity_subtracts_only_remaining_canon_sells() -> None:
    strategy = Harness()
    strategy.cached.working = [working_sell(), working_sell("owner-buy", side=OrderSide.BUY)]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert strategy.sent[0].quantity == Quantity.from_int(7)
    assert not strategy.canceled


@pytest.mark.parametrize("cid", [CANON_SELL, "owner-limit"])
def test_fully_covered_long_gets_no_second_sell(cid) -> None:
    strategy = Harness()
    strategy.cached.working = [working_sell(cid, qty="12", filled="2")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert not strategy.sent
    assert not strategy.canceled
    assert any("covered" in note for note in strategy.said)


def test_partial_foreign_cover_freezes_as_in_protect_py() -> None:
    strategy = Harness()
    strategy.cached.working = [working_sell("owner-limit")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert not strategy.sent
    assert not strategy.canceled
    assert "foreign" in strategy.frozen[AAA]


def test_partial_foreign_cover_is_named_in_the_log_at_each_onset() -> None:
    """The lead's ruling A (2026-11-13, 15:00): a long covered in part by a foreign sell is a named
    limit, left untouched and named in the log each time it arises, not once per run."""

    def named() -> int:
        return sum(
            "AAA.ALPACA" in note and "covered in part by a foreign sell" in note
            for note in strategy.said
        )

    strategy = Harness()
    strategy.cached.latest = None  # no quote: nothing can arm between the two onsets
    strategy.cached.working = [working_sell("owner-limit")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert named() == 1 and "foreign" in strategy.frozen[AAA]
    strategy._on_step(cast(Any, SimpleNamespace()))  # the same state again is not a new onset
    assert named() == 1
    strategy.cached.working = []  # the owner's sell ends; with no quote the long is unprotected
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert "foreign" not in strategy.frozen.get(AAA, "")
    strategy.cached.working = [working_sell("owner-later")]  # a second partial cover arises
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert named() == 2 and "foreign" in strategy.frozen[AAA]
    assert not strategy.sent and not strategy.canceled


def test_cancel_and_stop_touch_only_ids_this_instance_sent() -> None:
    strategy = Harness()
    inherited = working_sell(strategy_id=PROTECTOR)
    strategy.cached.working = [inherited]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    own_id = strategy.sent[0].client_order_id
    foreign = working_sell("owner-later", instrument_id=BBB)
    strategy.cached.working.append(foreign)
    strategy.stop_instrument(AAA)
    strategy.on_stop()
    assert strategy.canceled == [own_id]
    assert inherited.client_order_id not in strategy.canceled
    assert foreign.client_order_id not in strategy.canceled


def test_adopt_holding_never_elects_on_one_observation_or_repeated_timer_steps() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    assert not strategy.sent
    for step in range(1, 4):
        strategy._on_step(cast(Any, SimpleNamespace(ts_event=T0 + step * SECOND)))
    strategy.observe("10.00", T0)  # duplicate venue observation, not confirmation
    assert not strategy.sent
    strategy.observe("9.99", T0 + 4 * SECOND)
    assert not strategy.sent, "the first quote received after the adoption is the guarded one"
    strategy.observe("9.98", T0 + 5 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == Price.from_str("10.00")
    assert strategy.sent[0].is_reduce_only


def test_first_quote_after_adoption_without_a_quote_does_not_elect() -> None:
    strategy = Harness()
    strategy.cached.latest = None
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy.observe("9.99", T0 + SECOND)  # V2: with no cached quote, this is the reference
    assert not strategy.sent
    strategy.observe("9.98", T0 + 2 * SECOND)  # the guarded first observation
    assert not strategy.sent
    strategy.observe("9.97", T0 + 3 * SECOND)
    assert len(strategy.sent) == 1


def test_recovery_above_given_level_arms_without_lowering_it() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy.observe("10.10", T0 + SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == Price.from_str("10.00")


@pytest.mark.parametrize("feed", ["iex", "IEX", None])
def test_iex_and_implicit_iex_are_refused_with_reason(feed) -> None:
    with pytest.raises(ValueError, match="IEX"):
        OwnerProtectorConfig(row="OWN1", feed=feed)


@pytest.mark.parametrize("feed", ["sip", "boats"])
def test_every_protective_limit_is_extended_hours_on_each_allowed_feed(feed) -> None:
    strategy = Harness(feed=feed)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert strategy.params == [{"extended_hours": True}]
    assert strategy.sent[0].time_in_force == TimeInForce.DAY
    assert strategy.subscriptions == [(AAA, {"feed": feed})]


@pytest.mark.parametrize(
    "settings,reason",
    [
        ({"external_order_instrument_ids": [AAA]}, "claim"),
        ({"oms_type": OmsType.HEDGING}, "NETTING"),
        ({"manage_stop": True}, "manage_stop"),
        ({"extended_hours": False}, "extended_hours"),
    ],
)
def test_config_refuses_unsafe_base_settings(settings, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        OwnerProtectorConfig(row="OWN1", **settings)


def test_runtime_external_claims_are_refused() -> None:
    strategy = Harness()
    with pytest.raises(ValueError, match="claim"):
        strategy.set_external_order_instrument_ids([AAA])
    assert not strategy.sent


def test_adapter_config_enables_external_reports_and_reduce_only_sells() -> None:
    config = owner_execution_config()
    assert config.report_external_orders
    assert config.report_external_trailing_stops
    assert config.reduce_only_sells


def test_start_adopts_configured_levels_and_names_the_feed() -> None:
    strategy = Harness(levels={AAA: Price.from_str("9.90")})
    strategy.on_start()
    new_quote(strategy)
    assert len(strategy.sent) == 1
    assert strategy.subscriptions == [(AAA, {"feed": "sip"})]
    strategy.on_stop()
    assert strategy.canceled == [strategy.sent[0].client_order_id]


def test_fractional_uncovered_quantity_is_never_rounded_up() -> None:
    strategy = Harness()
    strategy.cached.held = [position("10.9")]
    strategy.cached.working = [working_sell()]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert strategy.sent[0].quantity == Quantity.from_int(7)
    assert any("fraction" in note for note in strategy.said)


def test_changed_holding_cancels_only_our_sell_and_waits_for_ack_before_rearming() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.held = [position("6")]
    strategy.observe("10.00", T0 + SECOND)
    strategy.observe("10.00", T0 + 2 * SECOND)
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
            trigger_price=Price.from_str("9.95"),
        )
    ]
    strategy.observe("10.00", T0 + 3 * SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[1].quantity == Quantity.from_int(6)
    assert strategy.sent[1].trigger_price == Price.from_str("9.95")
    assert strategy.params == [{"extended_hours": True}, {"extended_hours": True}]


def test_later_foreign_cover_cancels_only_our_sell_and_freezes() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    strategy.cached.working.append(working_sell("owner-later"))
    strategy.observe("10.00", T0 + SECOND)
    assert strategy.canceled == [strategy.sent[0].client_order_id]
    assert len(strategy.sent) == 1
    assert "foreign" in strategy.frozen[AAA]


def test_no_new_sell_after_instrument_or_strategy_stop() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy.stop_instrument(AAA)
    strategy.observe("9.90", T0 + SECOND)
    assert not strategy.sent
    strategy.on_stop()
    strategy.adopt_holding(AAA, Price.from_str("9.80"))
    strategy.observe("9.90", T0 + 2 * SECOND)
    assert not strategy.sent


def test_client_id_collision_never_makes_an_existing_order_ours_to_cancel(monkeypatch) -> None:
    strategy = Harness()
    inherited = strategy.factory.limit(
        AAA,
        OrderSide.SELL,
        Quantity.from_int(3),
        Price.from_str("11.00"),
        client_order_id=ClientOrderId(CANON_SELL),
    )
    strategy.cached.working = [inherited]
    # A protect-lineage ID the protector would name again (pnode: canon-protect- client IDs).
    monkeypatch.setattr(strategy, "_protect_id", lambda: ClientOrderId(CANON_SELL))
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    strategy.on_stop()
    assert not strategy.sent
    assert not strategy.canceled
    assert "client order ID" in strategy.frozen[AAA]


def test_rearm_keeps_the_released_orders_trigger_from_its_events() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.held = [position("6")]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=None,
            events=lambda: [SimpleNamespace(trigger_price=Price.from_str("10.10"))],
        )
    ]
    strategy.observe("10.20", T0 + SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[1].trigger_price == Price.from_str("10.10")


def test_cancel_rejected_releases_wait_and_allows_resize() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.held = [position("6")]
    strategy.observe("10.00", T0 + SECOND)
    assert strategy.canceled == [first.client_order_id]
    released = working_sell(
        str(first.client_order_id),
        qty="10",
        filled="0",
        strategy_id=PROTECTOR,
        status=OrderStatus.ACCEPTED,
        events=lambda: [SimpleNamespace(trigger_price=Price.from_str("9.95"))],
    )
    strategy.cached.working = [released]
    strategy.on_order_cancel_rejected(
        OrderCancelRejected(
            TraderId("TESTER-001"),
            PROTECTOR,
            AAA,
            first.client_order_id,
            "synthetic venue refused cancel",
            UUID4(),
            T0 + SECOND,
            T0 + SECOND,
            False,
        )
    )
    assert first.client_order_id not in strategy.cancel_requested
    strategy.observe("10.00", T0 + 2 * SECOND)
    assert strategy.canceled == [first.client_order_id, first.client_order_id]
    assert len(strategy.sent) == 1  # Still no replacement before a terminal acknowledgement.
    released.status, released.is_closed = OrderStatus.CANCELED, True
    strategy.observe("10.00", T0 + 3 * SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[1].quantity == Quantity.from_int(6)
    assert strategy.sent[1].trigger_price == Price.from_str("9.95")
    assert any("cancel rejected" in note for note in strategy.said)


def test_filled_out_holding_self_arms_with_a_new_level_and_fresh_observations() -> None:
    # Also cover a re-buy before the next quote/timer sees the flat cache.
    for observe_flat in (True, False):
        strategy = Harness()
        strategy.adopt_holding(AAA, Price.from_str("9.90"))
        new_quote(strategy)
        first = strategy.sent[0]
        strategy.cached.working = [
            working_sell(
                str(first.client_order_id),
                qty="10",
                filled="10",
                strategy_id=PROTECTOR,
                status=OrderStatus.FILLED,
                is_closed=True,
                events=lambda: [SimpleNamespace(trigger_price=Price.from_str("12.00"))],
            )
        ]
        strategy.cached.held = []
        if observe_flat:
            strategy.observe("12.00", T0 + SECOND)
        bought_again = position()
        bought_again.ts_opened = T0 + 2 * SECOND
        strategy.cached.held = [bought_again]
        strategy.observe("11.00", T0 + 3 * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
        assert len(strategy.sent) == 1, "a re-buy must not inherit the old 12.00 trigger"
        assert AAA not in strategy.levels
        assert first.client_order_id not in strategy.cancel_requested
        assert AAA in strategy.slots, "a re-buy self-arms without another supplied level"
        assert strategy.slots[AAA].level != Price.from_str("12.00")
        fresh_level = strategy.slots[AAA].level
        strategy._on_step(cast(Any, SimpleNamespace()))
        strategy.observe("11.00", T0 + 3 * SECOND)
        assert len(strategy.sent) == 1
        strategy.observe("10.99", T0 + 4 * SECOND)
        assert len(strategy.sent) == 1, "the first quote received after the arming is guarded"
        strategy.observe("10.98", T0 + 5 * SECOND)
        assert len(strategy.sent) == 2
        assert strategy.sent[1].trigger_price == fresh_level


def test_other_strategy_inventory_and_sells_never_cover_the_external_long() -> None:
    mover = StrategyId("Mover-001")
    for mover_holds in (True, False):
        strategy = Harness()
        strategy.cached.held = [position("100")]
        if mover_holds:
            strategy.cached.held.append(position("50", mover))
        strategy.cached.working = [
            working_sell("O-20261113-190000-001-001-1", qty="50", filled="0", strategy_id=mover)
        ]
        strategy.adopt_holding(AAA, Price.from_str("9.90"))
        assert not strategy.sent, "the Mover's 50-share sell is not owner coverage"
        assert "Mover-001" in strategy.frozen[AAA]
        assert any("AAA.ALPACA" in note and "Mover-001" in note for note in strategy.said)
        assert not strategy.canceled


def test_expired_released_day_limit_rearms_at_the_retained_trigger() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.EXPIRED,
            is_closed=True,
            events=lambda: [SimpleNamespace(trigger_price=Price.from_str("10.10"))],
        )
    ]
    strategy.observe("10.20", T0 + SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[1].client_order_id != first.client_order_id
    assert strategy.sent[1].trigger_price == Price.from_str("10.10")
    assert strategy.sent[1].quantity == Quantity.from_int(10)
    assert strategy.sent[1].time_in_force == TimeInForce.DAY
    assert strategy.params[-1] == {"extended_hours": True}
    assert AAA not in strategy.frozen


def test_benign_foreign_cover_keeps_armed_protection_until_oversell_is_possible() -> None:
    for status in (OrderStatus.PENDING_UPDATE, OrderStatus.ACCEPTED):
        strategy = Harness()
        strategy.cached.held = [position("100")]
        strategy.cached.working = [working_sell(qty="10", filled="0")]
        strategy.adopt_holding(AAA, Price.from_str("9.90"))
        new_quote(strategy)
        own = strategy.sent[0]
        assert own.quantity == Quantity.from_int(90)
        foreign = working_sell("owner-later", qty="5", filled="0", status=status)
        strategy.cached.working = [own, foreign]  # The earlier 10-share cover has ended.
        strategy.observe("10.00", T0 + SECOND)
        assert AAA in strategy.frozen
        assert not strategy.canceled, "95 working sells cannot oversell the 100-share long"
        assert len(strategy.sent) == 1
        foreign.quantity = foreign.leaves_qty = Quantity.from_int(11)
        strategy.observe("10.00", T0 + 2 * SECOND)
        assert strategy.canceled == [own.client_order_id]
        assert foreign.client_order_id not in strategy.canceled
        assert len(strategy.sent) == 1


def test_adoption_before_reconciliation_drops_old_level_and_self_arms_the_later_long() -> None:
    strategy = Harness()
    strategy.cached.held = []
    level = Price.from_str("10.00")
    strategy.adopt_holding(AAA, level)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert not strategy.sent
    strategy.cached.held = [position()]
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert AAA not in strategy.levels
    distance, _ = task_distance(strategy, "OWN1")
    fresh_level = Price.from_str(str(up_on_step(Decimal("10.00") - distance)))
    assert strategy.slots[AAA].level == fresh_level
    assert fresh_level != level, "a level supplied while flat must not bind later"
    assert not strategy.sent  # Reconciliation's first cached quote cannot elect.
    strategy.observe("9.99", T0 + SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe("9.98", T0 + 2 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == fresh_level
    assert AAA not in strategy.frozen


def test_owner_execution_config_accepts_only_common_fields() -> None:
    for forbidden in (
        {"submit_trailing_stops": True},
        {"sell_to_close_order_types": {OrderType.MARKET, OrderType.LIMIT}},
        {"sell_to_close_on_long_exits": True},
        {"reduce_only_sells": False},
        {"report_external_orders": False},
        {"report_external_trailing_stops": False},
        {"paper": False},
    ):
        with pytest.raises(TypeError, match="common"):
            owner_execution_config(**forbidden)
    defaults = owner_execution_config()
    config = owner_execution_config(
        instrument_provider=InstrumentProviderConfig(load_ids=[str(AAA)]),
        routing=RoutingConfig(default=True, venues=["ALPACA"]),
    )
    assert config.instrument_provider.load_ids == [str(AAA)]
    assert config.routing.default
    assert config.routing.venues == ["ALPACA"]
    assert config.reduce_only_sells
    assert config.report_external_orders and config.report_external_trailing_stops
    assert config.sell_to_close_order_types == defaults.sell_to_close_order_types
    assert not config.submit_trailing_stops
    assert not config.sell_to_close_on_long_exits


def minute_bars(width: str = "0.04", count: int = 14) -> list[Bar]:
    return [
        Bar(
            BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"),
            Price.from_str("10.00"),
            Price.from_str(str(Decimal("10.00") + Decimal(width) / 2)),
            Price.from_str(str(Decimal("10.00") - Decimal(width) / 2)),
            Price.from_str("10.00"),
            Quantity.from_int(100),
            T0 - minute * 60 * SECOND,
            T0 - (minute - 1) * 60 * SECOND,
        )
        for minute in range(1, count + 1)  # Cache returns newest first.
    ]


def task_distance(strategy: Harness, row: str):
    """Feed paper:protect's actual planner the same quote and bars as the node cache."""
    tick = strategy.cached.latest
    assert tick is not None
    now = datetime.fromtimestamp(strategy.clock.timestamp_ns() / SECOND, UTC)
    bars = BarSet(
        {
            "AAA": [
                {
                    "t": datetime.fromtimestamp(bar.ts_event / SECOND, UTC),
                    "o": bar.open.as_double(),
                    "h": bar.high.as_double(),
                    "l": bar.low.as_double(),
                    "c": bar.close.as_double(),
                    "v": 100,
                    "n": 10,
                    "vw": 10.0,
                }
                for bar in strategy.cached.history
                if bar.ts_event >= strategy.clock.timestamp_ns() - 3600 * SECOND
            ]
        }
    )
    data = SimpleNamespace(
        get_stock_latest_quote=lambda _request: {
            "AAA": SimpleNamespace(
                bid_price=tick.bid_price.as_decimal(),
                ask_price=tick.ask_price.as_decimal(),
                timestamp=datetime.fromtimestamp(tick.ts_event / SECOND, UTC),
            )
        },
        get_stock_bars=lambda _request: bars,
    )
    return protect.plan_distance(data, "AAA", now, row)


def test_row_has_no_default_and_obsolete_fractions_are_refused() -> None:
    with pytest.raises(TypeError, match="row"):
        OwnerProtectorConfig()  # ty: ignore[missing-argument] - exercise the public refusal
    for field in ("trail", "exit_through"):
        with pytest.raises(TypeError, match=field):
            changes: dict[str, Any] = {field: Decimal("0.01")}
            OwnerProtectorConfig(row="OWN1", **changes)


@pytest.mark.parametrize("row", ["", "UNKNOWN", None])
def test_unregistered_row_cannot_start(row) -> None:
    with pytest.raises(ValueError, match="row"):
        OwnerProtectorConfig(row=row)


@pytest.mark.parametrize(
    "row,width,count,bound",
    [
        ("OWN1", "0.06", 14, "from atr"),
        ("OWN3", "0.06", 14, "from atr"),
        ("OWN1", "0.01", 14, "from spread floor"),
        ("OWN1", "0.06", 0, "ATR cold"),
        ("F2", "0.06", 14, "the fixed row"),
    ],
)
def test_distance_matches_protect_task_and_logs_its_source(row, width, count, bound) -> None:
    strategy = Harness()
    strategy.settings.row = row
    strategy.cached.history = minute_bars(width, count)
    distance, planned = task_distance(strategy, row)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert strategy.sent[0].trailing_offset == distance
    assert any(planned.source() in note and bound in note for note in strategy.said)
    assert strategy.bar_subscriptions == [
        (BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"), {"feed": "sip"})
    ]


def test_row_is_computed_once_and_retained_across_resize_and_expiry(monkeypatch) -> None:
    strategy = Harness()
    strategy.settings.row = "OWN1"
    strategy.cached.history = minute_bars("0.06")
    calls = []
    original = protect.row_distance

    def planned(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(protect, "row_distance", planned)
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert len(calls) == 1
    first = strategy.sent[0]
    strategy.cached.history = minute_bars("0.60")  # Would now refuse above the cap.
    strategy.cached.held = [position("6")]
    strategy.observe("11.00", T0 + SECOND)
    assert strategy.canceled == [first.client_order_id]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=Price.from_str("10.94"),
        )
    ]
    strategy.observe("11.01", T0 + 2 * SECOND)
    second = strategy.sent[-1]
    assert second.quantity == Quantity.from_int(6)
    strategy.cached.working = [
        working_sell(
            str(second.client_order_id),
            qty="6",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.EXPIRED,
            is_closed=True,
            trigger_price=Price.from_str("10.94"),
        )
    ]
    strategy.observe("11.02", T0 + 3 * SECOND)
    assert len(strategy.sent) == 3
    assert all(order.trailing_offset == first.trailing_offset for order in strategy.sent)
    assert all(order.limit_offset == first.limit_offset for order in strategy.sent)
    assert len(calls) == 1


def test_row_uses_only_completed_recent_minutes_and_names_cold_path() -> None:
    strategy = Harness()
    strategy.settings.row = "OWN1"
    bars = minute_bars("0.06", 13)
    # Neither this unfinished minute nor an old bar may warm the 14-period ATR.
    for at in (T0, T0 - 61 * 60 * SECOND):
        bars.append(
            Bar(
                bars[0].bar_type,
                Price.from_str("10.00"),
                Price.from_str("10.03"),
                Price.from_str("9.97"),
                Price.from_str("10.00"),
                Quantity.from_int(100),
                at,
                at,
            )
        )
    strategy.cached.history = bars
    distance, _ = task_distance(strategy, "OWN1")
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert strategy.sent[0].trailing_offset == distance
    assert any("ATR cold" in note for note in strategy.said)


@pytest.mark.parametrize("bad_book", ["stale", "over_ceiling"])
def test_row_refusal_names_the_symbol_without_a_fixed_distance_fallback(bad_book) -> None:
    strategy = Harness()
    strategy.settings.row = "OWN1"
    if bad_book == "stale":
        strategy.test_clock.set_time(T0 + 61 * SECOND)
    else:
        # One ATR of 1.50 at a 10.00 bid is 0.15, over the found holding's 0.10 ceiling (ADR 0142).
        strategy.cached.history = minute_bars("1.50")
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    assert not strategy.sent
    assert any("AAA.ALPACA" in note and "distance refused" in note for note in strategy.said)
    if bad_book == "over_ceiling":
        assert any("distance_over_ceiling" in note for note in strategy.said)


def test_own_cap_distance_over_the_entry_cap_stands_up_to_the_ceiling() -> None:
    """Under OWN1's cap arm own, a found holding keeps its own distance over the Mover's 0.02 entry
    cap, here one ATR of 1.00 at a 10.00 bid, 0.10, the ceiling itself (protect.row_distance)."""
    strategy = Harness()
    strategy.cached.history = minute_bars("1.00")
    distance, _ = task_distance(strategy, "OWN1")
    assert distance == Decimal("1.00")
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trailing_offset == distance


def test_an_unarmed_level_ends_when_its_holding_disappears() -> None:
    strategy = Harness()
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy.cached.held = []
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert AAA not in strategy.levels
    assert AAA not in strategy.slots
    assert any("holding ended" in note for note in strategy.said)


def test_level_supplied_while_flat_is_refused_by_name_and_dropped() -> None:
    strategy = Harness()
    strategy.cached.held = []
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    assert AAA not in strategy.levels
    assert AAA not in strategy.slots
    assert any(
        "AAA.ALPACA" in note and "level refused" in note and "dropped" in note
        for note in strategy.said
    )


def test_startup_level_binds_to_reconciled_holding_with_node_start_ts_init() -> None:
    strategy = Harness(levels={AAA: Price.from_str("10.00")})
    strategy.cached.held[0].ts_init = T0
    strategy.cached.held[0].ts_opened = T0 - 7 * 24 * 3600 * SECOND
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("10.00")
    assert not strategy.sent
    strategy.observe("9.99", T0 + SECOND)
    assert not strategy.sent, "the first quote received after the adoption is the guarded one"
    strategy.observe("9.98", T0 + 2 * SECOND)
    assert len(strategy.sent) == 1


def test_residual_after_our_fill_gets_fresh_row_distance_level_and_guard() -> None:
    strategy = Harness()
    strategy.settings.row = "OWN1"
    strategy.cached.history = minute_bars("0.06")
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="10",
            strategy_id=PROTECTOR,
            status=OrderStatus.FILLED,
            is_closed=True,
            trigger_price=Price.from_str("12.00"),
        )
    ]
    strategy.cached.held = [position("5")]  # Same native position cycle: owner added before fill.
    strategy.cached.history = minute_bars("0.10")
    strategy.observe("11.00", T0 + SECOND)
    distance, _ = task_distance(strategy, "OWN1")
    fresh = strategy.slots[AAA]
    assert fresh.level == equity().make_price(Decimal("11.00") - distance)
    assert len(strategy.sent) == 1
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.observe(str(fresh.level), T0 + SECOND)  # Same observation cannot elect.
    assert len(strategy.sent) == 1
    assert any("our sell filled" in note and "new holding" in note for note in strategy.said)
    strategy.observe(str(fresh.level), T0 + 2 * SECOND)
    assert len(strategy.sent) == 1, "the first quote received after the arming is guarded"
    strategy.observe(str(fresh.level), T0 + 3 * SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[-1].trailing_offset == distance
    assert strategy.sent[-1].quantity == Quantity.from_int(5)
    assert strategy.sent[-1].trigger_price != Price.from_str("12.00")


def test_mover_cancellation_limit_is_logged_on_each_cancel_attempt() -> None:
    strategy = Harness()
    strategy.cached.held = [position("100")]
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    own = strategy.sent[0]
    mover = StrategyId("Mover-001")
    strategy.cached.held.append(position("50", mover))
    strategy.cached.working.append(
        working_sell(
            "O-20261113-190000-001-001-1",
            qty="50",
            filled="0",
            strategy_id=mover,
        )
    )
    strategy.observe("10.01", T0 + SECOND)
    assert strategy.canceled == [own.client_order_id]
    assert (
        sum(
            "NAMED LIMIT" in note and "Mover-001" in note and "canceling our sell" in note
            for note in strategy.said
        )
        == 1
    )
    strategy.cancel_requested.clear()  # A CancelRejected permits a second attempt.
    strategy.observe("10.02", T0 + 2 * SECOND)
    assert len(strategy.canceled) == 2
    assert (
        sum(
            "NAMED LIMIT" in note and "Mover-001" in note and "canceling our sell" in note
            for note in strategy.said
        )
        == 2
    )


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
def test_found_long_self_arms_from_bid_minus_row_distance_rounded_up(row, monkeypatch) -> None:
    strategy = Harness(row=row)
    strategy.cached.latest = quote("10.0017")
    strategy.cached.history = minute_bars("0.06")
    monkeypatch.setattr(strategy.cached, "instrument", lambda iid: equity(iid, precision=4))
    distance, planned = task_distance(strategy, row)
    expected = up_on_step(Decimal("10.0017") - distance)
    strategy.on_start()
    assert AAA in strategy.slots, "startup must find the reconciled EXTERNAL long"
    level = strategy.slots[AAA].level
    assert level is not None
    assert level.as_decimal() == expected
    strategy.observe("10.0017", T0 + SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe("10.0017", T0 + 2 * SECOND)
    assert len(strategy.sent) == 1
    order = strategy.sent[0]
    assert order.trigger_price.as_decimal() == expected
    assert expected >= Decimal("10.0017") - distance
    assert expected == expected.quantize(tick(expected))
    assert order.trailing_offset == distance
    assert order.quantity == Quantity.from_int(10)
    assert order.is_reduce_only and order.side == OrderSide.SELL
    assert any(planned.source() in line for line in strategy.said)


def test_new_owner_fill_after_start_self_arms_on_the_timer() -> None:
    strategy = Harness()
    strategy.cached.held = []
    strategy.on_start()
    assert not strategy.slots
    strategy.cached.held = [position()]
    strategy.cached.held[0].ts_opened = T0 + SECOND
    strategy.cached.latest = quote("10.00", T0 + SECOND)
    strategy.test_clock.set_time(T0 + SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert AAA in strategy.slots, "an EXTERNAL fill must not need adopt_holding"
    strategy.observe("10.00", T0 + 2 * SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe("10.00", T0 + 3 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].quantity == Quantity.from_int(10)
    assert strategy.subscriptions == [(AAA, {"feed": "sip"})]
    assert strategy.bar_subscriptions == [
        (BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"), {"feed": "sip"})
    ]


def test_supplied_level_binds_its_symbol_and_another_found_long_self_arms(monkeypatch) -> None:
    supplied = Price.from_str("9.90")
    strategy = Harness(levels={AAA: supplied})
    other = position("7")
    other.instrument_id, other.id = BBB, PositionId("BBB.ALPACA-EXTERNAL")
    strategy.cached.held.append(other)
    monkeypatch.setattr(strategy.cached, "quote", lambda iid: quote("10.00", T0, iid=iid))
    distance, _ = task_distance(strategy, "OWN1")
    strategy.on_start()
    assert strategy.slots[AAA].level == supplied
    assert BBB in strategy.slots, "a supplied AAA level must not hide an uncovered BBB holding"
    level = strategy.slots[BBB].level
    assert level is not None
    assert level.as_decimal() == up_on_step(Decimal("10.00") - distance)
    strategy.on_quote(quote("10.00", T0 + SECOND // 2, iid=AAA))  # AAA's first new quote sends
    strategy.on_quote(quote("10.00", T0 + SECOND, iid=BBB))
    assert len(strategy.sent) == 1, "BBB's first quote received after its arming is guarded"
    strategy.on_quote(quote("10.00", T0 + 2 * SECOND, iid=BBB))
    assert len(strategy.sent) == 2
    orders = {order.instrument_id: order for order in strategy.sent}
    assert orders[AAA].trigger_price == supplied
    assert orders[BBB].trigger_price != supplied
    assert orders[BBB].quantity == Quantity.from_int(7)


def test_self_discovery_preserves_the_refusal_of_an_invalid_supplied_level() -> None:
    # This instrument only represents cents; the supplied carry level cannot be adopted.
    supplied = Price.from_str("9.9017")
    strategy = Harness(levels={AAA: supplied})
    strategy.on_start()
    strategy.observe("10.00", T0 + SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert not strategy.sent, "discovery must not replace a refused carry level with its own"
    assert AAA not in strategy.slots
    # Dropped at the refusal, and its holding named unprotected (the lead's ruling, 15:55).
    assert AAA not in strategy.levels
    assert "supplied level" in strategy.frozen[AAA]
    assert "unprotected" in strategy.frozen[AAA]


@pytest.mark.parametrize("reason", ["row_raises", "over_ceiling", "no_quote", "stale", "crossed"])
def test_unplannable_found_long_is_recorded_and_logged_unprotected_every_step(
    reason, monkeypatch
) -> None:
    strategy = Harness()
    if reason == "row_raises":

        def refuse(*_args):
            raise ValueError("synthetic row refusal")

        monkeypatch.setattr(protect, "row_distance", refuse)
    elif reason == "over_ceiling":
        strategy.cached.history = minute_bars("1.50")
    elif reason == "no_quote":
        strategy.cached.latest = None
    elif reason == "stale":
        strategy.test_clock.set_time(T0 + 61 * SECOND)
    else:
        strategy.cached.latest = QuoteTick(
            AAA,
            Price.from_str("10.00"),
            Price.from_str("9.99"),
            Quantity.from_int(100),
            Quantity.from_int(100),
            T0,
            T0,
        )
    strategy.on_start()
    for expected in (1, 2, 3):
        assert not strategy.sent
        assert AAA in strategy.frozen, "unprotected holdings need a named state record"
        assert "unprotected" in strategy.frozen[AAA]
        assert "OWN1" in strategy.frozen[AAA]
        named = [line for line in strategy.said if "AAA.ALPACA" in line and "unprotected" in line]
        assert len(named) == expected, "every failed planning step must name the unprotected long"
        if expected < 3:
            strategy._on_step(cast(Any, SimpleNamespace()))


def test_missing_row_and_f2_are_refused_by_name() -> None:
    with pytest.raises(TypeError, match="row"):
        OwnerProtectorConfig()  # ty: ignore[missing-argument] - public refusal
    with pytest.raises(ValueError, match="F2"):
        OwnerProtectorConfig(row="F2")


def test_start_revalidates_and_refuses_f2_by_name() -> None:
    strategy = Harness()
    strategy.settings.row = "F2"
    with pytest.raises(ValueError, match="F2"):
        strategy.on_start()
    assert not strategy.sent and not strategy.slots


@pytest.mark.parametrize("below", [Decimal(0), Decimal("0.01")])
def test_self_arming_first_step_and_duplicate_quote_cannot_elect(below) -> None:
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    assert AAA in strategy.slots
    assert strategy.slots[AAA].level is None
    strategy.observe("10.00", T0 + SECOND)
    level = strategy.slots[AAA].level
    assert level is not None
    assert not strategy.sent, "the new holding's first observation must be guarded"
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.observe(str(level.as_decimal() - below), T0 + SECOND)
    assert not strategy.sent, "a same-step quote at/below the level is not confirmation"
    strategy.observe(str(level.as_decimal() - below), T0 + 2 * SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe(str(level.as_decimal() - below), T0 + 3 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == level


@pytest.mark.parametrize(
    "row,width,count,source",
    [
        ("OWN1", "0.06", 14, "from atr"),
        ("OWN3", "0.06", 14, "from atr"),
        ("OWN1", "0.01", 14, "from spread floor"),
        ("OWN1", "0.06", 0, "ATR cold"),
    ],
)
def test_self_arming_logs_the_task_distance_and_source(row, width, count, source) -> None:
    strategy = Harness(row=row)
    strategy.cached.history = minute_bars(width, count)
    distance, planned = task_distance(strategy, row)
    strategy.on_start()
    assert any(
        f"armed distance {distance}" in line and planned.source() in line and source in line
        for line in strategy.said
    )


def test_self_arming_fixes_distance_once_for_the_holding(monkeypatch) -> None:
    strategy = Harness()
    strategy.cached.history = minute_bars("0.06")
    calls = []
    original = protect.row_distance

    def planned(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(protect, "row_distance", planned)
    strategy.on_start()
    assert len(calls) == 1
    distance = strategy.slots[AAA].distance
    strategy.cached.history = minute_bars("1.00")  # Would refuse if recomputed.
    strategy.observe("10.00", T0 + SECOND)
    strategy.observe("10.50", T0 + 2 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert len(calls) == 1 and len(strategy.sent) == 1
    assert strategy.slots[AAA].distance is distance


def test_remaining_long_after_our_sale_uses_the_rounded_up_new_level(monkeypatch) -> None:
    strategy = Harness()
    monkeypatch.setattr(strategy.cached, "instrument", lambda iid: equity(iid, precision=4))
    strategy.cached.history = minute_bars("0.06")
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    new_quote(strategy)
    first = strategy.sent[0]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="10",
            strategy_id=PROTECTOR,
            status=OrderStatus.FILLED,
            is_closed=True,
            trigger_price=Price.from_str("12.00"),
        )
    ]
    strategy.cached.held = [position("5")]
    strategy.observe("10.0017", T0 + SECOND)
    distance, _ = task_distance(strategy, "OWN1")
    expected = up_on_step(Decimal("10.0017") - distance)
    level = strategy.slots[AAA].level
    assert level is not None
    assert level.as_decimal() == expected
    assert len(strategy.sent) == 1
    strategy.observe("10.0017", T0 + 2 * SECOND)
    assert len(strategy.sent) == 1, "the first quote received after the arming is guarded"
    strategy.observe("10.0017", T0 + 3 * SECOND)
    assert strategy.sent[1].trigger_price.as_decimal() == expected
    assert strategy.sent[1].quantity == Quantity.from_int(5)


def test_discovery_does_not_restart_an_explicitly_stopped_instrument() -> None:
    strategy = Harness()
    strategy.on_start()
    strategy.stop_instrument(AAA)
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.observe("9.00", T0 + SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert AAA not in strategy.slots and not strategy.sent


class Owner(Strategy):
    """A synthetic pre-existing owner: fill before adoption, under EXTERNAL ownership."""

    def __new__(cls) -> Self:
        return super().__new__(cls, StrategyConfig(strategy_id=EXTERNAL, oms_type=OmsType.NETTING))  # ty: ignore[too-many-positional-arguments]

    def __init__(self) -> None:
        super().__init__(StrategyConfig(strategy_id=EXTERNAL, oms_type=OmsType.NETTING))
        self.bought = False

    def on_start(self) -> None:
        self.subscribe_quotes(AAA)

    def on_quote(self, quote) -> None:
        if not self.bought:
            self.bought = True
            self.submit_order(self.order_factory.market(AAA, OrderSide.BUY, Quantity.from_int(10)))


class SimulatedProtector(OwnerProtector):
    def __new__(cls, level: str) -> Self:
        return super().__new__(cls, OwnerProtectorConfig(strategy_id=PROTECTOR, row="OWN1"))  # ty: ignore[too-many-positional-arguments]

    def __init__(self, level: str) -> None:
        super().__init__(OwnerProtectorConfig(strategy_id=PROTECTOR, row="OWN1"))
        self.level = Price.from_str(level)
        self.adopted = False
        self.submits: list[tuple[Any, dict | None]] = []
        self.fills: list[Any] = []
        self.after_adopt: list[Any] = []
        self.stop_on_result = False
        self.refusals: list[Any] = []

    def on_start(self) -> None:
        super().on_start()
        self.subscribe_quotes(AAA, params={"feed": "sip"})

    def on_quote(self, quote) -> None:
        if not self.adopted and self.cache.positions_open(instrument_id=AAA, strategy_id=EXTERNAL):
            self.adopted = True
            self.adopt_holding(AAA, self.level)
            self.after_adopt = self.cache.orders(strategy_id=self.strategy_id)
        super().on_quote(quote)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submits.append((order, params))
        super().submit_order(order, position_id, client_id, params)

    def on_order_filled(self, event) -> None:
        self.fills.append(event)
        if self.stop_on_result:
            self.shutdown_system("synthetic protection filled")

    def on_order_denied(self, event) -> None:
        self.refusals.append(event)
        if self.stop_on_result:
            self.shutdown_system("synthetic protection denied")

    def on_order_rejected(self, event) -> None:
        self.refusals.append(event)
        if self.stop_on_result:
            self.shutdown_system("synthetic protection rejected")


@pytest.mark.parametrize("level", ["9.90", "10.00"])
def test_cash_backtest_names_its_external_position_selection_limit(level) -> None:
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
    owner = Owner()
    protector = SimulatedProtector(level)
    engine.add_strategy(owner)
    engine.add_strategy(protector)
    engine.add_data(
        [
            quote(bid, T0 + step * SECOND)
            for step, bid in enumerate(["10.00", "10.00", "9.89", "9.88"])
        ]
    )
    try:
        engine.run()
        assert owner.strategy_id == EXTERNAL
        assert not protector.fills
        assert len(protector.refusals) == 1
        assert "Short selling not permitted on a CASH account" in protector.refusals[0].reason
        assert len(protector.submits) == 1
        assert protector.submits[0][0].is_reduce_only
        assert protector.submits[0][1] == {"extended_hours": True}
        positions = engine.cache.positions()
        assert len(positions) == 1
        assert positions[0].strategy_id == EXTERNAL
        assert positions[0].quantity == Quantity.from_int(10)
        assert not engine.cache.positions(strategy_id=protector.strategy_id)
        if level == "10.00":
            assert not protector.after_adopt
    finally:
        engine.dispose()


class SyntheticData(MarketDataClient):
    """Pinned Python adapter contract; only in-memory instrument and quote outputs."""

    async def _connect(self) -> None:
        self._handle_instrument(equity())
        self.publishing = False

    async def _disconnect(self) -> None:
        pass

    async def _subscribe_quotes(self, command) -> None:
        if not self.publishing:
            self.publishing = True
            self.create_task(self.publish_tape())

    async def _unsubscribe_quotes(self, command) -> None:
        pass

    async def _subscribe_bars(self, command) -> None:
        pass  # No historical request; this synthetic node starts with a cold ATR.

    async def _unsubscribe_bars(self, command) -> None:
        pass

    async def publish_tape(self) -> None:
        started = self.clock.timestamp_ns()
        for step, bid in enumerate(["10.00", "10.00", "9.89", "9.88"]):
            self._handle_data(quote(bid, started + step * SECOND))
            await asyncio.sleep(0.025)  # allow the queued core to process the preceding fill


class SyntheticExecution(ExecutionClient):
    """Simulate venue acknowledgements with actual core order/fill events, never a request."""

    async def _connect(self) -> None:
        cash = Money.from_str("100000 USD")
        self.generate_account_state(
            [AccountBalance(cash, Money.from_str("0 USD"), cash)],
            [],
            True,
            self.clock.timestamp_ns(),
        )
        self.released_params: list[dict] = []

    async def _disconnect(self) -> None:
        pass

    async def _generate_order_status_reports(self, command):
        return []

    async def _generate_fill_reports(self, command):
        return []

    async def _generate_position_status_reports(self, command):
        return []

    async def _submit_order(self, command) -> None:
        order = command.order
        tick = self.cache.quote(order.instrument_id)
        assert tick is not None
        if order.side == OrderSide.SELL:
            assert order.order_type == OrderType.LIMIT
            assert order.is_reduce_only
            assert order.price <= tick.bid_price
            held = self.cache.positions_open(instrument_id=AAA, strategy_id=EXTERNAL)
            assert len(held) == 1
            assert order.quantity <= held[0].quantity
            assert command.position_id is None
            self.released_params.append(command.params)
        else:
            assert order.strategy_id == EXTERNAL
        # TradeId holds at most 36 characters; a canon-protect- client order ID is longer.
        venue_id = VenueOrderId(str(order.client_order_id))
        self.generate_order_submitted(order)
        self.generate_order_accepted(order, venue_id, self.clock.timestamp_ns())
        self.generate_order_filled(
            order,
            venue_id,
            None,
            TradeId(str(order.client_order_id)[-36:]),
            order.quantity,
            tick.bid_price if order.side == OrderSide.SELL else tick.ask_price,
            USD,
            Money.from_str("0 USD"),
            LiquiditySide.TAKER,
            self.clock.timestamp_ns(),
        )


@pytest.mark.parametrize("level", ["9.90", "10.00"])
def test_real_node_reduces_external_inventory_without_claim_or_ownership_transfer(level) -> None:
    """reconciliation.md:305-335 through the actual execution engine and emulator.

    The native backtest matcher selects a strategy-owned position before submission and cannot
    exercise this live-fill fallback. These synthetic adapter clients implement the pinned
    examples/live/_template contracts without transport, credentials, files or broker clients.
    """
    gc.collect()  # Upstream's collect_node_cycles fixture releases the previous thread-local node.
    clients: list[SyntheticExecution] = []

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return SyntheticData(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = SyntheticExecution(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )
            clients.append(client)
            return client

    node = LiveNode.build(
        "OWNER-PROTECTION-TEST",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": DataClientConfig()},
            exec_clients={"ALPACA": ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    owner = Owner()
    protector = SimulatedProtector(level)
    protector.stop_on_result = True
    node.add_strategy(owner)
    node.add_strategy(protector)

    async def run() -> None:
        async with asyncio.timeout(5):
            await node.run_async()

    try:
        asyncio.run(run())
        assert not protector.refusals
        assert len(protector.fills) == 1
        assert protector.fills[0].order_side == OrderSide.SELL
        assert protector.fills[0].last_qty == Quantity.from_int(10)
        assert len(protector.submits) == 1
        assert clients[0].released_params == [{"extended_hours": True}]
        positions = node.cache.positions()
        assert len(positions) == 1
        assert positions[0].strategy_id == EXTERNAL
        assert positions[0].is_closed
        assert not node.cache.positions(strategy_id=protector.strategy_id)
        if level == "10.00":
            assert not protector.after_adopt
    finally:
        node.dispose()


@pytest.mark.parametrize("at_start", [True, False], ids=["reconciled-long", "later-owner-fill"])
def test_engine_starts_protector_and_self_arms_external_inventory(at_start) -> None:
    """Real startup reconciliation / new fill, real cache, emulator and reduce-only execution.

    The only transport is the in-memory adapter template. The startup case supplies an old
    position report without fill history; the other case's EXTERNAL seed fills after on_start.
    No caller supplies a level or invokes adopt_holding, on_start or _on_step by hand.
    """
    gc.collect()
    clients: list[SyntheticExecution] = []

    class Recording(OwnerProtector):
        def __init__(self, config):
            super().__init__(config)
            self.start_quantity = Decimal(0)
            self.started_with_slot = False
            self.submits: list[Any] = []
            self.fills: list[Any] = []
            self.said: list[str] = []

        def on_start(self):
            self.start_quantity = sum(
                (p.signed_qty for p in self.cache.positions_open(strategy_id=EXTERNAL)),
                Decimal(0),
            )
            super().on_start()
            self.started_with_slot = AAA in self.slots

        def _say(self, text):
            self.said.append(text)

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.submits.append(order)
            super().submit_order(order, position_id, client_id, params)

        def on_order_filled(self, event):
            self.fills.append(event)
            self.shutdown_system("automatic owner protection filled")

    class AutomaticData(SyntheticData):
        async def _connect(self):
            await super()._connect()
            self._handle_data(quote("10.00", self.clock.timestamp_ns()))

        async def publish_tape(self):
            # The late-fill case gives the real one-second discovery timer time to fire.
            for bid in [*(["10.00"] * 15), "9.78", "9.77"]:
                self._handle_data(quote(bid, self.clock.timestamp_ns()))
                await asyncio.sleep(0.1)

    class ReconciledExecution(SyntheticExecution):
        async def _generate_position_status_reports(self, command):
            if not at_start:
                return []
            now = self.clock.timestamp_ns()
            return [
                PositionStatusReport(
                    AccountId("ALPACA-001"),
                    AAA,
                    PositionSide.LONG,
                    Quantity.from_int(10),
                    now - 7 * 24 * 3600 * SECOND,
                    now,
                    avg_px_open=Decimal("10.00"),
                )
            ]

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return AutomaticData(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            client = ReconciledExecution(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )
            clients.append(client)
            return client

    node = LiveNode.build(
        "AUTOMATIC-OWNER-PROTECTION-TEST",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": DataClientConfig()},
            exec_clients={"ALPACA": ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    protector = Recording(OwnerProtectorConfig(strategy_id=PROTECTOR, row="OWN1"))
    if not at_start:
        node.add_strategy(Owner())
    node.add_strategy(protector)

    async def finish_if_unprotected():
        await asyncio.sleep(2.5)
        protector.shutdown_system("end bounded self-arming test")

    async def run():
        finish = asyncio.create_task(finish_if_unprotected())
        try:
            async with asyncio.timeout(5):
                await node.run_async()
        finally:
            finish.cancel()
            await asyncio.gather(finish, return_exceptions=True)

    try:
        asyncio.run(run())
        assert protector.start_quantity == (Decimal(10) if at_start else Decimal(0))
        assert protector.started_with_slot == at_start
        assert len(protector.submits) == 1, "EXTERNAL inventory must arm without a supplied level"
        assert protector.submits[0].trigger_price == Price.from_str("9.79")
        assert protector.submits[0].trailing_offset == Decimal("0.21")
        assert len(protector.fills) == 1
        assert protector.fills[0].last_qty == Quantity.from_int(10)
        assert clients[0].released_params == [{"extended_hours": True}]
        assert any("ATR cold" in line for line in protector.said)
        assert all(p.strategy_id == EXTERNAL and p.is_closed for p in node.cache.positions())
        assert not node.cache.positions(strategy_id=PROTECTOR)
    finally:
        node.dispose()
