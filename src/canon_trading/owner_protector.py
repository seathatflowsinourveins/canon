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

Coverage follows protect.py:514-570: sum remaining shares of simple working sells; unknown cover,
contingencies and pending changes freeze; full cover needs no sell; partial foreign cover freezes;
otherwise protect uncovered whole shares. Order ownership must establish EXTERNAL coverage before
node/protect lineage is counted; another strategy's position or sell freezes the instrument by name.
Freezing retains an armed sell unless aggregate working sells could oversell the EXTERNAL long.
Cancellation requires an exact client ID this instance sent. Re-sizing our protection is cancel,
acknowledgement, then re-arm from the retained level; DAY expiry also permits re-arming.

The engine owns trailing and release after arming. Every submission is a reduce-only DAY trailing
stop-limit, emulated on BID_ASK, with extended_hours=True. Offsets use Mover's existing
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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_FLOOR
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING
from typing import Any
from typing import Self

from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import ContingencyType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OmsType
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

from . import protect
from .custody import OURS
from .custody import PROTECT
from .custody import lineage
from .ma_cross import refuse_unknown_keywords
from .mover import offsets
from .price_step import up_on_step

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import OrderCancelRejected


STEP_TIMER = "owner-protection"
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


def _after_arming(slot: Holding, tick: QuoteTick) -> bool:
    """Received after the level was fixed, and stamped after the quote it was fixed on."""
    return (
        slot.armed_at is not None
        and tick.ts_init > slot.armed_at
        and (slot.armed_event is None or tick.ts_event > slot.armed_event)
    )


@dataclass
class Holding:
    level: Price | None
    opened_at: int
    initialized_at: int
    protect_id: ClientOrderId | None = None
    last_observation: int | None = None
    distance: protect.Distance | None = None
    limit_offset: Decimal | None = None
    fresh: bool = False
    ended: bool = False
    # The clock when the level was fixed. Only a quote received after it is an observation
    # (the lead's ruling, 2026-11-13, 15:55: ruling 6, no election on one observation).
    armed_at: int | None = None
    # The venue time of the quote the level was fixed on, if any: a quote spends the guard only if
    # it is also stamped after it, so a re-delivered or late older quote never does.
    armed_event: int | None = None


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
        self.subscribed: set[InstrumentId] = set()
        # The holding (ts_opened, ts_init) whose supplied level was refused: it is named
        # unprotected at every step, and a later holding on the symbol self-arms.
        self.refused: dict[InstrumentId, tuple[int, int]] = {}
        self.stopped_instruments: set[InstrumentId] = set()
        self.stopping = False

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
        cached = self.cache.quote(instrument_id)
        self.slots[instrument_id] = Holding(
            level,
            position.ts_opened,
            position.ts_init,
            armed_at=self.clock.timestamp_ns(),
            armed_event=None if cached is None else cached.ts_event,
        )
        self._subscribe_holding(instrument_id)
        self._manage(instrument_id)

    def _subscribe_holding(self, instrument_id: InstrumentId) -> None:
        if instrument_id not in self.subscribed:
            self.subscribe_quotes(instrument_id, params={"feed": self.settings.feed})
            self.subscribe_bars(
                BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"),
                params={"feed": self.settings.feed},
            )
            self.subscribed.add(instrument_id)

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
            self.slots[instrument_id] = Holding(
                None, position.ts_opened, position.ts_init, fresh=True
            )
            self._subscribe_holding(instrument_id)
            self._manage(instrument_id)

    def on_start(self) -> None:
        self.settings.validate_protection()
        for instrument_id, level in list(self.levels.items()):
            self.adopt_holding(instrument_id, level)
        self._discover_holdings()
        self.clock.set_timer(STEP_TIMER, timedelta(seconds=1), callback=self._on_step)

    def on_quote(self, quote: QuoteTick) -> None:
        if not self.stopping and quote.instrument_id in self.slots:
            self._manage(quote.instrument_id, quote)

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        if event.strategy_id != self.strategy_id or event.client_order_id not in self.sent_ids:
            return
        self.cancel_requested.discard(event.client_order_id)
        self._say(
            f"{event.instrument_id}: our cancel rejected: {event.reason}; "
            "re-evaluate on the next quote or step"
        )

    def _on_step(self, event: TimeEvent) -> None:
        if self.stopping:
            return
        for instrument_id in list(self.slots):
            self._manage(instrument_id)
        self._discover_holdings()

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

    def _plan_holding(self, instrument_id: InstrumentId, slot: Holding, tick: QuoteTick) -> bool:
        """One cache snapshot through paper:protect's planner; no history request or tightening.

        Cache.bars is newest first, so feed ATR in timestamp order. The adapter's external minute
        bars are stamped at their open (data.py:1237-1257); ts_init is receipt, not completeness.
        """
        now = self.clock.timestamp_ns()
        age = timedelta(seconds=(now - tick.ts_event) / 1_000_000_000)
        try:
            if age > protect.QUOTE_MOST:
                # The bound paper:protect plans on (protect.py:360, :693); never armed from it.
                raise ValueError(
                    f"waiting for a fresh quote: the cached one is {age.total_seconds():.0f} s "
                    f"old, over the {protect.QUOTE_MOST.total_seconds():.0f} s a level is "
                    "planned on"
                )
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
                slot.level, slot.armed_at, slot.armed_event = level, now, tick.ts_event
            slot.distance, slot.limit_offset = plan, limit
            self._say(f"{instrument_id}: armed distance {distance}; {plan.source()}")
        except ValueError as exc:
            self._record_unprotected(instrument_id, str(exc))
            return False
        return True

    def _manage(self, instrument_id: InstrumentId, quote: QuoteTick | None = None) -> None:
        slot = self.slots[instrument_id]
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        if slot.protect_id is not None and order is None:
            self._freeze(instrument_id, "our sent sell is absent from cache; awaiting its state")
            return
        if order is not None and order.status == OrderStatus.FILLED:
            # Completion of our sale ends the level even if an owner buy kept NETTING open.
            self._reset_holding(instrument_id)
            position = self._external_long(instrument_id)
            if position is not None:
                self._say(f"{instrument_id}: our sell filled; remaining shares are a new holding")
                self.slots[instrument_id] = Holding(
                    None, position.ts_opened, position.ts_init, fresh=True
                )
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
                    self._freeze(
                        instrument_id, f"our protective sell ended {order.status}; inspect it"
                    )
                    return
                self.cancel_requested.discard(order.client_order_id)
                slot.protect_id, order = None, None
            elif order.client_order_id in self.cancel_requested:
                return  # No replacement until the original has a terminal acknowledgement.
        uncovered = self._uncovered(instrument_id, position.signed_qty, slot.protect_id)
        if uncovered is None:
            return
        self.frozen.pop(instrument_id, None)
        if order is not None:
            if _remaining(order) != uncovered:
                self._cancel_own(slot)
            return
        if uncovered <= 0:
            return
        tick = quote if quote is not None else self.cache.quote(instrument_id)
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
        if slot.distance is None and not self._plan_holding(instrument_id, slot, tick):
            return
        assert (
            slot.level is not None and slot.distance is not None and slot.limit_offset is not None
        )
        previous = slot.last_observation
        if previous is not None and tick.ts_event <= previous:
            return
        if previous is None and not _after_arming(slot, tick):
            return  # No send and no guard before a new observation since the arming.
        if previous is None and (slot.fresh or bid <= slot.level.as_decimal()):
            slot.last_observation = tick.ts_event
            self._say(f"{instrument_id}: holding level {slot.level}; one observation cannot elect")
            return
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
            None,
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_price=slot.level,
            trigger_type=TriggerType.BID_ASK,
            time_in_force=TimeInForce.DAY,
            reduce_only=True,
            emulation_trigger=TriggerType.BID_ASK,
        )
        if self.cache.order(order.client_order_id) is not None:
            self._freeze(instrument_id, "generated client order ID already exists; left untouched")
            return
        slot.protect_id = order.client_order_id
        self.sent_ids.add(order.client_order_id)
        self.submit_order(order, params={"extended_hours": True})

    def _reset_holding(self, instrument_id: InstrumentId) -> None:
        slot = self.slots.pop(instrument_id)
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
        slot = self.slots.pop(instrument_id, None)
        self.levels.pop(instrument_id, None)
        if slot is not None:
            self._cancel_own(slot)

    def on_stop(self) -> None:
        self.stopping = True
        if STEP_TIMER in self.clock.timer_names():
            self.clock.cancel_timer(STEP_TIMER)
        for instrument_id in list(self.slots):
            self.stop_instrument(instrument_id)
