"""Blind H1-H6 tests at 556a70f3, using the existing offline LiveNode harness.

Contract: NautilusTrader d53849b216e2, live/clients.py's stream/query/report hooks and
queued generate_order_filled/generate_order_canceled outputs; execution/manager.rs's
targeted reconciliation treats a successful empty answer as missing at the venue.
Alpaca payloads use alpaca-py c42add7c0a70 Order, TradeUpdate and fill-activity fields.
Exact source lines and the base outcomes are recorded in the untracked REPORT.md.

Event delivery is delayed at the documented generate_* boundary, never by changing
the adapter's finished set or the native cache. All scheduling waits have conditions
and deadlines; no fixed sleeps, network, credentials, skip or xfail marks are used.
"""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from itertools import permutations
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from alpaca.trading.models import TradeUpdate
from canon_alpaca import execution
from canon_alpaca.execution import AlpacaExecutionClient
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderStatusReport
from nautilus_trader.model import VenueOrderId

from .test_execution_client import Trader
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - pytest fixture
from .test_execution_client import placed_order
from .test_execution_client import run_with_client
from .test_execution_client import trade_ids
from .test_execution_client import trader
from .test_execution_client import venue as venue  # noqa: PLC0414 - pytest fixture

WAIT_BOUND_SECS = 3.0


async def turn() -> None:
    """Yield to already ready callbacks, without a wall-clock delay."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.call_soon(wake)
    await ready


async def until(
    condition: Callable[[], bool], reason: str, bound_secs: float = WAIT_BOUND_SECS
) -> None:
    try:
        async with asyncio.timeout(bound_secs):
            while not condition():
                await turn()
    except TimeoutError:
        raise AssertionError(reason) from None


def cached(client: AlpacaExecutionClient, strategy: Trader) -> Any:
    assert strategy.client_order_id is not None
    order = client.cache.order(strategy.client_order_id)
    assert order is not None
    return order


class EventGate:
    """Queue the adapter's real output calls, preserving their order on release."""

    def __init__(self, client: AlpacaExecutionClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self.open = False
        self.calls: list[str] = []
        self.pending: list[Callable[[], Any]] = []
        for name in ("generate_order_filled", "generate_order_canceled"):
            original = getattr(client, name)

            def capture(*args: Any, _name=name, _original=original, **kwargs: Any) -> None:
                self.calls.append(_name)
                if self.open:
                    _original(*args, **kwargs)
                else:
                    self.pending.append(lambda: _original(*args, **kwargs))

            monkeypatch.setattr(client, name, capture)

    def release(self) -> None:
        self.open = True
        pending, self.pending = self.pending, []
        for deliver in pending:
            deliver()


class StreamProbe:
    """Observe consumption of actual FakeTradeStream messages, including blocked ones."""

    def __init__(self, client: AlpacaExecutionClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waiters: dict[tuple[str, str, str], tuple[asyncio.Event, asyncio.Event]] = {}
        original = client._apply_trade_update

        async def observed(update: TradeUpdate) -> None:
            pair = self.waiters.get(self.key(update))
            if pair is not None:
                pair[0].set()
            try:
                await original(update)
            finally:
                if pair is not None:
                    pair[1].set()

        monkeypatch.setattr(client, "_apply_trade_update", observed)

    @staticmethod
    def key(update: TradeUpdate) -> tuple[str, str, str]:
        return str(update.event), str(update.timestamp), str(update.execution_id)

    def post(self, venue: SimpleNamespace, raw: dict) -> tuple[asyncio.Event, asyncio.Event]:
        pair = asyncio.Event(), asyncio.Event()
        self.waiters[self.key(TradeUpdate.model_validate(raw))] = pair
        venue.stream.send(raw)
        return pair

    async def send(self, venue: SimpleNamespace, raw: dict) -> None:
        _, complete = self.post(venue, raw)
        await until(complete.is_set, "the fake stream update was not consumed")


class LogProbe:
    def __init__(self, original: Any) -> None:
        self.original = original
        self.lines: list[str] = []

    def __getattr__(self, name: str) -> Any:
        original = getattr(self.original, name)
        if name not in {"debug", "info", "warning", "error"}:
            return original

        def record(message: str, *args: Any, **kwargs: Any) -> Any:
            self.lines.append(str(message))
            return original(message, *args, **kwargs)

        return record


@dataclass
class HeldCase:
    client: AlpacaExecutionClient
    strategy: Trader
    venue: SimpleNamespace
    gate: EventGate
    stream: StreamProbe
    log: LogProbe
    placed: dict
    first_fill: dict
    cancel: dict
    mapped: asyncio.Event
    reports: list[tuple[OrderStatusReport, bool]] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    def command(self) -> Any:
        return SimpleNamespace(
            client_order_id=self.strategy.client_order_id,
            venue_order_id=VenueOrderId(self.placed["id"]),
            instrument_id=None,
            start=None,
            end=None,
            open_only=False,
        )

    async def query(
        self, kind: str = "query", allow_deferred_error: bool = False
    ) -> asyncio.Task[Any]:
        self.mapped.clear()
        hook = {
            "query": self.client._query_order,
            "single": self.client._generate_order_status_report,
            "bulk": self.client._generate_order_status_reports,
        }[kind]
        task = asyncio.create_task(hook(self.command()))
        self.tasks.append(task)
        await until(
            lambda: self.mapped.is_set() or task.done(), "the fake order read did not finish"
        )
        if task.done() and task.exception() is not None and not allow_deferred_error:
            task.result()
        await turn()
        return task

    def named_lines(self) -> list[str]:
        return [line for line in self.log.lines if str(self.strategy.client_order_id) in line]

    def hold_lines(self) -> list[str]:
        return [
            line
            for line in self.named_lines()
            if re.search(r"report|update|snapshot", line, re.IGNORECASE)
            and re.search(r"held|hold|wait|defer", line, re.IGNORECASE)
            and re.search(r"cache|engine|node", line, re.IGNORECASE)
            and re.search(r"clos|end", line, re.IGNORECASE)
            and not re.search(
                r"releas|resum|applied|timed out|ran out|abandon|discard|drop|expir",
                line,
                re.IGNORECASE,
            )
        ]

    def release_lines(self) -> list[str]:
        return [
            line
            for line in self.named_lines()
            if re.search(r"report|update|snapshot", line, re.IGNORECASE)
            and re.search(
                r"releas|resum|applied|timed out|ran out|abandon|discard|drop|expir",
                line,
                re.IGNORECASE,
            )
        ]

    async def close_cache(self) -> None:
        self.gate.release()
        await until(
            lambda: cached(self.client, self.strategy).is_closed,
            "the original queued fill and cancel did not close the cache",
        )

    async def cleanup(self) -> None:
        self.gate.release()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def ended_ahead_of_cache(
    client: AlpacaExecutionClient,
    strategy: Trader,
    venue: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> HeldCase:
    await until(lambda: "OrderAccepted" in strategy.names(), "the fake order was not accepted")
    gate = EventGate(client, monkeypatch)
    stream = StreamProbe(client, monkeypatch)
    placed = placed_order(venue)
    first = venue.trading.update(
        placed["id"], "partial_fill", 1, deliver=False, status="partially_filled", filled_qty="1"
    )
    await stream.send(venue, first)
    cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
    await stream.send(venue, cancel)
    assert gate.calls == ["generate_order_filled", "generate_order_canceled"]
    assert not cached(client, strategy).is_closed
    assert cached(client, strategy).filled_qty.as_double() == 0

    log = LogProbe(client._log)
    monkeypatch.setattr(client, "_log", log)
    case = HeldCase(
        client, strategy, venue, gate, stream, log, placed, first, cancel, asyncio.Event()
    )
    original_map = client._order_report
    original_report = client._handle_report

    async def mapped(*args: Any, **kwargs: Any) -> OrderStatusReport | None:
        report = await original_map(*args, **kwargs)
        case.mapped.set()
        return report

    def delivered(report: object, fills: Any = None) -> None:
        if isinstance(report, OrderStatusReport):
            case.reports.append((report, cached(client, strategy).is_closed))
        original_report(report, fills)

    monkeypatch.setattr(client, "_order_report", mapped)
    monkeypatch.setattr(client, "_handle_report", delivered)
    return case


def holding_strategy(venue: SimpleNamespace) -> Trader:
    venue.trading.script = "accept"
    strategy = trader(order_type="limit", quantity=4)
    strategy.stopped_for = "test owns shutdown"
    return strategy


@pytest.mark.parametrize("shown", [0, 1], ids=["older-report", "queued-fill-report"])
def test_h1_read_report_is_held_until_cache_close_then_released(venue, monkeypatch, shown) -> None:
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            case.placed["filled_qty"] = str(shown)
            await case.query()
            assert not case.reports, (
                "H1: a report reached the engine while its cache was still open"
            )
            assert case.gate.calls == ["generate_order_filled", "generate_order_canceled"]
            await case.close_cache()
            await until(
                lambda: bool(case.reports or case.release_lines()),
                "H1: closing the cache neither released nor ended the held report by name",
            )
            assert all(closed for _, closed in case.reports)
            assert all(r.client_order_id == strategy.client_order_id for r, _ in case.reports)
            assert trade_ids(cached(client, strategy)) == [case.first_fill["execution_id"]]
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("hold_bound_secs", [1.0], ids=["one-second-test-bound"])
def test_h2_a_cache_that_never_closes_cannot_hold_a_report_forever(
    venue, monkeypatch, hold_bound_secs: float
) -> None:
    # This is an explicitly named test deadline, not a literal copied from an implementation.
    # The property supplies no production bound/config name; REPORT.md records that limitation.
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            await case.query()
            await until(
                lambda: bool(case.release_lines()),
                f"H2: no named release/end within hold_bound_secs={hold_bound_secs} with cache open",
                hold_bound_secs,
            )
            assert case.hold_lines(), "H2: the report was never held before its bounded outcome"
            assert not case.gate.open, "the test must not close the cache to satisfy the bound"
            assert any(
                re.search(
                    r"bound|timeout|time.?out|timed out|ran out|limit|deadline", line, re.IGNORECASE
                )
                for line in case.release_lines()
            ), case.named_lines()
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("source", ["query", "stream"])
def test_h3_hold_and_release_each_log_the_order_and_reason(venue, monkeypatch, source) -> None:
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            if source == "query":
                await case.query()
            else:
                started, _ = case.stream.post(venue, case.cancel)
                await until(started.is_set, "the late cancel snapshot never reached the client")
            assert len(case.hold_lines()) == 1, (
                "H3: expected one line naming the held report, its order, and cache-close reason",
                case.named_lines(),
            )
            await case.close_cache()
            await until(
                lambda: bool(case.release_lines()), "H3: no named release after cache close"
            )
            assert len(case.release_lines()) == 1, case.named_lines()
            assert len(case.hold_lines()) == 1, "holding must not log on every loop turn"
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("source", ["query", "stream"])
def test_h4_a_held_snapshot_with_an_additional_fill_preserves_both_trade_ids_once(
    venue, monkeypatch, source
) -> None:
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            extra = venue.trading.update(
                case.placed["id"],
                "partial_fill",
                1,
                deliver=False,
                status="partially_filled",
                filled_qty="2",
            )
            snapshot = venue.trading.update(
                case.placed["id"], "canceled", deliver=False, status="canceled"
            )
            if source == "query":
                await case.query()
            else:
                started, _ = case.stream.post(venue, snapshot)
                await until(started.is_set, "the larger snapshot never reached the client")
            assert not case.reports, "H1/H4: the snapshot reached the engine before cache close"
            assert case.gate.calls == ["generate_order_filled", "generate_order_canceled"]
            await case.close_cache()
            await until(
                lambda: cached(client, strategy).filled_qty.as_double() == 2,
                "H4: closing the cache discarded the extra fill in the held snapshot",
            )
            # Both sources repeat both fills after release, including the same activity UUIDs.
            await case.stream.send(venue, case.first_fill)
            await case.stream.send(venue, extra)
            task = await case.query()
            async with asyncio.timeout(WAIT_BOUND_SECS):
                await task
            await turn()
            order = cached(client, strategy)
            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty.as_double() == strategy.filled == 2
            assert trade_ids(order) == [case.first_fill["execution_id"], extra["execution_id"]]
            assert strategy.names().count("OrderFilled") == 2
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@pytest.mark.parametrize("kind", ["single", "bulk"])
def test_h5_held_reports_never_return_successful_missing_venue_answers(
    venue, monkeypatch, kind
) -> None:
    strategy = holding_strategy(venue)

    def present(result: Any) -> bool:
        if kind == "single":
            return isinstance(result, OrderStatusReport)
        return any(r.client_order_id == strategy.client_order_id for r in result)

    async def body(client: AlpacaExecutionClient) -> None:
        case = await ended_ahead_of_cache(client, strategy, venue, monkeypatch)
        try:
            task = await case.query(kind, allow_deferred_error=True)
            deferred = task.done() and task.exception() is not None
            if task.done():
                if deferred:
                    reason = str(task.exception())
                    assert str(strategy.client_order_id) in reason
                    assert re.search(r"held|hold|wait|defer", reason, re.IGNORECASE), reason
                else:
                    assert present(task.result()), (
                        "H5: a held venue report became a successful None/empty answer; "
                        "the pinned engine can resolve the order as missing"
                    )
            await case.close_cache()
            if deferred:
                task = await case.query(kind)
            async with asyncio.timeout(WAIT_BOUND_SECS):
                result = await task
            assert present(result), (
                "H5: closing the cache must complete the original report request"
            )
            assert cached(client, strategy).filled_qty.as_double() == 1
        finally:
            await case.cleanup()

    run_with_client(body, strategy)


@dataclass
class ReadBarrier:
    requested: asyncio.Event = field(default_factory=asyncio.Event)
    start: asyncio.Event = field(default_factory=asyncio.Event)
    reading: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.mark.parametrize("read_number", [1, 2, 3])
@pytest.mark.parametrize("arrival", ["before", "during", "after"])
def test_h6_fill_update_before_during_or_after_each_catch_up_read_counts_once(
    venue, monkeypatch, read_number: int, arrival: str
) -> None:
    # The first read is inside the stream consumer: an update arriving during it is queued.
    # Reads two and three are background reads: the stream can overtake them completely.
    monkeypatch.setattr(execution, "FILL_READ_RETRY_SECS", 0.0)
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        await until(lambda: "OrderAccepted" in strategy.names(), "the fake order was not accepted")
        stream = StreamProbe(client, monkeypatch)
        placed = placed_order(venue)
        fill = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        activity = venue.trading.activities.pop()
        barriers = [ReadBarrier() for _ in range(3)]
        original = client._fill_activities
        reads = 0

        async def read(start: Any, order_ids: Any = None) -> Any:
            nonlocal reads
            if start is not None or not order_ids or placed["id"] not in order_ids:
                return await original(start, order_ids)
            reads += 1
            assert reads <= len(barriers), "H6: catch-up exceeded three reads"
            barrier = barriers[reads - 1]
            barrier.requested.set()
            await barrier.start.wait()
            barrier.reading.set()
            await barrier.finish.wait()
            try:
                return await original(start, order_ids)
            finally:
                barrier.returned.set()

        monkeypatch.setattr(client, "_fill_activities", read)
        cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        stream.post(venue, cancel)
        try:
            for index in range(read_number - 1):
                earlier = barriers[index]
                await until(earlier.requested.is_set, "an earlier catch-up read was not requested")
                earlier.start.set()
                earlier.finish.set()
                await until(
                    earlier.returned.is_set, "an earlier empty catch-up read did not return"
                )

            target = barriers[read_number - 1]
            await until(target.requested.is_set, "the selected catch-up read was not requested")
            if arrival == "before":
                _, consumed = stream.post(venue, fill)
                if read_number > 1:
                    await until(consumed.is_set, "the fill did not precede the background read")
            target.start.set()
            await until(target.reading.is_set, "the selected catch-up read did not start")
            if arrival == "during":
                stream.post(venue, fill)
            if arrival != "after" and read_number > 1:
                await until(
                    lambda: cached(client, strategy).is_closed,
                    "the stream did not overtake the outstanding background read",
                )
            venue.trading.activities.append(activity)
            target.finish.set()
            await until(target.returned.is_set, "the selected catch-up read did not return")
            if arrival == "after":
                await stream.send(venue, fill)
            await until(lambda: cached(client, strategy).is_closed, "the order did not end")
            await until(
                lambda: strategy.client_order_id not in client._catching_up,
                "the completed catch-up worker did not settle",
            )
            # A completed QueryOrder and another stream copy must not duplicate the activity.
            await client._query_order(
                cast(
                    Any,
                    SimpleNamespace(
                        venue_order_id=VenueOrderId(placed["id"]),
                        client_order_id=strategy.client_order_id,
                    ),
                )
            )
            await stream.send(venue, fill)
            order = cached(client, strategy)
            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty.as_double() == strategy.filled == 1
            assert trade_ids(order) == [fill["execution_id"]]
            assert strategy.names().count("OrderFilled") == 1
            assert strategy.names().count("OrderCanceled") == 1
            assert reads == read_number
        finally:
            for barrier in barriers:
                barrier.start.set()
                barrier.finish.set()

    run_with_client(body, strategy)


SEQUENTIAL_SCHEDULES = [
    "-".join(schedule)
    for read_count in range(4)
    for schedule in sorted(set(permutations(("F", "C", *("R" for _ in range(read_count))))))
]


@pytest.mark.parametrize("schedule", SEQUENTIAL_SCHEDULES)
def test_h6_every_sequential_order_of_fill_cancel_and_up_to_three_reads_counts_once(
    venue, monkeypatch, schedule: str
) -> None:
    strategy = holding_strategy(venue)

    async def body(client: AlpacaExecutionClient) -> None:
        await until(lambda: "OrderAccepted" in strategy.names(), "the fake order was not accepted")
        stream = StreamProbe(client, monkeypatch)
        placed = placed_order(venue)
        fill = venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        cancel = venue.trading.update(placed["id"], "canceled", deliver=False, status="canceled")
        command = SimpleNamespace(
            venue_order_id=VenueOrderId(placed["id"]), client_order_id=strategy.client_order_id
        )
        for event in schedule.split("-"):
            if event == "F":
                await stream.send(venue, fill)
            elif event == "C":
                await stream.send(venue, cancel)
            else:
                await client._query_order(cast(Any, command))
        await until(lambda: cached(client, strategy).is_closed, "the order did not end")
        await stream.send(venue, fill)
        order = cached(client, strategy)
        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty.as_double() == strategy.filled == 1
        assert trade_ids(order) == [fill["execution_id"]]
        assert strategy.names().count("OrderFilled") == 1
        assert strategy.names().count("OrderCanceled") == 1

    run_with_client(body, strategy)
