"""The gate: rank the trials of a ledger study and test the pick's deflated Sharpe ratio at 0.95.

Contract: ADR 0010, protocol step 3, computed as ADR 0031 and ADR 0080 decide, on the ledger of ADR 0035, ADR 0054 and ADR 0081
over the sample of ADR 0041. skfolio
1.12.8, read 2026-11-10 in the installed `measures/_measures.py` and `portfolio/_base.py`; at 1.12.9
the first is byte-identical and the second differs only in `ty: ignore` comments (ADR 0031):

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
V > 0. Every series it computes on, and each trial's Sharpe ratio, comes from
`canon_trading.returns_matrix.session_returns`, whose one numeric check (that module's header; ADR
0080, 2026-11-11) refuses, for every record that ran on the current sample and before any two are
compared, a return that is not a finite float, and a series of session days that does not vary beyond
rounding at its own size (a constant one, or one of zeros), whose statistics are not finite, or whose
moments underflow. No moment is checked here. Sharpe ratios are compared by one bound, each series'
own (`SessionReturns.rounding`, what rounding can move its computed ratio by; ADR 0080, 2026-11-11),
and two ratios are told apart only when they differ by more than the sum of their two bounds. Both
rules of the study are judged on the set, never pair by pair, so no order of recording changes
either, and the columns are taken in the order of the keys, so V, N-hat and which of several keys
with the same returns is named the pick are the same to the bit in any order. V counts as 0 when the highest and the lowest ratio are not told apart, read from the ratios
since skfolio's variance of equal numbers can round above 0; it also stops when that variance is
not above 0 or not finite. Ratios told apart give the V they have. A value told is held to the same
bound of the one computed again (`_shape_faults`). A study of any directions but one objective maximized stops it too:
`record_trial` tells one value, and a record told more is another writer's (`origin`).
It also stops on what ADR 0054 terms 5 and 6, ADR 0041 and ADR 0053 add: a configuration with a failed or
`RUNNING` record that no later record of it resolves (a record with a key and no sample number is
read as on the current sample, unless it ran; one with outcome traded or idle ran, in any state
`record_trial` leaves, `RUNNING` included, since the outcome is written last), an idle one, records of one configuration that
disagree (their outcome included), one not run on the study's current sample, returns ADR 0010 step 2
rejects or a record not in the shape `record_trial` writes (on any sample: a stored `config` not in
its three parts or that does not hash to its key, a state, outcome, parameter, intermediate value or
system attribute `record_trial` never leaves, a keyed `WAITING` record, and one that ran on an earlier
sample with no `config`; so moving the sample clears none of them), records that read one bar type as
different bars or one instrument as different definitions, trials that do not cover the same session days over the sample, and a record with no
key not marked by `mark_not_run`. A record that ran on the current sample is in `record_trial`'s shape
only if its run's bounds are the sample's, its `run_config_id` is its key, its digest names exactly
the bar types its config loads and the instruments they name, each with a SHA-256 hex digest, and every return and session day falls on a UTC day of
the sample (after the account's registration day, `canon_trading.ledger.registration_day` of its
start, to `canon_trading.ledger.session_day` of its end) no later than the session day of the run's
own end, its session days include the sample's last (`record_trial` refuses a sample whose last
session day holds no bar), and a value told is the per-session Sharpe ratio of its returns; Optuna validates no
user attribute. A record with a key that did not run to the end
is a failure a later record resolves only if it is one `record_trial` leaves: one that shows a
result Optuna keeps (`COMPLETE`, `PRUNED`, a value or an intermediate value), or an outcome
`record_trial` never writes, stops for good. A keyless record marked by `mark_not_run` is cleared
only while it is in a state the mark leaves: `FAIL` for one in `not_run`, and for one marked while
`WAITING` (`not_run_waiting`) `WAITING`, or `FAIL` with the note `record_trial` writes on a trial it
takes from Optuna's queue and runs nothing in; started by another writer after its mark, it stops
for good. Who wrote a record is read by the ledger's one rule, `canon_trading.ledger.origin`: a
keyless record another writer started, told or wrote on (a system attribute, a user attribute
`record_trial` never writes before its key) stops for good, marked or not. A keyed record `canon_trading.ledger.retire_queued` retired, another writer's queued
trial, is never read, and its key stays counted in N; a key tried only in such records stops until
`record_trial` runs it on the current sample. The stops on another writer's queued keyed record,
`WAITING` or taken from Optuna's queue, name `retire_queued` (ADR 0081, 2026-11-10).
The samples are read once, so the current sample and the numbers known agree.
Every such stop is gathered before one is raised. The top group is every configuration whose Sharpe
ratio is not told apart from the highest. Inside it returns are compared exactly, day by day: the
same on every day, the group is one pick; with any day that differs it leaves no single pick and
stops the ranking, naming the group, whatever order they were recorded in. N-hat
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

import numpy as np
from nautilus_trader.model import BarType
from optuna.study import StudyDirection
from optuna.trial import TrialState
from skfolio import measures

from .ledger import FAILED
from .ledger import IDLE
from .ledger import NOT_RUN
from .ledger import NOT_RUN_WAITING
from .ledger import OUTCOME
from .ledger import RAN
from .ledger import RETIRED
from .ledger import TRADED
from .ledger import Origin
from .ledger import Sample
from .ledger import after_its_mark
from .ledger import cleared_while_waiting
from .ledger import config_key
from .ledger import data_bar_types
from .ledger import has_key
from .ledger import is_not_run_list
from .ledger import origin
from .ledger import registration_day
from .ledger import samples
from .ledger import session_day
from .ledger import shows_an_evaluation
from .ledger import taken_untold
from .returns_matrix import DAY_NS
from .returns_matrix import ReturnsRejected
from .returns_matrix import SessionReturns
from .returns_matrix import is_ns_text
from .returns_matrix import returns_matrix
from .returns_matrix import session_returns

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
    ranked_sample: Sample
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
    """Whether the record ran to the end: it has outcome traded or idle (ADR 0054 term 5).

    Public, with `on_current_sample`, so that a caller deciding which configurations to run again
    (`canon_trading.variant_study`) applies the rule `rank` applies, and follows it if it changes.

    The ledger writes the outcome last, after every result (ADR 0035 term 2), so a record that has one
    of those finished its run in any state `record_trial` leaves it in: `COMPLETE` or `FAIL` as told,
    `FAIL` too when its `tell` failed after the outcome, or still `RUNNING` if no `tell` could be
    written (`canon_trading.ledger._tell_failed`). Its run is compared with every other record of its
    key like any other; read as cut short, a later record of its key would resolve it and the returns
    and data it read would never be compared. A traded record told `FAIL` because its own Sharpe ratio
    is not finite ran to the end too; the gate stops on it below, under ADR 0080, and no later record
    of its key resolves it. Such an outcome in another state (`WAITING`, `PRUNED`) is no run of the
    writer's, and `_not_the_writers` stops on it.
    """
    return trial.state in _RAN_STATES and trial.user_attrs.get(OUTCOME) in RAN


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


# The states `canon_trading.ledger.record_trial` leaves a record that ran to the end in, and each
# (state, outcome, told a value) it leaves one in (`ran_to_the_end`, `_not_the_writers`).
_RAN_AS_TOLD = frozenset(
    {
        (TrialState.COMPLETE, TRADED, True),
        (TrialState.FAIL, TRADED, False),
        (TrialState.FAIL, IDLE, False),
        (TrialState.RUNNING, TRADED, False),
        (TrialState.RUNNING, IDLE, False),
    }
)
_RAN_STATES = frozenset(state for state, _, _ in _RAN_AS_TOLD)

# A record that ran, still `RUNNING`, with a value. `record_trial` leaves none so: Optuna's `tell`
# writes the value and the state in one transaction (`set_trial_state_values`,
# `optuna/storages/_rdb/storage.py` at the pinned commit). A read can fall across that write and
# show one: Optuna reads a study's trials in several statements, and a `tell` between two of them
# is observed in `tests/test_ledger.py`,
# `test_a_read_split_by_a_tell_stops_the_ranking_and_never_ranks_half_a_record`. So the stop does
# not call the ledger damaged: it names the remedy that tells a writer still recording from
# another writer's record, ranking again.
_MAY_BE_RECORDING = (
    "is RUNNING with a value, as record_trial leaves none: a writer may be recording it now"
)

# The parts of the `config` `canon_trading.ledger.record_trial` stores (`trial_config`).
CONFIG_PARTS = ("data", "strategies", "venue")


def _not_the_writers(trial: FrozenTrial) -> str | None:
    """Why `trial`, a record with a key, is in no state and outcome `record_trial` leaves, if it is.

    `record_trial` leaves a record that ran to the end `COMPLETE`, `traded`, with a value, or `FAIL` or
    `RUNNING` (a `tell` that failed after the outcome), `traded` or `idle`, with none; one cut short
    `RUNNING` or `FAIL`, `failed` or with no outcome. Who wrote the record is read first, by the
    ledger's one rule (`canon_trading.ledger.origin`): a queued record, one taken from the queue, and
    one another writer started, told or wrote on (a system attribute, a parameter, an intermediate
    value or a user attribute `record_trial` never writes) are another writer's. Anything else
    in no state and outcome `record_trial` leaves was told by another writer: read as a failure it would be resolved by a later record and drop out
    of every check, and read as a run it would enter the matrix unchecked. `rank` reads every keyed
    record with this, on every sample, so a move of the sample clears none.
    """
    reading = origin(trial)
    if reading.origin is not Origin.WRITER:
        return reading.why
    outcome = trial.user_attrs.get(OUTCOME)
    state = trial.state
    if outcome not in (None, FAILED, TRADED, IDLE):
        return f"has outcome {outcome!r}, not one record_trial writes"
    if outcome in RAN:
        if (state, outcome, trial.values is not None) in _RAN_AS_TOLD:
            return None
        if state is TrialState.RUNNING:  # so with a value: the one state not in the set above
            if outcome == IDLE:
                return "is RUNNING with a value, and record_trial tells an idle run none"
            return _MAY_BE_RECORDING
        return f"was told {state.name} with outcome {outcome!r}, as record_trial never tells one"
    if shows_an_evaluation(trial):
        return "shows a result Optuna keeps but no outcome record_trial writes for one"
    return None


def _remedy(trial: FrozenTrial) -> str:
    """The remedy for another writer's record, if it is one `retire_queued` takes: a queued keyed
    record, `WAITING` or taken from Optuna's queue with its note (`origin`, ADR 0081)."""
    if origin(trial).origin in (Origin.QUEUED, Origin.TAKEN):
        return f"; retire_queued(study, {trial.number}) retires it, its key still counted in N"
    return ""


def _config_fault(trial: FrozenTrial, key: str) -> str | None:
    """Why `trial`'s stored `config` is not the configuration its key, `key`, names, if it is not.

    `record_trial` writes `config` beside the key it hashed it to (ADR 0054 term 4), so a record whose
    config hashes to another key, or to none, was written by another writer: counted under its key, it
    would take that configuration's place in the matrix and lower N. A record with no `config` is what
    an error after the key's write leaves, before any run; one that ran needs it (`_shape_faults` on
    the current sample, `_ran_unconfigured` on an earlier one). The config is in the three parts
    `record_trial` stores (`CONFIG_PARTS`).
    Checked on every sample, since N counts keys on every sample; like a record that ran with no
    sample number of the study's (ADR 0041 term 5), it stops for good, and no move clears it.
    """
    if "config" not in trial.user_attrs:
        return None
    config = trial.user_attrs["config"]
    if not isinstance(config, dict) or sorted(config) != list(CONFIG_PARTS):
        held = sorted(config) if isinstance(config, dict) else type(config).__name__
        return f"is keyed though its config holds {held}, not the parts record_trial stores, {list(CONFIG_PARTS)}"
    try:
        hashed = config_key(config)
    except (TypeError, ValueError) as error:
        return f"has a config with no canonical form ({error})"
    if hashed != key:
        return f"has a config that hashes to {hashed}, not to its key"
    traded = _traded_bar_types(config["strategies"])
    loaded = _loaded_bar_types(config)
    if traded is None or loaded is None or data_bar_types(traded) != loaded:
        # `trial_config` writes the data's bar types from the strategies' own (`data_bar_types`),
        # so a record of its writer never differs: one whose strategies trade other bars than its
        # data and digest name would be read as a run of bars it never traded (ADR 0054 term 6).
        return (
            f"has a config whose strategies trade {traded!r} where its data loads {loaded!r}, "
            "not the bar types of its strategies, each once, as record_trial writes them"
        )
    return None


def _traded_bar_types(strategies: object) -> list[str] | None:
    """The bar type each stored strategy trades, in order, or `None` if any names none in the form
    `trial_config` writes (`{"config": {"bar_type": ...}}`, a text), or there is no strategy."""
    if not isinstance(strategies, list) or not strategies:
        return None
    traded = []
    for strategy in strategies:
        fields = strategy.get("config") if isinstance(strategy, dict) else None
        bar_type = fields.get("bar_type") if isinstance(fields, dict) else None
        if not isinstance(bar_type, str):
            return None
        traded.append(bar_type)
    return traded


def _ran_unconfigured(trial: FrozenTrial, sample: Sample, known: set[int]) -> str | None:
    """Why `trial`, a keyed record that ran to the end on an earlier sample, is not the writer's, if
    it holds no `config`: `record_trial` stores one before every run. On the current sample
    `_shape_faults` names it, and a record that ran with no sample number of the study's stops as
    such; one left on an earlier sample is read by nothing else, and a move would clear it."""
    if (
        _known_sample(trial, known)
        and trial.user_attrs["sample"] != sample.number
        and ran_to_the_end(trial)
        and "config" not in trial.user_attrs
    ):
        return f"ran on sample {trial.user_attrs['sample']} with no config, which record_trial stores before every run"
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
    directions = study.directions
    if directions != [StudyDirection.MAXIMIZE]:
        # `record_trial` tells one value, the Sharpe ratio; Optuna 5.0.0 refuses one value on a
        # study of more objectives (`study/_tell.py`), so no record of its own is told there.
        stops.append(
            f"study {study.study_name!r} has {len(directions)} objectives "
            f"({', '.join(d.name for d in directions)}), where a ledger study has one, MAXIMIZE, "
            "the Sharpe ratio record_trial tells (ADR 0035, ADR 0054)",
        )
    marks: dict[str, set[int]] = {}
    malformed: set[str] = set()  # the lists not in their form, whose remedies cannot be taken
    for name in (NOT_RUN, NOT_RUN_WAITING, RETIRED):
        marked = study.user_attrs.get(name, [])
        if is_not_run_list(marked):
            marks[name] = set(marked)
        else:
            marks[name] = set()
            malformed.add(name)
            stops.append(
                f"study user attribute {name} is {marked!r}, not a list of trial numbers in the "
                "form mark_not_run and retire_queued write, so no record is read as marked by it "
                "(ADR 0053, ADR 0054)",
            )
    # Another writer's queued records `retire_queued` retired, by key: none is read, and each key
    # stays counted in N (ADR 0081, 2026-11-10).
    retired: dict[str, list[FrozenTrial]] = defaultdict(list)
    for trial in trials:
        if has_key(trial) and trial.number in marks[RETIRED]:
            retired[trial.user_attrs["config_key"]].append(trial)
        elif has_key(trial):
            by_key[trial.user_attrs["config_key"]].append(trial)
        elif trial.number in marks[NOT_RUN_WAITING]:
            # Read before `origin`, so a record started after its mark is named as such.
            if taken_untold(trial):
                stops.append(
                    f"#{trial.number} (running) was marked not run while WAITING and taken from "
                    "Optuna's queue with record_trial's note, whose tell was refused; if its "
                    f"process is gone, mark_not_run(study, {trial.number}) tells it FAIL, and its "
                    "mark then clears it (ADR 0053)",
                )
            elif not cleared_while_waiting(trial):
                stops.append(
                    f"#{trial.number} ({trial.state.name.lower()}) was marked not run while "
                    f"WAITING and {after_its_mark(trial)}, not as record_trial takes a queued trial "
                    "(told FAIL with its note), so it may have run anything; nothing clears it "
                    "(ADR 0053)",
                )
        elif (reading := origin(trial)).origin is Origin.FOREIGN:
            # Marked or not: a mark the code before wrote on such a record clears nothing.
            stops.append(
                f"#{trial.number} ({trial.state.name.lower()}) has no config key in the form "
                f"record_trial writes but {reading.why}: another writer's record, which may have "
                "run anything, and nothing clears it (ADR 0053, ADR 0054)",
            )
        elif trial.number not in marks[NOT_RUN] or trial.state != TrialState.FAIL:
            # `mark_not_run` leaves every record it puts in `not_run` `FAIL`.
            stops.append(
                f"#{trial.number} ({trial.state.name.lower()}) has no config key, so it ran no "
                f"backtest; if its process is gone, mark_not_run(study, {trial.number}) clears it "
                "(ADR 0053)",
            )
    counted = {
        key: [*by_key.get(key, []), *retired.get(key, [])]
        for key in dict.fromkeys([*by_key, *retired])
    }
    n = len(counted)
    stops.extend(
        f"{key}: tried only in another writer's queued "
        f"{'trials' if len(records) > 1 else 'trial'} {', '.join(f'#{t.number}' for t in records)}, "
        f"retired, which never ran here and is not read; its key stays counted in N, so record_trial "
        f"of its configuration on the current {sample.describe()} runs it (ADR 0054)"
        for key, records in retired.items()
        if key not in by_key
    )

    chosen: dict[str, FrozenTrial] = {}
    on_sample: dict[str, list[FrozenTrial]] = {}
    ran_on_sample: dict[
        str, list[FrozenTrial]
    ] = {}  # of those, the ones that ran, for what names them
    read: list[FrozenTrial] = []  # every record on the current sample that ran to the end
    series: dict[
        str, SessionReturns
    ] = {}  # each chosen record's returns, as the one check read them
    for key, every in by_key.items():
        # On every sample, as N counts keys on every sample: a move clears none of these.
        foreign = [
            (trial, why)
            for trial in every
            if (
                why := _config_fault(trial, key)
                or _not_the_writers(trial)
                or _ran_unconfigured(trial, sample, known)
            )
        ]
        if foreign:
            # The key's records are not all the writer's of one configuration, so none is read.
            stops.extend(
                f"{key}: {_describe(trial)} {why}: "
                + (
                    "rank again once it has finished; if it is still so then, another writer "
                    "left it, and no later record resolves it"
                    if why == _MAY_BE_RECORDING
                    else "not in the shape record_trial writes, so another writer left it, and "
                    "no later record resolves it"
                )
                + f"{_remedy(trial) if RETIRED not in malformed else ''} (ADR 0054)"
                for trial, why in foreign
            )
            continue
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
        ran = [trial for trial in records if ran_to_the_end(trial)]
        if not ran:
            stops.append(
                f"{key}: no record ran to the end ({', '.join(map(_describe, records))})",
            )
            continue
        # Every record that ran is read as returns enter the gate, once, whatever their order,
        # before any is compared with another (ADR 0010 step 2): the series with its Sharpe ratio,
        # or why it is refused.
        readings = {trial.number: _read(key, trial) for trial in ran}
        faulty = [
            (trial, faults)
            for trial in ran
            if (faults := _shape_faults(trial, sample, readings[trial.number]))
        ]
        if faulty:
            stops.extend(
                f"{key}: {_describe(trial)} is not in the shape record_trial writes on "
                f"{sample.describe()}: {'; '.join(faults)} (ADR 0041, ADR 0053)"
                for trial, faults in faulty
            )
            continue
        read.extend(ran)
        ran_on_sample[key] = ran
        # An idle record's zeros are what never trading leaves, and the stop below says so:
        # that they do not vary is no second reason. Any other refusal of its returns is.
        rejected = [
            f"{reading} (#{trial.number})"
            for trial in ran
            if isinstance(reading := readings[trial.number], ReturnsRejected)
            and not (reading.no_spread and trial.user_attrs[OUTCOME] == IDLE)
        ]
        stops.extend(rejected)  # the key's other stops are still gathered; no pick is read from it
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
        if rejected:
            continue
        reading = readings[first.number]
        if isinstance(reading, ReturnsRejected):  # named above; told apart for the type alone
            continue
        chosen[key] = first
        series[key] = reading

    stops.extend(_data_disagreements(read, sample))
    matrix = None
    if len(series) > 1:
        try:
            # In the order of the keys, not of recording: what is computed from the columns (V,
            # N-hat, which of several keys with the same returns is named) then follows no order
            # of recording, to the last bit (ADR 0080, 2026-11-11).
            matrix = returns_matrix([series[key].series for key in sorted(series)])
        except ReturnsRejected as rejected:
            named = ", ".join(f"{key} (#{chosen[key].number})" for key in rejected.names)
            stops.append(f"{rejected}\nTrials: {named}")
    # Each finite: a series whose ratio is not was refused by the one check, by name, above.
    sharpes = {key: one.sharpe for key, one in series.items()}
    if n < 2:
        stops.append(
            f"N = {n}; the DSR needs N >= 2 ({'; '.join(_named(k, r) for k, r in counted.items())})",
        )
    if stops or matrix is None:
        raise RankingStopped(_stopped(study, n, stops))

    values = [sharpes[key] for key in matrix.columns]
    everyone = "; ".join(_named(key, ran_on_sample[key]) for key in matrix.columns)
    best, worst = max(values), min(values)

    def rounding_at(ratio: float) -> float:
        """The widest bound among the keys whose Sharpe ratio is `ratio`, whatever their order."""
        return max(series[key].rounding for key in matrix.columns if sharpes[key] == ratio)

    # Two computed ratios are told apart only when they differ by more than the rounding of both
    # (ADR 0080, 2026-11-11). Judged on the set, against the highest, so no order of recording
    # changes the group: a key within rounding of a member but not of the highest is not in it.
    at_best, at_worst = rounding_at(best), rounding_at(worst)
    top = [key for key in matrix.columns if best - sharpes[key] <= at_best + series[key].rounding]
    pick = top[0]  # any of them, once every one is seen to have the same returns
    unranked = []
    if any(not np.array_equal(matrix[pick].to_numpy(), matrix[key].to_numpy()) for key in top[1:]):
        unranked.append(
            "rank within rounding of each other on returns that differ, so there is no one pick "
            "(ADR 0031): "
            + "; ".join(
                f"{_named(key, ran_on_sample[key])}, Sharpe ratio {sharpes[key]!r} give or take "
                f"{series[key].rounding:.3g}"
                for key in top
            ),
        )
    v = float(measures.variance(values))
    spread = at_best + at_worst
    if best - worst <= spread:
        # Read from the ratios: skfolio's variance of equal numbers can round above 0.
        unranked.append(
            f"V = 0: the highest Sharpe ratio, {best!r}, and the lowest, {worst!r}, are within "
            f"rounding of each other ({spread:.3g}), so eq. 1 has no spread to deflate by "
            f"(ADR 0031): {everyone}",
        )
    elif not (math.isfinite(v) and v > 0):
        unranked.append(
            f"V = {v}: skfolio's variance of the trials' Sharpe ratios is not above 0 and finite "
            f"(ADR 0031): {everyone}",
        )
    if unranked:
        # A series next to the numeric check's threshold has a bound above its own ratio: it is
        # the cause, since nothing can be told apart from it, and is named as that.
        unranked.extend(
            f"{_named(key, ran_on_sample[key])}: its returns barely vary beyond rounding at their "
            f"own size, so rounding can move its Sharpe ratio, {sharpes[key]!r}, by "
            f"{series[key].rounding:.3g}, more than the ratio itself, so no trial is told apart "
            "from it (ADR 0031)"
            for key in matrix.columns
            if series[key].rounding >= max(1.0, abs(sharpes[key]))
        )
        raise RankingStopped(_stopped(study, n, unranked))
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
        ranked_sample=sample,
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
        tied=tuple(top[1:]),
        pick_trials=tuple(sorted(t.number for key in top for t in ran_on_sample[key])),
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


def _read(key: str, trial: FrozenTrial) -> SessionReturns | ReturnsRejected:
    """`trial`'s returns on its session days with their Sharpe ratio, as the gate's one check reads
    them (`canon_trading.returns_matrix.session_returns`), or its refusal, naming `key`."""
    try:
        return session_returns(
            key, trial.user_attrs.get("returns_series"), trial.user_attrs.get("session_days")
        )
    except ReturnsRejected as rejection:
        return rejection


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


def _shape_faults(
    trial: FrozenTrial, sample: Sample, reading: SessionReturns | ReturnsRejected
) -> list[str]:
    """How `trial`, a record that ran on `sample`, differs from what `record_trial` writes there;
    `reading` is its returns as `_read` read them.

    NautilusTrader sets a run's `backtest_start` to the start it was given and its `backtest_end` to
    the engine's clock when the run ends, at or before the end it was given (`run_impl` and
    `end_impl`, `crates/backtest/src/engine.rs` at the pinned commit), so the start must be the
    sample's and the end within it. The returns and session days must fall on the run's own days, from
    the session day of its start to that of its end: a run books nothing after its clock stopped. Their
    own form is `session_returns`'s to refuse; here only their range. A value told must be the one
    `record_trial` tells, the per-session Sharpe ratio of the returns the record holds
    (`canon_trading.ledger.told_sharpe`, which reads it as `reading` was read), within what rounding
    can move a second computation of it (`SessionReturns.rounding`, the one bound the gate compares
    ratios by, ADR 0080); where its returns are refused, `record_trial` tells no value, so any
    value stops.
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
    elif (named := _instruments_of(loaded)) is None:
        faults.append(f"config names bar types {loaded}, not each in NautilusTrader's form")
    elif set(digests) != {*loaded, *named}:
        # `record_trial` digests every bar type the run loads, one with no bar included, and the
        # definition of each instrument they name (ADR 0053 term 1).
        faults.append(
            f"data_digest names {sorted(digests)}, not the bar types it loads and their "
            f"instruments, {sorted({*loaded, *named})}"
        )
    # The first day a run books a return on is the one after the account's registration, which
    # holds none; before a midnight start that is the start's own day, not `session_day`'s day
    # before it (`record_trial` refuses a bar on the registration's day).
    low, high = registration_day(sample.start) + DAY_NS, session_day(sample.end)
    ran_until = session_day(end) if end_ok else high
    held = attrs.get("session_days")
    if isinstance(held, list) and str(high) not in held:
        # `record_trial` refuses a sample whose last session day holds no bar, so its run books it.
        faults.append(
            f"session_days lacks {_day(high)}, the session day of the end of {sample.describe()}, "
            "which every run record_trial records there books"
        )
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
    if trial.values is not None:
        told = trial.values[0]
        if isinstance(reading, ReturnsRejected):
            faults.append(
                f"told {told!r} where record_trial tells no value, its returns being refused "
                f"({str(reading).removeprefix(f'{attrs.get("config_key")}: ')})"
            )
        elif not abs(told - reading.sharpe) <= reading.rounding:  # a NaN told is not within it
            faults.append(
                f"told {told!r}, not {reading.sharpe!r}, the per-session Sharpe ratio of its "
                f"returns that record_trial tells, nor within {reading.rounding:.3g} of it, what "
                "rounding can move it"
            )
    return faults


def _instruments_of(bar_types: list[str]) -> list[str] | None:
    """The instrument IDs `bar_types` name, read by NautilusTrader's `BarType.from_str`, or `None` if
    one of them is not a bar type in its form."""
    try:
        return [str(BarType.from_str(bar_type).instrument_id) for bar_type in bar_types]
    except ValueError:
        return None


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
    """One reason for each bar type that records on `sample` read as different bars, and for each
    instrument they read as different definitions (ADR 0053 term 1).

    Every record in `read` has passed `_shape_faults`, so its digest is a mapping of texts, each a bar
    type or an instrument ID.
    """
    by_digest: dict[str, dict[str, list[FrozenTrial]]] = defaultdict(lambda: defaultdict(list))
    for trial in read:
        for name, digest in trial.user_attrs["data_digest"].items():
            by_digest[name][digest].append(trial)
    return [
        f"{name}: records on {sample.describe()} read different "
        + ("bars" if _instruments_of([name]) is not None else "definitions of the instrument")
        + ": "
        + "; ".join(
            f"{digest[:12]} by {', '.join(f'#{t.number}' for t in trials)}"
            for digest, trials in digests.items()
        )
        + "; move the sample and run every configuration again (ADR 0053)"
        for name, digests in sorted(by_digest.items())
        if len(digests) > 1
    ]


def _stopped(study: optuna.Study, n: int, reasons: list[str]) -> str:
    return f"The ranking of study {study.study_name!r} (N = {n}) stopped:\n" + "\n".join(
        f"* {reason}" for reason in reasons
    )
