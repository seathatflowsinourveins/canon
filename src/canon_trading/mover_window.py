"""The mover window's composition, registered rows, context supply and entry diagnostics (ADRs 0088/0091).

Contract: NautilusTrader v2.0.0rc8 at d53849b216e2, DataActor's signal API, subscribe_bars,
request_bars/on_historical_bars/on_data_response and clock timers (common/__init__.pyi); distinct actor
and strategy IDs (concepts/strategies); Mover's explicit Day input contract from ADR 0092. Read
2026-11-11. The ranking is ADR 0107's actor, constructed only after session_window initializes because
its arithmetic module reads that module's time helpers.

mover_universe_parts builds one strategy and a position monitor per loaded roster name. Paper callers
pass the startup account's entry figure and select whole-share budget sizing; each name's share cap
and position monitor follow that figure at its current entry limit. The engine's per-order outer bound
is a larger multiple because it caps sells too. Legacy FixedRank simulations retain their explicit
500/5,000 row. Slots default to ten in regular/after-hours and one in pre-market/overnight; entries
have no bound and continue after sales until wind-down, under cooldown and reclaim rules. The window's
spares setting adds watched names for rotation.

window_rank retains every tick's public record from ADR 0107's actor. No actor generation means no
entry; the startup roster's order is never substituted. mover_context_feeds keeps each roster name's
Day prefix current even while it is unranked, through the node's existing data client and external
minute bars; a completed native history request repairs a gap, and a failed one leaves coverage where
it was and is asked again in a later minute. The strategy itself requests no data. entry_diagnostics reads qualification delay and the move
still ahead from the window's own orders, bars and per-name reference closes.

FixedRank/mover_parts remain the explicit single-name bridge used by compatibility simulations. They
publish the original sample timestamp repeatedly and extend their staleness row to the window end;
the live composition uses window_rank and the strategy's ordinary 90-second freshness row.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

from nautilus_trader.common import DataActor
from nautilus_trader.common import DataActorConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import ActorId
from nautilus_trader.model import BarType
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .mover import RANK_SIGNAL
from .mover import Mover
from .mover import MoverConfig
from .mover import format_rank
from .position_cap import PositionCap
from .position_cap import PositionCapConfig

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar
    from nautilus_trader.model import InstrumentId

    from .early_entry_study import Day
    from .movers_rank_actor import MoversRankActor
    from .movers_rank_actor import MoversRankActorConfig

MOVER = "Mover"
"""The name `--strategy` takes."""
MOVER_ID = StrategyId("Mover-001")
ENTRY_NOTIONAL_USD = Decimal(500)
"""The original explicit simulation row. Paper windows always supply their startup account figure."""
OUTER_BOUND_MULTIPLE = 10
"""The risk engine's per-order notional cap for a mover node, as a multiple of its startup entry notional
(UNCALIBRATED): an outer bound against a runaway order, which a sale meets only once the position is
worth this many times its entry."""
OUTER_BOUND_USD = ENTRY_NOTIONAL_USD * OUTER_BOUND_MULTIPLE
NO_CHASE_CAP = Decimal(100)
"""`MoverConfig.max_move` for a window: 100 times the last close, above any move the selection has shown,
so the row is stated and does not bind until a measurement sets it (UNCALIBRATED)."""
ENTRIES: int | None = None
"""Entries a window may fill: no bound. A completed trade does not end the node; it re-enters or
rotates until the part ends (the lead's decision of 2026-11-11, Task M's gap 6), under `Mover`'s
cooldown and reclaim rows."""
PART_SLOTS = 7
"""The names a window's node holds at once in every part (the lead's ruling of 2026-11-12): the worst
measured minute is 175 trading requests against the 180 throttle, and ten slots would pass it (ADR
0083). A run's `--slots` sets its own."""
CHASE = "resend"
"""How a window re-prices a resting sale: cancel, see the cancel's terminal state at the next step, then
send the new limit for what is held. No order of a mover node is modified at the venue (the lead's
decision of 2026-11-11, until the adapter's replace chain has passed two clean outside reviews); the cost
is two requests a re-price and a moment with no working sale."""
LIVE_BARS = "1-MINUTE-LAST-INTERNAL"
"""One-minute entry bars the data engine builds from the trade stream."""
RANK_REPEAT_SECS = 15
"""How often `FixedRank` repeats its ranking."""
REPEAT_TIMER = "fixed-rank"
CONTEXT_REFRESH_SECS = 1
"""Gap requests are started by a timer, one outstanding per name, rather than from a bar callback.
The interval is UNCALIBRATED; completed live bars need no request."""


def mover_context_feeds(strategy: Mover, feed: str) -> tuple[DataActor, ...]:
    """Follow every loaded name's external bars, including names not currently ranked/watched.

    Contract: rc8 DataActor.subscribe_bars/request_bars, on_historical_bars/on_data_response and clock
    timers (`common/__init__.pyi:830,1066-1086`); the request/response pair is also bars_to_catalog's.
    A native request fills a startup or stream gap; only its successful completion grants coverage,
    including sparse no-trade minutes. A failed read leaves coverage unchanged, so no entry passes past
    it, and is asked again at the first refresh of a later minute. No client,
    stream or credential is created here. Each actor has at most one request outstanding.
    """
    return tuple(
        _context_feed(strategy.entry_contexts, instrument, feed, index)
        for index, instrument in enumerate(strategy.entry_contexts, start=1)
    )


def _context_feed(
    contexts: dict[InstrumentId, Day], instrument: InstrumentId, feed: str, index: int
) -> DataActor:
    from . import early_entry_study as study

    bar_type = BarType.from_str(f"{instrument}-1-MINUTE-LAST-EXTERNAL")
    params = {"feed": feed}
    timer = "entry-context"

    class ContextFeed(DataActor):
        pending = False
        need_history = True
        target = 0
        failed_target: int | None = None

        def on_start(self) -> None:
            self.subscribe_bars(bar_type, params=params)
            self.clock.set_timer(
                timer, timedelta(seconds=CONTEXT_REFRESH_SECS), callback=self._refresh
            )
            self._refresh(None)

        def on_stop(self) -> None:
            if timer in self.clock.timer_names():
                self.clock.cancel_timer(timer)

        def _refresh(self, event: TimeEvent | None) -> None:
            day = contexts[instrument]
            if self.pending or day.missing is not None:
                return
            now = datetime.fromtimestamp(
                self.clock.timestamp_ns() // 1_000_000_000, UTC
            ).astimezone(study.NEW_YORK)
            self.target = min(study.WINDOW_END, max(study.PART_START, now.hour * 60 + now.minute))
            if self.failed_target is not None and self.target <= self.failed_target:
                return  # a failed read is asked again once the minute moves, not every tick
            if self.target <= day.covered_to:
                self.need_history = False
                return
            self.pending, self.need_history = True, False
            self.request_bars(
                bar_type,
                start=datetime.combine(
                    day.day, dt_time(day.covered_to // 60, day.covered_to % 60), study.NEW_YORK
                ),
                end=datetime.combine(
                    day.day, dt_time(self.target // 60, self.target % 60), study.NEW_YORK
                ),
                params=params,
            )

        def _advance(self) -> None:
            day = contexts[instrument]
            known = {b.start for b in day.bars}
            covered = day.covered_to
            while covered in known:
                covered += 1
            contexts[instrument] = replace(day, covered_to=covered)
            self.need_history = bool(known and max(known) + 1 > covered)

        def on_bar(self, bar: Bar) -> None:
            day = contexts[instrument]
            opened = datetime.fromtimestamp(bar.ts_event / 1e9, study.NEW_YORK)
            minute = opened.hour * 60 + opened.minute
            if (
                day.missing is not None
                or opened.date() != day.day
                or not study.PART_START <= minute < study.WINDOW_END
            ):
                return
            known = {b.start: b for b in day.bars}
            known[minute] = study.Minute(
                minute,
                float(bar.open.as_decimal()),
                float(bar.high.as_decimal()),
                float(bar.low.as_decimal()),
                float(bar.close.as_decimal()),
                float(bar.volume.as_decimal()),
            )
            contexts[instrument] = replace(day, bars=tuple(known[m] for m in sorted(known)))
            self._advance()

        def on_historical_bars(self, bars: Sequence[Bar]) -> None:
            for bar in bars:
                opened = datetime.fromtimestamp(bar.ts_event / 1e9, study.NEW_YORK)
                if opened.hour * 60 + opened.minute < self.target:
                    self.on_bar(bar)

        def on_data_response(self, response: Any) -> None:
            if not self.pending:
                return
            day = contexts[instrument]
            if response.failure_reason:
                # coverage stays where it was and a later minute asks again, so one failed
                # response does not end the name's entries (the lead's ruling of 2026-11-12 on the
                # ultra review of the composition, finding 5)
                self.log.warning(
                    f"native history for {instrument} failed, asked again in a later minute: "
                    f"{response.failure_reason}"
                )
                self.pending, self.need_history = False, True
                self.failed_target = self.target
            elif response.completed:
                contexts[instrument] = replace(day, covered_to=max(day.covered_to, self.target))
                self.pending, self.failed_target = False, None
                self._advance()

    return ContextFeed(DataActorConfig(actor_id=ActorId(f"EntryContext-{index:03}")))


def window_rank(config: MoversRankActorConfig, records: list[dict[str, object]]) -> MoversRankActor:
    """ADR 0107's actor, with every tick's public last_record retained in `records` for the window.

    The base actor's on_tick performs all ranking and publication; this wrapper only keeps its result.
    Import at construction: the ranking module also reads session_window's time helpers.
    """
    from .movers_rank_actor import MoversRankActor

    class RecordedRank(MoversRankActor):
        def on_tick(self, event: TimeEvent) -> None:
            super().on_tick(event)
            if self.last_record is not None:
                records.append(dict(self.last_record))

    return RecordedRank(config)


class FixedRank(DataActor):
    """Publish one ranking, computed before the node was built, every `RANK_REPEAT_SECS`."""

    def __init__(self) -> None:
        # `DataActor` is a native class whose `__new__` takes only a config, so `mover_parts` sets the
        # ranking after construction (as `bars_to_catalog._RequestBars` is built).
        super().__init__()
        self.value = ""
        self.computed_ns = 0
        self.published = 0

    def on_start(self) -> None:
        self.clock.set_timer(
            REPEAT_TIMER, timedelta(seconds=RANK_REPEAT_SECS), callback=self._publish
        )

    def _publish(self, event: TimeEvent) -> None:
        self.publish_signal(RANK_SIGNAL, self.value, ts_event=self.computed_ns)
        self.published += 1

    def on_stop(self) -> None:
        if self.clock.timer_names().count(REPEAT_TIMER):
            self.clock.cancel_timer(REPEAT_TIMER)


RANK_MAX_AGE_SECS = 90
"""How old a published ranking `Mover` still acts on, by default (`MoverConfig.rank_max_age_secs`)."""


def part_slots(part: str, slots: int | None = None) -> int:
    """The names a session part's node holds at once: `slots` where the run sets it, else
    `PART_SLOTS`, the same in every part. More than `PART_SLOTS` is refused by name: the window's
    account check's deadline, its end margin and its longest window count that many names at most
    (`session_window.after_stop_secs`), until slots become a setting those bounds scale from (the
    lead's ruling of 2026-11-12)."""
    if slots is None:
        return PART_SLOTS
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
        raise ValueError(f"slots must be a positive whole number, got {slots!r}")
    if slots > PART_SLOTS:
        raise ValueError(
            f"slots must be at most {PART_SLOTS}, got {slots}: the account check's deadline and the "
            f"window's end margin count {PART_SLOTS} names at most"
        )
    return slots


def max_shares(price: Decimal, entry_notional: Decimal = ENTRY_NOTIONAL_USD) -> int:
    """The shares the largest entry buys at `price`, the name's price when the window chose it: the
    strategy's bound on an entry in shares, and the position cap's bound."""
    if price <= 0:
        raise ValueError(f"the price must be positive, got {price}")
    if not entry_notional.is_finite() or entry_notional <= 0:
        raise ValueError("entry_notional must be positive and finite")
    return int(entry_notional // price)


def mover_position_caps(
    prices: Mapping[InstrumentId, Decimal], entry_notional: Decimal = ENTRY_NOTIONAL_USD
) -> tuple[PositionCap, ...]:
    """One independent monitor per window name, under distinct upstream strategy IDs and tags
    (`concepts/strategies`, "Multiple strategy instances"). Each cap is the largest entry's shares
    at that name's initial price; these monitors share the node and its engine's outer order bound."""
    caps: list[PositionCap] = []
    for index, (instrument_id, price) in enumerate(prices.items(), start=1):
        shares = max_shares(price, entry_notional)
        if shares < 1:
            raise ValueError(
                f"one share of {instrument_id} at {price} USD is over the largest entry of "
                f"{entry_notional} USD"
            )
        tag = f"CAP{index:03}"
        caps.append(
            PositionCap(
                PositionCapConfig(
                    instrument_id=instrument_id,
                    max_position=Decimal(shares),
                    strategy_id=StrategyId(f"PositionCap-{tag}"),
                    order_id_tag=tag,
                )
            )
        )
    return tuple(caps)


def mover_universe_parts(
    prices: Mapping[InstrumentId, Decimal],
    *,
    extended: bool,
    feed: str | None,
    wind_down_at: datetime,
    market_exit_interval_ms: int,
    market_exit_max_attempts: int,
    slots: int,
    entry_arm: str = "registered",
    entry_contexts: Mapping[InstrumentId, Day] | None = None,
    bar_spec: str = LIVE_BARS,
    rank_max_age_secs: int = RANK_MAX_AGE_SECS,
    entry_notional: Decimal = ENTRY_NOTIONAL_USD,
    size_from_budget: bool = False,
) -> tuple[Mover, tuple[PositionCap, ...]]:
    """One strategy and independent position monitors on the loaded roster. The ranking arrives only
    on the bus; the startup order is not a substitute. Every name has an entry share bound, and a name
    outside the roster cannot enter. The default registered arm gets explicit per-name Day contexts.
    """
    if not prices:
        raise ValueError("a mover window needs at least one priced instrument")
    caps = mover_position_caps(prices, entry_notional)
    shares = {cap.instrument_id: int(cap.max_position) for cap in caps}
    strategy = Mover(
        MoverConfig(
            max_notional=entry_notional,
            max_move=NO_CHASE_CAP,
            max_shares=next(iter(shares.values()))
            if len(shares) == 1 and not size_from_budget
            else None,
            share_caps=shares,
            size_from_budget=size_from_budget,
            entry_arm=entry_arm,
            entries=ENTRIES,
            slots=slots,
            watch=max(slots, len(prices)),
            bar_spec=bar_spec,
            chase=CHASE,
            rank_max_age_secs=rank_max_age_secs,
            extended_hours=extended,
            feed=feed,
            wind_down_at=wind_down_at,
            strategy_id=MOVER_ID,
            order_id_tag="001",
            manage_stop=not extended,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
            market_exit_interval_ms=market_exit_interval_ms,
            market_exit_max_attempts=market_exit_max_attempts,
        )
    )
    strategy.entry_contexts.update(entry_contexts or {})
    strategy.position_caps = {cap.instrument_id: cap for cap in caps}
    return strategy, caps


def mover_parts(
    instrument_id: InstrumentId,
    *,
    price: Decimal,
    move: Decimal,
    computed_at: datetime,
    ends: datetime,
    extended: bool,
    feed: str | None,
    wind_down_at: datetime,
    market_exit_interval_ms: int,
    market_exit_max_attempts: int,
    slots: int = 1,
    entry_notional: Decimal = ENTRY_NOTIONAL_USD,
) -> tuple[Mover, FixedRank, PositionCap]:
    """`Mover` on the one name a window chose, the actor that publishes that choice as the ranking, and
    the position cap at the shares the largest entry buys at `price`. `move` is the name's move from its
    last close at `computed_at`, as the choice measured it; the staleness row reaches from then to
    `ends`, the window's end, since the choice is made once. In the session the wind-down and the stop are upstream's market
    exit with DAY orders that Alpaca takes; outside it every order is a marked limit and the stop
    cancels. `slots` is the part's (`part_slots`), and as many names are watched; entries have no
    bound, so a sold name is bought back on a reclaim after its cooldown, or a freed slot rotates, until
    the wind-down. The entry is sized from `entry_notional` (a paper window's startup account figure;
    the 500-dollar simulation row by default) as a paper window's is: clamped to free cash, capped by the
    flow, with no global share row."""
    strategy, caps = mover_universe_parts(
        {instrument_id: price},
        extended=extended,
        feed=feed,
        wind_down_at=wind_down_at,
        market_exit_interval_ms=market_exit_interval_ms,
        market_exit_max_attempts=market_exit_max_attempts,
        slots=slots,
        entry_arm="bridge",  # the FixedRank compatibility composition; live windows supply context
        rank_max_age_secs=max(1, math.ceil((ends - computed_at).total_seconds())),
        entry_notional=entry_notional,
        size_from_budget=True,
    )
    ranking = FixedRank()
    ranking.value = format_rank([(instrument_id, move)])
    ranking.computed_ns = dt_to_unix_nanos(computed_at)
    return strategy, ranking, caps[0]


def entry_diagnostics(
    entries: Sequence[Mapping[str, Any]],
    fills: Sequence[Sequence[Any]],
    bars: Mapping[str, Sequence[tuple[int, Decimal]]],
    references: Mapping[str, Decimal | None],
) -> list[dict[str, Any]]:
    """Two diagnostics of each entry `Mover` sent (`Mover.record()`'s `entries` and `fills`), read from
    the window's own bars, `bars` by instrument ID as (`ts_init`, high) pairs:

    * `qualified_to_submit_ms`: from the node's receipt of the bar that completed the rising run to the
      entry's creation, both on the node's clock. The trade that made the qualifying close may be up to
      a bar earlier than the bar's close; that wait is the rule's (one-minute bars), not the node's.
    * how much of the move was still ahead at the fill: `rise_left`, the highest high of a bar that
      closed after the fill over the fill's mean price, less one; and `ahead`, that rise as a share of
      the move from the name's reference close (`references`, the prior session's, as the window's
      choice read it) to that high, ADR 0092's measure on the window's tape. With no fill, no bar after
      it, or no reference, the measure is None. Four decimals.

    These are diagnostics for the window's record; no rule and no gate reads them."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        cid, instrument = str(entry["client_order_id"]), str(entry["instrument_id"])
        qualified, submitted = entry.get("qualified_ns"), entry.get("submitted_ns")
        delay = (
            None
            if qualified is None or submitted is None
            else (int(submitted) - int(qualified)) // 1_000_000
        )
        bought = [f for f in fills if f[0] == cid and f[1] == "BUY"]
        qty = sum((Decimal(str(f[2])) for f in bought), Decimal(0))
        px = (
            None
            if not qty
            else sum((Decimal(str(f[2])) * Decimal(str(f[3])) for f in bought), Decimal(0)) / qty
        )
        filled_ns = max((int(f[4]) for f in bought), default=None)
        after = [
            high
            for ts, high in bars.get(instrument, ())
            if filled_ns is not None and ts > filled_ns
        ]
        high = max(after, default=None)
        reference = references.get(instrument)
        rise_left = ahead = None
        if px is not None and high is not None:
            rise_left = _four(high / px - 1)
            if reference is not None and high > reference:
                ahead = _four((high - px) / (high - reference))
        out.append(
            {
                "client_order_id": cid,
                "instrument_id": instrument,
                "qualified_to_submit_ms": delay,
                "entry_px": None if px is None else _four(px),
                "high_after": None if high is None else str(high),
                "rise_left": rise_left,
                "ahead": ahead,
            }
        )
    return out


def window_bars(cache: Any, instrument_ids: Sequence[str]) -> dict[str, list[tuple[int, Decimal]]]:
    """The window's own bars of each of `instrument_ids` (`LIVE_BARS`, as the node's cache holds them,
    `Cache.bars`), oldest first, as `entry_diagnostics` reads them: (`ts_init`, high)."""
    found: dict[str, list[tuple[int, Decimal]]] = {}
    for instrument_id in dict.fromkeys(instrument_ids):
        bars = cache.bars(BarType.from_str(f"{instrument_id}-{LIVE_BARS}"))
        found[instrument_id] = sorted((int(b.ts_init), b.high.as_decimal()) for b in bars)
    return found


def _four(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))
