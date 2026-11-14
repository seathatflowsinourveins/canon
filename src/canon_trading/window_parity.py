"""Compare a recorded Mover window with a catalog replay, without explaining away differences.

Contract: session_window.write_record and Mover.record (entries/fills/exits and live
ranking_records/rankings); NautilusTrader v2.0.0rc8's publish_signal ts_event contract
(common/__init__.pyi:720) and backtest engine reports (:529-537). The signal codec is
movers_rank_signal's. This is a table over those records, with no trading decisions.

Rank generations are compared by UTC minute, retaining every generation in that minute.
Only a unique equal generation in an adjacent, otherwise absent minute proves a minute
shift. Catalog absence needs a named uncovered interval for a changed symbol's input
minute; sparse observations alone do not prove it. Matched order instructions with only
a fill-price/filled-quantity difference can be labelled fill model when the replay names
its simulated preset and the code agrees. A different commit explains the code row only.
Other differences remain unexplained. This attribution names observable mechanisms, not
an estimate of what the live venue would have filled with identical input data.

Record IDs are local to each run: entries/exits align by instrument and occurrence, not
by generated client order ID. Missing fills remain null and zero filled quantity; they
are never dropped. A window's startup roster is separate from its live ranking.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from .movers_rank_signal import decode_ranking
from .window_replay import CHOICES
from .window_replay import record_digest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

MINUTE = 60_000_000_000
CAUSES = (
    "data absent from the catalog",
    "generation minute differs",
    "fill model",
    "code version",
    CHOICES,
    "unexplained",
)


def _run(document: Mapping[str, Any]) -> Mapping[str, Any]:
    return document.get("mover", {}).get("run", {})


def _number(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


def _instrument(symbol: str) -> str:
    return symbol if symbol.endswith(".ALPACA") else f"{symbol}.ALPACA"


def _generations(
    document: Mapping[str, Any], bounds: tuple[int, int] | None
) -> dict[int, list[dict[str, Any]]]:
    run = _run(document)
    records = run.get("ranking_records") or document.get("generations")
    raw = records if records else run.get("rankings", ())
    found: dict[int, list[dict[str, Any]]] = {}
    for row in raw:
        if records and not row.get("published", True):
            continue
        stamp = row.get(
            "ts_event", row.get("computed_ns", row.get("latency_ns", {}).get("generation"))
        )
        if stamp is None:
            raise ValueError("a ranking generation has no computation timestamp")
        stamp = int(stamp)
        if bounds and not bounds[0] <= stamp <= bounds[1]:
            continue
        if "value" in row:
            names = [str(entry.instrument_id) for entry in decode_ranking(row["value"])]
        else:
            names = [_instrument(top["symbol"]) for top in row.get("top", ())]
        found.setdefault(stamp // MINUTE * MINUTE, []).append({"ts_event": stamp, "names": names})
    for rows in found.values():
        rows.sort(key=lambda row: row["ts_event"])
    return found


def _names(generations: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    return [row["names"] for row in generations]


def _missing(
    replay: Mapping[str, Any],
    instruments: set[str],
    low: int,
    high: int,
    data_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    return [
        row
        for row in replay.get("catalog", {}).get("missing", ())
        if row["instrument_id"] in instruments
        and row["data_type"] in data_types
        and int(row["start_ns"]) <= high
        and int(row["end_ns"]) >= low
    ]


def _row(
    section: str,
    key: str,
    before: Any,
    after: Any,
    difference: list[str],
    cause: str = "unexplained",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "section": section,
        "key": key,
        "record": before,
        "replay": after,
        "status": "different" if difference else "same",
        "difference": difference,
        "cause": cause if difference else None,
        **extra,
    }


def _rank_rows(record: Mapping[str, Any], replay: Mapping[str, Any]) -> list[dict[str, Any]]:
    bounds = None
    if "window_start" in replay and "window_end" in replay:
        bounds = (
            int(datetime.fromisoformat(replay["window_start"]).timestamp() * 1_000_000_000),
            int(datetime.fromisoformat(replay["window_end"]).timestamp() * 1_000_000_000),
        )
    left, right = _generations(record, bounds), _generations(replay, bounds)
    shifted: set[int] = set()
    # Do not move a generation onto a minute that already has a live/replay counterpart,
    # or pair repeated rankings ambiguously.
    candidates = {
        minute: [
            other
            for other in right.keys() - left.keys()
            if abs(other - minute) == MINUTE and _names(right[other]) == _names(left[minute])
        ]
        for minute in left.keys() - right.keys()
    }
    for minute, matches in candidates.items():
        if len(matches) == 1 and sum(matches[0] in options for options in candidates.values()) == 1:
            shifted.update((minute, matches[0]))
    rows = []
    for minute in sorted(left.keys() | right.keys()):
        before, after = left.get(minute, []), right.get(minute, [])
        changed = _names(before) != _names(after)
        cause, proof = "unexplained", []
        if minute in shifted:
            cause = "generation minute differs"
        elif changed:
            old_names = {name for generation in before for name in generation["names"]}
            new_names = {name for generation in after for name in generation["names"]}
            proof = _missing(replay, old_names ^ new_names, minute - MINUTE, minute, ("Bar",))
            if proof:
                cause = "data absent from the catalog"
        rows.append(
            _row(
                "ranking",
                datetime.fromtimestamp(minute / 1e9, UTC).isoformat(),
                before,
                after,
                ["ranked names"] if changed else [],
                cause,
                minute_ns=minute,
                evidence=proof,
            )
        )
    return rows


def _entries(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    run = _run(document)
    found, counts = {}, {}
    for entry in sorted(
        run.get("entries", ()), key=lambda row: (row.get("submitted_ns") or 0, row["instrument_id"])
    ):
        instrument = entry["instrument_id"]
        counts[instrument] = counts.get(instrument, 0) + 1
        fills = [
            fill
            for fill in run.get("fills", ())
            if fill[0] == entry["client_order_id"] and fill[1] == "BUY"
        ]
        quantity = sum((Decimal(str(fill[2])) for fill in fills), Decimal(0))
        dollars = sum((Decimal(str(fill[2])) * Decimal(str(fill[3])) for fill in fills), Decimal(0))
        found[f"{instrument}/{counts[instrument]}"] = {
            "instrument_id": instrument,
            "submit_time": entry.get("submitted_ns"),
            "fill_time": max((int(fill[4]) for fill in fills), default=None),
            "fill_price": _number(dollars / quantity) if quantity else None,
            "quantity": _number(quantity),
            "requested_quantity": _number(
                entry.get("quantity", (entry.get("sizing") or {}).get("quantity"))
            ),
        }
    return found


def _exits(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    found, counts = {}, {}
    for exit_row in sorted(
        _run(document).get("exits", ()),
        key=lambda row: (row.get("trigger_ns") or 0, row["instrument_id"]),
    ):
        instrument = exit_row["instrument_id"]
        counts[instrument] = counts.get(instrument, 0) + 1
        submissions = [
            sale["sent_ns"] for sale in exit_row.get("sales", ()) if sale.get("sent_ns") is not None
        ]
        found[f"{instrument}/{counts[instrument]}"] = {
            "instrument_id": instrument,
            "submit_time": min(submissions) if submissions else None,
            "trigger_time": exit_row.get("trigger_ns"),
            "fill_time": exit_row.get("final_fill_ns"),
            "fill_price": _number(exit_row.get("fill_vwap")),
            "quantity": _number(exit_row.get("filled_quantity")),
            "requested_quantity": _number(exit_row.get("quantity")),
        }
    return found


def _trade_rows(
    section: str,
    before: Mapping[str, dict[str, Any]],
    after: Mapping[str, dict[str, Any]],
    record: Mapping[str, Any],
    replay: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key), after.get(key)
        fields = list((left or right or {}).keys())
        difference = [
            field for field in fields if (left or {}).get(field) != (right or {}).get(field)
        ]
        if left is None or right is None:
            difference = ["entry absent" if section == "entry" else "exit absent"]
        cause = "unexplained"
        if (
            left
            and right
            and difference
            and set(difference) <= {"fill_price", "quantity", "fill_time"}
        ):
            submitted = left.get("submit_time")
            code = record.get("commit")
            same_code = (
                code
                and not code.startswith("unknown")
                and "with uncommitted changes" not in code
                and code == replay.get("commit")
            )
            known_instruction = submitted is not None and left["requested_quantity"] is not None
            missing = _missing(
                replay,
                {left["instrument_id"]},
                submitted or 0,
                max(
                    (submitted or 0) + MINUTE,
                    left.get("fill_time") or 0,
                    right.get("fill_time") or 0,
                ),
                ("QuoteTick", "TradeTick"),
            )
            if (
                same_code
                and known_instruction
                and not missing
                and replay.get("venue", {}).get("preset") in ("touch", "tape")
            ):
                cause = "fill model"
        rows.append(_row(section, key, left, right, difference, cause))
    return rows


def compare(record: Mapping[str, Any], replay: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON table with the fixed cause vocabulary and evidence for each row."""
    rows = _rank_rows(record, replay)
    before = record.get("mover", {}).get("roster", record.get("roster", []))
    after = replay.get("mover", {}).get("roster", replay.get("roster", []))

    def roster(value):
        return [
            {
                "instrument_id": _instrument(row["symbol"]),
                "entry_limit": _number(row["entry_limit"]),
            }
            for row in value
        ]

    before, after = roster(before), roster(after)
    rows.append(_row("roster", "startup", before, after, ["roster"] if before != after else []))
    source = replay.get("source_record") or {}
    if source.get("choices") == CHOICES and source.get("sha256") == record_digest(record):
        rows.append(
            _row(
                "choice",
                "roster source",
                "pre-node selection",
                "copied from this record",
                ["source"],
                CHOICES,
                evidence=source["sha256"],
            )
        )
    rows.extend(_trade_rows("entry", _entries(record), _entries(replay), record, replay))
    rows.extend(_trade_rows("exit", _exits(record), _exits(replay), record, replay))
    before_code, after_code = record.get("commit"), replay.get("commit")
    rows.append(
        _row(
            "code",
            "commit",
            before_code,
            after_code,
            ["commit"] if before_code != after_code else [],
            "code version" if before_code and after_code else "unexplained",
        )
    )
    return {"schema": 1, "causes": list(CAUSES), "rows": rows}


def markdown(table: Mapping[str, Any]) -> str:
    def cell(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
        return text.replace("|", "\\|").replace("\n", "<br>")

    lines = [
        "| Section | Key | Record | Replay | Status | Difference | Cause |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in table["rows"]:
        lines.append(
            "| "
            + " | ".join(
                cell(row[key])
                for key in ("section", "key", "record", "replay", "status", "difference", "cause")
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    table = compare(json.loads(args.record.read_text()), json.loads(args.replay.read_text()))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "parity.json").write_text(json.dumps(table, indent=2, allow_nan=False) + "\n")
    (args.out / "parity.md").write_text(markdown(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
