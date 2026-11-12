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
exception. Each root is read with one query per symbol batch, over the span from its first allowed
session's start to its last's end; a bar is counted only in the allowed session whose part holds it, so a
bar on a date not supplied is never counted. A span never crosses as_of. The registered holdout list
(--holdout-file; its path, whole-file sha256 and dates-only sha256, the sorted ISO dates each ending
in a newline, in the header) makes the default builder, the one any replay uses,
refuse by name, before any query, a span that contains a holdout date: a span over a holdout is never
issued. Only build_live_profile (CLI --live-file), for the live file that sizes stops on later trading
days, may read holdout dates; its header records live_file true and the holdout dates it read
(the lead's ruling of 2026-11-12).
For a replay the reader requires a target as_of and refuses a profile whose as_of is later.
An explicit absent long (long_root None, CLI --long-absent; the coordinator's order of 2026-11-12) writes
the header field windows = "recent only; long absent", sessions and roots with recent alone, and a null
long for every symbol; it locks and reads only the recent and overnight roots. The full shape carries
no windows field, and the reader refuses any mix of the two.
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
import hashlib
import json
import math
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
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
LONG_ABSENT = "recent only; long absent"


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


def _session_bounds(day: date, feed: str) -> tuple[int, int]:
    """The feed's clock hours of one session: sip 04:00-20:00, boats the night before until 04:00."""
    boundary = movers_rank.ny_ns(day, movers_rank.DAY_START)
    if feed == "sip":
        return boundary, movers_rank.ny_ns(day, movers_rank.PART_END)
    return movers_rank.part_start_ns(day), boundary


def read_holdout(path: Path) -> tuple[frozenset[date], str]:
    """The registered holdout dates, one ISO date a line ('#' comments allowed), and the file's sha256."""
    try:
        raw = path.read_bytes()
        dates = frozenset(
            date.fromisoformat(line.strip())
            for line in raw.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except (OSError, ValueError) as exc:
        raise history_load.HistoryStopped(f"holdout file {path} unreadable: {exc}") from exc
    if not dates:
        raise history_load.HistoryStopped(f"holdout file {path} lists no date")
    return dates, hashlib.sha256(raw).hexdigest()


def _runs(days: Sequence[date], recent: Sequence[date]) -> list[tuple[date, ...]]:
    """`days` (sorted) cut into those before the recent window and the window itself: a run never spans
    the dates between dev's end and a later recent window, which select_sessions excludes from long."""
    window = set(recent)
    return [
        run
        for run in (
            tuple(d for d in days if d not in window),
            tuple(d for d in days if d in window),
        )
        if run
    ]


def _read_bars(
    catalog: ParquetDataCatalog,
    identifiers: Sequence[str],
    days: Sequence[date],
    feed: str,
    recent: Sequence[date],
) -> list[tuple[date, Bar]]:
    """One query per run of `days` (sorted) over its span, each bar paired with the session whose part
    (20:00 to 20:00 New York) holds its open. A bar in an allowed session outside the feed's hours is
    refused; a bar in a session not allowed, such as a date not supplied, is dropped."""
    allowed = set(days)
    held = []
    for run in _runs(days, recent):
        for bar in catalog.query_bars(
            identifiers=identifiers,
            start=_session_bounds(run[0], feed)[0] + movers_baseline.MINUTE_NS,
            end=_session_bounds(run[-1], feed)[1],
        ):
            start, end = movers_rank.bar_bounds_ns(bar)
            opened = movers_rank.ny_time(start)
            day = opened.date() + timedelta(days=opened.time() >= movers_rank.PART_START)
            if day not in allowed:
                continue
            lo, hi = _session_bounds(day, feed)
            if not lo <= start < end <= hi:
                raise history_load.HistoryStopped(
                    f"{feed} bar {bar.bar_type} opened {opened.isoformat()}: bar outside feed hours"
                )
            held.append((day, bar))
    return held


def build_profile(
    *,
    sessions: Sequence[date],
    holdout_file: Path,
    as_of: date | None = None,
    recent_root: Path = DEFAULT_RECENT_ROOT,
    long_root: Path | None = DEFAULT_LONG_ROOT,
    overnight_root: Path = DEFAULT_OVERNIGHT_ROOT,
    baseline_dir: Path = movers_baseline.DEFAULT_BASELINE_DIR,
) -> Path:
    """The default and replay builder: refuses any span that contains a registered holdout date."""
    return _build(
        sessions=sessions,
        holdout_file=holdout_file,
        as_of=as_of,
        recent_root=recent_root,
        long_root=long_root,
        overnight_root=overnight_root,
        baseline_dir=baseline_dir,
        live_file=False,
    )


def build_live_profile(
    *,
    sessions: Sequence[date],
    holdout_file: Path,
    as_of: date | None = None,
    recent_root: Path = DEFAULT_RECENT_ROOT,
    long_root: Path | None = DEFAULT_LONG_ROOT,
    overnight_root: Path = DEFAULT_OVERNIGHT_ROOT,
    baseline_dir: Path = movers_baseline.DEFAULT_BASELINE_DIR,
) -> Path:
    """The live file only: may read holdout dates, as it sizes stops on later trading days; its header
    says so and names each holdout date it read."""
    return _build(
        sessions=sessions,
        holdout_file=holdout_file,
        as_of=as_of,
        recent_root=recent_root,
        long_root=long_root,
        overnight_root=overnight_root,
        baseline_dir=baseline_dir,
        live_file=True,
    )


def _build(
    *,
    sessions: Sequence[date],
    holdout_file: Path,
    live_file: bool,
    as_of: date | None = None,
    recent_root: Path = DEFAULT_RECENT_ROOT,
    long_root: Path | None = DEFAULT_LONG_ROOT,
    overnight_root: Path = DEFAULT_OVERNIGHT_ROOT,
    baseline_dir: Path = movers_baseline.DEFAULT_BASELINE_DIR,
) -> Path:
    """Write the dated v1 profile under the locks of the roots it reads, or stop with the old file
    intact. long_root None is an explicit absent long: no dev lock, read or long row."""
    target = as_of or coming_session(sessions)
    days = select_sessions(sessions, as_of=target)  # refusal BEFORE catalog construction/query
    long_absent = long_root is None
    if long_absent:
        del days["long"]
    holdout, holdout_sha256 = read_holdout(holdout_file)
    read_days = days.get("long", days["recent"])
    spans = {
        "sip": days["recent"],
        "dev": tuple(day for day in read_days if day <= LONG_CUTOFF),
        "boats": read_days,
    }
    holdout_read = sorted(
        {
            day
            for selected in spans.values()
            for run in _runs(selected, days["recent"])
            for day in holdout
            if run[0] <= day <= run[-1]
        }
    )
    if holdout_read and not live_file:
        raise history_load.HistoryStopped(
            f"a span would contain holdout date {holdout_read[0]} (holdout file {holdout_file}); "
            "only the live file (--live-file) may read a holdout date"
        )
    sources = [
        (movers_baseline._outside_ledger(path), feed, dev)
        for path, feed, dev in (
            (recent_root, "sip", False),
            (long_root, "sip", True),
            (overnight_root, "boats", False),
        )
        if path is not None
    ]
    roots = [root for root, _, _ in sources]
    output = movers_baseline._outside_ledger(baseline_dir) / "trail-profile-v1.json"
    if len(set(roots)) != len(roots):
        raise history_load.HistoryStopped("recent, long and overnight need distinct catalog roots")
    operation = "opening trail-profile catalogs"
    try:
        with ExitStack() as stack:
            catalogs = []
            instruments = []
            for root, feed, _ in sources:
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
                movers_baseline.QUERY_SYMBOLS * movers_rank.BASELINE_SESSIONS // len(read_days),
            )
            for first in range(0, len(ids), batch_size):
                batch = ids[first : first + batch_size]
                histories: dict[str, dict[str, dict[date, dict[int, Bar]]]] = {
                    name: {identifier: {day: {} for day in selected} for identifier in batch}
                    for name, selected in days.items()
                }
                plan = [spans["dev" if dev else feed] for _, feed, dev in sources]
                for catalog, versions, selected, (root, feed, dev) in zip(
                    catalogs, instruments, plan, sources, strict=True
                ):
                    identifiers = [
                        f"{identifier}-1-MINUTE-LAST-EXTERNAL"
                        for identifier in batch
                        if identifier in versions
                    ]
                    if not identifiers or not selected:
                        continue
                    operation = f"reading {feed} sessions {selected[0]}..{selected[-1]} in {root}"
                    for day, bar in _read_bars(
                        catalog, identifiers, selected, feed, days["recent"]
                    ):
                        identifier = str(bar.bar_type.instrument_id)
                        start, _ = movers_rank.bar_bounds_ns(bar)
                        for name, selected_days in days.items():
                            if day not in selected_days or (name == "recent" and dev):
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
                    if long_absent:
                        symbols[symbol]["long"] = None
            document = {
                "format": FORMAT,
                "as_of": target.isoformat(),
                "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "sessions": {
                    name: [day.isoformat() for day in selected] for name, selected in days.items()
                },
                "roots": {
                    "recent": [str(roots[0]), str(roots[-1])],
                    **(
                        {}
                        if long_absent
                        else {"long": [str(roots[1]), str(roots[0]), str(roots[2])]}
                    ),
                },
                "feeds": ["sip", "boats"],
                "live_file": live_file,
                "holdout": {
                    "path": str(holdout_file),
                    "sha256": holdout_sha256,
                    "dates_sha256": hashlib.sha256(
                        "".join(f"{day.isoformat()}\n" for day in sorted(holdout)).encode()
                    ).hexdigest(),
                    "dates_read": [day.isoformat() for day in holdout_read],
                },
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
            if long_absent:
                document = {"windows": LONG_ABSENT, **document}
            for root, feed, _ in sources:
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
        for key in ("as_of", "written_at", "sessions", "roots", "live_file", "holdout"):
            if key not in document:
                raise ValueError(f"header {key} missing")
        if type(document["live_file"]) is not bool:
            raise ValueError("header live_file must be true or false")
        holdout = document["holdout"]
        if (
            not isinstance(holdout, dict)
            # dates_sha256 is optional: the 2026-11-12 09:30 file was written before it existed.
            or not {"path", "sha256", "dates_read"} <= set(holdout)
            or not set(holdout) <= {"path", "sha256", "dates_sha256", "dates_read"}
            or not isinstance(holdout["path"], str)
            or any(
                not isinstance(holdout.get(key, "0" * 64), str)
                or len(holdout.get(key, "0" * 64)) != 64
                or any(c not in "0123456789abcdef" for c in holdout.get(key, "0" * 64))
                for key in ("sha256", "dates_sha256")
            )
            or not isinstance(holdout["dates_read"], list)
            or not all(isinstance(day, str) for day in holdout["dates_read"])
            or (holdout["dates_read"] and not document["live_file"])
        ):
            raise ValueError("header holdout must name its file, sha256 and the dates read")
        built_for = date.fromisoformat(document["as_of"])
        if built_for > as_of:
            raise ValueError(f"header as_of {built_for} is later than replay date {as_of}")
        if datetime.fromisoformat(document["written_at"]).tzinfo is None:
            raise ValueError("header written_at needs a timezone")
        long_absent = "windows" in document
        if long_absent and document["windows"] != LONG_ABSENT:
            raise ValueError(f"header windows must be {LONG_ABSENT!r} or absent")
        names = {"recent"} if long_absent else {"recent", "long"}
        sessions = document["sessions"]
        if not isinstance(sessions, dict) or set(sessions) != names:
            raise ValueError(
                "header sessions must name recent alone"
                if long_absent
                else "header sessions must name recent and long"
            )
        parsed = {
            name: tuple(date.fromisoformat(day) for day in selected)
            for name, selected in sessions.items()
        }
        selected = select_sessions(parsed.get("long", parsed["recent"]), as_of=built_for)
        if long_absent:
            del selected["long"]
        if parsed != selected:
            raise ValueError("header sessions violate as_of, recent window or holdout rule")
        roots = document["roots"]
        if not isinstance(roots, dict) or set(roots) != names:
            raise ValueError("header roots must name the windows present")
        for name, count in (("recent", 2), ("long", 3))[: len(names)]:
            if (
                not isinstance(roots[name], list)
                or len(roots[name]) != count
                or any(
                    not isinstance(root, str) or not Path(root).is_absolute()
                    for root in roots[name]
                )
            ):
                raise ValueError(f"header roots.{name} needs {count} absolute local paths")
        _check_symbols(document["symbols"], parsed, long_absent=long_absent)
        return document
    except (OSError, ValueError, TypeError, KeyError, history_load.HistoryStopped) as exc:
        raise history_load.HistoryStopped(
            f"{path}: trail profile header/data refused: {exc}"
        ) from exc


def _check_symbols(
    symbols: Any, sessions: Mapping[str, Sequence[date]], *, long_absent: bool = False
) -> None:
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("symbols must be a nonempty object")
    for symbol, sets in symbols.items():
        if (
            not isinstance(symbol, str)
            or not isinstance(sets, dict)
            or set(sets) != {"recent", "long"}
            or (sets["long"] is None) != long_absent
        ):
            raise ValueError(f"invalid symbol sets for {symbol}")
        for name, rows in sets.items():
            if name not in sessions:
                continue
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
    parser.add_argument(
        "--holdout-file",
        type=Path,
        required=True,
        help="the registered holdout dates, one a line; a span containing one is refused",
    )
    parser.add_argument(
        "--live-file",
        action="store_true",
        help="the live file only: may read holdout dates, and its header says so (never a replay)",
    )
    parser.add_argument("--recent-root", type=Path, default=DEFAULT_RECENT_ROOT)
    long = parser.add_mutually_exclusive_group()
    long.add_argument("--long-root", type=Path, default=DEFAULT_LONG_ROOT)
    long.add_argument(
        "--long-absent",
        action="store_true",
        help=f"write '{LONG_ABSENT}': no dev root locked or read, a null long per symbol",
    )
    parser.add_argument("--overnight-root", type=Path, default=DEFAULT_OVERNIGHT_ROOT)
    parser.add_argument("--baseline-dir", type=Path, default=movers_baseline.DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    try:
        sessions = [
            date.fromisoformat(line.strip())
            for line in args.sessions_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        output = (build_live_profile if args.live_file else build_profile)(
            sessions=sessions,
            holdout_file=args.holdout_file,
            as_of=args.as_of,
            recent_root=args.recent_root,
            long_root=None if args.long_absent else args.long_root,
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
