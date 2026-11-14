"""Units that met the two-quote sale at a merge, each shown at a count of two on the pinned engine.

Contract: owner_protector's module header (the roster's read, review F1; the owner's hold of one
symbol) and sale_table.json (the hold is the table's stop, its release the event RL). The harness is
test_protector_node_new_symbol's: NautilusTrader's own `BacktestEngine`, its matching engine, and a
stand-in for the account's GET. No broker, credential, paper state, catalog or ledger.

* A long first bought outside the roster is named once and ends the run by name at a count of two
  as at one: the read is in the step, which both counts share.
* A symbol the owner holds is not sold on the engine while its quotes run under its level, and the
  same tape with no hold ends in a sale FILLED by the engine's own matching.

A NAMED DEPARTURE in the second case, and why: the pinned simulated venue refuses a sale of a long
another strategy holds, at every native venue setting (tape_replay's header cites
crates/execution/src/matching_engine/mod.rs:3583-3667), so a long under EXTERNAL can show a leg sent
and refused, never a fill. There the protector buys the long itself and is told to read inventory
under its own ID. Everything after the buy is the protector's own code and the engine's own
matching. What differs from a live run: the long's position events reach the protector, which they
do not for an EXTERNAL long.

NOT SHOWN ON THE ENGINE by these cases; the paper proof is where each is shown:
* discovery's own checks on a live account, which run degenerate under the departure: exactly one
  EXTERNAL long, and another strategy's sell or position on the instrument;
* a sale that reaches its end on quote and timer steps alone: under the departure the long's
  position events give the sale's step extra calls at the buy and at the fill;
* a leg in flight when a new holding outside the roster ends the run, and a count that completes
  between the end's request and the stop.

Every case fails on a handler error in the engine's log and on a step the protector swallowed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import OrderStatus
from nautilus_trader.model import StrategyId

from canon_trading import owner_protector
from canon_trading import symbol_hold
from tests import test_protector_node_new_symbol as n

alpaca_stream_lock = n.alpaca_stream_lock

AT_TWO: dict[str, Any] = {"breach_count": 2, "confirm_gap_ms": 1000}


def falling(seconds: range, *, falls_at: int) -> list[Any]:
    """AAA at 10.00, then at 9.00 from `falls_at`: far under any level armed at 10.00."""
    return [n.quote(s, "10.00" if s < falls_at else "9.00") for s in seconds]


OWN = StrategyId("Own-000")
"""The ID the engine gives the one `Own` strategy of a run: its class name and order tag."""


def own_booking(monkeypatch, account: n.Account, **changes: Any):
    """A protector that buys ten AAA itself at its first quote and reads inventory under its own ID,
    so the engine's venue fills its sell (the module docstring's named departure)."""
    configure, recording = n.owner_type()
    monkeypatch.setattr(owner_protector, "EXTERNAL", OWN)

    class Own(recording):
        def __init__(self, config) -> None:
            self.said: list[tuple[int, str]] = []
            self.bought = False
            super().__init__(config)
            self.supplied_for_holding = True  # no supplied level: the holding self-arms

        def _say(self, text: str) -> None:
            self.said.append((self.clock.timestamp_ns(), text))
            super()._say(text)

        def on_start(self) -> None:
            super().on_start()
            self.subscribe_quotes(n.AAA)

        def on_quote(self, quote) -> None:
            if not self.bought:
                self.bought = True
                self.submit_order(
                    self.order_factory.market(n.AAA, n.OrderSide.BUY, n.Quantity.from_int(10))
                )
            super().on_quote(quote)

    settings: dict[str, Any] = {"feed": "sip", "levels": {}, "trading": account, **AT_TWO}
    settings.update(changes)
    return Own(configure(**settings))


def sells(strategy) -> list[Any]:
    return [
        order
        for order in strategy.cache.orders(instrument_id=n.AAA, strategy_id=strategy.strategy_id)
        if order.side == n.OrderSide.SELL
    ]


def no_handler_failed(capfd, *strategies) -> None:
    """The real-run rule: a raise inside a handler is logged and swallowed by the engine, and one
    inside a step by the protector; either fails the case."""
    logged = capfd.readouterr()
    errors = [
        line for line in (logged.out + logged.err).splitlines() if n.HANDLER_ERROR.search(line)
    ]
    assert not errors, errors
    for strategy in strategies:
        swallowed = [line for _at, line in strategy.said if "step failed" in line]
        assert not swallowed, swallowed


def test_at_two_a_long_bought_outside_the_roster_is_named_and_ends_the_run(capfd) -> None:
    account = n.Account()
    owner = n.Owner(account)
    guard = n.protector(account, roster=(n.AAA,), **AT_TWO)
    eng = n.run((owner, guard), n.tape(range(200), later_at=40))
    try:
        bought = owner.filled_at["BBB"]
        named = n.alerts(guard, "BBB")
        assert len(named) == 1, guard.said
        at, line = named[0]
        assert line.startswith(f"{n.BBB}: ALERT: new holding outside the roster: 5 held")
        assert 0 <= at - bought <= n.BOUND_SECS * n.SECOND
        assert guard.shutdowns == [(at, "new holding outside the roster: BBB 5")], guard.shutdowns
        assert n.BBB not in guard.slots, "a holding outside the roster is never armed by this run"
        assert any(
            line.startswith(f"{n.AAA}: no carried level") and "self-armed" in line
            for said_at, line in guard.said
            if said_at < at
        ), "the roster's own holding is protected until the end"
        assert sells(guard) == [], "a run's end sells nothing"
    finally:
        eng.end()
        eng.dispose()
    no_handler_failed(capfd, guard)


def test_at_two_a_held_symbol_is_not_sold_and_the_same_tape_with_no_hold_ends_in_a_fill(
    tmp_path: Path, capfd, monkeypatch
) -> None:
    held_dir, free_dir = tmp_path / "held", tmp_path / "free"
    held_dir.mkdir()
    free_dir.mkdir()
    symbol_hold.write_hold(held_dir, "AAA")
    for name, folder in (("held", held_dir), ("free", free_dir)):
        guard = own_booking(monkeypatch, n.Account(), symbol_holds_dir=folder)
        eng = n.run((guard,), falling(range(120), falls_at=30))
        try:
            assert guard.strategy_id == OWN, guard.strategy_id
            said = [line for _at, line in guard.said]
            ours = sells(guard)
            long = sum(
                (p.signed_qty for p in guard.cache.positions_open(instrument_id=n.AAA)),
                Decimal(0),
            )
            slot = guard.slots.get(n.AAA)
            row = None if slot is None or slot.sale_state is None else slot.sale_state.row
            if name == "held":
                assert ours == [], said
                assert long == 10, (long, said)
                assert row == "T0", (row, said)
            else:
                filled = [order for order in ours if order.status == OrderStatus.FILLED]
                assert filled, ("no sell of ours FILLED", [str(order) for order in ours], said)
                assert sum(order.filled_qty.as_decimal() for order in ours) == 10
                assert long == 0, (long, said)
                assert any("sale record " in line for line in said), said
        finally:
            eng.end()
            eng.dispose()
        no_handler_failed(capfd, guard)
