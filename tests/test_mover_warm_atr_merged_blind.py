"""F-2 properties from the lead's 2026-11-13 00:10 ruling, written against 4971c474.

Contracts: NautilusTrader d53849b216e2 trading/__init__.pyi's request_bars,
on_historical_bars and on_data_response; common/src/python/response.rs delivers the
payload before completion; indicators/src/volatility/atr.rs supplies the real ATR.
Cache views follow test_mover.DeferredInstruments; the entry replay uses the existing
test_mover_opening cash-venue fixture. All bars, replies and HTTP counts are synthetic.
"""

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
from canon_alpaca import providers
from nautilus_trader.core import UUID4
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OrderSide

from canon_trading import mover
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover_opening import FALL_TURN
from tests.test_mover_opening import OPEN
from tests.test_mover_opening import opening_strategy
from tests.test_mover_opening import opening_tape
from tests.test_mover_trail import minute_bars
from tests.test_mover_trail import replace_bar

PERIOD = 14
D = Decimal


def cache_warm_record(record):
    """The ONLY assumed future names/shape; UNVERIFIED until F-2 supplies its record.

    Assume record['atr_warm_outcomes']['warm_from_cache'] counts cache-warm openings
    and record['atr_warm'] is a list of rows with 'cached' and 'bars' counts.
    Missing fields deliberately return absence, so the test fails an assertion,
    not an import/attribute/key error. Adapt only this binding if F-2 names differ.
    """
    # Re-pointed by L6 to F-2's record (the report's sanctioned adaptation): the outcomes are
    # record["warm_up"]["outcomes"], the rows record["warm_up"]["slots"]; a row's "bars" counts
    # history bars only, so the total warm input is its "cached" plus its "bars".
    warm_up = record.get("warm_up", {})
    return (
        warm_up.get("outcomes", {}).get("warm_from_cache", 0),
        [
            (row.get("cached"), (row.get("cached") or 0) + (row.get("bars") or 0))
            for row in warm_up.get("slots", [])
        ],
    )


class ObservedATR:
    """Observe the effective input series while delegating every calculation to rc8.

    A reset starts a new effective series, allowing either rebuilding or buffering
    a late answer. The tests prescribe the resulting series, not either algorithm.
    """

    def __init__(self, *args, **kwargs):
        self.native = AverageTrueRange(*args, **kwargs)
        self.minutes = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def handle_bar(self, bar):
        opened = bar.ts_init - MINUTE if bar.bar_type.is_internally_aggregated() else bar.ts_event
        self.minutes.append(opened)
        self.native.handle_bar(bar)

    def reset(self):
        self.minutes.clear()
        self.native.reset()


class HistoryReplay(Recording):
    """Existing strategy hooks with a cache view and an offline request/reply boundary."""

    def __init__(self, config):
        super().__init__(config)
        self.cached_bars = None
        self.requests = []
        self.replied = set()
        self.delivered = []
        self.auto_history = None
        self.opening_times = []

    @property
    def cache(self):
        native = super().cache
        cached = self.cached_bars

        class View:
            def bars(self, kind):
                if cached is not None and kind == bar_type(AAA):
                    return list(reversed(cached))  # Cache.bars is newest first at the pin.
                return native.bars(kind)

            def __getattr__(self, name):
                return getattr(native, name)

        return View()

    def _open_slot(self, instrument_id):
        self.opening_times.append(self.clock.timestamp_ns())
        super()._open_slot(instrument_id)

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ):
        request = SimpleNamespace(
            request_id=str(UUID4()),
            bar_type=bar_type,
            start=start,
            end=end,
            limit=limit,
            client_id=client_id,
            params=params,
            callback=callback,
            at=self.clock.timestamp_ns(),
        )
        self.requests.append(request)
        return request.request_id

    def answer(self, bars, request=None):
        # At the base there is no request. Still exercise its existing inherited
        # historical hook, so series tests fail for missing data, not a missing API.
        if request is None and self.requests:
            request = self.requests[0]
        correlation = str(UUID4()) if request is None else request.request_id
        self.delivered.extend(bars)
        self.on_historical_bars(bars)
        self.on_data_response(
            SimpleNamespace(
                correlation_id=correlation,
                sub_correlation_id=None,
                completed=True,
                failure_reason=None,
                kind=NautilusDataType.Bar,
                client_id=None if request is None else request.client_id,
                params=None if request is None else request.params,
                start=None if request is None or request.start is None else ns(request.start),
                end=None if request is None or request.end is None else ns(request.end),
                ts_init=self.clock.timestamp_ns(),
                data=bars,
            )
        )
        self.replied.add(correlation)
        if request is not None and request.callback is not None:
            request.callback(correlation)

    def _on_step(self, event):
        if self.auto_history is not None:
            for request in list(self.requests):
                if request.request_id in self.replied or event.ts_event <= request.at:
                    continue
                # Honor the requested time range: a request starting at 09:30
                # cannot accidentally receive the pre-market fixture.
                bars = [
                    bar
                    for bar in self.auto_history
                    if (request.start is None or bar.ts_event >= ns(request.start))
                    and (request.end is None or bar.ts_event < ns(request.end))
                    and bar.ts_init <= request.at
                ]
                self.answer(bars, request)
        super()._on_step(event)


def ns(at):
    return int(at.timestamp() * SECOND)


def bars_for(count, *, start=T0):
    # Unequal ranges/gaps make ordering and lost/duplicated bars affect Wilder ATR.
    closes = [10.0 + ((i * 7) % 11) / 10 for i in range(count)]
    return [replace_bar(bar, start) for bar in minute_bars(closes)]


@contextmanager
def opened(cached, *, now=T0 + PERIOD * MINUTE, mode="atr"):
    eng = engine()
    s = HistoryReplay(strategy(trail_mode=mode, atr_n=PERIOD, run_bars=50).settings)
    s.cached_bars = cached
    try:
        eng.add_strategy(s)
        # Same direct-call boundary as test_mover_trail's reopened-slot test:
        # an engine which has not run has no feed to subscribe/unsubscribe.
        for verb in ("subscribe", "unsubscribe"):
            for channel in ("quotes", "trades", "bars"):
                setattr(s, f"{verb}_{channel}", lambda *args, **kwargs: None)
        s.clock.set_time(now)
        s._open_slot(AAA)
        yield s, s.slots[AAA]
    finally:
        eng.dispose()


def step(s, now):
    s.clock.set_time(now)
    s._on_step(SimpleNamespace(ts_event=now))


def assert_series(slot, expected):
    assert slot.atr.minutes == [bar.ts_event for bar in expected], (
        "ATR must contain the chronological union, each minute once, including cache-only minutes"
    )
    reference = AverageTrueRange(PERIOD)
    for bar in expected:
        reference.handle_bar(bar)
    assert slot.atr.count == reference.count
    assert slot.atr.initialized == reference.initialized
    assert slot.atr.value == pytest.approx(reference.value)


@pytest.fixture(autouse=True)
def observe_atr(monkeypatch):
    monkeypatch.setattr(mover, "AverageTrueRange", ObservedATR)


@pytest.mark.parametrize("count", [PERIOD, PERIOD + 1])
def test_full_cache_is_warm_without_a_request_and_is_counted_as_cache_warm(count):
    cached = bars_for(count)
    with opened(cached, now=T0 + count * MINUTE) as (s, slot):
        assert slot.atr.initialized
        assert_series(slot, cached)
        assert s.requests == [], "a full cache must not spend a history request"
        assert cache_warm_record(s.record()) == (1, [(count, count)]), (
            "record one warm_from_cache outcome and the cached/bars counts for this opening"
        )


def test_a_live_repeat_of_a_cache_minute_does_not_feed_the_atr_again():
    cached = bars_for(PERIOD)
    with opened(cached) as (s, slot):
        last = cached[-1]
        duplicate = Bar(
            last.bar_type,
            last.open,
            last.high,
            last.low,
            last.close,
            last.volume,
            last.ts_event,
            last.ts_init + SECOND,  # same minute, a later receipt
        )
        s.clock.set_time(duplicate.ts_init)
        s.on_bar(duplicate)
        assert_series(slot, cached)


@pytest.mark.parametrize("live_before_reply", [0, 2], ids=["prompt", "two_minutes_late"])
def test_history_and_short_cache_form_one_ordered_series_even_after_live_bars(live_before_reply):
    all_bars = bars_for(PERIOD + 3)
    # History overlaps 10 and 11, but omits cache-only 12 and 13.
    cached, history = all_bars[10:14], all_bars[:12]
    with opened(cached) as (s, slot):
        for bar in all_bars[14 : 14 + live_before_reply]:
            s.clock.set_time(bar.ts_init)
            s.on_bar(bar)
        now = T0 + (PERIOD + live_before_reply) * MINUTE + SECOND
        s.clock.set_time(now)
        s.answer(history)
        step(s, now)
        expected = all_bars[: 14 + live_before_reply]
        assert_series(slot, expected)
        # Re-delivery on the live channel includes a history-only minute, an
        # overlap, and a cache-only minute. None may advance or rewind the ATR.
        for bar in (all_bars[8], all_bars[10], all_bars[13]):
            s.on_bar(bar)
        assert_series(slot, expected)
        following = all_bars[14 + live_before_reply]
        s.clock.set_time(following.ts_init)
        s.on_bar(following)
        assert_series(slot, [*expected, following])
        assert len(s.requests) == 1


def test_only_a_short_cache_requests_once_per_opening_including_after_reopen():
    observations = {}
    for mode, count, label in (
        ("fixed", 0, "fixed"),
        ("atr", PERIOD, "warm"),
        ("atr", PERIOD - 1, "short"),
    ):
        with opened(bars_for(count), mode=mode) as (s, slot):
            counts = [len(s.requests)]
            for second in (1, 2, 30, 61, 120):
                now = T0 + PERIOD * MINUTE + second * SECOND
                s.rank_ns, s.rank = now, [(AAA, D("0.25"))]
                step(s, now)
            counts.append(len(s.requests))
            s._close_slot(slot)
            s._open_slot(AAA)
            counts.append(len(s.requests))
            observations[label] = counts
    assert observations["fixed"] == [0, 0, 0]
    assert observations["warm"] == [0, 0, 0]
    assert observations["short"] == [1, 1, 2], (
        "one request for each short-cache opening, never another while that opening waits"
    )


def test_0931_opening_uses_premarket_history_for_its_first_entry_plan():
    eng = engine()
    template = opening_strategy(trail_mode="atr", atr_n=PERIOD)
    s = HistoryReplay(template.settings)
    s.baseline_dollar_volume_per_minute = dict(template.baseline_dollar_volume_per_minute)
    regular = opening_tape(FALL_TURN)
    s.auto_history = [
        *bars_for(20, start=OPEN - 20 * MINUTE),
        next(bar for bar in regular if isinstance(bar, Bar)),
    ]
    try:
        orders = run(eng, s, ranker(rank(), values={1: rank((AAA, "0.25"))}), regular)
        # The fixture's step timer opens at 09:31:01, within the specified minute.
        assert OPEN + MINUTE <= s.opening_times[0] < OPEN + 2 * MINUTE
        buys = [order for order in orders if order.side == OrderSide.BUY]
        assert len(buys) == 1 and buys[0].status.name == "FILLED"
        [entry] = s.record()["entries"]
        assert entry["arm"] == "opening"
        assert entry["trail_source"] != "cold_atr_fallback", (
            "the 09:31 slot must already be warm at its first entry from pre-market history"
        )
        assert entry["atr"] is not None
        assert len(s.requests) == 1
        assert s.requests[0].start < datetime.fromtimestamp(OPEN / SECOND, UTC)
        assert any(bar.ts_event < OPEN for bar in s.delivered)
    finally:
        eng.dispose()


def test_a_successful_history_answer_after_a_counted_429_still_warms_the_slot(monkeypatch):
    counter = providers.RequestCounter(clock=lambda: (T0 + PERIOD * MINUTE) / SECOND)
    monkeypatch.setattr(providers, "REQUESTS", counter)
    all_bars = bars_for(PERIOD)
    with opened(all_bars[10:14]) as (s, slot):
        # A client's recovered retry is represented at the existing accounting
        # boundary. No transport/client is started and the headers are empty.
        request = requests.Request(
            "GET", "https://data.alpaca.markets/v2/stocks/AAA/bars"
        ).prepare()
        counter.sent(request)
        counter.answered(request, 429)
        counter.sent(request)
        counter.answered(request, 200)
        s.answer(all_bars[:12])
        step(s, T0 + PERIOD * MINUTE + SECOND)
        [count] = providers.request_counts()
        assert (count.api, count.total, count.rate_limited_total) == ("data", 2, 1)
        assert slot.atr.initialized, "a recovered 429 must not discard a successful history answer"
        assert_series(slot, all_bars)
        assert len(s.requests) == 1, "the client retry must not trigger a second strategy request"
