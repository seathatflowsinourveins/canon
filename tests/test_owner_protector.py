"""Unit 10: EXTERNAL custody, uncovered shares and a two-observation adoption guard.

All positions, orders and quotes are synthetic. The stand-in records commands using the real
upstream order factory; the final cases run the pinned BacktestEngine and its emulator. U-e's
level books use pytest's temp tree, isolated through CLAUDE_CONFIG_DIR. No real paper state,
catalog, ledger, credential or network service is used by this file.
"""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
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
from nautilus_trader.live import BarsResponse
from nautilus_trader.live import InstrumentProviderConfig
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import QuotesResponse
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
from canon_trading.start_level import Entry as start_level_entry
from canon_trading.start_level import LevelBook

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


@pytest.fixture(autouse=True)
def isolated_level_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart test's state always belongs to this test, never the real watch folder."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


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
        self.earlier_quotes: list[QuoteTick] = []
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

    def quotes(self, iid):
        """The pin's cache is newest arrival first, without sorting by venue stamp."""
        latest = [] if self.latest is None else [self.latest]
        return [q for q in [*latest, *self.earlier_quotes] if q.instrument_id == iid]

    def instrument(self, iid):
        return equity(iid)


class VenueOrders:
    """A stand-in for the runner's raw GET reader (OwnerProtectorConfig.trading): /assets and
    /orders only, no network. `orders` are Alpaca order objects as GET /orders returns them, and
    the query is honoured as Alpaca does (review F9): `side`, `symbols`, and `after` and `until`
    bounding submission, both exclusive (alpaca-py c42add7c0a70 requests.py:211-212), ordered by
    `direction`, at most `limit`."""

    def __init__(
        self,
        orders: list[dict[str, Any]] | None = None,
        *,
        asset: Any = None,
        failure: Exception | None = None,
        answer: Any = None,
    ) -> None:
        self.orders = list(orders or [])
        self.asset = asset
        self.failure = failure
        self.answer = answer
        self.reads: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, route: str, params: dict[str, Any] | None = None) -> Any:
        self.reads.append((route, params))
        if route.startswith("/assets/"):
            return self.asset
        assert route == "/orders" and params is not None, route
        if self.failure is not None:
            raise self.failure
        if self.answer is not None:
            return self.answer
        after, until = params["after"], params.get("until")
        rows = [
            order
            for order in self.orders
            if order["side"] == params["side"]
            and order["symbol"] in params["symbols"].split(",")
            and _venue_ns(after) < _venue_ns(order["submitted_at"])
            and (until is None or _venue_ns(order["submitted_at"]) < _venue_ns(until))
        ]
        rows.sort(key=lambda o: _venue_ns(o["submitted_at"]), reverse=params["direction"] == "desc")
        return rows[: params["limit"]]


def _venue_ns(text: str) -> int:
    moment = datetime.fromisoformat(text) - datetime(1970, 1, 1, tzinfo=UTC)
    return moment // timedelta(microseconds=1) * 1000


def _venue_iso(ns: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)).isoformat()


_venue_ids = iter(range(1, 10**9))


def venue_fill(
    side: str,
    qty: str,
    symbol: str = "AAA",
    *,
    submitted: int = T0 + 60 * SECOND,
    filled: int | None = T0 + 90 * SECOND,
) -> dict[str, Any]:
    """An order as GET /orders shows it; by default submitted and filled between a restart test's
    book write (T0 + 1 s) and its start (T0 + 180 s)."""
    return {
        "id": f"venue-{next(_venue_ids)}",
        "symbol": symbol,
        "side": side,
        "filled_qty": qty,
        "status": "filled",
        "submitted_at": _venue_iso(submitted),
        "filled_at": None if filled is None else _venue_iso(filled),
    }


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

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ) -> str:
        self.on_historical_bars(self.cache.bars(bar_type) or [])
        if callback is not None:
            callback("bars-harness")
        return "bars-harness"

    def request_quotes(
        self,
        instrument_id,
        start=None,
        end=None,
        limit=None,
        client_id=None,
        params=None,
        callback=None,
    ) -> str:
        tick = self.cache.quote(instrument_id)
        self.on_historical_quotes([] if tick is None else [tick])
        if callback is not None:
            callback("quotes-harness")
        return "quotes-harness"

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


def test_adoption_before_reconciliation_keeps_the_level_for_the_later_long() -> None:
    """P11 (the lead, 2026-11-13 16:55) replaces unit 10's drop: a level supplied before its
    holding is selectable is kept and arms that holding when it appears, never the row's
    self-arm below it."""
    strategy = Harness()
    strategy.settings.trading = VenueOrders()  # U-e B: no buy filled after it was kept.
    strategy.cached.held = []
    level = Price.from_str("10.00")
    strategy.adopt_holding(AAA, level)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert not strategy.sent
    assert strategy.levels[AAA] == level, "a level supplied before its holding must be kept"
    strategy.cached.held = [position()]
    strategy._on_step(cast(Any, SimpleNamespace()))
    distance, _ = task_distance(strategy, "OWN1")
    self_armed = Price.from_str(str(up_on_step(Decimal("10.00") - distance)))
    assert strategy.slots[AAA].level == level
    assert self_armed != level
    assert not strategy.sent  # Reconciliation's first cached quote cannot elect.
    strategy.observe("9.99", T0 + SECOND)
    assert not strategy.sent, "the first quote received after the arming is the guarded one"
    strategy.observe("9.98", T0 + 2 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == level
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


# Session-day completeness and old bars are tested in test_owner_protector_cold_start.py.


@pytest.mark.parametrize("bad_book", ["over_ceiling"])
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


def test_level_supplied_while_flat_is_kept_by_name_and_not_dropped() -> None:
    strategy = Harness()
    strategy.cached.held = []
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    assert strategy.levels[AAA] == Price.from_str("10.00")
    assert AAA not in strategy.slots
    assert any(
        "AAA.ALPACA" in note and "level kept" in note and "no selectable EXTERNAL holding" in note
        for note in strategy.said
    )
    assert not any("dropped" in note for note in strategy.said)


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


@pytest.mark.parametrize(
    "reason", ["row_raises", "over_ceiling", "no_quote", "prior_part", "crossed"]
)
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
    elif reason == "prior_part":
        strategy.test_clock.set_time(T0 + 6 * 3600 * SECOND)
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
    startup_lines = len(
        [line for line in strategy.said if "AAA.ALPACA" in line and "unprotected" in line]
    )
    for expected in (1, 2, 3):
        assert not strategy.sent
        assert AAA in strategy.frozen, "unprotected holdings need a named state record"
        assert "unprotected" in strategy.frozen[AAA]
        assert "OWN1" in strategy.frozen[AAA]
        named = [line for line in strategy.said if "AAA.ALPACA" in line and "unprotected" in line]
        assert len(named) == startup_lines + expected - 1, "every step names the unprotected long"
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


def restart_record(**changes: Any) -> dict[str, Any]:
    record = {
        "account_label": "shape",
        "instrument_id": str(AAA),
        "protection_start_ns": T0,
        "last_level": "1.10",
        "quantity": "10",
        "written_ns": T0 + SECOND,
        "run": "shape-prior",
        # Review fix round, ruling (a) (2026-11-13 17:56): a carried entry with no distance never
        # takes the cold fallback, so a carry case keeps the distance OWN1's cold trail gives
        # here (0.04); an old entry passes distance=None. See FIXROUND-L1.md §Review fix round.
        "distance": "0.04",
    }
    record.update(changes)
    if record["distance"] is None:
        del record["distance"]
    return record


def restart_harness(path: Path, **settings: Any) -> Harness:
    """Set the new fields after construction so the red cases reach the old arming code."""
    strategy = Harness()
    strategy.settings.account_label = "shape"
    strategy.settings.level_book_path = path
    strategy.settings.run = "shape-next"
    strategy.settings.session_part = "regular"
    strategy.settings.trading = VenueOrders()  # U-e B: no sell since the entry: same holding.
    for name, value in settings.items():
        setattr(strategy.settings, name, value)
    strategy.cached.latest = quote("1.05", T0 + 180 * SECOND)
    strategy.test_clock.set_time(T0 + 180 * SECOND)
    strategy.cached.history = [
        Bar(
            BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"),
            Price.from_str(close),
            Price.from_str(high),
            Price.from_str(close),
            Price.from_str(close),
            Quantity.from_int(100),
            at,
            at,
        )
        for at, close, high in (
            (T0 - 60 * SECOND, "1.06", "1.06"),
            (T0, "1.06", "1.06"),
            (T0 + 60 * SECOND, "1.06", "1.06"),
            (T0 + 120 * SECOND, "1.06", "1.06"),
            (T0 + 180 * SECOND, "4.00", "4.00"),  # incomplete at start
        )
    ]
    # U-h (the lead's 18:02 terms): every completed bar of the session day counts, so these
    # closes sit flat at 1.06 and their ATR is 0; OWN1's trail is the 2-spread floor, 0.04
    # (C0's last-hour mix of 2.00, 5.00 and 1.03 would now count whole).
    return strategy


def test_restart_keeps_previous_level_and_first_observation_guard(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.10")
    assert not strategy.sent
    strategy.observe("1.05", T0 + 181 * SECOND)
    assert not strategy.sent, "the carried breach must not sell on its first observation"
    strategy.observe("1.04", T0 + 182 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == Price.from_str("1.10")
    assert strategy.params == [{"extended_hours": True}]


def test_no_carried_level_self_arms_from_the_bid_not_from_closes(tmp_path: Path) -> None:
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid.
    # A found long with no entry arms at unit 10's bid less the row distance (1.05 - 0.04),
    # not at the closes' 1.06 - 0.04.
    strategy = restart_harness(tmp_path / "missing.json")
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01")
    assert any(
        line.startswith("AAA.ALPACA: no carried level (no previous entry); self-armed at 1.01")
        for line in strategy.said
    ), strategy.said


@pytest.mark.parametrize(
    "previous,supplied,want,source",
    [
        ("1.10", "1.00", "1.10", "; the carried level armed, over the supplied 1.00"),
        ("1.00", "1.12", "1.12", "; the supplied level 1.12 armed, over the carried 1.00"),
        ("1.00", "1.00", "1.00", ""),
    ],
)
def test_restart_names_winning_floor_and_keeps_supplied_level(
    tmp_path: Path, previous, supplied, want, source
) -> None:
    # superseded by the lead's ruling 2026-11-13 17:43: the level is the carried level; closes are not a bid:
    # the greater of the carried and supplied levels, never a level rebuilt from closes.
    # Ruling (b), 2026-11-13 17:56: the line names which armed and what the other was.
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record(last_level=previous)]}))
    strategy = restart_harness(path)
    strategy.levels[AAA] = Price.from_str(supplied)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str(want)
    assert any(
        s.startswith(f"AAA.ALPACA: carried level {previous} and distance 0.04 from run shape-prior")
        and s.endswith(source or "today's rule gives 0.05")
        for s in strategy.said
    ), strategy.said


def test_a_lower_supplied_level_never_loosens_a_carried_one(tmp_path: Path) -> None:
    """Ruling (b): carried 1.10 and supplied 1.00 arm at 1.10, and the sell's trigger is 1.10."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record()]}))
    strategy = restart_harness(path)
    strategy.levels[AAA] = Price.from_str("1.00")
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("1.10")


@pytest.mark.parametrize(
    "change",
    [
        {"account_label": "other"},
        {"instrument_id": str(BBB)},
    ],
)
def test_previous_level_is_bound_to_exact_holding(tmp_path: Path, change: dict) -> None:
    """U-e A: the key is account and instrument; whether the holding is the same is U-e B's."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record(**change)]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01")  # 17:43: unit 10's self-arm


@pytest.mark.parametrize("quantity", ["4", "20"])
def test_changed_quantity_keeps_the_same_holdings_previous_level(tmp_path: Path, quantity) -> None:
    # Review F4: a change of quantity carries only where the venue's fills explain it: 4 to 10
    # by a buy of 6, 20 to 10 by a sale of 10. See FIXROUND-L1.md §Review fix round.
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record(quantity=quantity)]}))
    fills = [venue_fill("buy", "6")] if quantity == "4" else [venue_fill("sell", "10")]
    strategy = restart_harness(path, trading=VenueOrders(fills))
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.10")
    if quantity == "4":
        assert any("added shares" in s and "carried level" in s for s in strategy.said)


@pytest.mark.parametrize("bad", ["missing", "malformed", "field", "unreadable"])
def test_bad_book_is_named_for_holding_and_rebuild_still_arms(tmp_path: Path, bad) -> None:
    path = tmp_path / "levels.json"
    if bad == "malformed":
        path.write_text("{")
    elif bad == "field":
        path.write_text(json.dumps({"schema": 1, "entries": [restart_record(last_level=1.10)]}))
    elif bad == "unreadable":
        path.mkdir()
    strategy = restart_harness(path)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01")  # 17:43: unit 10's self-arm
    assert any(s.startswith("AAA.ALPACA: no carried level") for s in strategy.said)
    if bad == "field":
        assert any("previous level unavailable: entry 0" in s for s in strategy.said)


@pytest.mark.parametrize(
    "asset",
    [
        {"overnight_tradable": False},
        {"overnight_tradable": True, "overnight_halted": True},
        {"attributes": 3},
        None,
    ],
)
def test_uncoverable_overnight_is_named_on_each_run_and_preserves_book(
    tmp_path: Path, asset
) -> None:
    path = tmp_path / "levels.json"
    original = json.dumps({"schema": 1, "entries": [restart_record()]})
    path.write_text(original)
    reads: list[str] = []

    def get(route: str):
        reads.append(route)
        return asset

    for _ in range(2):
        strategy = restart_harness(path, session_part="overnight", trading=SimpleNamespace(get=get))
        strategy.on_start()
        strategy.observe("1.05", T0 + 181 * SECOND)
        strategy.observe("1.04", T0 + 182 * SECOND)
        assert not strategy.sent, "an ineligible overnight name received a protective order"
        assert any("AAA.ALPACA" in s and "uncoverable overnight:" in s for s in strategy.said)
        strategy.on_stop()
        # Item 4: the report names the holding frozen; its level and holding stay as they were.
        (row,) = json.loads(path.read_text())["entries"]
        kept = {k: v for k, v in row.items() if k not in ("state", "reason")}
        assert kept == restart_record()
        assert row["state"] == "frozen" and "uncoverable overnight:" in row["reason"]
    assert reads == ["/assets/AAA", "/assets/AAA"]


def test_eligible_overnight_reuses_asset_attributes_and_arms(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    strategy = restart_harness(
        path,
        session_part="overnight",
        trading=VenueOrders(asset={"attributes": ["overnight_tradable"]}),
    )
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.10")
    strategy.observe("1.05", T0 + 181 * SECOND)
    assert not strategy.sent
    strategy.observe("1.04", T0 + 182 * SECOND)
    assert len(strategy.sent) == 1


@pytest.mark.parametrize("armed", [False, True])
def test_stop_writes_current_trigger_or_retained_level(tmp_path: Path, armed) -> None:
    path = tmp_path / "levels.json"
    strategy = restart_harness(path)
    strategy.on_start()
    if armed:
        strategy.observe("1.20", T0 + 181 * SECOND)
        strategy.observe("1.20", T0 + 182 * SECOND)
        order = strategy.sent[0]
        strategy.cached.working = [
            working_sell(
                str(order.client_order_id),
                qty="10",
                filled="0",
                strategy_id=PROTECTOR,
                trigger_price=Price.from_str("1.15"),
                status=OrderStatus.EMULATED,
            )
        ]
    strategy.on_stop()
    assert path.is_file(), "stopping lost the run's last protection level"
    record = json.loads(path.read_text())["entries"][0]
    assert record["last_level"] == ("1.15" if armed else "1.01")  # 17:43: self-armed
    assert record["quantity"] == "10"
    assert record["protection_start_ns"] == T0
    assert record["run"] == "shape-next"
    assert record["written_ns"] == T0 + (182 if armed else 180) * SECOND


def test_failed_book_write_names_failure_and_stopping_still_cancels(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory" / "levels.json"
    path.parent.write_text("cannot contain a book")
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    strategy.on_stop()
    assert strategy.stopping
    assert strategy.canceled == [strategy.sent[0].client_order_id]
    assert any("level book write failed" in s for s in strategy.said)


def test_back_to_back_engine_runs_keep_the_peak_trigger_after_twenty_percent_fall(
    tmp_path: Path,
) -> None:
    from tests.test_owner_protector_blind import replay

    path = tmp_path / "levels.json"

    class Recording(OwnerProtector):
        # U-h's start requests answered from this replay's own cache (as uh-build4's blind files do).
        request_bars = Harness.request_bars
        request_quotes = Harness.request_quotes

        def __init__(self, config):
            super().__init__(config)
            self.commands: list[tuple[Any, Any]] = []
            self.observations: list[tuple[Price, int]] = []

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.commands.append((order, params))
            super().submit_order(order, position_id, client_id, params)

        def on_quote(self, quote):
            super().on_quote(quote)
            slot = self.slots.get(quote.instrument_id)
            if slot is not None and slot.level is not None:
                self.observations.append((slot.level, len(self.commands)))

    def configured():
        config = OwnerProtectorConfig(row="OWN1", strategy_id=PROTECTOR)
        config.account_label = "shape"
        config.level_book_path = path
        config.run = "shape-engine"
        config.trading = VenueOrders()
        return Recording(config)

    first = configured()
    with replay(strategies=(first,)) as tape:
        initial = first.slots[AAA].level
        tape.drive("11.00", "12.50", "12.50")
        order = tape.engine.cache.orders(strategy_id=first.strategy_id)[0]
        last = order.trigger_price
        assert last is not None and last > initial
    assert path.exists(), "the first engine run did not save its trigger at planned end"

    second = configured()
    with replay(strategies=(second,)) as tape:
        # replay starts at 10.00, twenty percent below 12.50, with the same synthetic holding.
        assert second.observations[0] == (last, 0)
        assert len(second.commands) == 1
        submitted, params = second.commands[0]
        assert submitted.trigger_price >= last
        released = tape.engine.cache.order(submitted.client_order_id)
        assert released.order_type == OrderType.LIMIT
        assert released.is_reduce_only
        assert params == {"extended_hours": True}


def book_levels(path: Path) -> dict[str, str]:
    return {
        row["instrument_id"]: row["last_level"] for row in json.loads(path.read_text())["entries"]
    }


def test_each_level_change_reaches_the_book_before_a_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10 (the lead, 2026-11-13): the book is written at each level change, not only at stop."""
    from canon_trading import rank_shared

    writes: list[tuple[Any, ...]] = []
    atomic = rank_shared.write_atomically

    def counted(path: Path, body: str) -> None:
        book = json.loads(body)
        writes.append(
            tuple((r["last_level"], r.get("state")) for r in book["entries"])
            + (book.get("last_step_ns"),)
        )
        atomic(path, body)

    monkeypatch.setattr(rank_shared, "write_atomically", counted)
    path = tmp_path / "levels.json"
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    order = strategy.sent[0]
    emulated = working_sell(
        str(order.client_order_id),
        qty="10",
        filled="0",
        strategy_id=PROTECTOR,
        trigger_price=Price.from_str("1.15"),
        status=OrderStatus.EMULATED,
    )
    strategy.cached.working = [emulated]
    strategy.observe("1.21", T0 + 183 * SECOND)
    assert book_levels(path) == {"AAA.ALPACA": "1.15"}, "the first rise was not written"
    emulated.trigger_price = Price.from_str("1.18")
    strategy.observe("1.22", T0 + 184 * SECOND)
    strategy.observe("1.22", T0 + 185 * SECOND)  # no change: no write
    strategy._on_step(cast(Any, SimpleNamespace()))
    # Once per change of level or state (item 4 adds the states), and the step's time once.
    assert writes == [
        (("1.01", "counting"), None),
        (("1.01", "counting"), None),
        (("1.01", "armed"), None),
        (("1.15", "armed"), None),
        (("1.18", "armed"), None),
        (("1.18", "armed"), {"shape": T0 + 185 * SECOND}),  # Review F5: per account label.
    ], "write once per change: 1.01 self-armed, 1.15, 1.18"
    del strategy, order, emulated  # Killed: discarded with no on_stop.
    gc.collect()
    assert book_levels(path) == {"AAA.ALPACA": "1.18"}

    restarted = restart_harness(path)
    restarted.on_start()
    assert restarted.slots[AAA].level == Price.from_str("1.18")
    restarted.observe("1.05", T0 + 181 * SECOND)
    assert not restarted.sent, "the carried breach must not sell on its first observation"
    restarted.observe("1.04", T0 + 182 * SECOND)
    assert restarted.sent[0].trigger_price == Price.from_str("1.18")


def test_a_failed_level_change_write_is_named_by_holding_and_protection_continues(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-a-directory" / "levels.json"
    path.parent.write_text("cannot contain a book")
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert len(strategy.sent) == 1, "a failed level write must never stop protection"
    assert any(
        "AAA.ALPACA" in note and "level book write failed" in note for note in strategy.said
    ), strategy.said
    assert not strategy.stopping


def test_engine_trigger_rise_reaches_the_book_while_the_run_continues(tmp_path: Path) -> None:
    """The pinned emulator raises the trigger in the cache with no strategy event (P10)."""
    from tests.test_owner_protector_blind import replay

    path = tmp_path / "levels.json"
    config = OwnerProtectorConfig(row="OWN1", strategy_id=PROTECTOR)
    config.account_label = "shape"
    config.level_book_path = path
    config.run = "shape-engine"
    strategy = EngineProtector(config)
    with replay(strategies=(strategy,)) as tape:
        tape.drive("11.00", "11.00")
        order = tape.engine.cache.orders(strategy_id=strategy.strategy_id)[0]
        first = order.trigger_price
        assert book_levels(path) == {"AAA.ALPACA": str(first)}
        tape.drive("12.50", "12.50")
        second = tape.engine.cache.order(order.client_order_id).trigger_price
        assert second > first
        assert book_levels(path) == {"AAA.ALPACA": str(second)}
        assert strategy.is_running()


@pytest.mark.parametrize("route", ["book", "levels"])
def test_a_kept_level_arms_a_late_appearing_long_at_that_level(tmp_path: Path, route) -> None:
    """P11 (the lead, 2026-11-13 16:55): a level whose holding is not selectable at start is
    kept and applied when the EXTERNAL long appears, with the first-observation guard."""
    path = tmp_path / "levels.json"
    if route == "book":
        path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    strategy = restart_harness(path)
    if route == "levels":
        strategy.levels[AAA] = Price.from_str("1.10")
    strategy.cached.held = []
    strategy.on_start()
    assert AAA not in strategy.slots
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.cached.held = [position()]
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert strategy.slots[AAA].level == Price.from_str("1.10"), "armed below the kept level"
    assert not strategy.sent
    strategy.observe("1.05", T0 + 181 * SECOND)
    assert not strategy.sent, "the kept breach must not sell on its first observation"
    strategy.observe("1.04", T0 + 182 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].trigger_price == Price.from_str("1.10")


@pytest.mark.parametrize("route", ["book", "levels"])
def test_a_kept_level_whose_holding_never_appears_is_named_and_its_entry_kept(
    tmp_path: Path, route
) -> None:
    path = tmp_path / "levels.json"
    original = json.dumps({"schema": 1, "entries": [restart_record()]})
    path.write_text(original)
    strategy = restart_harness(path)
    if route == "levels":
        strategy.levels[AAA] = Price.from_str("1.10")
    strategy.cached.held = []
    strategy.on_start()
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.on_stop()
    # Item 4 adds the step's time at file level; the entry itself is unchanged.
    assert json.loads(path.read_text())["entries"] == json.loads(original)["entries"], (
        "the book entry was deleted or lowered"
    )
    assert any("AAA.ALPACA" in note and "not applied" in note for note in strategy.said), (
        strategy.said
    )


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, UTC).isoformat()


def orders_query(side: str, since_ns: int) -> dict[str, Any]:
    """The walk's first page (review F1): GetOrdersRequest(status=all, side, symbols=[AAA], after
    92 days before `since_ns`, limit=500, direction=desc), as TradingClient.get_orders sends it
    (alpaca-py c42add7c0a70 alpaca/trading/client.py:120-125)."""
    return {
        "status": "all",
        "limit": 500,
        "after": _venue_iso(since_ns - 92 * 86_400 * SECOND),
        "direction": "desc",
        "side": side,
        "symbols": "AAA",
    }


def test_a_book_level_carries_when_the_venue_sold_less_than_its_quantity(tmp_path: Path) -> None:
    """U-e B: 9 of 10 sold since the entry was written, so never flat: the same holding."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    venue = VenueOrders([venue_fill("sell", "5"), venue_fill("sell", "4"), venue_fill("buy", "9")])
    strategy = restart_harness(path, trading=venue)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.10")
    # Review F1/F4: 9 sold and 9 bought explain the 10 held; a sell walk, then a buy walk.
    assert venue.reads == [("/orders", orders_query(side, T0 + SECOND)) for side in ("sell", "buy")]
    strategy.on_stop()
    (row,) = json.loads(path.read_text())["entries"]
    assert (row["last_level"], row["protection_start_ns"]) == ("1.10", T0)


def test_a_book_level_is_not_carried_when_the_venue_sold_it_out_since(tmp_path: Path) -> None:
    """U-e B: 10 of 10 sold since the entry: the account may have gone flat, a new holding."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    venue = VenueOrders([venue_fill("sell", "6"), venue_fill("sell", "4"), venue_fill("buy", "10")])
    strategy = restart_harness(path, trading=venue)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01"), "carried across a sell-out"
    assert any(
        "previous level not carried: a new holding: sold out since "
        f"{iso(T0 + SECOND)} (10 of 10 sold); it was 1.10" in note
        for note in strategy.said
    ), strategy.said
    strategy.on_stop()
    (row,) = json.loads(path.read_text())["entries"]
    assert (row["last_level"], row["run"]) == ("1.01", "shape-next")


@pytest.mark.parametrize(
    "venue",
    [
        None,
        VenueOrders(failure=OSError("neutral read failure")),
        VenueOrders(answer={"message": "not a list"}),
        VenueOrders([venue_fill("sell", "0")] * 500),
    ],
    ids=["no-reader", "read-fails", "not-a-list", "full-page"],
)
def test_a_book_level_is_not_carried_when_the_venue_is_unread(tmp_path: Path, venue) -> None:
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    strategy = restart_harness(path, trading=venue)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01"), "the self-arm still arms"
    assert any(
        "previous level not carried: venue orders unread" in note for note in strategy.said
    ), strategy.said
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("1.01")
    # Review F2/F9: the book keeps the entry as written, at its own level, not the self-arm's.
    strategy.on_stop()
    (row,) = json.loads(path.read_text())["entries"]
    assert (row["last_level"], row["run"], row["written_ns"]) == (
        "1.10",
        "shape-prior",
        T0 + SECOND,
    )


def test_a_late_reconciled_long_binds_the_kept_level_when_no_buy_filled_after_it() -> None:
    """U-e B for a supplied level: reconciled an hour late (its ts_opened is the reconciliation
    clock, item 1), with no buy filled since the level was kept: held all along, so it binds."""
    venue = VenueOrders([venue_fill("sell", "3")])
    strategy = Harness()
    strategy.settings.trading = venue
    strategy.cached.held = []
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    strategy._on_step(cast(Any, SimpleNamespace()))
    late = position("10")
    late.ts_opened = late.ts_init = T0 + 3600 * SECOND
    strategy.cached.held = [late]
    strategy.test_clock.set_time(T0 + 3600 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert strategy.slots[AAA].level == Price.from_str("10.00")
    assert venue.reads == [("/orders", orders_query("buy", T0))]


def test_a_buy_filled_after_the_kept_level_is_a_new_holding_that_self_arms() -> None:
    """Unit 10's P1-2 in venue terms: a buy an hour later is a fill an hour later."""
    strategy = Harness()
    strategy.settings.trading = VenueOrders([venue_fill("buy", "10")])
    strategy.cached.held = []
    strategy.adopt_holding(AAA, Price.from_str("10.00"))
    for _ in range(3):
        strategy._on_step(cast(Any, SimpleNamespace()))
    later = position("10")
    later.ts_opened = T0 + 3600 * SECOND
    strategy.cached.held = [later]
    # The lead, 19:23 (ii): a seed read after the fill that returned the T0 quote would make it
    # current (case 3); here the read returns nothing, so the first quote after the fill plans.
    strategy.cached.latest = None
    strategy.test_clock.set_time(T0 + 3600 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert any(
        "kept level 10.00 not applied: a buy of 10 filled after it was kept" in note
        for note in strategy.said
    ), strategy.said
    strategy.observe("8.00", T0 + 3601 * SECOND)
    distance, _ = task_distance(strategy, "OWN1")
    strategy.observe("7.99", T0 + 3602 * SECOND)
    strategy.observe("7.98", T0 + 3603 * SECOND)
    assert strategy.sent[0].trigger_price.as_decimal() == up_on_step(Decimal("8.00") - distance)


def history_bar(close: str, at: int) -> Bar:
    return Bar(
        BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL"),
        Price.from_str(close),
        Price.from_str(close),
        Price.from_str(close),
        Price.from_str(close),
        Quantity.from_int(100),
        at,
        at,
    )


FALLBACK = Decimal("0.0200")
"""The planner's fixed fallback distance in the ruling's sub-dollar shape."""


def planner_distance(
    monkeypatch: pytest.MonkeyPatch, width: Decimal = FALLBACK, *, fallback: bool = True
) -> list[Any]:
    """The planner falls back (a cold ATR) or plans a row distance of `width`; returns its calls."""
    from canon_trading.mover import TrailPlan

    calls: list[Any] = []

    def planned(book, symbol, row, planner):
        calls.append(symbol)
        plan = TrailPlan(
            trail=width / book.ask,
            exit_through=width / book.ask / 4,
            chase_cap=Decimal("0.02"),
            atr=None,
            spread=book.ask - book.bid,
            bound="fixed" if fallback else "atr",
            note="ATR has fewer than 14 complete one-minute bars: fixed trail fallback"
            if fallback
            else None,
            source="cold_atr_fallback" if fallback else None,
        )
        # U-h: `fallback` is a field row_distance sets, no longer read from the plan's source.
        return width, protect.Distance(
            book.ask, plan, Decimal(0), book.age, width, width, row, book.bid, fallback=fallback
        )

    monkeypatch.setattr(protect, "row_distance", planned)
    return calls


def carried_start(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    level: str,
    bid: str,
    highest: str,
    distance: str | None,
    precision: int = 4,
    width: Decimal = FALLBACK,
    fallback: bool = True,
) -> tuple[Harness, list[Any]]:
    """The lead's ruling, 2026-11-13 17:43: a carried long, a gap whose highest completed close
    is `highest` (a trade print, not a bid), and a first bid of `bid`."""
    record = restart_record(last_level=level, distance=distance)
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    strategy = restart_harness(path)
    strategy.cached.instrument = lambda iid: equity(iid, precision=precision)  # type: ignore[method-assign]
    strategy.cached.latest = quote(bid, T0 + 180 * SECOND)
    strategy.cached.history = [
        history_bar(highest, T0 + 60 * SECOND),
        history_bar(bid, T0 + 120 * SECOND),
    ]
    calls = planner_distance(monkeypatch, width, fallback=fallback)
    strategy.on_start()
    return strategy, calls


def gap_notes(strategy: Harness) -> list[str]:
    return [note for note in strategy.said if "not read: a rise in the gap's bids" in note]


def test_a_carried_sub_dollar_long_arms_at_its_carried_level_and_distance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ruling's paper case: level 0.7926, distance 0.0285; the gap's highest close 0.8350,
    the fallback 0.02, the first bid 0.7985. Closes rebuilt it to 0.8150, over the bid: a sale."""
    path = tmp_path / "levels.json"
    strategy, calls = carried_start(
        path, monkeypatch, level="0.7926", bid="0.7985", highest="0.8350", distance="0.0285"
    )
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str("0.7926"), strategy.said
    assert slot.distance is not None and slot.distance.distance == Decimal("0.0285")
    assert not calls, "the row's planner re-derived a carried distance"
    assert any(
        "AAA.ALPACA: carried level 0.7926 and distance 0.0285 from run shape-prior" in note
        for note in strategy.said
    ), strategy.said
    strategy.observe("0.7985", T0 + 181 * SECOND)
    strategy.observe("0.7990", T0 + 182 * SECOND)
    (sent,) = strategy.sent
    assert sent.trigger_price == Price.from_str("0.7926")
    assert sent.trigger_price < Price.from_str("0.7985"), "the stop is above the bid: a sale"
    assert sent.trailing_offset == Decimal("0.0285")
    row = book_rows(path)["AAA.ALPACA"]
    assert (row["last_level"], row["distance"]) == ("0.7926", "0.0285")


def test_a_carried_dollar_scale_long_arms_at_its_carried_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy, calls = carried_start(
        tmp_path / "levels.json",
        monkeypatch,
        level="51.69",
        bid="51.90",
        highest="53.00",
        distance="0.85",
        precision=2,
        width=Decimal("0.40"),
    )
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str("51.69"), strategy.said
    assert slot.distance is not None and slot.distance.distance == Decimal("0.85")
    assert not calls


@pytest.mark.parametrize("fallback", [True, False], ids=["fallback", "row"])
def test_a_carried_distance_smaller_than_the_planners_is_kept_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    strategy, calls = carried_start(
        tmp_path / "levels.json",
        monkeypatch,
        level="0.7926",
        bid="0.7985",
        highest="0.8350",
        distance="0.0100",
        fallback=fallback,
    )
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str("0.7926")
    assert slot.distance is not None and slot.distance.distance == Decimal("0.0100")
    assert not calls, "nothing widens a carried distance"
    strategy.observe("0.7985", T0 + 181 * SECOND)
    strategy.observe("0.7990", T0 + 182 * SECOND)
    assert strategy.sent[0].trailing_offset == Decimal("0.0100")


def test_each_start_names_the_gap_it_could_not_read_once_per_carried_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy, _ = carried_start(
        tmp_path / "levels.json",
        monkeypatch,
        level="0.7926",
        bid="0.7985",
        highest="0.8350",
        distance="0.0285",
    )
    assert gap_notes(strategy) == [
        (
            f"AAA.ALPACA: gap {iso(T0 + SECOND)}-{iso(T0 + 180 * SECOND)} not read: a rise in "
            "the gap's bids is not in this level"
        )
    ]
    strategy._on_step(cast(Any, SimpleNamespace()))
    strategy.observe("0.7990", T0 + 181 * SECOND)
    assert len(gap_notes(strategy)) == 1, "named once at the start, not at every step"


def test_an_entry_without_a_distance_keeps_its_level_and_takes_the_rows_distance_for_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling (a), 2026-11-13 17:56: an entry with no distance keeps its level and takes the
    row's distance for later raises only, and says so."""
    path = tmp_path / "levels.json"
    strategy, calls = carried_start(
        path,
        monkeypatch,
        level="0.7926",
        bid="0.7985",
        highest="0.8350",
        distance=None,
        width=Decimal("0.0300"),
        fallback=False,
    )
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str("0.7926"), strategy.said
    assert slot.distance is not None and slot.distance.distance == Decimal("0.0300")
    assert calls == ["AAA"]
    assert any(
        "AAA.ALPACA: carried level 0.7926 and no distance (written before distances were "
        "kept); the row's distance 0.0300 used for later raises only" in note
        for note in strategy.said
    ), strategy.said
    strategy.observe("0.7985", T0 + 181 * SECOND)
    strategy.observe("0.7990", T0 + 182 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("0.7926")
    assert strategy.sent[0].trailing_offset == Decimal("0.0300")


def test_an_entry_without_a_distance_never_takes_the_fixed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling (a): with only the fixed fallback to plan from, the carried level is kept, nothing
    is raised or sent, and that is named at each step until a distance can be planned."""
    path = tmp_path / "levels.json"
    strategy, _calls = carried_start(
        path, monkeypatch, level="0.7926", bid="0.7985", highest="0.8350", distance=None
    )
    assert strategy.slots[AAA].level is None

    def named(notes: list[str]) -> int:
        return sum(
            "the row gives only the fixed fallback" in note
            and "carried level 0.7926 kept, raises nothing until a distance can be planned" in note
            for note in notes
        )

    # U-h plans at the start on each answer (cache, seed, bars), each refusal named.
    assert named(strategy.said) >= 1, strategy.said
    for n in (181, 182, 183):
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy.cached.latest = quote("0.7985", T0 + n * SECOND)
        before = len(strategy.said)
        strategy._on_step(cast(Any, SimpleNamespace()))
        assert named(strategy.said[before:]) == 1, strategy.said[before:]  # Each step, once.
    assert not strategy.sent, "a carried long armed from the fixed fallback"
    assert book_rows(path)["AAA.ALPACA"]["last_level"] == "0.7926"
    assert "fallback" not in " ".join(n for n in strategy.said if "armed distance" in n)


def test_an_entry_without_a_distance_and_no_quote_keeps_its_level_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling (a): no quote to plan from: the level is kept, nothing raised, named each step."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record(distance=None)]}))
    strategy = restart_harness(path)
    strategy.cached.latest = None
    planner_distance(monkeypatch, Decimal("0.03"), fallback=False)
    strategy.on_start()
    for _ in range(2):
        strategy._on_step(cast(Any, SimpleNamespace()))
    named = [
        note
        for note in strategy.said
        # U-h names the seed's own answer, or "no two-sided quote to plan protection".
        if "no two-sided quote" in note
        and "carried level 1.10 kept, raises nothing until a distance can be planned" in note
    ]
    assert len(named) >= 3, strategy.said
    assert not strategy.sent
    assert book_rows(path)["AAA.ALPACA"]["last_level"] == "1.10"


def test_the_carried_exit_is_level_distance_and_limit_offset_together(tmp_path: Path) -> None:
    """Ruling (c): the book carries the limit offset; a carried exit uses all three, and the
    offset is written back beside them."""
    path = tmp_path / "levels.json"
    record = restart_record(last_level="1.00", distance="0.04", limit_offset="0.07")
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert any(
        note.startswith(
            "AAA.ALPACA: carried level 1.00 and distance 0.04 from run shape-prior, "
            "limit offset 0.07"
        )
        for note in strategy.said
    ), strategy.said
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    (sent,) = strategy.sent
    assert (sent.trigger_price, sent.trailing_offset, sent.limit_offset) == (
        Price.from_str("1.00"),
        Decimal("0.04"),
        Decimal("0.07"),
    )
    row = book_rows(path)["AAA.ALPACA"]
    assert (row["last_level"], row["distance"], row["limit_offset"]) == ("1.00", "0.04", "0.07")


def test_an_entry_without_a_limit_offset_takes_todays_rule_by_name(tmp_path: Path) -> None:
    """Ruling (c): an old entry with no offset uses today's quarter-trail rule, named."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record(last_level="1.00")]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert any(
        note.endswith(
            "; no limit offset (written before offsets were kept): today's rule gives 0.05"
        )
        for note in strategy.said
    ), strategy.said
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert strategy.sent[0].limit_offset == Decimal("0.05")
    assert book_rows(path)["AAA.ALPACA"]["limit_offset"] == "0.05"


def test_an_unread_venue_keeps_the_books_entry_as_written(tmp_path: Path) -> None:
    """Review F2: a failed read never lowers the book; the run protects at its self-armed level
    in memory, and both the start and the stop line say so."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record()]}))
    before = book_rows(path)["AAA.ALPACA"]
    strategy = restart_harness(path, trading=VenueOrders(failure=OSError("neutral outage")))
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01")
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("1.01")
    strategy.on_stop()
    assert book_rows(path)["AAA.ALPACA"] == before
    assert any(
        "previous level not carried: venue orders unread" in note
        and "the book keeps it as written while the holding is unknown" in note
        for note in strategy.said
    ), strategy.said
    assert any(
        note.startswith("AAA.ALPACA: book level 1.10")
        and note.endswith("its entry kept as written; this run protected at 1.01 in memory")
        for note in strategy.said
    ), strategy.said


def test_an_unknown_holdings_entry_is_replaced_only_by_a_higher_level(tmp_path: Path) -> None:
    """Review F2: never written lower; once this run's own level is over the entry's, it is."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record(last_level="1.10")]}))
    strategy = restart_harness(path, trading=VenueOrders(failure=OSError("neutral outage")))
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    order = strategy.sent[0]
    emulated = working_sell(
        str(order.client_order_id),
        qty="10",
        filled="0",
        strategy_id=PROTECTOR,
        trigger_price=Price.from_str("1.08"),
        status=OrderStatus.EMULATED,
    )
    strategy.cached.working = [emulated]
    strategy.observe("1.21", T0 + 183 * SECOND)
    assert book_rows(path)["AAA.ALPACA"]["last_level"] == "1.10"
    emulated.trigger_price = Price.from_str("1.16")
    strategy.observe("1.24", T0 + 184 * SECOND)
    row = book_rows(path)["AAA.ALPACA"]
    assert (row["last_level"], row["run"]) == ("1.16", "shape-next")


def test_a_sold_out_holdings_entry_is_replaced_by_the_new_holdings(tmp_path: Path) -> None:
    """Review F2: the run KNOWS the holding is new (sold out), so the lower level is written."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record()]}))
    strategy = restart_harness(path, trading=VenueOrders([venue_fill("sell", "10")]))
    strategy.on_start()
    strategy.on_stop()
    row = book_rows(path)["AAA.ALPACA"]
    assert (row["last_level"], row["run"]) == ("1.01", "shape-next")
    assert any(
        note.startswith("AAA.ALPACA: book level 1.10")
        and "replaced by run shape-next's entry" in note
        for note in strategy.said
    ), strategy.said


@pytest.mark.parametrize(
    "held,fills,carried",
    [
        ("100", [], False),
        ("1", [], False),
        ("15", [venue_fill("buy", "5")], True),
        ("6", [venue_fill("sell", "4")], True),
    ],
    ids=["forward-split", "reverse-split", "rise-by-buys", "fall-by-sells"],
)
def test_a_quantity_the_fills_do_not_explain_refuses_the_carry_by_name(
    tmp_path: Path, held, fills, carried
) -> None:
    """Review F4: a split, forward or reverse, refuses the carry by name and re-derives nothing;
    the holding self-arms, guarded."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record()]}))
    strategy = restart_harness(path, trading=VenueOrders(fills))
    strategy.cached.held = [position(held)]
    strategy.on_start()
    if carried:
        assert strategy.slots[AAA].level == Price.from_str("1.10")
        return
    assert strategy.slots[AAA].level == Price.from_str("1.01"), strategy.said
    assert any(
        f"previous level not carried: quantity {held} is not the entry's 10 less 0 sold plus 0 "
        "bought since"
        in note
        and "a split or a change the fills do not show" in note
        for note in strategy.said
    ), strategy.said
    strategy.observe("1.05", T0 + 181 * SECOND)
    assert not strategy.sent, "the first observation of a self-armed holding is guarded"


def test_a_holding_this_run_saw_end_is_retired_with_no_level(tmp_path: Path) -> None:
    """Review F3: the run saw the holding end: its entry is kept with no level, state frozen
    "holding ended", so the next run cannot carry it."""
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 2, "entries": [restart_record()]}))
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.cached.held = []
    strategy._on_step(cast(Any, SimpleNamespace()))
    row = book_rows(path)["AAA.ALPACA"]
    assert row["last_level"] is None and (row["state"], row["reason"]) == (
        "frozen",
        "holding ended",
    ), row
    strategy.on_stop()
    following = restart_harness(path, trading=VenueOrders())
    following.on_start()
    assert following.slots[AAA].level == Price.from_str("1.01")
    assert following.settings.trading.reads == [], "an entry with no level needs no venue read"


def test_two_accounts_sharing_a_book_keep_a_step_each_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F5: last_step_ns per account label; each read, merge and replace under the lock."""
    import fcntl

    from canon_trading import start_level

    path = tmp_path / "levels.json"
    held: list[bool] = []
    write = start_level.LevelBook.write

    def locked_write(book: Path, entries: Any, *, last_step_ns: Any = None) -> None:
        with (Path(book).parent / f"{Path(book).name}.lock").open("a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                fcntl.flock(other, fcntl.LOCK_UN)
                held.append(False)
        write(book, entries, last_step_ns=last_step_ns)

    monkeypatch.setattr(start_level.LevelBook, "write", staticmethod(locked_write))
    first, second = restart_harness(path), restart_harness(path)
    second.settings.account_label = "other"
    for strategy, at in ((first, 183), (second, 184)):
        strategy.on_start()
        strategy.test_clock.set_time(T0 + at * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
    book = json.loads(path.read_text())
    assert book["last_step_ns"] == {"shape": T0 + 183 * SECOND, "other": T0 + 184 * SECOND}
    assert {(r["account_label"], r["instrument_id"]) for r in book["entries"]} == {
        ("shape", "AAA.ALPACA"),
        ("other", "AAA.ALPACA"),
    }
    assert held and all(held), "a book write ran without the lock"


def test_the_default_book_is_one_file_per_account_label(tmp_path: Path) -> None:
    """Review F5: with no path supplied, each label has its own file."""
    strategy = restart_harness(tmp_path / "unused.json")
    strategy.settings.level_book_path = None
    strategy.on_start()
    assert strategy.level_book_path == tmp_path / "canon" / "watch" / "owner-levels-shape.json"
    with pytest.raises(ValueError, match="name the default level book file"):
        OwnerProtectorConfig(
            row="OWN1", strategy_id=PROTECTOR, account_label="../x"
        ).validate_protection()


def test_an_alpaca_reader_gets_the_adapters_timeout_at_start(tmp_path: Path) -> None:
    """Review F6: an alpaca-py RESTClient reader is bounded at on_start (ADR 0039); no request
    is sent here, as no holding is held."""
    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import timeout_adapter

    reader = TradingClient(api_key="neutral", secret_key="neutral", paper=True, raw_data=True)
    assert timeout_adapter(reader) is None
    strategy = restart_harness(tmp_path / "levels.json", trading=reader)
    strategy.cached.held = []
    strategy.on_start()
    assert timeout_adapter(reader) is not None


def test_an_unread_eligibility_is_retried_once_a_minute_and_named_each_time(
    tmp_path: Path,
) -> None:
    """Review F7: an unread eligibility is not kept for the run; it is read again after a minute,
    never more often, and each refusal is named."""
    reads: list[str] = []
    answers: list[Any] = [OSError("neutral"), {"attributes": ["overnight_tradable"]}]

    def get(route: str, params: Any = None):
        if route.startswith("/assets/"):
            reads.append(route)
            answer = answers[0] if len(reads) == 1 else answers[1]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return []

    strategy = restart_harness(
        tmp_path / "levels.json", session_part="overnight", trading=SimpleNamespace(get=get)
    )
    strategy.on_start()
    assert AAA not in strategy.slots
    for n in (190, 230):
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
    assert reads == ["/assets/AAA"], "read again inside a minute"
    unread = [
        n
        for n in strategy.said
        if "could not be read" in n and "read again at most once a minute" in n
    ]
    assert len(unread) == 3, strategy.said
    strategy.test_clock.set_time(T0 + 241 * SECOND)
    strategy.cached.latest = quote("1.05", T0 + 241 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert reads == ["/assets/AAA", "/assets/AAA"]
    assert AAA in strategy.slots, "an eligible name stayed refused for the run"


@pytest.mark.parametrize(
    "book,venue,why",
    [
        (None, VenueOrders(), "no previous entry"),
        (
            {"schema": 2, "entries": [restart_record(last_level="0.7926", distance="0.0285")]},
            VenueOrders([venue_fill("sell", "10")]),
            "previous level not carried: a new holding: sold out since",
        ),
        ("not a book", VenueOrders(), "no previous entry"),
    ],
    ids=["no-entry", "sold-out", "unreadable-book"],
)
def test_no_carried_level_self_arms_guarded_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, book, venue, why
) -> None:
    path = tmp_path / "levels.json"
    if book is not None:
        path.write_text(book if isinstance(book, str) else json.dumps(book))
    strategy = restart_harness(path, trading=venue)
    strategy.cached.instrument = lambda iid: equity(iid, precision=4)  # type: ignore[method-assign]
    strategy.cached.latest = quote("0.7985", T0 + 180 * SECOND)
    strategy.cached.history = [history_bar("0.8350", T0 + 60 * SECOND)]
    planner_distance(monkeypatch)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("0.7785"), strategy.said
    assert any(
        note.startswith(f"AAA.ALPACA: no carried level ({why}")
        and "self-armed at 0.7785, the bid less the row distance 0.0200, first observation guarded"
        in note
        for note in strategy.said
    ), strategy.said
    assert not gap_notes(strategy)
    strategy.observe("0.7985", T0 + 181 * SECOND)
    assert not strategy.sent, "the first observation of a found long is guarded"
    strategy.observe("0.7990", T0 + 182 * SECOND)
    assert strategy.sent[0].trigger_price == Price.from_str("0.7785")


def book_rows(path: Path) -> dict[str, dict[str, Any]]:
    return {row["instrument_id"]: row for row in json.loads(path.read_text())["entries"]}


def book_state(path: Path, instrument: str = "AAA.ALPACA") -> tuple[Any, Any]:
    row = book_rows(path)[instrument]
    return row.get("state"), row.get("count")


def test_each_state_reaches_the_book_when_it_changes(tmp_path: Path) -> None:
    """Item 4 (the lead, 2026-11-13 17:13): the book is the node report; every state is written."""
    path = tmp_path / "levels.json"
    strategy = restart_harness(path)
    strategy.cached.latest = None
    strategy.on_start()
    assert book_rows(path) == {} or book_state(path)[0] == "waiting"
    # U-h (Q1, the lead 18:17): a level is planned only from a quote received, stream or seed;
    # a cache entry no delivery brought is not one, so the first quote arrives on the stream.
    strategy.observe("1.05", T0 + 180 * SECOND)
    assert book_state(path) == ("counting", 0)
    strategy.observe("1.20", T0 + 181 * SECOND)
    assert book_state(path) == ("counting", 1)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert book_state(path) == ("armed", None)
    assert book_rows(path)["AAA.ALPACA"]["last_level"] == "1.01"


def test_waiting_and_frozen_states_reach_the_book_with_their_reasons(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    strategy = restart_harness(path)
    # U-h (Q4, the lead 18:17): a seed plans at any age inside the open session part (T0 is
    # 11:00 New York), so the unplannable seed is one stamped before the regular part opened.
    strategy.cached.latest = quote("1.05", T0 - 2 * 3600 * SECOND)
    strategy.on_start()
    state = book_rows(path)["AAA.ALPACA"]
    assert state["state"] == "waiting" and "before the open session part" in state["reason"]
    strategy.observe("1.20", T0 + 180 * SECOND)
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    assert book_state(path)[0] == "armed"
    strategy.cached.working.append(
        working_sell("foreign-1", qty="5", filled="0", strategy_id=StrategyId("Other-001"))
    )
    strategy.observe("1.21", T0 + 183 * SECOND)
    state = book_rows(path)["AAA.ALPACA"]
    assert state["state"] == "frozen" and "Other-001" in state["reason"]


def test_guarded_state_reaches_the_book_when_the_carried_level_is_breached(
    tmp_path: Path,
) -> None:
    path = tmp_path / "levels.json"
    path.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert book_state(path)[0] == "guarded"
    strategy.observe("1.05", T0 + 181 * SECOND)
    assert book_state(path)[0] == "guarded"
    strategy.observe("1.04", T0 + 182 * SECOND)
    assert book_state(path)[0] == "armed"


def test_a_reported_entry_with_no_level_restarts_at_the_self_armed_level(tmp_path: Path) -> None:
    """Item 4: a waiting entry has no level; the next run self-arms by name (17:43 ruling)."""
    path = tmp_path / "levels.json"
    record = restart_record(
        last_level=None, state="waiting", reason="no quote to plan protection", distance=None
    )
    path.write_text(json.dumps({"schema": 1, "entries": [record]}))
    strategy = restart_harness(path)
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.01")
    assert any(
        "no carried level (previous entry from run shape-prior has no level (waiting" in note
        for note in strategy.said
    ), strategy.said


def test_last_step_advances_with_steps_while_nothing_else_changes(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    before = book_rows(path)
    steps = []
    for n in (183, 184, 185):
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
        steps.append(json.loads(path.read_text())["last_step_ns"]["shape"])  # Review F5.
    assert steps == [T0 + n * SECOND for n in (183, 184, 185)]
    assert book_rows(path) == before, "a step alone must not rewrite a holding's entry"

    last = steps[-1]
    del strategy  # Killed: discarded with no on_stop; nothing advances the book any more.
    gc.collect()
    assert json.loads(path.read_text())["last_step_ns"] == {"shape": last}


def test_a_failing_book_is_named_once_per_kind_and_protection_continues(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory" / "levels.json"
    path.parent.write_text("cannot contain a book")
    strategy = restart_harness(path)
    strategy.on_start()
    strategy.observe("1.20", T0 + 181 * SECOND)
    strategy.observe("1.20", T0 + 182 * SECOND)
    for n in range(183, 190):
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
    assert len(strategy.sent) == 1
    failures = [s for s in strategy.said if "level book write failed" in s]
    assert len(failures) == 1, failures
    assert "AAA.ALPACA" in failures[0]


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


class EngineProtector(OwnerProtector):
    """OwnerProtector in a backtest engine, its U-h start requests answered from the engine's
    own cache: backtest has no latest endpoint, and everything else is the engine's own."""

    request_bars = Harness.request_bars
    request_quotes = Harness.request_quotes


class SimulatedProtector(EngineProtector):
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
def test_cash_backtest_names_its_external_position_selection_limit(level, monkeypatch) -> None:
    # Backtest's data client has no latest-quote endpoint; the request stand-in answers the
    # cache snapshot, through the same hooks, leaving the real emulator/execution untouched.
    monkeypatch.setattr(SimulatedProtector, "request_bars", Harness.request_bars)
    monkeypatch.setattr(SimulatedProtector, "request_quotes", Harness.request_quotes)
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
        pass

    async def _request_bars(self, request) -> None:
        self._handle_response(
            BarsResponse(
                self.client_id,
                request.bar_types[0],
                [],
                request.request_id,
                self.clock.timestamp_ns(),
                request.start_ns,
                request.end_ns,
                request.params,
            )
        )

    async def _request_quotes(self, request) -> None:
        tick = self.cache.quote(request.instrument_ids[0])
        self._handle_response(
            QuotesResponse(
                self.client_id,
                request.instrument_ids,
                [] if tick is None else [tick],
                request.request_id,
                self.clock.timestamp_ns(),
                params=request.params,
            )
        )

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
        # U-h's start requests answered from this replay's own cache (as uh-build4's blind files do).
        request_bars = Harness.request_bars
        request_quotes = Harness.request_quotes

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


@pytest.mark.parametrize("late", [False, True], ids=["startup-reconciliation", "after-start-check"])
def test_reconciled_external_long_opens_at_reconciliation_time_not_the_venue_open(
    late, tmp_path: Path
) -> None:
    """P11's check (the lead, 2026-11-13): what ts_opened a reconciled EXTERNAL long carries.

    At the pin, a position report with no order or fill history becomes a FILLED reconciliation
    order stamped with the engine clock (live/src/execution/manager.rs:4301-4313 passes ts_now;
    live/src/execution/reconciliation.rs:980-995 sets ts_accepted, ts_last and ts_init to it), its
    inferred fill takes the order report's ts_last as ts_event
    (execution/src/reconciliation/orders.rs:1322,1348), and the position opens at its first fill's
    ts_event (model/src/position.rs:232). The report's own ts_last is used only for the synthetic
    venue order ID. The adapter's position reports carry no open time either
    (canon_alpaca/execution.py:1564-1575). Startup reconciliation runs before strategies start; the
    periodic check (position_check_interval_secs, live/src/node/mod.rs:1440-1445) runs after.

    U-e A and B on that path: a previous run's book entry for the name, with no sell on the venue
    since it was written, carries its level and its own protection start across either path, and
    (the lead's ruling, 2026-11-13 17:43) arms at its carried level and carried distance.
    """
    gc.collect()
    seen: dict[str, int] = {}
    path = tmp_path / "levels.json"
    previous = start_level_entry(
        last_level=Decimal("9.99"),
        protection_start_ns=T0,
        quantity=Decimal(10),
        written_ns=T0 + SECOND,
        run="shape-previous",
        distance=Decimal("0.25"),
    )
    LevelBook.write(path, {("shape", AAA): previous})
    venue = VenueOrders()
    config = OwnerProtectorConfig(row="OWN1", strategy_id=PROTECTOR)
    config.account_label = "shape"
    config.level_book_path = path
    config.run = "shape-node"
    config.trading = venue

    class Saying(OwnerProtector):
        # U-h's start requests answered from this replay's own cache (as uh-build4's blind files do).
        request_bars = Harness.request_bars
        request_quotes = Harness.request_quotes

        said: list[str]

        def _say(self, text: str) -> None:
            self.said.append(text)
            super()._say(text)

    protector = Saying(config)
    protector.said = []

    class Watch(Strategy):
        def on_start(self):
            seen["started"] = self.clock.timestamp_ns()
            self.clock.set_timer("watch", timedelta(milliseconds=100), callback=self.look)

        def look(self, _event):
            # Open or closed: the carried level may already have sold it on the falling tape.
            for held in self.cache.positions(strategy_id=EXTERNAL):
                if "ts_opened" not in seen:
                    seen["ts_opened"] = held.ts_opened
            # The protector writes its armed level at once (P10); its sell may fill on the tape.
            rows = json.loads(path.read_text())["entries"]
            if "ts_opened" in seen and rows[0]["run"] == "shape-node":
                # Review F3: once its sell filled, the entry is retired with no level.
                level = rows[0]["last_level"]
                seen["level"] = 0 if level is None else int(Decimal(level) * 100)
                self.shutdown_system("reconciled long seen and protected")

    class Reporting(SyntheticExecution):
        async def _generate_position_status_reports(self, command):
            now = self.clock.timestamp_ns()
            if late and "started" not in seen:
                return []
            seen.setdefault("reported", now)
            seen.setdefault("venue_open", now - 7 * 24 * 3600 * SECOND)
            return [
                PositionStatusReport(
                    AccountId("ALPACA-001"),
                    AAA,
                    PositionSide.LONG,
                    Quantity.from_int(10),
                    seen["venue_open"],
                    now,
                    avg_px_open=Decimal("10.00"),
                )
            ]

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return SyntheticData(**kwargs, venue=Venue("ALPACA"))

    class ExecutionFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Reporting(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-001"),
                account_type=AccountType.CASH,
                oms_type=OmsType.NETTING,
            )

    node = LiveNode.build(
        "RECONCILED-OPEN-TIME-TEST",
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
            exec_engine=LiveExecutionEngineConfig(
                position_check_interval_secs=0.5, reconciliation_startup_delay_secs=0.0
            ),
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecutionFactory},
    )
    node.add_strategy(
        Watch(StrategyConfig(strategy_id=StrategyId("Watch-002"), order_id_tag="002"))
    )
    node.add_strategy(protector)

    async def run():
        async with asyncio.timeout(6):
            await node.run_async()

    try:
        asyncio.run(run())
    finally:
        node.dispose()
    assert "ts_opened" in seen, "no reconciled EXTERNAL long reached the cache"
    assert seen["ts_opened"] != seen["venue_open"], "the venue's open time was kept"
    assert seen["ts_opened"] >= seen["reported"], "opened before the report was generated"
    if late:
        assert seen["ts_opened"] > seen["started"], "a late long opens after the strategy started"
    else:
        assert seen["ts_opened"] < seen["started"], "startup reconciliation precedes on_start"
    assert seen["level"] in (999, 0), "the previous run's level was not carried"
    assert any(
        line == "AAA.ALPACA: holding level 9.99; one observation cannot elect"
        for line in protector.said
    ), protector.said
    # Review F1/F4: a sell walk and a buy walk, each from 92 days before the entry's write.
    assert venue.reads == [("/orders", orders_query(side, T0 + SECOND)) for side in ("sell", "buy")]
    (row,) = json.loads(path.read_text())["entries"]
    assert row["protection_start_ns"] == T0, "the entry's own protection start was not kept"
    if row["last_level"] is None:
        # The carried level sold it on the falling tape, and this run saw it end (review F3).
        assert (row["state"], row["reason"]) == ("frozen", "holding ended"), row
    else:
        assert (row["last_level"], row["distance"]) == ("9.99", "0.25"), row
    assert any(
        "AAA.ALPACA: carried level 9.99 and distance 0.25 from run shape-previous" in line
        for line in protector.said
    ), protector.said
    assert sum("not read: a rise in the gap's bids" in line for line in protector.said) == 1


def test_each_start_names_the_level_book_state_for_its_account(tmp_path: Path) -> None:
    """3d's cell 0c (the lead, 2026-11-13 19:12): every start says the account's level book is
    enabled, or that it is disabled, by name, so a proof can fail on the disabled line."""
    strategy = restart_harness(tmp_path / "levels.json")
    strategy.on_start()
    assert "level book enabled for account shape" in strategy.said, strategy.said
    assert not any("level book disabled" in note for note in strategy.said)
    bare = Harness()
    bare.on_start()
    assert any(note.startswith("level book disabled") for note in bare.said), bare.said


def test_one_bad_book_clock_does_not_abort_other_holdings(tmp_path):
    """Codex's P1-b on 62941f39 (its drop-in, verbatim but for the book's making): one entry's
    stamp past the nanosecond range. The writer now refuses such a stamp itself, so the bad entry
    is written to disk as a damaged or hand-edited book would hold it."""
    path = tmp_path / "levels.json"
    LevelBook.write(
        path,
        {
            ("shape", AAA): start_level_entry(
                Decimal("1.10"),
                T0,
                Decimal(10),
                T0,
                "bad-clock",
                distance=Decimal("0.04"),
            )
        },
    )
    book = json.loads(path.read_text())
    book["entries"][0]["written_ns"] = 10**30
    path.write_text(json.dumps(book))
    strategy = restart_harness(path)
    strategy.cached.latest = None
    second = position()
    second.instrument_id = BBB
    strategy.cached.held.append(second)
    strategy.on_start()
    assert BBB in strategy.slots
    # The lead's property (2026-11-13 20:31): the bad entry costs its own holding only, named
    # with an ALERT; that holding is found and armed cold like any long with no entry.
    assert AAA in strategy.slots
    assert any(
        line.startswith(f"{AAA}: ALERT: previous level unavailable") for line in strategy.said
    ), strategy.said


def test_review4_a_row_the_book_rejects_costs_its_holding_the_carried_level_with_an_alert(
    tmp_path,
):
    """Landing lane's /code-review high finding 4: the node counts a row LevelBook.read rejects
    (a counting entry's count of 2). The lead's ruling (2026-11-13 20:45): every rejected row
    costs that holding its carried level only, with an ALERT carrying the row's fields, and it
    arms cold; the two parsers' disagreement is F10's follow-up."""
    from canon_trading.protector_node import _book_levels

    path = tmp_path / "levels.json"
    row = restart_record(state="counting", count=2)
    path.write_text(json.dumps({"schema": 2, "entries": [row]}))
    assert _book_levels(path, "shape") == {AAA: Price.from_str("1.10")}  # the node hands 1 level
    strategy = restart_harness(path)
    strategy.on_start()
    alerts = [line for line in strategy.said if line.startswith(f"{AAA}: ALERT: previous level")]
    assert len(alerts) == 1, strategy.said
    assert '"count": 2' in alerts[0] and '"last_level": "1.10"' in alerts[0], alerts[0]
    assert "armed as a holding with no carried level" in alerts[0]
    assert strategy.slots[AAA].level is not None, strategy.said


def test_both_rows_of_a_duplicate_holding_are_named_with_an_alert(tmp_path):
    """F4's property: no rejected row is dropped silently, the earlier of a duplicate pair too."""
    path = tmp_path / "levels.json"
    rows = [restart_record(), restart_record(last_level="1.20")]
    path.write_text(json.dumps({"schema": 2, "entries": rows}))
    notes: list[str] = []
    assert LevelBook.read(path, report=notes.append) == {}
    assert [note.split(":")[0] for note in notes] == [str(AAA), str(AAA)], notes
    assert all("ALERT: previous level unavailable" in note for note in notes)
    assert '"last_level": "1.10"' in notes[0] and '"last_level": "1.20"' in notes[1], notes


def test_an_alert_line_is_logged_as_an_error_and_any_other_at_info():
    """The lead's addendum on land-composed's F6 (2026-11-13 20:46): an ALERT must reach a person."""
    logged: list[tuple[str, str]] = []
    log = SimpleNamespace(
        info=lambda text: logged.append(("info", text)),
        error=lambda text: logged.append(("error", text)),
    )
    speaker = cast(Any, SimpleNamespace(log=log))
    OwnerProtector._say(speaker, f"{AAA}: ALERT: our protective sell was refused by the venue")
    OwnerProtector._say(speaker, f"{AAA}: armed distance 0.04")
    assert [level for level, _ in logged] == ["error", "info"]
