"""Offline checks of the 2026-11-13 exit-monitor properties, unit one.

Contract: alpaca-py c42add7c0a70 models/requests and the monitor's pure assessment,
one-pass and local-file seams. All instruments, clients and clocks here are synthetic;
every write goes below pytest's tmp_path, never to a paper state directory.
"""

from __future__ import annotations

import importlib
import json
import time
import tomllib
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 13, 10, tzinfo=NY)
PROTECT = "canon-protect-20261113T140000Z-1-0123abcd"
NODE = "O-20261113-140000-001-001-1"
VENUE_ID = "00000000-0000-4000-8000-000000000001"


def position(qty: str = "100", *, symbol: str = "SYN", side: str = "long", gain="1"):
    return Position.model_validate(
        {
            "asset_id": VENUE_ID,
            "symbol": symbol,
            "exchange": "ARCA",
            "asset_class": "us_equity",
            "avg_entry_price": "100",
            "qty": qty,
            "side": side,
            "cost_basis": "100",
            "unrealized_pl": gain,
        }
    )


def order(**changes):
    payload = {
        "id": VENUE_ID,
        "client_order_id": PROTECT,
        "created_at": NOW,
        "updated_at": NOW,
        "submitted_at": NOW,
        "symbol": "SYN",
        "asset_id": VENUE_ID,
        "asset_class": "us_equity",
        "qty": "100",
        "filled_qty": "0",
        "order_class": "simple",
        "type": "trailing_stop",
        "order_type": "trailing_stop",
        "side": "sell",
        "time_in_force": "gtc",
        "status": "new",
        "extended_hours": False,
    }
    payload.update(changes)
    return Order.model_validate(payload)


def calendar():
    return [
        Calendar(
            date=f"2026-11-{day:02d}",
            open="09:30",
            close="16:00",
            session_open="0400",
            session_close="2000",
        )
        for day in (4, 5, 6, 9, 10, 11, 12, 13, 16)
    ]


class Paper:
    def __init__(self, *, positions=None, orders=None, now=NOW):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.now = now
        self.calls = []
        self.fail: dict[str, BaseException] = {}
        self.days = calendar()

    def read(self, name, value):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]
        return value

    def get_clock(self):
        opened = datetime(2026, 11, 16, 9, 30, tzinfo=NY)
        if self.now.hour < 9 or (self.now.hour == 9 and self.now.minute < 30):
            opened = self.now.replace(hour=9, minute=30)
        close = self.now.replace(hour=16, minute=0)
        is_open = self.now.weekday() < 5 and self.now.replace(hour=9, minute=30) <= self.now < close
        return self.read(
            "clock", Clock(timestamp=self.now, is_open=is_open, next_open=opened, next_close=close)
        )

    def get_all_positions(self):
        return self.read("positions", self.positions)

    def get_orders(self, request):
        status = str(request.status.value)
        self.calls.append(request)
        rows = [
            o
            for o in self.orders
            if (o.status.value in {"filled", "rejected", "canceled"}) == (status == "closed")
        ]
        return self.read(status, rows)

    def get_calendar(self, request):
        return self.read("calendar", self.days)


@pytest.fixture
def em(tmp_path, monkeypatch):
    from canon_alpaca import providers

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))
    # No test leaves the data-only switch set: setting it here records its prior state, which
    # teardown restores whatever a case or the module did to it after.
    monkeypatch.setenv(providers.DATA_ONLY_ENV, "")
    monkeypatch.delenv(providers.DATA_ONLY_ENV)
    # The process's request counter is shared with every window's record (a stand-in 429 here
    # must not read as Alpaca's answer there); each case counts into its own, as other tests do.
    monkeypatch.setattr(providers, "REQUESTS", providers.RequestCounter())
    return importlib.import_module("canon_trading.exit_monitor")


def run(em, tmp_path, paper, **kwargs):
    return em.run_pass(account="A", trading=paper, memory_path=tmp_path / "memory.json", **kwargs)


def seed(em, tmp_path):
    (tmp_path / "memory.json").write_text(json.dumps(em.empty_memory("A")))


def alerts(tmp_path):
    path = tmp_path / "canon/exit-monitor/A/alerts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assess(em, *, orders=(), reports=(), positions=None, **kwargs):
    return em.assess(
        account="A",
        positions=positions or [position()],
        orders=list(orders),
        reports=list(reports),
        memory={},
        now=NOW,
        **kwargs,
    )


@pytest.mark.parametrize(
    "status",
    [
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "held",
        "replaced",
        "filled",
        "done_for_day",
        "accepted",
    ],
)
def test_only_acknowledged_stops_count(em, status):
    assert assess(em, orders=[order(status=status)])[0]["state"] == "uncovered"


@pytest.mark.parametrize("status", ["new", "partially_filled"])
def test_decimal_remainders_and_excess(em, status):
    found = assess(
        em, positions=[position("0.3")], orders=[order(qty="0.4", filled_qty="0.1", status=status)]
    )[0]
    assert found["state"] == "covered"
    assert Decimal(found["cover"]) == Decimal("0.3")
    more = assess(em, orders=[order(qty="101.125")])[0]
    assert Decimal(more["excess"]) == Decimal("1.125")
    assert "excess" in more["reason"]


@pytest.mark.parametrize(
    "changes",
    [{"side": "buy"}, {"type": "limit"}, {"client_order_id": "foreign"}, {"client_order_id": ""}],
)
def test_other_orders_are_not_cover(em, changes):
    assert assess(em, orders=[order(**changes)])[0]["state"] == "uncovered"


def test_reports_are_fresh_explicit_and_have_session_eligibility(em):
    fresh = {"symbol": "SYN", "quantity": "100", "armed_at": NOW - timedelta(seconds=150)}
    assert assess(em, reports=[fresh])[0]["state"] == "covered"
    for stamp in (NOW - timedelta(seconds=151), NOW + timedelta(seconds=1)):
        assert assess(em, reports=[dict(fresh, armed_at=stamp)])[0]["state"] == "uncovered"
    assert assess(em, reports=[fresh], is_open=False)[0]["state"] == "dormant"
    all_hours = dict(fresh, sessions=["regular", "after-hours"])
    assert (
        assess(em, reports=[all_hours], is_open=False, part="after-hours")[0]["state"] == "covered"
    )


def level_report(**changes):
    """One holding of the proposed writer's level book, with its node's step metadata."""
    report = {
        "symbol": "SYN",
        "quantity": "50",
        "state": "armed",
        "level": "99",
        "armed_at": NOW,
        "last_step_at": NOW,
        "step_seconds": 20,
        "sessions": ["regular", "after-hours"],
    }
    report.update(changes)
    return report


@pytest.mark.parametrize("identified", [False, True], ids=["ambiguous", "same-stop"])
@pytest.mark.parametrize("part", ["regular", "after-hours"])
def test_node_report_does_not_add_the_same_venue_stop_twice(em, identified, part):
    report = level_report(**({"client_order_id": NODE} if identified else {}))
    found = assess(
        em,
        orders=[order(qty="50", client_order_id=NODE)],
        reports=[report],
        part=part,
        is_open=part == "regular",
    )[0]
    assert (found["state"], found["cover"], found["remainder"]) == ("uncovered", "50", "50")
    assert "report not added" in found["reason"]


def test_node_report_adds_only_a_distinct_engine_stop(em):
    found = assess(
        em,
        orders=[order(qty="50", client_order_id=NODE)],
        reports=[
            level_report(client_order_id=NODE),
            level_report(client_order_id="O-20261113-140000-001-001-2"),
        ],
    )[0]
    assert (found["state"], found["cover"], found["excess"]) == ("covered", "100", "0")


def test_same_stop_report_does_not_restore_its_filled_quantity(em):
    found = assess(
        em,
        orders=[order(qty="100", filled_qty="50", status="partially_filled", client_order_id=NODE)],
        reports=[level_report(quantity="100", client_order_id=NODE)],
    )[0]
    assert (found["state"], found["cover"], found["remainder"]) == ("uncovered", "50", "50")


def test_duplicate_node_report_is_counted_once(em):
    report = level_report(client_order_id=NODE)
    found = assess(em, reports=[report, dict(report)])[0]
    assert (found["state"], found["cover"], found["remainder"]) == ("uncovered", "50", "50")
    assert "report not added" in found["reason"]


def test_unidentified_report_is_not_added_to_an_identified_engine_stop(em):
    found = assess(em, reports=[level_report(), level_report(client_order_id=NODE)])[0]
    assert (found["state"], found["cover"]) == ("uncovered", "50")
    assert "report not added" in found["reason"]


def test_level_book_stop_identity_must_be_of_our_node_lineage(em):
    found = assess(em, reports=[level_report(quantity="100", client_order_id="foreign")])[0]
    assert found["state"] == "unknown"


def test_level_book_missing_state_cannot_imply_armed(em):
    report = level_report(quantity="100")
    del report["state"]
    assert assess(em, reports=[report])[0]["state"] == "unknown"


@pytest.mark.parametrize(
    ("step_seconds", "age", "expected"),
    [(20, 60, "covered"), (20, 61, "uncovered"), (100, 300, "covered"), (100, 301, "uncovered")],
)
def test_level_book_freshness_uses_three_node_steps_not_armed_time(em, step_seconds, age, expected):
    report = level_report(
        quantity="100",
        step_seconds=step_seconds,
        last_step_at=NOW - timedelta(seconds=age),
        armed_at=NOW - timedelta(seconds=600) if expected == "covered" else NOW,
    )
    found = assess(em, reports=[report])[0]
    assert found["state"] == expected
    if expected == "uncovered":
        assert "no protector running" in found["reason"]


@pytest.mark.parametrize("state", ["armed", "counting", "guarded", "waiting", "frozen"])
def test_level_book_only_armed_or_counting_is_cover(em, state):
    report = level_report(
        quantity="100",
        state=state,
        count=1,
        required_count=3,
        reason="quote awaited",
        result="denied",
    )
    found = assess(em, reports=[report])[0]
    assert found["state"] == ("covered" if state in ("armed", "counting") else "uncovered")
    assert found["cover"] == ("100" if state in ("armed", "counting") else "0")
    if state == "frozen":
        assert found["frozen_source"] == "node report"


@pytest.mark.parametrize("step_seconds", [0, -1, "NaN", None])
def test_level_book_requires_a_positive_node_step(em, step_seconds):
    report = level_report(quantity="100", step_seconds=step_seconds)
    assert assess(em, reports=[report])[0]["state"] == "unknown"


@pytest.mark.parametrize("age", [60, 61])
def test_run_pass_reads_the_level_book_and_says_when_no_protector_is_running(em, tmp_path, age):
    root = tmp_path / "canon/exit-monitor/A"
    root.mkdir(parents=True)
    holding = level_report(quantity="100", state="counting", count=1, required_count=3)
    del holding["last_step_at"], holding["step_seconds"]
    holding["armed_at"] = (NOW - timedelta(seconds=600)).isoformat()
    book = {
        "version": 1,
        "account": "A",
        "last_step_at": (NOW - timedelta(seconds=age)).isoformat(),
        "step_seconds": 20,
        "holdings": [holding],
    }
    (root / "reports.json").write_text(json.dumps(book))
    result = run(em, tmp_path, Paper(positions=[position()]))
    assert result["ok"]
    found = result["findings"][0]
    assert found["state"] == ("covered" if age == 60 else "uncovered")
    if age == 61:
        assert "no protector running" in found["reason"]
        assert "no node report" in result["summary"]


def test_pure_assessment_does_not_mutate_inputs(em):
    memory = em.empty_memory("A")
    kwargs = {
        "account": "A",
        "positions": [position()],
        "orders": [],
        "reports": [],
        "memory": memory,
        "now": NOW,
    }
    before = json.dumps(memory)
    assert em.assess(**kwargs) == em.assess(**kwargs)
    assert json.dumps(memory) == before


def test_flat_short_foreign_and_held_run(em, tmp_path):
    assert "no position" in run(em, tmp_path, Paper())["summary"]
    assert assess(em, positions=[position(side="short")])[0]["reason"].startswith("a short")
    assert (
        "foreign sell rests for 100"
        in assess(em, orders=[order(client_order_id="foreign")])[0]["reason"]
    )
    assert assess(em, run_holds=True)[0]["state"] == "unknown"


@pytest.mark.parametrize("corrupt", [False, True], ids=["missing", "unreadable"])
def test_missing_or_unreadable_memory_keeps_venue_fill_age_and_says_so_once(em, tmp_path, corrupt):
    if corrupt:
        (tmp_path / "memory.json").write_text("{not json")
    fill = order(
        side="buy", status="filled", filled_qty="100", filled_at=NOW - timedelta(seconds=100)
    )
    paper = Paper(positions=[position()], orders=[fill])
    first = run(em, tmp_path, paper)
    finding = first["findings"][0]
    assert (finding["age_source"], finding["age_seconds"]) == ("since fill", 100)
    message = (
        "memory unreadable" if corrupt else "no memory file"
    ) + ": ages from the venue's fills where shown, else from first seen"
    assert first["summary"].count(message) == 1
    assert "lost" not in first["summary"]
    assert first["alerts"] == []
    paper.now += timedelta(seconds=60)
    second = run(em, tmp_path, paper)
    finding = second["findings"][0]
    assert (finding["age_source"], finding["age_seconds"]) == ("since fill", 160)
    assert "memory" not in second["summary"]
    assert len(second["alerts"]) == 1
    assert paper.calls.count("closed") == 1
    record = next((tmp_path / "canon/exit-monitor/A/records").glob("*.jsonl"))
    assert record.read_text().count(message) == 1


def test_fill_age_with_valid_memory_and_history_only_on_new_growth_or_cover_loss(em, tmp_path):
    seed(em, tmp_path)
    fill = order(
        side="buy", status="filled", filled_qty="100", filled_at=NOW - timedelta(seconds=40)
    )
    paper = Paper(positions=[position()], orders=[fill])
    first = run(em, tmp_path, paper)["findings"][0]
    assert (first["age_source"], first["age_seconds"]) == ("since fill", 40)
    paper.now += timedelta(seconds=30)
    assert run(em, tmp_path, paper)["findings"][0]["age_seconds"] == 70
    assert paper.calls.count("closed") == 1
    request = next(c for c in paper.calls if getattr(c, "status", None) == "closed")
    assert request.after.astimezone(NY) >= NOW.replace(day=12, hour=20) - timedelta(microseconds=1)


def test_growth_does_not_reset_age_and_cover_loss_starts_at_zero(em, tmp_path):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    paper.now += timedelta(seconds=60)
    paper.positions = [position("150")]
    assert run(em, tmp_path, paper)["findings"][0]["age_seconds"] == 60
    paper.orders = [order(qty="150")]
    healed = run(em, tmp_path, paper)
    assert healed["cleared"] == ["SYN"]
    paper.orders = []
    paper.now += timedelta(seconds=60)
    found = run(em, tmp_path, paper)["findings"][0]
    assert (found["age_source"], found["age_seconds"]) == ("since cover lost", 0)


def test_alert_age_repeat_dormant_and_cleared(em, tmp_path):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    paper.now += timedelta(seconds=119)
    run(em, tmp_path, paper)
    assert alerts(tmp_path) == []
    paper.now += timedelta(seconds=1)
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 1
    paper.now += timedelta(seconds=899)
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 1
    paper.now += timedelta(seconds=1)
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 2
    paper.orders = [order()]
    paper.now = NOW.replace(hour=17)
    assert run(em, tmp_path, paper)["findings"][0]["state"] == "dormant"
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 3
    paper.positions = []
    assert run(em, tmp_path, paper)["cleared"] == ["SYN"]
    assert len(alerts(tmp_path)) == 3


@pytest.mark.parametrize("read", ["clock", "positions", "open", "calendar", "closed"])
def test_failure_preserves_old_positions_and_blind_alert_is_once(em, tmp_path, read):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    paper.positions = [position("200", symbol="ALT")]
    paper.fail[read] = TimeoutError("not safe to print: account-123 and order-456")
    for _ in range(4):
        result = run(em, tmp_path, paper)
        assert not result["ok"]
        assert all(f["state"] == "unknown" for f in result["findings"])
        assert "SYN" in {f["symbol"] for f in result["findings"]}
    assert len([a for a in alerts(tmp_path) if "blind" in a["flag"]]) == 1
    assert "account-123" not in json.dumps(result)


def test_rejected_sell_alerts_at_once_and_repeats_without_refetch(em, tmp_path):
    rejected = order(status="rejected", failed_at=NOW - timedelta(seconds=2))
    paper = Paper(positions=[position()], orders=[rejected])
    result = run(em, tmp_path, paper)
    found = result["findings"][0]
    assert found["state"] == "uncovered"
    assert "frozen by the protector: its sell was rejected" in found["reason"]
    assert found["frozen_source"] == "venue"
    assert len(alerts(tmp_path)) == 1
    paper.now += timedelta(seconds=899)
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 1
    paper.now += timedelta(seconds=1)
    run(em, tmp_path, paper)
    assert len(alerts(tmp_path)) == 2
    assert paper.calls.count("closed") == 1


def test_latest_sell_and_whole_working_cover_control_frozen_state(em, tmp_path):
    rejected = order(status="rejected", failed_at=NOW - timedelta(seconds=2))
    later = order(id="00000000-0000-4000-8000-000000000002", status="canceled", canceled_at=NOW)
    paper = Paper(positions=[position()], orders=[rejected, later])
    assert "frozen" not in run(em, tmp_path, paper)["findings"][0]["reason"]
    paper.orders = [rejected, order()]
    assert run(em, tmp_path, paper)["findings"][0]["state"] == "covered"


def test_frozen_node_report_and_locally_denied_limit(em, tmp_path):
    paper = Paper(positions=[position()])
    first = run(em, tmp_path, paper)
    assert (
        "no node report: a locally denied sell is alerted at the alert age, not at once"
        in first["summary"]
    )
    assert alerts(tmp_path) == []
    frozen = {
        "symbol": "SYN",
        "quantity": "100",
        "armed_at": NOW,
        "state": "frozen",
        "result": "denied",
    }
    found = run(em, tmp_path, paper, reports=[frozen])["findings"][0]
    assert found["frozen_source"] == "node report"
    assert "denied" in found["reason"]
    assert len(alerts(tmp_path)) == 1


def test_boundary_gain_uses_unrealized_result_and_deduplicates(em, tmp_path):
    paper = Paper(positions=[position()], orders=[order()], now=NOW.replace(hour=15, minute=44))
    tape = lambda *args: (Decimal(1000), 5)
    run(em, tmp_path, paper, tape_reader=tape)
    assert alerts(tmp_path) == []
    paper.now += timedelta(minutes=1)
    result = run(em, tmp_path, paper, tape_reader=tape)
    assert len(alerts(tmp_path)) == 1
    assert "exit cannot act next" in alerts(tmp_path)[0]["flag"]
    assert result["status"]["boundary_warnings"]
    paper.now += timedelta(minutes=1)
    run(em, tmp_path, paper, tape_reader=tape)
    assert len(alerts(tmp_path)) == 1
    paper.now = NOW.replace(hour=16)
    assert run(em, tmp_path, paper, tape_reader=tape)["status"]["boundary_warnings"] == []


def test_p20_p21_boundary_notices_are_separate_and_each_sent_once(em, tmp_path):
    paper = Paper(
        positions=[position("1000"), position("1000", symbol="ALT", gain="-1")],
        orders=[order(qty="1000"), order(qty="1000", symbol="ALT")],
        now=NOW.replace(hour=15, minute=45),
    )
    tape = lambda *args: (Decimal(400), 5)
    first = run(em, tmp_path, paper, tape_reader=tape)
    syn = [event for event in first["alerts"] if "SYN" in event["flag"]]
    assert len(syn) == 2
    assert len(first["alerts"]) == 3
    p20 = next(event for event in syn if "exit cannot act next" in event["flag"])
    p21 = next(event for event in syn if "ratio 2.5" in event["flag"])
    assert "tape" not in p20["flag"]
    assert "exit cannot act next" not in p21["flag"]
    assert ":P20:" in p20["key"]
    assert ":P21:" in p21["key"]
    assert len({event["key"] for event in first["alerts"]}) == 3
    assert alerts(tmp_path) == first["alerts"]
    warnings = first["status"]["boundary_warnings"]
    assert len(warnings) == 3
    assert {(entry["symbol"], entry["kind"]) for entry in warnings} == {
        ("SYN", "P20"),
        ("SYN", "P21"),
        ("ALT", "P21"),
    }
    root = tmp_path / "canon/exit-monitor/A"
    assert json.loads((root / "status.json").read_text())["boundary_warnings"] == warnings
    record = json.loads((root / "records/2026-11-13.jsonl").read_text())
    assert record["alerts"] == first["alerts"]
    assert record["status"]["boundary_warnings"] == warnings

    paper.now += timedelta(minutes=1)
    second = run(em, tmp_path, paper, tape_reader=tape)
    assert second["alerts"] == []
    assert alerts(tmp_path) == first["alerts"]
    assert len(second["status"]["boundary_warnings"]) == 3
    assert all(entry in second["status"]["boundary_warnings"] for entry in warnings)

    # A later P20 fact has its own key; it neither repeats nor is silenced by P21.
    paper.positions = [position("1000", gain="2"), position("1000", symbol="ALT")]
    paper.now += timedelta(minutes=1)
    third = run(em, tmp_path, paper, tape_reader=tape)
    assert len(third["alerts"]) == 1
    assert "ALT" in third["alerts"][0]["flag"]
    assert "exit cannot act next" in third["alerts"][0]["flag"]
    assert "tape" not in third["alerts"][0]["flag"]
    assert len(third["status"]["boundary_warnings"]) == 4
    assert len({event["key"] for event in alerts(tmp_path)}) == 4
    paper.now += timedelta(minutes=1)
    assert run(em, tmp_path, paper, tape_reader=tape)["alerts"] == []
    assert len(alerts(tmp_path)) == 4


@pytest.mark.parametrize("gain", ["0", "-1"])
def test_nonpositive_gain_is_not_boundary_warning(em, tmp_path, gain):
    paper = Paper(
        positions=[position(gain=gain)], orders=[order()], now=NOW.replace(hour=15, minute=45)
    )
    run(em, tmp_path, paper, tape_reader=lambda *args: (Decimal(1000), 5))
    assert alerts(tmp_path) == []


def test_closed_day_has_no_boundary_and_early_close_comes_from_calendar(em, tmp_path):
    paper = Paper(
        positions=[position()], orders=[order()], now=NOW.replace(day=14, hour=15, minute=45)
    )
    result = run(em, tmp_path, paper)
    assert result["status"]["boundary_warnings"] == []
    early = Calendar(
        date="2026-11-13", open="09:30", close="13:00", session_open="0400", session_close="2000"
    )
    bounds = em.boundaries([early])
    assert any(b["at"] == NOW.replace(hour=13) for b in bounds)
    assert not any(b["at"] == NOW.replace(hour=16) for b in bounds)
    assert {b["at"].hour for b in bounds} >= {4, 9, 13, 20}


@pytest.mark.parametrize(
    "median,count,expected",
    [
        ("400", 5, "2.5"),
        ("400", 3, "rests on 3 sessions"),
        (None, 0, "tape not read"),
        ("0", 5, "zero"),
    ],
)
def test_tape_ratio_insufficient_and_unread_are_named_once(em, tmp_path, median, count, expected):
    paper = Paper(
        positions=[position("1000", gain="-1")],
        orders=[order(qty="1000")],
        now=NOW.replace(hour=15, minute=45),
    )
    calls = []

    def tape(symbol, start, end):
        calls.append((symbol, start, end))
        return (Decimal(median) if median is not None else None), count

    run(em, tmp_path, paper, tape_reader=tape)
    paper.now += timedelta(minutes=1)
    run(em, tmp_path, paper, tape_reader=tape)
    assert len(calls) == 1
    assert (calls[0][2] - calls[0][1]).total_seconds() == 1800
    assert len(alerts(tmp_path)) == 1
    assert expected in alerts(tmp_path)[0]["flag"]


def test_tape_is_not_read_outside_boundary_window(em, tmp_path):
    def never(*args):
        pytest.fail("tape read outside the boundary window")

    run(em, tmp_path, Paper(positions=[position()], orders=[order()]), tape_reader=never)


def test_record_counts_and_trade_date_rollover(em, tmp_path):
    paper = Paper(positions=[position()])
    first = run(em, tmp_path, paper)
    paper.now += timedelta(seconds=60)
    result = run(em, tmp_path, paper)
    assert result["totals"]["passes"] == 2
    assert result["totals"]["episodes"] == 1
    assert result["totals"]["minutes_uncovered"] == 1
    assert result["totals"]["longest_uncovered_age"] == 60
    assert first["status"]["healthy"]
    paper.now += timedelta(seconds=60)
    result = run(em, tmp_path, paper)
    assert not result["status"]["healthy"]
    assert not em.status_health(result["status"], paper.now + timedelta(seconds=181))["healthy"]
    records = list((tmp_path / "canon/exit-monitor/A/records").glob("*.jsonl"))
    assert len(records[0].read_text().splitlines()) == 3
    paper.now = NOW.replace(hour=20)
    result = run(em, tmp_path, paper)
    assert result["trade_date"] == "2026-11-14"
    assert result["totals"]["passes"] == 1


def test_alert_schema_matches_watchdog_and_record_contains_alert(em, tmp_path):
    paper = Paper(positions=[position()], orders=[order(status="rejected", failed_at=NOW)])
    result = run(em, tmp_path, paper)
    event = alerts(tmp_path)[0]
    assert set(event) >= {"at", "key", "sid", "lane", "flag", "state", "quiet_min", "fail"}
    assert result["alerts"] == [event]
    assert VENUE_ID not in json.dumps(result)
    assert PROTECT not in json.dumps(result)


def test_accounts_are_isolated_and_c_is_refused_before_factory(em, tmp_path):
    calls = []

    def factory(account):
        calls.append(account)
        return None if account == "B" else Paper(positions=[position()], orders=[order()])

    assert "no pair" in em.monitor_task("B", trading_for=factory)["summary"]
    assert em.monitor_task("A", trading_for=factory)["findings"][0]["state"] == "covered"
    assert not em.monitor_task("C", trading_for=factory, accounts=("A", "B", "C"))["ok"]
    assert calls == ["B", "A"]
    assert (tmp_path / "canon/exit-monitor/A/status.json").exists()
    assert (tmp_path / "canon/exit-monitor/B/status.json").exists()


def test_deadline_ends_read_and_records_failure(em, tmp_path, monkeypatch):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    monkeypatch.setattr(em, "PASS_SECONDS", 0.02)

    def hung():
        time.sleep(0.1)
        pytest.fail("deadline did not interrupt the read")

    paper.get_all_positions = hung
    result = run(em, tmp_path, paper)
    assert not result["ok"]
    assert result["findings"][0]["state"] == "unknown"
    assert "deadline" in result["summary"]


def test_overlap_is_refused_without_account_or_stream_lock(em, tmp_path):
    paper = Paper()
    with em.pass_slot(tmp_path / "memory.json"):
        result = run(em, tmp_path, paper)
    assert not result["ok"]
    assert "already running" in result["summary"]
    assert paper.calls == []
    assert not list(tmp_path.rglob("*.lock"))


def test_task_entry_is_one_account_get_only_and_reads_c_only_behind_the_word(em):
    root = Path(__file__).parents[1]
    task = tomllib.loads((root / "mise.toml").read_text())["tasks"]["paper:exit-monitor"]
    assert 'choices "tests" "windows" "protect"' in task["usage"]
    assert 'flag "--lead-go <lead_go>"' in task["usage"]
    run_body = task["run"]
    go_check = run_body.index('[ "${usage_lead_go:-}" = "exit-monitor protect" ]')
    assert go_check < run_body.index("canon-alpaca-paper-protect") < run_body.index("security")
    assert "CANON_ALPACA_DATA_ONLY=1" in task["run"]
    assert "canon_trading.exit_monitor" in task["run"]
    assert "lockf" not in task["run"]


def test_read_tape_uses_five_sessions_sip_and_named_pace(em, monkeypatch):
    asked = []
    sleeps = []
    monkeypatch.setattr(em.time, "sleep", sleeps.append)

    class Data:
        def get(self, path, data):
            asked.append((path, data))
            return {
                "bars": {"SYN": [{"t": data["start"], "v": str(len(asked) * 100)}]},
                "next_page_token": None,
            }

    budget = em.ReadBudget()
    result = em.read_tape(
        Data(),
        calendar(),
        "SYN",
        NOW.replace(hour=15, minute=45),
        NOW.replace(hour=16, minute=15),
        budget,
    )
    assert result == (Decimal(300), 5)
    assert len(asked) == budget.counts["tape"] == 5
    assert all(
        path == "/stocks/bars" and params["feed"] == "sip" and params["timeframe"] == "1Min"
        for path, params in asked
    )
    assert sleeps and all(s >= em.READ_PACE_SECONDS for s in sleeps)


def test_truncated_pages_and_budget_exhaustion_are_never_silently_complete(
    em, tmp_path, monkeypatch
):
    monkeypatch.setattr(em, "ORDERS_PAGE_LIMIT", 1)
    paper = Paper(positions=[position()], orders=[order()])
    result = run(em, tmp_path, paper)
    assert not result["ok"] and result["findings"][0]["state"] == "unknown"
    budget = em.ReadBudget()
    for _ in range(em.BASE_GET_LIMIT):
        budget.call("base", lambda: None)
    with pytest.raises(em.ReadFailed):
        budget.call("base", lambda: pytest.fail("over-budget call reached transport"))


def test_real_transport_requires_get_only_paper_client(em, tmp_path, monkeypatch):
    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import DATA_ONLY_ENV

    monkeypatch.delenv(DATA_ONLY_ENV, raising=False)
    client = TradingClient("made-up-key", "made-up-secret", paper=True)

    def never(*args, **kwargs):
        pytest.fail("unguarded client reached transport")

    monkeypatch.setattr(client._session, "request", never)
    result = run(em, tmp_path, client)
    assert not result["ok"]
    assert "GET" in result["summary"]


def test_finite_quantities_and_report_shapes_fail_closed(em):
    for qty in ("NaN", "Infinity", "-1"):
        found = assess(em, orders=[order(qty=qty)])[0]
        assert found["state"] == "unknown"
    assert (
        assess(em, reports=[{"symbol": "SYN", "quantity": "NaN", "armed_at": NOW}])[0]["state"]
        == "unknown"
    )


def test_bad_position_keeps_one_unknown_finding(em, tmp_path):
    result = run(em, tmp_path, Paper(positions=[position("NaN")]))
    assert not result["ok"]
    assert len(result["findings"]) == 1
    assert result["findings"][0]["symbol"] == "SYN"
    assert result["findings"][0]["state"] == "unknown"


@pytest.mark.parametrize("missing", ["blind_alerted", "failures", "states", "boundaries"])
def test_incomplete_memory_is_unreadable_not_a_crash(em, tmp_path, missing):
    memory = em.empty_memory("A")
    del memory[missing]
    (tmp_path / "memory.json").write_text(json.dumps(memory))
    result = run(em, tmp_path, Paper(positions=[position()]))
    assert result["ok"]
    assert "memory unreadable: ages from the venue's fills where shown" in result["summary"]


def test_missing_memory_without_a_fill_is_said_only_on_the_pass_that_reads_it(em, tmp_path):
    paper = Paper(positions=[position()])
    first = run(em, tmp_path, paper)
    assert "no memory file" in first["summary"]
    assert "lost" not in first["summary"]
    assert first["findings"][0]["age_source"] == "since first seen"
    paper.now += timedelta(seconds=60)
    result = run(em, tmp_path, paper)
    assert "memory" not in result["summary"]
    assert result["findings"][0]["age_seconds"] == 60


def test_recovered_summary_is_current_and_record_clears_blind_episode(em, tmp_path):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    paper.fail["positions"] = TimeoutError()
    run(em, tmp_path, paper)
    run(em, tmp_path, paper)
    run(em, tmp_path, paper)
    paper.fail.clear()
    result = run(em, tmp_path, paper)
    assert "blind" not in result["summary"]
    assert result["blind_cleared"]


def test_guarded_sdk_transport_is_get_only_and_429_never_retries(em, tmp_path, monkeypatch):
    import requests
    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import DATA_ONLY_ENV
    from canon_alpaca.providers import with_http_timeout
    from requests.adapters import HTTPAdapter

    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    client = with_http_timeout(TradingClient("made-up-key", "made-up-secret", paper=True))
    responses = {
        "/v2/clock": Paper().get_clock().model_dump(mode="json"),
        "/v2/positions": [position().model_dump(mode="json")],
        "/v2/orders": [order().model_dump(mode="json")],
        "/v2/calendar": [],
    }
    methods = []
    limited = False

    def fake_send(self, request, **kwargs):
        methods.append(request.method)
        response = requests.Response()
        response.status_code = 429 if limited else 200
        path = request.path_url.split("?")[0]
        payload = [] if "status=closed" in request.path_url else responses[path]
        response._content = json.dumps({"message": "private-body"} if limited else payload).encode()
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    result = run(em, tmp_path, client)
    assert result["ok"]
    assert result["findings"][0]["state"] == "covered"
    assert methods == ["GET"] * 5
    limited = True
    methods.clear()
    result = run(em, tmp_path, client)
    assert not result["ok"]
    assert methods == ["GET"]
    assert "429" in result["summary"]
    assert "private-body" not in json.dumps(result)
    assert result["findings"][0]["state"] == "unknown"


def test_tape_429_stops_other_symbols_and_marks_all_unknown(em, tmp_path):
    from types import SimpleNamespace

    from alpaca.common.exceptions import APIError

    calls = []

    def tape(*args):
        calls.append(args)
        raise APIError(
            '{"message":"private-body"}', SimpleNamespace(response=SimpleNamespace(status_code=429))
        )

    paper = Paper(
        positions=[position(symbol="SYN"), position(symbol="ALT")],
        now=NOW.replace(hour=15, minute=45),
    )
    result = run(em, tmp_path, paper, tape_reader=tape)
    assert len(calls) == 1
    assert not result["ok"]
    assert {f["state"] for f in result["findings"]} == {"unknown"}
    assert "429" in result["summary"]
    notices = alerts(tmp_path)
    assert len(notices) == 4
    gain_notices = [notice for notice in notices if ":P20:" in notice["key"]]
    tape_notices = [notice for notice in notices if ":P21:" in notice["key"]]
    assert len(gain_notices) == len(tape_notices) == 2
    assert all("exit cannot act next" in notice["flag"] for notice in gain_notices)
    assert all("tape" not in notice["flag"] for notice in gain_notices)
    assert all("tape not read" in notice["flag"] for notice in tape_notices)
    assert all("exit cannot act next" not in notice["flag"] for notice in tape_notices)


def test_tape_does_not_compare_overnight_minutes_that_prior_session_lacks(em, monkeypatch):
    monkeypatch.setattr(em.time, "sleep", lambda seconds: None)
    requested = []

    class Data:
        def get(self, path, data):
            requested.append(data)
            return {"bars": {"SYN": [{"t": data["start"], "v": "100"}]}, "next_page_token": None}

    # A window straddling 20:00 has no following overnight on a Friday;
    # 2026-11-06 is a trading day but does not have these minutes.
    em.read_tape(
        Data(),
        calendar(),
        "SYN",
        NOW.replace(hour=19, minute=45),
        NOW.replace(hour=20, minute=15),
        em.ReadBudget(),
    )
    assert all("2026-11-07" not in params["start"] for params in requested)


def test_output_failure_is_named_and_retains_known_findings(em, tmp_path, monkeypatch):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)

    def fail(path, value):
        raise OSError("private path details")

    monkeypatch.setattr(em, "_append", fail)
    result = run(em, tmp_path, paper)
    assert not result["ok"]
    assert result["findings"][0]["state"] == "unknown"
    assert "output" in result["summary"]
    assert "private path details" not in result["summary"]


def test_history_fetch_failure_is_retried_next_pass_without_losing_fill_evidence(em, tmp_path):
    seed(em, tmp_path)
    fill = order(
        side="buy", status="filled", filled_qty="100", filled_at=NOW - timedelta(seconds=40)
    )
    paper = Paper(positions=[position()], orders=[fill])
    paper.fail["closed"] = TimeoutError()
    assert not run(em, tmp_path, paper)["ok"]
    paper.fail.clear()
    run(em, tmp_path, paper)
    assert paper.calls.count("closed") == 2


def test_rejection_submitted_exactly_at_trade_date_start_is_not_lost(em, tmp_path):
    start = NOW.replace(day=12, hour=20)
    rejected = order(status="rejected", submitted_at=start, failed_at=NOW)
    paper = Paper(positions=[position()], orders=[rejected])
    original = paper.get_orders

    def exclusive(request):
        return [
            row
            for row in original(request)
            if request.after is None or row.submitted_at > request.after
        ]

    paper.get_orders = exclusive
    result = run(em, tmp_path, paper)
    assert result["findings"][0]["frozen_source"] == "venue"
    assert len(alerts(tmp_path)) == 1


def test_deadline_also_covers_output_work(em, tmp_path, monkeypatch):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    monkeypatch.setattr(em, "PASS_SECONDS", 0.02)
    original = em._append

    def delayed(path, value):
        time.sleep(0.1)
        original(path, value)

    monkeypatch.setattr(em, "_append", delayed)
    result = run(em, tmp_path, paper)
    assert not result["ok"]
    assert result["findings"][0]["state"] == "unknown"
    assert "deadline" in result["summary"]


def test_latest_changed_sell_clears_old_venue_rejection_when_history_is_read(em, tmp_path):
    rejected = order(status="rejected", failed_at=NOW - timedelta(seconds=2))
    paper = Paper(positions=[position()], orders=[rejected])
    assert run(em, tmp_path, paper)["findings"][0]["frozen_source"] == "venue"
    paper.positions = [position("101")]
    paper.orders = [order(status="canceled", canceled_at=NOW)]
    assert run(em, tmp_path, paper)["findings"][0]["frozen_source"] == ""


def test_rejection_does_not_need_an_optional_fill_quantity(em, tmp_path):
    rejected = order(status="rejected", failed_at=NOW, filled_qty=None)
    result = run(em, tmp_path, Paper(positions=[position()], orders=[rejected]))
    assert result["findings"][0]["frozen_source"] == "venue"
    assert result["findings"][0]["state"] == "uncovered"
    assert len(alerts(tmp_path)) == 1


def test_tape_compares_whole_clock_minutes(em, tmp_path):
    asked = []

    def tape(symbol, start, end):
        asked.append((start, end))
        return Decimal(1000), 5

    paper = Paper(
        positions=[position()], orders=[order()], now=NOW.replace(hour=15, minute=45, second=30)
    )
    run(em, tmp_path, paper, tape_reader=tape)
    assert asked == [(NOW.replace(hour=15, minute=45), NOW.replace(hour=16, minute=15))]


def test_transport_uses_only_explicit_auth_and_keeps_sdk_redirect_refusal(
    em, tmp_path, monkeypatch
):
    import requests
    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import DATA_ONLY_ENV
    from canon_alpaca.providers import with_http_timeout
    from requests.adapters import HTTPAdapter

    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    client = with_http_timeout(TradingClient("made-up-key", "made-up-secret", paper=True))
    sent = []
    implicit_auth = []
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda url: implicit_auth.append(url))

    def redirect(self, request, **kwargs):
        sent.append(request.method)
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "https://paper-api.alpaca.markets/v2/clock"
        response._content = b"{}"
        response._content_consumed = True
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", redirect)
    result = run(em, tmp_path, client)
    assert not result["ok"]
    assert sent == ["GET"]
    assert implicit_auth == []


def test_tape_figure_is_kept_even_when_the_size_is_not_named(em, tmp_path):
    paper = Paper(
        positions=[position(gain="-1")], orders=[order()], now=NOW.replace(hour=15, minute=45)
    )
    run(em, tmp_path, paper, tape_reader=lambda *args: (Decimal(1000), 5))
    assert alerts(tmp_path) == []
    memory = json.loads((tmp_path / "memory.json").read_text())
    entry = next(iter(memory["boundaries"].values()))
    assert entry["tape"] == {"median": "1000", "sessions": 5}


@pytest.mark.parametrize("symbol,age", [("SYN", 151), ("ALT", 0)])
def test_stale_or_other_symbol_report_keeps_denied_limit_visible(em, tmp_path, symbol, age):
    report = {
        "symbol": symbol,
        "quantity": "100",
        "armed_at": NOW - timedelta(seconds=age),
    }
    result = run(em, tmp_path, Paper(positions=[position()]), reports=[report])
    assert result["findings"][0]["state"] == "uncovered"
    assert em.DENIED_LIMIT in result["summary"]


@pytest.mark.parametrize("kind", ["uncovered", "blind"])
def test_replayed_outbox_alert_keeps_its_watchdog_dedup_key(em, tmp_path, kind):
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    if kind == "blind":
        paper.fail["positions"] = TimeoutError()
        run(em, tmp_path, paper)
        run(em, tmp_path, paper)
    saved = (tmp_path / "memory.json").read_text()
    paper.now += timedelta(seconds=120)
    run(em, tmp_path, paper)
    # A process stopped after the outbox append, before its atomic memory write.
    (tmp_path / "memory.json").write_text(saved)
    paper.now += timedelta(seconds=60)
    run(em, tmp_path, paper)
    rows = alerts(tmp_path)
    assert len(rows) == 2
    assert rows[0]["key"] == rows[1]["key"]


def test_long_history_walk_does_not_treat_a_short_covering_buy_as_an_entry(em, tmp_path):
    seed(em, tmp_path)
    covering = order(
        symbol="ALT", side="buy", qty="5", filled_qty="5", status="filled", filled_at=NOW
    )
    paper = Paper(
        positions=[position(), position("-5", side="short", symbol="ALT")], orders=[covering]
    )
    result = run(em, tmp_path, paper)
    short = next(f for f in result["findings"] if f["symbol"] == "ALT")
    assert result["ok"]
    assert short["state"] == "uncovered"
    assert short["age_source"] == "since first seen"


# Review round (code review at high effort, 2026-11-13): each case below was red at the tip first.


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending_cancel"},
        {"status": "pending_new"},
        {"status": "accepted"},
        {"type": "limit", "order_type": "limit"},
    ],
    ids=["pending_cancel", "pending_new", "accepted", "limit"],
)
@pytest.mark.parametrize("identified", [True, False], ids=["same-stop", "ambiguous"])
def test_report_of_a_sell_the_venue_shows_but_does_not_count_adds_nothing(em, changes, identified):
    """P2 and R7: a sell of ours the venue shows, being canceled, not yet acknowledged or not a stop,
    is no cover, and a report that names it, or cannot be told apart from it, does not make it one."""
    report = level_report(quantity="100", **({"client_order_id": NODE} if identified else {}))
    found = assess(em, orders=[order(client_order_id=NODE, **changes)], reports=[report])[0]
    assert (found["state"], found["cover"], found["remainder"]) == ("uncovered", "0", "100")
    assert "report not added" in found["reason"]


@pytest.mark.parametrize("blip", [False, True], ids=["direct", "through-unknown"])
def test_a_dormant_notice_does_not_delay_the_uncovered_alert(em, tmp_path, blip):
    """P11: a dormant notice is told once; the uncovered alert of the same episode still comes at the
    alert age, not one repeat interval after the dormant notice."""
    paper = Paper(positions=[position()], orders=[order()], now=NOW.replace(hour=17))
    assert run(em, tmp_path, paper)["findings"][0]["state"] == "dormant"
    assert len(alerts(tmp_path)) == 1
    if blip:
        paper.now += timedelta(minutes=1)
        paper.fail["clock"] = TimeoutError("one failed read")
        assert run(em, tmp_path, paper)["findings"][0]["state"] == "unknown"
        del paper.fail["clock"]
    paper.orders = []
    paper.now = NOW.replace(hour=17, minute=5)
    found = run(em, tmp_path, paper)["findings"][0]
    assert found["state"] == "uncovered"
    assert found["age_seconds"] >= 120
    uncovered = [a for a in alerts(tmp_path) if a["state"] == "uncovered"]
    assert len(uncovered) == 1


@pytest.mark.parametrize("sessions", [[["regular"]], [{"part": "regular"}]], ids=["list", "dict"])
def test_malformed_report_ends_the_pass_unknown_and_written(em, tmp_path, sessions):
    """P5a: a report the pass cannot use is a failed read: every position unknown, the pass recorded
    and counted toward the blind telling, never a traceback that writes nothing."""
    paper = Paper(positions=[position()])
    result = run(em, tmp_path, paper, reports=[level_report(quantity="100", sessions=sessions)])
    assert not result["ok"]
    assert [f["state"] for f in result["findings"]] == ["unknown"]
    status = json.loads((tmp_path / "canon/exit-monitor/A/status.json").read_text())
    assert status["ok"] is False
    assert status["unknown"] == 1
    memory = json.loads((tmp_path / "memory.json").read_text())
    assert memory["failures"] == 1


def test_open_orders_429_is_named(em, tmp_path):
    """P17: a 429 on the open-orders read ends the pass by name, without Alpaca's body."""
    from types import SimpleNamespace

    from alpaca.common.exceptions import APIError

    paper = Paper(positions=[position()])
    paper.fail["open"] = APIError(
        '{"message":"private-body"}', SimpleNamespace(response=SimpleNamespace(status_code=429))
    )
    result = run(em, tmp_path, paper)
    assert not result["ok"]
    assert "429" in result["summary"]
    assert "open orders" in result["summary"]
    assert "private-body" not in json.dumps(result)


def test_boundary_notice_is_not_lost_when_a_later_holding_fails(em, tmp_path):
    """P20: a pass that fails part-way through the boundary walk tells nothing and remembers nothing,
    so the next pass inside the window tells each holding once."""
    tape = lambda *args: (Decimal(1000), 5)
    paper = Paper(
        positions=[position(symbol="SYN"), position(symbol="ALT", gain="NaN")],
        now=NOW.replace(hour=15, minute=45),
    )
    result = run(em, tmp_path, paper, tape_reader=tape)
    assert not result["ok"]
    assert alerts(tmp_path) == []
    paper.positions = [position(symbol="SYN"), position(symbol="ALT")]
    paper.now += timedelta(minutes=1)
    assert run(em, tmp_path, paper, tape_reader=tape)["ok"]
    told = [a for a in alerts(tmp_path) if "exit cannot act next" in a["flag"]]
    assert sorted(a["key"].rsplit(":", 1)[-1] for a in told) == ["ALT", "SYN"]


def test_a_torn_record_line_does_not_stop_later_passes(em, tmp_path):
    """P10 and P12: a record line cut short by a killed pass is skipped; later passes still write their
    status, their record line and their alerts."""
    paper = Paper(positions=[position()])
    run(em, tmp_path, paper)
    (record,) = (tmp_path / "canon/exit-monitor/A/records").glob("*.jsonl")
    with record.open("a") as stream:
        stream.write('{"account": "A", "at"')
    paper.now += timedelta(seconds=120)
    result = run(em, tmp_path, paper)
    assert result["ok"]
    last = json.loads(record.read_text().splitlines()[-1])
    assert last["at"] == result["at"]
    assert last["totals"]["passes"] == 2
    assert len(alerts(tmp_path)) == 1


# The data-only switch never leaks (the lead's order, 2026-11-13 20:34): it is set only at the monitor's
# own entry point, never at import and never by a pass; no test leaves it set.


def test_importing_the_monitor_leaves_the_environment_as_it_was(tmp_path):
    import os
    import subprocess
    import sys

    from canon_alpaca.providers import DATA_ONLY_ENV

    env = {k: v for k, v in os.environ.items() if k != DATA_ONLY_ENV}
    probe = (
        "import os, json; before = dict(os.environ); import canon_trading.exit_monitor; "
        "print(json.dumps(dict(os.environ) == before))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "true"


def test_a_pass_never_turns_the_process_data_only(em, tmp_path, monkeypatch):
    """A guarded client handed to a pass in a process that is not data-only is refused by name, and the
    pass leaves the switch unset: only the entry point turns a process data-only."""
    import os

    from alpaca.trading.client import TradingClient
    from canon_alpaca.providers import DATA_ONLY_ENV
    from canon_alpaca.providers import with_http_timeout

    monkeypatch.delenv(DATA_ONLY_ENV, raising=False)
    client = with_http_timeout(TradingClient("made-up-key", "made-up-secret", paper=True))

    def never(*args, **kwargs):
        pytest.fail("a client reached transport outside data-only mode")

    monkeypatch.setattr(client._session, "request", never)
    result = run(em, tmp_path, client)
    assert DATA_ONLY_ENV not in os.environ
    assert not result["ok"]
    assert "data-only" in result["summary"]


# Unit two (the lead's ruling, 2026-11-14): C is admitted only behind the lead's word, by name, read in
# the same step; A and B keep their default, and without the word C reads nothing.
C_GO = "exit-monitor protect"


def test_c_is_admitted_only_with_the_lead_go_named_in_that_step(em, tmp_path):
    calls = []

    def factory(account):
        calls.append(account)
        return Paper(positions=[position()], orders=[order()])

    assert em.LEAD_GO == C_GO
    assert "C" not in em.DEFAULT_ACCOUNTS
    assert not em.monitor_task("C", trading_for=factory)["ok"]
    for wrong in ("", "exit-monitor C", "protect OWN1", C_GO + " ", "Exit-monitor protect"):
        refused = em.monitor_task("C", trading_for=factory, lead_go=wrong)
        assert not refused["ok"] and "lead's named word" in refused["summary"]
    assert calls == []
    assert not (tmp_path / "canon/exit-monitor/C").exists()
    admitted = em.monitor_task("C", trading_for=factory, lead_go=C_GO)
    assert calls == ["C"]
    assert admitted["findings"][0]["state"] == "covered"
    assert json.loads((tmp_path / "canon/exit-monitor/C/status.json").read_text())["account"] == "C"


def test_the_go_never_admits_a_or_b_differently_and_a_pass_on_c_needs_it_too(em, tmp_path):
    paper = Paper(positions=[position()], orders=[order()])
    assert not em.run_pass("C", paper)["ok"]
    assert paper.calls == []
    assert em.run_pass("C", paper, memory_path=tmp_path / "c.json", lead_go=C_GO)["ok"]
    assert em.monitor_task(
        "A", trading_for=lambda a: Paper(positions=[position()], orders=[order()]), lead_go=C_GO
    )["ok"]


def test_the_entry_point_takes_c_and_its_word_and_refuses_c_without_it(
    em, tmp_path, monkeypatch, capsys
):
    seen = []
    monkeypatch.setattr(
        em, "monitor_task", lambda account, **kw: seen.append((account, kw)) or {"ok": True}
    )
    monkeypatch.setattr("sys.argv", ["exit_monitor", "C", "--lead-go", C_GO])
    assert em.main() == 0
    assert seen == [("C", {"lead_go": C_GO})]
    monkeypatch.undo()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["exit_monitor", "C"])
    assert em.main() == 1
    assert "lead's named word" in capsys.readouterr().out


def test_the_symbol_holds_map_names_c_as_the_protect_instance():
    from canon_trading import symbol_hold

    assert symbol_hold.MONITOR_ACCOUNTS == {"A": "tests", "B": "windows", "C": "protect"}
