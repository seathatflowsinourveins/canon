"""The gate: rank the trials of a ledger study and test the pick's deflated Sharpe ratio at 0.95.

Contract: ADR 0010, protocol step 3, computed as ADR 0031 decides, on the ledger of ADR 0035 over the
sample of ADR 0041. skfolio
1.12.8, read 2026-11-10 in the installed `measures/_measures.py` and `portfolio/_base.py`:

* `Portfolio.sharpe_ratio` of a one-asset portfolio with weight 1: each trial's Sharpe ratio per
  session, mean over sample standard deviation, the convention `probabilistic_sharpe_ratio` uses for
  the pick.
* `measures.variance` of those Sharpe ratios: V, the sample variance.
* `measures.deflated_sharpe_ratio(returns, trial_sharpe_ratios)`: the DSR, eqs. 1-2, with N the length
  of the array.
* `measures.correlation`, `measures.standard_deviation`, `measures.expected_max_sharpe_ratio` and
  `measures.probabilistic_sharpe_ratio`: N-hat = rho + (1 - rho) N (eq. 9), rho the mean of the
  correlations between trials, reported with the DSR it gives only when T >= N(N-1)/2 (p. 15) and
  never used in the gate.

Nothing statistical is computed here; the mean of the off-diagonal correlations is eq. 9's own input.
skfolio raises nothing on input that lets a pick pass more easily (ADR 0031), so the ranking stops,
naming the trials, unless every trial's Sharpe ratio and every return of the pick is finite, N >= 2 and
V > 0. V counts as 0 when every trial's Sharpe ratio equals the first's under `math.isclose` at its
default tolerance (ADR 0031): copies of one configuration differ only in rounding, and would otherwise
pass undeflated. A return that is not finite is refused, for every trial, by
`canon_trading.returns_matrix`.
It also stops on what ADR 0035 terms 5 and 6 and ADR 0041 add: a configuration with a failed or
`RUNNING` record that no later record of it resolves, an idle one, records of one configuration that
disagree, one not run on the study's current sample, returns ADR 0010 step 2 rejects, and trials that
do not cover the same session days over the sample. Two configurations sharing the highest Sharpe
ratio leave no single pick, and stop it too.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from skfolio import Portfolio
from skfolio import measures

from .ledger import IDLE
from .ledger import OUTCOME
from .ledger import TRADED
from .ledger import Sample
from .ledger import current_sample
from .returns_matrix import ReturnsRejected
from .returns_matrix import daily_series
from .returns_matrix import returns_matrix

if TYPE_CHECKING:
    import optuna
    from optuna.trial import FrozenTrial

THRESHOLD = 0.95


class RankingStopped(Exception):
    """The ranking cannot go on; the message names every trial that stopped it."""


@dataclass(frozen=True)
class RankingReport:
    """What a ranking states (ADR 0010, Confirmation): N, T, V, the DSR and the ledger range read."""

    study: str
    sample: str
    records: int
    all_records: int
    first_trial: int
    last_trial: int
    first_recorded: str
    last_recorded: str
    n: int
    t: int
    v: float
    first_day: str
    last_day: str
    pick: str
    pick_trials: tuple[int, ...]
    sharpe: float
    dsr: float
    passed: bool
    n_hat: float | None
    dsr_at_n_hat: float | None

    def summary(self) -> str:
        verdict = "passes" if self.passed else "is rejected"
        lines = [
            (
                f"Study {self.study!r}, {self.sample}: {self.records} records on it, trials "
                f"{self.first_trial} to {self.last_trial}, recorded {self.first_recorded} to "
                f"{self.last_recorded}; {self.all_records} records on every sample."
            ),
            (
                f"N = {self.n} configurations over every sample, T = {self.t} sessions ({self.first_day} to "
                f"{self.last_day}), V = {self.v:.6g}."
            ),
            (
                f"Pick {self.pick} (trials {', '.join(map(str, self.pick_trials))}): Sharpe ratio "
                f"{self.sharpe:.6g} per session, DSR {self.dsr:.4f}; it {verdict} at {THRESHOLD}."
            ),
        ]
        if self.n_hat is None:
            lines.append(f"N-hat not reported: T < N(N-1)/2 = {self.n * (self.n - 1) // 2}.")
        else:
            lines.append(
                f"N-hat = {self.n_hat:.4g}, DSR at N-hat {self.dsr_at_n_hat:.4f} (not used in the gate).",
            )
        return "\n".join(lines)


def _describe(trial: FrozenTrial) -> str:
    return f"#{trial.number} ({trial.user_attrs.get(OUTCOME, trial.state.name.lower())})"


def _ran(trial: FrozenTrial) -> bool:
    """Whether the record ran to the end: finished, with outcome traded or idle (ADR 0035 term 5).

    The ledger writes the outcome last, so a record that has one of those finished its run. A traded
    record told `FAIL` because its own Sharpe ratio is not finite ran to the end too; the gate stops
    on it below, under ADR 0031, and no later record of its key resolves it.
    """
    return trial.state.is_finished() and trial.user_attrs.get(OUTCOME) in (TRADED, IDLE)


def rank(study: optuna.Study) -> RankingReport:
    """Rank every trial of `study` and test the pick, or raise `RankingStopped` saying why not."""
    trials = study.trials
    if not trials:
        raise RankingStopped(f"study {study.study_name!r} has no trials")
    sample = current_sample(study)
    by_key: dict[str, list[FrozenTrial]] = defaultdict(list)
    for trial in trials:
        by_key[trial.user_attrs.get("config_key") or f"no key, trial #{trial.number}"].append(trial)
    n = len(by_key)

    stops: list[str] = []
    chosen: dict[str, FrozenTrial] = {}
    on_sample: dict[str, list[FrozenTrial]] = {}
    read: list[FrozenTrial] = []  # every record on the current sample that ran to the end
    for key, every in by_key.items():
        records = [t for t in every if t.user_attrs.get("sample") == sample.number]
        if not records and "config_key" in every[0].user_attrs:
            stops.append(
                f"{key}: tried ({', '.join(map(_describe, every))}) but not run on the current "
                f"{sample.describe()}; run it on this sample (ADR 0041)",
            )
            continue
        records = records or every
        on_sample[key] = records
        ran = [trial for trial in records if _ran(trial)]
        read.extend(ran)
        if not ran:
            stops.append(
                f"{key}: no record ran to the end ({', '.join(map(_describe, records))})",
            )
            continue
        unresolved = [
            trial
            for trial in records
            if not _ran(trial) and not any(other.number > trial.number for other in ran)
        ]
        if unresolved:
            stops.append(
                f"{key}: no later record ran to the end after "
                f"{', '.join(map(_describe, unresolved))}",
            )
            continue
        first = ran[0]
        keep = ("backtest_start", "backtest_end", "returns_series", "session_days", "data_digest")
        differ = [
            t for t in ran[1:] if any(t.user_attrs.get(a) != first.user_attrs.get(a) for a in keep)
        ]
        if differ:
            stops.append(
                f"{key}: records {', '.join(map(_describe, [first, *differ]))} ran the same "
                f"configuration on {sample.describe()} and disagree on their range, returns, "
                "session days or data",
            )
            continue
        if first.user_attrs[OUTCOME] == IDLE:
            stops.append(
                f"{key}: never traded ({', '.join(map(_describe, ran))}); ADR 0010 gives an idle "
                "trial no value",
            )
            continue
        chosen[key] = first

    stops.extend(_data_disagreements(read, sample))
    series = {}
    for key, trial in chosen.items():
        try:
            series[key] = daily_series(
                key,
                trial.user_attrs["returns_series"],
                trial.user_attrs.get("session_days", []),
            )
        except ReturnsRejected as rejected:
            stops.append(f"{rejected} (#{trial.number})")
    if stops:
        raise RankingStopped(_stopped(study, n, stops))
    if n < 2:
        raise RankingStopped(_stopped(study, n, [f"N = {n}; the DSR needs N >= 2"]))

    try:
        matrix = returns_matrix(list(series.values()))
    except ReturnsRejected as rejected:
        named = ", ".join(f"{key} (#{chosen[key].number})" for key in rejected.names)
        raise RankingStopped(_stopped(study, n, [f"{rejected}\nTrials: {named}"])) from rejected
    sharpes = {
        key: float(Portfolio(X=matrix[[key]], weights=[1.0], name=key).sharpe_ratio)
        for key in matrix.columns
    }
    not_finite = [key for key, sharpe in sharpes.items() if not math.isfinite(sharpe)]
    if not_finite:
        raise RankingStopped(
            _stopped(
                study,
                n,
                [f"{key}: Sharpe ratio {sharpes[key]} over the common range" for key in not_finite],
            ),
        )
    values = list(sharpes.values())
    v = float(measures.variance(values))
    if all(math.isclose(value, values[0]) for value in values):
        raise RankingStopped(
            _stopped(
                study,
                n,
                [
                    (
                        f"V = {v}: every trial has the same Sharpe ratio, {values[0]!r}, within "
                        "floating-point noise (math.isclose at its default rel_tol, 1e-9; ADR 0031)"
                    ),
                ],
            ),
        )
    best = max(values)
    picks = [key for key, sharpe in sharpes.items() if sharpe == best]
    if len(picks) > 1:
        raise RankingStopped(
            _stopped(study, n, [f"{', '.join(picks)} share the highest Sharpe ratio"])
        )
    [pick] = picks
    pick_returns = [float(value) for value in matrix[pick]]
    dsr = float(measures.deflated_sharpe_ratio(pick_returns, values))
    if not math.isfinite(dsr):
        raise RankingStopped(_stopped(study, n, [f"{pick}: skfolio gives DSR {dsr}"]))

    t = len(matrix)
    n_hat = dsr_at_n_hat = None
    if t >= n * (n - 1) / 2:
        correlations = measures.correlation(matrix.to_numpy())
        pairs = [correlations[i][j] for i in range(n) for j in range(n) if i != j]
        rho = sum(pairs) / len(pairs)
        n_hat = float(rho + (1 - rho) * n)
        benchmark = measures.expected_max_sharpe_ratio(
            n_hat, float(measures.standard_deviation(values))
        )
        dsr_at_n_hat = float(
            measures.probabilistic_sharpe_ratio(pick_returns, benchmark_sharpe_ratio=benchmark)
        )

    current = sorted(
        (trial for records in on_sample.values() for trial in records), key=lambda t: t.number
    )
    numbers = [trial.number for trial in current]
    starts = sorted(trial.datetime_start for trial in current if trial.datetime_start is not None)
    return RankingReport(
        study=study.study_name,
        sample=sample.describe(),
        records=len(current),
        all_records=len(trials),
        first_trial=min(numbers),
        last_trial=max(numbers),
        first_recorded=starts[0].isoformat() if starts else "unknown",
        last_recorded=starts[-1].isoformat() if starts else "unknown",
        n=n,
        t=t,
        v=v,
        first_day=matrix.index[0].date().isoformat(),
        last_day=matrix.index[-1].date().isoformat(),
        pick=pick,
        pick_trials=tuple(trial.number for trial in on_sample[pick]),
        sharpe=sharpes[pick],
        dsr=dsr,
        passed=dsr >= THRESHOLD,
        n_hat=n_hat,
        dsr_at_n_hat=dsr_at_n_hat,
    )


def _data_disagreements(read: list[FrozenTrial], sample: Sample) -> list[str]:
    """One reason for each bar type that records on `sample` read as different bars (ADR 0041)."""
    by_digest: dict[str, dict[str, list[FrozenTrial]]] = defaultdict(lambda: defaultdict(list))
    for trial in read:
        digests = trial.user_attrs.get("data_digest")
        for bar_type, digest in (digests if isinstance(digests, dict) else {}).items():
            by_digest[bar_type][digest].append(trial)
    return [
        f"{bar_type}: records on {sample.describe()} read different bars: "
        + "; ".join(
            f"{digest[:12]} by {', '.join(f'#{t.number}' for t in trials)}"
            for digest, trials in digests.items()
        )
        + "; move the sample and run every configuration again (ADR 0041)"
        for bar_type, digests in sorted(by_digest.items())
        if len(digests) > 1
    ]


def _stopped(study: optuna.Study, n: int, reasons: list[str]) -> str:
    return f"The ranking of study {study.study_name!r} (N = {n}) stopped:\n" + "\n".join(
        f"* {reason}" for reason in reasons
    )
