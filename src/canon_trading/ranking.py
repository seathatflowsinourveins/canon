"""The gate: rank the trials of a ledger study and test the pick's deflated Sharpe ratio at 0.95.

Contract: ADR 0010, protocol step 3, computed as ADR 0031 decides, on the ledger of ADR 0035 and ADR 0054
over the sample of ADR 0041. skfolio
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
It also stops on what ADR 0054 terms 5 and 6, ADR 0041 and ADR 0053 add: a configuration with a failed or
`RUNNING` record that no later record of it resolves (a record with a key and no sample number is
read as on the current sample, unless it ran), an idle one, records of one configuration that
disagree (their outcome included), one not run on the study's current sample, returns ADR 0010 step 2
rejects or a record not in the shape `record_trial` writes, records that read one bar type as
different bars, trials that do not cover the same session days over the sample, and a record with no
key not marked by `mark_not_run`. A record that ran on the current sample is in `record_trial`'s shape
only if its run's bounds are the sample's, its `run_config_id` is its key, its digest names at least
one bar type, each with a SHA-256 hex digest, and every return and session day falls on a UTC day of
the sample (`canon_trading.ledger.session_day` of its two ends) no later than the session day of the
run's own end; Optuna validates no user attribute. A record with a key that did not run to the end
is a failure a later record resolves only if it is one `record_trial` leaves: one that shows a
result Optuna keeps (`COMPLETE`, `PRUNED`, a value or an intermediate value), or an outcome
`record_trial` never writes, stops for good. A keyless record marked by `mark_not_run` is cleared
only while it is in a state the mark leaves, `FAIL` or `WAITING`. The samples are read once, so the
current sample and the numbers known agree.
Every such stop is gathered before one is raised. Configurations tied at the highest Sharpe ratio with
the same returns are one pick; tied with other returns, they leave no single pick and stop it. N-hat
below 1 is raised to 1, skfolio's least, and the DSR at N-hat is withheld when its benchmark is below
0 (ADR 0031).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from optuna.trial import TrialState
from skfolio import measures

from .ledger import FAILED
from .ledger import IDLE
from .ledger import NOT_RUN
from .ledger import OUTCOME
from .ledger import TRADED
from .ledger import Sample
from .ledger import has_key
from .ledger import is_not_run_list
from .ledger import ran_something
from .ledger import samples
from .ledger import session_day
from .ledger import shows_an_evaluation
from .returns_matrix import ReturnsRejected
from .returns_matrix import daily_series
from .returns_matrix import is_ns_text
from .returns_matrix import returns_matrix
from .returns_matrix import session_sharpe

if TYPE_CHECKING:
    from collections.abc import Sequence

    import optuna
    import pandas as pd
    from optuna.trial import FrozenTrial

THRESHOLD = 0.95
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


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
    tied: tuple[str, ...]
    pick_trials: tuple[int, ...]
    sharpe: float
    dsr: float
    passed: bool
    n_hat: float | None
    dsr_at_n_hat: float | None
    n_hat_note: str | None = None

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
                f"Pick {self.pick}"
                + (f", the same returns as {', '.join(self.tied)}" if self.tied else "")
                + f" (trials {', '.join(map(str, self.pick_trials))}): Sharpe ratio "
                f"{self.sharpe:.6g} per session, DSR {self.dsr:.4f}; it {verdict} at {THRESHOLD}."
            ),
        ]
        if self.n_hat is not None:
            at = "" if self.dsr_at_n_hat is None else f", DSR at N-hat {self.dsr_at_n_hat:.4f}"
            lines.append(f"N-hat = {self.n_hat:.4g}{at} (not used in the gate).")
        if self.n_hat_note:
            lines.append(self.n_hat_note)
        return "\n".join(lines)


def _describe(trial: FrozenTrial) -> str:
    return f"#{trial.number} ({trial.user_attrs.get(OUTCOME, trial.state.name.lower())})"


def ran_to_the_end(trial: FrozenTrial) -> bool:
    """Whether the record ran to the end: finished, with outcome traded or idle (ADR 0054 term 5).

    Public, with `on_current_sample`, so that a caller deciding which configurations to run again
    (`canon_trading.variant_study`) applies the rule `rank` applies, and follows it if it changes.

    The ledger writes the outcome last, so a record that has one of those finished its run. A traded
    record told `FAIL` because its own Sharpe ratio is not finite ran to the end too; the gate stops
    on it below, under ADR 0031, and no later record of its key resolves it.
    """
    return trial.state.is_finished() and trial.user_attrs.get(OUTCOME) in (TRADED, IDLE)


def on_current_sample(trial: FrozenTrial, sample: Sample, known: set[int]) -> bool:
    """Whether `rank` reads `trial`, a record with a key, as on `sample`, the study's current one.

    `known` is the number of every sample the study has had. A record holding one of those numbers
    is on the current sample if the number is `sample`'s. A record with a key and no such number is
    read as on the current sample if it did not run to the end (ADR 0041 term 5): it is what an
    error between the key's write and the sample's leaves, before any run, and a later record of its
    key on the current sample resolves it; left out, its failure would not count. One that ran to the
    end with no such number came from another writer: it is on no sample, and `rank` stops on it.
    """
    if _known_sample(trial, known):
        return trial.user_attrs["sample"] == sample.number
    return not ran_to_the_end(trial)


# The states `canon_trading.ledger.mark_not_run` leaves a record in. A marked record in another state
# was started after its mark, by an `ask` that took it from Optuna's queue, and stops again.
_LEFT_BY_A_MARK = (TrialState.FAIL, TrialState.WAITING)


def _not_the_writers(trial: FrozenTrial) -> str | None:
    """Why `trial`, a record with a key, is in no state and outcome `record_trial` leaves, if it is.

    `record_trial` leaves a record that ran to the end `COMPLETE`, `traded`, with a value, or `FAIL`,
    `traded` or `idle`, with none; one cut short `RUNNING` with any outcome or none, or `FAIL`,
    `failed` or with none. It suggests no parameter and reports no intermediate value. Anything else
    was told by another writer: read as a failure it would be resolved by a later record and drop out
    of every check, and read as a run it would enter the matrix unchecked.
    """
    outcome = trial.user_attrs.get(OUTCOME)
    state = trial.state
    if outcome not in (None, FAILED, TRADED, IDLE):
        return f"has outcome {outcome!r}, not one record_trial writes"
    if trial.intermediate_values or trial.distributions:
        return "holds an intermediate value or a parameter, which record_trial never writes"
    if ran_to_the_end(trial):
        told = (state, outcome, trial.values is not None)
        if told in ((TrialState.COMPLETE, TRADED, True), (TrialState.FAIL, TRADED, False)) or (
            told == (TrialState.FAIL, IDLE, False)
        ):
            return None
        return f"was told {state.name} with outcome {outcome!r}, as record_trial never tells one"
    if shows_an_evaluation(trial):
        return "shows a result Optuna keeps but no outcome record_trial writes for one"
    return None


def _known_sample(trial: FrozenTrial, known: set[int]) -> bool:
    """Whether `trial` holds the number of one of the study's samples (ADR 0041 term 5)."""
    number = trial.user_attrs.get("sample")
    return not isinstance(number, bool) and isinstance(number, int) and number in known


def _named(key: str, records: Sequence[FrozenTrial]) -> str:
    return f"{key} ({', '.join(f'#{t.number}' for t in records)})"


def rank(study: optuna.Study) -> RankingReport:
    """Rank every trial of `study` and test the pick, or raise `RankingStopped` saying why not.

    Every stop a trial causes is gathered before any is raised, so one call names them all: each
    configuration's records, the shape of its returns, the data it read, the session days it covers
    and its Sharpe ratio. The stops of the study as a whole (N, V, a tie at the top, the DSR) need
    every trial to have cleared those, and come after.
    """
    trials = study.get_trials(deepcopy=False)  # read only; a copy of every series is not needed
    if not trials:
        raise RankingStopped(f"study {study.study_name!r} has no trials")
    history = samples(study)  # one read, so the current sample and the known numbers agree
    sample = history[-1]
    known = {s.number for s in history}
    by_key: dict[str, list[FrozenTrial]] = defaultdict(list)
    stops: list[str] = []
    marked = study.user_attrs.get(NOT_RUN, [])
    if is_not_run_list(marked):
        not_run = set(marked)
    else:
        not_run = set()
        stops.append(
            f"study user attribute {NOT_RUN} is {marked!r}, not a list of trial numbers in the form "
            "mark_not_run writes, so no record is read as marked (ADR 0053)",
        )
    for trial in trials:
        if has_key(trial):
            by_key[trial.user_attrs["config_key"]].append(trial)
        elif ran_something(trial):
            stops.append(
                f"#{trial.number} ({trial.state.name.lower()}) has no config key in the form "
                "record_trial writes but shows a run: a trial of an unknown configuration, which "
                "another writer left and nothing clears (ADR 0053)",
            )
        elif trial.number not in not_run or trial.state not in _LEFT_BY_A_MARK:
            stops.append(
                f"#{trial.number} ({trial.state.name.lower()}) has no config key, so it ran no "
                f"backtest; if its process is gone, mark_not_run(study, {trial.number}) clears it "
                "(ADR 0053)",
            )
    n = len(by_key)

    chosen: dict[str, FrozenTrial] = {}
    on_sample: dict[str, list[FrozenTrial]] = {}
    ran_on_sample: dict[
        str, list[FrozenTrial]
    ] = {}  # of those, the ones that ran, for what names them
    read: list[FrozenTrial] = []  # every record on the current sample that ran to the end
    for key, every in by_key.items():
        records = []
        for trial in every:
            if on_current_sample(trial, sample, known):
                records.append(trial)
            elif not _known_sample(trial, known):  # so it ran to the end
                stops.append(
                    f"#{trial.number} ({trial.user_attrs[OUTCOME]}) ran with sample "
                    f"{trial.user_attrs.get('sample')!r}, not a sample number of the study: not in "
                    "the shape record_trial writes (ADR 0041)",
                )
        if not records:
            stops.append(
                f"{key}: tried ({', '.join(map(_describe, every))}) but not run on the current "
                f"{sample.describe()}; run it on this sample (ADR 0041)",
            )
            continue
        on_sample[key] = records
        foreign = [(trial, why) for trial in records if (why := _not_the_writers(trial))]
        if foreign:
            stops.extend(
                f"{key}: {_describe(trial)} {why}: not in the shape record_trial writes, so another "
                "writer left it and no later record resolves it (ADR 0054)"
                for trial, why in foreign
            )
            continue
        ran = [trial for trial in records if ran_to_the_end(trial)]
        if not ran:
            stops.append(
                f"{key}: no record ran to the end ({', '.join(map(_describe, records))})",
            )
            continue
        faulty = [(trial, faults) for trial in ran if (faults := _shape_faults(trial, sample))]
        if faulty:
            stops.extend(
                f"{key}: {_describe(trial)} is not in the shape record_trial writes on "
                f"{sample.describe()}: {'; '.join(faults)} (ADR 0041, ADR 0053)"
                for trial, faults in faulty
            )
            continue
        read.extend(ran)
        ran_on_sample[key] = ran
        unresolved = [
            trial
            for trial in records
            if not ran_to_the_end(trial) and not any(other.number > trial.number for other in ran)
        ]
        if unresolved:
            stops.append(
                f"{key}: no later record ran to the end after "
                f"{', '.join(map(_describe, unresolved))}",
            )
            continue
        first = ran[0]
        keep = (
            OUTCOME,
            "backtest_start",
            "backtest_end",
            "returns_series",
            "session_days",
            "data_digest",
        )
        differ = [
            t for t in ran[1:] if any(t.user_attrs.get(a) != first.user_attrs.get(a) for a in keep)
        ]
        if differ:
            stops.append(
                f"{key}: records {', '.join(map(_describe, [first, *differ]))} ran the same "
                f"configuration on {sample.describe()} and disagree on their outcome, range, "
                "returns, session days or data; if the catalog or this code changed, move the "
                "sample and run every configuration again (ADR 0041, ADR 0053)",
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
                key, trial.user_attrs.get("returns_series"), trial.user_attrs.get("session_days")
            )
        except ReturnsRejected as rejected:
            stops.append(f"{rejected} (#{trial.number})")
    matrix = None
    if len(series) > 1:
        try:
            matrix = returns_matrix(list(series.values()))
        except ReturnsRejected as rejected:
            named = ", ".join(f"{key} (#{chosen[key].number})" for key in rejected.names)
            stops.append(f"{rejected}\nTrials: {named}")
    sharpes = {key: session_sharpe(one) for key, one in series.items()}
    stops.extend(
        f"{key}: Sharpe ratio {sharpe} over the sample (#{chosen[key].number})"
        for key, sharpe in sharpes.items()
        if not math.isfinite(sharpe)
    )
    if n < 2:
        stops.append(
            f"N = {n}; the DSR needs N >= 2 ({'; '.join(_named(k, r) for k, r in by_key.items())})",
        )
    if stops or matrix is None:
        raise RankingStopped(_stopped(study, n, stops))

    values = [sharpes[key] for key in matrix.columns]
    everyone = "; ".join(_named(key, ran_on_sample[key]) for key in matrix.columns)
    v = float(measures.variance(values))
    if all(math.isclose(value, values[0]) for value in values):
        raise RankingStopped(
            _stopped(
                study,
                n,
                [
                    (
                        f"V = {v}: every trial has the same Sharpe ratio, {values[0]!r}, within "
                        "floating-point noise (math.isclose at its default rel_tol, 1e-9; "
                        f"ADR 0031): {everyone}"
                    ),
                ],
            ),
        )
    best = max(values)
    tied = [key for key in matrix.columns if math.isclose(sharpes[key], best)]
    pick = tied[0]
    if any(not _same(matrix[pick], matrix[key]) for key in tied[1:]):
        raise RankingStopped(
            _stopped(
                study,
                n,
                [
                    "share the highest Sharpe ratio within floating-point noise, with returns that "
                    "differ, so there is no one pick (ADR 0031): "
                    + "; ".join(_named(key, ran_on_sample[key]) for key in tied),
                ],
            ),
        )
    pick_returns = [float(value) for value in matrix[pick]]
    dsr = float(measures.deflated_sharpe_ratio(pick_returns, values))
    if not math.isfinite(dsr):
        raise RankingStopped(
            _stopped(
                study,
                n,
                [f"skfolio gives DSR {dsr} for the pick {_named(pick, ran_on_sample[pick])}"],
            )
        )

    t = len(matrix)
    n_hat, dsr_at_n_hat, n_hat_note = _n_hat(matrix, values, pick_returns, n)

    current = sorted(
        (trial for records in on_sample.values() for trial in records), key=lambda t: t.number
    )
    numbers = [trial.number for trial in current]
    starts = sorted(_utc(t.datetime_start) for t in current if t.datetime_start is not None)
    return RankingReport(
        study=study.study_name,
        sample=sample.describe(),
        records=len(current),
        all_records=len(trials),
        first_trial=min(numbers),
        last_trial=max(numbers),
        first_recorded=starts[0] if starts else "unknown",
        last_recorded=starts[-1] if starts else "unknown",
        n=n,
        t=t,
        v=v,
        first_day=matrix.index[0].date().isoformat(),
        last_day=matrix.index[-1].date().isoformat(),
        pick=pick,
        tied=tuple(tied[1:]),
        pick_trials=tuple(sorted(t.number for key in tied for t in ran_on_sample[key])),
        sharpe=sharpes[pick],
        dsr=dsr,
        passed=dsr >= THRESHOLD,
        n_hat=n_hat,
        dsr_at_n_hat=dsr_at_n_hat,
        n_hat_note=n_hat_note,
    )


def _utc(start: datetime) -> str:
    """A trial's start in UTC, with its offset: ISO 8601, seconds and microseconds.

    Optuna 5.0.0 stores the start in UTC and returns it as naive local time
    (`storages/_rdb/models.py`, `datetime_start`), so it is read back as local time and converted.
    The UTC texts sort as the instants do; in the hour a fall-back repeats, the local
    time is ambiguous and the earlier instant is taken (Python's `fold` = 0). The conversion uses the
    zone the process has now: a study object kept open while the process changes zone mixes zones.
    """
    return start.astimezone(UTC).isoformat()


def _same(a: pd.Series, b: pd.Series) -> bool:
    """Whether two columns are equal day by day within the tolerance V uses (ADR 0031).

    Each day's two returns must agree to `math.isclose`'s default `rel_tol` of the larger of them, or
    of the largest return either column has, so a 0.0 against 1e-19 of rounding counts as equal.
    """
    scale = max(abs(float(x)) for x in (*a, *b))
    return all(math.isclose(x, y, abs_tol=1e-9 * scale) for x, y in zip(a, b, strict=True))


def _n_hat(
    matrix: pd.DataFrame, values: list[float], pick_returns: list[float], n: int
) -> tuple[float | None, float | None, str | None]:
    """N-hat (eq. 9) and the DSR at it, or why either is not reported; never used in the gate."""
    t = len(matrix)
    if t < n * (n - 1) / 2:
        return None, None, f"N-hat not reported: T < N(N-1)/2 = {n * (n - 1) // 2} (p. 15)."
    correlations = measures.correlation(matrix.to_numpy())
    pairs = [correlations[i][j] for i in range(n) for j in range(n) if i != j]
    rho = sum(pairs) / len(pairs)
    n_hat = float(rho + (1 - rho) * n)
    notes = []
    if n_hat < 1:
        # rho rounds above 1, its bound (p. 14); skfolio takes N >= 1 only.
        notes.append(f"N-hat {n_hat!r} raised to 1, the least skfolio takes (rho = {rho!r})")
        n_hat = 1.0
    benchmark = measures.expected_max_sharpe_ratio(
        n_hat, float(measures.standard_deviation(values))
    )
    if benchmark < 0:
        notes.append(
            f"DSR at N-hat withheld: its benchmark SR0 = {benchmark:.6g} is below 0, where the "
            "expected maximum of N-hat >= 1 trials of mean 0 cannot be; eq. 1's approximation "
            "undershoots for N-hat this small",
        )
        return n_hat, None, "; ".join(notes) + "."
    dsr = float(measures.probabilistic_sharpe_ratio(pick_returns, benchmark_sharpe_ratio=benchmark))
    return n_hat, dsr, ("; ".join(notes) + ".") if notes else None


def _shape_faults(trial: FrozenTrial, sample: Sample) -> list[str]:
    """How `trial`, a record that ran on `sample`, differs from what `record_trial` writes there.

    NautilusTrader sets a run's `backtest_start` to the start it was given and its `backtest_end` to
    the engine's clock when the run ends, at or before the end it was given (`run_impl` and
    `end_impl`, `crates/backtest/src/engine.rs` at the pinned commit), so the start must be the
    sample's and the end within it. The returns and session days must fall on the run's own days, from
    the session day of its start to that of its end: a run books nothing after its clock stopped. Their
    own form is `daily_series`'s to refuse; here only their range.
    """
    attrs = trial.user_attrs
    faults = []
    start, end = attrs.get("backtest_start"), attrs.get("backtest_end")
    if not (isinstance(start, int) and not isinstance(start, bool) and start == sample.start):
        faults.append(f"backtest_start {start!r}, not the start of {sample.describe()}")
    end_ok = (
        isinstance(end, int) and not isinstance(end, bool) and sample.start <= end <= sample.end
    )
    if not end_ok:
        faults.append(f"backtest_end {end!r}, not within {sample.describe()}")
    if attrs.get("run_config_id") != attrs.get("config_key"):
        faults.append(f"run_config_id {attrs.get('run_config_id')!r}, not its config key")
    digests = attrs.get("data_digest")
    if (
        not isinstance(digests, dict)
        or not digests
        or not all(
            isinstance(bar_type, str)
            and bar_type
            and isinstance(digest, str)
            and SHA256_HEX.fullmatch(digest)
            for bar_type, digest in digests.items()
        )
    ):
        faults.append(f"data_digest {digests!r}")
    elif (loaded := _loaded_bar_types(attrs.get("config"))) is None:
        faults.append(
            f"config {attrs.get('config')!r} names no bar types in the form record_trial writes"
        )
    elif set(digests) != set(loaded):
        # `record_trial` digests every bar type the run loads, one with no bar included.
        faults.append(f"data_digest names {sorted(digests)}, not the bar types it loads, {loaded}")
    low, high = session_day(sample.start), session_day(sample.end)
    ran_until = session_day(end) if end_ok else high
    for values, what in (
        (attrs.get("returns_series"), "returns_series has a key on"),
        (attrs.get("session_days"), "session_days has"),
    ):
        days = sorted(
            int(day)
            for day in (values if isinstance(values, dict | list) else ())
            if is_ns_text(day)
        )
        outside = [day for day in days if not low <= day <= high]
        if outside:
            faults.append(
                f"{what} {_day(outside[0])}, outside {sample.describe()} ({len(outside)} in all)"
            )
        after = [day for day in days if ran_until < day <= high]
        if after:
            faults.append(f"{what} {_day(after[0])}, after its backtest_end ({len(after)} in all)")
    return faults


def _loaded_bar_types(config: object) -> list[str] | None:
    """The bar types a record's `config` says its run loads, or `None` if it says none in that form."""
    data = config.get("data") if isinstance(config, dict) else None
    bar_types = data.get("bar_types") if isinstance(data, dict) else None
    if isinstance(bar_types, list) and all(isinstance(b, str) for b in bar_types):
        return bar_types
    return None


def _day(ns: int) -> str:
    return datetime.fromtimestamp(ns // 1_000_000_000, UTC).date().isoformat()


def _data_disagreements(read: list[FrozenTrial], sample: Sample) -> list[str]:
    """One reason for each bar type that records on `sample` read as different bars (ADR 0053 term 1).

    Every record in `read` has passed `_shape_faults`, so its digest is a mapping of texts.
    """
    by_digest: dict[str, dict[str, list[FrozenTrial]]] = defaultdict(lambda: defaultdict(list))
    for trial in read:
        for bar_type, digest in trial.user_attrs["data_digest"].items():
            by_digest[bar_type][digest].append(trial)
    return [
        f"{bar_type}: records on {sample.describe()} read different bars: "
        + "; ".join(
            f"{digest[:12]} by {', '.join(f'#{t.number}' for t in trials)}"
            for digest, trials in digests.items()
        )
        + "; move the sample and run every configuration again (ADR 0053)"
        for bar_type, digests in sorted(by_digest.items())
        if len(digests) > 1
    ]


def _stopped(study: optuna.Study, n: int, reasons: list[str]) -> str:
    return f"The ranking of study {study.study_name!r} (N = {n}) stopped:\n" + "\n".join(
        f"* {reason}" for reason in reasons
    )
