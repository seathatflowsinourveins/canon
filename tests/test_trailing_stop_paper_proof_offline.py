"""Offline checks of the trailing stop's paper proof (`tests/test_paper_venue_trailing_stop.py`), so that
its first paper run is not the first run of its own parts: the reader of what Alpaca holds, each step of
its strategy on a stand-in, and the node it builds. No request reaches Alpaca and no credential is read;
the node is built with placeholders and never run. The file's name keeps it out of the paper task's
`test_paper_*` selection.

What these cannot show is what the paper run is for: that Alpaca accepts, replaces and cancels the order.
"""

from __future__ import annotations

import time
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

OURS = "O-20261112-143000-001-001-1"
"""A client order ID of the form our nodes write (`custody.NODE_CLIENT_ORDER_ID`)."""


def alpaca_order(**changes: Any) -> SimpleNamespace:
    """An open trailing stop as alpaca-py's `Order` shows one: enums with a `value`, numbers as text."""
    fields: dict[str, Any] = {
        "type": SimpleNamespace(value="trailing_stop"),
        "side": SimpleNamespace(value="sell"),
        "qty": "2",
        "time_in_force": SimpleNamespace(value="day"),
        "trail_percent": "5.0",
        "trail_price": None,
        "hwm": "200.00",
        "stop_price": "190.00",
        "position_intent": SimpleNamespace(value="sell_to_close"),
        "client_order_id": OURS,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_what_alpaca_holds_is_read_with_no_venue_id_and_no_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seam, "open_orders", lambda trading: [alpaca_order()])
    read = paper.at_alpaca(object())
    assert read == {
        "type": "trailing_stop",
        "side": "sell",
        "quantity": "2",
        "time in force": "day",
        "trail percent": "5.0",
        "trail price": "none",
        "high-water mark": "set",
        "stop price follows the mark and the trail": "yes",
        "position intent": "sell_to_close",
        "lineage": custody.OURS,
    }
    assert "200" not in "".join(read.values()) and "190" not in "".join(read.values())


@pytest.mark.parametrize(
    ("changes", "follows", "lineage"),
    [
        ({"stop_price": "190.01"}, "yes", custody.OURS),  # within a cent of the mark less the trail
        ({"stop_price": "189.00"}, "no", custody.OURS),
        ({"hwm": None}, "unknown", custody.OURS),
        ({"stop_price": None}, "unknown", custody.OURS),
        # a replacement carries an ID Alpaca made: foreign by custody's rule
        ({"client_order_id": "0f6d2c1e-5d2b-4c0e-9d57-7f1f0a3e2b11"}, "yes", custody.FOREIGN),
    ],
)
def test_the_stop_price_is_checked_against_the_mark_and_the_trail(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], follows: str, lineage: str
) -> None:
    monkeypatch.setattr(seam, "open_orders", lambda trading: [alpaca_order(**changes)])
    read = paper.at_alpaca(object())
    assert read["stop price follows the mark and the trail"] == follows
    assert read["lineage"] == lineage


@pytest.mark.parametrize("count", [0, 2])
def test_anything_but_one_open_order_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setattr(seam, "open_orders", lambda trading: [alpaca_order()] * count)
    with pytest.raises(AssertionError, match=f"{count} open AAPL orders, not one"):
        paper.at_alpaca(object())


class Walk:
    """Stands in for the strategy, one step at a time: the cache's long, order and instrument, and what
    each step sent. Every method under test is the strategy's own."""

    _step = paper.Trailer._step
    _sell = paper.Trailer._sell
    _timed_out = paper.Trailer._timed_out
    on_order_event = paper.Trailer.on_order_event
    _mark = seam.Adopter._mark
    _finish = seam.Adopter._finish

    def __init__(self) -> None:
        self.sell_price = ""
        self.params = None
        self.foreign_exit = False
        self.limit_ns = int(60e9)
        self.phase = "waiting for the long"
        self.started_ns: int | None = time.time_ns()
        self.times: dict[str, int] = {}
        self.seen: dict[str, Any] = {}
        self.events: list = []
        self.refusals: list = []
        self.own: list = []
        self.foreign = None
        self.outcome = "not finished"
        self.read: dict[str, dict[str, str]] = {}
        self.modify_refusals: list[str] = []
        self.strategy_id = seam.ADOPTER
        self.long = Decimal(0)
        self.order: SimpleNamespace | None = None
        self.made: list[tuple] = []
        self.modified: list[tuple] = []
        self.canceled: list = []
        self.stopped: list[str] = []
        self.cache = SimpleNamespace(
            positions_open=self._positions,
            orders=lambda **_: [],
            order=lambda _id: self.order,
            instrument=lambda _id: SimpleNamespace(make_qty=lambda q: Quantity.from_str(str(q))),
        )
        self.order_factory = SimpleNamespace(trailing_stop_market=self._trailing)

    def _positions(self, **_: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(signed_qty=self.long, avg_px_open=100.0)] if self.long else []

    def _trailing(self, *positional: Any, **named: Any) -> SimpleNamespace:
        self.made.append((positional, named))
        return SimpleNamespace(
            client_order_id=SimpleNamespace(value=OURS),
            quantity=positional[2],
            is_open=False,
            is_closed=False,
            is_pending_update=False,
            status=SimpleNamespace(name="SUBMITTED"),
        )

    def submit_order(self, order: SimpleNamespace) -> None:
        self.order = order

    def modify_order(self, client_order_id: Any, **changes: Any) -> None:
        self.modified.append((client_order_id.value, changes))

    def cancel_order(self, client_order_id: Any) -> None:
        self.canceled.append(client_order_id.value)

    def shutdown_system(self, reason: str) -> None:
        self.stopped.append(reason)

    def step(self) -> str:
        self._step(None)
        return self.phase


def working() -> Walk:
    """The walk as far as the stop working at Alpaca: a long of two shares adopted, the stop placed."""
    walk = Walk()
    assert walk.step() == "waiting for the long"
    walk.long = Decimal(1)
    assert walk.step() == "waiting for the long"  # one share adopted: the stop waits for both
    assert walk.made == [] and walk.order is None
    walk.long = Decimal(paper.SHARES)
    assert walk.step() == "placing"
    assert walk.step() == "waiting for the sell to work"
    assert walk.order is not None
    assert walk.step() == "waiting for the sell to work"  # submitted, not yet acknowledged
    walk.order.is_open = True
    assert walk.step() == "waiting for the first read"
    return walk


def test_the_stop_is_a_day_trailing_sell_by_basis_points_for_the_whole_long() -> None:
    walk = working()
    ((positional, named),) = walk.made
    assert positional == (
        paper.AAPL,
        OrderSide.SELL,
        Quantity.from_int(paper.SHARES),
        paper.FIRST_BPS,
    )
    assert named == {
        "trailing_offset_type": TrailingOffsetType.BASIS_POINTS,
        "time_in_force": TimeInForce.DAY,
    }
    assert walk.order is not None
    assert walk.own == [walk.order.client_order_id] and walk.modified == [] and walk.canceled == []


def test_each_step_waits_for_the_read_before_it_and_the_walk_ends_done() -> None:
    walk = working()
    assert walk.step() == "waiting for the first read" and walk.modified == []
    walk.read["first"] = {}
    assert walk.step() == "waiting for the replace"
    assert walk.modified == [
        (OURS, {"quantity": Quantity.from_int(1), "params": {"trailing_offset": "400"}})
    ]
    assert walk.order is not None
    walk.order.is_pending_update = True
    walk.order.quantity = Quantity.from_int(1)
    assert walk.step() == "waiting for the replace"  # the update is not in yet
    walk.order.is_pending_update = False
    assert walk.step() == "waiting for the second read"
    assert walk.step() == "waiting for the second read" and walk.canceled == []
    walk.read["second"] = {}
    assert walk.step() == "waiting for the sell's cancel" and walk.canceled == [OURS]
    assert walk.step() == "waiting for the sell's cancel"
    walk.order.is_open, walk.order.is_closed = False, True
    assert walk.step() == "done"
    assert walk.outcome == "done" and walk.stopped == ["done"]
    assert len(walk.modified) == 1 and len(walk.canceled) == 1
    for name in ("adopted", "stop working", "replace sent", "replaced", "cancel sent"):
        assert name in walk.times, name


def test_a_first_read_that_failed_ends_the_stop_without_changing_it() -> None:
    walk = working()
    walk.read["first"] = {"failed": "AssertionError: 2 open AAPL orders, not one"}
    assert walk.step() == "waiting for the sell's cancel"
    assert walk.modified == [] and walk.canceled == [OURS]
    assert walk.order is not None
    walk.order.is_open, walk.order.is_closed = False, True
    assert walk.step() == "done" and walk.outcome == "done"


class OrderModifyRejected:
    """An event as a handler reads one: its type's name, the order it is for and the reason."""

    def __init__(self, reason: str) -> None:
        self.client_order_id = SimpleNamespace(value=OURS)
        self.reason = reason


class OrderAccepted:
    def __init__(self) -> None:
        self.client_order_id = SimpleNamespace(value=OURS)


def test_a_modify_rejection_is_kept_from_the_event_as_plain_text() -> None:
    walk = Walk()
    walk.on_order_event(OrderModifyRejected("422 no"))
    assert walk.modify_refusals == ["422 no"]
    assert [(name, order_id) for name, order_id, _ in walk.events] == [
        ("OrderModifyRejected", OURS)
    ]
    walk.on_order_event(OrderAccepted())
    assert walk.modify_refusals == ["422 no"] and len(walk.events) == 2


def test_only_part_of_the_long_past_the_limit_ends_the_walk_with_nothing_placed() -> None:
    walk = Walk()
    walk.long = Decimal(1)
    walk.started_ns = time.time_ns() - walk.limit_ns - 1
    assert walk.step() == "done"
    assert walk.outcome == "timed out while waiting for the long" and walk.made == []


def listing(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[int]:
    """Alpaca's open-order listing as a script, one answer a read and the last one from then on; an
    exception in the script is raised. The waits between reads are not slept."""
    reads: list[int] = []

    def open_orders(trading: Any) -> list[SimpleNamespace]:
        reads.append(1)
        answer = answers[min(len(reads), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(seam, "open_orders", open_orders)
    monkeypatch.setattr(paper.time, "sleep", lambda _seconds: None)
    return reads


def test_a_read_waits_for_the_listing_to_show_the_stop_as_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing lags: nothing yet, then the old stop beside its replacement, then the replacement
    with no high-water mark, then the stop as asked. One read sees it through."""
    reads = listing(
        monkeypatch,
        [
            [],
            [alpaca_order(), alpaca_order(qty="1", trail_percent="4.0")],
            [alpaca_order(qty="1", trail_percent="4.0", hwm=None)],
            [alpaca_order(qty="1", trail_percent="4.0", stop_price="192.00")],
        ],
    )
    read = paper.read_stop(object(), 1, Decimal(4))
    assert len(reads) == 4 and "failed" not in read
    assert (read["quantity"], read["trail percent"], read["high-water mark"]) == ("1", "4.0", "set")
    assert read["stop price follows the mark and the trail"] == "yes"


@pytest.mark.parametrize(
    ("answer", "kept"),
    [
        ([alpaca_order(hwm=None)], {"high-water mark": "none"}),
        ([], {"failed": "AssertionError: 0 open AAPL orders, not one"}),
        (OSError("read timed out"), {"failed": "OSError: read timed out"}),
    ],
    ids=["mark-never-set", "never-listed", "network"],
)
def test_a_read_that_never_settles_returns_what_it_last_saw_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch, answer: Any, kept: dict[str, str]
) -> None:
    listing(monkeypatch, [answer])
    read = paper.read_stop(object(), 2, Decimal(5), seconds=0.0)
    assert {name: read[name] for name in kept} == kept


def test_a_refused_replace_cancels_the_stop_and_names_the_reason() -> None:
    walk = working()
    walk.read["first"] = {}
    assert walk.step() == "waiting for the replace"
    walk.modify_refusals.append("the venue said no")
    assert walk.step() == "done"
    assert walk.canceled == [OURS]
    assert walk.outcome == "the replace was refused: the venue said no"


def test_a_stop_that_ends_before_its_cancel_ends_the_walk_by_its_status() -> None:
    walk = working()
    assert walk.order is not None
    walk.order.is_open, walk.order.is_closed = False, True
    walk.order.status = SimpleNamespace(name="FILLED")
    assert walk.step() == "done"
    assert walk.outcome == "the trailing stop ended FILLED while waiting for the first read"
    assert walk.modified == [] and walk.canceled == []


def test_a_wait_past_the_limit_ends_the_walk_by_the_step_it_was_in() -> None:
    walk = working()
    walk.started_ns = time.time_ns() - walk.limit_ns - 1
    assert walk.step() == "done"
    assert walk.outcome == "timed out while waiting for the first read"


class EndedNode:
    """A node whose run is over at once, as one that stopped itself at start-up is."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.disposed = False

    async def run_async(self) -> None:
        if self.error is not None:
            raise self.error

    def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize("error", [None, RuntimeError("the start-up reconciliation failed")])
def test_a_node_that_ended_by_itself_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None
) -> None:
    """The strategy never reaches its end, so the reads would go on to the run's time limit: they end
    with the node, at once, and what ended the node is what the run raises."""
    node = EndedNode(error)
    monkeypatch.setattr(paper, "node_for", lambda strategy: node)
    walk: Any = Walk()
    walk.phase = "waiting for the replace"
    began = time.monotonic()
    if error is None:
        paper.run(walk, 30, object())
    else:
        with pytest.raises(RuntimeError, match="the start-up reconciliation failed"):
            paper.run(walk, 30, object())
    assert time.monotonic() - began < 5 and node.disposed and walk.read == {}


def test_the_node_is_configured_with_the_risk_engine_bypassed_and_the_setting_on_and_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node is built as the paper run builds it and disposed without being run: its configuration
    is one upstream accepts, with placeholders where the paper task puts the account's pair."""
    monkeypatch.setenv(ENV_API_KEY, "offline-placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "offline-placeholder")
    config = paper.node_config()
    assert config.risk_engine.bypass is True
    assert config.exec_clients["ALPACA"].submit_trailing_stops is True
    assert config.data_clients == {}
    strategy = paper.trailer(120)
    assert strategy.limit_ns == int(120e9) and strategy.phase == "waiting for the long"
    node = paper.node_for(strategy)
    try:
        assert type(node).__name__ == "LiveNode"
    finally:
        node.dispose()
