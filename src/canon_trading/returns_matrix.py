"""The returns matrix: one column per configuration, T common rows of daily portfolio returns.

Contract: ADR 0010, protocol step 2. Accept a trial's `returns_series` only as the daily portfolio series
with one key per UTC day, reject any other shape, drop the days the venue was closed, and trim every
variant to a common range of T rows.

The shape accepted is NautilusTrader's daily series (v2.0.0rc8, `crates/analysis/src/analyzer.rs` at the
pinned commit, `calculate_daily_returns`): a return for every UTC day from the first to the last, keyed
at the day's midnight, the balance carried forward over days with no event. Its fallback, per-position
returns when fewer than two days have balances, is keyed at position close times. So keys must be
consecutive UTC midnights; anything else is that fallback or a gap, and is rejected.

A closed day is a UTC day on which no bar of the trial's run became available (its `session_days`, ADR
0035 term 3). Nothing can fill or reprice on such a day, so its return must be 0, and one that is not
is refused rather than dropped. A return that is not finite is refused too: ADR 0031 requires every
return of the pick to be finite, and skfolio's `Portfolio` raises on one.

Which upstream call does what:

* `pandas.to_datetime(..., unit="ns", utc=True)` and `pandas.Series`: each trial's session returns,
  indexed by UTC date. Closed days are dropped before the series is built, by keeping only session days.
* skfolio 1.12.8 (`portfolio/_portfolio.py`, `population/_population.py`): each series becomes a
  one-asset `Portfolio` with weight 1, whose returns are the series itself and whose dates come from
  the frame's index (`Series.to_frame`); `Population.returns_df` joins them on their dates, NaN where a
  trial has no row.
* `DataFrame.loc[first:last]`: the common range, from the latest first day to the earliest last day.
  `DataFrame.isna` finds a day one trial has and another lacks inside it; their calendars differ, and the
  matrix is refused rather than trimmed further.

No statistic is computed here.
"""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

import pandas as pd
from skfolio import Population
from skfolio import Portfolio

from .ledger import DAY_NS

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence


class ReturnsRejected(ValueError):
    """Returns that ADR 0010 step 2 does not accept; the message names the trial."""


def daily_series(name: str, returns: Mapping[str, float], session_days: Sequence[str]) -> pd.Series:
    """One trial's daily portfolio returns on its session days, indexed by UTC date, named `name`.

    `returns` is a ledger entry's `returns_series` (nanoseconds as text to a return) and `session_days`
    its `session_days`.
    """
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
    kept = [key for key in keys if key in open_days]
    if not kept:
        raise ReturnsRejected(f"{name}: no return falls on a session day")
    return pd.Series(
        [float(returns[str(key)]) for key in kept],
        index=pd.to_datetime(kept, unit="ns", utc=True),
        name=name,
    )


def returns_matrix(series: Sequence[pd.Series]) -> pd.DataFrame:
    """Join `series` on their dates through skfolio and trim them to their common range of T rows."""
    names = [s.name for s in series]
    if not series or len(set(names)) != len(names):
        raise ValueError("returns_matrix takes one or more series with distinct names")
    population = Population(
        [Portfolio(X=s.to_frame(), weights=[1.0], name=str(s.name)) for s in series],
    )
    joined = population.returns_df(use_tag_in_column_name=False)
    first = max(s.index[0] for s in series)
    last = min(s.index[-1] for s in series)
    if first > last:
        raise ReturnsRejected(
            f"the trials have no day in common: the latest first day is {first.date()} and the "
            f"earliest last day {last.date()}",
        )
    matrix = joined.loc[first:last]
    missing = matrix.isna()
    if missing.to_numpy().any():
        column = missing.any().idxmax()
        day = missing[column].idxmax()
        raise ReturnsRejected(
            f"{column}: no return on {day.date()}, a session day of another trial in the common "
            "range; their calendars differ",
        )
    return matrix
