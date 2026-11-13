"""Offline tuning under ADR 0101's proposed bars record: folds, full rows and a closed holdout."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import optuna
import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from conftest import AlpacaBars

NY = ZoneInfo("America/New_York")
MINUTE = 60_000_000_000


def dates(count: int, *, first: date = date(2025, 3, 10)) -> tuple[date, ...]:
    return tuple(first + timedelta(days=i) for i in range(count))


SESSIONS = dates(10)
HELD = tuple(SESSIONS[7:])
"""A plan's sessions and the held 30% `split_dates` gives them."""


def catalog_data(
    path: Path,
    alpaca_bars: AlpacaBars,
    *,
    part: str = "regular",
    fall: bool = False,
    first: date = date(2025, 2, 18),
    daily: bool = True,
) -> tuple[date, ...]:
    """Twenty baseline sessions, then ten differing mover sessions, on generated regular bars."""
    path.mkdir()
    (path / "alpaca-feed").write_text("boats\n" if part == "overnight" else "sip\n")
    catalog = ParquetDataCatalog(str(path))
    instrument = alpaca_bars.equity()
    catalog.write_instruments([instrument])
    days = dates(30, first=first)
    minute_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    day_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
    minute_bars, daily_bars = [], []
    for n, day in enumerate(days):
        hour, minute = {
            "pre-market": (4, 0),
            "regular": (9, 30),
            "after-hours": (16, 0),
            "overnight": (20, 0),
        }[part]
        start = (
            int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).timestamp()) * 10**9
        )
        gain = 0.1 + (n % 7) * 0.03
        closes = [10 + gain * i for i in range(8)] if n >= 20 else [10.0] * 8
        if fall and n >= 20:
            closes[5:] = [10 + gain, 10 + gain * 4, 9.8]
        for i, close in enumerate(closes):
            previous = closes[max(i - 1, 0)]
            minute_bars.append(
                Bar(
                    minute_type,
                    Price(previous, 4),
                    Price(max(previous, close), 4),
                    Price(min(previous, close), 4),
                    Price(close, 4),
                    Quantity.from_int(100_000 if n >= 20 else 100),
                    start + i * MINUTE,
                    start + (i + 1) * MINUTE,
                )
            )
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=NY).timestamp()) * 10**9
        daily_bars.append(
            Bar(
                day_type,
                Price(10, 4),
                Price(10, 4),
                Price(10, 4),
                Price(10, 4),
                Quantity.from_int(100_000),
                midnight,
                midnight + 86_400 * 10**9,
            )
        )
    catalog.write_bars(minute_bars, start=minute_bars[0].ts_event, end=minute_bars[-1].ts_init)
    if daily:
        catalog.write_bars(daily_bars, start=daily_bars[0].ts_event, end=daily_bars[-1].ts_init)
    return tuple(day + timedelta(days=1) if part == "overnight" else day for day in days[20:])


def test_walk_forward_never_sees_later_sessions() -> None:
    from canon_trading.tuning_study import walk_forward_folds

    sessions = dates(20)
    folds, holdout = walk_forward_folds(
        sessions,
    )
    assert holdout == sessions[14:]
    assert folds
    for fold in folds:
        assert max(fold.train) < min(fold.test)
        assert set(fold.train + fold.test).isdisjoint(holdout)
        assert all(day < min(holdout) for day in fold.train + fold.test)
    assert len(folds[-1].train) > len(folds[0].train)


def test_holdout_requires_every_registered_arm_and_opens_once(tmp_path: Path) -> None:
    from canon_trading.tuning_study import REGISTERED_ARMS
    from canon_trading.tuning_study import open_holdout

    marker = tmp_path / "registered.json"
    for arms in (None, list(REGISTERED_ARMS)[:-1]):
        if arms is not None:
            marker.write_text(json.dumps({"arms": arms, "plan": "abc"}))
        with pytest.raises(ValueError, match="registration|every arm"):
            open_holdout(marker, plan_digest="abc", sessions=SESSIONS)
        assert not marker.with_suffix(".opened").exists()
    marker.write_text(
        json.dumps(
            {
                "arms": list(REGISTERED_ARMS),
                "plan": "abc",
                "holdout_dates": [day.isoformat() for day in HELD],
            }
        )
    )
    with pytest.raises(ValueError, match="plan"):
        open_holdout(marker, plan_digest="other", sessions=SESSIONS)
    assert open_holdout(marker, plan_digest="abc", sessions=SESSIONS, replay=lambda: None).is_dir()
    with pytest.raises(PermissionError):
        open_holdout(marker, plan_digest="abc", sessions=SESSIONS, replay=lambda: None)


def test_optuna_seed_reproduces_real_suggestions() -> None:
    from canon_trading.tuning_study import sample_arm

    def sampled(seed: int) -> list:
        study = optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=seed))
        results = []
        for _ in range(4):
            trial = study.ask()
            rows, settings = sample_arm(
                trial, "bars-registered", buying_power=Decimal(1000), slots=2
            )
            assert trial.params and trial.distributions
            results.append((rows, settings.run_bars, settings.trail, dict(trial.params)))
            study.tell(trial, 0.0)
        return results

    assert sampled(17) == sampled(17)
    assert sampled(17) != sampled(18)


def test_parts_with_no_catalog_data_are_named_and_holdout_is_not_read(tmp_path: Path) -> None:
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    plan = StudyPlan(
        name="empty",
        catalog=tmp_path / "catalog",
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=dates(10),
        buying_power=Decimal(1000),
        slots=1,
        trials_per_arm=1,
    )
    report = run_study(plan)
    assert set(report.no_data) == {"pre-market", "regular", "after-hours", "overnight"}
    assert "boats" in report.no_data["overnight"]
    for window in (1, 3, 5):
        assert f"rolling-bar-vwap-count-{window}m" in report.waiting
        assert f"recorder-sum-{window}m" in report.waiting
    assert "print-level" in report.waiting
    assert not plan.ledger.exists()
    assert not plan.catalog.exists()
    assert "holdout: CLOSED" in report.summary()


def test_real_engine_trials_are_ranked_by_part_without_reading_holdout(
    tmp_path: Path, alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canon_trading.ledger import open_study
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    sessions = catalog_data(tmp_path / "catalog", alpaca_bars)
    plan = StudyPlan(
        name="engine",
        catalog=tmp_path / "catalog",
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=2,
        arms=("bars-run-1",),
        trials_per_arm=2,
    )
    cutoff = int(datetime.combine(sessions[7], datetime.min.time(), NY).timestamp()) * 10**9
    original = ParquetDataCatalog.query
    reads = []

    def query(self, *args, **kwargs):
        reads.append(kwargs["end"])
        assert kwargs["end"] < cutoff
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ParquetDataCatalog, "query", query)
    report = run_study(plan)
    assert reads
    assert len(report.folds) == 6
    fold = report.folds[-1]
    assert fold.part == "regular"
    assert len(fold.training_keys) == 2
    assert "DSR" in fold.training_summary
    assert (
        "| Arm and settings | Config key | Trial | Sharpe | DSR of pick |" in fold.training_summary
    )
    with open_study(fold.training_study, plan.ledger) as study:
        assert len(study.trials) == 2
        for trial in study.trials:
            assert trial.user_attrs["outcome"] == "traded"
            config = trial.user_attrs["config"]
            replay = config["strategies"][0]["config"]["replay"]
            assert replay["sizing"] == "buying_power / slots"
            assert replay["buying_power"] == "1000"
            assert replay["execution"] == "bars-native-trailing-close-entry-v1"
            assert "UNCALIBRATED" in config["venue"]["fee_model"]["calibration"]
            assert "digest" not in config["data"]
            assert trial.user_attrs["mover_sample"]["history_digest"]


def test_rolling_flow_requires_vwap_and_counts_without_close_substitution() -> None:
    from canon_trading.tuning_study import BarMetrics
    from canon_trading.tuning_study import rolling_flow

    bar_type = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
    bars = [
        Bar(
            bar_type,
            Price(10, 4),
            Price(10, 4),
            Price(10, 4),
            Price(10, 4),
            Quantity.from_int(100),
            i * MINUTE,
            (i + 1) * MINUTE,
        )
        for i in (0, 2, 3)
    ]
    with pytest.raises(LookupError, match="VWAP|trade_count"):
        rolling_flow(bars, {}, window_minutes=3)
    metrics = {bar.ts_init: BarMetrics(vwap=Decimal(9), trade_count=7) for bar in bars}
    assert rolling_flow(bars, metrics, window_minutes=3) == (Decimal(1800), 14)


@pytest.mark.parametrize("part", ["pre-market", "after-hours", "overnight"])
def test_each_session_part_replays_its_own_feed(
    tmp_path: Path, alpaca_bars: AlpacaBars, part: str
) -> None:
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars, part=part)
    plan = StudyPlan(
        name=part,
        catalog=tmp_path / "absent" if part == "overnight" else root,
        boats_catalog=root if part == "overnight" else None,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=1,
        arms=("bars-run-1",),
        trials_per_arm=1,
    )
    report = run_study(plan)
    assert report.folds
    assert {fold.part for fold in report.folds} == {part}
    assert part not in report.no_data


def test_after_hours_in_winter_is_not_quiet_for_lack_of_a_print_after_19_00(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    """code-review-high-n2b/after-hours-est: in EST, 20:00 New York is 01:00 UTC the next day, so a
    sample ending at the calendar bound needed a print from 19:00 on each slice's last day, and one
    quiet hour skipped the whole part. The sample ends at its last bar instead."""
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars, part="after-hours", first=date(2025, 1, 6))
    plan = StudyPlan(
        name="winter",
        catalog=root,
        boats_catalog=None,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=1,
        arms=("bars-run-1",),
        trials_per_arm=1,
    )
    report = run_study(plan)
    assert "after-hours" not in report.quiet and "after-hours" not in report.no_data
    assert {fold.part for fold in report.folds} == {"after-hours"}


def test_a_catalog_without_day_bars_is_named_no_data_not_a_failed_ranking(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    """code-review-high-n2b/day-bars: without 1-DAY bars no name has a reference close, so every
    trial was idle and the part read 'DSR unavailable'; it is named under no_data instead."""
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars, daily=False)
    plan = StudyPlan(
        name="no-days",
        catalog=root,
        boats_catalog=None,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=1,
        arms=("bars-run-1",),
        trials_per_arm=1,
    )
    report = run_study(plan)
    assert "1-DAY" in report.no_data["regular"]
    assert not report.folds


def test_recording_a_trial_does_not_deep_copy_the_whole_study(
    tmp_path: Path, alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code-review-high-n2b/deepcopy: reading one recorded value through `study.trials` deep-copied
    every trial once per trial; deep copies must not grow with the trials per arm."""
    import optuna

    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    original = optuna.Study.get_trials
    copies: list[int] = []

    def counting(self, deepcopy=True, states=None):
        copies.append(int(deepcopy))
        return original(self, deepcopy=deepcopy, states=states)

    monkeypatch.setattr(optuna.Study, "get_trials", counting)
    counts = []
    for trials in (1, 3):
        copies.clear()
        run_study(
            StudyPlan(
                name=f"copies-{trials}",
                catalog=root,
                boats_catalog=None,
                ledger=tmp_path / f"trials-{trials}.db",
                symbols=("AAPL",),
                sessions=sessions,
                buying_power=Decimal(1000),
                slots=1,
                arms=("bars-run-1",),
                trials_per_arm=trials,
            )
        )
        counts.append(sum(copies))
    assert counts[0] == counts[1]


def test_bar_protection_can_fill_a_native_trailing_order(
    tmp_path: Path, alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    import canon_trading.tuning_study as tuning

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars, fall=True)
    original = tuning.record_trial
    orders = []

    def record(*args, **kwargs):
        recorded = original(*args, **kwargs)
        orders.extend(recorded.run.orders)
        return recorded

    monkeypatch.setattr(tuning, "record_trial", record)
    tuning.run_study(
        tuning.StudyPlan(
            name="native-stop",
            catalog=root,
            ledger=tmp_path / "trials.db",
            symbols=("AAPL",),
            sessions=sessions,
            buying_power=Decimal(1000),
            slots=1,
            arms=("bars-run-1",),
            trials_per_arm=2,
        )
    )
    assert any(
        order.order_type.name == "TRAILING_STOP_LIMIT" and order.filled_qty.is_positive()
        for order in orders
    ), sorted(
        {
            (order.status.name, str(order.events()[-1]))
            for order in orders
            if order.order_type.name == "TRAILING_STOP_LIMIT"
        }
    )


def test_candidates_feed_n3s_json_safe_mover_settings_schema(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    from canon_trading.ledger import open_study
    from canon_trading.ranking import RankingStopped
    from canon_trading.ranking import rank
    from canon_trading.tuning_contract import RankedConfiguration
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    plan = StudyPlan(
        name="n3-schema",
        catalog=root,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=2,
        arms=("bars-run-1",),
        trials_per_arm=2,
    )
    emitted = json.loads(json.dumps(run_study(plan).to_dict(), allow_nan=False))
    assert emitted["holdout"] == "CLOSED" and len(emitted["folds"]) == 6
    for fold in emitted["folds"]:
        for stage in ("training", "validation"):
            bundle = fold[stage]
            candidates = bundle["candidates_by_trial_key"]
            assert bundle["part"] == "regular" and len(candidates) == 2
            with open_study(bundle["study"], plan.ledger) as study:
                by_key = {
                    trial.user_attrs["config_key"]: trial.user_attrs["config"]
                    for trial in study.trials
                }
                assert set(candidates) == set(by_key)
                for key, payload in candidates.items():
                    promoted = RankedConfiguration.decode(payload, expected_key=key)
                    assert promoted.config == by_key[key]
                    assert (
                        promoted.rows.list_ratio
                        == by_key[key]["strategies"][0]["config"]["replay"]["rows"]["list_ratio"]
                    )
                    assert (
                        str(promoted.mover_config.max_move)
                        == by_key[key]["strategies"][0]["config"]["max_move"]
                    )
                try:
                    ranked = rank(study, plan=list(candidates))
                except RankingStopped:
                    assert bundle["ranking"] is None and bundle["ranking_stopped"]
                else:
                    assert bundle["ranking"]["pick"] == ranked.pick
                    assert bundle["ranking"]["dsr"] == ranked.dsr
                    assert bundle["ranking"]["passed"] is ranked.passed


def tape_bar(
    symbol: str,
    day: date,
    minute: int,
    close: float,
    *,
    opened: float | None = None,
    low: float | None = None,
    high: float | None = None,
    volume: int = 100_000,
) -> Bar:
    start = (
        int(
            (datetime.combine(day, datetime.min.time(), NY) + timedelta(minutes=minute)).timestamp()
        )
        * 10**9
    )
    opened = close if opened is None else opened
    return Bar(
        BarType.from_str(f"{symbol}.ALPACA-1-MINUTE-LAST-EXTERNAL"),
        Price(opened, 4),
        Price(max(opened, close) if high is None else high, 4),
        Price(min(opened, close) if low is None else low, 4),
        Price(close, 4),
        Quantity.from_int(volume),
        start,
        start + MINUTE,
    )


def replay_inputs(
    alpaca_bars: AlpacaBars,
    bars: list[Bar],
    *,
    run: int = 1,
    protect: bool = False,
    rows=None,
    reference: float = 10.0,
):
    from canon_trading.early_entry_study import REGISTERED
    from canon_trading.ledger import MoverTrialConfig
    from canon_trading.mover import MoverConfig
    from canon_trading.tuning_study import ReplayData

    symbols = tuple(sorted({bar.bar_type.instrument_id.symbol.value for bar in bars}))
    day = datetime.fromtimestamp(bars[0].ts_event / 10**9, NY).date()
    history = list(bars)
    for symbol in symbols:
        prior = day - timedelta(days=1)
        history.append(tape_bar(symbol, prior, 570, reference, volume=100))
        midnight = int(datetime.combine(prior, datetime.min.time(), NY).timestamp()) * 10**9
        price = Price(reference, 4)
        history.append(
            Bar(
                BarType.from_str(f"{symbol}.ALPACA-1-DAY-LAST-EXTERNAL"),
                price,
                price,
                price,
                price,
                Quantity.from_int(100),
                midnight,
                midnight + 86_400 * 10**9,
            )
        )
    config = MoverTrialConfig(
        settings=MoverConfig(
            max_notional=Decimal(1000),
            max_move=Decimal(1000),
            run_bars=run,
            protect=protect,
            entry_arm="bridge",
        ),
        symbols=symbols,
        part="regular",
        sessions=(day,),
        history_start=day - timedelta(days=30),
        buying_power=Decimal(1000),
        arm="bars-registered",
        rows=rows
        or replace(REGISTERED, list_move=0.001, list_ratio=0.1, baseline_sessions=1, run_bars=run),
    )
    data = ReplayData(
        tuple(bars),
        tuple(sorted(history, key=lambda bar: bar.ts_init)),
        tuple(alpaca_bars.equity(symbol) for symbol in symbols),
        tuple(f"{symbol}.ALPACA-1-MINUTE-LAST-EXTERNAL" for symbol in symbols),
    )
    return config, data


def replay_tape(alpaca_bars: AlpacaBars, bars: list[Bar], **kwargs):
    from nautilus_trader.config import LoggerConfig

    from canon_trading.ledger import Sample
    from canon_trading.tuning_study import bounds
    from canon_trading.tuning_study import built_mover_replay

    cfg, data = replay_inputs(alpaca_bars, bars, **kwargs)
    sample = Sample(0, *bounds(cfg.sessions[0], "regular"))
    with built_mover_replay(
        cfg, data, sample, "review-regression", ["100000 USD"], LoggerConfig(bypass_logging=True)
    ) as run:
        return run()


def test_agy_1_last_print_does_not_predict_the_session_close(alpaca_bars: AlpacaBars) -> None:
    from canon_trading.tuning_study import bounds

    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 570 + i, 10 + i * 0.1) for i in range(4)]
    result = replay_tape(alpaca_bars, bars)
    sells = [
        order
        for order in result.orders
        if order.side.name == "SELL" and order.filled_qty.is_positive()
    ]
    assert sells
    assert (
        min(
            event.ts_event
            for order in sells
            for event in order.events()
            if type(event).__name__ == "OrderFilled"
        )
        >= bounds(day, "regular")[1]
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_agy_2_shared_slots_choose_the_best_name_after_all_bars(
    alpaca_bars: AlpacaBars, reverse: bool
) -> None:
    day = date(2025, 3, 10)
    bars = []
    for i in range(4):
        pair = [
            tape_bar("AAPL", day, 570 + i, 10.5 + i * 0.1),
            tape_bar("MSFT", day, 570 + i, 11.2 + i * 0.1),
        ]
        bars.extend(reversed(pair) if reverse else pair)
    result = replay_tape(alpaca_bars, bars, run=2)
    buys = [
        order
        for order in result.orders
        if order.side.name == "BUY" and order.filled_qty.is_positive()
    ]
    assert buys and {str(order.instrument_id) for order in buys} == {"MSFT.ALPACA"}


def test_agy_3_stop_is_submitted_in_the_entry_timestamp(alpaca_bars: AlpacaBars) -> None:
    day = date(2025, 3, 10)
    bars = [
        tape_bar("AAPL", day, 570, 10.1),
        tape_bar("AAPL", day, 571, 10.2),
        tape_bar("AAPL", day, 572, 7.5, opened=10.2, low=7),
        tape_bar("AAPL", day, 573, 10.1),
    ]
    result = replay_tape(alpaca_bars, bars, protect=True)
    buy = next(order for order in result.orders if order.side.name == "BUY")
    stop = next(order for order in result.orders if order.order_type.name == "TRAILING_STOP_LIMIT")
    assert buy.ts_init <= stop.ts_init < bars[2].ts_init
    assert stop.trigger_price.as_decimal() > Decimal(9)


def test_agy_3_entry_with_no_positive_planned_stop_is_refused(alpaca_bars: AlpacaBars) -> None:
    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 570 + i, 0.0001 + i * 0.0001) for i in range(4)]
    result = replay_tape(alpaca_bars, bars, protect=True, reference=0.0001)
    assert not any(order.side.name == "BUY" for order in result.orders)


@pytest.mark.parametrize("missing", ["symbol", "session"])
def test_agy_4_incomplete_catalog_is_refused_by_name(
    tmp_path: Path, alpaca_bars: AlpacaBars, missing: str
) -> None:
    from canon_trading.ledger import Sample
    from canon_trading.tuning_study import bounds
    from canon_trading.tuning_study import load_replay

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.1)])
    if missing == "symbol":
        ParquetDataCatalog(str(root)).write_instruments([alpaca_bars.equity("MSFT")])
        cfg = replace(cfg, symbols=("AAPL", "MSFT"))
        named = "MSFT"
    else:
        cfg = replace(cfg, sessions=(sessions[0], sessions[-1] + timedelta(days=1)))
        named = cfg.sessions[-1].isoformat()
    with pytest.raises(LookupError, match=named):
        load_replay(
            root,
            cfg,
            Sample(0, bounds(cfg.sessions[0], cfg.part)[0], bounds(cfg.sessions[-1], cfg.part)[1]),
        )


def test_agy_6_missing_validation_is_reported_before_any_study_is_written(
    tmp_path: Path, alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canon_trading.tuning_study import StudyPlan
    from canon_trading.tuning_study import bounds
    from canon_trading.tuning_study import run_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    original = ParquetDataCatalog.query
    cutoff = bounds(sessions[5], "regular")[0]

    def query(self, *args, **kwargs):
        return [
            bar
            for bar in original(self, *args, **kwargs)
            if not str(bar.bar_type).endswith("1-MINUTE-LAST-EXTERNAL") or bar.ts_init < cutoff
        ]

    monkeypatch.setattr(ParquetDataCatalog, "query", query)
    plan = StudyPlan(
        name="missing-validation",
        catalog=root,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=1,
        arms=("bars-run-1",),
        trials_per_arm=1,
    )
    report = run_study(plan)
    assert "regular" in report.quiet and any(
        "2025-03-15" in item for item in report.quiet["regular"]
    )
    assert not plan.ledger.exists()


def test_agy_8_overnight_reference_is_fixed_at_session_start(alpaca_bars: AlpacaBars) -> None:
    from canon_trading.tuning_study import BarsReplayMover

    monday = date(2025, 3, 10)
    bars = [tape_bar("AAPL", monday, 1200, 21), tape_bar("AAPL", monday + timedelta(days=1), 1, 21)]
    cfg, data = replay_inputs(alpaca_bars, bars, reference=10)
    midnight = int(datetime.combine(monday, datetime.min.time(), NY).timestamp()) * 10**9
    price = Price(20, 4)
    daily = Bar(
        BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL"),
        price,
        price,
        price,
        price,
        Quantity.from_int(100),
        midnight,
        midnight + 86_400 * 10**9,
    )
    baseline = tape_bar("AAPL", monday - timedelta(days=1), 1200, 10, volume=100)
    cfg = replace(cfg, part="overnight", sessions=(monday + timedelta(days=1),))
    data = replace(
        data, history=tuple(sorted((*data.history, daily, baseline), key=lambda b: b.ts_init))
    )
    strategy = BarsReplayMover(cfg.settings)
    strategy.prepare(cfg, data)
    assert strategy._listing(bars[0]) == strategy._listing(bars[1])
    assert strategy._reference(bars[0].bar_type.instrument_id, cfg.sessions[0]) == Decimal(20)


@pytest.mark.parametrize("case", ["aliases", "opened-suffix", "bad-arm"])
def test_agy_9_holdout_consumption_belongs_to_the_plan_not_the_marker(
    tmp_path: Path, case: str
) -> None:
    from canon_trading.tuning_study import REGISTERED_ARMS
    from canon_trading.tuning_study import open_holdout

    first = tmp_path / ("reg.opened" if case == "opened-suffix" else "reg_a.json")
    marker = {
        "arms": list(REGISTERED_ARMS),
        "plan": "same-plan",
        "holdout_dates": [day.isoformat() for day in HELD],
    }
    if case == "bad-arm":
        marker["arms"] = [None, *REGISTERED_ARMS]
    first.write_text(json.dumps(marker))
    if case == "bad-arm":
        with pytest.raises(ValueError, match="every arm"):
            open_holdout(first, plan_digest="same-plan", sessions=SESSIONS)
    else:
        opened = open_holdout(
            first, plan_digest="same-plan", sessions=SESSIONS, replay=lambda: None
        )
        assert opened.is_dir() and opened != first
        if case == "aliases":
            second = tmp_path / "reg_b.json"
            second.write_text(json.dumps(marker))
            with pytest.raises(PermissionError):
                open_holdout(
                    second,
                    plan_digest="same-plan",
                    sessions=SESSIONS,
                    replay=lambda: None,
                )


def test_agy_10_sparse_run_uses_actual_base_and_empty_quiet_falls_back(
    alpaca_bars: AlpacaBars,
) -> None:
    from canon_trading.early_entry_study import REGISTERED

    day = date(2025, 3, 10)
    bars = [
        tape_bar("AAPL", day, 570, 10.1),
        tape_bar("AAPL", day, 590, 14),
        tape_bar("AAPL", day, 959, 14.1),
    ]
    rows = replace(
        REGISTERED,
        list_move=0.001,
        list_ratio=0.1,
        baseline_sessions=1,
        run_bars=1,
        quiet_minutes=5,
        max_above_quiet=0.1,
    )
    result = replay_tape(alpaca_bars, bars, rows=rows)
    assert not any(order.side.name == "BUY" for order in result.orders)


def test_agy_11_registered_rows_and_paired_arm_anchors_are_preserved() -> None:
    from canon_trading.early_entry_study import REGISTERED
    from canon_trading.tuning_study import ARMS
    from canon_trading.tuning_study import sample_arm

    study = optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=0))
    for name, anchor in ARMS.items():
        trial = study.ask()
        rows, settings = sample_arm(trial, name, buying_power=Decimal(1000), slots=1)
        assert rows == anchor
        assert settings.run_bars == anchor.run_bars
        if name == "bars-registered":
            assert rows == REGISTERED
        study.tell(trial, 0.0)


def test_agy_5_moving_the_sample_preserves_mover_keys_and_n(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    import math

    from nautilus_trader.config import LoggerConfig

    from canon_trading.ledger import create_ledger_study
    from canon_trading.ledger import move_sample
    from canon_trading.ledger import record_trial
    from canon_trading.ranking import rank
    from canon_trading.tuning_study import bounds

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    configs = []
    for run in (1, 2):
        cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.1)], run=run)
        configs.append(replace(cfg, sessions=sessions[:5]))
    with create_ledger_study(
        "move-mover",
        start=bounds(sessions[0], "regular")[0],
        end=bounds(sessions[4], "regular")[1],
        path=tmp_path / "trials.db",
    ) as study:
        before = [
            record_trial(
                study,
                root,
                [cfg],
                starting_balances=["100000 USD"],
                logging=LoggerConfig(bypass_logging=True),
            ).config_key
            for cfg in configs
        ]
        move_sample(
            study, start=bounds(sessions[0], "regular")[0], end=bounds(sessions[6], "regular")[1]
        )
        after = [
            record_trial(
                study,
                root,
                [replace(cfg, sessions=sessions[:7])],
                starting_balances=["100000 USD"],
                logging=LoggerConfig(bypass_logging=True),
            ).config_key
            for cfg in configs
        ]
        assert after == before
        ranked = rank(study)
        assert ranked.n == 2 and math.isfinite(ranked.dsr)


def test_agy_7_post_submit_settlement_arms_a_single_tick_gap(
    alpaca_bars: AlpacaBars,
) -> None:
    day = date(2025, 3, 10)
    bars = [
        tape_bar("AAPL", day, 570, 10.1),
        tape_bar("AAPL", day, 571, 10.2),
        tape_bar("AAPL", day, 572, 9.78, opened=9.78, volume=1),
    ]
    result = replay_tape(alpaca_bars, bars, protect=True)
    native = [order for order in result.orders if order.order_type.name == "TRAILING_STOP_LIMIT"]
    assert native
    assert all(not order.filled_qty.is_positive() for order in native)
    assert any(
        type(event).__name__ == "OrderTriggered" and event.ts_event == bars[2].ts_init
        for order in native
        for event in order.events()
    )


def test_agy_5_changed_history_requires_a_sample_move(
    tmp_path: Path, alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nautilus_trader.config import LoggerConfig

    import canon_trading.tuning_study as tuning
    from canon_trading.ledger import create_ledger_study
    from canon_trading.ledger import record_trial

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.1)])
    cfg = replace(cfg, sessions=sessions[:5])
    with create_ledger_study(
        "history-identity",
        start=tuning.bounds(sessions[0], "regular")[0],
        end=tuning.bounds(sessions[4], "regular")[1],
        path=tmp_path / "trials.db",
    ) as study:
        record_trial(
            study,
            root,
            [cfg],
            starting_balances=["100000 USD"],
            logging=LoggerConfig(bypass_logging=True),
        )
        original = tuning.load_replay

        def changed(*args, **kwargs):
            data = original(*args, **kwargs)
            bar = data.history[0]
            amended = Bar(
                bar.bar_type,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                Quantity.from_int(int(bar.volume.as_decimal()) + 1),
                bar.ts_event,
                bar.ts_init,
            )
            return replace(data, history=(amended, *data.history[1:]))

        monkeypatch.setattr(tuning, "load_replay", changed)
        with pytest.raises(ValueError, match="history changed.*move_sample"):
            record_trial(
                study,
                root,
                [cfg],
                starting_balances=["100000 USD"],
                logging=LoggerConfig(bypass_logging=True),
            )
        assert len(study.trials) == 1


def test_replay_data_for_other_sessions_is_refused_before_a_trial(
    tmp_path: Path, alpaca_bars: AlpacaBars
) -> None:
    """code-review-high-n2b/replay-data: bars handed in as replay_data must be the config's own
    sessions and symbols within the sample, or the trial records sessions A over B's bars."""
    from nautilus_trader.config import LoggerConfig

    import canon_trading.tuning_study as tuning
    from canon_trading.ledger import Sample
    from canon_trading.ledger import create_ledger_study
    from canon_trading.ledger import record_trial

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.1)])
    cfg = replace(cfg, sessions=sessions[:5])
    start, end = tuning.bounds(sessions[0], "regular")[0], tuning.bounds(sessions[8], "regular")[1]
    other = tuning.load_replay(root, replace(cfg, sessions=sessions[5:9]), Sample(0, start, end))
    with create_ledger_study(
        "replay-data", start=start, end=end, path=tmp_path / "trials.db"
    ) as study:
        with pytest.raises(ValueError, match="replay_data"):
            record_trial(
                study,
                root,
                [cfg],
                starting_balances=["100000 USD"],
                logging=LoggerConfig(bypass_logging=True),
                replay_data=other,
            )
        assert not study.trials


def test_agy_10_pullback_includes_the_actual_run_base(alpaca_bars: AlpacaBars) -> None:
    from canon_trading.early_entry_study import REGISTERED

    day = date(2025, 3, 10)
    bars = [
        tape_bar("AAPL", day, 570 + i, close)
        for i, close in enumerate([10.2, 12.0, 10.2, 12.0, 12.1])
    ]
    rows = replace(
        REGISTERED,
        list_move=0.001,
        list_ratio=0.1,
        baseline_sessions=1,
        run_bars=1,
        quiet_minutes=5,
        max_above_quiet=0.05,
        pullback=0.05,
    )
    result = replay_tape(alpaca_bars, bars, rows=rows)
    buys = [
        order
        for order in result.orders
        if order.side.name == "BUY" and order.filled_qty.is_positive()
    ]
    assert buys and buys[0].ts_init == bars[3].ts_init


def test_l5_registered_fifths_are_the_shared_fold_rule() -> None:
    from canon_trading import early_entry_study as early
    from canon_trading.tuning_study import walk_forward_folds

    sessions = dates(20)
    development, holdout = early.split_dates(sessions)
    result, withheld = walk_forward_folds(
        sessions,
    )
    assert [(list(fold.train), list(fold.test)) for fold in result] == early.folds(development)
    assert tuple(holdout) == withheld


def test_l5_listing_averages_available_baselines_using_the_registered_helper(
    alpaca_bars: AlpacaBars,
) -> None:
    from canon_trading.tuning_study import BarsReplayMover

    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 570, 10.5)]
    cfg, data = replay_inputs(alpaca_bars, bars)
    cfg = replace(cfg, rows=replace(cfg.rows, baseline_sessions=5))
    strategy = BarsReplayMover(cfg.settings)
    strategy.prepare(cfg, data)
    assert strategy._listing(bars[0]) is not None  # ADR 0092 averages the one earlier session.


def test_l5_covered_quiet_symbol_session_is_not_missing_data(
    tmp_path: Path,
    alpaca_bars: AlpacaBars,
) -> None:
    from canon_trading.ledger import Sample
    from canon_trading.tuning_study import bounds
    from canon_trading.tuning_study import load_replay

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    catalog = ParquetDataCatalog(str(root))
    catalog.write_instruments([alpaca_bars.equity("MSFT")])
    # A real covered window between prints, with no print in this symbol-session.
    before = tape_bar("MSFT", sessions[0] - timedelta(days=1), 570, 10)
    after = tape_bar("MSFT", sessions[0] + timedelta(days=1), 570, 10)
    catalog.write_bars([before, after], start=before.ts_event, end=after.ts_init)
    midnight = (
        int(datetime.combine(sessions[0] - timedelta(days=1), time(), NY).timestamp()) * 10**9
    )
    reference = Bar(
        BarType.from_str("MSFT.ALPACA-1-DAY-LAST-EXTERNAL"),
        *(Price(10, 4),) * 4,
        Quantity.from_int(100_000),
        midnight,
        midnight + 86_400 * 10**9,
    )
    catalog.write_bars([reference], start=reference.ts_event, end=reference.ts_init)
    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.5)])
    cfg = replace(cfg, symbols=("AAPL", "MSFT"))
    data = load_replay(root, cfg, Sample(0, *bounds(sessions[0], "regular")))
    assert data.bars and not any(
        str(bar.bar_type.instrument_id) == "MSFT.ALPACA" for bar in data.bars
    )
    assert data.quiet == (f"MSFT.ALPACA-1-MINUTE-LAST-EXTERNAL {sessions[0]}",)


def test_l5_baseline_candidates_keep_the_registered_lookback_bound(
    alpaca_bars: AlpacaBars,
) -> None:
    from canon_trading.early_entry_study import MAX_BASELINE
    from canon_trading.tuning_study import BarsReplayMover

    day = date(2025, 3, 10)
    bar = tape_bar("AAPL", day, 570, 10.5)
    cfg, data = replay_inputs(alpaca_bars, [bar])
    oldest = day - timedelta(days=MAX_BASELINE + 1)

    class Coverage:
        def get_missing_intervals_for_request(self, start, end, *_):
            # Only the session beyond the registered candidate bound is covered.
            return (
                [] if datetime.fromtimestamp(start / 10**9, NY).date() == oldest else [(start, end)]
            )

    history = (
        *data.history,
        *(
            tape_bar("AAPL", day - timedelta(days=i), 570, 10, volume=100)
            for i in range(2, MAX_BASELINE + 2)
        ),
    )
    strategy = BarsReplayMover(cfg.settings)
    strategy.prepare(cfg, replace(data, history=history, catalog=Coverage()))
    assert not strategy.frames[(bar.bar_type.instrument_id, day)].earlier
    assert strategy._listing(bar) is None


def test_l5_overnight_reference_uses_the_previous_daily_close(alpaca_bars: AlpacaBars) -> None:
    from canon_trading.tuning_study import BarsReplayMover

    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 1200, 21)]
    cfg, data = replay_inputs(alpaca_bars, bars)
    midnight = int(datetime.combine(day, datetime.min.time(), NY).timestamp()) * 10**9
    p = Price(20, 4)
    previous_day = Bar(
        BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL"),
        p,
        p,
        p,
        p,
        Quantity.from_int(100),
        midnight,
        midnight + 86_400 * 10**9,
    )
    cfg = replace(cfg, part="overnight", sessions=(day + timedelta(days=1),))
    data = replace(data, history=(*data.history, previous_day))
    strategy = BarsReplayMover(cfg.settings)
    strategy.prepare(cfg, data)
    assert strategy._reference(bars[0].bar_type.instrument_id, cfg.sessions[0]) == Decimal(20)


def test_l5_catalog_definitions_are_aligned_one_per_bar_type(
    tmp_path: Path,
    alpaca_bars: AlpacaBars,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from canon_trading.ledger import Sample
    from canon_trading.tuning_study import bounds
    from canon_trading.tuning_study import load_replay

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    original = ParquetDataCatalog.instruments
    monkeypatch.setattr(
        ParquetDataCatalog, "instruments", lambda self, **kw: original(self, **kw) * 2
    )
    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", sessions[0], 570, 10.5)])
    data = load_replay(root, cfg, Sample(0, *bounds(sessions[0], "regular")))
    assert len(data.instruments) == len(data.bar_types) == 1


def test_l5_complete_ranked_configuration_round_trips_through_the_promotion_contract(
    alpaca_bars: AlpacaBars,
) -> None:
    from canon_trading.ledger import trial_config
    from canon_trading.tuning_contract import RankedConfiguration

    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", date(2025, 3, 10), 570, 10.5)])
    cfg = replace(
        cfg,
        rows=replace(
            cfg.rows,
            list_move=0.05,
            list_ratio=5.0,
            quiet_minutes=30,
            max_above_quiet=0.25,
            pullback=0.05,
        ),
    )
    ranked = trial_config([cfg], starting_balances=["100000 USD"])
    candidate = RankedConfiguration.from_config(ranked)
    promoted = RankedConfiguration.decode(json.loads(json.dumps(candidate.encode())))
    assert promoted.config == ranked
    assert promoted.rows == cfg.rows
    assert promoted.mover_config.max_move == cfg.settings.max_move
    assert promoted.key == candidate.key
    changed = candidate.encode()
    changed["config"]["strategies"][0]["config"]["max_move"] = "0.123"
    with pytest.raises(ValueError, match="differs from the ranked key"):
        RankedConfiguration.decode(changed, expected_key=candidate.key)


def test_a_configuration_keyed_with_both_persistence_arms_off_holds_no_persistence_row(
    alpaca_bars: AlpacaBars,
) -> None:
    """A trial keyed before the persistence rows existed has none of them, and one keyed now with both
    arms off has none either: the same key, and a stored configuration that decodes as off."""
    from canon_trading.ledger import trial_config
    from canon_trading.mover import PERSISTENCE_FIELDS
    from canon_trading.tuning_contract import RankedConfiguration

    cfg, _ = replay_inputs(alpaca_bars, [tape_bar("AAPL", date(2025, 3, 10), 570, 10.5)])
    ranked = trial_config([cfg], starting_balances=["100000 USD"])
    assert not set(PERSISTENCE_FIELDS) & set(ranked["strategies"][0]["config"])
    promoted = RankedConfiguration.decode(
        json.loads(json.dumps(RankedConfiguration.from_config(ranked).encode()))
    )
    settings = promoted.mover_config
    assert settings.persistence is False
    assert (settings.breach_count, settings.high_count) == (1, 1)


def test_l5_catalog_history_is_read_once_per_feed_in_a_study_run(
    tmp_path: Path,
    alpaca_bars: AlpacaBars,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import canon_trading.tuning_study as tuning

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    original = ParquetDataCatalog.query
    reads = []

    def query(self, *args, **kwargs):
        reads.append(tuple(kwargs["identifiers"]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ParquetDataCatalog, "query", query)
    tuning.run_study(
        tuning.StudyPlan(
            name="read-once",
            catalog=root,
            ledger=tmp_path / "trials.db",
            symbols=("AAPL",),
            sessions=sessions,
            buying_power=Decimal(1000),
            slots=1,
            arms=("bars-run-1",),
            trials_per_arm=2,
        )
    )
    assert len(reads) == 2  # minute and daily bars, reused by every SIP part/fold/trial.


def test_l5_holdout_dates_are_shared_and_failed_replay_does_not_consume(
    tmp_path: Path,
) -> None:
    from canon_trading import early_entry_study as early
    from canon_trading import tuning_study as tuning

    held = HELD
    one, two = tmp_path / "seed-a.json", tmp_path / "seed-b.json"

    def registration(path: Path, plan: str):
        path.write_text(
            json.dumps(
                {
                    "plan": plan,
                    "arms": list(tuning.REGISTERED_ARMS),
                    "holdout_dates": [day.isoformat() for day in held],
                }
            )
        )

    registration(one, "seed-a")
    registration(two, "seed-b")

    def failure():
        raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError, match="replay failed"):
        tuning.open_holdout(one, plan_digest="seed-a", sessions=SESSIONS, replay=failure)
    tuning.open_holdout(one, plan_digest="seed-a", sessions=SESSIONS, replay=lambda: None)
    with pytest.raises(PermissionError, match="opened once"):
        tuning.open_holdout(two, plan_digest="seed-b", sessions=SESSIONS, replay=lambda: None)
    with pytest.raises(PermissionError, match="opened once"):
        early.open_holdout(two, dates=held, replay=lambda: None)


def test_l5_no_value_trials_sort_after_numeric_trials(
    tmp_path: Path,
    alpaca_bars: AlpacaBars,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import canon_trading.tuning_study as tuning
    from canon_trading.ledger import open_study

    root = tmp_path / "catalog"
    sessions = catalog_data(root, alpaca_bars)
    sample = tuning.sample_arm
    calls = 0

    def sampled(*args, **kwargs):
        nonlocal calls
        rows, settings = sample(*args, **kwargs)
        if calls % 2 == 0:
            rows = replace(rows, run_bars=50)
            settings.run_bars = 50
        calls += 1
        return rows, settings

    monkeypatch.setattr(tuning, "sample_arm", sampled)
    plan = tuning.StudyPlan(
        name="sort-empty",
        catalog=root,
        ledger=tmp_path / "trials.db",
        symbols=("AAPL",),
        sessions=sessions,
        buying_power=Decimal(1000),
        slots=1,
        arms=("bars-run-1",),
        trials_per_arm=2,
    )
    report = tuning.run_study(plan)
    candidate = report.folds[-1].training
    with open_study(candidate.study, plan.ledger) as study:
        assert study.trials[0].value is None and study.trials[1].value is not None
        assert (
            next(iter(candidate.candidates_by_trial_key))
            == study.trials[1].user_attrs["config_key"]
        )


def test_tuning_and_early_entry_share_one_holdout_root_whatever_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code-review-high-n2b/holdout-root: the tuning marker sat beside --ledger while
    early_entry_study used the default ledger's folder, so the same dates opened once in each."""
    import canon_trading.early_entry_study as early
    import canon_trading.tuning_study as tuning

    monkeypatch.setattr(early, "default_holdout_root", lambda: tmp_path / "shared")
    sessions = dates(10)
    _, held = early.split_dates(sessions)
    registration = tmp_path / "reg.json"
    registration.write_text(
        json.dumps(
            {
                "plan": "p",
                "arms": list(tuning.REGISTERED_ARMS),
                "holdout_dates": [day.isoformat() for day in held],
            }
        )
    )
    tuning.open_holdout(
        registration,
        plan_digest="p",
        sessions=sessions,
        replay=lambda: None,
    )
    with pytest.raises(PermissionError, match="opened once"):
        early.open_holdout(tmp_path / "list.csv", dates=held, replay=lambda: None)


def test_a_registration_must_hold_the_plans_own_holdout(tmp_path: Path) -> None:
    """code-review-high-n2b/holdout-split: holdout_dates must be split_dates(plan.sessions)'s
    held 30%, not any dates the registration names."""
    import canon_trading.early_entry_study as early
    import canon_trading.tuning_study as tuning

    sessions = dates(10)
    _, held = early.split_dates(sessions)
    registration = tmp_path / "reg.json"
    for wrong in (held[:1], sessions[:3]):
        registration.write_text(
            json.dumps(
                {
                    "plan": "p",
                    "arms": list(tuning.REGISTERED_ARMS),
                    "holdout_dates": [day.isoformat() for day in wrong],
                }
            )
        )
        with pytest.raises(ValueError, match="plan's holdout"):
            tuning.open_holdout(
                registration, plan_digest="p", sessions=sessions, replay=lambda: None
            )


def test_tests_open_no_holdout_in_the_main_checkout() -> None:
    """A test that opens a holdout at the default root must not mark the main checkout's
    ledger/holdouts, which would refuse the real study's opening of those dates."""
    from canon_trading import early_entry_study
    from canon_trading import ledger

    assert (
        early_entry_study.default_holdout_root() != ledger.default_ledger_path().parent / "holdouts"
    )


def test_an_error_inside_a_replay_handler_fails_the_replay(
    alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pinned engine logs and suppresses a Python time-event callback's exception
    # (crates/common/src/timer.rs, PythonTimeEventCallback::call), so a replay must carry its own.
    from canon_trading import tuning_study

    def broken(self, *args, **kwargs):
        raise RuntimeError("entry sizing broke")

    monkeypatch.setattr(tuning_study.BarsReplayMover, "_buy", broken)
    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 570 + i, 10 + i * 0.1) for i in range(4)]
    with pytest.raises(RuntimeError, match="replay handler raised.*entry sizing broke"):
        replay_tape(alpaca_bars, bars)


def test_a_native_stop_limit_seeded_under_its_limit_offset_is_priced_at_least_one_tick(
    alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code-review-high-n4/sub-dollar-limit: the seed is the close when protection is placed, which
    can lie under the limit offset (at least a cent under a dollar, `offsets`) once the close has
    fallen since the entry; `close - limit` then priced the native stop-limit below zero."""
    from canon_trading import tuning_study

    real = tuning_study.offsets

    def wide(entry: Decimal, trail: Decimal, exit_through: Decimal) -> tuple[Decimal, Decimal]:
        trailing, _ = real(entry, trail, exit_through)
        return trailing, entry + Decimal("0.01")  # a limit offset over the seed close

    monkeypatch.setattr(tuning_study, "offsets", wide)
    day = date(2025, 3, 10)
    bars = [tape_bar("AAPL", day, 570 + i, 0.50 + i * 0.01) for i in range(4)]
    result = replay_tape(alpaca_bars, bars, protect=True, reference=0.30)
    seeded = [
        order.events()[0].price.as_decimal()
        for order in result.orders
        if order.order_type.name == "TRAILING_STOP_LIMIT"
    ]
    assert seeded and min(seeded) >= Decimal("0.0001")


def test_a_plans_opening_lists_its_waiting_arms_as_pending_at_the_checkouts_head(
    tmp_path: Path,
) -> None:
    """code-review-high-n2c/1: the registration names the waiting arms so that they may replay on the
    same held dates once their inputs land; the opening's manifest is what lets them (ADR 0100)."""
    from canon_trading import early_entry_study
    from canon_trading.tuning_study import ARMS
    from canon_trading.tuning_study import REGISTERED_ARMS
    from canon_trading.tuning_study import WAITING
    from canon_trading.tuning_study import open_holdout

    marker = tmp_path / "registered.json"
    marker.write_text(
        json.dumps(
            {
                "arms": list(REGISTERED_ARMS),
                "plan": "abc",
                "holdout_dates": [day.isoformat() for day in HELD],
            }
        )
    )
    root = open_holdout(marker, plan_digest="abc", sessions=SESSIONS, replay=lambda: None)
    (manifest,) = root.glob("*.manifest.json")
    head = early_entry_study.registered_commit("HEAD")
    assert json.loads(manifest.read_text())["arms"] == {
        **{arm: {"commit": head, "status": early_entry_study.REPLAYED} for arm in ARMS},
        **{arm: {"commit": head, "status": early_entry_study.PENDING} for arm in WAITING},
    }


def test_a_trial_frames_only_its_own_sessions_and_hashes_the_history_once(
    alpaca_bars: AlpacaBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of N2 (code-review-high-n2c/5): every trial built a frame, a registered listing and
    the baseline catalog probes for every day of the ~90-day history, not only its own sessions, and
    the history digest was hashed again on every record."""
    from canon_trading import tuning_study
    from canon_trading.early_entry_study import MAX_BASELINE
    from canon_trading.tuning_study import BarsReplayMover

    day = date(2025, 3, 10)
    bar = tape_bar("AAPL", day, 570, 10.5)
    cfg, data = replay_inputs(alpaca_bars, [bar])
    history = (
        *data.history,
        *(tape_bar("AAPL", day - timedelta(days=i), 570, 10, volume=100) for i in range(2, 40)),
    )
    probes: list[int] = []

    class Coverage:
        def get_missing_intervals_for_request(self, start, end, *_):
            probes.append(start)
            return []

    strategy = BarsReplayMover(cfg.settings)
    strategy.prepare(cfg, replace(data, history=history, catalog=Coverage()))
    assert {on for _, on in strategy.frames} == {day}
    assert len(probes) <= MAX_BASELINE
    hashed: list[int] = []
    real = tuning_study.bars_digest

    def counting(*args, **kwargs):
        hashed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(tuning_study, "bars_digest", counting)
    once = replace(data, history=history)
    assert once.history_digest == once.history_digest
    assert len(hashed) == 1


def p0_trial():
    from canon_trading.early_entry_study import REGISTERED
    from canon_trading.ledger import MoverTrialConfig
    from canon_trading.mover import MoverConfig

    return MoverTrialConfig(
        settings=MoverConfig(
            max_notional=Decimal(1000),
            max_move=Decimal(1000),
            run_bars=1,
            protect=False,
            entry_arm="bridge",
        ),
        symbols=("AAPL",),
        part="regular",
        sessions=(date(2025, 3, 10),),
        history_start=date(2025, 2, 8),
        buying_power=Decimal(1000),
        arm="bars-registered",
        rows=REGISTERED,
    )


def test_p0_existing_mover_trial_keeps_its_literal_key() -> None:
    from canon_trading.ledger import config_key
    from canon_trading.ledger import trial_config

    cfg = p0_trial()
    assert config_key(trial_config([cfg], starting_balances=["100000 USD"])) == (
        "213b37b6d3bba04c68c7c7d5b8bb91b73e74a8476c6f5a46179c18b0eec2cf88"
    )


@pytest.mark.parametrize("mode", ["atr", "tier", "ratchet"])
def test_p0_bars_replay_refuses_non_fixed_trail_by_name(mode: str) -> None:
    from canon_trading.mover import MoverConfig
    from canon_trading.tuning_study import BarsReplayMover

    config = MoverConfig(max_notional=Decimal(1000), max_move=Decimal(1000), trail_mode=mode)
    with pytest.raises(ValueError, match=f"trail_mode.*{mode}"):
        BarsReplayMover(config)


def test_p0_window_rows_do_not_move_the_trial_key_or_replay_settings() -> None:
    from canon_trading.ledger import config_key
    from canon_trading.ledger import trial_config
    from canon_trading.mover import TRAIL_SETTINGS
    from canon_trading.tuning_contract import BARS_REPLAY_FIXED_ROWS
    from canon_trading.tuning_contract import RankedConfiguration
    from canon_trading.tuning_study import BarsReplayMover

    cfg = p0_trial()
    original = trial_config([cfg], starting_balances=["100000 USD"])
    replay = BarsReplayMover(cfg.settings)
    expected = {name: getattr(replay.settings, name) for name in BARS_REPLAY_FIXED_ROWS}
    assert expected == BARS_REPLAY_FIXED_ROWS
    for name, pinned in BARS_REPLAY_FIXED_ROWS.items():
        rows = TRAIL_SETTINGS[name]
        values = (pinned, *rows.get("alternates", rows.get("arms", ())))
        for value in values:
            from canon_trading.ma_cross import BASE_FIELDS
            from canon_trading.mover import OWN_FIELDS
            from canon_trading.mover import MoverConfig

            own = {field: getattr(cfg.settings, field) for field in OWN_FIELDS}
            base = {field: getattr(cfg.settings, field) for field in BASE_FIELDS}
            settings = MoverConfig(**(own | {name: value}), **base)
            if name in ("trail_mode", "reentry_arm") and value != pinned:
                with pytest.raises(ValueError, match=f"{name}.*{value}"):
                    BarsReplayMover(settings)
                with pytest.raises(ValueError, match=f"{name}.*{value}"):
                    trial_config(
                        [replace(cfg, settings=settings)], starting_balances=["100000 USD"]
                    )
                continue
            changed = trial_config(
                [replace(cfg, settings=settings)], starting_balances=["100000 USD"]
            )
            assert changed == original and config_key(changed) == config_key(original)
            replay = BarsReplayMover(settings)
            assert {
                field: getattr(replay.settings, field) for field in BARS_REPLAY_FIXED_ROWS
            } == expected
            # The caller's own/base configuration was copied, preserving its selected inactive row.
            assert getattr(settings, name) == value
    decoded = RankedConfiguration.decode(RankedConfiguration.from_config(original).encode())
    assert {
        name: getattr(decoded.mover_config, name) for name in BARS_REPLAY_FIXED_ROWS
    } == expected
