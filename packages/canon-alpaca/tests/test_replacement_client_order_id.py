"""The client order ID the client gives a replacement it sends.

Contract: Alpaca's "Replace Order by ID" takes `client_order_id`, "A unique identifier for the new order.
Automatically generated if not sent. (<= 128 characters)"
(https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md, read 2026-11-12), and alpaca-py's
`ReplaceOrderRequest` carries it (alpaca-py c42add7c0a70 `alpaca/trading/requests.py:307-335`). A replace
that keeps the order's own ID is refused, 422 "client_order_id must be unique" (paper, 2026-11-10, ADR
0042), so the ID sent is a new one: the node's own with a clock stamp after its last field. Whoever reads
an account by client order ID then reads our replacement as it reads the order it replaced, and an order
replaced by hand, which carries an ID Alpaca made, as not ours. That Alpaca's paper endpoint keeps the ID
sent is not shown by these tests.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.requests import ReplaceOrderRequest
from canon_alpaca import execution
from canon_alpaca.execution import replacement_client_order_id
from nautilus_trader.model import VenueOrderId

from .test_execution_client import alpaca_order_model
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - fixture
from .test_execution_client import forget
from .test_execution_client import logged
from .test_execution_client import modifier
from .test_execution_client import placed_order
from .test_execution_client import run_modify
from .test_execution_client import venue as venue  # noqa: PLC0414 - fixture

STAMP = 1_762_999_999_123_456_789
AHEAD = 9_000_000_000_000_000_000
"""A stamp far ahead of any clock reading a test takes, nineteen digits still."""
NODE_ID = "O-20261112-143000-001-001-7"


def own_id(strategy) -> str:
    assert strategy.client_order_id is not None
    return strategy.client_order_id.value


@pytest.mark.parametrize(
    ("own", "sent"),
    [
        (NODE_ID, "O-20261112-143000-001-001-71762999999123456789"),
        ("O-20261112-143000-001-001-1234", "O-20261112-143000-001-001-12341762999999123456789"),
        ("hand-7", "hand-71762999999123456789"),  # a foreign form stays a foreign form
        ("placed-elsewhere", None),  # no numeric last field: Alpaca makes the ID, as before
        ("7", None),  # nothing before the count to keep
        ("", None),
        ("O-20261112-143000-001-001-", None),
        ("O-20261112-143000-001-001-٧", None),  # a digit, but not ASCII
        ("x" * 120 + "-7", None),  # over Alpaca's 128 characters with the stamp
    ],
)
def test_the_id_is_the_orders_own_with_a_clock_stamp_after_its_last_field(own, sent) -> None:
    assert replacement_client_order_id(own, STAMP) == sent
    assert sent is None or len(sent) <= execution.CLIENT_ORDER_ID_MAX == 128


def test_the_stamp_is_nineteen_digits_whatever_the_clock_reads() -> None:
    """A fixed width keeps one order's replacements apart from another's: the count is what is left
    when the last nineteen digits are taken off."""
    early = replacement_client_order_id(NODE_ID, 5)
    assert early == "O-20261112-143000-001-001-70000000000000000005"
    assert replacement_client_order_id("O-20261112-143000-001-001-70", STAMP) != (
        replacement_client_order_id(NODE_ID, STAMP)
    )
    # a clock reading that is no nineteen-digit count names nothing, like an ID with no count
    assert replacement_client_order_id(NODE_ID, 10**19) is None
    assert replacement_client_order_id(NODE_ID, -1) is None


@pytest.mark.parametrize(
    ("carried", "named"),
    [
        (f"{NODE_ID}{STAMP}", True),
        (f"{NODE_ID}0000000000000000005", True),
        (NODE_ID, False),  # the order's own: no replacement carries it
        (f"{NODE_ID}123", False),  # another order of the node's, not a stamp's nineteen digits
        (f"{NODE_ID}{STAMP}0", False),
        ("0f6d2c1e-5d2b-4c0e-9d57-7f1f0a3e2b11", False),  # one Alpaca made
        ("", False),
        (None, False),
    ],
)
def test_an_id_this_client_names_for_a_replacement_is_told_from_any_other(carried, named) -> None:
    assert execution.names_replacement(NODE_ID, carried) is named


def test_a_replace_names_a_new_id_of_the_nodes_form_and_the_nodes_own_id_does_not_change(
    venue,
) -> None:
    venue.trading.script = "accept"
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    own = own_id(strategy)
    ((_, request),) = venue.trading.replace_requests
    assert isinstance(request, ReplaceOrderRequest)
    assert re.fullmatch(re.escape(own) + "[0-9]{19}", request.client_order_id)
    old, new = placed_order(venue, 0), placed_order(venue, 1)
    assert (old["client_order_id"], new["client_order_id"]) == (own, request.client_order_id)
    updated = strategy.last("OrderUpdated")
    assert updated.client_order_id == strategy.client_order_id
    assert updated.venue_order_id.value == new["id"]


def test_two_replaces_of_one_order_get_two_ids_in_the_order_they_were_sent(venue) -> None:
    venue.trading.script = "accept"
    strategy = modifier(prices=["150.50", "151.00"])

    run_modify(venue, strategy, lambda: strategy.names().count("OrderUpdated") == 2)

    own = own_id(strategy)
    first, second = (request.client_order_id for _, request in venue.trading.replace_requests)
    assert all(re.fullmatch(re.escape(own) + "[0-9]{19}", sent) for sent in (first, second))
    assert first < second


def test_the_stamp_never_repeats_in_a_run_and_follows_the_nodes_clock(venue) -> None:
    """Two replaces in one reading of the clock, or after the clock stepped back, still get two IDs."""
    venue.trading.script = "accept"
    strategy = modifier(prices=[])
    stamps: list[int] = []

    async def read(client) -> None:
        before = client.clock.timestamp_ns()
        stamps.extend((before, client._replacement_stamp()))
        client._last_replacement_stamp = AHEAD  # a clock that has since stepped back
        stamps.extend((client._replacement_stamp(), client._replacement_stamp()))

    run_modify(venue, strategy, lambda: "OrderAccepted" in strategy.names(), read)

    before, from_the_clock, third, fourth = stamps
    assert before <= from_the_clock < before + 10**10
    assert (third, fourth) == (AHEAD + 1, AHEAD + 2)


def test_a_replacement_the_node_never_saw_confirmed_is_traced_through_its_replaces_link(
    venue,
) -> None:
    """A run that ended between a replace's answer and its `replaced`: the node's cache holds the
    order under the Alpaca order it was on before, and a new client knows nothing. The replacement's
    own client order ID is of the node's form and is no order the node holds, so it is found as it
    was when Alpaca made the ID: by reading the order it `replaces`."""
    venue.trading.script = "accept"
    venue.trading.replace_script = "silent"
    strategy = modifier()
    reports: list = []
    mapped: list = []

    async def report(client) -> None:
        old, new = placed_order(venue, 0), placed_order(venue, 1)
        assert client.cache.order(strategy.client_order_id).venue_order_id.value == old["id"]
        assert client.cache.client_order_id(VenueOrderId(new["id"])) is None
        forget(client)
        venue.trading.orders = list(venue.trading._placed.values())
        mapped.append(await client._node_order_id(alpaca_order_model(new)))
        window = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)
        reports.extend(await client._generate_order_status_reports(window))

    run_modify(venue, strategy, lambda: len(venue.trading.replace_requests) == 1, report)

    new = placed_order(venue, 1)
    assert re.fullmatch(re.escape(own_id(strategy)) + "[0-9]{19}", new["client_order_id"])
    assert mapped == [strategy.client_order_id]
    assert [(r.venue_order_id.value, r.client_order_id) for r in reports] == [
        (new["id"], strategy.client_order_id)
    ]


OTHER_ID = "carries a client order ID this client did not name"


@pytest.mark.parametrize("answer", ["read", "lost"])
def test_a_replacement_under_another_id_is_named_once_however_it_is_adopted(
    venue, capfd, answer
) -> None:
    """If Alpaca takes the replace and makes its own client order ID after all, the modify stands and
    the node's order follows it through `replaces`, but an account read by client order ID no longer
    shows the replacement as the node's. It is said once for the replacement, where it is adopted:
    with the answer read, and with the answer lost and the stream's `replaced` alone."""
    venue.trading.script = "accept"
    venue.trading.keeps_named_id = False
    if answer == "lost":
        venue.trading.replace_error = requests.ReadTimeout("read timed out")
        venue.trading.took_before_error = True
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    ((_, request),) = venue.trading.replace_requests
    new = placed_order(venue, 1)
    assert request.client_order_id is not None and new["client_order_id"] != request.client_order_id
    assert strategy.last("OrderUpdated").venue_order_id.value == new["id"]
    assert logged(capfd).count(OTHER_ID) == 1


@pytest.mark.parametrize("answer", ["read", "lost"])
def test_a_replacement_under_the_id_named_says_nothing(venue, capfd, answer) -> None:
    venue.trading.script = "accept"
    if answer == "lost":
        venue.trading.replace_error = requests.ReadTimeout("read timed out")
        venue.trading.took_before_error = True
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    assert OTHER_ID not in logged(capfd)


def test_a_replace_that_can_name_no_id_goes_out_without_one_and_its_replacement_is_named(
    venue, capfd, monkeypatch
) -> None:
    """An order whose own ID gives nothing to build on is replaced as before, Alpaca making the ID.
    Nothing is said before a replacement exists: a refused replace leaves none to name."""
    venue.trading.script = "accept"
    monkeypatch.setattr(execution, "replacement_client_order_id", lambda *_: None)
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderUpdated" in strategy.names())

    ((_, request),) = venue.trading.replace_requests
    assert request.client_order_id is None
    assert logged(capfd).count(OTHER_ID) == 1


def test_a_refused_replace_names_no_replacement(venue, alpaca, capfd, monkeypatch) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = alpaca.api_error(422, 42210000, "order is not replaceable")
    monkeypatch.setattr(execution, "replacement_client_order_id", lambda *_: None)
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert OTHER_ID not in logged(capfd)


def test_the_stand_in_refuses_only_an_id_an_order_already_carries(venue, alpaca) -> None:
    """What paper showed (ADR 0042): a replace that keeps the order's own client order ID is refused.
    A new one is what Alpaca documents, and the stand-in takes it and gives it to the replacement."""
    venue.trading.script = "accept"
    strategy = modifier(prices=[])

    async def replace(client) -> None:
        old = placed_order(venue, 0)
        with pytest.raises(APIError, match="client_order_id must be unique"):
            venue.trading.replace_order_by_id(
                old["id"], ReplaceOrderRequest(qty=1, client_order_id=old["client_order_id"])
            )
        made = venue.trading.replace_order_by_id(
            old["id"], ReplaceOrderRequest(qty=1, client_order_id="a-new-one")
        )
        assert made.client_order_id == "a-new-one" and str(made.replaces) == old["id"]

    run_modify(venue, strategy, lambda: "OrderAccepted" in strategy.names(), replace)
