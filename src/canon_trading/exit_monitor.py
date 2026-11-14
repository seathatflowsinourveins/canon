"""Display-only exit monitor, including the lead's frozen third-line brief.

Third-line contract: packages/canon-alpaca/README.md, "Wash trades", OBSERVED
2026-11-13 post-market. Blocking buys are separate from coverage and use only
the existing open-order/position reads (alpaca-py models.py:150-199,217-304).
The observed table defines the two blocking kinds. The price cut for a limit buy
(SALE_REACH) is this monitor's rule, not an observation, and each line says which is which;
a holding covered only by a node's own stop is still told, since that stop is no order at the venue.
Python b703b2c5e704 Doc/library/hashlib.rst:98-116: SHA-256 episode identities
keep venue/client order IDs out of every output, including local memory.

Contracts read at the pins on 2026-11-13:
* alpaca-py c42add7c0a70: trading/client.py:107-131,235-247,422-463;
  trading/requests.py:205-226; trading/models.py:150-199,217-303,427-466.
  Positions, flat open orders, clock and calendar are GETs. Closed-order bounds
  select SUBMISSION time; this module deliberately reads only the trade date,
  not custody.ended_orders' older 92-day walk. Older submissions are a limit.
* alpaca-skills 0e7c35163ae4, trading-api/paper-trading/SKILL.md:118,365,590-599:
  only limit orders qualify for extended hours; new is routed, accepted is not
  yet routed. Stop cover here requires new or partially_filled, never pending,
  held, accepted, canceled or ended. Regular/extended/overnight hours come from
  that pin and the returned Calendar (including early closes), never weekdays
  in the production pass. A fresh engine report names its eligible sessions.
* alpaca-py common/rest.py:35-100,371-411 and data/historical/stock.py:75-96:
  the REST get method, /stocks/bars parameters, SIP 1Min volume, pagination.
  We read raw pages explicitly to count, pace and bound every GET, with _retry=0
  as session_window.send_once does. No SDK automatic pagination is used.
* canon_alpaca.providers.require_get_only/TimeoutAdapter, custody.lineage;
  home/.claude/canon/lanes-watch.sh:87-90,116-117: alert JSONL fields below.
* Python's flock and SIGALRM contracts, already used in tests/conftest.py:
  a monitor-only descriptor excludes another pass, never an account/stream lock;
  ITIMER_REAL interrupts reads on the main thread. No background work survives.
* alpaca-py common/rest.py:115-130 already sets allow_redirects=False, so a
  redirect cannot multiply a counted GET. requests 6e83187b8feb,
  sessions.py:490-492: trust_env=False prevents implicit netrc authentication
  or proxy configuration, keeping the task's explicit pair as the sole source.

Files default to CLAUDE_CONFIG_DIR/canon/exit-monitor/<A|B>/ (else ~/.claude).
memory.json carries episodes; status.json is for unit two to consume; records/
has one JSONL file per New York trade date (20:00 rolls the date). alerts.jsonl
is this monitor's OUTBOX, in the watchdog's form, not its shared alert file.
Unit two owns delivery/retry, status display and installation of the 60s schedule.

Boundary ruling (lead, 2026-11-13): P20 and P21 are separate facts calling for
different acts, so each has its own notice and status/record entry. Deduplication
is per kind, account, holding and boundary, never by their joined message.

Node-report contract (the lead's 2026-11-13 R7 level-book ruling), for its later
writer: reports.json contains version=1, account=A|B, last_step_at (aware ISO),
step_seconds (positive), and holdings=[{symbol, quantity, state, level,
armed_at, count, required_count, reason, sessions, client_order_id?, result?}].
Each holding names armed at its level, guarded, counting n of N, waiting with
its reason, or frozen with its reason and result=denied|rejected. Only armed
and counting cover; freshness is at most three steps since last_step_at, not
since armed_at. The monitor uses the state/quantity; the level, countdown and
free-text reason stay in the book (raw text could contain a venue identifier).
Only our node's holdings belong there. The default session list is regular.
An optional client_order_id identifies ONE stop whose quantity is its open
remainder, never an aggregate. A venue stop with that identity stands alone;
an unidentified report adds nothing where another exit could be the same one.
Repeated identities count once. No client order identifier is emitted.
The injected assess/run_pass seam retains its original {symbol, quantity,
armed_at} armed-stop shorthand (150s bound); files require the level book.
Missing or stale reports read "no protector running".
There is no read-only owner query in the account's flock contract; the production
pass does not probe/take that lock. run_holds is an explicit injected observation
for callers which can establish it, and defaults false; absent reports leave an
uncovered holding. An engine DENIED cannot be inferred from the venue.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import time as wall_time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.common.enums import BaseURL
from alpaca.common.enums import Sort
from alpaca.common.rest import RESTClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import data_only
from canon_alpaca.providers import require_get_only

from . import custody
from . import session_window

NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_ACCOUNTS = ("A", "B")
INTERVAL_SECONDS = 60  # UNCALIBRATED: P19's schedule contract; installed by unit two.
REPORT_FRESH_SECONDS = 150  # UNCALIBRATED: original injected armed-stop shorthand only.
REPORT_FRESH_STEPS = 3  # UNCALIBRATED (P2/R7); the level book supplies the node's step duration.
ALERT_AGE_SECONDS = 120  # UNCALIBRATED (P11).
REPEAT_SECONDS = 900  # UNCALIBRATED (P11).
BLIND_PASSES = 3  # UNCALIBRATED (P11).
STALE_INTERVALS = 3  # UNCALIBRATED (P10).
BASE_GET_LIMIT = 4  # UNCALIBRATED (P17), separate from the conditional history walk.
HISTORY_GET_LIMIT = 4  # UNCALIBRATED: at most four pages in that one walk.
TAPE_GET_LIMIT = 20  # UNCALIBRATED: counted separately, still inside the pass deadline.
PASS_SECONDS = 45  # UNCALIBRATED (P17), including pacing and tape.
READ_PACE_SECONDS = 2.5  # UNCALIBRATED; session_window.POLL_SECS's existing read pace.
BOUNDARY_MINUTES = 15  # UNCALIBRATED (P20/P21).
TAPE_SESSIONS = 5  # UNCALIBRATED (P21).
TAPE_RATIO = Decimal(1)  # UNCALIBRATED (P21).
CALENDAR_DAYS_BACK = 30  # UNCALIBRATED; insufficient sessions are named, not fabricated.
CALENDAR_DAYS_AHEAD = 7  # UNCALIBRATED; next regular open also comes from the clock.
ORDERS_PAGE_LIMIT = 500  # Alpaca's documented maximum, not a calibrated threshold.
BAR_PAGE_LIMIT = 10000  # alpaca-py's documented /stocks/bars page size at the pin.
STOP_KINDS = frozenset({"stop", "stop_limit", "trailing_stop"})
ACKNOWLEDGED = frozenset({"new", "partially_filled"})
STATES = ("covered", "dormant", "uncovered", "unknown")
PARTS = frozenset({"regular", "pre-market", "after-hours", "overnight"})
DENIED_LIMIT = "no node report: a locally denied sell is alerted at the alert age, not at once"
# Source: packages/canon-alpaca/README.md, "Wash trades"; observations, not documentation.
WASH_TRADE_CODE = 40_310_000  # OBSERVED 2026-11-13 post-market.
WASH_TRADE_MESSAGE = "potential wash trade detected"  # OBSERVED 2026-11-13 post-market.
BLOCKING_BUY_KINDS = frozenset({"market", "limit"})  # OBSERVED 2026-11-13 post-market.
# UNCALIBRATED: frozen brief's default; no measured source or protector limit offset
# is exposed at this base. This monitor does not know the protector's sale limit.
SALE_REACH = Decimal("0.02")
NODE_STOP = "node engine stop"
"""The cover a node report gives: a stop the node holds itself, with no order at the venue."""


class ReadFailed(RuntimeError):
    """A locally authored diagnostic, never a venue response body."""


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _field(value: Any, field: str, default: Any = None) -> Any:
    return (
        value.get(field, default) if isinstance(value, Mapping) else getattr(value, field, default)
    )


def _decimal(value: Any, *, signed: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except ArithmeticError, ValueError:
        raise ReadFailed("quantity or result is not shown as a decimal") from None
    if not number.is_finite() or (number < 0 and not signed):
        raise ReadFailed("quantity or result is not a finite permitted decimal")
    return number


def _stamp(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ReadFailed("timestamp is not shown") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ReadFailed("timestamp is not shown with a timezone")
    return value


def _local(value: datetime) -> datetime:
    # Calendar.open/close are naive New York wall times at this SDK pin.
    return value.replace(tzinfo=NEW_YORK) if value.tzinfo is None else value.astimezone(NEW_YORK)


def _fresh(report: Mapping[str, Any], now: datetime) -> bool:
    if not isinstance(report, Mapping):
        raise ReadFailed("node report is not an object")
    sessions = report.get("sessions", ["regular"])
    if not isinstance(sessions, list) or not all(isinstance(part, str) for part in sessions):
        raise ReadFailed("node report names no valid session eligibility")
    for field in ("symbol", "state", "client_order_id"):
        if report.get(field) is not None and not isinstance(report[field], str):
            raise ReadFailed(f"node report {field} is not text")
    if "last_step_at" in report or "step_seconds" in report:
        step = _decimal(report.get("step_seconds"))
        if not step:
            raise ReadFailed("node report step duration must be positive")
        age = (now - _stamp(report.get("last_step_at"))).total_seconds()
        return 0 <= age and Decimal(str(age)) <= REPORT_FRESH_STEPS * step
    age = (now - _stamp(report.get("armed_at"))).total_seconds()
    return 0 <= age <= REPORT_FRESH_SECONDS


def _cover(symbol: str, orders: Sequence[Any], reports: Sequence[Any], now: datetime, part: str):
    cover = active = foreign = Decimal(0)
    kinds: set[str] = set()
    venue_stops: set[str] = set()
    report_notes: set[str] = set()
    frozen = ""
    for order in orders:
        if _field(order, "symbol") != symbol or _value(_field(order, "side")) != "sell":
            continue
        status = _value(_field(order, "status"))
        if status in custody.ENDED_AT:
            continue
        qty = _decimal(_field(order, "qty"))
        filled = _decimal(_field(order, "filled_qty"))
        if filled > qty:
            raise ReadFailed("a sell's filled quantity exceeds its quantity")
        left = qty - filled
        owner = custody.lineage(_field(order, "client_order_id"))
        if owner not in (custody.OURS, custody.PROTECT):
            foreign += left
            continue
        # R7: any open sell of ours the venue shows, counted or not (being canceled, not yet
        # acknowledged, not a stop kind), is one a node report may not add as engine cover.
        venue_stops.add(str(_field(order, "client_order_id")))
        kind = _value(_field(order, "type") or _field(order, "order_type"))
        if kind not in STOP_KINDS or status not in ACKNOWLEDGED:
            continue
        cover += left
        kinds.add(f"{owner} {kind}")
        if part == "regular":
            active += left
    current_reports = []
    for report in reports:
        if not isinstance(report, Mapping):
            raise ReadFailed("node report is not an object")
        if report.get("symbol") == symbol and _fresh(report, now):
            current_reports.append(report)
    if not current_reports:
        report_notes.add("no protector running")
    armed_reports = []
    for report in current_reports:
        state = report.get("state", "armed" if "last_step_at" not in report else None)
        if state == "frozen":
            result = report.get("result")
            if result not in ("denied", "rejected"):
                raise ReadFailed("frozen node report names no denied or rejected sell")
            frozen = result
            continue
        if state in ("guarded", "waiting"):
            continue
        if state not in ("armed", "counting"):
            raise ReadFailed("node report state is not a level-book state")
        armed_reports.append(report)
    seen_stops: set[str] = set()
    for report in armed_reports:
        qty = _decimal(report.get("quantity"))
        sessions = report.get("sessions", ["regular"])
        if not isinstance(sessions, list) or not set(sessions) <= PARTS:
            raise ReadFailed("node report names no valid session eligibility")
        identity = report.get("client_order_id")
        if identity:
            if custody.lineage(identity) != custody.OURS:
                raise ReadFailed("node report stop identity is not of our node lineage")
            identity = str(identity)
            if identity in venue_stops:
                report_notes.add("report not added: the same stop is shown at the venue")
                continue
            if identity in seen_stops:
                report_notes.add("report not added: the same stop was already reported")
                continue
            seen_stops.add(identity)
        elif venue_stops or len(armed_reports) > 1:
            report_notes.add("report not added: its stop cannot be distinguished from another exit")
            continue
        cover += qty
        kinds.add(NODE_STOP)
        if part in sessions:
            active += qty
    return (
        cover,
        active,
        foreign,
        ", ".join(sorted(kinds)) or "no armed exit",
        frozen,
        "; ".join(sorted(report_notes)),
    )


def assess(
    account: str,
    positions: Sequence[Any],
    orders: Sequence[Any],
    reports: Sequence[Any],
    memory: Mapping[str, Any],
    now: datetime,
    run_holds: bool = False,
    *,
    is_open: bool | None = None,
    part: str | None = None,
    next_open: datetime | None = None,
    fills: Mapping[str, datetime] | None = None,
    rejected: Mapping[str, str] | None = None,
    read_error: str = "",
) -> list[dict[str, Any]]:
    """P1-P9/P22: pure; supplied clock/calendar facts override the wall-time test seam.

    No file, transport, lock or clock is read, and memory is never mutated here.
    A caller without a venue clock supplies is_open/part; the short seam uses
    the pinned ordinary regular hours, not a claim about a holiday calendar.
    """
    now = _stamp(now)
    local = now.astimezone(NEW_YORK)
    if is_open is None:
        is_open = local.weekday() < 5 and wall_time(9, 30) <= local.time() < wall_time(16)
    part = part or ("regular" if is_open else "closed")
    findings = []
    for position in positions:
        symbol = str(_field(position, "symbol"))
        side = _value(_field(position, "side"))
        invalid = ""
        try:
            quantity = _decimal(_field(position, "qty"), signed=True)
        except ReadFailed as exc:
            quantity, invalid = Decimal(0), str(exc)
        if not quantity and not invalid:
            continue
        short = side == "short" or quantity < 0
        quantity = abs(quantity)
        cover = active = foreign = Decimal(0)
        exit_kind, frozen, frozen_source = "no armed exit", "", ""
        report_note = ""
        reason = ""
        state = "uncovered"
        try:
            cover, active, foreign, exit_kind, report_frozen, report_note = _cover(
                symbol, orders, reports, now, part
            )
            if short:
                cover = active = Decimal(0)
                reason = "a short: no exit form is defined for it"
            elif cover >= quantity:
                state = (
                    "covered" if active >= quantity else "dormant" if not active else "uncovered"
                )
                reason = (
                    "whole armed exit"
                    if state == "covered"
                    else "exit cannot act for the whole holding in this session"
                )
            elif report_frozen:
                frozen, frozen_source = report_frozen, "node report"
            elif symbol in (rejected or {}):
                frozen, frozen_source = "rejected", "venue"
            else:
                old = memory.get("episodes", {}).get(symbol, {})
                frozen, frozen_source = old.get("frozen", ""), old.get("frozen_source", "")
                if frozen_source == "venue" and rejected is not None:
                    frozen, frozen_source = "", ""
                if not frozen and run_holds and not cover:
                    state = "unknown"
                    reason = "held by a run of ours, its exit is not visible"
            if frozen:
                reason = f"frozen by the protector: its sell was {frozen} (source: {frozen_source})"
            if not reason:
                reason = "no whole armed exit of ours"
        except ReadFailed as exc:
            state, reason, invalid = "unknown", str(exc), str(exc)
        if invalid:
            state, reason = "unknown", invalid
        if read_error:
            state, reason = "unknown", read_error
        if foreign:
            reason += f"; a foreign sell rests for {foreign}"
        if report_note:
            reason += f"; {report_note}"
        excess = max(Decimal(0), cover - quantity)
        if excess:
            reason += f"; excess {excess} could open a short where shorting is allowed"
        remainder = max(Decimal(0), quantity - cover)
        if state == "uncovered" and cover >= quantity:
            remainder = max(Decimal(0), quantity - active)
        source, since = "since first seen", now
        if state != "covered":
            previous = memory.get("episodes", {}).get(symbol)
            if previous:
                since, source = _stamp(previous["since"]), previous["source"]
            elif memory.get("states", {}).get(symbol) == "covered":
                source = "since cover lost"
            elif symbol in (fills or {}):
                since, source = _stamp((fills or {})[symbol]), "since fill"
        findings.append(
            {
                "account": account,
                "symbol": symbol,
                "quantity": "not shown" if invalid and not quantity else str(quantity),
                "side": "short" if short else "long",
                "state": state,
                "cover": str(cover),
                "remainder": str(remainder),
                "excess": str(excess),
                "age_source": source,
                "age_seconds": max(0.0, (now - since).total_seconds()) if state != "covered" else 0,
                "since": since.isoformat(),
                "reason": reason,
                "exit": exit_kind,
                "next_act": now.isoformat()
                if state == "covered"
                else next_open.isoformat()
                if cover and next_open
                else "next regular open not supplied"
                if cover
                else "no armed exit",
                "frozen": frozen,
                "frozen_source": frozen_source,
                "invalid": bool(invalid),
            }
        )
    return findings


def empty_memory(account: str) -> dict[str, Any]:
    return {
        "version": 1,
        "account": account,
        "positions": {},
        "states": {},
        "episodes": {},
        "failures": 0,
        "blind_alerted": False,
        "blind_since": None,
        "boundaries": {},
        "exit_blocks": {},
    }


def _load_memory(path: Path, account: str):
    try:
        value = json.loads(path.read_text())
        if value["version"] != 1 or value["account"] != account:
            raise ValueError
        for key in ("positions", "states", "episodes", "boundaries"):
            if not isinstance(value[key], dict):
                raise TypeError
        if not isinstance(value["failures"], int) or value["failures"] < 0:
            raise ValueError
        if not isinstance(value["blind_alerted"], bool):
            raise TypeError
        if value.get("blind_since"):
            _stamp(value["blind_since"])
        if any(state not in STATES for state in value["states"].values()):
            raise ValueError
        for row in value["positions"].values():
            _decimal(row["qty"], signed=True)
            if row["side"] not in ("long", "short") or not isinstance(row["symbol"], str):
                raise ValueError
        for episode in value["episodes"].values():
            _stamp(episode["since"])
            if episode["source"] not in ("since fill", "since first seen", "since cover lost"):
                raise ValueError
            if episode.get("last_alert"):
                _stamp(episode["last_alert"])
            if episode["kind"] not in STATES or episode.get("frozen", "") not in (
                "",
                "denied",
                "rejected",
            ):
                raise ValueError
            if not isinstance(episode.get("memory_lost", False), bool):
                raise TypeError
        for entry in value["boundaries"].values():
            _stamp(entry["at"])
            if not isinstance(entry["symbol"], str) or not isinstance(entry["message"], str):
                raise TypeError
        # Older unit-one memory remains valid; this fact owns a separate episode map, and a map
        # that cannot be read is dropped ALONE: the coverage episodes and ages above are kept.
        value.pop("exit_blocks_lost", None)
        try:
            _check_block_memory(value.setdefault("exit_blocks", {}))
        except ValueError, KeyError, TypeError, ReadFailed:
            value["exit_blocks"], value["exit_blocks_lost"] = {}, True
        return value, ""
    except FileNotFoundError:
        return empty_memory(account), "no memory file"
    except OSError, ValueError, KeyError, TypeError, ReadFailed:
        return empty_memory(account), "memory unreadable"


def _check_block_memory(episodes: Any) -> None:
    if not isinstance(episodes, dict):
        raise TypeError
    for key, episode in episodes.items():
        if not isinstance(key, str) or not isinstance(episode, dict):
            raise TypeError
        _stamp(episode["since"])
        _stamp(episode["last_alert"])
        if episode.get("cleared"):
            _stamp(episode["cleared"])
        if not isinstance(episode["alert_count"], int) or episode["alert_count"] < 1:
            raise ValueError
        row = episode["row"]
        if row["kind"] != "exit blocked" or not isinstance(row["message"], str):
            raise ValueError


def _root(account: str) -> Path:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    return config / "canon" / "exit-monitor" / account


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torn = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            torn = stream.read(1) != b"\n"
    with path.open("a") as stream:
        stream.write(("\n" if torn else "") + json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def pass_slot(memory_path: Path) -> Generator[None]:
    """Serialize only this monitor's account file, without touching trading locks."""
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    with memory_path.with_suffix(".running").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadFailed("monitor pass already running for this account") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def _deadline() -> Generator[None]:
    if threading.current_thread() is not threading.main_thread():
        raise ReadFailed("pass deadline requires the main thread; no reads started")

    def expired(signum, frame):
        raise ReadFailed("pass deadline exceeded")

    old_handler = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, PASS_SECONDS)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )


class ReadBudget:
    """Count the four ordinary reads, conditional walk and tape independently."""

    def __init__(self):
        self.counts = {"base": 0, "history": 0, "tape": 0}

    def call(self, group: str, call: Callable[[], Any]) -> Any:
        limit = {"base": BASE_GET_LIMIT, "history": HISTORY_GET_LIMIT, "tape": TAPE_GET_LIMIT}[
            group
        ]
        if self.counts[group] >= limit:
            raise ReadFailed(f"{group} GET bound reached; read incomplete")
        if group != "base" and self.counts[group]:
            time.sleep(READ_PACE_SECONDS)
        self.counts[group] += 1
        return call()


def _guard(client: Any, *, paper: bool) -> None:
    # The transport-free assess/run_pass test seam can use in-memory stand-ins.
    # Every real SDK client, injected or built here, must carry the adapter guard.
    if isinstance(client, RESTClient):
        # The switch is the entry point's alone (monitor_task); a pass never sets it, so a
        # process that is not data-only is refused here rather than turned data-only unasked.
        if not data_only():
            raise ReadFailed(
                f"GET-only monitor requires data-only mode ({DATA_ONLY_ENV}=1); no reads started"
            )
        if paper and (client._base_url != BaseURL.TRADING_PAPER or not client._sandbox):
            raise ReadFailed("GET-only monitor requires paper=True and no URL override")
        try:
            require_get_only(client)
        except MethodRefused:
            raise ReadFailed("GET-only adapter guard missing; no reads started") from None
        client._session.trust_env = False
        session_window.send_once(client)


def _trade_bounds(now: datetime):
    local = now.astimezone(NEW_YORK)
    day = local.date() + (timedelta(days=1) if local.time() >= wall_time(20) else timedelta())
    end = datetime.combine(day, wall_time(20), NEW_YORK)
    return day.isoformat(), end - timedelta(days=1), end


def ended_orders(trading: Any, now: datetime, budget: ReadBudget) -> list[Order]:
    """One date-bounded walk; bounded pages, overlap, no retry, no 92-day read."""
    _, start, end = _trade_bounds(now)
    until = end
    seen: dict[str, Order] = {}
    edge: set[str] = set()
    while True:
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=start - timedelta(microseconds=1),
            until=until,
            direction=Sort.DESC,
            limit=ORDERS_PAGE_LIMIT,
            nested=False,
        )
        page = budget.call("history", lambda request=request: trading.get_orders(request))
        if not isinstance(page, list) or not all(isinstance(o, Order) for o in page):
            raise ReadFailed("ended orders response is not a list of orders")
        stamps = [_stamp(o.submitted_at) for o in page]
        if any(t < start or t >= until for t in stamps) or stamps != sorted(stamps, reverse=True):
            raise ReadFailed("ended order page is outside its date bound or order")
        if not edge <= {str(o.id) for o in page}:
            raise ReadFailed("ended order page lost its overlapping boundary")
        for order in page:
            key = str(order.id)
            if key in seen and seen[key] != order:
                raise ReadFailed("ended order changed across pages")
            seen[key] = order
        if len(page) < ORDERS_PAGE_LIMIT:
            break
        earliest = min(stamps)
        boundary = earliest + timedelta(microseconds=1)
        if boundary >= until:
            raise ReadFailed("ended order page cannot advance")
        edge = {str(o.id) for o in page if o.submitted_at == earliest}
        until = boundary
    return [
        o
        for o in seen.values()
        if (field := custody.ENDED_AT.get(_value(o.status)))
        and start <= _stamp(getattr(o, field)) < end
    ]


def _history_facts(orders: Sequence[Order], positions: Sequence[Any]):
    fills: dict[str, datetime] = {}
    rejected: dict[str, str] = {}
    for position in positions:
        # P6 defines no short exit form; a buy covering a short is not a long entry.
        if _value(_field(position, "side")) != "long":
            continue
        symbol = _field(position, "symbol")
        rows = [o for o in orders if o.symbol == symbol]
        sells = [
            o
            for o in rows
            if _value(o.side) == "sell"
            and custody.lineage(o.client_order_id) in (custody.OURS, custody.PROTECT)
        ]
        if sells:
            latest = max(
                sells, key=lambda o: _stamp(getattr(o, custody.ENDED_AT[_value(o.status)]))
            )
            if _value(latest.status) == "rejected":
                rejected[symbol] = "venue"
        # A complete, unambiguous set of buys with no sells/replacement links
        # establishes the currently held quantity. Other history never invents age.
        try:
            positive = [o for o in rows if _decimal(o.filled_qty) > 0]
        except ReadFailed:
            # Optional fill quantities cannot establish a fill-derived age.
            # They are not needed to establish the dated rejection above.
            continue
        if (
            positive
            and all(
                _value(o.side) == "buy" and not o.replaces and not o.replaced_by for o in positive
            )
            and sum((_decimal(o.filled_qty) for o in positive), Decimal(0))
            == _decimal(_field(position, "qty"))
        ):
            fills[symbol] = min(_stamp(o.filled_at) for o in positive)
    return fills, rejected


def boundaries(days: Sequence[Calendar]) -> list[dict[str, Any]]:
    """Returned regular open/close, extended edges, and pinned overnight starts."""
    found: dict[datetime, dict[str, Any]] = {}
    opens = sorted(_local(d.open) for d in days)

    def put(at: datetime, part: str):
        following = next((opened for opened in opens if opened >= at), None)
        found[at] = {"at": at, "part": part, "next_open": following}

    for day in sorted(days, key=lambda d: d.date):
        if not day.session_open or not day.session_close:
            raise ReadFailed("calendar does not show extended session bounds")
        pre = datetime.combine(
            day.date, datetime.strptime(day.session_open, "%H%M").time(), NEW_YORK
        )
        after = datetime.combine(
            day.date, datetime.strptime(day.session_close, "%H%M").time(), NEW_YORK
        )
        put(datetime.combine(day.date - timedelta(days=1), wall_time(20), NEW_YORK), "overnight")
        put(pre, "pre-market")
        put(_local(day.open), "regular")
        put(_local(day.close), "after-hours")
        # A following overnight may overwrite this closed edge below.
        if after not in found:
            put(after, "closed")
    return [found[stamp] for stamp in sorted(found)]


def _part(now: datetime, clock: Clock, days: Sequence[Calendar] | None) -> str:
    if clock.is_open:
        return "regular"
    if days is None:
        return "closed"
    edges = [edge for edge in boundaries(days) if edge["at"] <= now]
    return edges[-1]["part"] if edges else "closed"


def tape_volume(symbol: str, start: datetime, end: datetime) -> tuple[Decimal | None, int]:
    """P21 seam: (figure, sessions); production supplies read_tape with its client."""
    return None, 0


def _has_minutes(days: Sequence[Calendar], begin: datetime, finish: datetime) -> bool:
    """The entire comparison window must be in the calendar's extended/overnight sessions."""
    spans = []
    for day in days:
        if day.session_open and day.session_close:
            pre = datetime.combine(
                day.date, datetime.strptime(day.session_open, "%H%M").time(), NEW_YORK
            )
            after = datetime.combine(
                day.date, datetime.strptime(day.session_close, "%H%M").time(), NEW_YORK
            )
            overnight = datetime.combine(day.date - timedelta(days=1), wall_time(20), NEW_YORK)
            spans.extend(((overnight, pre), (pre, after)))
    cursor = begin
    for left, right in sorted(spans):
        if left <= cursor < right:
            cursor = right
            if cursor >= finish:
                return True
    return False


def read_tape(
    data: Any,
    days: Sequence[Calendar],
    symbol: str,
    start: datetime,
    end: datetime,
    budget: ReadBudget,
) -> tuple[Decimal | None, int]:
    """Consolidated raw one-minute volume in the same wall minutes of five sessions.

    Each prior session gets one bounded window; all continuation pages are paced
    and charged to the separate tape budget. A session returning no bars is not
    counted as an observed zero-volume session. No feed substitution is made.
    """
    _guard(data, paper=False)
    local_start, local_end = start.astimezone(NEW_YORK), end.astimezone(NEW_YORK)
    candidates = sorted(
        (day for day in days if day.date < local_start.date()), key=lambda d: d.date, reverse=True
    )
    volumes = []
    for day in candidates:
        if len(volumes) >= TAPE_SESSIONS:
            break
        begin = datetime.combine(day.date, local_start.time(), NEW_YORK)
        finish = datetime.combine(
            day.date + (local_end.date() - local_start.date()), local_end.time(), NEW_YORK
        )
        if not _has_minutes(days, begin, finish):
            continue
        params = {
            "symbols": symbol,
            "timeframe": "1Min",
            "feed": "sip",
            "adjustment": "raw",
            "start": begin.astimezone(UTC).isoformat(),
            "end": (finish - timedelta(microseconds=1)).astimezone(UTC).isoformat(),
            "limit": BAR_PAGE_LIMIT,
            "sort": "asc",
        }
        tokens: set[str] = set()
        bars: dict[datetime, Decimal] = {}
        while True:
            response = budget.call(
                "tape", lambda params=params: data.get("/stocks/bars", data=params)
            )
            if not isinstance(response, dict) or not isinstance(response.get("bars"), dict):
                raise ReadFailed("tape not read: bars response malformed")
            for bar in response["bars"].get(symbol, []):
                stamp = _stamp(bar["t"])
                volume = _decimal(bar["v"])
                if not begin <= stamp < finish:
                    raise ReadFailed("tape not read: bar outside requested minutes")
                if stamp in bars and bars[stamp] != volume:
                    raise ReadFailed("tape not read: a bar changed across pages")
                bars[stamp] = volume
            token = response.get("next_page_token")
            if not token:
                break
            if token in tokens:
                raise ReadFailed("tape not read: pagination cannot advance")
            tokens.add(token)
            params["page_token"] = token
        if bars:
            volumes.append(sum(bars.values(), Decimal(0)))
    return (median(volumes) if volumes else None), len(volumes)


def _error(exc: Exception) -> str:
    if isinstance(exc, (ReadFailed, custody.ReadFailed)):
        return str(exc)
    code = getattr(exc, "status_code", None)
    return f"read failed: {type(exc).__name__}" + (f" HTTP {code}" if isinstance(code, int) else "")


def _notice(account: str, now: datetime, key: str, message: str, state: str) -> dict[str, Any]:
    """The watchdog's JSONL shape, including a stable key for unit two's retry."""
    return {
        "at": now.isoformat(),
        "key": f"exit-monitor:{account}:{key}",
        "sid": f"exit-monitor:{account}",
        "lane": f"exit monitor {account}",
        "flag": message,
        "state": state,
        "quiet_min": 0,
        "fail": 0,
    }


def _blocking_buys(account, positions, orders, findings, part):
    """Pure third-line assessment; a buy's status never limits it to routed orders."""
    # T5: a long whose exit is already PLACED at the venue gets no telling. A node's own stop is no
    # order at the venue: its sale is a new sell, the kind the table shows refused, so a holding
    # covered that way is still told.
    holdings = {
        f["symbol"]: f
        for f in findings
        if f["side"] == "long" and (f["state"] != "covered" or NODE_STOP in f["exit"])
    }
    prices = {_field(p, "symbol"): _field(p, "current_price") for p in positions}
    rows = {}
    for order in orders:
        symbol = _field(order, "symbol")
        status = _value(_field(order, "status"))
        if (
            symbol not in holdings
            or _value(_field(order, "side")) != "buy"
            or status in custody.ENDED_AT
        ):
            continue
        identity = json.dumps(("exit blocked", account, symbol, str(_field(order, "id"))))
        key = hashlib.sha256(identity.encode()).hexdigest()
        kind = _value(_field(order, "type") or _field(order, "order_type"))
        order_class = _value(_field(order, "order_class", "simple"))
        complex_order = order_class not in ("simple", "") or bool(_field(order, "group_id"))
        blocked = kind in BLOCKING_BUY_KINDS and not complex_order
        unavailable = False
        limit = None
        try:
            if _field(order, "limit_price") is not None:
                limit = _decimal(_field(order, "limit_price"))
            if blocked and kind == "limit":
                current = _decimal(prices.get(symbol))
                if not current or not limit:
                    raise ReadFailed("limit comparison unavailable")
                if limit < current * (1 - SALE_REACH):
                    continue
        except ReadFailed:
            blocked, unavailable = False, True
        owner = custody.lineage(_field(order, "client_order_id"))
        whose = (
            "ours"
            if owner in (custody.OURS, custody.PROTECT)
            else "owner's (foreign lineage)"
            if owner == custody.FOREIGN
            else "ownership not shown"
        )
        label = "exit blocked" if blocked else "not observed: may block"
        detail = (
            f"resting buy {kind}, time in force {_value(_field(order, 'time_in_force'))}, "
            f"status {status}, {whose}"
        )
        if limit is not None:
            detail += f", limit {limit}"
        if complex_order:
            detail += f", order class {order_class}; grouped order not observed"
        if unavailable:
            detail += "; comparison unavailable: current price or buy limit not shown as positive"
        if kind == "stop" and not complex_order:
            detail += (
                "; documented to block (Alpaca's page: a new order is rejected when either order "
                "is a market or stop order), not observed"
            )
        message = f"{symbol} quantity {holdings[symbol]['quantity']}: {label.upper() if blocked else label}; {detail}"
        if blocked:
            refused = (
                "extended-hours limit sell, with or without sell_to_close, and market sell refused "
                f'(HTTP 403, code {WASH_TRADE_CODE}, "{WASH_TRADE_MESSAGE}"); '
                "Alpaca's own trailing stop accepted beside it"
            )
            if kind == "limit":
                # The table's refusals beside a limit buy are one row: queued, not extended, over
                # the sell's limit. The price rule is this monitor's, not an observation.
                message += (
                    "; by this monitor's rule (a limit buy at or over where our sale would go: "
                    f"current price less SALE_REACH {SALE_REACH}, UNCALIBRATED); OBSERVED 2026-11-13 "
                    "post-market only beside a queued limit buy not marked for extended hours, "
                    f"priced over the sell's limit: {refused}; a live extended-hours limit buy "
                    "under the market was observed NOT to block a market sell"
                )
            else:
                message += f"; OBSERVED 2026-11-13 post-market: {refused}"
            if NODE_STOP in holdings[symbol]["exit"] and holdings[symbol]["state"] == "covered":
                message += (
                    f"; covered only by a {NODE_STOP}, which is no order at the venue: its sale "
                    "is a new sell"
                )
            if part != "after-hours":
                message += "; not observed in this session"
        rows[key] = {
            "account": account,
            "symbol": symbol,
            "quantity": holdings[symbol]["quantity"],
            "kind": label,
            "message": message,
            "comparison_unavailable": unavailable,
        }
    return {key: rows[key] for key in sorted(rows)}


def _block_events(account, now, rows, memory, complete):
    """Independent order episodes: immediate, repeat, and one silent clear."""
    if not complete:
        # A failed read cannot establish that a previously blocking order is gone.
        return [], []
    previous = memory.get("exit_blocks", {})
    episodes = {}
    lines = list(rows.values())
    events = []
    for key, row in rows.items():
        old = previous.get(key)
        if row["comparison_unavailable"]:
            if old:
                episodes[key] = old
            continue
        if row["kind"] != "exit blocked":
            continue
        # `old` may be an episode that cleared inside the repeat interval (kept below): the same
        # order blocking again is the same telling, so a price at the reach's edge tells once.
        episode = {
            "since": old["since"] if old else now.isoformat(),
            "last_alert": old["last_alert"] if old else None,
            "alert_count": old["alert_count"] if old else 0,
            "row": row,
        }
        if old is None or (now - _stamp(old["last_alert"])).total_seconds() >= REPEAT_SECONDS:
            notice_key = f"exit blocked:{key}:{episode['since']}:{episode['alert_count']}"
            events.append(_notice(account, now, notice_key, row["message"], "exit blocked"))
            episode["last_alert"] = now.isoformat()
            episode["alert_count"] += 1
        episodes[key] = episode
    for key in sorted(set(previous) - set(episodes)):
        old = previous[key]
        row = old["row"]
        if not old.get("cleared"):
            lines.append(
                {
                    **row,
                    "kind": "cleared",
                    "message": row["message"].replace("EXIT BLOCKED", "exit blocked cleared", 1),
                }
            )
        if (now - _stamp(old["last_alert"])).total_seconds() < REPEAT_SECONDS:
            episodes[key] = {**old, "cleared": old.get("cleared") or now.isoformat()}
    memory["exit_blocks"] = episodes
    return lines, events


def _blocks_kept(memory) -> int:
    """The block episodes that are ongoing: kept in memory and not cleared."""
    return sum(not episode.get("cleared") for episode in memory.get("exit_blocks", {}).values())


def _boundary_warnings(account, now, clock, days, positions, orders, reports, memory, reader):
    edges: list[dict[str, Any]]
    if days is None:
        # A transport-free caller may supply only the clock. It proves these two
        # regular boundaries, but cannot establish extended/overnight edges.
        edges = (
            [{"at": clock.next_close, "part": "after-hours", "next_open": clock.next_open}]
            if clock.is_open
            else [{"at": clock.next_open, "part": "regular", "next_open": clock.next_open}]
        )
    else:
        edges = boundaries(days)
    events = []
    error = ""
    # Worked on a copy and saved only on return: a walk that fails part-way leaves no entry
    # whose notice was never sent.
    cache = dict(memory["boundaries"])
    for edge in edges:
        at = edge["at"]
        if not 0 < (at - now).total_seconds() <= BOUNDARY_MINUTES * 60:
            continue
        for position in positions:
            symbol = _field(position, "symbol")
            key = f"{at.isoformat()}:{symbol}"
            quantity = abs(_decimal(_field(position, "qty"), signed=True))
            if not quantity:
                continue
            gain_key = f"P20:{key}"
            gain = _field(position, "unrealized_pl")
            if gain_key not in cache and gain is not None and _decimal(gain, signed=True) > 0:
                _, active, _, kind, _, _ = _cover(symbol, orders, reports, now, edge["part"])
                if active < quantity:
                    next_act = edge["next_open"] or clock.next_open
                    text = f"exit cannot act next: {symbol} quantity {quantity}, unrealized result {gain}, {kind}; next act {next_act.isoformat() if kind != 'no armed exit' else 'no armed exit'}"
                    cache[gain_key] = {
                        "at": at.isoformat(),
                        "symbol": symbol,
                        "kind": "P20",
                        "message": text,
                    }
                    events.append(_notice(account, now, "boundary:" + gain_key, text, "boundary"))
            tape_key = f"P21:{key}"
            if tape_key in cache:
                continue
            text = ""
            tape_sample = {"median": None, "sessions": 0}
            try:
                first_minute = now.replace(second=0, microsecond=0)
                volume, sessions = (
                    reader(symbol, first_minute, at + (at - first_minute))
                    if not error
                    else (None, 0)
                )
                if not isinstance(sessions, int) or sessions < 0:
                    raise ReadFailed("tape not read: invalid session count")
                if volume is not None:
                    volume = _decimal(volume)
                tape_sample = {
                    "median": str(volume) if volume is not None else None,
                    "sessions": sessions,
                }
                if volume is None:
                    text = f"{symbol} quantity {quantity}: tape not read; unjudged; rests on {sessions} sessions"
                elif sessions < TAPE_SESSIONS:
                    text = f"{symbol} quantity {quantity}: rests on {sessions} sessions; median {volume}; unjudged"
                else:
                    volume = _decimal(volume)
                    if not volume:
                        text = f"{symbol} quantity {quantity}: tape median zero, {sessions} sessions; ratio undefined; larger than tape"
                    elif (ratio := quantity / volume) > TAPE_RATIO:
                        text = f"{symbol} quantity {quantity}: tape median {volume}, {sessions} sessions; ratio {ratio}"
            except Exception as exc:  # noqa: BLE001 -- every read failure becomes unknown (P5a).
                error = _error(exc)
                text = f"{symbol} quantity {quantity}: tape not read; unjudged; {error}"
            cache[tape_key] = {
                "at": at.isoformat(),
                "symbol": symbol,
                "kind": "P21",
                "message": text,
                "tape": tape_sample,
            }
            if text:
                events.append(_notice(account, now, "boundary:" + tape_key, text, "boundary"))
    held = {_field(p, "symbol") for p in positions}
    active = [
        entry
        for entry in cache.values()
        if entry["symbol"] in held and _stamp(entry["at"]) > now and entry["message"]
    ]
    memory["boundaries"] = cache
    return events, active, error


def status_health(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    result = dict(row)
    reasons = []
    if (now - _stamp(row["checked_at"])).total_seconds() > STALE_INTERVALS * INTERVAL_SECONDS:
        reasons.append("stale pass")
    if not row["ok"]:
        reasons.append("last pass failed")
    if row["uncovered"] and row["oldest_uncovered_age"] >= ALERT_AGE_SECONDS:
        reasons.append("uncovered at or over alert age")
    result.update(healthy=not reasons, unhealthy_reasons=reasons)
    return result


def _snapshot(position: Any) -> dict[str, Any]:
    return {
        "symbol": _field(position, "symbol"),
        "qty": str(_field(position, "qty")),
        "side": _value(_field(position, "side")),
        "unrealized_pl": _field(position, "unrealized_pl"),
    }


def _reports(path: Path, account: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        report = json.loads(path.read_text())
        if (
            report["version"] != 1
            or report["account"] != account
            or not isinstance(report["holdings"], list)
        ):
            raise ValueError
        _stamp(report["last_step_at"])
        if not _decimal(report["step_seconds"]):
            raise ValueError
        holdings = []
        for holding in report["holdings"]:
            if not isinstance(holding, dict) or "state" not in holding:
                raise ValueError
            holdings.append(
                {
                    **holding,
                    "last_step_at": report["last_step_at"],
                    "step_seconds": report["step_seconds"],
                }
            )
        return holdings
    except OSError, ValueError, KeyError, TypeError:
        raise ReadFailed("node report file unreadable or invalid") from None


def _totals(record: Path, now: datetime, findings, episodes: int):
    previous = None
    if record.exists():
        with record.open() as stream:
            for line in stream:
                if line.strip():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue  # a line cut short by a killed pass
                    if isinstance(value, dict) and "totals" in value:
                        previous = value
    totals = (
        dict(previous["totals"])
        if previous
        else {"passes": 0, "episodes": 0, "longest_uncovered_age": 0, "minutes_uncovered": 0}
    )
    totals["passes"] += 1
    totals["episodes"] += episodes
    age = max((f["age_seconds"] for f in findings if f["state"] == "uncovered"), default=0)
    totals["longest_uncovered_age"] = max(totals["longest_uncovered_age"], age)
    if previous and any(
        f["state"] == "uncovered" and f["side"] == "long" for f in previous["findings"]
    ):
        # Do not claim observations across an outage: at most one cadence is
        # attributed to the preceding observed uncovered holding.
        elapsed = max(0.0, min(INTERVAL_SECONDS, (now - _stamp(previous["at"])).total_seconds()))
        totals["minutes_uncovered"] += elapsed / 60
    return totals


def _finish(
    account,
    root,
    path,
    memory,
    memory_note,
    now,
    findings,
    error,
    budget,
    boundary_events,
    boundary_warnings,
    notes,
    blocking_buys,
):
    events = list(boundary_events)
    exit_blocks, block_notices = _block_events(account, now, blocking_buys, memory, not error)
    events.extend(block_notices)
    old_episodes = memory["episodes"]
    current = {f["symbol"]: f for f in findings if f["state"] != "covered"}
    cleared = sorted(set(old_episodes) - set(current))
    new_episodes = 0
    episodes = {}
    for symbol, finding in current.items():
        old = old_episodes.get(symbol, {})
        new_episodes += not bool(old)
        episode = {
            "since": finding["since"],
            "source": finding["age_source"],
            "last_alert": old.get("last_alert"),
            "kind": finding["state"],
            "frozen": finding["frozen"],
            "frozen_source": finding["frozen_source"],
            "alert_count": old.get("alert_count", 0),
        }
        last = _stamp(episode["last_alert"]) if episode["last_alert"] else None
        due = (finding["state"] == "dormant" and old.get("kind") != "dormant") or (
            finding["state"] == "uncovered"
            and (finding["frozen"] or finding["age_seconds"] >= ALERT_AGE_SECONDS)
            and (
                last is None
                or (now - last).total_seconds() >= REPEAT_SECONDS
                or (finding["frozen"] and not old.get("frozen"))
            )
        )
        if due:
            message = (
                f"{symbol} quantity {finding['quantity']} {finding['state']}, remainder {finding['remainder']}, "
                f"age {finding['age_seconds']}s {finding['age_source']}; {finding['reason']}; next act {finding['next_act']}"
            )
            key = f"{symbol}:{finding['since']}:{finding['state']}:{episode['alert_count']}"
            events.append(_notice(account, now, key, message, finding["state"]))
            if finding["state"] == "uncovered":
                episode["last_alert"] = now.isoformat()
            episode["alert_count"] += 1
        episodes[symbol] = episode
    if error and not memory["failures"]:
        memory["blind_since"] = now.isoformat()
    memory["failures"] = memory["failures"] + 1 if error else 0
    if memory["failures"] >= BLIND_PASSES and not memory["blind_alerted"]:
        events.append(
            _notice(
                account,
                now,
                f"blind:{memory['blind_since']}",
                f"monitor blind on {account}",
                "unknown",
            )
        )
        memory["blind_alerted"] = True
    blind_cleared = not error and memory["blind_alerted"]
    if not error:
        memory["blind_alerted"] = False
        memory["blind_since"] = None
    counts = {state: sum(f["state"] == state for f in findings) for state in STATES}
    oldest = max((f["age_seconds"] for f in findings if f["state"] == "uncovered"), default=0)
    status = status_health(
        {
            "account": account,
            "checked_at": now.isoformat(),
            "ok": not error,
            "longs": sum(f["side"] == "long" for f in findings),
            **counts,
            "oldest_uncovered_age": oldest,
            "boundary_warnings": boundary_warnings,
            "exit_blocked": None if error else _blocks_kept(memory),
            "exit_blocks_observed": not bool(error),
            "error": error,
            "interval_seconds": INTERVAL_SECONDS,
        },
        now,
    )
    summary = (
        f"unknown: {error}"
        if error
        else "no position"
        if not findings
        else "all covered"
        if all(f["state"] == "covered" for f in findings)
        else ", ".join(f"{n} {state}" for state, n in counts.items() if n)
    )
    if memory.pop("exit_blocks_lost", False):
        notes.append("exit-block memory unreadable: its episodes start again; coverage memory kept")
    if memory_note:
        notes.append(
            f"{memory_note}: ages from the venue's fills where shown, else from first seen"
        )
    if cleared or blind_cleared:
        notes.append(
            "cleared: "
            + ", ".join(cleared + (["read failure; reads recovered"] if blind_cleared else []))
        )
    summary += "; " + "; ".join(notes) if notes else ""
    day, _, _ = _trade_bounds(now)
    record_path = root / "records" / f"{day}.jsonl"
    totals = _totals(record_path, now, findings, new_episodes)
    result = {
        "account": account,
        "at": now.isoformat(),
        "trade_date": day,
        "ok": not error,
        "summary": summary,
        "findings": findings,
        "exit_blocks": exit_blocks,
        "status": status,
        "alerts": events,
        "cleared": cleared,
        "blind_cleared": blind_cleared,
        "totals": totals,
        "requests": budget.counts,
    }
    record = {**result, "findings": [f for f in findings if f["state"] != "covered"]}
    _append(record_path, record)
    for event in events:
        _append(root / "alerts.jsonl", event)
    _atomic(root / "status.json", status)
    memory["episodes"] = episodes
    memory["states"] = {f["symbol"]: f["state"] for f in findings}
    # Boundary cache is only retained for the current trade date (upcoming 20:00
    # can belong to the following one, so keep future entries too).
    _, start, _ = _trade_bounds(now)
    memory["boundaries"] = {
        k: v for k, v in memory["boundaries"].items() if _stamp(v["at"]) >= start
    }
    _atomic(path, memory)
    return result


def run_pass(
    account: str,
    trading: Any,
    memory_path: Path | None = None,
    reports: Sequence[Any] | None = None,
    run_holds: bool = False,
    *,
    tape_reader: Callable | None = None,
    data: Any = None,
) -> dict[str, Any]:
    """P10-P22: one bounded read pass; all effects are monitor-owned local files."""
    if account not in DEFAULT_ACCOUNTS:
        return {
            "ok": False,
            "findings": [],
            "summary": "account refused; C needs the lead's named word; nothing read",
        }
    root = _root(account)
    path = memory_path or root / "memory.json"
    findings: list[dict[str, Any]] = []
    try:
        with pass_slot(path), _deadline():
            memory, memory_note = _load_memory(path, account)
            now = datetime.now(UTC)
            positions = list(memory["positions"].values())
            orders, fresh_reports = [], []
            clock, days = None, None
            error = ""
            budget = ReadBudget()
            notes = []
            fills, rejected = {}, None
            boundary_events, boundary_warnings = [], []
            try:
                if trading is None:
                    raise ReadFailed("no pair")
                _guard(trading, paper=True)
                clock = budget.call("base", trading.get_clock)
                if not isinstance(clock, Clock):
                    raise ReadFailed("clock response malformed")
                now = _stamp(clock.timestamp)
                positions = budget.call("base", trading.get_all_positions)
                if not isinstance(positions, list) or not all(
                    isinstance(p, Position) for p in positions
                ):
                    raise ReadFailed("positions response malformed")
                orders = budget.call("base", lambda: custody.open_orders(trading))
                if len(orders) >= ORDERS_PAGE_LIMIT:
                    raise ReadFailed("open orders page may be truncated")
                if hasattr(trading, "get_calendar"):
                    today = now.astimezone(NEW_YORK).date()
                    days = budget.call(
                        "base",
                        lambda: trading.get_calendar(
                            GetCalendarRequest(
                                start=today - timedelta(days=CALENDAR_DAYS_BACK),
                                end=today + timedelta(days=CALENDAR_DAYS_AHEAD),
                            )
                        ),
                    )
                    if not isinstance(days, list) or not all(
                        isinstance(day, Calendar) for day in days
                    ):
                        raise ReadFailed("calendar response malformed")
                else:
                    notes.append(
                        "calendar not supplied; only clock-confirmed regular boundaries assessed"
                    )
                fresh_reports = (
                    list(reports)
                    if reports is not None
                    else _reports(root / "reports.json", account)
                )
                fresh_reports = [report for report in fresh_reports if _fresh(report, now)]
                current_part = _part(now, clock, days)
                preliminary = assess(
                    account,
                    positions,
                    orders,
                    fresh_reports,
                    memory,
                    now,
                    run_holds,
                    is_open=clock.is_open,
                    part=current_part,
                    next_open=clock.next_open,
                )
                if any(f["invalid"] for f in preliminary):
                    raise ReadFailed("position or exit data invalid")
                previous = memory["positions"]
                history_needed = any(
                    f["side"] == "long"
                    and (
                        f["symbol"] not in previous
                        or Decimal(f["quantity"])
                        > abs(_decimal(previous[f["symbol"]]["qty"], signed=True))
                        or (
                            f["state"] == "uncovered"
                            and memory["states"].get(f["symbol"]) != "uncovered"
                        )
                    )
                    for f in preliminary
                )
                if history_needed:
                    fills, rejected = _history_facts(ended_orders(trading, now, budget), positions)
                reader = tape_reader or (
                    (
                        lambda symbol, start, end: read_tape(
                            data, days or [], symbol, start, end, budget
                        )
                    )
                    if data is not None
                    else tape_volume
                )
                boundary_events, boundary_warnings, error = _boundary_warnings(
                    account, now, clock, days, positions, orders, fresh_reports, memory, reader
                )
            except Exception as exc:  # noqa: BLE001 -- every read failure becomes unknown (P5a).
                error = _error(exc)
            if error:
                # A failed later read cannot erase an old position, even when
                # today's position read was empty or listed different symbols.
                known = dict(memory["positions"])
                if isinstance(positions, list):
                    known.update({_field(p, "symbol"): _snapshot(p) for p in positions})
                positions = list(known.values())
            if error:
                fresh_reports = []
            findings = assess(
                account,
                positions,
                orders,
                fresh_reports,
                memory,
                now,
                run_holds,
                is_open=clock.is_open if clock else False,
                part=_part(now, clock, days) if clock and not error else "closed",
                next_open=clock.next_open if clock else None,
                fills=fills,
                rejected=rejected,
                read_error=error,
            )
            blocking_buys = (
                _blocking_buys(account, positions, orders, findings, _part(now, clock, days))
                if clock and not error
                else {}
            )
            memory["positions"] = {_field(p, "symbol"): _snapshot(p) for p in positions}
            if not fresh_reports or any(
                finding["side"] == "long"
                and not any(
                    isinstance(report, Mapping) and report.get("symbol") == finding["symbol"]
                    for report in fresh_reports
                )
                for finding in findings
            ):
                notes.append(DENIED_LIMIT)
            notes.append(
                "history covers submissions on this trade date only; older fills/rejections not read"
            )
            return _finish(
                account,
                root,
                path,
                memory,
                memory_note,
                now,
                findings,
                error,
                budget,
                boundary_events,
                boundary_warnings,
                notes,
                blocking_buys,
            )
    except (ReadFailed, OSError, ValueError) as exc:
        # Output failures are visible to the caller; never claim a successful
        # pass whose record could not be written. Prior status then goes stale.
        reason = (
            _error(exc) if isinstance(exc, ReadFailed) else "output failed: " + type(exc).__name__
        )
        unknown = [{**finding, "state": "unknown", "reason": reason} for finding in findings]
        return {"ok": False, "findings": unknown, "summary": reason}


def monitor_task(
    account: str, trading_for: Callable | None = None, *, accounts: Sequence[str] | None = None
) -> dict[str, Any]:
    """One account per invocation. A/B default; no standing C authorization."""
    configured = tuple(accounts) if accounts is not None else DEFAULT_ACCOUNTS
    if account not in configured or account not in DEFAULT_ACCOUNTS:
        return {
            "ok": False,
            "findings": [],
            "summary": "account refused; C needs the lead's named word; nothing read",
        }
    if trading_for is not None:
        return run_pass(account, trading_for(account))
    os.environ[DATA_ONLY_ENV] = "1"
    # The task loads exactly one account's pair into its own environment using
    # paper:account's keychain pattern. Nothing reads a second account's pair.
    try:
        trading = session_window.paper_trading()
        data = session_window.window_data()
    except KeyError:
        return run_pass(account, None)
    return run_pass(account, trading, data=data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Display-only paper exit monitor; one pass, GET only"
    )
    parser.add_argument("account", choices=DEFAULT_ACCOUNTS)
    args = parser.parse_args()
    result = monitor_task(args.account)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
