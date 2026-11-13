"""Blind reproductions of Antigravity #337 claims 1 and 2 at edec08cd.

C1 uses test_mover's offline BacktestEngine and its requests_sent monkeypatch seam.
C2 delivers complete bars to Mover.on_bar and checks the public ReentryState;
the trade-date contract is trail.profile_session_date (20:00 New York).
"""

from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import bar_type
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


def _ny_ns(hour: int, minute: int) -> int:
    return int(datetime(2026, 11, 12, hour, minute, tzinfo=mover.NEW_YORK).timestamp()) * SECOND


def _minute(closed_ns: int, close: str) -> Bar:
    price = Price.from_str(close)
    return Bar(
        bar_type(AAA),
        price,
        price,
        price,
        price,
        Quantity.from_int(100_000),
        closed_ns - MINUTE,
        closed_ns,
    )


def _sold_strategy(arm: str) -> Recording:
    """Seed a completed sale, as the existing test_mover_trail state cases do."""
    s = strategy(reentry_arm=arm, turn_k=2, turn_d=Decimal(1))
    s.slots[AAA] = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    sale_ns = _ny_ns(19, 50)
    s.sold[AAA] = (sale_ns, Decimal("10.00"))
    s.sold_fill_ns[AAA] = sale_ns
    s.reentry_states[AAA] = mover.ReentryState(sale_ns, Decimal("10.00"), Decimal("0.20"))
    return s
