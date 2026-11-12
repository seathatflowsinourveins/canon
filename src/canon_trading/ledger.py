"""The trial ledger: every backtest meant as a trial, recorded as one trial of an Optuna study.

Contract: ADR 0010, protocol step 1 ("Ledger every trial"), in the store and on the terms ADR 0035,
ADR 0054 and ADR 0081 decide, over the sample ADR 0041 fixes for each study. Optuna 5.0.0, read 2026-11-10 at tag v5.0.0:

* `docs/source/faq.rst` ("save and resume studies"): `RDBStorage` on a local SQLite file.
  `optuna.create_study` without `load_if_exists` creates a study and raises `DuplicatedStudyError` for
  a name already taken; `optuna.load_study` opens one and raises `KeyError` for a name not there.
  `RDBStorage(..., skip_table_creation=True)` creates no tables in the file it opens.
* `optuna/study/study.py`, `create_study`: the study and its direction are one write
  (`storage.create_new_study`), and a user attribute is another; Optuna writes no study and its
  attributes together. With `load_if_exists`, a name already taken returns the study that holds it.
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
identity is glue, ADR 0054 term 4: `config_key` is the SHA-256 of the canonical JSON of `trial_config`,
built from what this repo sets, and the run is built with it as its `id`. Nothing upstream identifies
the data a run read either, so `data_digest` is glue too (ADR 0053).

The value told for a run that traded is the per-session Sharpe ratio the gate would take of the
run's own series, mean over sample standard deviation, read from
`canon_trading.returns_matrix.session_returns`, the gate's one numeric check (ADR 0080;
`told_sharpe`), which asks skfolio what the ranking asks it. A run whose series that check refuses,
one skfolio gives no deflated Sharpe ratio of among them, is told no value, and `FAIL`, still
`traded`: a record is `COMPLETE` exactly when the ranking takes its series.
The value is there for a later sampler; `canon_trading.ranking` ranks from the returns and holds a
told value to this one.

A study is on one account in one currency, US dollars: the key takes one starting balance in it
(`_balances`), by the refusal the venue's configuration gives
(`canon_trading.backtest.account_balances`), so a run NautilusTrader's daily returns cannot book is
no trial.

First creation of a ledger file is serialised with an exclusive `fcntl.flock` (the standard library's
`fcntl`, POSIX `flock(2)`) on `trials.db.lock` beside it. Optuna's `RDBStorage` runs SQLAlchemy's
`create_all` on construction, and two processes creating one new file raced on it: 14 of 15 rounds of
two processes crashed with "table studies already exists" (measured on this machine, 2026-11-10). The
lock guards first creation only; Optuna's FAQ keeps `JournalFileBackend` for parallel optimisation,
and ADR 0035 keeps `RDBStorage` on SQLite for the reasons it gives.

One writer at a time writes a study (ADR 0081, 2026-11-11). Each writer this module names,
`record_trial`, `move_sample`, `mark_not_run` and `retire_queued`, holds the study's write lock for
everything it reads and writes: an exclusive, blocking `fcntl.flock` on a file beside the ledger named
for the study (`_write_lock`), so a second writer waits for the first. Optuna gives no such guarantee
itself: `Study.set_user_attr` replaces a study attribute whole (`RDBStorage.set_study_user_attr`,
5.0.0) and offers no compare-and-set, so two moves, or two marks, that read a list and wrote it back
at once each kept only their own entry; and a record is made in several writes with a run between
them, so a sample moved, a record marked or a trial retired by another caller landed in the middle
of one. `record_trial` holds the lock from its first read of the study, the sample, to its last write,
the `tell` of a run or of its failure, the run included. Readers take no lock: `open_study`, the
ranking and the study's pass read without waiting, and a record being written reads as what it is
because its outcome is written after every result (`record_trial`). The lock is named from the
ledger's file, so a writer takes only a study `create_ledger_study` or `open_study` opened, which the
module keeps a weak map of. It serialises this module's writers and nothing else: a record written to
the store by hand, `Study.enqueue_trial` included, is outside it, and what `_own_trial` and
`retire_queued` do about a queue is a guard for that hand-written class only.

A study is created in two writes, its name and then its sample, under the creation lock. A creation
interrupted between them leaves a name with no sample and no trial. The next `create_ledger_study` of
that name takes it through Optuna's `load_if_exists` and writes the sample, so nothing has to be
edited by hand; a name that has a sample or a trial is a duplicate. Every creator holds the lock, so a
study with neither, seen under it, is such a leftover and never one being created.

A session day (ADR 0035 term 3) is a UTC day on which a bar of the run became available, its `ts_init`,
a bar at exactly 00:00 UTC counting for the day ending there, as the daily series books it.
`BacktestResult` has no such list. The session days and the data digest are taken from the bars the
run read: those the engine's cache kept (`BacktestRun.bars`, from `Cache.bars`, with the run built by
`checked_run_config` so the cache can keep every one), not a later read of the catalog, which may have
changed after the replay. The digest holds the instrument definitions the run traded against too, as
the cache held them (`BacktestRun.instruments`): the node loads them from the catalog with the bars. A run whose cache holds another number of bars than it replayed
(`BacktestResult.iterations`), or a bar outside the sample, is told `FAIL`. So is a run whose bars
differ from those the coverage check read (`CheckedRunConfig.bars`): the node reads the catalog again
from its path, and a catalog replaced between the check and the replay was never checked to cover the
sample. The two are compared by their data digest, so the check and the run agree on one read.

Before a trial is asked for, a sample whose ends NautilusTrader's daily series cannot book is refused
(`_refuse_unbooked_ends`, ADR 0041 term 1): a bar on the day the series books the account's
registration on, the day before the start (`snapshot_day_start`, `crates/analysis/src/analyzer.rs` at
the pinned commit), and no bar on the session day of the end, where the run's last fills are booked.
Either made `record_trial` tell its own run `FAIL`. A sample holding no bar at all, which the catalog's
recorded window can still cover (a weekend), is refused there too: it had been recorded as an idle
trial, its key counted in N.

Another writer's keyed trial waiting in Optuna's queue is refused before `ask`, and
`retire_queued` retires it, or one `record_trial` took before it refused, through Optuna's public
`Study.ask`, `Trial.set_user_attr`, `Study.tell` and `Study.set_user_attr` (ADR 0081, 2026-11-10).
Who wrote a record, `record_trial` or another writer, and whether another writer's was queued, taken
from the queue here or started outside it, is decided in one place, `origin`, by key, state, note and
attributes; `record_trial`, `retire_queued`, `mark_not_run`, the ranking and the study's pass all read
it (ADR 0081, 2026-11-11). The attributes it compares are those Optuna 5.0.0 keeps on a
`FrozenTrial` (`trial/_frozen.py`): `user_attrs`, `system_attrs`, `distributions` and
`intermediate_values`.
"""

from __future__ import annotations

import enum
import fcntl
import functools
import hashlib
import inspect
import json
import math
import sqlite3
import subprocess
import threading
import traceback
import weakref
from contextlib import ExitStack
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.parse import quote

import optuna
import pandas as pd
from canon_alpaca.constants import VENUE
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import Currency
from optuna.study import StudyDirection
from optuna.trial import TrialState

from .backtest import FEE_RATES
from .backtest import VENUE_SETTINGS
from .backtest import account_balances
from .backtest import built_backtest
from .backtest import checked_run_config
from .backtest import run_built
from .ma_cross import BASE_FIELDS
from .ma_cross import MaCross
from .ma_cross import MaCrossConfig
from .ma_cross import check_as_built
from .mover import OWN_FIELDS as MOVER_FIELDS
from .mover import MoverConfig
from .returns_matrix import DAY_NS
from .returns_matrix import NS_RANGE
from .returns_matrix import ReturnsRejected
from .returns_matrix import session_returns

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Generator
    from collections.abc import Iterable
    from collections.abc import Sequence
    from typing import IO

    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import Bar
    from optuna.trial import FrozenTrial

    from .backtest import BacktestRun
    from .backtest import BuiltBacktest
    from .early_entry_study import Rows
    from .tuning_study import ReplayData

LEDGER_FILE = Path("ledger") / "trials.db"

# The user attribute holding a trial's outcome, and its three values (ADR 0035 term 2).
OUTCOME = "outcome"
TRADED = "traded"
IDLE = "idle"
FAILED = "failed"
# The outcomes of a run that ran to the end; `record_trial` writes one last, after every result.
RAN = (TRADED, IDLE)

# The study user attributes holding the study's samples, oldest first, and the numbers of records
# that ran no backtest (ADR 0041, ADR 0053 term 3).
SAMPLES = "samples"
NOT_RUN = "not_run"
# The numbers of records marked while `WAITING`, apart from `not_run`: such a record can still be
# started by an `ask`, and its mark holds only while it is not (`mark_not_run`).
NOT_RUN_WAITING = "not_run_waiting"
# The error `record_trial` writes on a trial it took from Optuna's queue and ran nothing in.
TAKEN_FROM_QUEUE = "taken from Optuna's queue by record_trial, which runs only in its own trial"
# The study user attribute holding the numbers of another writer's queued keyed records that
# `retire_queued` retired: never read as a run, each one's key still counted in N (ADR 0081).
RETIRED = "retired"
# The error `retire_queued` writes on a queued record it takes from Optuna's queue to retire it.
RETIRED_NOTE = (
    "another writer's queued trial, retired by retire_queued: it never ran here, and its key stays "
    "counted in N"
)
# The two notes this module writes on a trial it takes from Optuna's queue (`carries_a_queue_note`).
QUEUE_NOTES = (TAKEN_FROM_QUEUE, RETIRED_NOTE)
STRATEGY_CLASS = f"{MaCross.__module__}:{MaCross.__qualname__}"
# The fields of NautilusTrader's `Currency` (its constructor and `Debug` at the pinned commit), each
# held for every currency an instrument holds (`instrument_digest`).
CURRENCY_FIELDS = ("code", "precision", "iso4217", "name", "currency_type")


# The ledger file of each study `create_ledger_study` or `open_study` opened, for its lock.
_OPENED: weakref.WeakKeyDictionary[optuna.Study, Path] = weakref.WeakKeyDictionary()


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


@dataclass(frozen=True)
class MoverTrialConfig:
    """A catalog-bars Mover replay (proposed bars replay record, ADR 0101).

    ``settings`` remains MoverConfig. The replay reconciles its bar_spec with ranking's explicit
    per-instrument bar_type, and names its execution/sizing departure and costs in the key.
    Its sample and history digest are recorded beside the key. One instance runs a universe
    together in Mover's shared slots, never one account per
    symbol. Only the named bars replay in tuning_study is built, never a live Mover/client.
    """

    settings: MoverConfig
    symbols: tuple[str, ...]
    part: str
    sessions: tuple[date, ...]
    history_start: date
    buying_power: Decimal
    arm: str
    rows: Rows
    parameters: dict[str, Any] = field(default_factory=dict)
    cost_name: str = "zero-fees-bar-close"
    maker_rate: Decimal = Decimal(0)
    taker_rate: Decimal = Decimal(0)
    calibration: str = "UNCALIBRATED"


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

    ADR 0041. A study of that name already in the file raises Optuna's `DuplicatedStudyError`, unless
    an interrupted creation left it with no sample and no trial: then it is completed (module header).
    A study whose write lock cannot be opened beside the ledger (`_write_lock`; a name longer than a
    file name may be, percent-encoded) raises `ValueError` before anything is created, since no
    writer could write in it.
    `start` and `end` are UTC nanoseconds, or a date or time that `pandas.Timestamp` reads, taken as UTC
    when it has no offset. Both are instants and both are included: a date with no time is its
    midnight, so an `end` of `"2025-06-16"` leaves out a bar of that day stamped after 00:00. With no
    `path`, the ledger is `default_ledger_path()`.
    """
    sample = Sample(0, *_range(start, end))
    path = _ledger(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The open is the check: a name the file system does not take as a file name is refused here.
        _write_lock_path(path, name).open("a").close()
    except OSError as error:
        raise ValueError(
            f"the write lock of study {name!r} cannot be opened beside the ledger at {path} "
            f"({error}), so no record could be written in it; the study is not created",
        ) from error
    with ExitStack() as opened:
        with _ledger_lock(path):
            storage = opened.enter_context(_storage(path, "rwc"))
            study = optuna.create_study(
                study_name=name, storage=storage, direction="maximize", load_if_exists=True
            )
            if not _left_by_an_interrupted_creation(study):
                raise optuna.exceptions.DuplicatedStudyError(
                    f"the ledger at {path} already holds study {name!r}; open_study opens it",
                )
            study.set_user_attr(SAMPLES, [_sample_attr(sample)])
        _OPENED[study] = path
        try:
            yield study
        finally:
            _OPENED.pop(study, None)


@contextmanager
def _ledger_lock(path: Path) -> Generator[None]:
    """The exclusive `flock` on `trials.db.lock` beside the ledger at `path` (module header)."""
    with (path.parent / f"{path.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _left_by_an_interrupted_creation(study: optuna.Study) -> bool:
    """Whether `study`, new or found, has no sample, no trial and the ledger's direction."""
    return (
        SAMPLES not in study.user_attrs
        and not study.get_trials(deepcopy=False)
        and study.directions == [StudyDirection.MAXIMIZE]
    )


@contextmanager
def open_study(name: str, path: Path | None = None) -> Generator[optuna.Study]:
    """Open the study `name` in the ledger at `path`; never create a file, a table or a study.

    With no `path`, the ledger is `default_ledger_path()`. A missing file or study raises
    `LedgerNotFound`, and so do a file that is not an Optuna ledger (`_not_a_ledger`), a study with
    no sample and a study whose directions are not the ledger's, one objective maximized. Any other
    error of the storage is raised as it is: Optuna's `StorageInternalError` with SQLAlchemy's
    and SQLite's own as its cause, or SQLAlchemy's own where Optuna reads outside the session
    that wraps one (`_VersionManager._init_alembic`, line 1124, on a bare `engine.connect()`). A
    ledger that cannot be read, or is damaged, is there, and
    saying it is not would send its reader to create one. A ledger another connection holds locked
    is waited for by SQLite itself, for `sqlite3.connect`'s default `timeout` of five seconds
    (https://docs.python.org/3/library/sqlite3.html, fetched 2026-11-11; 5.3 seconds measured
    here), and after that the same error is raised, `SQLITE_BUSY` its cause, with a note that the
    ledger is busy and can be opened again; nothing here waits a second time. On leaving the `with` block the storage's
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
        except Exception as error:
            # Read for what SQLite said, then raised as it is unless that names no ledger.
            if _not_a_ledger(error):
                raise LedgerNotFound(f"the file at {path} is not an Optuna ledger") from error
            if _sqlite_code(error) == sqlite3.SQLITE_BUSY:
                error.add_note(
                    f"the ledger at {path} is busy: another connection held its SQLite lock for "
                    "longer than SQLite waits for one (sqlite3's timeout, five seconds by "
                    "default); the ledger is there, so open it again",
                )
            raise
        if _left_by_an_interrupted_creation(study):
            raise LedgerNotFound(
                f"study {name!r} in the ledger at {path} has no sample: its creation was interrupted "
                "before the sample was written, and create_ledger_study completes it",
            )
        samples(study)
        if study.directions != [StudyDirection.MAXIMIZE]:
            # `create_ledger_study` makes one objective, maximized: `record_trial` tells one value.
            raise LedgerNotFound(
                f"study {name!r} in the ledger at {path} has directions "
                f"{[d.name for d in study.directions]}, not the ledger's one objective, MAXIMIZE, "
                "the Sharpe ratio record_trial tells",
            )
        _OPENED[study] = path
        try:
            yield study
        finally:
            _OPENED.pop(study, None)


def _sqlite_error(error: BaseException) -> sqlite3.Error | None:
    """SQLite's own error behind an error the storage raised, if there is one.

    Optuna 5.0.0 wraps every `sqlalchemy.exc.SQLAlchemyError` of a session in
    `StorageInternalError` (`_create_scoped_session`, `optuna/storages/_rdb/storage.py` lines
    91-98 at the pinned commit), and SQLAlchemy keeps the driver's exception as `orig` on its
    `DBAPIError`. So it is the `orig` of the error's cause, or of the error itself where Optuna
    read outside such a session and SQLAlchemy's error came through unwrapped.
    """
    for raised in (error.__cause__, error):
        orig = getattr(raised, "orig", None)
        if isinstance(orig, sqlite3.Error):
            return orig
    return None


def _sqlite_code(error: BaseException) -> int | None:
    """SQLite's primary result code behind `error`, if any.

    `sqlite3.Error.sqlite_errorcode` (Python 3.11) is the extended code, whose low byte is the
    primary one (https://sqlite.org/rescode.html, fetched 2026-11-11: an application finds the
    primary code "merely by extracting the least significant 8 bits"): `SQLITE_BUSY_RECOVERY`,
    `SQLITE_BUSY_SNAPSHOT` and `SQLITE_BUSY_TIMEOUT`, 261, 517 and 773, are all `SQLITE_BUSY`, 5.
    """
    code = getattr(_sqlite_error(error), "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def _not_a_ledger(error: BaseException) -> bool:
    """Whether SQLite said the file `open_study` read is no Optuna ledger: it is not a database
    (`SQLITE_NOTADB`), or it is one without a table Optuna reads (`SQLITE_ERROR`, "no such table",
    SQLite's own text for it; an empty file is a database with no table).

    Only these two are the condition `LedgerNotFound` names. A lock held past SQLite's wait
    (`SQLITE_BUSY`), a file that cannot be opened (`SQLITE_CANTOPEN`), a damaged one
    (`SQLITE_CORRUPT`) and every other code are a ledger that is there and could not be read.
    """
    code, said = _sqlite_code(error), str(_sqlite_error(error))
    return code == sqlite3.SQLITE_NOTADB or (
        code == sqlite3.SQLITE_ERROR and said.startswith("no such table")
    )


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


@dataclass
class _WriteLock:
    """One write lock as this process holds it: the thread's lock, how deep it is held, the file."""

    thread: threading.RLock
    depth: int = 0
    file: IO[str] | None = None


_WRITE_LOCKS: dict[Path, _WriteLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock_path(path: Path, name: str) -> Path:
    """The write lock file of the study `name` in the ledger at `path` (`_write_lock`)."""
    return path.parent / f"{path.name}.{quote(name, safe='')}.write.lock"


@contextmanager
def _write_lock(study: optuna.Study, to: str) -> Generator[None]:
    """Hold `study`'s write lock, waiting for whoever holds it: one writer at a time (module header).

    An exclusive `fcntl.flock` on `<ledger>.<name>.write.lock` beside the ledger, the name
    percent-encoded with no character left safe, as the pass lock's is
    (`canon_trading.variant_study`), so a study name holding `/` names one file. `flock(2)` locks an
    open file description, so a second `open` of the file in the process that holds the lock would
    wait for it for ever; each lock file therefore has one `threading.RLock` in the process, and the
    file is locked when a thread first takes that and unlocked when it last leaves. So a writer
    called inside another on the same study and thread, through any handle on it, goes on, and one
    on another thread or in another process waits, with no time limit: a writer that never returns
    holds the others until its process ends. On a file system that folds case, two study names
    that differ only in case share one file, and their writers take turns. The `RLock` is found by
    the lock file's path as spelled, so a writer nested in another on one thread through a second
    spelling of one file (those two names, or the ledger's folder in another case) would wait for
    itself; no writer here calls another. A study not opened by `create_ledger_study` or
    `open_study` raises `ValueError`: its lock cannot be named. `create_ledger_study` opens the
    file once, so a study whose lock cannot be opened is never created.
    """
    path = _opened_ledger(study, to)
    lock_path = _write_lock_path(path, study.study_name)
    with _WRITE_LOCKS_GUARD:
        held = _WRITE_LOCKS.setdefault(lock_path, _WriteLock(threading.RLock()))
    with held.thread:
        if held.file is None:
            file = lock_path.open("a")
            try:
                fcntl.flock(file, fcntl.LOCK_EX)
            except BaseException:
                file.close()
                raise
            held.file = file
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
            if held.depth == 0 and held.file is not None:
                # Closing the one description that holds the lock releases it (`flock(2)`).
                file, held.file = held.file, None
                file.close()


def samples(study: optuna.Study) -> list[Sample]:
    """Every sample `study` has had, oldest first; the last is the current one (ADR 0041)."""
    stored = study.user_attrs.get(SAMPLES)
    found = []
    for entry in stored if isinstance(stored, list) else [None]:
        fields = (
            [entry.get("number"), entry.get("start"), entry.get("end")]
            if isinstance(entry, dict)
            else [None]
        )
        # Each an `int` as `_sample_attr` writes it, never a `bool`, over a range `_range` accepts.
        match fields:
            case [int() as number, int() as start, int() as end] if (
                not any(isinstance(v, bool) for v in fields)
                and start in NS_RANGE
                and end in NS_RANGE
                and start < end
            ):
                found.append(Sample(number, start, end))
            case _:
                found = []
                break
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
    The samples are read and written under the study's write lock (module header), so two moves at
    once are both kept, numbered one after the other, and a move waits for a record being written:
    it lands before a record's first read of the sample or after its last write, never between. A
    study not opened by `create_ledger_study` or `open_study` raises `ValueError`: its lock cannot
    be named.
    """
    first, last = _range(start, end)
    with _write_lock(study, "move its sample"):
        history = samples(study)
        sample = Sample(history[-1].number + 1, first, last)
        study.set_user_attr(SAMPLES, [*map(_sample_attr, history), _sample_attr(sample)])
    return sample


def _opened_ledger(study: optuna.Study, to: str) -> Path:
    """The ledger file of `study`, for its write lock, or `ValueError` if the ledger did not open it."""
    path = _OPENED.get(study)
    if path is None:
        raise ValueError(
            f"study {study.study_name!r} was not opened by open_study or create_ledger_study, so "
            f"its write lock cannot be held to {to}; open it with open_study",
        )
    return path


def _range(start: int | str | pd.Timestamp, end: int | str | pd.Timestamp) -> tuple[int, int]:
    """A sample's two ends in UTC nanoseconds, each one a timestamp holds, the first before the last.

    NautilusTrader's `UnixNanos` wraps a `u64` and a pandas `Timestamp` holds at most 2**63 - 1, so a
    bound outside `NS_RANGE` is refused before it is written: every later read of it would crash.
    """
    first, last = _ns(start), _ns(end)
    outside = [bound for bound in (first, last) if bound not in NS_RANGE]
    if outside:
        raise ValueError(
            f"a sample's bounds are nanoseconds from 0 to 2**63 - 1, got {outside[0]}",
        )
    if not first < last:
        raise ValueError(f"a sample needs start < end, got {_date(first)} and {_date(last)}")
    return first, last


def _ns(value: int | str | pd.Timestamp) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        stamp = pd.Timestamp(value)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        return int(stamp.value)
    except (OverflowError, pd.errors.OutOfBoundsDatetime) as error:
        # pandas 3 keeps an instant before 1677-09-21 or after 2262-04-11 in microseconds, and gives
        # no nanoseconds for it.
        raise ValueError(f"{value!r} is no instant pandas holds in nanoseconds: {error}") from error


def _date(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()


def _sample_attr(sample: Sample) -> dict[str, int]:
    return {"number": sample.number, "start": sample.start, "end": sample.end}


def _canonical(value: object) -> Any:
    """`value` in the canonical form of ADR 0054 term 4, or `TypeError` if it has no stable one.

    A `Decimal` is normalised (`0`, `0.00` and `0E-2` all give `0`). A `Quantity`, `Price` or `Money`
    prints with its precision, which is not normalised away: an order size of `10.0` is refused by an
    instrument that takes whole shares, where `10` runs, so the two are different configurations.
    """
    if isinstance(value, enum.Enum):
        # An `IntEnum` or `StrEnum` is an `int` or `str` too, and would be keyed by its value, where
        # ADR 0053 term 2 keys enums by name. NautilusTrader's enums are not Python enums (pyo3
        # classes at the pin) and print by name below; a Python enum is refused.
        raise TypeError(
            f"{type(value).__name__} {value!r} has no stable text form for a config key"
        )
    if isinstance(value, float) and not math.isfinite(value):
        # Refused here, not left to `json.dumps`: as a dict key it would become the text "nan".
        raise TypeError(f"float {value!r} has no stable text form for a config key")
    if value is None or isinstance(value, bool | int | float | str):
        return value  # `json.dumps` writes a float by `repr`
    if isinstance(value, Decimal):
        if not value.is_finite():  # NaN, sNaN and infinities print as text json.dumps would take
            raise TypeError(f"Decimal {value!r} has no stable text form for a config key")
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
    strategies: Sequence[MaCrossConfig | MoverTrialConfig],
    *,
    starting_balances: Sequence[str],
) -> dict[str, Any]:
    """The object ADR 0054 term 4 hashes: what this repo sets for a run, normalised.

    Each strategy's class and the fields its config sets (`_strategy_fields`: its own attributes, and
    each base `StrategyConfig` field away from its pinned default, which `MaCrossConfig` passes on to
    the engine), the venue's name, `VENUE_SETTINGS`, `FEE_RATES` and starting balances, and the bar
    types the run loads, one per instrument as `backtest_run_config` derives them. A base field at its
    default is left out, so a default config's key is what it was; a pin that changes a default moves
    the key of a config holding that field at either value (ADR 0053 term 2). The study's sample is
    not in it.

    One MoverTrialConfig instead uses the named bars replay and reconciles bar_spec/bar_type;
    its sample and input/history identity remain trial attributes under ADR 0041 and ADR 0053.

    Each config must be a `MaCrossConfig` itself, not a subclass: `record_trial` builds `MaCross` from
    every config and the key names that class, so a subclass's own fields (`SessionMaCrossConfig`'s
    `limit_offset`, `wind_down_at`) would be keyed though no run reads them, and two configs that differ
    only there would be two trials of one run. Such a config raises `TypeError`, naming its class, and
    so does one whose attributes are not the four `MaCrossConfig` builds, of their types, and
    `ValueError` one whose values its constructor would refuse (`ma_cross.check_as_built`): a value
    changed or added after it was built would be keyed though the run does not read it as keyed.
    """
    if len(strategies) == 1 and type(strategies[0]) is MoverTrialConfig:
        return _mover_trial_config(strategies[0], starting_balances)
    other = [type(config).__name__ for config in strategies if type(config) is not MaCrossConfig]
    if other:
        raise TypeError(
            f"a trial runs MaCross, so each config is a MaCrossConfig itself: got {other[0]}, not "
            "MaCrossConfig",
        )
    strategies = cast("Sequence[MaCrossConfig]", strategies)
    for config in strategies:
        check_as_built(config)
    bar_types = data_bar_types(str(config.bar_type) for config in strategies)
    return {
        "strategies": [
            {
                "class": STRATEGY_CLASS,
                "config": _strategy_fields(config),
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
            "starting_balances": _balances(starting_balances),
        },
        "data": {"data_type": "Bar", "bar_types": bar_types},
    }


def data_bar_types(traded: Iterable[str]) -> list[str]:
    """The bar types a trial's data loads, from those its strategies trade: each once, in order.

    `trial_config` writes them so, and the ranking holds a stored config to the same rule.
    """
    return list(dict.fromkeys(traded))


def _mover_trial_config(
    trial: MoverTrialConfig,
    balances: Sequence[str],
) -> dict[str, Any]:
    """Ranking-readable Mover configuration, named costs and execution model, excluding sample data."""
    from .tuning_study import ARMS
    from .tuning_study import BAR_SPEC
    from .tuning_study import EXECUTION
    from .tuning_study import PARTS

    settings = trial.settings
    if type(settings) is not MoverConfig or set(vars(settings)) != set(MOVER_FIELDS):
        raise TypeError("a Mover trial needs an unextended MoverConfig with its declared fields")
    own = {name: getattr(settings, name) for name in MOVER_FIELDS}
    base = {name: getattr(settings, name) for name in BASE_FIELDS}
    MoverConfig(**own, **base)  # validate fields changed after construction, before a trial
    if settings.bar_spec != BAR_SPEC:
        raise ValueError("Mover tuning requires 1-MINUTE-LAST-EXTERNAL bars")
    if trial.part not in PARTS or trial.arm not in ARMS:
        raise ValueError("a Mover trial must name a registered runnable arm and session part")
    if not trial.symbols or len(set(trial.symbols)) != len(trial.symbols):
        raise ValueError("a Mover trial must name unique symbols")
    if not trial.sessions or tuple(sorted(set(trial.sessions))) != trial.sessions:
        raise ValueError("a Mover trial must name unique chronological sessions")
    if not trial.buying_power.is_finite() or trial.buying_power <= 0:
        raise ValueError("buying_power must be positive")
    if settings.max_notional != trial.buying_power / settings.slots:
        raise ValueError("max_notional must equal the named buying_power / slots")
    if (
        trial.cost_name != "zero-fees-bar-close"
        or trial.maker_rate != 0
        or trial.taker_rate != 0
        or trial.calibration != "UNCALIBRATED"
    ):
        raise ValueError("the supported cost model is zero-fees-bar-close, explicitly UNCALIBRATED")
    types = [f"{symbol}.ALPACA-{settings.bar_spec}" for symbol in trial.symbols]
    # MoverConfig has a suffix, not an instrument. Each stored entry names bar_type as rank requires;
    # the one engine runs ONE BarsReplayMover over this whole universe.
    fields = _strategy_fields(settings)
    fields["replay"] = {
        "execution": EXECUTION,
        "base_strategy": "canon_trading.mover:Mover",
        "sizing": "buying_power / slots",
        "buying_power": _canonical(trial.buying_power),
        "flow": "bars-close-volume",
        "protection_settlement_delay_ns": 1,
        "native_activation": "post-submit-iteration-before-next-bar",
        "arm": trial.arm,
        "rows": asdict(trial.rows),
        "parameters": trial.parameters,
        "part": trial.part,
    }
    return {
        "strategies": [
            {
                "class": "canon_trading.tuning_study:BarsReplayMover",
                "config": {**fields, "bar_type": bar_type},
            }
            for bar_type in types
        ],
        "venue": {
            "name": str(VENUE),
            **{name: _canonical(value) for name, value in VENUE_SETTINGS.items()},
            "fee_model": {
                "class": "MakerTakerFeeModel",
                "name": trial.cost_name,
                "maker_rate": _canonical(trial.maker_rate),
                "taker_rate": _canonical(trial.taker_rate),
                "calibration": trial.calibration,
            },
            "starting_balances": _balances(balances),
        },
        "data": {"data_type": "Bar", "bar_types": types},
    }


def _balances(starting_balances: Sequence[str]) -> list[str]:
    """The starting balance as the key holds it, as `Money` prints it: one, in the account's base
    currency, or `ValueError` by name (`canon_trading.backtest.account_balances`, the refusal the
    venue's configuration gives).

    A study is on one account in one currency, so the key takes no second balance: a run with two
    books per-position returns that the gate does not take, was recorded as traded and stopped
    its study on every sample (round 8). One balance has no order and no repeat to normalise.
    """
    return [_canonical(balance) for balance in account_balances(starting_balances)]


def base_config_defaults() -> dict[str, Any]:
    """A copy of `_base_config_defaults()`, read once per process: the pin does not change in one."""
    return dict(_base_config_defaults())


@functools.cache
def _base_config_defaults() -> dict[str, Any]:
    """Each field of NautilusTrader's `StrategyConfig`, canonical, at its default at the pinned release.

    The fields are `canon_trading.ma_cross.BASE_FIELDS`, read from the class by reflection once, for
    the config's refusal of unknown keywords and for the key alike: its data descriptors, the pyo3
    getters of a Rust struct. So a field upstream adds is in the key's reach without a change here.
    """
    default = StrategyConfig()
    return {name: _field(name, getattr(default, name)) for name in sorted(BASE_FIELDS)}


def _strategy_fields(config: MaCrossConfig | MoverConfig) -> dict[str, Any]:
    """A strategy config's fields as the key holds them (ADR 0053 term 2).

    Its own fields, and each base `StrategyConfig` field whose value is not its default at the pin
    (`base_config_defaults`): such a value was set here and reaches the engine, as `order_id_tag`
    does. A base field at its default is left out, so a default config's key is what it was before
    base fields were read, and an upstream default moves no key of a config that keeps it.
    """
    fields = {
        name: (_field(name, value) if not isinstance(value, datetime) else value.isoformat())
        for name, value in vars(config).items()
        if not name.startswith("_")
    }
    for name, default in _base_config_defaults().items():
        value = _field(name, getattr(config, name))
        if value != default:  # a base field is a read-only pyo3 getter, never in `vars(config)`
            fields[name] = value
    return dict(sorted(fields.items()))


def _field(name: str, value: object) -> Any:
    """`_canonical(value)`, or `TypeError` naming the field whose value has no stable text form."""
    try:
        return _canonical(value)
    except TypeError as error:
        raise TypeError(f"field {name!r}: {error}") from None


def canonical_json(config: dict[str, Any]) -> str:
    return json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def config_key(config: dict[str, Any]) -> str:
    """The SHA-256 hex digest of the UTF-8 bytes of `canonical_json(config)`."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


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


def data_digest(
    bars: Sequence[Bar], bar_types: Sequence[str], instruments: Sequence[Any]
) -> dict[str, str]:
    """ADR 0053's identity of the data a run read: `bars_digest`, and for each instrument in
    `instruments` a SHA-256 of its definition (`instrument_digest`), keyed by its ID (`AAPL.ALPACA`),
    which no bar type's text equals."""
    digests = bars_digest(bars, bar_types)
    for instrument in instruments:
        digests[str(instrument.id)] = instrument_digest(instrument)
    return dict(sorted(digests.items()))


def bars_digest(bars: Sequence[Bar], bar_types: Sequence[str]) -> dict[str, str]:
    """The bars' part of the data identity: for each bar type, a SHA-256 of its bars (ADR 0053 term 1).

    Each of `bar_types`, the bar types the run loads, has an entry even with no bar in `bars`, the
    digest of no lines: the run read that bar type and found nothing, which another run that found
    bars there must disagree with.

    The digest of a bar type is that of the UTF-8 bytes of one line per bar, in `bars`' order,
    `bar_type|open|high|low|close|volume|ts_event|ts_init`, the prices and the volume as NautilusTrader
    prints them (their precision included), joined by `\\n`. It is taken from the bars themselves, not
    from the catalog's files, so rewriting or consolidating files moves no digest. Kept per bar type, so
    two trials on one sample that read one bar type can be checked to have read the same bars.
    """
    lines: dict[str, list[str]] = {bar_type: [] for bar_type in bar_types}
    for bar in bars:
        lines.setdefault(str(bar.bar_type), []).append(
            f"{bar.bar_type}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}|"
            f"{bar.ts_event}|{bar.ts_init}",
        )
    return {
        bar_type: hashlib.sha256("\n".join(found).encode("utf-8")).hexdigest()
        for bar_type, found in sorted(lines.items())
    }


def instrument_digest(instrument: Any) -> str:
    """The SHA-256 of an instrument's definition, as the data identity holds it (ADR 0053 term 1).

    The definition is the one NautilusTrader's backtest node compares: `instrument_definitions_match`
    (`crates/backtest/src/exchange.rs` at the pinned commit) refuses a second definition of an
    instrument ID unless the two agree on their serialized form without `ts_event` and `ts_init`,
    every field and `info` included. Here that form is upstream's own dict of the instrument,
    `Instrument.to_dict()`, which writes each field as the serialization does (a price, quantity or
    decimal as its text, so `5` and `5.0` are two definitions to the node as to this digest), in
    `canonical_json`. Without the two stamps: they say when the definition was stamped, not what it
    is, and a load that stores the same definition again stamps it anew.

    Upstream writes a currency as its code alone (`Currency`'s `Serialize`, and its `PartialEq` and
    `Hash`, read only `code`): the node compares two definitions in one process, where a code names
    one currency of the process's registry. The ledger compares runs of different processes, and a
    currency registered again (`Currency.register(..., overwrite=True)`) under the same code at
    another precision changed what a run traded on the same bars. So each currency the instrument
    holds, the `Currency` of each of its pyo3 getters named for one (`quote_currency` and, by class,
    `base_currency`, `settlement_currency` or `currency`, found by reflection), is held beside the definition with the fields `Currency`
    has: `code`, `precision`, `iso4217`, `name` and `currency_type`.

    The node loads the definition from the catalog and the engine trades against it: a quantity limit
    alone changed what a run traded on the same bars (`tests/test_ledger.py`), so two runs on one
    sample that read two definitions of one instrument read different data. A catalog that holds two
    definitions of one instrument does not run (the check above), so the cache's definition is the one
    the run traded against. `info` is kept: the node compares it, and a load writes a definition only
    when the catalog holds none (`canon_trading.bars_to_catalog`), so a refresh of the bars does not
    restamp it. A value with no stable text form raises `TypeError` naming the instrument and its field.
    """
    try:
        definition = {
            name: _field(name, value)
            for name, value in instrument.to_dict().items()
            if name not in ("ts_event", "ts_init")
        }
    except TypeError as error:
        raise TypeError(f"instrument {instrument.id}'s definition: {error}") from None
    getters = [
        name
        for name, _ in inspect.getmembers(type(instrument), inspect.isgetsetdescriptor)
        if name.endswith("currency")
    ]
    try:
        held_currencies = {name: getattr(instrument, name) for name in getters}
    except Exception as error:  # noqa: BLE001, named and raised as the definition's own error
        raise TypeError(f"instrument {instrument.id}'s currencies: {error!r}") from None
    currencies = {
        name: {field: _canonical(getattr(currency, field)) for field in CURRENCY_FIELDS}
        for name, currency in held_currencies.items()
        if isinstance(currency, Currency)
    }
    held = {"definition": definition, "currencies": currencies}
    return hashlib.sha256(canonical_json(held).encode("utf-8")).hexdigest()


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
    strategies: Sequence[MaCrossConfig | MoverTrialConfig],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
    on_sample: Sample | None = None,
    replay_data: ReplayData | None = None,
) -> RecordedTrial:
    """Build strategies from their configs and run the study's sample as one trial.

    MaCrossConfig follows the original path below, unchanged. One MoverTrialConfig uses the
    catalog-only bars replay (proposed bars record, ADR 0101); its bar_spec/bar_type mapping,
    named costs and buying-power sizing are in the key, with input/history digests beside it.
    That path builds a BacktestEngine from the exact catalog objects identified before asking.

    ADR 0035 terms 2 and 3, ADR 0041 and ADR 0053. The key is computed from the configs first, and
    the strategies are built from them here, so what is hashed is what runs: a config changed
    afterwards changes nothing. What `checked_run_config` refuses, or a value with no stable text form, raises before
    anything is recorded: such a call is not a trial. The trial is then asked for, so a run that never
    returns stays in the study as `RUNNING`. The run is bounded by the study's current sample and built
    with the key as its `id`; its session days and data digest are those of the bars the run read, as
    the engine's cache kept them, and those must be the bars the coverage check read (module header).
    A run that raises, or whose result cannot be
    recorded before its outcome, is told `FAIL` with outcome `failed` and its error, and the exception
    passes through. A
    run with no filled order is told `FAIL` with outcome `idle`; any other `COMPLETE` with `traded`, or `FAIL`, still `traded`, if
    the gate's numeric check refuses its series (`told_sharpe`). The outcome is written last, so a record that has one ran to the end:
    a `tell` that fails after it leaves the outcome as it is, told `FAIL` with its error, or still
    `RUNNING` if that cannot be told either (`_tell_failed`). A config that is not a `MaCrossConfig`
    itself is refused before anything is recorded (`trial_config`), and so is a study where another
    writer's keyed trial waits in Optuna's queue, unless it is retired (`_own_trial`, `retire_queued`).
    Every write after the trial is asked for, the outcome and the `tell` included, is inside the `try`
    that tells a failure, so an error or `KeyboardInterrupt` there is told `FAIL` with its error. Only
    a process killed outright, or a store that refuses the very first write, leaves a record with no
    key; no backtest ran for it, and `mark_not_run` clears it (ADR 0053 term 3). Nothing is overwritten: the
    same configuration again is a new record.

    A caller that checked the study's sample against a plan passes the sample it checked as
    `on_sample`, and a study whose current sample is another one, moved since by any caller
    (`move_sample`), is refused before a trial, naming both: the run is bounded by the sample
    read here, once, and the record carries that sample's number.

    The whole attempt holds the study's write lock (`_write_lock`, module header), from that read of
    the sample to the last write, so no other writer this module names moves the sample, marks a
    record or retires one while a record is made, and a second `record_trial` on the study waits for
    the first. A study not opened by `create_ledger_study` or `open_study` raises `ValueError`
    before anything is read. Whatever error the attempt lets through carries the number of the trial
    it asked for, or none if it was refused before one (`trial_asked_for`), so a caller reads its own
    record and no other caller's.
    """
    asked: list[int] = []
    try:
        with _write_lock(study, "record a trial"):
            return _record(
                study,
                catalog_path,
                strategies,
                starting_balances=starting_balances,
                logging=logging,
                on_sample=on_sample,
                asked=asked,
                replay_data=replay_data,
            )
    except BaseException as error:
        _name_the_trial(error, asked[0] if asked else None)
        raise


ASKED_FOR = "canon_trial_asked_for"


def _name_the_trial(error: BaseException, number: int | None) -> None:
    """Write on `error` the number of the trial the attempt that raised it asked for, or none.

    Written on every error `record_trial` lets through, so one raised before by another attempt
    carries this attempt's number and not that one's. An error that takes no attribute names no
    trial, which its reader takes as a refusal before one.
    """
    with suppress(Exception):
        setattr(error, ASKED_FOR, number)


def trial_asked_for(error: BaseException) -> int | None:
    """The number of the trial `record_trial` asked for in the attempt that raised `error`.

    None for an attempt refused before its trial, where nothing was recorded, and for an error
    `record_trial` did not raise. The trial is the attempt's own, one `Study.ask` created
    (`_own_trial`): a trial it took from Optuna's queue and told `FAIL` with the queue's note is not.
    The number says which trial the attempt asked for, not how it was told: read the trial for that
    (an error raised after the `tell`, while the node is disposed of, names a trial told as it ran).
    """
    number = getattr(error, ASKED_FOR, None)
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def _record(
    study: optuna.Study,
    catalog_path: Path,
    strategies: Sequence[MaCrossConfig | MoverTrialConfig],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None,
    on_sample: Sample | None,
    asked: list[int],
    replay_data: ReplayData | None = None,
) -> RecordedTrial:
    """`record_trial`'s attempt, under the study's write lock; `asked` takes its trial's number."""
    sample = current_sample(study)
    refuse_a_moved_sample(study, sample, on_sample)
    # The configuration is frozen here, once: the configs and the balances as tuples, and one
    # strategy built from each config. The key, the run config and the node are all made from
    # these, so a sequence its caller changes afterwards changes neither the key nor the run.
    configs = tuple(strategies)
    balances = tuple(starting_balances)
    if len(configs) == 1 and type(configs[0]) is MoverTrialConfig:
        return _record_mover(
            study, catalog_path, configs[0], balances, logging, sample, asked, replay_data
        )
    config = trial_config(configs, starting_balances=balances)
    key = config_key(config)
    built = [MaCross(strategy) for strategy in cast("Sequence[MaCrossConfig]", configs)]
    if config_key(trial_config(configs, starting_balances=balances)) != key:
        raise RuntimeError("a strategy config changed while its strategies were built")
    checked = checked_run_config(
        catalog_path,
        built,
        starting_balances=balances,
        logging=logging,
        run_config_id=key,
        start=sample.start,
        end=sample.end,
    )
    run_config = checked.config
    bar_types = [str(bar_type) for data in run_config.data for bar_type in data.bar_types or ()]
    if bar_types != config["data"]["bar_types"]:
        raise RuntimeError(f"the run loads {bar_types}, not the bar types its key names")
    _refuse_unbooked_ends(checked.bars, sample)
    # The bars only: the instruments are the run's own, from its cache (ADR 0053 term 1).
    checked_digest = bars_digest(_in_order(checked.bars), bar_types)
    del checked  # only the digest is kept, so the checked bars are not held through the run

    # The build is the check (ADR 0081): the node is built from the frozen configuration and
    # its strategies added before a trial is asked for, so a configuration upstream refuses leaves
    # no record, and the node run in the trial is that one. It is disposed however this is left.
    with built_backtest(run_config, built) as node:
        return _run_as_a_trial(
            study,
            node,
            sample=sample,
            key=key,
            config=config,
            bar_types=bar_types,
            checked_digest=checked_digest,
            asked=asked,
        )


def _record_mover(
    study: optuna.Study,
    catalog_path: Path,
    config: MoverTrialConfig,
    balances: Sequence[str],
    logging: LoggerConfig | None,
    sample: Sample,
    asked: list[int],
    replay_data: ReplayData | None = None,
) -> RecordedTrial:
    """Snapshot bars, build and record one Mover run under ADR 0101's proposed bars record."""
    from .tuning_study import built_mover_replay
    from .tuning_study import check_replay_data
    from .tuning_study import load_replay

    # ADR 0041/0053: the key is configuration, with the sample and actual digests beside it.
    trial_config([config], starting_balances=balances)
    own = {name: getattr(config.settings, name) for name in MOVER_FIELDS}
    base = {name: getattr(config.settings, name) for name in BASE_FIELDS}
    config = replace(
        config,
        settings=MoverConfig(**own, **base),
        parameters=json.loads(canonical_json(config.parameters)),
    )
    data = load_replay(catalog_path, config, sample) if replay_data is None else replay_data
    check_replay_data(config, sample, data)
    _refuse_unbooked_ends(data.bars, sample)
    history = data.history_digest
    for previous in study.get_trials(deepcopy=False):
        if previous.user_attrs.get("sample") != sample.number:
            continue
        held = previous.user_attrs.get("mover_sample", {}).get("history_digest", {})
        if any(held[kind] != history[kind] for kind in held.keys() & history.keys()):
            raise ValueError(
                "Mover history changed on the current sample; move_sample and replay every configuration"
            )
    frozen = trial_config([config], starting_balances=balances)
    key = config_key(frozen)
    with built_mover_replay(config, data, sample, key, balances, logging) as replay:
        return _run_as_a_trial(
            study,
            None,
            sample=sample,
            key=key,
            config=frozen,
            bar_types=list(data.bar_types),
            checked_digest=bars_digest(data.bars, data.bar_types),
            asked=asked,
            replay=replay,
            mover_sample={
                "sessions": [day.isoformat() for day in config.sessions],
                "history_start": config.history_start.isoformat(),
                "history_digest": history,
            },
        )


def _run_as_a_trial(
    study: optuna.Study,
    node: BuiltBacktest | None,
    *,
    sample: Sample,
    key: str,
    config: dict[str, Any],
    bar_types: list[str],
    checked_digest: dict[str, str],
    asked: list[int],
    replay: Callable[[], BacktestRun] | None = None,
    mover_sample: dict[str, Any] | None = None,
) -> RecordedTrial:
    """Ask for the attempt's trial, run `node` in it, write what the run left and tell it.

    `node` is the node built from the configuration `key` hashes (`_record`), over `sample`, whose
    bars the coverage check read with digest `checked_digest`. Everything here after the trial is
    asked for is inside the `try` that tells a failure (`record_trial`). The node is disposed of
    after this returns or raises (`_record`), so after the `tell`: upstream's `dispose` returns
    nothing and refuses nothing, and an error or interrupt raised while it runs leaves the trial as
    it was told and names it (`trial_asked_for`).
    """
    trial = _own_trial(study)
    asked.append(trial.number)
    try:
        # The key first, so the shortest stretch of a record has none; the run starts only after it.
        trial.set_user_attr("config_key", key)
        trial.set_user_attr("sample", sample.number)
        trial.set_user_attr("config", config)
        if mover_sample is not None:
            trial.set_user_attr("mover_sample", mover_sample)
        if replay is not None:
            run = replay()
        else:
            assert node is not None
            run = run_built(node)
        result = run.result
        returns = {str(ns): value for ns, value in sorted(result.returns_series.items())}
        bars = _in_order(run.bars)
        if len(bars) != result.iterations:
            raise RuntimeError(
                f"the engine's cache holds {len(bars)} bars and the run replayed "
                f"{result.iterations}, so the data it read cannot be identified (ADR 0053); the "
                "catalog may have grown between the count and the replay: record the trial again",
            )
        outside = [bar for bar in bars if not sample.start <= bar.ts_init <= sample.end]
        if outside:
            raise RuntimeError(
                f"the run replayed {len(outside)} bars outside {sample.describe()}, the first "
                f"at {_date(outside[0].ts_init)}",
            )
        missing = [b for b, i in zip(bar_types, run.instruments, strict=True) if i is None]
        if missing:
            raise RuntimeError(
                f"the engine's cache holds no instrument for {missing[0]}, so the data the run read "
                "cannot be identified (ADR 0053)",
            )
        if bars_digest(bars, bar_types) != checked_digest:
            raise RuntimeError(
                f"the run read other bars than the coverage check read over {sample.describe()}: "
                "the catalog changed between the check and the replay, so what the run read was "
                "never checked to cover the sample (ADR 0041); record the trial again",
            )
        digest = data_digest(bars, bar_types, run.instruments)
        days = session_days(bars)
        trial.set_user_attr("run_config_id", result.run_config_id)
        trial.set_user_attr("backtest_start", result.backtest_start)
        trial.set_user_attr("backtest_end", result.backtest_end)
        trial.set_user_attr("returns_series", returns)
        trial.set_user_attr("session_days", days)
        trial.set_user_attr("data_digest", digest)
        traded = any(order.filled_qty.is_positive() for order in run.orders)
        sharpe = told_sharpe(returns, days) if traded else math.nan
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


def refuse_a_moved_sample(study: optuna.Study, sample: Sample, on_sample: Sample | None) -> None:
    """Refuse `study`, read on `sample`, if a caller checked `on_sample` against its plan and the
    study has moved since (ADR 0041 term 5), naming both; the one refusal `record_trial` and the
    study's pass (`canon_trading.variant_study.record_grid`) give."""
    if on_sample is not None and sample != on_sample:
        raise ValueError(
            f"study {study.study_name!r} moved to {sample.describe()} after the pass checked "
            f"{on_sample.describe()} against its plan; nothing was recorded on the moved sample",
        )


def registration_day(start: int) -> int:
    """The UTC day NautilusTrader's daily series books a run's account on when it starts at `start`.

    `snapshot_day_start` in `crates/analysis/src/analyzer.rs` at the pinned commit books the first
    snapshot of an account, its registration at the run's start, on the day before the one holding
    it, as the base of the first return, and holds no return for that day itself.
    """
    return max(start // DAY_NS * DAY_NS - DAY_NS, 0)


def _refuse_unbooked_ends(bars: Sequence[Bar], sample: Sample) -> None:
    """Refuse a sample whose ends leave a run's moves on a day no session day holds (ADR 0041 term 1).

    NautilusTrader's daily series books the run as `session_day` and `registration_day` say. A bar on
    the registration's day, as one at exactly a sample's start at 00:00 UTC is (`session_day` moves
    it back a day), is a session day with no return, so `session_returns` refuses every such run. And a
    run's last fills, its strategies' `on_stop`, are booked on the session day of its end, which must
    hold a bar: with none there, a fill that moved the balance gave a return on a day with no bar,
    refused the same way. A sample holding no bar at all has no session day of its end either, and is
    refused first, by name. Each is refused here, before a trial is asked for, as a sample the catalog
    does not cover is: such a call is not a trial.
    """
    base = registration_day(sample.start)
    days = {session_day(bar.ts_init) for bar in bars}
    if not days:
        raise ValueError(
            f"{sample.describe()} holds no bar, though the catalog's recorded window covers it: a run "
            "over it trades nothing and books no session day, which is no trial (ADR 0041 term 1); "
            "give the study a sample that holds its bars",
        )
    if base in days:
        raise ValueError(
            f"{sample.describe()} starts at {_date(sample.start)}, a UTC midnight on which a bar "
            "becomes available: NautilusTrader's daily series books that bar on the day before, "
            "the account's registration, which holds no return; start the sample before it",
        )
    if session_day(sample.end) not in days:
        raise ValueError(
            f"{sample.describe()} ends on {_date(session_day(sample.end))}, a session day of no "
            f"bar, the last on {_date(max(days))}: NautilusTrader books a run's last fills on the "
            "session day of its end, where no return is accepted; end the sample on its last bar's "
            "day",
        )


def _own_trial(study: optuna.Study) -> optuna.Trial:
    """A trial `Study.ask` creates, never one it takes from Optuna's queue of `WAITING` trials.

    `Study.ask` takes the oldest `WAITING` trial before it creates one (`Study._pop_waiting_trial_id`,
    Optuna 5.0.0), and such a trial holds another writer's parameters and attributes, and a number
    below records already made, where the ranking reads a higher number as a later record (ADR 0054
    term 5). A taken one is told `FAIL` at once with the queue's note (`TAKEN_FROM_QUEUE`), and the
    next is asked for; one queued with no key holds no outcome, so it shows no run and `mark_not_run`
    clears it, and a keyed one stops the ranking naming `retire_queued`. A trial is taken from the queue if it was
    `WAITING` before `ask`, or if it holds anything a trial `ask` creates does not: a user or system
    attribute (`Study.enqueue_trial` writes `fixed_params` on every trial it queues), a parameter or
    an intermediate value. Only the `WAITING` and `RUNNING` trials are read, not every series. A
    trial that cannot be read back is told `FAIL` with the queue's note and the error raised
    (`_tell_unread`): it may be a keyed trial taken from the queue, and no other note keeps the
    ranking from reading one as a run of its key.
    A queued trial that holds a config key and is not retired (`retire_queued`) is refused before
    `ask`, naming it: taken, it would keep that key and any outcome another writer gave it, and only
    its note would keep the ranking from reading it as a run of that key (ADR 0081, 2026-11-10). One
    queued between that check and `ask` is still taken, with the note, and the ranking stops on it
    naming `retire_queued`, which retires it; if that note is refused too, the error says so, naming
    the trial (`_untold`).

    No writer this module names queues a trial: one in the queue was put there by hand, through
    `Study.enqueue_trial` or the store, outside the study's write lock. So everything here about a
    queue, with `_refuse_a_keyed_queue`, `_tell_unread` and `_untold`, guards that hand-written
    class, which ADR 0081's limits record once (ADR 0073's reachability rule, 2026-11-11), and is
    kept as it was: the gate trusts the ledger as its writers write it.
    """
    while True:
        waiting = study.get_trials(deepcopy=False, states=(TrialState.WAITING,))
        retired = _refuse_a_keyed_queue(study, waiting)
        queued = {t.number for t in waiting}
        trial = study.ask()
        try:
            frozen = _asked(study, trial)
        except BaseException as error:
            error.add_note(f"trial #{trial.number}, asked by record_trial, could not be read back")
            _tell_unread(study, trial, error, TAKEN_FROM_QUEUE)
            raise
        if not _from_the_queue(frozen, queued):
            return trial
        _tell_taken(study, trial, TAKEN_FROM_QUEUE, _untold(frozen, retired))


def _tell_unread(study: optuna.Study, trial: optuna.Trial, error: BaseException, note: str) -> None:
    """Tell `trial`, which `Study.ask` gave and which could not be read back, `FAIL` with `note`, a
    note of a trial taken from Optuna's queue; if that is refused, say so on `error`, the read's.

    Nothing read tells whether `ask` created the trial or took it from the queue, and another writer
    may have queued a keyed trial after the queue was last read (ADR 0081's race), whose
    outcome and returns that writer's would, under any other note, read as a run of its key. With
    the queue's note the ranking stops on such a record naming `retire_queued`, which retires it
    (`carries_a_queue_note`); one `ask` created holds no key, so it shows no run and `mark_not_run`
    clears it. A trial another process told meanwhile, which only `mark_not_run` does to a keyless
    one, refuses every write (`UpdateFinishedTrialError`, Optuna 5.0.0 `storages/_base.py`) and is
    left as it is. If the note is otherwise refused, the trial is still told `FAIL` (`_tell_taken`)
    and `error` names the limit left: a raced record left so reads as a run of its key, as ADR 0081
    states. An interrupt while the note is written is raised with the same note, the read's error
    its context.
    """
    try:
        _tell_taken(study, trial, note, None)
    except optuna.exceptions.UpdateFinishedTrialError:
        error.add_note(f"trial #{trial.number} was already told by another process; left as it is")
    except BaseException as refused:
        limit = (
            f"trial #{trial.number} may be another writer's queued trial, and its note was refused "
            f"({refused!r}), so the ranking may read it as a run of its key (ADR 0081's "
            "named limit)"
        )
        error.add_note(limit)
        for told in getattr(refused, "__notes__", ()):
            error.add_note(told)
        if not isinstance(refused, Exception):
            refused.add_note(limit)
            raise


def _untold(frozen: FrozenTrial, retired: set[int]) -> str | None:
    """What a refused note leaves on another writer's trial taken from the queue: the note added to
    the store's error, or nothing for a retired one, which the list already holds.

    A keyed one, not retired, may read as a run of its key (ADR 0081's named limit). A keyless
    one holding what `record_trial` never writes on such a record (`Study.enqueue_trial`'s
    `fixed_params`, another writer's attributes) reads, with no note, as another writer's started
    record (`origin`): it stops the ranking for good, the strict side, and nothing clears it. A
    keyless one holding nothing reads as `record_trial`'s own, which `mark_not_run` clears.
    """
    if frozen.number in retired:
        return None
    if not has_key(frozen):
        if origin(frozen).origin is not Origin.FOREIGN:
            return None
        return (
            f"trial #{frozen.number}, queued by another writer with no config key, was taken without "
            "its note, so it reads as another writer's started record: it stops the ranking for "
            "good, and mark_not_run does not clear it (ADR 0081)"
        )
    return (
        f"trial #{frozen.number}, queued by another writer with config key "
        f"{frozen.user_attrs['config_key']}, was taken without its note, so the ranking may read it "
        "as a run of that key (ADR 0081's named limit)"
    )


def _asked(study: optuna.Study, trial: optuna.Trial) -> FrozenTrial:
    """The trial `Study.ask` just gave, read back as the store holds it."""
    [frozen] = [
        t
        for t in study.get_trials(deepcopy=False, states=(TrialState.RUNNING,))
        if t.number == trial.number
    ]
    return frozen


def _from_the_queue(frozen: FrozenTrial, queued: set[int]) -> bool:
    """Whether `Study.ask` took `frozen` from Optuna's queue rather than creating it: it was
    `WAITING` before `ask` (`queued`), or holds what a created trial does not (`_own_trial`)."""
    return frozen.number in queued or bool(
        frozen.user_attrs
        or frozen.system_attrs
        or frozen.distributions
        or frozen.intermediate_values
    )


def _tell_taken(study: optuna.Study, trial: optuna.Trial, note: str, limit: str | None) -> None:
    """Write `note` as the `error` of `trial`, taken from the queue, and tell it `FAIL`.

    If the note is refused, the trial is still told `FAIL`, and the store's error is raised with
    `limit` added, if given, and the `tell`'s own error if that is refused too, so neither replaces it.
    """
    try:
        trial.set_user_attr("error", note)
    except BaseException as error:
        if limit is not None:
            error.add_note(limit)
        try:
            study.tell(trial, state=TrialState.FAIL, skip_if_finished=True)
        except Exception as telling:  # noqa: BLE001, the note's refusal is the error raised
            error.add_note(f"trial #{trial.number} could not be told FAIL either: {telling!r}")
        raise
    # `skip_if_finished`: a hand-written `tell` may have finished a taken trial between the two
    # writes (ADR 0081, the hand-written class; `retire_queued` waits for the write lock).
    study.tell(trial, state=TrialState.FAIL, skip_if_finished=True)


def _refuse_a_keyed_queue(study: optuna.Study, waiting: Sequence[FrozenTrial]) -> set[int]:
    """Refuse to `ask` while another writer's keyed trial, not retired, waits in Optuna's queue;
    otherwise give the numbers retired (`retired_numbers`, which refuses a list not in its form)."""
    retired = retired_numbers(study)
    # Every one of `waiting` is `WAITING`, which `origin` reads as `QUEUED` by its state alone.
    keyed = [t for t in waiting if has_key(t) and t.number not in retired]
    if keyed:
        first = keyed[0]
        raise ValueError(
            f"study {study.study_name!r} holds trial #{first.number}, queued by another writer with "
            f"config key {first.user_attrs['config_key']}: Study.ask takes the oldest queued trial, "
            f"so record_trial would take it; retire_queued(study, {first.number}) retires it, its "
            "key still counted in N, and record_trial then goes on (ADR 0054)",
        )
    return retired


def retired_numbers(study: optuna.Study) -> set[int]:
    """The numbers in `study`'s `retired` list, or `ValueError` if it is not in the form
    `retire_queued` writes: then no record can be read as retired, and every reader refuses it
    (`canon_trading.ranking` stops on it)."""
    marked = study.user_attrs.get(RETIRED, [])
    if not is_not_run_list(marked):
        raise ValueError(
            f"study user attribute {RETIRED} is {marked!r}, not a list of trial numbers in the form "
            "retire_queued writes; another writer left it, and no record is read as retired until "
            "it is",
        )
    return set(marked)


def carries_a_queue_note(trial: FrozenTrial) -> bool:
    """Whether `trial` was taken from Optuna's queue with a note this module writes on such a record,
    and is `FAIL` or, its `tell` refused, still `RUNNING`: the states that taking it leaves.

    `record_trial` writes `TAKEN_FROM_QUEUE` on any trial it takes, and `retire_queued` writes
    `RETIRED_NOTE` only on the keyed record it retires (it refuses a keyless one), so a keyless record
    carries this module's note only if it is `TAKEN_FROM_QUEUE`."""
    notes = QUEUE_NOTES if has_key(trial) else (TAKEN_FROM_QUEUE,)
    return (
        trial.state in (TrialState.FAIL, TrialState.RUNNING)
        and trial.user_attrs.get("error") in notes
    )


# Every user attribute `record_trial` writes on a record (ADR 0035 term 3, ADR 0053): on one with no
# key, only its error, since the key is its first write.
WRITTEN = frozenset(
    {
        "config_key",
        "sample",
        "config",
        "run_config_id",
        "backtest_start",
        "backtest_end",
        "returns_series",
        "session_days",
        "data_digest",
        "mover_sample",
        OUTCOME,
        "error",
    }
)
WRITTEN_KEYLESS = frozenset({"error"})


class Origin(enum.Enum):
    """Who wrote a record, as `origin` reads it (ADR 0081)."""

    WRITER = "record_trial's own"
    QUEUED = "another writer's, waiting in Optuna's queue"
    TAKEN = "another writer's, taken from Optuna's queue here with its note"
    FOREIGN = "another writer's, started, told or written outside the queue"


@dataclass(frozen=True)
class Reading:
    """A record's `Origin`, and for any other than `record_trial`'s own, why."""

    origin: Origin
    why: str = ""


def origin(trial: FrozenTrial) -> Reading:
    """Who wrote `trial`: the one rule every reader of the ledger applies (ADR 0081).

    `record_trial` asks for its own trial, writes its key first and then only the attributes in
    `WRITTEN` (on a record with no key, only its error), and suggests no parameter, reports no
    intermediate value and writes no system attribute. This module takes another writer's queued
    trial only through `Study.ask`, and writes on it at once its note (`carries_a_queue_note`), so
    a taken trial holds that writer's attributes beside the note and ran nothing here. Read in this
    order, by values, key, state, note and attributes:

    * a record told more than one value is `FOREIGN`: `record_trial` tells one, the Sharpe ratio, on
      a study of one objective (`canon_trading.ranking` stops on a study of more);
    * a record with no key in the form `record_trial` writes that shows a run (`ran_something`) is
      `FOREIGN`: a trial of an unknown configuration, whatever else it holds;
    * a `WAITING` one is `QUEUED`: another writer queued it, and nothing has started it;
    * one `FAIL` or `RUNNING` with a queue note this module writes on it is `TAKEN`: either note on
      a keyed record, only `record_trial`'s on a keyless one (`carries_a_queue_note`);
    * one with a queue note in another state, or `retire_queued`'s on a keyless record, which it
      never takes, a system attribute (`Study.enqueue_trial` writes
      `fixed_params` on every trial it queues), a user attribute `record_trial` does not write on such
      a record, a parameter or an intermediate value is `FOREIGN`: another writer started, told or
      wrote it, outside the queue's take, so it may have run anything;
    * anything else is `WRITER`, `record_trial`'s own: for a keyed one, `canon_trading.ranking`
      then checks its state, outcome and results against what `record_trial` leaves; a keyless one is
      what a process killed before the key's write leaves, and ran nothing.

    What each reader does with it: `record_trial` refuses to `ask` beside a keyed `QUEUED` record not
    retired (`_own_trial`); `retire_queued` retires a keyed `QUEUED` or `TAKEN` record and nothing
    else; `mark_not_run` marks a keyless `WRITER`, `QUEUED` or `TAKEN` record and refuses a `FOREIGN`
    one, and the ranking clears a marked one only while it is in the state its mark leaves
    (`cleared_while_waiting`), and stops on a `FOREIGN` one, keyed or not, for good; the study's pass
    reads only a keyed `WRITER` record as `record_trial`'s written failure
    (`canon_trading.variant_study`). A record another writer made in `record_trial`'s exact form,
    with no system attribute, reads as `WRITER`: nothing on it tells the two apart, and the ledger's
    writers are this repository's own sessions (ADR 0081's named limits).

    One question is asked apart, at `ask` and of the trial `ask` just gave: whether it was taken from
    the queue (`_from_the_queue`). There a trial `ask` created holds nothing at all, so anything on
    it, even an error alone, means it was queued with it; the note then written makes it `TAKEN`
    here, or, refused, leaves what `_untold` names.
    """
    attrs = trial.user_attrs
    keyed = has_key(trial)
    if trial.values is not None and len(trial.values) != 1:
        return Reading(
            Origin.FOREIGN,
            f"was told {len(trial.values)} values, {trial.values!r}, where record_trial tells one, "
            "the Sharpe ratio",
        )
    if not keyed and ran_something(trial):
        return Reading(Origin.FOREIGN, "shows a run, a trial of an unknown configuration")
    if trial.state == TrialState.WAITING:
        return Reading(
            Origin.QUEUED,
            "is WAITING, queued by another writer, which record_trial never leaves and refuses to "
            "start beside",
        )
    if carries_a_queue_note(trial):
        return Reading(
            Origin.TAKEN,
            "was taken from Optuna's queue, another writer's queued trial that never ran here and "
            "that no record_trial keys",
        )
    if not keyed and attrs.get("error") == RETIRED_NOTE:
        return Reading(
            Origin.FOREIGN,
            "carries retire_queued's note with no config key, though retire_queued takes only a "
            "keyed record",
        )
    if attrs.get("error") in QUEUE_NOTES:
        return Reading(
            Origin.FOREIGN,
            f"was taken from Optuna's queue and is {trial.state.name}, which taking a trial never "
            "leaves",
        )
    if trial.system_attrs:
        return Reading(
            Origin.FOREIGN,
            f"holds system attributes {sorted(trial.system_attrs)}, which record_trial never writes",
        )
    if not keyed and "config_key" in attrs:
        return Reading(
            Origin.FOREIGN,
            f"holds config key {attrs['config_key']!r}, not a non-empty text as record_trial writes",
        )
    extra = set(attrs) - (WRITTEN if keyed else WRITTEN_KEYLESS)
    if extra:
        return Reading(
            Origin.FOREIGN,
            f"holds user attributes {sorted(extra)}, which record_trial never writes"
            + ("" if keyed else " on a record before its key"),
        )
    if trial.intermediate_values or trial.distributions:
        return Reading(
            Origin.FOREIGN,
            "holds an intermediate value or a parameter, which record_trial never writes",
        )
    return Reading(Origin.WRITER)


def _trial_numbered(study: optuna.Study, number: int) -> FrozenTrial:
    """Trial `number` of `study`, or `LookupError`; `TypeError` for a number that is not an `int`.

    Optuna numbers trials with `int`s, and `False == 0 == 0.0`: a `bool` or a float would find trial
    #0 or #1, and a list it then joined would hold it as given, a list every reader refuses
    (`is_not_run_list`).
    """
    if not isinstance(number, int) or isinstance(number, bool):
        raise TypeError(
            f"a trial number is an int, not {type(number).__name__} {number!r}; pass int(number) "
            "for an integer of another type"
        )
    found = [trial for trial in study.get_trials(deepcopy=False) if trial.number == number]
    if not found:
        raise LookupError(f"study {study.study_name!r} has no trial #{number}")
    [trial] = found
    return trial


def retire_queued(study: optuna.Study, number: int) -> None:
    """Retire trial `number` of `study`, another writer's queued record with a config key (ADR 0081).

    Such a record never ran here, and whatever results it holds came from its writer. Retired, it is
    never read as a run, and its key stays counted in N as a configuration that was tried: the
    strict side, as `not_run_waiting` keeps a keyless one (ADR 0053 term 3). Its number joins the
    study user attribute `retired`; nothing is deleted. A record is retired once: a second retirement
    is refused.

    Two kinds of record are retired: one still `WAITING`, beside which `record_trial` refuses to
    start, and one taken from the queue with a note this module writes (`carries_a_queue_note`), as
    `record_trial` took one before it refused. A `FAIL` one is finished, and Optuna 5.0.0 writes
    nothing on a finished trial (`check_trial_is_updatable`, `storages/_base.py`), so the list is its
    retirement; a `RUNNING` one, whose `tell` was refused, is then told `FAIL`. A `WAITING` one is
    taken from the queue through `Study.ask`, which takes the oldest `WAITING` trial
    (`_pop_waiting_trial_id`, in order of trial ID): every older one is taken first, with the note
    `record_trial` writes, and an older keyed one not retired is refused before anything is written,
    naming it. Each taken trial is told `FAIL`, the retired one with `RETIRED_NOTE`. The list is
    written first, so a process stopped between the two, or a note that is refused, leaves a record
    the ranking reads as retired and never as a run; `record_trial` takes one left `WAITING`. The
    record's other contents are not read.

    Both kinds are as `origin` reads them, `QUEUED` and `TAKEN`. A keyless record is refused
    (`mark_not_run` clears one), and so is any other keyed record: a record `record_trial` wrote is a
    trial of its key, and only a later record of that key resolves it. A number that is not an `int`
    is refused before anything is read (`_trial_numbered`). Everything is read and written under the
    study's write lock (module header); a study not opened by `create_ledger_study` or `open_study`
    raises `ValueError`. `record_trial` takes and tells a queued trial under the same lock, so a
    retirement waits for it, and a `RUNNING` record read here with the queue's note is one whose
    `tell` was refused or whose process is gone, never one between its note and its `tell`; the
    `tell` here still skips a finished trial (`skip_if_finished`).
    """
    with _write_lock(study, "retire a queued trial"):
        trial = _trial_numbered(study, number)
        if not has_key(trial):
            raise ValueError(
                f"trial #{number} has no config key; if it ran no backtest, mark_not_run clears it",
            )
        retired = retired_numbers(study)
        if number in retired:
            raise ValueError(
                f"trial #{number} is retired already ({trial.state.name}); it is not read as a run "
                "and its key stays counted in N",
            )
        reading = origin(trial).origin
        if reading is Origin.QUEUED:
            older = [
                t.number
                for t in study.get_trials(deepcopy=False, states=(TrialState.WAITING,))
                if t.number < number and has_key(t) and t.number not in retired
            ]
            if older:
                raise ValueError(
                    f"trial #{older[0]}, keyed and queued before #{number}, would be taken from "
                    f"Optuna's queue first; retire #{older[0]} first",
                )
        elif reading is not Origin.TAKEN:
            raise ValueError(
                f"trial #{number} ({trial.state.name}) is not another writer's queued record, "
                "WAITING or taken from Optuna's queue with its note: a record record_trial wrote is "
                "a trial of its key, and only a later record of that key resolves it",
            )
        retired = {*retired, number}
        study.set_user_attr(RETIRED, sorted(retired))
        if trial.state == TrialState.WAITING:
            _take_retired(study, number, retired)
        elif trial.state == TrialState.RUNNING:
            study.tell(number, state=TrialState.FAIL, skip_if_finished=True)


def _take_retired(study: optuna.Study, number: int, retired: set[int]) -> None:
    """Take trials from Optuna's queue through `Study.ask` until retired trial `number` is taken.

    Each taken is told `FAIL` (`_tell_taken`): a retired one with `RETIRED_NOTE`, any other with
    `record_trial`'s note, which `retire_queued` accepts. A trial `ask` took from the queue is known
    as `_own_trial` knows it (`_from_the_queue`). If `number` has left the queue, taken by another
    process, it stays retired and nothing more is taken; a trial `ask` created because the queue
    emptied meanwhile ran nothing, and is told `FAIL` keyless, for `mark_not_run` to clear. A trial
    that cannot be read back is noted as taken from the queue, whatever the queue read before `ask`
    says (`_tell_unread`), so one `ask` created is noted so too, keyless, and `mark_not_run` clears it.
    """
    while True:
        queued = {t.number for t in study.get_trials(deepcopy=False, states=(TrialState.WAITING,))}
        if number not in queued:
            return
        trial = study.ask()
        try:
            frozen = _asked(study, trial)
        except BaseException as error:
            # Noted as taken from the queue, whatever the queue before `ask` says (`_tell_unread`).
            error.add_note(f"trial #{trial.number}, asked by retire_queued, could not be read back")
            _tell_unread(study, trial, error, _note_for(trial.number, True, retired))
            raise
        from_the_queue = _from_the_queue(frozen, queued)
        note = _note_for(frozen.number, from_the_queue, retired)
        _tell_taken(study, trial, note, _untold(frozen, retired))
        if frozen.number == number or not from_the_queue:
            return


def _note_for(number: int, from_the_queue: bool, retired: set[int]) -> str:
    """The note `_take_retired` writes on trial `number`, taken from the queue or created by `ask`."""
    if not from_the_queue:
        return "asked by retire_queued after its trial left Optuna's queue; ran nothing"
    return RETIRED_NOTE if number in retired else TAKEN_FROM_QUEUE


def _in_order(bars: Sequence[Bar]) -> list[Bar]:
    """`bars` in a total order: bars that tie on both times by their text (ADR 0053 term 1)."""
    return sorted(bars, key=lambda bar: (str(bar.bar_type), bar.ts_init, bar.ts_event, str(bar)))


def mark_not_run(study: optuna.Study, number: int) -> None:
    """Record that trial `number` of `study`, a record with no config key, ran no backtest (ADR 0053 term 3).

    `record_trial` writes the key before it starts a run, so a record without one ran nothing: it is
    what a process killed between `ask` and that write leaves. The record is kept; a `RUNNING` one is
    told `FAIL`, through `Study.tell`, which takes a trial number, and its number joins the study user
    attribute `not_run`, as does a `FAIL` one's. A `WAITING` one, which another writer queued
    (`Study.enqueue_trial`) and never started, is left `WAITING`: Optuna 5.0.0 tells only a `RUNNING`
    trial (`study/_tell.py`), and its one public way out of `WAITING` is `Study.ask`, which takes the
    oldest queued trial (`Study._pop_waiting_trial_id`); the storage that could set its state "is not
    supposed to be directly accessed by library users" (`storages/_base.py`). Its number joins
    `not_run_waiting` instead, kept apart because an `ask` may still start it: the ranking leaves it out
    of N only while it is `WAITING`, or once `record_trial` took it from the queue and ran nothing in
    it (`_own_trial`). One `record_trial` took whose `tell` was refused, left `RUNNING` with its note
    (`taken_untold`), is told `FAIL` here, as an unmarked one is, and its mark then clears it. One
    another writer started after its mark may have run anything, so the ranking stops on it and it
    cannot be marked again. A record that has a key is refused, and so is one
    `origin` reads as another writer's outside the queue (`FOREIGN`): one that shows a run
    (`ran_something`), a result told by a writer other than `record_trial` included, a trial of an
    unknown configuration; and one another writer started, told or wrote on, which may have run
    anything (a system attribute such as `Study.enqueue_trial`'s `fixed_params`, or a user attribute
    `record_trial` never writes before its key). Nothing clears either.
    The check, the `tell` and the lists are read and written under the study's write lock (module
    header), so two marks at once are both kept; a study not opened by `create_ledger_study` or
    `open_study` raises `ValueError`. `record_trial` holds the same lock from before it asks for its
    trial until it has told it, so a mark waits for a record being written, which then has its key
    and is refused: a keyless `RUNNING` record read here is not `record_trial` at work. A run that
    never returns holds the lock until its process ends, and the mark waits that long. Another
    writer's live trial is outside the lock: use this only on a record whose process is gone, since
    a live one would have its run told `FAIL`.
    """
    with _write_lock(study, "mark a trial not run"):
        trial = _trial_numbered(study, number)
        if has_key(trial):
            raise ValueError(
                f"trial #{number} has config key {trial.user_attrs['config_key']}: it was tried, and"
                " only a later record of that key resolves it",
            )
        lists = {name: study.user_attrs.get(name, []) for name in (NOT_RUN, NOT_RUN_WAITING)}
        for name, marked in lists.items():
            if not is_not_run_list(marked):
                raise ValueError(
                    f"study user attribute {name} is {marked!r}, not a list of trial numbers in the "
                    "form mark_not_run writes; another writer left it",
                )
        if number in lists[NOT_RUN_WAITING]:
            if taken_untold(trial):
                # `skip_if_finished`: a `tell` by another hand may land first, to the same end
                # (the hand-written class; `record_trial`'s own waits for the write lock).
                study.tell(number, state=TrialState.FAIL, skip_if_finished=True)
                return
            if not cleared_while_waiting(trial):
                raise ValueError(
                    f"trial #{number} was marked not run while WAITING and "
                    f"{after_its_mark(trial)} ({trial.state.name}), so it may have run anything; it "
                    "is not marked again",
                )
            return
        reading = origin(trial)
        if reading.origin is Origin.FOREIGN:
            raise ValueError(
                f"trial #{number} has no config key in the form record_trial writes but "
                f"{reading.why} (outcome {trial.user_attrs.get(OUTCOME)!r}): another writer's "
                "record, which may have run anything, so it is not cleared",
            )
        name = NOT_RUN_WAITING if reading.origin is Origin.QUEUED else NOT_RUN
        if trial.state == TrialState.RUNNING:
            study.tell(number, state=TrialState.FAIL)
        if number not in lists[name]:
            study.set_user_attr(name, sorted([*lists[name], number]))


def cleared_while_waiting(trial: FrozenTrial) -> bool:
    """Whether `trial`, marked not run while `WAITING`, is still in a state that mark clears.

    It is if it is still `WAITING`, or if `record_trial` took it from Optuna's queue and told it
    `FAIL` at once, its error that note and no run shown: `origin` reads it `QUEUED` or, `FAIL`,
    `TAKEN`, which on a keyless record is `record_trial`'s note alone (`carries_a_queue_note`). One so
    taken and left `RUNNING`, its `tell` refused, is `taken_untold`, which `mark_not_run` finishes.
    Anything else is a start or a write by another writer after the mark (`after_its_mark`).
    """
    reading = origin(trial).origin
    return reading is Origin.QUEUED or (reading is Origin.TAKEN and trial.state == TrialState.FAIL)


def taken_untold(trial: FrozenTrial) -> bool:
    """Whether `trial`, a keyless record, was taken from Optuna's queue with `TAKEN_FROM_QUEUE`, by
    `record_trial` or by `retire_queued` on its way to a retired trial, and left `RUNNING`, its `tell`
    refused (`_tell_taken`): it ran nothing, and `mark_not_run` tells it `FAIL`, marked or not."""
    return (
        not has_key(trial)
        and trial.state == TrialState.RUNNING
        and origin(trial).origin is Origin.TAKEN
    )


def after_its_mark(trial: FrozenTrial) -> str:
    """What another writer did to `trial`, marked while `WAITING`, that its mark no longer clears."""
    if trial.state == TrialState.WAITING:
        return "was written on by another writer after its mark"
    return "started after its mark"


def is_not_run_list(marked: object) -> bool:
    """Whether `marked` is a `not_run` list as `mark_not_run` writes it: trial numbers, no `bool`."""
    return isinstance(marked, list) and all(
        isinstance(number, int) and not isinstance(number, bool) for number in marked
    )


def has_key(trial: FrozenTrial) -> bool:
    """Whether `trial` holds a config key in the form `record_trial` writes, a non-empty text."""
    key = trial.user_attrs.get("config_key")
    return isinstance(key, str) and bool(key)


def ran_something(trial: FrozenTrial) -> bool:
    """Whether `trial` shows a run: an outcome, returns or session days, or a result (ADR 0053 term 3).

    Any outcome counts, `failed` too: `record_trial` writes one only on a record that has its key,
    so a keyless record with an outcome came from another writer. An evaluation is
    `shows_an_evaluation`'s.
    """
    attrs = trial.user_attrs
    return (
        OUTCOME in attrs
        or "returns_series" in attrs
        or "session_days" in attrs
        or shows_an_evaluation(trial)
    )


def shows_an_evaluation(trial: FrozenTrial) -> bool:
    """Whether Optuna keeps an evaluation of `trial`: a result, or a parameter it suggested.

    A result is the state `COMPLETE` or `PRUNED`, a value told, or an intermediate value reported; a
    suggested parameter is in `distributions` (`optuna/trial/_state.py` and `_frozen.py` at 5.0.0).
    `Study.optimize` leaves a parameter on a trial whose objective raised or returned NaN, which it
    tells `FAIL` with no value (`study/_tell.py`). `record_trial` suggests no parameter, reports no
    intermediate value and tells a value only after outcome `traded`.
    """
    return (
        trial.state in (TrialState.COMPLETE, TrialState.PRUNED)
        or trial.values is not None
        or bool(trial.intermediate_values)
        or bool(trial.distributions)
    )


def _tell_failed(study: optuna.Study, trial: optuna.Trial, error: BaseException) -> None:
    """Tell `trial` `FAIL` with its error and outcome `failed`, or the outcome its run already ended
    with; if that cannot be written, say so on `error`.

    A trial already told, as when an interrupt lands just after its `tell`, is left as it is, and the
    note says so. A trial whose key was never written started no run, so it is told `FAIL` with its
    error and no outcome: an outcome on a keyless record shows a run (ADR 0053 term 3), and
    `mark_not_run` clears this one. A trial that already holds outcome `traded` or `idle` ran to the
    end, every result written before it (`record_trial` writes the outcome last), and only its `tell`
    failed: it keeps that outcome and is told `FAIL` with its error, so the ranking reads its run and
    compares it with every other record of its key (ADR 0054 term 5). Overwritten `failed`, it would be
    resolved by a later record of its key, and the returns and data it read never compared.
    """
    try:
        [frozen] = [t for t in study.get_trials(deepcopy=False) if t.number == trial.number]
        if frozen.state.is_finished():
            error.add_note(
                f"trial #{trial.number} was already told {frozen.state.name}; left as it is"
            )
            return
        trial.set_user_attr("error", "".join(traceback.format_exception_only(error)).strip())
        if has_key(frozen) and frozen.user_attrs.get(OUTCOME) not in RAN:
            trial.set_user_attr(OUTCOME, FAILED)
        study.tell(trial, state=TrialState.FAIL)
    except Exception as recording:  # noqa: BLE001, the run's own error is the one raised
        error.add_note(
            f"trial #{trial.number} could not be told FAIL and stays RUNNING: {recording!r}"
        )


def told_sharpe(returns: object, days: object) -> float:
    """The per-session Sharpe ratio the gate would take of this run, or NaN where it takes none.

    The value `record_trial` tells, and the one `canon_trading.ranking` holds a told value to. It is
    read from `session_returns`, ADR 0010 step 2 and the gate's one numeric check as the gate applies
    them (ADR 0080), so a run whose returns the gate refuses, a constant series, one of zeros or
    one skfolio gives no deflated Sharpe ratio of among them, is told no value (and `FAIL`, still
    `traded`), not one the gate never sees;
    `session_returns` refuses `returns` and `days` not in the shape `record_trial` writes.
    """
    try:
        return session_returns("run", returns, days).sharpe
    except ReturnsRejected:
        return math.nan
