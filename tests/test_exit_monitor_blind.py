"""Blind tests for `canon_trading.exit_monitor` at main `8d44ae3b`, where the module does not exist.

The unit is the exit monitor: data only, one finding per position, `covered`, `dormant`, `uncovered` or
`unknown` (`l8-protect/texts/exit-monitor-properties.md`, 2026-11-13; the reader's note is
`deputy-eebefdab/reviews/handoff/exitmon-reader/NOTE.md`). These tests are written to the three public
names the properties give - `assess`, `run_pass`, and the task a schedule runs once a minute, one process
per account - and to the GET-only reads the note names (`TradingClient.get_all_positions` and
`custody.open_orders`, which is `TradingClient.get_orders`). Every case is expected RED at this base: the
module is absent, so each case raises ModuleNotFoundError in its `World` fixture.

Call shape this file fixes, because a blind file must fix one (the builder may rename only what a module
constant names): `assess(account, positions, orders, reports, memory, now, run_holds)` returns findings;
`run_pass(account, trading, memory_path, reports, run_holds)` returns a result with `findings` and
`summary`; `monitor_task(account, trading_for)` runs one account's pass. A finding answers `symbol`,
`state`, `quantity`, `cover`, `remainder`, `excess`, `age_seconds`, `age_source`, `reason`, `next_act`.
A node's armed-stop report is `{symbol, quantity, armed_at}`. `run_holds` is P5b's signal; the properties
leave it to the builder to find, so this file passes it explicitly instead of inventing a file format.

Two rulings are awaited. `DORMANT_STATE` gates every case that needs the `dormant` state, and
`COUNT_RECORD` gates the per-trade-date record (P12); the other value skips with reason "not ruled".
`TAPE_BOUNDARY` is the one module attribute the P21 fake history source replaces; the builder renames it
here if the production name differs. `ALERT_FILE`, `STATUS_FILE` and `RECORD_DIR` are the local output
paths under `CLAUDE_CONFIG_DIR` - the watchdog's `watch/alerts.jsonl`, the P10 status record and the P12
per-date record; they are constants for the same reason.

No test reads a credential, builds a client, makes a request, or touches the account lock, the catalog or
the ledger. Prices are scaled and symbols neutral; the account is the label "A"/"B"/"C", never a number.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position

from canon_trading import custody

MODULE = "canon_trading.exit_monitor"
"""The module under test: absent at the blind base, so every case is red."""

DORMANT_STATE = True
"""The ruling awaited on the `dormant` state: True means the state exists and its cases run."""

COUNT_RECORD = "per_date"
"""The ruling awaited on the record: `per_date` means P12's per-trade-date file exists."""

TAPE_BOUNDARY = "tape_volume"
"""The module attribute the P21 fake history source replaces: the figure printed for one symbol over
the same clock minutes in prior sessions, and the count of sessions that figure rests on (P21). The
builder renames this one constant if the seam is named otherwise."""

ALERT_FILE = Path("canon") / "exit-monitor" / "A" / "alerts.jsonl"
"""P11's sink, the watchdog's own alert file, under `CLAUDE_CONFIG_DIR`."""

STATUS_FILE = Path("canon") / "exit-monitor" / "A" / "status.json"
"""P10's status record, under `CLAUDE_CONFIG_DIR`, read by `scripts/automation status`."""

RECORD_DIR = Path("canon") / "exit-monitor" / "A" / "records"
"""P12's per-trade-date record directory, under `CLAUDE_CONFIG_DIR`."""

DEFAULT_ACCOUNTS = ("A", "B")
"""P18: the configuration lists A and B; C is read only when named, and is not named by default."""

NEW_YORK = ZoneInfo("America/New_York")
OPEN_STATUSES = ("new", "accepted", "partially_filled", "pending_new", "pending_cancel")

PROTECT_ID = "canon-protect-20261111T140000Z-1-0123abcd"
NODE_ID = "O-20261111-140000-001-001-1"
FOREIGN_ID = "a-foreign-order-1"
_VENUE_IDS = iter(f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 1000))

DORMANT = pytest.mark.skipif(not DORMANT_STATE, reason="not ruled")
PER_DATE = pytest.mark.skipif(COUNT_RECORD != "per_date", reason="not ruled")


def at(hour: int, minute: int, second: int = 0, day: int = 11) -> datetime:
    """A New York wall-clock time on Wednesday 2026-11-11 (day 11) or Thursday (day 12)."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK)


REGULAR = at(10, 0)
AFTER_CLOSE = at(17, 0)


def a_position(
    symbol: str = "SYN",
    *,
    qty: str = "100",
    side: str = "long",
    entry: str = "100.00",
    current: str = "101.00",
    unrealized_pl: str = "1.00",
) -> Position:
    """A made-up Alpaca position; an `unrealized_pl` above zero is in gain, used by P20."""
    payload: dict[str, Any] = {
        "asset_id": "10000000-0000-4000-8000-000000000001",
        "symbol": symbol,
        "exchange": "ARCA",
        "asset_class": "us_equity",
        "avg_entry_price": entry,
        "qty": qty,
        "side": side,
        "market_value": current,
        "cost_basis": entry,
        "unrealized_pl": unrealized_pl,
        "unrealized_plpc": "0.001",
        "unrealized_intraday_pl": unrealized_pl,
        "unrealized_intraday_plpc": "0.001",
        "current_price": current,
        "lastday_price": "99.00",
        "change_today": "0.01",
        "qty_available": qty,
    }
    return Position.model_validate(payload)


def an_order(
    *,
    symbol: str = "SYN",
    side: str = "sell",
    qty: str = "100",
    filled: str = "0",
    kind: str = "trailing_stop",
    status: str = "new",
    client_order_id: str = PROTECT_ID,
    limit: str | None = None,
    time_in_force: str = "gtc",
    filled_at: datetime | None = None,
    submitted_at: datetime | None = None,
) -> Order:
    """A made-up Alpaca order, with the field `custody.lineage` classifies on."""
    when = submitted_at or REGULAR
    payload: dict[str, Any] = {
        "id": next(_VENUE_IDS),
        "client_order_id": client_order_id,
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
        "submitted_at": when.isoformat(),
        "asset_id": "10000000-0000-4000-8000-000000000002",
        "symbol": symbol,
        "asset_class": "us_equity",
        "qty": qty,
        "filled_qty": filled,
        "order_class": "simple",
        "order_type": kind,
        "type": kind,
        "side": side,
        "time_in_force": time_in_force,
        "status": status,
        "extended_hours": False,
    }
    if limit is not None:
        payload["limit_price"] = limit
    if kind in ("trailing_stop", "stop", "stop_limit"):
        payload["stop_price"] = "95.00"
        payload["trail_price"] = "5.00"
    if filled_at is not None:
        payload["filled_at"] = filled_at.isoformat()
    if status == "canceled":
        payload["canceled_at"] = when.isoformat()
    return Order.model_validate(payload)


def failure(kind: str) -> BaseException:
    """One of P5a's three failed reads: an HTTP error, a timeout, a 429."""
    if kind == "timeout":
        return TimeoutError("the read timed out")
    code = 429 if kind == "429" else 500
    message = '{"message": "rate limit exceeded"}' if code == 429 else '{"message": "server error"}'
    return APIError(message, SimpleNamespace(response=SimpleNamespace(status_code=code)))


class FakeTrading:
    """The paper read boundary: alpaca-py's `TradingClient` methods the note names, GET only, recorded.

    Nothing here is a client, a URL or a credential; the answers are alpaca-py's own models made from
    made-up payloads, as `tests/test_custody.py` builds them.
    """

    def __init__(
        self,
        now: datetime = REGULAR,
        *,
        positions: list[Position] | None = None,
        history: list[Order] | None = None,
    ) -> None:
        self.now = now
        self.positions = list(positions or [])
        self.history = list(history or [])
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, BaseException] = {}

    def _read(self, name: str, value: Any) -> Any:
        self.calls.append((name, None))
        if name in self.fail:
            raise self.fail[name]
        return value

    def open_orders(self) -> list[Order]:
        return [
            order
            for order in self.history
            if str(getattr(order.status, "value", order.status)) in OPEN_STATUSES
        ]

    def walked(self) -> list[Any]:
        """The ended-orders walk: a `get_orders` request with Alpaca's closed status."""
        found = []
        for name, request in self.calls:
            status = getattr(getattr(request, "status", None), "value", None)
            if name == "get_orders" and status == "closed":
                found.append(request)
        return found

    def get_clock(self) -> Clock:
        local = self.now.astimezone(NEW_YORK)
        is_open = local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 0)
        close = local.replace(hour=16, minute=0, second=0, microsecond=0)
        if local >= close:
            close += timedelta(days=1)
        opened = local.replace(hour=9, minute=30, second=0, microsecond=0)
        if local >= opened:
            opened += timedelta(days=1)
        return self._read(
            "get_clock",
            Clock.model_validate(
                {
                    "timestamp": local.astimezone(UTC).isoformat(),
                    "is_open": is_open,
                    "next_open": opened.astimezone(UTC).isoformat(),
                    "next_close": close.astimezone(UTC).isoformat(),
                }
            ),
        )

    def get_all_positions(self) -> list[Position]:
        return self._read("get_all_positions", list(self.positions))

    def get_orders(self, request: Any = None) -> list[Order]:
        self.calls.append(("get_orders", request))
        if "get_orders" in self.fail:
            raise self.fail["get_orders"]
        status = getattr(getattr(request, "status", None), "value", "open")
        if status == "open":
            rows = self.open_orders()
        else:
            rows = [order for order in self.history if order not in self.open_orders()]
        symbols = getattr(request, "symbols", None)
        if symbols:
            rows = [order for order in rows if order.symbol in symbols]
        after = getattr(request, "after", None)
        until = getattr(request, "until", None)
        if after is not None:
            rows = [
                order
                for order in rows
                if order.submitted_at is not None and order.submitted_at >= after
            ]
        if until is not None:
            rows = [
                order
                for order in rows
                if order.submitted_at is not None and order.submitted_at < until
            ]
        rows.sort(key=lambda order: order.submitted_at or REGULAR, reverse=True)
        limit = getattr(request, "limit", None) or 500
        return rows[:limit]

    def get_account_configurations(self) -> AccountConfiguration:
        return self._read(
            "get_account_configurations",
            AccountConfiguration.model_validate(
                {
                    "no_shorting": True,
                    "suspend_trade": False,
                    "fractional_trading": True,
                    "max_margin_multiplier": "4",
                    "trade_confirm_email": "all",
                    "ptp_no_exception_entry": False,
                    "max_options_trading_level": 3,
                    "disable_overnight_trading": False,
                }
            ),
        )


def _get(finding: Any, name: str) -> Any:
    """A finding's field, from a dataclass, a mapping or a named tuple; the builder's shape is its own."""
    if isinstance(finding, Mapping):
        return finding.get(name)
    return getattr(finding, name, None)


def _findings(result: Any) -> list[Any]:
    if isinstance(result, list | tuple):
        return list(result)
    if isinstance(result, Mapping):
        found = result.get("findings")
    else:
        found = getattr(result, "findings", None)
    assert found is not None, f"the pass gave no findings: {result!r}"
    return list(found)


def _summary(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("summary", ""))
    return str(getattr(result, "summary", ""))


def _only(result: Any, symbol: str = "SYN") -> Any:
    matches = [finding for finding in _findings(result) if str(_get(finding, "symbol")) == symbol]
    assert len(matches) == 1, f"expected exactly one finding for {symbol}: {_findings(result)!r}"
    return matches[0]


def _state(finding: Any) -> str:
    value = _get(finding, "state")
    assert value is not None, f"the finding has no state: {finding!r}"
    return str(value).strip().lower()


def _number(finding: Any, name: str) -> Decimal:
    value = _get(finding, name)
    assert value is not None, f"the finding names no {name}: {finding!r}"
    return Decimal(str(value))


def _age(finding: Any) -> int:
    value = _get(finding, "age_seconds")
    assert value is not None, f"the finding carries no age: {finding!r}"
    return round(float(value))


def _source(finding: Any) -> str:
    value = _get(finding, "age_source")
    assert value is not None, f"the finding names no age source: {finding!r}"
    return str(value).strip().lower()


def _reason(finding: Any) -> str:
    return str(_get(finding, "reason") or "").lower()


def _moment(value: Any) -> datetime | str:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return str(value)


def _alerts(world: World) -> list[dict[str, Any]]:
    path = world.config / ALERT_FILE
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"line": raw}
        lines.append(parsed if isinstance(parsed, dict) else {"line": parsed})
    return lines


def _naming(world: World, symbol: str) -> list[dict[str, Any]]:
    return [line for line in _alerts(world) if symbol in json.dumps(line)]


def _text(world: World) -> str:
    """Every local output this pass may write, as one string (P13 and the "cleared" line read it)."""
    parts = [json.dumps(line) for line in _alerts(world)]
    status = world.config / STATUS_FILE
    if status.exists():
        parts.append(status.read_text(encoding="utf-8"))
    records = world.config / RECORD_DIR
    if records.exists():
        parts.extend(path.read_text(encoding="utf-8") for path in sorted(records.glob("*.jsonl")))
    return "\n".join(parts)


def _status_row(world: World, account: str = "A") -> dict[str, Any]:
    path = world.config / STATUS_FILE
    assert path.exists(), f"the pass wrote no status record at {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, Mapping) and isinstance(data.get("accounts"), list):
        rows = data["accounts"]
    else:
        rows = [data]
    for row in rows:
        if isinstance(row, Mapping) and row.get("account") in (None, account):
            return dict(row)
    raise AssertionError(f"no status row for account {account}: {data!r}")


def _record_lines(world: World) -> list[dict[str, Any]]:
    directory = world.config / RECORD_DIR
    assert directory.exists(), f"the pass wrote no per-date record at {directory}"
    lines = []
    for path in sorted(directory.glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    return lines


class World:
    """One fake account, its memory, the fake tape and the local outputs, under a temporary config."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        now: datetime = REGULAR,
        positions: list[Position] | None = None,
        orders: list[Order] | None = None,
        reports: list[dict[str, Any]] | None = None,
        tape_volume: str = "0",
        tape_sessions: int = 0,
        run_holds: bool = False,
    ) -> None:
        self.config = tmp_path
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        self.module: ModuleType = importlib.import_module(MODULE)
        self.trading = FakeTrading(now, positions=positions, history=orders)
        self.reports = list(reports or [])
        self.run_holds = run_holds
        self.memory_path = tmp_path / "canon" / "exit-monitor" / "memory.json"
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.tape_calls: list[tuple[str, datetime, datetime]] = []
        self.tape_result = Decimal(tape_volume)
        self.tape_sessions = tape_sessions
        self.tape: Callable[[str, datetime, datetime], tuple[Decimal, int]] = self._tape
        monkeypatch.setattr(self.module, TAPE_BOUNDARY, self.tape)

    def _tape(self, symbol: str, start: datetime, end: datetime) -> tuple[Decimal, int]:
        self.tape_calls.append((symbol, start, end))
        return self.tape_result, self.tape_sessions

    def assess(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "account": "A",
            "positions": list(self.trading.positions),
            "orders": self.trading.open_orders(),
            "reports": list(self.reports),
            "memory": {},
            "now": self.trading.now,
            "run_holds": self.run_holds,
        }
        fields.update(overrides)
        return self.module.assess(**fields)

    def run(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "account": "A",
            "trading": self.trading,
            "memory_path": self.memory_path,
            "reports": list(self.reports),
            "run_holds": self.run_holds,
        }
        fields.update(overrides)
        return self.module.run_pass(**fields)

    def step(self, seconds: int, **overrides: Any) -> Any:
        self.trading.now += timedelta(seconds=seconds)
        return self.run(**overrides)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------------------------------------
# P1 to P6: what "covered" means


def test_case_01_a_flat_account_says_no_position_and_alerts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1, P1/P6: flat is no finding, the pass says "no position", no notification."""
    world = World(tmp_path, monkeypatch)
    result = world.run()
    assert _findings(result) == []
    assert "no position" in _summary(result).lower()
    assert _alerts(world) == []


def test_case_02_a_whole_protect_stop_in_the_regular_session_is_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 2, P2a/P3: one working protect stop for the whole quantity is `covered`."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100", filled="0")],
    )
    assert custody.lineage(PROTECT_ID) == custody.PROTECT
    finding = _only(world.assess())
    assert _state(finding) == "covered"
    assert _number(finding, "cover") == Decimal(100)


@DORMANT
def test_case_03_the_same_after_the_close_is_dormant_and_alerts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3, P4/P11: a whole cover outside the session is `dormant`, says when it can next act, and
    raises one notification when it begins and none on the next pass."""
    world = World(
        tmp_path,
        monkeypatch,
        now=AFTER_CLOSE,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100", filled="0")],
    )
    first = world.run()
    finding = _only(first)
    assert _state(finding) == "dormant"
    next_act = _moment(_get(finding, "next_act"))
    if isinstance(next_act, datetime):
        assert next_act.astimezone(NEW_YORK) == at(9, 30, day=12)
    else:
        assert "09:30" in next_act
    assert len(_naming(world, "SYN")) == 1
    world.step(60)
    assert len(_naming(world, "SYN")) == 1


def test_case_04_no_order_is_uncovered_and_alerts_at_the_alert_age_then_the_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4, P3/P7/P11: no order is `uncovered` for the whole, age `since first seen`, no
    notification under the 120 s alert age, exactly one at it, none on the next four passes, one more
    at the 900 s repeat."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100", current="99.00")],
    )
    first = world.run()
    finding = _only(first)
    assert _state(finding) == "uncovered"
    assert _number(finding, "quantity") == Decimal(100)
    assert _number(finding, "remainder") == Decimal(100)
    assert "since first seen" in _source(finding)
    assert _age(finding) == 0
    world.step(119)
    assert _naming(world, "SYN") == []
    world.step(1)
    at_alert = world.run()
    assert _age(_only(at_alert)) == 120
    assert len(_naming(world, "SYN")) == 1
    for _ in range(4):
        world.step(60)
        world.run()
    assert len(_naming(world, "SYN")) == 1
    world.step(1020 - 120 - 240)
    world.run()
    assert len(_naming(world, "SYN")) == 2


def test_case_05_a_partial_cover_is_uncovered_for_the_remainder_with_both_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 5, P3: a long of 100 with a stop for 60 is `uncovered` for 40, both numbers shown."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100", current="99.00")],
        orders=[an_order(qty="60", filled="0")],
    )
    finding = _only(world.assess())
    assert _state(finding) == "uncovered"
    assert _number(finding, "cover") == Decimal(60)
    assert _number(finding, "remainder") == Decimal(40)


def test_case_06_a_foreign_sell_stop_is_uncovered_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 6, P2: a foreign sell stop covers nothing, and the finding names it."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100", current="99.00")],
        orders=[
            an_order(
                symbol="SYN", qty="100", kind="stop", client_order_id=FOREIGN_ID, limit="90.00"
            )
        ],
    )
    assert custody.lineage(FOREIGN_ID) == custody.FOREIGN
    finding = _only(world.assess())
    assert _state(finding) == "uncovered"
    assert _number(finding, "remainder") == Decimal(100)
    assert "foreign" in _reason(finding) or "foreign" in json.dumps(finding, default=str).lower()


def test_case_07_a_foreign_open_buy_is_not_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 7, P2/P3: a foreign open BUY adds nothing; the long stays `uncovered`."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100", current="99.00")],
        orders=[
            an_order(symbol="SYN", side="buy", qty="100", kind="market", client_order_id=FOREIGN_ID)
        ],
    )
    finding = _only(world.assess())
    assert _state(finding) == "uncovered"
    assert _number(finding, "cover") == Decimal(0)


def test_case_08_our_resting_limit_sell_above_the_market_is_not_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 8, P2: our own limit sell above the market is not a stop kind and covers nothing."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100", current="99.00")],
        orders=[
            an_order(
                symbol="SYN",
                qty="100",
                kind="limit",
                client_order_id=NODE_ID,
                limit="120.00",
                time_in_force="day",
            )
        ],
    )
    assert custody.lineage(NODE_ID) == custody.OURS
    finding = _only(world.assess())
    assert _state(finding) == "uncovered"
    assert _number(finding, "cover") == Decimal(0)


def test_case_09_an_unacknowledged_order_and_one_being_canceled_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 9, P2: `pending_new` is not yet acknowledged and `pending_cancel` is being canceled."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position("SYN", qty="10", current="99.00"), a_position("ALT", qty="10")],
        orders=[
            an_order(symbol="SYN", qty="10", status="pending_new"),
            an_order(symbol="ALT", qty="10", status="pending_cancel"),
        ],
    )
    found = world.assess()
    assert _state(_only(found, "SYN")) == "uncovered"
    assert _state(_only(found, "ALT")) == "uncovered"


def test_case_10_a_fresh_node_report_covers_and_a_stale_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 10, P2c/P5b: a node's report within the 150 s bound covers; past it, it is no report."""
    fresh = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100")],
        reports=[
            {
                "symbol": "SYN",
                "quantity": "100",
                "armed_at": REGULAR - timedelta(seconds=30),
            }
        ],
    )
    covered = _only(fresh.assess())
    assert _state(covered) == "covered"
    stale = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100")],
        reports=[
            {
                "symbol": "SYN",
                "quantity": "100",
                "armed_at": REGULAR - timedelta(seconds=151),
            }
        ],
    )
    found = _only(stale.assess())
    assert _state(found) != "covered"
    assert _number(found, "cover") == Decimal(0)


def test_case_11_a_run_of_ours_holding_the_account_is_unknown_not_uncovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 11, P5b: a run holds the account, no venue exit is visible and no report covers it; the
    long is `unknown`, and no uncovered notification is raised."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100")],
        run_holds=True,
    )
    finding = _only(world.assess())
    assert _state(finding) == "unknown"
    assert "held by a run of ours" in _reason(finding) or "held" in _reason(finding)
    world.run()
    world.step(300)
    world.run()
    assert _naming(world, "SYN") == []


@pytest.mark.parametrize("kind", ["http", "timeout", "429"])
def test_case_12_a_failed_read_makes_every_position_unknown_and_three_passes_alert_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Case 12, P5a: an HTTP error, a timeout or a 429 makes every known position `unknown` with the
    reason; three such passes raise one "blind"; a later good pass clears it."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position("SYN", qty="100"), a_position("ALT", qty="100")],
    )
    good = world.run()
    assert len(_findings(good)) == 2
    world.trading.fail["get_all_positions"] = failure(kind)
    for index in range(3):
        failed = world.step(60)
        found = _findings(failed)
        assert {str(_get(finding, "symbol")) for finding in found} == {"SYN", "ALT"}
        assert {_state(finding) for finding in found} == {"unknown"}
        assert all(_reason(finding) for finding in found)
        blind = [line for line in _alerts(world) if "blind" in json.dumps(line).lower()]
        assert len(blind) == (1 if index == 2 else 0), (
            "one blind telling, only once the third failing pass has run"
        )
    world.trading.fail.clear()
    healed = world.run()
    assert all(_state(finding) != "unknown" for finding in _findings(healed))
    assert "blind" not in _summary(healed).lower()


# ---------------------------------------------------------------------------------------------------------
# P7 to P9: age


def test_case_13_age_since_fill_reads_the_ended_orders_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 13, P7: the opening fill 40 s before the first pass gives `since fill`, age 40 plus the
    passes' time, and the walk is asked once, not on later passes."""
    filled = REGULAR - timedelta(seconds=40)
    opening = an_order(
        symbol="SYN",
        side="buy",
        qty="5",
        filled="5",
        kind="market",
        status="filled",
        client_order_id=NODE_ID,
        filled_at=filled,
        submitted_at=filled - timedelta(seconds=10),
    )
    world = World(
        tmp_path, monkeypatch, now=REGULAR, positions=[a_position(qty="5")], orders=[opening]
    )
    first = world.run()
    finding = _only(first)
    assert _state(finding) == "uncovered"
    assert "since fill" in _source(finding)
    assert _age(finding) == 40
    assert len(world.trading.walked()) == 1
    world.step(30)
    later = world.run()
    assert _age(_only(later)) == 70
    assert len(world.trading.walked()) == 1


def test_case_13b_a_missing_memory_file_still_reads_since_fill_from_the_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 13b, P8 as amended by ruling R1: with no memory file, a position opened 100 s before the
    first pass still reads `since fill` from the venue's fill, and the missing file is said."""
    filled = REGULAR - timedelta(seconds=100)
    opening = an_order(
        symbol="SYN",
        side="buy",
        qty="5",
        filled="5",
        kind="market",
        status="filled",
        client_order_id=NODE_ID,
        filled_at=filled,
        submitted_at=filled - timedelta(seconds=10),
    )
    world = World(
        tmp_path, monkeypatch, now=REGULAR, positions=[a_position(qty="5")], orders=[opening]
    )
    assert not world.memory_path.exists()
    first = world.run()
    assert "no memory file" in _summary(first).lower()
    finding = _only(first)
    assert _state(finding) == "uncovered"
    assert "since fill" in _source(finding)
    assert _age(finding) == 100
    assert len(world.trading.walked()) == 1


def test_case_14_cover_lost_runs_from_the_pass_that_saw_it_uncovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 14, P7: covered, then the stop is gone with the long held; `since cover lost` and the age
    from the pass that saw it."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    assert _state(_only(world.run())) == "covered"
    world.trading.history = []
    world.step(120)
    lost = world.run()
    finding = _only(lost)
    assert _state(finding) == "uncovered"
    assert "since cover lost" in _source(finding)
    assert _age(finding) == 0
    world.step(60)
    assert _age(_only(world.run())) == 60


def test_case_15_growth_keeps_the_episode_and_a_new_episode_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 15, P8/P11: growth inside an episode does not restart the age; covered again ends the
    episode with a "cleared" line, and the next uncovered pass starts a new episode from zero."""
    world = World(tmp_path, monkeypatch, now=REGULAR, positions=[a_position(qty="100")])
    world.run()
    world.step(60)
    world.run()
    world.trading.positions = [a_position(qty="150")]
    world.step(60)
    grown = world.run()
    finding = _only(grown)
    assert _state(finding) == "uncovered"
    assert _age(finding) == 120
    world.trading.history = [an_order(qty="150")]
    world.step(60)
    covered = world.run()
    assert _state(_only(covered)) == "covered"
    assert "cleared" in (_summary(covered) + _text(world)).lower()
    world.trading.history = []
    world.step(60)
    new = world.run()
    finding = _only(new)
    assert _state(finding) == "uncovered"
    assert _age(finding) == 0


def test_case_16_missing_then_corrupt_memory_is_said_and_yields_no_false_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 16, P8 (amended by ruling R1): a missing memory is said as "no memory file" and, with no
    venue fill shown, ages start from `since first seen`; a corrupt one is said as "memory unreadable"
    on the pass that reads it and never yields `covered` it could not show."""
    world = World(
        tmp_path, monkeypatch, now=REGULAR, positions=[a_position(qty="100", current="99.00")]
    )
    missing = world.run()
    assert "no memory file" in _summary(missing).lower()
    finding = _only(missing)
    assert _state(finding) == "uncovered"
    assert "since first seen" in _source(finding)
    world.memory_path.write_text("{not json", encoding="utf-8")
    corrupt = world.step(60)  # the pass that reads the corrupt file
    assert "memory unreadable" in _summary(corrupt).lower()
    finding = _only(corrupt)
    assert _state(finding) != "covered"
    assert "since first seen" in _source(finding)


def test_case_17_a_short_is_one_uncovered_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 17, P6: a short is not a long and is not passed over; one `uncovered` finding says "short"."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position("SYN", qty="5", side="short", current="101.00")],
    )
    found = _findings(world.assess())
    assert len(found) == 1
    finding = found[0]
    assert _state(finding) == "uncovered"
    assert "short" in _reason(finding)


def test_case_18_cover_over_the_quantity_is_covered_and_the_excess_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 18, P3: a stop for more than the quantity is `covered`, and the excess is named."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="150", filled="0")],
    )
    finding = _only(world.assess())
    assert _state(finding) == "covered"
    assert _number(finding, "cover") == Decimal(150)
    assert _number(finding, "excess") == Decimal(50)


# ---------------------------------------------------------------------------------------------------------
# P1, P9, P10, P12 to P19: the pass, the record, the safety


def test_p1_a_pass_with_any_non_covered_finding_is_not_all_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: a finding is one of exactly four states and never blank; the summary is not "all covered"
    when one position is uncovered."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position("SYN", qty="100"), a_position("ALT", qty="100", current="99.00")],
        orders=[an_order(symbol="SYN", qty="100")],
    )
    result = world.run()
    found = _findings(result)
    assert {str(_get(finding, "symbol")) for finding in found} == {"SYN", "ALT"}
    states = {_state(finding) for finding in found}
    assert states <= {"covered", "dormant", "uncovered", "unknown"}
    assert "" not in states
    assert _state(_only(result, "SYN")) == "covered"
    assert _state(_only(result, "ALT")) == "uncovered"
    assert "all covered" not in _summary(result).lower()


def test_case_19_a_pass_sends_only_gets_within_its_pace_and_takes_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 19, P14/P15/P17: every recorded read is a GET, at most the four a pass is allowed, no
    send exists, and no lock file is created."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    world.run()
    names = [name for name, _ in world.trading.calls]
    assert names
    assert all(name.startswith("get_") for name in names)
    assert len(names) <= (5 if world.trading.walked() else 4)
    assert not any("submit" in name or "cancel" in name or "close" in name for name in names)
    assert list(world.config.rglob("*.lock")) == []


def test_p9_assess_twice_with_the_same_inputs_returns_equal_findings_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 22, P9: `assess` reads no clock and no file, so the same inputs give equal findings."""
    world = World(
        tmp_path,
        monkeypatch,
        positions=[a_position(qty="100"), a_position("ALT", qty="5", side="short")],
        orders=[an_order(qty="60")],
    )
    first = world.assess()
    second = world.assess()
    assert first == second
    assert world.trading.calls == []


def test_case_20_an_account_outside_the_configuration_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 20, P18: C is absent from the default configuration, so a pass for C reads nothing."""
    world = World(tmp_path, monkeypatch)
    calls: list[str] = []

    def trading_for(account: str) -> FakeTrading:
        calls.append(account)
        return FakeTrading(REGULAR)

    assert "C" not in DEFAULT_ACCOUNTS
    world.module.monitor_task(account="C", trading_for=trading_for)
    assert calls == []


def test_case_21_no_output_carries_a_venue_order_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 21, P13: no local output carries a venue order ID (the file carries no account number at
    all, so that half is not expressible here)."""
    order = an_order(qty="100")
    world = World(tmp_path, monkeypatch, positions=[a_position(qty="100")], orders=[order])
    result = world.run()
    text = _summary(result) + "\n" + _text(world)
    assert str(order.id) not in text


def test_p10_the_status_row_counts_each_state_and_names_what_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10: one status row per account with the pass time, the long count and the four state counts,
    with the oldest uncovered age; an uncovered finding at the alert age makes the row unhealthy."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    world.run()
    row = _status_row(world)
    assert row["longs"] == 1
    assert row["covered"] == 1
    assert row["dormant"] == 0
    assert row["uncovered"] == 0
    assert row["unknown"] == 0
    assert "2026-11-11" in json.dumps(row, default=str)
    world.trading.history = []
    world.step(60)
    row = _status_row(world)
    assert row["uncovered"] == 1
    assert row["healthy"] is True  # the cover was first seen lost on this pass: its age is 0
    world.step(120)  # carry the episode to the 120 s alert age
    row = _status_row(world)
    assert row["uncovered"] == 1
    assert row["healthy"] is False
    assert "uncovered" in json.dumps(row, default=str).lower()


@PER_DATE
def test_p12_each_pass_appends_one_line_to_the_trade_date_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P12 [ruling pending]: each pass appends one line per account to the New York trade date's
    record, with that finding's symbol, quantity, remainder, state, age and source."""
    world = World(
        tmp_path,
        monkeypatch,
        now=REGULAR,
        positions=[a_position(qty="100", current="99.00")],
        orders=[an_order(qty="60")],
    )
    world.run()
    world.step(60)  # the second pass
    world.run()  # the third pass
    lines = _record_lines(world)
    assert len(lines) == 3, "each of the three passes appends exactly one line"
    line = lines[-1]
    text = json.dumps(line, default=str)
    for expected in ("A", "SYN", "100", "40", "uncovered"):
        assert expected in text
    assert "age" in text.lower()
    assert "source" in text.lower()


def test_p16_a_missing_pair_is_unknown_and_the_other_account_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P16: a pair missing from the keychain is `unknown: no pair` for that account, and the other
    account is still checked; one process reads one account."""
    world = World(tmp_path, monkeypatch)
    calls: list[str] = []

    def trading_for(account: str) -> FakeTrading | None:
        calls.append(account)
        if account == "B":
            return None
        return FakeTrading(
            REGULAR, positions=[a_position(qty="100")], history=[an_order(qty="100")]
        )

    missing = world.module.monitor_task(account="B", trading_for=trading_for)
    assert "no pair" in _summary(missing).lower()
    worked = world.module.monitor_task(account="A", trading_for=trading_for)
    assert _state(_only(worked)) == "covered"
    assert calls == ["B", "A"]


def test_p19_the_pass_runs_outside_a_session_and_keeps_its_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P19: every minute, at all hours; on a Saturday morning the pass still runs and its finding is
    `dormant` or `uncovered`, never no position."""
    saturday = datetime(2026, 11, 14, 3, 0, tzinfo=NEW_YORK)
    world = World(
        tmp_path,
        monkeypatch,
        now=saturday,
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    result = world.run()
    finding = _only(result)
    assert _state(finding) in {"dormant", "uncovered"}
    assert "no position" not in _summary(result).lower()


# ---------------------------------------------------------------------------------------------------------
# P20: the boundary warning (display only, the lead's ruling 10, 16:35)


def test_p20_nothing_is_named_at_15_44(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P20: fifteen minutes before the 16:00 boundary nothing is named; at 15:44 the holding in gain
    with a regular-hours-only stop is not named."""
    world = World(
        tmp_path,
        monkeypatch,
        now=at(15, 44),
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    result = world.run()
    assert _state(_only(result)) == "covered"
    assert _naming(world, "SYN") == []


def test_p20_the_holding_in_gain_is_named_at_15_45(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P20: at 15:45, fifteen minutes before the 16:00 boundary, the holding in gain whose regular-
    hours-only stop cannot act in the next part of the session is named on the alert path, beside
    P21's "unjudged" telling for the same holding (its tape stub rests on no sessions)."""
    world = World(
        tmp_path,
        monkeypatch,
        now=at(15, 45),
        positions=[a_position(qty="100")],
        orders=[an_order(qty="100")],
    )
    world.run()
    named = _naming(world, "SYN")
    told_by_p20 = [
        line
        for line in named
        if not any(
            word in json.dumps(line).lower() for word in ("unjudged", "tape", "ratio", "sessions")
        )
    ]
    assert len(told_by_p20) == 1, "one P20 line, not only P21's unjudged telling"


def test_p20_a_holding_not_in_gain_is_not_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P20: the line names holdings whose unrealized result is above zero only; at the same boundary
    a holding whose result is at zero is not named, though P21's "unjudged" telling may still name it
    because its tape stub rests on no sessions."""
    world = World(
        tmp_path,
        monkeypatch,
        now=at(15, 45),
        positions=[a_position(qty="100", entry="100.00", current="99.00", unrealized_pl="0.00")],
        orders=[an_order(qty="100")],
    )
    world.run()
    named = _naming(world, "SYN")
    told_by_p20 = [
        line
        for line in named
        if not any(
            word in json.dumps(line).lower() for word in ("unjudged", "tape", "ratio", "sessions")
        )
    ]
    assert told_by_p20 == [], "P20 names only holdings in gain; P21's unjudged telling is allowed"


# ---------------------------------------------------------------------------------------------------------
# P21: size against the tape (display only, the lead's ruling 10, 16:35)


def test_p21_a_quantity_under_the_tape_is_not_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P21: at 15:45, inside the fifteen minutes before the close, a quantity under the tape's figure
    (held over five prior sessions) is not named; the fake history source at `TAPE_BOUNDARY` is used."""
    world = World(
        tmp_path,
        monkeypatch,
        now=at(15, 45),
        positions=[a_position(qty="50")],
        orders=[an_order(qty="50")],
        tape_volume="100",
        tape_sessions=5,
    )
    world.run()
    assert world.tape_calls
    size_lines = [
        line
        for line in _naming(world, "SYN")
        if any(
            word in json.dumps(line).lower() for word in ("unjudged", "tape", "ratio", "sessions")
        )
    ]
    assert size_lines == []


def test_p21_a_quantity_over_the_tape_is_named_with_the_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P21: at 15:45, inside the fifteen minutes before the close, a quantity over the tape's figure
    is named with the ratio (80 held over 50 printed is 1.6) and the count of sessions it rests on."""
    world = World(
        tmp_path,
        monkeypatch,
        now=at(15, 45),
        positions=[a_position(qty="80")],
        orders=[an_order(qty="80")],
        tape_volume="50",
        tape_sessions=5,
    )
    world.run()
    size_lines = [
        line
        for line in _naming(world, "SYN")
        if any(
            word in json.dumps(line).lower() for word in ("unjudged", "tape", "ratio", "sessions")
        )
    ]
    text = json.dumps(size_lines, default=str)
    assert "SYN" in text
    assert "1.6" in text or "160" in text
    assert "session" in text.lower()
