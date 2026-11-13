"""Every reader of the last trade reads the newest print by its own time, not the one received last.

At the pin `Cache.trade` is the front of the instrument's deque, the trade most recently added, and
"Entries are not sorted by timestamp" (crates/common/src/cache/mod.rs:6364-6373); the data engine
caches and publishes a trade without checking its order (crates/data/src/engine/dispatch.rs:117-128).
An older print reported late is therefore the cache's last trade. The price the trigger follows, the
kept-trigger compare, the held stop and a sale priced with no bid never move on such a print, and each
one set aside is counted by name with its lateness (the lead's unit (a), S1 to S3)."""

from collections import deque
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import Price
from nautilus_trader.model import TriggerType

from canon_trading.mover import Mover
from canon_trading.mover import Slot
from canon_trading.price_step import exit_price
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Seam
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import rank
from tests.test_mover import ranker
from tests.test_mover import run
from tests.test_mover import seam_slot
from tests.test_mover import strategy
from tests.test_mover_breach import MS
from tests.test_mover_breach import prefix
from tests.test_mover_breach import print_at
from tests.test_mover_breach import quote_at
from tests.test_mover_print_breach import holding
from tests.test_mover_print_breach import stamped

NEWER, LATE = T0 + 2 * SECOND, T0 + SECOND
"""The newer print's time and the late one's, one second older, received after it."""


def _print(price: str, ts: int) -> SimpleNamespace:
    return SimpleNamespace(instrument_id=AAA, price=Price.from_str(price), ts_event=ts, ts_init=ts)


def _late_print_seam(bid: str = "10.20", **rows: Any) -> tuple[Any, Any]:
    """A slot that saw 9.50 and then, reported late, an older 10.00: the cache's last trade."""
    seam = cast(Any, Seam(bid, **rows))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    newer, older = _print("9.50", NEWER), _print("10.00", LATE)
    newer.ts_init, older.ts_init = NEWER + 5, NEWER + 10
    for trade in (newer, older):
        Mover.on_trade(seam, trade)
    seam.cache.trade = lambda _: older
    return seam, slot


@pytest.mark.parametrize("persistence", [False, True])
def test_the_held_stop_reads_the_newest_print_not_the_last_received(persistence: bool) -> None:
    rows: dict[str, Any] = (
        {"breach_count": 2, "trigger": TriggerType.LAST_PRICE} if persistence else {}
    )
    seam, slot = _late_print_seam(**rows)
    assert seam._last_known(slot) == (D("9.50"), "last trade")


def test_a_last_price_trail_follows_the_newest_print_not_the_last_received() -> None:
    seam, slot = _late_print_seam(trigger=TriggerType.LAST_PRICE)
    assert seam._followed(slot) == D("9.50")


def test_a_kept_trigger_met_by_the_newest_print_sells_though_a_late_print_is_above_it() -> None:
    """A trailing sale had reached 9.60 and was canceled. The newest print, 9.50, is through it; an
    older 10.00 reported late does not make the step remake a trailing sale above the market."""
    seam, slot = _late_print_seam(trigger=TriggerType.LAST_PRICE)
    slot.trail_trigger = D("9.60")
    seam._protect(slot, D(3), NEWER + 5 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting


def test_with_no_bid_the_plain_sale_is_priced_from_the_newest_print_with_no_arm_on() -> None:
    """With no persistence arm on, a sale with no bid is priced from the newest print too: an older
    10.00 reported late does not put the limit over the market at 9.50."""
    seam, slot = _late_print_seam(bid="0")
    seam._limit_sell(slot, D(5), NEWER + SECOND, None)
    expected = exit_price(D("9.50"), seam.settings.exit_ticks, None)
    assert seam.sent[-1].price.as_decimal() == expected


def test_a_late_print_from_before_the_fill_does_not_turn_the_arming_step_into_a_held_stop() -> None:
    """The entry filled between the two prints by the venue's time. The print received last is the
    late one, from before the fill; the newest, after it, is of this holding, so the step arms the
    trailing sale on it and holds no stop (the lead's S3)."""
    seam, slot = _late_print_seam(trigger=TriggerType.LAST_PRICE)
    seam.fills.append(("E-1", "BUY", "3", "10.00", T0 + SECOND + SECOND // 2))
    assert seam._followed(slot) == D("9.50")
    seam._protect(slot, D(3), NEWER + 5 * SECOND)
    assert slot.held_since is None and seam.protect_paths["held_stop"] == 0
    assert seam.sent and seam.sent[0].kind == "TRAILING"


def test_a_print_older_than_the_newest_is_set_aside_by_name_with_its_lateness() -> None:
    seam, slot = _late_print_seam()
    assert slot.newest_print.price == D("9.50")
    assert seam.late_prints == {"AAA.ALPACA": {"count": 1, "most_late_ms": 1000}}
    Mover.on_trade(seam, _print("9.40", NEWER - SECOND // 2))
    assert seam.late_prints == {"AAA.ALPACA": {"count": 2, "most_late_ms": 1000}}


def test_a_print_of_the_same_time_is_not_late_and_is_read_in_the_order_it_arrived() -> None:
    seam, slot = _late_print_seam()
    Mover.on_trade(seam, _print("9.45", NEWER))
    assert slot.newest_print.price == D("9.45")
    assert seam.late_prints["AAA.ALPACA"]["count"] == 1
    seam.late_prints.clear()
    Mover.on_trade(seam, _print("9.44", NEWER - 1))  # 1 ns late reads as 1 ms, as the adapter's
    assert seam.late_prints == {"AAA.ALPACA": {"count": 1, "most_late_ms": 1}}


def test_the_run_record_carries_the_late_prints_set_aside() -> None:
    s = strategy()
    s.late_prints["AAA.ALPACA"] = {"count": 3, "most_late_ms": 250}
    assert s.record()["late_prints_set_aside"] == {"AAA.ALPACA": {"count": 3, "most_late_ms": 250}}


def test_a_late_print_through_the_real_data_engine_is_set_aside_and_carried_in_the_record() -> None:
    """Through the engine's own data path: a print stamped 300 ms before one already received
    arrives after it. The cache's last trade is the late one; the slot keeps the newer, and the
    run's record names the one set aside with its lateness."""
    s = strategy(trigger=TriggerType.LAST_PRICE)
    eng = engine()
    newer = 4 * MINUTE + 3 * SECOND + 600 * MS
    try:
        run(
            eng,
            s,
            ranker(rank((AAA, "0.1"))),
            prefix(confirm=False),
            [
                print_at(newer, "10.33"),
                print_at(newer - 300 * MS, "10.45", received=newer + 50 * MS),
                quote_at(newer + SECOND),
            ],
        )
        cached = eng.cache.trade(AAA)
        assert cached is not None and cached.price == Price.from_str("10.4500")
        slot = s.slots[AAA]
        assert slot.newest_print is not None and slot.newest_print.price == D("10.33")
        assert s._last_known(slot) == (D("10.33"), "last trade")
        late = s.record()["late_prints_set_aside"]
        assert late == {"AAA.ALPACA": {"count": 1, "most_late_ms": 300}}
    finally:
        eng.dispose()


def test_a_print_older_than_the_newest_at_arming_counts_toward_no_breach() -> None:
    """The persistence count starts after the newest print already received, by its own stamp: a
    print reported after arming but older than that one is history, and confirms nothing, however
    far through the trigger it is (the lead's S1)."""
    seam = cast(Any, Seam("10.20", breach_count=2, trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    Mover.on_trade(seam, _print("10.05", NEWER))
    seam._persistent_trail(slot, NEWER + SECOND)
    assert slot.trail_trigger is not None and slot.observed_ns == NEWER
    for at in (LATE, LATE + SECOND // 2):
        Mover.on_trade(seam, _print("9.00", at))
    seam._persistent_trail(slot, NEWER + 2 * SECOND)
    assert not slot.breach_observations and not slot.exiting


def test_a_print_stamped_ahead_within_the_bound_hides_no_on_time_print_from_the_count() -> None:
    """A print stamped 4 s ahead of its receipt orders the readers by the venue's time, so the
    on-time prints after it are set aside there; the persistence count keeps its own stamp and
    reads every one of them (the lead's ruling (b), its point 3)."""
    seam = cast(Any, Seam("10.20", breach_count=2, trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    slot.trail_trigger = D("9.40")
    ahead = _print("9.50", T0 + 4 * SECOND)
    ahead.ts_init = T0
    Mover.on_trade(seam, ahead)
    for k in (1, 2, 3):
        Mover.on_trade(seam, _print("9.00", T0 + k * SECOND))
    assert [stamp for stamp, _ in slot.observations] == [T0 + k * SECOND for k in (0, 1, 2, 3)]
    assert seam.late_prints["AAA.ALPACA"]["count"] == 3


def test_a_slot_opened_after_prints_starts_from_the_newest_the_cache_holds() -> None:
    """The cache holds prints of a name before its slot opens, the last received a late one. The
    slot starts from the newest by its own time, so the first print after it opens, older than
    that, is set aside and counted, not read as the newest."""
    s = strategy(trigger=TriggerType.LAST_PRICE)
    eng = engine()
    newest = 30 * SECOND
    try:
        run(
            eng,
            s,
            ranker(rank(), values={2: rank((AAA, "0.1"))}),
            [
                quote_at(0, "9.99", "10.01"),
                print_at(newest, "10.10"),
                print_at(20 * SECOND, "10.40", received=40 * SECOND),
                print_at(25 * SECOND, "10.60", received=2 * MINUTE + 10 * SECOND),
                quote_at(2 * MINUTE + 20 * SECOND),
            ],
        )
        slot = s.slots[AAA]
        assert slot.newest_print is not None and slot.newest_print.price == D("10.10")
        assert s._last_known(slot) == (D("10.10"), "last trade")
        late = s.record()["late_prints_set_aside"]
        assert late == {"AAA.ALPACA": {"count": 1, "most_late_ms": 5000}}
    finally:
        eng.dispose()


def test_a_host_clock_behind_the_venue_still_sets_an_older_print_aside_by_its_own_time() -> None:
    """This host's clock 100 ms behind the venue's, the feed 20 ms late: every print arrives before
    its own time on this clock. A print of the venue's T+80 ms, then one of T+30 ms received after
    it: the second is older at the venue and is set aside, 50 ms late (the lead's ruling (b))."""
    seam = cast(Any, Seam("10.20", trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    newer = _print("9.50", T0 + 80 * MS)
    newer.ts_init = T0
    older = _print("10.00", T0 + 30 * MS)
    older.ts_init = T0 + MS
    for trade in (newer, older):
        Mover.on_trade(seam, trade)
    seam.cache.trade = lambda _: older
    assert slot.newest_print is not None and slot.newest_print.price == D("9.50")
    assert seam._followed(slot) == D("9.50")
    assert seam.late_prints == {"AAA.ALPACA": {"count": 1, "most_late_ms": 50}}


def test_a_print_stamped_far_ahead_of_its_receipt_never_becomes_the_newest() -> None:
    """A print whose own time is more than `PRINT_AHEAD_MOST_NS` ahead of its receipt has no time
    that can order it: it is not set aside and not the newest, so it hides none of the prints that
    follow it, and the cache holding it last does not make it the price read."""
    seam, slot = _late_print_seam(trigger=TriggerType.LAST_PRICE)
    ahead = _print("12.00", NEWER + 20 * SECOND)
    ahead.ts_init = NEWER + MS
    Mover.on_trade(seam, ahead)
    seam.cache.trade = lambda _: ahead
    assert slot.newest_print.price == D("9.50") and seam._followed(slot) == D("9.50")
    after = _print("9.40", NEWER + SECOND)
    Mover.on_trade(seam, after)
    assert slot.newest_print.price == D("9.40")
    assert seam.late_prints["AAA.ALPACA"]["count"] == 1
    assert seam.prints_ahead == {"AAA.ALPACA": 1}


def test_the_run_record_carries_the_prints_stamped_ahead() -> None:
    s = strategy()
    s.prints_ahead["AAA.ALPACA"] = 2
    assert s.record()["prints_stamped_ahead"] == {"AAA.ALPACA": 2}


def test_the_window_names_prints_stamped_ahead_only_when_there_are_any() -> None:
    """The lead's 23:09 addition: the window's own Mover record names the prints stamped ahead of
    their receipt whenever there are any, beside its run record; a count of them means this host's
    clock ran behind the venue's and the ordering was off for that window. Reporting only."""
    from canon_trading import session_window

    assert session_window.clock_notes({"prints_stamped_ahead": {}}) == {}
    assert session_window.clock_notes({}) == {}
    assert session_window.clock_notes({"prints_stamped_ahead": {"AAA.ALPACA": 0}}) == {}
    ahead = {"prints_stamped_ahead": {"AAA.ALPACA": 3}}
    assert session_window.clock_notes(ahead) == {"prints_stamped_ahead": {"AAA.ALPACA": 3}}


@pytest.mark.parametrize(
    "event,received,expected",
    [(3 * SECOND, SECOND, D("9.50")), (SECOND, 3 * SECOND, None), (2 * SECOND, SECOND, D("9.50"))],
    ids=["after-fill", "before-fill", "at-fill"],
)
def test_holding_readers_compare_the_prints_venue_time_with_the_fill(
    event: int, received: int, expected: D | None
) -> None:
    """A pre-fill print cannot arm or cross this holding's stop, even if received after the fill."""
    seam = cast(Any, Seam("10.20", trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    seam.fills.append(("E-1", "BUY", "3", "10.00", T0 + 2 * SECOND))
    trade = _print("9.50", T0 + event)
    trade.ts_init = T0 + received
    Mover.on_trade(seam, trade)
    seam.cache.trade = lambda _: trade
    assert seam._followed(slot) == expected
    assert seam._last_known(slot) == (expected, "none" if expected is None else "last trade")


@pytest.mark.parametrize("ahead", [False, True])
def test_window_record_keeps_late_counts_under_run_and_names_only_positive_ahead_counts(
    monkeypatch, tmp_path, references_in, ahead: bool
) -> None:
    """Run the window's real report assembly with the existing offline account/data/node seam."""
    from canon_trading import session_window
    from tests.test_session_window import Evening
    from tests.test_session_window import Quoted
    from tests.test_session_window import a_pick
    from tests.test_session_window import quiet_window

    class ReportingMover(Mover):
        def __init__(self, config):
            super().__init__(config)
            self.slots[AAA] = Slot(AAA, bar_type(AAA), deque(), deque())
            self.on_trade(_print("10.00", NEWER))
            late = _print("9.00", LATE)
            late.ts_init = NEWER + SECOND
            self.on_trade(late)
            if ahead:
                future = _print("99.00", NEWER + 20 * SECOND)
                future.ts_init = NEWER + 2 * SECOND
                self.on_trade(future)

    monkeypatch.setenv(session_window.ACCOUNT_ENV, "windows")
    monkeypatch.setattr(session_window, "Mover", ReportingMover)
    monkeypatch.setattr(session_window, "choose_mover", lambda *a, **k: a_pick())
    monkeypatch.setattr(session_window, "window_data", lambda: Quoted())
    _, latest, _ = quiet_window(
        monkeypatch,
        tmp_path,
        quotes=5,
        trades=5,
        account=Evening(17),
        session="extended",
        mover=True,
    )
    assert latest["result"] == "ok"
    window = latest["mover"]
    assert window["run"]["late_prints_set_aside"] == {
        "AAA.ALPACA": {"count": 1, "most_late_ms": 1000}
    }
    assert ("prints_stamped_ahead" in window) is ahead
    if ahead:
        assert window["prints_stamped_ahead"] == {"AAA.ALPACA": 1}
        assert window["run"]["prints_stamped_ahead"] == {"AAA.ALPACA": 1}


def test_with_the_host_clock_far_behind_and_no_print_kept_the_trail_follows_the_cache() -> None:
    """Review R3 (2026-11-13): with this host's clock more than 5 s behind the venue's, every print
    is stamped ahead and none is kept. The trail still follows the cache's last trade, as before,
    instead of finding no price and holding the stop to an unpriced sale."""
    seam = cast(Any, Seam("10.20", trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    ahead = _print("9.70", T0 + 10 * SECOND)
    ahead.ts_init = T0  # its own time 10 s ahead of its receipt
    Mover.on_trade(seam, ahead)
    seam.cache.trade = lambda _: ahead
    assert slot.newest_print is None
    assert seam._followed(slot) == D("9.70")
    assert seam.prints_ahead == {str(AAA): 1}


def test_a_print_set_aside_as_late_still_counts_toward_the_hold_s_low() -> None:
    """The lead's ruling of 2026-11-13 (09:27, F1 (b)): a holding's low is the lowest print traded at or
    after the holding began, whatever order the prints arrive in. Held at 9.80 under a print trigger,
    the holding prints 9.90, and then a print of 9.70 arrives, stamped by the venue a second before the
    9.90 but after the fill. It is set aside as late, so it is not the newest price, but it is the
    hold's low: the step sells, priced from the bid with the 9.90 as its followed price
    (deepseek-w1-d-lpm/1)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    seam.slots, seam.trades = {slot.instrument_id: slot}, 0
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, D(3), filled + SECOND)
    assert slot.held_stop is not None
    for price, at, received in (
        ("9.9000", filled + 3 * SECOND, filled + 3 * SECOND),
        ("9.7000", filled + 2 * SECOND, filled + 3 * SECOND + 1),
    ):
        trade = SimpleNamespace(
            instrument_id=slot.instrument_id,
            price=Price.from_str(price),
            ts_event=at,
            ts_init=received,
        )
        last.append(trade)
        seam.on_trade(trade)
    assert seam.late_prints[str(slot.instrument_id)]["count"] == 1
    assert slot.newest_print is not None and slot.newest_print.price == D("9.90")
    assert slot.held_low == D("9.7000")
    seam._protect(slot, D(3), filled + 4 * SECOND)
    assert [o.kind for o in seam.sent] == ["LIMIT"] and slot.exiting
    assert slot.exit_record.trigger_reason == "held stop (followed price)"


def test_a_late_print_from_before_the_holding_began_counts_toward_no_low() -> None:
    """The same ruling: a print from before the holding began never counts, set aside as late or not.
    A 9.70 stamped before the entry's acceptance, arriving after the 9.90, moves neither the newest
    price nor the hold's low, and the hold's end arms the trail."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    seam.slots, seam.trades = {slot.instrument_id: slot}, 0
    last.append(stamped("10.0000", filled - 3 * SECOND, filled - 3 * SECOND))
    seam._protect(slot, D(3), filled + SECOND)
    since = seam._holding_since(slot)
    assert since is not None
    for price, at, received in (
        ("9.9000", filled + 3 * SECOND, filled + 3 * SECOND),
        ("9.7000", since - 1, filled + 3 * SECOND + 1),
    ):
        trade = SimpleNamespace(
            instrument_id=slot.instrument_id,
            price=Price.from_str(price),
            ts_event=at,
            ts_init=received,
        )
        last.append(trade)
        seam.on_trade(trade)
    assert slot.held_low == D("9.9000")
    seam._protect(slot, D(3), filled + 4 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING"] and not slot.exiting


def test_with_no_entry_fill_on_record_a_late_print_counts_toward_no_low() -> None:
    """The same ruling: a print from before the holding began never counts. With no entry fill on
    record, as for a holding adopted after a restart, when the holding began cannot be read, so a print
    set aside as late counts toward no low; a print kept as the newest still does, as on main
    (code-review-high-host-clock-v5/4)."""
    seam, slot, filled, last = holding(TriggerType.LAST_PRICE, bid="9.89", ask="9.91")
    seam.slots, seam.trades = {slot.instrument_id: slot}, 0
    last.append(stamped("10.0000", filled - SECOND, filled - SECOND))
    seam._protect(slot, D(3), filled + SECOND)
    assert slot.held_stop is not None
    seam.fills = []
    assert seam._holding_since(slot) is None
    for price, at, received in (
        ("9.9000", filled + 3 * SECOND, filled + 3 * SECOND),
        ("9.7000", filled - 600 * SECOND, filled + 3 * SECOND + 1),
    ):
        trade = SimpleNamespace(
            instrument_id=slot.instrument_id,
            price=Price.from_str(price),
            ts_event=at,
            ts_init=received,
        )
        last.append(trade)
        seam.on_trade(trade)
    assert seam.late_prints[str(slot.instrument_id)]["count"] == 1
    assert slot.held_low == D("9.9000")


@pytest.mark.parametrize(
    ("trigger", "anchor"), [(TriggerType.LAST_PRICE, None), (TriggerType.BID_ASK, D("9.50"))]
)
def test_with_no_print_at_all_a_sale_with_no_bid_anchors_as_the_held_stop_reads(
    trigger: TriggerType, anchor: D | None
) -> None:
    """With no print cached or kept, the anchor of a sale with no bid is what the held stop reads: under
    a print trigger nothing, since a minute close is no price of this holding (the trigger unit's
    code-review-high-succ/3), and under the bid the latest minute close (P7)."""
    seam = cast(Any, Seam("10.20", trigger=trigger))
    slot = seam_slot(seam)
    seam.cache.trade = lambda _: None
    slot.closes = deque([D("8.50"), D("9.50")], maxlen=4)
    assert seam._last_print(slot) == anchor


def test_persistence_arms_after_the_highest_stamp_received_not_the_newest_prints() -> None:
    """Persistence keeps its own watermark (P2). A print kept as the newest at the venue but received
    early (event 10 s, received 7 s) and a print set aside as late but received after it (event 9 s,
    received 9 s): the count starts after 9 s, so a print reported later with a stamp of 8 s confirms
    nothing, however far through the trigger it is."""
    seam = cast(Any, Seam("10.20", breach_count=2, trigger=TriggerType.LAST_PRICE))
    slot = seam_slot(seam)
    seam.slots, seam.trades = {AAA: slot}, 0
    kept = _print("10.05", T0 + 10 * SECOND)
    kept.ts_init = T0 + 7 * SECOND
    late = _print("10.04", T0 + 9 * SECOND)
    for trade in (kept, late):
        Mover.on_trade(seam, trade)
    assert seam.late_prints["AAA.ALPACA"]["count"] == 1
    seam._persistent_trail(slot, T0 + 10 * SECOND)
    assert slot.trail_trigger is not None and slot.observed_ns == T0 + 9 * SECOND
    for at in (T0 + 8 * SECOND, T0 + 8 * SECOND + SECOND // 2):
        older = _print("9.00", at)
        older.ts_init = T0 + 11 * SECOND
        Mover.on_trade(seam, older)
    seam._persistent_trail(slot, T0 + 12 * SECOND)
    assert not slot.breach_observations and not slot.exiting
