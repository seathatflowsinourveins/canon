"""The movers ranking's arithmetic (`canon_trading.movers_rank`): the per-symbol tally, the 15-minute
segment baseline and its file, the move, the dollar-volume ratio, the score, ties, the filters, the whole
universe with its unknowns, the separate clocks and the top entries. Pure apart from the baseline file: every
input is a plain value, the bars built by the adapter's own `parse_bar` from Alpaca-shaped bars."""

from __future__ import annotations

import builtins
import importlib.util
import json
import math
import os
import sys
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.models.corporate_actions import CorporateActionsSet
from alpaca.data.models.corporate_actions import ForwardSplit
from alpaca.data.models.corporate_actions import ReverseSplit
from alpaca.trading.models import Asset
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.indicators import VolumeWeightedAveragePrice
from nautilus_trader.model import Bar as NautilusBar
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import MarketStatusAction
from nautilus_trader.model import Price

from canon_trading import movers_rank
from canon_trading.movers_rank import BASELINE_SESSIONS
from canon_trading.movers_rank import NEW_YORK
from canon_trading.movers_rank import SEGMENTS
from canon_trading.movers_rank import TOP
from canon_trading.movers_rank import Baseline
from canon_trading.movers_rank import InvalidGeneration
from canon_trading.movers_rank import Reference
from canon_trading.movers_rank import SecurityType
from canon_trading.movers_rank import SplitRead
from canon_trading.movers_rank import Tally
from canon_trading.movers_rank import bar_dollars
from canon_trading.movers_rank import baseline_at
from canon_trading.movers_rank import rank
from canon_trading.movers_rank import read_baselines
from canon_trading.movers_rank import security_type_from_name
from canon_trading.movers_rank import segment_baseline
from canon_trading.movers_rank import split_since
from canon_trading.movers_rank import top_entries
from canon_trading.movers_rank import write_baselines
from canon_trading.movers_rank_signal import MAX_ENTRIES
from canon_trading.movers_rank_signal import RankEntry
from canon_trading.movers_rank_signal import decode_ranking
from canon_trading.movers_rank_signal import encode_ranking

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.model import Bar

SESSION = date(2026, 11, 11)  # a Wednesday
PRIOR = date(2026, 11, 10)
COMPUTED_NS = 1_800_000_000_000_000_000
ASSET: dict[str, object] = {
    "class": "us_equity",
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "name": "Example Inc. Common Stock",
    "shortable": True,
    "status": "active",
    "tradable": True,
}


def info(symbol: str, **fields: object) -> dict[str, object]:
    """The asset fields the adapter keeps in `Equity.info` (`canon_alpaca.providers.parse_equity`)."""
    equity = parse_equity(Asset.model_validate({**ASSET, "symbol": symbol}), 0)
    return {**equity.info, **fields}


def bars(
    symbol: str,
    day: date,
    start: time,
    closes: Sequence[float],
    volume: int = 1_000,
    spread: float = 0.0,
    minutes: int = 1,
) -> list[Bar]:
    """`closes` as consecutive bars of `minutes` each from `start` New York on `day`, parsed by the adapter
    (`ts_event` the open, `ts_init` the end); high and low are `spread` either side of the close."""
    instrument = parse_equity(Asset.model_validate({**ASSET, "symbol": symbol}), 0)
    # nt d53849b2 crates/model/src/data/bar.rs:562-565: minute steps divide 60 and are below 60.
    spec = f"{minutes // 60}-HOUR" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}-MINUTE"
    bar_type = BarType.from_str(f"{instrument.id}-{spec}-LAST-EXTERNAL")
    first = datetime.combine(day, start, NEW_YORK)
    out = []
    for i, close in enumerate(closes):
        t = (first + timedelta(minutes=i * minutes)).astimezone(UTC)
        payload = {
            "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": close,
            "h": close + spread,
            "l": close - spread,
            "c": close,
            "v": volume,
            "n": 10,
            "vw": close,
        }
        out.append(parse_bar(AlpacaBar(symbol, payload), bar_type, instrument))
    return out


def tally(items: Iterable[Bar], session: date = SESSION) -> Tally:
    t = Tally(session=session)
    for b in items:
        t = t.add(b)
    return t


def weekdays_before(day: date, n: int) -> list[date]:
    found: list[date] = []
    while len(found) < n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            found.append(day)
    return found


def baseline_of(
    symbol: str,
    closes: Sequence[float] = (10.0, 10.0, 10.0),
    volume: int = 1_000,
    start: time = time(9, 30),
) -> Baseline:
    days = weekdays_before(SESSION, BASELINE_SESSIONS)
    return segment_baseline({d: bars(symbol, d, start, closes, volume) for d in days}, SESSION)


FULL_READ = SplitRead(start=PRIOR + timedelta(days=1), end=SESSION, actions=())


def ranked(
    symbols: Sequence[str],
    today: Mapping[str, Sequence[Bar]],
    *,
    references: Mapping[str, Reference] | None = None,
    baselines: Mapping[str, Baseline] | None = None,
    opens: Mapping[str, float] | None = None,
    statuses: Mapping[str, MarketStatusAction] | None = None,
    splits: SplitRead | None = FULL_READ,
    assets: Mapping[str, Mapping[str, object]] | None = None,
    security_types: Mapping[str, SecurityType] | None = None,
    order_cap: float = 1_000.0,
    computed_ns: int | None = None,
    max_data_age_secs: float | None = None,
    zero_baseline_dollar_floor: float | None = None,
    zero_baseline_arm: str = "relative-floor",
    overnight_baseline: bool = False,
    no_baseline: bool = False,
) -> movers_rank.Generation:
    age_kwargs: dict[str, float] = (
        {} if max_data_age_secs is None else {"max_data_age_secs": max_data_age_secs}
    )
    if computed_ns is None:  # M-b: five seconds after the last bar, inside the age bound
        computed_ns = max(
            (bar.ts_init for items in today.values() for bar in items), default=COMPUTED_NS
        )
        computed_ns += 5_000_000_000
    return rank(
        session=SESSION,
        tallies={s: tally(b) for s, b in today.items()},
        assets={s: info(s) for s in symbols} if assets is None else assets,
        baselines={s: baseline_of(s) for s in symbols} if baselines is None else baselines,
        references=(
            {s: Reference(close=10.0, session=PRIOR) for s in symbols}
            if references is None
            else references
        ),
        opens=opens or {},
        statuses=statuses or {},
        splits=splits,
        security_types=(
            dict.fromkeys(symbols, SecurityType.SHARE) if security_types is None else security_types
        ),
        order_cap=order_cap,
        computed_ns=computed_ns,
        zero_baseline_dollar_floor=zero_baseline_dollar_floor,
        zero_baseline_arm=zero_baseline_arm,
        **age_kwargs,
        no_baseline=no_baseline,
        overnight_baseline=overnight_baseline,
    )


def row(generation: movers_rank.Generation, symbol: str) -> movers_rank.Row:
    (found,) = [r for r in generation.rows if r.instrument_id.symbol.value == symbol]
    return found


def day_bars(symbol: str, closes: Sequence[float], volume: int = 1_000) -> list[Bar]:
    return bars(symbol, SESSION, time(9, 30), closes, volume)


def rebadge(bar: Bar, instrument_id: str) -> Bar:
    """`bar` as a bar of `instrument_id`, every other field the same."""
    spec = str(bar.bar_type).split("-", 1)[1]
    return NautilusBar(
        BarType.from_str(f"{instrument_id}-{spec}"),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event,
        bar.ts_init,
    )


def ns(day: date, clock: time) -> int:
    return int(datetime.combine(day, clock, NEW_YORK).timestamp() * 1e9)


def received_at(bar: Bar, received_ns: int) -> Bar:
    """A replay with an initialization clock independent of its interval boundary, not parse_bar's output."""
    return NautilusBar(
        bar.bar_type,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event,
        received_ns,
    )


# --- the corporate-action filter


def corporate_action(kind: str, record: dict[str, object]) -> ForwardSplit | ReverseSplit:
    """One split as alpaca-py parses Alpaca's response (`CorporateActionsSet`), in the shape alpaca-py's
    own test gives it (`tests/data/test_corporate_actions.py`)."""
    (action,) = CorporateActionsSet({kind: [record]}).data[kind]
    assert isinstance(action, ForwardSplit | ReverseSplit)
    return action


def reverse_split(symbol: str, ex_date: date) -> ForwardSplit | ReverseSplit:
    return corporate_action(
        "reverse_splits",
        {
            "id": "a7932b15-f816-4f83-921e-998b524487b0",
            "symbol": symbol,
            "old_cusip": "OLDCUSIP1",
            "new_cusip": "NEWCUSIP1",
            "new_rate": 1,
            "old_rate": 5,
            "process_date": ex_date.isoformat(),
            "ex_date": ex_date.isoformat(),
        },
    )


def forward_split(symbol: str, ex_date: date) -> ForwardSplit | ReverseSplit:
    return corporate_action(
        "forward_splits",
        {
            "id": "a7932b15-f816-4f83-921e-998b524487b1",
            "symbol": symbol,
            "cusip": "CUSIP0001",
            "new_rate": 2,
            "old_rate": 1,
            "process_date": ex_date.isoformat(),
            "ex_date": ex_date.isoformat(),
        },
    )


def test_a_split_since_the_reference_close_excludes_the_name() -> None:
    """A one-for-five reverse split on the session's own ex-date: the prior close is a pre-split price, so
    a 1.24 last over a 0.2608 reference reads as a 375% move that is not one."""
    read = SplitRead(
        start=PRIOR + timedelta(days=1), end=SESSION, actions=(reverse_split("RVS", SESSION),)
    )
    assert split_since(read, "RVS", PRIOR, SESSION) is True
    generation = ranked(
        ["RVS"],
        {"RVS": day_bars("RVS", [1.24])},
        references={"RVS": Reference(close=0.2608, session=PRIOR)},
        splits=read,
    )
    r = row(generation, "RVS")
    assert r.split_since_reference is True
    assert r.eligible is False
    assert "corporate_action" in r.excluded_by
    assert r.move == pytest.approx(1.24 / 0.2608 - 1)  # recorded, never ranked
    assert top_entries(generation) == []


@pytest.mark.parametrize(
    ("ex_date", "expected"),
    [
        (PRIOR, False),  # on the reference close's own session: that close is already adjusted
        (PRIOR + timedelta(days=1), True),
        (SESSION, True),
        (SESSION + timedelta(days=1), False),  # after today: today's prices are pre-split too
    ],
)
def test_split_window_is_after_the_reference_session_through_today(
    ex_date: date, expected: bool
) -> None:
    read = SplitRead(
        start=PRIOR - timedelta(days=5),
        end=SESSION + timedelta(days=5),
        actions=(forward_split("FWD", ex_date),),
    )
    assert split_since(read, "FWD", PRIOR, SESSION) is expected


def test_a_split_of_another_symbol_does_not_exclude() -> None:
    read = SplitRead(
        start=PRIOR + timedelta(days=1), end=SESSION, actions=(reverse_split("OTHER", SESSION),)
    )
    assert split_since(read, "MINE", PRIOR, SESSION) is False


def test_corporate_actions_not_read_over_the_whole_window_are_unknown() -> None:
    assert split_since(None, "X", PRIOR, SESSION) is None
    late = SplitRead(start=SESSION, end=SESSION, actions=())  # misses the day after the reference
    assert split_since(late, "X", date(2026, 11, 6), SESSION) is None
    early = SplitRead(start=PRIOR + timedelta(days=1), end=PRIOR + timedelta(days=1), actions=())
    assert split_since(early, "X", PRIOR, date(2026, 11, 12)) is None
    generation = ranked(["X"], {"X": day_bars("X", [11.0])}, splits=None)
    r = row(generation, "X")
    assert r.split_since_reference is None
    assert r.eligible is False
    assert "corporate_action:unknown" in r.excluded_by


# --- the tally


def test_a_tally_keeps_the_last_bar_its_end_and_the_parts_dollar_volume() -> None:
    t = tally([*bars("A", PRIOR, time(23, 59), [9.0]), *day_bars("A", [10.0, 11.0, 12.0])])
    assert t.last == 12.0
    assert t.last_bar_ns == ns(SESSION, time(9, 33))
    assert t.minutes == 4
    # the part runs from 20:00 the evening before: the overnight bar counts (the lead, 2026-11-11)
    assert t.dollar_volume == pytest.approx((9.0 + 10.0 + 11.0 + 12.0) * 1_000)
    empty = Tally(session=SESSION)
    assert empty.add(day_bars("A", [10.0])[0]) is not empty
    assert empty.minutes == 0
    assert empty.last is None


def test_consecutive_streamed_bars_keep_receipts_separate_from_interval_ends() -> None:
    """Task M gap 2: 09:30 received at 09:31:00.100, then 09:31 at 09:32:00.100."""
    first, second = day_bars("A", [10.0, 11.0])
    first_receipt = ns(SESSION, time(9, 31)) + 100_000_000
    second_receipt = ns(SESSION, time(9, 32)) + 100_000_000
    t = tally([received_at(first, first_receipt), received_at(second, second_receipt)])
    assert t.minutes == 2
    assert t.dollar_volume == pytest.approx(21_000.0)
    assert t.last == 11.0
    assert t.last_open_ns == ns(SESSION, time(9, 31))
    assert t.last_bar_ns == ns(SESSION, time(9, 32))
    assert t.last_received_ns == second_receipt


def test_a_duplicate_or_out_of_order_bar_invalidates() -> None:
    """A second bar for one minute is a correction or a duplicate, and an older bar after a newer one is
    out of order: either invalidates, rather than counting twice or overwriting the last price (Antigravity's
    findings 5 and 6 against the probe's tape)."""
    first, second = day_bars("A", [10.0, 11.0])
    t = tally([first, second])
    with pytest.raises(InvalidGeneration, match="duplicate"):
        t.add(second)
    with pytest.raises(InvalidGeneration, match="order"):
        t.add(first)


def test_a_bar_outside_the_trade_date_invalidates() -> None:
    with pytest.raises(InvalidGeneration, match="trade date"):
        tally(bars("A", PRIOR, time(9, 30), [11.0]))
    with pytest.raises(InvalidGeneration, match="trade date"):  # 20:00 opens the next trade date
        tally(bars("A", SESSION, time(20, 0), [11.0]))
    assert (
        tally(bars("A", PRIOR, time(20, 0), [11.0])).last == 11.0
    )  # the overnight session's start


def test_a_bar_overlapping_the_one_before_invalidates() -> None:
    """A one-minute bar inside a five-minute bar already added (two bar types, or a backfill beside the
    stream) would count that minute twice: a bar opens no earlier than the one before ends."""
    (five,) = bars("A", SESSION, time(9, 30), [10.0], minutes=5)
    (inside,) = bars("A", SESSION, time(9, 31), [10.0])
    with pytest.raises(InvalidGeneration, match="bar type"):
        tally([five, inside])
    (next_one,) = bars("A", SESSION, time(9, 35), [10.0])
    with pytest.raises(InvalidGeneration, match="bar type"):
        tally([five, next_one])


def test_interval_overlap_is_rejected_even_when_receipt_precedes_the_end() -> None:
    (five,) = bars("A", SESSION, time(9, 30), [10.0], minutes=5)
    (inside,) = bars("A", SESSION, time(9, 31), [10.0], minutes=5)
    early_receipt = ns(SESSION, time(9, 30)) + 100_000_000
    with pytest.raises(InvalidGeneration, match="overlapping"):
        tally([received_at(five, early_receipt), inside])


def test_a_bar_without_a_time_interval_invalidates() -> None:
    (bar,) = day_bars("A", [10.0])
    tick_bar = NautilusBar(
        BarType.from_str("A.ALPACA-1-TICK-LAST-EXTERNAL"),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event,
        bar.ts_init,
    )
    with pytest.raises(InvalidGeneration, match="interval"):
        tally([tick_bar])


def test_a_bar_of_another_venue_or_instrument_invalidates() -> None:
    """A bar not on the adapter's venue, or of another symbol than the tally's, is refused rather than
    relabelled or merged (Antigravity, rank-codec finding 6)."""
    first, second = day_bars("A", [10.0, 11.0])
    with pytest.raises(InvalidGeneration, match="venue"):
        tally([rebadge(first, "A.XNAS")])
    with pytest.raises(InvalidGeneration, match="instrument"):
        tally([first, rebadge(second, "B.ALPACA")])
    with pytest.raises(InvalidGeneration, match="instrument"):
        segment_baseline(
            {
                PRIOR: [
                    *bars("A", PRIOR, time(9, 30), [10.0]),
                    *bars("B", PRIOR, time(9, 31), [10.0]),
                ]
            },
            SESSION,
        )
    days = weekdays_before(SESSION, 2)
    with pytest.raises(InvalidGeneration, match="instrument"):  # one symbol's baseline, day by day
        segment_baseline(
            {
                days[0]: bars("A", days[0], time(9, 30), [10.0]),
                days[1]: bars("B", days[1], time(9, 30), [10.0]),
            },
            SESSION,
        )
    with pytest.raises(InvalidGeneration, match="B"):
        bare_rank({"B": tally(day_bars("A", [10.0]))}, {})


def test_a_bar_ending_after_the_trade_date_invalidates() -> None:
    """A five-minute bar opening at 19:58 ends at 20:03, in the next trade date: it is refused, not
    silently left out of the part's dollar volume."""
    with pytest.raises(InvalidGeneration, match="trade date"):
        tally(bars("A", SESSION, time(19, 58), [11.0], minutes=5))
    assert tally(
        bars("A", SESSION, time(19, 55), [11.0], minutes=5)
    ).dollar_volume == pytest.approx(11_000.0)


def test_the_last_interval_counts_when_its_receipt_is_after_the_trade_date() -> None:
    (bar,) = bars("A", SESSION, time(19, 59), [10.0])
    receipt = ns(SESSION, time(20, 0)) + 100_000_000
    t = tally([received_at(bar, receipt)])
    assert t.last_bar_ns == ns(SESSION, time(20, 0))
    assert t.last_received_ns == receipt
    assert t.dollar_volume == pytest.approx(10_000.0)
    (prior_bar,) = bars("A", PRIOR, time(19, 59), [10.0])
    b = segment_baseline(
        {PRIOR: [received_at(prior_bar, ns(PRIOR, time(20, 0)) + 100_000_000)]}, SESSION
    )
    assert b.cumulative[-2:] == pytest.approx((0.0, 10_000.0))


def test_an_interval_past_the_trade_date_invalidates_even_with_an_early_receipt() -> None:
    (bar,) = bars("A", SESSION, time(19, 58), [10.0], minutes=5)
    with pytest.raises(InvalidGeneration, match="trade date"):
        tally([received_at(bar, ns(SESSION, time(19, 59)))])


def test_bar_dollars_is_upstreams_vwap_times_volume_of_the_typical_price() -> None:
    (bar,) = bars("A", SESSION, time(9, 30), [10.0], volume=100, spread=1.0)
    vwap = VolumeWeightedAveragePrice()
    vwap.handle_bar(bar)
    assert bar_dollars(bar) == pytest.approx(vwap.value * 100)
    # NautilusTrader's typical price, (high + low + close) / 3, is the close here: the bar is symmetric
    assert bar_dollars(bar) == pytest.approx(1_000.0)
    assert bar_dollars(bars("A", SESSION, time(9, 30), [10.0], volume=0)[0]) == 0.0


def test_bar_dollars_takes_the_typical_price_not_the_close() -> None:
    """An asymmetric bar tells the typical price from the close (Antigravity, rank-codec finding 9)."""
    (bar,) = bars("A", SESSION, time(9, 30), [10.0], volume=100)
    lopsided = NautilusBar(
        bar.bar_type,
        bar.open,
        Price.from_str("16.00"),
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event,
        bar.ts_init,
    )
    assert bar_dollars(lopsided) == pytest.approx(
        (16.0 + 10.0 + 10.0) / 3 * 100
    )  # 1,200, not 1,000


# --- the segment baseline


def test_the_baseline_is_the_mean_cumulative_dollar_volume_at_each_segment_end() -> None:
    days = weekdays_before(SESSION, 2)
    b = segment_baseline(
        {
            days[0]: bars("A", days[0], time(4, 0), [10.0], volume=100, minutes=15),
            days[1]: bars("A", days[1], time(4, 15), [10.0, 10.0], volume=300, minutes=15),
        },
        SESSION,
    )
    assert len(b.cumulative) == SEGMENTS
    # 04:00 opens segment 32: (1,000 + 0) / 2; segment 33: (1,000 + 3,000) / 2; on: (1,000 + 6,000) / 2
    assert b.cumulative[:32] == (0.0,) * 32  # nothing traded overnight
    assert b.cumulative[32:36] == pytest.approx((500.0, 2_000.0, 3_500.0, 3_500.0))
    assert b.cumulative[-1] == pytest.approx(3_500.0)
    assert b.sessions == tuple(sorted(days))


def test_baseline_segments_use_interval_ends_when_receipts_cross_the_boundary() -> None:
    first, second = bars("A", PRIOR, time(9, 44), [10.0, 20.0], volume=100)
    b = segment_baseline(
        {
            PRIOR: [
                received_at(first, ns(PRIOR, time(9, 45)) + 100_000_000),
                received_at(second, ns(PRIOR, time(9, 46)) + 100_000_000),
            ]
        },
        SESSION,
    )
    assert b.cumulative[53:57] == pytest.approx((0.0, 1_000.0, 3_000.0, 3_000.0))


def test_a_session_read_with_no_trade_counts_zero_and_no_holiday_is_guessed() -> None:
    """A thin name traded on one of five sessions: its baseline is a fifth of that day, whatever other
    names were read beside it (Antigravity's finding 8 against the probe's batch holiday rule)."""
    days = weekdays_before(SESSION, BASELINE_SESSIONS)
    read: dict[date, list[Bar]] = {d: [] for d in days}
    read[days[0]] = bars("THIN", days[0], time(7, 0), [10.0], volume=1_000)
    b = segment_baseline(read, SESSION)
    assert b.sessions == tuple(sorted(days))
    assert b.cumulative[-1] == pytest.approx(2_000.0)


def test_the_baseline_takes_only_the_latest_sessions_before_the_session() -> None:
    days = weekdays_before(SESSION, BASELINE_SESSIONS + 3)
    read = {d: bars("A", d, time(9, 30), [10.0]) for d in [*days, SESSION]}
    b = segment_baseline(read, SESSION)
    assert b.sessions == tuple(sorted(days[:BASELINE_SESSIONS]))
    assert b.short is False


def test_fewer_sessions_than_the_row_asks_are_named() -> None:
    days = weekdays_before(SESSION, 2)
    b = segment_baseline({d: bars("A", d, time(9, 30), [10.0]) for d in days}, SESSION)
    assert b.short is True
    assert f"2 of {BASELINE_SESSIONS}" in b.name
    assert segment_baseline({}, SESSION).name.startswith("none")


def test_the_baseline_part_runs_from_2000_the_evening_before() -> None:
    """The lead's definition (ADR 0087, 2026-11-11): the overnight part has the same 15-minute segments,
    so a day's part is its whole trade date, 96 segments from 20:00 the evening before."""
    assert SEGMENTS == 96
    b = segment_baseline(
        {
            PRIOR: [
                *bars("A", PRIOR - timedelta(days=1), time(20, 0), [10.0]),  # segment 0
                *bars("A", PRIOR, time(3, 59), [10.0]),  # segment 31
                *bars("A", PRIOR, time(19, 59), [10.0]),  # segment 95
            ]
        },
        SESSION,
    )
    assert b.cumulative[0] == pytest.approx(10_000.0)
    assert b.cumulative[30] == pytest.approx(10_000.0)
    assert b.cumulative[31] == pytest.approx(20_000.0)
    assert b.cumulative[-2] == pytest.approx(20_000.0)
    assert b.cumulative[-1] == pytest.approx(30_000.0)
    assert b.traded_sessions is not None
    assert [k for k, n in enumerate(b.traded_sessions) if n] == [0, 31, 95]
    with pytest.raises(InvalidGeneration, match="trade date"):  # 19:59 belongs to the day before
        segment_baseline(
            {PRIOR: bars("A", PRIOR - timedelta(days=1), time(19, 59), [10.0])}, SESSION
        )


def test_mondays_part_starts_on_sunday_evening() -> None:
    monday = date(2026, 11, 9)
    b = segment_baseline({monday: bars("A", date(2026, 11, 8), time(20, 0), [10.0])}, SESSION)
    assert b.cumulative[0] == pytest.approx(10_000.0)


@pytest.mark.parametrize(
    ("read", "match"),
    [
        ({PRIOR: [*bars("A", PRIOR, time(9, 30), [10.0])] * 2}, "duplicate"),
        ({PRIOR: list(reversed(bars("A", PRIOR, time(9, 30), [10.0, 11.0])))}, "order"),
        ({PRIOR: bars("A", SESSION, time(9, 30), [10.0])}, "trade date"),
        ({PRIOR: bars("A", PRIOR, time(20, 0), [10.0])}, "trade date"),
        (
            {
                PRIOR: [
                    *bars("A", PRIOR, time(9, 30), [10.0], minutes=5),
                    *bars("A", PRIOR, time(9, 31), [10.0]),
                ]
            },
            "bar type",
        ),
        ({PRIOR: bars("A", PRIOR, time(9, 40), [10.0], minutes=15)}, "segment"),
    ],
    ids=[
        "duplicate",
        "out-of-order",
        "another-date",
        "next-trade-date",
        "overlapping",
        "crossing-a-segment",
    ],
)
def test_a_baseline_day_with_a_bar_counted_twice_or_out_of_place_invalidates(
    read: dict[date, list[Bar]], match: str
) -> None:
    """The same checks as a tally's: a duplicate bar would double a segment, and a bar filed under the
    wrong date would enter another session's mean."""
    with pytest.raises(InvalidGeneration, match=match):
        segment_baseline(read, SESSION)


def test_the_baseline_within_a_segment_is_pro_rata_of_that_segment() -> None:
    """Segment 0 is 20:00 to 20:15 the evening before; a clock of 20:00 is the part's end, since no bar
    can end at its start."""
    cumulative = (1_000.0, 4_000.0, *([4_000.0] * (SEGMENTS - 2)))
    b = Baseline(cumulative=cumulative, sessions=(PRIOR,))
    assert baseline_at(b, time(20, 3)) == pytest.approx(200.0)
    assert baseline_at(b, time(20, 15)) == pytest.approx(1_000.0)
    assert baseline_at(b, time(20, 20)) == pytest.approx(2_000.0)
    assert baseline_at(b, time(3, 0)) == pytest.approx(4_000.0)
    assert baseline_at(b, time(20, 0)) == pytest.approx(4_000.0)
    assert baseline_at(Baseline(cumulative=(0.0,) * SEGMENTS, sessions=()), time(9, 0)) is None


def test_an_overnight_segment_is_established_by_its_own_trades() -> None:
    days = sorted(weekdays_before(SESSION, BASELINE_SESSIONS))
    b = segment_baseline(
        {d: bars("A", d - timedelta(days=1), time(21, 0), [10.0]) for d in days}, SESSION
    )
    assert b.established(time(21, 1)) is True  # 21:00 to 21:15 the evening before: segment 4
    assert b.established(time(9, 31)) is False


def test_an_overnight_name_without_an_overnight_baseline_ranks_in_the_tier() -> None:
    """Until a boats loader writes overnight bars into the baseline, no name is established overnight: it
    ranks on dollars under the move condition (ADR 0087, the lead's ruling of 2026-11-11)."""
    generation = ranked(
        ["NIGHT", "SMALL"],
        {
            "NIGHT": bars("NIGHT", PRIOR, time(21, 0), [11.0], volume=2_000),
            "SMALL": bars("SMALL", PRIOR, time(21, 0), [11.0], volume=1_000),
        },
    )
    night = row(generation, "NIGHT")
    assert night.dollar_volume == pytest.approx(22_000.0)
    assert night.established is False and night.ratio == math.inf
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["NIGHT", "SMALL"]


def test_with_no_baseline_file_every_name_ranks_in_the_dollar_tier() -> None:
    """ultra-3a8613a/2, the lead's ruling of 2026-11-11 (21:49): a part with no baseline file ranks every
    name as not established, on dollars under the move condition; with no file named absent, a name
    with no baseline stays a hole and is not ranked."""
    today = {
        "NIGHT": bars("NIGHT", PRIOR, time(21, 0), [11.0], volume=2_000),
        "SMALL": bars("SMALL", PRIOR, time(21, 0), [11.0], volume=1_000),
    }
    generation = ranked(["NIGHT", "SMALL"], today, baselines={}, no_baseline=True)
    night = row(generation, "NIGHT")
    assert night.established is False and night.ratio == math.inf
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["NIGHT", "SMALL"]
    assert not top_entries(ranked(["NIGHT", "SMALL"], today, baselines={}))
    with pytest.raises(ValueError, match="no baseline"):
        ranked(["NIGHT"], today, no_baseline=True)


def test_a_missing_baseline_file_is_named_absent_and_an_unreadable_one_is_not(
    tmp_path: Path,
) -> None:
    missing = read_baselines(tmp_path / "boats.json", SESSION)
    assert missing.absent and missing.problem and missing.baselines == {}
    (tmp_path / "bad.json").write_text("{")
    assert not read_baselines(tmp_path / "bad.json", SESSION).absent


def test_an_overnight_clock_never_makes_the_regular_open_known() -> None:
    r = row(ranked(["A"], {"A": bars("A", PRIOR, time(21, 0), [11.0])}, opens={"A": 10.0}), "A")
    assert r.move_from_open is None


def test_a_baseline_file_of_the_0400_part_is_refused_whole(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["format"] == "canon-movers-baseline/2"
    assert document["part_start"] == "20:00" and document["segments"] == SEGMENTS
    for change in (
        {"format": "canon-movers-baseline/1"},
        {"segments": 64},
        {"part_start": "04:00"},
    ):
        path.write_text(json.dumps({**document, **change}), encoding="utf-8")
        read = read_baselines(path, SESSION)
        assert read.baselines == {} and read.problem


def test_a_baseline_of_the_wrong_length_is_refused() -> None:
    assert SEGMENTS == 96  # 20:00 the evening before to 20:00 New York in 15-minute segments
    with pytest.raises(ValueError, match=str(SEGMENTS)):
        Baseline(cumulative=(1.0,), sessions=(PRIOR,))


@pytest.mark.parametrize(
    ("cumulative", "sessions"),
    [
        ((float("nan"),) * SEGMENTS, (PRIOR,)),
        ((float("inf"),) * SEGMENTS, (PRIOR,)),
        ((-1.0,) * SEGMENTS, (PRIOR,)),
        ((2.0, *([1.0] * (SEGMENTS - 1))), (PRIOR,)),  # a cumulative that falls
        ((1.0,) * SEGMENTS, tuple(weekdays_before(SESSION, BASELINE_SESSIONS + 1))[::-1]),
        ((1.0,) * SEGMENTS, (PRIOR, PRIOR)),
        ((1.0,) * SEGMENTS, (PRIOR, PRIOR - timedelta(days=1))),
    ],
    ids=["nan", "inf", "negative", "falling", "too-many", "repeated", "unordered"],
)
def test_a_baseline_that_cannot_be_a_mean_cumulative_is_refused(
    cumulative: tuple[float, ...], sessions: tuple[date, ...]
) -> None:
    with pytest.raises(ValueError, match="baseline"):
        Baseline(cumulative=cumulative, sessions=sessions)


# --- the baseline file


def test_the_baseline_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    baselines = {"A": baseline_of("A"), "B": baseline_of("B", volume=7)}
    write_baselines(path, SESSION, baselines)
    read = read_baselines(path, SESSION)
    assert read.problem is None
    assert read.baselines == baselines


@pytest.mark.parametrize(
    "content",
    [
        None,  # no file
        "not json",
        json.dumps({"format": "something else"}),
        json.dumps(
            {
                "format": movers_rank.BASELINE_FORMAT,
                "session": SESSION.isoformat(),
                "symbols": {"A": {"sessions": ["2026-11-10"], "cumulative": [1.0]}},
            }
        ),
        *(
            json.dumps(
                {
                    "format": movers_rank.BASELINE_FORMAT,
                    "session": SESSION.isoformat(),
                    "part_start": "20:00",
                    "segment_minutes": 15,
                    "segments": SEGMENTS,
                    "lookback_sessions": BASELINE_SESSIONS,
                    "symbols": {"A": {"sessions": sessions, "cumulative": [value] * SEGMENTS}},
                }
            )
            for sessions, value in [
                (["2026-11-10"], float("nan")),
                (["2026-11-10"], -1.0),
                ([SESSION.isoformat()], 1.0),  # a session not before the one ranked
                ([d.isoformat() for d in sorted(weekdays_before(SESSION, 6))], 1.0),
            ]
        ),
    ],
)
def test_a_missing_or_unreadable_baseline_file_means_no_baseline_and_says_why(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "baseline.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    read = read_baselines(path, SESSION)
    assert read.baselines == {}
    assert read.problem


def test_a_failed_baseline_replace_preserves_the_last_file_and_removes_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coordinator requested the shared writer unchanged: replace-whole, with cleanup on failure."""
    path = tmp_path / "baseline.json"
    write_baselines(path, PRIOR, {"A": baseline_of("A")})
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    assert read_baselines(path, SESSION).problem is None

    def refuse(*_: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        write_baselines(path, PRIOR, {"A": baseline_of("A")})
    assert read_baselines(path, SESSION).problem is None
    assert [p.name for p in tmp_path.iterdir()] == ["baseline.json"]


def test_a_baseline_file_for_another_session_is_not_used(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, PRIOR, {"A": baseline_of("A")})
    read = read_baselines(path, SESSION)
    assert read.baselines == {}
    assert read.problem is not None
    assert PRIOR.isoformat() in read.problem


# --- the move and the ratio


def test_the_move_is_from_the_reference_close_and_the_move_from_the_open_is_recorded() -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [10.5, 11.0, 12.0])},
        references={"A": Reference(close=10.0, session=PRIOR)},
        opens={"A": 10.5},
    )
    r = row(generation, "A")
    assert r.last == 12.0
    assert r.reference == 10.0
    assert r.move == pytest.approx(0.2)
    assert r.move_from_open == pytest.approx(12.0 / 10.5 - 1)


def test_an_unknown_reference_or_open_stays_unknown() -> None:
    generation = ranked(["A"], {"A": day_bars("A", [12.0])}, references={}, opens={})
    r = row(generation, "A")
    assert r.reference is None
    assert r.move is None
    assert r.move_from_open is None
    assert r.score is None
    assert r.eligible is False
    assert "reference:unknown" in r.excluded_by


@pytest.mark.parametrize(
    "baselines",
    [{}, {"A": Baseline(cumulative=(0.0,) * SEGMENTS, sessions=())}],
    ids=["missing", "no-sessions-read"],
)
def test_a_name_with_no_ratio_says_so_and_is_not_eligible(baselines: dict[str, Baseline]) -> None:
    """Eligible means rankable: a name with no ratio is excluded by name, not dropped unrecorded
    (Antigravity, rank-codec finding 8)."""
    r = row(ranked(["A"], {"A": day_bars("A", [12.0])}, baselines=baselines), "A")
    assert r.ratio is None
    assert r.eligible is False
    assert "ratio:unknown" in r.excluded_by


@pytest.mark.parametrize("day", [SESSION, SESSION + timedelta(days=1)])
def test_a_reference_not_before_the_session_invalidates(day: date) -> None:
    """A close of the session itself or later is not known through its trade date, and would empty the
    split window (Antigravity, rank-codec finding 1)."""
    with pytest.raises(InvalidGeneration, match="reference"):
        ranked(["A"], {"A": day_bars("A", [12.0])}, references={"A": Reference(10.0, day)})


def test_an_open_before_the_symbols_regular_bar_is_unknown_on_its_row() -> None:
    """Code review round 3, finding 3: one such open voided the whole generation; it is that row's
    unknown now, and the open is never used."""
    for start in (time(9, 29), time(8, 0)):  # the 09:29 bar ends at 09:30: no regular bar yet
        r = row(ranked(["A"], {"A": bars("A", SESSION, start, [12.0])}, opens={"A": 10.5}), "A")
        assert r.move_from_open is None
    r = row(ranked(["A"], {"A": bars("A", SESSION, time(8, 0), [12.0])}), "A")
    assert r.move_from_open is None


def test_a_late_premarket_receipt_does_not_make_the_regular_open_known() -> None:
    (bar,) = bars("A", SESSION, time(9, 29), [12.0])
    generation = ranked(
        ["A"],
        {"A": [received_at(bar, ns(SESSION, time(9, 31)) + 100_000_000)]},
        opens={"A": 10.5},
    )
    assert row(generation, "A").move_from_open is None


def test_a_publication_after_the_compute_time_invalidates() -> None:
    with pytest.raises(InvalidGeneration, match="compute"):
        ranked(["A"], {"A": day_bars("A", [12.0])}, computed_ns=ns(SESSION, time(9, 30)))
    assert (
        ranked(["A"], {"A": day_bars("A", [12.0])}, computed_ns=ns(SESSION, time(9, 31))).latency_ns
        == 0
    )


def test_an_interval_after_compute_time_invalidates_even_with_an_early_receipt() -> None:
    (bar,) = day_bars("A", [12.0])
    with pytest.raises(InvalidGeneration, match="compute"):
        ranked(
            ["A"],
            {"A": [received_at(bar, ns(SESSION, time(9, 30)) + 100_000_000)]},
            computed_ns=ns(SESSION, time(9, 30)) + 200_000_000,
        )


def test_a_receipt_after_compute_time_invalidates() -> None:
    (bar,) = day_bars("A", [12.0])
    with pytest.raises(InvalidGeneration, match="compute"):
        ranked(
            ["A"],
            {"A": [received_at(bar, ns(SESSION, time(9, 32)))]},
            computed_ns=ns(SESSION, time(9, 31)),
        )


def test_a_tally_keeps_the_latest_receipt_when_initialization_time_moves_backwards() -> None:
    first, second = day_bars("A", [10.0, 11.0])
    latest_receipt = ns(SESSION, time(9, 33))
    streamed = [
        received_at(first, latest_receipt),
        received_at(second, ns(SESSION, time(9, 32)) + 100_000_000),
    ]
    assert tally(streamed).last_received_ns == latest_receipt
    with pytest.raises(InvalidGeneration, match="compute"):
        ranked(["A"], {"A": streamed}, computed_ns=ns(SESSION, time(9, 32)) + 200_000_000)
    generation = ranked(["A"], {"A": streamed}, computed_ns=ns(SESSION, time(9, 34)))
    assert generation.received_ns == latest_receipt
    assert row(generation, "A").last_received_ns == latest_receipt
    assert generation.published_ns == ns(SESSION, time(9, 32))


def test_delayed_receipt_does_not_advance_publication_freshness_or_baseline() -> None:
    (bar,) = day_bars("A", [11.0])
    receipt = ns(SESSION, time(9, 45)) + 100_000_000
    generation = ranked(
        ["A"],
        {"A": [received_at(bar, receipt)]},
        computed_ns=ns(SESSION, time(9, 46)),
        max_data_age_secs=3600,  # the 9:31 bar is 15 minutes old; freshness is tested on its own
    )
    r = row(generation, "A")
    assert r.last_bar_ns == ns(SESSION, time(9, 31))
    assert r.last_received_ns == receipt
    assert r.baseline == pytest.approx(2_000.0)  # one of 15 minutes of the 30,000-dollar segment
    assert r.ratio == pytest.approx(5.5)
    assert generation.published_ns == ns(SESSION, time(9, 31))
    assert generation.received_ns == receipt
    assert generation.latency_ns == 15 * 60 * 1_000_000_000
    assert generation.clocks == {
        "publication": ns(SESSION, time(9, 31)),
        "receipt": receipt,
        "generation": ns(SESSION, time(9, 46)),
    }


@pytest.mark.parametrize("close", [float("inf"), float("nan"), 1e-308])
def test_a_reference_that_is_not_a_price_or_overflows_is_unknown(close: float) -> None:
    """An infinite or NaN close is not a price, and one so small the move overflows has no finite move:
    the move, score and entry stay unknown (Antigravity, rank-codec finding 5)."""
    generation = ranked(
        ["A"], {"A": day_bars("A", [12.0])}, references={"A": Reference(close, PRIOR)}
    )
    r = row(generation, "A")
    assert r.move is None
    assert r.score is None
    assert r.eligible is False
    assert top_entries(generation) == []


@pytest.mark.parametrize("open_", [0.0, -1.0, float("nan"), float("inf"), 1e-320])
def test_an_open_that_is_not_a_price_leaves_the_move_from_the_open_unknown(open_: float) -> None:
    r = row(ranked(["A"], {"A": day_bars("A", [12.0])}, opens={"A": open_}), "A")
    assert r.move_from_open is None
    assert r.move == pytest.approx(0.2)


def test_a_non_positive_reference_is_unknown_not_a_division() -> None:
    generation = ranked(
        ["A"], {"A": day_bars("A", [12.0])}, references={"A": Reference(close=0.0, session=PRIOR)}
    )
    assert row(generation, "A").move is None


def test_the_ratio_is_against_the_same_part_of_earlier_sessions() -> None:
    """Today, from 20:00 the evening before to 09:45 New York, against the baseline's cumulative value at
    09:45, a segment's end."""
    baseline = baseline_of("A", [10.0], volume=3_000)  # 30,000 a session, in 09:30-09:45
    today = day_bars("A", [10.0] * 15, volume=200)  # 09:30 to 09:45: 30,000
    r = row(ranked(["A"], {"A": today}, baselines={"A": baseline}), "A")
    assert r.dollar_volume == pytest.approx(30_000.0)
    assert r.baseline == pytest.approx(30_000.0)
    assert r.ratio == pytest.approx(1.0)
    assert r.baseline_name == baseline.name


def test_inside_a_segment_the_baseline_is_scaled_by_the_elapsed_share() -> None:
    baseline = baseline_of("A", [10.0], volume=3_000)  # 30,000 in 09:30-09:45
    today = day_bars("A", [10.0] * 3, volume=1_000)  # to 09:33: 30,000
    r = row(ranked(["A"], {"A": today}, baselines={"A": baseline}), "A")
    assert r.baseline == pytest.approx(6_000.0)  # 3 of the segment's 15 minutes
    assert r.ratio == pytest.approx(5.0)


def test_a_part_is_compared_by_new_york_clock_across_a_clock_change() -> None:
    """The earlier days are in daylight time, today in standard time: the same New York clock span."""
    summer = [date(2026, 10, d) for d in range(26, 31)]  # EDT; 2026-11-01 is the change
    baseline = segment_baseline(
        {d: bars("A", d, time(9, 30), [10.0] * 15) for d in summer}, SESSION
    )
    assert baseline.established(time(9, 45))  # every lookback session traded in the segment
    r = row(ranked(["A"], {"A": day_bars("A", [10.0] * 15)}, baselines={"A": baseline}), "A")
    assert r.ratio == pytest.approx(1.0)


def test_no_baseline_is_unknown_never_zero() -> None:
    today = {"A": day_bars("A", [11.0] * 3)}
    r = row(ranked(["A"], today, baselines={}), "A")
    assert r.baseline is None
    assert r.ratio is None
    assert r.score is None


def test_the_overnight_session_sets_the_last_price_and_counts_its_dollars() -> None:
    """Bars from 20:00 the evening before are the session's part: they set the last price and their
    dollars count from 20:00."""
    r = row(ranked(["A"], {"A": bars("A", PRIOR, time(23, 59), [10.5])}), "A")
    assert r.last == 10.5
    assert r.dollar_volume == pytest.approx(10_500.0)


# --- the score and ties


def test_the_score_is_move_times_ratio_with_no_price_floor() -> None:
    today = {"P": day_bars("P", [0.15] * 15, volume=2_000)}  # 4,500 to 09:45
    r = row(
        ranked(
            ["P"],
            today,
            baselines={"P": baseline_of("P", [0.10], volume=15_000)},  # 1,500
            references={"P": Reference(close=0.10, session=PRIOR)},
        ),
        "P",
    )
    assert r.move == pytest.approx(0.5)
    assert r.ratio == pytest.approx(3.0)
    assert r.score == pytest.approx(1.5)
    assert r.eligible is True


def test_ties_go_by_instrument_id_and_acceleration_is_unknown() -> None:
    symbols = ["ZZZ", "AAA", "MMM"]
    generation = ranked(symbols, {s: day_bars(s, [11.0] * 3) for s in symbols})
    entries = top_entries(generation)
    assert [str(e.instrument_id) for e in entries] == ["AAA.ALPACA", "MMM.ALPACA", "ZZZ.ALPACA"]
    assert all(r.acceleration is None for r in generation.rows)


def test_the_top_is_best_first_positive_scores_and_at_most_top() -> None:
    symbols = [f"S{i:02d}" for i in range(TOP + 5)]
    today = {s: day_bars(s, [10.0 + 0.1 * (i + 1)] * 3) for i, s in enumerate(symbols)}
    today["DOWN"] = day_bars("DOWN", [9.0] * 3)
    generation = ranked([*symbols, "DOWN"], today)
    entries = top_entries(generation)
    assert len(entries) == TOP
    assert entries[0].instrument_id == InstrumentId.from_str(f"{symbols[-1]}.ALPACA")
    moves = [e.move for e in entries]
    assert moves == sorted(moves, reverse=True)
    assert entries[0].move == pytest.approx(row(generation, symbols[-1]).move)
    assert "DOWN.ALPACA" not in {str(e.instrument_id) for e in entries}
    assert len(top_entries(generation, 3)) == 3
    assert top_entries(generation, 0) == []
    with pytest.raises(ValueError, match="n >= 0"):
        top_entries(generation, -1)


# --- the filters


def test_an_untradable_inactive_or_unknown_asset_is_excluded() -> None:
    names = ("NT", "IN", "NOFIELD")
    generation = ranked(
        list(names),
        {s: day_bars(s, [11.0] * 3) for s in names},
        assets={
            "NT": info("NT", tradable=False),
            "IN": info("IN", status="inactive"),
            "NOFIELD": {"name": "No Fields Inc. Common Stock"},
        },
    )
    for s in ("NT", "IN"):
        assert row(generation, s).tradable is False
        assert "tradable" in row(generation, s).excluded_by
    assert row(generation, "NOFIELD").tradable is None
    assert "tradable:unknown" in row(generation, "NOFIELD").excluded_by
    assert top_entries(generation) == []


@pytest.mark.parametrize(
    ("status", "halted"),
    [
        (MarketStatusAction.TRADING, False),
        (MarketStatusAction.HALT, True),
        (MarketStatusAction.PAUSE, True),
        (MarketStatusAction.QUOTING, True),  # quote-only after a halt: not trading yet
        (MarketStatusAction.CLOSE, True),
        (MarketStatusAction.NONE, None),
        (MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE, None),
    ],
)
def test_only_trading_admits_and_a_status_that_says_nothing_is_unknown(
    status: MarketStatusAction, halted: bool | None
) -> None:
    r = row(ranked(["H"], {"H": day_bars("H", [11.0] * 3)}, statuses={"H": status}), "H")
    assert r.halted is halted
    assert ("halt" in r.excluded_by) is (halted is True)
    assert r.eligible is (halted is not True)


def test_an_unknown_halt_state_is_recorded_and_its_eligibility_is_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = {"U": day_bars("U", [11.0] * 3)}
    r = row(ranked(["U"], today), "U")
    assert r.halted is None
    assert r.unchecked == ("halt",)
    assert r.eligible is movers_rank.HALT_UNKNOWN_ELIGIBLE
    monkeypatch.setattr(movers_rank, "HALT_UNKNOWN_ELIGIBLE", False)
    r = row(ranked(["U"], today), "U")
    assert r.eligible is False
    assert r.unchecked == ()
    assert "halt:unknown" in r.excluded_by


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("NexGel, Inc Warrant", SecurityType.WARRANT),
        (
            "Example Acquisition Corp. Warrants, each exercisable for one share",
            SecurityType.WARRANT,
        ),
        (
            "Example Acquisition Corp. Units, each consisting of one share and one right",
            SecurityType.UNIT,
        ),
        ("Example Acquisition Corp. Unit", SecurityType.UNIT),
        ("Example Acquisition Corp. Rights", SecurityType.RIGHT),
        ("Example Inc. Common Stock", SecurityType.SHARE),
        ("Example Ltd. American Depositary Shares", SecurityType.SHARE),
        ("United Example Corp. Common Stock", SecurityType.SHARE),  # whole words only
        (None, None),
        ("", None),
    ],
)
def test_the_name_proxy_for_security_type(name: str | None, expected: SecurityType | None) -> None:
    assert security_type_from_name(name) is expected


def test_a_blank_name_is_an_unknown_type() -> None:
    assert security_type_from_name("   ") is None


def test_a_status_that_says_nothing_keeps_the_one_held() -> None:
    """A short-sale restriction change after a halt does not erase the halt; the actor folds each status
    with `movers_rank.status_after` (Antigravity, rank-codec finding 7)."""
    halt, trading = MarketStatusAction.HALT, MarketStatusAction.TRADING
    ssr = MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE
    assert movers_rank.status_after(halt, ssr) == halt
    assert movers_rank.status_after(halt, MarketStatusAction.NONE) == halt
    assert movers_rank.status_after(halt, trading) == trading
    assert movers_rank.status_after(None, ssr) is None
    assert movers_rank.status_after(trading, MarketStatusAction.PAUSE) == MarketStatusAction.PAUSE


def test_warrants_units_rights_and_unknown_types_are_excluded() -> None:
    symbols = ["W", "U", "R", "Q", "S"]
    generation = ranked(
        symbols,
        {s: day_bars(s, [11.0] * 3) for s in symbols},
        security_types={
            "W": SecurityType.WARRANT,
            "U": SecurityType.UNIT,
            "R": SecurityType.RIGHT,
            "S": SecurityType.SHARE,
        },
    )
    for s in ("W", "U", "R"):
        assert "security_type" in row(generation, s).excluded_by
    assert "security_type:unknown" in row(generation, "Q").excluded_by
    assert [str(e.instrument_id) for e in top_entries(generation)] == ["S.ALPACA"]


def test_the_name_proxy_maps_the_asset_fields() -> None:
    types = movers_rank.security_types_from_names(
        {"W": info("W", name="NexGel, Inc Warrant"), "S": info("S"), "N": info("N", name=None)}
    )
    assert types == {"W": SecurityType.WARRANT, "S": SecurityType.SHARE}


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 0.0, -1.0])
def test_an_order_cap_that_is_not_an_amount_invalidates(cap: float) -> None:
    """A bad cap would exclude every name, or none, and look like a market: it is refused instead."""
    with pytest.raises(InvalidGeneration, match="order cap"):
        ranked(["A"], {"A": day_bars("A", [12.0])}, order_cap=cap)


def test_a_symbol_that_is_not_an_instrument_invalidates() -> None:
    with pytest.raises(
        InvalidGeneration, match="symbol"
    ):  # NautilusTrader's Symbol refuses an empty one
        bare_rank({}, {"": info("A")})


def test_one_share_must_fit_the_order_cap() -> None:
    generation = ranked(
        ["BIG", "OK"],
        {"BIG": day_bars("BIG", [1_100.0] * 3), "OK": day_bars("OK", [11.0] * 3)},
        order_cap=1_000.0,
    )
    assert row(generation, "BIG").fits_order_cap_at_last is False
    assert "order_cap_at_last" in row(generation, "BIG").excluded_by
    assert row(generation, "OK").fits_order_cap_at_last is True
    assert [str(e.instrument_id) for e in top_entries(generation)] == ["OK.ALPACA"]


# --- the universe, the clocks and the output


def bare_rank(tallies: Mapping[str, Tally], assets: Mapping[str, Mapping[str, object]]):
    return rank(
        session=SESSION,
        tallies=tallies,
        assets=assets,
        baselines={},
        references={},
        opens={},
        statuses={},
        splits=FULL_READ,
        security_types={},
        order_cap=1_000.0,
        computed_ns=COMPUTED_NS,
    )


def test_the_generation_keeps_the_whole_universe_with_unknowns() -> None:
    """An asset with no bar today, and a tally with no asset, are both rows; neither is zero."""
    generation = bare_rank(
        {"BARONLY": tally(day_bars("BARONLY", [11.0] * 3))}, {"QUIET": info("QUIET")}
    )
    assert [r.instrument_id.symbol.value for r in generation.rows] == ["BARONLY", "QUIET"]
    quiet = row(generation, "QUIET")
    assert quiet.last is None
    assert quiet.dollar_volume is None
    assert quiet.minutes == 0
    assert quiet.fits_order_cap_at_last is None
    assert quiet.eligible is False
    bar_only = row(generation, "BARONLY")
    assert bar_only.tradable is None
    assert bar_only.minutes == 3
    assert bar_only.eligible is False


def test_a_tally_of_another_session_invalidates() -> None:
    with pytest.raises(InvalidGeneration, match="session"):
        bare_rank({"A": tally(bars("A", PRIOR, time(9, 30), [11.0]), session=PRIOR)}, {})


def test_the_generation_carries_its_data_time_and_compute_time() -> None:
    generation = ranked(
        ["A", "B"],
        {"A": day_bars("A", [11.0] * 3), "B": day_bars("B", [11.0] * 5)},
        computed_ns=COMPUTED_NS,
    )
    assert generation.published_ns == ns(SESSION, time(9, 35))
    assert generation.generated_ns == COMPUTED_NS
    assert generation.latency_ns == COMPUTED_NS - ns(SESSION, time(9, 35))
    assert generation.received_ns == ns(SESSION, time(9, 35))
    assert generation.clocks == {
        "publication": generation.published_ns,
        "receipt": generation.received_ns,
        "generation": COMPUTED_NS,
    }
    assert row(generation, "A").last_bar_ns == ns(SESSION, time(9, 33))


def test_no_bar_today_means_no_data_time_and_no_entries() -> None:
    generation = ranked(["A"], {})
    assert generation.published_ns is None
    assert generation.latency_ns is None
    assert top_entries(generation) == []


def test_entries_take_the_signal_contracts_form() -> None:
    """Best first, each with the ranking signal's entry fields, `instrument_id` and a float `move`
    (Antigravity, rank-codec finding 3: the codec reads the fields, not a pair's positions)."""
    entries = top_entries(
        ranked(["A", "B"], {"A": day_bars("A", [12.0] * 3), "B": day_bars("B", [11.0] * 3)})
    )
    assert [str(e.instrument_id) for e in entries] == ["A.ALPACA", "B.ALPACA"]
    assert [e.move for e in entries] == [12.0 / 10.0 - 1, 11.0 / 10.0 - 1]
    assert all(type(e.move) is float for e in entries)


# --- L1 high and T1 regressions, producer-shaped bars


def engine_bar(bar: Bar) -> Bar:
    """The default live TimeBarAggregator's shape at d53849b2: INTERNAL, ts_event == ts_init == close.

    crates/data/src/aggregation.rs:1735-1752, 5361-5368; live/src/node/config.rs:2217.
    The input is parse_bar's open-stamped EXTERNAL bar, with ts_init already its interval end.
    """
    return NautilusBar(
        BarType.from_str(str(bar.bar_type).replace("-EXTERNAL", "-INTERNAL")),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_init,
        bar.ts_init,
    )


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
def test_real_producer_stamps_give_the_same_completed_intervals(internal: bool) -> None:
    items = day_bars("A", [10.0, 11.0])
    if internal:
        items = [engine_bar(b) for b in items]
    t = tally(items)
    assert t.last_open_ns == ns(SESSION, time(9, 31))
    assert t.last_bar_ns == ns(SESSION, time(9, 32))
    assert t.last_received_ns == t.last_bar_ns  # both producers synthesize initialization at close
    generation = ranked(["A"], {"A": items}, computed_ns=ns(SESSION, time(9, 32)) + 100_000_000)
    assert generation.published_ns == t.last_bar_ns
    assert generation.received_ns == t.last_bar_ns
    assert generation.latency_ns == 100_000_000


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
def test_real_producers_include_1959_without_counting_the_next_trade_date(internal: bool) -> None:
    last = bars("A", SESSION, time(19, 59), [10.0])
    if internal:
        last = [engine_bar(b) for b in last]
    t = tally(last)
    assert t.last_bar_ns == ns(SESSION, time(20, 0))
    assert t.dollar_volume == 10_000.0
    next_day = bars("A", SESSION, time(20, 0), [10.0])
    if internal:
        next_day = [engine_bar(b) for b in next_day]
    with pytest.raises(InvalidGeneration, match="trade date"):
        tally(next_day)


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
def test_real_producer_open_gate_uses_the_interval_after_0930(internal: bool) -> None:
    premarket = bars("A", SESSION, time(9, 29), [11.0])
    regular = day_bars("A", [11.0])
    if internal:
        premarket = [engine_bar(b) for b in premarket]
        regular = [engine_bar(b) for b in regular]
    assert row(ranked(["A"], {"A": premarket}, opens={"A": 10.5}), "A").move_from_open is None
    assert row(ranked(["A"], {"A": regular}, opens={"A": 10.5}), "A").move_from_open == (
        pytest.approx(11.0 / 10.5 - 1)
    )


def test_engine_bars_use_their_open_for_part_and_baseline_segment_membership() -> None:
    (before_0400,) = bars("A", SESSION, time(3, 59), [11.0])
    assert tally([engine_bar(before_0400)]).dollar_volume == 11_000.0  # the part holds the night
    (before_0400,) = bars("A", PRIOR, time(3, 59), [11.0])
    filed = segment_baseline({PRIOR: [engine_bar(before_0400)]}, SESSION).cumulative
    assert (filed[31], filed[32]) == (11_000.0, 11_000.0)  # in segment 31, by its open
    original = bars("A", PRIOR, time(9, 44), [10.0, 20.0], volume=100)
    external = segment_baseline({PRIOR: original}, SESSION)
    internal = segment_baseline({PRIOR: [engine_bar(b) for b in original]}, SESSION)
    assert internal == external
    assert internal.cumulative[53:57] == (0.0, 1_000.0, 3_000.0, 3_000.0)


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
def test_a_real_producer_duplicate_is_rejected_when_initialization_equals_close(
    internal: bool,
) -> None:
    (bar,) = day_bars("A", [11.0])
    if internal:
        bar = engine_bar(bar)
    with pytest.raises(InvalidGeneration, match="duplicate"):
        tally([bar, bar])


@pytest.mark.parametrize("kind", ["source", "specification"])
def test_a_tally_and_baseline_never_mix_bar_types_even_across_days(kind: str) -> None:
    first = day_bars("A", [11.0])
    second = bars("A", SESSION, time(9, 35), [12.0], minutes=5 if kind == "specification" else 1)
    if kind == "source":
        second = [engine_bar(b) for b in second]
    with pytest.raises(InvalidGeneration, match="bar type"):
        tally([*first, *second])
    older = PRIOR - timedelta(days=1)
    old_bars = bars("A", older, time(9, 30), [11.0])
    new_bars = bars("A", PRIOR, time(9, 30), [11.0], minutes=5 if kind == "specification" else 1)
    if kind == "source":
        new_bars = [engine_bar(b) for b in new_bars]
    with pytest.raises(InvalidGeneration, match="bar type"):
        segment_baseline({older: old_bars, PRIOR: new_bars}, SESSION)


def test_check_bars_checks_even_when_the_caller_does_not_iterate_the_result() -> None:
    (bar,) = day_bars("A", [11.0])
    with pytest.raises(InvalidGeneration, match="duplicate"):
        movers_rank.check_bars([bar, bar], SESSION)


@pytest.mark.parametrize("interval", [0, -60_000_000_000])
def test_a_nonpositive_bar_interval_is_refused(interval: int) -> None:
    """Real BarSpecification refuses a nonpositive step; also guard its interval at our boundary."""
    (original,) = day_bars("A", [11.0])
    malformed = SimpleNamespace(
        ts_event=original.ts_event,
        bar_type=SimpleNamespace(
            spec=SimpleNamespace(get_interval_ns=lambda: interval),
            is_internally_aggregated=lambda: False,
        ),
    )
    with pytest.raises(InvalidGeneration, match="interval"):
        movers_rank.bar_end_ns(cast("Bar", malformed))


def test_each_symbol_uses_its_own_baseline_clock() -> None:
    today = {
        "A": bars("A", SESSION, time(9, 30), [11.0] * 10),
        "B": bars("B", SESSION, time(10, 14), [11.0]),
    }
    # A's 9:40 bar is 35 minutes behind B's: wide enough an age bound that only the clocks differ
    generation = ranked(["A", "B"], today, max_data_age_secs=3600)
    alone = row(ranked(["A"], {"A": today["A"]}, max_data_age_secs=3600), "A")
    assert row(generation, "A").baseline == alone.baseline
    assert row(generation, "A").ratio == alone.ratio
    assert generation.published_ns == ns(SESSION, time(10, 15))


@pytest.mark.parametrize("has_bar", [False, True], ids=["absent", "premarket"])
def test_another_symbols_regular_bar_cannot_make_an_open_known(has_bar: bool) -> None:
    today = {"B": day_bars("B", [11.0])}
    if has_bar:
        today["A"] = bars("A", SESSION, time(9, 19), [11.0])
    generation = ranked(["A", "B"], today, opens={"A": 10.0, "B": 10.0})
    assert row(generation, "A").move_from_open is None
    assert row(generation, "B").move_from_open == pytest.approx(0.1)


def test_epoch_zero_is_a_known_clock() -> None:
    t = replace(Tally(session=SESSION), last_bar_ns=0, last_received_ns=0)
    generation = bare_rank({"A": t}, {})
    assert generation.published_ns == 0
    assert generation.received_ns == 0
    assert generation.latency_ns == COMPUTED_NS


def test_positive_volume_over_zero_baseline_ranks_first_then_by_dollars() -> None:
    """Lead 2026-11-11: an explicitly set floor, cleared by both names; long-only still applies."""
    symbols = ["LOW", "HIGH", "FINITE", "DOWN", "FLAT"]
    today = {
        "LOW": bars("LOW", SESSION, time(4, 0), [11.0], volume=1_000),
        "HIGH": bars("HIGH", SESSION, time(4, 0), [10.5], volume=2_000),
        "FINITE": bars("FINITE", SESSION, time(4, 0), [12.0], volume=500),
        "DOWN": bars("DOWN", SESSION, time(4, 0), [9.0]),
        "FLAT": bars("FLAT", SESSION, time(4, 0), [10.0]),
    }
    baselines = {s: baseline_of(s, volume=0) for s in symbols}
    baselines["FINITE"] = baseline_of("FINITE", [10.0], volume=1, start=time(4, 0))
    generation = ranked(symbols, today, baselines=baselines, zero_baseline_dollar_floor=10_000.0)
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == [
        "HIGH",
        "LOW",
        "FINITE",
    ]
    for symbol in ("LOW", "HIGH"):
        r = row(generation, symbol)
        assert r.eligible
        assert r.baseline == 0.0
        assert r.ratio == math.inf
    assert decode_ranking(encode_ranking(top_entries(generation))) == tuple(top_entries(generation))


def test_no_ratio_qualifier_leaves_zero_baseline_tier_ordered_by_dollars() -> None:
    generation = ranked(
        ["A", "B"],
        {"A": day_bars("A", [11.0]), "B": day_bars("B", [12.0], volume=2_000)},
        baselines={s: baseline_of(s, volume=0) for s in ("A", "B")},
    )
    assert row(generation, "A").eligible
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["B", "A"]


def test_zero_baseline_above_relative_floor_precedes_finite_qualifiers() -> None:
    symbols = ["ZERO", "QLOW", "QHIGH"]
    generation = ranked(
        symbols,
        {
            "ZERO": day_bars("ZERO", [12.0], volume=1_500),
            "QLOW": day_bars("QLOW", [11.0]),
            "QHIGH": day_bars("QHIGH", [12.0], volume=2_000),
        },
        baselines={s: baseline_of(s, volume=0 if s == "ZERO" else 1_000) for s in symbols},
    )
    # the floor is the qualifiers' median, 17,500 (the lead's definition, 2026-11-11), not their least
    assert row(generation, "ZERO").dollar_volume == 18_000.0
    assert row(generation, "QLOW").dollar_volume == 11_000.0
    assert row(generation, "QHIGH").dollar_volume == 24_000.0
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == [
        "ZERO",
        "QHIGH",
        "QLOW",
    ]


def test_zero_baseline_below_relative_floor_is_not_ranked() -> None:
    generation = ranked(
        ["ZERO", "Q"],
        {"ZERO": day_bars("ZERO", [11.0]), "Q": day_bars("Q", [12.0])},
        baselines={"ZERO": baseline_of("ZERO", volume=0), "Q": baseline_of("Q")},
        zero_baseline_dollar_floor=0.0,  # test-local: permits the old absolute gate to expose the defect
    )
    assert not row(generation, "ZERO").eligible
    assert "zero_baseline:relative_floor" in row(generation, "ZERO").excluded_by
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["Q"]


def test_zero_baseline_equal_to_relative_floor_is_ranked() -> None:
    generation = ranked(
        ["ZERO", "Q"],
        {s: day_bars(s, [11.0]) for s in ("ZERO", "Q")},
        baselines={"ZERO": baseline_of("ZERO", volume=0), "Q": baseline_of("Q")},
    )
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["ZERO", "Q"]


def test_only_established_movers_set_the_relative_floor() -> None:
    """The lead's set (2026-11-11): established, a finite ratio, the move condition passed, whatever the
    other filters. Counting DOWN or MISSING would lift the floor above ZERO; leaving HALTED out would
    leave it at Q's 12,000, also above ZERO. Only the right set gives (12,000 + 11) / 2."""
    symbols = ["ZERO", "Q", "HALTED", "DOWN", "MISSING"]
    generation = ranked(
        symbols,
        {
            "ZERO": day_bars("ZERO", [11.0]),
            "Q": day_bars("Q", [12.0]),
            "HALTED": day_bars("HALTED", [11.0], volume=1),
            "DOWN": day_bars("DOWN", [9.0], volume=1_000_000),
            "MISSING": day_bars("MISSING", [11.0], volume=1_000_000),
        },
        baselines={
            "ZERO": baseline_of("ZERO", volume=0),
            "Q": baseline_of("Q"),
            "HALTED": baseline_of("HALTED"),
            "DOWN": baseline_of("DOWN"),
        },
        statuses={"HALTED": MarketStatusAction.HALT},
    )
    assert not row(generation, "HALTED").eligible
    assert row(generation, "ZERO").eligible
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["ZERO", "Q"]


def test_the_excluded_zero_baseline_arm_is_available_for_walk_forward() -> None:
    generation = ranked(
        ["ZERO"],
        {"ZERO": day_bars("ZERO", [11.0])},
        baselines={"ZERO": baseline_of("ZERO", volume=0)},
        zero_baseline_arm="exclude",
    )
    assert not row(generation, "ZERO").eligible
    assert "zero_baseline:excluded_arm" in row(generation, "ZERO").excluded_by
    assert top_entries(generation) == []


def test_an_unknown_zero_baseline_arm_invalidates() -> None:
    with pytest.raises(InvalidGeneration, match="zero_baseline_arm"):
        ranked(["A"], {"A": day_bars("A", [11.0])}, zero_baseline_arm="other")


@pytest.mark.parametrize("floor", [12_000.0, 11_000.0], ids=["below", "equal"])
def test_zero_baseline_volume_must_clear_the_configured_floor(floor: float) -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [11.0])},
        baselines={"A": baseline_of("A", volume=0)},
        zero_baseline_dollar_floor=floor,
    )
    r = row(generation, "A")
    assert r.dollar_volume == 11_000.0
    # code review round 3, finding 5: a known ratio and score, excluded by name, not "unknown"
    assert r.ratio == math.inf and r.score == math.inf
    assert r.excluded_by == ("zero_baseline:absolute_floor",)
    assert top_entries(generation) == []


def test_a_symbol_the_signal_cannot_carry_is_excluded_by_name() -> None:
    """Code review round 3, finding 4: NautilusTrader's Symbol takes these, the signal's codec does not,
    and one in the top made `top_entries` raise ValueError on every tick."""
    for bad in ("A;B", "A=B", "A B"):
        generation = ranked([bad, "C"], {bad: day_bars(bad, [12.0]), "C": day_bars("C", [11.0])})
        assert row(generation, bad).excluded_by == ("symbol:unencodable",)
        assert [str(e.instrument_id) for e in top_entries(generation)] == ["C.ALPACA"]


def test_zero_volume_over_zero_baseline_is_not_ranked() -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [11.0], volume=0)},
        baselines={"A": baseline_of("A", volume=0)},
        zero_baseline_dollar_floor=10_000.0,
    )
    r = row(generation, "A")
    assert r.dollar_volume == 0.0
    assert r.baseline == 0.0
    assert r.ratio is None
    assert not r.eligible
    assert top_entries(generation) == []


def test_a_configured_floor_does_not_replace_a_missing_baseline() -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [11.0])},
        baselines={},
        zero_baseline_dollar_floor=10_000.0,
    )
    assert row(generation, "A").baseline is None
    assert not row(generation, "A").eligible
    assert top_entries(generation) == []


@pytest.mark.parametrize("floor", [float("nan"), float("inf"), -1.0])
def test_an_invalid_zero_baseline_floor_invalidates(floor: float) -> None:
    with pytest.raises(InvalidGeneration, match="zero_baseline_dollar_floor"):
        ranked(["A"], {"A": day_bars("A", [11.0])}, zero_baseline_dollar_floor=floor)


def test_an_overflowed_finite_baseline_score_is_ineligible() -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [12.0])},
        # a day baseline of 1.0: nothing before 04:00 (segment 32), which a day ratio leaves out
        baselines={
            "A": Baseline(cumulative=(0.0,) * 32 + (1.0,) * (SEGMENTS - 32), sessions=(PRIOR,))
        },
        references={"A": Reference(1e-307, PRIOR)},
    )
    r = row(generation, "A")
    assert r.move is not None and math.isfinite(r.move)
    assert r.score is None
    assert not r.eligible
    assert "score:unknown" in r.excluded_by


def test_bar_dollars_is_reentrant_without_a_shared_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministically interleave two calls at the point where T1's shared indicator lost state."""
    first, second = day_bars("A", [10.0, 20.0], volume=100)
    nested: list[float] = []
    entered = False

    class ReentrantVWAP:
        def __init__(self) -> None:
            self.indicator = VolumeWeightedAveragePrice()

        def reset(self) -> None:
            self.indicator.reset()

        def handle_bar(self, bar: Bar) -> None:
            nonlocal entered
            self.indicator.handle_bar(bar)
            if not entered:
                entered = True
                nested.append(bar_dollars(second))

        @property
        def value(self) -> float:
            return self.indicator.value

        @property
        def initialized(self) -> bool:
            return self.indicator.initialized

    monkeypatch.setattr(movers_rank, "_VWAP", ReentrantVWAP(), raising=False)
    monkeypatch.setattr(movers_rank, "VolumeWeightedAveragePrice", ReentrantVWAP)
    assert bar_dollars(first) == 1_000.0
    assert nested == [2_000.0]


@pytest.mark.parametrize("bad", [[True] * SEGMENTS, ["1"] * SEGMENTS, "1" * SEGMENTS])
def test_baseline_json_refuses_booleans_numeric_strings_and_string_arrays(
    tmp_path: Path, bad: object
) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    document = json.loads(path.read_text())
    document["symbols"]["A"]["cumulative"] = bad
    path.write_text(json.dumps(document))
    read = read_baselines(path, SESSION)
    assert read.baselines == {}
    assert read.problem


def test_a_baseline_integer_too_large_for_a_float_is_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    document = json.loads(path.read_text())
    document["symbols"]["A"]["cumulative"] = [10**400] * SEGMENTS
    path.write_text(json.dumps(document))
    read = read_baselines(path, SESSION)
    assert read.baselines == {}
    assert read.problem


def test_rank_imports_without_loading_session_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The future window -> actor -> rank import must not need a partially initialized window."""
    importer = builtins.__import__

    def refuse_window(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ):
        if name == "canon_trading.session_window":
            raise ImportError("the window is still importing its ranking actor")
        return importer(name, globals, locals, fromlist, level)

    spec = importlib.util.spec_from_file_location(
        "canon_trading._rank_import_test", movers_rank.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(builtins, "__import__", refuse_window)
    spec.loader.exec_module(module)


def test_baselines_and_window_use_the_same_atomic_writer_and_name_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canon_trading import rank_shared
    from canon_trading import session_window

    assert session_window.write_atomically is rank_shared.write_atomically
    assert session_window.NOT_COMMON is rank_shared.NOT_COMMON
    assert movers_rank.NOT_COMMON is rank_shared.NOT_COMMON
    written: list[tuple[Path, str]] = []

    def record(path: Path, body: str) -> None:
        written.append((path, body))
        rank_shared.write_atomically(path, body)

    monkeypatch.setattr(movers_rank, "write_atomically", record)
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    assert len(written) == 1
    assert written[0][0] == path
    assert read_baselines(path, SESSION).problem is None


def test_top_entries_are_validated_signal_entries() -> None:
    entries = top_entries(ranked(["A"], {"A": day_bars("A", [11.0])}))
    assert all(isinstance(e, RankEntry) for e in entries)
    assert TOP == MAX_ENTRIES
    assert decode_ranking(encode_ranking(entries)) == tuple(entries)


def test_top_entries_cannot_exceed_signal_capacity() -> None:
    symbols = [f"S{i:02d}" for i in range(MAX_ENTRIES + 1)]
    generation = ranked(symbols, {s: day_bars(s, [11.0]) for s in symbols})
    with pytest.raises(ValueError, match="at most"):
        top_entries(generation, MAX_ENTRIES + 1)


# --- Antigravity round-2 findings


def test_adr_0087_distinguishes_built_rows_from_planned_actor_and_freshness() -> None:
    root = Path(__file__).resolve().parents[1]
    record = (
        root / "docs/adr/0087-movers-ranking-score-and-the-actor-that-publishes-it-as-a-signal.md"
    )
    document = record.read_text()
    rows = document.split("* Rows, all UNCALIBRATED:", 1)[1].split("\n\n", 1)[0]
    assert f"`SEGMENT_MINUTES = {movers_rank.SEGMENT_MINUTES}`" in rows
    assert '`zero_baseline_arm = "relative-floor"`' in rows
    if not hasattr(movers_rank, "MAX_DATA_AGE_SECS"):
        assert "**Planned freshness (UNBUILT, UNVERIFIED in this checkout)**" in document
    if not (root / "src/canon_trading/movers_rank_actor.py").exists():
        assert "**Planned actor (UNBUILT, UNVERIFIED in this checkout)**" in document
    if not (root / "tests/test_movers_rank_actor.py").exists():
        confirmation = document.split("### Confirmation", 1)[1].split("## Evidence", 1)[0]
        assert "UNBUILT, UNVERIFIED" in confirmation
        assert "actor subscription and first-tick tests are UNVERIFIED in this checkout" in document


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
@pytest.mark.parametrize(
    ("start", "minutes"), [(time(3, 58), 5), (time(3, 0), 120)], ids=["5-minute", "2-hour"]
)
def test_a_bar_across_0400_is_refused_by_a_tally_and_a_baseline(
    internal: bool, start: time, minutes: int
) -> None:
    """04:00 is where a day ratio's span starts (the lead's rule), so a tally cannot split a bar across
    it (code review of the span, finding 1); it also crosses a segment's end, so a baseline cannot file
    it in one segment."""
    today = bars("A", SESSION, start, [11.0], minutes=minutes)
    before = bars("A", PRIOR, start, [11.0], minutes=minutes)
    if internal:
        today = [engine_bar(b) for b in today]
        before = [engine_bar(b) for b in before]
    with pytest.raises(InvalidGeneration, match="04:00"):
        tally(today)
    with pytest.raises(InvalidGeneration, match="segment"):
        segment_baseline({PRIOR: before}, SESSION)


@pytest.mark.parametrize("internal", [False, True], ids=["adapter", "live-engine"])
def test_a_bar_ending_or_starting_exactly_at_0400_is_unambiguous(internal: bool) -> None:
    before = bars("A", PRIOR, time(3, 55), [11.0], minutes=5)
    after = bars("A", PRIOR, time(4, 0), [12.0], minutes=5)
    if internal:
        before = [engine_bar(b) for b in before]
        after = [engine_bar(b) for b in after]
    assert tally(before, session=PRIOR).dollar_volume == 11_000.0
    assert tally(after, session=PRIOR).dollar_volume == 12_000.0
    assert segment_baseline({PRIOR: before}, SESSION).cumulative[31] == 11_000.0  # 03:55 to 04:00
    assert segment_baseline({PRIOR: after}, SESSION).cumulative[31] == 0.0  # 04:00 opens segment 32


@pytest.mark.parametrize("baseline", [None, 0.0, 1.0], ids=["missing", "zero", "finite"])
@pytest.mark.parametrize("floor", [float("nan"), float("inf"), -1.0, True, False])
def test_volume_score_validates_its_floor_even_without_a_usable_baseline(
    baseline: float | None, floor: float
) -> None:
    with pytest.raises(InvalidGeneration, match="zero_baseline_dollar_floor"):
        movers_rank.volume_score(0.1, 100.0, baseline, floor, established=True)


@pytest.mark.parametrize("value", [True, False])
def test_rank_refuses_a_boolean_order_cap_or_floor(value: bool) -> None:
    with pytest.raises(InvalidGeneration, match="order cap"):
        ranked([], {}, order_cap=value)
    with pytest.raises(InvalidGeneration, match="zero_baseline_dollar_floor"):
        ranked([], {}, zero_baseline_dollar_floor=value)


@pytest.mark.parametrize("clock", [True, False, float("nan"), float("inf"), -1, 1.0])
def test_compute_time_is_a_nonnegative_integer_clock_even_without_bars(clock: object) -> None:
    with pytest.raises(InvalidGeneration, match="compute time"):
        ranked([], {}, computed_ns=cast("int", clock))


@pytest.mark.parametrize("price", [True, False])
def test_a_boolean_reference_price_stays_unknown(price: bool) -> None:
    r = row(
        ranked(["A"], {"A": day_bars("A", [11.0])}, references={"A": Reference(price, PRIOR)}), "A"
    )
    assert r.reference is None
    assert r.move is None
    assert not r.eligible


@pytest.mark.parametrize("price", [True, False])
def test_a_boolean_regular_open_stays_unknown(price: bool) -> None:
    r = row(ranked(["A"], {"A": day_bars("A", [11.0])}, opens={"A": price}), "A")
    assert r.move_from_open is None


@pytest.mark.parametrize("move", [True, False, float("nan"), float("inf")])
def test_volume_score_cannot_turn_an_invalid_move_into_a_zero_baseline_score(move: float) -> None:
    ratio, score = movers_rank.volume_score(move, 100.0, 0.0, None, established=True)
    assert ratio == math.inf
    assert score is None


@pytest.mark.parametrize(
    ("dollars", "baseline"),
    [(True, 0.0), (False, 1.0), (-1.0, 1.0), (100.0, True), (100.0, False)],
)
def test_volume_score_does_not_interpret_booleans_or_negative_volume_as_measurements(
    dollars: float, baseline: float
) -> None:
    assert movers_rank.volume_score(0.1, dollars, baseline, None, established=True) == (
        None,
        None,
    )


@pytest.mark.parametrize("arm", ["relative-floor", "exclude"])
@pytest.mark.parametrize("close", [9.0, 10.0, 11.0], ids=["falling", "flat", "rising"])
@pytest.mark.parametrize("halted", [False, True], ids=["no-other-filter", "halted"])
def test_zero_baseline_exclusions_are_recorded_independently_of_other_filters_and_move(
    arm: str, close: float, halted: bool
) -> None:
    generation = ranked(
        ["ZERO", "Q"],
        {"ZERO": day_bars("ZERO", [close]), "Q": day_bars("Q", [12.0], volume=2_000)},
        baselines={"ZERO": baseline_of("ZERO", volume=0), "Q": baseline_of("Q")},
        statuses={"ZERO": MarketStatusAction.HALT} if halted else {},
        zero_baseline_arm=arm,
    )
    r = row(generation, "ZERO")
    expected = "zero_baseline:excluded_arm" if arm == "exclude" else "zero_baseline:relative_floor"
    assert r.ratio == math.inf
    assert expected in r.excluded_by
    assert ("halt" in r.excluded_by) is halted
    assert not r.eligible


# --- the lead's definitions of 2026-11-11 (ADR 0087): an established baseline, the move condition, and
# the median relative floor


def baseline_traded_in(symbol: str, traded: int, volume: int = 1_000) -> Baseline:
    """A baseline from `BASELINE_SESSIONS` sessions read, the symbol trading at 09:30 in only `traded`
    of them: a session read with no trade counts zero."""
    days = sorted(weekdays_before(SESSION, BASELINE_SESSIONS))
    return segment_baseline(
        {
            d: bars(symbol, d, time(9, 30), [10.0], volume) if i < traded else []
            for i, d in enumerate(days)
        },
        SESSION,
    )


def test_segment_trade_counts_count_the_sessions_with_a_trade_in_each_segment() -> None:
    days = sorted(weekdays_before(SESSION, BASELINE_SESSIONS))
    sessions = {
        d: [
            *bars("A", d, time(4, 0), [10.0], volume=100 if i < 2 else 0),
            *bars("A", d, time(9, 30), [10.0], volume=100),
        ]
        for i, d in enumerate(days)
    }
    counts = movers_rank.segment_trade_counts(sessions, SESSION)
    assert len(counts) == SEGMENTS
    assert counts[32] == 2  # 04:00 opens segment 32; a bar with no volume is not a trade
    assert counts[54] == BASELINE_SESSIONS  # 09:30 opens segment 54
    assert sum(counts) == 2 + BASELINE_SESSIONS
    assert segment_baseline(sessions, SESSION).traded_sessions == counts


def test_segment_trade_counts_use_the_bars_interval_not_its_stamps() -> None:
    """An engine bar is stamped at its close; the 09:44 bar of an INTERNAL type is in segment 54, not 55."""
    days = sorted(weekdays_before(SESSION, BASELINE_SESSIONS))
    sessions = {d: [engine_bar(b) for b in bars("A", d, time(9, 44), [10.0])] for d in days}
    counts = movers_rank.segment_trade_counts(sessions, SESSION)
    assert counts[54] == BASELINE_SESSIONS and counts[55] == 0


def test_only_a_baseline_traded_in_every_lookback_session_of_the_segment_is_established() -> None:
    assert baseline_traded_in("A", BASELINE_SESSIONS).established(time(9, 31)) is True
    assert baseline_traded_in("A", BASELINE_SESSIONS - 1).established(time(9, 31)) is False
    assert (
        baseline_traded_in("A", BASELINE_SESSIONS).established(time(9, 46)) is False
    )  # segment 55
    # the bar ending 09:45 is the segment's last: its own segment, not the next
    assert baseline_traded_in("A", BASELINE_SESSIONS).established(time(9, 45)) is True
    short = Baseline(
        cumulative=(1.0,) * SEGMENTS, sessions=(PRIOR,), traded_sessions=(1,) * SEGMENTS
    )
    assert short.established(time(9, 31)) is False  # fewer sessions than the lookback
    assert (
        Baseline(cumulative=(1.0,) * SEGMENTS, sessions=(PRIOR,)).established(time(9, 31)) is None
    )


@pytest.mark.parametrize(
    "traded", [(1,) * (SEGMENTS - 1), (2,) * SEGMENTS, (-1,) * SEGMENTS, (True,) * SEGMENTS]
)
def test_a_trade_count_that_cannot_be_the_baselines_is_refused(traded: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="trade count"):
        Baseline(cumulative=(1.0,) * SEGMENTS, sessions=(PRIOR,), traded_sessions=traded)


def test_a_stray_trade_in_the_lookback_gives_no_ratio_and_ranks_below_a_real_mover() -> None:
    """Code review round 3, finding 1: one stray trade in the lookback made a nearly flat name's ratio
    unbounded; without an established baseline it joins the zero-baseline tier and needs the floor."""
    generation = ranked(
        ["STRAY", "REAL"],
        {
            "STRAY": day_bars("STRAY", [10.5], volume=10),
            "REAL": day_bars("REAL", [11.0], volume=5_000),
        },
        baselines={"STRAY": baseline_traded_in("STRAY", 1, volume=1), "REAL": baseline_of("REAL")},
    )
    stray = row(generation, "STRAY")
    assert stray.established is False and stray.baseline is not None and stray.baseline > 0
    assert stray.ratio == math.inf  # the tier's mark: no finite ratio
    assert "zero_baseline:relative_floor" in stray.excluded_by
    assert row(generation, "REAL").established is True
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["REAL"]


def test_a_tenth_of_a_percent_on_a_hundred_dollars_is_not_ranked() -> None:
    """The lead's example: a +0.1% name with $100 of volume and no baseline ranks below the real mover."""
    generation = ranked(
        ["TINY", "REAL"],
        {"TINY": day_bars("TINY", [10.01], volume=10), "REAL": day_bars("REAL", [11.0])},
        baselines={"TINY": baseline_of("TINY", volume=0), "REAL": baseline_of("REAL")},
    )
    assert "move_condition" in row(generation, "TINY").excluded_by
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["REAL"]


def test_the_move_condition_applies_to_established_names_too() -> None:
    generation = ranked(
        ["SLOW", "FAST"],
        {"SLOW": day_bars("SLOW", [10.29], volume=50_000), "FAST": day_bars("FAST", [10.3])},
    )
    assert row(generation, "SLOW").excluded_by == ("move_condition",)
    assert row(generation, "FAST").eligible  # 3% exactly passes
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["FAST"]


def test_z_below_the_median_of_established_movers_ranks_below_big() -> None:
    """Code review round 3, finding 2: the least dollars of any riser let Z take the top before BIG; the
    floor is the median dollar volume of the established names passing the move condition."""
    symbols = ["Z", "BIG", "SMALL", "MID"]
    generation = ranked(
        symbols,
        {
            "Z": day_bars("Z", [10.5], volume=2_000),
            "BIG": day_bars("BIG", [10.4], volume=100_000),
            "SMALL": day_bars("SMALL", [10.4], volume=100),
            "MID": day_bars("MID", [10.4], volume=10_000),
        },
        baselines={**{s: baseline_of(s) for s in symbols}, "Z": baseline_of("Z", volume=0)},
    )
    # established dollars 1,040,000, 1,040 and 104,000: the median is 104,000, above Z's 21,000
    assert "zero_baseline:relative_floor" in row(generation, "Z").excluded_by
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == [
        "BIG",
        "MID",
        "SMALL",
    ]


def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two() -> None:
    symbols = ["Z", "A", "B"]
    today = {
        "A": day_bars("A", [10.5], volume=1_000),  # 10,500
        "B": day_bars("B", [10.5], volume=3_000),  # 31,500
    }
    baselines = {"A": baseline_of("A"), "B": baseline_of("B"), "Z": baseline_of("Z", volume=0)}
    for volume, admitted in [
        (2_000, True),
        (1_999, False),
    ]:  # 21,000 is the floor; Z's close is 10.5
        generation = ranked(
            symbols, {**today, "Z": day_bars("Z", [10.5], volume=volume)}, baselines=baselines
        )
        assert row(generation, "Z").eligible is admitted


def test_without_an_established_baseline_the_paired_arm_does_not_rank() -> None:
    generation = ranked(
        ["PART", "REAL"],
        {"PART": day_bars("PART", [11.0], volume=50_000), "REAL": day_bars("REAL", [11.0])},
        baselines={"PART": baseline_traded_in("PART", 4), "REAL": baseline_of("REAL")},
        zero_baseline_arm="exclude",
    )
    assert "zero_baseline:excluded_arm" in row(generation, "PART").excluded_by
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["REAL"]


def test_a_baseline_without_trade_counts_is_unknown_not_established() -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [11.0])},
        baselines={"A": replace(baseline_of("A"), traded_sessions=None)},
    )
    r = row(generation, "A")
    assert r.established is None
    assert "established:unknown" in r.excluded_by
    assert top_entries(generation) == []


@pytest.mark.parametrize("kind", ["zero", "partial"])
def test_the_earlier_tier_cases_hold_for_a_name_without_an_established_baseline(kind: str) -> None:
    """The five zero-baseline cases, restated: a name whose baseline is not established, zero or not,
    is in the tier, ordered by dollars, admitted at or above the floor, excluded by the paired arm, and
    unranked with no dollars today."""

    def tier(symbol: str) -> Baseline:
        return baseline_of(symbol, volume=0) if kind == "zero" else baseline_traded_in(symbol, 4)

    # ordered by dollars when no established name qualifies
    generation = ranked(
        ["A", "B"],
        {"A": day_bars("A", [11.0]), "B": day_bars("B", [12.0], volume=2_000)},
        baselines={s: tier(s) for s in ("A", "B")},
    )
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["B", "A"]
    # at or above the floor, the tier precedes the established names
    generation = ranked(
        ["T", "Q"],
        {s: day_bars(s, [11.0]) for s in ("T", "Q")},
        baselines={"T": tier("T"), "Q": baseline_of("Q")},
    )
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["T", "Q"]
    # below it, not ranked
    generation = ranked(
        ["T", "Q"],
        {"T": day_bars("T", [11.0]), "Q": day_bars("Q", [12.0])},
        baselines={"T": tier("T"), "Q": baseline_of("Q")},
    )
    assert "zero_baseline:relative_floor" in row(generation, "T").excluded_by
    # the paired arm excludes it
    generation = ranked(
        ["T"], {"T": day_bars("T", [11.0])}, baselines={"T": tier("T")}, zero_baseline_arm="exclude"
    )
    assert "zero_baseline:excluded_arm" in row(generation, "T").excluded_by
    # no dollars today: no ratio and no rank
    generation = ranked(["T"], {"T": day_bars("T", [11.0], volume=0)}, baselines={"T": tier("T")})
    assert row(generation, "T").ratio is None and top_entries(generation) == []


def test_the_baseline_file_carries_trade_counts_lookback_and_a_writers_header(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline.json"
    baselines = {"A": baseline_of("A"), "B": baseline_traded_in("B", 3)}
    write_baselines(path, SESSION, baselines, header={"source": "test", "excluded": ["X"]})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["lookback_sessions"] == BASELINE_SESSIONS
    assert document["source"] == "test" and document["excluded"] == ["X"]
    assert document["symbols"]["B"]["traded_sessions"][54] == 3
    read = read_baselines(path, SESSION)
    assert read.problem is None and read.baselines == baselines


def test_a_header_may_not_replace_a_key_of_the_format(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    with pytest.raises(ValueError, match="symbols"):
        write_baselines(path, SESSION, {}, header={"symbols": {}})
    assert not path.exists()


def test_a_baseline_file_without_trade_counts_is_read_without_them(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    document = {
        "format": movers_rank.BASELINE_FORMAT,
        "session": SESSION.isoformat(),
        "part_start": "20:00",
        "segment_minutes": 15,
        "segments": SEGMENTS,
        "lookback_sessions": BASELINE_SESSIONS,
        "symbols": {"A": {"sessions": [PRIOR.isoformat()], "cumulative": [1.0] * SEGMENTS}},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    read = read_baselines(path, SESSION)
    assert read.problem is None and read.baselines["A"].traded_sessions is None


@pytest.mark.parametrize(
    "change",
    [
        {"lookback_sessions": BASELINE_SESSIONS + 1},
        {"lookback_sessions": None},  # trade counts with no lookback to read them against
        {"traded": [1.5] * SEGMENTS},
        {"traded": [True] * SEGMENTS},
        {"traded": [1] * (SEGMENTS - 1)},
    ],
    ids=["other-lookback", "no-lookback", "float", "bool", "short"],
)
def test_a_baseline_file_with_counts_it_cannot_mean_is_unreadable(
    tmp_path: Path, change: dict[str, object]
) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    document = json.loads(path.read_text(encoding="utf-8"))
    if "lookback_sessions" in change:
        if change["lookback_sessions"] is None:
            del document["lookback_sessions"]
        else:
            document["lookback_sessions"] = change["lookback_sessions"]
    if "traded" in change:
        document["symbols"]["A"]["traded_sessions"] = change["traded"]
    path.write_text(json.dumps(document), encoding="utf-8")
    read = read_baselines(path, SESSION)
    assert read.baselines == {} and read.problem


def test_a_baseline_file_written_before_trade_counts_says_so(tmp_path: Path) -> None:
    """Code review of the definitions, finding 3: a file from the earlier writer has no lookback, so no
    name in it can be established; the read says why instead of every row failing silently."""
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")})
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["lookback_sessions"]
    del document["symbols"]["A"]["traded_sessions"]
    path.write_text(json.dumps(document), encoding="utf-8")
    read = read_baselines(path, SESSION)
    assert read.baselines == {} and read.problem and "lookback_sessions" in read.problem


@pytest.mark.parametrize(("reference", "last"), [(69.0, 71.07), (138.0, 142.14), (251.0, 258.53)])
def test_an_exact_three_percent_move_passes_the_condition(reference: float, last: float) -> None:
    """Finding 5: these are +3.000% exactly, and the quotient is a hair below 0.03 in binary."""
    assert last / reference - 1.0 < movers_rank.MOVE_CONDITION
    generation = ranked(
        ["A"], {"A": day_bars("A", [last])}, references={"A": Reference(reference, PRIOR)}
    )
    assert "move_condition" not in row(generation, "A").excluded_by


def test_the_median_is_of_established_movers_whatever_their_other_filters() -> None:
    """Finding 6: the lead's floor is the median of the established names passing the move condition;
    a big mover failing another filter still sets it, so a thin tier name cannot take the top."""
    symbols = ["Z", "BIG", "SMALL"]
    generation = ranked(
        symbols,
        {
            "Z": day_bars("Z", [10.5], volume=2_000),  # 21,000
            "BIG": day_bars("BIG", [10.5], volume=1_000_000),  # 10,500,000
            "SMALL": day_bars("SMALL", [10.5], volume=100),  # 1,050
        },
        baselines={**{s: baseline_of(s) for s in symbols}, "Z": baseline_of("Z", volume=0)},
        references={s: Reference(10.0, PRIOR) for s in symbols},
        security_types={"Z": SecurityType.SHARE, "SMALL": SecurityType.SHARE},  # BIG's is unknown
    )
    assert not row(generation, "BIG").eligible
    # the median of 10,500,000 and 1,050 is far above Z's 21,000
    assert "zero_baseline:relative_floor" in row(generation, "Z").excluded_by
    assert [e.instrument_id.symbol.value for e in top_entries(generation)] == ["SMALL"]


def test_unknown_trade_counts_are_one_reason_not_three() -> None:
    """Finding 10: the ratio is still a measurement; only establishment is unknown."""
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [11.0])},
        baselines={"A": replace(baseline_of("A"), traded_sessions=None)},
    )
    r = row(generation, "A")
    assert r.excluded_by == ("established:unknown",)
    assert r.ratio is not None and math.isfinite(r.ratio)


def test_task_m_gap_4_stale_prices_are_unknown_even_with_a_fresh_peer() -> None:
    generation = ranked(
        ["OLD", "FRESH"],
        {"OLD": day_bars("OLD", [15.0]), "FRESH": bars("FRESH", SESSION, time(9, 34), [12.0])},
        computed_ns=ns(SESSION, time(9, 35, 5)),
    )
    assert [str(entry.instrument_id) for entry in top_entries(generation)] == ["FRESH.ALPACA"]
    stale = row(generation, "OLD")
    assert stale.last_bar_ns == ns(SESSION, time(9, 31))
    assert stale.last is None
    assert stale.move is None
    assert stale.move_from_open is None
    assert stale.ratio is None
    assert stale.score is None
    assert "last_bar:stale" in stale.excluded_by


@pytest.mark.parametrize(("extra_ns", "eligible"), [(0, True), (1, False)])
def test_task_m_gap_4_the_age_bound_includes_its_exact_deadline(
    extra_ns: int, eligible: bool
) -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [12.0])},
        computed_ns=ns(SESSION, time(9, 34)) + extra_ns,
    )
    assert row(generation, "A").eligible is eligible
    assert bool(top_entries(generation)) is eligible


@pytest.mark.parametrize(("bound", "eligible"), [(60.0, False), (300.0, True)])
def test_task_m_gap_4_the_age_bound_is_a_setting(bound: float, eligible: bool) -> None:
    generation = ranked(
        ["A"],
        {"A": day_bars("A", [12.0])},
        computed_ns=ns(SESSION, time(9, 33, 5)),
        max_data_age_secs=bound,
    )
    assert row(generation, "A").eligible is eligible
    assert bool(top_entries(generation)) is eligible


@pytest.mark.parametrize("bound", [0.0, -1.0, float("nan"), float("inf")])
def test_task_m_gap_4_the_age_bound_must_be_finite_and_positive(bound: float) -> None:
    with pytest.raises(InvalidGeneration, match="data age"):
        ranked(["A"], {"A": day_bars("A", [12.0])}, max_data_age_secs=bound)


@pytest.mark.parametrize("sunday", [date(2026, 11, 1), date(2026, 3, 8)], ids=["fall", "spring"])
def test_a_part_across_a_clock_change_is_refused(sunday: date) -> None:
    """Code review of the overnight part, finding 3: no trade date's part spans a Sunday morning, so a
    part that is not 24 hours long is not a trade date's; it was filed by elapsed time into the wrong
    segment, or past the last."""
    items = bars("A", sunday, time(19, 30), [10.0])
    with pytest.raises(InvalidGeneration, match="hours"):
        segment_baseline({sunday: items}, SESSION)
    with pytest.raises(InvalidGeneration, match="hours"):
        tally(items, session=sunday)


@pytest.mark.parametrize("monday", [date(2026, 11, 2), date(2026, 3, 9)], ids=["fall", "spring"])
def test_the_monday_after_a_clock_change_starts_at_2000_sunday(monday: date) -> None:
    sunday = monday - timedelta(days=1)
    items = [*bars("A", sunday, time(20, 0), [10.0]), *bars("A", monday, time(19, 59), [10.0])]
    t = tally(items, session=monday)
    assert t.dollar_volume == pytest.approx(20_000.0)
    b = segment_baseline({monday: items}, monday + timedelta(days=1))
    assert b.traded_sessions is not None
    assert [k for k, n in enumerate(b.traded_sessions) if n] == [0, 95]
    with pytest.raises(InvalidGeneration, match="trade date"):
        tally(bars("A", sunday, time(19, 59), [10.0]), session=monday)


def test_only_a_version_1_file_is_named_as_one(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"format": "something else"}), encoding="utf-8")
    problem = read_baselines(path, SESSION).problem
    assert problem and "version 1" not in problem
    path.write_text(json.dumps({"format": "canon-movers-baseline/1"}), encoding="utf-8")
    problem = read_baselines(path, SESSION).problem
    assert problem and "version 1" in problem


# --- the lead's rule of 2026-11-11: a ratio's numerator counts exactly the clock minutes its baseline
# covers (code review of the overnight part, finding 1)


def usual(symbol: str, *, overnight: bool) -> Baseline:
    """Five sessions, each 1,500,000 from 09:30 to 09:45 and, with `overnight`, 3,000,000 at 21:00 the
    evening before."""
    days = sorted(weekdays_before(SESSION, BASELINE_SESSIONS))
    return segment_baseline(
        {
            d: [
                *(
                    bars(symbol, d - timedelta(days=1), time(21, 0), [10.0], 300_000)
                    if overnight
                    else []
                ),
                *bars(symbol, d, time(9, 30), [10.0] * 15, 10_000),
            ]
            for d in days
        },
        SESSION,
    )


TODAY_WITH_A_NIGHT = {
    "A": [
        *bars("A", PRIOR, time(21, 0), [10.0], 300_000),  # 3,000,000 overnight
        *bars("A", SESSION, time(9, 30), [10.0] * 15, 10_000),  # 1,500,000 to 09:45
    ]
}


@pytest.mark.parametrize("overnight_in_file", [False, True], ids=["sip-only", "with-night"])
def test_without_an_overnight_feed_the_day_ratio_counts_from_0400(overnight_in_file: bool) -> None:
    """(a) The header lists no overnight feed: the night's dollars are kept apart and the day's ratio is
    against the day's usual, 1.0, whatever the file holds before 04:00."""
    r = row(
        ranked(
            ["A"],
            TODAY_WITH_A_NIGHT,
            baselines={"A": usual("A", overnight=overnight_in_file)},
            overnight_baseline=False,
        ),
        "A",
    )
    assert r.ratio_span == "04:00-now"
    assert r.dollar_volume == pytest.approx(1_500_000.0)
    assert r.dollars_before_day == pytest.approx(3_000_000.0)
    assert r.ratio == pytest.approx(1.0)


def test_with_an_overnight_feed_both_sides_count_from_2000() -> None:
    """(b) A baseline built with the overnight feed: both sides include the night."""
    r = row(
        ranked(
            ["A"],
            TODAY_WITH_A_NIGHT,
            baselines={"A": usual("A", overnight=True)},
            overnight_baseline=True,
        ),
        "A",
    )
    assert r.ratio_span == "20:00-now"
    assert r.dollar_volume == pytest.approx(4_500_000.0)
    assert r.ratio == pytest.approx(1.0)


def test_an_overnight_clock_counts_from_2000_either_way() -> None:
    r = row(ranked(["A"], {"A": bars("A", PRIOR, time(21, 0), [11.0])}), "A")
    assert r.ratio_span == "20:00-now"
    assert r.dollar_volume == pytest.approx(11_000.0)


@pytest.mark.parametrize(
    ("header", "overnight"),
    [
        (None, False),
        ({"feed": "sip"}, False),
        ({"feed": "sip", "feeds": ["sip"]}, False),
        ({"feed": "sip", "feeds": ["sip", "boats"]}, True),
    ],
)
def test_the_file_says_whether_its_baseline_holds_the_overnight_feed(
    tmp_path: Path, header: dict[str, object] | None, overnight: bool
) -> None:
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")}, header=header)
    read = read_baselines(path, SESSION)
    assert read.problem is None and read.overnight is overnight


def test_the_span_is_the_generations_so_a_name_last_seen_before_0400_counts_no_night_by_day() -> (
    None
):
    """Code review of the span, finding 2: at 10:00 every name's ratio counts from 04:00, so a name whose
    last bar was at 03:50 brings no night dollars into the tier or its floor."""
    generation = ranked(
        ["NIGHT", "DAY"],
        {
            "NIGHT": bars("NIGHT", SESSION, time(3, 49), [11.0], volume=500_000),
            "DAY": bars("DAY", SESSION, time(9, 59), [11.0], volume=1_000),
        },
        # NIGHT's 03:50 bar is six hours old at 10:00: a bound past it, so only the span decides
        max_data_age_secs=7 * 3600,
    )
    night = row(generation, "NIGHT")
    assert night.ratio_span == "04:00-now"
    assert night.dollar_volume == 0.0 and night.dollars_before_day == pytest.approx(5_500_000.0)
    assert "NIGHT" not in [e.instrument_id.symbol.value for e in top_entries(generation)]


def test_the_days_dollars_are_added_up_not_left_over_from_the_night() -> None:
    """Finding 4: a large night less itself loses the day's small dollars."""
    t = tally(
        [
            *bars("A", PRIOR, time(21, 0), [1_000_000.0], volume=1_000_000_000),  # 1e15
            *bars("A", SESSION, time(9, 30), [0.3], volume=1),
        ]
    )
    assert t.dollars_from_day == pytest.approx(0.3)


def test_a_generation_at_0400_exactly_still_counts_from_2000() -> None:
    """Finding 5: the span moves to 04:00 only once the data time is past it."""
    r = row(ranked(["A"], {"A": bars("A", SESSION, time(3, 59), [11.0])}), "A")
    assert r.ratio_span == "20:00-now" and r.dollar_volume == pytest.approx(11_000.0)


@pytest.mark.parametrize("feeds", ["sip,boats", [1], {"boats": True}])
def test_a_feeds_header_that_is_not_a_list_of_names_is_refused(
    tmp_path: Path, feeds: object
) -> None:
    """Finding 9: a feed list the reader cannot read would silently drop the night."""
    path = tmp_path / "baseline.json"
    write_baselines(path, SESSION, {"A": baseline_of("A")}, header={"feeds": feeds})
    read = read_baselines(path, SESSION)
    assert read.baselines == {} and read.problem and "feeds" in read.problem
