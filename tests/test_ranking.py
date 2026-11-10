"""The gate of ADR 0010 step 3 and ADR 0031, on a ledger study: its refusals, one by one, and two picks.

No test here runs a backtest, opens a network connection or reads a credential. Each writes trials to
an Optuna SQLite study in pytest's temporary directory, in the form `canon_trading.ledger.record_trial`
leaves them (the user attributes of ADR 0035 term 3); `tests/test_ledger.py` shows that form on real
runs. Every day is a session day, from Tuesday 2025-03-11, so the matrix is the series as written.

The two picks are worked here with the standard library, under the conventions ADR 0031 read in
skfolio's source (sample standard deviation in each Sharpe ratio, population skewness and raw
kurtosis, √(T−1)), the way `tests/test_deflated_sharpe.py` works the paper's example. T = 30 sessions
of a ten-day pattern; per session:

* the pick: Sharpe ratio 0.69911, skewness -0.14482, kurtosis 1.96139;
* a second trial at 0.22556, and a third at 0.22556 (passing case) or -0.67668 (failing case).

With N = 3, the passing case has SR₀ = 0.23316 and DSR = 0.98849, above 0.95. The failing case has the
wider spread, SR₀ = 0.59606 and DSR = 0.69241, below it. Both have T = 30 ≥ N(N−1)/2 = 3, so N̂ is
reported: the passing case's mean correlation between trials is 0.11226, so N̂ = 2.7755.
"""

from __future__ import annotations

import itertools
import math
import statistics
from statistics import NormalDist
from typing import TYPE_CHECKING

import optuna
import pytest
from optuna.trial import TrialState

from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import open_study
from canon_trading.ranking import THRESHOLD
from canon_trading.ranking import RankingStopped
from canon_trading.ranking import rank

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

optuna.logging.set_verbosity(optuna.logging.WARNING)

DAY_0 = 1_741_651_200 * 1_000_000_000  # 2025-03-11T00:00:00Z
EULER_MASCHERONI = 0.5772156649015329
T = 30
PATTERN = [0.01, -0.004, 0.006, 0.002, -0.003, 0.008, 0.004, -0.001, 0.005, 0.003]
PICK = [PATTERN[i % 10] + 0.0001 * (i % 3) for i in range(T)]
SECOND = [PATTERN[(i + 3) % 10] * 0.5 - 0.001 for i in range(T)]
THIRD_CLOSE = [PATTERN[(i + 5) % 10] * 0.6 - 0.0012 for i in range(T)]
THIRD_WIDE = [-PATTERN[(i + 5) % 10] * 0.8 for i in range(T)]


@pytest.fixture
def study(tmp_path: Path) -> Iterator[optuna.Study]:
    with open_study("gate", tmp_path / "trials.db") as opened:
        yield opened


def write(
    study: optuna.Study,
    key: str | None,
    returns: list[float] | None,
    *,
    outcome: str = TRADED,
    first_day: int = 0,
    tell: bool = True,
    told_complete: bool = True,
) -> int:
    """Record one trial as the ledger does; `returns` start at day `first_day`."""
    trial = study.ask()
    if key is not None:
        trial.set_user_attr("config_key", key)
    if outcome == FAILED:
        trial.set_user_attr(OUTCOME, FAILED)
        trial.set_user_attr("error", "RuntimeError: made up")
        study.tell(trial, state=TrialState.FAIL)
        return trial.number
    if not tell:
        return trial.number  # left RUNNING, as a run that never returned
    days = [DAY_0 + (first_day + n) * DAY_NS for n in range(len(returns or []))]
    trial.set_user_attr("run_config_id", key)
    trial.set_user_attr("backtest_start", days[0] if days else None)
    trial.set_user_attr("backtest_end", days[-1] if days else None)
    trial.set_user_attr(
        "returns_series", {str(d): value for d, value in zip(days, returns or [], strict=True)}
    )
    trial.set_user_attr("session_days", [str(d) for d in days])
    trial.set_user_attr(OUTCOME, outcome)
    if outcome == IDLE or not told_complete:
        study.tell(trial, state=TrialState.FAIL)
    else:
        study.tell(trial, 0.0)
    return trial.number


def sharpe(values: list[float]) -> float:
    return statistics.fmean(values) / statistics.stdev(values)


def worked_dsr(returns: list[float], trial_sharpes: list[float]) -> tuple[float, float]:
    """SR₀ and the DSR by eqs. 1-2, without skfolio (see the module docstring)."""
    mean = statistics.fmean(returns)
    variance = statistics.fmean((value - mean) ** 2 for value in returns)
    skewness = statistics.fmean((value - mean) ** 3 for value in returns) / variance**1.5
    kurtosis = statistics.fmean((value - mean) ** 4 for value in returns) / variance**2
    pick = sharpe(returns)
    trials = len(trial_sharpes)
    normal = NormalDist()
    sr0 = statistics.stdev(trial_sharpes) * (
        (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * math.e))
    )
    spread = math.sqrt(1 - skewness * pick + (kurtosis - 1) / 4 * pick**2)
    return sr0, normal.cdf((pick - sr0) * math.sqrt(len(returns) - 1) / spread)


def test_the_worked_numbers_are_the_docstrings() -> None:
    assert sharpe(PICK) == pytest.approx(0.69911, abs=5e-6)
    assert sharpe(SECOND) == pytest.approx(0.22556, abs=5e-6)
    assert sharpe(THIRD_CLOSE) == pytest.approx(0.22556, abs=5e-6)
    assert sharpe(THIRD_WIDE) == pytest.approx(-0.67668, abs=5e-6)
    sr0, dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_CLOSE)])
    assert (sr0, dsr) == pytest.approx((0.23316, 0.98849), abs=5e-6)
    sr0, dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_WIDE)])
    assert (sr0, dsr) == pytest.approx((0.59606, 0.69241), abs=5e-6)


def test_a_pick_above_the_threshold_passes(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    report = rank(study)

    sharpes = [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_CLOSE)]
    _, dsr = worked_dsr(PICK, sharpes)
    assert report.pick == "pick"
    assert (report.n, report.t) == (3, T)
    assert report.v == pytest.approx(statistics.variance(sharpes), rel=1e-12)
    assert report.sharpe == pytest.approx(sharpe(PICK), rel=1e-12)
    assert report.dsr == pytest.approx(dsr, abs=1e-12)
    assert report.dsr == pytest.approx(0.98849, abs=5e-6)
    assert report.passed and report.dsr >= THRESHOLD
    rho = statistics.fmean(
        statistics.correlation(a, b)
        for a, b in itertools.permutations([PICK, SECOND, THIRD_CLOSE], 2)
    )
    assert report.n_hat == pytest.approx(rho + (1 - rho) * 3, rel=1e-9)
    assert report.n_hat == pytest.approx(2.7755, abs=5e-5)
    assert report.dsr_at_n_hat is not None and report.dsr_at_n_hat > report.dsr
    assert (report.first_day, report.last_day) == ("2025-03-11", "2025-04-09")
    assert (report.records, report.first_trial, report.last_trial) == (3, 0, 2)
    summary = report.summary()
    assert "N = 3 configurations, T = 30 sessions" in summary
    assert "it passes at 0.95" in summary


def test_a_pick_below_the_threshold_is_rejected(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_WIDE)

    report = rank(study)

    _, dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_WIDE)])
    assert report.pick == "pick"
    assert report.dsr == pytest.approx(dsr, abs=1e-12)
    assert report.dsr == pytest.approx(0.69241, abs=5e-6)
    assert not report.passed
    assert "is rejected at 0.95" in report.summary()


def test_n_hat_is_not_reported_when_t_is_short(study: optuna.Study) -> None:
    """N = 4 needs T >= 6 for N-hat (p. 15); with T = 5 it is left out and the gate still runs."""
    for index, key in enumerate("abcd"):
        write(study, key, [PICK[i] * (1 - 0.2 * index) + 0.001 * index for i in range(5)])

    report = rank(study)

    assert (report.n, report.t) == (4, 5)
    assert report.n_hat is None and report.dsr_at_n_hat is None
    assert "N-hat not reported: T < N(N-1)/2 = 6" in report.summary()


def test_an_empty_study_stops(study: optuna.Study) -> None:
    with pytest.raises(RankingStopped, match="has no trials"):
        rank(study)


def test_a_failed_trial_stops_and_counts(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(study, "broken", None, outcome=FAILED)

    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*broken: no record ran to the end \(#{number} \(failed\)\)",
    ):
        rank(study)


def test_a_trial_left_running_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(study, "crashed", None, tell=False)

    with pytest.raises(
        RankingStopped, match=rf"crashed: no record ran to the end \(#{number} \(running\)\)"
    ):
        rank(study)


def test_a_later_run_of_the_same_configuration_resolves_a_failure(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", None, outcome=FAILED)
    write(study, "third", THIRD_CLOSE)

    report = rank(study)

    assert (report.n, report.records) == (3, 4)
    assert report.dsr == pytest.approx(0.98849, abs=5e-6)


def test_a_failure_after_a_run_of_the_same_configuration_stops(study: optuna.Study) -> None:
    """Only a later record resolves a failure: the configuration failed when it was last run."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    number = write(study, "third", None, outcome=FAILED)

    with pytest.raises(
        RankingStopped,
        match=rf"third: no later record ran to the end after #{number} \(failed\)",
    ):
        rank(study)


@pytest.mark.filterwarnings("ignore:invalid value encountered in scalar divide:RuntimeWarning")
def test_a_traded_record_with_no_finite_sharpe_ratio_ran_and_stops(study: optuna.Study) -> None:
    """Told FAIL by the ledger, still traded: it ran to the end, and ADR 0031 stops the gate on it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "flat", [0.0] * T, told_complete=False)

    with pytest.raises(RankingStopped, match="flat: Sharpe ratio nan"):
        rank(study)


def test_a_later_run_does_not_resolve_a_record_that_ran(study: optuna.Study) -> None:
    """A traded record with no finite Sharpe ratio is not a failure for a later run to resolve."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    flat = write(study, "flat", [0.0] * T, told_complete=False)
    again = write(study, "flat", THIRD_CLOSE)

    with pytest.raises(
        RankingStopped, match=rf"#{flat} \(traded\), #{again} \(traded\) ran the same"
    ):
        rank(study)


def test_an_idle_trial_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(study, "idle", [0.0] * T, outcome=IDLE)

    with pytest.raises(RankingStopped, match=rf"idle: never traded \(#{number} \(idle\)\)"):
        rank(study)


def test_two_records_of_one_configuration_that_disagree_stop(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    first = write(study, "second", SECOND)
    again = write(study, "second", [value + 0.001 for value in SECOND])

    with pytest.raises(
        RankingStopped, match=rf"#{first} \(traded\), #{again} \(traded\) ran the same"
    ):
        rank(study)


def test_a_rejected_series_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    trial = study.ask()
    trial.set_user_attr("config_key", "fallback")
    trial.set_user_attr(OUTCOME, TRADED)
    trial.set_user_attr(
        "returns_series", {str(DAY_0 + 3_600 * 10**9): 0.01, str(DAY_0 + DAY_NS): 0.02}
    )
    trial.set_user_attr("session_days", [str(DAY_0), str(DAY_0 + DAY_NS)])
    study.tell(trial, 0.0)

    with pytest.raises(
        RankingStopped, match="fallback: returns_series has keys off a UTC midnight"
    ):
        rank(study)


def test_one_configuration_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "pick", PICK)

    with pytest.raises(RankingStopped, match="N = 1; the DSR needs N >= 2"):
        rank(study)


@pytest.mark.filterwarnings("ignore:invalid value encountered in scalar divide:RuntimeWarning")
def test_a_sharpe_ratio_that_is_not_finite_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "flat", [0.0] * T)

    with pytest.raises(RankingStopped, match="flat: Sharpe ratio nan"):
        rank(study)


def test_a_return_that_is_not_finite_stops(study: optuna.Study) -> None:
    write(study, "pick", [math.inf, *PICK[1:]])
    write(study, "second", SECOND)

    with pytest.raises(RankingStopped, match="pick: return inf on 2025-03-11 is not finite"):
        rank(study)


def test_no_spread_between_trials_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "copy", PICK)

    with pytest.raises(RankingStopped, match="V = 0.0"):
        rank(study)


def test_a_shared_highest_sharpe_ratio_stops(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "copy", PICK)
    write(study, "second", SECOND)

    with pytest.raises(RankingStopped, match="pick, copy share the highest Sharpe ratio"):
        rank(study)


def test_every_reason_is_named_at_once(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "broken", None, outcome=FAILED)
    write(study, "idle", [0.0] * T, outcome=IDLE)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    assert "broken: no record ran to the end" in str(stopped.value)
    assert "idle: never traded" in str(stopped.value)
