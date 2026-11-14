"""U-e: retain a holding's protection floor across bounded protector runs.

Contract: NautilusTrader d53849b216e2 crates/execution/src/trailing.rs:61-102 seeds
the existing trigger and only raises a sell's trigger. No rounding belongs in the maximum.
The level-book fields follow L1's U-e handoff (2026-11-13, Part 1). The holding key is the
account label and instrument id alone (schema 2, the lead's goal of 2026-11-13): at the pin a
reconciled EXTERNAL position opens at the reconciliation clock, not the venue's open
(crates/live/src/execution/manager.rs:4282-4330 passes ts_now; reconciliation.rs:955-1003 stamps
the synthetic report's ts_last with it; execution/src/reconciliation/orders.rs:1322,1348 gives the
inferred fill that ts_event; model/src/position.rs:232 sets ts_opened from it), so ts_opened names
no holding across runs. An entry keeps its own protection_start_ns, the time a run first saw the
holding, carried while the holding continues. Whether a book entry's holding is the one held now
is read from the venue's orders (`sold_since`, `bought_since`), never from NautilusTrader stamps.
An entry also keeps the trailing distance its level was armed with, written beside it (the
lead's ruling, 2026-11-13 17:43, point 1), and the sell's limit offset beside both (ruling (c),
17:56): a carried exit is level, distance and offset together. An entry written before either was
kept still reads. Fills are counted by fill time over a bounded walk of the venue's orders
(`holding_verdict`, review F1); a quantity the fills do not explain is unknown, never carried (F4).
The default book is one file per account label, and a writer's read, merge and replace runs under an
exclusive flock on a sibling lock file, the pattern of ledger._ledger_lock (ledger.py:351-359)
copied, since importing ledger would load Optuna into the node (review F5).
CPython b703b2c5e704 Doc/library/json.rst:260-269,356-371 supplies JSON text; writes
reuse rank_shared.write_atomically (os.rst:2827-2835: atomic same-filesystem replace).
The watch folder follows scripts/automation:62-63,102 (CLAUDE_CONFIG_DIR/canon/watch).
Overnight eligibility delegates to session_window.overnight_eligibility; this module
constructs no client, reads no credential, and owns no scheduler or node.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import InstrumentId

from . import rank_shared

type Key = tuple[str, InstrumentId]
"""(account label, instrument id). The instrument id is stored as its text on disk and parsed back
with InstrumentId.from_str; the protection start is the entry's own field."""
SCHEMA = 2
"""Schema 1 keyed a holding by its protection start too; it still reads, its start kept as the
entry's protection_start_ns."""
type Reporter = Callable[[str], None]


def _positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


def start_level(previous: Decimal | None, rebuilt: Decimal) -> Decimal:
    """The exact greater level; a missing previous level leaves the rebuilt one unchanged."""
    _positive(rebuilt, "rebuilt")
    if previous is None:
        return rebuilt
    _positive(previous, "previous")
    return max(previous, rebuilt)


STATES = ("armed", "guarded", "counting", "waiting", "frozen")
"""Item 4 (the lead, 2026-11-13 17:13): the book is the protector's node report. `armed` has a
working sell at its level; `guarded` holds the sale on the first-observation guard; `counting` is
n of 2 observations; `waiting` and `frozen` carry a reason. None is a book written before states."""
NEEDS_LEVEL = ("armed", "guarded", "counting")
NEEDS_REASON = ("waiting", "frozen")


@dataclass(frozen=True)
class Entry:
    """One holding's last level and state; Decimal values are serialized as text, never floats.

    A `waiting` or `frozen` holding may have no level yet; every other entry has one.
    """

    last_level: Decimal | None
    protection_start_ns: int
    quantity: Decimal
    written_ns: int
    run: str
    state: str | None = None
    count: int | None = None
    reason: str | None = None
    distance: Decimal | None = None
    """The 17:43 ruling: the trailing distance in dollars the level was armed with, written
    whenever the level is; None for an entry written before distances were kept."""
    limit_offset: Decimal | None = None
    """Ruling (c), 2026-11-13 17:56: the sell's limit offset in dollars below its trigger, written
    beside the distance; None for an entry written before offsets were kept."""


def _check_state(entry: Entry) -> None:
    state = entry.state
    if state is not None and state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}, not {state!r}")
    if entry.last_level is None:
        if state not in NEEDS_REASON:
            raise ValueError(f"a {state or 'stateless'} entry needs a level")
    else:
        _positive(entry.last_level, "last_level")
    if state == "counting":
        if type(entry.count) is not int or entry.count not in (0, 1):
            raise ValueError("a counting entry's count is 0 or 1 of the 2 observations")
    elif entry.count is not None:
        raise ValueError("only a counting entry has a count")
    if state in NEEDS_REASON or entry.reason is not None:
        _text(entry.reason, "reason")
    if state is None and entry.reason is not None:
        raise ValueError("a stateless entry has no reason")
    if entry.distance is not None:
        if entry.last_level is None:
            raise ValueError("a distance is kept only beside a level")
        _positive(entry.distance, "distance")
    if entry.limit_offset is not None:
        if entry.distance is None:
            raise ValueError("a limit offset is kept only beside a distance")
        _positive(entry.limit_offset, "limit_offset")


LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""An account label that can name a file: no separator, no leading dot."""


def book_path(account_label: str) -> Path:
    """One book per account label (review F5), resolved at use time so a test's
    CLAUDE_CONFIG_DIR never touches the owner's watch folder."""
    if not isinstance(account_label, str) or not LABEL.fullmatch(account_label):
        raise ValueError(f"account label {account_label!r} cannot name a level book file")
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(configured) if configured else Path.home() / ".claude"
    return home / "canon" / "watch" / f"owner-levels-{account_label}.json"


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


NS_MAX = 2**63 - 1
"""The greatest nanosecond stamp a book entry may carry, the int64 range Nautilus stamps fit
(UnixNanos); a larger one overflows `_moment`'s datetime (Codex's P1-b on 62941f39)."""


def _ns(value: Any, name: str) -> int:
    if type(value) is not int or not 0 <= value <= NS_MAX:
        raise ValueError(f"{name} must be integer nanoseconds from 0 to {NS_MAX}")
    return value


def _entry(row: Any) -> tuple[Key, Entry]:
    if not isinstance(row, dict):
        raise TypeError("entry must be an object")
    account = _text(row.get("account_label"), "account_label")
    instrument = InstrumentId.from_str(_text(row.get("instrument_id"), "instrument_id"))
    opened = _ns(row.get("protection_start_ns"), "protection_start_ns")
    raw_level = row.get("last_level")
    level = None if raw_level is None else Decimal(_text(raw_level, "last_level"))
    quantity = Decimal(_text(row.get("quantity"), "quantity"))
    _positive(quantity, "quantity")
    raw_distance = row.get("distance")
    distance = None if raw_distance is None else Decimal(_text(raw_distance, "distance"))
    raw_offset = row.get("limit_offset")
    offset = None if raw_offset is None else Decimal(_text(raw_offset, "limit_offset"))
    entry = Entry(
        level,
        opened,
        quantity,
        _ns(row.get("written_ns"), "written_ns"),
        _text(row.get("run"), "run"),
        row.get("state"),
        row.get("count"),
        row.get("reason"),
        distance,
        offset,
    )
    _check_state(entry)
    return (account, instrument), entry


class LevelBook:
    """An atomic snapshot, keyed by (account label, instrument id).

    Missing/malformed rows never prevent rebuilding other holdings. A caller should also name
    each holding with no matching previous entry, since a broken file may have no readable key.
    Writers sharing a book path serialize their read, merge and replace under `locked`; atomic
    replace alone is not a lock.
    """

    @staticmethod
    @contextmanager
    def locked(path: Path | str) -> Generator[None]:
        """The exclusive, blocking flock on `<book>.lock` beside the book (ledger.py:351-359)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / f"{path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def read(path: Path | str, *, report: Reporter | None = None) -> dict[Key, Entry]:
        report = report or logging.getLogger(__name__).warning
        try:
            data = json.loads(Path(path).read_text())
            if (
                not isinstance(data, dict)
                or type(data.get("schema")) is not int
                or data["schema"] not in (1, SCHEMA)
                or not isinstance(data.get("entries"), list)
            ):
                raise ValueError(f"expected level book schema 1 or {SCHEMA} and an entries array")
        except (OSError, ValueError) as exc:
            report(f"previous level book unavailable: {type(exc).__name__}: {exc}")
            return {}
        old = data["schema"] == 1
        if old and data["entries"]:
            report(
                "level book schema 1: each entry's protection start is kept as its "
                "protection_start_ns; holdings are now keyed by account and instrument"
            )
        entries: dict[Key, Entry] = {}
        duplicates: set[Key] = set()
        for index, row in enumerate(data["entries"]):
            try:
                key, entry = _entry(row)
                if old and key in entries:
                    # Schema 1 could hold one name under two starts; the newest write is kept.
                    newer, older = sorted((entries[key], entry), key=lambda e: -e.written_ns)
                    entries[key] = newer
                    raise ValueError(
                        f"schema 1 entry with protection start {older.protection_start_ns} "
                        "dropped: a newer entry names the same holding"
                    )
                if key in entries or key in duplicates:
                    entries.pop(key, None)
                    duplicates.add(key)
                    raise ValueError(f"duplicate holding {key}")
                entries[key] = entry
            except (ValueError, TypeError, ArithmeticError) as exc:
                label = (
                    row.get("instrument_id", "unknown holding")
                    if isinstance(row, dict)
                    else "unknown holding"
                )
                report(f"{label}: ALERT: previous level unavailable: entry {index}: {exc}")
        return entries

    @staticmethod
    def last_steps(path: Path | str) -> dict[str, int]:
        """Each account label's last node step (review F5). Empty for an unreadable book; a book
        written before steps were per account (one bare time) names no account, so is empty too."""
        try:
            data = json.loads(Path(path).read_text())
        except OSError, ValueError:
            return {}
        value = data.get("last_step_ns") if isinstance(data, dict) else None
        if not isinstance(value, dict):
            return {}
        return {
            label: ns
            for label, ns in value.items()
            if isinstance(label, str) and type(ns) is int and ns >= 0
        }

    @staticmethod
    def last_step_ns(path: Path | str, account_label: str) -> int | None:
        """One account's last node step; None when the book has none for it."""
        return LevelBook.last_steps(path).get(account_label)

    @staticmethod
    def write(
        path: Path | str,
        entries: Mapping[Key, Entry],
        *,
        last_step_ns: Mapping[str, int] | None = None,
    ) -> None:
        rows = []
        for (account, instrument), entry in sorted(
            entries.items(), key=lambda item: (item[0][0], str(item[0][1]))
        ):
            if not isinstance(instrument, InstrumentId):
                raise TypeError("a level book key's instrument must be an InstrumentId")
            _check_state(entry)
            _positive(entry.quantity, "quantity")
            row: dict[str, Any] = {
                "account_label": account,
                "instrument_id": str(instrument),
                "protection_start_ns": entry.protection_start_ns,
                "last_level": None if entry.last_level is None else str(entry.last_level),
                "quantity": str(entry.quantity),
                "written_ns": entry.written_ns,
                "run": entry.run,
            }
            for name in ("state", "count", "reason"):
                if getattr(entry, name) is not None:
                    row[name] = getattr(entry, name)
            if entry.distance is not None:
                row["distance"] = str(entry.distance)
            if entry.limit_offset is not None:
                row["limit_offset"] = str(entry.limit_offset)
            _entry(row)
            rows.append(row)
        path = Path(path)
        book: dict[str, Any] = {"schema": SCHEMA, "entries": rows}
        if last_step_ns:
            book["last_step_ns"] = {
                _text(label, "account label"): _ns(ns, "last_step_ns")
                for label, ns in sorted(last_step_ns.items())
            }
        body = json.dumps(book, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        rank_shared.write_atomically(path, body)


ORDERS_MOST = 500
"""GetOrdersRequest's documented maximum per response (alpaca-py c42add7c0a70
alpaca/trading/requests.py:210)."""
LOOKBACK = timedelta(days=92)
"""How far before the reference time the walk reads submissions: Alpaca's aged-order policy cancels a
GTC order 90 days after creation, plus two days for a cancel still pending, the bound main's
ended-orders walk records (#405, 02d6153d; custody.py's header, orders-at-alpaca, Time in Force).
An order submitted earlier than this cannot still be filling."""
PAGES_MOST = 4
"""Pages one walk reads at most (2000 orders of one side on one name); a walk that needs more is
unknown, never a partial count. It also bounds the reads' cost at a start (owner_protector header)."""
NEW, SAME, UNKNOWN = "new", "same", "unknown"
"""A book entry's holding against the one held now (holding_verdict)."""


class FillsUnknown(ValueError):
    """The fills since a time could not be established from the venue's orders."""


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _moment(at_ns: int) -> datetime:
    """Exact to the microsecond, Alpaca's finest step; a float would round the page boundary."""
    return EPOCH + timedelta(microseconds=at_ns // 1000)


def _iso(at_ns: int) -> str:
    return _moment(at_ns).isoformat()


def _stamp(value: Any, name: str) -> int:
    """An Alpaca timestamp as nanoseconds; one not shown, or without an offset, is unknown."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FillsUnknown(f"an order's {name} {value!r} is not a time") from exc
    else:
        raise FillsUnknown(f"an order's {name} is not shown")
    if moment.utcoffset() is None:
        raise FillsUnknown(f"an order's {name} {value!r} has no timezone")
    return (moment - EPOCH) // timedelta(microseconds=1) * 1000


def _key(order: dict[str, Any]) -> str:
    return str(order["id"]) if "id" in order else json.dumps(order, sort_keys=True, default=str)


def _orders_around(trading: Any, symbol: str, side: str, since_ns: int) -> list[dict[str, Any]]:
    """Every order of `side` on `symbol` submitted from LOOKBACK before `since_ns` on, any status.

    The request is alpaca-py's own GetOrdersRequest (alpaca-py c42add7c0a70
    alpaca/trading/requests.py:205-226: status, side, symbols, after, until, limit, direction), turned
    into the query TradingClient.get_orders sends (alpaca/trading/client.py:107-130:
    to_request_fields, symbols joined, GET /orders), through the runner's raw reader. `after` and
    `until` bound submission, not filling (requests.py:211-212), and get_orders does not paginate, so
    pages descend by submission as main's ended-orders walk does (#405/#415, custody.ended_orders):
    each full page's earliest submission plus one microsecond is the next `until`, overlapping the
    boundary; order IDs deduplicate the overlap, and a lost overlap, a page that cannot advance or
    more than PAGES_MOST pages is FillsUnknown, never a short count.
    """
    from alpaca.common.enums import Sort
    from alpaca.trading.enums import OrderSide
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    if trading is None:
        raise FillsUnknown("no venue order reader supplied")
    after = since_ns - int(LOOKBACK.total_seconds()) * 1_000_000_000
    until: int | None = None
    listed: dict[str, dict[str, Any]] = {}
    edge: set[str] = set()
    for _ in range(PAGES_MOST):
        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            side=OrderSide(side),
            symbols=[symbol],
            after=_moment(after),
            until=None if until is None else _moment(until),
            limit=ORDERS_MOST,
            direction=Sort.DESC,
        )
        params = request.to_request_fields()
        params["symbols"] = ",".join(params["symbols"])
        page = trading.get("/orders", params)
        if not isinstance(page, list) or not all(isinstance(o, dict) for o in page):
            raise FillsUnknown(f"orders answer is not a list of orders: {type(page).__name__}")
        keys = {_key(order) for order in page}
        if not edge <= keys:
            raise FillsUnknown("the orders pagination lost its overlapping boundary")
        for order in page:
            listed.setdefault(_key(order), order)
        if len(page) < ORDERS_MOST:
            return [
                o for o in listed.values() if o.get("symbol") == symbol and o.get("side") == side
            ]
        stamps = {_key(o): _stamp(o.get("submitted_at"), "submitted_at") for o in page}
        earliest = min(stamps.values())
        boundary = earliest + 1000  # One microsecond, the finest step Alpaca's times show.
        if until is not None and boundary >= until:
            raise FillsUnknown("the orders pagination cannot advance: one microsecond is full")
        edge = {key for key, stamp in stamps.items() if stamp == earliest}
        until = boundary
    raise FillsUnknown(
        f"more than {PAGES_MOST * ORDERS_MOST} {side} orders of {symbol} submitted since "
        f"{_iso(after)}"
    )


def _filled(order: dict[str, Any]) -> Decimal:
    value = Decimal(str(order.get("filled_qty") or "0"))
    if not value.is_finite() or value < 0:
        raise ValueError(f"order filled_qty {order.get('filled_qty')!r} is not a share count")
    return value


def _filled_since(trading: Any, symbol: str, side: str, since_ns: int) -> Decimal:
    """Shares of `symbol` filled on `side` after `since_ns`, counted by fill time (GET only).

    An order counts its whole filled_qty, a partial fill's included, when its filled_at
    (alpaca-py trading/models.py:227,270) is after `since_ns`; a filled quantity with no filled_at
    shown cannot be placed and is FillsUnknown. An order whose fills straddle `since_ns` counts all
    of them: an over-count, which errs toward no carry and no binding.
    """
    total = Decimal(0)
    for order in _orders_around(trading, symbol, side, since_ns):
        quantity = _filled(order)
        if quantity and _stamp(order.get("filled_at"), "filled_at") > since_ns:
            total += quantity
    return total


def sold_since(trading: Any, symbol: str, after_ns: int) -> Decimal:
    """Shares of `symbol` sold in fills after `after_ns` (GET only)."""
    return _filled_since(trading, symbol, "sell", after_ns)


def bought_since(trading: Any, symbol: str, after_ns: int) -> Decimal:
    """Shares of `symbol` bought in fills after `after_ns` (GET only)."""
    return _filled_since(trading, symbol, "buy", after_ns)


def holding_verdict(
    trading: Any, symbol: str, entry: Entry, held: Decimal | None = None
) -> tuple[str, str | None]:
    """Whether the book entry's holding is the one held now: (SAME, None), (NEW, why) or
    (UNKNOWN, why).

    Sold at least the entry's quantity since it was written: it may have gone flat, so NEW. Less:
    the account never went flat. With `held`, the quantity held now must be the entry's less the
    sales plus the buys since it was written; any other (a split, or a change the fills do not
    show) is UNKNOWN, by name. A failed or incomplete read is UNKNOWN.
    """
    written = _iso(entry.written_ns)
    try:
        sold = sold_since(trading, symbol, entry.written_ns)
        if sold >= entry.quantity:
            return NEW, f"a new holding: sold out since {written} ({sold} of {entry.quantity} sold)"
        if held is not None:
            bought = bought_since(trading, symbol, entry.written_ns)
            if held != entry.quantity - sold + bought:
                return UNKNOWN, (
                    f"quantity {held} is not the entry's {entry.quantity} less {sold} sold plus "
                    f"{bought} bought since {written}: a split or a change the fills do not show"
                )
    except Exception as exc:  # noqa: BLE001 - an unread venue never carries a level.
        return UNKNOWN, f"venue orders unread since {written} ({type(exc).__name__}: {exc})"
    return SAME, None


def same_holding(trading: Any, symbol: str, entry: Entry) -> str | None:
    """None if the book entry's holding is the one held now; else why its level is not carried."""
    return holding_verdict(trading, symbol, entry)[1]


def overnight_answer(trading: Any, symbol: str) -> tuple[str | None, bool]:
    """(None, True) if eligible; else a named reason and whether the asset ANSWERED it (review F7).

    An asset that could not be read is not an answer: session_window raises that WindowRefused
    from the read's failure (session_window.py:3936-3942: `from e`, or an answer that is not an
    asset), and any other exception is unreadable too. The caller retries an unanswered one.
    Import lazily: session_window owns the eligibility contract and imports strategy modules.
    The caller supplies the raw-GET client; a missing reader is an unreadable eligibility.
    """
    from .session_window import WindowRefused
    from .session_window import overnight_eligibility

    try:
        overnight_eligibility(trading, symbol)
    except WindowRefused as exc:
        answered = exc.__cause__ is None and "could not be read" not in str(exc)
        return f"uncoverable overnight: {exc}", answered
    except Exception as exc:  # noqa: BLE001 - unreadable eligibility must fail closed by name.
        return (
            f"uncoverable overnight: unreadable eligibility ({type(exc).__name__}: {exc})",
            False,
        )
    return None, True


def overnight_reason(trading: Any, symbol: str) -> str | None:
    """None if eligible; otherwise a named reason, including unreadable eligibility."""
    return overnight_answer(trading, symbol)[0]
