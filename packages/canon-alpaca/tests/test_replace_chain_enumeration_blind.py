"""Blind enumeration: a replace chain that contains the node's own order must never leave it out of the
node's open-order picture, whichever link cannot be held and whichever order the adapter sees first.

Contract: NautilusTrader d53849b216e2's Python adapter report and stream hooks, and alpaca-py
c42add7c0a70's ``Order.replaces`` / ``Order.replaced_by`` (the chain links). The harness is the existing
offline ``LiveNode`` one, driven the way the neighbouring tests drive order status reports and the
trade-update stream.

The property, stated once: for every replace chain that contains the node's own order, whichever link
cannot be held, and whichever order is traced first, on the stream's path and on the running check's
path alike, the node's order is never left out of the open-order check.

Each parameter set is one shape, named by chain length, the node's position and the unholdable link's
position (oldest link is 0), the link the adapter sees or traces first, and the path:

* ``running-check``: the order status report asked for the first-seen link, then the open-order check.
* ``stream``: the trade update the first-seen link would send, then the open-order check.

The node's order is a real limit order the node submitted, so its venue order ID is a link of the chain
and its client order ID is the one the node holds. A link "cannot be held" when its client order ID is
the empty string, which NautilusTrader's ``ClientOrderId`` refuses. Only fake order UUIDs are used; no
network, no credential, no skip and no xfail.
"""

import asyncio
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.model import VenueOrderId

from . import test_execution_client as execution_tests
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - pytest fixture
from .test_execution_client import placed_order
from .test_execution_client import run_with_client
from .test_execution_client import trader
from .test_execution_client import until_idle
from .test_execution_client import venue as venue  # noqa: PLC0414 - pytest fixture

BASE_TIME = datetime(2026, 11, 14, 14, 0, tzinfo=UTC)
"""The chain's submission times: oldest first, one minute apart, so Alpaca's newest-first order reads are
stable. A fixed instant, not ``now``, so a shape is the same case every run."""

PATHS = ("running-check", "stream")


def chain_uuid(length: int, node_pos: int, cut_pos: int | None, index: int) -> str:
    """A fake Alpaca order UUID for one synthetic link: deterministic, so ids name the same shape."""
    seed = (
        length * 10_000 + node_pos * 1_000 + (cut_pos if cut_pos is not None else 9) * 100 + index
    )
    return str(uuid.UUID(int=0xB1000000000000000000000000000000 + seed))


def chain_of(
    alpaca: SimpleNamespace,
    placed: dict,
    length: int,
    node_pos: int,
    cut_pos: int | None,
) -> list[dict]:
    """The chain's Alpaca orders, oldest first: the node's submitted order at ``node_pos`` (its real
    venue order ID and client order ID), an empty client order ID at ``cut_pos``, and ``replaced`` /
    ``replaced_by`` links between neighbours. The newest link is open; the others were replaced."""
    ids = [
        placed["id"] if index == node_pos else chain_uuid(length, node_pos, cut_pos, index)
        for index in range(length)
    ]
    chain: list[dict] = []
    for index in range(length):
        base = placed if index == node_pos else alpaca.alpaca_order()
        client_order_id = (
            placed["client_order_id"]
            if index == node_pos
            else ("" if index == cut_pos else f"outside-{index}")
        )
        stamp = (BASE_TIME + timedelta(minutes=index)).isoformat()
        chain.append(
            {
                **base,
                "id": ids[index],
                "client_order_id": client_order_id,
                "replaces": ids[index - 1] if index else None,
                "replaced_by": ids[index + 1] if index < length - 1 else None,
                "status": "new" if index == length - 1 else "replaced",
                "submitted_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
                "filled_qty": "0",
                "filled_avg_price": None,
                "filled_at": None,
            }
        )
    return chain


def lines_of(reports: list) -> list[tuple[str, str]]:
    """Each report's venue order ID and client order ID, for an exact open-order comparison."""
    return [
        (
            report.venue_order_id.value,
            report.client_order_id.value if report.client_order_id is not None else "",
        )
        for report in reports
    ]


async def assert_open_check(client: AlpacaExecutionClient, live_id: str, node_cid: str) -> None:
    """The property's running-check side: exactly the working link, under the node's client order ID."""
    command = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)
    try:
        reports = await client._generate_order_status_reports(command)  # ty: ignore[invalid-argument-type]
    except Exception as error:  # noqa: BLE001 - the check failing is the property failing
        pytest.fail(f"the open-order check raised {error!r}; the node's order was left out")
    assert lines_of(reports) == [(live_id, node_cid)], (
        f"the open-order check reported {lines_of(reports)}; the node's own order {node_cid} "
        f"working at {live_id} must not be left out"
    )


async def run_running_check(
    client: AlpacaExecutionClient, chain: list[dict], first_pos: int, live_id: str, node_cid: str
) -> None:
    """The reconciliation request names the first-seen link; the open-order check follows it."""
    command = SimpleNamespace(
        venue_order_id=VenueOrderId(chain[first_pos]["id"]), client_order_id=None
    )
    try:
        report = await client._generate_order_status_report(command)  # ty: ignore[invalid-argument-type]
    except Exception as error:  # noqa: BLE001 - the report failing is the property failing
        pytest.fail(
            f"the order status report for link {first_pos} raised {error!r}; "
            "the node's order was left out"
        )
    assert report is not None, (
        f"the order status report for link {first_pos} was empty; the node's own order "
        f"{node_cid} working at {live_id} was left out"
    )
    assert (report.venue_order_id.value, report.client_order_id.value) == (live_id, node_cid), (  # ty: ignore[missing-union-attribute]
        f"the order status report for link {first_pos} named "
        f"{(report.venue_order_id.value, report.client_order_id.value)}, not the node's own order "  # ty: ignore[missing-union-attribute]
        f"{node_cid} working at {live_id}"
    )
    await assert_open_check(client, live_id, node_cid)


async def run_stream(
    client: AlpacaExecutionClient, venue: SimpleNamespace, chain: list[dict], first_pos: int
) -> None:
    """Deliver the trade update the first-seen link would send."""
    link = chain[first_pos]
    event = "new" if first_pos == len(chain) - 1 else "replaced"
    venue.stream.send({"event": event, "order": dict(link), "timestamp": link["updated_at"]})
    await until_idle(client, venue)


def shapes() -> list[Any]:
    """One pytest case per enumerated shape: length, node position, unholdable position, first-seen
    link and path. Positions are chain indices, oldest first; ``cut=None`` is a chain with no link that
    cannot be held."""
    cases = []
    for length in (1, 2, 3, 4):
        cuts: tuple[int | None, ...] = (None,) if length == 1 else (None, *range(length))
        for node_pos in range(length):
            for cut_pos in cuts:
                if cut_pos is not None and cut_pos == node_pos:
                    continue  # the node's own order is holdable by definition
                for first_pos in range(length):
                    for path in PATHS:
                        cut = "none" if cut_pos is None else str(cut_pos)
                        cases.append(
                            pytest.param(
                                length,
                                node_pos,
                                cut_pos,
                                first_pos,
                                path,
                                id=(f"len{length}-node{node_pos}-cut{cut}-first{first_pos}-{path}"),
                            )
                        )
    return cases


SHAPES = shapes()


def test_the_enumeration_covers_every_shape() -> None:
    """A guard on the enumeration itself: the count and the id vocabulary the report relies on."""
    assert len(SHAPES) == 200, f"the enumeration changed: {len(SHAPES)} shapes"
    ids = {case.id for case in SHAPES}
    assert len(ids) == len(SHAPES)
    assert {"len1-node0-cutnone-first0-running-check", "len1-node0-cutnone-first0-stream"} <= ids
    assert "len4-node0-cut3-first0-stream" in ids
    assert "len2-node1-cut0-first1-running-check" in ids


@pytest.mark.parametrize(("length", "node_pos", "cut_pos", "first_pos", "path"), SHAPES)
def test_a_replace_chain_that_contains_the_nodes_order_never_hides_it(
    venue: SimpleNamespace,
    alpaca: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    length: int,
    node_pos: int,
    cut_pos: int | None,
    first_pos: int,
    path: str,
) -> None:
    if path == "stream":
        # The stream path is the one that reports an order the node does not hold (after a restart):
        # its status report creates the node's own order under its client order ID.
        request.getfixturevalue("external_reports")
    venue.trading.script = "accept"  # one `new` update; the limit order stays open
    strategy = trader(order_type="limit", price="150.00")

    async def body(client: AlpacaExecutionClient) -> None:
        async with asyncio.timeout(10):
            while "OrderAccepted" not in strategy.names():
                await asyncio.sleep(0.005)
        await until_idle(client, venue)
        assert strategy.client_order_id is not None
        node_cid = strategy.client_order_id
        placed = dict(placed_order(venue))
        assert placed["client_order_id"] == node_cid.value
        assert placed["status"] == "new"
        venue_id = placed["id"]
        node_order = client.cache.order(node_cid)
        assert node_order is not None and node_order.venue_order_id is not None
        assert node_order.venue_order_id.value == venue_id

        chain = chain_of(alpaca, placed, length, node_pos, cut_pos)
        live = chain[-1]
        assert [link["id"] for link in chain].count(venue_id) == 1
        assert chain[node_pos]["client_order_id"] == node_cid.value
        if cut_pos is not None:
            assert chain[cut_pos]["client_order_id"] == ""
        # Nothing of the chain is left in the venue's own placed table, so the order reads return the
        # chain exactly once and the node's order is found through its cache, not through the venue.
        venue.trading._placed.clear()
        venue.trading.orders = chain

        captured: list = []
        if path == "stream":
            original = client._handle_report

            def record(report: object, fills: object = None) -> None:
                captured.append(report)
                original(report, fills)  # ty: ignore[invalid-argument-type]

            monkeypatch.setattr(client, "_handle_report", record)
            await run_stream(client, venue, chain, first_pos)
            reported = lines_of(captured)
            assert all(line == (live["id"], node_cid.value) for line in reported), (
                f"the stream reported {reported} for a chain whose working order belongs to "
                f"{node_cid}; the node's order was not named"
            )
            await assert_open_check(client, live["id"], node_cid.value)
        else:
            await run_running_check(client, chain, first_pos, live["id"], node_cid.value)

    run_with_client(body, strategy)


@pytest.fixture
def external_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on the one config field the stream path needs: report an order the node does not hold."""

    def config() -> AlpacaExecutionClientConfig:
        return AlpacaExecutionClientConfig(
            instrument_provider=execution_tests.provider_config(),
            report_external_orders=True,
        )

    monkeypatch.setattr(execution_tests, "exec_config", config)
