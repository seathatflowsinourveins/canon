"""The session run's pieces offline: `SessionMaCross`, `PositionCap` and the window's checks (ADR 0044).

No test here opens a network connection or reads a credential. The strategy and the cap run on NautilusTrader's
`BacktestEngine` with a simulated `ALPACA` cash venue under `risk_limits`, as in `tests/test_risk_limits.py`,
over invented one-minute bars around $750 with a two-cent quote just before each bar closes. The window's
session and account checks run against hand-built alpaca-py `Clock` and `Calendar` models and a stand-in
for the paper `TradingClient`; the times are New York's.
"""

import json
import math
import os
import subprocess
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace
from typing import cast

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from canon_alpaca.constants import PRICE_INCREMENT
from canon_alpaca.constants import PRICE_PRECISION
from canon_alpaca.constants import USD
from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.backtest import BacktestEngineConfig
from nautilus_trader.common import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live import LiveExecutionEngineConfig
from nautilus_trader.model import AccountType
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
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
from nautilus_trader.model import TraderId
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy

from canon_trading import session_ma_cross
from canon_trading import session_window
from canon_trading.paper_node import exec_engine_config
from canon_trading.paper_node import risk_limits
from canon_trading.position_cap import PositionCap
from canon_trading.position_cap import position_cap_config
from canon_trading.session_ma_cross import EXTENDED_HOURS
from canon_trading.session_ma_cross import SessionMaCross
from canon_trading.session_ma_cross import SessionMaCrossConfig
from canon_trading.session_ma_cross import through_the_touch
from canon_trading.session_window import NEW_YORK
from canon_trading.session_window import WindowConfig
from canon_trading.session_window import session_end

SPY = InstrumentId.from_str("SPY.ALPACA")
BARS = BarType.from_str("SPY.ALPACA-1-MINUTE-LAST-EXTERNAL")
T0 = 1_762_790_400_000_000_000  # 2025-11-10T16:00:00Z
MINUTE = 60_000_000_000


def engine() -> BacktestEngine:
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"), risk_engine=RiskEngineConfig(**risk_limits(SPY))
        )
    )
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
    )
    eng.add_instrument(
        Equity(
            instrument_id=SPY,
            raw_symbol=Symbol("SPY"),
            currency=USD,
            price_precision=PRICE_PRECISION,
            price_increment=PRICE_INCREMENT,
            ts_event=0,
            ts_init=0,
        )
    )
    return eng


def market(minutes: int) -> list:
    """Bars whose closes swing $3 around $750 every few minutes, each with a quote a nanosecond before
    it closes, so a strategy deciding on the bar sees a quote at its close."""
    data: list = []
    for i in range(minutes):
        close = 750 + 3 * math.sin(i / 6)
        ts_close = T0 + (i + 1) * MINUTE
        data.append(
            Bar(
                BARS,
                Price(close, 4),
                Price(close + 0.05, 4),
                Price(close - 0.05, 4),
                Price(close, 4),
                Quantity.from_int(1000),
                T0 + i * MINUTE,
                ts_close,
            )
        )
        data.append(
            QuoteTick(
                SPY,
                Price(close - 0.01, 4),
                Price(close + 0.01, 4),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts_close - 1,
                ts_close - 1,
            )
        )
    return data


class Recording(SessionMaCross):
    """`SessionMaCross` that keeps the params of each submit."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.params: list[dict | None] = []

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.params.append(params)
        super().submit_order(order, position_id=position_id, client_id=client_id, params=params)


def strategy(**changes: object) -> Recording:
    settings: dict = {
        "bar_type": BARS,
        "trade_size": Quantity.from_int(1),
        "fast_period": 3,
        "slow_period": 8,
        "strategy_id": StrategyId("SessionMaCross-001"),
        "order_id_tag": "001",
    }
    settings.update(changes)
    return Recording(SessionMaCrossConfig(**settings))


def run(eng: BacktestEngine, *strategies) -> list:
    eng.add_data(market(60))
    for s in strategies:
        eng.add_strategy(s)
    eng.run()
    return sorted(
        eng.cache.orders(strategy_id=StrategyId("SessionMaCross-001")),
        key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
    )


def test_a_marketable_limit_goes_through_the_touch_in_whole_cents() -> None:
    bid, ask = Decimal("750.0050"), Decimal("750.0150")
    assert through_the_touch(OrderSide.BUY, bid, ask, Decimal("0.05")) == Decimal("750.07")
    assert through_the_touch(OrderSide.SELL, bid, ask, Decimal("0.05")) == Decimal("749.95")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"limit_offset": Decimal("0.005")}, "whole cents"),
        ({"limit_offset": Decimal("-0.01")}, "whole cents"),
        ({"limit_timeout_secs": 0}, "limit_timeout_secs"),
        ({"wind_down_at": datetime(2025, 11, 10, 16, 20)}, "aware"),
    ],
)
def test_the_config_refuses_settings_it_cannot_honour(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        strategy(**changes)


def test_in_the_session_it_trades_like_macross_with_market_orders() -> None:
    eng = engine()
    s = strategy()
    orders = run(eng, s)
    sides = [o.side for o in orders]
    types = {o.order_type.name for o in orders}
    eng.dispose()
    assert len(orders) >= 4
    assert sides[0] == OrderSide.BUY and all(a != b for a, b in pairwise(sides))
    assert types == {"MARKET"}
    # MaCross's own exit is reduce-only, which the adapter denies; these closes are plain DAY orders.
    sells = [o for o in orders if o.side == OrderSide.SELL]
    assert sells and not any(o.is_reduce_only for o in sells)
    assert {o.time_in_force.name for o in sells} == {"DAY"}
    assert s.params == [None] * s.params.count(None)


def test_outside_the_session_every_order_is_a_marked_day_limit_through_the_touch() -> None:
    eng = engine()
    s = strategy(extended_hours=True)
    orders = run(eng, s)
    summary = [(o.order_type.name, o.time_in_force.name) for o in orders]
    statuses = {o.status.name for o in orders}
    eng.dispose()
    assert orders and set(summary) == {("LIMIT", "DAY")}
    assert s.params and all(p == EXTENDED_HOURS for p in s.params)
    assert statuses == {"FILLED"}  # through the touch, so the simulated venue fills each at once


def test_a_limit_not_filled_in_time_is_canceled_and_its_signal_dropped(monkeypatch) -> None:
    """With the price moved a dollar away from the market the limit rests; after `limit_timeout_secs`
    it is canceled and the signal dropped, and while it rested no second order was sent."""

    def passive(side, bid, ask, offset):
        return (bid - 1 if side == OrderSide.BUY else ask + 1).quantize(Decimal("0.01"))

    monkeypatch.setattr(session_ma_cross, "through_the_touch", passive)
    eng = engine()
    s = strategy(extended_hours=True, limit_timeout_secs=90)
    orders = run(eng, s)
    statuses = [o.status.name for o in orders]
    eng.dispose()
    assert statuses and set(statuses) == {"CANCELED"}
    assert s.dropped == ["BUY: not filled in 90 s"] * len(orders)
    for earlier, later in pairwise(orders):
        assert (
            later.ts_init - earlier.ts_init >= 90 * 1_000_000_000
        )  # the guard held while it rested


def test_the_wind_down_in_the_session_is_upstreams_market_exit() -> None:
    """At `wind_down_at` the strategy calls `market_exit()`: the close is tagged `MARKET_EXIT`, DAY and not
    reduce-only, as the window configures it, and nothing is entered after it."""
    eng = engine()
    s = strategy(
        wind_down_at=datetime(2025, 11, 10, 16, 40, tzinfo=UTC),
        market_exit_reduce_only=False,
        market_exit_time_in_force=TimeInForce.DAY,
    )
    orders = run(eng, s)
    last = orders[-1]
    held = s.held()
    eng.dispose()
    assert held == 0
    assert last.side == OrderSide.SELL and "MARKET_EXIT" in (last.tags or [])
    assert not last.is_reduce_only and last.time_in_force == TimeInForce.DAY
    assert all(o.ts_init <= T0 + 41 * MINUTE for o in orders)


def test_the_wind_down_outside_the_session_closes_with_a_marked_limit() -> None:
    eng = engine()
    s = strategy(extended_hours=True, wind_down_at=datetime(2025, 11, 10, 16, 40, tzinfo=UTC))
    orders = run(eng, s)
    last = orders[-1]
    held = s.held()
    eng.dispose()
    assert held == 0
    assert (last.side, last.order_type.name) == (OrderSide.SELL, "LIMIT")
    assert all(o.ts_init <= T0 + 41 * MINUTE for o in orders)


class BuysTwo(Strategy):
    """Two market buys of one share each on its first two quotes: each passes the notional cap."""

    def __init__(self) -> None:
        super().__init__(StrategyConfig(strategy_id=StrategyId("BuysTwo-002"), order_id_tag="002"))

    def on_start(self) -> None:
        self.subscribe_quotes(SPY)

    def on_quote(self, quote) -> None:  # the first quote only times the order
        if len(self.cache.orders(strategy_id=self.strategy_id)) < 2:
            self.submit_order(self.order_factory.market(SPY, OrderSide.BUY, Quantity.from_int(1)))


def test_the_cap_trips_over_one_share_and_shuts_the_system_down(monkeypatch) -> None:
    reasons: list[str | None] = []
    monkeypatch.setattr(
        PositionCap, "shutdown_system", lambda self, reason=None: reasons.append(reason)
    )
    eng = engine()
    cap = PositionCap(position_cap_config(SPY))
    eng.add_data(market(5))
    eng.add_strategy(BuysTwo())
    eng.add_strategy(cap)
    eng.run()
    eng.dispose()
    assert cap.tripped == "net position 2 in SPY.ALPACA is over the cap of 1"
    assert reasons == [cap.tripped]
    assert cap.peak == 2


def test_the_cap_holds_still_at_one_share() -> None:
    eng = engine()
    cap = PositionCap(position_cap_config(SPY))
    orders = run(eng, strategy(), cap)
    eng.dispose()
    assert orders and cap.tripped is None and cap.peak == 1 and cap.checks > 3000


def test_the_native_runtime_checks_are_turned_on() -> None:
    config = exec_engine_config(SPY)
    assert config.open_check_interval_secs == 10.0
    assert config.position_check_interval_secs == 60.0
    assert config.reconciliation_instrument_ids == ["SPY.ALPACA"]
    assert config.reconciliation_lookback_mins == 1
    assert LiveExecutionEngineConfig().open_check_interval_secs is None
    assert LiveExecutionEngineConfig().position_check_interval_secs is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"session": "overnight"}, "session"),
        ({"symbol": "AAPL"}, "sized for SPY"),
        ({"feed": "sip"}, "iex"),
        ({"minutes": 61}, "minutes"),
        ({"minutes": 2}, "wind-down"),
    ],
)
def test_a_window_config_refuses_what_is_not_run(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        WindowConfig(**changes)


@pytest.fixture
def extended(monkeypatch):
    """Extended windows as they will be once the adapter's extended-hours change is on main."""
    monkeypatch.setattr(session_window, "EXTENDED_READY", True)


def test_an_extended_window_is_refused_until_one_has_passed_on_paper() -> None:
    assert session_window.EXTENDED_READY is False
    with pytest.raises(ValueError, match="waits until one has passed on paper"):
        WindowConfig(session="extended")


def test_the_calendar_is_read_in_new_york_whatever_the_host_zone() -> None:
    """alpaca-py builds `Calendar.open` naive; it is New York time, not the host's."""
    assert CALENDAR.open.tzinfo is None
    assert session_window.in_new_york(CALENDAR.open) == datetime(
        2026, 11, 10, 9, 30, tzinfo=NEW_YORK
    )


def clock(at: datetime, *, is_open: bool) -> Clock:
    return Clock(
        timestamp=at,
        is_open=is_open,
        next_open=datetime(2026, 11, 11, 9, 30, tzinfo=NEW_YORK),
        next_close=datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK),
    )


CALENDAR = Calendar(
    date="2026-11-10", open="09:30", close="16:00", session_open="0400", session_close="2000"
)


def test_a_regular_window_ends_with_the_session() -> None:
    at = datetime(2026, 11, 10, 15, 0, tzinfo=NEW_YORK)
    end = session_end(WindowConfig(), clock(at, is_open=True), CALENDAR)
    assert end == datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK)
    with pytest.raises(RuntimeError, match="regular session is closed"):
        session_end(WindowConfig(), clock(at, is_open=False), CALENDAR)


@pytest.mark.parametrize(
    ("hour", "minute", "end"),
    [(5, 0, datetime(2026, 11, 10, 9, 30)), (17, 0, datetime(2026, 11, 10, 20, 0))],
)
def test_an_extended_window_ends_with_pre_market_or_after_hours(
    hour, minute, end, extended
) -> None:
    at = datetime(2026, 11, 10, hour, minute, tzinfo=NEW_YORK)
    found = session_end(WindowConfig(session="extended"), clock(at, is_open=False), CALENDAR)
    assert found == end.replace(tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    ("at", "is_open", "message"),
    [
        (datetime(2026, 11, 10, 21, 0), False, "overnight is not run"),
        (datetime(2026, 11, 10, 11, 0), True, "regular session is open"),
        (datetime(2026, 11, 11, 5, 0), False, "not a trading day"),
    ],
)
def test_an_extended_window_refuses_overnight_the_session_and_other_days(
    at, is_open, message, extended
):
    with pytest.raises(RuntimeError, match=message):
        session_end(
            WindowConfig(session="extended"),
            clock(at.replace(tzinfo=NEW_YORK), is_open=is_open),
            CALENDAR,
        )


class Account:
    """A stand-in for the paper `TradingClient`: a clock, a calendar day, and what the account holds."""

    def __init__(self, *, orders: list | None = None) -> None:
        self.orders = orders or []

    def get_clock(self) -> Clock:
        return clock(datetime(2026, 11, 10, 11, 0, tzinfo=NEW_YORK), is_open=True)

    def get_calendar(self, filters=None) -> list[Calendar]:
        return [CALENDAR]

    def get_orders(self, filter=None) -> list:
        return self.orders

    def get_open_position(self, symbol):
        from alpaca.common.exceptions import APIError

        raise APIError(
            '{"code": 40410000, "message": "position does not exist"}',
            SimpleNamespace(response=SimpleNamespace(status_code=404)),
        )


def test_a_window_does_not_start_unless_the_account_is_flat(monkeypatch, tmp_path) -> None:
    held = SimpleNamespace(client_order_id="someone-elses")
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(orders=[held]))
    with pytest.raises(RuntimeError, match="not flat in SPY; not starting"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_a_window_waits_out_the_gap_after_the_last_one(tmp_path) -> None:
    import time

    (tmp_path / "last-window-end").write_text(str(time.time() - 10))
    with pytest.raises(RuntimeError, match="under 300 s ago"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_the_regular_window_node_runs_upstreams_managed_stop(tmp_path) -> None:
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    node, s, _ = session_window.window_node(
        WindowConfig(log_dir=tmp_path), wind_down_at=at, log_name="r"
    )
    try:
        assert s.manage_stop and not s.extended_hours and s.warm_up
        assert str(s.history_bar_type) == "SPY.ALPACA-1-MINUTE-LAST-EXTERNAL"
    finally:
        node.dispose()


def test_the_extended_window_node_builds_without_connecting(tmp_path, extended) -> None:
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    node, s, cap = session_window.window_node(
        WindowConfig(session="extended", log_dir=tmp_path), wind_down_at=at, log_name="t"
    )
    try:
        assert s.extended_hours and not s.manage_stop and s.wind_down_at == at
        assert cap.max_position == 1
        assert str(s.bar_type) == "SPY.ALPACA-1-MINUTE-LAST-INTERNAL"
        assert not node.is_running
    finally:
        node.dispose()


class Probe:
    """Calls the strategy's stop and close paths with its reads replaced, to see what it would send."""

    def __init__(self, s: SessionMaCross, *, working: bool, held: Decimal) -> None:
        self.sent: list[tuple[str, object]] = []
        s.working = lambda: working
        s.held = lambda: held
        s.cancel_all_orders = lambda *a, **k: self.sent.append(("cancel_all", a))
        s._limit = lambda side, quantity: self.sent.append((side.name, quantity))
        s.close_all_positions = lambda *a, **k: self.sent.append(("close_all", a))


def test_a_stop_while_an_order_works_cancels_and_sends_no_second_exit() -> None:
    s = strategy(extended_hours=True)
    probe = Probe(s, working=True, held=Decimal(1))
    s.on_stop()
    assert probe.sent == [("cancel_all", (SPY,))]


def test_a_short_is_bought_back_not_added_to() -> None:
    s = strategy(extended_hours=True)
    probe = Probe(s, working=False, held=Decimal(-1))
    s.on_stop()
    assert probe.sent == [("BUY", Decimal(1))]


class Holding:
    """A paper account holding one open order and one share of SPY: a cancel takes a poll to land, and a
    close is refused (403) while the order still holds the share, as Alpaca refuses it."""

    def __init__(self) -> None:
        self.orders = [SimpleNamespace(id="o-1", client_order_id="exit-1")]
        self.canceling = False
        self.qty = 1
        self.calls: list[str] = []
        self.is_open = True

    def get_clock(self) -> Clock:
        return clock(datetime(2026, 11, 10, 15, 0, tzinfo=NEW_YORK), is_open=self.is_open)

    def get_orders(self, filter=None) -> list:
        if self.canceling:
            self.orders, self.canceling = [], False
        return list(self.orders)

    def get_open_position(self, symbol):
        from alpaca.common.exceptions import APIError

        if not self.qty:
            raise APIError(
                '{"code": 40410000, "message": "position does not exist"}',
                SimpleNamespace(response=SimpleNamespace(status_code=404)),
            )
        return SimpleNamespace(qty=str(self.qty), side="long", __class__=object)

    def cancel_order_by_id(self, order_id) -> None:
        self.calls.append(f"cancel {order_id}")
        self.canceling = True

    def close_position(self, symbol) -> None:
        from alpaca.common.exceptions import APIError

        if self.orders:
            raise APIError('{"code": 40310000, "message": "insufficient qty available"}')
        self.calls.append(f"close {symbol}")
        self.qty = 0


def test_after_the_stop_the_cancels_land_before_the_close(monkeypatch) -> None:
    monkeypatch.setattr(session_window, "POLL_SECS", 0.0)
    monkeypatch.setattr(session_window, "position_qty", lambda t, s: Decimal(t.qty))
    account = Holding()
    report = session_window.WindowReport("regular", "SPY", started="t")
    left = session_window._flatten_after_stop(cast(TradingClient, account), WindowConfig(), report)
    assert account.calls == ["cancel o-1", "close SPY"]
    assert left == [] and report.closed_after_stop and report.errors == []


def test_outside_the_session_the_orders_are_canceled_and_the_position_reported(
    monkeypatch, extended
) -> None:
    monkeypatch.setattr(session_window, "POLL_SECS", 0.0)
    monkeypatch.setattr(session_window, "position_qty", lambda t, s: Decimal(t.qty))
    account = Holding()
    account.is_open = False  # outside the regular session
    report = session_window.WindowReport("extended", "SPY", started="t")
    left = session_window._flatten_after_stop(
        cast(TradingClient, account), WindowConfig(session="extended"), report
    )
    assert account.calls == ["cancel o-1"]
    assert left == ["position of 1 SPY"]


def test_a_gap_marker_cut_short_still_dates_the_last_window(tmp_path) -> None:
    (tmp_path / "last-window-end").write_text("17628")  # cut short; written just now
    with pytest.raises(RuntimeError, match="under 300 s ago"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_a_short_found_on_a_bar_is_closed_before_anything_else() -> None:
    s = strategy(extended_hours=True)
    probe = Probe(s, working=False, held=Decimal(-1))
    s.is_exiting = lambda: False
    s.on_bar(cast(Bar, SimpleNamespace(bar_type=None)))  # a bar of no subscribed type
    assert probe.sent == [("BUY", Decimal(1))]


def test_the_wind_down_waits_for_a_working_order_before_the_market_exit() -> None:
    """In the session the wind-down cancels what works and leaves the market exit to a later bar, so a
    second close never joins one in flight."""
    s = strategy()
    probe = Probe(s, working=True, held=Decimal(1))
    exits: list[str] = []
    s.market_exit = lambda: exits.append("market_exit")
    s._wind_down(cast(TimeEvent, None))  # the alert only times it
    assert probe.sent == [("cancel_all", (SPY,))] and exits == []


LIVE = BarType.from_str("SPY.ALPACA-1-MINUTE-LAST-INTERNAL")
HISTORY = BarType.from_str("SPY.ALPACA-1-MINUTE-LAST-EXTERNAL")


class FirstBar(SessionMaCross):
    """Records, at its first live bar, whether the averages were already initialized."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.ready_at_first_bar: bool | None = None
        self.live_bars = 0

    def on_bar(self, bar) -> None:
        if bar.bar_type == LIVE:
            self.live_bars += 1
            if self.ready_at_first_bar is None:
                self.ready_at_first_bar = self.hydrated and self.indicators_initialized()
        super().on_bar(bar)


def warm_up_run(tmp_path, *, warm_up: bool) -> FirstBar:
    """Thirty minutes of the adapter's EXTERNAL one-minute bars in a catalog, as `request_bars` would get
    them, then twenty minutes of live trades every ten seconds, from which the data engine builds the
    INTERNAL bars the window trades on."""
    from nautilus_trader.config import DataCatalogConfig
    from nautilus_trader.model import AggressorSide
    from nautilus_trader.model import TradeId
    from nautilus_trader.model import TradeTick
    from nautilus_trader.persistence import ParquetDataCatalog

    probe = engine()
    instrument = probe.cache.instrument(SPY)
    probe.dispose()
    history = []
    for i in range(30):
        close = Price(750 + i / 100, 4)
        history.append(
            Bar(
                HISTORY,
                Price(750, 4),
                Price(750.40, 4),
                Price(749.95, 4),
                close,
                Quantity.from_int(1000),
                T0 + i * MINUTE,
                T0 + (i + 1) * MINUTE,
            )
        )
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_instruments([instrument])
    catalog.write_bars(history)
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            risk_engine=RiskEngineConfig(**risk_limits(SPY)),
            catalogs=[DataCatalogConfig(path=str(tmp_path))],
        )
    )
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
    )
    eng.add_instrument(instrument)
    start = T0 + 30 * MINUTE
    eng.add_data(
        [
            TradeTick(
                SPY,
                Price(750.30 + (k % 3) / 100, 4),
                Quantity.from_int(10),
                AggressorSide.NO_AGGRESSOR,
                TradeId(f"t{k}"),
                start + k * 10_000_000_000,
                start + k * 10_000_000_000,
            )
            for k in range(1, 121)
        ]
    )
    s = FirstBar(
        SessionMaCrossConfig(
            bar_type=LIVE,
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            warm_up=warm_up,
            strategy_id=StrategyId("SessionMaCross-001"),
            order_id_tag="001",
        )
    )
    eng.add_strategy(s)
    eng.run()
    eng.dispose()
    return s


def test_the_warm_up_initializes_the_averages_before_the_first_live_bar(tmp_path) -> None:
    """Upstream's pattern (concepts/strategies, "Handler example"): the averages are registered for the
    adapter's EXTERNAL bars too, and the request's bars hydrate them before any live bar arrives."""
    s = warm_up_run(tmp_path, warm_up=True)
    assert s.history_bars == 30  # the whole hour asked for, which the averages keep the end of
    assert s.history_dropped == 0
    assert s.live_bars > 0 and s.ready_at_first_bar is True


def test_without_the_warm_up_the_first_live_bar_finds_the_averages_cold(tmp_path) -> None:
    s = warm_up_run(tmp_path, warm_up=False)
    assert s.history_bars == 0
    assert s.live_bars > 0 and s.ready_at_first_bar is False


def unstarted(**changes: object) -> SessionMaCross:
    s = strategy(bar_type=LIVE, warm_up=True, **changes)
    s.hydrated, s.live_bars = False, 0
    return s


def history(count: int) -> list:
    return [
        Bar(
            HISTORY,
            Price(750, 4),
            Price(751, 4),
            Price(749, 4),
            Price(750, 4),
            Quantity.from_int(1),
            T0 + i * MINUTE,
            T0 + (i + 1) * MINUTE,
        )
        for i in range(count)
    ]


def test_a_warm_up_answer_after_a_live_bar_is_dropped_not_fed_after_it() -> None:
    s = unstarted()
    s.live_bars = 1  # a live bar has already reached the averages
    s.on_historical_bars(history(13))
    assert (s.history_bars, s.history_dropped, s.slow.count) == (0, 13, 0)


def test_a_warm_up_answer_after_the_timeout_is_dropped() -> None:
    s = unstarted()
    s._on_history_timeout(cast(TimeEvent, None))
    assert s.hydrated
    s.on_historical_bars(history(13))
    assert (s.history_bars, s.history_dropped) == (0, 13)


def test_a_warm_up_answer_in_time_initializes_the_averages() -> None:
    s = unstarted()
    s.on_historical_bars(list(reversed(history(13))))  # fed oldest first whatever the order
    assert s.history_bars == 13 and s.fast.initialized and s.slow.initialized


def test_the_warm_up_needs_time_bars() -> None:
    with pytest.raises(ValueError, match="needs time bars"):
        strategy(bar_type=BarType.from_str("SPY.ALPACA-100-TICK-LAST-INTERNAL"), warm_up=True)


@pytest.mark.parametrize(
    ("at", "is_open", "ready", "expected"),
    [
        (datetime(2026, 11, 10, 11, 0), True, False, "regular"),
        (datetime(2026, 11, 10, 17, 0), False, False, None),
        (datetime(2026, 11, 10, 17, 0), False, True, "extended"),
        (datetime(2026, 11, 10, 21, 0), False, True, None),  # overnight is not run
    ],
)
def test_auto_takes_the_session_alpacas_clock_says_is_open(
    monkeypatch, at, is_open, ready, expected
):
    monkeypatch.setattr(session_window, "EXTENDED_READY", ready)
    found, why = session_window.resolve_session(
        clock(at.replace(tzinfo=NEW_YORK), is_open=is_open), CALENDAR
    )
    assert found == expected and why


def test_auto_outside_every_session_runs_nothing_and_exits_0(monkeypatch, tmp_path, capsys) -> None:
    class Closed(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 10, 17, 0, tzinfo=NEW_YORK), is_open=False)

    monkeypatch.setattr(session_window, "paper_trading", lambda: Closed())
    assert session_window.main(["--session", "auto", "--log-dir", str(tmp_path)]) == 0
    assert "nothing to run" in capsys.readouterr().out
    assert not (tmp_path / "latest.json").exists()


def test_a_refused_window_is_the_latest_record(monkeypatch, tmp_path) -> None:
    held = SimpleNamespace(client_order_id="someone-elses")
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(orders=[held]))
    monkeypatch.setenv(session_window.COMMIT_ENV, "a" * 40)
    assert session_window.main(["--log-dir", str(tmp_path)]) == 1
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("refused: the paper account is not flat")
    assert latest["commit"] == "a" * 40


def test_the_commit_comes_from_the_runner_else_from_git_else_says_unknown(
    monkeypatch, tmp_path
) -> None:
    """ADR 0050: every record names the code that ran; the field is never left out or empty."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv(
        "GIT_CONFIG_GLOBAL", os.devnull
    )  # no signing, hooks or template of the caller's
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "checkout"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f").write_text("1")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    monkeypatch.delenv(session_window.COMMIT_ENV, raising=False)
    assert session_window.running_commit(repo) == head
    (repo / "f").write_text("2")
    assert session_window.running_commit(repo) == head  # read once a process
    session_window._checkout_commit.cache_clear()
    assert session_window.running_commit(repo) == f"{head} with uncommitted changes"
    (tmp_path / "plain").mkdir()
    assert session_window.running_commit(tmp_path / "plain").startswith("unknown: ")
    monkeypatch.setenv(session_window.COMMIT_ENV, "b" * 40)
    assert session_window.running_commit(tmp_path / "plain") == "b" * 40


def test_a_window_that_fails_after_it_started_records_failed(monkeypatch, tmp_path) -> None:
    """The `running` record is written before the node is built; a failure then turns it into `failed`,
    and the account is still checked."""
    seen: list[str] = []

    def broken(config, **kwargs):
        running = json.loads((tmp_path / "latest.json").read_text())
        seen.extend((running["result"], running["commit"]))
        raise RuntimeError("the node could not be built")

    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", broken)
    monkeypatch.setenv(session_window.COMMIT_ENV, "c" * 40)
    with pytest.raises(RuntimeError, match="could not be built"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert seen == ["running", "c" * 40] and latest["commit"] == "c" * 40
    assert latest["result"] == "failed: RuntimeError: the node could not be built"
    assert latest["left"] == [] and (tmp_path / f"window-{latest['started']}.json").exists()


def test_a_window_whose_account_check_fails_is_recorded_failed_not_ok(
    monkeypatch, tmp_path
) -> None:
    """Review finding: an error checking the account after a clean run was recorded `ok`."""

    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [])

        def dispose(self) -> None:
            pass

    s = SimpleNamespace(dropped=[], history_bars=0, history_dropped=0)
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: (Ran(), s, cap))
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)

    def unreachable(trading, config, report):
        raise RuntimeError("Alpaca answered 503")

    monkeypatch.setattr(session_window, "_flatten_after_stop", unreachable)
    with pytest.raises(RuntimeError, match="503"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"] == "failed: RuntimeError: Alpaca answered 503"


def test_a_window_inside_the_gap_is_skipped_exit_0_and_the_last_result_kept(tmp_path, capsys):
    (tmp_path / "last-window-end").write_text("1")
    (tmp_path / "latest.json").write_text('{"result": "ok"}')
    assert session_window.main(["--log-dir", str(tmp_path)]) == 0
    assert "skipped: the last window ended under 300 s ago" in capsys.readouterr().out
    assert json.loads((tmp_path / "latest.json").read_text()) == {"result": "ok"}
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("skipped:")


def test_every_run_leaves_a_heartbeat_even_with_nothing_to_run(monkeypatch, tmp_path) -> None:
    class Closed(Account):
        def get_clock(self) -> Clock:
            return clock(
                datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK), is_open=False
            )  # a Saturday

    monkeypatch.setattr(session_window, "paper_trading", lambda: Closed())
    monkeypatch.setenv(session_window.COMMIT_ENV, "d" * 40)
    assert session_window.main(["--session", "auto", "--log-dir", str(tmp_path)]) == 0
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("nothing to run") and heard["commit"] == "d" * 40


# Review findings on the session windows (a second reader, 2026-11-10): each shown here before its fix.


def test_the_stop_is_bounded_from_the_nodes_own_bounds_inside_a_record_going_stale() -> None:
    """The window's wait for the stop is the market exit at its longest, the node's own stop bounds and
    one Alpaca request; with the account check after it, a window still ends before `stale_after`."""
    assert session_window.STOP_TIMEOUT_SECS == pytest.approx(60 + 5 + 15 + 5 + 3.05 + 27)
    longest = (
        session_window.MAX_MINUTES * 60
        + session_window.STOP_TIMEOUT_SECS
        + session_window.AFTER_STOP_SECS
        + 30  # the last settle
    )
    assert longest < session_window.STALE_RUNNING_SECS


def test_a_stop_that_timed_out_ends_the_process_after_its_record(monkeypatch, tmp_path) -> None:
    import os

    def timed_out(config, **kwargs):
        session_window._ABANDONED.append(("runner", "task"))
        raise session_window.StopTimedOut("the node did not stop within 115 s")

    exits: list[int] = []

    def exit_now(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "run_window", timed_out)
    monkeypatch.setattr(os, "_exit", exit_now)
    with pytest.raises(SystemExit):
        session_window.main(["--log-dir", str(tmp_path)])
    assert exits == [1]
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("failed: StopTimedOut")


class Hung:
    """A node whose stop never ends: `run_async` waits forever and absorbs a cancel, as upstream's hosted
    run does while its shutdown runs ("cancelling the awaiting task requests the same graceful shutdown,
    waits for it to finish", `crates/live/src/python/node.rs` at the pin)."""

    cache = SimpleNamespace(orders=lambda **kwargs: [])

    def __init__(self) -> None:
        self.stops = 0
        self.disposed = False

    def handle(self):
        return SimpleNamespace(stop=self._stop)

    def _stop(self) -> None:
        self.stops += 1

    async def run_async(self) -> None:
        import asyncio

        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    def dispose(self) -> None:
        self.disposed = True


def ran_node(node) -> tuple:
    s = SimpleNamespace(dropped=[], history_bars=0, history_dropped=0)
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    return node, s, cap


def test_a_stop_that_hangs_is_bounded_recorded_failed_and_the_account_still_checked(
    monkeypatch, tmp_path
) -> None:
    """Finding 1: after the stop, the window waited on the node with no limit."""
    import threading

    node = Hung()
    checked: list[str] = []
    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "STOP_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: ran_node(node))
    monkeypatch.setattr(
        session_window,
        "_flatten_after_stop",
        lambda trading, config, report: checked.append("checked") or [],
    )
    raised: list[BaseException] = []

    def window() -> None:  # the stand-in clock is in the past, so the window's end has come
        try:
            session_window.run_window(WindowConfig(log_dir=tmp_path))
        except BaseException as e:  # noqa: BLE001 - the test reads what was raised
            raised.append(e)

    thread = threading.Thread(target=window, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "the window still waits on a node whose stop hangs"
    assert node.stops == 1 and checked == ["checked"]
    assert raised and isinstance(raised[0], session_window.StopTimedOut)
    assert not node.disposed  # its task still owns it
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("failed: StopTimedOut: the node did not stop within 0.2 s")


class FakeTime:
    """`time` with a monotonic clock that only moves when the code sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        import time

        return time.time()


class Clearing(Holding):
    """`Holding`, whose cancel lands only `after` seconds of `FakeTime`, past one settle's 30 s; the
    clock is open unless the test closes it; a close that would queue (session closed) stays open."""

    def __init__(self, fake: FakeTime, *, after: float = 40.0, is_open: bool = True) -> None:
        super().__init__()
        self.fake = fake
        self.after = after
        self.canceled_at: float | None = None
        self.is_open = is_open
        self.queue_closes = False

    def get_orders(self, filter=None) -> list:
        if self.canceled_at is not None and self.fake.now - self.canceled_at >= self.after:
            self.orders = [o for o in self.orders if o.id != "o-1"]
        return list(self.orders)

    def cancel_order_by_id(self, order_id) -> None:
        self.calls.append(f"cancel {order_id}")
        if order_id == "o-1":
            self.canceled_at = self.fake.now
        else:
            self.orders = [o for o in self.orders if o.id != order_id]

    def close_position(self, symbol) -> None:
        from alpaca.common.exceptions import APIError

        if any(o.id == "o-1" for o in self.orders):
            self.calls.append("close refused")
            raise APIError('{"code": 40310000, "message": "insufficient qty available"}')
        self.calls.append(f"close {symbol}")
        if self.queue_closes:  # a DAY market order after the close waits for the next session
            self.orders.append(SimpleNamespace(id="c-1", client_order_id="close-1"))
            self.is_open = False
        else:
            self.qty = 0


@pytest.fixture
def fake_time(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(session_window, "time", fake)
    monkeypatch.setattr(session_window, "position_qty", lambda t, s: Decimal(t.qty))
    return fake


def test_a_close_refused_while_a_cancel_clears_is_tried_again(fake_time) -> None:
    """Finding 2: the market close was tried once; Alpaca's 403 while the cancel cleared ended it."""
    account = Clearing(fake_time, after=40.0)
    report = session_window.WindowReport("regular", "SPY", started="t")
    left = session_window._flatten_after_stop(cast(TradingClient, account), WindowConfig(), report)
    assert left == [] and report.closed_after_stop
    assert account.calls[-1] == "close SPY"


def test_no_market_close_is_sent_once_alpacas_clock_says_the_session_closed(fake_time) -> None:
    """Finding 2: a regular window's close after 16:00 would only queue for the next session."""
    account = Clearing(fake_time, after=0.0, is_open=False)
    report = session_window.WindowReport("regular", "SPY", started="t")
    left = session_window._flatten_after_stop(cast(TradingClient, account), WindowConfig(), report)
    assert "close SPY" not in account.calls and not report.closed_after_stop
    assert left == ["position of 1 SPY"]


def test_a_close_that_would_only_queue_is_canceled(fake_time) -> None:
    """Finding 2: a close sent as the session closed queued, and nothing canceled it."""
    account = Clearing(fake_time, after=0.0)
    account.queue_closes = True
    report = session_window.WindowReport("regular", "SPY", started="t")
    left = session_window._flatten_after_stop(cast(TradingClient, account), WindowConfig(), report)
    assert account.calls[-1] == "cancel c-1"
    assert left == ["position of 1 SPY"]


def test_the_cap_stops_the_node_and_leaves_a_non_flattening_strategys_position(monkeypatch) -> None:
    """Finding 3: the cap's header said the strategies then flatten. It sends no order of its own, and a
    strategy that does not flatten on stop (upstream's `EmaCross`, like `BuysTwo` here) leaves its
    position; the caller closes it (the paper test's `left_as_found`, the window's account check)."""
    monkeypatch.setattr(PositionCap, "shutdown_system", lambda self, reason=None: None)
    eng = engine()
    cap = PositionCap(position_cap_config(SPY))
    eng.add_data(market(5))
    eng.add_strategy(BuysTwo())
    eng.add_strategy(cap)
    eng.run()
    held = eng.cache.positions_open(instrument_id=SPY)
    capped = eng.cache.orders(strategy_id=cap.strategy_id)
    eng.dispose()
    assert cap.tripped and not capped
    assert [p.signed_qty for p in held] == [2]
    from canon_trading import position_cap

    assert "then flatten" not in (position_cap.__doc__ or "")


def test_a_window_that_never_ran_leaves_no_gap_marker(monkeypatch, tmp_path) -> None:
    """Finding 4: a build failure wrote the gap marker, so a retry was skipped for 300 s."""

    def broken(config, **kwargs):
        raise RuntimeError("build boom")

    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", broken)
    with pytest.raises(RuntimeError, match="build boom"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert not (tmp_path / "last-window-end").exists()
    with pytest.raises(RuntimeError, match="build boom"):  # the retry runs, not skipped
        session_window.run_window(WindowConfig(log_dir=tmp_path))


class Regimes(Recording):
    """`Recording`, noting at each bar whether the fast average is above the slow, and the bar each
    submit came on."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.above: list[bool] = []
        self.sent_at: list[int] = []

    def on_bar(self, bar) -> None:
        if self.indicators_initialized():
            self.above.append(self.fast.value > self.slow.value)
        else:
            self.above.append(False)
        super().on_bar(bar)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.sent_at.append(len(self.above) - 1)
        super().submit_order(order, position_id=position_id, client_id=client_id, params=params)


def test_a_dropped_entry_is_not_chased_until_the_signal_changes(monkeypatch) -> None:
    """Finding 5: a timed-out entry was sent again on the next bar while the cross held."""

    def passive(side, bid, ask, offset):
        return (bid - 1 if side == OrderSide.BUY else ask + 1).quantize(Decimal("0.01"))

    monkeypatch.setattr(session_ma_cross, "through_the_touch", passive)
    eng = engine()
    s = Regimes(
        SessionMaCrossConfig(
            bar_type=BARS,
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            extended_hours=True,
            limit_timeout_secs=90,
            strategy_id=StrategyId("SessionMaCross-001"),
            order_id_tag="001",
        )
    )
    run(eng, s)
    eng.dispose()
    assert len(s.sent_at) >= 2
    for earlier, later in pairwise(s.sent_at):
        assert not all(s.above[earlier : later + 1]), (
            f"a second entry at bar {later} while the cross from bar {earlier} still held"
        )


def test_a_refused_window_updates_the_heartbeat(monkeypatch, tmp_path) -> None:
    """Finding 6: the heartbeat still read 'a regular window' after a refusal."""
    held = SimpleNamespace(client_order_id="someone-elses")
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(orders=[held]))
    assert session_window.main(["--log-dir", str(tmp_path)]) == 1
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("refused: the paper account is not flat")


# Code review of the fixes above (2026-11-10): each shown here before its fix.


def test_an_abandoned_stop_ends_the_process_whatever_error_reaches_main(monkeypatch, tmp_path):
    """The account check after a timed-out stop can raise in its place; the process still ends."""
    import os

    def timed_out_then_unreachable(config, **kwargs):
        session_window._ABANDONED.append(("runner", "task"))
        raise APIError('{"code": 50010000, "message": "internal server error"}')

    exits: list[int] = []

    def exit_now(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "run_window", timed_out_then_unreachable)
    monkeypatch.setattr(os, "_exit", exit_now)
    with pytest.raises(SystemExit):
        session_window.main(["--log-dir", str(tmp_path)])
    assert exits == [1]


INTERRUPTED = """
import asyncio, signal, threading, time
from datetime import UTC, datetime
from canon_trading import session_window

class Hung:
    stops = 0
    def handle(self):
        return self
    def stop(self):
        Hung.stops += 1
    async def run_async(self):
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

session_window.STOP_TIMEOUT_SECS = 0.3
signal.signal(signal.SIGINT, signal.default_int_handler)  # as in a terminal; a script may inherit SIG_IGN
main = threading.main_thread().ident  # a terminal Ctrl-C reaches the main thread
threading.Timer(0.1, signal.pthread_kill, (main, signal.SIGINT)).start()
import sys
ends = datetime(2100, 1, 1, tzinfo=UTC) if sys.argv[1] == "running" else datetime.now(UTC)
session_window.STOP_TIMEOUT_SECS = 0.3 if sys.argv[1] == "running" else 30
try:
    session_window._run_node(Hung(), ends)
except KeyboardInterrupt:
    print("interrupted", Hung.stops, len(session_window._ABANDONED), flush=True)
"""


@pytest.mark.parametrize("when", ["running", "stopping"])
def test_an_interrupt_during_a_hung_stop_abandons_the_node_and_does_not_wait(when: str) -> None:
    """Ctrl-C while the stop hangs: closing the runner would cancel the node's task and wait for it. Run in
    a child process, where `asyncio.Runner` handles SIGINT on the main thread and a hang is killed."""
    import subprocess
    import sys

    try:
        child = subprocess.run(
            [sys.executable, "-c", INTERRUPTED, when],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the interrupted window still waits on a node whose stop hangs")
    assert "interrupted 1 1" in child.stdout, child.stderr[-2000:]


def test_a_window_ends_early_enough_for_its_account_check_inside_the_session() -> None:
    """A window clipped to the session's end still checks the account, after a stop at its bound, while
    the market close can be sent."""
    assert session_window.END_MARGIN_SECS >= (
        session_window.STOP_TIMEOUT_SECS + session_window.AFTER_STOP_SECS
    )


class Timeouts(SessionMaCross):
    """Calls `_on_limit_timeout` for an order given by the test, with the cancel recorded."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.test_order = None
        self.canceled: list[str] = []

    def _order(self, client_order_id):
        return self.test_order

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.canceled.append(str(client_order_id))


def timed_out(side: OrderSide, client_order_id: str = "O-1"):
    from nautilus_trader.model import ClientOrderId

    return SimpleNamespace(
        is_closed=False, is_open=True, side=side, client_order_id=ClientOrderId(client_order_id)
    )


def unstarted_timeouts(order) -> Timeouts:
    s = Timeouts(
        SessionMaCrossConfig(
            bar_type=BARS,
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            extended_hours=True,
            strategy_id=StrategyId("SessionMaCross-001"),
            order_id_tag="001",
        )
    )
    s.test_order = order
    return s


def test_a_short_buy_back_that_times_out_does_not_hold_back_an_entry() -> None:
    s = unstarted_timeouts(timed_out(OrderSide.BUY))
    s._on_limit_timeout(cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1")))
    assert s.canceled == ["O-1"] and s.dropped_side is None


def test_a_dropped_entry_is_held_through_a_bar_with_equal_averages() -> None:
    s = unstarted_timeouts(timed_out(OrderSide.BUY))
    s.signal_orders.add("O-1")
    s._on_limit_timeout(cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1")))
    assert s.dropped_side == OrderSide.BUY
    s._clear_dropped(above=False, below=False)  # fast == slow: no cross yet
    assert s.dropped_side == OrderSide.BUY
    s._clear_dropped(above=False, below=True)
    assert s.dropped_side is None


# Second code review (2026-11-10): each shown here before its fix.


def test_the_windows_own_alpaca_client_has_the_adapters_http_timeout(monkeypatch) -> None:
    """The account check after an abandoned stop must not wait on a request forever (ADR 0039)."""
    from canon_alpaca.providers import session_of

    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = session_of(session_window.paper_trading())
    assert session is not None
    adapter = session.get_adapter("https://paper-api.alpaca.markets")
    assert type(adapter).__name__ == "TimeoutAdapter"


def test_the_end_margin_leaves_room_beyond_the_stop_and_the_check() -> None:
    """A minute beyond the stop's bound and the check's: the calls before the check's own deadline, and
    skew between Alpaca's clock and this host's."""
    assert session_window.END_MARGIN_SECS >= (
        session_window.STOP_TIMEOUT_SECS + session_window.AFTER_STOP_SECS + 60
    )


def test_an_error_after_an_abandoned_stop_is_printed_and_heard_before_the_exit(
    monkeypatch, tmp_path, capsys
) -> None:
    import os

    def abandoned_then_broken(config, **kwargs):
        session_window._ABANDONED.append(("runner", "task"))
        raise AssertionError("an unexpected answer")

    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "run_window", abandoned_then_broken)
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        session_window.main(["--log-dir", str(tmp_path)])
    assert "AssertionError: an unexpected answer" in capsys.readouterr().err
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("failed: AssertionError")


def test_a_node_is_disposed_unless_its_own_run_was_abandoned(monkeypatch, tmp_path) -> None:
    """An earlier abandonment in the process does not keep a later node from being disposed."""

    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [])
        disposed = False

        def dispose(self) -> None:
            Ran.disposed = True

    s = SimpleNamespace(dropped=[], history_bars=0, history_dropped=0)
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    monkeypatch.setattr(session_window, "_ABANDONED", [("an earlier runner", "task")])
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: (Ran(), s, cap))
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert Ran.disposed


def test_a_signal_limit_is_forgotten_once_its_timeout_has_run() -> None:
    s = unstarted_timeouts(timed_out(OrderSide.BUY))
    s.signal_orders.add("O-1")
    s._on_limit_timeout(cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1")))
    assert s.signal_orders == set()
