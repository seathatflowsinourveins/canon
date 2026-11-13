"""Synthetic records for the D3 acceptance terms (S5, A1-R).

Contracts: the record/change/term fixture pattern in test_d3_check.py; pytest's
parametrization contract at pytest@ae1d0ff57b8b, src/_pytest/python.py:1307.
Writer evidence at edec08cd: mover.py:1912-1990,3268-3277,3599-3649,3843-3990;
session_window.py:1074-1141,1709-1741,3226-3236,3357-3389,4563-4583.
The price parity oracle is price_step.tick and sizing.sized_stop, used only here.
"""

import ast
import copy
import json
import sys
from decimal import ROUND_FLOOR
from decimal import Decimal
from itertools import pairwise
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from canon_trading import d3_check
from canon_trading import price_step
from canon_trading.sizing import sized_stop

type Record = dict[str, Any]

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/canon_trading/d3_check.py",
    "tests/test_d3_check.py",
    "tests/test_d3_check_round2_blind.py",
)
FORBIDDEN = (
    "/" + "Users/",  # split, so this list is not itself a home path
    "deputy",
    "dispatch",
    "specs/",
    "L5",
    "R9",
    "batch B",
    "TODO(",
    "ruling_p",
    "test_p1_",
    "test_p2_",
    "test_p3_",
    "test_p4_",
    "test_p5_",
    "test_p6_",
    "test_p7_",
    "test_p8_",
    "test_p9_",
    "test_p10_",
    "pre_boundary",
)
BASE = "c3e1a51ee7c7cc2c473bda1db53a4f48b735652d"
UNLISTED_COMMIT = "edec08cd98b1100ec9ef98df332633eac7a0d2c6"
RUN: list[str | int] = ["mover", "run"]
ENTRY = [*RUN, "entries", 0]
EXIT = [*RUN, "exits", 0]
WASH_TRADE = "potential wash trade detected"


@pytest.fixture
def record() -> Record:
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
                "rows": {"trail": "0.02"},
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


def change(record: Record, path: list[str | int], value: Any) -> None:
    parent: Any = record
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def term(result: d3_check.Check, name: str) -> d3_check.Term:
    return next(item for item in result.terms if item.name == name)


def no_entry(record: Record, *, sent: bool) -> None:
    run = record["mover"]["run"]
    if not sent:
        run["entries"] = []
    run["fills"] = []
    run["exits"] = []
    record["mover"]["result"]["by_instrument"] = {}
    record["orders"] = {"buy-1": ["OrderCanceled"]} if sent else {}


def venue_refusal(cid: str, reason: Any = WASH_TRADE) -> Record:
    return {"client_order_id": cid, "by": "venue", "cause": "other", "reason": reason}


def cross_dollar(record: Record) -> None:
    run = record["mover"]["run"]
    entry = run["entries"][0]
    entry.update(trail="0.015", spread="0.005", spread_floor="0.015")
    entry["sizing"].update(
        planned_loss="1.52",
        trail="0.015",
        stop="0.9948",
        loss_per_share="0.01",
        quantity="100",
        notional="101",
    )
    run["fills"][0][2:4] = ["100", "1.01"]
    run["fills"][1][2:4] = ["100", "1.00"]
    exit_row = run["exits"][0]
    exit_row.update(quantity="100", filled_quantity="100", trigger_price="1.00")
    exit_row["sales"][0].update(
        trailing_offset="0.01",
        limit_offset="0.015",
        distance="0.01",
        kept_trigger="1.00",
        spread="0.005",
    )


def test_only_buy_refused_for_wash_trade_breaks(
    record: Record, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    no_entry(record, sent=True)
    record["mover"]["run"]["order_refusals"] = [venue_refusal("buy-1")]
    record["orders"] = {"buy-1": ["OrderRejected"]}
    result = d3_check.check(record)
    assert term(result, "A7").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)
    path = tmp_path / "refused.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert d3_check.main([str(path)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "breaks"


@pytest.mark.parametrize("failure", ["cleanup", "not-flat", "failed-record"])
def test_failed_a6_without_entry_breaks(record: Record, failure: str) -> None:
    no_entry(record, sent=False)
    if failure == "cleanup":
        record["closed_after_stop"] = True
    elif failure == "not-flat":
        record["mover"]["result"]["flat"] = False
        record["left"] = ["SYN"]
    else:
        record["result"] = "failed: interrupted"
    result = d3_check.check(record)
    assert term(result, "A6").status == "failed"
    assert (result.summary, result.exit_code) == ("breaks", 1)


@pytest.mark.parametrize("sent", [False, True], ids=["no-order", "unfilled-order"])
def test_no_failed_term_stays_no_entry(record: Record, sent: bool) -> None:
    no_entry(record, sent=sent)
    result = d3_check.check(record)
    assert all(item.status != "failed" for item in result.terms)
    assert (result.summary, result.exit_code) == ("no entry", 3)


@pytest.mark.parametrize("legacy_fields", [True, False], ids=["legacy-notes", "entries-only"])
def test_unlisted_commit_without_refusal_rows_judges_entries(
    record: Record, legacy_fields: bool
) -> None:
    record["commit"] = UNLISTED_COMMIT
    run = record["mover"]["run"]
    del run["refusals"]
    if legacy_fields:
        run["dropped"] = [
            "SYN.ALPA: the spread floor exceeds the 0.02 cap; no entry satisfies both"
        ]
        run["sizing_refusals"] = {"distance_over_ceiling": 1}
    else:
        del run["dropped"]
        del run["sizing_refusals"]
    result = d3_check.check(record)
    a1 = term(result, "A1")
    assert a1.status == "met"
    assert a1.counts["named_refusals"] == (1 if legacy_fields else 0)
    assert any("no refusal rows" in note.lower() for note in a1.notes)
    if legacy_fields:
        assert any("dropped" in path for path in a1.paths)
    else:
        assert not any("dropped" in path or "sizing_refusals" in path for path in a1.paths)
    assert (result.summary, result.exit_code) == ("counts", 0)


def test_absent_refusal_rows_do_not_excuse_an_entry_below_the_floor(record: Record) -> None:
    record["commit"] = UNLISTED_COMMIT
    del record["mover"]["run"]["refusals"]
    change(record, [*ENTRY, "spread_floor"], "0.03")
    assert term(d3_check.check(record), "A1").status == "failed"


@pytest.mark.parametrize("failed_term", [None, "A2", "A3", "A5", "A6", "A7"])
def test_fixed_arm_findings_are_named_and_return_a_distinct_nonzero_code(
    record: Record,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failed_term: str | None,
) -> None:
    """Practice names every failed shared term and signals a finding without a revert."""
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread_floor=None, trail_bound="fixed", trail_source="fixed")
    if failed_term == "A2":
        entry["sizing"]["planned_loss"] = "1.00"
    elif failed_term == "A3":
        change(record, [*EXIT, "sales", 0, "trailing_offset"], "0.10")
    elif failed_term == "A5":
        change(record, [*EXIT, "trigger_reason"], "")
    elif failed_term == "A6":
        record["closed_after_stop"] = True
    elif failed_term == "A7":
        record["mover"]["run"]["order_refusals"] = [venue_refusal("other-buy")]
    result = d3_check.check(record)
    a1 = term(result, "A1")
    assert a1.status == "not judged"
    assert a1.counts["fixed_arm_entries"] == 1
    assert a1.counts.get("fixed_plan_count", 0) == 0
    assert result.lines()[0].startswith("A1: not judged (fixed arm) | ")
    assert term(result, "A4").status == "not judged"
    assert result.lines()[3].startswith("A4: not judged (fixed arm) | ")
    for name in ("A2", "A3", "A5", "A6", "A7"):
        assert term(result, name).status == ("failed" if name == failed_term else "met")
    if failed_term is not None:
        assert any(
            line.startswith(f"{failed_term}: failed (fixed arm) | ") for line in result.lines()
        )
    summary = "practice" if failed_term is None else f"practice (finding: {failed_term})"
    code = 0 if failed_term is None else 5
    assert (result.summary, result.exit_code) == (summary, code)
    path = tmp_path / "fixed.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert d3_check.main([str(path)]) == code
    assert capsys.readouterr().out.splitlines()[-1] == summary


@pytest.mark.parametrize("changed", ["trail_source", "trail_bound"])
def test_partial_fixed_label_does_not_hide_missing_floor(record: Record, changed: str) -> None:
    """The fixed-arm exemption needs a fixed arm, not one partial entry label."""
    entry = record["mover"]["run"]["entries"][0]
    entry["spread_floor"] = None
    entry[changed] = "fixed"
    result = d3_check.check(record)
    assert term(result, "A1").status == "not shown"
    assert term(result, "A4").status == "counted"
    assert result.summary == "incomplete"


def test_unusable_spread_fixed_plan_still_counts(record: Record) -> None:
    """A1 and A4 still apply to a per-symbol arm's fixed fallback."""
    record["arm"] = "OWN1"
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread=None, spread_floor=None, trail_bound="fixed", trail_source="fixed")
    result = d3_check.check(record)
    assert term(result, "A1").counts["fixed_plan_count"] == 1
    assert term(result, "A1").counts.get("fixed_arm_entries", 0) == 0
    assert term(result, "A4").status == "counted"
    assert result.lines()[0].startswith("A1: met (no usable spread: fixed plan) | ")
    assert (result.summary, result.exit_code) == ("counts", 0)


@pytest.mark.parametrize("reason", ["trailing stop", "wind-down"])
def test_quick_fixed_plan_sale_is_counted_without_a_floor(record: Record, reason: str) -> None:
    entry = record["mover"]["run"]["entries"][0]
    entry.update(spread=None, spread_floor=None, trail_bound="fixed", trail_source="fixed")
    change(record, [*EXIT, "trigger_reason"], reason)
    change(record, [*EXIT, "trigger_ns"], 3_000_000_000)
    change(record, [*EXIT, "final_fill_ns"], 4_000_000_000)
    change(record, [*RUN, "fills", 1, 4], 4_000_000_000)
    result = d3_check.check(record)
    assert term(result, "A1").status == "met"
    a4 = term(result, "A4")
    assert a4.counts["inside_60s"] == 1
    assert a4.status == "counted"
    assert (result.summary, result.exit_code) == ("counts", 0)


def test_cross_dollar_entry_allows_one_tick_of_rounding(record: Record) -> None:
    cross_dollar(record)
    result = d3_check.check(record)
    assert term(result, "A2").status == "met"
    assert term(result, "A1").status == "met"
    assert (result.summary, result.exit_code) == ("counts", 0)


def test_more_than_one_tick_below_floor_still_fails(record: Record) -> None:
    cross_dollar(record)
    entry = record["mover"]["run"]["entries"][0]
    entry.update(trail="0.025", spread_floor="0.025")
    entry["sizing"].update(trail="0.025", stop="0.9847", planned_loss="2.53")
    result = d3_check.check(record)
    a1 = term(result, "A1")
    assert a1.status == "failed"
    assert a1.first_failure is not None
    assert "loss_per_share" in a1.first_failure["path"]
    assert (result.summary, result.exit_code) == ("breaks", 1)


def test_tolerance_does_not_excuse_fractional_trail_below_floor(record: Record) -> None:
    cross_dollar(record)
    change(record, [*ENTRY, "trail"], "0.014")
    change(record, [*ENTRY, "sizing", "trail"], "0.014")
    assert term(d3_check.check(record), "A1").status == "failed"


@pytest.mark.parametrize("reason", ["insufficient buying power", "market closed", "halted"])
def test_readable_other_venue_refusal_is_an_observation(record: Record, reason: str) -> None:
    record["mover"]["run"]["order_refusals"] = [venue_refusal("other-buy", reason)]
    result = d3_check.check(record)
    a7 = term(result, "A7")
    assert a7.status == "met"
    assert any(reason in note for note in a7.notes)
    assert (result.summary, result.exit_code) == ("counts", 0)


@pytest.mark.parametrize(
    "reason", [None, "", "   ", 7, {}], ids=["null", "empty", "space", "int", "map"]
)
def test_unreadable_refusal_reason_stays_not_shown(record: Record, reason: Any) -> None:
    record["mover"]["run"]["order_refusals"] = [venue_refusal("other-buy", reason)]
    result = d3_check.check(record)
    assert term(result, "A7").status == "not shown"
    assert (result.summary, result.exit_code) == ("incomplete", 2)


def test_missing_refusal_reason_stays_not_shown(record: Record) -> None:
    row = venue_refusal("other-buy")
    del row["reason"]
    record["mover"]["run"]["order_refusals"] = [row]
    assert term(d3_check.check(record), "A7").status == "not shown"


def test_empty_structured_refusals_do_not_mask_wash_trade_denial(record: Record) -> None:
    record["denials"] = {"other-buy": WASH_TRADE}
    record["orders"]["other-buy"] = ["OrderRejected"]
    result = d3_check.check(record)
    assert term(result, "A7").status == "failed"
    assert any("denials" in path for path in term(result, "A7").paths)
    assert (result.summary, result.exit_code) == ("breaks", 1)


@pytest.mark.parametrize(
    "event,status,summary,code",
    [
        ("OrderRejected", "not shown", "incomplete", 2),
        ("OrderModifyRejected", "not shown", "incomplete", 2),
        ("OrderCancelRejected", "met", "counts", 0),
    ],
)
def test_structured_list_does_not_mask_reject_event_without_reason(
    record: Record, event: str, status: str, summary: str, code: int
) -> None:
    record["orders"]["other-order"] = [event]
    result = d3_check.check(record)
    a7 = term(result, "A7")
    assert a7.status == status
    assert any("orders" in path for path in term(result, "A7").paths)
    if event == "OrderModifyRejected":
        assert a7.first_failure is None
        assert any("no modify reason" in note for note in a7.notes)
    elif event == "OrderCancelRejected":
        assert any(event in note and "outside A7" in note for note in a7.notes)
        assert all(count == 0 for count in a7.counts.values())
    assert (result.summary, result.exit_code) == (summary, code)


def test_same_client_order_id_is_counted_once_across_sources(record: Record) -> None:
    record["mover"]["run"]["order_refusals"] = [venue_refusal("other-buy")]
    once = term(d3_check.check(record), "A7")
    repeated = copy.deepcopy(record)
    repeated["denials"] = {"other-buy": WASH_TRADE}
    repeated["orders"]["other-buy"] = ["OrderRejected"]
    duplicate = term(d3_check.check(repeated), "A7")
    assert once.status == duplicate.status == "failed"
    # Counter names are not prescribed; the nonzero count must describe one order in both records.
    assert 1 in duplicate.counts.values(), duplicate.counts
    assert duplicate.counts == once.counts


def test_distinct_client_order_ids_are_counted_separately(record: Record) -> None:
    record["mover"]["run"]["order_refusals"] = [venue_refusal("other-buy-1")]
    record["denials"] = {"other-buy-2": WASH_TRADE}
    record["orders"]["other-buy-1"] = ["OrderRejected"]
    record["orders"]["other-buy-2"] = ["OrderRejected"]
    result = d3_check.check(record)
    a7 = term(result, "A7")
    assert a7.status == "failed"
    assert 2 in a7.counts.values(), a7.counts


def test_sold_wind_down_accepts_null_trigger_price(record: Record) -> None:
    change(record, [*EXIT, "trigger_reason"], "wind-down")
    change(record, [*EXIT, "trigger_price"], None)
    result = d3_check.check(record)
    assert term(result, "A5").status == "met"
    assert (result.summary, result.exit_code) == ("counts", 0)


@pytest.mark.parametrize(
    "reason,status,summary,code",
    [("trailing stop", "not shown", "incomplete", 2), ("unpriced_exit", "met", "counts", 0)],
)
def test_null_trigger_price_requires_a_named_unpriced_exit(
    record: Record, reason: str, status: str, summary: str, code: int
) -> None:
    change(record, [*EXIT, "trigger_reason"], reason)
    change(record, [*EXIT, "trigger_price"], None)
    result = d3_check.check(record)
    assert term(result, "A5").status == status
    assert (result.summary, result.exit_code) == (summary, code)


def test_null_wind_down_price_does_not_skip_final_fill_evidence(record: Record) -> None:
    change(record, [*EXIT, "trigger_reason"], "wind-down")
    change(record, [*EXIT, "trigger_price"], None)
    change(record, [*EXIT, "final_fill_ns"], None)
    result = d3_check.check(record)
    a5 = term(result, "A5")
    assert a5.status == "failed"
    assert a5.first_failure is not None
    assert "final_fill_ns" in a5.first_failure["path"]


@pytest.mark.parametrize("source", SOURCES)
def test_source_has_no_private_paths_or_coordination_words(source: str) -> None:
    text = (ROOT / source).read_text(encoding="utf-8")
    if source == "tests/test_d3_check_round2_blind.py":
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "FORBIDDEN" for target in node.targets
            ):
                literal = ast.get_source_segment(text, node.value)
                assert literal is not None
                text = text.replace(literal, "", 1)
                break
    found = [word for word in FORBIDDEN if word in text]
    assert found == [], f"{source}: forbidden text {found}"


@pytest.mark.parametrize("source", SOURCES)
def test_terms_have_portable_citation(source: str) -> None:
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    assert "the D3 acceptance terms (S5, A1-R)" in (ast.get_docstring(tree) or "")


def test_no_statements_follow_an_always_raising_unknown_call() -> None:
    tree = ast.parse((ROOT / SOURCES[0]).read_text(encoding="utf-8"))
    unreachable: list[int] = []
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            for current, following in pairwise(value):
                if (
                    isinstance(current, ast.Expr)
                    and isinstance(current.value, ast.Call)
                    and isinstance(current.value.func, ast.Attribute)
                    and current.value.func.attr == "unknown"
                    and isinstance(following, ast.stmt)
                ):
                    unreachable.append(following.lineno)
    assert unreachable == [], f"statements after an always-raising call: {unreachable}"


def test_no_unreachable_assertion_placeholder() -> None:
    tree = ast.parse((ROOT / SOURCES[0]).read_text(encoding="utf-8"))
    found = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "AssertionError"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "unreachable" for arg in node.exc.args
        )
    ]
    assert found == [], f"unreachable assertion placeholders: {found}"


def test_checker_imports_only_standard_library_modules() -> None:
    tree = ast.parse((ROOT / SOURCES[0]).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import on line {node.lineno}"
            assert node.module is not None
            assert node.module.split(".")[0] in sys.stdlib_module_names
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names


PRICES = tuple(
    map(
        Decimal,
        (
            "0.0001",
            "0.0002",
            "0.001",
            "0.01",
            "0.0999",
            "0.10",
            "0.1234",
            "0.9998",
            "0.9999",
            "1.00",
            "1.01",
            "1.02",
            "1.10",
            "10.29",
            "500",
        ),
    )
)
DISTANCES = tuple(
    map(Decimal, ("0.0001", "0.005", "0.015", "0.02", "0.03333333333333333333333333333", "0.10"))
)


def test_tick_matches_price_step_on_price_grid() -> None:
    for price in PRICES:
        assert d3_check._tick(price) == price_step.tick(price), f"price={price}"


def test_a2_stop_rounding_matches_sized_stop_on_grid(record: Record) -> None:
    for price, distance in product(PRICES, DISTANCES):
        expected_stop = sized_stop(price, distance)
        loss = price - expected_stop
        opening_distance = loss.quantize(price_step.tick(price), rounding=ROUND_FLOOR)
        fixture = copy.deepcopy(record)
        entry = fixture["mover"]["run"]["entries"][0]
        entry["trail"] = str(distance)
        sizing = entry["sizing"]
        sizing.update(
            trail=str(distance),
            stop=str(expected_stop),
            loss_per_share=str(opening_distance),
            quantity="10",
            notional=str(price * 10),
            planned_loss=str(loss * 10),
            budget_notional=str(price * 1000),
            flow_shares="100",
        )
        a2 = term(d3_check.check(fixture), "A2")
        context = f"price={price}, distance={distance}, stop={expected_stop}"
        if expected_stop <= 0 or loss > price * Decimal("0.10") or opening_distance <= 0:
            # Such a row cannot be an accepted writer plan: zero stop, ceiling, or no usable L.
            assert a2.status == "failed", context
            continue
        assert a2.status == "met", (context, a2.first_failure, a2.notes)
        # An adjacent lower step must not be accepted as the plan's exact stop either.
        wrong_stop = price_step.stop_trigger_price(expected_stop - price_step.tick(expected_stop))
        sizing["stop"] = str(wrong_stop)
        sizing["loss_per_share"] = str(
            (price - wrong_stop).quantize(price_step.tick(price), rounding=ROUND_FLOOR)
        )
        rejected = term(d3_check.check(fixture), "A2")
        assert rejected.status == "failed", context
        assert rejected.first_failure is not None, context
        assert '"stop"' in rejected.first_failure["path"], (context, rejected.first_failure)
