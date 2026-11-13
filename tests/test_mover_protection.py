"""Window protection diagnostics from pinned Nautilus order events, without a venue connection.

Contract: nt@d53849b216e2 model Order*.ts_event, OrderFilled.last_qty, OrderUpdated.quantity,
and Order.events(); emulator.rs preserves pre-release events on the transformed limit.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.core import UUID4
from nautilus_trader.model import AccountId
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Currency
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import OrderAccepted
from nautilus_trader.model import OrderCanceled
from nautilus_trader.model import OrderEmulated
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderInitialized
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import OrderUpdated
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import VenueOrderId

from canon_trading import mover

SECOND = 1_000_000_000
T0 = 1_762_790_400_000_000_000


def event(kind: type, cid: str, second: float, *, qty: int = 10, symbol: str = "AAA") -> Any:
    """Real upstream events; the event clock can differ from the node's receipt clock."""
    common: dict[str, Any] = {
        "trader_id": TraderId("TESTER-001"),
        "strategy_id": StrategyId("Mover-001"),
        "instrument_id": InstrumentId.from_str(f"{symbol}.ALPACA"),
        "client_order_id": ClientOrderId(cid),
        "event_id": UUID4(),
        "ts_event": T0 + round(second * SECOND),
        "ts_init": T0 + round(second * SECOND) + 123,
    }
    venue: dict[str, Any] = {
        "venue_order_id": VenueOrderId(cid),
        "account_id": AccountId("TEST-001"),
    }
    side = OrderSide.BUY if cid.startswith("B") else OrderSide.SELL
    if kind is OrderInitialized:
        return kind(
            **common,
            order_side=side,
            order_type=OrderType.LIMIT,
            quantity=Quantity.from_int(qty),
            time_in_force=TimeInForce.DAY,
            post_only=False,
            reduce_only=False,
            quote_quantity=False,
            reconciliation=False,
            price=Price.from_str("10.00"),
        )
    if kind is OrderFilled:
        return kind(
            **common,
            **venue,
            trade_id=TradeId(str(common["event_id"])),
            order_side=side,
            order_type=OrderType.LIMIT,
            last_qty=Quantity.from_int(qty),
            last_px=Price.from_str("10.00"),
            currency=Currency.from_str("USD"),
            liquidity_side=LiquiditySide.TAKER,
            reconciliation=False,
        )
    if kind is OrderAccepted:
        return kind(**common, **venue, reconciliation=False)
    if kind is OrderCanceled:
        return kind(**common, reconciliation=False)
    if kind is OrderReleased:
        return kind(**common, released_price=Price.from_str("10.00"))
    if kind is OrderUpdated:
        return kind(**common, quantity=Quantity.from_int(qty), reconciliation=False)
    return kind(**common)


def order(cid: str, *events: Any, qty: int = 10, symbol: str = "AAA") -> Any:
    init = event(OrderInitialized, cid, 0, qty=qty, symbol=symbol)
    return SimpleNamespace(
        client_order_id=init.client_order_id,
        instrument_id=init.instrument_id,
        side=init.order_side,
        init_event=init,
        events=lambda: [init, *events],
    )


def diagnostics(*orders: Any, end: float = 20, part: str = "overnight") -> tuple[list, dict]:
    entries = [
        {"client_order_id": o.client_order_id.value, "instrument_id": str(o.instrument_id)}
        for o in orders
        if o.side == OrderSide.BUY
    ]
    return mover.protection_diagnostics(entries, orders, part=part, end_ns=T0 + round(end * SECOND))


def test_protection_emulated_at_the_fill_has_zero_gap() -> None:
    entries, summary = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", event(OrderEmulated, "S-1", 1)),
    )
    (got,) = entries
    assert got["part"] == "overnight"
    assert got["first_fill_ns"] == T0 + SECOND
    assert got["first_fill_to_protection_ms"] == 0
    assert got["max_unprotected_ms"] == 0
    assert summary["overnight"]["entries_over_one_second"] == 0


@pytest.mark.parametrize("venue_accepted", [True, False])
def test_release_needs_a_working_limit_and_cancellation_ends_its_coverage(venue_accepted) -> None:
    events = [event(OrderEmulated, "S-1", 1), event(OrderReleased, "S-1", 2)]
    if venue_accepted:
        events.append(event(OrderAccepted, "S-1", 4))
    events.append(event(OrderCanceled, "S-1", 8))
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", *events),
        order("S-2", event(OrderAccepted, "S-2", 10)),
        end=12,
    )
    # Release is not acceptance: 2..4 is uncovered. Accepted limit covers 4..8,
    # cancellation uncovers 8..10. Without acceptance, the whole 2..10 is uncovered.
    assert entries[0]["max_unprotected_ms"] == (2000 if venue_accepted else 8000)


def test_partial_entry_fills_need_coverage_for_every_held_share() -> None:
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1, qty=4), event(OrderFilled, "B-1", 4, qty=6)),
        order("S-1", event(OrderEmulated, "S-1", 3), event(OrderCanceled, "S-1", 7), qty=4),
        order("S-2", event(OrderEmulated, "S-2", 8)),
    )
    assert entries[0]["first_fill_to_protection_ms"] == 2000
    # 1..3 starts unprotected; 4..8 has at least six uncovered shares.
    assert entries[0]["max_unprotected_ms"] == 4000


@pytest.mark.parametrize("sold_at,expected", [(9, 4000), (None, 15000)])
def test_canceled_trail_gap_ends_at_the_sale_or_window_end(sold_at, expected) -> None:
    orders = [
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", event(OrderEmulated, "S-1", 1), event(OrderCanceled, "S-1", 5)),
    ]
    if sold_at is not None:
        orders.append(order("S-2", event(OrderFilled, "S-2", sold_at)))
    entries, summary = diagnostics(*orders)
    assert entries[0]["max_unprotected_ms"] == expected
    assert summary["overnight"]["entries_over_one_second"] == 1


def test_a_partial_sale_reduces_both_held_shares_and_working_sale_quantity() -> None:
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order(
            "S-1",
            event(OrderEmulated, "S-1", 1),
            event(OrderReleased, "S-1", 2),
            event(OrderFilled, "S-1", 2.5, qty=4),
            event(OrderCanceled, "S-1", 6),
        ),
        end=9,
    )
    # The partial fill proves the remaining released limit works from 2.5..6;
    # its cancellation leaves six shares without a working sale until 9.
    assert entries[0]["max_unprotected_ms"] == 3000


def test_a_quantity_update_can_leave_some_shares_without_coverage() -> None:
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order(
            "S-1",
            event(OrderAccepted, "S-1", 1),
            event(OrderUpdated, "S-1", 3, qty=4),
            event(OrderUpdated, "S-1", 5, qty=10),
        ),
    )
    assert entries[0]["max_unprotected_ms"] == 2000


def test_never_protected_and_unfilled_entries_are_distinct_and_summary_keeps_the_worst() -> None:
    entries, summary = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("B-2", event(OrderFilled, "B-2", 5, symbol="BBB"), symbol="BBB"),
        order("S-2", event(OrderAccepted, "S-2", 6.000000001, symbol="BBB"), symbol="BBB"),
        order("B-3", symbol="CCC"),
        end=10,
    )
    first, second, unfilled = entries
    assert first["first_fill_to_protection_ms"] is None
    assert first["max_unprotected_ms"] == 9000
    assert second["first_fill_to_protection_ms"] == 1000.000001
    assert unfilled["first_fill_ns"] is None and unfilled["max_unprotected_ms"] is None
    assert summary == {
        "overnight": {
            "entries": 3,
            "filled_entries": 2,
            "entries_never_protected": 1,
            "worst_first_fill_to_protection_ms": 1000.000001,
            "worst_unprotected_ms": 9000,
            "entries_over_one_second": 2,
        }
    }


def test_one_second_exactly_does_not_exceed_the_threshold() -> None:
    _, summary = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", event(OrderAccepted, "S-1", 2)),
        part="regular",
    )
    assert summary["regular"]["entries_over_one_second"] == 0


def test_reentry_does_not_inherit_the_previous_holdings_gap() -> None:
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", event(OrderFilled, "S-1", 4)),
        order("B-2", event(OrderFilled, "B-2", 8)),
        order("S-2", event(OrderEmulated, "S-2", 8)),
    )
    assert [e["max_unprotected_ms"] for e in entries] == [3000, 0]
    assert entries[1]["first_fill_to_protection_ms"] == 0


def test_transformed_limit_initialization_does_not_restart_protection() -> None:
    entries, _ = diagnostics(
        order("B-1", event(OrderFilled, "B-1", 1)),
        order(
            "S-1",
            event(OrderEmulated, "S-1", 1),
            event(OrderInitialized, "S-1", 2),
            event(OrderReleased, "S-1", 2),
            event(OrderAccepted, "S-1", 2.025),
            event(OrderFilled, "S-1", 3),
        ),
    )
    assert entries[0]["first_fill_to_protection_ms"] == 0
    assert entries[0]["max_unprotected_ms"] == 25


def test_real_engine_events_reach_the_protection_record_without_changing_orders() -> None:
    from tests.test_mover import AAA
    from tests.test_mover import FALL
    from tests.test_mover import MOVER_ID
    from tests.test_mover import RISING
    from tests.test_mover import engine
    from tests.test_mover import rank
    from tests.test_mover import ranker
    from tests.test_mover import run
    from tests.test_mover import strategy
    from tests.test_mover import tape

    eng = engine()
    s = strategy()
    try:
        orders = run(eng, s, ranker(rank((AAA, "1.2"))), tape(AAA, RISING + FALL))
        record = s.record()
        entries, summary = mover.protection_diagnostics(
            record["entries"],
            orders,
            part="regular",
            end_ns=s.clock.timestamp_ns(),
        )
        assert entries and summary["regular"]["filled_entries"] == 1
        assert entries[0]["first_fill_to_protection_ms"] is not None
        assert Decimal(str(entries[0]["max_unprotected_ms"])) >= 0
        assert not eng.cache.positions_open(strategy_id=MOVER_ID)
        assert any(any(isinstance(e, OrderReleased) for e in o.events()) for o in orders)
    finally:
        eng.dispose()


@pytest.mark.parametrize("broken", [False, True])
def test_window_records_protection_and_names_a_diagnostic_failure(
    monkeypatch, tmp_path, broken, references_in
):
    from canon_trading import session_window
    from tests.test_session_window import Assets
    from tests.test_session_window import Quoted
    from tests.test_session_window import mover_pick
    from tests.test_session_window import quiet_window

    class DiagnosticMover:
        """Window seam: an observed clock without starting a node or opening a connection."""

        def __init__(self, config):
            self.settings = config
            self.clock = SimpleNamespace(timestamp_ns=lambda: T0 + 20 * SECOND)
            self.dropped = []
            self.history_bars = self.history_dropped = 0
            self.ranking_records = []
            self.opened_slots = set()  # the on-demand overlay's final account check reads it

        def record(self):
            return {"entries": [], "ranking_records": self.ranking_records}

    sent = [{"client_order_id": "B-1", "instrument_id": "AAA.ALPACA", "ahead": "0.5"}]
    orders = [
        order("B-1", event(OrderFilled, "B-1", 1)),
        order("S-1", event(OrderEmulated, "S-1", 1)),
    ]
    observed = []

    def protection(entries, cached_orders, **kwargs):
        observed.append(kwargs)
        assert cached_orders == []  # the fixture's strategy-filtered cache
        if broken:
            raise ValueError("protection evidence unavailable")
        return mover.protection_diagnostics(entries, orders, **kwargs)

    monkeypatch.setattr(session_window, "Mover", DiagnosticMover)
    monkeypatch.setattr(session_window, "choose_mover", lambda *a, **k: mover_pick())
    monkeypatch.setattr(session_window, "window_data", lambda: Quoted())
    monkeypatch.setattr(session_window, "mover_entry_contexts", lambda *a, **k: {})
    monkeypatch.setattr(session_window, "mover_entries", lambda *a: sent)
    monkeypatch.setattr(session_window, "protection_diagnostics", protection)
    tallies = iter([{"GET a/v2/clock": 1}, {"GET a/v2/clock": 3}])
    monkeypatch.setattr(
        session_window, "endpoint_counts", lambda: next(tallies, {"GET a/v2/clock": 3})
    )

    def roster(run):
        if broken:
            raise KeyError("instrument_id")
        return {"read_from": sorted(run)}

    monkeypatch.setattr(session_window, "roster_entries", roster)
    _, report, _ = quiet_window(
        monkeypatch,
        tmp_path,
        quotes=5,
        trades=5,
        account=Assets(),
        strategy="Mover",
        mover=True,
    )
    assert report["result"] == "ok"
    assert observed == [{"part": "regular", "end_ns": T0 + 20 * SECOND}]
    assert report["mover"]["http_requests"] == {
        "to_node_start": {"GET a/v2/clock": 1},
        "to_window_end": {"GET a/v2/clock": 3},
    }
    # read from the strategy's own record; a failure is named and fails nothing
    # (code-review-high/universe-2-2, -8)
    assert report["mover"]["entries_by_roster"] == (
        "not read: KeyError: 'instrument_id'"
        if broken
        else {"read_from": ["entries", "ranking_records"]}
    )
    if broken:  # the entry rows are read on their own, so a protection failure keeps them
        assert report["mover"]["entries"] == sent
        assert (
            report["mover"]["protection_by_part"]
            == "not read: ValueError: protection evidence unavailable"
        )
    else:
        assert report["mover"]["entries"][0]["ahead"] == "0.5"
        assert report["mover"]["entries"][0]["max_unprotected_ms"] == 0
        assert report["mover"]["protection_by_part"]["regular"]["filled_entries"] == 1
