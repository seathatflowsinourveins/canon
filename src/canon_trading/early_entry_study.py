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
  the same clock minutes in earlier sessions, as `canon_trading.movers_stream.normal` defines it (a test
  holds this module's running sums equal to it). A bar's dollar
  volume is close x volume, as `movers_stream` takes it. The ranking functions of ADR 0087 are not on
  `main`; when they are, this module calls them instead of `movers_stream`'s, and a difference in a
  listing minute is a finding (their dollar volume is VWAP x volume, which a catalog `Bar` does not carry).
* **The entry** is shared with `canon_trading.mover.Mover` by `entry_runs` and `entry_refusal`:
  ADR 0092's table rows 73-79 fix the listing, run and extension rules. A watched
  name's bars are kept from the minute it is listed, a run is `run_bars` closes in a row each above the
  one before (a repeated close is not a rise), a run is decided on once, and the buy is a limit at the
  ask. The strategy checks the cached bid against its planned stop; the diagnostic checks its
  spread-adjusted bid. The exit mirrors the strategy's protection: a trailing trigger `TRAIL` of the entry under the highest bid
  since the entry, a released limit `EXIT_THROUGH` of the entry under the trigger, and a chase that
  never prices below `CHASE_CAP` under that first limit. Those three are `MoverConfig`'s defaults,
  not arms here. The strategy's bridge remains separately selectable; its registered arms use
  this module's listing, run and extension implementation, with explicitly supplied day history.
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
import fcntl
import hashlib
import json
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from functools import lru_cache
from itertools import accumulate
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from canon_alpaca.constants import VENUE
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from skfolio.model_selection import WalkForward

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Iterator
    from collections.abc import Mapping
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
DEVELOPMENT_TENTHS = 7
"""The earliest tenths of the list's dates that are development; the rest is the holdout (ADR 0092)."""
BLOCKS = 5
"""A walk-forward block is one in this many of the development dates, rounded down."""
HOLDOUT_MARK = ".holdout-opened"
REPOSITORY = Path(__file__).resolve().parents[2]
"""The checkout this module runs from, whose HEAD a registration commit must be or precede."""
REPLAYED = "replayed"
ADMITTED = "admitted"
"""An arm a freeze admits to the opening, replayed at the one read against it (the lead's decision of
2026-11-12 on ADR 0096's opening)."""
NOT_ADMITTED = "not admitted"
"""An arm a freeze names and leaves out of the opening, each with its reason and the record's line."""
FREEZE_FORMAT = "canon.holdout-freeze.v1"
NOTHING_READ = (
    "No process has read an outcome on a holdout date: this freeze fixes which arms are admitted, and "
    "the one read of holdout outcomes comes later, against it, after verifying its hashes."
)
"""The statement every freeze carries (the lead's decision of 2026-11-12 on ADR 0096's opening)."""
PENDING = "registered, replay pending"
"""An arm an opening lists without having replayed it: it may replay on that opening's dates once, at
its registration commit, and nothing else may (the lead's ruling of 2026-11-12, ADR 0100)."""


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
    (`max_move`, the no-chase cap on the move from the reference close, is required there with no
    default, so `None` here, no cap, is a run `Mover` itself cannot make: ADR 0092 says why). The last four are in no code yet and take no default: `first_leg_minutes` (a run
    that completes later than this after the name was first listed is extended), `quiet_minutes` and
    `max_above_quiet` (a run whose close is further than this above the quiet price is extended; the
    quiet price is the median close of the bars in the `quiet_minutes` before the run's first bar) and
    `pullback` (an extended run is refused, unless after the highest close up to the run's first bar some
    close up to that bar was at least this fraction under it). For these four `None` turns the rule off, as at `660b37e`.
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
    start: int = PART_START
    end: int = WINDOW_END


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


def minute_dollars(
    bars: Iterable[Minute], *, start: int = PART_START, end: int = WINDOW_END
) -> list[float]:
    """Dollar volume by minute of the window, from `PART_START`."""
    by_minute = [0.0] * (end - start)
    for bar in bars:
        by_minute[bar.start - start] += bar.dollars
    return by_minute


def listing(day: Day, rows: Rows) -> tuple[Listed, ...]:
    """The listing thresholds at each bar's close: the move from the reference and the dollar volume
    since 04:00 over the mean of the same minutes in the latest `baseline_sessions` earlier sessions
    (`canon_trading.movers_stream.normal`'s definition, kept as running sums). A name with no
    reference is never listed; one with no normal (no earlier session, or none that traded by that
    minute) has no ratio and is not listed, as the ranking leaves an unknown out."""
    return _listing(day, rows.list_move, rows.list_ratio, rows.baseline_sessions)


@lru_cache(maxsize=4096)
def _listing(day: Day, list_move: float, list_ratio: float, sessions: int) -> tuple[Listed, ...]:
    taken = day.earlier[:sessions]
    normals = [0.0] * (day.end - day.start + 1)
    for session in taken:
        for elapsed, total in enumerate(
            accumulate(minute_dollars(session, start=day.start, end=day.end)), 1
        ):
            normals[elapsed] += total / len(taken)
    out, dollars = [], 0.0
    for bar in day.bars:
        dollars += bar.dollars
        base = normals[bar.start + 1 - day.start] or None
        move = bar.close / day.reference - 1.0 if day.reference else float("nan")
        ratio = None if base is None else dollars / base
        listed = bool(
            day.reference and move >= list_move and ratio is not None and ratio >= list_ratio
        )
        out.append(Listed(bar.start + 1, move, dollars, base, ratio, listed))
    return tuple(out)


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


def planned_stop(
    entry: float,
    *,
    trail: float | None = None,
    exit_through: float | None = None,
    chase_cap: float | None = None,
) -> float:
    """`Mover`'s planned stop at `660b37e`: the first trigger, less the limit's give, less the chase.
    The rows default to `TRAIL`, `EXIT_THROUGH` and `CHASE_CAP`, read at the call."""
    trail = TRAIL if trail is None else trail
    exit_through = EXIT_THROUGH if exit_through is None else exit_through
    chase_cap = CHASE_CAP if chase_cap is None else chase_cap
    return (entry - entry * (trail + exit_through)) * (1 - chase_cap)


def entry_runs(
    day: Day, rows: Rows, *, start: int = PART_START
) -> Iterator[tuple[int, Sequence[Minute], int]]:
    """Listed rising runs, shared with Mover (ADR 0092:51, :73-76).

    Yield the completing bar's index, the run and the day's first listing minute. Earlier bars
    establish the listing clock and cumulative dollars, but only bars watched from `start` count
    towards a run. Losing the listing clears the run, not the day's first listing clock.
    """
    seen = listing(day, rows)
    listed_at = first_listed(seen)
    if listed_at is None:
        return
    closes: list[Minute] = []
    for index, (bar, now) in enumerate(zip(day.bars, seen, strict=True)):
        if not now.listed or bar.start < start:
            closes.clear()
            continue
        closes.append(bar)
        run = closes[-(rows.run_bars + 1) :]
        if len(run) > rows.run_bars and all(a.close < b.close for a, b in pairwise(run)):
            yield index, run, listed_at


def entry_refusal(
    day: Day, rows: Rows, index: int, run: Sequence[Minute], listed_at: int
) -> str | None:
    """No-chase and extension refusal shared with Mover (ADR 0092:77-79); no spread/exit model."""
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
    return None


def refusal(
    day: Day, rows: Rows, costs: Costs, index: int, run: Sequence[Minute], listed_at: int
) -> str | None:
    """The shared entry rule, then the diagnostic's spread against its planned stop."""
    why = entry_refusal(day, rows, index, run, listed_at)
    if why is not None:
        return why
    bar = day.bars[index]
    known = bar.start + 1
    half = costs.half(known)
    if bar.close * (1 - half) < planned_stop(bar.close * (1 + half)):
        return "its bid is under the planned stop"
    return None


def protect(
    bars: Sequence[Minute],
    entry: float,
    bid: float,
    costs: Costs,
    *,
    trail: float | None = None,
    exit_through: float | None = None,
    chase_cap: float | None = None,
    adaptive: bool = False,
    unfilled: bool = False,
) -> Exit | None:
    """The trailing sell over the bars after the entry's bar, each walked open, high, low, close.

    The trigger starts `TRAIL` of the entry under `bid`, the bid at the entry, and follows the highest
    bid up. A low that reaches it sells at the trigger. An open already under it sells at that open's
    bid if that is within the released limit's give and the chase's cap, and otherwise rests at the
    cap's floor until a bid reaches it: at a later open's bid if that is above the floor, else at the
    floor. None if nothing sold by the last bar: the position is closed at the window's end.

    The keyword rows default to `TRAIL`, `EXIT_THROUGH` and `CHASE_CAP`, read at the call. The order
    is NautilusTrader's default bar path, open, high, low, close (docs/concepts/backtesting/
    bar-execution.md at v2.0.0rc8). `adaptive` is its other documented path,
    `bar_adaptive_high_low_ordering`: the low first unless the open is strictly nearer the high
    (crates/execution/src/matching_engine/mod.rs:2400-2402), then the close. The open still raises the
    trigger first (`mod.rs:2085-2108`, trailing maintenance after each update at `:4635-4643`), and a
    sale on that low, before the bar's high is reached, is why "trail-low". `unfilled` returns a
    limit still resting at the floor after the last bar as an Exit at that bar's close bid, why
    "unfilled", instead of None.
    """
    trail = TRAIL if trail is None else trail
    exit_through = EXIT_THROUGH if exit_through is None else exit_through
    chase_cap = CHASE_CAP if chase_cap is None else chase_cap
    trailing = entry * trail
    first = trigger = bid - trailing
    raises, floor = 0, None
    bids: list[float] = []
    for bar in bars:
        half = costs.half(bar.start + 1)
        bids = [p * (1 - half) for p in (bar.open, bar.high, bar.low, bar.close)]
        if floor is not None:  # a released limit resting at the chase's floor
            if bids[0] >= floor:
                return Exit(bar.start + 1, bids[0], "floor", first, trigger, raises)
            if bids[1] >= floor:
                return Exit(bar.start + 1, floor, "floor", first, trigger, raises)
            continue
        if bids[0] <= trigger:  # opened under the trigger: the limit is released into a gap
            floor = (trigger - entry * exit_through) * (1 - chase_cap)
            if bids[0] >= floor:
                return Exit(bar.start + 1, bids[0], "gap", first, trigger, raises)
            if bids[1] >= floor:
                return Exit(bar.start + 1, floor, "floor", first, trigger, raises)
            continue
        if adaptive and not bar.high - bar.open < bar.open - bar.low:  # open, low, high, close
            if bids[0] - trailing > trigger:  # the open is the first price the trail follows
                trigger, raises = bids[0] - trailing, raises + 1
            if bids[2] <= trigger:
                return Exit(bar.start + 1, trigger, "trail-low", first, trigger, raises)
            if bids[1] - trailing > trigger:
                trigger, raises = bids[1] - trailing, raises + 1
            if bids[3] <= trigger:
                return Exit(bar.start + 1, trigger, "trail", first, trigger, raises)
            continue
        if bids[1] - trailing > trigger:
            trigger, raises = bids[1] - trailing, raises + 1
        if bids[2] <= trigger:
            return Exit(bar.start + 1, trigger, "trail", first, trigger, raises)
    if unfilled and floor is not None:
        return Exit(bars[-1].start + 1, bids[3], "unfilled", first, trigger, raises)
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
    for index, run, listed_at in entry_runs(day, rows, start=start):
        bar, now = day.bars[index], seen[index]
        why = refusal(day, rows, costs, index, run, listed_at)
        if why is not None:
            refusals.append((now.minute, why))
            continue
        entry = bar.close * (1 + costs.half(now.minute))
        after = day.bars[index + 1 :]
        out = protect(after, entry, bar.close * (1 - costs.half(now.minute)), costs)
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
    """The list's dates in order, cut into development (the earliest `DEVELOPMENT_TENTHS` tenths, rounded down)
    and the later holdout. Refused if development is short of one date a block."""
    unique = sorted(set(dates))
    cut = len(unique) * DEVELOPMENT_TENTHS // 10
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
    late, then the median share of the move still ahead at entry, a name not entered counting zero."""
    held = [r for r in runs if r.day.missing is None]
    if not held:
        return (0.0, 0.0)
    return (
        sum(not r.late for r in held) / len(held),
        statistics.median(r.ahead or 0.0 for r in held),
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


def default_holdout_root() -> Path:
    from .ledger import default_ledger_path

    return default_ledger_path().parent / "holdouts"


def _listed_holdout_dates(list_path: Path) -> tuple[date, ...]:
    try:
        days = [
            date.fromisoformat(line.split(",", 1)[0].strip())
            for line in list_path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        _, held = split_dates(days)
    except ValueError, FileNotFoundError:
        return ()  # preserve the historical standalone one-line marker API
    return tuple(held)


def holdout_marker(list_path: Path) -> Path:
    """The file beside the list that said, before per-date markers, that its holdout was opened."""
    return list_path.with_name(list_path.name + HOLDOUT_MARK)


def _held(list_path: Path, dates: Sequence[date] | None) -> tuple[date, ...]:
    return tuple(sorted(set(dates))) if dates is not None else _listed_holdout_dates(list_path)


def _date_marker(root: Path, day: date) -> Path:
    return root / f"{day.isoformat()}{HOLDOUT_MARK}"


def opened_holdout(
    list_path: Path, *, dates: Sequence[date] | None = None, root: Path | None = None
) -> list[str]:
    """What already opened this holdout: the list's own marker from before per-date markers, then
    each held date already opened under `root` (the default ledger's `holdouts`), by any list or
    registered tuning plan. Empty when the holdout is unopened."""
    found = [holdout_marker(list_path).name] if holdout_marker(list_path).exists() else []
    base = default_holdout_root() if root is None else root
    held = _held(list_path, dates)
    return found + [day.isoformat() for day in held if _date_marker(base, day).exists()]


def _manifest(root: Path, held: Sequence[date]) -> Path:
    return root / f"{held[0].isoformat()}..{held[-1].isoformat()}.manifest.json"


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY), *args], capture_output=True, text=False, check=False
    )


def registered_commit(commit: str) -> str:
    """`commit` as a full SHA, if it names a commit that is this checkout's HEAD or an ancestor of it:
    the code an arm was registered at must be code the checkout holds. Refused otherwise, by name."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(REPOSITORY), *args], capture_output=True, text=True, check=False
        )

    found = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}")
    if found.returncode != 0:
        raise ValueError(f"{commit} names no commit in {REPOSITORY}")
    sha = found.stdout.strip()
    if git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        raise ValueError(f"{commit} is not the checkout's HEAD or an ancestor of it")
    return sha


def _arms_record(arms: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    record = {}
    for name, arm in arms.items():
        commit, status = arm.get("commit"), arm.get("status")
        if not isinstance(commit, str) or not commit:
            raise ValueError(f"arm {name} names no registration commit")
        if status not in (REPLAYED, PENDING):
            raise ValueError(
                f"arm {name}'s status must be {REPLAYED!r} or {PENDING!r}, not {status!r}"
            )
        try:
            record[name] = {"commit": registered_commit(commit), "status": status}
        except ValueError as e:
            raise ValueError(f"arm {name}: {e}") from None
    return record


def _committed(path: str, commit: str) -> bytes | None:
    """The bytes of `path` (relative to the checkout) at `commit`, or None if it is not tracked there."""
    shown = _git("show", f"{commit}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def _frozen_file(path: Path, commit: str) -> dict[str, str]:
    """A freeze's record of one input: its path in the checkout and its SHA-256, which must be its form
    at `commit`, so a later read can find it in history and check it again."""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPOSITORY.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"{path} is not in the checkout: a freeze names tracked files") from None
    data = resolved.read_bytes()
    committed = _committed(relative, commit)
    if committed is None:
        raise ValueError(f"{relative} is not tracked at {commit}: commit it before the freeze")
    if committed != data:
        raise ValueError(f"{relative} differs from its form at {commit}: commit it first")
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}


def freeze_opening(
    out: Path,
    *,
    holdout_list: Path,
    amendments: Path,
    population_rule: str,
    admitted: Mapping[str, str],
    pending: Mapping[str, str],
    bars_read: Path,
    not_admitted: Mapping[str, str] | None = None,
    reasons: Mapping[str, str] | None = None,
) -> Path:
    """Freeze ADR 0096's opening (the lead's decision of 2026-11-12): HEAD's commit, the holdout list's
    and the amendment's hashes at it, the population rule by citation, every admitted arm and every arm
    pending replay with its registration commit, the statement that no outcome on a holdout date was
    read, and what read holdout bars. Each arm left out is named with its reason (`not_admitted`), and
    `reasons` gives the "admitted" and "pending" groups theirs. It reads no catalog and no list of
    names, and is written once."""
    left_out, why = dict(not_admitted or {}), dict(reasons or {})
    named = [*admitted, *pending, *left_out]
    twice = sorted({name for name in named if named.count(name) > 1})
    if twice:
        raise ValueError(f"{', '.join(twice)} is named in more than one group")
    if set(why) - {"admitted", "pending"}:
        raise ValueError(f"a reason is for 'admitted' or 'pending', not {sorted(set(why))}")
    if any(not reason.strip() for reason in left_out.values()):
        raise ValueError("an arm left out of the opening is named with its reason")
    if not population_rule.strip():
        raise ValueError("a freeze cites the population rule")
    commit = registered_commit("HEAD")
    arms: dict[str, dict[str, str]] = {}
    for status, group, members in ((ADMITTED, "admitted", admitted), (PENDING, "pending", pending)):
        for name, registered in members.items():
            try:
                arms[name] = {"commit": registered_commit(registered), "status": status}
            except ValueError as e:
                raise ValueError(f"arm {name}: {e}") from None
            if why.get(group, "").strip():
                arms[name]["reason"] = why[group].strip()
    for name, reason in left_out.items():
        arms[name] = {"status": NOT_ADMITTED, "reason": reason.strip()}
    read_bytes = bars_read.read_bytes()
    record = {
        "format": FREEZE_FORMAT,
        "commit": commit,
        "frozen": datetime.now(UTC).isoformat(),
        "holdout_list": _frozen_file(holdout_list, commit),
        "amendments": _frozen_file(amendments, commit),
        "population_rule": population_rule.strip(),
        "arms": arms,
        "nothing_read": NOTHING_READ,
        "bars_read": [
            line.rstrip() for line in read_bytes.decode("utf-8").splitlines() if line.strip()
        ],
        "bars_read_sha256": hashlib.sha256(read_bytes).hexdigest(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError(f"{out} exists: a freeze is written once")
    # whole or not at all: written beside it, then linked into place, which refuses an existing file
    # (review of the freeze, finding 7)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.link(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def verify_freeze(path: Path) -> dict[str, Any]:
    """A freeze, once it holds: the freeze file itself is tracked, unchanged in the checkout and
    committed once, so its arms, rule and bars-read lines are what was frozen; its commit is a full SHA
    that is the checkout's HEAD or an ancestor of it, as is each admitted or pending arm's, and every arm
    has a known status; the holdout list hashes as frozen both at that commit and in the checkout now,
    since the read takes its dates from it; the amendment hashes as frozen at that commit, since its
    record may be edited later. The one read of holdout outcomes is to call this first; any mismatch is
    refused by name (the lead's decision of 2026-11-12; review of the freeze, findings 1, 3, 4, 6, and
    its second read, findings 1-3)."""
    name = path.name
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPOSITORY.resolve()).as_posix()
    except ValueError:
        raise PermissionError(f"{name} is not in the checkout, so it is not tracked") from None
    data = resolved.read_bytes()
    at_head = _committed(relative, "HEAD")
    if at_head is None:
        raise PermissionError(f"{relative} is not tracked: a freeze is read from main")
    if at_head != data:
        raise PermissionError(f"{relative} differs from its committed form")
    touched = _git("log", "--format=%H", "--", f":(literal){relative}").stdout.split()
    if len(touched) != 1:
        raise PermissionError(f"{relative} was committed more than once: a freeze is written once")
    try:
        record = json.loads(data)
        if record["format"] != FREEZE_FORMAT:
            raise PermissionError(f"{name} is not a {FREEZE_FORMAT} freeze")
        commit = record["commit"]
        files = [
            (key, record[key]["path"], record[key]["sha256"])
            for key in ("holdout_list", "amendments")
        ]
        arms = record["arms"].items()
        if not all(isinstance(text, str) for _, *pair in files for text in pair):
            raise TypeError("a frozen file's path and sha256 are text")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise PermissionError(f"{name} is malformed: {type(e).__name__}: {e}") from None
    try:
        if registered_commit(commit) != commit:
            raise ValueError(f"its commit {commit} is not a full SHA")
        for arm, entry in arms:
            status = entry.get("status")
            if status == NOT_ADMITTED:
                continue
            if status not in (ADMITTED, PENDING):
                raise ValueError(f"arm {arm} has an unknown status {status!r}")
            if registered_commit(entry["commit"]) != entry["commit"]:
                raise ValueError(f"arm {arm}'s commit {entry['commit']} is not a full SHA")
    except (KeyError, TypeError, AttributeError) as e:
        raise PermissionError(f"{name} is malformed: {type(e).__name__}: {e}") from None
    except ValueError as e:
        raise PermissionError(f"{name}: {e}") from None
    for key, relative_path, digest in files:
        forms = [(f"at {commit}", _committed(relative_path, commit))]
        if key == "holdout_list":
            current = REPOSITORY / relative_path
            forms.append(("in the checkout", current.read_bytes() if current.is_file() else None))
        for form, found in forms:
            if found is None or hashlib.sha256(found).hexdigest() != digest:
                raise PermissionError(
                    f"{name}: {relative_path} {form} does not hash to the frozen {digest}"
                )
    return record


def _write_json(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def open_holdout(
    list_path: Path,
    *,
    dates: Sequence[date] | None = None,
    root: Path | None = None,
    replay: Callable[[], object] | None = None,
    arms: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    """Serialize replay for held dates and mark each date opened only after successful replay.

    Each held date is opened once, under one root shared by every list and registered tuning plan, so
    a subset, superset or overlap of dates already opened is refused (ADR 0092). Returns that root.
    A standalone caller with no parseable dates keeps the one marker beside its list, and may omit
    replay. Study callers put all held-date evaluation in the callback so an exception leaves the
    opening available.
    """
    held = _held(list_path, dates)
    if arms is not None and not held:
        raise ValueError("an opening's arm manifest needs its held dates")
    record = None if arms is None else _arms_record(arms)
    base = default_holdout_root() if root is None else root
    target = base if held else holdout_marker(list_path)
    (base if held else target.parent).mkdir(parents=True, exist_ok=True)
    lock_path = base / ".lock" if held else target.with_name(target.name + ".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            opened = opened_holdout(list_path, dates=held, root=base)
            if opened:
                raise PermissionError(
                    f"the holdout was already opened ({', '.join(opened)}): it is opened once"
                )
            if replay is not None:
                replay()
            opened_at = datetime.now(UTC).isoformat()
            named = "" if record is None else f", manifest {_manifest(base, held).name}"
            for marker in [_date_marker(base, day) for day in held] or [target]:
                with marker.open("x", encoding="utf-8") as out:
                    out.write(f"opened {opened_at} for {list_path.name}{named}\n")
            # After the markers, which are what opens the dates, and replaced whole: a failed marker
            # leaves no manifest to block the next attempt, and a failed manifest leaves the dates
            # opened with no arm pending, so every late arm goes to the later holdout.
            if record is not None:
                _write_json(
                    _manifest(base, held),
                    {
                        "dates": [day.isoformat() for day in held],
                        "list": list_path.name,
                        "opened": opened_at,
                        "arms": record,
                    },
                )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return target


def replay_pending_arm(
    arm: str,
    *,
    commit: str,
    dates: Sequence[date],
    replay: Callable[[], object],
    root: Path | None = None,
) -> Path:
    """Replay one arm on an opened holdout's dates, once, where that opening's manifest lists it as
    `PENDING` at `commit`; then mark it replayed. Anything else (another commit, an arm the opening
    replayed or never named, dates that are not exactly one opening's) is refused and goes to the later
    holdout (the lead's ruling of 2026-11-12, ADR 0100). A replay that raises leaves the arm pending."""
    held = tuple(sorted(set(dates)))
    base = default_holdout_root() if root is None else root
    manifest = _manifest(base, held) if held else base / "none"
    later = "it goes to the later holdout"
    if not base.is_dir():
        raise PermissionError(f"no holdout was ever opened under {base}: {later}")
    try:
        commit = registered_commit(commit)
    except ValueError as e:
        raise PermissionError(f"{e}: {later}") from None
    with (base / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not manifest.is_file():
                raise PermissionError(f"no opening's manifest covers exactly these dates: {later}")
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record.get("dates") != [day.isoformat() for day in held]:
                raise PermissionError(f"{manifest.name} names other dates: {later}")
            entry = record.get("arms", {}).get(arm)
            if entry is None or entry.get("status") != PENDING:
                raise PermissionError(f"{arm} is not pending in {manifest.name}: {later}")
            if entry.get("commit") != commit:
                raise PermissionError(
                    f"{arm} was registered at {entry.get('commit')}, not {commit}: {later}"
                )
            replay()
            entry["status"] = f"{REPLAYED} later, {datetime.now(UTC).isoformat()}"
            _write_json(manifest, record)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return manifest


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
        if _covered_to(catalog, minute_type, sessions[0]) != WINDOW_END:
            return gone(
                f"no daily bar for {sessions[0]}, and its minute bars are not covered to 16:00"
            )
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


def segments(day: Day, sessions: int) -> list[tuple[int, int, float, float | None]]:
    """Each 15-minute segment's start and end, its dollar volume and the mean of the same minutes in
    earlier sessions. The last segment ends where the catalog's cover of the day does."""
    today = minute_dollars(day.bars)
    before = [minute_dollars(s) for s in day.earlier[:sessions]]
    covered = day.covered_to - PART_START
    out = []
    for at in range(0, covered, SEGMENT):
        end = min(at + SEGMENT, covered)
        mean = sum(sum(s[at:end]) for s in before) / len(before) if before else None
        out.append((PART_START + at, PART_START + end, sum(today[at:end]), mean))
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
    for at, end, dollars, mean in segments(day, rows.baseline_sessions):
        total, total_mean = total + dollars, total_mean + (mean or 0.0)
        lines.append(
            f"| {clock(at)}{'' if end - at == SEGMENT else f' to {clock(end)}'} | {dollars:,.0f} | {'-' if mean is None else f'{mean:,.0f}'} | "
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


def _priced(costs: Costs | None) -> list[tuple[str, Costs]]:
    """Gross, and with a spread given the costs at one and at two times it (ADR 0092, measure 4)."""
    return [("gross", GROSS)] + (
        [] if costs is None else [("costs x1", costs), ("costs x2", costs.times(2))]
    )


def _at_each_cost(days: Sequence[Day], rows: Rows, costs: Costs | None) -> str:
    return "\n\n".join(
        f"*{label}*\n\n" + "\n".join(summary([replay(day, rows, cost) for day in days]))
        for label, cost in _priced(costs)
    )


def _listed_days(args: argparse.Namespace, costs: Costs | None) -> int:
    """The walk-forward over the list's development dates; with `--open-holdout`, the one holdout run.

    An arm is chosen at the spread given (gross without one) and reported at every cost. The holdout is
    marked opened only once its days are read and replayed, so a run that fails, or finds none of its
    names in the catalog, leaves it unopened; a holdout already opened is refused before anything runs."""
    listed = read_days(args.days_file, args.sha256)
    development, holdout = split_dates(d for d, _ in listed)
    if args.open_holdout and opened_holdout(args.days_file, dates=holdout):
        open_holdout(args.days_file, dates=holdout)  # raises: it is opened once
    pending = {
        name: {"commit": registered_commit(commit), "status": PENDING}
        for name, commit in args.pending_arm or ()
    }
    cost = costs or GROSS
    print(
        f"## Early-entry walk-forward (DIAGNOSTIC: changes no row): {len(listed)} name-days on "
        f"{len(development) + len(holdout)} dates, {len(development)} development, {len(holdout)} "
        f"holdout; {'gross' if costs is None else f'half-spread {args.half_spread_bps} bps'}\n"
    )
    days = [load_day(args.catalog, s, d) for d, s in listed if d in development]
    for number, fold in enumerate(walk_forward(days, development, cost), 1):
        judged = [run.day for run in fold.runs]
        print(
            f"**Fold {number}**: chosen on {len(fold.chosen_on)} dates, judged on "
            f"{len(fold.judged)} later dates: `{fold.arm}`\n\n"
            + _at_each_cost(judged, ARMS[fold.arm], costs)
            + "\n"
        )
    chosen = choose(days, cost)
    print(
        f"**Chosen on all development dates**: `{chosen}`\n\n" + "\n".join(arms_table(days, cost))
    )
    if args.open_holdout:
        if chosen in pending:
            raise ValueError(f"{chosen} is the arm this opening replays, so it is not pending")
        arms = None
        if pending:
            arms = {chosen: {"commit": registered_commit("HEAD"), "status": REPLAYED}, **pending}
        reports = []

        def evaluate() -> None:
            held_out = [load_day(args.catalog, s, d) for d, s in listed if d in holdout]
            if all(day.missing is not None for day in held_out):
                raise LookupError(
                    "none of the holdout's names is in the catalog, so the holdout stays unopened"
                )
            reports.append(_at_each_cost(held_out, ARMS[chosen], costs))

        marker = open_holdout(args.days_file, dates=holdout, replay=evaluate, arms=arms)
        report = reports[0]
        manifest = "" if arms is None else f"; its arms in {_manifest(marker, holdout).name}"
        print(
            f"\n**Holdout, opened once (each held date marked in {marker}{manifest})**: `{chosen}`\n\n"
            + report
        )
    return 0


def _pending_arg(text: str) -> tuple[str, str]:
    name, equals, commit = (part.strip() for part in text.partition("="))
    if not equals or not name or not commit:
        raise argparse.ArgumentTypeError(f"{text!r} is not ARM=COMMIT")
    return name, commit


def _freeze(args: argparse.Namespace) -> int:
    for flag, named in (
        ("--admitted", args.admitted),
        ("--pending-arm", args.pending_arm),
        ("--not-admitted", args.not_admitted),
        ("--reason", args.reason),
    ):
        names = [name for name, _ in named or ()]
        if len(set(names)) != len(names):
            raise ValueError(f"{flag} names an arm twice")
    out = freeze_opening(
        args.freeze,
        holdout_list=args.holdout_list,
        amendments=args.amendments,
        population_rule=args.population_rule,
        admitted=dict(args.admitted or ()),
        pending=dict(args.pending_arm or ()),
        bars_read=args.bars_read,
        not_admitted=dict(args.not_admitted or ()),
        reasons=dict(args.reason or ()),
    )
    record = json.loads(out.read_text(encoding="utf-8"))
    statuses = [arm["status"] for arm in record["arms"].values()]
    print(
        f"frozen: {out} at {record['commit']}: {statuses.count(ADMITTED)} arms admitted, "
        f"{statuses.count(PENDING)} pending replay, {statuses.count(NOT_ADMITTED)} left out; "
        "nothing read"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.early_entry_study",
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument("--catalog", type=Path, help="a catalog root of one feed")
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
        "--pending-arm",
        action="append",
        type=_pending_arg,
        metavar="ARM=COMMIT",
        help="with --open-holdout or --freeze: an arm registered at COMMIT (HEAD or an ancestor of "
        "it) whose replay on these dates is pending; the opening lists it (ADR 0100)",
    )
    parser.add_argument(
        "--freeze",
        type=Path,
        metavar="OUT",
        help="freeze ADR 0096's opening into OUT, written once: the arms admitted and pending, the "
        "holdout list's and the amendment's hashes; reads no catalog and no list of names",
    )
    parser.add_argument("--holdout-list", type=Path, help="with --freeze: the tracked held dates")
    parser.add_argument("--amendments", type=Path, help="with --freeze: the tracked amendment")
    parser.add_argument("--population-rule", help="with --freeze: the population rule, by citation")
    parser.add_argument(
        "--admitted",
        action="append",
        type=_pending_arg,
        metavar="ARM=COMMIT",
        help="with --freeze: an arm admitted to the opening, registered at COMMIT",
    )
    parser.add_argument(
        "--not-admitted",
        action="append",
        type=_pending_arg,
        metavar="ARM=REASON",
        help="with --freeze: an arm left out of the opening, with its reason and the record's line",
    )
    parser.add_argument(
        "--reason",
        action="append",
        type=_pending_arg,
        metavar="GROUP=TEXT",
        help="with --freeze: the reason and line for the 'admitted' or 'pending' group",
    )
    parser.add_argument(
        "--bars-read",
        type=Path,
        help="with --freeze: what has read holdout bars and why, one line each",
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
    freeze_only = (
        args.holdout_list,
        args.amendments,
        args.population_rule,
        args.admitted,
        args.not_admitted,
        args.reason,
    )
    if args.freeze is not None:
        others = (args.catalog, args.day, args.symbols, args.days_file, args.sha256)
        if (
            any(value is not None for value in others)
            or args.open_holdout
            or args.arms
            or args.start != PART_START
            or args.half_spread_bps is not None
        ):
            parser.error("--freeze reads no catalog and no list: give it only its own options")
        if None in (args.holdout_list, args.amendments, args.population_rule, args.bars_read):
            parser.error(
                "--freeze needs --holdout-list, --amendments, --population-rule and --bars-read"
            )
        return _freeze(args)
    if any(value is not None for value in freeze_only) or args.bars_read is not None:
        parser.error("--freeze's own options go with --freeze")
    if args.catalog is None:
        parser.error("--catalog is required")
    one_day = args.day is not None and bool(args.symbols)
    a_list = args.days_file is not None and bool(args.sha256)
    if one_day == a_list or (a_list and (args.day or args.symbols or args.arms)):
        parser.error("give --day with --symbols, or --days-file with --sha256, and not both")
    if args.open_holdout and not a_list:
        parser.error("--open-holdout goes with --days-file")
    if args.pending_arm and not args.open_holdout:
        parser.error("--pending-arm goes with --open-holdout")
    names = [name for name, _ in args.pending_arm or ()]
    if len(set(names)) != len(names):
        parser.error("--pending-arm names an arm twice")
    if a_list and args.start != PART_START:
        parser.error("--from goes with --day: a list is replayed from 04:00")
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
