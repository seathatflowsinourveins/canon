"""Handmade examples of the D3 acceptance terms (S5, A1-R); no account record is a fixture.

pytest's parametrization/tmp_path contracts: pytest@ae1d0ff57b8b,
src/_pytest/python.py:1307 and doc/en/how-to/tmp_path.rst:22.
"""

import copy
import importlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
from decimal import ROUND_FLOOR
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from canon_trading import price_step
from canon_trading.sizing import sized_stop

BASE = "c3e1a51ee7c7cc2c473bda1db53a4f48b735652d"
RUN = ["mover", "run"]
ENTRY = [*RUN, "entries", 0]
EXIT = [*RUN, "exits", 0]


def checker():
    assert importlib.util.find_spec("canon_trading.d3_check") is not None, "checker not built yet"
    return importlib.import_module("canon_trading.d3_check")


@pytest.fixture
def record() -> dict[str, Any]:
    return {
        "commit": BASE,
        "result": "ok",
        "ended": "20261113T000200Z",
        "left": [],
        "closed_after_stop": False,
        "closes": {},
        "denials": {},
        "orders": {"buy-1": ["OrderFilled"], "sell-1": ["OrderFilled"]},
        "mover": {
            "result": {"flat": True, "by_instrument": {"SYN.ALPA": {"open_shares": "0"}}},
            "run": {
                "rows": {"trail": "0.02", "spread_floor_m": "2"},
                "entries": [
                    {
                        "client_order_id": "buy-1",
                        "instrument_id": "SYN.ALPA",
                        "trail": "0.02",
                        "spread": "0.05",
                        "spread_floor": "0.01",
                        "trail_bound": "atr",
                        "trail_source": "atr",
                        "ratchet": {"tightened": False, "when_ns": None},
                        "trail_changes": [],
                        "sizing": {
                            "planned_loss": "2.00",
                            "trail": "0.02",
                            "stop": "9.80",
                            "loss_per_share": "0.20",
                            "quantity": "10",
                            "notional": "100",
                            "budget_notional": "1000",
                            "flow_shares": "100",
                            "start_cap": None,
                            "held_by": "planned_loss",
                        },
                    }
                ],
                "fills": [
                    ["buy-1", "BUY", "10", "10.00", 1_000_000_000],
                    ["sell-1", "SELL", "10", "9.80", 91_000_000_000],
                ],
                "exits": [
                    {
                        "instrument_id": "SYN.ALPA",
                        "entry_client_order_id": "buy-1",
                        "quantity": "10",
                        "filled_quantity": "10",
                        "trigger_reason": "trailing stop",
                        "trigger_price": "9.80",
                        "trigger_ns": 90_000_000_000,
                        "final_fill_ns": 91_000_000_000,
                        "sales": [
                            {
                                "client_order_id": "sell-1",
                                "sent_ns": 2_000_000_000,
                                "trailing_offset": "0.20",
                                "limit_offset": "0.25",
                                "distance": "0.20",
                                "kept_trigger": "9.80",
                                "spread": "0.05",
                                "outcome": "filled",
                                "reason": None,
                            }
                        ],
                    }
                ],
                "refusals": [],
                "order_refusals": [],
                "dropped": [],
                "sizing_refusals": {},
                "refused": [],
            },
        },
    }


def matched_spread(entry: dict[str, Any]) -> None:
    """The quoted spread the writer's floor implies at spread_floor_m 2 (mover.py:4252)."""
    sizing = entry["sizing"]
    price = Decimal(sizing["notional"]) / Decimal(sizing["quantity"])
    entry["spread"] = str(Decimal(entry["spread_floor"]) * price / 2)


def change(record: dict[str, Any], path: list[Any], value: Any) -> None:
    parent: Any = record
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def term(result: Any, name: str) -> Any:
    return next(t for t in result.terms if t.name == name)


def test_counts(record):
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("counts", 0)
    assert [t.status for t in result.terms] == ["met", "met", "met", "counted", "met", "met", "met"]
    assert result.lines()[-1] == "counts"


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("A1", [*ENTRY, "spread_floor"], "0.03"),
        ("A2", [*ENTRY, "sizing", "planned_loss"], "1.00"),
        ("A3", [*EXIT, "sales", 0, "trailing_offset"], "0.10"),
        ("A5", [*EXIT, "trigger_reason"], None),
        ("A6", ["closed_after_stop"], True),
        (
            "A7",
            [*RUN, "order_refusals"],
            [{"by": "venue", "cause": "wash_trade", "reason": "potential wash trade detected"}],
        ),
    ],
)
def test_breaks_for_each_term(record, name, path, value):
    change(record, path, value)
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("breaks", 1)
    assert term(result, name).status == "failed"
    assert term(result, name).first_failure is not None


@pytest.mark.parametrize(
    "other_evidence,summary,code",
    [("met", "counts", 0), ("missing", "incomplete", 2), ("failed", "breaks", 1)],
)
def test_a4_opening_stop_met_when_armed_is_counted_without_a_finding(
    record, tmp_path, capsys, other_evidence, summary, code
):
    change(record, [*EXIT, "trigger_reason"], "trailing stop (L under the fill, met when armed)")
    change(record, [*EXIT, "trigger_ns"], 2_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 2_000_000_000)  # sold then
    if other_evidence == "missing":
        del record["ended"]
    elif other_evidence == "failed":
        record["closed_after_stop"] = True
    result = checker().check(record)
    a4 = term(result, "A4")
    assert a4.status == "counted"
    assert a4.first_failure is None
    assert a4.counts["opening_l_stop_met_when_armed_inside_60s"] == 1
    assert (result.summary, result.exit_code) == (summary, code)
    assert result.lines()[3].startswith("A4: counted | ")
    assert "failed" not in result.lines()[3]
    assert len(result.lines()) == 8
    assert result.lines()[-1] == summary
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(record))
    assert checker().main([str(path)]) == code
    assert capsys.readouterr().out.splitlines()[-2:] == result.lines()[-2:]


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("A1", [*ENTRY, "spread"]),
        ("A2", [*ENTRY, "sizing"]),
        ("A3", [*EXIT, "sales"]),
        ("A4", [*EXIT, "trigger_ns"]),
        ("A5", [*EXIT, "filled_quantity"]),
        ("A6", ["ended"]),
        ("A7", [*RUN, "refused"]),
    ],
)
def test_missing_key_is_not_shown(record, name, path):
    parent = record
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    if name == "A7":
        del record["mover"]["run"]["order_refusals"]
    result = checker().check(record)
    # The exit clock used by A4 is also required by A5; A4 alone cannot cause this summary.
    assert (result.summary, result.exit_code) == ("incomplete", 2)
    assert term(result, name).status == "not shown"
    assert any(path[-1] in p for p in term(result, name).missing)


def test_failed_correctness_term_precedes_no_entry(record):
    for key in ("entries", "fills", "exits"):
        record["mover"]["run"][key] = []
    record["mover"]["result"]["by_instrument"] = {}
    record["result"] = "failed: interrupted"
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("breaks", 1)
    assert term(result, "A6").status == "failed"


def test_sent_but_unfilled_is_no_entry(record):
    record["mover"]["run"]["fills"] = []
    record["mover"]["run"]["exits"] = []
    record["mover"]["result"]["by_instrument"] = {}
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("no entry", 3)


def test_empty_object_is_incomplete():
    result = checker().check({})
    assert (result.summary, result.exit_code) == ("incomplete", 2)
    assert all(t.status == "not shown" for t in result.terms)


def test_fixed_fallback_is_met_and_counted(record):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread=None, spread_floor=None, trail_bound="fixed", trail_source="fixed")
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("counts", 0)
    assert "met (no usable spread: fixed plan)" in result.lines()[0]
    assert "fixed_plan_count" in result.lines()[0]
    assert term(result, "A1").counts["fixed_plan_count"] == 1


@pytest.mark.parametrize(
    "path,value",
    [
        (["arm"], "F2"),
        (["mover", "trail_mode"], "fixed"),
        ([*RUN, "rows", "trail_mode"], "fixed"),
        ([*ENTRY, "trail_mode"], "fixed"),
    ],
)
def test_recorded_fixed_arm_is_practice_even_without_spread(record, path, value):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread=None, spread_floor=None, trail_bound="fixed", trail_source="fixed")
    change(record, path, value)
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("practice", 0)
    assert term(result, "A1").status == term(result, "A4").status == "not judged"
    assert term(result, "A1").counts["fixed_arm_entries"] == 1
    assert all(term(result, name).status == "met" for name in ("A2", "A3", "A5", "A6", "A7"))


def test_fixed_arm_does_not_evaluate_a1_or_a4(record):
    record["arm"] = "F2"
    del record["mover"]["run"]["entries"][0]["spread"]
    change(record, [*EXIT, "trigger_ns"], 2_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 2_000_000_000)  # sold then
    change(record, [*EXIT, "trigger_reason"], "trailing stop (L under the fill, met when armed)")
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("practice", 0)
    for name in ("A1", "A4"):
        assert term(result, name).status == "not judged"
        assert term(result, name).missing == []
        assert term(result, name).first_failure is None


@pytest.mark.parametrize("name,path", [("A2", [*ENTRY, "sizing"]), ("A3", [*EXIT, "sales"])])
def test_a_fixed_arm_with_absent_shared_evidence_is_incomplete(record, name, path):
    record["arm"] = "F2"
    change(record, path, None)
    result = checker().check(record)
    assert term(result, name).status == "not shown"
    assert (result.summary, result.exit_code) == ("incomplete", 2)


def test_fixed_arm_without_entry_reports_failed_flatness_as_practice(record):
    record["arm"] = "F2"
    for key in ("entries", "fills", "exits"):
        record["mover"]["run"][key] = []
    record["mover"]["result"]["by_instrument"] = {}
    record["closed_after_stop"] = True
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("practice (finding: A6)", 5)
    assert term(result, "A6").status == "failed"
    assert result.lines()[5].startswith("A6: failed (fixed arm) | ")


def test_fixed_arm_summary_names_all_failed_terms(record, tmp_path, capsys):
    record["arm"] = "F2"
    change(record, [*EXIT, "trigger_reason"], "")
    record["closed_after_stop"] = True
    record["denials"] = {"sell-1": "potential wash trade detected"}
    result = checker().check(record)
    summary = "practice (finding: A5, A6, A7)"
    assert (result.summary, result.exit_code) == (summary, 5)
    assert result.lines()[-1] == summary
    for name in ("A5", "A6", "A7"):
        assert term(result, name).status == "failed"
        assert any(line.startswith(f"{name}: failed (fixed arm) | ") for line in result.lines())
    path = tmp_path / "practice.json"
    path.write_text(json.dumps(record))
    assert checker().main([str(path)]) == 5
    assert capsys.readouterr().out.splitlines()[-1] == summary


@pytest.mark.parametrize("key,value", [("trail_source", "atr"), ("trail", "0.03")])
def test_no_spread_requires_the_recorded_fixed_plan(record, key, value):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread=None, spread_floor=None, trail_bound="fixed", trail_source="fixed")
    entry[key] = value
    assert term(checker().check(record), "A1").status == "failed"


def test_null_floor_with_usable_spread_is_not_shown(record):
    change(record, [*ENTRY, "spread_floor"], None)
    assert term(checker().check(record), "A1").status == "not shown"


def test_a1_uses_actual_dollar_distance(record):
    change(record, [*ENTRY, "sizing", "loss_per_share"], "0.09")
    result = checker().check(record)
    failure = term(result, "A1").first_failure
    assert failure["value"] == "0.09"
    assert "loss_per_share" in failure["path"]


def test_named_drops_supply_refusals_when_structured_rows_are_absent(record):
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = ["SYN.ALPA: the spread floor exceeds the 0.02 cap; no entry satisfies both"]
    result = checker().check(record)
    assert term(result, "A1").status == "met"
    assert any("dropped" in p for p in term(result, "A1").paths)


def test_new_refusals_supersede_dropped(record):
    run = record["mover"]["run"]
    run["refusals"] = [
        {
            "reason": "floor_over_cap",
            "distance": "0.03",
            "cap": "0.02",
            "when_ns": 10,
            "instrument_id": "AAA.ALPACA",
            "decisions": 1,
            "last_ns": 10,
        }
    ]
    run["dropped"] = ["not the refusal channel"]
    result = checker().check(record)
    assert term(result, "A1").status == "met"
    assert not any("dropped" in p for p in term(result, "A1").paths)


WRITER_REFUSAL = {"when_ns": 10, "instrument_id": "AAA.ALPACA", "decisions": 1, "last_ns": 10}


def test_the_writers_refusal_rows_are_read_by_their_reason(record):
    """The prospective record contract (2026-11-13): a refusal row names its cause in `reason`, not
    `cause`; `distance` and `cap` are on the two trail-cap reasons only, and a `reentry_max` row
    carries `reentry_max` and `reentries` instead."""
    record["mover"]["run"]["refusals"] = [
        {**WRITER_REFUSAL, "reason": "floor_over_cap", "distance": "0.03", "cap": "0.02"},
        {**WRITER_REFUSAL, "reason": "distance_over_cap", "distance": "0.025", "cap": "0.02"},
        {**WRITER_REFUSAL, "reason": "reentry_max", "reentry_max": 2, "reentries": 2},
    ]
    result = checker().check(record)
    assert term(result, "A1").status == "met"
    assert term(result, "A1").counts["named_refusals"] == 3


@pytest.mark.parametrize(
    "row",
    [
        {**WRITER_REFUSAL, "reason": "a reason no writer gives"},
        {**WRITER_REFUSAL, "cause": "floor_over_cap", "distance": "0.03", "cap": "0.02"},
        {**WRITER_REFUSAL, "reason": "floor_over_cap"},
    ],
)
def test_a_refusal_row_the_writer_would_not_write_is_not_shown(record, row):
    record["mover"]["run"]["refusals"] = [row]
    assert term(checker().check(record), "A1").status == "not shown"


def test_unknown_commit_reads_named_drops_with_a_no_refusal_rows_note(record):
    del record["mover"]["run"]["refusals"]
    record["commit"] = "f" * 40
    result = checker().check(record)
    assert term(result, "A1").status == "met"
    assert term(result, "A1").counts["named_refusals"] == 0
    assert any("no refusal rows" in note for note in term(result, "A1").notes)
    assert any("dropped" in p for p in term(result, "A1").paths)


@pytest.mark.parametrize("commit", [BASE, "16d02e1f9aa2253f6818aa46d17a2535d5a2f5d9", "f" * 40])
def test_named_drops_are_counted_at_every_commit(record, commit):
    record["commit"] = commit
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = [
        "SYN.ALPA: the spread floor exceeds the 0.02 cap; no entry satisfies both",
        "SYN.ALPA: the trail 0.03 (atr) is over the 0.02 cap; refused (trail_cap refuse)",
        (
            "SYN.ALPA: the distance 0.005 sized for an entry at 1.01 is under its step 0.01: "
            "no trail can hold it"
        ),
        (
            "SYN.ALPA: the bid 9.80 is at or under 9.85, the distance 0.20 sized for an "
            "entry at 10.00 under the ask 10.05: the quote is wider than the trail"
        ),
        "SYN.ALPA: no positive bid to check against the planned stop",
    ]
    run["sizing_refusals"] = {"distance_under_step": 1, "bid_at_stop": 1}
    a1 = term(checker().check(record), "A1")
    assert a1.status == "met"
    assert a1.counts["named_refusals"] == 5
    assert any("no refusal rows" in note for note in a1.notes)


@pytest.mark.parametrize(
    "reason",
    [
        # mover.py:3444-3452,3500-3518,3652-3678,3829 at 16d02e1f.
        "no share cap for this window instrument",
        "no quote yet",
        "no account balance to size from",
        "no room for the trail under 0.01",
        "no positive bid to check against the planned stop",
        (
            "the bid 9.80 is under the planned stop 9.85 of an entry at 10.00: "
            "the quote is wider than the plan"
        ),
        "no free balance to size from",
        "the caps leave no share at 10.00",
        "the size from risk came to nothing at 10.00",
        "the entry switch reads off",
        # _buy forwards entry_price's named errors (price_step.py:59-71).
        "no limit price 0 ticks above NaN in ALPACA_US_EQUITY_LIMIT",
        "no limit price 0 ticks above 5000: above the top tier of ALPACA_US_EQUITY_LIMIT, 4999.99",
        # _buy:3469-3471 forwards TurnState.turn_refusal:1261-1270 with this prefix.
        "opening: turn state or trail distance unavailable",
        "opening: turn depth 0.10 is under 2 trail distances (0.20)",
        "opening: turn needs 3 rising closes from the low; has 1",
        # _buy:3573-3578 has its own size_zero text; :3603-3617 names two further bounds.
        "sizing refused: size_zero (the figure 1 USD buys no share at 10)",
        (
            "the distance 0.005 sized for an entry at 1.01 is under its step 0.01: "
            "no trail can hold it"
        ),
        (
            "the bid 9.80 is at or under 9.85, the distance 0.20 sized for an "
            "entry at 10.00 under the ask 10.05: the quote is wider than the trail"
        ),
        # _entry_trail:3850,3876,3964-3971; the no-quote branch is covered above.
        "profile document is missing; no profile entry is allowed",
        "the spread floor exceeds the 0.02 cap; no entry satisfies both",
        (
            "the trail 0.03 (atr) is over the 0.02 cap, and trail_cap own needs "
            "size_from_budget to size it; refused"
        ),
        "the trail 0.03 (atr) is over the 0.02 cap; refused (trail_cap refuse)",
    ],
)
def test_each_named_entry_drop_is_counted_once(record, reason):
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = [f"SYN.ALPA: {reason}"]
    a1 = term(checker().check(record), "A1")
    assert a1.status == "met"
    assert a1.counts["named_refusals"] == 1


@pytest.mark.parametrize(
    "name",
    [
        # sizing.py:54-66, rendered by mover.py:3588-3598 at 16d02e1f.
        "distance_unknown",
        "distance_over_ceiling",
        "stop_zero",
        "entry_invalid",
        "entry_off_step",
        "planned_loss_invalid",
        "planned_loss_precision",
        "flow_cap_invalid",
        "budget_invalid",
        "loss_under_one_share",
        "size_zero",
    ],
)
def test_each_named_sizing_refusal_is_counted_once_without_adding_its_counter(record, name):
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = [
        (
            f"SYN.ALPA: sizing refused: {name} "
            "(trail 0.12, planned loss 2 USD, budget 100 USD, share cap 10, at 10)"
        )
    ]
    run["sizing_refusals"] = {name: 1}
    a1 = term(checker().check(record), "A1")
    assert a1.status == "met"
    assert a1.counts["named_refusals"] == 1


@pytest.mark.parametrize(
    "reason",
    [
        "sizing failed, not a refusal: ValueError: unknown upstream error",
        "sizing refused: unknown_name (not a writer refusal)",
        "no usable quoted spread (missing/zero bid, crossed or locked): fixed trail fallback",
        "ATR has fewer than 14 complete one-minute bars: fixed trail fallback",
        "no longer ranked",
    ],
)
def test_notes_and_unlisted_drop_reasons_are_not_named_entry_refusals(record, reason):
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = [f"SYN.ALPA: {reason}"]
    assert term(checker().check(record), "A1").counts["named_refusals"] == 0


def test_refusal_never_excuses_an_actual_bad_entry(record):
    run = record["mover"]["run"]
    run["refusals"] = [
        {
            "reason": "floor_over_cap",
            "distance": "0.03",
            "cap": "0.02",
            "when_ns": 10,
            "instrument_id": "AAA.ALPACA",
            "decisions": 1,
            "last_ns": 10,
        }
    ]
    change(record, [*ENTRY, "spread_floor"], "0.03")
    assert term(checker().check(record), "A1").status == "failed"


def test_null_sizing_is_not_shown(record):
    change(record, [*ENTRY, "sizing"], None)
    assert term(checker().check(record), "A2").status == "not shown"


@pytest.mark.parametrize(
    "field,value", [("quantity", "10.5"), ("stop", "9.805"), ("quantity", "9")]
)
def test_sizing_rejects_wrong_size_or_price_step(record, field, value):
    sizing = record["mover"]["run"]["entries"][0]["sizing"]
    sizing[field] = value
    if field == "quantity":
        from decimal import Decimal

        sizing["notional"] = str(Decimal(value) * 10)
    assert term(checker().check(record), "A2").status == "failed"


def test_budget_and_start_cap_can_bind_below_planned_loss(record):
    sizing = record["mover"]["run"]["entries"][0]["sizing"]
    sizing.update(
        planned_loss="20", start_cap="5", quantity="5", notional="50", held_by="start_cap"
    )
    assert term(checker().check(record), "A2").status == "met"


@pytest.mark.parametrize("roster", [False, True])
def test_unrecorded_max_shares_bound_is_not_shown(record, roster):
    sizing = record["mover"]["run"]["entries"][0]["sizing"]
    sizing.update(quantity="5", notional="50", held_by="max_shares")
    if roster:
        record["mover"]["roster"] = [{"instrument_id": "SYN.ALPA", "max_shares": 100}]
    a2 = term(checker().check(record), "A2")
    assert a2.status == "not shown"
    assert a2.first_failure is None
    assert any("max_shares" in note and "mover.py:1924" in note for note in a2.notes)


def test_cross_dollar_stop_sizes_against_stop_before_distance_is_floored(record):
    entry = record["mover"]["run"]["entries"][0]
    entry["trail"] = "0.015"
    entry["spread_floor"] = "0.005"
    entry["sizing"].update(
        planned_loss="1.52",
        trail="0.015",
        stop="0.9948",
        loss_per_share="0.01",
        quantity="100",
        notional="101",
    )
    assert term(checker().check(record), "A2").status == "met"


@pytest.mark.parametrize("entries", [1, 2])
def test_cross_dollar_floor_tolerance_is_counted_per_entry(record, entries):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(trail="0.015", spread_floor="0.015")
    entry["sizing"].update(
        planned_loss="1.52",
        trail="0.015",
        stop="0.9948",
        loss_per_share="0.01",
        quantity="100",
        notional="101",
    )
    matched_spread(entry)
    record["mover"]["run"]["entries"] = [copy.deepcopy(entry) for _ in range(entries)]
    change(record, [*EXIT, "trigger_ns"], 2_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 2_000_000_000)  # sold then
    result = checker().check(record)
    assert term(result, "A1").status == "met"
    assert term(result, "A1").counts["p5_tolerance_used"] == entries
    assert term(result, "A4").status == "counted"
    assert term(result, "A4").counts["inside_60s"] == 1
    detail = json.loads(result.lines()[0].split(" | ", 1)[1])
    assert detail["counts"]["p5_tolerance_used"] == entries


@pytest.mark.parametrize("quick_exits", [0, 1, 2])
def test_a4_counts_each_cross_dollar_tolerance_use(record, quick_exits):
    run = record["mover"]["run"]
    entry = run["entries"][0]
    entry.update(trail="0.015", spread_floor="0.015")
    entry["sizing"].update(
        planned_loss="1.52",
        trail="0.015",
        stop="0.9948",
        loss_per_share="0.01",
        quantity="100",
        notional="101",
    )
    matched_spread(entry)
    exits = []
    for index in range(3):
        row = copy.deepcopy(run["exits"][0])
        row["trigger_ns"] = 2_000_000_000 if index < quick_exits else 90_000_000_000
        row["final_fill_ns"] = row["trigger_ns"]  # sold then
        exits.append(row)
    run["exits"] = exits
    result = checker().check(record)
    a4 = term(result, "A4")
    assert a4.status == "counted"
    assert a4.counts["inside_60s"] == quick_exits
    assert a4.counts.get("p5_tolerance_used", 0) == quick_exits
    assert term(result, "A1").counts["p5_tolerance_used"] == 1
    detail = json.loads(result.lines()[3].split(" | ", 1)[1])
    assert detail["counts"].get("p5_tolerance_used", 0) == quick_exits


@pytest.mark.parametrize(
    "price,stop,distance,floor",
    [("10", "9.80", "0.19", "0.02"), ("0.50", "0.4900", "0.0099", "0.02")],
)
def test_same_step_one_tick_floor_shortfall_fails(record, price, stop, distance, floor):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(trail=floor, spread_floor=floor)
    entry["sizing"].update(
        trail=floor, stop=stop, notional=str(Decimal(price) * 10), loss_per_share=distance
    )
    matched_spread(entry)
    a1 = term(checker().check(record), "A1")
    assert a1.status == "failed"
    assert '"loss_per_share"' in a1.first_failure["path"]
    assert a1.counts.get("p5_tolerance_used", 0) == 0


def test_no_floor_shortfall_does_not_count_tolerance(record):
    assert term(checker().check(record), "A1").counts.get("p5_tolerance_used", 0) == 0


def test_floor_shortfall_over_one_tick_fails_even_without_stop(record):
    sizing = record["mover"]["run"]["entries"][0]["sizing"]
    sizing["loss_per_share"] = "0.08"
    del sizing["stop"]
    a1 = term(checker().check(record), "A1")
    assert a1.status == "failed"
    assert '"loss_per_share"' in a1.first_failure["path"]
    assert a1.counts["p5_tolerance_used"] == 0


def test_sizing_and_tick_parity_on_price_distance_grid(record):
    prices = map(
        Decimal,
        (
            "0.0001",
            "0.0002",
            "0.001",
            "0.01",
            "0.1234",
            "0.9998",
            "0.9999",
            "1",
            "1.01",
            "1.02",
            "10.29",
            "500",
        ),
    )
    distances = map(
        Decimal, ("0.0001", "0.005", "0.015", "0.02", "0.03333333333333333333333333333", "0.10")
    )
    for price, distance in product(prices, distances):
        assert checker()._tick(price) == price_step.tick(price)
        stop = sized_stop(price, distance)
        loss = price - stop
        opening = loss.quantize(price_step.tick(price), rounding=ROUND_FLOOR)
        fixture = copy.deepcopy(record)
        entry = fixture["mover"]["run"]["entries"][0]
        entry["trail"] = str(distance)
        sizing = entry["sizing"]
        sizing.update(
            trail=str(distance),
            stop=str(stop),
            loss_per_share=str(opening),
            quantity="10",
            notional=str(price * 10),
            planned_loss=str(loss * 10),
            budget_notional=str(price * 1000),
            flow_shares="100",
        )
        a2 = term(checker().check(fixture), "A2")
        if stop <= 0 or loss > price * Decimal("0.10") or opening <= 0:
            assert a2.status == "failed", (price, distance)
        else:
            assert a2.status == "met", (price, distance, a2.first_failure, a2.notes)
            wrong = price_step.stop_trigger_price(stop - price_step.tick(stop))
            sizing["stop"] = str(wrong)
            sizing["loss_per_share"] = str(
                (price - wrong).quantize(price_step.tick(price), rounding=ROUND_FLOOR)
            )
            a2 = term(checker().check(fixture), "A2")
            assert a2.status == "failed", (price, distance)
            assert '"stop"' in a2.first_failure["path"]


def test_every_remake_is_checked_and_first_bad_value_is_kept(record):
    sales = record["mover"]["run"]["exits"][0]["sales"]
    sales.extend([dict(sales[0], trailing_offset="0.10"), dict(sales[0], trailing_offset="0.05")])
    result = checker().check(record)
    failure = term(result, "A3").first_failure
    assert failure["value"] == "0.10"
    assert '[1]["trailing_offset"]' in failure["path"]


def test_missing_sale_then_bad_sale_still_fails(record):
    sales = record["mover"]["run"]["exits"][0]["sales"]
    sales.append(dict(sales[0], trailing_offset="0.10"))
    del sales[0]["distance"]
    assert term(checker().check(record), "A3").status == "failed"


def test_empty_sales_with_a_holding_is_not_proof(record):
    change(record, [*EXIT, "sales"], [])
    assert term(checker().check(record), "A3").status == "not shown"


def test_no_sales_key_even_with_no_exits_is_not_shown(record):
    record["mover"]["run"]["exits"] = []
    assert term(checker().check(record), "A3").status == "not shown"


def test_sizing_plan_cannot_accept_a_distance_over_its_ceiling(record):
    entry = record["mover"]["run"]["entries"][0]
    entry["trail"] = "0.20"
    entry["sizing"].update(trail="0.20", stop="8.00", loss_per_share="2", planned_loss="20")
    assert term(checker().check(record), "A2").status == "failed"


def test_sizing_plan_cannot_accept_rounding_past_its_ceiling(record):
    entry = record["mover"]["run"]["entries"][0]
    entry["trail"] = "0.10"
    entry["sizing"].update(
        trail="0.10", stop="1.01", loss_per_share="0.12", planned_loss="1.2", notional="11.3"
    )
    assert term(checker().check(record), "A2").status == "failed"


def test_partial_holding_cannot_have_a_final_fill_clock(record):
    change(record, [*EXIT, "filled_quantity"], "5")
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "5"
    assert term(checker().check(record), "A5").status == "failed"


def test_quick_sale_above_floor_is_counted(record):
    change(record, [*EXIT, "trigger_ns"], 2_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 2_000_000_000)  # sold then
    result = checker().check(record)
    assert term(result, "A4").status == "counted"
    assert term(result, "A4").counts["inside_60s"] == 1


def test_exactly_sixty_seconds_is_not_inside(record):
    change(record, [*EXIT, "trigger_ns"], 61_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 61_000_000_000)  # sold then
    change(record, [*EXIT, "trigger_reason"], "trailing stop (L under the fill, met when armed)")
    a4 = term(checker().check(record), "A4")
    assert a4.status == "counted"
    assert a4.counts["opening_l_stop_met_when_armed_inside_60s"] == 0


def test_quick_ratchet_cannot_be_blame_assigned_to_opening_distance(record):
    change(record, [*EXIT, "trigger_ns"], 3_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 3_000_000_000)  # sold then
    change(record, [*EXIT, "trigger_reason"], "trailing stop (L under the fill, met when armed)")
    change(record, [*ENTRY, "ratchet"], {"tightened": True, "when_ns": 2_000_000_000})
    assert term(checker().check(record), "A4").status == "not shown"


def test_a4_missing_attribution_does_not_make_the_window_incomplete(record, tmp_path, capsys):
    change(record, [*EXIT, "trigger_ns"], 3_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 3_000_000_000)  # sold then
    change(record, [*EXIT, "trigger_reason"], "trailing stop (L under the fill, met when armed)")
    change(record, [*ENTRY, "ratchet"], {"tightened": True, "when_ns": 2_000_000_000})
    result = checker().check(record)
    a4 = term(result, "A4")
    assert a4.status == "not shown"
    assert a4.counts["inside_60s"] == 1
    assert (result.summary, result.exit_code) == ("counts", 0)
    assert "counted" in result.lines()[3]
    assert "failed" not in result.lines()[3]
    path = tmp_path / "uncertain-observation.json"
    path.write_text(json.dumps(record))
    assert checker().main([str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "counts"


def test_a4_bad_sizing_is_missing_observation_not_a_failure(record):
    change(record, [*EXIT, "trigger_ns"], 2_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 2_000_000_000)  # sold then
    change(record, [*ENTRY, "sizing", "quantity"], "0")
    result = checker().check(record)
    a4 = term(result, "A4")
    assert a4.status == "not shown"
    assert a4.first_failure is None
    assert "failed" not in result.lines()[3]
    assert "counted" in result.lines()[3]


@pytest.mark.parametrize(
    "price,stop,distance,floor,trail,expected",
    [
        ("10", "9.80", "0.20", "0.02", "0.02", (True, False)),
        ("10", "9.80", "0.19", "0.02", "0.02", (False, False)),
        ("1.01", "0.9948", "0.01", "0.015", "0.015", (True, True)),
        ("1.01", "0.9847", "0.01", "0.025", "0.025", (False, False)),
        ("1.01", "0.9958", "0.01", "0.015", "0.014", (False, False)),
    ],
)
def test_opening_floor_reports_whether_cross_dollar_tolerance_was_used(
    record, price, stop, distance, floor, trail, expected
):
    entry = record["mover"]["run"]["entries"][0]
    entry.update(trail=trail, spread_floor=floor)
    entry["sizing"].update(stop=stop, loss_per_share=distance, notional=str(Decimal(price) * 10))
    observation = checker().Term("A4", record)
    observation.counts["p5_tolerance_used"] = 0
    assert checker()._opening_floor(observation, tuple(ENTRY)) == expected
    assert observation.counts["p5_tolerance_used"] == 0


def test_buy_without_an_exit_record_is_not_silently_met(record):
    record["mover"]["run"]["exits"] = []
    assert term(checker().check(record), "A5").status == "failed"


@pytest.mark.parametrize("identity", [None, "", "   ", 7, {}, "absent"])
def test_unreadable_exit_identity_is_missing_evidence(record, identity):
    row = record["mover"]["run"]["exits"][0]
    if identity == "absent":
        del row["entry_client_order_id"]
    else:
        row["entry_client_order_id"] = identity
    result = checker().check(record)
    a5 = term(result, "A5")
    assert a5.status == "not shown"
    assert a5.first_failure is None
    assert any("entry_client_order_id" in note for note in a5.notes)
    assert (result.summary, result.exit_code) == ("incomplete", 2)


def test_unreadable_exit_identity_does_not_hide_a_named_exit_failure(record):
    rows = record["mover"]["run"]["exits"]
    bad_exit = copy.deepcopy(rows[0])
    bad_exit["trigger_reason"] = ""
    rows[0]["entry_client_order_id"] = None
    rows.append(bad_exit)
    a5 = term(checker().check(record), "A5")
    assert a5.status == "failed"
    assert a5.first_failure["path"].endswith('[1]["trigger_reason"]')


@pytest.mark.parametrize("field,value", [("trigger_reason", ""), ("final_fill_ns", None)])
def test_exit_requires_reason_and_completion_evidence(record, field, value):
    change(record, [*EXIT, field], value)
    assert term(checker().check(record), "A5").status == "failed"


def test_named_unsold_holding_fails_flatness_not_documentation(record):
    change(record, [*EXIT, "filled_quantity"], "5")
    change(record, [*EXIT, "final_fill_ns"], None)
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "5"
    record["mover"]["result"]["flat"] = False
    result = checker().check(record)
    assert term(result, "A5").status == "met"
    assert term(result, "A6").status == "failed"


@pytest.mark.parametrize("reason", ["wind-down", "unpriced_exit"])
def test_null_exit_price_exemption_requires_a_sold_holding(record, reason):
    change(record, [*EXIT, "trigger_reason"], reason)
    change(record, [*EXIT, "trigger_price"], None)
    change(record, [*EXIT, "filled_quantity"], "5")
    change(record, [*EXIT, "final_fill_ns"], None)
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "5"
    record["mover"]["result"]["flat"] = False
    assert term(checker().check(record), "A5").status == "not shown"


@pytest.mark.parametrize("final_fill", [91_000_000_000, None])
def test_sold_unpriced_exit_accepts_null_price_but_requires_final_fill(record, final_fill):
    change(record, [*EXIT, "trigger_reason"], "unpriced_exit")
    change(record, [*EXIT, "trigger_price"], None)
    change(record, [*EXIT, "final_fill_ns"], final_fill)
    result = checker().check(record)
    a5 = term(result, "A5")
    if final_fill is None:
        assert a5.status == "failed"
        assert a5.first_failure["path"].endswith('["final_fill_ns"]')
    else:
        assert a5.status == "met"
        assert (result.summary, result.exit_code) == ("counts", 0)


@pytest.mark.parametrize(
    "path,value",
    [
        (["closes"], {"close-1": ["filled"]}),
        (["left"], ["SYN"]),
        (["result"], "failed: stop"),
        (["mover", "result", "flat"], False),
    ],
)
def test_a6_requires_own_wind_down(record, path, value):
    change(record, path, value)
    assert term(checker().check(record), "A6").status == "failed"


@pytest.mark.parametrize(
    "reason",
    [
        "potential wash trade detected",
        "Alpaca rejected the replace (trade update)",
        "stop price 9.805 has more than 2 decimals, which Alpaca rejects for a price at or above $1.00",
    ],
)
def test_documented_refusal_patterns(record, reason):
    record["mover"]["run"]["order_refusals"] = [{"by": "venue", "cause": "other", "reason": reason}]
    assert term(checker().check(record), "A7").status == "failed"


def test_pre_venue_refusal_does_not_claim_alpaca_refused(record):
    record["mover"]["run"]["order_refusals"] = [
        {
            "by": "before_venue",
            "cause": "off_step",
            "reason": "stop price 9.805 has more than 2 decimals, which Alpaca rejects for a price at or above $1.00",
        }
    ]
    assert term(checker().check(record), "A7").status == "met"


def test_readable_other_refusal_is_an_observation_regardless_of_cause_label(record):
    record["mover"]["run"]["order_refusals"] = [
        {"by": "venue", "cause": "wash_trade", "reason": "something unfamiliar"}
    ]
    a7 = term(checker().check(record), "A7")
    assert a7.status == "met"
    assert any("something unfamiliar" in note for note in a7.notes)


def test_unknown_then_known_refusal_still_fails(record):
    record["mover"]["run"]["order_refusals"] = [
        {"by": "venue", "cause": "other", "reason": "unknown"},
        {"by": "venue", "cause": "wash_trade", "reason": "potential wash trade detected"},
    ]
    assert term(checker().check(record), "A7").status == "failed"


def test_structured_refusals_do_not_hide_legacy_refused_rows(record):
    record["mover"]["run"]["refused"] = [["other-buy", "potential wash trade detected"]]
    assert term(checker().check(record), "A7").status == "failed"


def test_duplicate_order_id_keeps_distinct_reasons(record):
    record["mover"]["run"]["order_refusals"] = [
        {"client_order_id": "other-buy", "by": "venue", "cause": "other", "reason": "halted"}
    ]
    record["denials"] = {"other-buy": "potential wash trade detected"}
    a7 = term(checker().check(record), "A7")
    assert a7.status == "failed"
    assert a7.counts["refused_orders"] == 1


def test_duplicate_pre_venue_reason_keeps_its_provenance(record):
    reason = "potential wash trade detected"
    record["mover"]["run"]["order_refusals"] = [
        {
            "client_order_id": "other-buy",
            "by": "before_venue",
            "cause": "wash_trade",
            "reason": reason,
        }
    ]
    record["denials"] = {"other-buy": reason}
    record["orders"]["other-buy"] = ["OrderDenied"]
    a7 = term(checker().check(record), "A7")
    assert a7.status == "met"
    assert a7.counts["refused_orders"] == 1


def test_venue_reason_does_not_need_a_cause_label(record):
    record["mover"]["run"]["order_refusals"] = [
        {"by": "venue", "reason": "potential wash trade detected"}
    ]
    assert term(checker().check(record), "A7").status == "failed"


@pytest.mark.parametrize("rows", [None, [{}]])
def test_bad_structured_refusals_do_not_mask_denials(record, rows):
    record["mover"]["run"]["order_refusals"] = rows
    record["denials"] = {"other-buy": "potential wash trade detected"}
    assert term(checker().check(record), "A7").status == "failed"


def test_legacy_denial_and_modify_event_evidence(record):
    del record["mover"]["run"]["order_refusals"]
    record["denials"] = {"sell-1": "potential wash trade detected"}
    assert term(checker().check(record), "A7").status == "failed"
    record["denials"] = {}
    record["orders"]["sell-1"] = ["OrderModifyRejected"]
    a7 = term(checker().check(record), "A7")
    assert a7.status == "not shown"
    assert a7.first_failure is None
    assert any("no modify reason" in note for note in a7.notes)


@pytest.mark.parametrize("reason", [None, "market closed"])
def test_modify_rejection_without_its_reason_cannot_identify_the_refuser(record, reason):
    record["orders"]["sell-1"] = ["OrderModifyRejected", "OrderModifyRejected", "OrderFilled"]
    if reason is not None:
        record["mover"]["run"]["order_refusals"] = [
            {"client_order_id": "sell-1", "by": "venue", "reason": reason}
        ]
    a7 = term(checker().check(record), "A7")
    assert a7.status == "not shown"
    assert a7.first_failure is None
    assert any("no modify reason" in note for note in a7.notes)
    assert any(
        "Mover never calls modify_order" in note and "unreachable" in note for note in a7.notes
    )
    assert a7.counts["refused_orders"] == 1


def test_cancel_rejection_is_not_an_a7_cause(record):
    record["orders"]["sell-1"] = ["OrderCancelRejected", "OrderFilled"]
    result = checker().check(record)
    a7 = term(result, "A7")
    assert a7.status == "met"
    assert a7.first_failure is None
    assert any("OrderCancelRejected" in note and "outside A7" in note for note in a7.notes)
    assert all(count == 0 for count in a7.counts.values())
    assert (result.summary, result.exit_code) == ("counts", 0)


def test_cancel_rejections_do_not_inflate_other_refused_order_counts(record):
    record["orders"]["sell-1"] = ["OrderCancelRejected", "OrderFilled", "OrderCancelRejected"]
    record["orders"]["sell-2"] = ["OrderCancelRejected"]
    record["denials"] = {"other-buy": "market closed"}
    a7 = term(checker().check(record), "A7")
    assert a7.status == "met"
    assert a7.counts == {"refused_orders": 1}


def test_modify_rejection_uncertainty_does_not_hide_a_proven_venue_failure(record):
    record["orders"]["sell-1"] = ["OrderModifyRejected"]
    record["denials"] = {"other-buy": "potential wash trade detected"}
    a7 = term(checker().check(record), "A7")
    assert a7.status == "failed"
    assert a7.first_failure["value"] == "potential wash trade detected"
    assert any("no modify reason" in note for note in a7.notes)


def test_usage_and_module_document_practice_finding_exit_code(capsys):
    assert checker().main([]) == 4
    assert "practice finding 5" in capsys.readouterr().out
    assert "practice finding 5" in checker().__doc__


def resolve_path(record, path):
    assert path.startswith("$")
    decoder = json.JSONDecoder()
    value = record
    tail = path[1:]
    while tail:
        assert tail.startswith("[")
        key, end = decoder.raw_decode(tail[1:])
        assert tail[end + 1] == "]"
        value = value[key]
        tail = tail[end + 2 :]
    return value


def test_every_printed_read_path_exists_in_its_fixture(record):
    variants = [record, copy.deepcopy(record), copy.deepcopy(record)]
    del variants[1]["mover"]["run"]["exits"][0]["sales"]
    variants[2]["mover"]["run"]["exits"][0]["sales"][0]["trailing_offset"] = "0.01"
    for fixture in variants:
        result = checker().check(fixture)
        for line in result.lines()[:-1]:
            detail = json.loads(line.split(" | ", 1)[1])
            assert detail["paths"]
            for path in detail["paths"]:
                resolve_path(fixture, path)
            if detail.get("first_failure"):
                failure = detail["first_failure"]
                assert resolve_path(fixture, failure["path"]) == failure["value"]


@pytest.mark.parametrize(
    "contents", ["{", "[]", '{"x": NaN}', '{"result": "ok", "result": "failed"}']
)
def test_unreadable_record(contents, tmp_path, capsys):
    path = tmp_path / "record.json"
    path.write_text(contents)
    assert checker().main([str(path)]) == 4
    assert "unreadable" in capsys.readouterr().out


def test_missing_file_is_unreadable(tmp_path, capsys):
    assert checker().main([str(tmp_path / "absent.json")]) == 4
    assert "unreadable" in capsys.readouterr().out


def test_cli_reads_only_requested_file_and_opens_no_network(record, tmp_path, monkeypatch, capsys):
    path = tmp_path / "record.json"
    text = json.dumps(record)
    path.write_text(text)
    module = checker()
    real_open = io.open
    reads = []

    def read_only(file, mode="r", *args, **kwargs):
        assert Path(file) == path
        assert mode == "r"
        reads.append(file)
        return real_open(file, mode, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("checker attempted network access")

    monkeypatch.setattr(io, "open", read_only)
    monkeypatch.setattr(socket, "socket", no_network)
    assert module.main([str(path)]) == 0
    assert len(reads) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "counts"


def test_module_invocation(record, tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record))
    checker()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "canon_trading.d3_check", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "counts"


@pytest.mark.parametrize(
    "result_text", ["running", "stopping: SIGTERM received; the window has not finished"]
)
@pytest.mark.parametrize(
    ("arm", "entered", "summary", "code"),
    [
        (None, True, "breaks", 1),
        (None, False, "breaks", 1),
        ("F2", True, "practice (finding: A6)", 5),
    ],
    ids=["default-arm", "default-arm-no-entry", "fixed-arm"],
)
def test_a_window_whose_record_never_closed_fails_flatness(
    record, result_text, arm, entered, summary, code
):
    """A record left `running` or `stopping` belongs to a window that died before its wind-down
    finished (`scripts/automation status` reads it as DIED: the account may not be flat), so A6 fails;
    it never reads as nothing to act on or as no entry."""
    record["result"] = result_text
    if arm is not None:
        record["arm"] = arm
    if not entered:
        for key in ("entries", "fills", "exits"):
            record["mover"]["run"][key] = []
        record["mover"]["result"]["by_instrument"] = {}
    result = checker().check(record)
    assert term(result, "A6").status == "failed"
    assert (result.summary, result.exit_code) == (summary, code)


@pytest.mark.parametrize(
    "reason",
    [
        # execution.py:3071 (`_rejected_takeover`): `_reject` -> OrderRejected -> Mover's refused.
        "Alpaca rejected the replacement order 0f3c2b1a-77de-4c8e-9a51-2b6e0e1d4c10",
    ],
)
def test_a_rejected_replacement_order_is_a_refused_replace(record, reason):
    record["mover"]["run"]["refused"] = [["buy-1", reason]]
    a7 = term(checker().check(record), "A7")
    assert a7.status == "failed"
    assert a7.first_failure["reason"] == "venue refusal: refused_replace"


def _sold_by_the_account_check(record: dict[str, Any], check_fills: Any) -> None:
    """A wind-down holding the strategy did not sell, closed by the account check after the stop."""
    run = record["mover"]["run"]
    run["fills"] = [run["fills"][0]]
    run["exits"][0].update(
        trigger_reason="wind-down", filled_quantity="0", final_fill_ns=None, sales=[]
    )
    record["closed_after_stop"] = True
    record["closes"] = {"check-1": ["sell 10 SYN market", "filled"]}
    if check_fills is not None:
        record["check_fills"] = check_fills
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "0"


def test_a_holding_the_account_check_sold_fails_flatness_not_its_exit_record(record):
    """The window's result counts the account check's closes (`trade_result` over `check_fills`), so
    its open shares are 0 while the exit row sold none: the exit record is right, and only A6, which
    names the account check's close, is breached."""
    _sold_by_the_account_check(record, [["check-1", "SYN.ALPA", "SELL", "10", "9.70"]])
    record["mover"]["result"]["check_closes"] = 1
    result = checker().check(record)
    assert term(result, "A5").status == "met", term(result, "A5").first_failure
    assert any("account check" in note for note in term(result, "A5").notes)
    assert term(result, "A6").status == "failed"


def test_open_shares_the_account_check_does_not_explain_still_fail_the_exit_record(record):
    _sold_by_the_account_check(record, [["check-1", "SYN.ALPA", "SELL", "4", "9.70"]])
    record["mover"]["result"]["check_closes"] = 1
    a5 = term(checker().check(record), "A5")
    assert a5.status == "failed"
    assert "6 shares" in a5.first_failure["reason"]


@pytest.mark.parametrize(
    "reason",
    [
        # trail.py:46, 66, 71, 74 and 112 at the base, raised through _entry_trail into _buy's
        # ValueError branch (mover.py:3457-3460), so into `dropped`.
        "profile time is outside the ranking clock's 96-segment part",
        "profile_k needs a finite registered Decimal",
        "profile_multipliers needs a registered tuple of Decimals",
        "profile_pctl must be one of ('p75', 'p90')",
        "profile segment must be an index from 0 to 95",
    ],
)
def test_each_profile_refusal_is_a_named_entry_drop(record, reason):
    run = record["mover"]["run"]
    del run["refusals"]
    run["dropped"] = [f"SYN.ALPA: {reason}"]
    assert term(checker().check(record), "A1").counts["named_refusals"] == 1


def test_check_fills_the_result_never_counted_are_not_subtracted(record):
    """The result counts the check's closes only when it is re-read after the check
    (`check_closes`); a check that raised after a fill leaves open_shares without them."""
    _sold_by_the_account_check(record, [["check-1", "SYN.ALPA", "SELL", "10", "9.70"]])
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "10"
    a5 = term(checker().check(record), "A5")
    assert a5.status == "met", a5.first_failure


@pytest.mark.parametrize(
    ("rows", "opened"),
    [
        ([["check-1", "OTHER.ALPA", "SELL", "5", "9.70"]], {"OTHER.ALPA": "-5", "SYN.ALPA": "10"}),
        ([["check-1", "SYN.ALPA", "SELL", "12", "9.70"]], {"SYN.ALPA": "-2"}),
        ([["check-1", "SYN.ALPA", "BUY", "5", "9.70"]], {"SYN.ALPA": "15"}),
    ],
    ids=["unseen-holding", "sold-past-the-remainder", "closed-a-short"],
)
def test_the_account_checks_net_closes_explain_open_shares(record, rows, opened):
    _sold_by_the_account_check(record, rows)
    record["mover"]["result"]["check_closes"] = len(rows)
    record["mover"]["result"]["by_instrument"].update(
        {k: {"open_shares": v} for k, v in opened.items()}
    )
    a5 = term(checker().check(record), "A5")
    assert a5.status == "met", a5.first_failure


@pytest.mark.parametrize("first", [True, False], ids=["bad-row-first", "bad-row-last"])
def test_an_unreadable_check_fill_leaves_the_remainder_not_shown(record, first):
    good = ["check-1", "SYN.ALPA", "SELL", "10", "9.70"]
    bad = ["check-2", "SYN.ALPA", "SELL", "n/a", "9.70"]
    _sold_by_the_account_check(record, [bad, good] if first else [good, bad])
    record["mover"]["result"]["check_closes"] = 2
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "-3"
    a5 = term(checker().check(record), "A5")
    assert a5.status == "not shown", a5.first_failure


@pytest.mark.parametrize(
    "reason",
    [
        "Alpaca rejected the replace (trade update) and more",
        "Alpaca rejected the replacement order abc, wrapped",
    ],
)
def test_a_longer_reason_is_not_a_refused_replace(record, reason):
    record["mover"]["run"]["refused"] = [["buy-1", reason]]
    a7 = term(checker().check(record), "A7")
    assert a7.first_failure is None


@pytest.mark.parametrize("missing", ["entry", "identity"])
def test_a_filled_buy_needs_an_identifiable_entry_plan(record, missing):
    run = record["mover"]["run"]
    if missing == "entry":
        run["entries"] = []
    else:
        del run["entries"][0]["client_order_id"]
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("incomplete", 2)
    assert term(result, "A1").status == "not shown"
    assert term(result, "A2").status == "not shown"


def test_a1_checks_the_floor_against_the_recorded_spread(record):
    run = record["mover"]["run"]
    run["rows"]["spread_floor_m"] = "2"
    run["entries"][0]["spread"] = "0.30"
    result = checker().check(record)
    assert term(result, "A1").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)


def test_a1_names_a_missing_floor_multiplier(record):
    del record["mover"]["run"]["rows"]["spread_floor_m"]
    a1 = term(checker().check(record), "A1")
    assert a1.status == "not shown"
    assert any("spread_floor_m" in path for path in a1.missing)


def test_a6_cannot_accept_a_flat_flag_over_a_nonzero_remainder(record):
    run = record["mover"]["run"]
    run["fills"][1][2] = "5"
    run["exits"][0].update(filled_quantity="5", final_fill_ns=None)
    record["mover"]["result"]["by_instrument"]["SYN.ALPA"]["open_shares"] = "5"
    result = checker().check(record)
    assert term(result, "A5").status == "met"
    assert term(result, "A6").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)


def test_a_quick_trigger_is_not_a_sale_inside_sixty_seconds(record):
    record["mover"]["run"]["exits"][0]["trigger_ns"] = 2_000_000_000
    result = checker().check(record)
    assert term(result, "A4").counts["inside_60s"] == 0


def test_an_unsold_exit_is_not_a_sale_inside_sixty_seconds(record):
    run = record["mover"]["run"]
    run["fills"] = [run["fills"][0]]
    run["exits"][0].update(
        trigger_ns=2_000_000_000, filled_quantity="0", final_fill_ns=None, sales=[]
    )
    assert term(checker().check(record), "A4").counts["inside_60s"] == 0


def test_a_truncated_sell_fill_makes_the_record_incomplete(record):
    record["mover"]["run"]["fills"][1] = ["sell-1", "SELL"]
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("incomplete", 2)
    assert term(result, "A5").status == "not shown"


def test_an_unrepresentable_json_number_has_an_unreadable_verdict(tmp_path, capsys):
    path = tmp_path / "record.json"
    path.write_text('{"x": 1e9999999999999999999}', encoding="utf-8")
    assert checker().main([str(path)]) == 4
    assert capsys.readouterr().out.startswith("unreadable:")


def test_a_fixed_arm_label_alone_is_not_successful_evidence():
    result = checker().check({"arm": "F2"})
    assert (result.summary, result.exit_code) == ("incomplete", 2)


@pytest.mark.parametrize("has_modify_event", [False, True])
def test_a_documented_http_replace_refusal_breaks_a7(record, has_modify_event):
    # execution.py:2338 at the base: the replace's HTTP refusal (422, `REPLACE_REFUSAL`).
    record["mover"]["run"]["order_refusals"] = [
        {
            "client_order_id": "sell-1",
            "by": "venue",
            "reason": "Alpaca refused the replace (422): 42210000: order is not replaceable",
        }
    ]
    if has_modify_event:
        record["orders"]["sell-1"].insert(0, "OrderModifyRejected")
    result = checker().check(record)
    assert term(result, "A7").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)


def _no_fills(record: dict[str, Any]) -> None:
    record["arm"] = "F2"
    for key in ("entries", "fills", "exits"):
        record["mover"]["run"][key] = []
    record["mover"]["result"]["by_instrument"] = {}


def test_a_fixed_arm_without_entry_and_with_its_close_shown_is_practice(record):
    _no_fills(record)
    result = checker().check(record)
    assert term(result, "A3").status == "not shown"  # nothing bought, so nothing sold to judge
    assert (result.summary, result.exit_code) == ("practice", 0)


def test_a_fixed_arm_without_entry_still_needs_its_close_shown(record):
    _no_fills(record)
    for key in ("ended", "left", "closes", "denials", "orders"):
        record.pop(key)
    del record["mover"]["result"]["flat"]
    record["result"] = "error: node crashed"
    result = checker().check(record)
    assert term(result, "A6").status == "not shown"
    assert (result.summary, result.exit_code) == ("incomplete", 2)


@pytest.mark.parametrize("arm", ["F2", "OWN1"])
def test_a_sale_with_no_buy_fill_is_not_a_window_without_entry(record, arm):
    record["arm"] = arm
    run = record["mover"]["run"]
    run["fills"] = [row for row in run["fills"] if row[1] == "SELL"]
    assert run["fills"]
    result = checker().check(record)
    assert (result.summary, result.exit_code) == ("incomplete", 2)


@pytest.mark.parametrize(
    "reason",
    ["Alpaca refused the replace (422): ", "Alpaca refused the replace (422): line one\nline two"],
)
def test_an_empty_or_multiline_http_replace_refusal_breaks_a7(record, reason):
    # execution.py:2338 joins the status and api_error_text unstripped; the text can be empty.
    record["mover"]["run"]["order_refusals"] = [
        {"client_order_id": "sell-1", "by": "venue", "reason": reason}
    ]
    result = checker().check(record)
    assert term(result, "A7").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)
