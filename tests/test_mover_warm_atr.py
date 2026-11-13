"""F-2: each Mover slot's ATR is warmed from history when it opens under a non-fixed trail.

Offline, through the real `BacktestEngine` of `tests.test_mover`. The data client is not asked: the test
strategy keeps each `request_bars` call and answers it on a later step through `on_data_response`, as
upstream's response path hands a Python strategy a response (`crates/common/src/python/response.rs:133-170`
at `d53849b216e2`: `on_historical_bars`, then `on_data_response`, then the callback). No credential, no
network.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import AggregationSource
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import mover
from canon_trading.mover import MoverConfig
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MINUTE
from tests.test_mover import RISING
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import strategy
from tests.test_mover import tape

D = Decimal
NOW = datetime(2026, 11, 13, 4, 30, tzinfo=UTC)


def history(instrument_id: InstrumentId, count: int, *, last_open: int = T0, width: str = "0.04"):
    """`count` complete EXTERNAL minute bars of `instrument_id`, the last one opening at `last_open`,
    each `width` from low to high around 10, stamped as the adapter stamps a requested bar (`ts_event` its
    open, `ts_init` its close: `canon_alpaca.data.parse_bar`)."""
    kind = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
    half = D(width) / 2
    return [
        Bar(
            kind,
            Price.from_str("10.0000"),
            Price.from_str(str(D(10) + half)),
            Price.from_str(str(D(10) - half)),
            Price.from_str("10.0000"),
            Quantity.from_int(1000),
            last_open - (count - 1 - i) * MINUTE,
            last_open - (count - 2 - i) * MINUTE,
        )
        for i in range(count)
    ]


class Warming(Recording):
    """`Recording` whose bar requests are kept and answered `delay` after they are sent, with `answer`'s
    bars for the instrument (or `failure`), through the strategy's own `on_data_response`."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.asked: list[tuple[BarType, Any, dict | None, str]] = []
        self.ends: list[Any] = []
        """Each request's `end`, in the order asked."""
        self.answer: dict[InstrumentId, list] = {}
        self.failure: str | None = None
        self.refuse: Exception | None = None
        self.delay = SECOND
        self.due: list[tuple[int, str, InstrumentId]] = []

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ):
        if self.refuse is not None:
            raise self.refuse
        request_id = f"bars-{len(self.asked) + 1}"
        self.asked.append((bar_type, start, params, request_id))
        self.ends.append(end)
        self.due.append(
            (self.clock.timestamp_ns() + self.delay, request_id, bar_type.instrument_id)
        )
        return request_id

    def _on_step(self, event) -> None:
        for due in list(self.due):
            when, request_id, instrument_id = due
            if when <= event.ts_event:
                self.due.remove(due)
                self.on_data_response(
                    response(request_id, self.answer.get(instrument_id, []), self.failure)
                )
        super()._on_step(event)


def response(request_id: str, bars: list, failure: str | None = None, completed: bool = True):
    return SimpleNamespace(
        correlation_id=request_id, completed=completed, failure_reason=failure, data=bars
    )


def warming(**changes: object) -> Warming:
    return Warming(strategy(**changes).settings)


def first_entry(s: mover.Mover) -> dict:
    return s.record()["entries"][0]


# --- property 1: with at least atr_n history bars the first entry is planned from a warm ATR ----------


def test_a_slot_with_atr_n_history_bars_enters_on_a_warm_atr_not_the_cold_fallback():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.answer[AAA] = history(AAA, 14)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    entry = first_entry(s)
    assert entry["trail_source"] != "cold_atr_fallback"
    assert entry["atr"] is not None
    # The node's cache already holds the minute opening at T0 (the tape's first EXTERNAL bar): it is
    # fed once, from the cache, and the answer's bar of that minute is dropped (one series).
    assert (s.history_bars, s.history_dropped) == (13, 1)
    assert s.record()["warm_up"]["slots"][0]["cached"] == 1
    [(kind, _, params, _)] = s.asked
    assert kind == BarType(AAA, bar_type(AAA).spec, AggregationSource.EXTERNAL)
    assert params == s.feed_params
    eng.dispose()


def test_the_request_reaches_back_at_least_atr_n_minutes_and_its_bars_warm_only_that_slot():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14, slots=2, watch=2)
    s.answer[AAA] = history(AAA, 14)
    s.answer[BBB] = history(BBB, 3)
    run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.20"))),
        tape(AAA, [10.0] * 4),
        tape(BBB, [20.0] * 4),
    )
    starts = {kind.instrument_id: start for kind, start, _, _ in s.asked}
    assert set(starts) == {AAA, BBB}
    opened = s.history_loads[0].requested_ns
    assert all(opened / SECOND - start.timestamp() >= 14 * 60 for start in starts.values())
    aaa, bbb = s.slots[AAA].atr, s.slots[BBB].atr
    assert aaa is not None and aaa.initialized
    assert bbb is not None and not bbb.initialized
    eng.dispose()


# --- property 2: history bars change nothing else ----------------------------------------------------


def test_history_bars_change_no_close_flow_entry_arm_or_live_bar_count():
    seen = []
    for answer in ([], history(AAA, 14)):
        eng = engine()
        s = warming(trail_mode="atr", atr_n=14)
        s.answer[AAA] = answer
        run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        slot = s.slots[AAA]
        entry = first_entry(s)
        seen.append(
            (
                s.live_bars,
                list(slot.closes),
                list(slot.dollars),
                [line for line in s.said if "run " in line],
                entry["qualified_ns"],
                entry["run_close"],
                entry["submitted_ns"],
            )
        )
        eng.dispose()
    assert seen[0] == seen[1]


# --- property 3: another slot's, late, repeated or overlapping bars are not counted twice ------------


def unit(atr_n: int = 3) -> tuple[mover.Mover, mover.Slot, mover.HistoryLoad]:
    """A Mover with one open slot of AAA whose warm-up request `bars-1` is outstanding."""
    s = strategy(trail_mode="atr", atr_n=atr_n, atr_k=D(1))
    slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
    slot.atr = AverageTrueRange(atr_n)
    s.slots[AAA] = slot
    load = mover.HistoryLoad(AAA, T0, request_id="bars-1")
    slot.history = load
    s.history_loads.append(load)
    s.history_by_request["bars-1"] = load
    return s, slot, load


def live(open_ns: int, close: str = "10.0000", width: str = "0.04") -> Bar:
    half = D(width) / 2
    return Bar(
        bar_type(AAA),
        Price.from_str(close),
        Price.from_str(str(D(close) + half)),
        Price.from_str(str(D(close) - half)),
        Price.from_str(close),
        Quantity.from_int(1000),
        open_ns,
        open_ns + MINUTE,
    )


def test_a_repeated_response_is_not_counted_twice():
    s, slot, load = unit()  # each step after the history's last close, T0 + MINUTE
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s._finish_history(T0 + MINUTE + SECOND)
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s._finish_history(T0 + MINUTE + 2 * SECOND)
    assert (s.history_bars, s.history_dropped) == (3, 3)
    assert (load.bars, load.dropped) == (3, 3)
    assert slot.atr is not None and slot.atr.count == 3


def test_a_response_for_another_slot_or_a_closed_one_warms_nothing():
    s, slot, _ = unit()
    other = mover.Slot(BBB, bar_type(BBB), deque(maxlen=4), deque(maxlen=5))
    other.atr = AverageTrueRange(3)
    s.slots[BBB] = other
    s.on_data_response(response("bars-1", history(BBB, 3)))
    s._finish_history(T0 + SECOND)
    assert (s.history_bars, s.history_dropped) == (0, 3)
    assert slot.atr is not None and slot.atr.count == 0
    assert other.atr.count == 0


def test_an_answer_after_its_slot_closed_and_opened_again_warms_nothing_and_is_named():
    s, slot, first = unit()
    fresh = mover.HistoryLoad(AAA, T0 + MINUTE, request_id="bars-2")
    s.history_by_request["bars-2"] = fresh
    slot.history = fresh  # the slot closed and opened again before `bars-1` was answered
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s._finish_history(T0 + MINUTE + SECOND)
    assert slot.atr is not None and slot.atr.count == 0
    assert (s.history_bars, s.history_dropped) == (0, 3)
    assert first.outcome(3) == "slot_closed_first" and fresh.outcome(3) == "unanswered"
    s.on_data_response(response("bars-2", history(AAA, 3)))
    s._finish_history(T0 + MINUTE + 2 * SECOND)
    assert slot.atr.count == 3 and fresh.outcome(3) == "warm"


def test_a_warm_up_whose_slot_closed_before_any_answer_is_named():
    s, _, load = unit()
    del s.slots[AAA]  # the ranking dropped AAA before its warm-up was answered (`_close_slot`)
    s._finish_history(T0 + SECOND)
    assert load.outcome(3) == "slot_closed_first"
    assert load.reason is not None and "closed before" in load.reason


def test_a_refusal_that_arrives_after_its_slot_closed_is_counted_refused():
    s, _, load = unit()
    del s.slots[AAA]
    s._finish_history(T0 + SECOND)
    s.on_data_response(response("bars-1", [], failure="429 Too Many Requests"))
    s._finish_history(T0 + 2 * SECOND)
    assert load.outcome(3) == "refused"
    assert load.reason is not None and "429 Too Many Requests" in load.reason
    assert s.record()["warm_up"]["outcomes"]["refused"] == 1


def test_a_refusal_for_a_slot_closed_in_the_same_step_is_counted_refused():
    s, _, load = unit()
    del s.slots[AAA]
    s.on_data_response(response("bars-1", [], failure="429 Too Many Requests"))
    s._finish_history(T0 + SECOND)
    assert load.outcome(3) == "refused"


def test_a_history_bar_still_forming_at_the_step_is_not_fed():
    """C-3: a bar is complete at its `ts_init` (its close); one later than the step is dropped, whatever
    the source sent (the adapter's own window already ends one interval back)."""
    s, slot, load = unit()
    s.on_data_response(response("bars-1", history(AAA, 4, last_open=T0)))
    s._finish_history(T0 + 30 * SECOND)  # the minute opening at T0 closes at T0 + MINUTE
    assert (load.bars, load.dropped) == (3, 1)
    assert slot.atr is not None and slot.atr.count == 3


def test_the_first_live_bar_after_the_overnight_gap_takes_the_jump_once():
    """Overnight history ends at least 15 minutes back (`OVERNIGHT_HISTORY_DELAY`): the first live
    bar's true range spans the gap from the last history close, and enters the ATR once, as one of its
    `atr_n` inputs, not as a range of its own."""
    s, slot, _ = unit()
    gap_end = T0 - 16 * MINUTE
    s.on_data_response(response("bars-1", history(AAA, 3, last_open=gap_end)))
    s._finish_history(T0)
    assert slot.atr is not None and slot.atr.initialized
    before = slot.atr.value
    jumped = live(T0, close="10.5000")  # 0.04 wide, 0.50 above the last history close
    s.on_bar(jumped)
    expected = AverageTrueRange(3)
    for bar in [*history(AAA, 3, last_open=gap_end), jumped]:
        expected.handle_bar(bar)
    assert slot.atr.value == pytest.approx(expected.value)
    assert before == pytest.approx(0.04)
    assert before < slot.atr.value < 0.52  # the jump's 0.52 range counts once, weighted


def test_a_late_response_after_live_bars_keeps_only_older_minutes_and_feeds_in_time_order():
    s, slot, _ = unit()
    s.on_bar(live(T0))  # the live bar of the minute opening at T0
    s.on_bar(live(T0 + MINUTE))
    # history ending at the minute opening at T0 + MINUTE overlaps both live minutes
    s.on_data_response(response("bars-1", history(AAA, 4, last_open=T0 + MINUTE)))
    s._finish_history(T0 + 3 * MINUTE)
    assert (s.history_bars, s.history_dropped) == (2, 2)
    assert s.live_bars == 2
    assert slot.atr is not None and slot.atr.count == 4
    expected = AverageTrueRange(3)
    for bar in [*history(AAA, 2, last_open=T0 - MINUTE), live(T0), live(T0 + MINUTE)]:
        expected.handle_bar(bar)
    assert slot.atr.value == pytest.approx(expected.value)


def test_a_live_bar_of_a_minute_history_already_gave_is_not_fed_twice():
    s, slot, _ = unit()
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s._finish_history(T0 + SECOND)
    s.on_bar(live(T0))  # the minute opening at T0 is the history's last one
    assert slot.atr is not None and slot.atr.count == 3
    assert s.live_bars == 1 and list(slot.closes) == [D(10)]


def test_an_unanswered_warm_up_is_named_and_a_late_answer_still_warms_once():
    s, slot, load = unit()
    s._finish_history(T0 + mover.HISTORY_WAIT_SECS * SECOND)
    assert load.late and load.outcome(3) == "unanswered"
    assert any("unanswered after" in note for note in s.notes[AAA])
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s.on_data_response(response("bars-1", history(AAA, 3)))
    s._finish_history(T0 + (mover.HISTORY_WAIT_SECS + 1) * SECOND)
    assert slot.atr is not None and slot.atr.count == 3
    assert (load.bars, load.dropped) == (3, 3) and load.outcome(3) == "warm"


def test_a_partial_answer_then_its_completion_are_one_warm_up():
    s, slot, load = unit()  # each step after the history's last close, T0 + MINUTE
    bars = history(AAA, 3)
    s.on_data_response(response("bars-1", bars[:2], completed=False))
    s._finish_history(T0 + MINUTE + SECOND)
    assert not load.completed and load.bars == 2
    s.on_data_response(response("bars-1", bars[2:]))
    s._finish_history(T0 + MINUTE + 2 * SECOND)
    assert load.completed and load.bars == 3 and load.outcome(3) == "warm"
    assert slot.atr is not None and slot.atr.initialized


# --- property 4: fewer than atr_n bars keeps today's cold path, named and counted --------------------


def test_fewer_than_atr_n_history_bars_keep_the_cold_fallback_named_and_counted():
    """Five history bars and the live bars to the entry are under 14: the entry is cold, as on main.
    History and live bars together complete the ATR, each minute once; history alone is not warm."""
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.answer[AAA] = history(AAA, 5)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert first_entry(s)["trail_source"] == "cold_atr_fallback"
    warm_up = s.record()["warm_up"]
    assert warm_up["bars"] == 4  # the fifth minute is the cache's (see the case above)
    assert warm_up["outcomes"]["fewer_than_atr_n"] == 1 and warm_up["outcomes"]["warm"] == 0
    [row] = warm_up["slots"]
    assert row["instrument_id"] == str(AAA) and (row["bars"], row["cached"]) == (4, 1)
    assert "fewer than 14" in row["reason"]
    assert any("fewer than 14" in note for note in s.notes[AAA])
    eng.dispose()


def test_thirteen_history_bars_are_completed_by_the_first_live_bar_and_counted_short():
    s, slot, load = unit(atr_n=14)
    s.on_data_response(response("bars-1", history(AAA, 13, last_open=T0 - MINUTE)))
    s._finish_history(T0 + SECOND)
    assert slot.atr is not None and not slot.atr.initialized
    assert load.outcome(14) == "fewer_than_atr_n"
    s.on_bar(live(T0))
    assert slot.atr.initialized and slot.atr.count == 14


# --- property 5: one request per opening, in the budget; a refusal is named and the slot stays cold --


def test_one_request_per_slot_opening_counted_in_the_record():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14, slots=2, watch=2)
    run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.20"))),
        tape(AAA, [10.0] * 4),
        tape(BBB, [20.0] * 4),
    )
    assert len(s.asked) == len(s.opened_slots) == 2
    record = s.record()
    assert record["history_requests"] == 2
    assert record["history_request_limit"] == mover.HISTORY_REQUEST_LIMIT
    eng.dispose()


def test_a_request_over_the_limit_is_held_by_name_and_asked_when_it_frees(monkeypatch):
    monkeypatch.setattr(mover, "HISTORY_REQUEST_LIMIT", 1)
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14, slots=2, watch=2)
    run(
        eng,
        s,
        ranker(rank((AAA, "0.25"), (BBB, "0.20"))),
        tape(AAA, [10.0] * 4),
        tape(BBB, [20.0] * 4),
    )
    # Held by name while the rolling minute is full, then asked once the minute frees, so the slot is
    # not cold for its whole life (review F3).
    assert any("held at the rolling minute limit" in n for n in s.notes[BBB])
    assert [kind.instrument_id for kind, *_ in s.asked] == [AAA, BBB]
    record = s.record()
    assert record["history_requests"] == 2
    assert record["warm_up"]["outcomes"]["held_at_limit"] == 0
    assert len(record["warm_up"]["slots"]) == 2
    [row] = [r for r in record["warm_up"]["slots"] if r["instrument_id"] == str(BBB)]
    assert row["request_id"] is not None
    eng.dispose()


@pytest.mark.parametrize(
    "failure", ["429 Too Many Requests", "Alpaca bar requests take feed iex, otc, sip, boats"]
)
def test_a_refused_answer_is_named_and_the_slot_enters_cold(failure):
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.answer[AAA] = history(AAA, 14)
    s.failure = failure
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert first_entry(s)["trail_source"] == "cold_atr_fallback"
    warm_up = s.record()["warm_up"]
    assert warm_up["outcomes"]["refused"] == 1 and warm_up["bars"] == 0
    assert failure in warm_up["slots"][0]["reason"]
    assert any(failure in note for note in s.notes[AAA])
    eng.dispose()


def test_a_request_the_strategy_cannot_send_is_named_and_the_slot_enters_cold():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.refuse = ValueError("no client for the bar type")
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert first_entry(s)["trail_source"] == "cold_atr_fallback"
    warm_up = s.record()["warm_up"]
    assert warm_up["outcomes"]["refused"] == 1
    assert "no client for the bar type" in warm_up["slots"][0]["reason"]
    eng.dispose()


def test_an_overnight_warm_up_asks_for_an_end_fifteen_minutes_old():
    """Alpaca serves overnight (BOATS) history only when its end is at least 15 minutes old
    (https://docs.alpaca.markets/us/docs/245-trading.md); any other feed asks up to now."""
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14, feed="boats")
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [10.0] * 4))
    [(_, start, params, _)] = s.asked
    [end] = s.ends
    assert params == {"feed": "boats"}
    assert end is not None
    assert end - start >= (14 + mover.WARM_UP_SPARE_BARS) * timedelta(minutes=1)
    eng.dispose()
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14, feed="sip")
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, [10.0] * 4))
    assert s.ends == [None]
    eng.dispose()


def test_the_overnight_end_is_the_request_time_less_the_delay():
    s = warming(trail_mode="atr", atr_n=14, feed="boats")
    assert mover.OVERNIGHT_HISTORY_DELAY == timedelta(minutes=15)
    assert mover.history_window(s.settings, s.feed_params, NOW) == (
        NOW - timedelta(minutes=15) - mover.WARM_UP_SPAN,
        NOW - timedelta(minutes=15),
    )
    assert mover.history_window(s.settings, {"feed": "sip"}, NOW) == (
        NOW - mover.WARM_UP_SPAN,
        None,
    )


# --- property 6: fixed mode requests nothing ----------------------------------------------------------


def test_the_fixed_trail_requests_no_history():
    eng = engine()
    s = warming(trail_mode="fixed")
    s.answer[AAA] = history(AAA, 14)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert s.asked == []
    assert s.record()["history_requests"] == 0
    assert s.history_bars == s.history_dropped == 0
    eng.dispose()


# --- the record: entries by how the distance was chosen ----------------------------------------------


def test_the_strategy_record_counts_its_entries_by_trail_source():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.answer[AAA] = history(AAA, 14)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    record = s.record()
    counted = record["entries_by_trail_source"]
    assert sum(counted.values()) == len(record["entries"])
    assert counted.get("cold_atr_fallback", 0) == 0
    eng.dispose()


# --- one ATR series, two sources: the cache first, the request only when it leaves the ATR short -------


def test_an_opening_the_cache_warms_asks_for_no_history_and_is_counted():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=3, run_bars=1)
    late = ranker(rank(), values={6: rank((AAA, "0.25"))})
    run(eng, s, late, tape(AAA, [10.0, 10.1, 10.0, 10.1, 10.0, 10.1, 10.0, 10.2, 10.3, 10.4, 10.5]))
    assert s.asked == []
    record = s.record()
    assert record["history_requests"] == 0
    assert record["warm_up"]["outcomes"]["warm_from_cache"] == 1
    [row] = record["warm_up"]["slots"]
    assert row["request_id"] is None and row["outcome"] == "warm_from_cache"
    assert record["entries"][0]["trail_source"] != "cold_atr_fallback"
    eng.dispose()


def test_an_opening_the_cache_leaves_short_asks_once():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.answer[AAA] = history(AAA, 14)
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert len(s.asked) == 1
    assert s.record()["warm_up"]["outcomes"]["warm_from_cache"] == 0
    eng.dispose()


def test_a_minute_the_cache_fed_is_not_fed_again_by_a_live_bar():
    s, slot, _ = unit()
    slot.history = None
    cached = history(AAA, 4, last_open=T0 - MINUTE)
    assert s._warm_atr(slot, list(reversed(cached)), T0 + SECOND)
    fed = slot.atr.count if slot.atr is not None else None
    s.on_bar(live(T0 - MINUTE))  # the cache's newest minute, now as a live bar
    assert slot.atr is not None and slot.atr.count == fed


def test_a_slot_opened_at_0931_is_warm_from_the_part_before():
    """The lead's ruling of 2026-11-13, 00:10: the request is not bounded to the session part, so a
    slot opened as the regular part begins is warm from the pre-market's minutes."""
    s, slot, load = unit(atr_n=14)
    opened = T0 - 89 * MINUTE  # 09:31 New York; T0 is 11:00
    load.requested_ns = opened
    s.on_data_response(response("bars-1", history(AAA, 14, last_open=opened - 2 * MINUTE)))
    s._finish_history(opened + SECOND)  # bars opening 09:16 to 09:29, all pre-market
    assert load.bars == 14 and load.outcome(14) == "warm"
    assert slot.atr is not None and slot.atr.initialized


def test_a_short_cache_and_its_answer_make_one_series_with_no_minute_twice():
    """The cache feeds what it has (two minutes); the answer adds the minutes before them, drops the
    two the cache gave, and the series is fed again in time order: nothing twice, nothing lost."""
    s, slot, load = unit()
    cached = history(AAA, 2, last_open=T0 - MINUTE, width="0.10")
    assert not s._warm_atr(slot, list(reversed(cached)), T0 + SECOND)
    load.cached = len(slot.atr_minutes)
    assert slot.atr is not None and slot.atr.count == 2
    answer = history(AAA, 4, last_open=T0 - MINUTE)  # its last two minutes are the cache's
    s.on_data_response(response("bars-1", answer))
    s._finish_history(T0 + 2 * SECOND)
    assert (load.bars, load.dropped, load.cached) == (2, 2, 2)
    expected = AverageTrueRange(3)
    for bar in [*answer[:2], *cached]:
        expected.handle_bar(bar)
    assert slot.atr.count == 4 and slot.atr.value == pytest.approx(expected.value)
    assert load.outcome(3) == "warm"


# --- a 429 that alpaca-py's own retry answers (the lead's wording of 2026-11-13, 00:03) ----------------


def test_a_429_the_client_retries_to_success_warms_the_slot_and_fails_the_window(monkeypatch):
    """The strategy sends one request per opening and none after a 429. alpaca-py repeats a request
    answered 429 (`common/constants.py:11-13`, `common/rest.py:127-135` at `c42add7c0a70`). Where its
    own retry succeeds, the slot is warm from that answer. The adapter names and counts the 429 and each
    attempt, and the window's record fails on it as on any 429 (`session_window.request_failure`)."""
    import json

    import requests
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from canon_alpaca import providers
    from canon_alpaca.providers import DATA_API
    from canon_alpaca.providers import RequestCounter
    from canon_alpaca.providers import request_counts
    from canon_alpaca.providers import with_http_timeout
    from requests.adapters import HTTPAdapter

    from canon_trading import session_window

    monkeypatch.setattr(providers, "REQUESTS", RequestCounter())
    statuses = [429]
    bar = {
        "t": "2026-11-13T05:00:00Z",
        "o": 10,
        "h": 10.02,
        "l": 9.98,
        "c": 10,
        "v": 1000,
        "n": 9,
        "vw": 10,
    }
    body = json.dumps({"bars": {"AAA": [bar]}, "next_page_token": None}).encode()

    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = statuses.pop(0) if statuses else 200
        response.reason = {200: "OK", 429: "Too Many Requests"}[response.status_code]
        response._content = body
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    key = "offline-placeholder"
    client = with_http_timeout(StockHistoricalDataClient(api_key=key, secret_key=key))
    client._retry_wait = 0  # the wait between alpaca-py's retries; the retry itself stays
    got = client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols="AAA",
            timeframe=TimeFrame.Minute,
            start=NOW - timedelta(hours=1),
            feed=DataFeed.SIP,
        )
    )
    assert len(got["AAA"]) == 1  # one answer, from the retry
    [count] = [c for c in request_counts() if c.api == DATA_API]
    assert (count.total, count.rate_limited_total) == (2, 1)
    assert count.rate_limited[0].name == "429 Too Many Requests"

    # The strategy's one request receives that success, and its slot is warm.
    s, slot, load = unit(atr_n=14)
    s.on_data_response(response("bars-1", history(AAA, 14)))
    s._finish_history(T0 + MINUTE + SECOND)
    assert load.outcome(14) == "warm" and slot.atr is not None and slot.atr.initialized
    assert list(s.history_by_request) == ["bars-1"]

    # The window's record fails on the counted 429.
    report = session_window.WindowReport("extended", "AAA", started="20261113T050000Z")
    report.request_budget = {}
    assert session_window._snapshot_requests(report) is None
    assert "Alpaca answered 429 to 1 request(s) of the data API" == session_window.request_failure(
        report
    )


# --- the review of the merge with batch A (2026-11-13) ---------------------------------------------------


def test_a_cached_minute_replayed_live_does_not_advance_the_atr():
    """Batch A reader A-1: an EXTERNAL bar of the cache's last minute, delivered again live (the cache
    replaces an equal-time bar, the data engine still publishes it), is not fed a second time."""
    from tests.test_mover_trail import minute_bars

    s, slot, _ = unit()
    slot.history = None
    bars = minute_bars([10.00, 10.00, 10.00, 10.10], width="0.02")
    assert s._warm_atr(slot, list(reversed(bars)), T0 + 4 * MINUTE)
    assert slot.atr is not None
    before = (slot.atr.count, slot.atr.value)
    s.on_bar(bars[-1])
    assert (slot.atr.count, slot.atr.value) == before


def test_a_held_warm_up_is_not_sent_once_the_slot_is_warm_or_the_window_winds_down():
    for winding_down in (False, True):
        s, slot, load = unit()
        s.history_by_request.clear()
        load.request_id, load.held = None, True
        if winding_down:
            s.winding_down = True
        else:
            for bar in history(AAA, 3, last_open=T0 - MINUTE):
                assert slot.atr is not None
                slot.atr.handle_bar(bar)
        s._finish_history(T0 + MINUTE)
        assert load.completed and not load.held and load.request_id is None
        assert load.reason is not None
        assert ("winding down" if winding_down else "warm before") in load.reason
        assert s.history_requests == 0


def test_a_late_answer_that_reshapes_the_atr_lets_the_holding_compare_once_more():
    s, slot, _ = unit()
    slot.warm_checked = True  # compared on live bars before the answer came
    s.on_data_response(response("bars-1", history(AAA, 3, last_open=T0 - MINUTE)))
    s._finish_history(T0 + SECOND)
    assert slot.warm_checked is False


def test_a_short_answer_after_live_bars_completed_the_atr_does_not_say_cold():
    s, slot, load = unit()
    s.on_bar(live(T0))
    s.on_bar(live(T0 + MINUTE))
    s.on_data_response(response("bars-1", history(AAA, 1, last_open=T0 - MINUTE)))
    s._finish_history(T0 + 3 * MINUTE)
    assert slot.atr is not None and slot.atr.initialized
    assert load.outcome(3) == "fewer_than_atr_n"
    assert load.reason is not None and "cold ATR fallback stays" not in load.reason
    assert "complete with live bars" in load.reason


def test_a_request_refused_before_it_leaves_takes_no_place_in_the_rolling_minute():
    eng = engine()
    s = warming(trail_mode="atr", atr_n=14)
    s.refuse = ValueError("no client for the bar type")
    run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
    assert s.record()["history_requests"] == 0 and len(s.history_sent) == 0
    eng.dispose()


def test_a_short_cache_counts_its_bars_in_the_entry_record_field():
    s, slot, _ = unit()
    cached = history(AAA, 2, last_open=T0 - MINUTE)
    assert not s._warm_atr(slot, list(reversed(cached)), T0 + SECOND)
    assert slot.atr_warm_bars == 2
