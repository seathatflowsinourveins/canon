"""The trial ledger: every backtest meant as a trial, recorded as one trial of an Optuna study.

Contract: ADR 0010, protocol step 1 ("Ledger every trial"), in the store and on the terms ADR 0035
decides, over the sample ADR 0041 fixes for each study. Optuna 5.0.0, read 2026-11-10 at tag v5.0.0:

* `docs/source/faq.rst` ("save and resume studies"): `RDBStorage` on a local SQLite file.
  `optuna.create_study` without `load_if_exists` creates a study and raises `DuplicatedStudyError` for
  a name already taken; `optuna.load_study` opens one and raises `KeyError` for a name not there.
  `RDBStorage(..., skip_table_creation=True)` creates no tables in the file it opens.
* `tutorial/20_recipes/009_ask_and_tell.py`, and `Study.ask` and `Study.tell` in
  `optuna/study/study.py`: a trial is asked for before the run and told its state after; `tell` takes
  a value for `COMPLETE` and none for `FAIL`. Under ask and tell nothing upstream marks a raised run
  `FAIL`, so `record_trial` tells it.
* `Trial.set_user_attr` and `Study.set_user_attr` store a JSON-serialisable value (`value_json` is
  `Text` in `optuna/storages/_rdb/models.py`), and `Study.trials` returns every trial in every state.

The SQLite file is opened through a URI filename (https://www.sqlite.org/uri.html): the path is
percent-encoded, and `mode=rw` refuses a file that does not exist where `mode=rwc` creates it. The URI
goes in SQLAlchemy's URL with `uri=true` (SQLAlchemy 2.1.8, `dialects/sqlite/pysqlite.py`, "URI
Connections"); SQLAlchemy percent-decodes the database part of a URL (`engine/url.py`, `_parse_url`), so
each `%` is encoded once more for it.

NautilusTrader v2.0.0rc8 binds no config identity to Python: `BacktestRunConfig.id` defaults to a fresh
UUID4 (`crates/backtest/src/config.rs` at the pinned commit) and the config cannot be serialised. So the
identity is glue, ADR 0035 term 4: `config_key` is the SHA-256 of the canonical JSON of `trial_config`,
built from what this repo sets, and the run is built with it as its `id`. Nothing upstream identifies
the data a run read either, so `data_digest` is glue too (ADR 0041).

The value told for a run that traded is skfolio's `Portfolio.sharpe_ratio` (1.12.8,
`portfolio/_base.py`) over the run's own series as `canon_trading.returns_matrix.daily_series` accepts
it: mean over sample standard deviation, per session. It is there for a later sampler;
`canon_trading.ranking` does not read it.

First creation of a ledger file is serialised with an exclusive `fcntl.flock` (the standard library's
`fcntl`, POSIX `flock(2)`) on `trials.db.lock` beside it. Optuna's `RDBStorage` runs SQLAlchemy's
`create_all` on construction, and two processes creating one new file raced on it: 14 of 15 rounds of
two processes crashed with "table studies already exists" (measured on this machine, 2026-11-10). The
lock guards first creation only, not recording; Optuna's FAQ keeps `JournalFileBackend` for parallel
optimisation, and ADR 0035 keeps `RDBStorage` on SQLite for the reasons it gives.

A session day (ADR 0035 term 3) is a UTC day on which a bar of the run became available, its `ts_init`,
a bar at exactly 00:00 UTC counting for the day ending there, as the daily series books it; read with
`ParquetDataCatalog.query` (the installed `nautilus_trader/persistence/__init__.pyi`) from the catalog
the run read, over the study's sample. `BacktestResult` has no such list.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import subprocess
import traceback
from contextlib import ExitStack
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote

import optuna
import pandas as pd
from canon_alpaca.constants import VENUE
from nautilus_trader.model import Money
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from optuna.trial import TrialState

from .backtest import FEE_RATES
from .backtest import VENUE_SETTINGS
from .backtest import backtest_run_config
from .backtest import run_backtest_config
from .ma_cross import MaCross
from .returns_matrix import DAY_NS
from .returns_matrix import ReturnsRejected
from .returns_matrix import daily_series
from .returns_matrix import session_sharpe

if TYPE_CHECKING:
    from collections.abc import Generator
    from collections.abc import Sequence

    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import Bar
    from optuna.trial import FrozenTrial

    from .backtest import BacktestRun
    from .ma_cross import MaCrossConfig

LEDGER_FILE = Path("ledger") / "trials.db"

# The user attribute holding a trial's outcome, and its three values (ADR 0035 term 2).
OUTCOME = "outcome"
TRADED = "traded"
IDLE = "idle"
FAILED = "failed"

# The study user attributes holding the study's samples, oldest first, and the numbers of records
# that ran no backtest (ADR 0041).
SAMPLES = "samples"
NOT_RUN = "not_run"
STRATEGY_CLASS = f"{MaCross.__module__}:{MaCross.__qualname__}"


class LedgerNotFound(LookupError):
    """There is no ledger, or no such study in it, where one was asked for; nothing was created."""


@dataclass(frozen=True)
class Sample:
    """A study's sample (ADR 0041): its sequence number and its range, UTC nanoseconds, both included."""

    number: int
    start: int
    end: int

    def describe(self) -> str:
        return f"sample {self.number} ({_date(self.start)} to {_date(self.end)})"


def default_ledger_path() -> Path:
    """`ledger/trials.db` in the main checkout of the clone this module was loaded from (ADR 0035).

    The main checkout is the directory holding the clone's common git directory
    (`git rev-parse --path-format=absolute --git-common-dir`), so every worktree of the clone resolves
    the same file, whatever the current directory. Outside a git checkout there is no default.
    """
    here = Path(__file__).resolve().parent
    try:
        common = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise LedgerNotFound(
            f"{here} is not in a git checkout, so the ledger has no default location; pass its path",
        ) from error
    return Path(common).parent / LEDGER_FILE


def _sqlite_url(path: Path, mode: str) -> str:
    """The SQLAlchemy URL of the SQLite file at `path`, opened with `mode` (module header)."""
    return f"sqlite:///file:{quote(str(path), safe='/').replace('%', '%25')}?mode={mode}&uri=true"


@contextmanager
def _storage(path: Path, mode: str) -> Generator[optuna.storages.RDBStorage]:
    """The `RDBStorage` of `path`; on leaving, its session removed and its engine disposed."""
    storage = optuna.storages.RDBStorage(_sqlite_url(path, mode), skip_table_creation=mode == "rw")
    try:
        yield storage
    finally:
        storage.remove_session()
        storage.engine.dispose()


def _ledger(path: Path | None) -> Path:
    return default_ledger_path() if path is None else Path(path).resolve()


@contextmanager
def create_ledger_study(
    name: str,
    *,
    start: int | str | pd.Timestamp,
    end: int | str | pd.Timestamp,
    path: Path | None = None,
) -> Generator[optuna.Study]:
    """Create the study `name` over the sample [start, end] and open it; create the file if need be.

    ADR 0041. A study of that name already in the file raises Optuna's `DuplicatedStudyError`. `start`
    and `end` are UTC nanoseconds, or a date or time that `pandas.Timestamp` reads, taken as UTC when it
    has no offset. Both are instants and both are included: a date with no time is its midnight, so an
    `end` of `"2025-06-16"` leaves out a bar of that day stamped after 00:00. With no `path`, the ledger
    is `default_ledger_path()`.
    """
    sample = Sample(0, *_range(start, end))
    path = _ledger(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as opened:
        with (path.parent / f"{path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                storage = opened.enter_context(_storage(path, "rwc"))
                study = optuna.create_study(study_name=name, storage=storage, direction="maximize")
                study.set_user_attr(SAMPLES, [_sample_attr(sample)])
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        yield study


@contextmanager
def open_study(name: str, path: Path | None = None) -> Generator[optuna.Study]:
    """Open the study `name` in the ledger at `path`; never create a file, a table or a study.

    With no `path`, the ledger is `default_ledger_path()`. A missing file or study raises
    `LedgerNotFound`, and so do a file that is not an Optuna ledger and a study with no sample. On leaving the `with` block the storage's
    session is removed (`RDBStorage.remove_session`) and its SQLAlchemy engine disposed, so no SQLite
    connection is left for the garbage collector.
    """
    path = _ledger(path)
    if not path.is_file():
        raise LedgerNotFound(f"there is no ledger at {path}; create_ledger_study creates one")
    with ExitStack() as opened:
        try:
            with _creation_settled(path):
                storage = opened.enter_context(_storage(path, "rw"))
                study = optuna.load_study(study_name=name, storage=storage)
        except KeyError:
            raise LedgerNotFound(
                f"the ledger at {path} holds no study {name!r}; create_ledger_study creates one",
            ) from None
        except optuna.exceptions.StorageInternalError as error:
            raise LedgerNotFound(f"the file at {path} is not an Optuna ledger") from error
        samples(study)
        yield study


@contextmanager
def _creation_settled(path: Path) -> Generator[None]:
    """Wait out a first creation in progress: a shared `flock` on the creation lock, if it exists.

    A reader never creates the lock file. The creator makes it before the ledger file, so a ledger
    still being created always has one. The shared lock is held only while the study is loaded.
    """
    try:
        lock = (path.parent / f"{path.name}.lock").open("r")
    except FileNotFoundError:
        yield
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def samples(study: optuna.Study) -> list[Sample]:
    """Every sample `study` has had, oldest first; the last is the current one (ADR 0041)."""
    stored = study.user_attrs.get(SAMPLES)
    try:
        found = [
            Sample(int(s["number"]), int(s["start"]), int(s["end"]))
            for s in (stored if isinstance(stored, list) else [])
        ]
    except TypeError, KeyError, ValueError:
        found = []
    if not found or [s.number for s in found] != list(range(len(found))):
        raise LedgerNotFound(
            f"study {study.study_name!r} has no sample in the form ADR 0041 gives: {stored!r}",
        )
    return found


def current_sample(study: optuna.Study) -> Sample:
    return samples(study)[-1]


def move_sample(
    study: optuna.Study, *, start: int | str | pd.Timestamp, end: int | str | pd.Timestamp
) -> Sample:
    """Give `study` a new current sample, numbered after the last; every record stays (ADR 0041).

    The same range again is a new sample too: that is how a corrected catalog is taken in. Every
    configuration the study has tried must then be run on the new sample before it can be ranked.
    """
    history = samples(study)
    sample = Sample(history[-1].number + 1, *_range(start, end))
    study.set_user_attr(SAMPLES, [*map(_sample_attr, history), _sample_attr(sample)])
    return sample


def _range(start: int | str | pd.Timestamp, end: int | str | pd.Timestamp) -> tuple[int, int]:
    first, last = _ns(start), _ns(end)
    if not first < last:
        raise ValueError(f"a sample needs start < end, got {_date(first)} and {_date(last)}")
    return first, last


def _ns(value: int | str | pd.Timestamp) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return int(stamp.value)


def _date(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()


def _sample_attr(sample: Sample) -> dict[str, int]:
    return {"number": sample.number, "start": sample.start, "end": sample.end}


def _canonical(value: object) -> Any:
    """`value` in the canonical form of ADR 0035 term 4, or `TypeError` if it has no stable one.

    A `Decimal` is normalised (`0`, `0.00` and `0E-2` all give `0`). A `Quantity`, `Price` or `Money`
    prints with its precision, which is not normalised away: an order size of `10.0` is refused by an
    instrument that takes whole shares, where `10` runs, so the two are different configurations.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value  # `json.dumps` writes a float by `repr` and refuses NaN and infinities
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return "0" if text == "-0" else text
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        canonical = {str(_canonical(key)): _canonical(item) for key, item in value.items()}
        if len(canonical) != len(value):
            raise TypeError(f"{value!r} has keys that share one text form")
        return canonical
    if type(value).__module__.startswith("nautilus_trader") and not callable(value):
        # Enums print by name (`NETTING`); value types and identifiers as themselves
        # (`AAPL.ALPACA-1-DAY-LAST-EXTERNAL`). A Rust struct prints by `Debug`, braces included, and a
        # class with no text form of its own as `<... object at 0x...>`, an address: both refused.
        text = str(value)
        if "{" not in text and not text.startswith("<"):
            return text
    raise TypeError(f"{type(value).__name__} {value!r} has no stable text form for a config key")


def trial_config(
    strategies: Sequence[MaCrossConfig], *, starting_balances: Sequence[str]
) -> dict[str, Any]:
    """The object ADR 0035 term 4 hashes: what this repo sets for a run, normalised.

    Each strategy's class and the fields its config sets (its own attributes; the base config's are
    upstream defaults `MaCrossConfig` never passes on), the venue's name, `VENUE_SETTINGS`, `FEE_RATES`
    and starting balances, and the bar types the run loads, one per instrument as
    `backtest_run_config` derives them. No property upstream reflects is read, so a default that
    changes with NautilusTrader's pin moves no key. The study's sample is not in it (ADR 0041).
    """
    bar_types = list(dict.fromkeys(str(config.bar_type) for config in strategies))
    return {
        "strategies": [
            {
                "class": STRATEGY_CLASS,
                "config": {
                    name: _canonical(value)
                    for name, value in sorted(vars(config).items())
                    if not name.startswith("_")
                },
            }
            for config in strategies
        ],
        "venue": {
            "name": str(VENUE),
            **{name: _canonical(value) for name, value in VENUE_SETTINGS.items()},
            "fee_model": {
                "class": "MakerTakerFeeModel",
                **{name: _canonical(rate) for name, rate in FEE_RATES.items()},
            },
            "starting_balances": [
                _canonical(Money.from_str(balance)) for balance in starting_balances
            ],
        },
        "data": {"data_type": "Bar", "bar_types": bar_types},
    }


def canonical_json(config: dict[str, Any]) -> str:
    return json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def config_key(config: dict[str, Any]) -> str:
    """The SHA-256 hex digest of the UTF-8 bytes of `canonical_json(config)`."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def bars_read(catalog_path: Path, bar_types: Sequence[str], start: int, end: int) -> list[Bar]:
    """The bars of `bar_types` in the catalog with `ts_init` in [start, end], in a stable order.

    The catalog is asked for that range only (`ParquetDataCatalog.query`, `start` and `end`, the bounds
    the node is given); the bars it returns are checked against the range again by their `ts_init`.
    """
    catalog = ParquetDataCatalog(str(Path(catalog_path).resolve()))
    bars = catalog.query(NautilusDataType.Bar, identifiers=list(bar_types), start=start, end=end)
    kept = [bar for bar in bars if start <= bar.ts_init <= end]
    return sorted(kept, key=lambda bar: (str(bar.bar_type), bar.ts_init, bar.ts_event))


def session_day(ts: int) -> int:
    """The UTC day, its midnight in nanoseconds, that NautilusTrader's daily series books `ts` on.

    The day that holds `ts`, except that an instant exactly at a UTC midnight belongs to the day ending
    there: `snapshot_day_start` in `crates/analysis/src/analyzer.rs` at the pinned commit moves a
    non-zero midnight back one day, and the series is built from those snapshots.
    """
    day = ts // DAY_NS * DAY_NS
    return day - DAY_NS if ts == day and ts != 0 else day


def session_days(bars: Sequence[Bar]) -> list[str]:
    """The UTC days, as nanoseconds in text, on which one of `bars` became available (`ts_init`)."""
    return [str(day) for day in sorted({session_day(bar.ts_init) for bar in bars})]


def data_digest(bars: Sequence[Bar]) -> dict[str, str]:
    """ADR 0041's identity of the data a run read: for each bar type, a SHA-256 of its bars.

    The digest of a bar type is that of the UTF-8 bytes of one line per bar, in `bars`' order,
    `bar_type|open|high|low|close|volume|ts_event|ts_init`, the prices and the volume as NautilusTrader
    prints them (their precision included), joined by `\\n`. It is taken from the bars themselves, not
    from the catalog's files, so rewriting or consolidating files moves no digest. Kept per bar type, so
    two trials on one sample that read one bar type can be checked to have read the same bars.
    """
    lines: dict[str, list[str]] = {}
    for bar in bars:
        lines.setdefault(str(bar.bar_type), []).append(
            f"{bar.bar_type}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}|"
            f"{bar.ts_event}|{bar.ts_init}",
        )
    return {
        bar_type: hashlib.sha256("\n".join(found).encode("utf-8")).hexdigest()
        for bar_type, found in sorted(lines.items())
    }


@dataclass(frozen=True)
class RecordedTrial:
    """A trial as recorded: its number in the study, its config key, its outcome and the run."""

    number: int
    config_key: str
    outcome: str
    run: BacktestRun


def record_trial(
    study: optuna.Study,
    catalog_path: Path,
    strategies: Sequence[MaCrossConfig],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
) -> RecordedTrial:
    """Build a `MaCross` from each config and run them over the study's sample, as one trial.

    ADR 0035 terms 2 and 3 and ADR 0041. The key is computed from the configs first, and the strategies
    are built from them here, so what is hashed is what runs: a config changed afterwards changes
    nothing. What `backtest_run_config` refuses, or a value with no stable text form, raises before
    anything is recorded: such a call is not a trial. The trial is then asked for, so a run that never
    returns stays in the study as `RUNNING`. The run is bounded by the study's current sample and built
    with the key as its `id`. A run that raises, or whose result cannot be recorded, is told `FAIL` with
    outcome `failed` and its error, and the exception passes through. A run with no filled order is told
    `FAIL` with outcome `idle`; any other `COMPLETE` with `traded`, or `FAIL`, still `traded`, if its
    Sharpe ratio is not finite. The outcome is written last, so a record that has one ran to the end.
    Every write after the trial is asked for, the outcome and the `tell` included, is inside the `try`
    that tells a failure, so an error or `KeyboardInterrupt` there is told `FAIL` with its error. Only
    a process killed outright, or a store that refuses the very first write, leaves a record with no
    key; no backtest ran for it, and `mark_not_run` clears it (ADR 0041). Nothing is overwritten: the
    same configuration again is a new record.
    """
    sample = current_sample(study)
    config = trial_config(strategies, starting_balances=starting_balances)
    key = config_key(config)
    built = [MaCross(strategy) for strategy in strategies]
    if config_key(trial_config(strategies, starting_balances=starting_balances)) != key:
        raise RuntimeError("a strategy config changed while its strategies were built")
    run_config = backtest_run_config(
        catalog_path,
        built,
        starting_balances=starting_balances,
        logging=logging,
        run_config_id=key,
        start=sample.start,
        end=sample.end,
    )
    bar_types = [str(bar_type) for data in run_config.data for bar_type in data.bar_types or ()]
    if bar_types != config["data"]["bar_types"]:
        raise RuntimeError(f"the run loads {bar_types}, not the bar types its key names")

    trial = study.ask()
    try:
        # The key first, so the shortest stretch of a record has none; the run starts only after it.
        trial.set_user_attr("config_key", key)
        trial.set_user_attr("sample", sample.number)
        trial.set_user_attr("config", config)
        run = run_backtest_config(run_config, built)
        result = run.result
        returns = {str(ns): value for ns, value in sorted(result.returns_series.items())}
        bars = bars_read(catalog_path, bar_types, sample.start, sample.end)
        days = session_days(bars)
        trial.set_user_attr("run_config_id", result.run_config_id)
        trial.set_user_attr("backtest_start", result.backtest_start)
        trial.set_user_attr("backtest_end", result.backtest_end)
        trial.set_user_attr("returns_series", returns)
        trial.set_user_attr("session_days", days)
        trial.set_user_attr("data_digest", data_digest(bars))
        traded = any(order.filled_qty.is_positive() for order in run.orders)
        sharpe = _session_sharpe(returns, days) if traded else math.nan
        outcome = TRADED if traded else IDLE
        trial.set_user_attr(OUTCOME, outcome)
        if math.isfinite(sharpe):
            study.tell(trial, sharpe)
        else:
            study.tell(trial, state=TrialState.FAIL)
    except BaseException as error:
        _tell_failed(study, trial, error)
        raise
    return RecordedTrial(number=trial.number, config_key=key, outcome=outcome, run=run)


def mark_not_run(study: optuna.Study, number: int) -> None:
    """Record that trial `number` of `study`, a record with no config key, ran no backtest (ADR 0041).

    `record_trial` writes the key before it starts a run, so a record without one ran nothing: it is
    what a process killed between `ask` and that write leaves. The record is kept; a `RUNNING` one is
    told `FAIL`, through `Study.tell`, which takes a trial number. Its number joins the study user
    attribute `not_run`, and the ranking leaves it out of N. A record that has a key is refused.
    Use it only on a record whose process is gone: a live one would have its run told `FAIL`.
    """
    found = [trial for trial in study.get_trials(deepcopy=False) if trial.number == number]
    if not found:
        raise LookupError(f"study {study.study_name!r} has no trial #{number}")
    [trial] = found
    if has_key(trial):
        raise ValueError(
            f"trial #{number} has config key {trial.user_attrs['config_key']}: it was tried, and only"
            " a later record of that key resolves it",
        )
    if ran_something(trial):
        raise ValueError(
            f"trial #{number} has no config key but shows a run (outcome "
            f"{trial.user_attrs.get(OUTCOME)!r}): it is a trial of an unknown configuration",
        )
    if not trial.state.is_finished():
        study.tell(number, state=TrialState.FAIL)
    marked = study.user_attrs.get(NOT_RUN, [])
    if number not in marked:
        study.set_user_attr(NOT_RUN, sorted([*marked, number]))


def has_key(trial: FrozenTrial) -> bool:
    """Whether `trial` holds a config key in the form `record_trial` writes, a non-empty text."""
    key = trial.user_attrs.get("config_key")
    return isinstance(key, str) and bool(key)


def ran_something(trial: FrozenTrial) -> bool:
    """Whether `trial` shows a run: an outcome of traded or idle, or returns or session days."""
    attrs = trial.user_attrs
    return (
        attrs.get(OUTCOME) in (TRADED, IDLE) or "returns_series" in attrs or "session_days" in attrs
    )


def _tell_failed(study: optuna.Study, trial: optuna.Trial, error: BaseException) -> None:
    """Tell `trial` `FAIL` with outcome `failed`; if that cannot be written, say so on `error`.

    A trial already told, as when an interrupt lands just after its `tell`, is left as it is, and the
    note says so.
    """
    [frozen] = [t for t in study.get_trials(deepcopy=False) if t.number == trial.number]
    if frozen.state.is_finished():
        error.add_note(f"trial #{trial.number} was already told {frozen.state.name}; left as it is")
        return
    try:
        trial.set_user_attr("error", "".join(traceback.format_exception_only(error)).strip())
        trial.set_user_attr(OUTCOME, FAILED)
        study.tell(trial, state=TrialState.FAIL)
    except Exception as recording:  # noqa: BLE001, the run's own error is the one raised
        error.add_note(
            f"trial #{trial.number} could not be told FAIL and stays RUNNING: {recording!r}"
        )


def _session_sharpe(returns: dict[str, float], days: Sequence[str]) -> float:
    """The per-session Sharpe ratio the gate would take of this run, or NaN where it takes none.

    The series goes through `daily_series`, ADR 0010 step 2 as the gate applies it, so a run whose
    returns the gate rejects is told no value (and `FAIL`, still `traded`), not one the gate never sees.
    """
    try:
        series = daily_series("run", returns, list(days))
    except ReturnsRejected:
        return math.nan
    return session_sharpe(series) if len(series) > 1 else math.nan
