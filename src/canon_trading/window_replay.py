"""One regular Mover window on NautilusTrader's catalog-fed simulated venue (S2).

Contract: nautilus-trader v2.0.0rc8 at d53849b216e2, backtesting/apis-and-runs.md:22-95
and backtest/__init__.pyi:413-537: add_data, run(streaming=True), clear_data, end and
the three engine reports. trade-execution.md:52-75 and fill-prices-and-matching.md:165-206
define the touch/tape presets. Composition is mover_window.mover_universe_parts and
window_rank; reference_reader/inline_reads are MoversRankActor's offline contract.

Shutdown: the pinned docs/concepts/strategies.md:768-774 warn that a managed stop requested
at a backtest's end cannot complete, since its first exit check falls past the end. So the
replay, through a clock alert (common/__init__.pyi:640-653), requests the Mover's managed
stop (trading/__init__.pyi:511) one market-exit interval before window_end, and the end's
timer flush (crates/backtest/src/engine.rs:1049-1078) runs that check at the boundary
(strategy/mod.rs:1861-2004). It does so only when the Mover is flat with no open order
(Cache.positions_open and orders_open, common/__init__.pyi:450-457,568-575). A Mover still
holding or working an order then is left as it is and named in the report's replay_stop,
because paper's market exit runs after window_end, on data the replay does not have.
Its wind-down and the paper managed stop are unchanged; no data or timer runs past window_end.

The JSON spec names bars_root, quotes_roots (a list), trades_root, baseline_path,
trade_date, timezone-aware window_start/window_end/wind_down_at, trail_arm, fill_arm,
slots, entry_notional, starting_balance (Money's "amount USD" form), roster (symbol,
entry_limit), and entry_contexts in the window record's summary shape. reference_closes
maps each universe symbol to {close, session}; alternatively reference_root holds daily
bars. Optional splits are alpaca-py's ForwardSplit/ReverseSplit payloads with their
corporate_action_type. All roots are local and already exist; this module fetches nothing.

The bars root defines the universe through its equity definitions. It also supplies the
registered entry arm's prior sessions and current-day prefix: summary counts are checked,
never converted into invented bars. Missing history stays named in Day.missing. Catalog
coverage and absent minute observations are reported separately: a no-trade minute is not
proof of missing data. Quotes from overlapping roots are deduplicated as one stamp's
multiset; roots that each hold a quote the other lacks at one stamp are refused. Universe bars
and roster ticks are queried in <=60-minute batches on ts_init, including every item at a
batch boundary exactly once.

Optional rank_file replaces the ranking actor with rank_record.RecordedRankings. Its snapshot,
day, actor settings, references, baselines and commit must match. Only names in generations
active during the window get definitions or ticks; only their Mover contexts get bars. Without
rank_file the full actor remains the default. rank_signals is an in-engine bus observation in
both modes, retaining exact values, ts_event and publication times for equivalence checks.

No live client or node is constructed. Both presets ingest trades, because Mover builds
its INTERNAL entry bars from them; only tape uses the prints as matching liquidity.

Each replay owns a fresh Python process: rc8's logger is process-global and ignores later
configuration (crates/common/src/logging/logger.rs:929-949). ERROR file logging and the
acknowledged logging_sync_to_disk barrier (common/__init__.pyi:1877) make swallowed Python
handler failures fatal, including stop/dispose failures, with their logged tracebacks.
The child is ordinary offline Python, through subprocess.run's documented input/capture
contract; it does not run an agent. Its temporary log is removed after the check.

Named limits (review of 2026-11-13, kept as divergences the parity table reports, not
explained away): the Mover is composed here, not through session_window.window_node, so
load_instruments stays off (only roster names can enter), no baseline dollar volume per
minute is given to its slots, and trigger and trail settings are the arm's rows, not the
record's; the engine streams from 04:00 New York, so the ranking actor tallies the whole
pre-market where paper's tallies begin at the node's start (whether paper's subscribe
backfills is UNVERIFIED); and each roster name's minute history is queried with no start
bound.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo

from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import FileWriterConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import DataCatalogConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.risk import RiskEngineConfig

from . import early_entry_study as study
from . import movers_rank
from . import rank_record
from . import session_window
from .backtest import FEE_RATES
from .backtest import VENUE_SETTINGS
from .backtest import account_balances
from .mover import PAPER_LOSS_SHARE
from .mover_window import OUTER_BOUND_MULTIPLE
from .mover_window import mover_context_feeds
from .mover_window import mover_universe_parts
from .mover_window import part_slots
from .movers_rank_actor import REFERENCE_LOOKBACK_DAYS

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Generator
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.model import Bar

NY = ZoneInfo("America/New_York")
SECOND = 1_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EXIT_INTERVAL_MS = session_window.MARKET_EXIT_INTERVAL_MS
MINUTE = 60 * SECOND
BAR_SPEC = "1-MINUTE-LAST-EXTERNAL"
CHOICES = "pre-node choice taken from the record"
_WORKER = """
import json
import sys
from canon_trading.window_replay import _run_replay
print(json.dumps(_run_replay(json.load(sys.stdin)), allow_nan=False))
"""


@contextmanager
def _checked_engine(
    *, errors: list[dict[str, Any]] | None = None, **config: Any
) -> Generator[tuple[BacktestEngine, Callable[[], None]]]:
    """One engine per process; inspect ERROR logs after batches and after disposal."""
    with TemporaryDirectory(prefix="canon-window-replay-") as folder:
        log = Path(folder) / "engine.jsonl"
        engine = BacktestEngine(
            BacktestEngineConfig(
                logging=LoggerConfig(
                    stdout_level=LogLevel.OFF,
                    fileout_level=LogLevel.ERROR,
                    is_colored=False,
                    file_config=FileWriterConfig(
                        directory=folder, file_name="engine", file_format="json"
                    ),
                ),
                **config,
            )
        )

        def check() -> None:
            logging_sync_to_disk()
            # A missing log is an audit failure too, never an implicit clean run.
            records = [json.loads(line) for line in log.read_text().splitlines()]
            if errors is not None:
                errors[:] = records
            handler_errors = []
            for row in records:
                if row["level"] == "ERROR" and re.search(r"\bPython \w+ failed", row["message"]):
                    handler_errors.append(
                        f"{row['timestamp']} {row['component']}: {row['message']}"
                    )
            if handler_errors:
                raise RuntimeError("replay engine handler error:\n" + "\n".join(handler_errors))

        try:
            yield engine, check
        except BaseException:
            # The body's own error is the one reported; the log check runs only after success.
            engine.dispose()
            raise
        engine.dispose()
        check()


def venue_preset(fill_arm: str) -> dict[str, Any]:
    """The complete preset, printable without native enum objects."""
    if fill_arm not in ("touch", "tape"):
        raise ValueError("fill_arm must be touch or tape")
    return {
        "name": str(VENUE),
        "preset": fill_arm,
        **{key: value.name for key, value in VENUE_SETTINGS.items()},
        **{key: str(value) for key, value in FEE_RATES.items()},
        "bar_execution": False,
        "trade_execution": fill_arm == "tape",
        "liquidity_consumption": fill_arm == "tape",
    }


def add_replay_venue(engine: BacktestEngine, fill_arm: str, starting_balance: str) -> None:
    """Apply the same preset that the report names, with the existing zero-fee rates."""
    preset = venue_preset(fill_arm)
    engine.add_venue(
        VENUE,
        starting_balances=account_balances([starting_balance]),
        fee_model=MakerTakerFeeModel(
            maker_rate=FEE_RATES["maker_rate"], taker_rate=FEE_RATES["taker_rate"]
        ),
        **VENUE_SETTINGS,
        **{
            key: preset[key]
            for key in ("bar_execution", "trade_execution", "liquidity_consumption")
        },
    )


def record_digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def spec_from_record(
    record: Mapping[str, Any],
    *,
    bars_root: str | Path,
    quotes_roots: Sequence[str | Path],
    trades_root: str | Path,
    baseline_path: str | Path,
    fill_arm: str,
    reference_closes: Mapping[str, Any] | None = None,
    reference_root: str | Path | None = None,
) -> dict[str, Any]:
    """Copy the pre-node choices; derive planned bounds from the record's own clock fields.

    write_record stores started_epoch and planned_seconds, not the original Alpaca clock
    sample or exact node-start nanosecond. The derived bounds retain that limitation in
    source_record; an explicit spec can supply exact bounds when independently recorded.
    ended includes shutdown/account checks and is deliberately not the planned window end.
    """
    if record.get("session") != "regular" or record.get("part", "regular") != "regular":
        raise ValueError("a replay requires a regular-session record")
    mover = record["mover"]
    allocation = mover["allocation"]
    start = datetime.fromtimestamp(float(record["started_epoch"]), UTC)
    end = start + timedelta(seconds=float(record["planned_seconds"]))
    spec = {
        "bars_root": str(bars_root),
        "quotes_roots": [str(root) for root in quotes_roots],
        "trades_root": str(trades_root),
        "baseline_path": str(baseline_path),
        "trade_date": start.astimezone(NY).date().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "wind_down_at": (end - timedelta(seconds=session_window.FLATTEN_SECS)).isoformat(),
        "trail_arm": record["arm"],
        "fill_arm": fill_arm,
        "slots": allocation["slots"],
        "entry_notional": allocation["entry_notional"],
        "starting_balance": f"{allocation['cash']} USD",
        "roster": copy.deepcopy(mover["roster"]),
        "entry_contexts": copy.deepcopy(mover["entry_contexts"]),
        "source_record": {
            "sha256": record_digest(record),
            "commit": record.get("commit"),
            "choices": CHOICES,
            "bounds_basis": "started_epoch + planned_seconds; exact node start UNVERIFIED",
        },
    }
    if reference_closes is not None:
        spec["reference_closes"] = copy.deepcopy(dict(reference_closes))
    if reference_root is not None:
        spec["reference_root"] = str(reference_root)
    return spec


def _at(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("window times must include a timezone")
    return result.astimezone(NY)


def _stop_if_flat(strategy: Any) -> dict[str, Any]:
    """Request the Mover's managed stop only if it is running, flat and has no open order."""
    if not strategy.is_running():
        return {"requested": False, "reason": "not running"}
    positions = strategy.cache.positions_open(strategy_id=strategy.strategy_id)
    orders = strategy.cache.orders_open(strategy_id=strategy.strategy_id)
    if positions or orders:
        return {
            "requested": False,
            "reason": "not flat one market-exit interval before window_end",
            "open_positions": len(positions),
            "open_orders": len(orders),
        }
    strategy.stop()
    return {"requested": True}


def _ns(value: datetime) -> int:
    # Exact: a float timestamp is off by about 100 ns for a time with a fractional second.
    return (value - EPOCH) // timedelta(microseconds=1) * 1000


def _local(value: str | Path, *, directory: bool) -> Path:
    if "://" in str(value):
        raise ValueError("only local catalog and baseline paths are allowed")
    path = Path(value)
    if not (path.is_dir() if directory else path.is_file()):
        raise FileNotFoundError(f"missing local {'catalog' if directory else 'file'}: {path}")
    return path


def _references(
    spec: Mapping[str, Any], instruments: Sequence[Equity], day: date, roster: set[InstrumentId]
) -> tuple[dict[str, movers_rank.Reference], list[str]]:
    """Each name's usable prior-session close. A roster name without one is refused; any other
    name is left out and named, unknown to the ranking as paper's actor leaves a name with no
    reference (movers_rank_actor.alpaca_references: a close of a session before the trade date and
    no more than REFERENCE_LOOKBACK_DAYS before it, and above zero)."""
    oldest = day - timedelta(days=REFERENCE_LOOKBACK_DAYS)
    if "reference_closes" in spec:
        references = {
            symbol: movers_rank.Reference(
                close=float(row["close"]), session=date.fromisoformat(row["session"])
            )
            for symbol, row in spec["reference_closes"].items()
        }
    elif "reference_root" in spec:
        catalog = ParquetDataCatalog(str(_local(spec["reference_root"], directory=True)))
        references = {}
        # The catalog filters on ts_init, a daily bar's next midnight (canon_alpaca.data's parse_bar),
        # so the query runs to the trade date's end and keeps the bars dated before it in New York.
        latest: dict[str, Bar] = {}
        for bar in catalog.query_bars(
            identifiers=[f"{instrument.id}-1-DAY-LAST-EXTERNAL" for instrument in instruments],
            start=_ns(datetime.combine(oldest, time(0), NY)),
            end=_ns(datetime.combine(day + timedelta(days=1), time(0), NY)),
        ):
            if datetime.fromtimestamp(bar.ts_event / SECOND, NY).date() >= day:
                continue
            symbol = bar.bar_type.instrument_id.symbol.value
            if symbol not in latest or bar.ts_event > latest[symbol].ts_event:
                latest[symbol] = bar
        for symbol, bar in latest.items():
            references[symbol] = movers_rank.Reference(
                close=float(bar.close.as_decimal()),
                session=datetime.fromtimestamp(bar.ts_event / SECOND, NY).date(),
            )
    else:
        raise ValueError("reference_closes or reference_root is required; no online reader is used")
    usable, unreferenced = {}, []
    for instrument in instruments:
        symbol = instrument.id.symbol.value
        reference = references.get(symbol)
        if (
            reference is None
            or reference.session >= day
            or reference.session < oldest
            or not Decimal(str(reference.close)).is_finite()
            or reference.close <= 0
        ):
            if instrument.id in roster:
                raise ValueError(f"no usable prior-session reference for {instrument.id}")
            unreferenced.append(symbol)
        else:
            usable[symbol] = reference
    return usable, sorted(unreferenced)


def _minute(bar: Bar) -> study.Minute:
    at = datetime.fromtimestamp(bar.ts_event / SECOND, NY)
    return study.Minute(
        at.hour * 60 + at.minute,
        *(
            float(value.as_decimal())
            for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
        ),
    )


def _contexts(
    spec: Mapping[str, Any],
    catalog: ParquetDataCatalog,
    prices: Mapping[InstrumentId, Decimal],
    start: datetime,
) -> dict[InstrumentId, study.Day]:
    contexts = {}
    for instrument in prices:
        summary = spec["entry_contexts"].get(str(instrument))
        if summary is None:
            raise ValueError(f"no recorded entry context for {instrument}")
        day = date.fromisoformat(summary["day"])
        if day != start.date():
            raise ValueError(f"entry context for {instrument} names another trade date")
        covered_to = int(summary["covered_to"])
        if covered_to > start.hour * 60 + start.minute:
            raise ValueError(f"entry context for {instrument} extends past window_start")
        bars = catalog.query_bars(identifiers=[f"{instrument}-{BAR_SPEC}"], end=_ns(start))
        by_day: dict[date, list[study.Minute]] = {}
        for bar in sorted(bars, key=lambda bar: bar.ts_event):
            at = datetime.fromtimestamp(bar.ts_event / SECOND, NY)
            minute = _minute(bar)
            if study.PART_START <= minute.start < study.WINDOW_END and (
                at.date() < day or minute.start < covered_to
            ):
                by_day.setdefault(at.date(), []).append(minute)
        wanted = int(summary["earlier_sessions"])
        previous = sorted((d for d in by_day if d < day), reverse=True)[:wanted]
        prefix = tuple(by_day.get(day, ()))
        missing = summary.get("missing")
        if missing is None and (len(previous) != wanted or len(prefix) != int(summary["bars"])):
            missing = "data absent from the catalog: entry context counts differ from the record"
        contexts[instrument] = study.Day(
            instrument.symbol.value,
            day,
            prefix,
            tuple(tuple(by_day[d]) for d in previous),
            summary.get("reference"),
            covered_to=covered_to,
            missing=missing,
            reference_from=summary.get("reference_from", "record"),
        )
    return contexts


def _quotes(
    catalogs: Sequence[ParquetDataCatalog], ids: Sequence[str], start: int, end: int
) -> list[Any]:
    # Recorded SIP quotes can share a timestamp and differ (two updates in one nanosecond), so the
    # quotes at one stamp are a multiset: a root whose quotes there are all among another's is an
    # overlapping copy, the longer list is kept, and roots that each hold a quote the other lacks
    # at one stamp are refused. Every root must be of one feed: quotes of two feeds at different
    # stamps never meet here (a named limit).
    found: dict[tuple[str, int, int], list[Any]] = {}
    for catalog in catalogs:
        here: dict[tuple[str, int, int], list[Any]] = {}
        for tick in catalog.query_quote_ticks(identifiers=ids, start=start, end=end):
            here.setdefault((str(tick.instrument_id), tick.ts_init, tick.ts_event), []).append(tick)
        for stamp, ticks in here.items():
            kept = found.get(stamp)
            if kept is None:
                found[stamp] = ticks
                continue
            small, large = (ticks, kept) if len(ticks) <= len(kept) else (kept, ticks)
            left = list(large)
            for tick in small:
                if tick not in left:
                    raise ValueError(f"conflicting catalog quotes for {stamp}")
                left.remove(tick)
            found[stamp] = large
    return sorted(
        (tick for ticks in found.values() for tick in ticks), key=lambda tick: tick.ts_init
    )


def _records(frame: Any) -> list[dict[str, Any]]:
    """Keep report indices (order/position IDs) and upstream's JSON-compatible cell values."""
    if frame.empty:
        return []
    return json.loads(
        frame.reset_index().to_json(orient="records", date_format="iso", date_unit="ns")
    )


def _uncovered(
    catalogs: Sequence[ParquetDataCatalog],
    data_type: NautilusDataType,
    identifier: str,
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    """Intersect upstream's missing intervals: a range supplied by any root is covered."""
    missing = [(start, end)]
    for catalog in catalogs:
        absent = catalog.get_missing_intervals_for_request(start, end, data_type, identifier)
        missing = [
            (max(low, other_low), min(high, other_high))
            for low, high in missing
            for other_low, other_high in absent
            if max(low, other_low) <= min(high, other_high)
        ]
    return missing


REPLAY_TIMEOUT_FLOOR_SECS = 600
"""The least time a replay process is given (UNCALIBRATED; a 46-minute recorded window ran in 7)."""
REPLAY_SECS_PER_WINDOW_SEC = 1
"""A replay process's further allowance per second of window (UNCALIBRATED)."""


def run_replay(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Run with an independent native logger even if the caller already ran another engine; a
    process that outlives its bound is stopped and fails by name."""
    window = (_at(spec["window_end"]) - _at(spec["window_start"])).total_seconds()
    bound = REPLAY_TIMEOUT_FLOOR_SECS + REPLAY_SECS_PER_WINDOW_SEC * max(window, 0)
    try:
        result = subprocess.run(
            [sys.executable, "-c", _WORKER],
            input=json.dumps(dict(spec), allow_nan=False),
            text=True,
            capture_output=True,
            check=False,
            timeout=bound,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"replay process did not finish within {bound:.0f} s") from exc
    if result.returncode:
        raise RuntimeError(f"replay process failed ({result.returncode}):\n{result.stderr}")
    return json.loads(result.stdout)


def _run_replay(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Run one spec offline; dispose the engine on success or failure, before returning its record."""
    day = date.fromisoformat(spec["trade_date"])
    start, end, wind_down = (
        _at(spec[key]) for key in ("window_start", "window_end", "wind_down_at")
    )
    if not (
        start.date() == end.date() == day
        and time(9, 30) <= start.time() < wind_down.time() < end.time() <= time(16)
    ):
        raise ValueError("window bounds must be ordered within one regular session")
    if not start < wind_down < end or end - start > timedelta(minutes=60):
        raise ValueError("a window lasts at most 60 minutes and includes its wind-down")
    if end - wind_down < timedelta(milliseconds=EXIT_INTERVAL_MS):
        raise ValueError(
            "wind_down_at must be at least one market-exit interval "
            f"({EXIT_INTERVAL_MS} ms) before window_end, where the replay stops the Mover"
        )
    arm = spec["trail_arm"]
    if arm not in session_window.TRAIL_ARMS:
        raise ValueError("trail_arm must be F2, OWN1 or OWN3")
    preset = venue_preset(spec["fill_arm"])
    bars_root = _local(spec["bars_root"], directory=True)
    catalog = ParquetDataCatalog(str(bars_root))
    quote_catalogs = [
        ParquetDataCatalog(str(_local(root, directory=True))) for root in spec["quotes_roots"]
    ]
    if not quote_catalogs:
        raise ValueError("quotes_roots needs at least one catalog")
    trades = ParquetDataCatalog(str(_local(spec["trades_root"], directory=True)))
    recorded = rank_record.read_record(spec["rank_file"], spec) if "rank_file" in spec else None
    selected = rank_record.listed_names(recorded, _ns(start), _ns(end)) if recorded else None
    instruments = [
        i
        for i in (catalog.instruments(instrument_ids=selected) if selected != [] else [])
        if isinstance(i, Equity) and i.id.venue == VENUE
    ]
    if not instruments and recorded is None:
        raise ValueError("the bars catalog holds no ALPACA equity definitions")
    universe = {i.id for i in instruments}
    prices = {
        InstrumentId.from_str(f"{row['symbol']}.{VENUE}"): Decimal(str(row["entry_limit"]))
        for row in spec["roster"]
    }
    if len(prices) != len(spec["roster"]) or not prices:
        raise ValueError("the roster must be distinct equities defined in the bars catalog")
    if recorded is None and not prices.keys() <= universe:
        raise ValueError("the roster must be distinct equities defined in the bars catalog")
    if selected is not None and set(selected) != {str(i) for i in universe}:
        raise ValueError("rank file lists equities not defined in the bars snapshot")
    context_prices = {i: price for i, price in prices.items() if i in universe}
    contexts = _contexts(spec, catalog, context_prices, start)
    opened = datetime.combine(day, time(4), NY)

    notional = Decimal(str(spec["entry_notional"]))
    trail = dict(session_window.TRAIL_ARMS[arm])
    strategy, caps = mover_universe_parts(
        prices,
        extended=False,
        feed="sip",
        wind_down_at=wind_down,
        market_exit_interval_ms=EXIT_INTERVAL_MS,
        market_exit_max_attempts=session_window.MARKET_EXIT_MAX_ATTEMPTS,
        slots=part_slots("regular", spec["slots"]),
        entry_arm="registered",
        entry_contexts=contexts,
        entry_notional=notional,
        size_from_budget=True,
        loss_share=PAPER_LOSS_SHARE,
        trail_cap=trail.pop("trail_cap"),
        trail_rows=trail,
    )
    if recorded is None:
        ranking, unreferenced = rank_record.make_actor(
            spec, instruments, set(prices), strategy.ranking_records
        )
    else:
        ranking = rank_record.RecordedRankings()
        ranking.generations = [r for r in recorded["generations"] if r["published_ns"] <= _ns(end)]
        unreferenced = recorded["unreferenced"]
    subscriber = rank_record.RankSubscriber()
    evidence: dict[str, Any] = {
        "bars_read": 0,
        "quotes_read": 0,
        "trades_read": 0,
        "missing": [],
        "instruments": sorted(str(i.id) for i in instruments),
    }
    engine_errors: list[dict[str, Any]] = []
    batches = []
    with _checked_engine(
        errors=engine_errors,
        catalogs=[DataCatalogConfig(path=str(bars_root))],
        risk_engine=RiskEngineConfig(
            max_notional_per_order={str(i): str(notional * OUTER_BOUND_MULTIPLE) for i in prices}
        ),
    ) as (engine, check_log):
        add_replay_venue(engine, spec["fill_arm"], spec["starting_balance"])
        for instrument in instruments:
            engine.add_instrument(instrument)
        engine.add_strategy(strategy)
        for actor in mover_context_feeds(strategy, "sip"):
            engine.add_actor(actor)
        engine.add_actor(subscriber)
        engine.add_actor(ranking)
        for cap in caps:
            engine.add_strategy(cap)

        replay_stop: dict[str, Any] = {}
        # run()'s own integer boundary, less the interval the Mover was built with.
        strategy.clock.set_time_alert_ns(
            "window-replay-stop",
            _ns(end) - EXIT_INTERVAL_MS * (SECOND // 1000),
            lambda _event: replay_stop.update(_stop_if_flat(strategy)),
        )
        bar_instruments = (
            instruments if recorded is None else [i for i in instruments if i.id in context_prices]
        )
        bar_types = [f"{i.id}-{BAR_SPEC}" for i in bar_instruments]
        for instrument, bar_type in zip(bar_instruments, bar_types, strict=True):
            for low, high in catalog.get_missing_intervals_for_request(
                _ns(opened), _ns(end), NautilusDataType.Bar, bar_type
            ):
                evidence["missing"].append(
                    {
                        "data_type": "Bar",
                        "instrument_id": str(instrument.id),
                        "start_ns": low,
                        "end_ns": high,
                    }
                )
        ids = [str(i) for i in prices] if selected is None else selected
        for identifier in ids:
            for data_type, roots in (
                (NautilusDataType.QuoteTick, quote_catalogs),
                (NautilusDataType.TradeTick, [trades]),
            ):
                for low, high in _uncovered(roots, data_type, identifier, _ns(start), _ns(end)):
                    evidence["missing"].append(
                        {
                            "data_type": str(data_type),
                            "instrument_id": identifier,
                            "start_ns": low,
                            "end_ns": high,
                        }
                    )
        low, last = _ns(opened), _ns(end)
        first = True
        while low <= last:
            high = min(low + 60 * MINUTE - 1, last)
            bars = (
                catalog.query_bars(identifiers=bar_types, start=low, end=high) if bar_types else []
            )
            quote_ticks, trade_ticks = [], []
            if high >= _ns(start) and ids:
                quote_ticks = _quotes(quote_catalogs, ids, max(low, _ns(start)), high)
                trade_ticks = trades.query_trade_ticks(
                    identifiers=ids, start=max(low, _ns(start)), end=high
                )
            for stream in (bars, quote_ticks, trade_ticks):
                if stream:
                    engine.add_data(stream)
            evidence["bars_read"] += len(bars)
            evidence["quotes_read"] += len(quote_ticks)
            evidence["trades_read"] += len(trade_ticks)
            batches.append(
                {
                    "start_ns": low,
                    "end_ns": high,
                    "bars": len(bars),
                    "quotes": len(quote_ticks),
                    "trades": len(trade_ticks),
                }
            )
            if bars or quote_ticks or trade_ticks:
                engine.run(start=_ns(opened) if first else None, end=last, streaming=True)
                check_log()
                first = False
                engine.clear_data()
            low = high + 1
        if first:
            if recorded is None:
                raise ValueError("the catalog has no data in the replay range")
            engine.run(start=_ns(opened), end=last, streaming=True)
            check_log()
        engine.end()
        check_log()
        run = strategy.record()
        report = {
            "schema": 1,
            "commit": session_window.running_commit(),
            "venue": preset,
            "trail_arm": arm,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "wind_down_at": wind_down.isoformat(),
            "trade_date": day.isoformat(),
            "source_record": copy.deepcopy(spec.get("source_record")),
            "roster": copy.deepcopy(spec["roster"]),
            "mover": {"roster": copy.deepcopy(spec["roster"]), "run": run},
            "generations": [
                {**row, "ts_event": row["latency_ns"]["generation"]}
                for row in run["ranking_records"]
                if row.get("published")
            ],
            "rank_signals": subscriber.generations,
            "rank_file": str(spec["rank_file"]) if recorded is not None else None,
            "catalog": evidence,
            "batches": batches,
            "engine_errors": engine_errors,
            "replay_stop": replay_stop,
            "unreferenced": unreferenced,
            "orders": _records(engine.generate_orders_report()),
            "fills": _records(engine.generate_fills_report()),
            "positions": _records(engine.generate_positions_report()),
            "open_positions": len(engine.cache.positions_open()),
            "position_caps": [
                {
                    "instrument_id": str(cap.instrument_id),
                    "checks": cap.checks,
                    "peak": str(cap.peak),
                    "tripped": cap.tripped,
                }
                for cap in caps
            ],
        }
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--rank-file", type=Path)
    parser.add_argument("--bars-root", type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text())
    if args.rank_file is not None:
        spec["rank_file"] = str(args.rank_file)
    if args.bars_root is not None:
        spec["bars_root"] = str(args.bars_root)
    report = run_replay(spec)
    args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
