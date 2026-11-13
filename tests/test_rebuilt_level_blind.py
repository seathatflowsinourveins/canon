"""Contract tests of ``canon_trading.rebuilt_level``, written from its stated properties alone
(never below the start; the highest close less the offset, floored to the step; monotone; closes
only; no closes gives the start; invalid input refused by name; pure), without reading the module.
Imports happen inside tests so every case fails on its own where the module is missing.

NautilusTrader d53849b216e20f2c122ca01b4dd0da377638b186, read from the pinned copy:
* python/nautilus_trader/model/__init__.py:21 exports the model names.
* crates/model/src/python/data/bar.rs:326-329: BarType.from_str;
  :430-453: Bar constructor; :478-523: bar_type, OHLC and timestamps.
* crates/model/src/python/types/price.rs:455-459: Price.from_str.
* crates/model/src/python/types/quantity.rs:457-461: Quantity.from_int.
* crates/model/src/data/bar.rs:1199-1209: OHLC ordering and uniform precision.
* python/tests/unit/model/test_bar.py:417-439: the same public construction pattern.
Pytest ae1d0ff57b8bd4252da9ade7353523ac0ac73281:
* src/_pytest/python.py:1307-1365: parametrize, including stacked parameters.
* src/_pytest/raises.py:104-154: raises as a context manager.

Nonempty numerical examples use starts on the price step; empty input returns the start, on
the step or off it.
"""

from collections.abc import Sequence
from decimal import Decimal
from importlib import import_module
from typing import Literal

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

D = Decimal
Form = Literal["closes", "bars"]
INVALID = ("0", "-0", "-0.01", "NaN", "-NaN", "sNaN", "Infinity", "-Infinity")


def _bar(
    close: str,
    minute: int,
    *,
    high: str | None = None,
    instrument: str = "AAA.ALPACA",
) -> Bar:
    price = Price.from_str(close)
    return Bar(
        bar_type=BarType.from_str(f"{instrument}-1-MINUTE-LAST-EXTERNAL"),
        open=price,
        high=price if high is None else Price.from_str(high),
        low=price,
        close=price,
        volume=Quantity.from_int(1),
        ts_event=minute * 60_000_000_000,
        ts_init=(minute + 1) * 60_000_000_000,
    )


def _rebuild(form: Form, closes: Sequence[Decimal], start: Decimal, offset: Decimal) -> Decimal:
    """Call exactly the module's public functions; never substitute an implementation."""
    if form == "bars":
        bars = [_bar(str(close), minute) for minute, close in enumerate(closes, start=1)]
        return import_module("canon_trading.rebuilt_level").rebuilt_level_from_bars(
            bars, start, offset
        )
    return import_module("canon_trading.rebuilt_level").rebuilt_level(closes, start, offset)


@pytest.mark.parametrize("form", ("closes", "bars"))
@pytest.mark.parametrize(
    ("closes", "start", "offset", "expected"),
    (
        (("8.50", "9.99", "9.25"), "10.00", "1.25", "10.00"),
        (("10.50",), "10.00", "0.50", "10.00"),
        (("0.02", "0.03"), "0.0100", "0.50", "0.0100"),
        (("10.12", "12.349", "10.00"), "9.50", "0.125", "12.22"),
        (("10.25", "10.40"), "9.00", "0.15", "10.25"),
        (("1.10",), "0.8000", "0.09", "1.01"),
        (("0.75001", "0.87659", "0.81000"), "0.5000", "0.12340", "0.7531"),
        (("0.8001",), "0.6000", "0.1000", "0.7001"),
        (("1.0000",), "0.9800", "0.00009", "0.9999"),
        (("1.0100",), "0.9900", "0.0100", "1.00"),
        (("1.01009",), "0.9900", "0.01000", "1.00"),
        (("1.02999",), "0.9900", "0.01000", "1.01"),
        (("0.00019",), "0.0001", "0.00001", "0.0001"),
    ),
    ids=(
        "start-above-every-close",
        "candidate-equals-start",
        "offset-larger-than-every-close",
        "interior-highest-close-cent-floor",
        "exact-cent",
        "decimal-cent-not-binary-float",
        "subdollar-floor",
        "exact-subdollar-step",
        "dollar-close-subdollar-level",
        "level-exactly-one-dollar",
        "level-just-over-one-dollar",
        "cent-floor-after-crossing-dollar",
        "smallest-price-step",
    ),
)
def test_level_is_exact_floor_between_start_and_close_bound(
    form: Form, closes: tuple[str, ...], start: str, offset: str, expected: str
) -> None:
    """P1/P2: hand-calculated outcomes, including both sides of the price-step boundary."""
    prices = tuple(D(close) for close in closes)
    result = _rebuild(form, prices, D(start), D(offset))
    assert isinstance(result, Decimal)
    assert D(start) <= result <= max(D(start), max(prices) - D(offset))
    assert result == D(expected)


@pytest.mark.parametrize("form", ("closes", "bars"))
@pytest.mark.parametrize(
    ("closes", "start", "offset", "expected"),
    (
        (
            ("10.50", "11.509", "9.00", "10.50"),
            "10.00",
            "0.10",
            ("10.00", "10.40", "11.40", "11.40", "11.40"),
        ),
        (
            ("0.80000", "0.81239", "0.71000", "0.81230"),
            "0.7000",
            "0.10000",
            ("0.7000", "0.7000", "0.7123", "0.7123", "0.7123"),
        ),
        (
            ("0.98000", "1.00999", "1.01008", "1.01009", "1.02009", "0.97000", "1.02009"),
            "0.9800",
            "0.01009",
            ("0.9800", "0.9800", "0.9999", "0.9999", "1.00", "1.01", "1.01", "1.01"),
        ),
    ),
    ids=("dollar-pullback", "subdollar-pullback", "across-dollar-and-back"),
)
def test_appending_closes_never_lowers_the_level(
    form: Form, closes: tuple[str, ...], start: str, offset: str, expected: tuple[str, ...]
) -> None:
    """P3 (also P1/P2/P5): every prefix keeps or raises the level after falls and ties."""
    prices = tuple(D(close) for close in closes)
    levels = tuple(
        _rebuild(form, prices[:length], D(start), D(offset)) for length in range(len(prices) + 1)
    )
    assert levels == tuple(D(value) for value in expected)
    assert levels == tuple(sorted(levels))


@pytest.mark.parametrize(
    ("closes", "high", "start", "offset", "expected"),
    (
        (("10.0000", "11.2349", "10.5000"), "999.0000", "9.00", "0.1250", "11.10"),
        (("0.75000", "0.87659", "0.81000"), "999.00000", "0.5000", "0.12340", "0.7531"),
        (("1.00000", "1.01009", "0.99000"), "999.00000", "0.9800", "0.01000", "1.00"),
    ),
    ids=("dollar-spike", "subdollar-spike", "spike-across-dollar"),
)
def test_a_bar_high_spike_cannot_raise_the_level(
    closes: tuple[str, ...], high: str, start: str, offset: str, expected: str
) -> None:
    """P4: change only the high, on the last bar whose close is below an earlier close."""
    ordinary = [_bar(close, minute) for minute, close in enumerate(closes, start=1)]
    spiked = [*ordinary[:-1], _bar(closes[-1], len(closes), high=high)]
    rebuild = import_module("canon_trading.rebuilt_level").rebuilt_level_from_bars
    assert rebuild(ordinary, D(start), D(offset)) == D(expected)
    assert rebuild(spiked, D(start), D(offset)) == D(expected)


@pytest.mark.parametrize("form", ("closes", "bars"))
@pytest.mark.parametrize("start", ("10.00", "1.00", "0.1234", "0.0001", "0.99999"))
def test_no_closes_gives_start_unchanged(form: Form, start: str) -> None:
    """P5: do not subtract the offset or round an empty history's start."""
    result = _rebuild(form, (), D(start), D("0.25"))
    assert isinstance(result, Decimal)
    assert result == D(start)


@pytest.mark.parametrize("form", ("closes", "bars"))
@pytest.mark.parametrize("empty", (False, True), ids=("with-history", "empty"))
@pytest.mark.parametrize("field", ("start", "offset"))
@pytest.mark.parametrize("invalid", INVALID)
def test_invalid_start_or_offset_raises_even_without_closes(
    form: Form, empty: bool, field: str, invalid: str
) -> None:
    """P6: reject zero, negative, NaNs and infinities before an empty-input return."""
    start = D(invalid) if field == "start" else D("9.00")
    offset = D(invalid) if field == "offset" else D("0.25")
    closes = () if empty else (D("10.00"), D("11.00"))
    with pytest.raises(ValueError):
        _rebuild(form, closes, start, offset)


@pytest.mark.parametrize("invalid", INVALID)
@pytest.mark.parametrize("position", (0, 1, 2), ids=("first", "middle", "last"))
def test_every_close_is_validated_even_when_it_cannot_be_the_maximum(
    invalid: str, position: int
) -> None:
    """P6: invalid closes cannot disappear behind a valid maximum or depend on position."""
    closes = [D("10.00"), D("20.00")]
    closes.insert(position, D(invalid))
    rebuild = import_module("canon_trading.rebuilt_level").rebuilt_level
    with pytest.raises(ValueError):
        rebuild(closes, D("9.00"), D("0.25"))


@pytest.mark.parametrize("invalid", ("0.0000", "-0.0000", "-0.0100"))
@pytest.mark.parametrize("position", (0, 1, 2), ids=("first", "middle", "last"))
def test_bars_reject_nonpositive_closes(invalid: str, position: int) -> None:
    """P6: construct the real bars outside raises, so constructor errors cannot pass this."""
    closes = ["10.0000", "20.0000"]
    closes.insert(position, invalid)
    bars = [_bar(close, minute) for minute, close in enumerate(closes, start=1)]
    rebuild = import_module("canon_trading.rebuilt_level").rebuilt_level_from_bars
    with pytest.raises(ValueError):
        rebuild(bars, D("9.00"), D("0.25"))


@pytest.mark.parametrize("minutes", ((3, 2, 1), (1, 3, 2)), ids=("reversed", "middle-inversion"))
def test_bars_reject_out_of_time_order(minutes: tuple[int, ...]) -> None:
    """P7: refusing disorder is observable even though max(close) would hide it."""
    bars = [_bar("10.0000", minute) for minute in minutes]
    rebuild = import_module("canon_trading.rebuilt_level").rebuilt_level_from_bars
    with pytest.raises(ValueError):
        rebuild(bars, D("9.00"), D("0.25"))


@pytest.mark.parametrize("foreign", ("BBB.ALPACA", "AAA.OTHER"), ids=("symbol", "venue"))
def test_bars_reject_mixed_instruments(foreign: str) -> None:
    """P7: both the symbol and venue identify the instrument; all timestamps are ordered."""
    bars = [
        _bar("10.0000", 1),
        _bar("20.0000", 2, instrument=foreign),
        _bar("11.0000", 3),
    ]
    rebuild = import_module("canon_trading.rebuilt_level").rebuilt_level_from_bars
    with pytest.raises(ValueError):
        rebuild(bars, D("9.00"), D("0.25"))
