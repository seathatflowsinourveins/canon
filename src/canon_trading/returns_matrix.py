"""The returns matrix: one column per configuration, T common rows of daily portfolio returns.

Contract: ADR 0010, protocol step 2. Accept a trial's `returns_series` only as the daily portfolio series
with one key per UTC day, reject any other shape, and drop the days the venue was closed. Every trial
runs over its study's sample and must have the same session days on it (ADR 0041, which supersedes
step 2's trim to a common range): the matrix is refused, naming every trial that lacks a day another
has, and never trimmed.

The shape accepted is NautilusTrader's daily series (v2.0.0rc8, `crates/analysis/src/analyzer.rs` at the
pinned commit, `calculate_daily_returns`): a return for every UTC day from the first to the last, keyed
at the day's midnight, the balance carried forward over days with no event. Its fallback, per-position
returns when fewer than two days have balances, is keyed at position close times. So keys must be
consecutive UTC midnights; anything else is that fallback or a gap, and is rejected.

A closed day is a UTC day on which no bar of the trial's run became available (its `session_days`, ADR
0035 term 3). Nothing can fill or reprice on such a day, so its return must be 0, and one that is not
is refused rather than dropped. A session day with no return in the series is refused as well, never
dropped: the series has a key for every UTC day from the first to the last, and a session day it lacks
is a day of the run the series does not describe. A return that is not finite is refused too: ADR 0031
requires every return of the pick to be finite, and skfolio's `Portfolio` does not raise on one, it
turns a NaN into 0.0 (observed at 1.12.8 and 1.12.9), so `returns_matrix` refuses one in any series
it is given as well.

Which upstream call does what:

* `pandas.to_datetime(..., unit="ns", utc=True)` and `pandas.Series`: each trial's session returns,
  indexed by UTC date. Closed days are dropped before the series is built, by keeping only session days.
* skfolio 1.12.9 (`portfolio/_portfolio.py`, `population/_population.py`): each series becomes a
  one-asset `Portfolio` with weight 1, whose returns are the series itself and whose dates come from
  the frame's index (`Series.to_frame`); `Population.returns_df` joins them on their dates, NaN where a
  trial has no row.
* `DataFrame.isna`: a day one trial has and another lacks. The rows are the union of the trials' session
  days, so a trial with a NaN did not cover the whole sample, and the matrix is refused.

No statistic is computed here.
"""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from skfolio import Population
from skfolio import Portfolio

if TYPE_CHECKING:
    from collections.abc import Sequence


DAY_NS = 86_400 * 1_000_000_000


class ReturnsRejected(ValueError):
    """Returns that ADR 0010 step 2 or ADR 0041 does not accept; the message names every trial.

    `names` holds the name of each series rejected, in the order the message gives them.
    """

    def __init__(self, message: str, names: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.names = tuple(names)


def daily_series(name: str, returns: object, session_days: object) -> pd.Series:
    """One trial's daily portfolio returns on its session days, indexed by UTC date, named `name`.

    `returns` is a ledger entry's `returns_series` (nanoseconds as text to a return) and `session_days`
    its `session_days`, in the shape `canon_trading.ledger.record_trial` writes them: each key and day
    the decimal text of a whole number, as `str(int)` gives it, and each return a number. Anything
    else came from another writer and is refused, naming `name`.
    """
    returns, session_days = _check_shape(name, returns, session_days)
    if not returns:
        raise ReturnsRejected(f"{name}: returns_series is empty")
    keys = sorted(int(key) for key in returns)
    off_midnight = [key for key in keys if key % DAY_NS]
    if off_midnight:
        raise ReturnsRejected(
            f"{name}: returns_series has keys off a UTC midnight, the first {off_midnight[0]}; that "
            "is the per-position fallback, not the daily portfolio series",
        )
    gaps = [(a, b) for a, b in itertools.pairwise(keys) if b - a != DAY_NS]
    if gaps:
        raise ReturnsRejected(
            f"{name}: returns_series skips from {gaps[0][0]} to {gaps[0][1]}; the daily portfolio "
            "series has one key for every UTC day",
        )
    not_finite = [key for key in keys if not math.isfinite(returns[str(key)])]
    if not_finite:
        day = not_finite[0]
        raise ReturnsRejected(
            f"{name}: return {returns[str(day)]} on "
            f"{pd.to_datetime(day, unit='ns', utc=True).date()} is not finite",
        )
    open_days = {int(day) for day in session_days}
    moved_while_closed = [key for key in keys if key not in open_days and returns[str(key)] != 0.0]
    if moved_while_closed:
        day = moved_while_closed[0]
        raise ReturnsRejected(
            f"{name}: return {returns[str(day)]} on {pd.to_datetime(day, unit='ns', utc=True).date()}, "
            "a day with no bar of the run",
        )
    unreturned = sorted(open_days.difference(keys))
    if unreturned:
        raise ReturnsRejected(
            f"{name}: no return on {len(unreturned)} of its {len(open_days)} session days, the "
            f"first {pd.to_datetime(unreturned[0], unit='ns', utc=True).date()}; the daily "
            "portfolio series has a return for every day of the run",
        )
    kept = [key for key in keys if key in open_days]
    if not kept:
        raise ReturnsRejected(f"{name}: no return falls on a session day")
    return pd.Series(
        [float(returns[str(key)]) for key in kept],
        index=pd.to_datetime(kept, unit="ns", utc=True),
        name=name,
    )


def _portfolio(series: pd.Series) -> Portfolio:
    """A one-asset skfolio `Portfolio` of `series` with weight 1, on its UTC dates without a zone.

    The dates are the same instants; skfolio builds a portfolio on a zone-aware index about seven
    times slower (2,500 rows, measured at 1.12.8).
    """
    plain = series.tz_convert(None) if getattr(series.index, "tz", None) is not None else series
    return Portfolio(X=plain.to_frame(), weights=[1.0], name=str(series.name))


def session_sharpe(series: pd.Series) -> float:
    """skfolio's per-session Sharpe ratio of one trial's series from `daily_series`."""
    return float(_portfolio(series).sharpe_ratio)


NS_RANGE = range(2**63)
"""Nanoseconds both NautilusTrader and pandas hold: `UnixNanos` wraps a `u64` (`crates/core/src/nanos.rs`
at the pinned commit), and a pandas `Timestamp` holds at most 2**63 - 1 (-2**63 is `NaT`)."""

_MISSING = object()


def is_ns_text(value: object) -> bool:
    """Whether `value` is a whole number of nanoseconds as `record_trial` writes it, `str(int)`.

    The number is a timestamp, so it must lie in `NS_RANGE`. The length is checked before `int`
    reads the text: Python refuses to convert a text of more than 4,300 digits, with `ValueError`.
    """
    return (
        isinstance(value, str)
        and value.isascii()
        and 0 < len(value) <= 19
        and value.isdecimal()
        and str(int(value)) == value
        and int(value) in NS_RANGE
    )


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _check_shape(
    name: str, returns: object, session_days: object
) -> tuple[dict[str, float], list[str]]:
    """Refuse `returns` and `session_days` unless they are in the shape `record_trial` writes."""
    if not isinstance(returns, dict):
        raise ReturnsRejected(f"{name}: returns_series is {type(returns).__name__}, not a mapping")
    if not isinstance(session_days, list):
        raise ReturnsRejected(f"{name}: session_days is {type(session_days).__name__}, not a list")
    bad_key = next((key for key in returns if not is_ns_text(key)), _MISSING)
    if bad_key is not _MISSING:
        raise ReturnsRejected(f"{name}: returns_series key {bad_key!r} is not nanoseconds as text")
    bad = next((key for key, value in returns.items() if not _is_number(value)), _MISSING)
    if bad is not _MISSING:
        raise ReturnsRejected(
            f"{name}: returns_series value {returns[bad]!r} on {bad} is not a number"
        )
    # A sentinel no JSON value can be: a JSON null in the list is a day this refuses.
    bad_day = next((day for day in session_days if not is_ns_text(day)), _MISSING)
    if bad_day is not _MISSING:
        raise ReturnsRejected(f"{name}: session day {bad_day!r} is not nanoseconds as text")
    return returns, session_days


def returns_matrix(series: Sequence[pd.Series]) -> pd.DataFrame:
    """Join `series` on their dates through skfolio; refuse them unless every one has every date.

    The T rows are the session days of the sample (ADR 0041). Each series that lacks one of them is
    named, with how many it lacks and its first and last day, so one stop lists every such trial.
    """
    names = [s.name for s in series]
    # skfolio names each column by `str` of the name, so names distinct only by type collide.
    if not series or len({str(name) for name in names}) != len(names):
        raise ValueError("returns_matrix takes one or more series with distinct names")
    not_finite = [str(s.name) for s in series if not np.isfinite(s.to_numpy(dtype=float)).all()]
    if not_finite:
        raise ReturnsRejected(
            "\n".join(f"{name}: a return that is not finite" for name in not_finite),
            names=not_finite,
        )
    population = Population([_portfolio(s) for s in series])
    matrix = population.returns_df(use_tag_in_column_name=False)
    # The UTC dates `daily_series` gave, their zone restored after skfolio's zone-free build.
    matrix.index = pd.DatetimeIndex(matrix.index).tz_localize("UTC")
    missing = matrix.isna()
    short = [name for name in matrix.columns if missing[name].any()]
    if short:
        by_name = {s.name: s for s in series}
        lines = [
            f"{name}: no return on {int(missing[name].sum())} of the {len(matrix)} session days of "
            f"the sample, the first {missing[name].idxmax().date()}; it covers "
            f"{by_name[name].index[0].date()} to {by_name[name].index[-1].date()}"
            for name in short
        ]
        raise ReturnsRejected(
            "every trial must cover the same session days over the sample (ADR 0041):\n"
            + "\n".join(lines),
            names=short,
        )
    return matrix
