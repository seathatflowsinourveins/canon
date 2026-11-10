"""The gate: rank the trials of a ledger study and test the pick's deflated Sharpe ratio at 0.95.

Contract: ADR 0010, protocol step 3, computed as ADR 0031 decides, on the ledger of ADR 0035. skfolio
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
V > 0. A return that is not finite is refused, for every trial, by `canon_trading.returns_matrix`.
It also stops on what ADR 0035 terms 5 and 6 add: a configuration with a failed or `RUNNING` record
that no later record of it resolves, an idle one, records of one configuration that disagree, and
returns ADR 0010 step 2 rejects. Two configurations sharing the highest Sharpe ratio leave no single
pick, and stop it too.
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
    records: int
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
                f"Study {self.study!r}: {self.records} records, trials {self.first_trial} to "
                f"{self.last_trial}, recorded {self.first_recorded} to {self.last_recorded}."
            ),
            (
                f"N = {self.n} configurations, T = {self.t} sessions ({self.first_day} to "
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
    by_key: dict[str, list[FrozenTrial]] = defaultdict(list)
    for trial in trials:
        by_key[trial.user_attrs.get("config_key") or f"no key, trial #{trial.number}"].append(trial)
    n = len(by_key)

    stops: list[str] = []
    chosen: dict[str, FrozenTrial] = {}
    for key, records in by_key.items():
        ran = [trial for trial in records if _ran(trial)]
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
        keep = ("backtest_start", "backtest_end", "returns_series")
        differ = [
            t for t in ran[1:] if any(t.user_attrs.get(a) != first.user_attrs.get(a) for a in keep)
        ]
        if differ:
            stops.append(
                f"{key}: records {', '.join(map(_describe, [first, *differ]))} ran the same "
                "configuration and disagree on their range or returns",
            )
            continue
        if first.user_attrs[OUTCOME] == IDLE:
            stops.append(
                f"{key}: never traded ({', '.join(map(_describe, ran))}); ADR 0010 gives an idle "
                "trial no value",
            )
            continue
        chosen[key] = first

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
        raise RankingStopped(_stopped(study, n, [str(rejected)])) from rejected
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
    if not v > 0:
        raise RankingStopped(
            _stopped(study, n, [f"V = {v}: every trial has the same Sharpe ratio"])
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

    numbers = [trial.number for trial in trials]
    starts = sorted(trial.datetime_start for trial in trials if trial.datetime_start is not None)
    return RankingReport(
        study=study.study_name,
        records=len(trials),
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
        pick_trials=tuple(trial.number for trial in by_key[pick]),
        sharpe=sharpes[pick],
        dsr=dsr,
        passed=dsr >= THRESHOLD,
        n_hat=n_hat,
        dsr_at_n_hat=dsr_at_n_hat,
    )


def _stopped(study: optuna.Study, n: int, reasons: list[str]) -> str:
    return f"The ranking of study {study.study_name!r} (N = {n}) stopped:\n" + "\n".join(
        f"* {reason}" for reason in reasons
    )
