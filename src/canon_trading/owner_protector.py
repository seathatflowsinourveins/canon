"""Protect an owner's EXTERNAL long, without entries or an instrument claim (unit 10).

Contract: decision C and ruling 6 of the lead's 2026-11-13 "24/5 trailing stop" plan. Read at
NautilusTrader d53849b216e2, v2.0.0rc8:

* docs/concepts/execution/reconciliation.md:305-335: a reduce-only NETTING fill falls back to
  exactly one matching EXTERNAL long if the strategy has no open position of its own; ownership
  stays EXTERNAL. :364-378: an instrument claim would take the owner's orders, so none is made.
* python/nautilus_trader/trading/__init__.pyi:482-600,745-750,1021-1055: Strategy, its config,
  subscribe_quotes, submit_order and cancel_order by client ID. StrategyConfig's Python constructor
  is crates/trading/src/python/strategy.rs:112-179; NETTING is set in that native constructor.
* python/nautilus_trader/common/__init__.pyi:300-307,372,438-449,568-575: cache quotes, instruments,
  orders and open positions; :625-675: Clock and timers; :1523-1550: trailing_stop_limit.
* python/nautilus_trader/model/__init__.pyi:2591-2659,5810-5891,6365-6405,8133-8172: order state,
  remaining shares, position ownership/quantity, QuoteTick and the status/type enums.
* crates/execution/src/trailing.rs:61-100: the initial trigger is retained, then raised only.
  crates/execution/src/order_emulator/emulator.rs:660-698: submitting a breached initial trigger
  matches immediately against the cached quote. Therefore adoption holds that level locally until
  a later quote; timer ticks and duplicate venue timestamps cannot confirm the first observation.
* crates/execution/src/engine/mod.rs:3557-3582: NETTING submissions cannot name an EXTERNAL
  position ID; leave it unset for the fill fallback. The cash backtest matcher does not perform
  that fallback (matching_engine/mod.rs:3583-3667,5545-5589); the tests also exercise real core
  fills through synthetic Python adapter clients, with no broker connection.
* crates/model/src/orders/mod.rs:944-950,1067-1078: CancelRejected restores the prior state;
  Expired closes the order. python/nautilus_trader/trading/__init__.pyi:665 exposes the callback.
  crates/model/src/position.rs:707-719 resets ts_init and ts_opened on a new position cycle.
  A level belongs to that cycle and ends also when our sell fills, even if NETTING stays open.
* python/nautilus_trader/common/__init__.pyi:300-309,629-634 and
  trading/__init__.pyi:745-763: cached quotes/bars, clock and explicit-feed subscriptions.
  indicators/__init__.pyi:155-186; crates/indicators/src/volatility/atr.rs:84-86,134-155:
  AverageTrueRange.handle_bar calls update_raw(high, low, close), as protect.read_book does.
* docs/concepts/execution/reconciliation.md:25-49,279-300,364-378: startup aligns explicit
  position reports before strategies start; unclaimed external reports update EXTERNAL positions
  through the normal event pipeline. Cache.positions_open (common/__init__.pyi:568-575) discovers
  those longs at start and on the one-second timer, without claiming their order/event topics.
  price_step.up_on_step uses tick_scheme.rs:555-578 to round a new level up, never looser.
* python/nautilus_trader/model/__init__.pyi:5868: Position.avg_px_open is the holding's
  average entry. trading/__init__.pyi:552-560: modify_order accepts a trigger and limit price.
  crates/execution/src/order_emulator/emulator.rs:814-928 applies that update locally and
  reindexes/matches it; trailing.rs:61-100,129-143 only raises sell triggers and limits.
  common/__init__.pyi:1523-1550 seeds both prices when remaking a trailing_stop_limit.
* crates/execution/src/order_emulator/emulator.rs:1720-1782: activation_price leaves the
  trailing order inert until the bid reaches it; it cannot protect the holding before gain
  arming. trailing.rs:59-62 seeds the trigger from the current trigger/offset, not activation.
  The strategy therefore retains the cost floor and applies it through modify_order, with
  observation checks and arming-time cost. :814-928 rematches a modify immediately, so a
  raised floor above the current usable bid must wait while the existing stop stays in force.
* U-d: crates/execution/src/order_emulator/emulator.rs:1394-1405,1447-1465,1534-1585
  transforms the trail into a LIMIT at its retained price and publishes OrderReleased.
  python/nautilus_trader/common/__init__.pyi:1388-1407 supplies OrderFactory.limit;
  trading/__init__.pyi:656,668 supplies the release/fill callbacks. A rejected cancel restores
  state (crates/model/src/orders/mod.rs:1067-1068); a new sale waits for a terminal cancel
  (ADR 0088:61-65), never modify_order. ADR 0036 excludes extended-session broker stops;
  canon_alpaca/execution.py:257-262 accepts a marked DAY limit (ADR 0038).

Coverage follows protect.py:514-570: sum remaining shares of simple working sells; unknown cover,
contingencies and pending changes freeze; full cover needs no sell; partial foreign cover freezes;
otherwise protect uncovered whole shares. Order ownership must establish EXTERNAL coverage before
node/protect lineage is counted; another strategy's position or sell freezes the instrument by name.
Freezing retains an armed sell unless aggregate working sells could oversell the EXTERNAL long.
Cancellation requires an exact client ID this instance sent. Before release, re-sizing protection
is cancel, acknowledgement, then re-arm from the retained level; DAY expiry also permits re-arming.

The engine owns trailing and release after arming. Initial protection is a reduce-only DAY trailing
stop-limit, emulated on BID_ASK, with extended_hours=True. Once released, the sale stays a marked,
reduce-only DAY limit across cancels and expiry. U-d imports main's exit_price, chase_floor and
registered Mover rows: a tick through the bid, down to the first sale's chase floor, released after
FLOOR_WAIT_SECS resting there with the bid through it. A gain trigger never floors the sale (the
lead's R3, 2026-11-13). Each sale logs its re-prices, trigger-to-fill time and floor wait; an
acknowledgement is not a fill. Offsets use Mover's existing
explicit-distance calculation with a dollar distance from protect.row_distance, the function used by
protect.plan_distance (ADR 0142). The required row is OWN1 or OWN3 from protect.ROWS, without a default
or fraction overrides; F2 is refused by name because the holding's distance must be per symbol.
The distance and limit offset are fixed once per holding and logged with
Distance.source(). The cache supplies a quote and the last hour's complete external minute bars;
the adapter stamps those at their open (canon_alpaca/data.py:1237-1257). No history is requested:
insufficient cached bars take the registered row's named cold path. The adapter config helper enables
external reports and reduce-only SELL_TO_CLOSE
(canon_alpaca/execution.py:569,614-646,1578); it constructs no client or node.

The startup hold/inbox gate, feed hand-over and account-A paper proofs belong to the landing lane.
A found EXTERNAL long without a supplied level self-arms from bid minus the registered row distance,
rounded up onto Alpaca's step, with the first observation guarded. Planning refusals name the
unprotected holding on every attempt, in the log and the frozen record. Adoption binds only to a
holding already in the cache:
a level supplied while flat is refused and dropped. A residual after our completed sale is a new
holding, with a new row distance, level and first-observation guard. A stopped node leaves no engine
protection. P1 3 is a named limit: another strategy's sell may cause cancellation of ours; every such
cancellation attempt names that limit and the other strategy.

Observations (the lead's venue-clock definition, 2026-11-13 17:36, replacing V2/V3; tested in
tests/test_owner_protector_stamp_table.py). For each instrument the venue clock is the running
maximum of sane stamps received, including quotes whose spread the planner refuses and quotes
received with no level armed. V1: a stamp more than `STAMP_AHEAD_MOST_NS` ahead of its own receipt
is refused and named (canon_alpaca/data.py:1044-1062); the newest `REFUSED_KEPT` (64) refused stamps
are kept, oldest refusal dropped first, and a stamp still in that record cannot advance the clock.
A quote counts (spends the guard, confirms, or raises a level) only if it raises the venue clock;
an equal or older stamp never counts, whatever its receipt. With no maximum at arming, the first
sane quote establishes it and does not count, even if planning refuses that quote. Planning
requires a planner-accepted quote at the current maximum, aged from the earlier of stamp and
receipt and no older than `protect.QUOTE_MOST`. A stale sane quote still moves the clock but cannot
plan a level. Cache.quote() is the last arrival, not the newest stamp: the pin's cache/mod.rs:2370,
6207-6216,6343-6349 stores reverse insertion order, and common/__init__.pyi:307 exposes quotes().
Adoption seeds the clock from that history and tests the last arrival against the maximum before
planning. If the newest quote cannot be planned from, arming waits and every step names the
unprotected holding (V4/I2). Receipt order never decides which quote is newer.
The first observation of a breached or fresh level is guarded; a second confirms (property (b): no
election on one observation). Its other half (e5): once our sell has gone out, the breach is
confirmed, so an exit in progress finishes and the rest of the holding goes out at once with no new
guard, whether our cancel resized the holding (a growth, property (e)) or our sell filled in part
and then ended. Named limits: (1) a host clock more than the bound behind Alpaca's makes every quote
on time insane, so nothing is placed and every step says why; (2) two different quotes with one
stamp are one observation, and the second waits for the next greater stamp; (e4) after a DAY expiry
the level is held and the sell is re-placed at once, the latest quote, however old, deciding only
the limit's sign; the two observations of the next arming (U-c) cover the election that follows;
(b) there is no on_resume or on_reset, so a node stopped and resumed protects nothing until it is
started fresh, as paper:protector starts each run. A protector that cannot act on its quotes says
so at every step.

Gain lock (ADR 0083:29; handbook/philosophy.md:29; owner, 2026-11-13; the arithmetic is
canon_trading.gain_lock, shared with the Mover): a subsequent fresh quote with bid >= average
entry + round-trip cost + noise floor latches a floor at up_on_step(entry + cost). The entry's
fill paid the ask and the lock reads the bid, so the spread is not counted again: the cost is fees
per share, zero on paper until ADR 0147 (the lead's cost ruling, 2026-11-13). Noise is
SPREAD_FLOOR_M times the arming quote's spread. Both are fixed at arming, UNCALIBRATED rows
ROUND_TRIP_COST and NOISE_FLOOR. The level is max(retained trail, applied floor + limit_offset -
trailing_offset), rounded up. A floor is applied only while the bid is above the trigger it would
set, so arming or a raise never trips the sale by itself; until then the previous applied floor
and stop are kept and the wait is named. A higher average raises the latched floor; a lower or
unknown one never lowers either floor. Only our still-emulated sell is modified, and never in a
step that cancels it for size. Arming needs a positive spread, an observation newer than the
quote the level was armed on (unit 10's _after_arming) with both stamps after the arming,
event age within protect.QUOTE_MOST, and forward skew within the UNCALIBRATED 100 ms row
GAIN_QUOTE_FORWARD_SKEW. The exit beats the lock (the lead's R3): the lock moves the trigger and
sets the first limit at the applied floor; it never withholds the sale, and a chase under the
floor is a later unit's. Arming and each floor change are logged and kept in gain_lock_events,
and each fill of a sale the armed lock protected records its result against the entry and the
spread when it was released (gain_lock_sales), in memory for the landing lane's record writer.
An unknown basis or quote names the unarmed lock at most once a second per holding, while the
ordinary trail remains in force.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_FLOOR
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING
from typing import Any
from typing import Self
from uuid import uuid4

from canon_alpaca.data import STAMP_AHEAD_MOST_NS
from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import ContingencyType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.trading import Strategy

from . import gain_lock
from . import protect
from .custody import OURS
from .custody import PROTECT
from .custody import PROTECT_PREFIX
from .custody import lineage
from .ma_cross import refuse_unknown_keywords
from .mover import FLOOR_WAIT_SECS
from .mover import SECOND_NS
from .mover import chase_floor
from .mover import offsets
from .price_step import exit_price
from .price_step import up_on_step

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import OrderCancelRejected
    from nautilus_trader.model import OrderFilled


STEP_TIMER = "owner-protection"
# How many stamps V1 refused are kept per instrument; when full, the oldest is dropped first.
REFUSED_KEPT = 64
WAITING = "unprotected: waiting"

GAIN_QUOTE_FORWARD_SKEW_NS = 100_000_000
"""100 ms forward event/receipt tolerance; UNCALIBRATED row GAIN_QUOTE_FORWARD_SKEW."""
EXTERNAL = StrategyId("EXTERNAL")
SUMMABLE_TYPES = frozenset(
    {
        OrderType.MARKET,
        OrderType.LIMIT,
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP_MARKET,
        OrderType.TRAILING_STOP_LIMIT,
    }
)


class OwnerProtectorConfig(StrategyConfig):
    """An explicit registered row; optional supplied levels override self-arming for their holding.

    `adopt_holding` can supply a level before a later holding is discovered. Only SIP and BOATS are
    accepted; an omitted feed names SIP explicitly. Base claims, HEDGING and manage_stop are refused.
    """

    def __new__(cls, **kwargs: Any) -> Self:
        kwargs.setdefault("oms_type", OmsType.NETTING)
        return super().__new__(cls, **kwargs)

    def __init__(
        self,
        *,
        row: str,
        levels: dict[InstrumentId, Price] | None = None,
        feed: str = "sip",
        extended_hours: bool = True,
        **_kwargs: Any,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        self.levels = dict(levels or {})
        self.feed = feed
        self.row = row
        self.extended_hours = extended_hours
        self.validate_protection()

    def validate_protection(self) -> None:
        if self.row == "F2":
            raise ValueError(
                "OwnerProtector refuses F2: a holding's distance is per symbol; use OWN1 or OWN3"
            )
        if self.row not in protect.ROWS:
            raise ValueError(f"row must name one of {tuple(protect.ROWS)}, got {self.row!r}")
        if self.feed not in ("sip", "boats"):
            raise ValueError(
                "OwnerProtector refuses IEX (including the adapter's implicit IEX default): "
                "one IEX bid can be below the market; protection requires explicit sip or boats "
                "(24/5 plan, ruling 6)"
            )
        if self.oms_type != OmsType.NETTING:
            raise ValueError("OwnerProtector requires NETTING to reduce EXTERNAL inventory")
        if self.external_order_instrument_ids:
            raise ValueError("OwnerProtector must never claim external orders or their instrument")
        if self.manage_stop:
            raise ValueError("OwnerProtector refuses manage_stop: stop cancels only its own sell")
        if self.extended_hours is not True:
            raise ValueError(
                "OwnerProtector requires extended_hours=True for every protective sell"
            )
        for instrument_id, level in self.levels.items():
            if not isinstance(instrument_id, InstrumentId):
                raise TypeError("levels must map InstrumentId to Price")
            _check_level(level)


def owner_execution_config(**kwargs: Any) -> AlpacaExecutionClientConfig:
    """Adapter config only: external fills stay visible and released reduce-only limits may send.

    `kwargs` holds the adapter's common instrument_provider/routing fields; the safety settings
    cannot be overridden. The adapter still refuses non-paper configuration.
    """
    unknown = kwargs.keys() - AlpacaExecutionClientConfig.COMMON_FIELDS
    if unknown:
        raise TypeError(
            "owner_execution_config accepts only common instrument_provider/routing fields; "
            f"refused: {', '.join(sorted(unknown))}"
        )
    return AlpacaExecutionClientConfig(
        report_external_orders=True,
        report_external_trailing_stops=True,
        reduce_only_sells=True,
        **kwargs,
    )


def _check_level(level: Price) -> None:
    if not isinstance(level, Price) or level.as_decimal() <= 0:
        raise ValueError("protection level must be a positive Price")


def _quantity(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = value.as_decimal() if hasattr(value, "as_decimal") else Decimal(str(value))
    except ValueError, ArithmeticError:
        return None
    return result if result.is_finite() else None


def _remaining(order: Any) -> Decimal | None:
    """Native quantity minus filled quantity, checked against the native leaves quantity."""
    total = _quantity(getattr(order, "quantity", None))
    filled = _quantity(getattr(order, "filled_qty", None))
    left = _quantity(getattr(order, "leaves_qty", None))
    if total is None or filled is None or left is None or not 0 <= filled <= total:
        return None
    return left if left == total - filled else None


def _sane(tick: QuoteTick) -> bool:
    """V1: a quote stamped more than the adapter's `STAMP_AHEAD_MOST_NS` ahead of its own receipt is
    no observation and never a reference (as `canon_alpaca.data.trade_stamp` holds for prints,
    data.py:1044-1062). The local clock does this and nothing else."""
    return tick.ts_event - tick.ts_init <= STAMP_AHEAD_MOST_NS


def _usable(tick: QuoteTick, now: int) -> bool:
    """A sane, fresh arming snapshot; planning also requires the current venue maximum."""
    age = now - min(tick.ts_event, tick.ts_init)
    return _sane(tick) and age <= int(protect.QUOTE_MOST.total_seconds()) * 1_000_000_000


def _ahead(tick: QuoteTick) -> str:
    seconds = (tick.ts_event - tick.ts_init) / 1_000_000_000
    return (
        f"a quote stamped within {STAMP_AHEAD_MOST_NS // 1_000_000_000} s of its receipt: the "
        f"latest is stamped {seconds:.1f} s ahead of its receipt"
    )


def _after_arming(slot: Holding, tick: QuoteTick) -> bool:
    """Received after the level was fixed, and stamped after the quote it was fixed on (C0's venue
    clock orders observations by stamp alone)."""
    return (
        slot.armed_at is not None
        and tick.ts_init > slot.armed_at
        and (slot.armed_event is None or tick.ts_event > slot.armed_event)
    )


def _gain_quote_problem(tick: QuoteTick | None, now: int, started_at: int) -> str | None:
    """Arming needs a usable observation and receipt after protection began, within both bounds."""
    if tick is None:
        return "unknown quote (absent)"
    if (
        tick.bid_price.as_decimal() <= 0
        or tick.bid_size.as_decimal() <= 0
        or tick.ask_size.as_decimal() <= 0
    ):
        return "unknown quote (invalid prices or sizes)"
    if tick.ask_price.as_decimal() <= tick.bid_price.as_decimal():
        return "unknown quote (waiting for a positive spread)"
    if max(tick.ts_event, tick.ts_init) > now + GAIN_QUOTE_FORWARD_SKEW_NS:
        return "unknown quote (GAIN_QUOTE_FORWARD_SKEW exceeded: 100 ms)"
    if now - tick.ts_event > int(protect.QUOTE_MOST.total_seconds()) * 10**9:
        return "unknown quote (stale: exceeds protect.QUOTE_MOST)"
    if min(tick.ts_event, tick.ts_init) <= started_at:
        return "unknown quote (waiting for observation and receipt after protection began)"
    return None


@dataclass
class Sale:
    """One released sale, retained across its cancel/expiry legs until its final fill."""

    trigger_ns: int
    first_price: Decimal
    floor: Decimal | None
    priced_ns: int
    last_price: Decimal
    reprices: int = 0
    floor_reached: bool = False
    floor_since: int | None = None
    floor_wait_ns: int = 0
    floor_release_price: Decimal | None = None
    filled_ns: int | None = None
    order_ids: set[ClientOrderId] = field(default_factory=set)

    def record(self, now: int) -> dict[str, Any]:
        return {
            "trigger_ns": self.trigger_ns,
            "first_price": str(self.first_price),
            "reprices": self.reprices,
            "trigger_to_fill_ns": (
                None if self.filled_ns is None else self.filled_ns - self.trigger_ns
            ),
            "floor_reached": self.floor_reached,
            "floor_wait_ns": self.floor_wait_ns
            + (0 if self.floor_since is None else max(0, now - self.floor_since)),
            "floor_release_price": (
                None if self.floor_release_price is None else str(self.floor_release_price)
            ),
        }


@dataclass
class Holding:
    level: Price | None
    opened_at: int
    initialized_at: int
    protect_id: ClientOrderId | None = None
    # The venue stamp of the last quote counted (V3).
    last_observation: int | None = None
    distance: protect.Distance | None = None
    limit_offset: Decimal | None = None
    fresh: bool = False
    ended: bool = False
    # The host clock when the level was fixed; it never orders venue observations.
    armed_at: int | None = None
    # The usable maximum at arming, or the first sane maximum received afterwards. Diagnostic
    # only: the instrument's running venue clock, not this snapshot, decides whether a quote counts.
    armed_event: int | None = None
    # Set at our first send for this holding: its breach was confirmed, or its level placed, on new
    # observations. Re-placing coverage after that needs none: after our own cancel for a grown
    # holding (property (e), 2026-11-13, 16:31: covered whole within one step), after our sell
    # filled in part and ended (e5: the exit in progress finishes), or after a DAY expiry (e4, a
    # named limit). A sell of ours that FILLED ends the holding; its residual is a new one.
    placed: bool = False
    gain_last_observation: int | None = None
    gain_entry: Decimal | None = None
    round_trip_cost: Decimal | None = None
    noise_floor: Decimal | None = None
    gain_armed_at: int | None = None
    gain_floor: Price | None = None
    gain_applied_floor: Price | None = None
    gain_unarmed_said_at: int | None = None
    # The spread when a sale protected by the armed lock was released (measure 1).
    gain_release_spread: Decimal | None = None
    sale: Sale | None = None


class OwnerProtector(Strategy):
    """No entries. Only an unclaimed EXTERNAL long may receive this strategy's protective sell."""

    def __init__(self, config: OwnerProtectorConfig) -> None:
        config.validate_protection()
        super().__init__(config)
        self.settings = config
        self.levels = dict(config.levels)
        self.slots: dict[InstrumentId, Holding] = {}
        self.frozen: dict[InstrumentId, str] = {}
        self.sent_ids: set[ClientOrderId] = set()
        self.cancel_requested: set[ClientOrderId] = set()
        self.sells_named = 0
        self.subscribed: set[InstrumentId] = set()
        # The holding (ts_opened, ts_init) whose supplied level was refused: it is named
        # unprotected at every step, and a later holding on the symbol self-arms.
        self.refused: dict[InstrumentId, tuple[int, int]] = {}
        self.stopped_instruments: set[InstrumentId] = set()
        self.stopping = False
        # Per instrument, the stamps V1 refused, newest last (REFUSED_KEPT): a quote with one of
        # them is the same observation and never counts, however it comes again.
        self.refused_stamps: dict[InstrumentId, deque[int]] = {}
        # One quote at the running maximum per instrument, independent of levels and planning.
        self.venue_quotes: dict[InstrumentId, QuoteTick] = {}
        self.gain_lock_events: list[dict[str, Any]] = []
        self.gain_lock_sales: list[dict[str, Any]] = []

    def _note(self, tick: QuoteTick | None) -> bool:
        """Record every quote; return whether it raises an already established venue clock.

        This runs before management can return for coverage or a planner refusal. The first
        sane quote establishes a clock and cannot count; repeating a cached quote cannot count.
        """
        if tick is None:
            return False
        if not _sane(tick):
            kept = self.refused_stamps.setdefault(tick.instrument_id, deque(maxlen=REFUSED_KEPT))
            if tick.ts_event not in kept:
                kept.append(tick.ts_event)
            return False
        if tick.ts_event in self.refused_stamps.get(tick.instrument_id, ()):
            return False
        previous = self.venue_quotes.get(tick.instrument_id)
        if previous is not None and tick.ts_event <= previous.ts_event:
            return False
        self.venue_quotes[tick.instrument_id] = tick
        slot = self.slots.get(tick.instrument_id)
        if (
            slot is not None
            and slot.armed_event is None
            and slot.armed_at is not None
            and tick.ts_init > slot.armed_at
        ):
            slot.armed_event = tick.ts_event
        return previous is not None

    def _seed_clock(self, instrument_id: InstrumentId) -> None:
        """Cache.quotes is reverse arrival order at the pin, not venue-stamp order."""
        for tick in reversed(self.cache.quotes(instrument_id) or []):
            self._note(tick)

    def set_external_order_instrument_ids(self, instrument_ids: Sequence[InstrumentId]) -> None:
        if instrument_ids:
            raise ValueError("OwnerProtector must never claim external orders or their instrument")

    def _say(self, text: str) -> None:
        self.log.info(text)

    def _freeze(self, instrument_id: InstrumentId, reason: str) -> None:
        if self.frozen.get(instrument_id) != reason:
            self._say(f"{instrument_id}: frozen: {reason}; no new sell")
        self.frozen[instrument_id] = reason
        slot = self.slots.get(instrument_id)
        if slot is not None and self._oversell_possible(instrument_id):
            other_sellers = sorted(
                {
                    str(order.strategy_id)
                    for order in self.cache.orders(instrument_id=instrument_id)
                    if not order.is_closed
                    and order.side == OrderSide.SELL
                    and order.strategy_id not in (EXTERNAL, self.strategy_id)
                }
            )
            limit = (
                f"{instrument_id}: NAMED LIMIT P1 3: sell belongs to {', '.join(other_sellers)}; "
                "aggregate coverage cannot separate that strategy's shares; canceling our sell"
                if other_sellers
                else None
            )
            self._cancel_own(slot, reason=limit)

    def _oversell_possible(self, instrument_id: InstrumentId) -> bool:
        """A freeze stops new sends; pull an armed sell only if its shares cannot be kept safely."""
        external = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
        if len(external) != 1 or external[0].signed_qty <= 0:
            return True
        total = Decimal(0)
        for order in self.cache.orders(instrument_id=instrument_id):
            if order.is_closed or order.side != OrderSide.SELL:
                continue
            left = _remaining(order)
            if (
                left is None
                or getattr(order, "is_quote_quantity", False)
                or getattr(order, "contingency_type", None)
                not in (None, ContingencyType.NO_CONTINGENCY)
                or getattr(order, "linked_order_ids", None)
                or getattr(order, "order_class", None) not in (None, "", "simple")
                or order.order_type not in SUMMABLE_TYPES
            ):
                return True
            total += left
        return total > external[0].signed_qty

    def _external_long(self, instrument_id: InstrumentId) -> Any | None:
        positions = self.cache.positions_open(instrument_id=instrument_id)
        external = [p for p in positions if p.strategy_id == EXTERNAL]
        other = sorted({str(p.strategy_id) for p in positions if p.strategy_id != EXTERNAL})
        if other:
            self._freeze(
                instrument_id,
                f"open position belongs to {', '.join(other)}; cannot select EXTERNAL",
            )
            return None
        if len(external) != 1 or external[0].signed_qty <= 0:
            self._freeze(instrument_id, "requires exactly one open EXTERNAL long")
            return None
        return external[0]

    def _uncovered(
        self, instrument_id: InstrumentId, held: Decimal, own_id: ClientOrderId | None
    ) -> Decimal | None:
        ours, foreign = Decimal(0), Decimal(0)
        for order in self.cache.orders(instrument_id=instrument_id):
            if order.is_closed or order.side != OrderSide.SELL or order.client_order_id == own_id:
                continue
            if order.strategy_id not in (EXTERNAL, self.strategy_id):
                self._freeze(
                    instrument_id,
                    f"sell belongs to {order.strategy_id}; EXTERNAL coverage is not established",
                )
                return None
            left = _remaining(order)
            reason = ""
            if left is None:
                reason = "sell has unknown remaining share quantity"
            elif getattr(order, "is_quote_quantity", False):
                reason = "sell names quote quantity, not shares"
            elif (
                getattr(order, "contingency_type", None)
                not in (None, ContingencyType.NO_CONTINGENCY)
                or getattr(order, "linked_order_ids", None)
                or getattr(order, "order_class", None) not in (None, "", "simple")
                or order.order_type not in SUMMABLE_TYPES
            ):
                reason = "sell kind/contingent legs cannot be summed"
            elif order.status in (OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_UPDATE):
                reason = "sell is pending cancel/update; coverage can change"
            if reason:
                self._freeze(instrument_id, reason)
                return None
            assert left is not None
            if order.client_order_id in self.sent_ids or lineage(str(order.client_order_id)) in (
                OURS,
                PROTECT,
            ):
                ours += left
            else:
                foreign += left
        if ours + foreign >= held:
            self._say(f"{instrument_id}: {held} held, fully covered by working sells")
            return Decimal(0)
        if foreign:
            self._freeze(instrument_id, "long is covered in part by a foreign sell; left untouched")
            return None
        rest = held - ours
        whole = rest.to_integral_value(rounding=ROUND_FLOOR)
        if whole != rest:
            self._say(
                f"{instrument_id}: uncovered fraction {rest - whole} left; {whole} whole shares"
            )
        return whole

    def adopt_holding(self, instrument_id: InstrumentId, level: Price) -> None:
        """Arm from `level` for an EXTERNAL long, or retain a breached level for a later quote.

        Repeating adoption never duplicates a sale or resets its observation guard. A later fresh
        quote confirms a startup breach; absent/duplicate/out-of-order observations do not.
        """
        if (
            self.stopping
            or instrument_id in self.stopped_instruments
            or instrument_id in self.slots
        ):
            return
        _check_level(level)
        position = self._external_long(instrument_id)
        if position is None:
            self.levels.pop(instrument_id, None)
            self._say(
                f"{instrument_id}: level refused: no selectable EXTERNAL holding; dropped {level}"
            )
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None or instrument.make_price(level.as_decimal()) != level:
            # Dropped at the refusal, so it never binds a later holding (P1-1, 15:55).
            self.levels.pop(instrument_id, None)
            self.refused[instrument_id] = (position.ts_opened, position.ts_init)
            self._name_refused(
                instrument_id,
                f"instrument missing or supplied level {level} off its precision; dropped",
            )
            return
        self.levels[instrument_id] = level
        self.slots[instrument_id] = self._new_holding(instrument_id, position, level)
        self._subscribe_holding(instrument_id)
        self._manage(instrument_id)

    def _protect_id(self) -> ClientOrderId:
        """A client order ID of the protect lineage, by custody's one rule
        (`custody.PROTECT_CLIENT_ORDER_ID`): a protective sale of the owner's shares is never one
        of our nodes' orders (the lead's ruling, 2026-11-13, 16:04:49; ADR 0148)."""
        self.sells_named += 1
        stamp = datetime.fromtimestamp(self.clock.timestamp_ns() // 1_000_000_000, UTC)
        return ClientOrderId(
            f"{PROTECT_PREFIX}{stamp:%Y%m%dT%H%M%SZ}-{self.sells_named}-{uuid4().hex[:8]}"
        )

    def _subscribe_holding(self, instrument_id: InstrumentId) -> None:
        if instrument_id not in self.subscribed:
            self.subscribe_quotes(instrument_id, params={"feed": self.settings.feed})
            self.subscribe_bars(
                BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"),
                params={"feed": self.settings.feed},
            )
            self.subscribed.add(instrument_id)

    def _new_holding(
        self, instrument_id: InstrumentId, position: Any, level: Price | None = None
    ) -> Holding:
        """A supplied level is fixed now, on the venue-maximum quote if usable; a fresh one at its
        plan. The venue clock is seeded from the cache's history first (C0's definition)."""
        self._seed_clock(instrument_id)
        if level is None:
            return Holding(None, position.ts_opened, position.ts_init, fresh=True)
        newest = self.venue_quotes.get(instrument_id)
        now = self.clock.timestamp_ns()
        return Holding(
            level,
            position.ts_opened,
            position.ts_init,
            armed_at=now,
            armed_event=None if newest is None or not _usable(newest, now) else newest.ts_event,
        )

    def _discover_holdings(self) -> None:
        """Find reconciled inventory and later EXTERNAL fills without an instrument claim."""
        instruments = {
            position.instrument_id
            for position in self.cache.positions_open(strategy_id=EXTERNAL)
            if position.signed_qty > 0
        }
        for instrument_id in sorted(instruments, key=str):
            if instrument_id in self.slots or instrument_id in self.stopped_instruments:
                continue
            position = self._external_long(instrument_id)
            if position is None:
                continue
            if self.refused.get(instrument_id) == (position.ts_opened, position.ts_init):
                # The holding whose supplied level was refused is not re-armed from the row.
                self._name_refused(instrument_id, "its supplied level was refused")
                continue
            self.refused.pop(instrument_id, None)
            self.slots[instrument_id] = self._new_holding(instrument_id, position)
            self._subscribe_holding(instrument_id)
            self._manage(instrument_id)

    def on_start(self) -> None:
        self.settings.validate_protection()
        for instrument_id, level in list(self.levels.items()):
            self.adopt_holding(instrument_id, level)
        self._discover_holdings()
        self.clock.set_timer(STEP_TIMER, timedelta(seconds=1), callback=self._on_step)

    def on_quote(self, quote: QuoteTick) -> None:
        if self.stopping or quote.instrument_id in self.stopped_instruments:
            return
        advanced = self._note(quote)
        if quote.instrument_id in self.slots:
            self._manage(quote.instrument_id, quote, advanced=advanced)

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        if event.strategy_id != self.strategy_id or event.client_order_id not in self.sent_ids:
            return
        self.cancel_requested.discard(event.client_order_id)
        self._say(
            f"{event.instrument_id}: our cancel rejected: {event.reason}; "
            "re-evaluate on the next quote or step"
        )

    def on_order_released(self, event: OrderReleased) -> None:
        """The released trail starts the sale's chase (U-d); a sale the applied gain lock protected
        also keeps the spread at its release (measure 1)."""
        slot = self.slots.get(event.instrument_id)
        if slot is None or event.client_order_id != slot.protect_id:
            return
        if slot.gain_applied_floor is not None:
            tick = self.cache.quote(event.instrument_id)
            slot.gain_release_spread = (
                None if tick is None else tick.ask_price.as_decimal() - tick.bid_price.as_decimal()
            )
        self._observe_sale(event.instrument_id, slot, self.cache.order(event.client_order_id))

    def on_order_filled(self, event: OrderFilled) -> None:
        """Measure 1 for a sale the armed lock protected, then the sale's own record (U-d)."""
        slot = self.slots.get(event.instrument_id)
        if slot is None or not (
            event.client_order_id == slot.protect_id
            or (slot.sale is not None and event.client_order_id in slot.sale.order_ids)
        ):
            return
        if slot.gain_armed_at is not None and slot.gain_entry is not None:
            self._gain_lock_sale(event, slot)
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        self._observe_sale(event.instrument_id, slot, order)

    def _gain_lock_sale(self, event: OrderFilled, slot: Holding) -> None:
        """Measure 1: each fill of a sale the armed lock protected, against the entry."""
        assert slot.gain_entry is not None
        price = event.last_px.as_decimal()
        record = {
            "instrument_id": str(event.instrument_id),
            "at": event.ts_event,
            "entry": str(slot.gain_entry),
            "fill_price": str(price),
            "quantity": str(event.last_qty.as_decimal()),
            "result_per_share": str(price - slot.gain_entry),
            "floor": str(slot.gain_floor),
            "applied_floor": str(slot.gain_applied_floor),
            "spread_at_release": (
                None if slot.gain_release_spread is None else str(slot.gain_release_spread)
            ),
        }
        self.gain_lock_sales.append(record)
        self._say(
            f"{event.instrument_id}: gain lock sale filled at {price} against entry "
            f"{slot.gain_entry}; spread at release {record['spread_at_release']}"
        )

    def _on_step(self, event: TimeEvent) -> None:
        if self.stopping:
            return
        for instrument_id in list(self.slots):
            self._manage(instrument_id)
            waiting = self.frozen.get(instrument_id, "")
            if waiting.startswith(WAITING):
                # A protector that cannot act on its quotes says so at every step (the lead's
                # ruling, 2026-11-13, 16:3x).
                self._say(f"{instrument_id}: {waiting}; no sell yet")
        self._discover_holdings()

    def _wait(self, instrument_id: InstrumentId, reason: str) -> None:
        """A holding with no sell of ours that waits on its quotes: a named record, said each step."""
        self.frozen[instrument_id] = f"{WAITING} for {reason}"

    def _record_unprotected(self, instrument_id: InstrumentId, reason: str) -> None:
        """A planning failure is a current named record and a log on every attempt."""
        reason = f"unprotected: row {self.settings.row} distance refused: {reason}"
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _name_refused(self, instrument_id: InstrumentId, reason: str) -> None:
        """A holding whose supplied level was refused is a named record and a log every time."""
        reason = f"unprotected: supplied level refused: {reason}"
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _gain_lock(
        self,
        instrument_id: InstrumentId,
        slot: Holding,
        position: Any,
        quote: QuoteTick | None,
        *,
        advanced: bool = False,
    ) -> None:
        """Latch on a new received quote, never on a cached quote/timer or an old observation.

        Under C0's venue clock a quote counts only if it raised the clock (`advanced`).

        The arming quote fixes cost and noise; a changed average can only raise the cost floor.
        No unknown input removes either the retained level or the already-armed latch.
        """
        entry = _quantity(getattr(position, "avg_px_open", None))
        if entry is not None and entry <= 0:
            entry = None
        now = self.clock.timestamp_ns()
        tick = quote if quote is not None else self.cache.quote(instrument_id)
        # Protection begins when the protective level is armed (unit 10's armed_at); before that
        # no quote counts toward the gain lock.
        quote_problem = (
            "unknown quote (waiting for the protective level to be armed)"
            if slot.armed_at is None
            else _gain_quote_problem(tick, now, slot.armed_at)
        )
        new_quote = (
            quote is not None
            and advanced
            and quote_problem is None
            and _after_arming(slot, quote)
            and (slot.gain_last_observation is None or quote.ts_event > slot.gain_last_observation)
        )
        if new_quote:
            assert quote is not None
            slot.gain_last_observation = quote.ts_event
        was_armed = slot.gain_armed_at is not None
        previous_floor = slot.gain_floor
        previous_applied_floor = slot.gain_applied_floor
        if not was_armed:
            reasons = []
            if entry is None:
                reasons.append("unknown cost basis")
            if quote_problem is not None:
                reasons.append(quote_problem)
            if slot.distance is None or slot.limit_offset is None:
                # The spacing a floor keeps is the plan's; armed at a spacing of 0, the later plan
                # would lift the trigger to the floor plus spacing, at or over the bid, and sell.
                reasons.append("trail not planned yet")
            if reasons:
                said = slot.gain_unarmed_said_at
                if said is None or now - said >= 1_000_000_000:
                    slot.gain_unarmed_said_at = now
                    self._say(
                        f"{instrument_id}: gain lock unarmed: {'; '.join(reasons)}; plain trail"
                    )
                return
            if not new_quote:
                return
            assert tick is not None and entry is not None
            measures = gain_lock.arming_measures(
                tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
            )
            if measures is None:
                return  # _gain_quote_problem already named a spread that is not positive.
            cost, noise = measures
            bid = tick.bid_price.as_decimal()
            if not gain_lock.arms(bid, entry, cost, noise):
                return
            # Arm only where the floor can apply: a trigger at or over the bid would sell on the
            # arming quote (emulator.rs:814-928), so the lock waits, named, until the bid clears it.
            instrument = self.cache.instrument(instrument_id)
            if instrument is not None:
                trigger = self._gain_trigger(
                    instrument, slot, instrument.make_price(gain_lock.cost_floor(entry, cost))
                )
                if trigger is not None and trigger.as_decimal() >= bid:
                    said = slot.gain_unarmed_said_at
                    if said is None or now - said >= 1_000_000_000:
                        slot.gain_unarmed_said_at = now
                        self._say(
                            f"{instrument_id}: gain lock waits: bid {tick.bid_price} not above the "
                            f"trigger {trigger} its floor sets; plain trail"
                        )
                    return
        else:
            if entry is None:
                entry = slot.gain_entry
            cost, noise = slot.round_trip_cost, slot.noise_floor
        assert entry is not None and cost is not None and noise is not None
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._say(
                f"{instrument_id}: gain lock floor unavailable: instrument absent; kept level"
            )
            return
        required = gain_lock.cost_floor(entry, cost)
        floor = instrument.make_price(required)
        if floor.as_decimal() < required:
            self._say(
                f"{instrument_id}: gain lock floor unavailable: instrument precision; kept level"
            )
            return
        slot.gain_entry, slot.round_trip_cost, slot.noise_floor = entry, cost, noise
        if not was_armed:
            slot.gain_armed_at = now
        if slot.gain_floor is None or floor.as_decimal() > slot.gain_floor.as_decimal():
            slot.gain_floor = floor
        target = self._gain_trigger(instrument, slot, slot.gain_floor)
        if (
            quote_problem is None
            and tick is not None
            and target is not None
            and target.as_decimal() < tick.bid_price.as_decimal()
        ):
            slot.gain_applied_floor = slot.gain_floor
            self._gain_level(instrument_id, slot)
        elif slot.gain_applied_floor != slot.gain_floor:
            reason = quote_problem
            if reason is None:
                assert tick is not None
                reason = (
                    f"bid {tick.bid_price} not above the trigger {target} that floor "
                    f"{slot.gain_floor} sets"
                    if target is not None
                    else "trigger unavailable at instrument precision"
                )
            self._say(
                f"{instrument_id}: gain lock floor deferred: {reason}; kept applied floor and trail"
            )
        assert slot.level is not None
        if (
            not was_armed
            or slot.gain_floor != previous_floor
            or slot.gain_applied_floor != previous_applied_floor
        ):
            event = "raised" if was_armed else "armed"
            record = {
                "instrument_id": str(instrument_id),
                "event": event,
                "at": now,
                "level_armed_at": slot.armed_at,
                "entry": str(entry),
                "round_trip_cost": str(cost),
                "noise_floor": str(noise),
                "armed_at": slot.gain_armed_at,
                "floor": str(slot.gain_floor),
                "applied_floor": str(slot.gain_applied_floor),
                "level": str(slot.level),
                "fees": "fees per share 0 on paper; the fee record (ADR 0147) is not on main",
            }
            self.gain_lock_events.append(record)
            self._say(
                f"{instrument_id}: gain lock {event}: entry={entry}, round_trip_cost={cost}, "
                f"noise_floor={noise}, armed_at={slot.gain_armed_at}, floor={slot.gain_floor}, "
                f"applied_floor={slot.gain_applied_floor}, level={slot.level}; "
                "fees per share 0 on paper (ADR 0147 not on main)"
            )

    def _gain_trigger(self, instrument: Any, slot: Holding, floor: Price) -> Price | None:
        """The trigger a floor sets: the floor plus the configured trigger-to-limit spacing, up."""
        spacing = (
            slot.limit_offset - slot.distance.distance
            if slot.limit_offset is not None and slot.distance is not None
            else Decimal(0)
        )
        required = up_on_step(floor.as_decimal() + spacing)
        trigger = instrument.make_price(required)
        return None if trigger.as_decimal() < required else trigger

    def _gain_level(self, instrument_id: InstrumentId, slot: Holding) -> None:
        """Keep the configured trigger-to-limit spacing when the applied floor binds."""
        if slot.gain_applied_floor is None:
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return
        level = self._gain_trigger(instrument, slot, slot.gain_applied_floor)
        if level is None:
            self._say(f"{instrument_id}: gain lock level unavailable: instrument precision")
            return
        if slot.level is None or level.as_decimal() > slot.level.as_decimal():
            slot.level = level

    def _raise_gain_order(self, slot: Holding, order: Any) -> None:
        """Only a locally emulated order can have its trigger raised; never touch a venue order."""
        if (
            slot.gain_applied_floor is None
            or not order.is_emulated
            or order.client_order_id not in self.sent_ids
            or order.strategy_id != self.strategy_id
            or order.side != OrderSide.SELL
            or order.order_type != OrderType.TRAILING_STOP_LIMIT
        ):
            return
        assert slot.level is not None
        trigger = order.trigger_price
        if trigger is None or slot.level.as_decimal() > trigger.as_decimal():
            trigger = slot.level
        price = order.price
        if price is None or slot.gain_applied_floor.as_decimal() > price.as_decimal():
            price = slot.gain_applied_floor
        if trigger != order.trigger_price or price != order.price:
            self.modify_order(order.client_order_id, trigger_price=trigger, price=price)

    def _plan_holding(self, instrument_id: InstrumentId, slot: Holding, tick: QuoteTick) -> bool:
        """One cache snapshot through paper:protect's planner; no history request or tightening.

        Cache.bars is newest first, so feed ATR in timestamp order. The adapter's external minute
        bars are stamped at their open (data.py:1237-1257); ts_init is receipt, not completeness.
        """
        now = self.clock.timestamp_ns()
        # Aged from the earlier of its stamp and its receipt: a stamp ahead of the clock never
        # makes an old quote young (the (e) review's P2-2).
        age = timedelta(seconds=(now - min(tick.ts_event, tick.ts_init)) / 1_000_000_000)
        try:
            if age > protect.QUOTE_MOST:
                # The bound paper:protect plans on (protect.py:360, :693); never armed from it.
                raise ValueError(
                    f"waiting for a fresh quote: the cached one is {age.total_seconds():.0f} s "
                    f"old, over the {protect.QUOTE_MOST.total_seconds():.0f} s a level is "
                    "planned on"
                )
            newest = self.venue_quotes.get(instrument_id)
            if (
                newest is None
                or tick.ts_event != newest.ts_event
                or tick.ts_event in self.refused_stamps.get(instrument_id, ())
            ):
                raise ValueError("waiting for a planable quote at the current venue maximum")
            atr = AverageTrueRange(protect.PLANNER.atr_n)
            bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
            earliest = now - int(protect.ATR_LOOKBACK.total_seconds()) * 1_000_000_000
            for bar in sorted(self.cache.bars(bar_type) or [], key=lambda b: b.ts_event):
                if earliest <= bar.ts_event and bar.ts_event + 60_000_000_000 <= now:
                    atr.handle_bar(bar)
            book = protect.Book(
                tick.bid_price.as_decimal(), tick.ask_price.as_decimal(), atr, age, monotonic()
            )
            planner = protect.mover.Mover(protect.planner_config(self.settings.row))
            distance, plan = protect.row_distance(
                book, instrument_id.symbol.value, self.settings.row, planner
            )
            _, limit = offsets(
                plan.price, plan.plan.trail, plan.plan.exit_through, distance=distance
            )
            if book.bid - limit <= 0:
                raise ValueError("no positive protective limit at this bid and row distance")
            if slot.level is None:
                instrument = self.cache.instrument(instrument_id)
                if instrument is None:
                    raise ValueError("instrument is absent from cache")
                planned_level = up_on_step(book.bid - distance)
                level = instrument.make_price(planned_level)
                if planned_level <= 0 or level.as_decimal() != planned_level:
                    raise ValueError("no positive level on Alpaca's step at instrument precision")
                if tick.ts_event in self.refused_stamps.get(instrument_id, ()):
                    raise ValueError("waiting for a newer quote: V1 refused this stamp")
                slot.level, slot.armed_at, slot.armed_event = level, now, tick.ts_event
            slot.distance, slot.limit_offset = plan, limit
            self._say(f"{instrument_id}: armed distance {distance}; {plan.source()}")
        except ValueError as exc:
            self._record_unprotected(instrument_id, str(exc))
            return False
        return True

    def _manage(
        self, instrument_id: InstrumentId, quote: QuoteTick | None = None, *, advanced: bool = False
    ) -> None:
        tick = quote if quote is not None else self.cache.quote(instrument_id)
        if quote is None:
            advanced = self._note(tick)
        slot = self.slots[instrument_id]
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        self._observe_sale(instrument_id, slot, order)
        if slot.protect_id is not None and order is None:
            self._freeze(instrument_id, "our sent sell is absent from cache; awaiting its state")
            return
        if order is not None and order.status == OrderStatus.FILLED:
            # Completion of our sale ends the level even if an owner buy kept NETTING open.
            self._reset_holding(instrument_id)
            position = self._external_long(instrument_id)
            if position is not None:
                self._say(f"{instrument_id}: our sell filled; remaining shares are a new holding")
                self.slots[instrument_id] = self._new_holding(instrument_id, position)
                self._manage(instrument_id, quote)
            return
        external = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
        if slot.ended or not external or (len(external) == 1 and external[0].signed_qty <= 0):
            self._end_holding(instrument_id, slot, order)
            return
        position = self._external_long(instrument_id)
        if position is None:
            return
        if (position.ts_opened, position.ts_init) != (slot.opened_at, slot.initialized_at):
            # A NETTING position can reopen under the same ID between two strategy callbacks.
            self._end_holding(instrument_id, slot, order)
            return
        if order is not None:
            trigger = getattr(order, "trigger_price", None)
            if trigger is None:
                # A released limit has no trigger field but keeps the trailing order's events
                # (nt@d53849b216e2 order_emulator/emulator.rs:1394-1405).
                for event in order.events():
                    reached = getattr(event, "trigger_price", None)
                    if reached is not None and (trigger is None or reached > trigger):
                        trigger = reached
            if trigger is not None and (slot.level is None or trigger > slot.level):
                slot.level = trigger
            if order.is_closed:
                if order.status in (OrderStatus.DENIED, OrderStatus.REJECTED):
                    if slot.sale is None:
                        self._freeze(
                            instrument_id, f"our protective sell ended {order.status}; inspect it"
                        )
                        return
                    # As main's chase does (mover.py:4002-4007): a refused sale is sent again
                    # once per chase interval, never left without one.
                    waited = self.clock.timestamp_ns() - slot.sale.priced_ns
                    if waited < protect.PLANNER.chase_secs * SECOND_NS:
                        self._sale_stalled(
                            instrument_id,
                            f"our sale ended {order.status}; sent again after the chase interval",
                        )
                        return
                    self._say(f"{instrument_id}: our sale ended {order.status}; sending it again")
                self.cancel_requested.discard(order.client_order_id)
                slot.protect_id, order = None, None
        if slot.sale is None:
            # The lock moves the trigger of a trail not yet released; the exit beats the lock (R3).
            self._gain_lock(instrument_id, slot, position, quote, advanced=advanced)
        if order is not None and order.client_order_id in self.cancel_requested:
            if slot.sale is not None:
                self._sale_stalled(instrument_id, "awaiting our sale's terminal cancel")
            return  # No replacement until the original has a terminal acknowledgement.
        uncovered = self._uncovered(instrument_id, position.signed_qty, slot.protect_id)
        if uncovered is None:
            if slot.sale is not None:
                self._say(f"{instrument_id}: sale stalled: {self.frozen[instrument_id]}")
            return
        self.frozen.pop(instrument_id, None)
        if order is not None:
            if _remaining(order) != uncovered:
                self._cancel_own(slot)  # Never modified in the step that cancels it for size.
            elif slot.sale is not None:
                self._chase_sale(instrument_id, slot, order, quote)
            else:
                self._raise_gain_order(slot, order)
            return
        if uncovered <= 0:
            return
        if slot.sale is not None:
            self._send_sale(instrument_id, slot, uncovered, quote)
            return
        if tick is None:
            self._record_unprotected(instrument_id, "no quote to plan protection")
            return
        bid, ask = tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
        if (
            bid <= 0
            or ask < bid
            or tick.bid_size.as_decimal() <= 0
            or tick.ask_size.as_decimal() <= 0
        ):
            self._record_unprotected(instrument_id, "no usable bid/ask prices and sizes")
            return
        if (
            not slot.placed
            and not _sane(tick)
            and self.clock.timestamp_ns() - tick.ts_init
            <= int(protect.QUOTE_MOST.total_seconds()) * 1_000_000_000
        ):
            # V1: no observation, no reference, and no quote a plan reads; a stale one is named
            # stale by the planner instead.
            self._note(tick)
            self._wait(instrument_id, _ahead(tick))
            return
        unarmed = slot.level is None
        if slot.distance is None and not self._plan_holding(instrument_id, slot, tick):
            return
        if unarmed:
            advanced = False  # Planning this level is not an observation after its arming.
        self._gain_level(instrument_id, slot)
        assert (
            slot.level is not None and slot.distance is not None and slot.limit_offset is not None
        )
        if not slot.placed:
            if not _sane(tick):
                self._note(tick)
                self._wait(instrument_id, _ahead(tick))
                return
            if not advanced:
                self._wait(
                    instrument_id,
                    "a quote stamped after the venue maximum"
                    if slot.last_observation is None
                    else "a second observation newer than the one guarded",
                )
                return
            if slot.last_observation is None and (slot.fresh or bid <= slot.level.as_decimal()):
                slot.last_observation = tick.ts_event
                self._say(
                    f"{instrument_id}: holding level {slot.level}; one observation cannot elect"
                )
                self._wait(instrument_id, "a second observation to confirm")
                return
        if advanced:
            slot.last_observation = tick.ts_event
        trailing, limit = slot.distance.distance, slot.limit_offset
        if bid - limit <= 0:
            self._freeze(instrument_id, "no positive protective limit at this bid and trail")
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._freeze(instrument_id, "instrument is absent from cache")
            return
        quantity = instrument.make_qty(uncovered)
        if quantity.as_decimal() != uncovered:
            self._freeze(instrument_id, "uncovered shares cannot be represented exactly")
            return
        order = self.order_factory.trailing_stop_limit(
            instrument_id,
            OrderSide.SELL,
            quantity,
            slot.gain_applied_floor,
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_price=slot.level,
            trigger_type=TriggerType.BID_ASK,
            time_in_force=TimeInForce.DAY,
            reduce_only=True,
            emulation_trigger=TriggerType.BID_ASK,
            # The pin takes the ID (common/src/factories/order.rs:950-973), and the emulator's
            # released LIMIT keeps it (order_emulator/emulator.rs:5094).
            client_order_id=self._protect_id(),
        )
        if self.cache.order(order.client_order_id) is not None:
            self._freeze(instrument_id, "generated client order ID already exists; left untouched")
            return
        slot.protect_id, slot.placed = order.client_order_id, True
        self.sent_ids.add(order.client_order_id)
        self.submit_order(order, params={"extended_hours": True})

    def _observe_sale(self, instrument_id: InstrumentId, slot: Holding, order: Any) -> None:
        """Only an owned, transformed LIMIT starts a chase; its release event supplies the clock.

        Inspecting the cache also captures a release/fill between steps, or a subclass which
        records callbacks without forwarding them. A pre-release trail never enters this path.
        """
        owned_limit = (
            order is not None
            and order.client_order_id in self.sent_ids
            and order.strategy_id == self.strategy_id
            and order.order_type == OrderType.LIMIT
            and getattr(order, "price", None) is not None
        )
        if slot.sale is None:
            if not owned_limit:
                return
            released = [event for event in order.events() if isinstance(event, OrderReleased)]
            at = released[0].ts_event if released else self.clock.timestamp_ns()
            first = order.price.as_decimal()
            slot.sale = Sale(at, first, chase_floor(first, protect.PLANNER.chase_cap), at, first)
            self._log_sale(instrument_id, slot.sale)
        sale = slot.sale
        if owned_limit:
            sale.order_ids.add(order.client_order_id)
        # A cancel acknowledgement can cross a fill. A completed earlier leg is still this
        # sale's fill, even while its replacement is being withdrawn from the now-flat long.
        filled = [
            leg.ts_closed
            for cid in sale.order_ids
            if (leg := self.cache.order(cid)) is not None
            and leg.status == OrderStatus.FILLED
            and leg.ts_closed is not None
        ]
        if filled and sale.filled_ns is None:
            sale.filled_ns = max(filled)
            if sale.floor_since is not None:
                sale.floor_wait_ns += max(0, sale.filled_ns - sale.floor_since)
                sale.floor_since = None
            self._log_sale(instrument_id, sale)

    def _log_sale(self, instrument_id: InstrumentId, sale: Sale) -> None:
        record = sale.record(self.clock.timestamp_ns())
        self._say(f"{instrument_id}: sale record {json.dumps(record, sort_keys=True)}")

    def _sale_stalled(self, instrument_id: InstrumentId, reason: str) -> None:
        reason = f"sale stalled: {reason}"
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _sale_bid(self, instrument_id: InstrumentId, quote: QuoteTick | None) -> Decimal | None:
        tick = quote if quote is not None else self.cache.quote(instrument_id)
        if tick is None:
            self._sale_stalled(instrument_id, "no quote to price the sale")
            return None
        bid, ask = tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
        if (
            bid <= 0
            or ask < bid
            or tick.bid_size.as_decimal() <= 0
            or tick.ask_size.as_decimal() <= 0
        ):
            self._sale_stalled(instrument_id, "no usable bid/ask quote to price the sale")
            return None
        return bid

    def _chase_sale(
        self, instrument_id: InstrumentId, slot: Holding, order: Any, quote: QuoteTick | None
    ) -> None:
        sale = slot.sale
        assert sale is not None
        if not order.is_open or order.is_pending_cancel or order.is_pending_update:
            self._sale_stalled(
                instrument_id, f"awaiting our sale's acknowledgement ({order.status})"
            )
            return
        bid = self._sale_bid(instrument_id, quote)
        if bid is None:
            return
        now = self.clock.timestamp_ns()
        through = exit_price(bid, protect.PLANNER.exit_ticks, None)
        # The wait runs while the exit price is through the floor, as main's (mover.py:4695).
        if (
            sale.floor is not None
            and order.price.as_decimal() == sale.floor
            and through < sale.floor
        ):
            sale.floor_reached = True
            if sale.floor_since is None:
                sale.floor_since = now
            if now - sale.floor_since >= FLOOR_WAIT_SECS * SECOND_NS:
                sale.floor_wait_ns += now - sale.floor_since
                sale.floor_since, sale.floor, sale.floor_release_price = None, None, through
                self._log_sale(instrument_id, sale)
        elif sale.floor_since is not None:
            sale.floor_wait_ns += now - sale.floor_since
            sale.floor_since = None
            self._log_sale(instrument_id, sale)
        if now - sale.priced_ns < protect.PLANNER.chase_secs * SECOND_NS:
            return
        price = exit_price(bid, protect.PLANNER.exit_ticks, sale.floor)
        if price >= order.price.as_decimal():  # only downward, as main's (mover.py:4707)
            return
        sale.priced_ns = now
        self._cancel_own(slot)  # A subsequent step re-reads quantity after a terminal cancel.

    def _send_sale(
        self,
        instrument_id: InstrumentId,
        slot: Holding,
        uncovered: Decimal,
        quote: QuoteTick | None,
    ) -> None:
        sale = slot.sale
        assert sale is not None
        bid = self._sale_bid(instrument_id, quote)
        if bid is None:
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._sale_stalled(instrument_id, "instrument is absent from cache")
            return
        quantity = instrument.make_qty(uncovered)
        if quantity.as_decimal() != uncovered:
            self._sale_stalled(instrument_id, "uncovered shares cannot be represented exactly")
            return
        price = exit_price(bid, protect.PLANNER.exit_ticks, sale.floor)
        limit = instrument.make_price(price)
        if limit.as_decimal() != price:
            self._sale_stalled(instrument_id, "sale price cannot be represented exactly")
            return
        order = self.order_factory.limit(
            instrument_id,
            OrderSide.SELL,
            quantity,
            limit,
            time_in_force=TimeInForce.DAY,
            reduce_only=True,
            client_order_id=self._protect_id(),
        )
        if self.cache.order(order.client_order_id) is not None:
            self._sale_stalled(instrument_id, "generated client order ID already exists")
            return
        now = self.clock.timestamp_ns()
        # A running floor wait carries across legs, as main's floor_since does: an expiry or a
        # resize at the floor does not restart it; _chase_sale closes it off the floor.
        sale.reprices += int(price != sale.last_price)
        sale.last_price, sale.priced_ns = price, now
        sale.floor_reached |= sale.floor is not None and price == sale.floor
        slot.protect_id = order.client_order_id
        self.sent_ids.add(order.client_order_id)
        sale.order_ids.add(order.client_order_id)
        self._log_sale(instrument_id, sale)
        self.submit_order(order, params={"extended_hours": True})

    def _reset_holding(self, instrument_id: InstrumentId) -> None:
        slot = self.slots.pop(instrument_id)
        if slot.sale is not None:  # the sale's final record, however its long ended
            if slot.sale.floor_since is not None:
                slot.sale.floor_wait_ns += self.clock.timestamp_ns() - slot.sale.floor_since
                slot.sale.floor_since = None
            self._log_sale(instrument_id, slot.sale)
        if slot.protect_id is not None:
            self.cancel_requested.discard(slot.protect_id)
        self.levels.pop(instrument_id, None)
        self.frozen.pop(instrument_id, None)
        self._say(f"{instrument_id}: previous holding ended; its level and distance were dropped")

    def _end_holding(self, instrument_id: InstrumentId, slot: Holding, order: Any) -> None:
        slot.ended = True
        self.levels.pop(instrument_id, None)
        if order is not None and not order.is_closed:
            self._cancel_own(slot)
            self._freeze(instrument_id, "holding ended; awaiting our old sell's end")
        else:
            self._reset_holding(instrument_id)

    def _cancel_own(self, slot: Holding, *, reason: str | None = None) -> None:
        cid = slot.protect_id
        if cid is None or cid not in self.sent_ids or cid in self.cancel_requested:
            return
        order = self.cache.order(cid)
        if order is None or order.is_closed or order.strategy_id != self.strategy_id:
            return
        if order.side != OrderSide.SELL:
            return
        if reason is not None:
            self._say(reason)
        self.cancel_requested.add(cid)
        self.cancel_order(cid)

    def stop_instrument(self, instrument_id: InstrumentId) -> None:
        """Stop this instrument by canceling only the sell this instance sent, with no liquidation."""
        self.stopped_instruments.add(instrument_id)
        self.refused_stamps.pop(instrument_id, None)
        self.venue_quotes.pop(instrument_id, None)
        slot = self.slots.pop(instrument_id, None)
        self.levels.pop(instrument_id, None)
        if self.frozen.get(instrument_id, "").startswith(WAITING):
            self.frozen.pop(instrument_id)  # A stopped instrument waits on nothing.
        if slot is not None:
            order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
            self._observe_sale(instrument_id, slot, order)
            if slot.sale is not None:
                self._log_sale(instrument_id, slot.sale)
            self._cancel_own(slot)

    def on_stop(self) -> None:
        self.stopping = True
        if STEP_TIMER in self.clock.timer_names():
            self.clock.cancel_timer(STEP_TIMER)
        for instrument_id in list(self.slots):
            self.stop_instrument(instrument_id)
