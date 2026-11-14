"""An order event our own command publishes reads nothing of the engine's (the lead, 2026-11-13 22:34).

At the pin, `submit_order`, `cancel_order` and `modify_order` hold the strategy mutably borrowed
while the command runs (nautilus-trader d53849b216e2 `crates/trading/src/python/strategy.rs:2016-2037`),
so an event published inside one (an emulated trail its own submit releases at once, because its
carried level is already through the bid) reaches the handler while reading `cache`, `clock` or
`log` raises "Already mutably borrowed". This is the form main's Mover states (`mover.py:230-237`):
such a handler only stores what the event carries, in plain attributes, and `_manage`'s next step
does the reading. The proof-3 tape run at a9c5afce logged six such errors from
`on_order_released` (owner_protector.py:1396), each at a segment start whose carried level was
already through the bid.

A fill never reaches `on_order_filled` inside our own command: the simulated venue queues its
commands by default (`crates/backtest/src/exchange.rs:909`, `:2361`) and the adapter's submit is a
task (`packages/canon-alpaca/src/canon_alpaca/execution.py:1907`); so that handler keeps reading
the engine in its event (the lead's 19:17 ruling).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import Price

from canon_trading.owner_protector import OwnerProtector
from tests.test_owner_protector_blind import AAA
from tests.test_owner_protector_blind import owner_type
from tests.test_owner_protector_blind import replay

HANDLER_ERROR = re.compile(r"Python on_\w+ failed")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override conftest's credential-environment reader for this entirely offline file."""


def test_a_carried_level_through_the_bid_releases_at_arming_with_no_handler_error(capfd) -> None:
    """The engine run (the lead's R3): a level above the bid arms a trail the emulator releases in
    the protector's own submit. No handler error is logged, and the sale starts at the next step,
    stamped at the release's own time."""
    config, recording = owner_type()
    strategy = recording(config(feed="sip", levels={AAA: Price.from_str("10.50")}))
    with replay(strategies=(strategy,)) as harness:
        harness.drive(*(["10.00"] * 4))
        slot = strategy.slots[AAA]
        order = harness.engine.cache.order(strategy.sent[0]["id"])
        released = [e for e in order.events() if type(e).__name__ == "OrderReleased"]
        assert released, "the carried level through the bid must release the trail at arming"
        assert slot.sale is not None, "the released trail's sale never started"
        assert slot.sale.trigger_ns == released[0].ts_event
    logged = capfd.readouterr()
    errors = [line for line in (logged.out + logged.err).splitlines() if HANDLER_ERROR.search(line)]
    assert not errors, errors


class Borrowed(OwnerProtector):
    """The protector while one of its own commands runs: plain attributes work, the engine's raise."""

    def __init__(
        self,
    ) -> None:  # no engine, no super(): only the plain attributes a handler may use
        self.__dict__.update(
            slots={AAA: SimpleNamespace(protect_id="P-1", gain_applied_floor=None)},
            sent_ids={"P-1"},
            cancel_requested={"P-1"},
            venue_quotes={},
            released_at={},
            deferred_lines=[],
            # The strategy's own settings object, a plain attribute: both handlers read its count
            # to leave a two-quote sale's events to the step that judges them.
            settings=SimpleNamespace(breach_count=1),
        )

    def __getattribute__(self, name: str) -> Any:
        if name in {"cache", "clock", "log", "portfolio", "strategy_id", "msgbus"}:
            raise RuntimeError(f"Already mutably borrowed: {name}")
        return super().__getattribute__(name)


def test_the_release_handler_only_stores_the_release() -> None:
    strategy = Borrowed()
    event = SimpleNamespace(instrument_id=AAA, client_order_id="P-1", ts_event=7)
    OwnerProtector.on_order_released(strategy, cast(Any, event))
    assert strategy.released_at == {"P-1": 7}


def test_the_cancel_rejected_handler_reads_nothing_of_the_engines() -> None:
    strategy = Borrowed()
    event = SimpleNamespace(instrument_id=AAA, client_order_id="P-1", reason="too late")
    OwnerProtector.on_order_cancel_rejected(strategy, cast(Any, event))
    assert strategy.cancel_requested == set()
    assert strategy.deferred_lines == [
        f"{AAA}: our cancel rejected: too late; re-evaluate on the next quote or step"
    ]
