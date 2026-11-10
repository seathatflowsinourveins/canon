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
  9:30am and after-hours 4:00pm to 8:00pm ET, Monday to Friday; overnight 8:00pm to 4:00am; "Orders not
  eligible for extended hours submitted after 4:00pm ET will be queued up for release the next trading day".
* `crates/live/src/node/mod.rs` and `python/node.rs` at the pin (`d53849b216e2`): the node bounds each
  stage of its own stop (`delay_post_stop`, `timeout_disconnection`, `timeout_shutdown`), logs the one that
  ran out and goes on; cancelling the task awaiting `run_async` "requests the same graceful shutdown, waits
  for it to finish", so neither a cancel nor `asyncio.run`'s exit can end a stop that hangs.

What one window is (one node, one process): the configured session and feed, checked against Alpaca's
clock and calendar before anything starts; the paper account holding no open order and no position in the
symbol; at least `GAP_SECS` since the last window ended; then the node runs for `minutes` (default 30, at
most 60), clipped to the session's end. The strategy winds down `FLATTEN_SECS` before the end, the node
stops, and the account is checked: no open order, no position. The wait for the stop is bounded by
`STOP_TIMEOUT_SECS`, the node's own stop bounds summed; past it the window is recorded failed, the
account is still checked, and `main` ends the process (`os._exit`), since the node's task can be neither
awaited nor cancelled. A position still held once the cancels have landed is closed at market through
alpaca-py while Alpaca's clock, read when the close is sent, says the regular session is open, and tried
again each poll while Alpaca refuses it, as upstream's market exit closes again once nothing works, for
at most `AFTER_STOP_SECS`. Otherwise the window fails and names it, and an order still open with the
session closed (a close that would only queue for the next session) is canceled. The run's node log and a
JSON report go to `log_dir`, outside the repository. Overnight is refused: what the feeds deliver then is
not yet observed.

Paper only: the execution client is `canon_alpaca`'s, whose config refuses anything but paper trading, and
the alpaca-py client here is built with `paper=True`. The key pair comes from the environment, which
`mise run paper:session` fills as `paper:test` does (ADR 0044, superseding ADR 0029's "only" clause). This evaluates nothing (ADR 0010).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
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
from canon_alpaca.providers import CONNECT_TIMEOUT_SECS
from canon_alpaca.providers import READ_TIMEOUT_SECS
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import LogLevel
from nautilus_trader.config import FileWriterConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .paper_node import DELAY_POST_STOP_SECS
from .paper_node import TIMEOUT_DISCONNECTION_SECS
from .paper_node import TIMEOUT_SHUTDOWN_SECS
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
END_MARGIN_SECS = 240
"""A window ends at least this long before its session does: a stop at its bound (`STOP_TIMEOUT_SECS`) and
the account check after it (`AFTER_STOP_SECS`) still find the session open, when a market close executes,
with a minute for the calls before the check's deadline and skew between Alpaca's clock and this host's."""
STRATEGY_ID = StrategyId("SessionMaCross-001")
STALE_RUNNING_SECS = 70 * 60
"""A record still `running` this long after it started is a window that died (60 minutes at most, plus
start-up, shutdown and the account check)."""
EXTENDED_READY = False
"""Whether extended windows may run. The adapter sends `extended_hours` from the submit params (ADR 0038);
a reviewed change turns this on once an extended window has passed on paper and the adapter's
disconnect-hang fix is on `main` (ADR 0044)."""
POLL_SECS = 2.0
"""Between account reads after the stop: at most three calls each, well inside Alpaca's 200 a minute."""
COMMIT_ENV = "CANON_COMMIT"
"""Set by the scheduled runner, `home/.claude/canon/paper-session.sh`, to the `origin/main` commit it checked
out for this firing (ADR 0047)."""
SOURCE_ROOT = Path(__file__).resolve().parents[2]
"""The checkout this module runs from."""
MARKET_EXIT_INTERVAL_MS = 1000
MARKET_EXIT_MAX_ATTEMPTS = 60
STOP_TIMEOUT_SECS = (
    MARKET_EXIT_INTERVAL_MS * MARKET_EXIT_MAX_ATTEMPTS / 1000
    + DELAY_POST_STOP_SECS
    + TIMEOUT_DISCONNECTION_SECS
    + TIMEOUT_SHUTDOWN_SECS
    + CONNECT_TIMEOUT_SECS
    + READ_TIMEOUT_SECS
)
"""How long the window waits for the node to stop (about 115 s): the strategy's market exit at its longest,
the node's own stop bounds (`paper_node`), and one Alpaca request at the adapter's timeouts (ADR 0039),
which a call in flight at the disconnect can outlast it by."""
AFTER_STOP_SECS = 60
"""How long the account check after the stop works at clearing the account before it reports."""


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


def running_commit(root: Path = SOURCE_ROOT) -> str:
    """The commit this run's code is at, for its records: `CANON_COMMIT` when the scheduled runner set it,
    else `git rev-parse HEAD` of `root`, marked when a tracked file differs from it, else `unknown: <why>`.
    Never empty, so a record always says which code ran or that it could not tell. Read from git once a
    process, so every record of one run names the same code."""
    if found := os.environ.get(COMMIT_ENV, "").strip():
        return found
    return _checkout_commit(root)


@functools.cache
def _checkout_commit(root: Path) -> str:
    try:
        head, changed = (
            subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            for args in (["rev-parse", "--verify", "HEAD"], ["status", "--porcelain", "-uno"])
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown: {type(e).__name__}: {e}"
    return f"{head} with uncommitted changes" if changed else head


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
            warm_up=True,  # the averages start from the adapter's last bars (ADR 0047)
            strategy_id=STRATEGY_ID,
            order_id_tag="001",
            # In the session, `stop()` runs upstream's market exit, with closing orders Alpaca takes.
            manage_stop=not extended,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
            market_exit_interval_ms=MARKET_EXIT_INTERVAL_MS,
            market_exit_max_attempts=MARKET_EXIT_MAX_ATTEMPTS,
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
    warm_up_bars: int = 0
    warm_up_dropped: int = 0
    result: str = "running"
    """`running` while the window runs, then `ok` or `failed: <why>`. A record still `running` after
    `stale_after` seconds belongs to a window that died; `scripts/automation status` reads both."""
    started_epoch: float = field(default_factory=time.time)
    stale_after: int = STALE_RUNNING_SECS
    commit: str = field(default_factory=running_commit)
    """The code this window runs (`running_commit`), in the `running` record and the result alike."""


class WindowRefused(RuntimeError):
    """The window did not start, for a reason someone must look at (the account not flat, the session not
    open when one was named). Nothing was placed. Recorded as the latest result; the run exits 1."""


class StopTimedOut(RuntimeError):
    """The node did not stop within `STOP_TIMEOUT_SECS`. Recorded as failed after the account check;
    `main` then ends the process, since the node's task still runs and nothing can end it (module header)."""


_ABANDONED: list = []
"""A runner and the node's task left running after a stop timed out, held so neither is collected (the
hosted run would then drive its shutdown inline, `python/node.rs`) before `main` ends the process."""


class WindowSkipped(RuntimeError):
    """The window did not start, for a reason that clears by itself (inside the gap after the last window,
    too little of the session left). Nothing was placed. Not recorded as a result; the run exits 0."""


def _write_atomically(path: Path, body: str) -> None:
    """Replace `path` whole (`os.replace`), so a reader or a kill never sees half a record."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(body)
    os.replace(temporary, path)


def write_record(log_dir: Path, report: WindowReport | dict) -> None:
    """Write `latest.json`, the one record `scripts/automation status` reads, and, for a window that ran,
    its own `window-<stamp>.json`."""
    body = json.dumps(asdict(report) if isinstance(report, WindowReport) else report, indent=2)
    if isinstance(report, WindowReport):
        _write_atomically(log_dir / f"window-{report.started}.json", body)
    _write_atomically(log_dir / "latest.json", body)


def write_heartbeat(log_dir: Path, outcome: str) -> None:
    """Write `last-firing.json` on every run, a window or not: status reads its age to tell a schedule
    that has stopped firing from one with nothing to run."""
    log_dir.mkdir(parents=True, exist_ok=True)
    _write_atomically(
        log_dir / "last-firing.json",
        json.dumps({"at_epoch": time.time(), "outcome": outcome, "commit": running_commit()}),
    )


def session_now(trading: TradingClient) -> tuple[AlpacaClock, AlpacaCalendar | None]:
    """Alpaca's clock and today's calendar day (New York), read once per run."""
    clock = trading.get_clock()
    assert isinstance(clock, AlpacaClock)
    today = clock.timestamp.astimezone(NEW_YORK).date()
    days = trading.get_calendar(GetCalendarRequest(start=today, end=today))
    return clock, days[0] if isinstance(days, list) and days else None


def paper_trading() -> TradingClient:
    """alpaca-py's paper client with the adapter's HTTP timeout mounted (ADR 0039): alpaca-py sends no
    timeout, and the account check after an abandoned stop must not wait on a request for ever."""
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
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


def run_window(
    config: WindowConfig,
    *,
    trading: TradingClient | None = None,
    market: tuple[AlpacaClock, AlpacaCalendar | None] | None = None,
) -> WindowReport:
    """Run one window as the module header describes; raise if it cannot start or is not left flat.
    `trading` and `market` (the clock and calendar) are read here unless the caller already has them."""
    config.log_dir.mkdir(parents=True, exist_ok=True)
    marker = config.log_dir / "last-window-end"
    # Dated by when it was written, so a marker cut short by a kill still counts.
    if marker.exists() and time.time() - marker.stat().st_mtime < GAP_SECS:
        raise WindowSkipped(f"the last window ended under {GAP_SECS} s ago")
    trading = trading or paper_trading()
    clock, calendar = market or session_now(trading)
    try:
        end_of_session = session_end(config, clock, calendar)
    except RuntimeError as e:
        raise WindowRefused(str(e)) from e
    if left := leftovers(trading, config.symbol):
        raise WindowRefused(
            f"the paper account is not flat in {config.symbol}; not starting: {left}"
        )

    now = clock.timestamp.astimezone(NEW_YORK)
    seconds = min(config.minutes * 60, (end_of_session - now).total_seconds() - END_MARGIN_SECS)
    if seconds <= FLATTEN_SECS + 1:
        raise WindowSkipped(f"under {FLATTEN_SECS + END_MARGIN_SECS} s left in the session")
    # Absolute times, taken from Alpaca's clock: start-up eats into the window, not into the wind-down.
    ends = clock.timestamp.astimezone(UTC) + timedelta(seconds=seconds)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = WindowReport(config.session, config.symbol, started=stamp)
    write_record(
        config.log_dir, report
    )  # `running`: a window killed from here on leaves this behind
    started = time.monotonic()
    abandoned_before = len(_ABANDONED)
    failure: BaseException | None = None
    ran = False  # whether the node was run: only then is the gap marker written
    try:
        node, strategy, cap = window_node(
            config, wind_down_at=ends - timedelta(seconds=FLATTEN_SECS), log_name=f"window-{stamp}"
        )
        cache = node.cache
        try:
            ran = True
            _run_node(node, ends)
        finally:
            try:
                report.seconds = round(time.monotonic() - started, 1)
                _fill(report, cache)
                report.dropped = list(strategy.dropped)
                report.warm_up_bars = strategy.history_bars
                report.warm_up_dropped = strategy.history_dropped
                report.cap_checks, report.cap_peak = cap.checks, str(cap.peak)
                report.cap_tripped = cap.tripped
            finally:
                if len(_ABANDONED) == abandoned_before:  # else its abandoned task still owns it
                    node.dispose()
    except BaseException as e:
        failure = e
        raise
    finally:
        try:
            report.left = _flatten_after_stop(trading, config, report)
        except BaseException as e:  # the account could not be checked: never recorded as ok
            failure = failure or e
            report.errors.append(f"checking the account after the stop: {type(e).__name__}: {e}")
            raise
        finally:
            report.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            if ran:
                marker.write_text(str(time.time()))
            if failure is not None:
                report.result = f"failed: {type(failure).__name__}: {failure}"
            elif report.left:
                report.result = f"failed: not left flat: {report.left}"
            elif report.cap_tripped:
                report.result = f"failed: {report.cap_tripped}"
            else:
                report.result = "ok"
            write_record(config.log_dir, report)
    if report.left:
        raise RuntimeError(f"the paper account was not left flat in {config.symbol}: {report.left}")
    if report.cap_tripped:
        raise RuntimeError(f"the position cap stopped the window: {report.cap_tripped}")
    return report


def _flatten_after_stop(trading: TradingClient, config: WindowConfig, report: WindowReport) -> list:
    """What the account holds in the symbol after the stop. Every open order in it is canceled, in any
    session. Then, each `POLL_SECS` for at most `AFTER_STOP_SECS`: once no order is open (a held order
    keeps its shares from a close, which Alpaca refuses with a 403), a position still held is closed at
    market if Alpaca's clock says the regular session is open at that moment, and closed again on the next
    poll if Alpaca refused it; with the session closed it is left and reported. Last, an order still open
    with the session closed (a close that would only queue for the next session) is canceled. A failed
    step is recorded and the next still runs."""
    symbol = config.symbol
    left = leftovers(trading, symbol)
    if not left:
        return left
    _cancel(trading, open_orders(trading, symbol), report)
    deadline = time.monotonic() + AFTER_STOP_SECS
    while True:
        working = open_orders(trading, symbol)
        held = position_qty(trading, symbol)
        if not working and not held:
            break
        if not working:
            if not _regular_session_open(trading):
                report.errors.append(
                    "not closing the position: Alpaca's clock says the regular session is closed, "
                    "so a market close would only queue"
                )
                break
            try:
                trading.close_position(symbol)
                report.closed_after_stop = True
            except APIError as e:
                if (error := f"closing the position: {e}") not in report.errors:
                    report.errors.append(error)
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECS)
    if (working := open_orders(trading, symbol)) and not _regular_session_open(trading):
        _cancel(trading, working, report)
        _settle(lambda: not open_orders(trading, symbol))
    return leftovers(trading, symbol)


def _regular_session_open(trading: TradingClient) -> bool:
    clock = trading.get_clock()
    assert isinstance(clock, AlpacaClock)
    return bool(clock.is_open)


def _cancel(trading: TradingClient, orders: list, report: WindowReport) -> None:
    for order in orders:
        try:
            trading.cancel_order_by_id(order.id)
        except APIError as e:  # it filled or ended meanwhile
            report.errors.append(f"canceling {order.client_order_id}: {e}")


def _settle(done, seconds: float = 30) -> None:
    """Poll `done` every `POLL_SECS` until it is true or `seconds` have gone."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not done():
        time.sleep(POLL_SECS)


def _run_node(node: LiveNode, ends: datetime) -> None:
    """`asyncio.run(_run_until(node, ends))`, except after a stop past `STOP_TIMEOUT_SECS`: closing the
    loop then would cancel the node's task and wait for the same stop, so the loop and the task are left
    (`_ABANDONED`) and `StopTimedOut` raised."""
    runner = asyncio.Runner()
    started: list[asyncio.Task] = []
    try:
        stuck = runner.run(_run_until(node, ends, started))
    except BaseException:
        if started and not started[0].done():  # interrupted (Ctrl-C) while the stop still runs
            _ABANDONED.append((runner, started[0]))
        else:
            runner.close()
        raise
    if stuck is None:
        runner.close()
        return
    _ABANDONED.append((runner, stuck))
    raise StopTimedOut(
        f"the node did not stop within {STOP_TIMEOUT_SECS:g} s of the window's end; left running"
    )


async def _run_until(node: LiveNode, ends: datetime, started: list) -> asyncio.Task | None:
    """Run the node until `ends`, stop it, and wait at most `STOP_TIMEOUT_SECS` for the stop, as the node
    bounds each stage of its own; return the node's task if it is still running then, else None. The
    task is also put in `started`, for the caller to see after an interrupt."""
    task = asyncio.ensure_future(node.run_async())
    started.append(task)
    try:
        await asyncio.wait({task}, timeout=max(0.0, (ends - datetime.now(UTC)).total_seconds()))
    finally:
        if not task.done():
            node.handle().stop()
        await asyncio.wait({task}, timeout=STOP_TIMEOUT_SECS)
    if not task.done():
        return task
    task.result()  # the node's own error, if it ended with one
    return None


def _fill(report: WindowReport, cache) -> None:
    for order in cache.orders(strategy_id=STRATEGY_ID):
        names = [type(e).__name__ for e in order.events()]
        report.orders[order.client_order_id.value] = names
        report.fills += names.count("OrderFilled")
        for event in order.events():
            if type(event).__name__ in {"OrderDenied", "OrderRejected"}:
                report.denials[order.client_order_id.value] = str(event.reason)


def resolve_session(clock: AlpacaClock, calendar: AlpacaCalendar | None) -> tuple[str | None, str]:
    """The session `--session auto` runs, from Alpaca's clock and calendar, or None and why not: the
    regular session when the clock says it is open; else the extended one, if `EXTENDED_READY` and it is
    open now; else nothing. The schedule fires on a timer and leaves this choice to Alpaca's clock."""
    if clock.is_open:
        return "regular", "Alpaca's clock says the regular session is open"
    if not EXTENDED_READY:
        return (
            None,
            "the regular session is closed and extended windows are not on (EXTENDED_READY)",
        )
    try:
        session_end(WindowConfig(session="extended"), clock, calendar)
    except RuntimeError as e:
        return None, str(e)
    return "extended", "outside the regular session, inside pre-market or after-hours"


def main(argv: Sequence[str] | None = None) -> int:
    """`_main`, and after a stop that timed out or was interrupted (`_ABANDONED`), whatever error then
    reached here, the process ended with `os._exit` once the record is written: the node's task would
    otherwise hold the interpreter's exit (module header)."""
    code = 1
    try:
        code = _main(argv)
    except BaseException as e:
        if not _ABANDONED:
            raise
        traceback.print_exc()  # `os._exit` below would discard it
        try:
            write_heartbeat(_log_dir(argv), f"failed: {type(e).__name__}: {e}")
        except OSError:
            pass
    finally:
        if _ABANDONED:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code or 1)
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One SessionMaCross window on Alpaca paper")
    parser.add_argument("--session", choices=(*SESSIONS, "auto"), default="regular")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    return parser


def _log_dir(argv: Sequence[str] | None) -> Path:
    return _parser().parse_args(argv).log_dir


def _main(argv: Sequence[str] | None) -> int:
    args = _parser().parse_args(argv)
    session = args.session
    try:
        trading = market = None
        if session == "auto":
            trading = paper_trading()
            market = session_now(trading)
            resolved, why = resolve_session(*market)
            if resolved is None:
                write_heartbeat(args.log_dir, f"nothing to run: {why}")
                print(f"paper:session: nothing to run: {why}")
                return 0
            session = resolved
        config = WindowConfig(
            symbol=args.symbol, session=session, minutes=args.minutes, log_dir=args.log_dir
        )
        write_heartbeat(args.log_dir, f"a {session} window")
        report = run_window(config, trading=trading, market=market)
    except WindowSkipped as e:
        write_heartbeat(args.log_dir, f"skipped: {e}")
        print(f"paper:session: skipped: {e}")
        return 0
    except WindowRefused as e:
        write_heartbeat(args.log_dir, f"refused: {e}")
        write_record(
            args.log_dir,
            {
                "session": session,
                "result": f"refused: {e}",
                "started_epoch": time.time(),
                "started": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
                "stale_after": STALE_RUNNING_SECS,
                "commit": running_commit(),
            },
        )
        print(f"paper:session: {e}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, KeyError, APIError) as e:  # KeyError: no key pair in the env
        write_heartbeat(args.log_dir, f"failed: {type(e).__name__}: {e}")
        print(f"paper:session: {e}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
