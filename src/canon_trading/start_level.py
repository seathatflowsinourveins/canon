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
is read from the venue's orders on the venue's clock alone (`holding_reading`, DeepSeek P2-2),
never from NautilusTrader stamps or the host's clock: the entry keeps the venue's reading of its
fills (`Horizon`, schema 3) and the orders the node applied, by a hash of their venue IDs.
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
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
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
SCHEMA = 3
"""Schema 1 keyed a holding by its protection start too; it still reads, its start kept as the
entry's protection_start_ns. Schema 3 adds each entry's venue reading (`Horizon`, the lead's
ruling on DeepSeek P2-2, 2026-11-13 21:23); a schema 1 or 2 entry still reads, with none."""
SCHEMAS_READ = (1, 2, SCHEMA)
"""Every schema a reader accepts: the book's own reader and the node's start (protector_node)
read through this one tuple, so the two never disagree on a format (the lead, 21:23: a book
the composed commit wrote is read, never refused)."""
type Reporter = Callable[[str], None]


@dataclass
class Unread:
    """What one read of the book could not use, for a writer to carry, never to replace (#443's
    max review F13; the lead, 2026-11-13 22:47: a row this build cannot read is carried through a
    write unchanged). `rows` holds each rejected row as it was on disk; `book` names why the whole
    file could not be read, and a writer then writes nothing over it."""

    rows: list[Any] = field(default_factory=list)
    book: str | None = None


def raw_key(row: Any) -> tuple[str, str] | None:
    """A rejected row's (account label, instrument id) as text, or None when either is not text.
    A row keyed by anything else (a list, say) names no holding this run can write, and its key
    must still hash, so one such row cannot stop every later write (land-sameclock review F1)."""
    if not isinstance(row, dict):
        return None
    account, instrument = row.get("account_label"), row.get("instrument_id")
    if not isinstance(account, str) or not isinstance(instrument, str):
        return None
    return (account, instrument)


class _Superseded(ValueError):
    """A schema 1 row dropped because a newer row names the same holding: never carried, since
    written back beside that newer entry it would make both a duplicate."""


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


STATES = ("armed", "guarded", "counting", "waiting", "frozen", "held")
"""Item 4 (the lead, 2026-11-13 17:13): the book is the protector's node report. `armed` has a
working sell at its level; `guarded` holds the sale on the first-observation guard; `counting` is
n of 2 observations; `waiting` and `frozen` carry a reason. None is a book written before states.
`held` is a holding the owner's word holds (symbol_hold.HELD): no sell of ours, its level kept as
written or as this run last raised it, never lowered, and a reason."""
NEEDS_LEVEL = ("armed", "guarded", "counting")
NEEDS_REASON = ("waiting", "frozen", "held")


@dataclass(frozen=True)
class Applied:
    """One order's shares the node had applied when the entry was written, matched to the
    venue's order by identity: its side, its venue legs (`leg_key` of each venue order ID the
    order had, replacements included) and its filled quantity."""

    side: str
    legs: tuple[str, ...]
    shares: Decimal


@dataclass(frozen=True)
class Horizon:
    """The venue's reading of a holding's fills, on the venue's clock alone (DeepSeek P2-2).

    `at_ns` is a venue stamp: Alpaca's clock (`GET /clock`, its `timestamp`, alpaca-py
    c42add7c0a70 trading/client.py:422-436, "the current market timestamp") read when the
    holding was found, or the newest order stamp a later same-holding reading showed. Every fill
    the entry's quantity holds is either on an `applied` order or on an order the venue last
    stamped at or before `at_ns`; any other order is a fill the entry does not hold.
    """

    at_ns: int
    applied: tuple[Applied, ...] = ()


def leg_key(venue_order_id: str) -> str:
    """A venue order ID as the book keeps it: 16 hex characters of its SHA-256, so the book
    matches the venue's orders without writing their IDs."""
    return hashlib.sha256(str(venue_order_id).encode()).hexdigest()[:16]


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
    horizon: Horizon | None = None
    """The venue's reading of the holding's fills (schema 3); None for an entry written before
    readings were kept, or one whose holding the venue could not read, which is never `same`."""


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


SIDES = ("buy", "sell")
LEG = re.compile(r"[0-9a-f]{16}")


def _horizon(row: dict[str, Any]) -> Horizon | None:
    if row.get("horizon_ns") is None:
        if row.get("applied") is not None:
            raise ValueError("applied orders are kept only beside a horizon_ns")
        return None
    at = _ns(row.get("horizon_ns"), "horizon_ns")
    raw = row.get("applied")
    if not isinstance(raw, list):
        raise TypeError("a horizon_ns needs an applied array")
    applied = []
    for item in raw:
        if not isinstance(item, dict) or item.get("side") not in SIDES:
            raise ValueError("an applied order needs a side, buy or sell")
        legs = item.get("legs")
        if (
            not isinstance(legs, list)
            or not legs
            or not all(isinstance(leg, str) and LEG.fullmatch(leg) for leg in legs)
        ):
            raise ValueError("an applied order needs its venue legs as 16 hex characters each")
        shares = Decimal(_text(item.get("shares"), "applied shares"))
        _positive(shares, "applied shares")
        applied.append(Applied(item["side"], tuple(legs), shares))
    return Horizon(at, tuple(applied))


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
        _horizon(row),
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
    def read(
        path: Path | str, *, report: Reporter | None = None, unread: Unread | None = None
    ) -> dict[Key, Entry]:
        """Every readable entry. With `unread`, each rejected row is collected there too, and a
        file that exists but cannot be read is named in `unread.book` for the writer to name,
        instead of through `report`."""
        report = report or logging.getLogger(__name__).warning
        try:
            data = json.loads(Path(path).read_text())
            if (
                not isinstance(data, dict)
                or type(data.get("schema")) is not int
                or data["schema"] not in SCHEMAS_READ
                or not isinstance(data.get("entries"), list)
            ):
                raise ValueError(
                    f"expected level book schema {', '.join(map(str, SCHEMAS_READ))} and an "
                    "entries array"
                )
        except FileNotFoundError as exc:
            report(f"previous level book unavailable: {type(exc).__name__}: {exc}")
            return {}
        except (OSError, ValueError) as exc:
            if unread is not None:
                unread.book = f"{type(exc).__name__}: {exc}"
                return {}
            # A book that exists but cannot be read costs every carried level: never at INFO.
            report(f"ALERT: previous level book unreadable: {type(exc).__name__}: {exc}")
            return {}
        old = data["schema"] == 1
        if data["schema"] < SCHEMA and data["entries"]:
            report(
                f"level book schema {data['schema']}: written before venue readings were kept; "
                "an entry without one is never the same holding, is named, and is replaced at "
                f"the next save, in schema {SCHEMA}"
            )
        if old and data["entries"]:
            report(
                "level book schema 1: each entry's protection start is kept as its "
                "protection_start_ns; holdings are now keyed by account and instrument"
            )
        entries: dict[Key, Entry] = {}
        duplicates: set[Key] = set()
        first: dict[Key, tuple[int, Any]] = {}

        def rejected(index: int, row: Any, why: object) -> None:
            # The lead's ruling on land-composed's F4 (2026-11-13 20:45): every row this read
            # rejects costs that holding its carried level only, with an ALERT carrying the row.
            # F13: a rejected row is carried through a write unchanged, a schema 1 row too
            # (land-sameclock review F7), except one a newer row of the same holding superseded.
            if unread is not None and not isinstance(why, _Superseded):
                unread.rows.append(row)
            label = (
                row.get("instrument_id", "unknown holding")
                if isinstance(row, dict)
                else "unknown holding"
            )
            fields = json.dumps(row, sort_keys=True, default=str)
            report(
                f"{label}: ALERT: previous level unavailable: entry {index}: {why}; "
                f"rejected row {fields[:400]}; armed as a holding with no carried level"
            )

        for index, row in enumerate(data["entries"]):
            try:
                key, entry = _entry(row)
                if old and key in entries:
                    # Schema 1 could hold one name under two starts; the newest write is kept.
                    newer, older = sorted((entries[key], entry), key=lambda e: -e.written_ns)
                    entries[key] = newer
                    raise _Superseded(
                        f"schema 1 entry with protection start {older.protection_start_ns} "
                        "dropped: a newer entry names the same holding"
                    )
                if key in entries or key in duplicates:
                    if entries.pop(key, None) is not None:
                        rejected(*first[key], f"duplicate holding {key}")
                    duplicates.add(key)
                    raise ValueError(f"duplicate holding {key}")
                entries[key] = entry
                first[key] = (index, row)
            except (ValueError, TypeError, ArithmeticError) as exc:
                rejected(index, row, exc)
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
        carried: Sequence[Any] = (),
    ) -> None:
        """`carried` rows are rows a read rejected (`Unread.rows`), written back as they were read,
        after the entries."""
        rows: list[Any] = []
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
            if entry.horizon is not None:
                row["horizon_ns"] = entry.horizon.at_ns
                row["applied"] = [
                    {"side": a.side, "legs": list(a.legs), "shares": str(a.shares)}
                    for a in entry.horizon.applied
                ]
            _entry(row)
            rows.append(row)
        rows.extend(carried)
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


EDGE = timedelta(days=1)
"""How far before a reading's horizon an order the venue stamped is kept by identity in the
reading: such an order may still fill after it, and its later fills are counted exactly."""
APPLIED_MOST = 200
"""Orders one entry keeps by identity at most; a reading that needs more has no horizon."""


@dataclass(frozen=True)
class Reading:
    """A same-holding verdict, with on `same` the venue's reading this run's entries carry."""

    kind: str
    reason: str | None
    horizon: Horizon | None = None


def _venue_id(order: dict[str, Any]) -> str:
    if not order.get("id"):
        raise FillsUnknown("an order shows no id")
    return str(order["id"])


def _newest(order: dict[str, Any]) -> int:
    """An order's newest venue stamp: its fill time and, if shown, its last update."""
    stamps = [_stamp(order.get("filled_at"), "filled_at")]
    if order.get("updated_at") is not None:
        stamps.append(_stamp(order["updated_at"], "updated_at"))
    return max(stamps)


def _unapplied(
    trading: Any, symbol: str, side: str, horizon: Horizon
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Shares on `side` the venue shows filled that the entry's quantity does not hold.

    An order the entry kept (`Applied`) is matched by its venue legs: the venue's filled
    quantity over the legs listed, less the shares applied; fewer at the venue than applied is
    unknown. An applied order none of whose legs the venue lists is no venue order (the pin's
    position-reconciliation order, crates/execution/src/reconciliation/ids.rs:124-154, or one
    older than the walk, which cannot still be filling) and adds nothing. Any other order counts
    its whole filled quantity when the venue last stamped it after the horizon. Venue stamps
    on both sides: the host's clock decides nothing (DeepSeek P2-2).
    """
    listed = _orders_around(trading, symbol, side, horizon.at_ns)
    by_leg = {leg_key(_venue_id(order)): order for order in listed}
    kept: set[str] = set()
    total = Decimal(0)
    for applied in horizon.applied:
        if applied.side != side:
            continue
        kept.update(applied.legs)
        present = [by_leg[leg] for leg in applied.legs if leg in by_leg]
        if not present:
            continue
        venue = sum((_filled(order) for order in present), Decimal(0))
        if venue < applied.shares:
            raise FillsUnknown(
                f"the venue shows {venue} filled on an order of which {applied.shares} were applied"
            )
        total += venue - applied.shares
    for order in listed:
        quantity = _filled(order)
        if quantity and leg_key(_venue_id(order)) not in kept and _newest(order) > horizon.at_ns:
            total += quantity
    return total, listed


def _reading_of(at_ns: int, listed: list[dict[str, Any]]) -> Horizon:
    """The horizon a complete walk of both sides gives: the newest venue stamp it showed (or the
    reading's own), and every order with fills the venue stamped within EDGE of it, by identity."""
    stamps = [_newest(order) for order in listed if _filled(order)]
    at = max([at_ns, *stamps])
    edge = at - int(EDGE.total_seconds()) * 1_000_000_000
    applied = tuple(
        Applied(str(order["side"]), (leg_key(_venue_id(order)),), _filled(order))
        for order in listed
        if _filled(order) and _newest(order) > edge
    )
    if len(applied) > APPLIED_MOST:
        raise FillsUnknown(f"more than {APPLIED_MOST} recent orders to keep by identity")
    return Horizon(at, applied)


def holding_reading(
    trading: Any, symbol: str, entry: Entry, held: Decimal | None = None
) -> Reading:
    """Whether the book entry's holding is the one held now, on the venue's clock alone.

    No venue reading in the entry (schema 1 or 2, or a holding the venue could not read): unknown,
    by name. Otherwise the fills the entry does not hold are counted (`_unapplied`). Sold at
    least the entry's quantity: it may have gone flat, so NEW. With `held`, the quantity held now
    must be the entry's less those sales plus those buys; any other (a split, or a change the
    fills do not show) is UNKNOWN, by name. A failed or incomplete read is UNKNOWN. On SAME with
    `held`, the walk's own reading is the horizon this run's entries carry.
    """
    horizon = entry.horizon
    if horizon is None:
        return Reading(
            UNKNOWN,
            "the entry holds no venue reading of its fills (written before readings were kept, "
            "or the venue could not be read when it was), so it is never the same holding",
        )
    since = _iso(horizon.at_ns)
    try:
        sold, sells = _unapplied(trading, symbol, "sell", horizon)
        if sold >= entry.quantity:
            return Reading(
                NEW,
                f"a new holding: sold out since the venue's {since} ({sold} of "
                f"{entry.quantity} sold)",
            )
        if held is None:
            return Reading(SAME, None)
        bought, buys = _unapplied(trading, symbol, "buy", horizon)
        if held != entry.quantity - sold + bought:
            return Reading(
                UNKNOWN,
                f"quantity {held} is not the entry's {entry.quantity} less {sold} sold plus "
                f"{bought} bought since the venue's {since}: a split or a change the fills do "
                "not show",
            )
        return Reading(SAME, None, _reading_of(horizon.at_ns, [*sells, *buys]))
    except Exception as exc:  # noqa: BLE001 - an unread venue never carries a level.
        return Reading(
            UNKNOWN, f"venue orders unread since the venue's {since} ({type(exc).__name__}: {exc})"
        )


def holding_verdict(
    trading: Any, symbol: str, entry: Entry, held: Decimal | None = None
) -> tuple[str, str | None]:
    """`holding_reading` as (kind, reason): (SAME, None), (NEW, why) or (UNKNOWN, why)."""
    reading = holding_reading(trading, symbol, entry, held)
    return reading.kind, reading.reason


def baseline(trading: Any, symbol: str, held: Decimal) -> Horizon:
    """A found holding's first venue reading (DeepSeek P2-2), GET only.

    Alpaca's clock first (the reading's venue stamp), then both sides' orders up to it and past
    it, then the position. The node's cache does not change inside one handler, so a venue
    quantity equal to `held` after the walk means every fill the walk showed is in `held`; any
    other quantity, a failed read or a position Alpaca does not show is FillsUnknown, and the
    holding has no reading yet.
    """
    if trading is None:
        raise FillsUnknown("no venue order reader supplied")
    clock = trading.get("/clock")
    at = _stamp(clock.get("timestamp") if isinstance(clock, dict) else None, "clock timestamp")
    listed = [
        *_orders_around(trading, symbol, "sell", at),
        *_orders_around(trading, symbol, "buy", at),
    ]
    position = trading.get(f"/positions/{symbol}")
    if not isinstance(position, dict):
        raise FillsUnknown(f"position answer is not a position: {type(position).__name__}")
    venue = Decimal(str(position.get("qty")))
    if venue != held:
        raise FillsUnknown(f"the venue holds {venue} and the node {held}")
    return _reading_of(at, listed)


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
