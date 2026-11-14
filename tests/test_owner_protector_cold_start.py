"""U-h: the protector's cold start after hours. Red first on C0 0c8ab948 (L6, 2026-11-13).

The lead's terms (relayed by the deputy at 18:02:39):

(1) The seed. At a start, and when a holding is adopted, the protector reads the venue's latest quote
    by a request through the adapter and plans from it.
    - The seed sets the venue clock.
    - It is never an observation.
    - Its age is named.
    - The guard still needs newer quotes.
(2) The distance. The distance comes from the last 14 one-minute bars that exist in the session day,
    which starts at the latest 04:00 New York, not from the last 14 clock minutes. It goes through
    `protect.distance_for` (L3's function; there is one distance).
    - With no spread quoted, the ATR stands alone.
    - No history uses the named, counted fixed fallback. The later accepted flat-day/no-spread
      ruling has its own named and counted fallback.

Tonight's event (W2 on account B, commit 48820dc0, row OWN1):
- A found long started with no quote in the node's cache, and with no bars since the subscription.
- No quote arrived for 5 min 23 s.
- The level was then planned on the fixed fallback (ATR cold), although the symbol had traded during
  the session day.
`test_tonights_cold_start_plans_from_the_seed_and_the_days_bars_not_the_fixed_fallback` replays it.

How requests and answers reach the protector:
- Both requests are NautilusTrader's own, `Strategy.request_quotes` and `Strategy.request_bars`
  (python/nautilus_trader/trading/__init__.pyi:963-1002 at d53849b216e2).
- The pin's crates/common/src/python/response.rs:145-171,300,311-340,381-425 dispatches the
  historical payload, on_data_response metadata (including failure_reason), then a terminal
  callback receiving the request ID string. Empty failures complete without historical data.
  The fake follows that order; the bars gate opens only on its request's terminal callback.
- Every distance here is compared with an oracle the change does not touch:
  - for a warm book, today's `protect.plan_distance` on 14 consecutive bars of the same width;
  - for the cold case, its fixed fallback.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick

from canon_trading import protect
from canon_trading.price_step import up_on_step
from tests.test_owner_protector import AAA
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness
from tests.test_owner_protector import minute_bars
from tests.test_owner_protector import position
from tests.test_owner_protector import quote
from tests.test_owner_protector import task_distance
from tests.test_owner_protector import working_sell

MINUTE = 60 * SECOND
AFTER_HOURS = T0 + 7 * 3600 * SECOND  # 2025-11-10 18:00 New York (23:00Z): T0 is 11:00 New York
SESSION_DAY = T0 - 7 * 3600 * SECOND  # 2025-11-10 04:00 New York (09:00Z)
BAR_TYPE = BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Synthetic requests only; never inspect a credential environment or paper lock."""


def bar(opened: int, width: str = "0.04") -> Bar:
    """A one-minute bar stamped at its open (canon_alpaca/data.py:1237-1257), closing mid-range."""
    half = Decimal(width) / 2
    return Bar(
        BAR_TYPE,
        Price.from_str("10.00"),
        Price.from_str(str(Decimal("10.00") + half)),
        Price.from_str(str(Decimal("10.00") - half)),
        Price.from_str("10.00"),
        Quantity.from_int(100),
        opened,
        opened + MINUTE,
    )


def quiet_day(width: str = "0.04") -> list[Bar]:
    """Six complete minutes spread over the session day, most of them more than an hour old (like
    U's quiet tape: 0-6 quotes a minute, 5 of 16 minutes empty), plus two that must not count:
    - a wide bar from before the session day;
    - the minute still open at `AFTER_HOURS`."""
    day = [
        SESSION_DAY + 30 * MINUTE,
        SESSION_DAY + 8 * 3600 * SECOND,
        AFTER_HOURS - 121 * MINUTE,
        AFTER_HOURS - 23 * MINUTE,
        AFTER_HOURS - 20 * MINUTE,
        AFTER_HOURS - 14 * MINUTE,
    ]
    return [
        bar(SESSION_DAY - 10 * MINUTE, "1.50"),
        *(bar(at, width) for at in day),
        bar(AFTER_HOURS, "1.50"),
    ]


def warm_distance(width: str = "0.04", bid: str = "10.00") -> Decimal:
    """Today's planner on 14 consecutive complete bars of `width` at T0: the oracle for a distance.
    Every bar here has a true range of `width`, so an ATR over any count of them is `width`."""
    warm = Harness()
    warm.cached.latest = quote(bid)
    warm.cached.history = minute_bars(width, 14)
    distance, _ = task_distance(warm, "OWN1")
    return distance


def fixed_distance(bid: str = "10.00") -> Decimal:
    """Today's planner with no bars at all: the registered row's fixed fallback."""
    cold = Harness()
    cold.cached.latest = quote(bid)
    distance, _ = task_distance(cold, "OWN1")
    return distance


class Cold(Harness):
    """A found long at 18:00 New York with nothing cached: no quote and no bars."""

    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self.cached.latest = None
        self.cached.history = []
        self.test_clock.set_time(AFTER_HOURS)
        self.cached.held[0].ts_opened = AFTER_HOURS - 3600 * SECOND
        self.quote_requests: list[dict[str, Any]] = []
        self.bar_requests: list[dict[str, Any]] = []

    def request_quotes(
        self,
        instrument_id,
        start=None,
        end=None,
        limit=None,
        client_id=None,
        params=None,
        callback=None,
    ) -> str:
        self.quote_requests.append(
            {
                "instrument_id": instrument_id,
                "start": start,
                "end": end,
                "limit": limit,
                "params": dict(params or {}),
                "callback": callback,
                "subscribed": instrument_id in {iid for iid, _ in self.subscriptions},
            }
        )
        return f"quotes-{len(self.quote_requests)}"

    def request_bars(
        self,
        bar_type,
        start=None,
        end=None,
        limit=None,
        client_id=None,
        params=None,
        callback=None,
    ) -> str:
        self.bar_requests.append(
            {
                "bar_type": bar_type,
                "start": start,
                "end": end,
                "limit": limit,
                "params": dict(params or {}),
                "callback": callback,
            }
        )
        return f"bars-{len(self.bar_requests)}"

    def step(self, seconds: int = 1) -> None:
        self.test_clock.set_time(self.test_clock.timestamp_ns() + seconds * SECOND)
        self._on_step(cast(Any, SimpleNamespace()))

    def answer(self, kind: str, data: list, failure: str | None = None, index: int = 0) -> None:
        requests = self.bar_requests if kind == "bars" else self.quote_requests
        if data or failure is None:
            if kind == "bars":
                self.cached.history.extend(data)
                self.on_historical_bars(data)
            else:
                self.on_historical_quotes(data)
        if requests:
            request_id = f"{kind}-{index + 1}"
            self.on_data_response(
                cast(
                    Any,
                    SimpleNamespace(
                        correlation_id=request_id,
                        failure_reason=failure,
                        completed=True,
                        data=data,
                    ),
                )
            )
            callback = requests[index]["callback"]
            if callback is not None:
                callback(request_id)

    def history(self, bars: list[Bar]) -> None:
        self.answer("bars", bars)

    def seed(self, bid: str = "10.00", age_s: int = 20, ask: str | None = None) -> QuoteTick:
        at = self.test_clock.timestamp_ns() - age_s * SECOND
        tick = QuoteTick(
            AAA,
            Price.from_str(bid),
            Price.from_str(ask if ask is not None else str(Decimal(bid) + Decimal("0.02"))),
            Quantity.from_int(100),
            Quantity.from_int(100),
            at,
            at,
        )
        # The engine adds a response's quotes to the cache (push_front) BEFORE it dispatches them
        # (crates/data/src/engine/mod.rs:1965-1972, dispatch.rs:387-390, cache/mod.rs:2379-2402 at
        # d53849b216e2; L5's read, F3): a seed answered late becomes `cache.quote()`, whatever its
        # stamp.
        if self.cached.latest is not None:
            self.cached.earlier_quotes.insert(0, self.cached.latest)
        self.cached.latest = tick
        self.answer("quotes", [tick])
        return tick


def said(strategy: Harness, *words: str) -> list[str]:
    return [line for line in strategy.said if all(word in line for word in words)]


@pytest.mark.parametrize("supplied", [False, True], ids=["found", "supplied-level"])
def test_a_start_asks_the_adapter_for_the_latest_quote_and_the_session_days_bars(supplied) -> None:
    """(1) and (2): one latest-quote request and one bars request per holding, on the protector's
    feed. The bars request starts at the session day's 04:00 New York, not one clock hour back."""
    strategy = Cold(levels={AAA: Price.from_str("9.90")}) if supplied else Cold()
    strategy.on_start()
    (asked,) = [r for r in strategy.quote_requests if r["instrument_id"] == AAA]
    assert asked["params"].get("feed") == "sip"
    assert asked["params"].get("latest") is True
    assert asked["subscribed"], "M6: the subscription is in place before the seed request"
    assert asked["limit"] == 1
    (bars,) = [r for r in strategy.bar_requests if r["bar_type"] == BAR_TYPE]
    assert bars["params"].get("feed") == "sip"
    assert bars["start"] == datetime.fromtimestamp(SESSION_DAY / SECOND, UTC)


def test_a_holding_found_after_the_start_is_seeded_too() -> None:
    strategy = Cold()
    strategy.cached.held = []
    strategy.on_start()
    assert not strategy.quote_requests
    strategy.cached.held = Harness().cached.held
    strategy.cached.held[0].ts_opened = AFTER_HOURS
    strategy.step()
    assert [r["instrument_id"] for r in strategy.quote_requests] == [AAA]
    assert [r["bar_type"] for r in strategy.bar_requests] == [BAR_TYPE]


def test_tonights_cold_start_plans_from_the_seed_and_the_days_bars_not_the_fixed_fallback() -> None:
    """W2's case: no quote cached, no bars since subscribing, a quiet tape that traded in the day."""
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed("10.00", age_s=20)
    expected = warm_distance()
    assert expected != fixed_distance(), "the oracle must tell the day's ATR from the fallback"
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str(str(up_on_step(Decimal("10.00") - expected)))
    assert strategy.venue_quotes[AAA].ts_event == seed.ts_event, "the seed sets the venue clock"
    assert said(strategy, "AAA.ALPACA", "seed", "20 s"), "the seed's age is named"
    assert said(strategy, "AAA.ALPACA", "ATR over 6 bars")
    assert not said(strategy, "fixed fallback")
    assert not strategy.sent, "nothing is sent on the seed"


def test_the_seed_is_never_an_observation() -> None:
    """The guard counts only quotes newer than the seed: its stamp again counts nothing, and a found
    long still needs two newer quotes before its sell is placed, as on a warm start."""
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed("10.00", age_s=20)
    strategy.observe("10.00", seed.ts_event)  # the same quote again, now on the stream
    strategy.observe("10.00", seed.ts_event)
    assert not strategy.sent
    strategy.observe("10.00", AFTER_HOURS + SECOND)
    assert not strategy.sent, "the first quote newer than the seed is the guarded one"
    strategy.observe("10.00", AFTER_HOURS + 2 * SECOND)
    (sell,) = strategy.sent
    assert sell.trailing_offset == warm_distance()


def test_a_seed_under_the_level_sells_nothing() -> None:
    """A supplied level above the seed's bid is a breach the seed cannot confirm, or even begin."""
    strategy = Cold(levels={AAA: Price.from_str("9.90")})
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed("9.80", age_s=5)
    strategy.step()
    strategy.step()
    assert not strategy.sent
    strategy.observe("9.80", seed.ts_event + SECOND)
    assert not strategy.sent, "one quote newer than the seed is one observation"


def test_a_seed_older_than_the_venue_clock_neither_moves_it_nor_plans() -> None:
    strategy = Cold()
    strategy.on_start()
    streamed = AFTER_HOURS - 5 * SECOND
    strategy.observe("10.00", streamed)
    strategy.history(quiet_day())
    strategy.seed("11.00", age_s=20)
    assert strategy.cached.latest is not None
    assert strategy.cached.latest.ts_event < streamed, "the late seed is now cache.quote()"
    strategy.step()
    strategy.step()
    assert strategy.venue_quotes[AAA].ts_event == streamed
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str(str(up_on_step(Decimal("10.00") - warm_distance())))
    assert not said(strategy, "11.00")


def test_a_late_seed_in_the_cache_prices_nothing_on_a_step() -> None:
    """F3. After a shrink, our sell is cancelled and re-sent once the cancel is acknowledged; at
    count one the re-send can go out on a timer step with no new quote.
    - A late seed under the limit, now `cache.quote()`, must neither freeze that re-send ("no
      positive protective limit") nor price it.
    - The builder adds one such case for each `cache.quote()` fallback on its base
      (owner_protector.py on 6737c08d: :753, :843, :1090, :1315)."""
    strategy = Cold(levels={AAA: Price.from_str("9.90")})
    strategy.on_start()
    strategy.history(quiet_day())
    strategy.observe("10.00", AFTER_HOURS - 5 * SECOND)
    strategy.observe("10.00", AFTER_HOURS - 4 * SECOND)
    (first,) = strategy.sent
    strategy.cached.held = [position("6")]
    strategy.cached.held[0].ts_opened = AFTER_HOURS - 3600 * SECOND
    strategy.step()
    assert strategy.canceled == [first.client_order_id]
    strategy.cached.working = [
        working_sell(
            str(first.client_order_id),
            qty="10",
            filled="0",
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=Price.from_str("9.95"),
        )
    ]
    strategy.seed("0.01", age_s=20)  # older than the venue clock, and under any limit
    for _ in range(3):
        strategy.step()
    assert AAA not in strategy.frozen, strategy.frozen.get(AAA)
    assert len(strategy.sent) == 2
    assert strategy.sent[1].quantity == Quantity.from_int(6)
    assert strategy.sent[1].trigger_price == Price.from_str("9.95")
    assert not said(strategy, "0.01")


def test_planning_waits_for_the_days_bars_and_says_so_at_every_step() -> None:
    """The cause of tonight's fallback: a level planned before the history could answer. With the
    seed in hand and the bars request unanswered, nothing is armed, and each step names the wait."""
    strategy = Cold()
    strategy.on_start()
    strategy.seed("10.00", age_s=20)
    for n in (1, 2, 3):
        strategy.step()
        assert strategy.slots[AAA].level is None
        assert "unprotected" in strategy.frozen[AAA]
        assert len(said(strategy, "AAA.ALPACA", "bars")) >= n
    assert not said(strategy, "fixed fallback")
    strategy.history(quiet_day())
    strategy.step()
    assert strategy.slots[AAA].level is not None


def test_no_bars_in_the_session_day_takes_the_fixed_fallback_named_and_counted() -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.history([bar(SESSION_DAY - 10 * MINUTE)])  # yesterday's alone
    strategy.seed("10.00", age_s=20)
    strategy.step()
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str(str(up_on_step(Decimal("10.00") - fixed_distance())))
    assert said(strategy, "AAA.ALPACA", "no history", "fixed fallback")
    strategy.said.clear()
    strategy.on_stop()
    assert said(strategy, "fixed fallback", "1", "AAA.ALPACA"), "the stop's record counts it"


def test_bars_older_than_an_hour_in_the_day_count_and_an_open_minute_does_not() -> None:
    """Replaces C0's `test_row_uses_only_completed_recent_minutes_and_names_cold_path`: 13 recent
    bars and one 61 minutes old are 14 that exist in the day, so the ATR is warm."""
    strategy = Harness()
    bars = minute_bars("0.06", 13)
    bars.append(bar(T0 - 61 * MINUTE, "0.06"))
    bars.append(bar(T0, "1.50"))  # the minute still open
    strategy.cached.history = bars
    strategy.on_historical_bars([])
    strategy.adopt_holding(AAA, Price.from_str("9.90"))
    strategy.observe("10.00", T0 + SECOND // 2)
    (sell,) = strategy.sent
    assert sell.trailing_offset == warm_distance("0.06")
    assert not said(strategy, "ATR cold")
    assert not said(strategy, "fixed fallback")


def test_a_locked_quote_plans_on_the_atr_alone_and_says_so() -> None:
    """(2): no spread is quoted, so the ATR stands alone inside the row's cap, through L3's function.
    A crossed quote stays refused before planning (C0's `no usable bid/ask` case)."""
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    strategy.seed("10.00", age_s=20, ask="10.00")
    strategy.step()
    day = [
        b for b in quiet_day() if SESSION_DAY <= b.ts_event and b.ts_event + MINUTE <= AFTER_HOURS
    ]
    expected, distance = protect.distance_for(
        "AAA", "OWN1", Decimal("10.00"), Decimal("10.00"), day
    )
    slot = strategy.slots[AAA]
    assert slot.level == Price.from_str(str(up_on_step(Decimal("10.00") - expected)))
    assert said(strategy, "AAA.ALPACA", replace(distance, quote_age=timedelta(seconds=20)).source())
    assert said(strategy, "ATR stands alone")


def test_the_history_and_cache_overlap_yields_exactly_the_last_atr_bars_minutes() -> None:
    strategy = Cold()
    strategy.on_start()
    # Cold.answer also caches these bars, just as the native engine does. Every stamp appears
    # in both sources, and sixteen complete minutes exist before the unfinished minute.
    strategy.history([bar(AFTER_HOURS - n * MINUTE) for n in range(16, -1, -1)])
    strategy.seed()
    distance = strategy.slots[AAA].distance
    assert distance is not None
    # L3's window, protect.ATR_BARS (atr_n + 1, as Mover._warm_atr feeds): fifteen of the sixteen.
    assert distance.bars == protect.ATR_BARS == 15
    assert distance.distance == warm_distance()
    assert said(strategy, f"ATR over {protect.ATR_BARS} bars")


def test_an_empty_seed_answer_is_named_at_every_step() -> None:
    """The zero bid, part (3): the adapter drops a one-sided quote before a QuoteTick exists
    (canon_alpaca/data.py:1276-1292), so when only such quotes arrive the protector receives no
    quote at all. The seed answers with nothing, and the holding's line says so at every step."""
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    strategy.answer("quotes", [])
    for n in (1, 2, 3):
        strategy.step()
        assert not strategy.sent
        assert strategy.slots[AAA].level is None
        assert len(said(strategy, "AAA.ALPACA", "unprotected", "no two-sided quote")) >= n


def test_a_zero_bid_past_the_adapter_spends_no_observation() -> None:
    """The zero bid, the protector's count. A zero-bid `QuoteTick` injected past the adapter moves
    no guard. The holding still needs two two-sided quotes before its sell is placed."""
    strategy = Harness()
    strategy.on_start()
    zero = QuoteTick(
        AAA,
        Price.from_str("0.00"),
        Price.from_str("10.02"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        T0 + SECOND,
        T0 + SECOND,
    )
    strategy.test_clock.set_time(T0 + SECOND)
    strategy.cached.latest = zero
    strategy.on_quote(zero)
    assert not strategy.sent
    assert strategy.slots[AAA].last_observation is None
    assert said(strategy, "AAA.ALPACA", "no usable bid")
    strategy.observe("10.00", T0 + 2 * SECOND)
    assert not strategy.sent, "the first two-sided quote is the guarded one"
    strategy.observe("10.00", T0 + 3 * SECOND)
    assert len(strategy.sent) == 1


@pytest.mark.parametrize("age_s", [61, 3600])
def test_an_old_seed_in_the_open_part_plans_and_names_its_age(age_s) -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed(age_s=age_s)
    assert strategy.slots[AAA].level is not None
    assert strategy.venue_quotes[AAA] == seed
    assert said(strategy, "seed", f"{age_s} s")
    assert strategy.slots[AAA].last_observation is None
    assert not strategy.sent


@pytest.mark.parametrize(
    "now,feed,stamp",
    [
        (
            AFTER_HOURS - 2 * 3600 * SECOND + 5 * SECOND,
            "sip",
            AFTER_HOURS - 2 * 3600 * SECOND - SECOND,
        ),
        (SESSION_DAY, "sip", SESSION_DAY - SECOND),
        (
            int(datetime(2025, 11, 10, 1, tzinfo=UTC).timestamp()) * SECOND,
            "boats",
            int(datetime(2025, 11, 8, 1, tzinfo=UTC).timestamp()) * SECOND,
        ),
    ],
    ids=["regular-to-post", "yesterday-at-four", "friday-at-sunday-open"],
)
def test_a_seed_from_before_this_part_waits_named(now, feed, stamp) -> None:
    strategy = Cold(feed=feed)
    strategy.test_clock.set_time(now)
    strategy.on_start()
    strategy.history([])
    strategy.seed(age_s=(now - stamp) // SECOND)
    strategy.step()
    assert strategy.slots[AAA].level is None
    assert said(strategy, "before", "session part", "unprotected")
    assert not strategy.sent


@pytest.mark.parametrize("kind", ["quotes", "bars"])
def test_a_failed_seed_or_history_request_waits_named_without_retry(kind) -> None:
    strategy = Cold()
    strategy.on_start()
    if kind == "bars":
        strategy.seed()
    else:
        strategy.history(quiet_day())
    strategy.answer(kind, [], "feed refused")
    for n in (1, 2, 3):
        strategy.step()
        assert strategy.slots[AAA].level is None
        assert len(said(strategy, "failed", "feed refused", "unprotected")) >= n
    assert len(strategy.quote_requests) == len(strategy.bar_requests) == 1
    assert not strategy.sent


def test_an_equal_stamp_keeps_the_last_arrival_for_planning_but_never_counts() -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.observe("10.00", AFTER_HOURS - 20 * SECOND)
    last = strategy.seed("11.00", age_s=20)
    strategy.history(quiet_day())
    assert strategy.venue_quotes[AAA] == last
    distance, _ = protect.distance_for(
        "AAA", "OWN1", Decimal(11), Decimal("11.02"), quiet_day()[1:-1]
    )
    level = strategy.slots[AAA].level
    assert level is not None
    assert level.as_decimal() == up_on_step(Decimal(11) - distance)
    assert strategy.slots[AAA].last_observation is None
    assert not strategy.sent


def test_the_seed_sets_the_clock_before_it_can_plan_m6() -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed()
    # Removing the seed (M5), or planning it before noting it (M6), leaves this unarmed.
    assert strategy.slots[AAA].armed_event == seed.ts_event
    assert strategy.slots[AAA].level is not None
    assert not strategy.sent


def test_end_counts_ahead_nonadvancing_and_refused_redeliveries() -> None:
    from canon_alpaca.data import STAMP_AHEAD_MOST_NS

    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day())
    seed = strategy.seed()
    strategy.observe("10.00", seed.ts_event)  # equal: no maximum raised
    bad = QuoteTick(
        AAA,
        Price.from_str("10.00"),
        Price.from_str("10.02"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        AFTER_HOURS + STAMP_AHEAD_MOST_NS + SECOND,
        AFTER_HOURS,
    )
    strategy.on_quote(bad)
    strategy.on_quote(bad)
    strategy.test_clock.set_time(bad.ts_event)
    strategy.on_quote(quote(at=bad.ts_event))  # now sane, still recorded as refused
    strategy.on_stop()
    assert said(strategy, "END", "refused-ahead=1", "not-raised=1", "refused-redeliveries=2")
    assert said(strategy, "END", f"first_quote={seed.ts_event}", f"last_quote={bad.ts_event}")


def test_a_late_seed_does_not_change_the_spread_at_release() -> None:
    strategy = Cold(levels={AAA: Price.from_str("9.90")})
    strategy.on_start()
    strategy.history(quiet_day())
    strategy.observe("10.00", AFTER_HOURS - 5 * SECOND)
    strategy.observe("10.00", AFTER_HOURS - 4 * SECOND)
    slot = strategy.slots[AAA]
    slot.gain_applied_floor = Price.from_str("9.80")
    strategy.seed("0.01", age_s=20, ask="0.50")
    strategy.on_order_released(
        cast(
            Any,
            SimpleNamespace(
                instrument_id=AAA,
                client_order_id=slot.protect_id,
            ),
        )
    )
    assert slot.gain_release_spread == Decimal("0.02")


def test_a_late_seed_does_not_defer_a_gain_floor_raise(monkeypatch) -> None:
    strategy = Cold(levels={AAA: Price.from_str("9.90")})
    monkeypatch.setattr(strategy, "modify_order", lambda *args, **kwargs: None)
    strategy.cached.held[0].avg_px_open = Decimal("9.80")
    strategy.on_start()
    strategy.history(quiet_day())
    strategy.seed()
    strategy.observe("10.00", AFTER_HOURS + SECOND)
    strategy.observe("10.00", AFTER_HOURS + 2 * SECOND)
    slot = strategy.slots[AAA]
    assert slot.gain_applied_floor == Price.from_str("9.80")
    strategy.seed("0.01", age_s=20)
    strategy.cached.held[0].avg_px_open = Decimal("9.90")
    strategy.step()
    assert slot.gain_applied_floor == Price.from_str("9.90")


def test_a_late_seed_does_not_price_a_released_sales_chase() -> None:
    from tests.test_owner_protector_chase import CHASE
    from tests.test_owner_protector_chase import RELEASE
    from tests.test_owner_protector_chase import released

    strategy, _ = released()
    strategy.observe("10.00", RELEASE + SECOND)
    late = quote("0.01", T0 - SECOND)
    strategy.cached.latest = late  # engine caches a response before delivering it
    strategy.on_historical_quotes([late])
    strategy.test_clock.set_time(RELEASE + CHASE + 2 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert not strategy.canceled, "the venue quote is above the sale; the late seed cannot chase"


def test_a_flat_day_without_a_spread_names_and_counts_its_own_fallback() -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.history(quiet_day("0.00"))
    strategy.seed(ask="10.00")
    assert strategy.slots[AAA].level is not None
    assert said(strategy, "no range", "fixed fallback")
    strategy.on_stop()
    assert said(strategy, "END", "flat session day=1", "no history=0")


def test_end_keeps_counts_after_a_holding_has_ended() -> None:
    strategy = Cold()
    strategy.on_start()
    strategy.history([])
    seed = strategy.seed()
    strategy.observe("10.00", seed.ts_event)
    strategy.stop_instrument(AAA)
    strategy.said.clear()
    strategy.on_stop()
    assert said(strategy, "END", "AAA.ALPACA", "not-raised=1", "fixed fallback=1")


def test_request_completions_are_per_holding_and_only_terminal_opens_the_bars_gate() -> None:
    from tests.test_owner_protector import BBB

    strategy = Cold()
    second = position()
    second.instrument_id = BBB
    strategy.cached.held.append(second)
    strategy.on_start()
    strategy.seed()
    # A payload is only a chunk. Its arrival and another holding's terminal completion cannot
    # make AAA ready; the callback supplied with AAA's request alone completes that request.
    strategy.on_historical_bars(quiet_day())
    strategy.bar_requests[1]["callback"]("bars-2")
    strategy.step()
    assert strategy.slots[AAA].level is None
    strategy.bar_requests[0]["callback"]("bars-1")
    assert strategy.slots[AAA].level is not None


# The lead's ruling, 2026-11-13 19:23, on L6's composition finding: a holding with no level is
# planned only from a quote KNOWN CURRENT for it: (i) stamped at or after the holding opened, or
# (ii) the venue's latest as a seed read REQUESTED at or after the holding opened returns it,
# whatever its stamp, inside the open session part. Anything else is named and waits.
OPENED = T0 + 3600 * SECOND


def opened_later(seed: QuoteTick | None, *, clock: int = OPENED) -> Harness:
    """Flat at T0 with a 10.00 maximum received; an EXTERNAL long of 10 opens an hour later, and
    the step that finds it reads the seed (`seed`, or nothing) with the clock at `clock`."""
    strategy = Harness()
    strategy.cached.held = []
    strategy.on_start()
    strategy.on_quote(quote("10.00", T0))
    later = position("10")
    later.ts_opened = later.ts_init = OPENED
    strategy.cached.held = [later]
    strategy.cached.latest = seed
    strategy.test_clock.set_time(clock)
    strategy._on_step(cast(Any, SimpleNamespace()))
    return strategy


def planned_from(strategy: Harness, bid: str) -> bool:
    slot = strategy.slots[AAA]
    assert slot.level is not None and slot.distance is not None, strategy.said
    return slot.level.as_decimal() == up_on_step(Decimal(bid) - slot.distance.distance)


def test_an_older_maximum_with_no_read_waits_and_the_first_quote_after_the_fill_plans() -> None:
    """Case 1: an hour-old maximum, and a seed read that returns nothing."""
    strategy = opened_later(None)
    assert strategy.slots[AAA].level is None, f"armed at {strategy.slots[AAA].level}"
    assert said(strategy, "AAA.ALPACA", "unprotected", "before the holding opened")
    strategy.observe("8.00", OPENED + SECOND)
    assert planned_from(strategy, "8.00")
    for n, bid in ((2, "7.99"), (3, "7.98")):
        strategy.observe(bid, OPENED + n * SECOND)
    assert all(o.trigger_price.as_decimal() < Decimal("8.00") for o in strategy.sent)


def test_a_read_after_the_fill_that_returns_a_newer_quote_plans_from_it() -> None:
    """Case 2: the read returns a quote newer than the maximum though older than the fill."""
    strategy = opened_later(quote("8.50", OPENED - 600 * SECOND))
    assert planned_from(strategy, "8.50")


def test_a_read_after_the_fill_that_confirms_the_old_stamp_plans_from_it_with_its_age() -> None:
    """Case 3: the read returns the maximum's own stamp: the quiet name's quote is current."""
    strategy = opened_later(quote("10.00", T0))
    assert planned_from(strategy, "10.00")
    assert said(strategy, "AAA.ALPACA: armed distance", "quote age 3600 s")


def test_a_read_requested_before_the_holding_opened_confirms_nothing() -> None:
    """Case 4: the fill's stamp runs ahead of the node's clock; the read made before it is no
    evidence the maximum is current for this holding."""
    strategy = opened_later(quote("10.00", T0), clock=OPENED - 100 * SECOND)
    assert strategy.slots[AAA].level is None, f"armed at {strategy.slots[AAA].level}"
    assert said(strategy, "AAA.ALPACA", "unprotected", "before the holding opened")


def test_a_seed_answered_after_the_holding_opened_but_requested_before_confirms_nothing() -> None:
    """Case 4 with the answer's receipt after the opening: (iii) is the STREAM's form of (ii); a
    seed answer is judged by when it was requested, never by when it arrived."""
    strategy = Harness()
    strategy.cached.held = []
    strategy.on_start()
    strategy.on_quote(quote("10.00", T0))
    later = position("10")
    later.ts_opened = later.ts_init = OPENED
    strategy.cached.held = [later]
    strategy.test_clock.set_time(OPENED - 100 * SECOND)
    answered = QuoteTick(
        AAA,
        Price.from_str("10.00"),
        Price.from_str("10.02"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        T0,
        OPENED + SECOND,  # Received after the holding opened, for a read requested before it.
    )
    strategy.cached.latest = answered
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert strategy.slots[AAA].level is None, f"armed at {strategy.slots[AAA].level}"


# The lead's ruling A, 2026-11-13 19:49, on clause (iii).


def test_a_reopened_holding_is_not_planned_from_a_quote_received_in_its_earlier_opening() -> None:
    """Term 2: (iii) is judged against THIS opening. At the pin a NETTING position that reopens takes
    the reopening fill's ts_init and ts_event (crates/model/src/position.rs:717-718, reset_cycle),
    so the protector's new holding has a new host time."""
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    strategy.on_quote(quote("10.00", T0 + 10 * SECOND))  # Received in the first opening.
    strategy.cached.held = []
    strategy.cached.working.clear()
    strategy.test_clock.set_time(T0 + 20 * SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    again = position("10")
    again.ts_opened = again.ts_init = OPENED
    strategy.cached.held = [again]
    strategy.test_clock.set_time(OPENED)
    strategy._on_step(cast(Any, SimpleNamespace()))
    slot = strategy.slots[AAA]
    assert slot.initialized_at == OPENED
    assert slot.level is None, f"armed at {slot.level} from the earlier opening's quote"


def test_a_plan_on_the_receipt_clause_alone_is_named_with_both_stamps_and_counted() -> None:
    """Term 3, a named limit: (iii) trusts the host's receipt, so a plan it alone allowed is named
    once with the quote's stamp and receipt, and the END line counts such plans."""
    strategy = Harness()
    strategy.cached.latest = None
    strategy.on_start()
    receipt = T0 + SECOND
    late = QuoteTick(
        AAA,
        Price.from_str("10.00"),
        Price.from_str("10.02"),
        Quantity.from_int(100),
        Quantity.from_int(100),
        T0 - 3 * SECOND,  # Stamped before the holding opened at T0, by the venue's clock.
        receipt,
    )
    strategy.test_clock.set_time(receipt)
    strategy.on_quote(late)
    assert strategy.slots[AAA].level is not None, strategy.said
    named = said(strategy, "AAA.ALPACA", "planned on its receipt alone")
    assert len(named) == 1, strategy.said
    assert str(T0 - 3 * SECOND) in named[0] and str(receipt) in named[0]
    strategy.on_quote(quote("10.01", T0 + 2 * SECOND))
    assert len(said(strategy, "planned on its receipt alone")) == 1
    strategy.on_stop()
    assert said(strategy, "AAA.ALPACA: END", "receipt-clause plans=1"), strategy.said


def test_previous_holdings_pending_seed_cannot_certify_a_reopening():
    """Codex's P2-a on 62941f39 (its drop-in, verbatim): request 1, sent for the holding that went
    flat, answers with a quote stamped before the re-opening while the new holding's request 2 is
    still in flight. The lead's rule of 19:49 as meant (20:31): a seed certifies only when every
    request in flight for the instrument was sent by the present holding at or after its opening."""
    strategy = Cold()
    strategy.on_start()
    strategy.cached.held = []
    strategy.step()
    reopened = position()
    reopened.ts_opened = reopened.ts_init = AFTER_HOURS + 2 * SECOND
    strategy.cached.held = [reopened]
    strategy.step()
    assert len(strategy.quote_requests) == 2
    strategy.answer("bars", [], index=1)
    strategy.answer("quotes", [quote("10.00", AFTER_HOURS - 20 * SECOND)], index=0)
    assert strategy.slots[AAA].level is None, strategy.said


def test_once_the_old_request_lands_the_present_holdings_own_seed_certifies():
    """The token of a landed request leaves with its callback, so the present holding's own read,
    sent after it opened, still certifies the venue's latest whatever its stamp (clause ii)."""
    strategy = Cold()
    strategy.on_start()
    strategy.cached.held = []
    strategy.step()
    reopened = position()
    reopened.ts_opened = reopened.ts_init = AFTER_HOURS + 2 * SECOND
    strategy.cached.held = [reopened]
    strategy.step()
    strategy.answer("bars", [], index=1)
    strategy.answer("quotes", [quote("10.00", AFTER_HOURS - 20 * SECOND)], index=0)
    assert strategy.slots[AAA].level is None
    strategy.answer("quotes", [quote("10.00", AFTER_HOURS - 10 * SECOND)], index=1)
    assert strategy.slots[AAA].level is not None, strategy.said
    assert strategy.seeds_in_flight[AAA] == []
