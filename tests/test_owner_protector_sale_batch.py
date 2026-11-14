"""Anything raised inside a two-quote sale's step keeps the rest of its stored batch.

Contract: owner_protector's module header ("Order handlers only enqueue event payloads. Steps replay
them on independent native LimitOrders, in arrival order"). A step takes the stored order events of
its holding before it reads the account and judges them one by one. Whatever is raised on the way,
the events not yet judged are put back, in their order and ahead of any echo of this step, so the
next step judges them: a lost acknowledgement or fill would leave the holding with no leg and a
false line. The event during whose judging something was raised is not judged again.
The stand-ins are test_owner_protector's. No broker, credential, paper state, catalog or ledger.
"""

from __future__ import annotations

from types import SimpleNamespace

from canon_trading import sale_table
from tests import test_owner_protector as h
from tests import test_owner_protector_sale_table as c

engine_checks = c.engine_checks
isolated_level_books = c.isolated_level_books
alpaca_stream_lock = c.alpaca_stream_lock


def step(strategy, seconds: int | None = None) -> None:
    if seconds is not None:
        strategy.test_clock.set_time(h.T0 + seconds * h.SECOND)
    strategy._on_step(SimpleNamespace(ts_event=strategy.clock.timestamp_ns()))


def raise_once(monkeypatch, strategy) -> None:
    """The account read of the next step raises once: nothing of the table has run yet."""
    original, raised = strategy._sale_snapshot, []

    def once(*args, **kwargs):
        if not raised:
            raised.append(True)
            raise RuntimeError("synthetic snapshot failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(strategy, "_sale_snapshot", once)


def test_a_raise_before_the_table_keeps_a_stored_cancel_acknowledgement(monkeypatch) -> None:
    """Our re-price cancel is acknowledged; the step that would judge it raises in its account read.
    The acknowledgement must still be judged: the next leg leaves, and no line says the cancel had
    no answer."""
    strategy = c.counted()
    c.first_leg(strategy)
    strategy.observe("9.70", h.T0 + 60 * h.SECOND)
    assert c.row(strategy).row == "PCR", c.row(strategy)
    strategy.deliver("Canceled", ts_event=h.T0 + 62 * h.SECOND)
    raise_once(monkeypatch, strategy)
    step(strategy, 62)
    assert any("step failed" in line for line in strategy.said), strategy.said
    assert strategy._sale_events.get(h.AAA), "the stored acknowledgement is still stored"
    step(strategy, 63)
    assert len(strategy.sent) == 2, ("the next leg leaves on the acknowledgement", c.row(strategy))
    assert c.row(strategy).row == "FL", c.row(strategy)
    step(strategy, 200)
    assert not [line for line in strategy.said if "sale cancel has no answer" in line], (
        "the cancel was answered: only the new leg's own submit may be told as unanswered"
    )


def test_a_raise_before_the_table_keeps_a_stored_fill(monkeypatch) -> None:
    """The same loss of a whole fill left a cancel recorded for a closed leg and a false line."""
    strategy = c.counted()
    c.first_leg(strategy)
    strategy.deliver("Filled", quantity=10)
    raise_once(monkeypatch, strategy)
    step(strategy, 10)
    step(strategy, 11)
    assert c.row(strategy).row == "X", c.row(strategy)
    assert strategy.canceled == []
    step(strategy, 200)
    assert not [line for line in strategy.said if "no answer" in line], strategy.said


def test_a_table_error_on_one_stored_event_does_not_lose_the_ones_after_it(monkeypatch) -> None:
    """Two part fills are stored; the table raises on the first. The holding freezes, and the
    second fill still reaches our copy of the leg at the next step, so the copy and the engine's
    cache agree and nothing is later decided from a copy that is one event behind."""
    strategy = c.counted()
    c.first_leg(strategy)
    leg = strategy.cached.working[-1]
    strategy.deliver("Filled", quantity=1)
    strategy.deliver("Filled", quantity=1)
    original, seen = sale_table.transition, []

    def first_fill_raises(state, n, code, facts):
        seen.append(code)
        if code == "oPF" and seen.count("oPF") == 1:
            raise sale_table.InvalidFacts("synthetic table error on the first stored event")
        return original(state, n, code, facts)

    monkeypatch.setattr(sale_table, "transition", first_fill_raises)
    step(strategy, 10)
    assert c.row(strategy).row in {"FZ", "FZW", "FZP"}, c.row(strategy)
    step(strategy, 11)
    copy = strategy._sale_orders[leg.client_order_id]
    assert copy.filled_qty.as_decimal() == leg.filled_qty.as_decimal() == 2
    assert c.row(strategy).row in {"FZ", "FZW", "FZP"}, "a later event never lifts the fault"
    assert strategy.canceled == [], "nothing is canceled from a copy that is behind"
