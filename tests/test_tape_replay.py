"""Tape replay: neutral catalogs through the pinned engine, never a broker or real state folder.

Catalog contracts: NautilusTrader d53849b216e2 persistence/__init__.pyi:161-188,232-240.
Instruments use the shared AlpacaBars.equity fixture; prices and times are synthetic.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
from collections import Counter
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeId
from nautilus_trader.model import TradeTick
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import protect
from canon_trading.backtest import FEE_RATES
from canon_trading.backtest import VENUE_SETTINGS

SECOND = 1_000_000_000
START = int(datetime(2025, 3, 11, 15, tzinfo=UTC).timestamp()) * SECOND
INTRO = (
    "The fill is not shown: the simulated venue refuses a sale of a long another strategy "
    "holds. What follows the release in this run is the protector's refusal path."
)
ENDING = (
    "Not shown by a simulation: the venue's own rules (wash-trade refusal, extended-hours order "
    "rules), the paper venue's fills, our own weight in a thin name. Our own weight in that name "
    "is not modelled, so what the tape absorbed is an upper bound."
)


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override the shared credential-environment reader: all cases are offline."""


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)


@pytest.fixture
def shape(tmp_path: Path, alpaca_bars: Any) -> dict[str, Any]:
    roots = {kind: tmp_path / kind for kind in ("quotes", "trades", "bars")}
    catalogs = {}
    for kind, path in roots.items():
        path.mkdir()
        catalogs[kind] = ParquetDataCatalog(str(path))
    instruments = [alpaca_bars.equity(symbol) for symbol in ("LLL", "MMM")]
    for catalog in catalogs.values():
        catalog.write_instruments(instruments)
    history = {}
    quotes = {}
    bids = ["20", "20", "20.01", "20.20", "20.40", "20.60", "21", "20.95"]
    bids += ["20.70"] * 18
    for instrument, width in zip(instruments, ("0.08", "0.15"), strict=True):
        symbol = instrument.id.symbol.value
        bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
        bars = []
        for i in range(14):
            at = START - (14 - i) * 60 * SECOND
            bars.append(
                Bar(
                    bar_type,
                    instrument.make_price(Decimal(20)),
                    instrument.make_price(Decimal(20) + Decimal(width)),
                    instrument.make_price(Decimal(20) - Decimal(width)),
                    instrument.make_price(Decimal(20)),
                    Quantity.from_int(200),
                    at,
                    at + 60 * SECOND,
                )
            )
        history[symbol] = bars
        catalogs["bars"].write_bars(bars)
        ticks = []
        trades = []
        for i, lll_bid in enumerate(bids):
            bid = Decimal(lll_bid if symbol == "LLL" else "20")
            at = START + i * SECOND
            ticks.append(
                QuoteTick(
                    instrument.id,
                    instrument.make_price(bid),
                    instrument.make_price(bid + Decimal("0.02")),
                    Quantity.from_int(100),
                    Quantity.from_int(100),
                    at,
                    at,
                )
            )
            trades.append(
                TradeTick(
                    instrument.id,
                    instrument.make_price(bid + Decimal("0.01")),
                    Quantity.from_int(5),
                    AggressorSide.BUY,
                    TradeId(f"neutral-{symbol}-{i}"),
                    at + SECOND // 2,
                    at + SECOND // 2,
                )
            )
        quotes[symbol] = ticks
        catalogs["quotes"].write_quote_ticks(ticks)
        catalogs["trades"].write_trade_ticks(trades)
    spec = {
        "quotes_root": str(roots["quotes"]),
        "trades_root": str(roots["trades"]),
        "bars_root": str(roots["bars"]),
        "holdings": {
            "LLL": {"buy_time": START, "quantity": 10},
            "MMM": {"buy_time": START, "quantity": 7},
        },
        "row": "OWN1",
        "fill_arm": "tape",
        "segments": [[START, START + len(bids) * SECOND]],
        "level_book_path": str(tmp_path / "levels.json"),
        "run_label": "neutral-shape",
    }
    return {"spec": spec, "history": history, "quotes": quotes}


def runner() -> Any:
    # Keep the base failures attached to each property, rather than one collection error.
    return importlib.import_module("canon_trading.tape_replay")


def holding(report: dict[str, Any], symbol: str = "LLL", segment: int = 0) -> dict[str, Any]:
    assert report["status"] == "completed", report
    return report["segments"][segment]["holdings"][symbol]


@pytest.mark.parametrize("arm", ["touch", "tape"])
def test_venue_profile_reaches_the_native_engine(shape, monkeypatch, arm) -> None:
    module = runner()
    native = module.BacktestEngine
    venues = []
    data = []

    class ObservedEngine:
        def __init__(self, config):
            self.engine = native(config)

        def __getattr__(self, name):
            return getattr(self.engine, name)

        def add_venue(self, *args, **kwargs):
            venues.append(kwargs)
            self.engine.add_venue(*args, **kwargs)

        def add_data(self, batch, **kwargs):
            data.extend(batch)
            self.engine.add_data(batch, **kwargs)

    monkeypatch.setattr(module, "BacktestEngine", ObservedEngine)
    shape["spec"]["fill_arm"] = arm
    report = module.replay(shape["spec"])
    assert report["status"] == "completed", report
    assert len(venues) == 1
    for name, expected in VENUE_SETTINGS.items():
        assert venues[0][name] == expected
        assert report["venue"][name] == expected.name
    for name, expected in {
        "bar_execution": False,
        "trade_execution": arm == "tape",
        "liquidity_consumption": arm == "tape",
        "queue_position": arm == "tape",
    }.items():
        assert venues[0][name] is expected
        assert report["venue"][name] is expected
    assert report["venue"]["fee_rates"] == {k: str(v) for k, v in FEE_RATES.items()}
    assert report["venue"]["profile"] == "alpaca-paper-sim-v1"
    assert bool(report["venue"]["departures"]) is (arm == "touch")
    assert venues[0]["fill_model"] is None
    assert venues[0]["latency_model"] is None
    assert report["venue"]["fill_model"] == "DefaultFillModel"
    assert report["venue"]["latency_ns"] == 0
    assert report["venue"]["starting_balances"] == ["100000 USD"]
    assert any(isinstance(item, TradeTick) for item in data) is (arm == "tape")
    assert report["segments"][0]["data_counts"]["trades"] == (52 if arm == "tape" else 0)
    assert all(
        Decimal(event["commission"].split()[0]) == 0 for event in holding(report)["owner_fills"]
    )


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
def test_arms_from_each_symbols_history_using_public_row_arithmetic(shape, row) -> None:
    shape["spec"]["row"] = row
    report = runner().replay(shape["spec"])
    distances = []
    for symbol in ("LLL", "MMM"):
        arm = holding(report, symbol)["arming"][0]
        inputs = arm["inputs"]
        expected, plan = protect.distance_for(
            symbol,
            row,
            Decimal(inputs["bid"]),
            Decimal(inputs["ask"]),
            shape["history"][symbol],
        )
        assert Decimal(arm["distance"]) == expected
        assert inputs["bars"] == 14
        assert Decimal(inputs["atr"]) == plan.plan.atr
        assert not inputs["fallback"]
        assert inputs["bar_stamps"] == [bar.ts_event for bar in shape["history"][symbol]]
        distances.append(expected)
    assert distances[0] != distances[1], "one symbol's history was reused for another"


def test_f2_records_the_protectors_own_config_refusal_without_an_engine(shape, monkeypatch) -> None:
    module = runner()
    monkeypatch.setattr(module, "BacktestEngine", lambda *_a, **_k: pytest.fail("engine built"))
    shape["spec"]["row"] = "F2"
    report = module.replay(shape["spec"])
    assert report["status"] == "refused"
    assert report["segments"] == []
    assert report["refusals"][0]["source"] == "OwnerProtectorConfig"
    assert "OwnerProtector refuses F2" in report["refusals"][0]["reason"]


def test_trail_never_lowers_and_release_is_the_crossing_quote(shape) -> None:
    row = holding(runner().replay(shape["spec"]))
    changes = row["level_changes"]
    assert changes
    assert all(Decimal(change["new"]) > Decimal(change["old"]) for change in changes)
    levels = [Decimal(state["level"]) for state in row["states"] if state["level"] is not None]
    assert levels == sorted(levels)
    (release,) = row["releases"]
    assert release["at"] == START + 8 * SECOND
    assert Decimal(release["quantity"]) == 10
    assert Decimal(release["price"]) > 0
    before = [change for change in changes if change["at"] < release["at"]][-1]
    assert shape["quotes"]["LLL"][7].bid_price.as_decimal() > Decimal(before["new"])
    assert shape["quotes"]["LLL"][8].bid_price.as_decimal() <= Decimal(before["new"])
    updated = [event for event in row["order_events"] if event["event"] == "OrderUpdated"]
    assert updated and any(event.get("trigger_price") == before["new"] for event in updated)


def test_refused_sale_retries_at_chase_interval_and_other_holding_stays_armed(shape) -> None:
    report = runner().replay(shape["spec"])
    row = holding(report)
    release = row["releases"][0]
    refusals = row["refusals"]
    assert len(refusals) >= 3
    assert all("Short selling not permitted" in event["reason"] for event in refusals)
    assert refusals[0]["at"] == release["at"]
    times = [event["at"] for event in refusals]
    assert [b - a for a, b in pairwise(times)] == [protect.PLANNER.chase_secs * SECOND] * (
        len(times) - 1
    )
    sent = [event for event in row["order_events"] if event["event"] == "OrderSubmitted"]
    assert [event["at"] for event in sent] == times
    assert len({event["client_order_id"] for event in sent}) == len(sent)
    assert not [event for event in row["order_events"] if event["event"] == "OrderFilled"]
    expected = Counter(
        line["text"] for line in report["segments"][0]["lines"] if line["symbol"] == "LLL"
    )
    assert {line["text"]: line["count"] for line in row["line_counts"]} == expected
    assert row["alert_count"] == sum(count for text, count in expected.items() if "ALERT" in text)
    stalled = [state for state in row["states"] if "sale stalled" in (state["reason"] or "")]
    assert stalled and all(state["state"] == "frozen" for state in stalled)
    other = holding(report, "MMM")
    armed = [state for state in other["states"] if state["state"] == "armed"]
    assert armed and len({state["level"] for state in armed}) == 1
    assert not other["releases"] and not other["refusals"]
    assert any("ALERT-once" in limit and "not reachable" in limit for limit in report["limits"])


def test_seam_restarts_from_last_written_level_distance_offset_with_zero_guard(shape) -> None:
    shape["spec"]["segments"] = [
        [START, START + 6 * SECOND],
        [START + 6 * SECOND, START + 8 * SECOND],
    ]
    report = runner().replay(shape["spec"])
    one, two = report["segments"]
    saved = next(
        entry for entry in one["book"]["entries"] if entry["instrument_id"] == "LLL.ALPACA"
    )
    row = holding(report, segment=1)
    arm = row["arming"][0]
    assert arm["at"] == START + 6 * SECOND
    assert (arm["level"], arm["distance"], arm["limit_offset"]) == (
        saved["last_level"],
        saved["distance"],
        saved["limit_offset"],
    )
    assert arm["guard_count"] == 0
    assert all(
        Decimal(state["level"]) >= Decimal(saved["last_level"])
        for state in row["states"]
        if state["level"] is not None
    )
    for segment in (one, two):
        assert all(
            segment["start"] <= state["at"] < segment["end"]
            for state in segment["holdings"]["LLL"]["states"]
        )
    assert [event["at"] for event in row["owner_fills"]] == [START + 6 * SECOND]
    assert any("bought again" in limit for limit in report["limits"])
    assert any("identity" in limit for limit in report["limits"])


def test_gain_lock_records_the_protectors_events_and_release_above_cost_floor(shape) -> None:
    row = holding(runner().replay(shape["spec"]))
    gain = row["gain_lock_events"][0]
    assert gain["event"] == "armed"
    assert gain["at"] > row["arming"][0]["at"]
    floor = Decimal(gain["entry"]) + Decimal(gain["round_trip_cost"])
    assert Decimal(gain["floor"]) >= floor
    assert Decimal(row["releases"][0]["price"]) >= Decimal(gain["floor"])


def test_full_run_and_cli_reports_need_no_socket_or_alpaca_environment(
    shape, tmp_path, monkeypatch
) -> None:
    def offline(*_args, **_kwargs):
        pytest.fail("socket.socket.connect was called")

    monkeypatch.setattr(socket.socket, "connect", offline)
    for name in tuple(os.environ):
        if name.startswith("ALPACA_"):
            monkeypatch.delenv(name)
    spec = tmp_path / "spec.json"
    report = tmp_path / "report.json"
    spec.write_text(json.dumps(shape["spec"]))
    assert runner().main(["--spec", str(spec), "--report", str(report)]) == 0
    result = json.loads(report.read_text())
    assert holding(result)["refusals"]
    assert result["introduction"] == INTRO and result["limitations"] == ENDING
    rendered = report.with_suffix(".md").read_text()
    assert rendered.startswith(INTRO + "\n\n")
    assert rendered.rstrip().endswith(ENDING)
    assert "Short selling not permitted" in rendered
    assert "OrderReleased" in rendered
    native = result["segments"][0]["engine_reports"]
    assert set(native) == {"orders", "order_fills", "fills", "positions"}
    assert native["orders"] and native["positions"]
    assert {row["side"] for row in native["orders"]} == {"BUY", "SELL"}
    assert {row["order_side"] for row in native["fills"]} == {"BUY"}
    assert {row["strategy_id"] for row in native["positions"]} == {"EXTERNAL"}
    assert all(row["client_order_id"] for row in native["order_fills"])
    assert all(row["ts_event"] for row in native["fills"])
    assert "### Native engine reports" in rendered
    assert "Regulatory" in rendered


def test_default_arm_is_tape_and_default_booking_is_foreign(shape) -> None:
    del shape["spec"]["fill_arm"]
    result = runner().replay(shape["spec"])
    assert result["status"] == "completed", result
    assert result["fill_arm"] == "tape"
    assert result["booking"] == "foreign"
    assert result["venue"]["queue_position"] is True
    assert result["venue"]["departures"] == []


def decisions(report: dict[str, Any], cutoff: int) -> dict[str, Any]:
    """Compare decisions, not random order IDs, through the foreign release's timestamp."""
    segment = report["segments"][0]
    values = {}
    for symbol, row in segment["holdings"].items():
        for field, keys in (
            ("arming", ("at", "level", "distance", "limit_offset")),
            ("level_changes", ("at", "old", "new")),
            ("releases", ("at", "price", "quantity")),
        ):
            values[f"{symbol}.{field}"] = [
                {key: event[key] for key in keys} for event in row[field] if event["at"] <= cutoff
            ]
        releases = [event for event in row["releases"] if event["at"] <= cutoff]
        lines_through_release = (
            segment["lines"][: releases[-1]["lines_seen"]] if releases else segment["lines"]
        )
        lines = Counter(
            line["text"]
            for line in lines_through_release
            if line["symbol"] == symbol and line["at"] <= cutoff
        )
        values[f"{symbol}.holding_lines"] = dict(lines)
        values[f"{symbol}.ALERT_count"] = sum(n for text, n in lines.items() if "ALERT" in text)
    return values


class BookingNotEquivalent(AssertionError):
    """Only the measured decision mismatch is the stopped property, not setup or engine errors."""


@pytest.mark.xfail(
    strict=True,
    raises=BookingNotEquivalent,
    reason="Stopped: the base protector discovers only EXTERNAL inventory; own never arms",
)
@pytest.mark.parametrize("quantity", [10, 200])
def test_own_booking_matches_foreign_through_release_and_fills_in_parts(
    shape, tmp_path, quantity
) -> None:
    module = runner()
    spec = shape["spec"]
    spec["fill_arm"] = "tape"
    spec["holdings"]["LLL"]["quantity"] = quantity
    reports = {}
    for booking in ("foreign", "own"):
        spec["booking"] = booking
        spec["level_book_path"] = str(tmp_path / f"{booking}-levels.json")
        spec_path = tmp_path / f"{booking}-spec.json"
        report_path = tmp_path / f"{booking}-report.json"
        spec_path.write_text(json.dumps(spec))
        assert module.main(["--spec", str(spec_path), "--report", str(report_path)]) == 0
        reports[booking] = json.loads(report_path.read_text())
    foreign, own = reports["foreign"], reports["own"]
    assert own["counted_proof"] is False
    assert own["introduction"] == (
        "The buy was booked under the protector's ID so that the simulated venue would fill its sale; "
        "up to the release its decisions equal the foreign run's."
    )
    assert "hypothesis, not a result" in own["equivalence"]
    cutoff = holding(foreign)["releases"][0]["at"]
    before, after = decisions(foreign, cutoff), decisions(own, cutoff)
    differences = [
        {"field": key, "foreign": value, "own": after[key]}
        for key, value in before.items()
        if value != after[key]
    ]
    comparison = {"cutoff": cutoff, "equivalent": not differences, "differences": differences}
    (tmp_path / "comparison.json").write_text(json.dumps(comparison, indent=2))
    assert {p["strategy_id"] for p in own["segments"][0]["engine_reports"]["positions"]} == {
        "OwnerProtector-001"
    }
    if differences:
        raise BookingNotEquivalent(json.dumps(differences, indent=2))
    fills = [
        event
        for event in own["segments"][0]["engine_reports"]["fills"]
        if event["order_side"] == "SELL" and event["instrument_id"] == "LLL.ALPACA"
    ]
    assert fills and all(
        event["ts_event"] and event["last_px"] and event["last_qty"] for event in fills
    )
    if quantity == 200:
        # This segment lies in one minute: all its synthetic prints total 26 * 5 = 130.
        assert quantity > len(shape["quotes"]["LLL"]) * 5
        assert len(fills) >= 2
        sends = [
            event for event in holding(own)["order_events"] if event["event"] == "OrderSubmitted"
        ]
        assert len(sends) >= 2


@pytest.mark.parametrize("problem", ["overlap", "gap", "missing definition", "fill arm", "booking"])
def test_bad_specs_are_refused_by_name_before_building_any_engine(
    shape, monkeypatch, problem
) -> None:
    module = runner()
    monkeypatch.setattr(module, "BacktestEngine", lambda *_a, **_k: pytest.fail("engine built"))
    spec = shape["spec"]
    if problem in ("overlap", "gap"):
        offset = -1 if problem == "overlap" else 1
        spec["segments"] = [
            [START, START + 5 * SECOND],
            [START + (5 + offset) * SECOND, START + 9 * SECOND],
        ]
    elif problem == "missing definition":
        del spec["holdings"]["LLL"]
        spec["holdings"]["NNN"] = {"buy_time": START, "quantity": 1}
    elif problem == "fill arm":
        spec["fill_arm"] = "unknown"
    else:
        spec["booking"] = "unknown"
    report = module.replay(spec)
    assert report["status"] == "refused"
    assert report["segments"] == []
    assert problem in report["refusals"][0]["reason"].lower()


@pytest.mark.parametrize(
    "text",
    [
        "LLL.ALPACA: armed distance 0.1600",
        "ALERT: LLL.ALPACA: our protective sell was refused by the venue",
        "LLL.ALPACA: ALERT: the session day's bars read failed",
    ],
)
def test_a_line_is_filed_under_the_instrument_it_names_wherever_it_names_it(text):
    from types import SimpleNamespace

    from nautilus_trader.model import InstrumentId

    module = runner()
    ids = [InstrumentId.from_str("LLL.ALPACA"), InstrumentId.from_str("LL.ALPACA")]
    observer = SimpleNamespace(
        trace=SimpleNamespace(ids=ids, lines=[]),
        clock=SimpleNamespace(timestamp_ns=lambda: 7),
    )
    module._RecordedProtector._say(observer, text)
    assert observer.trace.lines == [{"at": 7, "symbol": "LLL", "text": text}]
