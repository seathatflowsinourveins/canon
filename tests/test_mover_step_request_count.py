"""Mover's own soft-line contract: count this step's venue commands before later buys.

Use test_mover's offline BacktestEngine and generated tapes. The counter can stay behind
the strategy for the entire step, as the adapter's worker-thread transport can in a node.
"""

from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TriggerType

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import VENUE
from tests.test_mover import ReportedAccount
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover import tape


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("headroom", [1, 2])
def test_each_buy_counts_before_the_next_candidate_without_double_counting(
    monkeypatch: pytest.MonkeyPatch, synchronous: bool, headroom: int
) -> None:
    eng = engine()
    s = strategy(slots=2, watch=2, soft_line=140, protect=False)
    burst_ns = T0 + 5 * MINUTE - 10 * SECOND
    sent: list[tuple[Any, int]] = []
    submit = s.submit_order

    def counted() -> int:
        background = s.settings.soft_line - headroom if s.clock.timestamp_ns() >= burst_ns else 0
        return background + (len(sent) if synchronous else 0)

    def submitted(order: Any, position_id=None, client_id=None, params=None) -> None:
        sent.append((order, s.clock.timestamp_ns()))
        submit(order, position_id, client_id, params)

    monkeypatch.setattr(mover, "requests_sent", counted)
    monkeypatch.setattr(s, "submit_order", submitted)
    try:
        run(
            eng,
            s,
            ranker(rank((BBB, "0.4"), (AAA, "0.25"))),
            tape(AAA, RISING[:6]),
            tape(BBB, RISING[:6]),
        )
        buys = [(order.instrument_id, at) for order, at in sent if order.side == OrderSide.BUY]
        assert [instrument for instrument, _ in buys] == [BBB, AAA][:headroom], buys
        assert len({at for _, at in buys}) == 1
        if headroom == 1:
            assert any("AAA.ALPACA" in why and "140 requests" in why for why in s.dropped)
    finally:
        eng.dispose()


@pytest.mark.parametrize("exit_command", ["limit sell", "chase cancel", "released stop"])
def test_manage_exit_traffic_reaches_the_soft_line_before_a_buy(
    monkeypatch: pytest.MonkeyPatch, exit_command: str
) -> None:
    """AAA is already held when BBB qualifies; its exempt exit spends the last request."""
    eng = engine()
    s = strategy(slots=2, watch=2, soft_line=140, protect=False, chase_secs=1)
    burst = False
    target_ns: int | None = None
    sent: list[tuple[str, int]] = []
    manage, submit, cancel = s._manage, s.submit_order, s.cancel_order

    def counted() -> int:
        nonlocal burst
        candidate = s.slots.get(BBB)
        burst |= candidate is not None and candidate.run_ns is not None
        # No command sent by this step is visible to the adapter's counter yet.
        return s.settings.soft_line - 1 if burst else 0

    def managed(slot: mover.Slot, now: int) -> None:
        nonlocal target_ns
        if slot.instrument_id == AAA and s._held(slot):
            candidate = s.slots.get(BBB)
            if candidate is not None and candidate.run_ns is not None and target_ns is None:
                target_ns = now
                if exit_command == "released stop":
                    # An emulated stop whose trigger the bid is already through: the emulator
                    # matches it as it is submitted and releases it to the venue at once
                    # (emulator.rs:679-699 at the pin), so the submit is a venue request.
                    instrument = s.cache.instrument(AAA)
                    quote = s.cache.quote(AAA)
                    assert instrument is not None and quote is not None
                    above = quote.bid_price.as_decimal() + Decimal("0.5")
                    stop = s.order_factory.stop_limit(
                        AAA,
                        OrderSide.SELL,
                        instrument.make_qty(s._held(slot)),
                        instrument.make_price(above - Decimal(1)),
                        instrument.make_price(above),
                        trigger_type=TriggerType.BID_ASK,
                        emulation_trigger=TriggerType.BID_ASK,
                    )
                    s.submit_order(stop)
                    released = s.cache.order(stop.client_order_id)
                    assert released is not None and not released.is_emulated
                else:
                    slot.exiting = True
                    slot.floor_released = True  # the sale is free to chase the current bid
            elif exit_command == "chase cancel" and slot.protect_id is None:
                # Leave a real limit resting well above the quote, ready for _manage to chase.
                s._limit_sell(slot, s._held(slot), now, Decimal(20))
        manage(slot, now)

    def submitted(order: Any, position_id=None, client_id=None, params=None) -> None:
        kind = "released stop" if order.emulation_trigger is not None else "limit sell"
        sent.append(("buy" if order.side == OrderSide.BUY else kind, s.clock.timestamp_ns()))
        submit(order, position_id, client_id, params)

    def canceled(client_order_id, client_id=None, params=None) -> None:
        sent.append(("chase cancel", s.clock.timestamp_ns()))
        cancel(client_order_id, client_id, params)

    monkeypatch.setattr(mover, "requests_sent", counted)
    monkeypatch.setattr(s, "_manage", managed)
    monkeypatch.setattr(s, "submit_order", submitted)
    monkeypatch.setattr(s, "cancel_order", canceled)
    try:
        run(
            eng,
            s,
            ranker(rank((AAA, "0.4"), (BBB, "0.25"))),
            tape(AAA, [*RISING, 10.8, 10.9]),
            tape(BBB, [*[10.0] * 5, *RISING[:5]]),
        )
        assert target_ns is not None, "BBB must qualify after AAA's entry filled"
        this_step = [command for command, at in sent if at == target_ns]
        assert exit_command in this_step, sent
        assert "buy" not in this_step, this_step
        assert any("BBB.ALPACA" in why and "140 requests" in why for why in s.dropped)
    finally:
        eng.dispose()


def test_a_fill_account_query_reaches_the_soft_line_before_a_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending fill causes the real account-query path to use the last request of the step."""
    eng = engine()
    s = strategy(soft_line=140, protect=False)
    account, ask, query = s._trading_account, s._ask_for_the_account, s.query_account
    reported: ReportedAccount | None = None
    queried: list[int] = []
    seeded = False
    burst_ns = T0 + 5 * MINUTE - 10 * SECOND

    def trading_account(venue: Any) -> Any:
        nonlocal reported
        real = account(venue)
        if real is not None and reported is None:
            reported = ReportedAccount(real)
        return reported

    def ask_after_fill() -> None:
        nonlocal seeded
        slot = s.slots.get(AAA)
        if slot is not None and slot.run_ns is not None and not seeded:
            # Deliver a previous buy's fill at the step with the next eligible entry.
            s.fills_to_report.append((VENUE, OrderSide.BUY, Decimal(1)))
            seeded = True
        ask()

    def queried_account(account_id, client_id=None, params=None) -> None:
        queried.append(s.clock.timestamp_ns())
        query(account_id, client_id, params)

    monkeypatch.setattr(
        mover,
        "requests_sent",
        lambda: s.settings.soft_line - 1 if s.clock.timestamp_ns() >= burst_ns else 0,
    )
    monkeypatch.setattr(s, "_trading_account", trading_account)
    monkeypatch.setattr(s, "_ask_for_the_account", ask_after_fill)
    monkeypatch.setattr(s, "query_account", queried_account)
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING[:6]))
        assert queried, "the pending fill must query the reported account"
        assert not [order for order in orders if order.side == OrderSide.BUY]
        assert any("140 requests" in why for why in s.dropped)
    finally:
        eng.dispose()
