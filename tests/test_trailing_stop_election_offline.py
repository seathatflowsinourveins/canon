"""Offline checks of the election case of the trailing stop's paper proof
(`tests/test_paper_venue_trailing_stop_election.py`), so that its first paper run is not the first run of
its own parts: each step of its strategy on a stand-in, and the reader of what Alpaca made of the sale.
No request reaches Alpaca and no credential is read. The file's name keeps it out of the paper task's
`test_paper_*` selection.

What these cannot show is what the paper run is for: how Alpaca elects the stop and books its fill.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType

from canon_trading import custody
from tests import test_paper_protect_seam as seam
from tests import test_paper_venue_trailing_stop as paper
from tests import test_paper_venue_trailing_stop_election as election
from tests import test_trailing_stop_paper_proof_offline as offline

OURS = offline.OURS
SINCE = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


class Walk(offline.Walk):
    """The proof's stand-in for the strategy, with the election case's own two methods."""

    _step = election.Elected._step
    _sell = election.Elected._sell
    _ended = election.Elected._ended
    on_order_event = election.Elected.on_order_event

    def __init__(self) -> None:
        super().__init__()
        self.cancel_refusals: list[str] = []

    def _trailing(self, *positional: Any, **named: Any) -> SimpleNamespace:
        order = super()._trailing(*positional, **named)
        order.filled_qty = Quantity.from_int(0)
        return order


def working() -> Walk:
    """The walk as far as the stop working at Alpaca: a long of one share adopted, the stop placed."""
    walk = Walk()
    assert walk.step() == "waiting for the long"
    walk.long = Decimal(election.ONE)
    assert walk.step() == "placing"
    assert walk.step() == "waiting for the sell to work"
    assert walk.order is not None
    assert walk.step() == "waiting for the sell to work"  # submitted, not yet acknowledged
    walk.order.is_open = True
    assert walk.step() == "waiting for the election"
    return walk


def end(walk: Walk, status: str, filled: int = 0) -> None:
    assert walk.order is not None
    walk.order.is_open, walk.order.is_closed = False, True
    walk.order.status = SimpleNamespace(name=status)
    walk.order.filled_qty = Quantity.from_int(filled)


def test_the_stop_is_a_day_trailing_sell_one_cent_under_the_mark_for_the_one_share() -> None:
    walk = working()
    ((positional, named),) = walk.made
    assert positional == (paper.AAPL, OrderSide.SELL, Quantity.from_int(1), Decimal("0.01"))
    assert named == {
        "trailing_offset_type": TrailingOffsetType.PRICE,
        "time_in_force": TimeInForce.DAY,
    }
    assert walk.modified == [] and walk.canceled == []


def test_the_walk_waits_for_the_nodes_order_to_fill_and_changes_nothing_meanwhile() -> None:
    walk = working()
    for _ in range(3):
        assert walk.step() == "waiting for the election"
    assert walk.modified == [] and walk.canceled == []
    end(walk, "FILLED", 1)
    assert walk.step() == "done"
    assert walk.outcome == "done" and walk.stopped == ["done"]
    assert walk.seen["the node's order ended"] == "FILLED"
    assert walk.seen["filled at the node"] == "1"
    assert "elected" not in walk.seen
    for name in ("adopted", "stop working", "stop ended"):
        assert name in walk.times, name


def test_a_stop_elected_before_the_node_saw_it_working_is_an_election_too() -> None:
    walk = Walk()
    walk.long = Decimal(election.ONE)
    assert walk.step() == "placing"
    assert walk.step() == "waiting for the sell to work"
    end(walk, "FILLED", 1)
    assert walk.step() == "done"
    assert walk.outcome == "done" and walk.seen["the node's order ended"] == "FILLED"


@pytest.mark.parametrize("status", ["REJECTED", "DENIED", "CANCELED", "EXPIRED"])
def test_a_stop_that_ends_any_other_way_ends_the_walk_by_its_status(status: str) -> None:
    walk = working()
    end(walk, status)
    assert walk.step() == "done"
    assert walk.outcome == f"the trailing stop ended {status} with no election"
    assert walk.canceled == []


def test_a_stop_not_elected_in_the_time_is_canceled_and_the_walk_says_so() -> None:
    walk = working()
    walk.times["stop working"] -= int((election.ELECT_SECS + 1) * 1e9)
    assert walk.step() == "waiting for the sell's cancel"
    assert walk.canceled == [OURS]
    assert walk.seen["elected"] == "no: canceled after the wait"
    assert walk.step() == "waiting for the sell's cancel"
    end(walk, "CANCELED")
    assert walk.step() == "done" and walk.outcome == "done"
    assert walk.seen["elected"] == "no: canceled after the wait"


def test_a_stop_filled_while_its_cancel_was_on_the_way_is_still_seen_filled() -> None:
    """The cancel can lose the race with the election: the walk then ends done with the order's own
    end, and the test reads the fill from it."""
    walk = working()
    walk.times["stop working"] -= int((election.ELECT_SECS + 1) * 1e9)
    assert walk.step() == "waiting for the sell's cancel"
    end(walk, "FILLED", 1)
    assert walk.step() == "done"
    assert walk.seen["the node's order ended"] == "FILLED"
    assert walk.seen["elected"] == "yes: while the walk's cancel was on its way"
    walk.events = [("OrderFilled", OURS, 0)]
    assert election.verdict(walk, [SOLD], Decimal(0), {"orders": 0}) == []


class OrderCancelRejected:
    def __init__(self, reason: str) -> None:
        self.client_order_id = SimpleNamespace(value=OURS)
        self.reason = reason


def test_a_refused_cancel_ends_the_walk_by_name_instead_of_waiting_out_the_limit() -> None:
    walk = working()
    walk.times["stop working"] -= int((election.ELECT_SECS + 1) * 1e9)
    assert walk.step() == "waiting for the sell's cancel"
    walk.on_order_event(OrderCancelRejected("Alpaca refused the cancel (422): 42210000: no"))
    assert walk.step() == "done"
    assert walk.outcome == "the stop's cancel was refused"
    assert walk.cancel_refusals == ["Alpaca refused the cancel (422): 42210000: no"]
    assert walk.modify_refusals == []


def test_a_fill_already_at_the_node_when_the_limit_passes_is_the_walks_end_not_a_time_out() -> None:
    """The fill can reach the node as the walk's own limit runs out. The order's end is read before
    the limit: a walk that timed out without recording it would have the verdict say the node's order
    missed a fill it got, which is ADR 0129's condition (b)."""
    walk = working()
    end(walk, "FILLED", 1)
    walk.events = [("OrderFilled", OURS, 0)]
    assert walk.started_ns is not None
    walk.started_ns = time.time_ns() - walk.limit_ns - 1
    assert walk.step() == "done"
    assert walk.outcome == "done"
    assert walk.seen["the node's order ended"] == "FILLED"
    assert election.verdict(walk, [SOLD], Decimal(0), {"orders": 0}) == []


def test_a_stop_that_ended_unfilled_as_the_limit_passes_is_named_by_its_end() -> None:
    walk = working()
    end(walk, "EXPIRED")
    assert walk.started_ns is not None
    walk.started_ns = time.time_ns() - walk.limit_ns - 1
    assert walk.step() == "done"
    assert walk.outcome == "the trailing stop ended EXPIRED with no election"
    assert walk.seen["the node's order ended"] == "EXPIRED"


def test_a_wait_past_the_limit_ends_the_walk_by_the_step_it_was_in() -> None:
    walk = working()
    walk.limit_ns = 0
    assert walk.step() == "done"
    assert walk.outcome == "timed out while waiting for the election"


def alpaca_order(**changes: Any) -> SimpleNamespace:
    """A closed order as alpaca-py's `Order` shows one: enums with a `value`, numbers as text."""
    fields: dict[str, Any] = {
        "id": "5b1f0a3e-0000-4000-8000-00000000000a",
        "client_order_id": OURS,
        "created_at": SINCE + timedelta(seconds=5),
        "type": SimpleNamespace(value="trailing_stop"),
        "side": SimpleNamespace(value="sell"),
        "status": SimpleNamespace(value="filled"),
        "qty": "1",
        "filled_qty": "1",
        "position_intent": SimpleNamespace(value="sell_to_close"),
        "replaces": None,
        "replaced_by": None,
        "legs": None,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


class Trading:
    def __init__(self, orders: list[SimpleNamespace]) -> None:
        self.orders = orders
        self.asked: list[Any] = []

    def get_orders(self, filter: Any = None) -> list[SimpleNamespace]:
        self.asked.append(filter)
        return self.orders


def test_the_sells_since_the_start_are_read_oldest_first_with_no_id_price_or_time() -> None:
    market = alpaca_order(
        id="5b1f0a3e-0000-4000-8000-00000000000b",
        client_order_id="0f6d2c1e-5d2b-4c0e-9d57-7f1f0a3e2b11",
        created_at=SINCE + timedelta(seconds=9),
        type=SimpleNamespace(value="market"),
        position_intent=None,
        replaces="5b1f0a3e-0000-4000-8000-00000000000a",
    )
    buy = alpaca_order(side=SimpleNamespace(value="buy"), created_at=SINCE)
    stop = alpaca_order(
        status=SimpleNamespace(value="replaced"),
        filled_qty="0",
        replaced_by="5b1f0a3e-0000-4000-8000-00000000000b",
    )
    trading = Trading([market, buy, stop])

    sells = election.sells_since(trading, SINCE, OURS)

    assert sells == [
        {
            "which": "the stop's own client order ID",
            "type": "trailing_stop",
            "status": "replaced",
            "quantity": "1",
            "filled": "0",
            "position intent": "sell_to_close",
            "replaces another order": "no",
            "replaced by another order": "yes",
            "legs": "0",
        },
        {
            "which": f"another client order ID ({custody.FOREIGN})",
            "type": "market",
            "status": "filled",
            "quantity": "1",
            "filled": "1",
            "position intent": "none",
            "replaces another order": "yes",
            "replaced by another order": "no",
            "legs": "0",
        },
    ]
    text = "".join(value for sell in sells for value in sell.values())
    assert "5b1f0a3e" not in text and OURS not in text and "0f6d2c1e" not in text
    (asked,) = trading.asked
    assert (asked.status.value, asked.symbols, asked.after, asked.nested) == (
        "all",
        [seam.SYMBOL],
        SINCE,
        True,
    )


def test_the_read_after_the_node_waits_for_a_fill_and_returns_what_it_last_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpaca's listing lags the fill: the read is repeated until a sell shows filled or the wait is
    over, and a read that cannot be made is recorded, never raised."""
    monkeypatch.setattr(election.time, "sleep", lambda _seconds: None)
    answers: list[Any] = [
        OSError("read timed out"),
        [alpaca_order(status=SimpleNamespace(value="new"), filled_qty="0")],
        [alpaca_order()],
    ]

    class Lagging:
        def get_orders(self, filter: Any = None) -> list[SimpleNamespace]:
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

    sells = election.settled_sells(Lagging(), SINCE, OURS)
    assert [sell["status"] for sell in sells] == ["filled"]

    answers[:] = [OSError("read timed out")]
    assert election.settled_sells(Lagging(), SINCE, OURS, seconds=0.0) == [{"failed": "OSError"}]
    answers[:] = [[alpaca_order(status=SimpleNamespace(value="new"), filled_qty="0")]]
    (last,) = election.settled_sells(Lagging(), SINCE, OURS, seconds=0.0)
    assert last["status"] == "new"


def filled_walk() -> Walk:
    """The walk after an election the node saw: its order FILLED, one `OrderFilled`."""
    walk = working()
    end(walk, "FILLED", 1)
    assert walk.step() == "done"
    walk.events = [(name, OURS, 0) for name in ("OrderSubmitted", "OrderAccepted", "OrderFilled")]
    return walk


def unfilled_walk() -> Walk:
    """The walk after no election the node saw: canceled after the wait."""
    walk = working()
    walk.times["stop working"] -= int((election.ELECT_SECS + 1) * 1e9)
    walk.step()
    end(walk, "CANCELED")
    assert walk.step() == "done"
    walk.events = [(name, OURS, 0) for name in ("OrderSubmitted", "OrderAccepted", "OrderCanceled")]
    return walk


SOLD = {"which": "the stop's own client order ID", "type": "trailing_stop", "status": "filled"}
BECAME = {"which": "another client order ID (foreign)", "type": "market", "status": "filled"}
CANCELED = {
    "which": "the stop's own client order ID",
    "type": "trailing_stop",
    "status": "canceled",
}
CONDITION_B = "Alpaca filled the sale and the node's order did not get it: ADR 0129, condition (b)"


@pytest.mark.parametrize("sells", [[SOLD], [SOLD, BECAME], [BECAME]], ids=["own", "both", "other"])
def test_an_election_the_node_saw_passes_however_alpaca_lists_the_sale(sells: list) -> None:
    """How Alpaca books the sale is recorded, not required: one filled row or two, under the stop's
    own order or another."""
    assert election.verdict(filled_walk(), sells, Decimal(0), {"orders": 0}) == []


def test_a_fill_the_nodes_order_did_not_get_is_named_as_condition_b_first() -> None:
    """Alpaca shows a filled sale and the node's order ended some other way, or never ended: that is
    the condition, whatever else the walk says about itself."""
    assert election.verdict(unfilled_walk(), [CANCELED, BECAME], Decimal(0), {"orders": 0})[0] == (
        CONDITION_B
    )
    timed_out = working()
    timed_out.limit_ns = 0
    timed_out.step()
    assert election.verdict(timed_out, [BECAME], Decimal(0), {"orders": 1})[0] == CONDITION_B


def test_no_election_in_the_time_is_said_as_nothing_shown_and_is_not_condition_b() -> None:
    problems = election.verdict(unfilled_walk(), [CANCELED], Decimal(1), {"orders": 0})
    assert problems == ["no election in 120 s: nothing is shown"]


def test_a_stop_alpaca_refused_is_named_by_how_it_ended() -> None:
    walk = working()
    end(walk, "REJECTED")
    walk.step()
    problems = election.verdict(walk, [], Decimal(1), {"orders": 0})
    assert problems == ["the walk ended: the trailing stop ended REJECTED with no election"]


@pytest.mark.parametrize(
    ("sells", "held", "cleaned", "events", "problem"),
    [
        ([{"failed": "OSError"}], 0, 0, 1, "how Alpaca booked the sale could not be read: OSError"),
        ([], 0, 0, 1, "Alpaca lists no filled sell for a stop the node saw filled"),
        ([SOLD], 1, 0, 1, "1 share(s) still held after the node's order filled"),
        ([SOLD], 0, 2, 1, "the cleaning found 2 open order(s)"),
        ([SOLD], 0, 0, 2, "2 OrderFilled events for the node's order, not one"),
    ],
    ids=["read-failed", "not-listed", "still-held", "order-left", "two-fills"],
)
def test_what_else_can_be_wrong_after_an_election_is_named(
    sells: list, held: int, cleaned: int, events: int, problem: str
) -> None:
    walk = filled_walk()
    walk.events = [("OrderFilled", OURS, 0)] * events
    assert election.verdict(walk, sells, Decimal(held), {"orders": cleaned}) == [problem]


def test_the_record_printed_carries_no_id_no_price_and_no_time() -> None:
    walk = filled_walk()
    assert walk.seen["protective sell"] == OURS and "average" in walk.seen  # what the seam keeps
    walk.refusals = [(OURS, "403 40310000 insufficient qty")]
    lines = election.record("regular", walk, [SOLD, BECAME], Decimal(0))
    text = "\n".join(lines)
    assert OURS not in text and "100.0" not in text and "average" not in text
    assert lines[0] == "[regular] a tight trailing stop left to trigger: outcome 'done'"
    for wanted in (
        "the node's order ended: FILLED",
        "filled at the node: 1",
        "elected: yes",
        "sell 1 at Alpaca: " + str(SOLD),
        "sell 2 at Alpaca: " + str(BECAME),
        "held after: 0",
        "events: ['OrderSubmitted', 'OrderAccepted', 'OrderFilled']",
        "refused: 403 40310000 insufficient qty",
    ):
        assert any(wanted in line for line in lines), wanted


def test_the_strategy_is_built_like_the_proofs_and_the_proofs_node_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node is built as the paper run builds it and disposed without being run, with placeholders
    where the paper task puts the account's pair."""
    monkeypatch.setenv(ENV_API_KEY, "offline-placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "offline-placeholder")
    strategy = election.elected(90)
    assert isinstance(strategy, election.Elected) and isinstance(strategy, paper.Trailer)
    assert strategy.limit_ns == int(90e9)
    assert strategy.phase == "waiting for the long"
    node = paper.node_for(strategy)
    try:
        assert type(node).__name__ == "LiveNode"
    finally:
        node.dispose()
