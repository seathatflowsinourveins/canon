"""The early-entry study: where in a mover's day the first-rising-run entry falls, measured from minute bars.

Contract: ADR 0092, which fixes the measures, the bar model and the arms before any result is read. Every
figure this module prints is a DIAGNOSTIC (ADR 0092, "Claims"): it is labelled so, enters no ranking and
changes no row. It places no order, builds no client and reads no credential: it reads a
`ParquetDataCatalog` that `canon_trading.bars_to_catalog.load_bars` filled (ADR 0040) and does arithmetic.

What is upstream's, and what is mirrored:

* **The bars** are NautilusTrader `Bar`s as `canon_alpaca.data.parse_bar` builds them (`ts_event` the open,
  `ts_init` the end of the minute), read with `ParquetDataCatalog.query` and checked for coverage with
  `get_missing_intervals_for_request`, as `canon_trading.backtest` does. Bar types are
  `<symbol>.ALPACA-1-MINUTE-LAST-EXTERNAL` and `-1-DAY-LAST-EXTERNAL`, on one feed a root.
* **The listing arithmetic** is the movers ranking's (ADR 0083, requirement 1): the move from the latest
  completed regular-session close, and the dollar volume since 04:00 New York over its normal, the mean of
  the same clock minutes in earlier sessions, by `canon_trading.movers_stream.normal`. A bar's dollar
  volume is close x volume, as `movers_stream` takes it. The ranking functions of ADR 0087 are not on
  `main`; when they are, this module calls them instead of `movers_stream`'s, and a difference in a
  listing minute is a finding (their dollar volume is VWAP x volume, which a catalog `Bar` does not carry).
* **The entry** mirrors `canon_trading.mover.Mover` as committed at `660b37e` (not on `main`): a watched
  name's bars are kept from the minute it is listed, a run is `run_bars` closes in a row each above the
  one before (a repeated close is not a rise), a run is decided on once, and the buy is a limit at the
  ask. The exit mirrors its protection: a trailing trigger `TRAIL` of the entry under the highest bid
  since the entry, a released limit `EXIT_THROUGH` of the entry under the trigger, and a chase that
  never prices below `CHASE_CAP` under that first limit. Those three are `MoverConfig`'s defaults there,
  not arms here. When `Mover` is on `main` the rule is imported, not mirrored.
* **The bar model** is NautilusTrader's own for bar execution (docs/concepts/backtesting/bar-execution.md
  at the pin): each bar is walked open, high, low, close (`bar_adaptive_high_low_ordering` at its default),
  and an order sent on a bar's close settles "against the book left at bar N's close". So an entry
  fills at the signal bar's close plus the half-spread, and the trigger rises on a bar's high before its
  low is tested against it.

What is this module's, since no upstream ships it (ADR 0092 registers each, UNCALIBRATED): the four
measures; the three rows no code holds yet (the first-leg window, the quiet price and the largest move
above it, the pullback that admits an extended name), which take no default; the half-spread as the cost
of each side, read from quotes outside this module and given on the command line, with no default; and
the walk-forward choice over development days.

Limits, stated and not solved (ADR 0092): minute bars count only minutes with a trade; a name's rank
among all names is not computed, so "listed" here means it met the listing thresholds; re-entries are
not replayed, only the first entry of a name's day; prices are not rounded to Alpaca's tick; the size,
the request budget and the free slot are not modelled; the after-hours session is outside the window.

Run: `python -m canon_trading.early_entry_study --catalog <a SIP root> --day 2026-11-11 --symbols A,B`
prints one section a name and a summary, as Markdown. `--half-spread-bps PRE REG` adds the outcomes at
cost and at twice it; `--from HH:MM` replays a window that starts watching then; `--arms` adds every arm.
`--days-file <list> --sha256 <hex>` runs the walk-forward over the list's development dates, and
`--open-holdout` then replays the chosen arm on the holdout, once.
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd
from canon_alpaca.constants import VENUE
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from skfolio.model_selection import WalkForward

from .movers_stream import normal

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Sequence

NEW_YORK = ZoneInfo("America/New_York")
PART_START = 4 * 60
"""04:00 New York, in minutes of the day: where the day's window and its dollar volume start."""
REGULAR_OPEN = 9 * 60 + 30
WINDOW_END = 16 * 60
"""The regular close: the window's end. The after-hours session is outside the study (ADR 0092)."""
SEGMENT = 15
MAX_BASELINE = 20
"""The longest baseline an arm takes, in sessions."""
LOOKBACK_DAYS = 45
"""Calendar days read before the day for `MAX_BASELINE` sessions and the reference close."""
TRAIL = 0.02
EXIT_THROUGH = 0.005
CHASE_CAP = 0.02
"""`MoverConfig`'s `trail`, `exit_through` and `chase_cap` defaults at `660b37e`: fractions of a price."""
LATE = 0.5
"""An entry after this share of the day's move is late (ADR 0092)."""
MINUTE_NS = 60 * 1_000_000_000
DEVELOPMENT_SHARE = 0.7
"""The earliest share of the list's dates that is development; the rest is the holdout (ADR 0092)."""
BLOCKS = 5
"""A walk-forward block is one in this many of the development dates, rounded down."""
HOLDOUT_MARK = ".holdout-opened"


@dataclass(frozen=True)
class Minute:
    """One minute bar: `start` is the minute of the New York day it opened at; it is known at `start + 1`."""

    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dollars(self) -> float:
        return self.close * self.volume


@dataclass(frozen=True)
class Rows:
    """One arm: the rows the listing and the first-leg entry are replayed under.

    The first five are in code: `list_move` and `list_ratio` are ADR 0083's starting rows,
    `baseline_sessions` the ranking's baseline length, `run_bars` and `max_move` `MoverConfig`'s
    (`max_move`, the no-chase cap on the move from the reference close, has no default there, and `None`
    here is no cap). The last four are in no code yet and take no default: `first_leg_minutes` (a run
    that completes later than this after the name was first listed is extended), `quiet_minutes` and
    `max_above_quiet` (a run whose close is further than this above the quiet price is extended; the
    quiet price is the median close of the bars in the `quiet_minutes` before the run's first bar) and
    `pullback` (an extended run is refused, unless after the highest close up to the run's first bar some
    close up to that bar was at least this fraction under it). `None` turns a rule off, which is `Mover` at `660b37e`.
    """

    list_move: float
    list_ratio: float
    baseline_sessions: int
    run_bars: int
    max_move: float | None
    first_leg_minutes: int | None
    quiet_minutes: int | None
    max_above_quiet: float | None
    pullback: float | None

    def __post_init__(self) -> None:
        if self.run_bars < 1 or not 1 <= self.baseline_sessions <= MAX_BASELINE:
            raise ValueError(
                f"run_bars must be 1 or more and baseline_sessions 1 to {MAX_BASELINE}"
            )
        if (self.quiet_minutes is None) != (self.max_above_quiet is None):
            raise ValueError("quiet_minutes and max_above_quiet are given together or not at all")


REGISTERED = Rows(
    list_move=0.03,
    list_ratio=3.0,
    baseline_sessions=5,
    run_bars=3,
    max_move=None,
    first_leg_minutes=None,
    quiet_minutes=None,
    max_above_quiet=None,
    pullback=None,
)
"""The rows registered today (ADR 0083's starting rows; `Mover` at `660b37e`), all UNCALIBRATED. Ignition
is always read under these, so every arm's entry is timed from one clock (ADR 0092)."""

ARMS: dict[str, Rows] = {
    "registered": REGISTERED,
    "move-2": replace(REGISTERED, list_move=0.02),
    "move-5": replace(REGISTERED, list_move=0.05),
    "ratio-2": replace(REGISTERED, list_ratio=2.0),
    "ratio-5": replace(REGISTERED, list_ratio=5.0),
    "baseline-10": replace(REGISTERED, baseline_sessions=10),
    "baseline-20": replace(REGISTERED, baseline_sessions=20),
    "run-1": replace(REGISTERED, run_bars=1),
    "run-2": replace(REGISTERED, run_bars=2),
    "window-15": replace(REGISTERED, first_leg_minutes=15),
    "window-60": replace(REGISTERED, first_leg_minutes=60),
    "quiet-10": replace(REGISTERED, quiet_minutes=30, max_above_quiet=0.10),
    "quiet-25": replace(REGISTERED, quiet_minutes=30, max_above_quiet=0.25),
    "window-15-pullback": replace(REGISTERED, first_leg_minutes=15, pullback=0.05),
    "quiet-10-pullback": replace(REGISTERED, quiet_minutes=30, max_above_quiet=0.10, pullback=0.05),
}
"""ADR 0092's fifteen arms: one row moved at a time from the registered rows, fixed before any result."""


@dataclass(frozen=True)
class Costs:
    """The half-spread paid on each side, a fraction of the price, before and in the regular session."""

    premarket: float
    regular: float

    def half(self, minute: int) -> float:
        return self.premarket if minute < REGULAR_OPEN else self.regular

    def times(self, k: float) -> Costs:
        return Costs(self.premarket * k, self.regular * k)


GROSS = Costs(0.0, 0.0)


@dataclass(frozen=True)
class Day:
    """One name on one day: its bars in the window, earlier sessions latest first, and the reference.

    `covered_to` is the minute the catalog covers the day to (the window's end for a whole day).
    `missing` names why the name cannot be replayed, and it is then counted, never dropped.
    `uncovered` counts the earlier sessions the catalog did not cover, which are left out of the normal.
    """

    symbol: str
    day: date
    bars: tuple[Minute, ...]
    earlier: tuple[tuple[Minute, ...], ...]
    reference: float | None
    covered_to: int = WINDOW_END
    missing: str | None = None
    uncovered: int = 0
    reference_from: str = "given"


@dataclass(frozen=True)
class Listed:
    """What the listing thresholds see at one bar's close."""

    minute: int
    move: float
    dollars: float
    normal: float | None
    ratio: float | None
    listed: bool


@dataclass(frozen=True)
class Exit:
    minute: int
    price: float
    why: str
    first_trigger: float
    last_trigger: float
    raises: int


@dataclass(frozen=True)
class Replay:
    """One name's day under one arm. Minutes are when a thing was known: a bar's close."""

    day: Day
    rows: Rows
    high: float | None = None
    high_minute: int | None = None
    ignition: int | None = None
    listed: int | None = None
    never_listed: str | None = None
    refusals: tuple[tuple[int, str], ...] = ()
    signal_close: float | None = None
    entry_minute: int | None = None
    entry: float | None = None
    exit: Exit | None = None
    last_close: float | None = None
    done_at_entry: float | None = None
    ahead: float | None = None
    managed: float | None = None
    hold: float | None = None
    start: int = PART_START
    done_at_start: float | None = None

    @property
    def lead(self) -> int | None:
        """Minutes from ignition to the entry (ADR 0092, measure 1)."""
        if self.entry_minute is None or self.ignition is None:
            return None
        return self.entry_minute - self.ignition

    @property
    def late(self) -> bool:
        """Never entered, or entered after `LATE` of the move (ADR 0092, measure 3)."""
        return self.done_at_entry is None or self.done_at_entry > LATE


def minute_dollars(bars: Iterable[Minute]) -> list[float]:
    """Dollar volume by minute of the window, from `PART_START`."""
    by_minute = [0.0] * (WINDOW_END - PART_START)
    for bar in bars:
        by_minute[bar.start - PART_START] += bar.dollars
    return by_minute


def listing(day: Day, rows: Rows) -> list[Listed]:
    """The listing thresholds at each bar's close: the move from the reference and the dollar volume
    since 04:00 over the mean of the same minutes in the latest `baseline_sessions` earlier sessions. A
    name with no reference is never listed; one with no normal (no earlier session, or none that traded
    by that minute) has no ratio and is not listed, as the ranking leaves an unknown out."""
    sessions = [minute_dollars(s) for s in day.earlier[: rows.baseline_sessions]]
    out, dollars = [], 0.0
    for bar in day.bars:
        dollars += bar.dollars
        base = normal(sessions, bar.start + 1 - PART_START)
        move = bar.close / day.reference - 1.0 if day.reference else float("nan")
        ratio = None if base is None else dollars / base
        listed = bool(
            day.reference
            and move >= rows.list_move
            and ratio is not None
            and ratio >= rows.list_ratio
        )
        out.append(Listed(bar.start + 1, move, dollars, base, ratio, listed))
    return out


def first_listed(seen: Sequence[Listed]) -> int | None:
    return next((s.minute for s in seen if s.listed), None)


def why_never_listed(day: Day, seen: Sequence[Listed], rows: Rows) -> str:
    if not day.reference:
        return "no reference close"
    if not day.earlier:
        return "no earlier session to take a normal from"
    if all(s.ratio is None for s in seen):
        return "no normal: no earlier session had traded by any of its minutes"
    moved = any(s.move >= rows.list_move for s in seen)
    busy = any(s.ratio is not None and s.ratio >= rows.list_ratio for s in seen)
    if moved and busy:
        return "the move and the volume ratio never met their thresholds in the same minute"
    if moved:
        return f"its volume ratio never reached {rows.list_ratio:g}"
    return f"its move never reached {rows.list_move:.0%}"


def quiet_price(day: Day, run_start: int, minutes: int) -> float | None:
    """The median close of the bars in the `minutes` before `run_start`; with none, the last close
    before it; with none of those, the reference close."""
    before = [b for b in day.bars if b.start < run_start]
    recent = [b.close for b in before if b.start >= run_start - minutes]
    if recent:
        return statistics.median(recent)
    return before[-1].close if before else day.reference


def pulled_back(day: Day, run_start: int, pullback: float) -> bool:
    """Whether, after the highest close up to the run's first bar (the base its rises count from), some
    close up to that bar was `pullback` under it."""
    before = [b for b in day.bars if b.start <= run_start]
    peak = max(range(len(before)), key=lambda i: (before[i].close, i))
    return min(b.close for b in before[peak:]) <= before[peak].close * (1 - pullback)


def planned_stop(entry: float) -> float:
    """`Mover`'s planned stop at `660b37e`: the first trigger, less the limit's give, less the chase."""
    return (entry - entry * (TRAIL + EXIT_THROUGH)) * (1 - CHASE_CAP)


def refusal(
    day: Day, rows: Rows, costs: Costs, index: int, run: Sequence[Minute], listed_at: int
) -> str | None:
    """Why the run that `day.bars[index]` completes is not entered, or None. The order follows `Mover`'s
    own refusals where it has one."""
    bar = day.bars[index]
    known = bar.start + 1
    assert day.reference is not None
    move = bar.close / day.reference - 1.0
    if rows.max_move is not None and move > rows.max_move:
        return f"moved {move:.1%}, over the no-chase cap of {rows.max_move:.0%}"
    extended = None
    if rows.first_leg_minutes is not None and known - listed_at > rows.first_leg_minutes:
        extended = f"{known - listed_at} min after listing, past the first-leg window of {rows.first_leg_minutes}"
    if extended is None and rows.quiet_minutes is not None and rows.max_above_quiet is not None:
        quiet = quiet_price(day, run[0].start, rows.quiet_minutes)
        if quiet and bar.close / quiet - 1.0 > rows.max_above_quiet:
            extended = (
                f"{bar.close / quiet - 1.0:.1%} above the quiet price {quiet:.4f}, over "
                f"{rows.max_above_quiet:.0%}"
            )
    if extended is not None and not (
        rows.pullback is not None and pulled_back(day, run[0].start, rows.pullback)
    ):
        return f"extended ({extended})" + (
            "" if rows.pullback is None else f" with no pullback of {rows.pullback:.0%}"
        )
    half = costs.half(known)
    if bar.close * (1 - half) < planned_stop(bar.close * (1 + half)):
        return "its bid is under the planned stop"
    return None


def protect(bars: Sequence[Minute], entry: float, costs: Costs) -> Exit | None:
    """The trailing sell over the bars after the entry's bar, each walked open, high, low, close.

    The trigger starts `TRAIL` of the entry under the entry's bid and follows the highest bid up. A low
    that reaches it sells at the trigger. An open already under it sells at that open's bid if that is
    within the released limit's give and the chase's cap, and otherwise rests at the cap's floor until a
    bid reaches it. None if nothing sold by the last bar: the position is closed at the window's end.
    """
    if not bars:
        return None
    trailing = entry * TRAIL
    high = entry / (1 + costs.half(bars[0].start)) * (1 - costs.half(bars[0].start))
    first = trigger = high - trailing
    raises, floor = 0, None
    for bar in bars:
        half = costs.half(bar.start + 1)
        bids = [p * (1 - half) for p in (bar.open, bar.high, bar.low, bar.close)]
        if floor is not None:  # a released limit resting at the chase's floor
            if max(bids) >= floor:
                return Exit(bar.start + 1, floor, "floor", first, trigger, raises)
            continue
        if bids[0] <= trigger:  # opened under the trigger: the limit is released into a gap
            floor = (trigger - entry * EXIT_THROUGH) * (1 - CHASE_CAP)
            if bids[0] >= floor:
                return Exit(bar.start + 1, bids[0], "gap", first, trigger, raises)
            if bids[1] >= floor:
                return Exit(bar.start + 1, floor, "floor", first, trigger, raises)
            continue
        if bids[1] - trailing > trigger:
            trigger, raises = bids[1] - trailing, raises + 1
        if bids[2] <= trigger:
            return Exit(bar.start + 1, trigger, "trail", first, trigger, raises)
    return None


def replay(day: Day, rows: Rows, costs: Costs = GROSS, *, start: int = PART_START) -> Replay:
    """Replay one name's day under `rows`: when it was listed, the first run entered or why each was
    refused, the trailing exit, and ADR 0092's measures. `start` is the minute the strategy starts
    watching (a window that begins then); the listing's volume still counts from 04:00."""
    if day.missing is not None or not day.bars:
        return Replay(day, rows, start=start)
    high_bar = max(day.bars, key=lambda b: (b.high, -b.start))
    seen = listing(day, rows)
    listed_at = first_listed(seen)
    reference = day.reference or 0.0
    span = high_bar.high - reference if reference else 0.0
    at_start = [b for b in day.bars if b.start < start]
    base = Replay(
        day,
        rows,
        high=high_bar.high,
        high_minute=high_bar.start + 1,
        ignition=first_listed(listing(day, REGISTERED)),
        listed=listed_at,
        last_close=day.bars[-1].close,
        start=start,
        done_at_start=(
            (at_start[-1].close - reference) / span
            if span > 0 and start > PART_START and at_start
            else None
        ),
    )
    if listed_at is None:
        return replace(base, never_listed=why_never_listed(day, seen, rows))
    refusals: list[tuple[int, str]] = []
    closes: list[Minute] = []
    for index, (bar, now) in enumerate(zip(day.bars, seen, strict=True)):
        if not now.listed or bar.start < start:
            closes.clear()  # an unranked name loses its slot, and its bars with it
            continue
        closes.append(bar)
        run = closes[-(rows.run_bars + 1) :]
        if len(run) <= rows.run_bars or not all(a.close < b.close for a, b in pairwise(run)):
            continue
        why = refusal(day, rows, costs, index, run, listed_at)
        if why is not None:
            refusals.append((now.minute, why))
            continue
        entry = bar.close * (1 + costs.half(now.minute))
        after = day.bars[index + 1 :]
        out = protect(after, entry, costs)
        last_bid = day.bars[-1].close * (1 - costs.half(day.bars[-1].start + 1))
        sold = last_bid if out is None else out.price
        ahead = (max(b.high for b in after) - entry) / span if after else 0.0
        return replace(
            base,
            refusals=tuple(refusals),
            signal_close=bar.close,
            entry_minute=now.minute,
            entry=entry,
            exit=out,
            done_at_entry=(entry - reference) / span if span > 0 else None,
            ahead=ahead if span > 0 else None,
            managed=sold / entry - 1.0,
            hold=last_bid / entry - 1.0,
        )
    return replace(base, refusals=tuple(refusals))


# --- the list of mover days, the split and the walk-forward (ADR 0092, "The method") ----------------


def read_days(path: Path, sha256: str) -> list[tuple[date, str]]:
    """The list of mover days at `path`, one `YYYY-MM-DD,SYMBOL` a line (`#` starts a comment), refused
    unless its bytes hash to `sha256`, or if a line is not of that form or is there twice."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256.lower():
        raise ValueError(
            f"the list's SHA-256 is {digest}, not the one given: it is not the list registered"
        )
    listed: list[tuple[date, str]] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        day, comma, symbol = (part.strip() for part in text.partition(","))
        if not comma or not symbol:
            raise ValueError(f"line {number} of the list is not YYYY-MM-DD,SYMBOL")
        try:
            entry = (date.fromisoformat(day), symbol.upper())
        except ValueError:
            raise ValueError(f"line {number} of the list is not YYYY-MM-DD,SYMBOL") from None
        if entry in listed:
            raise ValueError(f"line {number} of the list repeats an earlier line")
        listed.append(entry)
    return listed


def split_dates(dates: Iterable[date]) -> tuple[list[date], list[date]]:
    """The list's dates in order, cut into development (the earliest `DEVELOPMENT_SHARE`, rounded down)
    and the later holdout. Refused if development is short of one date a block."""
    unique = sorted(set(dates))
    cut = int(len(unique) * DEVELOPMENT_SHARE)
    if cut < BLOCKS or cut == len(unique):
        raise ValueError(
            f"{len(unique)} dates leave {cut} for development, where the walk-forward needs {BLOCKS} "
            "and the holdout at least one"
        )
    return unique[:cut], unique[cut:]


def folds(development: Sequence[date]) -> list[tuple[list[date], list[date]]]:
    """Skfolio's `WalkForward` over the development dates: blocks of a fifth of them, rounded down, the
    training set growing from the first block, each fold's test block the one after its training set.
    That is four folds where the count divides by five and more where it does not; dates past the last
    whole block are judged by no fold."""
    block = len(development) // BLOCKS
    splitter = WalkForward(test_size=block, train_size=block, expand_train=True)
    return [
        ([development[i] for i in train], [development[i] for i in test])
        for train, test in splitter.split(pd.DataFrame(index=pd.DatetimeIndex(development)))
    ]


def early_share(runs: Sequence[Replay]) -> tuple[float, float]:
    """The choice's objective: the share of the names replayed (missing ones left out) entered not
    late, then the median share of the move still ahead at entry (zero with no entry)."""
    held = [r for r in runs if r.day.missing is None]
    if not held:
        return (0.0, 0.0)
    ahead = [r.ahead for r in held if r.ahead is not None]
    return (
        sum(not r.late for r in held) / len(held),
        statistics.median(ahead) if ahead else 0.0,
    )


def choose(days: Sequence[Day], costs: Costs = GROSS) -> str:
    """The arm with the largest `early_share` on `days`; a tie goes to the arm listed first."""
    best, best_score = "", (-1.0, -1.0)
    for name, rows in ARMS.items():
        score = early_share([replay(day, rows, costs) for day in days])
        if score > best_score:
            best, best_score = name, score
    return best


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: the arm chosen on `chosen_on` dates and its replays on the `judged` dates."""

    chosen_on: tuple[date, ...]
    judged: tuple[date, ...]
    arm: str
    runs: tuple[Replay, ...]


def walk_forward(
    days: Sequence[Day], development: Sequence[date], costs: Costs = GROSS
) -> list[Fold]:
    """For each fold of `folds`, choose the arm on the earlier dates' names and replay it on the next
    block's. An arm is never judged on a date it was chosen on."""
    out = []
    for train, test in folds(development):
        arm = choose([d for d in days if d.day in train], costs)
        runs = tuple(replay(d, ARMS[arm], costs) for d in days if d.day in test)
        out.append(Fold(tuple(train), tuple(test), arm, runs))
    return out


def open_holdout(list_path: Path) -> Path:
    """Mark the holdout of the list at `list_path` as opened, in a file beside it, and refuse a second
    opening: the marker is created exclusively, so it cannot be made twice."""
    marker = list_path.with_name(list_path.name + HOLDOUT_MARK)
    try:
        with marker.open("x", encoding="utf-8") as out:
            out.write(f"opened {datetime.now(UTC).isoformat()}\n")
    except FileExistsError:
        raise PermissionError(
            f"the holdout was already opened ({marker.name} exists beside the list): it is opened once"
        ) from None
    return marker


# --- reading the catalog ---------------------------------------------------------------------------


def _ns(day: date, minute: int) -> int:
    at = datetime.combine(day, time(), NEW_YORK) + timedelta(minutes=minute)
    return int(at.astimezone(UTC).timestamp()) * 1_000_000_000


def _covered_to(catalog: ParquetDataCatalog, bar_type: str, day: date) -> int | None:
    """The minute of `day` the catalog covers its window to, from 04:00; None if 04:00 is not covered.
    A later load's interval starts the nanosecond after the one before it ends (`bars_to_catalog`: an
    overlapping load "asks only for the part not covered and writes it to a file of its own"), so
    intervals that touch are read as one."""
    first, last = _ns(day, PART_START + 1), _ns(day, WINDOW_END)
    reach = None
    for low, high in sorted(catalog.get_intervals(NautilusDataType.Bar, bar_type)):
        if reach is None:
            reach = high if low <= first <= high else None
        elif low <= reach + 1:
            reach = max(reach, high)
        else:
            break
    if reach is None:
        return None
    return WINDOW_END if reach >= last else PART_START + int((reach - first) // MINUTE_NS) + 1


def load_day(catalog_path: Path, symbol: str, day: date) -> Day:
    """One name's day from the catalog at `catalog_path`: its minute bars from 04:00 to the regular
    close, the same window of up to `MAX_BASELINE` earlier sessions, and the reference close. An earlier
    session is a day before it with a daily bar or a minute bar of the name. The reference is the latest
    session's daily bar close; where the catalog holds no daily bar for that session (a daily bar loads
    only a day after its session, `bars_to_catalog`'s refusal), it is the close of that session's last
    regular minute bar, and `reference_from` says which. Nothing is fetched."""
    catalog = ParquetDataCatalog(str(Path(catalog_path).resolve()))
    minute_type = f"{symbol}.{VENUE}-1-MINUTE-LAST-EXTERNAL"
    daily_type = f"{symbol}.{VENUE}-1-DAY-LAST-EXTERNAL"

    def gone(why: str) -> Day:
        return Day(symbol, day, (), (), None, missing=why)

    if not catalog.get_intervals(NautilusDataType.Bar, minute_type):
        return gone("no minute bars in the catalog")
    covered_to = _covered_to(catalog, minute_type, day)
    if covered_to is None:
        return gone("the catalog does not cover the day's minute bars from 04:00")
    back = day - timedelta(days=LOOKBACK_DAYS)
    dailies = catalog.query(
        NautilusDataType.Bar,
        identifiers=[daily_type],
        start=_ns(back, 0),
        end=_ns(day + timedelta(days=2), 0),
    )
    closes = {
        datetime.fromtimestamp(b.ts_event / 1e9, NEW_YORK).date(): float(b.close.as_decimal())
        for b in dailies
    }
    by_day: dict[date, list[Minute]] = {}
    for b in catalog.query(
        NautilusDataType.Bar,
        identifiers=[minute_type],
        start=_ns(back, 0),
        end=_ns(day, covered_to),
    ):
        opened = datetime.fromtimestamp(b.ts_event / 1e9, NEW_YORK)
        start = opened.hour * 60 + opened.minute
        if PART_START <= start < WINDOW_END:
            by_day.setdefault(opened.date(), []).append(
                Minute(
                    start,
                    float(b.open.as_decimal()),
                    float(b.high.as_decimal()),
                    float(b.low.as_decimal()),
                    float(b.close.as_decimal()),
                    float(b.volume.as_decimal()),
                )
            )
    for kept in by_day.values():
        kept.sort(key=lambda m: m.start)
    sessions = sorted((d for d in {*closes, *by_day} if d < day), reverse=True)
    if not sessions:
        return gone("no bar before the day: the reference close is unknown")
    reference, source = closes.get(sessions[0]), "the daily bar"
    if reference is None:
        regular = [m for m in by_day.get(sessions[0], ()) if m.start >= REGULAR_OPEN]
        if not regular:
            return gone(f"no daily bar and no regular-session minute bar on {sessions[0]}")
        reference, source = regular[-1].close, f"the {clock(regular[-1].start)} minute bar"
    earlier, uncovered = [], 0
    for session in sessions[:MAX_BASELINE]:
        if _covered_to(catalog, minute_type, session) != WINDOW_END:
            uncovered += 1
            continue
        earlier.append(tuple(by_day.get(session, ())))
    bars = tuple(by_day.get(day, ()))
    return Day(
        symbol,
        day,
        bars,
        tuple(earlier),
        reference,
        covered_to=covered_to,
        missing=None if bars else "no minute bar in the day's window",
        uncovered=uncovered,
        reference_from=f"{source} of {sessions[0]}",
    )


# --- the report ------------------------------------------------------------------------------------


def clock(minute: int | None) -> str:
    return "-" if minute is None else f"{minute // 60:02d}:{minute % 60:02d}"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1%}"


def segments(day: Day, sessions: int) -> list[tuple[int, float, float | None]]:
    """Each 15-minute segment's dollar volume and the mean of the same segment in earlier sessions."""
    today = minute_dollars(day.bars)
    before = [minute_dollars(s) for s in day.earlier[:sessions]]
    out = []
    for at in range(0, day.covered_to - PART_START, SEGMENT):
        mean = sum(sum(s[at : at + SEGMENT]) for s in before) / len(before) if before else None
        out.append((PART_START + at, sum(today[at : at + SEGMENT]), mean))
    return out


def section(day: Day, rows: Rows, costs: Costs | None, start: int) -> list[str]:
    """One name's day as Markdown lines: the facts, the entry and exit, and the segment table."""
    gross = replay(day, rows, start=start)
    lines = [f"### {day.symbol}, {day.day} (DIAGNOSTIC)", ""]
    if day.missing is not None:
        return [*lines, f"* missing: {day.missing}", ""]
    assert gross.high is not None and gross.last_close is not None and day.reference is not None
    lines += [
        (
            f"* window: 04:00 to {clock(day.covered_to)} New York; {len(day.bars)} minute bars; "
            f"{len(day.earlier)} earlier sessions read, {day.uncovered} not covered"
        ),
        (
            f"* reference close {day.reference:.4f} ({day.reference_from}); window high {gross.high:.4f} at "
            f"{clock(gross.high_minute)} ({gross.high / day.reference - 1:+.1%}); last close "
            f"{gross.last_close:.4f} ({gross.last_close / day.reference - 1:+.1%})"
        ),
        f"* ignition (registered rows): {clock(gross.ignition)}; listed under this arm: "
        f"{clock(gross.listed)}" + (f" ({gross.never_listed})" if gross.never_listed else ""),
    ]
    if start > PART_START:
        lines.append(
            f"* watching from {clock(start)}: the price then stood {pct(gross.done_at_start)} of the way "
            f"from the reference to the window high, which came at {clock(gross.high_minute)}"
        )
    priced = [] if costs is None else [("costs x1", costs), ("costs x2", costs.times(2))]
    for name, cost in (("gross", GROSS), *priced):
        run = replay(day, rows, cost, start=start)
        if run.entry is None:
            first = "; ".join(f"{clock(m)} {why}" for m, why in run.refusals[:3])
            lines.append(
                f"* {name}: no entry"
                + (f"; {len(run.refusals)} runs refused, first: {first}" if run.refusals else "")
            )
            continue
        out = run.exit
        sold = (
            f"held to the window's end at {run.last_close:.4f}"
            if out is None
            else f"sold {out.price:.4f} at {clock(out.minute)} ({out.why}); trigger from "
            f"{out.first_trigger:.4f} to {out.last_trigger:.4f} in {out.raises} raises"
        )
        lines.append(
            f"* {name}: entry {run.entry:.4f} at {clock(run.entry_minute)}, lead "
            f"{run.lead if run.lead is not None else '-'} min, {pct(run.done_at_entry)} of the move done, "
            f"{pct(run.ahead)} still ahead; {sold}; managed {pct(run.managed)} against flat, "
            f"hold to the end {pct(run.hold)}"
            + (f"; {len(run.refusals)} earlier runs refused" if run.refusals else "")
        )
    lines += [
        "",
        f"| segment | dollars | mean of {min(rows.baseline_sessions, len(day.earlier))} sessions | ratio | cumulative ratio |",
        "|---|---|---|---|---|",
    ]
    total = total_mean = 0.0
    for at, dollars, mean in segments(day, rows.baseline_sessions):
        total, total_mean = total + dollars, total_mean + (mean or 0.0)
        lines.append(
            f"| {clock(at)} | {dollars:,.0f} | {'-' if mean is None else f'{mean:,.0f}'} | "
            f"{'-' if not mean else f'{dollars / mean:.1f}'} | "
            f"{'-' if not total_mean else f'{total / total_mean:.1f}'} |"
        )
    return [*lines, ""]


def summary(runs: Sequence[Replay]) -> list[str]:
    """ADR 0092's measures over `runs`, as Markdown lines, with the counts behind each."""
    held = [r for r in runs if r.day.missing is None]
    entered = [r for r in held if r.entry is not None]
    lines = [
        (
            f"* names: {len(runs)}; missing: {len(runs) - len(held)}; entered: {len(entered)}; "
            f"never entered: {len(held) - len(entered)}; late or never entered: "
            f"{sum(r.late for r in held)} of {len(held)}"
        ),
    ]
    leads = [r.lead for r in entered if r.lead is not None]
    ahead = [r.ahead for r in entered if r.ahead is not None]
    if leads:
        lines.append(f"* entry lead, median of {len(leads)}: {statistics.median(leads):g} min")
    if ahead:
        lines.append(
            f"* share of the move still ahead at entry, median of {len(ahead)}: "
            f"{statistics.median(ahead):.1%}"
        )
    if entered:
        managed = [r.managed for r in entered if r.managed is not None]
        hold = [r.hold for r in entered if r.hold is not None]
        lines.append(
            f"* outcome, mean of {len(managed)} entries: managed {statistics.fmean(managed):+.1%} "
            f"against flat; hold to the end {statistics.fmean(hold):+.1%}"
        )
    return lines


def arms_table(days: Sequence[Day], costs: Costs, start: int = PART_START) -> list[str]:
    """Each arm of ADR 0092 over `days`, one line an arm: the shadow arms beside the registered rows."""
    lines = [
        "| arm | entered | late or never | median lead, min | median share ahead | mean managed | mean hold |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, rows in ARMS.items():
        runs = [replay(day, rows, costs, start=start) for day in days]
        held = [r for r in runs if r.day.missing is None]
        leads = [r.lead for r in held if r.lead is not None]
        ahead = [r.ahead for r in held if r.ahead is not None]
        managed = [r.managed for r in held if r.managed is not None]
        hold = [r.hold for r in held if r.hold is not None]
        lines.append(
            f"| {name} | {len(managed)} of {len(held)} | {sum(r.late for r in held)} | "
            f"{statistics.median(leads) if leads else '-'} | "
            f"{pct(statistics.median(ahead)) if ahead else '-'} | "
            f"{pct(statistics.fmean(managed)) if managed else '-'} | "
            f"{pct(statistics.fmean(hold)) if hold else '-'} |"
        )
    return lines


def name_arms(day: Day, costs: Costs, start: int = PART_START) -> list[str]:
    """One name's day under each arm: when it was listed and entered, and what the entry came to."""
    lines = [
        "| arm | listed | entry | at | lead, min | move done | still ahead | managed | hold |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in ARMS.items():
        run = replay(day, rows, costs, start=start)
        lines.append(
            f"| {name} | {clock(run.listed)} | {clock(run.entry_minute)} | "
            f"{'-' if run.entry is None else f'{run.entry:.4f}'} | "
            f"{'-' if run.lead is None else run.lead} | {pct(run.done_at_entry)} | {pct(run.ahead)} | "
            f"{pct(run.managed)} | {pct(run.hold)} |"
        )
    return lines


def _clock_arg(text: str) -> int:
    at = time.fromisoformat(text)
    return at.hour * 60 + at.minute


def _one_day(args: argparse.Namespace, costs: Costs | None) -> int:
    print(
        f"## Early entry on {args.day}: registered rows, "
        + (
            "gross only (no spread given)"
            if costs is None
            else f"half-spread {args.half_spread_bps} bps"
        )
        + " (DIAGNOSTIC: changes no row)\n"
    )
    days = [load_day(args.catalog, s.strip().upper(), args.day) for s in args.symbols.split(",")]
    for day in days:
        print("\n".join(section(day, REGISTERED, costs, args.start)))
        if args.arms and day.missing is None:
            print("\n".join(name_arms(day, costs or GROSS, args.start)) + "\n")
    priced = [] if costs is None else [("costs x1", costs), ("costs x2", costs.times(2))]
    for label, cost in (("gross", GROSS), *priced):
        runs = [replay(day, REGISTERED, cost, start=args.start) for day in days]
        print(f"**Summary, {label}**\n\n" + "\n".join(summary(runs)) + "\n")
        if args.arms:
            print("\n".join(arms_table(days, cost, args.start)) + "\n")
    return 0


def _listed_days(args: argparse.Namespace, costs: Costs | None) -> int:
    """The walk-forward over the list's development dates; with `--open-holdout`, the one holdout run."""
    listed = read_days(args.days_file, args.sha256)
    development, holdout = split_dates(d for d, _ in listed)
    cost = costs or GROSS
    print(
        f"## Early-entry walk-forward (DIAGNOSTIC: changes no row): {len(listed)} name-days on "
        f"{len(development) + len(holdout)} dates, {len(development)} development, {len(holdout)} "
        f"holdout; {'gross' if costs is None else f'half-spread {args.half_spread_bps} bps'}\n"
    )
    days = [load_day(args.catalog, s, d) for d, s in listed if d in development]
    for number, fold in enumerate(walk_forward(days, development, cost), 1):
        print(
            f"**Fold {number}**: chosen on {len(fold.chosen_on)} dates, judged on "
            f"{len(fold.judged)} later dates: `{fold.arm}`\n\n"
            + "\n".join(summary(fold.runs))
            + "\n"
        )
    chosen = choose(days, cost)
    print(
        f"**Chosen on all development dates**: `{chosen}`\n\n" + "\n".join(arms_table(days, cost))
    )
    if args.open_holdout:
        marker = open_holdout(args.days_file)
        held_out = [load_day(args.catalog, s, d) for d, s in listed if d in holdout]
        runs = [replay(day, ARMS[chosen], cost) for day in held_out]
        print(
            f"\n**Holdout, opened once ({marker.name} written beside the list)**: `{chosen}`\n\n"
            + "\n".join(summary(runs))
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.early_entry_study",
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument("--catalog", type=Path, required=True, help="a catalog root of one feed")
    parser.add_argument("--day", type=date.fromisoformat, help="one day, with --symbols")
    parser.add_argument("--symbols", help="comma-separated")
    parser.add_argument(
        "--arms", action="store_true", help="add each arm of ADR 0092 to a day's report"
    )
    parser.add_argument("--days-file", type=Path, help="the list of mover days, with --sha256")
    parser.add_argument("--sha256", help="the list's registered SHA-256")
    parser.add_argument(
        "--open-holdout", action="store_true", help="replay the chosen arm on the holdout, once"
    )
    parser.add_argument(
        "--half-spread-bps",
        type=float,
        nargs=2,
        metavar=("PRE", "REG"),
        help="the half-spread from quotes, before and in the regular session; no default: without "
        "it only gross figures are printed",
    )
    parser.add_argument(
        "--from",
        dest="start",
        type=_clock_arg,
        default=PART_START,
        metavar="HH:MM",
        help="the minute the strategy starts watching (default 04:00), for one day",
    )
    args = parser.parse_args(argv)
    one_day = args.day is not None and bool(args.symbols)
    a_list = args.days_file is not None and bool(args.sha256)
    if one_day == a_list or (a_list and (args.day or args.symbols or args.arms)):
        parser.error("give --day with --symbols, or --days-file with --sha256, and not both")
    if args.open_holdout and not a_list:
        parser.error("--open-holdout goes with --days-file")
    if not PART_START <= args.start < WINDOW_END:
        parser.error("--from must be from 04:00 to before 16:00")
    costs = None
    if args.half_spread_bps is not None:
        if min(args.half_spread_bps) < 0:
            parser.error("--half-spread-bps must not be negative")
        costs = Costs(*(bps / 10_000 for bps in args.half_spread_bps))
    return _listed_days(args, costs) if a_list else _one_day(args, costs)


if __name__ == "__main__":
    sys.exit(main())
