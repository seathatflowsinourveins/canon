"""Lead's 2026-11-14 distance ruling, against both real planning callers.

Only synthetic holdings and transports; expected distances are literal case data.
Source: Alpaca orders-at-alpaca.md, sub-penny increments and trailing stops;
Canon price_step's existing stock-price tiers and venue_smallest's retained floor.
"""

import re
import socket
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import price_step
from canon_trading import protect
from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401
from tests.test_owner_protector import equity
from tests.test_owner_protector import isolated_level_books  # noqa: F401
from tests.test_protect import Data
from tests.test_protect import Paper
from tests.test_protect import _position

CASES = [
    pytest.param("0.9998", "0.9999", "1.00", "0.025", "0.0250", "0.0750", id="below-dollar"),
    pytest.param("0.9999", "1.00", "1.00", "0.025", "0.03", "0.08", id="at-dollar"),
    pytest.param("1.00", "1.01", "1.01", "0.025", "0.03", "0.08", id="above-dollar-reader"),
    pytest.param("1.00", "1.01", "1.00", "0.0234375", "0.02", "0.07", id="nearest-down"),
    pytest.param("1.00", "1.01", "1.00", "0.02734375", "0.03", "0.08", id="nearest-up"),
    pytest.param("0.9883", "1.00", "1.00", "0.001953125", "0.03", "0.03", id="spread-floor"),
    pytest.param("0.9999", "1.00", "1.00", "0.0009765625", "0.02", "0.02", id="venue-floor"),
    pytest.param(
        "4999.99", "5000.00", "5000.00", "1.00", "5.01", "5.01", id="first-above-entry-bound"
    ),
    pytest.param("5999.99", "6000.00", "6000.00", "1.00", "6.01", "6.01", id="high-price-reader"),
    pytest.param("5999.99", "6000.00", "6000.00", "4.00", "6.01", "12.00", id="high-price-atr"),
]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("distance regression must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(protect, "monotonic", lambda: 0.0)


def test_imports_this_repositorys_source():
    root = Path(__file__).resolve().parents[1]
    assert Path(protect.__file__).resolve() == root / "src/canon_trading/protect.py"
    assert Path(price_step.__file__).resolve() == root / "src/canon_trading/price_step.py"


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
@pytest.mark.parametrize("plan_only", [False, True], ids=["send", "plan-only"])
@pytest.mark.parametrize("bid,ask,mid,width,own1,own3", CASES)
def test_stop_sender_keeps_a_stop_or_plan(bid, ask, mid, width, own1, own3, row, plan_only):
    expected = D(own1 if row == "OWN1" else own3)
    paper = Paper(positions=[_position("AAA", "10")])
    report = protect.protect_longs(
        cast(Any, paper), Data({"AAA": (bid, ask, mid, width)}), row=row, plan_only=plan_only
    )
    lines = list(report.lines())
    print(
        f"sender {row} ask={ask} ATR={width} plan_only={plan_only} expected={expected} exit={report.exit_code}"
    )
    print("\n".join(lines))
    assert report.exit_code == 0, "\n".join(lines)
    row_lines = [line for line in lines if f"(row {row}," in line]
    assert len(row_lines) == 1, lines
    match = re.search(r"distance ([0-9.]+)", row_lines[0])
    assert match is not None
    used = D(match.group(1))
    assert used == expected
    assert used >= 2 * (D(ask) - D(bid))
    assert used >= protect.venue_smallest(D(ask))
    if plan_only:
        assert paper.submitted == []
    else:
        assert len(paper.submitted) == 1
        request = paper.submitted[0]
        assert D(str(request.trail_price)) == expected
        assert request.qty == 10
        assert request.side.value == "sell"
        assert request.time_in_force.value == "gtc"
        assert request.position_intent.value == "sell_to_close"


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
@pytest.mark.parametrize("bid,ask,mid,width,own1,own3", CASES)
def test_protector_plans_existing_long(bid, ask, mid, width, own1, own3, row, monkeypatch):
    expected = D(own1 if row == "OWN1" else own3)
    strategy = Harness(row=row)
    monkeypatch.setattr(strategy.cached, "instrument", lambda iid: equity(iid, precision=4))
    strategy.cached.latest = QuoteTick(
        AAA,
        Price.from_str(f"{D(bid):.4f}"),
        Price.from_str(f"{D(ask):.4f}"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        T0,
        T0,
    )
    half = D(width) / 2
    bar_type = BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL")
    strategy.cached.history = [
        Bar(
            bar_type,
            Price.from_str(mid),
            Price.from_str(str(D(mid) + half)),
            Price.from_str(str(D(mid) - half)),
            Price.from_str(mid),
            Quantity.from_int(100),
            T0 - i * 60 * SECOND,
            T0 - (i - 1) * 60 * SECOND,
        )
        for i in range(1, 16)
    ]
    strategy.on_start()
    print(f"protector {row} ask={ask} ATR={width} expected={expected}")
    print("\n".join(strategy.said))
    assert AAA in strategy.slots
    slot = strategy.slots[AAA]
    assert slot.distance is not None, "\n".join(strategy.said)
    assert slot.level is not None
    assert slot.distance.distance == expected
    floor = slot.distance.floor
    assert floor is not None
    assert floor == 2 * (D(ask) - D(bid))
    assert slot.distance.distance >= floor
    assert slot.distance.distance >= protect.venue_smallest(D(ask))
    if slot.distance.planned != expected and expected != protect.venue_smallest(D(ask)):
        source = slot.distance.source()
        assert "rounded to the price step" in source or "raised to the spread floor" in source
        assert "bound by Alpaca's smallest trail" not in source
    assert slot.level.as_decimal() >= D(bid) - expected
    assert any(f"armed distance {slot.distance.distance};" in line for line in strategy.said)
    assert not strategy.sent, "the startup seed plans; it is not an observation"


@pytest.mark.parametrize(
    "bid,ask,width,label",
    [
        (
            "1.00",
            "1.01",
            "0.0234375",
            "rounded to the price step 0.01: distance 0.02, from the Mover's",
        ),
        ("0.9883", "1.00", "0.001953125", "raised to the spread floor up to the price step 0.01"),
        ("0.9999", "1.00", "0.0009765625", "bound by Alpaca's smallest trail"),
    ],
    ids=["nearest-down", "spread-floor", "venue-floor"],
)
def test_the_report_names_the_rule_that_set_the_distance(bid, ask, width, label):
    paper = Paper(positions=[_position("AAA", "10")])
    report = protect.protect_longs(
        cast(Any, paper), Data({"AAA": (bid, ask, ask, width)}), row="OWN1", plan_only=True
    )
    (line,) = [x for x in report.lines() if x.startswith("would send AAA under OWN1:")]
    assert label in line, line


def test_entry_bound_stays_an_entry_rule():
    with pytest.raises(ValueError, match="above the top tier"):
        price_step.entry_price(D(6000), 0)
    assert price_step.up_on_step(D(6000)) == D(6000)
