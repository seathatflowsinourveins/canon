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
* `adopt_holding` attaches an own cached long without an entry, and arms that same emulated sell
  reduce-only from a supplied trigger rounded up onto Alpaca's step, logging both prices if it moves.
  Carry currently requires the BID_ASK quote trigger: LAST_PRICE adoption is refused by name before
  arming because a late trade seed can bypass held-stop protection.
  A level already met rests behind the native activation price
  (`crates/execution/src/order_emulator/emulator.rs:1721-1782`) until a later observation confirms it
  or the followed price recovers. Timers and repeated event times do not confirm it; a second call
  leaves the existing stop alone. Persistence arms must be off for this emulated-order contract.
* **The protection with both persistence arms off** is the engine's order emulator holding a
  `TRAILING_STOP_LIMIT` sell
  (`OrderFactory.trailing_stop_limit`, `common/__init__.pyi:1523-1547`; concepts/orders/emulated and
  trailing_stop_limit): its trigger follows the bid (`BID_ASK`) or the last trade (`LAST_PRICE`) up and
  never down, and at the trigger the emulator releases a `LIMIT` through the risk engine. A release
  keeps the submit command's params (`crates/execution/src/order_emulator/emulator.rs:1368` and
  `:1522-1531`; upstream's test of it, `:4513`, covers stop orders), so the `extended_hours` mark is given
  once, on the submit. Every protective LIMIT sell carries it, including protection armed in the
  regular session; the entry's mark still follows `extended_hours`. On paper, one marked DAY limit sell
  sent in the regular session was accepted and filled (2026-11-13); whether Alpaca keeps one working
  across 16:00 and overnight is UNVERIFIED.
  T1 (owner/lead, 2026-11-12) selects a fixed entry-price offset, or rc8's
  `AverageTrueRange(period).handle_bar` / `initialized` / `value`
  (`indicators/__init__.pyi:155-185`, `crates/indicators/src/python/volatility/atr.rs`). The engine's
  default is Wilder smoothing with the previous close. Each slot feeds only complete one-minute
  bars. On opening, a slot warms from `Cache.bars`' newest-first cached EXTERNAL minute bars
  (`common/__init__.pyi:309`, `crates/common/src/cache/mod.rs:6276-6284`), closed in this session
  part. The adapter's `parse_bar` stamps streamed bars with receipt time, so their interval end is
  derived from the open, as that contract requires. If those leave the ATR short, under a non-fixed
  trail the opening asks once for the name's minute bars of the last hour, with no session-part bound
  (F-2, the owner's plan of 2026-11-12; the two bounds differ on purpose, UNCALIBRATED): upstream's
  `Strategy.request_bars` (`trading/__init__.pyi:993`), answered through `on_data_response`
  (`crates/common/src/python/response.rs:331-342`), as
  `SessionMaCross` warms its averages. Both sources feed that slot's ATR only, each minute once, in
  time order, and nothing else (`_warm`). Fewer than `atr_n` bars, a refusal or no answer keeps the
  cold fallback, each named and counted in the record's `warm_up`. A cold-fallback holding may tighten
  once when its ATR warms. `atr_n` 14 has two of three landscape sources; `atr_k` and the spread floor
  have none: both UNCALIBRATED. The dollar-volume tier is a stand-in for size, never market
  capitalization. `ratchet_base`
  pairs ratchet with plain ATR or tier: at r initial trails of gain it tightens once to g of the initial trail, subject to
  the latest spread floor, canceling and remaking the emulator order while retaining its trigger.
  All arms keep PRICE offsets frozen from entry; BASIS_POINTS is supported upstream
  (`docs/concepts/orders/index.md:236`) but would change the offset as the followed price rises.
  ADR 0096 amendment 10's `profile` uses trail.py's pure arithmetic and T5's strict document,
  retaining tier at the top liquidity cut. Once per complete slot minute it may tighten through
  the same trigger-preserving remake path as ratchet; profile and ratchet are separate arms.
  `Cache.positions_open` and `Position.avg_px_open` (`common/__init__.pyi:568`,
  `model/__init__.pyi:5868`) supply the whole holding's average, including another entry's late fill;
  absent that average, the entry fill anchors protection, else the current bid. Uses without a
  filled entry are counted by name in the window record.
  Exit timing uses `OrderReleased.ts_event/released_price` and `OrderFilled.ts_event/last_qty/last_px`
  (`model/__init__.pyi:4822-4853`, `:4436-4498`). The release price is the observed bid when triggered
  (`crates/execution/src/order_emulator/emulator.rs:1424-1427`), not the sale's limit price.
  Sale completion takes the latest fill event time, independent of delivery order (the lead,
  2026-11-13; r-dsh-intmark/sr-a-355, P1). `OrderFilled.ts_event` is the venue event clock
  (`crates/model/src/events/order/filled.rs:68-81`, `docs/concepts/data/index.md:453-497`, read
  at the pin 2026-11-13); each holding keeps that maximum across its applied sale fills.
  Re-entry is the lead's state rule (owner, 2026-11-12): reclaim, turn or tape-only VWAP;
  lead, no primary source, UNCALIBRATED. No arm waits for a time cooldown. Live bars have no
  session VWAP, so that arm refuses by name unless the exact completed bar's tape supplies it.
  Each slot accepts strictly newer interval ends before observing a live bar (the lead's R1,
  2026-11-13). Upstream's `validate_data_sequence` defaults to false (`crates/data/src/engine/config.rs:59-61`)
  and its bar check rejects only earlier timestamps, not equal ones (`engine/dispatch.rs:477-493`),
  read at the pin 2026-11-13; the slot's mark also covers direct `on_bar` callers.
  Bars and fills arrive independently (`docs/concepts/data/index.md:453-497`, same pin, read
  2026-11-13). While a holding awaits its final fill callback, retain bounded completed-close
  snapshots and replay only interval ends after that fill into its new state (sr-c-355, P2).
  The clock arm keeps those snapshots until flattening creates its record state (sr-a-355, P2b),
  without moving the cooldown's start or preserving a run across that step.
  Interval ends require fixed time bars: upstream rejects non-time `get_interval_ns` calls
  (`crates/model/src/python/data/bar.rs:164-174`) and MONTH/YEAR use 30/365-day proxies
  (`crates/model/src/data/bar.rs:625-643`), read at the pin 2026-11-13 (sr-a-355, P4).
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

R2's optional opening arm is **lead: the owner's observation**, UNCALIBRATED, no primary source.
It uses the same turn and per-name trail plan as re-entry/protection: top-tier ranked names only,
the observed 09:30 open and running minute low, one opening entry per New York session, within M
minutes and after a fall of at least f trail distances. It is off by default. Its entries use the
existing sizing, request budget, slot and protection path; subsequent entries use the re-entry arms.
Its fall is read against its own row f alone, never the re-entry row's d; its turn from the slot's own
bar series alone. An entry that fills spends the session's opening; one denied, rejected, expired or
canceled with nothing filled is counted (`opening_outcomes`) and leaves it available.
- Open (M row): entries run M-1 minutes; the refusal tests the turn bar's close minute, the row says neither.
- Open (turn row): a wick-only equal low closing below the prior close holds `rises` at 0 until a new low.

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
   follows a fast fall below the stop the entry was sized against. A one-sided quote's zero bid is
   no price, and neither is no quote: the last trade then stands in for the bid, in the reference
   and as the sale's anchor, on every remake; it is the trade newest by its own time, not the one
   received last. The floor is set once; released, it stays released. Every reader of the last
   trade reads that newest print (`_newest_trade`), with an arm on or off: the price a `LAST_PRICE`
   trigger follows, the compare with a reached trigger, the held stop and a sale with no bid. A
   print older than one already received for the name is set aside, and counted per name with the
   most it was late by in the record's `late_prints_set_aside`.
   `exit_ticks` prices that reference and the sale; `exit_through` still shapes entry risk sizing.
3. *Entries.* A slot whose bars have just closed `run_bars` times in a row above the close before is a
   candidate under the explicitly selected `entry_arm="bridge"`. The default `registered` arm is
   selected from `early_entry_study.ARMS`: listing needs both move and cumulative dollar-volume
   ratio, and its run/extension rules are that module's `entry_runs` and `entry_refusal`.
   ADR 0092's table rows are :73 (move), :74 (ratio), :75 (baseline sessions), :76 (rises),
   :77 (first-leg window), :78 (quiet price) and :79 (pullback). No row is tuned here.
   Registered arms require a supplied `Day` in `entry_contexts`; no context, no entry. The strategy
   accepts roster contexts or a composition-supplied on-demand reader on one worker. Completed
   reads are installed on the strategy thread by the step/bar handlers; a pending or failed read
   refuses entry by name. CPython 3.14.10's `ThreadPoolExecutor.submit` / `shutdown(cancel_futures=True)`
   (`Lib/concurrent/futures/thread.py:199-215`, `:254-274`) supplies that worker. Its native bars
   request is the ATR's warm-up (F-2), below.
   Candidates are taken best-ranked first while a slot is free, each refused by name
   (`dropped`) when the ranking is older than `rank_max_age_secs`, the name is no longer in it, its move
   is over `max_move` (the no-chase cap), no quote is in the cache, the size comes to nothing, or the
   requests of the last 60 seconds have reached `soft_line`, which holds entries and instrument requests, never a sale. A name sold before uses the clock cooldown and reclaim by default, or a selected state arm: a reclaim above its sale, a turn from a deep enough low, or a
   tape-supplied VWAP crossing. The default `clock` keeps the existing cooldown and reclaim; the state arms impose no time delay.

Exits never wait for the ranking, the budget or `soft_line`; the engine's submit throttle counts their submits, not their cancels. The strategy is long only: a sell is sized from what is
held, one sell works at a time for a name, and nothing is sold while flat.

The entry switch follows ADR 0115's `arming.read_arming` contract: read the file at each buy
decision, immediately before the order exists; an off read stops the buy and is recorded by name.
Sales never read it. The deployment supplies an absolute path and the absent-file rule; the default
paper configuration names no file and keeps its existing entries.

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
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Context
from decimal import Decimal
from decimal import localcontext
from itertools import groupby
from itertools import pairwise
from pathlib import Path
from statistics import median_high
from statistics import median_low
from types import NoneType
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple
from zoneinfo import ZoneInfo

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS
from canon_alpaca.data import bar_interval_ns
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import request_counts
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import AggregationSource
from nautilus_trader.model import BarAggregation
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

from .arming import Arming
from .arming import read_arming
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
from .price_step import down_on_step
from .price_step import entry_price
from .price_step import exit_price
from .price_step import stop_trigger_price
from .price_step import tick
from .price_step import up_on_step
from .session_ma_cross import WARM_UP_SPAN
from .session_ma_cross import WARM_UP_SPARE_BARS
from .sizing import REFUSALS as SIZING_REFUSALS
from .sizing import LossPlan
from .sizing import plan_for_loss
from .sizing import sized_stop
from .trail import PROFILE_SETTINGS
from .trail import profile_distance
from .trail import profile_record
from .trail import profile_segment
from .trail import validate_profile_settings

if TYPE_CHECKING:
    from collections.abc import Callable
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
NEW_YORK = ZoneInfo("America/New_York")
HISTORY_REQUEST_LIMIT = RANK_MOST
"""At most one ranking's worth of ATR warm-up requests per rolling minute (20), one per slot opening under
a non-fixed trail; an opening over it is held by name and asked again each step until the minute frees or its slot closes
(F-2). Each is one data-API bars request, which the adapter counts per API with every other HTTP attempt
(`canon_alpaca.providers`). It is not counted against `soft_line`, which is the trading API's budget
(`requests_sent`): the data API is limited apart from it, and this limit is that request's own budget."""
HISTORY_WAIT_SECS = QUERY_WAIT_SECS
"""How long a slot's warm-up may go unanswered before it is named unanswered: one adapter REST call at its
longest. The slot enters on the cold path meanwhile; an answer after it still warms, each minute once."""
CONTEXT_READ_LIMIT = HISTORY_REQUEST_LIMIT
"""At most 20 entry-context reads started per rolling minute, including refreshes and retries.
The window separately bounds actual data GET attempts, including pagination and SDK retries, at
ADR 0093's 150/minute row; none consumes the trading-only soft line."""
CONTEXT_RETRY_NS = BUDGET_SPAN_NS
"""A failed read waits 60 seconds from failure before retrying (UNCALIBRATED)."""
OVERNIGHT_HISTORY_FEED = "boats"
"""The overnight part's feed (`session_window.MOVER_PARTS["overnight"]`)."""
OVERNIGHT_HISTORY_DELAY = timedelta(minutes=15)
"""Alpaca serves overnight (BOATS) history only when the request's `end` is at least 15 minutes old
(https://docs.alpaca.markets/us/docs/245-trading.md, read 2026-11-12): an overnight warm-up asks for a
span ending that long ago, and the first live bar's true range takes the gap from its last close, once,
as one of the ATR's `atr_n` inputs."""
HISTORY_REFUSED = "history request refused"
"""How a warm-up the adapter or the strategy refused is named: a 429, a refused feed, no client."""
HISTORY_OUTCOMES = (
    "warm_from_cache",
    "warm",
    "fewer_than_atr_n",
    "refused",
    "held_at_limit",
    "unanswered",
    "slot_closed_first",
)
"""What became of each slot's warm-up, as the record counts them (F-2)."""
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
`sizing.plan_for_loss`, whose 0.10 ceiling still refuses by name, among the sizing refusals.
UNCALIBRATED. In every arm but `fixed`, an entry with a usable quoted spread trails at least the
spread floor at its step, a cold-ATR entry's fixed trail included, and a sale remade at a lower
average keeps the floor its plan took in price terms (`offsets_in_force`), even where that is over
the cap at the lower average; an entry the cap forbids is refused by name, one row of the run's
`refusals` a name and cause, with its first distance, the decisions refused and the last (the
lead's O1-O3 and record rule 1 of 2026-11-12). A trailing sale remade for a changed holding or a
tightening sends L, or the plan's distance, even where the spread floor at the remake's step is over
it, and is counted (`remakes_under_floor`, on the run and the window report), its sale row keeping
that floor, L and the distance sent (the lead's ruling (ii) of 2026-11-13; whether to send the floor
is a registered replay's to decide). Known limit, counted and left as it is (the lead's note of
2026-11-12, 21:01), of `own`: (1) a profile re-evaluation is
clamped to the cap (`_profile_tighten`), so a holding on its own distance over the cap tightens to the
cap, not to the profile's own distance."""
DISTANCE_UNDER_STEP = "distance_under_step"
"""The Mover's own entry refusal after sizing: L, rounded down onto the entry's step, is nothing (S2)."""
DISTANCE_UNDER_FLOOR = "distance_under_floor"
"""The Mover's own entry refusal after sizing: in an arm that takes the spread floor, L on the entry's
step, taken up onto it and sized again, is still under the floor at that step (the lead's cross-dollar
property of 2026-11-13, #338's second reader, P1-1)."""
BID_AT_STOP = "bid_at_stop"
"""The Mover's own entry refusal after sizing, counted with `sizing.REFUSALS`: the bid is at or under the
stop the entry was sized against, so the first trailing sell would trigger at once (S2)."""
PAPER_LOSS_SHARE = Decimal("0.02")
"""The planned loss on paper accounts A and B, as a share of the slot's startup figure (the lead's ruling
of 2026-11-12, 16:36): the loss at the stop that the budget sizing already accepted at the 0.02 cap, so a
2% stop sizes to the budget, a wider one sizes down and a tighter one is held at the budget. No new
number; UNCALIBRATED. Account C and a live node have no default: there the figure is the owner's."""
PART_BOUNDS = ((4, 0), (9, 30), (16, 0), (20, 0))
"""New York clock times at which a part of the 24-hour session begins: pre-market, regular, after-hours
and overnight (`bars_list.PARTS`, and `session_window.session_part` for the overnight 20:00 start)."""
WARM_BAR_SPEC = "1-MINUTE-LAST-EXTERNAL"
"""The rank actor's minute bars (`movers_rank_actor.BAR_SPEC`), which the node's cache already holds for
every ranked name; a slot's ATR warms from them when it opens (the lead's (a), 2026-11-12)."""

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
        "arms": ("fixed", "atr", "tier", "ratchet", "profile"),
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
    "cooldown_secs": {
        "default": 300,
        "status": "UNCALIBRATED",
        "note": "clock keeps the existing cooldown; state arms impose no time delay",
    },
    "reentry_arm": {
        "default": "clock",
        "arms": ("clock", "reclaim", "turn", "vwap"),
        "status": "UNCALIBRATED",
        "source": "lead, no primary source",
    },
    "turn_k": {
        "default": 2,
        "arms": (1, 2, 3),
        "status": "UNCALIBRATED",
        "source": "lead, no primary source",
    },
    "turn_d": {
        "default": Decimal(1),
        "arms": (Decimal(1), Decimal(2)),
        "status": "UNCALIBRATED",
        "source": "lead, no primary source",
    },
    "opening_arm": {
        "default": "off",
        "arms": ("off", "on"),
        "status": "UNCALIBRATED",
        "source": "lead: the owner's observation, no primary source",
    },
    "opening_minutes": {
        "default": 30,
        "arms": (15, 30),
        "status": "UNCALIBRATED",
        "source": "lead: the owner's observation, no primary source",
    },
    "opening_f": {
        "default": Decimal(1),
        "arms": (Decimal(1), Decimal(2)),
        "status": "UNCALIBRATED",
        "source": "lead: the owner's observation, no primary source",
    },
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
    **PROFILE_SETTINGS,
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
RECLAIM_LIMIT = (
    "Re-entry is read on a minute close, by state with no time cooldown: reclaim/turn/vwap; "
    "lead, no primary source, UNCALIBRATED. Live session vwap unavailable; tape values only."
)
SHIPPED_ATR_NOTE = (
    "The one shipped ATR default found is backtesting.py TrailingStrategy's 6 ATR at its bar "
    "frequency (set_trailing_sl(n_atr=6)); this is a note, not a row to fire: "
    "https://kernc.github.io/backtesting.py/doc/backtesting/lib.html#backtesting.lib.TrailingStrategy"
)
OPENING_LIMIT = (
    "Opening arm: lead: the owner's observation; UNCALIBRATED, no primary source. "
    "Regular session only; the observed 09:30 open and complete minute lows, with the shared turn "
    "and current per-name trail distance. Missing opening coverage refuses entry."
)
OPENING_FIELDS = ("opening_arm", "opening_minutes", "opening_f")
REGULAR_OPEN_MINUTE = 9 * 60 + 30


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
    entry: Decimal,
    trail: Decimal,
    exit_through: Decimal,
    *,
    distance: Decimal | None = None,
    least: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """The trailing offset and the limit offset, as prices. The trailing offset is `distance` when the
    entry was sized for one (S2's L, already on the step), else `trail` of the entry up to the entry's
    step: it places the trigger, which the emulator keeps to itself. The limit offset is on
    `tick(entry)`, with spacing below the trigger of at least one step and at most the registered
    CHASE_CAP times `entry - trailing`, rounded down to that step. Keep the old cent-rounded result
    to the digit wherever it meets both bounds; otherwise round the requested give to the entry's
    step and bound it there. Where the cap, rounded down, is under one step, the spacing is ONE step
    (the lead's ruling of 2026-11-13, 17:27): the old cent pair is never kept over the cap, and a
    trigger or limit at or under zero is left to `_trail`/`_hold`'s no-room guards, which read
    `max(trailing, limit)`. An off-step `distance` (S2's L) can leave no on-step limit between one
    step and the cap: the limit is then the largest on-step one within the cap, a positive spacing
    under one step, a named limit. Legacy sizing's `planned_stop` includes the first-sale price.

    NautilusTrader d53849b216e2 (`crates/execution/src/trailing.rs:104-148,176-221`) applies one
    offset type to both distances: PRICE keeps S2/ATR distances fixed, whereas BASIS_POINTS would
    scale both. A fractional-cent basis still retains its residue above $1: the native calculation
    uses the instrument's fixed increment, not PRICES; the adapter owns that venue-price correction.
    `least`, a price, bounds the trailing offset from below, up to the step: the floor a plan took at
    its own step, which a lower average must not cut (`offsets_in_force`); `distance` is not bounded.
    """
    # Import after this module is initialized: protect's planner uses MoverConfig's registered
    # chase_cap default, and protect itself imports mover. No second copy of the row's number.
    from .protect import PLANNER

    step = tick(entry)
    trailing = (
        max((entry * trail).quantize(step, rounding=ROUND_CEILING), step)
        if distance is None
        else distance
    )
    if distance is None and least is not None:
        trailing = max(trailing, least.quantize(step, rounding=ROUND_CEILING))
    limit = (trailing + entry * exit_through).quantize(CENT, rounding=ROUND_CEILING)
    limit = max(limit, (trailing + SUB_DOLLAR).quantize(CENT, rounding=ROUND_CEILING))
    bound = ((entry - trailing) * PLANNER.chase_cap).quantize(step, rounding=ROUND_FLOOR)
    if limit % step == 0 and step <= limit - trailing <= bound:
        return trailing, limit
    lower = (trailing + step).quantize(step, rounding=ROUND_CEILING)
    upper = (trailing + bound).quantize(step, rounding=ROUND_FLOOR)
    if bound < step:
        return trailing, lower
    if lower > upper:
        return trailing, upper
    limit = (trailing + entry * exit_through).quantize(step, rounding=ROUND_CEILING)
    return trailing, min(max(limit, lower), upper)


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
    "profile_k": Decimal,
    "profile_pctl": str,
    "profile_floor": str,
    "profile_multipliers": tuple,
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
    "reentry_arm": str,
    "turn_k": int,
    "turn_d": Decimal,
    "opening_arm": str,
    "opening_minutes": int,
    "opening_f": Decimal,
    "rank_max_age_secs": int,
    "soft_line": int,
    "protect": bool,
    "trigger": TriggerType,
    "extended_hours": bool,
    "wind_down_at": (datetime, NoneType),
    "feed": (str, NoneType),
    "arming_path": (str, NoneType),
    "arming_absent_is_on": bool,
}
"""`MoverConfig`'s own fields and the type each takes."""
ARMING_FIELDS = ("arming_path", "arming_absent_is_on")
"""The entry switch's settings: the run record keeps them under `arming`, not among its rows."""


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
    remain the defaults. State arms ignore cooldown_secs only when explicitly selected.
    Turn uses k rising closes from a low at least d actual trail distances below the sale.
    VWAP is tape-only: no live bar VWAP is inferred. Re-entry arms, ATR multiples and the spread floor are
    lead, no primary source; every row is UNCALIBRATED until the tape with measured spreads.
    Non-fixed entries scale exit_through to trail/4 and keep chase_cap at max(trail, chase_cap) (the
    lead's (ii), 2026-11-12), with an unavoidable spread floor (`spread_floor_m >= 1`) and 0.02 cap.
    An unusable spread selects fixed by name; no quote still refuses entry. An ATR that is not warm
    (fewer than `atr_n` minute bars: cached when the slot opened, of this part; requested (F-2), of
    the last hour with no part bound; or live since) selects fixed as `cold_atr_fallback` (the lead's
    (ii)), raised to the spread floor at its step when that is over it (the lead's O1); such a holding
    tightens once to its warmed ATR's plan, never wider (the lead's (b)). `liquidity_tier_cut`
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
        profile_k: Decimal = Decimal(2),
        profile_pctl: str = "p75",
        profile_floor: str = "median",
        profile_multipliers: tuple[Decimal, Decimal, Decimal] = (
            Decimal(1),
            Decimal(1),
            Decimal(1),
        ),
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
        reentry_arm: str = "clock",
        turn_k: int = 2,
        turn_d: Decimal = Decimal(1),
        opening_arm: str = "off",
        opening_minutes: int = 30,
        opening_f: Decimal = Decimal(1),
        rank_max_age_secs: int = 90,
        soft_line: int = 140,
        protect: bool = True,
        trigger: TriggerType = TriggerType.BID_ASK,
        extended_hours: bool = False,
        wind_down_at: datetime | None = None,
        feed: str | None = None,
        arming_path: str | None = None,
        arming_absent_is_on: bool = True,
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
        validate_profile_settings(profile_k, profile_pctl, profile_floor, profile_multipliers)
        for name in ("reentry_arm", "turn_k", "turn_d", *OPENING_FIELDS):
            if fields[name] not in TRAIL_SETTINGS[name]["arms"]:
                raise ValueError(f"{name} must be one of {TRAIL_SETTINGS[name]['arms']}")
        if opening_arm == "on" and not protect:
            raise ValueError("opening_arm='on' requires protect=True; no unprotected opening entry")
        if opening_arm == "on" and extended_hours:
            raise ValueError("opening_arm='on' is a regular-session arm; extended_hours refuses it")
        if opening_arm == "on" and bar_spec not in (
            "1-MINUTE-LAST-EXTERNAL",
            "1-MINUTE-LAST-INTERNAL",
        ):
            raise ValueError("opening_arm='on' requires 1-MINUTE-LAST bars")
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
            specification = BarType.from_str(f"X.X-{bar_spec}").spec
        except ValueError as e:
            raise ValueError(
                f"bar_spec {bar_spec!r} is not <step>-<aggregation>-<price>-<source>: {e}"
            ) from e
        if not specification.is_time_aggregated() or specification.aggregation in (
            BarAggregation.MONTH,
            BarAggregation.YEAR,
        ):
            raise ValueError(
                f"bar_spec {bar_spec!r} requires a fixed time interval (MILLISECOND through WEEK)"
            )
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
        self.profile_k, self.profile_pctl = profile_k, profile_pctl
        self.profile_floor, self.profile_multipliers = profile_floor, profile_multipliers
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
        self.reentry_arm, self.turn_k, self.turn_d = reentry_arm, turn_k, turn_d
        self.opening_arm, self.opening_minutes, self.opening_f = (
            opening_arm,
            opening_minutes,
            opening_f,
        )
        self.rank_max_age_secs = rank_max_age_secs
        self.soft_line = soft_line
        self.protect = protect
        self.trigger = trigger
        self.extended_hours = extended_hours
        self.wind_down_at = wind_down_at
        self.feed = feed
        if arming_path is not None and (
            "\x00" in arming_path or not arming_path or not Path(arming_path).is_absolute()
        ):
            raise ValueError(
                f"arming_path must be an absolute file path, got {arming_path!r}: a relative one "
                "would be read against whatever folder the node runs in"
            )
        self.arming_path, self.arming_absent_is_on = arming_path, arming_absent_is_on

    @property
    def persistence(self) -> bool:
        """Whether either persistence arm is on: the strategy then holds the stop itself."""
        return self.breach_count > 1 or self.high_count > 1


class DistanceRefusal(ValueError):
    """An entry the trail cap forbids (the lead's O2 of 2026-11-12): its cause, `floor_over_cap` for a
    spread floor over the cap or `distance_over_cap` for a selected distance the arm refuses over it,
    and that distance as a fraction of the entry."""

    def __init__(self, cause: str, distance: Decimal, message: str) -> None:
        super().__init__(message)
        self.cause = cause
        self.distance = distance


class Refused(str):
    """Why no entry was sent, already written as a row of the run's `refusals` (the lead's record rule
    1 of 2026-11-12): `_enter` says it and does not repeat it in `dropped`."""

    __slots__ = ()


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
    profile: dict[str, Any] | None = None
    """Amendment 10's input calculation before the common bounds, including a named thin reason."""


PENDING_REENTRY_BARS = 64
"""Maximum completed-close snapshots per holding awaiting its fill or flat state (sr-c/a-355, P2).
If eviction loses a post-sale close, turn and VWAP refuse that sale's incomplete history by name;
evicted intervals ending at or before the sale do not affect its re-entry state. A memory bound,
not a trading parameter; replay consumes the snapshots once."""


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
    held_stop_kept: Decimal | None = None
    """The level a held stop keeps; a cheaper fill never lowers it (the lead, 2026-11-13 08:24)."""
    held_stop_from_average: Decimal | None = None
    """The average fill less that kept level: negative when a cheaper fill left the stop over it."""
    peak: Decimal | None = None
    trigger_ns: int | None = None
    trigger_price: Decimal | None = None
    trigger_reason: str | None = None
    final_fill_ns: int | None = None
    last_fill_ns: int | None = None
    """Latest SELL event applied to this holding, including parts delivered before a late BUY."""
    entry_received_ns: int | None = None
    """First host receipt (`ts_init`) of the late entry this row was opened for while no slot owned
    it, so its recovery restores the hold's receipt clock (r-pair-codex/P1)."""
    pending_reentry_closes: deque[tuple[int, Decimal, Decimal | None, date]] = field(
        default_factory=lambda: deque(maxlen=PENDING_REENTRY_BARS)
    )
    """Accepted interval end, close, exact-bar VWAP and session date pending this sale's state."""
    lost_reentry_close_ns: int | None = None
    """Newest evicted interval end: only loss strictly after the eventual final fill is incomplete."""
    wind_down_wait_ns: int | None = None
    """From the decision to sell to the wind-down, for a sale still unfilled when it began: a gap deeper
    than the chase floor rests until the wind-down's floorless sale (the coordinator, 2026-11-12)."""
    floor_wait_ns: int | None = None
    """How long the sale rested at its chase floor before the floor was released, if it was."""
    sales: list[dict[str, Any]] = field(default_factory=list)
    """Each sale order the Mover sent for this holding, in order: the lead's D3 terms of 2026-11-12,
    A3, read from the order sent and the record at that moment (`Mover._sale_sent`); its end and any
    refusal's reason come from the order's own events."""

    def take_sale(self, quantity: Decimal, price: Decimal, at: int) -> Decimal:
        """Add a sale fill to the holding's totals, no more than it still holds: a canceled leg's late
        fill after another leg sold it all is counted once (the pinned `crates/model/src/orders/
        mod.rs:247`, `:295`; the Antigravity reader of #351, P2-1). Its last fill time is the latest
        event time taken, so a fill that arrives late with an earlier time does not move it back.
        Returns the quantity taken."""
        take = min(quantity, max(Decimal(0), self.quantity - self.filled))
        if take > 0:
            self.filled += take
            self.notional += take * price
            self.last_fill_ns = at if self.last_fill_ns is None else max(self.last_fill_ns, at)
            if self.filled >= self.quantity:
                self.final_fill_ns = self.last_fill_ns
        return take

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
            "held_stop_kept": None if self.held_stop_kept is None else str(self.held_stop_kept),
            "held_stop_from_average": (
                None if self.held_stop_from_average is None else str(self.held_stop_from_average)
            ),
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
            "sales": [dict(sale) for sale in self.sales],
        }


@dataclass(kw_only=True)
class TurnState:
    """T1/T2's turn, shared with R2: consecutive rising closes starting at the observed low.

    Re-entry observes close lows. Opening observes minute lows and anchors the same rising-close
    sequence to the close of the minute that made that low. An equal low reached only by a wick
    does not erase a rising close; an equal/new low close still restarts the re-entry turn exactly.
    """

    low: Decimal | None = None
    previous: Decimal | None = None
    rises: int = 0
    low_close: Decimal | None = None

    def observe_turn(self, close: Decimal, low: Decimal | None = None) -> None:
        observed = close if low is None else low
        if self.low is None or observed < self.low or close <= self.low:
            self.low, self.rises, self.low_close = observed, 0, close
        elif (
            self.previous is not None
            and close > self.previous
            and (self.rises > 0 or self.previous in (self.low, self.low_close))
        ):
            self.rises += 1
        else:
            self.rises = 0
        self.previous = close

    def turn_refusal(
        self, reference: Decimal | None, distance: Decimal | None, k: int, d: Decimal
    ) -> str | None:
        if reference is None or distance is None or self.low is None:
            return "turn state or trail distance unavailable"
        depth, required = reference - self.low, d * distance
        if depth < required:
            return f"turn depth {depth} is under {d} trail distances ({required})"
        if self.rises < k:
            return f"turn needs {k} rising closes from the low; has {self.rises}"
        return None


@dataclass
class OpeningState(TurnState):
    """R2 state belongs to the name/session and survives slot removal; no history is invented."""

    day: date
    open: Decimal
    low_minute: int
    last_open_ns: int
    complete: bool = True
    used: bool = False
    order_id: ClientOrderId | None = None
    """The opening entry still undecided: a fill spends the opening, an end with nothing filled frees it."""
    turn_ns: int | None = None
    turn_minute: int | None = None


SOLD_THIS_TRADE_DATE = "sold_this_trade_date"
"""The opening's refusal of a name sold on its own trade date, which the record names."""


@dataclass
class ReentryState(TurnState):
    """The lead's post-sale state, kept by name across slots; no clock-based brake.

    Only complete closes after the sale count. A turn starts at the lowest observed close and
    needs consecutive rises from that low. A tape VWAP must belong to this exact completed bar;
    a missing value clears the crossing, so stale or future VWAPs cannot admit an entry.
    """

    sale_ns: int
    sale_price: Decimal | None
    trail_distance: Decimal | None
    vwap: Decimal | None = None
    below_vwap: bool = False
    crossed_vwap: bool = False
    vwap_day: date | None = None
    history_complete: bool = True
    """False after a pending post-sale close was evicted; turn/VWAP refuse until a new sale."""

    def observe_pending(self, row: ExitRecord) -> None:
        """Consume a holding's accepted closes exactly once, using the final fill's event clock."""
        if row.lost_reentry_close_ns is not None and row.lost_reentry_close_ns > self.sale_ns:
            self.history_complete = False
        while row.pending_reentry_closes:
            closed, close, vwap, day = row.pending_reentry_closes.popleft()
            if closed > self.sale_ns:
                self.observe(close, vwap, day)
        row.lost_reentry_close_ns = None

    def observe(self, close: Decimal, vwap: Decimal | None, day: date) -> None:
        self.observe_turn(close)
        if day != self.vwap_day:
            self.below_vwap, self.vwap_day = False, day
        self.vwap = vwap if vwap is not None and vwap.is_finite() and vwap > 0 else None
        self.crossed_vwap = self.vwap is not None and self.below_vwap and close > self.vwap
        self.below_vwap = self.vwap is not None and close < self.vwap

    def restart(self) -> None:
        """A reopened slot missed the closes in between: its low, rises and VWAP side start again."""
        self.low, self.previous, self.rises = None, None, 0
        self.vwap, self.below_vwap, self.crossed_vwap = None, False, False


def history_window(
    settings: MoverConfig, feed_params: dict[str, str] | None, now: datetime
) -> tuple[datetime, datetime | None]:
    """The `start` and `end` of a slot's warm-up request at `now`: `WARM_UP_SPAN`, or `atr_n` plus
    `WARM_UP_SPARE_BARS` minutes if that is longer, ending now (`end` None) or, on the overnight feed,
    `OVERNIGHT_HISTORY_DELAY` ago. Not bounded to the session part open now, as the cached warm-up is
    (`Mover._warm_atr`): a slot opened at a part's start is warm from the part before (the lead's
    ruling of 2026-11-13, 00:10). The two bounds differ on purpose, UNCALIBRATED, for ADR 0133."""
    span = max(WARM_UP_SPAN, (settings.atr_n + WARM_UP_SPARE_BARS) * timedelta(minutes=1))
    feed = (feed_params or {}).get("feed")
    end = now - OVERNIGHT_HISTORY_DELAY if feed == OVERNIGHT_HISTORY_FEED else None
    return (now if end is None else end) - span, end


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


def part_opened_ns(now_ns: int) -> int:
    """When the session part open at `now_ns` began, by `PART_BOUNDS` in New York."""
    at = datetime.fromtimestamp(now_ns / SECOND_NS, NEW_YORK)
    starts = [
        datetime.combine(at.date() - timedelta(days=back), time(hour, minute), NEW_YORK)
        for back in (0, 1)
        for hour, minute in PART_BOUNDS
    ]
    return int(max(start for start in starts if start <= at).timestamp()) * SECOND_NS


def bar_opened_ns(bar: Bar) -> int:
    """A minute bar's open: an INTERNAL bar's ts_init less a minute, an adapter EXTERNAL bar's ts_event.
    The one rule for `on_bar`, `_registered_run` and the cached warm."""
    return bar.ts_init - 60 * SECOND_NS if bar.bar_type.is_internally_aggregated() else bar.ts_event


def bar_closed_ns(bar: Bar) -> int:
    """When a bar's interval ended: an INTERNAL bar's ts_init, an adapter EXTERNAL bar's ts_event plus
    its own interval (`canon_alpaca.data.parse_bar`'s rule, by `bar_interval_ns`). A streamed EXTERNAL
    bar is received after this, so its receipt time can follow a fill its close preceded."""
    if bar.bar_type.is_internally_aggregated():
        return bar.ts_init
    return bar.ts_event + bar_interval_ns(bar.bar_type)


def opening_ended(
    states: dict[InstrumentId, OpeningState], outcomes: dict[str, int], event: Any, outcome: str
) -> None:
    """Count how an opening entry ended (the lead's ruling on review finding (8), 2026-11-12). Its
    first fill spends the opening; an end with nothing filled (denied, rejected, expired, or canceled,
    as the entry's time-out cancels it) frees the session's opening again. A function of plain values,
    so an order event handler can call it while the strategy is borrowed."""
    state = states.get(event.instrument_id)
    if state is None or state.order_id != event.client_order_id:
        return
    outcomes[outcome] = outcomes.get(outcome, 0) + 1
    state.order_id = None
    if outcome != "filled":
        state.used = False


def refusal_cause(reason: str) -> str:
    """The cause of a refusal, for the D3 terms' A7 (a refusal for a cause of ours), from its text:
    `wash_trade` for Alpaca's paper text "potential wash trade detected" (the adapter's README, "Wash
    trades"), `off_step` for the adapter's `sub_penny_denial` of a price off Alpaca's increment or
    for Alpaca's own rejection of one ("sub-penny increment does not fulfill minimum pricing
    criteria", "Orders at Alpaca"), and `other` for anything else, whose text is kept beside it."""
    if "wash trade" in reason.lower():
        return "wash_trade"
    if "decimals, which Alpaca rejects" in reason or "sub-penny increment" in reason.lower():
        return "off_step"
    return "other"


def sale_ended(
    rows: dict[str, dict[str, Any]], event: Any, ended: str, reason: str | None = None
) -> None:
    """How a sale the Mover sent ended, from its own order event, with the venue's or the node's
    reason and its cause for a refusal; the first end is kept. A function of plain values, so an
    order event handler can call it while the strategy is borrowed (as `opening_ended`)."""
    row = rows.get(event.client_order_id.value)
    if row is not None and row["ended"] is None:
        row["ended"], row["ended_ns"], row["reason"] = ended, event.ts_event, reason
        row["cause"] = None if reason is None else refusal_cause(reason)


def name_oversale(dropped: list[str], event: Any, excess: Decimal, held: Decimal) -> None:
    """Name, once, a sale fill beyond what its holding held; its own row and `fills` keep it. Plain
    values only, as the fill handler may run while the strategy is borrowed."""
    if excess <= 0:
        return
    text = (
        f"{event.instrument_id}: {event.client_order_id} sold {excess} beyond its holding's {held}"
    )
    if text not in dropped:
        dropped.append(text)


def _sold_out(row: ExitRecord | None) -> bool:
    """Whether a holding's sale is complete: every share it bought sold, its final fill known."""
    return row is not None and row.final_fill_ns is not None and row.filled >= row.quantity


def entry_basis(entry: Any) -> Decimal | None:
    """The entry order's own average fill, finite and positive, else None: the price its plan's fraction
    was selected over, never a stale anchor or a bid (`offsets_in_force`, code-review-l5-latebuy2/F3).
    As `_trail_anchor`: no other average anchors."""
    if entry is None or entry.avg_px is None:
        return None
    filled = Decimal(str(entry.avg_px))
    return filled if filled.is_finite() and filled > 0 else None


def late_receipt(row: ExitRecord, event: Any) -> None:
    """Keep the first host receipt (`ts_init`) of a late entry fill no slot owns yet, so its
    recovery restores the hold's receipt clock, not the venue's (r-pair-codex/P1). Plain values
    only, as the fill handler may run while the strategy is borrowed."""
    received = getattr(event, "ts_init", None)
    if row.entry_received_ns is None and received is not None:
        row.entry_received_ns = int(received)


def resold(
    sold: dict[InstrumentId, tuple[int, Decimal | None]],
    sold_fill_ns: dict[InstrumentId, int],
    reentry_states: dict[InstrumentId, ReentryState],
    instrument_id: InstrumentId,
    row: ExitRecord,
) -> None:
    """A holding `_flat` marked sold took a late sale fill: its sale is at the holding's average now,
    from when it was marked, and its re-entry state is taken again from that sale."""
    when, _ = sold[instrument_id]
    price = row.notional / row.filled
    sold[instrument_id] = (when, price)
    filled_ns = row.last_fill_ns if row.last_fill_ns is not None else when
    sold_fill_ns[instrument_id] = filled_ns
    reentry_states[instrument_id] = ReentryState(filled_ns, price, row.trail_distance)


def entry_filled(
    entries: list[dict[str, Any]], fills: list[tuple[str, str, str, str, int]], client_order_id: str
) -> None:
    """The entry's average fill so far, from its own BUY fills only, so its trail is read in price
    terms (D3 terms, A1); its opening row's `entry_price` and its re-entry row's `reentry_price` are
    the same average, and `minutes_between` runs from the sale to its own first fill. A function of
    plain values, so an order event handler can call it while the strategy is borrowed (as
    `sale_ended`)."""
    entry = next((e for e in reversed(entries) if e["client_order_id"] == client_order_id), None)
    if entry is None:
        return
    quantity = notional = Decimal(0)
    first: int | None = None
    for cid, side, qty, px, at in fills:
        if cid == client_order_id and side == OrderSide.BUY.name:
            quantity += Decimal(qty)
            notional += Decimal(qty) * Decimal(px)
            first = at if first is None else min(first, at)
    if not quantity or first is None:
        return
    price = str(notional / quantity)
    entry["entry_price"] = price
    opening = entry.get("opening")
    if opening is not None:
        opening["entry_price"] = price
    reentry = entry.get("reentry")
    if reentry is not None:
        reentry["reentry_price"] = price
        # read again at every fill: an earlier-stamped part fill can arrive second (xr7-r-a3fix, P2)
        reentry["minutes_between"] = (first - reentry["sale_ns"]) / (60 * SECOND_NS)


NOT_SENT = "not sent:"
"""How the adapter's `not_sent_reason` begins: a rejection for a command a connect timeout proves never
reached Alpaca (ADR 0039)."""


def refusal_row(event: Any, by: str) -> dict[str, Any]:
    """One refusal with who refused it, `venue` (Alpaca's rejection) or `before_venue` (a denial in
    the node, or a rejection the adapter proves was never sent), and its cause; plain values, for an
    event handler."""
    reason = str(event.reason)
    return {
        "client_order_id": event.client_order_id.value,
        "by": "before_venue" if reason.startswith(NOT_SENT) else by,
        "cause": refusal_cause(reason),
        "reason": reason,
    }


def quoted_spread(quote: Any) -> Decimal | None:
    """The latest ask minus a positive bid; crossed, locked or absent bids are no usable spread."""
    bid_price = getattr(quote, "bid_price", None)
    ask_price = getattr(quote, "ask_price", None)
    if bid_price is None or ask_price is None:
        return None
    bid, ask = bid_price.as_decimal(), ask_price.as_decimal()
    return ask - bid if 0 < bid < ask else None


def two_sided(quote: Any) -> bool:
    """Whether a quote has both sides above zero, locked or crossed included: the adapter refuses only a
    side at or under zero (`canon_alpaca.data.parse_quote`), so a quote it kept with both sides is not
    one-sided, though it has no usable spread (`quoted_spread`)."""
    bid_price = getattr(quote, "bid_price", None)
    ask_price = getattr(quote, "ask_price", None)
    if bid_price is None or ask_price is None:
        return False
    return bid_price.as_decimal() > 0 and ask_price.as_decimal() > 0


def ratchet_trail(
    initial: TrailPlan,
    bought: Decimal,
    followed: Decimal,
    spread: Decimal,
    settings: MoverConfig,
) -> TrailPlan | None:
    """The lead's T1 ratchet rule, paired with ATR or tier; no widening or spread-floor exemption.
    A fallback plan (cold ATR, raised to its spread floor or not, or an unusable spread) keeps its
    offsets."""
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
class ContextRead:
    """One late name's Day read; only the strategy thread installs its result or live minutes."""

    loaded_ns: int
    started_ns: int = 0
    future: Future | None = None
    reads: int = 0
    ready_ns: int | None = None
    failures: list[str] = field(default_factory=list)
    last_failure: str | None = None
    failed_ns: int = 0
    deferred: str | None = None
    live_day: date | None = None
    live: dict[int, entry_study.Minute] = field(default_factory=dict)


@dataclass
class HistoryLoad:
    """One slot opening's ATR warm-up (F-2): its bars request, and what its answers gave that slot."""

    instrument_id: InstrumentId
    requested_ns: int
    request_id: str | None = None
    """None when no request was sent: held at the rolling minute limit, or refused before it left."""
    held: bool = False
    """Held at the rolling minute limit and not sent yet: asked again each step while its slot is open."""
    cached: int = 0
    """Cached minute bars fed to the slot's ATR at the opening (`Mover._warm_atr`), before any answer."""
    from_cache: bool = False
    """The cache alone warmed the ATR at the opening, so nothing was asked."""
    completed: bool = False
    """The answer is complete or refused, or none was sent: a later answer for it warms nothing."""
    bars: int = 0
    """History bars this request fed the slot's ATR, each minute once."""
    dropped: int = 0
    """Bars of its answers not fed: another instrument's, a minute already fed, a minute from the slot's
    first live bar on, or any bar of an answer repeated or come after its slot closed."""
    late: bool = False
    """Unanswered after `HISTORY_WAIT_SECS`; an answer after that is still taken."""
    closed_first: bool = False
    """Its slot closed, or closed and opened again, before the answer came: the answer fed nothing."""
    reason: str | None = None
    """Why the slot is not warm from history, by name; None while waiting and when warm."""

    def outcome(self, atr_n: int) -> str:
        """Its `HISTORY_OUTCOMES` name: warm when its answer and the cache gave the ATR `atr_n` bars."""
        if self.from_cache:
            return "warm_from_cache"
        if self.reason is not None and self.reason.startswith(HISTORY_REFUSED):
            return "refused"
        if self.request_id is None:
            # never sent: held to the end, its slot closed first (`reason` says so), or let go once the
            # ATR was warm or the window wound down (`reason` says which); `reason` tells them apart
            return "held_at_limit"
        if self.closed_first:
            return "slot_closed_first"
        if self.completed:
            return "warm" if self.bars + self.cached >= atr_n else "fewer_than_atr_n"
        return "unanswered"


PRINT_AHEAD_MOST_NS = 5 * SECOND_NS
"""How far a print's venue time may run ahead of the stream's and still order it: 5 s, UNCALIBRATED,
the value of the adapter's `STAMP_AHEAD_MOST_NS` in its late-print setting, which lands with the
adapter's part and measures against receipt; it is to move to this reference with it, or a host
clock more than 5 s behind sets every print aside there. "Ahead" is measured on the venue's time as
the stream shows it (the lead's ruling of 2026-11-13, 08:49): a print's venue time less its receipt
here, less the same of its name's latest quotes (`Slot.quote_lag`, their upper median, so one quote
reported late or stamped ahead moves nothing), so this host's clock offset cancels, whatever its size. A print further
ahead has no usable time: it is neither the newest nor set aside, and `prints_stamped_ahead` counts it.
With no quote of the name yet, the venue's time cannot be had, and the print is measured against its
receipt as before; `prints_timed_on_receipt` counts each print so judged. A name whose every latest
quote runs more than 5 s behind its prints sets them aside as ahead until a timely quote comes: a
limit, counted in `prints_stamped_ahead`. NautilusTrader at the pin corrects no clock offset
(crates/common, data, live and core read none), so the rule is ours."""
CLOCK_QUOTES = 101
"""The latest quotes, of every name, whose venue time less their receipt on this host (`ts_event` less
`ts_init`) the run record summarises as `host_clock`: their median, with the least and most of every
quote. Quotes, not prints: a print reported late, as off-exchange prints are, reads below the clocks'
difference. A record only; the lead reads it past 2 s (`session_window.CLOCK_NOTE_MOST_MS`)."""
QUOTE_REFERENCE_QUOTES = 9
"""The latest quotes of a name whose upper median venue time less receipt is its reference
(`Slot.quote_lag`). A quote's delay only lowers that difference, so the upper median leans to the
timely quotes: with two, the timely one; with nine, up to four reported late or stamped ahead move
nothing. A step of this host's clock is followed within five quotes. UNCALIBRATED."""


def print_time(trade: Any, reference: int | None = None) -> int | None:
    """The venue's time, regardless of this host's clock, unless over the ahead bound
    (`PRINT_AHEAD_MOST_NS`): measured against its name's quotes, `reference` (`Slot.quote_lag`), else
    its receipt.

    TradeTick's `ts_event` is when the trade occurred and `ts_init` when the instance was created
    (crates/model/src/data/trade.rs:55-58 at the pin).
    """
    event = int(trade.ts_event)
    lag = event - int(trade.ts_init) - (0 if reference is None else reference)
    return None if lag > PRINT_AHEAD_MOST_NS else event


def quote_lag(quote: Any) -> int | None:
    """A quote's venue time less its receipt on this host, the reference `print_time` measures a print
    against; None with no quote."""
    return None if quote is None else int(quote.ts_event) - int(quote.ts_init)


class Print(NamedTuple):
    """One price with the two times its readers need. `event_ns` orders prints and compares with
    an entry fill, both at the venue. `ns`, the earlier of event and receipt, is persistence's own
    stamp: a print stamped ahead of receipt cannot hide the prints after it behind that watermark. With
    this host's clock behind the venue's, the receipt is read on the venue's clock through the name's
    quotes (`stamp_of`), so a print reported late still stamps below the newer ones."""

    ns: int
    price: Decimal
    event_ns: int

    @classmethod
    def of(cls, trade: Any, reference: int | None = None) -> Print:
        event = int(trade.ts_event)
        return cls(stamp_of(trade, reference), trade.price.as_decimal(), event)


def stamp_of(trade: Any, reference: int | None) -> int:
    """Persistence's stamp of a print (`Print`): the earlier of its venue time and its receipt. When
    its name's quotes (`reference`) show this host's clock behind the venue's, the receipt is read on
    the venue's clock; otherwise as on main, so with the clocks agreeing a quote's delay moves no
    stamp. With the clock behind, a print received faster than the name's quotes stamps that much
    before its venue time."""
    receipt = int(trade.ts_init) + (0 if reference is None else max(0, reference))
    return min(int(trade.ts_event), receipt)


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
    tightened: bool = False
    """A recorded tightening (ratchet, warmed ATR or profile) has taken the trigger from L: the tighter
    plan's trail places it from then on (the lead's ruling of 2026-11-12, 21:01)."""
    warm_checked: bool = False
    """Whether a cold-fallback holding has been compared once with its warmed ATR (the lead's (b))."""
    atr_warm_bars: int = 0
    """How many cached minute bars warmed the ATR when the slot opened (the lead's (a)); 0 for none."""
    entry_record: dict[str, Any] | None = None
    exit_record: ExitRecord | None = None
    last_bid: Decimal | None = None
    protection_anchor: Decimal | None = None
    holding_average: Decimal | None = None
    """The whole holding's valid average from `_held`, or None for a quantity-only reader."""
    held_stop: Decimal | None = None
    """With no followed price, the level a held stop sells through: the average fill, else the entry's
    limit, less the trail distance in force. Checked each step on the last trade, else the minute close."""
    held_since: int | None = None
    """When the held stop began: the entry's first fill, else the step that found no followed price."""
    entry_received_ns: int | None = None
    """This host's receipt of the entry's first fill (its `ts_init`), the clock the hold's bound reads;
    None for a holding this node did not see fill (a restart, or fills reconciled before the handler)."""
    held_low: Decimal | None = None
    """The lowest price of the holding received while the stop is held, between steps (`on_quote`'s
    bids under `BID_ASK`; under a print trigger `on_trade`'s prints of the holding, a late one too):
    the hold's end reads it beside the price the cache has at that step, so a price through the held
    stop and back before the step is not forgotten (the xr4 reader's F2)."""
    holding_from: int | None = None
    """While the stop is held, the venue time from which a print is of this holding, as the step last
    read it (`_holding_since`, in `_hold`), so the trade handler reads no engine attribute; None with
    no entry fill on record."""
    held_average: Decimal | None = None
    """The last average fill a hold step read (`_held_anchor`), which the exit record measures the kept
    level from; a step that reads none keeps it, so a later kept level is still measured from an
    average, never from a bid or a stale anchor (r-dsh-heldrec P2, P3; the fix's review, finding 1)."""
    held_at_l: Decimal | None = None
    """The highest level L under the average fill this hold has held (`_held_at_l`): a cheaper fill
    that lowers the average never lowers the held stop (the xr4 reader's F1; the lead's H3)."""
    floor_since: int | None = None
    """When the sale was first seen resting at its floor with the bid through it."""
    floor_released: bool = False
    """The floor was dropped after `FLOOR_WAIT_SECS`: later sales of this holding follow the bid."""
    trailed: bool = False
    """An emulated trailing sell has been placed for this holding: a later hold counts from its own start."""
    last_sale: Decimal | None = None
    """The price of this holding's last plain sale: a remake with no bid is never priced above it."""
    last_bar_closed_ns: int | None = None
    """The last accepted live interval end, independent of a run being consumed or a holding sold."""
    run_open_ns: int | None = None
    """Open of the complete minute, including the New York session date of a tape VWAP."""
    run_ns: int | None = None
    """When the last rising run completed (the bar's `ts_event`), until an entry has been decided on."""
    run_close: Decimal | None = None
    run_seen_ns: int | None = None
    """When the node had the bar that completed the run (its `ts_init`): the qualifying market event as
    the strategy can know it, from which an entry's delay is measured (`mover_window.entry_diagnostics`)."""
    run_closed_ns: int | None = None
    """When the minute of the run's close ended (`bar_closed_ns`): its close is the last trade before
    then, so a re-entry compares this, not the receipt time, with the sale fill (the batch A head
    reader, A-2 and A-3, 2026-11-13)."""
    run_arm: str | None = None
    """Only the opening candidate sets this; the off path preserves existing records."""
    entry_id: ClientOrderId | None = None
    entry_cancel_ns: int = 0
    protect_id: ClientOrderId | None = None
    """The one sell of this name: the trailing stop-limit, then the limit it became, or a plain limit."""
    adopted: bool = False
    """An own long attached without an entry: all its protective sales are reduce-only."""
    adoption_guard: tuple[int, int | None] | None = None
    """The adoption step and first followed observation's venue time (None before a positive price).
    A later step needs a strictly newer observation; timers and repeats cannot confirm the first."""
    exiting: bool = False
    """The trigger has been met, or the wind-down has begun: from here every sell is a plain limit."""
    floor: Decimal | None = None
    """The lowest price this position's sale is chased to (`chase_floor`), from its first released price
    or persistence confirmation reference. A sale that is refused or re-sent keeps it."""
    trail_trigger: Decimal | None = None
    """The trigger a trailing sell had reached when it was canceled to be made again: the next one starts
    from it, so the trigger never falls; with persistence on, the local confirmed trigger itself."""
    trail_limit: Decimal | None = None
    """The release limit that canceled trailing sell had reached, kept beside its trigger: the remade
    sell starts from it, so the price a release may sell at never falls either (Astra second read of
    #316, finding 1)."""
    trail_high: Decimal | None = None
    """The confirmed high with persistence on; the emulator owns it when both arms are off."""
    observations: deque[tuple[int, Decimal]] = field(default_factory=deque)
    """Trade events delivered since the last step, stored without reading engine attributes."""
    high_observations: deque[tuple[int, Decimal]] = field(default_factory=deque)
    breach_observations: deque[int] = field(default_factory=deque)
    breach_price: Decimal | None = None
    """The confirming print's price: a later recovery cannot lift the position's chase floor."""
    newest_print: Print | None = None
    """The trade newest by venue time among those cached when the slot opened and received since
    (`_newest_trade`): what the trigger follows, the held stop reads and stands in for a missing bid.
    The cache's last trade is the last received, which can be an older print reported late. This is
    the name's, not a holding's, so going flat does not clear it. Ties follow arrival order."""
    observed_ns: int = 0
    """Only strictly newer print times are read; duplicates, older prints and timers never count."""
    stamp_seen: int = 0
    """The highest persistence stamp (`Print.ns`) of every print received for the slot, kept, set aside
    or ahead: persistence's own watermark at arming, apart from the readers' venue-time order (P2)."""
    quote_lags: deque[int] = field(default_factory=lambda: deque(maxlen=QUOTE_REFERENCE_QUOTES))
    """The name's latest quotes' venue time less their receipt on this host (`on_quote`; the cache's
    quote at the opening first)."""
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
    profile_evaluated_ns: int | None = None
    """Open of the last complete minute considered for tightening, never the one-second step."""
    history: HistoryLoad | None = None
    """This opening's ATR warm-up, under a non-fixed trail (F-2); None under the fixed trail."""
    atr_minutes: set[int] = field(default_factory=set)
    """The minute opens (`bar_opened_ns`) the ATR has been fed, from history or live: none twice."""
    atr_bars: list[tuple[int, Bar]] = field(default_factory=list)
    """Every bar fed to the ATR by minute open, in time order, kept only while the warm-up is waiting:
    an answer after live bars resets the ATR and feeds it again in time order (F-2)."""
    first_live_open: int | None = None
    """The minute open of the slot's first live bar: history from that minute on is the live bars'."""

    @property
    def quote_lag(self) -> int | None:
        """The upper median of `quote_lags` (`QUOTE_REFERENCE_QUOTES`): the reference a print is measured
        against (`print_time`, `stamp_of`); None before any quote of the name."""
        return median_high(self.quote_lags) if self.quote_lags else None


def l_in_force(slot: Slot, bought: Decimal) -> Decimal | None:
    """L while it places the trailing sell's trigger: the entry was sized for it (S2), it has room
    under `bought`, and no recorded tightening has taken the trigger (`Slot.tightened`); else None,
    and the plan's trail places it (the lead's ruling of 2026-11-12, 21:01)."""
    if slot.tightened or fill_trigger(bought, slot.sized_loss) is None:
        return None
    return slot.sized_loss


def offsets_in_force(
    slot: Slot,
    bought: Decimal,
    trail: Decimal,
    exit_through: Decimal,
    floor_m: Decimal | None,
    basis: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """The offsets the working sale trails by (`offsets`): L's while L is in force, else the plan's.
    `_trail` sends these, and each tightening is taken only when it is closer than these. A tightening
    is taken against L at the average of its step, so once one has, the plan's trailing offset at a
    later, higher average is held at L, never over it (the high read of the ruling's fix, item 1).
    The plan's offset is never under the floor the plan in force took at its step, `floor_m` times the
    spread it was taken on, in price terms, so a later fill at a lower average does not cut it (the
    lead's O3 of 2026-11-12); `floor_m` is None in the fixed arm, which takes no floor.

    `basis` is the entry's own average fill (`entry_basis`), the price the plan's fraction was selected
    over, when the sale is placed from another price, the whole holding's average: the plan's trailing
    distance is then that fraction of its own basis, up to the step at the average, so a remake for a
    changed quantity trails by the distance in force, whatever tightened it (limit 20 of the last
    trailing landing). The limit offset's `exit_through` share is still of the average."""
    plan = slot.trail_plan or slot.initial_trail
    least = (
        None if floor_m is None or plan is None or plan.spread is None else floor_m * plan.spread
    )
    distance = l_in_force(slot, bought)
    if distance is None:
        own = basis if basis is not None and basis > 0 and basis != bought else None
        planned = (
            offsets(bought, trail, exit_through, least=least)[0]
            if own is None
            else offsets(own, trail, exit_through, least=least)[0].quantize(
                tick(bought), rounding=ROUND_CEILING
            )
        )
        loss = slot.sized_loss
        if (
            slot.tightened
            and loss is not None
            and fill_trigger(bought, loss) is not None
            and planned > loss
        ):
            distance = loss
        elif own is not None:
            distance = planned
    return offsets(bought, trail, exit_through, distance=distance, least=least)


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
        self.late_buys: dict[InstrumentId, ExitRecord] = {}
        """Holdings awaiting attachment to their late entry, including fills received with no slot.
        Keep their BUY and SELL totals before the next step, even if already flat by then (the
        canceled-fill contract, `crates/model/src/orders/mod.rs:247`; w3-r-latefill, F1 and F5)."""
        self.sold_rows: dict[InstrumentId, ExitRecord] = {}
        """The holding each name's last sale (`sold`) was marked for, so a late fill of its sale after the
        mark brings the sale's price up to date (`resold`; w3-t-latefill, P2)."""
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
        self.sale_rows: dict[str, dict[str, Any]] = {}
        # each sale's holding, so a late fill with no holding in the name now completes the record of
        # the holding that sent it (xr7-r-a3fix, P2)
        self.sale_owner: dict[str, ExitRecord] = {}
        """Each sale row by its client order ID, the same object as in its exit record's `sales`, so
        an order event handler sets its end with plain values."""
        self.remakes_under_floor = 0
        """The trailing sales remade (for a changed holding or a tightening) whose distance sent was
        under the spread floor at the remake's step (`spread_floor_m` times the quoted spread then): the lead's ruling
        (ii) of 2026-11-13 sends L or the plan's distance as before, and counts it; each such sale's
        row keeps that floor, L and the distance (`_remade`)."""
        self.protection_fallbacks = {"position average price": 0, "current bid": 0}
        self.protect_paths = {"held_stop": 0, "unpriced_exit": 0, "trail": 0}
        """How each held stop ended: sold through its level, sold after `HOLD_MOST_SECS`, or released."""
        self.late_prints: dict[str, dict[str, int]] = {}
        """Per symbol, prints older at the venue than one already kept: count and greatest lateness
        in whole milliseconds rounded up, so even under 1 ms reads 1."""
        self.prints_ahead: dict[str, int] = {}
        """Per symbol, prints over `PRINT_AHEAD_MOST_NS` ahead of the stream (`print_time`): neither
        newest nor late."""
        self.prints_on_receipt: dict[str, int] = {}
        """Per symbol, prints received (`on_trade`) and measured against their receipt for want of a
        quote of the name (`print_time`)."""
        self.quote_lags: deque[int] = deque(maxlen=CLOCK_QUOTES)
        """The latest quotes' venue time less their receipt, of every name (`CLOCK_QUOTES`)."""
        self.quote_lag_least: int | None = None
        self.quote_lag_most: int | None = None
        """The least and most venue time less receipt of every quote in the run, so a clock's step
        shows; one quote reported late sets them too."""
        self.refused: list[tuple[str, str]] = []
        self.order_refusals: list[dict[str, Any]] = []
        """Each order refusal in `refused`, with who refused it and its cause (the D3 terms, A7)."""
        self.refusals: list[dict[str, Any]] = []
        """Each entry the trail cap forbade, once, with its cause, distance, cap and time (the lead's
        record rule 1 of 2026-11-12), or a state re-entry missing its interval end, with its cause
        and time (R2, 2026-11-13), or missing pending post-sale history (sr-c-355, P2);
        `dropped` does not repeat it."""
        self.opening_refused: dict[str, str] = {}
        self.opening_outcomes: dict[str, int] = {}
        """How each opening entry ended: `filled` (the opening is spent), or `denied`, `rejected`,
        `expired` or `canceled` with nothing filled (the session's opening stays available)."""
        self.dropped: list[str] = []
        """Why each candidate entry was not sent, for the run's report."""
        self.sizing_refusals: dict[str, int] = dict.fromkeys(
            (*SIZING_REFUSALS, DISTANCE_UNDER_STEP, DISTANCE_UNDER_FLOOR, BID_AT_STOP), 0
        )
        """Each `sizing.plan_for_loss` refusal, counted by its own name (the lead's S2 requirement)."""
        self.sizing_bindings: dict[str, int] = {}
        """Each bound that held a sized entry (`held_by`), counted: the budget binding is counted apart."""
        self.quotes = 0
        self.trades = 0
        self.live_bars = 0
        self.history_bars = 0
        self.history_dropped = 0
        """History bars fed to the slots' ATRs and history bars not fed (`HistoryLoad.dropped`): a
        window's report reads these of every strategy as `warm_up_bars` and `warm_up_dropped`."""
        self.history_loads: list[HistoryLoad] = []
        """Every slot opening's warm-up, in order (F-2)."""
        self.history_by_request: dict[str, HistoryLoad] = {}
        self.history_requests = 0
        self.history_sent: deque[int] = deque()
        self.history_responses: list[tuple[str, bool, str | None, list[Bar]]] = []
        """Each warm-up answer since the last step, as plain values: request ID, completed, failure."""
        self.context_reader: Callable[[InstrumentId, int], entry_study.Day] | None = None
        """Composition's history loader, called off the event loop on one context worker."""
        self.context_submit: Callable[[Callable[[], Any]], Future] | None = None
        """Optional deterministic executor for simulations; otherwise the strategy's one-worker pool."""
        self.context_stop: Callable[[], None] | None = None
        """Stop the reader's next HTTP attempt, including another page of a read still running."""
        self.context_budget_record: Callable[[], dict[str, Any]] | None = None
        self.context_reads: dict[InstrumentId, ContextRead] = {}
        self.context_sources: dict[InstrumentId, str] = {}
        self.context_sent: deque[int] = deque()
        self._context_executor: ThreadPoolExecutor | None = None
        self._contexts_stopped = False
        self.entry_contexts: dict[InstrumentId, entry_study.Day] = {}
        """Explicit per-name ADR 0092 history, supplied by composition before registered-arm entry.

        A Day names its New York date, reference close, earlier sessions latest first, and covered
        bars since 04:00 (the study's Day contract). Callers supply complete history, with `missing`
        naming any coverage fault; absent contexts use the optional reader, and wrong-name/day or
        missing contexts never qualify. `covered_to` must reach the incoming minute's open: after a
        data gap the caller supplies the missing prefix or marks it missing. Completed live bars extend coverage
        to that minute's end and replace that minute in the context; future minutes
        are discarded, and pre-watch history informs listing/extension only, never the rising run.
        Context survives unwatching so the listing clock is the day's, not a new slot's.
        """
        self.sold: dict[InstrumentId, tuple[int, Decimal | None]] = {}
        """When each name was last sold, and at what price: the name's, until its next sale, so that
        neither a slot closed and opened again nor an entry that never filled loses its sale state."""
        self.sold_fill_ns: dict[InstrumentId, int] = {}
        """The actual last sale fill, retained across unwatching for state and elapsed-minute records."""
        self.reentry_states: dict[InstrumentId, ReentryState] = {}
        self.opening_states: dict[InstrumentId, OpeningState] = {}
        self.session_vwaps: dict[InstrumentId, dict[int, Decimal]] = {}
        """Tape-supplied session VWAPs keyed by completed bar ts_init. Live composition leaves empty;
        no OHLC substitute, rolling approximation or last-known VWAP is used for this state arm."""
        self.baseline_dollar_volume_per_minute: dict[InstrumentId, Decimal] = {}
        """The ranking's startup normal, supplied by composition: a stand-in for size, not market cap."""
        self.trail_profile: dict[str, Any] | None = None
        """The strict T5 document, supplied by composition; profile refuses entries without it."""
        self.rdc_1: dict[InstrumentId, Decimal] = {}
        """Composition's latest RDc_1 ratios; absent ratios explicitly take profile's first bucket."""
        self.notes: dict[InstrumentId, list[str]] = {}
        """What was noted of each name, for the run's report; kept when its slot is closed."""
        self.entries_sent: list[dict[str, Any]] = []
        """Each entry sent, as plain values: its client order ID, its instrument, when the node had the
        bar that qualified it (`qualified_ns`) and that bar's close, and the order's creation time
        (`submitted_ns`), both on the node's clock. A window reads its diagnostics from these and its
        own bars (`mover_window.entry_diagnostics`)."""
        self.entries_off: list[dict[str, Any]] = []
        """Each buy decision the entry switch stopped: its time, name, kind and what was read."""
        self.arming_warned: dict[InstrumentId, str] = {}
        """The read each name was last warned of; a change of what is read warns again."""
        self.arming_warnings: list[tuple[str, str]] = []
        self.winding_down = False
        self.sent: deque[tuple[int, int]] = deque()
        """(time, the adapter's request total) at each step of the last 60 seconds."""
        self.recent = 0
        """The step's sampled adapter count over 60 seconds, plus venue commands since that sample
        and outstanding instrument-GET reserves, held against `soft_line`."""

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
        """Only retain the response; the next step checks the cache and subscribes outside reentry.
        A warm-up's answer is kept apart, with its bars, for the step to feed (`_finish_history`)."""
        request_id = str(response.correlation_id)
        if request_id in self.history_by_request:
            bars = getattr(response, "data", None) or []
            self.history_responses.append(
                (request_id, bool(response.completed), response.failure_reason, list(bars))
            )
            return
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
        if event.order_side == OrderSide.BUY:
            opening_ended(self.opening_states, self.opening_outcomes, event, "filled")
        quantity = event.last_qty.as_decimal()
        # Each fill reaches its own order's rows by client order ID, whatever the slot holds now (the
        # pre-landing reader of A3 with O, P2-1, P2-2): an entry's from `entries_sent`, so a late fill
        # of an earlier entry lands on that entry's record; a sale's from `sale_rows`, so a fill after
        # its holding's record is cleared (`_flat`) or its slot removed (`_close_slot`) still lands
        sale: dict[str, Any] | None = None
        if event.order_side == OrderSide.BUY:
            entry_filled(self.entries_sent, self.fills, event.client_order_id.value)
        else:
            sale = self.sale_rows.get(event.client_order_id.value)
            if sale is not None:
                filled = Decimal(sale["filled_quantity"]) + quantity
                sale["filled_quantity"] = str(filled)
                # a canceled or expired order can still fill ("Real world possibility", pinned
                # `crates/model/src/orders/mod.rs:247`, `:295`): filled in full, it ended filled
                ended = sale["ended"] in (None, "canceled", "expired")
                if ended and filled >= Decimal(sale["quantity"]):
                    sale["ended"] = "filled"
                    sale["ended_ns"] = max(
                        at
                        for cid, side, _, _, at in self.fills
                        if cid == event.client_order_id.value and side == OrderSide.SELL.name
                    )

        slot = self.slots.get(event.instrument_id)
        late = getattr(self, "late_buys", {}).get(event.instrument_id)
        # A sale fill sells the slot's holding, so a canceled leg's late fill of a sold holding is
        # charged to that holding (code-review-l5-latebuy/4).
        current = late if slot is None else slot.exit_record
        # The slot's own entry is excluded while no late buy waits: its remainder after a first part
        # sold joins the slot's holding, and `_time_out` cancels it at once once the sale is released
        # (code-review-l5-latebuy/2). Once a late buy waits, every buy joins it, so the sold holding
        # stays sold and one order's shares are in one record (code-review-l5-latebuy2/F1).
        completed = (
            slot is not None
            and _sold_out(slot.exit_record)
            and (event.client_order_id != slot.entry_id or late is not None)
        )
        if event.order_side == OrderSide.BUY and (slot is None or completed):
            # Plain accounting only: subscriptions wait for the timer. A sale callback can arrive
            # before that timer too, so an order ID alone would lose the intervening holding (F5).
            # A complete sale followed by a buy is a new holding even before `_flat` runs
            # (r-l5comp-ag/P1-1). Keep the old row intact until the timer records and clears it.
            row = late
            if row is None:
                row = ExitRecord(event.instrument_id, event.client_order_id.value, Decimal(0))
                self.late_buys[event.instrument_id] = row
            late_receipt(row, event)
            row.quantity += quantity
            row.bought_notional += quantity * event.last_px.as_decimal()
            row.final_fill_ns = None
            return
        if event.order_side == OrderSide.SELL and current is None:
            # no holding in the name now: the fill completes the totals of the holding that sent the
            # sale, as a record only (xr7-r-a3fix, P2); no trading state is written for a holding the
            # slot no longer has
            owner = self.sale_owner.get(event.client_order_id.value)
            if owner is not None:
                # The holding's totals take no more than it held (`take_sale`). The order's own row
                # above and `fills` keep the whole fill, and the excess is named.
                take = owner.take_sale(quantity, event.last_px.as_decimal(), event.ts_event)
                name_oversale(self.dropped, event, quantity - take, owner.quantity)
                if take > 0 and self.sold_rows.get(event.instrument_id) is owner:
                    # the holding was marked sold (`_flat`) before this fill came: its sale's price
                    # is the holding's average with it, so the above-sale rule reads the price the
                    # shares sold at under every arm (w3-t-latefill, P2)
                    resold(
                        self.sold,
                        self.sold_fill_ns,
                        self.reentry_states,
                        event.instrument_id,
                        owner,
                    )
        if slot is None and current is None:
            return
        if event.order_side == OrderSide.BUY and slot is not None:
            # NautilusTrader d53849b216e2: OrderFilled's venue/event and initialization clocks are
            # distinct (crates/model/src/events/order/filled.rs:78-81); the live client supplies
            # its host clock for ts_init (python/nautilus_trader/live/clients.py:585-613). Keep the
            # first receipt of the slot's entry for the hold, without changing the venue times in `fills`.
            if (
                slot.entry_received_ns is None
                and slot.entry_id is not None
                and event.client_order_id.value == slot.entry_id.value
            ):
                received = getattr(event, "ts_init", None)
                slot.entry_received_ns = None if received is None else int(received)
            row = (
                self._ensure_exit(slot, Decimal(0))
                if slot.exit_record is None
                else slot.exit_record
            )
            if slot.entry_id is None or (
                slot.entry_id != event.client_order_id and current is None
            ):
                # Ranking reopened this name first, possibly with a newer unfilled entry (F1).
                # Tie the holding to the fill now; recover its plan on the timer, without losing
                # the newer order's cancellation tracking.
                row.entry_client_order_id = event.client_order_id.value
                late_receipt(row, event)
                self.late_buys[event.instrument_id] = row
            elif (
                slot.entry_id == event.client_order_id
                and slot.entry_record is not None
                and slot.entry_record.get("client_order_id") != event.client_order_id.value
            ):
                # The slot's own newer entry fills after a late buy's recovery gave the slot that
                # buy's record and plan: it takes back its own (r-latefill-delta2/F2)
                self._take_entry_plan(slot, event.client_order_id.value)
            row.quantity += quantity
            row.bought_notional += quantity * event.last_px.as_decimal()
            row.final_fill_ns = None
            # The same holding still includes its earlier sale fills; a new holding gets a new row.
            plan = slot.trail_plan
            # the distance the sale trails by: L's while L is in force (the high read of the
            # ruling's fix, item 2), so a sale already due at the next step keeps it in the record
            row.trail_distance = offsets_in_force(
                slot,
                row.bought_notional / row.quantity,
                self.settings.trail if plan is None else plan.trail,
                self.settings.exit_through if plan is None else plan.exit_through,
                self._floor_m(),
            )[0]
            if slot.last_bid is not None:
                row.peak = max(row.peak or slot.last_bid, slot.last_bid)
        elif current is not None:
            # every sale fill in the name sells the current holding, an earlier holding's late one
            # too: the shares are one position, and its totals are what the re-entry state and
            # `_flat` read (xr7-r-a3fix, P1). The sale's own row above names the order; the earlier
            # holding's totals are not added to as well, which would count the shares twice
            row = current
            # no more than the holding holds, at its latest fill time (`take_sale`): a canceled
            # leg's late fill after another leg sold it all is not counted again (w3-t-latefill, P3)
            take = row.take_sale(quantity, event.last_px.as_decimal(), event.ts_event)
            name_oversale(self.dropped, event, quantity - take, row.quantity)
            # inline rather than `_sold_out`, so the checker keeps `final_fill_ns` narrowed
            if (
                take > 0
                and row.filled >= row.quantity
                and row.final_fill_ns is not None
                and self.settings.reentry_arm != "clock"
            ):
                sold = row.notional / row.filled
                self.sold[event.instrument_id] = (row.final_fill_ns, sold)
                self.sold_fill_ns[event.instrument_id] = row.final_fill_ns
                state = self.reentry_states[event.instrument_id] = ReentryState(
                    row.final_fill_ns, sold, row.trail_distance
                )
                state.observe_pending(row)

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
        self.order_refusals.append(refusal_row(event, "before_venue"))
        opening_ended(self.opening_states, self.opening_outcomes, event, "denied")
        sale_ended(self.sale_rows, event, "denied", str(event.reason))

    def on_order_rejected(self, event: Any) -> None:
        self.refused.append((event.client_order_id.value, str(event.reason)))
        self.order_refusals.append(refusal_row(event, "venue"))
        opening_ended(self.opening_states, self.opening_outcomes, event, "rejected")
        sale_ended(self.sale_rows, event, "rejected", str(event.reason))

    def on_order_expired(self, event: Any) -> None:
        opening_ended(self.opening_states, self.opening_outcomes, event, "expired")
        sale_ended(self.sale_rows, event, "expired")

    def on_order_canceled(self, event: Any) -> None:
        opening_ended(self.opening_states, self.opening_outcomes, event, "canceled")
        sale_ended(self.sale_rows, event, "canceled")

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
        row = slot.exit_record
        trailing = getattr(order, "trailing_offset", None)
        limit_offset = getattr(order, "limit_offset", None) if trailing is not None else None
        price = None if trailing is not None else order.price
        spread = quoted_spread(self.cache.quote(slot.instrument_id))
        floor_m = self._floor_m()
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
        self.sale_rows[sale["client_order_id"]] = sale
        if row is not None:
            row.sales.append(sale)
            self.sale_owner[sale["client_order_id"]] = row

    def on_quote(self, quote: Any) -> None:
        self.quotes += 1
        # the clocks' difference, in plain attributes: a handler may run while the strategy is borrowed
        lag = int(quote.ts_event) - int(quote.ts_init)  # as `quote_lag`, for a quote that is there
        self.quote_lags.append(lag)
        least, most = self.quote_lag_least, self.quote_lag_most
        self.quote_lag_least = lag if least is None else min(least, lag)
        self.quote_lag_most = lag if most is None else max(most, lag)
        slot = self.slots.get(quote.instrument_id)
        if slot is not None:
            slot.quote_lags.append(lag)
            bid = quote.bid_price.as_decimal()
            if bid > 0:  # a one-sided quote's zero bid is no price: keep the last real one
                slot.last_bid = bid
                if slot.held_since is not None and self.settings.trigger == TriggerType.BID_ASK:
                    slot.held_low = bid if slot.held_low is None else min(slot.held_low, bid)
            row = slot.exit_record
            if bid > 0 and row is not None and row.filled < row.quantity:
                row.peak = max(row.peak or bid, bid)

    def on_trade(self, trade: Any) -> None:
        self.trades += 1
        slot = self.slots.get(trade.instrument_id)
        if slot is None:
            return
        event = int(trade.ts_event)
        name = str(trade.instrument_id)
        lag = slot.quote_lag
        if lag is None:
            self.prints_on_receipt[name] = self.prints_on_receipt.get(name, 0) + 1
        stamp = stamp_of(trade, lag)
        price = trade.price.as_decimal()
        slot.stamp_seen = max(slot.stamp_seen, stamp)
        if self.persistence and slot.trail_trigger is not None and not slot.exiting:
            # Persistence keeps its own receipt-clamped stamp and watermark, independently of
            # the readers' venue-time order. Even ahead and late prints reach that watermark.
            slot.observations.append((stamp, price))
        kept = slot.newest_print
        if print_time(trade, lag) is None:
            self.prints_ahead[name] = self.prints_ahead.get(name, 0) + 1
            return
        late_print = kept is not None and event < kept.event_ns
        if late_print:
            late = self.late_prints.setdefault(name, {"count": 0, "most_late_ms": 0})
            late["count"] += 1
            late["most_late_ms"] = max(
                late["most_late_ms"], -(-(kept.event_ns - event) // 1_000_000)
            )
        else:
            slot.newest_print = Print(stamp, price, event)
        if slot.held_since is not None and self.settings.trigger == TriggerType.LAST_PRICE:
            # the hold's low is the lowest print of this holding (`_holding_since`, as
            # `_holding_trade`) while its stop is held, whatever order the prints arrive in: one set
            # aside as late counts, though it is never the newest price nor prices a sale (the lead's
            # ruling of 2026-11-13, F1 (b)); one with no usable time has no place in that order. With
            # no entry fill on record when the holding began cannot be read, so no print counts: one
            # from before it would stay in the low once the fill is recorded
            since = slot.holding_from
            if since is not None and event >= since:
                slot.held_low = price if slot.held_low is None else min(slot.held_low, price)

    def record(self) -> dict[str, Any]:
        """What a run's report keeps of the strategy, in plain values: its rows, the ranking it last
        had, each fill, each order refused, the orders the emulator released, and each slot's notes."""
        record = {
            "rows": {
                # Multi-value rows keep their shape in JSON, with each decimal written as text.
                name: [str(part) for part in value]
                if isinstance(value, tuple)
                else {str(key): str(part) for key, part in value.items()}
                if isinstance(value, dict)
                else str(value)
                for name in OWN_FIELDS
                if name != "max_shares" or not self.settings.size_from_budget
                if name not in ARMING_FIELDS
                and (self.settings.opening_arm != "off" or name not in OPENING_FIELDS)
                for value in (getattr(self.settings, name),)
            },
            "arming": {
                "path": self.settings.arming_path,
                "absent_is_on": self.settings.arming_absent_is_on,
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
            "entries_off": [dict(row) for row in self.entries_off],
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
            "remakes_under_floor": self.remakes_under_floor,
            "instrument_request_limit": INSTRUMENT_REQUEST_LIMIT,
            "history_requests": self.history_requests,
            "history_request_limit": HISTORY_REQUEST_LIMIT,
            "warm_up": self._history_record(),
            "entries_by_trail_source": {
                source: sum(
                    1
                    for entry in self.entries_sent
                    if (entry.get("trail_source") or "unrecorded") == source
                )
                for source in dict.fromkeys(
                    entry.get("trail_source") or "unrecorded" for entry in self.entries_sent
                )
            },
            "instrument_request_reserve": INSTRUMENT_REQUEST_RESERVE,
            "dropped": list(self.dropped),
            "sizing_refusals": dict(self.sizing_refusals),
            "sizing_bindings": dict(self.sizing_bindings),
            "rank_faults": self.rank_faults,
            "fills": [list(fill) for fill in self.fills],
            "refused": [list(refused) for refused in self.refused],
            "order_refusals": [dict(refusal) for refusal in self.order_refusals],
            "refusals": [dict(refusal) for refusal in self.refusals],
            "released": sorted(self.released),
            "protection_fallbacks": dict(self.protection_fallbacks),
            "protect_paths": dict(self.protect_paths),
            "late_prints_set_aside": {name: dict(row) for name, row in self.late_prints.items()},
            "prints_stamped_ahead": dict(self.prints_ahead),
            "prints_timed_on_receipt": dict(self.prints_on_receipt),
            "host_clock": self._host_clock(),
            "sell_triggers": dict(self.sell_triggers),
            "trail_notes": self._trail_notes(),
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
        if self.context_reader is not None or self.context_reads:
            record.update(
                context_read_limit=CONTEXT_READ_LIMIT,
                context_request_budget=(
                    None if self.context_budget_record is None else self.context_budget_record()
                ),
                on_demand_contexts={
                    str(instrument): {
                        "source": self.context_sources.get(instrument),
                        "loaded_ns": read.loaded_ns,
                        "ready_ns": read.ready_ns,
                        "seconds": self._context_seconds(instrument),
                        "reads": read.reads,
                        "pending": read.future is not None,
                        "deferred": read.deferred,
                        "failures": list(read.failures),
                        "last_failure": read.last_failure,
                    }
                    for instrument, read in self.context_reads.items()
                },
            )
        if self.settings.opening_arm != "off":
            record["opening_refused"] = dict(self.opening_refused)
            record["opening_outcomes"] = dict(self.opening_outcomes)
        return record

    # --- start, bars, the step -------------------------------------------------------------------------

    def on_start(self) -> None:
        for instrument_id in self.entry_contexts:
            self.context_sources.setdefault(instrument_id, "roster")
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
        closed = bar_closed_ns(bar)
        if slot.last_bar_closed_ns is not None and closed <= slot.last_bar_closed_ns:
            return
        slot.last_bar_closed_ns = closed
        self.live_bars += 1
        if slot.atr is not None:
            opened = bar_opened_ns(bar)
            if slot.first_live_open is None:
                slot.first_live_open = opened
            if opened not in slot.atr_minutes:  # a minute the warm-up already fed is not fed twice
                slot.atr_minutes.add(opened)
                if slot.history is not None and not slot.history.completed:
                    slot.atr_bars.append((opened, bar))
                slot.atr.handle_bar(bar)
        slot.run_open_ns = bar_opened_ns(bar)
        close = bar.close.as_decimal()
        if self.settings.opening_arm == "on":
            if slot.run_arm == "opening":
                # A run not decided on before this bar stays opening-only, never a bridge run.
                slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None
            slot.run_arm = None
            self._sync_opening(slot, bar.ts_init)
            self._observe_opening(slot, bar)
        before = slot.closes[-1] if slot.closes else None
        slot.closes.append(close)
        slot.dollars.append(close * bar.volume.as_decimal())
        if (
            self.settings.trail_mode == "profile"
            and not self.winding_down
            and (self.settings.protect or slot.exiting)
            # as on the step path: while upstream's market exit runs it alone sells
            and not self.is_exiting()
        ):
            order = self._order(slot.protect_id)
            if order is not None and order.is_emulated and (held := self._held(slot)) > 0:
                # Each complete minute takes the existing protection path even if several bars
                # arrive before the next timer step. The per-slot bar mark prevents step repeats.
                self._protect(slot, held, bar.ts_init)
        state = self.reentry_states.get(slot.instrument_id)
        row = slot.exit_record
        vwap = self.session_vwaps.get(slot.instrument_id, {}).get(bar.ts_init)
        day = datetime.fromtimestamp(slot.run_open_ns / SECOND_NS, NEW_YORK).date()
        if row is not None and (row.filled < row.quantity or self.settings.reentry_arm == "clock"):
            # The clock arm creates its record state at _flat, even if the fill arrived first.
            pending = row.pending_reentry_closes
            if len(pending) == PENDING_REENTRY_BARS:
                row.lost_reentry_close_ns = pending[0][0]
            pending.append((closed, close, vwap, day))
        elif state is not None and closed > state.sale_ns:  # a close after the sale (A-2)
            state.observe(close, vwap, day)
        if self.settings.entry_arm != "bridge":
            self._registered_run(slot, bar)
            self._opening_run(slot, bar)
            return
        if slot.instrument_id in self.sold and self.settings.reentry_arm in ("turn", "vwap"):
            # Turn's k and VWAP's crossing replace the first entry's rising-run length; the entry
            # check still enforces state, ranking, eligibility, free slots and request budget.
            slot.run_ns, slot.run_close, slot.run_seen_ns = bar.ts_event, close, bar.ts_init
            slot.run_closed_ns = closed
            self._opening_run(slot, bar)  # name sale refusals without replacing the re-entry run
            return
        # Each accepted bar replaces the bridge candidate, as _registered_run does (sr-a-355, P3).
        slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None
        rising = 0  # the closes in a row, to this one, each above the one before
        for later, earlier in pairwise(reversed(slot.closes)):
            if not later > earlier:
                break
            rising += 1
        wanted = self.settings.run_bars
        if rising >= wanted:
            slot.run_ns, slot.run_close, slot.run_seen_ns = bar.ts_event, close, bar.ts_init
            slot.run_closed_ns = closed
            self._say(f"{slot.instrument_id}: run qualified, {wanted} rising closes to {close}")
        elif before is not None and rising == 0:
            self._say(f"{slot.instrument_id}: run restarted, {close} is not above {before}")
        else:
            after = "" if before is None else f" after {before}"
            self._say(
                f"{slot.instrument_id}: run building, {rising} of {wanted} rising closes "
                f"({close}{after})"
            )
        self._opening_run(slot, bar)

    def _sync_opening(self, slot: Slot, now: int) -> None:
        """Observe this part's opening minutes from the node's cached bars of the slot's own series
        (`slot.bar_type`, no request), oldest first; `Cache.bars` returns newest first at the
        NautilusTrader pin. Minutes already observed are not observed twice.

        One series decides the turn, the one the slot trades on (the lead's ruling on review finding
        (2), 2026-11-12): another source's bar for the same minute (the rank actor's EXTERNAL bars when
        the slot trades INTERNAL) never stands in for the slot's own. An INTERNAL slot opened after
        09:30 has no 09:30 bar of its series, so it has no opening coverage and enters by no opening.
        """
        if self.settings.opening_arm != "on" or self.settings.extended_hours:
            return
        cached = self.cache.bars(slot.bar_type)
        state = self.opening_states.get(slot.instrument_id)
        today = datetime.fromtimestamp(now / SECOND_NS, NEW_YORK).date()
        start = int(datetime.combine(today, time(9, 30), NEW_YORK).timestamp()) * SECOND_NS
        since = state.last_open_ns if state is not None and state.day == today else start - 1
        fresh = []
        for bar in cached or []:
            opened = bar_opened_ns(bar)
            if opened <= since:
                break
            if bar.ts_init <= now:
                fresh.append((opened, bar))
        for opened, bar in reversed(fresh):
            self._observe_opening(slot, bar, opened)

    def _observe_opening(self, slot: Slot, bar: Bar, opened_ns: int | None = None) -> None:
        """Observe complete minutes from the actual 09:30 bar; a gap invalidates this session.

        Upstream rc8 aggregation.rs:1735 stamps INTERNAL bars' close in ts_init; EXTERNAL Alpaca
        bars use their open in ts_event (model/__init__.pyi:112-141; adapter parse_bar).
        `opened_ns` is the bar's open, the slot's `run_open_ns` for its own bars.
        """
        if opened_ns is None:
            opened_ns = slot.run_open_ns
        if self.settings.extended_hours or opened_ns is None:
            return
        if bar.bar_type.is_internally_aggregated() and opened_ns < slot.subscribed_ns:
            return  # a partial first INTERNAL minute, as `_registered_run` skips it: no 09:30 open
        opened = datetime.fromtimestamp(opened_ns / SECOND_NS, NEW_YORK)
        minute = opened.hour * 60 + opened.minute
        if not REGULAR_OPEN_MINUTE <= minute < REGULAR_OPEN_MINUTE + self.settings.opening_minutes:
            return
        state = self.opening_states.get(slot.instrument_id)
        if state is not None and state.day != opened.date():
            del self.opening_states[slot.instrument_id]
            state = None
        if state is None:
            if minute != REGULAR_OPEN_MINUTE:
                self._note(slot, f"{slot.instrument_id}: opening: no complete 09:30 open observed")
                return
            state = OpeningState(
                opened.date(), bar.open.as_decimal(), minute, opened_ns - 60 * SECOND_NS
            )
            self.opening_states[slot.instrument_id] = state
        if opened_ns <= state.last_open_ns:
            return  # no duplicate or out-of-order minute can count as another rising close
        if opened_ns != state.last_open_ns + 60 * SECOND_NS:
            state.complete = False
            self._note(slot, f"{slot.instrument_id}: opening: missing minute coverage since 09:30")
        state.last_open_ns = opened_ns
        low, close = bar.low.as_decimal(), bar.close.as_decimal()
        if state.low is None or low < state.low or close <= state.low:
            state.low_minute = minute  # the minute `observe_turn` anchors the turn to
        state.observe_turn(close, low)
        state.turn_ns, state.turn_minute = bar.ts_init, minute

    def _opening_refusal(self, slot: Slot, now: int) -> str | None:
        settings = self.settings
        state = self.opening_states.get(slot.instrument_id)
        current = datetime.fromtimestamp(now / SECOND_NS, NEW_YORK)
        minute = current.hour * 60 + current.minute
        if settings.opening_arm != "on" or settings.extended_hours or not settings.protect:
            return "opening is off or outside the protected regular-session arm"
        if not REGULAR_OPEN_MINUTE <= minute < REGULAR_OPEN_MINUTE + settings.opening_minutes:
            return "opening window has ended"
        if state is None or state.day != current.date() or not state.complete:
            return "opening has no complete coverage from 09:30"
        if state.used:
            return "opening entry already used this session"
        if not self._top_tier(slot):
            return "opening requires the top liquidity tier"
        if (sold := self.sold.get(slot.instrument_id)) is not None:
            from .movers_rank_actor import trade_date

            # the lead (2026-11-12): the trade date, with its 20:00 rollover, as the carry and the
            # baselines use; a name sold on it comes back only through the re-entry arm. Checked
            # last here, so a record of it names an opening only the sale refused (the ranking and
            # the fall are the entry check's, later).
            if trade_date(self.sold_fill_ns.get(slot.instrument_id, sold[0])) == state.day:
                return SOLD_THIS_TRADE_DATE
        return None

    def _top_tier(self, slot: Slot) -> bool:
        """Whether the name's baseline dollar volume per minute is known, finite and at the tier cut."""
        baseline = self.baseline_dollar_volume_per_minute.get(slot.instrument_id)
        return (
            baseline is not None
            and baseline.is_finite()
            and baseline >= self.settings.liquidity_tier_cut
        )

    def _opening_run(self, slot: Slot, bar: Bar) -> None:
        if self.settings.opening_arm != "on":
            return
        state = self.opening_states.get(slot.instrument_id)
        if state is None or state.turn_ns != bar.ts_init or state.rises < self.settings.turn_k:
            return
        if (why := self._opening_refusal(slot, bar.ts_init)) is not None:
            if why == SOLD_THIS_TRADE_DATE:  # named even when the re-entry arm holds the run
                self.opening_refused[str(slot.instrument_id)] = why
            return
        if slot.run_ns is not None:
            return  # an existing arm keeps its qualifying close
        slot.run_ns, slot.run_close, slot.run_seen_ns = (
            bar.ts_event,
            bar.close.as_decimal(),
            bar.ts_init,
        )
        slot.run_closed_ns = bar_closed_ns(bar)
        slot.run_arm = "opening"

    def _registered_run(self, slot: Slot, bar: Bar) -> None:
        """Use the study's listing clock, rising run and extension rule on this bar's known prefix."""
        from . import early_entry_study as entry_study

        slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None
        arm = self.settings.entry_arm
        rows = entry_study.ARMS[arm]
        if slot.instrument_id in self.sold and self.settings.reentry_arm == "turn":
            rows = replace(rows, run_bars=self.settings.turn_k)
        # At the pin internal time bars' ts_init is the close regardless of ts_event configuration
        # (aggregation.rs:1735); adapter EXTERNAL bars' ts_event is their open (parse_bar).
        opened_ns = bar_opened_ns(bar)
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
        if self.context_reader is not None:
            self._finish_contexts()
            context = self.entry_contexts.get(slot.instrument_id)
        if (
            context is not None
            and context.day == opened.date()
            and context.symbol == slot.instrument_id.symbol.value
            and context.missing is None
            and self.context_sources.get(slot.instrument_id) == "on_demand"
        ):
            context = self._fill_context_gap(slot.instrument_id, context, opened_ns, bar.ts_init)
        why = None
        if context is None:
            why = self._context_pending(slot.instrument_id) or "no entry context supplied"
        elif context.symbol != slot.instrument_id.symbol.value or context.day != opened.date():
            why = (
                self._context_pending(slot.instrument_id, refresh=True)
                if self.context_sources.get(slot.instrument_id) == "on_demand"
                else None
            ) or "entry context names another symbol or New York day"
        elif context.missing is not None:
            why = f"entry context missing: {context.missing}"
        elif not entry_study.PART_START <= minute < entry_study.WINDOW_END:
            why = "outside the entry study's 04:00-16:00 New York window"
        elif context.covered_to < minute:
            why = (
                f"entry context coverage gap at minute {context.covered_to}; "
                "waiting for complete cached minute bars"
                if self.context_sources.get(slot.instrument_id) == "on_demand"
                else "entry context coverage does not reach this minute's open"
            )
        live = entry_study.Minute(
            minute,
            float(bar.open.as_decimal()),
            float(bar.high.as_decimal()),
            float(bar.low.as_decimal()),
            float(bar.close.as_decimal()),
            float(bar.volume.as_decimal()),
        )
        if why is not None:
            read = self.context_reads.get(slot.instrument_id)
            if read is not None and entry_study.PART_START <= minute < entry_study.WINDOW_END:
                if read.live_day != opened.date():
                    read.live, read.live_day = {}, opened.date()
                read.live[minute] = live
            self._note(slot, f"{slot.instrument_id}: entry arm {arm}: {why}")
            return
        assert context is not None
        assert slot.entry_start is not None
        known = {b.start: b for b in context.bars if b.start <= minute}
        known[minute] = live
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
        runs = entry_study.entry_runs(day, rows, start=slot.entry_start)
        if slot.instrument_id in self.sold and self.settings.reentry_arm == "vwap":
            listed_at = entry_study.first_listed(seen)
            assert listed_at is not None  # the latest minute is listed above
            # VWAP's crossing replaces the run, while the shared extension/no-chase checks remain.
            runs = iter([(len(day.bars) - 1, (day.bars[-1],), listed_at)])
        for index, run, listed_at in runs:
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
            slot.run_closed_ns = bar_closed_ns(bar)
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
        self.recent = self._recent_requests(now)
        self._ask_for_the_account()
        self._say_ranking(now)
        self._watch(now)
        self._finish_history(now)
        # Reserve every possible retry while the asynchronous GET is outstanding. Its actual HTTP
        # attempts already enter requests_sent through the provider's existing transport, not here.
        self.recent += self._instrument_reserve()
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
        self._contexts_stopped = True
        if self.context_stop is not None:
            self.context_stop()
        if self._context_executor is not None:
            self._context_executor.shutdown(wait=False, cancel_futures=True)
            self._context_executor = None
        self._finish_contexts()
        for instrument_id, read in self.context_reads.items():
            future = read.future
            if future is None:
                continue
            if future.done():
                self._finish_contexts()
                continue
            state = "cancelled at stop" if future.cancel() else "running at stop"
            read.future = None
            self._context_failed(instrument_id, read, state, self.clock.timestamp_ns())
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
        """Read this strategy's signed open quantity and whole-holding average together."""
        positions = self.cache.positions_open(
            instrument_id=slot.instrument_id, strategy_id=self.strategy_id
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

    def adopt_holding(self, instrument_id: InstrumentId, level: Price) -> None:
        """Attach this strategy's cached open long and protect it from the supplied level.

        Cache.positions_open filters by strategy (rc8 common/__init__.pyi:568-576).
        The existing arming path uses OrderFactory.trailing_stop_limit's trigger_price,
        activation_price and reduce_only (same file:1523-1547). A breached first quote
        rests below activation until a fresh observation confirms it, or price recovers
        above the level (emulator.rs:1721-1782); trailing.rs:58-100 only raises the stop.
        Persistence must be off: adoption never replaces its configured confirmation counts.
        Carry runs on the BID_ASK quote trigger for now: LAST_PRICE is refused before
        arming because a late delivered trade can seed an unsafe trailing sell or leave
        the held stop unchecked. A positive supplied level goes up onto Alpaca's step
        through price_step.up_on_step, as fill_trigger does; a move logs both figures.
        """
        slot = self.slots.get(instrument_id)
        if slot is not None and slot.adopted:
            return
        if (
            self.winding_down
            or self.is_exiting()
            or (
                slot is not None
                and (
                    slot.exiting
                    or (slot.protect_id is not None and slot.protect_id.value in self.released)
                )
            )
        ):
            raise ValueError(f"{instrument_id}: cannot adopt while an exit or wind-down is active")
        positions = self.cache.positions_open(
            instrument_id=instrument_id, strategy_id=self.strategy_id
        )
        if not positions or any(position.signed_qty <= 0 for position in positions):
            owners = sorted(
                {
                    str(position.strategy_id)
                    for position in self.cache.positions_open(instrument_id=instrument_id)
                }
            )
            raise ValueError(
                f"{instrument_id}: cannot adopt: no own open long for {self.strategy_id}; "
                f"open position strategies: {', '.join(owners) or 'none'}"
            )
        value = level.as_decimal()
        if not value.is_finite() or value <= 0:
            reason = f"{instrument_id}: invalid adoption level {level}: expected a positive price"
            self._say(reason)
            raise ValueError(reason)
        if self.persistence:
            raise ValueError(
                f"{instrument_id}: cannot adopt with persistence: the emulated trailing sell "
                "requires both persistence arms off"
            )
        if self.settings.trigger == TriggerType.LAST_PRICE:
            reason = (
                f"{instrument_id}: cannot adopt with LAST_PRICE: carry requires the BID_ASK "
                "quote trigger; a late trade seed can bypass held-stop protection"
            )
            self._say(reason)
            raise ValueError(reason)
        if slot is not None and self._working(slot):
            raise ValueError(f"{instrument_id}: cannot adopt over a working slot order")
        on_step = up_on_step(value)
        # Validate on an unpublished slot (or a copy of an existing one). Neither
        # a refused plan nor a missing anchor may leave subscriptions or half a slot.
        candidate = self._prepare_slot(instrument_id) if slot is None else replace(slot)
        held = self._held(candidate)
        now = self.clock.timestamp_ns()
        quote = self.cache.quote(instrument_id)
        anchor = self._trail_anchor(candidate, record=False)
        if candidate.trail_plan is None:
            candidate.trail_plan = candidate.initial_trail = self._entry_trail(
                candidate, anchor, quote, now=now, allow_no_quote=True
            )
        observation = self._adoption_observation(candidate)
        if slot is None:
            self._publish_slot(candidate)
            slot = self.slots[instrument_id]
        else:
            slot.trail_plan, slot.initial_trail = candidate.trail_plan, candidate.initial_trail
            slot.holding_average = candidate.holding_average
        slot.adopted = slot.was_held = True
        slot.trail_trigger = on_step
        slot.protect_id = None  # any previous sell is terminal, checked by `_working` above
        slot.adoption_guard = (now, None if observation is None else observation[0])
        if on_step != value:
            self._say(
                f"{instrument_id}: adoption level {level} rounded up to {on_step} on Alpaca's step"
            )
        self._protect(slot, held, now)

    def _adoption_observation(self, slot: Slot) -> tuple[int, Decimal] | None:
        """The configured trigger's observed price and venue time, never the step's time."""
        if self.settings.trigger == TriggerType.BID_ASK:
            quote = self.cache.quote(slot.instrument_id)
            if quote is not None and quote.bid_price.as_decimal() > 0:
                return quote.ts_event, quote.bid_price.as_decimal()
        else:
            trade = self._holding_trade(slot)
            if trade is not None and trade.price > 0:
                return trade.event_ns, trade.price
        return None

    def _watch(self, now: int) -> None:
        if self.context_reader is not None:
            self._finish_contexts(now)
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
                            f"{instrument_id}: entry arm {self.settings.entry_arm}: "
                            + (self._context_pending(instrument_id) or "no entry context supplied"),
                        )
                elif (
                    instrument_id in self.slots
                    and instrument_id in self.context_reads
                    and instrument_id not in self.entry_contexts
                ):
                    self._request_context(instrument_id, now=now)
        self._reopen_late_buys(now)
        for instrument_id, slot in list(self.slots.items()):
            if instrument_id in top or self._working(slot) or self._held(slot) or slot.was_held:
                continue  # `was_held`: its sale is recorded by `_flat` before the slot may go
            if self._fresh(now) or self.winding_down:  # a stale ranking unwatches nothing
                self._close_slot(slot)

    def _reopen_late_buys(self, now: int) -> None:
        """Open a slot, in the wind-down or off the ranking too, for each name a late entry fill bought
        with no slot to hold it, so its shares are held by a slot like any other: protected by the next
        step's sale, in the record (`_ensure_exit`) and in the wind-down's market exit (`_end`)."""
        for instrument_id, row in list(self.late_buys.items()):
            del self.late_buys[instrument_id]
            if row.entry_client_order_id is None:
                continue
            client_order_id = ClientOrderId(row.entry_client_order_id)
            slot = self.slots.get(instrument_id)
            if slot is None:
                self._open_slot(instrument_id)
                slot = self.slots[instrument_id]
            elif slot.exit_record is not row and _sold_out(slot.exit_record):
                # The old holding sold in full before this late buy arrived. Finish it through
                # the same path as an ordinary flat step before restoring the new holding.
                self._flat(slot, now)
                if slot.exit_record is not None and slot.was_held:
                    # Its protect leg or entry still works, so `_flat` only withdrew it: the late
                    # buy waits for the next step, and the old holding's sale is recorded first
                    # (code-review-l5-latebuy/1). A sold row no step held has no sale for `_flat`
                    # to record, and the late buy takes the slot now (code-review-l5-latebuy2/F5).
                    self.late_buys[instrument_id] = row
                    continue
            entry = self._order(slot.entry_id)
            # An order's average is unset until it fills (the pinned
            # `crates/model/src/orders/limit.rs:414`): a newer entry with shares of its own keeps its
            # order, its record, its plan and the loss it was sized for; the late buy's row is
            # attached to the holding, not its plan (code-review-l5-late-fills-fix/P2).
            own_fill = entry is not None and entry.avg_px is not None
            if entry is None or (entry.is_closed and not own_fill):
                slot.entry_id = client_order_id
            if not own_fill and slot.entry_received_ns is None:
                # The hold's clock starts at the late entry's own first receipt, as a slot that kept
                # owning the entry would have taken it; a newer filled entry keeps its own
                # (r-pair-codex/P1).
                slot.entry_received_ns = row.entry_received_ns
            # A newer working entry stays tracked until canceled/closed; the holding's own fills
            # anchor persistence while that order has no average fill (F1).
            slot.exit_record = row
            if not any(kept is row for kept in self.exits):
                self.exits.append(row)
            if not own_fill:
                self._take_entry_plan(slot, client_order_id.value)
            plan = slot.trail_plan
            row.trail_distance = offsets_in_force(
                slot,
                row.bought_notional / row.quantity,
                self.settings.trail if plan is None else plan.trail,
                self.settings.exit_through if plan is None else plan.exit_through,
                self._floor_m(),
            )[0]
            if slot.protection_anchor is None:
                # An ordinary holding enters the wind-down with its protection's anchor set, which
                # `_limit_sell` prices from when no bid or print is known. The recovered holding
                # takes its own average fill, so the wind-down sends its sale on that same path
                # (r-latefill-delta2/F1).
                slot.protection_anchor = row.bought_notional / row.quantity
            slot.was_held = True
            held = self._held(slot)
            if held > 0:
                self._ensure_exit(slot, held)
            elif held == 0 and row.filled == row.quantity:
                self._flat(slot, now)
            self._note(
                slot,
                f"{instrument_id}: a fill of {client_order_id} came after its slot closed; "
                f"the slot is opened again for the {held} held",
            )

    def _take_entry_plan(self, slot: Slot, client_order_id: str) -> None:
        """Give the slot the entry's own record, its frozen trail plan and the loss it was sized
        for, as `_buy` recorded them in `entries_sent`. Plain data only, so a fill handler may call
        it while the strategy is borrowed."""
        slot.entry_record = next(
            (
                entry_row
                for entry_row in reversed(self.entries_sent)
                if entry_row.get("client_order_id") == client_order_id
            ),
            None,
        )
        record = slot.entry_record
        if record is None:
            return
        # These are the frozen entry fractions and L already recorded by `_buy` (F2),
        # not a fresh selection from the reopened slot's cold indicators.
        if record.get("trail") is not None:
            slot.trail_plan = slot.initial_trail = TrailPlan(
                trail=Decimal(record["trail"]),
                exit_through=Decimal(record["exit_through"]),
                chase_cap=Decimal(record["chase_cap"]),
                atr=None if record.get("atr") is None else Decimal(record["atr"]),
                spread=None if record.get("spread") is None else Decimal(record["spread"]),
                bound=record["trail_bound"],
                source=record.get("trail_source"),
                floor=(
                    None if record.get("spread_floor") is None else Decimal(record["spread_floor"])
                ),
                measure=(
                    None if record.get("own_measure") is None else Decimal(record["own_measure"])
                ),
                profile=record.get("profile_at_entry", record.get("profile")),
            )
        sizing = record.get("sizing")
        slot.sized_loss = (
            None
            if sizing is None or sizing.get("loss_per_share") is None
            else Decimal(sizing["loss_per_share"])
        )

    # --- on-demand entry contexts -------------------------------------------------------------------

    def _request_context(
        self, instrument_id: InstrumentId, *, refresh: bool = False, now: int | None = None
    ) -> bool:
        """Start one bounded read, keep its pending future, or record why it is deferred.

        Whole-history refreshes use the same limits as initial reads. The window's transport hook
        bounds actual pages/retries; this bound limits submitted work, even if its worker is slow.
        """
        if self.context_reader is None:
            return False
        now = self.clock.timestamp_ns() if now is None else now
        read = self.context_reads.get(instrument_id)
        if read is None:
            load = self.instrument_loads.get(instrument_id)
            read = self.context_reads[instrument_id] = ContextRead(
                now if load is None else load.requested_ns
            )
        if self._contexts_stopped or self.winding_down:
            read.deferred = "window stopped" if self._contexts_stopped else "window winding down"
            return True
        if read.future is not None:
            return True
        known = self.entry_contexts.get(instrument_id)
        if (
            known is not None
            and known.missing is not None
            and known.day == datetime.fromtimestamp(now // SECOND_NS, NEW_YORK).date()
        ):
            # mover_entry_contexts' missing reasons describe prior-session reference/calendar
            # inputs. This window has no correction signal; only a new New York day changes
            # those inputs. Keep the named Day, including across slot reopen/refresh, instead
            # of treating it as a transient transport failure with a 60-second retry.
            return True
        if read.reads and not refresh and instrument_id in self.entry_contexts:
            return True
        if read.last_failure is not None and now - read.failed_ns < CONTEXT_RETRY_NS:
            return True
        while self.context_sent and self.context_sent[0] <= now - BUDGET_SPAN_NS:
            self.context_sent.popleft()
        if len(self.context_sent) >= CONTEXT_READ_LIMIT:
            read.deferred = (
                f"entry context read held at the rolling minute limit of {CONTEXT_READ_LIMIT}"
            )
            return True
        read.deferred = None
        self.context_sent.append(now)
        reader = self.context_reader
        read.started_ns, read.reads = now, read.reads + 1
        try:
            read.future = (self.context_submit or self._submit_context)(
                lambda: reader(instrument_id, now)
            )
        except Exception as error:  # noqa: BLE001 - a refused worker is named like a refused read
            self._context_failed(instrument_id, read, f"{type(error).__name__}: {error}", now)
        return True

    def _submit_context(self, call: Callable[[], Any]) -> Future:
        if self._context_executor is None:
            self._context_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="mover-entry-context"
            )
        return self._context_executor.submit(call)

    def _context_failed(
        self, instrument_id: InstrumentId, read: ContextRead, error: str, now: int
    ) -> None:
        read.failures.append(error)
        read.last_failure, read.failed_ns = error, now
        text = f"{instrument_id}: entry context read failed: {error}"
        notes = self.notes.setdefault(instrument_id, [])
        if text not in notes:
            notes.append(text)
            self.dropped.append(text)

    def _finish_contexts(self, now: int | None = None) -> None:
        """Install on the strategy thread. History owns its minutes; same-day live bars fill gaps."""
        from . import early_entry_study as study

        now = self.clock.timestamp_ns() if now is None else now
        for instrument_id, read in self.context_reads.items():
            future = read.future
            if future is None or not future.done():
                continue
            read.future = None
            error = None
            if future.cancelled():
                error = "cancelled at stop" if self._contexts_stopped else "cancelled"
            elif (failed := future.exception()) is not None:
                error = f"{type(failed).__name__}: {failed}"
            if error is not None:
                self._context_failed(instrument_id, read, error, now)
                continue
            day = future.result()
            if day.symbol != instrument_id.symbol.value:
                self._context_failed(instrument_id, read, "entry context names another symbol", now)
                continue
            read.last_failure = None
            if day.missing is not None:
                self.entry_contexts[instrument_id] = day
                self.context_sources[instrument_id] = "on_demand"
                read.ready_ns = None
                read.deferred = "entry context missing; reread on New York day change"
                text = (
                    f"{instrument_id}: entry arm {self.settings.entry_arm}: "
                    f"entry context missing: {day.missing}"
                )
                notes = self.notes.setdefault(instrument_id, [])
                if text not in notes:
                    notes.append(text)
                    self.dropped.append(text)
                continue
            known = self.entry_contexts.get(instrument_id)
            bars = {} if known is None or known.day != day.day else {b.start: b for b in known.bars}
            if read.live_day == day.day:
                bars.update(read.live)
            bars.update({b.start: b for b in day.bars})
            read.live, read.live_day = {}, None
            covered_to = max(
                day.covered_to,
                known.covered_to if known is not None and known.day == day.day else 0,
            )
            while covered_to in bars and covered_to < study.WINDOW_END:
                covered_to += 1
            self.entry_contexts[instrument_id] = replace(
                day,
                bars=tuple(bars[m] for m in sorted(bars)),
                covered_to=min(covered_to, study.WINDOW_END),
            )
            self.context_sources[instrument_id] = "on_demand"
            if read.ready_ns is None:
                read.ready_ns = now

    def _fill_context_gap(
        self, instrument_id: InstrumentId, day: entry_study.Day, opened_ns: int, now: int
    ) -> entry_study.Day:
        """Extend coverage only through complete minutes the node already holds, with no read.

        Cache.bars is newest first (rc8 crates/common/src/cache/mod.rs:6276-6284).
        The rank feed's EXTERNAL minutes are complete even when this slot's first INTERNAL
        minute is partial. Their open/close follow adapter parse_bar and bar_closed_ns;
        neither another day's bars nor bars received/closing after this prefix fill its gap.
        """
        from . import early_entry_study as study

        minute = datetime.fromtimestamp(opened_ns // SECOND_NS, NEW_YORK)
        until = min(minute.hour * 60 + minute.minute, study.WINDOW_END)
        if day.covered_to >= until:
            return day
        read = self.context_reads[instrument_id]
        bars = dict(read.live) if read.live_day == day.day else {}
        bars.update({bar.start: bar for bar in day.bars})  # history keeps its own minutes
        cached = self.cache.bars(BarType.from_str(f"{instrument_id}-{WARM_BAR_SPEC}"))
        for bar in cached or []:
            opened = datetime.fromtimestamp(bar_opened_ns(bar) // SECOND_NS, NEW_YORK)
            start = opened.hour * 60 + opened.minute
            if (
                opened.date() == day.day
                and day.covered_to <= start < until
                and bar.ts_init <= now
                and bar_closed_ns(bar) <= opened_ns
            ):
                bars.setdefault(
                    start,
                    study.Minute(
                        start,
                        float(bar.open.as_decimal()),
                        float(bar.high.as_decimal()),
                        float(bar.low.as_decimal()),
                        float(bar.close.as_decimal()),
                        float(bar.volume.as_decimal()),
                    ),
                )
        covered_to = day.covered_to
        while covered_to < until and covered_to in bars:
            covered_to += 1
        day = replace(day, bars=tuple(bars[m] for m in sorted(bars)), covered_to=covered_to)
        self.entry_contexts[instrument_id] = day
        return day

    def _context_pending(self, instrument_id: InstrumentId, *, refresh: bool = False) -> str | None:
        if not self._request_context(instrument_id, refresh=refresh):
            return None
        read = self.context_reads[instrument_id]
        seconds = (self.clock.timestamp_ns() - read.loaded_ns) / SECOND_NS
        why = read.deferred or read.last_failure
        detail = f"; {why}" if why else ""
        return f"entry_context_pending: {seconds:.1f} s since the name was loaded{detail}"

    def _context_seconds(self, instrument_id: InstrumentId) -> float | None:
        read = self.context_reads.get(instrument_id)
        if read is None or read.ready_ns is None:
            return None
        return round((read.ready_ns - read.loaded_ns) / SECOND_NS, 3)

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

    def _prepare_slot(self, instrument_id: InstrumentId) -> Slot:
        """Build and warm a slot from cache without publishing it or subscribing."""
        bar_type = BarType.from_str(f"{instrument_id}-{self.spec_suffix}")
        reference = quote_lag(self.cache.quote(instrument_id))
        slot = Slot(
            instrument_id=instrument_id,
            bar_type=bar_type,
            closes=deque(maxlen=self.settings.run_bars + 1),
            dollars=deque(maxlen=self.settings.flow_bars),
            atr=None
            if self.settings.trail_mode == "fixed"
            else AverageTrueRange(self.settings.atr_n),
            subscribed_ns=self.clock.timestamp_ns(),
            # Cache.trades is in reverse insertion order, not venue-time order
            # (crates/common/src/cache/mod.rs:6218-6228 at the pin).
            newest_print=self._newest_cached(instrument_id, reference),
            quote_lags=deque(
                [] if reference is None else [reference], maxlen=QUOTE_REFERENCE_QUOTES
            ),
        )
        if slot.atr is not None:
            # The lead's (a), 2026-11-12: warm from the minute bars this node's cache already holds
            # for the name, the rank actor's EXTERNAL bars, with no request. A slot's own INTERNAL bars
            # are not used: the first of each subscription is a partial minute. `Cache.bars` gives
            # None for a bar type it holds none of (crates/common/src/python/cache.rs at the pin).
            cached = self.cache.bars(BarType.from_str(f"{instrument_id}-{WARM_BAR_SPEC}"))
            if self._warm_atr(slot, cached or [], slot.subscribed_ns):
                slot.history = HistoryLoad(
                    instrument_id,
                    slot.subscribed_ns,
                    completed=True,
                    cached=slot.atr_warm_bars,
                    from_cache=True,
                )
        return slot

    def _open_slot(self, instrument_id: InstrumentId) -> None:
        self._publish_slot(self._prepare_slot(instrument_id))

    def _publish_slot(self, slot: Slot) -> None:
        """Install a slot `_prepare_slot` built, then subscribe it; `adopt_holding` publishes the
        slot it validated, so a subclass's `_open_slot` keeps main's signature."""
        instrument_id = slot.instrument_id
        self.slots[instrument_id] = slot
        self.opened_slots.add(instrument_id)
        if (state := self.reentry_states.get(instrument_id)) is not None:
            state.restart()  # only closes this slot observes count toward a turn or a crossing
        if slot.history is not None:
            self.history_loads.append(slot.history)
            self.notes.setdefault(instrument_id, []).append(
                f"{instrument_id}: ATR warmed from {slot.atr_warm_bars} cached one-minute bars "
                f"({slot.atr_bars[-1][1].bar_type}), no request"
            )
        self._sync_opening(slot, slot.subscribed_ns)
        # Quotes and trades first, on the feed: the data engine hands the client only an instrument's
        # first subscription (ADR 0055), and the emulator's own subscription names no feed.
        self.subscribe_quotes(instrument_id, params=self.feed_params)
        self.subscribe_trades(instrument_id, params=self.feed_params)
        self.subscribe_bars(slot.bar_type)
        slot = self.slots[instrument_id]
        if slot.atr is not None and slot.history is None:  # F-2: only when the cache left it short
            self._request_history(slot)
            if slot.history is not None:
                slot.history.cached = len(slot.atr_minutes)  # what the cache fed, before any answer

    def _host_clock(self) -> dict[str, Any]:
        """The venue's time less this host's receipt of the latest quotes (`CLOCK_QUOTES`), in whole
        milliseconds rounded to the nearest: their median (`median_of` quotes), with the least and most
        of every quote in the run (`quotes`). It is the clocks' difference less the feed's latency."""
        least, most = self.quote_lag_least, self.quote_lag_most
        if not self.quote_lags or least is None or most is None:
            return {"venue_minus_receipt_ms": None, "median_of": 0, "quotes": self.quotes}
        return {
            "venue_minus_receipt_ms": {
                "median": round(median_low(self.quote_lags) / 1_000_000),
                "least": round(least / 1_000_000),
                "most": round(most / 1_000_000),
            },
            "median_of": len(self.quote_lags),
            "quotes": self.quotes,
        }

    def _newest_cached(self, instrument_id: InstrumentId, reference: int | None) -> Print | None:
        """Seed from all cached trades by venue time, the later received on a tie. Only prints
        received after this slot opens are counted; cached older or ahead prints are passed over."""
        newest = max(
            (
                trade
                for trade in self.cache.trades(instrument_id) or []
                if print_time(trade, reference) is not None
            ),
            key=lambda trade: int(trade.ts_event),
            default=None,
        )
        return None if newest is None else Print.of(newest, reference)

    # --- the ATR's warm-up (F-2) ---------------------------------------------------------------------

    def _request_history(self, slot: Slot) -> None:
        """Ask once, for this opening, for the slot's most recent complete one-minute bars: upstream's
        `Strategy.request_bars` with a `start` and no `limit` (`trading/__init__.pyi:993-1002`), the
        EXTERNAL form of the slot's bar type on the slot's feed, as `SessionMaCross` warms its averages.
        The span is `WARM_UP_SPAN`, or `atr_n` plus `WARM_UP_SPARE_BARS` minutes if that is longer, since
        a thin name has minutes with no bar. Held by name over `HISTORY_REQUEST_LIMIT` a rolling minute,
        and sent from a later step once the minute frees (`_finish_history`).

        Known limits, said so (review of 2026-11-12): the hour can reach into the session part before
        this one, and a name's quiet minutes before it began to move, so the first ATR is the last hour's,
        UNCALIBRATED and judged by ADR 0133's record. History bars are Alpaca's EXTERNAL bars, with no bar
        for a minute without trades, where the live INTERNAL bars are built flat for such a minute; the
        replay and tuning that judge `atr_k` read the same EXTERNAL minute bars
        (`reentry_replay.py`, `tuning_study.BAR_SPEC`), so the warm-up keeps that basis."""
        now = self.clock.timestamp_ns()
        load = slot.history = HistoryLoad(slot.instrument_id, now)
        self.history_loads.append(load)
        self._send_history(slot, load, now)

    def _send_history(self, slot: Slot, load: HistoryLoad, now: int) -> None:
        """Send `load`'s request, or hold it by name while the rolling minute's limit is reached."""
        while self.history_sent and now - self.history_sent[0] > BUDGET_SPAN_NS:
            self.history_sent.popleft()
        if len(self.history_sent) >= HISTORY_REQUEST_LIMIT:
            load.held = True
            load.reason = (
                f"history request held at the rolling minute limit of {HISTORY_REQUEST_LIMIT}"
            )
            self._history_note(slot.instrument_id, load.reason)
            return
        load.held, load.reason, load.requested_ns = False, None, now
        start, end = history_window(self.settings, self.feed_params, self.clock.utc_now())
        bar_type = BarType(slot.instrument_id, slot.bar_type.spec, AggregationSource.EXTERNAL)
        try:
            request_id = self.request_bars(bar_type, start=start, end=end, params=self.feed_params)
        except (RuntimeError, ValueError, TypeError) as e:
            load.completed = True
            load.reason = f"{HISTORY_REFUSED}: {type(e).__name__}: {e}"
            self._history_note(slot.instrument_id, load.reason)
            return  # refused before it left: it takes no place in the rolling minute
        self.history_sent.append(now)
        self.history_requests += 1
        load.request_id = str(request_id)
        self.history_by_request[load.request_id] = load

    def _finish_history(self, now: int) -> None:
        """Feed each warm-up answer since the last step to its own slot's ATR (`_warm`), and name each
        warm-up that left its slot cold: refused (a 429 or a feed the adapter refuses arrives as the
        answer's failure, `crates/live/src/python/client/mod.rs:1828-1849`), fewer than `atr_n` bars,
        or unanswered after `HISTORY_WAIT_SECS`. An answer for a request already complete, or for an
        opening whose slot has closed since, feeds nothing and is counted as dropped; a refusal is still
        named a refusal. A warm-up whose slot closed unanswered is named so, and one held at the rolling
        minute's limit is sent once the minute frees, while its slot is open."""
        taken, self.history_responses = self.history_responses, []
        for request_id, completed, failure, bars in taken:
            load = self.history_by_request[request_id]
            slot = self.slots.get(load.instrument_id)
            if load.completed or slot is None or slot.history is not load:
                load.dropped += len(bars)
                self.history_dropped += len(bars)
                if failure is not None and (load.closed_first or not load.completed):
                    load.completed = load.closed_first = True
                    load.reason = f"{HISTORY_REFUSED} after its slot closed: {failure}"
                elif not load.completed and completed:
                    load.completed = load.closed_first = True
                    load.reason = "answered after its slot closed: nothing fed"
                continue
            if failure is not None:
                load.completed = True
                load.dropped += len(bars)
                self.history_dropped += len(bars)
                load.reason = f"{HISTORY_REFUSED}: {failure}"
                self._history_note(load.instrument_id, load.reason)
                continue
            self._warm(slot, bars, now)
            if not completed:
                continue
            load.completed = True
            slot.atr_bars = []  # nothing more can arrive to be ordered among them
            cached = f" and {load.cached} cached" if load.cached else ""
            if load.bars + load.cached >= self.settings.atr_n:
                load.reason = None
                self._say(
                    f"{load.instrument_id}: ATR warm from {load.bars} history bars{cached}"
                    + (" (answered late)" if load.late else "")
                )
            else:
                load.reason = (
                    f"{load.bars} history bars{cached}, fewer than {self.settings.atr_n}: "
                    + (
                        "the ATR is complete with live bars"
                        if slot.atr is not None and slot.atr.initialized
                        else "the cold ATR fallback stays until live bars complete it"
                    )
                )
                self._history_note(load.instrument_id, load.reason)
        for load in self.history_loads:
            if load.completed:
                continue
            slot = self.slots.get(load.instrument_id)
            if slot is None or slot.history is not load:
                load.completed = load.closed_first = True
                load.reason = (
                    "slot closed before the request was sent"
                    if load.held
                    else "slot closed before its answer: nothing fed"
                )
            elif load.held and (
                self.winding_down or (slot.atr is not None and slot.atr.initialized)
            ):
                load.completed, load.held = True, False
                load.reason = (
                    "held request not sent: the window is winding down"
                    if self.winding_down
                    else "held request not sent: the ATR was warm before the minute freed"
                )
                self._history_note(load.instrument_id, load.reason)
            elif load.held:
                self._send_history(slot, load, now)
        for slot in self.slots.values():
            load = slot.history
            if (
                load is not None
                and not load.completed
                and not load.held
                and not load.late
                and now - load.requested_ns >= HISTORY_WAIT_SECS * SECOND_NS
            ):
                load.late = True
                load.reason = f"history request unanswered after {HISTORY_WAIT_SECS} s"
                self._history_note(load.instrument_id, load.reason)

    def _warm(self, slot: Slot, bars: Sequence[Bar], now: int) -> tuple[int, int]:
        """Feed `bars` to this slot's ATR alone, each minute once, and return how many were fed and how
        many dropped. A bar of another instrument, of a minute already fed, of a minute from the
        slot's first live bar on, or still forming at `now` (its `ts_init`, its close, is later) is
        dropped. Closes, dollar flow, the entry arms and the live-bar count are the live bars' alone. If
        live bars were fed first, the ATR is reset and fed again in time order (`Indicator.reset`, as
        `SessionMaCross.on_historical_bars` does for its averages)."""
        assert slot.atr is not None and slot.history is not None
        kept: list[tuple[int, Bar]] = []
        for bar in bars:
            opened = bar_opened_ns(bar)
            if (
                bar.bar_type.instrument_id != slot.instrument_id
                or opened in slot.atr_minutes
                or bar.ts_init > now
                or (slot.first_live_open is not None and opened >= slot.first_live_open)
            ):
                continue
            slot.atr_minutes.add(opened)
            kept.append((opened, bar))
        if kept:
            slot.atr_bars = sorted([*slot.atr_bars, *kept], key=lambda pair: pair[0])
            slot.atr.reset()
            for _, bar in slot.atr_bars:
                slot.atr.handle_bar(bar)
            slot.warm_checked = False  # a reshaped ATR: `_warm_tighten` compares once more
        dropped = len(bars) - len(kept)
        slot.history.bars += len(kept)
        slot.history.dropped += dropped
        self.history_bars += len(kept)
        self.history_dropped += dropped
        return len(kept), dropped

    def _history_note(self, instrument_id: InstrumentId, text: str) -> None:
        """Name a warm-up's outcome once in the name's notes and the node's log (step only)."""
        line = f"{instrument_id}: {text}"
        notes = self.notes.setdefault(instrument_id, [])
        if line not in notes:
            notes.append(line)
            self._say(line)

    def _history_record(self) -> dict[str, Any]:
        """The warm-up's part of the record: totals, each outcome counted, and each opening's row."""
        outcomes = dict.fromkeys(HISTORY_OUTCOMES, 0)
        rows = []
        for load in self.history_loads:
            outcome = load.outcome(self.settings.atr_n)
            outcomes[outcome] += 1
            rows.append(
                {
                    "instrument_id": str(load.instrument_id),
                    "request_id": load.request_id,
                    "requested_ns": load.requested_ns,
                    "completed": load.completed,
                    "late": load.late,
                    "bars": load.bars,
                    "cached": load.cached,
                    "dropped": load.dropped,
                    "outcome": outcome,
                    "reason": load.reason,
                }
            )
        return {
            "bars": self.history_bars,
            "dropped": self.history_dropped,
            "outcomes": outcomes,
            "slots": rows,
        }

    def _trail_notes(self) -> list[str]:
        """The record's limits; the state re-entry note only when a state arm runs."""
        reentry = [] if self.settings.reentry_arm == "clock" else [RECLAIM_LIMIT]
        return [PAPER_TRAIL_LIMIT, TRAIL_SOURCE_LIMIT, *reentry, SHIPPED_ATR_NOTE]

    def _warm_atr(self, slot: Slot, bars_newest_first: list[Bar], now: int) -> bool:
        """Feed the slot's ATR the last `atr_n + 1` cached bars closed by `now`, oldest first.

        Only bars that opened in the session part open at `now` count, so no range spans a session
        gap. `Cache.bars` returns the newest bar first (NautilusTrader, crates/common/src/cache/mod.rs
        at the pin). A streamed EXTERNAL bar's ts_init is its receipt time (`canon_alpaca.data.parse_bar`),
        so its interval must also have ended by `now`. With fewer than `atr_n` such bars the ATR stays
        cold and the entry's fixed fallback applies (the lead's (ii)), and the opening asks for history
        (F-2, `_request_history`), which completes the same series.
        """
        if slot.atr is None:
            return False
        n = self.settings.atr_n
        since = part_opened_ns(now)
        closed = [
            bar
            for bar in bars_newest_first
            if bar.ts_init <= now and since <= bar_opened_ns(bar) <= now - 60 * SECOND_NS
        ][: n + 1]
        for bar in reversed(closed):
            # F-2, the lead's ruling of 2026-11-13, 00:10: one series, two sources. Every cached
            # minute is fed, even short of `atr_n`, and marked, so a requested answer neither feeds
            # it again nor drops it when it re-feeds the series in time order (`_warm`).
            opened = bar_opened_ns(bar)
            slot.atr_minutes.add(opened)
            slot.atr_bars.append((opened, bar))
            slot.atr.handle_bar(bar)
        slot.atr_warm_bars = len(closed)
        if len(closed) < n:
            return False
        return slot.atr.initialized

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
        if _sold_out(slot.exit_record) and slot.instrument_id in getattr(self, "late_buys", {}):
            # The holding sold in full and a late buy of the name waits for its own record
            # (`_reopen_late_buys`). Only withdraw the old holding's leg here: nothing is sold or
            # protected through its exit state. The late shares are unprotected until that leg
            # closes, usually one step (code-review-l5-latebuy/1).
            self._flat(slot, now)
            return
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
        elif (self.settings.protect or slot.adopted or slot.exiting) and not self.is_exiting():
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
            row = slot.exit_record
            averaged = row.notional / row.filled if row is not None and row.filled else None
            sold_px = None if order is None or order.avg_px is None else Decimal(str(order.avg_px))
            # The holding includes every sale leg, including canceled legs' late fills. One order's
            # non-None average may cover only a partial sale, under the clock arm too (F3).
            price = averaged if averaged is not None else sold_px
            self.sold[slot.instrument_id] = (now, price)
            if row is not None:
                self.sold_rows[slot.instrument_id] = row
            sale_fills = [
                at
                for cid, side, _, _, at in self.fills
                if order is not None
                and cid == order.client_order_id.value
                and side == OrderSide.SELL.name
            ]
            filled_ns = (
                row.final_fill_ns
                if row is not None and row.final_fill_ns is not None
                else max(sale_fills, default=now)
            )
            self.sold_fill_ns[slot.instrument_id] = filled_ns
            if (
                state := self.reentry_states.get(slot.instrument_id)
            ) is None or state.sale_ns != filled_ns:
                state = self.reentry_states[slot.instrument_id] = ReentryState(
                    filled_ns, price, None if row is None else row.trail_distance
                )
                if row is not None:
                    state.observe_pending(row)
            slot.was_held = False
            slot.exiting = False
            slot.floor = slot.trail_trigger = slot.trail_limit = None
            slot.sized_loss = slot.exit_trigger = None
            slot.trail_high = None
            slot.observations.clear()
            slot.high_observations.clear()
            slot.breach_observations.clear()
            slot.breach_price = None
            slot.observed_ns = 0
            slot.entry_id = slot.protect_id = None
            slot.entry_cancel_ns = slot.priced_ns = 0
            if (
                self.settings.reentry_arm == "clock"
                or slot.run_closed_ns is None
                or slot.run_closed_ns <= filled_ns
            ):
                # a state arm's run whose minute ended after the final fill is a close after the
                # sale: it is kept for this step's entry check (the batch A head reader, A-3); the
                # clock arm compares no close with the fill, so its run is cleared as before
                slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None
            slot.trail_plan = slot.initial_trail = None
            slot.ratcheted, slot.tightened, slot.entry_record = False, False, None
            slot.warm_checked = False
            slot.profile_evaluated_ns = None
            slot.exit_record = None
            slot.protection_anchor = slot.held_stop = slot.held_since = None
            slot.held_low = slot.held_at_l = slot.entry_received_ns = None
            slot.holding_from = slot.held_average = None
            slot.floor_since, slot.floor_released = None, False
            slot.trailed, slot.last_sale = False, None
            slot.adopted, slot.adoption_guard = False, None

    def _protect(self, slot: Slot, held: Decimal, now: int) -> None:
        self._ensure_exit(slot, held)
        order = self._order(slot.protect_id)
        adoption_ready = False
        if slot.adoption_guard is not None and not slot.exiting:
            armed_at, observed_at = slot.adoption_guard
            observation = self._adoption_observation(slot)
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
        if self.persistence and not slot.exiting:
            self._persistent_trail(slot, now)
            if not slot.exiting:
                return
        order = self._order(slot.protect_id)
        raised = False
        if not slot.exiting and slot.adoption_guard is None and slot.sized_loss is not None:
            # H1-H3 apply to the holding, including fills of another entry (lead, 2026-11-13).
            # Read its average before either anchor is refreshed. A cheaper fill cannot lower a
            # reached stop; if a fill brings the average from above the kept stop to at or under
            # it, latch the sale here for both the held and emulated paths. A stop already at or
            # above the average is a winner's stop: a lower average crosses nothing (lead's H3).
            average = self._position_average(slot)
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
                self._mark_exit(slot, now, average, "held stop (holding average)")
                if slot.exit_record is not None:
                    slot.exit_record.held_stop_kept = kept
                    slot.exit_record.held_stop_from_average = average - kept
                if slot.entry_record is not None:
                    slot.entry_record["held_stop_level"] = str(kept)
                    slot.entry_record["held_stop_trigger"] = "holding average"
                self._end_hold(slot, now, "held_stop")
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
                self.cancel_order(order.client_order_id)
            order = self._order(slot.protect_id)
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
                anchor = None
                if self.persistence:
                    # The lower of the confirming print and the bid. A one-sided quote's zero bid
                    # is no price, and neither is no quote: the trade newest by its own time
                    # (`_last_print`) then stands in for the bid, so a recovery cannot lift the
                    # sale and a fall under the print is followed, and the sale is given that
                    # basis as its anchor (`_limit_sell`).
                    quote = self.cache.quote(slot.instrument_id)
                    basis = None if quote is None else quote.bid_price.as_decimal()
                    quoted = basis is not None and basis > 0
                    if not quoted:
                        basis = self._last_print(slot)
                    if slot.breach_price is not None:
                        basis = (
                            slot.breach_price if basis is None else min(basis, slot.breach_price)
                        )
                    if not quoted and basis is not None:
                        anchor = basis
                        self._note(
                            slot,
                            f"{slot.instrument_id}: no bid at a confirmed sale; priced from the "
                            "lower of the confirming print and the newest trade",
                        )
                    # set once, for the first sale: a floor released after its wait stays released
                    if basis is not None and slot.floor is None and not slot.floor_released:
                        first = exit_price(basis, self.settings.exit_ticks, None)
                        slot.floor = chase_floor(first, self.settings.chase_cap)
                self._limit_sell(slot, held, now, slot.floor, anchor=anchor)
            else:
                self._trail(slot, held, now)
        elif order.is_emulated:
            tightened = self._warm_tighten(slot, now)
            if self.settings.trail_mode == "ratchet" and not slot.ratcheted and not tightened:
                tightened = self._ratchet(slot, now)
            elif self.settings.trail_mode == "profile" and not tightened:
                tightened = self._profile_tighten(slot, now)
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
                    self.sell_triggers[order.client_order_id.value] = str(reached)
                if order.price is not None:
                    limit = order.price.as_decimal()
                    slot.trail_limit = max(limit, slot.trail_limit or limit)
                self.cancel_order(order.client_order_id)
                # The emulator cancels its own order before the call returns, so the cancel is seen
                # terminal here and the new sell follows in this step, not a second later.
                canceled = self._order(slot.protect_id)
                if canceled is not None and canceled.is_closed:
                    self._trail(slot, held, now)
                    if slot.protect_id != order.client_order_id:
                        self._remade(slot)
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
        row = slot.exit_record
        bought = (
            Decimal(str(entry.avg_px))
            if entry is not None and entry.avg_px is not None
            else row.bought_notional / row.quantity
            if row is not None and row.quantity > 0 and row.bought_notional > 0
            else None
        )
        if bought is None:
            self._note(
                slot,
                f"{slot.instrument_id}: held with no filled entry of this strategy to size a "
                "trailing sell from; not protected, and sold only at the wind-down",
            )
            return
        trailing, _ = offsets(bought, self.settings.trail, self.settings.exit_through)
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
                    # the decision to sell, at the confirming print's own time and price: the
                    # strategy's own trailing stop, its exit row naming the prints and the trigger
                    # (the D3 terms, A5)
                    self._mark_exit(
                        slot,
                        stamp,
                        price,
                        f"trailing stop (persistence: breach confirmed by {len(breaches)} prints at "
                        f"or under {slot.trail_trigger})",
                    )
                    slot.observations.clear()
                    self._say(
                        f"{slot.instrument_id}: breach confirmed at {stamp}, trigger "
                        f"{slot.trail_trigger}; selling by limit"
                    )
                    if entry is not None and not entry.is_closed:
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
        if (at is None or at <= 0) and slot.adoption_guard is None:
            # The emulator cannot arm without a market price: hold a stop the strategy checks itself
            # (the lead's decision (b), 2026-11-12), not a sale at once.
            self._hold(slot, held, now)
            return
        if slot.held_since is not None and slot.adoption_guard is None:
            # the held stop at the average as it is now, before any price is compared with it: a fill
            # since the last hold step moves it, as each hold step would (dsh-reader-343, F1;
            # t-heldrefresh, H1 and H2), and the exit record names the refreshed level, measured from
            # the average it was refreshed at, as each hold step records it (the combined landing's
            # code review, finding 1)
            self._held_at_l(slot)
            self._record_held_stop(slot)
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
                self._mark_exit(slot, now, low, "held stop (followed price)")
                if slot.entry_record is not None:
                    slot.entry_record["held_stop_trigger"] = "followed price"
                slot.exit_trigger = level  # before `_end_hold` clears it, as `_hold` does
                self._end_hold(slot, now, "held_stop")
                self._limit_sell(slot, held, now, slot.floor, anchor=at)
                return
        try:
            bought = slot.protection_anchor = self._trail_anchor(slot)
        except RuntimeError as error:
            # Raised from the step timer this would stop every slot; it is named and retried next step.
            self._note(slot, str(error))
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
            self.settings.trail if plan is None else plan.trail,
            self.settings.exit_through if plan is None else plan.exit_through,
            self._floor_m(),
            basis=entry_basis(self._order(slot.entry_id)),
        )
        if kept is not None and at is not None and at <= kept and slot.adoption_guard is None:
            # the market is at the trigger the canceled sell had reached, or the stop held: the sale
            # is due now, whatever room the floor leaves a trailing sell (the landing review, P2)
            if holding:
                self._end_hold(slot, now, "trail")
            slot.exiting, slot.trail_trigger, slot.trail_limit = True, None, None
            self._mark_exit(slot, now, at, met)
            self._say(
                f"{slot.instrument_id}: at {at}, the trigger {kept} ({origin}) is met; "
                "selling by limit"
            )
            slot.exit_trigger = kept
            self._limit_sell(slot, held, now, slot.floor)
            return
        seed = at
        if trigger == TriggerType.LAST_PRICE:
            # rc8's OrderEmulator seeds synchronously at submission from its last delivered trade
            # (emulator.rs:665, :1616-1621), not the venue-newest print `_followed` reads. Check that
            # price too: a late print must not seed a non-positive trigger or release limit (P1-1).
            trade = self.cache.trade(slot.instrument_id)
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
            self._hold(slot, held, now, followed=at)
            return
        if holding:
            self._end_hold(slot, now, "trail")
            # the held stop's place goes to the trigger the remade sell starts from
            slot.trail_trigger = reached
        instrument = self._instrument(slot.instrument_id)
        if slot.exit_record is not None:
            slot.exit_record.trail_distance = trailing
        self._say(
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
        order = self.order_factory.trailing_stop_limit(
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
            reduce_only=slot.adopted,
        )
        slot.protect_id = order.client_order_id
        slot.trailed = True
        if slot.adoption_guard is not None:
            slot.trail_trigger = kept
        if kept is not None:
            self.sell_triggers[order.client_order_id.value] = str(kept)
        slot.priced_ns = (
            now  # the trigger is kept: a remake that is refused starts the next from it
        )
        self._sale_sent(slot, order, now, kept)
        self.submit_order(order, params=dict(EXTENDED_HOURS))

    def _remade(self, slot: Slot) -> None:
        """Mark the trailing sale `_trail` just sent in place of one canceled for a changed holding or
        a tightening as a remake, and count it when its trailing distance is under the spread floor at
        this step (the lead's ruling (ii) on re-flooring, 2026-11-13): what is sent does not change. A
        sale at once (`_limit_sell`) is no remade trailing sale and is not marked; the fixed arm, which
        takes no floor, has none to be under. Not counted: a remake refused and sent again later, which
        goes as a first send, and one whose market price was missing, held (`_hold`) and sent later."""
        row = None if slot.protect_id is None else self.sale_rows.get(slot.protect_id.value)
        if row is None or row["kind"] != "trailing_stop_limit":
            return
        row["remake"] = True
        if row["trailing_offset"] is None or row["spread_floor"] is None:
            return
        row["under_floor"] = Decimal(row["trailing_offset"]) < Decimal(row["spread_floor"])
        if row["under_floor"]:
            self.remakes_under_floor += 1

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
        if slot.held_since is None:
            received = slot.entry_received_ns
            if received is None:
                # no receipt on this node (a restart, or fills reconciled before the handler ran): the
                # venue's time of the entry's first fill, as before the one-clock change, so a holding
                # found long after its fill is not held a further 60 s (code-review-high-succ2/8)
                received = self._entry_filled_ns(slot)
            # receipt and `now` are both this host's clock; a hold after the holding was trailed starts now
            slot.held_since = now if slot.trailed or received is None else received
            anchor = self._held_anchor(slot)
            if anchor is not None:
                plan = slot.trail_plan
                distance = offsets_in_force(
                    slot,
                    anchor,
                    self.settings.trail if plan is None else plan.trail,
                    self.settings.exit_through if plan is None else plan.exit_through,
                    self._floor_m(),
                    basis=entry_basis(self._order(slot.entry_id)),
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
            self._note(
                slot,
                f"{slot.instrument_id}: no safe trailing price; holding a stop at {slot.held_stop}",
            )
        self._ensure_exit(slot, held)
        self._held_at_l(slot)
        self._record_held_stop(slot)
        slot.holding_from = self._holding_since(slot)
        low = slot.held_low
        if followed is None:
            followed = self._followed(slot)
        if followed is not None and followed > 0:
            # a price followed as the hold was set up, which no quote or print handler caught because
            # the hold had not begun: it meets the stop in this step, as `_trail` compares the price it
            # follows, and is kept as the hold's low only where `on_quote` or `on_trade` would keep it,
            # so under a print trigger with the holding's start unknown a print from before the entry
            # is not kept once the fill is recorded (#367's code review, finding 1). Read here when no
            # caller hands it, so every caller compares it; a low kept above today's stop can meet a
            # stop a later fill raises (the closing unit's reader, limit 9)
            low = followed if low is None else min(low, followed)
            if self.settings.trigger == TriggerType.BID_ASK or slot.holding_from is not None:
                slot.held_low = low
        last, source = self._last_known(slot)
        met, by = last, source
        if slot.held_stop is not None and low is not None and low <= slot.held_stop:
            # a followed price since the hold began met the stop: a met held stop is never forgotten, and
            # a later print the holding does not own (a late one stamped before the entry) must not hide
            # it (the lead's H3, 2026-11-13 09:20; the DeepSeek reader of the successor, P2). A low that
            # never met the stop prices nothing here, the unpriced exit included (code-review-high-succ2/1)
            met, by = low, "followed price"
        if slot.held_stop is not None and met is not None and met <= slot.held_stop:
            slot.exiting = True
            self._mark_exit(slot, now, met, f"held stop ({by})")
            if slot.entry_record is not None:
                slot.entry_record["held_stop_trigger"] = by
            slot.exit_trigger = slot.held_stop  # before `_end_hold` clears it
            self._end_hold(slot, now, "held_stop")
            self._limit_sell(slot, held, now, slot.floor, anchor=met)
            return
        # `unpriced_exit` is for a holding with no two-sided quote; under `LAST_PRICE` one with a
        # two-sided quote and no print of its own waits for a print or the wind-down
        if now - slot.held_since >= HOLD_MOST_SECS * SECOND_NS and not (
            self.settings.trigger == TriggerType.LAST_PRICE
            # a locked or crossed quote has both sides: it waits too (w3-d-succ2/P2)
            and two_sided(self.cache.quote(slot.instrument_id))
        ):
            anchor = last if last is not None else slot.protection_anchor
            slot.exiting = True
            self._mark_exit(slot, now, last, "unpriced_exit")
            self._end_hold(slot, now, "unpriced_exit")
            self._limit_sell(slot, held, now, slot.floor, anchor=anchor)

    def _held_at_l(self, slot: Slot) -> None:
        """S2 under #320's held stop: an entry sized for L holds its stop L under the average fill,
        rounded up onto the step (`fill_trigger`), the level its trailing sell would start from, and
        follows the average as the entry fills further, as a trailing sell remade for all that is held
        would; a trigger a trailed sale had reached still holds it up. A recorded tightening (the
        ratchet's, the warmed ATR's or a profile's) follows the average too, at the distance in force
        (`offsets_in_force`: the tightened trail, never wider than L), so a later fill at a higher
        average moves the stop (dsh-reader-337, P1). No room for L under the average keeps #320's
        level, tightened or not."""
        if slot.sized_loss is None:
            return
        anchor = self._held_anchor(slot)
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
                self.settings.trail if plan is None else plan.trail,
                self.settings.exit_through if plan is None else plan.exit_through,
                self._floor_m(),
                basis=entry_basis(self._order(slot.entry_id)),
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
        if slot.exit_record is None or slot.held_stop is None:
            return
        # a step that cannot read the average (the entry gone from the cache, no position average,
        # no limit price) measures the level in force from the last average a step read, and with none
        # read records no distance. The protection anchor is no stand-in: it can date from the hold's
        # first step, or be a bid (`_trail_anchor`) (r-dsh-heldrec P2, P3)
        anchor = self._held_anchor(slot)
        if anchor is not None:
            slot.held_average = anchor
        slot.exit_record.held_stop_kept = slot.held_stop
        slot.exit_record.held_stop_from_average = (
            None if slot.held_average is None else slot.held_average - slot.held_stop
        )

    def _end_hold(self, slot: Slot, now: int, path: str) -> None:
        """Record how the held stop ended and for how long it held; clear it."""
        if slot.held_since is None:
            return
        if slot.entry_record is not None:
            slot.entry_record["protect_path"] = path
            slot.entry_record["held_stop_secs"] = (now - slot.held_since) / SECOND_NS
        self.protect_paths[path] += 1
        slot.held_stop = slot.held_since = slot.held_low = slot.held_at_l = None
        slot.holding_from = None

    def _held_anchor(self, slot: Slot) -> Decimal | None:
        """The whole holding's average, else the entry's average fill, else its limit."""
        average = self._position_average(slot)
        if average is not None:
            return average
        entry = self._order(slot.entry_id)
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
        kept = slot.newest_print
        trade = self.cache.trade(slot.instrument_id)
        at = None if trade is None else print_time(trade, slot.quote_lag)
        if trade is not None and at is not None and (kept is None or at > kept.event_ns):
            return Print.of(trade, slot.quote_lag)
        return kept

    def _last_print(self, slot: Slot) -> Decimal | None:
        """The newest trade's price, else what the held stop reads (`_last_known`): the anchor of a
        sale with no bid. With no print, that is the latest minute close under `BID_ASK` and nothing
        under `LAST_PRICE`, where a close is no price of this holding (code-review-high-succ/3)."""
        newest = self._newest_trade(slot)
        if newest is not None:
            return newest.price
        return self._last_known(slot)[0]

    def _last_known(self, slot: Slot) -> tuple[Decimal | None, str]:
        """The newest trade by venue time, else the latest minute close: what the held stop reads.
        Under `LAST_PRICE` only a trade of this holding (`_holding_trade`): with none, a minute close is
        no newer than a print of it, so nothing is known (code-review-high-succ/3); under `BID_ASK` as
        #320 made it."""
        trade = self._newest_trade(slot)
        if self.settings.trigger == TriggerType.LAST_PRICE:
            trade = None if trade is None else self._holding_trade(slot, trade)
            return (None, "none") if trade is None else (trade.price, "last trade")
        if trade is not None:
            return trade.price, "last trade"
        if slot.closes:
            return slot.closes[-1], "minute close"
        return None, "none"

    def _position_average(self, slot: Slot) -> Decimal | None:
        """Refresh through the holding reader; a quantity-only seam supplies no average."""
        slot.holding_average = None
        held = getattr(self, "_held", None)
        if held is not None:
            held(slot)
        elif hasattr(self.cache, "positions_open"):
            # Method-only seams may borrow the protection path without the holding reader.
            Mover._held(self, slot)
        return slot.holding_average

    def _trail_anchor(self, slot: Slot, *, record: bool = True) -> Decimal:
        """The cache's quantity-weighted holding average, else the entry fill, else the current bid.

        `Cache.positions_open(instrument_id, strategy_id)` and `Position.avg_px_open/signed_qty`
        are pinned rc8's contracts. Missing or non-positive/non-finite averages cannot anchor a sale.
        Each fallback use (including a refused remake) is counted, without changing entry gating.
        """
        entry = self._order(slot.entry_id)
        average = self._position_average(slot)
        if entry is not None and entry.avg_px is not None:
            filled = Decimal(str(entry.avg_px))
            if filled.is_finite() and filled > 0:
                # one entry's holding averages its own fill: the same value, kept as the fill reads
                return filled if average is None or average == filled else average
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
        if record:
            self.protection_fallbacks[source] += 1
            self._note(
                slot,
                f"{slot.instrument_id}: no filled entry price; trailing sell anchored on {source} {anchor}",
            )
        return anchor

    def _followed(self, slot: Slot) -> Decimal | None:
        """The trigger's followed price: the bid for BID_ASK, the newest trade of this holding for
        LAST_PRICE (`_holding_trade`); None before the first."""
        if self.settings.trigger == TriggerType.BID_ASK:
            quote = self.cache.quote(slot.instrument_id)
            return None if quote is None else quote.bid_price.as_decimal()
        trade = self._holding_trade(slot)
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
        trade = self._newest_trade(slot) if trade is None else trade
        if trade is None:
            return None
        since = self._holding_since(slot)
        return trade if since is None or trade.event_ns >= since else None

    def _holding_since(self, slot: Slot) -> int | None:
        """The venue time from which a print is of this holding: the earlier of Alpaca's acceptance of
        the entry (`ts_accepted`) and its first fill on record, or None with no entry fill on record.
        `_holding_trade` and the held stop's low (`on_trade`, through `Slot.holding_from`) both read
        it, so a print one follows the other counts."""
        filled = self._entry_filled_ns(slot)
        if filled is None:
            return None
        entry = self._order(slot.entry_id)
        accepted = None if entry is None else getattr(entry, "ts_accepted", None)
        # a 0 is an unset time, no boundary: the pinned order reads None until accepted and the first
        # fill sets it if acceptance did not (NautilusTrader d53849b216e2, crates/model/src/python/orders/
        # mod.rs:188-190, crates/model/src/orders/mod.rs:1413-1414), but an own-book copy defaults it to 0
        # (orders/mod.rs:539) and a stand-in or restored order may carry that
        return filled if not accepted else min(filled, int(accepted))

    def _entry_filled_ns(self, slot: Slot) -> int | None:
        """The venue's time of the entry's first fill on record, or None."""
        entry = None if slot.entry_id is None else slot.entry_id.value
        return min((ns for cid, _, _, _, ns in self.fills if cid == entry), default=None)

    def _chase(
        self, slot: Slot, order: Any, held: Decimal, now: int, floor: Decimal | None
    ) -> None:
        """Re-price a sell limit that still rests after `chase_secs`: `exit_ticks` through the bid, not
        below `floor`, and only downwards. The soft line does not hold it. `chase_secs` paces it, and
        the engine's submit throttle (`paper_node.REQUEST_CEILING` on a paper node, else the run's own
        rate) counts the remade sale but not the cancel, so a chase is two requests to Alpaca."""
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
        # Sales are exempt from the soft line and stop only at the engine's hard ceiling (the lead's
        # decision of 2026-11-12; ADR 0091: no new bound on the sale path).
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
            # no bid: the given anchor, else the newest trade or minute close, else the entry's anchor
            bid = anchor if anchor is not None else self._last_print(slot)
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
            reduce_only=slot.adopted,
        )
        slot.protect_id = order.client_order_id
        if slot.exit_trigger is not None:
            self.sell_triggers[order.client_order_id.value] = str(slot.exit_trigger)
        slot.priced_ns = now
        self._say(f"{slot.instrument_id}: SELL {held} limit {price} (floor {floor})")
        self._sale_sent(slot, order, now, slot.exit_trigger)
        self.submit_order(order, params=dict(EXTENDED_HOURS))

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
        self.recent = recent
        candidates = [slot for slot in self.slots.values() if slot.run_ns is not None]
        if not candidates:
            return
        ranked = self._ranked(now)
        candidates.sort(key=lambda slot: ranked.get(slot.instrument_id, (RANK_MOST, 0))[0])
        for slot in candidates:
            run_close, seen = slot.run_close, slot.run_seen_ns
            try:
                if slot.was_held or self._working(slot) or self._held(slot):
                    continue
                why = self._refusal(slot, run_close, ranked, self.recent, now)
                if why is None:
                    why = self._buy(slot, ranked[slot.instrument_id][1], seen, run_close)
            finally:
                # Preserve C0's override contracts: arm and run state are plain slot data, never a
                # new keyword sent through a replay subclass's _buy or _refusal override. A run is
                # decided on once: its fields are cleared after the decision, so `_refusal` reads them.
                slot.run_arm = None
                slot.run_ns = slot.run_close = slot.run_seen_ns = slot.run_closed_ns = None
            if why is not None:
                if not isinstance(why, Refused):  # already a row of `refusals` (record rule 1)
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
        """Why this candidate is not entered now, or None. The slot keeps its run through this
        decision, preserving C0's five-argument override contract."""
        settings = self.settings
        if not self._fresh(now):
            if self.rank_malformed:
                return "the last ranking was malformed"
            return "the ranking is stale" if self.rank_ns else "no ranking yet"
        opening = slot.run_arm == "opening"
        if slot.instrument_id not in ranked:
            return "no longer ranked"
        move = ranked[slot.instrument_id][1]
        if move > settings.max_move:
            return f"moved {move}, over the no-chase cap of {settings.max_move}"
        if opening and (why := self._opening_refusal(slot, now)) is not None:
            return why
        if slot.instrument_id in self.sold and not opening:
            sold_ns, sold_px = self.sold[slot.instrument_id]
            if settings.reentry_arm == "clock":
                if now - sold_ns < settings.cooldown_secs * SECOND_NS:
                    return "inside the cooldown after its sale"
                from .movers_rank_actor import trade_date

                # The lead (2026-11-13): every sale has a price, so the clock arm's above-sale rule
                # holds on the sale's own trade date only, with its 20:00 rollover, as the opening
                # arm's `SOLD_THIS_TRADE_DATE` does; a Mover that runs a replay's whole sample does not
                # carry one session's sale into the next, as a window's Mover never can
                sale_date = trade_date(self.sold_fill_ns.get(slot.instrument_id, sold_ns))
                if (
                    sold_px is not None
                    and run_close is not None
                    and run_close <= sold_px
                    and sale_date == trade_date(now)
                ):
                    return f"its run closed at {run_close}, not above the sale at {sold_px}"
            else:
                filled_ns = self.sold_fill_ns.get(slot.instrument_id, sold_ns)
                # the minute's end, not its receipt: a streamed close received after the fill can
                # still precede it (the batch A head reader, A-2)
                closed = slot.run_closed_ns
                if closed is None:
                    reason = "qualifying close interval end unavailable"
                    self.refusals.append(
                        {
                            "when_ns": now,
                            "instrument_id": str(slot.instrument_id),
                            "reason": reason,
                        }
                    )
                    return Refused(reason)
                if closed <= filled_ns:
                    return "its qualifying close preceded the sale fill"
                state = self.reentry_states.get(slot.instrument_id)
                if (
                    settings.reentry_arm in ("turn", "vwap")
                    and state is not None
                    and not state.history_complete
                ):
                    reason = (
                        f"post-sale close history exceeded {PENDING_REENTRY_BARS} pending intervals"
                    )
                    self.refusals.append(
                        {"when_ns": now, "instrument_id": str(slot.instrument_id), "reason": reason}
                    )
                    return Refused(reason)
                if settings.reentry_arm == "reclaim":
                    if sold_px is None:
                        return "reclaim sale price unavailable"
                    if run_close is None or run_close <= sold_px:
                        return f"its run closed at {run_close}, not above the sale at {sold_px}"
                elif settings.reentry_arm == "turn":
                    if state is None:
                        return "turn state or trail distance unavailable"
                    why = state.turn_refusal(
                        state.sale_price, state.trail_distance, settings.turn_k, settings.turn_d
                    )
                    if why is not None:
                        return why
                elif state is None or state.vwap is None:
                    return "vwap unavailable"
                elif not state.crossed_vwap:
                    return "vwap needs a close below then the first close above"
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

    # The adapter increments its count in a worker thread (execution.py:_call, providers.py:sent).
    # Reserve each venue command here for the rest of the step; the next step samples the adapter
    # anew. Never add a second adapter read to these reserves: a command already counted by the
    # transport would be counted twice. Limit: a command still queued in that thread when the next
    # step samples is in neither count until it is sent; the soft line's margin under Alpaca's
    # limit is what absorbs it. Limit, the conservative side: a submit the risk engine denies is
    # reserved for the rest of the step all the same, since its `OrderDenied` reaches the strategy
    # only after the step's decisions (the xr7 reader's P2; `on_order_denied` follows them in the
    # backtest engine too); the next step samples the adapter anew.
    # rc8 Strategy signatures: trading/__init__.pyi:538-544, :571-576, :613-618; routing to the local
    # emulator instead of execution: crates/trading/src/strategy/mod.rs:646-655, :2436-2442.
    def submit_order(
        self, order: Any, position_id: Any = None, client_id: Any = None, params: dict | None = None
    ) -> None:
        emulated = order.emulation_trigger is not None
        super().submit_order(order, position_id, client_id, params)
        # An emulated order the bid or print is already through is matched as it is submitted and
        # released to the venue at once (emulator.rs:679-699 at the pin): a venue request too.
        released = self._order(order.client_order_id) if emulated else None
        if not emulated or (released is not None and not released.is_emulated):
            self.recent += 1

    def cancel_order(
        self, client_order_id: ClientOrderId, client_id: Any = None, params: dict | None = None
    ) -> None:
        order = self._order(client_order_id)
        if (
            order is not None
            and order.emulation_trigger is None
            and not order.is_emulated
            and not order.is_closed
            and not order.is_pending_cancel
        ):
            self.recent += 1
        super().cancel_order(client_order_id, client_id, params)

    def query_account(
        self, account_id: Any, client_id: Any = None, params: dict | None = None
    ) -> None:
        self.recent += 1
        super().query_account(account_id, client_id, params)

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
        opening = slot.run_arm == "opening"
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
            plan = self._entry_trail(slot, price, quote, now=self.clock.timestamp_ns())
        except DistanceRefusal as refusal:
            now, name = self.clock.timestamp_ns(), str(slot.instrument_id)
            row = next(
                (
                    row
                    for row in self.refusals
                    if row["reason"] == refusal.cause and row["instrument_id"] == name
                ),
                None,
            )
            if row is None:  # one row a name and cause: its first refusal, and the decisions since
                self.refusals.append(
                    {
                        "when_ns": now,
                        "instrument_id": name,
                        "reason": refusal.cause,
                        "distance": str(refusal.distance),
                        "cap": str(TRAIL_CAP),
                        "decisions": 1,
                        "last_ns": now,
                    }
                )
            else:
                row.update(decisions=row["decisions"] + 1, last_ns=now)
            return Refused(f"{refusal.cause}: {refusal}")
        except ValueError as error:
            return str(error)
        opening_record = None
        # the opening's low, distance and fall, for its re-entry row
        measured: tuple[Decimal, Decimal, Decimal] | None = None
        if opening:
            state = self.opening_states[slot.instrument_id]
            distance = offsets(price, plan.trail, plan.exit_through)[0]
            # The fall is the opening's own row, f, alone; the re-entry row's d is never read here
            # (the lead's ruling on review finding (3), 2026-11-12). The turn's k is shared.
            why = state.turn_refusal(state.open, distance, settings.turn_k, settings.opening_f)
            if why is not None:
                return f"opening: {why}"
            assert state.low is not None
            fall = (state.open - state.low) / distance
            measured = (state.low, distance, fall)
            opening_record = {
                "day": state.day.isoformat(),
                "open": str(state.open),
                "low": str(state.low),
                "low_minute": state.low_minute,
                "fall_in_trail_distances": str(fall),
                "trail_distance": str(distance),
                "trail_source": plan.source or plan.bound,
                "turn_minute": state.turn_minute,
                "turn_ns": state.turn_ns,
                "entry_limit": str(price),
                "entry_price": None,
                "M": settings.opening_minutes,
                "f": str(settings.opening_f),
                "k": settings.turn_k,
            }
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
        floor_m = self._floor_m()
        least = None if floor_m is None or plan.spread is None else floor_m * plan.spread
        if stop is not None and least is not None and least >= price:
            # the legacy path sizes no loss: a spread floor in price terms (A3+O's O3) at or over the
            # entry leaves no room for any stop under it, which the planned stop's fraction alone
            # would admit (w1-t-l5, P4)
            return (
                f"sizing refused: the spread floor {least} is at or over the entry {price}, no room"
            )
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
            if self.recent >= settings.soft_line:
                # the query just sent is a venue request too (`query_account`): `_refusal` passed
                # this buy before it, so the soft line is read again before the buy adds one
                return f"{self.recent} requests in the last 60 s, at the soft line of {settings.soft_line}"
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

            def size(trail: Decimal, why: str = "") -> LossPlan | str:
                try:
                    return plan_for_loss(
                        instrument=instrument,
                        entry=Price.from_str(f"{price:.{precision}f}"),
                        distance=trail,
                        planned_loss=planned_loss,
                        flow_cap=share_cap,
                        budget=entry_budget,
                    )
                except (ValueError, TypeError) as error:
                    name = str(error)
                    if name not in SIZING_REFUSALS:
                        # upstream's own error, not a refusal: named, not counted, and the step goes
                        # on to its other candidates rather than lose them
                        return f"sizing failed, not a refusal: {type(error).__name__}: {error}"
                    self.sizing_refusals[name] += 1
                    return (
                        f"sizing refused: {name} (trail {trail}, planned loss {planned_loss} USD, "
                        f"budget {entry_budget} USD, share cap {share_cap}, at {price}){why}"
                    )

            sized_trail = plan.trail
            sized = size(sized_trail)
            if isinstance(sized, str):
                return sized
            quantity, entry_stop = sized
            # L on the entry's step, down: where the stop crosses $1 its sub-dollar step leaves a
            # residue a cent-priced bid cannot trail by, and down keeps the loss within the plan
            distance = (price - entry_stop).quantize(tick(price), rounding=ROUND_FLOOR)
            # The lead's cross-dollar property of 2026-11-13 (#338's second reader, P1-1): in an arm
            # that takes the spread floor, L on the entry's step is at or above the floor at that
            # step. Rounded down under it, L is taken up onto the step and the size taken again for
            # it, so the loss at the stop stays within the plan; one that still cannot hold the
            # floor is refused by name and counted.
            least = None if plan.floor is None else price * plan.floor
            if least is not None and distance < least:
                wider = (price - entry_stop).quantize(tick(price), rounding=ROUND_CEILING)
                # the fraction rounded down, so `sized_stop`'s product is at most `wider` and its
                # stop, rounded down onto the step, is `price - wider` itself, which is on it: the
                # size, its stop and L are one distance (the A3+O reader, P2: 0.04 / 1.03 rounded
                # to nearest put the stop at 0.9899 under an L of 0.04)
                with localcontext(Context(prec=28, rounding=ROUND_FLOOR)):
                    sized_trail = wider / price
                sized = size(sized_trail, "; re-sized for the spread floor")
                if isinstance(sized, str):
                    return sized
                quantity, entry_stop = sized
                distance = (price - entry_stop).quantize(tick(price), rounding=ROUND_FLOOR)
                if distance < least:
                    self.sizing_refusals[DISTANCE_UNDER_FLOOR] += 1
                    return (
                        f"the distance {distance} on the step {tick(price)} for an entry at {price} "
                        f"is under the spread floor {least}: no trail at the floor holds the loss"
                    )
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
                # the fraction the size was taken at: the plan's, or L taken up onto the step
                "sized_trail": str(sized_trail),
                "stop": str(entry_stop),
                "loss_shares": str(loss_shares),
                # L, the distance the trailing sell protects at: the D3 check reads it as the
                # opening distance (`d3_check.py`, `_opening_floor`); the loss at the sizing stop,
                # `price - stop`, has its own key, so the row names each distance once (w1-d-351, P2)
                "loss_per_share": str(distance),
                "stop_loss_per_share": str(price - entry_stop),
                "loss_at_stop": str(quantity.as_decimal() * (price - entry_stop)),
                "binding": binding,
                "held_by": held_by,
                # None: no start-up roster to be in or outside of
                "roster": None
                if settings.share_caps is None
                else slot.instrument_id in settings.share_caps,
                "quantity": str(quantity),
                "notional": str(quantity.as_decimal() * price),
            }
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
        # ADR 0115: read at this buy decision, after every other check and before the order or
        # sizing log exists. Main's startup share and position caps remain fixed; no sale reads it.
        switch = self._arming_read()
        if not switch.armed:
            return self._switch_off(slot, switch)
        self._switch_armed()
        if sizing is not None:
            self._say(
                f"{slot.instrument_id}: size for a loss of {sizing['planned_loss']} USD at the stop "
                f"{sizing['stop']}: {sizing['loss_shares']} shares; budget {sizing['budget_notional']} "
                f"USD ({sizing['budget_shares']} shares), flow cap {sizing['flow_shares']} shares from "
                f"{sizing['flow_dollars']} USD, binding {sizing['binding']}, quantity {quantity}"
            )
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
        slot.ratcheted = slot.tightened = slot.warm_checked = False
        slot.profile_evaluated_ns = slot.run_open_ns
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
            "entry_price": None,
            "stopped_out": False,
            "arming": switch.read,
            "reentry": self._reentry_row(slot, measured, run_close, order),
            "exit_through": str(plan.exit_through),
            "chase_cap": str(plan.chase_cap),
            "baseline_dollar_volume_per_minute": (
                None
                if slot.instrument_id not in self.baseline_dollar_volume_per_minute
                else str(self.baseline_dollar_volume_per_minute[slot.instrument_id])
            ),
            "liquidity_basis": "baseline dollar volume per minute, a stand-in for size",
            "ratchet": {"tightened": False, "when_ns": None, "from": None, "to": None},
            "trail_changes": [],
            "atr_warm_bars": slot.atr_warm_bars,
            "profile": None if plan.profile is None else profile_record(plan.profile),
            "notes": self._trail_notes() + ([] if plan.note is None else [plan.note]),
        }
        if self.context_reader is not None or self.context_reads:
            slot.entry_record.update(
                context_source=self.context_sources.get(
                    slot.instrument_id,
                    "roster" if slot.instrument_id in self.entry_contexts else None,
                ),
                context_seconds=self._context_seconds(slot.instrument_id),
                baseline_status=(
                    "supplied"
                    if slot.instrument_id in self.baseline_dollar_volume_per_minute
                    else "absent: not in the startup roster"
                    if slot.instrument_id in self.instrument_loads
                    else "absent: not supplied"
                ),
                position_cap=(
                    "native PositionCap and Mover sizing"
                    if slot.instrument_id in self.position_caps
                    else "Mover sizing only; no native PositionCap"
                ),
            )
        if opening_record is not None:
            slot.entry_record.update(arm="opening", opening=opening_record)
            slot.entry_record["notes"].append(OPENING_LIMIT)
            state = self.opening_states[slot.instrument_id]
            state.used, state.order_id = True, order.client_order_id
        if settings.trail_mode == "profile":
            slot.entry_record["profile_skips"] = []
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

    def _reentry_row(
        self,
        slot: Slot,
        opening: tuple[Decimal, Decimal, Decimal] | None,
        run_close: Decimal | None,
        order: Any,
    ) -> dict[str, Any] | None:
        """An entry's re-entry row, for a name this run (or its carry) has sold; else None.

        The lead's rows for the 09:30 firing (2026-11-12): the rule that fired (`opening`, or the
        re-entry arm), the fall and the rise it measured in the distance it measured them in, and the
        seconds from the sale's fill to this entry's submit. The two rules measure from different
        points, which `fall_from` names: the re-entry arm from the sale price, to the lowest minute
        close, in the sale's trail distance; the opening (`opening` is its low, distance and fall) from
        its open, to the lowest minute low, in the entry's trail distance. `secs_since_stop_out` counts
        from the name's last full sale, whatever sold it (`exits` gives the reason).
        """
        sold = self.sold.get(slot.instrument_id)
        if sold is None:
            return None
        sale_ns = self.sold_fill_ns.get(slot.instrument_id, sold[0])
        sale_price = sold[1]
        state = self.reentry_states.get(slot.instrument_id)
        distance: Decimal | None
        if opening is not None:
            low, distance, fall = opening
        else:
            low = None if state is None else state.low
            distance = None if state is None else state.trail_distance
            fall = (
                None
                if sale_price is None or low is None or not distance
                else (sale_price - low) / distance
            )
        rise = (
            None
            if run_close is None or low is None or not distance
            else (run_close - low) / distance
        )
        return {
            "arm": self.settings.reentry_arm,
            "rule": "opening" if opening is not None else self.settings.reentry_arm,
            "fall_from": "open" if opening is not None else "sale",
            "sale_ns": sale_ns,
            "sale_price": None if sale_price is None else str(sale_price),
            "low": None if low is None else str(low),
            "run_close": None if run_close is None else str(run_close),
            "distance": None if distance is None else str(distance),
            "fall_in_distances": None if fall is None else str(fall),
            "rise_in_distances": None if rise is None else str(rise),
            "secs_since_stop_out": (order.init_event.ts_init - sale_ns) / SECOND_NS,
            "reentry_price": None,
            "minutes_between": None,
        }

    def _switch_off(self, slot: Slot, switch: Arming) -> str:
        """Record a buy the switch stopped, and warn once per name for each thing read while off."""
        self.entries_off.append(
            {
                "when_ns": self.clock.timestamp_ns(),
                "instrument_id": str(slot.instrument_id),
                "kind": "opening"
                if slot.run_arm == "opening"
                else "reentry"
                if slot.instrument_id in self.sold
                else "entry",
                "read": switch.read,
            }
        )
        if self.arming_warned.get(slot.instrument_id) != switch.read:
            self.arming_warned[slot.instrument_id] = switch.read
            self.arming_warnings.append((str(slot.instrument_id), switch.read))
            self.log.warning(f"{slot.instrument_id}: no entry while the switch reads {switch.read}")
        return f"the entry switch reads {switch.read}"

    def _switch_armed(self) -> None:
        """An armed read, for any name, ends the off period: the next stop of every name is warned."""
        self.arming_warned.clear()

    def _floor_m(self) -> Decimal | None:
        """The spread floor's multiple a sale's offset keeps (`offsets_in_force`), or None in the fixed
        arm, `MoverConfig`'s default, which takes no floor and does not move (the lead's O1-O3 of
        2026-11-12). A Mover window that names no arm runs OWN1, which takes it."""
        return None if self.settings.trail_mode == "fixed" else self.settings.spread_floor_m

    def _arming_read(self) -> Arming:
        """The entry switch now (`arming.read_arming`); nothing is cached between decisions."""
        path = self.settings.arming_path
        return read_arming(
            None if path is None else Path(path),
            absent_is_on=self.settings.arming_absent_is_on,
        )

    def _entry_trail(
        self,
        slot: Slot,
        price: Decimal,
        quote: Any | None,
        *,
        now: int | None = None,
        profile_price: Decimal | None = None,
        found_holding: bool = False,
        allow_no_quote: bool = False,
    ) -> TrailPlan:
        """Freeze T1 at entry. A spread floor or distance the cap forbids refuses the entry by name
        (`DistanceRefusal`); an unusable spread selects the configured fixed plan, and a cold ATR the
        fixed plan raised to the spread floor at the step (`_cold_plan`), by name. A held profile's
        warm check supplies its completed close as the ATR basis, keeping `price` as its anchor.
        `found_holding` lets protect retain an own-cap holding's distance without choosing an entry
        size; that caller enforces `sizing.DISTANCE_CEILING` (ADR 0142, 2026-11-13). Only adoption may
        take the no-spread plan before a quote; entries still refuse it."""
        settings = self.settings
        if settings.trail_mode == "profile" and self.trail_profile is None:
            raise ValueError("profile document is missing; no profile entry is allowed")
        atr = None if slot.atr is None or not slot.atr.initialized else Decimal(str(slot.atr.value))
        if quote is None and not allow_no_quote:
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
        if floor > TRAIL_CAP and (
            settings.trail_cap != "own" or not (settings.size_from_budget or found_holding)
        ):
            raise DistanceRefusal(
                "floor_over_cap",
                floor,
                "the spread floor exceeds the 0.02 cap; no entry satisfies both",
            )
        baseline = self.baseline_dollar_volume_per_minute.get(slot.instrument_id)
        base = settings.ratchet_base if settings.trail_mode == "ratchet" else settings.trail_mode
        tier = self._tier_applies(slot)
        if atr is None and slot.run_arm == "opening":
            # The coordinator's rule for R2: a cold-ATR opening entry uses the fixed plan, a top-tier
            # name included, recorded as cold_atr_fallback; the lead's O1 of 2026-11-12: the fixed
            # plan is raised to a spread floor over it. An opening is always top tier, so under a
            # tier base (`tier`, `profile`, or `ratchet` on either) it keeps the fixed plan for the life
            # of the position: once the ATR warms, `_warm_tighten` re-selects tier, not the warmed
            # ATR's plan, and keeps the fixed one (the lead's ruling of 2026-11-13, 03:02: a tier plan
            # would sell by a change of rule, not by price). Under the `atr` base it may tighten (b).
            return self._cold_plan(
                f"ATR has fewer than {settings.atr_n} complete one-minute bars: opening uses the "
                "fixed trail",
                spread,
                floor,
                None,
            )
        note = (
            "baseline dollar volume per minute is unknown: tier uses ATR, a stand-in for size"
            if base == "tier" and baseline is None
            else None
        )
        calculation = None
        source = None
        thin = None
        if tier:
            selected, bound = TIER_TRAIL, "tier"
            if base == "profile":
                source = "tier"
        else:
            if base == "profile":
                assert self.trail_profile is not None
                calculation = profile_distance(
                    self.trail_profile,
                    slot.instrument_id.symbol.value,
                    profile_segment(self.clock.timestamp_ns() if now is None else now),
                    None
                    if atr is None
                    else atr / (price if profile_price is None else profile_price),
                    self.rdc_1.get(slot.instrument_id),
                    **{name: getattr(settings, name) for name in PROFILE_SETTINGS},
                )
                thin = calculation["reason"]
            if calculation is not None and thin is None:
                selected, bound, source = calculation["distance"], "profile", "profile"
            elif atr is None:
                # Coordinator's (ii), 2026-11-12: a cold ATR enters on the fixed plan, named, rather
                # than waiting for `atr_n` minutes; the slot's opening asked for history (F-2,
                # `_request_history`), so it is cold only when that gave fewer than `atr_n` bars, was
                # refused, or has not been answered yet.
                cold = f"ATR has fewer than {settings.atr_n} complete one-minute bars: fixed trail fallback"
                return self._cold_plan(cold, spread, floor, calculation, note)
            else:
                selected, bound = settings.atr_k * atr / price, "atr"
                if thin is not None:
                    source = "atr"  # categorical; the thin reason is in the calculation
        measure = selected
        if floor >= selected:
            selected, bound = floor, "spread floor"
        if selected > TRAIL_CAP:
            if settings.trail_cap == "own" and not (settings.size_from_budget or found_holding):
                # Entries need sizing; protect checks the ceiling for a holding already found.
                raise DistanceRefusal(
                    "distance_over_cap",
                    selected,
                    f"the trail {selected} ({bound}) is over the 0.02 cap, and trail_cap own needs "
                    "size_from_budget to size it; refused",
                )
            if settings.trail_cap == "refuse":
                raise DistanceRefusal(
                    "distance_over_cap",
                    selected,
                    f"the trail {selected} ({bound}) is over the 0.02 cap; refused (trail_cap refuse)",
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
            source=source,
            floor=floor,
            measure=measure,
            profile=calculation,
        )

    def _cold_plan(
        self,
        cold: str,
        spread: Decimal,
        floor: Decimal,
        calculation: dict[str, Any] | None,
        note: str | None = None,
    ) -> TrailPlan:
        """A cold-ATR entry's plan, `cold_atr_fallback`: the configured fixed trail, raised to the
        spread floor at the step when that is over it (the lead's O1 of 2026-11-12, in every cap arm;
        `_entry_trail` has already refused a floor the arm's cap forbids). The source stays
        categorical, as the warm check reads it; a thin reason is in the calculation."""
        settings = self.settings
        if floor > settings.trail:
            return TrailPlan(
                trail=floor,
                exit_through=floor / 4,
                chase_cap=max(floor, settings.chase_cap),
                atr=None,
                spread=spread,
                bound="spread floor",
                note=f"{cold}, raised to the spread floor" + ("" if note is None else f"; {note}"),
                source="cold_atr_fallback",
                floor=floor,
                profile=calculation,
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
            profile=calculation,
        )

    def _profile_tighten(self, slot: Slot, now: int) -> bool:
        """Consider each complete minute once, then use ratchet's existing cancel/remake path.

        Known limit: offsets remain PRICE offsets from the holding's entry anchor, as in every
        arm; they do not grow with the followed price. ATR uses this minute's close as its price
        basis, and the latest spread floor is converted at the frozen anchor. A thin update leaves
        the plan alone. Top-tier tier protection and ratchet never combine with this arm.
        """
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
            or self._top_tier(slot)
        ):
            return False  # not a profile holding, so there is no tighten to skip
        if slot.exiting:
            return self._profile_skip(slot, now, opened, "the sale is being chased")
        if self.trail_profile is None:
            return self._profile_skip(slot, now, opened, "no profile document")
        if anchor is None:
            return self._profile_skip(slot, now, opened, "no protection anchor")
        if not slot.closes:
            return self._profile_skip(slot, now, opened, "no complete minute's close")
        quote = self.cache.quote(slot.instrument_id)
        spread = None if quote is None else quoted_spread(quote)
        if spread is None:
            return self._profile_skip(slot, now, opened, "no usable quoted spread")
        floor = self.settings.spread_floor_m * spread / anchor
        if floor > TRAIL_CAP:
            return self._profile_skip(
                slot, now, opened, f"the spread floor exceeds the {TRAIL_CAP} cap"
            )
        atr = None if slot.atr is None or not slot.atr.initialized else Decimal(str(slot.atr.value))
        try:
            calculation = profile_distance(
                self.trail_profile,
                slot.instrument_id.symbol.value,
                profile_segment(opened),
                None if atr is None else atr / slot.closes[-1],
                self.rdc_1.get(slot.instrument_id),
                **{name: getattr(self.settings, name) for name in PROFILE_SETTINGS},
            )
        except (ValueError, ArithmeticError, RuntimeError) as error:
            # Raised from the step timer or a bar this would stop every slot; it is named instead.
            self._note(slot, f"{slot.instrument_id}: profile not re-evaluated: {error}")
            return self._profile_skip(slot, now, opened, f"profile not re-evaluated: {error}")
        if calculation["reason"] is not None:
            return self._profile_skip(slot, now, opened, f"profile: {calculation['reason']}")
        selected, bound = calculation["distance"], "profile"
        if floor >= selected:
            selected, bound = floor, "spread floor"
        if selected > TRAIL_CAP:
            selected, bound = TRAIL_CAP, "0.02 cap"
        before = offsets_in_force(
            slot, anchor, current.trail, current.exit_through, self._floor_m()
        )[0]
        after = offsets(anchor, selected, selected / 4)[0]
        increment = self._instrument(slot.instrument_id).price_increment.as_decimal()
        if selected >= current.trail:
            return self._profile_skip(slot, now, opened, "not narrower than the trail in force")
        if after >= before and l_in_force(slot, anchor) is not None:
            # narrower than the plan but not closer than L in force: taking it would loosen the sale
            # (the lead's ruling of 2026-11-12, 21:01)
            return self._profile_skip(slot, now, opened, f"not narrower than L's {before} in force")
        if before - after < increment:
            return self._profile_skip(
                slot, now, opened, "narrower by less than one price increment"
            )
        plan = replace(
            current,
            trail=selected,
            # Keep the bounds the warm check retained from the cold plan (sr-c-337 P2): a later
            # profile tightening must not lower a reached limit or deepen the chase.
            exit_through=min(selected / 4, current.exit_through),
            chase_cap=min(max(selected, self.settings.chase_cap), current.chase_cap),
            atr=atr,
            spread=spread,
            bound=bound,
            source="profile",
            profile=calculation,
        )
        slot.trail_plan, slot.tightened = plan, True
        self._trail_change(
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
        settings = self.settings
        base = settings.ratchet_base if settings.trail_mode == "ratchet" else settings.trail_mode
        return base in ("tier", "profile") and self._top_tier(slot)

    def _warm_tighten(self, slot: Slot, now: int) -> bool:
        """Once a cold-fallback holding's ATR warms, re-select its trail; take it only if tighter.

        The lead's (b), 2026-11-12: the trail may tighten to the warmed ATR's plan and never widens.
        The comparison is made once, on the first step with a fill price, a usable quoted spread and a
        plan; until then it is retried each step. Each change is recorded with its time in the entry's
        `trail_changes`; the caller cancels and remakes the emulated sale, as for the ratchet.
        """
        plan = slot.trail_plan
        if (
            slot.warm_checked
            or plan is None
            or plan.source != "cold_atr_fallback"
            or slot.atr is None
            or not slot.atr.initialized
        ):
            return False
        bought = self._bought(slot)
        quote = self.cache.quote(slot.instrument_id)
        if bought is None or bought <= 0 or quote is None or quoted_spread(quote) is None:
            return False  # retried next step
        profile_minute = slot.run_open_ns if self.settings.trail_mode == "profile" else None
        profile_price = None
        if profile_minute is not None:
            # This is the complete bar's profile evaluation, even if its plan is kept: use its
            # segment and close, as `_profile_tighten` does, and prevent a second evaluation on the
            # next step after a warm remake (sr-a-337 F1).
            slot.profile_evaluated_ns = profile_minute
            profile_price = slot.closes[-1] if slot.closes else None
        try:
            warmed = self._entry_trail(
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
            self._note(slot, f"{slot.instrument_id}: warmed ATR not applied yet: {error}")
            return False
        slot.warm_checked = True
        if self._tier_applies(slot):  # by the selection, since a spread floor can cover the bound
            self._note(
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
            self._note(
                slot,
                f"{slot.instrument_id}: the plan re-selected with the warmed ATR ({warmed.bound} "
                f"{warmed.trail}) is not tighter than {plan.trail}; kept",
            )
            return False
        in_force = offsets_in_force(slot, bought, plan.trail, plan.exit_through, self._floor_m())
        candidate = offsets(bought, warmed.trail, warmed.exit_through)
        if l_in_force(slot, bought) is not None and not candidate < in_force:
            # narrower than the plan but not closer than L in force: taking it would loosen the sale
            # (the lead's ruling of 2026-11-12, 21:01)
            self._note(
                slot,
                f"{slot.instrument_id}: the plan re-selected with the warmed ATR ({warmed.bound} "
                f"{warmed.trail}) is not tighter than L {in_force[0]} in force; kept",
            )
            return False
        slot.trail_plan, slot.tightened = warmed, True
        if not slot.ratcheted:
            slot.initial_trail = warmed
        self._trail_change(
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
        filled = entry_basis(self._order(slot.entry_id))
        return slot.protection_anchor if filled is None else filled

    def _ratchet(self, slot: Slot, now: int) -> bool:
        """Tighten once only while the original emulated sale works; caller cancels and remakes it."""
        initial = slot.initial_trail
        quote = self.cache.quote(slot.instrument_id)
        followed = self._followed(slot)
        bought = self._bought(slot)
        if initial is None or bought is None or quote is None or followed is None:
            return False
        spread = quoted_spread(quote)
        if spread is None:
            return False
        plan = ratchet_trail(initial, bought, followed, spread, self.settings)
        if plan is None:
            return False
        current = slot.trail_plan or initial
        # The offsets in force are `_trail`'s: L under the fill while it has room and nothing has
        # tightened (the landing review of this fix on S2, finding 5), else the plan's.
        in_force = offsets_in_force(
            slot, bought, current.trail, current.exit_through, self._floor_m()
        )
        if not offsets(bought, plan.trail, plan.exit_through) < in_force:
            # Rounded, the candidate's trigger and then its limit are no closer than those in force:
            # the one ratchet is kept for a later step that can tighten (Astra second read of #316,
            # finding 2).
            return False
        before = slot.trail_plan.trail if slot.trail_plan is not None else initial.trail
        slot.trail_plan, slot.ratcheted, slot.tightened = plan, True, True
        self._trail_change(slot, now, before, plan, "ratchet")
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
