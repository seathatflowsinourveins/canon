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
* U-h: trading/__init__.pyi:674-683,963-1002 supplies request_quotes/request_bars and historical
  hooks. crates/common/src/python/response.rs:145-171,286-340,381-419 sends the historical
  payload, then on_data_response (including failure_reason), then the callback's request-ID
  string. A terminal failure can have no payload (:77-79,423-425). data/engine/mod.rs:1965-1972
  and dispatch.rs:387-390 cache response quotes first; common/cache/mod.rs:2379-2402 pushes
  them to the front regardless of venue stamp. Every fallback here reads venue_quotes instead.
* Overnight cold start (the lead's amended ruling, 2026-11-13): use Mover's BOATS history
  delay and protect.distance_for for the cold rule as well as the warm row. Its Distance.fallback
  names the cold rule. trading/__init__.pyi:552-560 cannot modify a trailing offset;
  warming therefore retains a working sell and never lowers its level.

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
explicit-distance calculation with a dollar distance from protect.distance_for, sharing row_distance with
protect.plan_distance (ADR 0142). The required row is OWN1 or OWN3 from protect.ROWS, without a default
or fraction overrides; F2 is refused by name because the holding's distance must be per symbol.
The distance and limit offset are logged with Distance.source(). At start and adoption, after
subscribing, the protector requests the latest quote on its live feed and bars since the latest
04:00 New York; BOATS history ends the Mover's delay before now. It merges history and cache by
event stamp, excludes an open minute, and passes the last 14 existing minutes to protect.distance_for.
The adapter stamps bars at their open (data.py:1237-1257). Until complete bars exist, a usable quote
arms through protect.distance_for(..., []), naming its fixed fallback as the cold rule. Bars then
replan the distance without lowering the level or replacing a working sell. A carried distance
stays whole; a carried level with no distance takes the higher of its level and the cold rule's
level until bars allow a plan. One to thirteen bars use their own ATR period; with no spread the
ATR stands alone. A flat day with no spread retains the named fixed fallback, counted separately
in END; a carried long with no carried distance takes it as the cold rule, planned again on bars.
Bars allow a plan only once the day's answer is in, or a full ATR's worth exists, judged on the
slice the planner is given. Failed reads are named once and never retried.
The adapter config helper enables
external reports and reduce-only SELL_TO_CLOSE
(canon_alpaca/execution.py:569,614-646,1578); it constructs no client or node.

The startup hold/inbox gate, feed hand-over and account-A paper proofs belong to the landing lane.
A found EXTERNAL long without a supplied level self-arms from bid minus the registered row distance,
rounded up onto Alpaca's step, with the first observation guarded. Planning refusals name the
unprotected holding on every attempt, in the log and the frozen record. Adoption binds only to a
holding in the cache: a level supplied while its holding is not selectable is kept, and arms that
holding when discovery finds it, with the first-observation guard (P11, the lead, 2026-11-13 16:55);
one never applied is named at stop and leaves its book entry untouched. A residual after our completed sale is a new
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
requires a planner-accepted quote at the current maximum. Its age from the earlier of stamp and
receipt is named, and age alone does not refuse it inside the open session part. A holding without
a level waits if that maximum is stamped before the part opens. A supplied level stays fixed.
Seeds set the clock before planning, never count, and never send or price an existing sale.
Among equal stamps the last arrival replaces the snapshot and never counts. Cache.quote() is the
last arrival, not the newest stamp: the pin's cache/mod.rs:2370,
6207-6216,6343-6349 stores reverse insertion order, and common/__init__.pyi:307 exposes quotes().
Adoption seeds the clock from that history and plans from its current maximum, stream or seed.
If the newest quote cannot be planned from, arming waits and every step names the
unprotected holding (V4/I2). Receipt order never decides which quote is newer.
The first observation of a breached or fresh level is guarded; a second confirms (property (b): no
election on one observation). Its other half (e5): once our sell has gone out, the breach is
confirmed, so an exit in progress finishes and the rest of the holding goes out at once with no new
guard, whether our cancel resized the holding (a growth, property (e)) or our sell filled in part
and then ended. Named limits: (1) a host clock more than the bound behind Alpaca's makes every quote
on time insane, so nothing is placed and every step says why; (2) two different quotes with one
stamp are one observation, and the second waits for the next greater stamp; (e4) after a DAY expiry
the level is held and the sell is re-placed at once, the latest usable quote, however old, deciding
only the limit's sign; our own size and re-price cancels use that same retained quote. A crossed
quote or one with a zero size cannot erase it; without any usable quote the holding is named at
ERROR as without its working sell until one arrives. The two observations of the next arming
(U-c) cover the election that follows;
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

U-e (L1's 2026-11-13 handoff): a labeled run reads the level book at startup and writes it at
stop and at each level change (P10): the retained level, or the emulated sell's trigger. At the pin
the emulator raises a trailing trigger by applying an OrderUpdated to the cache and sending it to the
risk engine's process endpoint (crates/execution/src/order_emulator/emulator.rs:1637-1683,
:1823-1827), which only logs it (crates/risk/src/engine/mod.rs:405-410,1991-1997); no strategy
callback is relied on, so the cached order's trigger is compared on each quote and step, and the
book is rewritten only when a holding's level differs from what was last written for it.
The book keys a holding by account label and instrument (U-e A, 2026-11-13): at the pin a
reconciled EXTERNAL position opens at the reconciliation clock, never the venue's open
(crates/live/src/execution/manager.rs:4282-4330, live/src/execution/reconciliation.rs:955-1003,
execution/src/reconciliation/orders.rs:1322,1348, model/src/position.rs:232), so ts_opened names
no holding across runs; the entry keeps its own protection start. Whether the entry's holding is
the one held now is read from the venue (U-e B, start_level.holding_verdict: GET /orders through
the runner's reader, fills counted by fill time over a 92-day walk, review F1): fewer shares sold
since the entry was written than it held, so never flat, and a quantity held now that those sells
and the buys since explain (review F4: a split is not), carries its level. A sell-out is a new
holding, and its entry may be replaced; a failed, incomplete or unexplained reading is unknown:
the holding self-arms, guarded, at a level kept in memory, and the book keeps the entry as written
unless this run's level is higher (review F2). A holding this run sees end has its entry retired:
written with no level, state frozen, reason "holding ended", so no later run carries it (F3). A
level supplied or kept while flat binds a holding found later only when no buy filled after it was
kept (start_level.bought_since). Limit: each of those GETs runs on the event thread at the start of
a holding's planning, with the reader's own bound: canon_alpaca.providers.with_http_timeout's
(connect, read) timeout is mounted on an alpaca-py RESTClient reader at on_start (ADR 0039, review
F6), alpaca-py's retries kept. One walk is at most start_level.PAGES_MOST pages; a carried holding
reads a sell walk and a buy walk, so at most 8 requests, each up to session_window.ADAPTER_CALL_SECS
(about 129 s) with those retries. No async path is built.
A level is rebuilt only from the price series of the live trigger (the lead's ruling, 2026-11-13
17:43): the trail is bid-triggered (crates/execution/src/trailing.rs:128-138), and a trade close
is not a bid. So a carried long arms at its carried level and carried distance, both kept in the
book, and nothing re-derives either: no level rebuilt from closes, no row distance, no fallback.
After the start only bids raise it, through the emulator. Each start names, per carried holding,
the gap since the entry was written as not read; raising the level from the gap's bids is ADR
0151's SIP quotes part, not built. Any other holding is named "no carried level" and self-arms as
unit 10 does, from the bid less the row distance, or arms at its supplied level. The
first-observation guard is unchanged. A carried entry with no distance keeps its level and takes
the row's distance for later raises only; a row that gives only the fixed fallback, or no distance,
leaves the carried level kept and arms nothing, named at each step (ruling (a), 2026-11-13 17:56).
A level both supplied and carried arms at the greater, named (ruling (b)). The book carries the
limit offset beside level and distance; an entry with none takes today's quarter-trail rule, named
(ruling (c)). The runner supplies session_part and a raw asset reader for overnight runs; an
answered eligibility is kept for the run, an unread one retried at most once a minute (review F7),
with refusals named and old entries untouched.
The book is also the node report (the lead, 2026-11-13 17:13): each entry carries its holding's
state (_holding_state maps this strategy's records to armed, guarded, counting, waiting, frozen),
written when it changes, and the file carries last_step_ns per account label, written at most
once per 1 s step, so a killed run shows as a step time that stops advancing. The default book is
one file per account label, and each read, merge and replace holds LevelBook.locked (review F5).
A write failure is named once per kind.
An omitted account label disables persistence by name; no guessed account shares a book key.
There is no runner here to start nodes back to back or supply their history and session bounds.

At breach_count above one, sale_table.json alone decides the sale's transitions and actions;
the driver stores its destination before submit/cancel echoes (strategy/mod.rs:2419, 836-862).
Named limit: the pin does not dispatch order events to a strategy that is not running
(crates/trading/src/strategy/mod.rs:1395-1399). A leg in flight at a whole-node stop is left working,
with an `ALERT:` line naming its instrument, side, quantity and limit, with no order ID; the next start
counts it as cover and does not chase with it, so while the market is under its limit the holding
has NO EFFECTIVE EXIT until that leg expires at the session part's end. The stop's owed cancel is
asked only where the instrument is stopped and the strategy runs on.
Named limit: a predecessor's working sell, including our lineage, is cover; its remainder is
subtracted from the long, never canceled or re-priced here, and a wholly covered long accrues no count.
Named limit: until the adapter's ran-out-path repair is on main, a next leg can be sized from a
count short by the fills lost there.
Named limit: a configured confirm span is refused at a count above one.
At a count above one confirm_gap_ms is required, including an explicit zero. Its value has
no measured source (UNCALIBRATED); 1000 ms is the node configuration's starting value.
U-c2 ruling 15/16: a venue refusal waits for a counted quote after its backoff wait; an unanswered
leg is told once after FLIGHT_SECS and never duplicated. Refusal data lives on the holding,
not the sale or level book. Native statuses follow model/src/orders/mod.rs:207-302.
Named limits at a count above one: added shares wait for the next leg; while the chase floor
holds, one ALERT names the uncovered quantity beside the working remainder. An acknowledgement
waits for the next quote or timer step, up to STEP_NS, before its next leg is sent; the send line
records ack_to_send_ms. An event on an ended holding's leg judges the following holding's row:
its late fill resets the count and its voided fill freezes the new holding. After a fill void
that did not reopen its order, remaining quantity cannot be read back by _remaining; the holding
is already frozen and a subsequent fill is read as its last. The adapter builds no voided fill.
Order handlers only enqueue event payloads. Steps replay them on independent native LimitOrders,
in arrival order (python/orders/limit.rs:463-466, 470-572), so a later cached state cannot supply
an earlier event's facts. Local echoes produced by a step wait for the following step too.
Every wait after a refusal or an outside cancel is counted from the later of the event's own
stamp and its receipt here. Named limit: a stamp ahead of the node's clock lengthens the wait
by the difference.

Review F1 of #443's max review (the lead's ruling, 2026-11-13 22:47 and 22:52): a holding first bought
after the start, in a symbol the node did not load, is named at once and has its exit within a bound.
The node loads the account's symbols once, at its start (protector_node.run_protector), and the
adapter reports no fill or position in an instrument it did not load
(packages/canon-alpaca/src/canon_alpaca/execution.py:369), so no such holding ever reaches the cache.
Given its `roster` (the node's instruments), the protector reads the account's positions
(Alpaca's GET /v2/positions, alpaca-py's get_all_positions, through session_window.position_reads)
at its first step and every ROSTER_POLL_SECS after, in the step timer's handler, which no command of
ours publishes, so the rule for order-event handlers (mover.py:231-238) does not bind it. A whole-share
long outside the roster is named by one alert line with its symbol and quantity, and the run ends by name,
ROSTER_ENDED, through the pinned Strategy.shutdown_system (nautilus-trader d53849b216e2
python/nautilus_trader/trading/__init__.pyi:525, crates/common/src/actor/data_actor.rs:4568-4582): the
live node shuts down on it (crates/live/src/node/mod.rs:1636-1641) as the backtest engine ends on it
(crates/backtest/src/engine.rs:882-884), and the runner's successor reads the account fresh at its
start, so the holding is on its roster. A long outside the roster that the start skips or refuses is
named once and does not itself end the run, since a successor would not protect it. What the successor's
start skips and refuses is one predicate, `session_window.start_account`, which the start and this read
both call: an unreadable position, or one listed at zero, is skipped by name, and a fractional one,
inside or outside the roster, refused, by the adapter's own whole-share rule. While the start would
refuse the account read, a whole-share long outside the roster does not end the run: it is named once as
unprotected with what blocks the restart, the run protects its roster to its planned end, and the first
read the start would accept ends it. The order half of the start's refusal, an open order the adapter
cannot map, runs inside reconciliation and is not called here, so a new holding beside such an order
still ends the run into a refused start (the deputy's ruling, 2026-11-13 23:53). The bound: the buy is
named, and the end asked for, at the first read after it, so within ROSTER_POLL_SECS and one step of the
venue listing it, when the read answers within its own bound (the same reader and the same limit as the
GETs above). The node's stop, the runner's seam and the successor's arming come after that and are not in
the bound; how long they take live is not measured (UNVERIFIED). A failed read is an alert line, read
again at the next interval. A holding outside the roster is never armed by this run.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_FLOOR
from decimal import ROUND_HALF_EVEN
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Self
from uuid import uuid4

from canon_alpaca.data import STAMP_AHEAD_MOST_NS
from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import ContingencyType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LimitOrder
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
from . import sale_table
from . import session_window
from . import symbol_hold
from .custody import OURS
from .custody import PROTECT
from .custody import PROTECT_PREFIX
from .custody import lineage
from .ma_cross import refuse_unknown_keywords
from .mover import FLOOR_WAIT_SECS
from .mover import OVERNIGHT_HISTORY_DELAY
from .mover import OVERNIGHT_HISTORY_FEED
from .mover import SECOND_NS
from .mover import chase_floor
from .mover import offsets
from .price_step import exit_price
from .price_step import up_on_step
from .start_level import LABEL
from .start_level import NEEDS_REASON
from .start_level import NEW
from .start_level import SAME
from .start_level import Entry
from .start_level import Key
from .start_level import LevelBook
from .start_level import book_path
from .start_level import bought_since
from .start_level import holding_verdict
from .start_level import overnight_answer
from .start_level import start_level

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import OrderCancelRejected
    from nautilus_trader.model import OrderFilled


STEP_TIMER = "owner-protection"
# How many stamps V1 refused are kept per instrument; when full, the oldest is dropped first.
REFUSED_KEPT = 64
WAITING = "unprotected: waiting"
STEP_NS = 1_000_000_000  # The step timer's interval (on_start), the floor between step writes.
OVERNIGHT_RETRY_NS = 60 * STEP_NS  # Review F7: an unread eligibility is read again after a minute.
# A refused sale is sent again at a growing wait: the chase interval, doubled for each refusal in a
# row, held at this cap, and started again by a leg the venue accepts or fills: 5, 10, 20, 40, then
# 60 s. The form is NautilusTrader's ExponentialBackoff (nt@d53849b216e2
# crates/network/src/backoff.rs:95-208: an initial delay, a factor, a maximum, a reset), which is
# Rust and not reachable from a strategy, so this is glue in its form; no jitter, one client per
# account. UNCALIBRATED: the cap is the bound of about sixty refused orders an hour (U-c2 ruling
# 15), which this pace keeps: about 63 in an hour of standing refusal.
REFUSAL_RETRY_MOST_SECS = 60
# After a cancel of our leg that we did not ask, no leg of that holding leaves for this long. It is
# its own constant: that minute is the owner's hand, not a refusal by the venue. UNCALIBRATED.
OUTSIDE_CANCEL_WAIT_SECS = 60
# Replaced by the per-symbol control unit when that control exists.
HOLD_ONE_SYMBOL = "hold this symbol: stop the node"
SALE_FAULT_ROWS = frozenset({"FZ", "FZW", "FZP"})
# Rows in which the owner's hold asks nothing more of the table: stopped already, ended, or
# frozen by a fault of ours with nothing open (which sends nothing and must stay frozen).
HOLD_SETTLED_ROWS = frozenset({"FZ", "X", "T0", "TW", "PCT"})
SALE_STOPPED_ROWS = frozenset({"T0", "TW", "PCT"})
SALE_NOTHING_OPEN_ROWS = frozenset({"G", "GC", "RW", "FZ", "FC", "T0", "X"})
SALE_COVER_ROWS = frozenset({"FC", "FCW", "FCP"})
SALE_CANCEL_ROWS = frozenset({"PCR", "PCO", "PCX", "FZP", "FCP", "PCT"})
# What answers a cancel of ours: its acknowledgement, its refusal, or the leg's own end.
SALE_CANCEL_ANSWERS = frozenset({"oCXL", "oCRJ", "oEXP", "oFF", "oREJ", "oDEN"})
# UNCALIBRATED (U-c2 ruling 16). paper_node.exec_engine_config deliberately retains
# unresolved submits: Alpaca's timeout instruction there says never resend one.
# The venue may hold it; a second sell beside it could sell more than is held.
FLIGHT_SECS = 60
ROSTER_POLL_SECS = 30
"""Review F1: how often the account's positions are read for a holding outside the roster. With a read
that answers in its bound, a new holding is named and the run's end asked for within this and one step
of the venue listing it; the stop and the successor's arming after it are not bounded here."""
ROSTER_ENDED = "new holding outside the roster"
"""The run's end by name when a whole-share long outside the roster is found (review F1)."""

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


_GAP_OMITTED = object()


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
        symbol_holds_dir: Path | str | None = None,
        symbol_holds: frozenset[str] | None = None,
        feed: str = "sip",
        extended_hours: bool = True,
        account_label: str | None = None,
        level_book_path: Path | str | None = None,
        run: str = "",
        session_part: str = "regular",
        trading: Any = None,
        breach_count: int = 1,
        confirm_gap_ms: Any = _GAP_OMITTED,
        roster: Sequence[InstrumentId] | None = None,
        **_kwargs: Any,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        self.levels = dict(levels or {})
        self.symbol_holds_dir = None if symbol_holds_dir is None else Path(symbol_holds_dir)
        self.symbol_holds = None if symbol_holds is None else frozenset(symbol_holds)
        self.feed = feed
        self.row = row
        self.extended_hours = extended_hours
        self.account_label = account_label
        self.level_book_path = level_book_path
        self.run = run
        self.session_part = session_part
        self.trading = trading
        self.breach_count = breach_count
        self._gap_given = confirm_gap_ms is not _GAP_OMITTED
        self.confirm_gap_ms = confirm_gap_ms if self._gap_given else None
        # Review F1: the instruments the node loaded at its start; None reads no account.
        self.roster = None if roster is None else tuple(roster)
        self.validate_protection()

    def validate_protection(self) -> None:
        if type(self.breach_count) is not int or self.breach_count < 1:
            raise ValueError("breach_count must be a whole number of at least one")
        if self._gap_given and (type(self.confirm_gap_ms) is not int or self.confirm_gap_ms < 0):
            raise ValueError("confirm_gap_ms must be a nonnegative whole number")
        if self.breach_count == 1 and self._gap_given:
            raise ValueError("confirm_gap_ms is refused at breach_count one")
        if self.breach_count > 1 and not self._gap_given:
            raise ValueError("confirm_gap_ms is required at breach_count above one")
        if self.breach_count > 2 and (
            self.level_book_path is not None or self.account_label is not None
        ):
            raise ValueError("breach_count above two cannot be represented by the level book")
        if self.account_label is not None and (
            not isinstance(self.account_label, str) or not self.account_label.strip()
        ):
            raise ValueError("account_label must be nonempty text, never an account number")
        if self.level_book_path is not None and self.account_label is None:
            raise ValueError("level_book_path requires an account_label")
        if (
            self.level_book_path is None
            and self.account_label is not None
            and not LABEL.fullmatch(self.account_label)
        ):
            # Review F5: the default book is one file per label, so the label names a file.
            raise ValueError("account_label must name the default level book file: [A-Za-z0-9._-]")
        if self.session_part not in ("regular", "pre-market", "after-hours", "overnight"):
            raise ValueError("session_part must name regular, pre-market, after-hours or overnight")
        if not isinstance(self.run, str):
            raise TypeError("run must be text")
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
        if self.roster is not None and not all(
            isinstance(instrument_id, InstrumentId) for instrument_id in self.roster
        ):
            raise TypeError("roster must name InstrumentIds")


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


def refusal_wait_secs(refusals: int) -> int:
    """The wait before a sale refused `refusals` times in a row is sent again: the chase interval,
    doubled for each refusal after the first, held at `REFUSAL_RETRY_MOST_SECS`; never under a
    second, so a chase interval of zero cannot make it zero."""
    first = max(1, protect.PLANNER.chase_secs)
    return min(REFUSAL_RETRY_MOST_SECS, first * 2 ** max(0, min(refusals, 16) - 1))


def _remaining(order: Any) -> Decimal | None:
    """Native quantity minus filled quantity, checked against the native leaves quantity."""
    total = _quantity(getattr(order, "quantity", None))
    filled = _quantity(getattr(order, "filled_qty", None))
    left = _quantity(getattr(order, "leaves_qty", None))
    if total is None or filled is None or left is None or not 0 <= filled <= total:
        return None
    return left if left == total - filled else None


def _reported(entry: Entry) -> tuple[Any, ...]:
    """What the node report shows of a holding; a change in any of these is written at once."""
    return (
        entry.last_level,
        entry.quantity,
        entry.state,
        entry.count,
        entry.reason,
        entry.distance,
        entry.limit_offset,
    )


def _own_level(slot: Holding, order: Any) -> Decimal | None:
    """This run's level for a holding: its slot's level, raised by our sell's trigger."""
    if slot.level is None:
        return None
    level = slot.level.as_decimal()
    if order is not None:
        trigger = getattr(order, "trigger_price", None)
        if trigger is not None:
            level = max(level, trigger.as_decimal())
        else:
            for event in order.events():
                trigger = getattr(event, "trigger_price", None)
                if trigger is not None:
                    level = max(level, trigger.as_decimal())
    return level


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
    end_recorded: bool = False
    resends: int = 0
    cancel_ns: int | None = None
    growth_told: bool = False

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
    plan_chase_cap: Decimal | None = None
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
    # The carried book entry's protection start (U-e A); None for a holding first seen this run,
    # whose start is its ts_opened in this run.
    protection_start: int | None = None
    history_ready: bool = False
    history_error: str | None = None
    history_error_said: bool = False
    cold: bool = False
    cold_seen: int | None = None
    seed_error: str | None = None
    first_quote: int | None = None
    last_quote: int | None = None
    # The lead's ruling, 2026-11-13 19:23: the node time this holding's own seed read was requested,
    # and the venue stamp from which its maximum is known current: what that read returned, or the
    # newer maximum held when it returned. A read requested before the holding opened sets neither.
    seed_requested_at: int | None = None
    current_from: int | None = None
    sale_state: sale_table.State | None = None
    counted_quote: QuoteTick | None = None
    usable_quote: QuoteTick | None = None
    opening_quantity: Decimal = Decimal(0)
    # Observed quantities identify changes, net of all our fills; never cached predicates.
    observed_quantities: tuple[Decimal, Decimal] | None = None
    # Run-local holding data: recounting and a new first leg do not reset a refusal.
    refusal_told: bool = False
    refusal_last_ns: int | None = None
    external_cancel_ns: int | None = None
    level_instrument_unread: bool = False
    refusal_reason: str | None = None
    refusal_first: str | None = None
    refusals: int = 0
    # Refusals in a row for the PACE alone: started again only by a fill or by a leg that ends
    # without a refusal. An acceptance alone does not: a venue that accepts and then rejects every
    # leg would otherwise hold the wait at its first step.
    refusal_streak: int = 0
    flight_sent_ns: int | None = None
    flight_told: bool = False
    flight_answered: bool = False
    flight_command: str = "submit"
    flight_answer_ns: int | None = None
    # Set when the owner's word held this holding after its sell was placed: its release re-places
    # the sell at the next step while the bid is above the level, with no new observation.
    rearm_at_step: bool = False
    # The owner's hold at a count above one (`_hold_counted`): applied to this holding, and its
    # waiting release already said.
    hold_applied: bool = False
    hold_release_said: bool = False
    # The table's `faulted`: a fault of ours was recorded for this holding in this run.
    sale_faulted: bool = False


STREAM_LOGGER = "alpaca.data.live.websocket"
STREAM_ERROR = "error during websocket communication"


class _StreamErrors(logging.Handler):
    """Test b (the lead, 2026-11-13 19:01:48), an upstream named limit: a stream message alpaca-py
    cannot parse ends its received batch, and the rest of that batch is lost, logged as STREAM_ERROR
    by STREAM_LOGGER with the socket kept up (alpaca/data/live/websocket.py:284-297, :568-575 at
    c42add7c). This counts those lines for the node; there is no patch to the client."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(STREAM_ERROR):
            self.lines.append(message)


def _known_current(slot: Holding, tick: QuoteTick, streamed: bool, received: int) -> bool:
    """The lead's ruling, 2026-11-13 19:23: a holding with no level plans only from a quote known
    current for it: (i) stamped at or after the holding opened, or (ii) at or after the stamp its own
    seed read, requested at or after the holding opened, made current (`current_from`); or (iii)
    received at or after the node knew of the holding, both on the host's clock (the stream's form
    of (ii): `ts_opened` is the venue's fill time for a streamed fill but the host's for a position
    reconciled at start, so (i) alone would hold a self-arm for as long as the host runs ahead).
    (iii) holds for a stream quote only (`streamed`): a seed answer's receipt is the response's, so it
    is judged by when its read was requested, under (ii). `received` is when the maximum's stamp
    first reached the node, so a re-delivery of a quote held before the opening is not new."""
    return (
        tick.ts_event >= slot.opened_at
        or (streamed and received >= slot.initialized_at)
        or (slot.current_from is not None and tick.ts_event >= slot.current_from)
    )


class OwnerProtector(Strategy):
    """No entries. Only an unclaimed EXTERNAL long may receive this strategy's protective sell."""

    def __init__(self, config: OwnerProtectorConfig) -> None:
        config.validate_protection()
        super().__init__(config)
        self.settings = config
        self.levels = dict(config.levels)
        self.slots: dict[InstrumentId, Holding] = {}
        self.frozen: dict[InstrumentId, str] = {}
        # The owner's word for one symbol (symbol_hold): held now, how many holds each symbol had
        # this run, and the kind of the last read's failure while the last holds read stand.
        self.held: set[InstrumentId] = set()
        self.hold_counts: dict[InstrumentId, int] = {}
        self.holds_unread: str | None = None
        self.holds_made = False  # A folder read once: from then on its absence is a failed read.
        self.sent_ids: set[ClientOrderId] = set()
        self.cancel_requested: set[ClientOrderId] = set()
        # An order event our own submit, cancel or modify publishes reaches its handler while the
        # strategy is mutably borrowed (nt@d53849b216e2 crates/trading/src/python/strategy.rs:
        # 2016-2037), where cache, clock and log raise. Such a handler only stores what the event
        # carries, here, and the next quote or step reads the engine (main's form, mover.py:230-237).
        self.released_at: dict[ClientOrderId, int] = {}
        self.deferred_lines: list[str] = []
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
        # F8: replacing our ended sell needs the latest usable quote even after bad arrivals
        # have displaced it from both the venue maximum and the bounded upstream cache.
        self.replacement_quotes: dict[InstrumentId, QuoteTick] = {}
        # F8's review, 2 and 3: a lost cover with no usable quote is ALERTed once per loss.
        self.replace_alerted: set[InstrumentId] = set()
        self.gain_lock_events: list[dict[str, Any]] = []
        self.gain_lock_sales: list[dict[str, Any]] = []
        self.previous_levels: dict[Key, Entry] = {}
        self.level_book_path: Path | None = None
        self.started_once = False
        self.streamed_max: dict[InstrumentId, bool] = {}
        # When the maximum's stamp first reached the node (host clock): clause (iii) needs a quote
        # NEW to the node, its stamp above the maximum held when it arrived. A later arrival with
        # an equal stamp keeps the first receipt (the lead's ruling on land-composed's F2, 20:45).
        self.max_received_at: dict[InstrumentId, int] = {}
        self.redelivery_named: set[tuple[InstrumentId, int]] = set()
        self.receipt_plans: dict[InstrumentId, int] = {}
        self.alerted: set[InstrumentId] = set()
        self.stream_errors = _StreamErrors()
        self.stream_errors_said = 0
        self.run = ""
        self.overnight_checks: dict[InstrumentId, str | None] = {}
        # P10: what the book on disk holds, per holding, as this run last wrote or read it.
        self.book_entries: dict[Key, Entry] = {}
        # Item 4: a write failure is named once per kind (exception type and text).
        self.write_failures: set[tuple[str, str]] = set()
        self.last_step_ns: int | None = None
        # P11: supplied levels and book entries that armed a holding this run.
        self.applied: set[InstrumentId] = set()
        # The clock when a supplied level was first kept with no selectable holding. A holding
        # found later binds to it only when no buy of the name filled after it (U-e B: read from
        # the venue's orders, `bought_since`); else it is a new holding and self-arms (P1-2).
        self.kept_at: dict[InstrumentId, int] = {}
        # U-e B: book entries whose holding the venue showed is not the one held now, by reason.
        self.not_carried: dict[Key, str] = {}
        # Keys this run has written: its own entries, never a previous run's undecided one.
        self.written_keys: set[Key] = set()
        self.kept_passed: set[InstrumentId] = set()
        self.carried: set[Key] = set()
        # Review F2: keys whose book entry this run may replace, lower level or not: the venue
        # showed a new holding, the entry had no level, or this run saw the holding end.
        self.replaceable: set[Key] = set()
        # Review F3: entries retired when this run saw their holding end, written with no level.
        self.retired: dict[Key, Entry] = {}
        # Review F7: an unread eligibility, by the clock of its last attempt, retried once a minute.
        self.overnight_unread: dict[InstrumentId, tuple[int, str]] = {}
        self.history_bars: dict[InstrumentId, dict[int, Bar]] = {}
        self.request_failures: dict[str, str] = {}
        # Each seed request in flight, per instrument, with the holding that sent it and when.
        # The pin hands a reply's quotes to the hook before its metadata and callback
        # (response.rs:153-167), so a reply cannot be matched to its request when it arrives
        # (Codex's P2-a on 62941f39, the lead's rule of 19:49 as meant, 20:31).
        self.seeds_in_flight: dict[InstrumentId, list[tuple[Holding, int]]] = {}
        self.stamp_counts: dict[InstrumentId, list[int]] = {}
        self.fallbacks: dict[InstrumentId, int] = {}
        self.flat_fallbacks: dict[InstrumentId, int] = {}
        self.quote_refusals: dict[InstrumentId, str] = {}
        self.stopped_sales: dict[InstrumentId, Holding] = {}
        self.readoption_named: set[InstrumentId] = set()
        self._sale_reading: set[InstrumentId] = set()
        self._sale_events: dict[InstrumentId, list[Any]] = {}
        self._sale_orders: dict[ClientOrderId, Any] = {}
        # Review F1: the step that last read the account for the roster, the run's end by name once
        # asked for, and the holdings outside the roster already named, by symbol and kind.
        self.roster_read_ns: int | None = None
        self.ended_for: str | None = None
        self.roster_named: set[tuple[str, str]] = set()

    @property
    def cache(self) -> Any:
        # py_submit_order holds &mut self while publishing Initialized. py_cache returns
        # an independent shared handle (python/strategy.rs:1750-1753, 2018-2036;
        # common/src/python/cache.rs:80-86, 733-740), never a snapshot of facts.
        handle = getattr(self, "_counted_cache", None)
        return super().cache if handle is None else handle

    @property
    def strategy_id(self) -> StrategyId:
        identity = getattr(self, "_counted_identity", None)
        return super().strategy_id if identity is None else identity

    @property
    def log(self) -> Any:
        logger = getattr(self, "_counted_logger", None)
        return super().log if logger is None else logger

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
            self._receive_quote(tick, "seed", manage=False)

    def _receive_quote(self, tick: QuoteTick, source: str, *, manage: bool = True) -> None:
        """One intake for stream and seed: note before planning; only a stream can count.

        Keep _note's guard unchanged. Equal stamps replace the stored snapshot, but its False
        result still prevents counting. Refused re-deliveries never replace that snapshot.
        """
        instrument_id = tick.instrument_id
        if self.stopping or instrument_id in self.stopped_instruments:
            return
        counts = self.stamp_counts.setdefault(instrument_id, [0, 0, 0])
        refused = tick.ts_event in self.refused_stamps.get(instrument_id, ())
        previous = self.venue_quotes.get(instrument_id)
        if refused:
            counts[2] += 1
        elif not _sane(tick):
            counts[0] += 1
            self.quote_refusals[instrument_id] = _ahead(tick)
        elif previous is not None and tick.ts_event <= previous.ts_event:
            counts[1] += 1
        advanced = self._note(tick)
        if not refused and _sane(tick):
            self.quote_refusals.pop(instrument_id, None)
        if (
            not refused
            and _sane(tick)
            and previous is not None
            and tick.ts_event == previous.ts_event
        ):
            self.venue_quotes[instrument_id] = tick
        usable = self.replacement_quotes.get(instrument_id)
        if (
            not refused
            and _sane(tick)
            and tick.bid_price.as_decimal() > 0
            and tick.ask_price.as_decimal() >= tick.bid_price.as_decimal()
            and tick.bid_size.as_decimal() > 0
            and tick.ask_size.as_decimal() > 0
            and (usable is None or tick.ts_event >= usable.ts_event)
        ):
            self.replacement_quotes[instrument_id] = tick
        if self.venue_quotes.get(instrument_id) is tick and (
            previous is None or tick.ts_event > previous.ts_event
        ):
            self.streamed_max[instrument_id] = source == "stream"  # (iii) is the stream's alone.
            self.max_received_at[instrument_id] = tick.ts_init
        slot = self.slots.get(instrument_id)
        if slot is not None:
            if slot.first_quote is None:
                slot.first_quote = tick.ts_event
            slot.last_quote = tick.ts_event
        if not manage or slot is None:
            return
        if source == "seed":
            if refused or not _sane(tick):
                slot.seed_error = "seed refused: " + _ahead(tick)
                return
            newest = self.venue_quotes.get(instrument_id)
            if (
                slot.seed_requested_at is not None
                and slot.seed_requested_at >= slot.opened_at
                and newest is not None
                and all(
                    sender is slot and sent >= slot.opened_at
                    for sender, sent in self.seeds_in_flight.get(instrument_id, ())
                )
            ):
                # (ii): the venue's latest, read after the holding opened, whatever its stamp;
                # the maximum held is at least as new, so it is current too. Only when every
                # seed in flight was sent by this holding since it opened: a reply to an earlier
                # holding's request, or one sent before this opening, certifies nothing.
                slot.current_from = newest.ts_event
            if newest == tick:
                age = (self.clock.timestamp_ns() - min(tick.ts_event, tick.ts_init)) / SECOND_NS
                self._say(f"{instrument_id}: seed age {age:.0f} s; never an observation")
                slot.seed_error = None
                self._plan_from_maximum(instrument_id, slot)
            return
        self._manage(instrument_id, tick, advanced=advanced)

    def _current_for(self, instrument_id: InstrumentId, slot: Holding, tick: QuoteTick) -> bool:
        """The known-current rule for this instrument's maximum, naming once per opening a
        re-delivery that is all that stands between it and clause (iii)."""
        streamed = self.streamed_max.get(instrument_id, False)
        received = self.max_received_at.get(instrument_id, 0)
        if _known_current(slot, tick, streamed, received):
            return True
        key = (instrument_id, slot.opened_at)
        if streamed and tick.ts_init >= slot.initialized_at and key not in self.redelivery_named:
            self.redelivery_named.add(key)
            self._say(
                f"{instrument_id}: a quote stamped {tick.ts_event} re-delivered at {tick.ts_init}; "
                f"the node held that stamp since {received}, before it knew of the holding "
                f"({slot.initialized_at}), so it plans nothing; waiting for a new quote or the seed"
            )
        return False

    def _plan_from_maximum(self, instrument_id: InstrumentId, slot: Holding) -> None:
        tick = self.venue_quotes.get(instrument_id)
        if (slot.distance is None or slot.cold) and tick is not None and slot.sale is None:
            self._plan_holding(instrument_id, slot, tick)

    def on_historical_quotes(self, quotes: Sequence[QuoteTick]) -> None:
        for tick in quotes:
            self._receive_quote(tick, "seed")

    def on_historical_bars(self, bars: Sequence[Bar]) -> None:
        # Engine responses also enter Cache.bars; merge by event stamp when planning.
        for bar in bars:
            self.history_bars.setdefault(bar.bar_type.instrument_id, {})[bar.ts_event] = bar

    def on_data_response(self, response: Any) -> None:
        # NT's callback receives str, not DataResponse (response.rs:300). Metadata arrives
        # here first (response.rs:166-167,331-340), including failures with no historical payload.
        if response.failure_reason is not None:
            self.request_failures[response.correlation_id] = response.failure_reason

    def _request_seed(self, instrument_id: InstrumentId) -> None:
        slot = self.slots[instrument_id]
        slot.seed_requested_at = self.clock.timestamp_ns()
        now = datetime.fromtimestamp(self.clock.timestamp_ns() / SECOND_NS, UTC)

        def bars_done(request_id: str) -> None:
            failure = self.request_failures.pop(request_id, None)
            if self.slots.get(instrument_id) is not slot or self.stopping:
                return
            slot.history_error = failure
            slot.history_ready = failure is None
            self._plan_from_maximum(instrument_id, slot)

        token = (slot, slot.seed_requested_at)

        def landed() -> None:
            # By identity: two holdings' tokens may compare equal field by field.
            flying = self.seeds_in_flight.get(instrument_id, [])
            flying[:] = [sent for sent in flying if sent is not token]

        def seed_done(request_id: str) -> None:
            landed()
            failure = self.request_failures.pop(request_id, None)
            if self.slots.get(instrument_id) is not slot or self.stopping:
                return
            if failure is not None:
                slot.seed_error = f"seed failed: {failure}; wait"
            elif instrument_id not in self.venue_quotes:
                slot.seed_error = "seed empty: no two-sided quote; wait"

        end = now - OVERNIGHT_HISTORY_DELAY if self.settings.feed == OVERNIGHT_HISTORY_FEED else now
        start = protect.session_start(now)
        if start >= end:
            # 04:00-04:15 New York on boats: no window ends 15 minutes ago inside the session day,
            # so nothing is asked and the cold rule stands until a full ATR's worth of live bars
            # exists (code-review-high/7, -high-2/2).
            self._say(
                f"{instrument_id}: no session day's bars to ask before {start + OVERNIGHT_HISTORY_DELAY:%H:%M} "
                "UTC on boats; the cold rule stands until a full ATR's worth of bars exists"
            )
        else:
            try:
                self.request_bars(
                    BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"),
                    start=start,
                    end=end,
                    params={"feed": self.settings.feed},
                    callback=bars_done,
                )
            except (ValueError, RuntimeError) as exc:
                slot.history_error = str(exc)
        self.seeds_in_flight.setdefault(instrument_id, []).append(token)
        try:
            self.request_quotes(
                instrument_id,
                limit=1,
                # L5's frozen shape (18:56; 44a3e0c0): the latest quote on the stream's own feed,
                # never from or into the catalog. Sent after `_subscribe_holding`, since the adapter
                # refuses a seed by name ("no stream feed yet") until its stream is built; that
                # refusal reaches `seed_done` like any failed seed, named, and the stream is awaited.
                params={
                    "feed": self.settings.feed,
                    "latest": True,
                    "skip_catalog_data": True,
                    "update_catalog": False,
                },
                callback=seed_done,
            )
        except (ValueError, RuntimeError) as exc:
            landed()
            slot.seed_error = f"seed failed: {exc}; wait"

    def set_external_order_instrument_ids(self, instrument_ids: Sequence[InstrumentId]) -> None:
        if instrument_ids:
            raise ValueError("OwnerProtector must never claim external orders or their instrument")

    def _say(self, text: str) -> None:
        # An ALERT must reach a person, so it is logged as an error, never at INFO (the lead's
        # addendum on land-composed's F6, 2026-11-13 20:46).
        if "ALERT:" in text:
            self.log.error(text)
        else:
            self.log.info(text)

    def _freeze(self, instrument_id: InstrumentId, reason: str) -> None:
        if self.settings.breach_count > 1:
            slot = self.slots.get(instrument_id) or self.stopped_sales.get(instrument_id)
            if (
                slot is None
                or slot.sale_state is None
                or slot.sale_state.row not in SALE_FAULT_ROWS
            ):
                self.frozen[instrument_id] = reason
            if instrument_id not in self._sale_reading:
                self._sale_step(instrument_id, ["FRZ"], reason=reason)
            return
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
        if self.settings.breach_count > 1 and instrument_id in self.stopped_sales:
            if instrument_id not in self.readoption_named:
                self.readoption_named.add(instrument_id)
                self._say(
                    f"{instrument_id}: previous sale still open; stopped instrument stays stopped until the next start"
                )
            return
        if (
            self.stopping
            or instrument_id in self.stopped_instruments
            or instrument_id in self.slots
        ):
            return
        _check_level(level)
        position = self._external_long(instrument_id)
        if position is None:
            # P11: kept, never dropped here; discovery applies it when the holding appears.
            if self.levels.get(instrument_id) != level or instrument_id not in self.kept_at:
                self._say(
                    f"{instrument_id}: level kept: no selectable EXTERNAL holding yet; "
                    f"{level} arms that holding when it appears"
                )
            self.levels[instrument_id] = level
            self.kept_at.setdefault(instrument_id, self.clock.timestamp_ns())
            return
        if not self._overnight_allowed(instrument_id):
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
        self.applied.add(instrument_id)
        self.kept_at.pop(instrument_id, None)
        self.slots[instrument_id] = self._new_holding(instrument_id, position, level)
        self._subscribe_holding(instrument_id)
        self._request_seed(instrument_id)
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
            if self.settings.roster is not None and instrument_id not in self.settings.roster:
                continue  # Review F1: named by the roster's read; this run never arms it.
            try:
                self._discover_holding(instrument_id)
            except Exception as exc:  # noqa: BLE001 - one holding never costs another discovery.
                # Codex's P1-b on 62941f39: at the start, a raise here failed on_start, so no other
                # holding was found and the timer never ran (strategy.rs:1122-1126 at the pin).
                # A slot made before the raise is kept, so the step manages it and no second sell
                # is ever made for it; with none, the next step's discovery tries again.
                self._say(
                    f"{instrument_id}: ALERT: unprotected: discovery failed: "
                    f"{type(exc).__name__}: {exc}; tried again at the next step"
                )

    def _discover_holding(self, instrument_id: InstrumentId) -> None:
        """One holding's discovery: the kept level's venue check, eligibility, then its slot."""
        position = self._external_long(instrument_id)
        if position is None:
            return
        kept = self.kept_at.get(instrument_id)
        if (
            kept is not None
            and instrument_id in self.levels
            and instrument_id not in self.kept_passed
        ):
            # U-e B: decided once, by the venue's fills, not by ts_opened (the reconciliation
            # clock at the pin; see the module header).
            self.kept_passed.add(instrument_id)
            try:
                bought = bought_since(self.settings.trading, instrument_id.symbol.value, kept)
                reason = (
                    None
                    if bought == 0
                    else f"a buy of {bought} filled after it was kept, so it is a new holding"
                )
            except Exception as exc:  # noqa: BLE001 - an unread venue never binds a level.
                reason = f"venue orders unread ({type(exc).__name__}: {exc})"
            if reason is None:
                # P11: held all along and reconciled late; the kept level arms it now.
                self.adopt_holding(instrument_id, self.levels[instrument_id])
                return
            self._say(
                f"{instrument_id}: kept level {self.levels[instrument_id]} not applied: "
                f"{reason}; self-arming"
            )
        if not self._overnight_allowed(instrument_id):
            return
        if self.refused.get(instrument_id) == (position.ts_opened, position.ts_init):
            # The holding whose supplied level was refused is not re-armed from the row.
            self._name_refused(instrument_id, "its supplied level was refused")
            return
        self.refused.pop(instrument_id, None)
        self.slots[instrument_id] = self._new_holding(instrument_id, position)
        self._subscribe_holding(instrument_id)
        self._request_seed(instrument_id)
        self._manage(instrument_id)

    def on_start(self) -> None:
        if self.started_once:
            # L8's P2 (the lead, 2026-11-13 19:05): the run, sells and holdings belong to one start.
            raise RuntimeError(
                "OwnerProtector starts once: a second start of the same object is refused; "
                "a restart is a new protector"
            )
        self.started_once = True
        self.settings.validate_protection()
        logging.getLogger(STREAM_LOGGER).addHandler(self.stream_errors)
        self.overnight_checks.clear()
        self.overnight_unread.clear()
        self._bound_reader()
        self.run = self.settings.run or f"{self.strategy_id}:{self.clock.timestamp_ns()}"
        if self.settings.account_label is not None:
            self.level_book_path = (
                book_path(self.settings.account_label)
                if self.settings.level_book_path is None
                else Path(self.settings.level_book_path)
            )
            self.previous_levels = LevelBook.read(self.level_book_path, report=self._say)
            self.book_entries = dict(self.previous_levels)
            # 3d's cell 0c (the lead, 2026-11-13 19:12): the book's state is said at every start.
            self._say(f"level book enabled for account {self.settings.account_label}")
        else:
            self._say("level book disabled: no account label supplied; no level is carried")
        if self.settings.symbol_holds_dir is None:
            self._say("no symbol-holds folder supplied: no per-symbol hold is read this run")
        for name in sorted(self.settings.symbol_holds or ()):
            # The node's start read under the locks stands if this strategy's own read fails.
            self.holds_made = True
            self._hold_begins(InstrumentId.from_str(f"{name}.ALPACA"))
        self._read_symbol_holds()
        for instrument_id, level in list(self.levels.items()):
            self.adopt_holding(instrument_id, level)
        self._discover_holdings()
        self._save_levels()
        self.clock.set_timer(STEP_TIMER, timedelta(seconds=1), callback=self._on_step)

    def _bound_reader(self) -> None:
        """Review F6: an alpaca-py RESTClient reader gets the adapter's (connect, read) timeout
        (canon_alpaca.providers.with_http_timeout, ADR 0039); a stand-in is left as it is."""
        from alpaca.common.rest import RESTClient
        from canon_alpaca.providers import with_http_timeout

        if isinstance(self.settings.trading, RESTClient):
            with_http_timeout(self.settings.trading)

    def _overnight_allowed(self, instrument_id: InstrumentId) -> bool:
        if self.settings.session_part != "overnight":
            return True
        reason = self.overnight_checks.get(instrument_id)
        if instrument_id not in self.overnight_checks:
            now = self.clock.timestamp_ns()
            unread = self.overnight_unread.get(instrument_id)
            if unread is not None and now - unread[0] < OVERNIGHT_RETRY_NS:
                reason = unread[1]
            else:
                reason, answered = overnight_answer(
                    self.settings.trading, instrument_id.symbol.value
                )
                if answered:
                    self.overnight_checks[instrument_id] = reason
                    self.overnight_unread.pop(instrument_id, None)
                else:
                    reason = f"{reason}; read again at most once a minute"
                    self.overnight_unread[instrument_id] = (now, reason)
        if reason is None:
            return True
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")
        return False

    def _previous_entry(
        self, instrument_id: InstrumentId, held: Decimal | None = None
    ) -> tuple[Key, Entry | None]:
        """The previous run's entry for this holding, with U-e B's venue reading decided once.

        An entry with no level carries nothing, so it is replaceable with no read. Otherwise the
        venue decides (start_level.holding_verdict, with the quantity held now): the same holding
        is carried; a new one is replaceable; an unknown one is neither (review F2).
        """
        key: Key = (self.settings.account_label or "", instrument_id)
        previous = self.previous_levels.get(key)
        if (
            previous is not None
            and key not in self.carried
            and key not in self.not_carried
            and key not in self.replaceable
        ):
            if previous.last_level is None:
                self.replaceable.add(key)
            else:
                kind, reason = holding_verdict(
                    self.settings.trading, instrument_id.symbol.value, previous, held
                )
                if kind == SAME:
                    self.carried.add(key)
                else:
                    assert reason is not None
                    self.not_carried[key] = reason
                    if kind == NEW:
                        self.replaceable.add(key)
        return key, previous

    def _name_start(
        self,
        instrument_id: InstrumentId,
        slot: Holding,
        previous: Entry | None,
        carried: Entry | None,
        chosen: Decimal,
        distance: Decimal,
        supplied: Decimal | None,
        limit: Decimal,
    ) -> None:
        """Say how this start's level was fixed, one line, and for a carried long one line for
        the gap it could not see (the lead's ruling, 2026-11-13 17:43, point 6)."""
        key: Key = (self.settings.account_label or "", instrument_id)
        if carried is not None:
            assert carried.last_level is not None
            slot.protection_start = carried.protection_start_ns
            kept = (
                f"distance {distance}"
                if carried.distance is not None
                else (
                    f"no carried distance; covered by the cold rule at {chosen} with distance "
                    f"{distance}, to be planned again when bars exist"
                    if slot.cold
                    else "no distance (written before distances were kept); the row's distance "
                    f"{distance} used for later raises only"
                )
            )
            offset = (
                f", limit offset {carried.limit_offset}"
                if carried.limit_offset is not None
                else f"; no limit offset (written before offsets were kept): today's rule gives "
                f"{limit}"
            )
            both = ""
            if supplied is not None and supplied != carried.last_level:
                # Ruling (b), 2026-11-13 17:56: the greater arms, and the other is named.
                both = (
                    f"; the supplied level {supplied} armed, over the carried {carried.last_level}"
                    if chosen == supplied
                    else f"; the carried level armed, over the supplied {supplied}"
                )
            self._say(
                f"{instrument_id}: carried level {carried.last_level} and {kept} from run "
                f"{carried.run}{offset}{both}"
                + ("; cold rule: protect.distance_for fixed fallback" if slot.cold else "")
            )
            now = self.clock.timestamp_ns()
            since, until = (
                datetime.fromtimestamp(at / 1_000_000_000, UTC).isoformat()
                for at in (carried.written_ns, now)
            )
            self._say(
                f"{instrument_id}: gap {since}-{until} not read: a rise in the gap's bids is not "
                "in this level"
            )
            positions = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
            if len(positions) == 1 and positions[0].signed_qty > carried.quantity:
                self._say(
                    f"{instrument_id}: added shares {positions[0].signed_qty - carried.quantity} "
                    "protected from the carried level"
                )
            return
        if self.level_book_path is None:
            why = "level book disabled"
        elif previous is None:
            why = "no previous entry"
        elif key in self.not_carried:
            why = (
                f"previous level not carried: {self.not_carried[key]}; it was "
                f"{previous.last_level} from run {previous.run}"
            )
            if key not in self.replaceable:
                why += ", and the book keeps it as written while the holding is unknown"
        else:
            why = (
                f"previous entry from run {previous.run} has no level "
                f"({previous.state}: {previous.reason})"
            )
        how = (
            f"self-armed at {chosen}, the bid less the row distance {distance}, first "
            "observation guarded"
            if supplied is None
            else f"the supplied level {chosen} arms it"
        )
        self._say(
            f"{instrument_id}: no carried level ({why}); {how}"
            + ("; cold rule: protect.distance_for fixed fallback" if slot.cold else "")
        )

    def _say_deferred(self) -> None:
        lines, self.deferred_lines = self.deferred_lines, []
        for line in lines:
            self._say(line)

    def on_quote(self, quote: QuoteTick) -> None:
        self._say_deferred()
        try:
            self._receive_quote(quote, "stream")
        except Exception as exc:  # noqa: BLE001 - named for its symbol, as a step's failure is.
            # The pin logs it as "Python on_quote failed" with no symbol (data_actor.rs:1036-1038).
            self._say(
                f"{quote.instrument_id}: unprotected: quote failed: {type(exc).__name__}: {exc}"
            )
            return
        if quote.instrument_id in self.slots:
            self._save_levels()

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        """Stores only (`released_at`'s note): our own IDs are `sent_ids`; the line is said at the
        next quote or step."""
        if self.settings.breach_count > 1:
            return  # The pin calls on_order_event after the specific hook, exactly once.
        if event.client_order_id not in self.sent_ids:
            return
        self.cancel_requested.discard(event.client_order_id)
        self.deferred_lines.append(
            f"{event.instrument_id}: our cancel rejected: {event.reason}; "
            "re-evaluate on the next quote or step"
        )

    def on_order_released(self, event: OrderReleased) -> None:
        """The released trail's sale starts at the next quote or step (U-d), from `_manage`'s
        `_observe_sale`, stamped at this release: a trail already through the bid is released
        inside our own submit, where the engine cannot be read (`released_at`'s note). A sale the
        applied gain lock protected also keeps the spread at its release (measure 1)."""
        if self.settings.breach_count > 1:
            return
        slot = self.slots.get(event.instrument_id)
        if slot is None or event.client_order_id != slot.protect_id:
            return
        self.released_at[event.client_order_id] = event.ts_event
        if slot.gain_applied_floor is not None:
            tick = self.venue_quotes.get(event.instrument_id)
            slot.gain_release_spread = (
                None if tick is None else tick.ask_price.as_decimal() - tick.bid_price.as_decimal()
            )

    def on_order_filled(self, event: OrderFilled) -> None:
        """Measure 1 for a sale the armed lock protected, then the holding re-evaluated at once.

        A fill on any leg, a late one on a canceled leg included (Canceled -> Filled,
        nt@d53849b216e2 crates/model/src/orders/mod.rs:247), changes the shares a working leg
        must cover. The engine updates the position before it publishes the fill
        (crates/execution/src/engine/mod.rs:3261, :3266), so `_manage` here withdraws a working
        leg for size in this event, not at the next quote or step (the lead, 2026-11-13 19:17).
        """
        if self.settings.breach_count > 1:
            return
        slot = self.slots.get(event.instrument_id)
        if slot is None or not (
            event.client_order_id == slot.protect_id
            or (slot.sale is not None and event.client_order_id in slot.sale.order_ids)
        ):
            return
        if slot.gain_armed_at is not None and slot.gain_entry is not None:
            self._gain_lock_sale(event, slot)
        if self.stopping or event.instrument_id in self.stopped_instruments:
            order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
            self._observe_sale(event.instrument_id, slot, order)
            return
        self._manage(event.instrument_id)  # Observes the sale's record first.

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
        self._say_deferred()
        if self.stopping:
            return
        self._read_symbol_holds()  # The owner's word, read at every step before any holding.
        if self.settings.breach_count > 1:
            for instrument_id in list(self.stopped_sales):
                try:
                    self._sale_step(instrument_id, ["NQ"])
                except Exception as exc:  # noqa: BLE001 - preserve the base's per-holding isolation.
                    self._say(
                        f"{instrument_id}: unprotected: stopped sale step failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        lost = len(self.stream_errors.lines)
        if lost > self.stream_errors_said:
            # Said once per new stream error, for each holding: its quotes may be among those lost.
            last = self.stream_errors.lines[-1].splitlines()[0][:200]
            for instrument_id in list(self.slots):
                self._say(
                    f"{instrument_id}: quotes may have been lost: the stream dropped the rest of a "
                    f"batch ({lost} so far): {last}"
                )
            self.stream_errors_said = lost
        for instrument_id in list(self.slots):
            try:
                self._manage(instrument_id)
            except Exception as exc:  # noqa: BLE001 - one holding never costs another its step.
                # At the pin a raise here is logged and swallowed (common/src/timer.rs:206-213) and
                # the timer fires on, so without this every later holding, discovery and the book
                # would be skipped at every step (the lead, 2026-11-13 18:58).
                self._say(f"{instrument_id}: unprotected: step failed: {type(exc).__name__}: {exc}")
                continue
            waiting = self.frozen.get(instrument_id, "")
            if waiting.startswith(WAITING):
                # A protector that cannot act on its quotes says so at every step (the lead's
                # ruling, 2026-11-13, 16:3x).
                self._say(f"{instrument_id}: {waiting}; no sell yet")
        stamp = getattr(event, "ts_event", None)
        self._read_roster(stamp if type(stamp) is int else self.clock.timestamp_ns())
        self._discover_holdings()
        # Item 4: the step's time reaches the book at most once per step, so a monitor sees a
        # killed run as a last_step_ns that stops advancing. A TimeEvent's ts_event is the step.
        at = getattr(event, "ts_event", None)
        self._save_levels(step_ns=at if type(at) is int else self.clock.timestamp_ns())

    def _read_roster(self, now: int) -> None:
        """Review F1: at the first step and every ROSTER_POLL_SECS, name each long the account holds
        outside the roster, and end the run by name for a whole-share one, unless the start's own
        predicate (`session_window.start_account`) would refuse the account read (module header)."""
        roster = self.settings.roster
        if roster is None or self.ended_for is not None:
            return
        if (
            self.roster_read_ns is not None
            and now - self.roster_read_ns < ROSTER_POLL_SECS * STEP_NS
        ):
            return
        self.roster_read_ns = now
        if self.settings.trading is None:
            if ("", "no reader") not in self.roster_named:
                self.roster_named.add(("", "no reader"))
                self._say(
                    "ALERT: no account reader: a holding bought after the start outside the roster "
                    "cannot be named by this run"
                )
            return
        try:
            held, unread = session_window.position_reads(self.settings.trading)
        except Exception as exc:  # noqa: BLE001 - an unread account is named, never taken as flat.
            self._say(
                f"ALERT: the account's positions could not be read ({type(exc).__name__}: {exc}): "
                "a holding bought after the start outside the roster may be unnamed; read again in "
                f"{ROSTER_POLL_SECS} s"
            )
            return
        loaded = {instrument_id.symbol.value for instrument_id in roster}
        # The start's own predicate, called, never copied (the lead's term (a), 2026-11-13 23:49): the
        # successor reads this same account, skips what it skips and refuses what it refuses.
        start = session_window.start_account(held, unread)
        for symbol, why in sorted(start.skipped.items()):
            if symbol not in loaded and (symbol, "skipped") not in self.roster_named:
                self.roster_named.add((symbol, "skipped"))
                self._say(
                    f"{symbol}.ALPACA: ALERT: held outside the roster and unprotected, its quantity "
                    f"unreadable or none ({why}): no exit in this run, and the next start skips it "
                    "too, so it does not end this run"
                )
        for symbol, why in sorted(start.refused.items()):
            if symbol not in loaded and (symbol, "refused") not in self.roster_named:
                self.roster_named.add((symbol, "refused"))
                self._say(
                    f"{symbol}.ALPACA: ALERT: held outside the roster and unprotected: {held[symbol]} "
                    f"held, which the next start refuses ({why}), so this run goes on; it has no exit "
                    "here"
                )
        whole = [
            (symbol, qty)
            for symbol, qty in sorted(start.held.items())
            if symbol not in loaded and qty > 0 and symbol not in start.refused
        ]
        if not whole:
            return
        if start.refused:
            # An end into a start that refuses leaves every holding, the roster's too, with no stop of
            # ours: the run goes on, protects its roster, and names each new holding once. Decided at
            # every read, never latched (term (b)): the first read the start would accept ends it.
            because = "; ".join(start.refused[symbol] for symbol in sorted(start.refused))
            for symbol, qty in whole:
                if (symbol, "blocked") not in self.roster_named:
                    self.roster_named.add((symbol, "blocked"))
                    self._say(
                        f"{symbol}.ALPACA: ALERT: unprotected: {qty} held, bought after this run's "
                        "start outside the roster; this run does not end, since the next start would "
                        f"refuse the account ({because}); it protects its roster to its planned end "
                        "and ends at the first read the start would accept"
                    )
            return
        for symbol, qty in whole:
            self._say(
                f"{symbol}.ALPACA: ALERT: {ROSTER_ENDED}: {qty} held, bought after this run's "
                "start; this run ends now, so the next start reads the account and protects it"
            )
        self.ended_for = f"{ROSTER_ENDED}: " + ", ".join(f"{s} {q}" for s, q in whole)
        self.shutdown_system(self.ended_for)

    def _wait(self, instrument_id: InstrumentId, reason: str) -> None:
        """A holding with no sell of ours that waits on its quotes: a named record, said each step."""
        self.frozen[instrument_id] = f"{WAITING} for {reason}"

    def _record_unprotected(self, instrument_id: InstrumentId, reason: str) -> None:
        """A planning failure is a current named record and a log on every attempt. A carried
        level is kept and raises nothing until a distance can be planned (ruling (a))."""
        reason = f"unprotected: row {self.settings.row} distance refused: {reason}"
        key: Key = (self.settings.account_label or "", instrument_id)
        previous = self.previous_levels.get(key)
        if previous is not None and previous.last_level is not None:
            # Decide the carry (once) so the record can say the carried level is kept.
            position = self._external_long(instrument_id)
            self._previous_entry(
                instrument_id, None if position is None else _quantity(position.quantity)
            )
        if key in self.carried and previous is not None and previous.last_level is not None:
            reason += (
                f"; carried level {previous.last_level} kept, raises nothing until a distance "
                "can be planned"
            )
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _name_refused(self, instrument_id: InstrumentId, reason: str) -> None:
        """A holding whose supplied level was refused is a named record and a log every time."""
        reason = f"unprotected: supplied level refused: {reason}"
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _read_symbol_holds(self) -> None:
        """Read the owner's per-symbol holds (symbol_hold.read_holds) and name each change.

        A new hold takes effect in this step's `_manage` (`_hold`); a removed one re-arms there.
        A folder that cannot be read leaves the last holds read standing, named at every read;
        a stray entry holds nothing and is named at every read too.
        """
        folder = self.settings.symbol_holds_dir
        if folder is None:
            return
        try:
            names, strays, made = symbol_hold.read_holds_if_made(folder)
            if not made and self.holds_made:
                raise FileNotFoundError(folder)
        except (OSError, ValueError) as exc:
            self.holds_unread = type(exc).__name__
            standing = ", ".join(sorted(str(i) for i in self.held)) or "none"
            self._say(
                f"ALERT: symbol-holds folder {folder} unread ({type(exc).__name__}): the last "
                f"holds read stand ({standing}); read again at the next step"
            )
            return
        if not made:
            if not self.holds_unread:
                self.holds_unread = ""  # Said once: no folder was ever made, so none is held.
                self._say(
                    f"no {symbol_hold.FOLDER} folder made at {folder}: none held; read at every step"
                )
            return
        self.holds_made = True
        if self.holds_unread:
            self._say(f"symbol-holds folder {folder} read again after {self.holds_unread}")
        self.holds_unread = None
        if strays:
            self._say(f"ALERT: {symbol_hold.strays_named(strays)}; named at every step")
        now = {InstrumentId.from_str(f"{name}.ALPACA") for name in names}
        for instrument_id in sorted(now - self.held, key=str):
            self._hold_begins(instrument_id)
        for instrument_id in sorted(self.held - now, key=str):
            self.held.discard(instrument_id)
            if self.settings.breach_count > 1:
                continue  # Said where the release is applied, with what it finds (_hold_counted).
            slot = self.slots.get(instrument_id)
            level = None if slot is None else slot.level
            self._say(
                f"{instrument_id}: hold removed: re-armed "
                + (
                    f"at {level}, never lowered"
                    if level is not None
                    else "as at a start, at its book level if it has one"
                )
                + (
                    "; its sell was placed before the hold, so while the bid is above the level "
                    "it is placed at this step, and otherwise"
                    if slot is not None and slot.rearm_at_step
                    else "; it is placed"
                )
                + " under the count rule (two new observations)"
            )

    def _hold_begins(self, instrument_id: InstrumentId) -> None:
        if instrument_id in self.held:
            return
        self.held.add(instrument_id)
        self.hold_counts[instrument_id] = self.hold_counts.get(instrument_id, 0) + 1
        self._say(
            f"{instrument_id}: {symbol_hold.HELD}: not armed; only our own sell of it is "
            "canceled, nothing is sold, and every other holding stays protected"
        )

    def _hold(self, instrument_id: InstrumentId, slot: Holding, order: Any) -> None:
        """The owner's word for this symbol: our own sell of it is canceled, nothing is sent,
        modified or chased for it, and the slot keeps its level (raised to our sell's trigger in
        `_manage` before this) and its venue clock, so the release re-arms from it, never lower.
        A sale in progress ends with its cancel; the release re-arms the trail, not the chase."""
        if slot.placed:
            slot.rearm_at_step = True
        # The guard is reset, a counting observation included (review F5), so a breach during the
        # hold needs the count rule's two new observations after it.
        slot.placed, slot.last_observation = False, None
        if self.frozen.get(instrument_id, "").startswith(WAITING):
            self.frozen.pop(instrument_id)
        if order is not None:
            self._cancel_own(
                slot,
                reason=f"{instrument_id}: {symbol_hold.HELD}: canceling our own sell only "
                "(nothing is sold)",
            )
            return
        if slot.sale is not None:
            self._log_sale(instrument_id, slot.sale)
            self._say(
                f"{instrument_id}: {symbol_hold.HELD}: our sale ended with its cancel; the "
                f"release re-arms the trail at {slot.level}"
            )
            slot.sale = None

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
        tick = quote if quote is not None else self.venue_quotes.get(instrument_id)
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

    def _name_history_failure(self, instrument_id: InstrumentId, slot: Holding) -> None:
        if slot.history_error is not None and not slot.history_error_said:
            slot.history_error_said = True
            self._say(
                f"{instrument_id}: ALERT: the session day's bars read failed ({slot.history_error}); "
                + (
                    "cold rule (fixed fallback) protects until complete bars exist"
                    if slot.cold
                    else "the armed level and distance remain in force"
                )
            )

    def _plan_holding(self, instrument_id: InstrumentId, slot: Holding, tick: QuoteTick) -> bool:
        """Replan cold protection when complete bars arrive, never lowering its level.

        A carried distance stays whole (U-e B). The lead's cold-start amendment allows a carried
        level without a distance to take the shared cold rule at once, at the higher level, then
        replan when bars exist. No level is rebuilt from closes. The trail
        is bid-triggered (crates/execution/src/trailing.rs:128-138), so after the start only bids
        raise it, through the emulator. rebuilt_level_from_bars (unit 4) is a last-price rule and
        is not called here; raising a carried level from the gap's bids is ADR 0151's SIP quotes
        part, not built. Any other holding is unit 10's self-arm, or its supplied level, through
        paper:protect's planner on one cache snapshot.

        Cache.bars is newest first, so feed ATR in timestamp order. The adapter's external minute
        bars are stamped at their open (data.py:1237-1257); ts_init is receipt, not completeness.

        U-h: a holding planned here takes the current venue maximum and the session day's completed
        bars through protect.distance_for (L3); a carried distance is used whole, as above.
        """
        now = self.clock.timestamp_ns()
        # Aged from the earlier of its stamp and its receipt: a stamp ahead of the clock never
        # makes an old quote young (the (e) review's P2-2).
        age = timedelta(seconds=(now - min(tick.ts_event, tick.ts_init)) / 1_000_000_000)
        try:
            newest = self.venue_quotes.get(instrument_id)
            if (
                newest is None
                or tick.ts_event != newest.ts_event
                or tick.ts_event in self.refused_stamps.get(instrument_id, ())
            ):
                raise ValueError("waiting for a planable quote at the current venue maximum")
            if (
                tick.bid_price.as_decimal() <= 0
                or tick.ask_price.as_decimal() < tick.bid_price.as_decimal()
                or tick.bid_size.as_decimal() <= 0
                or tick.ask_size.as_decimal() <= 0
            ):
                raise ValueError("no usable bid/ask prices and sizes")
            if slot.level is None:
                local = datetime.fromtimestamp(now / SECOND_NS, session_window.NEW_YORK)
                if self.settings.feed == "boats":
                    opened = local.replace(hour=20, minute=0, second=0, microsecond=0)
                    if opened > local:
                        opened -= timedelta(days=1)
                else:
                    hour, minute = (
                        (16, 0)
                        if local.hour >= 16
                        else ((9, 30) if (local.hour, local.minute) >= (9, 30) else (4, 0))
                    )
                    opened = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if tick.ts_event < int(opened.timestamp()) * SECOND_NS:
                    raise ValueError(
                        "quote stamped before the open session part; wait for its first quote"
                    )
                if not self._current_for(instrument_id, slot, tick):
                    raise ValueError(
                        "quote stamped before the holding opened and no seed read since confirms "
                        "it; wait for its first quote"
                    )
            bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
            earliest = (
                int(protect.session_start(datetime.fromtimestamp(now / SECOND_NS, UTC)).timestamp())
                * SECOND_NS
            )
            bars = dict(self.history_bars.get(instrument_id, {}))
            bars.update({b.ts_event: b for b in self.cache.bars(bar_type) or []})
            complete = [
                b for at, b in sorted(bars.items()) if earliest <= at and at + 60 * SECOND_NS <= now
            ]
            # Bars allow a plan only on the slice the planner is given (code-review-high/4), and
            # only once the session day's answer is in, or a full ATR's worth has been seen: one
            # live bar while the answer is pending never fixes the run's distance (-high/1).
            # A failed read is no answer one live bar may warm (code-review-high-2/1).
            answered = slot.history_ready
            sound = [b for b in complete[-protect.ATR_BARS :] if protect._sound(b)]
            has_bars = bool(sound) and (answered or len(sound) >= protect.ATR_BARS)
            latest_bar_ns = sound[-1].ts_event if has_bars else None
            replanning = slot.cold and slot.distance is not None
            # A cold plan is planned again only on a newer sound bar than it last saw: no replan
            # on every quote or step for the same bars (code-review-high/2, /8).
            if replanning and (not has_bars or latest_bar_ns == slot.cold_seen):
                self._name_history_failure(instrument_id, slot)
                return True  # Keep the first cold plan until bars allow a new one.
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                raise ValueError("instrument is absent from cache")
            position = self._external_long(instrument_id)
            held = None if position is None else _quantity(position.quantity)
            key, previous = self._previous_entry(instrument_id, held)
            carried = (
                previous
                if previous is not None and key in self.carried and previous.last_level is not None
                else None
            )
            bid = tick.bid_price.as_decimal()
            cold = False
            if carried is not None and carried.distance is not None:
                distance = carried.distance
                share = distance / bid
                # The limit sits a quarter of the trail through the trigger, the Mover's
                # exit_through for the ATR rows OWN1 and OWN3 (mover.py:5630-5632, :961).
                trail = protect.mover.TrailPlan(share, share / 4, share, None, None, "carried")
                plan = protect.Distance(
                    bid, trail, Decimal(0), age, distance, distance, self.settings.row, bid
                )
                source = f"carried from run {carried.run}"
            else:
                ask = tick.ask_price.as_decimal()
                cold = not has_bars
                distance, plan = protect.distance_for(
                    instrument_id.symbol.value,
                    self.settings.row,
                    bid,
                    ask,
                    [] if cold else complete[-protect.ATR_BARS :],
                )
                plan = replace(plan, quote_age=age)
                if plan.fallback:
                    # The fixed fallback is the cold rule, carried or not, and is planned again
                    # on bars (code-review-high-2/6). The lead's ruling (2026-11-13, 23:2x): a
                    # carried long whose record has no distance takes it, never left bare; flat
                    # bars give the same fallback as none (code-review-high/3).
                    cold = True
                if plan.fallback and not replanning:  # counted per holding (-high-2/4)
                    self.fallbacks[instrument_id] = self.fallbacks.get(instrument_id, 0) + 1
                    if plan.bars:
                        self.flat_fallbacks[instrument_id] = (
                            self.flat_fallbacks.get(instrument_id, 0) + 1
                        )
                source = plan.source()
            if carried is not None and carried.limit_offset is not None:
                limit = carried.limit_offset  # Ruling (c): the carried exit, whole.
            else:
                _, limit = offsets(
                    plan.price, plan.plan.trail, plan.plan.exit_through, distance=distance
                )
            if bid - limit <= 0:
                raise ValueError("no positive protective limit at this bid and distance")
            supplied = None if slot.level is None else slot.level.as_decimal()
            if carried is not None:
                assert carried.last_level is not None
                chosen = (
                    carried.last_level
                    if supplied is None
                    else start_level(carried.last_level, supplied)
                )
                if cold and carried.distance is None:
                    chosen = max(chosen, up_on_step(bid - distance))
            else:
                chosen = up_on_step(bid - distance) if supplied is None else supplied
            if replanning:
                order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
                retained = _own_level(slot, order)
                assert retained is not None
                if order is not None and not order.is_closed:
                    # The pin cannot modify a trailing offset. Keep this working sell whole;
                    # warming is never a cancel/re-place gap or a second sell of these shares.
                    slot.level = instrument.make_price(retained)
                    # still cold if the new plan is the fallback again (code-review-high-2/5)
                    slot.cold, slot.cold_seen = cold, latest_bar_ns
                    self._say(
                        f"{instrument_id}: replanned distance {distance}; {source}; "
                        f"working sell keeps distance {slot.distance.distance}, limit offset "
                        f"{slot.limit_offset} and level {slot.level}"
                    )
                    return True
                chosen = max(retained, up_on_step(bid - distance))
            level = instrument.make_price(chosen)
            if chosen <= 0 or level.as_decimal() != chosen:
                raise ValueError(f"no exact positive level {chosen} at instrument precision")
            if slot.level is None and tick.ts_event in self.refused_stamps.get(instrument_id, ()):
                raise ValueError("waiting for a newer quote: V1 refused this stamp")
            if not replanning:
                slot.cold = cold
                self._name_start(
                    instrument_id, slot, previous, carried, chosen, distance, supplied, limit
                )
            if slot.level is None:
                slot.armed_at, slot.armed_event = now, tick.ts_event
                if tick.ts_event < slot.opened_at and (
                    slot.current_from is None or tick.ts_event < slot.current_from
                ):
                    # The lead's ruling A (19:49), term 3, a named limit: (iii) alone allowed this
                    # plan, trusting the host's receipt; named once with both stamps, counted at END.
                    self.receipt_plans[instrument_id] = self.receipt_plans.get(instrument_id, 0) + 1
                    self._say(
                        f"{instrument_id}: planned on its receipt alone: quote stamped "
                        f"{tick.ts_event}, received {tick.ts_init}; the holding opened "
                        f"{slot.opened_at} (venue or host), known to the node {slot.initialized_at}"
                    )
            slot.level = level
            slot.distance, slot.limit_offset = plan, limit
            slot.plan_chase_cap = plan.plan.chase_cap
            slot.cold, slot.cold_seen = cold, latest_bar_ns
            self._say(
                f"{instrument_id}: {'replanned' if replanning else 'armed'} distance {distance}; {source}; "
                f"quote age {age.total_seconds():.0f} s"
                + (
                    f"; confirm_gap_ms={self.settings.confirm_gap_ms}"
                    if self.settings.breach_count > 1
                    else ""
                )
            )
            self._name_history_failure(instrument_id, slot)
        except ValueError as exc:
            self._record_unprotected(instrument_id, str(exc))
            return False
        return True

    def _hold_counted(
        self, instrument_id: InstrumentId, slot: Holding, quote: QuoteTick | None
    ) -> bool:
        """The owner's word for this symbol at a count above one; True where it governs this call.

        Both halves act through the table. A hold is its stop of the instrument (ST): a count is
        dropped, a working sell of ours is asked to cancel, a leg in flight is told, and nothing
        is counted or sent while the hold stands; quotes are not judged, so the level stays where
        it was. Stored order events are judged on quote calls too, so an owed cancel is not left
        to the next timer step. The release is the table's RL, emitted in every call while a
        holding the hold stopped is no longer held: with nothing of ours open the count begins
        again at zero at the kept level, a recorded fault of ours stays a freeze, and a refusal's
        wait still guards the first leg. A row frozen with nothing open is left as it is.
        """
        assert slot.sale_state is not None
        row = slot.sale_state.row
        if instrument_id in self.held:
            slot.hold_applied, slot.hold_release_said = True, False
            if row not in HOLD_SETTLED_ROWS:
                self._sale_step(instrument_id, ["ST"])
            else:
                self._sale_step(instrument_id, ["NQ"] if quote is None else [])
            return True
        if not slot.hold_applied:
            return False
        if row not in SALE_STOPPED_ROWS:
            slot.hold_applied = False  # ended, or frozen with nothing open: the hold asked nothing
            return False
        self._sale_step(instrument_id, ["RL"] if quote is not None else ["RL", "NQ"])
        row = slot.sale_state.row
        if row in SALE_STOPPED_ROWS:
            if not slot.hold_release_said:
                slot.hold_release_said = True
                self._say(
                    f"{instrument_id}: hold removed: our sell of it is still open (sale row {row}); "
                    "the count starts again once it has ended"
                )
            return True
        slot.hold_applied = False
        if row in {"G", "GC"}:
            slot.counted_quote = slot.usable_quote = None
            self._say(
                f"{instrument_id}: hold removed: the count starts again at zero at its level "
                f"{slot.level}, never lowered; a sale needs {self.settings.breach_count} counted "
                f"quotes under it; confirm_gap_ms={self.settings.confirm_gap_ms}"
            )
        return True

    def _manage_counted(
        self, instrument_id: InstrumentId, quote: QuoteTick | None, *, advanced: bool
    ) -> None:
        """Plan as before; only a counted stream quote can enter the sale's quote handler."""
        slot = self.slots[instrument_id]
        if slot.sale_state == sale_table.State("X"):
            positions = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
            if (
                len(positions) == 1
                and positions[0].signed_qty > 0
                and (positions[0].ts_opened, positions[0].ts_init)
                != (slot.opened_at, slot.initialized_at)
            ):
                self._reset_holding(instrument_id)
                slot = self._new_holding(instrument_id, positions[0])
                self.slots[instrument_id] = slot
                self._request_seed(instrument_id)
        if slot.sale_state is None:
            slot.sale_state = sale_table.State("G", 0)
            self._say(
                f"{instrument_id}: sale count starts again at zero; "
                f"confirm_gap_ms={self.settings.confirm_gap_ms}"
            )
        tick = self.venue_quotes.get(instrument_id)
        was_unplanned = slot.level is None
        if slot.distance is None:
            if tick is None:
                self._record_unprotected(
                    instrument_id, slot.seed_error or "no two-sided quote to plan protection"
                )
            else:
                self._plan_holding(instrument_id, slot, tick)
        if self._hold_counted(instrument_id, slot, quote):
            return
        events = []
        if quote is None:
            events.append("NQ")
        elif slot.level is not None and slot.distance is not None:
            code = self._sale_quote(instrument_id, slot, quote, advanced and not was_unplanned)
            if code is not None:
                events.append(code)
        self._sale_step(instrument_id, events)

    def _sale_quote(
        self, instrument_id: InstrumentId, slot: Holding, quote: QuoteTick, advanced: bool
    ) -> str | None:
        """One quote judge: the base venue clock, arming, shape, gap and pin's trail rounding."""
        assert slot.level is not None and slot.distance is not None
        bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        reason = None
        if (
            bid <= 0
            or ask < bid
            or min(quote.bid_size.as_decimal(), quote.ask_size.as_decimal()) <= 0
        ):
            reason = "one-sided, crossed or zero bid quote not counted"
        elif not advanced or not _sane(quote):
            reason = "quote refused by the venue clock; not counted"
        if reason is not None:
            self._say(f"{instrument_id}: {reason}")
            return None
        # Pricing uses every usable stream maximum, independently of arming and count gap.
        slot.usable_quote = quote
        if slot.armed_at is None or quote.ts_init <= slot.armed_at:
            reason = "quote before the arming; not counted"
        elif slot.armed_event is not None and quote.ts_event < slot.armed_event:
            reason = "quote before the arming's venue stamp; not counted"
        elif (
            bid <= slot.level.as_decimal()
            and slot.last_observation is not None
            and quote.ts_event - slot.last_observation
            < (self.settings.confirm_gap_ms or 0) * 1_000_000
        ):
            reason = "gap not reached; quote not counted"
        if reason is not None:
            self._say(f"{instrument_id}: {reason}")
            return None
        slot.counted_quote = quote
        slot.last_observation = quote.ts_event
        above = bid > slot.level.as_decimal()
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        if above and (order is None or order.is_closed):
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                slot.level_instrument_unread = True
                self._freeze(instrument_id, "instrument is absent from cache")
                return None
            # trailing.rs:182-220: precision first, then a coarser increment, nearest even.
            increment = instrument.price_increment
            candidate = bid - slot.distance.distance
            level = Price.from_decimal_dp(candidate, increment.precision)
            if level.raw % increment.raw:
                candidate = (candidate / increment.as_decimal()).to_integral_value(
                    rounding=ROUND_HALF_EVEN
                ) * increment.as_decimal()
                level = Price.from_decimal_dp(candidate, increment.precision)
            if level.as_decimal() > slot.level.as_decimal():
                slot.level = level
            position = self._external_long(instrument_id)
            if position is not None:
                self._gain_lock(instrument_id, slot, position, quote, advanced=True)
        return "QA" if above else "QU"

    def _sale_snapshot(self, instrument_id: InstrumentId, slot: Holding) -> tuple[Any, ...]:
        """Read coverage through the unchanged base function; suppress recursive freeze delivery."""
        positions = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
        position = positions[0] if len(positions) == 1 else None
        held = max(Decimal(0), position.signed_qty) if position is not None else Decimal(0)
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        position_readable = True
        self._sale_reading.add(instrument_id)
        try:
            uncovered = (
                self._uncovered(instrument_id, held, slot.protect_id) if held > 0 else Decimal(0)
            )
            if any(
                p.strategy_id != EXTERNAL
                for p in self.cache.positions_open(instrument_id=instrument_id)
            ):
                self._freeze(instrument_id, "open position belongs to another strategy")
                uncovered = None
                position_readable = False
            if len(positions) > 1 or (position is not None and position.signed_qty < 0):
                self._freeze(instrument_id, "requires exactly one EXTERNAL long")
                uncovered = None
                position_readable = False
            if slot.protect_id is not None and order is None:
                self._freeze(instrument_id, "our sent sell is absent from cache")
                uncovered = None
            if slot.level_instrument_unread:
                if self.cache.instrument(instrument_id) is None:
                    self._freeze(instrument_id, "instrument is absent from cache")
                    uncovered = None
                else:
                    slot.level_instrument_unread = False
        finally:
            self._sale_reading.discard(instrument_id)
        return position, held, uncovered, order, position_readable

    def _sale_sold(self, instrument_id: InstrumentId, ids: set[ClientOrderId]) -> Decimal:
        """All legs, ended as well as working; filled_qty includes the pin's void corrections."""
        return sum(
            (
                order.filled_qty.as_decimal()
                for order in self.cache.orders(instrument_id=instrument_id)
                if order.client_order_id in ids
            ),
            Decimal(0),
        )

    def _sale_reprice_due(
        self, instrument_id: InstrumentId, slot: Holding, order: Any, uncovered: Decimal | None
    ) -> bool:
        """The base chase arithmetic as a question, asked only for a counted QU."""
        sale, quote = slot.sale, slot.counted_quote
        if (
            sale is None
            or quote is None
            or order is None
            or order.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
        ):
            return False
        now = self.clock.timestamp_ns()
        bid = quote.bid_price.as_decimal()
        through = exit_price(bid, protect.PLANNER.exit_ticks, None)
        if (
            sale.floor is not None
            and order.price.as_decimal() == sale.floor
            and through < sale.floor
        ):
            sale.floor_reached = True
            if sale.floor_since is None:
                sale.floor_since = now
            remaining = _remaining(order)
            if (
                not sale.growth_told
                and uncovered is not None
                and remaining is not None
                and uncovered > remaining
            ):
                self._sale_alert(
                    instrument_id,
                    f"floor wait: uncovered {uncovered}, working remainder {remaining}; "
                    "the rest is sold with the next leg",
                )
                sale.growth_told = True
            if now - sale.floor_since >= FLOOR_WAIT_SECS * SECOND_NS:
                sale.floor_wait_ns += now - sale.floor_since
                sale.floor_since, sale.floor, sale.floor_release_price = None, None, through
                self._log_sale(instrument_id, sale)
        elif sale.floor_since is not None:
            sale.floor_wait_ns += now - sale.floor_since
            sale.floor_since = None
            self._log_sale(instrument_id, sale)
        return (
            now - sale.priced_ns >= protect.PLANNER.chase_secs * SECOND_NS
            and (
                sale.cancel_ns is None
                or now - sale.cancel_ns >= protect.PLANNER.chase_secs * SECOND_NS
            )
            and exit_price(bid, protect.PLANNER.exit_ticks, sale.floor) < order.price.as_decimal()
        )

    def _sale_facts(
        self,
        instrument_id: InstrumentId,
        slot: Holding,
        code: str,
        snapshot: tuple[Any, ...],
        *,
        judged: bool = False,
        event_held: Decimal | None = None,
    ) -> tuple[dict[str, bool], Any]:
        assert slot.sale_state is not None
        _, held, uncovered, _, _ = snapshot
        # Actions can replace the leg inside this callback. Coverage is read once;
        # the current leg is always read after the preceding action.
        order = (
            self._sale_orders.get(slot.protect_id)
            if judged
            else None
            if slot.protect_id is None
            else self.cache.order(slot.protect_id)
        )
        quantity = uncovered or Decimal(0)
        event_uncovered = uncovered
        if event_held is not None and uncovered is not None:
            # A later queued fill must not turn an earlier cancellation/expiry into
            # whole cover. Candidate sizes still use the actual uncovered quantity.
            event_uncovered = max(Decimal(0), uncovered + event_held - held)
        candidate = None
        cell = sale_table.TABLE.cells[slot.sale_state.row, code]
        if quantity > 0 and any(sale_table.Action.SEND in b.actions for b in cell.branches):
            quote = slot.usable_quote
            instrument = self.cache.instrument(instrument_id)
            if slot.level is not None and instrument is not None:
                first = slot.sale_state.row == "G"
                floor = (
                    (
                        None
                        if slot.gain_applied_floor is None
                        else slot.gain_applied_floor.as_decimal()
                    )
                    if first
                    else (None if slot.sale is None else slot.sale.floor)
                )
                price = sale_table.leg_limit(
                    None
                    if quote is None
                    else exit_price(quote.bid_price.as_decimal(), protect.PLANNER.exit_ticks, None),
                    level=slot.level.as_decimal(),
                    previous=None if first or slot.sale is None else slot.sale.last_price,
                    floor=floor,
                )
                try:
                    size = instrument.make_qty(quantity)
                    limit = None if price is None else instrument.make_price(price)
                except ValueError, OverflowError:
                    size = limit = None  # The table's formable predicate is false.
                if (
                    price is not None
                    and price > 0
                    and size is not None
                    and limit is not None
                    and size.as_decimal() == quantity
                    and limit.as_decimal() == price
                ):
                    candidate = (size, limit)
        return {
            "whole_cover": event_uncovered == 0,
            "faulted": slot.sale_faulted,
            "filled_part": order is not None and order.filled_qty.as_decimal() > 0,
            "long_remains": (held if event_held is None else event_held) > 0 and not slot.ended,
            "leg_positive": quantity > 0,
            "cancelable": order is not None
            and order.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED),
            "formable": candidate is not None,
            "reprice_due": self._sale_reprice_due(instrument_id, slot, order, uncovered)
            if code == "QU" and slot.sale_state.row in {"WK", "PF"}
            else False,
            "ends_filled": order is not None and order.status == OrderStatus.FILLED,
            "refusal_told": slot.refusal_told,
            "resend_due": code == "QU"
            and all(
                at is None or self.clock.timestamp_ns() - at >= wait * SECOND_NS
                for at, wait in (
                    (slot.refusal_last_ns, refusal_wait_secs(slot.refusal_streak)),
                    (slot.external_cancel_ns, OUTSIDE_CANCEL_WAIT_SECS),
                )
            ),
            "flight_alert_due": code in {"QU", "QA", "NQ"}
            and slot.sale_state.row in SALE_CANCEL_ROWS | {"FL"}
            and not slot.flight_told
            and not slot.flight_answered
            and slot.flight_sent_ns is not None
            and self.clock.timestamp_ns() - slot.flight_sent_ns >= FLIGHT_SECS * SECOND_NS,
        }, candidate

    def _sale_step(
        self,
        instrument_id: InstrumentId,
        events: list[str],
        *,
        reason: str = "",
        order_event_ns: int | None = None,
    ) -> None:
        """Take one event batch, invalidate first, replay arrivals, cut, then judge the quote."""
        slot = self.slots.get(instrument_id) or self.stopped_sales.get(instrument_id)
        if slot is None:
            return
        if slot.sale_state is None:
            slot.sale_state = sale_table.State("G", 0)
        # Detach before doing any work. Submit/cancel echoes append to the next batch.
        queued = self._sale_events.pop(instrument_id, [])
        events = list(events)
        snapshot = self._sale_snapshot(instrument_id, slot)
        position, held, uncovered, order, position_readable = snapshot
        net = held + self._sale_sold(instrument_id, self.sent_ids)
        previous = slot.observed_quantities
        foreign = (
            held - uncovered if uncovered is not None else (previous[1] if previous else Decimal(0))
        )
        # Store observations before actions: their nested callbacks see this same cache snapshot.
        # An unread position is FRZ, not evidence that the holding shrank or ended.
        # Keep the last answer until the position can actually be read again.
        if position_readable:
            slot.observed_quantities = net, foreign
        if previous is not None and position_readable:
            if net < previous[0]:
                events.append("HE" if held == 0 else "HS")
            elif net > previous[0]:
                events.append("HG")
            if foreign != previous[1]:
                events.append("CP" if foreign > previous[1] else "CL")
        elif uncovered == 0 and held > 0:
            events.append("CP")
        if (
            position_readable
            and position is not None
            and "HE" not in events
            and (position.ts_opened, position.ts_init)
            != (
                slot.opened_at,
                slot.initialized_at,
            )
        ):
            events.append("HE")
        if uncovered is None and "FRZ" not in events:
            events.append("FRZ")
        elif (
            uncovered is not None and "FRZ" not in events and slot.sale_state.row in SALE_COVER_ROWS
        ):
            events.append("UFZ")
        # The position already includes the entire batch. Undo its fills for the event
        # projection, then consume them in arrival order; invalidations above still use
        # the actual holding net of ALL native fills. Sends use actual uncovered shares.
        event_held = held + sum(
            (item.last_qty.as_decimal() for item in queued if type(item).__name__ == "OrderFilled"),
            Decimal(0),
        )
        entries = [(code, None) for code in events]
        entries.extend(
            (sale_table.order_event_column(type(item).__name__), item) for item in queued
        )
        derive_mismatch = "MO" not in events
        if derive_mismatch:
            entries.append(("MO", None))
        cell = f"{slot.sale_state.row} x {events[-1] if events else 'step'}"
        try:
            for tagged in sale_table.order_step(
                sale_table.Event(code, {str(index): True})
                for index, (code, _) in enumerate(entries)
            ):
                code, item = entries[int(next(iter(tagged.facts)))]
                current_reason, current_ns = reason, order_event_ns
                wait_from_ns = order_event_ns
                if item is not None:
                    code = self._judge_sale_event(slot, item)
                    current_reason = str(getattr(item, "reason", None) or "")
                    current_ns = item.ts_event
                    # A wait is a pace toward the venue as the node can keep it: counted from
                    # the later of the event's own time and its receipt here.
                    wait_from_ns = max(current_ns, getattr(item, "ts_init", current_ns))
                    # Each order event of ours, with the node's own lag from its receipt to this
                    # step: the cost of judging in a step, measured on every event.
                    lag = self.clock.timestamp_ns() - getattr(item, "ts_init", current_ns)
                    self._say(
                        f"{instrument_id}: sale event {code} in {slot.sale_state.row}; "
                        f"judged_after_ms={lag / 1_000_000:g}"
                    )
                    if type(item).__name__ == "OrderFilled":
                        event_held -= item.last_qty.as_decimal()
                if code == "MO" and derive_mismatch:
                    current = self._sale_orders.get(slot.protect_id)
                    if current is None:
                        current = (
                            None if slot.protect_id is None else self.cache.order(slot.protect_id)
                        )
                    if (
                        current is None
                        or slot.sale_state.row in SALE_CANCEL_ROWS
                        or current.client_order_id in self.cancel_requested
                        or current.status
                        not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
                    ):
                        continue
                    remaining = _remaining(current)
                    too_large = (
                        remaining is not None and remaining > uncovered
                        if uncovered is not None
                        else self._oversell_possible(instrument_id)
                    )
                    if not too_large:
                        continue
                event = sale_table.Event(code, {})
                cell = f"{slot.sale_state.row} x {event.code}"
                facts, candidate = self._sale_facts(
                    instrument_id,
                    slot,
                    event.code,
                    snapshot,
                    judged=bool(queued),
                    event_held=event_held if item is not None else None,
                )
                before = slot.sale_state
                result = sale_table.transition(
                    before, self.settings.breach_count, event.code, facts
                )
                slot.sale_state = (
                    result.state
                )  # Initialized/PendingCancel can reenter inside actions.
                table_cell = sale_table.TABLE.cells[before.row, event.code]
                cell_reason = (
                    result.named or f"{cell}: {table_cell.note or f'sale row {result.state.row}'}"
                )
                if event.code == "FRZ":
                    cell_reason = (
                        f"{cell}: {self.frozen.get(instrument_id, reason or 'cover unread')}"
                    )
                action_reason = cell_reason + (f"; {current_reason}" if current_reason else "")
                if event.code == "ST" and current_reason:
                    action_reason = current_reason
                terms = set(result.when.split(" and "))
                refusal = event.code == "oREJ" and bool(
                    terms & {"refusal_told", "not refusal_told"}
                )
                silence = "flight_alert_due" in terms
                unformed = "not formable" in result.when
                external_cancel = event.code == "oCXL" and before.row in {"FL", "WK", "PF", "FCW"}
                if result.state.row in SALE_FAULT_ROWS or event.code in {"oDEN", "oVOID", "oOTH"}:
                    slot.sale_faulted = True
                if event.code == "oREJ" and before.row not in SALE_NOTHING_OPEN_ROWS:
                    # EVERY rejection of an open sale of ours is recorded, whatever the row that
                    # judges it (stopped, held, cover unread), so the pace guards the next leg.
                    now = self.clock.timestamp_ns() if current_ns is None else current_ns
                    slot.refusal_last_ns = now if wait_from_ns is None else wait_from_ns
                    if slot.refusal_first is None:
                        slot.refusal_first = datetime.fromtimestamp(
                            now / SECOND_NS, UTC
                        ).isoformat()
                    slot.refusal_reason = self._sale_reason(instrument_id, current_reason)
                    slot.refusals += 1
                    slot.refusal_streak += 1
                if refusal:
                    wait = refusal_wait_secs(slot.refusal_streak)
                    action_reason = (
                        f"{cell}: venue refused our sale (REJECTED); sent again on a counted quote "
                        f"under the level after {wait} s; {current_reason}"
                    )
                    if slot.refusal_told:
                        self._say(
                            f"{instrument_id}: our sale refusal {slot.refusals} "
                            f"since {slot.refusal_first}; REJECTED; sent again after {wait} s"
                        )
                elif silence:
                    action_reason = (
                        f"our sale {slot.flight_command} has no answer after {FLIGHT_SECS} s; "
                        "outcome unknown, never sent again while unresolved"
                    )
                elif unformed:
                    action_reason = (
                        f"{cell}: our sale cannot be formed; next counted quote judged again"
                    )
                elif external_cancel:
                    slot.external_cancel_ns = (
                        self.clock.timestamp_ns() if wait_from_ns is None else wait_from_ns
                    )
                    action_reason = (
                        f"{cell}: our sale canceled, and not by us (the venue or another hand); no sale for "
                        f"{OUTSIDE_CANCEL_WAIT_SECS} s; then a full new count; {HOLD_ONE_SYMBOL}"
                    )
                if result.state.row in SALE_FAULT_ROWS and before.row not in SALE_FAULT_ROWS:
                    self.frozen[instrument_id] = action_reason
                if (
                    event.code == "UFZ"
                    and before.row in SALE_COVER_ROWS
                    and result.state.row not in SALE_COVER_ROWS
                ):
                    self.frozen.pop(instrument_id, None)
                    follows = (
                        "a new count"
                        if result.state.row in {"G", "GC"}
                        else "our leg, still working"
                    )
                    self._say(
                        f"{instrument_id}: cover reads again; {follows}; sale row {result.state.row}"
                    )
                if result.state.row in (
                    SALE_FAULT_ROWS
                    | SALE_COVER_ROWS
                    | {"G", "GC", "T0", "X", "OX", "PCX", "PCT", "TW"}
                ):
                    self._finish_counted_sale(instrument_id, slot, current_ns)
                if before.row == "G" and event.code in {"QU", "QA"}:
                    count = result.state.count
                    if count is None:
                        count = (
                            self.settings.breach_count
                            if sale_table.Action.SEND in result.actions
                            else 0
                        )
                    # Only a count that changed is said: a holding above its level, or one held
                    # by the interval, would otherwise write a line for every quote.
                    if count != before.count:
                        self._say(
                            f"{instrument_id}: counted quote count={count}; "
                            f"confirm_gap_ms={self.settings.confirm_gap_ms}"
                        )
                if result.named and sale_table.Action.ALERT not in result.actions:
                    self._say(f"{instrument_id}: {result.named}")
                for action in result.actions:
                    self._sale_action(
                        instrument_id,
                        slot,
                        action,
                        candidate,
                        before,
                        action_reason,
                    )
                    if action == sale_table.Action.ALERT:
                        if "not refusal_told" in terms:
                            slot.refusal_told = True
                        elif silence:
                            slot.flight_told = True
                if refusal and slot.sale is not None:
                    self._log_sale(instrument_id, slot.sale)
                if slot.sale_state.row in {"G", "GC", "FZ", "FC", "T0", "X"}:
                    slot.protect_id = None
                    slot.sale = None
                if slot.sale_state.row == "X" and not slot.ended:
                    slot.ended = True
                    self._retire(instrument_id)
                    self.levels.pop(instrument_id, None)
                if slot.sale_state.row in {"T0", "X"}:
                    self.stopped_sales.pop(instrument_id, None)
        except sale_table.SaleTableError as exc:
            slot.sale_faulted = True
            self.frozen[instrument_id] = f"{cell}: {type(exc).__name__}: {exc}"
            order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
            slot.sale_state = sale_table.State(
                ("FZP" if order.client_order_id in self.cancel_requested else "FZW")
                if order is not None and not order.is_closed
                else "FZ"
            )
            self._finish_counted_sale(instrument_id, slot, order_event_ns)
            self._sale_alert(instrument_id, self.frozen[instrument_id])

    def _finish_counted_sale(
        self, instrument_id: InstrumentId, slot: Holding, event_ns: int | None
    ) -> None:
        """Record an episode's end once, retaining an open leg for its later callbacks."""
        sale = slot.sale
        if sale is None or sale.end_recorded:
            return
        filled = [
            getattr(order, "ts_closed", None) or event_ns or self.clock.timestamp_ns()
            for cid in sale.order_ids
            if (order := self._sale_orders.get(cid)) is not None
            and order.status == OrderStatus.FILLED
        ]
        if filled:
            sale.filled_ns = max(filled)
        sale.end_recorded = True
        self._log_sale(instrument_id, sale)

    def _sale_reason(self, instrument_id: InstrumentId, reason: str) -> str:
        """The same bounded, ID-free venue text in alerts and sale records."""
        identities = {str(cid) for cid in self.sent_ids}
        for order in self.cache.orders(instrument_id=instrument_id):
            identities.add(str(order.client_order_id))
            venue_id = getattr(order, "venue_order_id", None)
            if venue_id is not None:
                identities.add(str(venue_id))
        for identity in sorted(identities, key=len, reverse=True):
            reason = reason.replace(identity, "our leg")
        return (reason.splitlines() or [""])[0][:160]

    def _sale_alert(self, instrument_id: InstrumentId, reason: str) -> None:
        self._say(f"ALERT: {instrument_id}: {self._sale_reason(instrument_id, reason)}")

    def _sale_action(
        self,
        instrument_id: InstrumentId,
        slot: Holding,
        action: sale_table.Action,
        candidate: Any,
        before: sale_table.State,
        reason: str,
    ) -> None:
        """The only count-above-one entry to submit or cancel; no modify is used."""
        self._counted_cache = self.cache
        self._counted_identity = self.strategy_id
        self._counted_logger = self.log
        if action == sale_table.Action.ALERT:
            self._sale_alert(instrument_id, reason)
        elif action == sale_table.Action.CANCEL:
            now = self.clock.timestamp_ns()
            if slot.sale is not None:
                slot.sale.cancel_ns = now
            slot.flight_sent_ns, slot.flight_told, slot.flight_answered = now, False, False
            slot.flight_command, slot.flight_answer_ns = "cancel", None
            self._cancel_own(slot)
        elif action == sale_table.Action.SEND:
            assert candidate is not None
            size, limit = candidate
            cid = self._protect_id()
            if self.cache.order(cid) is not None:
                raise sale_table.InvalidFacts("new sale ID already exists in cache")
            candidate = self.order_factory.limit(
                instrument_id,
                OrderSide.SELL,
                size,
                limit,
                time_in_force=TimeInForce.DAY,
                reduce_only=True,
                client_order_id=cid,
            )
            now, price = self.clock.timestamp_ns(), candidate.price.as_decimal()
            if before.row == "G":
                slot.opening_quantity = candidate.quantity.as_decimal()
                slot.sale = Sale(
                    now, price, self._sale_floor(instrument_id, slot, price), now, price
                )
                quote = slot.counted_quote
                slot.gain_release_spread = (
                    None
                    if quote is None
                    else quote.ask_price.as_decimal() - quote.bid_price.as_decimal()
                )
            assert slot.sale is not None
            previous = None if before.row == "G" else slot.sale.last_price
            slot.sale.reprices += int(before.row == "PCR")
            slot.sale.resends += int(before.row == "RW")
            slot.sale.last_price, slot.sale.priced_ns = price, now
            slot.sale.order_ids.add(candidate.client_order_id)
            slot.protect_id = candidate.client_order_id
            slot.flight_sent_ns, slot.flight_told, slot.flight_answered = now, False, False
            ack_ns = slot.flight_answer_ns if before.row == "PCR" else None
            refused_ns = slot.refusal_last_ns if before.row == "RW" else None
            slot.flight_command, slot.flight_answer_ns = "submit", None
            self.sent_ids.add(candidate.client_order_id)
            self._sale_orders[candidate.client_order_id] = LimitOrder.from_dict(candidate.to_dict())
            floor = slot.gain_applied_floor if before.row == "G" else slot.sale.floor
            self._say(
                f"{instrument_id}: sale send limit={price} level={slot.level} "
                f"previous={previous} floor={floor}"
                + ("" if ack_ns is None else f" ack_to_send_ms={(now - ack_ns) / 1_000_000:g}")
                + (
                    ""
                    if refused_ns is None
                    else f" refusal_to_send_ms={(now - refused_ns) / 1_000_000:g}"
                )
            )
            self._log_sale(instrument_id, slot.sale)
            self.submit_order(candidate, params={"extended_hours": True})

    def on_order_event(self, event: Any) -> None:
        if self.settings.breach_count == 1 or event.client_order_id not in self.sent_ids:
            return
        self._sale_events.setdefault(event.instrument_id, []).append(event)

    def _judge_sale_event(self, slot: Holding, event: Any) -> str:
        """Replay only arrivals already detached for this step, using the pinned order machine."""
        order = self._sale_orders.get(event.client_order_id)
        if order is None:
            raise sale_table.InvalidFacts("our queued event has no sent leg to replay")
        ended = any(
            type(prior).__name__ in {"OrderCanceled", "OrderExpired"} for prior in order.events()
        )
        column = sale_table.order_event_column(type(event).__name__)
        if column not in {"oINI", "oOTH"}:
            try:
                order.apply(event)
            except (ValueError, RuntimeError, TypeError) as exc:
                raise sale_table.InvalidFacts(
                    f"native order replay refused {type(event).__name__}: {exc}"
                ) from exc
        if order.status != OrderStatus.PENDING_CANCEL:
            self.cancel_requested.discard(event.client_order_id)
        code = sale_table.order_event_column(
            type(event).__name__,
            leg_ended=ended,
            leaves_remainder=(_remaining(order) or Decimal(0)) > 0,
        )
        if slot is not None:
            # A cancel is answered only by what ends the leg or refuses the cancel: a part fill, an
            # acceptance or an update while it is pending says nothing of the cancel.
            answers = (
                code in SALE_CANCEL_ANSWERS
                if slot.flight_command == "cancel"
                else code not in {"oINI", "oSUB", "oPCN"}
            )
            if event.client_order_id == slot.protect_id and answers:
                slot.flight_answered = True
                # A lag is the node's own, counted from its receipt of the answer.
                slot.flight_answer_ns = getattr(event, "ts_init", event.ts_event)
            if code in {"oPF", "oFF", "oLF", "oCXL", "oEXP"}:
                slot.refusal_streak = 0  # a fill, or a leg that ended without a refusal
            if code in {"oACC", "oPF", "oFF", "oLF"}:
                if slot.refusals and code == "oACC":
                    self._say(
                        f"{event.instrument_id}: our sale was accepted after {slot.refusals} "
                        f"refusals since {slot.refusal_first}"
                    )
                had_refusal = slot.refusals > 0
                slot.refusal_told = False
                slot.refusal_reason, slot.refusal_first, slot.refusals = None, None, 0
                if had_refusal and slot.sale is not None:
                    self._log_sale(event.instrument_id, slot.sale)
            if (
                type(event).__name__ == "OrderFilled"
                and slot.sale is not None
                and event.client_order_id in slot.sale.order_ids
                and slot.gain_armed_at is not None
                and slot.gain_entry is not None
            ):
                self._gain_lock_sale(event, slot)
        return code

    def on_position_event(self, event: Any) -> None:
        """Not delivered for these EXTERNAL holdings at d53849b216e2.

        engine/mod.rs:4109-4118 publishes to the position's strategy ID. Each
        quote and each timer step reads the holding, independently of this hook.
        """
        if self.settings.breach_count > 1:
            self._sale_step(event.instrument_id, [])

    def _manage(
        self, instrument_id: InstrumentId, quote: QuoteTick | None = None, *, advanced: bool = False
    ) -> None:
        if self.settings.breach_count > 1:
            self._manage_counted(instrument_id, quote, advanced=advanced)
            return
        tick = quote if quote is not None else self.venue_quotes.get(instrument_id)
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
                self._request_seed(instrument_id)
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
                        # C1's P2-1, the ruled design (the lead, 2026-11-13 19:11): a 403 or 422 is
                        # definitive. Frozen with the venue's own reason, never sent again in this
                        # run, one ALERT; the next start re-arms the holding from the book.
                        venue = next(
                            (
                                str(event.reason)
                                for event in reversed(order.events())
                                if getattr(event, "reason", None)
                            ),
                            "no reason given",
                        )
                        reason = (
                            f"our protective sell ended {order.status.name}: {venue}; not sent "
                            "again this run; the next start re-arms it from the book"
                        )
                        if instrument_id not in self.alerted:
                            self.alerted.add(instrument_id)
                            self._say(
                                f"{instrument_id}: ALERT: our protective sell was refused by the "
                                f"venue ({order.status.name}: {venue}); the holding is unprotected "
                                "until the next start"
                            )
                        self._freeze(instrument_id, reason)
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
        if instrument_id in self.held:
            self._hold(instrument_id, slot, order)
            return
        if slot.sale is None:
            if slot.cold:
                self._plan_from_maximum(instrument_id, slot)
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
            self.replace_alerted.discard(instrument_id)
            if _remaining(order) != uncovered:
                self._cancel_own(slot)  # Never modified in the step that cancels it for size.
            elif slot.sale is not None:
                self._chase_sale(instrument_id, slot, order, quote)
            else:
                self._raise_gain_order(slot, order)
            return
        if uncovered <= 0:
            return
        usable = self.replacement_quotes.get(instrument_id)
        sale_quote = quote
        if slot.placed and usable is not None:
            # A terminal acknowledgement has removed our cover. Re-place on the retained
            # usable quote without an age or new-observation gate, never re-plan the level.
            tick = sale_quote = usable
            advanced = advanced and tick is quote
        elif slot.placed and instrument_id not in self.replace_alerted:
            # Our cover ended and no usable quote has ever arrived for it: named once at
            # ERROR; the records below (a stalled sale, an unprotected row) say it each step.
            self.replace_alerted.add(instrument_id)
            self._say(
                f"{instrument_id}: ALERT: holding without its working sell; no usable quote "
                "to place its protective sale; waiting for one"
            )
        if slot.sale is not None:
            self._send_sale(instrument_id, slot, uncovered, sale_quote)
            return
        if tick is None or (
            slot.level is None
            and not self._current_for(instrument_id, slot, tick)
            and instrument_id in self.quote_refusals
        ):
            # No quote, or none known current while every newer one is refused: the refusal is
            # why nothing can be planned, so the wait names it (e1).
            if instrument_id in self.quote_refusals:
                self._wait(instrument_id, self.quote_refusals[instrument_id])
                return
            self._record_unprotected(
                instrument_id, slot.seed_error or "no two-sided quote to plan protection"
            )
            return
        if slot.distance is None:
            tick = self.venue_quotes.get(instrument_id, tick)
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
        if not slot.placed and not advanced and instrument_id in self.quote_refusals:
            self._wait(instrument_id, self.quote_refusals[instrument_id])
            return
        if not slot.placed:
            # Released from the owner's hold above its level: re-placed at this step on the venue
            # maximum, with no new observation, since a sell resting under the bid elects nothing.
            newest = self.venue_quotes.get(instrument_id)
            rearm = (
                slot.rearm_at_step
                and newest is not None
                and tick.ts_event == newest.ts_event
                and bid > slot.level.as_decimal()
                and _usable(tick, self.clock.timestamp_ns())
            )
            if not _sane(tick):
                self._note(tick)
                self._wait(instrument_id, _ahead(tick))
                return
            if not advanced and not rearm:
                self._wait(
                    instrument_id,
                    "a quote stamped after the venue maximum"
                    if slot.last_observation is None
                    else "a second observation newer than the one guarded",
                )
                return
            if (
                slot.last_observation is None
                and not rearm
                and (slot.fresh or bid <= slot.level.as_decimal())
            ):
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
        slot.rearm_at_step = False
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
            at = (
                released[0].ts_event
                if released
                else self.released_at.get(order.client_order_id, self.clock.timestamp_ns())
            )
            first = order.price.as_decimal()
            slot.sale = Sale(at, first, self._sale_floor(instrument_id, slot, first), at, first)
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

    def _sale_floor(self, instrument_id: InstrumentId, slot: Holding, price: Decimal) -> Decimal:
        cap = protect.PLANNER.chase_cap
        source = "registered row"
        if slot.plan_chase_cap is not None and slot.plan_chase_cap >= cap:
            cap, source = slot.plan_chase_cap, "holding plan"
        floor = chase_floor(price, cap)
        self._say(f"{instrument_id}: sale floor {floor}; {source} cap={cap}")
        return floor

    def _log_sale(self, instrument_id: InstrumentId, sale: Sale) -> None:
        record = sale.record(self.clock.timestamp_ns())
        if self.settings.breach_count > 1:
            slot = self.slots.get(instrument_id) or self.stopped_sales.get(instrument_id)
            record.update(
                confirm_gap_ms=self.settings.confirm_gap_ms,
                resends=sale.resends,
                refusal_reason=None if slot is None else slot.refusal_reason,
                refusal_first=None if slot is None else slot.refusal_first,
                refusals=0 if slot is None else slot.refusals,
            )
        self._say(f"{instrument_id}: sale record {json.dumps(record, sort_keys=True)}")

    def _sale_stalled(self, instrument_id: InstrumentId, reason: str) -> None:
        reason = f"sale stalled: {reason}"
        self.frozen[instrument_id] = reason
        self._say(f"{instrument_id}: {reason}; no new sell")

    def _sale_bid(self, instrument_id: InstrumentId, quote: QuoteTick | None) -> Decimal | None:
        tick = quote if quote is not None else self.venue_quotes.get(instrument_id)
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
        self._retire(instrument_id)
        self._say(f"{instrument_id}: previous holding ended; its level and distance were dropped")

    def _retire(self, instrument_id: InstrumentId) -> None:
        """U-e A: the key outlives the holding, so the next holding on this name never takes this
        one's entry or level. Review F3: its book entry is retired, written with no level, state
        frozen, reason "holding ended", so no later run can carry it."""
        key: Key = (self.settings.account_label or "", instrument_id)
        self.carried.discard(key)
        self.written_keys.discard(key)
        self.replaceable.add(key)
        self.not_carried[key] = "a new holding: the previous one ended this run"
        prior = self.book_entries.get(key)
        if self.settings.account_label is not None and prior is not None:
            self.retired[key] = Entry(
                None,
                prior.protection_start_ns,
                prior.quantity,
                self.clock.timestamp_ns(),
                self.run,
                "frozen",
                None,
                "holding ended",
            )

    def _end_holding(self, instrument_id: InstrumentId, slot: Holding, order: Any) -> None:
        slot.ended = True
        self._retire(instrument_id)
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
        if self.settings.breach_count > 1:
            self.stopped_instruments.add(instrument_id)
            slot = self.slots.pop(instrument_id, None)
            if slot is not None:
                self.stopped_sales[instrument_id] = slot
                order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
                reason = ""
                if self.stopping and order is not None and not order.is_closed:
                    reason = (
                        f"{order.side.name} {order.quantity} LIMIT {order.price}; "
                        "left working at whole-node stop; next start: cover; below limit "
                        "NO EFFECTIVE EXIT until session part end expiry"
                    )
                self._sale_step(instrument_id, ["ST"], reason=reason)
            self.levels.pop(instrument_id, None)
            return
        self.stopped_instruments.add(instrument_id)
        self.refused_stamps.pop(instrument_id, None)
        self.venue_quotes.pop(instrument_id, None)
        self.replacement_quotes.pop(instrument_id, None)
        self.replace_alerted.discard(instrument_id)
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
        self._say_deferred()
        self.stopping = True
        logging.getLogger(STREAM_LOGGER).removeHandler(self.stream_errors)
        self._save_levels(final=True)
        self._name_unapplied()
        if STEP_TIMER in self.clock.timer_names():
            self.clock.cancel_timer(STEP_TIMER)
        for instrument_id in sorted(
            self.stamp_counts.keys() | self.slots.keys() | self.held, key=str
        ):
            slot = self.slots.get(instrument_id)
            ahead, not_raised, redelivered = self.stamp_counts.get(instrument_id, [0, 0, 0])
            fallbacks = self.fallbacks.get(instrument_id, 0)
            flat = self.flat_fallbacks.get(instrument_id, 0)
            self._say(
                f"{instrument_id}: END refused-ahead={ahead}, not-raised={not_raised}, "
                f"refused-redeliveries={redelivered}; "
                f"receipt-clause plans={self.receipt_plans.get(instrument_id, 0)}; "
                f"stream-errors (node)={len(self.stream_errors.lines)}; "
                f"fixed fallback={fallbacks}, "
                f"no history={fallbacks - flat}, flat session day={flat}; "
                f"first_quote={None if slot is None else slot.first_quote}, "
                f"last_quote={None if slot is None else slot.last_quote}; "
                f"owner holds={self.hold_counts.get(instrument_id, 0)}, "
                + (
                    f"{symbol_hold.HELD} at end"
                    if instrument_id in self.held
                    else "not held at end"
                )
                + (
                    f"; refusals={0 if slot is None else slot.refusals}, "
                    f"refusal_first={None if slot is None else slot.refusal_first}"
                    if self.settings.breach_count > 1
                    else ""
                )
            )
            self.stop_instrument(instrument_id)

    def _name_unapplied(self) -> None:
        """P11: a kept level whose holding never appeared is named; its book entry stays as it was."""
        for instrument_id, level in sorted(self.levels.items(), key=lambda item: str(item[0])):
            if instrument_id not in self.applied:
                self._say(
                    f"{instrument_id}: supplied level {level} not applied: no selectable EXTERNAL "
                    "holding appeared this run"
                )
        for key, entry in sorted(self.previous_levels.items(), key=lambda kv: str(kv[0][1])):
            if key[0] == self.settings.account_label and key not in self.carried:
                # Review F2: say what the book holds now, as the last write left it.
                now = self.book_entries.get(key)
                if key in self.retired:
                    fate = "its holding ended this run, so its entry is retired with no level"
                elif now is None or now == entry:
                    fate = "its entry kept as written"
                else:
                    fate = (
                        f"replaced by run {now.run}'s entry: level {now.last_level} ({now.state}"
                        f"{': ' + now.reason if now.reason else ''})"
                    )
                slot = self.slots.get(key[1])
                memory = (
                    f"; this run protected at {slot.level} in memory"
                    if slot is not None and slot.level is not None and (now is None or now == entry)
                    else ""
                )
                self._say(
                    f"{key[1]}: book level {entry.last_level} (protection start "
                    f"{entry.protection_start_ns}) not applied this run: "
                    f"{symbol_hold.HELD if key[1] in self.held else self.not_carried.get(key, 'its holding was not selectable')}; "
                    f"{fate}{memory}"
                )

    def _holding_state(
        self, instrument_id: InstrumentId, slot: Holding | None
    ) -> tuple[str, int | None, str | None]:
        """Map this strategy's own state to the node report's states (item 4).

        A frozen record (`_freeze`, `_record_unprotected`, `_name_refused`) is the first answer:
        a planner refusal (`_record_unprotected`, re-tried on each quote and step) is `waiting`;
        every other record holds the holding until its cause ends, so it is `frozen`. A working
        sell of ours is `armed` (`_manage` sends it once the guard passes), unless its cancel was
        requested to re-size (`_cancel_own`), which `_manage` waits on: `waiting`. Before a send,
        a level not yet planned is `waiting`; a bid at or under the level is `guarded` (the
        first-observation guard in `_manage` holds that sale); otherwise `counting`, with 1 once
        the first observation is recorded in `slot.last_observation`, of the 2 a send needs.
        A `_wait` record (the venue clock's `WAITING`: a holding waiting on its quotes, not a
        freeze) is `waiting` with its reason until a level and distance are planned, and after
        that maps as the planned slot does: `guarded` or `counting`.
        """
        if self.settings.breach_count > 1 and slot is not None:
            row = None if slot.sale_state is None else slot.sale_state.row
            held = instrument_id in self.held
            if row in SALE_FAULT_ROWS | SALE_COVER_ROWS:
                reason = self.frozen.get(instrument_id, f"sale row {row}")
                return "frozen", None, f"{reason}; {symbol_hold.HELD}" if held else reason
            if held:
                if row in {None, "G", "GC", "RW", "T0", "X"}:
                    return (
                        "held",
                        None,
                        f"{symbol_hold.HELD}: not armed; removing the hold re-arms it",
                    )
                # Our sell still rests, its cancel asked: told as it is until the cancel ends it.
                return "waiting", None, f"sale row {row}; {symbol_hold.HELD}"
            if row == "GC":
                return "waiting", None, "foreign sells cover the whole long; no count accrues"
            if slot.level is None or slot.distance is None or slot.sale_state is None:
                return "waiting", None, "no level planned yet"
            row = slot.sale_state.row
            if row == "G":
                return "counting", slot.sale_state.count, None
            if row == "RW":
                # Nothing of ours works at the venue: never `armed`. The book holds what counting
                # holds, at zero, and nothing of the refusal (the lead's T3, 2026-11-13 22:34).
                return "counting", 0, None
            if row in {"FL", "WK", "PF"}:
                return "armed", None, None
            return "waiting", None, f"sale row {row}"
        if instrument_id in self.held:
            frozen = self.frozen.get(instrument_id)
            if frozen is not None and not frozen.startswith(WAITING):
                return "frozen", None, f"{frozen}; {symbol_hold.HELD}"  # Review F6: kept.
            if slot is None or slot.protect_id is None:
                return "held", None, f"{symbol_hold.HELD}: not armed; removing the hold re-arms it"
            # Our sell still rests, its cancel asked: told as it is until the cancel ends it.
        reason = self.frozen.get(instrument_id)
        if reason is not None and reason.startswith(WAITING):
            if slot is None or slot.level is None or slot.distance is None:
                return "waiting", None, reason
            reason = None
        if reason is not None:
            waiting = reason.startswith(f"unprotected: row {self.settings.row} ")
            return ("waiting" if waiting else "frozen"), None, reason
        if slot is None:
            return "waiting", None, "holding not yet selected"
        order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
        if order is not None and not order.is_closed:
            if order.client_order_id in self.cancel_requested:
                return "waiting", None, "our sell is being canceled to re-size"
            return "armed", None, None
        if slot.level is None or slot.distance is None:
            return "waiting", None, "no level planned yet"
        tick = self.cache.quote(instrument_id)
        if tick is not None and tick.bid_price.as_decimal() <= slot.level.as_decimal():
            return "guarded", None, "the first-observation guard holds the sale"
        return "counting", (0 if slot.last_observation is None else 1), None

    def _current_levels(self) -> dict[Key, Entry]:
        """Each holding's level and state now: the emulated sell's trigger, else the retained one.

        A holding with no level of this run keeps its book entry's level, so no write lowers it.
        """
        assert self.settings.account_label is not None
        account = self.settings.account_label
        now = self.clock.timestamp_ns()
        entries: dict[Key, Entry] = {}
        for instrument_id, slot in self.slots.items():
            if slot.ended:
                continue
            positions = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
            if len(positions) != 1 or positions[0].signed_qty <= 0:
                continue
            position = positions[0]
            if (position.ts_opened, position.ts_init) != (slot.opened_at, slot.initialized_at):
                continue
            order = None if slot.protect_id is None else self.cache.order(slot.protect_id)
            if order is not None and order.status == OrderStatus.FILLED:
                continue  # Its completed sale ended this protection cycle.
            key: Key = (account, instrument_id)
            prior = self.book_entries.get(key)
            state, count, reason = self._holding_state(instrument_id, slot)
            own = _own_level(slot, order)
            if prior is not None and key not in self.carried and key not in self.written_keys:
                if key in self.replaceable:
                    prior = None  # Another holding's entry: replaced, never merged into this one.
                elif key in self.not_carried:
                    # Review F2: unknown whose holding this is, so never written lower; this run's
                    # own level replaces it only once it is higher.
                    if prior.last_level is not None and (own is None or own <= prior.last_level):
                        continue
                    prior = None
                else:
                    # U-e B undecided: a previous run's entry is kept as written until the venue
                    # shows whose it is; only the state is new, and only where it fits its level.
                    if prior.last_level is not None or state in NEEDS_REASON:
                        entries[key] = replace(prior, state=state, count=count, reason=reason)
                    continue
            level = None if prior is None else prior.last_level
            distance = None if prior is None else prior.distance
            offset = None if prior is None else prior.limit_offset
            if slot.distance is not None:
                distance = slot.distance.distance  # 17:43 ruling, point 1: beside the level.
                offset = slot.limit_offset  # Ruling (c): the offset beside it.
            start = slot.protection_start if slot.protection_start is not None else slot.opened_at
            if own is not None:
                level = start_level(level, own)
            if level is None and state not in NEEDS_REASON:
                continue
            entries[key] = Entry(
                level,
                start,
                position.signed_qty,
                now,
                self.run,
                state,
                count,
                reason,
                None if level is None else distance,
                None if level is None or distance is None else offset,
            )
        for instrument_id, reason in self.frozen.items():
            if instrument_id in self.slots:
                continue  # Its slot's entry above carries this record.
            positions = self.cache.positions_open(instrument_id=instrument_id, strategy_id=EXTERNAL)
            if len(positions) != 1 or positions[0].signed_qty <= 0:
                continue
            position = positions[0]
            key = (account, instrument_id)
            prior = self.book_entries.get(key)
            state, _, reason = self._holding_state(instrument_id, None)
            if prior is not None:
                # Not carried this run (P11): its fields stay as written; only its state is new.
                entries[key] = replace(prior, state=state, count=None, reason=reason)
                continue
            entries[key] = Entry(
                None, position.ts_opened, position.signed_qty, now, self.run, state, None, reason
            )
        for key, retired in self.retired.items():
            entries.setdefault(key, retired)  # Review F3: unless a new holding's entry replaced it.
        return entries

    def _save_levels(self, *, final: bool = False, step_ns: int | None = None) -> None:
        """Write the book when a holding's level or state changed (P10, item 4), every holding's
        at stop (P5), and the step's time at most once per step (item 4).

        Entries other runs wrote since this one read the book are re-read and kept; atomic
        replacement alone does not serialize two writers. A failed write is named once per kind,
        with the holdings it held, and never changes protection or the run's outcome.
        """
        if self.level_book_path is None or self.settings.account_label is None:
            return
        changed: dict[Key, Entry] = {}
        try:
            stepped = step_ns is not None and (
                self.last_step_ns is None or step_ns - self.last_step_ns >= STEP_NS
            )
            if stepped:
                self.last_step_ns = step_ns
            for key, entry in self._current_levels().items():
                written = self.book_entries.get(key)
                if final or written is None or _reported(written) != _reported(entry):
                    changed[key] = entry
            if not changed and not stepped:
                return
            # Review F5: the read, merge and replace hold the book's lock, so a writer sharing
            # the path can neither lose this run's entries nor have its own lost.
            with LevelBook.locked(self.level_book_path):
                on_disk = LevelBook.read(self.level_book_path, report=lambda _note: None)
                entries = {**self.book_entries, **on_disk, **changed}
                steps = LevelBook.last_steps(self.level_book_path)
                if self.last_step_ns is not None:
                    steps[self.settings.account_label] = self.last_step_ns
                LevelBook.write(self.level_book_path, entries, last_step_ns=steps)
            self.written_keys.update(k for k, e in changed.items() if e.run == self.run)
        except Exception as exc:  # noqa: BLE001 - persistence must not replace the run's outcome.
            kind = (type(exc).__name__, str(exc))
            if kind in self.write_failures:
                return  # Named once per kind; each later change, quote or step retries.
            self.write_failures.add(kind)
            held = "; ".join(
                f"{key[1]} level {entry.last_level} ({entry.state})"
                for key, entry in sorted(changed.items(), key=lambda kv: str(kv[0][1]))
            )
            self._say(
                f"level book write failed at {self.level_book_path}: {kind[0]}: {kind[1]}"
                f"{'; holdings: ' + held if held else ''}; "
                "protection and the run outcome unchanged; this kind is not named again"
            )
            return
        self.book_entries = entries
