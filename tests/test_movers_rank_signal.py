"""The movers ranking's signal value: one string, best first, that the ranking actor publishes and the mover
strategy parses, built and read by the one shared function pair, and carried by upstream's message bus.
Offline; one backtest engine with no data and no venue."""

from __future__ import annotations

import math
import random
from typing import cast

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.backtest import BacktestEngineConfig
from nautilus_trader.common import DataActor
from nautilus_trader.model import InstrumentId

from canon_trading.movers_rank_signal import MAX_ENTRIES
from canon_trading.movers_rank_signal import SIGNAL_NAME
from canon_trading.movers_rank_signal import RankEntry
from canon_trading.movers_rank_signal import decode_ranking
from canon_trading.movers_rank_signal import encode_ranking

EXAMPLE = "NXXT.ALPACA=9.5094;NCT.ALPACA=1.3639"


def entry(symbol: str, move: float) -> RankEntry:
    return RankEntry(InstrumentId.from_str(f"{symbol}.ALPACA"), move)


def test_the_signal_name_is_movers_rank() -> None:
    assert SIGNAL_NAME == "movers-rank"


def test_the_example_is_built_and_read_back_exactly() -> None:
    entries = (entry("NXXT", 9.5094), entry("NCT", 1.3639))
    assert encode_ranking(entries) == EXAMPLE
    assert decode_ranking(EXAMPLE) == entries


def test_order_is_kept_best_first_as_given() -> None:
    entries = (entry("AAA", 0.1), entry("BBB", 5.0), entry("CCC", 0.2))
    assert decode_ranking(encode_ranking(entries)) == entries


def test_a_symbol_with_a_dot_round_trips() -> None:
    entries = (entry("BRK.B", 0.0312),)
    assert encode_ranking(entries) == "BRK.B.ALPACA=0.0312"
    assert decode_ranking("BRK.B.ALPACA=0.0312") == entries


def test_an_empty_ranking_is_the_empty_string() -> None:
    assert encode_ranking(()) == ""
    assert decode_ranking("") == ()


def test_random_moves_round_trip_exactly() -> None:
    rng = random.Random(20261111)
    for _ in range(500):
        n = rng.randint(1, MAX_ENTRIES)
        entries = tuple(entry(f"S{i}", rng.uniform(-1.0, 50.0)) for i in range(n))
        value = encode_ranking(entries)
        assert decode_ranking(value) == entries
        assert encode_ranking(decode_ranking(value)) == value


def test_at_most_twenty_entries() -> None:
    assert MAX_ENTRIES == 20
    entries = tuple(entry(f"S{i}", 1.0) for i in range(MAX_ENTRIES + 1))
    with pytest.raises(ValueError, match="21 entries"):
        encode_ranking(entries)
    with pytest.raises(ValueError, match="21 entries"):
        decode_ranking(";".join(f"S{i}.ALPACA=1.0" for i in range(MAX_ENTRIES + 1)))


@pytest.mark.parametrize("move", [math.nan, math.inf, -math.inf])
def test_a_move_that_is_not_finite_is_refused(move: float) -> None:
    with pytest.raises(ValueError, match="not finite"):
        entry("AAA", move)


@pytest.mark.parametrize("text", ["nan", "inf", "-inf"])
def test_a_value_with_a_move_that_is_not_finite_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not finite"):
        decode_ranking(f"AAA.ALPACA={text}")


def test_a_repeated_instrument_is_refused() -> None:
    with pytest.raises(ValueError, match="AAA.ALPACA twice"):
        encode_ranking((entry("AAA", 1.0), entry("AAA", 2.0)))
    with pytest.raises(ValueError, match="AAA.ALPACA twice"):
        decode_ranking("AAA.ALPACA=1.0;AAA.ALPACA=2.0")


def test_another_venue_is_refused() -> None:
    with pytest.raises(ValueError, match="not ALPACA"):
        decode_ranking("AAA.XNAS=1.0")


@pytest.mark.parametrize(
    "value",
    [
        "AAA.ALPACA",  # no move
        "AAA.ALPACA=",  # empty move
        "=1.0",  # no instrument
        "AAA.ALPACA=1.0;",  # a trailing separator
        ";AAA.ALPACA=1.0",  # a leading separator
        "AAA.ALPACA=1.0;;BBB.ALPACA=2.0",  # an empty entry
        "AAA.ALPACA=1.50",  # not the shortest form of the move
        "AAA.ALPACA=+1.5",
        "AAA.ALPACA=1e3",
        "AAA.ALPACA= 1.5",
        " AAA.ALPACA=1.5",
        "AAA.ALPACA=one",
        "AAA.ALPACA=1.5=2",
        "AAA=1.5",  # no venue
    ],
)
def test_a_malformed_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        decode_ranking(value)


def test_an_instrument_id_holding_whitespace_is_refused() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        RankEntry(InstrumentId.from_str(" AAA.ALPACA"), 1.0)
    with pytest.raises(ValueError, match="whitespace"):
        decode_ranking(" AAA.ALPACA=1.5")


@pytest.mark.parametrize("symbol", ["A\x00B", "A\x07B"])
def test_a_symbol_holding_a_control_character_is_refused(symbol: str) -> None:
    with pytest.raises(ValueError, match="control"):
        RankEntry(InstrumentId.from_str(f"{symbol}.ALPACA"), 1.0)


@pytest.mark.parametrize("symbol", ["A;B", "A=B"])
def test_a_symbol_holding_a_separator_is_refused(symbol: str) -> None:
    with pytest.raises(ValueError, match="separator"):
        RankEntry(InstrumentId.from_str(f"{symbol}.ALPACA"), 1.0)


def publish_and_read(value: str, ts_event: int = 7) -> list[tuple[str, int]]:
    """One actor publishes `value` with `publish_signal`; another, subscribed by `subscribe_signal` before it,
    records each signal's value and ts_event. Actors start in the order added (observed at v2.0.0rc8: a
    strategy added before the publisher but started after it got nothing)."""
    got: list[tuple[str, int]] = []

    class Publisher(DataActor):
        def on_start(self) -> None:
            self.publish_signal(SIGNAL_NAME, value, ts_event)

    class Reader(DataActor):
        def on_start(self) -> None:
            self.subscribe_signal(SIGNAL_NAME)

        def on_signal(
            self, signal
        ) -> None:  # upstream's Signal class is not exported to Python here
            got.append((signal.value, signal.ts_event))

    engine = BacktestEngine(BacktestEngineConfig())
    try:
        engine.add_actor(Reader())
        engine.add_actor(Publisher())
        engine.run()
    finally:
        engine.dispose()
    return got


def test_the_value_crosses_upstreams_message_bus_unchanged() -> None:
    entries = (entry("NXXT", 9.5094), entry("NCT", 1.3639))
    got = publish_and_read(encode_ranking(entries), 1_794_418_620_000_000_000)
    assert got == [(EXAMPLE, 1_794_418_620_000_000_000)]
    assert decode_ranking(got[0][0]) == entries


@pytest.mark.parametrize("move", [1, True, 2**53 + 1, 10**400, "1.5", None])
def test_a_move_that_is_not_a_float_is_refused_when_built(move: object) -> None:
    with pytest.raises(TypeError, match="not a float"):
        RankEntry(InstrumentId.from_str("AAA.ALPACA"), cast("float", move))


def test_an_entry_that_could_not_cross_the_bus_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="not ALPACA"):
        RankEntry(InstrumentId.from_str("AAA.XNAS"), 1.0)
    with pytest.raises(ValueError, match="not finite"):
        RankEntry(InstrumentId.from_str("AAA.ALPACA"), math.nan)
    with pytest.raises(TypeError, match="not an InstrumentId"):
        RankEntry(cast("InstrumentId", "AAA.ALPACA"), 1.0)


@pytest.mark.parametrize("count", [0, MAX_ENTRIES])
def test_an_empty_and_a_full_ranking_cross_the_bus(count: int) -> None:
    entries = tuple(entry(f"S{i}", 0.5 + i) for i in range(count))
    assert publish_and_read(encode_ranking(entries)) == [(encode_ranking(entries), 7)]
    assert decode_ranking(encode_ranking(entries)) == entries
