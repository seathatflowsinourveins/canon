"""One holding's failure never costs another its protection (the lead, 2026-11-13 18:58: per-holding
isolation in the step, red first). A holding whose management raises in a step is named
`unprotected: step failed: <type>: <message>` and the step goes on to the next holding, to
discovery and to the book; a quote whose handling raises is named the same way for its own symbol."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import InstrumentId

from canon_trading.owner_protector import STEP_TIMER
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import position
from tests.test_owner_protector import quote

BBB = InstrumentId.from_str("BBB.ALPACA")


def two_holdings(monkeypatch: pytest.MonkeyPatch) -> tuple[Harness, list[Any], list[str]]:
    strategy = Harness()
    strategy.on_start()
    assert AAA in strategy.slots
    strategy.slots[BBB] = strategy.slots[AAA]  # A second holding; management is stood in below.
    managed: list[Any] = []
    after: list[str] = []

    def manage(instrument_id, quote=None, *, advanced=False) -> None:
        managed.append(instrument_id)
        if instrument_id == AAA:
            raise RuntimeError("boom")

    monkeypatch.setattr(strategy, "_manage", manage)
    monkeypatch.setattr(strategy, "_discover_holdings", lambda: after.append("discovered"))
    real_save = strategy._save_levels

    def save(*args: Any, **kwargs: Any) -> None:
        after.append("saved")
        real_save(*args, **kwargs)

    monkeypatch.setattr(strategy, "_save_levels", save)
    return strategy, managed, after


def test_one_holding_that_raises_in_a_step_leaves_the_others_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy, managed, after = two_holdings(monkeypatch)
    strategy._on_step(cast(Any, SimpleNamespace(ts_event=T0 + SECOND)))
    assert managed == [AAA, BBB], "the second holding was not managed after the first raised"
    assert after == ["discovered", "saved"], "the step stopped before discovery and the book"
    named = [n for n in strategy.said if n.startswith(f"{AAA}: unprotected: step failed:")]
    assert named == [f"{AAA}: unprotected: step failed: RuntimeError: boom"], strategy.said


def test_a_holding_that_raises_is_named_at_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy, managed, _after = two_holdings(monkeypatch)
    for n in (1, 2, 3):
        strategy._on_step(cast(Any, SimpleNamespace(ts_event=T0 + n * SECOND)))
    assert managed == [AAA, BBB] * 3
    assert sum("unprotected: step failed: RuntimeError: boom" in n for n in strategy.said) == 3


def test_a_quote_whose_handling_raises_is_named_for_its_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = Harness()
    strategy.on_start()

    def receive(tick, source) -> None:
        raise RuntimeError("bad quote")

    monkeypatch.setattr(strategy, "_receive_quote", receive)
    strategy.on_quote(quote("10.01", T0 + SECOND))  # Raises nothing into the engine.
    assert f"{AAA}: unprotected: quote failed: RuntimeError: bad quote" in strategy.said


def test_a_second_start_of_the_same_protector_is_refused_by_name() -> None:
    """L8's P2 (the lead, 2026-11-13 19:05): a protector object starts once. Its run, its sent
    sells and its holdings belong to that start; a second start of the same object is refused by
    name, and a restart is a new object built by the node."""
    strategy = Harness()
    strategy.on_start()
    sent = list(strategy.sent)
    with pytest.raises(RuntimeError, match="OwnerProtector starts once"):
        strategy.on_start()
    assert strategy.sent == sent, "the refused start sent something"


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Synthetic only, as the sibling owner-protector files: never inspect a credential environment
    or a paper lock (tests/conftest.py's autouse fixture reads the key variables)."""


def test_one_holding_whose_discovery_raises_leaves_the_others_found_and_the_timer_set(
    monkeypatch,
):
    """Codex's P1-b on 62941f39, by any route: discovery at the start had no per-holding boundary,
    so one holding's raise failed on_start, no other holding was found and no timer was set."""
    strategy = Harness()
    second = position()
    second.instrument_id = BBB
    strategy.cached.held.append(second)
    allowed = strategy._overnight_allowed

    def broken(instrument_id: InstrumentId) -> bool:
        if instrument_id == AAA:
            raise OverflowError("Python int too large to convert to C int")
        return allowed(instrument_id)

    monkeypatch.setattr(strategy, "_overnight_allowed", broken)
    strategy.on_start()
    assert BBB in strategy.slots and AAA not in strategy.slots
    assert f"{AAA}: ALERT: unprotected: discovery failed: OverflowError: " in "\n".join(
        strategy.said
    )
    assert STEP_TIMER in strategy.clock.timer_names(), "the start set no step timer"
    monkeypatch.setattr(strategy, "_overnight_allowed", allowed)
    strategy._on_step(cast(Any, SimpleNamespace(ts_event=strategy.clock.timestamp_ns())))
    assert AAA in strategy.slots, "the next step's discovery tries the holding again"
