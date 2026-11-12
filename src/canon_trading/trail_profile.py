"""T5 step 1, the owner's per-symbol trail-profile contract (2026-11-12), local catalogs only.

Upstream contracts read at the pins: NautilusTrader v2.0.0rc8 ParquetDataCatalog.instruments and
query_bars (persistence/__init__.pyi; catalog/types.rs: inclusive ts_init bounds); CPython 3.14.10
statistics.quantiles(method="inclusive"). Reuse movers_baseline's catalog validation/lock and
movers_rank's session bounds, bar validation and 96-segment clock, plus its atomic JSON writer.
No client, network call, Alpaca request, credential or execution object is constructed here.

The ordered sessions file is an explicit calendar, never inferred from absent bars. Every set uses
only dates strictly before as_of. Recent means its latest RECENT_SESSIONS dates, read from sip-history-20
and the overnight root. Long means all supplied earlier dates, read from sip-history-dev through
2026-10-15, together with the recent root's window and overnight. Refuse any long date after
2026-10-15 unless it is in that recent window; never query dev after that cutoff, even for the recent
exception. Queries cover individual allowed sessions, never a span over a holdout or as_of date.
For a replay the reader requires a target as_of and refuses a profile whose as_of is later.
The live default is the first supplied session whose 20:00 New York end is still in the future.

Price basis is the previous observed minute close within the same 20:00-to-20:00 session, including
across a no-trade gap or the boats/sip boundary. The first bar has no true-range observation; neither
previous close nor a run is carried across sessions. True range / price is
max(high-low, abs(high-previous_close), abs(low-previous_close)) / previous_close.

A rising run starts with the bar immediately before a strictly higher close (the anchor). Until
that rise, each bar replaces the anchor. The run fails when a close is at or below the anchor close;
an unfinished run at session end also contributes nothing. A dip requires a strictly lower close
than the previous bar, after the run started. The new high is the first subsequent bar whose high
strictly exceeds every earlier high in that run, while its close remains above the anchor. A lower
close and breakout in the same bar do not confirm a dip: OHLC cannot establish their order.
The dip depth is the largest close-to-low drawdown from run start through the confirming bar:
max(max(0, highest earlier close in the run - bar.low) / previous_close). Thus its numerator uses the
running close peak and its denominator always uses that bar's previous close, NOT the current price.
No within-bar order is inferred; the confirming bar's low is included. Count one observation in the
confirming bar's segment, then make that bar the next anchor: runs do not overlap. A dip that never
makes a new high, or fails at the anchor first, is not counted. This definition is descriptive and
UNCALIBRATED, not a trail setting; integration into the trail is a separate step.

Each statistic uses CPython's inclusive linear percentiles, with no smoothing or borrowing. Fewer
than MIN_SAMPLES observations makes that metric thin and all its percentiles null; a segment is thin
if either metric is thin. Counts remain visible, including bars without a previous close and distinct
sessions with a bar. Missing catalog coverage is never declared a zero observation or borrowed from
another name/segment. Both feeds are required and restricted to their own clock hours. Overlapping
dev/recent bars must agree exactly; identical duplicates count once. A failed read keeps the old file.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from statistics import quantiles
from typing import Any

from canon_alpaca.constants import VENUE
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import history_load
from canon_trading import movers_baseline
from canon_trading import movers_rank
from canon_trading.bars_to_catalog import catalog_feed
from canon_trading.rank_shared import write_atomically

FORMAT = "canon-trail-profile/1"
RECENT_SESSIONS = 20
MIN_SAMPLES = 20
"""Minimum observations per metric and segment (UNCALIBRATED), not a calibrated trading threshold."""
LONG_CUTOFF = date(2026, 10, 15)
DEFAULT_RECENT_ROOT = movers_baseline.DEFAULT_CATALOG / "sip-history-20"
DEFAULT_LONG_ROOT = movers_baseline.DEFAULT_CATALOG / "sip-history-dev"
DEFAULT_OVERNIGHT_ROOT = movers_baseline.DEFAULT_CATALOG / "boats-history-20"
TRUE_RANGE_DEFINITION = (
    "max(high-low, abs(high-previous_close), abs(low-previous_close)) / previous_close; "
    "previous observed minute close in the same session; first bar omitted"
)
PULLBACK_DEFINITION = (
    __doc__.split("A rising run starts", 1)[1].split("Each statistic uses", 1)[0].strip()
)
PULLBACK_DEFINITION = "A rising run starts " + PULLBACK_DEFINITION
HOLDOUT_RULE = (
    "long dates after 2026-10-15 refused unless in recent's latest 20 supplied sessions before as_of; "
    "dev never queried after 2026-10-15; all sets strictly before as_of"
)
PERCENTILE_METHOD = "CPython statistics.quantiles(n=100, method='inclusive')"


def _calendar(sessions: Sequence[date]) -> tuple[date, ...]:
    if not sessions or list(sessions) != sorted(set(sessions)):
        raise history_load.HistoryStopped(
            "session calendar must be nonempty distinct dates in order"
        )
    return tuple(sessions)


def coming_session(sessions: Sequence[date], *, now: datetime | None = None) -> date:
    """Coming trade date from the supplied calendar, without inventing an exchange holiday."""
    current = now or history_load.utc_now()
    if current.tzinfo is None:
        raise history_load.HistoryStopped("coming session needs a timezone-aware current time")
    for day in _calendar(sessions):
        if datetime.combine(day, movers_rank.PART_END, movers_rank.NEW_YORK) > current:
            return day
    raise history_load.HistoryStopped("no coming trade date in supplied session calendar")


def select_sessions(sessions: Sequence[date], *, as_of: date) -> dict[str, tuple[date, ...]]:
    earlier = tuple(day for day in _calendar(sessions) if day < as_of)
    if not earlier:
        raise history_load.HistoryStopped(f"no supplied session strictly before as_of {as_of}")
    recent = earlier[-RECENT_SESSIONS:]
    for day in earlier:
        if day > LONG_CUTOFF and day not in recent:
            raise history_load.HistoryStopped(
                f"holdout session {day} in long is after {LONG_CUTOFF} and outside recent window"
            )
    return {"recent": recent, "long": earlier}


def percentiles(values: Sequence[float]) -> dict[str, float]:
    cuts = quantiles(values, n=100, method="inclusive")
    return {"median": cuts[49], "p75": cuts[74], "p90": cuts[89]}


def true_range_fraction(bar: Bar, previous_close: float) -> float:
    if not math.isfinite(previous_close) or previous_close <= 0:
        raise history_load.HistoryStopped("true range needs a finite positive previous close")
    high, low = bar.high.as_double(), bar.low.as_double()
    return max(high - low, abs(high - previous_close), abs(low - previous_close)) / previous_close


def pullback_samples(bars: Sequence[Bar]) -> list[tuple[int, float]]:
    """(Confirming bar index, drawdown fraction) under the run definition in the module header."""
    if not bars:
        return []
    anchor = previous = peak_close = bars[0].close.as_double()
    peak_high = bars[0].high.as_double()
    rising = dipped = False
    depth = 0.0
    result = []
    for index, bar in enumerate(bars[1:], 1):
        close, high, low = bar.close.as_double(), bar.high.as_double(), bar.low.as_double()
        if close <= anchor:
            anchor = peak_close = close
            peak_high, depth, rising, dipped = high, 0.0, False, False
        else:
            depth = max(depth, max(0.0, peak_close - low) / previous)
            # Check before setting dipped: same-bar decline + breakout has no known order.
            if rising and dipped and high > peak_high:
                result.append((index, depth))
                anchor = peak_close = close
                peak_high, depth, rising, dipped = high, 0.0, False, False
            else:
                dipped = dipped or (rising and close < previous)
                rising = True
                peak_high = max(peak_high, high)
                peak_close = max(peak_close, close)
        previous = close
    return result


def _metric(values: Sequence[float]) -> dict[str, Any]:
    thin = len(values) < MIN_SAMPLES
    return {
        "count": len(values),
        "thin": thin,
        **({"median": None, "p75": None, "p90": None} if thin else percentiles(values)),
    }


def segment_profile(sessions: Mapping[date, Sequence[Bar]], *, as_of: date) -> list[dict[str, Any]]:
    """One symbol's raw observations by segment, ignoring every date on or after as_of."""
    counts = [0] * movers_rank.SEGMENTS
    traded = [0] * movers_rank.SEGMENTS
    ranges: list[list[float]] = [[] for _ in counts]
    dips: list[list[float]] = [[] for _ in counts]
    bar_type: BarType | None = None
    for day in sorted(day for day in sessions if day < as_of):
        sample = movers_rank.check_bars(sessions[day], day, bar_type=bar_type)
        previous = None
        segments = []
        for bar in sample:
            bar_type = bar.bar_type
            start, end = movers_rank.bar_bounds_ns(bar)
            if end - start != movers_baseline.MINUTE_NS:
                raise history_load.HistoryStopped(f"{bar.bar_type} is not a one-minute bar")
            if any(
                not math.isfinite(p.as_double()) or p.as_double() <= 0
                for p in (bar.open, bar.high, bar.low, bar.close)
            ):
                raise history_load.HistoryStopped(
                    f"{bar.bar_type} has nonpositive/nonfinite prices"
                )
            k = (start - movers_rank.part_start_ns(day)) // movers_rank.SEGMENT_NS
            segments.append(k)
            counts[k] += 1
            if previous is not None:
                ranges[k].append(true_range_fraction(bar, previous))
            previous = bar.close.as_double()
        for k in set(segments):
            traded[k] += 1
        for index, depth in pullback_samples(sample):
            dips[segments[index]].append(depth)
    rows = []
    for k in range(movers_rank.SEGMENTS):
        tr, dip = _metric(ranges[k]), _metric(dips[k])
        rows.append(
            {
                "bars": counts[k],
                "sessions": traded[k],
                "thin": tr["thin"] or dip["thin"],
                "true_range": tr,
                "pullback": dip,
            }
        )
    return rows


def _thin_rule() -> dict[str, Any]:
    return {
        "minimum_samples": MIN_SAMPLES,
        "calibration": "UNCALIBRATED",
        "metric": "count < minimum_samples: thin, null percentiles; no smoothing or borrowing",
        "segment": "thin if either true_range or pullback is thin",
    }


def _read_bars(
    catalog: ParquetDataCatalog,
    identifiers: Sequence[str],
    day: date,
    feed: str,
) -> list[Bar]:
    boundary = movers_rank.ny_ns(day, movers_rank.DAY_START)
    start, end = (
        (boundary, movers_rank.ny_ns(day, movers_rank.PART_END))
        if feed == "sip"
        else (movers_rank.part_start_ns(day), boundary)
    )
    return catalog.query_bars(
        identifiers=identifiers,
        start=start + movers_baseline.MINUTE_NS,
        end=end,
    )


def build_profile(
    *,
    sessions: Sequence[date],
    as_of: date | None = None,
    recent_root: Path = DEFAULT_RECENT_ROOT,
    long_root: Path = DEFAULT_LONG_ROOT,
    overnight_root: Path = DEFAULT_OVERNIGHT_ROOT,
    baseline_dir: Path = movers_baseline.DEFAULT_BASELINE_DIR,
) -> Path:
    """Write the dated v1 profile under all catalog locks, or stop with the old file intact."""
    target = as_of or coming_session(sessions)
    days = select_sessions(sessions, as_of=target)  # refusal BEFORE catalog construction/query
    roots = [movers_baseline._outside_ledger(p) for p in (recent_root, long_root, overnight_root)]
    output = movers_baseline._outside_ledger(baseline_dir) / "trail-profile-v1.json"
    if len(set(roots)) != 3:
        raise history_load.HistoryStopped(
            "recent, long and overnight need three distinct catalog roots"
        )
    operation = "opening trail-profile catalogs"
    try:
        with ExitStack() as stack:
            catalogs = []
            instruments = []
            for root, feed in zip(roots, ("sip", "sip", "boats"), strict=True):
                if not root.is_dir():
                    raise history_load.HistoryStopped(
                        f"catalog {root} must be an existing directory"
                    )
                stack.enter_context(history_load._catalog_lock(root))
                if catalog_feed(root) != feed:
                    raise history_load.HistoryStopped(f"catalog {root} must name feed {feed}")
                catalog = ParquetDataCatalog(str(root))
                versions = {
                    str(instrument.id): instrument
                    for instrument in catalog.instruments()
                    if isinstance(instrument, Equity) and instrument.id.venue == VENUE
                }
                if not versions:
                    raise history_load.HistoryStopped(
                        f"no ALPACA equity definitions in catalog {root}"
                    )
                catalogs.append(catalog)
                instruments.append(versions)
            symbols: dict[str, Any] = {}
            ids = sorted(set().union(*(set(versions) for versions in instruments)))
            # Preserve the baseline's approximate batch bar bound over a longer history.
            batch_size = max(
                1,
                movers_baseline.QUERY_SYMBOLS * movers_rank.BASELINE_SESSIONS // len(days["long"]),
            )
            for first in range(0, len(ids), batch_size):
                batch = ids[first : first + batch_size]
                histories: dict[str, dict[str, dict[date, dict[int, Bar]]]] = {
                    name: {identifier: {day: {} for day in selected} for identifier in batch}
                    for name, selected in days.items()
                }
                plan = (
                    days["recent"],
                    tuple(day for day in days["long"] if day <= LONG_CUTOFF),
                    days["long"],
                )
                for source_index, (catalog, versions, selected, feed, root) in enumerate(
                    zip(
                        catalogs,
                        instruments,
                        plan,
                        ("sip", "sip", "boats"),
                        roots,
                        strict=True,
                    )
                ):
                    identifiers = [
                        f"{identifier}-1-MINUTE-LAST-EXTERNAL"
                        for identifier in batch
                        if identifier in versions
                    ]
                    if not identifiers:
                        continue
                    for day in selected:
                        operation = f"reading {feed} session {day} in catalog {root}"
                        for bar in _read_bars(catalog, identifiers, day, feed):
                            identifier = str(bar.bar_type.instrument_id)
                            start, end = movers_rank.bar_bounds_ns(bar)
                            boundary = movers_rank.ny_ns(day, movers_rank.DAY_START)
                            lo, hi = (
                                (boundary, movers_rank.ny_ns(day, movers_rank.PART_END))
                                if feed == "sip"
                                else (movers_rank.part_start_ns(day), boundary)
                            )
                            if not lo <= start < end <= hi:
                                raise history_load.HistoryStopped(
                                    f"{operation}: bar outside feed hours"
                                )
                            for name, selected_days in days.items():
                                if day not in selected_days or (
                                    name == "recent" and source_index == 1
                                ):
                                    continue
                                held = histories[name][identifier][day]
                                if start in held and held[start] != bar:
                                    raise history_load.HistoryStopped(
                                        f"{operation}: conflicting duplicate {identifier} at {start}"
                                    )
                                held[start] = bar
                for identifier in batch:
                    symbol = identifier.removesuffix(f".{VENUE}")
                    symbols[symbol] = {
                        name: segment_profile(
                            {
                                day: sorted(held.values(), key=lambda b: b.ts_event)
                                for day, held in history[identifier].items()
                            },
                            as_of=target,
                        )
                        for name, history in histories.items()
                    }
            document = {
                "format": FORMAT,
                "as_of": target.isoformat(),
                "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "sessions": {
                    name: [day.isoformat() for day in selected] for name, selected in days.items()
                },
                "roots": {
                    "recent": [str(roots[0]), str(roots[2])],
                    "long": [str(roots[1]), str(roots[0]), str(roots[2])],
                },
                "feeds": ["sip", "boats"],
                "price_basis": "previous close",
                "true_range_definition": TRUE_RANGE_DEFINITION,
                "pullback_definition": PULLBACK_DEFINITION,
                "holdout_rule": HOLDOUT_RULE,
                "thin_rule": _thin_rule(),
                "percentile_method": PERCENTILE_METHOD,
                "part_start": "20:00",
                "segment_minutes": movers_rank.SEGMENT_MINUTES,
                "segments": movers_rank.SEGMENTS,
                "timezone": "America/New_York",
                "symbols": symbols,
            }
            for root, feed in zip(roots, ("sip", "sip", "boats"), strict=True):
                if catalog_feed(root) != feed:
                    raise history_load.HistoryStopped(f"catalog feed changed during {operation}")
            operation = f"writing trail profile {output}"
            output.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(output, json.dumps(document, allow_nan=False))
        return output
    except history_load.HistoryStopped:
        raise
    except Exception as exc:
        raise history_load.HistoryStopped(
            f"{operation} failed ({type(exc).__name__}): {exc}"
        ) from exc


def read_profile(path: Path, *, as_of: date) -> dict[str, Any]:
    """Read only a fully marked v1 file safe for the target replay/trade date, or refuse by name."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "format": FORMAT,
            "feeds": ["sip", "boats"],
            "price_basis": "previous close",
            "true_range_definition": TRUE_RANGE_DEFINITION,
            "pullback_definition": PULLBACK_DEFINITION,
            "holdout_rule": HOLDOUT_RULE,
            "thin_rule": _thin_rule(),
            "percentile_method": PERCENTILE_METHOD,
            "part_start": "20:00",
            "segment_minutes": movers_rank.SEGMENT_MINUTES,
            "segments": movers_rank.SEGMENTS,
            "timezone": "America/New_York",
        }
        if not isinstance(document, dict):
            raise TypeError("header must be a JSON object")
        for key, value in expected.items():
            if document.get(key) != value:
                raise ValueError(f"header {key} missing or incompatible")
        for key in ("as_of", "written_at", "sessions", "roots"):
            if key not in document:
                raise ValueError(f"header {key} missing")
        built_for = date.fromisoformat(document["as_of"])
        if built_for > as_of:
            raise ValueError(f"header as_of {built_for} is later than replay date {as_of}")
        if datetime.fromisoformat(document["written_at"]).tzinfo is None:
            raise ValueError("header written_at needs a timezone")
        sessions = document["sessions"]
        if not isinstance(sessions, dict) or set(sessions) != {"recent", "long"}:
            raise ValueError("header sessions must name recent and long")
        parsed = {
            name: tuple(date.fromisoformat(day) for day in selected)
            for name, selected in sessions.items()
        }
        selected = select_sessions(parsed["long"], as_of=built_for)
        if parsed != selected:
            raise ValueError("header sessions violate as_of, recent window or holdout rule")
        roots = document["roots"]
        if not isinstance(roots, dict) or set(roots) != {"recent", "long"}:
            raise ValueError("header roots must name recent and long")
        for name, count in (("recent", 2), ("long", 3)):
            if (
                not isinstance(roots[name], list)
                or len(roots[name]) != count
                or any(
                    not isinstance(root, str) or not Path(root).is_absolute()
                    for root in roots[name]
                )
            ):
                raise ValueError(f"header roots.{name} needs {count} absolute local paths")
        _check_symbols(document["symbols"], parsed)
        return document
    except (OSError, ValueError, TypeError, KeyError, history_load.HistoryStopped) as exc:
        raise history_load.HistoryStopped(
            f"{path}: trail profile header/data refused: {exc}"
        ) from exc


def _check_symbols(symbols: Any, sessions: Mapping[str, Sequence[date]]) -> None:
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("symbols must be a nonempty object")
    for symbol, sets in symbols.items():
        if not isinstance(symbol, str) or not isinstance(sets, dict) or set(sets) != set(sessions):
            raise ValueError(f"invalid symbol sets for {symbol}")
        for name, rows in sets.items():
            if not isinstance(rows, list) or len(rows) != movers_rank.SEGMENTS:
                raise ValueError(f"{symbol}/{name} needs 96 segments")
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError(f"{symbol}/{name} has a malformed segment")
                n, b = row["sessions"], row["bars"]
                if (
                    type(n) is not int
                    or type(b) is not int
                    or not 0 <= n <= len(sessions[name])
                    or b < n
                ):
                    raise ValueError(f"{symbol}/{name} has invalid session/bar counts")
                for key in ("true_range", "pullback"):
                    metric = row[key]
                    count = metric["count"]
                    if type(count) is not int or not 0 <= count <= b:
                        raise ValueError(f"{symbol}/{name}/{key} has invalid count")
                    thin = count < MIN_SAMPLES
                    if type(metric["thin"]) is not bool or metric["thin"] != thin:
                        raise ValueError(f"{symbol}/{name}/{key} has invalid thin flag")
                    values = [metric[k] for k in ("median", "p75", "p90")]
                    if thin:
                        if values != [None, None, None]:
                            raise ValueError(
                                f"{symbol}/{name}/{key} borrows values in a thin segment"
                            )
                    elif any(
                        type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values
                    ):
                        raise ValueError(f"{symbol}/{name}/{key} has invalid percentiles")
                    elif values != sorted(values):
                        raise ValueError(f"{symbol}/{name}/{key} percentiles are out of order")
                if type(row["thin"]) is not bool or row["thin"] != (
                    row["true_range"]["thin"] or row["pullback"]["thin"]
                ):
                    raise ValueError(f"{symbol}/{name} has invalid segment thin flag")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.trail_profile", description=__doc__
    )
    parser.add_argument(
        "--sessions-file",
        type=Path,
        required=True,
        help="ordered ISO session dates, one per line; # comments allowed",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, help="default: coming supplied trade date"
    )
    parser.add_argument("--recent-root", type=Path, default=DEFAULT_RECENT_ROOT)
    parser.add_argument("--long-root", type=Path, default=DEFAULT_LONG_ROOT)
    parser.add_argument("--overnight-root", type=Path, default=DEFAULT_OVERNIGHT_ROOT)
    parser.add_argument("--baseline-dir", type=Path, default=movers_baseline.DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    try:
        sessions = [
            date.fromisoformat(line.strip())
            for line in args.sessions_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        output = build_profile(
            sessions=sessions,
            as_of=args.as_of,
            recent_root=args.recent_root,
            long_root=args.long_root,
            overnight_root=args.overnight_root,
            baseline_dir=args.baseline_dir,
        )
    except (history_load.HistoryStopped, OSError, ValueError) as exc:
        print(f"trail-profile: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"trail-profile: wrote {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
