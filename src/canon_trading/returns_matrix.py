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

Every return of a ledger record is read once, where it enters the gate (`session_returns`): it must
be a float, as `record_trial` writes each one (NautilusTrader's `f64`, a `float` through JSON), never
a `bool` or an `int`, which Python's `==` reads as equal to 0.0 or 1.0, and finite. `returns_matrix`
keeps its own refusal of a return that is not finite, for a series given to it directly.

**The numeric check** (ADR 0080, 2026-11-11; ADR 0073 rule 4, the family found in rounds 5, 6 and
7) is one function, `_numerically_valid`, with one call site: `session_returns`, on the series of
session days alone, after closed days are dropped, which is the series every gate calculation takes.
Taken over every UTC day, the zeros of closed days moved the mean and hid moments that overflow or
underflow on the session days (round 7). It computes what the gate consumes by the functions the
gate computes with, skfolio 1.12.9's, read in the installed `measures/_measures.py` and
`portfolio/_base.py`, and returns the series with its Sharpe ratio, so `canon_trading.ranking` and
`canon_trading.ledger.told_sharpe` read every statistic of a series from a result that passed. In
order:

1. *No spread at its own size.* One measure, `rounding_of_the_mean`: n returns no larger than m in
   size have a mean that rounding moves by less than n times float64's spacing at m
   (`math.ulp(m)`): NumPy's mean is n - 1 additions and a division, each rounding by at most half
   the spacing at its result, and no partial sum exceeds n times m. A series of n equal returns has
   that one error as every deviation, so skfolio's sample standard deviation of it
   (`measures.standard_deviation`, NumPy's `nanvar` with `ddof=1`) is the error times
   sqrt(n / (n - 1)), at most sqrt(2) of it, where exact arithmetic gives 0: thirty returns of -0.01
   read 1.8e-18 and a Sharpe ratio of -5.67e15 (run at 1.12.9, 2026-11-11). So a series whose
   standard deviation is no more than `DEGENERATE` = 2 times the measure, the least whole number
   above sqrt(2), cannot be told from a constant one and is refused by name. Exact constancy is its
   special case, a series of zeros included, and is read from the returns themselves before any
   statistic: thirty returns of 1e200 leave an error whose square overflows, and skfolio's variance
   of them is inf (run at 1.12.9, 2026-11-11).
   skfolio's own skewness and kurtosis follow the same idea with a lower floor (NaN when the second
   moment is at most `(eps * mean)**2`, `_standardized_moment`); every series this accepts is above
   that floor. A choice, stated as one: the factor 2 separates rounding from spread, it does not
   make a series just above it meaningful. Such a series has the very large Sharpe ratio its returns
   give (up to about 2**52 / n) and a bound, below, above that ratio, so the ranking stops on it;
   one further from the threshold ranks as it is (ADR 0080's limits).
2. *Moments that underflow.* The square of the second central moment (`measures.variance(biased=
   True)`), which the kurtosis divides the fourth by, must be at or above float64's smallest normal
   (`sys.float_info.min`): below it IEEE 754 keeps absolute, not relative, precision, and with
   returns about 1e-81 apart the kurtosis rounds toward 1 or 0 and the DSR of a pick rejected at its
   own scale passes (run at 1.12.9, 2026-11-11). The fourth moment is never below that square, the
   3/2 power the skewness divides by is above it, and two such standard deviations multiply to a
   normal number, so this one floor covers them. It is read before item 3 so that the cause named
   is the underflow, where the skewness and the kurtosis it leaves are NaN; and returns that differ
   with a standard deviation of exactly 0.0, about 1e-170 apart, are refused here too, not by
   item 1, since it is their squares that vanished.
3. *Not finite.* The Sharpe ratio (`session_sharpe`), the variance, the fourth central moment, the
   skewness, the kurtosis and the series' correlation with itself, by `measures.correlation`, whose
   inputs are NumPy's covariance of the column and the product of two standard deviations: each must
   be finite. Returns of about 1e77 overflow the fourth moment, and of 1.34e154 the variance, where
   skfolio's Sharpe ratio reads 0.0 rather than failing (run at 1.12.9, 2026-11-11).

The same measure gives the one bound the gate compares Sharpe ratios by
(`SessionReturns.rounding`): two computations of one series' ratio, summing in any order, have means
less than twice the measure apart and standard deviations whose squares differ by less than twice
its square and the rounding of n squares. With r the measure over the standard deviation, under 1/2
for a series that passed, that is less than 13 r max(1, |ratio|) to first order (the largest seen
over reordered sums is 0.56 of r max(1, |ratio|), run 2026-11-11), and `RECOMPUTED` = 16 is the next
power of two. A ratio is known no more finely than that, so the gate holds a value told to within
it of the one computed again, and reads two trials' ratios as told apart only when they differ by
more than the sum of their two bounds (`canon_trading.ranking`; ADR 0080, 2026-11-11). It is about
2e-13 for thirty daily returns of a per cent and 2e-11 to 3e-11 for the step-7 records of 1,366
sessions (run 2026-11-11), and wide only next to the threshold of item 1, where it exceeds the
ratio itself.

Which upstream call does what:

* `pandas.to_datetime(..., unit="ns", utc=True)` and `pandas.Series`: each trial's session returns,
  indexed by UTC date. Closed days are dropped before the series is built, by keeping only session days.
* skfolio 1.12.9 (`portfolio/_portfolio.py`, `population/_population.py`): each series becomes a
  one-asset `Portfolio` with weight 1, whose returns are the series itself and whose dates come from
  the frame's index (`Series.to_frame`); `Population.returns_df` joins them on their dates, NaN where a
  trial has no row.
* `DataFrame.isna`: a day one trial has and another lacks. The rows are the union of the trials' session
  days, so a trial with a NaN did not cover the whole sample, and the matrix is refused.

The only statistic kept is each series' Sharpe ratio, skfolio's; the others the check takes only
refuse a series.
"""

from __future__ import annotations

import itertools
import math
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from skfolio import Population
from skfolio import Portfolio
from skfolio import measures

if TYPE_CHECKING:
    from collections.abc import Sequence


DAY_NS = 86_400 * 1_000_000_000


class ReturnsRejected(ValueError):
    """Returns that ADR 0010 step 2 or ADR 0041 does not accept; the message names every trial.

    `names` holds the name of each series rejected, in the order the message gives them.
    `no_spread` says the one series was refused for not varying beyond rounding (the numeric
    check's first item), which is what a trial that never traded leaves.
    """

    def __init__(self, message: str, names: Sequence[str] = (), *, no_spread: bool = False) -> None:
        super().__init__(message)
        self.names = tuple(names)
        self.no_spread = no_spread


DEGENERATE = 2
"""A series whose standard deviation is no more than this many times `rounding_of_the_mean` has no
spread at its own size: the least whole number above sqrt(2), the most a constant series' deviation
can be of its mean's rounding (module header)."""

RECOMPUTED = 16
"""How many times `rounding_of_the_mean` over the standard deviation two computations of one series'
Sharpe ratio can differ by, relative to the larger of 1 and the ratio (module header)."""


@dataclass(frozen=True)
class SessionReturns:
    """One trial's returns on its session days, having passed the numeric check, with what the gate
    reads of them.

    `series` is indexed by UTC date and named for the trial; `sharpe` is skfolio's per-session
    Sharpe ratio of it; `rounding` is how far a second computation of that ratio from the same
    returns can be from it (module header, `RECOMPUTED`), the one bound the gate compares ratios by.
    """

    series: pd.Series
    sharpe: float
    rounding: float


def session_returns(name: str, returns: object, session_days: object) -> SessionReturns:
    """One trial's daily portfolio returns on its session days, named `name`, with its Sharpe ratio.

    `returns` is a ledger entry's `returns_series` (nanoseconds as text to a return) and `session_days`
    its `session_days`, in the shape `canon_trading.ledger.record_trial` writes them: each key and day
    the decimal text of a whole number, as `str(int)` gives it, each session day once and in order,
    and each return a number. Anything
    else came from another writer and is refused, naming `name`; so is a series the numeric check
    refuses (module header). Every series the gate computes on comes from here.
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
    by_day = _finite_floats(name, returns, keys)
    open_days = {int(day) for day in session_days}
    moved_while_closed = [key for key in keys if key not in open_days and by_day[key] != 0.0]
    if moved_while_closed:
        day = moved_while_closed[0]
        raise ReturnsRejected(
            f"{name}: return {by_day[day]} on {_date(day)}, a day with no bar of the run",
        )
    unreturned = sorted(open_days.difference(keys))
    if unreturned:
        raise ReturnsRejected(
            f"{name}: no return on {len(unreturned)} of its {len(open_days)} session days, the "
            f"first {_date(unreturned[0])}; the daily "
            "portfolio series has a return for every day of the run",
        )
    kept = [key for key in keys if key in open_days]
    if not kept:
        raise ReturnsRejected(f"{name}: no return falls on a session day")
    return _numerically_valid(
        pd.Series(
            [by_day[key] for key in kept],
            index=pd.to_datetime(kept, unit="ns", utc=True),
            name=name,
        )
    )


def daily_series(name: str, returns: object, session_days: object) -> pd.Series:
    """The series of `session_returns`, checked as there, for a caller that reads no statistic
    of it."""
    return session_returns(name, returns, session_days).series


def rounding_of_the_mean(values: np.ndarray) -> float:
    """The gate's one measure of rounding: less than this separates the mean NumPy takes of
    `values` from their exact mean, the count times float64's spacing at the largest of them in
    size (module header, item 1)."""
    return len(values) * math.ulp(float(np.abs(values).max()))


def _numerically_valid(series: pd.Series) -> SessionReturns:
    """`series`, one trial's returns on its session days, with its Sharpe ratio, or `ReturnsRejected`
    naming it: the gate's one numeric check (module header), called from `session_returns` alone."""
    name = series.name
    values = series.to_numpy(dtype=float)
    count = len(values)
    if count < 2:  # skfolio's sample variance needs two returns
        raise ReturnsRejected(
            f"{name}: has {count} session day, and a Sharpe ratio needs two",
        )
    largest = float(np.abs(values).max())
    if values.max() == values.min():
        # Before any statistic: the square of what rounding left of a large constant overflows.
        raise ReturnsRejected(
            f"{name}: its returns do not vary beyond rounding at their own size: each of the "
            f"{count} is {float(values[0])}, so they give no Sharpe ratio (ADR 0031)",
            no_spread=True,
        )
    with np.errstate(all="ignore"):
        deviation = float(measures.standard_deviation(values))
        read = {
            "Sharpe ratio": session_sharpe(series),
            "variance": float(measures.variance(values)),
            "fourth central moment": float(measures.fourth_central_moment(values)),
            "skewness": float(measures.skew(values)),
            "kurtosis": float(measures.kurtosis(values)),
            "correlation with itself": float(
                measures.correlation(np.column_stack([values, values]))[0, 1]
            ),
        }
        # The second central moment, whose square skfolio's kurtosis divides the fourth by.
        second = float(measures.variance(values, biased=True))
    rounding = rounding_of_the_mean(values)
    # A standard deviation of exactly 0.0 from returns that differ is their squares vanishing,
    # which the next check names.
    if 0.0 < deviation <= DEGENERATE * rounding:
        raise ReturnsRejected(
            f"{name}: its returns do not vary beyond rounding at their own size: their standard "
            f"deviation is {deviation}, no more than {DEGENERATE * rounding}, {DEGENERATE} times "
            f"what rounding the mean of {count} returns as large as {largest} "
            "can leave of a constant series, so they give no Sharpe ratio (ADR 0031)",
            no_spread=True,
        )
    if second * second < sys.float_info.min:
        raise ReturnsRejected(
            f"{name}: the square of the second central moment of its returns on its session days "
            f"is {second * second}, below float64's smallest normal, {sys.float_info.min}, so the "
            "DSR's kurtosis underflows (ADR 0031)",
        )
    not_finite = [f"{what} is {value}" for what, value in read.items() if not math.isfinite(value)]
    if not_finite:
        raise ReturnsRejected(
            f"{name}: of its returns on its session days, skfolio's {', its '.join(not_finite)}: "
            "not finite, so the gate cannot compute with them (ADR 0031)",
        )
    sharpe = read["Sharpe ratio"]
    return SessionReturns(series, sharpe, RECOMPUTED * rounding / deviation * max(1.0, abs(sharpe)))


def _portfolio(series: pd.Series) -> Portfolio:
    """A one-asset skfolio `Portfolio` of `series` with weight 1, on its UTC dates without a zone.

    The dates are the same instants; skfolio builds a portfolio on a zone-aware index about seven
    times slower (2,500 rows, measured at 1.12.8).
    """
    plain = series.tz_convert(None) if getattr(series.index, "tz", None) is not None else series
    return Portfolio(X=plain.to_frame(), weights=[1.0], name=str(series.name))


def session_sharpe(series: pd.Series) -> float:
    """skfolio's per-session Sharpe ratio of one trial's series: `Portfolio.sharpe_ratio`, the mean
    over the sample standard deviation. The gate reads it from `session_returns`' result."""
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


def _finite_floats(name: str, returns: dict[str, object], keys: list[int]) -> dict[int, float]:
    """Each of `returns` by its day, keyed at `keys` in order; refused unless each is a finite float,
    closed days' included. What the series of session days amounts to is `_numerically_valid`'s."""
    checked: dict[int, float] = {}
    for key in keys:
        value = returns[str(key)]
        # A float, as `record_trial` writes each return; `bool` is an `int`, never a `float`.
        if not isinstance(value, float):
            raise ReturnsRejected(
                f"{name}: returns_series value {value!r} on {key} is not a float, as record_trial "
                "writes each return"
            )
        if not math.isfinite(value):
            raise ReturnsRejected(
                f"{name}: return {value} on {_date(key)} is not finite",
            )
        checked[key] = value
    return checked


def _check_shape(
    name: str, returns: object, session_days: object
) -> tuple[dict[str, object], list[str]]:
    """Refuse `returns` and `session_days` unless they are in the shape `record_trial` writes; each
    return is `_finite_floats`'s."""
    if not isinstance(returns, dict):
        raise ReturnsRejected(f"{name}: returns_series is {type(returns).__name__}, not a mapping")
    if not isinstance(session_days, list):
        raise ReturnsRejected(f"{name}: session_days is {type(session_days).__name__}, not a list")
    bad_key = next((key for key in returns if not is_ns_text(key)), _MISSING)
    if bad_key is not _MISSING:
        raise ReturnsRejected(f"{name}: returns_series key {bad_key!r} is not nanoseconds as text")
    # A sentinel no JSON value can be: a JSON null in the list is a day this refuses.
    bad_day = next((day for day in session_days if not is_ns_text(day)), _MISSING)
    if bad_day is not _MISSING:
        raise ReturnsRejected(f"{name}: session day {bad_day!r} is not nanoseconds as text")
    # `record_trial` writes each session day once, in order (`canon_trading.ledger.session_days`).
    disorder = next(
        ((a, b) for a, b in itertools.pairwise(int(day) for day in session_days) if b <= a), None
    )
    if disorder is not None:
        raise ReturnsRejected(
            f"{name}: session day {_date(disorder[1])} follows {_date(disorder[0])}, not after it; "
            "record_trial writes each session day once, in order",
        )
    return returns, session_days


def _date(ns: int) -> str:
    return str(pd.to_datetime(ns, unit="ns", utc=True).date())


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
    # The UTC dates `session_returns` gave, their zone restored after skfolio's zone-free build.
    matrix.index = pd.DatetimeIndex(matrix.index).tz_localize("UTC")
    missing = matrix.isna()
    short = [name for name in matrix.columns if missing[name].any()]
    if short:
        by_name = {str(s.name): s for s in series}  # the columns are `str` of each name
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
