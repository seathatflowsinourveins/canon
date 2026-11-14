"""Read Alpaca's split records over a span of days, GET only, and write them beside the catalog.

`mise run paper:history <account> corporate-actions` runs this module with one paper account's pair in
data-only mode (ADR 0070, ADR 0093). It sends Alpaca's documented `GET /v1/corporate-actions` on the
market data host (https://docs.alpaca.markets/reference/corporateactions-1, read 2026-11-14) with `types`
`reverse_split,forward_split,unit_split`, `start` and `end` as inclusive `YYYY-MM-DD` dates, and `symbols`
when names are given, through alpaca-py's `CorporateActionsClient` at `c42add7c0a70`
(`alpaca/data/historical/corporate_actions.py`, `api_version="v1"`, `BaseURL.DATA`). Its pages are asked
one at a time through the client's own `RESTClient.get`, following `next_page_token`, rather than
`get_corporate_actions`, whose pager (`alpaca/common/rest.py`, `_get_marketdata`) asks pages back to back
and stops at the request's `limit` (default 1000) without saying so; here every send, alpaca-py's own
retries included, is paced at `paper:history`'s row of ADR 0093 by history_load's paced transport, under
the root's catalog lock, and a cap reached with a page left stops the run by name. That paper keys
may call the Market Data API is documented (https://docs.alpaca.markets/us/docs/authentication.md); that
they may call this endpoint is not stated, so a 401 or 403 stops the run by name.

The span is checked first against a date map (`--date-map`, the sim lane's `{"dates": {iso: {"kind"}}}`):
every day in it must be `development` or `closed`; a weekday the map omits, or any day outside the map's
first to last date, is refused, and a malformed map is refused by name. `--check-only`
makes that check and nothing else, with no pair; the task runs it before it reads a pair.

Each row keeps six fields: `type`, `symbol`, `ex_date`, `process_date`, `old_rate` and `new_rate`. Every
other field Alpaca sends (identifiers, CUSIPs, ISINs, currency, other dates and any text) is dropped
before anything is written or printed. A unit split has no `symbol` or `ex_date`; its `old_symbol` and
`effective_date` fill them, and the row says so in `mapped_from`. A row whose Alpaca record names a
`new_symbol` other than its symbol is counted in `renamed_rows`, a named gap: the new ticker is not kept.
The reference page says the actions "are sorted by their `process_date`" and does not say which date the
span bounds, so a split whose `ex_date` is in the span but whose `process_date` is not may be missing; that
is named in `gaps`, and a row whose `ex_date` lies outside the span is marked. An `ex_date` or
`process_date` on a day the map would refuse is withheld, with the reason. A symbol that is not a plain
ticker is written masked and counted. Nothing of the account is read; the answer is market data.

The file is `splits-<start>_<end>-all.json` for every symbol, or a digest of the names asked in place of
`all`. `split_read` turns it into the ranking's `movers_rank.SplitRead`, whose `split_since` is the
exclusion's test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Sequence
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models.corporate_actions import ForwardSplit
from alpaca.data.models.corporate_actions import ReverseSplit
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import data_only
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import session_of
from canon_alpaca.providers import with_http_timeout

from canon_trading.history_load import LEDGER
from canon_trading.history_load import PACE
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import _catalog_lock
from canon_trading.history_load import _paced_trade_gets
from canon_trading.history_load import stop_at_429
from canon_trading.movers_rank import SplitRead

__all__ = [
    "KEPT",
    "MAX_PAGES",
    "PACE",
    "PAGE_ROWS",
    "PATH",
    "TYPES",
    "corporate_actions_client",
    "main",
    "out_path",
    "read_splits",
    "span_refusals",
    "split_read",
]

NY = ZoneInfo("America/New_York")
PATH = "/corporate-actions"
"""Alpaca's path for corporate actions, under the client's `v1` on the market data host."""
SOURCE = "GET https://data.alpaca.markets/v1/corporate-actions"
TYPES = ("reverse_split", "forward_split", "unit_split")
ARRAYS = {
    "reverse_splits": "reverse_split",
    "forward_splits": "forward_split",
    "unit_splits": "unit_split",
}
"""The answer's keys under `corporate_actions` (the reference page) and the type each holds."""
KEPT = ("type", "symbol", "ex_date", "process_date", "old_rate", "new_rate")
PAGE_ROWS = 1000
"""The endpoint's largest `limit` (the reference page: maximum 1000)."""
MAX_PAGES = 50
"""Pages of one read before it stops: 50,000 split records, far more than months of splits hold
(UNCALIBRATED)."""
ALLOWED = frozenset({"development", "closed"})
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-/]{0,15}")
CLOCK = time.monotonic
SLEEP = time.sleep
"""The pace's clock and wait, history_load's paced transport's (`_paced_trade_gets`), which paces each send
alpaca-py makes, its own retries included."""


class MapUnreadable(ValueError):
    """The date map is not `{"dates": {iso: {"kind": ...}}}`."""


def corporate_actions_client() -> CorporateActionsClient:
    """alpaca-py's corporate actions client on the environment's pair: GET only, with the HTTP timeout,
    the first 429 and a 401 or 403 ending the run. Refused outside data-only mode."""
    if not data_only():
        raise HistoryStopped(
            f"paper:history runs in data-only mode only ({DATA_ONLY_ENV}=1, ADR 0070); nothing was built"
        )
    api_key, secret_key = credentials_from_env()
    client = with_http_timeout(CorporateActionsClient(api_key=api_key, secret_key=secret_key))
    require_get_only(client)
    session = session_of(client)
    assert session is not None  # with_http_timeout raised otherwise
    session.hooks["response"].append(stop_at_429)
    session.hooks["response"].append(_stop_at_refusal)
    return client


def _stop_at_refusal(
    response: requests.Response, *args: object, **kwargs: object
) -> requests.Response:
    if response.status_code in (401, 403):
        raise HistoryStopped(
            f"Alpaca refused the read with {response.status_code} on /v1{PATH}; "
            "this pair may not read corporate actions, and nothing more was asked"
        )
    return response


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _map(path: Path) -> tuple[dict[str, str], str]:
    """Each day's kind and the map's `generated` stamp; `MapUnreadable` names the first entry it cannot
    read."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise MapUnreadable(f"{path} cannot be read as JSON ({type(e).__name__})") from e
    dates = raw.get("dates") if isinstance(raw, dict) else None
    if not isinstance(dates, dict):
        raise MapUnreadable(f"{path} has no dates")
    kinds = {}
    for day, entry in dates.items():
        if not isinstance(entry, dict):
            raise MapUnreadable(f"{path}: the entry for {day} is not an object")
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            raise MapUnreadable(f"{path}: {day} no kind")
        kinds[day] = kind
    return kinds, str(raw.get("generated", ""))


def _refusal(day: date, kinds: dict[str, str]) -> str | None:
    """Why the map does not allow a day: its kind when not allowed, `not in the map` for a weekday the map
    leaves out or any day outside the map's first to last date; None for an allowed day (a weekend day
    inside the map's range needs no entry)."""
    kind = kinds.get(day.isoformat())
    if kind is not None:
        return None if kind in ALLOWED else kind
    inside = bool(kinds) and min(kinds) <= day.isoformat() <= max(kinds)
    return None if inside and day.weekday() >= 5 else "not in the map"


def _withheld(day: str, kinds: dict[str, str]) -> str | None:
    """Why a date the answer names is withheld, by `_refusal`; None for a day the map allows."""
    return _refusal(date.fromisoformat(day), kinds)


def span_refusals(start: date, end: date, kinds: dict[str, str]) -> list[str]:
    """Every day of [start, end] the map does not allow, by name; empty when the span may be read."""
    return [f"{d} {why}" for d in _days(start, end) if (why := _refusal(d, kinds)) is not None]


def read_splits(
    client: Any,
    *,
    start: date,
    end: date,
    symbols: Sequence[str],
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Every page of the span's split records, as (type, Alpaca's record) pairs, and the pages read. The
    caller paces the client's sends."""
    rows: list[tuple[str, dict[str, Any]]] = []
    token: str | None = None
    pages = 0
    while True:
        if pages == MAX_PAGES:
            raise HistoryStopped(f"{MAX_PAGES} pages read and Alpaca has more; shorten the span")
        query: dict[str, Any] = {
            "types": ",".join(TYPES),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": PAGE_ROWS,
            "sort": "asc",
        }
        if symbols:
            query["symbols"] = ",".join(symbols)
        if token:
            query["page_token"] = token
        try:
            page = client.get(PATH, query)
        except HistoryStopped:
            raise
        except APIError as e:
            raise HistoryStopped(f"page {pages + 1} failed: Alpaca answered {e.status_code}") from e
        except Exception as e:
            raise HistoryStopped(f"page {pages + 1} failed ({type(e).__name__})") from e
        pages += 1
        if not isinstance(page, dict) or not isinstance(page.get("corporate_actions"), dict):
            raise HistoryStopped(f"page {pages} has no corporate_actions field")
        for key, kind in ARRAYS.items():
            for record in page["corporate_actions"].get(key) or []:
                if not isinstance(record, dict):
                    raise HistoryStopped(f"page {pages}: a {kind} record is not an object")
                rows.append((kind, record))
        token = page.get("next_page_token")
        if not token:
            return rows, pages


MASKED = "<masked>"


def _symbol(value: object) -> str:
    return value if isinstance(value, str) and SYMBOL.fullmatch(value) else MASKED


def _iso(value: object, field: str, kind: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise HistoryStopped(f"a {kind} record's {field} is not a date") from None


def _rate(value: object, field: str, kind: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HistoryStopped(f"a {kind} record's {field} is not a number")
    return value


def masked_row(
    kind: str, record: dict[str, Any], *, start: date, end: date, kinds: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    """The six kept fields of one record, its span mark, and whether it names a new symbol."""
    unit = kind == "unit_split"
    symbol = _symbol(record.get("old_symbol" if unit else "symbol"))
    ex = _iso(record.get("effective_date" if unit else "ex_date"), "ex_date", kind)
    row: dict[str, Any] = {
        "type": kind,
        "symbol": symbol,
        "ex_date": ex,
        "process_date": _iso(record.get("process_date"), "process_date", kind),
        "old_rate": _rate(record.get("old_rate"), "old_rate", kind),
        "new_rate": _rate(record.get("new_rate"), "new_rate", kind),
        "ex_date_in_read_span": start <= date.fromisoformat(ex) <= end,
    }
    if unit:
        row["mapped_from"] = {"symbol": "old_symbol", "ex_date": "effective_date"}
    for field in ("ex_date", "process_date"):
        why = _withheld(row[field], kinds)
        if why is not None:
            row[field] = None
            row[f"{field}_withheld"] = why
    new = record.get("new_symbol")
    return row, bool(new) and new != record.get("old_symbol" if unit else "symbol")


def out_path(catalog: Path, start: date, end: date, symbols: Sequence[str]) -> Path:
    """Where a read is written: `-all` for every symbol, else a digest of the names asked, so a named read
    never replaces the span's read of every symbol."""
    tag = "all" if not symbols else hashlib.sha256(",".join(symbols).encode()).hexdigest()[:12]
    return catalog / "corporate-actions" / f"splits-{start}_{end}-{tag}.json"


def split_read(record: dict[str, Any]) -> tuple[SplitRead, dict[str, int]]:
    """The ranking's `SplitRead` from a written record: its forward and reverse splits over the record's
    span, and what was left out by kind. A unit split is not a forward or reverse split and a row whose
    ex-date was withheld has none, so neither enters. The models are built without validation
    (`model_construct`) because the record keeps six fields and the models also require CUSIPs, which
    the split test does not read."""
    actions: list[ForwardSplit | ReverseSplit] = []
    left_out = {"unit_split": 0, "withheld": 0}
    for row in record["rows"]:
        if row["type"] == "unit_split":
            left_out["unit_split"] += 1
            continue
        if row["ex_date"] is None:
            left_out["withheld"] += 1
            continue
        model = ForwardSplit if row["type"] == "forward_split" else ReverseSplit
        actions.append(
            model.model_construct(
                corporate_action_type=f"{row['type']}s",
                symbol=row["symbol"],
                ex_date=date.fromisoformat(row["ex_date"]),
                process_date=None
                if row["process_date"] is None
                else date.fromisoformat(row["process_date"]),
                old_rate=row["old_rate"],
                new_rate=row["new_rate"],
            )
        )
    span = SplitRead(
        start=date.fromisoformat(record["start"]),
        end=date.fromisoformat(record["end"]),
        actions=tuple(actions),
    )
    return span, left_out


def _span_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


def _catalog(text: str) -> Path:
    path = Path(text).resolve()
    if path.is_relative_to(LEDGER.resolve()):
        raise argparse.ArgumentTypeError(
            f"{text} is under ledger/; the splits go beside the catalog"
        )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper:history corporate-actions", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--start", type=_span_date, required=True, help="first day, YYYY-MM-DD, inclusive"
    )
    parser.add_argument(
        "--end", type=_span_date, required=True, help="last day, YYYY-MM-DD, inclusive"
    )
    parser.add_argument("--date-map", type=Path, required=True, help="the sim lane's date map")
    parser.add_argument(
        "--catalog", type=_catalog, default=Path("catalog"), help="the catalog root"
    )
    names = parser.add_mutually_exclusive_group()
    names.add_argument("--symbols", help="comma-separated; default every symbol")
    names.add_argument("--symbols-file", type=Path, help="a file of symbols, one a line")
    parser.add_argument(
        "--check-only", action="store_true", help="check the span against the map; send nothing"
    )
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end is before --start")
    try:
        kinds, generated = _map(args.date_map)
    except MapUnreadable as e:
        print(
            f"paper:history corporate-actions: refused: the date map is unreadable: {e}; nothing was read"
        )
        return 3
    bad = span_refusals(args.start, args.end, kinds)
    if bad:
        print(
            f"paper:history corporate-actions: refused by the date map ({generated}): {', '.join(bad)}; nothing was read"
        )
        return 3
    if args.check_only:
        print(
            f"paper:history corporate-actions: {args.start}..{args.end} allowed by the date map ({generated})"
        )
        return 0
    text = (
        args.symbols_file.read_text()
        if args.symbols_file
        else (args.symbols or "").replace(",", "\n")
    )
    symbols = sorted({s.strip().upper() for s in text.splitlines() if s.strip()})
    args.catalog.mkdir(parents=True, exist_ok=True)
    out = out_path(args.catalog, args.start, args.end, symbols)
    try:
        with _catalog_lock(args.catalog):
            client = corporate_actions_client()
            with _paced_trade_gets(client, PACE, CLOCK, SLEEP):
                raw, pages = read_splits(client, start=args.start, end=args.end, symbols=symbols)
            masked = [masked_row(k, r, start=args.start, end=args.end, kinds=kinds) for k, r in raw]
            rows = [row for row, _ in masked]
            renamed = sum(1 for _, new in masked if new)
            hidden = sum(1 for row in rows if row["symbol"] == MASKED)
            outside = sum(1 for row in rows if not row["ex_date_in_read_span"])
            spread = sorted(
                (date.fromisoformat(r["ex_date"]) - date.fromisoformat(r["process_date"])).days
                for r in rows
                if r["ex_date"] is not None and r["process_date"] is not None
            )
            record = _record(
                args, symbols, generated, pages, rows, renamed, hidden, outside, spread
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            partial = out.with_name(f"{out.name}.{os.getpid()}.partial")
            partial.write_text(json.dumps(record, indent=1) + "\n")
            partial.replace(out)
    except HistoryStopped as e:
        print(f"paper:history corporate-actions: stopped: {e}")
        return 1
    counts = {t: sum(1 for r in rows if r["type"] == t) for t in TYPES}
    print(
        f"paper:history corporate-actions: {len(rows)} rows in {pages} pages "
        f"({', '.join(f'{t} {n}' for t, n in counts.items())}); ex_date outside the span {outside}; "
        f"renamed {renamed}; masked symbols {hidden}; wrote {out}"
    )
    return 0


def _record(
    args: argparse.Namespace,
    symbols: Sequence[str],
    generated: str,
    pages: int,
    rows: list[dict[str, Any]],
    renamed: int,
    hidden: int,
    outside: int,
    spread: list[int],
) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "client": "alpaca-py CorporateActionsClient, pages through RESTClient.get",
        "read_at": datetime.now(NY).isoformat(timespec="seconds"),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "types": list(TYPES),
        "symbols": symbols or "all",
        "date_map": str(args.date_map),
        "date_map_generated": generated,
        "pages": pages,
        "rows": rows,
        "renamed_rows": renamed,
        "ex_date_outside_read_span": outside,
        "masked_symbols": hidden,
        "ex_minus_process_days": {"min": spread[0], "max": spread[-1]} if spread else None,
        "gaps": [
            (
                "the span bounds the read by Alpaca's process_date (implied by the reference page, not stated): "
                "a split whose ex_date is in the span and whose process_date is not may be missing"
            ),
            (
                f"{renamed} rows name a new_symbol, which is not kept: a name ranked under its new ticker is "
                "not matched by symbol"
            ),
            (
                f"{hidden} rows have a symbol that is not a plain ticker, written as {MASKED}: their "
                "splits cannot be matched by symbol"
            ),
            "Alpaca has no guarantees on the creation time of corporate actions (the reference page)",
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
