"""The trial ledger (ADR 0035) recording real backtests, read back through a new `Study` object.

No test here opens a network connection, reads a credential or downloads a file. Each test writes a
`ParquetDataCatalog` and an Optuna SQLite study in pytest's temporary directory. The bars are built as
`tests/test_backtest.py` builds them: the asset and bar payloads are Alpaca-shaped, go through the
adapter's `parse_equity` and `parse_bar`, and are 40 daily bars on consecutive weekdays from Monday
2025-03-10, opening at midnight New York time. Closes climb 101 to 115, fall to 100, then climb to 110.

On that series (worked in `tests/test_backtest.py`'s docstring) a 3-bar and an 8-bar average trade
twice; a 3-bar and a 50-bar average never fill their slow window in 40 bars, so they never trade; and a
trade size of 1.5 shares fails the strategy's start, because an equity is whole-share only.

Each bar is stamped available 24 hours after its open, at 04:00 UTC the next day (the adapter's
`parse_bar`), so the 40 session days are the UTC days Tuesday to Saturday of each week, from
2025-03-11 to 2025-05-03.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import optuna
import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.trading.models import Asset
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import DataCatalogConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.model import BarType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import Quantity
from nautilus_trader.model import TraderId
from nautilus_trader.persistence import ParquetDataCatalog
from optuna.trial import TrialState

from canon_trading.backtest import backtest_run_config
from canon_trading.backtest import simulated_alpaca_venue
from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import canonical_json
from canon_trading.ledger import config_key
from canon_trading.ledger import open_study
from canon_trading.ledger import record_trial
from canon_trading.ledger import trial_config
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig
from canon_trading.returns_matrix import daily_series

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from nautilus_trader.model import Bar

NEW_YORK = ZoneInfo("America/New_York")
BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
MSFT_BAR_TYPE = BarType.from_str("MSFT.ALPACA-1-DAY-LAST-EXTERNAL")
BALANCES = ["100000 USD"]
CLOSES = [
    *(float(c) for c in range(101, 116)),
    *(float(c) for c in range(114, 99, -1)),
    *(float(c) for c in range(101, 111)),
]
ASSET = {
    "borrow_status": "easy_to_borrow",
    "class": "us_equity",
    "easy_to_borrow": True,
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "name": "Apple Inc. Common Stock",
    "shortable": True,
    "status": "active",
    "symbol": "AAPL",
    "tradable": True,
}

optuna.logging.set_verbosity(optuna.logging.WARNING)


@pytest.fixture
def stack() -> Iterator[ExitStack]:
    """Closes every study a test opens."""
    with ExitStack() as opened:
        yield opened


def write_catalog(path: Path, symbols: tuple[str, ...] = ("AAPL",)) -> list[Bar]:
    """Write each symbol's instrument and the 40 bars to a new catalog; return the first's bars."""
    path.mkdir(exist_ok=True)
    catalog = ParquetDataCatalog(str(path))
    written = []
    for symbol in symbols:
        instrument = parse_equity(Asset.model_validate({**ASSET, "symbol": symbol}), 0)
        bar_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        bars = []
        day = datetime(2025, 3, 10, tzinfo=NEW_YORK)
        previous = 100.0
        for close in CLOSES:
            while day.weekday() >= 5:
                day += timedelta(days=1)
            payload = {
                "t": day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": previous,
                "h": max(previous, close),
                "l": min(previous, close),
                "c": close,
                "v": 1_000_000,
                "n": 1_000,
                "vw": close,
            }
            bars.append(parse_bar(AlpacaBar(symbol, payload), bar_type, instrument))
            previous = close
            day += timedelta(days=1)
        catalog.write_instruments([instrument])
        catalog.write_bars(bars)
        written.append(bars)
    return written[0]


def strategy(
    *,
    fast: int = 3,
    slow: int = 8,
    size: Quantity | None = None,
    bar_type: BarType = BAR_TYPE,
) -> MaCross:
    return MaCross(
        MaCrossConfig(
            bar_type=bar_type,
            trade_size=size or Quantity.from_int(10),
            fast_period=fast,
            slow_period=slow,
        ),
    )


def keys(study: optuna.Study) -> set[str]:
    return {trial.user_attrs["config_key"] for trial in study.trials}


def test_a_trial_round_trips_through_the_sqlite_file(tmp_path: Path, stack: ExitStack) -> None:
    bars = write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("round-trip", tmp_path / "ledger" / "trials.db"))

    recorded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = stack.enter_context(
        open_study("round-trip", tmp_path / "ledger" / "trials.db")
    ).trials
    result = recorded.run.result
    assert trial.number == recorded.number
    assert trial.state == TrialState.COMPLETE
    assert trial.user_attrs[OUTCOME] == TRADED == recorded.outcome
    assert trial.user_attrs["config_key"] == recorded.config_key == result.run_config_id
    assert trial.user_attrs["run_config_id"] == result.run_config_id
    assert trial.user_attrs["backtest_start"] == result.backtest_start == bars[0].ts_init
    assert trial.user_attrs["backtest_end"] == result.backtest_end == bars[-1].ts_init
    assert trial.user_attrs["returns_series"] == {
        str(ns): value for ns, value in result.returns_series.items()
    }
    assert trial.user_attrs["session_days"] == [str(bar.ts_init // DAY_NS * DAY_NS) for bar in bars]
    assert config_key(trial.user_attrs["config"]) == recorded.config_key
    strategy_config = trial.user_attrs["config"]["strategies"][0]
    assert strategy_config["class"] == "canon_trading.ma_cross:MaCross"
    assert strategy_config["config"]["fast_period"] == 3
    assert strategy_config["config"]["trade_size"] == "10"
    assert strategy_config["config"]["bar_type"] == str(BAR_TYPE)
    returns = trial.user_attrs["returns_series"]
    on_session_days = [returns[day] for day in trial.user_attrs["session_days"] if day in returns]
    assert trial.value == pytest.approx(_sharpe(on_session_days), rel=1e-12)


def test_the_recorded_series_is_the_shape_the_returns_matrix_accepts(
    tmp_path: Path, stack: ExitStack
) -> None:
    """NautilusTrader's own daily series, on a real run, passes ADR 0010 step 2 and loses only zeros."""
    write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("shape", tmp_path / "trials.db"))
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    [trial] = study.trials
    returns = trial.user_attrs["returns_series"]
    sessions = set(trial.user_attrs["session_days"])

    series = daily_series("t", returns, trial.user_attrs["session_days"])

    assert len(series) == len(sessions & set(returns))
    assert all(value == 0.0 for key, value in returns.items() if key not in sessions)
    assert any(value != 0.0 for value in series)


def test_a_failed_and_an_idle_trial_are_counted(tmp_path: Path, stack: ExitStack) -> None:
    write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("outcomes", tmp_path / "trials.db"))

    traded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    idle = record_trial(
        study, tmp_path / "catalog", [strategy(slow=50)], starting_balances=BALANCES
    )
    with pytest.raises(RuntimeError, match=r"trade_size 1\.5 is finer than"):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(size=Quantity.from_str("1.5"))],
            starting_balances=BALANCES,
        )

    trials = stack.enter_context(open_study("outcomes", tmp_path / "trials.db")).trials
    assert [(trial.state, trial.user_attrs[OUTCOME]) for trial in trials] == [
        (TrialState.COMPLETE, TRADED),
        (TrialState.FAIL, IDLE),
        (TrialState.FAIL, FAILED),
    ]
    assert traded.outcome == TRADED and idle.outcome == IDLE
    assert len(keys(study)) == 3
    assert "trade_size 1.5 is finer than" in trials[2].user_attrs["error"]
    assert not idle.run.result.total_orders
    assert set(trials[1].user_attrs["returns_series"].values()) == {0.0}


def test_a_call_refused_before_a_config_exists_is_not_a_trial(
    tmp_path: Path, stack: ExitStack
) -> None:
    write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("refused", tmp_path / "trials.db"))

    with pytest.raises(LookupError, match="holds no MSFT"):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(bar_type=MSFT_BAR_TYPE)],
            starting_balances=BALANCES,
        )

    assert study.trials == []


def test_the_same_config_twice_is_one_configuration_with_two_records(
    tmp_path: Path, stack: ExitStack
) -> None:
    write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("twice", tmp_path / "trials.db"))

    first = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    second = record_trial(
        study,
        tmp_path / "catalog",
        [strategy()],
        starting_balances=BALANCES,
    )

    trials = study.trials
    assert len(trials) == 2
    assert first.config_key == second.config_key
    assert keys(study) == {first.config_key}
    assert trials[0].user_attrs["returns_series"] == trials[1].user_attrs["returns_series"]


def test_the_catalog_path_does_not_change_the_key(tmp_path: Path) -> None:
    write_catalog(tmp_path / "here")
    write_catalog(tmp_path / "there")
    keys_found = {
        config_key(
            trial_config(
                backtest_run_config(tmp_path / place, [strategy()], starting_balances=BALANCES),
                [strategy()],
            ),
        )
        for place in ("here", "there")
    }

    assert len(keys_found) == 1


def _run_config(**changes: object) -> BacktestRunConfig:
    """A run config like `backtest_run_config`'s, with one field changed."""
    fields: dict = {
        "venues": [simulated_alpaca_venue(BALANCES)],
        "data": [
            BacktestDataConfig(
                data_type=NautilusDataType.Bar,
                catalog=DataCatalogConfig(path="/catalog"),
                bar_types=[str(BAR_TYPE)],
            ),
        ],
        "engine": BacktestEngineConfig(),
    }
    fields.update(changes)
    return BacktestRunConfig(**fields)


@pytest.mark.parametrize(
    ("run_config", "strategies"),
    [
        pytest.param(_run_config(), [strategy(fast=4)], id="strategy config"),
        pytest.param(_run_config(), [strategy(), strategy()], id="strategy count"),
        pytest.param(_run_config(venues=[simulated_alpaca_venue(["50000 USD"])]), None, id="venue"),
        pytest.param(
            _run_config(
                data=[
                    BacktestDataConfig(
                        data_type=NautilusDataType.Bar,
                        catalog=DataCatalogConfig(path="/catalog"),
                        bar_types=[str(MSFT_BAR_TYPE)],
                    ),
                ],
            ),
            None,
            id="data",
        ),
        pytest.param(
            _run_config(engine=BacktestEngineConfig(trader_id=TraderId("TESTER-002"))),
            None,
            id="engine",
        ),
        pytest.param(_run_config(start="2025-03-12"), None, id="range start"),
        pytest.param(_run_config(end="2025-04-01"), None, id="range end"),
        pytest.param(_run_config(chunk_size=1000), None, id="chunk size"),
    ],
)
def test_runs_that_differ_in_one_input_get_different_keys(
    run_config: BacktestRunConfig, strategies: list[MaCross] | None
) -> None:
    baseline = config_key(trial_config(_run_config(), [strategy()]))

    assert config_key(trial_config(run_config, strategies or [strategy()])) != baseline


def test_logging_the_catalog_and_the_run_id_leave_the_key_alone() -> None:
    baseline = config_key(trial_config(_run_config(), [strategy()]))
    quieter = _run_config(
        engine=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)),
        data=[
            BacktestDataConfig(
                data_type=NautilusDataType.Bar,
                catalog=DataCatalogConfig(path="/elsewhere"),
                bar_types=[str(BAR_TYPE)],
            ),
        ],
        id="another-id",
    )

    assert config_key(trial_config(quieter, [strategy()])) == baseline


def test_the_canonical_form_is_sorted_compact_json() -> None:
    assert canonical_json({"b": [1, 2.5], "a": None}) == '{"a":null,"b":[1,2.5]}'
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"a": float("nan")})


def test_a_value_with_no_stable_text_form_is_refused() -> None:
    class Opaque:
        pass

    config = MaCrossConfig(bar_type=BAR_TYPE, trade_size=Quantity.from_int(10))
    setattr(config, "extra", Opaque())  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="no stable text form"):
        trial_config(_run_config(), [MaCross(config)])


def test_a_callable_config_field_is_refused() -> None:
    config = MaCrossConfig(bar_type=BAR_TYPE, trade_size=Quantity.from_int(10))
    setattr(config, "sizer", len)  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="no stable text form"):
        trial_config(_run_config(), [MaCross(config)])


def test_a_result_that_cannot_be_recorded_is_told_failed(
    tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run returned, but reading its session days raised: the record fails, and says why."""
    write_catalog(tmp_path / "catalog")
    study = stack.enter_context(open_study("unrecorded", tmp_path / "trials.db"))

    def unreadable(*_: object) -> list[str]:
        raise OSError("catalog went away")

    monkeypatch.setattr("canon_trading.ledger.session_days", unreadable)
    with pytest.raises(OSError, match="catalog went away"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert trial.state == TrialState.FAIL
    assert trial.user_attrs[OUTCOME] == FAILED
    assert trial.user_attrs["error"] == "OSError: catalog went away"
    assert "returns_series" not in trial.user_attrs


def _sharpe(values: list[float]) -> float:
    """Mean over sample standard deviation, worked with the standard library."""
    import statistics

    return statistics.fmean(values) / statistics.stdev(values)
