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

import dataclasses
import hashlib
import itertools
import math
import re
import statistics
import time
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

import numpy as np
import optuna
import pytest
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity
from optuna.study import StudyDirection
from optuna.trial import TrialState
from skfolio import Portfolio
from skfolio import measures

import canon_trading.ledger
from canon_trading import ranking
from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import config_key
from canon_trading.ledger import create_ledger_study
from canon_trading.ledger import current_sample
from canon_trading.ledger import mark_not_run
from canon_trading.ledger import move_sample
from canon_trading.ledger import open_study
from canon_trading.ledger import told_sharpe
from canon_trading.ma_cross import MaCrossConfig
from canon_trading.ranking import SHA256_HEX
from canon_trading.ranking import THRESHOLD
from canon_trading.ranking import RankingReport
from canon_trading.ranking import RankingStopped

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import AlpacaBars

optuna.logging.set_verbosity(optuna.logging.WARNING)

DAY_0 = 1_741_651_200 * 1_000_000_000  # 2025-03-11T00:00:00Z
T = 30
PATTERN = [0.01, -0.004, 0.006, 0.002, -0.003, 0.008, 0.004, -0.001, 0.005, 0.003]
PICK = [PATTERN[i % 10] + 0.0001 * (i % 3) for i in range(T)]
SECOND = [PATTERN[(i + 3) % 10] * 0.5 - 0.001 for i in range(T)]
THIRD_CLOSE = [PATTERN[(i + 5) % 10] * 0.6 - 0.0012 for i in range(T)]
THIRD_WIDE = [-PATTERN[(i + 5) % 10] * 0.8 for i in range(T)]
# The sample's end, the midnight after its last day: its T session days are DAY_0 to DAY_0 + T - 1,
# the way a real run books a sample that ends at a midnight (`canon_trading.ledger.session_day`).
END = DAY_0 + T * DAY_NS
# The sample's last session day, which every record `record_trial` writes on it holds.
LAST_DAY = str(DAY_0 + (T - 1) * DAY_NS)


@pytest.fixture
def study(tmp_path: Path) -> Iterator[optuna.Study]:
    with create_ledger_study("gate", start=DAY_0, end=END, path=tmp_path / "trials.db") as opened:
        yield opened


def bars_of(label: str) -> str:
    """The bar type of the configuration named `label`, on an instrument of its own."""
    return f"{label.upper()}.ALPACA-1-DAY-LAST-EXTERNAL"


def instrument_of(label: str) -> str:
    return f"{label.upper()}.ALPACA"


def config_of(label: str) -> dict[str, object]:
    """A stored config for the configuration named `label`, in `record_trial`'s three parts, with only
    the data and the bar type its strategy trades filled in as the gate reads them."""
    return {
        "strategies": [{"label": label, "config": {"bar_type": bars_of(label)}}],
        "venue": {},
        "data": {"data_type": "Bar", "bar_types": [bars_of(label)]},
    }


def digest_of(label: str) -> dict[str, str]:
    """A data digest as `record_trial` writes one for `config_of(label)`: its bars and instrument."""
    return {
        bars_of(label): hashlib.sha256(f"bars {label}".encode()).hexdigest(),
        instrument_of(label): hashlib.sha256(f"instrument {label}".encode()).hexdigest(),
    }


# Each key `key_of` made, mapped back to its label, so a message reads "pick: ..." for the test.
LABELS: dict[str, str] = {}


def key_of(label: str) -> str:
    """The key `record_trial` would write for `config_of(label)`: the hash of the config it stores."""
    key = config_key(config_of(label))
    LABELS[key] = label
    return key


def labelled(text: str) -> str:
    """`text` with each key `key_of` made shown as its label (the keys are 64 hex digits, never a
    word, so no other text is touched)."""
    return SHA256_HEX.sub(lambda found: LABELS.get(found.group(), found.group()), text)


def rank(study: optuna.Study) -> RankingReport:
    """`canon_trading.ranking.rank`, its keys shown as the labels the tests wrote them under."""
    try:
        report = ranking.rank(study)
    except RankingStopped as stopped:
        raise RankingStopped(labelled(str(stopped))) from stopped
    return dataclasses.replace(
        report, pick=labelled(report.pick), tied=tuple(map(labelled, report.tied))
    )


def write(
    study: optuna.Study,
    label: str | None,
    returns: list[float] | None,
    *,
    outcome: str = TRADED,
    first_day: int = 0,
    tell: bool = True,
    told_complete: bool = True,
    sample: object = 0,
    extra_session_days: int = 0,
    attrs: dict[str, object] | None = None,
    drop: tuple[str, ...] = (),
    also: tuple[float, ...] = (),
) -> int:
    """Record one trial of the configuration `label` as the ledger does; `returns` start at day `first_day`.

    Its key is the hash of the config it stores, `config_of(label)` or the one `attrs` gives, shown
    as `label` in what `rank` says, and the run's bounds are the current sample's, as `record_trial`
    writes them. `attrs` then replaces attributes and `drop` leaves some out, for a record not in that
    shape; a `config_key` in `attrs` replaces the key, for a record whose config is not its key's.
    `also` adds values to the one told, for a study with more than one objective.
    """
    key = None if label is None else key_of(label)
    stored = (attrs or {}).get("config")
    if (
        label is not None
        and isinstance(stored, dict)
        and "config" not in drop
        and "config_key" not in (attrs or {})
    ):
        key = config_key(stored)
        LABELS[key] = label
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
    calendar = days + [days[-1] + n * DAY_NS for n in range(1, extra_session_days + 1)]
    bounds = current_sample(study)
    written: dict[str, object] = {
        "run_config_id": key,
        "backtest_start": bounds.start,
        "backtest_end": bounds.end,
        "returns_series": {str(d): value for d, value in zip(days, returns or [], strict=True)},
        "session_days": [str(d) for d in calendar],
        "data_digest": digest_of(str(label)),
        "config": config_of(str(label)),
    }
    written.update(attrs or {})
    for name, value in written.items():
        if name not in drop:
            trial.set_user_attr(name, value)
    trial.set_user_attr(OUTCOME, outcome)
    with np.errstate(divide="ignore", invalid="ignore"):  # constant returns: NaN, told FAIL
        value = told_sharpe(written.get("returns_series"), written.get("session_days"))
    if outcome == IDLE or not told_complete or not math.isfinite(value):
        study.tell(trial, state=TrialState.FAIL)  # as `record_trial` tells a ratio not finite
    else:
        study.tell(trial, [value, *also] if also else value)
    return trial.number


def ma_cross_config() -> MaCrossConfig:
    """A configuration `record_trial` can check against `conftest.AlpacaBars`' catalog."""
    return MaCrossConfig(
        bar_type=BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL"),
        trade_size=Quantity.from_int(10),
        fast_period=3,
        slow_period=8,
    )


def sharpe(values: list[float]) -> float:
    return statistics.fmean(values) / statistics.stdev(values)


def test_the_worked_numbers_are_the_docstrings(
    worked_dsr: Callable[..., tuple[float, float]],
) -> None:
    assert sharpe(PICK) == pytest.approx(0.69911, abs=5e-6)
    assert sharpe(SECOND) == pytest.approx(0.22556, abs=5e-6)
    assert sharpe(THIRD_CLOSE) == pytest.approx(0.22556, abs=5e-6)
    assert sharpe(THIRD_WIDE) == pytest.approx(-0.67668, abs=5e-6)
    sr0, dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_CLOSE)])
    assert (sr0, dsr) == pytest.approx((0.23316, 0.98849), abs=5e-6)
    sr0, dsr = worked_dsr(PICK, [sharpe(PICK), sharpe(SECOND), sharpe(THIRD_WIDE)])
    assert (sr0, dsr) == pytest.approx((0.59606, 0.69241), abs=5e-6)


def test_a_pick_above_the_threshold_passes(
    worked_dsr: Callable[..., tuple[float, float]], study: optuna.Study
) -> None:
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
    # Eqs. 1-2 by hand at N-hat: fails with a population deviation, half N-hat or no benchmark.
    _, at_n_hat = worked_dsr(PICK, sharpes, report.n_hat)
    assert report.dsr_at_n_hat == pytest.approx(at_n_hat, abs=1e-12)
    assert report.dsr_at_n_hat == pytest.approx(0.99063, abs=5e-6)
    assert (report.first_day, report.last_day) == ("2025-03-11", "2025-04-09")
    assert (report.records, report.first_trial, report.last_trial) == (3, 0, 2)
    summary = report.summary()
    assert "N = 3 configurations over every sample, T = 30 sessions" in summary
    assert "it passes at 0.95" in summary


def test_a_pick_below_the_threshold_is_rejected(
    worked_dsr: Callable[..., tuple[float, float]], study: optuna.Study
) -> None:
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


def test_n_hat_is_not_reported_when_t_is_short(tmp_path: Path) -> None:
    """N = 4 needs T >= 6 for N-hat (p. 15); with T = 5 it is left out and the gate still runs."""
    with create_ledger_study(
        "short", start=DAY_0, end=DAY_0 + 5 * DAY_NS, path=tmp_path / "short.db"
    ) as study:
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
    write(
        study,
        "fallback",
        PICK,
        attrs={
            "returns_series": {str(DAY_0 + 3_600 * 10**9): 0.01, str(DAY_0 + DAY_NS): 0.02},
            "session_days": [str(DAY_0), str(DAY_0 + DAY_NS), LAST_DAY],
        },
    )

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


def test_bit_identical_columns_tied_at_the_top_are_one_pick(
    worked_dsr: Callable[..., tuple[float, float]], study: optuna.Study
) -> None:
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
        pytest.param({str(DAY_0): 10**400}, [str(DAY_0)], "is not a float", id="huge integer"),
    ],
)
def test_a_record_not_in_the_writers_shape_stops_naming_it(
    study: optuna.Study, returns: object, days: object, message: str
) -> None:
    """Another writer's record raised a bare KeyError, ValueError or TypeError naming nothing."""
    write(study, "pick", PICK)
    number = write(
        study,
        "odd",
        PICK,
        # With the sample's last day, which `_shape_faults` asks for before the form is read.
        attrs={
            "returns_series": returns,
            "session_days": [*days, LAST_DAY] if isinstance(days, list) else days,
        },
        drop=("returns_series",) if returns is None else (),
    )

    with pytest.raises(RankingStopped, match=rf"odd: [^\n]*{message}[^\n]*\(#{number}\)"):
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


def test_trials_that_each_lack_days_the_other_has_are_each_named(study: optuna.Study) -> None:
    """Each holds the sample's last day, as `record_trial` writes every record (`early` no return
    on it), and lacks days the other has."""
    early = write(
        study,
        "early",
        [*PICK[:10], *[0.0] * (T - 10)],
        attrs={"session_days": [*(str(DAY_0 + n * DAY_NS) for n in range(10)), LAST_DAY]},
    )
    late = write(study, "late", SECOND[20:], first_day=20)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    assert f"early (#{early}), late (#{late})" in str(stopped.value)


def test_a_configuration_not_run_on_the_current_sample_stops(study: optuna.Study) -> None:
    """N counts every configuration the study tried; each must run on the current sample."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    old = write(study, "third", THIRD_CLOSE)
    move_sample(study, start=DAY_0, end=END)
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


@pytest.mark.parametrize(
    "how", ["queued and started", "an empty key", "a key not text", "an attribute of its own"]
)
def test_a_keyless_record_another_writer_started_is_not_cleared(
    study: optuna.Study, how: str
) -> None:
    """Antigravity, gate round after #172: a keyless record another writer queued
    (`Study.enqueue_trial`, which writes `fixed_params`) or wrote attributes on, then started and
    told `FAIL` before any mark, was read as `record_trial`'s own record lost before its key:
    `mark_not_run` cleared it, it left N, and the gate passed at N = 3, DSR 0.9974, though it may have
    run anything. `record_trial` writes nothing on a keyless record but its error, so such a record is
    another writer's, and nothing clears it; a mark the code before wrote clears it no more."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    if how == "queued and started":
        study.enqueue_trial({"fast": 6, "slow": 11})
    other = study.ask()
    if how == "an empty key":
        other.set_user_attr("config_key", "")
    elif how == "a key not text":
        other.set_user_attr("config_key", 123)
    elif how == "an attribute of its own":
        other.set_user_attr("strategy", "custom_ma_6_11")
    study.tell(other, state=TrialState.FAIL)
    number = other.number
    stop = rf"#{number} \(fail\) [^\n]*another writer's record, which may have run anything"

    with pytest.raises(RankingStopped, match=stop) as stopped:
        rank(study)
    [line] = [line for line in str(stopped.value).splitlines() if f"#{number} " in line]
    assert "mark_not_run" not in line
    with pytest.raises(ValueError, match="another writer's record, which may have run anything"):
        mark_not_run(study, number)
    assert "not_run" not in study.user_attrs

    study.set_user_attr("not_run", [number])  # as `mark_not_run` wrote it before this fix
    with pytest.raises(RankingStopped, match=stop):
        rank(study)


def test_a_keyless_queued_record_record_trial_took_is_still_cleared(study: optuna.Study) -> None:
    """The other side of the rule above: a keyless queued record `record_trial` took from the queue
    holds the other writer's attributes beside its note, and ran nothing here, so `mark_not_run`
    still clears it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.enqueue_trial({"fast": 6}, user_attrs={"strategy": "custom"})
    taken = study.ask()
    taken.set_user_attr("error", canon_trading.ledger.TAKEN_FROM_QUEUE)
    study.tell(taken, state=TrialState.FAIL)

    mark_not_run(study, taken.number)

    assert study.user_attrs["not_run"] == [taken.number]
    assert rank(study).n == 3


@pytest.mark.parametrize("number", [False, True, 0.0, 1.0])
def test_a_trial_number_that_is_not_an_int_is_refused(study: optuna.Study, number: Any) -> None:
    """Antigravity, gate round after #172: `False == 0 == 0.0`, so `mark_not_run` and `retire_queued`
    found trial #0 or #1 by a `bool` or a float and wrote it into their lists as given; every reader
    refuses such a list, so a later `retired` list of `[0, True]` read neither as retired, the
    ranking stopped for good and `record_trial` was locked out. Both now refuse the number first."""
    study.enqueue_trial({}, user_attrs={"config_key": key_of("pick"), "sample": 0})
    study.enqueue_trial({}, user_attrs={"config_key": key_of("second"), "sample": 0})

    with pytest.raises(TypeError, match="a trial number is an int"):
        canon_trading.ledger.retire_queued(study, number)
    with pytest.raises(TypeError, match="a trial number is an int"):
        mark_not_run(study, number)

    assert not {"retired", "not_run", "not_run_waiting"} & set(study.user_attrs)
    assert [t.state for t in study.trials] == [TrialState.WAITING, TrialState.WAITING]
    canon_trading.ledger.retire_queued(study, 0)
    canon_trading.ledger.retire_queued(study, 1)
    assert study.user_attrs["retired"] == [0, 1]


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
            user_attrs={
                **trial.user_attrs,
                "config_key": key_of("other"),
                "run_config_id": key_of("other"),
                "config": config_of("other"),
                "data_digest": {"BT": ["x"]},
            },
        )
    )

    with pytest.raises(
        RankingStopped,
        match=r"other: #\d+ \(traded\) is not in the shape[^\n]*data_digest \{'BT': \['x'\]\}",
    ):
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


def test_a_dsr_that_is_not_finite_stops_naming_the_pick(
    study: optuna.Study, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = write(study, "pick", PICK)
    write(study, "second", SECOND)
    monkeypatch.setattr(
        "canon_trading.ranking.measures.deflated_sharpe_ratio", lambda *_: float("nan")
    )

    with pytest.raises(
        RankingStopped, match=rf"skfolio gives DSR nan for the pick pick \(#{first}\)"
    ):
        rank(study)


def test_the_recorded_range_is_utc_whatever_the_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optuna returns a trial's start as naive local time, so the review saw three ranges in three
    zones; the report gives one, in UTC, with its offset."""
    before = datetime.now(UTC)
    with create_ledger_study("times", start=DAY_0, end=END, path=tmp_path / "trials.db") as written:
        write(written, "pick", PICK)
        write(written, "second", SECOND)
    after = datetime.now(UTC)
    seen = set()
    try:
        for zone in ("Asia/Tokyo", "UTC", "America/New_York"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            with open_study("times", tmp_path / "trials.db") as reopened:
                report = rank(reopened)
            seen.add((report.first_recorded, report.last_recorded))
    finally:
        monkeypatch.undo()
        time.tzset()

    [(first, last)] = seen
    assert first.endswith("+00:00") and last.endswith("+00:00")
    assert before <= datetime.fromisoformat(first) <= datetime.fromisoformat(last) <= after


def test_session_days_with_no_return_stop_and_are_named(study: optuna.Study) -> None:
    """A later review of 2026-11-10: three trials recording 31 session days with 30 returns each
    ranked at T = 30, the missing day dropped from the calendar. Here 30 session days, 29 returns."""
    numbers = [
        write(study, key, returns[:-1], extra_session_days=1)
        for key, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE))
    ]

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    for key, number in zip(("pick", "second", "third"), numbers, strict=True):
        assert f"{key}: no return on 1 of its 30 session days" in message
        assert f"(#{number})" in message


@pytest.mark.parametrize("number", [7, "0", True], ids=["unknown", "text", "bool"])
def test_a_record_that_ran_with_no_sample_number_of_the_study_stops(
    study: optuna.Study, number: object
) -> None:
    """Read as on the current sample only if it did not run: one that ran came from another writer."""
    trial = write(study, "pick", PICK, sample=number)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    assert f"#{trial} (traded) ran with sample {number!r}, not a sample number" in str(
        stopped.value
    )


# The third review of the gate, 2026-11-10: each test failed before its fix.


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"first_day": -365},
            "returns_series has a key on 2024-03-11, outside sample 0",
            id="prior-year returns",
        ),
        pytest.param({"drop": ("backtest_start",)}, "backtest_start None", id="no start"),
        pytest.param(
            {"attrs": {"backtest_start": DAY_0 + DAY_NS}},
            f"backtest_start {DAY_0 + DAY_NS}, not the start of sample 0",
            id="other start",
        ),
        pytest.param({"drop": ("backtest_end",)}, "backtest_end None", id="no end"),
        pytest.param(
            {"attrs": {"backtest_end": END + 1}},
            f"backtest_end {END + 1}, not within sample 0",
            id="end past the sample",
        ),
        pytest.param({"attrs": {"data_digest": {}}}, "data_digest {}", id="empty digest"),
        pytest.param(
            {"attrs": {"data_digest": {"BT": ""}}}, "data_digest {'BT': ''}", id="empty hash"
        ),
        pytest.param(
            {"attrs": {"run_config_id": "elsewhere"}},
            "run_config_id 'elsewhere', not its config key",
            id="other run id",
        ),
    ],
)
def test_a_record_that_ran_out_of_the_writers_shape_stops_naming_it(
    study: optuna.Study, change: dict[str, Any], message: str
) -> None:
    """Three records of prior-year returns, missing run bounds or empty digests on the declared
    sample passed at DSR 0.98849, the shape of the returns being all that was checked."""
    numbers = [
        write(study, key, returns, **change)
        for key, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE))
    ]

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    text = str(stopped.value)
    for key, number in zip(("pick", "second", "third"), numbers, strict=True):
        assert re.search(rf"{key}: #{number} [^\n]*{re.escape(message)}", text), text
        assert "not in the shape record_trial writes" in text


def test_a_keyless_record_told_a_value_cannot_be_marked_and_stops(study: optuna.Study) -> None:
    """An Optuna writer told a result with none of the ledger's attributes; marking it dropped a
    trial that ran from N, and the gate passed at N = 3 with four completed trials."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    trial = study.ask()
    study.tell(trial, 1.5)

    with pytest.raises(ValueError, match="shows a run"):
        mark_not_run(study, trial.number)
    assert "not_run" not in study.user_attrs
    with pytest.raises(RankingStopped, match=rf"#{trial.number} \(complete\)[^\n]*shows a run"):
        rank(study)


@pytest.mark.parametrize("idle_first", [False, True], ids=["traded first", "idle first"])
def test_records_of_one_key_that_differ_in_outcome_stop_whatever_their_order(
    study: optuna.Study, idle_first: bool
) -> None:
    """One key, a traded and an idle record alike in all else: the answer hung on record order."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    outcomes = [IDLE, TRADED] if idle_first else [TRADED, IDLE]
    first, again = (write(study, "pick", PICK, outcome=outcome) for outcome in outcomes)

    with pytest.raises(
        RankingStopped,
        match=rf"pick: records #{first} \({outcomes[0]}\), #{again} \({outcomes[1]}\) ran the "
        r"same configuration[^\n]*disagree on their outcome",
    ):
        rank(study)


def test_the_pick_s_trials_are_only_records_that_ran(study: optuna.Study) -> None:
    """The report credited the pick with a failed record and one left running."""
    failed = write(study, "pick", None, outcome=FAILED)
    ran = write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    report = rank(study)

    assert failed not in report.pick_trials
    assert report.pick_trials == (ran,)
    assert f"(trials {ran})" in report.summary()


# The fourth review of the gate, 2026-11-10: each test failed before its fix.


@pytest.mark.parametrize("result", ["complete", "pruned", "intermediate value"])
def test_a_keyed_record_with_a_result_and_no_outcome_stops_for_good(
    study: optuna.Study, result: str
) -> None:
    """Another writer's completed record of a key was read as a failure a later run resolves, so a
    traded record of that key passed and the completed one was never checked."""
    trial = study.ask()
    trial.set_user_attr("config_key", key_of("pick"))  # the pick's own key, so a resolve would pass
    trial.set_user_attr("sample", 0)
    if result == "complete":
        study.tell(trial, 1.5)
    elif result == "pruned":
        study.tell(trial, state=TrialState.PRUNED)
    else:
        trial.report(1.5, step=0)
        study.tell(trial, state=TrialState.FAIL)
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    assert "(N = 3)" in str(stopped.value), stopped.value  # one key with the pick's records
    assert re.search(
        rf"pick: #{trial.number} \([a-z]+\) (shows a result Optuna keeps but no outcome "
        r"record_trial writes|holds an intermediate value or a parameter)",
        str(stopped.value),
    ), stopped.value


def test_returns_after_the_run_s_end_stop(study: optuna.Study) -> None:
    """A record whose run ended at the sample's start carried 29 more days of returns and passed:
    its returns and session days were checked against the sample, not against the run."""
    numbers = [
        write(study, key, returns, attrs={"backtest_end": DAY_0 + DAY_NS})
        for key, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE))
    ]

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    text = str(stopped.value)
    for key, number in zip(("pick", "second", "third"), numbers, strict=True):
        assert re.search(
            rf"{key}: #{number} [^\n]*returns_series has a key on 2025-03-12, after its "
            r"backtest_end \(29 in all\)",
            text,
        ), text


@pytest.mark.parametrize(
    ("days", "message"),
    [
        pytest.param([str(DAY_0), None], "session day None is not nanoseconds", id="null day"),
        pytest.param(
            [str(DAY_0), "1" + "0" * 30],
            "session day '1000000000000000000000000000000' is not nanoseconds",
            id="31-digit day",
        ),
        pytest.param([str(DAY_0), "1" * 5000], "is not nanoseconds as text", id="5000-digit day"),
        pytest.param([str(DAY_0), str(2**63)], f"session day '{2**63}' is not", id="past int64"),
        pytest.param([str(DAY_0), "-5"], "session day '-5' is not", id="negative"),
    ],
)
def test_a_session_day_that_crashed_the_ranking_stops_naming_it(
    study: optuna.Study, days: list[object], message: str
) -> None:
    """A JSON null in a list of days raised TypeError, a 31-digit day OverflowError, and one of
    5,000 digits ValueError; a day past a 64-bit count of nanoseconds names no instant."""
    write(study, "pick", PICK)
    number = write(study, "odd", PICK[:1], attrs={"session_days": [*days, LAST_DAY]})

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    [line] = [line for line in str(stopped.value).splitlines() if line.startswith("* odd:")]
    assert message in line and f"#{number}" in line, line


def test_a_keyless_record_with_a_failed_outcome_shows_a_run(study: optuna.Study) -> None:
    """ADR 0053 term 3: an outcome shows a run, and `failed` is an outcome. Marking it dropped a
    trial of an unknown configuration from N, and the gate passed at N = 3."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    trial = study.ask()
    trial.set_user_attr(OUTCOME, FAILED)
    study.tell(trial, state=TrialState.FAIL)

    with pytest.raises(ValueError, match="shows a run"):
        mark_not_run(study, trial.number)
    assert "not_run" not in study.user_attrs
    with pytest.raises(RankingStopped, match=rf"#{trial.number} \(fail\)[^\n]*shows a run"):
        rank(study)


def test_a_keyless_waiting_record_can_be_marked_not_run(study: optuna.Study) -> None:
    """Optuna refuses to tell a WAITING trial, so the remedy the stop names always raised."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.enqueue_trial({})
    [waiting] = [t.number for t in study.trials if t.state == TrialState.WAITING]

    with pytest.raises(RankingStopped, match=rf"mark_not_run\(study, {waiting}\) clears it"):
        rank(study)
    mark_not_run(study, waiting)

    [trial] = [t for t in study.trials if t.number == waiting]
    assert trial.state == TrialState.WAITING  # Optuna tells only a RUNNING trial
    assert study.user_attrs["not_run_waiting"] == [waiting]
    assert "not_run" not in study.user_attrs
    assert rank(study).n == 3

    # Optuna's next `ask` takes the queued trial; started, it is no longer cleared by its mark, and
    # it is not marked again: whoever started it may have run anything in it.
    assert study.ask().number == waiting
    with pytest.raises(
        RankingStopped, match=rf"#{waiting} \(running\) [^\n]*started after its mark"
    ):
        rank(study)
    with pytest.raises(ValueError, match="started after its mark"):
        mark_not_run(study, waiting)


def test_a_trial_is_marked_not_run_only_on_a_study_the_ledger_opened(tmp_path: Path) -> None:
    """The mark holds the ledger's lock, which only a study the ledger opened can name."""
    bare = optuna.create_study(
        study_name="bare", storage=f"sqlite:///{tmp_path / 'bare.db'}", direction="maximize"
    )
    bare.ask()

    with pytest.raises(ValueError, match="open_study"):
        mark_not_run(bare, 0)
    assert "not_run" not in bare.user_attrs


@pytest.mark.parametrize(
    ("state", "outcome", "value"),
    [
        pytest.param(TrialState.PRUNED, TRADED, None, id="pruned, traded"),
        pytest.param(TrialState.COMPLETE, IDLE, 0.5, id="complete, idle"),
    ],
)
def test_a_record_that_ran_in_a_state_the_writer_never_tells_stops(
    study: optuna.Study, state: TrialState, outcome: str, value: float | None
) -> None:
    """A keyed record that ran to the end in a state record_trial never leaves entered the matrix."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    trial = study.ask()
    for name, attr in {
        "config_key": "pick",
        "sample": 0,
        "run_config_id": "pick",
        "backtest_start": DAY_0,
        "backtest_end": END,
        "returns_series": {str(DAY_0 + n * DAY_NS): r for n, r in enumerate(PICK)},
        "session_days": [str(DAY_0 + n * DAY_NS) for n in range(T)],
        "data_digest": digest_of("pick"),
        OUTCOME: outcome,
    }.items():
        trial.set_user_attr(name, attr)
    study.tell(trial, value, state=state) if value is not None else study.tell(trial, state=state)

    with pytest.raises(RankingStopped, match=rf"pick: #{trial.number} [^\n]*was told {state.name}"):
        rank(study)


def test_a_keyless_record_with_a_suggested_parameter_shows_a_run(study: optuna.Study) -> None:
    """Study.optimize tells FAIL, with no value, an objective that returned NaN; its parameter
    shows the evaluation, and marking it dropped a tried configuration from N."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.optimize(lambda trial: trial.suggest_int("fast", 2, 9) * math.nan, n_trials=1)
    [tried] = [t.number for t in study.trials if t.params]

    with pytest.raises(ValueError, match="shows a run"):
        mark_not_run(study, tried)
    with pytest.raises(RankingStopped, match=rf"#{tried} \(fail\)[^\n]*shows a run"):
        rank(study)


@pytest.mark.parametrize("marked", [[0, True], None, [None], "0"])
def test_a_not_run_list_not_in_the_writers_shape_stops(study: optuna.Study, marked: object) -> None:
    """`True` cleared trial #1, and `None` raised a bare TypeError."""
    keyless = write(study, None, None, tell=False)
    second = study.ask()  # #1, keyless and RUNNING
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.set_user_attr("not_run", marked)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    text = str(stopped.value)
    assert f"not_run is {marked!r}, not a list of trial numbers" in text
    assert f"#{second.number} (running) has no config key" in text
    assert f"#{keyless} (running) has no config key" in text
    with pytest.raises(ValueError, match="not a list of trial numbers"):
        mark_not_run(study, second.number)


def test_a_digest_that_lacks_a_bar_type_the_run_loads_stops(study: optuna.Study) -> None:
    """A run that read no bar of a bar type left it out of the digest, where two trials then agreed
    on bars one of them never read."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(
        study,
        "third",
        THIRD_CLOSE,
        attrs={
            "config": {
                **config_of("third"),
                # A strategy on each bar type, as `trial_config` writes a two-instrument trial.
                "strategies": [
                    {"config": {"bar_type": bars_of("third")}},
                    {"config": {"bar_type": "MSFT.ALPACA-1-DAY-LAST-EXTERNAL"}},
                ],
                "data": {
                    "data_type": "Bar",
                    "bar_types": [bars_of("third"), "MSFT.ALPACA-1-DAY-LAST-EXTERNAL"],
                },
            },
        },
    )

    with pytest.raises(
        RankingStopped,
        match=rf"third: #{number} [^\n]*data_digest names \['THIRD\.ALPACA', 'THIRD\.ALPACA-1[^\]]*\], not",
    ):
        rank(study)


@pytest.mark.parametrize(
    "config",
    [None, {**config_of("third"), "data": {"bar_types": "bars"}}],
    ids=["none", "odd"],
)
def test_a_record_that_ran_with_no_config_in_the_writers_form_stops(
    study: optuna.Study, config: object
) -> None:
    """Without the bar types it loads, a digest could not be checked to name them all."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(
        study,
        "third",
        THIRD_CLOSE,
        attrs={"config": config},
        drop=("config",) if config is None else (),
    )

    with pytest.raises(RankingStopped, match=rf"third: #{number} [^\n]*config [^\n]*bar types"):
        rank(study)


def test_a_record_whose_config_is_not_the_one_its_key_hashes_stops(study: optuna.Study) -> None:
    """A second reader: four configurations, the fourth recorded under the first's key with its
    returns, ranked as N = 3 and passed; nothing checked that a key is the hash of its config."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    [first] = [t for t in study.trials if t.number == 0]
    fourth = {**config_of("fourth"), "data": config_of("pick")["data"]}
    number = write(
        study,
        "fourth",
        PICK,
        attrs={
            "config_key": key_of("pick"),
            "run_config_id": key_of("pick"),
            "data_digest": first.user_attrs["data_digest"],
            "config": fourth,
        },
    )

    with pytest.raises(RankingStopped) as stopped:
        rank(study)
    assert re.search(
        rf"\* pick: #{number} \(traded\) [^\n]*config [^\n]*hashes to "
        rf"{config_key(fourth)}",
        str(stopped.value),
    ), stopped.value


def test_a_pick_whose_config_is_another_configuration_s_stops(study: optuna.Study) -> None:
    """A second reader: the pick's stored config replaced by another configuration's, with the bar
    types its digest names kept, was accepted as the pick."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    other = {**config_of("other"), "data": config_of("pick")["data"]}  # the pick's data, kept
    number = write(
        study,
        "pick",
        PICK,
        attrs={"config": other, "config_key": key_of("pick"), "run_config_id": key_of("pick")},
    )

    with pytest.raises(RankingStopped, match=rf"pick: #{number} [^\n]*hashes to"):
        rank(study)


def test_a_failed_record_whose_config_is_not_its_key_s_stops(study: optuna.Study) -> None:
    """A failed record is resolved by a later one of its key, but one whose config is another
    configuration's was not written by `record_trial`, and would drop that configuration from N."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    trial = study.ask()
    trial.set_user_attr("config_key", key_of("pick"))
    trial.set_user_attr("sample", 0)
    trial.set_user_attr("config", config_of("third"))
    trial.set_user_attr(OUTCOME, FAILED)
    study.tell(trial, state=TrialState.FAIL)
    write(study, "pick", PICK)

    with pytest.raises(RankingStopped, match=rf"pick: #{trial.number} [^\n]*hashes to"):
        rank(study)


def test_a_record_left_running_after_its_outcome_ran_and_is_compared(study: optuna.Study) -> None:
    """A second reader: a record whose `tell` failed after its outcome was written read as cut short,
    so a later record of its key resolved it and the returns it had read were never compared."""
    write(study, "second", SECOND)
    later = write(study, "pick", PICK)
    [done] = [t for t in study.trials if t.number == later]
    left = study.ask()
    for name, value in done.user_attrs.items():
        left.set_user_attr(name, value)
    left.set_user_attr("returns_series", {**done.user_attrs["returns_series"], str(DAY_0): 0.02})
    write(study, "pick", PICK)

    with pytest.raises(
        RankingStopped, match=rf"pick: records #{later} \(traded\), #{left.number} \(traded\) ran"
    ):
        rank(study)


@pytest.mark.parametrize(
    ("state", "ran"),
    [
        (TrialState.COMPLETE, True),
        (TrialState.FAIL, True),
        (TrialState.RUNNING, True),
        (TrialState.WAITING, False),
        (TrialState.PRUNED, False),
    ],
)
def test_an_outcome_is_a_run_only_in_a_state_the_writer_leaves(
    state: TrialState, ran: bool
) -> None:
    """A traded record `WAITING` or `PRUNED` is no run of the writer's: read as one, a plan would
    never run its key again, though the gate stops on it for good."""
    trial = optuna.trial.FrozenTrial(
        number=0,
        state=state,
        value=None,
        datetime_start=None,
        datetime_complete=None,
        params={},
        distributions={},
        user_attrs={OUTCOME: TRADED},
        system_attrs={},
        intermediate_values={},
        trial_id=0,
    )
    assert ranking.ran_to_the_end(trial) is ran


def test_a_digest_without_its_instruments_stops(study: optuna.Study) -> None:
    """A second reader: the data identity held the bars only, so records on two definitions of one
    instrument agreed. A record whose digest names its bar types and not their instruments, as every
    record made before the instruments joined it, is not in the writer's shape (ADR 0053 term 1)."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    bars_only = {bars_of("third"): digest_of("third")[bars_of("third")]}
    number = write(study, "third", THIRD_CLOSE, attrs={"data_digest": bars_only})

    with pytest.raises(
        RankingStopped, match=rf"third: #{number} [^\n]*not the bar types it loads and their instr"
    ):
        rank(study)


def test_a_waiting_record_marked_and_then_failed_by_another_writer_stops(
    study: optuna.Study,
) -> None:
    """A third reader: a queued record marked not run while `WAITING`, then started and told `FAIL` by
    another writer (`Study.optimize`, or `ask` and `tell`), stayed cleared and out of N, though it
    may have run a backtest after its mark."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.enqueue_trial({})
    [waiting] = [t.number for t in study.trials if t.state == TrialState.WAITING]
    mark_not_run(study, waiting)
    assert rank(study).n == 3

    started = study.ask()
    assert started.number == waiting
    study.tell(started, state=TrialState.FAIL)

    with pytest.raises(RankingStopped, match=rf"#{waiting} \(fail\) [^\n]*started after its mark"):
        rank(study)
    with pytest.raises(ValueError, match="started after its mark"):
        mark_not_run(study, waiting)


def test_a_record_of_another_writer_on_an_earlier_sample_still_stops(study: optuna.Study) -> None:
    """A third reader: moving the sample to the same range left another writer's record behind, and
    its stop, which no later record resolves, cleared without anything having changed."""
    write(study, "pick", PICK)
    trial = study.ask()
    trial.set_user_attr("config_key", key_of("second"))
    trial.set_user_attr("sample", 0)
    study.tell(trial, 99.0)  # COMPLETE with a value and no outcome: never the writer's
    with pytest.raises(RankingStopped, match=rf"second: #{trial.number} [^\n]*another writer"):
        rank(study)

    bounds = current_sample(study)
    move_sample(study, start=bounds.start, end=bounds.end)
    write(study, "pick", PICK, sample=1)
    write(study, "second", SECOND, sample=1)
    write(study, "third", THIRD_CLOSE, sample=1)

    with pytest.raises(RankingStopped, match=rf"second: #{trial.number} [^\n]*another writer"):
        rank(study)


@pytest.mark.parametrize("how", ["queued", "with a system attribute"])
def test_a_keyed_record_queued_or_with_system_attributes_is_another_writers(
    study: optuna.Study, how: str
) -> None:
    """A third reader: a keyed record queued by another writer (`Study.enqueue_trial`, which writes
    `fixed_params`) was read as a failure a later record of its key resolves."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    if how == "queued":
        study.enqueue_trial({}, user_attrs={"config_key": key_of("third"), "sample": 0})
        [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]
    else:
        study.add_trial(
            optuna.trial.create_trial(
                state=TrialState.FAIL,
                user_attrs={"config_key": key_of("third"), "sample": 0},
                system_attrs={"fixed_params": {"fast": 2}},
            )
        )
        number = study.trials[-1].number
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(RankingStopped, match=rf"third: #{number} [^\n]*another writer"):
        rank(study)


def test_a_told_value_that_is_not_the_sharpe_ratio_of_its_returns_stops(
    study: optuna.Study,
) -> None:
    """A third reader: a record told a value its returns do not give was read like the writer's;
    `record_trial` tells the per-session Sharpe ratio of the returns it stores."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(study, "third", THIRD_CLOSE)
    [trial] = [t for t in study.trials if t.number == number]
    study.add_trial(
        optuna.trial.create_trial(
            state=TrialState.COMPLETE, value=-999999.0, user_attrs=trial.user_attrs
        )
    )

    with pytest.raises(RankingStopped, match=r"third: #\d+ [^\n]*told -999999\.0"):
        rank(study)


def test_a_config_not_in_the_form_record_trial_writes_stops(study: optuna.Study) -> None:
    """A third reader: a config naming only the data, with no strategies or venue, hashed to its
    key and passed; `record_trial` stores the strategies, the venue and the data."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    data_only = {"data": config_of("third")["data"]}
    number = write(study, "third", THIRD_CLOSE, attrs={"config": data_only})

    with pytest.raises(RankingStopped, match=rf"third: #{number} [^\n]*config holds \['data'\]"):
        rank(study)


@pytest.mark.parametrize(
    "strategies",
    [
        [{"label": "third", "config": {"bar_type": bars_of("third")}}],
        [],
        "MaCross",
        [{"label": "third"}],
        [
            {"label": "other", "config": {"bar_type": bars_of("other")}},
            {"label": "third", "config": {"bar_type": bars_of("third")}},
        ],
    ],
    ids=["another instrument", "none", "not a list", "no bar type", "one more"],
)
def test_a_config_whose_strategies_trade_other_bars_than_its_data_loads_stops(
    study: optuna.Study, strategies: object
) -> None:
    """A fifth-round reader (Muse): a record whose strategies trade one instrument and whose data and
    digest name another, rehashed to its key, passed at DSR 0.98849. `trial_config` writes the data's
    bar types from the strategies' own, one per instrument, so no record of its writer differs."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    mixed = {**config_of("other"), "strategies": strategies}
    number = write(
        study, "third", THIRD_CLOSE, attrs={"config": mixed, "data_digest": digest_of("other")}
    )

    with pytest.raises(RankingStopped, match=rf"third: #{number} [^\n]*strategies trade"):
        rank(study)


def test_a_config_loading_one_bar_type_twice_stops(study: optuna.Study) -> None:
    """A fifth-round reader (Muse): a bar type named twice in the data passed; `trial_config` names
    each once."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    twice = config_of("third")
    twice["data"] = {"data_type": "Bar", "bar_types": [bars_of("third")] * 2}
    number = write(study, "third", THIRD_CLOSE, attrs={"config": twice})

    with pytest.raises(RankingStopped, match=rf"third: #{number} [^\n]*strategies trade"):
        rank(study)


def test_a_record_that_ran_on_an_earlier_sample_with_no_config_still_stops(
    study: optuna.Study,
) -> None:
    """A third reader: a record that ran with no `config` under a configuration's key stopped the
    ranking only while its sample was current; a move to the same range cleared it, at N = 3."""
    write(study, "pick", PICK)
    number = write(study, "second", SECOND, drop=("config",))
    bounds = current_sample(study)
    move_sample(study, start=bounds.start, end=bounds.end)
    for label, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE)):
        write(study, label, returns, sample=1)

    with pytest.raises(
        RankingStopped, match=rf"second: #{number} [^\n]*ran on sample 0 with no config"
    ):
        rank(study)


def test_a_keyed_record_record_trial_took_from_the_queue_stops(study: optuna.Study) -> None:
    """A third reader: `record_trial` took another writer's queued record of a key and told it `FAIL`
    with its note, its outcome kept, which read as a run of that key's (`record_trial` now refuses to
    take one; a record left so by the code before stops)."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    number = write(
        study,
        "pick",
        PICK,
        told_complete=False,
        attrs={"error": canon_trading.ledger.TAKEN_FROM_QUEUE},
    )

    with pytest.raises(RankingStopped, match=rf"pick: #{number} [^\n]*taken from Optuna's queue"):
        rank(study)


def test_a_value_told_where_record_trial_tells_none_stops(study: optuna.Study) -> None:
    """Code review: a value told on returns that give no Sharpe ratio (one session here) passed the
    check of told values; `record_trial` tells `FAIL` with no value there."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(study, "third", THIRD_CLOSE[:1], told_complete=False)
    [trial] = [t for t in study.trials if t.number == number]
    study.add_trial(
        optuna.trial.create_trial(state=TrialState.COMPLETE, value=2.5, user_attrs=trial.user_attrs)
    )

    with pytest.raises(RankingStopped, match=r"third: #\d+ [^\n]*told 2\.5, not nan"):
        rank(study)


# A recorded retirement of another writer's queued record (ADR 0054, 2026-11-10): each test failed
# before its change.


def test_a_retired_record_is_not_read_and_its_key_stays_counted(
    study: optuna.Study, stated_n: Callable[[optuna.Study], int]
) -> None:
    """A record `record_trial` took from another writer's queue, its outcome and returns that
    writer's and not the key's run, stops the ranking naming its remedy; retired, it is not read,
    and N is what it was."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    number = write(
        study,
        "pick",
        SECOND,
        told_complete=False,
        attrs={"error": canon_trading.ledger.TAKEN_FROM_QUEUE},
    )
    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*pick: #{number} \(traded\) was taken from Optuna's queue[^\n]*"
        rf"retire_queued\(study, {number}\) retires it",
    ):
        rank(study)
    before = stated_n(study)

    canon_trading.ledger.retire_queued(study, number)

    report = rank(study)
    assert (before, report.n, report.records, report.all_records) == (3, 3, 3, 4)
    assert number not in report.pick_trials


def test_a_key_tried_only_in_a_retired_record_stays_counted_and_names_record_trial(
    study: optuna.Study, stated_n: Callable[[optuna.Study], int]
) -> None:
    """A retired record alone on its key: N still counts the key, and the stop names `record_trial`,
    which runs the configuration, as the remedy; once it has run, the study ranks at that N."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    number = write(
        study,
        "third",
        PICK,
        told_complete=False,
        attrs={"error": canon_trading.ledger.TAKEN_FROM_QUEUE},
    )
    before = stated_n(study)

    canon_trading.ledger.retire_queued(study, number)

    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*third: tried only in another writer's queued trial #{number}, "
        r"retired[^\n]*record_trial of its configuration",
    ):
        rank(study)
    after = stated_n(study)
    write(study, "third", THIRD_CLOSE)
    assert (before, after, rank(study).n) == (3, 3, 3)


def test_a_waiting_keyed_record_names_retire_queued(study: optuna.Study) -> None:
    """A keyed record queued through `Study.enqueue_trial`, which writes `fixed_params`, stops naming
    `retire_queued`, the one remedy that keeps its key counted."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.enqueue_trial({}, user_attrs={"config_key": key_of("third"), "sample": 0})
    [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]

    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*third: #{number} \(waiting\) is WAITING[^\n]*"
        rf"retire_queued\(study, {number}\) retires it",
    ):
        rank(study)


def test_a_retired_list_not_in_its_form_stops(study: optuna.Study) -> None:
    """A `retired` attribute another writer left in another form is read as no list, and stops."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.set_user_attr("retired", ["1"])

    with pytest.raises(RankingStopped, match=r"study user attribute retired is \['1'\]"):
        rank(study)


def test_a_taken_record_left_running_is_retired_and_told_fail(study: optuna.Study) -> None:
    """Code review: a keyed record taken from the queue with its note, its `tell` refused and so
    left `RUNNING`, was sent to `retire_queued`, which refused it. It is now retired and told `FAIL`."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    left = study.ask()
    left.set_user_attr("config_key", key_of("third"))
    left.set_user_attr("error", canon_trading.ledger.TAKEN_FROM_QUEUE)
    with pytest.raises(
        RankingStopped, match=rf"third: #{left.number} [^\n]*retire_queued\(study, {left.number}\)"
    ):
        rank(study)

    canon_trading.ledger.retire_queued(study, left.number)

    assert study.trials[left.number].state == TrialState.FAIL
    with pytest.raises(RankingStopped, match=r"\(N = 3\)[\s\S]*third: tried only in"):
        rank(study)


def test_a_queued_record_with_another_fault_still_names_retire_queued(study: optuna.Study) -> None:
    """Second reader: a queued keyed record whose config is not in `record_trial`'s parts stopped
    on that check first, and its stop named no remedy, though `retire_queued` takes it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.enqueue_trial({}, user_attrs={"config_key": key_of("third"), "config": {"x": 1}})
    [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]

    with pytest.raises(
        RankingStopped,
        match=rf"third: #{number} [^\n]*not the parts[^\n]*retire_queued\(study, {number}\)",
    ):
        rank(study)


def test_no_remedy_is_named_while_the_retired_list_is_not_in_its_form(study: optuna.Study) -> None:
    """Second review: with the `retired` list in another form, the stop on a queued record still
    named `retire_queued`, which refuses that list; only the list's own stop names the cause."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.enqueue_trial({}, user_attrs={"config_key": key_of("third"), "sample": 0})
    study.set_user_attr("retired", "none")

    with pytest.raises(RankingStopped, match=r"study user attribute retired is 'none'") as stopped:
        rank(study)
    assert "retire_queued(study" not in str(stopped.value)


# The gate round after the retirement of queued records (ADR 0054 term 7, ADR 0073): each test failed
# before its change, or on the mutation of the guard it names.


def test_a_raced_queued_record_whose_read_back_is_refused_stops_and_is_retired(
    study: optuna.Study, tmp_path: Path, alpaca_bars: AlpacaBars, stated_n: Callable[..., int]
) -> None:
    """Codex, gate round after #160: another writer's keyed record, queued between `record_trial`'s
    check and its `ask` with no system attribute, was taken by `ask`, and a refused read of it then
    told it `FAIL` with a generic error, the other writer's outcome kept. That erased the only sign it
    was taken from the queue, and the gate passed on its returns at N = 3, DSR 0.9885, with no
    backtest run. A trial `ask` gave that cannot be read back may be such a record, so it now carries
    `record_trial`'s queue note: the ranking stops on it naming `retire_queued`, which retires it."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    with create_ledger_study("shape", start=DAY_0, end=END, path=tmp_path / "shape.db") as shape:
        write(shape, "pick", PICK)
        foreign = dict(shape.trials[0].user_attrs)
    alpaca_bars.write(tmp_path / "catalog")
    real_ask = optuna.Study.ask
    raced: list[int] = []

    def queued_then_asked(self: optuna.Study, *args: Any, **kwargs: Any) -> optuna.trial.Trial:
        self.add_trial(optuna.trial.create_trial(state=TrialState.WAITING, user_attrs=foreign))
        raced.append(self.trials[-1].number)
        return real_ask(self, *args, **kwargs)

    def unread(*_: object) -> None:
        raise optuna.exceptions.StorageInternalError("the store refused the read")

    def never(*_: object) -> None:
        raise AssertionError("no backtest starts on a trial that was not read back")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(optuna.Study, "ask", queued_then_asked)
        patched.setattr(canon_trading.ledger, "_asked", unread)
        patched.setattr(canon_trading.ledger, "run_backtest_config", never)
        with pytest.raises(optuna.exceptions.StorageInternalError, match="refused the read") as err:
            canon_trading.ledger.record_trial(
                study, tmp_path / "catalog", [ma_cross_config()], starting_balances=["100000 USD"]
            )

    [number] = raced
    assert f"trial #{number}, asked by record_trial, could not be read back" in "\n".join(
        err.value.__notes__
    )
    taken = study.trials[number]
    assert (taken.state, taken.user_attrs["error"], taken.user_attrs[OUTCOME]) == (
        TrialState.FAIL,
        canon_trading.ledger.TAKEN_FROM_QUEUE,
        TRADED,
    )
    with pytest.raises(
        RankingStopped,
        match=rf"\(N = 3\)[\s\S]*pick: #{number} \(traded\) was taken from Optuna's queue[^\n]*"
        rf"retire_queued\(study, {number}\) retires it",
    ):
        rank(study)
    before = stated_n(study)

    canon_trading.ledger.retire_queued(study, number)

    assert study.user_attrs["retired"] == [number]
    with pytest.raises(RankingStopped, match=r"\(N = 3\)[\s\S]*pick: tried only in"):
        rank(study)
    assert before == 3


@pytest.mark.parametrize(
    "note", [canon_trading.ledger.TAKEN_FROM_QUEUE, canon_trading.ledger.RETIRED_NOTE]
)
def test_a_record_with_either_queue_note_not_in_the_retired_list_stops(
    study: optuna.Study, note: str
) -> None:
    """Muse, gate round after #160: no test failed with `RETIRED_NOTE` dropped from the ranking's
    queue notes, and the gate then passed at DSR 0.9885 on a taken record left out of the `retired`
    list, read as a run of its key. Either note stops it, naming `retire_queued`."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    number = write(study, "pick", PICK, told_complete=False, attrs={"error": note})

    with pytest.raises(
        RankingStopped,
        match=rf"pick: #{number} \(traded\) was taken from Optuna's queue[^\n]*"
        rf"retire_queued\(study, {number}\)",
    ):
        rank(study)


def test_a_failed_record_with_an_outcome_the_writer_never_writes_stops_for_good(
    study: optuna.Study,
) -> None:
    """Muse, gate round after #160: no test failed with the check of a keyed record's outcome
    removed, and a `FAIL` record of the pick's key with outcome `done` was then read as a failure
    the later pick resolves, and the gate passed at DSR 0.9885. It stops for good."""
    foreign = study.ask()
    foreign.set_user_attr("config_key", key_of("pick"))
    foreign.set_user_attr("sample", 0)
    foreign.set_user_attr(OUTCOME, "done")
    study.tell(foreign, state=TrialState.FAIL)
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(
        RankingStopped,
        match=rf"pick: #{foreign.number} [^\n]*has outcome 'done', not one record_trial writes",
    ):
        rank(study)


@pytest.mark.parametrize(
    "shown",
    [
        {"returns_series": {str(DAY_0): 0.01}},
        {"session_days": [str(DAY_0)]},
    ],
    ids=["returns only", "session days only"],
)
def test_a_keyless_record_holding_only_returns_or_session_days_shows_a_run(
    study: optuna.Study, shown: dict[str, object]
) -> None:
    """Muse, gate round after #160: no test failed with returns and session days dropped from what
    shows a run on a keyless record (ADR 0053 term 3), and such a record was then marked not run,
    cleared, and the gate passed at DSR 0.9885 without it in N. It cannot be marked, and stops."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    keyless = study.ask()
    for name, value in shown.items():
        keyless.set_user_attr(name, value)
    study.tell(keyless, state=TrialState.FAIL)

    with pytest.raises(ValueError, match="shows a run"):
        mark_not_run(study, keyless.number)
    with pytest.raises(RankingStopped, match=rf"#{keyless.number} \(fail\)[^\n]*shows a run"):
        rank(study)


def test_a_keyless_queued_trial_taken_without_its_note_says_it_stops_for_good(
    study: optuna.Study, tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    """Code review of the one model: a keyless queued trial `record_trial` took, whose note the store
    refused, holds `Study.enqueue_trial`'s `fixed_params` and no note, so the ledger's rule reads it as
    another writer's started record, which nothing clears: the strict side, but the error raised did
    not say so. It now names the trial and the stop."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.enqueue_trial({})
    [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]
    alpaca_bars.write(tmp_path / "catalog")
    real_set = optuna.trial.Trial.set_user_attr

    def refused_note(self: optuna.trial.Trial, key: str, value: Any) -> None:
        if key == "error" and value == canon_trading.ledger.TAKEN_FROM_QUEUE:
            raise optuna.exceptions.StorageInternalError("the store refused the note")
        real_set(self, key, value)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(optuna.trial.Trial, "set_user_attr", refused_note)
        with pytest.raises(optuna.exceptions.StorageInternalError, match="refused the note") as err:
            canon_trading.ledger.record_trial(
                study, tmp_path / "catalog", [ma_cross_config()], starting_balances=["100000 USD"]
            )

    assert f"trial #{number}, queued by another writer with no config key, was taken without" in (
        "\n".join(err.value.__notes__)
    )
    assert study.trials[number].state == TrialState.FAIL
    with pytest.raises(RankingStopped, match=rf"#{number} \(fail\) [^\n]*nothing clears it"):
        rank(study)


def test_a_marked_waiting_record_written_on_after_its_mark_is_named_as_such(
    study: optuna.Study,
) -> None:
    """Code review of the one model: a keyless record marked while `WAITING`, then given an outcome by
    another writer while still `WAITING`, was said to have been started after its mark."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.enqueue_trial({})
    [waiting] = [t for t in study.trials if t.state == TrialState.WAITING]
    mark_not_run(study, waiting.number)
    study._storage.set_trial_user_attr(waiting._trial_id, OUTCOME, TRADED)

    stop = rf"#{waiting.number} \(waiting\) [^\n]*written on by another writer after its mark"
    with pytest.raises(RankingStopped, match=stop):
        rank(study)
    with pytest.raises(ValueError, match="written on by another writer after its mark"):
        mark_not_run(study, waiting.number)


def test_a_marked_waiting_record_told_fail_with_the_retirement_s_note_still_stops(
    study: optuna.Study,
) -> None:
    """Code review of the one model: a keyless record marked while `WAITING` is cleared once taken
    only with the note `record_trial` writes on what it takes; `retire_queued` never takes a keyless
    one, so its note there is another writer's, and the record stops as before the model."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    study.enqueue_trial({})
    [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]
    mark_not_run(study, number)
    taken = study.ask()
    taken.set_user_attr("error", canon_trading.ledger.RETIRED_NOTE)
    study.tell(taken, state=TrialState.FAIL)

    with pytest.raises(RankingStopped, match=rf"#{number} \(fail\) [^\n]*after its mark"):
        rank(study)


@pytest.mark.parametrize(
    "directions",
    [[StudyDirection.MAXIMIZE, StudyDirection.MAXIMIZE], [StudyDirection.MINIMIZE]],
    ids=["two objectives", "minimized"],
)
def test_a_study_not_of_the_ledger_s_one_maximized_objective_is_refused_by_name(
    tmp_path: Path, directions: list[StudyDirection]
) -> None:
    """Codex, gate round 4: a study of two objectives, with the ledger's sample, opened, and the
    three passing records each told `[its Sharpe ratio, 123.0]`, ranked at N = 3, DSR 0.988485, as
    the one-objective study does. `record_trial` tells one value, the Sharpe ratio, so a record told
    two is another writer's, and a study of other directions than `create_ledger_study`'s one
    `MAXIMIZE` is no ledger study: `open_study` refuses it before any run, and the ranking of one
    opened another way stops naming it and each record told two values."""
    path = tmp_path / "trials.db"
    made = optuna.create_study(
        study_name="other", storage=f"sqlite:///{path}", directions=directions
    )
    sample = canon_trading.ledger.Sample(0, DAY_0, END)
    made.set_user_attr(canon_trading.ledger.SAMPLES, [canon_trading.ledger._sample_attr(sample)])
    also = (123.0,) if len(directions) > 1 else ()
    numbers = [
        write(made, label, returns, also=also)
        for label, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE))
    ]

    refused = pytest.raises(
        canon_trading.ledger.LedgerNotFound, match="not the ledger's one objective"
    )
    with refused, open_study("other", path):
        pass
    with pytest.raises(RankingStopped) as stopped:
        rank(made)

    message = str(stopped.value)
    named = ", ".join(d.name for d in directions)
    assert f"has {len(directions)} objectives ({named}), where a ledger study has one" in message
    for label, number in zip(("pick", "second", "third"), numbers, strict=True):
        assert bool(re.search(rf"{label}: #{number} [^\n]*was told 2 values", message)) is bool(
            also
        )


def test_a_record_told_more_than_one_value_is_another_writers() -> None:
    """Codex, gate round 4: the ledger's rule read a record told two values as `record_trial`'s
    own, keyed or not, so `mark_not_run` would clear a keyless one and the pass read a keyed one."""
    told = optuna.trial.create_trial(
        values=[0.5, 123.0], user_attrs={"config_key": key_of("pick"), "sample": 0}
    )
    keyless = optuna.trial.create_trial(values=[0.5, 123.0])

    for trial in (told, keyless):
        reading = canon_trading.ledger.origin(trial)
        assert reading.origin is canon_trading.ledger.Origin.FOREIGN
        assert "was told 2 values" in reading.why


@pytest.mark.parametrize("with_queue", [True, False], ids=["queued", "asked"])
def test_a_keyless_record_with_the_retirement_s_note_is_another_writers(
    study: optuna.Study, with_queue: bool
) -> None:
    """Antigravity, gate round 4: a keyless record another writer started and told `FAIL` with
    `retire_queued`'s note, unmarked, was read as taken from the queue here, so `mark_not_run`
    cleared it and the gate passed at N = 3, DSR 0.98849. `retire_queued` takes only a keyed
    record, so its note on a keyless one is another writer's, and nothing clears it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    if with_queue:
        study.enqueue_trial({"fast": 6}, user_attrs={"strategy": "custom"})
    taken = study.ask()
    if not with_queue:
        taken.set_user_attr("config_key", "")
        taken.set_user_attr("strategy", "custom")
    taken.set_user_attr("error", canon_trading.ledger.RETIRED_NOTE)
    study.tell(taken, state=TrialState.FAIL)
    [frozen] = [t for t in study.trials if t.number == taken.number]

    assert canon_trading.ledger.origin(frozen).origin is canon_trading.ledger.Origin.FOREIGN
    with pytest.raises(RankingStopped, match=rf"#{taken.number} \(fail\) [^\n]*nothing clears it"):
        rank(study)
    with pytest.raises(ValueError, match="retire_queued's note"):
        mark_not_run(study, taken.number)
    assert "not_run" not in study.user_attrs


def test_a_marked_waiting_record_taken_whose_tell_was_refused_is_finished_by_its_mark(
    study: optuna.Study,
) -> None:
    """Antigravity, gate round 4: a keyless record marked while `WAITING`, then taken from Optuna's
    queue by `record_trial`, which wrote its note and whose `tell` the store refused, stayed
    `RUNNING`: the gate stopped on it for good and `mark_not_run` refused it, where an unmarked one
    left so is told `FAIL` by its mark. The mark now finishes the take, and clears it."""
    write(study, "pick", PICK)
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    study.enqueue_trial({})
    [number] = [t.number for t in study.trials if t.state == TrialState.WAITING]
    mark_not_run(study, number)
    taken = study.ask()  # what `record_trial` does, its `tell` then refused
    taken.set_user_attr("error", canon_trading.ledger.TAKEN_FROM_QUEUE)

    stop = rf"#{number} \(running\) [^\n]*mark_not_run\(study, {number}\) tells it FAIL"
    with pytest.raises(RankingStopped, match=stop):
        rank(study)
    mark_not_run(study, number)

    assert study.trials[number].state == TrialState.FAIL
    assert study.user_attrs["not_run_waiting"] == [number]
    report = rank(study)
    assert (report.n, report.passed) == (3, True)


SCALE = 1e158  # each return about 1e155: finite, its square past float64's largest, about 1.8e308


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            {"pick": PICK, "second": SECOND, "third": [x * SCALE for x in THIRD_WIDE]},
            id="a rejected study passed",
        ),
        pytest.param(
            {
                "super": [(0.02 + 0.005 * (-1) ** i) * SCALE for i in range(T)],
                "pick": PICK,
                "second": SECOND,
            },
            id="the winner inverted",
        ),
        pytest.param(
            {
                "pick": PICK,
                "second": [x * SCALE for x in SECOND],
                "third": [x * SCALE + 1e155 for x in THIRD_CLOSE],
            },
            id="N-hat raised ValueError",
        ),
    ],
)
def test_returns_whose_squares_overflow_stop_by_name(
    study: optuna.Study, case: dict[str, list[float]]
) -> None:
    """Antigravity, gate round 4: finite returns whose squares pass float64's largest made skfolio's
    standard deviation infinite and the Sharpe ratio 0.0, so a study rejected at DSR 0.69241 passed
    at 0.97295, a pick of Sharpe ratio 3.93 lost to one of 0.699, and two such trials raised a bare
    ValueError from N-hat. The series is refused where it enters the gate, naming it."""
    big = [label for label, returns in case.items() if max(map(abs, returns)) > 1e150]
    numbers = {label: write(study, label, returns) for label, returns in case.items()}

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    for label in big:
        assert re.search(
            rf"{label}: the sum of the squares of its returns is inf[^\n]*\(#{numbers[label]}\)",
            message,
        )


@pytest.mark.parametrize("bool_first", [True, False], ids=["bool first", "bool second"])
def test_every_record_of_a_key_has_its_returns_checked_whatever_their_order(
    study: optuna.Study, bool_first: bool
) -> None:
    """Antigravity, gate round 4: two records of one key on the sample, one holding `False` for a
    day's 0.0, compared equal under Python's `==`, and only the first was read by `daily_series`:
    the gate stopped with the `False` record first and passed with it second. Every record that ran
    is now read the same way, so the gate stops either way, naming the record."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    zeroed = [0.0, *PICK[1:]]
    written = {str(DAY_0 + n * DAY_NS): value for n, value in enumerate(zeroed)}
    with_bool = {**written, str(DAY_0): False}
    order = [with_bool, written] if bool_first else [written, with_bool]
    numbers = [
        write(
            study,
            "pick",
            zeroed,
            attrs={"returns_series": returns},
            told_complete=returns is written,
        )
        for returns in order
    ]
    flagged = numbers[0] if bool_first else numbers[1]

    with pytest.raises(
        RankingStopped,
        match=rf"pick: returns_series value False on {DAY_0} is not a float[^\n]*\(#{flagged}\)",
    ):
        rank(study)


def test_a_return_refused_still_lets_its_key_s_disagreement_be_named(study: optuna.Study) -> None:
    """Code review of round 4's fix: with every record of a key read, a rejected return hid the
    key's other stops, so one call no longer named them all. Both are named now."""
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)
    write(study, "pick", PICK)
    odd = write(
        study, "pick", PICK, attrs={"returns_series": {str(DAY_0): True}}, told_complete=False
    )
    write(
        study, "pick", PICK, attrs={"data_digest": {**digest_of("pick"), bars_of("pick"): "0" * 64}}
    )

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert f"pick: returns_series value True on {DAY_0} is not a float" in message
    assert f"(#{odd})" in message
    assert "ran the same configuration" in message


def test_returns_whose_fourth_central_moment_overflows_stop_by_name(study: optuna.Study) -> None:
    """Code review of round 4's fix: returns of about 1e80 have finite squares but a fourth central
    moment past float64's largest, and the gate stopped only on skfolio's DSR of NaN, naming no
    series. The series is refused where it enters the gate, naming it."""
    number = write(study, "pick", [x * 1e80 for x in PICK])
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(
        RankingStopped,
        match=rf"pick: skfolio's variance [^\n]*fourth central moment inf[^\n]*\(#{number}\)",
    ):
        rank(study)


def test_a_record_that_lacks_the_sample_s_last_session_day_stops(study: optuna.Study) -> None:
    """A sixth-round reader (Antigravity): records that stopped 10 days into the sample passed as
    the whole sample. `record_trial` refuses a sample whose last session day holds no bar, so each of
    its records holds that day."""
    for label, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE)):
        write(study, label, returns[:20], attrs={"backtest_end": DAY_0 + 20 * DAY_NS})

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    for label in ("pick", "second", "third"):
        assert re.search(
            rf"{label}: #\d+ [^\n]*session_days lacks 2025-04-09, the session day of the end of "
            r"sample 0",
            str(stopped.value),
        ), str(stopped.value)


def test_returns_on_the_registration_day_before_a_midnight_start_stop(study: optuna.Study) -> None:
    """A sixth-round reader (Antigravity): with the sample starting at 00:00 UTC, records carrying a
    return and a session day on the day before passed. NautilusTrader books only the account's
    registration there, and `record_trial` refuses a bar on it."""
    for label, returns in (("pick", PICK), ("second", SECOND), ("third", THIRD_CLOSE)):
        write(study, label, [0.0, *returns], first_day=-1)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    for label in ("pick", "second", "third"):
        assert re.search(
            rf"{label}: #\d+ [^\n]*session_days has 2025-03-10, outside sample 0",
            str(stopped.value),
        ), str(stopped.value)


def test_session_days_out_of_order_stop(study: optuna.Study) -> None:
    """A sixth-round reader (Antigravity): session days reversed with a day twice passed at DSR 1.0."""
    days = [str(DAY_0 + n * DAY_NS) for n in range(T)]
    write(study, "pick", PICK, attrs={"session_days": [*reversed(days), days[0], days[0]]})
    write(study, "second", SECOND)
    write(study, "third", THIRD_CLOSE)

    with pytest.raises(RankingStopped, match=r"pick: session day .* not after it"):
        rank(study)


def test_a_pick_whose_moments_underflow_stops(study: optuna.Study, tmp_path: Path) -> None:
    """A sixth-round reader (Antigravity): the same pick rescaled by 1e-81, its Sharpe ratio, skewness
    and kurtosis unchanged in exact arithmetic, went from DSR 0.9464, rejected, to 0.9570, passed:
    its fourth central moment rounded to 0.0 and skfolio's kurtosis read 0."""
    pick = [0.88 + x for x in [1.24, -1.24] * 14 + [1.45, -1.45]]
    third = [PATTERN[(i + 5) % 10] * 0.6 - 0.0024 for i in range(T)]
    write(study, "pick", pick)
    write(study, "second", SECOND)
    write(study, "third", third)
    report = rank(study)
    assert report.pick == "pick"
    assert report.dsr == pytest.approx(0.94637, abs=5e-6)
    assert not report.passed

    with create_ledger_study("scaled", start=DAY_0, end=END, path=tmp_path / "scaled.db") as scaled:
        write(scaled, "pick", [value * 1e-81 for value in pick])
        write(scaled, "second", SECOND)
        write(scaled, "third", third)
        with pytest.raises(RankingStopped, match=r"pick: .* below float64's smallest normal"):
            rank(scaled)
