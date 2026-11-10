"""One bounded window of `SessionMaCross` on Alpaca's paper environment: start flat, trade, flatten, check, report.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveExecutionEngineConfig` and `LoggerConfig` with a
`FileWriterConfig` (`live/__init__.pyi`, `common/__init__.pyi`); alpaca-py 0.45.0's `TradingClient` (paper)
`get_clock`, `get_calendar`, `get_orders`, `get_open_position`. Read 2026-11-10 (ADR 0044):

* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md, "Continuous reconciliation":
  `open_check_interval_secs` ("None or 0.0 disables. Recommended: 5-10s") and `position_check_interval_secs`
  ("On discrepancy, queries for missing fills. None disables. Recommended: 30-60s") are off by default.
* https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md, "Runtime checks": the open
  check reconciles cached orders with the venue's; the position check matches the net position with the
  venue's position reports and aligns it.
* https://nautilustrader.io/docs/md/latest/concepts/logging.md: `FileWriterConfig.directory` and `file_name`.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading": pre-market 4:00am to
  9:30am and after-hours 4:00pm to 8:00pm ET, Monday to Friday; overnight 8:00pm to 4:00am.

What one window is (one node, one process): the configured session and feed, checked against Alpaca's
clock and calendar before anything starts; the paper account holding no open order and no position in the
symbol; at least `GAP_SECS` since the last window ended; then the node runs for `minutes` (default 30, at
most 60), clipped to the session's end. The strategy winds down `FLATTEN_SECS` before the end, the node
stops, and the account is checked: no open order, no position. In the regular session a position still
held is closed at market through alpaca-py; outside it the window fails and names it, since a market close
would only queue for the next session. The run's node log and a JSON report go to `log_dir`, outside the
repository. Overnight is refused: what the feeds deliver then is not yet observed.

Paper only: the execution client is `canon_alpaca`'s, whose config refuses anything but paper trading, and
the alpaca-py client here is built with `paper=True`. The key pair comes from the environment, which
`mise run paper:session` fills as `paper:test` does (ADR 0044, superseding ADR 0029's "only" clause). This evaluates nothing (ADR 0010).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Calendar as AlpacaCalendar
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import FEED
from canon_alpaca.constants import VENUE
from nautilus_trader.common import LogLevel
from nautilus_trader.config import FileWriterConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .paper_node import alpaca_paper_builder
from .position_cap import PositionCap
from .position_cap import position_cap_config
from .session_ma_cross import SessionMaCross
from .session_ma_cross import SessionMaCrossConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

NEW_YORK = ZoneInfo("America/New_York")
SESSIONS = ("regular", "extended")
DEFAULT_MINUTES = 30
MAX_MINUTES = 60
GAP_SECS = 300
"""Between one window's end and the next one's start, so paper tests waiting for the lock get it."""
FLATTEN_SECS = 120
"""The strategy winds down this long before the window ends."""
END_MARGIN_SECS = 60
"""A window ends at least this long before its session does."""
STRATEGY_ID = StrategyId("SessionMaCross-001")
EXTENDED_READY = False
"""Whether extended windows may run. The adapter sends `extended_hours` from the submit params (ADR 0038);
a reviewed change turns this on once an extended window has passed on paper and the adapter's
disconnect-hang fix is on `main` (ADR 0044)."""
POLL_SECS = 2.0
"""Between account reads after the stop: two calls each, well inside Alpaca's 200 calls a minute."""


def default_log_dir() -> Path:
    """The per-user temporary folder `mise run paper:session` passes, whatever `$TMPDIR` a session sets,
    so the gap marker is found by every caller; elsewhere than macOS, the system's temporary folder."""
    try:
        found = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        found = tempfile.gettempdir()
    return Path(found) / "canon-paper-session"


@dataclass(frozen=True)
class WindowConfig:
    """What one window runs. `session` is `regular` (market orders) or `extended` (marked limits)."""

    symbol: str = "SPY"
    session: str = "regular"
    feed: str = FEED.value
    minutes: int = DEFAULT_MINUTES
    fast_period: int = 10
    slow_period: int = 20
    log_dir: Path = field(default_factory=default_log_dir)

    def __post_init__(self) -> None:
        if self.session not in SESSIONS:
            raise ValueError(f"session must be one of {SESSIONS}, got {self.session!r}")
        if self.symbol != "SPY":
            raise ValueError(
                f"the notional cap is sized for SPY (paper_node.MAX_NOTIONAL_PER_ORDER_USD); got {self.symbol}"
            )
        if self.feed != FEED.value:
            raise ValueError(f"the adapter streams the {FEED.value} feed only, got {self.feed!r}")
        if not 1 <= self.minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be 1 to {MAX_MINUTES}, got {self.minutes}")
        if self.minutes * 60 <= FLATTEN_SECS:
            raise ValueError(f"a window must be longer than its {FLATTEN_SECS} s wind-down")
        if self.session == "extended" and not EXTENDED_READY:
            raise ValueError(
                "an extended window waits until one has passed on paper and the adapter's "
                "disconnect-hang fix is on main (EXTENDED_READY, ADR 0044)"
            )

    @property
    def instrument_id(self) -> InstrumentId:
        return InstrumentId.from_str(f"{self.symbol}.{VENUE}")

    @property
    def bar_type(self) -> BarType:
        """One-minute bars the data engine builds from the trade stream (the adapter streams no bars)."""
        return BarType.from_str(f"{self.instrument_id}-1-MINUTE-LAST-INTERNAL")


def in_new_york(moment: datetime) -> datetime:
    """`moment` in New York time. alpaca-py builds `Calendar.open` and `close` naive, from the response's
    date and HH:MM (`trading/models.py`), which Alpaca gives in New York time; an aware value is converted."""
    return moment.replace(tzinfo=NEW_YORK) if moment.tzinfo is None else moment.astimezone(NEW_YORK)


def session_end(
    config: WindowConfig, clock: AlpacaClock, calendar: AlpacaCalendar | None
) -> datetime:
    """When the configured session that is open now ends, or `RuntimeError` if it is not open now."""
    now = clock.timestamp.astimezone(NEW_YORK)
    if config.session == "regular":
        if not clock.is_open:
            raise RuntimeError(
                "the regular session is closed; Alpaca's clock says it next opens "
                f"{clock.next_open.astimezone(NEW_YORK):%Y-%m-%d %H:%M} New York"
            )
        return clock.next_close.astimezone(NEW_YORK)
    if clock.is_open:
        raise RuntimeError("the regular session is open; an extended window runs outside it")
    if calendar is None or calendar.date != now.date() or not calendar.session_open:
        raise RuntimeError(f"{now:%Y-%m-%d} is not a trading day on Alpaca's calendar")
    opens = datetime.strptime(calendar.session_open, "%H%M").time()
    closes = datetime.strptime(calendar.session_close or "2000", "%H%M").time()
    regular_open, regular_close = in_new_york(calendar.open), in_new_york(calendar.close)
    pre = (datetime.combine(now.date(), opens, NEW_YORK), regular_open)
    after = (regular_close, datetime.combine(now.date(), closes, NEW_YORK))
    for start, end in (pre, after):
        if start <= now < end:
            return end
    raise RuntimeError(
        f"{now:%H:%M} New York is outside pre-market and after-hours; overnight is not run"
    )


def window_node(
    config: WindowConfig, *, wind_down_at: datetime, log_name: str
) -> tuple[LiveNode, SessionMaCross, PositionCap]:
    """The node, Alpaca's clients, the native risk limits and runtime checks, with the strategy and the cap
    it runs (returned too: the node does not hand its Python strategies back)."""
    extended = config.session == "extended"
    node = (
        alpaca_paper_builder("CANON-PAPER-SESSION", config.instrument_id)
        .with_logging(
            LoggerConfig(
                fileout_level=LogLevel.INFO,  # file output is off unless a level is given
                file_config=FileWriterConfig(directory=str(config.log_dir), file_name=log_name),
            )
        )
        .build()
    )
    strategy = SessionMaCross(
        SessionMaCrossConfig(
            bar_type=config.bar_type,
            trade_size=Quantity.from_int(1),
            fast_period=config.fast_period,
            slow_period=config.slow_period,
            extended_hours=extended,
            wind_down_at=wind_down_at,
            strategy_id=STRATEGY_ID,
            order_id_tag="001",
            # In the session, `stop()` runs upstream's market exit, with closing orders Alpaca takes.
            manage_stop=not extended,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
            market_exit_interval_ms=1000,
            market_exit_max_attempts=60,
        )
    )
    cap = PositionCap(position_cap_config(config.instrument_id))
    node.add_strategy(strategy)
    node.add_strategy(cap)
    return node, strategy, cap


@dataclass
class WindowReport:
    """What a window did, written as JSON beside its log."""

    session: str
    symbol: str
    started: str
    ended: str = ""
    seconds: float = 0.0
    orders: dict[str, list[str]] = field(default_factory=dict)
    fills: int = 0
    denials: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    cap_checks: int = 0
    cap_peak: str = "0"
    cap_tripped: str | None = None
    left: list[str] = field(default_factory=list)
    closed_after_stop: bool = False
    errors: list[str] = field(default_factory=list)


def paper_trading() -> TradingClient:
    return TradingClient(
        api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
    )


def open_orders(trading: TradingClient, symbol: str) -> list:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
    assert isinstance(orders, list)
    return orders


def position_qty(trading: TradingClient, symbol: str) -> Decimal:
    """The signed quantity Alpaca holds in `symbol`, 0 when none (404)."""
    try:
        position = trading.get_open_position(symbol)
    except APIError as e:
        if e.status_code == 404:
            return Decimal(0)
        raise
    assert isinstance(position, AlpacaPosition)
    qty = abs(Decimal(str(position.qty)))
    return -qty if getattr(position.side, "value", position.side) == "short" else qty


def leftovers(trading: TradingClient, symbol: str) -> list[str]:
    left = [f"open order {o.client_order_id}" for o in open_orders(trading, symbol)]
    if qty := position_qty(trading, symbol):
        left.append(f"position of {qty} {symbol}")
    return left


def run_window(config: WindowConfig) -> WindowReport:
    """Run one window as the module header describes; raise if it cannot start or is not left flat."""
    config.log_dir.mkdir(parents=True, exist_ok=True)
    marker = config.log_dir / "last-window-end"
    # Dated by when it was written, so a marker cut short by a kill still counts.
    if marker.exists() and time.time() - marker.stat().st_mtime < GAP_SECS:
        raise RuntimeError(f"the last window ended under {GAP_SECS} s ago; waiting is the caller's")
    trading = paper_trading()
    clock = trading.get_clock()
    assert isinstance(clock, AlpacaClock)
    today = clock.timestamp.astimezone(NEW_YORK).date()
    days = trading.get_calendar(GetCalendarRequest(start=today, end=today))
    calendar = days[0] if isinstance(days, list) and days else None
    end_of_session = session_end(config, clock, calendar)
    if left := leftovers(trading, config.symbol):
        raise RuntimeError(
            f"the paper account is not flat in {config.symbol}; not starting: {left}"
        )

    now = clock.timestamp.astimezone(NEW_YORK)
    seconds = min(config.minutes * 60, (end_of_session - now).total_seconds() - END_MARGIN_SECS)
    if seconds <= FLATTEN_SECS + 1:
        raise RuntimeError(f"under {FLATTEN_SECS + END_MARGIN_SECS} s left in the session")
    # Absolute times, taken from Alpaca's clock: start-up eats into the window, not into the wind-down.
    ends = clock.timestamp.astimezone(UTC) + timedelta(seconds=seconds)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = WindowReport(config.session, config.symbol, started=stamp)
    started = time.monotonic()
    try:
        node, strategy, cap = window_node(
            config, wind_down_at=ends - timedelta(seconds=FLATTEN_SECS), log_name=f"window-{stamp}"
        )
        cache = node.cache
        try:
            asyncio.run(_run_until(node, ends))
        finally:
            try:
                report.seconds = round(time.monotonic() - started, 1)
                _fill(report, cache)
                report.dropped = list(strategy.dropped)
                report.cap_checks, report.cap_peak = cap.checks, str(cap.peak)
                report.cap_tripped = cap.tripped
            finally:
                node.dispose()
    finally:
        try:
            report.left = _flatten_after_stop(trading, config, report)
        finally:
            report.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            marker.write_text(str(time.time()))
            (config.log_dir / f"window-{stamp}.json").write_text(
                json.dumps(asdict(report), indent=2)
            )
    if report.left:
        raise RuntimeError(f"the paper account was not left flat in {config.symbol}: {report.left}")
    if report.cap_tripped:
        raise RuntimeError(f"the position cap stopped the window: {report.cap_tripped}")
    return report


def _flatten_after_stop(trading: TradingClient, config: WindowConfig, report: WindowReport) -> list:
    """What the account holds in the symbol after the stop. Every open order in it is canceled, in any
    session, and the cancels are waited for, since a held order keeps its shares from a close (Alpaca
    answers 403). In the regular session a position is then closed at market; outside it a market close
    would only queue, so it is reported. A failed step is recorded and the next still runs."""
    left = leftovers(trading, config.symbol)
    if not left:
        return left
    for order in open_orders(trading, config.symbol):
        try:
            trading.cancel_order_by_id(order.id)
        except APIError as e:  # it filled or ended meanwhile
            report.errors.append(f"canceling {order.client_order_id}: {e}")
    _settle(lambda: not open_orders(trading, config.symbol))
    if config.session == "regular" and position_qty(trading, config.symbol):
        try:
            trading.close_position(config.symbol)
            report.closed_after_stop = True
        except APIError as e:
            report.errors.append(f"closing the position: {e}")
        _settle(
            lambda: (
                not position_qty(trading, config.symbol) and not open_orders(trading, config.symbol)
            ),
        )
    return leftovers(trading, config.symbol)


def _settle(done, seconds: float = 30) -> None:
    """Poll `done` every `POLL_SECS` until it is true or `seconds` have gone."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not done():
        time.sleep(POLL_SECS)


async def _run_until(node: LiveNode, ends: datetime) -> None:
    task = asyncio.ensure_future(node.run_async())
    try:
        await asyncio.wait({task}, timeout=max(0.0, (ends - datetime.now(UTC)).total_seconds()))
    finally:
        if not task.done():
            node.handle().stop()
        await task


def _fill(report: WindowReport, cache) -> None:
    for order in cache.orders(strategy_id=STRATEGY_ID):
        names = [type(e).__name__ for e in order.events()]
        report.orders[order.client_order_id.value] = names
        report.fills += names.count("OrderFilled")
        for event in order.events():
            if type(event).__name__ in {"OrderDenied", "OrderRejected"}:
                report.denials[order.client_order_id.value] = str(event.reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One SessionMaCross window on Alpaca paper")
    parser.add_argument("--session", choices=SESSIONS, default="regular")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    args = parser.parse_args(argv)
    try:
        config = WindowConfig(
            symbol=args.symbol, session=args.session, minutes=args.minutes, log_dir=args.log_dir
        )
        report = run_window(config)
    except (RuntimeError, ValueError, KeyError, APIError) as e:  # KeyError: no key pair in the env
        print(f"paper:session: {e}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
