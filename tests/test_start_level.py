"""U-e: exact restart floors and atomic, per-holding state, in pytest's temp tree only."""

from __future__ import annotations

import importlib
import json
from decimal import Decimal
from decimal import Inexact
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.model import InstrumentId

SHAPE = InstrumentId.from_str("SHAPE.ALPACA")
OTHER = InstrumentId.from_str("OTHER.ALPACA")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for offline level-book cases."""


@pytest.fixture(autouse=True)
def isolated_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


@pytest.fixture
def subject():
    return importlib.import_module("canon_trading.start_level")


@pytest.mark.parametrize(
    "previous,rebuilt,want",
    [
        (None, "1.02", "1.02"),
        ("1.10", "1.02", "1.10"),
        ("1.02", "1.10", "1.10"),
        ("1.10", "1.10", "1.10"),
        ("1.10512345678901234567890123456789", "1.02", "1.10512345678901234567890123456789"),
        ("0.0000000000001", "0.00000000000011", "0.00000000000011"),
        ("1E+100", "1.02", "1E+100"),
    ],
)
def test_start_level_is_exact_maximum(subject, previous, rebuilt, want) -> None:
    before = None if previous is None else Decimal(previous)
    with localcontext(prec=3) as context:
        context.traps[Inexact] = True
        assert subject.start_level(before, Decimal(rebuilt)) == Decimal(want)


@pytest.mark.parametrize("field", ["previous", "rebuilt"])
@pytest.mark.parametrize(
    "invalid",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal(-1), Decimal(0), "1.10", 1.10],
)
def test_bad_levels_are_not_used(subject, field, invalid) -> None:
    values = {"previous": Decimal("1.10"), "rebuilt": Decimal("1.02"), field: invalid}
    with pytest.raises((ValueError, TypeError), match=field):
        subject.start_level(**values)


def entry(subject, **changes):
    values = {
        "last_level": Decimal("1.105"),
        "protection_start_ns": 100,
        "quantity": Decimal(10),
        "written_ns": 200,
        "run": "shape-first",
    }
    # DeepSeek P2-2: an entry carries the venue's reading of its fills, here at its write;
    # horizon=None is an entry written before readings were kept, never the same holding.
    values.setdefault("horizon", subject.Horizon(values["written_ns"]))
    values.update(changes)
    if "written_ns" in changes and "horizon" not in changes:
        values["horizon"] = subject.Horizon(changes["written_ns"])
    return subject.Entry(**values)


def test_book_roundtrip_keeps_all_keys_and_exact_decimal_text(subject, tmp_path: Path) -> None:
    path = tmp_path / "watch" / "levels.json"
    entries = {
        ("shape", SHAPE): entry(subject),
        ("other", SHAPE): entry(subject, last_level=Decimal("1.20")),
        ("shape", OTHER): entry(subject, quantity=Decimal("7.5")),
        ("third", SHAPE): entry(subject, protection_start_ns=300),
    }
    subject.LevelBook.write(path, entries)
    assert subject.LevelBook.read(path) == entries
    assert all(isinstance(key[1], InstrumentId) for key in subject.LevelBook.read(path))
    data = json.loads(path.read_text())
    assert data["schema"] == subject.SCHEMA == 3
    assert [row["protection_start_ns"] for row in data["entries"]] == [100, 100, 100, 300]
    assert all(isinstance(row["last_level"], str) for row in data["entries"])
    assert "1.105" in [row["last_level"] for row in data["entries"]]


def test_book_refuses_an_instrument_key_given_as_text(subject, tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    with pytest.raises(TypeError, match="InstrumentId"):
        subject.LevelBook.write(path, {("shape", "SHAPE.ALPACA"): entry(subject)})
    assert not path.exists()


def test_default_book_is_under_configured_watch_folder(subject, tmp_path: Path) -> None:
    # Review F5: one default book per account label.
    assert subject.book_path("shape") == tmp_path / "canon" / "watch" / "owner-levels-shape.json"
    assert subject.book_path("other-2") != subject.book_path("shape")


@pytest.mark.parametrize("label", ["", "../shape", "shape/other", ".hidden", "a b", None])
def test_a_label_that_cannot_name_a_book_file_is_refused(subject, label) -> None:
    with pytest.raises(ValueError, match="cannot name a level book file"):
        subject.book_path(label)


@pytest.mark.parametrize(
    "body",
    [None, "{", "[]", '{"schema": 4, "entries": []}', '{"schema": 1, "entries": {}}'],
)
def test_missing_or_malformed_book_returns_no_previous_entries(subject, tmp_path: Path, body):
    path = tmp_path / "levels.json"
    if body is not None:
        path.write_text(body)
    notes: list[str] = []
    assert subject.LevelBook.read(path, report=notes.append) == {}
    assert notes


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_level", 1.1),
        ("last_level", "NaN"),
        ("last_level", "0"),
        ("quantity", 10),
        ("quantity", "Infinity"),
        ("quantity", "-1"),
        ("protection_start_ns", True),
        ("protection_start_ns", "100"),
        ("written_ns", -1),
        ("written_ns", 1.5),
        ("run", None),
        ("account_label", None),
        ("instrument_id", []),
        ("instrument_id", "no-venue"),
        ("distance", 0.04),
        ("distance", "0"),
        ("distance", "-0.04"),
        ("distance", "NaN"),
    ],
)
def test_wrong_type_row_does_not_discard_other_holdings(subject, tmp_path: Path, field, value):
    path = tmp_path / "levels.json"
    good = {
        "account_label": "shape",
        "instrument_id": "SHAPE.ALPACA",
        "protection_start_ns": 100,
        "last_level": "1.10",
        "quantity": "10",
        "written_ns": 200,
        "run": "shape-first",
    }
    bad = dict(good, instrument_id="OTHER.ALPACA")
    bad[field] = value
    path.write_text(json.dumps({"schema": 1, "entries": [good, bad]}))
    notes: list[str] = []
    rows = subject.LevelBook.read(path, report=notes.append)
    assert list(rows) == [("shape", SHAPE)]
    assert any("previous level unavailable: entry 1:" in note for note in notes), notes


def test_failed_atomic_replace_preserves_the_entire_old_book(subject, tmp_path, monkeypatch):
    from canon_trading import rank_shared

    path = tmp_path / "levels.json"
    key = ("shape", SHAPE)
    subject.LevelBook.write(path, {key: entry(subject)})
    original = path.read_bytes()

    def killed(source, destination):
        assert Path(destination) == path
        assert Path(source).parent == path.parent
        raise OSError("shaped interruption before replace")

    monkeypatch.setattr(rank_shared.os, "replace", killed)
    with pytest.raises(OSError, match="shaped interruption"):
        subject.LevelBook.write(path, {key: entry(subject, last_level=Decimal("1.20"))})
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "asset,want",
    [
        ({"overnight_tradable": True}, None),
        ({"attributes": ["overnight_tradable"]}, None),
        ({"overnight_tradable": False}, "overnight_tradable"),
        ({"attributes": ["overnight_tradable", "overnight_halted"]}, "overnight_halted"),
        ({"attributes": 3}, "unreadable"),
    ],
)
def test_overnight_reason_reuses_the_existing_eligibility_contract(subject, asset, want):
    client = SimpleNamespace(get=lambda _: asset)
    reason = subject.overnight_reason(client, "SHAPE")
    if want is None:
        assert reason is None
    else:
        assert "uncoverable overnight:" in reason
        assert want in reason


def test_unreadable_overnight_asset_names_failure(subject):
    def failed(_):
        raise OSError("shape asset unavailable")

    reason = subject.overnight_reason(SimpleNamespace(get=failed), "SHAPE")
    assert "uncoverable overnight:" in reason and "could not be read" in reason


def test_an_old_format_book_reads_with_unknown_state_and_no_step(subject, tmp_path: Path) -> None:
    """Item 4: a book written before `state` and `last_step_ns` existed still reads."""
    path = tmp_path / "levels.json"
    row = {
        "account_label": "shape",
        "instrument_id": "SHAPE.ALPACA",
        "protection_start_ns": 100,
        "last_level": "1.10",
        "quantity": "10",
        "written_ns": 200,
        "run": "shape-first",
    }
    path.write_text(json.dumps({"schema": 1, "entries": [row]}))
    notes: list[str] = []
    rows = subject.LevelBook.read(path, report=notes.append)
    assert rows[("shape", SHAPE)].last_level == Decimal("1.10")
    assert rows[("shape", SHAPE)].protection_start_ns == 100
    assert rows[("shape", SHAPE)].state is None
    assert subject.LevelBook.last_step_ns(path, "shape") is None
    assert notes == [
        (
            "level book schema 1: written before venue readings were kept; an entry without one "
            "is never the same holding, is named, and is replaced at the next save, in schema 3"
        ),
        (
            "level book schema 1: each entry's protection start is kept as its "
            "protection_start_ns; holdings are now keyed by account and instrument"
        ),
    ]
    assert rows[("shape", SHAPE)].horizon is None


@pytest.mark.parametrize(
    "state,count,reason,level",
    [
        ("armed", None, None, "1.10"),
        ("guarded", None, "the first-observation guard holds the sale", "1.10"),
        ("counting", 1, None, "1.10"),
        ("waiting", None, "no quote to plan protection", None),
        ("frozen", None, "sell belongs to Other-001", None),
    ],
)
def test_book_round_trips_state_and_last_step(subject, tmp_path, state, count, reason, level):
    path = tmp_path / "levels.json"
    key = ("shape", SHAPE)
    made = entry(
        subject,
        last_level=None if level is None else Decimal(level),
        state=state,
        count=count,
        reason=reason,
    )
    subject.LevelBook.write(path, {key: made}, last_step_ns={"shape": 300})
    assert subject.LevelBook.read(path) == {key: made}
    assert subject.LevelBook.last_step_ns(path, "shape") == 300


def test_last_step_is_kept_per_account_label(subject, tmp_path: Path) -> None:
    """Review F5: two accounts sharing a book keep a step each; a bare step written before steps
    were per account names no account."""
    path = tmp_path / "levels.json"
    subject.LevelBook.write(path, {}, last_step_ns={"shape": 300, "other": 500})
    assert subject.LevelBook.last_step_ns(path, "shape") == 300
    assert subject.LevelBook.last_step_ns(path, "other") == 500
    assert subject.LevelBook.last_step_ns(path, "third") is None
    path.write_text(json.dumps({"schema": 2, "entries": [], "last_step_ns": 300}))
    assert subject.LevelBook.last_step_ns(path, "shape") is None


def test_the_book_lock_is_an_exclusive_flock_beside_the_book(subject, tmp_path: Path) -> None:
    """Review F5: `locked` holds an exclusive flock on `<book>.lock` (ledger.py:351-359's form);
    a second open file description cannot take it until it is released."""
    import fcntl

    path = tmp_path / "watch" / "levels.json"
    with (
        subject.LevelBook.locked(path),
        (path.parent / "levels.json.lock").open("a") as other,
        pytest.raises(BlockingIOError),
    ):
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (path.parent / "levels.json.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "sleeping"},
        {"state": "counting", "count": 3},
        {"state": "armed", "last_level": None},
        {"state": None, "last_level": None},
    ],
)
def test_book_refuses_an_unknown_or_incomplete_state(subject, tmp_path, changes) -> None:
    with pytest.raises(ValueError):
        subject.LevelBook.write(
            tmp_path / "levels.json", {("shape", SHAPE): entry(subject, **changes)}
        )


def test_book_round_trips_the_distance_beside_the_level(subject, tmp_path: Path) -> None:
    """The lead's ruling, 2026-11-13 17:43, point 1: each entry keeps the distance it was armed
    with, as exact Decimal text beside last_level."""
    path = tmp_path / "levels.json"
    key = ("shape", SHAPE)
    made = entry(subject, last_level=Decimal("0.7926"), distance=Decimal("0.0400"))
    subject.LevelBook.write(path, {key: made})
    row = json.loads(path.read_text())["entries"][0]
    assert (row["last_level"], row["distance"]) == ("0.7926", "0.0400")
    assert subject.LevelBook.read(path) == {key: made}


def test_an_entry_without_a_distance_still_reads_and_writes_none(subject, tmp_path: Path) -> None:
    """The 17:43 ruling: an entry written before distances were kept reads with distance None."""
    path = tmp_path / "levels.json"
    key = ("shape", SHAPE)
    subject.LevelBook.write(path, {key: entry(subject)})
    assert "distance" not in json.loads(path.read_text())["entries"][0]
    assert subject.LevelBook.read(path)[key].distance is None


@pytest.mark.parametrize(
    "changes",
    [
        {"distance": Decimal(0)},
        {"distance": Decimal("NaN")},
        {"distance": "0.04"},
        {"distance": Decimal("0.04"), "last_level": None, "state": "waiting", "reason": "r"},
    ],
    ids=["zero", "nan", "text", "no-level"],
)
def test_book_refuses_a_distance_that_is_not_a_positive_level_distance(
    subject, tmp_path, changes
) -> None:
    with pytest.raises((ValueError, TypeError)):
        subject.LevelBook.write(
            tmp_path / "levels.json", {("shape", SHAPE): entry(subject, **changes)}
        )


def test_book_round_trips_the_limit_offset_beside_the_distance(subject, tmp_path: Path) -> None:
    """Ruling (c), 2026-11-13 17:56: the book carries the limit offset beside level and distance;
    an entry written before offsets were kept reads with None."""
    path = tmp_path / "levels.json"
    key = ("shape", SHAPE)
    made = entry(
        subject,
        last_level=Decimal("0.7926"),
        distance=Decimal("0.0400"),
        limit_offset=Decimal("0.0100"),
    )
    subject.LevelBook.write(path, {key: made})
    row = json.loads(path.read_text())["entries"][0]
    assert (row["distance"], row["limit_offset"]) == ("0.0400", "0.0100")
    assert subject.LevelBook.read(path) == {key: made}
    subject.LevelBook.write(path, {key: entry(subject, distance=Decimal("0.04"))})
    assert "limit_offset" not in json.loads(path.read_text())["entries"][0]
    assert subject.LevelBook.read(path)[key].limit_offset is None


@pytest.mark.parametrize(
    "changes",
    [
        {"distance": Decimal("0.04"), "limit_offset": Decimal(0)},
        {"distance": Decimal("0.04"), "limit_offset": Decimal("NaN")},
        {"limit_offset": Decimal("0.01")},
    ],
    ids=["zero", "nan", "no-distance"],
)
def test_book_refuses_a_bad_limit_offset(subject, tmp_path, changes) -> None:
    with pytest.raises(ValueError):
        subject.LevelBook.write(
            tmp_path / "levels.json", {("shape", SHAPE): entry(subject, **changes)}
        )


def test_a_schema_1_book_naming_one_holding_twice_keeps_the_newest_by_name(subject, tmp_path):
    """U-e A: schema 1 keyed by protection start; under (account, instrument) the newest wins."""
    path = tmp_path / "levels.json"
    row = {
        "account_label": "shape",
        "instrument_id": "SHAPE.ALPACA",
        "quantity": "10",
        "run": "shape-first",
    }
    older = dict(row, protection_start_ns=100, last_level="1.30", written_ns=200)
    newer = dict(row, protection_start_ns=300, last_level="1.10", written_ns=400)
    path.write_text(json.dumps({"schema": 1, "entries": [newer, older]}))
    notes: list[str] = []
    rows = subject.LevelBook.read(path, report=notes.append)
    assert rows == {
        ("shape", SHAPE): entry(
            subject,
            last_level=Decimal("1.10"),
            protection_start_ns=300,
            written_ns=400,
            horizon=None,
        )
    }
    assert any("protection start 100 dropped" in note for note in notes), notes


WRITTEN = 1_762_790_400_000_000_000
SECOND = 1_000_000_000
DAY = 86_400 * SECOND
_ids = iter(range(1, 10**9))


def stamp(ns: int) -> str:
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)).isoformat()


def moment(text: str) -> int:
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    return (
        (datetime.fromisoformat(text) - datetime(1970, 1, 1, tzinfo=UTC))
        // timedelta(microseconds=1)
        * 1000
    )


class Orders:
    """GET /orders as Alpaca answers it (review F9): `side`, `symbols`, and `after` and `until`
    bounding SUBMISSION, both exclusive (alpaca-py c42add7c0a70 requests.py:211-212); `direction`
    orders by submission and `limit` caps the page. No network."""

    def __init__(self, orders=None, failure=None, until_ignored=False):
        self.orders, self.failure, self.reads = list(orders or []), failure, []
        self.until_ignored = until_ignored

    def get(self, route, params=None):
        assert params is not None
        self.reads.append((route, dict(params)))
        if self.failure is not None:
            raise self.failure
        after = moment(params["after"])
        until = None if self.until_ignored else params.get("until")
        rows = [
            o
            for o in self.orders
            if o["side"] == params["side"]
            and o["symbol"] in params["symbols"].split(",")
            and after < moment(o["submitted_at"])
            and (until is None or moment(o["submitted_at"]) < moment(until))
        ]
        rows.sort(key=lambda o: moment(o["submitted_at"]), reverse=params["direction"] == "desc")
        return rows[: params["limit"]]


def fill(
    side: str,
    qty: str,
    symbol: str = "SHAPE",
    *,
    submitted: int = WRITTEN + SECOND,
    filled: int | None = WRITTEN + 2 * SECOND,
    status: str = "filled",
) -> dict:
    """An order as Alpaca shows it; by default submitted and filled after WRITTEN."""
    return {
        "id": f"order-{next(_ids)}",
        "symbol": symbol,
        "side": side,
        "filled_qty": qty,
        "status": status,
        "submitted_at": stamp(submitted),
        "filled_at": None if filled is None else stamp(filled),
    }


def walk(side: str, until: str | None = None) -> dict:
    """The first page's query (review F1): status all, newest first, 92 days before WRITTEN."""
    query = {
        "status": "all",
        "limit": 500,
        "after": "2025-08-10T16:00:00+00:00",
        "direction": "desc",
        "side": side,
        "symbols": "SHAPE",
    }
    if until is not None:
        query["until"] = until
    return query


def test_sold_and_bought_since_send_the_pinned_orders_query(subject) -> None:
    """GetOrdersRequest (alpaca-py c42add7c0a70 alpaca/trading/requests.py:205-226) as
    TradingClient.get_orders sends it (client.py:120-125), walking back 92 days (review F1)."""
    venue = Orders(
        [fill("sell", "3"), fill("sell", "2.5"), fill("sell", "9", "OTHER"), fill("buy", "4")]
    )
    assert subject.sold_since(venue, "SHAPE", WRITTEN) == Decimal("5.5")
    assert subject.bought_since(venue, "SHAPE", WRITTEN) == Decimal(4)
    assert venue.reads == [("/orders", walk(side)) for side in ("sell", "buy")]


def test_fills_are_counted_by_fill_time_not_submission(subject) -> None:
    """Review F1: an order counts where its filled_at is after the write, a partial fill its
    filled quantity; one filled at or before the write does not count."""
    venue = Orders(
        [
            fill("sell", "3", submitted=WRITTEN - DAY, filled=WRITTEN + SECOND),
            fill("sell", "4", submitted=WRITTEN - DAY, filled=WRITTEN, status="canceled"),
            fill("sell", "5", submitted=WRITTEN - DAY, filled=WRITTEN - SECOND),
            fill("sell", "1.5", status="partially_filled"),
            fill("sell", "0", filled=None, status="new"),
        ]
    )
    assert subject.sold_since(venue, "SHAPE", WRITTEN) == Decimal("4.5")


def test_a_filled_quantity_with_no_fill_time_is_unknown(subject) -> None:
    venue = Orders([fill("sell", "3", filled=None)])
    with pytest.raises(subject.FillsUnknown, match="filled_at is not shown"):
        subject.sold_since(venue, "SHAPE", WRITTEN)


def test_the_walk_pages_past_the_500_cap_by_an_overlapping_until(subject) -> None:
    """Review F1: 1200 sells in three pages; each next `until` is the page's earliest submission
    plus one microsecond, and IDs deduplicate the overlap."""
    venue = Orders(
        [
            fill("sell", "0.01", submitted=WRITTEN + i * SECOND, filled=WRITTEN + (i + 1) * SECOND)
            for i in range(1, 1201)
        ]
    )
    assert subject.sold_since(venue, "SHAPE", WRITTEN) == Decimal("12.00")
    untils = [params.get("until") for _, params in venue.reads]
    assert untils == [
        None,
        stamp(WRITTEN + 701 * SECOND + 1000),
        stamp(WRITTEN + 202 * SECOND + 1000),
    ]


def test_more_pages_than_the_bound_is_unknown_not_a_short_count(subject) -> None:
    """Review F1: a walk that cannot finish in PAGES_MOST pages is unknown, named."""
    venue = Orders([fill("sell", "0.01", submitted=WRITTEN + i * SECOND) for i in range(1, 2002)])
    with pytest.raises(subject.FillsUnknown, match="more than 2000 sell orders"):
        subject.sold_since(venue, "SHAPE", WRITTEN)
    assert len(venue.reads) == subject.PAGES_MOST


def test_a_venue_that_ignores_until_cannot_advance_and_is_unknown(subject) -> None:
    venue = Orders(
        [fill("sell", "0.01", submitted=WRITTEN + i * SECOND) for i in range(1, 600)],
        until_ignored=True,
    )
    with pytest.raises(subject.FillsUnknown, match="lost its overlapping boundary|cannot advance"):
        subject.sold_since(venue, "SHAPE", WRITTEN)


@pytest.mark.parametrize(
    "venue,want",
    [
        (Orders([fill("sell", "9.5")]), None),
        (Orders([fill("sell", "10")]), "a new holding: sold out since"),
        (Orders([fill("sell", "4"), fill("sell", "7")]), "a new holding: sold out since"),
        (None, "venue orders unread"),
        (Orders(failure=OSError("neutral")), "venue orders unread"),
        (Orders([fill("sell", "0", submitted=WRITTEN + SECOND)] * 500), "venue orders unread"),
        (Orders([fill("sell", "-1")]), "venue orders unread"),
    ],
    ids=["same", "sold-out", "sold-out-in-parts", "no-reader", "fails", "full-page", "bad-qty"],
)
def test_same_holding_is_decided_by_the_venues_sells(subject, venue, want) -> None:
    made = entry(subject, quantity=Decimal(10), written_ns=WRITTEN)
    reason = subject.same_holding(venue, "SHAPE", made)
    if want is None:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(want), reason


@pytest.mark.parametrize(
    "orders,quantity,held,want",
    [
        ([], "10", "100", "unknown"),
        ([], "100", "10", "unknown"),
        ([fill("buy", "5")], "10", "15", "same"),
        ([fill("sell", "4")], "10", "6", "same"),
        ([fill("buy", "5")], "10", "50", "unknown"),
        ([fill("sell", "10"), fill("buy", "100")], "10", "100", "new"),
    ],
    ids=[
        "forward-split",
        "reverse-split",
        "rise-by-buys",
        "fall-by-sells",
        "rise-unexplained",
        "sold-out-then-bought",
    ],
)
def test_a_quantity_the_fills_do_not_explain_is_unknown(
    subject, orders, quantity, held, want
) -> None:
    """Review F4: the quantity held now must be the entry's less sales plus buys since its write;
    a split, forward or reverse, is unknown by name, never carried."""
    made = entry(subject, quantity=Decimal(quantity), written_ns=WRITTEN)
    kind, reason = subject.holding_verdict(Orders(orders), "SHAPE", made, Decimal(held))
    assert kind == want, reason
    if want == "unknown":
        assert reason is not None and "a split or a change the fills do not show" in reason


# Review fix round (REVIEW-L1, 2026-11-13): the reviewer's repros, copied unchanged from
# REVIEW-L1-repro.py beside the review, with its fake and helpers renamed `r_` to keep them apart.

R_WRITTEN = 1_762_790_400_000_000_000
R_MIN = 60_000_000_000


def r_iso(ns: int) -> str:
    from datetime import UTC
    from datetime import datetime

    return datetime.fromtimestamp(ns / 1e9, UTC).isoformat()


class AlpacaOrders:
    """GET /orders as alpaca-py documents `after`: orders *submitted* after it
    (alpaca/trading/requests.py:211)."""

    def __init__(self, orders):
        self.orders = orders

    def get(self, route, params=None):
        from datetime import datetime

        assert params is not None  # Added for ty; otherwise the reviewer's fake unchanged.
        after = datetime.fromisoformat(params["after"])
        return [
            o
            for o in self.orders
            if o["side"] == params["side"] and datetime.fromisoformat(o["submitted_at"]) > after
        ]


def r_order(side: str, qty: str, submitted: int, filled: int) -> dict:
    return {
        "symbol": "SHAPE",
        "side": side,
        "filled_qty": qty,
        "status": "filled",
        "submitted_at": r_iso(submitted),
        "filled_at": r_iso(filled),
    }


def test_r1_sell_submitted_before_the_write_and_filled_after_is_a_new_holding(subject) -> None:
    entry = subject.Entry(Decimal("1.70"), 0, Decimal(10), R_WRITTEN, "run-1")
    venue = AlpacaOrders([r_order("sell", "10", R_WRITTEN - R_MIN, R_WRITTEN + R_MIN)])
    assert subject.same_holding(venue, "SHAPE", entry) is not None, (
        "sold out after the write, yet carried"
    )


def test_r1b_buy_submitted_before_kept_and_filled_after_counts(subject) -> None:
    venue = AlpacaOrders([r_order("buy", "10", R_WRITTEN - R_MIN, R_WRITTEN + R_MIN)])
    assert subject.bought_since(venue, "SHAPE", R_WRITTEN) == 10


def test_r2_an_unread_venue_never_lowers_the_books_level(subject, tmp_path: Path) -> None:
    from tests import test_owner_protector as base
    from tests import test_start_level_blind as blind

    path = tmp_path / "levels.json"
    blind.seed_book(path, {blind.book_key(): blind.entry(str(blind.CARRY))})
    first = blind.protector(tmp_path, history=blind.climbing_tape(), bid="1.10")
    first.settings.trading = base.VenueOrders(failure=OSError("neutral outage"))
    first.on_start()
    blind.two_quotes(first)
    first.on_stop()
    kept = subject.LevelBook.read(path)[blind.book_key()]
    assert kept.last_level is not None and kept.last_level >= blind.CARRY.as_decimal(), kept


def test_r3_forward_split_carries_a_level_far_over_the_bid_and_sells(
    subject, tmp_path: Path
) -> None:
    """10:1 split: 10 shares at ~1.75 become 100 at ~0.175. No order sold anything."""
    from tests import test_start_level_blind as blind

    path = tmp_path / "levels.json"
    subject.LevelBook.write(
        path,
        {
            blind.book_key(): subject.Entry(
                Decimal("1.70"),
                blind.START,
                Decimal(10),
                blind.T0 - R_MIN,
                "run-1",
                distance=Decimal("0.05"),
            )
        },
    )
    strategy = blind.protector(tmp_path, held=[blind.holding("100")], history=[], bid="0.17")
    strategy.on_start()
    blind.two_quotes(strategy)
    assert not strategy.sent, (
        f"sent {[(o.quantity, o.trigger_price) for o in strategy.sent]} at bid 0.17"
    )


def test_r1c_a_holding_the_run_saw_end_leaves_no_carriable_entry(subject, tmp_path: Path) -> None:
    from typing import Any
    from typing import cast

    from tests import test_start_level_blind as blind

    path = tmp_path / "levels.json"
    run = blind.protector(tmp_path, history=blind.climbing_tape(), bid="1.75")
    run.on_start()
    blind.two_quotes(run)
    assert len(run.sent) == 1
    run.cached.held = []  # the owner's own sell flattened it, seen by this run
    run._on_step(cast(Any, SimpleNamespace()))
    run.on_stop()
    found = subject.LevelBook.read(path)
    entry = found.get(blind.book_key())
    assert entry is None or entry.last_level is None, (
        f"an ended holding's level stays carriable: {entry}"
    )
