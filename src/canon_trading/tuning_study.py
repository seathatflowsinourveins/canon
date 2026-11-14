"""Catalog-only Mover tuning (proposed bars replay record, ADR 0101).

Contracts read at the pins: Optuna 5.0.0's ``009_ask_and_tell.py``, ``Trial.suggest_*``
and ``RandomSampler(seed)``; skfolio's ``WalkForward(expand_train=True)``; NautilusTrader
d53849b216e2's ``BacktestEngine`` and ``docs/concepts/backtesting/bar-execution.md``.
Each suggestion is replayed and written by ``ledger.record_trial``; ``ranking.rank`` alone
computes DSR. Search trials live in a separate Optuna sampler study: the ledger's provenance
contract deliberately refuses parameter distributions. Their parameters are in the replay key.

The fifteen earlier bar arms use CLOSE x volume, explicitly named here, from historical SIP
minute/day bars (ADR 0093). Rolling VWAP/count rows wait for the history sidecar; recorder sums
and individual prints wait for recordings. There is no fetch, live client or credential access.
Training reads stop before the last 30% of the supplied session dates. The holdout has no CLI
switch: a later caller must present a marker registering EVERY arm before consuming it once.
No result here changes a paper row or asserts paper parity.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections import deque
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import optuna
from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.persistence import ParquetDataCatalog
from optuna.trial import TrialState

from .backtest import VENUE_SETTINGS
from .backtest import BacktestRun
from .backtest import account_balances
from .early_entry_study import ARMS as EARLY_ARMS
from .early_entry_study import MAX_BASELINE
from .early_entry_study import PENDING
from .early_entry_study import REPLAYED
from .early_entry_study import Day
from .early_entry_study import Minute
from .early_entry_study import Rows
from .early_entry_study import _listing as registered_listing
from .early_entry_study import folds as registered_folds
from .early_entry_study import open_holdout as registered_open_holdout
from .early_entry_study import pulled_back
from .early_entry_study import quiet_price
from .early_entry_study import registered_commit
from .early_entry_study import split_dates
from .ledger import MoverTrialConfig
from .ledger import Sample
from .ledger import bars_digest
from .ledger import canonical_json
from .ledger import config_key
from .ledger import create_ledger_study
from .ledger import data_digest
from .ledger import instrument_digest
from .ledger import record_trial
from .ma_cross import BASE_FIELDS
from .mover import OWN_FIELDS
from .mover import Mover
from .mover import MoverConfig
from .mover import Slot
from .mover import offsets
from .mover import planned_stop
from .price_step import entry_price
from .ranking import RankingStopped
from .ranking import rank
from .tuning_contract import BARS_REPLAY_FIXED_ROWS
from .tuning_contract import RankedConfiguration
from .tuning_contract import RankedPayload
from .tuning_contract import check_bars_replay_modes

NY = ZoneInfo("America/New_York")
MINUTE_NS = 60_000_000_000
BAR_SPEC = "1-MINUTE-LAST-EXTERNAL"
EXECUTION = "bars-native-trailing-close-entry-v1"
COST_NAME = "zero-fees-bar-close"
PARTS = ("pre-market", "regular", "after-hours", "overnight")
ARMS = {f"bars-{name}": rows for name, rows in EARLY_ARMS.items()}
WAITING = {
    **{
        f"rolling-bar-vwap-count-{n}m": "waiting: historical bar VWAP/trade_count sidecar"
        for n in (1, 3, 5)
    },
    **{f"recorder-sum-{n}m": "waiting: recorded dollar sums and print counts" for n in (1, 3, 5)},
    "print-level": "waiting: individual recorded prints",
}
REGISTERED_ARMS = tuple(ARMS) + tuple(WAITING)


def session_of(bar: Bar, part: str) -> date | None:
    """Session label from the bar OPEN, not its close; 20:00 starts the following overnight date."""
    opened = datetime.fromtimestamp(bar.ts_event / 10**9, NY)
    minute = opened.hour * 60 + opened.minute
    if part == "overnight":
        if minute >= 20 * 60:
            return opened.date() + timedelta(days=1)
        return opened.date() if minute < 4 * 60 else None
    low, high = {"pre-market": (240, 570), "regular": (570, 960), "after-hours": (960, 1200)}[part]
    return opened.date() if low <= minute < high else None


def bounds(day: date, part: str) -> tuple[int, int]:
    low, high = {
        "pre-market": (240, 570),
        "regular": (570, 960),
        "after-hours": (960, 1200),
        "overnight": (-240, 240),
    }[part]
    midnight = datetime.combine(day, time(), NY)
    return (
        int((midnight + timedelta(minutes=low)).timestamp()) * 10**9,
        int((midnight + timedelta(minutes=high)).timestamp()) * 10**9,
    )


@dataclass(frozen=True)
class Fold:
    train: tuple[date, ...]
    test: tuple[date, ...]


def walk_forward_folds(
    sessions: Sequence[date],
) -> tuple[tuple[Fold, ...], tuple[date, ...]]:
    """Skfolio expanding folds on the first 70%, with a later, untouched 30% (ADR 0092)."""
    days = tuple(sessions)
    if days != tuple(sorted(set(days))):
        raise ValueError("sessions must be unique and chronological")
    development, holdout = split_dates(days)
    folds = tuple(Fold(tuple(train), tuple(test)) for train, test in registered_folds(development))
    if not folds:
        raise ValueError("development dates leave no complete walk-forward fold")
    return folds, tuple(holdout)


def open_holdout(
    registration: Path,
    *,
    plan_digest: str,
    sessions: Sequence[date],
    replay: Callable[[], object] | None = None,
) -> Path:
    """Require every arm for this plan and the plan's own held dates (`split_dates(sessions)`);
    share each date's opening with early_entry_study under the default ledger's `holdouts`,
    whatever ledger the study writes (code-review-high-n2b).

    The imported helper holds one lock across replay and exclusive marker creation. Failed
    replay leaves the holdout unopened (ADR 0092); this function reads no catalog itself.
    """
    if not registration.is_file():
        raise ValueError("holdout needs a full registration marker naming every arm")
    marker = json.loads(registration.read_text())
    if not isinstance(marker, dict) or marker.get("plan") != plan_digest:
        raise ValueError("holdout registration names another plan")
    arms = marker.get("arms")
    if (
        not isinstance(arms, list)
        or not all(isinstance(arm, str) for arm in arms)
        or sorted(arms) != sorted(REGISTERED_ARMS)
    ):
        raise ValueError(
            "holdout registration must name every arm exactly once, including waiting arms"
        )
    held = marker.get("holdout_dates")
    if not isinstance(held, list) or not held or not all(isinstance(day, str) for day in held):
        raise ValueError("registration needs holdout_dates")
    days = tuple(date.fromisoformat(day) for day in held)
    if tuple(sorted(set(days))) != days:
        raise ValueError("holdout_dates must be chronological and unique")
    if days != tuple(split_dates(sessions)[1]):
        raise ValueError(
            "holdout_dates must be the plan's holdout, split_dates(sessions)'s last 30%"
        )
    if replay is None:
        raise ValueError("holdout needs a replay callback; consume only after success")
    # Each arm registered at the checkout's HEAD: the bars arms replay now; the waiting arms are listed
    # pending, so each may replay on these dates once its inputs land (ADR 0100).
    head = registered_commit("HEAD")
    arms = {
        **{arm: {"commit": head, "status": REPLAYED} for arm in ARMS},
        **{arm: {"commit": head, "status": PENDING} for arm in WAITING},
    }
    return registered_open_holdout(registration, dates=days, replay=replay, arms=arms)


@dataclass(frozen=True)
class BarMetrics:
    vwap: Decimal
    trade_count: int

    def __post_init__(self) -> None:
        if (
            not self.vwap.is_finite()
            or self.vwap <= 0
            or type(self.trade_count) is not int
            or self.trade_count < 0
        ):
            raise ValueError("bar VWAP must be positive and trade_count a nonnegative integer")


def read_bar_metrics(
    catalog: Path, bar_type: str, start: int, end: int
) -> Mapping[int, BarMetrics]:
    """Sidecar integration boundary: close-keyed historical VWAP/count, NEVER fabricated from OHLCV.

    The coordinator assigned preservation to cx-history-ext (2026-11-11 20:44). Until that
    contract is available, rolling arms remain waiting even if minute OHLCV bars exist.
    """
    raise LookupError(
        f"{catalog}: {bar_type} [{start}, {end}] waiting for VWAP/trade_count sidecar"
    )


def rolling_flow(
    bars: Sequence[Bar], metrics: Mapping[int, BarMetrics], *, window_minutes: int
) -> tuple[Decimal, int]:
    """Volume x bar VWAP and bar trade counts in a WALL-CLOCK window, separate from recorder sums."""
    if window_minutes not in (1, 3, 5) or not bars:
        raise ValueError("rolling bar flow requires bars and a 1, 3 or 5 minute window")
    end = max(bar.ts_init for bar in bars)
    selected = [bar for bar in bars if end - window_minutes * MINUTE_NS < bar.ts_init <= end]
    if any(bar.ts_init not in metrics for bar in selected):
        raise LookupError("rolling bar flow needs each bar's historical VWAP and trade_count")
    return (
        sum((bar.volume.as_decimal() * metrics[bar.ts_init].vwap for bar in selected), Decimal(0)),
        sum(metrics[bar.ts_init].trade_count for bar in selected),
    )


def sample_arm(
    trial: optuna.Trial, arm: str, *, buying_power: Decimal, slots: int
) -> tuple[Rows, MoverConfig]:
    """Real Optuna suggestions for each arm's named row; ranges and execution rows UNCALIBRATED."""
    rows = ARMS[arm]
    prefix = f"{arm}."
    changes: dict[str, Any] = {}
    if "move-" in arm:
        changes["list_move"] = trial.suggest_float(
            prefix + "list_move", rows.list_move, rows.list_move
        )
    if "ratio-" in arm:
        changes["list_ratio"] = trial.suggest_float(
            prefix + "list_ratio", rows.list_ratio, rows.list_ratio
        )
    if "baseline-" in arm:
        changes["baseline_sessions"] = trial.suggest_int(
            prefix + "baseline_sessions", rows.baseline_sessions, rows.baseline_sessions
        )
    if "run-" in arm:
        changes["run_bars"] = trial.suggest_int(prefix + "run_bars", rows.run_bars, rows.run_bars)
    if rows.first_leg_minutes is not None:
        changes["first_leg_minutes"] = trial.suggest_int(
            prefix + "first_leg_minutes", rows.first_leg_minutes, rows.first_leg_minutes
        )
    if rows.quiet_minutes is not None:
        changes["quiet_minutes"] = trial.suggest_int(
            prefix + "quiet_minutes", rows.quiet_minutes, rows.quiet_minutes
        )
        assert rows.max_above_quiet is not None
        changes["max_above_quiet"] = trial.suggest_float(
            prefix + "max_above_quiet", rows.max_above_quiet, rows.max_above_quiet
        )
    if rows.pullback is not None:
        changes["pullback"] = trial.suggest_float(prefix + "pullback", rows.pullback, rows.pullback)
    rows = replace(rows, **changes)
    settings = MoverConfig(
        max_notional=buying_power / slots,
        max_move=Decimal(str(trial.suggest_float(prefix + "max_move", 0.1, 1.0))),
        slots=slots,
        watch=max(slots, 3),
        run_bars=rows.run_bars,
        trail=Decimal(str(trial.suggest_float(prefix + "trail", 0.01, 0.04))),
        bar_spec=BAR_SPEC,
        trigger=TriggerType.LAST_PRICE,
        # the replay lists by `rows` itself and takes the live Mover's run-only rule, which this study
        # was built on; the live registered arm on the same rows is the shared configuration's (N4)
        entry_arm="bridge",
    )
    return rows, settings


@dataclass(frozen=True)
class ReplayData:
    bars: tuple[Bar, ...]
    history: tuple[Bar, ...]
    instruments: tuple[Any, ...]
    bar_types: tuple[str, ...]
    coverage: dict[str, tuple[tuple[int, int], ...]] | None = None
    quiet: tuple[str, ...] = ()
    catalog: Any | None = None

    @property
    def digest(self) -> dict[str, str]:
        return data_digest(self.bars, self.bar_types, self.instruments)

    @cached_property
    def history_digest(self) -> dict[str, str]:
        """Hashed once per replay data: the history does not change within a study (review of N2,
        code-review-high-n2c/5)."""
        return bars_digest(self.history, sorted({str(bar.bar_type) for bar in self.history}))


@dataclass(frozen=True)
class CatalogHistory:
    """One read of each minute/daily series per feed for a whole study invocation."""

    path: str
    catalog: Any
    history: tuple[Bar, ...]
    instruments: tuple[Any, ...]
    bar_types: tuple[str, ...]
    coverage: dict[str, tuple[tuple[int, int], ...]]


def read_catalog(catalog_path: Path, config: MoverTrialConfig, end: int) -> CatalogHistory:
    expected_feed = "boats" if config.part == "overnight" else "sip"
    if not catalog_path.is_dir() or not (catalog_path / "data").is_dir():
        raise LookupError(f"{config.part}: no data in {expected_feed} catalog {catalog_path}")
    marker = catalog_path / "alpaca-feed"
    if not marker.is_file() or marker.read_text().strip() != expected_feed:
        raise ValueError(
            f"{config.part}: catalog must be explicitly marked {expected_feed}: {catalog_path}"
        )
    path = str(catalog_path.resolve())
    catalog = ParquetDataCatalog(path)
    types = tuple(f"{symbol}.ALPACA-{BAR_SPEC}" for symbol in config.symbols)
    found: dict[str, Any] = {}
    for instrument in catalog.instruments(
        instrument_ids=[f"{symbol}.ALPACA" for symbol in config.symbols]
    ):
        key = str(instrument.id)
        if key in found and instrument_digest(found[key]) != instrument_digest(instrument):
            raise LookupError(f"conflicting instrument definitions for {key}")
        found[key] = instrument
    identifiers = tuple(str(BarType.from_str(kind).instrument_id) for kind in types)
    if any(identifier not in found for identifier in identifiers):
        raise LookupError(f"{config.part}: missing instrument definitions for {config.symbols}")
    coverage = {kind: tuple(catalog.get_intervals(NautilusDataType.Bar, kind)) for kind in types}
    history = []
    start = bounds(config.history_start, "overnight")[0]
    for kind in types:
        for requested in (kind, kind.replace(BAR_SPEC, "1-DAY-LAST-EXTERNAL")):
            history.extend(
                catalog.query(NautilusDataType.Bar, identifiers=[requested], start=start, end=end)
            )
    history.sort(key=lambda bar: (bar.ts_init, str(bar.bar_type)))
    if any(
        tuple(catalog.get_intervals(NautilusDataType.Bar, kind)) != covered
        for kind, covered in coverage.items()
    ):
        raise LookupError(f"{config.part}: catalog intervals changed during coverage/query; retry")
    return CatalogHistory(
        path, catalog, tuple(history), tuple(found[key] for key in identifiers), types, coverage
    )


def check_replay_data(config: MoverTrialConfig, sample: Sample, data: ReplayData) -> None:
    """Refuse bars handed to a trial that are not this config's: its symbols' bar types, every bar in
    one of its sessions and the sample, and each symbol-session either holding bars or named quiet,
    as `load_replay` gives them (code-review-high-n2b/replay-data)."""
    types = tuple(f"{symbol}.ALPACA-{BAR_SPEC}" for symbol in config.symbols)
    sessions = set(config.sessions)
    stray = [
        bar
        for bar in data.bars
        if str(bar.bar_type) not in types
        or session_of(bar, config.part) not in sessions
        or not sample.start <= bar.ts_init <= sample.end
    ]
    present = {(str(bar.bar_type), session_of(bar, config.part)) for bar in data.bars}
    expected = {f"{kind} {day}" for kind in types for day in config.sessions}
    accounted = {f"{kind} {day}" for kind, day in present} | set(data.quiet)
    if data.bar_types != types or stray or accounted != expected:
        raise ValueError(
            f"replay_data is not this trial's data: bar types {list(data.bar_types)} for {list(types)}, "
            f"{len(stray)} bars outside its {config.part} sessions or {sample.describe()}, "
            f"symbol-sessions {sorted(accounted ^ expected)} unaccounted"
        )


def load_replay(
    catalog_path: Path,
    config: MoverTrialConfig,
    sample: Sample,
    *,
    source: CatalogHistory | None = None,
) -> ReplayData:
    """Coverage is required; covered zero-print symbol-sessions remain quiet, with no invented bars."""
    if config.settings.bar_spec != BAR_SPEC:
        raise ValueError("Mover tuning requires 1-MINUTE-LAST-EXTERNAL bars")
    source = read_catalog(catalog_path, config, sample.end) if source is None else source
    types = tuple(f"{symbol}.ALPACA-{BAR_SPEC}" for symbol in config.symbols)
    if source.path != str(catalog_path.resolve()) or source.bar_types != types:
        raise ValueError("cached catalog universe/path differs from the replay")
    gaps = []
    for kind in types:
        for day in config.sessions:
            low, high = bounds(day, config.part)
            missing = source.catalog.get_missing_intervals_for_request(
                low, high, NautilusDataType.Bar, kind
            )
            if missing:
                gaps.append(f"{kind} {day}: uncovered {missing}")
    if gaps:
        raise LookupError(
            f"{config.part}: catalog does not cover every requested session: " + "; ".join(gaps)
        )
    # Each name's reference close is its latest 1-DAY bar dated before the session (`_reference`).
    # A name with none before its last session is never listable, so every trial on it is idle: that is
    # missing data, named (code-review-high-n2b/day-bars). Sessions before a name's first day bar,
    # as a new listing has, stay unlistable as they would be live.
    earliest: dict[str, date] = {}
    for bar in source.history:
        kind = str(bar.bar_type)
        if kind.endswith("-1-DAY-LAST-EXTERNAL"):
            dated = datetime.fromtimestamp(bar.ts_event / 10**9, NY).date()
            earliest[kind] = min(earliest.get(kind, dated), dated)
    unreferenced = [
        daily
        for daily in (kind.replace(BAR_SPEC, "1-DAY-LAST-EXTERNAL") for kind in types)
        if daily not in earliest or earliest[daily] >= max(config.sessions)
    ]
    if unreferenced:
        raise LookupError(
            f"{config.part}: no reference close, no 1-DAY bar before {max(config.sessions)}: "
            + ", ".join(unreferenced)
        )
    sessions = set(config.sessions)
    history = tuple(bar for bar in source.history if bar.ts_init <= sample.end)
    bars = tuple(
        bar
        for bar in history
        if str(bar.bar_type) in types
        and session_of(bar, config.part) in sessions
        and sample.start <= bar.ts_init <= sample.end
    )
    present = {(str(bar.bar_type), session_of(bar, config.part)) for bar in bars}
    quiet = tuple(
        f"{kind} {day}" for kind in types for day in config.sessions if (kind, day) not in present
    )
    return ReplayData(
        bars, history, source.instruments, types, source.coverage, quiet, source.catalog
    )


class BarsReplayMover(Mover):
    """Temporary Mover replay under ADR 0101's proposed bars record; paper parity is UNVERIFIED.

    Inherits Mover's rising runs, slot/cooldown/entry limits and order-event bookkeeping. Decisions
    happen on completed bars. Entry is buying_power / slots, in whole shares capped by free cash;
    FixedRiskSizer and the flow-share cap are never called. Protection is the matching engine's
    native LAST_PRICE trailing stop-limit, driven by its synthetic OHLC walk, rather than the
    quote-driven emulator. Released native limits rest until filled or the session close; the
    quote-driven chase is not simulated. Calendar-close alerts liquidate against the last known
    book; the last future print is never used to predict an exit. The pinned native trailing
    order starts inactive when seeded: its first matching iteration activates AFTER matching
    (execution::matching.rs:555; matching_engine/mod.rs:7007). Our post-submit settlement runs
    that iteration before the next bar (backtest/engine.rs:2097-2106), including one-share gaps.
    No quote,
    print or spread is invented. This class goes away after M-c makes Mover's own sizing compatible;
    quote/emulator parity will still require suitable recorded data.
    """

    def __init__(self, config: MoverConfig) -> None:
        check_bars_replay_modes(config)
        if config.persistence:
            # Only this class's `_trail` protects here, and with an arm on `Mover._protect` never
            # reaches it; bars deliver no trade print for the strategy-held stop to count.
            raise ValueError(
                "the bars replay cannot run a persistence arm (breach_count or high_count above "
                "one): it delivers no trade print, so the strategy-held stop would never sell"
            )
        own = {name: getattr(config, name) for name in OWN_FIELDS}
        base = {name: getattr(config, name) for name in BASE_FIELDS}
        # Only inactive numeric rows are normalized after the selected modes have been checked.
        # This own-and-base copy keeps callers' settings and all pre-port rows unchanged.
        super().__init__(MoverConfig(**(own | BARS_REPLAY_FIXED_ROWS), **base))

    def prepare(self, config: MoverTrialConfig, data: ReplayData) -> None:
        """Build indexed days and registered prefix listing once; each on-bar lookup is O(1)."""
        self.replay_config, self.data = config, data
        self.failure: Exception | None = None
        self.current: dict[InstrumentId, Bar] = {}
        self.first_listing: dict[tuple[InstrumentId, date], int] = {}
        self.pending: list[Bar] = []
        self.pending_ns: int | None = None
        self.slot_bars: dict[InstrumentId, deque[Bar]] = {}
        self.references: dict[tuple[InstrumentId, date], Decimal | None] = {}
        self.minute_history: dict[tuple[InstrumentId, date], list[Bar]] = defaultdict(list)
        self.daily: dict[InstrumentId, list[Bar]] = defaultdict(list)
        for bar in data.history:
            identifier = bar.bar_type.instrument_id
            if str(bar.bar_type).endswith("1-DAY-LAST-EXTERNAL"):
                self.daily[identifier].append(bar)
                continue
            opened = datetime.fromtimestamp(bar.ts_event / 10**9, NY)
            if config.part == "overnight":
                day = session_of(bar, config.part)
            else:
                day = (
                    opened.date()
                    if 240 <= opened.hour * 60 + opened.minute < self._window()[1]
                    else None
                )
            if day is not None:
                self.minute_history[(identifier, day)].append(bar)
        self.frames: dict[tuple[InstrumentId, date], Day] = {}
        self.listed: dict[tuple[InstrumentId, int], Any] = {}
        # only the trial's own sessions are replayed, so only they are framed and listed; earlier
        # days reach a frame as its baseline (review of N2, code-review-high-n2c/5)
        replayed = set(config.sessions)
        for (identifier, day), bars in self.minute_history.items():
            if day not in replayed:
                continue
            frame = self._frame(identifier, day)
            self.frames[(identifier, day)] = frame
            seen = registered_listing(
                frame, config.rows.list_move, config.rows.list_ratio, config.rows.baseline_sessions
            )
            for bar, listed in zip(bars, seen, strict=True):
                self.listed[(identifier, bar.ts_init)] = listed
                if listed.listed and bar.ts_init <= bounds(day, config.part)[0]:
                    self.first_listing.setdefault((identifier, day), bar.ts_init)

    def _window(self) -> tuple[int, int]:
        return {
            "pre-market": (240, 570),
            "regular": (240, 960),
            "after-hours": (240, 1200),
            "overnight": (-240, 240),
        }[self.replay_config.part]

    def _minute(self, bar: Bar, day: date) -> Minute:
        opened = datetime.fromtimestamp(bar.ts_event / 10**9, NY)
        start = (opened.date() - day).days * 1440 + opened.hour * 60 + opened.minute
        return Minute(
            start,
            float(bar.open.as_decimal()),
            float(bar.high.as_decimal()),
            float(bar.low.as_decimal()),
            float(bar.close.as_decimal()),
            float(bar.volume.as_decimal()),
        )

    def _frame(self, identifier: InstrumentId, day: date) -> Day:
        low, high = self._window()
        available = {on for item, on in self.minute_history if item == identifier and on < day}
        available.update(
            datetime.fromtimestamp(bar.ts_event / 10**9, NY).date()
            for bar in self.daily[identifier]
            if datetime.fromtimestamp(bar.ts_event / 10**9, NY).date() < day
        )
        earlier = []
        for on in sorted(available, reverse=True)[:MAX_BASELINE]:
            if self.data.catalog is not None:
                midnight = datetime.combine(on, time(), NY)
                start = int((midnight + timedelta(minutes=low + 1)).timestamp()) * 10**9
                end = int((midnight + timedelta(minutes=high)).timestamp()) * 10**9
                if self.data.catalog.get_missing_intervals_for_request(
                    start, end, NautilusDataType.Bar, f"{identifier}-{BAR_SPEC}"
                ):
                    continue
            earlier.append(
                tuple(
                    self._minute(bar, on) for bar in self.minute_history.get((identifier, on), ())
                )
            )
        reference = self._reference(identifier, day)
        return Day(
            identifier.symbol.value,
            day,
            tuple(self._minute(bar, day) for bar in self.minute_history[(identifier, day)]),
            tuple(earlier),
            None if reference is None else float(reference),
            covered_to=high,
            start=low,
            end=high,
        )

    def on_start(self) -> None:
        for bar_type in self.data.bar_types:
            self.subscribe_bars(BarType.from_str(bar_type))
        for day in self.replay_config.sessions:
            self.clock.set_time_alert_ns(
                "mover-close-" + day.isoformat(),
                bounds(day, self.replay_config.part)[1],
                self._guarded(self._session_close),
            )

    def _open_slot(self, instrument_id: InstrumentId) -> None:
        self.slots[instrument_id] = Slot(
            instrument_id,
            BarType.from_str(f"{instrument_id}-{BAR_SPEC}"),
            deque(maxlen=self.settings.run_bars + 1),
            deque(maxlen=self.settings.flow_bars),
        )
        self.slot_bars[instrument_id] = deque(maxlen=self.settings.run_bars + 1)

    def _close_slot(self, slot: Slot) -> None:
        del self.slots[slot.instrument_id]
        self.slot_bars.pop(slot.instrument_id, None)

    def _reference(self, identifier: InstrumentId, day: date) -> Decimal | None:
        key = identifier, day
        if key not in self.references:
            known = [
                bar
                for bar in self.daily[identifier]
                if datetime.fromtimestamp(bar.ts_event / 10**9, NY).date() < day
            ]
            latest = max(known, key=lambda bar: bar.ts_event) if known else None
            self.references[key] = latest.close.as_decimal() if latest is not None else None
        return self.references[key]

    def _listing(self, bar: Bar) -> Decimal | None:
        seen = self.listed.get((bar.bar_type.instrument_id, bar.ts_init))
        return Decimal(str(seen.move)) if seen is not None and seen.listed else None

    def on_bar(self, bar: Bar) -> None:
        """Queue bars; pinned engine.rs:944 flushes same-time timers AFTER all their data."""
        if self.pending_ns is None:
            self.pending_ns = bar.ts_init
            self.clock.set_time_alert_ns(
                "mover-batch-" + str(bar.ts_init),
                bar.ts_init,
                self._guarded(self._batch),
                allow_past=True,
            )
        self.pending.append(bar)

    def _guarded(self, handler: Callable[[Any], None]) -> Callable[[Any], None]:
        """The pinned engine logs and suppresses a Python time-event callback's exception
        (crates/common/src/timer.rs, `PythonTimeEventCallback::call`); keep the first one, handle
        nothing after it, and let `built_mover_replay` raise it once the engine stops."""

        def call(event: Any) -> None:
            if self.failure is not None:
                return
            try:
                handler(event)
            except Exception as e:  # noqa: BLE001 - raised by built_mover_replay after the run
                self.failure = e

        return call

    def _batch(self, event: Any) -> None:
        self._flush_pending()

    def _session_close(self, event: Any) -> None:
        self._flush_pending()
        for identifier in self.current:
            self.cancel_all_orders(identifier)
            self.close_all_positions(identifier)
        for slot in self.slots.values():
            slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None

    def _flush_pending(self) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, []
        now, self.pending_ns = pending[0].ts_init, None
        for bar in pending:
            self._remember(bar)
        ranking = []
        for identifier, latest in self.current.items():
            day = session_of(latest, self.replay_config.part)
            if day is None or now >= bounds(day, self.replay_config.part)[1]:
                continue
            if now - latest.ts_init > self.settings.rank_max_age_secs * 10**9:
                continue
            move = self._listing(latest)
            if move is not None:
                ranking.append((identifier, move))
                self.first_listing.setdefault((identifier, day), latest.ts_init)
        self.rank = sorted(ranking, key=lambda item: (-item[1], str(item[0])))
        self.rank_ns = now
        self._watch(now)
        for slot in list(self.slots.values()):
            self._manage(slot, now)
        for bar in pending:
            slot = self.slots.get(bar.bar_type.instrument_id)
            if slot is not None:
                self.slot_bars[slot.instrument_id].append(bar)
            super().on_bar(bar)
        self._enter(now, 0)
        # Catch entries already settled; queued timer submissions settle after this handler.
        for slot in list(self.slots.values()):
            self._manage(slot, now)

        # Timer commands settle after the handler (engine.rs::run_timer_handlers_at). A
        # second alert one nanosecond later sees the entry fills, before any next bar walk.
        if self.settings.protect and any(
            slot.entry_id is not None and slot.protect_id is None for slot in self.slots.values()
        ):
            self.clock.set_time_alert_ns(
                "mover-protect-" + str(now), now + 1, self._guarded(self._after_entries)
            )

    def _after_entries(self, event: Any) -> None:
        for slot in list(self.slots.values()):
            self._manage(slot, event.ts_event)

    def _busy(self) -> int:
        return sum(
            bool(slot.was_held or self._working(slot) or self._held(slot))
            for slot in self.slots.values()
        )

    def _remember(self, bar: Bar) -> None:
        identifier = bar.bar_type.instrument_id
        day = session_of(bar, self.replay_config.part)
        assert day is not None
        previous = self.current.get(identifier)
        if previous is not None and session_of(previous, self.replay_config.part) != day:
            slot = self.slots.get(identifier)
            if slot is not None:
                slot.closes.clear()
                slot.dollars.clear()
                self.slot_bars[identifier].clear()
        self.current[identifier] = bar

    def _buy(
        self,
        slot: Slot,
        move: Decimal,
        seen: int | None = None,
        run_close: Decimal | None = None,
    ) -> str | None:
        """Mover's `_buy` signature; `seen` and `run_close` feed only the paper record."""
        bar = self.current[slot.instrument_id]
        config, rows = self.replay_config, self.replay_config.rows
        day = session_of(bar, config.part)
        assert day is not None
        listed = self.first_listing[(slot.instrument_id, day)]
        extended = (
            rows.first_leg_minutes is not None
            and bar.ts_init - listed > rows.first_leg_minutes * MINUTE_NS
        )
        frame = self.frames[(slot.instrument_id, day)]
        run_start = self._minute(self.slot_bars[slot.instrument_id][0], day).start
        if rows.quiet_minutes is not None and rows.max_above_quiet is not None:
            quiet = quiet_price(frame, run_start, rows.quiet_minutes)
            if quiet and float(bar.close.as_decimal()) / quiet - 1 > rows.max_above_quiet:
                extended = True
        if extended and not (
            rows.pullback is not None and pulled_back(frame, run_start, rows.pullback)
        ):
            return "extended first leg without the registered pullback"
        instrument = self._instrument(slot.instrument_id)
        account = self.portfolio.account(VENUE)
        free = None if account is None else account.balance_free(instrument.quote_currency)
        if free is None:
            return "no free cash"
        price = entry_price(bar.close.as_decimal(), self.settings.entry_ticks)
        if (
            planned_stop(
                price, self.settings.trail, self.settings.exit_through, self.settings.chase_cap
            )
            <= 0
        ):
            return "no positive planned stop under the entry"
        quantity = min(config.buying_power / self.settings.slots, free.as_decimal()) // price
        if self.settings.max_shares is not None:
            quantity = min(quantity, Decimal(self.settings.max_shares))
        if quantity < 1:
            return "buying_power / slots leaves no whole share"
        order = self.order_factory.limit(
            slot.instrument_id,
            OrderSide.BUY,
            instrument.make_qty(quantity),
            instrument.make_price(price),
            time_in_force=TimeInForce.DAY,
        )
        slot.entry_id = order.client_order_id
        slot.entry_cancel_ns = 0
        self.submit_order(order)
        return None

    def _trail(self, slot: Slot, held: Decimal, now: int) -> None:
        entry = self._order(slot.entry_id)
        if entry is None or entry.avg_px is None:
            return
        instrument = self._instrument(slot.instrument_id)
        trail, limit = offsets(
            Decimal(str(entry.avg_px)),
            self.settings.trail,
            self.settings.exit_through,
            chase_cap=self.settings.chase_cap,
        )
        # At the pin risk::AccountRisk::trailing_order_price needs an explicit trigger or
        # cached ticks. Only the matching engine sees the bar's synthetic ticks, so seed the
        # native order from the completed bar. No quote/trade is inserted into the cache.
        at = self.current[slot.instrument_id].close.as_decimal()
        order = self.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            # At least one tick: the close can lie under a sub-dollar limit offset (`offsets`).
            instrument.make_price(max(at - limit, instrument.price_increment.as_decimal())),
            limit_offset=limit,
            trailing_offset=trail,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_price=instrument.make_price(at - trail),
            trigger_type=TriggerType.LAST_PRICE,
            time_in_force=TimeInForce.DAY,
        )
        slot.protect_id, slot.priced_ns = order.client_order_id, now
        self.submit_order(order)

    def on_stop(self) -> None:
        for instrument_id in self.current:
            self.cancel_all_orders(instrument_id)
            self.close_all_positions(instrument_id)


@contextmanager
def built_mover_replay(
    config: MoverTrialConfig,
    data: ReplayData,
    sample: Sample,
    key: str,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None,
) -> Generator[Callable[[], BacktestRun]]:
    """Build before the ledger asks; replay the exact catalog objects hashed, and dispose always."""
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=logging,
            cache=CacheConfig(bar_capacity=max(len(data.bars), CacheConfig().bar_capacity)),
        )
    )
    try:
        engine.add_venue(
            VENUE,
            starting_balances=account_balances(starting_balances),
            fee_model=MakerTakerFeeModel(
                maker_rate=config.maker_rate, taker_rate=config.taker_rate
            ),
            **VENUE_SETTINGS,
        )
        for instrument in data.instruments:
            engine.add_instrument(instrument)
        strategy = BarsReplayMover(config.settings)
        strategy.prepare(config, data)
        engine.add_strategy(strategy)
        engine.add_data(data.bars)

        def run() -> BacktestRun:
            engine.run(start=sample.start, end=sample.end, run_config_id=key)
            if strategy.failure is not None:
                failure = strategy.failure
                raise RuntimeError(
                    f"a replay handler raised: {type(failure).__name__}: {failure}"
                ) from failure
            return BacktestRun(
                engine.get_result(),
                engine.cache.orders(),
                engine.cache.positions(),
                engine.cache.position_snapshots(),
                engine.cache.account_for_venue(VENUE),
                [
                    bar
                    for kind in data.bar_types
                    for bar in engine.cache.bars(BarType.from_str(kind)) or []
                ],
                list(data.instruments),
            )

        yield run
    finally:
        engine.dispose()


@dataclass(frozen=True)
class StudyPlan:
    name: str
    catalog: Path
    ledger: Path
    symbols: tuple[str, ...]
    sessions: tuple[date, ...]
    buying_power: Decimal
    slots: int
    arms: tuple[str, ...] = tuple(ARMS)
    trials_per_arm: int = 4
    seed: int = 17
    boats_catalog: Path | None = None
    starting_balance: str = "100000 USD"

    def __post_init__(self) -> None:
        if not self.buying_power.is_finite() or self.buying_power <= 0 or not 1 <= self.slots <= 3:
            raise ValueError("buying_power must be positive and slots from 1 to 3")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be nonempty and unique")
        if not self.arms or len(set(self.arms)) != len(self.arms) or set(self.arms) - ARMS.keys():
            raise ValueError("arms must name unique runnable bars arms")
        if self.trials_per_arm <= 0:
            raise ValueError("trials_per_arm must be positive")

    @property
    def digest(self) -> str:
        values = asdict(self)
        values.update(
            catalog=str(self.catalog.resolve()),
            ledger=str(self.ledger.resolve()),
            boats_catalog=None if self.boats_catalog is None else str(self.boats_catalog.resolve()),
            sessions=[day.isoformat() for day in self.sessions],
            buying_power=str(self.buying_power),
            registered_arms=list(REGISTERED_ARMS),
        )
        return config_key(values)


@dataclass(frozen=True)
class Candidates:
    """N3's per-part promote/challenger inputs, read from the actual ledger trial identities.

    ``candidates_by_trial_key`` carries the complete typed N2/N3 ranked payload, including
    the listing/arm rows, window rows, venue/costs, sizing and bar specifications.
    Ranking is the existing gate's result, or a named refusal; this is never a promotion.
    """

    part: str
    study: str
    candidates_by_trial_key: dict[str, RankedPayload]
    ranking: dict[str, Any] | None
    ranking_stopped: str | None
    summary: str


@dataclass(frozen=True)
class FoldReport:
    part: str
    fold: Fold
    training_study: str
    validation_study: str
    training_keys: tuple[str, ...]
    training_summary: str
    validation_summary: str
    training: Candidates
    validation: Candidates


@dataclass(frozen=True)
class StudyReport:
    plan_digest: str
    folds: tuple[FoldReport, ...]
    no_data: dict[str, str]
    waiting: dict[str, str]
    quiet: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe output for N3: separate part/study/candidate/rank bundles at each fold stage."""
        return {
            "plan_digest": self.plan_digest,
            "holdout": "CLOSED",
            "no_data": dict(self.no_data),
            "waiting": dict(self.waiting),
            "quiet": dict(self.quiet),
            "folds": [
                {
                    "part": fold.part,
                    "train_sessions": [day.isoformat() for day in fold.fold.train],
                    "test_sessions": [day.isoformat() for day in fold.fold.test],
                    "training": asdict(fold.training),
                    "validation": asdict(fold.validation),
                }
                for fold in self.folds
            ],
        }

    def summary(self) -> str:
        lines = [
            f"Mover tuning {self.plan_digest}; holdout: CLOSED",
            "Costs: zero-fees-bar-close, UNCALIBRATED; sizing: buying_power / slots",
            "Ranking flow: bars-close-volume; execution: " + EXECUTION,
        ]
        lines.extend(f"{part}: {reason}" for part, reason in self.no_data.items())
        lines.extend(
            f"{part}: quiet, no selection; {', '.join(names)}" for part, names in self.quiet.items()
        )
        lines.extend(f"{name}: {reason}" for name, reason in self.waiting.items())
        for fold in self.folds:
            lines.extend(
                (
                    f"{fold.part} train {fold.fold.train[0]} to {fold.fold.train[-1]}",
                    fold.training_summary,
                    fold.validation_summary,
                )
            )
        return "\n".join(lines)


def _ranked(
    study: optuna.Study, keys: Sequence[str], legend: Mapping[str, str], part: str
) -> Candidates:
    ranked = None
    refusal = None
    try:
        ranked = rank(study, list(dict.fromkeys(keys)), legend=legend)
        summary = ranked.summary()
    except RankingStopped as stopped:
        refusal = str(stopped)
        summary = f"DSR unavailable: {stopped}"
    trials = study.trials
    trials.sort(
        key=lambda trial: (trial.value is None, -(trial.value if trial.value is not None else 0.0))
    )
    table = [
        "",
        "| Arm and settings | Config key | Trial | Sharpe | DSR of pick |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for trial in trials:
        key = trial.user_attrs["config_key"]
        name = legend.get(
            key, trial.user_attrs["config"]["strategies"][0]["config"]["replay"]["arm"]
        )
        sharpe = "unavailable" if trial.value is None else f"{trial.value:.6g}"
        dsr = f"{ranked.dsr:.4f}" if ranked is not None and key == ranked.pick else "—"
        table.append(f"| {name} | {key} | {trial.number} | {sharpe} | {dsr} |")
    candidates = {
        trial.user_attrs["config_key"]: RankedConfiguration.from_config(
            trial.user_attrs["config"]
        ).encode()
        for trial in trials
    }
    return Candidates(
        part,
        study.study_name,
        candidates,
        None if ranked is None else asdict(ranked),
        refusal,
        summary + "\n" + "\n".join(table),
    )


def _sample_for(
    root: Path, base: MoverTrialConfig, sessions: tuple[date, ...], source: CatalogHistory
) -> tuple[MoverTrialConfig, Sample, ReplayData]:
    cfg = replace(base, sessions=sessions)
    sample = Sample(0, bounds(sessions[0], base.part)[0], bounds(sessions[-1], base.part)[1])
    data = load_replay(root, cfg, sample, source=source)
    if data.bars:
        # End at the last bar, as the ledger's unbooked-end refusal advises: a calendar bound past
        # a UTC midnight (after-hours in EST ends 01:00 UTC) books the run's end on a day with no bar
        # (code-review-high-n2b/after-hours-est). The replay's own session close stays at the bound.
        last = max(bar.ts_init for bar in data.bars)
        if last < sample.end:
            sample = Sample(sample.number, sample.start, last)
    return cfg, sample, data


def run_study(plan: StudyPlan) -> StudyReport:
    """Run every training suggestion and its next-block replay; never query a holdout date.

    Every fold has separate ledger studies for training and validation, containing all arms, so
    every search configuration counts in N. A stopped gate is printed with its cause, never turned
    into an objective. No winner is promoted. New studies are created, never silently resumed.
    """
    folds, _ = walk_forward_folds(plan.sessions)
    missing: dict[str, str] = {}
    reports = []
    quiet_parts: dict[str, tuple[str, ...]] = {}
    sources: dict[tuple[Path, str], CatalogHistory] = {}
    quiet = LoggerConfig(bypass_logging=True)
    history_start = plan.sessions[0] - timedelta(days=90)
    for part in PARTS:
        root = plan.boats_catalog if part == "overnight" else plan.catalog
        if root is None:
            missing[part] = "no data: no boats catalog supplied"
            continue
        base = MoverTrialConfig(
            MoverConfig(
                max_notional=plan.buying_power / plan.slots, max_move=Decimal(1), slots=plan.slots
            ),
            plan.symbols,
            part,
            folds[0].train,
            history_start,
            plan.buying_power,
            plan.arms[0],
            ARMS[plan.arms[0]],
        )
        prepared = {}
        try:
            feed = "boats" if part == "overnight" else "sip"
            source_key = root.resolve(), feed
            if source_key not in sources:
                last_part = "overnight" if part == "overnight" else "after-hours"
                sources[source_key] = read_catalog(
                    root, base, bounds(folds[-1].test[-1], last_part)[1]
                )
            source = sources[source_key]
            # Availability only: no prices from these probes reach a training replay or sampler.
            # Refuse an incomplete development plan before creating any study for this part.
            for number, fold in enumerate(folds):
                prepared[number, "train"] = _sample_for(root, base, fold.train, source)
                prepared[number, "test"] = _sample_for(root, base, fold.test, source)
        except LookupError as no_data:
            missing[part] = str(no_data)
            continue
        # a slice with no bar is quiet; one with bars ends at its last bar (`_sample_for`), so its
        # end always has a bar (review of N2, code-review-high-n2c/4: the second clause never held)
        quiet_slices = [data for _, _, data in prepared.values() if not data.bars]
        if quiet_slices:
            quiet_parts[part] = tuple(
                sorted({name for data in quiet_slices for name in data.quiet})
            )
            continue  # no fictional bars/returns: the ledger refuses a wholly barless sample (ADR 0041)
        for number, fold in enumerate(folds):
            training_name = f"{plan.name}-{part}-{number}-train"
            validation_name = f"{plan.name}-{part}-{number}-validation"
            sampler = optuna.create_study(
                direction="maximize", sampler=optuna.samplers.RandomSampler(seed=plan.seed + number)
            )
            sampled: list[MoverTrialConfig] = []
            keys: list[str] = []
            legend: dict[str, str] = {}
            # Keep the full calendar part bounds; coverage and bar presence were preflighted.
            training_config, sample, training_data = prepared[number, "train"]
            with create_ledger_study(
                training_name, start=sample.start, end=sample.end, path=plan.ledger
            ) as ledger:
                for arm in plan.arms:
                    for _ in range(plan.trials_per_arm):
                        suggestion = sampler.ask()
                        rows, settings = sample_arm(
                            suggestion, arm, buying_power=plan.buying_power, slots=plan.slots
                        )
                        cfg = replace(
                            training_config,
                            settings=settings,
                            rows=rows,
                            arm=arm,
                            parameters=dict(suggestion.params),
                        )
                        try:
                            recorded = record_trial(
                                ledger,
                                root,
                                [cfg],
                                starting_balances=[plan.starting_balance],
                                logging=quiet,
                                replay_data=training_data,
                            )
                        except BaseException:
                            sampler.tell(suggestion, state=TrialState.FAIL)
                            raise
                        sampled.append(cfg)
                        keys.append(recorded.config_key)
                        legend[recorded.config_key] = arm + " " + canonical_json(cfg.parameters)
                        trial = ledger.get_trials(deepcopy=False)[recorded.number]
                        if trial.value is None:
                            sampler.tell(suggestion, state=TrialState.FAIL)
                        else:
                            sampler.tell(suggestion, trial.value)
                training_candidates = _ranked(ledger, keys, legend, part)
            _, validation, validation_data = prepared[number, "test"]
            validation_keys = []
            with create_ledger_study(
                validation_name, start=validation.start, end=validation.end, path=plan.ledger
            ) as ledger:
                for cfg in sampled:
                    recorded = record_trial(
                        ledger,
                        root,
                        [replace(cfg, sessions=fold.test)],
                        starting_balances=[plan.starting_balance],
                        logging=quiet,
                        replay_data=validation_data,
                    )
                    validation_keys.append(recorded.config_key)
                validation_candidates = _ranked(ledger, validation_keys, legend, part)
            reports.append(
                FoldReport(
                    part,
                    fold,
                    training_name,
                    validation_name,
                    tuple(keys),
                    training_candidates.summary,
                    validation_candidates.summary,
                    training_candidates,
                    validation_candidates,
                )
            )
    return StudyReport(plan.digest, tuple(reports), missing, dict(WAITING), quiet_parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--boats-catalog", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument(
        "--sessions",
        required=True,
        help="Comma-separated chronological YYYY-MM-DD dates, including closed holdout",
    )
    parser.add_argument("--buying-power", type=Decimal, required=True)
    parser.add_argument("--slots", type=int, required=True)
    parser.add_argument("--trials-per-arm", type=int, default=4)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--json", action="store_true", help="Emit N3's per-part candidate bundles")
    args = parser.parse_args(argv)
    plan = StudyPlan(
        args.name,
        args.catalog,
        args.ledger,
        tuple(args.symbols.split(",")),
        tuple(date.fromisoformat(day) for day in args.sessions.split(",")),
        args.buying_power,
        args.slots,
        trials_per_arm=args.trials_per_arm,
        seed=args.seed,
        boats_catalog=args.boats_catalog,
    )
    report = run_study(plan)
    print(json.dumps(report.to_dict(), allow_nan=False) if args.json else report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
