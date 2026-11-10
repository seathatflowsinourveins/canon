"""The trial ledger: every backtest meant as a trial, recorded as one trial of an Optuna study.

Contract: ADR 0010, protocol step 1 ("Ledger every trial"), in the store and on the terms ADR 0035
decides. Optuna 5.0.0, read 2026-11-10 at tag v5.0.0:

* `docs/source/faq.rst` ("save and resume studies"): `RDBStorage` on a local SQLite file, which
  `open_study` builds; `optuna.create_study(..., load_if_exists=True)` reopens a study.
* `tutorial/20_recipes/009_ask_and_tell.py`, and `Study.ask` and `Study.tell` in
  `optuna/study/study.py`: a trial is asked for before the run and told its state after; `tell` takes
  a value for `COMPLETE` and none for `FAIL`. Under ask and tell nothing upstream marks a raised run
  `FAIL`, so `record_trial` tells it.
* `Trial.set_user_attr` stores a JSON-serialisable value (`value_json` is `Text` in
  `optuna/storages/_rdb/models.py`), and `Study.trials` returns every trial in every state.

NautilusTrader v2.0.0rc8 binds no config identity to Python: `BacktestRunConfig.id` defaults to a fresh
UUID4 (`crates/backtest/src/config.rs` at the pinned commit) and the config cannot be serialised. So the
identity is glue, ADR 0035 term 4: `config_key` is the SHA-256 of the canonical JSON of `trial_config`,
read from the run config's own properties, and the run is built with it as its `id`.

The value told for a run that traded is skfolio's `Portfolio.sharpe_ratio` (1.12.8,
`portfolio/_base.py`) over the run's own series: mean over sample standard deviation, per session. It is
there for a later sampler; `canon_trading.ranking` does not read it.

A session day (ADR 0035 term 3) is a UTC day on which a bar of the run became available, its `ts_init`,
read with `ParquetDataCatalog.query` (the installed `nautilus_trader/persistence/__init__.pyi`) from the
catalog the run read. `BacktestResult` has no such list.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import optuna
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from optuna.trial import TrialState
from skfolio import Portfolio

from .backtest import backtest_run_config
from .backtest import run_backtest_config

if TYPE_CHECKING:
    from collections.abc import Generator
    from collections.abc import Sequence

    from nautilus_trader.config import BacktestRunConfig
    from nautilus_trader.config import LoggerConfig

    from .backtest import BacktestRun
    from .ma_cross import MaCross

LEDGER_PATH = Path("ledger") / "trials.db"
DAY_NS = 86_400 * 1_000_000_000

# The user attribute holding a trial's outcome, and its three values (ADR 0035 term 2).
OUTCOME = "outcome"
TRADED = "traded"
IDLE = "idle"
FAILED = "failed"

# Engine config properties left out of the key: logging changes no result, the instance ID is new for
# every engine, and catalogs and streaming name places, not data (ADR 0035 term 4).
_ENGINE_LEFT_OUT = ("catalogs", "instance_id", "logging", "streaming")


@contextmanager
def open_study(name: str, path: Path = LEDGER_PATH) -> Generator[optuna.Study]:
    """Open the study `name` in the SQLite file at `path`, creating either if it does not exist.

    A relative `path` is taken from the current directory. On leaving the `with` block the storage's
    session is removed (`RDBStorage.remove_session`) and its SQLAlchemy engine disposed, so no SQLite
    connection is left for the garbage collector.
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = optuna.storages.RDBStorage(f"sqlite:///{path}")
    try:
        yield optuna.create_study(
            study_name=name,
            storage=storage,
            direction="maximize",
            load_if_exists=True,
        )
    finally:
        storage.remove_session()
        storage.engine.dispose()


def _canonical(value: object) -> Any:
    """`value` in the canonical form of ADR 0035 term 4, or `TypeError` if it has no stable one."""
    if value is None or isinstance(value, bool | int | float | str):
        return value  # `json.dumps` writes a float by `repr` and refuses NaN and infinities
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(_canonical(key)): _canonical(item) for key, item in value.items()}
    if type(value).__module__.startswith("nautilus_trader") and not callable(value):
        text = str(value)
        # Value types and identifiers print as themselves (`AAPL.ALPACA-1-DAY-LAST-EXTERNAL`, `10`), and
        # enums by name (`NETTING`). A Rust struct prints by `Debug`, braces included, and a class with
        # no text form of its own prints as `<... object at 0x...>`, an address.
        if "{" not in text and not text.startswith("<"):
            return text
        # The one struct accepted is the fee model with no per-instrument overrides: those are a map,
        # and the order a map prints in is not stable.
        if type(value).__name__ == "MakerTakerFeeModel" and "overrides: {}" in text:
            return text
    raise TypeError(f"{type(value).__name__} {value!r} has no stable text form for a config key")


def _is_method(owner: type, name: str) -> bool:
    """Whether `name` on `owner` is a method, as opposed to a field or a property."""
    attribute = inspect.getattr_static(owner, name, None)
    return isinstance(attribute, classmethod | staticmethod) or (
        callable(attribute) and not isinstance(attribute, type)
    )


def _properties(obj: object, *, left_out: Sequence[str] = ()) -> dict[str, Any]:
    """Every public field and property of `obj`, its class's and its own, in canonical form.

    Methods are skipped. A field or property whose value is callable has no stable text form and is
    refused by `_canonical`, not skipped.
    """
    own = {name for name in getattr(obj, "__dict__", {}) if not name.startswith("_")}
    names = {
        name
        for name in dir(type(obj))
        if not name.startswith("_") and not _is_method(type(obj), name)
    }
    return {name: _canonical(getattr(obj, name)) for name in sorted((names | own) - set(left_out))}


def trial_config(run_config: BacktestRunConfig, strategies: Sequence[MaCross]) -> dict[str, Any]:
    """The object ADR 0035 term 4 hashes, read from `run_config` and each strategy's config.

    `run_backtest` adds strategies and no actor or execution algorithm, so those are all it covers.
    Each data config's catalog and the engine properties in `_ENGINE_LEFT_OUT` are left out.
    """
    return {
        "strategies": [
            {
                "class": f"{type(strategy).__module__}:{type(strategy).__qualname__}",
                "config": _properties(strategy.config),
            }
            for strategy in strategies
        ],
        "venues": [_properties(venue) for venue in run_config.venues],
        "data": [_properties(data, left_out=("catalog",)) for data in run_config.data],
        "engine": _properties(run_config.engine, left_out=_ENGINE_LEFT_OUT),
        "start": run_config.start,
        "end": run_config.end,
        "chunk_size": run_config.chunk_size,
    }


def canonical_json(config: dict[str, Any]) -> str:
    return json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def config_key(config: dict[str, Any]) -> str:
    """The SHA-256 hex digest of the UTF-8 bytes of `canonical_json(config)`."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def session_days(catalog_path: Path, bar_types: Sequence[str], start: int, end: int) -> list[str]:
    """UTC days, as nanoseconds in text, on which a bar of `bar_types` became available in [start, end].

    The catalog is asked for that range only (`ParquetDataCatalog.query`, `start` and `end`); the bars
    it returns are checked against the range again by their `ts_init`.
    """
    catalog = ParquetDataCatalog(str(Path(catalog_path).resolve()))
    bars = catalog.query(NautilusDataType.Bar, identifiers=list(bar_types), start=start, end=end)
    days = {bar.ts_init // DAY_NS * DAY_NS for bar in bars if start <= bar.ts_init <= end}
    return [str(day) for day in sorted(days)]


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
    strategies: Sequence[MaCross],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
) -> RecordedTrial:
    """Run a backtest with these arguments, as `run_backtest` would, as one trial of `study`.

    ADR 0035 terms 2 and 3. The key is computed first. What `backtest_run_config` refuses, or a config
    value with no stable text form, raises before anything is recorded: such a call is not a trial. The
    trial is then asked for, so a run that never returns stays in the study as `RUNNING`. The run uses
    the config the key was read from, built again with the key as its `id` and checked to give the same
    key. A run that raises, or whose result cannot be recorded, is told `FAIL` with outcome `failed`
    and its error, and the exception passes through. A run with no filled order is told `FAIL` with
    outcome `idle`; any other `COMPLETE` with `traded`, or `FAIL`, still `traded`, if its Sharpe ratio
    is not finite. The outcome is written last, so a record that has one ran to the end. Nothing is
    overwritten: the same configuration again is a new record.
    """
    config = trial_config(
        backtest_run_config(catalog_path, strategies, starting_balances=starting_balances),
        strategies,
    )
    key = config_key(config)
    run_config = backtest_run_config(
        catalog_path,
        strategies,
        starting_balances=starting_balances,
        logging=logging,
        run_config_id=key,
    )
    if config_key(trial_config(run_config, strategies)) != key:
        raise RuntimeError("the run config built with its key as id does not give that key")

    trial = study.ask()
    trial.set_user_attr("config", config)
    trial.set_user_attr("config_key", key)
    try:
        run = run_backtest_config(run_config, strategies)
        result = run.result
        returns = {str(ns): value for ns, value in sorted(result.returns_series.items())}
        bar_types = [str(bar_type) for data in run_config.data for bar_type in data.bar_types or ()]
        days = session_days(
            catalog_path, bar_types, result.backtest_start or 0, result.backtest_end or 0
        )
        trial.set_user_attr("run_config_id", result.run_config_id)
        trial.set_user_attr("backtest_start", result.backtest_start)
        trial.set_user_attr("backtest_end", result.backtest_end)
        trial.set_user_attr("returns_series", returns)
        trial.set_user_attr("session_days", days)
        traded = any(order.filled_qty.is_positive() for order in run.orders)
        sharpe = _session_sharpe(returns, days) if traded else math.nan
        outcome = TRADED if traded else IDLE
    except BaseException as error:
        _tell_failed(study, trial, error)
        raise
    trial.set_user_attr(OUTCOME, outcome)
    if math.isfinite(sharpe):
        study.tell(trial, sharpe)
    else:
        study.tell(trial, state=TrialState.FAIL)
    return RecordedTrial(number=trial.number, config_key=key, outcome=outcome, run=run)


def _tell_failed(study: optuna.Study, trial: optuna.Trial, error: BaseException) -> None:
    """Tell `trial` `FAIL` with outcome `failed`; if that cannot be written, say so on `error`."""
    try:
        trial.set_user_attr("error", "".join(traceback.format_exception_only(error)).strip())
        trial.set_user_attr(OUTCOME, FAILED)
        study.tell(trial, state=TrialState.FAIL)
    except Exception as recording:  # noqa: BLE001, the run's own error is the one raised
        error.add_note(
            f"trial #{trial.number} could not be told FAIL and stays RUNNING: {recording!r}"
        )


def _session_sharpe(returns: dict[str, float], days: Sequence[str]) -> float:
    """skfolio's per-session Sharpe ratio over the returns on `days`; NaN if none can be taken."""
    values = [returns[day] for day in days if day in returns]
    if len(values) < 2 or not all(math.isfinite(value) for value in values):
        return math.nan
    return float(Portfolio(X=[[value] for value in values], weights=[1.0]).sharpe_ratio)
