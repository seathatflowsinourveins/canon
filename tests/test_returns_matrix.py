"""The returns matrix of ADR 0010 step 2: the shapes it rejects, closed days, and the common range.

No test here runs a backtest, opens a network connection or reads a credential. The series are written
by hand in the form a ledger entry holds them: nanoseconds since the epoch, as text, to a return. Day 0
is Tuesday 2025-03-11 at 00:00 UTC.
"""

from __future__ import annotations

from datetime import date

import pytest

from canon_trading.ledger import DAY_NS
from canon_trading.returns_matrix import ReturnsRejected
from canon_trading.returns_matrix import daily_series
from canon_trading.returns_matrix import returns_matrix

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
            sessions(5),
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


def test_the_matrix_is_trimmed_to_the_common_range() -> None:
    """One trial covers days 0 to 6, the other 2 to 9: the common range is days 2 to 6, T = 5."""
    first = daily_series("a", entry({n: 0.01 * n for n in range(7)}), sessions(*range(7)))
    second = daily_series("b", entry({n: -0.01 * n for n in range(2, 10)}), sessions(*range(2, 10)))

    matrix = returns_matrix([first, second])

    assert list(matrix.columns) == ["a", "b"]
    assert len(matrix) == 5
    assert matrix.index[0].date() == date(2025, 3, 13)
    assert matrix.index[-1].date() == date(2025, 3, 17)
    assert list(matrix["a"]) == pytest.approx([0.02, 0.03, 0.04, 0.05, 0.06])
    assert list(matrix["b"]) == pytest.approx([-0.02, -0.03, -0.04, -0.05, -0.06])


def test_calendars_that_differ_inside_the_range_are_refused() -> None:
    """Trial b had no bar on day 3, so its row is gone there while trial a has one."""
    first = daily_series("a", entry({n: 0.01 for n in range(6)}), sessions(*range(6)))
    second = daily_series(
        "b", entry({n: 0.0 if n == 3 else 0.02 for n in range(6)}), sessions(0, 1, 2, 4, 5)
    )

    with pytest.raises(ReturnsRejected, match=r"b: no return on 2025-03-14"):
        returns_matrix([first, second])


def test_ranges_that_do_not_overlap_are_refused() -> None:
    first = daily_series("a", entry({0: 0.01, 1: 0.02}), sessions(0, 1))
    second = daily_series("b", entry({4: 0.01, 5: 0.02}), sessions(4, 5))

    with pytest.raises(ReturnsRejected, match="no day in common"):
        returns_matrix([first, second])
