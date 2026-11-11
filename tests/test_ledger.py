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
catalog, unless a test says otherwise; `append_bars` adds 30 more, from 2025-05-05, and the catalog
then covers 2025-03-10 to 2025-06-15.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import math
import shutil
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo

import optuna
import pytest
from alpaca.trading.models import Asset
from canon_alpaca.providers import parse_equity
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog
from optuna.exceptions import DuplicatedStudyError
from optuna.trial import TrialState

import canon_trading.ledger
from canon_trading.ledger import DAY_NS
from canon_trading.ledger import FAILED
from canon_trading.ledger import IDLE
from canon_trading.ledger import NOT_RUN
from canon_trading.ledger import OUTCOME
from canon_trading.ledger import SAMPLES
from canon_trading.ledger import TRADED
from canon_trading.ledger import LedgerNotFound
from canon_trading.ledger import Sample
from canon_trading.ledger import canonical_json
from canon_trading.ledger import config_key
from canon_trading.ledger import create_ledger_study
from canon_trading.ledger import current_sample
from canon_trading.ledger import data_digest
from canon_trading.ledger import default_ledger_path
from canon_trading.ledger import instrument_digest
from canon_trading.ledger import mark_not_run
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
from canon_trading.session_ma_cross import SessionMaCrossConfig

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
        path,
        APPENDED,
        first=datetime(2025, 5, 5, tzinfo=NEW_YORK),
        previous=alpaca_bars.CLOSES[-1],
        covered_from=utc_ns(END) + 1,  # the next load asks from where the last one ended
    )


def utc_ns(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) * 1_000_000_000


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
    assert trial.system_attrs == {}  # the ranking reads any as another writer's (ADR 0054)
    digests = trial.user_attrs["data_digest"]
    assert set(digests) == {str(BAR_TYPE), str(BAR_TYPE.instrument_id)}
    assert all(len(digest) == 64 for digest in digests.values())
    assert digests[str(BAR_TYPE.instrument_id)] == instrument_digest(alpaca_bars.equity())
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
    assert len(keys(reopened)) == 3  # N, read back through a new Study object (ADR 0054 term 5)
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

    with pytest.raises(TypeError, match="not as MaCrossConfig builds it"):
        trial_config([config], starting_balances=BALANCES)
    # The key's own refusal still stands behind it, for a field the class may declare later.
    with pytest.raises(TypeError, match="no stable text form"):
        canon_trading.ledger._field("extra", vars(config)["extra"])


def test_a_callable_config_field_is_refused() -> None:
    config = strategy()
    setattr(config, "sizer", len)  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="not as MaCrossConfig builds it"):
        trial_config([config], starting_balances=BALANCES)
    # The key's own refusal still stands behind it, for a field the class may declare later.
    with pytest.raises(TypeError, match="no stable text form"):
        canon_trading.ledger._field("sizer", vars(config)["sizer"])


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
    """The run returned, but reading the days of the bars it read raised: the record fails, and says
    why."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "unrecorded")

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

    later = move_sample(study, start=START, end="2025-06-15")
    again = move_sample(study, start=START, end="2025-06-15")

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
    move_sample(study, start=START, end="2025-06-15")
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
    assert "sample 1 (2025-03-10T00:00:00+00:00 to 2025-06-15T00:00:00+00:00)" in report.summary()


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
        RankingStopped, match="disagree on their outcome, range, returns, session days or data"
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

    with pytest.raises(LookupError, match="does not cover the sample: missing 2015-03-10"):
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
    assert trial.state == TrialState.FAIL
    assert trial.user_attrs["error"] == "OSError: database is locked"
    if failing == "outcome":
        assert trial.user_attrs[OUTCOME] == FAILED
    else:
        # No key was written, so no run started: no outcome, which would show one (ADR 0053
        # term 3), and the record is cleared as one that ran nothing.
        assert OUTCOME not in trial.user_attrs
        mark_not_run(study, trial.number)
        assert study.user_attrs[NOT_RUN] == [trial.number]


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


@pytest.mark.parametrize("refusals", [1, 2], ids=["the tell", "the tell and its FAIL"])
def test_a_tell_refused_after_the_run_keeps_the_outcome_it_ran_to(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    refusals: int,
) -> None:
    """A second reader: a store that refused the last `tell`, after every result and the outcome were
    written, had the record overwritten `failed`, so a later record of its key resolved it and the
    ranking never compared the returns and data it had read."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "refused tell")
    real = optuna.Study.tell
    left = [refusals]

    def refused(self: optuna.Study, *args: Any, **kwargs: Any) -> object:
        if left[0]:
            left[0] -= 1
            raise OSError("database is locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(optuna.Study, "tell", refused)
    with pytest.raises(OSError, match="database is locked"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    monkeypatch.undo()

    [trial] = study.trials
    assert trial.user_attrs[OUTCOME] == TRADED
    assert trial.state == (TrialState.FAIL if refusals == 1 else TrialState.RUNNING)
    assert trial.user_attrs["error"] == "OSError: database is locked"

    # It ran to the end, so a later record of its key that read other returns disagrees with it,
    # where it used to resolve it: the later record here is the same run with one return changed.
    returns = dict(trial.user_attrs["returns_series"])
    first_day = next(iter(returns))
    returns[first_day] += 0.01
    study.add_trial(
        optuna.trial.create_trial(
            state=TrialState.FAIL,  # traded, with no value, as `record_trial` tells returns it refuses
            user_attrs={**trial.user_attrs, "returns_series": returns},
        )
    )
    later = study.trials[-1].number
    with pytest.raises(
        RankingStopped, match=rf"#{trial.number} \(traded\), #{later} \(traded\) ran the same"
    ):
        rank(study)


def test_a_config_of_another_strategy_class_is_refused_before_a_trial(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A second reader: `record_trial` builds `MaCross` from every config, and a `SessionMaCrossConfig`
    was keyed with session fields that never ran (two limit offsets, two trials of one run), or
    refused for a `wind_down_at` with no canonical form."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "session config")
    session = SessionMaCrossConfig(
        bar_type=BAR_TYPE,
        trade_size=Quantity.from_int(10),
        fast_period=3,
        slow_period=8,
        limit_offset=Decimal("0.02"),
    )

    for refused in (
        lambda: trial_config([session], starting_balances=BALANCES),
        lambda: record_trial(study, tmp_path / "catalog", [session], starting_balances=BALANCES),
    ):
        with pytest.raises(TypeError, match="got SessionMaCrossConfig, not MaCrossConfig"):
            refused()
    assert study.trials == []


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
    # Ending on the session day of the last bar, 2025-05-02 (`_refuse_unbooked_ends`).
    study = new_study(stack, tmp_path, "midnight", end="2025-05-03")
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


# A later review of the gate, 2026-11-10: each test failed before its fix.


def test_the_digest_is_of_the_bars_the_run_read_not_a_later_catalog(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog replaced between the replay and the hash gave the old run the new bars' digest, so
    two runs on different bars agreed and the gate ranked them (DSR 0.95144 in the review)."""
    original = alpaca_bars.write(tmp_path / "catalog")
    corrected = list(alpaca_bars.CLOSES)
    corrected[9] += 0.5
    study = new_study(stack, tmp_path, "replaced")
    real = canon_trading.ledger.run_backtest_config

    def run_then_replace(*args: Any) -> object:
        run = real(*args)
        shutil.rmtree(tmp_path / "catalog")
        alpaca_bars.write(tmp_path / "catalog", corrected)
        return run

    monkeypatch.setattr(canon_trading.ledger, "run_backtest_config", run_then_replace)
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    monkeypatch.undo()
    record_trial(
        study, tmp_path / "catalog", [strategy(fast=2, slow=5)], starting_balances=BALANCES
    )

    first, second = study.trials
    assert first.user_attrs["data_digest"] == data_digest(
        original, [str(BAR_TYPE)], [alpaca_bars.equity()]
    )
    assert second.user_attrs["data_digest"] != first.user_attrs["data_digest"]
    with pytest.raises(RankingStopped, match="read different bars"):
        rank(study)


@pytest.mark.parametrize(
    ("start", "end", "missing"),
    [
        pytest.param(START, "2026-05-04", "2025-05-04", id="bars end a year early"),
        pytest.param("2024-03-10", END, "2024-03-10", id="bars start a year late"),
    ],
)
def test_a_sample_the_catalog_covers_in_part_is_refused_naming_the_gap(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    stack: ExitStack,
    start: str,
    end: str,
    missing: str,
) -> None:
    """Any overlap was accepted: on a sample to May 2026 with bars to May 2025, two configurations
    ranked on the 40 days they shared (DSR 0.98118 in the review)."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "in part", start=start, end=end)

    with pytest.raises(LookupError, match=rf"does not cover the sample: missing {missing}"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    assert study.trials == []


def test_a_keyed_failure_before_its_sample_is_written_stops_the_ranking(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error on the sample write left #2 failed with a key and no sample; the ranking left it out
    and passed on #0 and #1. A later record of its key resolves it."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "no sample")
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    record_trial(
        study, tmp_path / "catalog", [strategy(fast=2, slow=5)], starting_balances=BALANCES
    )
    real = optuna.trial.Trial.set_user_attr

    def locked(trial: optuna.trial.Trial, key: str, value: object) -> None:
        if key == "sample":
            raise OSError("database is locked")
        real(trial, key, value)

    monkeypatch.setattr(optuna.trial.Trial, "set_user_attr", locked)
    with pytest.raises(OSError, match="database is locked"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    monkeypatch.undo()

    failed = study.trials[2]
    assert "sample" not in failed.user_attrs and failed.state == TrialState.FAIL
    with pytest.raises(RankingStopped, match=r"no later record ran to the end after #2 \(failed\)"):
        rank(study)
    record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    assert rank(study).n == 2


def test_a_creation_interrupted_before_its_sample_is_completed_by_the_next(
    tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name was committed before its sample: open refused it as having no sample, and creation
    as a duplicate, so nothing short of editing the file could recover it."""
    real = optuna.Study.set_user_attr

    def interrupted(study: optuna.Study, key: str, value: object) -> None:
        if key == SAMPLES:
            raise KeyboardInterrupt
        real(study, key, value)

    monkeypatch.setattr(optuna.Study, "set_user_attr", interrupted)
    with pytest.raises(KeyboardInterrupt):
        new_study(stack, tmp_path, "half made")
    monkeypatch.undo()

    with pytest.raises(LedgerNotFound, match="create_ledger_study completes it"):
        stack.enter_context(open_study("half made", tmp_path / "trials.db"))
    study = new_study(stack, tmp_path, "half made")
    assert current_sample(study) == Sample(0, utc_ns(START), utc_ns(END))
    reopened = stack.enter_context(open_study("half made", tmp_path / "trials.db"))
    assert samples(reopened) == [current_sample(study)]
    with pytest.raises(DuplicatedStudyError):
        new_study(stack, tmp_path, "half made")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            "drop", "the engine's cache holds 39 bars and the run replayed 40", id="one short"
        ),
        pytest.param("outside", "the run replayed 1 bars outside sample 0", id="one outside"),
    ],
)
def test_bars_the_cache_does_not_account_for_are_told_failed(
    alpaca_bars: AlpacaBars,
    tmp_path: Path,
    stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    message: str,
) -> None:
    """The digest is only of the bars the run read if the cache holds every one, and only those."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "unaccounted")
    real = canon_trading.ledger.run_backtest_config
    stray = alpaca_bars.daily(
        alpaca_bars.equity(), [1.0], first=datetime(2024, 3, 11, tzinfo=NEW_YORK)
    )

    def changed(*args: Any) -> object:
        run = real(*args)
        bars = run.bars[1:] if change == "drop" else [*run.bars[1:], *stray]
        return dataclasses.replace(run, bars=bars)

    monkeypatch.setattr(canon_trading.ledger, "run_backtest_config", changed)
    with pytest.raises(RuntimeError, match=message):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME]) == (TrialState.FAIL, FAILED)
    assert "data_digest" not in trial.user_attrs


def test_a_hole_no_load_can_fill_is_closed_by_consolidating_the_catalog(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A weekend between two loads has no bar, so no load records it; upstream's consolidation does."""
    path = tmp_path / "catalog"
    alpaca_bars.write(path)  # covers to Sunday 2025-05-04 00:00 UTC
    append_bars(alpaca_bars, path)
    ParquetDataCatalog(str(path)).consolidate_data(NautilusDataType.Bar, str(BAR_TYPE))
    alpaca_bars.write(tmp_path / "holed")
    alpaca_bars.write(
        tmp_path / "holed",
        APPENDED,
        first=datetime(2025, 5, 5, tzinfo=NEW_YORK),
        previous=alpaca_bars.CLOSES[-1],
    )  # covers from Monday 2025-05-05 00:00 UTC: Sunday is a hole
    study = new_study(stack, tmp_path, "holed", end="2025-06-15")

    with pytest.raises(
        LookupError, match="missing 2025-05-04T00:00:00.000000001.*consolidate_data"
    ):
        record_trial(study, tmp_path / "holed", [strategy()], starting_balances=BALANCES)
    ParquetDataCatalog(str(tmp_path / "holed")).consolidate_data(
        NautilusDataType.Bar, str(BAR_TYPE)
    )
    holed = record_trial(study, tmp_path / "holed", [strategy()], starting_balances=BALANCES)
    whole = record_trial(study, path, [strategy()], starting_balances=BALANCES)

    first, second = study.trials
    assert first.user_attrs["data_digest"] == second.user_attrs["data_digest"]
    assert holed.run.result.iterations == whole.run.result.iterations == 70


# The third review of the gate, 2026-11-10: each test failed before its fix.


def test_two_sample_moves_at_once_are_both_kept(
    tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two handles on one study both returned sample 1, and only the last write survived."""
    new_study(stack, tmp_path, "moving")
    handles = [stack.enter_context(open_study("moving", tmp_path / "trials.db")) for _ in "ab"]
    real = canon_trading.ledger.samples

    def slow_first(study: optuna.Study) -> list[Sample]:
        found = real(study)
        if threading.current_thread().name == "first":
            time.sleep(0.5)  # between reading the samples and writing the new list
        return found

    monkeypatch.setattr(canon_trading.ledger, "samples", slow_first)
    moved: dict[str, Sample] = {}

    def move(handle: optuna.Study, end: str) -> None:
        moved[threading.current_thread().name] = move_sample(handle, start=START, end=end)

    first = threading.Thread(target=move, args=(handles[0], "2025-06-15"), name="first")
    second = threading.Thread(target=move, args=(handles[1], "2025-06-30"), name="second")
    first.start()
    time.sleep(0.1)
    second.start()
    first.join(10)
    second.join(10)
    monkeypatch.undo()

    assert sorted(s.number for s in moved.values()) == [1, 2]
    reopened = stack.enter_context(open_study("moving", tmp_path / "trials.db"))
    assert [s.number for s in samples(reopened)] == [0, 1, 2]
    assert set(samples(reopened)[1:]) == set(moved.values())


def test_a_sample_is_moved_only_on_a_study_the_ledger_opened(tmp_path: Path) -> None:
    """The move holds the ledger's lock, which only a study the ledger opened can name."""
    url = f"sqlite:///{tmp_path / 'bare.db'}"
    bare = optuna.create_study(study_name="bare", storage=url, direction="maximize")
    bare.set_user_attr(SAMPLES, [{"number": 0, "start": utc_ns(START), "end": utc_ns(END)}])

    with pytest.raises(ValueError, match="open_study"):
        move_sample(bare, start=START, end="2025-06-15")
    assert len(bare.user_attrs[SAMPLES]) == 1


def test_a_catalog_swapped_between_the_check_and_the_replay_is_told_failed(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coverage check read a 70-bar catalog, the node then read a 40-bar fragment swapped in,
    and two configurations ranked over the 70-day sample at T = 40 (DSR 0.99853 in the review)."""
    path = tmp_path / "catalog"
    alpaca_bars.write(path)
    append_bars(alpaca_bars, path)
    study = new_study(stack, tmp_path, "swapped", end="2025-06-15")
    real = canon_trading.ledger.run_backtest_config

    def swap_then_run(*args: Any) -> object:
        shutil.rmtree(path)
        alpaca_bars.write(path)  # the first 40 bars, covering 2025-03-10 to 2025-05-04
        return real(*args)

    monkeypatch.setattr(canon_trading.ledger, "run_backtest_config", swap_then_run)
    for config in (strategy(), strategy(fast=2, slow=5)):
        with pytest.raises(RuntimeError, match="other bars than the coverage check read"):
            record_trial(study, path, [config], starting_balances=BALANCES)
        shutil.rmtree(path)
        alpaca_bars.write(path)
        append_bars(alpaca_bars, path)
    monkeypatch.undo()

    assert [(t.state, t.user_attrs[OUTCOME]) for t in study.trials] == [
        (TrialState.FAIL, FAILED)
    ] * 2
    assert all("data_digest" not in t.user_attrs for t in study.trials)
    with pytest.raises(RankingStopped, match="no record ran to the end"):
        rank(study)
    for config in (strategy(), strategy(fast=2, slow=5)):
        record_trial(study, path, [config], starting_balances=BALANCES)
    assert rank(study).t == 70


def test_a_catalog_swapped_between_the_check_and_its_query_is_refused(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapped after the intervals were checked and before the bars were read, the fragment was both
    what the check's query and the node read, so the two digests agreed (code review)."""
    path = tmp_path / "catalog"
    alpaca_bars.write(path)
    append_bars(alpaca_bars, path)
    study = new_study(stack, tmp_path, "swapped early", end="2025-06-15")
    real = ParquetDataCatalog.query
    swapped = []

    def swap_then_query(self: ParquetDataCatalog, *args: Any, **kwargs: Any) -> object:
        if not swapped:
            swapped.append(True)
            shutil.rmtree(path)
            alpaca_bars.write(path)  # the first 40 bars only
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ParquetDataCatalog, "query", swap_then_query)
    with pytest.raises(LookupError, match="changed while it was checked"):
        record_trial(study, path, [strategy()], starting_balances=BALANCES)
    monkeypatch.undo()

    assert study.trials == []


# The fourth review of the gate, 2026-11-10: each test failed before its fix.


def test_two_marks_at_once_are_both_kept(
    tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two handles marking two keyless records both returned, and one mark was lost: each wrote
    back the `not_run` list it had read."""
    study = new_study(stack, tmp_path, "marking")
    numbers = [study.ask().number for _ in "ab"]
    handles = [stack.enter_context(open_study("marking", tmp_path / "trials.db")) for _ in "ab"]
    real = optuna.Study.set_user_attr

    def slow_first(self: optuna.Study, key: str, value: object) -> None:
        if key == NOT_RUN and threading.current_thread().name == "first":
            time.sleep(0.5)  # between reading the marks and writing the new list
        real(self, key, value)

    monkeypatch.setattr(optuna.Study, "set_user_attr", slow_first)
    errors: list[BaseException] = []

    def mark(handle: optuna.Study, number: int) -> None:
        try:
            mark_not_run(handle, number)
        except BaseException as error:  # noqa: BLE001, reported below
            errors.append(error)

    first = threading.Thread(target=mark, args=(handles[0], numbers[0]), name="first")
    second = threading.Thread(target=mark, args=(handles[1], numbers[1]), name="second")
    first.start()
    time.sleep(0.1)
    second.start()
    first.join(10)
    second.join(10)
    monkeypatch.undo()

    assert errors == []
    reopened = stack.enter_context(open_study("marking", tmp_path / "trials.db"))
    assert reopened.user_attrs[NOT_RUN] == numbers


def test_a_trial_is_recorded_in_its_own_trial_not_one_from_optuna_s_queue(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """`Study.ask` takes the oldest queued trial, numbered below records already made, where the
    ranking reads a higher number as a later record; the queued one is told FAIL and left keyless."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "queued")
    study.enqueue_trial({"fast": 5})

    recorded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    queued, own = study.trials
    assert (recorded.number, own.user_attrs[OUTCOME]) == (1, TRADED)
    assert (queued.state, queued.user_attrs) == (
        TrialState.FAIL,
        {"error": "taken from Optuna's queue by record_trial, which runs only in its own trial"},
    )
    mark_not_run(study, queued.number)
    assert study.user_attrs[NOT_RUN] == [queued.number]


def test_a_failure_whose_record_cannot_be_read_still_raises_the_run_s_error(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A storage error reading the trial back replaced the run's own error, with no note."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "unreadable")

    def engine_error(*_: object) -> None:
        raise RuntimeError("engine error")

    real = optuna.Study.get_trials
    calls: list[int] = []

    def locked_after_the_run(self: optuna.Study, *args: Any, **kwargs: Any) -> object:
        if calls:
            raise OSError("database is locked")
        return real(self, *args, **kwargs)

    def run_then_lock(*args: object) -> None:
        calls.append(1)
        engine_error()

    monkeypatch.setattr(canon_trading.ledger, "run_backtest_config", run_then_lock)
    monkeypatch.setattr(optuna.Study, "get_trials", locked_after_the_run)
    with pytest.raises(RuntimeError, match="engine error") as raised:
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    monkeypatch.undo()

    assert any("could not be told FAIL" in note for note in raised.value.__notes__)


class _Mode(enum.IntEnum):
    LIVE = 1


class _Venue(enum.StrEnum):
    LIVE = "live"


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(_Mode.LIVE, id="IntEnum"),
        pytest.param(_Venue.LIVE, id="StrEnum"),
        pytest.param({math.nan: 1}, id="NaN key"),
        pytest.param({math.inf: 1}, id="infinite key"),
        pytest.param({Decimal("Infinity"): 1}, id="infinite decimal key"),
        pytest.param(Decimal("NaN"), id="decimal NaN"),
        pytest.param(Decimal("sNaN"), id="signalling decimal NaN"),
    ],
)
def test_a_value_keyed_by_what_it_is_not_is_refused(extra: object) -> None:
    """An IntEnum or StrEnum was keyed by its value, against "enums by name", and a NaN dict key
    became the text "nan", past the canonical JSON's refusal of NaN."""
    config = strategy()
    setattr(config, "extra", extra)  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="not as MaCrossConfig builds it"):
        trial_config([config], starting_balances=BALANCES)
    # The key's own refusal still stands behind it, for a field the class may declare later.
    with pytest.raises(TypeError, match="no stable text form"):
        canon_trading.ledger._field("extra", vars(config)["extra"])


def test_a_waiting_trial_added_with_attributes_is_not_recorded_in(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A `WAITING` trial added through `Study.add_trial` carries no `fixed_params`, and its own
    attributes would have stayed on the record `record_trial` wrote into it."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "added")
    study.add_trial(optuna.trial.create_trial(state=TrialState.WAITING, user_attrs={OUTCOME: IDLE}))

    recorded = record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)

    added, own = study.trials
    assert recorded.number == own.number == 1
    assert added.state == TrialState.FAIL and "config_key" not in added.user_attrs


# The fourth review of the gate, second part, 2026-11-10: each test failed before its fix.

EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
# Every config key of the step 7 study (ADR 0048) and of this module's default configuration, as
# they were before the key took in base `StrategyConfig` fields: a default config's key must not move.
STEP_7_KEYS = (
    "5725676d4cedee320a72f9a10600074bf76fcb0ade7ab7fcc21d722771e8241d",
    "a437779d65601e16bb9ccfb46e9cfc2a9c26c49477063a31cdec742742c28297",
    "ace41039b4144caa9dec8308a98aa64d89aabd2af3a5812c0c8e8c10eee0e127",
    "c816426ebdc1e811981e5014ed3e4b9ba0d4fe12369aaad03d95a7e5bc845aaa",
    "aaa1fe2872e2b5b4295c5179ea412b3daeb422ade2a7297a6c6a455df1c07a54",
    "e4829d149e404bb99706d47411dd7ec61ad8040e0fa676169ef594a24b43ec18",
    "17d11aa5d362d137a4e61dc00c884edec1dc8210a1dd67189965dcc1e881845f",
    "6829078a18d0c165f9c49e4755459311d8a60f602970771118d5abbb143a9d42",
)
DEFAULT_CONFIG_SHA256 = "b78e0df8b2441340cab424695d300ab08a2fcd66a58ccb6185bbfdb48da99436"


def tagged(bar_type: BarType, tag: str) -> MaCrossConfig:
    """`strategy()`'s configuration on `bar_type` with `order_id_tag`, which two in one run need."""
    return MaCrossConfig(
        bar_type=bar_type,
        trade_size=Quantity.from_int(10),
        fast_period=3,
        slow_period=8,
        order_id_tag=tag,
    )


def test_a_bar_type_with_no_bar_in_the_sample_is_in_the_digest(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A bar type the catalog covers with no bar in the sample was left out of the digest, so a
    trial on 0 of its bars and one on 40 agreed; alone, its run wrote `data_digest = {}`."""
    alpaca_bars.write(tmp_path / "catalog")
    alpaca_bars.write(
        tmp_path / "catalog",
        symbols=("MSFT",),
        first=datetime(2025, 6, 2, tzinfo=NEW_YORK),
        covered_from=utc_ns(START),
    )
    study = new_study(stack, tmp_path, "empty bar type")

    recorded = record_trial(
        study,
        tmp_path / "catalog",
        [tagged(BAR_TYPE, "A"), tagged(MSFT_BAR_TYPE, "M")],
        starting_balances=BALANCES,
    )

    [trial] = study.trials
    assert recorded.outcome == TRADED
    assert set(trial.user_attrs["data_digest"]) == {
        str(BAR_TYPE),
        str(MSFT_BAR_TYPE),
        str(BAR_TYPE.instrument_id),
        str(MSFT_BAR_TYPE.instrument_id),
    }
    assert trial.user_attrs["data_digest"][str(MSFT_BAR_TYPE)] == EMPTY_DIGEST


def test_a_base_strategy_config_field_set_away_from_its_default_is_in_the_key() -> None:
    """`order_id_tag` reaches the engine and was left out of the key: two configurations shared one."""
    keys = [
        config_key(trial_config([tagged(BAR_TYPE, tag)], starting_balances=BALANCES))
        for tag in ("A", "B")
    ]

    assert keys[0] != keys[1]
    assert (
        config_key(trial_config([strategy()], starting_balances=BALANCES)) == DEFAULT_CONFIG_SHA256
    )


def test_the_keys_of_default_configs_are_as_they_were() -> None:
    """Taking in base fields set away from their defaults moves no key of a default config."""
    from canon_trading.variant_study import grid
    from canon_trading.variant_study import key_of

    assert tuple(key_of(config) for config in grid()) == STEP_7_KEYS
    assert (
        config_key(trial_config([strategy()], starting_balances=BALANCES)) == DEFAULT_CONFIG_SHA256
    )


def test_the_base_defaults_the_key_is_taken_against_are_the_pinned_ones() -> None:
    """The key leaves out a base field at its default at the pinned NautilusTrader. A pin that changes
    a default moves the key of a config that sets that field to either value: this fails on such a
    pin, and the move must say which keys change (ADR 0053 term 2)."""
    assert canon_trading.ledger.base_config_defaults() == {
        "external_order_instrument_ids": None,
        "log_commands": True,
        "log_events": True,
        "log_rejected_due_post_only_as_warning": True,
        "manage_contingent_orders": False,
        "manage_gtd_expiry": False,
        "manage_stop": False,
        "market_exit_interval_ms": 100,
        "market_exit_max_attempts": 100,
        "market_exit_reduce_only": True,
        "market_exit_time_in_force": "GTC",
        "oms_type": None,
        "order_id_tag": None,
        "strategy_id": None,
        "use_hyphens_in_client_order_ids": True,
        "use_uuid_client_order_ids": False,
    }


def test_a_field_with_no_stable_text_form_is_named() -> None:
    config = strategy()
    setattr(config, "extra", object())  # noqa: B010, a field the class does not declare

    with pytest.raises(TypeError, match="not as MaCrossConfig builds it"):
        trial_config([config], starting_balances=BALANCES)
    # The key's own refusal still stands behind it, for a field the class may declare later.
    with pytest.raises(TypeError, match="field 'extra'"):
        canon_trading.ledger._field("extra", vars(config)["extra"])


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(0, 10**20, id="end past 2**63"),
        pytest.param(-5, 10, id="negative start"),
        pytest.param("1600-01-01", "1600-01-02", id="before pandas' first instant"),
    ],
)
def test_a_sample_no_timestamp_holds_is_refused(
    tmp_path: Path, start: int | str, end: int | str
) -> None:
    """Such a sample was written, and every later read of it crashed in pandas."""
    with (
        pytest.raises(ValueError, match="nanoseconds"),
        create_ledger_study("s", start=start, end=end, path=tmp_path / "trials.db"),
    ):
        pass


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param([{"number": False, "start": 0, "end": 10}], id="bool number"),
        pytest.param([{"number": 0, "start": 500, "end": 100}], id="start after end"),
        pytest.param([{"number": 0, "start": True, "end": 100}], id="bool start"),
        pytest.param([{"number": 0, "start": 0, "end": 10**20}], id="end past 2**63"),
    ],
)
def test_a_stored_sample_not_in_the_writers_form_is_refused(
    tmp_path: Path, stack: ExitStack, stored: object
) -> None:
    study = new_study(stack, tmp_path, "odd sample")
    study.set_user_attr(SAMPLES, stored)

    with pytest.raises(LedgerNotFound, match="no sample in the form"):
        samples(study)


def restricted_msft(alpaca_bars: AlpacaBars, path: Path) -> None:
    """AAPL and MSFT bars as `alpaca_bars` writes them, with MSFT's `Equity` taking at most 5 shares an
    order: the bars are the same, and only the instrument definition the node loads differs."""
    alpaca_bars.write(path, symbols=("AAPL",))
    alpaca_bars.write(path, symbols=("MSFT",), instrument=False)
    listed = alpaca_bars.equity("MSFT")
    ParquetDataCatalog(str(path)).write_instruments(
        [
            Equity(
                instrument_id=listed.id,
                raw_symbol=listed.raw_symbol,
                currency=listed.quote_currency,
                price_precision=listed.price_precision,
                price_increment=listed.price_increment,
                ts_event=0,
                ts_init=0,
                max_quantity=Quantity.from_int(5),
                info=dict(listed.info),
            )
        ]
    )


def pair(fast: int, slow: int) -> list[MaCrossConfig]:
    return [
        MaCrossConfig(
            bar_type=bar_type,
            trade_size=Quantity.from_int(10),
            fast_period=fast,
            slow_period=slow,
            order_id_tag=tag,
        )
        for bar_type, tag in ((BAR_TYPE, "A"), (MSFT_BAR_TYPE, "M"))
    ]


def test_trials_on_two_definitions_of_one_instrument_stop(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A second reader: the data identity held the bars only, so trials that ran on one catalog's bars
    with two definitions of MSFT (a 5-share order limit in one) agreed on their digests, and three keys
    recorded across the two ranked and passed at DSR 0.9916, though the limit changed what one run
    traded and so its returns."""
    alpaca_bars.write(tmp_path / "plain", symbols=("AAPL", "MSFT"))
    restricted_msft(alpaca_bars, tmp_path / "restricted")
    study = new_study(stack, tmp_path, "two definitions")

    first = record_trial(study, tmp_path / "plain", pair(3, 8), starting_balances=BALANCES)
    again = record_trial(study, tmp_path / "restricted", pair(3, 8), starting_balances=BALANCES)
    trials = {t.number: t for t in study.trials}
    # The same configuration on the same bars traded other returns under the other definition.
    assert (first.outcome, again.outcome) == (TRADED, TRADED)
    assert (
        trials[first.number].user_attrs["returns_series"]
        != trials[again.number].user_attrs["returns_series"]
    )

    other = new_study(stack, tmp_path, "across keys")
    record_trial(other, tmp_path / "plain", pair(3, 8), starting_balances=BALANCES)
    record_trial(other, tmp_path / "restricted", pair(4, 9), starting_balances=BALANCES)
    record_trial(other, tmp_path / "restricted", pair(2, 7), starting_balances=BALANCES)
    with pytest.raises(
        RankingStopped,
        match=r"MSFT\.ALPACA: records on sample 0 [^\n]*read different definitions of the instr",
    ):
        rank(other)


def test_an_instrument_s_digest_is_its_definition_not_when_it_was_stamped(
    alpaca_bars: AlpacaBars,
) -> None:
    listed = alpaca_bars.equity()
    stamped_later = parse_equity(Asset.model_validate(alpaca_bars.ASSET), 1_000)
    limited = Equity(
        instrument_id=listed.id,
        raw_symbol=listed.raw_symbol,
        currency=listed.quote_currency,
        price_precision=listed.price_precision,
        price_increment=listed.price_increment,
        ts_event=0,
        ts_init=0,
        max_quantity=Quantity.from_int(5),
        info=dict(listed.info),
    )

    assert stamped_later.ts_init != listed.ts_init
    assert instrument_digest(stamped_later) == instrument_digest(listed)
    assert instrument_digest(limited) != instrument_digest(listed)


def test_a_catalog_with_two_definitions_of_one_instrument_does_not_run(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """The cache's definition is the one the run traded against only if a run reads one: the node
    refuses a catalog that holds two definitions of an instrument, so no run read another first."""
    alpaca_bars.write(tmp_path / "catalog", instrument=False)
    listed = alpaca_bars.equity()
    limited = Equity(
        instrument_id=listed.id,
        raw_symbol=listed.raw_symbol,
        currency=listed.quote_currency,
        price_precision=listed.price_precision,
        price_increment=listed.price_increment,
        ts_event=1,
        ts_init=1,
        max_quantity=Quantity.from_int(5),
        info=dict(listed.info),
    )
    ParquetDataCatalog(str(tmp_path / "catalog")).write_instruments([listed])
    ParquetDataCatalog(str(tmp_path / "catalog")).write_instruments([limited])
    study = new_study(stack, tmp_path, "two versions")

    with pytest.raises(RuntimeError, match="already registered with a different definition"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    [trial] = study.trials
    assert (trial.state, trial.user_attrs[OUTCOME]) == (TrialState.FAIL, FAILED)


@pytest.mark.parametrize(
    ("given", "named"),
    [
        pytest.param({"fast": 3, "slow": 12}, "'fast', 'slow'", id="a mistyped field"),
        pytest.param({"order_tag": "01"}, "'order_tag'", id="a mistyped base field"),
    ],
)
def test_a_keyword_ma_cross_config_does_not_take_is_refused(
    given: dict[str, Any], named: str
) -> None:
    """A third reader of the gate: `MaCrossConfig` took any keyword and ran its defaults, so two
    configs with a mistyped field were one run under one key; only the base `StrategyConfig` fields
    pass through."""
    with pytest.raises(
        TypeError, match=f"MaCrossConfig got an unexpected keyword argument {named}"
    ):
        MaCrossConfig(bar_type=BAR_TYPE, trade_size=Quantity.from_int(10), **given)
    tagged_config = MaCrossConfig(
        bar_type=BAR_TYPE, trade_size=Quantity.from_int(10), order_id_tag="A"
    )
    assert tagged_config.order_id_tag == "A"


@pytest.mark.parametrize(
    "given",
    [
        pytest.param({"fast_period": True}, id="bool period"),
        pytest.param({"fast_period": 3.0}, id="float period"),
        pytest.param({"slow_period": "20"}, id="text period"),
        pytest.param({"trade_size": Decimal(10)}, id="decimal size"),
    ],
)
def test_a_field_of_another_type_is_refused(given: dict[str, object]) -> None:
    """A third reader of the gate: values of another type keyed apart (`3.0` and `3`) or together
    (`Decimal("10")` and `Quantity(10)` print alike) though they run alike or not at all; each field
    takes one type."""
    fields: dict[str, Any] = {"bar_type": BAR_TYPE, "trade_size": Quantity.from_int(10), **given}
    with pytest.raises(TypeError, match=f"MaCrossConfig's {next(iter(given))} is"):
        MaCrossConfig(**fields)


def test_a_config_whose_fields_were_changed_after_it_was_built_is_refused() -> None:
    """A third reader of the gate: a value in `vars(config)` under a base field's name was keyed
    though the engine reads the base field's own getter; so would a field added after construction."""
    for change in ({"order_id_tag": "01"}, {"extra": 1}, {"fast_period": "3"}):
        config = strategy()
        vars(config).update(change)
        with pytest.raises(TypeError, match="not as MaCrossConfig builds it"):
            trial_config([config], starting_balances=BALANCES)
    for change in ({"fast_period": 30, "slow_period": 20}, {"trade_size": Quantity.from_int(0)}):
        config = strategy()
        vars(config).update(change)
        with pytest.raises(ValueError, match="not as MaCrossConfig builds it"):
            trial_config([config], starting_balances=BALANCES)


# The gate's remaining claims, 2026-11-10: each test failed before its fix unless it says otherwise.


def test_a_sample_starting_at_a_bar_at_utc_midnight_is_refused_before_a_trial(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A third reader: a sample starting at 00:00 UTC on a bar's `ts_init` put that bar on the day
    NautilusTrader's daily series books the account's registration on, which holds no return, so
    `record_trial` told its own run `FAIL` and the ranking stopped for good."""
    alpaca_bars.write(tmp_path / "catalog", first=datetime(2025, 3, 10, tzinfo=UTC))
    study = new_study(stack, tmp_path, "midnight start", start="2025-03-11", end="2025-05-03")

    with pytest.raises(ValueError, match="books that bar on the day before"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    assert study.trials == []


def test_a_sample_ending_on_a_day_with_no_bar_is_refused_before_a_trial(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A third reader: on bars too thin for the strategy's closing order, a sample ending two days
    after its last bar had `on_stop`'s fill booked on a day with no bar, and `record_trial` told its
    own run `FAIL` (traded, no value), which no later record resolves."""
    from nautilus_trader.model import Bar

    listed = alpaca_bars.equity()
    thin = [
        Bar(b.bar_type, b.open, b.high, b.low, b.close, Quantity.from_int(4), b.ts_event, b.ts_init)
        for b in alpaca_bars.daily(listed)
    ]
    (tmp_path / "catalog").mkdir()
    catalog = ParquetDataCatalog(str(tmp_path / "catalog"))
    catalog.write_instruments([listed])
    last = thin[-1].ts_init // DAY_NS * DAY_NS
    catalog.write_bars(
        thin, start=(thin[0].ts_init - DAY_NS) // DAY_NS * DAY_NS, end=last + 3 * DAY_NS
    )
    study = stack.enter_context(
        create_ledger_study(
            "late end",
            start=START,
            end=last + 2 * DAY_NS + DAY_NS // 2,
            path=tmp_path / "trials.db",
        )
    )

    with pytest.raises(ValueError, match="books a run's last fills on the session day of its end"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)
    assert study.trials == []


def test_an_instrument_s_digest_holds_the_precision_of_its_currency(
    alpaca_bars: AlpacaBars,
) -> None:
    """A second reader and a third: `to_dict` writes a currency as its code, so USD at precisions 2,
    0 and 8 gave one digest though the engine rounds money to the currency's precision, and three
    keys recorded across them passed at DSR 0.9922."""
    from nautilus_trader.model import Currency

    listed = alpaca_bars.equity()
    usd = listed.quote_currency
    digests = set()
    for precision in (2, 0, 8):
        currency = Currency(usd.code, precision, usd.iso4217, usd.name, usd.currency_type)
        held = Equity(
            instrument_id=listed.id,
            raw_symbol=listed.raw_symbol,
            currency=currency,
            price_precision=listed.price_precision,
            price_increment=listed.price_increment,
            ts_event=0,
            ts_init=0,
            info=dict(listed.info),
        )
        assert held.to_dict() == listed.to_dict()  # the form upstream compares names USD alone
        digests.add(instrument_digest(held))
    assert len(digests) == 3


@pytest.mark.parametrize(
    ("field", "values"),
    [
        pytest.param(
            "max_quantity", (Quantity.from_str("5"), Quantity.from_str("5.0")), id="5, 5.0"
        ),
        pytest.param("margin_init", (Decimal("0.1"), Decimal("0.10")), id="0.1, 0.10"),
    ],
)
def test_two_texts_of_one_value_are_two_definitions_to_the_node_and_the_digest(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack, field: str, values: tuple[Any, Any]
) -> None:
    """Two readers: values equal as numbers but written with another precision gave two digests.
    That is upstream's own rule, not a defect (passes before and after): the node refuses a second
    definition whose serialized form differs, a value's text included, and the digest follows it."""
    alpaca_bars.write(tmp_path / "catalog", instrument=False)
    listed = alpaca_bars.equity()
    defined = [
        Equity(
            instrument_id=listed.id,
            raw_symbol=listed.raw_symbol,
            currency=listed.quote_currency,
            price_precision=listed.price_precision,
            price_increment=listed.price_increment,
            ts_event=stamp,
            ts_init=stamp,
            info=dict(listed.info),
            **{field: value},
        )
        for stamp, value in enumerate(values)
    ]
    assert getattr(defined[0], field) == getattr(defined[1], field)
    assert instrument_digest(defined[0]) != instrument_digest(defined[1])
    for definition in defined:
        ParquetDataCatalog(str(tmp_path / "catalog")).write_instruments([definition])
    study = new_study(stack, tmp_path, "two texts")

    with pytest.raises(RuntimeError, match="already registered with a different definition"):
        record_trial(study, tmp_path / "catalog", [strategy()], starting_balances=BALANCES)


def test_the_digest_holds_each_instrument_the_run_names(alpaca_bars: AlpacaBars) -> None:
    """A second reader: `test_data_digest_names_every_bar_type_the_run_loads` passed no instrument,
    so it held whatever `data_digest` did with one (it passed with the instruments left out)."""
    msft = alpaca_bars.equity("MSFT")
    assert data_digest([], [str(MSFT_BAR_TYPE)], [msft]) == {
        str(MSFT_BAR_TYPE): EMPTY_DIGEST,
        str(msft.id): instrument_digest(msft),
    }


def test_starting_balances_are_keyed_one_per_currency_in_any_order() -> None:
    """A third reader: the balances were keyed in the order given and with repeats, though the venue
    keeps one per currency, so runs alike took two keys and N grew."""
    one = config_key(trial_config([strategy()], starting_balances=["100000 USD", "50000 EUR"]))
    other = config_key(trial_config([strategy()], starting_balances=["50000 EUR", "100000 USD"]))
    assert one == other
    with pytest.raises(ValueError, match="name USD more than once"):
        trial_config([strategy()], starting_balances=["50000 USD", "50000 USD"])
    assert (
        config_key(trial_config([strategy()], starting_balances=BALANCES)) == DEFAULT_CONFIG_SHA256
    )


def test_a_queued_trial_with_a_key_that_record_trial_takes_stops_the_ranking(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A third reader: `record_trial` took another writer's queued record of a key, outcome `traded`
    and returns made up, and told it `FAIL`, which reads as a run that ran to the end; the ranking
    then passed on returns no backtest gave. Taken with its note, it now stops the ranking for good."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "queued key")
    key = config_key(trial_config([strategy()], starting_balances=BALANCES))
    study.add_trial(
        optuna.trial.create_trial(
            state=TrialState.WAITING, user_attrs={"config_key": key, "sample": 0, OUTCOME: TRADED}
        )
    )

    for fast, slow in ((3, 8), (2, 5)):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(fast=fast, slow=slow)],
            starting_balances=BALANCES,
        )

    taken = study.trials[0]
    assert (taken.state, taken.user_attrs["error"]) == (
        TrialState.FAIL,
        canon_trading.ledger.TAKEN_FROM_QUEUE,
    )
    with pytest.raises(RankingStopped, match=r"#0 \(traded\) was taken from Optuna's queue"):
        rank(study)


def test_a_queued_record_marked_while_waiting_stays_cleared_once_record_trial_takes_it(
    alpaca_bars: AlpacaBars, tmp_path: Path, stack: ExitStack
) -> None:
    """A mark made while `WAITING` holds only while the record is not started, and `record_trial`'s
    own `ask` takes the oldest queued trial: told `FAIL` with its note and no run, it stays cleared,
    as it was before the mark was kept apart (the case that change must keep)."""
    alpaca_bars.write(tmp_path / "catalog")
    study = new_study(stack, tmp_path, "marked queue")
    study.enqueue_trial({})
    mark_not_run(study, 0)
    for fast, slow in ((3, 8), (2, 5)):
        record_trial(
            study,
            tmp_path / "catalog",
            [strategy(fast=fast, slow=slow)],
            starting_balances=BALANCES,
        )

    taken = study.trials[0]
    assert (taken.state, taken.user_attrs) == (
        TrialState.FAIL,
        {"error": canon_trading.ledger.TAKEN_FROM_QUEUE},
    )
    mark_not_run(study, 0)  # cleared already: nothing to refuse
    assert rank(study).n == 2
