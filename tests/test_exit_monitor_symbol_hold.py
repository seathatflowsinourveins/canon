"""Property 3 of the hold-one-symbol unit: the exit monitor shows a long held by the owner's word
as held, not as a fault. Its word is read from the protector instance's symbol-holds folder (A reads
tests', B windows'); a held long raises no alert and adds nothing to the oldest uncovered age."""

from __future__ import annotations

import json
from datetime import timedelta

from canon_trading import symbol_hold
from tests.test_exit_monitor import NOW
from tests.test_exit_monitor import Paper
from tests.test_exit_monitor import alerts
from tests.test_exit_monitor import assess
from tests.test_exit_monitor import em  # noqa: F401 - the fixture
from tests.test_exit_monitor import level_report
from tests.test_exit_monitor import order
from tests.test_exit_monitor import position
from tests.test_exit_monitor import run


def test_assess_names_a_held_uncovered_long_held_by_the_owners_word(em):  # noqa: F811
    (finding,) = assess(em, held=frozenset({"SYN"}))
    assert finding["state"] == "held"
    assert symbol_hold.HELD in finding["reason"]


def test_the_hold_is_a_fact_apart_from_coverage(em):  # noqa: F811
    (held,) = assess(em, held=frozenset({"SYN"}))
    assert held["held_by_owner"] is True and held["state"] == "held"
    (plain,) = assess(em)
    assert plain["held_by_owner"] is False and plain["state"] == "uncovered"
    (covered,) = assess(em, orders=[order()], held=frozenset({"SYN"}))
    assert covered["state"] == "covered", "a cover still standing is told as cover"
    assert covered["held_by_owner"] is True, "and the owner's word beside it"


def test_a_node_report_in_the_held_state_is_read_not_refused(em):  # noqa: F811
    report = level_report(state="held", reason=symbol_hold.HELD)
    (finding,) = assess(em, reports=[report], held=frozenset({"SYN"}))
    assert finding["state"] == "held" and not finding["invalid"]


def test_a_held_long_raises_no_alert_and_no_uncovered_age(em, tmp_path):  # noqa: F811
    folder = symbol_hold.instance_holds("tests", home=tmp_path)
    symbol_hold.write_hold(folder, "SYN")
    paper = Paper(positions=[position(), position("5", symbol="OTH")])
    for _ in range(4):
        result = run(em, tmp_path, paper)
        paper.now += timedelta(seconds=300)
    states = {f["symbol"]: f["state"] for f in result["findings"]}
    assert states == {"SYN": "held", "OTH": "uncovered"}
    sent = json.dumps(alerts(tmp_path))
    assert "OTH" in sent, "the uncovered long is alerted as before"
    assert "SYN" not in sent, "a held long is not a fault"
    assert result["status"]["oldest_uncovered_age"] == 900, result["status"]


def test_removing_the_hold_shows_the_long_uncovered_again(em, tmp_path):  # noqa: F811
    folder = symbol_hold.instance_holds("tests", home=tmp_path)
    symbol_hold.write_hold(folder, "SYN")
    paper = Paper(positions=[position()])
    assert run(em, tmp_path, paper)["findings"][0]["state"] == "held"
    symbol_hold.remove_hold(folder, "SYN")
    paper.now = NOW + timedelta(seconds=60)
    assert run(em, tmp_path, paper)["findings"][0]["state"] == "uncovered"


def test_an_unread_hold_folder_is_named_not_taken_as_no_hold(em, tmp_path):  # noqa: F811
    folder = symbol_hold.instance_holds("tests", home=tmp_path)
    folder.parent.mkdir(parents=True)
    folder.write_text("not a folder")
    result = run(em, tmp_path, Paper(positions=[position()]))
    assert result["findings"][0]["state"] == "unknown"
    assert "symbol-holds" in result["findings"][0]["reason"]


# Review round (/code-review high, 2026-11-14): each case fails before its fix.


def test_review_f7_a_released_hold_starts_its_uncovered_age_at_the_release(em, tmp_path):  # noqa: F811
    folder = symbol_hold.instance_holds("tests", home=tmp_path)
    symbol_hold.write_hold(folder, "SYN")
    paper = Paper(positions=[position()])
    for _ in range(13):  # an hour held
        run(em, tmp_path, paper)
        paper.now += timedelta(seconds=300)
    symbol_hold.remove_hold(folder, "SYN")
    (found,) = run(em, tmp_path, paper)["findings"]
    assert found["state"] == "uncovered"
    assert found["age_seconds"] == 0, found
    assert "SYN" not in json.dumps(alerts(tmp_path)), "no alert for a holding just released"


def test_review_f8_an_unread_hold_folder_does_not_hide_a_rejection(em):  # noqa: F811
    (found,) = assess(em, rejected={"SYN": "rejected"}, holds_error="symbol-holds folder unread")
    assert found["frozen"] == "rejected", found
    assert found["state"] == "uncovered"
