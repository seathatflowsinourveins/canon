"""A whole-market instrument load at the queue's limit: what is kept, what upstream refuses, and how many.

Contract: NautilusTrader's Python adapter guide at the pinned commit `d53849b216e2`
(`docs/developer_guide/python_adapters.md`): "Commands and subscription changes share a FIFO queue with
capacity for 1,024 waiting operations per client. A full queue rejects further admission" (:168-170), and
"`_on_instrument` and `_register_external_order` are async notifications admitted to the command queue"
(:408). The capacity is `COMMAND_CAPACITY` (`crates/live/src/python/runtime.rs:39`), the refusal
`runtime.rs:187-192`, and each published instrument is admitted to the execution client's queue by
`crates/live/src/python/client/mod.rs:1960-1965`, from the engine's venue subscription
(`crates/execution/src/engine/mod.rs:288-303`).

The limit is upstream's and no pacing here can lift it: the node's start connects the data clients, then
puts every instrument they buffered into the cache in one synchronous loop, then connects the execution
clients (`crates/live/src/node/mod.rs:35-48`, `:484-493`; `flush_pending_data`,
`crates/live/src/runner.rs:375-411`). No Python runs inside that loop, so the queue cannot drain in it,
and the notification of each instrument past the first 1,024 is refused and logged as an error. These
tests hold what the trading path needs in spite of it, and count the refusals exactly, so that a change
upstream fails one of them by name (the package README, "Known limits of the execution slice").

A real `LiveNode` with the adapter's two clients on the stand-ins of `conftest.py`; no network.
"""

from __future__ import annotations

import asyncio

import pytest
from canon_alpaca.constants import VENUE
from canon_alpaca.execution import INSTRUMENT_REFUSAL
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import Equity

from .test_execution_client import BUILT_NODES
from .test_execution_client import TRADER
from .test_execution_client import Trader
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - pytest fixture
from .test_execution_client import logged
from .test_execution_client import trader
from .test_execution_client import venue as venue  # noqa: PLC0414 - pytest fixture

COMMAND_CAPACITY = 1024
"""`crates/live/src/python/runtime.rs:39` at the pinned commit; the guide's "1,024 waiting operations"."""


def whole_market(base: dict, count: int) -> list[dict]:
    """`count` assets like `base`, the docs' asset, each with its own ID and symbol."""
    return [
        {**base, "id": f"{i:08d}-0000-4000-8000-000000000000", "symbol": f"S{i:05d}"}
        for i in range(count)
    ]


def load_all_node(
    strategy: Trader,
    data: InstrumentProviderConfig | None = None,
    execution: InstrumentProviderConfig | None = None,
) -> LiveNode:
    """A node whose two clients each load every asset, unless `data` or `execution` says what that client
    loads. It is not `test_execution_client.build_node`: that one loads AAPL alone and gives a connect two
    seconds, and its other callers rely on both."""
    everything = InstrumentProviderConfig(load_all=True)
    node = LiveNode.build(
        "CANON-ALPACA-LOAD",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TRADER,
            timeout_connection_secs=20,
            timeout_reconciliation_secs=5,
            timeout_disconnection_secs=5,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": DataClientConfig(instrument_provider=data or everything)},
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(instrument_provider=execution or everything)
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
    node.add_strategy(strategy)
    BUILT_NODES.append(node)
    return node


NOTIFIED: list[str] = []
"""Each instrument the execution client was notified of (`_on_instrument`), in order, in the test running."""


class AtStart(Trader):
    """An idle strategy that notes, in `on_start`, how many notifications had reached the client by then
    and how many instruments the cache held."""

    def __init__(self) -> None:
        super().__init__()
        self.idle = True
        self.notified_at_start: int | None = None
        self.cached_at_start: int | None = None

    def on_start(self) -> None:
        self.notified_at_start = len(NOTIFIED)
        self.cached_at_start = len(self.cache.instrument_ids(VENUE))
        super().on_start()


@pytest.fixture
def notified(monkeypatch) -> list[str]:
    NOTIFIED.clear()

    async def record(self: AlpacaExecutionClient, instrument: Equity) -> None:
        NOTIFIED.append(str(instrument.id))

    monkeypatch.setattr(AlpacaExecutionClient, "_on_instrument", record)
    return NOTIFIED


def run_until_started(node: LiveNode, strategy: Trader) -> None:
    """Run the node until the strategy has started and the loop has had its turns, then stop it."""

    async def main() -> None:
        async with asyncio.timeout(60):
            task = asyncio.ensure_future(node.run_async())
            try:
                while not (task.done() or strategy.started):
                    await asyncio.sleep(0.005)
                if task.done():
                    task.result()
                for _ in range(20):
                    await asyncio.sleep(0.01)
            finally:
                if not task.done():
                    node.handle().stop()
                await task

    asyncio.run(main())


def error_lines(log: str) -> list[str]:
    return [line for line in log.splitlines() if "[ERROR]" in line]


def refusals(errors: list[str]) -> list[str]:
    """The error lines that are upstream's refusal of an instrument's notification, by the marks a
    window's record counts them by (`INSTRUMENT_REFUSAL`): read here off a real node's log."""
    return [line for line in errors if all(mark in line for mark in INSTRUMENT_REFUSAL)]


JUST_AAPL = InstrumentProviderConfig(load_ids=["AAPL.ALPACA"])


def test_a_paper_nodes_whole_market_start_refuses_the_instruments_less_the_capacity_and_trades(
    venue, alpaca, notified, capfd
) -> None:
    """The shape a paper node with a ranking runs (`canon_trading.paper_node`, `data_load_all`): the data
    client loads the whole market and the execution client the listed instruments. The flush after the
    data connect admits the first 1,024 notifications and refuses the rest, the execution client hands
    on nothing, since the cache holds its one instrument, and the order sent on the first quote after
    the start is admitted and fills."""
    count = 3 * COMMAND_CAPACITY + 7
    venue.trading.assets = [alpaca.ASSET_AAPL, *whole_market(alpaca.ASSET_AAPL, count)]
    strategy = trader()
    node = load_all_node(strategy, execution=JUST_AAPL)

    async def main() -> None:
        async with asyncio.timeout(60):
            await node.run_async()

    asyncio.run(main())

    errors = error_lines(logged(capfd))
    assert len(refusals(errors)) == count + 1 - COMMAND_CAPACITY
    assert errors == refusals(errors), [line for line in errors if line not in refusals(errors)][:3]
    assert len(notified) == len(set(notified)) == COMMAND_CAPACITY
    assert len(node.cache.instrument_ids(VENUE)) == count + 1
    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]


def test_upstream_refuses_one_notification_for_each_instrument_past_the_queues_capacity(
    venue, alpaca, notified, capfd
) -> None:
    """Three times the queue's capacity and a few more, loaded by both clients, the shape with the most
    to refuse. Upstream's flush after the data connect admits the first 1,024 notifications and refuses
    the rest: the refusals are the instruments less the capacity, each instrument once, since the
    execution client hands on nothing the cache already holds. They are the only error lines, and the
    queue is empty when the strategy starts: every notification admitted has been delivered by then,
    and none arrives later."""
    count = 3 * COMMAND_CAPACITY + 7
    venue.trading.assets = whole_market(alpaca.ASSET_AAPL, count)
    strategy = AtStart()
    node = load_all_node(strategy)

    run_until_started(node, strategy)

    errors = error_lines(logged(capfd))
    refused = refusals(errors)
    assert len(refused) == count - COMMAND_CAPACITY, len(refused)
    assert errors == refused, [line for line in errors if line not in refused][:3]
    assert notified == [f"S{i:05d}.ALPACA" for i in range(COMMAND_CAPACITY)]
    assert strategy.notified_at_start == COMMAND_CAPACITY
    assert strategy.cached_at_start == count


def test_a_load_within_the_queues_capacity_is_notified_in_full_with_no_refusal(
    venue, alpaca, notified, capfd
) -> None:
    count = COMMAND_CAPACITY
    venue.trading.assets = whole_market(alpaca.ASSET_AAPL, count)
    strategy = AtStart()
    node = load_all_node(strategy)

    run_until_started(node, strategy)

    assert error_lines(logged(capfd)) == []
    assert notified == [f"S{i:05d}.ALPACA" for i in range(count)]


def test_the_execution_client_hands_on_what_the_data_client_did_not_load(
    venue, alpaca, notified, capfd
) -> None:
    """The data client loads one instrument and the execution client every one: the cache ends with all
    of them, the one the cache held is not handed on again, and each is notified once."""
    count = 50
    venue.trading.assets = [alpaca.ASSET_AAPL, *whole_market(alpaca.ASSET_AAPL, count)]
    strategy = AtStart()
    node = load_all_node(strategy, data=JUST_AAPL)

    run_until_started(node, strategy)

    assert error_lines(logged(capfd)) == []
    assert len(node.cache.instrument_ids(VENUE)) == count + 1
    assert sorted(notified) == sorted(["AAPL.ALPACA", *(f"S{i:05d}.ALPACA" for i in range(count))])


def test_after_a_whole_market_load_every_instrument_is_cached_and_an_order_at_start_fills(
    venue, alpaca
) -> None:
    """What the trading path needs of the load, whatever the queue did with the notifications: the
    node's cache holds every instrument both clients loaded, and the first order command, sent on the
    strategy's first quote after start, is admitted, accepted and filled."""
    count = 3 * COMMAND_CAPACITY + 7
    venue.trading.assets = [alpaca.ASSET_AAPL, *whole_market(alpaca.ASSET_AAPL, count)]
    strategy = trader()
    node = load_all_node(strategy)

    async def main() -> None:
        async with asyncio.timeout(60):
            await node.run_async()

    asyncio.run(main())

    assert len(node.cache.instrument_ids(VENUE)) == count + 1
    assert strategy.names() == ["OrderSubmitted", "OrderAccepted", "OrderFilled"]


def test_each_burst_past_the_capacity_is_refused_when_the_execution_client_loads_more(
    venue, alpaca, notified, capfd
) -> None:
    """The data client loads one instrument and the execution client more than the queue holds: what it
    hands on at its own connect is a second burst, drained in one go like the first, and again each
    notification past 1,024 is refused. The cache still ends with every instrument."""
    count = 2 * COMMAND_CAPACITY
    venue.trading.assets = [alpaca.ASSET_AAPL, *whole_market(alpaca.ASSET_AAPL, count)]
    strategy = AtStart()
    node = load_all_node(strategy, data=JUST_AAPL)

    run_until_started(node, strategy)

    errors = error_lines(logged(capfd))
    assert len(refusals(errors)) == count - COMMAND_CAPACITY
    assert errors == refusals(errors)
    assert strategy.cached_at_start == count + 1
