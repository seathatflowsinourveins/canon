"""S2 parity uses synthetic session_window/Mover records; every output stays in tmp_path."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

MINUTE = 60_000_000_000
AT = 1_762_786_200_000_000_000


def generation(minute, symbols):
    return {
        "published": True,
        "latency_ns": {"generation": AT + minute * MINUTE + 5_000_000_000},
        "top": [{"symbol": symbol} for symbol in symbols],
    }


def pair():
    entry = {
        "client_order_id": "B1",
        "instrument_id": "LLL.ALPACA",
        "submitted_ns": AT + 4 * MINUTE,
        "quantity": "10",
    }
    run = {
        "ranking_records": [
            generation(0, ["LLL", "Z0"]),
            generation(1, ["LLL"]),
            generation(3, ["Z0"]),
            generation(5, ["Z1"]),
        ],
        "entries": [entry],
        "fills": [["B1", "BUY", "10", "10.10", AT + 4 * MINUTE]],
        "exits": [
            {
                "instrument_id": "LLL.ALPACA",
                "entry_client_order_id": "B1",
                "quantity": "10",
                "filled_quantity": "10",
                "fill_vwap": "10.30",
                "trigger_ns": AT + 6 * MINUTE,
                "final_fill_ns": AT + 6 * MINUTE,
                "sales": [{"submitted_ns": AT + 6 * MINUTE}],
            }
        ],
    }
    record: dict[str, Any] = {
        "commit": "same-code",
        "mover": {"roster": [{"symbol": "LLL", "entry_limit": "10.01"}], "run": run},
    }
    replay = copy.deepcopy(record)
    replay.update({"venue": {"preset": "touch"}, "catalog": {"missing": []}})
    return record, replay


def test_parity_assigns_only_supported_causes_and_keeps_unexplained():
    from canon_trading.window_parity import compare

    record, replay = pair()
    ranks = replay["mover"]["run"]["ranking_records"]
    ranks[1] = generation(2, ["LLL"])
    ranks[2] = generation(3, [])
    ranks[3] = generation(5, ["Z2"])
    replay["catalog"]["missing"] = [
        {
            "data_type": "Bar",
            "instrument_id": "Z0.ALPACA",
            "start_ns": AT + 2 * MINUTE,
            "end_ns": AT + 3 * MINUTE,
        }
    ]
    replay["mover"]["run"]["fills"][0][3] = "10.12"
    result = compare(record, replay)
    rows = result["rows"]
    ranking = [row for row in rows if row["section"] == "ranking"]
    assert ranking[0]["status"] == "same"
    shifted = [row for row in ranking if row["minute_ns"] in (AT + MINUTE, AT + 2 * MINUTE)]
    assert len(shifted) == 2
    assert all(row["cause"] == "generation minute differs" for row in shifted)
    assert (
        next(row for row in ranking if row["minute_ns"] == AT + 3 * MINUTE)["cause"]
        == "data absent from the catalog"
    )
    assert (
        next(row for row in ranking if row["minute_ns"] == AT + 5 * MINUTE)["cause"]
        == "unexplained"
    )
    entry = next(row for row in rows if row["section"] == "entry")
    assert entry["difference"] == ["fill_price"] and entry["cause"] == "fill model"
    assert next(row for row in rows if row["section"] == "exit")["status"] == "same"
    assert next(row for row in rows if row["section"] == "roster")["status"] == "same"


def test_code_provenance_does_not_explain_arbitrary_trade_changes():
    from canon_trading.window_parity import compare

    record, replay = pair()
    replay["commit"] = "other-code"
    replay["mover"]["run"]["entries"][0]["submitted_ns"] += MINUTE
    rows = compare(record, replay)["rows"]
    assert next(row for row in rows if row["section"] == "code")["cause"] == "code version"
    assert next(row for row in rows if row["section"] == "entry")["cause"] == "unexplained"


def test_rankings_fallback_multiple_generations_and_unfilled_entries_are_not_dropped():
    from canon_trading.window_parity import compare

    record, replay = pair()
    for document in (record, replay):
        run = document["mover"]["run"]
        run.pop("ranking_records")
        run["rankings"] = [
            {"value": "LLL.ALPACA=0.25", "computed_ns": AT},
            {"value": "Z0.ALPACA=0.3", "computed_ns": AT + 2_000_000_000},
        ]
        run["entries"].append(
            {"client_order_id": "B2", "instrument_id": "Z0.ALPACA", "submitted_ns": AT + MINUTE}
        )
    rows = compare(record, replay)["rows"]
    ranks = [row for row in rows if row["section"] == "ranking"]
    assert len(ranks) == 1 and len(ranks[0]["record"]) == 2
    assert len([row for row in rows if row["section"] == "entry"]) == 2
    assert all(row["status"] == "same" for row in rows)


def test_cli_writes_json_and_markdown_with_the_same_rows(tmp_path):
    from canon_trading.window_parity import main

    record, replay = pair()
    before, after, output = tmp_path / "record.json", tmp_path / "replay.json", tmp_path / "out"
    before.write_text(json.dumps(record))
    after.write_text(json.dumps(replay))
    assert main(["--record", str(before), "--replay", str(after), "--out", str(output)]) == 0
    result = json.loads((output / "parity.json").read_text())
    markdown = (output / "parity.md").read_text()
    assert result["rows"] and all(row["status"] == "same" for row in result["rows"])
    assert "| Section |" in markdown and "LLL.ALPACA" in markdown
    assert markdown.count("| same |") == len(result["rows"])


def test_exit_submit_time_uses_the_sale_record_sent_ns():
    from canon_trading.window_parity import compare

    record, replay = pair()
    for document in (record, replay):
        document["mover"]["run"]["exits"][0]["sales"] = [{"sent_ns": AT + 6 * MINUTE}]
    row = next(row for row in compare(record, replay)["rows"] if row["section"] == "exit")
    assert row["record"]["submit_time"] == AT + 6 * MINUTE


def test_pre_node_choice_cause_requires_the_exact_source_record():
    from canon_trading.window_parity import compare
    from canon_trading.window_replay import CHOICES
    from canon_trading.window_replay import record_digest

    record, replay = pair()
    replay["source_record"] = {"choices": CHOICES, "sha256": record_digest(record)}
    choice = next(row for row in compare(record, replay)["rows"] if row["section"] == "choice")
    assert choice["cause"] == CHOICES and choice["status"] == "different"
    replay["source_record"]["sha256"] = "different-record"
    assert not any(row["section"] == "choice" for row in compare(record, replay)["rows"])


def test_entry_with_null_sizing_keeps_its_explicit_requested_quantity():
    from canon_trading.window_parity import compare

    record, replay = pair()
    for document in (record, replay):
        document["mover"]["run"]["entries"][0]["sizing"] = None
    row = next(row for row in compare(record, replay)["rows"] if row["section"] == "entry")
    assert row["status"] == "same"
    assert row["record"]["requested_quantity"] == "10"


def test_missing_data_during_a_delayed_fill_is_not_called_fill_model():
    from canon_trading.window_parity import compare

    record, replay = pair()
    for document in (record, replay):
        document["mover"]["run"]["fills"][0][4] = AT + 10 * MINUTE
    replay["mover"]["run"]["fills"][0][3] = "10.12"
    replay["catalog"]["missing"] = [
        {
            "data_type": "QuoteTick",
            "instrument_id": "LLL.ALPACA",
            "start_ns": AT + 9 * MINUTE,
            "end_ns": AT + 10 * MINUTE,
        }
    ]
    row = next(row for row in compare(record, replay)["rows"] if row["section"] == "entry")
    assert row["cause"] == "unexplained"


@pytest.mark.parametrize("code", ["unknown: no checkout", "same-code with uncommitted changes"])
def test_unverified_code_identity_cannot_prove_a_fill_model_difference(code):
    from canon_trading.window_parity import compare

    record, replay = pair()
    record["commit"] = replay["commit"] = code
    replay["mover"]["run"]["fills"][0][3] = "10.12"
    row = next(row for row in compare(record, replay)["rows"] if row["section"] == "entry")
    assert row["cause"] == "unexplained"


def test_a_submit_later_in_the_same_minute_is_latency_with_the_fill_model():
    """Paper submits about a second after the minute it decides on; the replay, with no latency,
    at the minute. That alone never makes a matched fill unexplained."""
    from canon_trading.window_parity import compare

    record, replay = pair()
    record["mover"]["run"]["entries"][0]["submitted_ns"] += 900_000_000
    replay["mover"]["run"]["fills"][0][3] = "10.12"
    entry = next(row for row in compare(record, replay)["rows"] if row["section"] == "entry")
    assert entry["difference"] == ["submit_time", "fill_price"]
    assert entry["cause"] == "latency and fill model"
