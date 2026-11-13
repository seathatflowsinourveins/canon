"""The initial unpriced hold lasts 60 host-clock seconds despite venue clock skew.

Contracts: NautilusTrader d53849b216e2 common/__init__.pyi Clock.set_time/timestamp_ns,
model/__init__.pyi OrderFilled.ts_event/ts_init and QuoteTick, and Strategy.on_order_filled.
The in-memory BacktestEngine supplies the same test clock and order factory used by
test_mover; only the cached entry, quote view and submission transport are stand-ins.
No timer state is seeded: the real fill handler and protection path start the hold.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountId
from nautilus_trader.model import AccountType
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
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

from canon_trading.mover import HOLD_MOST_SECS
from canon_trading.mover import Mover
from canon_trading.mover import MoverConfig
from canon_trading.mover import Slot

SECOND = 1_000_000_000
START = 1_762_790_400_000_000_000  # test_mover's T0; synthetic 2025-11-10T16:00Z.
INSTRUMENT = InstrumentId.from_str("AAA.ALPACA")
ENTRY = ClientOrderId("blind-hold-entry")
HELD = Decimal(5)
USD = Currency.from_str("USD")


class QuoteView:
    """Supply a missing/one-sided quote while preserving the engine's other cache reads."""

    def __init__(self, native: Any, quote: QuoteTick | None) -> None:
        self.native = native
        self.latest = quote

    def quote(self, instrument_id: InstrumentId) -> QuoteTick | None:
        assert instrument_id == INSTRUMENT
        return self.latest

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)


class HoldProbe(Mover):
    latest_quote: QuoteTick | None = None

    @property
    def cache(self) -> Any:
        return QuoteView(super().cache, self.latest_quote)


@pytest.fixture(params=["missing", "zero-bid"])
def holding(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[HoldProbe, Slot, list[Any]]]:
    """An untrailed five-share fill at 10, with no trade/close to trigger the held stop."""
    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("TESTER-001")))
    try:
        engine.add_venue(
            Venue("ALPACA"),
            OmsType.NETTING,
            AccountType.CASH,
            [Money(100_000, USD)],
            fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        )
        engine.add_instrument(
            Equity(
                instrument_id=INSTRUMENT,
                raw_symbol=Symbol("AAA"),
                currency=USD,
                price_precision=4,
                price_increment=Price.from_str("0.0001"),
                ts_event=0,
                ts_init=0,
            )
        )
        probe = HoldProbe(
            MoverConfig(
                max_notional=Decimal(50_000),
                max_move=Decimal(5),
                strategy_id=StrategyId("Mover-001"),
                order_id_tag="001",
                trail_mode="fixed",
                trigger=TriggerType.BID_ASK,
                breach_count=1,
                high_count=1,
            )
        )
        engine.add_strategy(probe)
        probe.clock.set_time(START)
        if request.param == "zero-bid":
            probe.latest_quote = QuoteTick(
                INSTRUMENT,
                Price.from_str("0.0000"),
                Price.from_str("10.0100"),
                Quantity.from_int(0),
                Quantity.from_int(100),
                START,
                START,
            )
        slot = Slot(
            INSTRUMENT,
            BarType.from_str(f"{INSTRUMENT}-1-MINUTE-LAST-EXTERNAL"),
            deque(maxlen=4),
            deque(maxlen=5),
        )
        slot.entry_id = ENTRY
        slot.entry_record = {"client_order_id": ENTRY.value, "stopped_out": False}
        probe.slots[INSTRUMENT] = slot
        entry = SimpleNamespace(
            client_order_id=ENTRY,
            avg_px=10.0,
            price=Price.from_str("10.0000"),
            is_closed=True,
        )
        monkeypatch.setattr(probe, "_order", lambda cid: entry if cid == ENTRY else None)
        sent: list[Any] = []

        def capture(order: Any, **_kwargs: Any) -> None:
            sent.append(order)

        monkeypatch.setattr(probe, "submit_order", capture)
        yield probe, slot, sent
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "host_offset_secs",
    [
        pytest.param(60, id="host-ahead-60s"),
        pytest.param(-60, id="host-behind-60s"),
        pytest.param(6, id="host-ahead-6s"),
        pytest.param(-6, id="host-behind-6s"),
        pytest.param(1, id="host-ahead-1s"),
        pytest.param(-1, id="host-behind-1s"),
        pytest.param(0, id="no-offset-control"),
    ],
)
def test_unpriced_exit_uses_60_seconds_on_one_clock(
    holding: tuple[HoldProbe, Slot, list[Any]], host_offset_secs: int
) -> None:
    probe, slot, sent = holding
    assert HOLD_MOST_SECS == 60
    fill = OrderFilled(
        trader_id=TraderId("TESTER-001"),
        strategy_id=StrategyId("Mover-001"),
        instrument_id=INSTRUMENT,
        client_order_id=ENTRY,
        venue_order_id=VenueOrderId("synthetic-entry"),
        account_id=AccountId("ALPACA-SYNTHETIC"),
        trade_id=TradeId("synthetic-fill"),
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        last_qty=Quantity.from_int(5),
        last_px=Price.from_str("10.0000"),
        currency=USD,
        liquidity_side=LiquiditySide.TAKER,
        event_id=UUID4(),
        ts_event=START - host_offset_secs * SECOND,
        ts_init=START,
        reconciliation=False,
    )
    assert fill.ts_init == probe.clock.timestamp_ns()
    assert fill.ts_init - fill.ts_event == host_offset_secs * SECOND
    probe.on_order_filled(fill)

    # Check every host second, plus one nanosecond before expiry. Venue time stays
    # in the fill; elapsed time comes only from the engine's actual test clock.
    for elapsed_ns in [*(second * SECOND for second in range(60)), 60 * SECOND - 1]:
        probe.clock.set_time(START + elapsed_ns)
        probe._protect(slot, HELD, probe.clock.timestamp_ns())
        assert not sent and not slot.exiting, (
            f"host offset {host_offset_secs:+d}s: exited after {elapsed_ns / SECOND}s "
            "of hold; the bound is 60s"
        )

    deadline = START + 60 * SECOND
    probe.clock.set_time(deadline)
    probe._protect(slot, HELD, probe.clock.timestamp_ns())
    assert len(sent) == 1, (
        f"host offset {host_offset_secs:+d}s: expected the unpriced exit at 60s, "
        f"got {len(sent)} submissions (exiting={slot.exiting})"
    )
    sale = sent[0]
    assert sale.ts_init == deadline
    assert sale.side == OrderSide.SELL
    assert sale.order_type == OrderType.LIMIT
    assert sale.time_in_force == TimeInForce.DAY
    assert sale.quantity.as_decimal() == HELD
    assert slot.exiting
    assert slot.exit_record is not None
    assert slot.exit_record.trigger_reason == "unpriced_exit"
    assert slot.exit_record.trigger_ns == deadline
    assert slot.entry_record is not None
    assert slot.entry_record["protect_path"] == "unpriced_exit"
    assert slot.entry_record["held_stop_secs"] == 60
    assert probe.protect_paths["unpriced_exit"] == 1
