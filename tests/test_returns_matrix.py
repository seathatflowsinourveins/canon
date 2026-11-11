"""The returns matrix of ADR 0010 step 2: the shapes it rejects, closed days, and the common range.

No test here runs a backtest, opens a network connection or reads a credential. The series are written
by hand in the form a ledger entry holds them: nanoseconds since the epoch, as text, to a return. Day 0
is Tuesday 2025-03-11 at 00:00 UTC.
"""

from __future__ import annotations

import math
import statistics
from datetime import date

import pandas as pd
import pytest
from skfolio import Portfolio

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
    first = daily_series("a", entry({0: 0.01, 1: 0.02}), sessions(0, 1))
    second = daily_series("b", entry({4: 0.01, 5: 0.02}), sessions(4, 5))

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
    short = daily_series("x", entry({0: 0.01, 1: 0.02}), sessions(0, 1)).rename(1)
    full = daily_series("y", entry({0: 0.01, 1: 0.02, 2: 0.03}), sessions(0, 1, 2))

    with pytest.raises(ReturnsRejected, match="1: no return on 1 of the 3 session days") as refused:
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
    largest, so skfolio's Sharpe ratio of them read 0.0 rather than failing. The check names the
    variance skfolio takes of them (round 7's one check), where it summed their squares itself."""
    returns = entry({0: 1e155, 1: -2e155, 2: 3e155})

    with pytest.raises(ReturnsRejected, match="a: [^\n]*skfolio's variance is inf"):
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
    central moment finite, while that of the session days alone is past float64's largest."""
    returns = over_weekends([6.0e76] * 29 + [-6.0e76])

    with pytest.raises(ReturnsRejected, match=r"a: [^\n]*fourth central moment is inf"):
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
    with pytest.raises(ReturnsRejected, match="a: has 1 session day"):
        daily_series("a", entry({0: 0.01}), sessions(0))


def test_a_series_just_above_the_threshold_is_accepted_with_the_sharpe_ratio_it_has() -> None:
    """The limit of the measure (ADR 0080): returns of 1.0 and 1.0 + 128 steps of float64's spacing
    there have a standard deviation of 65 steps, above the 60 that twice thirty returns' rounding
    allows, so the series is accepted, and its Sharpe ratio is the 6.9e13 its returns give. With 96
    steps the deviation is 49, and the series is refused."""
    step = math.ulp(1.0)
    above = entry({n: 1.0 + 128 * step * (n % 2) for n in range(30)})
    below = entry({n: 1.0 + 96 * step * (n % 2) for n in range(30)})

    accepted = session_returns("a", above, sessions(*range(30)))

    assert accepted.sharpe == pytest.approx((1.0 + 64 * step) / (64 * step * math.sqrt(30 / 29)))
    assert accepted.sharpe > 6.9e13
    assert list(accepted.series) == list(above.values())
    with pytest.raises(ReturnsRejected, match="a: its returns do not vary beyond rounding"):
        session_returns("a", below, sessions(*range(30)))


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
