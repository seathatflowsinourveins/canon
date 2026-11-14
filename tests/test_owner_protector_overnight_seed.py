"""Frozen overnight brief: delay BOATS history, arm cold, then warm without a coverage gap.

All books and engine reports are in pytest's temporary tree. The engine uses its own cache,
emulator and simulated exchange; only historical transport is answered from synthetic inputs.
Contracts: NT d53849b216e2 trading/__init__.pyi:963-1002 (history requests),
backtest/__init__.pyi:502-535 (run and reports), common/__init__.pyi:185-224 (logging).
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LoggerConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue

from canon_trading import mover
from canon_trading import protect
from canon_trading.owner_protector import OwnerProtector
from canon_trading.owner_protector import OwnerProtectorConfig
from canon_trading.price_step import up_on_step
from tests.test_owner_protector import AAA
from tests.test_owner_protector import EXTERNAL
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import USD
from tests.test_owner_protector import Harness
from tests.test_owner_protector import Owner
from tests.test_owner_protector import VenueOrders
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture
from tests.test_owner_protector import equity
from tests.test_owner_protector import isolated_level_books  # noqa: F401 -- temp state only
from tests.test_owner_protector import quote
from tests.test_owner_protector import restart_record
from tests.test_owner_protector_cold_start import Cold
from tests.test_owner_protector_cold_start import bar
from tests.test_owner_protector_cold_start import said

OPEN = int(datetime(2026, 11, 16, 1, tzinfo=UTC).timestamp()) * SECOND
START = OPEN + 30 * SECOND  # Sunday 20:00:30 New York, before the first complete minute.


def overnight(**settings: Any) -> Cold:
    strategy = Cold(
        feed="boats",
        session_part="overnight",
        trading=VenueOrders(asset={"overnight_tradable": True}),
        **settings,
    )
    strategy.test_clock.set_time(START)
    strategy.cached.held[0].ts_opened = OPEN - 3600 * SECOND
    return strategy


@pytest.mark.parametrize("feed", ["boats", "sip"])
def test_history_end_uses_the_movers_overnight_delay_only_on_boats(feed: str) -> None:
    strategy = Cold(feed=feed)
    strategy.test_clock.set_time(START)
    strategy.on_start()
    expected = datetime.fromtimestamp(START / SECOND, UTC)
    if feed == mover.OVERNIGHT_HISTORY_FEED:
        expected -= mover.OVERNIGHT_HISTORY_DELAY
    assert strategy.bar_requests[0]["end"] == expected
    assert strategy.bar_requests[0]["params"] == {"feed": feed}


@pytest.mark.parametrize("answer", ["pending", "empty", "refused"])
@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
@pytest.mark.parametrize("ask", ["10.00", "10.02", "10.05", "10.20"])
def test_usable_quote_arms_with_the_shared_cold_rule_without_bars(
    answer: str, row: str, ask: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    distance, expected = protect.distance_for("AAA", row, Decimal(10), Decimal(ask), [])
    planner = protect.distance_for
    calls = []

    def observed(*args, **kwargs):
        calls.append(args)
        return planner(*args, **kwargs)

    monkeypatch.setattr(protect, "distance_for", observed)
    strategy = overnight(row=row)
    strategy.on_start()
    if answer != "pending":
        strategy.answer("bars", [], "synthetic refusal" if answer == "refused" else None)
    strategy.seed(ask=ask, age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level is not None, strategy.said
    assert slot.distance is not None
    assert slot.distance.distance == distance
    assert slot.level.as_decimal() == up_on_step(Decimal(10) - distance)
    assert slot.distance.fallback == expected.fallback
    assert calls == [("AAA", row, Decimal(10), Decimal(ask), [])]
    first = next(line for line in strategy.said if "self-armed" in line)
    assert "cold rule" in first and "fixed fallback" in first, strategy.said
    assert said(strategy, "armed distance", slot.distance.source()), strategy.said
    assert not strategy.sent, "the seed must not spend the observation guard"
    strategy.observe("10.00", START + SECOND)
    assert not strategy.sent, "the first new observation is guarded"
    strategy.observe("10.00", START + 2 * SECOND)
    assert len(strategy.sent) == 1
    assert strategy.sent[0].quantity == Quantity.from_int(10)
    assert strategy.sent[0].trailing_offset == distance


@pytest.mark.parametrize("answer", ["empty", "refused"])
@pytest.mark.parametrize("width", ["0.02", "0.40"])
def test_bars_replan_a_cold_holding_without_lowering_its_level(answer: str, width: str) -> None:
    strategy = overnight()
    strategy.on_start()
    if answer != "pending":
        strategy.answer("bars", [], "synthetic refusal" if answer == "refused" else None)
    strategy.seed(ask="10.05", age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level is not None, strategy.said
    initial = slot.level
    assert slot.distance is not None
    initial_distance = slot.distance.distance
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    # A narrower new quote cannot re-plan a cold distance by itself.
    strategy.seed(ask="10.02", age_s=0)
    assert slot.distance.distance == initial_distance
    strategy.on_historical_bars([bar(OPEN, width)])
    strategy.step()
    assert slot.level is not None and slot.level.as_decimal() >= initial.as_decimal()
    assert slot.distance is not None and slot.distance.bars == 1
    if width == "0.02":
        assert slot.distance.distance < initial_distance
        assert slot.level.as_decimal() > initial.as_decimal()
    else:
        assert slot.distance.distance > initial_distance
        assert slot.level == initial
    assert said(strategy, "replanned", "ATR over 1 bars"), strategy.said
    assert not strategy.sent
    assert not strategy.canceled


def test_warming_keeps_the_working_sell_and_never_sends_more_than_held() -> None:
    strategy = overnight()
    strategy.on_start()
    strategy.answer("bars", [])
    strategy.seed(ask="10.05", age_s=0)
    strategy.observe("10.00", START + SECOND)
    strategy.observe("10.00", START + 2 * SECOND)
    assert len(strategy.sent) == 1
    working = strategy.sent[0]
    before = working.trigger_price
    strategy.cached.working = [
        SimpleNamespace(
            **{
                name: getattr(working, name)
                for name in (
                    "instrument_id",
                    "strategy_id",
                    "client_order_id",
                    "side",
                    "order_type",
                    "quantity",
                    "filled_qty",
                    "leaves_qty",
                    "is_quote_quantity",
                    "contingency_type",
                    "linked_order_ids",
                    "price",
                    "limit_offset",
                    "trailing_offset",
                )
            },
            status=OrderStatus.EMULATED,
            trigger_price=before,
            is_closed=False,
        )
    ]
    changes = []
    strategy.modify_order = lambda *args, **kwargs: changes.append((args, kwargs))
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    strategy.on_historical_bars([bar(OPEN, "0.02")])
    strategy.step()
    assert said(strategy, "replanned", "ATR over 1 bars"), strategy.said
    assert not strategy.canceled, "history must never cancel a working protective sell"
    assert len(strategy.sent) == 1
    level = strategy.slots[AAA].level
    assert level is not None and level.as_decimal() >= before.as_decimal()
    assert sum(o.quantity.as_decimal() for o in strategy.sent) == Decimal(10)


@pytest.mark.parametrize("answer", ["pending", "empty", "refused"])
def test_cold_and_warm_plans_keep_a_carried_distance_and_level(tmp_path: Path, answer: str) -> None:
    path = tmp_path / "levels.json"
    record = restart_record(
        last_level="9.50",
        distance="0.25",
        protection_start_ns=OPEN - 3600 * SECOND,
        written_ns=OPEN - 60 * SECOND,
    )
    record["limit_offset"] = "0.30"
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    strategy = overnight(account_label="shape", level_book_path=path)
    strategy.on_start()
    if answer != "pending":
        strategy.answer("bars", [], "synthetic refusal" if answer == "refused" else None)
    strategy.seed(ask="10.05", age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str("9.50"), strategy.said
    assert slot.distance is not None
    assert slot.distance.distance == Decimal("0.25")
    assert not slot.distance.fallback
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    strategy.seed(ask="10.02", age_s=0)
    strategy.on_historical_bars([bar(OPEN, "0.02")])
    strategy.step()
    assert slot.level == Price.from_str("9.50"), strategy.said
    assert slot.distance.distance == Decimal("0.25")
    assert slot.limit_offset == Decimal("0.30")
    assert not strategy.sent and not strategy.canceled


@pytest.mark.parametrize("answer", ["pending", "empty", "refused"])
@pytest.mark.parametrize("bid", ["9.00", "10.00"], ids=["under-carried", "over-carried"])
@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
def test_carried_level_without_distance_uses_cold_rule_at_once_then_replans(
    tmp_path: Path, answer: str, bid: str, row: str
) -> None:
    path = tmp_path / "levels.json"
    record = restart_record(
        last_level="9.50",
        distance=None,
        protection_start_ns=OPEN - 3600 * SECOND,
        written_ns=OPEN - 60 * SECOND,
    )
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    strategy = overnight(account_label="shape", level_book_path=path, row=row)
    strategy.on_start()
    if answer != "pending":
        strategy.answer("bars", [], "synthetic refusal" if answer == "refused" else None)
    ask = str(Decimal(bid) + Decimal("0.02"))
    distance, expected = protect.distance_for("AAA", row, Decimal(bid), Decimal(ask), [])
    strategy.seed(bid, ask=ask, age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level is not None, strategy.said
    initial = max(Decimal("9.50"), up_on_step(Decimal(bid) - distance))
    assert slot.level.as_decimal() == initial
    assert slot.distance is not None and slot.distance.distance == distance
    assert slot.distance.fallback == expected.fallback
    assert said(strategy, "carried level", "no carried distance", "cold rule", "when bars exist"), (
        strategy.said
    )
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    strategy.seed(bid, ask=ask, age_s=0)
    bars = [bar(OPEN, "0.02")]
    warm_distance, warm = protect.distance_for("AAA", row, Decimal(bid), Decimal(ask), bars)
    if answer == "pending":  # the day's answer, not a bar ahead of it (code-review-high/1)
        strategy.answer("bars", bars)
    else:
        strategy.on_historical_bars(bars)
    strategy.step()
    assert slot.level is not None
    assert slot.level.as_decimal() == max(initial, up_on_step(Decimal(bid) - warm_distance))
    assert slot.distance is not None and slot.distance.distance == warm_distance
    assert slot.distance.bars == warm.bars == 1
    assert not slot.distance.fallback
    assert said(strategy, "replanned", slot.distance.source()), strategy.said
    assert not strategy.sent and not strategy.canceled


class EngineCold(OwnerProtector):
    """Real strategy; the historical transport alone has an empty/refused synthetic answer."""

    request_quotes = Harness.request_quotes

    def __init__(self, config: OwnerProtectorConfig) -> None:
        super().__init__(config)
        self.lines: list[str] = []
        self.refuse_history = False

    def _say(self, text: str) -> None:
        self.lines.append(text)
        super()._say(text)

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ) -> str:
        request_id = "synthetic-bars"
        if self.refuse_history:
            self.on_data_response(
                SimpleNamespace(correlation_id=request_id, failure_reason="synthetic refusal")
            )
        else:
            self.on_historical_bars([])
        if callback is not None:
            callback(request_id)
        return request_id


@pytest.mark.parametrize("refused", [False, True], ids=["first-minute-empty", "bars-refused"])
def test_engine_cold_first_arm_and_no_handler_error(
    refused: bool, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            bypass_logging=False,
            logging=LoggerConfig(is_colored=False, buffered_stdout=False),
        )
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
    protector = EngineCold(
        OwnerProtectorConfig(
            strategy_id=PROTECTOR,
            row="OWN1",
            feed="boats",
            session_part="overnight",
            trading=VenueOrders(asset={"overnight_tradable": True}),
        )
    )
    protector.refuse_history = refused
    engine.add_strategy(Owner())
    engine.add_strategy(protector)
    engine.add_data(
        [
            quote(bid, START + n * SECOND)
            for n, bid in enumerate(["10.00", "10.00", "9.95", "9.94", "9.93", "9.93"])
        ]
    )
    try:
        engine.run()
        reports = {
            "orders": engine.generate_orders_report(),
            "fills": engine.generate_order_fills_report(),
            "positions": engine.generate_positions_report(),
        }
        for name, report in reports.items():
            (tmp_path / f"{name}.txt").write_text(report.to_string(index=False))
        (tmp_path / "arm-lines.txt").write_text("\n".join(protector.lines))
        positions = engine.cache.positions(strategy_id=EXTERNAL)
        assert len(positions) == 1 and positions[0].quantity == Quantity.from_int(10)
    finally:
        engine.dispose()
    captured = capfd.readouterr()
    engine_log = captured.out + captured.err
    (tmp_path / "engine.log").write_text(engine_log)
    assert engine_log, "the engine log must be enabled, never bypassed"
    assert not re.search(r"Python .*? failed", engine_log), engine_log
    arms = [line for line in protector.lines if "armed distance" in line or "self-armed" in line]
    assert arms and "cold rule" in arms[0] and "fixed fallback" in arms[0], protector.lines
    distance, _ = protect.distance_for("AAA", "OWN1", Decimal("10.00"), Decimal("10.02"), [])
    assert any(f"armed distance {distance}" in line for line in arms)


def carried_without_distance(tmp_path: Path, row: str = "OWN1") -> Cold:
    path = tmp_path / "levels.json"
    record = restart_record(
        last_level="9.50",
        distance=None,
        protection_start_ns=OPEN - 3600 * SECOND,
        written_ns=OPEN - 60 * SECOND,
    )
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    return overnight(account_label="shape", level_book_path=path, row=row)


def test_a_pending_history_keeps_the_cold_plan_until_the_answer_not_one_live_bar() -> None:
    """code-review-high/1: while the session day's bars answer is pending, one bar the live
    subscription cached does not fix the run's distance; the answer does."""
    strategy = overnight()
    strategy.on_start()
    strategy.seed(ask="10.05", age_s=0)
    slot = strategy.slots[AAA]
    assert slot.cold and slot.distance is not None, strategy.said
    cold_distance = slot.distance.distance
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    strategy.cached.history = [bar(OPEN, "0.02")]
    strategy.seed(ask="10.05", age_s=0)
    strategy.step()
    assert slot.cold, "one live bar while the history is pending must not end the cold plan"
    assert slot.distance.distance == cold_distance
    n = protect.ATR_BARS
    history = [bar(OPEN - (n - i) * 60 * SECOND, "0.02") for i in range(n)]
    strategy.answer("bars", history)
    strategy.step()
    assert not slot.cold, strategy.said
    assert slot.distance.bars == protect.ATR_BARS, strategy.said


def test_a_carried_long_without_distance_on_flat_bars_with_no_spread_is_covered(
    tmp_path: Path,
) -> None:
    """code-review-high/3: flat bars and no usable spread give the same fixed fallback as no
    bars; a carried long with no carried distance takes it, never left bare (the lead's ruling)."""
    strategy = carried_without_distance(tmp_path)
    strategy.on_start()
    strategy.test_clock.set_time(OPEN + 61 * SECOND)
    strategy.answer("bars", [bar(OPEN, "0")])
    before = len(strategy.said)
    strategy.seed("10.00", ask="10.00", age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level is not None, strategy.said
    assert slot.distance is not None and slot.distance.fallback, strategy.said
    expected = max(Decimal("9.50"), up_on_step(Decimal("10.00") - slot.distance.distance))
    assert slot.level.as_decimal() == expected
    assert not [line for line in strategy.said[before:] if "unprotected" in line], strategy.said


def test_unsound_last_bars_plan_cold_and_a_carried_long_without_distance_is_covered(
    tmp_path: Path,
) -> None:
    """code-review-high/4: whether bars allow a plan is judged on the slice the planner is given;
    one sound bar before ATR_BARS unsound ones is no plan, so the cold rule covers the holding."""
    strategy = carried_without_distance(tmp_path)
    strategy.on_start()
    strategy.test_clock.set_time(OPEN + 20 * 60 * SECOND)
    strategy.answer("bars", [])
    unsound = {
        OPEN + i * 60 * SECOND: SimpleNamespace(
            ts_event=OPEN + i * 60 * SECOND, high=10.10, low=10.05, close=10.00
        )
        for i in range(1, protect.ATR_BARS + 1)
    }
    strategy.history_bars[AAA] = cast(Any, {OPEN: bar(OPEN, "0.02"), **unsound})
    before = len(strategy.said)
    strategy.seed("10.00", ask="10.02", age_s=0)
    slot = strategy.slots[AAA]
    assert slot.level is not None and slot.distance is not None, strategy.said
    assert slot.cold and slot.distance.bars == 0, strategy.said
    assert not [line for line in strategy.said[before:] if "unprotected" in line], strategy.said


def test_a_boats_request_at_0405_never_starts_after_it_ends() -> None:
    """code-review-high/7: at 04:05 New York the latest 04:00 is after now less the delay; the
    window is never inverted."""
    strategy = Cold(feed="boats")
    strategy.test_clock.set_time(int(datetime(2026, 11, 16, 9, 5, tzinfo=UTC).timestamp()) * SECOND)
    strategy.on_start()
    request = strategy.bar_requests[0]
    assert request["start"] <= request["end"], request
