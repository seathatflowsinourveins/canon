"""The returns matrix of ADR 0010 step 2: the shapes it rejects, closed days, and the common range.

No test here runs a backtest, opens a network connection or reads a credential. The series are written
by hand in the form a ledger entry holds them: nanoseconds since the epoch, as text, to a return. Day 0
is Tuesday 2025-03-11 at 00:00 UTC.
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from fractions import Fraction

import numpy as np
import pandas as pd
import pytest
from skfolio import Portfolio
from skfolio import measures

from canon_trading.ledger import DAY_NS
from canon_trading.returns_matrix import ReturnsRejected
from canon_trading.returns_matrix import daily_series
from canon_trading.returns_matrix import returns_matrix
from canon_trading.returns_matrix import session_returns

DAY_0 = 1_741_651_200 * 1_000_000_000  # 2025-03-11T00:00:00Z


def day(n: int) -> int:
    return DAY_0 + n * DAY_NS


def entry(values: dict[int, float]) -> dict[str, float]:
    return {str(day(n)): value for n, value in values.items()}


def sessions(*days: int) -> list[str]:
    return [str(day(n)) for n in days]


def test_a_daily_series_keeps_its_session_days() -> None:
    returns = entry({0: 0.01, 1: -0.02, 2: 0.0, 3: 0.03})

    series = daily_series("a", returns, sessions(0, 1, 2, 3))

    assert list(series) == [0.01, -0.02, 0.0, 0.03]
    assert [stamp.date() for stamp in series.index] == [
        date(2025, 3, 11),
        date(2025, 3, 12),
        date(2025, 3, 13),
        date(2025, 3, 14),
    ]
    assert all(stamp.tzname() == "UTC" for stamp in series.index)


def test_closed_days_are_dropped() -> None:
    """Days 5 and 6 (Sunday and Monday) had no bar: their zero returns go, the rest stay."""
    returns = entry({n: 0.0 if n in (5, 6) else 0.001 * (n + 1) for n in range(9)})

    series = daily_series("a", returns, sessions(0, 1, 2, 3, 4, 7, 8))

    assert list(series) == pytest.approx([0.001, 0.002, 0.003, 0.004, 0.005, 0.008, 0.009])


@pytest.mark.parametrize(
    ("returns", "open_days", "message"),
    [
        pytest.param({}, sessions(0), "returns_series is empty", id="empty"),
        pytest.param(
            {str(day(0) + 3_600 * 10**9): 0.01, str(day(1)): 0.02},
            sessions(0, 1),
            "keys off a UTC midnight",
            id="per-position-fallback",
        ),
        pytest.param(
            entry({0: 0.01, 1: 0.02, 3: 0.01}),
            sessions(0, 1, 2, 3),
            "skips from",
            id="a-missing-day",
        ),
        pytest.param(
            entry({0: 0.01, 1: 0.02, 2: 0.005}),
            sessions(0, 1),
            "a day with no bar of the run",
            id="a-return-on-a-closed-day",
        ),
        pytest.param(
            entry({0: 0.01, 1: float("nan")}),
            sessions(0, 1),
            "return nan on 2025-03-12 is not finite",
            id="a-return-that-is-not-finite",
        ),
        pytest.param(
            entry({0: 0.0, 1: 0.0}),
            sessions(),
            "no return falls on a session day",
            id="no-session-day",
        ),
    ],
)
def test_any_other_shape_is_rejected(
    returns: dict[str, float], open_days: list[str], message: str
) -> None:
    with pytest.raises(ReturnsRejected, match=message):
        daily_series("t", returns, open_days)


def test_a_trial_over_a_shorter_range_is_refused_not_trimmed() -> None:
    """One trial covers days 0 to 6, the other 2 to 9: each lacks days the other has (ADR 0041)."""
    first = daily_series("a", entry({n: 0.01 * n for n in range(7)}), sessions(*range(7)))
    second = daily_series("b", entry({n: -0.01 * n for n in range(2, 10)}), sessions(*range(2, 10)))

    with pytest.raises(ReturnsRejected) as rejected:
        returns_matrix([first, second])

    assert rejected.value.names == ("a", "b")
    message = str(rejected.value)
    assert "a: no return on 3 of the 10 session days of the sample, the first 2025-03-18" in message
    assert "b: no return on 2 of the 10 session days of the sample, the first 2025-03-11" in message


def test_series_on_the_same_days_make_the_matrix() -> None:
    first = daily_series("a", entry({n: 0.01 * n for n in range(5)}), sessions(*range(5)))
    second = daily_series("b", entry({n: -0.01 * n for n in range(5)}), sessions(*range(5)))

    matrix = returns_matrix([first, second])

    assert list(matrix.columns) == ["a", "b"]
    assert len(matrix) == 5
    assert matrix.index[0].date() == date(2025, 3, 11)
    assert list(matrix["b"]) == pytest.approx([0.0, -0.01, -0.02, -0.03, -0.04])


def test_calendars_that_differ_inside_the_range_are_refused() -> None:
    """Trial b had no bar on day 3, so its row is gone there while trial a has one."""
    first = daily_series("a", entry({n: 0.01 * (n + 1) for n in range(6)}), sessions(*range(6)))
    second = daily_series(
        "b",
        entry({n: 0.0 if n == 3 else 0.02 * (n + 1) for n in range(6)}),
        sessions(0, 1, 2, 4, 5),
    )

    with pytest.raises(
        ReturnsRejected, match=r"b: no return on 1 of the 6 session days[^\n]*2025-03-14"
    ):
        returns_matrix([first, second])


def test_ranges_that_do_not_overlap_are_refused_naming_both() -> None:
    first = daily_series("a", entry({0: 0.01, 1: 0.02, 2: 0.04}), sessions(0, 1, 2))
    second = daily_series("b", entry({4: 0.01, 5: 0.02, 6: 0.04}), sessions(4, 5, 6))

    with pytest.raises(ReturnsRejected) as rejected:
        returns_matrix([first, second])

    assert rejected.value.names == ("a", "b")


def test_a_return_that_is_not_finite_is_refused_when_called_directly() -> None:
    """skfolio's `Portfolio` turns a NaN into 0.0; the matrix would hold a return nobody earned."""
    index = pd.to_datetime([day(n) for n in range(3)], unit="ns", utc=True)
    first = pd.Series([0.01, float("nan"), 0.02], index=index, name="a")
    second = pd.Series([0.01, 0.0, 0.02], index=index, name="b")

    with pytest.raises(ReturnsRejected, match="a: a return that is not finite") as rejected:
        returns_matrix([first, second])

    assert rejected.value.names == ("a",)


def test_a_session_day_with_no_return_is_refused() -> None:
    """A later review of 2026-11-10: a session day missing from the returns was dropped from the
    calendar, so three trials recording 31 session days and 30 returns ranked at T = 30."""
    returns = entry({n: 0.001 * (n + 1) for n in range(30)})

    with pytest.raises(
        ReturnsRejected, match="a: no return on 1 of its 31 session days, the first 2025-04-10"
    ):
        daily_series("a", returns, sessions(*range(31)))


def test_names_distinct_only_by_type_are_refused() -> None:
    """skfolio names each column by `str`, so series named 1 and "1" became columns 1 and 1_1."""
    index = pd.to_datetime([0, DAY_NS], unit="ns", utc=True)
    one, other = (
        pd.Series([0.01, 0.02], index=index, name=1),
        pd.Series([0.01, 0.03], index=index, name="1"),
    )

    with pytest.raises(ValueError, match="distinct names"):
        returns_matrix([one, other])


def test_series_named_by_numbers_that_cover_other_days_are_refused_by_name() -> None:
    """A third reader: names that are not text met skfolio's text column names in the lookup, and a
    series short of a day raised `KeyError` instead of being named."""
    short = daily_series("x", entry({0: 0.01, 1: 0.02, 2: 0.04}), sessions(0, 1, 2)).rename(1)
    full = daily_series("y", entry({0: 0.01, 1: 0.02, 2: 0.03, 3: 0.05}), sessions(0, 1, 2, 3))

    with pytest.raises(ReturnsRejected, match="1: no return on 1 of the 4 session days") as refused:
        returns_matrix([short, full.rename(2)])
    assert refused.value.names == ("1",)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(False, "value False on [0-9]+ is not a float", id="bool"),
        pytest.param(1, "value 1 on [0-9]+ is not a float", id="int"),
        pytest.param(float("nan"), "return nan on 2025-03-11 is not finite", id="nan"),
    ],
)
def test_a_return_that_is_not_a_finite_float_is_refused(value: object, message: str) -> None:
    """Antigravity, gate round 4: `record_trial` writes each return as a float, and a `bool` read
    as equal to 0.0 or 1.0 by another check went through."""
    returns = {**entry({0: 0.01, 1: 0.02, 2: 0.03}), str(day(0)): value}

    with pytest.raises(ReturnsRejected, match=f"a: [^\n]*{message}"):
        daily_series("a", returns, sessions(0, 1, 2))


def test_returns_whose_squares_overflow_are_refused() -> None:
    """Antigravity, gate round 4: each return finite, about 1e155, their squares past float64's
    largest, so skfolio's Sharpe ratio of them read 0.0 rather than failing. skfolio itself is
    asked for what the gate takes of them (round 8), and gives no deflated Sharpe ratio: its
    skewness and kurtosis of them are NaN."""
    returns = entry({0: 1e155, 1: -2e155, 2: 3e155})

    with pytest.raises(
        ReturnsRejected,
        match="a: [^\n]*skfolio's deflated Sharpe ratio is nan[^\n]*skewness nan, kurtosis nan",
    ):
        daily_series("a", returns, sessions(0, 1, 2))


@pytest.mark.parametrize(
    "days",
    [
        pytest.param(sessions(2, 1, 0), id="reversed"),
        pytest.param(sessions(0, 1, 1, 2), id="a day twice"),
    ],
)
def test_session_days_out_of_record_trial_s_order_are_refused(days: list[str]) -> None:
    """A sixth-round reader (Antigravity): `record_trial` writes each session day once, in order, and
    a list reversed or holding a day twice was read as a set and passed."""
    returns = entry({0: 0.01, 1: -0.02, 2: 0.03})

    with pytest.raises(ReturnsRejected, match=r"a: session day .* follows .*, not after it"):
        daily_series("a", returns, days)


def test_returns_whose_moments_underflow_are_refused() -> None:
    """A sixth-round reader (Antigravity): returns of about 1e-81 apart passed every check, and their
    fourth central moment rounded to 0.0, so skfolio's kurtosis read 0, below the least any series
    has, and a pick that is rejected at its own scale passed at 0.95."""
    pattern = [1.24, -1.24] * 14 + [1.45, -1.45]
    returns = entry({n: (0.88 + x) * 1e-81 for n, x in enumerate(pattern)})

    with pytest.raises(ReturnsRejected, match=r"a: .* below float64's smallest normal"):
        daily_series("a", returns, sessions(*range(30)))


# The gate's seventh outside round, 2026-11-11: one numeric check of the session-day series every
# gate calculation uses (ADR 0080). Each test below failed before that check.

# Monday 2025-03-10 is day -1: forty UTC days from it hold thirty weekday sessions and five weekends.
WEEKDAYS = [n for n in range(-1, 39) if (n + 1) % 7 < 5]
SWINGS = [1.0, -1.0] * 14 + [2.0, -2.0]


def over_weekends(open_day_returns: list[float]) -> dict[str, float]:
    """`open_day_returns` on the thirty weekdays, and 0.0 on each closed day between them."""
    by_day = dict(zip(WEEKDAYS, open_day_returns, strict=True))
    return entry({n: by_day.get(n, 0.0) for n in range(-1, 39)})


def test_closed_day_zeros_do_not_hide_moments_that_underflow() -> None:
    """Antigravity, gate round 7: the moments were taken over every UTC day, closed days' zeros
    included, and the gate computes on session days only. Returns of 4.0e-77, 1.6e-81 apart, kept
    the checked moments normal through the weekend zeros, while the session-day kurtosis rounded
    from 1.39 to 1.0 and a pick rejected at its own scale passed."""
    returns = over_weekends([4.0e-77 + x * 1.6e-81 for x in SWINGS])

    with pytest.raises(ReturnsRejected, match=r"a: .* below float64's smallest normal"):
        daily_series("a", returns, sessions(*WEEKDAYS))


def test_closed_day_zeros_do_not_hide_moments_that_overflow() -> None:
    """Antigravity, gate round 7: weekend zeros pulled the mean toward 0 and kept the checked fourth
    central moment finite, while that of the session days alone is past float64's largest: skfolio's
    kurtosis of them is NaN, and it gives no deflated Sharpe ratio."""
    returns = over_weekends([6.0e76] * 29 + [-6.0e76])

    with pytest.raises(
        ReturnsRejected, match=r"a: [^\n]*deflated Sharpe ratio is nan[^\n]*kurtosis nan"
    ):
        daily_series("a", returns, sessions(*WEEKDAYS))


@pytest.mark.parametrize("constant", [-0.01, 0.01, -0.07, 0.1, 0.3])
def test_a_constant_series_is_refused_whatever_its_mean_rounds_to(constant: float) -> None:
    """Antigravity, gate round 7: thirty returns of -0.01 have a mean one rounding step from -0.01,
    so skfolio's standard deviation of them is 1.8e-18, not 0, and their Sharpe ratio a finite
    -5.67e15 (-0.07 happens to round to itself, and gave -inf). A series with no spread at its own
    size is refused by name, by one measure, whichever way its mean rounds."""
    returns = entry(dict.fromkeys(range(30), constant))

    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding"):
        daily_series("a", returns, sessions(*range(30)))


@pytest.mark.parametrize("constant", [1e200, -1e200, 3e250])
def test_a_constant_series_too_large_to_square_is_refused_as_one_that_does_not_vary(
    constant: float,
) -> None:
    """Second code review of the round-7 change: thirty returns of 1e200 are a constant series,
    but the square of what rounding their mean left overflows, so skfolio's variance is inf and
    the refusal named that, not the constancy. Equal returns are refused as not varying before
    any statistic of them is read."""
    returns = entry(dict.fromkeys(range(30), constant))

    with pytest.raises(ReturnsRejected, match="a: its returns do not vary") as rejected:
        session_returns("a", returns, sessions(*range(30)))

    assert rejected.value.no_spread and "inf" not in str(rejected.value)


def test_a_series_of_zeros_is_refused_as_one_that_does_not_vary() -> None:
    """A strategy that never moved its balance: every return 0.0, the exact case of the same
    measure (float64's spacing at 0 is its smallest subnormal, and no spread is no more than it)."""
    returns = entry(dict.fromkeys(range(30), 0.0))

    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding"):
        daily_series("a", returns, sessions(*range(30)))


@pytest.mark.parametrize(("low", "high"), [(1e-170, 2e-170), (1e-160, 3e-160), (1e-90, 3e-90)])
def test_returns_that_vary_and_underflow_are_refused_as_underflow(low: float, high: float) -> None:
    """Code review of the round-7 change: returns that differ by a factor of two or three, small
    enough that their squares underflow, were refused for the wrong reason: at 1e-170 as not
    varying, their standard deviation having underflowed to 0.0, and at 1e-160 and 1e-90 as a
    kurtosis that is NaN. The cause is the underflow, and the refusal names it."""
    returns = entry({n: high if n % 2 else low for n in range(30)})

    with pytest.raises(ReturnsRejected) as rejected:
        session_returns("a", returns, sessions(*range(30)))

    message = str(rejected.value)
    assert "underflow" in message and "below float64's smallest normal" in message
    assert "do not vary" not in message and "nan" not in message


def test_one_session_day_gives_no_series() -> None:
    """One return has no sample standard deviation: skfolio's Sharpe ratio of it is NaN, and that
    is the refusal, not a count kept here."""
    with pytest.raises(
        ReturnsRejected,
        match="a: of its returns on its 1 session day, skfolio's Sharpe ratio is nan",
    ) as rejected:
        session_returns("a", entry({0: 0.01}), sessions(0))

    assert not rejected.value.no_spread


def test_a_series_just_above_the_threshold_is_accepted_with_the_sharpe_ratio_it_has() -> None:
    """The limit of the measure (ADR 0080): returns of 1.0 and 1.0 + 1,024 steps of float64's
    spacing there have a standard deviation of 521 steps, above the 480 that sixteen times thirty
    returns' rounding allows, so the series is accepted, its Sharpe ratio is the 8.6e12 its returns
    give, and rounding can move it by less than its own size. With 896 steps the deviation is 456,
    and the series is refused: its bound would not be below its ratio."""
    step = math.ulp(1.0)
    above = entry({n: 1.0 + 1024 * step * (n % 2) for n in range(30)})
    below = entry({n: 1.0 + 896 * step * (n % 2) for n in range(30)})

    accepted = session_returns("a", above, sessions(*range(30)))

    assert accepted.sharpe == pytest.approx((1.0 + 512 * step) / (512 * step * math.sqrt(30 / 29)))
    assert accepted.sharpe > 8.6e12
    assert 0.9 * accepted.sharpe < accepted.rounding < accepted.sharpe
    assert list(accepted.series) == list(above.values())
    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding"):
        session_returns("a", below, sessions(*range(30)))


# The gate's eighth outside round, 2026-11-11 (Antigravity): a series is valid exactly when the
# skfolio functions the gate calls accept it, with the two float64 conditions skfolio does not
# catch (ADR 0080). Each test below failed before that model.

# Fifty-five returns at two levels whose Sharpe ratio is next to 2 / skewness, where the variance
# adjustment of skfolio's probabilistic Sharpe ratio, (1 - skew * SR / 2)**2 for two levels, is 0.
HIGH, LOW = 0.05650675558054833, 0.05450675558054833


def test_two_session_days_give_no_series_since_skfolio_gives_no_dsr_of_them() -> None:
    """Round 8, finding 3: the check counted two returns as enough, the least a sample variance
    takes, and read six finite statistics of them, while skfolio's deflated Sharpe ratio, which
    the gate takes, is NaN below three. A record of two session days was told `COMPLETE`."""
    returns = entry({0: 0.01, 1: -0.02})
    assert math.isnan(measures.deflated_sharpe_ratio([0.01, -0.02], [-0.2357]))

    with pytest.raises(
        ReturnsRejected,
        match="a: of its returns on its 2 session days, skfolio's deflated Sharpe ratio is nan",
    ):
        session_returns("a", returns, sessions(0, 1))

    three = session_returns("a", entry({0: 0.01, 1: -0.02, 2: 0.005}), sessions(0, 1, 2))
    assert math.isfinite(three.sharpe)


@pytest.mark.parametrize("sign", [1.0, -1.0], ids=["a positive ratio", "a negative ratio"])
def test_a_variance_adjustment_not_above_zero_gives_no_series_in_either_sign(sign: float) -> None:
    """Round 8, finding 3: 27 returns at one level and 28 at another, 0.002 apart, have a finite
    Sharpe ratio (54.99), variance, skewness, kurtosis and self-correlation, and a spread 2.6e12
    times the mean's rounding, so the check accepted them; skfolio's variance adjustment of them
    is -6e-13, and its deflated Sharpe ratio NaN. With a negative ratio the series was never the
    pick, so nothing found it. The same levels with the counts exchanged have a DSR of 1.0: the
    condition is skfolio's, of the series and its sign as it stands, and the check asks it."""
    values = [sign * HIGH] * 27 + [sign * LOW] * 28
    with np.errstate(all="ignore"):
        ratio = float(np.mean(values) / measures.standard_deviation(values))
        assert math.isnan(measures.deflated_sharpe_ratio(values, [ratio]))
    assert ratio == pytest.approx(sign * 54.9909)

    with pytest.raises(ReturnsRejected, match="a: [^\n]*deflated Sharpe ratio is nan") as rejected:
        session_returns("a", entry(dict(enumerate(values))), sessions(*range(55)))

    assert "variance adjustment" in str(rejected.value) and not rejected.value.no_spread
    exchanged = [sign * HIGH] * 28 + [sign * LOW] * 27
    accepted = session_returns("a", entry(dict(enumerate(exchanged))), sessions(*range(55)))
    assert accepted.sharpe == pytest.approx(sign * 55.0269)


def test_what_skfolio_raises_on_is_refused_naming_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's other half: a call skfolio raises `ValueError` on refuses the series with
    skfolio's own words. No series reaches one today, since the deflated Sharpe ratio is asked only
    of a finite ratio, so the call is made to raise."""

    def raises(*_: object) -> float:
        raise ValueError("trial_sharpe_ratios must not contain infinite values")

    monkeypatch.setattr("canon_trading.returns_matrix.measures.deflated_sharpe_ratio", raises)

    with pytest.raises(
        ReturnsRejected,
        match="a: [^\n]*skfolio's deflated Sharpe ratio raised ValueError: trial_sharpe_ratios",
    ) as rejected:
        session_returns("a", entry({0: 0.01, 1: -0.02, 2: 0.005}), sessions(0, 1, 2))

    assert "variance adjustment" not in str(rejected.value)  # that explains a NaN, not an error


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([-1.0 - 880 * math.ulp(1.0) * (n % 2) for n in range(30)], id="14.9 times"),
        pytest.param(
            [-1.5 + k * math.ulp(1.5) for k in [1776, -1776, 432, -432] + [0] * 26],
            id="16 times exactly",
        ),
        pytest.param([1.0 + 128 * math.ulp(1.0) * (n % 2) for n in range(30)], id="2.2 times"),
    ],
)
def test_a_series_whose_ratio_rounding_can_move_by_its_own_size_is_refused(
    values: list[float],
) -> None:
    """Round 8, finding 2: a series was refused when its standard deviation was at most 2 times
    the mean's rounding, and its ratio's bound is 16 times that rounding over the deviation, so
    between the two it was accepted with a bound at or above its own ratio. skfolio answers for
    each with finite numbers. One threshold now, the bound's own: such a series is refused, in
    either sign."""
    with np.errstate(all="ignore"):
        ratio = float(np.mean(values) / measures.standard_deviation(values))
        assert math.isfinite(measures.deflated_sharpe_ratio(values, [ratio]))

    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding") as no:
        session_returns("a", entry(dict(enumerate(values))), sessions(*range(30)))

    assert (
        no.value.no_spread
        and "move their Sharpe ratio by the larger of 1 and its own size" in str(no.value)
    )


def test_a_spread_inside_the_mean_s_rounding_is_what_skfolio_does_not_catch() -> None:
    """Why the spread is still measured here (ADR 0080): skfolio's variance subtracts the computed
    mean once, so a spread smaller than that mean's rounding error is read as the error. One
    hundred returns of 1.63 and two or four steps of float64's spacing above it have a Sharpe
    ratio of 4.46e15 in exact arithmetic; skfolio gives 2.15e15, and a finite deflated Sharpe
    ratio."""
    step = 2 * math.ulp(1.6304211694124828)
    values = [1.6304211694124828 + step * (n * 7 % 3) for n in range(100)]
    exact = [Fraction(value) for value in values]
    mean = sum(exact) / 100
    deviation = math.sqrt(sum((value - mean) ** 2 for value in exact) / 99)
    with np.errstate(all="ignore"):
        ratio = float(np.mean(values) / measures.standard_deviation(values))
        assert math.isfinite(measures.deflated_sharpe_ratio(values, [ratio]))
    assert float(mean) / deviation == pytest.approx(4.46e15, rel=1e-2)
    assert ratio == pytest.approx(2.15e15, rel=1e-2)

    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding"):
        session_returns("a", entry(dict(enumerate(values))), sessions(*range(100)))


def test_the_series_comes_with_skfolio_s_sharpe_ratio_of_it() -> None:
    """The gate reads each trial's Sharpe ratio from the check's result: skfolio's own, of the
    session days alone, and how far a second computation of it could differ."""
    values = [0.001 * (n + 1) * (-1) ** n for n in range(9)]
    returns = entry({n: 0.0 if n in (5, 6) else values[n] for n in range(9)})

    checked = session_returns("a", returns, sessions(0, 1, 2, 3, 4, 7, 8))

    kept = [values[n] for n in (0, 1, 2, 3, 4, 7, 8)]
    assert list(checked.series) == kept
    assert checked.sharpe == float(Portfolio(X=[[v] for v in kept], weights=[1.0]).sharpe_ratio)
    assert checked.sharpe == pytest.approx(statistics.fmean(kept) / statistics.stdev(kept))
    assert 0 < checked.rounding < 1e-12
