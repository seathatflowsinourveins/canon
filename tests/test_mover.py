"""The mover strategy offline: `canon_trading.mover.Mover` through a real `BacktestEngine` (ADR 0088).

No test here opens a network connection or reads a credential. Bars and quotes are generated in this
file: each minute a bar, and a quote a nanosecond before the bar closes, a cent either side of its close,
so a decision taken on a bar finds the quote of that close in the cache. The simulated venue fills on
quotes only (`bar_execution=False`): with bars and quotes together it also fills a resting limit from the
bar's earlier path (measured at the pin, ADR 0088). The ranking comes from a test component that publishes
the `movers-rank` signal each minute, as the ranking actor will (ADR 0087): nothing of a vendor's list
reaches the strategy.
"""

from __future__ import annotations

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
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
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

from canon_trading import mover
from canon_trading.mover import EXTENDED_HOURS
from canon_trading.mover import RANK_SIGNAL
from canon_trading.mover import Mover
from canon_trading.mover import MoverConfig
from canon_trading.mover import format_rank
from canon_trading.mover import tick
from canon_trading.mover_window import OUTER_BOUND_USD
from canon_trading.mover_window import max_shares
from canon_trading.mover_window import mover_parts
from canon_trading.movers_rank_signal import SIGNAL_NAME
from canon_trading.movers_rank_signal import RankEntry
from canon_trading.movers_rank_signal import decode_ranking
from canon_trading.movers_rank_signal import encode_ranking

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


def engine(*, order_cap: Decimal | None = None, liquid: bool = False) -> BacktestEngine:
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
        [Money(100_000, USD)],
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
        "strategy_id": MOVER_ID,
        "order_id_tag": "001",
    }
    settings.update(changes)
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
                name=SIGNAL_NAME, value=encode_ranking([RankEntry(AAA, 0.25)]), ts_event=5
            ),
        )
    )
    s.on_signal(cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=bad, ts_event=6)))
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
                name=SIGNAL_NAME, value=encode_ranking([RankEntry(AAA, 0.25)]), ts_event=5
            ),
        )
    )
    s.on_signal(cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=bad, ts_event=6)))
    assert s.rank == []
    assert s.rank_faults == 1 and s.rank_ns == 5


def test_the_ranking_signal_round_trips_and_a_malformed_one_is_refused() -> None:
    value = rank((AAA, "0.25"), (BBB, "0.1"))
    assert value == "AAA.ALPACA=0.25;BBB.ALPACA=0.1"
    s = strategy()

    def hear(value: str) -> None:
        s.on_signal(cast(Any, SimpleNamespace(name=SIGNAL_NAME, value=value, ts_event=5)))

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
    assert mover.entry_price(Decimal("0.9999"), 2) == Decimal("1.01")
    assert mover.exit_price(Decimal("1.00"), 1, None) == Decimal("0.9999")
    assert mover.exit_price(Decimal("10.29"), 1, None) == Decimal("10.28")


# --- findings of the review before landing (`/code-review`, 2026-11-11) --------------------------------


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
    assert any("no limit price" in why for why in s.dropped)


def test_a_malformed_and_a_good_ranking_between_two_steps_are_both_said() -> None:
    said: list[str] = []
    stand_in = cast(Any, Borrowed())
    stand_in.rank_said = None
    stand_in._say = said.append

    def hear(value: str, at: int) -> None:
        Mover.on_signal(
            stand_in, cast(Any, SimpleNamespace(name=RANK_SIGNAL, value=value, ts_event=at))
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
    stale by the last trade too: a bid at a dollar over a last trade under it changes nothing."""
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
        self.refused: list = []
        self.rank: list = []
        self.rank_ns = 0
        self.rank_faults = 0
        self.quotes = 0
        self.trades = 0

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
            SimpleNamespace(name=RANK_SIGNAL, value=rank((AAA, "0.25")), ts_event=5)
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
    the largest entry (500 // 10.41 = 48 shares), holds it under the trailing sell while the name
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
    assert summary[0] == ("BUY", "LIMIT", "FILLED") and bought == Decimal(48)
    assert [side for side, *_ in summary].count("BUY") == 1
    assert summary[-1] == ("SELL", "MARKET", "FILLED") and flat
    assert s.refused == [] and cap.tripped is None
    assert s.live_bars > 0


def test_a_windows_rows_are_the_first_runs() -> None:
    s, _, cap = window_parts(price=Decimal("2.40"))
    rows = s.settings
    assert (rows.max_notional, rows.slots, rows.watch, rows.entries) == (Decimal(500), 1, 1, 1)
    assert OUTER_BOUND_USD == Decimal(5000)
    assert rows.max_shares == max_shares(Decimal("2.40")) == 208
    assert cap.max_position == Decimal(208)
    assert rows.bar_spec == "1-MINUTE-LAST-INTERNAL"
    assert rows.chase == "resend"  # no order of a mover node is modified at the venue
    assert rows.rank_max_age_secs == 23 * 60  # from the choice to the window's end
    assert rows.manage_stop is True and rows.extended_hours is False
    outside, _, _ = window_parts(extended=True)
    assert outside.settings.manage_stop is False and outside.settings.extended_hours is True


def test_a_name_whose_one_share_is_over_the_largest_entry_is_refused_before_the_node_is_built() -> (
    None
):
    with pytest.raises(ValueError, match="over the largest entry"):
        window_parts(price=Decimal("500.01"))


# --- states the simulated venue does not reach: the sale path's own methods on a stand-in -----------------


class Seam:
    """Stands in for a `Mover` in one step of its sale path, for states a second reader named that the
    simulated venue does not produce (a refused remake, a cancel that failed, a position with no entry
    of the strategy's): the orders by client order ID, one bid, and what the step sent. Every method
    under test is `Mover`'s own."""

    _protect = Mover._protect
    _trail = Mover._trail
    _followed = Mover._followed
    _limit_sell = Mover._limit_sell
    _sell_out = Mover._sell_out
    _time_out = Mover._time_out
    _chase = Mover._chase
    _note = Mover._note

    def __init__(self, bid: str, **rows: Any) -> None:
        self.settings = MoverConfig(max_notional=Decimal(50_000), max_move=Decimal(5), **rows)
        self.submit_params = None
        self.winding_down = False
        self.bid = bid
        self.orders: dict[str, SimpleNamespace] = {}
        self.sent: list[SimpleNamespace] = []
        self.canceled: list[str] = []
        self.said: list[str] = []
        self.notes: dict = {}
        self.dropped: list[str] = []
        self.cache = SimpleNamespace(quote=self._quote)
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
        return self._new("TRAILING", quantity=qty, trigger_price=kw["trigger_price"], price=None)

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


def test_a_position_with_no_entry_of_the_strategys_is_named_as_unprotected() -> None:
    """Shares held in a watched name with no entry order of this strategy have no price to size a
    trailing sell from. Nothing is sent, and the run's record says so, once."""
    seam = Seam("10.50")
    slot = seam_slot(seam, bought=None)
    for step in range(3):
        cast(Any, seam)._protect(slot, Decimal(5), T0 + step * SECOND)
    assert seam.sent == []
    assert len(seam.notes[AAA]) == 1
    assert "no filled entry of this strategy" in seam.notes[AAA][0]


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
