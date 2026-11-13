"""The mover strategy offline: `canon_trading.mover.Mover` through a real `BacktestEngine` (ADR 0088).

No test here opens a network connection or reads a credential. Bars and quotes are generated in this
file: each minute a bar, and a quote a nanosecond before the bar closes, a cent either side of its close,
so a decision taken on a bar finds the quote of that close in the cache. The simulated venue fills on
quotes only (`bar_execution=False`): with bars and quotes together it also fills a resting limit from the
bar's earlier path (measured at the pin, ADR 0088). The ranking comes from a test component that publishes
the `movers-rank` signal each minute, as the ranking actor does (ADR 0107): nothing of a vendor's list
reaches the strategy.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
from canon_alpaca.execution import sub_penny_denial
from canon_alpaca.execution import venue_limit_price
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType
from nautilus_trader.model import Venue
from nautilus_trader.risk import FixedRiskSizer
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy

from canon_trading import early_entry_study as study
from canon_trading import mover
from canon_trading import mover_window
from canon_trading import movers_rank
from canon_trading.mover import EXTENDED_HOURS
from canon_trading.mover import PAPER_LOSS_SHARE
from canon_trading.mover import RANK_SIGNAL
from canon_trading.mover import Mover
from canon_trading.mover import MoverConfig
from canon_trading.mover import format_rank
from canon_trading.mover_window import OUTER_BOUND_USD
from canon_trading.mover_window import entry_diagnostics
from canon_trading.mover_window import max_shares
from canon_trading.mover_window import mover_parts
from canon_trading.mover_window import part_slots
from canon_trading.mover_window import window_bars
from canon_trading.movers_rank_actor import MoversRankActorConfig
from canon_trading.movers_rank_actor import ReferenceRead
from canon_trading.movers_rank_signal import SIGNAL_NAME
from canon_trading.movers_rank_signal import RankEntry
from canon_trading.movers_rank_signal import decode_ranking
from canon_trading.movers_rank_signal import encode_ranking
from canon_trading.price_step import entry_price
from canon_trading.price_step import exit_price
from canon_trading.price_step import tick

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent

USD = Currency.from_str("USD")
VENUE = Venue("ALPACA")
AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")
T0 = 1_762_790_400_000_000_000  # 2025-11-10T16:00:00Z
MINUTE = 60_000_000_000
SECOND = 1_000_000_000
MOVER_ID = StrategyId("Mover-001")

RISING = [10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70]
"""Closes of a first leg: every bar closes above the one before."""
FALL = [10.46, 10.00, 9.90, 9.80]
"""Then a fall through the trigger (10.48 after a bid of 10.69) that stops above the released limit
(10.42), and lower still."""


def engine(
    *, order_cap: Decimal | None = None, liquid: bool = False, cash: Decimal = Decimal(100_000)
) -> BacktestEngine:
    """The engine; with `order_cap`, the risk engine's per-order notional cap on AAA, as a node sets it
    (`paper_node.risk_limits`); with `liquid`, a fill takes no more than the quote's size."""
    risk = (
        RiskEngineConfig()
        if order_cap is None
        else RiskEngineConfig(max_notional_per_order={str(AAA): order_cap})
    )
    eng = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("TESTER-001"), risk_engine=risk))
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(float(cash), USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
        liquidity_consumption=liquid,
    )
    for instrument_id in (AAA, BBB):
        eng.add_instrument(
            Equity(
                instrument_id=instrument_id,
                raw_symbol=Symbol(instrument_id.symbol.value),
                currency=USD,
                price_precision=4,
                price_increment=Price.from_str("0.0001"),
                ts_event=0,
                ts_init=0,
                info={"name": "Example Inc. Common Stock", "status": "active", "tradable": True},
            )
        )
    return eng


def bar_type(instrument_id: InstrumentId) -> BarType:
    return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")


def tape(
    instrument_id: InstrumentId, closes: list[float], *, volume: int = 100_000, spread: float = 0.01
) -> list:
    """One bar a minute from `T0` with these closes, and the quote of each close a nanosecond before it."""
    data: list = []
    for i, close in enumerate(closes):
        opened = closes[i - 1] if i else close
        ts_close = T0 + (i + 1) * MINUTE
        data.append(
            Bar(
                bar_type(instrument_id),
                Price(opened, 4),
                Price(max(opened, close), 4),
                Price(min(opened, close), 4),
                Price(close, 4),
                Quantity.from_int(volume),
                T0 + i * MINUTE,
                ts_close,
            )
        )
        data.append(
            QuoteTick(
                instrument_id,
                Price(close - spread, 4),
                Price(close + spread, 4),
                Quantity.from_int(10_000),
                Quantity.from_int(10_000),
                ts_close - 1,
                ts_close - 1,
            )
        )
    return data


class RankerConfig(StrategyConfig):
    def __init__(self, **_kwargs: object) -> None:
        super().__init__()


class Ranker(Strategy):
    """Publishes `value` as the ranking at its start and every minute until `until_minute`, as the
    ranking actor will; `values` replaces it from a given minute on. Build it with `ranker`."""

    value = ""
    until_minute = 10_000
    values: dict[int, str] | None = None
    minute = 0

    def on_start(self) -> None:
        self.publish_signal(RANK_SIGNAL, self.value, ts_event=self.clock.timestamp_ns())
        self.clock.set_timer("ranker", timedelta(minutes=1), callback=self._publish)

    def _publish(self, event: TimeEvent) -> None:
        self.minute += 1
        self.value = (self.values or {}).get(self.minute, self.value)
        if self.minute <= self.until_minute:
            self.publish_signal(RANK_SIGNAL, self.value, ts_event=event.ts_event)


def ranker(
    value: str, *, until_minute: int = 10_000, values: dict[int, str] | None = None
) -> Ranker:
    made = Ranker(RankerConfig(strategy_id=StrategyId("Ranker-001"), order_id_tag="RNK"))
    made.value, made.until_minute, made.values = value, until_minute, values
    return made


class Recording(Mover):
    """`Mover` that keeps each submit's params and each order it sent, in the order sent."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.params: list[dict | None] = []
        self.triggers: list[Decimal] = []
        self.said: list[str] = []

    def _say(self, text: str) -> None:
        self.said.append(text)
        super()._say(text)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.params.append(params)
        super().submit_order(order, position_id, client_id, params)

    def _on_step(self, event) -> None:
        super()._on_step(event)
        for slot in self.slots.values():
            order = self._order(slot.protect_id) if slot.protect_id else None
            if order is not None and order.is_emulated and order.trigger_price is not None:
                self.triggers.append(order.trigger_price.as_decimal())


def strategy(**changes: object) -> Recording:
    settings: dict = {
        "max_notional": Decimal(50_000),
        "max_move": Decimal(5),
        "entry_arm": "bridge",  # order-path cases; registered-arm cases select their row explicitly
        "strategy_id": MOVER_ID,
        "order_id_tag": "001",
    }
    settings.update(changes)
    if settings.get("size_from_budget") and not {"planned_loss", "loss_share"} & set(changes):
        # the paper row (the lead's ruling of 2026-11-12): the loss at a 2% stop of the slot's figure
        settings["loss_share"] = PAPER_LOSS_SHARE
    return Recording(MoverConfig(**settings))


def rank(*entries: tuple[InstrumentId, str]) -> str:
    return encode_ranking([RankEntry(i, float(m)) for i, m in entries])


def run(eng: BacktestEngine, s: Mover, ranking: Ranker | None, *tapes: list) -> list:
    for data in tapes:
        eng.add_data(data)
    eng.add_strategy(s)  # first, so it has subscribed before the ranking's first publish
    if ranking is not None:
        eng.add_strategy(ranking)
    eng.run()
    return sorted(
        eng.cache.orders(strategy_id=MOVER_ID),
        key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
    )


def sides(orders: list) -> list[str]:
    return [f"{o.side.name} {o.order_type.name} {o.status.name}" for o in orders]


class DeferredInstruments(Recording):
    """The real risk engine has the definitions; this strategy sees a delayed data-client reply.

    The adapter's own real-node test covers inserting an initially absent definition. This view lets
    a backtest exercise Mover's wait and real orders without inventing a backtest instrument server.
    """

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.settings.load_instruments = True
        self.available = {AAA}
        self.load_delay: int | None = SECOND
        self.load_failure: str | None = None
        self.asked: list[tuple[InstrumentId, int]] = []
        self.answers: list[tuple[int, InstrumentId, str]] = []
        self.subscriptions: list[tuple[str, InstrumentId, dict | None]] = []
        self.steps: list[int] = []

    @property
    def cache(self) -> Any:
        native = super().cache
        available = self.available

        class View:
            def instrument(self, instrument_id):
                return native.instrument(instrument_id) if instrument_id in available else None

            def __getattr__(self, name):
                return getattr(native, name)

        return View()

    def request_instrument(
        self, instrument_id, start=None, end=None, client_id=None, params=None, callback=None
    ):
        now = self.clock.timestamp_ns()
        self.asked.append((instrument_id, now))
        request_id = f"asset-{len(self.asked)}"
        if self.load_delay is not None:
            self.answers.append((now + self.load_delay, instrument_id, request_id))
        return request_id

    def subscribe_quotes(self, instrument_id, client_id=None, params=None):
        self.subscriptions.append(("quotes", instrument_id, params))
        super().subscribe_quotes(instrument_id, client_id=client_id, params=params)

    def subscribe_trades(self, instrument_id, client_id=None, params=None):
        self.subscriptions.append(("trades", instrument_id, params))
        super().subscribe_trades(instrument_id, client_id=client_id, params=params)

    def subscribe_bars(self, bar_type, client_id=None, params=None):
        self.subscriptions.append(("bars", bar_type.instrument_id, params))
        super().subscribe_bars(bar_type, client_id=client_id, params=params)

    def _on_step(self, event):
        self.steps.append(event.ts_event)
        for due, instrument, request_id in list(self.answers):
            if due <= event.ts_event:
                self.answers.remove((due, instrument, request_id))
                if self.load_failure is None:
                    self.available.add(instrument)
                self.on_data_response(
                    SimpleNamespace(
                        correlation_id=request_id,
                        completed=True,
                        failure_reason=self.load_failure,
                    )
                )
        super()._on_step(event)


def deferred_strategy(**changes: object) -> DeferredInstruments:
    original = strategy(**changes)
    return DeferredInstruments(original.settings)


def test_an_on_demand_buy_and_sale_after_a_rise_pass_without_a_roster_engine_cap():
    # The real risk engine caps only AAA at $1; BBB must pass with its absent map entry, both ways.
    eng = engine(order_cap=Decimal(1))
    s = deferred_strategy(
        max_notional=Decimal(1000), size_from_budget=True, share_caps={AAA: 1}, watch=1
    )
    orders = run(eng, s, ranker(rank((BBB, "0.4"))), tape(BBB, RISING + FALL))
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert s.asked and [instrument for instrument, _ in s.asked] == [BBB]
    buy, sale = orders
    assert buy.side == OrderSide.BUY and sale.side == OrderSide.SELL
    assert buy.status.name == sale.status.name == "FILLED"
    assert buy.quantity.as_decimal() * buy.price.as_decimal() <= 1000
    assert sale.quantity.as_decimal() * sale.price.as_decimal() > 1000
    assert flat and not s.refused
    assert s.record()["instrument_loads"][str(BBB)]["engine_cap"] is None
    sizing = s.entries_sent[0]["sizing"]  # the lead's ruling of 2026-11-12 on non-roster entries
    assert sizing["roster"] is False
    assert sizing["held_by"] in ("entry_notional", "free_balance", "flow_share")
    assert Decimal(sizing["notional"]) == buy.quantity.as_decimal() * buy.price.as_decimal()


def test_an_on_demand_name_waits_without_blocking_and_subscribes_on_its_feed():
    eng = engine()
    s = deferred_strategy(watch=1, feed="sip", protect=False)
    s.load_delay = 3 * SECOND
    run(eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.2"))), tape(BBB, RISING))
    eng.dispose()
    assert [instrument for instrument, _ in s.asked] == [BBB]
    assert len(s.steps) >= 4 and all(b - a == SECOND for a, b in pairwise(s.steps[:4]))
    assert s.subscriptions[:3] == [
        ("quotes", BBB, {"feed": "sip"}),
        ("trades", BBB, {"feed": "sip"}),
        ("bars", BBB, None),
    ]
    assert AAA not in s.opened_slots
    assert s.slots[BBB].subscribed_ns >= s.asked[0][1] + 3 * SECOND


@pytest.mark.parametrize("answer", ["unknown asset", "timeout"])
def test_an_on_demand_instrument_failure_is_named_once_and_never_qualifies(answer):
    eng = engine()
    s = deferred_strategy(watch=1)
    if answer == "timeout":
        s.load_delay = None
    else:
        s.load_failure = answer
    orders = run(eng, s, ranker(rank((BBB, "0.4"))), tape(BBB, RISING))
    eng.dispose()
    assert len(s.asked) == 1 and not orders and BBB not in s.opened_slots
    assert any(str(BBB) in why and answer in why for why in s.record()["dropped"])


def test_on_demand_registered_names_with_no_history_are_passed_over_by_name():
    eng = engine()
    s = deferred_strategy(entry_arm="registered", watch=1)
    orders = run(eng, s, ranker(rank((BBB, "0.4"))), tape(BBB, RISING))
    eng.dispose()
    assert s.asked and not orders and BBB in s.opened_slots
    assert any(str(BBB) in why and "context" in why for why in s.record()["dropped"])
    assert BBB not in s.entry_contexts


def test_on_demand_requests_are_bounded_in_a_rolling_minute_and_counted():
    eng = engine()
    s = deferred_strategy(watch=mover.RANK_MOST)
    s.load_failure = "unknown asset"
    generations = [
        rank(*[(InstrumentId.from_str(f"N{generation:02}{i:02}.ALPACA"), "0.4") for i in range(20)])
        for generation in range(3)
    ]
    run(
        eng,
        s,
        ranker(generations[0], values={1: generations[1], 2: generations[2]}),
        tape(AAA, [10.0] * 4),
    )
    eng.dispose()
    assert len(s.asked) > mover.RANK_MOST
    times = [when for _, when in s.asked]
    assert max(sum(start <= when <= start + MINUTE for when in times) for start in times) <= 20
    assert len({instrument for instrument, _ in s.asked}) == len(s.asked)
    assert s.record()["instrument_requests"] == len(s.asked)


def test_on_demand_requests_reserve_retries_and_include_startup_request_traffic(monkeypatch):
    monkeypatch.setattr(mover, "requests_sent", lambda: 138)
    eng = engine()
    s = deferred_strategy(watch=1)
    run(eng, s, ranker(rank((BBB, "0.4"))), tape(AAA, [10.0, 10.0]))
    eng.dispose()
    assert not s.asked
    assert any(str(BBB) in why and "request" in why for why in s.record()["dropped"])


# --- the contract's small parts ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["0.1234567890123456789", "123456789.123456789", "1E+400", "1E-400"],
)
def test_format_rank_refuses_a_decimal_that_cannot_round_trip_as_a_float(text: str) -> None:
    with pytest.raises(ValueError, match=r"AAA[.]ALPACA.*round-trip.*float") as refusal:
        format_rank([(AAA, Decimal(text))])
    assert text in str(refusal.value)


@pytest.mark.parametrize(
    "text",
    ["0", "-0.0", "0.2500", "1E-7", "9.5094", "-1.3639", "1.7976931348623157E+308", "5E-324"],
)
def test_format_rank_preserves_a_decimal_the_shared_codec_can_carry(text: str) -> None:
    move = Decimal(text)
    entries = decode_ranking(format_rank([(AAA, move)]))
    assert len(entries) == 1 and entries[0].instrument_id == AAA
    carried = Decimal(str(entries[0].move))
    assert carried == move and carried.is_signed() == move.is_signed()


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([(AAA, Decimal("0.1")), (AAA, Decimal("0.2"))], r"AAA[.]ALPACA twice"),
        (
            [
                (InstrumentId.from_str(f"NAME{i}.ALPACA"), Decimal("0.1"))
                for i in range(mover.RANK_MOST + 1)
            ],
            "at most 20",
        ),
        ([(InstrumentId.from_str("AAA.XNAS"), Decimal("0.1"))], r"AAA[.]XNAS.*not ALPACA"),
        *[
            ([(AAA, Decimal(text))], r"AAA[.]ALPACA.*not finite")
            for text in ("NaN", "sNaN", "Infinity", "-Infinity")
        ],
    ],
)
def test_format_rank_names_its_refusal_before_a_ranking_can_be_published(
    entries: list[tuple[InstrumentId, Decimal]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        format_rank(entries)


def test_the_strategy_reads_and_records_what_encode_ranking_writes() -> None:
    entries = (RankEntry(AAA, 1e-7), RankEntry(BBB, 0.25))
    value = encode_ranking(entries)
    eng = engine()
    s = strategy()
    run(eng, s, ranker(value), tape(AAA, RISING), tape(BBB, [20.0] * len(RISING)))
    record = s.record()
    eng.dispose()
    assert RANK_SIGNAL == SIGNAL_NAME
    assert s.rank == [(AAA, Decimal("1e-7")), (BBB, Decimal("0.25"))]
    assert s.rank_ns >= T0 and s.rank_faults == 0
    assert record["rank"] == value
    assert decode_ranking(record["rank"]) == entries
    assert any(f"Ranking: {value};" in line for line in s.said)


@pytest.mark.parametrize("bad", ["AAA.NASDAQ=0.25", "AAA.ALPACA=0.250", "AAA.ALPACA=1E-7"])
def test_the_strategy_refuses_what_the_shared_ranking_contract_refuses(bad: str) -> None:
    s = strategy()
    s.on_signal(
        cast(
            Any,
            SimpleNamespace(
                name=SIGNAL_NAME,
                value=encode_ranking([RankEntry(AAA, 0.25)]),
                ts_event=5,
                ts_init=5,
            ),
        )
    )
    s.on_signal(cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=bad, ts_event=6, ts_init=6)))
    assert s.rank == []
    assert s.rank_faults == 1 and s.rank_ns == 5


@pytest.mark.parametrize("bad", [0.25, 1, None, b"AAA.ALPACA=0.25"])
def test_a_ranking_signal_that_is_not_text_is_a_fault_not_an_error(bad: object) -> None:
    # Upstream's `publish_signal` takes a value of any type (`value: typing.Any`); the shared
    # contract is text, so anything else empties the ranking and counts a fault, as a malformed
    # text does, instead of raising out of the strategy's handler.
    s = strategy()
    s.on_signal(
        cast(
            Any,
            SimpleNamespace(
                name=SIGNAL_NAME,
                value=encode_ranking([RankEntry(AAA, 0.25)]),
                ts_event=5,
                ts_init=5,
            ),
        )
    )
    s.on_signal(cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=bad, ts_event=6, ts_init=6)))
    assert s.rank == []
    assert s.rank_faults == 1 and s.rank_ns == 5


def test_a_malformed_ranking_counts_as_stale_not_as_a_fresh_empty_one() -> None:
    """Review of fire-m-c, finding 7: a malformed ranking emptied the ranking but kept the last good
    one's time, so for up to `rank_max_age_secs` the strategy took it as a fresh ranking naming nobody
    and unwatched every idle slot, which a stale ranking never does. Until one parses it is stale."""
    s = strategy()

    def heard(value: object, at: int) -> None:
        s.on_signal(
            cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=value, ts_event=at, ts_init=at))
        )

    heard(encode_ranking([RankEntry(AAA, 0.25)]), 5)
    assert s._fresh(6)
    heard("AAA.NASDAQ=0.25", 6)
    assert not s._fresh(7)
    assert s._refusal(cast(Any, None), None, {}, 0, 7) == "the last ranking was malformed"
    heard(encode_ranking([RankEntry(AAA, 0.25)]), 8)
    assert s._fresh(9)


def test_the_ranking_signal_round_trips_and_a_malformed_one_is_refused() -> None:
    value = rank((AAA, "0.25"), (BBB, "0.1"))
    assert value == "AAA.ALPACA=0.25;BBB.ALPACA=0.1"
    s = strategy()

    def hear(value: str) -> None:
        s.on_signal(
            cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=value, ts_event=5, ts_init=5))
        )

    hear(value)
    assert s.rank == [(AAA, Decimal("0.25")), (BBB, Decimal("0.1"))]
    hear("")
    assert s.rank == [] and s.rank_faults == 0
    for bad in ("AAA.ALPACA", "AAA.ALPACA=x", "=0.1", "AAA.ALPACA=0.1;AAA.ALPACA=0.2", "AAA=0.1"):
        hear(bad)
        assert s.rank == []
    assert s.rank_faults == 5


def test_a_price_has_two_decimals_at_or_above_a_dollar_and_four_below() -> None:
    """Alpaca's "Sub-penny increments for limit orders": 2 decimals at or above $1.00, 4 below."""
    assert tick(Decimal("1.00")) == Decimal("0.01")
    assert tick(Decimal(750)) == Decimal("0.01")
    assert tick(Decimal("0.9999")) == Decimal("0.0001")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_notional": Decimal(0)}, "max_notional"),
        ({"max_move": Decimal(0)}, "max_move"),
        ({"slots": 0}, "slots"),
        ({"watch": 0, "slots": 1}, "watch"),
        ({"risk": Decimal(0)}, "risk"),
        ({"risk": Decimal(1)}, "risk"),
        ({"run_bars": 0}, "run_bars"),
        ({"trail": Decimal(0)}, "trail"),
        ({"chase": "market"}, "chase"),
        ({"trigger": TriggerType.MARK_PRICE}, "trigger"),
        ({"wind_down_at": datetime(2025, 11, 10, 16, 20)}, "aware"),
        ({"bar_spec": "1-MINUTE"}, "bar_spec"),
        ({"entries": 0}, "entries"),
        ({"max_shares": 0}, "max_shares"),
    ],
)
def test_the_config_refuses_settings_it_cannot_honour(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        strategy(**changes)


@pytest.mark.parametrize("misspelled", ["max_notinal", "slot", "wind_down"])
def test_the_config_refuses_a_keyword_it_does_not_have(misspelled: str) -> None:
    with pytest.raises(
        TypeError, match=f"MoverConfig got an unexpected keyword argument '{misspelled}'"
    ):
        strategy(**{misspelled: 1})


def test_the_config_has_no_default_for_the_notional_cap_or_the_no_chase_cap() -> None:
    """No cap, no trade (ADR 0083, gap 2): the run states both; neither is assumed."""
    only_move: dict = {"max_move": Decimal(1)}
    only_notional: dict = {"max_notional": Decimal(1000)}
    with pytest.raises(TypeError, match="max_notional"):
        MoverConfig(**only_move)
    with pytest.raises(TypeError, match="max_move"):
        MoverConfig(**only_notional)


def test_a_setting_of_another_type_is_refused() -> None:
    with pytest.raises(TypeError, match="MoverConfig's extended_hours is str"):
        strategy(extended_hours="False")
    with pytest.raises(TypeError, match="MoverConfig's slots is bool"):
        strategy(slots=True)


# --- entry -------------------------------------------------------------------------------------------


def entry_day(closes: list[float], normal_dollars: float = 1000.0) -> study.Day:
    """The same generated minute bars as `tape`, with an explicit pre-watch baseline."""
    opened = datetime.fromtimestamp(T0 / SECOND, study.NEW_YORK)
    start = opened.hour * 60 + opened.minute
    bars = tuple(study.Minute(start + i, c, c, c, c, 100_000.0) for i, c in enumerate(closes))
    earlier = tuple(
        (study.Minute(study.PART_START, 10.0, 10.0, 10.0, 10.0, normal_dollars / 10.0),)
        for _ in range(20)
    )
    return study.Day("AAA", opened.date(), bars, earlier, 10.0)


def entry_tape(closes: list[float]) -> list:
    """A quote at the window's start lets the watch timer subscribe before the first bar closes."""
    return [
        QuoteTick(
            AAA,
            Price(9.99, 4),
            Price(10.01, 4),
            Quantity.from_int(10_000),
            Quantity.from_int(10_000),
            T0,
            T0,
        ),
        *tape(AAA, closes),
    ]


@pytest.mark.parametrize(
    ("closes", "normal_dollars"),
    [([10.0, 10.01, 10.02, 10.03, 10.04], 1000.0), ([10.4, 10.5, 10.6, 10.7, 10.8], 10_000_000.0)],
)
def test_the_registered_entry_needs_both_listing_thresholds(
    closes: list[float], normal_dollars: float
) -> None:
    """Task M gap 5: the bridge buys even though the study never lists this name."""
    day = entry_day(closes, normal_dollars)
    assert study.replay(day, study.REGISTERED).entry is None
    s = strategy()
    # Assign the requested state after construction so the original bridge actually runs RED.
    s.settings.entry_arm = "registered"
    s.entry_contexts = {AAA: day}
    orders = run(engine(), s, ranker(rank((AAA, "0.01"))), entry_tape(closes))
    assert [o for o in orders if o.side == OrderSide.BUY] == []


def test_a_registered_arm_without_context_refuses_entry_by_name() -> None:
    s = strategy()
    s.settings.entry_arm = "registered"
    orders = run(engine(), s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert [o for o in orders if o.side == OrderSide.BUY] == []
    assert any("entry context" in why for why in s.dropped)


def test_the_default_entry_arm_never_trades_without_its_listing_context() -> None:
    eng = engine()
    s = Recording(
        MoverConfig(
            max_notional=Decimal(500),
            max_move=Decimal(100),
            strategy_id=MOVER_ID,
            order_id_tag="001",
        )
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    eng.dispose()
    assert not any(o.side == OrderSide.BUY for o in orders)
    assert any("entry context" in why for why in s.dropped)


def test_the_default_entry_arm_requires_listing_even_when_a_run_rises() -> None:
    closes = [10.0, 10.01, 10.02, 10.03, 10.04]
    eng = engine()
    s = Recording(
        MoverConfig(
            max_notional=Decimal(500),
            max_move=Decimal(100),
            strategy_id=MOVER_ID,
            order_id_tag="001",
        )
    )
    s.entry_contexts[AAA] = entry_day(closes, 1000)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), entry_tape(closes))
    eng.dispose()
    assert not any(o.side == OrderSide.BUY for o in orders)
    assert any("not listed" in why for why in s.dropped)


def test_a_registered_entry_keeps_the_qualifying_bar_receipt_for_its_diagnostic() -> None:
    s, buys = registered_orders(entry_day(RISING, 1000), "registered", RISING)
    assert buys
    entry = s.entries_sent[0]
    qualifying = next(
        b
        for b in tape(AAA, RISING)
        if isinstance(b, Bar) and b.close.as_decimal() == Decimal(entry["run_close"])
    )
    assert entry["qualified_ns"] == qualifying.ts_init
    assert 0 <= entry["submitted_ns"] - entry["qualified_ns"] <= SECOND


def registered_orders(day: study.Day, arm: str, closes: list[float]) -> tuple[Recording, list]:
    s = strategy(entry_arm=arm, protect=False, entries=1)
    s.entry_contexts[AAA] = day
    orders = run(engine(), s, ranker(rank((AAA, "0.25"))), entry_tape([*closes, closes[-1]]))
    return s, [o for o in orders if o.side == OrderSide.BUY]


@pytest.mark.parametrize(
    ("arm", "minute"),
    [
        ("registered", 6),
        ("move-2", 5),
        ("move-5", 7),
        ("ratio-2", 6),
        ("ratio-5", 6),
        ("baseline-10", 6),
        ("baseline-20", 6),
        ("run-1", 4),
        ("run-2", 5),
        ("window-15", 6),
        ("window-60", 6),
        ("quiet-10", 6),
        ("quiet-25", 6),
        ("window-15-pullback", 6),
        ("quiet-10-pullback", 6),
    ],
)
def test_each_registered_arm_agrees_with_the_study_on_the_same_bars(arm: str, minute: int) -> None:
    """Literal minutes catch listing-before-run and run-length drift, not just equal implementations."""
    closes = [10.1, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9]
    day = entry_day(closes)
    replay = study.replay(day, study.ARMS[arm])
    assert replay.entry_minute == day.bars[0].start + minute
    s, buys = registered_orders(day, arm, closes)
    assert len(buys) == 1
    assert (buys[0].ts_init - T0) // MINUTE == minute
    assert buys[0].price.as_decimal() == Decimal(str(closes[minute - 1])) + Decimal("0.01")
    assert s.record()["rows"]["entry_arm"] == arm


@pytest.mark.parametrize("internal", [False, True])
def test_the_registered_clock_uses_the_minute_open_not_bar_receipt_time(internal: bool) -> None:
    """External streamed bars can arrive late; internal time bars stamp their close at this pin."""
    closes = [10.1, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9]
    day = entry_day(closes)
    s = strategy(
        entry_arm="registered",
        protect=False,
        entries=1,
        bar_spec=f"1-MINUTE-LAST-{'INTERNAL' if internal else 'EXTERNAL'}",
    )
    s.entry_contexts[AAA] = replace(day, bars=())
    data = [entry_tape(closes)[0], *trade_tape(AAA, closes)] if internal else entry_tape(closes)
    if not internal:
        data = [
            Bar(
                item.bar_type,
                item.open,
                item.high,
                item.low,
                item.close,
                item.volume,
                item.ts_event,
                item.ts_init + 123_000_000,
            )
            if isinstance(item, Bar)
            else item
            for item in data
        ]
    orders = run(engine(), s, ranker(rank((AAA, "0.25"))), data)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and (buys[0].ts_init - T0) // MINUTE == 6
    assert s.entry_contexts[AAA].bars[0].start == day.bars[0].start + int(internal)


@pytest.mark.parametrize(("arm", "minute"), [("ratio-2", 6), ("registered", 8), ("ratio-5", 11)])
def test_an_arms_ratio_uses_cumulative_dollars_since_four_am(arm: str, minute: int) -> None:
    closes = [10.1, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 11.2]
    day = entry_day(closes, normal_dollars=1_500_000.0)
    assert study.replay(day, study.ARMS[arm]).entry_minute == day.bars[0].start + minute
    _, buys = registered_orders(day, arm, closes)
    assert len(buys) == 1 and (buys[0].ts_init - T0) // MINUTE == minute


@pytest.mark.parametrize("arm", ["baseline-10", "baseline-20"])
def test_baseline_arms_take_their_registered_number_of_earlier_sessions(arm: str) -> None:
    closes = [10.4, 10.5, 10.6, 10.7, 10.8, 10.9]
    day = entry_day(closes)
    busy = entry_day(closes, normal_dollars=20_000_000.0).earlier
    day = replace(day, earlier=(*day.earlier[:5], *busy[5:]))
    assert study.replay(day, study.REGISTERED).entry_minute == day.bars[0].start + 4
    assert study.replay(day, study.ARMS[arm]).entry is None
    _, buys = registered_orders(day, arm, closes)
    assert buys == []


@pytest.mark.parametrize(
    ("closes", "arm", "minute"),
    [
        ([10.4] * 17 + [10.5, 10.6, 10.7, 10.8], "window-15", None),
        ([10.4] * 17 + [10.5, 10.6, 10.7, 10.8], "window-60", 20),
        ([10.4] * 17 + [10.5, 10.6, 10.7, 10.8], "window-15-pullback", None),
        ([10.4] * 16 + [11.5, 10.4, 10.5, 10.6, 10.7, 10.8], "window-15", None),
        ([10.4] * 16 + [11.5, 10.4, 10.5, 10.6, 10.7, 10.8], "window-15-pullback", 21),
        ([10.0] * 30 + [10.4, 10.8, 11.2, 11.6, 11.7], "quiet-10", None),
        ([10.0] * 30 + [10.4, 10.8, 11.2, 11.6, 11.7], "quiet-25", 34),
        ([10.0] * 30 + [10.4, 10.8, 11.2, 11.6, 11.7], "quiet-10-pullback", None),
        ([10.0] * 30 + [13.0, 11.5, 11.6, 11.7, 11.8, 11.9], "quiet-10", None),
        ([10.0] * 30 + [13.0, 11.5, 11.6, 11.7, 11.8, 11.9], "quiet-10-pullback", 35),
    ],
)
def test_registered_extension_arms_agree_with_the_study(
    closes: list[float], arm: str, minute: int | None
) -> None:
    day = entry_day(closes)
    assert study.replay(day, study.ARMS[arm]).entry_minute == (
        None if minute is None else day.bars[0].start + minute
    )
    s, buys = registered_orders(day, arm, closes)
    if minute is None:
        assert buys == []
        assert any("extended" in why for why in s.dropped)
    else:
        assert len(buys) == 1 and (buys[0].ts_init - T0) // MINUTE == minute


def test_an_unlisted_bar_clears_the_registered_run() -> None:
    closes = [10.4, 10.5, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8]
    day = entry_day(closes)
    assert study.replay(day, study.REGISTERED).entry_minute == day.bars[0].start + 7
    _, buys = registered_orders(day, "registered", closes)
    assert len(buys) == 1 and (buys[0].ts_init - T0) // MINUTE == 7


def test_pre_watch_history_sets_the_original_listing_clock_but_never_warms_a_run() -> None:
    closes = [10.7, 10.8, 10.9, 11.0, 11.1]
    day = entry_day(closes)
    history = (
        study.Minute(study.PART_START, 10.4, 10.4, 10.4, 10.4, 100_000.0),
        study.Minute(day.bars[0].start - 2, 10.5, 10.5, 10.5, 10.5, 100_000.0),
        study.Minute(day.bars[0].start - 1, 10.6, 10.6, 10.6, 10.6, 100_000.0),
    )
    day = replace(day, bars=(*history, *day.bars))
    _, buys = registered_orders(day, "window-15", closes)
    assert buys == []  # already listed at 04:01, not when this watch began
    _, buys = registered_orders(day, "registered", closes)
    assert len(buys) == 1 and (buys[0].ts_init - T0) // MINUTE == 4


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"symbol": "BBB"}, "another symbol"),
        ({"day": datetime(2025, 11, 7).date()}, "New York day"),
        ({"missing": "incomplete coverage"}, "incomplete coverage"),
        ({"covered_to": study.PART_START + 1}, "coverage"),
        ({"reference": None}, "not listed"),
        ({"earlier": ()}, "volume ratio unknown"),
    ],
)
def test_a_registered_arm_refuses_unusable_context(changes: dict, why: str) -> None:
    day = replace(entry_day(RISING), **changes)
    s, buys = registered_orders(day, "registered", RISING)
    assert buys == []
    assert any(why in dropped for dropped in s.dropped)


def test_a_new_day_context_starts_a_new_watch_clock() -> None:
    """Replacing yesterday's context must not keep yesterday afternoon's minute as today's start."""
    closes = [10.4, 10.5, 10.6, 10.7, 10.8]
    day = entry_day(closes)
    old_day = replace(day, day=day.day - timedelta(days=1), bars=())
    slot = mover.Slot(AAA, bar_type(AAA), deque(), deque())
    stand_in: Any = SimpleNamespace(
        settings=SimpleNamespace(entry_arm="registered"),
        sold={},
        entry_contexts={AAA: old_day},
        _note=lambda _slot, _why: None,
        _say=lambda _text: None,
    )
    first = next(item for item in entry_tape(closes) if isinstance(item, Bar))
    yesterday = T0 - 24 * 60 * MINUTE + 4 * 60 * MINUTE
    Mover._registered_run(
        stand_in,
        slot,
        Bar(
            first.bar_type,
            first.open,
            first.high,
            first.low,
            first.close,
            first.volume,
            yesterday,
            yesterday + MINUTE,
        ),
    )
    stand_in.entry_contexts[AAA] = day
    for item in entry_tape(closes):
        if isinstance(item, Bar):
            Mover._registered_run(stand_in, slot, item)
    assert slot.run_ns == T0 + 4 * MINUTE


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_arm": "unregistered"},
        {"entry_arm": "registered", "run_bars": 4},
        {"entry_arm": "registered", "bar_spec": "5-MINUTE-LAST-EXTERNAL"},
    ],
)
def test_registered_arms_refuse_unregistered_rules(changes: dict) -> None:
    with pytest.raises(ValueError):
        strategy(**changes)


def test_with_no_ranking_nothing_is_watched_and_nothing_is_sent() -> None:
    eng = engine()
    s = strategy()
    orders = run(eng, s, None, tape(AAA, RISING))
    eng.dispose()
    assert orders == []
    assert s.slots == {}


def test_it_buys_the_first_rising_run_with_a_limit_at_the_ask_sized_from_risk() -> None:
    """The slot opens at the first step, so the bars from the second minute on are seen: 10.10, 10.20,
    10.30, 10.40 are three closes in a row above the one before. The buy is a DAY limit at the ask of that
    close (10.41), sized by upstream's `FixedRiskSizer`: 1% of the balance over the distance to the
    lowest planned sale (10.41 less the 0.27 the trail and the release give, less 2%: 9.93)."""
    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    entry = orders[0]
    instrument = eng.cache.instrument(AAA)
    price = Decimal("10.41")
    expected = FixedRiskSizer(instrument).calculate(
        Price.from_str("10.41"),
        Price.from_str("9.93"),
        Money(100_000, USD),
        Decimal("0.01"),
        hard_limit=Decimal(50_000) // price,
    )
    summary = (entry.side, entry.order_type.name, entry.time_in_force, entry.price.as_decimal())
    quantity, status, sent = entry.quantity, entry.status.name, entry.ts_init
    eng.dispose()
    assert summary == (OrderSide.BUY, "LIMIT", TimeInForce.DAY, price)
    assert quantity == expected and quantity.as_decimal() == Decimal(2083)
    assert status == "FILLED"
    assert T0 + 5 * MINUTE <= sent <= T0 + 5 * MINUTE + 2 * SECOND  # the step after the fifth bar


def test_the_size_is_capped_by_the_notional_cap_and_by_the_share_of_recent_dollar_volume() -> None:
    eng = engine()
    s = strategy(max_notional=Decimal(1000))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    capped = orders[0].quantity.as_decimal()
    eng.dispose()
    assert capped == Decimal(96)  # 1,000 // 10.41

    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, volume=2000))
    thin = orders[0].quantity.as_decimal()
    eng.dispose()
    # 5% of the four bars seen: (10.10 + 10.20 + 10.30 + 10.40) x 2,000 = 82,000 dollars -> 4,100 // 10.41
    assert thin == Decimal(393)


@pytest.mark.parametrize(
    ("changes", "ranking", "why"),
    [
        (
            {"max_move": Decimal("0.2")},
            {"value": rank((AAA, "0.25"))},
            "moved 0.25, over the no-chase cap",
        ),
        ({}, {"value": rank((BBB, "0.25"))}, None),  # another name ranks; AAA is not watched
        ({}, {"value": rank((AAA, "0.25")), "until_minute": 1}, "the ranking is stale"),
    ],
)
def test_no_entry_past_the_no_chase_cap_for_an_unranked_name_or_on_a_stale_ranking(
    changes: dict, ranking: dict, why: str | None
) -> None:
    eng = engine()
    s = strategy(**changes)
    orders = run(eng, s, ranker(ranking.pop("value"), **ranking), tape(AAA, RISING))
    eng.dispose()
    assert orders == []
    if why is not None:
        assert any(why in reason for reason in s.dropped), s.dropped


def test_entries_yield_at_the_soft_line_and_exits_do_not(monkeypatch) -> None:
    """With the adapter's count of the last 60 seconds at the soft line no entry is sent; the position
    another run opened is still protected and sold."""
    total = {"n": 0}

    def counted() -> int:
        total["n"] += 10  # ten requests a step: 600 in 60 s, over any line
        return total["n"]

    monkeypatch.setattr(mover, "requests_sent", counted)
    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    eng.dispose()
    assert orders == []
    assert any("soft line" in reason for reason in s.dropped), s.dropped


def test_one_slot_takes_the_better_ranked_name_when_two_signal_in_the_same_minute() -> None:
    eng = engine()
    s = strategy(slots=1, watch=2)
    orders = run(
        eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.25"))), tape(AAA, RISING), tape(BBB, RISING)
    )
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [BBB]


def test_two_slots_take_both() -> None:
    eng = engine()
    s = strategy(slots=2, watch=2)
    orders = run(
        eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.25"))), tape(AAA, RISING), tape(BBB, RISING)
    )
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [BBB, AAA]


def test_budget_composition_does_not_freeze_a_global_share_cap_at_the_initial_price():
    strategy, _ = mover_window.mover_universe_parts(
        {AAA: Decimal(20)},
        extended=False,
        feed="sip",
        wind_down_at=at(T0 + 20 * MINUTE),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
        slots=1,
        entry_notional=Decimal(1000),
        size_from_budget=True,
        loss_share=PAPER_LOSS_SHARE,
    )
    assert strategy.settings.max_shares is None
    assert strategy.settings.share_caps == {AAA: 50}


def test_a_budget_entry_after_a_30_percent_price_fall_is_clamped_to_its_start_time_cap():
    """ultra-3a8613a/14, the lead's change of 2026-11-12: price drift must not trip the guard. An entry
    is sized min(budget / price, start-time cap), recorded as `cap_bound` when the cap binds, and the
    name's share cap and its monitor's cap stay at the start-time bound."""
    start = Decimal("14.30")  # the name's price when the window chose it; the tape trades 30% under
    (cap,) = mover_window.mover_position_caps({AAA: start}, Decimal(10_000))
    start_cap = int(cap.max_position)
    eng = engine(order_cap=Decimal(100_000))
    s = strategy(
        max_notional=Decimal(10_000),
        share_caps={AAA: start_cap},
        risk=Decimal("0.00000001"),
        flow_share=Decimal(1),  # wide, so the budget and the cap are what bind
        protect=False,
    )
    s.settings.size_from_budget = True
    s.settings.loss_share = PAPER_LOSS_SHARE
    s.position_caps = {AAA: cap}
    eng.add_strategy(cap)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    buys = [order for order in orders if order.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 1
    assert Decimal(10_000) // buys[0].price.as_decimal() > start_cap, "the budget alone buys more"
    assert buys[0].filled_qty.as_decimal() == start_cap
    assert s.entries_sent[0]["sizing"]["binding"] == "cap_bound"
    assert s.entries_sent[0]["sizing"]["roster"] is True
    assert s.entries_sent[0]["sizing"]["held_by"] == "start_cap"
    assert s.settings.share_caps == {AAA: start_cap}
    assert cap.max_position == start_cap and cap.tripped is None


@pytest.mark.parametrize(
    ("free", "flow_share", "held_by"),
    [
        (Decimal(100_000), Decimal(1), "entry_notional"),
        (Decimal(500), Decimal(1), "free_balance"),
        (Decimal(100_000), Decimal("0.0001"), "flow_share"),
    ],
)
def test_an_entry_with_no_roster_records_which_bound_held_and_no_roster_flag(
    free, flow_share, held_by
):
    """The lead's ruling of 2026-11-12: the record names the bound that held. With no start-up roster
    (`share_caps` None) an entry is neither in nor outside one (code-review-high/universe-2-3)."""
    eng = engine(order_cap=Decimal(100_000), cash=free)
    s = strategy(
        max_notional=Decimal(1000),
        risk=Decimal("0.00000001"),
        flow_share=flow_share,
        protect=False,
    )
    s.settings.size_from_budget = True
    s.settings.loss_share = PAPER_LOSS_SHARE
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    eng.dispose()
    assert [o.side for o in orders][:1] == [OrderSide.BUY]
    sizing = s.entries_sent[0]["sizing"]
    assert sizing["roster"] is None
    assert sizing["held_by"] == held_by


@pytest.mark.parametrize("clamped", [True, False])
def test_a_forced_20x_sizing_fault_is_clamped_and_trips_the_monitor_if_it_gets_past(clamped):
    """ultra-3a8613a/14: a sizing fault twenty times the start figure is clamped to the start-time cap;
    with no share cap to clamp it, it reaches the venue and the monitor, whose cap was not rewritten,
    trips."""
    (cap,) = mover_window.mover_position_caps({AAA: Decimal(10)}, Decimal(1_000))
    start_cap = int(cap.max_position)
    eng = engine(order_cap=Decimal(100_000))
    s = strategy(
        max_notional=Decimal(
            20_000
        ),  # the fault: twenty times the 1000 USD the window started with
        share_caps={AAA: start_cap} if clamped else None,
        risk=Decimal("0.00000001"),
        flow_share=Decimal(1),
        protect=False,
    )
    s.settings.size_from_budget = True
    s.settings.loss_share = PAPER_LOSS_SHARE
    s.position_caps = {AAA: cap}
    eng.add_strategy(cap)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    buys = [order for order in orders if order.side == OrderSide.BUY]
    eng.dispose()
    assert buys and cap.max_position == start_cap
    if clamped:
        assert buys[0].quantity.as_decimal() == start_cap and cap.tripped is None
    else:
        assert buys[0].quantity.as_decimal() > 10 * start_cap
        assert cap.tripped is not None


def test_a_budget_entry_after_a_loss_fits_the_remaining_free_cash():
    eng = engine(cash=Decimal(1000))
    s = strategy(
        max_notional=Decimal(1000),
        size_from_budget=True,
        entries=2,
        cooldown_secs=0,
    )
    closes = [*RISING[:5], 10.1, 10.0, 9.9, 9.8, *RECLAIM, 10.8]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    eng.dispose()
    assert not any("NOTIONAL_EXCEEDS_FREE_BALANCE" in reason for _, reason in s.refused), s.refused
    bought = [
        order for order in orders if order.side == OrderSide.BUY and order.filled_qty.is_positive()
    ]
    assert len(bought) == 2
    assert bought[1].filled_qty.as_decimal() < Decimal(1000) // bought[1].price.as_decimal()


def test_two_entries_sent_in_one_step_share_the_free_cash_neither_has_locked_yet():
    """Review of fire-m-c, finding 6: entries sent in the same step were each sized from the same free
    cash, since the account locks an entry's cash only once the venue takes it, so the second was
    denied for exceeding the free balance and its run was lost. An entry not yet taken is now counted
    against the free cash the next one is sized from."""
    eng = engine(cash=Decimal(1500))
    s = strategy(
        max_notional=Decimal(1000),
        size_from_budget=True,
        slots=2,
        watch=2,
        flow_share=Decimal(1),
        protect=False,
    )
    orders = run(
        eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.25"))), tape(AAA, RISING), tape(BBB, RISING)
    )
    buys = [order for order in orders if order.side == OrderSide.BUY]
    eng.dispose()
    assert not s.refused, s.refused
    assert [o.instrument_id for o in buys[:2]] == [BBB, AAA]
    first, second = (o.quantity.as_decimal() * o.price.as_decimal() for o in buys[:2])
    assert first + second <= Decimal(1500)
    assert Decimal(s.entries_sent[1]["sizing"]["unlocked_entries"]) == first


def test_budget_sizing_preserves_the_configured_global_share_cap_for_each_slot():
    eng = engine()
    s = strategy(
        slots=2,
        watch=2,
        max_notional=Decimal(10_000),
        max_shares=7,
        size_from_budget=True,
        protect=False,
    )
    orders = run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.3"))),
        tape(AAA, RISING),
        tape(BBB, [price + 10 for price in RISING]),
    )
    eng.dispose()
    bought = [order for order in orders if order.side == OrderSide.BUY]
    assert s.settings.max_shares == 7
    assert [order.filled_qty.as_decimal() for order in bought] == [Decimal(7), Decimal(7)]


def test_the_cash_engines_funding_limit_is_explicit_in_a_buying_power_allocation():
    from alpaca.trading.models import TradeAccount

    from canon_trading import session_window

    account = TradeAccount.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "account_number": "OFFLINE",
            "status": "ACTIVE",
            "cash": "100",
            "buying_power": "200",
        }
    )
    allocation = session_window.mover_allocation(
        cast(Any, SimpleNamespace(get_account=lambda: account)), 1
    )
    eng = engine(cash=Decimal(100), order_cap=allocation.entry_notional * 10)
    s = strategy(max_notional=allocation.entry_notional, size_from_budget=True, protect=False)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    buys = [order for order in orders if order.side == OrderSide.BUY]
    eng.dispose()
    assert buys and buys[0].status.name == "FILLED", s.refused
    assert allocation.entry_notional == Decimal(100)
    assert allocation.record()["engine_cash_limit"] == "100"


def test_the_record_keeps_every_opened_slot_even_after_unwatching():
    eng = engine()
    s = strategy(slots=1, watch=1)
    run(
        eng,
        s,
        ranker(rank((AAA, "0.25")), values={3: rank((BBB, "0.3")), 6: ""}),
        tape(AAA, [10.0] * 9),
        tape(BBB, [20.0] * 9),
    )
    eng.dispose()
    assert not s.slots
    assert s.record()["opened_slots"] == ["AAA.ALPACA", "BBB.ALPACA"]


def test_each_name_uses_its_own_share_cap_under_the_entry_notional_bound() -> None:
    eng = engine(order_cap=OUTER_BOUND_USD)
    s = strategy(
        slots=2,
        watch=2,
        max_notional=Decimal(500),
        share_caps={AAA: 7, BBB: 13},
    )
    orders = run(
        eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.25"))), tape(AAA, RISING), tape(BBB, RISING)
    )
    buys = {o.instrument_id: o for o in orders if o.side == OrderSide.BUY}
    eng.dispose()
    assert {i: o.filled_qty.as_decimal() for i, o in buys.items()} == {
        AAA: Decimal(7),
        BBB: Decimal(13),
    }
    assert all(o.quantity.as_decimal() * o.price.as_decimal() <= 500 for o in buys.values())


def test_a_global_share_cap_can_tighten_each_names_own_cap() -> None:
    eng = engine()
    s = strategy(slots=2, watch=2, max_shares=5, share_caps={AAA: 7, BBB: 3})
    orders = run(
        eng, s, ranker(rank((BBB, "0.4"), (AAA, "0.25"))), tape(AAA, RISING), tape(BBB, RISING)
    )
    buys = {o.instrument_id: o.filled_qty.as_decimal() for o in orders if o.side == OrderSide.BUY}
    eng.dispose()
    assert buys == {AAA: Decimal(5), BBB: Decimal(3)}


def test_a_ranked_name_without_a_window_share_cap_cannot_enter() -> None:
    eng = engine()
    s = strategy(share_caps={AAA: 7})
    orders = run(eng, s, ranker(rank((BBB, "0.4"))), tape(BBB, RISING))
    eng.dispose()
    assert not any(o.side == OrderSide.BUY for o in orders)
    assert any("no share cap" in why for why in s.dropped)


@pytest.mark.parametrize("caps", [{AAA: 0}, {AAA: -1}, {AAA: True}, {AAA: 1.5}, {"AAA": 7}])
def test_a_names_share_cap_requires_an_instrument_and_positive_whole_shares(caps) -> None:
    with pytest.raises(
        ValueError, match="share_caps must map instrument IDs to positive whole shares"
    ):
        strategy(share_caps=caps)


def test_each_window_name_has_an_independent_position_cap_in_the_same_node() -> None:
    eng = engine(order_cap=OUTER_BOUND_USD)
    caps = mover_window.mover_position_caps({AAA: Decimal(10), BBB: Decimal(20)})
    s = strategy(slots=2, watch=2, max_notional=Decimal(500), share_caps={AAA: 50, BBB: 25})
    for cap in caps:
        eng.add_strategy(cap)
    orders = run(
        eng,
        s,
        ranker(rank((BBB, "0.4"), (AAA, "0.25"))),
        tape(AAA, RISING),
        tape(BBB, [2 * close for close in RISING]),
    )
    quantities = {
        o.instrument_id: o.filled_qty.as_decimal() for o in orders if o.side == OrderSide.BUY
    }
    eng.dispose()
    assert quantities == {AAA: Decimal(48), BBB: Decimal(24)}
    assert [cap.max_position for cap in caps] == [Decimal(50), Decimal(25)]
    assert all(cap.checks > 1 and cap.tripped is None for cap in caps)


def test_the_second_names_position_cap_stops_an_oversized_position() -> None:
    eng = engine()
    caps = mover_window.mover_position_caps({AAA: Decimal(10), BBB: Decimal(20)})
    s = strategy(share_caps={BBB: 30})
    for cap in caps:
        eng.add_strategy(cap)
    run(eng, s, ranker(rank((BBB, "0.4"))), tape(BBB, [2 * close for close in RISING]))
    eng.dispose()
    assert caps[0].tripped is None
    assert caps[1].tripped == "net position 30 in BBB.ALPACA is over the cap of 25"


def test_the_multiname_window_parts_trade_both_names_with_the_registered_arm() -> None:
    eng = engine(order_cap=OUTER_BOUND_USD)
    bbb_closes = [2 * close for close in RISING]
    s, caps = mover_window.mover_universe_parts(
        {AAA: Decimal(10), BBB: Decimal(20)},
        extended=False,
        feed="sip",
        wind_down_at=at(T0 + 10 * MINUTE),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
        slots=2,
        bar_spec="1-MINUTE-LAST-EXTERNAL",
        entry_contexts={
            AAA: entry_day(RISING, 1000),
            BBB: replace(entry_day(bbb_closes, 1000), symbol="BBB", reference=20.0),
        },
    )
    for cap in caps:
        eng.add_strategy(cap)
    orders = run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.4"))),
        entry_tape(RISING),
        tape(BBB, bbb_closes),
    )
    eng.dispose()
    buys = {o.instrument_id: o for o in orders if o.side == OrderSide.BUY}
    assert set(buys) == {AAA, BBB}
    assert s.settings.entry_arm == "registered" and s.settings.entries is None
    assert s.settings.share_caps == {AAA: 50, BBB: 25}
    assert s.settings.rank_max_age_secs == 90
    assert all(o.quantity.as_decimal() * o.price.as_decimal() <= 500 for o in buys.values())
    assert all(cap.checks > 0 and cap.tripped is None for cap in caps)


def test_every_published_rerank_keeps_its_value_and_both_clocks_in_the_record() -> None:
    eng = engine()
    s = strategy()
    first, second = rank((AAA, "0.25")), rank((BBB, "0.4"), (AAA, "0.25"))
    run(eng, s, ranker(first, until_minute=2, values={2: second}), tape(AAA, RISING))
    rankings = s.record()["rankings"]
    eng.dispose()
    assert [r["value"] for r in rankings] == [first, first, second]
    assert all(r["received_ns"] >= r["computed_ns"] > 0 for r in rankings)


def live_ranking(tmp_path, records, *, baseline: str = "valid"):
    session = at(T0).astimezone(study.NEW_YORK).date()
    previous = session - timedelta(days=3)
    path = tmp_path / "sip.json"
    if baseline != "missing":
        movers_rank.write_baselines(
            path,
            session if baseline == "valid" else previous,
            {
                # established in every segment (ADR 0087): five earlier sessions, a trade in each
                symbol: movers_rank.Baseline(
                    cumulative=tuple(1000.0 * (i + 1) for i in range(movers_rank.SEGMENTS)),
                    sessions=tuple(
                        previous - timedelta(days=d)
                        for d in reversed(range(movers_rank.BASELINE_SESSIONS))
                    ),
                    traded_sessions=(movers_rank.BASELINE_SESSIONS,) * movers_rank.SEGMENTS,
                )
                for symbol in ("AAA", "BBB")
            },
        )

    def references(symbols, cutoff, session):
        return ReferenceRead(
            cutoff,
            {symbol: movers_rank.Reference(10.0, previous) for symbol in symbols},
            movers_rank.SplitRead(previous, session, ()),
            T0,
        )

    return mover_window.window_rank(
        MoversRankActorConfig(
            feed="sip",
            order_cap=500.0,
            baseline_path=str(path),
            reference_reader=references,
            inline_reads=True,
        ),
        records,
    )


def test_the_actor_reranks_the_loaded_roster_and_a_freed_slot_rotates(tmp_path) -> None:
    eng = engine(order_cap=OUTER_BOUND_USD)
    aaa = [*RISING, *FALL, *[9.8] * 12]
    bbb = [*[10.0] * 13, *[10.0 + 0.1 * i for i in range(1, 9)], *[10.8] * 3]
    s, caps = mover_window.mover_universe_parts(
        {AAA: Decimal(10), BBB: Decimal(10)},
        extended=False,
        feed="sip",
        wind_down_at=at(T0 + 22 * MINUTE),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
        slots=1,
        bar_spec="1-MINUTE-LAST-EXTERNAL",
        entry_contexts={
            AAA: replace(entry_day(aaa, 1000), bars=(), covered_to=660),
            BBB: replace(entry_day(bbb, 1000), symbol="BBB", bars=(), covered_to=660),
        },
    )
    ranking = live_ranking(tmp_path, s.ranking_records)
    eng.add_strategy(s)
    eng.add_actor(ranking)
    for context_feed in mover_window.mover_context_feeds(s, "sip"):
        eng.add_actor(context_feed)
    for cap in caps:
        eng.add_strategy(cap)
    eng.add_data(tape(AAA, aaa))
    eng.add_data(tape(BBB, bbb))
    eng.run()
    buys = sorted(
        (o for o in eng.cache.orders(strategy_id=MOVER_ID) if o.side == OrderSide.BUY),
        key=lambda o: o.init_event.ts_init,
    )
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert [o.instrument_id for o in buys] == [AAA, BBB], s.dropped
    assert any(
        str(r["value"]).startswith("AAA.ALPACA") for r in s.ranking_records if r["published"]
    )
    assert any(
        str(r["value"]).startswith("BBB.ALPACA") for r in s.ranking_records if r["published"]
    )
    assert len(s.ranking_records) > 2 and len(s.rankings) > 2
    assert flat and all(cap.tripped is None for cap in caps)


@pytest.mark.parametrize("failed", ["never", "always", "once"])
def test_startup_context_gaps_use_the_nodes_history_and_a_failed_read_is_retried(
    monkeypatch, failed
) -> None:
    """ultra-3a8613a/5, the lead's ruling of 2026-11-12: a failed native history read leaves the
    name's coverage unchanged and is asked again at the next refresh, so one 5xx, timeout or 429
    does not take the name out of registered-arm entries for the rest of the window; while it keeps
    failing, an entry is refused as not covered, and it is asked again once a minute, not each tick."""
    eng = engine()
    s = strategy(entry_arm="registered", protect=False, entries=1)
    s.entry_contexts[AAA] = replace(entry_day(RISING, 1000), bars=(), covered_to=659)
    (context_feed,) = mover_window.mover_context_feeds(s, "sip")
    requests: list = []

    def history(bar_type, **kwargs):
        requests.append((str(bar_type), kwargs))
        fails = failed == "always" or (failed == "once" and len(requests) == 1)
        context_feed.on_data_response(
            cast(
                Any,
                SimpleNamespace(completed=True, failure_reason="history failed" if fails else None),
            )
        )
        return "context-request"

    monkeypatch.setattr(context_feed, "request_bars", history)
    eng.add_actor(context_feed)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), entry_tape(RISING))
    eng.dispose()
    bar_type, request = requests[0]
    assert bar_type == "AAA.ALPACA-1-MINUTE-LAST-EXTERNAL"
    assert request["params"] == {"feed": "sip"}
    assert request["start"] == at(T0 - MINUTE)
    assert request["end"] == at(T0)
    buys = [o for o in orders if o.side == OrderSide.BUY]
    context = s.record()["entry_contexts"][str(AAA)]
    assert context["missing"] is None
    if failed == "never":
        assert len(requests) == 1 and buys
    elif failed == "once":
        assert len(requests) == 2 and buys
        assert requests[1][1]["start"] == at(T0 - MINUTE), "coverage moved on a failed read"
    else:
        assert len(requests) > 2 and not buys
        ends = [r[1]["end"] for r in requests]
        assert len(set(ends)) == len(ends), "a failed read was asked again inside one minute"
        assert any("coverage does not reach" in why for why in s.dropped)
    if buys:
        assert context["covered_to"] >= 668


@pytest.mark.parametrize("baseline", ["missing", "other-session"])
def test_without_a_current_baseline_the_actor_records_the_problem_and_never_substitutes_a_rank(
    tmp_path, baseline
) -> None:
    eng = engine()
    s = strategy()
    ranking = live_ranking(tmp_path, s.ranking_records, baseline=baseline)
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_data(tape(AAA, RISING))
    eng.run()
    orders = eng.cache.orders(strategy_id=MOVER_ID)
    eng.dispose()
    assert not orders and s.rankings == []
    assert s.ranking_records and not any(r["published"] for r in s.ranking_records)
    assert any("baseline" in str(r["reasons"]) for r in s.ranking_records)


def test_an_unranked_names_context_is_refreshed_by_time_without_external_bars(monkeypatch) -> None:
    closes = [10.0 + i / 10 for i in range(16)]
    history = [item for item in tape(AAA, closes) if isinstance(item, Bar)]
    eng = engine()
    s = strategy(
        entry_arm="registered", bar_spec="1-MINUTE-LAST-INTERNAL", protect=False, entries=1
    )
    s.entry_contexts[AAA] = replace(entry_day(closes, 1000), bars=(), covered_to=660)
    (context_feed,) = mover_window.mover_context_feeds(s, "sip")
    requested: list = []

    def read(bar_type, **kwargs):
        requested.append(kwargs)
        context_feed.on_historical_bars(
            [bar for bar in history if kwargs["start"] <= at(bar.ts_event) < kwargs["end"]]
        )
        context_feed.on_data_response(
            cast(Any, SimpleNamespace(completed=True, failure_reason=None))
        )
        return "context-request"

    monkeypatch.setattr(context_feed, "request_bars", read)
    eng.add_actor(context_feed)
    orders = run(eng, s, ranker("", values={5: rank((AAA, "0.25"))}), trade_tape(AAA, closes))
    eng.dispose()
    assert any(o.side == OrderSide.BUY for o in orders), s.dropped
    ends = [request["end"] for request in requested]
    assert len(ends) >= 5 and len(ends) == len(set(ends))
    assert sum(end <= at(T0 + 5 * MINUTE) for end in ends) == 5
    assert all(request["end"] - request["start"] <= timedelta(minutes=1) for request in requested)


@pytest.mark.parametrize("has_history", [True, False])
def test_a_mid_minute_subscription_never_replaces_context_with_a_partial_bar(has_history):
    eng = engine()
    s = strategy(entry_arm="registered", bar_spec="1-MINUTE-LAST-INTERNAL", protect=False)
    original = study.Minute(660, 10.0, 11.0, 9.0, 10.2, 123_456.0)
    s.entry_contexts[AAA] = replace(
        entry_day([10.2]),
        bars=(original,) if has_history else (),
        covered_to=661 if has_history else 660,
    )
    quote = QuoteTick(
        AAA,
        Price(10.39, 4),
        Price(10.41, 4),
        Quantity.from_int(10000),
        Quantity.from_int(10000),
        T0 + MINUTE // 2,
        T0 + MINUTE // 2,
    )
    tail = QuoteTick(
        AAA,
        Price(10.39, 4),
        Price(10.41, 4),
        Quantity.from_int(10000),
        Quantity.from_int(10000),
        T0 + MINUTE + SECOND,
        T0 + MINUTE + SECOND,
    )
    run(eng, s, ranker(rank((AAA, "0.25"))), [quote, *trade_tape(AAA, [10.4]), tail])
    eng.dispose()
    known = {bar.start: bar for bar in s.entry_contexts[AAA].bars}
    if has_history:
        assert known[660] == original
    else:
        assert 660 not in known


def test_a_registered_watch_starts_at_the_first_full_minute_after_subscription():
    closes = [10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
    eng = engine()
    s = strategy(
        entry_arm="registered", bar_spec="1-MINUTE-LAST-INTERNAL", protect=False, entries=1
    )
    context = entry_day(closes)
    s.entry_contexts[AAA] = replace(context, reference=9.0, bars=context.bars[:1], covered_to=661)
    orders = run(
        eng,
        s,
        ranker(rank((AAA, "0.25"))),
        [quote(30, 10.09, 10.11), *trade_tape(AAA, closes)],
    )
    eng.dispose()
    assert s.slots[AAA].entry_start == 661
    buys = [order for order in orders if order.side == OrderSide.BUY]
    assert buys and (buys[0].init_event.ts_init - T0) // MINUTE == 5


@pytest.mark.parametrize("arm", ["bridge", "registered"])
def test_a_partial_internal_bar_never_counts_as_a_watched_close_or_flow(arm):
    closes = [10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
    eng = engine()
    s = strategy(entry_arm=arm, bar_spec="1-MINUTE-LAST-INTERNAL", protect=False, entries=1)
    context = entry_day(closes)
    s.entry_contexts[AAA] = replace(context, reference=9.0, bars=context.bars[:1], covered_to=661)
    orders = run(
        eng,
        s,
        ranker(rank((AAA, "0.25"))),
        [quote(30, 10.09, 10.11), *trade_tape(AAA, closes)],
    )
    eng.dispose()
    assert s.live_bars == 4
    assert len(s.slots[AAA].dollars) == 4
    assert sum(s.slots[AAA].dollars) == Decimal(100_000) * sum(
        map(Decimal, ("10.2", "10.3", "10.4", "10.5"))
    )
    buys = [order for order in orders if order.side == OrderSide.BUY]
    assert buys and (buys[0].init_event.ts_init - T0) // MINUTE == 5


def test_an_entry_not_filled_in_time_is_canceled(monkeypatch) -> None:
    monkeypatch.setattr(mover, "entry_price", lambda ask, ticks: ask - 1)  # far below the market
    eng = engine()
    s = strategy(entry_timeout_secs=20)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    first = orders[0]
    status, lived = first.status.name, first.ts_last - first.ts_init
    eng.dispose()
    assert status == "CANCELED"
    assert 20 * SECOND <= lived <= 22 * SECOND
    assert all(o.side == OrderSide.BUY for o in orders)  # nothing was held, so nothing was sold


# --- protection and exit -----------------------------------------------------------------------------


def test_the_fill_is_protected_by_an_emulated_trailing_stop_limit_that_only_rises() -> None:
    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    entry, protect = orders[0], orders[1]
    held = eng.cache.positions_open(instrument_id=AAA, strategy_id=MOVER_ID)[0].quantity
    facts = (
        protect.side,
        protect.order_type.name,
        protect.status.name,
        protect.emulation_trigger,
        protect.time_in_force,
        protect.quantity == entry.quantity == held,
    )
    eng.dispose()
    assert facts == (
        OrderSide.SELL,
        "TRAILING_STOP_LIMIT",
        "CANCELED",  # by the run's stop; it was held by the emulator until then (`triggers`)
        TriggerType.BID_ASK,
        TimeInForce.DAY,
        True,
    )
    assert len(orders) == 2
    assert s.triggers and all(a <= b for a, b in pairwise(s.triggers))
    assert s.triggers[-1] == Decimal("10.69") - Decimal("0.21")  # the last bid less the trail


def test_a_fall_through_the_trigger_releases_a_limit_that_sells_all_and_nothing_is_sold_flat() -> (
    None
):
    """The bid falls from 10.69 to 10.45, through the trigger at 10.48: the emulator releases a DAY limit
    below the trigger (10.42), which the bid fills. After it the strategy is flat and sends nothing."""
    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    entry, exit_ = orders[0], orders[1]
    facts = (exit_.side, exit_.order_type.name, exit_.status.name, exit_.time_in_force)
    sold, bought = exit_.filled_qty, entry.filled_qty
    limit = exit_.price.as_decimal()
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert facts == (OrderSide.SELL, "LIMIT", "FILLED", TimeInForce.DAY)
    assert sold == bought and flat
    assert limit == Decimal("10.69") - Decimal("0.21") - Decimal("0.06")  # trail, then 0.5% through
    assert len(orders) == 2  # one buy, one sell: long only, and no sale while flat


def test_a_release_that_rests_is_chased_down_to_the_bid_but_not_below_its_cap() -> None:
    """A gap: the bid falls from 10.69 to 9.99 in one quote, so the released limit (10.42) rests above
    it. Every `chase_secs` it is canceled and sent again one tick through the bid, never below 2% under
    its first price (10.42 -> floor 10.22); the bid is under the floor, so it rests there, unfilled,
    until the run's stop cancels it."""
    eng = engine()
    s = strategy(chase_secs=5)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [*RISING, 10.00, 10.00]))
    exit_ = orders[-1]
    facts = (exit_.order_type.name, exit_.status.name, exit_.price.as_decimal())
    count = len(orders)
    eng.dispose()
    assert facts == ("LIMIT", "CANCELED", Decimal("10.22"))
    assert count == 3


def test_a_resting_sale_is_chased_over_the_soft_line_and_fills(monkeypatch) -> None:
    """The lead's decision of 2026-11-12: sales are exempt from the
    soft line and stop only at the engine's hard ceiling. With the last 60 seconds far over the soft line,
    the released limit (10.42) resting above the bid is still sent again at 10.28 and filled."""
    total = {"n": 0}
    s = strategy(chase_secs=5)

    def counted() -> int:
        if any(slot.exiting for slot in s.slots.values()):
            total["n"] += 1000  # a thousand a step once the sale is out: over any line
        return total["n"]

    monkeypatch.setattr(mover, "requests_sent", counted)
    eng = engine()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [*RISING, 10.30, 10.30]))
    summary = sides(orders)
    sale = orders[-1].price.as_decimal()
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert summary == ["BUY LIMIT FILLED", "SELL LIMIT CANCELED", "SELL LIMIT FILLED"]
    assert sale == Decimal("10.28") and flat
    assert not any("soft line" in reason for reason in s.dropped), s.dropped


def test_a_chase_inside_the_cap_follows_the_bid_and_fills() -> None:
    """The bid gaps to 10.29, inside the cap: the limit is sent again at 10.28 and is filled by the next quote."""
    eng = engine()
    s = strategy(chase_secs=5)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [*RISING, 10.30, 10.30]))
    exit_ = orders[-1]
    facts = (exit_.status.name, exit_.price.as_decimal())
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    count = len(orders)
    eng.dispose()
    assert facts == ("FILLED", Decimal("10.28"))
    assert flat and count == 3


def test_a_chase_by_resend_cancels_the_resting_limit_and_sends_one_at_the_new_price() -> None:
    eng = engine()
    s = strategy(chase_secs=5)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [*RISING, 10.30, 10.30]))
    summary = sides(orders)
    last_price = orders[-1].price.as_decimal()
    sold = sum(o.filled_qty.as_decimal() for o in orders if o.side == OrderSide.SELL)
    bought = orders[0].filled_qty.as_decimal()
    eng.dispose()
    assert summary == ["BUY LIMIT FILLED", "SELL LIMIT CANCELED", "SELL LIMIT FILLED"]
    assert last_price == Decimal("10.28")
    assert sold == bought


def test_the_hold_to_close_row_sends_no_protection_and_sells_at_the_wind_down() -> None:
    eng = engine()
    s = strategy(
        protect=False,
        wind_down_at=datetime.fromtimestamp((T0 + 10 * MINUTE) / 1e9, tz=UTC),
        extended_hours=True,
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    summary = sides(orders)
    closed_at = orders[-1].ts_init
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert summary == ["BUY LIMIT FILLED", "SELL LIMIT FILLED"]
    assert closed_at >= T0 + 10 * MINUTE
    assert flat


# --- sessions and the end ----------------------------------------------------------------------------


def test_outside_the_session_every_order_is_marked_extended_hours() -> None:
    eng = engine()
    s = strategy(extended_hours=True)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    kinds = {(o.order_type.name, o.time_in_force.name) for o in orders}
    eng.dispose()
    assert len(orders) == 2 and kinds == {("LIMIT", "DAY")}
    assert s.params == [EXTENDED_HOURS, EXTENDED_HOURS]


def test_in_the_session_no_order_is_marked() -> None:
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    eng.dispose()
    assert s.params == [None, None]


def test_the_wind_down_outside_the_session_cancels_the_protection_and_sells_with_a_marked_limit() -> (
    None
):
    eng = engine()
    s = strategy(
        extended_hours=True, wind_down_at=datetime.fromtimestamp((T0 + 6 * MINUTE) / 1e9, tz=UTC)
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    summary = sides(orders)
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert summary == [
        "BUY LIMIT FILLED",
        "SELL TRAILING_STOP_LIMIT CANCELED",
        "SELL LIMIT FILLED",
    ]
    assert flat
    assert s.params == [EXTENDED_HOURS] * 3


def test_the_wind_down_in_the_session_is_upstreams_market_exit_and_nothing_is_entered_after() -> (
    None
):
    eng = engine()
    s = strategy(
        wind_down_at=datetime.fromtimestamp((T0 + 6 * MINUTE) / 1e9, tz=UTC),
        market_exit_reduce_only=False,
        market_exit_time_in_force=TimeInForce.DAY,
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    last = orders[-1]
    facts = (
        last.side,
        last.order_type.name,
        last.status.name,
        last.is_reduce_only,
        list(last.tags or []),
    )
    buys = [o for o in orders if o.side == OrderSide.BUY]
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    trailing = (orders[1].status.name, orders[1].ts_last <= last.ts_init)
    eng.dispose()
    assert facts == (OrderSide.SELL, "MARKET", "FILLED", False, ["MARKET_EXIT"])
    assert len(buys) == 1 and flat
    # at the pin upstream's market exit leaves an emulated order where it is (fixed in rc9's notes), so
    # the trailing sell is canceled first and the market exit follows
    assert trailing == ("CANCELED", True)


# --- what the node's log says of each decision ----------------------------------------------------------


def test_the_log_names_the_ranking_each_bars_run_and_the_order_sent() -> None:
    """A window's record of which rule decided is built from these lines: the ranking when it arrives or
    changes, with its age; for each bar of a watched name the state of its run (at most a line a minute
    a name); and the entry."""
    eng = engine()
    s = strategy()
    closes = [10.00, 10.10, 10.20, 10.20, 10.30, 10.40, 10.50, 10.60]
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    eng.dispose()
    runs = [line for line in s.said if line.startswith("AAA.ALPACA: run ")]
    assert s.said[0].startswith("Ranking: AAA.ALPACA=0.25; computed ") and s.said[0].endswith(
        " s ago"
    )
    # a ranking is said once: the test's ranker computes one a minute, and the step runs each second
    assert 1 <= sum(line.startswith("Ranking: ") for line in s.said) <= len(closes)
    assert runs == [
        "AAA.ALPACA: run building, 0 of 3 rising closes (10.1000)",  # the slot's first bar
        "AAA.ALPACA: run building, 1 of 3 rising closes (10.2000 after 10.1000)",
        "AAA.ALPACA: run restarted, 10.2000 is not above 10.2000",
        "AAA.ALPACA: run building, 1 of 3 rising closes (10.3000 after 10.2000)",
        "AAA.ALPACA: run building, 2 of 3 rising closes (10.4000 after 10.3000)",
        "AAA.ALPACA: run qualified, 3 rising closes to 10.5000",
        "AAA.ALPACA: run qualified, 3 rising closes to 10.6000",
    ]
    assert any(line.startswith("AAA.ALPACA: BUY ") and "limit 10.51" in line for line in s.said)


def test_the_log_names_the_rule_that_refused_an_entry_with_its_two_numbers() -> None:
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0.30))
    eng.dispose()
    refusals = [line for line in s.said if "no entry" in line]
    assert refusals and all(
        "the bid 10." in line and "planned stop 10." in line for line in refusals
    )
    assert [line.split(": no entry: ", 1)[1] for line in refusals] == [
        why.split(": ", 1)[1] for why in s.dropped
    ]


# --- findings of the recorded day's replay and of the second reader (Codex), 2026-11-11 ---------------


def quote(sec: float, bid: float, ask: float, *, bid_size: int = 10_000, ask_size: int = 10_000):
    ns = T0 + int(sec * SECOND)
    return QuoteTick(
        AAA,
        Price(bid, 4),
        Price(ask, 4),
        Quantity.from_int(bid_size),
        Quantity.from_int(ask_size),
        ns,
        ns,
    )


def thin_entry(ask_size: int = 1) -> list:
    """The first leg's tape up to the entry's quote, which offers `ask_size` shares at the ask."""
    data = []
    for item in tape(AAA, RISING[:5]):
        if isinstance(item, QuoteTick) and item.ts_event == T0 + 5 * MINUTE - 1:
            item = QuoteTick(
                item.instrument_id,
                item.bid_price,
                item.ask_price,
                Quantity.from_int(10_000),
                Quantity.from_int(ask_size),
                item.ts_event,
                item.ts_init,
            )
        data.append(item)
    return data


def test_a_working_entry_is_canceled_once_its_exit_is_released() -> None:
    """The recorded day's replay (SCKT, 2026-11-11): the entry kept filling for 14 seconds after the
    trailing sell had released, each lot sold a second later at a loss. Three shares are wanted and one
    is offered; the bid then falls through the trigger, and two more are offered at the entry's price
    two seconds later. The entry is canceled at the step after the release, so they are not bought."""
    eng = engine(liquid=True)
    s = strategy(max_notional=Decimal("31.5"))
    data = [*thin_entry(), quote(304, 9.00, 10.50), quote(306, 9.00, 10.41, ask_size=2)]
    data += [quote(312, 10.20, 10.50), quote(320, 10.20, 10.50)]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    entry = orders[0]
    facts = (entry.status.name, entry.filled_qty.as_decimal())
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert facts == ("CANCELED", Decimal(1))
    assert flat


def test_shares_bought_after_the_release_are_covered_by_a_new_sale_for_all_that_is_held() -> None:
    """Codex's finding P1-2 at 660b37e: the first share's sell had released when two more shares of the
    entry filled, in the same second, and the resting sell of one stayed the only exit, under no chase
    since the bid was below its floor. The sale that covers less than is held is canceled, and the
    next is for all three."""
    eng = engine(liquid=True)
    s = strategy(max_notional=Decimal("31.5"), chase_secs=5)
    data = [*thin_entry(), quote(304, 9.00, 10.50), quote(304.5, 9.00, 10.41, ask_size=2)]
    data += [quote(312, 9.00, 10.50), quote(320, 9.00, 10.50), quote(330, 10.30, 10.50)]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    bought = orders[0].filled_qty.as_decimal()
    sells = [o for o in orders if o.side == OrderSide.SELL]
    last = (sells[-1].quantity.as_decimal(), sells[-1].status.name)
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert bought == Decimal(3)
    assert last == (Decimal(3), "FILLED") and flat


def test_a_partly_filled_entry_is_protected_for_what_is_held_without_a_modify() -> None:
    """No order of this strategy is modified (the lead's decision of 2026-11-11): when more of the entry
    fills, the trailing sell held by the emulator is canceled and one for all that is held replaces it."""
    eng = engine(liquid=True)
    s = strategy(max_notional=Decimal("31.5"))
    s.modify_order = lambda *a, **k: pytest.fail("an order was modified")  # type: ignore[method-assign]
    data = [*thin_entry(), quote(303, 10.39, 10.41, ask_size=2), quote(320, 10.39, 10.60)]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    trailing = [o for o in orders if o.order_type.name == "TRAILING_STOP_LIMIT"]
    sizes = [o.quantity.as_decimal() for o in trailing]
    gap = trailing[1].ts_init - trailing[0].ts_last
    eng.dispose()
    assert sizes == [Decimal(1), Decimal(3)]
    # the emulator confirms its own cancel at once, so the new sell is made in the same step: the first
    # paper window had a second between the two, four times (ADR 0091)
    assert gap == 0


def test_a_release_that_is_refused_keeps_the_chase_floor_of_its_first_price() -> None:
    """Codex's finding P1-3 at 660b37e: the release (10.42) was denied before any step had seen it
    open, so no floor was set, and the sale sent in its place went through the bid at 9.98, under the
    2% cap (10.22). The floor is taken from the released order's own price, open or not."""
    eng = engine(order_cap=Decimal(1000))
    s = strategy(max_notional=Decimal(1000))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [*RISING, 10.00, 10.00]))
    sells = [o for o in orders if o.side == OrderSide.SELL]
    prices = {o.price.as_decimal() for o in sells[1:]}
    sold = sum(o.filled_qty.as_decimal() for o in sells)
    eng.dispose()
    assert sells[0].status.name == "DENIED"
    assert prices == {Decimal("10.22")} and sold == 0  # it rests at its floor, above the bid


def test_no_entry_into_a_quote_whose_bid_is_under_the_planned_stop() -> None:
    """The recorded day's replay (SCKT): bought at an ask of 0.3857 against a bid near 0.28, so the sale
    the size was risked against (the planned stop) was above the bid before the order was sent."""
    eng = engine()
    s = strategy()
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING, spread=0.30))
    eng.dispose()
    assert orders == []
    assert any("under the planned stop" in why for why in s.dropped)


def test_a_flat_minute_restarts_the_run() -> None:
    """A live node builds a bar for every clock minute, a paused one too, flat and with no volume
    (upstream's `time_bars_build_with_no_updates`, on by default). A repeated close is not a rise: the
    run starts again after it, so no entry is decided across a pause."""
    eng = engine()
    s = strategy()
    paused = [10.00, 10.10, 10.20, 10.20, 10.30, 10.40, 10.40, 10.50, 10.60]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, paused))
    eng.dispose()
    assert orders == []


def test_a_trailing_sell_made_under_a_dollar_releases_a_price_alpaca_takes_over_it() -> None:
    """Codex's findings P2-4 at 660b37e and P1-2 on the first run's tree: offsets on the sub-dollar
    step let the emulator release a limit like 1.0762, which Alpaca refuses at or above a dollar, and a
    check once a second cannot stand between two quotes a tenth of a second apart. Only the limit
    offset reaches the venue, so it is a whole number of cents: under a bid in cents the release is in
    cents, and under a dollar any four-decimal price is taken. The trigger's offset stays fine."""
    assert mover.offsets(Decimal("0.95"), Decimal("0.02"), Decimal("0.005")) == (
        Decimal("0.0190"),
        Decimal("0.03"),
    )
    assert mover.offsets(Decimal("0.2965"), Decimal("0.02"), Decimal("0.005")) == (
        Decimal("0.0060"),
        Decimal("0.01"),
    )
    assert mover.offsets(Decimal("10.41"), Decimal("0.02"), Decimal("0.005")) == (
        Decimal("0.21"),
        Decimal("0.27"),
    )
    # slowly: a bar a minute up through a dollar, then down through the trigger
    eng = engine()
    s = strategy(max_notional=Decimal(1000))
    closes = [0.90, 0.91, 0.92, 0.93, 0.94, 1.03, 1.08, 1.10, 1.07, 1.07]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    bought = orders[0].price.as_decimal()
    sells = [o for o in orders if o.side == OrderSide.SELL]
    slow = [(o.order_type.name, o.status.name, o.price.as_decimal()) for o in sells]
    eng.dispose()
    assert bought == Decimal("0.95")
    assert slow == [("LIMIT", "FILLED", Decimal("1.06"))]  # one sell, never remade: 1.09 less 0.03

    # at once: two quotes a tenth of a second apart, right after the trailing sell is made
    eng = engine()
    s = strategy(max_notional=Decimal(1000))
    data = [*tape(AAA, closes[:5]), quote(302.1, 1.10, 1.11), quote(302.2, 1.07, 1.08)]
    data += [quote(304, 1.07, 1.08), quote(306, 1.07, 1.08)]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    sells = [o for o in orders if o.side == OrderSide.SELL]
    fast = [(o.status.name, o.price.as_decimal(), sub_penny_denial(o.price)) for o in sells]
    eng.dispose()
    assert fast == [("FILLED", Decimal("1.07"), None)]  # 1.10 less 0.03
    assert entry_price(Decimal("0.9999"), 2) == Decimal("1.01")
    assert exit_price(Decimal("1.00"), 1, None) == Decimal("0.9999")
    assert exit_price(Decimal("10.29"), 1, None) == Decimal("10.28")


def test_a_fractional_cent_bid_never_releases_a_sub_penny_limit_above_one_dollar() -> None:
    """The coordinator's cross-check on the adapter's 0.0001 Equity increment (cx-m-c-composition's
    REPORT.md, "ORB cross-check"): a highest bid of 10.6937, then a fall through the trigger between
    timer steps. NautilusTrader's emulator rounds the trailing limit only to the instrument's increment
    (`trailing.rs`), so it releases LIMIT 10.4237, which Alpaca rejects; the adapter sends it at 10.42,
    a sell rounded down (`venue_limit_price`, ADR 0030), which Alpaca takes."""
    eng = engine()
    s = strategy(max_notional=Decimal(1000))
    data = [
        *tape(AAA, RISING[:5]),
        quote(302.1, 10.6937, 10.7037),
        quote(302.2, 10.46, 10.47),
        quote(304, 10.46, 10.47),
    ]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    sold = [order for order in orders if order.side == OrderSide.SELL]
    eng.dispose()
    assert sold
    released = [order.price.as_decimal() for order in sold]
    assert any(sub_penny_denial(order.price) is not None for order in sold), released
    sent = [venue_limit_price(order.price, order.side) for order in sold]
    assert all(sub_penny_denial(price) is None for price in sent), released
    assert all(s.as_decimal() <= r for s, r in zip(sent, released, strict=True))


def test_a_trailing_sell_made_again_keeps_the_trigger_it_had_reached() -> None:
    """One share of three is bought and protected; the bid rises to 10.59 (trigger 10.38), comes back
    to 10.39, and the other two shares fill. The sell for one share is canceled and one for three is
    made: its trigger starts where the first had reached, not 0.21 under the bid of that moment."""
    eng = engine(liquid=True)
    s = strategy(max_notional=Decimal("31.5"))
    data = [*thin_entry(), quote(302, 10.59, 10.61), quote(304, 10.39, 10.41, ask_size=2)]
    data += [quote(306, 10.39, 10.60), quote(310, 10.39, 10.60)]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    sizes = [o.quantity.as_decimal() for o in orders if o.side == OrderSide.SELL]
    eng.dispose()
    assert sizes == [Decimal(1), Decimal(3)]
    assert Decimal("10.38") in s.triggers
    assert all(a <= b for a, b in pairwise(s.triggers)) and s.triggers[-1] == Decimal("10.38")


def test_an_off_step_trailing_trigger_stays_in_the_emulator_and_its_remake_is_on_step() -> None:
    """An off-step trigger cannot reach the adapter today: the trailing stop-limit is emulated, and at
    the pin `crates/execution/src/order_emulator/emulator.rs:1283-1285,1355-1364,1394-1415` releases it
    as a plain LIMIT with no trigger, so the adapter's stop-price denial never sees one. The line moves
    anyway (the price-step unit, 2026-11-12): every price the Mover hands upstream is put on Alpaca's
    step by one function, and a long's stop rounds down. A partial entry remakes the trailing sell from
    the reached 10.4237; it is submitted at 10.42. Later moves of the trigger are the emulator's, at the
    instrument's 0.0001, and stay inside it.
    """
    submitted: list[Decimal | None] = []

    class Remakes(Recording):
        def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
            if order.order_type.name == "TRAILING_STOP_LIMIT":
                assert order.emulation_trigger == TriggerType.BID_ASK
                submitted.append(
                    None if order.trigger_price is None else order.trigger_price.as_decimal()
                )
            super().submit_order(order, position_id, client_id, params)

    eng = engine(liquid=True)
    s = Remakes(
        MoverConfig(
            max_notional=Decimal("31.5"),
            max_move=Decimal(5),
            entry_arm="bridge",
            entry_ticks=5,
            strategy_id=MOVER_ID,
            order_id_tag="001",
        )
    )
    data = [*thin_entry(), quote(302, 10.6337, 10.6537), quote(304, 10.43, 10.44, ask_size=2)]
    data += [quote(306, 10.41, 10.43), quote(310, 10.41, 10.43)]
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
        sells = [o for o in orders if o.side == OrderSide.SELL]
        assert Decimal("10.4237") in s.triggers  # upstream trails at the instrument's 0.0001
        assert [o.order_type.name for o in sells] == ["TRAILING_STOP_LIMIT", "LIMIT"]
        assert not any(type(e).__name__ == "OrderSubmitted" for e in sells[0].events())
        assert not sells[1].has_trigger_price and sells[1].status.name == "FILLED"
        assert submitted == [None, Decimal("10.42")]
    finally:
        eng.dispose()


def test_a_name_sold_while_unranked_keeps_its_cooldown() -> None:
    """AAA leaves the ranking while it is held, and its sale fills between two steps. The slot is not
    closed before the sale is recorded, so when AAA ranks again its cooldown still holds."""
    eng = engine()
    s = strategy(cooldown_secs=3600, watch=1)
    closes = [*RISING, *FALL, *RECLAIM]
    ranking = ranker(rank((AAA, "0.25")), values={7: rank((BBB, "0.3")), 11: rank((AAA, "0.25"))})
    orders = run(eng, s, ranking, tape(AAA, closes), tape(BBB, [20.0] * len(closes)))
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [AAA]
    assert any("inside the cooldown" in why for why in s.dropped)


def test_an_entry_that_never_fills_does_not_erase_the_sale_it_must_close_above() -> None:
    """Sold at 10.45. After the cooldown a run closes above it and a buy is sent, which nobody fills
    (no share is offered) and which is canceled at its time. A later run that closes under the sale's
    price is still refused: the sale's price is the name's until its next sale."""
    eng = engine(liquid=True)
    s = strategy(cooldown_secs=60, entry_timeout_secs=20)
    low = [10.40, 10.00, 10.05, 10.10, 10.15, 10.20]
    closes = [*RISING, *FALL, *RECLAIM, *low]
    reclaim_at = (
        T0 + (len(RISING) + len(FALL) + 6) * MINUTE - 1
    )  # the quote the second buy would hit
    data = []
    for item in tape(AAA, closes):
        if isinstance(item, QuoteTick) and item.ts_event >= reclaim_at:
            item = QuoteTick(
                item.instrument_id,
                item.bid_price,
                item.ask_price,
                Quantity.from_int(10_000),
                Quantity.from_int(0),
                item.ts_event,
                item.ts_init,
            )
        data.append(item)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    buys = [
        (o.status.name, o.filled_qty.as_decimal() > 0, o.price.as_decimal())
        for o in orders
        if o.side == OrderSide.BUY
    ]
    eng.dispose()
    assert buys[0][:2] == ("FILLED", True)
    assert len(buys) >= 2 and all(s == "CANCELED" and not filled for s, filled, _ in buys[1:])
    assert all(price > Decimal("10.45") for *_, price in buys[1:])  # none under the sale's price


def test_an_empty_ranking_unwatches_and_is_not_called_stale() -> None:
    """A ranking that names nobody is a ranking: a name that holds nothing loses its slot, and nothing
    is refused as stale."""
    eng = engine()
    s = strategy()
    flat = [10.00] * 8
    run(eng, s, ranker(rank((AAA, "0.25")), values={3: ""}), tape(AAA, flat))
    eng.dispose()
    assert s.slots == {}
    assert not any("stale" in why for why in s.dropped)


def test_a_runs_record_keeps_the_notes_of_a_name_whose_slot_was_closed() -> None:
    eng = engine()
    s = strategy(slots=1, watch=1)
    first = RISING + FALL
    ranking = ranker(rank((AAA, "0.25")), values={len(first): rank((BBB, "0.3"))})
    run(eng, s, ranking, tape(AAA, first + [9.8] * 4), tape(BBB, [20.0] * (len(first) + 4)))
    notes = s.record()["notes"]
    eng.dispose()
    assert AAA not in s.slots
    assert notes[str(AAA)] == ["entered at move 0.25"]


def test_a_price_over_the_schemes_top_is_refused_by_name_and_the_step_goes_on() -> None:
    eng = engine()
    s = strategy(max_notional=Decimal(1_000_000))
    orders = run(
        eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [6000, 6010, 6020, 6030, 6040, 6050])
    )
    eng.dispose()
    assert orders == []
    assert any("above the top tier of ALPACA_US_EQUITY_LIMIT" in why for why in s.dropped)


def test_a_malformed_and_a_good_ranking_between_two_steps_are_both_said() -> None:
    said: list[str] = []
    stand_in = cast(Any, Borrowed())
    stand_in.rank_said = None
    stand_in._say = said.append

    def hear(value: str, at: int) -> None:
        Mover.on_signal(
            stand_in,
            cast(Any, SimpleNamespace(name=RANK_SIGNAL, value=value, ts_event=at, ts_init=at)),
        )

    hear("?", 1)
    Mover._say_ranking(stand_in, 5 * SECOND)
    hear("?", 2)
    hear(rank((AAA, "0.25")), 6 * SECOND)
    Mover._say_ranking(stand_in, 8 * SECOND)
    assert said == [
        "Ranking: malformed (1 so far); no entry until one parses",
        "Ranking: malformed (2 so far); no entry until one parses",
        "Ranking: AAA.ALPACA=0.25; computed 2 s ago",
    ]


def test_a_trailing_sell_on_the_last_trade_is_not_remade_while_only_the_bid_is_at_a_dollar() -> (
    None
):
    """With `LAST_PRICE` the sell's offsets are on the step of the last trade, so the step is judged
    stale by the last trade too: a bid at a dollar over a last trade under it changes nothing. The
    trailing sell starts from a print after the entry's fill, never one cached before it
    (`Mover._followed`), so one print under a dollar follows the fill."""
    eng = engine()
    s = strategy(
        max_notional=Decimal(1000),
        trigger=TriggerType.LAST_PRICE,
        bar_spec="1-MINUTE-LAST-INTERNAL",
    )
    data = trade_tape(AAA, [0.90, 0.91, 0.92, 0.93, 0.94, 0.95], size=100_000)
    last = T0 + 6 * MINUTE
    data += [
        QuoteTick(
            AAA,
            Price(1.00, 4),
            Price(1.01, 4),
            Quantity.from_int(100),
            Quantity.from_int(100),
            t,
            t,
        )
        for t in range(last + SECOND, last + 12 * SECOND, 2 * SECOND)
    ]
    after = last + 2 * SECOND
    data.append(
        TradeTick(
            AAA,
            Price(0.95, 4),
            Quantity.from_int(100),
            AggressorSide.BUY,
            TradeId("A"),
            after,
            after,
        )
    )
    data.sort(key=lambda d: d.ts_init)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), data)
    trailing = [o for o in orders if o.side == OrderSide.SELL]
    eng.dispose()
    assert len(trailing) == 1


# --- the engine's per-order cap, which holds for a sale as for a buy --------------------------------


def test_a_full_sale_after_a_rise_passes_the_engines_cap_set_as_the_outer_bound() -> None:
    """The risk engine's per-order notional cap holds for every order whatever its side (at the pin,
    `crates/risk/src/engine/mod.rs:2491-2507`): with the cap at the entry's own notional, 96 shares
    bought at 10.41 inside 1,000 dollars are worth more once the bid is above it, and their one sale is
    denied (first case). So the entry is bounded where it is sized (`max_notional`), and the engine's
    cap is the node's outer bound at a multiple of it: the whole position is then sold in one order
    (second case)."""
    eng = engine(order_cap=Decimal(1000))
    s = strategy(max_notional=Decimal(1000))
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    eng.dispose()
    assert s.refused and all("NOTIONAL_EXCEEDS_MAX_PER_ORDER" in why for _, why in s.refused)
    # a refused sale is sent again, since an exit is never given up, but once every `chase_secs`, not
    # at every step: the release, then one each 5 s of the minute until the bid has fallen so far that
    # the 96 shares fit the cap again
    assert len(s.refused) == 1 + 60 // 5

    eng = engine(order_cap=Decimal(10_000))
    s = strategy(max_notional=Decimal(1000))
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    summary = [
        (o.side.name, o.order_type.name, o.status.name, o.quantity.as_decimal()) for o in orders
    ]
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert s.refused == []
    assert summary == [
        ("BUY", "LIMIT", "FILLED", Decimal(96)),
        ("SELL", "LIMIT", "FILLED", Decimal(96)),
    ]
    assert flat


def test_an_account_budget_sale_after_a_rise_passes_the_outer_engine_bound():
    entry = Decimal(40_000) / 4
    eng = engine(order_cap=entry * mover_window.OUTER_BOUND_MULTIPLE)
    s = strategy(max_notional=entry, size_from_budget=True)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL))
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert s.refused == []
    sold = [order for order in orders if order.side == OrderSide.SELL and order.is_closed]
    assert sold and sold[0].quantity.as_decimal() * sold[0].price.as_decimal() > entry
    assert flat


def test_the_size_is_capped_by_the_shares_a_run_allows() -> None:
    eng = engine()
    s = strategy(max_shares=50)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    quantity = orders[0].quantity.as_decimal()
    eng.dispose()
    assert quantity == Decimal(50)


def test_a_run_of_one_entry_buys_once_and_names_why_the_reclaim_was_passed_over() -> None:
    eng = engine()
    s = strategy(cooldown_secs=60, entries=1)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + RECLAIM))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 1
    assert any("the run's 1 entry is used" in why for why in s.dropped)


def test_live_bars_are_counted_for_the_runs_report() -> None:
    eng = engine()
    s = strategy()
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    eng.dispose()
    assert s.live_bars == len(RISING) - 1  # the slot opens at the first step, after the first bar


# --- re-entry and rotation ---------------------------------------------------------------------------

RECLAIM = [10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70]


def test_after_an_exit_and_the_cooldown_it_buys_back_on_a_reclaim_while_the_name_ranks() -> None:
    """Sold at 10.45 after the fall. After the cooldown a new rising run that closes above the sale's
    price is a reclaim: a second buy."""
    eng = engine()
    s = strategy(cooldown_secs=60)
    closes = RISING + FALL + RECLAIM
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    second_close = (buys[1].ts_init - T0) // MINUTE if len(buys) > 1 else None
    eng.dispose()
    assert len(buys) == 2
    # the run must end above the sale's price: 10.50 is the first close of the new leg that does
    assert second_close == len(RISING + FALL) + 6


def test_no_buy_back_inside_the_cooldown_or_below_the_sale_price() -> None:
    eng = engine()
    s = strategy(cooldown_secs=3600)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + RECLAIM))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 1

    eng = engine()
    s = strategy(cooldown_secs=60)
    below = [9.80, 9.85, 9.90, 9.95, 10.00, 10.05]  # rising, and never back above the sale
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + below))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 1


def test_a_freed_slot_rotates_into_the_new_top_name() -> None:
    """AAA is sold; from minute 12 the ranking names BBB alone, whose first leg then starts."""
    eng = engine()
    s = strategy(slots=1, watch=1)
    first = RISING + FALL
    flat_then_leg = [20.0] * len(first) + [20.0, 20.2, 20.4, 20.6, 20.8, 21.0, 21.2, 21.4]
    ranking = ranker(rank((AAA, "0.25")), values={len(first): rank((BBB, "0.3"))})
    orders = run(eng, s, ranking, tape(AAA, first + [9.8] * 8), tape(BBB, flat_then_leg))
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [AAA, BBB]


def test_a_name_unwatched_after_its_sale_keeps_its_cooldown_and_its_sale_price() -> None:
    """Codex's finding P2-5 at 660b37e: AAA is sold, leaves the ranking for a minute (its slot is
    closed) and returns; the cooldown and the sale's price went with the slot, so the next rising run
    bought it back at once. They are the name's, kept by the strategy."""
    eng = engine()
    s = strategy(cooldown_secs=3600, watch=1)
    closes = [*RISING, *FALL, *RECLAIM]
    ranking = ranker(rank((AAA, "0.25")), values={12: rank((BBB, "0.3")), 13: rank((AAA, "0.25"))})
    orders = run(eng, s, ranking, tape(AAA, closes), tape(BBB, [20.0] * len(closes)))
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [AAA]
    assert any("inside the cooldown" in why for why in s.dropped)


# --- handlers that an order command of its own can call back into -----------------------------------


class Borrowed:
    """Stands in for a strategy while one of its own commands runs: at the pin the order events that a
    `submit_order`, `cancel_order` or `modify_order` publishes reach the handlers while the strategy
    object is mutably borrowed, so reading an engine attribute there raises (the 24/5 run's finding).
    Plain attributes work; the engine's do not."""

    def __init__(self) -> None:
        self.released: set[str] = set()
        self.fills: list = []
        self.fills_to_report: list = []
        self.refused: list = []
        self.rank: list = []
        self.rank_ns = 0
        self.rank_faults = 0
        self.rankings: list = []
        self.quotes = 0
        self.trades = 0
        self.persistence = False
        self.slots: dict = {}
        self.late_prints: dict = {}
        self.prints_ahead: dict = {}
        self.prints_on_receipt: dict = {}
        self.quote_lags: deque = deque(maxlen=101)
        self.quote_lag_least = None
        self.quote_lag_most = None
        self.opening_states: dict = {}
        self.opening_outcomes: dict = {}
        self.sale_rows: dict = {}
        self.sale_owner: dict = {}
        self.remakes_under_floor = 0
        self.order_refusals: list = []
        self.entries_sent: list = []

    def __getattr__(self, name: str):
        raise RuntimeError(f"Already mutably borrowed: {name}")


def test_no_order_event_handler_reads_the_engine() -> None:
    cid = SimpleNamespace(value="O-1")
    event = SimpleNamespace(
        client_order_id=cid,
        instrument_id=AAA,
        order_side=OrderSide.BUY,
        last_qty=Quantity.from_int(1),
        last_px=Price.from_str("10.00"),
        released_price=Price.from_str("10.00"),
        reason="no",
        ts_event=1,
        ts_init=1,
    )
    own = [
        name
        for name in vars(Mover)
        if name.startswith(("on_order_", "on_position_"))
        or name in ("on_signal", "on_quote", "on_trade")
    ]
    assert {"on_order_released", "on_order_filled", "on_signal"} <= set(own)
    borrowed = Borrowed()
    for name in own:
        payload = (
            SimpleNamespace(name=RANK_SIGNAL, value=rank((AAA, "0.25")), ts_event=5, ts_init=5)
            if name == "on_signal"
            else event
        )
        getattr(Mover, name)(cast(Any, borrowed), payload)
    assert borrowed.released == {"O-1"}
    assert borrowed.rank == [(AAA, Decimal("0.25"))] and borrowed.rank_ns == 5


def test_a_malformed_ranking_is_counted_and_leaves_no_ranking() -> None:
    borrowed = Borrowed()
    Mover.on_signal(
        cast(Any, borrowed),
        cast(Any, SimpleNamespace(name=RANK_SIGNAL, value="AAA.ALPACA", ts_event=5)),
    )
    assert borrowed.rank == [] and borrowed.rank_faults == 1


# --- what a session window adds (`canon_trading.mover_window`) ---------------------------------------


def trade_tape(instrument_id: InstrumentId, closes: list[float], *, size: int = 100_000) -> list:
    """What the adapter streams, a trade and a quote and no bar: each minute a trade at the close and
    the quote of that close just before the minute ends, so the data engine builds the minute's bar."""
    data: list = []
    for i, close in enumerate(closes):
        ts_close = T0 + (i + 1) * MINUTE
        data.append(
            TradeTick(
                instrument_id,
                Price(close, 4),
                Quantity.from_int(size),
                AggressorSide.BUY,
                TradeId(f"T-{i}"),
                ts_close - 2,
                ts_close - 2,
            )
        )
        data.append(
            QuoteTick(
                instrument_id,
                Price(close - 0.01, 4),
                Price(close + 0.01, 4),
                Quantity.from_int(10_000),
                Quantity.from_int(10_000),
                ts_close - 1,
                ts_close - 1,
            )
        )
    return data


def at(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def window_parts(**changes: object) -> tuple:
    settings: dict = {
        "price": Decimal("10.00"),
        "move": Decimal("0.25"),
        "computed_at": at(T0 - 3 * MINUTE),
        "ends": at(T0 + 20 * MINUTE),
        "extended": False,
        "feed": None,
        "wind_down_at": at(T0 + 18 * MINUTE),
        "market_exit_interval_ms": 1000,
        "market_exit_max_attempts": 60,
    }
    settings.update(changes)
    return mover_parts(AAA, **settings)


def test_a_windows_parts_trade_one_name_from_a_ranking_fixed_before_the_node_was_built() -> None:
    """The window's choice is published by `FixedRank` with the time it was computed, three minutes
    before the node started; the strategy builds its bars from trades, buys the first rising run inside
    the largest entry (500 // 10.51 = 47 shares), holds it under the trailing sell while the name
    rises, and sells at the wind-down by upstream's market exit. The risk engine's cap is the outer
    bound, and the position cap the shares the largest entry buys at the choice's price."""
    eng = engine(order_cap=OUTER_BOUND_USD)
    s, ranking, cap = window_parts()
    eng.add_data(trade_tape(AAA, [round(10 + 0.1 * i, 2) for i in range(22)]))  # rising throughout
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    eng.run()
    orders = sorted(
        eng.cache.orders(strategy_id=MOVER_ID),
        key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
    )
    summary = [(o.side.name, o.order_type.name, o.status.name) for o in orders]
    bought = orders[0].filled_qty.as_decimal()
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert s.rank_ns == T0 - 3 * MINUTE and s.rank_faults == 0
    assert ranking.published > 0
    assert summary[0] == ("BUY", "LIMIT", "FILLED") and bought == Decimal(47)
    assert [side for side, *_ in summary].count("BUY") == 1
    assert summary[-1] == ("SELL", "MARKET", "FILLED") and flat
    assert s.refused == [] and cap.tripped is None
    assert s.live_bars > 0


def test_a_windows_rows_are_the_first_runs() -> None:
    s, _, cap = window_parts(price=Decimal("2.40"))
    rows = s.settings
    # one slot by default, as outside the regular part; no bound on entries (`ENTRIES`)
    assert (rows.max_notional, rows.slots, rows.watch, rows.entries) == (Decimal(500), 1, 1, None)
    assert OUTER_BOUND_USD == Decimal(5000)
    assert rows.max_shares is None
    assert "max_shares" not in s.record()["rows"]
    assert max_shares(Decimal("2.40")) == 208
    assert cap.max_position == Decimal(208)
    assert rows.bar_spec == "1-MINUTE-LAST-INTERNAL"
    assert rows.chase == "resend"  # no order of a mover node is modified at the venue
    assert rows.rank_max_age_secs == 23 * 60  # from the choice to the window's end
    assert rows.manage_stop is True and rows.extended_hours is False
    outside, _, _ = window_parts(extended=True)
    assert outside.settings.manage_stop is False and outside.settings.extended_hours is True


def test_a_windows_slot_row_is_forwarded_to_the_strategy() -> None:
    outside, _, _ = window_parts(extended=True, slots=2)
    assert outside.settings.slots == outside.settings.watch == 2


@pytest.mark.parametrize("part", ["after-hours", "overnight", "pre-market"])
def test_each_extended_windows_orders_are_marked_day_limits(part) -> None:
    """Run the window's actual rows through the engine, including the emulator's released sell."""
    from canon_trading.session_window import MOVER_PARTS

    row = MOVER_PARTS[part]
    made, ranking, cap = window_parts(extended=True, feed=row.feed, slots=row.slots)
    s = Recording(made.settings)
    eng = engine(order_cap=OUTER_BOUND_USD)
    eng.add_data(trade_tape(AAA, RISING + FALL))
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    try:
        eng.run()
        orders = eng.cache.orders(strategy_id=MOVER_ID)
        assert {o.side.name for o in orders} == {"BUY", "SELL"}
        assert {(o.order_type.name, o.time_in_force.name) for o in orders} == {("LIMIT", "DAY")}
        assert s.params and all(params == EXTENDED_HOURS for params in s.params)
        assert s.settings.slots == part_slots(part) and cap.tripped is None
        assert not eng.cache.positions_open(strategy_id=MOVER_ID)
    finally:
        eng.dispose()


def test_a_name_whose_one_share_is_over_the_largest_entry_is_refused_before_the_node_is_built() -> (
    None
):
    with pytest.raises(ValueError, match="over the largest entry"):
        window_parts(price=Decimal("500.01"))


def test_a_session_parts_slots_follow_the_named_per_part_settings_unless_overridden() -> None:
    """Seven in every part (the lead's ruling of 2026-11-12), unless the run sets its own."""
    assert part_slots("regular") == 7
    assert [part_slots(p) for p in ("pre-market", "after-hours", "overnight")] == [7, 7, 7]
    assert part_slots("regular", 2) == 2 and part_slots("overnight", 3) == 3
    with pytest.raises(ValueError, match="slots"):
        part_slots("regular", 0)
    rows = window_parts(slots=4)[0].settings
    assert (rows.slots, rows.watch, rows.entries) == (4, 4, None)


SOLD_THEN_RECLAIMED = [
    *RISING,
    *FALL,
    *[9.80] * 5,  # the cooldown of 300 s, flat
    *[10.00, 10.20, 10.40, 10.60, 10.80, 11.00, 11.20],  # a new run closing above the sale's price
    *[11.20] * 4,
]


def test_a_windows_parts_buy_back_after_a_sale_until_the_part_ends() -> None:
    """A completed trade does not end the node (the lead's decision of 2026-11-11, Task M's gap 6): the
    name is sold on the fall, and after the cooldown a reclaim above the sale's price is bought again.
    Before, the window allowed one entry and named the reclaim as the entry allowance used."""
    eng = engine(order_cap=OUTER_BOUND_USD)
    minutes = len(SOLD_THEN_RECLAIMED)
    s, ranking, cap = window_parts(
        ends=at(T0 + (minutes + 2) * MINUTE), wind_down_at=at(T0 + minutes * MINUTE)
    )
    eng.add_data(trade_tape(AAA, SOLD_THEN_RECLAIMED))
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    eng.run()
    orders = sorted(
        eng.cache.orders(strategy_id=MOVER_ID),
        key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
    )
    buys = [o for o in orders if o.side == OrderSide.BUY and o.filled_qty.as_decimal() > 0]
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    assert len(buys) == 2, s.dropped
    assert not any("entr" in why and "used" in why for why in s.dropped)
    assert flat and cap.tripped is None


def test_each_entry_keeps_its_qualifying_bar_and_its_submit_for_the_record() -> None:
    """What the diagnostics are read from: the bar whose close completed the run (its `ts_init`, when
    the node had it) and the order's own creation time, on the node's clock."""
    eng = engine(order_cap=OUTER_BOUND_USD)
    s, ranking, cap = window_parts()
    eng.add_data(trade_tape(AAA, [round(10 + 0.1 * i, 2) for i in range(22)]))
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    eng.run()
    entry = s.record()["entries"][0]
    order = eng.cache.order(ClientOrderId(entry["client_order_id"]))
    assert order is not None
    eng.dispose()
    assert entry["instrument_id"] == str(AAA)
    # The slot opens mid-minute 1: skip that partial, then full minutes 2..5 are the four closes.
    assert entry["qualified_ns"] == T0 + 6 * MINUTE and entry["run_close"] == "10.5000"
    assert entry["submitted_ns"] == order.init_event.ts_init
    assert 0 <= entry["submitted_ns"] - entry["qualified_ns"] <= SECOND


def test_a_windows_entry_diagnostics_are_read_from_the_bars_its_node_built() -> None:
    """The same run as above, read as a window reads it after the stop: the entry at 10.51 on the bar
    that closed at 10.50, the delay under a step, and the bars built from trades after the fill up to
    the wind-down's sale as the move still ahead."""
    eng = engine(order_cap=OUTER_BOUND_USD)
    s, ranking, cap = window_parts()
    eng.add_data(trade_tape(AAA, [round(10 + 0.1 * i, 2) for i in range(22)]))
    eng.add_strategy(s)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    eng.run()
    record = s.record()
    bars = window_bars(eng.cache, [str(AAA)])
    eng.dispose()
    (got,) = entry_diagnostics(record["entries"], record["fills"], bars, {str(AAA): Decimal(8)})
    assert 0 <= got["qualified_to_submit_ms"] <= 1000
    assert got["entry_px"] == "10.5100"
    highs = [high for _, high in bars[str(AAA)]]
    assert highs == sorted(highs) and got["high_after"] == str(highs[-1])
    high = highs[-1]
    assert got["rise_left"] == str((high / Decimal("10.51") - 1).quantize(Decimal("0.0001")))
    assert got["ahead"] == str(((high - Decimal("10.51")) / (high - 8)).quantize(Decimal("0.0001")))


def test_the_entry_diagnostics_read_the_delay_and_the_move_still_ahead_from_the_tape() -> None:
    """From the window's own bars: the time from the qualifying bar to the submit, and how much of the
    move was still ahead at the fill, as a return on the entry and as a share of the move from the
    reference close to the highest high the window saw after the entry (ADR 0092's measure on the
    window's tape)."""
    entries = [
        {
            "client_order_id": "O-1",
            "instrument_id": "AAA.ALPACA",
            "qualified_ns": T0,
            "run_close": "10.4",
            "submitted_ns": T0 + 400_000_000,
        },
        {
            "client_order_id": "O-3",
            "instrument_id": "BBB.ALPACA",
            "qualified_ns": T0,
            "run_close": "20",
            "submitted_ns": T0 + 2 * SECOND,
        },
    ]
    fills = [["O-1", "BUY", "48", "10.41", T0 + SECOND], ["O-1", "BUY", "2", "10.43", T0 + SECOND]]
    bars = {
        "AAA.ALPACA": [
            (T0, Decimal("10.40")),
            (T0 + MINUTE, Decimal("11.00")),
            (T0 + 9 * MINUTE, Decimal("12.42")),
        ],
    }
    got = entry_diagnostics(entries, fills, bars, {"AAA.ALPACA": Decimal("8.42")})
    first, second = got
    assert first["qualified_to_submit_ms"] == 400
    assert first["entry_px"] == "10.4108"  # the fills' mean, weighted
    assert first["high_after"] == "12.42"
    assert first["rise_left"] == "0.1930"  # (12.42 - 10.4108) / 10.4108
    assert first["ahead"] == "0.5023"  # (12.42 - 10.4108) / (12.42 - 8.42)
    # an entry that never filled has a delay and nothing after it
    assert second["qualified_to_submit_ms"] == 2000
    assert second["entry_px"] is None and second["ahead"] is None and second["rise_left"] is None


# --- states the simulated venue does not reach: the sale path's own methods on a stand-in -----------------


class Seam:
    """Stands in for a `Mover` in one step of its sale path, for states a second reader named that the
    simulated venue does not produce (a refused remake, a cancel that failed, a position with no entry
    of the strategy's): the orders by client order ID, one bid, and what the step sent. Every method
    under test is `Mover`'s own."""

    _protect = Mover._protect
    _trail = Mover._trail
    _remade = Mover._remade
    _persistent_trail = Mover._persistent_trail
    _followed = Mover._followed
    _holding_trade = Mover._holding_trade
    _entry_filled_ns = Mover._entry_filled_ns
    _holding_since = Mover._holding_since
    on_quote = Mover.on_quote
    on_trade = Mover.on_trade
    _limit_sell = Mover._limit_sell
    _sell_out = Mover._sell_out
    _time_out = Mover._time_out
    _chase = Mover._chase
    _note = Mover._note
    _trail_anchor = Mover._trail_anchor
    _ensure_exit = Mover._ensure_exit
    _mark_exit = Mover._mark_exit
    _hold = Mover._hold
    _held_at_l = Mover._held_at_l
    _end_hold = Mover._end_hold
    _held_anchor = Mover._held_anchor
    _last_known = Mover._last_known
    _last_print = Mover._last_print
    _newest_trade = Mover._newest_trade
    _position_average = Mover._position_average
    _entry_trail = Mover._entry_trail
    _cold_plan = Mover._cold_plan
    _floor_m = Mover._floor_m
    _warm_tighten = Mover._warm_tighten
    _tier_applies = Mover._tier_applies
    _top_tier = Mover._top_tier
    _bought = Mover._bought
    _trail_change = Mover._trail_change
    _sale_sent = Mover._sale_sent

    def __init__(self, bid: str, **rows: Any) -> None:
        rows.setdefault("trail_mode", "fixed")
        self.settings = MoverConfig(max_notional=Decimal(50_000), max_move=Decimal(5), **rows)
        self.persistence = self.settings.breach_count > 1 or self.settings.high_count > 1
        self.submit_params = None
        self.winding_down = False
        self.bid = bid
        self.orders: dict[str, SimpleNamespace] = {}
        self.sent: list[SimpleNamespace] = []
        self.canceled: list[str] = []
        self.said: list[str] = []
        self.notes: dict = {}
        self.dropped: list[str] = []
        self.exits: list = []
        self.sale_rows: dict = {}
        self.sale_owner: dict = {}
        self.remakes_under_floor = 0
        self.sell_triggers: dict[str, str] = {}
        self.protection_fallbacks = {"position average price": 0, "current bid": 0}
        self.protect_paths = {"held_stop": 0, "unpriced_exit": 0, "trail": 0}
        self.late_prints: dict[str, dict[str, Any]] = {}
        self.prints_ahead: dict[str, int] = {}
        self.prints_on_receipt: dict[str, int] = {}
        self.quotes = 0
        self.quote_lags: deque[int] = deque(maxlen=101)
        self.quote_lag_least: int | None = None
        self.quote_lag_most: int | None = None
        self.fills: list = []
        self.recent = 0
        self.baseline_dollar_volume_per_minute: dict = {}
        self.positions: list = []
        self.strategy_id = MOVER_ID
        self.cache = SimpleNamespace(
            quote=self._quote, trade=lambda _: None, positions_open=lambda **_: self.positions
        )
        self.order_factory = SimpleNamespace(trailing_stop_limit=self._trailing, limit=self._limit)

    def _quote(self, _instrument_id: InstrumentId) -> SimpleNamespace:
        return SimpleNamespace(bid_price=Price.from_str(self.bid))

    def _order(self, client_order_id: Any) -> SimpleNamespace | None:
        return None if client_order_id is None else self.orders.get(client_order_id.value)

    def _instrument(self, _instrument_id: InstrumentId) -> SimpleNamespace:
        return SimpleNamespace(
            make_qty=lambda q: Quantity.from_str(str(q)),
            make_price=lambda p: Price.from_str(f"{p:.4f}"),
            price_precision=4,
        )

    def _new(self, kind: str, **fields: Any) -> SimpleNamespace:
        cid = SimpleNamespace(value=f"S-{len(self.orders) + 1}")
        working = {"is_open": kind == "LIMIT", "is_emulated": kind == "TRAILING"}
        pending = {"is_pending_cancel": False, "is_pending_update": False}
        return SimpleNamespace(
            client_order_id=cid,
            kind=kind,
            status=None,
            is_closed=False,
            **working,
            **pending,
            **fields,
        )

    def _trailing(self, _id: Any, _side: Any, qty: Any, _price: Any, **kw: Any) -> SimpleNamespace:
        return self._new(
            "TRAILING",
            quantity=qty,
            trigger_price=kw["trigger_price"],
            price=_price,
            trailing_offset=kw["trailing_offset"],
            limit_offset=kw["limit_offset"],
        )

    def _limit(self, _id: Any, _side: Any, qty: Any, price: Any, **_kw: Any) -> SimpleNamespace:
        return self._new("LIMIT", quantity=qty, leaves_qty=qty, price=price, trigger_price=None)

    def submit_order(self, order: SimpleNamespace, params: Any = None) -> None:
        self.orders[order.client_order_id.value] = order
        self.sent.append(order)

    def cancel_order(self, client_order_id: Any) -> None:
        self.canceled.append(client_order_id.value)

    def _say(self, text: str) -> None:
        self.said.append(text)

    def refuse(self, order: SimpleNamespace) -> None:
        """The order as the cache shows one the risk engine denied."""
        order.status = mover.REFUSED[0]
        order.is_closed, order.is_open, order.is_emulated = True, False, False


def seam_slot(seam: Seam, *, bought: str | None = "10.00") -> mover.Slot:
    """A slot of AAA whose entry filled at `bought`; with None, a position the strategy has no entry for."""
    slot = cast(Any, mover.Slot)(AAA, None, None, None)
    if bought is not None:
        slot.entry_id = SimpleNamespace(value="E-1")
        seam.orders["E-1"] = SimpleNamespace(avg_px=float(bought), is_closed=True)
    return slot


def test_a_refused_remake_of_the_trailing_sell_keeps_the_trigger_it_had_reached() -> None:
    """A trailing sell is made again from a trigger of 10.38 and the risk engine denies it. The bid is
    then 10.20, under that trigger: the next step sells by limit. It does not start a new trailing
    sell 0.20 under a bid that is already through the trigger."""
    seam = Seam("10.50")
    slot = seam_slot(seam)
    slot.trail_trigger = Decimal("10.38")
    cast(Any, seam)._protect(slot, Decimal(3), T0)
    remade = seam.sent[-1]
    assert (remade.kind, remade.trigger_price) == ("TRAILING", Price.from_str("10.3800"))
    seam.refuse(remade)
    seam.bid = "10.20"
    cast(Any, seam)._protect(slot, Decimal(3), T0 + 5 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING", "LIMIT"]
    assert slot.exiting


def test_a_position_with_no_entry_of_the_strategys_has_a_sale_from_the_current_bid() -> None:
    """The missing entry must not leave five held shares waiting for wind-down."""
    seam = Seam("10.50")
    slot = seam_slot(seam, bought=None)
    for step in range(3):
        cast(Any, seam)._protect(slot, Decimal(5), T0 + step * SECOND)
    assert len(seam.sent) == 1
    assert seam.sent[0].quantity == Quantity.from_int(5)
    assert seam.sent[0].is_emulated
    assert len(seam.notes[AAA]) == 1
    assert "current bid" in seam.notes[AAA][0]


def test_a_holding_above_the_schemes_top_exits_chases_and_closes_on_the_cent() -> None:
    """The lead's ruling (ii), 2026-11-12: a name bought under $5,000 that trades above the tick
    scheme's last tick (4999.99) is still sold on Alpaca's step there, a cent. Its exit by limit, once
    the reached trigger is met, goes at the bid; its chase floor is `chase_cap` under that, on the cent,
    so a falling bid re-prices the sale; and its close at the wind-down outside the session goes at the
    bid. None is the scheme's last tick or its lowest, and the floor is not the sale's own price."""
    exit_seam = Seam("6000.2370", exit_ticks=0)
    cast(Any, exit_seam).recent = 0
    slot = seam_slot(exit_seam, bought="4990.00")
    slot.trail_trigger = Decimal("6100.00")
    cast(Any, exit_seam)._protect(slot, Decimal(3), T0)
    assert [(o.kind, o.price) for o in exit_seam.sent] == [("LIMIT", Price.from_str("6000.2300"))]

    cast(Any, exit_seam)._protect(slot, Decimal(3), T0 + SECOND)
    assert slot.floor == Decimal("5880.23")  # 6000.23 x 0.98 = 5880.2254, up to the cent
    exit_seam.bid = "5940.2370"
    cast(Any, exit_seam)._protect(slot, Decimal(3), T0 + 6 * SECOND)
    assert exit_seam.canceled == [exit_seam.sent[0].client_order_id.value]

    close_seam = Seam("6000.2370", exit_ticks=0, extended_hours=True)
    close_seam.winding_down = True
    slot = seam_slot(close_seam, bought="4990.00")
    cast(Any, close_seam)._sell_out(slot, Decimal(3), T0)
    assert [(o.kind, o.price) for o in close_seam.sent] == [("LIMIT", Price.from_str("6000.2300"))]


def test_a_config_that_modifies_orders_is_refused_by_name() -> None:
    """No order is modified until the adapter's replace chain has been read twice from outside (ADR
    0088): the row that would turn `modify_order` on is refused, not left to each run's choice."""
    with pytest.raises(ValueError, match="no order is modified"):
        MoverConfig(max_notional=Decimal(500), max_move=Decimal(5), chase="modify")
    code = [f.__code__ for f in vars(Mover).values() if hasattr(f, "__code__")]
    assert code and not any("modify_order" in c.co_names for c in code)


def test_a_refused_sale_in_the_wind_down_outside_the_session_waits_chase_secs() -> None:
    """Outside the session the wind-down sells by limit. A sale the engine denies is sent again after
    `chase_secs`, not at every one-second step."""
    seam = Seam("10.20", extended_hours=True, chase_secs=5)
    seam.winding_down = True
    slot = seam_slot(seam)
    cast(Any, seam)._sell_out(slot, Decimal(3), T0)
    seam.refuse(seam.sent[-1])
    for step in range(1, 5):
        cast(Any, seam)._sell_out(slot, Decimal(3), T0 + step * SECOND)
    assert len(seam.sent) == 1
    cast(Any, seam)._sell_out(slot, Decimal(3), T0 + 5 * SECOND)
    assert len(seam.sent) == 2


def test_the_wind_down_outside_the_session_does_not_wait_on_a_refused_trailing_sell() -> None:
    """The trailing sell was refused a second before the wind-down began. The wind-down's first limit
    goes at once: the wait is for a sale the wind-down itself had sent."""
    seam = Seam("10.50", extended_hours=True, chase_secs=5)
    slot = seam_slot(seam)
    cast(Any, seam)._protect(slot, Decimal(3), T0)
    seam.refuse(seam.sent[-1])
    seam.winding_down = True
    cast(Any, seam)._sell_out(slot, Decimal(3), T0 + SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING", "LIMIT"]


def test_an_entry_whose_cancel_failed_during_the_exit_is_canceled_again_after_chase_secs() -> None:
    """The sale has been released and the entry's cancel was rejected while it still works. The cancel
    is sent again after `chase_secs`, not after the entry's own timeout of 30 seconds."""
    seam = Seam("10.20", chase_secs=5, entry_timeout_secs=30)
    slot = seam_slot(seam)
    slot.exiting = True
    entry = SimpleNamespace(
        client_order_id=SimpleNamespace(value="E-1"),
        ts_init=T0,
        is_open=True,
        is_pending_cancel=False,
    )
    cast(Any, seam)._time_out(slot, entry, T0)
    cast(Any, seam)._time_out(slot, entry, T0 + 4 * SECOND)
    assert seam.canceled == ["E-1"]
    cast(Any, seam)._time_out(slot, entry, T0 + 5 * SECOND)
    assert seam.canceled == ["E-1", "E-1"]


class ReportedAccount:
    """The Alpaca adapter's account as NautilusTrader holds it live, with its calculation off: the free
    cash is the cash Alpaca last reported (the adapter reports cash as total and free, nothing locked,
    `canon_alpaca.execution._report_account`), and it changes only when a report arrives. A report
    asked for lands before the next read, with the backtest's cash then, as a GET /account sent after
    the fill reads it."""

    calculate_account_state = False

    def __init__(self, real) -> None:
        self.real, self.id = real, real.id
        self.cash = real.balance_total(USD)
        self.count, self.asked = 1, 0

    def _land(self) -> None:
        if self.asked:
            self.cash, self.count, self.asked = (
                self.real.balance_total(USD),
                self.count + self.asked,
                0,
            )

    @property
    def event_count(self) -> int:
        self._land()
        return self.count

    def balance_free(self, currency=None):
        self._land()
        return self.cash

    def balance_total(self, currency=None):
        self._land()
        return self.cash

    def balance_locked(self, currency=None):
        return Money(0, USD)


class Reporting(Recording):
    """`Recording` on a `ReportedAccount`; each `query_account` is kept and, with `deliver`, answered
    with a report before the next read."""

    deliver = True

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.reported: ReportedAccount | None = None
        self.queries: list = []

    def _trading_account(self, venue):
        real = super()._trading_account(venue)  # a copy at the pin, so taken again at each read
        if real is not None and self.reported is None:
            self.reported = ReportedAccount(real)
        elif real is not None and self.reported is not None:
            self.reported.real = real
        return self.reported

    def query_account(self, account_id, client_id=None, params=None) -> None:
        self.queries.append(account_id)
        if self.deliver and self.reported is not None:
            self.reported.asked += 1


@pytest.mark.parametrize("deliver", [True, False])
def test_a_reported_account_is_queried_on_each_fill_and_buys_since_its_report_are_subtracted(
    deliver,
):
    """ultra-3a8613a/3, the lead's ruling of 2026-11-12: live, the account is reported with its
    calculation off, so its free cash stood at the connect-time cash all window. Each fill now asks for
    the account (the adapter's `_query_account`, one GET /account), and a buy filled since the last
    report is subtracted from the free cash the next entry is sized from; a sale's proceeds count only
    once a report has them."""
    eng = engine(cash=Decimal(1000))
    s = Reporting(
        MoverConfig(
            max_notional=Decimal(1000),
            max_move=Decimal(5),
            entry_arm="bridge",
            strategy_id=MOVER_ID,
            order_id_tag="001",
            size_from_budget=True,
            loss_share=PAPER_LOSS_SHARE,
            entries=2,
            cooldown_secs=0,
        )
    )
    s.deliver = deliver
    closes = [*RISING[:5], 10.1, 10.0, 9.9, 9.8, *RECLAIM, 10.8]
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    eng.dispose()
    assert s.fills and s.queries
    if deliver:
        assert len(s.queries) <= len(s.fills), "answered at once: at most one account query a fill"
    first = s.entries_sent[0]
    assert first["sizing"]["unreported_fills"] == "0"
    assert first["sizing"]["account_query_wait_secs"] == "0.0"
    bought = [o for o in orders if o.side == OrderSide.BUY and o.filled_qty.is_positive()]
    spent = bought[0].filled_qty.as_decimal() * Decimal(str(bought[0].avg_px))
    assert not any("NOTIONAL_EXCEEDS_FREE_BALANCE" in reason for _, reason in s.refused), s.refused
    if deliver:
        # the report after the sale has the cash the loss left, and nothing is held back
        assert len(bought) == 2
        [second] = [
            e["sizing"]
            for e in s.entries_sent
            if e["client_order_id"] == bought[1].client_order_id.value
        ]
        assert second["unreported_fills"] == "0"
        assert Decimal(second["free_cash"]) < Decimal(1000)
    else:
        # no report came: the first buy is still subtracted and the sale's proceeds do not count
        # yet, so the cash left cannot fund a second entry
        assert len(bought) == 1
        assert all(e["sizing"]["unreported_fills"] == str(spent) for e in s.entries_sent[1:])
        # the lead's bound: no sale waits on the query outstanding; the due sale went out and filled
        sold = [o for o in orders if o.side == OrderSide.SELL and o.filled_qty.is_positive()]
        assert sold and sold[0].ts_init > bought[0].ts_last, (
            "the sale went out with the query unanswered"
        )


def test_a_reported_account_counts_an_accepted_unfilled_buy_against_free_cash() -> None:
    """ultra-3a8613a/3: a reported account locks nothing, so an entry the venue took and has not filled
    is counted against the free cash at its open quantity; a calculated account locks it itself."""
    from nautilus_trader.model import OrderStatus

    def order(status, quantity, leaves, price):
        return SimpleNamespace(
            status=status,
            quantity=Quantity.from_int(quantity),
            leaves_qty=Quantity.from_int(leaves),
            price=Price.from_str(price),
        )

    working = [
        order(OrderStatus.SUBMITTED, 10, 10, "5.00"),
        order(OrderStatus.ACCEPTED, 20, 20, "5.00"),
        order(OrderStatus.PARTIALLY_FILLED, 30, 10, "5.00"),
        order(OrderStatus.FILLED, 40, 0, "5.00"),
    ]
    assert mover.entry_reserve(working, reported=False) == Decimal(50)
    assert mover.entry_reserve(working, reported=True) == Decimal(200)


class OneAccount(Recording):
    """`Recording` with a reported account it reads directly, its queries kept and its clock set."""

    def __init__(self, account) -> None:
        super().__init__(
            MoverConfig(
                max_notional=Decimal(1000),
                max_move=Decimal(5),
                strategy_id=MOVER_ID,
                order_id_tag="001",
            )
        )
        self.account, self.queries, self.now_ns = account, [], 0

    def _trading_account(self, venue):
        return self.account

    def query_account(self, account_id, client_id=None, params=None) -> None:
        self.queries.append(account_id)

    def _now_ns(self) -> int:
        return self.now_ns


def test_a_reported_account_has_one_query_in_flight_and_a_fill_waits_for_a_query_sent_after_it():
    """Review of the composition's fixes, high round, findings 4 and 5: every fill sent its own GET
    /account, partial fills and sales included, against the same request budget the entries are held
    to; and with several queries in flight, an older reply applied last moved the event count past a
    buy its balance did not have. One query is in flight at a time, for every fill since the last."""
    account = SimpleNamespace(calculate_account_state=False, event_count=1, id="A")
    s = OneAccount(account)
    buy, sell = OrderSide.BUY, OrderSide.SELL
    s.fills_to_report = [
        (VENUE, buy, Decimal(100)),
        (VENUE, buy, Decimal(50)),
        (VENUE, sell, Decimal(70)),
    ]
    s._ask_for_the_account()
    assert len(s.queries) == 1, "one query for the step's fills"
    assert s._unreported_buys(account) == Decimal(150)
    s.fills_to_report = [(VENUE, buy, Decimal(30))]
    s._ask_for_the_account()
    assert len(s.queries) == 1, "none while the first is unanswered"
    assert s._unreported_buys(account) == Decimal(180)
    s.now_ns = 2 * mover.SECOND_NS
    assert s._query_wait_secs(account) == 2.0, "what a buy sized now records it waited"
    s.now_ns = 0
    account.event_count = 2  # the first query's report: it was sent before the 30 filled
    assert s._unreported_buys(account) == Decimal(30)
    assert s._query_wait_secs(account) == 0.0
    s._ask_for_the_account()
    assert len(s.queries) == 2
    account.event_count = 3
    assert s._unreported_buys(account) == Decimal(0)
    # a query never answered is asked again once the adapter's longest call has passed
    s.fills_to_report = [(VENUE, buy, Decimal(10))]
    s._ask_for_the_account()
    assert len(s.queries) == 3
    s.fills_to_report = [(VENUE, buy, Decimal(5))]
    s.now_ns = (mover.QUERY_WAIT_SECS - 1) * mover.SECOND_NS
    s._ask_for_the_account()
    assert len(s.queries) == 3
    s.now_ns = mover.QUERY_WAIT_SECS * mover.SECOND_NS
    s._ask_for_the_account()
    assert len(s.queries) == 4
    assert s._unreported_buys(account) == Decimal(15)


def test_a_lost_account_query_is_sent_again_on_expiry_without_another_fill():
    """Second read of the high round (53269f4): a query that is lost (the adapter's `_query_account`
    fails and reports nothing) was sent again only when another fill came, so with a position held and
    no other fill every later buy was sized with that buy still held back until the position's own sale.
    It is sent again once `QUERY_WAIT_SECS` has passed, fill or none."""
    account = SimpleNamespace(calculate_account_state=False, event_count=1, id="A")
    s = OneAccount(account)
    s.fills_to_report = [(VENUE, OrderSide.BUY, Decimal(500))]
    s._ask_for_the_account()
    assert len(s.queries) == 1
    s.now_ns = (mover.QUERY_WAIT_SECS - 1) * mover.SECOND_NS
    s._ask_for_the_account()
    assert len(s.queries) == 1, "not before it has had its time"
    s.now_ns = mover.QUERY_WAIT_SECS * mover.SECOND_NS
    s._ask_for_the_account()
    assert len(s.queries) == 2, "sent again with no other fill"
    assert s._unreported_buys(account) == Decimal(500)
    account.event_count = 2  # the second query's report
    assert s._unreported_buys(account) == Decimal(0)
    s.now_ns = 10 * mover.QUERY_WAIT_SECS * mover.SECOND_NS
    s._ask_for_the_account()
    assert len(s.queries) == 2, "an answered query is not sent again"


class StoppedMidway(Recording):
    """`Recording` that asks for the strategy's stop at `stop_at`, as a window's end does in the session."""

    stop_at: datetime | None = None
    stop_ns = 0
    managed_exiting = 0

    def _manage(self, slot, now: int) -> None:
        self.managed_exiting += self.is_exiting()
        super()._manage(slot, now)

    def on_start(self) -> None:
        super().on_start()
        if self.stop_at is not None:
            self.clock.set_time_alert("test-stop", self.stop_at, self._request_stop)

    def _request_stop(self, event: TimeEvent) -> None:
        self.stop_ns = event.ts_event
        self.stop()


def test_no_trailing_sell_is_remade_while_upstreams_managed_stop_exits_in_the_session() -> None:
    """code-review-high-d04/5: a stop asked for in the session, before the wind-down, runs upstream's
    market exit, which cancels the emulated trailing sell. While that exit is unfilled the step must not
    take the canceled sell for one to remake: upstream denies such an order (`MARKET_EXIT_IN_PROGRESS`,
    crates/trading/src/strategy/mod.rs:2396-2402 at the pin), so making it only adds a denied order each
    `chase_secs`. The step still manages each slot meanwhile, since upstream's exit never cancels an
    order a second time and the step's own cancels must go on."""
    eng = engine(liquid=True)
    s = StoppedMidway(
        MoverConfig(
            max_notional=Decimal(50_000),
            max_move=Decimal(5),
            entry_arm="bridge",
            strategy_id=MOVER_ID,
            order_id_tag="001",
            manage_stop=True,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
        )
    )
    after = T0 + 7 * MINUTE
    s.stop_at = datetime.fromtimestamp((after + 200_000_000) / 1e9, tz=UTC)
    # no bid to sell into for four seconds after the stop, then one
    unfilled = [
        QuoteTick(
            AAA,
            Price(10.59, 4),
            Price(10.61, 4),
            Quantity.from_int(0),
            Quantity.from_int(10_000),
            after + 100_000_000 + i * 250_000_000,
            after + 100_000_000 + i * 250_000_000,
        )
        for i in range(17)
    ]
    sized = QuoteTick(
        AAA,
        Price(10.59, 4),
        Price(10.61, 4),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        after + 6_000_000_000,
        after + 6_000_000_000,
    )
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING[:7]), [*unfilled, sized])
    flat = not eng.cache.positions_open(strategy_id=MOVER_ID)
    eng.dispose()
    # upstream's market exit sends its MARKET sell again after each refusal; nothing else may follow
    sent_after = {
        f"{o.side.name} {o.order_type.name} {list(o.tags or [])}"
        for o in orders
        if o.ts_init >= s.stop_ns
    }
    # the case needs a trailing sell still emulated when the stop is asked for, canceled by the exit
    working_at_stop = [
        o
        for o in orders
        if o.order_type.name == "TRAILING_STOP_LIMIT"
        and o.ts_init < s.stop_ns <= o.ts_last
        and o.status.name == "CANCELED"
    ]
    assert working_at_stop
    assert s.managed_exiting
    assert s.stop_ns
    assert sent_after == {"SELL MARKET ['MARKET_EXIT']"}
    assert flat


def test_a_name_sold_while_unranked_keeps_its_sale_state() -> None:
    """AAA leaves the ranking while it is held, and its sale fills between two steps. The slot is not
    closed before the sale is recorded, so a later reclaim still names that sale."""
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=3600, watch=1)
    closes = [*RISING, *FALL, *RECLAIM]
    ranking = ranker(rank((AAA, "0.25")), values={7: rank((BBB, "0.3")), 11: rank((AAA, "0.25"))})
    orders = run(eng, s, ranking, tape(AAA, closes), tape(BBB, [20.0] * len(closes)))
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [AAA, AAA]
    assert Decimal(s.record()["entries"][1]["reentry"]["sale_price"]) == Decimal("10.45")


def test_after_an_exit_it_buys_back_on_a_reclaim_while_the_name_ranks() -> None:
    """Sold at 10.45 after the fall. A new rising run that closes above the sale's
    price is a reclaim: a second buy."""
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=60)
    closes = RISING + FALL + RECLAIM
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    second_close = (buys[1].ts_init - T0) // MINUTE if len(buys) > 1 else None
    eng.dispose()
    assert len(buys) == 2
    # the run must end above the sale's price: 10.50 is the first close of the new leg that does
    assert second_close == len(RISING + FALL) + 6


def test_legacy_cooldown_does_not_delay_reclaim_but_sale_price_still_binds() -> None:
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=3600)
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + RECLAIM))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 2

    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=60)
    below = [9.80, 9.85, 9.90, 9.95, 10.00, 10.05]  # rising, and never back above the sale
    orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING + FALL + below))
    buys = [o for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert len(buys) == 1


def test_a_name_unwatched_after_its_sale_keeps_its_sale_price_for_state_reentry() -> None:
    """Codex's finding P2-5 at 660b37e: AAA is sold, leaves the ranking for a minute (its slot is
    closed) and returns; the sale's price belongs to the name and still binds its reclaim, while
    the legacy cooldown value imposes no clock wait."""
    eng = engine()
    s = strategy(reentry_arm="reclaim", cooldown_secs=3600, watch=1)
    closes = [*RISING, *FALL, *RECLAIM]
    ranking = ranker(rank((AAA, "0.25")), values={12: rank((BBB, "0.3")), 13: rank((AAA, "0.25"))})
    orders = run(eng, s, ranking, tape(AAA, closes), tape(BBB, [20.0] * len(closes)))
    bought = [o.instrument_id for o in orders if o.side == OrderSide.BUY]
    eng.dispose()
    assert bought == [AAA, AAA]
    assert Decimal(s.record()["entries"][1]["reentry"]["sale_price"]) == Decimal("10.45")
