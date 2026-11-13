"""The mover strategy: ranked names in slots, a limit entry on the first rising run, a trailing sell that only rises.

A composition of upstream parts (ADR 0083, ADR 0088). What each part is, at NautilusTrader v2.0.0rc8
(`nautilus-trader@d53849b216e2`), read 2026-11-11:

* **The ranking** reaches the strategy as upstream's signal: `subscribe_signal(name)` and `on_signal`
  (`python/nautilus_trader/trading/__init__.pyi:696`, `:633`; concepts/message_bus, "Signal
  publish/subscribe": "The value is converted to a string on publish"). The ranking actor publishes
  `RANK_SIGNAL` each minute (ADR 0107); its value is `movers_rank_signal.encode_ranking`'s string, read
  by the shared `decode_ranking`: the ranked instrument IDs best first, each with its move from the last
  regular close, and its `ts_event` the time the ranking was computed. Nothing here ranks, and no
  vendor's list is read.
* **The entry** is `OrderFactory.limit` (`python/nautilus_trader/common/__init__.pyi:1388`), time in force
  DAY, at the ask of the latest quote in the cache (`entry_ticks` through it, none by default), in every
  session; it is never a market order and is not re-priced: it rests until `entry_timeout_secs`.
* **Paper size** (`size_from_budget=True`) is whole shares of the supplied startup entry figure at
  the current entry limit, through instrument.make_qty (`model/__init__.pyi`). session_window resolves
  that figure once from Alpaca's reported buying_power and the CASH engine's cash funding limit,
  divided by configured slots. Free cash read at entry clamps that startup figure, and a missing free
  balance refuses the entry by name. `flow_share` of the last `flow_bars` bars' dollar volume caps that
  size; each entry records both caps and which bound it. An explicit global max_shares can tighten the
  entry; the per-name share and position monitor follow its resolved current limit. Within those bounds
  the entry is sized for a planned loss at its trail's stop by `sizing.plan_for_loss` (S2, the lead's
  requirements of 2026-11-12), the one call site: `planned_loss`, or `loss_share` of the startup figure
  measured on Alpaca's step (`share_loss`), so a 2% trail buys the figure's shares, a wider one fewer and
  a tighter one no more. The loss per share it was sized for, L (the planned entry less the stop it
  returns), is the one value sized and sent: the trailing sell's trigger is L under the average fill,
  rounded up onto the step, then L under the highest price since (the lead's S2 property of 2026-11-12,
  17:21). L bounds the loss at the trigger, and a gap through the trigger is not bounded: each exit's
  trigger is kept beside its fill (`sell_triggers`, `fills`), so the loss past L can be measured from
  the record per exit and per window (17:43). Each refusal is counted by its name and each bound that held by its name in
  the record. `trail_cap` (`TRAIL_CAPS`) says what a non-fixed trail over the
  0.02 cap does: clamp (the default), refuse, or keep the name's own distance (`own`).
  **Legacy risk sizing** is `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, hard_limit=...)`
  (`python/nautilus_trader/risk/__init__.pyi:16-29`; `crates/risk/src/sizing.rs:39-117`): `risk` of the
  account's total balance over the distance from the entry to the lowest price the first sale is planned
  at (`planned_stop`: the first trigger, less the released limit's give, less the chase's cap), in whole
  shares, capped by `hard_limit`: the largest entry (`max_notional`, `max_shares`, and each name's
  `share_caps` where the window sets them), the free balance
  and `flow_share` of the dollar volume of the last `flow_bars` bars. No entry is sent into a quote whose
  bid is already under that planned sale: the quote is then wider than the plan the size rests on.
* **The protection with both persistence arms off** is the engine's order emulator holding a
  `TRAILING_STOP_LIMIT` sell
  (`OrderFactory.trailing_stop_limit`, `common/__init__.pyi:1523-1547`; concepts/orders/emulated and
  trailing_stop_limit): its trigger follows the bid (`BID_ASK`) or the last trade (`LAST_PRICE`) up and
  never down, and at the trigger the emulator releases a `LIMIT` through the risk engine. A release
  keeps the submit command's params (`crates/execution/src/order_emulator/emulator.rs:1368` and
  `:1522-1531`; upstream's test of it, `:4513`, covers stop orders), so the `extended_hours` mark is given
  once, on the submit.
  T1 (owner/lead, 2026-11-12) selects a fixed entry-price offset, or rc8's
  `AverageTrueRange(period).handle_bar` / `initialized` / `value`
  (`indicators/__init__.pyi:155-185`, `crates/indicators/src/python/volatility/atr.rs`). The engine's
  default is Wilder smoothing with the previous close. Each slot feeds only complete one-minute
  bars. The dollar-volume tier is a stand-in for size, never market capitalization. `ratchet_base`
  pairs ratchet with plain ATR or tier: at r initial trails of gain it tightens once to g of the initial trail, subject to
  the latest spread floor, canceling and remaking the emulator order while retaining its trigger.
  All arms keep PRICE offsets frozen from entry; BASIS_POINTS is supported upstream
  (`docs/concepts/orders/index.md:236`) but would change the offset as the followed price rises.
  If the entry order has no filled price, `Cache.positions_open` and `Position.avg_px_open`
  (`common/__init__.pyi:568`, `model/__init__.pyi:5868`) supply the held position's average;
  otherwise the current bid anchors protection. Each use is counted by name in the window record.
  Exit timing uses `OrderReleased.ts_event/released_price` and `OrderFilled.ts_event/last_qty/last_px`
  (`model/__init__.pyi:4822-4853`, `:4436-4498`). The release price is the observed bid when triggered
  (`crates/execution/src/order_emulator/emulator.rs:1424-1427`), not the sale's limit price.
  Re-entry is the lead's state rule (owner, 2026-11-12): reclaim, turn or tape-only VWAP;
  lead, no primary source, UNCALIBRATED. No arm waits for a time cooldown. Live bars have no
  session VWAP, so that arm refuses by name unless the exact completed bar's tape supplies it.
* **Persistence**, when either arm is on, composes `Strategy.on_trade`
  (`trading/__init__.pyi:638`), the step timer and `OrderFactory.limit` / `submit_order`. The
  emulator elects on one tick
  (`crates/execution/src/order_emulator/emulator.rs:1076-1104`), accepts only DEFAULT, BID_ASK and
  LAST_PRICE (`:598-606`), and the trigger test has no count or time
  (`crates/execution/src/matching.rs:555-572`), read at the pin 2026-11-12. No upstream part confirms
  a breach on N observations, so the strategy holds the stop itself and emulates no order in this
  arm. Only trade prints count; persistence requires LAST_PRICE. Handlers queue plain (timestamp,
  price) pairs and the step confirms consecutive prints, independently for a breach and a new high.
  The level is armed from the entry's own average fill, never from a cached print: one print is
  one observation. Each print is read at the earlier of its event time and its receipt, so one
  stamped ahead cannot hide those after it. Counted prints are at least `confirm_gap_ms` apart;
  zero means any strictly newer time.
  A recovery resets the breach even inside the gap; a print at or under the confirmed high resets
  the high arm. Counts never expire. N supporting prints raise the level to their lowest price less
  the trail, on Alpaca's price step, never down; a confirmed breach latches a DAY limit due in that
  step through the existing floor and chase. Each count defaults to one (off); G defaults to 100 ms.
  The on row for both arms is N=2, G=100 ms, UNCALIBRATED (ADR 0119). An arm runs on the fixed
  trail only.
* **The chase** of a released limit that rests is `cancel_order`, the cancel seen terminal at a later
  step, then a new limit for all that is held (`chase="resend"`, the default). No order is modified: the
  lead's decision of 2026-11-11, until the adapter's replace chain has passed two clean outside reviews.
  `Strategy.modify_order` (concepts/strategies, "Modifying orders") is not called, and the row that
  would ask for it, `chase="modify"`, is refused by name. The cost of a re-price is two requests and a
  moment with no working sale.
* **Prices** are ticks of upstream's `TieredTickScheme` stating Alpaca's two bands (`price_step.PRICES`;
  `crates/model/src/instruments/tick_scheme.rs`), through `next_ask_price` and `next_bid_price`.
  Native trailing releases use the instrument's fixed increment instead: a fractional-cent basis
  can release a sub-penny limit above $1. The observed regression is handed to the lead for a
  Claude-owned release-price contract; these helpers alone do not guarantee that native price.
* **The end** in the session is upstream's `market_exit()` (concepts/strategies, "Market exit"), as
  `session_ma_cross` uses it; outside the session a market close would only queue, so it is a limit.
* **Timers** are the clock's `set_timer` and `set_time_alert` with a callback (concepts/strategies,
  "Timers").

Alpaca's rules it works within (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, read
2026-11-11): outside the regular session "Only limit orders with `time_in_force` set to `day` or `gtc`
orders are accepted as extended hours eligible", so every order there is a DAY limit submitted with
`params={"extended_hours": True}` (ADR 0038); "Limit price >=$1.00: Max Decimals= 2", "Limit price <$1.00:
Max Decimals = 4" (`tick`).

What is written here, since no upstream ships it (ADR 0083, gap 3): the rule that reads a rising run from
bars, the slots, the two persistence confirmations, the re-entry and rotation rules, the chase's price
and its cap, and the yield of entries to the request budget. Every number of them is a registered row,
UNCALIBRATED (ADR 0088; the persistence rows are ADR 0119's).

**How it runs.** `on_signal` keeps the latest ranking. Once a second (`STEP_SECS`) a timer steps:

1. *Watch.* The top `watch` names of a fresh ranking get a slot: paper composition opts into
   `load_instruments`, requests missing definitions through upstream's `request_instrument`
   (`trading/__init__.pyi:916-924`), and waits for their response and cache insertion on later steps.
   One request may be outstanding, at most `RANK_MOST` per rolling minute, with all asset-GET retries
   reserved under the request soft line. The provider's actual GET attempts enter the adapter's
   ordinary request tally. On-demand paper names use the engine's absent-cap default
   (`crates/risk/src/engine/mod.rs:1448-1454`, `:2491-2505`); startup roster caps stay configured.
   Quotes and trades are subscribed first, on `feed` (ADR 0055), then the bars (`bar_spec`). A name that
   has left the ranking loses its slot once it holds nothing and nothing of it works.
2. *Each slot.* An entry not filled within `entry_timeout_secs` is canceled. What is held is protected:
   the emulated trailing stop-limit is submitted for the held quantity once a quote (or a trade, for
   `LAST_PRICE`) is in the cache; when the held quantity changes it is canceled and made again, from
   the trigger it had reached, so the trigger never falls. Its limit offset is a whole number of
   cents, so its release is a price Alpaca takes on either side of a dollar (`offsets`). At the release
   (`on_order_released`) a still-working entry is canceled, since nothing more is bought of a name
   that is being sold; a sale that covers less than is held is canceled and the next covers all of it,
   so no share is without its sale and none has two. A limit that still rests after `chase_secs` is
   re-priced `exit_ticks` through the bid, never below the position's floor: `chase_cap` under its
   first sale price, kept whether that first sale filled, rests or was refused. Below it the sale rests.
   A sale the risk engine or the venue refuses is sent again every `chase_secs`. With persistence on,
   the entry's average fill arms a local trigger, and the prints delivered after that step confirm
   its high and its breach by their own times, not the timer's. The sale goes out in the first step
   after the confirming print, so up to `STEP_SECS` later. The floor's reference is the lower of the
   confirming print and the current bid, so a recovery in the same step cannot lift it and the sale
   follows a fast fall below the stop the entry was sized against. `exit_ticks` prices that
   reference and the sale; `exit_through` still shapes entry risk sizing.
3. *Entries.* A slot whose bars have just closed `run_bars` times in a row above the close before is a
   candidate under the explicitly selected `entry_arm="bridge"`. The default `registered` arm is
   selected from `early_entry_study.ARMS`: listing needs both move and cumulative dollar-volume
   ratio, and its run/extension rules are that module's `entry_runs` and `entry_refusal`.
   ADR 0092's table rows are :73 (move), :74 (ratio), :75 (baseline sessions), :76 (rises),
   :77 (first-leg window), :78 (quiet price) and :79 (pullback). No row is tuned here.
   Registered arms require a supplied `Day` in `entry_contexts`; no context, no entry. The strategy
   makes no history request: live composition supplies history and selects the arm (M-c).
   Candidates are taken best-ranked first while a slot is free, each refused by name
   (`dropped`) when the ranking is older than `rank_max_age_secs`, the name is no longer in it, its move
   is over `max_move` (the no-chase cap), no quote is in the cache, the size comes to nothing, or the
   requests of the last 60 seconds have reached `soft_line`, which holds entries and chase re-prices. A name sold before uses the clock cooldown and reclaim by default, or a reclaim above its sale. The existing clock cooldown and reclaim are unchanged.

Exits never wait for ranking or entry budgets; chase re-prices wait at `soft_line`. The strategy is long only: a sell is sized from what is
held, one sell works at a time for a name, and nothing is sold while flat.

**Order events.** At the pin, an event that the strategy's own `submit_order`, `cancel_order` or
`modify_order` publishes reaches its handler while the strategy object is mutably borrowed
(`crates/trading/src/python/strategy.rs:2016-2037`), and reading `cache`, `portfolio`, `clock` or `log`
there raises "Already mutably borrowed"; a signal can reenter an actor too (concepts/message_bus,
"Signal callback reentry"). So every handler of an order event or a signal only stores what the event
carries, in plain attributes. The trade handler likewise queues only event timestamps and prices,
without reading engine attributes. All reading of the engine and every command happens in the timer's
step and in `on_bar`.

Measured at the pin, offline, 2026-11-11 (ADR 0088): on bars alone the emulator cannot activate a
trailing stop ("no market price available"); it needs a quote for `BID_ASK` or a trade for `LAST_PRICE`.
With a limit offset smaller than the trailing offset the released sell limit sits above a market that
has fallen through the trigger and does not fill; so the limit offset here is the trailing offset plus
`exit_through`. An emulated order is not `is_open`; `modify_order` and `cancel_order` take the client
order ID. The risk engine's per-order notional cap holds for a sale as for a buy
(`crates/risk/src/engine/mod.rs:2491-2507`; its one exemption, `:667-720`, is a full exit of a crypto
derivative), so the entry is bounded here and a node's cap is an outer bound (`mover_window`). A live
node builds a bar for every clock minute, a paused one too, flat and without volume
(`time_bars_build_with_no_updates`, on by default): a repeated close is not a rise, so a pause restarts
the run, and its minute counts as no flow.

Not built, and said so: a re-priced entry; the gain lock; a broker-held stop as an outage backstop (a
second sell for the same shares is refused while the first works); slots reserved for the newest
movers; no order into a halt (ADR 0083, gap 4). Known limits: the strategy does not know the session
calendar (the run checks it); a partly filled entry is protected only from the next step.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from itertools import groupby
from itertools import pairwise
from types import NoneType
from typing import TYPE_CHECKING
from typing import Any

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import request_counts
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderAccepted
from nautilus_trader.model import OrderCanceled
from nautilus_trader.model import OrderDenied
from nautilus_trader.model import OrderEmulated
from nautilus_trader.model import OrderExpired
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderInitialized
from nautilus_trader.model import OrderRejected
from nautilus_trader.model import OrderReleased
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderUpdated
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.risk import FixedRiskSizer
from nautilus_trader.trading import Strategy

from .ma_cross import refuse_another_type
from .ma_cross import refuse_unknown_keywords
from .movers_rank_signal import MAX_ENTRIES as RANK_MOST
from .movers_rank_signal import SIGNAL_NAME as RANK_SIGNAL
from .movers_rank_signal import RankEntry
from .movers_rank_signal import decode_ranking
from .movers_rank_signal import encode_ranking
from .paper_node import REQUEST_CEILING
from .price_step import CENT
from .price_step import SUB_DOLLAR
from .price_step import entry_price
from .price_step import exit_price
from .price_step import stop_trigger_price
from .price_step import tick
from .price_step import up_on_step
from .sizing import REFUSALS as SIZING_REFUSALS
from .sizing import plan_for_loss
from .sizing import sized_stop

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from collections.abc import Sequence
    from datetime import date

    from nautilus_trader.common import Signal
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar

    from . import early_entry_study as entry_study
    from .position_cap import PositionCap


@dataclass
class _ProtectionHolding:
    row: dict[str, Any]
    held: Decimal = Decimal(0)
    first_fill: int | None = None
    first_protection: int | None = None
    gap: int | None = None
    longest: int = 0


@dataclass
class _ProtectionSale:
    quantity: Decimal
    filled: Decimal = Decimal(0)
    working: bool = False


def protection_diagnostics(
    entries: Sequence[Mapping[str, Any]],
    orders: Sequence[Any],
    *,
    part: str,
    end_ns: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Add protection evidence to the window's entry diagnostics, without touching an order.

    Contract: nt@d53849b216e2 `Order.events`, `OrderInitialized.quantity`,
    `OrderUpdated.quantity`, `OrderFilled.last_qty/ts_event` (model/__init__.pyi),
    orders/mod.rs's open states and emulator.rs:1394-1411,1530-1588: a transformed limit keeps
    the trail's events. Read before disposal, inside the window's named diagnostics try.

    Emulated and venue-accepted sells cover their remaining quantity. Release ends emulator
    coverage; the limit covers again on acceptance (or a partial fill proving it works).
    Pending cancels/updates retain coverage until a terminal event; initialization alone covers
    nothing. Every held share needs a working sale. Sales and coverage are allocated FIFO across
    entries of the same instrument, in first-fill order; Mover normally enters only when flat.
    All durations use event clocks, with equal timestamps one instant, and stop at end_ns.
    `first_fill_to_protection_ms` is None if never filled or never protected; `first_fill_ns`
    and the summary's never-protected count distinguish them. An open gap ends at the window's
    actual observation time, not its planned end. Milliseconds retain nanosecond precision;
    the one-second count is compared in integer nanoseconds, strictly greater than one second.
    """
    holdings = {str(e["client_order_id"]): _ProtectionHolding(dict(e, part=part)) for e in entries}
    known = {o.client_order_id.value for o in orders}
    missing = holdings.keys() - known
    if missing:
        raise ValueError(f"protection: entry orders absent from cache: {sorted(missing)}")
    by_instrument: dict[str, list[tuple[int, Any, Any]]] = {}
    instruments = {str(e["instrument_id"]) for e in entries}
    for order in orders:
        instrument = str(order.instrument_id)
        if instrument in instruments:
            by_instrument.setdefault(instrument, []).extend(
                (event.ts_event, order, event)
                for event in order.events()
                if event.ts_event <= end_ns
            )

    def coverage(
        sales: dict[str, _ProtectionSale], active: dict[str, _ProtectionHolding]
    ) -> list[tuple[_ProtectionHolding, Decimal]]:
        left = sum(
            (max(Decimal(0), s.quantity - s.filled) for s in sales.values() if s.working),
            Decimal(0),
        )
        covered = []
        for holding in active.values():
            amount = min(holding.held, left)
            covered.append((holding, amount))
            left -= amount
        return covered

    def first_protection(now: int, covered: Sequence[tuple[_ProtectionHolding, Decimal]]) -> None:
        for holding, amount in covered:
            if amount > 0 and holding.first_protection is None:
                holding.first_protection = now

    for timeline in by_instrument.values():
        sales: dict[str, _ProtectionSale] = {}
        active: dict[str, _ProtectionHolding] = {}  # insertion order is first-fill order

        for now, grouped in groupby(sorted(timeline, key=lambda item: item[0]), key=lambda x: x[0]):
            batch = list(grouped)
            # All same-clock buys precede sales for allocation, independent of cache iteration order.
            for _, order, event in batch:
                if order.side != OrderSide.BUY or not isinstance(event, OrderFilled):
                    continue
                cid = order.client_order_id.value
                if cid not in holdings:
                    raise ValueError(f"protection: filled buy absent from entries: {cid}")
                holding = holdings[cid]
                active.setdefault(cid, holding)
                holding.held += event.last_qty.as_decimal()
                if holding.first_fill is None:
                    holding.first_fill = now
            first_protection(now, coverage(sales, active))
            for _, order, event in batch:
                if order.side != OrderSide.SELL:
                    continue
                sale = sales.setdefault(
                    order.client_order_id.value,
                    _ProtectionSale(order.init_event.quantity.as_decimal()),
                )
                if isinstance(event, OrderInitialized):
                    continue  # transformed limits prepend the trail's original initialization
                if isinstance(event, OrderUpdated):
                    sale.quantity = event.quantity.as_decimal()
                elif isinstance(event, (OrderEmulated, OrderAccepted)):
                    sale.working = True
                elif isinstance(
                    event, (OrderReleased, OrderCanceled, OrderDenied, OrderRejected, OrderExpired)
                ):
                    sale.working = False
                elif isinstance(event, OrderFilled):
                    sold = event.last_qty.as_decimal()
                    sale.filled += sold
                    sale.working = sale.filled < sale.quantity
                    for holding in active.values():
                        take = min(sold, holding.held)
                        holding.held -= take
                        sold -= take
                    if sold:
                        raise ValueError("protection: sale exceeds the recorded entries' holdings")
                first_protection(now, coverage(sales, active))
            for holding, amount in coverage(sales, active):
                uncovered = holding.held > amount
                if uncovered and holding.gap is None:
                    holding.gap = now
                elif not uncovered and holding.gap is not None:
                    holding.longest = max(holding.longest, now - holding.gap)
                    holding.gap = None
        for holding in active.values():
            if holding.gap is not None:
                holding.longest = max(holding.longest, end_ns - holding.gap)

    delays = []
    filled = [h for h in holdings.values() if h.first_fill is not None]
    for holding in holdings.values():
        delay = None
        if holding.first_fill is not None and holding.first_protection is not None:
            delay = holding.first_protection - holding.first_fill
            delays.append(delay)
        holding.row.update(
            first_fill_ns=holding.first_fill,
            first_protection_ns=holding.first_protection,
            first_fill_to_protection_ms=None if delay is None else delay / 1_000_000,
            max_unprotected_ms=None if holding.first_fill is None else holding.longest / 1_000_000,
        )
    return [h.row for h in holdings.values()], {
        part: {
            "entries": len(holdings),
            "filled_entries": len(filled),
            "entries_never_protected": sum(h.first_protection is None for h in filled),
            "worst_first_fill_to_protection_ms": max(delays) / 1_000_000 if delays else None,
            "worst_unprotected_ms": max(h.longest for h in filled) / 1_000_000 if filled else None,
            "entries_over_one_second": sum(h.longest > 1_000_000_000 for h in filled),
        }
    }


EXTENDED_HOURS = {"extended_hours": True}
"""The submit params that mark an order as extended-hours eligible for the adapter (ADR 0038)."""
STEP_SECS = 1
"""How often the strategy steps: reads the cache and acts."""
STEP_TIMER = "mover-step"
WIND_DOWN_ALERT = "mover-wind-down"
SECOND_NS = 1_000_000_000
BUDGET_SPAN_NS = 60 * SECOND_NS
QUERY_WAIT_SECS = 130
"""How long an account query may go unanswered before another is sent: one adapter REST call at its
longest, alpaca-py's first request and three retries at the adapter's timeouts and the waits between
them (`session_window.ADAPTER_CALL_SECS`, about 129 s). A reply later than this can arrive after the
next query's, the one case in which a report could count a buy its balance does not have."""
INSTRUMENT_REQUEST_LIMIT = RANK_MOST
"""At most one ranking's worth of instrument requests per rolling minute (20); one outstanding.
Asset GETs use alpaca-py's existing retry policy; every HTTP attempt is counted by the adapter."""
INSTRUMENT_REQUEST_RESERVE = DEFAULT_RETRY_ATTEMPTS + 1
"""Reserve all attempts of an outstanding asset GET (alpaca-py common/constants.py:11-13)."""
CHASES = ("resend",)
TRIGGERS = (TriggerType.BID_ASK, TriggerType.LAST_PRICE)
"""The triggers the local emulator accepts for an order it holds (concepts/orders/emulated)."""
REFUSED = (OrderStatus.DENIED, OrderStatus.REJECTED)
"""An order the risk engine denied or the venue rejected."""
UNLOCKED = (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED)
"""An order's states before the venue has taken it, so before the account locks its cash."""
TRAIL_CAP = Decimal("0.02")
"""The cap on a non-fixed trail (the lead's (ii), 2026-11-12), applied as `trail_cap` says. UNCALIBRATED."""
TRAIL_CAPS = ("clamp", "refuse", "own")
TRAIL_CAP_DEFAULT = TRAIL_CAPS[0]
"""What an entry does with a non-fixed trail over `TRAIL_CAP` (the third cap arm, the lead's T3 of
2026-11-12): `clamp` sets it to the cap and refuses a spread floor over it (the default, as before);
`refuse` refuses the entry by name; `own` keeps the name's own distance and the size carries it, through
`sizing.plan_for_loss`, whose 0.10 ceiling still refuses by name. UNCALIBRATED."""
DISTANCE_UNDER_STEP = "distance_under_step"
"""The Mover's own entry refusal after sizing: L, rounded down onto the entry's step, is nothing (S2)."""
BID_AT_STOP = "bid_at_stop"
"""The Mover's own entry refusal after sizing, counted with `sizing.REFUSALS`: the bid is at or under the
stop the entry was sized against, so the first trailing sell would trigger at once (S2)."""
PAPER_LOSS_SHARE = Decimal("0.02")
"""The planned loss on paper accounts A and B, as a share of the slot's startup figure (the lead's ruling
of 2026-11-12, 16:36): the loss at the stop that the budget sizing already accepted at the 0.02 cap, so a
2% stop sizes to the budget, a wider one sizes down and a tighter one is held at the budget. No new
number; UNCALIBRATED. Account C and a live node have no default: there the figure is the owner's."""
HOLD_MOST_SECS = 60
"""The lead's bound (2026-11-12, decision (b)) on a held stop: with no two-sided quote within this many
seconds of the fill, the holding is sold by limit and counted as an `unpriced_exit`. UNCALIBRATED."""
FLOOR_WAIT_SECS = 60
"""The lead's (b), 2026-11-12: a sale resting at its chase floor with the bid through it for this many
seconds drops the floor and follows the bid (`floor_released`). UNCALIBRATED."""
TIER_TRAIL = Decimal("0.002")
TRAIL_SETTINGS: dict[str, dict[str, Any]] = {
    "trail_mode": {
        "default": "fixed",
        "arms": ("fixed", "atr", "tier", "ratchet"),
        "status": "UNCALIBRATED",
    },
    "atr_k": {
        "default": Decimal(1),
        "alternates": (Decimal(2), Decimal(3)),
        "status": "UNCALIBRATED",
        "source": "lead, no primary source",
    },
    "atr_n": {"default": 14, "alternates": (7, 28), "status": "UNCALIBRATED"},
    "spread_floor_m": {
        "default": Decimal(2),
        "alternates": (Decimal(1), Decimal(3)),
        "status": "UNCALIBRATED",
        "source": "lead, no primary source",
    },
    "liquidity_tier_cut": {
        "default": Decimal(1_000_000),
        "alternates": (Decimal(500_000), Decimal(2_000_000)),
        "status": "UNCALIBRATED",
    },
    "ratchet_r": {
        "default": Decimal(2),
        "alternates": (Decimal(1), Decimal(3)),
        "status": "UNCALIBRATED",
    },
    "ratchet_g": {
        "default": Decimal("0.5"),
        "alternates": (Decimal("0.25"), Decimal("0.75")),
        "status": "UNCALIBRATED",
    },
    "ratchet_base": {"default": "atr", "arms": ("atr", "tier"), "status": "UNCALIBRATED"},
    "flow_share": {
        "default": Decimal("0.05"),
        "alternates": (Decimal("0.01"),),
        "status": "UNCALIBRATED",
        "source": (
            "Lean VolumeWeightedAveragePriceExecutionModel defaults to 1% of the current bar's "
            "volume; this alternate applies the fraction to our configured flow_bars window: "
            "https://github.com/QuantConnect/Lean/blob/master/Algorithm.Framework/Execution/VolumeWeightedAveragePriceExecutionModel.py"
        ),
    },
}
"""T1/T2's registered settings; the liquidity cut is baseline dollars per minute, a stand-in for size.
The clock default retains the cooldown and reclaim check; state arms are window opt-ins."""
PAPER_TRAIL_LIMIT = (
    "Alpaca's paper fills are simulated and ignore quoted size and market impact: tight stops look "
    "better on paper than live, including slippage; UNCALIBRATED until the tape with measured spreads."
)
TRAIL_SOURCE_LIMIT = (
    "ATR multiples and the spread floor: lead, no primary source; no outside source agrees on an "
    "ATR multiple or ties a stop to the spread. Both UNCALIBRATED until the tape with measured spreads."
)
SHIPPED_ATR_NOTE = (
    "The one shipped ATR default found is backtesting.py TrailingStrategy's 6 ATR at its bar "
    "frequency (set_trailing_sl(n_atr=6)); this is a note, not a row to fire: "
    "https://kernc.github.io/backtesting.py/doc/backtesting/lib.html#backtesting.lib.TrailingStrategy"
)
WORKING = (
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_UPDATE,
    OrderStatus.PENDING_CANCEL,
    OrderStatus.TRIGGERED,
)
"""An order's states after the venue took it and before it is done, in which a buy can still fill."""


def entry_reserve(orders: Iterable[Any], *, reported: bool) -> Decimal:
    """What the other slots' entry `orders` hold against the free cash the next entry is sized from.

    A calculated account locks an order's cash once the venue takes it, so only an order not yet taken
    (`UNLOCKED`) is counted, at its quantity. A reported account locks nothing (the Alpaca adapter
    reports cash as total and free), so every entry still working is counted at its open quantity: the
    filled part is counted as a fill since the last report (ultra review of the composition, finding 3).
    """
    held = Decimal(0)
    for order in orders:
        if order.status in UNLOCKED:
            held += order.quantity.as_decimal() * order.price.as_decimal()
        elif reported and order.status in WORKING:
            held += order.leaves_qty.as_decimal() * order.price.as_decimal()
    return held


def format_rank(entries: Sequence[tuple[InstrumentId, Decimal]]) -> str:
    """Adapt Decimal moves to the shared encoder, retaining the window and replay callers' API.

    A move must be finite and preserved by the float's shortest text; otherwise a `ValueError` names
    the instrument and move. The shared codec also refuses duplicate names, more than `RANK_MOST`
    entries, and instrument IDs outside ALPACA before a value can be published.
    """
    ranked = []
    for instrument_id, move in entries:
        if not move.is_finite():
            raise ValueError(f"{instrument_id} has a move that is not finite: {move}")
        converted = float(move)
        if Decimal(str(converted)) != move:
            raise ValueError(
                f"{instrument_id} has a move that cannot round-trip as a float: {move}"
            )
        ranked.append(RankEntry(instrument_id, converted))
    return encode_ranking(ranked)


def chase_floor(first: Decimal, chase_cap: Decimal) -> Decimal:
    """The lowest price a position's sale is chased to: `chase_cap` under its first sale price, up to
    the next price Alpaca takes."""
    return up_on_step(first * (1 - chase_cap))


def offsets(
    entry: Decimal, trail: Decimal, exit_through: Decimal, *, distance: Decimal | None = None
) -> tuple[Decimal, Decimal]:
    """The trailing offset and the limit offset, as prices. The trailing offset is `distance` when the
    entry was sized for one (S2's L, already on the step), else `trail` of the entry up to the entry's
    step: it places the trigger, which the emulator keeps to itself. The limit offset
    is that plus `exit_through` of the entry, up to a whole cent and a cent over the trailing offset at
    least: it places the released limit, the one price that reaches the venue. This preserves cents
    when the highest bid is in cents, including across a dollar between steps. A fractional-cent
    basis retains its residue: the native calculation uses the instrument's fixed 0.0001 increment
    (`crates/execution/src/trailing.rs:182-220`), not PRICES. That observed invalid release above $1 is
    handed off to the lead's Claude lane; these offsets do not fix it. The cost under a dollar is a
    release up to a cent lower than `exit_through` asks, included in `planned_stop` for legacy sizing.
    """
    step = tick(entry)
    trailing = (
        max((entry * trail).quantize(step, rounding=ROUND_CEILING), step)
        if distance is None
        else distance
    )
    limit = (trailing + entry * exit_through).quantize(CENT, rounding=ROUND_CEILING)
    return trailing, max(limit, (trailing + SUB_DOLLAR).quantize(CENT, rounding=ROUND_CEILING))


def fill_trigger(fill: Decimal, distance: Decimal | None) -> Decimal | None:
    """A trailing sell's trigger `distance` (S2's L) under the average `fill`, rounded up onto Alpaca's
    step (`up_on_step`) so a share never loses more than L at it; None with no L or no room under the
    fill. The lead's S2 property of 2026-11-12, 17:21: the stop is L under the fill, never under the
    entry's limit."""
    if distance is None or fill - distance <= 0:
        return None
    return up_on_step(fill - distance)


def share_loss(entry: Decimal, figure: Decimal, share: Decimal | None) -> Decimal:
    """The loss the budget sizing accepted at a `share` stop, measured on Alpaca's step: the figure's
    whole shares at `entry` times what each loses at that stop (`sizing.sized_stop`, the one stop
    computation `plan_for_loss` sizes against). A figure under one share prices no loss,
    which the caller refuses as `size_zero`. A trail of `share` then sizes to the figure exactly, a wider one
    sizes down and a tighter one is held at the figure (the lead's ruling of 2026-11-12, option 2 of
    L6's question). Nothing is sent at this stop: it prices the loss only."""
    if share is None:
        raise ValueError("no planned loss and no loss share")
    return (figure // entry) * (entry - sized_stop(entry, share))


def planned_stop(
    entry: Decimal, trail: Decimal, exit_through: Decimal, chase_cap: Decimal
) -> Decimal:
    """The lowest price the first sale is planned at, which the size is risked against: the first
    trigger less what the released limit may give (`offsets`), less the chase's cap under that."""
    step = tick(entry)
    _, limit = offsets(entry, trail, exit_through)
    return ((entry - limit) * (1 - chase_cap)).quantize(step, rounding=ROUND_FLOOR)


def requests_sent() -> int:
    """Every trading-API request this process has sent so far, by the adapter's own count."""
    return sum(count.total for count in request_counts() if count.api == TRADING_API)


PERSISTENCE_FIELDS = ("breach_count", "high_count", "confirm_gap_ms")
"""The persistence rows. With both counts at one the arms are off and no run reads any of the three, so
a trial's key leaves them out and a stored configuration without them decodes as off
(`ledger._strategy_fields`, `tuning_contract.RankedConfiguration.mover_config`): a configuration
recorded before these rows existed keeps its key."""
OWN_FIELDS: dict[str, type | tuple[type, ...]] = {
    "max_notional": Decimal,
    "max_move": Decimal,
    "max_shares": (int, NoneType),
    "share_caps": (dict, NoneType),
    "size_from_budget": bool,
    "load_instruments": bool,
    "entries": (int, NoneType),
    "bar_spec": str,
    "slots": int,
    "watch": int,
    "risk": Decimal,
    "run_bars": int,
    "entry_arm": str,
    "trail": Decimal,
    "trail_mode": str,
    "trail_cap": str,
    "planned_loss": (Decimal, NoneType),
    "loss_share": (Decimal, NoneType),
    "atr_k": Decimal,
    "atr_n": int,
    "spread_floor_m": Decimal,
    "liquidity_tier_cut": Decimal,
    "ratchet_r": Decimal,
    "ratchet_g": Decimal,
    "ratchet_base": str,
    "exit_through": Decimal,
    "chase_cap": Decimal,
    "chase_secs": int,
    "chase": str,
    "breach_count": int,
    "high_count": int,
    "confirm_gap_ms": int,
    "flow_share": Decimal,
    "flow_bars": int,
    "entry_ticks": int,
    "exit_ticks": int,
    "entry_timeout_secs": int,
    "cooldown_secs": int,
    "rank_max_age_secs": int,
    "soft_line": int,
    "protect": bool,
    "trigger": TriggerType,
    "extended_hours": bool,
    "wind_down_at": (datetime, NoneType),
    "feed": (str, NoneType),
}
"""`MoverConfig`'s own fields and the type each takes."""


class MoverConfig(StrategyConfig):
    """Parameters of `Mover`. Every number is a registered row, UNCALIBRATED (ADR 0088).

    `max_notional` (the largest notional of an entry, in dollars) and `max_move` (the no-chase cap on
    the ranking's move, a fraction) have no default: the run states them. The entry is bounded here,
    where it is sized; the startup roster's engine per-order cap is the node's outer bound, a multiple
    of `max_notional`, since that cap holds for a sale as for a buy and no cap shapes or refuses a sale
    (ADR 0088). `max_shares` bounds an entry in shares, and `entries` how many entries a run may fill
    (None: no bound). `share_caps`, when set, bounds each named instrument's entry in shares; a name
    outside that map cannot enter, except under load_instruments with size_from_budget: there any name
    outside the map that is in the cache or loaded on demand may enter (with the data client's
    `load_all`, the whole market), bounded only by the sizing below. It has no start-time cap, no
    position monitor and no engine per-order cap, and none is written for it. Paper windows select size_from_budget=True: whole shares of the
    supplied max_notional at the current entry limit, clamped to the name's start-time share cap
    (`binding` `cap_bound` when it binds); neither that cap nor the name's position monitor is
    rewritten, so a monitor trip means a real fault, not a price drift. This path clamps the startup
    allocation to balance_free read at each entry (a missing free balance refuses the entry), and
    `flow_share` of the last `flow_bars` bars' dollar volume caps the size; it preserves any explicit global max_shares cap without rewriting it, and does not use
    risk sizing. The startup allocation names the initial cash-engine funding limit; later losses can
    reduce free funds. With size_from_budget=False the
    legacy FixedRiskSizer and optional tighter share caps remain for explicit simulations. `bar_spec`
    is the bars' specification and source after the instrument ID, `1-MINUTE-LAST-EXTERNAL` in a replay
    and `1-MINUTE-LAST-INTERNAL` for live entry bars built from trades. `slots` is
    how many names are held at once and `watch` how many of the ranking's top are followed. `risk` is
    `FixedRiskSizer`'s fraction of the balance. `trail`, `exit_through` and `chase_cap` are fractions of
    a price; `flow_share` of the last `flow_bars` bars' dollar volume caps the size. `protect=False` is
    the hold-to-close row: no trailing sell, sold at the wind-down. `wind_down_at` is an aware
    `datetime`. `breach_count` confirms a breach and `high_count` confirms a new high, on trade
    prints only and only with LAST_PRICE. Each defaults to one (off); both off preserve the emulator
    path. `confirm_gap_ms` is the minimum gap between counted prints in either arm (default 100 ms;
    zero admits any strictly newer event time); counts have no expiry. The on row is N=2, G=100 ms
    for both arms, UNCALIBRATED. With either on, `exit_through` shapes planned-stop sizing and
    `exit_ticks` prices the actual sale. Any other keyword must be a base `StrategyConfig` field,
    and each field takes one type.

    `entry_arm` defaults to `registered`, or is `bridge` (the existing run-only rule) or another key
    of ADR 0092's `ARMS`. Registered
    arms take their run length from that table; `run_bars` overrides apply only to the bridge.
    Each registered arm needs an explicit `Day` history context in `Mover.entry_contexts`.

    T1/T2's `TRAIL_SETTINGS` adds selectable window modes; fixed 0.02 and the clock cooldown
    remain the defaults. The existing cooldown and reclaim are unchanged.
    Turn uses k rising closes from a low at least d actual trail distances below the sale.
    VWAP is tape-only: no live bar VWAP is inferred. Re-entry arms, ATR multiples and the spread floor are
    lead, no primary source; every row is UNCALIBRATED until the tape with measured spreads.
    Non-fixed entries scale exit_through to trail/4 and keep chase_cap at max(trail, chase_cap) (the
    lead's (ii), 2026-11-12), with an unavoidable spread floor (`spread_floor_m >= 1`) and 0.02 cap.
    An unusable spread selects fixed by name; no quote still refuses entry. An ATR with fewer than
    `atr_n` complete slot minutes selects fixed as `cold_atr_fallback` (the lead's (ii)). `liquidity_tier_cut`
    is baseline dollars per minute, a stand-in for size; an unknown baseline selects ATR by name.
    `ratchet_base` chooses the paired ATR or tier plan, and r/g tighten it once. All new numeric rows
    and their alternates are UNCALIBRATED. Fixed preserves explicit legacy trail/exit/chase overrides.
    """

    def __init__(
        self,
        *,
        max_notional: Decimal,
        max_move: Decimal,
        max_shares: int | None = None,
        share_caps: dict[InstrumentId, int] | None = None,
        size_from_budget: bool = False,
        load_instruments: bool = False,
        entries: int | None = None,
        bar_spec: str = "1-MINUTE-LAST-EXTERNAL",
        slots: int = 1,
        watch: int = 3,
        risk: Decimal = Decimal("0.01"),
        run_bars: int = 3,
        entry_arm: str = "registered",
        trail: Decimal = Decimal("0.02"),
        trail_mode: str = "fixed",
        trail_cap: str = TRAIL_CAP_DEFAULT,
        planned_loss: Decimal | None = None,
        loss_share: Decimal | None = None,
        atr_k: Decimal = Decimal(1),
        atr_n: int = 14,
        spread_floor_m: Decimal = Decimal(2),
        liquidity_tier_cut: Decimal = Decimal(1_000_000),
        ratchet_r: Decimal = Decimal(2),
        ratchet_g: Decimal = Decimal("0.5"),
        ratchet_base: str = "atr",
        exit_through: Decimal = Decimal("0.005"),
        chase_cap: Decimal = Decimal("0.02"),
        chase_secs: int = 5,
        chase: str = "resend",
        breach_count: int = 1,
        high_count: int = 1,
        confirm_gap_ms: int = 100,
        flow_share: Decimal = Decimal("0.05"),
        flow_bars: int = 5,
        entry_ticks: int = 0,
        exit_ticks: int = 1,
        entry_timeout_secs: int = 30,
        cooldown_secs: int = 300,
        rank_max_age_secs: int = 90,
        soft_line: int = 140,
        protect: bool = True,
        trigger: TriggerType = TriggerType.BID_ASK,
        extended_hours: bool = False,
        wind_down_at: datetime | None = None,
        feed: str | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        fields = {name: value for name, value in locals().items() if name in OWN_FIELDS}
        refuse_another_type(self, fields, OWN_FIELDS)
        for name in ("max_notional", "max_move", "trail", "flow_share"):
            if not fields[name].is_finite() or fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        for name in ("exit_through", "chase_cap"):
            if not fields[name].is_finite() or not 0 <= fields[name] < 1:
                raise ValueError(f"{name} must be a fraction from 0 to under 1, got {fields[name]}")
        for name in ("max_shares", "entries"):
            if fields[name] is not None and fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        if share_caps is not None and any(
            not isinstance(instrument, InstrumentId) or type(shares) is not int or shares < 1
            for instrument, shares in share_caps.items()
        ):
            raise ValueError("share_caps must map instrument IDs to positive whole shares")
        if not risk.is_finite() or not 0 < risk < 1:
            raise ValueError(f"risk must be a fraction over 0 and under 1, got {risk}")
        if trail >= 1:
            raise ValueError(f"trail must be a fraction under 1, got {trail}")
        if trail_mode not in TRAIL_SETTINGS["trail_mode"]["arms"]:
            raise ValueError(f"trail_mode must be one of {TRAIL_SETTINGS['trail_mode']['arms']}")
        if trail_cap not in TRAIL_CAPS:
            raise ValueError(f"trail_cap must be one of {TRAIL_CAPS}, got {trail_cap!r}")
        if planned_loss is not None and (not planned_loss.is_finite() or planned_loss <= 0):
            raise ValueError(f"planned_loss must be a positive amount, got {planned_loss}")
        if loss_share is not None and (not loss_share.is_finite() or not 0 < loss_share < 1):
            raise ValueError(f"loss_share must be a fraction over 0 and under 1, got {loss_share}")
        if planned_loss is not None and loss_share is not None:
            raise ValueError("give planned_loss or loss_share, not both")
        if size_from_budget and planned_loss is None and loss_share is None:
            raise ValueError(
                "planned_loss or loss_share is required with size_from_budget: the loss at the stop "
                "an entry is sized for; paper A and B take PAPER_LOSS_SHARE of the slot's figure, "
                "and anywhere else it is the owner's figure, with no default"
            )
        for name in ("atr_k", "spread_floor_m", "liquidity_tier_cut", "ratchet_r"):
            if not fields[name].is_finite() or fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        if spread_floor_m < 1:
            raise ValueError("spread_floor_m must be at least one quoted spread")
        if not 1 <= atr_n <= 1 << 24:  # rc8 indicators/support/mod.rs MAX_PERIOD
            raise ValueError("atr_n must be from 1 to 16777216 one-minute bars")
        if not ratchet_g.is_finite() or not 0 < ratchet_g < 1:
            raise ValueError("ratchet_g must be a fraction over 0 and under 1")
        if ratchet_base not in TRAIL_SETTINGS["ratchet_base"]["arms"]:
            raise ValueError("ratchet_base must be atr or tier")
        if trail_mode != "fixed" and bar_spec not in (
            "1-MINUTE-LAST-EXTERNAL",
            "1-MINUTE-LAST-INTERNAL",
        ):
            raise ValueError("a non-fixed trail_mode requires 1-MINUTE-LAST bars")
        for name in ("slots", "run_bars", "flow_bars", "chase_secs", "entry_timeout_secs",
                     "rank_max_age_secs", "soft_line", "breach_count", "high_count"):  # fmt: skip
            if fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        for name in ("entry_ticks", "exit_ticks", "cooldown_secs", "confirm_gap_ms"):
            if fields[name] < 0:
                raise ValueError(f"{name} must not be negative, got {fields[name]}")
        if not slots <= watch <= RANK_MOST:
            raise ValueError(f"watch must be from slots ({slots}) to {RANK_MOST}, got {watch}")
        if entry_arm != "bridge":
            from . import early_entry_study

            if entry_arm not in early_entry_study.ARMS:
                raise ValueError(
                    f"entry_arm must be bridge or one of {tuple(early_entry_study.ARMS)}"
                )
            if run_bars != 3:
                raise ValueError("run_bars overrides apply only to entry_arm='bridge'")
            if bar_spec not in ("1-MINUTE-LAST-EXTERNAL", "1-MINUTE-LAST-INTERNAL"):
                raise ValueError("a registered entry_arm requires 1-MINUTE-LAST bars")
        if chase == "modify":
            raise ValueError(
                "chase='modify' is refused: no order is modified until the adapter's replace chain "
                "has passed two outside reviews (ADR 0088); a re-price is chase='resend'"
            )
        if chase not in CHASES:
            raise ValueError(f"chase must be one of {CHASES}, got {chase!r}")
        if trigger not in TRIGGERS:
            raise ValueError(
                f"trigger must be BID_ASK or LAST_PRICE, which the emulator accepts, got {trigger}"
            )
        if (breach_count > 1 or high_count > 1) and trigger != TriggerType.LAST_PRICE:
            raise ValueError(
                "BID_ASK is refused with persistence: breach_count and high_count "
                "require trigger=LAST_PRICE when either is above one"
            )
        if (breach_count > 1 or high_count > 1) and trail_mode != "fixed":
            raise ValueError(
                f"trail_mode={trail_mode!r} is refused with persistence: breach_count and "
                "high_count hold the level at the fixed trail's distance, and a trail plan's "
                "distance, tighten and chase cap are not carried into it (ADR 0119)"
            )
        if (breach_count > 1 or high_count > 1) and size_from_budget:
            raise ValueError(
                "size_from_budget is refused with persistence: breach_count and high_count hold the "
                "level at the fixed trail's distance from the fill, and the loss per share the budget "
                "sized for (S2's L) is not carried into it"
            )
        if (breach_count > 1 or high_count > 1) and not protect:
            raise ValueError(
                "protect=False is refused with persistence: breach_count and high_count confirm "
                "the protective sale, and a hold-to-close row has none"
            )
        if wind_down_at is not None and wind_down_at.tzinfo is None:
            raise ValueError("wind_down_at must be an aware datetime")
        try:
            BarType.from_str(f"X.X-{bar_spec}")
        except ValueError as e:
            raise ValueError(
                f"bar_spec {bar_spec!r} is not <step>-<aggregation>-<price>-<source>: {e}"
            ) from e
        self.max_notional = max_notional
        self.max_move = max_move
        self.max_shares = max_shares
        self.share_caps = None if share_caps is None else dict(share_caps)
        self.size_from_budget = size_from_budget
        self.load_instruments = load_instruments
        self.entries = entries
        self.bar_spec = bar_spec
        self.slots = slots
        self.watch = watch
        self.risk = risk
        self.entry_arm = entry_arm
        self.run_bars = run_bars
        if entry_arm != "bridge":
            self.run_bars = early_entry_study.ARMS[entry_arm].run_bars
        self.trail = trail
        self.trail_mode = trail_mode
        self.trail_cap = trail_cap
        self.planned_loss = planned_loss
        self.loss_share = loss_share
        self.atr_k, self.atr_n = atr_k, atr_n
        self.spread_floor_m = spread_floor_m
        self.liquidity_tier_cut = liquidity_tier_cut
        self.ratchet_r, self.ratchet_g = ratchet_r, ratchet_g
        self.ratchet_base = ratchet_base
        self.exit_through = exit_through
        self.chase_cap = chase_cap
        self.chase_secs = chase_secs
        self.chase = chase
        self.breach_count = breach_count
        self.high_count = high_count
        self.confirm_gap_ms = confirm_gap_ms
        self.flow_share = flow_share
        self.flow_bars = flow_bars
        self.entry_ticks = entry_ticks
        self.exit_ticks = exit_ticks
        self.entry_timeout_secs = entry_timeout_secs
        self.cooldown_secs = cooldown_secs
        self.rank_max_age_secs = rank_max_age_secs
        self.soft_line = soft_line
        self.protect = protect
        self.trigger = trigger
        self.extended_hours = extended_hours
        self.wind_down_at = wind_down_at
        self.feed = feed

    @property
    def persistence(self) -> bool:
        """Whether either persistence arm is on: the strategy then holds the stop itself."""
        return self.breach_count > 1 or self.high_count > 1


@dataclass(frozen=True)
class TrailPlan:
    """The fractions selected at entry, shared by risk sizing, protection and its chase floor."""

    trail: Decimal
    exit_through: Decimal
    chase_cap: Decimal
    atr: Decimal | None
    spread: Decimal | None
    bound: str
    note: str | None = None
    source: str | None = None
    """Where the trail came from when that is not its bound: `cold_atr_fallback` (the lead's (ii))."""
    floor: Decimal | None = None
    """The spread floor as a fraction of the entry, `spread_floor_m` quoted spreads (a row of its own)."""
    measure: Decimal | None = None
    """The name's own distance before the floor and the cap: `atr_k` ATRs, or the tier's fraction."""


@dataclass
class ExitRecord:
    """One holding across every replacement sale; event clocks and bid marks, never timer estimates.

    Slippage is positive for a worse fill: trigger bid less fill VWAP, divided by the trigger bid
    or multiplied by sold shares. Peak giveback uses the highest observed held bid, for sold shares.
    A still-open holding has no final-fill clock; amounts describe only the shares already sold.
    """

    instrument_id: InstrumentId
    entry_client_order_id: str | None
    quantity: Decimal
    filled: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    bought_notional: Decimal = Decimal(0)
    trail_distance: Decimal | None = None
    peak: Decimal | None = None
    trigger_ns: int | None = None
    trigger_price: Decimal | None = None
    trigger_reason: str | None = None
    final_fill_ns: int | None = None
    wind_down_wait_ns: int | None = None
    """From the decision to sell to the wind-down, for a sale still unfilled when it began: a gap deeper
    than the chase floor rests until the wind-down's floorless sale (the coordinator, 2026-11-12)."""
    floor_wait_ns: int | None = None
    """How long the sale rested at its chase floor before the floor was released, if it was."""

    def record(self) -> dict[str, Any]:
        vwap = self.notional / self.filled if self.filled else None
        slip = None if vwap is None or self.trigger_price is None else self.trigger_price - vwap
        giveback = None if vwap is None or self.peak is None else max(Decimal(0), self.peak - vwap)
        return {
            "instrument_id": str(self.instrument_id),
            "entry_client_order_id": self.entry_client_order_id,
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled),
            "trail_distance": None if self.trail_distance is None else str(self.trail_distance),
            "peak_price": None if self.peak is None else str(self.peak),
            "trigger_ns": self.trigger_ns,
            "trigger_price": None if self.trigger_price is None else str(self.trigger_price),
            "trigger_reason": self.trigger_reason,
            "final_fill_ns": self.final_fill_ns,
            "floor_released": self.floor_wait_ns is not None,
            "floor_wait_secs": None if self.floor_wait_ns is None else self.floor_wait_ns / 1e9,
            "waited_for_wind_down": self.wind_down_wait_ns is not None,
            "wind_down_wait_minutes": (
                None if self.wind_down_wait_ns is None else self.wind_down_wait_ns / 60_000_000_000
            ),
            "trigger_to_final_fill_ms": (
                None
                if self.trigger_ns is None or self.final_fill_ns is None
                else (self.final_fill_ns - self.trigger_ns) / 1_000_000
            ),
            "fill_vwap": None if vwap is None else str(vwap),
            "slip_fraction": (
                None
                if slip is None or self.trigger_price is None or self.trigger_price <= 0
                else str(slip / self.trigger_price)
            ),
            "slip_beyond_trail_pct": (
                None
                if slip is None or self.trigger_price is None or self.trigger_price <= 0
                else str(slip / self.trigger_price * 100)
            ),
            "slip_dollars": None if slip is None else str(slip * self.filled),
            "peak_giveback_dollars": None if giveback is None else str(giveback * self.filled),
        }


def baseline_per_minute(record: dict[str, Any]) -> Decimal | None:
    """The startup ranking's existing normal / clock minutes, without another history read.

    `movers_stream.rank` defines dvol_ratio = dvol / normal; `normal` is cumulative across the
    recorded elapsed minutes. Missing inputs are unknown size, so tier uses the ATR arm by name.
    """
    inputs = record.get("inputs") or {}
    sample = record.get("sample") or {}  # null, as `session_window.ranked_table` reads it
    elapsed = sample.get("elapsed_minutes", record.get("elapsed_minutes"))
    values = (inputs.get("dvol"), inputs.get("dvol_ratio"), elapsed)
    if any(v is None for v in values):
        return None
    dollars, ratio, minutes = (Decimal(str(v)) for v in values)
    if any(not v.is_finite() or v <= 0 for v in (dollars, ratio, minutes)):
        return None
    return dollars / ratio / minutes


def quoted_spread(quote: Any) -> Decimal | None:
    """The latest ask minus a positive bid; crossed, locked or absent bids are no usable spread."""
    bid_price = getattr(quote, "bid_price", None)
    if bid_price is None:
        return None
    bid, ask = bid_price.as_decimal(), quote.ask_price.as_decimal()
    return ask - bid if 0 < bid < ask else None


def ratchet_trail(
    initial: TrailPlan,
    bought: Decimal,
    followed: Decimal,
    spread: Decimal,
    settings: MoverConfig,
) -> TrailPlan | None:
    """The lead's T1 ratchet rule, paired with ATR or tier; no widening or spread-floor exemption.
    A plan that fell back to fixed (cold ATR or unusable spread) keeps its configured offsets."""
    if initial.bound == "fixed" or initial.source == "cold_atr_fallback":
        return None
    if followed - bought < settings.ratchet_r * initial.trail * bought:
        return None
    floor = settings.spread_floor_m * spread / bought
    tightened = max(initial.trail * settings.ratchet_g, floor)
    if tightened >= initial.trail:
        return None
    return replace(
        initial,
        trail=tightened,
        exit_through=tightened / 4,
        chase_cap=max(tightened, settings.chase_cap),
        spread=spread,
        bound="spread floor" if floor >= tightened else "ratchet",
    )


@dataclass
class InstrumentLoad:
    """One native instrument request; a timed-out request retains its budget reserve until answered."""

    requested_ns: int
    request_id: str | None = None
    completed: bool = False
    ready: bool = False
    reason: str | None = None


@dataclass
class Slot:
    """What the strategy keeps for one watched name. Plain values: the cache holds the orders."""

    instrument_id: InstrumentId
    bar_type: BarType
    closes: deque[Decimal]
    dollars: deque[Decimal]
    atr: AverageTrueRange | None = None
    trail_plan: TrailPlan | None = None
    initial_trail: TrailPlan | None = None
    ratcheted: bool = False
    entry_record: dict[str, Any] | None = None
    exit_record: ExitRecord | None = None
    last_bid: Decimal | None = None
    protection_anchor: Decimal | None = None
    held_stop: Decimal | None = None
    """With no followed price, the level a held stop sells through: the average fill, else the entry's
    limit, less the trail distance in force. Checked each step on the last trade, else the minute close."""
    held_since: int | None = None
    """When the held stop began: the entry's first fill, else the step that found no followed price."""
    floor_since: int | None = None
    """When the sale was first seen resting at its floor with the bid through it."""
    floor_released: bool = False
    """The floor was dropped after `FLOOR_WAIT_SECS`: later sales of this holding follow the bid."""
    trailed: bool = False
    """An emulated trailing sell has been placed for this holding: a later hold counts from its own start."""
    last_sale: Decimal | None = None
    """The price of this holding's last plain sale: a remake with no bid is never priced above it."""
    run_ns: int | None = None
    """When the last rising run completed (the bar's `ts_event`), until an entry has been decided on."""
    run_close: Decimal | None = None
    run_seen_ns: int | None = None
    """When the node had the bar that completed the run (its `ts_init`): the qualifying market event as
    the strategy can know it, from which an entry's delay is measured (`mover_window.entry_diagnostics`)."""
    entry_id: ClientOrderId | None = None
    entry_cancel_ns: int = 0
    protect_id: ClientOrderId | None = None
    """The one sell of this name: the trailing stop-limit, then the limit it became, or a plain limit."""
    exiting: bool = False
    """The trigger has been met, or the wind-down has begun: from here every sell is a plain limit."""
    floor: Decimal | None = None
    """The lowest price this position's sale is chased to (`chase_floor`), from its first released price
    or persistence confirmation reference. A sale that is refused or re-sent keeps it."""
    trail_trigger: Decimal | None = None
    """The trigger a trailing sell had reached when it was canceled to be made again: the next one starts
    from it, so the trigger never falls; with persistence on, the local confirmed trigger itself."""
    trail_high: Decimal | None = None
    """The confirmed high with persistence on; the emulator owns it when both arms are off."""
    observations: deque[tuple[int, Decimal]] = field(default_factory=deque)
    """Trade events delivered since the last step, stored without reading engine attributes."""
    high_observations: deque[tuple[int, Decimal]] = field(default_factory=deque)
    breach_observations: deque[int] = field(default_factory=deque)
    breach_price: Decimal | None = None
    """The confirming print's price: a later recovery cannot lift the position's chase floor."""
    observed_ns: int = 0
    """Only strictly newer print times are read; duplicates, older prints and timers never count."""
    sized_loss: Decimal | None = None
    """L, the loss per share the entry was sized for (`sizing.plan_for_loss`): the planned entry less its
    stepped stop, on Alpaca's step. The trailing sell holds its trigger L under the fill, then L under the
    highest price since, so the distance sized and the distance sent are one value (the lead's S2
    property of 2026-11-12, 17:21)."""
    exit_trigger: Decimal | None = None
    """The trigger this exit was decided at: the one the emulator released the trailing sell at, the one
    already met when a limit sold for it, or the confirmed level a persistence breach crossed. Every sale
    sent for the exit, re-sent or chased, records it beside its own client order ID (`sell_triggers`)."""
    priced_ns: int = 0
    was_held: bool = False
    entry_start: int | None = None
    """First minute watched by a registered arm; history never warms its rising run."""
    entry_day: date | None = None
    """The watch-start minute belongs to this New York day, and resets with a new day."""
    subscribed_ns: int = 0
    """When the slot started receiving bars; its first internal minute may be partial."""


class Mover(Strategy):
    """Hold up to `slots` of the ranked names, each bought on its first rising run and sold by a trailing
    limit, as the module header describes. It makes no claim to an edge: the gate measures that."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.settings = config
        self.persistence = config.persistence
        self.spec_suffix = config.bar_spec
        self.submit_params = dict(EXTENDED_HOURS) if config.extended_hours else None
        self.feed_params = {"feed": config.feed} if config.feed else None
        self.wind_down_at = config.wind_down_at
        self.manage_stop_set = config.manage_stop
        self.slots: dict[InstrumentId, Slot] = {}
        self.opened_slots: set[InstrumentId] = set()
        """Every slot subscribed during the run, including names since unwatched, for stream checks."""
        self.rank: list[tuple[InstrumentId, Decimal]] = []
        """The latest ranking, best first; empty until one arrives and after a malformed one."""
        self.instrument_loads: dict[InstrumentId, InstrumentLoad] = {}
        self.instrument_responses: dict[str, str | None] = {}
        self.instrument_requests = 0
        self.instrument_sent: deque[int] = deque()
        self.rank_ns = 0
        self.rank_faults = 0
        self.rank_malformed = False
        """Whether the last ranking heard failed to parse; the ranking is stale until one parses."""
        self.rankings: list[dict[str, Any]] = []
        """Every valid published generation, its value and its event/receipt clocks, for the window record."""
        self.ranking_records: list[dict[str, object]] = []
        """Every ranking actor tick's result, including why it published nothing; supplied by composition."""
        self.position_caps: dict[InstrumentId, PositionCap] = {}
        self.rank_said: tuple[int, str, int] | None = None
        """The ranking the log last named: its time, its value and the malformed ones counted then."""
        self.released: set[str] = set()
        """Client order IDs the emulator has released, as `on_order_released` saw them."""
        self.sell_triggers: dict[str, str] = {}
        """Each sell's last trigger by client order ID: the trigger a trailing sell was sent with or had
        reached when the emulator released it, or the trigger already met that a limit sell was sent
        for. With `fills` it is how far each exit's fill fell past its trigger, the loss over L a gap
        made (the lead's ruling of 2026-11-12 17:43); it is the record's figure, nothing measures it
        here. The emulator releases a triggered sell as a limit
        with no trigger (`crates/execution/src/order_emulator/emulator.rs:1398-1402`), its release
        names the bid (`:1424-1427`), and its trailing moves go to the risk engine, not the strategy
        (`:1657-1686`, `:1823-1827`), so the step reads the trigger from the released order's events,
        which the transform keeps (`:1404`)."""
        self.sell_triggers_read: set[str] = set()
        """Released orders already read: a released sell's trigger is final at its release."""
        self.fills: list[tuple[str, str, str, str, int]] = []
        """Each fill as its event gave it: client order ID, side, quantity, price, time. The session's
        reflection reads a sale's counterfactuals (held on, flat, managed) from these and the tape."""
        self.unreported: list[tuple[int, Decimal]] = []
        """Each buy filled on a reported account, asked for and not yet in a report: the account's
        event count that has it, and its notional (ultra review of the composition, finding 3)."""
        self.unasked: list[Decimal] = []
        """Each buy filled on a reported account while a query was in flight: its notional, until a
        query sent after it."""
        self.unasked_venue: Any = None
        """The venue of a reported account with a fill no query has been sent for yet, or None."""
        self.query_venue: Any = None
        """The venue of the reported account the last query was sent to, or None before the first."""
        self.fills_to_report: list[tuple[Any, OrderSide, Decimal]] = []
        """Each fill since the last step: its venue, side and notional (`on_order_filled`)."""
        self.query_awaits: int | None = None
        """The event count the account query in flight brings the account to, or None."""
        self.query_sent_ns = 0
        """When the account query in flight was sent, on the node's clock."""
        self.exits: list[ExitRecord] = []
        self.protection_fallbacks = {"position average price": 0, "current bid": 0}
        self.protect_paths = {"held_stop": 0, "unpriced_exit": 0, "trail": 0}
        """How each held stop ended: sold through its level, sold after `HOLD_MOST_SECS`, or released."""
        self.refused: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        """Why each candidate entry was not sent, for the run's report."""
        self.sizing_refusals: dict[str, int] = dict.fromkeys(
            (*SIZING_REFUSALS, DISTANCE_UNDER_STEP, BID_AT_STOP), 0
        )
        """Each `sizing.plan_for_loss` refusal, counted by its own name (the lead's S2 requirement)."""
        self.sizing_bindings: dict[str, int] = {}
        """Each bound that held a sized entry (`held_by`), counted: the budget binding is counted apart."""
        self.quotes = 0
        self.trades = 0
        self.live_bars = 0
        self.history_bars = 0
        self.history_dropped = 0
        """It requests no history: a window's report reads these of every strategy."""
        self.entry_contexts: dict[InstrumentId, entry_study.Day] = {}
        """Explicit per-name ADR 0092 history, supplied by composition before registered-arm entry.

        A Day names its New York date, reference close, earlier sessions latest first, and covered
        bars since 04:00 (the study's Day contract). Callers supply complete history, with `missing`
        naming any coverage fault; Mover fetches no history and refuses absent, wrong-name/day or
        missing contexts. `covered_to` must reach the incoming minute's open: after a data gap the
        caller supplies the missing prefix or marks it missing. Completed live bars extend coverage
        to that minute's end and replace that minute in the context; future minutes
        are discarded, and pre-watch history informs listing/extension only, never the rising run.
        Context survives unwatching so the listing clock is the day's, not a new slot's.
        """
        self.sold: dict[InstrumentId, tuple[int, Decimal | None]] = {}
        """When each name was last sold, and at what price: the name's, until its next sale, so that
        neither a slot closed and opened again nor an entry that never filled loses its sale state."""
        self.baseline_dollar_volume_per_minute: dict[InstrumentId, Decimal] = {}
        """The ranking's startup normal, supplied by composition: a stand-in for size, not market cap."""
        self.notes: dict[InstrumentId, list[str]] = {}
        """What was noted of each name, for the run's report; kept when its slot is closed."""
        self.entries_sent: list[dict[str, Any]] = []
        """Each entry sent, as plain values: its client order ID, its instrument, when the node had the
        bar that qualified it (`qualified_ns`) and that bar's close, and the order's creation time
        (`submitted_ns`), both on the node's clock. A window reads its diagnostics from these and its
        own bars (`mover_window.entry_diagnostics`)."""
        self.winding_down = False
        self.sent: deque[tuple[int, int]] = deque()
        """(time, the adapter's request total) at each step of the last 60 seconds."""
        self.recent = 0
        """The adapter's requests over the last 60 seconds plus outstanding instrument-GET reserves,
        which `soft_line` holds entries, instrument requests and chases against."""

    # --- handlers that only store what they are given (module header, "Order events") ---------------

    def on_signal(self, signal: Signal) -> None:
        if signal.name != RANK_SIGNAL:
            return
        # Upstream's `publish_signal` takes a value of any type (`value: typing.Any`); the shared
        # contract is text, so anything else is a fault like a malformed text, never an error.
        try:
            if not isinstance(signal.value, str):
                raise TypeError(f"a ranking is text, not {type(signal.value).__name__}")
            self.rank = [
                (entry.instrument_id, Decimal(str(entry.move)))
                for entry in decode_ranking(signal.value)
            ]
        except ValueError, TypeError:
            self.rank = []
            self.rank_faults += 1
            self.rank_malformed = True
            return
        self.rank_ns = signal.ts_event
        self.rank_malformed = False
        self.rankings.append(
            {
                "value": format_rank(self.rank),
                "computed_ns": signal.ts_event,
                "received_ns": signal.ts_init,
            }
        )

    def on_order_released(self, event: Any) -> None:
        self.released.add(event.client_order_id.value)
        slot = self.slots.get(event.instrument_id)
        if slot is not None and slot.protect_id == event.client_order_id:
            self._mark_exit(
                slot, event.ts_event, event.released_price.as_decimal(), "trailing stop"
            )

    def on_data_response(self, response: Any) -> None:
        """Only retain the response; the next step checks the cache and subscribes outside reentry."""
        if response.failure_reason is not None or response.completed:
            self.instrument_responses[str(response.correlation_id)] = response.failure_reason

    def on_order_filled(self, event: Any) -> None:
        if (
            event.order_side == OrderSide.SELL
            and event.client_order_id.value not in self.sell_triggers
        ):
            # a sale the Mover did not send, upstream's market exit at the wind-down or the managed
            # stop (`crates/trading/src/strategy/mod.rs:1131`, `:1749`), gets the exit's trigger here,
            # from plain attributes, so a gapped exit closed that way is still in the record
            slot = self.slots.get(event.instrument_id)
            if slot is not None and slot.exit_trigger is not None:
                self.sell_triggers[event.client_order_id.value] = str(slot.exit_trigger)
        self.fills.append(
            (
                event.client_order_id.value,
                event.order_side.name,
                str(event.last_qty),
                str(event.last_px),
                event.ts_event,
            )
        )
        # plain attributes only: the handler can run while the strategy is borrowed (see the class
        # note), so the account is asked for at the next step or before the next buy is sized
        self.fills_to_report.append(
            (
                event.instrument_id.venue,
                event.order_side,
                event.last_qty.as_decimal() * event.last_px.as_decimal(),
            )
        )

        slot = self.slots.get(event.instrument_id)
        if slot is None:
            return
        quantity = event.last_qty.as_decimal()
        if event.order_side == OrderSide.BUY:
            row = (
                self._ensure_exit(slot, Decimal(0))
                if slot.exit_record is None
                else slot.exit_record
            )
            row.quantity += quantity
            row.bought_notional += quantity * event.last_px.as_decimal()
            row.final_fill_ns = None
            plan = slot.trail_plan
            row.trail_distance = offsets(
                row.bought_notional / row.quantity,
                self.settings.trail if plan is None else plan.trail,
                self.settings.exit_through if plan is None else plan.exit_through,
            )[0]
            if slot.last_bid is not None:
                row.peak = max(row.peak or slot.last_bid, slot.last_bid)
        elif slot.exit_record is not None:
            row = slot.exit_record
            row.filled += quantity
            row.notional += quantity * event.last_px.as_decimal()
            if row.filled >= row.quantity:
                row.final_fill_ns = event.ts_event

    def _trading_account(self, venue: Any) -> Any:
        """The venue's account as the portfolio holds it."""
        return self.portfolio.account(venue)

    def _now_ns(self) -> int:
        return self.clock.timestamp_ns()

    def _ask_for_the_account(self) -> None:
        """Ask for a reported account after fills, one query in flight at a time, before any later buy
        is sized: the adapter's `_query_account`, one GET /account, through upstream's
        `Strategy.query_account` (the lead's rulings of 2026-11-12 on the ultra review of the
        composition, finding 3, and on the high round's findings 4 and 5). A calculated account has
        every fill already and is not asked.

        A query is sent for every fill since the last one, so a step's partial fills cost one request,
        not one each; while it is unanswered no other is sent, so its report, the account's next
        event, is taken after every buy it was sent for. A buy filled while it is in flight waits for
        the next query. One unanswered for `QUERY_WAIT_SECS` is taken as lost and another sent, with
        or without a fill since (the second read of the high round). A buy's notional is subtracted from
        the free cash until a report has it; sales never wait on a query.
        """
        taken, self.fills_to_report = self.fills_to_report, []
        for venue, side, notional in taken:
            account = self._trading_account(venue)
            if account is None or account.calculate_account_state:
                continue
            if side == OrderSide.BUY:
                self.unasked.append(notional)
            self.unasked_venue = venue
        venue = self.unasked_venue or self.query_venue
        if venue is None:
            return
        account = self._trading_account(venue)
        if account is None:
            return
        now = self._now_ns()
        in_flight = self._in_flight(account)
        if self.unasked_venue is None and not in_flight:
            return  # the last query was answered and no fill has come since
        if in_flight and now - self.query_sent_ns < QUERY_WAIT_SECS * SECOND_NS:
            return
        self.query_awaits, self.query_sent_ns = account.event_count + 1, now
        self.query_venue = venue
        self.unreported += [(self.query_awaits, notional) for notional in self.unasked]
        self.unasked, self.unasked_venue = [], None
        self.query_account(account.id)

    def _in_flight(self, account: Any) -> bool:
        """Whether the last account query sent is still unanswered: the account has had no report since."""
        return self.query_awaits is not None and account.event_count < self.query_awaits

    def _unreported_buys(self, account: Any) -> Decimal:
        """The buys filled that the reported account does not have yet, dropping those it has."""
        self.unreported = [(n, v) for n, v in self.unreported if account.event_count < n]
        waiting = sum(self.unasked, Decimal(0))
        return sum((v for _, v in self.unreported), waiting)

    def _query_wait_secs(self, account: Any) -> float:
        """How long the account query in flight has gone unanswered, in seconds; 0 with none: what a
        buy sized now holds back on it (the lead's ruling of 2026-11-12)."""
        if not self._in_flight(account):
            return 0.0
        return max(0, self._now_ns() - self.query_sent_ns) / SECOND_NS

    def on_order_denied(self, event: Any) -> None:
        self.refused.append((event.client_order_id.value, str(event.reason)))

    def on_order_rejected(self, event: Any) -> None:
        self.refused.append((event.client_order_id.value, str(event.reason)))

    def on_quote(self, quote: Any) -> None:
        self.quotes += 1
        slot = self.slots.get(quote.instrument_id)
        if slot is not None:
            bid = quote.bid_price.as_decimal()
            if bid > 0:  # a one-sided quote's zero bid is no price: keep the last real one
                slot.last_bid = bid
            row = slot.exit_record
            if bid > 0 and row is not None and row.filled < row.quantity:
                row.peak = max(row.peak or bid, bid)

    def on_trade(self, trade: Any) -> None:
        self.trades += 1
        if self.persistence:
            slot = self.slots.get(trade.instrument_id)
            if slot is not None and slot.trail_trigger is not None and not slot.exiting:
                # No print happened after it was received: one stamped ahead of its receipt is read
                # at its receipt, so it cannot hide the prints that follow it behind the watermark.
                stamp = min(int(trade.ts_event), int(trade.ts_init))
                slot.observations.append((stamp, trade.price.as_decimal()))

    def record(self) -> dict[str, Any]:
        """What a run's report keeps of the strategy, in plain values: its rows, the ranking it last
        had, each fill, each order refused, the orders the emulator released, and each slot's notes."""
        return {
            "rows": {
                name: str(getattr(self.settings, name))
                for name in OWN_FIELDS
                if name != "max_shares" or not self.settings.size_from_budget
            },
            "persistence_arms": {
                arm: {
                    "state": "on" if count > 1 else "off",
                    "count": count,
                    "gap_ms": self.settings.confirm_gap_ms,
                }
                for arm, count in (
                    ("breach", self.settings.breach_count),
                    ("high", self.settings.high_count),
                )
            },
            "rank": format_rank(self.rank),
            "rank_ns": self.rank_ns,
            "rankings": [dict(ranking) for ranking in self.rankings],
            "ranking_records": [dict(record) for record in self.ranking_records],
            "opened_slots": sorted(str(instrument) for instrument in self.opened_slots),
            "instrument_loads": {
                str(instrument): {
                    "request_id": load.request_id,
                    "requested_ns": load.requested_ns,
                    "completed": load.completed,
                    "ready": load.ready,
                    "reason": load.reason,
                    "engine_cap": None,
                }
                for instrument, load in self.instrument_loads.items()
            },
            "instrument_requests": self.instrument_requests,
            "instrument_request_limit": INSTRUMENT_REQUEST_LIMIT,
            "instrument_request_reserve": INSTRUMENT_REQUEST_RESERVE,
            "dropped": list(self.dropped),
            "sizing_refusals": dict(self.sizing_refusals),
            "sizing_bindings": dict(self.sizing_bindings),
            "rank_faults": self.rank_faults,
            "fills": [list(fill) for fill in self.fills],
            "refused": [list(refused) for refused in self.refused],
            "released": sorted(self.released),
            "protection_fallbacks": dict(self.protection_fallbacks),
            "protect_paths": dict(self.protect_paths),
            "sell_triggers": dict(self.sell_triggers),
            "trail_notes": [PAPER_TRAIL_LIMIT, TRAIL_SOURCE_LIMIT, SHIPPED_ATR_NOTE],
            "exits": [row.record() for row in self.exits],
            "notes": {str(i): list(notes) for i, notes in self.notes.items()},
            "entries": [dict(entry) for entry in self.entries_sent],
            "entry_contexts": {
                str(instrument): {
                    "day": context.day.isoformat(),
                    "bars": len(context.bars),
                    "covered_to": context.covered_to,
                    "missing": context.missing,
                }
                for instrument, context in self.entry_contexts.items()
            },
        }

    # --- start, bars, the step -------------------------------------------------------------------------

    def on_start(self) -> None:
        self.subscribe_signal(RANK_SIGNAL)
        self.clock.set_timer(STEP_TIMER, timedelta(seconds=STEP_SECS), callback=self._on_step)
        if self.wind_down_at is not None:
            self.clock.set_time_alert(
                WIND_DOWN_ALERT, self.wind_down_at, self._wind_down, allow_past=True
            )

    def on_bar(self, bar: Bar) -> None:
        """Keep the bar's close and dollar volume; note a rising run that this bar completes."""
        slot = self.slots.get(bar.bar_type.instrument_id)
        if slot is None or bar.bar_type != slot.bar_type:
            return
        # rc8 internal minute bars stamp their close at ts_init. The first one can begin before
        # subscription; neither entry arm may count its partial close, volume or live-bar total.
        if (
            bar.bar_type.is_internally_aggregated()
            and bar.ts_init - 60 * SECOND_NS < slot.subscribed_ns
        ):
            self._note(slot, f"{slot.instrument_id}: skipping the first partial INTERNAL minute")
            return
        self.live_bars += 1
        if slot.atr is not None:
            slot.atr.handle_bar(bar)
        close = bar.close.as_decimal()
        before = slot.closes[-1] if slot.closes else None
        slot.closes.append(close)
        slot.dollars.append(close * bar.volume.as_decimal())
        if self.settings.entry_arm != "bridge":
            self._registered_run(slot, bar)
            return
        rising = 0  # the closes in a row, to this one, each above the one before
        for later, earlier in pairwise(reversed(slot.closes)):
            if not later > earlier:
                break
            rising += 1
        wanted = self.settings.run_bars
        if rising >= wanted:
            slot.run_ns, slot.run_close, slot.run_seen_ns = bar.ts_event, close, bar.ts_init
            self._say(f"{slot.instrument_id}: run qualified, {wanted} rising closes to {close}")
        elif before is not None and rising == 0:
            self._say(f"{slot.instrument_id}: run restarted, {close} is not above {before}")
        else:
            after = "" if before is None else f" after {before}"
            self._say(
                f"{slot.instrument_id}: run building, {rising} of {wanted} rising closes "
                f"({close}{after})"
            )

    def _registered_run(self, slot: Slot, bar: Bar) -> None:
        """Use the study's listing clock, rising run and extension rule on this bar's known prefix."""
        from . import early_entry_study as entry_study

        slot.run_ns = slot.run_close = slot.run_seen_ns = None
        arm = self.settings.entry_arm
        rows = entry_study.ARMS[arm]
        # At the pin internal time bars' ts_init is the close regardless of ts_event configuration
        # (aggregation.rs:1735); adapter EXTERNAL bars' ts_event is their open (parse_bar).
        opened_ns = (
            bar.ts_init - entry_study.MINUTE_NS
            if bar.bar_type.is_internally_aggregated()
            else bar.ts_event
        )
        opened = datetime.fromtimestamp(opened_ns / SECOND_NS, entry_study.NEW_YORK)
        minute = opened.hour * 60 + opened.minute
        context = self.entry_contexts.get(slot.instrument_id)
        if bar.bar_type.is_internally_aggregated() and opened_ns < slot.subscribed_ns:
            # History owns this minute if it exists. An INTERNAL bar contains only trades received
            # since this subscription, so it must not overwrite a complete history bar or grant
            # coverage for the missing part of its minute.
            self._note(slot, f"{slot.instrument_id}: skipping the first partial INTERNAL minute")
            return
        if slot.entry_day != opened.date():
            slot.entry_day = opened.date()
            slot.entry_start = minute
        why = None
        if context is None:
            why = "no entry context supplied"
        elif context.symbol != slot.instrument_id.symbol.value or context.day != opened.date():
            why = "entry context names another symbol or New York day"
        elif context.missing is not None:
            why = f"entry context missing: {context.missing}"
        elif not entry_study.PART_START <= minute < entry_study.WINDOW_END:
            why = "outside the entry study's 04:00-16:00 New York window"
        elif context.covered_to < minute:
            why = "entry context coverage does not reach this minute's open"
        if why is not None:
            self._note(slot, f"{slot.instrument_id}: entry arm {arm}: {why}")
            return
        assert context is not None
        assert slot.entry_start is not None
        known = {b.start: b for b in context.bars if b.start <= minute}
        known[minute] = entry_study.Minute(
            minute,
            float(bar.open.as_decimal()),
            float(bar.high.as_decimal()),
            float(bar.low.as_decimal()),
            float(bar.close.as_decimal()),
            float(bar.volume.as_decimal()),
        )
        day = replace(context, bars=tuple(known[m] for m in sorted(known)), covered_to=minute + 1)
        self.entry_contexts[slot.instrument_id] = day
        seen = entry_study.listing(day, rows)
        if not seen[-1].listed:
            ratio = "unknown" if seen[-1].ratio is None else f"{seen[-1].ratio:.3g}"
            self._note(
                slot,
                f"{slot.instrument_id}: entry arm {arm}: not listed "
                f"(move {seen[-1].move:.1%}, volume ratio {ratio})",
            )
            return
        for index, run, listed_at in entry_study.entry_runs(day, rows, start=slot.entry_start):
            if index != len(day.bars) - 1:
                continue
            why = entry_study.entry_refusal(day, rows, index, run, listed_at)
            if why is not None:
                self._note(slot, f"{slot.instrument_id}: entry arm {arm}: {why}")
                return
            slot.run_ns, slot.run_close, slot.run_seen_ns = (
                bar.ts_event,
                bar.close.as_decimal(),
                bar.ts_init,
            )
            self._say(f"{slot.instrument_id}: entry arm {arm}: run qualified at {slot.run_close}")

    def _wind_down(self, event: TimeEvent) -> None:  # the alert only marks it; the step acts
        self.winding_down = True
        for slot in self.slots.values():
            row = slot.exit_record
            if row is not None and row.trigger_ns is not None and row.final_fill_ns is None:
                row.wind_down_wait_ns = event.ts_event - row.trigger_ns

    def _say(self, text: str) -> None:
        """One line in the node's log, from which a run's record reads which rule decided. Called from
        the step and from `on_bar` only: a handler of an order event or a signal never logs (module
        header, "Order events")."""
        self.log.info(text)

    def _say_ranking(self, now: int) -> None:
        """The ranking, once each time it arrives new or changed, with its age; a malformed one, once."""
        heard = (self.rank_ns, format_rank(self.rank), self.rank_faults)
        said = self.rank_said or (0, "", 0)
        if heard == said:
            return
        if heard[2] > said[2]:
            self._say(f"Ranking: malformed ({heard[2]} so far); no entry until one parses")
        if heard[0] and heard[:2] != said[:2]:
            age = (now - heard[0]) / SECOND_NS
            self._say(f"Ranking: {heard[1] or 'no name'}; computed {age:.0f} s ago")
        self.rank_said = heard

    def _on_step(self, event: TimeEvent) -> None:
        now = event.ts_event
        self._ask_for_the_account()
        self._say_ranking(now)
        recent = self.recent = self._recent_requests(now)
        self._watch(now)
        # Reserve every possible retry while the asynchronous GET is outstanding. Its actual HTTP
        # attempts already enter requests_sent through the provider's existing transport, not here.
        self.recent = recent + self._instrument_reserve()
        self._read_released_triggers()
        for slot in list(self.slots.values()):
            self._manage(slot, now)
        if self.winding_down:
            self._end()
        else:
            self._enter(now, self.recent)

    def on_stop(self) -> None:
        """Cancel what works; what is then still held is the window's to find and report. With
        `manage_stop` upstream's market exit has already run before this is called."""
        if self.clock.timer_names().count(STEP_TIMER):
            self.clock.cancel_timer(STEP_TIMER)
        self._read_released_triggers()
        if self.manage_stop_set:
            return
        for slot in self.slots.values():
            if self._working(slot):
                self.cancel_all_orders(slot.instrument_id)

    # --- reading the cache ---------------------------------------------------------------------------

    def _read_released_triggers(self) -> None:
        """Keep, for each sell the emulator released, the last trigger its events carry: the one it was
        released at, final then, so each is read once, in the step, since an order-event handler here
        reads no cache. The exit it decides keeps that trigger for every sale sent after it."""
        for cid in sorted(self.released - self.sell_triggers_read):
            self.sell_triggers_read.add(cid)
            order = self._order(ClientOrderId(cid))
            if order is None or order.side != OrderSide.SELL:
                continue
            trigger = None
            for event in order.events():
                price = getattr(event, "trigger_price", None)
                if price is not None:
                    trigger = price
            if trigger is None:
                continue
            self.sell_triggers[cid] = str(trigger)
            for slot in self.slots.values():
                if slot.protect_id is not None and slot.protect_id.value == cid:
                    slot.exit_trigger = Decimal(str(trigger))

    def _order(self, client_order_id: ClientOrderId | None) -> Any | None:
        """The cached order (one seam, so a test can stand in for the cache)."""
        return None if client_order_id is None else self.cache.order(client_order_id)

    def _instrument(self, instrument_id: InstrumentId) -> Any:
        """The cached instrument of a watched name; a slot is opened only for one that is there."""
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Mover: instrument {instrument_id} has left the cache")
        return instrument

    def _held(self, slot: Slot) -> Decimal:
        """This strategy's signed open quantity in the slot's instrument."""
        return sum(
            (
                p.signed_qty
                for p in self.cache.positions_open(
                    instrument_id=slot.instrument_id, strategy_id=self.strategy_id
                )
            ),
            Decimal(0),
        )

    def _working(self, slot: Slot) -> bool:
        """Whether an order of this slot is not closed: emulated, in flight or open."""
        return any(
            order is not None and not order.is_closed
            for order in (self._order(slot.entry_id), self._order(slot.protect_id))
        )

    def _fresh(self, now: int) -> bool:
        """Whether a ranking has arrived and is no older than `rank_max_age_secs`."""
        return (
            not self.rank_malformed
            and bool(self.rank_ns)
            and now - self.rank_ns <= self.settings.rank_max_age_secs * SECOND_NS
        )

    def _ranked(self, now: int) -> dict[InstrumentId, tuple[int, Decimal]]:
        """Each ranked name's place and move; nothing when the ranking is stale, or names nobody."""
        if not self._fresh(now):
            return {}
        return {
            instrument_id: (place, move) for place, (instrument_id, move) in enumerate(self.rank)
        }

    # --- watch ---------------------------------------------------------------------------------------

    def _watch(self, now: int) -> None:
        ranked = self._ranked(now)
        if self.settings.load_instruments:
            self._finish_instruments(now)
            top = list(ranked)[: self.settings.watch]
        else:
            top = [i for i in ranked if self.cache.instrument(i) is not None][: self.settings.watch]
        if not self.winding_down:
            for instrument_id in top:
                if instrument_id not in self.slots and (
                    not self.settings.load_instruments or self._instrument_ready(instrument_id, now)
                ):
                    self._open_slot(instrument_id)
                    if (
                        self.settings.entry_arm != "bridge"
                        and instrument_id not in self.entry_contexts
                    ):
                        self._note(
                            self.slots[instrument_id],
                            f"{instrument_id}: entry arm {self.settings.entry_arm}: no entry context supplied",
                        )
        for instrument_id, slot in list(self.slots.items()):
            if instrument_id in top or self._working(slot) or self._held(slot) or slot.was_held:
                continue  # `was_held`: its sale is recorded by `_flat` before the slot may go
            if self._fresh(now) or self.winding_down:  # a stale ranking unwatches nothing
                self._close_slot(slot)

    def _instrument_reserve(self) -> int:
        return INSTRUMENT_REQUEST_RESERVE * sum(
            not load.completed for load in self.instrument_loads.values()
        )

    def _instrument_drop(self, instrument_id: InstrumentId, why: str) -> None:
        text = f"{instrument_id}: {why}"
        if text not in self.dropped:
            self.dropped.append(text)
            self._say(text)

    def _finish_instruments(self, now: int) -> None:
        """Read a completed request's cache result on the timer, never in its response handler."""
        for instrument_id, load in self.instrument_loads.items():
            if load.completed:
                continue
            request_id = load.request_id
            if request_id is not None and request_id in self.instrument_responses:
                load.completed = True
                reason = self.instrument_responses.pop(request_id)
                if load.reason is not None:  # a timeout remains a named refusal for this window
                    continue
                if reason is not None:
                    load.reason = f"instrument request refused: {reason}"
                elif self.cache.instrument(instrument_id) is None:
                    load.reason = "instrument response completed without a cached definition"
                else:
                    load.ready = True
            elif (
                load.reason is None
                and now - load.requested_ns >= self.settings.rank_max_age_secs * SECOND_NS
            ):
                load.reason = (
                    f"instrument request timeout after {self.settings.rank_max_age_secs} s"
                )
            if load.reason is not None:
                self._instrument_drop(instrument_id, load.reason)

    def _instrument_ready(self, instrument_id: InstrumentId, now: int) -> bool:
        """Paper opt-in: native request_instrument -> provider -> cache, then the ordinary subscriptions.

        rc8 trading/__init__.pyi:916-924, live/providers.py:110-115; the adapter's hook sends an
        InstrumentResponse. No live risk setter is exposed to Python: on-demand paper names use
        upstream's absent-map-entry default (risk/engine/mod.rs:1448-1454, :2491-2505; ADR 0091).
        """
        if load := self.instrument_loads.get(instrument_id):
            return load.ready
        if self.cache.instrument(instrument_id) is not None:
            if (
                self.settings.share_caps is not None
                and instrument_id not in self.settings.share_caps
            ):
                self.instrument_loads[instrument_id] = InstrumentLoad(
                    now, completed=True, ready=True
                )
            return True
        while self.instrument_sent and now - self.instrument_sent[0] > BUDGET_SPAN_NS:
            self.instrument_sent.popleft()
        if self._instrument_reserve():
            self._instrument_drop(instrument_id, "waiting for the outstanding instrument request")
            return False
        if len(self.instrument_sent) >= INSTRUMENT_REQUEST_LIMIT:
            self._instrument_drop(
                instrument_id, "instrument request held at the rolling minute limit"
            )
            return False
        line = min(self.settings.soft_line, REQUEST_CEILING)
        if self.recent + INSTRUMENT_REQUEST_RESERVE >= line:
            self._instrument_drop(instrument_id, "instrument request held at the request soft line")
            return False
        load = self.instrument_loads[instrument_id] = InstrumentLoad(now)
        self.instrument_sent.append(now)
        self.instrument_requests += 1
        try:
            load.request_id = self.request_instrument(instrument_id)
        except (RuntimeError, ValueError, TypeError) as e:
            load.completed = True
            load.reason = f"instrument request refused: {type(e).__name__}: {e}"
            self._instrument_drop(instrument_id, load.reason)
        return False

    def _open_slot(self, instrument_id: InstrumentId) -> None:
        bar_type = BarType.from_str(f"{instrument_id}-{self.spec_suffix}")
        self.opened_slots.add(instrument_id)
        self.slots[instrument_id] = Slot(
            instrument_id=instrument_id,
            bar_type=bar_type,
            closes=deque(maxlen=self.settings.run_bars + 1),
            dollars=deque(maxlen=self.settings.flow_bars),
            atr=None
            if self.settings.trail_mode == "fixed"
            else AverageTrueRange(self.settings.atr_n),
            subscribed_ns=self.clock.timestamp_ns(),
        )
        # Quotes and trades first, on the feed: the data engine hands the client only an instrument's
        # first subscription (ADR 0055), and the emulator's own subscription names no feed.
        self.subscribe_quotes(instrument_id, params=self.feed_params)
        self.subscribe_trades(instrument_id, params=self.feed_params)
        self.subscribe_bars(bar_type)

    def _close_slot(self, slot: Slot) -> None:
        del self.slots[slot.instrument_id]
        self.unsubscribe_bars(slot.bar_type)
        self.unsubscribe_trades(slot.instrument_id)
        self.unsubscribe_quotes(slot.instrument_id)

    # --- one slot ------------------------------------------------------------------------------------

    def _ensure_exit(self, slot: Slot, held: Decimal) -> ExitRecord:
        """Start a holding's record before any synchronous release/fill; keep it through re-prices."""
        if slot.exit_record is None:
            slot.exit_record = ExitRecord(
                slot.instrument_id,
                None if slot.entry_record is None else slot.entry_record["client_order_id"],
                held,
            )
            self.exits.append(slot.exit_record)
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

    def _manage(self, slot: Slot, now: int) -> None:
        if slot.protect_id is not None and slot.protect_id.value in self.released:
            slot.exiting = True
        entry = self._order(slot.entry_id)
        if entry is not None and not entry.is_closed:
            self._time_out(slot, entry, now)
        held = self._held(slot)
        if held < 0:
            self._note(
                slot,
                f"{slot.instrument_id}: short {held}, which this strategy never opens; nothing sent",
            )
            return
        if held == 0:
            self._flat(slot, now)
            return
        slot.was_held = True
        row = self._ensure_exit(slot, held)
        quote = self.cache.quote(slot.instrument_id)
        if quote is not None and (bid := quote.bid_price.as_decimal()) > 0:
            row.peak = max(row.peak or bid, bid)
        if self.winding_down:
            if self.settings.extended_hours:
                self._sell_out(slot, held, now)
        elif (self.settings.protect or slot.exiting) and not self.is_exiting():
            # While upstream's market exit runs it alone sells: a sell made here would be denied, being
            # neither reduce-only nor the exit's own (MARKET_EXIT_IN_PROGRESS, pinned
            # crates/trading/src/strategy/mod.rs:2396-2402). Cancels and bookkeeping go on.
            self._protect(slot, held, now)

    def _time_out(self, slot: Slot, entry: Any, now: int) -> None:
        """Cancel an entry still working after its time (again after another, if it is open still); at
        once when the wind-down has begun or this name's sale has been released, since nothing more is
        bought of a name that is being sold, and then again every `chase_secs` while it still works."""
        timeout = self.settings.entry_timeout_secs * SECOND_NS
        due = (slot.entry_cancel_ns or entry.ts_init) + timeout
        at_once = self.winding_down or slot.exiting
        if (now < due and not at_once) or entry.is_pending_cancel or not entry.is_open:
            return
        retry = self.settings.chase_secs * SECOND_NS  # a cancel that failed while the name is sold
        if at_once and slot.entry_cancel_ns and now < slot.entry_cancel_ns + retry:
            return
        slot.entry_cancel_ns = now
        self.cancel_order(entry.client_order_id)

    def _flat(self, slot: Slot, now: int) -> None:
        """Nothing held: withdraw a sell that can no longer sell, and mark the sale once all is closed."""
        order = self._order(slot.protect_id)
        if order is not None and not order.is_closed:
            if not slot.was_held:
                return
            if (order.is_emulated or order.is_open) and not order.is_pending_cancel:
                self.cancel_order(order.client_order_id)
            return
        if slot.was_held and not self._working(slot):
            price = None if order is None or order.avg_px is None else Decimal(str(order.avg_px))
            self.sold[slot.instrument_id] = (now, price)
            slot.was_held = False
            slot.exiting = False
            slot.floor = slot.trail_trigger = slot.sized_loss = slot.exit_trigger = None
            slot.trail_high = None
            slot.observations.clear()
            slot.high_observations.clear()
            slot.breach_observations.clear()
            slot.breach_price = None
            slot.observed_ns = 0
            slot.entry_id = slot.protect_id = None
            slot.entry_cancel_ns = slot.priced_ns = 0
            slot.run_ns = slot.run_close = slot.run_seen_ns = None
            slot.trail_plan = slot.initial_trail = None
            slot.ratcheted, slot.entry_record = False, None
            slot.exit_record = None
            slot.protection_anchor = slot.held_stop = slot.held_since = None
            slot.floor_since, slot.floor_released = None, False
            slot.trailed, slot.last_sale = False, None

    def _protect(self, slot: Slot, held: Decimal, now: int) -> None:
        self._ensure_exit(slot, held)
        if self.persistence and not slot.exiting:
            self._persistent_trail(slot, now)
            if not slot.exiting:
                return
        order = self._order(slot.protect_id)
        if (
            slot.exiting
            and slot.floor is None
            and not slot.floor_released
            and order is not None
            and order.price is not None
        ):
            # the released sale's own price, whether it is open, filled or was refused
            cap = self.settings.chase_cap if slot.trail_plan is None else slot.trail_plan.chase_cap
            slot.floor = chase_floor(order.price.as_decimal(), cap)
            if order.is_open:
                slot.priced_ns = now  # the chase's clock starts when the sale is seen working
        if order is None or order.is_closed:
            if (
                order is not None
                and order.status in REFUSED
                and now - slot.priced_ns < self.settings.chase_secs * SECOND_NS
            ):
                return  # a sale the engine or the venue refused is sent again, every `chase_secs`
            if slot.exiting:
                if self.persistence and slot.floor is None:
                    quote = self.cache.quote(slot.instrument_id)
                    if quote is not None:
                        basis = quote.bid_price.as_decimal()
                        if slot.breach_price is not None:
                            basis = min(basis, slot.breach_price)
                        first = exit_price(basis, self.settings.exit_ticks, None)
                        slot.floor = chase_floor(first, self.settings.chase_cap)
                self._limit_sell(slot, held, now, slot.floor)
            else:
                self._trail(slot, held, now)
        elif order.is_emulated:
            tightened = False
            if self.settings.trail_mode == "ratchet" and not slot.ratcheted:
                tightened = self._ratchet(slot, now)
            # No order is modified. A trailing sell that no longer covers what is held is canceled,
            # and one for all of it is made from the trigger this one had reached.
            if (order.quantity.as_decimal() != held or tightened) and not order.is_pending_cancel:
                if order.trigger_price is not None:
                    reached = order.trigger_price.as_decimal()
                    slot.trail_trigger = max(reached, slot.trail_trigger or reached)
                    self.sell_triggers[order.client_order_id.value] = str(reached)
                self.cancel_order(order.client_order_id)
                # The emulator cancels its own order before the call returns, so the cancel is seen
                # terminal here and the new sell follows in this step, not a second later.
                canceled = self._order(slot.protect_id)
                if canceled is not None and canceled.is_closed:
                    self._trail(slot, held, now)
        elif slot.exiting:
            self._chase(slot, order, held, now, slot.floor)

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
        entry = self._order(slot.entry_id)
        if entry is None or entry.avg_px is None:
            self._note(
                slot,
                f"{slot.instrument_id}: held with no filled entry of this strategy to size a "
                "trailing sell from; not protected, and sold only at the wind-down",
            )
            return
        trailing, _ = offsets(
            Decimal(str(entry.avg_px)), self.settings.trail, self.settings.exit_through
        )
        if slot.trail_high is None or slot.trail_trigger is None:
            # The level starts from the entry's own average fill, the one price here that is a trade
            # of ours. A cached last print is a single observation: taken as the high, one stray
            # print above the market would put the level over it and the next ordinary prints
            # would sell. Prints after this step raise the high only as N of them confirm it.
            bought = Decimal(str(entry.avg_px))
            slot.trail_high = bought
            slot.trail_trigger = stop_trigger_price(bought - trailing)
            slot.observed_ns = 0
            slot.observations.clear()
            self._say(f"{slot.instrument_id}: persistence armed, trigger {slot.trail_trigger}")
            return
        gap_ns = self.settings.confirm_gap_ms * 1_000_000
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
                spaced = self.settings.high_count == 1 or not highs
                if fresh and (spaced or stamp - highs[-1][0] >= gap_ns):
                    highs.append((stamp, price))
                    if len(highs) > self.settings.high_count:
                        highs.popleft()
                    if len(highs) == self.settings.high_count:
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
                if len(breaches) >= self.settings.breach_count:
                    slot.exiting = True
                    slot.breach_price = price
                    slot.exit_trigger = slot.trail_trigger
                    slot.observations.clear()
                    self._say(
                        f"{slot.instrument_id}: breach confirmed at {stamp}, trigger "
                        f"{slot.trail_trigger}; selling by limit"
                    )
                    if not entry.is_closed:
                        self._time_out(slot, entry, now)
                    return
            else:
                breaches.clear()

    def _trail(self, slot: Slot, held: Decimal, now: int) -> None:
        """Protect what is held, using the cache average or bid when the entry fill is unknown.

        Arm the emulator from its followed quote/trade, or hold a stop at the known anchor
        while the followed market price is unavailable.
        """
        trigger = self.settings.trigger
        at = self._followed(slot)
        if at is None or at <= 0:
            # The emulator cannot arm without a market price: hold a stop the strategy checks itself
            # (the lead's decision (b), 2026-11-12), not a sale at once.
            self._hold(slot, held, now)
            return
        try:
            bought = slot.protection_anchor = self._trail_anchor(slot)
        except RuntimeError as error:
            # Raised from the step timer this would stop every slot; it is named and retried next step.
            self._note(slot, str(error))
            return
        if slot.held_since is not None:
            self._end_hold(slot, now, "trail")
        # the reached trigger on Alpaca's step, one value for the check below and for the remade sell
        reached = slot.trail_trigger
        kept = None if reached is None else stop_trigger_price(reached)
        origin = None if reached is None else f"reached {reached}"
        anchor = fill_trigger(bought, slot.sized_loss)
        if anchor is not None and (kept is None or anchor > kept):
            # L under the average fill, rounded up onto the step so a share never loses more than L;
            # the emulator then moves the trigger only up from it (`crates/execution/src/trailing.rs:61`,
            # `:100`), and a remade sell keeps the higher of it and the trigger reached
            kept, origin = anchor, f"{slot.sized_loss} under the fill {bought}"
        if kept is not None and at <= kept:
            # the market is at the trigger the canceled sell had reached: its sale is due now
            slot.exiting, slot.trail_trigger = True, None
            # a reached trigger is retained; with none, or with L under the fill above it, the stop L
            # under the fill is met as the sale is armed
            retained = reached is not None and kept == stop_trigger_price(reached)
            met = (
                "retained trigger"
                if retained
                else "trailing stop (L under the fill, met when armed)"
            )
            self._mark_exit(slot, now, at, met)
            self._say(
                f"{slot.instrument_id}: at {at}, the trigger {kept} ({origin}) is met; "
                "selling by limit"
            )
            slot.exit_trigger = kept
            self._limit_sell(slot, held, now, slot.floor)
            return
        instrument = self._instrument(slot.instrument_id)
        plan = slot.trail_plan
        trailing, limit = offsets(
            bought,
            self.settings.trail if plan is None else plan.trail,
            self.settings.exit_through if plan is None else plan.exit_through,
            # L until a ratchet tightens the plan: the tighter trail then places the trigger; with no
            # room for L under the fill, the trail of the fill, as before S2, never a trigger at zero
            distance=None if slot.ratcheted or anchor is None else slot.sized_loss,
        )
        if slot.exit_record is not None:
            slot.exit_record.trail_distance = trailing
        self._say(
            f"{slot.instrument_id}: trailing SELL {held}, its trigger {trailing} under the highest "
            f"{'bid' if trigger == TriggerType.BID_ASK else 'trade'}, its limit {limit} under it"
            + ("" if kept is None else f", from the trigger {kept} ({origin}) on the step")
        )
        order = self.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            None,
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_price=None if kept is None else instrument.make_price(kept),
            trigger_type=trigger,
            time_in_force=TimeInForce.DAY,
            emulation_trigger=trigger,
        )
        slot.protect_id = order.client_order_id
        slot.trailed = True
        if kept is not None:
            self.sell_triggers[order.client_order_id.value] = str(kept)
        slot.priced_ns = (
            now  # the trigger is kept: a remake that is refused starts the next from it
        )
        self.submit_order(order, params=self.submit_params)

    def _hold(self, slot: Slot, held: Decimal, now: int) -> None:
        """The lead's decision (b), 2026-11-12: with no followed price, hold a stop at the average fill,
        else the entry's limit, less the trail distance in force. It sells at once only when the last
        known price (the last trade the cache has, else the latest minute close) is through that level,
        so it does not depend on the missing side and adds no subscription. With no two-sided quote
        within `HOLD_MOST_SECS` of the fill it sells by limit as an `unpriced_exit`."""
        if slot.held_since is None:
            entry = self._order(slot.entry_id)
            fills = [
                at
                for cid, side, *_, at in self.fills
                if entry is not None
                and cid == entry.client_order_id.value
                and side == OrderSide.BUY.name
            ]
            # 60 s from the fill (the lead's bound); a hold after the holding was trailed, from now
            slot.held_since = now if slot.trailed else min(fills, default=now)
            anchor = self._held_anchor(slot)
            if anchor is not None:
                plan = slot.trail_plan
                distance = offsets(
                    anchor,
                    self.settings.trail if plan is None else plan.trail,
                    self.settings.exit_through if plan is None else plan.exit_through,
                )[0]
                slot.held_stop = anchor - distance
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
            self._note(
                slot,
                f"{slot.instrument_id}: no followed price; holding a stop at {slot.held_stop}",
            )
        self._ensure_exit(slot, held)
        self._held_at_l(slot)
        last, source = self._last_known(slot)
        if slot.held_stop is not None and last is not None and last <= slot.held_stop:
            slot.exiting = True
            self._mark_exit(slot, now, last, f"held stop ({source})")
            if slot.entry_record is not None:
                slot.entry_record["held_stop_trigger"] = source
            slot.exit_trigger = slot.held_stop  # before `_end_hold` clears it
            self._end_hold(slot, now, "held_stop")
            self._limit_sell(slot, held, now, slot.floor, anchor=last)
            return
        if now - slot.held_since >= HOLD_MOST_SECS * SECOND_NS:
            anchor = last if last is not None else slot.protection_anchor
            slot.exiting = True
            self._mark_exit(slot, now, last, "unpriced_exit")
            self._end_hold(slot, now, "unpriced_exit")
            self._limit_sell(slot, held, now, slot.floor, anchor=anchor)

    def _held_at_l(self, slot: Slot) -> None:
        """S2 under #320's held stop: an entry sized for L holds its stop L under the average fill,
        rounded up onto the step (`fill_trigger`), the level its trailing sell would start from, and
        follows the average as the entry fills further, as a trailing sell remade for all that is held
        would; a trigger a trailed sale had reached still holds it up. A ratchet's tighter trail, or no
        room for L, keeps #320's level at the trail's distance."""
        if slot.sized_loss is None or slot.ratcheted:
            return
        anchor = self._held_anchor(slot)
        at_l = None if anchor is None else fill_trigger(anchor, slot.sized_loss)
        if at_l is None:
            return
        reached = None if slot.trail_trigger is None else stop_trigger_price(slot.trail_trigger)
        slot.held_stop = at_l if reached is None else max(at_l, reached)
        slot.protection_anchor = anchor
        if slot.exit_record is not None:
            slot.exit_record.trail_distance = slot.sized_loss
        if slot.entry_record is not None:
            slot.entry_record["held_stop_level"] = str(slot.held_stop)

    def _end_hold(self, slot: Slot, now: int, path: str) -> None:
        """Record how the held stop ended and for how long it held; clear it."""
        if slot.held_since is None:
            return
        if slot.entry_record is not None:
            slot.entry_record["protect_path"] = path
            slot.entry_record["held_stop_secs"] = (now - slot.held_since) / SECOND_NS
        self.protect_paths[path] += 1
        slot.held_stop = slot.held_since = None

    def _held_anchor(self, slot: Slot) -> Decimal | None:
        """The average fill (the entry's, else the cache's position average), else the entry's limit."""
        entry = self._order(slot.entry_id)
        if entry is not None and entry.avg_px is not None:
            filled = Decimal(str(entry.avg_px))
            if filled.is_finite() and filled > 0:
                return filled
        average = self._position_average(slot)
        if average is not None:
            return average
        price = None if entry is None else getattr(entry, "price", None)
        return None if price is None else price.as_decimal()

    def _last_known(self, slot: Slot) -> tuple[Decimal | None, str]:
        """The last trade the cache has, else the latest minute close: what the held stop reads."""
        trade = self.cache.trade(slot.instrument_id)
        if trade is not None:
            return trade.price.as_decimal(), "last trade"
        if slot.closes:
            return slot.closes[-1], "minute close"
        return None, "none"

    def _position_average(self, slot: Slot) -> Decimal | None:
        """The cache's quantity-weighted average of this strategy's open long positions, or None."""
        positions = self.cache.positions_open(
            instrument_id=slot.instrument_id, strategy_id=self.strategy_id
        )
        quantity, notional = Decimal(0), Decimal(0)
        for position in positions:
            average = position.avg_px_open
            if average is None:
                return None
            average = Decimal(str(average))
            if not average.is_finite() or average <= 0 or position.signed_qty <= 0:
                return None
            quantity += position.signed_qty
            notional += position.signed_qty * average
        return notional / quantity if quantity else None

    def _trail_anchor(self, slot: Slot) -> Decimal:
        """Entry fill, else the cache's quantity-weighted position average, else the current bid.

        `Cache.positions_open(instrument_id, strategy_id)` and `Position.avg_px_open/signed_qty`
        are pinned rc8's contracts. Missing or non-positive/non-finite averages cannot anchor a sale.
        Each fallback use (including a refused remake) is counted, without changing entry gating.
        """
        entry = self._order(slot.entry_id)
        if entry is not None and entry.avg_px is not None:
            filled = Decimal(str(entry.avg_px))
            if filled.is_finite() and filled > 0:
                return filled
        average = self._position_average(slot)
        source = "position average price" if average is not None else "current bid"
        quote = self.cache.quote(slot.instrument_id)
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
        self.protection_fallbacks[source] += 1
        self._note(
            slot,
            f"{slot.instrument_id}: no filled entry price; trailing sell anchored on {source} {anchor}",
        )
        return anchor

    def _followed(self, slot: Slot) -> Decimal | None:
        """The price the trailing sell's trigger follows, as the cache has it now: the bid for `BID_ASK`,
        the last trade for `LAST_PRICE`; None before the first."""
        if self.settings.trigger == TriggerType.BID_ASK:
            quote = self.cache.quote(slot.instrument_id)
            return None if quote is None else quote.bid_price.as_decimal()
        trade = self.cache.trade(slot.instrument_id)
        return None if trade is None else trade.price.as_decimal()

    def _chase(
        self, slot: Slot, order: Any, held: Decimal, now: int, floor: Decimal | None
    ) -> None:
        """Re-price a sell limit that still rests after `chase_secs`: `exit_ticks` through the bid, not
        below `floor`, and only downwards. A re-price waits at the entry soft line."""
        if not order.is_open or order.is_pending_update or order.is_pending_cancel:
            return
        if order.leaves_qty.as_decimal() < held:
            # more was bought after this sale was sent: cancel it, and the next step sells all that is
            # held in one order, so no share is without its sale and none has two
            self.cancel_order(order.client_order_id)
            return
        if now - slot.priced_ns < self.settings.chase_secs * SECOND_NS:
            return
        quote = self.cache.quote(slot.instrument_id)
        if quote is None or order.price is None:
            return
        bid = quote.bid_price.as_decimal()
        if bid <= 0:
            return  # a one-sided quote is no price to chase to, nor a bid through the floor
        price = exit_price(bid, self.settings.exit_ticks, floor)
        through = exit_price(bid, self.settings.exit_ticks, None)
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
        if self.recent >= self.settings.soft_line:
            # the lead's backstop (ADR 0083): near the ceiling a re-price waits, the sale keeps working
            self._note(
                slot,
                f"{slot.instrument_id}: sale not chased while the last 60 s's requests are at the "
                f"soft line of {self.settings.soft_line}",
            )
            return
        slot.priced_ns = now
        self.cancel_order(
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
        quote = self.cache.quote(slot.instrument_id)
        bid = None if quote is None else quote.bid_price.as_decimal()
        quoted = bid is not None and bid > 0
        if not quoted:
            # no bid: the given anchor, else the last trade or minute close, else the entry's anchor
            bid = anchor if anchor is not None else self._last_known(slot)[0]
            if bid is None:
                bid = slot.protection_anchor
        if bid is None:
            return
        instrument = self._instrument(slot.instrument_id)
        price = exit_price(bid, self.settings.exit_ticks, floor)
        if not quoted and slot.last_sale is not None:
            price = min(price, slot.last_sale)  # a remake with no bid never re-prices upwards
        slot.last_sale = price
        order = self.order_factory.limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            Price.from_str(f"{price:.{instrument.price_precision}f}"),
            time_in_force=TimeInForce.DAY,
        )
        slot.protect_id = order.client_order_id
        if slot.exit_trigger is not None:
            self.sell_triggers[order.client_order_id.value] = str(slot.exit_trigger)
        slot.priced_ns = now
        self._say(f"{slot.instrument_id}: SELL {held} limit {price} (floor {floor})")
        self.submit_order(order, params=self.submit_params)

    def _sell_out(self, slot: Slot, held: Decimal, now: int) -> None:
        """The wind-down outside the session: withdraw the entry and the trailing sell, then sell what
        is held with a limit through the bid, re-priced every `chase_secs`, with no floor."""
        # `_time_out` cancels a working entry at once; what it still buys is covered by `_chase`, which
        # re-sends for all that is held
        self._ensure_exit(slot, held)
        quote = self.cache.quote(slot.instrument_id)
        self._mark_exit(
            slot, now, None if quote is None else quote.bid_price.as_decimal(), "wind-down"
        )
        begun, slot.exiting = slot.exiting, True
        chase = self.settings.chase_secs * SECOND_NS
        order = self._order(slot.protect_id)
        if order is None or order.is_closed:
            refused = begun and order is not None and order.status in REFUSED
            if refused and now - slot.priced_ns < chase:
                return  # as in `_protect`: a refused sale is sent again every `chase_secs`
            self._limit_sell(slot, held, now, None)
        elif order.is_emulated:
            if not order.is_pending_cancel:
                self.cancel_order(order.client_order_id)
        else:
            self._chase(slot, order, held, now, None)

    # --- the end -------------------------------------------------------------------------------------

    def _end(self) -> None:
        """The wind-down in the session: upstream's market exit, once, for every slot.

        Pinned rc8 strategies.md:768-774 documents that a managed stop requested at backtest end
        cannot complete: engine.rs:1041-1083 already flushed timers before stop_trader, and the
        completion check is later. A flat strategy may remain RUNNING in that simulation. Live time
        continues advancing; the coordinator's 2026-11-11 ruling keeps this wind-down unchanged.
        """
        if self.settings.extended_hours or self.is_exiting():
            return
        if any(self._held(slot) or self._working(slot) for slot in self.slots.values()):
            for slot in self.slots.values():
                if held := self._held(slot):
                    self._ensure_exit(slot, held)
                    quote = self.cache.quote(slot.instrument_id)
                    self._mark_exit(
                        slot,
                        self.clock.timestamp_ns(),
                        None if quote is None else quote.bid_price.as_decimal(),
                        "wind-down",
                    )
            self._say("Winding down: upstream's market exit for what is held and what works")
            self.market_exit()

    # --- entries -------------------------------------------------------------------------------------

    def _enter(self, now: int, recent: int) -> None:
        candidates = [slot for slot in self.slots.values() if slot.run_ns is not None]
        if not candidates:
            return
        ranked = self._ranked(now)
        candidates.sort(key=lambda slot: ranked.get(slot.instrument_id, (RANK_MOST, 0))[0])
        for slot in candidates:
            run_close, seen = slot.run_close, slot.run_seen_ns
            slot.run_ns = slot.run_close = slot.run_seen_ns = None  # a run is decided on once
            if slot.was_held or self._working(slot) or self._held(slot):
                continue
            why = self._refusal(slot, run_close, ranked, recent, now)
            if why is None:
                why = self._buy(slot, ranked[slot.instrument_id][1], seen, run_close)
            if why is not None:
                self.dropped.append(f"{slot.instrument_id}: {why}")
                self._say(f"{slot.instrument_id}: no entry: {why}")

    def _entered(self) -> int:
        """The entries this run has filled, in whole or in part, and those still working."""
        bought = {cid for cid, side, *_ in self.fills if side == OrderSide.BUY.name}
        for slot in self.slots.values():
            entry = self._order(slot.entry_id)
            if entry is not None and not entry.is_closed:
                bought.add(entry.client_order_id.value)
        return len(bought)

    def _busy(self) -> int:
        return sum(1 for slot in self.slots.values() if slot.was_held or self._working(slot))

    def _refusal(
        self,
        slot: Slot,
        run_close: Decimal | None,
        ranked: dict[InstrumentId, tuple[int, Decimal]],
        recent: int,
        now: int,
    ) -> str | None:
        """Why this candidate is not entered now, or None."""
        settings = self.settings
        if not self._fresh(now):
            if self.rank_malformed:
                return "the last ranking was malformed"
            return "the ranking is stale" if self.rank_ns else "no ranking yet"
        if slot.instrument_id not in ranked:
            return "no longer ranked"
        move = ranked[slot.instrument_id][1]
        if move > settings.max_move:
            return f"moved {move}, over the no-chase cap of {settings.max_move}"
        if slot.instrument_id in self.sold:
            sold_ns, sold_px = self.sold[slot.instrument_id]
            if now - sold_ns < settings.cooldown_secs * SECOND_NS:
                return "inside the cooldown after its sale"
            if sold_px is not None and run_close is not None and run_close <= sold_px:
                return f"its run closed at {run_close}, not above the sale at {sold_px}"
        if self._busy() >= settings.slots:
            return "no slot is free"
        if settings.entries is not None and self._entered() >= settings.entries:
            return f"the run's {settings.entries} entr{'y is' if settings.entries == 1 else 'ies are'} used"
        if recent >= settings.soft_line:
            return f"{recent} requests in the last 60 s, at the soft line of {settings.soft_line}"
        return None

    def _recent_requests(self, now: int) -> int:
        """The adapter's requests over the last 60 seconds, from its running total sampled each step."""
        total = requests_sent()
        if self.settings.load_instruments and not self.sent:
            # Include startup asset/reconciliation traffic conservatively for the first minute.
            self.sent.append((now, 0))
        self.sent.append((now, total))
        while self.sent and now - self.sent[0][0] > BUDGET_SPAN_NS:
            self.sent.popleft()
        return total - self.sent[0][1]

    def _buy(
        self,
        slot: Slot,
        move: Decimal,
        seen: int | None = None,
        run_close: Decimal | None = None,
    ) -> str | None:
        """Send the entry; return why not, if it was not sent. `seen` and `run_close` are the
        qualifying bar's `ts_init` and close, kept with the entry for the run's record."""
        settings = self.settings
        if (
            settings.share_caps is not None
            and slot.instrument_id not in settings.share_caps
            and not (settings.load_instruments and settings.size_from_budget)
        ):
            return "no share cap for this window instrument"
        quote = self.cache.quote(slot.instrument_id)
        if quote is None:
            return "no quote yet"
        instrument = self._instrument(slot.instrument_id)
        account = self._trading_account(instrument.id.venue)
        balance = None if account is None else account.balance_total(instrument.quote_currency)
        if balance is None:
            return "no account balance to size from"
        try:
            price = entry_price(quote.ask_price.as_decimal(), settings.entry_ticks)
        except ValueError as e:
            return str(e)
        try:
            plan = self._entry_trail(slot, price, quote)
        except ValueError as error:
            return str(error)
        if plan.note is not None:
            self._note(slot, f"{slot.instrument_id}: {plan.note}")
        # budget sizing refuses a trail with no room by name (`plan_for_loss`) and a bid at the
        # first trigger by name (`BID_AT_STOP`); the planned stop and its two checks are the legacy path's
        stop = (
            None
            if settings.size_from_budget
            else planned_stop(price, plan.trail, plan.exit_through, plan.chase_cap)
        )
        if stop is not None and stop <= 0:
            return f"no room for the trail under {price}"
        bid_price = getattr(quote, "bid_price", None)
        bid = None if bid_price is None else bid_price.as_decimal()
        if bid is None or bid <= 0:
            return "no positive bid to check against the planned stop"
        if stop is not None and bid < stop:
            return (
                f"the bid {bid} is under the planned stop {stop} of an entry at {price}: the quote "
                "is wider than the plan"
            )
        precision = instrument.price_precision
        sizing: dict[str, str | bool | None] | None = None
        entry_stop: Decimal | None = None
        distance: Decimal | None = None
        if settings.size_from_budget:
            free = account.balance_free(instrument.quote_currency)
            if free is None:
                return "no free balance to size from"
            # the account locks an entry's cash once the venue takes it: one sent and not yet taken
            # (in this step, say) still counts against what the next entry is sized from; a
            # reported account locks nothing, so every working entry counts (`entry_reserve`)
            reported = not account.calculate_account_state
            unlocked = entry_reserve(
                (
                    order
                    for other in self.slots.values()
                    if other is not slot and (order := self._order(other.entry_id)) is not None
                ),
                reported=reported,
            )
            # a buy filled since the last report is not in the reported cash yet, and a sale's
            # proceeds count only once a report has them (the lead's ruling of 2026-11-12)
            self._ask_for_the_account()  # a fill this step, before this buy is sized
            unreported = self._unreported_buys(account) if reported else Decimal(0)
            query_wait = self._query_wait_secs(account) if reported else 0.0
            spendable = free.as_decimal() - unlocked - unreported
            # no cash left to spend is a budget of nothing, refused below as size_zero
            entry_budget = max(min(settings.max_notional, spendable), Decimal(0))
            budget_shares = entry_budget // price
            flow_dollars = sum(slot.dollars, Decimal(0))
            flow_shares = settings.flow_share * flow_dollars // price
            cap = min(budget_shares, flow_shares)
            binding = "flow" if flow_shares < budget_shares else "budget"
            # which bound held, for the record (the lead's ruling of 2026-11-12 on non-roster entries)
            held_by = (
                "flow_share"
                if binding == "flow"
                else "entry_notional"
                if settings.max_notional <= spendable
                else "free_balance"
            )
            # the share bounds, apart from the budget: what upstream takes as its hard limit with it
            share_cap = flow_shares
            if settings.max_shares is not None and Decimal(settings.max_shares) < cap:
                cap, binding, held_by = Decimal(settings.max_shares), "max_shares", "max_shares"
            if settings.max_shares is not None:
                share_cap = min(share_cap, Decimal(settings.max_shares))
            # the name's start-time share cap bounds the entry and is never rewritten, so a price
            # that drifted since the start does not trip the monitor and a trip means a real fault
            # (the lead's change of 2026-11-12, ultra review of the composition, finding 14)
            start_cap = (
                None if settings.share_caps is None else settings.share_caps.get(slot.instrument_id)
            )
            if start_cap is not None and Decimal(start_cap) < cap:
                cap, binding, held_by = Decimal(start_cap), "cap_bound", "start_cap"
            if start_cap is not None:
                share_cap = min(share_cap, Decimal(start_cap))
            # S2: the one call that sizes an entry for its planned loss and gives the stop it was sized
            # from; entry less that stop is L, the distance `_trail` holds under the fill, one value
            planned_loss = settings.planned_loss
            if planned_loss is None:  # MoverConfig refuses size_from_budget without one of the two
                planned_loss = share_loss(price, settings.max_notional, settings.loss_share)
            if planned_loss <= 0:  # the slot's figure buys no share at this price
                self.sizing_refusals["size_zero"] += 1
                return (
                    f"sizing refused: size_zero (the figure {settings.max_notional} USD buys no share "
                    f"at {price})"
                )
            try:
                sized = plan_for_loss(
                    instrument=instrument,
                    entry=Price.from_str(f"{price:.{precision}f}"),
                    distance=plan.trail,
                    planned_loss=planned_loss,
                    flow_cap=share_cap,
                    budget=entry_budget,
                )
            except (ValueError, TypeError) as error:
                name = str(error)
                if name not in SIZING_REFUSALS:
                    # upstream's own error, not a refusal: named, not counted, and the step goes on
                    # to its other candidates rather than lose them
                    return f"sizing failed, not a refusal: {type(error).__name__}: {error}"
                self.sizing_refusals[name] += 1
                return (
                    f"sizing refused: {name} (trail {plan.trail}, planned loss {planned_loss} USD, "
                    f"budget {entry_budget} USD, share cap {share_cap}, at {price})"
                )
            quantity, entry_stop = sized
            # L on the entry's step, down: where the stop crosses $1 its sub-dollar step leaves a
            # residue a cent-priced bid cannot trail by, and down keeps the loss within the plan
            distance = (price - entry_stop).quantize(tick(price), rounding=ROUND_FLOOR)
            if distance <= 0:
                self.sizing_refusals[DISTANCE_UNDER_STEP] += 1
                return (
                    f"the distance {price - entry_stop} sized for an entry at {price} is under its "
                    f"step {tick(price)}: no trail can hold it"
                )
            # a fill at the ask holds its first trigger L under it (`fill_trigger`, the one
            # computation `_trail` makes): a bid already there would sell at once
            ask = quote.ask_price.as_decimal()
            at_ask = fill_trigger(ask, distance)
            if at_ask is None or bid <= at_ask:
                self.sizing_refusals[BID_AT_STOP] += 1
                return (
                    f"the bid {bid} is at or under {at_ask}, the distance {distance} sized for an "
                    f"entry at {price} under the ask {ask}: the quote is wider than the trail"
                )
            # upstream's hard limit is `cap` (budget, flow, max_shares, start cap), so a quantity under
            # it was held by the planned loss; loss_shares is the record's figure of that bound
            loss_shares = planned_loss // (price - entry_stop)
            if quantity.as_decimal() < cap:
                binding = held_by = "planned_loss"
            self.sizing_bindings[held_by] = self.sizing_bindings.get(held_by, 0) + 1
            sizing = {
                "startup_budget_notional": str(settings.max_notional),
                "free_cash": str(free.as_decimal()),
                "unlocked_entries": str(unlocked),
                "unreported_fills": str(unreported),
                "account_query_wait_secs": str(round(query_wait, 3)),
                "budget_notional": str(entry_budget),
                "budget_shares": str(budget_shares),
                "flow_dollars": str(flow_dollars),
                "flow_shares": str(flow_shares),
                "start_cap": None if start_cap is None else str(start_cap),
                "planned_loss": str(planned_loss),
                "trail": str(plan.trail),
                "stop": str(entry_stop),
                "loss_shares": str(loss_shares),
                "loss_per_share": str(distance),
                "loss_at_stop": str(quantity.as_decimal() * distance),
                "binding": binding,
                "held_by": held_by,
                # None: no start-up roster to be in or outside of
                "roster": None
                if settings.share_caps is None
                else slot.instrument_id in settings.share_caps,
                "quantity": str(quantity),
                "notional": str(quantity.as_decimal() * price),
            }
            self._say(
                f"{slot.instrument_id}: size for a loss of {planned_loss} USD at the stop {entry_stop}: "
                f"{loss_shares} shares; budget {entry_budget} USD ({budget_shares} shares), flow cap "
                f"{flow_shares} shares from {flow_dollars} USD, binding {binding}, quantity {quantity}"
            )
        else:
            free = account.balance_free(instrument.quote_currency)
            if free is None:
                return "no free balance to size from"
            assert stop is not None  # the legacy path computed its planned stop above
            cap = min(settings.max_notional, free.as_decimal()) // price
            if len(slot.dollars) > 0:
                cap = min(cap, (settings.flow_share * sum(slot.dollars)) // price)
            if settings.max_shares is not None:
                cap = min(cap, Decimal(settings.max_shares))
            if settings.share_caps is not None:
                cap = min(cap, Decimal(settings.share_caps[slot.instrument_id]))
            if cap < 1:
                return f"the caps leave no share at {price}"
            quantity = FixedRiskSizer(instrument).calculate(
                Price.from_str(f"{price:.{precision}f}"),
                Price.from_str(f"{stop:.{precision}f}"),
                balance,
                settings.risk,
                hard_limit=cap,
            )
        if not quantity.is_positive():
            return f"the size from risk came to nothing at {price}"
        order = self.order_factory.limit(
            slot.instrument_id,
            OrderSide.BUY,
            quantity,
            Price.from_str(f"{price:.{precision}f}"),
            time_in_force=TimeInForce.DAY,
        )
        slot.entry_id = order.client_order_id
        slot.entry_cancel_ns = 0
        slot.trail_plan = slot.initial_trail = plan
        slot.sized_loss = distance
        slot.ratcheted = False
        slot.last_bid = bid
        slot.entry_record = {
            "client_order_id": order.client_order_id.value,
            "instrument_id": str(slot.instrument_id),
            "qualified_ns": seen,
            "run_close": None if run_close is None else str(run_close),
            "submitted_ns": order.init_event.ts_init,
            "sizing": sizing,
            "trail_mode": settings.trail_mode,
            "ratchet_base": settings.ratchet_base if settings.trail_mode == "ratchet" else None,
            "trail": str(plan.trail),
            "atr": None if plan.atr is None else str(plan.atr),
            "spread": None if plan.spread is None else str(plan.spread),
            "trail_bound": plan.bound,
            "trail_cap": settings.trail_cap,
            "spread_floor": None if plan.floor is None else str(plan.floor),
            "own_measure": None if plan.measure is None else str(plan.measure),
            "trail_source": plan.source or plan.bound,
            "stopped_out": False,
            "exit_through": str(plan.exit_through),
            "chase_cap": str(plan.chase_cap),
            "baseline_dollar_volume_per_minute": (
                None
                if slot.instrument_id not in self.baseline_dollar_volume_per_minute
                else str(self.baseline_dollar_volume_per_minute[slot.instrument_id])
            ),
            "liquidity_basis": "baseline dollar volume per minute, a stand-in for size",
            "ratchet": {"tightened": False, "when_ns": None, "from": None, "to": None},
            "notes": [PAPER_TRAIL_LIMIT, TRAIL_SOURCE_LIMIT, SHIPPED_ATR_NOTE]
            + ([] if plan.note is None else [plan.note]),
        }
        self.entries_sent.append(slot.entry_record)
        self.notes.setdefault(slot.instrument_id, []).append(f"entered at move {move}")
        self._say(
            f"{slot.instrument_id}: BUY {quantity} limit {price} (bid {bid}, "
            + (
                f"planned stop {stop}"
                if slot.sized_loss is None
                else f"sized for {slot.sized_loss} a share, the trigger that far under the fill"
            )
            + f", move {move})"
        )
        self.submit_order(order, params=self.submit_params)
        return None

    def _entry_trail(self, slot: Slot, price: Decimal, quote: Any | None) -> TrailPlan:
        """Freeze T1 at entry. An impossible spread floor refuses the entry by name; an unusable spread or
        a cold ATR selects the configured fixed plan, by name."""
        settings = self.settings
        atr = None if slot.atr is None or not slot.atr.initialized else Decimal(str(slot.atr.value))
        if quote is None:
            raise ValueError("no quote yet")
        spread = quoted_spread(quote)
        if settings.trail_mode == "fixed":
            return TrailPlan(
                trail=settings.trail,
                exit_through=settings.exit_through,
                chase_cap=settings.chase_cap,
                atr=atr,
                spread=spread,
                bound="fixed",
            )
        if spread is None:
            return TrailPlan(
                trail=settings.trail,
                exit_through=settings.exit_through,
                chase_cap=settings.chase_cap,
                atr=atr,
                spread=None,
                bound="fixed",
                note="no usable quoted spread (missing/zero bid, crossed or locked): fixed trail fallback",
            )
        floor = settings.spread_floor_m * spread / price
        if floor > TRAIL_CAP and (settings.trail_cap != "own" or not settings.size_from_budget):
            raise ValueError("the spread floor exceeds the 0.02 cap; no entry satisfies both")
        baseline = self.baseline_dollar_volume_per_minute.get(slot.instrument_id)
        base = settings.ratchet_base if settings.trail_mode == "ratchet" else settings.trail_mode
        tier = base == "tier" and baseline is not None and baseline >= settings.liquidity_tier_cut
        note = (
            "baseline dollar volume per minute is unknown: tier uses ATR, a stand-in for size"
            if base == "tier" and baseline is None
            else None
        )
        if tier:
            selected, bound = TIER_TRAIL, "tier"
        else:
            if atr is None:
                # Coordinator's (ii), 2026-11-12: a cold ATR enters on the fixed plan, named, rather
                # than waiting `atr_n` live minutes from the slot's opening; no history is requested.
                cold = f"ATR has fewer than {settings.atr_n} complete one-minute bars: fixed trail fallback"
                if settings.trail_cap == "own" and floor > settings.trail:
                    # `own` drops the cap's refusal of a spread floor over it, so the floor still
                    # bounds the fallback from below: the distance is the larger of the two (T3)
                    return TrailPlan(
                        trail=floor,
                        exit_through=floor / 4,
                        chase_cap=max(floor, settings.chase_cap),
                        atr=None,
                        spread=spread,
                        bound="spread floor",
                        note=f"{cold}, raised to the spread floor"
                        + ("" if note is None else f"; {note}"),
                        source="cold_atr_fallback",
                        floor=floor,
                    )
                return TrailPlan(
                    trail=settings.trail,
                    exit_through=settings.exit_through,
                    chase_cap=settings.chase_cap,
                    atr=None,
                    spread=spread,
                    bound="fixed",
                    note=cold if note is None else f"{cold}; {note}",
                    source="cold_atr_fallback",
                    floor=floor,
                )
            selected, bound = settings.atr_k * atr / price, "atr"
        measure = selected
        if floor >= selected:
            selected, bound = floor, "spread floor"
        if selected > TRAIL_CAP:
            if settings.trail_cap == "own" and not settings.size_from_budget:
                # only `plan_for_loss` carries a trail over the cap, its 0.10 ceiling refusing past it
                raise ValueError(
                    f"the trail {selected} ({bound}) is over the 0.02 cap, and trail_cap own needs "
                    "size_from_budget to size it; refused"
                )
            if settings.trail_cap == "refuse":
                raise ValueError(
                    f"the trail {selected} ({bound}) is over the 0.02 cap; refused (trail_cap refuse)"
                )
            if settings.trail_cap == "clamp":
                selected, bound = TRAIL_CAP, "0.02 cap"
            # `own`: the name's own distance stands, and the entry's size carries it (`_buy`)
        # Coordinator's (ii), 2026-11-12: the chase keeps the configured depth under a narrow trail,
        # so a gap through the trigger is chased to a fill, not left resting above the market.
        chase = max(selected, settings.chase_cap)
        return TrailPlan(
            trail=selected,
            exit_through=selected / 4,
            chase_cap=chase,
            atr=atr,
            spread=spread,
            bound=bound,
            note=note,
            floor=floor,
            measure=measure,
        )

    def _ratchet(self, slot: Slot, now: int) -> bool:
        """Tighten once only while the original emulated sale works; caller cancels and remakes it."""
        initial = slot.initial_trail
        entry = self._order(slot.entry_id)
        quote = self.cache.quote(slot.instrument_id)
        followed = self._followed(slot)
        bought = (
            Decimal(str(entry.avg_px))
            if entry is not None and entry.avg_px is not None
            else slot.protection_anchor
        )
        if initial is None or bought is None or quote is None or followed is None:
            return False
        spread = quoted_spread(quote)
        if spread is None:
            return False
        plan = ratchet_trail(initial, bought, followed, spread, self.settings)
        if plan is None:
            return False
        slot.trail_plan, slot.ratcheted = plan, True
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
        notes = self.notes.setdefault(slot.instrument_id, [])
        if text not in notes:
            notes.append(text)
            self.dropped.append(text)
