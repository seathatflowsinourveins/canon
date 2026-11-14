"""Cases generated from sale_table.json, the pinned plain-limit sale contract.

Cell/branch expectations and histories are data, not a second transition table.
All writes (the relocated-package check) use pytest's temporary directory.
"""

import ast
import hashlib
import importlib.util
import itertools
import json
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader import model

from canon_trading import sale_table as sale

DATA_PATH = Path(__file__).resolve().parents[1] / "src/canon_trading/sale_table.json"
DATA = json.loads(DATA_PATH.read_text())
HISTORIES = json.loads(Path(__file__).with_name("sale_table_histories.json").read_text())
CELLS_HASH = "046ef1f03b02491e885a24f5c1380de813b84db48f69437971888e46e7835eee"
CELLS = [(r, e, c) for r, row in DATA["cells"].items() for e, c in row.items()]
BRANCHES = [(r, e, b) for r, e, c in CELLS for b in c["branches"]]


def names(when):
    return set(when.split()) - {"always", "and", "not"}


def holds(when, facts):
    if when == "always":
        return True
    return all(
        facts[term.removeprefix("not ")] == (not term.startswith("not "))
        for term in when.split(" and ")
    )


def assignments(cell):
    predicates = sorted(set().union(*(names(b["when"]) for b in cell["branches"])))
    return [
        dict(zip(predicates, values))
        for values in itertools.product((False, True), repeat=len(predicates))
    ]


def inputs(row, facts):
    count = int(facts.get("count_complete", True)) if row == "G" else None
    return sale.State(row, count), {k: v for k, v in facts.items() if k != "count_complete"}


def expected_count(state, branch):
    if branch["next"] != "G":
        return None
    match branch.get("count", "="):
        case "0":
            return 0
        case "+1":
            return state.count + 1
        case "=":
            return state.count
    raise AssertionError(branch)


@pytest.mark.parametrize(
    "row,event,branch", BRANCHES, ids=[f"{r}-{e}-{b['when']}" for r, e, b in BRANCHES]
)
def test_each_generated_branch(row, event, branch):
    cell = DATA["cells"][row][event]
    facts = next(f for f in assignments(cell) if holds(branch["when"], f))
    state, supplied = inputs(row, facts)
    result = sale.transition(state, 2, event, supplied)
    assert result.state == sale.State(branch["next"], expected_count(state, branch))
    assert result.actions == tuple(
        action
        for key, action in (
            ("send", sale.Action.SEND),
            ("cancel", sale.Action.CANCEL),
            ("alert", sale.Action.ALERT),
        )
        if branch[key]
    )
    assert result.when == branch["when"]


@pytest.mark.parametrize("row,event,cell", CELLS, ids=[f"{r}-{e}" for r, e, _ in CELLS])
def test_every_assignment_selects_exactly_one_branch(row, event, cell):
    for facts in assignments(cell):
        matches = [b for b in cell["branches"] if holds(b["when"], facts)]
        assert len(matches) == 1, (row, event, facts, matches)
        state, supplied = inputs(row, facts)
        assert sale.transition(state, 2, event, supplied).when == matches[0]["when"]


def test_tracked_table_has_exact_contract_and_cells():
    assert set(DATA) == {
        "pin",
        "pin_paths",
        "applies",
        "rows",
        "events",
        "predicates",
        "step_order",
        "order_event_inventory",
        "properties_for_step_d",
        "sending_transitions",
        "price_rule",
        "freeze_causes",
        "cells",
    }
    encoded = json.dumps(DATA["cells"], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == CELLS_HASH
    assert len(DATA["rows"]) == 20
    assert len(DATA["events"]) == 28
    assert len(CELLS) == 560
    assert set(sale.TABLE.rows) == set(DATA["rows"])
    assert set(sale.TABLE.events) == set(DATA["events"])


def test_module_header_names_current_cells():
    assert sale.__doc__ is not None and CELLS_HASH in sale.__doc__


@pytest.mark.parametrize(
    "stepped,level,previous,floor,expected",
    [
        (11, 10, None, None, 10),
        (9, 10, 8, None, 8),
        (7, 10, None, 8, 8),
        (7, 10, None, 11, 10),
        (7, 10, 8, 9, 8),
        (None, 10, 8, 9, 8),
        (None, 10, None, 9, None),
    ],
)
def test_leg_limit_caps_floor_and_no_quote(stepped, level, previous, floor, expected):
    assert (
        sale.leg_limit(
            None if stepped is None else Decimal(stepped),
            level=Decimal(level),
            previous=None if previous is None else Decimal(previous),
            floor=None if floor is None else Decimal(floor),
        )
        == expected
    )


def test_seventh_form_properties_are_in_the_contract():
    assert {
        "every_leg_steps_down_and_stays_under_the_level",
        "every_leg_is_the_whole_uncovered_quantity",
        "no_leg_rests_above_a_falling_market_in_silence",
        "a_leg_and_its_floor_agree",
        "order_events_are_judged_in_a_step",
        "an_unanswered_cancel_is_told_once",
        "the_holdings_end_cancels_a_working_sale",
        "no_timer_transition",
    } <= set(DATA["properties_for_step_d"])


def test_silence_predicate_is_used_only_by_waiting_rows_and_quote_or_timer():
    waiting = {"FL", "PCR", "PCO", "PCX", "PCT", "FZP", "FCP"}
    for row, event, cell in CELLS:
        reads = any("flight_alert_due" in names(b["when"]) for b in cell["branches"])
        assert reads == (row in waiting and event in {"QU", "QA", "NQ"})
        if reads:
            assert {b["when"] for b in cell["branches"]} == {
                "flight_alert_due",
                "not flight_alert_due",
            }


@pytest.mark.parametrize("row", ["WK", "PF"])
@pytest.mark.parametrize("event", ["HE", "oLF"])
def test_holdings_end_cancels_the_working_sale_in_that_step(row, event):
    result = sale.transition(sale.State(row), 2, event, {"long_remains": False})
    assert result.state == sale.State("PCX")
    assert sale.Action.CANCEL in result.actions


def test_resting_rows_without_a_chase_are_entered_with_an_alert():
    resting = {"FZW", "FCW", "TW", "OX"}
    for row, _, branch in BRANCHES:
        if row not in resting and branch["next"] in resting:
            # One exception, by name: a cancel refused AGAIN for a leg whose refused cancel was
            # already told returns to its resting row in silence (said once per leg).
            told = "cancel_refusal_told" in branch["when"].split(" and ")
            assert branch["alert"] != told, (row, branch)
            assert not told or (row, branch["next"]) in {("PCX", "OX"), ("PCT", "TW")}


def test_quantity_and_cover_properties_are_in_the_contract():
    assert {
        "no_leg_of_size_zero",
        "nothing_for_a_whole_cover",
        "an_outside_freeze_ends_when_its_cause_ends",
        "never_more_working_than_held_whatever_the_cover",
    } <= set(DATA["properties_for_step_d"])


FAULT = {"FZ", "FZW", "FZP"}
COVER = {"FC", "FCW", "FCP"}


def test_every_freeze_cause_is_listed_and_classified():
    actual = set()
    for row, event, cell in CELLS:
        if cell["occurs"] in {"no", "n/a"}:
            continue
        for branch in cell["branches"]:
            into = branch["next"]
            family = FAULT if into in FAULT else COVER if into in COVER else set()
            if family and row not in family:
                actual.add((row, event, into))
    listed = set()
    for cause in DATA["freeze_causes"]:
        assert cause["cause"] and "UNCLASSIFIED" not in cause["cause"]
        if cause["into"] in COVER:
            assert cause["event"] == "FRZ" and cause["cause"].startswith("outside:")
        elif cause["event"] == "oVOID":
            assert "HELD" in cause["cause"]
        else:
            # RL: a hold's release never lifts a fault recorded for the holding (T0 x RL).
            assert cause["event"] in {"oDEN", "oOTH", "oINI", "oPCN", "RL"}
            assert cause["cause"].startswith("ours:")
        listed.update((r, cause["event"], cause["into"]) for r in cause["from"])
    assert actual == listed


def test_freeze_cells_obey_ruling_seventeen():
    live = {
        "G": "FC",
        "GC": "FC",
        "RW": "FC",
        "FL": "FCW",
        "WK": "FCW",
        "PF": "FCW",
        "PCR": "FCP",
        "PCO": "FCP",
    }
    for row, event, branch in BRANCHES:
        if event == "FRZ":
            assert branch["next"] not in FAULT or row in FAULT
            if row in live:
                assert branch["next"] == live[row] and branch["alert"]
        if row in FAULT:
            assert branch["next"] in FAULT | {"X", "T0", "OX", "PCX", "PCT", "TW"}
            assert DATA["cells"][row]["UFZ"]["occurs"] == "n/a"
        if row in FAULT | COVER:
            assert not branch["send"]
        if "not formable" in branch["when"]:
            assert branch["next"] not in FAULT | COVER and not branch["send"]
            assert branch["alert"] == ("not refusal_told" in branch["when"])
        if event == "UFZ":
            assert (DATA["cells"][row][event]["occurs"] == "yes") == (row in COVER)
            assert not (branch["send"] or branch["cancel"] or branch["alert"])


@pytest.mark.parametrize("row", sorted(COVER))
def test_cover_reads_again_restores_only_the_cover_or_leg_state(row):
    cell = DATA["cells"][row]["UFZ"]
    for facts in assignments(cell):
        expected = (
            ("GC" if facts["whole_cover"] else "G")
            if row == "FC"
            else ("FL" if not facts["cancelable"] else "PF" if facts["filled_part"] else "WK")
            if row == "FCW"
            else "FCP"
        )
        result = sale.transition(sale.State(row), 2, "UFZ", facts)
        assert result.state == sale.State(expected, 0 if expected == "G" else None)
        assert not result.actions


def test_refusal_and_silence_properties_are_in_the_contract():
    assert {
        "one_alert_per_holding_until_accepted",
        "resend_no_sooner_than_the_interval",
        "resend_never_more_than_held",
        "a_silent_leg_is_told_once_and_never_resent",
    } <= set(DATA["properties_for_step_d"])


def test_refusal_cells_and_waiting_row_obey_ruling_fifteen():
    for row, event, branch in BRANCHES:
        if branch["next"] == "RW" and row != "RW":
            assert (event == "oREJ" and row in {"FL", "WK", "PF", "PCR"}) or (
                (row, event) == ("PCR", "oCXL") and "not formable" in branch["when"]
            )
        if row in {"FL", "WK", "PF", "PCR"} and event == "oREJ":
            assert branch["next"] == "RW"
            assert branch["alert"] == (branch["when"] == "not refusal_told")
        if row == "RW" and branch["alert"]:
            assert branch["next"] in {"FZ", "FC"} or (
                branch["next"] == "RW" and "not formable" in branch["when"]
            )
        if row in {"RW", "G"} and branch["send"]:
            assert event == "QU" and "resend_due" in branch["when"].split(" and ")
        if row == "RW" and event in {"QA", "NQ"}:
            assert not branch["send"]


@pytest.mark.parametrize("row", ["FL", "PCR", "PCO", "PCX", "PCT", "FZP", "FCP"])
@pytest.mark.parametrize("event", ["QU", "QA", "NQ"])
def test_silent_flight_alert_is_the_only_change(row, event):
    for due in (False, True):
        result = sale.transition(sale.State(row), 2, event, {"flight_alert_due": due})
        assert result.state == sale.State(row)
        assert result.actions == ((sale.Action.ALERT,) if due else ())


def test_complete_count_waits_for_interval_without_counting_past_n():
    facts = {
        "resend_due": False,
        "leg_positive": True,
        "whole_cover": False,
        "formable": True,
        "refusal_told": False,
    }
    for n in (2, 3):
        result = sale.transition(sale.State("G", n - 1), n, "QU", facts)
        assert result.state == sale.State("G", n - 1) and not result.actions


def test_first_send_branches_use_the_modules_count():
    cell = DATA["cells"]["G"]["QU"]
    # A complete count stands while the refusal interval has not passed.
    assert len(cell["branches"]) == 7
    assert set().union(*(names(b["when"]) for b in cell["branches"])) == {
        "count_complete",
        "leg_positive",
        "formable",
        "whole_cover",
        "resend_due",
        "refusal_told",
    }
    for n in (2, 3):
        for count in range(n):
            for facts in assignments(cell):
                facts["count_complete"] = count == n - 1
                branch = next(b for b in cell["branches"] if holds(b["when"], facts))
                supplied = {k: v for k, v in facts.items() if k != "count_complete"}
                result = sale.transition(sale.State("G", count), n, "QU", supplied)
                assert result.when == branch["when"]
                assert result.state == sale.State(
                    branch["next"], expected_count(sale.State("G", count), branch)
                )
                if count == n - 1 and supplied["resend_due"] and not supplied["leg_positive"]:
                    assert result.actions == ()
                    assert result.state.row == ("GC" if supplied["whole_cover"] else "G")


@pytest.mark.parametrize("row", ["G", "GC"])
@pytest.mark.parametrize("event", ["HS", "HG", "CP", "CL", "oLF"])
def test_size_and_cover_events_reread_cover(row, event):
    branches = DATA["cells"][row][event]["branches"]
    nexts = {b["next"]: b["when"].split(" and ") for b in branches}
    assert "whole_cover" in nexts.get("GC", ())
    assert "not whole_cover" in nexts.get("G", ())


@pytest.mark.parametrize("event", ["oACC", "oPF", "MO"])
def test_stopped_open_sale_asks_the_owed_cancel(event):
    result = sale.transition(sale.State("TW"), 2, event, {"cancel_due": True})
    assert result.state == sale.State("PCT")
    assert result.actions == (sale.Action.CANCEL,)


def test_data_loads_beside_relocated_module(tmp_path, monkeypatch):
    """The existing uv_build backend includes all files under the package root."""
    source = Path(sale.__file__)
    target = tmp_path / "sale_table.py"
    target.write_bytes(source.read_bytes())
    target.with_suffix(".json").write_bytes(DATA_PATH.read_bytes())
    spec = importlib.util.spec_from_file_location("relocated_sale_table", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.transition(module.State("G", 0), 2, "NQ", {}).state.count == 0


def test_module_imports_only_pure_standard_library_dependencies():
    tree = ast.parse(Path(sale.__file__).read_text())
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "decimal",
        "enum",
        "json",
        "pathlib",
        "types",
        "typing",
    }
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add((node.module or "").split(".")[0])
    assert imports <= allowed
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"eval", "exec", "__import__"}
        for node in ast.walk(tree)
    )


def test_transition_and_step_do_not_read_resources(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("transition read a resource")

    monkeypatch.setattr(Path, "read_text", refused)
    facts = {
        "formable": True,
        "leg_positive": True,
        "whole_cover": False,
        "resend_due": True,
        "refusal_told": False,
    }
    state = sale.State("G", 0)
    events = (sale.Event("QU", facts), sale.Event("oOTH", {}))
    before = deepcopy(facts)
    assert sale.apply_step(state, 2, events) == sale.apply_step(state, 2, events)
    assert facts == before


def test_exactly_three_sending_transitions_into_flight():
    actual = [{"row": r, "event": e, "when": b["when"]} for r, e, b in BRANCHES if b["send"]]
    assert actual == DATA["sending_transitions"]
    assert [(s["row"], s["event"]) for s in actual] == [
        ("G", "QU"),
        ("RW", "QU"),
        ("PCR", "oCXL"),
    ]
    assert all(b["next"] == "FL" for _, _, b in BRANCHES if b["send"])
    assert all("leg_positive" in b["when"].split(" and ") for _, _, b in BRANCHES if b["send"])


@pytest.mark.parametrize("row", DATA["rows"])
def test_timer_event_changes_no_row_and_only_alerts_silence(row):
    state = sale.State(row, 1 if row == "G" else None)
    for due in (False, True):
        result = sale.apply_step(state, 2, [sale.Event("NQ", {"flight_alert_due": due})])
        assert result.state == state
        silent = row == "FL" or DATA["rows"][row]["phase"] == "pc"
        assert result.actions == ((sale.Action.ALERT,) if due and silent else ())
    assert all(
        b["next"] == row and not (b["send"] or b["cancel"])
        for b in DATA["cells"][row]["NQ"]["branches"]
    )


def test_open_sale_entered_only_by_first_send_or_resend():
    for row, event, branch in BRANCHES:
        if DATA["rows"][row]["phase"] == "none" and DATA["rows"][branch["next"]]["phase"] != "none":
            assert row in {"G", "RW"}
            assert (event, branch["next"], branch["send"]) == ("QU", "FL", True)


def test_reprice_cancel_entry_only_under_level():
    for row, event, branch in BRANCHES:
        if row != "PCR" and branch["next"] == "PCR":
            assert row in {"WK", "PF"}
            assert event == "QU" and branch["cancel"] and not branch["send"]


def test_count_reentry_is_zero_and_cancel_has_no_send():
    for row, _, branch in BRANCHES:
        if row != "G" and branch["next"] == "G":
            assert branch["count"] == "0"
        if branch["cancel"]:
            assert row in {"WK", "PF", "OX", "FZW", "FCW", "TW"}
            assert DATA["rows"][branch["next"]]["phase"] == "pc"
            assert not branch["send"]


def test_every_row_reachable_from_counting():
    reached = {"G"}
    while True:
        next_rows = reached | {b["next"] for r, _, b in BRANCHES if r in reached}
        if reached == next_rows:
            break
        reached = next_rows
    assert reached == set(DATA["rows"])


@pytest.mark.parametrize("invalidation", ["ST", "HE", "HS", "CP", "FRZ"])
@pytest.mark.parametrize("row,trigger", [("G", "QU"), ("PCR", "oCXL")])
def test_invalidation_precedes_send_trigger(invalidation, row, trigger):
    facts = {name: True for name in DATA["predicates"] if name != "count_complete"}
    facts["whole_cover"] = False
    result = sale.apply_step(
        sale.State(row, 1 if row == "G" else None),
        2,
        [sale.Event(trigger, facts), sale.Event(invalidation, facts)],
    )
    assert sale.Action.SEND not in result.actions
    assert result.events == (invalidation, trigger)


def test_step_order_preserves_order_event_arrivals():
    codes = ["QU", "oPF", "CL", "MO", "UFZ", "FRZ", "oCXL", "HG", "CP", "ST", "HS", "HE"]
    events = [sale.Event(code, {}) for code in codes]
    assert tuple(e.code for e in sale.order_step(events)) == (
        "ST",
        "HE",
        "HS",
        "HG",
        "CP",
        "CL",
        "FRZ",
        "UFZ",
        "oPF",
        "oCXL",
        "MO",
        "QU",
    )


@pytest.mark.parametrize(
    "row,event,cell",
    [(r, e, c) for r, e, c in CELLS if c["occurs"] in {"no", "n/a"}],
    ids=lambda x: str(x) if isinstance(x, str) else x["occurs"],
)
def test_impossible_or_not_emitted_event_is_named_and_inert(row, event, cell):
    state = sale.State(row, 1 if row == "G" else None)
    result = sale.transition(state, 2, event, {})
    assert result.state == state and result.actions == ()
    assert result.named and row in result.named and event in result.named


@pytest.mark.parametrize(
    "group,name,history",
    [(g, n, h) for g, histories in HISTORIES.items() for n, h in histories.items()],
    ids=[f"{g}-{n}" for g, histories in HISTORIES.items() for n in histories],
)
def test_recorded_history(group, name, history):
    state = sale.State("G", 0)
    sends = 0
    for step in history["steps"]:
        assert state.row == step["row"], (group, name, step)
        candidates = assignments(DATA["cells"][state.row][step["event"]])
        facts = next(f for f in candidates if holds(step["when"], f))
        supplied = {k: v for k, v in facts.items() if k != "count_complete"}
        result = sale.transition(state, 2, step["event"], supplied)
        assert result.when == step["when"]
        sends += result.actions.count(sale.Action.SEND)
        state = result.state
    assert (state.row, sends) == (history["ends_in"], history["sends"])


def test_all_histories_are_present_with_public_names():
    assert len(HISTORIES["named_paths"]) == 33
    assert len(HISTORIES["histories_walked"]) == 10
    for group in HISTORIES.values():
        for name in group:
            assert name == name.lower() and all(c.islower() or c == "-" for c in name)
            assert "reader" not in name


def test_missing_fact_and_caller_supplied_count_are_named():
    with pytest.raises(sale.MissingPredicate, match="formable"):
        sale.transition(sale.State("G", 0), 2, "QU", {})
    with pytest.raises(sale.InvalidFacts, match="count_complete"):
        sale.transition(sale.State("G", 0), 2, "QU", {"count_complete": True, "formable": True})
    with pytest.raises(sale.InvalidFacts, match="formable"):
        sale.transition(sale.State("G", 0), 2, "QU", json.loads('{"formable": 1}'))
    with pytest.raises(sale.InvalidFacts, match="unknown_fact"):
        sale.transition(sale.State("G", 0), 2, "NQ", {"unknown_fact": True})


@pytest.mark.parametrize(
    "row,event,error", [("unknown", "NQ", sale.UnknownRow), ("G", "unknown", sale.UnknownEvent)]
)
def test_unknown_row_or_event_raises(row, event, error):
    with pytest.raises(error, match="unknown"):
        sale.transition(sale.State(row, 0), 2, event, {})


@pytest.mark.parametrize(
    "state,n",
    [
        (sale.State("G", -1), 2),
        (sale.State("G", 2), 2),
        (sale.State("G", None), 2),
        (sale.State("GC", 0), 2),
        (sale.State("G", 0), 1),
        (sale.State("G", True), 2),
    ],
)
def test_invalid_count_state_is_refused(state, n):
    with pytest.raises(sale.InvalidState):
        sale.transition(state, n, "NQ", {})


@pytest.mark.parametrize(
    "mode,error", [("none", sale.NoMatchingBranch), ("two", sale.AmbiguousBranch)]
)
def test_missing_or_overlapping_branch_raises_by_name(mode, error):
    data = deepcopy(DATA)
    branch = data["cells"]["G"]["QU"]["branches"][0]
    data["cells"]["G"]["QU"]["branches"] = [branch] if mode == "none" else [branch, branch]
    with pytest.raises(error, match="G.*QU"):
        sale.transition(
            sale.State("G", 1 if mode == "none" else 0),
            2,
            "QU",
            {"formable": True, "leg_positive": True, "whole_cover": False},
            table=sale.table_from_data(data),
        )


def test_step_rejects_two_quotes_or_unknown_event():
    with pytest.raises(sale.InvalidFacts, match="quote"):
        sale.order_step([sale.Event("QU", {}), sale.Event("NQ", {})])
    with pytest.raises(sale.UnknownEvent, match="unknown"):
        sale.order_step([sale.Event("unknown", {})])


def test_order_inventory_is_complete_and_reaches_every_order_column():
    kinds = DATA["order_event_inventory"]["kinds"]
    assert len(kinds) == 17
    reached = set()
    for spec in kinds.values():
        reached.update(spec["columns"])
        if spec["treatment"] == "own column":
            assert len(spec["columns"]) == 1 and spec["columns"] != ["oOTH"]
        elif spec["treatment"] == "cannot occur":
            assert spec["reason"] and spec["columns"] == ["oOTH"]
        else:
            assert spec["treatment"] == "folded into named columns"
            assert spec["reason"]
    assert reached == {e for e, spec in DATA["events"].items() if spec["kind"] == "order"}


def test_inventory_matches_installed_pins_order_event_classes():
    # The pin exports all event classes at model's Python surface (python/model
    # __init__.py:22; crates/model/src/python/mod.rs:315-332). Events carry
    # event_id; OrderSnapshot and OrderStatusReport are not order events.
    offered = {
        name.removeprefix("Order")
        for name in dir(model)
        if name.startswith("Order")
        and isinstance(getattr(model, name), type)
        and hasattr(getattr(model, name), "event_id")
    }
    assert offered == set(DATA["order_event_inventory"]["kinds"])


@pytest.mark.parametrize("ended,remainder", list(itertools.product((False, True), repeat=2)))
def test_event_mapping_comes_only_from_inventory(ended, remainder):
    for kind, spec in DATA["order_event_inventory"]["kinds"].items():
        columns = spec["columns"]
        expected = (
            columns[0] if len(columns) == 1 else columns[0 if ended else 1 if remainder else 2]
        )
        assert (
            sale.order_event_column("Order" + kind, leg_ended=ended, leaves_remainder=remainder)
            == expected
        )
    assert sale.order_event_column("OrderNotInThisPin") == "oOTH"
    changed = deepcopy(DATA)
    changed["order_event_inventory"]["kinds"]["Accepted"]["columns"] = ["oSUB"]
    assert sale.order_event_column("OrderAccepted", table=sale.table_from_data(changed)) == "oSUB"


# Every account event a step judges before our order's events can stand between a send and its echo.
SEND_ECHO_ROWS = {"FL"} | {
    branch["next"]
    for event in ("ST", "HE", "RL", "HS", "HG", "CP", "CL", "FRZ", "UFZ")
    for branch in DATA["cells"]["FL"][event]["branches"]
}


def test_the_rows_a_sends_echo_can_meet_are_those_one_account_event_from_flight():
    assert SEND_ECHO_ROWS == {"FL", "FCW", "TW", "OX"}


def test_an_ended_holdings_open_sale_is_asked_canceled_whenever_it_can_take_a_cancel():
    """OX x HE: the end seen again (another position under the instrument, or a refused cancel).
    A reopened holding at least as large as our leg never shows a mismatch, so the end asks."""
    asked = sale.transition(sale.State("OX"), 2, "HE", {"cancelable": True, "cancel_due": True})
    assert asked.state == sale.State("PCX") and asked.actions == (sale.Action.CANCEL,)
    owed = sale.transition(sale.State("OX"), 2, "HE", {"cancelable": False, "cancel_due": True})
    assert owed.state == sale.State("OX") and owed.actions == ()


@pytest.mark.parametrize(
    "row,event,pending",
    [
        ("OX", "HE", "PCX"),
        ("OX", "MO", "PCX"),
        ("TW", "HE", "PCT"),
        ("TW", "MO", "PCT"),
        ("TW", "ST", "PCT"),
    ],
)
def test_a_cancel_that_repeats_waits_the_refusals_pace(row, event, pending):
    """An ended or stopped holding's open sale: its cancel is asked when due and not before."""
    facts = {"cancelable": True}
    due = sale.transition(sale.State(row), 2, event, {**facts, "cancel_due": True})
    assert due.state == sale.State(pending) and due.actions == (sale.Action.CANCEL,)
    waits = sale.transition(sale.State(row), 2, event, {**facts, "cancel_due": False})
    assert waits.state == sale.State(row) and waits.actions == ()


@pytest.mark.parametrize("row,resting", [("PCX", "OX"), ("PCT", "TW")])
@pytest.mark.parametrize("event", ["oCRJ", "oACC"])
def test_a_refused_cancel_that_is_asked_again_is_told_once(row, resting, event):
    first = sale.transition(sale.State(row), 2, event, {"cancel_refusal_told": False})
    assert first.state == sale.State(resting) and first.actions == (sale.Action.ALERT,)
    later = sale.transition(sale.State(row), 2, event, {"cancel_refusal_told": True})
    assert later.state == sale.State(resting) and later.actions == ()


@pytest.mark.parametrize("row", DATA["rows"])
@pytest.mark.parametrize("echo", ["oINI", "oPCN"])
def test_every_echo_cell(row, echo):
    state = sale.State(row, 1 if row == "G" else None)
    result = sale.transition(state, 2, echo, {})
    # A send's echo is judged in the step after it, behind that step's account events: the cover
    # may have gone unread (FL x FRZ), the instrument stopped (FL x ST) or the holding ended
    # (FL x HE) in between, so the echo is as inert in those rows as in FL.
    inert = row in SEND_ECHO_ROWS if echo == "oINI" else DATA["rows"][row]["phase"] == "pc"
    if inert:
        assert result.state == state and result.actions == () and result.named is None
    else:
        other = sale.transition(state, 2, "oOTH", {})
        assert result.state == other.state and result.actions == other.actions
        assert bool(result.named) == bool(other.named)


@pytest.mark.parametrize(
    "row,event,branch", [(r, e, b) for r, e, b in BRANCHES if b["send"] or b["cancel"]]
)
def test_action_echo_requires_the_destination_row(row, event, branch):
    cell = DATA["cells"][row][event]
    facts = next(f for f in assignments(cell) if holds(branch["when"], f))
    state, supplied = inputs(row, facts)
    result = sale.transition(state, 2, event, supplied)
    echo = "oINI" if branch["send"] else "oPCN"
    during = sale.transition(result.state, 2, echo, {})
    assert during.state == result.state and during.actions == () and during.named is None
    premature = sale.transition(state, 2, echo, {})
    assert premature.named is not None
