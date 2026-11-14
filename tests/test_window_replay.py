"""S2: neutral catalogs exercise the pinned BacktestEngine and the real Mover composition."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.trading import Strategy

from canon_trading import movers_rank
from canon_trading import movers_rank_actor
from canon_trading.movers_rank_signal import decode_ranking

NY = ZoneInfo("America/New_York")
START = datetime(2025, 11, 10, 9, 30, tzinfo=NY)
OPEN = START.replace(hour=4, minute=0)
END = START + timedelta(minutes=20)
MINUTE = 60_000_000_000
SECOND = 1_000_000_000
OFFLINE_GUARDS = """
import os
import socket
def forbidden(*args, **kwargs):
    raise AssertionError("outside world reached")
socket.socket.connect = forbidden
for key in tuple(os.environ):
    if key.startswith("ALPACA_"):
        del os.environ[key]
original_getitem = os.environ.__class__.__getitem__
def no_key_reads(self, key):
    if key.startswith("ALPACA_"):
        raise AssertionError("Alpaca key environment read")
    return original_getitem(self, key)
os.environ.__class__.__getitem__ = no_key_reads
from canon_trading import window_replay
from canon_trading import movers_rank_actor
from canon_alpaca import providers
from alpaca.trading.client import TradingClient
providers.credentials_from_env = forbidden
movers_rank_actor.credentials_from_env = forbidden
movers_rank_actor.alpaca_reference_reader = forbidden
movers_rank_actor.StockHistoricalDataClient.__init__ = forbidden
movers_rank_actor.CorporateActionsClient.__init__ = forbidden
TradingClient.__init__ = forbidden
"""


def ns(at: datetime) -> int:
    return int(at.timestamp() * SECOND)


def minute_bar(instrument, opened: datetime, close: float, volume: int) -> Bar:
    price = Price(close, 4)
    return Bar(
        BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL"),
        price,
        price,
        price,
        price,
        Quantity.from_int(volume),
        ns(opened),
        ns(opened) + MINUTE,
    )


def quote(instrument, at: int, bid: float, ask: float, size: int = 100_000) -> QuoteTick:
    return QuoteTick(
        instrument.id,
        Price(bid, 4),
        Price(ask, 4),
        Quantity.from_int(size),
        Quantity.from_int(size),
        at,
        at,
    )


def trade(instrument, at: int, price: float, size: int = 100_000) -> TradeTick:
    return TradeTick(
        instrument.id,
        Price(price, 4),
        Quantity.from_int(size),
        AggressorSide.BUY,
        TradeId(f"T-{at}"),
        at,
        at,
    )


@pytest.fixture
def neutral_catalogs(tmp_path, alpaca_bars):
    """Every file, including the baseline and engine data, is under this test's tmp_path."""
    bars_root, quotes_root, trades_root = (tmp_path / name for name in ("bars", "quotes", "trades"))
    for root in (bars_root, quotes_root, trades_root):
        root.mkdir()
    bars = ParquetDataCatalog(str(bars_root))
    quotes = ParquetDataCatalog(str(quotes_root))
    trades = ParquetDataCatalog(str(trades_root))
    instruments = [alpaca_bars.equity("LLL"), alpaca_bars.equity("Z0")]
    bars.write_instruments(instruments)
    sessions = tuple(START.date() - timedelta(days=n) for n in (3, 4, 5, 6, 7))
    current = []
    closes = [10 + i / 10 for i in range(9)] + [10.45, 10.0, 9.9] + [9.8] * 8
    for instrument in instruments:
        # Previous-session minute history is needed by the registered entry arm, separately
        # from the ranking actor's segment baseline. No fabricated live history is substituted.
        earlier = [
            minute_bar(instrument, OPEN.replace(day=day.day) + timedelta(minutes=i), 8, 1)
            for day in reversed(sessions)
            for i in range(720)
        ]
        bars.write_bars(earlier, start=earlier[0].ts_event, end=earlier[-1].ts_init)
        series = [minute_bar(instrument, OPEN + timedelta(minutes=i), 8, 100) for i in range(330)]
        series += [
            minute_bar(
                instrument,
                START + timedelta(minutes=i),
                close if instrument == instruments[0] else 8,
                100_000,
            )
            for i, close in enumerate(closes)
        ]
        bars.write_bars(series, start=ns(OPEN), end=ns(END))
        current.extend(series)
    tape, book = [], []
    for i, close in enumerate(closes):
        at = ns(START) + (i + 1) * MINUTE
        tape.append(trade(instruments[0], at - 2, close))
        book.append(quote(instruments[0], at - 1, close - 0.01, close + 0.01))
    trades.write_trade_ticks(tape, start=ns(START), end=ns(END))
    quotes.write_quote_ticks(book, start=ns(START), end=ns(END))
    baseline_path = tmp_path / "baseline.json"
    baselines = {
        instrument.id.symbol.value: movers_rank.Baseline(
            cumulative=tuple(1000.0 * (i + 1) for i in range(movers_rank.SEGMENTS)),
            sessions=tuple(sorted(sessions)),
            traded_sessions=(5,) * movers_rank.SEGMENTS,
        )
        for instrument in instruments
    }
    movers_rank.write_baselines(baseline_path, START.date(), baselines)
    spec = {
        "bars_root": str(bars_root),
        "quotes_roots": [str(quotes_root)],
        "trades_root": str(trades_root),
        "baseline_path": str(baseline_path),
        "trade_date": START.date().isoformat(),
        "window_start": START.isoformat(),
        "window_end": END.isoformat(),
        "wind_down_at": (END - timedelta(minutes=2)).isoformat(),
        "trail_arm": "F2",
        "fill_arm": "touch",
        "slots": 1,
        "entry_notional": "500",
        "starting_balance": "5000 USD",
        "roster": [{"symbol": "LLL", "entry_limit": "10.01"}],
        "entry_contexts": {
            "LLL.ALPACA": {
                "day": START.date().isoformat(),
                "bars": 330,
                "earlier_sessions": 5,
                "reference": 8.0,
                "reference_from": "synthetic prior session",
                "covered_to": 570,
                "missing": None,
            }
        },
        "reference_closes": {
            instrument.id.symbol.value: {"close": 8.0, "session": sessions[0].isoformat()}
            for instrument in instruments
        },
    }
    return spec, instruments, current, baselines


class OwnedLimit(Strategy):
    """Buy a real long, then release its sell LIMIT on the next quote; never sell while flat."""

    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        self.instrument = None
        self.bought = False
        self.sold = False
        self.sell = None
        self.released = []

    def on_start(self):
        assert self.instrument is not None
        self.subscribe_quotes(self.instrument.id)

    def on_quote(self, quote):
        assert self.instrument is not None
        if not self.bought:
            self.bought = True
            self.submit_order(
                self.order_factory.market(self.instrument.id, OrderSide.BUY, Quantity.from_int(10))
            )
        elif not self.sold and self.portfolio.net_position(self.instrument.id) == Decimal(10):
            self.sold = True
            self.sell = self.order_factory.stop_limit(
                self.instrument.id,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price(11, 4),
                trigger_price=Price(10, 4),
                trigger_type=TriggerType.BID_ASK,
                emulation_trigger=TriggerType.BID_ASK,
            )
            self.submit_order(self.sell)

    def on_order_released(self, event):
        self.released.append(event.client_order_id)


@pytest.mark.parametrize("arm,expected", [("touch", [10]), ("tape", [3, 3, 3, 1])])
def test_venue_presets_release_owned_limit_at_touch_or_print_size(alpaca_bars, arm, expected):
    from canon_trading.window_replay import add_replay_venue
    from canon_trading.window_replay import venue_preset

    preset = venue_preset(arm)
    assert preset == {
        "name": "ALPACA",
        "preset": arm,
        "oms_type": "NETTING",
        "account_type": "CASH",
        "book_type": "L1_MBP",
        "maker_rate": "0",
        "taker_rate": "0",
        "bar_execution": False,
        "trade_execution": arm == "tape",
        "liquidity_consumption": arm == "tape",
    }
    instrument = alpaca_bars.equity("LLL")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True)))
    add_replay_venue(engine, arm, "5000 USD")
    engine.add_instrument(instrument)
    strategy = OwnedLimit(StrategyConfig())
    strategy.instrument = instrument
    engine.add_strategy(strategy)
    at = ns(START)
    engine.add_data(
        [
            quote(instrument, at, 9.99, 10.01),
            quote(instrument, at + SECOND, 9.99, 10.01),
            trade(instrument, at + 2 * SECOND, 11, 3),
            trade(instrument, at + 3 * SECOND, 11, 3),
            trade(instrument, at + 4 * SECOND, 11, 3),
            # A later minute supplies enough quoted liquidity to finish. During the print
            # minute tape can fill only nine of the ten shares. Touch waits for this quote
            # and fills the whole sell; neither preset is a trade-only venue.
            quote(instrument, at + 61 * SECOND, 11.01, 11.02, 10),
        ]
    )
    try:
        engine.run()
        assert strategy.sell is not None
        assert strategy.released == [strategy.sell.client_order_id]
        fills = engine.generate_fills_report().to_dict("records")
        sizes = [
            int(Decimal(str(row["last_qty"]))) for row in fills if str(row["order_side"]) == "SELL"
        ]
        assert sizes == expected
        if arm == "tape":
            assert sum(sizes[:-1]) == 9
            assert all(size <= 3 for size in sizes[:-1])
        assert engine.portfolio.net_position(instrument.id) == Decimal(10 - sum(expected))
    finally:
        engine.dispose()


def test_every_generation_matches_pure_rank_over_the_same_prefix(neutral_catalogs, monkeypatch):
    from canon_trading import window_replay

    spec, instruments, bars, baselines = neutral_catalogs

    def forbidden(*args, **kwargs):
        pytest.fail("default Alpaca reference reader called")

    monkeypatch.setattr(movers_rank_actor, "alpaca_reference_reader", forbidden)
    monkeypatch.setattr(window_replay, "_WORKER", OFFLINE_GUARDS + window_replay._WORKER)
    report = window_replay.run_replay(spec)
    generations = report["generations"]
    assert len(generations) > 300
    references = {
        symbol: movers_rank.Reference(
            close=float(row["close"]), session=datetime.fromisoformat(row["session"]).date()
        )
        for symbol, row in spec["reference_closes"].items()
    }
    assets = {i.id.symbol.value: dict(i.info) for i in instruments}
    for recorded in generations:
        at = recorded["ts_event"]
        tallies = {}
        for bar in sorted(bars, key=lambda b: b.ts_init):
            if bar.ts_init > at:
                continue
            symbol = bar.bar_type.instrument_id.symbol.value
            tallies[symbol] = tallies.get(symbol, movers_rank.Tally(session=START.date())).add(bar)
        generation = movers_rank.rank(
            session=START.date(),
            assets=assets,
            tallies=tallies,
            baselines=baselines,
            references=references,
            opens={},
            statuses={},
            splits=movers_rank.SplitRead(
                start=min(r.session for r in references.values()), end=START.date(), actions=()
            ),
            security_types=movers_rank.security_types_from_names(assets),
            order_cap=500,
            computed_ns=at,
        )
        expected = tuple(movers_rank.top_entries(generation))
        assert decode_ranking(recorded["value"]) == expected
    assert report["catalog"]["bars_read"] == len(bars)
    assert all(batch["end_ns"] - batch["start_ns"] <= 60 * MINUTE for batch in report["batches"])
    assert len(report["batches"]) >= 6


@pytest.mark.parametrize("fill_arm", ["touch", "tape"])
@pytest.mark.parametrize("trail_arm", ["F2", "OWN1", "OWN3"])
def test_window_enters_protects_trails_and_exits(
    neutral_catalogs, monkeypatch, fill_arm, trail_arm
):
    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    # Sample native cached stop prices; emulated trailing updates do not reach the
    # strategy's on_order_updated callback at this pin. Keep the actual quote handler.
    injection = """
from canon_trading.mover import Mover
on_quote = Mover.on_quote
record = Mover.record
triggers = {}
def observed_quote(self, quote):
    on_quote(self, quote)
    for order in self.cache.orders(strategy_id=self.strategy_id):
        price = getattr(order, "trigger_price", None)
        if price is not None:
            triggers.setdefault(str(order.client_order_id), []).append(str(price))
def observed_record(self):
    return {**record(self), "observed_trigger_prices": triggers}
Mover.on_quote = observed_quote
Mover.record = observed_record
"""
    monkeypatch.setattr(window_replay, "_WORKER", injection + window_replay._WORKER)
    report = window_replay.run_replay({**spec, "fill_arm": fill_arm, "trail_arm": trail_arm})
    run = report["mover"]["run"]
    assert run["rows"]["entry_arm"] == "registered"
    assert run["entries"], run["notes"]
    assert all(ns(START) <= e["submitted_ns"] < ns(END) for e in run["entries"])
    assert run["rankings"] and run["ranking_records"]
    assert run["released"] and run["exits"]
    assert any(
        len(prices) > 1 and Decimal(prices[-1]) > Decimal(prices[0])
        for prices in run["observed_trigger_prices"].values()
    ), run["observed_trigger_prices"]
    assert all(row["trigger_reason"] == "trailing stop" for row in run["exits"])
    assert {fill[1] for fill in run["fills"]} == {"BUY", "SELL"}
    assert run["refused"] == []
    assert report["orders"] and report["fills"] and report["positions"]
    assert report["open_positions"] == 0
    assert all(cap["checks"] > 0 and cap["tripped"] is None for cap in report["position_caps"])
    assert report["venue"]["preset"] == fill_arm
    assert report["trail_arm"] == trail_arm
    assert report["catalog"]["quotes_read"] == 20
    assert report["catalog"]["trades_read"] == 20
    json.dumps(report, allow_nan=False)


def test_full_cli_run_never_reaches_the_outside_world(neutral_catalogs, monkeypatch, tmp_path):
    from alpaca.trading.client import TradingClient
    from canon_alpaca import providers

    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    for name in tuple(os.environ):
        if name.startswith("ALPACA_"):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("outside world reached")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(providers, "credentials_from_env", forbidden)
    monkeypatch.setattr(movers_rank_actor, "credentials_from_env", forbidden)
    monkeypatch.setattr(movers_rank_actor.StockHistoricalDataClient, "__init__", forbidden)
    monkeypatch.setattr(movers_rank_actor.CorporateActionsClient, "__init__", forbidden)
    monkeypatch.setattr(TradingClient, "__init__", forbidden)
    monkeypatch.setattr(window_replay, "_WORKER", OFFLINE_GUARDS + window_replay._WORKER)
    source, output = tmp_path / "spec.json", tmp_path / "report.json"
    source.write_text(json.dumps(spec))
    assert window_replay.main(["--spec", str(source), "--report", str(output)]) == 0
    assert json.loads(output.read_text())["mover"]["run"]["entries"]


def test_spec_from_record_keeps_choices_contexts_and_window_bounds(neutral_catalogs):
    from canon_trading.window_replay import spec_from_record

    spec, *_ = neutral_catalogs
    record: dict[str, Any] = {
        "session": "regular",
        "part": "regular",
        "strategy": "Mover",
        "arm": "F2",
        "started_epoch": START.timestamp(),
        "planned_seconds": 1200,
        "commit": "synthetic-code",
        "mover": {
            "roster": spec["roster"],
            "entry_contexts": spec["entry_contexts"],
            "allocation": {"slots": 1, "entry_notional": "500", "cash": "5000"},
        },
    }
    converted = spec_from_record(
        record,
        bars_root=spec["bars_root"],
        quotes_roots=spec["quotes_roots"],
        trades_root=spec["trades_root"],
        baseline_path=spec["baseline_path"],
        reference_closes=spec["reference_closes"],
        fill_arm="tape",
    )
    for key in ("roster", "entry_contexts", "trade_date", "slots", "entry_notional"):
        assert converted[key] == spec[key]
    assert datetime.fromisoformat(converted["window_start"]) == START
    assert datetime.fromisoformat(converted["window_end"]) == END
    assert converted["source_record"]["commit"] == "synthetic-code"
    assert converted["source_record"]["choices"] == "pre-node choice taken from the record"


def test_cli_imports_in_a_fresh_process_without_keys_or_connections(neutral_catalogs, tmp_path):
    spec, *_ = neutral_catalogs
    source, output = tmp_path / "cold-spec.json", tmp_path / "cold-report.json"
    source.write_text(json.dumps(spec))
    script = f"""
import sys
import socket
def forbidden(*args, **kwargs):
    raise AssertionError("socket connection attempted")
socket.socket.connect = forbidden
from canon_trading import window_replay
window_replay._WORKER = {OFFLINE_GUARDS!r} + window_replay._WORKER
sys.exit(window_replay.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "--spec", str(source), "--report", str(output)],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["mover"]["run"]["entries"]


def test_daily_reference_catalog_and_explicit_split_reach_the_actor(
    neutral_catalogs, alpaca_bars, tmp_path
):
    from canon_trading.window_replay import run_replay

    spec, instruments, *_ = neutral_catalogs
    root = tmp_path / "daily"
    root.mkdir()
    catalog = ParquetDataCatalog(str(root))
    for instrument in instruments:
        bars = alpaca_bars.daily(
            instrument, [8.0], first=START.replace(day=7, hour=0, minute=0), previous=8.0
        )
        catalog.write_bars(bars)
    spec.pop("reference_closes")
    spec["reference_root"] = str(root)
    spec["splits"] = [
        {
            "corporate_action_type": "forward_splits",
            "id": "00000000-0000-0000-0000-000000000000",
            "symbol": "LLL",
            "cusip": "000000000",
            "new_rate": 2.0,
            "old_rate": 1.0,
            "process_date": START.date().isoformat(),
            "ex_date": START.date().isoformat(),
        }
    ]
    report = run_replay(spec)
    assert report["generations"]
    assert all("LLL" in row["corporate_action_symbols"] for row in report["generations"])
    assert not report["mover"]["run"]["entries"]


def test_catalog_coverage_names_missing_roster_ticks(neutral_catalogs, tmp_path):
    from canon_trading.window_replay import run_replay

    spec, *_ = neutral_catalogs
    root = tmp_path / "empty-quotes"
    root.mkdir()
    report = run_replay({**spec, "quotes_roots": [str(root)]})
    assert report["catalog"]["quotes_read"] == 0
    missing = report["catalog"]["missing"]
    assert any(
        row["data_type"] == "QuoteTick" and row["instrument_id"] == "LLL.ALPACA" for row in missing
    )
    covered = run_replay(
        {**spec, "quotes_roots": [str(root), *spec["quotes_roots"], *spec["quotes_roots"]]}
    )
    assert covered["catalog"]["quotes_read"] == 20
    assert not any(row["data_type"] == "QuoteTick" for row in covered["catalog"]["missing"])


@pytest.mark.parametrize("handler", ["on_order_event", "on_order_released"])
def test_replay_fails_on_logged_python_handler_error(neutral_catalogs, monkeypatch, handler):
    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    # Install the stand-in in the same fresh process as the native engine/logger.
    injection = f"""
from canon_trading.mover import Mover
def broken(self, *args):
    raise RuntimeError("synthetic handler failure")
Mover.{handler} = broken
"""
    monkeypatch.setattr(window_replay, "_WORKER", injection + window_replay._WORKER)
    with pytest.raises(RuntimeError, match=f"Python {handler} failed") as failure:
        window_replay.run_replay(spec)
    assert "Traceback (most recent call last)" in str(failure.value)
    assert "synthetic handler failure" in str(failure.value)


@pytest.mark.parametrize("handler", ["on_stop", "on_dispose"])
def test_checked_engine_fails_on_lifecycle_handler_error(neutral_catalogs, monkeypatch, handler):
    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    # A stand-in without managed stopping reaches both callbacks. Mover's managed stop
    # remains RUNNING at this pin's backtest end and does not invoke its Python on_stop.
    worker = f"""
import json
import sys
from nautilus_trader.trading import Strategy
from nautilus_trader.persistence import ParquetDataCatalog
from canon_trading.window_replay import _checked_engine, add_replay_venue
class Broken(Strategy):
    def on_start(self):
        pass
    def {handler}(self):
        raise RuntimeError("synthetic lifecycle failure")
spec = json.load(sys.stdin)
with _checked_engine() as (engine, check):
    add_replay_venue(engine, "touch", spec["starting_balance"])
    for instrument in ParquetDataCatalog(spec["bars_root"]).instruments():
        engine.add_instrument(instrument)
    engine.add_strategy(Broken())
    engine.add_data(ParquetDataCatalog(spec["quotes_roots"][0]).query_quote_ticks())
    engine.run()
    check()
print(json.dumps({{"completed": True}}))
"""
    monkeypatch.setattr(window_replay, "_WORKER", worker)
    with pytest.raises(RuntimeError, match=f"Python {handler} failed") as failure:
        window_replay.run_replay(spec)
    assert "Traceback (most recent call last)" in str(failure.value)
    assert "synthetic lifecycle failure" in str(failure.value)


def test_report_keeps_non_handler_engine_errors(neutral_catalogs, monkeypatch):
    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    injection = """
from canon_trading import window_replay
from nautilus_trader.common import Logger
factory = window_replay.BacktestEngine
def logged_engine(config):
    engine = factory(config)
    Logger("ReplayTest").error("synthetic engine diagnostic")
    return engine
window_replay.BacktestEngine = logged_engine
"""
    monkeypatch.setattr(window_replay, "_WORKER", injection + window_replay._WORKER)
    report = window_replay.run_replay(spec)
    assert any(row["message"] == "synthetic engine diagnostic" for row in report["engine_errors"])
    assert all(row["level"] == "ERROR" for row in report["engine_errors"])


def test_a_universe_name_without_a_reference_stays_unknown_and_is_named(neutral_catalogs):
    """Paper's actor leaves a name with no reference unknown (movers_rank_actor.py:155-159);
    a replay keeps it out of the ranking and names it, and refuses only a roster name."""
    from canon_trading.window_replay import run_replay

    spec, *_ = neutral_catalogs
    del spec["reference_closes"]["Z0"]
    report = run_replay(spec)
    assert report["unreferenced"] == ["Z0"]
    assert report["generations"]


def test_a_roster_name_without_a_reference_is_refused(neutral_catalogs):
    from canon_trading.window_replay import run_replay

    spec, *_ = neutral_catalogs
    del spec["reference_closes"]["LLL"]
    with pytest.raises(RuntimeError, match="no usable prior-session reference for LLL.ALPACA"):
        run_replay(spec)


def test_distinct_quotes_on_one_nanosecond_are_kept_and_overlapping_copies_dropped(
    alpaca_bars, tmp_path
):
    """Recorded SIP quotes can share a timestamp and differ in size; each is its own update.
    A second root holding the same updates is a copy; one that differs is refused."""
    from canon_trading.window_replay import _quotes

    instrument = alpaca_bars.equity("LLL")
    at = ns(START)
    first, second = quote(instrument, at, 10.0, 10.02, 100), quote(instrument, at, 10.0, 10.02, 200)
    roots = []
    for name, ticks in (("a", [first, second]), ("b", [first, second]), ("c", [first, first])):
        root = tmp_path / name
        root.mkdir()
        ParquetDataCatalog(str(root)).write_quote_ticks(ticks, start=at, end=at)
        roots.append(ParquetDataCatalog(str(root)))
    kept = _quotes(roots[:2], [str(instrument.id)], at - 1, at + 1)
    assert [int(tick.ask_size) for tick in kept] == [100, 200]
    with pytest.raises(ValueError, match="conflicting catalog quotes"):
        _quotes([roots[0], roots[2]], [str(instrument.id)], at - 1, at + 1)


def test_a_reference_root_gives_the_previous_calendar_days_close(alpaca_bars, tmp_path):
    """A daily bar's ts_init is its next midnight (parse_bar); Tuesday's reference is Monday's."""
    from datetime import date

    from canon_trading.window_replay import _references

    instrument = alpaca_bars.equity("LLL")
    root = tmp_path / "daily"
    root.mkdir()
    catalog = ParquetDataCatalog(str(root))
    catalog.write_bars(
        alpaca_bars.daily(instrument, [7.0, 9.0], first=datetime(2025, 11, 7, tzinfo=NY))
    )
    found, unreferenced = _references(
        {"reference_root": str(root)}, [instrument], date(2025, 11, 11), {instrument.id}
    )
    assert found["LLL"].close == 9.0
    assert found["LLL"].session == date(2025, 11, 10)
    assert unreferenced == []


def test_a_reference_older_than_the_actors_lookback_is_unknown(alpaca_bars):
    """Paper's actor gives no reference from a bar older than REFERENCE_LOOKBACK_DAYS."""
    from datetime import date

    from canon_trading.window_replay import _references

    old, other = alpaca_bars.equity("LLL"), alpaca_bars.equity("Z0")
    spec = {
        "reference_closes": {
            "LLL": {"close": 8.0, "session": "2025-10-20"},
            "Z0": {"close": 8.0, "session": "2025-10-20"},
        }
    }
    found, unreferenced = _references(spec, [old, other], date(2025, 11, 10), set())
    assert found == {} and unreferenced == ["LLL", "Z0"]
    with pytest.raises(ValueError, match="no usable prior-session reference for LLL.ALPACA"):
        _references(spec, [old, other], date(2025, 11, 10), {old.id})


def test_a_root_holding_part_of_anothers_quotes_on_one_stamp_agrees(alpaca_bars, tmp_path):
    from canon_trading.window_replay import _quotes

    instrument = alpaca_bars.equity("LLL")
    at = ns(START)
    first, second = quote(instrument, at, 10.0, 10.02, 100), quote(instrument, at, 10.0, 10.02, 200)
    roots = []
    for name, ticks in (("a", [first, second]), ("b", [second])):
        root = tmp_path / name
        root.mkdir()
        ParquetDataCatalog(str(root)).write_quote_ticks(ticks, start=at, end=at)
        roots.append(ParquetDataCatalog(str(root)))
    kept = _quotes(roots, [str(instrument.id)], at - 1, at + 1)
    assert [int(tick.ask_size) for tick in kept] == [100, 200]
    kept = _quotes(roots[::-1], [str(instrument.id)], at - 1, at + 1)
    assert sorted(int(tick.ask_size) for tick in kept) == [100, 200]


def test_a_replay_that_never_ends_is_stopped_by_name(neutral_catalogs, monkeypatch):
    from canon_trading import window_replay

    spec, *_ = neutral_catalogs
    monkeypatch.setattr(window_replay, "_WORKER", "import time; time.sleep(30)")
    monkeypatch.setattr(window_replay, "REPLAY_TIMEOUT_FLOOR_SECS", 1)
    monkeypatch.setattr(window_replay, "REPLAY_SECS_PER_WINDOW_SEC", 0)
    with pytest.raises(RuntimeError, match="did not finish within"):
        window_replay.run_replay(spec)


def test_a_replay_error_before_the_log_check_is_the_one_reported(neutral_catalogs):
    from canon_trading.window_replay import run_replay

    spec, *_ = neutral_catalogs
    later = {
        key: (datetime.fromisoformat(spec[key]) + timedelta(days=1)).isoformat()
        for key in ("window_start", "window_end", "wind_down_at")
    }
    with pytest.raises(RuntimeError) as failed:
        run_replay({**spec, **later, "trade_date": (START + timedelta(days=1)).date().isoformat()})
    assert str(failed.value).rstrip().splitlines()[-1].startswith("ValueError")
