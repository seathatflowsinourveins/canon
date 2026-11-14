"""F15: a carried level survives a gap through its limit offset and sells after confirmation.

Synthetic holdings, quotes and native orders only. The level book lives in pytest's temp tree.
The price oracle is the existing sale contract: one venue tick through the bid, at least 0.0001;
the first sale's chase floor is two percent below it, rounded up onto that same venue step.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce

from canon_trading.owner_protector import OwnerProtector
from tests import test_owner_protector as h
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture
from tests.test_owner_protector import isolated_level_books  # noqa: F401 -- temp books only
from tests.test_owner_protector_chase import accepted
from tests.test_owner_protector_chase import terminal

START = h.T0 + 180 * h.SECOND


class Recording(h.Harness):
    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self.logged: list[tuple[str, str]] = []
        self.test_log = SimpleNamespace(
            info=lambda text: self.logged.append(("INFO", text)),
            error=lambda text: self.logged.append(("ERROR", text)),
        )

    @property
    def log(self) -> Any:
        return self.test_log

    def _say(self, text: str) -> None:
        self.said.append(text)
        OwnerProtector._say(self, text)


def carried_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bid: str) -> Recording:
    path = tmp_path / "levels.json"
    record = h.restart_record(last_level="2.00", distance="0.30", limit_offset="0.38")
    path.write_text(json.dumps({"schema": 2, "entries": [record]}))
    strategy = Recording(
        account_label="shape",
        level_book_path=path,
        run="shape-next",
        session_part="regular",
        trading=h.VenueOrders(),
    )
    monkeypatch.setattr(strategy.cached, "instrument", lambda iid: h.equity(iid, precision=4))
    strategy.cached.latest = h.quote(bid, START)
    strategy.test_clock.set_time(START)
    strategy.on_start()
    return strategy


def step(strategy: Recording) -> None:
    strategy._on_step(cast(Any, SimpleNamespace()))


@pytest.mark.parametrize("bid", ["0.35", "0.38"], ids=["below-offset", "at-offset"])
def test_carried_offset_breach_retains_the_level_and_distances(tmp_path, monkeypatch, bid) -> None:
    strategy = carried_gap(tmp_path, monkeypatch, bid)
    slot = strategy.slots[h.AAA]
    assert slot.level == Price.from_str("2.00"), strategy.said
    assert slot.distance is not None and slot.distance.distance == Decimal("0.30")
    assert slot.limit_offset == Decimal("0.38")
    assert not strategy.sent, "a startup seed is never an observation"


@pytest.mark.parametrize("bid", ["0.35", "0.38"], ids=["below-offset", "at-offset"])
def test_carried_offset_breach_alerts_at_error_once(tmp_path, monkeypatch, bid) -> None:
    strategy = carried_gap(tmp_path, monkeypatch, bid)
    step(strategy)
    strategy.observe(bid, START + h.SECOND)
    strategy.observe(bid, START + h.SECOND)
    step(strategy)
    alerts = [(level, text) for level, text in strategy.logged if "ALERT:" in text]
    assert len(alerts) == 1, strategy.logged
    severity, text = alerts[0]
    assert severity == "ERROR"
    assert str(h.AAA) in text and "breached through its offset" in text
    assert bid in text and "0.38" in text
    assert not strategy.sent, "the alert does not spend the observation guard"
    strategy.observe(bid, START + 2 * h.SECOND)
    step(strategy)
    assert [(level, text) for level, text in strategy.logged if "ALERT:" in text] == alerts


@pytest.mark.parametrize(
    ("bid", "limit", "floor"),
    [
        ("0.35", "0.3499", "0.3430"),
        ("0.38", "0.3799", "0.3724"),
        ("0.0001", "0.0001", "0.0001"),
    ],
    ids=["below-offset", "at-offset", "lowest-venue-price"],
)
def test_carried_offset_breach_sells_on_step_only_after_two_new_observations(
    tmp_path, monkeypatch, bid, limit, floor
) -> None:
    strategy = carried_gap(tmp_path, monkeypatch, bid)
    step(strategy)
    strategy.observe(bid, START)
    strategy.observe(bid, START + h.SECOND)
    strategy.observe(bid, START + h.SECOND)
    strategy.observe(bid, START)
    step(strategy)
    assert not strategy.sent, "seed, duplicate, older quote and timer cannot confirm the breach"
    strategy.observe(bid, START + 2 * h.SECOND)
    assert len(strategy.sent) == 1, strategy.said
    (order,) = strategy.sent
    assert order.order_type == OrderType.LIMIT
    assert order.side == OrderSide.SELL and order.is_reduce_only
    assert order.quantity == Quantity.from_int(10)
    assert order.time_in_force == TimeInForce.DAY
    assert order.price.as_decimal() == Decimal(limit)
    assert order.price.as_decimal() > 0
    assert order.price.as_decimal() % Decimal("0.0001") == 0
    assert strategy.params == [{"extended_hours": True}]
    assert str(order.client_order_id).startswith("canon-protect-")
    slot = strategy.slots[h.AAA]
    assert slot.level == Price.from_str("2.00")
    assert slot.placed and slot.sale is not None
    assert slot.sale.first_price == Decimal(limit) and slot.sale.floor == Decimal(floor)
    assert slot.sale.trigger_ns == START + 2 * h.SECOND


def test_carried_offset_breach_uses_the_existing_chase_floor_and_cancel_ack(
    tmp_path, monkeypatch
) -> None:
    strategy = carried_gap(tmp_path, monkeypatch, "0.35")
    strategy.observe("0.35", START + h.SECOND)
    strategy.observe("0.35", START + 2 * h.SECOND)
    assert len(strategy.sent) == 1, strategy.said
    (first,) = strategy.sent
    accepted(first, START + 2 * h.SECOND)
    strategy.observe("0.30", START + 6 * h.SECOND)
    assert not strategy.canceled, "the sale is paced by the existing five-second chase"
    strategy.observe("0.30", START + 7 * h.SECOND)
    assert strategy.canceled == [first.client_order_id]
    step(strategy)
    assert len(strategy.sent) == 1, "replacement waits for the terminal cancel"
    terminal(first, START + 8 * h.SECOND)
    strategy.observe("0.30", START + 8 * h.SECOND)
    assert len(strategy.sent) == 2
    assert strategy.sent[-1].price.as_decimal() == Decimal("0.3430")
    assert strategy.sent[-1].quantity == Quantity.from_int(10)
    assert len([text for _, text in strategy.logged if "ALERT:" in text]) == 1


def test_after_our_trailing_order_expires_one_observation_through_the_offset_never_sells(
    tmp_path, monkeypatch
) -> None:
    """F15's review, 1: the guard is skipped once a sell has been placed, so after our trailing
    order expired one quote through the offset started a sale (ruling 6: no election on one)."""
    strategy = carried_gap(tmp_path, monkeypatch, "2.50")
    step(strategy)
    strategy.observe("2.50", START + h.SECOND)
    strategy.observe("2.50", START + 2 * h.SECOND)
    (trailing,) = strategy.sent
    assert trailing.order_type == OrderType.TRAILING_STOP_LIMIT
    accepted(trailing, START + 2 * h.SECOND)
    terminal(trailing, START + 3 * h.SECOND, expired=True)
    strategy.observe("0.35", START + 4 * h.SECOND)
    step(strategy)
    strategy.observe("0.35", START + 4 * h.SECOND)
    assert len(strategy.sent) == 1, ("a sale on one observation", strategy.said)
    strategy.observe("0.35", START + 5 * h.SECOND)
    assert len(strategy.sent) == 2, strategy.said
    sale = strategy.sent[1]
    assert sale.order_type == OrderType.LIMIT and sale.price.as_decimal() == Decimal("0.3499")


def test_a_holding_armed_this_run_that_gaps_through_its_offset_sells(monkeypatch) -> None:
    """F15's review, 4: the sale was gated on a carried level, so a long armed this run from a
    supplied level froze at every step once its bid fell through the planned offset."""
    strategy = Recording()
    monkeypatch.setattr(strategy.cached, "instrument", lambda iid: h.equity(iid, precision=4))
    strategy.test_clock.set_time(START - h.SECOND)
    strategy.cached.latest = h.quote("10.00", START - h.SECOND)
    strategy.adopt_holding(h.AAA, Price.from_str("9.95"))
    slot = strategy.slots[h.AAA]
    assert slot.limit_offset is not None and slot.limit_offset > Decimal("0.14")
    for n in (1, 2):
        strategy.observe("0.14", START + n * h.SECOND)
    (order,) = strategy.sent
    assert order.order_type == OrderType.LIMIT and order.is_reduce_only
    assert order.price.as_decimal() == Decimal("0.1399")
    alerts = [text for level, text in strategy.logged if "ALERT:" in text]
    assert len(alerts) == 1 and "carried" not in alerts[0], alerts
    assert not any("frozen: no positive protective limit" in note for note in strategy.said)
