"""The deflated Sharpe ratio that gates a ranking (ADR 0010), checked against the paper's own example.

No test here opens a network connection, reads a credential or runs a backtest. This file tests skfolio,
pinned in `pyproject.toml`, not code of this repo; `tests/test_ranking.py` tests the code that ranks. The
three functions ADR 0031 names are `measures.expected_max_sharpe_ratio`,
`measures.probabilistic_sharpe_ratio` and `measures.deflated_sharpe_ratio`. They implement equations 1
and 2 of Bailey and López de Prado, "The Deflated Sharpe Ratio" (2014),
https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf. Four more, `measures.mean`,
`standard_deviation`, `skew` and `kurtosis`, are called to show which moments those three read.
Everything else is the standard library.

The example is the paper's "A numerical example" (pp. 9-10). A strategist ran N = 100 trials whose
Sharpe ratios have variance 1/2, annualised, and picked one with an annualised Sharpe ratio of 2.5 over
T = 1250 daily observations, skewness -3 and kurtosis 10. With 250 observations a year the paper gets,
per observation:

* an expected maximum Sharpe ratio of 0.1132 under no skill, and a deflated Sharpe ratio of 0.9004,
  below the 0.95 the investor asks for;
* 0.9505 had the same pick come from N = 46 trials;
* 0.9505 from N = 88 trials had its returns been Normal, with skewness 0 and kurtosis 3.

skfolio takes the pick's return series and every trial's Sharpe ratio, not their moments, so each case
is run on a made-up series that carries the moments:

* Skewness -3 with kurtosis 10 lies on the boundary kurtosis = skewness² + 1, which only a
  distribution with two values reaches. With 1250 observations the nearest split is 105 of one value
  and 1145 of the other, which has skewness -2.9994 and kurtosis 9.9965. skfolio's own test of this
  example uses the same series (`tests/test_measures/test_measures.py`, the same at v1.12.8 and
  v1.12.9). On it the first two results are 0.900405 and 0.950508, where the paper's exact
  moments give 0.900397 and 0.950502: the same four printed decimals.
* Skewness 0 with kurtosis 3 is met exactly by four values, +1, -1, +c and -c, 500 each of the first
  two and 125 each of the others, with c chosen below.
* The trial Sharpe ratios alternate between two values, scaled to the sample variance the paper states.

Every number passed to skfolio is per observation, as ADR 0031 requires. The last four tests show what
skfolio does with input the protocol forbids. In each it returns a number and raises nothing, so the
code that applies the gate has to refuse that input itself.
"""

import math
import statistics
from collections.abc import Callable

import pytest
from skfolio import measures

OBSERVATIONS = 1250
OBSERVATIONS_PER_YEAR = 250
SHARPE = 2.5 / math.sqrt(OBSERVATIONS_PER_YEAR)
TRIAL_SHARPE_STD = math.sqrt(0.5 / OBSERVATIONS_PER_YEAR)
THRESHOLD = 0.95

# The paper prints four decimals.
PRINTED = 5e-5


def _with_the_picks_sharpe(values: list[float]) -> list[float]:
    """Shift and scale `values` to mean `SHARPE` and sample standard deviation 1."""
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values)
    return [(value - mean) / deviation + SHARPE for value in values]


def _skewed_returns() -> list[float]:
    return _with_the_picks_sharpe([0.0] * 105 + [1.0] * 1145)


def _normal_returns() -> list[float]:
    ones, others = 500, 125
    # The values are symmetric about zero, so kurtosis is T·Σv⁴ / (Σv²)², with T = OBSERVATIONS,
    # Σv² = 2·ones + 2·others·c² and Σv⁴ = 2·ones + 2·others·c⁴. Setting it to 3 gives a quadratic in
    # x = c²: (2T·others - 12·others²)·x² - 24·ones·others·x + (2T·ones - 12·ones²) = 0. With these
    # counts one root is negative, so c² is the other one, about 13.07.
    quadratic = 2 * OBSERVATIONS * others - 12 * others**2
    linear = -24 * ones * others
    constant = 2 * OBSERVATIONS * ones - 12 * ones**2
    c_squared = (-linear + math.sqrt(linear**2 - 4 * quadratic * constant)) / (2 * quadratic)
    assert c_squared > 0
    c = math.sqrt(c_squared)
    return _with_the_picks_sharpe([1.0] * ones + [-1.0] * ones + [c] * others + [-c] * others)


def _trial_sharpes(trials: int) -> list[float]:
    signs = [1.0 if trial % 2 == 0 else -1.0 for trial in range(trials)]
    scale = TRIAL_SHARPE_STD / statistics.stdev(signs)
    return [sign * scale for sign in signs]


def _deflated(returns: list[float], trial_sharpes: list[float]) -> float:
    return float(measures.deflated_sharpe_ratio(returns, trial_sharpes))


def test_the_series_carry_the_moments_of_the_example() -> None:
    skewed = _skewed_returns()
    normal = _normal_returns()

    for returns in (skewed, normal):
        assert len(returns) == OBSERVATIONS
        sharpe = float(measures.mean(returns)) / float(measures.standard_deviation(returns))
        assert sharpe == pytest.approx(SHARPE, abs=1e-12)

    # skfolio's kurtosis is the raw one, 3 for a Normal distribution, which is what equation 2 takes.
    assert float(measures.skew(skewed)) == pytest.approx(-3.0, abs=1e-3)
    assert float(measures.kurtosis(skewed)) == pytest.approx(10.0, abs=5e-3)
    assert float(measures.skew(normal)) == pytest.approx(0.0, abs=1e-9)
    assert float(measures.kurtosis(normal)) == pytest.approx(3.0, abs=1e-9)

    for trials in (46, 47, 48, 88, 89, 100):
        sharpes = _trial_sharpes(trials)
        assert len(sharpes) == trials
        assert float(measures.standard_deviation(sharpes)) == pytest.approx(
            TRIAL_SHARPE_STD, abs=1e-12
        )


def test_the_expected_maximum_sharpe_ratio_is_the_papers() -> None:
    expected_maximum = measures.expected_max_sharpe_ratio(100, TRIAL_SHARPE_STD)

    assert expected_maximum == pytest.approx(0.1132, abs=PRINTED)


@pytest.mark.parametrize(
    ("make_returns", "trials", "printed", "passes"),
    [
        pytest.param(_skewed_returns, 100, 0.9004, False, id="100-trials-rejected"),
        pytest.param(_skewed_returns, 46, 0.9505, True, id="46-trials-pass"),
        pytest.param(_normal_returns, 88, 0.9505, True, id="88-trials-pass-if-normal"),
    ],
)
def test_the_deflated_sharpe_ratio_is_the_papers(
    worked_dsr: Callable[..., tuple[float, float]],
    make_returns: Callable[[], list[float]],
    trials: int,
    printed: float,
    passes: bool,
) -> None:
    returns = make_returns()
    sharpes = _trial_sharpes(trials)

    deflated = _deflated(returns, sharpes)

    assert deflated == pytest.approx(printed, abs=PRINTED)
    assert (deflated >= THRESHOLD) is passes
    assert deflated == pytest.approx(worked_dsr(returns, sharpes)[1], abs=1e-9)


@pytest.mark.parametrize(
    ("make_returns", "last_passing"),
    [
        pytest.param(_skewed_returns, 46, id="skewed"),
        pytest.param(_normal_returns, 88, id="normal"),
    ],
)
def test_each_further_trial_raises_the_hurdle(
    make_returns: Callable[[], list[float]], last_passing: int
) -> None:
    """N is the length of the trial array: with the same spread, one more trial lowers the result."""
    returns = make_returns()

    here, one_more, two_more = (
        _deflated(returns, _trial_sharpes(last_passing + extra)) for extra in (0, 1, 2)
    )

    assert here > one_more > two_more
    assert here >= THRESHOLD > one_more


def test_the_two_step_route_gives_the_same_result() -> None:
    """ADR 0010 reports an adjusted trial count through the two functions `deflated_sharpe_ratio` wraps (ADR 0031)."""
    returns = _skewed_returns()
    sharpes = _trial_sharpes(46)

    expected_maximum = measures.expected_max_sharpe_ratio(
        len(sharpes), float(measures.standard_deviation(sharpes))
    )
    in_two_steps = measures.probabilistic_sharpe_ratio(
        returns, benchmark_sharpe_ratio=expected_maximum
    )

    assert _deflated(returns, sharpes) == pytest.approx(float(in_two_steps), abs=1e-12)


def test_skfolio_drops_a_trial_that_has_no_sharpe_ratio() -> None:
    """A NaN trial is left out of N and of the spread, and here that lowers the hurdle."""
    returns = _skewed_returns()
    sharpes = _trial_sharpes(48)
    with_gaps = [math.nan, math.nan, *sharpes[2:]]

    dropped = _deflated(returns, with_gaps)

    assert dropped == pytest.approx(_deflated(returns, sharpes[2:]), abs=1e-12)
    assert dropped > _deflated(returns, sharpes)


def test_skfolio_ignores_a_missing_return_of_the_pick() -> None:
    """A NaN return is left out of T and of every moment, and here that raises the result."""
    returns = _skewed_returns()
    sharpes = _trial_sharpes(48)
    with_gaps = [math.nan] * 5 + returns[5:]

    ignored = _deflated(with_gaps, sharpes)

    assert ignored == pytest.approx(_deflated(returns[5:], sharpes), abs=1e-12)
    assert ignored > _deflated(returns, sharpes)


def test_one_trial_or_identical_trials_leave_no_hurdle() -> None:
    """With N = 1 or no spread between trials, the result is the probabilistic Sharpe ratio against 0."""
    returns = _skewed_returns()
    against_zero = float(measures.probabilistic_sharpe_ratio(returns))

    assert _deflated(returns, [0.3]) == pytest.approx(against_zero, abs=1e-12)
    assert _deflated(returns, [5.0] * 46) == pytest.approx(against_zero, abs=1e-12)
    assert against_zero > _deflated(returns, _trial_sharpes(46))


def test_skfolio_gives_a_constant_series_the_spread_its_mean_rounds_to() -> None:
    """What the gate's numeric check rests on (ADR 0031, 2026-11-11; Antigravity, gate round 7):
    skfolio's variance subtracts the computed mean once, so thirty returns of -0.01, whose mean
    rounds, keep a standard deviation and a mean over it above 1e15, where thirty of 0.07, whose
    mean comes out exact, have none.
    Its skewness and kurtosis centre again and call both undefined."""
    rounded, exact = [-0.01] * 30, [0.07] * 30

    deviation = float(measures.standard_deviation(rounded))

    assert 0 < deviation < 30 * math.ulp(0.01)
    assert abs(float(measures.mean(rounded)) / deviation) > 1e15
    assert float(measures.standard_deviation(exact)) == 0.0
    for series in (rounded, exact):
        assert math.isnan(float(measures.skew(series)))
        assert math.isnan(float(measures.kurtosis(series)))


def test_skfolio_s_variance_of_equal_trial_sharpe_ratios_can_be_above_zero() -> None:
    """So V = 0 is read from the ratios, never from their variance (ADR 0031, 2026-11-11)."""
    assert float(measures.variance([0.1] * 3)) > 0
    assert float(measures.variance([5.0] * 46)) == 0.0


def test_annualised_trial_sharpe_ratios_do_not_mix_with_daily_returns() -> None:
    """The units are the caller's to keep: skfolio cannot tell an annualised Sharpe ratio from a daily one."""
    returns = _skewed_returns()
    sharpes = _trial_sharpes(46)
    assert _deflated(returns, sharpes) >= THRESHOLD

    annualised = [sharpe * math.sqrt(OBSERVATIONS_PER_YEAR) for sharpe in sharpes]

    assert _deflated(returns, annualised) < 0.01
