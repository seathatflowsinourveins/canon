"""Unit 4: rebuild a carried long's stop from shaped minute closes, without external state.

Real Bar/BarType/Price/Quantity objects follow NautilusTrader d53849b216e2:
crates/model/src/python/data/bar.rs:326-330,401-405,431-454,478-524;
crates/model/src/python/types/price.rs:455-459,518-522; types/quantity.rs:463-467.
All shaped data stays in memory; pytest's shared holdout fixture uses --basetemp.
"""

import builtins
import io
import os
import socket
import time
from collections.abc import Sequence
from decimal import ROUND_CEILING
from decimal import Decimal
from decimal import Inexact
from decimal import Rounded
from decimal import localcontext
from itertools import pairwise
from pathlib import Path
from typing import NoReturn

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

import canon_trading.rebuilt_level as rebuilding
from canon_trading.rebuilt_level import rebuilt_level
from canon_trading.rebuilt_level import rebuilt_level_from_bars

MINUTE = 60_000_000_000
INVALID = ("0", "-0", "-1", "NaN", "sNaN", "Infinity", "-Infinity")


def _bar(
    close: str,
    ts_event: int,
    *,
    high: str | None = None,
    instrument: str = "AAA.NASDAQ",
    spec: str = "1-MINUTE-LAST",
    ts_init: int | None = None,
) -> Bar:
    price = Price.from_str(close)
    return Bar(
        bar_type=BarType.from_str(f"{instrument}-{spec}-EXTERNAL"),
        open=price,
        high=price if high is None else Price.from_str(high),
        low=price,
        close=price,
        volume=Quantity.from_str("1"),
        ts_event=ts_event,
        ts_init=ts_event if ts_init is None else ts_init,
    )


@pytest.mark.parametrize("start", ["9.00", "0.9999", "10.005", "0.000001"])
def test_no_closes_preserves_the_exact_start(start: str) -> None:
    level = Decimal(start)
    assert rebuilt_level((), level, Decimal("0.20")) == level


@pytest.mark.parametrize(
    ("closes", "start", "offset"),
    [
        (("10", "9", "8"), "10", "0.50"),
        (("0.03", "0.01"), "0.01", "1"),
        (("100", "98"), "99", "5"),
        (("10.20",), "10.005", "0.20"),
        (("10.209",), "10.005", "0.20"),
        (("0.00009",), "0.000001", "0.00001"),
    ],
)
def test_closes_never_lower_the_start(closes: tuple[str, ...], start: str, offset: str) -> None:
    level = Decimal(start)
    assert rebuilt_level(tuple(map(Decimal, closes)), level, Decimal(offset)) == level


@pytest.mark.parametrize(
    ("highest", "start", "offset", "expected"),
    [
        ("10.629995", "9", "0.20", "10.42"),
        ("10.49", "8", "0.20", "10.29"),
        ("0.44379", "0.40", "0.02", "0.4237"),
        ("0.443699", "0.40", "0.02", "0.4236"),
        ("1.01999", "0.50", "0.02", "0.9999"),
        ("1.02", "0.50", "0.02", "1.00"),
        ("1.02999", "0.50", "0.02", "1.00"),
        ("6000.4371", "5900", "0.20", "6000.23"),
        ("5000.20", "4990", "0.20", "5000.00"),
        ("0.0003", "0.0001", "0.0001", "0.0002"),
        ("10.62999999999999999999999999", "9", "0.20", "10.42"),
    ],
)
def test_highest_close_less_offset_rounds_down_at_the_resulting_price(
    highest: str, start: str, offset: str, expected: str
) -> None:
    high, initial, trail = Decimal(highest), Decimal(start), Decimal(offset)
    # The highest close is neither first nor last; the floor changes bands at $1.
    closes = [Decimal("0.000001"), high, high / 2]
    result = rebuilt_level(closes, initial, trail)
    assert result == Decimal(expected)
    assert initial <= result <= max(initial, high - trail)


def test_appending_closes_never_lowers_the_rebuilt_level() -> None:
    closes = tuple(map(Decimal, ("0.96", "1.01999", "1.02999", "0.90", "1.05999", "0.80")))
    levels = [rebuilt_level(closes[:n], Decimal("0.95"), Decimal("0.02")) for n in range(7)]
    assert levels == list(map(Decimal, ("0.95", "0.95", "0.9999", "1.00", "1.00", "1.03", "1.03")))
    assert all(previous <= current for previous, current in pairwise(levels))


@pytest.mark.parametrize(("close", "expected"), [("10.43", "10.42"), ("0.5000", "0.4999")])
def test_a_tiny_offset_cannot_round_the_candidate_up_to_the_next_tick(
    close: str, expected: str
) -> None:
    assert rebuilt_level([Decimal(close)], Decimal("0.1"), Decimal("1e-40")) == Decimal(expected)


def test_the_callers_decimal_context_cannot_change_the_level() -> None:
    with localcontext(prec=5, rounding=ROUND_CEILING) as context:
        # localcontext copies the thread's sticky flags; clear them so only this call can set one
        context.clear_flags()
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = rebuilt_level([Decimal("10.629995")], Decimal(9), Decimal("0.20"))
        assert context.prec == 5
        assert context.rounding == ROUND_CEILING
        assert not context.flags[Inexact]
        assert not context.flags[Rounded]
    assert result == Decimal("10.42")


@pytest.mark.parametrize("name", ["start", "offset", "close"])
@pytest.mark.parametrize("invalid", INVALID)
def test_each_nonpositive_or_nonfinite_input_is_refused_by_name(name: str, invalid: str) -> None:
    start = Decimal(invalid) if name == "start" else Decimal(9)
    offset = Decimal(invalid) if name == "offset" else Decimal("0.20")
    closes = [Decimal(11), Decimal(invalid), Decimal(10)] if name == "close" else [Decimal(11)]
    with pytest.raises(ValueError, match=name):
        rebuilt_level(closes, start, offset)


@pytest.mark.parametrize("name", ["start", "offset"])
@pytest.mark.parametrize("invalid", INVALID)
def test_no_closes_still_validates_the_start_and_offset(name: str, invalid: str) -> None:
    start = Decimal(invalid) if name == "start" else Decimal(9)
    offset = Decimal(invalid) if name == "offset" else Decimal("0.20")
    with pytest.raises(ValueError, match=name):
        rebuilt_level([], start, offset)


def test_rebuilds_do_not_retain_a_previous_tape_or_modify_the_inputs() -> None:
    closes = [Decimal(11), Decimal(10)]
    before = closes.copy()
    assert rebuilt_level(closes, Decimal(9), Decimal("0.20")) == Decimal("10.80")
    assert rebuilt_level([Decimal(10)], Decimal(9), Decimal("0.20")) == Decimal("9.80")
    assert rebuilt_level(tuple(closes), Decimal(9), Decimal("0.20")) == Decimal("10.80")
    assert closes == before


def test_bars_use_closes_and_ignore_a_stray_high() -> None:
    ordinary = [_bar("10.5000", MINUTE), _bar("10.6299", 2 * MINUTE), _bar("10.2500", 3 * MINUTE)]
    spike = [ordinary[0], _bar("10.6299", 2 * MINUTE, high="999999.0000"), ordinary[2]]
    assert rebuilt_level_from_bars(ordinary, Decimal(9), Decimal("0.20")) == Decimal("10.42")
    assert rebuilt_level_from_bars(spike, Decimal(9), Decimal("0.20")) == Decimal("10.42")


def test_bars_delegate_their_decimal_closes_to_the_pure_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [_bar("0.9800", MINUTE), _bar("1.0199", 2 * MINUTE)]
    received: list[tuple[tuple[Decimal, ...], Decimal, Decimal]] = []

    def capture(closes: Sequence[Decimal], start: Decimal, offset: Decimal) -> Decimal:
        received.append((tuple(closes), start, offset))
        return Decimal("0.9999")

    monkeypatch.setattr(rebuilding, "rebuilt_level", capture)
    assert rebuilt_level_from_bars(bars, Decimal("0.95"), Decimal("0.02")) == Decimal("0.9999")
    assert received == [((Decimal("0.9800"), Decimal("1.0199")), Decimal("0.95"), Decimal("0.02"))]


@pytest.mark.parametrize("events", [(2 * MINUTE, MINUTE), (0, 2 * MINUTE, MINUTE)])
def test_bars_out_of_event_time_order_are_refused(events: tuple[int, ...]) -> None:
    # Receipt times are ascending; event times decide whether the tape is ordered.
    bars = [_bar("10.50", event, ts_init=(n + 3) * MINUTE) for n, event in enumerate(events)]
    with pytest.raises(ValueError, match="time|order"):
        rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20"))


def test_bar_receipt_order_does_not_change_the_tape_order() -> None:
    bars = [_bar("10.50", 0, ts_init=4 * MINUTE), _bar("10.75", MINUTE, ts_init=3 * MINUTE)]
    assert rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20")) == Decimal("10.55")


def test_repeating_a_bar_at_the_same_event_time_changes_nothing() -> None:
    bar = _bar("10.50", MINUTE)
    assert rebuilt_level_from_bars([bar, bar], Decimal(9), Decimal("0.20")) == Decimal("10.30")


@pytest.mark.parametrize("other", ["BBB.NASDAQ", "AAA.NYSE"])
def test_bars_of_mixed_instruments_are_refused(other: str) -> None:
    bars = [_bar("10.50", MINUTE), _bar("10.75", 2 * MINUTE, instrument=other)]
    with pytest.raises(ValueError, match="instrument"):
        rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20"))


@pytest.mark.parametrize("close", ["0.00", "-1.00"])
def test_bar_closes_have_the_same_positive_price_check(close: str) -> None:
    with pytest.raises(ValueError, match="close"):
        rebuilt_level_from_bars([_bar(close, MINUTE)], Decimal(9), Decimal("0.20"))


def test_no_bars_preserves_the_exact_start() -> None:
    assert rebuilt_level_from_bars([], Decimal("10.005"), Decimal("0.20")) == Decimal("10.005")


@pytest.mark.parametrize("name", ["start", "offset"])
@pytest.mark.parametrize("invalid", INVALID)
def test_no_bars_still_validates_the_start_and_offset(name: str, invalid: str) -> None:
    start = Decimal(invalid) if name == "start" else Decimal(9)
    offset = Decimal(invalid) if name == "offset" else Decimal("0.20")
    with pytest.raises(ValueError, match=name):
        rebuilt_level_from_bars([], start, offset)


def test_rebuilds_need_no_clock_file_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = [_bar("10.50", MINUTE), _bar("10.75", 2 * MINUTE)]
    closes = [Decimal("10.50"), Decimal("10.75")]
    start, offset = Decimal(9), Decimal("0.20")

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a rebuild must not read the clock, files or network")

    # Scope patches to the calls, so pytest can report and clean up normally.
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(io, "open", forbidden)
        guard.setattr(os, "open", forbidden)
        guard.setattr(Path, "open", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(time, "time", forbidden)
        guard.setattr(time, "time_ns", forbidden)
        guard.setattr(time, "monotonic", forbidden)
        guard.setattr(time, "monotonic_ns", forbidden)
        guard.setattr(time, "perf_counter", forbidden)
        guard.setattr(time, "perf_counter_ns", forbidden)
        direct = rebuilt_level(closes, start, offset)
        wrapped = rebuilt_level_from_bars(bars, start, offset)

    assert direct == wrapped == Decimal("10.55")


@pytest.mark.parametrize("spec", ["1-DAY-LAST", "5-MINUTE-LAST", "1-MINUTE-ASK", "1-MINUTE-MID"])
def test_a_bar_of_another_series_of_the_same_name_is_refused(spec: str) -> None:
    # a day, five-minute or quote bar of the same name is not a minute trade close: an ask bar
    # at 60.00 among trade closes near 10 would otherwise lift the level to 59.80
    bars = [_bar("10.00", MINUTE), _bar("60.00", 2 * MINUTE, spec=spec)]
    with pytest.raises(ValueError, match=r"bars\[1\]"):
        rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20"))
    with pytest.raises(ValueError, match=r"bars\[0\]"):
        rebuilt_level_from_bars(bars[1:], Decimal(9), Decimal("0.20"))


def test_two_different_closes_for_one_minute_are_refused() -> None:
    # a corrected or second print of the same minute: which close is the minute's is not known
    bars = [_bar("10.50", MINUTE), _bar("12.00", MINUTE), _bar("10.60", 2 * MINUTE)]
    with pytest.raises(ValueError, match=r"bars\[1\]"):
        rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20"))


def test_the_same_minute_delivered_twice_is_read_once() -> None:
    bars = [_bar("10.50", MINUTE), _bar("10.50", MINUTE), _bar("10.60", 2 * MINUTE)]
    assert rebuilt_level_from_bars(bars, Decimal(9), Decimal("0.20")) == Decimal("10.40")


@pytest.mark.parametrize(
    ("closes", "start", "offset", "name"),
    [
        ([float("nan")], Decimal(9), Decimal("0.20"), r"close\[0\]"),
        ([10.5], Decimal(9), Decimal("0.20"), r"close\[0\]"),
        ([Decimal("10.5")], 9, Decimal("0.20"), "start"),
        ([Decimal("10.5")], Decimal(9), 0.2, "offset"),
    ],
)
def test_a_value_that_is_not_a_decimal_is_refused_by_name(closes, start, offset, name) -> None:
    with pytest.raises(TypeError, match=name):
        rebuilt_level(closes, start, offset)
