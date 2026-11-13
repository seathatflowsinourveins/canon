"""Blind reproduction of Antigravity #337 claim 1 (P1-1) at edec08cd.

It uses test_mover's offline BacktestEngine and its requests_sent monkeypatch seam. Claim 2's
cases go with its own fix (P1-2).
"""

from typing import Any

import pytest
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover import tape


def test_entries_stop_when_this_steps_first_buy_reaches_the_soft_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ranked names qualify together with only one request left in the rolling minute."""
    eng = engine()
    s = strategy(slots=2, watch=2, soft_line=140, protect=False)
    starting_count = s.settings.soft_line - 1
    burst_ns = T0 + 5 * MINUTE - 10 * SECOND
    sent_ns: list[int] = []
    buys: list[tuple[InstrumentId, int, int]] = []

    def counted() -> int:
        # A fixed burst ten seconds before the first qualifying bar, then actual submits.
        # Reading the counter never itself sends or counts a request.
        background = starting_count if s.clock.timestamp_ns() >= burst_ns else 0
        return background + len(sent_ns)

    submit = s.submit_order

    def counted_submit(
        order: Any,
        position_id: Any = None,
        client_id: Any = None,
        params: Any = None,
    ) -> None:
        now = s.clock.timestamp_ns()
        recent = starting_count if 0 <= now - burst_ns < MINUTE else 0
        recent += sum(0 <= now - at < MINUTE for at in sent_ns)
        if order.side == OrderSide.BUY:
            buys.append((order.instrument_id, now, recent))
        # Protection is off: these are venue-bound limits, with no local emulator submits.
        sent_ns.append(now)  # requests_sent sees this synchronously, before the next candidate
        submit(order, position_id, client_id, params)

    monkeypatch.setattr(mover, "requests_sent", counted)
    monkeypatch.setattr(s, "submit_order", counted_submit)
    try:
        run(
            eng,
            s,
            ranker(rank((BBB, "0.4"), (AAA, "0.25"))),
            tape(AAA, RISING[:6]),
            tape(BBB, RISING[:6]),
        )
        assert buys, "the below-budget leading candidate must be eligible"
        assert buys[0][0] == BBB
        assert buys[0][2] == starting_count
        assert all(recent < s.settings.soft_line for _, _, recent in buys), buys
    finally:
        eng.dispose()
