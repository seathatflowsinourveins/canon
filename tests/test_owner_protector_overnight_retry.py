"""F7: unread overnight assets share a minute of rest after the last blocking read.

Synthetic holdings and the existing command harness only: the test clock advances inside
the raw asset reader instead of sleeping, and no network or persistent book is used.
Contract: OwnerProtector discovery and start_level.overnight_answer's unread result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price

from tests.test_owner_protector import AAA
from tests.test_owner_protector import BBB
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import Harness
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture
from tests.test_owner_protector import position

CCC = InstrumentId.from_str("CCC.ALPACA")


class SlowAssets:
    """Each synthetic unread asset spends 31 seconds on the event thread, without a wait."""

    def __init__(self, strategy: Harness, *, malformed: bool = False) -> None:
        self.strategy = strategy
        self.malformed = malformed
        self.answers: dict[str, dict[str, Any]] = {}
        self.reads: list[tuple[str, int, int]] = []

    def get(self, route: str, params: Any = None) -> Any:
        assert route.startswith("/assets/"), route
        clock = self.strategy.test_clock
        start = clock.timestamp_ns()
        clock.set_time(start + 31 * SECOND)
        self.reads.append((route, start, clock.timestamp_ns()))
        if route in self.answers:
            return self.answers[route]
        if self.malformed:
            return None
        raise OSError("synthetic asset timeout")


def hold(strategy: Harness, instruments: tuple[InstrumentId, ...]) -> None:
    strategy.cached.held = []
    for instrument_id in instruments:
        held = position()
        held.instrument_id = instrument_id
        strategy.cached.held.append(held)


def step(strategy: Harness, at: int) -> None:
    strategy.test_clock.set_time(at)
    strategy._on_step(cast(Any, SimpleNamespace(ts_event=at)))


@pytest.mark.parametrize("instruments", [(AAA,), (AAA, BBB), (AAA, BBB, CCC)])
@pytest.mark.parametrize("malformed", [False, True], ids=["raises", "unreadable-answer"])
def test_unread_batch_waits_a_minute_after_its_last_read(
    instruments: tuple[InstrumentId, ...], malformed: bool
) -> None:
    strategy = Harness(session_part="overnight")
    reader = SlowAssets(strategy, malformed=malformed)
    strategy.settings.trading = reader
    hold(strategy, instruments)
    strategy.on_start()
    routes = [f"/assets/{instrument_id.symbol.value}" for instrument_id in instruments]
    assert [route for route, _, _ in reader.reads] == routes

    for batch in (1, 2):
        ended = reader.reads[-1][2]
        # Include the boundary at one nanosecond short, even when the whole batch took >60 s.
        for at in (ended + SECOND, ended + 59 * SECOND, ended + 60 * SECOND - 1):
            step(strategy, at)
            assert len(reader.reads) == batch * len(instruments), (
                "retried before a minute after the last read ended",
                reader.reads,
            )
        assert all(instrument_id in strategy.frozen for instrument_id in instruments)
        assert not strategy.slots
        step(strategy, ended + 60 * SECOND)
        assert [route for route, _, _ in reader.reads] == routes * (batch + 1), (
            "the due unread names must all be retried in the same pass",
            reader.reads,
        )


def test_two_slow_unread_names_leave_armed_quotes_and_steps_time_to_run(monkeypatch) -> None:
    strategy = Harness()
    strategy.levels[AAA] = Price.from_str("9.90")
    strategy.on_start()
    strategy.observe("10.00", strategy.clock.timestamp_ns() + SECOND)
    assert len(strategy.sent) == 1
    # The next discovery finds two unread names beside an already armed holding.
    strategy.settings.session_part = "overnight"
    reader = SlowAssets(strategy)
    strategy.settings.trading = reader
    hold(strategy, (AAA, BBB, CCC))
    managed: list[InstrumentId] = []
    original = strategy._manage

    def manage(instrument_id, *args, **kwargs):
        managed.append(instrument_id)
        return original(instrument_id, *args, **kwargs)

    monkeypatch.setattr(strategy, "_manage", manage)
    start = strategy.clock.timestamp_ns()
    quote_stamps = []
    for _ in range(10):
        at = strategy.clock.timestamp_ns() + SECOND
        strategy.observe("10.00", at)
        quote_stamps.append(at)
        step(strategy, at)
    elapsed = (strategy.clock.timestamp_ns() - start) // SECOND
    assert (len(reader.reads), elapsed) == (2, 72), (
        "F7 on the base: 20 failed reads consume 620 of 630 seconds across ten callbacks",
        len(reader.reads),
        elapsed,
    )
    assert managed == [AAA] * 20, "every quote and timer step must still manage the armed long"
    assert strategy.venue_quotes[AAA].ts_event == quote_stamps[-1]
    assert len(strategy.sent) == 1 and not strategy.canceled
    assert not any("failed:" in note for note in strategy.said)


@pytest.mark.parametrize("eligible", [False, True], ids=["answered-no", "answered-yes"])
def test_slow_answer_in_retry_batch_also_starts_the_shared_minute(eligible: bool) -> None:
    strategy = Harness(session_part="overnight")
    reader = SlowAssets(strategy)
    strategy.settings.trading = reader
    hold(strategy, (AAA, BBB))
    strategy.on_start()
    ended = reader.reads[-1][2]
    reader.answers["/assets/BBB"] = {"overnight_tradable": eligible}
    step(strategy, ended + 60 * SECOND)
    assert [route for route, _, _ in reader.reads] == ["/assets/AAA", "/assets/BBB"] * 2
    assert (BBB in strategy.slots) is eligible
    ended = reader.reads[-1][2]
    step(strategy, ended + 60 * SECOND - 1)
    assert len(reader.reads) == 4, "even an answered final read belongs to the blocking batch"
    step(strategy, ended + 60 * SECOND)
    assert [route for route, _, _ in reader.reads[4:]] == ["/assets/AAA"]
    # Answered names stay cached; a no-longer-held unread name must not be polled again.
    hold(strategy, (BBB,))
    step(strategy, reader.reads[-1][2] + 120 * SECOND)
    assert len(reader.reads) == 5
