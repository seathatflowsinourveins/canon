"""The study of roadmap step 7: `MaCross` variants on Alpaca's daily bars, recorded and ranked.

Contract: ADR 0010's protocol, on the ledger of ADR 0035 over the sample of ADR 0041, with the grid,
the instruments, the bar type and the sample pre-registered in ADR 0048 before any run. This module is
wiring: it holds that plan as data, records each configuration through
`canon_trading.ledger.record_trial`, and ranks the study through `canon_trading.ranking.rank`. It
computes no statistic and holds no strategy logic.

The strategy is `canon_trading.ma_cross.MaCross`, shaped on the EMA cross of upstream's quickstart
(its module header says where it differs). Upstream's own built-in `EmaCross`
(`crates/trading/src/examples/strategies/ema_cross/` at the pinned commit `d53849b216e2`) subscribes
to quotes and keeps its averages of the mid; on a catalog of bars only it placed no order in a
`BacktestNode` (run, 2026-11-10), so every trial would be idle (ADR 0048).

The period pairs are the ones upstream names for its EMA cross, at the same commit: fast 10 and slow
20, the defaults of the bar-based `EMACrossConfig` in `docs/tutorials/ema_cross.py` and of
`MaCrossConfig`; and 5 and 20, 10 and 50, 20 and 100, the "Common period pairs" table of
`crates/trading/src/examples/strategies/ema_cross/README.md`. They are upstream's, not chosen here.

Bars come from the catalog only. `tests/test_paper_variant_study_bars.py` loads them through
`canon_trading.bars_to_catalog.load_bars` under `mise run paper:test` (ADR 0040); recording and
ranking read no credential and call nothing outside the process.

Run `python -m canon_trading.variant_study --create` once to create the study with its sample, then
`python -m canon_trading.variant_study` to record and rank. A pass records every configuration of the
grid whose latest record on the current sample did not run to the end, the same rule the gate uses to
resolve a key (ADR 0054 term 5), then ranks and prints the report or the stop. `--rank-only` ranks
without recording. Either way the study is ranked only once every configuration of the plan has a
record: `rank` counts N from the ledger and cannot see a planned configuration never tried, so a
pass stopped part way would be ranked at an N short of the plan's (ADR 0010 step 1, ADR 0048). A run
that raises goes into the pass's failures only once `record_trial` has told it `FAIL` with its error
(a refused `tell` after a run included, which keeps the run's outcome, and both its `tell`s refused,
which leaves it `RUNNING` with its outcome and error); one whose
failure could not be written stops the pass, with the storage error as a note on the run's own. A
pass holds an exclusive `fcntl.flock` (the standard library's, as
`canon_trading.ledger` takes for creation) on a lock file beside the ledger, named for the study
(percent-encoded, so a name holding `/` names one file), so a second pass on the same study is
refused rather than run beside it. A pass that only opens a study looks for the ledger before it takes
that lock, and creates no folder and no file where there is none. The catalog defaults to the
`catalog/` of the checkout this module is in, where the paper test loads the bars; the ledger to
`canon_trading.ledger.default_ledger_path()`, shared by every worktree (ADR 0035).
"""

from __future__ import annotations

import argparse
import fcntl
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from canon_alpaca.constants import VENUE
from canon_alpaca.data import NEW_YORK
from nautilus_trader.common import LogLevel
from nautilus_trader.config import LoggerConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity
from optuna.trial import TrialState

from .ledger import FAILED
from .ledger import OUTCOME
from .ledger import RAN
from .ledger import LedgerNotFound
from .ledger import config_key
from .ledger import create_ledger_study
from .ledger import current_sample
from .ledger import default_ledger_path
from .ledger import has_key
from .ledger import open_study
from .ledger import record_trial
from .ledger import retired_numbers
from .ledger import samples
from .ledger import trial_config
from .ma_cross import MaCrossConfig
from .ranking import RankingReport
from .ranking import RankingStopped
from .ranking import on_current_sample
from .ranking import ran_to_the_end
from .ranking import rank

if TYPE_CHECKING:
    from collections.abc import Generator
    from collections.abc import Sequence

    import optuna

CATALOG = Path(__file__).resolve().parents[2] / "catalog"
"""The `catalog/` of this checkout, where `tests/test_paper_variant_study_bars.py` loads the bars."""

# The plan pre-registered in ADR 0048. Changing any of it is a new study, not an edit of this one.
STUDY = "step7-ma-cross-daily-spy-qqq-2020-07-to-2025"
SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")
PERIODS: tuple[tuple[int, int], ...] = ((10, 20), (5, 20), (10, 50), (20, 100))
TRADE_SIZE = Quantity.from_int(100)
STARTING_BALANCES: tuple[str, ...] = ("100_000 USD",)
# A daily bar's `ts_init` is the next midnight New York, 24 hours after its open (`canon_alpaca.data`;
# ADR 0040). Noon on 2020-07-27 to noon on 2026-01-01 holds the bars of the trading days 2020-07-27 to
# 2025-12-31, and the load and the sample use the same window, so the catalog covers the whole sample.
# The plan's start was 2020-01-02; Alpaca's IEX daily bars of both symbols begin on 2020-07-27 (loaded
# 2026-11-10, before any trial), so the sample starts on that first common day (ADR 0048).
WINDOW_START = datetime(2020, 7, 27, 12, tzinfo=NEW_YORK)
WINDOW_END = datetime(2026, 1, 1, 12, tzinfo=NEW_YORK)


def bar_type(symbol: str) -> BarType:
    """The bar type of `symbol` the adapter serves: daily, last price, from Alpaca (ADR 0040)."""
    return BarType.from_str(f"{symbol}.{VENUE}-1-DAY-LAST-EXTERNAL")


def grid(
    symbols: Sequence[str] = SYMBOLS, periods: Sequence[tuple[int, int]] = PERIODS
) -> list[MaCrossConfig]:
    """One `MaCrossConfig` per configuration of the grid, instrument first, in the plan's order.

    Each configuration is one trial: a run of one strategy on one instrument.
    """
    return [
        MaCrossConfig(
            bar_type=bar_type(symbol),
            trade_size=TRADE_SIZE,
            fast_period=fast,
            slow_period=slow,
        )
        for symbol in symbols
        for fast, slow in periods
    ]


def key_of(config: MaCrossConfig, starting_balances: Sequence[str] = STARTING_BALANCES) -> str:
    """The ledger's config key of a one-strategy trial of `config` (ADR 0053 term 2)."""
    return config_key(trial_config([config], starting_balances=starting_balances))


def _to_record(study: optuna.Study, keys: Sequence[str]) -> list[str]:
    """The keys among `keys` whose latest record on the current sample did not run to the end.

    A key is resolved for the gate when every record that did not run to the end has a later one that
    did (ADR 0054 term 5, `canon_trading.ranking`), which holds exactly when its latest record ran. So
    a key with no record on the sample, or whose latest record failed or is still `RUNNING` with no
    outcome of a run, is run again; one left `RUNNING` with outcome traded or idle, its `tell`
    refused after the run, ran to the end and is not. Records are placed on samples as `rank` places them (ADR 0041 term 5): a keyed one with no
    sample number of the study's is on the current sample, unless it ran to the end, which `rank`
    stops on as another writer's and which no re-run clears. The rule is the gate's own,
    `canon_trading.ranking.on_current_sample` and `ran_to_the_end`, so it follows any change to it.
    A retired record (`canon_trading.ledger.retire_queued`) is another writer's, which `rank` never
    reads, so it is no run of its key either; a `retired` list not in its form raises
    `ValueError` (`retired_numbers`), a refusal before any trial, which stops the pass. The study is
    read once.
    """
    history = samples(study)  # one read, as `rank` takes it
    sample = history[-1]
    known = {s.number for s in history}
    retired = retired_numbers(study)
    latest: dict[str, optuna.trial.FrozenTrial] = {}
    for trial in study.get_trials(deepcopy=False):
        key = trial.user_attrs.get("config_key")
        if not isinstance(key, str) or key not in keys or trial.number in retired:
            continue
        if on_current_sample(trial, sample, known) and (
            key not in latest or trial.number > latest[key].number
        ):
            latest[key] = trial
    return [key for key in keys if key not in latest or not ran_to_the_end(latest[key])]


@dataclass(frozen=True)
class Recorded:
    """What one pass over the grid did: trial numbers recorded, keys skipped, runs that failed."""

    recorded: tuple[int, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


def _told_failed(study: optuna.Study, key: str, before: int) -> bool:
    """Whether a record of `key` numbered `before` or later holds its failure as `record_trial` wrote it.

    `record_trial` tells a raised run `FAIL` with outcome `failed`, and a run whose `tell` was refused
    after its outcome `FAIL` with that outcome kept (`traded` or `idle`); either way it writes the
    error (`canon_trading.ledger._tell_failed`), and the failure is in the ledger for the gate to read.
    When that `FAIL` is refused too, a run that ran to the end stays `RUNNING` with its outcome and
    its error written: every result is there, and the gate reads it as the run it was
    (`canon_trading.ranking.ran_to_the_end`), so it is a written failure as well. Any other record
    still `RUNNING`, or one with no error written, is a failure that could not be told, and stops the
    pass.
    """
    return any(
        trial.number >= before
        and trial.user_attrs.get("config_key") == key
        and "error" in trial.user_attrs
        and (
            (trial.state == TrialState.FAIL and trial.user_attrs.get(OUTCOME) in (FAILED, *RAN))
            or (trial.state == TrialState.RUNNING and trial.user_attrs.get(OUTCOME) in RAN)
        )
        for trial in study.get_trials(deepcopy=False)
    )


def record_grid(
    study: optuna.Study,
    catalog_path: Path,
    configs: Sequence[MaCrossConfig],
    *,
    starting_balances: Sequence[str] = STARTING_BALANCES,
) -> Recorded:
    """Record each configuration `_to_record` names, one trial each, in the plan's order.

    A trial `record_trial` told `FAIL` with its error (`_told_failed`) is noted among the failures and
    the pass goes on, so every configuration of the plan is tried: a run that raised, and also a
    result it could not record or bars that differ from those its check read, once the failure itself
    was written. A run whose `tell` alone was refused, after its outcome, is noted there too, for the
    storage error, though it ran to the end and the gate reads its run. That is safe for the gate:
    the ranking stops on the key until a later record of it runs to the end (ADR 0054 term 5), and a
    catalog changed mid-pass leaves records that read different bars, which stop it too (ADR 0053
    term 1). Anything that leaves no failure told (a refusal before a trial exists, a failure that
    could not be written, a defect here) stops the pass.
    """
    keys = [key_of(config, starting_balances) for config in configs]
    pending = set(_to_record(study, keys))
    quiet = LoggerConfig(stdout_level=LogLevel.ERROR)
    recorded, skipped, failed = [], [], []
    for config, key in zip(configs, keys, strict=True):
        if key not in pending:
            skipped.append(key)
            continue
        before = len(study.get_trials(deepcopy=False))
        try:
            trial = record_trial(
                study, catalog_path, [config], starting_balances=starting_balances, logging=quiet
            )
        except Exception as error:
            if not _told_failed(study, key, before):
                raise
            failed.append(f"{describe(config)}: {error!r}")
            continue
        recorded.append(trial.number)
    return Recorded(tuple(recorded), tuple(skipped), tuple(failed))


def describe(config: MaCrossConfig) -> str:
    return f"{config.bar_type.instrument_id.symbol} {config.fast_period}/{config.slow_period}"


def legend(
    configs: Sequence[MaCrossConfig], starting_balances: Sequence[str] = STARTING_BALANCES
) -> dict[str, str]:
    """Each configuration's key, mapped to its instrument and periods, for reading a report."""
    return {key_of(config, starting_balances): describe(config) for config in configs}


def run_study(
    catalog_path: Path = CATALOG,
    *,
    name: str = STUDY,
    ledger_path: Path | None = None,
    configs: Sequence[MaCrossConfig] | None = None,
    start: datetime = WINDOW_START,
    end: datetime = WINDOW_END,
    create: bool = False,
    rank_only: bool = False,
) -> tuple[Recorded | None, RankingReport | RankingStopped]:
    """Create the study if `create`, else open it; record the grid unless `rank_only`; rank it.

    Creating a study that exists raises Optuna's `DuplicatedStudyError`, and opening one that does not
    raises `LedgerNotFound`, each from `canon_trading.ledger`: nothing is created unless asked. A study
    whose current sample is not [start, end] is refused, since the sample is part of the plan. The
    pass holds the study's pass lock (module header). Returns what the pass recorded and the report, or
    the `RankingStopped` that stopped it, which names any configuration of `configs` with no record.
    """
    configs = grid() if configs is None else configs
    first, last = dt_to_unix_nanos(start), dt_to_unix_nanos(end)
    ledger = default_ledger_path() if ledger_path is None else Path(ledger_path).resolve()
    if not create and not ledger.is_file():
        # Before the pass lock, whose file would be created beside a ledger that is not there.
        raise LedgerNotFound(f"there is no ledger at {ledger}; create_ledger_study creates one")
    with _pass_lock(ledger, name, create=create):
        if create:
            opened = create_ledger_study(name, start=first, end=last, path=ledger)
        else:
            opened = open_study(name, ledger)
        with opened as study:
            return _record_and_rank(study, catalog_path, configs, first, last, rank_only)


@contextmanager
def _pass_lock(ledger: Path, name: str, *, create: bool) -> Generator[None]:
    """Hold an exclusive, non-blocking `flock` on `<ledger>.<name>.pass.lock`; refuse if it is held.

    The name is percent-encoded with no character left safe (`urllib.parse.quote`, as the ledger
    encodes its SQLite path), so a study name holding `/` names one file beside the ledger. On a file
    system that folds case, two names that differ only in case share one lock, and a pass on one
    refuses a pass on the other, which runs nothing twice. The ledger's folder is made only for a pass
    that creates the study.
    """
    if create:
        ledger.parent.mkdir(parents=True, exist_ok=True)
    with (ledger.parent / f"{ledger.name}.{quote(name, safe='')}.pass.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another pass over study {name!r} holds its lock") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _record_and_rank(
    study: optuna.Study,
    catalog_path: Path,
    configs: Sequence[MaCrossConfig],
    first: int,
    last: int,
    rank_only: bool,
) -> tuple[Recorded | None, RankingReport | RankingStopped]:
    sample = current_sample(study)
    if (sample.start, sample.end) != (first, last):
        raise ValueError(
            f"study {study.study_name!r} is on {sample.describe()}, not the planned range "
            f"{first} to {last}",
        )
    recorded = None if rank_only else record_grid(study, catalog_path, configs)
    unrecorded = _never_recorded(study, configs)
    try:
        report = rank(study)
    except RankingStopped as stopped:
        if not unrecorded:
            return recorded, stopped
        return recorded, RankingStopped(f"{_short_of_the_plan(study, unrecorded)}\n{stopped}")
    if unrecorded:
        return recorded, RankingStopped(_short_of_the_plan(study, unrecorded))
    return recorded, report


def _never_recorded(study: optuna.Study, configs: Sequence[MaCrossConfig]) -> list[MaCrossConfig]:
    """The configurations of the plan with no record of their key in `study`, on any sample.

    `rank` counts N from the ledger (ADR 0010 step 1), so it cannot see a planned configuration that
    was never tried: a pass stopped part way leaves a study whose N is short of the plan's, and a
    smaller N deflates the pick's Sharpe ratio less. The plan is ADR 0048's, so the study is not
    ranked until every configuration of it has a record. A key with a record that did not run to the
    end, or one on another sample only, `rank` stops on itself.
    """
    keys = {
        trial.user_attrs["config_key"]
        for trial in study.get_trials(deepcopy=False)
        if has_key(trial)
    }
    return [config for config in configs if key_of(config) not in keys]


def _short_of_the_plan(study: optuna.Study, configs: Sequence[MaCrossConfig]) -> str:
    return (
        f"The ranking of study {study.study_name!r} stopped: {len(configs)} configurations of "
        "the plan (ADR 0048) have no record, so N would be short of the plan's; record the grid "
        "first:\n"
        + "\n".join(
            f"* {key_of(config)} ({describe(config)}): planned but never recorded"
            for config in configs
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canon_trading.variant_study")
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--ledger", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--create", action="store_true", help="create the study first")
    mode.add_argument("--rank-only", action="store_true", help="rank without recording")
    args = parser.parse_args(argv)
    configs = grid()
    recorded, outcome = run_study(
        args.catalog,
        ledger_path=args.ledger,
        configs=configs,
        create=args.create,
        rank_only=args.rank_only,
    )
    if recorded is not None:
        print(f"Recorded trials {recorded.recorded}; already run: {len(recorded.skipped)}")
        for failure in recorded.failed:
            print(f"Failed: {failure}")
    print("Configurations:")
    for key, text in legend(configs).items():
        print(f"  {key}  {text}")
    if isinstance(outcome, RankingStopped):
        print(outcome)
        return 2
    print(outcome.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
