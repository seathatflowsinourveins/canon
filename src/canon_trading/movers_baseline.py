"""Write ADR 0087's nightly movers baseline from one feed's local minute-bar catalog.

Contracts, read at the pins: NautilusTrader v2.0.0rc8's ParquetDataCatalog.instruments,
get_missing_intervals_for_request and query_bars (persistence/__init__.pyi and the catalog guide);
the adapter's parse_bar timestamps (ts_event=open, ts_init=end); history_load's advisory catalog
lock (Python 3.14 fcntl.flock, ADR 0093); and movers_rank.segment_baseline/write_baselines (ADR 0087).
The latter own the typical-price dollar-volume arithmetic, the 96 cumulative 15-minute segments
from 20:00 New York the evening before to 20:00 on the trade date (format version 2), the five-session
mean, each segment's traded-session count, and the actor's atomic file format.

The universe is every ALPACA equity definition in the root, one per id (the latest stored version),
which must hold the full asset list for a whole-market result. The caller supplies an ordered
exchange-session calendar including the coming trade date and at least BASELINE_SESSIONS prior
sessions. Missing bars never imply a holiday.

The feed's hours (`FEED_HOURS`): sip has no overnight session, so a sip session is the minute ends 04:01
through 20:00 New York inclusive, an early-close date included. A name is written when the catalog
covers those minute ends on each selected session; the night before 04:00 needs no coverage, and a bar
outside the feed's hours counts nowhere. The baseline's 32 overnight segments (20:00-04:00) are then
zero, and the header's `feeds` lists sip alone, so `read_baselines` reports no overnight dollars and the
ranking counts its day ratio from 04:00 (the lead's rule, ADR 0087, 2026-11-11). A covered session with
no bars contributes zero, as segment_baseline specifies. Each written name also carries, per segment,
the count of lookback sessions in which it traded there (`Baseline.traded_sessions`), and the file its
`lookback_sessions`: the lead's ADR 0087 definition establishes a segment's baseline only where the two
are equal.

A symbol whose catalog lacks any of the five sessions is left out of the file and listed under
`excluded` with the feed, the session and the first missing range. history_load writes nothing for an
empty answer (ADR 0093), so a name with no trade in a loaded range is never covered and can never be
written; a name left out gets no ratio, and the ranking excludes it by name (`ratio:unknown`). The
write stops by name, keeping the previous file, only when the share of distinct equities left out is
above `MAX_EXCLUDED_SHARE` (UNCALIBRATED). The file's header names its `source` (writer, feed, catalog,
sessions, equities, time written), `excluded_share` and `excluded`. Load a missing range with
history_load (GET only, its pacing and catalog lock, ADR 0093) and write again. No client is built and
no credential is read here. Legacy catalog writers do not share the history lock and must run
separately (ADR 0093).

Which sessions, per feed (the coordinator's rule, 2026-11-11): `sip` serves the pre-market, regular and
after-hours parts, and its baseline for trade date D is built from the five sip sessions before D, so it
can be written once D-1's 20:00 bars are published. `boats` serves the overnight part, whose 20:00-04:00
segments ADR 0087 now defines. history_load loads boats bars into `catalog/boats` (2026-11-13), but
this writer does not read a boats root yet, so `--feed boats` stays refused by name until it does (the
lead's ruling, 2026-11-11).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from collections.abc import Sequence
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path

from canon_alpaca.constants import VENUE
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import history_load
from canon_trading import movers_rank
from canon_trading.bars_to_catalog import DEFAULT_CATALOG
from canon_trading.bars_to_catalog import catalog_feed

FEEDS = ("sip", "boats")
DEFAULT_BASELINE_DIR = DEFAULT_CATALOG / "movers-baseline"
MINUTE_NS = 60 * 1_000_000_000
FEED_HOURS = {"sip": (movers_rank.DAY_START, movers_rank.PART_END)}
"""Each feed's hours, New York, on a trade date: what a session's coverage is checked over and the only
bars it counts. sip has no overnight session (ADR 0087's `DAY_START`)."""
QUERY_SYMBOLS = 200
"""Equities read in one catalog query, and between two progress lines of the command line's run; their
bars, at most five sessions of minutes each, bound the memory a batch holds."""
MAX_EXCLUDED_SHARE = 0.5
"""The most of the catalog's equities a written baseline may leave out (UNCALIBRATED): above it the
catalog is not loaded well enough to stand for the market, and nothing is written."""


def _sessions(calendar: Sequence[date], session: date) -> tuple[date, ...]:
    if list(calendar) != sorted(set(calendar)):
        raise history_load.HistoryStopped("the session calendar must be distinct dates in order")
    if session not in calendar:
        raise history_load.HistoryStopped(
            f"trade session {session} is not in the supplied calendar"
        )
    earlier = [day for day in calendar if day < session]
    if len(earlier) < movers_rank.BASELINE_SESSIONS:
        raise history_load.HistoryStopped(
            f"session {session} needs {movers_rank.BASELINE_SESSIONS} prior calendar sessions; "
            f"only {len(earlier)} supplied: no holiday or session is inferred from missing bars"
        )
    return tuple(earlier[-movers_rank.BASELINE_SESSIONS :])


def _outside_ledger(path: Path) -> Path:
    resolved = path.resolve()
    if "ledger" in path.parts or "ledger" in resolved.parts:
        raise history_load.HistoryStopped(
            f"{path} is under ledger/, which holds the trial ledger only"
        )
    return resolved


def build_baseline(
    *,
    session: date,
    sessions: Sequence[date],
    feed: str,
    catalog_path: Path,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    max_excluded_share: float = MAX_EXCLUDED_SHARE,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Write <baseline_dir>/<feed>.json for session, or stop by name without replacing it.

    Read one symbol's five sessions at a time, bounding bar memory independently of market size: one
    coverage read and one bar query for the span of the five, each session's minute ends then checked
    against the missing intervals. Hold the same catalog lock as history_load through coverage, reads
    and the final atomic write.
    """
    operation = f"baseline for session {session}, feed {feed}"
    try:
        if not 0 <= max_excluded_share < 1:
            raise history_load.HistoryStopped(
                f"the excluded share must be at least 0 and below 1, got {max_excluded_share!r}"
            )
        if feed not in FEEDS:
            raise history_load.HistoryStopped(f"baseline feed must be sip or boats, got {feed!r}")
        if feed not in FEED_HOURS:
            raise history_load.HistoryStopped(
                f"a {feed} baseline serves the overnight part, 20:00-04:00 New York, and needs a {feed} "
                "history loader's root read the way this writer reads sip; history_load now loads boats "
                "into catalog/boats, but this writer does not read it yet: refused until it does (the "
                "lead's ruling, 2026-11-11)"
            )
        days = _sessions(sessions, session)
        path = _outside_ledger(Path(catalog_path))
        output = _outside_ledger(Path(baseline_dir)) / f"{feed}.json"
        if not path.is_dir():
            raise history_load.HistoryStopped(
                f"the catalog at {path} must be an existing directory"
            )
        with history_load._catalog_lock(path):
            held = catalog_feed(path)
            if held != feed:
                raise history_load.HistoryStopped(
                    f"catalog {path} has feed {held!r}, requested {feed!r}: each feed needs its own root"
                )
            catalog = ParquetDataCatalog(str(path))
            # `instruments()` with no bounds returns every stored version, oldest first: the latest of
            # each id stands for it (NautilusTrader rc8 `catalog/types.rs`, as history_load keys them)
            versions = {
                instrument.id: instrument
                for instrument in catalog.instruments()
                if isinstance(instrument, Equity) and instrument.id.venue == VENUE
            }
            equities = sorted(versions.values(), key=lambda instrument: str(instrument.id))
            if not equities:
                raise history_load.HistoryStopped(f"no ALPACA equity definitions in catalog {path}")
            opens, closes = FEED_HOURS[feed]
            windows = {  # each session's minute ends in the feed's hours
                day: (
                    movers_rank.ny_ns(day, opens) + MINUTE_NS,
                    movers_rank.ny_ns(day, closes),
                )
                for day in days
            }
            latest = history_load.utc_now() - history_load.margin("1-MINUTE")
            for day, (_, end) in windows.items():
                if unix_nanos_to_dt(end) > latest:
                    raise history_load.HistoryStopped(
                        f"{feed} session {day} is not complete and published yet; no baseline written"
                    )
            span = (windows[days[0]][0], windows[days[-1]][1])
            baselines = {}
            excluded: dict[str, str] = {}
            for first in range(0, len(equities), QUERY_SYMBOLS):
                if progress is not None and first:
                    progress(f"{first} of {len(equities)} equities read, {len(excluded)} left out")
                covered: dict[str, BarType] = {}
                for equity in equities[first : first + QUERY_SYMBOLS]:
                    symbol = equity.id.symbol.value
                    bar_type = BarType.from_str(f"{equity.id}-1-MINUTE-LAST-EXTERNAL")
                    operation = (
                        f"{feed} sessions {days[0]} to {days[-1]} for {equity.id} in catalog {path}"
                    )
                    missing = catalog.get_missing_intervals_for_request(
                        span[0], span[1], NautilusDataType.Bar, str(bar_type)
                    )
                    gap = next(
                        (
                            (day, max(lo, start), min(hi, end))
                            for day, (start, end) in windows.items()
                            for lo, hi in missing
                            if lo <= end and hi >= start
                        ),
                        None,
                    )
                    if gap is None:
                        covered[symbol] = bar_type
                        continue
                    day, lo, hi = gap
                    excluded[symbol] = (
                        f"missing {feed} session {day}, minute ends {unix_nanos_to_dt(lo)} to "
                        f"{unix_nanos_to_dt(hi)}"
                    )
                if not covered:
                    continue
                operation = f"reading {len(covered)} {feed} symbols' bars in catalog {path}"
                histories: dict[str, dict[date, list[Bar]]] = {
                    str(bar_type): {day: [] for day in days} for bar_type in covered.values()
                }
                # one query for the batch: a query's fixed cost dwarfs its rows (about 0.4 s against
                # 0.01 s a symbol, measured on the first whole-market root, 2026-11-11)
                for bar in catalog.query_bars(
                    identifiers=[str(bar_type) for bar_type in covered.values()],
                    start=span[0],
                    end=span[1],
                ):
                    history = histories.get(str(bar.bar_type))
                    if history is None:
                        continue
                    for day, (start, end) in windows.items():
                        if start <= bar.ts_init <= end:
                            history[day].append(bar)
                            break
                for symbol, bar_type in covered.items():
                    operation = (
                        f"computing {feed} baseline for {bar_type.instrument_id}, session {session}"
                    )
                    history = histories[str(bar_type)]
                    baselines[symbol] = movers_rank.segment_baseline(history, session)
            share = len(excluded) / len(equities)
            if not baselines or share > max_excluded_share:
                first = "; ".join(f"{s}: {why}" for s, why in list(excluded.items())[:3])
                raise history_load.HistoryStopped(
                    f"{len(excluded)} of {len(equities)} equities in catalog {path} lack a {feed} "
                    f"session ({share:.1%}, above {max_excluded_share:.1%}), for example {first}: "
                    "load them through paper:history (ADR 0093), then retry; no baseline written"
                )
            operation = f"writing {feed} baseline for session {session} to {output}"
            if catalog_feed(path) != feed:
                raise history_load.HistoryStopped(f"catalog feed changed during {operation}")
            output.parent.mkdir(parents=True, exist_ok=True)
            movers_rank.write_baselines(
                output,
                session,
                baselines,
                header={
                    "feeds": [feed],
                    "source": {
                        "writer": "canon_trading.movers_baseline",
                        "feed": feed,
                        "catalog": str(path),
                        "sessions": [day.isoformat() for day in days],
                        "equities": len(equities),
                        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                    "excluded_share": round(share, 6),
                    "excluded": excluded,
                },
            )
        return output
    except history_load.HistoryStopped:
        raise
    except Exception as exc:
        raise history_load.HistoryStopped(
            f"{operation} failed ({type(exc).__name__}): {exc}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.movers_baseline", description=__doc__
    )
    parser.add_argument(
        "--session", type=date.fromisoformat, required=True, help="coming trade date"
    )
    parser.add_argument(
        "--sessions-file",
        type=Path,
        required=True,
        help="ordered calendar dates, one ISO date per line, including --session and five prior sessions",
    )
    parser.add_argument("--feed", choices=FEEDS, default="sip")
    parser.add_argument(
        "--catalog", type=Path, help="the feed's catalog root; default catalog/<feed>"
    )
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument(
        "--max-excluded-share",
        type=float,
        default=MAX_EXCLUDED_SHARE,
        help="the most of the catalog's equities a baseline may leave out (UNCALIBRATED)",
    )
    args = parser.parse_args(argv)
    try:
        text = args.sessions_file.read_text(encoding="utf-8")
        sessions = [
            date.fromisoformat(line.strip())
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        output = build_baseline(
            session=args.session,
            sessions=sessions,
            feed=args.feed,
            catalog_path=args.catalog or DEFAULT_CATALOG / args.feed,
            baseline_dir=args.baseline_dir,
            max_excluded_share=args.max_excluded_share,
            progress=lambda line: print(f"movers-baseline: {line}", file=sys.stderr, flush=True),
        )
    except (history_load.HistoryStopped, OSError, ValueError) as exc:
        print(f"movers-baseline: {exc}", file=sys.stderr, flush=True)
        return 1
    excluded = json.loads(output.read_text(encoding="utf-8")).get("excluded", {})
    print(
        f"movers-baseline: session {args.session}, feed {args.feed}: wrote {output}, "
        f"{len(excluded)} equities excluded",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
