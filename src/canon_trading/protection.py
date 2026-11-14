"""The Mover's protection, composed inside its owning NautilusTrader strategy.

Contract: nautilus-trader@d53849b216e2 python/tests/strategies/acceptance.py:514-589,
631-657 arms a trailing sell for an entry; nautilus-trader-v1@27a8e54e7ac3
nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:376-385 arms on
PositionOpened/Changed. The owning strategy supplies the order factory, cache and
submit/cancel commands (rc8 trading/__init__.pyi:538-612); trailing_stop_limit
accepts reduce_only (common/__init__.pyi:1523-1547), and the emulator retains
submit params on release (crates/execution/src/order_emulator/emulator.rs:1522-1536).

The Mover lends its existing state to this part, preserving every scheduling and
order decision. Other strategies use the position/quote callbacks with private
holding state. The fixed default distances are MoverConfig's defaults; no template
selects or derives a separate trail. Advanced Mover arms retain their existing
context, calculations and records. The retained method entry points on Mover are
forwarders so the existing override and method-only test seams keep working.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal
from typing import Any

from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType

from . import mover
from .mover import EXTENDED_HOURS
from .mover import FLOOR_WAIT_SECS
from .mover import HOLD_MOST_SECS
from .mover import PROFILE_SETTINGS
from .mover import REFUSED
from .mover import SECOND_NS
from .mover import SUB_DOLLAR
from .mover import TRAIL_CAP
from .mover import DistanceRefusal
from .mover import ExitRecord
from .mover import Mover
from .mover import Print
from .mover import Slot
from .mover import TrailPlan
from .mover import chase_floor
from .mover import entry_basis
from .mover import fill_trigger
from .mover import l_in_force
from .mover import offsets
from .mover import offsets_in_force
from .mover import print_time
from .mover import profile_record
from .mover import profile_segment
from .mover import quoted_spread
from .mover import ratchet_trail
from .mover import two_sided
from .mover import up_on_step
from .price_step import down_on_step
from .price_step import exit_price
from .price_step import stop_trigger_price


@dataclass(frozen=True)
class ProtectionConfig:
    """The default Mover arm, with reduce-only sells for a composed template."""

    trail: Decimal = Decimal("0.02")
    exit_through: Decimal = Decimal("0.005")
    chase_cap: Decimal = Decimal("0.02")
    chase_secs: int = 5
    exit_ticks: int = 1
    reduce_only: bool = True

    @property
    def trail_mode(self) -> str:
        return "fixed"

    @property
    def trigger(self) -> TriggerType:
        return TriggerType.BID_ASK


class _HoldingContext:
    """State for a strategy that forwards events; engine access stays on that strategy."""

    def __init__(self, protection: Protection, strategy: Any, config: ProtectionConfig) -> None:
        self.protection = protection
        self.strategy = strategy
        self.settings = config
        self.persistence = False
        self.slots: dict[InstrumentId, Slot] = {}
        self.exits: list[ExitRecord] = []
        self.sale_rows: dict[str, dict[str, Any]] = {}
        self.sale_owner: dict[str, ExitRecord] = {}
        self.sell_triggers: dict[str, str] = {}
        self.sell_triggers_read: set[str] = set()
        self.released: set[str] = set()
        self.protection_fallbacks = {"position average price": 0, "current bid": 0}
        self.protect_paths = {"held_stop": 0, "unpriced_exit": 0, "trail": 0}
        self.remakes_under_floor = 0
        self.fills: list[tuple[str, str, str, str, int]] = []
        self.notes: dict[InstrumentId, list[str]] = {}
        self.dropped: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return getattr(self.protection, name)
        return getattr(self.strategy, name)


class Protection:
    """One protective path, submitting and canceling through the owning strategy."""

    def __init__(
        self,
        strategy: Any,
        config: ProtectionConfig,
        *,
        context: Any | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config
        # Mover's state and method-only stand-ins include the optional advanced arms;
        # the callback context below only runs the fixed default arm.
        self._context: Any = _HoldingContext(self, strategy, config) if context is None else context

    def on_position_opened(self, event: Any) -> None:
        """Arm from the owning long position; later partial fills reuse its one slot."""
        if event.strategy_id != self.strategy.strategy_id or event.signed_qty <= 0:
            return
        state = self._context
        slot = state.slots.get(event.instrument_id)
        if slot is None:
            slot = Slot(
                event.instrument_id,
                BarType.from_str(f"{event.instrument_id}-1-MINUTE-LAST-EXTERNAL"),
                deque(),
                deque(),
            )
            state.slots[event.instrument_id] = slot
            slot.entry_id = event.opening_order_id
            slot.entry_received_ns = event.ts_init
            slot.was_held = True
        held = self._held(slot)
        if held > 0:
            self._protect(slot, held, event.ts_init)

    def on_position_changed(self, event: Any) -> None:
        self.on_position_opened(event)

    def on_position_closed(self, event: Any) -> None:
        if event.strategy_id != self.strategy.strategy_id:
            return
        slot = self._context.slots.get(event.instrument_id)
        if slot is not None:
            self.cancel_flat(slot)
            order = self._order(slot.protect_id)
            if order is None or order.is_closed:
                self._context.slots.pop(event.instrument_id)

    def on_quote(self, quote: Any) -> None:
        slot = self._context.slots.get(quote.instrument_id)
        if slot is None:
            return
        order = self._order(slot.protect_id)
        if order is not None and any(isinstance(e, OrderReleased) for e in order.events()):
            self._context.released.add(order.client_order_id.value)
            slot.exiting = True
        self._read_released_triggers()
        held = self._held(slot)
        if held > 0:
            self._protect(slot, held, quote.ts_init)
        elif held == 0:
            self.cancel_flat(slot)

    def cancel_flat(self, slot: Slot) -> bool:
        """Withdraw the previous holding's sell; report whether it was still working."""
        state = self._context
        order = state._order(slot.protect_id)
        if order is None or order.is_closed:
            return False
        if slot.was_held and (order.is_emulated or order.is_open) and not order.is_pending_cancel:
            state.cancel_order(order.client_order_id)
        return True

    def stop_mover(self) -> None:
        """Preserve the Mover's stop-time cancellation contract."""
        state = self._context
        for slot in state.slots.values():
            if state._working(slot):
                state.cancel_all_orders(slot.instrument_id)

    def _sale_sent(
        self,
        slot: Slot,
        order: Any,
        now: int,
        kept: Decimal | None,
    ) -> None:
        """One row of the holding's exit record for a sale order sent (the lead's D3 terms of
        2026-11-12, A3): the offsets or limit price read off the order, the distance the record gives
        at this moment, the trigger kept beside the sale, and the quoted spread at this step."""
        state = self._context
        row = slot.exit_record
        trailing = getattr(order, "trailing_offset", None)
        limit_offset = getattr(order, "limit_offset", None) if trailing is not None else None
        price = None if trailing is not None else order.price
        spread = quoted_spread(state.cache.quote(slot.instrument_id))
        floor_m = state._floor_m()
        floor = None if floor_m is None or spread is None else floor_m * spread
        sale: dict[str, Any] = {
            "client_order_id": order.client_order_id.value,
            "kind": "limit" if trailing is None else "trailing_stop_limit",
            "sent_ns": now,
            "quantity": str(order.quantity),
            "trailing_offset": None if trailing is None else str(trailing),
            "limit_offset": None if limit_offset is None else str(limit_offset),
            "limit_price": None if price is None else str(price),
            "kept_trigger": None if kept is None else str(kept),
            "distance_in_force": (
                None if row is None or row.trail_distance is None else str(row.trail_distance)
            ),
            "spread": None if spread is None else str(spread),
            "spread_floor": None if floor is None else str(floor),
            "sized_loss": None if slot.sized_loss is None else str(slot.sized_loss),
            "remake": False,
            "under_floor": None,
            "filled_quantity": "0",
            "ended": None,
            "ended_ns": None,
            "reason": None,
            "cause": None,
        }
        state.sale_rows[sale["client_order_id"]] = sale
        if row is not None:
            row.sales.append(sale)
            state.sale_owner[sale["client_order_id"]] = row

    def _read_released_triggers(self) -> None:
        """Keep, for each sell the emulator released, the last trigger its events carry: the one it was
        released at, final then, so each is read once, in the step, since an order-event handler here
        reads no cache. The exit it decides keeps that trigger for every sale sent after it."""
        state = self._context
        for cid in sorted(state.released - state.sell_triggers_read):
            state.sell_triggers_read.add(cid)
            order = state._order(ClientOrderId(cid))
            if order is None or order.side != OrderSide.SELL:
                continue
            trigger = None
            for event in order.events():
                price = getattr(event, "trigger_price", None)
                if price is not None:
                    trigger = price
            if trigger is None:
                continue
            state.sell_triggers[cid] = str(trigger)
            for slot in state.slots.values():
                if slot.protect_id is not None and slot.protect_id.value == cid:
                    slot.exit_trigger = Decimal(str(trigger))

    def _order(self, client_order_id: ClientOrderId | None) -> Any | None:
        """The cached order (one seam, so a test can stand in for the cache)."""
        state = self._context
        return None if client_order_id is None else state.cache.order(client_order_id)

    def _instrument(self, instrument_id: InstrumentId) -> Any:
        """The cached instrument of a watched name; a slot is opened only for one that is there."""
        state = self._context
        instrument = state.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Mover: instrument {instrument_id} has left the cache")
        return instrument

    def _held(self, slot: Slot) -> Decimal:
        """Read this strategy's signed open quantity and whole-holding average together."""
        state = self._context
        positions = state.cache.positions_open(
            instrument_id=slot.instrument_id, strategy_id=state.strategy_id
        )
        quantity, notional = Decimal(0), Decimal(0)
        valid_average = True
        for position in positions:
            quantity += position.signed_qty
            average = getattr(position, "avg_px_open", None)
            if average is None:
                valid_average = False
                continue
            average = Decimal(str(average))
            if not average.is_finite() or average <= 0 or position.signed_qty <= 0:
                valid_average = False
                continue
            notional += position.signed_qty * average
        slot.holding_average = notional / quantity if valid_average and quantity > 0 else None
        return quantity

    def _working(self, slot: Slot) -> bool:
        """Whether an order of this slot is not closed: emulated, in flight or open."""
        state = self._context
        return any(
            order is not None and not order.is_closed
            for order in (state._order(slot.entry_id), state._order(slot.protect_id))
        )

    def _ensure_exit(self, slot: Slot, held: Decimal) -> ExitRecord:
        """Start a holding's record before any synchronous release/fill; keep it through re-prices."""
        state = self._context
        if slot.exit_record is None:
            slot.exit_record = ExitRecord(
                slot.instrument_id,
                None if slot.entry_record is None else slot.entry_record["client_order_id"],
                held,
            )
            state.exits.append(slot.exit_record)
        else:
            slot.exit_record.quantity = held + slot.exit_record.filled
        return slot.exit_record

    def _mark_exit(self, slot: Slot, now: int, price: Decimal | None, reason: str) -> None:
        """Store the first decision to sell. Safe in an event handler: plain values only."""
        row = slot.exit_record
        if row is not None and row.trigger_ns is None:
            row.trigger_ns, row.trigger_price, row.trigger_reason = now, price, reason
        stops = ("trailing stop", "retained trigger", "held stop")
        if slot.entry_record is not None and reason.startswith(stops):
            slot.entry_record["stopped_out"] = True

    def _protect(self, slot: Slot, held: Decimal, now: int) -> None:
        state = self._context
        state._ensure_exit(slot, held)
        order = state._order(slot.protect_id)
        adoption_ready = False
        if slot.adoption_guard is not None and not slot.exiting:
            armed_at, observed_at = slot.adoption_guard
            observation = state._adoption_observation(slot)
            # The fill/held anchor or the emulator may have raised the supplied
            # level. Confirmation belongs to the actual order's trigger.
            level = slot.trail_trigger
            if order is not None and order.trigger_price is not None:
                level = order.trigger_price.as_decimal()
            if (
                now > armed_at
                and observation is not None
                and (observed_at is None or observation[0] > observed_at)
            ):
                if observed_at is None:
                    slot.adoption_guard = (now, observation[0])
                elif (level is not None and observation[1] <= level) or (
                    order is not None and order.is_emulated and order.is_activated
                ):
                    # Above the armed level, a dormant order still waits for a price
                    # that can seed positive offsets. Re-arming it into `_hold` here
                    # would replace that level with an unrequested stop at the average.
                    slot.adoption_guard = None
                    adoption_ready = True
            if (
                slot.adoption_guard is not None
                and order is not None
                and not order.is_closed
                and (not order.is_emulated or order.quantity.as_decimal() == held)
            ):
                return
        if state.persistence and not slot.exiting:
            state._persistent_trail(slot, now)
            if not slot.exiting:
                return
        order = state._order(slot.protect_id)
        raised = False
        if not slot.exiting and slot.adoption_guard is None and slot.sized_loss is not None:
            # H1-H3 apply to the holding, including fills of another entry (lead, 2026-11-13).
            # Read its average before either anchor is refreshed. A cheaper fill cannot lower a
            # reached stop; if a fill brings the average from above the kept stop to at or under
            # it, latch the sale here for both the held and emulated paths. A stop already at or
            # above the average is a winner's stop: a lower average crosses nothing (lead's H3).
            average = state._position_average(slot)
            levels = [slot.held_stop, slot.trail_trigger]
            if order is not None and order.is_emulated and order.trigger_price is not None:
                levels.append(order.trigger_price.as_decimal())
            reached = max(
                (level for level in levels if level is not None and level > 0), default=None
            )
            kept = None if reached is None else stop_trigger_price(reached)
            if (
                average is not None
                and slot.protection_anchor is not None
                and average < slot.protection_anchor
                and kept is not None
                and slot.protection_anchor > kept
                and average <= kept
            ):
                slot.exiting = True
                slot.protection_anchor, slot.exit_trigger = average, kept
                state._mark_exit(slot, now, average, "held stop (holding average)")
                if slot.exit_record is not None:
                    slot.exit_record.held_stop_kept = kept
                    slot.exit_record.held_stop_from_average = average - kept
                if slot.entry_record is not None:
                    slot.entry_record["held_stop_level"] = str(kept)
                    slot.entry_record["held_stop_trigger"] = "holding average"
                state._end_hold(slot, now, "held_stop")
            else:
                # The average can rise with the quantity unchanged (a late fill of an earlier
                # holding's canceled sale, then a dearer buy): the stop is remade from the new
                # average, keeping any level reached, as a change of quantity remakes it
                # (r-heldnext P1-2; position.rs:1050-1118 keeps the average of what is held)
                raised = (
                    average is not None
                    and slot.protection_anchor is not None
                    and average > slot.protection_anchor
                )
        if slot.exiting and order is not None and order.is_emulated:
            # rc8's emulator cancels locally; confirm it ended before selling all held shares.
            if not order.is_pending_cancel:
                state.cancel_order(order.client_order_id)
            order = state._order(slot.protect_id)
            if order is not None and not order.is_closed:
                return
        if (
            slot.exiting
            and slot.floor is None
            and not slot.floor_released
            and order is not None
            and order.price is not None
        ):
            # the released sale's own price, whether it is open, filled or was refused
            cap = state.settings.chase_cap if slot.trail_plan is None else slot.trail_plan.chase_cap
            slot.floor = chase_floor(order.price.as_decimal(), cap)
            if order.is_open:
                slot.priced_ns = now  # the chase's clock starts when the sale is seen working
        if order is None or order.is_closed:
            if (
                order is not None
                and order.status in REFUSED
                and now - slot.priced_ns < state.settings.chase_secs * SECOND_NS
            ):
                return  # a sale the engine or the venue refused is sent again, every `chase_secs`
            if slot.exiting:
                anchor = None
                if state.persistence:
                    # The lower of the confirming print and the bid. A one-sided quote's zero bid
                    # is no price, and neither is no quote: the trade newest by its own time
                    # (`_last_print`) then stands in for the bid, so a recovery cannot lift the
                    # sale and a fall under the print is followed, and the sale is given that
                    # basis as its anchor (`_limit_sell`).
                    quote = state.cache.quote(slot.instrument_id)
                    basis = None if quote is None else quote.bid_price.as_decimal()
                    quoted = basis is not None and basis > 0
                    if not quoted:
                        basis = state._last_print(slot)
                    if slot.breach_price is not None:
                        basis = (
                            slot.breach_price if basis is None else min(basis, slot.breach_price)
                        )
                    if not quoted and basis is not None:
                        anchor = basis
                        state._note(
                            slot,
                            f"{slot.instrument_id}: no bid at a confirmed sale; priced from the "
                            "lower of the confirming print and the newest trade",
                        )
                    # set once, for the first sale: a floor released after its wait stays released
                    if basis is not None and slot.floor is None and not slot.floor_released:
                        first = exit_price(basis, state.settings.exit_ticks, None)
                        slot.floor = chase_floor(first, state.settings.chase_cap)
                state._limit_sell(slot, held, now, slot.floor, anchor=anchor)
            else:
                state._trail(slot, held, now)
        elif order.is_emulated:
            tightened = state._warm_tighten(slot, now)
            if state.settings.trail_mode == "ratchet" and not slot.ratcheted and not tightened:
                tightened = state._ratchet(slot, now)
            elif state.settings.trail_mode == "profile" and not tightened:
                tightened = state._profile_tighten(slot, now)
            # No order is modified. A trailing sell that no longer covers what is held is canceled,
            # and one for all of it is made from the trigger this one had reached.
            if (
                order.quantity.as_decimal() != held
                or tightened
                or raised
                or (adoption_ready and not order.is_activated)
            ) and not order.is_pending_cancel:
                if order.trigger_price is not None:
                    reached = order.trigger_price.as_decimal()
                    slot.trail_trigger = max(reached, slot.trail_trigger or reached)
                    state.sell_triggers[order.client_order_id.value] = str(reached)
                if order.price is not None:
                    limit = order.price.as_decimal()
                    slot.trail_limit = max(limit, slot.trail_limit or limit)
                state.cancel_order(order.client_order_id)
                # The emulator cancels its own order before the call returns, so the cancel is seen
                # terminal here and the new sell follows in this step, not a second later.
                canceled = state._order(slot.protect_id)
                if canceled is not None and canceled.is_closed:
                    state._trail(slot, held, now)
                    if slot.protect_id != order.client_order_id:
                        state._remade(slot)
        elif slot.exiting:
            state._chase(slot, order, held, now, slot.floor)

    def _persistent_trail(self, slot: Slot, now: int) -> None:
        """Confirm queued trades in the step; no order is emulated with either persistence arm on.

        Consecutive counted prints have at least G milliseconds between their times, with no expiry.
        Any recovery resets the breach, even inside G or at the last print's own time; any print at
        or under the confirmed high resets the high arm. A print with an older time than the last
        read, a late report, is dropped: it neither counts nor resets. A new high is the lowest of
        N supporting prints, so a spike alone cannot lift the level; with `high_count` at one
        that arm is off and one higher print does lift it, a limit of a row with only the breach
        arm on. The level is armed from the entry's average fill as it stands at the arming step and
        is not re-based as a partly filled entry fills further. Confirmation latches through later
        recoveries in this batch, and `_protect` sends the plain DAY limit in this same step.
        """
        state = self._context
        entry = state._order(slot.entry_id)
        row = slot.exit_record
        bought = (
            Decimal(str(entry.avg_px))
            if entry is not None and entry.avg_px is not None
            else row.bought_notional / row.quantity
            if row is not None and row.quantity > 0 and row.bought_notional > 0
            else None
        )
        if bought is None:
            state._note(
                slot,
                f"{slot.instrument_id}: held with no filled entry of this strategy to size a "
                "trailing sell from; not protected, and sold only at the wind-down",
            )
            return
        trailing, _ = offsets(bought, state.settings.trail, state.settings.exit_through)
        if slot.trail_high is None or slot.trail_trigger is None:
            # The level starts from the entry's own average fill, the one price here that is a trade
            # of ours. A cached last print is a single observation: taken as the high, one stray
            # print above the market would put the level over it and the next ordinary prints
            # would sell. Prints after this step raise the high only as N of them confirm it.
            slot.trail_high = bought
            slot.trail_trigger = stop_trigger_price(bought - trailing)
            # Begin after the highest stamp already received, on persistence's own stamp, not the
            # readers' venue-newest print. A late report of an older print after this step must not
            # count toward this holding's breach. The seeded print counts as received.
            kept = slot.newest_print
            slot.observed_ns = max(slot.stamp_seen, 0 if kept is None else kept.ns)
            slot.observations.clear()
            state._say(f"{slot.instrument_id}: persistence armed, trigger {slot.trail_trigger}")
            return
        gap_ns = state.settings.confirm_gap_ms * 1_000_000
        # These prints have arrived already; exchange timestamps need not match the timer's clock.
        while slot.observations:
            stamp, price = slot.observations.popleft()
            if stamp < slot.observed_ns:
                continue  # an older time, a late report: it neither counts nor resets
            # A print sharing the last time, as a sweep's prints can, is not a second observation:
            # it may reset either arm and never counts.
            fresh = stamp > slot.observed_ns
            slot.observed_ns = stamp
            highs = slot.high_observations
            if price > slot.trail_high:
                spaced = state.settings.high_count == 1 or not highs
                if fresh and (spaced or stamp - highs[-1][0] >= gap_ns):
                    highs.append((stamp, price))
                    if len(highs) > state.settings.high_count:
                        highs.popleft()
                    if len(highs) == state.settings.high_count:
                        slot.trail_high = max(slot.trail_high, min(p for _, p in highs))
                        slot.trail_trigger = stop_trigger_price(
                            max(slot.trail_trigger, slot.trail_high - trailing)
                        )
            else:
                highs.clear()
            breaches = slot.breach_observations
            if price <= slot.trail_trigger:
                if fresh and (not breaches or stamp - breaches[-1] >= gap_ns):
                    breaches.append(stamp)
                if len(breaches) >= state.settings.breach_count:
                    slot.exiting = True
                    slot.breach_price = price
                    slot.exit_trigger = slot.trail_trigger
                    # the decision to sell, at the confirming print's own time and price: the
                    # strategy's own trailing stop, its exit row naming the prints and the trigger
                    # (the D3 terms, A5)
                    state._mark_exit(
                        slot,
                        stamp,
                        price,
                        f"trailing stop (persistence: breach confirmed by {len(breaches)} prints at "
                        f"or under {slot.trail_trigger})",
                    )
                    slot.observations.clear()
                    state._say(
                        f"{slot.instrument_id}: breach confirmed at {stamp}, trigger "
                        f"{slot.trail_trigger}; selling by limit"
                    )
                    if entry is not None and not entry.is_closed:
                        state._time_out(slot, entry, now)
                    return
            else:
                breaches.clear()

    def _trail(self, slot: Slot, held: Decimal, now: int) -> None:
        """Protect what is held, using the cache average or bid when the entry fill is unknown.

        Arm the emulator from its followed quote/trade, or hold a stop at the known anchor
        while the followed market price is unavailable.
        """
        state = self._context
        trigger = state.settings.trigger
        at = state._followed(slot)
        if (at is None or at <= 0) and slot.adoption_guard is None:
            # The emulator cannot arm without a market price: hold a stop the strategy checks itself
            # (the lead's decision (b), 2026-11-12), not a sale at once.
            state._hold(slot, held, now)
            return
        if slot.held_since is not None and slot.adoption_guard is None:
            # the held stop at the average as it is now, before any price is compared with it: a fill
            # since the last hold step moves it, as each hold step would (dsh-reader-343, F1;
            # t-heldrefresh, H1 and H2), and the exit record names the refreshed level, measured from
            # the average it was refreshed at, as each hold step records it (the combined landing's
            # code review, finding 1)
            state._held_at_l(slot)
            state._record_held_stop(slot)
        if (
            slot.adoption_guard is None
            and slot.held_since is not None
            and slot.held_stop is not None
            and at is not None
        ):
            # the first followed price meets the held stop before the hold ends, under either trigger
            # (the lead's H1-H3, 2026-11-12; the xr3 fast reader's SF1 and its BID_ASK twin): at or
            # under it, the hold's sale at once, as `_hold` compares, with the hold's low received since
            # the hold began (`Slot.held_low`; the xr4 reader's F2; the lead's H3); checked before the
            # anchor is read, so an anchor that cannot be read does not leave the hold unchecked
            # (code-review-high-succ/5)
            level = slot.held_stop
            low = at if slot.held_low is None else min(at, slot.held_low)
            if low <= level:
                slot.exiting, slot.trail_trigger, slot.trail_limit = True, None, None
                state._mark_exit(slot, now, low, "held stop (followed price)")
                if slot.entry_record is not None:
                    slot.entry_record["held_stop_trigger"] = "followed price"
                slot.exit_trigger = level  # before `_end_hold` clears it, as `_hold` does
                state._end_hold(slot, now, "held_stop")
                state._limit_sell(slot, held, now, slot.floor, anchor=at)
                return
        try:
            bought = slot.protection_anchor = state._trail_anchor(slot)
        except RuntimeError as error:
            # Raised from the step timer this would stop every slot; it is named and retried next step.
            state._note(slot, str(error))
            return
        # the stop held while no price was followed, read before `_end_hold` clears it: the remade sell
        # keeps it as it keeps a reached trigger (xr7-r-heldl, P1), rounded up onto the step, never down
        # (xr7-cx-succ/F3)
        held_level = (
            None if slot.held_stop is None or slot.held_stop <= 0 else up_on_step(slot.held_stop)
        )
        holding = slot.held_since is not None
        # the reached trigger on Alpaca's step, one value for the check below and for the remade sell
        reached = slot.trail_trigger
        from_hold = False
        if holding and held_level is not None:
            # above the held stop, the trail starts from it, never lower, so the held stop goes only
            # by a sale or by a trigger at least as high taking its place; taken on the slot only as
            # the hold ends below, so a hold kept for want of room keeps its start
            from_hold = reached is None or held_level >= stop_trigger_price(reached)
            reached = held_level if reached is None else max(reached, held_level)
        # Every slot's kept trigger floors onto Alpaca's step, as main does. An adopted slot's
        # supplied level went UP onto the step at adoption (`adopt_holding`), so it floors to
        # itself; a native trigger the emulator reached off the step is floored, never raised,
        # since raising it sold a bid that never met it (the lead's ruling, 2026-11-13, 15:48).
        kept = None if reached is None else stop_trigger_price(reached)
        origin = None if reached is None else f"reached {reached}"
        met = "retained trigger"
        if from_hold:
            # the held stop is the level kept, at or over any trigger a trailed sale had reached
            origin, met = f"held stop {kept}", "held stop (retained)"
        anchor = fill_trigger(bought, slot.sized_loss)
        if anchor is not None and (kept is None or anchor > kept):
            # L under the average fill, rounded up onto the step so a share never loses more than L;
            # the emulator then moves the trigger only up from it (`crates/execution/src/trailing.rs:61`,
            # `:100`), and a remade sell keeps the higher of it, the trigger reached and the held stop
            kept, origin = anchor, f"{slot.sized_loss} under the fill {bought}"
            # with no higher retained level, the stop L under the fill is met as the sale is armed
            met = "trailing stop (L under the fill, met when armed)"
        plan = slot.trail_plan
        trailing, limit = offsets_in_force(
            slot,
            bought,
            # L until a recorded tightening (ratchet, warmed ATR or profile) takes the trigger: the
            # tighter trail then places it; with no room for L under the fill, the trail of the fill,
            # as before S2, never a trigger at zero
            state.settings.trail if plan is None else plan.trail,
            state.settings.exit_through if plan is None else plan.exit_through,
            state._floor_m(),
            basis=entry_basis(state._order(slot.entry_id)),
        )
        if kept is not None and at is not None and at <= kept and slot.adoption_guard is None:
            # the market is at the trigger the canceled sell had reached, or the stop held: the sale
            # is due now, whatever room the floor leaves a trailing sell (the landing review, P2)
            if holding:
                state._end_hold(slot, now, "trail")
            slot.exiting, slot.trail_trigger, slot.trail_limit = True, None, None
            state._mark_exit(slot, now, at, met)
            state._say(
                f"{slot.instrument_id}: at {at}, the trigger {kept} ({origin}) is met; "
                "selling by limit"
            )
            slot.exit_trigger = kept
            state._limit_sell(slot, held, now, slot.floor)
            return
        seed = at
        if trigger == TriggerType.LAST_PRICE:
            # rc8's OrderEmulator seeds synchronously at submission from its last delivered trade
            # (emulator.rs:665, :1616-1621), not the venue-newest print `_followed` reads. Check that
            # price too: a late print must not seed a non-positive trigger or release limit (P1-1).
            trade = state.cache.trade(slot.instrument_id)
            seed = None if trade is None else trade.price.as_decimal()
        unsafe_seed = seed is None or at is None or min(at, seed) - max(trailing, limit) <= 0
        if unsafe_seed and slot.adoption_guard is None:
            # A3+O's floor in price terms can reach the followed price: the emulator would seed the
            # trigger at it less the offset (`crates/execution/src/trailing.rs:61`), at or under zero,
            # and its limit with it. No such sell is sent: the stop is held, and sells through the
            # last known price or ends in its bounded unpriced exit (xr7-r-a3o-1afe, P2). Before
            # `_end_hold`, so the hold keeps its start. The price followed now is handed to the hold,
            # which compares it with the stop it sets up and keeps it as the hold's low: a bid already
            # through that stop sells in this step, and a recovery before the next one does not hide
            # it (the combined landing's Codex second reader, P1)
            state._hold(slot, held, now, followed=at)
            return
        if holding:
            state._end_hold(slot, now, "trail")
            # the held stop's place goes to the trigger the remade sell starts from
            slot.trail_trigger = reached
        instrument = state._instrument(slot.instrument_id)
        if slot.exit_record is not None:
            slot.exit_record.trail_distance = trailing
        state._say(
            f"{slot.instrument_id}: trailing SELL {held}, its trigger {trailing} under the highest "
            f"{'bid' if trigger == TriggerType.BID_ASK else 'trade'}, its limit {limit} under it"
            + ("" if kept is None else f", from the trigger {kept} ({origin}) on the step")
        )
        # The pinned emulator seeds a trailing limit from the order's own price and moves it only to a
        # better one (`crates/execution/src/trailing.rs:61-65`); with no price the remade sell's first
        # limit is the bid less its offset, under the one already reached after a pullback.
        # Capped at the kept trigger less this sell's own spread between its offsets, the limit the
        # emulator would seed beside that trigger: the kept trigger is on Alpaca's step,
        # and a sub-cent trail can leave the reached limit above it (the landing review of this fix,
        # finding 1), and a released sale keeps the room its limit offset gives it under the trigger.
        # Without a ratchet over $1 the cap is the lower, by what the trigger lost to the step. The
        # cap is rounded down to Alpaca's step, which a sub-cent offset leaves it off (the review of
        # the cap, finding 1); the adapter would otherwise floor it out of sight.
        kept_limit = (
            None
            if kept is None or slot.trail_limit is None
            else down_on_step(min(slot.trail_limit, kept - (limit - trailing)))
        )
        if kept_limit is not None and kept_limit <= 0:
            # The offset gap can exceed a small kept trigger, or its cap can round to zero (P2-1).
            # Leave that limit unset: the seed checked above supplies a positive price, and rc8's
            # trailing.rs:61-100 only raises sell levels afterwards, including between our steps.
            kept_limit = None
        order = state.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            None if kept_limit is None else instrument.make_price(kept_limit),
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            # A preset trigger alone matches at submission (emulator.rs:679-699). A stop
            # already met by adoption's first bid rests until recovery above that level;
            # a fresh breached quote instead takes the ordinary cancel/remake sale path.
            activation_price=(
                instrument.make_price(up_on_step(max(kept, limit, trailing) + SUB_DOLLAR))
                if slot.adoption_guard is not None and kept is not None
                else None
            ),
            trigger_price=None if kept is None else instrument.make_price(kept),
            trigger_type=trigger,
            time_in_force=TimeInForce.DAY,
            emulation_trigger=trigger,
            reduce_only=slot.adopted or self.config.reduce_only,
        )
        slot.protect_id = order.client_order_id
        slot.trailed = True
        if slot.adoption_guard is not None:
            slot.trail_trigger = kept
        if kept is not None:
            state.sell_triggers[order.client_order_id.value] = str(kept)
        slot.priced_ns = (
            now  # the trigger is kept: a remake that is refused starts the next from it
        )
        state._sale_sent(slot, order, now, kept)
        state.submit_order(order, params=dict(EXTENDED_HOURS))

    def _remade(self, slot: Slot) -> None:
        """Mark the trailing sale `_trail` just sent in place of one canceled for a changed holding or
        a tightening as a remake, and count it when its trailing distance is under the spread floor at
        this step (the lead's ruling (ii) on re-flooring, 2026-11-13): what is sent does not change. A
        sale at once (`_limit_sell`) is no remade trailing sale and is not marked; the fixed arm, which
        takes no floor, has none to be under. Not counted: a remake refused and sent again later, which
        goes as a first send, and one whose market price was missing, held (`_hold`) and sent later."""
        state = self._context
        row = None if slot.protect_id is None else state.sale_rows.get(slot.protect_id.value)
        if row is None or row["kind"] != "trailing_stop_limit":
            return
        row["remake"] = True
        if row["trailing_offset"] is None or row["spread_floor"] is None:
            return
        row["under_floor"] = Decimal(row["trailing_offset"]) < Decimal(row["spread_floor"])
        if row["under_floor"]:
            state.remakes_under_floor += 1

    def _hold(self, slot: Slot, held: Decimal, now: int, followed: Decimal | None = None) -> None:
        """The lead's decision (b), 2026-11-12: without a safe price to arm the emulator, hold a stop
        at the average fill, else the entry's limit, less the trail distance in force. It sells when
        the followed price or a price received during the hold meets the stop, or when the last
        known price (the newest trade by venue time, else the latest minute close: `_last_known`)
        is through that level, so it does not depend on the missing side and adds no subscription.
        With no two-sided quote within `HOLD_MOST_SECS` of receiving the first fill it sells by limit
        as an `unpriced_exit`. A hold the floor starts for want of room under a followed price is
        handed that price (`followed`); with none handed the hold reads it itself (`_followed`), so
        every caller compares it, and a price already through the stop it sets up sells at once."""
        state = self._context
        if slot.held_since is None:
            received = slot.entry_received_ns
            if received is None:
                # no receipt on this node (a restart, or fills reconciled before the handler ran): the
                # venue's time of the entry's first fill, as before the one-clock change, so a holding
                # found long after its fill is not held a further 60 s (code-review-high-succ2/8)
                received = state._entry_filled_ns(slot)
            # receipt and `now` are both this host's clock; a hold after the holding was trailed starts now
            slot.held_since = now if slot.trailed or received is None else received
            anchor = state._held_anchor(slot)
            if anchor is not None:
                plan = slot.trail_plan
                distance = offsets_in_force(
                    slot,
                    anchor,
                    state.settings.trail if plan is None else plan.trail,
                    state.settings.exit_through if plan is None else plan.exit_through,
                    state._floor_m(),
                    basis=entry_basis(state._order(slot.entry_id)),
                )[0]
                # A3+O's floor in price terms can exceed the anchor: no stop at or under zero is held,
                # and with none the hold ends in its bounded unpriced exit (xr7-r-a3o-1afe, P2); else on
                # Alpaca's step from the start, rounded up, the level its trailing sell would start
                # from, so the hold's checks and its record read the stop in force (code-review-high-succ2/4)
                level = anchor - distance
                on_step = level if level <= 0 else up_on_step(level)
                slot.held_stop = None if level <= 0 else level if on_step == level else on_step
                slot.protection_anchor = anchor
            if slot.trail_trigger is not None:
                # a trailed sale had reached this trigger: the hold keeps it, on Alpaca's step (#312)
                kept = stop_trigger_price(slot.trail_trigger)
                slot.held_stop = kept if slot.held_stop is None else max(slot.held_stop, kept)
            if slot.entry_record is not None:
                slot.entry_record["protect_path"] = "held_stop"
                slot.entry_record["held_stop_level"] = (
                    None if slot.held_stop is None else str(slot.held_stop)
                )
            state._note(
                slot,
                f"{slot.instrument_id}: no safe trailing price; holding a stop at {slot.held_stop}",
            )
        state._ensure_exit(slot, held)
        state._held_at_l(slot)
        state._record_held_stop(slot)
        slot.holding_from = state._holding_since(slot)
        low = slot.held_low
        if followed is None:
            followed = state._followed(slot)
        if followed is not None and followed > 0:
            # a price followed as the hold was set up, which no quote or print handler caught because
            # the hold had not begun: it meets the stop in this step, as `_trail` compares the price it
            # follows, and is kept as the hold's low only where `on_quote` or `on_trade` would keep it,
            # so under a print trigger with the holding's start unknown a print from before the entry
            # is not kept once the fill is recorded (#367's code review, finding 1). Read here when no
            # caller hands it, so every caller compares it; a low kept above today's stop can meet a
            # stop a later fill raises (the closing unit's reader, limit 9)
            low = followed if low is None else min(low, followed)
            if state.settings.trigger == TriggerType.BID_ASK or slot.holding_from is not None:
                slot.held_low = low
        last, source = state._last_known(slot)
        met, by = last, source
        if slot.held_stop is not None and low is not None and low <= slot.held_stop:
            # a followed price since the hold began met the stop: a met held stop is never forgotten, and
            # a later print the holding does not own (a late one stamped before the entry) must not hide
            # it (the lead's H3, 2026-11-13 09:20; the DeepSeek reader of the successor, P2). A low that
            # never met the stop prices nothing here, the unpriced exit included (code-review-high-succ2/1)
            met, by = low, "followed price"
        if slot.held_stop is not None and met is not None and met <= slot.held_stop:
            slot.exiting = True
            state._mark_exit(slot, now, met, f"held stop ({by})")
            if slot.entry_record is not None:
                slot.entry_record["held_stop_trigger"] = by
            slot.exit_trigger = slot.held_stop  # before `_end_hold` clears it
            state._end_hold(slot, now, "held_stop")
            state._limit_sell(slot, held, now, slot.floor, anchor=met)
            return
        # `unpriced_exit` is for a holding with no two-sided quote; under `LAST_PRICE` one with a
        # two-sided quote and no print of its own waits for a print or the wind-down
        if now - slot.held_since >= HOLD_MOST_SECS * SECOND_NS and not (
            state.settings.trigger == TriggerType.LAST_PRICE
            # a locked or crossed quote has both sides: it waits too (w3-d-succ2/P2)
            and two_sided(state.cache.quote(slot.instrument_id))
        ):
            anchor = last if last is not None else slot.protection_anchor
            slot.exiting = True
            state._mark_exit(slot, now, last, "unpriced_exit")
            state._end_hold(slot, now, "unpriced_exit")
            state._limit_sell(slot, held, now, slot.floor, anchor=anchor)

    def _held_at_l(self, slot: Slot) -> None:
        """S2 under #320's held stop: an entry sized for L holds its stop L under the average fill,
        rounded up onto the step (`fill_trigger`), the level its trailing sell would start from, and
        follows the average as the entry fills further, as a trailing sell remade for all that is held
        would; a trigger a trailed sale had reached still holds it up. A recorded tightening (the
        ratchet's, the warmed ATR's or a profile's) follows the average too, at the distance in force
        (`offsets_in_force`: the tightened trail, never wider than L), so a later fill at a higher
        average moves the stop (dsh-reader-337, P1). No room for L under the average keeps #320's
        level, tightened or not."""
        state = self._context
        if slot.sized_loss is None:
            return
        anchor = state._held_anchor(slot)
        if anchor is None or fill_trigger(anchor, slot.sized_loss) is None:
            return
        distance = slot.sized_loss
        if slot.tightened:
            plan = slot.trail_plan
            # the floor in price terms too (O3), as `_trail` sends it: the held stop follows the
            # distance the remade sale would trail by
            distance = offsets_in_force(
                slot,
                anchor,
                state.settings.trail if plan is None else plan.trail,
                state.settings.exit_through if plan is None else plan.exit_through,
                state._floor_m(),
                basis=entry_basis(state._order(slot.entry_id)),
            )[0]
        at_l = fill_trigger(anchor, distance)
        if at_l is None:
            return
        reached = None if slot.trail_trigger is None else stop_trigger_price(slot.trail_trigger)
        # never lower than this hold has held at L: a cheaper fill lowers the average, not the stop
        at_l = at_l if slot.held_at_l is None else max(at_l, slot.held_at_l)
        slot.held_at_l = at_l
        slot.held_stop = at_l if reached is None else max(at_l, reached)
        slot.protection_anchor = anchor
        if slot.exit_record is not None:
            slot.exit_record.trail_distance = distance
        if slot.entry_record is not None:
            slot.entry_record["held_stop_level"] = str(slot.held_stop)

    def _record_held_stop(self, slot: Slot) -> None:
        """The kept level and its distance from the average on the exit record, for every holding that
        holds a stop, sized for L or not (w3-d-succ2/P3-1). A cheaper fill can make the distance
        negative: the stop then sits over the average and the next price through it sells (H1)."""
        state = self._context
        if slot.exit_record is None or slot.held_stop is None:
            return
        # a step that cannot read the average (the entry gone from the cache, no position average,
        # no limit price) measures the level in force from the last average a step read, and with none
        # read records no distance. The protection anchor is no stand-in: it can date from the hold's
        # first step, or be a bid (`_trail_anchor`) (r-dsh-heldrec P2, P3)
        anchor = state._held_anchor(slot)
        if anchor is not None:
            slot.held_average = anchor
        slot.exit_record.held_stop_kept = slot.held_stop
        slot.exit_record.held_stop_from_average = (
            None if slot.held_average is None else slot.held_average - slot.held_stop
        )

    def _end_hold(self, slot: Slot, now: int, path: str) -> None:
        """Record how the held stop ended and for how long it held; clear it."""
        state = self._context
        if slot.held_since is None:
            return
        if slot.entry_record is not None:
            slot.entry_record["protect_path"] = path
            slot.entry_record["held_stop_secs"] = (now - slot.held_since) / SECOND_NS
        state.protect_paths[path] += 1
        slot.held_stop = slot.held_since = slot.held_low = slot.held_at_l = None
        slot.holding_from = None

    def _held_anchor(self, slot: Slot) -> Decimal | None:
        """The whole holding's average, else the entry's average fill, else its limit."""
        state = self._context
        average = state._position_average(slot)
        if average is not None:
            return average
        entry = state._order(slot.entry_id)
        if entry is not None and entry.avg_px is not None:
            filled = Decimal(str(entry.avg_px))
            if filled.is_finite() and filled > 0:
                return filled
        price = None if entry is None else getattr(entry, "price", None)
        return None if price is None else price.as_decimal()

    def _newest_trade(self, slot: Slot) -> Print | None:
        """One reader for the newest usable trade by venue time: the slot's kept print or the
        cache's last trade, whichever is later at the venue. Cache.trade returns the last insertion,
        not the newest event (crates/common/src/cache/mod.rs:6364-6373 at the pin); an older print
        reported late therefore moves none of these readers. The engine's order emulator is not one
        of them: it elects an emulated trailing sell on each trade as delivered
        (crates/execution/src/order_emulator/emulator.rs:1093-1105), so a late print reaches it unless
        the adapter sets it aside before the data engine. A cache trade whose venue time is unusable
        (`print_time`) is never read, kept print or not (the lead's order of 2026-11-13, 10:01)."""
        state = self._context
        kept = slot.newest_print
        trade = state.cache.trade(slot.instrument_id)
        at = None if trade is None else print_time(trade, slot.quote_lag)
        if trade is not None and at is not None and (kept is None or at > kept.event_ns):
            return Print.of(trade, slot.quote_lag)
        return kept

    def _last_print(self, slot: Slot) -> Decimal | None:
        """The newest trade's price, else what the held stop reads (`_last_known`): the anchor of a
        sale with no bid. With no print, that is the latest minute close under `BID_ASK` and nothing
        under `LAST_PRICE`, where a close is no price of this holding (code-review-high-succ/3)."""
        state = self._context
        newest = state._newest_trade(slot)
        if newest is not None:
            return newest.price
        return state._last_known(slot)[0]

    def _last_known(self, slot: Slot) -> tuple[Decimal | None, str]:
        """The newest trade by venue time, else the latest minute close: what the held stop reads.
        Under `LAST_PRICE` only a trade of this holding (`_holding_trade`): with none, a minute close is
        no newer than a print of it, so nothing is known (code-review-high-succ/3); under `BID_ASK` as
        #320 made it."""
        state = self._context
        trade = state._newest_trade(slot)
        if state.settings.trigger == TriggerType.LAST_PRICE:
            trade = None if trade is None else state._holding_trade(slot, trade)
            return (None, "none") if trade is None else (trade.price, "last trade")
        if trade is not None:
            return trade.price, "last trade"
        if slot.closes:
            return slot.closes[-1], "minute close"
        return None, "none"

    def _position_average(self, slot: Slot) -> Decimal | None:
        """Refresh through the holding reader; a quantity-only seam supplies no average."""
        state = self._context
        slot.holding_average = None
        held = getattr(state, "_held", None)
        if held is not None:
            held(slot)
        elif hasattr(state.cache, "positions_open"):
            # Method-only seams may borrow the protection path without the holding reader.
            Mover._held(state, slot)
        return slot.holding_average

    def _trail_anchor(self, slot: Slot, *, record: bool = True) -> Decimal:
        """The cache's quantity-weighted holding average, else the entry fill, else the current bid.

        `Cache.positions_open(instrument_id, strategy_id)` and `Position.avg_px_open/signed_qty`
        are pinned rc8's contracts. Missing or non-positive/non-finite averages cannot anchor a sale.
        Each fallback use (including a refused remake) is counted, without changing entry gating.
        """
        state = self._context
        entry = state._order(slot.entry_id)
        average = state._position_average(slot)
        if entry is not None and entry.avg_px is not None:
            filled = Decimal(str(entry.avg_px))
            if filled.is_finite() and filled > 0:
                # one entry's holding averages its own fill: the same value, kept as the fill reads
                return filled if average is None or average == filled else average
        source = "position average price" if average is not None else "current bid"
        quote = state.cache.quote(slot.instrument_id)
        anchor = (
            average
            if average is not None
            else None
            if quote is None
            else quote.bid_price.as_decimal()
        )
        if anchor is None or not anchor.is_finite() or anchor <= 0:
            raise RuntimeError(
                f"{slot.instrument_id}: no entry, position average price or positive current bid to protect held shares"
            )
        if record:
            state.protection_fallbacks[source] += 1
            state._note(
                slot,
                f"{slot.instrument_id}: no filled entry price; trailing sell anchored on {source} {anchor}",
            )
        return anchor

    def _followed(self, slot: Slot) -> Decimal | None:
        """The trigger's followed price: the bid for BID_ASK, the newest trade of this holding for
        LAST_PRICE (`_holding_trade`); None before the first."""
        state = self._context
        if state.settings.trigger == TriggerType.BID_ASK:
            quote = state.cache.quote(slot.instrument_id)
            return None if quote is None else quote.bid_price.as_decimal()
        trade = state._holding_trade(slot)
        return None if trade is None else trade.price

    def _holding_trade(self, slot: Slot, trade: Print | None = None) -> Print | None:
        """The newest print by venue time (`_newest_trade`), or None when the venue stamped it before
        Alpaca accepted the entry (else before its first fill on record): such a print is not of this
        holding, and one stray print would set the trigger, meet a kept one or go through the held stop
        (`_persistent_trail` never takes the print cached before arming either). The acceptance, not the
        fill: Alpaca stamps a fill with its trade update's time and the SIP stamps the print of that fill
        on its own clock, which can be earlier, so the fill's own print would be dropped. All are venue
        times (the print's `ts_event`; the order's `ts_accepted`, Alpaca's `submitted_at`; the fill's),
        so this host's clock does not move the boundary. With no entry fill on record it is the newest
        print. A caller may pass its own read of the newest print."""
        state = self._context
        trade = state._newest_trade(slot) if trade is None else trade
        if trade is None:
            return None
        since = state._holding_since(slot)
        return trade if since is None or trade.event_ns >= since else None

    def _holding_since(self, slot: Slot) -> int | None:
        """The venue time from which a print is of this holding: the earlier of Alpaca's acceptance of
        the entry (`ts_accepted`) and its first fill on record, or None with no entry fill on record.
        `_holding_trade` and the held stop's low (`on_trade`, through `Slot.holding_from`) both read
        it, so a print one follows the other counts."""
        state = self._context
        filled = state._entry_filled_ns(slot)
        if filled is None:
            return None
        entry = state._order(slot.entry_id)
        accepted = None if entry is None else getattr(entry, "ts_accepted", None)
        # a 0 is an unset time, no boundary: the pinned order reads None until accepted and the first
        # fill sets it if acceptance did not (NautilusTrader d53849b216e2, crates/model/src/python/orders/
        # mod.rs:188-190, crates/model/src/orders/mod.rs:1413-1414), but an own-book copy defaults it to 0
        # (orders/mod.rs:539) and a stand-in or restored order may carry that
        return filled if not accepted else min(filled, int(accepted))

    def _entry_filled_ns(self, slot: Slot) -> int | None:
        """The venue's time of the entry's first fill on record, or None."""
        state = self._context
        entry = None if slot.entry_id is None else slot.entry_id.value
        return min((ns for cid, _, _, _, ns in state.fills if cid == entry), default=None)

    def _chase(
        self, slot: Slot, order: Any, held: Decimal, now: int, floor: Decimal | None
    ) -> None:
        """Re-price a sell limit that still rests after `chase_secs`: `exit_ticks` through the bid, not
        below `floor`, and only downwards. The soft line does not hold it. `chase_secs` paces it, and
        the engine's submit throttle (`paper_node.REQUEST_CEILING` on a paper node, else the run's own
        rate) counts the remade sale but not the cancel, so a chase is two requests to Alpaca."""
        state = self._context
        if not order.is_open or order.is_pending_update or order.is_pending_cancel:
            return
        if order.leaves_qty.as_decimal() < held:
            # more was bought after this sale was sent: cancel it, and the next step sells all that is
            # held in one order, so no share is without its sale and none has two
            state.cancel_order(order.client_order_id)
            return
        if now - slot.priced_ns < state.settings.chase_secs * SECOND_NS:
            return
        quote = state.cache.quote(slot.instrument_id)
        if quote is None or order.price is None:
            return
        bid = quote.bid_price.as_decimal()
        if bid <= 0:
            return  # a one-sided quote is no price to chase to, nor a bid through the floor
        price = exit_price(bid, state.settings.exit_ticks, floor)
        through = exit_price(bid, state.settings.exit_ticks, None)
        if floor is None or price < order.price.as_decimal() or through >= floor:
            slot.floor_since = None  # not resting at the floor with the bid through it: restart
        else:
            # resting at its floor with the bid through it: after FLOOR_WAIT_SECS, the lead's (b)
            if slot.floor_since is None:
                slot.floor_since = now
            if now - slot.floor_since < FLOOR_WAIT_SECS * SECOND_NS:
                return
            if slot.exit_record is not None:
                slot.exit_record.floor_wait_ns = now - slot.floor_since
            slot.floor, slot.floor_since, slot.floor_released = None, None, True
            price = through
        if price >= order.price.as_decimal():
            return
        # Sales are exempt from the soft line and stop only at the engine's hard ceiling (the lead's
        # decision of 2026-11-12; ADR 0091: no new bound on the sale path).
        slot.priced_ns = now
        state.cancel_order(
            order.client_order_id
        )  # the next step finds it closed and sends the new limit

    def _limit_sell(
        self,
        slot: Slot,
        held: Decimal,
        now: int,
        floor: Decimal | None,
        *,
        anchor: Decimal | None = None,
    ) -> None:
        """A plain DAY limit for what is held, `exit_ticks` through the bid, not below `floor`; the exit's
        trigger (`Slot.exit_trigger`), when it has one, is kept beside this sale in the record.

        An unarmable trail supplies its known anchor so that no quote still leaves a working sale.
        """
        state = self._context
        quote = state.cache.quote(slot.instrument_id)
        bid = None if quote is None else quote.bid_price.as_decimal()
        quoted = bid is not None and bid > 0
        if not quoted:
            # no bid: the given anchor, else the newest trade or minute close, else the entry's anchor
            bid = anchor if anchor is not None else state._last_print(slot)
            if bid is None:
                bid = slot.protection_anchor
        if bid is None:
            return
        instrument = state._instrument(slot.instrument_id)
        price = exit_price(bid, state.settings.exit_ticks, floor)
        if not quoted and slot.last_sale is not None:
            price = min(price, slot.last_sale)  # a remake with no bid never re-prices upwards
        slot.last_sale = price
        order = state.order_factory.limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            Price.from_str(f"{price:.{instrument.price_precision}f}"),
            time_in_force=TimeInForce.DAY,
            reduce_only=slot.adopted or self.config.reduce_only,
        )
        slot.protect_id = order.client_order_id
        if slot.exit_trigger is not None:
            state.sell_triggers[order.client_order_id.value] = str(slot.exit_trigger)
        slot.priced_ns = now
        state._say(f"{slot.instrument_id}: SELL {held} limit {price} (floor {floor})")
        state._sale_sent(slot, order, now, slot.exit_trigger)
        state.submit_order(order, params=dict(EXTENDED_HOURS))

    def _sell_out(self, slot: Slot, held: Decimal, now: int) -> None:
        """The wind-down outside the session: withdraw the entry and the trailing sell, then sell what
        is held with a limit through the bid, re-priced every `chase_secs`, with no floor."""
        state = self._context
        # `_time_out` cancels a working entry at once; what it still buys is covered by `_chase`, which
        # re-sends for all that is held
        state._ensure_exit(slot, held)
        quote = state.cache.quote(slot.instrument_id)
        state._mark_exit(
            slot, now, None if quote is None else quote.bid_price.as_decimal(), "wind-down"
        )
        begun, slot.exiting = slot.exiting, True
        chase = state.settings.chase_secs * SECOND_NS
        order = state._order(slot.protect_id)
        if order is None or order.is_closed:
            refused = begun and order is not None and order.status in REFUSED
            if refused and now - slot.priced_ns < chase:
                return  # as in `_protect`: a refused sale is sent again every `chase_secs`
            state._limit_sell(slot, held, now, None)
        elif order.is_emulated:
            if not order.is_pending_cancel:
                state.cancel_order(order.client_order_id)
        else:
            state._chase(slot, order, held, now, None)

    def _end(self) -> None:
        """The wind-down in the session: upstream's market exit, once, for every slot.

        Pinned rc8 strategies.md:768-774 documents that a managed stop requested at backtest end
        cannot complete: engine.rs:1041-1083 already flushed timers before stop_trader, and the
        completion check is later. A flat strategy may remain RUNNING in that simulation. Live time
        continues advancing; the coordinator's 2026-11-11 ruling keeps this wind-down unchanged.
        """
        state = self._context
        if state.settings.extended_hours or state.is_exiting():
            return
        if any(state._held(slot) or state._working(slot) for slot in state.slots.values()):
            for slot in state.slots.values():
                if held := state._held(slot):
                    state._ensure_exit(slot, held)
                    quote = state.cache.quote(slot.instrument_id)
                    state._mark_exit(
                        slot,
                        state.clock.timestamp_ns(),
                        None if quote is None else quote.bid_price.as_decimal(),
                        "wind-down",
                    )
            state._say("Winding down: upstream's market exit for what is held and what works")
            state.market_exit()

    def _floor_m(self) -> Decimal | None:
        """The spread floor's multiple a sale's offset keeps (`offsets_in_force`), or None in the fixed
        arm, `MoverConfig`'s default, which takes no floor and does not move (the lead's O1-O3 of
        2026-11-12). A Mover window that names no arm runs OWN1, which takes it."""
        state = self._context
        return None if state.settings.trail_mode == "fixed" else state.settings.spread_floor_m

    def _profile_tighten(self, slot: Slot, now: int) -> bool:
        """Consider each complete minute once, then use ratchet's existing cancel/remake path.

        Known limit: offsets remain PRICE offsets from the holding's entry anchor, as in every
        arm; they do not grow with the followed price. ATR uses this minute's close as its price
        basis, and the latest spread floor is converted at the frozen anchor. A thin update leaves
        the plan alone. Top-tier tier protection and ratchet never combine with this arm.
        """
        state = self._context
        opened = slot.run_open_ns
        if opened is None or (
            slot.profile_evaluated_ns is not None and opened <= slot.profile_evaluated_ns
        ):
            return False
        slot.profile_evaluated_ns = opened
        current = slot.trail_plan
        anchor = slot.protection_anchor
        if (
            current is None
            or current.source == "tier"
            # a top-tier name keeps tier: an opening's cold-ATR entry keeps the fixed plan, raised to
            # its spread floor, for the life of the position (`_warm_tighten` keeps it once the ATR
            # warms)
            or state._top_tier(slot)
        ):
            return False  # not a profile holding, so there is no tighten to skip
        if slot.exiting:
            return state._profile_skip(slot, now, opened, "the sale is being chased")
        if state.trail_profile is None:
            return state._profile_skip(slot, now, opened, "no profile document")
        if anchor is None:
            return state._profile_skip(slot, now, opened, "no protection anchor")
        if not slot.closes:
            return state._profile_skip(slot, now, opened, "no complete minute's close")
        quote = state.cache.quote(slot.instrument_id)
        spread = None if quote is None else quoted_spread(quote)
        if spread is None:
            return state._profile_skip(slot, now, opened, "no usable quoted spread")
        floor = state.settings.spread_floor_m * spread / anchor
        if floor > TRAIL_CAP:
            return state._profile_skip(
                slot, now, opened, f"the spread floor exceeds the {TRAIL_CAP} cap"
            )
        atr = None if slot.atr is None or not slot.atr.initialized else Decimal(str(slot.atr.value))
        try:
            calculation = mover.profile_distance(
                state.trail_profile,
                slot.instrument_id.symbol.value,
                profile_segment(opened),
                None if atr is None else atr / slot.closes[-1],
                state.rdc_1.get(slot.instrument_id),
                **{name: getattr(state.settings, name) for name in PROFILE_SETTINGS},
            )
        except (ValueError, ArithmeticError, RuntimeError) as error:
            # Raised from the step timer or a bar this would stop every slot; it is named instead.
            state._note(slot, f"{slot.instrument_id}: profile not re-evaluated: {error}")
            return state._profile_skip(slot, now, opened, f"profile not re-evaluated: {error}")
        if calculation["reason"] is not None:
            return state._profile_skip(slot, now, opened, f"profile: {calculation['reason']}")
        selected, bound = calculation["distance"], "profile"
        if floor >= selected:
            selected, bound = floor, "spread floor"
        if selected > TRAIL_CAP:
            selected, bound = TRAIL_CAP, "0.02 cap"
        before = offsets_in_force(
            slot, anchor, current.trail, current.exit_through, state._floor_m()
        )[0]
        after = offsets(anchor, selected, selected / 4)[0]
        increment = state._instrument(slot.instrument_id).price_increment.as_decimal()
        if selected >= current.trail:
            return state._profile_skip(slot, now, opened, "not narrower than the trail in force")
        if after >= before and l_in_force(slot, anchor) is not None:
            # narrower than the plan but not closer than L in force: taking it would loosen the sale
            # (the lead's ruling of 2026-11-12, 21:01)
            return state._profile_skip(
                slot, now, opened, f"not narrower than L's {before} in force"
            )
        if before - after < increment:
            return state._profile_skip(
                slot, now, opened, "narrower by less than one price increment"
            )
        plan = replace(
            current,
            trail=selected,
            # Keep the bounds the warm check retained from the cold plan (sr-c-337 P2): a later
            # profile tightening must not lower a reached limit or deepen the chase.
            exit_through=min(selected / 4, current.exit_through),
            chase_cap=min(max(selected, state.settings.chase_cap), current.chase_cap),
            atr=atr,
            spread=spread,
            bound=bound,
            source="profile",
            profile=calculation,
        )
        slot.trail_plan, slot.tightened = plan, True
        state._trail_change(
            slot,
            now,
            current.trail,
            plan,
            f"profile segment {calculation['segment']}",
            profile_record(calculation),
        )
        return True

    def _profile_skip(self, slot: Slot, now: int, minute_ns: int, reason: str) -> bool:
        """Record why this evaluated minute left the trail as it was (the lead's rule); returns False."""
        if slot.entry_record is not None:
            slot.entry_record.setdefault("profile_skips", []).append(
                {"when_ns": now, "minute_ns": minute_ns, "reason": reason}
            )
        return False

    def _trail_change(
        self,
        slot: Slot,
        now: int,
        before: Decimal,
        plan: TrailPlan,
        why: str,
        profile: dict[str, Any] | None = None,
    ) -> None:
        """Keep one change of a holding's trail. A profile change carries the calculation it was made
        from, and the entry's `profile` is set to it, so the record's calculation is the one in force;
        the calculation at entry stays in `profile_at_entry`."""
        if slot.entry_record is not None:
            change = {
                "when_ns": now,
                "from": str(before),
                "to": str(plan.trail),
                "bound": plan.bound,
                "atr": None if plan.atr is None else str(plan.atr),
                "why": why,
            }
            if profile is not None:
                change["profile"] = profile
                slot.entry_record.setdefault("profile_at_entry", slot.entry_record.get("profile"))
                slot.entry_record["profile"] = dict(profile)
            slot.entry_record.setdefault("trail_changes", []).append(change)

    def _tier_applies(self, slot: Slot) -> bool:
        """The tier trail is selected: a tier base and a known baseline at or over the cut."""
        state = self._context
        settings = state.settings
        base = settings.ratchet_base if settings.trail_mode == "ratchet" else settings.trail_mode
        return base in ("tier", "profile") and state._top_tier(slot)

    def _warm_tighten(self, slot: Slot, now: int) -> bool:
        """Once a cold-fallback holding's ATR warms, re-select its trail; take it only if tighter.

        The lead's (b), 2026-11-12: the trail may tighten to the warmed ATR's plan and never widens.
        The comparison is made once, on the first step with a fill price, a usable quoted spread and a
        plan; until then it is retried each step. Each change is recorded with its time in the entry's
        `trail_changes`; the caller cancels and remakes the emulated sale, as for the ratchet.
        """
        state = self._context
        plan = slot.trail_plan
        if (
            slot.warm_checked
            or plan is None
            or plan.source != "cold_atr_fallback"
            or slot.atr is None
            or not slot.atr.initialized
        ):
            return False
        bought = state._bought(slot)
        quote = state.cache.quote(slot.instrument_id)
        if bought is None or bought <= 0 or quote is None or quoted_spread(quote) is None:
            return False  # retried next step
        profile_minute = slot.run_open_ns if state.settings.trail_mode == "profile" else None
        profile_price = None
        if profile_minute is not None:
            # This is the complete bar's profile evaluation, even if its plan is kept: use its
            # segment and close, as `_profile_tighten` does, and prevent a second evaluation on the
            # next step after a warm remake (sr-a-337 F1).
            slot.profile_evaluated_ns = profile_minute
            profile_price = slot.closes[-1] if slot.closes else None
        try:
            warmed = state._entry_trail(
                slot,
                bought,
                quote,
                now=now if profile_minute is None else profile_minute,
                profile_price=profile_price,
            )
        except DistanceRefusal:
            # the cap refuses the warmed plan: the one comparison is made and the trail in force kept,
            # neither retried each step nor noted as not applied yet (the Antigravity reader of #351,
            # P3-1)
            slot.warm_checked = True
            return False
        except (ValueError, ArithmeticError) as error:
            # The completed close can give no usable price basis; keep the working protection and
            # name the failure, as `_profile_tighten` does, rather than raise from the step timer.
            state._note(slot, f"{slot.instrument_id}: warmed ATR not applied yet: {error}")
            return False
        slot.warm_checked = True
        if state._tier_applies(slot):  # by the selection, since a spread floor can cover the bound
            state._note(
                slot,
                f"{slot.instrument_id}: the plan re-selected once the ATR warmed is tier, not the "
                f"warmed ATR's; {plan.trail} kept",
            )
            return False
        # Never wider: the sale's offset through its trigger and the chase floor keep the cold plan's
        # bounds when the warmed plan's are wider (selected / 4 and max(selected, chase_cap)).
        warmed = replace(
            warmed,
            exit_through=min(warmed.exit_through, plan.exit_through),
            chase_cap=min(warmed.chase_cap, plan.chase_cap),
        )
        if warmed.trail >= plan.trail:
            state._note(
                slot,
                f"{slot.instrument_id}: the plan re-selected with the warmed ATR ({warmed.bound} "
                f"{warmed.trail}) is not tighter than {plan.trail}; kept",
            )
            return False
        in_force = offsets_in_force(slot, bought, plan.trail, plan.exit_through, state._floor_m())
        candidate = offsets(bought, warmed.trail, warmed.exit_through, chase_cap=warmed.chase_cap)
        if l_in_force(slot, bought) is not None and not candidate < in_force:
            # narrower than the plan but not closer than L in force: taking it would loosen the sale
            # (the lead's ruling of 2026-11-12, 21:01)
            state._note(
                slot,
                f"{slot.instrument_id}: the plan re-selected with the warmed ATR ({warmed.bound} "
                f"{warmed.trail}) is not tighter than L {in_force[0]} in force; kept",
            )
            return False
        slot.trail_plan, slot.tightened = warmed, True
        if not slot.ratcheted:
            slot.initial_trail = warmed
        state._trail_change(
            slot,
            now,
            plan.trail,
            warmed,
            f"ATR warmed while held; bound {warmed.bound}",
            None if warmed.profile is None else profile_record(warmed.profile),
        )
        return True

    def _bought(self, slot: Slot) -> Decimal | None:
        """The holding's purchase price for re-selecting its trail: the entry's average fill, else the
        protection anchor."""
        state = self._context
        filled = entry_basis(state._order(slot.entry_id))
        return slot.protection_anchor if filled is None else filled

    def _ratchet(self, slot: Slot, now: int) -> bool:
        """Tighten once only while the original emulated sale works; caller cancels and remakes it."""
        state = self._context
        initial = slot.initial_trail
        quote = state.cache.quote(slot.instrument_id)
        followed = state._followed(slot)
        bought = state._bought(slot)
        if initial is None or bought is None or quote is None or followed is None:
            return False
        spread = quoted_spread(quote)
        if spread is None:
            return False
        plan = ratchet_trail(initial, bought, followed, spread, state.settings)
        if plan is None:
            return False
        current = slot.trail_plan or initial
        # The offsets in force are `_trail`'s: L under the fill while it has room and nothing has
        # tightened (the landing review of this fix on S2, finding 5), else the plan's.
        in_force = offsets_in_force(
            slot, bought, current.trail, current.exit_through, state._floor_m()
        )
        if not offsets(bought, plan.trail, plan.exit_through, chase_cap=plan.chase_cap) < in_force:
            # Rounded, the candidate's trigger and then its limit are no closer than those in force:
            # the one ratchet is kept for a later step that can tighten (Astra second read of #316,
            # finding 2).
            return False
        before = slot.trail_plan.trail if slot.trail_plan is not None else initial.trail
        slot.trail_plan, slot.ratcheted, slot.tightened = plan, True, True
        state._trail_change(slot, now, before, plan, "ratchet")
        if slot.entry_record is not None:
            slot.entry_record["ratchet"] = {
                "tightened": True,
                "when_ns": now,
                "from": str(initial.trail),
                "to": str(plan.trail),
                "bound": plan.bound,
                "spread": str(spread),
            }
        return True

    def _note(self, slot: Slot, text: str) -> None:
        state = self._context
        notes = state.notes.setdefault(slot.instrument_id, [])
        if text not in notes:
            notes.append(text)
            state.dropped.append(text)

    def _say(self, text: str) -> None:
        """One line in the node's log, from which a run's record reads which rule decided. Called from
        the step and from `on_bar` only: a handler of an order event or a signal never logs (module
        header, "Order events")."""
        state = self._context
        state.log.info(text)
