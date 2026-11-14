"""Units that met the two-quote sale at a merge, each shown at a count of two on the pinned engine.

Contract: owner_protector's module header (the roster's read, review F1; the owner's hold of one
symbol) and sale_table.json (the hold is the table's stop, its release the event RL). The harness is
test_protector_node_new_symbol's: NautilusTrader's own `BacktestEngine`, its matching engine, an
owner strategy under EXTERNAL that buys, and a stand-in for the account's GET. No broker,
credential, paper state, catalog or ledger.

* A long first bought outside the roster is named once and ends the run by name at a count of two
  as at one: the read is in the step, which both counts share.
* A symbol the owner holds is not sold on the engine while its quotes run under its level, and the
  same tape with no hold sells it, so the case has teeth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from canon_trading import symbol_hold
from tests import test_protector_node_new_symbol as n

alpaca_stream_lock = n.alpaca_stream_lock

AT_TWO: dict[str, Any] = {"breach_count": 2, "confirm_gap_ms": 1000}


def falling(seconds: range, *, falls_at: int) -> list[Any]:
    """AAA at 10.00, then at 9.00 from `falls_at`: far under any level armed at 10.00."""
    return [n.quote(s, "10.00" if s < falls_at else "9.00") for s in seconds]


def sells(strategy) -> list[Any]:
    return [
        order
        for order in strategy.cache.orders(instrument_id=n.AAA, strategy_id=strategy.strategy_id)
        if order.side == n.OrderSide.SELL
    ]


def test_at_two_a_long_bought_outside_the_roster_is_named_and_ends_the_run() -> None:
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


def test_at_two_a_held_symbol_is_not_sold_under_its_level_on_the_engine(tmp_path: Path) -> None:
    held_dir, free_dir = tmp_path / "held", tmp_path / "free"
    held_dir.mkdir()
    free_dir.mkdir()
    symbol_hold.write_hold(held_dir, "AAA")
    outcomes: dict[str, tuple[int, str | None, list[str]]] = {}
    for name, folder in (("held", held_dir), ("free", free_dir)):
        account = n.Account()
        guard = n.protector(account, symbol_holds_dir=folder, **AT_TWO)
        eng = n.run((n.Owner(account), guard), falling(range(120), falls_at=30))
        try:
            slot = guard.slots.get(n.AAA)
            row = None if slot is None or slot.sale_state is None else slot.sale_state.row
            outcomes[name] = (len(sells(guard)), row, [line for _at, line in guard.said])
        finally:
            eng.end()
            eng.dispose()
    sold, row, said = outcomes["held"]
    assert sold == 0, said
    assert row == "T0", (row, said)
    sold, _row, said = outcomes["free"]
    assert sold >= 1, ("the same tape with no hold sells, so the hold is what stopped it", said)
