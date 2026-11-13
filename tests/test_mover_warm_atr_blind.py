"""F-2: ATR history is isolated to a slot, before its first entry (offline).

Contracts read at NautilusTrader d53849b216e2: Strategy.request_bars and
on_historical_bars (python/nautilus_trader/trading/__init__.pyi:683,993), payload then
on_data_response then completion callback (crates/common/src/python/response.rs:133-171),
and AverageTrueRange.handle_bar/reset (crates/indicators/src/volatility/atr.rs:89-101).
The main branch's test_mover cash venue supplies instruments, cache and order factory;
the clock, subscriptions, responses and unsubmitted orders stay entirely in memory.
No history is fed to an indicator by these fixtures: Mover must perform the warm-up.
"""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from canon_alpaca.providers import DATA_API
from canon_alpaca.providers import RequestCounter
from nautilus_trader.core import UUID4
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Money
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId

from canon_trading import mover
from canon_trading import session_window
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import USD
from tests.test_mover import Recording
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import entry_day
from tests.test_mover import rank
from tests.test_mover import strategy

D = Decimal


def distance_counts(s: Any) -> tuple[int | None, int | None, int | None]:
    """ONE naming assumption for L6: the new END-record aggregate, absent on main.

    Repoint only this helper if the builder chooses another record key or bucket names.
    A missing aggregate stays visibly missing; never synthesize it from the entries.
    """
    record = s.record()
    if "entries_by_trail_source" not in record:
        return None, None, None
    # the builder's aggregate holds the buckets that occurred: an absent bucket is none of them
    counts = record["entries_by_trail_source"]
    return counts.get("atr", 0), counts.get("spread floor", 0), counts.get("cold_atr_fallback", 0)


class Probe(Recording):
    """Real Mover decisions, with history requests captured instead of sent to a client."""

    def __init__(self, config):
        super().__init__(config)
        self.asked: list[dict[str, Any]] = []
        self.placed: list[Any] = []
        self.latest: dict[Any, QuoteTick] = {}
        self.http = RequestCounter(clock=lambda: self.clock.timestamp_ns() / SECOND)

    @property
    def cache(self) -> Any:
        native = super().cache
        latest = self.latest

        class View:
            def quote(self, instrument_id):
                return latest.get(instrument_id)

            def __getattr__(self, name):
                return getattr(native, name)

        return View()

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ):
        request_id = str(UUID4())
        # This is a transport observation, not a network send. It exercises the existing
        # window budget's consumption of the adapter's counter, not adapter integration.
        prepared = requests.Request("GET", "https://data.alpaca.markets/v2/stocks/bars").prepare()
        self.http.sent(prepared)
        self.asked.append(
            {
                "id": request_id,
                "bar_type": bar_type,
                "start": start,
                "end": end,
                "limit": limit,
                "client_id": client_id,
                "params": params,
                "callback": callback,
                "prepared": prepared,
            }
        )
        return request_id

    def submit_order(self, order, position_id=None, client_id=None, params=None):
        self.placed.append(order)

    def _trading_account(self, venue):
        return SimpleNamespace(
            calculate_account_state=True,
            balance_total=lambda _: Money(100_000, USD),
            balance_free=lambda _: Money(100_000, USD),
        )


@pytest.fixture
def make_probe(monkeypatch):
    eng = engine()
    made = 0

    def make(**rows: Any) -> Any:
        nonlocal made
        made += 1
        settings = {
            "strategy_id": StrategyId(f"Mover-{made:03}"),
            "order_id_tag": f"{made:03}",
            "trail_mode": "atr",
            "trail_cap": "own",
            "atr_n": 14,
            "atr_k": D(1),
            "size_from_budget": True,
            "feed": "sip",
            "bar_spec": "1-MINUTE-LAST-INTERNAL",
        }
        settings.update(rows)
        s = Probe(strategy(**settings).settings)
        eng.add_strategy(s)
        s.clock.set_time(T0)
        for method in (
            "subscribe_quotes",
            "subscribe_trades",
            "subscribe_bars",
            "unsubscribe_quotes",
            "unsubscribe_trades",
            "unsubscribe_bars",
        ):
            monkeypatch.setattr(s, method, lambda *args, **kwargs: None)
        return s

    yield make
    eng.dispose()


def watch(s, *instruments) -> None:
    now = s.clock.timestamp_ns()
    s.on_signal(
        SimpleNamespace(
            name=mover.RANK_SIGNAL,
            value=rank(*((instrument, "0.25") for instrument in instruments)),
            ts_event=now,
            ts_init=now,
        )
    )
    s._watch(now)


def bars(instrument=AAA, *, start=-14, count=14, width="0.08", internal=False) -> list[Bar]:
    """Flat closes keep the expected ATR equal to width; times name complete minutes.

    Internal bars stamp their close, while the adapter's external history stamps its
    open. Thus matching overlap by ts_event alone would count the same minute twice.
    """
    kind = (
        BarType.from_str(f"{instrument}-1-MINUTE-LAST-INTERNAL")
        if internal
        else bar_type(instrument)
    )
    half = D(width) / 2
    result = []
    for minute in range(start, start + count):
        opened = T0 + minute * MINUTE
        closed = opened + MINUTE
        result.append(
            Bar(
                kind,
                Price(10, 4),
                Price(float(D(10) + half), 4),
                Price(float(D(10) - half), 4),
                Price(10, 4),
                Quantity.from_int(100_000),
                closed if internal else opened,
                closed,
            )
        )
    return result


def reply(s, history, *, request=0, failure=None) -> None:
    """Deliver the existing callbacks in the pin's order, then let the timer consume them.

    Main requests nothing; an unsolicited payload still reaches its existing no-op
    historical handler so missing warm-up fails as a behavioral assertion, not indexing.
    This does not emulate upstream's feeding of *registered* indicators (the strategy registers none).
    """
    sent = s.asked[request] if len(s.asked) > request else {}
    request_id = sent.get("id", "00000000-0000-4000-8000-000000000001")
    if failure is None:
        s.on_historical_bars(history)
    elif failure.startswith("429") and sent:
        s.http.answered(sent["prepared"], 429)
    s.on_data_response(
        SimpleNamespace(
            correlation_id=request_id,
            completed=True,
            failure_reason=failure,
            data=history if failure is None else None,
            params=sent.get("params"),
            client_id=sent.get("client_id"),
            start=None,
            end=None,
            ts_init=s.clock.timestamp_ns(),
        )
    )
    if callback := sent.get("callback"):
        callback(request_id)
    s._on_step(SimpleNamespace(ts_event=s.clock.timestamp_ns()))


def live(s, history) -> None:
    for bar in history:
        s.clock.set_time(bar.ts_init)
        s.on_bar(bar)


def other_state(s, slot) -> tuple:
    return (
        tuple(slot.closes),
        tuple(slot.dollars),
        slot.run_ns,
        slot.run_close,
        slot.run_seen_ns,
        slot.entry_start,
        slot.entry_day,
        s.live_bars,
        deepcopy(s.entry_contexts),
        deepcopy(s.entries_sent),
    )


def first_entry(s, *, spread="0.002", count=4, width="0.04", start=0):
    """A qualifying live run, followed by the real timer-side entry decision and record."""
    kind = s.slots[AAA].bar_type
    for i in range(count):
        close = D(10) + D(i) / 100
        closed = T0 + (start + i + 1) * MINUTE
        bar = Bar(
            kind,
            Price(float(close), 4),
            Price(float(close + D(width) / 2), 4),
            Price(float(close - D(width) / 2), 4),
            Price(float(close), 4),
            Quantity.from_int(100_000),
            closed if kind.is_internally_aggregated() else closed - MINUTE,
            closed,
        )
        s.clock.set_time(closed)
        s.on_bar(bar)
        s.latest[AAA] = QuoteTick(
            AAA,
            Price(float(close - D(spread)), 4),
            Price(float(close), 4),
            Quantity.from_int(10_000),
            Quantity.from_int(10_000),
            closed,
            closed,
        )
    watch(s, AAA)  # fresh ranking at the decision, without making a second subscription
    s._enter(s.clock.timestamp_ns(), 0)
    assert s.entries_sent, (s.dropped, s.said)
    return s.entries_sent[-1]


@pytest.mark.parametrize("count", [14, 28])
def test_first_entry_uses_history_atr_before_fourteen_live_minutes(make_probe, count):
    s = make_probe()
    watch(s, AAA)
    reply(s, bars(start=-count, count=count))
    entry = first_entry(s)
    assert entry["trail_source"] == "atr"
    assert entry["atr"] is not None and D(entry["atr"]) > 0
    assert s.history_bars == count
    assert s.live_bars == 4
    assert entry["submitted_ns"] - s.slots[AAA].subscribed_ns < 14 * MINUTE


@pytest.mark.parametrize("arm", ["bridge", "registered"])
def test_history_changes_only_atr_not_closes_flow_or_entry_arms(make_probe, arm):
    s = make_probe(entry_arm=arm)
    watch(s, AAA)
    s.entry_contexts[AAA] = entry_day([10.0, 10.1, 10.2, 10.3])
    slot = s.slots[AAA]
    before = other_state(s, slot)
    reply(s, bars())
    assert other_state(s, slot) == before
    assert (slot.atr.count, s.history_bars) == (14, 14)
    assert slot.atr.value == pytest.approx(0.08)
    assert not s.placed


def test_another_slots_response_warms_only_its_own_atr(make_probe):
    s = make_probe(slots=2, watch=2)
    watch(s, AAA, BBB)
    reply(s, bars(BBB, width="0.40"), request=1)
    assert s.slots[AAA].atr.count == 0
    assert not s.slots[AAA].atr.initialized
    assert s.slots[BBB].atr.count == 14
    assert s.slots[BBB].atr.value == pytest.approx(0.40)
    reply(s, bars(AAA, width="0.08"), request=0)
    assert s.slots[AAA].atr.value == pytest.approx(0.08)
    assert s.slots[BBB].atr.value == pytest.approx(0.40)
    assert s.history_bars == 28


def test_a_duplicate_bar_cannot_turn_thirteen_history_minutes_into_fourteen(make_probe):
    s = make_probe()
    watch(s, AAA)
    history = bars(start=-13, count=13)
    reply(s, history[:7] + history[6:])
    slot = s.slots[AAA]
    assert not slot.atr.initialized
    assert (slot.atr.count, s.history_bars) == (13, 13)


def test_a_repeated_response_is_counted_once_even_after_a_live_bar(make_probe):
    s = make_probe()
    watch(s, AAA)
    history = bars()
    reply(s, history)
    live_bars = bars(start=0, count=1, width="0.20", internal=True)
    live(s, live_bars)
    reply(s, history)
    expected = AverageTrueRange(14)
    for bar in history + live_bars:
        expected.handle_bar(bar)
    assert (s.history_bars, s.slots[AAA].atr.count, s.live_bars) == (14, 15, 1)
    assert s.slots[AAA].atr.value == pytest.approx(expected.value)


def test_a_late_response_drops_overlapping_live_minutes_and_preserves_time_order(make_probe):
    s = make_probe()
    watch(s, AAA)
    live_bars = bars(start=0, count=2, width="0.20", internal=True)
    live(s, live_bars)
    before = other_state(s, s.slots[AAA])
    history = bars()
    # Different OHLC makes retaining the history's duplicate observably wrong.
    overlap = bars(start=0, count=2, width="0.90")
    reply(s, history + overlap)
    assert other_state(s, s.slots[AAA]) == before
    assert (s.history_bars, s.history_dropped, s.slots[AAA].atr.count) == (14, 2, 16)
    expected = AverageTrueRange(14)
    for bar in history + live_bars:
        expected.handle_bar(bar)
    assert s.slots[AAA].atr.value == pytest.approx(expected.value)


def test_a_retired_slots_late_response_cannot_warm_its_replacement(make_probe):
    s = make_probe()
    watch(s, AAA)
    s._close_slot(s.slots[AAA])
    s.clock.set_time(T0 + 2 * MINUTE)
    watch(s, AAA)
    reply(s, bars(width="0.90"), request=0)
    assert (s.history_bars, s.slots[AAA].atr.count) == (0, 0)
    reply(s, bars(start=-12, width="0.08"), request=1)
    assert (s.history_bars, s.slots[AAA].atr.count) == (14, 14)
    assert s.slots[AAA].atr.value == pytest.approx(0.08)


@pytest.mark.parametrize("count", [0, 5, 9])
def test_short_history_keeps_and_counts_the_named_cold_first_entry(make_probe, count):
    s = make_probe()
    watch(s, AAA)
    reply(s, bars(start=-count, count=count))
    entry = first_entry(s)
    assert entry["trail_source"] == "cold_atr_fallback"
    assert entry["trail"] == "0.02" and entry["atr"] is None
    assert not s.slots[AAA].atr.initialized  # at most thirteen history + live bars
    assert distance_counts(s) == (0, 0, 1)
    assert s.history_bars == count


def test_one_request_per_opening_reaches_the_window_request_budget(make_probe, monkeypatch):
    s = make_probe()
    watch(s, AAA)
    opening_counts = [len(s.asked)]
    s.clock.set_time(T0 + SECOND)
    watch(s, AAA)
    opening_counts.append(len(s.asked))
    s._close_slot(s.slots[AAA])
    s.clock.set_time(T0 + 2 * SECOND)
    watch(s, AAA)
    opening_counts.append(len(s.asked))
    assert opening_counts == [1, 1, 2]
    for sent in s.asked:
        assert sent["bar_type"] == bar_type(AAA)
        assert sent["params"] == {"feed": "sip"}
        assert sent["start"].tzinfo is not None
        assert sent["start"] < datetime.fromtimestamp(T0 / SECOND, UTC)
        assert sent["end"] is None or sent["end"] <= s.clock.utc_now()
        assert sent["limit"] is None  # ascending history must not truncate to the oldest 14
    monkeypatch.setattr(session_window, "request_counts", s.http.counts)
    monkeypatch.setattr(session_window, "endpoint_counts", s.http.endpoint_counts)
    monkeypatch.setattr(session_window, "observation_seconds", lambda: 60.0)
    monkeypatch.setattr(session_window, "condition_counts", dict)
    report: dict[str, Any] = {
        "result": "ok",
        "ended": "2025-11-10T16:01:00+00:00",
        "request_budget": {},
        "mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}},
    }
    assert session_window._snapshot_requests(report) is None
    data = [row for row in report["requests_by_api"] if row["api"] == DATA_API]
    assert len(data) == 1 and data[0]["total"] == 2
    assert sum(report["mover"]["http_requests"]["to_window_end"].values()) == 2


@pytest.mark.parametrize(
    "failure", ["429 Too Many Requests", "bar requests: feed overnight refused"]
)
def test_a_failed_history_request_is_named_and_the_slot_stays_cold(make_probe, failure):
    s = make_probe()
    watch(s, AAA)
    reply(s, [], failure=failure)
    assert not s.slots[AAA].atr.initialized
    assert s.history_bars == 0
    recorded = str(s.record()) + " ".join(s.said)
    named = failure in recorded
    assert named, f"warm-up failure was not recorded: {failure}"
    entry = first_entry(s)
    assert entry["trail_source"] == "cold_atr_fallback"
    assert len(s.asked) == 1
    assert distance_counts(s) == (0, 0, 1)


@pytest.mark.parametrize("mode", ["atr", "tier", "ratchet"])
def test_fixed_mode_requests_nothing_with_a_non_fixed_positive_control(make_probe, mode):
    fixed = make_probe(trail_mode="fixed")
    active = make_probe(trail_mode=mode)
    watch(fixed, AAA)
    watch(active, AAA)
    assert fixed.asked == []
    assert fixed.slots[AAA].atr is None
    assert (fixed.history_bars, fixed.history_dropped) == (0, 0)
    # Fixed alone already passes on main; the positive control prevents a no-op warmer
    # from satisfying this mode distinction, without deliberately breaking fixed behavior.
    assert len(active.asked) == 1


def test_end_record_counts_atr_spread_floor_and_cold_entries_once(make_probe):
    s = make_probe()
    watch(s, AAA)
    # Live warm-up isolates the new aggregate from the new historical warm-up. Main can
    # already produce all three entry sources, including a cold fallback raised to a floor.
    first = first_entry(s, count=14, width="0.08")
    s._close_slot(s.slots[AAA])
    watch(s, AAA)
    second = first_entry(s, count=14, start=14, spread="0.04", width="0.002")
    s._close_slot(s.slots[AAA])
    watch(s, AAA)
    third = first_entry(s, count=4, start=28, spread="0.40")
    assert [entry["trail_source"] for entry in (first, second, third)] == [
        "atr",
        "spread floor",
        "cold_atr_fallback",
    ]
    assert third["trail_bound"] == "spread floor"
    assert len(s.entries_sent) == 3
    assert distance_counts(s) == (1, 1, 1)  # the cold floor is cold, not a second floor entry
    assert distance_counts(s) == (1, 1, 1)  # reading the END record must not increment it
