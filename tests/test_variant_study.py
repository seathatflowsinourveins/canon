"""The step 7 study's wiring (ADR 0048), offline, on Alpaca-shaped bars the tests make.

What is checked: the pre-registered grid is the eight configurations ADR 0048 lists, each its own
config key; a pass records every configuration as one trial, two instruments in one study, and the
gate ranks them; a second pass records nothing new, and one after a killed pass runs again only the
key the gate would stop on; a configuration that never trades is recorded and stops the ranking, as
ADR 0010's Limits give; a refusal before any trial stops the pass; nothing is created unless asked; a
study on another range, and a second pass on one study, are refused.
The bars are invented (`conftest.AlpacaBars`), so no number here says anything about a market.
"""

from __future__ import annotations

import fcntl
from datetime import datetime
from typing import TYPE_CHECKING

import optuna
import pytest
from nautilus_trader.model import Quantity
from optuna.exceptions import DuplicatedStudyError

import canon_trading.ledger
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import LedgerNotFound
from canon_trading.ledger import create_ledger_study
from canon_trading.ledger import open_study
from canon_trading.ranking import RankingReport
from canon_trading.ranking import RankingStopped
from canon_trading.ranking import rank
from canon_trading.variant_study import NEW_YORK
from canon_trading.variant_study import PERIODS
from canon_trading.variant_study import SYMBOLS
from canon_trading.variant_study import WINDOW_END
from canon_trading.variant_study import WINDOW_START
from canon_trading.variant_study import bar_type
from canon_trading.variant_study import grid
from canon_trading.variant_study import key_of
from canon_trading.variant_study import legend
from canon_trading.variant_study import run_study

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from conftest import AlpacaBars
    from nautilus_trader.config import BacktestRunConfig

    from canon_trading.backtest import BacktestRun
    from canon_trading.ma_cross import MaCross

optuna.logging.set_verbosity(optuna.logging.WARNING)

# The 40 generated bars open on weekdays from 2025-03-10 to 2025-05-02; their `ts_init` is the next
# midnight New York, so this range holds all of them.
START = datetime(2025, 3, 10, 12, tzinfo=NEW_YORK)
END = datetime(2025, 5, 3, 12, tzinfo=NEW_YORK)
QQQ_CLOSES = tuple(100.0 + (i * 7) % 13 for i in range(40))
SHORT = ((2, 5), (3, 8))


def write_both(alpaca_bars: AlpacaBars, path: Path) -> None:
    alpaca_bars.write(path, symbols=("SPY",))
    alpaca_bars.write(path, QQQ_CLOSES, symbols=("QQQ",))


def test_the_grid_is_the_plan_of_adr_0048() -> None:
    configs = grid()

    assert SYMBOLS == ("SPY", "QQQ")
    assert PERIODS == ((10, 20), (5, 20), (10, 50), (20, 100))
    assert [(str(c.bar_type), c.fast_period, c.slow_period) for c in configs] == [
        (f"{symbol}.ALPACA-1-DAY-LAST-EXTERNAL", fast, slow)
        for symbol in SYMBOLS
        for fast, slow in PERIODS
    ]
    assert all(c.trade_size == Quantity.from_int(100) for c in configs)
    assert len({key_of(c) for c in configs}) == 8
    assert sorted(legend(configs).values()) == sorted(
        f"{s} {f}/{w}" for s in SYMBOLS for f, w in PERIODS
    )
    assert WINDOW_START.isoformat() == "2020-07-27T12:00:00-04:00"
    assert WINDOW_END.isoformat() == "2026-01-01T12:00:00-05:00"


def test_two_instruments_are_recorded_and_ranked_in_one_study(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = grid(periods=SHORT)
    ledger = tmp_path / "trials.db"

    recorded, report = run_study(
        tmp_path / "catalog",
        name="s",
        ledger_path=ledger,
        configs=configs,
        start=START,
        end=END,
        create=True,
    )

    assert recorded is not None
    assert recorded.failed == ()
    assert len(recorded.recorded) == 4
    assert isinstance(report, RankingReport), report
    assert (report.n, report.t) == (4, 40)
    assert report.pick in legend(configs)
    with open_study("s", ledger) as study:
        outcomes = [t.user_attrs[OUTCOME] for t in study.trials]
        bar_types = {b for t in study.trials for b in t.user_attrs["data_digest"]}
    assert outcomes == [TRADED] * 4
    assert bar_types == {str(bar_type("SPY")), str(bar_type("QQQ"))}

    again, report_again = run_study(
        tmp_path / "catalog", name="s", ledger_path=ledger, configs=configs, start=START, end=END
    )
    assert again is not None
    assert (again.recorded, len(again.skipped)) == ((), 4)
    assert report_again == report


def test_a_configuration_that_never_trades_is_a_trial_and_stops_the_ranking(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = grid(periods=((2, 5), (20, 100)))

    recorded, outcome = run_study(
        tmp_path / "catalog",
        name="idle",
        ledger_path=tmp_path / "trials.db",
        configs=configs,
        start=START,
        end=END,
        create=True,
    )

    assert recorded is not None
    assert len(recorded.recorded) == 4
    assert isinstance(outcome, RankingStopped)
    with open_study("idle", tmp_path / "trials.db") as study:
        idle = [t for t in study.trials if t.user_attrs[OUTCOME] == IDLE]
    assert len(idle) == 2
    for trial in idle:
        assert f"{trial.user_attrs['config_key']}: never traded" in str(outcome)


def test_a_study_on_another_range_is_refused(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    ledger = tmp_path / "trials.db"
    with create_ledger_study("s", start="2025-03-10", end="2025-05-04", path=ledger):
        pass

    with pytest.raises(ValueError, match="not the planned range"):
        run_study(tmp_path / "catalog", name="s", ledger_path=ledger, start=START, end=END)


def test_nothing_is_created_unless_asked(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    for rank_only in (False, True):
        with pytest.raises(LedgerNotFound):
            run_study(
                tmp_path / "catalog", name="s", ledger_path=tmp_path / "t.db", rank_only=rank_only
            )
    assert not (tmp_path / "t.db").exists()

    write_both(alpaca_bars, tmp_path / "catalog")
    run_study(
        tmp_path / "catalog",
        name="s",
        ledger_path=tmp_path / "t.db",
        configs=grid(periods=SHORT),
        start=START,
        end=END,
        create=True,
    )
    with pytest.raises(DuplicatedStudyError):
        run_study(tmp_path / "catalog", name="s", ledger_path=tmp_path / "t.db", create=True)


@pytest.mark.parametrize("with_sample", [True, False], ids=["sample written", "key only"])
def test_a_key_whose_latest_record_did_not_run_is_run_again(
    alpaca_bars: AlpacaBars, tmp_path: Path, with_sample: bool
) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = grid(periods=SHORT)
    ledger = tmp_path / "trials.db"
    run_study(
        tmp_path / "catalog",
        name="s",
        ledger_path=ledger,
        configs=configs,
        start=START,
        end=END,
        create=True,
    )
    key = key_of(configs[0])
    with open_study("s", ledger) as study:
        # What a pass killed during its run leaves: a later record of the key, still RUNNING, with
        # its sample number, or killed between the key's write and the sample's, without it.
        left = study.ask()
        left.set_user_attr("config_key", key)
        if with_sample:
            left.set_user_attr("sample", 0)
        with pytest.raises(RankingStopped, match=f"{key}: no later record ran to the end"):
            rank(study)

    recorded, report = run_study(
        tmp_path / "catalog", name="s", ledger_path=ledger, configs=configs, start=START, end=END
    )

    assert recorded is not None
    assert len(recorded.recorded) == 1
    assert len(recorded.skipped) == 3
    assert isinstance(report, RankingReport), report


def test_a_refusal_before_a_trial_stops_the_pass(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = [*grid(symbols=("MSFT",), periods=SHORT[:1]), *grid(periods=SHORT)]

    with pytest.raises(LookupError, match="holds no MSFT"):
        run_study(
            tmp_path / "catalog",
            name="s",
            ledger_path=tmp_path / "trials.db",
            configs=configs,
            start=START,
            end=END,
            create=True,
        )
    with open_study("s", tmp_path / "trials.db") as study:
        assert study.trials == []


def test_a_second_pass_on_the_study_is_refused(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    ledger = tmp_path / "trials.db"
    with (tmp_path / "trials.db.s.pass.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="holds its lock"):
            run_study(tmp_path / "catalog", name="s", ledger_path=ledger, create=True)
    assert not ledger.exists()


def test_a_run_that_raises_is_recorded_failed_and_the_pass_goes_on(
    alpaca_bars: AlpacaBars, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = grid(periods=SHORT)
    real = canon_trading.ledger.run_backtest_config
    calls = []

    def first_raises(config: BacktestRunConfig, strategies: Sequence[MaCross]) -> BacktestRun:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("engine error")
        return real(config, strategies)

    monkeypatch.setattr(canon_trading.ledger, "run_backtest_config", first_raises)
    recorded, outcome = run_study(
        tmp_path / "catalog",
        name="s",
        ledger_path=tmp_path / "trials.db",
        configs=configs,
        start=START,
        end=END,
        create=True,
    )

    assert recorded is not None
    assert len(recorded.recorded) == 3
    assert len(recorded.failed) == 1
    assert "engine error" in recorded.failed[0]
    assert isinstance(outcome, RankingStopped)
    assert f"{key_of(configs[0])}: no record ran to the end" in str(outcome)


def test_a_completed_record_with_no_sample_number_is_not_a_run_of_its_key(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    write_both(alpaca_bars, tmp_path / "catalog")
    configs = grid(symbols=("SPY",), periods=SHORT)
    ledger = tmp_path / "trials.db"
    with create_ledger_study(
        "s", start=START.isoformat(), end=END.isoformat(), path=ledger
    ) as study:
        # Another writer's record: the first key, told COMPLETE and traded, with no sample number.
        stray = study.ask()
        stray.set_user_attr("config_key", key_of(configs[0]))
        stray.set_user_attr(OUTCOME, TRADED)
        study.tell(stray, 0.1)

    recorded, outcome = run_study(
        tmp_path / "catalog", name="s", ledger_path=ledger, configs=configs, start=START, end=END
    )

    # As `rank` reads it, the stray is no run of the key, so the pass runs the key; `rank` still
    # stops on the stray, which no run clears.
    assert recorded is not None
    assert len(recorded.recorded) == 2
    assert isinstance(outcome, RankingStopped)
    assert "not a sample number of the study" in str(outcome)
