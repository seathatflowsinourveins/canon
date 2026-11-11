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
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID
from uuid import uuid4

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.requests import MarketOrderRequest
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
from requests import Response
from requests.adapters import HTTPAdapter

from canon_trading import session_ma_cross
from canon_trading import session_window
from canon_trading.paper_node import MAX_ORDER_SUBMIT_RATE
from canon_trading.paper_node import exec_engine_config
from canon_trading.paper_node import risk_limits
from canon_trading.position_cap import PositionCap
from canon_trading.position_cap import PositionCapConfig
from canon_trading.position_cap import position_cap_config
from canon_trading.session_ma_cross import EXTENDED_HOURS
from canon_trading.session_ma_cross import SessionMaCross
from canon_trading.session_ma_cross import SessionMaCrossConfig
from canon_trading.session_ma_cross import through_the_touch
from canon_trading.session_window import NEW_YORK
from canon_trading.session_window import WindowConfig
from canon_trading.session_window import session_part


def insufficient() -> APIError:
    """Alpaca's 403 for a close while an order holds the shares: a refusal, nothing placed."""
    return APIError(
        '{"code": 40310000, "message": "insufficient qty available"}',
        SimpleNamespace(response=SimpleNamespace(status_code=403)),
    )


SPY = InstrumentId.from_str("SPY.ALPACA")
BARS = BarType.from_str("SPY.ALPACA-1-MINUTE-LAST-EXTERNAL")
T0 = 1_762_790_400_000_000_000  # 2025-11-10T16:00:00Z
MINUTE = 60_000_000_000


def engine(*, max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE) -> BacktestEngine:
    limits = risk_limits(SPY, max_order_submit_rate=max_order_submit_rate)
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"), risk_engine=RiskEngineConfig(**limits)
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


@pytest.mark.parametrize("misspelled", ["wind_down", "fast_perid", "manage_stp"])
def test_the_config_refuses_a_keyword_it_does_not_have(misspelled: str) -> None:
    """A misspelled `wind_down_at` used to mean no wind-down, silently (reported by DeepSeek Harness)."""
    with pytest.raises(
        TypeError, match=f"SessionMaCrossConfig got an unexpected keyword argument '{misspelled}'"
    ):
        strategy(**{misspelled: datetime(2025, 11, 10, 21, tzinfo=UTC)})


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


def test_the_cap_config_refuses_a_keyword_it_does_not_have() -> None:
    """`max_postion=0` used to leave the cap at one share, silently (reported by DeepSeek Harness)."""
    with pytest.raises(
        TypeError, match="PositionCapConfig got an unexpected keyword argument 'max_postion'"
    ):
        PositionCapConfig(instrument_id=SPY, max_postion=Decimal(0))


def test_the_cap_config_keeps_its_base_fields() -> None:
    config = position_cap_config(SPY, max_position=Decimal(0))
    assert (config.max_position, config.order_id_tag) == (Decimal(0), "CAP")


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        pytest.param({"extended_hours": "False"}, "extended_hours", id="text flag"),
        pytest.param({"warm_up": 1}, "warm_up", id="int flag"),
        pytest.param({"limit_timeout_secs": 30.5}, "limit_timeout_secs", id="float seconds"),
        pytest.param({"limit_offset": 0.05}, "limit_offset", id="float offset"),
        pytest.param({"wind_down_at": "2025-11-10T21:00Z"}, "wind_down_at", id="text time"),
        pytest.param({"fast_period": 3.0}, "fast_period", id="float period"),
        pytest.param({"feed": b"boats"}, "feed", id="bytes feed"),
    ],
)
def test_the_config_refuses_a_setting_of_another_type(changes: dict, named: str) -> None:
    """`extended_hours="False"` is true and would run the window on marked limits; each setting takes
    one type."""
    with pytest.raises(TypeError, match=f"SessionMaCrossConfig's {named} is"):
        strategy(**changes)


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        pytest.param({"instrument_id": "SPY.ALPACA"}, "instrument_id", id="text instrument"),
        pytest.param({"max_position": 1}, "max_position", id="int cap"),
        pytest.param({"interval_secs": True}, "interval_secs", id="bool interval"),
    ],
)
def test_the_cap_config_refuses_a_field_of_another_type(changes: dict, named: str) -> None:
    fields: dict = {"instrument_id": SPY, **changes}
    with pytest.raises(TypeError, match=f"PositionCapConfig's {named} is"):
        PositionCapConfig(**fields)


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


def test_an_extended_window_is_refused_while_extended_windows_are_off(monkeypatch) -> None:
    monkeypatch.setattr(session_window, "EXTENDED_READY", False)
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
    end = session_part("regular", clock(at, is_open=True), CALENDAR)[0]
    assert end == datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK)
    with pytest.raises(RuntimeError, match="regular session is closed"):
        session_part("regular", clock(at, is_open=False), CALENDAR)[0]


@pytest.mark.parametrize(
    ("hour", "minute", "end"),
    [(5, 0, datetime(2026, 11, 10, 9, 30)), (17, 0, datetime(2026, 11, 10, 20, 0))],
)
def test_an_extended_window_ends_with_pre_market_or_after_hours(
    hour, minute, end, extended
) -> None:
    at = datetime(2026, 11, 10, hour, minute, tzinfo=NEW_YORK)
    found = session_part("extended", clock(at, is_open=False), CALENDAR)[0]
    assert found == end.replace(tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    ("at", "is_open", "message"),
    [
        (datetime(2026, 11, 10, 21, 0), False, "outside pre-market, after-hours and the overnight"),
        (datetime(2026, 11, 10, 11, 0), True, "regular session is open"),
        (datetime(2026, 11, 11, 5, 0), False, "not a trading day"),
    ],
)
def test_an_extended_window_refuses_overnight_the_session_and_other_days(
    at, is_open, message, extended
):
    with pytest.raises(RuntimeError, match=message):
        session_part("extended", clock(at.replace(tzinfo=NEW_YORK), is_open=is_open), CALENDAR)[0]


@pytest.mark.parametrize("hour", [5, 17])
@pytest.mark.parametrize(
    "missing",
    [("session_open",), ("session_close",), ("session_open", "session_close")],
)
def test_an_extended_window_is_refused_when_the_calendar_omits_its_session_bounds(
    hour, missing, extended
) -> None:
    """`Calendar.session_open` and `session_close` are `Optional[str]` in alpaca-py 0.45.0
    (`trading/models.py`); a day that omits either does not say when the extended session starts or ends,
    so the window is refused, naming the field, rather than given a bound of its own (20:00 was assumed).
    Sweep reading by Muse Code and DeepSeek Harness."""
    bounds: dict[str, str | None] = {"session_open": "0400", "session_close": "2000"}
    bounds.update(dict.fromkeys(missing))
    calendar = Calendar(date="2026-11-10", open="09:30", close="16:00", **bounds)
    at = datetime(2026, 11, 10, hour, 0, tzinfo=NEW_YORK)
    with pytest.raises(RuntimeError, match=f"no {' or '.join(missing)},"):
        session_part("extended", clock(at, is_open=False), calendar)[0]


def day(on: str, **changes: object) -> Calendar:
    """A trading day on Alpaca's calendar, the regular session 09:30-16:00 and the extended 04:00-20:00."""
    fields: dict = {
        "date": on,
        "open": "09:30",
        "close": "16:00",
        "session_open": "0400",
        "session_close": "2000",
    }
    fields.update(changes)
    return Calendar(**fields)


@pytest.mark.parametrize(
    ("at", "days", "end", "part"),
    [
        # Tuesday after 20:00: the overnight session of Wednesday's trade date
        (
            datetime(2026, 11, 10, 21, 0),
            ["2026-11-10", "2026-11-11"],
            datetime(2026, 11, 11, 4, 0),
            "overnight",
        ),
        # Wednesday before 04:00, the same session
        (
            datetime(2026, 11, 11, 2, 0),
            ["2026-11-11", "2026-11-12"],
            datetime(2026, 11, 11, 4, 0),
            "overnight",
        ),
        # Sunday evening opens Monday's overnight session
        (datetime(2026, 11, 15, 20, 30), ["2026-11-16"], datetime(2026, 11, 16, 4, 0), "overnight"),
        (
            datetime(2026, 11, 10, 5, 0),
            ["2026-11-10", "2026-11-11"],
            datetime(2026, 11, 10, 9, 30),
            "pre-market",
        ),
        (
            datetime(2026, 11, 10, 17, 0),
            ["2026-11-10", "2026-11-11"],
            datetime(2026, 11, 10, 20, 0),
            "after-hours",
        ),
    ],
)
def test_an_extended_window_runs_in_pre_market_after_hours_and_overnight(
    at, days, end, part
) -> None:
    """Alpaca's 24/5 page: overnight 8:00 PM - 4:00 AM ET, "on the evening before the trade date",
    Sunday to Friday, following the NYSE holiday calendar; it ends as the trade date's pre-market opens."""
    found = session_window.session_part(
        "extended", clock(at.replace(tzinfo=NEW_YORK), is_open=False), [day(d) for d in days]
    )
    assert found == (end.replace(tzinfo=NEW_YORK), part)


@pytest.mark.parametrize(
    ("at", "days", "message"),
    [
        # Friday evening: Saturday is no trade date, so no overnight session
        (
            datetime(2026, 11, 13, 21, 0),
            ["2026-11-13"],
            "outside pre-market, after-hours and the overnight",
        ),
        # the evening before a holiday: no overnight session either
        (
            datetime(2026, 11, 25, 21, 0),
            ["2026-11-25"],
            "outside pre-market, after-hours and the overnight",
        ),
        (datetime(2026, 11, 14, 12, 0), [], "not a trading day"),
    ],
)
def test_no_overnight_session_runs_before_a_day_that_is_not_a_trade_date(at, days, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        session_window.session_part(
            "extended", clock(at.replace(tzinfo=NEW_YORK), is_open=False), [day(d) for d in days]
        )


def test_an_overnight_window_needs_its_trade_dates_session_open() -> None:
    """The overnight session ends as the trade date's pre-market opens; a day without `session_open`
    gives it no end."""
    with pytest.raises(RuntimeError, match="2026-11-11 gives no session_open"):
        session_window.session_part(
            "extended",
            clock(datetime(2026, 11, 10, 21, 0, tzinfo=NEW_YORK), is_open=False),
            [day("2026-11-10"), day("2026-11-11", session_open=None)],
        )


def test_auto_takes_the_overnight_session_when_extended_windows_are_on(monkeypatch) -> None:
    monkeypatch.setattr(session_window, "EXTENDED_READY", True)
    found, why = session_window.resolve_session(
        clock(datetime(2026, 11, 10, 21, 0, tzinfo=NEW_YORK), is_open=False),
        [day("2026-11-10"), day("2026-11-11")],
    )
    assert (found, why) == ("extended", "outside the regular session, in its overnight part")


@pytest.mark.parametrize(
    ("part", "expected"),
    [("overnight", "boats"), ("regular", "iex")],
)
def test_a_window_streams_boats_overnight_and_iex_in_the_regular_session(part, expected) -> None:
    """ADR 0055: on paper `boats` delivered the overnight session's quotes, trades and bars."""
    assert session_window.window_feed(part) == expected


def test_a_part_with_no_feed_is_skipped_and_an_unknown_part_named(monkeypatch) -> None:
    monkeypatch.setitem(
        session_window.SESSION_FEEDS,
        "after-hours",
        session_window.SessionFeed(None, "probed on paper and no stream delivered"),
    )
    with pytest.raises(
        session_window.WindowSkipped, match="no after-hours window: probed on paper"
    ):
        session_window.window_feed("after-hours")
    with pytest.raises(RuntimeError, match="no feed is set for the 'midday' part"):
        session_window.window_feed("midday")


def test_each_part_of_a_session_names_its_feed_and_what_paper_has_shown_of_it() -> None:
    """One table holds, per part, the feed a window uses and whether paper has shown it delivering."""
    table = session_window.SESSION_FEEDS
    assert set(table) == {"regular", "pre-market", "after-hours", "overnight"}
    assert table["after-hours"].feed == "sip" and "2026-11-10" in table["after-hours"].observed
    assert table["overnight"].feed == "boats" and "2026-11-10" in table["overnight"].observed
    assert table["pre-market"].feed == "sip" and "2026-11-11" in table["pre-market"].observed
    assert table["regular"].feed == "iex" and "2026-11-10" in table["regular"].observed


@pytest.mark.parametrize(
    ("at", "part", "observed"),
    [
        (datetime(2026, 11, 10, 17, 0), "after-hours", "16:46 New York, 2026-11-10"),
        (datetime(2026, 11, 10, 5, 0), "pre-market", "04:25 New York, 2026-11-11"),
    ],
)
def test_a_part_with_no_feed_is_skipped_by_name_not_failed(
    monkeypatch, tmp_path, capsys, at, part, observed
) -> None:
    """The lead decided on 2026-11-11 that a part no feed has been seen to serve on paper is skipped,
    naming the part and the observation, rather than run to fail for no data every evening; after-hours
    on the probe of 2026-11-10, pre-market on the windows and the probe of 2026-11-11, until each was
    given sip (a row with no feed stands in for them here)."""
    monkeypatch.setitem(
        session_window.SESSION_FEEDS,
        part,
        session_window.SessionFeed(None, f"probed at {observed}"),
    )

    class Outside(Account):
        def get_clock(self) -> Clock:
            return clock(at.replace(tzinfo=NEW_YORK), is_open=False)

    monkeypatch.setattr(session_window, "paper_trading", lambda: Outside())
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    (tmp_path / "latest.json").write_text('{"result": "ok"}')
    for session, said in (("auto", "nothing to run"), ("extended", "skipped")):
        assert session_window.main(["--session", session, "--log-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert f"{said}: no {part} window" in out and observed in out
        assert json.loads((tmp_path / "latest.json").read_text()) == {"result": "ok"}
        heard = json.loads((tmp_path / "last-firing.json").read_text())
        assert heard["outcome"].startswith(f"{said}: no {part} window")


@pytest.mark.parametrize(
    ("at", "part"),
    [(datetime(2026, 11, 10, 4, 0), "pre-market"), (datetime(2026, 11, 10, 17, 0), "after-hours")],
)
def test_pre_market_and_after_hours_stream_sip_from_their_open(at, part) -> None:
    """The sip feed authenticated on paper on 2026-11-10, and authenticated and delivered on 2026-11-11,
    where IEX sent too few trades to warm the averages up (ADR 0055): both parts run on it, from the calendar's bounds."""
    found, why = session_window.resolve_session(
        clock(at.replace(tzinfo=NEW_YORK), is_open=False), CALENDAR
    )
    assert (found, why) == ("extended", f"outside the regular session, in its {part} part")
    assert session_window.window_feed(part) == "sip"


@pytest.mark.parametrize(
    ("at", "days", "expected"),
    [
        # Wednesday: Tuesday's regular close less the slow average's 20 bars and 5 spare
        (
            datetime(2026, 11, 11, 8, 30),
            ["2026-11-10", "2026-11-11"],
            datetime(2026, 11, 10, 15, 35),
        ),
        # Monday: Friday's, across the weekend
        (datetime(2026, 11, 9, 8, 30), ["2026-11-06", "2026-11-09"], datetime(2026, 11, 6, 15, 35)),
    ],
)
def test_a_pre_market_warm_up_starts_in_the_prior_session(at, days, expected) -> None:
    """A pre-market window's own session is too thin for the slow average (ADR 0044), so its warm-up
    starts the slow average's bars and the spare ones before the last trading day's regular close."""
    start = session_window.warm_up_start(
        "pre-market",
        at.replace(tzinfo=NEW_YORK),
        [day(d) for d in days],
        span=timedelta(minutes=25),
    )
    assert start == expected.replace(tzinfo=NEW_YORK)


def test_other_parts_warm_up_on_their_own_hour() -> None:
    at = datetime(2026, 11, 10, 11, 0, tzinfo=NEW_YORK)
    assert (
        session_window.warm_up_start("regular", at, [day("2026-11-09")], span=timedelta(minutes=25))
        is None
    )
    assert (
        session_window.warm_up_start(
            "overnight", at, [day("2026-11-09")], span=timedelta(minutes=25)
        )
        is None
    )


def test_a_pre_market_warm_up_with_no_prior_trading_day_on_the_calendar_is_refused() -> None:
    at = datetime(2026, 11, 11, 8, 30, tzinfo=NEW_YORK)
    with pytest.raises(RuntimeError, match="no trading day before 2026-11-11"):
        session_window.warm_up_start(
            "pre-market", at, [day("2026-11-11")], span=timedelta(minutes=25)
        )


def test_a_pre_market_window_with_no_prior_day_on_the_calendar_is_refused_not_failed(
    monkeypatch, tmp_path
) -> None:
    """Review finding: the warm-up's start was outside the refusals, so a calendar without the prior
    trading day failed the window as a `RuntimeError` instead of refusing it before anything ran."""

    class OnlyToday(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 11, 8, 30, tzinfo=NEW_YORK), is_open=False)

        def get_calendar(self, filters=None) -> list[Calendar]:
            return [day("2026-11-11")]

    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    with pytest.raises(session_window.WindowRefused, match="no trading day before 2026-11-11"):
        session_window.run_window(
            WindowConfig(session="extended", log_dir=tmp_path), trading=cast(Any, OnlyToday())
        )


def test_the_pre_market_warm_up_span_is_counted_in_the_bar_types_interval(
    monkeypatch, tmp_path
) -> None:
    """Review finding: the reach-back was counted in minutes whatever the bar type; five-minute bars need
    five times the span for the same 25 bars."""
    built = {}

    class PreMarket(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 11, 8, 30, tzinfo=NEW_YORK), is_open=False)

        def get_calendar(self, filters=None) -> list[Calendar]:
            return [day("2026-11-10"), day("2026-11-11")]

    def node(config, **kw):
        built.update(kw)
        raise RuntimeError("built")

    monkeypatch.setattr(session_window, "window_node", node)
    config = WindowConfig(session="extended", log_dir=tmp_path)
    monkeypatch.setattr(
        type(config),
        "bar_type",
        property(lambda self: BarType.from_str("SPY.ALPACA-5-MINUTE-LAST-INTERNAL")),
    )
    with pytest.raises(RuntimeError, match="built"):
        session_window.run_window(config, trading=cast(Any, PreMarket()))
    assert built["warm_up_start"] == datetime(2026, 11, 10, 13, 55, tzinfo=NEW_YORK)


def test_the_calendar_read_reaches_back_a_week_for_the_prior_session() -> None:
    asked = []

    class Reads(Account):
        def get_calendar(self, filters=None) -> list[Calendar]:
            asked.append(filters)
            return [CALENDAR]

    session_window.session_now(cast(Any, Reads()))
    assert (asked[0].start, asked[0].end) == (date(2026, 11, 3), date(2026, 11, 11))


def test_a_pre_market_window_hands_the_warm_up_start_to_its_node(monkeypatch, tmp_path) -> None:
    built = {}

    class PreMarket(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 11, 8, 30, tzinfo=NEW_YORK), is_open=False)

        def get_calendar(self, filters=None) -> list[Calendar]:
            return [day("2026-11-10"), day("2026-11-11")]

    def node(config, **kw):
        built.update(kw)
        raise RuntimeError("built")

    monkeypatch.setattr(session_window, "window_node", node)
    with pytest.raises(RuntimeError, match="built"):
        session_window.run_window(
            WindowConfig(session="extended", log_dir=tmp_path), trading=cast(Any, PreMarket())
        )
    assert built["feed"] == "sip"
    assert built["warm_up_start"] == datetime(2026, 11, 10, 15, 35, tzinfo=NEW_YORK)


def test_the_warm_up_start_reaches_the_strategy(tmp_path, extended) -> None:
    at = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)
    start = datetime(2026, 11, 10, 15, 35, tzinfo=NEW_YORK)
    node, s, _ = session_window.window_node(
        WindowConfig(session="extended", log_dir=tmp_path),
        wind_down_at=at,
        log_name="w",
        feed="iex",
        warm_up_start=start,
    )
    try:
        assert isinstance(s, SessionMaCross) and s.warm_up_start == start
    finally:
        node.dispose()


def test_a_part_given_a_feed_again_runs_its_windows(monkeypatch) -> None:
    """Review finding: with pre-market and after-hours skipped, nothing showed that a row given a feed
    again (a later probe that delivers) runs that part's windows on it."""
    monkeypatch.setattr(session_window, "EXTENDED_READY", True)
    monkeypatch.setitem(
        session_window.SESSION_FEEDS,
        "pre-market",
        session_window.SessionFeed("iex", "a later probe delivered"),
    )
    found, why = session_window.resolve_session(
        clock(datetime(2026, 11, 10, 8, 30, tzinfo=NEW_YORK), is_open=False), CALENDAR
    )
    assert (found, why) == ("extended", "outside the regular session, in its pre-market part")
    assert session_window.window_feed("pre-market") == "iex"


def test_an_overnight_window_needs_only_its_trade_dates_session_open() -> None:
    """Review finding: a trade date without `session_close`, or today without its bounds, refused the
    overnight session, whose end is the trade date's `session_open` alone."""
    found = session_window.session_part(
        "extended",
        clock(datetime(2026, 11, 10, 21, 0, tzinfo=NEW_YORK), is_open=False),
        [day("2026-11-10", session_close=None), day("2026-11-11", session_close=None)],
    )
    assert found == (datetime(2026, 11, 11, 4, 0, tzinfo=NEW_YORK), "overnight")


class Subscribing(Recording):
    """`Recording` that also keeps each subscription and the warm-up request, with its params, in order."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, dict | None]] = []

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.calls.append(("quotes", params))
        super().subscribe_quotes(instrument_id, client_id=client_id, params=params)

    def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
        self.calls.append(("trades", params))
        super().subscribe_trades(instrument_id, client_id=client_id, params=params)

    def subscribe_bars(self, bar_type, client_id=None, params=None) -> None:
        self.calls.append(("bars", params))
        super().subscribe_bars(bar_type, client_id=client_id, params=params)


@pytest.mark.parametrize(("feed", "params"), [("boats", {"feed": "boats"}), (None, None)])
def test_the_strategy_names_its_feed_in_quotes_and_trades_before_its_bars(feed, params) -> None:
    """The data engine hands the client only an instrument's first subscription (ADR 0055), so quotes
    and trades go first, on the feed; the bars built from those trades follow. Every quote is counted."""
    eng = engine()
    s = Subscribing(
        SessionMaCrossConfig(
            bar_type=BARS,
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            feed=feed,
            strategy_id=StrategyId("SessionMaCross-001"),
            order_id_tag="001",
        )
    )
    run(eng, s)
    eng.dispose()
    assert s.calls[:3] == [("quotes", params), ("trades", params), ("bars", None)]
    assert s.quotes == 60 and s.trades == 0  # the invented market has a quote a minute and no trade


def test_a_strategy_whose_instrument_is_missing_subscribes_nothing() -> None:
    """Review finding: quotes and trades went out before `MaCross`'s instrument check, so the client's one
    stream could start for a strategy that never ran."""
    eng = engine()
    s = Subscribing(
        SessionMaCrossConfig(
            bar_type=BarType.from_str("QQQ.ALPACA-1-MINUTE-LAST-INTERNAL"),
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            feed="boats",
            warm_up=True,
            strategy_id=StrategyId("SessionMaCross-001"),
            order_id_tag="001",
        )
    )
    s.request_bars = lambda *a, **k: pytest.fail("bars were requested")
    eng.add_strategy(s)
    with pytest.raises(RuntimeError, match="not in the cache"):
        s.on_start()
    eng.dispose()
    assert s.calls == []


def getconf_failing(monkeypatch, error: BaseException | str) -> None:
    """`subprocess.run` raising `error` for `getconf`, or answering it with `error` when it is a string;
    any other command runs as before."""
    real = subprocess.run

    def run(command, *args, **kwargs):
        if command != ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"]:
            return real(command, *args, **kwargs)
        if isinstance(error, BaseException):
            raise error
        return subprocess.CompletedProcess(command, 0, stdout=error, stderr="")

    monkeypatch.setattr(session_window.subprocess, "run", run)


@pytest.mark.parametrize(
    "error",
    [OSError("no such file"), subprocess.CalledProcessError(1, "getconf"), "\n", "T/\n"],
)
def test_the_records_folder_is_refused_on_macos_when_getconf_does_not_give_it(
    monkeypatch, tmp_path, error
) -> None:
    """On macOS the records go to the per-user folder `getconf DARWIN_USER_TEMP_DIR` names, whatever
    `$TMPDIR` says; one `getconf` does not give is refused by name, not swapped for `$TMPDIR`'s, where the
    gap marker and `scripts/automation status` would not look. Sweep reading by DeepSeek Harness."""
    monkeypatch.setattr(session_window.sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    session_window.tempfile.tempdir = None  # `gettempdir` caches; read `$TMPDIR` again
    try:
        getconf_failing(monkeypatch, error)
        with pytest.raises(RuntimeError, match="DARWIN_USER_TEMP_DIR"):
            session_window.default_log_dir()
    finally:
        session_window.tempfile.tempdir = None


def test_elsewhere_than_macos_the_records_folder_is_the_systems_temporary_one(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(session_window.sys, "platform", "linux")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    session_window.tempfile.tempdir = None
    try:
        getconf_failing(monkeypatch, OSError("no such file"))
        assert session_window.default_log_dir() == tmp_path / "canon-paper-session"
    finally:
        session_window.tempfile.tempdir = None


def test_a_records_folder_given_is_used_though_getconf_fails(monkeypatch, tmp_path) -> None:
    """`mise run paper:session` passes `--log-dir`; the default is not looked for then, so a failing
    `getconf` neither stops nor redirects that run."""
    monkeypatch.setattr(session_window.sys, "platform", "darwin")
    getconf_failing(monkeypatch, OSError("no such file"))
    assert session_window._args(["--log-dir", str(tmp_path)]).log_dir == tmp_path


def test_a_relative_records_folder_is_refused(monkeypatch, capsys) -> None:
    """`mise run paper:session` builds `--log-dir` from `getconf`; one that printed nothing leaves the bare
    name, a folder inside wherever the run started."""
    monkeypatch.setattr(session_window, "paper_trading", lambda: pytest.fail("Alpaca was read"))
    assert session_window.main(["--session", "auto", "--log-dir", "canon-paper-session"]) == 1
    assert "--log-dir must be an absolute folder" in capsys.readouterr().err


def test_a_run_whose_records_folder_cannot_be_found_says_so_and_exits_1(
    monkeypatch, tmp_path, capsys
) -> None:
    """Refused before anything reads Alpaca or writes a record."""
    monkeypatch.setattr(session_window.sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv(session_window.COMMIT_ENV, "test")
    session_window.tempfile.tempdir = None
    monkeypatch.setattr(session_window, "paper_trading", lambda: pytest.fail("Alpaca was read"))
    try:
        getconf_failing(monkeypatch, OSError("no such file"))
        assert session_window.main(["--session", "auto"]) == 1
    finally:
        session_window.tempfile.tempdir = None
    assert "DARWIN_USER_TEMP_DIR" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


LONG_ONLY: dict[str, Any] = {
    "no_shorting": True,
    "suspend_trade": False,
    "fractional_trading": True,
    "max_margin_multiplier": "4",
    "trade_confirm_email": "all",
    "ptp_no_exception_entry": False,
    "max_options_trading_level": 3,
    "disable_overnight_trading": False,
}
"""Alpaca's account configuration as `GET /v2/account/configurations` answers it since the fields it
stopped sending (alpaca-py 0.45.0's `AccountConfiguration`), on an account held long-only."""


class Account:
    """A stand-in for the paper `TradingClient`: a clock, a calendar day, what the account holds, and its
    configuration (long-only unless `no_shorting` says otherwise)."""

    def __init__(self, *, orders: list | None = None, no_shorting: bool = True) -> None:
        self.orders = orders or []
        self.no_shorting = no_shorting

    def get_account_configurations(self) -> AccountConfiguration:
        return AccountConfiguration.model_validate({**LONG_ONLY, "no_shorting": self.no_shorting})

    def get_clock(self) -> Clock:
        return clock(datetime(2026, 11, 10, 11, 0, tzinfo=NEW_YORK), is_open=True)

    def get_calendar(self, filters=None) -> list[Calendar]:
        return [CALENDAR]

    def get_orders(self, filter=None) -> list:
        return self.orders

    def get_all_positions(self) -> list:
        return []


def test_a_window_does_not_start_unless_the_account_is_flat(monkeypatch, tmp_path) -> None:
    held = SimpleNamespace(client_order_id="someone-elses", symbol="SPY")
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(orders=[held]))
    with pytest.raises(RuntimeError, match="not flat; not starting"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_a_window_waits_out_the_gap_after_the_last_one(tmp_path) -> None:
    import time

    (tmp_path / "last-window-end").write_text(str(time.time() - 10))
    with pytest.raises(RuntimeError, match="under 300 s ago"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_the_regular_window_node_runs_upstreams_managed_stop(tmp_path) -> None:
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    node, s, _ = session_window.window_node(
        WindowConfig(log_dir=tmp_path), wind_down_at=at, log_name="r", feed="iex"
    )
    try:
        assert isinstance(s, SessionMaCross)
        assert s.manage_stop and not s.extended_hours and s.warm_up
        assert s.feed_params == {"feed": "iex"}
        assert str(s.history_bar_type) == "SPY.ALPACA-1-MINUTE-LAST-EXTERNAL"
    finally:
        node.dispose()


def test_the_extended_window_node_builds_without_connecting(tmp_path, extended) -> None:
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    node, s, cap = session_window.window_node(
        WindowConfig(session="extended", log_dir=tmp_path),
        wind_down_at=at,
        log_name="t",
        feed="boats",
    )
    try:
        assert isinstance(s, SessionMaCross)
        assert s.extended_hours and not s.manage_stop and s.wind_down_at == at
        assert s.feed_params == {"feed": "boats"}
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
        self.history_seen: list[tuple] = []
        """The slow average's (count, initialized, value) as the warm-up's bars reach the callback,
        and again after it has run."""

    def on_historical_bars(self, bars) -> None:
        self.history_seen.append(self._slow_state())
        self.live_closes = [b.close.as_double() for b in reversed(self.cache.bars(LIVE) or [])]
        super().on_historical_bars(bars)
        self.history_seen.append(self._slow_state())

    def _slow_state(self) -> tuple:
        return (self.slow.count, self.slow.initialized, self.slow.value)

    def on_bar(self, bar) -> None:
        if bar.bar_type == LIVE:
            self.live_bars += 1
            if self.ready_at_first_bar is None:
                self.ready_at_first_bar = self.hydrated and self.indicators_initialized()
        super().on_bar(bar)


class LateAnswer(FirstBar):
    """Asks for the history again at its first live bar, so an answer comes back after a live bar has
    reached the averages, through upstream's own response path."""

    def on_bar(self, bar) -> None:
        first = bar.bar_type == LIVE and self.ready_at_first_bar is None
        super().on_bar(bar)
        if first:
            self.request_bars(self.history_bar_type, start=datetime.fromtimestamp(T0 / 1e9, UTC))


def warm_up_run(
    tmp_path,
    *,
    warm_up: bool,
    gap: int = 0,
    warm_up_start: datetime | None = None,
    kind: type[FirstBar] = FirstBar,
) -> FirstBar:
    """Thirty minutes of the adapter's EXTERNAL one-minute bars in a catalog, as `request_bars` would get
    them, then, `gap` minutes after them, twenty minutes of live trades every ten seconds, from which the
    data engine builds the INTERNAL bars the window trades on."""
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
    start = T0 + (30 + gap) * MINUTE
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
    s = kind(
        SessionMaCrossConfig(
            bar_type=LIVE,
            trade_size=Quantity.from_int(1),
            fast_period=3,
            slow_period=8,
            warm_up=warm_up,
            warm_up_start=warm_up_start,
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


def test_the_warm_up_reaches_back_to_its_start_across_a_gap_with_no_bars(tmp_path) -> None:
    """A window whose own part of the session is too thin for the slow average (pre-market, ADR 0044)
    warms up from `warm_up_start`, in the prior session: here bars that ended 15 hours before the live
    trades, all of them served and fed; without it the request's hour finds none of them."""
    at = datetime.fromtimestamp(T0 / 1e9, UTC)
    (tmp_path / "start").mkdir()
    (tmp_path / "hour").mkdir()
    reached = warm_up_run(tmp_path / "start", warm_up=True, gap=900, warm_up_start=at)
    assert reached.history_bars == 30 and reached.ready_at_first_bar is True
    hour = warm_up_run(tmp_path / "hour", warm_up=True, gap=900)
    assert hour.history_bars == 0 and hour.ready_at_first_bar is False


def test_upstream_feeds_the_warm_up_to_the_averages_and_the_callback_adds_nothing(tmp_path) -> None:
    """Upstream's response path updates the registered averages before `on_historical_bars` runs
    (`prepare_response`, `crates/common/src/python/response.rs` at the pin), routing by instrument and
    bar specification whatever the aggregation source (`BarType::id_spec_key`), so the EXTERNAL history
    reaches the averages registered for the INTERNAL bars. The callback must not feed them again: the
    slow average holds the last eight closes on its way in, and holds them unchanged on its way out."""
    s = warm_up_run(tmp_path, warm_up=True)
    last_eight = sum(750 + i / 100 for i in range(22, 30)) / 8
    assert s.history_seen[0][:2] == (8, True)
    assert s.history_seen[0][2] == pytest.approx(last_eight)
    assert s.history_seen[1] == s.history_seen[0]


def test_thin_history_leaves_the_slow_average_cold_rather_than_doubled(tmp_path) -> None:
    """Fewer history bars than `slow_period` (five against eight, the hour asked for reaching back over
    the last five): fed once, the slow average holds five bars and is not initialized, so the first live
    bar decides nothing. Fed twice it held them doubled, out of order, and reported itself initialized."""
    s = warm_up_run(tmp_path, warm_up=True, gap=55)
    assert s.history_bars == 5
    assert s.history_seen[0][:2] == (5, False)
    assert s.history_seen[1] == s.history_seen[0]
    assert s.ready_at_first_bar is False


def test_a_warm_up_start_without_the_warm_up_is_refused() -> None:
    """Review finding: a start given with `warm_up=False` was stored and ignored, the averages starting
    cold with nothing said."""
    with pytest.raises(ValueError, match="warm_up_start needs warm_up"):
        strategy(
            bar_type=LIVE,
            warm_up=False,
            warm_up_start=datetime(2026, 11, 10, 15, 35, tzinfo=NEW_YORK),
        )


def test_the_warm_up_start_must_be_an_aware_datetime() -> None:
    with pytest.raises(ValueError, match="warm_up_start must be an aware datetime"):
        strategy(bar_type=LIVE, warm_up=True, warm_up_start=datetime(2026, 11, 10, 15, 35))


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


def test_a_warm_up_answer_after_a_live_bar_is_fed_again_in_time_order(tmp_path) -> None:
    """Upstream applies a late answer to the averages before the callback, after newer live bars; the
    callback cannot stop that, so it resets them and feeds them again in time order: the history older
    than the first live bar, then the live bars. They stay initialized, with no wait for new bars."""
    s = warm_up_run(tmp_path, warm_up=True, kind=LateAnswer)
    # each answer's 30 bars counted, all older than the live bars, so none dropped
    assert s.history_bars == 60 and s.history_dropped == 0
    assert s.live_closes  # the live bar the averages had taken before the answer
    closes = [750 + i / 100 for i in range(30)] + s.live_closes
    count, initialized, value = s.history_seen[3]
    assert (count, initialized) == (8, True)
    assert value == pytest.approx(sum(closes[-8:]) / 8)


def test_the_callback_feeds_the_averages_nothing() -> None:
    """Called directly, with no upstream response path in front of it, the callback counts the bars and
    leaves the averages as they were."""
    s = unstarted()
    s.on_historical_bars(history(13))
    assert (s.history_bars, s.history_dropped, s.slow.count) == (13, 0, 0)


def test_a_warm_up_answer_after_the_timeout_with_no_live_bar_yet_is_kept() -> None:
    """After the timeout and before any live bar, upstream has applied the answer in order, oldest first,
    with nothing newer in the averages, so it is kept."""
    s = unstarted()
    s._on_history_timeout(cast(TimeEvent, None))
    assert s.hydrated
    s.on_historical_bars(history(13))
    assert (s.history_bars, s.history_dropped) == (13, 0)


def closes_bars(bar_type: BarType, minutes: range) -> list:
    """One-minute bars closing at `T0` plus each minute, each closing at 750 plus a cent a minute."""
    return [
        Bar(
            bar_type,
            Price(750, 4),
            Price(751, 4),
            Price(749, 4),
            Price(750 + i / 100, 4),
            Quantity.from_int(1),
            T0 + i * MINUTE,
            T0 + i * MINUTE,
        )
        for i in minutes
    ]


def mean_close(minutes) -> float:
    return sum(750 + i / 100 for i in minutes) / len(minutes)


def with_live(bars: list) -> SessionMaCross:
    """An unstarted strategy whose averages have taken `bars` as live bars, as upstream feeds them."""
    s = unstarted()
    for bar in bars:
        s.fast.handle_bar(bar)
        s.slow.handle_bar(bar)
        s.live_bars += 1  # what `on_bar` keeps, without the engine its order checks need
        s.taken.append(bar)
    s.first_live_ns = bars[0].ts_init
    return s


def test_a_warm_up_answer_out_of_time_order_is_fed_again_oldest_first() -> None:
    """Upstream feeds an answer in the order served; one out of order leaves the averages on the bars
    that came last, not the newest, so the callback feeds them again oldest first."""
    s = unstarted()
    s.on_historical_bars(list(reversed(closes_bars(HISTORY, range(13)))))
    assert (s.history_bars, s.history_dropped) == (13, 0)
    assert s.slow.initialized and s.slow.value == pytest.approx(mean_close(range(5, 13)))


def test_a_warm_up_answer_with_a_bar_twice_is_fed_again_once_per_bar() -> None:
    """A bar repeated (a page boundary served twice) is not in strict time order: fed again, it counts
    once, so five bars do not pass for the eight the slow average needs."""
    bars = closes_bars(HISTORY, range(5))
    s = unstarted()
    s.on_historical_bars(bars[:3] + bars[2:])
    assert (s.history_bars, s.slow.count, s.slow.initialized) == (5, 5, False)


def test_a_late_answer_overlapping_the_live_bars_drops_the_minutes_they_cover() -> None:
    """Live bars from minute 10 on; an answer for minutes 0 to 12 after them: minutes 0 to 9 of it and
    then the live bars are fed again, and its minutes 10 to 12 are dropped and counted."""
    s = with_live(closes_bars(LIVE, range(10, 13)))
    s.on_historical_bars(closes_bars(HISTORY, range(13)))
    assert (s.history_bars, s.history_dropped) == (10, 3)
    assert s.slow.count == 8 and s.slow.value == pytest.approx(mean_close(range(5, 13)))
    assert s.fast.value == pytest.approx(mean_close(range(10, 13)))


def test_an_answer_older_than_the_bars_already_taken_is_fed_again_in_time_order() -> None:
    """Two answers before any live bar, the second older than the first: in order on its own, but older
    than what the averages hold, so it is not kept as served; they are fed again and end on the newest."""
    s = unstarted()
    s.on_historical_bars(closes_bars(HISTORY, range(10, 20)))
    s.on_historical_bars(closes_bars(HISTORY, range(10)))
    assert s.slow.value == pytest.approx(mean_close(range(12, 20)))
    assert s.history_bars == 20 and s.history_dropped == 0


def test_an_empty_warm_up_answer_after_a_live_bar_leaves_the_averages_alone() -> None:
    s = unstarted()
    s.live_bars = 1
    s.slow.update_raw(750.0)
    s.on_historical_bars([])
    assert (s.history_bars, s.history_dropped, s.slow.count) == (0, 0, 1)


def test_the_warm_up_needs_time_bars() -> None:
    with pytest.raises(ValueError, match="needs time bars"):
        strategy(bar_type=BarType.from_str("SPY.ALPACA-100-TICK-LAST-INTERNAL"), warm_up=True)


@pytest.mark.parametrize(
    ("at", "is_open", "ready", "expected"),
    [
        (datetime(2026, 11, 10, 11, 0), True, False, "regular"),
        (datetime(2026, 11, 10, 17, 0), False, False, None),
        (datetime(2026, 11, 10, 5, 0), False, True, "extended"),  # pre-market, on sip
        (datetime(2026, 11, 10, 17, 0), False, True, "extended"),  # after-hours, on sip
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
        def get_clock(self) -> Clock:  # a Saturday
            return clock(datetime(2026, 11, 14, 17, 0, tzinfo=NEW_YORK), is_open=False)

    monkeypatch.setattr(session_window, "paper_trading", lambda: Closed())
    assert session_window.main(["--session", "auto", "--log-dir", str(tmp_path)]) == 0
    assert "nothing to run" in capsys.readouterr().out
    assert not (tmp_path / "latest.json").exists()


def test_a_refused_window_is_the_latest_record(monkeypatch, tmp_path) -> None:
    held = SimpleNamespace(client_order_id="someone-elses", symbol="SPY")
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

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0
    )
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: (Ran(), s, cap))
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)

    def unreachable(trading, config, report, **kwargs):
        raise RuntimeError("Alpaca answered 503")

    monkeypatch.setattr(session_window, "_flatten_after_stop", unreachable)
    with pytest.raises(RuntimeError, match="503"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"] == "failed: RuntimeError: Alpaca answered 503"


def quiet_window(
    monkeypatch,
    tmp_path,
    *,
    quotes,
    trades,
    live_bars=0,
    log=(),
    account=None,
    strategy="SessionMaCross",
    session="regular",
    mover=False,
):
    """A window of `strategy` whose node runs at once, its strategy having seen `quotes`, `trades` and
    `live_bars`, and its node log holding the lines `log`; returns the report or the error, and the
    latest record."""

    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [])

        def dispose(self) -> None:
            pass

    s = SimpleNamespace(
        dropped=[],
        history_bars=0,
        history_dropped=0,
        quotes=quotes,
        trades=trades,
        live_bars=live_bars,
    )
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    seen: dict = {}

    def node(config, **kw):
        seen.update(kw, config=config)
        (config.log_dir / f"{kw['log_name']}.log").write_text("\n".join(log))
        return Ran(), s, cap

    monkeypatch.setattr(session_window, "paper_trading", lambda: account or Account())
    monkeypatch.setattr(session_window, "window_node", node)
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    monkeypatch.setattr(session_window, "_flatten_after_stop", lambda *a, **k: [])
    try:
        outcome: Any = session_window.run_window(
            WindowConfig(log_dir=tmp_path, strategy=strategy, session=session, mover=mover)
        )
    except RuntimeError as e:
        outcome = e
    return outcome, json.loads((tmp_path / "latest.json").read_text()), seen


def test_a_quiet_market_with_quotes_and_no_bar_is_ok_and_shows_it(monkeypatch, tmp_path) -> None:
    """A symbol that trades nothing, overnight say, gives no bar; quotes show the stream delivered, so
    the window is ok, its record saying how much arrived."""
    _, latest, seen = quiet_window(monkeypatch, tmp_path, quotes=40, trades=0)
    assert latest["result"] == "ok"
    assert (latest["quotes"], latest["trades"], latest["live_bars"]) == (40, 0, 0)
    assert (latest["part"], latest["feed"], seen["feed"]) == ("regular", "iex", "iex")
    assert latest["stream"] == [] and latest["errors"] == []


def test_a_window_whose_stream_delivered_nothing_is_failed(monkeypatch, tmp_path) -> None:
    """alpaca-py retries a stream that never connects with warnings only, so nothing arrives: the
    record and the result say so, not a quiet market."""
    never = [
        (
            "2026-11-11T03:00:01.000000000Z [WARN] CANON-PAPER-SESSION.AlpacaDataClient: Alpaca stream "
            "on feed iex: error during websocket communication: server rejected WebSocket connection"
        )
    ]
    error, latest, _ = quiet_window(monkeypatch, tmp_path, quotes=0, trades=0, log=never)
    assert isinstance(error, RuntimeError) and "without market data" in str(error)
    assert latest["result"].startswith("failed: no market data: the iex stream delivered no quote")
    assert latest["stream"] == [never[0]]


class PreMarketAccount(Account):
    """The stand-in at 08:30 New York on a Wednesday, in pre-market, with Tuesday on the calendar."""

    def get_clock(self) -> Clock:
        return clock(datetime(2026, 11, 11, 8, 30, tzinfo=NEW_YORK), is_open=False)

    def get_calendar(self, filters=None) -> list[Calendar]:
        return [day("2026-11-10"), day("2026-11-11")]


@pytest.mark.parametrize(
    ("quotes", "log", "said"),
    [
        (
            0,
            [
                (
                    "2026-11-11T13:30:01.000000000Z [ERROR] CANON-PAPER-SESSION.AlpacaDataClient: "
                    "Alpaca stream on feed sip: auth failed"
                )
            ],
            "failed: the sip data stream reported an error: ",
        ),
        (0, [], "failed: no market data: the sip stream delivered no quote"),
    ],
)
def test_a_refused_or_empty_sip_fails_the_pre_market_window_by_name(
    monkeypatch, tmp_path, quotes, log, said
) -> None:
    """The lead decided on 2026-11-11 that a session's feed is one setting in `SESSION_FEEDS`: a sip that
    Alpaca refuses, or that delivers nothing, fails the window naming sip and Alpaca's own text, and the
    window streams no other feed."""
    error, latest, seen = quiet_window(
        monkeypatch,
        tmp_path,
        quotes=quotes,
        trades=0,
        log=log,
        account=PreMarketAccount(),
        session="extended",
    )
    assert isinstance(error, RuntimeError)
    assert latest["result"].startswith(said) and all(line in latest["result"] for line in log[:1])
    assert (latest["part"], latest["feed"], seen["feed"]) == ("pre-market", "sip", "sip")


def test_a_window_whose_stream_ended_by_itself_is_failed_though_data_came(monkeypatch, tmp_path):
    ended = [
        "2026-11-11T03:00:01.000000000Z [INFO] CANON-PAPER-SESSION.AlpacaDataClient: connected",
        (
            "2026-11-11T03:10:01.000000000Z [ERROR] CANON-PAPER-SESSION.AlpacaDataClient: Alpaca stream "
            "on feed iex ended by itself while subscribed (None); this client does not restart it"
        ),
    ]
    error, latest, _ = quiet_window(monkeypatch, tmp_path, quotes=500, trades=20, log=ended)
    assert isinstance(error, RuntimeError)
    assert latest["result"].startswith("failed: the iex data stream reported an error: ")
    assert latest["stream"] == [ended[1]]


def test_a_refused_subscription_fails_the_window_though_quotes_came(monkeypatch, tmp_path) -> None:
    """Review finding: a trade subscription the adapter refused (another feed than its stream's) left
    quotes flowing and no bar, read as a quiet market."""
    refused = [
        (
            "2026-11-11T03:00:01.000000000Z [ERROR] CANON-PAPER-SESSION.AlpacaDataClient: Cannot "
            "subscribe: this client runs one stream, on feed boats from its first subscription"
        )
    ]
    _, latest, _ = quiet_window(monkeypatch, tmp_path, quotes=50, trades=0, log=refused)
    assert latest["result"].startswith("failed: the iex data stream reported an error: ")
    assert latest["stream"] == refused


def test_a_warning_that_quotes_an_error_does_not_fail_the_window(monkeypatch, tmp_path) -> None:
    """Review finding: the level is the log line's own field, not any "[ERROR]" in its message."""
    warned = [
        (
            "2026-11-11T03:00:01.000000000Z [WARN] CANON-PAPER-SESSION.AlpacaDataClient: Alpaca stream "
            "on feed iex: server said [ERROR] slow consumer, reconnecting"
        )
    ]
    _, latest, _ = quiet_window(monkeypatch, tmp_path, quotes=50, trades=3, log=warned)
    assert latest["result"] == "ok" and latest["stream"] == warned


def test_an_unread_node_log_is_noted_not_taken_for_a_quiet_stream(monkeypatch, tmp_path) -> None:
    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [])

        def dispose(self) -> None:
            pass

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=5, trades=5, live_bars=1
    )
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(
        session_window,
        "window_node",
        lambda config, **kw: (Ran(), s, SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)),
    )
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    monkeypatch.setattr(session_window, "_flatten_after_stop", lambda *a, **k: [])
    report = session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert any("reading the node log" in e for e in report.errors)


class Overnight(Account):
    """The stand-in at 21:00 New York on a Tuesday, with Wednesday on the calendar and SPY's asset."""

    def __init__(self, asset: dict | str) -> None:
        super().__init__()
        self.asset = asset

    def get_clock(self) -> Clock:
        return clock(datetime(2026, 11, 10, 21, 0, tzinfo=NEW_YORK), is_open=False)

    def get_calendar(self, filters=None) -> list[Calendar]:
        return [day("2026-11-10"), day("2026-11-11")]

    def get(self, path, data=None, **kwargs):
        assert path == "/assets/SPY"
        if self.asset == "APIError":
            raise APIError(
                '{"code": 50010000, "message": "internal server error"}',
                SimpleNamespace(response=SimpleNamespace(status_code=500)),
            )
        return self.asset


@pytest.mark.parametrize(
    ("asset", "message"),
    [
        ({"symbol": "SPY", "attributes": []}, "SPY is not overnight_tradable"),
        ({"symbol": "SPY", "overnight_tradable": False}, "SPY is not overnight_tradable"),
        (
            {"symbol": "SPY", "overnight_tradable": True, "overnight_halted": True},
            "overnight_halted",
        ),
        (
            {"symbol": "SPY", "attributes": ["overnight_tradable", "overnight_halted"]},
            "overnight_halted",
        ),
        ("APIError", "SPY could not be read"),
    ],
)
def test_an_overnight_window_is_refused_on_an_asset_alpaca_does_not_trade_overnight(
    monkeypatch, tmp_path, extended, asset, message
) -> None:
    """Alpaca's 24/5 page: eligibility is the asset's `overnight_tradable`, with `overnight_halted` for
    one halted; a window refuses by name rather than place an order and learn from a rejection."""
    monkeypatch.setattr(session_window, "paper_trading", lambda: Overnight(asset))
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    with pytest.raises(session_window.WindowRefused, match=message):
        session_window.run_window(WindowConfig(session="extended", log_dir=tmp_path))


@pytest.mark.parametrize(
    "asset",
    [
        {"symbol": "SPY", "overnight_tradable": True},
        {"symbol": "SPY", "attributes": ["overnight_tradable"]},
    ],
)
def test_an_overnight_window_streams_boats(monkeypatch, tmp_path, extended, asset) -> None:
    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [])

        def dispose(self) -> None:
            pass

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=3, trades=0, live_bars=0
    )
    seen: dict = {}
    monkeypatch.setattr(session_window, "paper_trading", lambda: Overnight(asset))
    monkeypatch.setattr(
        session_window,
        "window_node",
        lambda config, **kw: (
            seen.update(kw) or (Ran(), s, SimpleNamespace(checks=1, peak=Decimal(0), tripped=None))
        ),
    )
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    monkeypatch.setattr(session_window, "_flatten_after_stop", lambda *a, **k: [])
    report = session_window.run_window(WindowConfig(session="extended", log_dir=tmp_path))
    assert (report.part, report.feed, seen["feed"]) == ("overnight", "boats", "boats")
    assert report.overnight == "overnight_tradable" and report.result == "ok"


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
    monkeypatch.setenv(session_window.SCHEDULED_ENV, "1")
    assert session_window.main(["--session", "auto", "--log-dir", str(tmp_path)]) == 0
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("nothing to run") and heard["commit"] == "d" * 40
    assert heard["scheduled"] is True


def test_a_heartbeat_says_whether_the_scheduled_runner_started_the_run(
    monkeypatch, tmp_path
) -> None:
    """A second reader's review: a window run by hand wrote the same heartbeat as a scheduled firing, so
    status could not tell the schedule had stopped. The runner stamps its own firings; the heartbeat says
    which kind of run wrote it, so status can name a run by hand. A second reader's review of that: the
    runner run by hand sets `CANON_COMMIT` too, so only the plist's `CANON_PAPER_SCHEDULED=1` says the
    schedule started it."""
    monkeypatch.delenv(session_window.SCHEDULED_ENV, raising=False)
    monkeypatch.setenv(session_window.COMMIT_ENV, "e" * 40)  # the runner, run by hand, says so
    monkeypatch.setenv(session_window.BY_HAND_ENV, "1")
    session_window.write_heartbeat(tmp_path, "a regular window")
    assert json.loads((tmp_path / "by-hand" / "last-firing.json").read_text())["scheduled"] is False
    monkeypatch.delenv(
        session_window.BY_HAND_ENV
    )  # a runner installed before the plist set SCHEDULED_ENV
    session_window.write_heartbeat(tmp_path, "a regular window")
    assert json.loads((tmp_path / "last-firing.json").read_text())["scheduled"] is True
    monkeypatch.setenv(session_window.SCHEDULED_ENV, "1")
    session_window.write_heartbeat(tmp_path, "a regular window")
    assert json.loads((tmp_path / "last-firing.json").read_text())["scheduled"] is True


def test_a_record_is_written_through_a_temporary_file_of_its_own(tmp_path) -> None:
    """A second reader's review: the runner's failure record and a window's heartbeat went through one
    temporary name, so two writers at once could publish each other's text. Each now has its own."""
    (tmp_path / ".last-firing.json.tmp").mkdir()  # the name both used, held by another writer
    session_window.write_heartbeat(tmp_path, "a regular window")
    assert json.loads((tmp_path / "last-firing.json").read_text())["outcome"] == "a regular window"
    session_window.write_record(tmp_path, {"result": "ok"})
    assert json.loads((tmp_path / "latest.json").read_text()) == {"result": "ok"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".last-firing.json.tmp",
        "last-firing.json",
        "latest.json",
    ]


def test_a_run_by_hand_writes_its_records_apart_and_keeps_the_gap_shared(
    monkeypatch, tmp_path
) -> None:
    """A second reader's review: a run by hand shared the schedule's records, so one after hours left the
    row REFUSED all night. `mise run paper:session` marks a run the schedule did not start; its records go to
    `by-hand/`, while the gap marker stays in the log folder every run reads."""
    monkeypatch.setenv(session_window.BY_HAND_ENV, "1")
    (tmp_path / "latest.json").write_text('{"result": "ok"}')
    session_window.write_record(tmp_path, {"result": "refused: the regular session is closed"})
    session_window.write_heartbeat(tmp_path, "refused: the regular session is closed")
    assert json.loads((tmp_path / "latest.json").read_text()) == {"result": "ok"}
    assert not (tmp_path / "last-firing.json").exists()
    assert json.loads((tmp_path / "by-hand" / "latest.json").read_text())["result"].startswith(
        "refused"
    )
    assert (tmp_path / "by-hand" / "last-firing.json").exists()
    (tmp_path / "last-window-end").write_text("1")  # a scheduled window just ended
    assert (
        session_window.main(["--log-dir", str(tmp_path)]) == 0
    )  # skipped inside the gap, by hand too
    monkeypatch.delenv(session_window.BY_HAND_ENV)
    session_window.write_record(tmp_path, {"result": "ok"})
    assert session_window.records_dir(tmp_path) == tmp_path


def _paper_task(name: str) -> tuple[int, int, int]:
    """(mise `timeout`, the body's `deadline`, its lock wait) of a paper task in `mise.toml`, in seconds."""
    task = tomllib.loads((session_window.SOURCE_ROOT / "mise.toml").read_text())["tasks"][name]
    timeout = task["timeout"]
    secs = int(timeout[:-1]) * {"s": 1, "m": 60, "h": 3600}[timeout[-1]]
    deadline = re.search(r"^deadline=(\d+)$", task["run"], re.MULTILINE)
    wait = re.search(r"^lockwait=(\d+)$", task["run"], re.MULTILINE)
    assert deadline is not None and wait is not None
    return secs, int(deadline.group(1)), int(wait.group(1))


# Counted on top of the lock wait and the start-up: the four Alpaca calls before the node runs (clock, calendar,
# open orders, position), each at its longest with alpaca-py's retries, though the window's end is timed from
# the first; and a host clock behind Alpaca's by up to `session_window.SKEW_SECS`.
START_CALLS = 4


def test_the_paper_session_deadline_covers_its_lock_wait_start_window_and_bounded_end() -> None:
    """A second reader's review: the deadline (85 minutes) was the lock wait plus the 70-minute stale ceiling,
    with nothing for uv's sync or the start, so a window of full length could be killed in mid-flatten. Now:
    the lock wait, the start-up, the calls before the node with their retries, the longest window and its
    bounded end all fit before uv's process group is killed, read from session_window's own bounds so the test
    follows them whatever they become; mise's own timeout is 60 s after that.

    Two second readers' review of that: the start-up was an allowance nothing held, so a slow sync ate the time
    the bounded end needs. Now the task holds uv's sync to its `sync` bound, read here from mise.toml (measured
    2026-11-10: 8 s with an empty uv cache, downloading 464 MB, 4 s for a new .venv from the cache, 0 s for one
    in step; 600 s covers that download at under 1 MB/s), and the imports after it are
    `session_window.START_UP_SECS`, which the shell cannot hold (ADR 0050)."""
    timeout, deadline, wait = _paper_task("paper:session")
    tasks = tomllib.loads((session_window.SOURCE_ROOT / "mise.toml").read_text())["tasks"]
    run = tasks["paper:session"]["run"]
    sync = re.search(r"^sync=(\d+)$", run, re.MULTILINE)
    assert sync is not None, "paper:session holds its start-up to a `sync=` bound"
    assert re.search(r'^  bounded "\$b" uv run .* /usr/bin/true \\$', run, re.MULTILINE)
    after_start = (
        session_window.ADAPTER_CALL_SECS
        + session_window.LONGEST_WINDOW_SECS
        + session_window.AFTER_END_SECS
    )
    assert deadline == timeout - 60
    start = (
        int(sync.group(1))
        + session_window.START_UP_SECS
        + (START_CALLS - 1) * session_window.ADAPTER_CALL_SECS
        + session_window.SKEW_SECS
    )
    assert deadline >= wait + start + after_start
    # the other way round: a window that runs as it should, from the stream lock to its end, fits a paper
    # test's wait for that lock, which is as long as its wait for account A's (ADR 0069, ADR 0050)
    test_timeout, test_deadline, test_wait = _paper_task("paper:test")
    assert (
        session_window.START_UP_SECS + after_start + session_window.SKEW_SECS
        <= test_wait
        == session_window.PAPER_LOCK_WAIT_SECS
    )
    assert test_deadline == test_timeout - 60 and test_deadline > test_wait
    # a record written `running` after the first clock read is not called dead while its window runs as it should
    assert session_window.STALE_RUNNING_SECS >= (
        session_window.LONGEST_WINDOW_SECS + session_window.AFTER_END_SECS
    )


# The login's market-data stream lock (ADR 0069): one block in both conftests, which a paper test that streams
# takes from its fixture, and a window from paper:session's body, after account B's lock.
CONFTESTS = ("tests/conftest.py", "packages/canon-alpaca/tests/conftest.py")
STREAMING = re.compile(
    r"subscribe_quotes|subscribe_trades|subscribe_bars|DataStream|ExecTester|DataTester"
    r"|ema_cross_paper_node|window_node"
)


def _stream_block(text: str) -> str:
    start = text.index("# --- The login's market-data stream lock (ADR 0069).")
    return text[start : text.index("# --- End of the stream lock block. ---", start)]


def _conftest() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "stream_lock_conftest", session_window.SOURCE_ROOT / CONFTESTS[0]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_conftests_hold_one_stream_lock_block_whose_wait_is_the_paper_tests() -> None:
    """The two conftests carry the same block, and its wait is paper:test's wait for account A's lock and
    `session_window.PAPER_LOCK_WAIT_SECS`, the bound a window that runs as it should fits."""
    blocks = [_stream_block((session_window.SOURCE_ROOT / c).read_text()) for c in CONFTESTS]
    assert blocks[0] == blocks[1]
    _, _, test_wait = _paper_task("paper:test")
    assert f"\nSTREAM_LOCK_WAIT_SECS = {test_wait}\n" in blocks[0]
    assert test_wait == session_window.PAPER_LOCK_WAIT_SECS


def test_every_paper_test_module_that_streams_is_marked_to_hold_the_stream_lock() -> None:
    """A paper module that subscribes to market data, through a strategy, upstream's testers or a node builder
    that adds one, or builds a stream carries the `alpaca_stream` marker, so each of its tests takes the
    stream lock. A data client alone opens no stream (it builds one at its first subscription), and the
    fixture fails any unmarked test that starts one on paper."""
    root = session_window.SOURCE_ROOT
    paper = sorted(
        [*root.glob("tests/test_paper_*.py"), *root.glob("packages/*/tests/test_paper_*.py")]
    )
    streaming = [p for p in paper if STREAMING.search(p.read_text())]
    assert len(streaming) >= 5
    unmarked = [p.name for p in streaming if "pytest.mark.alpaca_stream" not in p.read_text()]
    assert unmarked == []


def test_the_stream_lock_waits_for_another_holder_and_gives_up_at_its_bound(tmp_path: Path) -> None:
    """Held by another process (lockf(1), as paper:session takes it, or, where there is none, a Python
    flock): the fixture's call gives up after its wait; once free it takes the lock, which lockf then cannot
    take, until the descriptor is closed."""
    conftest = _conftest()
    lock = tmp_path / "canon-alpaca-stream.lock"
    if Path("/usr/bin/lockf").exists():
        holder = subprocess.Popen(["/usr/bin/lockf", "-k", str(lock), "sleep", "30"])
    else:  # GitHub's Ubuntu runner has no lockf(1)
        hold = "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT); fcntl.flock(fd,fcntl.LOCK_EX); time.sleep(30)"
        holder = subprocess.Popen([sys.executable, "-c", hold, str(lock)])
    try:
        for _ in range(100):
            fd = conftest.take_stream_lock(str(lock), 0)
            if fd is None:
                break
            os.close(fd)
            time.sleep(0.05)
        else:
            pytest.fail("the holder never took the lock")
        started = datetime.now(UTC)
        assert conftest.take_stream_lock(str(lock), 1.5) is None
        assert (datetime.now(UTC) - started).total_seconds() >= 1.5
    finally:
        holder.kill()
        holder.wait()
    fd = conftest.take_stream_lock(str(lock), 5)
    assert fd is not None
    try:
        if Path("/usr/bin/lockf").exists():
            took = subprocess.run(
                ["/usr/bin/lockf", "-k", "-t", "0", str(lock), "true"], check=False
            )
            assert took.returncode != 0
    finally:
        os.close(fd)
    if Path("/usr/bin/lockf").exists():
        took = subprocess.run(["/usr/bin/lockf", "-k", "-t", "0", str(lock), "true"], check=False)
        assert took.returncode == 0


def test_a_paper_test_not_marked_cannot_start_a_market_data_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the marker, alpaca-py's stream is stopped in `run()`, before it connects, and named for the
    fixture to fail the test with."""
    from alpaca.data.live.stock import StockDataStream

    started = _conftest().refuse_streams(monkeypatch)
    stream = StockDataStream("PKMADEUPMADEUP", "made-up")
    with pytest.raises(RuntimeError, match="not marked alpaca_stream"):
        stream.run()
    assert started == ["StockDataStream"]


def test_a_streaming_test_waits_for_the_lock_only_while_room_before_the_deadline_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second reader's finding (/code-review, 2026-11-11): a test could wait 3900 s for account A's lock and
    then 3900 s for the stream lock, past paper:test's deadline, which kills it part way through, its cleanup
    unrun. The wait now stops `TEST_ROOM_SECS` before the deadline paper:test exports, and with none named
    the test fails before it places anything."""
    conftest = _conftest()
    room, most = conftest.TEST_ROOM_SECS, conftest.STREAM_LOCK_WAIT_SECS
    monkeypatch.setenv(conftest.DEADLINE_ENV, "100000")
    assert conftest.stream_wait_secs(100000 - room - most - 10) == most
    assert conftest.stream_wait_secs(100000 - room - 600) == 600
    assert conftest.stream_wait_secs(100000 - room + 1) < 0
    monkeypatch.delenv(conftest.DEADLINE_ENV)
    with pytest.raises(pytest.fail.Exception, match="CANON_PAPER_TEST_DEADLINE is not set"):
        conftest.stream_wait_secs(0)
    # and paper:test exports it, its deadline from its start
    run = tomllib.loads((session_window.SOURCE_ROOT / "mise.toml").read_text())["tasks"][
        "paper:test"
    ]["run"]
    assert "CANON_PAPER_TEST_DEADLINE=$((start + deadline))" in run


def test_the_stream_lock_follows_the_tests_streams_until_each_has_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second reader's finding (/code-review, 2026-11-11): the lock went at the test's teardown while a stream
    thread that outlived its node's stop could still be connected. Each stream a marked test starts is now
    tracked to the end of its `run()`, which the teardown waits for before it lets the lock go."""
    import threading

    from alpaca.data.live.stock import StockDataStream

    release = threading.Event()
    monkeypatch.setattr(
        "alpaca.data.live.websocket.DataStream.run", lambda stream: release.wait(10)
    )
    ended = _conftest().track_streams(monkeypatch)
    stream = StockDataStream("PKMADEUPMADEUP", "made-up")
    thread = threading.Thread(target=stream.run)
    thread.start()
    try:
        for _ in range(100):
            if ended:
                break
            time.sleep(0.01)
        assert len(ended) == 1 and not ended[0].wait(0.2)
    finally:
        release.set()
        thread.join(5)
    assert ended[0].is_set()


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
    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0
    )
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
        lambda trading, config, report, **kwargs: checked.append("checked") or [],
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


@pytest.fixture
def fake_time(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(session_window, "time", fake)
    return fake


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
    held = SimpleNamespace(client_order_id="someone-elses", symbol="SPY")
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


class Timeouts(SessionMaCross):
    """Calls `_on_limit_timeout` for an order given by the test, with the cancel recorded."""

    def __init__(self, config: SessionMaCrossConfig) -> None:
        super().__init__(config)
        self.test_order = None
        self.canceled: list[str] = []
        self.rearmed: list[str] = []

    def _order(self, client_order_id):
        return self.test_order

    def _rearm(self, name: str) -> None:  # an unregistered strategy has no clock
        self.rearmed.append(name)

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.canceled.append(str(client_order_id))


def timed_out(side: OrderSide, client_order_id: str = "O-1"):
    from nautilus_trader.model import ClientOrderId

    return SimpleNamespace(
        is_closed=False,
        is_open=True,
        is_pending_cancel=False,
        side=side,
        client_order_id=ClientOrderId(client_order_id),
        filled_qty=Quantity.from_int(0),
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

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0
    )
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


# Two second readers' reviews of the window (2026-11-10): each shown here before its fix.


class Answering(HTTPAdapter):
    """A requests adapter that answers every request with one status code and counts what it sent."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.sent = 0

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.sent += 1
        response = Response()
        response.status_code = self.status
        response._content = b'{"code": 50400000, "message": "gateway timeout"}'
        response.url = request.url
        response.request = request
        return response


def test_the_windows_own_alpaca_client_sends_a_request_once(monkeypatch) -> None:
    """alpaca-py repeats a request answered 429 or 504 (`RESTClient._request`), three times, three
    seconds apart, outside the check's deadline and its reading of Alpaca's clock: three slow 504s gave
    a fourth market close after the session had closed. The window's client sends each request once;
    the check repeats it under its own deadline."""
    from alpaca.common import rest
    from canon_alpaca.providers import session_of

    slept: list[float] = []
    monkeypatch.setattr(rest.time, "sleep", slept.append)
    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    trading = session_window.paper_trading()
    session = session_of(trading)
    assert session is not None
    answering = Answering(504)
    session.mount("https://", answering)
    session_window.send_once(trading)
    with pytest.raises(APIError):
        trading.submit_order(
            MarketOrderRequest(symbol="SPY", qty=1, side="sell", time_in_force="day")
        )
    assert answering.sent == 1 and slept == []


def test_the_account_check_sends_each_request_once(monkeypatch) -> None:
    from alpaca.common import rest
    from canon_alpaca.providers import session_of

    slept: list[float] = []
    monkeypatch.setattr(rest.time, "sleep", slept.append)
    monkeypatch.setattr(session_window, "AFTER_STOP_SECS", 0.05)
    monkeypatch.setattr(session_window, "POLL_SECS", 0.01)
    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    trading = session_window.paper_trading()
    session = session_of(trading)
    assert session is not None
    answering = Answering(504)
    session.mount("https://", answering)
    report = session_window.WindowReport("regular", "SPY", started="t")
    with pytest.raises(RuntimeError, match="could not be read"):
        session_window._flatten_after_stop(trading, WindowConfig(), report)
    # alpaca-py's retry would wait `DEFAULT_RETRY_WAIT_SECONDS` (3) between sends; only polls waited
    assert answering.sent >= 1 and set(slept) == {0.01}


class Delayed:
    """A node whose stop hangs while a REST call it started in a worker thread (as the adapter's `_call`
    does, shielded) reaches the venue only after the window has given up on the stop."""

    cache = SimpleNamespace(orders=lambda **kwargs: [])

    def __init__(self, account, delay: float) -> None:
        self.account = account
        self.delay = delay
        self.disposed = False

    def handle(self):
        return SimpleNamespace(stop=lambda: None)

    def _submit(self) -> None:
        import time

        time.sleep(self.delay)
        self.account.orders.append(
            SimpleNamespace(id="late-1", client_order_id="entry-late", symbol="SPY")
        )

    async def run_async(self) -> None:
        import asyncio

        asyncio.ensure_future(asyncio.shield(asyncio.to_thread(self._submit)))
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    def dispose(self) -> None:
        self.disposed = True


class Late(Account):
    """`Account`, whose open orders change, and whose cancel lands at once."""

    def __init__(self) -> None:
        super().__init__(orders=[])
        self.canceled: list[str] = []

    def cancel_order_by_id(self, order_id) -> None:
        self.canceled.append(order_id)
        self.orders = [o for o in self.orders if o.id != order_id]


def test_a_call_the_abandoned_node_left_running_is_waited_for_before_the_check(
    monkeypatch, tmp_path
) -> None:
    """After an abandoned stop the check read the account at once, while the adapter's shielded REST
    call still ran in its worker thread; an order it sent after the flat read was left outside the
    check."""
    import time

    account = Late()
    node = Delayed(account, delay=1.0)
    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "STOP_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(session_window, "ADAPTER_CALL_SECS", 10.0)
    monkeypatch.setattr(session_window, "POLL_SECS", 0.05)
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: ran_node(node))
    with pytest.raises(session_window.StopTimedOut):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path), trading=cast(TradingClient, account)
        )
    time.sleep(1.2)  # long past the late call, which the check must have waited for
    assert account.canceled == ["late-1"] and account.orders == []


def test_a_call_after_the_stop_was_abandoned_cannot_start() -> None:
    """Once the stop is abandoned, the node's loop takes no new worker call: its executor is shut."""
    import asyncio

    calls = session_window.Calls()
    loop = asyncio.new_event_loop()
    try:
        loop.set_default_executor(calls)
        calls.shutdown(wait=False, cancel_futures=True)
        with pytest.raises(RuntimeError, match="after shutdown"):
            loop.run_until_complete(asyncio.to_thread(lambda: None))
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("dropped", "fast", "working", "held"),
    [
        (OrderSide.BUY, 1.0, True, Decimal(0)),  # the dropped entry's cancel still pending
        (OrderSide.SELL, 3.0, True, Decimal(1)),  # the dropped close's cancel still pending
        (OrderSide.BUY, 1.0, False, Decimal(-1)),  # a short, closed before anything else
    ],
    ids=["entry-cancel-pending", "close-cancel-pending", "short-found"],
)
def test_an_opposite_cross_on_any_bar_clears_the_dropped_signal(
    dropped, fast, working, held
) -> None:
    """A bar crossing the other way while the dropped limit's cancel was pending (or with a short to
    close) returned before the dropped side was cleared, so the next regime's signal was never sent."""
    s = unstarted_timeouts(timed_out(dropped))
    s.dropped_side = dropped
    s.working = lambda: working
    s.held = lambda: held
    s._close = lambda: None
    s.is_exiting = lambda: False
    s.indicators_initialized = lambda: True
    probe = cast(Any, s)
    probe.fast, probe.slow = SimpleNamespace(value=fast), SimpleNamespace(value=2.0)
    s.on_bar(cast(Bar, SimpleNamespace(bar_type=s.bar_type, ts_init=T0)))
    assert s.dropped_side is None


def test_the_post_end_bound_fits_the_end_margin_the_stale_record_and_the_paper_lock() -> None:
    """After a window's end: the stop at its bound, the abandoned node's calls at their longest, and the
    account check with one request past its deadline. The end margin covers the last close's clock read
    and a minute of skew; a record goes stale, and a paper test waiting for the lock gives up, only after
    the longest window and all of that."""
    import re
    import tomllib

    w = session_window
    before = w.STOP_TIMEOUT_SECS + w.ADAPTER_CALL_SECS + w.AFTER_STOP_SECS
    reading = (w.LOOKUPS + 2) * w.REQUEST_SECS  # its lookups, the open orders and the position
    last_close = before + reading + w.REQUEST_SECS  # the last poll's reading and its clock read
    assert last_close == w.LAST_CLOSE_SECS
    # that poll at its longest (a reading, the clock, its cancels), then the last reading, as long
    after_end = before + 2 * (reading + (1 + w.CANCELS) * w.REQUEST_SECS)
    assert after_end == w.AFTER_END_SECS
    assert w.END_MARGIN_SECS >= last_close + 60
    assert w.LONGEST_WINDOW_SECS + w.SKEW_SECS + after_end < w.STALE_RUNNING_SECS
    tasks = tomllib.loads((Path(__file__).parents[1] / "mise.toml").read_text())["tasks"]
    found = re.search(r"^lockwait=(\d+)$", tasks["paper:test"]["run"], re.MULTILINE)
    assert found is not None and int(found.group(1)) == w.PAPER_LOCK_WAIT_SECS
    longest = (
        w.START_UP_SECS + w.ADAPTER_CALL_SECS + w.LONGEST_WINDOW_SECS + w.SKEW_SECS + after_end
    )
    assert longest <= w.PAPER_LOCK_WAIT_SECS
    # and inside `paper:session`'s own deadline, which counts its wait for the lock
    session = tasks["paper:session"]
    waits = re.search(r"^lockwait=(\d+)$", session["run"], re.MULTILINE)
    deadline = re.fullmatch(r"(\d+)m", session["timeout"])
    assert waits is not None and deadline is not None
    assert int(waits.group(1)) + longest <= int(deadline.group(1)) * 60


def test_a_window_is_shortened_to_fit_the_lock_wait_and_says_so() -> None:
    """A window of the 60 minutes `--minutes` allows runs `LONGEST_WINDOW_SECS` (about 49 minutes), and
    its record says why; one the session clips says that instead; a scheduled 30 is not shortened."""
    w = session_window
    assert 45 * 60 < w.LONGEST_WINDOW_SECS < 60 * 60
    seconds, why = w.window_seconds(60, session_left=4 * 3600)
    assert seconds == w.LONGEST_WINDOW_SECS
    assert why.startswith("shortened from 60 minutes") and "3900 s wait" in why
    seconds, why = w.window_seconds(30, session_left=600)
    assert seconds == 600 - w.END_MARGIN_SECS and "before the session does" in why
    assert w.window_seconds(30, session_left=4 * 3600) == (1800, "")


# A third second reader's review of the window (2026-11-10): each shown here before its fix.


class Builder:
    """A stand-in for `LiveNode.builder`'s chain that records each setting and builds a stand-in node."""

    def __init__(self, *args) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def setting(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return setting

    def build(self):
        return SimpleNamespace(add_strategy=lambda strategy: None)


def test_a_managed_stop_has_the_post_stop_delay_its_market_exit_needs(
    monkeypatch, tmp_path
) -> None:
    """The node processes events after the trader's stop only for `delay_post_stop`, then disconnects
    (`LiveNode::stop`, `process_runner_for`, at the pin); the managed stop's market exit runs in that
    time, so at 5 s it was cut off after five attempts and the strategy never stopped."""
    built: list[Builder] = []

    def builder(*args, delay_post_stop_secs: int = 5, **kwargs) -> Builder:
        built.append(Builder())
        built[-1].calls.append(("with_delay_post_stop_secs", (delay_post_stop_secs,)))
        return built[-1]

    monkeypatch.setattr(session_window, "alpaca_paper_builder", builder)
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    _, s, _ = session_window.window_node(
        WindowConfig(log_dir=tmp_path), wind_down_at=at, log_name="r", feed="iex"
    )
    assert isinstance(s, SessionMaCross) and s.manage_stop
    delays = [args[0] for name, args in built[-1].calls if name == "with_delay_post_stop_secs"]
    exit_secs = (
        session_window.MARKET_EXIT_INTERVAL_MS * session_window.MARKET_EXIT_MAX_ATTEMPTS / 1000
    )
    assert delays and delays[-1] >= exit_secs + session_window.DELAY_POST_STOP_SECS
    assert session_window.STOP_TIMEOUT_SECS >= (
        delays[-1]
        + session_window.TIMEOUT_DISCONNECTION_SECS
        + session_window.TIMEOUT_SHUTDOWN_SECS
        + session_window.REQUEST_SECS
    )


@pytest.mark.parametrize("where", ["auto", "window"])
def test_a_network_error_leaves_a_failed_heartbeat(monkeypatch, tmp_path, where) -> None:
    """`_main` caught `APIError` but not the HTTP layer's errors (an `OSError`): a connect timeout left
    the heartbeat unwritten, or at "a regular window", while the run exited with a traceback."""

    def unreachable(*args, **kwargs):
        raise requests.ConnectTimeout("connect timed out")

    if where == "auto":
        monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
        monkeypatch.setattr(session_window, "session_now", unreachable)
        argv = ["--session", "auto", "--log-dir", str(tmp_path)]
    else:
        monkeypatch.setattr(session_window, "run_window", unreachable)
        argv = ["--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 1
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("failed: ConnectTimeout")


def rearming(order) -> Timeouts:
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


def test_a_limit_whose_cancel_did_not_land_is_canceled_again() -> None:
    """The timeout canceled once and set no further alert: a cancel rejected or unanswered left the order
    working for good, every later bar returned at the guard, and the stop skipped the close."""
    s = rearming(timed_out(OrderSide.SELL))
    s.signal_orders.add("O-1")
    event = cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1"))
    s._on_limit_timeout(event)
    assert s.canceled == ["O-1"] and s.rearmed == ["limit-timeout-O-1"]
    s._on_limit_timeout(event)  # open again: the cancel was rejected
    assert s.canceled == ["O-1", "O-1"] and len(s.rearmed) == 2
    assert len(s.dropped) == 1 and s.dropped_side == OrderSide.SELL
    s.test_order = SimpleNamespace(is_closed=True, filled_qty=Quantity.from_int(0))
    s._on_limit_timeout(event)
    assert len(s.rearmed) == 2 and s.signal_orders == set()


def test_a_partly_filled_close_that_times_out_does_not_hold_back_the_rest() -> None:
    """A close that filled in part and timed out held the dropped side, so the shares left were not
    closed for the rest of the cross; a partly filled limit is no dropped signal."""
    order = timed_out(OrderSide.SELL)
    order.filled_qty = Quantity.from_int(1)
    s = rearming(order)
    s.signal_orders.add("O-1")
    s._on_limit_timeout(cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1")))
    assert s.canceled == ["O-1"] and s.dropped_side is None


def test_a_close_that_filled_in_part_while_its_cancel_was_on_its_way_drops_nothing() -> None:
    order = timed_out(OrderSide.SELL)
    s = rearming(order)
    s.signal_orders.add("O-1")
    event = cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1"))
    s._on_limit_timeout(event)
    assert s.dropped_side == OrderSide.SELL
    order.is_open, order.is_closed, order.filled_qty = False, True, Quantity.from_int(1)
    s._on_limit_timeout(event)
    assert s.dropped_side is None


def test_a_limit_pending_cancel_is_watched_not_canceled_again() -> None:
    """A cancel sent again for an order pending cancel is refused at the pin and sends nothing; the
    strategy looks again instead."""
    order = timed_out(OrderSide.SELL)
    s = rearming(order)
    event = cast(TimeEvent, SimpleNamespace(name="limit-timeout-O-1"))
    s._on_limit_timeout(event)
    order.is_pending_cancel = True
    s._on_limit_timeout(event)
    assert s.canceled == ["O-1"] and len(s.rearmed) == 2


def test_a_worker_call_left_after_a_clean_stop_is_bounded(monkeypatch) -> None:
    """`Runner.close` waits up to 300 s for the executor's calls; a call still running after a clean
    stop held the account check that long."""
    import threading
    import time

    release = threading.Event()

    class Clean:
        async def run_async(self) -> None:
            import asyncio

            asyncio.ensure_future(asyncio.to_thread(release.wait, 30))
            await asyncio.sleep(0.05)

        def handle(self):
            return SimpleNamespace(stop=lambda: None)

    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "ADAPTER_CALL_SECS", 0.2)
    started = time.monotonic()
    try:
        with pytest.raises(session_window.StopTimedOut, match="still ran"):
            session_window._run_node(cast(Any, Clean()), datetime.now(UTC))
        assert time.monotonic() - started < 5
        assert len(session_window._ABANDONED) == 1
    finally:
        release.set()


# An independent review of the account check (2026-11-10): each shown here before its fix.


class OrderInitialized: ...


class OrderSubmitted: ...


class OrderAccepted: ...


class OrderFilled: ...


class OrderDenied: ...


class OrderCanceled:
    def __init__(self, reconciliation: bool = False) -> None:
        self.reconciliation = reconciliation


class OrderRejected:
    def __init__(self, reason: str = "HTTP 403: no", reconciliation: bool = False) -> None:
        self.reason = reason
        self.reconciliation = reconciliation


def node_order(client_order_id: str, *events, side=OrderSide.BUY, is_closed: bool = False):
    """An order as the node's cache holds it, as far as the window reads it: its ID, side, events and
    whether it is closed (the pinned `MarketOrder` and `LimitOrder` have each)."""
    return SimpleNamespace(
        client_order_id=SimpleNamespace(value=client_order_id),
        side=side,
        is_closed=is_closed,
        events=lambda: list(events),
    )


def test_a_node_order_the_node_did_not_see_end_is_unsettled() -> None:
    """An order the adapter sent and heard nothing back for (`_log_unknown_outcome`) stays submitted, and
    one Alpaca accepted may still work when the node stops; either may be at Alpaca after the stop. The
    node's own rejection of a submit never acknowledged (`INFLIGHT_TIMEOUT`, `NOT_FOUND_AT_VENUE`:
    `create_reconciliation_rejected` at the pin) is not Alpaca's answer, and nor is any end the node
    gave an order by its own reconciliation (`create_reconciliation_canceled`, and that rejection of an
    accepted order): those events carry `reconciliation`."""
    sent = (OrderInitialized(), OrderSubmitted())
    cache = SimpleNamespace(
        orders=lambda **kwargs: [
            node_order("lost", *sent, side=OrderSide.SELL),
            node_order("working", *sent, OrderAccepted()),
            node_order("part-filled", *sent, OrderAccepted(), OrderFilled()),
            node_order("timed-out", *sent, OrderRejected("INFLIGHT_TIMEOUT"), is_closed=True),
            node_order("unseen", *sent, OrderRejected("NOT_FOUND_AT_VENUE"), is_closed=True),
            node_order("refused", *sent, OrderRejected(), is_closed=True),
            node_order("found-gone", *sent, OrderAccepted(), OrderCanceled(True), is_closed=True),
            node_order("canceled", *sent, OrderAccepted(), OrderCanceled(), is_closed=True),
            node_order(
                "lost-after",
                *sent,
                OrderAccepted(),
                OrderRejected("NOT_FOUND_AT_VENUE", reconciliation=True),
                is_closed=True,
            ),
            node_order("denied", OrderInitialized(), OrderDenied(), is_closed=True),
            node_order("filled", *sent, OrderFilled(), is_closed=True),
            node_order("never-sent", OrderInitialized()),
        ]
    )
    assert session_window._unsettled(cache, session_window.STRATEGY_ID) == {
        "lost": ("sell", "sent with no answer"),
        "working": ("buy", "open at the node"),
        "part-filled": ("buy", "open at the node"),
        "timed-out": ("buy", "sent with no answer"),
        "unseen": ("buy", "sent with no answer"),
        "found-gone": ("buy", "ended by the node's own reconciliation"),
        "lost-after": ("buy", "ended by the node's own reconciliation"),
    }


def test_a_real_order_of_the_pinned_node_has_what_the_window_reads() -> None:
    """The stand-in above against the pinned library: a filled market order from the engine's cache is
    closed, has a side with a name, and is not unsettled."""
    eng = engine()
    (first, *_) = run(eng, strategy())
    assert first.is_closed is True and first.side.name in {"BUY", "SELL"}
    assert session_window._unsettled(eng.cache, session_window.STRATEGY_ID) == {}


def test_the_window_hands_the_nodes_orders_in_doubt_to_the_check(monkeypatch, tmp_path) -> None:
    lost = node_order("entry-late", OrderInitialized(), OrderSubmitted())

    class Ran:
        cache = SimpleNamespace(orders=lambda **kwargs: [lost])

        def dispose(self) -> None: ...

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0
    )
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    handed: list = []
    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: (Ran(), s, cap))
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    monkeypatch.setattr(
        session_window,
        "_flatten_after_stop",
        lambda trading, config, report, **kw: handed.append(kw["unsettled"]) or [],
    )
    report = session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert handed == [{"entry-late": "buy"}]
    assert report.unsettled == {"entry-late": ["buy: sent with no answer"]}


# An independent review of the in-doubt lookup as landed (2026-11-10): each shown here before its fix.


def test_the_orders_in_doubt_are_read_before_the_node_is_disposed(monkeypatch, tmp_path) -> None:
    """On a clean stop the node was disposed, which clears its cache, before the orders in doubt were read
    from it, so none was looked up."""

    lost = node_order("entry-late", OrderInitialized(), OrderSubmitted())
    held = [lost]

    class Disposing:
        cache = SimpleNamespace(orders=lambda **kwargs: list(held))

        def dispose(self) -> None:
            held.clear()  # as the live kernel's disposal clears the cache

    s = SimpleNamespace(
        dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0
    )
    cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
    handed: list = []
    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(session_window, "window_node", lambda config, **kw: (Disposing(), s, cap))
    monkeypatch.setattr(session_window, "_run_node", lambda node, ends: None)
    monkeypatch.setattr(
        session_window,
        "_flatten_after_stop",
        lambda trading, config, report, **kw: handed.append(kw["unsettled"]) or [],
    )
    session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert handed == [{"entry-late": "buy"}]


# Four further independent reviews of the check (2026-11-10): each shown here before its fix.


def test_a_window_whose_start_up_reads_ran_into_its_end_is_skipped(
    monkeypatch, fake_time, tmp_path
):
    """The time left was judged from Alpaca's clock before the calendar and flatness reads; reads that took
    longer than what was left built and ran a node after its planned end, and could record ok."""

    class SlowFlat(Account):
        qty = 0

        def get_orders(self, filter=None) -> list:
            fake_time.now += 150
            return []

    # 100 s of window left after the end margin and the wind-down, then 150 s of reads
    left = session_window.END_MARGIN_SECS + session_window.FLATTEN_SECS + 100
    closes = datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK)
    near_close = clock(closes - timedelta(seconds=left), is_open=True)
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    with pytest.raises(session_window.WindowSkipped, match="start-up"):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path),
            trading=cast(TradingClient, SlowFlat()),
            market=(near_close, CALENDAR),
        )


def test_the_reads_after_the_clock_are_inside_the_window_not_after_it(
    monkeypatch, tmp_path
) -> None:
    """The window's end is Alpaca's clock reading plus its length, an absolute time the node runs to:
    the reads after the clock eat into the window, and the lock's arithmetic counts only the first."""
    w = session_window
    handed: list = []
    monkeypatch.setattr(w, "_ABANDONED", [])
    monkeypatch.setattr(w, "window_node", lambda config, **kw: ran_node(Hung()))
    monkeypatch.setattr(w, "_run_node", lambda node, ends: handed.append(ends))
    monkeypatch.setattr(w, "_flatten_after_stop", lambda *a, **k: [])
    at = datetime(2026, 11, 10, 10, 0, tzinfo=NEW_YORK)
    w.run_window(
        WindowConfig(log_dir=tmp_path, minutes=60),
        trading=cast(TradingClient, Account()),
        market=(clock(at, is_open=True), CALENDAR),
    )
    assert handed == [at.astimezone(UTC) + timedelta(seconds=w.LONGEST_WINDOW_SECS)]


def test_a_refusal_whose_records_cannot_be_written_still_says_why(monkeypatch, tmp_path, capsys):
    """A heartbeat or record that could not be written (the log folder gone) replaced the run's own
    error, so nothing named the refusal."""
    heartbeat = session_window.write_heartbeat

    def unwritable(log_dir, outcome):
        if outcome.startswith("refused"):
            raise PermissionError("read-only file system")
        heartbeat(log_dir, outcome)

    def refused(config, **kwargs):
        raise session_window.WindowRefused("the paper account is not flat in SPY")

    monkeypatch.setattr(session_window, "write_heartbeat", unwritable)
    monkeypatch.setattr(session_window, "run_window", refused)
    assert session_window.main(["--log-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "not flat in SPY" in err and "could not be written" in err


def test_the_wait_for_a_worker_call_is_paid_once() -> None:
    """A clean stop with a worker still running waited `ADAPTER_CALL_SECS`, then the check waited for the
    same call again."""
    import threading

    release = threading.Event()
    calls = session_window.Calls()
    calls.submit(release.wait, 30)
    try:
        calls.until = None
        session_window.ADAPTER_CALL_SECS, saved = 0.2, session_window.ADAPTER_CALL_SECS
        try:
            import time

            started = time.monotonic()
            assert calls.settle()  # the first wait, about 0.2 s
            assert calls.settle()  # the second ends at once
            assert time.monotonic() - started < 0.35
        finally:
            session_window.ADAPTER_CALL_SECS = saved
    finally:
        release.set()


def test_a_node_that_ends_with_its_own_error_does_not_wait_300_s_on_a_worker(monkeypatch) -> None:
    import threading
    import time

    release = threading.Event()

    class Broken:
        async def run_async(self) -> None:
            import asyncio

            asyncio.ensure_future(asyncio.to_thread(release.wait, 30))
            await asyncio.sleep(0.05)
            raise RuntimeError("the node failed")

        def handle(self):
            return SimpleNamespace(stop=lambda: None)

    monkeypatch.setattr(session_window, "_ABANDONED", [])
    monkeypatch.setattr(session_window, "ADAPTER_CALL_SECS", 0.2)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="the node failed"):
            session_window._run_node(cast(Any, Broken()), datetime(2100, 1, 1, tzinfo=UTC))
        assert time.monotonic() - started < 5 and len(session_window._ABANDONED) == 1
    finally:
        release.set()


BLOCKED_WORKER = """
import asyncio, json, sys, threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from canon_trading import session_window

class Clean:
    cache = SimpleNamespace(orders=lambda **kwargs: [])
    async def run_async(self):
        asyncio.ensure_future(asyncio.to_thread(threading.Event().wait))  # blocks for ever
        await asyncio.sleep(0.05)
    def handle(self):
        return SimpleNamespace(stop=lambda: None)
    def dispose(self):
        pass

class Flat:
    def get_clock(self):
        from alpaca.trading.models import Clock
        return Clock(timestamp=datetime.now(UTC), is_open=True,
                     next_open=datetime(2100, 1, 1, tzinfo=UTC), next_close=datetime(2100, 1, 1, tzinfo=UTC))
    def get_calendar(self, filters=None):
        return []
    def get_orders(self, filter=None):
        return []
    def get_all_positions(self):
        return []
    def get_account_configurations(self):
        from alpaca.trading.models import AccountConfiguration
        return AccountConfiguration(no_shorting=True, suspend_trade=False, fractional_trading=True,
                                    max_margin_multiplier="4", trade_confirm_email="all",
                                    ptp_no_exception_entry=False)

session_window.ADAPTER_CALL_SECS = 0.3
session_window.paper_trading = lambda: Flat()
session_window.window_data = lambda: SimpleNamespace(
    get_stock_latest_trade=lambda r: {r.symbol_or_symbols: SimpleNamespace(price=750.0)}
)
s = SimpleNamespace(dropped=[], history_bars=0, history_dropped=0, quotes=1, trades=1, live_bars=0)
cap = SimpleNamespace(checks=1, peak=Decimal(0), tripped=None)
session_window.window_node = lambda config, **kw: (Clean(), s, cap)
session_window.window_seconds = lambda minutes, left: (130.0, "")
session_window.FLATTEN_SECS = 1
real = session_window._run_node
session_window._run_node = lambda node, ends: real(node, datetime.now(UTC))
code = session_window.main(["--session", "regular", "--log-dir", sys.argv[1]])
print("not reached", code)
"""


def test_a_blocked_worker_after_a_clean_stop_ends_the_process_recorded_failed(tmp_path) -> None:
    """A worker thread blocked for ever after a clean stop held the interpreter's exit, and the paper
    lock with it, until mise's deadline; the process now ends with the window recorded failed."""
    import subprocess
    import sys

    try:
        child = subprocess.run(
            [sys.executable, "-c", BLOCKED_WORKER, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the process still waits on a blocked worker after a clean stop")
    assert child.returncode == 1 and "not reached" not in child.stdout, child.stderr[-2000:]
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("failed: StopTimedOut: a worker call of the node still ran")


def test_the_auto_paths_clock_read_counts_in_the_time_left(
    monkeypatch, fake_time, tmp_path
) -> None:
    """With `--session auto` the clock is read in `_main`; the calendar read after it took time the
    window's own measure did not see."""

    class FlatAccount(Account):
        qty = 0

    left = session_window.END_MARGIN_SECS + session_window.FLATTEN_SECS + 100
    closes = datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK)
    market = (clock(closes - timedelta(seconds=left), is_open=True), CALENDAR)

    def slow_session_now(trading):
        read_at = (
            fake_time.now
        )  # the clock answers; then the calendar read with alpaca-py's retries
        fake_time.now += 150
        return (*market, read_at)

    monkeypatch.setattr(session_window, "paper_trading", lambda: FlatAccount())
    monkeypatch.setattr(session_window, "session_now", slow_session_now)
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    assert session_window.main(["--session", "auto", "--log-dir", str(tmp_path)]) == 0
    heard = json.loads((tmp_path / "last-firing.json").read_text())
    assert heard["outcome"].startswith("skipped: the start-up reads")


def test_a_refusal_is_recorded_though_its_heartbeat_cannot_be_written(
    monkeypatch, tmp_path
) -> None:
    heartbeat = session_window.write_heartbeat

    def unwritable(log_dir, outcome):
        if outcome.startswith("refused"):
            raise PermissionError("read-only file")
        heartbeat(log_dir, outcome)

    def refused(config, **kwargs):
        raise session_window.WindowRefused("the paper account is not flat in SPY")

    monkeypatch.setattr(session_window, "write_heartbeat", unwritable)
    monkeypatch.setattr(session_window, "run_window", refused)
    assert session_window.main(["--log-dir", str(tmp_path)]) == 1
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("refused: the paper account is not flat")


# The account check after the stop, against one stand-in for Alpaca's paper Trading API (ADR 0060).


def api_error(status: int, code: int, message: str) -> APIError:
    """An `APIError` as alpaca-py raises it: the body's text, and the status on the HTTP error's response."""
    return APIError(
        json.dumps({"code": code, "message": message}),
        SimpleNamespace(response=SimpleNamespace(status_code=status)),
    )


def no_answer() -> requests.ReadTimeout:
    """The adapter's read timeout: the request was sent and no answer came."""
    return requests.ReadTimeout("read timed out")


TERMINAL = {"filled", "canceled", "expired", "rejected"}
SESSION_STARTS = datetime(2026, 11, 10, 15, 0, tzinfo=NEW_YORK)
"""What Alpaca's clock says at `FakeTime` 0: an hour of the regular session left."""


@dataclass
class Held:
    """One order as the stand-in holds it; the times are `FakeTime`'s."""

    client_order_id: str
    side: str
    qty: Decimal
    type: str
    placed_at: float
    seen_at: float
    """From when a lookup by either ID and the all-orders listing show it."""
    listed_at: float
    """From when the open-orders listing shows it."""
    fills_at: float | None
    cancel_after: float
    status: str = "accepted"
    cancel_lands_at: float | None = None
    id: UUID = field(default_factory=uuid4)
    submitted: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def working(self) -> bool:
        return self.status not in TERMINAL


@dataclass
class Fault:
    """What one kind of request gets in place of its answer, `times` times (None: every time) while
    `when` holds: `error` raised, or, with None, an answer as if all were well. The request has done
    nothing at Alpaca unless `placed`: a close `placed` reaches it all the same, `arrives_after` seconds
    later, as an order with `order`'s settings."""

    kind: str
    error: Exception | None
    times: int | None = 1
    when: Any = None
    placed: bool = False
    arrives_after: float = 0.0
    order: dict = field(default_factory=dict)


class Venue:
    """A stand-in for Alpaca's paper Trading API as alpaca-py 0.45.0's `TradingClient` gives it to the
    account check: its `Order`, `Position` and `Clock` models, and `APIError` with the status Alpaca
    answers. One account, one symbol. Every request is logged with the `FakeTime` it began at and takes
    `latency` seconds of it (a number, or one per kind of request); what a request does happens as it
    arrives, its answer comes after. As Alpaca documents or its own error guide describes: a market order
    placed in the regular session fills, outside it queues; a sell for shares an open sell already holds
    is refused (403, "insufficient qty available for order"); a market order against an open order of
    the other side is refused (403, wash trade); closing a position that is not there is a 404; an
    order that is `filled` or `canceled` cannot be canceled (422). What Alpaca does not document, a test
    sets: how long after it is placed an order fills (`fill_after`), shows in a lookup (`seen_after`) or
    in the open listing (`listed_after`), and how long a cancel takes to land (`cancel_after`)."""

    def __init__(
        self, fake: FakeTime, *, qty: int = 0, is_open: bool = True, latency: Any = 0.0
    ) -> None:
        self.fake = fake
        self.qty = Decimal(qty)
        self.is_open = is_open
        self.closes_at: float | None = None
        self.latency = latency
        self.fill_after: float | None = 0.0
        self.seen_after = 0.0
        self.listed_after: float | None = None
        self.cancel_after = 0.0
        self.held: list[Held] = []
        self.arriving: list[tuple[float, dict[str, Any]]] = []
        self.calls: list[tuple[float, str]] = []
        self.faults: list[Fault] = []
        self.before: dict[str, Any] = {}
        self._closes: set[str] = set()

    # What a test sets up.

    def order(
        self,
        client_order_id: str,
        side: str,
        *,
        qty: int = 1,
        type: str = "market",
        at: float | None = None,
        fills: float | None = None,
        seen_after: float | None = None,
        listed_after: float | None = None,
        cancel_after: float | None = None,
        status: str = "accepted",
    ) -> None:
        """An order reaching Alpaca now, or at `FakeTime` `at`: one of the node's, or another's. It fills
        `fills` seconds after it arrives (None: it stays working)."""
        entry: dict[str, Any] = {
            "client_order_id": client_order_id,
            "side": side,
            "qty": qty,
            "type": type,
            "fills": fills,
            "seen_after": seen_after,
            "listed_after": listed_after,
            "cancel_after": cancel_after,
            "status": status,
        }
        if at is None:
            self._place(self.fake.now, **entry)
        else:
            self.arriving.append((at, entry))

    def fail(self, kind: str, error: Exception | None, **how: Any) -> None:
        self.faults.append(Fault(kind, error, **how))

    def did(self, *kinds: str) -> list[str]:
        """The requests of those kinds, in the order they were sent."""
        return [name for _, name in self.calls if name.split()[0] in kinds]

    def working(self) -> list[Held]:
        self.settle()
        return [h for h in self.held if h.working]

    def closes(self) -> list[Held]:
        """The closing orders that reached Alpaca through the API, whatever became of them."""
        return [h for h in self.held if h.client_order_id in self._closes]

    def later(self, seconds: float) -> None:
        self.fake.now += seconds
        self.settle()

    # How the stand-in keeps its account.

    def _session_open(self, at: float) -> bool:
        return self.is_open and (self.closes_at is None or at < self.closes_at)

    def _place(
        self,
        at: float,
        *,
        client_order_id: str,
        side: str,
        qty: int | Decimal,
        type: str = "market",
        fills: float | None = None,
        seen_after: float | None = None,
        listed_after: float | None = None,
        cancel_after: float | None = None,
        status: str = "accepted",
    ) -> Held:
        seen = at + (self.seen_after if seen_after is None else seen_after)
        if listed_after is None:
            listed_after = self.listed_after
        held = Held(
            client_order_id=client_order_id,
            side=side,
            qty=Decimal(qty),
            type=type,
            placed_at=at,
            seen_at=seen,
            listed_at=seen if listed_after is None else at + listed_after,
            fills_at=None if fills is None else at + fills,
            cancel_after=self.cancel_after if cancel_after is None else cancel_after,
            status=status,
        )
        self.held.append(held)
        return held

    def settle(self) -> None:
        """Bring the account to `FakeTime` now: orders that arrive, fill, or whose cancel lands."""
        now = self.fake.now
        for at, entry in [a for a in self.arriving if a[0] <= now]:
            self.arriving.remove((at, entry))
            self._place(at, **entry)
        for held in self.held:
            if not held.working:
                continue
            fills = held.fills_at
            if fills is not None and held.type == "market" and not self._session_open(fills):
                fills = None  # it queues for the next session
            lands = held.cancel_lands_at
            if fills is not None and fills <= now and (lands is None or fills <= lands):
                held.status = "filled"
                self.qty += held.qty if held.side == "buy" else -held.qty
            elif lands is not None and lands <= now:
                held.status = "canceled"

    def _latency(self, kind: str) -> float:
        if isinstance(self.latency, dict):
            return self.latency.get(kind, self.latency.get("", 0.0))
        return self.latency(self) if callable(self.latency) else self.latency

    def _request(self, name: str) -> Fault | None:
        """Log a request as it arrives and take the fault set for its kind, if one applies."""
        kind = name.split()[0]
        self._arrive(kind)
        self.calls.append((self.fake.now, name))
        for fault in self.faults:
            if fault.kind == kind and (fault.when is None or fault.when(self)):
                if fault.times is not None:
                    fault.times -= 1
                    if not fault.times:
                        self.faults.remove(fault)
                return fault
        return None

    def _arrive(self, kind: str) -> None:
        """What a test set to happen just before the next request of this kind arrives, then the account
        as it stands."""
        if hook := self.before.pop(kind, None):
            hook()
        self.settle()

    def _answer(self, kind: str, fault: Fault | None = None) -> None:
        self.fake.now += self._latency(kind)
        if fault is not None and fault.error is not None:
            raise fault.error

    def _snapshot(self, held: Held) -> Order:
        return Order(
            id=held.id,
            client_order_id=held.client_order_id,
            created_at=held.submitted,
            updated_at=held.submitted,
            submitted_at=held.submitted,
            symbol="SPY",
            qty=str(held.qty),
            filled_qty=str(held.qty) if held.status == "filled" else "0",
            type=held.type,
            side=held.side,
            time_in_force="day",
            status=held.status,
            extended_hours=False,
        )

    def _take(self, side: str, qty: Decimal, client_order_id: str, fault: Fault | None) -> Held:
        """A closing order as it arrives through the API: refused as Alpaca refuses, else placed."""
        working = [h for h in self.held if h.working]
        if side == "sell" and self.qty > 0:
            reserved = sum(h.qty for h in working if h.side == "sell")
            if qty > self.qty - reserved:
                raise api_error(
                    403,
                    40310000,
                    f"insufficient qty available for order (requested: {qty}, "
                    f"available: {self.qty - reserved})",
                )
        if any(h.side != side for h in working):
            raise api_error(403, 40310000, "potential wash trade detected")
        entry: dict[str, Any] = {"fills": self.fill_after, **(fault.order if fault else {})}
        self._closes.add(client_order_id)
        return self._place(
            self.fake.now, client_order_id=client_order_id, side=side, qty=qty, **entry
        )

    def _close(self, side: str, qty: Decimal, client_order_id: str) -> Order:
        fault = self._request(f"close {side} {qty}")
        if fault is not None and not fault.placed:
            self._answer("close", fault)
        if fault is not None and fault.arrives_after:
            self._closes.add(client_order_id)
            entry: dict[str, Any] = {
                "client_order_id": client_order_id,
                "side": side,
                "qty": qty,
                "fills": self.fill_after,
                **fault.order,
            }
            self.arriving.append((self.fake.now + fault.arrives_after, entry))
            self._answer("close", fault)
        try:
            held = self._take(side, qty, client_order_id, fault)
        except APIError:
            self.fake.now += self._latency("close")
            raise
        self._answer("close", fault)
        return self._snapshot(held)

    # alpaca-py's `TradingClient`, as far as the window uses it.

    def get_clock(self) -> Clock:
        fault = self._request("clock")
        now = SESSION_STARTS + timedelta(seconds=self.fake.now)
        is_open = self._session_open(self.fake.now)
        closes = SESSION_STARTS + timedelta(
            seconds=3600 if self.closes_at is None else self.closes_at
        )
        found = Clock(
            timestamp=now,
            is_open=is_open,
            next_open=SESSION_STARTS + timedelta(days=1),
            next_close=closes if is_open else closes + timedelta(days=1),
        )
        self._answer("clock", fault)
        return found

    def get_orders(self, filter=None) -> list:
        status = getattr(getattr(filter, "status", None), "value", None) or "open"
        fault = self._request("orders" if status == "open" else f"orders {status}")
        now = self.fake.now
        if status == "open":
            found = [h for h in self.held if h.working and h.listed_at <= now]
        else:
            after = getattr(filter, "after", None)
            found = [
                h
                for h in self.held
                if h.seen_at <= now
                and (after is None or h.submitted > after)
                and (status == "all" or not h.working)
            ]
        answer = [self._snapshot(h) for h in found]
        self._answer("orders", fault)
        return answer

    def get_all_positions(self) -> list[Position]:
        fault = self._request("position")
        qty = self.qty
        self._answer("position", fault)
        if not qty:
            return []
        return [
            Position(
                asset_id=uuid4(),
                symbol="SPY",
                exchange="ARCA",
                asset_class="us_equity",
                avg_entry_price="750",
                qty=str(qty),
                side="long" if qty > 0 else "short",
                cost_basis=str(750 * qty),
            )
        ]

    def get_order_by_client_id(self, client_id: str) -> Order:
        fault = self._request(f"lookup {client_id}")
        found = [
            h for h in self.held if h.client_order_id == client_id and h.seen_at <= self.fake.now
        ]
        answer = self._snapshot(found[0]) if found else None
        self._answer("lookup", fault)
        if answer is None:
            raise api_error(404, 40410000, "order not found")
        return answer

    def cancel_order_by_id(self, order_id) -> None:
        found = [h for h in self.held if str(h.id) == str(order_id)]
        fault = self._request(f"cancel {found[0].client_order_id if found else order_id}")
        if fault is not None and not fault.placed:
            self._answer("cancel", fault)
            return  # answered as taken, and it did not land
        if not found:
            self.fake.now += self._latency("cancel")
            raise api_error(404, 40410000, "order not found")
        held = found[0]
        if not held.working:
            self.fake.now += self._latency("cancel")
            raise api_error(422, 42210000, f'order is already in "{held.status}" state')
        if held.cancel_lands_at is None:  # a cancel sent again does not restart one on its way
            held.cancel_lands_at = self.fake.now + held.cancel_after
        self.settle()
        self._answer("cancel", fault)

    def close_position(self, symbol_or_asset_id, close_options=None) -> Order:
        self._arrive("close")
        if not self.qty:
            self._request("close position")
            self.fake.now += self._latency("close")
            raise api_error(404, 40410000, "position does not exist")
        side = "sell" if self.qty > 0 else "buy"
        return self._close(side, abs(self.qty), f"alpaca-made-{uuid4()}")

    def submit_order(self, order_data) -> Order:
        side = getattr(order_data.side, "value", order_data.side)
        qty = Decimal(str(order_data.qty))
        return self._close(side, qty.normalize(), order_data.client_order_id)


def check(
    venue: Venue, unsettled: dict[str, str] | None = None, *, session: str = "regular"
) -> tuple[list, session_window.WindowReport]:
    """Run the account check after the stop against `venue`; `unsettled` is the node's orders it must
    see ended, each with its side."""
    report = session_window.WindowReport(session, "SPY", started="t")
    left = session_window._flatten_after_stop(
        cast(TradingClient, venue), WindowConfig(session=session), report, unsettled=unsettled or {}
    )
    return left, report


def ends_flat(venue: Venue) -> bool:
    """Whether the account is flat ten minutes on, with nothing working: what a record saying ok claims."""
    venue.later(600)
    return venue.qty == 0 and not venue.working()


LAST_POLL = (
    session_window.AFTER_STOP_SECS
    - session_window.AFTER_STOP_SECS % session_window.POLL_SECS
    - session_window.POLL_SECS
)
"""When the check's last poll before its deadline begins, if every request before it answered at once."""


def refused_until_the_LAST_POLL(venue: Venue) -> None:
    """Every close is refused (403, nothing placed) until the check's last poll."""
    venue.fail("close", insufficient(), times=None, when=lambda v: v.fake.now < LAST_POLL)


def test_after_the_stop_the_cancels_land_before_the_close(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit", cancel_after=1.0)  # a cancel takes a poll to land
    left, report = check(venue)
    assert venue.did("cancel", "close") == ["cancel exit-1", "close sell 1"]
    assert left == [] and report.closed_after_stop and report.errors == []
    (close,) = report.closes.values()
    assert close == ["sent: sell 1 SPY at market", "accepted", "filled"]


def test_the_close_is_a_market_day_order_under_an_id_of_the_checks_own(fake_time) -> None:
    """What the check sends to close: alpaca-py's `MarketOrderRequest` for the held quantity, DAY, with a
    client order ID that names the window and is new for each close."""
    venue = Venue(fake_time, qty=1)
    sent: list = []
    submit = venue.submit_order
    venue.submit_order = lambda order_data: sent.append(order_data) or submit(order_data)
    venue.fail("close", insufficient())
    left, report = check(venue)
    assert left == [] and len(sent) == 2
    for request in sent:
        assert isinstance(request, MarketOrderRequest)
        assert request.to_request_fields() == {
            "symbol": "SPY",
            "qty": 1.0,
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": request.client_order_id,
        }
        assert request.client_order_id.startswith("canon-close-t-")
    assert len({r.client_order_id for r in sent}) == 2
    assert [log[-1] for log in report.closes.values()] == ["filled"]
    assert report.closes_not_placed == 1


def test_a_short_is_closed_by_a_buy(fake_time) -> None:
    venue = Venue(fake_time, qty=-1)
    left, _ = check(venue)
    assert venue.did("close") == ["close buy 1"] and left == [] and venue.qty == 0


def test_outside_the_session_the_orders_are_canceled_and_the_position_reported(
    fake_time, extended
) -> None:
    venue = Venue(fake_time, qty=1, is_open=False)  # outside the regular session
    venue.order("exit-1", "sell", type="limit")
    left, _ = check(venue, session="extended")
    assert venue.did("cancel", "close") == ["cancel exit-1"]
    assert left == ["position of 1 SPY"]


def test_a_close_waits_for_a_cancel_that_is_slow_to_land(fake_time) -> None:
    """Finding 2: the market close was tried once; Alpaca's 403 while the cancel cleared ended it."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit", cancel_after=40.0)
    left, report = check(venue)
    assert left == [] and report.closed_after_stop
    assert venue.did("close") == ["close sell 1"] and set(venue.did("cancel")) == {"cancel exit-1"}


def test_no_market_close_is_sent_once_alpacas_clock_says_the_session_closed(fake_time) -> None:
    """Finding 2: a regular window's close after 16:00 would only queue for the next session."""
    venue = Venue(fake_time, qty=1, is_open=False)
    left, report = check(venue)
    assert venue.did("close") == [] and not report.closed_after_stop
    assert left == ["position of 1 SPY"]
    assert any("would only queue" in e for e in report.errors)


def test_a_close_that_would_only_queue_is_canceled(fake_time) -> None:
    """Finding 2: a close sent as the session closed queued, and nothing canceled it."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 1.0
    venue.closes_at = 0.5  # the session closes between the clock read and the fill
    left, report = check(venue)
    (close,) = venue.closes()
    assert venue.did("cancel") == [f"cancel {close.client_order_id}"]
    assert left == ["position of 1 SPY"] and close.status == "canceled"
    assert report.closes[close.client_order_id][-1] == "canceled"


def test_the_account_check_ends_within_its_bound_however_slow_alpaca_is(fake_time) -> None:
    """Calls before the check's deadline was set, and after it passed, were not counted in it: with each
    call near the request timeout the check ran far past `AFTER_STOP_SECS`."""
    w = session_window
    venue = Venue(fake_time, qty=1, latency=SLOW)
    venue.order("entry-1", "buy", type="limit")
    venue.fail("cancel", api_error(422, 42210000, "order is not cancelable"), times=None)
    left, _ = check(venue)
    assert fake_time.now <= w.AFTER_END_SECS - w.STOP_TIMEOUT_SECS - w.ADAPTER_CALL_SECS
    assert venue.did("cancel") == ["cancel entry-1"] * 2 and venue.did("close") == []
    assert left == ["open order entry-1", "position of 1 SPY"]


@pytest.mark.parametrize(
    "error",
    [api_error(422, 42210000, "order is not cancelable"), None],
    ids=["refused", "taken-not-landed"],
)
def test_a_working_order_whose_cancel_did_not_land_is_canceled_again(fake_time, error) -> None:
    """The open orders were canceled once; one whose cancel failed or did not land stayed working while
    the session was open, could fill once the process had gone, and kept the position from its close."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit")
    venue.fail("cancel", error, times=2)
    left, report = check(venue)
    assert venue.did("cancel", "close") == ["cancel exit-1"] * 3 + ["close sell 1"]
    assert left == [] and report.closed_after_stop


def test_a_timeout_on_one_cancel_does_not_skip_the_next_or_the_close(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit")
    venue.order("exit-2", "sell", type="limit")
    venue.fail("cancel", requests.ConnectTimeout("connect timed out"))
    left, report = check(venue)
    assert venue.did("cancel", "close") == [
        "cancel exit-1",
        "cancel exit-2",
        "cancel exit-1",
        "close sell 1",
    ]
    assert left == [] and len(report.errors) == 1


def test_more_working_orders_than_one_step_cancels_are_canceled_over_the_polls(fake_time) -> None:
    """A step sends `CANCELS` cancels, so the bound can count them; with more orders working, those not
    yet tried come first in the next poll."""
    venue = Venue(fake_time, qty=0)
    for n in range(5):
        venue.order(f"entry-{n}", "buy", type="limit")
    left, _ = check(venue)
    assert left == [] and not venue.working()
    cancels = [(at, name) for at, name in venue.calls if name.startswith("cancel")]
    assert [sum(at == poll for at, _ in cancels) for poll in (0.0, 2.5, 5.0)] == [2, 2, 1]
    assert sorted(name for _, name in cancels) == [f"cancel entry-{n}" for n in range(5)]


def test_a_close_taken_in_the_LAST_POLL_is_read_back_not_reported_held(fake_time) -> None:
    """The result was the reading from before the last poll's close, so a window left flat was recorded
    not flat."""
    venue = Venue(fake_time, qty=1)
    refused_until_the_LAST_POLL(venue)
    left, _ = check(venue)
    assert [at for at, name in venue.calls if name == "close sell 1"][-1] == LAST_POLL
    assert left == []


def test_the_checks_own_close_is_known_even_when_its_answer_was_lost(fake_time) -> None:
    """A close whose call failed after reaching Alpaca was taken for another's order and canceled. It is
    known by its client order ID, whatever became of the answer."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 4.0  # working for a poll, then filled
    venue.fail("close", no_answer(), placed=True)
    left, report = check(venue)
    assert venue.did("close", "cancel") == ["close sell 1"] and left == []
    (close,) = report.closes.values()
    assert close == ["sent: sell 1 SPY at market", "no answer: ReadTimeout", "accepted", "filled"]
    assert report.closed_after_stop


def test_the_checks_own_close_is_left_to_fill_while_the_session_is_open(fake_time) -> None:
    """The documented exception: a close the check sent is not canceled while the session is open."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 9.0
    left, _ = check(venue)
    assert venue.did("close", "cancel") == ["close sell 1"] and left == []


@pytest.mark.parametrize(
    ("which", "error"),
    [
        ("clock", api_error(500, 50000000, "internal server error")),
        ("orders", requests.ReadTimeout("read timed out")),
        ("position", requests.ConnectionError("connection reset")),
    ],
    ids=["clock-500", "orders-timeout", "position-reset"],
)
def test_one_transient_error_does_not_end_the_account_check(fake_time, which, error) -> None:
    """Only the close's `APIError` was caught: an error from a read or the clock ended the check at
    once, with its budget unused and a closable position left."""
    venue = Venue(fake_time, qty=1)
    venue.fail(which, error)
    left, report = check(venue)
    assert left == [] and venue.did("close") == ["close sell 1"]
    assert len(report.errors) == 1


def test_a_close_refused_with_no_order_open_is_tried_again(fake_time) -> None:
    """The retry test waited for the cancel to clear before its only close, so a check that dropped the
    retry after a real 403 passed it; here the 403 comes, twice, after the order has gone. Alpaca's
    refusal says nothing was placed, so each next close is a new request under a new ID."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit")
    venue.fail("close", insufficient(), times=2)
    left, report = check(venue)
    assert venue.did("cancel", "close") == ["cancel exit-1"] + ["close sell 1"] * 3
    assert left == [] and report.closed_after_stop
    assert [log[-1] for log in report.closes.values()] == ["filled"]
    assert report.closes_not_placed == 2


def test_a_close_alpaca_shows_ended_unfilled_is_followed_by_another(fake_time) -> None:
    """A close Alpaca took and then shows `rejected` or `canceled` is settled on Alpaca's word: it sold
    nothing and never will, so the position still held is closed by a new order."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", None, placed=True, order={"fills": None, "status": "rejected"})
    left, report = check(venue)
    assert left == [] and len(venue.closes()) == 2
    assert [log[-1] for log in report.closes.values()] == ["rejected", "filled"]


def test_a_flat_reading_with_a_node_order_in_doubt_is_not_flat(fake_time) -> None:
    """The check read no open order and no position and recorded ok, while a submit whose answer was
    lost could still be taken by Alpaca after it: a live buy outside the check."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-late", "buy", type="limit", at=4.0)  # it reaches Alpaca two polls on
    left, report = check(venue, {"entry-late": "buy"})
    assert venue.did("cancel") == ["cancel entry-late"] and left == []
    assert report.unsettled == {"entry-late": ["buy", "accepted", "canceled"]}


def test_a_node_order_never_found_is_left_named(fake_time) -> None:
    venue = Venue(fake_time, qty=0)
    left, _ = check(venue, {"entry-late": "buy"})
    assert left == ["order entry-late: sent, not known to have ended, not found at Alpaca"]


def test_a_node_order_alpaca_shows_filled_is_settled(fake_time) -> None:
    """The node's exit was sent with no answer and filled: Alpaca shows it filled under its client order
    ID, the account is flat, and nothing is left."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", fills=0.0)
    left, report = check(venue, {"exit-1": "sell"})
    assert left == [] and venue.did("close", "cancel") == []
    assert report.unsettled == {"exit-1": ["sell", "filled"]}


def test_no_close_is_sent_while_a_node_order_on_its_side_is_not_found(fake_time) -> None:
    """The node's exit, sent with no answer, is not found at Alpaca, which may still place it: a close
    beside it could sell the share twice. The position is left and both are named."""
    venue = Venue(fake_time, qty=1)
    left, report = check(venue, {"exit-9": "sell"})
    assert venue.did("close") == []
    assert left == [
        "position of 1 SPY",
        "order exit-9: sent, not known to have ended, not found at Alpaca",
    ]
    assert any("not closing the position while exit-9" in e for e in report.errors)


def test_a_node_order_on_the_other_side_does_not_hold_the_close_back(fake_time) -> None:
    """A buy sent with no answer cannot close a long position a second time: the close goes out, and the
    buy, never found, is still named."""
    venue = Venue(fake_time, qty=1)
    left, _ = check(venue, {"entry-9": "buy"})
    assert venue.did("close") == ["close sell 1"] and venue.qty == 0
    assert left == ["order entry-9: sent, not known to have ended, not found at Alpaca"]


def test_a_node_order_whose_side_is_not_known_holds_the_close_back(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    left, _ = check(venue, {"order-9": ""})
    assert venue.did("close") == [] and left[0] == "position of 1 SPY"


def test_a_node_order_found_working_is_canceled_before_the_close(fake_time) -> None:
    """The node's own sell, in doubt, reaches Alpaca and works: it is canceled as any other's order, and
    the close goes out only once Alpaca shows it ended. Which order is the check's own is its client
    order ID, never an order's side, size and type."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-late", "sell", type="limit")
    left, _ = check(venue, {"exit-late": "sell"})
    assert venue.did("cancel", "close") == ["cancel exit-late", "close sell 1"]
    assert left == []


def test_more_orders_than_one_reading_looks_up_are_each_looked_up_in_turn(fake_time) -> None:
    """A reading looks `LOOKUPS` orders up, so the bound can count them; with more unsettled, each has
    its turn over the polls."""
    venue = Venue(fake_time, qty=0)
    doubtful = {f"entry-{n}": "buy" for n in range(5)}
    for client_order_id in doubtful:
        venue.order(client_order_id, "buy", fills=0.0)  # each was placed and filled
    venue.settle()
    venue.qty = Decimal(0)  # and the node's exits sold what they bought
    left, report = check(venue, doubtful)
    assert left == []
    lookups = [at for at, name in venue.calls if name.startswith("lookup")]
    assert [lookups.count(at) for at in (0.0, 2.5, 5.0)] == [2, 2, 1]
    assert all(log == ["buy", "filled"] for log in report.unsettled.values())


def test_a_late_order_seen_after_a_lost_close_is_canceled_and_the_close_left(fake_time) -> None:
    """Every order first seen after a close attempt was taken for the check's own close, so a late buy
    of the node's was left working."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 9.0
    venue.fail("close", no_answer(), placed=True)
    venue.order("entry-late", "buy", type="limit", at=1.0)
    left, _ = check(venue)
    assert venue.did("close", "cancel") == ["close sell 1", "cancel entry-late"] and left == []


def test_a_close_with_no_answer_that_was_never_placed_is_not_sent_again(fake_time) -> None:
    """What following Alpaca costs: a close whose answer was lost and which Alpaca never placed cannot be
    told from one it has not shown yet, so no other is sent; another's order seen after it is still
    canceled, and the window is recorded not flat, naming the position and the close."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", no_answer())
    venue.order("entry-late", "buy", type="limit", at=1.0)
    left, report = check(venue)
    assert venue.did("close", "cancel") == ["close sell 1", "cancel entry-late"]
    (client_order_id,) = report.closes
    assert left == [
        "position of 1 SPY",
        f"close {client_order_id}: sent with no answer, not found at Alpaca",
    ]


def test_an_order_found_by_its_lookup_but_not_listed_is_canceled_not_taken_as_flat(
    fake_time,
) -> None:
    """Flat by the listing and the position, while the lookup of the node's order finds it accepted (the
    two endpoints not yet agreeing); its lookup fails first with a 500."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-late", "buy", type="limit", listed_after=10_000.0)
    venue.fail("lookup", api_error(500, 50000000, "internal server error"))
    left, report = check(venue, {"entry-late": "buy"})
    assert venue.did("cancel") == ["cancel entry-late"] and left == []
    assert any("looking up entry-late" in e for e in report.errors)


def test_a_refused_close_is_not_a_lost_one(fake_time) -> None:
    """A close Alpaca refused placed nothing: it is settled, and does not hold the next close back."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", insufficient())
    venue.order("exit-late", "sell", type="limit", at=1.0)  # another's sell, seen after the refusal
    left, _ = check(venue)
    assert venue.did("close", "cancel") == ["close sell 1", "cancel exit-late", "close sell 1"]
    assert left == []


@pytest.mark.parametrize("placed", [True, False], ids=["found-filled", "never-found"])
def test_a_close_whose_answer_was_lost_keeps_the_account_unconfirmed(fake_time, placed) -> None:
    """A lost close was left out of the flat test: an empty listing and no position read as flat while
    the close could still be taken. Here another exit flattens the account; it is flat only once Alpaca
    shows the close ended."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", no_answer(), placed=placed, order={"fills": None})
    venue.order("exit-9", "sell", at=1.0, fills=0.0)  # the node's answered exit, arriving late
    left, report = check(venue)
    (client_order_id,) = report.closes
    if placed:  # found working with nothing left to close: canceled, then flat
        assert left == [] and venue.did("cancel") == [f"cancel {client_order_id}"]
    else:
        assert left == [f"close {client_order_id}: sent with no answer, not found at Alpaca"]


def test_the_last_reading_looks_for_a_close_whose_answer_was_lost(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    refused_until_the_LAST_POLL(venue)
    venue.fail("close", no_answer(), placed=True, when=lambda v: v.fake.now >= LAST_POLL)
    left, _ = check(venue)
    assert len(venue.closes()) == 1 and left == []


def test_a_late_node_sell_after_a_filled_lost_close_is_canceled(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    venue.fail("close", no_answer(), placed=True)
    venue.order("exit-late", "sell", type="limit", at=1.0)
    left, _ = check(venue)
    assert venue.did("cancel") == ["cancel exit-late"] and left == [] and venue.qty == 0


def test_three_slow_reads_do_not_starve_the_close(fake_time) -> None:
    """Reads of orders, position and clock at 21 s each used the 60 s up before the first close."""
    venue = Venue(fake_time, qty=1, latency={"orders": 21.0, "position": 21.0, "clock": 21.0})
    left, report = check(venue)
    assert venue.did("close") == ["close sell 1"] and report.closed_after_stop
    assert left == []


def test_an_order_first_seen_in_the_final_reading_is_canceled(fake_time) -> None:
    """The reading after the deadline was never acted on: a close that queued as the session closed,
    first seen there, was left working after the process exited."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 5.0
    venue.closes_at = LAST_POLL + 1.0  # the session closes a second after the last poll's close
    refused_until_the_LAST_POLL(venue)
    left, _ = check(venue)
    (close,) = venue.closes()
    assert venue.did("cancel") == [f"cancel {close.client_order_id}"] and not venue.working()
    assert left == [f"open order {close.client_order_id}", "position of 1 SPY"]


def test_an_order_in_doubt_done_for_the_day_is_canceled_as_working(fake_time) -> None:
    """`done_for_day` can still be updated and canceled (orders-at-alpaca, "Order Lifecycle"): the check
    cancels it, and it is terminal once canceled."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-1", "buy", type="limit", status="done_for_day", listed_after=10_000.0)
    left, _ = check(venue, {"entry-1": "buy"})
    assert venue.did("cancel") == ["cancel entry-1"] and left == []


def test_a_replaced_order_is_not_taken_as_ended(fake_time) -> None:
    """Alpaca says of `filled`, `canceled`, `expired` and `rejected` that "no further updates will occur";
    of `replaced` only that the order "was replaced by another order". A node order shown replaced goes on
    as another the window does not know: it stays unsettled and is named."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-1", "buy", type="limit", status="replaced", listed_after=10_000.0)
    venue.fail("cancel", api_error(422, 42210000, "order is not cancelable"), times=None)
    left, report = check(venue, {"entry-1": "buy"})
    assert left == ["open order entry-1"]
    assert report.unsettled == {"entry-1": ["buy", "replaced"]}


def test_the_checks_own_close_seen_last_is_left_to_fill_in_the_session(fake_time) -> None:
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 30.0  # still working in the reading after the deadline
    refused_until_the_LAST_POLL(venue)
    left, _ = check(venue)
    assert len(venue.closes()) == 1 and venue.did("cancel") == []
    assert left and ends_flat(venue)


def test_the_stand_in_refuses_as_alpaca_does(fake_time) -> None:
    """The stand-in's own rules, from Alpaca's documentation and error guide: a sell for shares an open
    sell holds and a market order against an open order of the other side are refused with a 403, a
    close of no position and a lookup of no order with a 404, a cancel of an ended order with a 422."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit")
    sell = MarketOrderRequest(symbol="SPY", qty=1, side="sell", time_in_force="day")
    with pytest.raises(APIError) as refused:
        venue.submit_order(sell)
    assert refused.value.status_code == 403 and "insufficient qty" in str(refused.value)
    (held,) = venue.working()
    venue.cancel_order_by_id(held.id)
    with pytest.raises(APIError) as ended:
        venue.cancel_order_by_id(held.id)
    assert ended.value.status_code == 422
    venue.order("entry-1", "buy", type="limit")
    with pytest.raises(APIError) as wash:
        venue.submit_order(sell)
    assert wash.value.status_code == 403 and "wash trade" in str(wash.value)
    with pytest.raises(APIError) as unknown:
        venue.get_order_by_client_id("no-such-order")
    assert unknown.value.status_code == 404
    flat = Venue(fake_time, qty=0)
    with pytest.raises(APIError) as gone:
        flat.close_position("SPY")
    assert gone.value.status_code == 404
    assert session_window.positions(cast(TradingClient, flat)) == {}
    assert session_window.positions(cast(TradingClient, venue)) == {"SPY": 1}


# Three independent reviews of the check as landed (2026-11-10): each shown here before its fix.


def test_a_close_with_no_answer_that_arrives_after_the_check_is_never_called_flat(
    fake_time,
) -> None:
    """The first close's answer was lost; two searches did not find it, a second close was answered and
    filled, the account read flat and the record said ok; the first, taken by Alpaca after the process
    had gone, then sold a share the account no longer held."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", no_answer(), placed=True, arrives_after=200.0)
    left, report = check(venue)
    assert len(venue.did("close")) == 1  # Alpaca: "You should not attempt to resend the order"
    assert len(left) == 2 and left[0] == "position of 1 SPY"
    assert "sent with no answer, not found at Alpaca" in left[1]
    assert not report.closed_after_stop


def test_a_close_with_no_answer_is_not_sent_again_while_it_may_still_be_placed(fake_time) -> None:
    """A second close went out after two searches had not found the first, which Alpaca had only not
    shown yet: both sold, the account went short, the check bought the share back and the record said
    ok with no trace of the extra sell and buy."""
    venue = Venue(fake_time, qty=1)
    venue.fail("close", no_answer(), placed=True, arrives_after=10.0)
    left, report = check(venue)
    assert len(venue.did("close")) == 1 and len(venue.closes()) == 1
    assert left == [] and ends_flat(venue)
    (close,) = report.closes.values()
    assert close[0] == "sent: sell 1 SPY at market" and close[-1] == "filled"
    assert report.closed_after_stop


def test_a_close_with_no_answer_found_working_with_nothing_to_close_is_canceled(fake_time) -> None:
    """The lost close was found working after another order had flattened the account; the search
    cleared the doubt without tracking the order, the account read flat, and the sell filled later."""
    venue = Venue(fake_time, qty=1)
    venue.listed_after = 10_000.0  # Alpaca holds it; the open listing does not show it
    venue.fail("close", no_answer(), placed=True, order={"fills": 100.0})
    venue.order("exit-9", "sell", at=1.0, fills=0.0)  # the node's own exit, answered, arriving late
    left, _ = check(venue)
    (close,) = venue.closes()
    assert venue.did("cancel") == [f"cancel {close.client_order_id}"]
    assert left == [] and close.status == "canceled" and ends_flat(venue)


def test_a_close_sent_in_the_LAST_POLL_is_canceled_though_the_reading_after_it_fails(
    fake_time,
) -> None:
    """The last poll sent a close as the session closed; the reading after it failed, so the close was in
    no reading and nothing canceled it: a DAY order left queued for the next session."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = 5.0
    venue.closes_at = LAST_POLL + 1.0  # the session closes a second after the last poll's close
    refused_until_the_LAST_POLL(venue)
    venue.fail("orders", no_answer(), times=None, when=lambda v: bool(v.closes()))
    left, _ = check(venue)
    (close,) = venue.closes()
    assert venue.did("cancel") == [f"cancel {close.client_order_id}"]
    assert close.status == "canceled"
    assert left == [f"open order {close.client_order_id}", "position of 1 SPY"]


def test_the_checks_close_is_left_to_fill_when_the_clock_cannot_be_read_as_it_ends(
    fake_time,
) -> None:
    """A clock read that failed as the check ended was taken for a closed session, and the check's own
    working close was canceled with the session open: the position stayed. The clock's last answer says
    when the session closes; a failed read is not that."""
    venue = Venue(fake_time, qty=1)
    venue.fill_after = session_window.AFTER_STOP_SECS + 30
    venue.fail("clock", no_answer(), times=None, when=lambda v: bool(v.closes()))
    left, _ = check(venue)
    assert venue.did("cancel") == []
    assert left and ends_flat(venue)  # recorded not flat; the close then fills


def test_a_node_exit_that_fills_as_the_check_reads_does_not_fail_a_flat_account(fake_time) -> None:
    """The position was read held, the node's own exit, sent with no answer, then filled, and the check's
    close, a `close_position` then, was answered 404 and taken for a close whose answer was lost: a flat
    account recorded failed. No close is sent while an order on its side may still be placed; the exit
    is found filled and the account flat."""
    venue = Venue(fake_time, qty=1, latency=0.1)
    venue.order("exit-9", "sell", at=0.25, fills=0.0)  # just after the first position read
    left, report = check(venue, {"exit-9": "sell"})
    assert left == [] and venue.did("close") == [] and not report.closed_after_stop
    assert ends_flat(venue)


def test_an_order_from_outside_the_window_that_fills_before_the_close_is_bought_back(
    fake_time,
) -> None:
    """What a market order does not have that `close_position` had: Alpaca does not refuse it for a
    position that has gone. An order the window knows nothing of (it is no order of the node's) that
    fills between the position read and the close leaves the close selling short; the next poll buys
    the share back, and the record shows both orders."""
    venue = Venue(fake_time, qty=1)
    venue.before["close"] = lambda: venue.order("by-hand", "sell", fills=0.0)
    left, report = check(venue)
    assert left == [] and ends_flat(venue)
    assert [c[0] for c in report.closes.values()] == [
        "sent: sell 1 SPY at market",
        "sent: buy 1 SPY at market",
    ]


SLOW = session_window.REQUEST_SECS
"""Every request answered just inside the adapter's timeouts."""


def test_orders_in_doubt_do_not_carry_the_check_past_its_bounds(fake_time) -> None:
    """Each poll looked every order in doubt up, uncounted: three put the last close past
    `LAST_CLOSE_SECS`, six past the session's close, twelve made the check 420 s and the lock's
    arithmetic wrong. A reading now looks at most `LOOKUPS` up, in turn, and the bounds count them."""
    w = session_window
    venue = Venue(fake_time, qty=1, latency=SLOW)
    venue.fail("close", insufficient(), times=None)
    doubtful = {f"entry-{n}": "buy" for n in range(12)}  # none ever found; none on the close's side
    left, _ = check(venue, doubtful)
    before = w.STOP_TIMEOUT_SECS + w.ADAPTER_CALL_SECS  # the stop and the node's last calls
    closes = [at for at, name in venue.calls if name.startswith("close")]
    assert closes and before + max(closes) <= w.LAST_CLOSE_SECS
    assert before + fake_time.now <= w.AFTER_END_SECS
    assert left[0] == "position of 1 SPY" and len(left) == 13
    # two readings, two lookups each: four were asked for, the rest never reached
    assert sum("not found at Alpaca" in what for what in left) == 4
    assert sum("not looked up in the check's time" in what for what in left) == 8


def test_the_last_close_goes_out_at_the_bound_counted_for_it(fake_time) -> None:
    """The bound's tests stubbed the position read at no time and held no order in doubt, so they could
    not see a request more. Here every request of the last poll takes its full timeout: the close is
    sent exactly where `LAST_CLOSE_SECS` puts it, less the time the poll started before the deadline."""
    w = session_window
    venue = Venue(fake_time, qty=1, latency=lambda v: SLOW if v.fake.now >= LAST_POLL else 0.0)
    venue.fail("close", insufficient(), times=None, when=lambda v: v.fake.now < LAST_POLL)
    left, _ = check(venue, {"entry-1": "buy", "entry-2": "buy"})
    (sent,) = [at for at, name in venue.calls if name.startswith("close sell") and at >= LAST_POLL]
    before = w.STOP_TIMEOUT_SECS + w.ADAPTER_CALL_SECS
    slack = w.AFTER_STOP_SECS - LAST_POLL
    assert before + sent + slack == pytest.approx(w.LAST_CLOSE_SECS)
    assert "position of 1 SPY" not in left


def test_the_whole_check_ends_at_the_bound_counted_for_it(fake_time) -> None:
    """The longest check: its last poll, begun just inside the deadline, and its last reading each look
    two orders up, read the orders, the position and the clock, and send two cancels, every request at
    its full timeout. It ends exactly where `AFTER_END_SECS` puts it, less the time the last poll started
    before the deadline; a request more in either would pass it."""
    w = session_window
    venue = Venue(fake_time, qty=1, latency=lambda v: SLOW if v.fake.now >= LAST_POLL else 0.0)
    venue.fill_after = None  # the check's close, sent in the first poll, works on
    # the node's two buys, sent with no answer, reach Alpaca as the last poll begins
    for late in ("entry-1", "entry-2"):
        venue.order(late, "buy", type="limit", at=LAST_POLL - 1, cancel_after=10_000.0)
    left, _ = check(venue, {"entry-1": "buy", "entry-2": "buy"})
    assert len(left) == 4  # three orders working and the position
    slack = w.AFTER_STOP_SECS - LAST_POLL
    before = w.STOP_TIMEOUT_SECS + w.ADAPTER_CALL_SECS
    assert before + fake_time.now + slack == pytest.approx(w.AFTER_END_SECS)


def test_a_skip_whose_heartbeat_cannot_be_written_still_exits_0_and_says_why(
    monkeypatch, tmp_path, capsys
) -> None:
    """A skip wrote its heartbeat before it printed, unguarded: an unwritable log folder turned a skip,
    which exits 0, into a traceback and exit 1."""
    heartbeat = session_window.write_heartbeat

    def unwritable(log_dir, outcome):
        if outcome.startswith(("skipped", "nothing to run")):
            raise PermissionError("read-only file")
        heartbeat(log_dir, outcome)

    def skipped(config, **kwargs):
        raise session_window.WindowSkipped("the last window ended under 300 s ago")

    monkeypatch.setattr(session_window, "write_heartbeat", unwritable)
    monkeypatch.setattr(session_window, "run_window", skipped)
    assert session_window.main(["--log-dir", str(tmp_path)]) == 0
    said = capsys.readouterr()
    assert "skipped: the last window ended under 300 s ago" in said.out
    assert "the heartbeat could not be written" in said.err


# A fourth independent review of the check as landed (2026-11-10): each shown here before its fix.


def test_an_order_canceled_in_the_last_poll_is_not_called_working_after_it(fake_time) -> None:
    """An order in doubt, found and canceled in the last poll, was added again after the deadline from the
    list the poll had kept, canceled a second time (a 422) and named as left: a flat window failed."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-1", "buy", type="limit", at=LAST_POLL - 0.5)
    left, report = check(venue, {"entry-1": "buy"})
    assert venue.did("cancel") == ["cancel entry-1"] and left == [] and report.errors == []


def test_an_order_found_in_the_last_poll_is_canceled_though_the_listing_then_fails(
    fake_time,
) -> None:
    """A lookup found the order in doubt working in the last poll, the open-orders read after it timed
    out, and the last cancels went to the last listing that had been read, which did not hold it: it was
    left working and the record said it was not found."""
    venue = Venue(fake_time, qty=0)
    venue.order("entry-1", "buy", type="limit", at=LAST_POLL - 0.5)
    venue.fail("orders", no_answer(), times=None, when=lambda v: v.fake.now >= LAST_POLL)
    left, _ = check(venue, {"entry-1": "buy"})
    assert venue.did("cancel") == ["cancel entry-1"] and not venue.working()
    assert left == ["open order entry-1"]


def test_an_interrupt_while_a_worker_call_is_waited_for_leaves_the_runner_abandoned(
    monkeypatch,
) -> None:
    """After a clean stop the runner was put with the abandoned only once the wait for its worker calls
    had returned, so a Ctrl-C during that wait let the window dispose the node under a running worker
    thread and `main` return without ending the process, which then hung at the interpreter's exit."""

    def interrupted():
        raise KeyboardInterrupt

    calls = SimpleNamespace(settle=interrupted)
    runner = SimpleNamespace(close=lambda: pytest.fail("the runner was closed"))
    monkeypatch.setattr(session_window, "_ABANDONED", [])
    with pytest.raises(KeyboardInterrupt):
        session_window._close_runner(cast(Any, runner), None, cast(Any, calls))
    assert session_window._ABANDONED == [(runner, None, calls)]


def test_the_clock_reads_own_retries_are_not_taken_from_the_window(
    monkeypatch, fake_time, tmp_path
) -> None:
    """The time left was measured from before the clock read, though the window's end is the timestamp
    that read returns: a clock answered after 95 s of retries had 95 s taken from a window that had not
    lost them, and a window with time enough was skipped."""

    class NodeBuilt(Exception): ...

    def built(*args, **kwargs):
        raise NodeBuilt

    closes = datetime(2026, 11, 10, 16, 0, tzinfo=NEW_YORK)
    left = session_window.END_MARGIN_SECS + session_window.FLATTEN_SECS + 85  # 205 s of window

    class SlowClock(Account):
        def get_clock(self) -> Clock:
            fake_time.now += 95  # alpaca-py's retries of a 504
            return clock(closes - timedelta(seconds=left), is_open=True)

    monkeypatch.setattr(session_window, "window_node", built)
    with pytest.raises(NodeBuilt):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path), trading=cast(TradingClient, SlowClock())
        )


# Code review of the account check as one set of unsettled requests (2026-11-10): each shown before its fix.


def test_a_close_that_was_never_sent_is_followed_by_another(fake_time) -> None:
    """A connect timeout on the close's only attempt proves nothing reached Alpaca (the adapter's
    `not_sent`); taken for a lost answer, it held every later close back and left the position."""
    venue = Venue(fake_time, qty=1)
    never_sent = requests.ConnectTimeout("connect timed out")
    never_sent.alpaca_sends = 1  # ty: ignore[missing-attribute] - as the adapter's `counted` leaves it
    venue.fail("close", never_sent)
    left, report = check(venue)
    assert venue.did("close") == ["close sell 1"] * 2 and left == []
    assert report.closes_not_placed == 1 and len(report.closes) == 1


def test_every_working_order_is_canceled_as_the_check_ends(fake_time) -> None:
    """The cancels as the check ends were cut to `CANCELS` like a poll's, so a third order working in the
    last reading was left working when the process exited. They go on while the step has time."""
    venue = Venue(fake_time, qty=0)
    for n in range(5):
        venue.order(f"late-{n}", "buy", type="limit", at=session_window.AFTER_STOP_SECS - 0.1)
    left, _ = check(venue, {"entry-9": "buy"})  # never found, so the check polls to its deadline
    assert len(left) == 6 and not venue.working()


def test_the_cancels_as_the_check_ends_stay_inside_their_counted_time(fake_time) -> None:
    w = session_window
    venue = Venue(
        fake_time, qty=0, latency=lambda v: SLOW if v.fake.now >= w.AFTER_STOP_SECS else 0.0
    )
    for n in range(5):
        venue.order(f"late-{n}", "buy", type="limit", at=w.AFTER_STOP_SECS - 0.1)
    check(venue, {"entry-9": "buy"})
    assert len(venue.did("cancel")) == w.CANCELS
    assert fake_time.now <= w.AFTER_STOP_SECS + w.POLL_AT_LONGEST_SECS


def test_an_answer_alpaca_py_cannot_read_does_not_end_the_check(fake_time) -> None:
    """Only `APIError` and `OSError` were caught: a response alpaca-py could not parse (a `ValueError`
    from its model) left the check at once, its remaining polls and last cancels unrun."""
    venue = Venue(fake_time, qty=0)
    venue.order("exit-1", "sell", type="limit")
    venue.fail("lookup", ValueError("1 validation error for Order"))
    left, report = check(venue, {"exit-1": "sell"})
    assert venue.did("cancel") == ["cancel exit-1"] and left == []
    assert any("looking up exit-1: ValueError" in e for e in report.errors)


def test_an_order_the_listing_shows_is_not_also_called_not_found(fake_time) -> None:
    """An unsettled order was shown only by its lookup: listed open while its lookup still answered 404,
    it was named both open and not found, and went on holding a close back after it was canceled."""
    venue = Venue(fake_time, qty=1)
    venue.order("exit-1", "sell", type="limit", seen_after=6.0, listed_after=0.0)
    left, report = check(venue, {"exit-1": "sell"})
    assert report.unsettled["exit-1"] == ["sell", "accepted", "canceled"]
    assert not any("not found at Alpaca" in e for e in report.errors)
    assert left == [] and venue.did("close") == ["close sell 1"]


def test_a_position_that_can_only_be_reported_is_read_once(fake_time) -> None:
    """With the session closed and nothing to look for, the check read the account a second time with
    nothing sent in between, each read up to a full timeout under the paper lock."""
    venue = Venue(fake_time, qty=1, is_open=False)
    left, _ = check(venue)
    assert [name for _, name in venue.calls] == ["orders", "position", "clock"]
    assert left == ["position of 1 SPY"]


# --- The strategy a window runs, from a fixed table (ADR 0068 step 3, ADR 0044) ---


def test_a_window_runs_session_ma_cross_unless_another_strategy_of_the_table_is_named() -> None:
    """The schedule passes no `--strategy`, so its windows stay `SessionMaCross`; a name outside the
    table is refused before anything is read."""
    assert WindowConfig().strategy == "SessionMaCross"
    assert session_window._parser().parse_args([]).strategy == "SessionMaCross"
    assert set(session_window.WINDOW_STRATEGIES) == {"SessionMaCross", "EmaCross", "Mover"}
    with pytest.raises(ValueError, match="strategy must be one of"):
        WindowConfig(strategy="GridMarketMaker")
    with pytest.raises(SystemExit):
        session_window._parser().parse_args(["--strategy", "GridMarketMaker"])


def test_ema_cross_is_refused_by_name_outside_the_regular_session(extended, ema_cross_runs) -> None:
    """`EmaCross` does not flatten on stop, and the account check closes at market in the regular
    session only, so an extended window of it could end holding a position."""
    with pytest.raises(ValueError, match="EmaCross runs in regular windows only"):
        WindowConfig(session="extended", strategy="EmaCross")


def test_auto_with_ema_cross_outside_the_regular_session_has_nothing_to_run_and_says_why(
    monkeypatch, tmp_path, capsys, extended
) -> None:
    """Review finding: `--session auto` resolved to the extended session whatever the strategy, so a run
    of `EmaCross` in pre-market failed (exit 1) where nothing was to run; it now says so by name and
    exits 0, keeping the last result. A named extended session is still refused (above)."""

    class PreMarket(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 10, 5, 0, tzinfo=NEW_YORK), is_open=False)

    monkeypatch.setattr(session_window, "paper_trading", lambda: PreMarket())
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    argv = ["--session", "auto", "--strategy", "EmaCross", "--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 0
    said = (
        "nothing to run: the regular session is closed, and EmaCross runs in regular windows only"
    )
    assert said in capsys.readouterr().out
    assert json.loads((tmp_path / "last-firing.json").read_text())["outcome"].startswith(said)
    assert not (tmp_path / "latest.json").exists()


def test_a_refusal_names_the_sessions_the_table_gives(monkeypatch) -> None:
    """Review finding: the refusal said "regular windows only" whatever the entry's sessions were."""
    monkeypatch.setitem(
        session_window.WINDOW_STRATEGIES,
        "EmaCross",
        session_window.WindowStrategy(
            session_window.WINDOW_STRATEGIES["EmaCross"].strategy_id, ("extended",), "a test"
        ),
    )
    with pytest.raises(
        ValueError, match="EmaCross runs in extended windows only, not in the regular"
    ):
        WindowConfig(strategy="EmaCross")


# --- Long-only: a window never opens a short (the lead's rule, 2026-11-11) ---


@pytest.fixture
def ema_cross_runs(monkeypatch):
    """`EmaCross` as the table held it before it was refused for selling while flat, for the tests of
    the window machinery it still exercises (its node, its record)."""
    from dataclasses import replace

    entry = session_window.WINDOW_STRATEGIES["EmaCross"]
    monkeypatch.setitem(
        session_window.WINDOW_STRATEGIES, "EmaCross", replace(entry, sells_while_flat="")
    )


def test_ema_cross_is_refused_by_name_because_it_sells_while_flat() -> None:
    """Upstream's `EmaCross` sends a market order at each cross whatever it holds, and on paper on
    2026-11-11 it sold 1 SPY while flat and held the short for the whole window. The table refuses it by
    name in every session, whatever the account's setting."""
    with pytest.raises(ValueError, match="EmaCross sells while flat") as refused:
        WindowConfig(strategy="EmaCross")
    assert "a window never opens a short" in str(refused.value)
    assert WindowConfig().strategy == "SessionMaCross"
    assert not session_window.WINDOW_STRATEGIES["SessionMaCross"].sells_while_flat


def test_a_window_of_ema_cross_is_refused_before_alpaca_is_read(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(session_window, "paper_trading", lambda: pytest.fail("Alpaca was read"))
    argv = ["--session", "regular", "--strategy", "EmaCross", "--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 1
    assert "EmaCross sells while flat" in capsys.readouterr().err


def test_a_window_starts_only_on_an_account_alpaca_holds_long_only(monkeypatch, tmp_path) -> None:
    """The start check reads Alpaca's account configuration and refuses, by name and before a node is
    built, an account whose `no_shorting` is false; the record of a window that ran says what it read."""
    assert session_window.VENUE_LONG_ONLY
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(no_shorting=False))
    monkeypatch.setattr(
        session_window, "window_node", lambda *a, **k: pytest.fail("a node was built")
    )
    with pytest.raises(session_window.WindowRefused, match="no_shorting is false") as refused:
        session_window.run_window(WindowConfig(log_dir=tmp_path))
    assert "paper:long-only" in str(refused.value)
    assert not (tmp_path / "latest.json").exists()
    monkeypatch.undo()
    monkeypatch.setattr(session_window, "window_data", lambda: Data())
    _, latest, _ = quiet_window(monkeypatch, tmp_path / "held", quotes=5, trades=0)
    assert latest["result"] == "ok" and latest["no_shorting"] is True


def test_a_refused_window_on_an_account_that_allows_shorting_is_recorded_by_name(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(no_shorting=False))
    argv = ["--session", "regular", "--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 1
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("refused: ") and "no_shorting is false" in latest["result"]


def test_in_report_mode_the_start_check_records_the_setting_and_starts_either_way(
    monkeypatch, tmp_path
) -> None:
    """`VENUE_LONG_ONLY` off is the state before the setting was first made: the check reads and
    records, and refuses nothing."""
    monkeypatch.setattr(session_window, "VENUE_LONG_ONLY", False)
    _, latest, _ = quiet_window(
        monkeypatch, tmp_path, quotes=5, trades=0, account=Account(no_shorting=False)
    )
    assert latest["result"] == "ok" and latest["no_shorting"] is False


def configured_client(monkeypatch, answer: dict) -> tuple[TradingClient, list]:
    """alpaca-py's own paper `TradingClient`, its HTTP calls answered here: `GET` with `answer`, a
    `PATCH` with what it sent, as Alpaca answers it. Each call is kept as (method, path, body)."""
    trading = TradingClient(api_key="stand-in", secret_key="stand-in", paper=True)
    calls: list = []

    def get(path, data=None, **kwargs):
        calls.append(("GET", path, data))
        return dict(answer)

    def patch(path, data=None, **kwargs):
        calls.append(("PATCH", path, data))
        return dict(data or {})

    monkeypatch.setattr(trading, "get", get)
    monkeypatch.setattr(trading, "patch", patch)
    return trading, calls


def test_the_setting_task_turns_long_only_on_and_off_by_alpaca_pys_documented_call(
    monkeypatch, capsys
) -> None:
    """`on` reads the configuration and sends it back through `set_account_configurations` with
    `no_shorting` true, everything else as read; `off` reverses it by the same call. It prints the
    setting's value and nothing else."""
    trading, calls = configured_client(monkeypatch, {**LONG_ONLY, "no_shorting": False})
    assert session_window.long_only_task("on", trading) == 0
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/account/configurations"),
        ("PATCH", "/account/configurations"),
    ]
    sent = calls[1][2]
    assert sent["no_shorting"] is True
    assert {k: v for k, v in sent.items() if k in LONG_ONLY and k != "no_shorting"} == {
        k: v for k, v in LONG_ONLY.items() if k != "no_shorting"
    }
    assert capsys.readouterr().out == "no_shorting: false -> true\n"

    trading, calls = configured_client(monkeypatch, LONG_ONLY)
    assert session_window.long_only_task("off", trading) == 0
    assert calls[1][2]["no_shorting"] is False
    assert capsys.readouterr().out == "no_shorting: true -> false\n"


def test_the_setting_tasks_check_reads_only_and_prints_only_the_value(monkeypatch, capsys) -> None:
    trading, calls = configured_client(monkeypatch, LONG_ONLY)
    assert session_window.long_only_task("check", trading) == 0
    assert [m for m, _, _ in calls] == ["GET"]
    assert capsys.readouterr().out == "no_shorting: true\n"


def test_the_setting_task_fails_naming_any_other_field_alpaca_changed(monkeypatch, capsys) -> None:
    """The PATCH sends the whole configuration as read (alpaca-py's `to_patch_payload`); a field that
    comes back different is named and the task fails, so a change beyond the setting is not missed."""
    trading = TradingClient(api_key="stand-in", secret_key="stand-in", paper=True)
    monkeypatch.setattr(trading, "get", lambda path, data=None, **kw: dict(LONG_ONLY))
    monkeypatch.setattr(
        trading,
        "patch",
        lambda path, data=None, **kw: {**(data or {}), "max_options_trading_level": None},
    )
    assert session_window.long_only_task("off", trading) == 1
    said = capsys.readouterr()
    assert said.out == "no_shorting: true -> false\n"
    assert "max_options_trading_level" in said.err
    with pytest.raises(ValueError, match="check, on or off"):
        session_window.long_only_task("short", trading)


def test_the_setting_task_sends_nothing_when_the_setting_is_already_as_asked(
    monkeypatch, capsys
) -> None:
    trading, calls = configured_client(monkeypatch, LONG_ONLY)
    assert session_window.long_only_task("on", trading) == 0
    assert [m for m, _, _ in calls] == ["GET"]
    assert capsys.readouterr().out == "no_shorting: true (unchanged)\n"


def test_the_refusal_names_a_task_that_exists_whatever_the_environment(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv(session_window.ACCOUNT_ENV, raising=False)
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(no_shorting=False))
    with pytest.raises(session_window.WindowRefused, match="paper:long-only windows on"):
        session_window.run_window(WindowConfig(log_dir=tmp_path))


def test_the_setting_task_reads_one_named_accounts_pair_and_holds_its_lock_to_change_it() -> None:
    """`paper:long-only <tests|windows> <check|on|off>`: account A's pair and lock for `tests`, account
    B's for `windows` (ADR 0069); the pair is read into the task's environment only, never echoed;
    a change waits for the account's lock, so no paper run is mid-way; `check` takes none."""
    root = Path(__file__).resolve().parents[1]
    task = tomllib.loads((root / "mise.toml").read_text())["tasks"]["paper:long-only"]
    run = task["run"]
    assert "tests) svc=canon-alpaca-paper; lock=canon-paper-test.lock ;;" in run
    assert "windows) svc=canon-alpaca-paper-windows; lock=canon-paper-windows.lock ;;" in run
    assert 'choices "check" "on" "off"' in task["usage"]
    assert "long_only_task" in run and '"$usage_action"' in run
    reads = [line for line in run.splitlines() if "find-generic-password" in line]
    assert len(reads) == 2 and all("/usr/bin/timeout 30 " in line for line in reads)
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", run)
    assert '[ "$usage_action" = check ] ||' in run


def test_the_ema_cross_window_node_adds_upstreams_builtin_beside_the_cap(
    monkeypatch, tmp_path, ema_cross_runs
) -> None:
    """`window_node` adds `EmaCross` through `add_builtin_strategy` with `paper_node.ema_cross_config`
    (one share an order, upstream's periods) and the cap beside it; with no managed stop to wait for,
    the post-stop delay is the node's own. A spy on each module function records that it was called."""
    calls: list[tuple[str, dict]] = []
    for name in ("ema_cross_config", "position_cap_config", "alpaca_paper_builder"):
        real = getattr(session_window, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append((_name, kwargs))
            return _real(*args, **kwargs)

        monkeypatch.setattr(session_window, name, spy)
    at = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    node, s, cap = session_window.window_node(
        WindowConfig(strategy="EmaCross", log_dir=tmp_path),
        wind_down_at=at,
        log_name="e",
        feed="iex",
    )
    try:
        assert [n for n, _ in calls] == [
            "alpaca_paper_builder",
            "ema_cross_config",
            "position_cap_config",
        ]
        assert calls[0][1]["delay_post_stop_secs"] == session_window.DELAY_POST_STOP_SECS
        assert calls[0][1]["max_order_submit_rate"] == session_window.EMA_CROSS_SUBMIT_RATE
        assert cap.max_position == 1 and not node.is_running
        assert (s.quotes, s.trades, s.live_bars, list(s.dropped)) == (0, 0, 0, [])
    finally:
        node.dispose()


def test_an_ema_cross_window_refuses_a_feed_its_subscription_cannot_name(
    tmp_path, ema_cross_runs
) -> None:
    """`EmaCross` subscribes with no params, so it streams the adapter's default feed (IEX): a window
    whose part names another would trade on data it did not ask for."""
    with pytest.raises(RuntimeError, match="EmaCross names no feed"):
        session_window.window_node(
            WindowConfig(strategy="EmaCross", log_dir=tmp_path),
            wind_down_at=datetime(2026, 11, 10, 21, 30, tzinfo=UTC),
            log_name="e",
            feed="boats",
        )


def ema_quotes(n: int) -> list:
    """Quotes whose mid swings $2 around $750, slowly enough for `EmaCross` to cross a few times."""
    out = []
    for i in range(n):
        mid = 750 + 2 * math.sin(i / 40.0)
        ts = T0 + i * 1_000_000_000
        out.append(
            QuoteTick(
                SPY,
                Price(mid - 0.01, 4),
                Price(mid + 0.01, 4),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts,
                ts,
            )
        )
    return out


def test_the_record_reads_a_builtins_orders_and_data_from_the_nodes_cache() -> None:
    """A built-in runs in Rust and hands the window no Python object: its orders are read from the cache
    by its own strategy ID, and the quotes and trades that reached it from the cache's counts."""
    from canon_trading.paper_node import EMA_CROSS
    from canon_trading.paper_node import EMA_CROSS_ID
    from canon_trading.paper_node import ema_cross_config

    eng = engine()
    eng.add_data(ema_quotes(600))
    eng.add_builtin_strategy(EMA_CROSS, ema_cross_config(SPY))
    eng.run()
    try:
        seen = session_window.CachedData(eng.cache, SPY)
        report = session_window.WindowReport("regular", "SPY", started="t")
        session_window._fill(report, eng.cache, EMA_CROSS_ID)
        assert seen.quotes == 600 and seen.trades == 0
        assert len(report.orders) >= 2
        assert session_window._unsettled(eng.cache, EMA_CROSS_ID) == {}
        # SessionMaCross's ID holds none of them
        assert session_window._unsettled(eng.cache, session_window.STRATEGY_ID) == {}
    finally:
        eng.dispose()


def test_a_window_record_names_its_strategy(monkeypatch, tmp_path, ema_cross_runs) -> None:
    """The record, `running` and the result alike, names the strategy, and `run_window` reads that
    strategy's orders by its own ID."""
    _, latest, _ = quiet_window(monkeypatch, tmp_path, quotes=5, trades=0, strategy="EmaCross")
    assert latest["strategy"] == "EmaCross" and latest["result"] == "ok"
    _, latest, _ = quiet_window(monkeypatch, tmp_path / "default", quotes=5, trades=0)
    assert latest["strategy"] == "SessionMaCross"


def test_a_refused_window_record_names_its_strategy(monkeypatch, tmp_path, ema_cross_runs) -> None:
    held = SimpleNamespace(client_order_id="someone-elses", symbol="SPY")
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account(orders=[held]))
    argv = ["--session", "regular", "--strategy", "EmaCross", "--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 1
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["result"].startswith("refused: ") and latest["strategy"] == "EmaCross"


def test_the_schedule_and_the_task_pass_strategy_through_and_name_none() -> None:
    """`--strategy` reaches the window through `paper:session` and the runner, which pass their
    arguments on; the schedule names none, so its windows stay `SessionMaCross`."""
    root = Path(__file__).resolve().parents[1]
    task = tomllib.loads((root / "mise.toml").read_text())["tasks"]["paper:session"]
    assert '"$@"' in task["run"] and "--strategy" not in task["run"]
    runner = (root / "home/.claude/canon/paper-session.sh").read_text()
    assert 'paper:session "$@"' in runner
    automation = (root / "scripts/automation").read_text()
    assert "<string>--session</string>" in automation  # the schedule's own arguments, in its plist
    assert "<string>--strategy</string>" not in automation


def test_a_session_ma_cross_window_keeps_the_ceiling_submit_rate(monkeypatch, tmp_path) -> None:
    seen: list[dict] = []
    real = session_window.alpaca_paper_builder

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_window, "alpaca_paper_builder", spy)
    node, _, _ = session_window.window_node(
        WindowConfig(log_dir=tmp_path),
        wind_down_at=datetime(2026, 11, 10, 21, 30, tzinfo=UTC),
        log_name="r",
        feed="iex",
    )
    node.dispose()
    assert seen[0]["max_order_submit_rate"] == MAX_ORDER_SUBMIT_RATE


def test_an_ema_cross_window_sends_one_order_and_the_engine_denies_every_later_cross() -> None:
    """`EmaCross` at the pin reads neither its position nor its working orders, and an order refused
    still flips its side, so a second order can stack a position: under the window's submit budget,
    the engine's own rate limit, one order reaches the venue in a window of any length the window
    allows, and every later cross is denied before it leaves the node (ADR 0037's budget)."""
    from canon_trading.paper_node import EMA_CROSS
    from canon_trading.paper_node import EMA_CROSS_ID
    from canon_trading.paper_node import ema_cross_config

    budget = session_window.EMA_CROSS_SUBMIT_RATE
    count, span = budget.split("/")
    hours, minutes, seconds = (int(x) for x in span.split(":"))
    assert int(count) == 1
    assert hours * 3600 + minutes * 60 + seconds >= session_window.MAX_MINUTES * 60
    eng = engine(max_order_submit_rate=budget)
    eng.add_data(ema_quotes(session_window.MAX_MINUTES * 60))  # a quote a second, an hour
    eng.add_builtin_strategy(EMA_CROSS, ema_cross_config(SPY))
    eng.run()
    try:
        orders = eng.cache.orders(strategy_id=EMA_CROSS_ID)
        reasons = [
            str(e.reason) for o in orders for e in o.events() if type(e).__name__ == "OrderDenied"
        ]
        sent = [o for o in orders if "OrderSubmitted" in [type(e).__name__ for e in o.events()]]
        assert len(orders) >= 4 and len(sent) == 1
        assert reasons == ["RATE_LIMIT_EXCEEDED"] * (len(orders) - 1)
    finally:
        eng.dispose()


@pytest.mark.parametrize(
    ("set_to", "recorded"),
    [("windows", "windows"), ("tests", "tests"), (None, "unknown"), ("PKMADEUP", "unknown")],
)
def test_a_window_record_names_the_paper_account_its_task_set_and_nothing_else(
    monkeypatch, tmp_path, set_to, recorded
) -> None:
    """A reader of the series sees which paper account a window used: the paper task names it
    (`ACCOUNT_ENV`), the record carries `windows` or `tests`, and any other value is recorded as `unknown`,
    never echoed, so nothing the variable held by mistake reaches a record."""
    if set_to is None:
        monkeypatch.delenv(session_window.ACCOUNT_ENV, raising=False)
    else:
        monkeypatch.setenv(session_window.ACCOUNT_ENV, set_to)
    report = session_window.WindowReport("regular", "SPY", started="20261111T143500Z")
    assert report.account == recorded
    session_window.write_record(tmp_path, report)
    body = (session_window.records_dir(tmp_path) / "latest.json").read_text()
    assert json.loads(body)["account"] == recorded
    assert "PKMADEUP" not in body


def test_a_refused_window_records_its_account_too(monkeypatch, tmp_path) -> None:
    """The refusal's own record, written by `main`, names the account as a window's report does."""
    monkeypatch.setenv(session_window.ACCOUNT_ENV, "windows")

    def refused(*_args, **_kwargs):
        raise session_window.WindowRefused("the account is not flat")

    monkeypatch.setattr(session_window, "run_window", refused)
    assert session_window.main(["--log-dir", str(tmp_path), "--session", "regular"]) == 1
    record = json.loads((session_window.records_dir(tmp_path) / "latest.json").read_text())
    assert record["result"].startswith("refused") and record["account"] == "windows"


def test_the_window_task_names_the_account_whose_pair_it_reads() -> None:
    """`paper:session` names its account `windows`, and the pair it reads is that account's service, the
    one `paper:check windows` checks, so the label cannot drift from the keys (ADR 0069)."""
    task = tomllib.loads((session_window.SOURCE_ROOT / "mise.toml").read_text())["tasks"][
        "paper:session"
    ]
    assert task["env"][session_window.ACCOUNT_ENV] == "windows"
    for item in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        assert f"-s canon-alpaca-paper-windows -a {item} -w" in task["env"][item]["value"]


# A window on today's top mover, as a check of the stack on a mover (ADR 0083, its dated line of
# 2026-11-11): the symbol chosen once before the node is built, one share inside the cap for any symbol,
# and the account checked whole at the start and after the stop.

TODAY = date(2026, 11, 10)
"""`Account`'s clock day: 11:00 New York, 90 minutes after `CALENDAR`'s regular open."""
AT = datetime(2026, 11, 10, 11, 0, tzinfo=NEW_YORK)


@dataclass
class Name:
    """One gainer as the fakes serve it: its last completed close, today's one bar (09:31 New York), its
    latest sip trade and how old that is at `AT`, and whether Alpaca's asset is tradable."""

    reference: float
    close: float
    volume: float
    price: float
    trade_age: float = 5.0
    tradable: bool = True
    name: str = "Example Inc. Common Stock"
    action: str = ""
    """A corporate action type Alpaca lists for it since the last session, if any."""
    ask: float | None = None
    """Its latest sip quote's ask, if the snapshot has one."""
    partial: float | None = None
    """The close of a bar for the minute still in progress at `AT`, if Alpaca serves one."""


NAMES = {
    # score = (close / reference - 1) x (close x volume / 100), the normal being 100 dollars a session
    "BBB": Name(1000.0, 1500.0, 10, 1500.0),  # 75: one share is over the cap
    "AAA": Name(3.0, 4.5, 1000, 4.5, name="Example Acquisition Corp. Warrants"),  # 22.5
    "CCC": Name(10.0, 15.0, 200, 15.0, tradable=False),  # 15
    "DDD": Name(10.0, 15.0, 150, 15.0, trade_age=300.0),  # 11.25: no sip trade for 300 s
    "KKK": Name(10.0, 15.0, 140, 15.0, action="reverse_splits"),  # 10.5
    "LLL": Name(1.0, 1.5, 1200, 1.5),  # 9.0 at 1.50 USD: no price floor, so the pick
    "EEE": Name(10.0, 12.0, 200, 12.0),  # 4.8
    "FFF": Name(10.0, 11.0, 100, 11.0),  # 1.1
    "GGG": Name(10.0, 9.0, 1000, 9.0),  # fallen: not ranked
}


def raw_bar(t: datetime, close: float, volume: float) -> dict:
    stamp = t.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "t": stamp,
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": volume,
        "n": 1,
        "vw": close,
    }


class Data:
    """A stand-in for alpaca-py's `StockHistoricalDataClient` (0.45.0): latest trades, snapshots and minute
    bars of `names`; every request is kept."""

    def __init__(self, names: dict[str, Name] | None = None, *, price: float = 750.0) -> None:
        self.names = NAMES if names is None else names
        self.price = price
        self.requests: list = []

    def get_stock_latest_trade(self, request_params) -> dict:
        from alpaca.data.models.trades import Trade

        self.requests.append(request_params)
        symbol = request_params.symbol_or_symbols
        raw = {"t": AT.astimezone(UTC).isoformat(), "p": self.price, "s": 1, "x": "V", "i": 1}
        return {symbol: Trade(symbol, raw)}

    def get_stock_snapshot(self, request_params) -> dict:
        self.requests.append(request_params)
        before = SimpleNamespace(timestamp=datetime(2026, 11, 9, 5, tzinfo=UTC), close=0.0)
        return {
            s: SimpleNamespace(
                daily_bar=None,
                previous_daily_bar=SimpleNamespace(timestamp=before.timestamp, close=n.reference),
                latest_trade=SimpleNamespace(
                    price=n.price, timestamp=AT - timedelta(seconds=n.trade_age)
                ),
                latest_quote=None
                if n.ask is None
                else SimpleNamespace(ask_price=n.ask, bid_price=0),
            )
            for s in request_params.symbol_or_symbols
            if (n := self.names.get(s)) is not None
        }

    def get_stock_bars(self, request_params):
        from alpaca.data.models.bars import BarSet

        self.requests.append(request_params)
        start = request_params.start.replace(tzinfo=UTC)
        if start.astimezone(NEW_YORK).date() == TODAY:  # today's, from the open
            bars = {
                s: [raw_bar(start + timedelta(minutes=1), n.close, n.volume)]
                + ([] if n.partial is None else [raw_bar(AT, n.partial, 1)])
                for s, n in self.names.items()
                if s in request_params.symbol_or_symbols
            }
        else:  # a prior session's same minutes: 100 dollars each
            bars = {s: [raw_bar(start, 10.0, 10)] for s in request_params.symbol_or_symbols}
        return BarSet(bars)


class Screener:
    """A stand-in for alpaca-py's `ScreenerClient`: Alpaca's movers, `names` as its gainers in order."""

    def __init__(self, names: dict[str, Name] | None = None) -> None:
        self.names = NAMES if names is None else names
        self.requests: list = []

    def get_market_movers(self, request_params):
        from alpaca.data.models.screener import Mover
        from alpaca.data.models.screener import Movers

        self.requests.append(request_params)
        gainers = [
            Mover(
                symbol=s,
                percent_change=(n.close / n.reference - 1) * 100,
                change=0.0,
                price=n.price,
            )
            for s, n in self.names.items()
        ]
        return Movers(gainers=gainers, losers=[], market_type="stocks", last_updated=AT)


class Actions:
    """A stand-in for alpaca-py's `CorporateActionsClient`: each of `NAMES` with an `action` has one."""

    def __init__(self) -> None:
        self.requests: list = []

    def get_corporate_actions(self, request_params):
        from alpaca.data.models.corporate_actions import CorporateActionsSet

        self.requests.append(request_params)
        found: dict[str, list] = {}
        for s in request_params.symbols:
            if (n := NAMES.get(s)) is not None and n.action == "reverse_splits":
                found.setdefault("reverse_splits", []).append(
                    {
                        "id": "00000000-0000-0000-0000-000000000000",
                        "symbol": s,
                        "old_cusip": "000000000",
                        "new_cusip": "000000001",
                        "new_rate": 1,
                        "old_rate": 10,
                        "process_date": "2026-11-10",
                        "ex_date": "2026-11-10",
                    }
                )
        return CorporateActionsSet(found)


PRIOR_DAY = Calendar(
    date="2026-11-09", open="09:30", close="16:00", session_open="0400", session_close="2000"
)


class Assets(Account):
    """`Account` with Alpaca's assets for `NAMES`, active, tradable as each says; reads counted."""

    def get_calendar(self, filters=None) -> list[Calendar]:
        return [PRIOR_DAY, CALENDAR]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.assets: list[str] = []

    def get_asset(self, symbol):
        from alpaca.trading.models import Asset

        self.assets.append(symbol)
        name = NAMES.get(symbol)
        return Asset(
            id=uuid4(),
            **{"class": "us_equity"},
            exchange="NASDAQ",
            symbol=symbol,
            name=name.name if name else "Example Inc. Common Stock",
            status="active",
            tradable=name.tradable if name else True,
            marginable=True,
            shortable=True,
            fractionable=True,
        )


@pytest.fixture(autouse=True)
def priced(monkeypatch):
    """Every window reads one share's price before it starts: here SPY's at $750 unless a test says
    otherwise; no test reads Alpaca's screener unless it hands one in."""
    monkeypatch.setattr(session_window, "window_data", lambda: Data(), raising=False)
    monkeypatch.setattr(
        session_window,
        "window_screener",
        lambda: pytest.fail("the screener was read"),
        raising=False,
    )
    monkeypatch.setattr(session_window, "window_actions", lambda: Actions(), raising=False)

    def no_stream():
        raise session_window.StreamSampleFailed("no stream in tests")

    monkeypatch.setattr(session_window, "window_stream", no_stream, raising=False)


def test_one_share_must_fit_the_cap_for_any_symbol(monkeypatch, tmp_path) -> None:
    """The window took SPY only, because the notional cap is sized for SPY. Now any symbol runs whose one
    share fits the per-order cap, read from its latest sip trade before the node is built."""
    WindowConfig(symbol="AAPL")  # no longer refused by name
    built: list = []

    class Built(Exception):
        pass

    def node(config, **kw):
        built.append(config)
        raise Built

    monkeypatch.setattr(session_window, "window_node", node)
    monkeypatch.setattr(session_window, "_flatten_after_stop", lambda *a, **k: [])
    monkeypatch.setattr(session_window, "window_data", lambda: Data(price=1000.01))
    with pytest.raises(session_window.WindowRefused, match="one share of AAPL at 1000.01 USD"):
        session_window.run_window(
            WindowConfig(symbol="AAPL", log_dir=tmp_path), trading=cast(TradingClient, Account())
        )
    assert built == []
    data = Data(price=250.0)
    monkeypatch.setattr(session_window, "window_data", lambda: data)
    with pytest.raises(Built):  # it got as far as building the node
        session_window.run_window(
            WindowConfig(symbol="AAPL", log_dir=tmp_path / "b"),
            trading=cast(TradingClient, Account()),
        )
    assert [c.symbol for c in built] == ["AAPL"]
    [asked] = data.requests
    assert asked.symbol_or_symbols == "AAPL" and asked.feed.value == "sip"


def test_the_start_check_reads_the_whole_account(monkeypatch, tmp_path) -> None:
    """The start check read open orders and the position in the window's symbol only, so a window could
    start beside an order or a position in another symbol."""

    class Elsewhere(Account):
        def get_all_positions(self) -> list:
            return [held_position("QQQ", 3)]

    monkeypatch.setattr(session_window, "window_node", lambda *a, **k: pytest.fail("built"))
    with pytest.raises(session_window.WindowRefused, match=r"not flat; not starting.*3 QQQ"):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path), trading=cast(TradingClient, Elsewhere())
        )
    order = SimpleNamespace(client_order_id="theirs", symbol="QQQ")
    with pytest.raises(session_window.WindowRefused, match="open order theirs in QQQ"):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path), trading=cast(TradingClient, Account(orders=[order]))
        )


def held_position(symbol: str, qty: int) -> Position:
    return Position(
        asset_id=uuid4(),
        symbol=symbol,
        exchange="NASDAQ",
        asset_class="us_equity",
        avg_entry_price="100",
        qty=str(qty),
        side="long" if qty > 0 else "short",
        cost_basis=str(100 * qty),
    )


def test_the_check_after_the_stop_reads_the_whole_account_and_touches_only_its_symbol(
    tmp_path,
) -> None:
    """After the stop the check read the window's symbol only, so an order or a position elsewhere was
    called flat. It names them now, and sends nothing for them: they are not the window's."""
    sent: list = []

    class Elsewhere(Account):
        def get_orders(self, filter=None) -> list:
            assert getattr(filter, "symbols", None) is None  # the whole account
            return [SimpleNamespace(id="o-1", client_order_id="theirs", symbol="QQQ", status="new")]

        def get_all_positions(self) -> list:
            return [held_position("QQQ", 2)]

        def cancel_order_by_id(self, order_id) -> None:
            sent.append(("cancel", order_id))

        def submit_order(self, order_data) -> None:
            sent.append(("submit", order_data))

    report = session_window.WindowReport("regular", "SPY", started="20261110T160000Z")
    left = session_window._flatten_after_stop(
        cast(TradingClient, Elsewhere()), WindowConfig(log_dir=tmp_path), report
    )
    assert left == ["open order theirs in QQQ", "position of 2 QQQ"]
    assert sent == []


def test_a_mover_is_the_top_ranked_name_that_passes_every_filter() -> None:
    """The pick is the first of the movers ranking (`movers_stream.rank`, UNCALIBRATED) over Alpaca's
    gainers that is active and tradable, not a warrant, unit or right, with no corporate action since the
    last session, a sip trade inside `FRESH_TRADE_SECS`, and one share inside the cap; no price floor.
    Each name passed over is named with why."""
    trading, data, screener, actions = Assets(), Data(), Screener(), Actions()
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    pick = session_window.select_mover(
        cast(TradingClient, trading),
        cast(Any, data),
        cast(Any, screener),
        cast(Any, actions),
        AT,
        opens,
        date(2026, 11, 9),
    )
    assert pick.symbol == "LLL" and pick.price == Decimal("1.5")
    record = pick.record
    assert record["symbol"] == "LLL" and record["source"] == "screener"
    assert record["inputs"]["score"] == pytest.approx(9.0)
    assert record["inputs"]["move"] == pytest.approx(0.5)
    assert record["inputs"]["dvol_ratio"] == pytest.approx(18.0)
    assert [g["symbol"] for g in record["screener"]] == list(NAMES)
    ranked = ["BBB", "AAA", "CCC", "DDD", "KKK", "LLL", "EEE", "FFF"]
    assert [r["symbol"] for r in record["ranked"]] == ranked
    passed = record["passed_over"]
    assert list(passed) == ["BBB", "AAA", "CCC", "DDD", "KKK"]
    assert "cap" in passed["BBB"] and "warrant" in passed["AAA"]
    assert "tradable" in passed["CCC"] and "300 s" in passed["DDD"]
    assert "reverse_splits" in passed["KKK"]
    assert record["elapsed_minutes"] == 90
    assert trading.assets == ["AAA", "CCC", "KKK", "LLL"]  # only names fresh and inside the cap
    [movers] = screener.requests
    assert movers.top == 50
    [asked] = actions.requests
    assert asked.symbols == ranked
    assert str(asked.start) == "2026-11-09" and str(asked.end) == "2026-11-10"
    assert all(str(getattr(r, "feed", "")) in {"sip", "DataFeed.SIP"} for r in data.requests)


def test_no_mover_qualifying_refuses_the_window_naming_each_name() -> None:
    names = {"BBB": NAMES["BBB"], "AAA": NAMES["AAA"]}
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    with pytest.raises(session_window.WindowRefused, match=r"no mover.*BBB.*AAA"):
        session_window.select_mover(
            cast(TradingClient, Assets()),
            cast(Any, Data(names)),
            cast(Any, Screener(names)),
            cast(Any, Actions()),
            AT,
            opens,
            date(2026, 11, 9),
        )


def test_a_mover_window_runs_the_pick_on_sip_and_records_the_selection(
    monkeypatch, tmp_path
) -> None:
    """A mover window's symbol is chosen once, before the node is built, and the node is built on it,
    streaming sip; the record names the symbol, its rank inputs and the screener's list."""
    data, screener = Data(), Screener()
    monkeypatch.setattr(session_window, "window_data", lambda: data)
    monkeypatch.setattr(session_window, "window_screener", lambda: screener)
    _, latest, seen = quiet_window(
        monkeypatch, tmp_path, quotes=5, trades=5, account=Assets(), mover=True
    )
    assert latest["symbol"] == "LLL" and latest["feed"] == "sip"
    assert latest["mover"]["inputs"]["score"] == pytest.approx(9.0)
    assert latest["mover"]["source"] == "screener"
    assert "no stream in tests" in latest["mover"]["stream_failed"]
    assert [g["symbol"] for g in latest["mover"]["screener"]] == list(NAMES)
    assert latest["share_price"] == "1.5"
    assert seen["config"].symbol == "LLL" and seen["feed"] == "sip"


def test_a_mover_window_runs_in_the_regular_session_only() -> None:
    with pytest.raises(ValueError, match="regular session only"):
        WindowConfig(mover=True, session="extended")


def test_the_mover_flag_and_a_symbol_are_not_both_taken(capsys) -> None:
    with pytest.raises(SystemExit):
        session_window._args(["--mover", "--symbol", "AAPL", "--log-dir", "/tmp/x"])
    assert session_window._args(["--mover", "--log-dir", "/tmp/x"]).mover is True


def test_auto_runs_no_mover_window_outside_the_regular_session(
    monkeypatch, tmp_path, capsys
) -> None:
    """The schedule fires `--session auto`; with `--mover` outside the regular session it has nothing to
    run, rather than a refusal every evening and morning."""

    class Evening(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 10, 17, 0, tzinfo=NEW_YORK), is_open=False)

    monkeypatch.setattr(session_window, "paper_trading", lambda: Evening())
    monkeypatch.setattr(session_window, "run_window", lambda *a, **k: pytest.fail("a window ran"))
    argv = ["--session", "auto", "--mover", "--log-dir", str(tmp_path)]
    assert session_window.main(argv) == 0
    assert "regular session only" in capsys.readouterr().out


def test_the_schedule_fires_the_mover_path() -> None:
    """The scheduled windows run on the day's mover, not SPY (ADR 0083's dated line of 2026-11-11): the
    plist `scripts/automation` writes passes `--mover` beside `--session auto`."""
    automation = (session_window.SOURCE_ROOT / "scripts/automation").read_text()
    assert "<string>auto</string>" in automation and "<string>--mover</string>" in automation


@pytest.mark.parametrize(
    ("named", "hour", "is_open", "mover", "ran"),
    [
        ("Mover", 11, True, True, [("Mover", "regular", True)]),
        ("Mover", 17, False, True, []),  # a mover window runs in the regular session only
        ("EmaCross", 17, False, False, [(session_window.DEFAULT_STRATEGY, "extended", False)]),
    ],
)
def test_auto_runs_the_strategy_the_schedule_names_for_the_regular_session(
    monkeypatch, tmp_path, named, hour, is_open, mover, ran
) -> None:
    """The schedule names the regular session's strategy (`--regular-strategy`, ADR 0091). With
    Alpaca's clock open the window is that strategy's; with it closed the name is not used, and the
    part runs what `--strategy` says, or nothing."""
    seen = []

    class At(Account):
        def get_clock(self) -> Clock:
            return clock(datetime(2026, 11, 10, hour, 0, tzinfo=NEW_YORK), is_open=is_open)

    def window(config, **_kwargs):
        seen.append((config.strategy, config.session, config.mover))
        raise session_window.WindowSkipped("the test stops here")

    monkeypatch.setattr(session_window, "EXTENDED_READY", True)
    monkeypatch.setattr(session_window, "paper_trading", lambda: At())
    monkeypatch.setattr(session_window, "run_window", window)
    argv = ["--session", "auto", "--regular-strategy", named, "--log-dir", str(tmp_path)]
    assert session_window.main(argv + (["--mover"] if mover else [])) == 0
    assert seen == ran


def test_a_regular_strategy_is_named_only_for_the_schedules_auto_session(capsys) -> None:
    with pytest.raises(SystemExit):
        session_window._args(["--regular-strategy", "Mover", "--mover", "--log-dir", "/tmp/x"])
    assert "needs --session auto" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # refused when the arguments are read, not at the first open
        session_window._args(
            ["--session", "auto", "--regular-strategy", "Mover", "--log-dir", "/tmp/x"]
        )
    assert "needs --mover" in capsys.readouterr().err


def test_the_schedule_names_the_mover_strategy_for_the_regular_session() -> None:
    """The scheduled agent's arguments name `Mover` for the regular session and no strategy for any
    other part (the lead's decision of 2026-11-11, ADR 0091)."""
    automation = (session_window.SOURCE_ROOT / "scripts/automation").read_text()
    named = "<string>--regular-strategy</string>\n    <string>$PAPER_REGULAR_STRATEGY</string>"
    assert named in automation and "\nPAPER_REGULAR_STRATEGY=Mover " in automation
    assert "<string>--strategy</string>" not in automation


def test_a_mover_window_enters_with_limits_never_a_market_order(tmp_path) -> None:
    """An entry is never a market order: a mover window's `SessionMaCross` sends its marked DAY limit
    through the touch in the regular session too, and its stop is the limit wind-down, not the managed
    market exit; a regular SPY window keeps its market orders."""
    node, strategy, _ = session_window.window_node(
        WindowConfig(symbol="LLL", mover=True, log_dir=tmp_path),
        wind_down_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
        log_name="w",
        feed="sip",
    )
    try:
        assert isinstance(strategy, SessionMaCross)
        assert strategy.extended_hours is True and strategy.manage_stop is False
        assert str(strategy.bar_type) == "LLL.ALPACA-1-MINUTE-LAST-INTERNAL"
    finally:
        node.dispose()
    assert not WindowConfig(log_dir=tmp_path).limits


class SampleStream:
    """A stand-in for alpaca-py's `StockDataStream` (0.45.0) for the mover sample: `run` hands each of
    `names` one minute bar at `AT`, decoded as `raw_data=True` gives it, then blocks until `stop`, or
    raises `fail` once the bars are sent."""

    def __init__(
        self, names: dict[str, Name] | None = None, fail: str = "", later: dict | None = None
    ) -> None:
        import threading

        self.names = NAMES if names is None else names
        self.later = later or {}
        """More bars, by symbol: how many minutes after `AT` each comes."""
        self.fail = fail
        self.fed, self.stopped = threading.Event(), threading.Event()
        self.subscribed: list = []
        self.handler = None

    def subscribe_bars(self, handler, *symbols) -> None:
        self.handler, self.subscribed = handler, [*self.subscribed, symbols]

    def run(self) -> None:
        import asyncio

        assert self.handler is not None
        for s, n in self.names.items():
            asyncio.run(self.handler({"S": s, "t": AT, "c": n.close, "v": n.volume}))
        for s, minutes in self.later.items():
            n = self.names[s]
            for m in minutes:
                at = AT + timedelta(minutes=m)
                asyncio.run(self.handler({"S": s, "t": at, "c": n.close, "v": n.volume}))
        self.fed.set()
        if self.fail:
            raise RuntimeError(self.fail)
        self.stopped.wait(5)

    def stop(self) -> None:
        self.stopped.set()

    def wait(self, _: float) -> None:
        """The sample's sleep: until the bars are sent."""
        self.fed.wait(5)


def test_a_stream_sample_ranks_the_whole_market_and_picks_the_first_that_passes() -> None:
    """The live ranking (ADR 0083's dated line of 2026-11-11): the whole market's sip minute bars for a
    short sample, ranked by `movers_stream.rank` on `movers_stream.refresh`'s references and normals, then
    the same filters as the screener's path; Alpaca's gainers are read for the record only."""
    stream, trading, data, screener, actions = (
        SampleStream(),
        Assets(),
        Data(),
        Screener(),
        Actions(),
    )
    pick = session_window.stream_mover(
        cast(TradingClient, trading),
        cast(Any, stream),
        cast(Any, data),
        cast(Any, screener),
        cast(Any, actions),
        date(2026, 11, 9),
        minutes=1,
        clock=lambda: AT.timestamp() + 30,
        sleep=stream.wait,
    )
    assert stream.subscribed == [("*",)] and stream.stopped.is_set()
    assert pick.symbol == "LLL" and pick.price == Decimal("1.5")
    record = pick.record
    assert record["source"] == "stream" and record["symbol"] == "LLL"
    assert record["sample"]["elapsed_minutes"] == 1
    assert record["sample"]["symbols"] == len(NAMES)
    assert record["sample"]["references_unread"] == 0 and not record["sample"]["reads_failed"]
    assert [r["symbol"] for r in record["ranked"]] == [
        "BBB",
        "AAA",
        "CCC",
        "DDD",
        "KKK",
        "LLL",
        "EEE",
        "FFF",
    ]
    assert list(record["passed_over"]) == ["BBB", "AAA", "CCC", "DDD", "KKK"]
    assert [g["symbol"] for g in record["screener"]] == list(NAMES)


def test_a_stream_that_stops_early_fails_the_sample() -> None:
    stream = SampleStream(fail="connection closed")
    with pytest.raises(session_window.StreamSampleFailed, match="connection closed"):
        session_window.sample_stream(
            cast(Any, stream), 1, clock=lambda: AT.timestamp() + 30, sleep=lambda _: time.sleep(0.3)
        )
    assert stream.stopped.is_set()


def test_a_sample_with_no_bar_fails() -> None:
    stream = SampleStream(names={})
    with pytest.raises(session_window.StreamSampleFailed, match="no bar"):
        session_window.sample_stream(
            cast(Any, stream),
            1,
            clock=lambda: AT.timestamp() + 30,
            sleep=stream.wait,
        )


def test_choose_mover_falls_back_to_the_screener_and_says_why(monkeypatch) -> None:
    def refused(*a, **k):
        raise session_window.WindowRefused("the stream sample ranked no name")

    monkeypatch.setattr(session_window, "window_stream", lambda: SampleStream())
    monkeypatch.setattr(session_window, "stream_mover", refused)
    monkeypatch.setattr(session_window, "window_screener", lambda: Screener())
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    pick = session_window.choose_mover(cast(TradingClient, Assets()), AT, opens, date(2026, 11, 9))
    assert pick.symbol == "LLL" and pick.record["source"] == "screener"
    assert "ranked no name" in pick.record["stream_failed"]


def test_the_record_keeps_each_fill_and_the_worst_submit_delay() -> None:
    """From each order's events: `OrderAccepted`'s `ts_init` less the first event's, and each
    `OrderFilled`'s quantity, price and venue time."""

    def event(name: str, ts_ms: int, **kw):
        return type(name, (), {"ts_init": ts_ms * 1_000_000, "ts_event": ts_ms * 1_000_000, **kw})()

    def order(cid: str, *events):
        return SimpleNamespace(
            client_order_id=SimpleNamespace(value=cid), events=lambda: list(events)
        )

    cache = SimpleNamespace(
        orders=lambda strategy_id: [
            order(
                "O-1",
                event("OrderInitialized", 1_000),
                event("OrderSubmitted", 1_002),
                event("OrderAccepted", 1_180),
                event("OrderFilled", 1_400, last_qty=1, last_px="1.25"),
            ),
            order("O-2", event("OrderInitialized", 5_000), event("OrderAccepted", 5_090)),
            order("O-3", event("OrderInitialized", 9_000), event("OrderDenied", 9_000, reason="x")),
        ]
    )
    report = session_window.WindowReport("regular", "LLL", started="s")
    session_window._fill(report, cache, StrategyId("SessionMaCross-001"))
    assert report.submit_delays_ms == {"O-1": 180, "O-2": 90}
    assert report.worst_submit_delay_ms == 180
    assert report.fill_prices == {"O-1": ["1@1.25 1970-01-01T00:00:01.400000+00:00"]}
    assert report.fills == 1


def test_a_sample_whose_stream_logs_a_reconnect_fails() -> None:
    """A reconnect drops minutes of bars: the sample fails (and `choose_mover` falls back) rather than
    rank a partial dollar volume, as `movers_stream.run` fails on what alpaca-py's stream logs."""
    import logging

    stream = SampleStream()
    original = stream.run

    def run() -> None:
        logging.getLogger("alpaca.data.live.websocket").warning("websocket error, restarting")
        original()

    stream.run = run
    with pytest.raises(session_window.StreamSampleFailed, match="restarting"):
        session_window.sample_stream(
            cast(Any, stream), 1, clock=lambda: AT.timestamp() + 30, sleep=stream.wait
        )


def test_a_failure_of_both_sources_refuses_the_window_naming_each(monkeypatch) -> None:
    def failed(*a, **k):
        raise APIError("500 from the screener")

    monkeypatch.setattr(session_window, "select_mover", failed)
    monkeypatch.setattr(session_window, "window_screener", lambda: Screener())
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    with pytest.raises(
        session_window.WindowRefused, match=r"no stream in tests.*500 from the screener"
    ):
        session_window.choose_mover(cast(TradingClient, Assets()), AT, opens, date(2026, 11, 9))


def test_one_share_must_fit_the_cap_at_the_price_of_the_order_sent() -> None:
    """Muse's finding 1 (2026-11-11): the risk engine prices a limit at its limit price, and a mover's
    entry is `SessionMaCross`'s limit through the touch (`through_the_touch`: the ask plus
    `LIMIT_OFFSET`), so the cap is checked at that price, not the last trade's: last 999 with an ask of
    1005 would send 1005.05, and every entry would be denied."""
    names = {"HHH": Name(500.0, 999.0, 10, 999.0, ask=1005.0), "LLL": NAMES["LLL"]}
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    pick = session_window.select_mover(
        cast(TradingClient, Assets()),
        cast(Any, Data(names)),
        cast(Any, Screener(names)),
        cast(Any, Actions()),
        AT,
        opens,
        date(2026, 11, 9),
    )
    assert pick.symbol == "LLL" and pick.entry == Decimal("1.55")
    assert "1005.05" in pick.record["passed_over"]["HHH"]
    assert pick.record["entry_limit"] == "1.55"


def test_the_fallback_ranks_on_completed_minutes_only() -> None:
    """Muse's finding 2 (2026-11-11): a bar for the minute still in progress is not the close the move
    is measured on, since `minute_dollars` leaves that minute out of the dollar volume."""
    names = {"LLL": Name(1.0, 1.5, 1200, 1.5, partial=3.0)}
    opens = datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    pick = session_window.select_mover(
        cast(TradingClient, Assets()),
        cast(Any, Data(names)),
        cast(Any, Screener(names)),
        cast(Any, Actions()),
        AT,
        opens,
        date(2026, 11, 9),
    )
    assert pick.record["inputs"]["close"] == pytest.approx(1.5)
    assert pick.record["inputs"]["move"] == pytest.approx(0.5)


def test_the_stream_paths_freshness_is_measured_on_alpacas_clock() -> None:
    """Muse's finding 3 (2026-11-11): the trade-age proxy is read against Alpaca's clock moved on by the
    process's monotonic time (`now`), not the host's clock, which may be off by up to `SKEW_SECS`: trades 35
    s old by the host are 135 s old by Alpaca's clock here, so every name is passed over as halted."""
    stream = SampleStream()
    venue = AT + timedelta(seconds=130)
    with pytest.raises(
        session_window.WindowRefused, match=r"LLL: its latest sip trade is 135 s old"
    ):
        session_window.stream_mover(
            cast(TradingClient, Assets()),
            cast(Any, stream),
            cast(Any, Data()),
            cast(Any, Screener()),
            cast(Any, Actions()),
            date(2026, 11, 9),
            minutes=1,
            clock=lambda: AT.timestamp() + 30,
            sleep=stream.wait,
            now=lambda: venue,
        )


def test_a_symbol_without_a_bar_in_every_minute_of_the_sample_is_not_ranked() -> None:
    """Muse's finding 4 (2026-11-11): the sample's minutes are counted per symbol, so a name that traded in
    its first minute only is not ranked on a dollar volume the others earned over the whole sample."""
    names = {"LLL": NAMES["LLL"], "EEE": NAMES["EEE"]}
    stream = SampleStream(names, later={"EEE": [1]})
    pick = session_window.stream_mover(
        cast(TradingClient, Assets()),
        cast(Any, stream),
        cast(Any, Data(names)),
        cast(Any, Screener(names)),
        cast(Any, Actions()),
        date(2026, 11, 9),
        minutes=2,
        clock=lambda: AT.timestamp() + 30,
        sleep=stream.wait,
    )
    assert pick.symbol == "EEE"
    assert [r["symbol"] for r in pick.record["ranked"]] == ["EEE"]
    # left out of the closes too, so none of the reads after the sample is spent on it
    assert pick.record["sample"]["symbols"] == 1
    assert pick.record["sample"]["elapsed_minutes"] == 2
    assert pick.record["sample"]["incomplete"] == 1


def test_a_first_minute_no_bar_was_received_for_does_not_leave_every_symbol_out() -> None:
    """Code review, 2026-11-11: the sample starts in the last second of a minute (or on a host clock behind
    Alpaca's), so that minute's bars went out before the subscription and no symbol has one. The sample's
    minutes are those from the first minute a bar was received for, not the host's minute at the start."""
    names = {"LLL": NAMES["LLL"], "EEE": NAMES["EEE"]}
    stream = SampleStream(names, later={"LLL": [1], "EEE": [1]})
    pick = session_window.stream_mover(
        cast(TradingClient, Assets()),
        cast(Any, stream),
        cast(Any, Data(names)),
        cast(Any, Screener(names)),
        cast(Any, Actions()),
        date(2026, 11, 9),
        minutes=2,
        clock=lambda: AT.timestamp() - 1,
        sleep=stream.wait,
    )
    assert sorted(r["symbol"] for r in pick.record["ranked"]) == ["EEE", "LLL"]
    assert pick.record["sample"]["elapsed_minutes"] == 2
    assert pick.record["sample"]["incomplete"] == 0
    assert pick.record["sample"]["from"] == AT.astimezone(UTC).isoformat()


# --- `Mover` in a window (ADR 0088) ------------------------------------------------------------------


def mover_pick(price: str = "1.5") -> session_window.MoverPick:
    return session_window.MoverPick(
        "LLL",
        Decimal(price),
        {"at": "2026-11-10T14:57:00+00:00", "inputs": {"move": 2.0000004}},
        Decimal(price),
    )


def test_mover_runs_only_on_the_name_the_ranking_gives() -> None:
    with pytest.raises(ValueError, match="run it with --mover"):
        WindowConfig(strategy="Mover")
    with pytest.raises(ValueError, match="regular"):
        WindowConfig(strategy="Mover", mover=True, session="extended")
    assert WindowConfig(strategy="Mover", mover=True).strategy == "Mover"


def test_a_mover_strategy_window_builds_the_strategy_its_ranking_and_the_outer_bound(
    monkeypatch, tmp_path
) -> None:
    """The node of a `Mover` window: the risk engine's per-order cap is the outer bound, ten times the
    largest entry, since the engine holds a sale to the cap as it does a buy; the strategy sizes its
    own entry inside 500 dollars, on bars built from trades; the window's choice is the ranking, with
    the time it was made; the position cap is the shares the largest entry buys at the choice's price;
    and its stop in the session is the managed market exit."""
    from canon_trading.mover import Mover

    seen: list[dict] = []
    real = session_window.alpaca_paper_builder

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_window, "alpaca_paper_builder", spy)
    ends = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)
    node, strategy, cap = session_window.window_node(
        WindowConfig(symbol="LLL", mover=True, strategy="Mover", log_dir=tmp_path),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="m",
        feed="sip",
        pick=mover_pick(),
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover)
        rows = strategy.settings
        assert seen[0]["max_notional_per_order"] == Decimal(5000)
        assert seen[0]["delay_post_stop_secs"] == session_window.MANAGED_DELAY_POST_STOP_SECS
        assert (rows.max_notional, rows.max_shares, rows.entries) == (Decimal(500), 333, 1)
        assert rows.bar_spec == "1-MINUTE-LAST-INTERNAL" and rows.feed == "sip"
        assert rows.manage_stop is True and rows.extended_hours is False
        assert rows.rank_max_age_secs == 33 * 60  # from the choice at 14:57 to the end at 15:30
        assert cap.max_position == Decimal(333)
        assert session_window.WINDOW_STRATEGIES["Mover"].strategy_id == strategy.strategy_id
    finally:
        node.dispose()


def test_a_mover_strategy_window_is_refused_without_its_choice(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="built on the window's choice"):
        session_window.window_node(
            WindowConfig(symbol="LLL", mover=True, strategy="Mover", log_dir=tmp_path),
            wind_down_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
            log_name="m",
            feed="sip",
        )


def test_a_mover_strategy_window_fails_by_name_over_its_request_ceiling_or_on_a_429() -> None:
    report = session_window.WindowReport("regular", "LLL", started="20261110T160000Z")
    quiet = {"account": 1, "api": "trading", "total": 40, "peak_minute": 30, "peak_60s": 31}
    report.requests_by_api = [{**quiet, "rate_limited": 0}]
    assert session_window.request_failure(report) is None
    report.requests_by_api = [{**quiet, "peak_60s": 180, "rate_limited": 0}]
    assert "180 requests" in (session_window.request_failure(report) or "")
    report.requests_by_api = [{**quiet, "rate_limited": 1}]
    assert "429" in (session_window.request_failure(report) or "")


class Quoted(Data):
    """`Data` that also answers alpaca-py's `get_stock_latest_quote`, with one `ask` for any symbol."""

    ask = 1.6

    def get_stock_latest_quote(self, request_params) -> dict:
        self.requests.append(request_params)
        symbol = request_params.symbol_or_symbols
        return {symbol: SimpleNamespace(ask_price=self.ask, bid_price=self.ask - 0.01)}


def test_a_mover_strategy_window_hands_the_node_its_choice_and_its_end(
    monkeypatch, tmp_path
) -> None:
    """The window's one choice reaches the node's build with the window's end, which bounds how long
    that choice stands as the ranking. One share is checked at the limit the strategy would send, the
    ask of the latest sip quote, since the risk engine values a limit order at its limit price, not at
    the last trade (a second reader's finding on the bridge window, Muse, 2026-11-11): the shares bound
    comes from that limit, and a name whose one share at it is over the largest entry starts nothing."""
    data, screener = Quoted(), Screener()
    monkeypatch.setattr(session_window, "window_data", lambda: data)
    monkeypatch.setattr(session_window, "window_screener", lambda: screener)
    outcome, latest, seen = quiet_window(
        monkeypatch, tmp_path, quotes=5, trades=5, account=Assets(), mover=True, strategy="Mover"
    )
    assert not isinstance(outcome, Exception), outcome
    assert latest["strategy"] == "Mover" and latest["symbol"] == "LLL" and latest["result"] == "ok"
    assert latest["share_price"] == "1.5" and latest["mover"]["entry_limit"] == "1.6000"
    assert seen["pick"].symbol == "LLL" and seen["pick"].price == Decimal("1.6")
    assert seen["ends"] - seen["wind_down_at"] == timedelta(seconds=session_window.FLATTEN_SECS)

    data.ask = 500.01  # the last trade is still 1.5: only the limit is over the largest entry
    monkeypatch.setattr(session_window, "GAP_SECS", 0)
    with pytest.raises(session_window.WindowRefused, match="at its entry limit of 500.0100"):
        session_window.run_window(
            WindowConfig(log_dir=tmp_path, strategy="Mover", mover=True),
            trading=cast(TradingClient, Assets()),
        )
