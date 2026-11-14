"""Blind contracts: exit-monitor-third-line-brief.md, frozen 2026-11-13 19:06.

Brief SHA-256: 432b4eddc63904b6abe0be602be930af6c7496c3b44568f22aa6a41680618224.
Only the brief and AGENTS.md supplied monitor behaviour; neither its implementation
nor either existing monitor test file was read. There is exactly one test per case.

ASSUMED public interface (the brief supplies no callable or output-schema names):
* The module is runnable as ``python -m canon_trading.exit_monitor A`` (the
  ``tests`` account's letter in AGENTS.md). The first run rejected the task alias
  ``tests`` at its public CLI, before any simulated read; the harness uses ``A``.
* Its normal home-relative output includes an outbox: a path that contains ``outbox``, or the
  monitor's own ``alerts.jsonl`` (this second form was added by the owning lane after the blind
  run; the first alone was this file's assumption and matched no file the monitor writes).
* Findings are text lines, JSON objects, or JSONL objects; one outbox entry is one
  notice. Multiple reporting sinks are checked separately, never added together.
* ``O-20261113-232600-001-001-N`` is a node's client-order lineage. The brief does
  not supply its spelling; both ownership cases explicitly depend on this choice.
* Unit-one request scope is measured on main 4d83db19 with no buying orders and
  frozen below; the test also compares each scenario with its no-buy counterpart.

These assumptions are concentrated in the harness, not discovered by introspection.
A failed harness precondition is NOT evidence that a blocking rule is wrong.
All inputs, account numbers and order identities below are synthetic sentinels.
pytest's fixture/monkeypatch/capture docs were read at ae1d0ff57b8b, and the
Alpaca models and REST request boundary at c42add7c0a70, in the pinned local copies.
"""

from __future__ import annotations

import contextlib
import datetime as datetime_module
import io
import json
import logging
import os
import re
import runpy
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Self
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests

NOW = datetime(2026, 11, 13, 23, 26, tzinfo=UTC)
ACCOUNT_NUMBER = "BLIND-ACCOUNT-NUMBER-DO-NOT-EMIT-917263"
ACCOUNT_ID = "c3e2c710-a151-4530-a492-129753098abc"
ORDER_IDS = tuple(f"a54c1831-d931-4a59-86bf-{number:012d}" for number in range(1, 5))
CLIENT_IDS = (
    "blind-owner-buy-do-not-emit-921",
    "blind-owner-buy-do-not-emit-922",
    "blind-owner-buy-do-not-emit-923",
    "O-20261113-232600-001-001-1",
    "O-20261113-232600-001-001-2",
    "O-20261113-230000-001-001-3",
)
SYMBOL = "SPY"
PRICE = Decimal("100.00")
# SALE_REACH is the only monitor public name the brief gives. Use its stated
# DEFAULT here: 2%; do not read an implementation value to construct its own oracle.
SALE_REACH = Decimal("0.02")
SALE_PRICE = PRICE * (1 - SALE_REACH)
# Observed through the fake HTTP recorder on unit one at 4d83db19: four base
# GETs, plus one closed-orders GET for long holdings (omitted on a cached pass).
# Keeping this fixed also catches a future unconditional extra request that both
# a blocking case and its contemporaneous no-buy control would otherwise make.
UNIT_ONE_BASE_REQUESTS = Counter(
    {
        ("GET", "https://paper-api.alpaca.markets/v2/clock"): 1,
        ("GET", "https://paper-api.alpaca.markets/v2/positions"): 1,
        ("GET", "https://paper-api.alpaca.markets/v2/orders"): 1,
        ("GET", "https://paper-api.alpaca.markets/v2/calendar"): 1,
    }
)


def _normal(text: str) -> str:
    return " ".join(text.casefold().replace("_", " ").split())


def _reported_words(text: str) -> str:
    """Search reported values, not a counter's key such as ``uncovered: 0``."""

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [part for child in value.values() for part in strings(child)]
        if isinstance(value, list):
            return [part for child in value for part in strings(child)]
        return []

    try:
        return _normal(" ".join(strings(json.loads(text))))
    except ValueError:
        return _normal(text)


def _position(*, short: bool = False) -> dict[str, Any]:
    return {
        "asset_id": "b6a4e059-bab8-447d-b7bf-04f93a2902be",
        "symbol": SYMBOL,
        "exchange": "ARCA",
        "asset_class": "us_equity",
        "avg_entry_price": "99.00",
        "qty": "-1" if short else "1",
        "qty_available": "-1" if short else "1",
        "side": "short" if short else "long",
        "market_value": "-100.00" if short else "100.00",
        "cost_basis": "-99.00" if short else "99.00",
        "unrealized_pl": "-1.00" if short else "1.00",
        "unrealized_plpc": "0.010101",
        "unrealized_intraday_pl": "0",
        "unrealized_intraday_plpc": "0",
        "current_price": str(PRICE),
        "lastday_price": "100.00",
        "change_today": "0",
    }


def _order(
    *,
    number: int = 0,
    kind: str = "market",
    side: str = "buy",
    status: str = "accepted",
    symbol: str = SYMBOL,
    limit: str | None = None,
    ours: bool = False,
) -> dict[str, Any]:
    return {
        "id": ORDER_IDS[number],
        "client_order_id": CLIENT_IDS[4 if side == "sell" else 3] if ours else CLIENT_IDS[number],
        "asset_id": "b6a4e059-bab8-447d-b7bf-04f93a2902be",
        "symbol": symbol,
        "asset_class": "us_equity",
        "created_at": "2026-11-13T23:25:00Z",
        "updated_at": "2026-11-13T23:25:00Z",
        "submitted_at": "2026-11-13T23:25:00Z",
        "filled_at": None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
        "replaced_by": None,
        "replaces": None,
        "qty": "1",
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": "simple",
        "order_type": kind,
        "type": kind,
        "side": side,
        "time_in_force": "gtc",
        "limit_price": limit,
        "stop_price": "105.00" if kind == "stop" else None,
        "status": status,
        "extended_hours": False,
        "legs": None,
        "trail_percent": "5" if kind == "trailing_stop" else None,
        "trail_price": None,
        "hwm": "100.00" if kind == "trailing_stop" else None,
        "position_intent": "sell_to_close" if side == "sell" else "buy_to_open",
    }


class _Response(io.BytesIO):
    """One offline response, usable at the urllib or requests transport boundary."""

    def __init__(self, payload: object) -> None:
        self.content = json.dumps(payload).encode()
        super().__init__(self.content)
        self.status = self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.text = self.content.decode()

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        return None

    def getcode(self) -> int:
        return 200


class _Transport:
    def __init__(
        self, positions: list[dict[str, Any]], orders: list[dict[str, Any]], instant: datetime
    ) -> None:
        self.positions = positions
        self.orders = orders
        self.instant = instant
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        parsed = urlsplit(url)
        path = parsed.path
        self.calls.append((method.upper(), parsed._replace(query="", fragment="").geturl()))
        assert method.upper() == "GET", f"non-GET request attempted: {method} {path}"
        query = parse_qs(parsed.query)
        # NonEmptyRequest.to_request_fields at the pin leaves str/Enum values
        # intact; requests would encode their string value at the HTTP boundary.
        query.update(
            {
                key: [str(value.value if isinstance(value, Enum) else value)]
                for key, value in (kwargs.get("params") or {}).items()
            }
        )
        if path == "/v2/positions":
            payload: object = self.positions
        elif path == "/v2/orders":
            # A completed entry well before this pass makes the existing uncovered
            # finding old enough to alert in case 1; no private memory is seeded.
            entry = {
                **_order(ours=True, status="filled"),
                "id": ORDER_IDS[3],
                "client_order_id": CLIENT_IDS[5],
                "created_at": "2026-11-13T14:25:00Z",
                "updated_at": "2026-11-13T14:25:01Z",
                "submitted_at": "2026-11-13T14:25:00Z",
                "filled_qty": "1",
                "filled_avg_price": "99.00",
                "filled_at": "2026-11-13T14:25:01Z",
            }
            history = [entry] if self.positions and self.positions[0]["side"] == "long" else []
            if query.get("status") == ["closed"]:
                payload = history
            elif query.get("status") == ["all"]:
                payload = [*self.orders, *history]
            else:
                payload = self.orders
        elif path == "/v2/clock":
            payload = {
                "timestamp": self.instant.isoformat(),
                "is_open": 14 <= self.instant.hour < 21,
                "next_open": "2026-11-16T14:30:00Z",
                "next_close": (
                    "2026-11-13T21:00:00Z"
                    if 14 <= self.instant.hour < 21
                    else "2026-11-16T21:00:00Z"
                ),
            }
        elif path == "/v2/calendar":
            # alpaca-py c42add7c0a70 trading/models.py, Calendar: HHMM session
            # bounds, distinct from the regular session's HH:MM open and close.
            payload = [
                {
                    "date": "2026-11-13",
                    "open": "09:30",
                    "close": "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                }
            ]
        elif path == "/v2/account/configurations":
            payload = {"no_shorting": True, "dtbp_check": "both", "trade_confirm_email": "none"}
        elif path.startswith("/v2/account/activities"):
            payload = []
        elif path == "/v2/account":
            payload = {
                "id": ACCOUNT_ID,
                "account_number": ACCOUNT_NUMBER,
                "status": "ACTIVE",
                "currency": "USD",
                "cash": "10000",
                "buying_power": "10000",
                "regt_buying_power": "10000",
                "daytrading_buying_power": "0",
                "equity": "10100",
                "last_equity": "10100",
                "portfolio_value": "10100",
                "long_market_value": "100",
                "short_market_value": "0",
                "initial_margin": "0",
                "maintenance_margin": "0",
                "last_maintenance_margin": "0",
                "sma": "0",
                "daytrade_count": 0,
                "multiplier": "1",
                "pattern_day_trader": False,
                "trading_blocked": False,
                "transfers_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
                "shorting_enabled": False,
                "created_at": "2026-01-01T00:00:00Z",
            }
        else:
            raise AssertionError(f"unmodelled request, no network allowed: {method} {path}")
        return _Response(payload)

    def urlopen(self, request: Any, *args: Any, **kwargs: Any) -> _Response:
        if isinstance(request, str):
            return self.request("GET", request)
        return self.request(request.get_method(), request.full_url)


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("offline blind test forbids sockets, subprocesses and sleeping")


class _WriteGuard:
    """Only active during a monitor invocation; prevents writes outside its tmp home."""

    root: Path | None = None

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if self.root is None:
            return
        paths: tuple[Any, ...] = ()
        if event == "open":
            path, mode, flags = args
            writing = any(char in (mode or "") for char in "wax+")
            writing |= bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing:
                paths = (path,)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"}:
            paths = args[:1]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = args[:2]
        elif event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            _refuse()
        for path in paths:
            if isinstance(path, int):
                raise TypeError("monitor attempted a write through an unverified descriptor")
            destination = Path(os.fsdecode(path)).resolve()
            assert destination.is_relative_to(self.root), "monitor write escaped the temporary home"


_WRITE_GUARD = _WriteGuard()
sys.addaudithook(_WRITE_GUARD.audit)


def _records(value: object) -> list[str]:
    """Keep each JSON event together; do not mistake pretty-print lines for events."""
    if isinstance(value, list):
        return [record for child in value for record in _records(child)]
    if isinstance(value, dict):
        if "findings" in value:
            # ASSUMED public report fields, confirmed by the CLI's JSON output.
            # The envelope's summary, counters and alert copies are not extra lines.
            return [
                record
                for key, child in value.items()
                if key not in {"alerts", "summary", "status", "totals", "requests"}
                and isinstance(child, (list, dict))
                for record in _records(child)
            ]
        nested_rows = {
            key: child
            for key, child in value.items()
            if isinstance(child, list) and any(isinstance(row, dict) for row in child)
        }
        if nested_rows:
            # A holding's state and a list of blocking-order lines remain separate
            # records, including when a producer nests the latter in the former.
            own_fields = {key: child for key, child in value.items() if key not in nested_rows}
            return [
                *(_records(own_fields) if own_fields else []),
                *(record for child in nested_rows.values() for record in _records(child)),
            ]
        event_words = ("exit blocked", "not observed: may block", "covered", "uncovered", "cleared")
        if any(
            isinstance(child, str) and any(word in _normal(child) for word in event_words)
            for child in value.values()
        ):
            return [json.dumps(value, sort_keys=True)]
        collections = [
            child for child in value.values() if isinstance(child, (list, dict)) and child
        ]
        if collections:
            return [record for child in collections for record in _records(child)]
        return [json.dumps(value, sort_keys=True)]
    return [str(value)]


def _text_records(text: str) -> list[str]:
    try:
        return _records(json.loads(text))
    except ValueError:
        records = []
        for line in text.splitlines():
            if line.strip():
                try:
                    records.extend(_records(json.loads(line)))
                except ValueError:
                    records.append(line)
        return records


@dataclass
class _Pass:
    channels: dict[str, list[str]]
    notices: list[str]
    all_output: str
    calls: list[tuple[str, str]]
    problem: str | None

    def ran(self) -> _Pass:
        assert self.problem is None, (
            f"ASSUMED public CLI/harness did not run: {self.problem}\n{self.all_output}"
        )
        paths = {urlsplit(url).path for _, url in self.calls}
        assert {"/v2/positions", "/v2/orders"} <= paths, "no positions/orders read; vacuous result"
        assert self.channels, "no observable monitor output; cannot infer absence of findings"
        return self

    def matching(self, phrase: str, *, exclude: str = "") -> list[str]:
        # One event may be printed and journalled; count in one sink, never their sum.
        alternatives = [
            [
                row
                for row in rows
                if phrase in _reported_words(row)
                and (not exclude or exclude not in _reported_words(row))
            ]
            for name, rows in self.channels.items()
            if "outbox" not in name.casefold() and "memory" not in name.casefold()
        ]
        return max(alternatives, key=len, default=[])

    def blocked(self) -> list[str]:
        return self.matching("exit blocked", exclude="cleared")

    def blocked_notices(self) -> list[str]:
        return [notice for notice in self.notices if "exit blocked" in _reported_words(notice)]


class _Harness:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.home.mkdir()
        self.previous: dict[str, list[str]] = {}

    def run(
        self,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        *,
        instant: datetime = NOW,
    ) -> _Pass:
        transport = _Transport(positions, orders, instant)
        stdout, stderr, log = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(log)
        problem = None

        class DateTimeMeta(type):
            def __instancecheck__(cls, instance: object) -> bool:
                # Parsers may return the original datetime class. Freezing the
                # clock must not make those already valid datetimes fail isinstance.
                return isinstance(instance, datetime)

        class FrozenDateTime(datetime, metaclass=DateTimeMeta):
            @classmethod
            def now(cls, tz: Any = None) -> Self:
                return cls.fromtimestamp(instant.timestamp(), tz=tz)

        def expanduser(path: str) -> str:
            if path == "~":
                return str(self.home)
            if path.startswith("~/"):
                return str(self.home / path[2:])
            assert not path.startswith("~"), "another user's home is outside this offline test"
            return path

        def request(session: object, method: str, url: str, **kwargs: Any) -> _Response:
            return transport.request(method, url, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            # Replace the mapping without reading any real environment value. These
            # are fake inputs, never credentials or a keychain lookup.
            patch.setattr(
                os,
                "environ",
                {
                    "ALPACA_API_KEY": "offline-blind-placeholder",
                    "ALPACA_SECRET_KEY": "offline-blind-placeholder",
                },
            )
            patch.setattr(Path, "home", classmethod(lambda cls: self.home))
            patch.setattr(os.path, "expanduser", expanduser)
            patch.setattr(sys, "dont_write_bytecode", True)
            patch.setattr(sys, "argv", ["canon_trading.exit_monitor", "A"])
            patch.setattr(datetime_module, "datetime", FrozenDateTime)
            patch.setattr(time, "time", lambda: instant.timestamp())
            patch.setattr(time, "sleep", _refuse)
            patch.setattr(subprocess, "Popen", _refuse)
            patch.setattr(socket.socket, "connect", _refuse)
            patch.setattr(urllib.request, "urlopen", transport.urlopen)
            patch.setattr(requests.Session, "request", request)
            logging.getLogger().addHandler(handler)
            _WRITE_GUARD.root = self.home.resolve()
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    # ASSUMED name: standard module CLI, positional account as in
                    # AGENTS.md's paper:exit-monitor row; no private API is inspected.
                    runpy.run_module("canon_trading.exit_monitor", run_name="__main__")
            except SystemExit as error:
                if error.code not in (None, 0):
                    problem = f"SystemExit({error.code}): {stderr.getvalue().strip()}"
            except Exception as error:  # noqa: BLE001 -- preserve blindness, suppress source traceback
                # No traceback: a blind test run must not reveal implementation text.
                problem = f"{type(error).__name__}: {error}"
            finally:
                _WRITE_GUARD.root = None
                logging.getLogger().removeHandler(handler)

        streams = {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "logging": log.getvalue(),
        }
        files = {
            str(path.relative_to(self.home)): path.read_text()
            for path in self.home.rglob("*")
            if path.is_file()
        }
        channels = {name: _text_records(text) for name, text in streams.items() if text.strip()}
        notices = []
        for name, text in files.items():
            records = _text_records(text)
            remaining = Counter(self.previous.get(name, []))
            new = []
            for record in records:
                if remaining[record]:
                    remaining[record] -= 1
                else:
                    new.append(record)
            self.previous[name] = records
            channels[name] = new
            # Corrected by the owning lane after the blind run: the brief names no path for the
            # notices, and the monitor's outbox is `alerts.jsonl` (exit_monitor's header). The
            # "outbox in the path" assumption found no notice in a correct run (cases 01 and 08).
            if "outbox" in name.casefold() or Path(name).name == "alerts.jsonl":
                notices.extend(new)
        return _Pass(
            channels=channels,
            notices=notices,
            all_output="\n".join([*streams.values(), *files.values()]),
            calls=transport.calls,
            problem=problem,
        )


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> Iterator[None]:
    """Override the parent's credential-reading fixture; this file has no paper tests."""
    yield


@pytest.fixture(scope="module")
def observed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _Pass]:
    """Fixed brief examples, not generated load; case 12 can inspect all independently."""
    root = tmp_path_factory.mktemp("exit-monitor-blocked-blind")
    long = [_position()]
    buy = _order()
    trail = _order(number=2, side="sell", kind="trailing_stop", status="new", ours=True)
    examples = {
        "baseline-long": (long, []),
        "baseline-covered": (long, [trail]),
        "baseline-short": ([_position(short=True)], []),
        "baseline-flat": ([], []),
        "01": (long, [buy]),
        "02": (long, [buy, trail]),
        "03": (long, [_order(kind="limit", limit="101.00")]),
        "04-at": (long, [_order(kind="limit", limit=str(SALE_PRICE))]),
        "04-under": (long, [_order(kind="limit", limit=str(SALE_PRICE - Decimal("0.01")))]),
        "05": (long, [_order(kind="limit", limit="95.00")]),
        "06": (long, [_order(ours=True)]),
        "07": (long, [_order(kind="stop")]),
        "09": (long, [buy, _order(number=1, kind="limit", limit="101.00")]),
        "10": (long, [_order(symbol="QQQ")]),
        "11-short": ([_position(short=True)], [buy]),
        "11-flat": ([], [buy]),
    }
    results = {}
    for name, (positions, orders) in examples.items():
        # "Working" in case 2 means a regular-session trailing stop. A resting
        # trailing stop at the observation's post-market time would be dormant.
        instant = NOW.replace(hour=15) if name in {"02", "baseline-covered"} else NOW
        if name in {"02", "baseline-covered"}:
            orders = [
                {
                    **order,
                    "created_at": "2026-11-13T15:25:00Z",
                    "updated_at": "2026-11-13T15:25:00Z",
                    "submitted_at": "2026-11-13T15:25:00Z",
                }
                for order in orders
            ]
        results[name] = _Harness(root / name).run(positions, orders, instant=instant)
    episode = _Harness(root / "08")
    results["08-accepted"] = episode.run(long, [buy])
    # One microsecond is before the repeat interval; the identity is unchanged.
    results["08-new"] = episode.run(
        long, [_order(status="new")], instant=NOW + timedelta(microseconds=1)
    )
    # A canceled order is absent from the open-orders response, not an open order.
    results["08-canceled"] = episode.run(long, [], instant=NOW + timedelta(microseconds=2))
    return results


def test_case_01_foreign_accepted_market_buy_is_uncovered_and_blocked(observed) -> None:
    result = observed["01"].ran()
    assert result.matching("uncovered"), "the holding must retain uncovered state"
    blocked = result.blocked()
    assert len(blocked) == 1
    line = _normal(blocked[0])
    assert "market" in line and "buy" in line
    assert "owner" in line or "foreign" in line
    assert len(result.notices) == 2, "uncovered and exit blocked each need a notice"


def test_case_02_working_own_trailing_stop_is_covered_without_telling(observed) -> None:
    result = observed["02"].ran()
    assert any(re.search(r"\bcovered\b", _normal(row)) for row in result.matching("covered"))
    assert not result.blocked()
    assert not result.blocked_notices()


def test_case_03_foreign_limit_over_current_price_is_named_and_blocked(observed) -> None:
    blocked = observed["03"].ran().blocked()
    assert len(blocked) == 1
    assert "limit" in _normal(blocked[0])
    assert re.search(r"\b101(?:\.0+)?\b", blocked[0]), "the buy's limit must be named"


def test_case_04_limit_at_sale_reach_blocks_but_one_tick_under_is_not_named(observed) -> None:
    assert len(observed["04-at"].ran().blocked()) == 1
    under = observed["04-under"].ran()
    assert not under.blocked()
    assert not under.blocked_notices()
    assert not under.matching("not observed: may block")
    assert not under.matching("97.99"), "the below-threshold buy must not be named"


def test_case_05_limit_five_percent_below_current_price_has_no_telling(observed) -> None:
    result = observed["05"].ran()
    assert not result.blocked()
    assert not result.blocked_notices()
    assert not result.matching("not observed: may block")


def test_case_06_own_market_buy_blocks_and_is_labelled_ours(observed) -> None:
    blocked = observed["06"].ran().blocked()
    assert len(blocked) == 1
    assert "ours" in _normal(blocked[0])


def test_case_07_stop_buy_is_one_unobserved_line_and_not_a_block(observed) -> None:
    result = observed["07"].ran()
    assert len(result.matching("not observed: may block")) == 1
    assert not result.blocked()
    assert not result.blocked_notices()


def test_case_08_status_change_keeps_episode_then_cancellation_clears_once(observed) -> None:
    first = observed["08-accepted"].ran()
    assert len(first.blocked()) == 1
    assert len(first.blocked_notices()) == 1
    changed = observed["08-new"].ran()
    assert not changed.notices, "status alone must not start another notification episode"
    cleared = observed["08-canceled"].ran()
    lines = cleared.matching("cleared")
    assert len([line for line in lines if "exit blocked" in _normal(line)]) == 1
    assert not cleared.notices, "cleared is a line, not a notification"


def test_case_09_two_blocking_orders_each_get_one_line(observed) -> None:
    blocked = observed["09"].ran().blocked()
    assert len(blocked) == 2
    for kind in ("market", "limit"):
        assert (
            sum(
                bool(
                    re.search(
                        rf"\b{kind} buy\b|\bbuy {kind}\b|type[\"']?\s*[:=]\s*[\"']?{kind}\b",
                        _normal(line),
                    )
                )
                for line in blocked
            )
            == 1
        )


def test_case_10_buy_in_another_symbol_has_no_telling(observed) -> None:
    result = observed["10"].ran()
    assert not result.blocked()
    assert not result.blocked_notices()
    assert not result.matching("not observed: may block")


def test_case_11_short_and_flat_with_resting_buy_have_no_telling(observed) -> None:
    for name in ("11-short", "11-flat"):
        result = observed[name].ran()
        assert not result.blocked(), name
        assert not result.blocked_notices(), name
        assert not result.matching("not observed: may block"), name


def test_case_12_every_output_redacts_identities_and_all_requests_stay_get_only(observed) -> None:
    forbidden = (ACCOUNT_NUMBER, ACCOUNT_ID, *ORDER_IDS, *CLIENT_IDS)
    for name, result in observed.items():
        result.ran()  # An empty recorder or failed invocation must not pass this case.
        for identity in forbidden:
            assert identity not in result.all_output, f"{name} leaked a synthetic identity"
        assert result.calls and all(method == "GET" for method, _ in result.calls), name
        baseline = "baseline-long"
        if name == "02" or name == "baseline-covered":
            baseline = "baseline-covered"
        elif name.endswith("short"):
            baseline = "baseline-short"
        elif name.endswith("flat"):
            baseline = "baseline-flat"
        limits = UNIT_ONE_BASE_REQUESTS.copy()
        if baseline not in {"baseline-short", "baseline-flat"}:
            limits[("GET", "https://paper-api.alpaca.markets/v2/orders")] += 1
        assert Counter(result.calls) <= limits, f"{name}: request beyond unit one at 4d83db19"
        assert Counter(result.calls) <= Counter(observed[baseline].ran().calls), (
            f"{name}: extra requests beyond unit one's corresponding no-buy pass"
        )


def test_case_13_wash_trades_record_contains_observations_without_inventing_a_rule() -> None:
    readme = Path(__file__).resolve().parents[1] / "packages/canon-alpaca/README.md"
    text = readme.read_text()
    # Existing paragraph's public heading is named explicitly in the brief.
    match = re.search(r"(?ms)^\* \*\*Wash trades:\*\*(.*?)(?=^\* \*\*|\Z)", text)
    assert match is not None, "the Wash trades paragraph must remain present"
    paragraph = match.group(1)
    normalized = _normal(paragraph)
    assert "40310000" in paragraph
    assert "potential wash trade detected" in normalized
    table = []
    for line in paragraph.splitlines():
        if line.strip().startswith("|"):
            cells = [cell.strip().casefold() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 5 and all(re.search(r"accepted|refused", cell) for cell in cells[1:]):
                table.append(cells)
    assert len(table) == 4, "the four resting-order rows must be recorded"
    expected = (
        ("accepted", "accepted", "accepted", "accepted"),
        ("refused", "refused", "refused", "accepted"),
        ("accepted", "accepted", "accepted", "accepted"),
        ("refused", "refused", "refused", "accepted"),
    )
    assert [tuple(re.findall(r"accepted|refused", " ".join(row[1:]))) for row in table] == list(
        expected
    )
    assert "none" in table[0][0] or "baseline" in table[0][0]
    assert "market" in table[1][0] and "buy" in table[1][0]
    assert "limit" in table[2][0] and "buy" in table[2][0]
    assert "limit" in table[3][0] and "buy" in table[3][0]
    reverse = re.search(r"revers[^\n]*(?:\n(?!\n)[^\n]*)*", paragraph, re.IGNORECASE)
    assert reverse is not None, "the reversed experiment must be present"
    reversed_text = _normal(reverse.group())
    assert all(word in reversed_text for word in ("market", "limit", "buy", "accepted"))
    assert "observ" in normalized and re.search(r"differ|depart|discrepan", normalized)
    assert re.search(r"row\s*2.{0,40}(?:\(c\)|cell\s*c)", normalized) or re.search(
        r"market sell.{0,250}(?:resting|live).{0,80}limit buy", normalized
    ), "the accepted market sell beside the lower live limit buy must be identified"
    assert "not shown" in normalized
    assert "price test" not in normalized
    # A syntactic guard for the specific forbidden generalisation. Prose semantics
    # beyond these formulations still need the owning lane's evidence review.
    for sentence in re.split(r"[.!?](?:\s|$)", normalized):
        claims_price_rule = re.search(
            r"price[- ]based|price rather than (?:order )?type|price.{0,30}not (?:of )?type"
            r"|(?:venue|alpaca).{0,30}(?:uses|applies|performs).{0,20}price",
            sentence,
        )
        if claims_price_rule:
            assert re.search(
                r"inference|hypothesis|unverified|not (?:a |shown|established)", sentence
            ), "the record must not promote the price-vs-type inference to a rule"
