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


def test_a_spec_latency_reaches_each_segment_venue_counted_from_send(shape, monkeypatch) -> None:
    from canon_trading.send_latency import SendLatency

    module = runner()
    native = module.BacktestEngine
    models = []

    class ObservedEngine:
        def __init__(self, config):
            self.engine = native(config)

        def __getattr__(self, name):
            return getattr(self.engine, name)

        def add_venue(self, *args, **kwargs):
            models.append(kwargs["latency_model"])
            self.engine.add_venue(*args, **kwargs)

    monkeypatch.setattr(module, "BacktestEngine", ObservedEngine)
    latency = {"insert_ns": 5_000_000, "update_ns": 7_000_000, "delete_ns": 7_000_000}
    shape["spec"]["latency"] = latency
    report = module.replay(shape["spec"])
    assert report["status"] == "completed", report
    assert models and all(isinstance(model, SendLatency) for model in models)
    assert len({id(model) for model in models}) == len(models)  # one model per segment's engine
    assert [(m.insert_ns, m.update_ns, m.delete_ns) for m in models] == [
        (5_000_000, 7_000_000, 7_000_000)
    ] * len(models)
    venue = report["venue"]
    assert venue["latency"] == {
        "model": "SendLatency",
        "kind": "counted from send",
        **latency,
    }
    assert "latency_ns" not in venue
    assert "latency_model" not in venue
    assert venue["departures"] == [
        (
            "latency: SendLatency, counted from send "
            "(insert 5000000, update 7000000, delete 7000000 ns); v1 has none"
        )
    ]
    assert not any("Zero latency" in limit for limit in report["limits"])
    assert any("in flight at a segment's end" in limit for limit in report["limits"])
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    "latency",
    [
        {"insert_ns": 5},
        {"insert_ns": 5, "update_ns": 7, "delete_ns": 7, "base_ns": 0},
        {"insert_ns": -1, "update_ns": 7, "delete_ns": 7},
        {"insert_ns": 5.0, "update_ns": 7, "delete_ns": 7},
        {"insert_ns": True, "update_ns": 7, "delete_ns": 7},
        {"insert_ns": 2**64, "update_ns": 7, "delete_ns": 7},
        {"insert_ns": 60_000_000_001, "update_ns": 7, "delete_ns": 7},
        [5, 7, 7],
    ],
)
def test_a_malformed_latency_is_refused_before_any_engine(shape, latency) -> None:
    shape["spec"]["latency"] = latency
    report = runner().replay(shape["spec"])
    assert report["status"] == "refused"
    assert "latency" in report["refusals"][0]["reason"]


def test_send_latency_refuses_a_command_handed_in_before_it_was_made() -> None:
    from types import SimpleNamespace

    from canon_trading.send_latency import SendLatency

    model = SendLatency(5, 7, 9)
    command = SimpleNamespace(ts_init=1_000)
    assert model.get_insert_latency(command, 1_000) == 5
    assert model.get_insert_latency(command, 1_250) == 255
    assert model.get_update_latency(command, 1_250) == 257
    assert model.get_delete_latency(command, 1_250) == 259
    with pytest.raises(ValueError, match="before the command's ts_init"):
        model.get_insert_latency(command, 999)


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


SEAM = START + 6 * SECOND


def two_segments(shape) -> dict[str, Any]:
    shape["spec"]["segments"] = [[START, SEAM], [SEAM, START + 8 * SECOND]]
    return shape["spec"]


def saved_entry(report: dict[str, Any], symbol: str) -> dict[str, Any]:
    return next(
        entry
        for entry in report["segments"][0]["book"]["entries"]
        if entry["instrument_id"] == f"{symbol}.ALPACA"
    )


def test_a_scripted_sell_out_at_the_seam_is_a_new_holding_and_carries_no_level(shape) -> None:
    # The venue shows LLL sold out and bought back at the seam, which the engine never did;
    # MMM has no scripted fill. The verdict runs both ways in one replay.
    spec = two_segments(shape)
    spec["venue_fills"] = {
        "LLL": [
            {"side": "sell", "quantity": 10, "filled_at": SEAM},
            {"side": "buy", "quantity": 10, "filled_at": SEAM + 1000},
        ]
    }
    report = runner().replay(spec)
    lll = holding(report, "LLL", segment=1)["arming"][0]
    assert lll["level"] != saved_entry(report, "LLL")["last_level"]
    lines = [line["text"] for line in report["segments"][1]["lines"] if line["symbol"] == "LLL"]
    assert any("not carried" in text and "a new holding" in text for text in lines), lines
    mmm = holding(report, "MMM", segment=1)["arming"][0]
    saved = saved_entry(report, "MMM")
    assert (mmm["level"], mmm["distance"], mmm["limit_offset"]) == (
        saved["last_level"],
        saved["distance"],
        saved["limit_offset"],
    )


def test_the_venue_reader_answers_as_the_venue_would_for_a_persisted_holding(shape) -> None:
    module = runner()
    spec = two_segments(shape)
    spec["venue_fills"] = {
        "LLL": [
            {"side": "buy", "quantity": 10, "filled_at": SEAM + 2000},
            {"side": "sell", "quantity": 10, "filled_at": SEAM},
        ]
    }
    checked = module._checked(spec, Path.cwd())
    reader = module._VenueReader(checked)
    now = {"ns": START - SECOND}

    class Clock:
        def timestamp_ns(self) -> int:
            return now["ns"]

    reader.protector = type("Engine", (), {"clock": Clock()})()
    assert reader.get("/clock") == {"timestamp": "2025-03-11T14:59:59+00:00"}
    with pytest.raises(ValueError, match="no position in LLL"):
        reader.get("/positions/LLL")
    sells = {"symbols": "LLL", "side": "sell", "after": "2025-03-10T15:00:00+00:00"}
    now["ns"] = SEAM - 1000
    assert reader.get("/positions/LLL") == {"symbol": "LLL", "qty": "10"}
    assert reader.get("/orders", sells) == []
    now["ns"] = SEAM
    # Flat between the sell and the buy-back: no position, as get_open_position raises.
    with pytest.raises(ValueError, match="no position in LLL"):
        reader.get("/positions/LLL")
    (order,) = reader.get("/orders", sells)
    assert (order["side"], order["filled_qty"], order["filled_at"]) == (
        "sell",
        "10",
        "2025-03-11T15:00:06+00:00",
    )
    assert reader.get("/orders", {**sells, "status": "open"}) == []
    assert reader.get("/orders", {**sells, "until": "2025-03-11T15:00:05+00:00"}) == []
    now["ns"] = SEAM + 2000
    assert reader.get("/positions/LLL") == {"symbol": "LLL", "qty": "10"}
    both = {"symbols": "LLL", "status": "all"}
    assert [o["side"] for o in reader.get("/orders", both)] == ["buy", "sell"]
    assert [o["side"] for o in reader.get("/orders", {**both, "direction": "asc"})] == [
        "sell",
        "buy",
    ]
    assert [o["side"] for o in reader.get("/orders", {**both, "limit": 1})] == ["buy"]
    assert reader.get("/positions/MMM") == {"symbol": "MMM", "qty": "7"}
    with pytest.raises(ValueError, match="cannot answer /account"):
        reader.get("/account")


def test_unread_entries_are_the_levels_each_segment_wrote_without_a_reading() -> None:
    def segment(index: int, *entries: dict[str, Any]) -> dict[str, Any]:
        return {"index": index, "book": {"entries": list(entries)}}

    unread = {"instrument_id": "LLL.ALPACA", "run": "r:1", "last_level": "20.44"}
    report = runner()._unread_entries(
        [
            segment(1, unread, {**unread, "instrument_id": "MMM.ALPACA", "horizon_ns": 1}),
            segment(
                2,
                unread,  # left by segment 1: not segment 2's
                {**unread, "run": "r:2", "last_level": None},  # no level: carries nothing
                {**unread, "run": "old:1"},  # an earlier replay's entry in a reused book
                {**unread, "instrument_id": "MMM.ALPACA", "run": "r:2"},
            ),
        ],
        "r",
    )
    assert [(row["segment"], row["instrument_id"]) for row in report] == [
        (1, "LLL.ALPACA"),
        (2, "MMM.ALPACA"),
    ]


def test_the_report_names_each_seam_crossing_and_each_entry_without_a_reading(shape) -> None:
    report = runner().replay(two_segments(shape))
    crossings = {row["symbol"]: row for row in report["seams"]}
    assert set(crossings) == {"LLL", "MMM"}
    for symbol, row in crossings.items():
        first = holding(report, symbol)["owner_fills"][0]
        again = holding(report, symbol, segment=1)["owner_fills"][0]
        assert (row["segment"], row["opened_at"], row["opened_price"]) == (
            2,
            first["at"],
            first["last_px"],
        )
        assert (row["rebought_at"], row["rebought_price"]) == (again["at"], again["last_px"])
        assert "artefact" in row
    for row in report["entries_without_reading"]:
        book = report["segments"][row["segment"] - 1]["book"]["entries"]
        (entry,) = [e for e in book if e["instrument_id"] == row["instrument_id"]]
        assert entry.get("horizon_ns") is None and entry["run"] == f"neutral-shape:{row['segment']}"
    rendered = runner().render_markdown(report)
    assert "## Seams (the re-buy is an artefact of the replay)" in rendered
    assert "## Book entries with no venue reading" in rendered


@pytest.mark.parametrize(
    ("fills", "message"),
    [
        ({"ZZZ": [{"side": "sell", "quantity": 1, "filled_at": SEAM}]}, "not a holding"),
        ({"LLL": [{"side": "short", "quantity": 1, "filled_at": SEAM}]}, "buy or sell"),
        ({"LLL": [{"side": "sell", "quantity": 0.5, "filled_at": SEAM}]}, "whole shares"),
        ({"LLL": [{"side": "sell", "quantity": 1}]}, "side, quantity and filled_at"),
        ({"LLL": [{"side": "sell", "quantity": 4, "filled_at": SEAM}]}, "net to zero"),
        (
            {
                "LLL": [
                    {"side": "sell", "quantity": 20, "filled_at": SEAM},
                    {"side": "buy", "quantity": 20, "filled_at": SEAM + 1000},
                ]
            },
            "sell more than the holding holds",
        ),
        ({"LLL": [{"side": "sell", "quantity": 1, "filled_at": SEAM - SECOND}]}, "first seam"),
        ({"LLL": [{"side": "sell", "quantity": 1, "filled_at": SEAM + 1}]}, "whole microsecond"),
    ],
)
def test_a_malformed_venue_fill_is_refused_before_any_engine(shape, fills, message) -> None:
    spec = two_segments(shape)
    spec["venue_fills"] = fills
    report = runner().replay(spec)
    assert report["status"] == "refused"
    assert report["refusals"][0]["source"] == "spec"
    assert message in report["refusals"][0]["reason"]


def test_gain_lock_records_the_protectors_events_and_release_above_cost_floor(shape) -> None:
    # The gain lock is off unless asked for (OwnerProtectorConfig.gain_lock); the spec asks for
    # it, as the gain-lock files do, and the builder passes it to the protector's config.
    module = runner()
    shape["spec"]["gain_lock"] = True
    report = module.replay(shape["spec"])
    assert report["status"] == "completed", report
    assert report["gain_lock"] is True
    row = holding(report)
    gain = row["gain_lock_events"][0]
    assert gain["event"] == "armed"
    assert gain["at"] > row["arming"][0]["at"]
    floor = Decimal(gain["entry"]) + Decimal(gain["round_trip_cost"])
    assert Decimal(gain["floor"]) >= floor
    assert Decimal(row["releases"][0]["price"]) >= Decimal(gain["floor"])


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [], {}])
def test_a_non_boolean_gain_lock_is_refused_by_name_before_any_engine(
    shape, monkeypatch, value
) -> None:
    module = runner()
    monkeypatch.setattr(module, "BacktestEngine", lambda *_a, **_k: pytest.fail("engine built"))
    shape["spec"]["gain_lock"] = value
    report = module.replay(shape["spec"])
    assert report["status"] == "refused"
    assert report["segments"] == []
    assert report["refusals"][0]["source"] == "spec"
    reason = report["refusals"][0]["reason"]
    assert "gain_lock" in reason and "boolean" in reason


@pytest.mark.parametrize(("given", "expected"), [(True, True), (False, False), ("absent", False)])
def test_the_builder_passes_gain_lock_to_the_protectors_config(shape, given, expected) -> None:
    module = runner()
    spec = dict(shape["spec"])
    if given != "absent":
        spec["gain_lock"] = given
    assert module._config(module._checked(spec, Path.cwd()), 0).gain_lock is expected


def test_an_absent_gain_lock_is_false_and_replays_as_it_does_today(shape) -> None:
    # No gain_lock key: the lock is off and the replay's decisions are the ones it had before
    # the key existed; the report now names the lock's state, which a keyless report did not.
    module = runner()
    assert "gain_lock" not in shape["spec"]
    report = module.replay(shape["spec"])
    assert report["status"] == "completed", report
    assert report["gain_lock"] is False
    row = holding(report)
    assert not row["gain_lock_events"]
    (release,) = row["releases"]
    assert release["at"] == START + 8 * SECOND
    assert Decimal(release["quantity"]) == 10


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


@pytest.mark.parametrize(
    ("held", "text"),
    [
        (["LL.ALPACA"], "ALERT: LLL.ALPACA: our protective sell was refused by the venue"),
        (["LL.ALPACA"], "LLL.ALPACA: armed distance 0.1600"),
        (["MMM.ALPACA"], "level book write failed: OSError: MMM.ALPACA: denied"),
        (["MMM.ALPACA"], "ALERT: previous level book unreadable: ValueError: bad"),
    ],
)
def test_a_line_naming_no_held_instrument_at_its_start_is_filed_under_none(held, text):
    from types import SimpleNamespace

    from nautilus_trader.model import InstrumentId

    module = runner()
    observer = SimpleNamespace(
        trace=SimpleNamespace(ids=[InstrumentId.from_str(i) for i in held], lines=[]),
        clock=SimpleNamespace(timestamp_ns=lambda: 7),
    )
    module._RecordedProtector._say(observer, text)
    assert observer.trace.lines == [{"at": 7, "symbol": None, "text": text}]
    assert module._unfiled_alerts(observer.trace.lines) == ("ALERT" in text)
