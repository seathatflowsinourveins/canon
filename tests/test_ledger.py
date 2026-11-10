"""The trial ledger (ADR 0035) recording real backtests, read back through a new `Study` object.

No test here opens a network connection, reads a credential or downloads a file. Each test writes a
`ParquetDataCatalog` and an Optuna SQLite study in pytest's temporary directory. The bars come from the
`alpaca_bars` fixture of `tests/conftest.py`: the asset and bar payloads are Alpaca-shaped, go through
the adapter's `parse_equity` and `parse_bar`, and are 40 daily bars on consecutive weekdays from Monday
2025-03-10, opening at midnight New York time. Closes climb 101 to 115, fall to 100, then climb to 110.

On that series (worked in `tests/test_backtest.py`'s docstring) a 3-bar and an 8-bar average trade
twice; a 3-bar and a 50-bar average never fill their slow window in 40 bars, so they never trade; and a
trade size of 1.5 shares fails the strategy's start, because an equity is whole-share only.

Each bar is stamped available 24 hours after its open, at 04:00 UTC the next day (the adapter's
`parse_bar`), so the 40 session days are the UTC days Tuesday to Saturday of each week, from
2025-03-11 to 2025-05-03. Each study's sample (ADR 0041) is 2025-03-10 to 2025-05-04, every bar of the
catalog, unless a test says otherwise; `append_bars` adds 30 more, from 2025-05-05.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo

import optuna
import pytest
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity
from optuna.exceptions import DuplicatedStudyError
from optuna.trial import TrialState

import canon_trading.ledger
from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import TRADED
from canon_trading.ledger import LedgerNotFound
from canon_trading.ledger import canonical_json
from canon_trading.ledger import config_key
from canon_trading.ledger import create_ledger_study
from canon_trading.ledger import current_sample
from canon_trading.ledger import default_ledger_path
from canon_trading.ledger import move_sample
from canon_trading.ledger import open_study
from canon_trading.ledger import record_trial
from canon_trading.ledger import samples
from canon_trading.ledger import session_day
from canon_trading.ledger import trial_config
from canon_trading.ma_cross import MaCrossConfig
from canon_trading.ranking import RankingStopped
from canon_trading.ranking import rank
from canon_trading.returns_matrix import daily_series

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import AlpacaBars
    from nautilus_trader.model import Bar

NEW_YORK = ZoneInfo("America/New_York")
BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
MSFT_BAR_TYPE = BarType.from_str("MSFT.ALPACA-1-DAY-LAST-EXTERNAL")
BALANCES = ["100000 USD"]
START = "2025-03-10"
END = "2025-05-04"

optuna.logging.set_verbosity(optuna.logging.WARNING)


@pytest.fixture
def stack() -> Iterator[ExitStack]:
    """Closes every study a test opens."""
    with ExitStack() as opened:
        yield opened


def new_study(
    stack: ExitStack, tmp_path: Path, name: str, *, start: str = START, end: str = END
) -> optuna.Study:
    return stack.enter_context(
        create_ledger_study(name, start=start, end=end, path=tmp_path / "trials.db")
    )


APPENDED = [110.0 + (i % 7) - 3 for i in range(30)]


def append_bars(alpaca_bars: AlpacaBars, path: Path) -> list[Bar]:
    """30 more AAPL bars, from Monday 2025-05-05, the weekday after the last of the 40."""
    return alpaca_bars.write(
        path, APPENDED, first=datetime(2025, 5, 5, tzinfo=NEW_YORK), previous=alpaca_bars.CLOSES[-1]
    )


def strategy(
    *,
    fast: int = 3,
    slow: int = 8,
    size: Quantity | None = None,
    bar_type: BarType = BAR_TYPE,
) -> MaCrossConfig:
    return MaCrossConfig(
        bar_type=bar_type,
        trade_size=size or Quantity.from_int(10),
        fast_period=fast,
        slow_period=slow,
    )


def keys(study: optuna.Study) -> set[str]:
    return {trial.user_attrs["config_key"] for trial in study.trials}


def test_a_trial_round_trips_through_the_sqlite_file(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    bars = alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "round-trip")

    recorded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = stack.enter_context(open_study("round-trip", tmp_path / "trials.db")).trials
    result = recorded.run.result
    assert trial.number == recorded.number
    assert trial.state == TrialState.COMPLETE
    assert trial.user_attrs[OUTCOME] == TRADED == recorded.outcome
    assert trial.user_attrs["config_key"] == recorded.config_key == result.run_config_id
    assert trial.user_attrs["run_config_id"] == result.run_config_id
    assert trial.user_attrs["sample"] == 0
    sample = current_sample(study)
    assert trial.user_attrs["backtest_start"] == result.backtest_start == sample.start
    assert trial.user_attrs["backtest_end"] == result.backtest_end == sample.end
    assert sample.start < bars[0].ts_init < bars[-1].ts_init < sample.end
    assert trial.user_attrs["returns_series"] == {
        str(ns): value for ns, value in result.returns_series.items()
    }
    assert trial.user_attrs["session_days"] == [str(bar.ts_init // DAY_NS * DAY_NS) for bar in bars]
    [(digested, digest)] = trial.user_attrs["data_digest"].items()
    assert (digested, len(digest)) == (str(BAR_TYPE), 64)
    assert config_key(trial.user_attrs["config"]) == recorded.config_key
    strategy_config = trial.user_attrs["config"]["strategies"][0]
    assert strategy_config == {
        "class": "canon_trading.ma_cross:MaCross",
        "config": {
            "bar_type": str(BAR_TYPE),
            "fast_period": 3,
            "slow_period": 8,
            "trade_size": "10",
        },
    }
    returns = trial.user_attrs["returns_series"]
    on_session_days = [returns[day] for day in trial.user_attrs["session_days"] if day in returns]
    assert trial.value == pytest.approx(_sharpe(on_session_days), rel=1e-12)


def test_the_recorded_series_is_the_shape_the_returns_matrix_accepts(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """NautilusTrader's own daily series, on a real run, passes ADR 0010 step 2 and loses only zeros."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "shape")
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    [trial] = study.trials
    returns = trial.user_attrs["returns_series"]
    sessions = set(trial.user_attrs["session_days"])

    series = daily_series("t", returns, trial.user_attrs["session_days"])

    assert len(series) == len(sessions & set(returns)) == len(sessions)
    assert all(value == 0.0 for key, value in returns.items() if key not in sessions)
    assert any(value != 0.0 for value in series)


def test_a_failed_and_an_idle_trial_are_counted(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "outcomes")

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

    reopened = stack.enter_context(open_study("outcomes", tmp_path / "trials.db"))
    trials = reopened.trials
    assert [(trial.state, trial.user_attrs[OUTCOME]) for trial in trials] == [
        (TrialState.COMPLETE, TRADED),
        (TrialState.FAIL, IDLE),
        (TrialState.FAIL, FAILED),
    ]
    assert traded.outcome == TRADED and idle.outcome == IDLE
    assert len(keys(reopened)) == 3  # N, read back through a new Study object (ADR 0035)
    assert "trade_size 1.5 is finer than" in trials[2].user_attrs["error"]
    assert not idle.run.result.total_orders
    assert set(trials[1].user_attrs["returns_series"].values()) == {0.0}


def test_a_call_refused_before_a_config_exists_is_not_a_trial(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "refused")

    with pytest.raises(LookupError, match="holds no MSFT"):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(bar_type=MSFT_BAR_TYPE)],
            starting_balances=BALANCES,
        )
    with pytest.raises(ValueError, match="Error parsing `Money`|invalid"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=["lots"])

    assert study.trials == []


def test_the_same_config_twice_is_one_configuration_with_two_records(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "twice")

    first = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    second = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    trials = study.trials
    assert len(trials) == 2
    assert first.config_key == second.config_key
    assert keys(study) == {first.config_key}
    for attribute in ("returns_series", "session_days", "data_digest"):
        assert trials[0].user_attrs[attribute] == trials[1].user_attrs[attribute]


def test_the_catalog_path_does_not_change_the_key_or_the_digest(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    alpaca_bars.write(tmp_path / "here")
    alpaca_bars.write(tmp_path / "there")
    study = new_study(stack, tmp_path, "places")

    for place in ("here", "there"):
        record_trial(study, tmp_path / place, [strategy()], starting_balances=BALANCES)

    assert len(keys(study)) == 1
    assert len({str(trial.user_attrs["data_digest"]) for trial in study.trials}) == 1


@pytest.mark.parametrize(
    ("strategies", "balances"),
    [
        pytest.param([strategy(fast=4)], BALANCES, id="period"),
        pytest.param([strategy(size=Quantity.from_int(20))], BALANCES, id="trade size"),
        pytest.param([strategy(size=Quantity.from_str("10.0"))], BALANCES, id="size precision"),
        pytest.param([strategy(), strategy()], BALANCES, id="strategy count"),
        pytest.param([strategy(bar_type=MSFT_BAR_TYPE)], BALANCES, id="bar type"),
        pytest.param([strategy()], ["50000 USD"], id="balance"),
        pytest.param([strategy()], ["100000 USD", "10 EUR"], id="second balance"),
    ],
)
def test_runs_that_differ_in_one_input_get_different_keys(
    strategies: list[MaCrossConfig], balances: list[str]
) -> None:
    baseline = config_key(trial_config([strategy()], starting_balances=BALANCES))

    assert config_key(trial_config(strategies, starting_balances=balances)) != baseline


@pytest.mark.parametrize("balance", ["100000 USD", "100_000 USD", "100000.00 USD", "1e5 USD"])
def test_one_configuration_spelled_two_ways_has_one_key(balance: str) -> None:
    """The review found "100000 USD" and "100_000 USD" keyed apart with bit-identical returns."""
    baseline = config_key(trial_config([strategy()], starting_balances=["100000 USD"]))

    assert config_key(trial_config([strategy()], starting_balances=[balance])) == baseline


def test_the_key_hashes_only_what_this_repo_sets() -> None:
    """No property upstream reflects is in the key, so a default moved by a pin moves no key."""
    assert canonical_json(trial_config([strategy()], starting_balances=BALANCES)) == (
        '{"data":{"bar_types":["AAPL.ALPACA-1-DAY-LAST-EXTERNAL"],"data_type":"Bar"},'
        '"strategies":[{"class":"canon_trading.ma_cross:MaCross","config":{'
        '"bar_type":"AAPL.ALPACA-1-DAY-LAST-EXTERNAL","fast_period":3,"slow_period":8,'
        '"trade_size":"10"}}],'
        '"venue":{"account_type":"CASH","book_type":"L1_MBP","fee_model":{'
        '"class":"MakerTakerFeeModel","maker_rate":"0","taker_rate":"0"},"name":"ALPACA",'
        '"oms_type":"NETTING","starting_balances":["100000.00 USD"]}}'
    )


def test_the_canonical_form_is_sorted_compact_json() -> None:
    assert canonical_json({"b": [1, 2.5], "a": None}) == '{"a":null,"b":[1,2.5]}'
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"a": float("nan")})


def test_a_value_with_no_stable_text_form_is_refused() -> None:
    class Opaque:
        pass

    config = strategy()
    setattr(config, "extra", Opaque())  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="no stable text form"):
        trial_config([config], starting_balances=BALANCES)


def test_a_callable_config_field_is_refused() -> None:
    config = strategy()
    setattr(config, "sizer", len)  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="no stable text form"):
        trial_config([config], starting_balances=BALANCES)


def test_a_config_changed_between_records_is_hashed_as_it_ran(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """The review's loop: one config object mutated between runs. Each record holds what it ran."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "mutated")
    shared = strategy()

    for fast, slow in ((3, 8), (2, 5), (4, 9)):
        shared.fast_period, shared.slow_period = fast, slow
        record_trial(study, tmp_path / "catalog", [shared], starting_balances=BALANCES)
    shared.fast_period, shared.slow_period = 5, 6

    stored = [trial.user_attrs["config"]["strategies"][0]["config"] for trial in study.trials]
    assert [(c["fast_period"], c["slow_period"]) for c in stored] == [(3, 8), (2, 5), (4, 9)]
    assert len(keys(study)) == 3
    alone = new_study(stack, tmp_path, "alone")
    for fast, slow in ((3, 8), (2, 5), (4, 9)):
        record_trial(
            alone,
            tmp_path / "catalog",
            [strategy(fast=fast, slow=slow)],
            starting_balances=BALANCES,
        )
    assert [t.user_attrs["returns_series"] for t in study.trials] == [
        t.user_attrs["returns_series"] for t in alone.trials
    ]


def test_a_result_that_cannot_be_recorded_is_told_failed(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run returned, but reading the bars it read raised: the record fails, and says why."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "unrecorded")

    def unreadable(*_: object) -> list[Bar]:
        raise OSError("catalog went away")

    monkeypatch.setattr("canon_trading.ledger.bars_read", unreadable)
    with pytest.raises(OSError, match="catalog went away"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert trial.state == TrialState.FAIL
    assert trial.user_attrs[OUTCOME] == FAILED
    assert trial.user_attrs["error"] == "OSError: catalog went away"
    assert "returns_series" not in trial.user_attrs


# The ledger's location, and what reading a ledger may not do (ADR 0035 term 1).


def test_every_directory_resolves_the_one_ledger_of_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review gave one study N = 2 in a second directory where the first had N = 28."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(canon_trading.ledger.__file__).parent,
    ).stdout.strip()
    found = set()
    for place in (tmp_path, Path(canon_trading.ledger.__file__).parent, Path("/")):
        monkeypatch.chdir(place)
        found.add(default_ledger_path())

    assert found == {Path(common).parent / "ledger" / "trials.db"}


def test_outside_a_git_checkout_there_is_no_default_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canon_trading.ledger, "__file__", str(tmp_path / "ledger.py"))

    with pytest.raises(LedgerNotFound, match="not in a git checkout"):
        default_ledger_path()


def test_reading_a_missing_ledger_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "ledger" / "trials.db"

    with pytest.raises(LedgerNotFound, match="there is no ledger"), open_study("s", missing):
        pass

    assert not missing.parent.exists()


def test_reading_a_missing_study_creates_nothing(tmp_path: Path, stack: ExitStack) -> None:
    new_study(stack, tmp_path, "present")

    with pytest.raises(LedgerNotFound, match="holds no study 'absent'"):
        stack.enter_context(open_study("absent", tmp_path / "trials.db"))

    names = optuna.get_all_study_names(f"sqlite:///{tmp_path / 'trials.db'}")
    assert names == ["present"]


def test_a_file_that_is_not_a_ledger_is_refused_unchanged(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()

    with pytest.raises(LedgerNotFound, match="is not an Optuna ledger"), open_study("s", empty):
        pass

    assert empty.read_bytes() == b""


def test_a_study_is_created_once(tmp_path: Path, stack: ExitStack) -> None:
    new_study(stack, tmp_path, "once")

    with pytest.raises(DuplicatedStudyError):
        new_study(stack, tmp_path, "once")


def test_a_path_with_uri_characters_opens_that_file(tmp_path: Path, stack: ExitStack) -> None:
    """`?`, `%41` and braces in a path name the file itself, not a query or an escape."""
    for name in ("q?mark", "pct%41", "br{x}", "sp ace#"):
        path = tmp_path / name / "trials.db"
        stack.enter_context(create_ledger_study("s", start=START, end=END, path=path))
        assert path.is_file()
        assert stack.enter_context(open_study("s", path)).study_name == "s"

    assert sorted(p.name for p in tmp_path.iterdir()) == ["br{x}", "pct%41", "q?mark", "sp ace#"]


# The sample (ADR 0041).


def test_a_study_keeps_its_samples_and_moving_one_keeps_every_record(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "moved")
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    later = move_sample(study, start=START, end="2025-06-16")
    again = move_sample(study, start=START, end="2025-06-16")

    reopened = stack.enter_context(open_study("moved", tmp_path / "trials.db"))
    assert [s.number for s in samples(reopened)] == [0, 1, 2]
    assert current_sample(reopened) == again
    assert (later.start, later.end) == (again.start, again.end)
    assert len(reopened.trials) == 1
    with pytest.raises(ValueError, match="start < end"):
        move_sample(study, start=END, end=START)


def test_the_run_is_bounded_by_the_sample_and_its_end_is_included(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    bars = alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "bounded", start=START, end=END)
    move_sample(study, start=bars[5].ts_init, end=bars[29].ts_init)

    recorded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert trial.user_attrs["sample"] == 1
    assert trial.user_attrs["backtest_start"] == bars[5].ts_init
    assert trial.user_attrs["backtest_end"] == bars[29].ts_init
    assert recorded.run.result.iterations == 25  # the node replayed both bars at the bounds
    assert trial.user_attrs["session_days"] == [
        str(bar.ts_init // DAY_NS * DAY_NS) for bar in bars[5:30]
    ]


def test_bars_added_after_the_sample_change_no_trial_on_it(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """The review: after 30 bars were appended, a new trial was ranked on another range than the rest."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "grown")
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    record_trial(
        study, tmp_path / "catalog", [strategy(fast=2, slow=5)], starting_balances=BALANCES
    )

    append_bars(alpaca_bars, tmp_path / "catalog")
    record_trial(
        study, tmp_path / "catalog", [strategy(fast=4, slow=9)], starting_balances=BALANCES
    )
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    attrs = [trial.user_attrs for trial in study.trials]
    assert {a["backtest_end"] for a in attrs} == {attrs[0]["backtest_end"]}
    assert {str(a["data_digest"]) for a in attrs} == {str(attrs[0]["data_digest"])}
    assert len({tuple(a["session_days"]) for a in attrs}) == 1
    report = rank(study)
    assert (report.n, report.t) == (3, 40)


def test_a_moved_sample_needs_every_configuration_run_on_it(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """N carries over; the ranking reads only the current sample and names what has not run on it."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "rerun")
    configs = [strategy(), strategy(fast=2, slow=5), strategy(fast=4, slow=9)]
    for config in configs:
        record_trial(study, tmp_path / "catalog", [config], starting_balances=BALANCES)
    append_bars(alpaca_bars, tmp_path / "catalog")
    move_sample(study, start=START, end="2025-06-16")
    first = record_trial(study, tmp_path / "catalog", [configs[0]], starting_balances=BALANCES)

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert "(N = 3)" in message
    assert message.count("not run on the current sample 1") == 2
    assert first.config_key not in message
    for config in configs[1:]:
        record_trial(study, tmp_path / "catalog", [config], starting_balances=BALANCES)
    report = rank(study)
    assert (report.n, report.t, report.records, report.all_records) == (3, 70, 3, 6)
    assert "sample 1 (2025-03-10T00:00:00+00:00 to 2025-06-16T00:00:00+00:00)" in report.summary()


def test_a_corrected_catalog_disagrees_until_the_sample_is_moved(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """Same range, other bars: the digest differs, the ranking stops, and a new sample clears it."""
    alpaca_bars.write(tmp_path / "catalog")
    corrected = list(alpaca_bars.CLOSES)
    corrected[9] += 0.5
    alpaca_bars.write(tmp_path / "corrected", corrected)
    study = new_study(stack, tmp_path, "corrected")
    for place in ("catalog", "corrected"):
        record_trial(study, tmp_path / place, [strategy()], starting_balances=BALANCES)
    record_trial(
        study, tmp_path / "catalog", [strategy(fast=2, slow=5)], starting_balances=BALANCES
    )

    with pytest.raises(
        RankingStopped, match="disagree on their range, returns, session days or data"
    ):
        rank(study)
    move_sample(study, start=START, end=END)
    for config in (strategy(), strategy(fast=2, slow=5)):
        record_trial(study, tmp_path / "corrected", [config], starting_balances=BALANCES)
    assert rank(study).n == 2


def test_two_configurations_that_read_different_bars_on_one_sample_stop(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A catalog corrected in place between two configurations, with no sample moved."""
    alpaca_bars.write(tmp_path / "catalog")
    corrected = list(alpaca_bars.CLOSES)
    corrected[9] += 0.5
    alpaca_bars.write(tmp_path / "corrected", corrected)
    study = new_study(stack, tmp_path, "read apart")
    first = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    second = record_trial(
        study, tmp_path / "corrected", [strategy(fast=2, slow=5)], starting_balances=BALANCES
    )

    with pytest.raises(RankingStopped) as stopped:
        rank(study)

    message = str(stopped.value)
    assert f"{BAR_TYPE}: records on sample 0" in message
    assert f"#{first.number}" in message and f"#{second.number}" in message


def test_a_sample_the_catalog_does_not_reach_is_refused_before_a_trial(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A mistyped year would otherwise replay nothing and be recorded as an idle trial."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "mistyped", start="2015-03-10", end="2015-05-04")

    with pytest.raises(LookupError, match="holds no AAPL.ALPACA-1-DAY-LAST-EXTERNAL bars from"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    assert study.trials == []


@pytest.mark.parametrize("failing", ["config_key", "outcome"])
def test_an_error_after_the_trial_is_asked_for_is_told_failed(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    """A locked store on the first write, or on the outcome, left the record RUNNING for good."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "locked")
    real = optuna.trial.Trial.set_user_attr

    def locked(trial: optuna.trial.Trial, key: str, value: object) -> None:
        if key == failing and value != FAILED:
            raise OSError("database is locked")
        real(trial, key, value)

    monkeypatch.setattr(optuna.trial.Trial, "set_user_attr", locked)
    with pytest.raises(OSError, match="database is locked"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME]) == (TrialState.FAIL, FAILED)
    assert trial.user_attrs["error"] == "OSError: database is locked"


def test_an_interrupt_during_the_run_is_told_failed(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "interrupted")

    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("canon_trading.ledger.run_backtest_config", interrupted)
    with pytest.raises(KeyboardInterrupt):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME]) == (TrialState.FAIL, FAILED)
    assert "config_key" in trial.user_attrs


def test_an_interrupt_just_after_the_tell_leaves_the_record_as_told(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "late interrupt")
    real = optuna.Study.tell

    def tell_then_interrupt(self: optuna.Study, *args: Any, **kwargs: Any) -> object:
        real(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(optuna.Study, "tell", tell_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as interrupted:
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME]) == (TrialState.COMPLETE, TRADED)
    assert interrupted.value.__notes__ == [
        f"trial #{trial.number} was already told COMPLETE; left as it is"
    ]


@pytest.mark.parametrize(
    ("ts", "day"),
    [
        pytest.param(DAY_NS * 20_160, DAY_NS * 20_159, id="exactly midnight"),
        pytest.param(DAY_NS * 20_160 + 1, DAY_NS * 20_160, id="just after"),
        pytest.param(DAY_NS * 20_161 - 1, DAY_NS * 20_160, id="just before the next"),
        pytest.param(0, 0, id="the epoch"),
    ],
)
def test_an_instant_at_utc_midnight_belongs_to_the_day_ending_there(ts: int, day: int) -> None:
    assert session_day(ts) == day


def test_daily_bars_stamped_at_utc_midnight_rank(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """Bars opening at 00:00 UTC are stamped available at the next midnight; the review found their
    returns on days the ledger called closed, so every ranking of the study stopped."""
    bars = alpaca_bars.write(tmp_path / "catalog", first=datetime(2025, 3, 10, tzinfo=UTC))
    assert all(bar.ts_init % DAY_NS == 0 for bar in bars)
    study = new_study(stack, tmp_path, "midnight")
    for fast, slow in ((3, 8), (2, 5)):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(fast=fast, slow=slow)],
            starting_balances=BALANCES,
        )

    report = rank(study)

    assert (report.n, report.t) == (2, 40)
    assert report.first_day == "2025-03-10"


def test_two_processes_creating_one_new_ledger_both_succeed(tmp_path: Path) -> None:
    """Without the creation lock, 14 of 15 rounds crashed: "table studies already exists".

    Each child imports first, then marks itself ready and waits for the other, so both reach the
    creation together however long the imports take.
    """
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from canon_trading.ledger import create_ledger_study\n"
        "here, name, other = Path(sys.argv[1]), sys.argv[2], sys.argv[3]\n"
        "(here.parent / f'ready-{name}').touch()\n"
        "deadline = time.monotonic() + 60\n"
        "while not (here.parent / f'ready-{other}').exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.001)\n"
        "with create_ledger_study(name, start='2025-03-10', end='2025-05-04', path=here):\n"
        "    pass\n"
    )
    for round_ in range(3):
        path = tmp_path / f"round-{round_}" / "trials.db"
        path.parent.mkdir()
        runs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(path), f"s{n}", f"s{1 - n}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for n in range(2)
        ]
        results = [(run.communicate(timeout=120)[1], run.returncode) for run in runs]
        assert [code for _, code in results] == [0, 0], [err[-300:] for err, _ in results]
        assert sorted(optuna.get_all_study_names(f"sqlite:///{path}")) == ["s0", "s1"]


def test_the_value_told_is_none_for_a_series_the_gate_rejects(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With five session days lost, returns fall on days with no bar: the gate refuses the series, so
    the run is told no value, `FAIL` and still `traded`, not `COMPLETE` with a Sharpe ratio."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "refused value")
    real = canon_trading.ledger.session_days
    monkeypatch.setattr(canon_trading.ledger, "session_days", lambda bars: real(bars)[:-5])

    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME], trial.value) == (TrialState.FAIL, TRADED, None)


def test_the_ledger_folder_is_ignored_only_at_the_root() -> None:
    root = Path(canon_trading.ledger.__file__).resolve().parents[2]

    def ignored(path: str) -> bool:
        return (
            subprocess.run(["git", "check-ignore", "-q", path], cwd=root, check=False).returncode
            == 0
        )

    assert ignored("ledger/trials.db")
    assert not ignored("src/canon_trading/ledger/module.py")


def _sharpe(values: list[float]) -> float:
    """Mean over sample standard deviation, worked with the standard library."""
    import statistics

    return statistics.fmean(values) / statistics.stdev(values)
