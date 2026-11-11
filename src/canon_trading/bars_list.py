"""Bars of a list of symbols and windows into one catalog root, through `load_bars`, one entry at a time.

Run with paper account A's pair as `CANON_BARS_LIST=<file> mise run paper:data bars-list --minutes <n>`:
the data-only task (ADR 0070), so every request but GET is refused, and this module opens no stream.
It calls `canon_trading.bars_to_catalog.load_bars` once per entry, as `test_paper_quote_study_bars.py`
calls it for ADR 0077's study: the asset lookup goes to Alpaca's paper Trading API, the bars to
Alpaca's Market Data API (`StockHistoricalDataClient.get_stock_bars`, which alpaca-py pages at its
`DATA_V2_MAX_LIMIT`, 10,000 bars a request).

The list is JSON, kept outside the repository because its symbols and windows are a study's and change
from day to day: `{"feed": "sip", "report": "<file>", "loads": [{"symbol": "AAPL", "bar": "1-MINUTE",
"start": "2026-11-02T04:00", "end": "2026-11-10T20:00"}]}`. Times are New York wall clock and bound the
bars' `ts_init`, as `load_bars` reads a window: a minute bar opening at 04:00 has its `ts_init` at 04:01,
a daily bar's is 24 hours after its midnight open (`canon_alpaca.data`, "Bar windows"). `feed` is one a
bars request takes (`canon_alpaca.constants.BARS_FEEDS`), `sip` when absent, and every entry goes into
the root `catalog/bars-list/<feed>/`, one feed per root as `load_bars` requires ("Feeds").

An entry whose end is closer to now than `load_bars` allows (one bar interval, at least its publication
delay) is cut to that margin less a minute, so today's bars load up to then and a later run of the same
list fetches only the rest; one with nothing left is reported as not yet loadable. A load that fails is
reported with its error and the others still run. No entry starts after `--minutes` have passed;
those left are reported as not started. The exit status is 1 when a load failed or an entry was not
started, else 0; a load with some weekdays missing still exits 0, and its row names them. Each entry is reported by New York
day: minute bars counted by the part of the extended session they open in, with the first and last
pre-market open; every weekday of the window with no bar is named as missing, never dropped (an
exchange holiday is named too: no calendar is read). The report records each load's wall time, which
is the pace this loader achieves. The report is rewritten after every entry, so a run the task kills
partway keeps the rows it finished. A relative `report` is read beside the list; `CANON_BARS_LIST`
itself should be absolute, since mise runs the task from the repository root. Limit: no lock is taken
on the catalog root, so two runs on one feed at once can write the same root; run one at a time.

Pace: each load makes one Trading API GET, the node's asset lookup (`canon_alpaca.providers`), and one
Market Data API GET per 10,000 bars. Unpaced, a load takes about 0.3 s (measured on paper, 2026-11-11),
so a long list would spend most of the account's Trading API limit, "200 requests per minute, per
account" (https://alpaca.markets/support/usage-limit-api-calls, ADR 0069), which the account's other
paper runs share. Loads start at most `LOADS_PER_MINUTE` a minute, the historical data limit (200 a
minute on Alpaca's free plan, https://docs.alpaca.markets/us/docs/about-market-data-api.md) included.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from canon_alpaca.constants import BARS_FEEDS
from canon_alpaca.constants import VENUE
from canon_alpaca.data import NEW_YORK
from canon_alpaca.data import bar_interval_ns
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.bars_to_catalog import PUBLICATION_DELAY
from canon_trading.bars_to_catalog import load_bars

if TYPE_CHECKING:
    from collections.abc import Sequence

LIST_ENV = "CANON_BARS_LIST"
REPO = Path(__file__).resolve().parents[2]
ROOTS = REPO / "catalog" / "bars-list"
MAX_MINUTES = 60
LOAD_TIMEOUT_SECS = 600
"""One load's bound in `load_bars`. The last entry may start at `MAX_MINUTES`, so the task's deadline holds
both (tests/test_bars_list.py checks the sum)."""
LOADS_PER_MINUTE = 30
"""At most this many loads start a minute: 15% of the account's Trading API limit (this module's header)."""

SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
BARS = ("1-MINUTE", "1-DAY")
PARTS = (
    ("pre", 4 * 60, 9 * 60 + 30),
    ("regular", 9 * 60 + 30, 16 * 60),
    ("post", 16 * 60, 20 * 60),
)
"""The New York minutes after midnight each part of the extended session spans: a bar opening at or after
the first and before the second is in it."""
LOAD_ERRORS = (RuntimeError, ValueError, LookupError, OSError)
"""What `load_bars` raises for a load that cannot be made or did not reach the catalog (`TimeoutError` is
an `OSError`)."""


def read_list(path: Path) -> tuple[str, Path, list[tuple[BarType, datetime, datetime]]]:
    """The feed, report path and (bar type, start, end) entries of the list at `path`."""
    path = path.resolve()
    if path.is_relative_to(REPO):
        raise ValueError(f"{path} is inside the repository; the load list stays outside it")
    spec = json.loads(path.read_text(encoding="utf-8"))
    feed = spec.get("feed", "sip")
    if feed not in {f.value for f in BARS_FEEDS}:
        raise ValueError(f"feed {feed!r} is not one a bars request takes")
    report = (path.parent / spec["report"]).resolve()
    if report.is_relative_to(REPO):
        raise ValueError(f"{report} is inside the repository; the report stays outside it")
    entries = []
    for entry in spec["loads"]:
        symbol, bar = entry["symbol"], entry["bar"]
        if not SYMBOL.fullmatch(symbol):
            raise ValueError(f"{symbol!r} is not a US equity symbol")
        if bar not in BARS:
            raise ValueError(f"bar {bar!r} is not one of {', '.join(BARS)}")
        times = [datetime.fromisoformat(entry[k]) for k in ("start", "end")]
        if any(t.tzinfo is not None for t in times):
            raise ValueError(
                f"{symbol}: times are New York wall clock, with no offset of their own"
            )
        start, end = (t.replace(tzinfo=NEW_YORK) for t in times)
        entries.append((BarType.from_str(f"{symbol}.{VENUE}-{bar}-LAST-EXTERNAL"), start, end))
    return feed, report, entries


def cut(bar_type: BarType, start: datetime, end: datetime, now: datetime) -> datetime | None:
    """`end`, cut to `load_bars`' margin before `now` less a minute; None if nothing is left."""
    margin = max(timedelta(microseconds=bar_interval_ns(bar_type) // 1_000), PUBLICATION_DELAY)
    latest = (now - margin - timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = min(end, latest)
    return end if end > start else None


def by_day(root: Path, bar_type: BarType, start: datetime, end: datetime) -> dict[str, Any]:
    """The bars the root holds in the window, by New York day of their open, as the catalog reads them."""
    bars = ParquetDataCatalog(str(root)).query_bars(
        [str(bar_type)], start=dt_to_unix_nanos(start), end=dt_to_unix_nanos(end)
    )
    interval = bar_interval_ns(bar_type)
    days: dict[date, list[datetime]] = defaultdict(list)
    for bar in bars:
        opened = datetime.fromtimestamp((bar.ts_init - interval) / 1e9, UTC).astimezone(NEW_YORK)
        days[opened.date()].append(opened)
    minute = "-1-MINUTE-" in str(bar_type)
    first = (start - timedelta(days=1)).date()
    calendar = [first + timedelta(days=n) for n in range((end.date() - first).days + 1)]
    expected = [d for d in calendar if d.weekday() < 5 and in_window(d, minute, start, end)]
    table = {}
    for day in sorted(days):
        opens = days[day]
        row: dict[str, Any] = {"bars": len(opens)}
        if minute:
            mins = [o.hour * 60 + o.minute for o in opens]
            row |= {name: sum(lo <= m < hi for m in mins) for name, lo, hi in PARTS}
            pre = [o for o, m in zip(opens, mins, strict=True) if m < 9 * 60 + 30]
            row["first_pre"] = pre[0].strftime("%H:%M") if pre else None
            row["last_pre"] = pre[-1].strftime("%H:%M") if pre else None
        table[day.isoformat()] = row
    return {
        "bars": len(bars),
        "days": table,
        "missing_days": [d.isoformat() for d in expected if d not in days],
    }


def in_window(day: date, minute: bool, start: datetime, end: datetime) -> bool:
    """Whether a bar of `day` could have its `ts_init` in `[start, end]`: for minute bars, any of the
    extended session's (04:01 to 20:00 New York), for a daily bar, its one (midnight after `day`)."""
    if minute:
        lo = datetime(day.year, day.month, day.day, 4, 1, tzinfo=NEW_YORK)
        hi = datetime(day.year, day.month, day.day, 20, tzinfo=NEW_YORK)
    else:
        lo = hi = datetime.combine(day + timedelta(days=1), datetime.min.time(), NEW_YORK)
    return lo <= end and hi >= start


def run(list_path: Path, minutes: int) -> int:
    """Load every entry of the list at `list_path`, write its report, and return the exit status."""
    feed, report_path, entries = read_list(list_path)
    root = ROOTS / feed
    began = time.monotonic()
    last = began - 60
    rows: list[dict[str, Any]] = []
    for bar_type, start, asked_end in entries:
        row: dict[str, Any] = {
            "bar_type": str(bar_type),
            "asked": [start.isoformat(), asked_end.isoformat()],
        }
        rows.append(row)
        if time.monotonic() - began > minutes * 60:
            row["status"] = "not started"
            continue
        end = cut(bar_type, start, asked_end, datetime.now(UTC))
        if end is None:
            row["status"] = "not yet loadable"
            continue
        row["window"] = [start.isoformat(), end.isoformat()]
        if (wait := last + 60 / LOADS_PER_MINUTE - time.monotonic()) > 0:
            time.sleep(wait)
        t0 = last = time.monotonic()
        try:
            load_bars(bar_type, start, end, root, timeout_secs=LOAD_TIMEOUT_SECS, feed=feed)
            counted = by_day(root, bar_type, start, end)
        except LOAD_ERRORS as exc:  # each load is reported, and the exit status says one failed
            row |= {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        else:
            row |= counted
            row["status"] = "loaded" if row["bars"] else "missing"
        row["secs"] = round(time.monotonic() - t0, 2)
        print(json.dumps({k: v for k, v in row.items() if k != "days"}), flush=True)
        write_report(report_path, feed, root, began, rows)
    report = write_report(report_path, feed, root, began, rows)
    print(
        f"report: {report_path}; {report['loads']} loads in {report['elapsed_secs']} s", flush=True
    )
    return 1 if report["failed"] or report["not_started"] else 0


def write_report(
    report_path: Path, feed: str, root: Path, began: float, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Write the report of `rows` so far, a row not yet ended counted under none of its lists."""
    elapsed = time.monotonic() - began
    report = {
        "feed": feed,
        "catalog": str(root),
        "written": datetime.now(UTC).isoformat(),
        "elapsed_secs": round(elapsed, 1),
        "loads": sum("secs" in r for r in rows),
        **{
            key: [r["bar_type"] for r in rows if r.get("status") == status]
            for key, status in (
                ("missing", "missing"),
                ("failed", "failed"),
                ("not_yet", "not yet loadable"),
                ("not_started", "not started"),
            )
        },
        "rows": rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="canon_trading.bars_list", description="Load a list of bar windows into the catalog."
    )
    parser.add_argument(
        "--minutes", type=int, default=10, help=f"no entry starts after this, 1 to {MAX_MINUTES}"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be 1 to {MAX_MINUTES}")
    if not os.environ.get(LIST_ENV):
        parser.error(f"{LIST_ENV} must name a load list outside the repository")
    return run(Path(os.environ[LIST_ENV]), args.minutes)


if __name__ == "__main__":
    sys.exit(main())
