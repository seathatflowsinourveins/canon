"""The movers ranking's arithmetic as pure functions, for the ranking actor: a per-symbol tally of today's
bars, a 15-minute segment baseline and its file, one generation over the whole universe with its filters,
and the top entries in the shape the ranking signal's codec takes (ADR 0083, gap 1; ADR 0087, proposed).

No network, no engine and no clock: every input is a plain value, and the only I/O is the baseline file's
reader and writer. Bars are NautilusTrader `Bar`s from two documented producers: the adapter's
`canon_alpaca.data.parse_bar` gives EXTERNAL bars an open `ts_event` and synthesizes `ts_init` as open plus
interval; NautilusTrader's default live engine gives INTERNAL bars a close `ts_event` and the same close
`ts_init` (`time_bars_timestamp_on_close=True`, `BarIntervalType.LeftOpen`). Nondefault engine timestamp
settings are outside this input contract. Bounds use the source and `BarSpecification.get_interval_ns`,
independently of initialization. Neither producer supplies a measured receipt clock here. Prior closes and opens are
numbers; asset fields are those the adapter keeps in `Equity.info` (`canon_alpaca.providers.parse_equity`);
corporate actions are alpaca-py `ForwardSplit` and `ReverseSplit` records, read by the caller through
alpaca-py's `CorporateActionsClient`; halt state is NautilusTrader's `MarketStatusAction`.

Upstream, at the pins (`nt:` nautilus-trader v2.0.0rc8, `ap:` alpaca-py v0.45.0), used as shipped:

* A bar's dollar volume is NautilusTrader's `VolumeWeightedAveragePrice` of the bar times its volume. Its
  `handle_bar` takes the bar's typical price, `(high + low + close) / 3`
  (`nt:crates/indicators/src/average/vwap.rs:57-63`, `support/mod.rs:36-38`), and skips a bar whose high,
  low and close are not ordered; NautilusTrader's `Bar` refuses such a bar when it is built
  (`nt:crates/model/src/data/bar.rs:1199-1203`), so every bar counts. It is an approximation of traded
  dollars: `Bar` carries no VWAP and no trade count (`nt:python/nautilus_trader/model/__init__.pyi`, class
  `Bar`), so Alpaca's own bar VWAP is not used.
* Asset fields read: `name`, `tradable` and `status` as `parse_equity` copies them from alpaca-py's `Asset`
  (`ap:alpaca/trading/models.py:37-77`); `parse_equity` admits `us_equity` only.
* Corporate actions (`ap:alpaca/data/models/corporate_actions.py:9-33`): `symbol` and `ex_date` of
  `ForwardSplit` and `ReverseSplit`.
* Halt state: `MarketStatusAction` (`nt:python/nautilus_trader/model/__init__.pyi`, its class of that
  name).
* Times: NautilusTrader's `dt_to_unix_nanos` and `unix_nanos_to_dt` (`nautilus_trader.core.datetime`).
* A trade date's bars run from 20:00 New York the evening before, its overnight session, to 20:00 on the
  date (`canon_trading.session_window.session_part`, after Alpaca's 24/5 page).

No upstream ships the ranking itself (ADR 0083, gap 1); the definitions below are the movers requirement's,
and every threshold is a registered UNCALIBRATED row:

* The move: the last bar's close over the latest split-adjusted official regular close of a session
  before the trade date, less one; the caller supplies that close and its session (`Reference`). From
  16:00 to 20:00 that is still the prior session's close, not today's (ADR 0087). The move
  from today's regular open is recorded beside it.
* The dollar-volume ratio: today's dollar volume from `PART_START` New York the evening before (the
  whole trade date, its overnight session included: the lead's definition, ADR 0087, 2026-11-11) to the
  symbol's data time, over the baseline at the same New York clock. The baseline is the mean, over the
  symbol's latest `BASELINE_SESSIONS` earlier sessions, of the cumulative dollar volume at the end of each
  15-minute segment from `PART_START` the evening before to 20:00 (`SEGMENTS` values, 96). The lead's
  rule: a numerator counts exactly the clock minutes its baseline covers, so while the baseline holds no
  overnight feed a ratio past 04:00 counts from `DAY_START` on both sides (`Row.ratio_span`); inside a segment it is the cumulative value at the
  segment's start plus the elapsed share of that segment's own dollar volume, an approximation. A session
  read with no trade counts zero, and no holiday is inferred from missing bars. Fewer earlier sessions are
  used when that is all there is, and named (`Baseline.name`).
* An established baseline (the lead's definition, 2026-11-11, ADR 0087): a symbol's baseline at its
  clock is established only if it traded in that clock's segment in every one of the `BASELINE_SESSIONS`
  sessions of the lookback (`segment_trade_counts`, `Baseline.established`); a baseline file without
  trade counts leaves that unknown, and the name ineligible. A name without one gets no ratio and joins
  the zero-baseline tier, with no denominator floor.
* The score: move times ratio, with no price or market-cap floor. Only a positive score enters the top: the
  practice is long only (ADR 0083), as `canon_trading.movers_stream.rank` already ranks only risers.
  The listing move condition, a move of at least `MOVE_CONDITION`, applies to every name in both tiers.
  Decided by the lead on 2026-11-11 (ADR 0087): names without an established baseline, a zero
  baseline among them, form a tier ordered by dollars, admitted only at or above the median dollar
  volume of the eligible finite positive-score names (established, so passing the move condition).
  With no such name the tier ranks by dollars alone. `zero_baseline_arm` selects this relative rule or
  the paired excluded arm, which ranks no name without an established baseline. The optional absolute
  `zero_baseline_dollar_floor` has no numeric default: unset means not applied. Zero dollars over a zero
  or unestablished baseline is unranked and missing baselines stay unknown. Infinity represents only the
  tier.
* Each name's last bar must be at most `max_data_age_secs` old at compute time (default
  `MAX_DATA_AGE_SECS = 180`, UNCALIBRATED). A stale name retains its row and bar timestamp, with
  `last_bar:stale` in `excluded_by`, but its price, moves, dollar-volume ratio and score are unknown;
  another name's fresh bar does not make it rankable.
* Ties: trade acceleration is not computed, is recorded unknown, and breaks no tie; tied names go by
  instrument id.
* Filters: tradable; halt state; security type; a split since the reference close; one share at the last
  close fitting the order cap (a screen: the strategy's check at its limit price binds). An unknown filter
  makes a name ineligible, except an unknown halt state while `HALT_UNKNOWN_ELIGIBLE` holds, which
  `Row.unchecked` records; so does an unknown reference or ratio, so an eligible name is a rankable one.
* Security type: Alpaca's asset record has no security-type field (Alpaca's asset reference lists none, and
  `asset_class` names an asset class, not a security type), so the filter takes a security type per
  symbol as an input and an unknown type is ineligible. Until a source exists, `security_type_from_name`
  derives it from words in the asset's name: a PROXY with no upstream field behind it, UNCALIBRATED.
* The universe: every asset and every symbol with a tally is a row; what is not known stays None, never
  zero.
* Clock fields: a generation carries its data time (`published_ns`, labelled `publication`: the end of the
  latest completed bar it used), its latest initialization time (`received_ns`, labelled `receipt`) and
  its compute time (`generated_ns`, labelled `generation`), given by the caller. The legacy `receipt`
  label means `ts_init`, not measured arrival: both producers above set it to the interval end, so it
  duplicates publication for their bars. Replayed initialization times may differ and never advance
  data time for baselines, the per-symbol open gate or the reported publication-to-compute latency.
  No maximum-data-age filter is implemented here; actor freshness policy is unbuilt (ADR 0087).
* Invalid input raises `InvalidGeneration`, and the caller publishes nothing, so the ranking goes stale:
  a bar outside the trade date, of another venue or instrument, a second bar for one minute, or one older
  than or overlapping the last (`check_bars`); an input the data time could not have known (a reference
  close of the session or later, a bar ending after the compute time, or initialization after that time);
  an order cap that is not a positive amount; a key that is not a symbol. Per row instead: today's open
  for a symbol none of whose own bars has yet ended after 09:30 is unknown on that row and never used, and
  a symbol the signal's codec cannot carry is excluded as `symbol:unencodable`.
* A minute whose bars never arrived, as when the bar stream restarts across a minute's end, is absent: a
  tally's dollar volume is short by that minute and `Tally.minutes` by one, and nothing here can tell a
  lost minute from one with no trade. The nightly baseline is rebuilt from bars read again over REST.

UNVERIFIED: whether Alpaca's corporate-actions data lists a split before Alpaca processes it; whether the
name proxy separates warrants, units and rights from shares across the whole asset list.
"""

from __future__ import annotations

import functools
import json
import math
import statistics
from dataclasses import dataclass
from dataclasses import replace
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from alpaca.trading.enums import AssetStatus
from canon_alpaca.constants import VENUE
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.indicators import VolumeWeightedAveragePrice
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import MarketStatusAction
from nautilus_trader.model import Symbol

from canon_trading.movers_rank_signal import MAX_ENTRIES
from canon_trading.movers_rank_signal import RankEntry
from canon_trading.rank_shared import NOT_COMMON
from canon_trading.rank_shared import write_atomically

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from collections.abc import Sequence
    from pathlib import Path

    from alpaca.data.models.corporate_actions import ForwardSplit
    from alpaca.data.models.corporate_actions import ReverseSplit
    from nautilus_trader.model import Bar
    from nautilus_trader.model import BarType

NEW_YORK = ZoneInfo("America/New_York")
TOP = MAX_ENTRIES
"""Compatibility name for the signal's shared capacity (UNCALIBRATED)."""
BASELINE_SESSIONS = 5
"""Earlier sessions the dollar-volume baseline is the mean of (UNCALIBRATED)."""
PART_START = time(20, 0)
"""The start, New York, of the part of the session a ratio compares, on the evening before the trade
date: the whole trade date, its overnight session included (the lead's definition, ADR 0087,
2026-11-11). Monday's part starts on Sunday evening."""
PART_END = time(20, 0)
"""The end of the part and of the trade date's bars, 20:00 New York on the trade date."""
DAY_START = time(4, 0)
"""Where `sip`'s extended hours begin, New York: a ratio counts from here while its baseline holds no
overnight feed (`BaselineRead.overnight`), the lead's rule of 2026-11-11 (ADR 0087)."""
OVERNIGHT_FEED = "boats"
"""The overnight feed's name, as the baseline writer lists it in its header's `feeds`."""
REGULAR_OPEN = time(9, 30)
"""The regular session's open, New York: until a bar has ended after it, today's regular open is not
known. That an early close moves only the close, never the open, is UNVERIFIED against Alpaca's
calendar."""
SEGMENT_MINUTES = 15
"""A baseline segment's length (UNCALIBRATED)."""
PART_MINUTES = 24 * 60
"""Minutes from `PART_START` the evening before to `PART_END`: 1440. A part holds no clock change:
New York's fall on a Sunday at 02:00, and no trade date's part spans a Sunday morning."""
SEGMENTS = PART_MINUTES // SEGMENT_MINUTES
"""Segments from `PART_START` to `PART_END`: 96; segment 32 opens at 04:00, 54 at 09:30."""
HALT_UNKNOWN_ELIGIBLE = True
"""Whether a name with no halt state is eligible (UNCALIBRATED). True while the adapter has no status
subscription (ADR 0083, gap 4), so the unknown is recorded per name; False makes the filter strict."""
MAX_DATA_AGE_SECS = 180
"""Maximum age of each name's latest bar at compute time, inclusive (UNCALIBRATED; ADR 0087)."""
MOVE_CONDITION = 0.03
"""The listing move condition (ADR 0083, ADR 0092; the move only, not the ignition row's volume ratio),
UNCALIBRATED: a name whose move is below it is not ranked, in either tier. The lead's definition,
2026-11-11. A move equal to it within `math.isclose`'s default tolerance passes, so an exact 3% that the
quotient rounds below still counts."""
SAYS_NOTHING = frozenset(
    {MarketStatusAction.NONE, MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE}
)
"""Statuses that say nothing of whether a name trades, so its halt state stays unknown. `TRADING` admits a
name and every other status (a halt, a pause, quoting only, a cross, a close) excludes it: no entry unless
the venue says the name is trading. How the adapter will map Alpaca's trading-status codes onto these is
UNVERIFIED until it has a status subscription (ADR 0083, gap 4)."""
BASELINE_FORMAT = "canon-movers-baseline/2"
"""Version 2: 96 segments from 20:00 the evening before. Version 1, 64 from 04:00, is refused whole."""

SEGMENT_NS = SEGMENT_MINUTES * 60 * 1_000_000_000
if PART_MINUTES != SEGMENTS * SEGMENT_MINUTES:
    raise ValueError("the part from PART_START to PART_END is not a whole number of segments")


class InvalidGeneration(ValueError):
    """The inputs cannot make a valid generation; nothing is published from them."""


class SecurityType(StrEnum):
    SHARE = "share"
    """Admitted: a common share or a depositary receipt. From the name proxy, anything it does not name a
    warrant, unit or right, so an ETF, a preferred or a note is admitted too; the filter excludes only the
    three. ADR 0083's starting row names every NMS common stock and ADR, so an admitted ETF, preferred or
    note departs from it until a security-type source exists (ADR 0087's recorded proxy)."""
    WARRANT = "warrant"
    UNIT = "unit"
    RIGHT = "right"


class ZeroBaselineArm(StrEnum):
    """The lead's registered zero-baseline row and its paired walk-forward arm (ADR 0087)."""

    RELATIVE_FLOOR = "relative-floor"
    EXCLUDE = "exclude"


@functools.cache
def ny_ns(day: date, clock: time) -> int:
    """`clock` New York on `day`, in UNIX nanoseconds; cached, as a session's bounds repeat per bar."""
    return dt_to_unix_nanos(datetime.combine(day, clock, NEW_YORK))


def ny_time(ts_ns: int) -> datetime:
    return unix_nanos_to_dt(ts_ns).astimezone(NEW_YORK)


def part_start_ns(session: date) -> int:
    """The start of `session`'s part and trade date: `PART_START` New York the evening before."""
    return ny_ns(session - timedelta(days=1), PART_START)


def bar_bounds_ns(bar: Bar) -> tuple[int, int]:
    """(Open, end) for adapter EXTERNAL or default live-engine INTERNAL time bars, not from ts_init.

    NautilusTrader d53849b2: crates/live/src/node/config.rs:2214-2219 defaults to close-stamped,
    left-open time bars; crates/data/src/aggregation.rs:1735-1752 stamps ts_event and ts_init at close.
    canon_alpaca.data.parse_bar stamps EXTERNAL ts_event at open. Nondefault engine stamps are not an
    input of this module. Equal ts_event/ts_init on an INTERNAL bar is normal, not a zero-length bar.
    """
    try:
        interval = bar.bar_type.spec.get_interval_ns()
    except ValueError as e:
        raise InvalidGeneration(f"{bar.bar_type} has no usable time interval: {e}") from e
    if interval <= 0:
        raise InvalidGeneration(f"{bar.bar_type} has a nonpositive time interval: {interval}")
    if bar.bar_type.is_internally_aggregated():
        return bar.ts_event - interval, bar.ts_event
    return bar.ts_event, bar.ts_event + interval


def bar_end_ns(bar: Bar) -> int:
    """The interval end of either supported producer (`bar_bounds_ns`)."""
    return bar_bounds_ns(bar)[1]


def check_bars(
    bars: Iterable[Bar],
    session: date,
    after: tuple[int, int] | None = None,
    bar_type: BarType | None = None,
) -> list[Bar]:
    """Eagerly check `bars` of one full bar type on the adapter's venue, inside `session`'s trade date (opening from
    20:00 New York the evening before, ending by 20:00 on the date) and opening no earlier than the one
    before ends; `after` is the (open, end) of a bar already counted and `bar_type` its full type.
    `InvalidGeneration` otherwise: a duplicate, a correction, an older or overlapping bar, or a bar of
    another venue, instrument, specification or source is not counted twice or under another name."""
    first = part_start_ns(session)
    end = ny_ns(session, PART_END)
    if end - first != PART_MINUTES * 60 * 1_000_000_000:  # code review, overnight part, finding 3
        raise InvalidGeneration(
            f"{session}'s part is {(end - first) / 3.6e12:g} hours, not {PART_MINUTES // 60}: it spans a "
            "clock change, so it is no trade date's"
        )
    previous = after
    checked = []
    for bar in bars:
        bar_id = bar.bar_type.instrument_id
        if bar_id.venue != VENUE:
            raise InvalidGeneration(f"{bar_id} has a bar on venue {bar_id.venue}, not {VENUE}")
        if bar_type is None:
            bar_type = bar.bar_type
        elif bar.bar_type != bar_type:
            raise InvalidGeneration(
                f"a bar type {bar.bar_type} (instrument {bar_id}) among bars of {bar_type}"
            )
        interval_start, interval_end = bar_bounds_ns(bar)
        if interval_start < first or interval_end > end:
            raise InvalidGeneration(
                f"{bar.bar_type.instrument_id} has a bar from {ny_time(interval_start)} to "
                f"{ny_time(interval_end)}, outside the trade date {session}"
            )
        if previous is not None and interval_start < previous[1]:
            what = (
                "a duplicate" if interval_start == previous[0] else "out of order or overlapping: a"
            )
            raise InvalidGeneration(
                f"{bar.bar_type.instrument_id} has {what} bar opening {ny_time(interval_start)} after "
                f"one from {ny_time(previous[0])} to {ny_time(previous[1])}"
            )
        previous = (interval_start, interval_end)
        checked.append(bar)
    return checked


def bar_dollars(bar: Bar) -> float:
    """One bar's dollars using a fresh upstream VWAP indicator; nested or concurrent calls share no state."""
    vwap = VolumeWeightedAveragePrice()
    vwap.handle_bar(bar)
    return vwap.value * bar.volume.as_double() if vwap.initialized else 0.0


@dataclass(frozen=True)
class Tally:
    """One symbol's bars of one trade date so far, added in time order."""

    session: date
    bar_type: BarType | None = None
    """The full type of every counted bar: instrument, specification and source."""
    last: float | None = None
    """The close of the latest bar."""
    last_open_ns: int | None = None
    last_bar_ns: int | None = None
    """The latest bar's interval end, derived from the producer's timestamp convention."""
    last_received_ns: int | None = None
    """Latest `ts_init` among counted bars; both supported producers synthesize it at interval close.
    Retains its legacy name; this is not a measured receipt time."""
    dollar_volume: float = 0.0
    """Dollar volume of the trade date's bars, from `PART_START` the evening before."""
    dollars_before_day: float = 0.0
    """The part of `dollar_volume` in bars ending by `DAY_START`, kept apart."""
    dollars_from_day: float = 0.0
    """The part of `dollar_volume` in bars opening at or after `DAY_START`, added up on its own so a large
    night does not round the day away (code review of the span, finding 4)."""
    minutes: int = 0
    """Bars added."""

    @property
    def instrument_id(self) -> InstrumentId | None:
        return None if self.bar_type is None else self.bar_type.instrument_id

    def add(self, bar: Bar) -> Tally:
        """This tally with `bar` added; `InvalidGeneration` for a bar outside the trade date, a second bar
        for one minute, an older bar or one overlapping the last (`check_bars`)."""
        after = (
            None
            if self.last_open_ns is None or self.last_bar_ns is None
            else (self.last_open_ns, self.last_bar_ns)
        )
        (bar,) = check_bars([bar], self.session, after, self.bar_type)
        interval_start, interval_end = bar_bounds_ns(bar)
        day_start = ny_ns(self.session, DAY_START)
        if interval_start < day_start < interval_end:  # code review of the span, finding 1
            raise InvalidGeneration(
                f"a bar from {ny_time(interval_start):%H:%M} to {ny_time(interval_end):%H:%M} crosses "
                f"{DAY_START:%H:%M}, where a day ratio's span starts: its dollars cannot be split"
            )
        dollars = bar_dollars(bar)
        by_day = interval_start >= day_start
        received = (
            bar.ts_init
            if self.last_received_ns is None
            else max(self.last_received_ns, bar.ts_init)
        )
        return replace(
            self,
            bar_type=bar.bar_type,
            last=bar.close.as_double(),
            last_open_ns=interval_start,
            last_bar_ns=interval_end,
            last_received_ns=received,
            dollar_volume=self.dollar_volume + dollars,
            dollars_before_day=self.dollars_before_day + (0.0 if by_day else dollars),
            dollars_from_day=self.dollars_from_day + (dollars if by_day else 0.0),
            minutes=self.minutes + 1,
        )


@dataclass(frozen=True)
class Baseline:
    """One symbol's mean cumulative dollar volume at the end of each segment, over `sessions`."""

    cumulative: tuple[float, ...]
    sessions: tuple[date, ...]
    traded_sessions: tuple[int, ...] | None = None
    """For each segment, in how many of `sessions` the symbol traded in it (`segment_trade_counts`);
    None when the file that held the baseline did not say, so whether it is established is unknown."""

    def __post_init__(self) -> None:
        if len(self.cumulative) != SEGMENTS:
            raise ValueError(f"a baseline has {SEGMENTS} segments, not {len(self.cumulative)}")
        if not all(math.isfinite(v) and v >= 0 for v in self.cumulative) or any(
            b < a for a, b in zip(self.cumulative, self.cumulative[1:], strict=False)
        ):
            raise ValueError("a baseline is finite, not negative and never falls")
        if len(self.sessions) > BASELINE_SESSIONS or list(self.sessions) != sorted(
            set(self.sessions)
        ):
            raise ValueError(
                f"a baseline has at most {BASELINE_SESSIONS} distinct sessions, in order"
            )
        if self.traded_sessions is not None and (
            len(self.traded_sessions) != SEGMENTS
            or any(
                type(n) is not int or not 0 <= n <= len(self.sessions) for n in self.traded_sessions
            )
        ):
            raise ValueError(
                f"a baseline's trade count is {SEGMENTS} whole numbers of its sessions, one a segment"
            )

    def established(self, clock: time) -> bool | None:
        """The lead's definition (ADR 0087, 2026-11-11): the baseline of the segment that holds the
        minute before `clock` is established only if the symbol traded in that segment in every one of
        the `BASELINE_SESSIONS` sessions of the lookback. None when the trade counts are unknown."""
        if self.traded_sessions is None:
            return None
        return self.traded_sessions[segment_before(clock)] == BASELINE_SESSIONS

    @property
    def short(self) -> bool:
        return len(self.sessions) < BASELINE_SESSIONS

    @property
    def name(self) -> str:
        n = len(self.sessions)
        if n == 0:
            return "none: no earlier session read"
        days = ", ".join(d.isoformat() for d in self.sessions)
        return (
            f"mean cumulative dollar volume by {SEGMENT_MINUTES}-minute segment from "
            f"{PART_START:%H:%M} New York the evening before, {n} of {BASELINE_SESSIONS} earlier "
            f"sessions ({days})"
        )


def segment_baseline(sessions: Mapping[date, Sequence[Bar]], session: date) -> Baseline:
    """The baseline for `session` from earlier sessions' bars of one symbol: the latest `BASELINE_SESSIONS`
    dates before `session`. A date present with no bar is a session read with no trade, so it counts zero;
    a date absent is not read. A bar outside its date's trade date, a second bar for one minute, an older
    or overlapping bar (`check_bars`) or a bar in the part that crosses a segment's end raises
    `InvalidGeneration`: its dollars would land in one segment."""
    days = sorted(d for d in sessions if d < session)[-BASELINE_SESSIONS:]
    totals = [0.0] * SEGMENTS
    traded = [0] * SEGMENTS
    bar_type: BarType | None = None  # one type, including source/specification, across every day
    for day in days:
        start = part_start_ns(day)
        running, segment = 0.0, [0.0] * SEGMENTS
        trades: set[int] = set()
        for bar in check_bars(sessions[day], day, None, bar_type):  # each inside [start, end]
            bar_type = bar.bar_type
            interval_start, interval_end = bar_bounds_ns(bar)
            k = (interval_start - start) // SEGMENT_NS
            if (interval_end - start - 1) // SEGMENT_NS != k:
                raise InvalidGeneration(
                    f"{bar.bar_type.instrument_id} has a bar from {ny_time(interval_start)} to "
                    f"{ny_time(interval_end)} crossing a {SEGMENT_MINUTES}-minute segment's end"
                )
            segment[k] += bar_dollars(bar)
            if bar.volume.as_double() > 0:
                trades.add(k)
        for k in range(SEGMENTS):
            running += segment[k]
            totals[k] += running
        for k in trades:
            traded[k] += 1
    n = len(days)
    return Baseline(
        cumulative=tuple(t / n if n else 0.0 for t in totals),
        sessions=tuple(days),
        traded_sessions=tuple(traded),
    )


def segment_trade_counts(sessions: Mapping[date, Sequence[Bar]], session: date) -> tuple[int, ...]:
    """For each segment, in how many of the sessions `segment_baseline` reads (the latest
    `BASELINE_SESSIONS` before `session`) the symbol traded in it: a bar with volume inside the part
    whose interval (`bar_bounds_ns`) lies in the segment. The same checks as `segment_baseline`."""
    counts = segment_baseline(sessions, session).traded_sessions
    assert counts is not None  # segment_baseline always counts
    return counts


def part_minutes(clock: time) -> float:
    """Minutes from `PART_START` the evening before to `clock` New York, in (0, `PART_MINUTES`]: a clock
    at or after 20:00 is that evening's, any other the trade date's. 20:00 itself is the part's end,
    since no bar of the trade date can end at its start."""
    minutes = (
        clock.hour * 60
        + clock.minute
        + clock.second / 60
        + clock.microsecond / 60e6
        - (PART_START.hour * 60 + PART_START.minute)
    ) % PART_MINUTES
    return minutes or PART_MINUTES


def segment_before(clock: time) -> int:
    """The segment that holds the minute before `clock` New York: a bar ending at a segment's end is
    that segment's last (`part_minutes` places the clock in the part)."""
    return math.ceil(part_minutes(clock) / SEGMENT_MINUTES) - 1  # in [0, SEGMENTS)


def baseline_at(baseline: Baseline, clock: time) -> float | None:
    """The baseline's dollar volume from `PART_START` the evening before to `clock` New York
    (`part_minutes`): pro rata inside a segment."""
    if not baseline.sessions:
        return None
    minutes = part_minutes(clock)
    if minutes >= PART_MINUTES:
        return baseline.cumulative[-1]
    k = int(minutes // SEGMENT_MINUTES)
    share = (minutes - k * SEGMENT_MINUTES) / SEGMENT_MINUTES
    before = baseline.cumulative[k - 1] if k else 0.0
    return before + share * (baseline.cumulative[k] - before)


NO_BASELINE = "no baseline: dollar tier under the move condition"
"""Each row's stated basis when a part ranks with no baseline file (`rank`'s `no_baseline`)."""


@dataclass(frozen=True)
class BaselineRead:
    baselines: Mapping[str, Baseline]
    problem: str | None
    """Why there is no baseline, when the file is missing or unreadable; None when it was read."""
    overnight: bool = False
    """Whether the writer's header lists the overnight feed among its `feeds`, so the baselines hold the
    night's dollars; the caller passes it to `rank` as `overnight_baseline`."""
    absent: bool = False
    """Whether the file does not exist, which a part ranking without a baseline may pass (`rank`'s
    `no_baseline`); a file that exists and cannot be used is never absent."""


def write_baselines(
    path: Path,
    session: date,
    baselines: Mapping[str, Baseline],
    *,
    header: Mapping[str, object] | None = None,
) -> None:
    """Write one JSON object through the shared replace-whole writer, with cleanup on failure. Each
    baseline's trade counts, when known, go beside it as `traded_sessions`, read against the file's
    `lookback_sessions`. `header` adds the writer's own keys (its source, what it left out);
    `read_baselines` ignores them, and none may replace a key the format defines."""
    document: dict[str, object] = {
        "format": BASELINE_FORMAT,
        "session": session.isoformat(),
        "part_start": f"{PART_START:%H:%M}",
        "segment_minutes": SEGMENT_MINUTES,
        "segments": SEGMENTS,
        "lookback_sessions": BASELINE_SESSIONS,
        "symbols": {
            symbol: {
                "sessions": [d.isoformat() for d in b.sessions],
                "cumulative": list(b.cumulative),
                **(
                    {}
                    if b.traded_sessions is None
                    else {"traded_sessions": list(b.traded_sessions)}
                ),
            }
            for symbol, b in sorted(baselines.items())
        },
    }
    if clash := sorted(set(header or {}) & set(document)):
        raise ValueError(f"a baseline header may not replace {', '.join(clash)}")
    document.update(header or {})
    write_atomically(path, json.dumps(document, allow_nan=False))


def read_baselines(path: Path, session: date) -> BaselineRead:
    """The baselines `write_baselines` wrote for `session`; a missing or unreadable file, or one for
    another session or segment, gives no baseline and says why."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("format") != BASELINE_FORMAT:
            old = isinstance(document, dict) and document.get("format") == "canon-movers-baseline/1"
            return BaselineRead(
                {},
                f"{path} is not a {BASELINE_FORMAT} file"
                + (" (version 1 was written for the 04:00 part)" if old else ""),
            )
        if document.get("session") != session.isoformat():
            return BaselineRead(
                {}, f"{path} is for the session {document.get('session')}, not {session}"
            )
        if (
            document.get("part_start") != f"{PART_START:%H:%M}"
            or document.get("segment_minutes") != SEGMENT_MINUTES
            or document.get("segments") != SEGMENTS
        ):
            return BaselineRead(
                {},
                f"{path} has other segments than {SEGMENTS} of {SEGMENT_MINUTES} minutes from "
                f"{PART_START:%H:%M} the evening before",
            )
        lookback = document.get("lookback_sessions")
        if (
            lookback is None
        ):  # code review of the definitions, finding 3: not one row each, silently
            return BaselineRead(
                {},
                f"{path} has no lookback_sessions: written before trade counts, so no name in it "
                "can be established",
            )
        if type(lookback) is not int or lookback != BASELINE_SESSIONS:
            return BaselineRead(
                {}, f"{path} has a lookback of {lookback!r} sessions, not {BASELINE_SESSIONS}"
            )
        symbols = document["symbols"]
        if not isinstance(symbols, dict):
            raise TypeError("symbols is not an object")
        baselines = {}
        for symbol, entry in symbols.items():
            if not isinstance(entry, dict):
                raise TypeError(f"{symbol}'s baseline is not an object")
            cumulative, sessions = entry["cumulative"], entry["sessions"]
            if not isinstance(cumulative, list) or any(
                type(v) not in (int, float) for v in cumulative
            ):
                raise ValueError(f"{symbol}'s cumulative is not an array of numbers")
            if not isinstance(sessions, list) or any(not isinstance(d, str) for d in sessions):
                raise ValueError(f"{symbol}'s sessions is not an array of date strings")
            traded = entry.get("traded_sessions")
            if traded is not None and (
                not isinstance(traded, list) or any(type(n) is not int for n in traded)
            ):
                raise ValueError(f"{symbol}'s traded_sessions is not an array of whole numbers")
            baselines[symbol] = Baseline(
                cumulative=tuple(float(v) for v in cumulative),
                sessions=tuple(date.fromisoformat(d) for d in sessions),
                traded_sessions=None if traded is None else tuple(traded),
            )
        late = sorted(s for s, b in baselines.items() if b.sessions and b.sessions[-1] >= session)
        if late:
            return BaselineRead({}, f"{path} has sessions on or after {session} for {late[:5]}")
    except FileNotFoundError as e:
        return BaselineRead({}, f"{path} could not be read: {type(e).__name__}: {e}", absent=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as e:
        return BaselineRead({}, f"{path} could not be read: {type(e).__name__}: {e}")
    feeds = document.get("feeds", [])
    if not isinstance(feeds, list) or any(not isinstance(f, str) for f in feeds):
        # code review of the span, finding 9: an unreadable feed list would silently drop the night
        return BaselineRead({}, f"{path}'s feeds is not an array of feed names: {feeds!r}")
    return BaselineRead(baselines, None, OVERNIGHT_FEED in feeds)


@dataclass(frozen=True)
class Reference:
    """The latest completed official regular-session close, as the caller read it split-adjusted, and the
    session it closed. Not every source adjusts a close for a split before its ex-date is processed, so
    the split filter excludes any name with a split since that session rather than trust the close."""

    close: float
    session: date


@dataclass(frozen=True)
class SplitRead:
    """Alpaca's forward and reverse splits for the whole universe with an ex-date from `start` to `end`,
    both inclusive, as the caller read them."""

    start: date
    end: date
    actions: Sequence[ForwardSplit | ReverseSplit]

    @functools.cached_property
    def ex_dates(self) -> Mapping[str, tuple[date, ...]]:
        """Each symbol's ex-dates, indexed once per read rather than scanned per name."""
        found: dict[str, list[date]] = {}
        for a in self.actions:
            found.setdefault(a.symbol, []).append(a.ex_date)
        return {symbol: tuple(dates) for symbol, dates in found.items()}


def split_since(
    read: SplitRead | None, symbol: str, reference_session: date, session: date
) -> bool | None:
    """Whether `symbol` has a split with `reference_session < ex_date <= session`; None when no read covers
    those ex-dates."""
    if read is None or read.start > reference_session + timedelta(days=1) or read.end < session:
        return None
    return any(reference_session < d <= session for d in read.ex_dates.get(symbol, ()))


def security_type_from_name(name: str | None) -> SecurityType | None:
    """The PROXY for a security type from an Alpaca asset's name (UNCALIBRATED, no upstream field): the
    first of warrant, unit or right found as a word, else a share; None for no name."""
    if name is None or not name.strip():
        return None
    found = NOT_COMMON.search(name)
    if found is None:
        return SecurityType.SHARE
    return SecurityType(found.group(1).lower().rstrip("s"))


def security_types_from_names(
    assets: Mapping[str, Mapping[str, object]],
) -> dict[str, SecurityType]:
    """`security_type_from_name` over the asset fields by symbol; a name it cannot read is left out."""
    types = {}
    for symbol, fields in assets.items():
        name = fields.get("name")
        kind = security_type_from_name(name if isinstance(name, str) else None)
        if kind is not None:
            types[symbol] = kind
    return types


@dataclass(frozen=True)
class Row:
    instrument_id: InstrumentId
    last: float | None
    last_bar_ns: int | None
    """The latest bar's interval end."""
    last_received_ns: int | None
    """Latest initialization clock among counted bars, kept separate from interval ends."""
    minutes: int
    reference: float | None
    move: float | None
    move_from_open: float | None
    dollar_volume: float | None
    """Today's dollars over `ratio_span`, the ratio's numerator, which the tier and its floor compare."""
    dollars_before_day: float | None
    """Today's dollars before `DAY_START`, kept apart when the span starts there."""
    ratio_span: str | None
    """The clock minutes the ratio counts: "04:00-now" while the baseline holds no overnight feed and the
    data time is past 04:00, else "20:00-now"."""
    baseline: float | None
    baseline_name: str
    established: bool | None
    """Whether the baseline at the symbol's clock is established (`Baseline.established`); None when
    there is no baseline or its trade counts are unknown."""
    ratio: float | None
    score: float | None
    acceleration: float | None
    """Trade acceleration: not computed, always unknown, so it breaks no tie."""
    tradable: bool | None
    halted: bool | None
    security_type: SecurityType | None
    split_since_reference: bool | None
    fits_order_cap_at_last: bool | None
    """One share at the last close within the order cap: a screen only. An entry sends a limit above the
    last, so the strategy's own check at its limit price is the binding one."""
    excluded_by: tuple[str, ...]
    """The filters the name fails, and `<filter>:unknown` for each it cannot be checked against and that
    excludes when unknown."""
    unchecked: tuple[str, ...]
    """The filters the name passed unchecked: `halt` while `HALT_UNKNOWN_ELIGIBLE` admits an unknown."""

    @property
    def eligible(self) -> bool:
        return not self.excluded_by


@dataclass(frozen=True)
class Generation:
    session: date
    rows: tuple[Row, ...]
    """Every name of the universe, by instrument id."""
    published_ns: int | None
    """Data time: the end of the latest completed bar used; None when there was no bar today."""
    generated_ns: int
    """Compute time, as the caller's clock gave it."""
    received_ns: int | None = None
    """Latest ts_init among counted bars, under the legacy receipt label; not measured arrival."""

    @property
    def clocks(self) -> dict[str, int | None]:
        return {
            "publication": self.published_ns,
            "receipt": self.received_ns,
            "generation": self.generated_ns,
        }

    @property
    def latency_ns(self) -> int | None:
        return None if self.published_ns is None else self.generated_ns - self.published_ns


def status_after(
    held: MarketStatusAction | None, action: MarketStatusAction
) -> MarketStatusAction | None:
    """The status a name has after `action`, given the one `held`: an action that says nothing of whether
    it trades (`SAYS_NOTHING`) keeps the held one, so a short-sale restriction change cannot erase a halt.
    The caller folds each status update of the session with this and passes the result to `rank`."""
    return held if action in SAYS_NOTHING else action


def finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def is_finite_number(value: object) -> bool:
    """A numeric measurement, not bool (CPython b703b2c5 Objects/boolobject.c:118,202: bool is an int)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def check_zero_baseline_floor(value: float | None) -> None:
    if value is not None and (not is_finite_number(value) or value < 0):
        raise InvalidGeneration("zero_baseline_dollar_floor must be a finite nonnegative amount")


def check(name: str, failed: bool | None, out: list[str]) -> None:
    if failed is None:
        out.append(f"{name}:unknown")
    elif failed:
        out.append(name)


def check_known(name: str, value: object, out: list[str]) -> None:
    if value is None:
        out.append(f"{name}:unknown")


def tally_clock(tally: Tally | None) -> time | None:
    """The symbol's own data clock, New York: its latest bar's end, which `part_minutes` places in the
    part (an evening clock is the evening before)."""
    if tally is None or tally.last_bar_ns is None:
        return None
    return ny_time(tally.last_bar_ns).time()


def volume_score(
    move: float | None,
    dollars: float | None,
    baseline: float | None,
    zero_baseline_dollar_floor: float | None,
    *,
    established: bool | None,
) -> tuple[float | None, float | None]:
    """(Ratio, score); the lead's zero-baseline decision and definitions are in ADR 0087 (2026-11-11).

    A baseline that is not established (or is zero) gives no finite ratio: the name joins the
    zero-baseline tier, marked by an infinite ratio, with no denominator floor. Unknown establishment
    leaves the ratio a measurement; `rank` excludes the name as `established:unknown`. The absolute floor is optional and has no numeric default; unset means not applied. The
    relative comparison and paired arm are applied over all rows by zero_baseline_rows. Ordinary
    arithmetic overflow is unknown; a missing baseline never substitutes for zero.
    """
    check_zero_baseline_floor(zero_baseline_dollar_floor)
    if (
        dollars is None
        or baseline is None
        or not is_finite_number(dollars)
        or not is_finite_number(baseline)
        or dollars < 0
        or baseline < 0
    ):
        return None, None
    if move is not None and not is_finite_number(move):
        move = None
    if baseline == 0 or established is False:
        if dollars <= 0:  # zero over zero stays unranked
            return None, None
        score = None if move is None else 0.0 if move == 0 else math.copysign(math.inf, move)
        return math.inf, score
    ratio = finite(dollars / baseline)
    return ratio, None if move is None or ratio is None else finite(move * ratio)


def zero_baseline_rows(
    rows: Sequence[Row], arm: ZeroBaselineArm, absolute_floor: float | None = None
) -> tuple[Row, ...]:
    """Apply the relative floor to the active zero-baseline tier, or exclude it for the paired arm.

    The lead's definition, 2026-11-11: the floor is the median dollar volume of the names with an
    established baseline, a finite ratio and a move passing `MOVE_CONDITION`, whatever their other
    filters (code review of the definitions, finding 6: a big mover over the order cap still measures
    what a mover trades); with none, the tier ranks by dollars alone. Every row's dollars are of the
    generation's one span (`Row.ratio_span`), to each symbol's data clock, so the median, the tier and
    the absolute floor compare the same minutes: from `DAY_START` once a generation without an
    overnight baseline is past it, else from `PART_START` the evening before (code review of the span,
    findings 2, 6 and 7).
    """
    qualifying = [
        r.dollar_volume
        for r in rows
        if r.established is True
        and r.ratio is not None
        and math.isfinite(r.ratio)
        and r.move is not None
        and passes_move_condition(r.move)
        and r.dollar_volume is not None
    ]
    relative_floor = statistics.median(qualifying) if qualifying else None
    result = []
    for r in rows:
        reason = None
        if r.ratio == math.inf:  # the tier: no established (or a zero) baseline
            # an active zero-baseline ratio requires positive dollars
            assert r.dollar_volume is not None
            if absolute_floor is not None and r.dollar_volume <= absolute_floor:
                reason = "zero_baseline:absolute_floor"  # round 3, finding 5: named, not unknown
            elif arm == ZeroBaselineArm.EXCLUDE:
                reason = "zero_baseline:excluded_arm"
            elif relative_floor is not None and r.dollar_volume < relative_floor:
                reason = "zero_baseline:relative_floor"
        result.append(r if reason is None else replace(r, excluded_by=(*r.excluded_by, reason)))
    return tuple(result)


def passes_move_condition(move: float) -> bool:
    return move >= MOVE_CONDITION or math.isclose(move, MOVE_CONDITION)


def tradable_from(fields: Mapping[str, object] | None) -> bool | None:
    if fields is None:
        return None
    tradable, status = fields.get("tradable"), fields.get("status")
    if not isinstance(tradable, bool) or not isinstance(status, str):
        return None
    return tradable and status == AssetStatus.ACTIVE.value


def rank(
    *,
    session: date,
    tallies: Mapping[str, Tally],
    assets: Mapping[str, Mapping[str, object]],
    baselines: Mapping[str, Baseline],
    references: Mapping[str, Reference],
    opens: Mapping[str, float],
    statuses: Mapping[str, MarketStatusAction],
    splits: SplitRead | None,
    security_types: Mapping[str, SecurityType],
    order_cap: float,
    computed_ns: int,
    max_data_age_secs: float = MAX_DATA_AGE_SECS,
    zero_baseline_dollar_floor: float | None = None,
    zero_baseline_arm: ZeroBaselineArm | str = ZeroBaselineArm.RELATIVE_FLOOR,
    overnight_baseline: bool = False,
    no_baseline: bool = False,
) -> Generation:
    """One generation over the universe, with each symbol's baseline and open gate at its own data time.

    Everything is by symbol, and a symbol absent from a mapping is unknown there. `assets` holds the fields
    of `Equity.info`; `opens` today's regular open; `order_cap` the most one order may spend;
    `computed_ns` the generation's compute time; `statuses` each name's status folded with `status_after`.
    `max_data_age_secs` bounds each name's bar age at that time, inclusive. A stale name stays a row
    with its last bar's timestamp, but its current price and derived figures are unknown and it cannot
    enter the projection. A fresh peer never refreshes its price.
    `zero_baseline_dollar_floor` has no numeric default; None means the absolute floor is not applied.
    A configured floor must be finite and nonnegative. `zero_baseline_arm` selects the lead's relative
    floor rule (default) or its paired excluded arm; neither changes the ordinary finite-ratio rows.

    `InvalidGeneration` for what would look ahead or mislabel: a tally of another session or instrument, a
    reference close of the session or later (unknown through the trade date, and it would empty the split
    window), or a bar ending or initialized after `computed_ns`. An open its own symbol's bars could not
    yet have known is unknown on that row and never used.

    `overnight_baseline` is `BaselineRead.overnight`: true only when the baselines hold the overnight
    feed, and then the caller's tallies must hold it too, or the night's minutes are counted on one side
    only (code review of the span, finding 3: the caller's duty, not checked here).

    `no_baseline` is the lead's ruling of 2026-11-11 (21:49) for a part with no baseline file: every
    name is not established and ranks in the zero-baseline tier on its dollars, under the move
    condition. It goes with empty `baselines`; without it a name with no baseline is a hole.
    """
    if no_baseline and baselines:
        raise InvalidGeneration(
            "no_baseline ranks a part that has no baseline file, so it takes no baselines"
        )
    if type(computed_ns) is not int or computed_ns < 0:
        raise InvalidGeneration("compute time must be a nonnegative integer nanosecond clock")
    if not is_finite_number(order_cap) or order_cap <= 0:
        raise InvalidGeneration(f"an order cap of {order_cap} is not an amount one order may spend")
    if not math.isfinite(max_data_age_secs) or max_data_age_secs <= 0:
        raise InvalidGeneration(
            f"maximum data age must be finite and positive, got {max_data_age_secs!r}"
        )
    oldest_bar_ns = computed_ns - int(max_data_age_secs * 1_000_000_000)
    check_zero_baseline_floor(zero_baseline_dollar_floor)
    try:
        arm = ZeroBaselineArm(zero_baseline_arm)
    except ValueError as e:
        raise InvalidGeneration(f"unknown zero_baseline_arm: {zero_baseline_arm!r}") from e
    for symbol, t in tallies.items():
        if t.session != session:
            raise InvalidGeneration(
                f"{symbol}'s tally is of the session {t.session}, not {session}"
            )
        if t.instrument_id is not None and t.instrument_id.symbol.value != symbol:
            raise InvalidGeneration(f"{symbol}'s tally holds bars of {t.instrument_id}")
    late = sorted(s for s, r in references.items() if r.session >= session)
    if late:
        raise InvalidGeneration(
            f"a reference close of {session} or later, for {late[:5]}: the reference is a session before"
        )
    published = max(
        (t.last_bar_ns for t in tallies.values() if t.last_bar_ns is not None), default=None
    )
    if published is not None and published > computed_ns:
        raise InvalidGeneration(
            f"a bar ends at {ny_time(published)}, after the compute time {ny_time(computed_ns)}"
        )
    received = max(
        (t.last_received_ns for t in tallies.values() if t.last_received_ns is not None),
        default=None,
    )
    if received is not None and received > computed_ns:
        raise InvalidGeneration(
            f"a bar initialization at {ny_time(received)}, after the compute time {ny_time(computed_ns)}"
        )
    # an open the symbol's own bars could not yet have known is unknown on its row and never used; one
    # lagging name does not void the generation (code review, round 3, finding 3)
    regular_open = ny_ns(session, REGULAR_OPEN)  # by instant: an evening clock is before it
    early_opens = {
        symbol
        for symbol in opens
        if (t := tallies.get(symbol)) is None
        or t.last_bar_ns is None
        or t.last_bar_ns <= regular_open
    }
    # the lead's rule (2026-11-11): the numerator counts exactly the clock minutes its baseline covers.
    # The span is the generation's, by its data time, so every row, the tier and its median count the
    # same minutes (code review of the span, finding 2); past 04:00 exactly, not at it (finding 5)
    from_day = (
        not overnight_baseline and published is not None and published > ny_ns(session, DAY_START)
    )
    span = None if published is None else f"{DAY_START if from_day else PART_START:%H:%M}-now"
    rows = []
    for symbol in sorted(assets.keys() | tallies.keys()):
        try:
            instrument_id = InstrumentId(Symbol(symbol), VENUE)
        except ValueError as e:
            raise InvalidGeneration(f"{symbol!r} is not a symbol: {e}") from e
        try:  # what the signal's codec refuses (`RankEntry`) never reaches `top_entries`
            RankEntry(instrument_id, 0.0)
            encodable = True
        except ValueError:
            encodable = False
        t = tallies.get(symbol)
        stale = t is not None and t.last_bar_ns is not None and t.last_bar_ns < oldest_bar_ns
        last = None if t is None or stale else t.last
        ref = references.get(symbol)
        ref_close = (
            ref.close if ref is not None and is_finite_number(ref.close) and ref.close > 0 else None
        )
        move = None if last is None or ref_close is None else finite(last / ref_close - 1.0)
        open_ = None if symbol in early_opens else opens.get(symbol)
        move_from_open = (
            None
            if last is None or open_ is None or not is_finite_number(open_) or open_ <= 0
            else finite(last / open_ - 1.0)
        )

        b = baselines.get(symbol)
        clock = tally_clock(t)
        dvol = (
            None
            if t is None or last is None
            else t.dollars_from_day
            if from_day
            else t.dollar_volume
        )
        base = None if b is None or clock is None else baseline_at(b, clock)
        if from_day and b is not None and base is not None:
            before_day = baseline_at(b, DAY_START)
            assert before_day is not None  # b has sessions, since base is known
            # a name last seen by 04:00 has no day minutes on either side
            base = max(0.0, base - before_day)
        established = None if b is None or clock is None else b.established(clock)
        if no_baseline and clock is not None:
            base, established = 0.0, False
        ratio, score = volume_score(
            move, dvol, base, zero_baseline_dollar_floor, established=established
        )

        tradable = tradable_from(assets.get(symbol))
        action = statuses.get(symbol)
        status = None if action is None else status_after(None, action)
        halted = None if status is None else status != MarketStatusAction.TRADING
        kind = security_types.get(symbol)
        split = None if ref is None else split_since(splits, symbol, ref.session, session)
        fits = None if last is None else last <= order_cap

        excluded: list[str] = [] if encodable else ["symbol:unencodable"]
        unchecked: tuple[str, ...] = ()
        if stale:
            excluded.append("last_bar:stale")
        check_known("reference", move, excluded)
        if move is not None and not passes_move_condition(move):
            excluded.append("move_condition")
        if b is not None and clock is not None and established is None:
            excluded.append("established:unknown")
        check_known("ratio", ratio, excluded)
        check_known("score", score, excluded)
        check("tradable", None if tradable is None else not tradable, excluded)
        if halted is None and HALT_UNKNOWN_ELIGIBLE:
            unchecked = ("halt",)
        else:
            check("halt", halted, excluded)
        check("security_type", None if kind is None else kind != SecurityType.SHARE, excluded)
        check("corporate_action", split, excluded)
        check("order_cap_at_last", None if fits is None else not fits, excluded)

        rows.append(
            Row(
                instrument_id=instrument_id,
                last=last,
                last_bar_ns=None if t is None else t.last_bar_ns,
                last_received_ns=None if t is None else t.last_received_ns,
                minutes=0 if t is None else t.minutes,
                reference=ref_close,
                move=move,
                move_from_open=move_from_open,
                dollar_volume=dvol,
                dollars_before_day=None if t is None or last is None else t.dollars_before_day,
                ratio_span=None if clock is None else span,
                baseline=base,
                baseline_name="none: no baseline" if b is None else b.name,
                established=established,
                ratio=ratio,
                score=score,
                acceleration=None,
                tradable=tradable,
                halted=halted,
                security_type=kind,
                split_since_reference=split,
                fits_order_cap_at_last=fits,
                excluded_by=tuple(excluded),
                unchecked=unchecked,
            )
        )
    return Generation(
        session=session,
        rows=zero_baseline_rows(rows, arm, zero_baseline_dollar_floor),
        published_ns=published,
        generated_ns=computed_ns,
        received_ns=received,
    )


def top_entries(generation: Generation, n: int = MAX_ENTRIES) -> list[RankEntry]:
    """The best `n` eligible names with a positive score, best first, ties by instrument id."""
    if n < 0:
        raise ValueError(f"top_entries takes n >= 0, not {n}")
    if n > MAX_ENTRIES:
        raise ValueError(f"a ranking holds at most {MAX_ENTRIES} entries, not {n}")
    scored = [
        (
            -r.score,
            -r.dollar_volume if r.ratio == math.inf and r.dollar_volume is not None else 0.0,
            str(r.instrument_id),
            r,
        )
        for r in generation.rows
        if r.eligible and r.score is not None and r.score > 0
    ]
    entries = []
    for _, _, _, r in sorted(scored)[:n]:
        # rank's eligibility invariant; RankEntry enforces the bus contract
        assert r.move is not None
        entries.append(RankEntry(r.instrument_id, r.move))
    return entries
