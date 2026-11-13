"""Custody offline: the lineage classifier and `paper:account`'s read (src/canon_trading/custody.py).

No test here opens a network connection or reads a credential. Alpaca's answers are alpaca-py 0.45.0's own
models, validated from made-up payloads; the task's shell body runs with a stand-in `security` and `uv`.
"""

import os
import re
import signal
import subprocess
import tomllib
from collections import deque
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.models import Quote
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.execution import replacement_client_order_id

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_NUMBER = "PA3MADEUP0001"
ACCOUNT_ID = str(uuid4())

OURS = [
    "O-20261111-165400-001-001-1",  # SessionMaCross in a window, as its record names it (2026-11-11)
    "O-20261111-150428-001-001-3",
    "O-20261111-150428-001-001-1234",
    "canon-close-20261111T150424Z-1-6168c4fe",  # the account check's close (ADR 0060)
    "canon-close-hand-20261111T150424Z-2-0123abcd",  # the close-only form's
]
FOREIGN = [
    str(uuid4()),  # what Alpaca generates when no ID is sent (the dashboard, another tool)
    "O-20261111-165400-002-001-1",  # another trader's tag
    "O-20261111-165400-001-CAP-1",  # another strategy's tag
    "O-20261111-165400-001-001-",  # no count
    "o-20261111-165400-001-001-1",
    "x O-20261111-165400-001-001-1",
    "O-20261111-165400-001-001-1 ",
    "canon-close-",  # the prefix alone
    "canon-close-manual",  # the prefix, typed by anyone
    "canon-close-start-20261111T150424Z-1-6168c4fe",  # a form no code writes yet
    "canon-close-20261111T150424Z-1-6168C4FE",
    "O-\u0662\u0660\u0662\u0666\u0661\u0661\u0661\u0661-165400-001-001-1",  # digits, but not ASCII
    "canon-resend-0001",
    "my-order-7",
]
MISSING: list[Any] = [None, "", "   "]
CONFIGURATION: dict[str, Any] = {
    "no_shorting": True,
    "suspend_trade": False,
    "fractional_trading": True,
    "max_margin_multiplier": "4",
    "trade_confirm_email": "all",
    "ptp_no_exception_entry": False,
    "max_options_trading_level": 3,
    "disable_overnight_trading": False,
}
"""`GET /v2/account/configurations` as alpaca-py 0.45.0's `AccountConfiguration` reads it (the same
made-up answer as `tests/test_session_window.py`'s)."""


@pytest.mark.parametrize("client_order_id", OURS)
def test_our_client_order_ids_are_ours(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.OURS


@pytest.mark.parametrize("client_order_id", FOREIGN)
def test_any_other_client_order_id_is_foreign(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.FOREIGN


@pytest.mark.parametrize("client_order_id", MISSING)
def test_an_order_with_no_client_order_id_is_never_ours(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.MISSING


STAMP = 1_762_999_999_123_456_789
"""A clock reading, as the adapter puts one after the count of a replacement's client order ID."""


@pytest.mark.parametrize("client_order_id", [o for o in OURS if o.startswith("O-")])
def test_a_replacement_our_adapter_sent_reads_as_ours(client_order_id) -> None:
    """R1 (the lead, 2026-11-12): the adapter gives its own replacement the node's ID with a clock
    stamp after the count, which is still an ID of our nodes' form."""
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is not None and sent != client_order_id
    assert custody.lineage(sent) == custody.OURS


def test_a_replacement_a_hand_made_is_foreign_although_it_replaces_ours() -> None:
    """R2: an order replaced by hand carries an ID Alpaca made. It freezes its symbol and is never
    canceled, whatever it replaces: nothing a hand touched is ours to end."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    # the stop of ours it replaced is no longer open, and is not listed
    by_hand = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", side="sell", replaces=str(uuid4())
    )
    report = close(venue)
    assert venue.sent == [] and venue.canceled == []
    assert report.frozen["SPY"] == ["open a foreign order: sell 1 SPY limit, new"]
    assert any(
        o.client_order_id == by_hand.client_order_id and _value(o.status) == "new"
        for o in venue.history
    )


def test_our_own_replacement_of_our_stop_is_canceled_like_any_order_of_ours() -> None:
    """R1 where it is acted on: the replaced stop no longer freezes its symbol. The stop it replaced
    is not listed, as one placed before the lookback is not, and with no fill it carries none."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    replacement = venue.open(
        client_order_id=replacement_client_order_id("O-20261111-145500-001-001-2", STAMP),
        symbol="SPY",
        side="sell",
        replaces=str(uuid4()),
    )
    report = close(venue)
    assert venue.canceled == [replacement.client_order_id]
    assert report.frozen == {}


@pytest.mark.parametrize("client_order_id", FOREIGN)
def test_no_foreign_order_reads_as_ours_through_its_replacement(client_order_id) -> None:
    """R3: the replacement's ID is derived from the order's own, so what was foreign stays foreign."""
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is None or custody.lineage(sent) == custody.FOREIGN


@pytest.mark.parametrize(
    "client_order_id",
    [
        *(o for o in OURS if o.startswith(custody.CHECK_CLOSE_PREFIX)),
        "canon-close-20261111T150424Z-1-12345678",  # a random tail that happens to be all digits
    ],
)
def test_a_replacement_of_a_checks_close_is_never_read_as_ours(client_order_id) -> None:
    """The limit of R1: only an ID of the form a node writes keeps its form through a replace. A
    close the account check or the close-only form placed ends in eight hex digits, so its replacement
    gets Alpaca's ID or, when those digits are all decimal, one that is no longer a close's: foreign
    either way. No node replaces such an order."""
    assert custody.lineage(client_order_id) == custody.OURS
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is None or custody.lineage(sent) == custody.FOREIGN


def chain(
    venue: Venue,
    *filled: str,
    qty: str = "20",
    last: str = "filled",
    side: str = "buy",
    by_hand: tuple[int, ...] = (),
    first: str | None = None,
    first_age: timedelta = timedelta(minutes=10),
) -> None:
    """An order of ours for `qty` replaced once for each further entry of `filled`, as Alpaca lists a
    chain: each replaced order with its own fills and `replaced_by`, each replacement with `replaces`
    and a filled quantity that counts the fills before it (ADR 0042, paper, 2026-11-10). A replacement
    is the adapter's, under the first order's ID with a clock stamp, unless its place is in `by_hand`,
    which gives it an ID Alpaca made. The first order was submitted `first_age` ago, each replacement
    within the last ten minutes."""
    ids = [str(uuid4()) for _ in filled]
    first = first or NODE_BUY
    for n, quantity in enumerate(filled):
        is_last = n == len(filled) - 1
        age = first_age if n == 0 else timedelta(minutes=10 - n)
        venue.open(
            id=ids[n],
            client_order_id=first
            if n == 0
            else str(uuid4())
            if n in by_hand
            else replacement_client_order_id(first, STAMP + n),
            symbol="SPY",
            side=side,
            qty=qty,
            filled_qty=quantity,
            status=last if is_last else "replaced",
            replaces=ids[n - 1] if n else None,
            replaced_by=None if is_last else ids[n + 1],
            submitted_at=(venue.now - age).isoformat(),
        )


def opened(venue: Venue) -> tuple[Decimal | None, str]:
    return custody._opened(cast(Any, venue), "SPY", venue.now)


def test_a_hands_replacement_in_the_middle_of_a_chain_takes_its_own_fills_out_of_ours() -> None:
    """Ours filled 16, a hand replaced it and 2 more filled, and the node replaced that one: the last
    order is ours again and shows 20, the whole chain's. Ours filled 16 and then 2 after the hand's
    18: 18, neither the 36 of the two orders of ours nor the chain's 20."""
    venue = Venue(REGULAR)
    chain(venue, "16", "18", "20", by_hand=(1,))
    assert opened(venue) == (Decimal(18), "")


def test_a_chain_of_our_sells_with_fills_on_both_orders_is_not_counted() -> None:
    """Turned on the lead's ruling of 2026-11-13: this case read the chain's 8 as carrying the 5 and
    counted 12 of ours. Whether it carries them is not established, so nothing is counted."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 12)  # 20 bought by ours in the lookback, 8 or 13 of them sold by the chain
    venue.history[-1] = venue.history[-1].model_copy(update={"qty": "20", "filled_qty": "20"})
    chain(venue, "5", "8", side="sell", first="O-20261111-145500-001-001-2")
    quantity, why = opened(venue)
    assert quantity is None and "two orders running" in why


def test_a_replacement_showing_fewer_fills_than_the_order_it_replaces_freezes_the_symbol() -> None:
    """Antigravity #345 F3: a positive per-leg count must not be clamped into a zero sale."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 5, client_order_id=None)
    venue.hold("SPY", 20)
    venue.held["SPY"] = Decimal(10)  # 5 ours after selling 15, beside 5 foreign shares
    chain(venue, "10", "5", side="sell", first="O-20261111-145500-001-001-2")

    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen
    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "fewer fills" in why
    assert isinstance(why, custody._Unread)


@pytest.mark.parametrize("side", [None, "", "hold"])
@pytest.mark.parametrize("filled", ["0", "5"])
def test_an_order_of_ours_without_a_side_freezes_the_symbol_instead_of_counting_as_a_sell(
    side: str | None, filled: str
) -> None:
    """Antigravity #345 F5; non-enum sides are defensive model-copy cases, not valid payloads."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=NODE_BUY)
    second = venue.open(
        client_order_id="O-20261111-145200-001-001-2",
        symbol="SPY",
        side=None,
        qty="5",
        filled_qty=filled,
        status="filled",
        submitted_at=venue.now.isoformat(),
    )
    assert second.side is None
    if side is not None:
        venue.history[-1] = second.model_copy(update={"side": side})
    venue.held["SPY"] = Decimal(20) - Decimal(filled)

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "side" in why
    assert isinstance(why, custody._Unread)
    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen


def test_an_order_of_our_form_that_replaces_a_foreign_one_counts_only_its_own_fills() -> None:
    """Nothing foreign reads as ours through a chain either: the 5 a foreign order filled stay
    foreign when an order of our form replaces it and shows 8."""
    venue = Venue(REGULAR)
    foreign = str(uuid4())
    venue.open(
        id=foreign, client_order_id=str(uuid4()), filled_qty="5", qty="20", status="replaced"
    )
    venue.open(client_order_id=OURS[1], filled_qty="8", qty="20", status="filled", replaces=foreign)
    assert opened(venue) == (Decimal(3), "")


def test_a_replacement_whose_replaced_order_is_not_listed_freezes_the_symbol() -> None:
    """A buy of ours working for more than the lookback filled 18 before it and was replaced inside
    it: the replacement shows 20, of which only 2 are the lookback's, and the listing does not hold
    the order that says so. With 18 foreign shares held the 20 would match and all be sold. What
    cannot be established freezes."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, "18", "20", first_age=custody.LINEAGE_LOOKBACK + timedelta(hours=1))
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order of ours in SPY replaces one the listing of the last 1 day, 0:00:00 does not hold, "
        "whose fills it carries, so the quantity ours opened is not established"
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [why]


def test_a_replace_chain_with_a_zero_fill_middle_link_walks_all_ancestors_before_closing() -> None:
    """Antigravity #345 F4: the zero middle link cannot hide fills outside the lookback."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, "18", "0", "20", first_age=custody.LINEAGE_LOOKBACK + timedelta(hours=1))

    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen
    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "listing" in why
    assert isinstance(why, custody._Unread)


def test_a_zero_fill_middle_link_does_not_count_earlier_fills_twice_and_sell_foreign_shares() -> (
    None
):
    venue = Venue(REGULAR)
    venue.hold("SPY", 14, client_order_id=None)  # the chain does not establish whose these are
    chain(venue, "4", "0", "10")

    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen
    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "not established" in why
    assert isinstance(why, custody._Unread)
    assert venue.held["SPY"] == 14


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
@pytest.mark.parametrize(
    ("filled", "by_hand", "held"),
    [
        (("4", "0", "10"), (), 10),
        (("4", "6", "0", "10"), (1,), 8),
        (("4", "0", "10"), (2,), 4),
    ],
    ids=["all-ours", "foreign-middle", "foreign-tail"],
)
def test_zero_fill_middle_links_after_earlier_fills_freeze_the_symbol(
    side: str, reverse: bool, filled: tuple[str, ...], by_hand: tuple[int, ...], held: int
) -> None:
    """L8 R1: the earlier build's numeric result cannot establish the holding's owner."""
    venue = ReplacedNotListedOpen(REGULAR)
    if side == "sell":
        venue.hold("SPY", 20)
        held = 20 - held
    chain(venue, *filled, side=side, by_hand=by_hand, first="O-20261111-145500-001-001-2")
    venue.held["SPY"] = Decimal(held)
    if reverse:
        venue.history.reverse()

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "not established" in why
    assert isinstance(why, custody._Unread)
    report = close(venue)
    assert not report.failed and not report.errors
    assert venue.sent == [] and venue.held["SPY"] == held
    assert "SPY" in report.frozen


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
def test_a_foreign_tail_with_decreased_fills_freezes_before_closing_ours(
    side: str, reverse: bool
) -> None:
    """L8 R2: a hand's smaller tail count must be checked even without our later replacement."""
    venue = ReplacedNotListedOpen(REGULAR)
    held = 12
    if side == "sell":
        venue.hold("SPY", 30)
        held = 18
    chain(venue, "10", "12", "5", side=side, by_hand=(2,))
    venue.held["SPY"] = Decimal(held)
    if reverse:
        venue.history.reverse()
    resting = venue.open(client_order_id=OURS[1], symbol="SPY", side="sell", limit_price="999")
    foreign = venue.open(client_order_id=FOREIGN[0], symbol="AAPL", side="buy")

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "fewer fills" in why
    assert isinstance(why, custody._Unread)
    report = close(venue)
    assert not report.failed and not report.errors
    assert venue.canceled == [resting.client_order_id]
    assert foreign.client_order_id not in venue.canceled
    assert venue.sent == [] and venue.held["SPY"] == held
    assert "SPY" in report.frozen


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
@pytest.mark.parametrize("owners", ["ours", "mixed", "foreign"])
@pytest.mark.parametrize("filled", [("4", "10"), ("0", "0")], ids=["filled", "unfilled"])
def test_more_than_one_listed_replacement_freezes_whatever_their_owners(
    side: str, reverse: bool, owners: str, filled: tuple[str, str]
) -> None:
    """L8 R3: two successors of the same listed order cannot each claim the same delta."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 20)
    chain(venue, *filled, side=side, by_hand=(1,) if owners == "foreign" else ())
    fork = venue.history[-1].model_copy(
        update={
            "id": uuid4(),
            "client_order_id": replacement_client_order_id(NODE_BUY, STAMP + 9)
            if owners == "ours"
            else FOREIGN[0],
        }
    )
    venue.history.append(fork)
    if reverse:
        venue.history.reverse()
    resting = venue.open(client_order_id=OURS[1], symbol="SPY", side="sell", limit_price="999")
    foreign = venue.open(client_order_id=FOREIGN[1], symbol="AAPL", side="buy")

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "more than one" in why
    assert isinstance(why, custody._Unread)
    report = close(venue)
    assert not report.failed and not report.errors
    assert venue.canceled == [resting.client_order_id]
    assert foreign.client_order_id not in venue.canceled
    assert venue.sent == [] and venue.held["SPY"] == 20
    assert "SPY" in report.frozen


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
@pytest.mark.parametrize("by_hand", [(), (1,)], ids=["all-ours", "foreign-middle"])
@pytest.mark.parametrize(
    ("filled", "last"),
    [(("0", "0", "10"), "filled"), (("4", "10", "0"), "rejected")],
    ids=["zeros-before-first-fill", "rejected-zero-tail"],
)
def test_zero_links_before_the_first_fill_and_a_rejected_zero_tail_keep_their_counts(
    side: str, reverse: bool, by_hand: tuple[int, ...], filled: tuple[str, ...], last: str
) -> None:
    """R1's exceptions: an unfilled stop tightened before filling, and a rejected last link."""
    venue = ReplacedNotListedOpen(REGULAR)
    ours = 4 if by_hand and last == "rejected" else 10
    held = ours
    if side == "sell":
        venue.hold("SPY", 20)
        held = 20 - ours
    chain(venue, *filled, side=side, by_hand=by_hand, last=last)
    venue.held["SPY"] = Decimal(held)
    if reverse:
        venue.history.reverse()

    if side == "sell" and last == "rejected":
        # Turned on the lead's ruling of 2026-11-13: 4 then 10 are fills on two sells running,
        # so the zero tail no longer has a count to keep.
        quantity, why = opened(venue)
        assert quantity is None and "two orders running" in why
        return
    assert opened(venue) == (Decimal(held), "")
    report = close(venue)
    assert not report.failed and not report.errors
    assert report.frozen == {} and report.flat
    assert [(request.symbol, request.qty) for request in venue.sent] == [("SPY", held)]


@pytest.mark.parametrize("cycle", ["self", "whole-chain", "through-zero"])
def test_a_replace_chain_cycle_freezes_the_symbol(cycle: str) -> None:
    venue = ReplacedNotListedOpen(REGULAR)
    chain(venue, "0" if cycle == "through-zero" else "4", "4", "4")
    at = -1 if cycle == "self" else 0
    venue.history[at] = venue.history[at].model_copy(update={"replaces": venue.history[-1].id})
    venue.held["SPY"] = Decimal(4)

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "cycle" in why
    assert isinstance(why, custody._Unread)
    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen


@pytest.mark.parametrize("zero_middle", [False, True])
def test_a_decrease_in_foreign_ancestors_leaves_our_replacements_fills_unknown(
    zero_middle: bool,
) -> None:
    venue = ReplacedNotListedOpen(REGULAR)
    if zero_middle:
        chain(venue, "10", "0", "5", "20", by_hand=(1, 2))
    else:
        chain(venue, "10", "5", "20", by_hand=(1,))
    venue.held["SPY"] = Decimal(25)

    quantity, why = opened(venue)
    assert quantity is None
    assert "SPY" in why and "fewer fills" in why
    report = close(venue)
    assert venue.sent == []
    assert "SPY" in report.frozen


def test_a_missing_ancestor_at_the_first_read_stays_frozen_after_cancel(monkeypatch) -> None:
    venue = ReplacedNotListedOpen(REGULAR)
    chain(venue, "4", "0", "10")
    venue.held["SPY"] = Decimal(10)
    root_id = venue.history[0].id
    resting = venue.open(client_order_id=OURS[1], symbol="SPY", side="sell", qty="1")
    get_orders = venue.get_orders
    history_reads = 0

    def first_root_missing(filter=None):
        nonlocal history_reads
        found = get_orders(filter)
        if filter is not None and filter.status.value == "all":
            history_reads += 1
            if history_reads == 1:
                return [order for order in found if order.id != root_id]
        return found

    monkeypatch.setattr(venue, "get_orders", first_root_missing)
    report = close(venue)
    assert venue.canceled == [resting.client_order_id]
    assert venue.sent == []
    assert any("at the first read" in why for why in report.frozen["SPY"])


@pytest.mark.parametrize("shown", [None, "", "many"], ids=["absent", "empty", "no number"])
def test_a_replaced_order_that_shows_no_fill_count_freezes_the_symbol(shown) -> None:
    """Ours filled nothing, a hand replaced it and 18 filled, and the node replaced that one, which
    shows the chain's 20. The listing holds the hand's order but not its fill count, which alpaca-py's
    `Order.filled_qty` allows. Read as zero, all 20 would be ours and the 18 foreign shares held would
    be sold with them. What the replaced order filled is not known, so nothing is established."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, "0", "18", "20", by_hand=(1,))
    (at,) = [n for n, order in enumerate(venue.history) if order.filled_qty == "18"]
    venue.history[at] = venue.history[at].model_copy(update={"filled_qty": shown})
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order in SPY shows no fill count that can be counted, so the quantity ours opened is not "
        "established"
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [why]


def test_a_replaced_order_that_shows_a_negative_fill_count_freezes_the_symbol() -> None:
    """A count below zero is no count either. Taken as shown, the replacement's 20 less the hand's
    "-18" would be 38 of ours, and with 38 held, 18 of them foreign, all would be sold."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 38, client_order_id=None)
    chain(venue, "0", "-18", "20", by_hand=(1,))
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order in SPY shows no fill count that can be counted, so the quantity ours opened is not "
        "established"
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [why]


NO_COUNT = [None, "", "many", "-18", "NaN", "Infinity"]
"""What a listing may show for a fill count that is no count: alpaca-py's `Order.filled_qty` is an
optional string or number with no bound."""


@pytest.mark.parametrize("shown", NO_COUNT, ids=[repr(v) for v in NO_COUNT])
def test_an_order_of_ours_that_shows_no_fill_count_freezes_the_symbol(shown) -> None:
    """Ours bought 20. Our sell filled nothing, our replacement of it sold 18, and a hand replaced
    that one, showing the chain's 20. Our replacement is listed without its count. Read as zero, our
    sale of 18 vanishes, ours still reads 20, and the 20 held, of which at most 2 are ours, are sold."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 20)
    chain(venue, "0", "18", "20", side="sell", by_hand=(2,), first="O-20261111-145500-001-001-2")
    (at,) = [n for n, order in enumerate(venue.history) if order.filled_qty == "18"]
    venue.history[at] = venue.history[at].model_copy(update={"filled_qty": shown})
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order in SPY shows no fill count that can be counted, so the quantity ours opened is not "
        "established"
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [why]


def test_an_order_of_ours_with_no_fill_count_still_has_our_open_orders_canceled() -> None:
    """The symbol freezes by name; it does not end the run. An open order of ours in it is still
    canceled, as in any frozen symbol, where a count that is no number used to raise before that."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 20)
    venue.open(client_order_id=OURS[1], side="sell", qty="1", filled_qty="many", status="canceled")
    resting = venue.open(
        client_order_id=OURS[2], side="sell", qty="1", type="limit", limit_price="999"
    )
    report = close(venue)
    assert venue.sent == []
    assert venue.canceled == [resting.client_order_id]
    assert report.frozen["SPY"] == [
        (
            "an order in SPY shows no fill count that can be counted, so the quantity ours opened is not "
            "established"
        )
    ]


NO_NUMBER = [None, "", "many", "NaN", "Infinity"]


@pytest.mark.parametrize("shown", NO_NUMBER, ids=[repr(v) for v in NO_NUMBER])
def test_a_position_that_shows_no_count_freezes_its_symbol_and_the_rest_goes_on(shown) -> None:
    """A position's quantity that is no finite number is not known, and its symbol freezes by name.
    It does not end the run: our open order in it is still canceled, and the long ours opened in
    another symbol is still closed."""
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty": shown}}
    venue.hold("SPY", 1)
    venue.hold("AAPL", 1)
    venue.history[-1] = venue.history[-1].model_copy(
        update={"client_order_id": OURS[1], "symbol": "AAPL"}
    )
    resting = venue.open(
        client_order_id=OURS[2], symbol="SPY", side="sell", qty="1", type="limit", limit_price="999"
    )
    report = close(venue)
    assert venue.canceled == [resting.client_order_id]
    why = (
        "the position in SPY shows no quantity that can be counted, so what is held there is not "
        "known"
    )
    assert report.frozen == {"SPY": [why]}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("AAPL", 1)


@pytest.mark.parametrize("shown", NO_NUMBER, ids=[repr(v) for v in NO_NUMBER])
def test_a_position_of_unknown_quantity_is_not_carried_as_holding_nothing(shown) -> None:
    """Unknown is not zero: the holding's quantity is None, apart from the zero of a symbol that has
    only an open order."""
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty": shown}}
    venue.hold("SPY", 1)
    venue.open(client_order_id=OURS[2], symbol="AAPL", side="buy", qty="1")
    unknown, only_an_order = sorted(
        custody.read_holdings(cast(Any, venue), venue.now), key=lambda h: h.symbol != "SPY"
    )
    assert (unknown.symbol, unknown.held) == ("SPY", None)
    assert unknown.ours_left() == []
    assert (only_an_order.symbol, only_an_order.held) == ("AAPL", Decimal(0))


def test_a_long_listed_below_zero_is_a_short_and_never_read_as_a_long() -> None:
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty": "-18"}}
    venue.hold("SPY", 18)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == ["a short of 18 SPY: never bought back here"]


@pytest.mark.parametrize(
    ("order", "quantity"),
    [
        ({"qty": "-1", "filled_qty": "NaN"}, "a quantity that is no count"),
        ({"qty": "many"}, "a quantity that is no count"),
        ({"qty": ""}, "a quantity that is no count"),
        ({"qty": None, "notional": "100"}, "notional 100"),
        ({"qty": "2"}, "2"),
    ],
    ids=["below zero", "no number", "empty", "by notional", "a count"],
)
def test_a_line_about_an_order_names_a_quantity_that_is_no_count(order, quantity) -> None:
    assert custody.order_line(_order(**order)).startswith(f"SPY buy {quantity} limit ")
    assert f"(buy {quantity} SPY limit, " in custody.describe(_order(**order))


@pytest.mark.parametrize(
    ("position", "said"),
    [
        ({"qty": "many"}, "SPY long a quantity that is no number avg_entry"),
        ({"qty": "NaN"}, "SPY long a quantity that is no number avg_entry"),
        ({"qty": "2"}, "SPY long 2 avg_entry"),
        ({"qty": "-3", "side": "short"}, "SPY short -3 avg_entry"),
    ],
    ids=["no number", "not finite", "a count", "a short"],
)
def test_a_line_about_a_position_names_a_quantity_that_is_no_number(position, said) -> None:
    assert custody.position_line(_position(**position)).startswith(said)


@pytest.mark.parametrize(
    ("position", "note"),
    [
        ({"qty": "2"}, ""),
        ({"qty": "-3", "side": "short"}, ""),
        ({"qty": "-18"}, " (below zero: read as a short)"),
        (
            {"qty": "2", "qty_available": "many"},
            " (the position in SPY lists a quantity no order holds that cannot be counted)",
        ),
        (
            {"qty": "many"},
            (
                " (the position in SPY shows no quantity that can be counted, so what is held there "
                "is not known)"
            ),
        ),
    ],
    ids=["a long", "a short", "a long below zero", "available unreadable", "no number"],
)
def test_the_accounts_line_reads_a_position_as_the_close_form_does(position, note) -> None:
    line = custody.position_line(_position(**position))
    assert line.endswith(f"unrealized_pl 0.88{note}")


@pytest.mark.parametrize("available", [None, "0", "1"], ids=["not listed", "none free", "one free"])
def test_the_quantity_no_order_holds_decides_nothing_and_may_be_left_out(available) -> None:
    """Custody reads a position's `qty_available` for no decision: the long of ours is closed whole
    whatever it shows, and a listing that leaves it out is read like any other. The day custody decides
    on it, this case changes and the field becomes required in that same change."""
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty_available": available}}
    venue.hold("SPY", 2)
    (holding,) = custody.read_holdings(cast(Any, venue), venue.now)
    assert (holding.held, holding.frozen) == (Decimal(2), [])
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 2)


@pytest.mark.parametrize("available", ["", "many", "-1", "NaN", "Infinity"])
def test_a_listed_quantity_no_order_holds_that_is_no_count_freezes_the_symbol(available) -> None:
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty_available": available}}
    venue.hold("SPY", 2)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [
        "the position in SPY lists a quantity no order holds that cannot be counted"
    ]


def test_a_short_listed_with_its_quantity_below_zero_is_still_a_short_never_bought_back() -> None:
    """Alpaca lists a short with its quantity below zero: there, and only there, that is a count."""
    venue = PositionShows(REGULAR)
    venue.shown = {"SPY": {"qty": "-3", "qty_available": "-3", "side": "short"}}
    venue.hold("SPY", -3, client_order_id=None)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == ["a short of 3 SPY: never bought back here"]


def test_an_order_placed_by_notional_names_no_quantity_and_freezes_nothing() -> None:
    """Alpaca takes an order for a dollar amount in place of a share quantity, and lists it with no
    quantity. That is not a quantity that cannot be counted: its fill count is still a count."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 2)
    by_notional = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", filled_qty="0.5", status="canceled"
    )
    venue.history[-1] = by_notional.model_copy(update={"qty": None, "notional": "100"})
    assert opened(venue) == (Decimal(2), "")
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 2)


def test_an_order_with_neither_a_quantity_nor_a_notional_freezes_the_symbol() -> None:
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 2)
    neither = venue.open(client_order_id=str(uuid4()), symbol="SPY", status="canceled")
    venue.history[-1] = neither.model_copy(update={"qty": None, "notional": None})
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order in SPY shows no quantity that can be counted, so the quantity ours opened is not "
        "established"
    )


@pytest.mark.parametrize("filled", [("18", "20"), ("16", "18", "20"), ("0", "20")])
def test_a_chain_of_our_own_replacements_counts_its_fills_once(filled) -> None:
    """Now that the adapter's replacement reads as ours, a partly filled buy and its replacement are
    both in the count, and the replacement's filled quantity already holds the first one's fills. The
    long of 20 is what the chain opened, not the sum: it is closed, and nothing is frozen."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, *filled)
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 20)


def test_a_chain_never_counts_more_than_it_filled_so_a_foreign_share_is_not_sold() -> None:
    """The case the sum would get wrong the dangerous way: 18 foreign shares beside the 20 a chain of
    ours filled. Summed, ours would read 38, the long would match and the close would sell all of it."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 38, client_order_id=None)
    chain(venue, "18", "20")
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [
        "38 SPY held, but our orders opened 20 in the last 1 day, 0:00:00"
    ]


def test_a_replacement_that_carries_no_fills_leaves_the_replaced_orders_own() -> None:
    """A replacement Alpaca rejected after `replaced` may show no fills: the chain still counts what
    the replaced order filled."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 18, client_order_id=None)
    chain(venue, "18", "0", last="rejected")
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 18)


def test_a_replacement_a_hand_made_takes_none_of_our_replaced_orders_fills() -> None:
    """Ours filled 18 and was then replaced by hand: the replacement is foreign and its fills are
    not ours, so the 18 still count and the 20 held do not match."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    by_hand = str(uuid4())
    venue.open(
        client_order_id=NODE_BUY,
        side="buy",
        qty="20",
        filled_qty="18",
        status="replaced",
        replaced_by=by_hand,
        submitted_at=(venue.now - timedelta(minutes=10)).isoformat(),
    )
    venue.open(
        id=by_hand,
        client_order_id=str(uuid4()),
        side="buy",
        qty="20",
        filled_qty="20",
        status="filled",
        submitted_at=(venue.now - timedelta(minutes=9)).isoformat(),
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [
        "20 SPY held, but our orders opened 18 in the last 1 day, 0:00:00"
    ]


def test_the_lineage_is_the_one_our_nodes_and_checks_are_built_with() -> None:
    """The pattern's parts are the ones the code that writes the IDs uses: `paper_node.TRADER_ID`'s tag,
    the order ID tag each order-sending strategy is given, and the account check's close prefix."""
    assert custody.TRADER_TAG == "001" and paper_node.TRADER_ID.value == "TESTER-001"
    instrument = session_window.WindowConfig(
        session="regular", log_dir=ROOT, symbol="SPY"
    ).instrument_id
    assert paper_node.ema_cross_config(instrument).order_id_tag == custody.ORDER_ID_TAG
    source = (ROOT / "src/canon_trading/session_window.py").read_text()
    assert source.count(f'order_id_tag="{custody.ORDER_ID_TAG}"') == 1
    assert session_window.CLOSE_ID_PREFIX == custody.CHECK_CLOSE_PREFIX


def _order(**fields: Any) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": OURS[0],
        "created_at": "2026-11-11T16:54:00Z",
        "updated_at": "2026-11-11T16:54:00Z",
        "submitted_at": "2026-11-11T16:54:00Z",
        "asset_id": str(uuid4()),
        "symbol": "SPY",
        "asset_class": "us_equity",
        "qty": "1",
        "filled_qty": "0",
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": "680.05",
        "status": "new",
        "extended_hours": True,
    }
    return Order.model_validate({**payload, **fields})


def _position(**fields: Any) -> Position:
    payload = {
        "asset_id": str(uuid4()),
        "symbol": "SPY",
        "exchange": "ARCA",
        "asset_class": "us_equity",
        "avg_entry_price": "680.12",
        "qty": "1",
        "side": "long",
        "market_value": "681.00",
        "cost_basis": "680.12",
        "unrealized_pl": "0.88",
        "unrealized_plpc": "0.0013",
        "unrealized_intraday_pl": "0.88",
        "unrealized_intraday_plpc": "0.0013",
        "current_price": "681.00",
        "lastday_price": "679.00",
        "change_today": "0.003",
    }
    payload = {**payload, **fields}
    # Alpaca lists what no order holds beside what is held; a test that wants it absent says so
    payload.setdefault("qty_available", payload["qty"])
    return Position.model_validate(payload)


class Paper:
    """A stand-in for the paper `TradingClient` that answers the four reads and records each call."""

    def __init__(self, *, positions=(), orders=(), no_shorting=True) -> None:
        self.positions = list(positions)
        self.orders = list(orders)
        self.no_shorting = no_shorting
        self.calls: list[tuple[str, Any]] = []

    def get_account(self):
        self.calls.append(("get_account", None))
        return TradeAccount.model_validate(
            {
                "id": ACCOUNT_ID,
                "account_number": ACCOUNT_NUMBER,
                "status": "ACTIVE",
                "currency": "USD",
                "buying_power": "199318.76",
                "cash": "99319.88",
                "portfolio_value": "100000.88",
                "equity": "100000.88",
            }
        )

    def get_all_positions(self):
        self.calls.append(("get_all_positions", None))
        return self.positions

    def get_orders(self, filter: Any = None):
        self.calls.append(("get_orders", filter))
        return self.orders

    def get_account_configurations(self):
        self.calls.append(("get_account_configurations", None))
        return AccountConfiguration.model_validate(
            {**CONFIGURATION, "no_shorting": self.no_shorting}
        )


def data_only_unset(monkeypatch) -> None:
    """Data-only mode off for the test, and whatever was there restored after it, even when the code
    under test sets it (a `setenv` is recorded; a `delenv` of an absent variable is not)."""
    monkeypatch.setenv(custody.DATA_ONLY_ENV, "0")
    monkeypatch.delenv(custody.DATA_ONLY_ENV)


checked: list = []


def test_the_account_read_names_balances_positions_orders_lineage_and_no_shorting(
    capsys, monkeypatch
) -> None:
    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", checked.append)
    ours, foreign, missing = (
        _order(symbol="SPY", side="sell", type="limit", limit_price="679.95"),
        _order(client_order_id=FOREIGN[0], symbol="AAPL", type="market", limit_price=None),
        _order(client_order_id="", symbol="AAPL", side="sell"),
    )
    paper = Paper(positions=[_position()], orders=[foreign, ours, missing])
    checked.clear()
    assert custody.account_task(cast(Any, paper)) == 0
    out = capsys.readouterr().out
    assert checked == [paper]  # checked to refuse all but GET before any read
    assert os.environ[custody.DATA_ONLY_ENV] == "1"
    assert out.splitlines() == [
        "equity: 100000.88",
        "cash: 99319.88",
        "buying_power: 199318.76",
        "positions: 1",
        "  SPY long 1 avg_entry 680.12 market_value 681.00 unrealized_pl 0.88",
        "open orders: 3",
        "  AAPL buy 1 market limit - time_in_force day position_intent not shown status new foreign",
        "  AAPL sell 1 limit limit 680.05 time_in_force day position_intent not shown status new missing",
        "  SPY sell 1 limit limit 679.95 time_in_force day position_intent not shown status new ours",
        "no_shorting: true",
    ]
    for identifier in (ACCOUNT_NUMBER, ACCOUNT_ID, str(ours.id), *OURS[:1], FOREIGN[0]):
        assert identifier not in out
    (_, request), *_ = [c for c in paper.calls if c[0] == "get_orders"]
    assert request.status.value == "open" and request.limit == 500


@pytest.mark.parametrize("intent", ["sell_to_close", "sell_to_open"])
def test_the_account_read_shows_a_gtc_stops_returned_position_intent(
    capsys, monkeypatch, intent: str
) -> None:
    """A protect lineage must not substitute its intended intent for Alpaca's returned value."""
    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", lambda client: None)
    stop = _order(
        client_order_id=PROTECT_STOP,
        side="sell",
        type="trailing_stop",
        order_type="trailing_stop",
        time_in_force="gtc",
        position_intent=intent,
        limit_price=None,
        trail_price="1.00",
        extended_hours=False,
    )
    assert custody.account_task(cast(Any, Paper(orders=[stop]))) == 0
    captured = capsys.readouterr()
    assert (
        "  SPY sell 1 trailing_stop limit - time_in_force gtc "
        f"position_intent {intent} status new ours (a protect stop)"
    ) in captured.out.splitlines()
    assert captured.err == ""
    for identifier in (ACCOUNT_NUMBER, ACCOUNT_ID, str(stop.id), stop.client_order_id):
        assert identifier not in captured.out


@pytest.mark.parametrize(
    "fields",
    [{}, {"position_intent": None}, {"position_intent": ""}],
    ids=["omitted", "null", "empty"],
)
def test_an_unreturned_position_intent_is_not_shown_not_inferred(fields: dict[str, Any]) -> None:
    stop = _order(client_order_id=PROTECT_STOP, side="sell", time_in_force="gtc", **fields)
    assert stop.position_intent is None
    line = custody.order_line(stop)
    assert "time_in_force gtc position_intent not shown status new ours (a protect stop)" in line
    assert "sell_to_close" not in line and PROTECT_STOP not in line


@pytest.mark.parametrize("field", ["time_in_force", "position_intent"])
def test_an_absent_order_field_is_not_shown(field: str) -> None:
    """Defensive model case: the pinned SDK requires time_in_force on validated payloads."""
    order = _order(time_in_force="gtc", position_intent="sell_to_close")
    delattr(order, field)
    line = custody.order_line(order)
    assert f"{field} not shown" in line
    assert line.endswith("status new ours")


def test_a_failed_read_is_named_without_its_body_and_exits_1(capsys, monkeypatch) -> None:
    from alpaca.common.exceptions import APIError

    class Refused(Paper):
        def get_all_positions(self):
            raise APIError(f'{{"code": 40110000, "message": "{ACCOUNT_NUMBER} is not allowed"}}')

    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", lambda client: None)
    assert custody.account_task(cast(Any, Refused())) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "paper:account: the positions failed: APIError" in captured.err
    assert ACCOUNT_NUMBER not in captured.err


def test_the_account_read_builds_a_client_that_refuses_all_but_get(monkeypatch, capsys) -> None:
    """With no client given, the task builds `session_window.paper_trading`'s, in data-only mode: a POST
    is refused before anything is sent (ADR 0070's adapter check)."""
    from canon_alpaca.providers import MethodRefused

    monkeypatch.setenv("ALPACA_API_KEY", "made-up-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "made-up-secret")
    data_only_unset(monkeypatch)
    built: list = []
    real = session_window.paper_trading

    def spy():
        client = real()
        built.append(client)

        def stop() -> None:
            raise OSError("stopped before any request")

        monkeypatch.setattr(
            client, "get_account", stop
        )  # the first read: nothing reaches the network
        return client

    monkeypatch.setattr(session_window, "paper_trading", spy)
    assert custody.account_task() == 1
    (client,) = built
    with pytest.raises(MethodRefused):
        client.submit_order(
            MarketOrderRequest(symbol="SPY", qty=1, side="buy", time_in_force="day")
        )
    err = capsys.readouterr().err
    assert "the account failed: OSError" in err and "made-up" not in err


def _task_body() -> str:
    return tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:account"]["run"]


def _run_task(tmp_path: Path, account: str) -> tuple[subprocess.CompletedProcess, str]:
    """The task's own body with `/usr/bin/security` and `uv` swapped for stand-ins: `security` logs its
    arguments and prints a made-up value; `uv` prints what it was asked and the flags it was given."""
    log = tmp_path / "security.log"
    stub = tmp_path / "security"
    stub.write_text(f'#!/bin/bash\necho "$*" >> {log}\necho "made-up-value-$5"\n')
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/bash\necho "uv $*"\n'
        'echo "data-only=$CANON_ALPACA_DATA_ONLY account=$CANON_PAPER_ACCOUNT"\n'
        '[ -n "$ALPACA_API_KEY" ] && [ -n "$ALPACA_SECRET_KEY" ] && echo "pair given"\n'
    )
    stub.chmod(0o755)
    uv.chmod(0o755)
    body = _task_body().replace("/usr/bin/security", str(stub))
    done = subprocess.run(
        ["bash", "-c", body],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "usage_account": account},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, log.read_text() if log.exists() else ""


@pytest.mark.skipif(not Path("/usr/bin/timeout").exists(), reason="no /usr/bin/timeout here")
@pytest.mark.parametrize(
    ("account", "service"),
    [
        ("tests", "canon-alpaca-paper"),
        ("windows", "canon-alpaca-paper-windows"),
        ("protect", "canon-alpaca-paper-protect"),
    ],
)
def test_the_task_reads_one_named_accounts_pair_in_data_only_mode_and_prints_neither_value(
    tmp_path, account, service
) -> None:
    done, asked = _run_task(tmp_path, account)
    assert done.returncode == 0, done.stderr
    assert asked.splitlines() == [
        f"find-generic-password -s {service} -a ALPACA_API_KEY -w",
        f"find-generic-password -s {service} -a ALPACA_SECRET_KEY -w",
    ]
    assert done.stdout.splitlines() == [
        f"account: {account}",
        (
            "uv run --locked python -c from canon_trading.custody import account_task; "
            "raise SystemExit(account_task())"
        ),
        f"data-only=1 account={account}",
        "pair given",
    ]
    assert "made-up" not in done.stdout + done.stderr


def test_the_task_refuses_any_other_account_before_asking_the_keychain(tmp_path) -> None:
    done, asked = _run_task(tmp_path, "live")
    assert done.returncode == 3 and asked == ""
    assert "no paper account named 'live'" in done.stderr


def test_the_task_takes_no_lock_and_names_its_three_accounts() -> None:
    """GET only, so no lock (the task's note); mise's own `choices` refuses any other account."""
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:account"]
    assert 'choices "tests" "windows" "protect"\n' in task["usage"]
    assert "lockf" not in task["run"]
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", task["run"])


def test_a_client_built_before_data_only_mode_is_refused_before_any_read(
    monkeypatch, capsys
) -> None:
    """A client given to the task that would send more than GET is refused before anything is read."""
    monkeypatch.setenv("ALPACA_API_KEY", "made-up-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "made-up-secret")
    data_only_unset(monkeypatch)
    client = session_window.paper_trading()  # outside data-only mode: its adapter sends any method
    monkeypatch.setattr(client, "get_account", lambda: pytest.fail("Alpaca was read"))
    assert custody.account_task(client) == 1
    assert "would send a method other than GET" in capsys.readouterr().err


def test_the_account_checks_own_close_ids_read_as_ours(monkeypatch) -> None:
    """The ID the check after a window's stop writes, made by its own `_close` with the stamp
    `run_window` gives its report (the form a paper record showed, 2026-11-11), is ours."""
    stamp = datetime(2026, 11, 11, 15, 4, 24, tzinfo=UTC).strftime("%Y%m%dT%H%M%SZ")
    sent: list = []
    check = SimpleNamespace(
        held={"SPY": Decimal(1)},
        closes_sent=0,
        requests={},
        turn=deque(),
        report=SimpleNamespace(started=stamp, closes={}, closes_not_placed=0),
        trading=SimpleNamespace(submit_order=lambda r: sent.append(r) or SimpleNamespace(id=None)),
    )
    for _ in range(3):
        session_window._AccountCheck._close(check, "SPY", "sell")  # ty: ignore[invalid-argument-type]
    ids = [r.client_order_id for r in sent]
    assert len(set(ids)) == 3 and all(custody.lineage(i) == custody.OURS for i in ids)
    source = (ROOT / "src/canon_trading/session_window.py").read_text()
    assert (
        'stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")' in source
    )  # the report's `started`


def test_a_listing_with_something_other_than_orders_is_a_named_read_failure(
    capsys, monkeypatch
) -> None:
    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", lambda client: None)
    paper = Paper(positions=[_position()], orders=[{"symbol": "SPY"}])
    assert custody.account_task(cast(Any, paper)) == 1
    assert (
        "the open orders answered something other than a list of Order" in capsys.readouterr().err
    )


@pytest.mark.parametrize("answer", [None, {"client_order_id": "foreign-id-not-to-print"}, []])
def test_a_lookup_with_something_other_than_an_order_is_a_named_read_failure(
    monkeypatch, answer: object
) -> None:
    venue = Venue(REGULAR)
    monkeypatch.setattr(venue, "get_order_by_client_id", lambda client_id: answer)
    with pytest.raises(custody.ReadFailed, match="the order lookup answered") as failed:
        custody._look_up(cast(Any, venue), OURS[0])
    assert "not an Order" in str(failed.value)
    assert "foreign-id-not-to-print" not in str(failed.value)


@pytest.mark.parametrize("phase", ["cancel", "close", "last-shown"])
def test_a_malformed_lookup_is_named_and_the_remaining_polls_continue(
    monkeypatch, phase: str
) -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    resting = (
        venue.open(client_order_id=OURS[1], symbol="SPY", side="sell")
        if phase == "cancel"
        else None
    )
    lookup = venue.get_order_by_client_id
    seen: dict[str, int] = {}

    def malformed_once(client_id):
        seen[client_id] = seen.get(client_id, 0) + 1
        target = (
            client_id == resting.client_order_id
            if resting
            else client_id.startswith(custody.HAND_CLOSE_PREFIX)
        )
        if target and seen[client_id] == (2 if phase == "last-shown" else 1):
            return {}
        return lookup(client_id)

    monkeypatch.setattr(venue, "get_order_by_client_id", malformed_once)
    report = close(venue)
    assert not report.failed
    assert any("the order lookup" in error and "dict" in error for error in report.errors)
    assert len(venue.sent) == 1 and report.flat
    if resting:
        assert venue.canceled == [resting.client_order_id]


def test_a_malformed_lookup_at_the_close_timeout_names_the_cancel_failure(monkeypatch) -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    lookup = venue.get_order_by_client_id
    lookups = 0
    clocks = Clocks()

    def missing_then_malformed(client_id):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))
        if lookups == 2:
            return {}
        return lookup(client_id)

    monkeypatch.setattr(venue, "get_order_by_client_id", missing_then_malformed)
    report = custody.close_ours(
        cast(Any, venue),
        Quotes(bid="760.10", ask="760.20"),
        sleep=lambda seconds: clocks.sleep(custody.CLOSE_WAIT_SECS),
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )
    assert not report.failed
    (sent,) = venue.sent
    assert (
        "cancel failed: the order lookup answered a dict, not an Order"
        in report.closes[sent.client_order_id]
    )
    assert any(sent.client_order_id in left for left in report.left)
    assert not report.flat


# ---------------------------------------------------------------------------------------------------------
# The close-only form, against a stand-in for Alpaca's paper Trading API and its latest quote


REGULAR = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)  # 10:00 New York, a Wednesday
AFTER_HOURS = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)  # 17:00 New York
SATURDAY = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
NODE_BUY = "O-20261111-145000-001-001-1"


class Venue(Paper):
    """Alpaca's paper Trading API as far as the form uses it: orders kept as alpaca-py `Order` models, a
    position per symbol, the clock and calendar for `now`. A market sell fills at the next lookup; a limit
    sells fills only if `limits_fill`. A cancel of an open order ends it; of an ended one, 422."""

    def __init__(self, now: datetime, *, limits_fill: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.now = now
        self.limits_fill = limits_fill
        self.held: dict[str, Decimal] = {}
        self.history: list[Order] = []
        self.sent: list[Any] = []
        self.canceled: list[str] = []
        self.submit_fails: BaseException | None = None
        self._retry = 3  # alpaca-py's `RESTClient._retry`, which `send_once` sets to 0

    # what the account holds
    def hold(self, symbol: str, qty: int | str, client_order_id: str | None = NODE_BUY) -> None:
        """A position of `qty` that `client_order_id`'s filled buy opened (None: nobody's in the
        lookback)."""
        self.held[symbol] = self.held.get(symbol, Decimal(0)) + Decimal(str(qty))
        if client_order_id is not None:
            self.history.append(
                _order(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    qty=str(qty),
                    filled_qty=str(qty),
                    status="filled",
                    type="market",
                    limit_price=None,
                    submitted_at=(self.now - timedelta(minutes=10)).isoformat(),
                )
            )

    def open(self, **fields: Any) -> Order:
        order = _order(**fields)
        self.history.append(order)
        return order

    def get_all_positions(self):
        self.calls.append(("get_all_positions", None))
        return [
            _position(symbol=s, qty=str(abs(q)), side="long" if q > 0 else "short")
            for s, q in self.held.items()
            if q
        ]

    def get_orders(self, filter: Any = None):
        self.calls.append(("get_orders", filter))
        status = filter.status.value if filter is not None and filter.status else "open"
        found = [
            o
            for o in self.history
            if (status == "all" or _value(o.status) not in session_window.TERMINAL)
            and (not filter.symbols or o.symbol in filter.symbols)
            and (filter.after is None or o.submitted_at > filter.after)
        ]
        return found[: filter.limit or 50]

    def get_clock(self):
        return Clock.model_validate(
            {
                "timestamp": self.now.isoformat(),
                "is_open": time_open(self.now),
                "next_open": "2026-11-12T14:30:00Z",
                "next_close": "2026-11-11T21:00:00Z",
            }
        )

    def get_calendar(self, filters=None):
        return [
            Calendar.model_validate(
                {
                    "date": "2026-11-11",
                    "open": "09:30",
                    "close": "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                    "settlement_date": "2026-11-12",
                }
            ),
            Calendar.model_validate(
                {
                    "date": "2026-11-12",
                    "open": "09:30",
                    "close": "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                    "settlement_date": "2026-11-13",
                }
            ),
        ]

    def submit_order(self, request):
        self.sent.append(request)
        if self.submit_fails is not None:
            raise self.submit_fails
        order = _order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=str(int(request.qty)),
            side=request.side.value,
            type=request.type.value,
            limit_price=None
            if getattr(request, "limit_price", None) is None
            else str(request.limit_price),
            extended_hours=bool(getattr(request, "extended_hours", False)),
            status="accepted",
            submitted_at=self.now.isoformat(),
        )
        self.history.append(order)
        return order

    def _fill(self, order: Order) -> Order:
        if _value(order.status) in session_window.TERMINAL:
            return order
        fills = _value(order.type) == "market" or self.limits_fill
        if not fills or _value(order.side) != "sell":
            return order
        filled = order.model_copy(update={"status": "filled", "filled_qty": order.qty})
        self.history[self.history.index(order)] = filled
        self.held[str(order.symbol)] -= Decimal(str(order.qty))
        return filled

    def get_order_by_client_id(self, client_id):
        for order in self.history:
            if order.client_order_id == client_id:
                return self._fill(order)
        raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))

    def cancel_order_by_id(self, order_id):
        for i, order in enumerate(self.history):
            if str(order.id) == str(order_id):
                if _value(order.status) in session_window.TERMINAL:
                    raise APIError('{"code": 42210000, "message": "not cancelable"}', _status(422))
                self.history[i] = order.model_copy(update={"status": "canceled"})
                self.canceled.append(order.client_order_id)
                return
        raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))


def _status(code: int):
    return SimpleNamespace(response=SimpleNamespace(status_code=code))


def _value(field):
    return str(getattr(field, "value", field))


def time_open(now: datetime) -> bool:
    return now == REGULAR


class Quotes:
    """alpaca-py's `StockHistoricalDataClient.get_stock_latest_quote`, answering one quote per symbol."""

    def __init__(self, bid="1.20", ask="1.24") -> None:
        self.bid, self.ask = bid, ask
        self.asked: list = []

    def get_stock_latest_quote(self, request):
        self.asked.append(request)
        return {
            request.symbol_or_symbols: Quote(
                request.symbol_or_symbols,
                {"t": "2026-11-11T21:59:58Z", "bp": self.bid, "ap": self.ask, "bs": 1, "as": 1},
            )
        }


class Clocks:
    """`sleep` that moves `monotonic` on, so a wait of 60 s takes no time."""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, secs: float) -> None:
        self.t += secs

    def monotonic(self) -> float:
        return self.t


class ReplacedNotListedOpen(Venue):
    """A venue whose listing of open orders does not hold a replaced order, so nothing but the
    quantity rule stands between a chain and the close."""

    def get_orders(self, filter: Any = None):
        found = super().get_orders(filter)
        if filter is not None and filter.status and filter.status.value == "all":
            return found
        return [order for order in found if _value(order.status) != "replaced"]


class PositionShows(Venue):
    """A venue whose position in one symbol is listed with a quantity of the test's choosing."""

    shown: dict[str, Any]

    def get_all_positions(self):
        return [
            position.model_copy(update=self.shown[position.symbol])
            if position.symbol in self.shown
            else position
            for position in super().get_all_positions()
        ]


def close(venue: Venue, data: Any = None):
    clocks = Clocks()
    return custody.close_ours(
        cast(Any, venue),
        data if data is not None else Quotes(),
        sleep=clocks.sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )


PROTECT_STOP = "canon-protect-20261111T150000Z-1-6168c4fe"
"""A trailing stop the protect task sent (`canon_trading.protect`)."""


def protect_stop(venue: Venue, **fields: Any) -> Order:
    return venue.open(
        **{
            "client_order_id": PROTECT_STOP,
            "symbol": "SPY",
            "side": "sell",
            "qty": "10",
            "type": "trailing_stop",
            "order_type": "trailing_stop",
            "limit_price": None,
            "time_in_force": "gtc",
            "extended_hours": False,
            "submitted_at": (venue.now - timedelta(minutes=5)).isoformat(),
            **fields,
        }
    )


def test_a_protect_stop_is_a_lineage_of_its_own_neither_ours_to_cancel_nor_foreign() -> None:
    assert custody.lineage(PROTECT_STOP) == custody.PROTECT
    assert custody.PROTECT not in (custody.OURS, custody.FOREIGN, custody.MISSING)
    for other in ("canon-protect-", "canon-protect-manual", "canon-protect-20261111T150000Z-1"):
        assert custody.lineage(other) == custody.FOREIGN


@pytest.mark.parametrize("opened_by", [None, NODE_BUY], ids=["somebody-elses-long", "our-own-long"])
def test_the_close_never_cancels_a_protect_stop_and_leaves_its_symbol_alone(opened_by) -> None:
    """The stop guards a long that need not be ours: a close-only run that canceled it would leave
    that long with nothing. So the symbol is frozen and named, whoever opened the long, and the stop
    stays; to close such a long, its stop is taken off first, on purpose."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 10, client_order_id=opened_by)
    stop = protect_stop(venue)
    report = close(venue)
    assert venue.sent == [] and venue.canceled == []
    why = report.frozen["SPY"][0]
    assert why.startswith(f"open a protect stop of ours, {PROTECT_STOP} (sell 10 SPY trailing_stop")
    assert "never cancels" in why
    assert any(
        o.client_order_id == stop.client_order_id and _value(o.status) == "new"
        for o in venue.history
    )


def test_a_protect_stop_that_triggered_is_not_a_sale_by_our_orders() -> None:
    """Ten of somebody else's shares were sold by a protect stop, and a node of ours then bought 5:
    our orders opened 5 and 5 are held, so they are ours to close. Counted as a sale of ours, the
    stop's fill would put ours at minus 5 and freeze them."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 5)
    protect_stop(venue, filled_qty="10", status="filled")
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 5)


def test_the_account_read_names_a_protect_stop_as_ours() -> None:
    venue = Venue(REGULAR)
    line = custody.order_line(protect_stop(venue))
    assert line.endswith("status new ours (a protect stop)") and PROTECT_STOP not in line


def test_in_the_regular_session_ours_is_closed_at_market_and_the_account_left_flat() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    report = close(venue)
    (sent,) = venue.sent
    assert isinstance(sent, MarketOrderRequest)
    assert (sent.symbol, sent.qty, _value(sent.side), _value(sent.time_in_force)) == (
        "SPY",
        1,
        "sell",
        "day",
    )
    assert sent.client_order_id.startswith(custody.HAND_CLOSE_PREFIX)
    assert custody.lineage(sent.client_order_id) == custody.OURS
    assert report.part == "regular"
    assert report.found == ["position of 1 SPY (ours opened 1)"]
    assert report.flat and not report.frozen and venue.held["SPY"] == 0
    assert "last shown: filled, filled 1" in report.closes[sent.client_order_id]


def test_a_foreign_open_order_freezes_its_symbol_and_is_never_touched() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    foreign = venue.open(client_order_id=FOREIGN[0], symbol="SPY", side="sell")
    ours = venue.open(client_order_id="O-20261111-145500-001-001-2", symbol="SPY", side="buy")
    report = close(venue)
    assert venue.sent == []  # no close in a frozen symbol
    assert venue.canceled == [ours.client_order_id]  # ours canceled, the foreign one never
    assert report.frozen["SPY"] == ["open a foreign order: sell 1 SPY limit, new"]
    assert FOREIGN[0] not in "\n".join(report.lines())
    assert report.left == []  # a frozen symbol's position is reported frozen, never as ours
    assert report.flat and report.frozen
    assert any(
        o.client_order_id == foreign.client_order_id and _value(o.status) == "new"
        for o in venue.history
    )


@pytest.mark.parametrize("symbol", [None, ""])
def test_an_open_order_with_no_symbol_is_still_canceled_if_ours_and_frozen_if_foreign(
    symbol: str | None,
) -> None:
    """Antigravity #345 F6; alpaca-py normalizes empty strings, so also test a copied empty field."""
    venue = Venue(REGULAR)
    ours = venue.open(client_order_id=OURS[0], symbol=None, status="new")
    foreign = venue.open(
        client_order_id="manual-mleg-1", symbol=None, order_class="mleg", status="new"
    )
    assert ours.symbol is None and foreign.symbol is None
    if symbol == "":
        venue.history = [order.model_copy(update={"symbol": ""}) for order in venue.history]

    report = close(venue)
    assert venue.canceled == [OURS[0]]
    assert report.frozen
    assert "(no symbol)" in report.frozen
    assert venue.sent == []
    assert _value(venue.history[-1].status) == "new"
    assert foreign.client_order_id not in "\n".join(report.lines())
    assert any("(no symbol)" in line and "foreign" in line for line in report.lines())


@pytest.mark.parametrize("mine", [True, False], ids=["ours", "foreign"])
def test_a_read_names_an_open_order_that_has_no_symbol(mine: bool) -> None:
    venue = Venue(REGULAR)
    unknown = venue.open(client_order_id=OURS[0] if mine else "manual-mleg-1", symbol=None)

    holdings = custody.read_holdings(cast(Any, venue), venue.now)
    assert len(holdings) == 1
    (holding,) = holdings
    assert holding.symbol == "(no symbol)"
    assert (holding.ours_open if mine else holding.foreign_open) == [unknown]
    assert holding.opened is None
    assert any("no symbol" in why for why in holding.frozen)
    assert any("(no symbol)" in line for line in custody.account_lines(cast(Any, venue)))
    assert not any(getattr(request, "symbols", None) for _, request in venue.calls)


@pytest.mark.parametrize("mine", [True, False], ids=["ours", "foreign"])
def test_an_unattributed_open_order_freezes_every_listed_symbol_for_the_run(mine: bool) -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.hold("AAPL", 2, client_order_id=OURS[1])
    unknown = venue.open(client_order_id=OURS[2] if mine else "manual-mleg-1", symbol=None)
    resting = venue.open(client_order_id=OURS[0], symbol="SPY", side="sell")
    foreign = venue.open(client_order_id="manual-qqq", symbol="QQQ", side="buy")

    report = close(venue)
    assert venue.sent == []
    expected = (
        [unknown.client_order_id, resting.client_order_id] if mine else [resting.client_order_id]
    )
    assert set(venue.canceled) == set(expected)
    assert set(report.canceled) == set(expected)
    assert foreign.client_order_id not in venue.canceled
    for symbol in ("SPY", "AAPL", "QQQ", "(no symbol)"):
        assert symbol in report.frozen
        assert any("no symbol" in why for why in report.frozen[symbol])
    assert report.flat and not report.failed
    assert venue.held == {"SPY": Decimal(1), "AAPL": Decimal(2)}


def test_an_unattributed_order_of_ours_still_open_after_cancel_is_named_as_left(
    monkeypatch,
) -> None:
    venue = Venue(REGULAR)
    ours = venue.open(client_order_id=OURS[0], symbol=None)

    def refused(order_id):
        raise APIError('{"code": 42210000, "message": "not cancelable"}', _status(422))

    monkeypatch.setattr(venue, "cancel_order_by_id", refused)
    report = close(venue)
    assert not report.flat
    assert any(ours.client_order_id in left and "(no symbol)" in left for left in report.left)
    assert "(no symbol)" in report.frozen
    assert venue.sent == []


def test_an_unattributed_order_also_freezes_a_position_first_listed_after_cancel(
    monkeypatch,
) -> None:
    venue = Venue(REGULAR)
    ours = venue.open(client_order_id=OURS[0], symbol=None)
    cancel = venue.cancel_order_by_id

    def cancel_then_a_position_appears(order_id):
        cancel(order_id)
        venue.hold("SPY", 2)

    monkeypatch.setattr(venue, "cancel_order_by_id", cancel_then_a_position_appears)
    report = close(venue)
    assert venue.canceled == [ours.client_order_id]
    assert venue.sent == []
    assert any("at the first read" in why for why in report.frozen["SPY"])
    assert venue.held["SPY"] == 2


def test_a_foreign_open_order_without_a_symbol_makes_the_close_task_report_frozen(capsys) -> None:
    venue = Venue(REGULAR)
    venue.open(client_order_id="manual-mleg-1", symbol=None, order_class="mleg")
    clocks = Clocks()
    code = custody.close_task(
        cast(Any, venue),
        Quotes(),
        sleep=clocks.sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )
    assert code == 2
    assert "frozen (no symbol):" in capsys.readouterr().out
    assert venue.sent == [] and venue.canceled == []


def test_an_unattributed_order_names_a_position_first_listed_at_the_last_read(monkeypatch) -> None:
    venue = Venue(REGULAR)
    ours = venue.open(client_order_id=OURS[0], symbol=None)
    get_positions = venue.get_all_positions
    reads = 0

    def position_only_at_the_end():
        nonlocal reads
        reads += 1
        if reads == 3:  # initial read, after cancel, then the final read
            venue.hold("SPY", 2)
        return get_positions()

    monkeypatch.setattr(venue, "get_all_positions", position_only_at_the_end)
    report = close(venue)
    assert venue.canceled == [ours.client_order_id]
    assert venue.sent == []
    assert any("SPY" in why and "no symbol" in why for why in report.frozen["SPY"])
    assert any("frozen SPY:" in line for line in report.lines())
    assert not any("SPY" in left for left in report.left)
    assert venue.held["SPY"] == 2


def test_a_quantity_ours_did_not_open_freezes_the_symbol() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.hold("SPY", 2, client_order_id=FOREIGN[0])  # the owner's buy beside ours
    venue.hold("AAPL", 5, client_order_id=None)  # held from before the lookback, by nobody we know
    report = close(venue)
    assert venue.sent == [] and venue.canceled == []
    assert report.frozen == {
        "AAPL": ["5 AAPL held, but our orders opened 0 in the last 1 day, 0:00:00"],
        "SPY": ["3 SPY held, but our orders opened 1 in the last 1 day, 0:00:00"],
    }


def test_a_short_is_never_bought_back() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", -1, client_order_id=None)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == ["a short of 1 SPY: never bought back here"]
    listed = [f for name, f in venue.calls if name == "get_orders"]
    assert all(_value(f.status) == "open" for f in listed)  # a frozen symbol's history is not read


def test_a_full_listing_of_the_symbols_orders_freezes_it() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    for n in range(2, custody.ORDERS_PAGE_LIMIT + 1):
        venue.history.append(
            _order(client_order_id=f"O-20261111-145000-001-001-{n}", status="canceled")
        )
    report = close(venue)
    assert venue.sent == []
    assert "the quantity ours opened is not established" in report.frozen["SPY"][0]


def test_outside_the_regular_session_a_marked_limit_through_the_touch_is_canceled_after_60_s() -> (
    None
):
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    quotes = Quotes(bid="760.10", ask="760.20")
    report = close(venue, quotes)
    (sent,) = venue.sent
    assert isinstance(sent, LimitOrderRequest)
    assert (sent.limit_price, sent.extended_hours, _value(sent.time_in_force)) == (
        760.05,
        True,
        "day",
    )
    (asked,) = quotes.asked
    assert (asked.symbol_or_symbols, _value(asked.feed)) == ("SPY", "sip")  # after-hours' feed
    log = report.closes[sent.client_order_id]
    assert log[0] == (
        "sent: sell 1 SPY limit 760.05, DAY, extended hours "
        "(sip quote at 21:59:58 UTC, bid 760.1, ask 760.2)"
    )
    assert "canceled after 60 s" in log and venue.canceled == [sent.client_order_id]
    assert report.left == ["position of 1 SPY (ours opened 1)"]  # not flat, by name


def test_outside_the_regular_session_a_limit_that_fills_leaves_the_account_flat() -> None:
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue)
    assert report.flat and venue.canceled == []


def test_a_holding_under_five_cents_is_closed_at_the_lowest_step_and_the_record_says_so() -> None:
    """The lead, 2026-11-12: a close exists to close. A bid of 0.04 less the five-cent offset is under
    Alpaca's finest step, so paper:close sends its sell at 0.0001, names that in its record, and the
    account is left flat; refusing it would leave the position open."""
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0.04", ask="0.05"))
    (sent,) = venue.sent
    assert sent.limit_price == 0.0001
    log = report.closes[sent.client_order_id]
    assert "less 0.05 is -0.01, under Alpaca's finest step" in log[0]
    assert "sent at the lowest, 0.0001" in log[0]
    assert report.flat


def test_a_bid_in_pennies_with_an_ask_that_is_not_sends_no_close_and_names_the_refusal() -> None:
    """The lead, 2026-11-12: a bid of 0.04 with an ask of 52.10 is a quote too wide to close on.
    paper:close sends nothing for it and names why; the position stays, by name."""
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0.04", ask="52.10"))
    assert venue.sent == []
    (error,) = report.errors
    assert "a quote too wide to close on" in error and "ask 52.1" in error
    assert not report.flat


def test_no_bid_sends_no_close_and_says_why() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0", ask="0"))
    assert venue.sent == []
    assert report.errors == ["SPY: no price for the close: ValueError"]
    assert not report.flat


def test_our_open_order_is_canceled_and_confirmed_ended_before_the_close() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    exit_order = venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    report = close(venue)
    assert venue.canceled[0] == exit_order.client_order_id
    assert report.canceled == [exit_order.client_order_id]
    assert len(venue.sent) == 1 and report.flat


def test_a_close_whose_answer_is_lost_is_looked_up_never_sent_again() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.submit_fails = requests.ReadTimeout("read timed out")
    report = close(venue)
    assert len(venue.sent) == 1  # one attempt
    (log,) = report.closes.values()
    assert log[1] == "no answer: ReadTimeout"
    assert "not found 60 s after it was sent" in log
    assert report.unfound == list(report.closes)  # it may yet appear, so it is named
    assert any(line.startswith("unfound: ") for line in report.lines())
    assert not report.flat


def test_a_close_alpaca_refuses_is_named_not_placed() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.submit_fails = APIError('{"code": 40310000, "message": "insufficient qty"}', _status(403))
    report = close(venue)
    (log,) = report.closes.values()
    assert log[1] == "not placed: APIError (HTTP 403)"
    assert report.left == ["position of 1 SPY (ours opened 1)"]


def test_with_no_session_open_nothing_is_sent_and_ours_is_left_by_name() -> None:
    venue = Venue(SATURDAY)
    venue.hold("SPY", 1)
    report = close(venue)
    assert venue.sent == [] and report.part == ""
    (error,) = report.errors
    assert error.startswith("SPY: no session part is open (2026-11-14 is not a trading day")
    assert error.endswith("), so no close was sent")
    assert report.left == ["position of 1 SPY (ours opened 1)"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [("flat", 0), ("ours", 0), ("frozen", 2), ("left", 1)],
)
def test_the_close_task_exits_by_what_is_left(setup, code, capsys) -> None:
    venue = Venue(REGULAR if setup != "left" else SATURDAY)
    if setup in ("ours", "left"):
        venue.hold("SPY", 1)
    if setup == "frozen":
        venue.hold("AAPL", 5, client_order_id=None)
    clocks = Clocks()
    assert (
        custody.close_task(
            cast(Any, venue),
            Quotes(),
            sleep=clocks.sleep,
            monotonic=clocks.monotonic,
            now=lambda: venue.now,
        )
        == code
    )
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("left: ")


def test_the_close_task_takes_the_accounts_lock_and_reads_its_pair_only() -> None:
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:close"]
    run = task["run"]
    assert 'choices "tests" "windows"' in task["usage"]
    assert "tests) svc=canon-alpaca-paper; lock=canon-paper-test.lock ;;" in run
    assert "windows) svc=canon-alpaca-paper-windows; lock=canon-paper-windows.lock ;;" in run
    assert "close_task" in run and 'lock_wait 9 "$lockwait" ||' in run
    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    block = re.compile(r"^lock_wait\(\) \{\n.*?^\}$", re.MULTILINE | re.DOTALL)
    found, want = block.search(run), block.search(tasks["paper:test"]["run"])
    # paper:test's text, which scripts/test-lanes holds the other paper tasks to
    assert found and want and found.group(0) == want.group(0)
    assert not re.search(r"/usr/bin/lockf -t [^0 ]", run)  # lockf's descriptor form busy-loops
    assert "CANON_ALPACA_DATA_ONLY" not in run
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", run)


def test_a_failure_part_way_cancels_the_close_it_sent_and_is_never_flat() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    looked_up = venue.get_order_by_client_id
    lookups = []

    def lookup_fails_once(client_id):
        lookups.append(client_id)
        if len(lookups) == 1:
            raise KeyError("a bug in the poll")
        return looked_up(client_id)

    venue.get_order_by_client_id = lookup_fails_once
    report = close(venue, Quotes(bid="760.10", ask="760.20"))
    (sent,) = venue.sent
    assert venue.canceled == [sent.client_order_id]  # not left working
    assert report.failed == "KeyError part way through"
    assert "canceled after the failure" in report.closes[sent.client_order_id]
    assert report.left == ["position of 1 SPY (ours opened 1)"] and not report.flat
    assert "failed: KeyError part way through" in report.lines()


def test_only_the_cancels_and_the_submit_are_sent_once_and_the_reads_keep_their_retries(
    monkeypatch,
) -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(session_window, "send_once", lambda t: setattr(t, "_retry", 0))
    for name in ("get_all_positions", "get_orders", "submit_order", "cancel_order_by_id"):
        call = getattr(venue, name)

        def spy(*args, _name=name, _call=call):
            seen.append((_name, venue._retry))
            return _call(*args)

        setattr(venue, name, spy)
    close(venue)
    assert {r for name, r in seen if name.startswith("get_")} == {3}
    assert {r for name, r in seen if name in ("submit_order", "cancel_order_by_id")} == {0}
    assert venue._retry == 3


def test_the_part_is_read_again_after_the_cancel_wait() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    cancel = venue.cancel_order_by_id

    def cancel_then_the_bell(order_id):
        cancel(order_id)
        venue.now = AFTER_HOURS  # the regular session ended while the cancel was waited on

    venue.cancel_order_by_id = cancel_then_the_bell
    report = close(venue)
    (sent,) = venue.sent
    assert isinstance(sent, LimitOrderRequest) and sent.extended_hours
    assert report.part == "after-hours"


def test_a_read_that_fails_is_named_and_the_run_is_never_flat() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)

    def positions_fail():
        raise APIError('{"code": 50010000, "message": "internal"}', _status(500))

    venue.get_all_positions = positions_fail
    report = close(venue)
    assert venue.sent == []
    assert report.failed == "the positions failed: APIError (HTTP 500) part way through"
    assert report.left == ["unknown: the account could not be read at the end"]
    assert not report.flat


def test_on_an_account_that_may_short_no_long_is_ours_to_close() -> None:
    venue = Venue(REGULAR, no_shorting=False)
    venue.hold("SPY", 1)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"][0].startswith("the account is not long-only (no_shorting is false)")


def test_a_sigterm_part_way_cancels_the_close_it_sent(capsys) -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    clocks = Clocks()

    def sleep(secs: float) -> None:
        clocks.sleep(secs)
        if venue.sent:  # the close is working: the task's timeout sends TERM, then another
            signal.raise_signal(signal.SIGTERM)

    before = signal.getsignal(signal.SIGTERM)
    code = custody.close_task(
        cast(Any, venue),
        Quotes(bid="760.10", ask="760.20"),
        sleep=sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )
    assert code == 1
    (sent,) = venue.sent
    assert venue.canceled == [sent.client_order_id]
    out = capsys.readouterr().out
    assert "failed: Stopped part way through" in out
    assert "canceled after the failure" in out
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_close_that_ends_just_before_its_cancel_is_not_named_a_failed_cancel() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    cancel = venue.cancel_order_by_id

    def fills_first(order_id):
        venue.limits_fill = True  # it fills between the last poll and the cancel
        for order in venue.history:
            if str(order.id) == str(order_id):
                venue._fill(order)
        cancel(order_id)

    venue.cancel_order_by_id = fills_first
    report = close(venue, Quotes(bid="760.10", ask="760.20"))
    (log,) = report.closes.values()
    assert "cancel refused (HTTP 422): it had ended; last shown below" in log
    assert log[-1] == "last shown: filled, filled 1"
    assert report.flat


def test_the_data_client_is_built_once_for_the_run() -> None:
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    venue.hold("QQQ", 2, client_order_id="O-20261111-145000-001-001-9")
    built: list[Quotes] = []

    def factory() -> Quotes:
        built.append(Quotes())
        return built[-1]

    report = close(venue, factory)
    assert len(venue.sent) == 2 and report.flat
    assert len(built) == 1


def test_a_foreign_order_by_notional_is_named_by_its_notional() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id=FOREIGN[0], symbol="SPY", side="buy", qty=None, notional="100")
    report = close(venue)
    assert report.frozen["SPY"] == ["open a foreign order: buy notional 100 SPY limit, new"]


# -- A sell chain of ours with fills on two consecutive links (the lead's ruling, 2026-11-13) -------
#
# Custody read a replacement's count as carrying its predecessor's, which one paper observation
# showed overnight (ADR 0042) and Alpaca's page does not state. Were a replacement to count its own
# fills only, a sell chain's sales would be under-counted and the close could sell shares ours did
# not open. So such a chain freezes its symbol until a regular-session paper read shows the count.

SELL = "O-20261111-145500-001-001-2"


@pytest.mark.parametrize(
    ("filled", "by_hand"),
    [
        (("3", "5"), ()),  # ours, then ours
        (("3", "5"), (1,)),  # ours, then a hand's replacement
        (("3", "5", "8"), (1,)),  # ours, a hand's, ours again
        (("0", "3", "5"), ()),  # nothing, then fills on two links running
        (("3", "3"), ()),  # the replacement shows no more than the order before it
    ],
    ids=["ours-ours", "ours-foreign", "ours-foreign-ours", "zero-then-two", "equal"],
)
def test_a_sell_chain_of_ours_with_fills_on_two_links_running_is_not_established(
    filled: tuple[str, ...], by_hand: tuple[int, ...]
) -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 10)
    chain(venue, *filled, side="sell", by_hand=by_hand, first=SELL)
    quantity, why = opened(venue)
    assert quantity is None, (quantity, why)
    assert isinstance(why, custody._Unread)
    assert "SPY" in why and "not established" in why
    assert "two orders running" in why, why  # the reason
    assert "ADR 0042" in why and "regular-session paper read" in why, why  # and what lifts it


def test_the_close_sells_nothing_on_a_sell_chain_with_fills_on_two_links_running() -> None:
    """Ours bought 10. Our sell filled 3 and its replacement shows 5. Carrying the 3, ours sold 5 and
    holds 5; counting its own, ours sold 8 and holds 2. Five are held, three of them perhaps a
    hand's: nothing is sold, our resting order is canceled, and the symbol is named."""
    venue = ReplacedNotListedOpen(REGULAR)
    venue.hold("SPY", 10)
    venue.held["SPY"] = Decimal(5)
    chain(venue, "3", "5", side="sell", first=SELL)
    resting = venue.open(client_order_id=OURS[1], symbol="SPY", side="sell", limit_price="999")
    report = close(venue)
    assert not report.failed and not report.errors, report.lines()
    assert venue.sent == [] and venue.held["SPY"] == Decimal(5)
    assert venue.canceled == [resting.client_order_id]
    assert any("two orders running" in why for why in report.frozen["SPY"]), report.lines()
    assert report.left == []  # frozen and named, not counted as ours left


@pytest.mark.parametrize(
    ("filled", "side", "by_hand", "expected"),
    [
        (("3", "5"), "buy", (), "5"),  # a buy chain: a wrong count there only freezes
        (("0", "5"), "sell", (), "5"),  # a replacement after an order that filled nothing
        (("5", "0"), "sell", (), "5"),  # a zero tail carries nothing
        (("5",), "sell", (), "5"),  # no replacement
    ],
    ids=["buy-chain", "sell-after-zero", "sell-zero-tail", "sell-alone"],
)
def test_the_chains_that_freeze_leaves_counted(
    filled: tuple[str, ...], side: str, by_hand: tuple[int, ...], expected: str
) -> None:
    venue = Venue(REGULAR)
    if side == "sell":
        venue.hold("SPY", 10)
        chain(venue, *filled, side="sell", by_hand=by_hand, first=SELL)
        assert opened(venue) == (Decimal(10) - Decimal(expected), "")
    else:
        chain(venue, *filled, by_hand=by_hand)
        assert opened(venue) == (Decimal(expected), "")


def test_a_hands_own_sell_chain_with_fills_on_two_links_running_leaves_our_count() -> None:
    """Ours bought 10. A hand's sell filled 3 and the hand's replacement shows 5: neither is ours,
    so nothing of ours is subtracted under either reading and our 10 stay counted."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 10)
    first, second = str(uuid4()), str(uuid4())
    for n, (order_id, filled) in enumerate(((first, "3"), (second, "5"))):
        venue.open(
            id=order_id,
            client_order_id=FOREIGN[n],
            symbol="SPY",
            side="sell",
            qty="20",
            filled_qty=filled,
            status="filled" if n else "replaced",
            replaces=first if n else None,
            replaced_by=None if n else second,
            submitted_at=(venue.now - timedelta(minutes=10 - n)).isoformat(),
        )
    assert opened(venue) == (Decimal(10), "")
