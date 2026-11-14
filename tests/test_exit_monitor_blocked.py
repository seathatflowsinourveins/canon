"""Frozen third-line brief: independent offline cases, including episode boundaries.

Contracts: alpaca-py c42add7c0a70 trading/models.py Position/Order and enums.py;
pytest ae1d0ff57b8b tmp_path and module fixture overrides. All values are synthetic;
all writes use tmp_path and no transport client is constructed.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY

from canon_trading import exit_monitor as em

NOW = datetime(2026, 11, 13, 18, 26, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture(autouse=True)
def alpaca_stream_lock(monkeypatch, tmp_path):
    """Offline: the parent fixture's key pair is taken out of the environment unread, and the
    monitor's files go under tmp_path. The rest of the environment stays as the runner gave it."""
    for name in (ENV_API_KEY, ENV_SECRET_KEY):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


def position(*, price="100", qty="1", side="long"):
    return Position.model_validate(
        {
            "asset_id": UUID(int=1),
            "symbol": "SYN",
            "exchange": "ARCA",
            "asset_class": "us_equity",
            "avg_entry_price": "100",
            "qty": qty,
            "side": side,
            "cost_basis": "100",
            "current_price": price,
        }
    )


def order(**changes):
    payload = {
        "id": UUID(int=2),
        "client_order_id": "synthetic-owner-buy",
        "created_at": NOW,
        "updated_at": NOW,
        "submitted_at": NOW,
        "symbol": "SYN",
        "asset_class": "us_equity",
        "qty": "1",
        "filled_qty": "0",
        "order_class": "simple",
        "type": "market",
        "side": "buy",
        "time_in_force": "gtc",
        "status": "accepted",
        "extended_hours": False,
    }
    payload.update(changes)
    return Order.model_validate(payload)


class Paper:
    def __init__(self, *, now=NOW, orders=None, positions=None, history=None):
        self.now = now
        self.orders = list(orders if orders is not None else [order()])
        self.positions = list(positions if positions is not None else [position()])
        self.history = list(history or [])
        self.calls = []
        self.fail = ""

    def get_clock(self):
        self.calls.append("clock")
        if self.fail == "clock":
            raise TimeoutError
        return Clock(
            timestamp=self.now,
            is_open=9 <= self.now.hour < 16,
            next_open=NOW.replace(day=16, hour=9, minute=30),
            next_close=NOW.replace(hour=16, minute=0),
        )

    def get_all_positions(self):
        self.calls.append("positions")
        return self.positions

    def get_orders(self, request):
        status = request.status.value
        self.calls.append(status)
        if self.fail == status:
            raise TimeoutError
        return self.orders if status == "open" else self.history

    def get_calendar(self, request):
        self.calls.append("calendar")
        if self.fail == "calendar":
            raise TimeoutError
        return [
            Calendar(
                date="2026-11-13",
                open="09:30",
                close="16:00",
                session_open="0400",
                session_close="2000",
            )
        ]


def run(paper, **kwargs):
    result = em.run_pass("A", paper, **kwargs)
    assert result["ok"], result["summary"]
    return result


def blocks(result):
    return [row for row in result.get("exit_blocks", []) if row["kind"] == "exit blocked"]


def notices(result):
    return [row for row in result["alerts"] if row["state"] == "exit blocked"]


@pytest.mark.parametrize(
    "status",
    [
        "new",
        "partially_filled",
        "done_for_day",
        "pending_cancel",
        "pending_replace",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "calculated",
        "held",
    ],
)
def test_every_returned_open_market_status_blocks_at_once(status):
    result = run(Paper(orders=[order(status=status)]))
    assert len(blocks(result)) == len(notices(result)) == 1
    line = blocks(result)[0]["message"]
    for phrase in (
        "SYN quantity 1",
        "buy market",
        "gtc",
        status,
        "owner",
        "extended-hours limit sell",
        "with or without sell_to_close",
        "market sell",
        "trailing stop",
        "accepted",
        "OBSERVED 2026-11-13 post-market",
    ):
        assert phrase in line
    assert "not observed in this session" not in line
    assert result["findings"][0]["state"] == "uncovered"
    assert result["status"]["exit_blocked"] == 1
    assert result["requests"] == {"base": 4, "history": 1, "tape": 0}


@pytest.mark.parametrize("hour", [2, 7, 10, 21])
def test_other_sessions_are_explicitly_unobserved(hour):
    result = run(Paper(now=NOW.replace(hour=hour)))
    assert len(blocks(result)) == 1
    assert "not observed in this session" in blocks(result)[0]["message"]


@pytest.mark.parametrize("price", ["98", "101"])
def test_limit_threshold_is_inclusive_and_a_lower_buy_is_not_named(price):
    paper = Paper(orders=[order(type="limit", limit_price=price)])
    first = run(paper)
    assert len(blocks(first)) == 1
    assert f"limit {price}" in blocks(first)[0]["message"]
    paper.orders = [order(type="limit", limit_price="97.99")]
    second = run(paper)
    assert not blocks(second) and not notices(second)
    assert len(second["exit_blocks"]) == 1
    assert second["exit_blocks"][0]["kind"] == "cleared"
    assert "97.99" not in json.dumps(second)
    assert not run(paper)["exit_blocks"]


@pytest.mark.parametrize(
    "kind,order_class",
    [
        ("stop", "simple"),
        ("stop_limit", "simple"),
        ("trailing_stop", "simple"),
        ("market", "bracket"),
        ("limit", "bracket"),
        ("limit", "oco"),
        ("market", "oto"),
    ],
)
def test_other_kinds_and_group_legs_are_named_but_never_counted(kind, order_class):
    result = run(Paper(orders=[order(type=kind, order_class=order_class, limit_price="101")]))
    rows = result.get("exit_blocks", [])
    assert len(rows) == 1
    assert rows[0]["kind"] == "not observed: may block"
    assert kind in rows[0]["message"]
    assert not blocks(result) and not notices(result)
    assert result["status"]["exit_blocked"] == 0


@pytest.mark.parametrize("state", ["covered", "dormant", "unknown"])
def test_block_is_independent_of_the_coverage_state(state):
    paper = Paper()
    assert len(blocks(run(paper))) == 1
    kwargs = {}
    if state == "unknown":
        kwargs["run_holds"] = True
    else:
        paper.orders.append(
            order(
                id=UUID(int=3),
                client_order_id=NOW.strftime("O-%Y%m%d-%H%M%S-001-001-1"),
                side="sell",
                type="trailing_stop",
                status="new",
            )
        )
        if state == "covered":
            paper.now = NOW.replace(hour=10)
    result = run(paper, **kwargs)
    assert result["findings"][0]["state"] == state
    assert len(blocks(result)) == (0 if state == "covered" else 1)


def test_status_changes_repeat_interval_and_replayed_outbox_key(tmp_path):
    paper = Paper()
    first = run(paper)
    assert len(notices(first)) == 1
    paper.orders = [order(status="new")]
    paper.now += timedelta(seconds=em.REPEAT_SECONDS - 1)
    assert not notices(run(paper))
    path = tmp_path / "canon/exit-monitor/A/memory.json"
    saved = path.read_text()
    paper.now += timedelta(seconds=1)
    repeat = run(paper)
    assert len(notices(repeat)) == 1
    assert notices(repeat)[0]["key"] != notices(first)[0]["key"]
    path.write_text(saved)
    assert notices(run(paper))[0]["key"] == notices(repeat)[0]["key"]


def test_old_uncovered_holding_and_block_each_write_one_outbox_notice(tmp_path):
    entry = order(
        id=UUID(int=3),
        status="filled",
        filled_qty="1",
        submitted_at=NOW - timedelta(minutes=3),
        filled_at=NOW - timedelta(minutes=3),
    )
    paper = Paper(history=[entry])
    first = run(paper)
    assert {notice["state"] for notice in first["alerts"]} == {"uncovered", "exit blocked"}
    outbox = tmp_path / "canon/exit-monitor/A/alerts.jsonl"
    assert [json.loads(line) for line in outbox.read_text().splitlines()] == first["alerts"]
    paper.now += timedelta(seconds=1)
    paper.orders = [order(status="new")]
    assert not run(paper)["alerts"]
    paper.orders = []
    cleared = run(paper)
    assert [row["kind"] for row in cleared["exit_blocks"]] == ["cleared"]
    assert not cleared["alerts"]
    assert len(outbox.read_text().splitlines()) == 2


@pytest.mark.parametrize("change", ["gone", "canceled", "filled", "flat", "short", "other"])
def test_episode_clears_once_without_a_notice(change):
    paper = Paper()
    assert len(blocks(run(paper))) == 1
    if change == "gone":
        paper.orders = []
    elif change in ("canceled", "filled"):
        paper.orders = [order(status=change)]
    elif change == "flat":
        paper.positions = []
    elif change == "short":
        paper.positions = [position(qty="-1", side="short")]
    else:
        paper.orders = [order(symbol="OTHER")]
    result = run(paper)
    assert not blocks(result) and not notices(result)
    assert [row["kind"] for row in result["exit_blocks"]] == ["cleared"]
    assert "exit blocked cleared" in result["exit_blocks"][0]["message"]
    assert not run(paper)["exit_blocks"]


def test_identity_is_per_order_with_no_raw_identity_in_any_output(tmp_path):
    one = order()
    two = order(id=UUID(int=3))
    paper = Paper(orders=[one, two, one])
    result = run(paper)
    assert len(blocks(result)) == len(notices(result)) == 2
    assert len({row["key"] for row in notices(result)}) == 2
    paper.orders.reverse()
    assert blocks(run(paper)) == blocks(result)
    outputs = json.dumps(result) + "".join(
        path.read_text() for path in tmp_path.rglob("*") if path.is_file()
    )
    for identity in (str(one.id), str(two.id), one.client_order_id):
        assert identity not in outputs


@pytest.mark.parametrize("failure", ["clock", "open", "calendar", "closed"])
def test_failed_read_does_not_fabricate_a_clear_or_restart_an_episode(failure):
    paper = Paper()
    assert len(notices(run(paper))) == 1
    paper.fail = failure
    if failure == "closed":
        paper.positions = [position(qty="2")]
    failed = em.run_pass("A", paper)
    assert not failed["ok"]
    assert failed["findings"][0]["state"] == "unknown"
    assert not any(row["kind"] == "cleared" for row in failed["exit_blocks"])
    paper.fail = ""
    recovered = run(paper)
    assert len(blocks(recovered)) == 1 and not notices(recovered)


@pytest.mark.parametrize("missing", ["current_price", "limit_price"])
def test_missing_limit_comparison_is_named_without_changing_coverage(missing):
    paper = Paper(orders=[order(type="limit", limit_price="98")])
    assert len(blocks(run(paper))) == 1
    if missing == "current_price":
        paper.positions = [position(price=None)]
    else:
        paper.orders = [order(type="limit", limit_price=None)]
    result = run(paper)
    assert result["findings"][0]["state"] == "uncovered"
    assert not blocks(result) and not notices(result)
    assert any("comparison unavailable" in row["message"] for row in result["exit_blocks"])
    assert not any(row["kind"] == "cleared" for row in result["exit_blocks"])


def test_named_observation_constants_and_uncalibrated_sale_reach():
    assert getattr(em, "SALE_REACH", None) == Decimal("0.02")
    assert getattr(em, "WASH_TRADE_CODE", None) == 40_310_000
    assert getattr(em, "WASH_TRADE_MESSAGE", None) == "potential wash trade detected"
    assert getattr(em, "BLOCKING_BUY_KINDS", None) == frozenset({"market", "limit"})


# The review's findings on the first build (each case failed before its fix).


def node_report(**changes):
    report = {
        "symbol": "SYN",
        "quantity": "1",
        "state": "armed",
        "level": "99",
        "armed_at": NOW,
        "last_step_at": NOW,
        "step_seconds": 20,
        "sessions": ["regular", "after-hours"],
    }
    report.update(changes)
    return report


def test_a_holding_covered_only_by_a_node_report_is_still_told_its_block():
    """A node's own stop is no order at the venue: its sale is a NEW sell, the kind the table shows
    refused beside the resting buy. So that cover does not silence the block, and a holding that
    becomes covered that way is not told "cleared" while the buy still rests."""
    paper = Paper()
    first = run(paper)
    assert len(blocks(first)) == 1 and len(notices(first)) == 1
    paper.now += timedelta(seconds=60)
    covered = run(paper, reports=[node_report(armed_at=paper.now, last_step_at=paper.now)])
    assert covered["findings"] == [] or covered["findings"][0]["state"] == "covered"
    assert len(blocks(covered)) == 1 and not notices(covered)
    assert not [row for row in covered["exit_blocks"] if row["kind"] == "cleared"]
    assert "node engine stop" in blocks(covered)[0]["message"]
    assert covered["status"]["exit_blocked"] == 1


def test_a_limit_at_the_boundary_that_flickers_gives_one_notice_inside_the_repeat_interval():
    """A limit buy at the reach's edge leaves and re-enters the rows as the price ticks. That is one
    order and one telling: a re-entry inside the repeat interval carries the episode on."""
    paper = Paper(orders=[order(type="limit", limit_price="98")])
    told = []
    for price in ("100", "100.02", "100", "100.02", "100"):
        paper.positions = [position(price=price)]
        told += notices(run(paper))
        paper.now += timedelta(seconds=60)
    assert len(told) == 1
    paper.positions = [position(price="100")]
    paper.now += timedelta(seconds=em.REPEAT_SECONDS)
    assert len(notices(run(paper))) == 1


def test_the_status_count_is_the_kept_episodes_and_is_unknown_on_a_failed_pass():
    paper = Paper(orders=[order(type="limit", limit_price="101")])
    assert run(paper)["status"]["exit_blocked"] == 1
    paper.fail = "clock"
    failed = em.run_pass("A", paper)
    assert failed["status"]["exit_blocked"] is None
    assert failed["status"]["exit_blocks_observed"] is False
    paper.fail = ""
    paper.positions = [position(price=None)]
    unread = run(paper)
    assert not blocks(unread) and not notices(unread)
    assert unread["status"]["exit_blocked"] == 1, "the episode is kept, so it is still counted"


def test_a_limit_block_says_what_was_observed_and_what_is_the_monitors_rule():
    market = blocks(run(Paper()))[0]["message"]
    assert "OBSERVED 2026-11-13 post-market" in market and "monitor's rule" not in market
    assert str(em.WASH_TRADE_CODE) in market and em.WASH_TRADE_MESSAGE in market
    limit = blocks(run(Paper(orders=[order(type="limit", limit_price="101")])))[0]["message"]
    assert "by this monitor's rule" in limit
    assert "only beside a queued limit buy not marked for extended hours" in limit
    assert "observed NOT to block a market sell" in limit


def test_a_stop_buy_is_named_as_documented_to_block_and_still_not_counted():
    result = run(Paper(orders=[order(type="stop", stop_price="101")]))
    (row,) = result["exit_blocks"]
    assert row["kind"] == "not observed: may block"
    assert "documented to block" in row["message"]
    assert not blocks(result) and not notices(result) and result["status"]["exit_blocked"] == 0


def test_unreadable_block_memory_is_dropped_alone_and_coverage_memory_is_kept(tmp_path):
    paper = Paper()
    run(paper)
    path = tmp_path / "canon/exit-monitor/A/memory.json"
    saved = json.loads(path.read_text())
    assert saved["episodes"] and saved["exit_blocks"]
    key = next(iter(saved["exit_blocks"]))
    saved["exit_blocks"][key]["alert_count"] = "many"
    path.write_text(json.dumps(saved))
    memory, note = em._load_memory(path, "A")
    assert note == "" and memory["episodes"] == saved["episodes"]
    assert memory["exit_blocks"] == {}
    paper.now += timedelta(seconds=60)
    again = run(paper)
    assert len(blocks(again)) == 1 and len(notices(again)) == 1
    assert "exit-block memory unreadable" in again["summary"]
