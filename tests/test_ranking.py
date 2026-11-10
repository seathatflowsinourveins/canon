"""The gate of ADR 0010 step 3 and ADR 0031, on a ledger study: its refusals, one by one, and two picks.

No test here runs a backtest, opens a network connection or reads a credential. Each writes trials to
an Optuna SQLite study in pytest's temporary directory, in the form `canon_trading.ledger.record_trial`
leaves them (the user attributes of ADR 0035 term 3); `tests/test_ledger.py` shows that form on real
runs. Every day is a session day, from Tuesday 2025-03-11, so the matrix is the series as written. The
study's sample (ADR 0041) is those T = 30 days.

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
from skfolio import Portfolio
from skfolio import measures

from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import create_ledger_study
from canon_trading.ledger import mark_not_run
from canon_trading.ledger import move_sample
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
    with create_ledger_study(
        "gate", start=DAY_0, end=DAY_0 + (T - 1) * DAY_NS, path=tmp_path / "trials.db"
    ) as opened:
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
    sample: int = 0,
) -> int:
    """Record one trial as the ledger does; `returns` start at day `first_day`."""
    trial = study.ask()
    if key is not None:
        trial.set_user_attr("config_key", key)
        trial.set_user_attr("sample", sample)
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
    trial.set_user_attr("data_digest", {f"bars of {key}": "digest"})
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
    assert "N = 3 configurations over every sample, T = 30 sessions" in summary
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
    trial.set_user_attr("sample", 0)
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

    with pytest.raises(RankingStopped, match=r"N = 1; the DSR needs N >= 2 \(pick \(#0, #1\)\)"):
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


@pytest.mark.parametrize("scale", [0.97, 3.0, 1 / 7])
def test_copies_that_differ_only_in_rounding_stop(study: optuna.Study, scale: float) -> None:
    """The review: one configuration at two scales gave V = 1.1e-27 and passed undeflated."""
    copy = [value * scale for value in PICK]
    sharpes = {
        float(Portfolio(X=[[v] for v in x], weights=[1.0]).sharpe_ratio) for x in (PICK, copy)
    }
    assert len(sharpes) == 2  # not bit-identical: an exact test of V = 0 would let this pass
    write(study, "pick", PICK)
    write(study, "copy", copy)

    with pytest.raises(RankingStopped, match="within floating-point noise"):
        rank(study)


def test_bit_identical_columns_tied_at_the_top_are_one_pick(study: optuna.Study) -> None:
    """The review's 4x4 grid: (2,6) and (3,6) traded the same bars under two keys and stopped it."""
    first = write(study, "pick", PICK)
    twin = write(study, "twin", PICK)
    write(study, "second", SECOND)

    report = rank(study)

    assert (report.n, report.pick, report.tied, report.pick_trials) == (
        3,
        "pick",
        ("twin",),
        (first, twin),
    )
    sr0_dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(PICK), sharpe(SECOND)])
    assert report.dsr == pytest.approx(sr0_dsr[1], abs=1e-12)
    assert "Pick pick, the same returns as twin" in report.summary()


def test_a_tie_at_the_top_with_different_returns_stops(study: optuna.Study) -> None:
    """The pick's returns in reverse order: the same Sharpe ratio, other returns, no one pick."""
    first = write(study, "pick", PICK)
    other = write(study, "reversed", PICK[::-1])
    write(study, "second", SECOND)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert "share the highest Sharpe ratio within floating-point noise" in message
    assert f"pick (#{first}); reversed (#{other})" in message


@pytest.mark.filterwarnings("ignore:invalid value encountered in scalar divide:RuntimeWarning")
def test_every_reason_is_named_at_once(study: optuna.Study) -> None:
    """The review needed three calls for a failed key, a flat key and one on a disjoint range."""
    write(study, "pick", PICK)
    broken = write(study, "broken", None, outcome=FAILED)
    idle = write(study, "idle", [0.0] * T, outcome=IDLE)
    flat = write(study, "flat", [0.0] * T)
    short = write(study, "short", SECOND[20:], first_day=20)
    keyless = write(study, None, None, tell=False)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert f"broken: no record ran to the end (#{broken} (failed))" in message
    assert f"idle: never traded (#{idle} (idle))" in message
    assert f"flat: Sharpe ratio nan over the sample (#{flat})" in message
    assert f"short (#{short})" in message
    assert f"#{keyless} (running) has no config key" in message


def test_a_record_with_no_key_stops_until_marked_not_run(study: optuna.Study) -> None:
    """A process killed between ask and the key's write: no backtest ran, and N must not count it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    keyless = write(study, None, None, tell=False)

    with pytest.raises(RankingStopped, match=rf"mark_not_run\(study, {keyless}\) clears it"):
        rank(study)
    mark_not_run(study, keyless)

    report = rank(study)
    assert report.n == 2
    [trial] = [t for t in study.trials if t.number == keyless]
    assert trial.state == TrialState.FAIL
    assert study.user_attrs["not_run"] == [keyless]


def test_a_record_with_a_key_cannot_be_marked_not_run(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    crashed = write(study, "crashed", None, tell=False)

    with pytest.raises(ValueError, match="only a later record of that key resolves it"):
        mark_not_run(study, crashed)
    with pytest.raises(LookupError, match="no trial #99"):
        mark_not_run(study, 99)

    assert "not_run" not in study.user_attrs
    assert [t.state for t in study.trials][-1] == TrialState.RUNNING


@pytest.mark.parametrize(
    ("returns", "days", "message"),
    [
        pytest.param({f"0{DAY_0}": 0.01}, [str(DAY_0)], "key '0", id="leading zero"),
        pytest.param({f"+{DAY_0}": 0.01}, [str(DAY_0)], r"key '\+", id="plus sign"),
        pytest.param({"2025-03-11": 0.01}, [str(DAY_0)], "key '2025-03-11'", id="date key"),
        pytest.param({str(DAY_0): "0.01"}, [str(DAY_0)], "value '0.01'", id="text return"),
        pytest.param({str(DAY_0): None}, [str(DAY_0)], "value None", id="null return"),
        pytest.param({str(DAY_0): 0.01}, None, "session_days is NoneType", id="null days"),
        pytest.param({str(DAY_0): 0.01}, ["2025-03-11"], "session day '2025-03-11'", id="date day"),
        pytest.param(None, [str(DAY_0)], "returns_series is NoneType", id="no returns"),
        pytest.param({"--5": 0.01}, [str(DAY_0)], "key '--5'", id="two minus signs"),
        pytest.param({"\u00b2": 0.01}, [str(DAY_0)], "key '\u00b2'", id="superscript digit"),
        pytest.param({str(DAY_0): 10**400}, [str(DAY_0)], "is not a number", id="huge integer"),
    ],
)
def test_a_record_not_in_the_writers_shape_stops_naming_it(
    study: optuna.Study, returns: object, days: object, message: str
) -> None:
    """Another writer's record raised a bare KeyError, ValueError or TypeError naming nothing."""
    write(study, "pick", PICK)
    trial = study.ask()
    trial.set_user_attr("config_key", "odd")
    trial.set_user_attr("sample", 0)
    if returns is not None:
        trial.set_user_attr("returns_series", returns)
    trial.set_user_attr("session_days", days)
    trial.set_user_attr(OUTCOME, TRADED)
    study.tell(trial, 0.0)

    with pytest.raises(RankingStopped, match=rf"odd: [^\n]*{message}[^\n]*\(#{trial.number}\)"):
        rank(study)


def test_a_trial_over_a_shorter_range_stops_and_is_named(study: optuna.Study) -> None:
    """The review: one trial on 116 of 250 days set T = 116 for all and turned a rejection to a pass."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    short = write(study, "short", THIRD_WIDE[18:], first_day=18)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert "every trial must cover the same session days over the sample" in message
    assert "short: no return on 18 of the 30 session days of the sample, the first 2025-03-11" in (
        message
    )
    assert f"short (#{short})" in message
    assert "pick" not in message.split("Trials:")[1]


def test_trials_with_no_day_in_common_are_each_named(study: optuna.Study) -> None:
    early = write(study, "early", PICK[:10])
    late = write(study, "late", SECOND[20:], first_day=20)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    assert f"early (#{early}), late (#{late})" in str(stopped.value)


def test_a_configuration_not_run_on_the_current_sample_stops(study: optuna.Study) -> None:
    """N counts every configuration the study tried; each must run on the current sample."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    old = write(study, "third", THIRD_CLOSE)
    move_sample(study, start=DAY_0, end=DAY_0 + (T - 1) * DAY_NS)
    write(study, "pick", PICK, sample=1)
    write(study, "second", SECOND, sample=1)

    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*third: tried \(#{old} \(traded\)\) but not run on the current "
        r"sample 1",
    ):
        rank(study)
    write(study, "third", THIRD_CLOSE, sample=1)
    report = rank(study)
    assert (report.n, report.records, report.all_records) == (3, 3, 6)
    assert report.pick_trials == (3,)
    assert report.dsr == pytest.approx(0.98849, abs=5e-6)


def test_affine_copies_report_n_hat_at_one_instead_of_raising(study: optuna.Study) -> None:
    """Perfectly correlated trials: on macOS arm64 rho rounded to 1.0000000000000002 and N-hat to
    0.9999999999999998, which skfolio refused with a bare ValueError after the gate's DSR was
    computed. How rho rounds depends on the platform's arithmetic, so the next test fixes it."""
    write(study, "pick", PICK)
    affine = [value * 0.5 + 0.001 for value in PICK]
    write(study, "affine", affine)

    report = rank(study)

    assert report.pick == "affine"
    assert report.n_hat == pytest.approx(1.0, abs=1e-12)
    assert report.n_hat is not None and report.n_hat >= 1.0
    # Just above 1, the benchmark falls below 0 and the DSR at N-hat is withheld; at 1 it is the PSR.
    undeflated = measures.probabilistic_sharpe_ratio(affine, 0.0)
    assert report.dsr_at_n_hat is None or report.dsr_at_n_hat <= undeflated + 1e-12


def test_a_mean_correlation_above_one_raises_n_hat_to_one(
    study: optuna.Study, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(study, "pick", PICK)
    write(study, "affine", [value * 0.5 + 0.001 for value in PICK])
    above_one = 1.0000000000000002
    monkeypatch.setattr(
        "canon_trading.ranking.measures.correlation",
        lambda _: [[1.0, above_one], [above_one, 1.0]],
    )

    report = rank(study)

    assert report.n_hat == 1.0
    assert report.n_hat_note is not None
    assert "N-hat 0.9999999999999998 raised to 1" in report.n_hat_note
    assert report.n_hat_note in report.summary()


def test_dsr_at_a_small_n_hat_is_withheld_not_inflated(study: optuna.Study) -> None:
    """N-hat 1.003 gave a benchmark below 0 and a "DSR at N-hat" above the undeflated value."""
    write(study, "pick", PICK)
    write(study, "near", [v * 0.9 + 0.0002 * math.sin(i) for i, v in enumerate(PICK)])
    write(study, "third", [v * 0.8 + 0.0003 * math.cos(i) for i, v in enumerate(PICK)])

    report = rank(study)

    assert report.n_hat is not None and 1 < report.n_hat < 1.28
    assert report.dsr_at_n_hat is None
    assert report.n_hat_note is not None and "withheld: its benchmark SR0" in report.n_hat_note
    assert report.n_hat_note in report.summary()


def test_an_empty_key_is_keyless_and_mark_not_run_clears_it(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    trial = study.ask()
    trial.set_user_attr("config_key", "")

    with pytest.raises(RankingStopped, match=rf"mark_not_run\(study, {trial.number}\)"):
        rank(study)
    mark_not_run(study, trial.number)
    assert rank(study).n == 2


def test_a_keyless_record_that_ran_cannot_be_marked_and_stops(study: optuna.Study) -> None:
    """Marking it would drop a trial that ran from N, and a smaller N inflates the DSR."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    trial = study.ask()
    trial.set_user_attr("returns_series", {str(DAY_0): 0.01})
    trial.set_user_attr(OUTCOME, TRADED)
    study.tell(trial, 0.0)

    with pytest.raises(ValueError, match="shows a run"):
        mark_not_run(study, trial.number)
    with pytest.raises(RankingStopped, match=rf"#{trial.number} \(complete\)[^\n]*shows a run"):
        rank(study)


def test_a_data_digest_not_in_the_writers_shape_stops_naming_it(study: optuna.Study) -> None:
    write(study, "pick", PICK)
    odd = write(study, "odd", SECOND)
    [trial] = [t for t in study.trials if t.number == odd]
    study.add_trial(
        optuna.trial.create_trial(
            state=TrialState.COMPLETE,
            value=0.0,
            user_attrs={**trial.user_attrs, "config_key": "other", "data_digest": {"BT": ["x"]}},
        )
    )

    with pytest.raises(RankingStopped, match=r"data_digest \{'BT': \['x'\]\} is not in the shape"):
        rank(study)


def test_returns_equal_but_for_rounding_around_zero_are_one_pick(study: optuna.Study) -> None:
    """One day's return is 0.0 in one column and 1e-19 in its twin: the same returns, one pick."""
    base = [0.0 if i == 5 else value for i, value in enumerate(PICK)]
    twin_returns = [1e-19 if i == 5 else value for i, value in enumerate(base)]
    first = write(study, "pick", base)
    twin = write(study, "twin", twin_returns)
    write(study, "second", SECOND)

    report = rank(study)

    assert {report.pick, *report.tied} == {"pick", "twin"}
    assert report.pick_trials == (first, twin)
