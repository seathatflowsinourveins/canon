"""The protective distance at a cold start after hours (U-h): `protect.session_bars` and `distance_for`.

The lead's order of 2026-11-13 (U-h, relayed by the deputy at 18:02): a symbol's distance takes the last
`atr_n` one-minute bars that exist in the session day, not the last `atr_n` minutes by the clock; with no
usable spread the ATR part stands alone inside the row's cap; the fixed fallback is left only for a symbol
with no history at all, named in the line and counted. Each case below is the shape of one of that
evening's arming events, which all fell to the fixed 2%: two longs with no usable spread, and one with
fewer than 14 complete one-minute bars in the last hour. Shapes only: no symbol or price of the day.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from alpaca.data.models import BarSet
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import protect
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture

EXIT = pytest.mark.no_position_without_its_exit(clause=2)

AFTER_HOURS = datetime(2026, 11, 13, 1, 30, tzinfo=UTC)
"""20:30 New York on 2026-11-12: after the post-market, a cold start's hour."""


def _bar(at: datetime, mid: str, width: str) -> dict:
    return {
        "t": at.isoformat().replace("+00:00", "Z"),
        "o": float(mid),
        "h": float(D(mid) + D(width) / 2),
        "l": float(D(mid) - D(width) / 2),
        "c": float(mid),
        "v": 100,
        "n": 3,
        "vw": float(mid),
    }


class SparseData:
    """alpaca-py's `StockHistoricalDataClient` with minute bars at the given times only, answering a bar
    request by its own `start` and `end` as Alpaca does."""

    def __init__(self, times: list[datetime], *, mid: str = "10.00", width: str = "0.04") -> None:
        self.bars = [_bar(t, mid, width) for t in times]
        self.asked: list = []

    def get_stock_bars(self, request):
        self.asked.append(request)
        # alpaca-py's request keeps the time as given or as naive UTC; read both as UTC
        start, end = (
            t if t.tzinfo else t.replace(tzinfo=UTC) for t in (request.start, request.end)
        )
        found = [b for b in self.bars if start <= datetime.fromisoformat(b["t"]) <= end]
        if request.sort is not None and request.sort.value == "desc":
            found.reverse()
        if request.limit is not None:
            found = found[: request.limit]
        return BarSet({request.symbol_or_symbols: found})


def _sparse(n: int, every: int, last: datetime) -> list[datetime]:
    """`n` bar times `every` minutes apart, the last opening at `last`."""
    return [last - timedelta(minutes=every * (n - 1 - i)) for i in range(n)]


@EXIT
def test_the_bars_are_the_last_ones_that_exist_in_the_session_day_not_the_last_hour() -> None:
    """A thin name's last 20 bars are 10 minutes apart and the last one is 90 minutes old: none falls in
    the last hour, and all lie in the session day that began at 04:00 New York."""
    times = _sparse(20, 10, AFTER_HOURS - timedelta(minutes=90))
    data = SparseData(times)
    bars = protect.session_bars(data, "AAA", AFTER_HOURS)
    assert len(bars) == protect.ATR_BARS
    assert [b.timestamp for b in bars] == times[-protect.ATR_BARS :]
    (request,) = data.asked
    assert request.limit == protect.ATR_BARS + 1 and request.sort.value == "desc"
    start = request.start if request.start.tzinfo else request.start.replace(tzinfo=UTC)
    assert start == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)  # 04:00 New York


def test_before_04_00_the_session_day_is_the_one_before() -> None:
    at = datetime(2026, 11, 13, 7, 30, tzinfo=UTC)  # 02:30 New York
    assert protect.session_start(at) == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    assert protect.session_start(datetime(2026, 11, 13, 9, 0, tzinfo=UTC)) == datetime(
        2026, 11, 13, 9, 0, tzinfo=UTC
    )


def test_a_minute_not_yet_complete_is_not_read() -> None:
    times = _sparse(3, 1, AFTER_HOURS - timedelta(seconds=30))
    bars = protect.session_bars(SparseData(times), "AAA", AFTER_HOURS)
    assert [b.timestamp for b in bars] == times[:-1]


def _bars(n: int, width: str = "0.04", mid: str = "10.00") -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            high=float(D(mid) + D(width) / 2), low=float(D(mid) - D(width) / 2), close=float(mid)
        )
        for _ in range(n)
    ]


@EXIT
def test_fewer_than_fourteen_complete_bars_in_the_last_hour_take_the_atr_of_those_that_exist() -> (
    None
):
    """W2's shape: fewer than 14 complete one-minute bars. Five bars exist: the ATR is taken over those
    five and named so, not the fixed 2%."""
    sent, distance = protect.distance_for("AAA", "OWN1", D("9.99"), D("10.00"), _bars(5))
    assert distance.plan.bound != "fixed" and distance.plan.source != "cold_atr_fallback"
    assert distance.plan.atr == pytest.approx(D("0.04"))
    assert not distance.fallback
    assert "ATR over 5 bars" in distance.source()
    assert sent == D("0.04")


@pytest.mark.parametrize(("bid", "ask"), [("0", "10.00"), ("10.01", "10.00"), ("10.00", "10.00")])
@EXIT
def test_with_no_usable_spread_the_atr_stands_alone_inside_the_cap(bid, ask) -> None:
    """Unit 8's shape: a long with no usable spread (no bid, crossed or locked). The ATR distance
    stands alone, named, with no spread floor; never the fixed 2%."""
    sent, distance = protect.distance_for("AAA", "OWN1", D(bid), D(ask), _bars(14))
    assert distance.plan.bound == "atr" and distance.plan.spread is None
    assert sent == D("0.04")
    assert not distance.fallback
    assert "): the ATR stands alone, with no spread floor" in distance.source()


def test_with_no_usable_spread_the_atr_distance_still_keeps_the_ceiling() -> None:
    """The ATR alone over the 0.10 ceiling of a found holding is refused by name, as with a spread."""
    with pytest.raises(ValueError, match="distance_over_ceiling"):
        protect.distance_for("AAA", "OWN1", D(0), D("10.00"), _bars(14, width="1.50"))


def test_with_no_usable_spread_each_atr_row_takes_its_own_multiple() -> None:
    _, distance = protect.distance_for("AAA", "OWN3", D(0), D("10.00"), _bars(14))
    assert distance.plan.atr == pytest.approx(D("0.04")) and distance.plan.measure is not None
    assert distance.plan.trail == distance.plan.measure


@EXIT
def test_only_a_symbol_with_no_history_at_all_takes_the_fixed_fallback_named_and_counted() -> None:
    sent, distance = protect.distance_for("AAA", "OWN1", D("9.99"), D("10.00"), [])
    assert distance.fallback
    assert "no history in the session day: fixed fallback" in distance.source()
    assert sent == D("0.20")


def test_the_fixed_row_stays_the_fixed_row_and_is_no_fallback() -> None:
    _, distance = protect.distance_for("AAA", "F2", D("9.99"), D("10.00"), _bars(14))
    assert distance.plan.bound == "fixed" and not distance.fallback


@pytest.mark.parametrize("n", [1, 5, 13, 14])
def test_an_atr_over_the_bars_that_exist_is_warm_after_exactly_that_many(n) -> None:
    """The Mover reads `slot.atr.initialized` to choose its fallback, so the ATR must be initialized
    after exactly the bars it was given (L6's check)."""
    atr = protect.warm_atr(_bars(n))
    assert atr is not None and atr.initialized and atr.value == pytest.approx(0.04)


def test_more_bars_than_atr_n_use_only_the_last_atr_n() -> None:
    wide, narrow = _bars(6, width="0.40"), _bars(protect.ATR_BARS)
    atr = protect.warm_atr(wide + narrow)
    assert atr is not None and atr.period == protect.PLANNER.atr_n
    assert atr.count == protect.ATR_BARS and atr.value == pytest.approx(0.04)


def test_a_bar_the_atr_would_skip_is_not_counted_so_the_atr_is_warm() -> None:
    """`update_raw` skips a bar whose close is outside its range; counting it would leave an ATR sized
    to the bars cold, and the Mover would take its fixed fallback unnamed."""
    bad = SimpleNamespace(high=10.0, low=9.0, close=11.0)
    atr = protect.warm_atr([*_bars(4), bad])
    assert atr is not None and atr.initialized and atr.period == 4


def test_no_sound_bar_is_no_history() -> None:
    bad = SimpleNamespace(high=9.0, low=10.0, close=9.5)
    _, distance = protect.distance_for("AAA", "OWN1", D("9.99"), D("10.00"), [bad])
    assert distance.fallback


def test_bars_with_no_range_and_no_spread_take_the_named_fallback_not_no_stop() -> None:
    """Flat single-print minutes give an ATR of 0; with no spread there is no measure at all, so the
    fixed fallback, named, rather than a zero distance and no stop."""
    sent, distance = protect.distance_for("AAA", "OWN1", D(0), D("10.00"), _bars(3, width="0"))
    assert distance.fallback and distance.flat and sent == D("0.20")
    assert "the session day's bars show no range: fixed fallback" in distance.source()


def test_a_tiny_atr_with_no_spread_is_raised_to_alpacas_smallest_trail() -> None:
    sent, distance = protect.distance_for(
        "AAA", "OWN1", D(0), D("10.00"), _bars(14, width="0.0001")
    )
    assert not distance.fallback and sent > 0
    assert distance.plan.bound == "Alpaca's smallest trail"


def test_the_fixed_row_with_no_usable_spread_names_it() -> None:
    _, distance = protect.distance_for("AAA", "F2", D("10.01"), D("10.00"), _bars(14))
    assert "no usable sip spread (bid 10.01, ask 10.00): no spread floor" in distance.source()


def test_with_no_bid_the_line_names_no_stop_level() -> None:
    _, distance = protect.distance_for("AAA", "OWN1", D(0), D("10.00"), _bars(14))
    line = distance.source()
    assert "stop opens about -" not in line and "no bid" in line


def test_a_nodes_own_bars_with_nautilus_prices_give_the_same_distance() -> None:
    """A node's protector feeds NautilusTrader `Bar`s, whose prices are `Price`; alpaca-py's carry
    floats. Both give one distance."""
    bar_type = BarType.from_str("AAA.ALPACA-1-MINUTE-LAST-EXTERNAL")
    nautilus = [
        Bar(
            bar_type,
            Price.from_str("10.00"),
            Price.from_str("10.02"),
            Price.from_str("9.98"),
            Price.from_str("10.00"),
            Quantity.from_int(100),
            i * 60_000_000_000,
            i * 60_000_000_000,
        )
        for i in range(14)
    ]
    assert (
        protect.distance_for("AAA", "OWN1", D(0), D("10.00"), nautilus)[0]
        == (protect.distance_for("AAA", "OWN1", D(0), D("10.00"), _bars(14))[0])
    )


def test_the_report_names_each_fixed_fallback_kind_by_its_count() -> None:
    report = protect.ProtectReport(fallbacks=2, flat_fallbacks=1)
    assert report.lines()[-2:] == [
        "fixed fallback: 2 sent with no history in the session day",
        "fixed fallback: 1 sent with no spread on a flat session day",
    ]
    assert protect.ProtectReport().lines() == ["no long held"]


@pytest.mark.parametrize(
    ("n", "fallback", "said"),
    [
        (0, True, "no history in the session day: fixed fallback"),
        (1, False, "ATR over 1 bars"),
        (13, False, "ATR over 13 bars"),
        (14, False, "ATR over 14 bars"),
    ],
)
@pytest.mark.parametrize(
    ("bid", "spread"), [("9.99", True), ("0", False)], ids=["spread", "no-spread"]
)
@EXIT
def test_the_bar_counts_the_lead_named(n, fallback, said, bid, spread) -> None:
    """The lead's cases (2026-11-13 18:17): 0 bars the fixed row, named; 1, 13 and 14 bars the ATR over
    the bars that exist, the count named; with a quoted spread and without."""
    sent, distance = protect.distance_for("AAA", "OWN1", D(bid), D("10.00"), _bars(n))
    assert distance.fallback is fallback and not distance.flat
    assert said in distance.source()
    assert sent == (D("0.20") if fallback else D("0.04"))
    assert (distance.plan.spread is not None) is spread


def test_flat_bars_with_a_spread_are_no_fallback_the_spread_floor_binds() -> None:
    """A flat session day with a usable spread: the ATR is 0, the two-spread floor is the distance, and
    no fallback is taken (the lead's second kind is for no spread only)."""
    sent, distance = protect.distance_for(
        "AAA", "OWN1", D("9.99"), D("10.00"), _bars(14, width="0")
    )
    assert not distance.fallback and not distance.flat
    assert distance.plan.bound == "spread floor" and sent == D("0.02")


def test_flat_bars_with_no_spread_are_the_second_fallback_kind_counted_apart() -> None:
    sent, distance = protect.distance_for("AAA", "OWN1", D(0), D("10.00"), _bars(14, width="0"))
    assert distance.fallback and distance.flat and sent == D("0.20")
