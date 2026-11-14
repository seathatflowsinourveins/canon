"""ADR 0158's staging (ii): an uncovered long on account A, told by the exit monitor's own agent.

What it shows: with the agent installed for account A by the owner, one share bought by this test with no
exit of any kind is written to A's outbox as `uncovered` once it is 120 s old, by the agent's own firings
(at least two passes more than 120 s apart see it), and the carrier delivers that notice. It prints how
long each step took from the fill, which ADR 0158's bound leaves `UNVERIFIED` until this runs. What it does
not show: a covered long (staging (iii)), C, or the banner itself, which the unified log shows:
`log show --last 15m --predicate 'process == "usernoted"'`.

The owner's steps around it, never a session's (ADR 0158):

    scripts/automation install-exit-monitor tests
    CANON_PAPER_EXITMON_STAGING=1 mise run paper:test -s -k test_paper_exit_monitor_tells_an_uncovered_long
    scripts/automation uninstall-exit-monitor tests
    scripts/automation status

It runs only when a paper key pair is in the environment, `CANON_PAPER_EXITMON_STAGING=1` asks for it and
the paper runner's own marker is there, so a routine `mise run paper:test` skips it. It refuses to start,
placing nothing, unless the agent's last firing for A was `ok` within 180 s, a session part is open by
Alpaca's clock, and account A holds no position and no open order. In the regular session it buys at market; in any other part by a DAY limit
marked extended hours through the latest quote on the part's feed (`session_window.close_limit`, the form
`paper:close` sells by), the only kind Alpaca takes overnight (ADR 0146's Evidence).

`CANON_PAPER_EXITMON_SYMBOL` names another stock than SPY. No order ID, account number or price is
printed, and no assertion compares an Alpaca object, so a failure shows none either. Whatever happens after the buy: every open order of the test's own is canceled, the share is sold
the same way it was bought if it is still held (a sell not filled in 60 s is canceled), and the account is
read flat; a step of the cleanup that raises is named and the rest still runs. A run killed before its
cleanup leaves one share of the symbol: `mise run paper:close tests` does not close it (the test's orders
are not of our nodes' lineage), so sell it in Alpaca's own screen.

Contract: alpaca-py at `c42add7c0a70`: `TradingClient.get_clock`, `get_calendar(GetCalendarRequest)`,
`get_all_positions`, `get_orders(GetOrdersRequest)`, `submit_order`, `get_order_by_client_id`,
`cancel_order_by_id`; `canon_trading.exit_monitor`'s folder and outbox key
(`exit-monitor:A:<symbol>:<since>:uncovered:<n>`), and `scripts/exit-monitor-notify`'s `delivered.json`.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import with_http_timeout

from canon_trading import exit_monitor
from canon_trading import session_window

SYMBOL = os.environ.get("CANON_PAPER_EXITMON_SYMBOL", "SPY")
PREFIX = "canon-check-exitmon-"
"""The test's own buy and sell: foreign to `custody.lineage`, as every paper test's orders are."""
ENDED = frozenset({"filled", "canceled", "expired", "rejected"})
FILL_WAIT_SECS = 60
AGENT_FRESH_SECS = 180
"""The agent's last firing must be this recent: three of its 60 s periods."""
SPAN_SECS = 120
"""Two of the agent's passes, both after the fill, must be more than this apart."""
ALERT_WAIT_SECS = 2 * 60 + exit_monitor.ALERT_AGE_SECONDS + 240 + 60
"""ADR 0158's bound, two periods plus the alert age plus one firing's 240 s bound, and a minute more."""
DELIVER_WAIT_SECS = 120
DEADLINE_ROOM_SECS = 1200

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "(mise run paper:test reads them from the login keychain)"
    ),
)


def observe(root: Path, symbol: str, since: datetime) -> tuple[list[datetime], dict[str, Any]]:
    """What the exit monitor's folder shows after `since`: the pass's `status.json` stamp, if that late,
    and the first `uncovered` outbox event for `symbol` written then or later (or {})."""
    stamps = []
    try:
        stamp = datetime.fromisoformat(json.loads((root / "status.json").read_text())["checked_at"])
        if stamp >= since:
            stamps.append(stamp)
    except OSError, ValueError, KeyError, TypeError:
        pass
    prefix = f"exit-monitor:A:{symbol}:"
    try:
        lines = (root / "alerts.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
            at = datetime.fromisoformat(event["at"])
        except ValueError, KeyError, TypeError:
            continue
        key = str(event.get("key", ""))
        if key.startswith(prefix) and event.get("state") == "uncovered" and at >= since:
            return stamps, event
    return stamps, {}


def delivered(root: Path) -> set[str]:
    """The keys the carrier has delivered (`scripts/exit-monitor-notify`'s `delivered.json`)."""
    try:
        return set(json.loads((root / "delivered.json").read_text()))
    except OSError, ValueError, TypeError:
        return set()


def _agent_ok() -> bool:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    try:
        agent = json.loads((config / "canon" / "watch" / "exit-monitor-tests.json").read_text())
        age = time.time() - float(agent["at_epoch"])
    except OSError, ValueError, KeyError, TypeError:
        return False
    return agent.get("outcome") == "ok" and age <= AGENT_FRESH_SECS


def _status(order: Any) -> str:
    return str(getattr(order.status, "value", order.status))


def _held(trading: Any) -> Decimal:
    for position in trading.get_all_positions():
        if position.symbol == SYMBOL:
            return Decimal(str(position.qty))
    return Decimal(0)


def _open_ours(trading: Any) -> list[Any]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
    return [o for o in orders if str(o.client_order_id or "").startswith(PREFIX)]


def _part(trading: Any) -> str:
    """The session part open now, as `custody` reads it, or "" when none is."""
    clock, calendar, _ = session_window.session_now(trading)
    if clock.is_open:
        return "regular"
    try:
        return session_window.session_part("extended", clock, calendar)[1]
    except RuntimeError:
        return ""


def _send(trading: Any, data: Any, side: OrderSide, part: str) -> str:
    client_order_id = f"{PREFIX}{uuid4().hex[:12]}"
    if part == "regular":
        request: Any = MarketOrderRequest(
            symbol=SYMBOL,
            qty=1,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    else:
        price, _ = session_window.close_limit(data, SYMBOL, side.value, part)
        request = LimitOrderRequest(
            symbol=SYMBOL,
            qty=1,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=float(price),
            extended_hours=True,
            client_order_id=client_order_id,
        )
    trading.submit_order(request)
    return client_order_id


def _wait_ended(trading: Any, client_order_id: str) -> str:
    status = ""
    for _ in range(FILL_WAIT_SECS):
        status = _status(trading.get_order_by_client_id(client_order_id))
        if status in ENDED:
            return status
        time.sleep(1)
    return status


def _close(trading: Any, data: Any, left: list[str]) -> None:
    for order in _open_ours(trading):
        try:
            trading.cancel_order_by_id(order.id)
        except Exception as exc:  # noqa: BLE001 - named, and the close is still tried
            left.append(f"a cancel failed: {type(exc).__name__}")
    if _held(trading) <= 0:
        return
    part = _part(trading)
    if not part:
        left.append("no session part is open to sell in")
        return
    sell = _send(trading, data, OrderSide.SELL, part)
    if _wait_ended(trading, sell) != "filled":
        left.append("the closing sell did not fill in 60 s; canceled")
        trading.cancel_order_by_id(trading.get_order_by_client_id(sell).id)


def test_paper_exit_monitor_tells_an_uncovered_long() -> None:
    marker = os.environ.get("CANON_ALPACA_STREAM_LOCK", "")
    if os.environ.get("CANON_PAPER_EXITMON_STAGING") != "1":
        pytest.skip("asked for only by CANON_PAPER_EXITMON_STAGING=1: ADR 0158's staging (ii)")
    if not marker or Path(marker).name != "canon-alpaca-stream-tests.lock":
        pytest.skip("not under mise run paper:test on account A")
    if not re.fullmatch(r"[A-Z]{1,5}", SYMBOL):
        pytest.skip("CANON_PAPER_EXITMON_SYMBOL is not a plain stock symbol")
    deadline = os.environ.get("CANON_PAPER_TEST_DEADLINE", "")
    if not deadline.isdigit() or int(deadline) - time.time() < DEADLINE_ROOM_SECS:
        pytest.skip(f"needs {DEADLINE_ROOM_SECS} s of the paper runner's deadline left")

    agent_ok = _agent_ok()
    assert agent_ok, (
        f"the exit monitor's agent for A has no ok firing in the last {AGENT_FRESH_SECS} s; "
        "the owner installs it: scripts/automation install-exit-monitor tests"
    )
    trading: Any = TradingClient(
        api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
    )
    data = with_http_timeout(StockHistoricalDataClient(*credentials_from_env()))
    part = _part(trading)
    if not part:
        pytest.skip("no session part is open by Alpaca's clock: nothing placed")
    positions = len(trading.get_all_positions())
    open_orders = len(trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=1)))
    assert positions == 0, "account A holds a position: nothing placed"
    assert open_orders == 0, "account A has an open order: nothing placed"
    root = exit_monitor._root("A")
    print(f"staging (ii) on A, {SYMBOL}, part {part}")
    left: list[str] = []
    try:
        bought = _wait_ended(trading, _send(trading, data, OrderSide.BUY, part))
        assert bought == "filled", "the one-share buy did not fill in 60 s"
        filled = datetime.now(UTC)
        seen: set[datetime] = set()
        alert: dict[str, Any] = {}
        ends = time.monotonic() + ALERT_WAIT_SECS
        while time.monotonic() < ends:
            stamps, found = observe(root, SYMBOL, filled)
            seen.update(stamps)
            alert = alert or found
            span = (max(seen) - min(seen)).total_seconds() if seen else 0.0
            if alert and span > SPAN_SECS:
                break
            time.sleep(5)
        told = bool(alert)
        assert told, f"no uncovered notice for the long within {ALERT_WAIT_SECS} s"
        span = (max(seen) - min(seen)).total_seconds() if seen else 0.0
        assert span > SPAN_SECS, f"no two passes after the fill more than {SPAN_SECS} s apart"
        delivered_at = None
        ends = time.monotonic() + DELIVER_WAIT_SECS
        while time.monotonic() < ends:
            if alert["key"] in delivered(root):
                delivered_at = datetime.now(UTC)
                break
            time.sleep(5)
        was_delivered = delivered_at is not None
        assert was_delivered, f"the carrier did not deliver the notice within {DELIVER_WAIT_SECS} s"
        written = (datetime.fromisoformat(alert["at"]) - filled).total_seconds()
        print(
            f"measured from the fill: notice written {written:.0f} s, delivered by "
            f"{(delivered_at - filled).total_seconds():.0f} s; {len(seen)} passes seen"
        )
    finally:
        try:
            _close(trading, data, left)
        except Exception as exc:  # noqa: BLE001 - named; the flat read still runs
            left.append(f"the close raised {type(exc).__name__}")
        flat = _held(trading) == 0 and not _open_ours(trading)
        print("account A read flat after the case" if flat else f"NOT flat: {left}")
        assert flat, f"left on A: {left}; sell the one share in Alpaca's own screen"
