"""The regular-session protect task: one trailing stop Alpaca holds for each long that no working sell
covers, on one paper account, then exit (`mise run paper:protect tests`, or `protect` for account C,
ADR 0142). A run that sends names the registered row its distance is planned on (`--row`, `ROWS`); there
is no default. `--plan-only` makes every read, names what each stop would be under every row, and sends
nothing. `--cancel` cancels the open stops of the protect lineage, every one or one symbol's, and nothing
else (`cancel_stops`): the way a stop that holds a long's shares is taken off before that long is sold.

Contracts, read 2026-11-12:

* Alpaca's trailing stop order (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Trailing Stop
  Orders"): `trail_price` is "a dollar value away from the highest water mark"; "Valid time-in-force
  values for trailing stop are "day" and "gtc""; "Trailing stop will not trigger outside of the regular
  market hours". https://docs.alpaca.markets/us/reference/postorder.md: `type` `trailing_stop` for
  equities; `trail_price` "this or `trail_percent` is required if type is `trailing_stop`"; `qty` "Can be
  fractionable for only market and day order types", so a GTC stop is for whole shares;
  `client_order_id` "<= 128 characters".
* alpaca-py at the pin `c42add7c0a70`: `TrailingStopOrderRequest` (`alpaca/trading/requests.py:624-670`,
  exactly one of `trail_price` and `trail_percent`), `TradingClient.submit_order` (`trading/client.py:90`),
  `get_order_by_client_id` (`:157`), `get_clock` (`:422-436`) and its `Clock` (`trading/models.py:410-424`);
  `StockHistoricalDataClient.get_stock_bars` and `get_stock_latest_quote` (`data/historical/stock.py:75`,
  `:163`) with `StockBarsRequest` and `StockLatestQuoteRequest` (`data/requests.py:85`, `:277`).
* ADR 0129 (`proposed`): a whole-share SELL, `GTC` for a holding that outlives the session, sent only
  while Alpaca's clock says the regular session is open. The value goes as the adapter puts a `PRICE`
  offset on the wire (`canon_alpaca.execution.venue_trail`). That record keeps the adapter's own stop to
  a long-only account; this task does not rest on the account's setting (the lead's ruling, 2026-11-13,
  ADR 0142): each stop is marked `sell_to_close` (alpaca-py's `OrderRequest.position_intent`,
  `alpaca/trading/requests.py:388`, `PositionIntent.SELL_TO_CLOSE`, `trading/enums.py:590-598`; ADR 0111)
  and is for no more than the shares Alpaca shows held and free in the send step (`_still`).
* Alpaca's smallest `trail_price`, 0.1% of the stock's price, read on paper on 2026-11-13 from a 422 and
  on no page of Alpaca's: a distance under it is raised to it (`venue_smallest`), since a refused stop
  would leave the long with none.
* The Mover's planning, called and not re-implemented: `Mover._entry_trail`, the trail as a fraction of
  the entry under the named row of `session_window.TRAIL_ARMS`: F2 the fixed trail, OWN1 and OWN3 the
  symbol's own ATR (`atr_k` ATRs of one-minute bars) over the spread floor (`spread_floor_m` quoted
  spreads, `quoted_spread`), which is held for the fixed row here too. The ATR rows carry the cap arm
  `own`: a found holding has no entry size left to choose, so its ATR or spread-floor distance stands
  up to `sizing.DISTANCE_CEILING`, including the stepped and venue-minimum distance; one over it is
  refused as `distance_over_ceiling` (the lead, 2026-11-13; ADR 0142). `_entry_trail(found_holding=True)`
  shares that planning without changing the Mover's entry sizing. The ATR rows were built for a fresh
  entry; no row is calibrated for a
  standing holding (all UNCALIBRATED, ADR 0088, ADR 0142); then the sizing unit's
  `sizing.sized_stop`, the one computation of the stop a long at `entry` is sized
  against at that fraction, so the distance is entry less that stop, as `Mover._buy` takes L. The entry is
  `price_step.entry_price` of the ask at the Mover's `entry_ticks`, as `_buy` prices
  one. The ATR is NautilusTrader's `AverageTrueRange` at the pin `d53849b216e2`, fed by `update_raw(high,
  low, close)`, which is what its `handle_bar` calls (`crates/indicators/src/volatility/atr.rs:84-86`),
  on alpaca-py's complete minute bars of the last `ATR_LOOKBACK`.

The run. It reads Alpaca's clock first and nothing else unless the regular session is open (`is_open`, as
`custody._part` reads it) with at least `CLOSE_MARGIN` left to its close: a stop Alpaca holds works only
there, and the session is read once. The time left is counted from before the clock was asked, and
each symbol checks it again before its reads and before its send, so no send starts with less than one
send's bound (`SEND_MARGIN`) to the close. It reads the account's `no_shorting` and says it, the
positions and the open orders, and refuses by name, with nothing sent, a listing of open orders at its
maximum (`ORDERS_PAGE_LIMIT`), which may not hold every sell. A short is named and left: no order and no
market data read for it. That a stop never opens or adds to a short rests on the holding and the free shares read in the send step, with the
milliseconds between that read and the send as its window, and on Alpaca holding the shares for a stop
that rests (read on paper, 2026-11-13: a second sell of them was refused with shorting allowed). Whether
Alpaca refuses a marked stop for more shares than are held where shorting is allowed is not shown, so the
mark alone is not what it rests on. For each long it sums what working sells leave to
fill (quantity less filled), by lineage (`custody.lineage`; a protect stop of an earlier run, `PROTECT`,
counts with ours): covered in full by any sells, it is named and left alone; covered in part by a foreign
or unmarked sell, it is named and its symbol left untouched, not even planned; so is a symbol with a sell
whose cover cannot be summed (one of any order class but `simple`, whose legs hold the same shares once,
or one pending cancel or replace); else the rest, in whole shares, gets one trailing stop at the symbol's
distance (above), planned on a quote no older than `QUOTE_MOST`. Just before its send the symbol's
position is read again (`get_open_position`), with Alpaca's own count of the shares no order holds
(`qty_available`): unless the holding is the one read and that many are free, nothing is sent and the
symbol is named, since the first reads age while earlier symbols are planned and sent. Each stop is one
attempt under a client order ID of the protect lineage (`custody.PROTECT_PREFIX`), with alpaca-py's
retries off (`custody._sending_once`); a refusal is named, and a lost answer is looked up once by its ID,
never sent again, and counts as sent only if Alpaca shows the order still working. It replaces, cancels
and sells nothing else, opens nothing, runs no window and does not wait for a stop to work. Plan-only
stops before the send: each long is named with what would be sent under every row, the distance as a
share of the price, about where the stop would open and what bound it, after the same reads, the send
step's included. A quote's age is counted when its symbol is planned: the clock's one read plus the time
the run has taken since. Each line is
printed as it happens, so a run that is stopped has already named what it sent; the task's stop signal
ends it as a failure by name (`custody._stop`). Exit 0 when each long is covered, 1 when a read or a send
failed or the run was stopped, 2 when a long is left uncovered by name, 3 when the run was refused by name.

A stop that rests holds its shares: Alpaca refuses another sell of them until the stop is canceled, the
holder's own included. `--cancel` (`cancel_stops`) cancels the open stops of the protect lineage, every one
or one symbol's, once each, and nothing else: no order is sent, and every other open order is named and left.
Only a stop that ended canceled with nothing filled counts as canceled; its exits are `cancel_task`'s.

Not checked against Alpaca, so UNVERIFIED until a paper run: that Alpaca takes a `trail_price` with
four decimals on a name under $1; how a GTC trailing stop elects at a later open (ADR 0129's gap); which
price Alpaca measures its smallest trail against; and what Alpaca does with a marked stop for more
shares than are held."""

from __future__ import annotations

import math
import signal
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from time import monotonic
from time import sleep
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.requests import TrailingStopOrderRequest
from canon_alpaca.execution import DEFINITIVE_SUBMIT_REFUSALS
from canon_alpaca.execution import ORDERS_PAGE_LIMIT
from canon_alpaca.execution import venue_trail
from canon_alpaca.providers import CONNECT_TIMEOUT_SECS
from canon_alpaca.providers import READ_TIMEOUT_SECS
from canon_alpaca.providers import counted
from canon_alpaca.providers import not_sent
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import TrailingOffsetType

from . import mover
from . import session_window
from . import sizing
from .custody import FOREIGN
from .custody import OURS
from .custody import PROTECT
from .custody import PROTECT_PREFIX
from .custody import Stopped
from .custody import _failure
from .custody import _look_up
from .custody import _read
from .custody import _sending_once
from .custody import _shown
from .custody import _stop
from .custody import _value
from .custody import describe
from .custody import lineage
from .custody import open_orders
from .price_step import entry_price
from .price_step import tick

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

__all__ = [
    "PROTECT_PREFIX",
    "ProtectReport",
    "cancel_stops",
    "cancel_task",
    "protect_longs",
    "protect_task",
]

FEED = DataFeed.SIP
"""The quote and bars the distance is planned on: sip, the consolidated NBBO the spread floor is measured
on (the lead, 2026-11-12: the third paper account runs on sip or not at all)."""
ATR_LOOKBACK = timedelta(minutes=60)
"""How far back the minute bars that warm the ATR are read; fewer than `atr_n` complete minutes in it
leave the ATR cold, and the Mover's planning then names its fixed fallback. UNCALIBRATED."""
ROWS = session_window.TRAIL_ARMS
"""The rows a stop's distance may be planned on: the Mover's registered trail arms, read from the registry
a window uses and never a figure of this task's own (the lead's ruling, 2026-11-13). F2 is the fixed
trail; OWN1 and OWN3 the ATR mode at one and three ATRs of one-minute bars over the spread floor. The
ATR rows were built for a fresh momentum entry, and no registered row is calibrated for a standing
holding: every one is UNCALIBRATED, and that is the open row. A run that sends names one; there is no
default."""


def planner_config(row: str) -> mover.MoverConfig:
    """The Mover's config for `row`, as registered. `max_notional` and `max_move` have no default and the
    planning reads neither. Nothing here sizes or enters: `row_distance` names a found holding to the
    shared planner and enforces the sizing ceiling for the ATR rows' cap arm `own`."""
    return mover.MoverConfig(max_notional=Decimal(1), max_move=Decimal(1), **ROWS[row])


PLANNER = planner_config(session_window.DEFAULT_ARM)
"""The rows the planning shares whatever the trail row: `atr_n`, `entry_ticks`, `spread_floor_m`."""
SEND_MARGIN = timedelta(seconds=math.ceil(CONNECT_TIMEOUT_SECS + READ_TIMEOUT_SECS))
"""The least time left to the close for one stop to be sent: the most one submit may take, the paper
client's own (connect, read) timeout (`session_window.paper_trading` mounts `providers.with_http_timeout`,
ADR 0039) with alpaca-py's retries off for the send (`_sending_once`). It is checked again before each
symbol's reads and just before its send. The bound is on each stall, not the whole exchange: the connect
wait is per address tried and the read wait is between bytes, and name resolution has none, so a send that
starts this near the close is most likely, not certainly, answered before it. What Alpaca does with a
stop that arrives after the close is UNVERIFIED."""
CLOSE_MARGIN = SEND_MARGIN
"""The least time left to the close for a run to start: one send's bound, since each send checks the
time again. It was 300 s, said to be the task's own bound, which is 10 minutes (`paper:protect`'s
`timeout`), so it held for no reason that stood, and refused a long bought in the last minutes before
the close (the lead's case of 2026-11-13); a run takes 2 to 4 s on paper (2026-11-13)."""


def _whole(span: timedelta) -> int:
    """Whole seconds, rounded down, so a time left is never said to be more than it is."""
    return math.floor(span.total_seconds())


QUOTE_MOST = timedelta(seconds=60)
"""The oldest latest quote a distance is planned on. An older one (a halt, a thin name) says nothing of
the spread now, and the spread is the floor. UNCALIBRATED."""
VENUE_SMALLEST = Decimal("0.001")
"""Alpaca's smallest `trail_price` as a share of the stock's price: a smaller one was refused on paper
with 422 (2026-11-13; observed, and on no page of Alpaca's read that day). Which price Alpaca measures
against is UNVERIFIED, so the floor here is this share of the planned entry, rounded up to the price
step, and one step over (`venue_smallest`)."""
SUMMABLE_CLASSES = ("simple", "")
"""The order classes whose sells each hold their own shares. A leg of any other class (`bracket`, `oco`,
`oto`) shares them with its sibling, so the legs cannot be added up from a flat listing."""
PENDING_OUT = ("pending_cancel", "pending_replace")
"""Statuses in which what a sell covers is about to change."""
RESTING = frozenset({"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled"})
"""The statuses in which a stop just sent counts as resting, of those Alpaca's order lifecycle lists
(orders-at-alpaca, "Order Lifecycle"): received or routed and not yet ended. Any other answer is
not shown to protect the long, a failure by name, never sent again: `suspended`, `stopped`,
`done_for_day` and a pending cancel or replace are open and do not rest as a stop; `replaced` and
`calculated` are not this stop working; and `held`, which alpaca-py's `OrderStatus` lists and Alpaca's
page does not describe, is undecided until a paper answer shows it, so it fails rather than counts."""
REFUSED = 3


@dataclass
class ProtectReport:
    """What the run found and did, by name. Each line is handed to `say` as it happens, when the run is
    given one, so a run stopped part way has already printed what it sent."""

    refused: str = ""
    sent: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    say: Callable[[str], None] | None = None

    @property
    def exit_code(self) -> int:
        if self.refused:
            return REFUSED
        if self.failed:
            return 1
        return 2 if self.unprotected else 0

    def note(self, kind: list[str] | None, line: str) -> None:
        """Keep `line` under its kind (None for a line that is none of them) and say it now."""
        if kind is not None:
            kind.append(line)
        self.said.append(line)
        if self.say is not None:
            self.say(line)

    def lines(self) -> list[str]:
        """Every line in the order it happened; a refusal alone; "no long held" when nothing was found."""
        if self.refused:
            return [f"refused: {self.refused}"]
        found = self.sent or self.planned or self.left_alone or self.unprotected or self.failed
        return [*self.said, *([] if found else ["no long held"])]


def _remaining(order: Order) -> Decimal | None:
    """What a working order has left to fill, or None when it shows no share quantity or no count of
    what filled: `Order.filled_qty` is optional (`alpaca/trading/models.py:283` at the pin), and what
    is not shown is not zero (`custody._shown`)."""
    qty, filled = _shown(order.qty), _shown(order.filled_qty)
    if qty is None or filled is None:
        return None
    return qty - filled


@dataclass
class Distance:
    """A symbol's trailing distance in dollars and where it came from, for the report."""

    price: Decimal
    plan: mover.TrailPlan
    floor: Decimal
    quote_age: timedelta
    distance: Decimal
    """What is sent: the Mover's distance, or Alpaca's smallest trail where that is the larger."""
    planned: Decimal
    """The Mover's own distance, before Alpaca's smallest trail."""
    row: str = ""
    bid: Decimal = Decimal(0)

    def source(self) -> str:
        plan = self.plan
        own = (
            f"the fixed row ({plan.trail:.0%} of the price)"
            if plan.bound == "fixed" and not plan.note
            else f"from {plan.bound}"
        )
        bound = (
            own
            if self.distance == self.planned
            else (
                f"bound by Alpaca's smallest trail ({VENUE_SMALLEST:.1%} of the price, up to the step "
                f"and one step over), over the Mover's {self.planned} {own}"
            )
        )
        opens = f"the bid {self.bid} less the distance; Alpaca sets it from its own mark"
        share = self.distance / self.price
        stepped = (
            f"{share:.4%} of {self.price}, over the {mover.TRAIL_CAP} cap by the price step"
            if share > mover.TRAIL_CAP and plan.trail <= mover.TRAIL_CAP
            else f"{share:.4%} of {self.price}"
        )
        parts = [
            f"row {self.row}",
            stepped,
            f"stop opens about {self.bid - self.distance} ({opens})",
            bound,
            f"trail {plan.trail:.6f} of entry {self.price}",
            f"spread {plan.spread}",
            f"floor {self.floor} ({PLANNER.spread_floor_m} spreads)",
            f"ATR {'cold' if plan.atr is None else f'{plan.atr:.6f}'}",
            f"quote {max(self.quote_age.total_seconds(), 0.0):.0f} s old",
        ]
        if plan.source:
            parts.append(plan.source)
        if plan.note:
            parts.append(plan.note)
        return ", ".join(parts)


def venue_smallest(price: Decimal) -> Decimal:
    """The least distance sent for a stock at `price`: `VENUE_SMALLEST` of it, rounded up to its price
    step, and one step over, so a rounding of Alpaca's own cannot put it under."""
    step = tick(price)
    return (price * VENUE_SMALLEST).quantize(step, rounding=ROUND_CEILING) + step


@dataclass
class Book:
    """What a symbol's distance is planned on, read once whatever the number of rows."""

    bid: Decimal
    ask: Decimal
    atr: AverageTrueRange
    age: timedelta
    read_at: float
    """`monotonic()` when the quote was read: its age is counted again just before the send."""

    def aged(self) -> timedelta:
        """The quote's age now: its age when read plus the time since."""
        return self.age + timedelta(seconds=monotonic() - self.read_at)


def read_book(data: Any, symbol: str, now: datetime) -> Book:
    """`symbol`'s latest quote and its ATR warmed on the complete minute bars of the last `ATR_LOOKBACK`:
    `ValueError` or `TypeError` naming why there is none."""
    asked = monotonic()
    found = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=FEED)
    )
    read_at = monotonic()
    quote = found.get(symbol) if isinstance(found, dict) else None
    if quote is None:
        raise ValueError(f"no latest {FEED.value} quote")
    stamped = getattr(quote, "timestamp", None)
    if not isinstance(stamped, datetime):
        raise ValueError(f"the latest {FEED.value} quote carries no time")  # noqa: TRY004 - named
    age = now + timedelta(seconds=read_at - asked) - stamped.astimezone(UTC)
    if age > QUOTE_MOST:
        raise ValueError(
            f"the latest {FEED.value} quote is {age.total_seconds():.0f} s old, over the "
            f"{QUOTE_MOST.total_seconds():.0f} s a spread floor is taken from"
        )
    bid = Decimal(str(getattr(quote, "bid_price", None) or 0))
    ask = Decimal(str(getattr(quote, "ask_price", None) or 0))
    if ask <= 0:
        raise ValueError(f"the latest {FEED.value} quote has no ask ({ask})")
    bars = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - ATR_LOOKBACK,
            end=now,
            feed=FEED,
        )
    )
    if not isinstance(bars, BarSet):
        raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
    atr = AverageTrueRange(PLANNER.atr_n)
    for bar in sorted(bars.data.get(symbol, []), key=lambda b: b.timestamp):
        if bar.timestamp + timedelta(minutes=1) <= now:  # complete minutes only
            atr.update_raw(bar.high, bar.low, bar.close)
    return Book(bid, ask, atr, age, read_at)


def row_distance(
    book: Book, symbol: str, row: str, planner: mover.Mover
) -> tuple[Decimal, Distance]:
    """`symbol`'s trailing distance in dollars under `row`, by the Mover's planning (module header),
    never under the spread floor or Alpaca's smallest trail (`venue_smallest`): `ValueError` naming why
    there is none. An own-cap holding keeps its distance up to `sizing.DISTANCE_CEILING`; the entry
    cap still applies to the other cap arms."""
    bid, ask = book.bid, book.ask
    instrument_id = InstrumentId.from_str(f"{symbol}.ALPACA")
    slot = mover.Slot(
        instrument_id,
        BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"),
        deque(),
        deque(),
        atr=book.atr,
    )
    price = entry_price(ask, PLANNER.entry_ticks)
    touch = SimpleNamespace(bid_price=Price.from_str(str(bid)), ask_price=Price.from_str(str(ask)))
    plan = planner._entry_trail(slot, price, touch, found_holding=True)
    if plan.spread is None:
        raise ValueError(
            f"no usable {FEED.value} spread (bid {bid}, ask {ask}), so the spread floor is not known"
        )
    # the fixed row carries no floor of its own; the spread floor is held for every row here
    floor = PLANNER.spread_floor_m * plan.spread / price if plan.floor is None else plan.floor
    own = planner.settings.trail_cap == "own"
    if own and plan.trail > sizing.DISTANCE_CEILING:
        raise ValueError(
            f"distance_over_ceiling: the trail {plan.trail} ({plan.bound}) is over the "
            f"{sizing.DISTANCE_CEILING} ceiling for a found holding"
        )
    distance = price - sizing.sized_stop(price, plan.trail)
    if distance <= 0:
        raise ValueError(f"the distance {distance} at {price} leaves no room for a trail")
    if distance != distance.quantize(tick(price)):
        raise ValueError(f"the distance {distance} is off the step {tick(price)} of {price}")
    if distance / price < floor:
        raise ValueError(f"the distance {distance} is under the spread floor at {price}")
    sent = max(distance, venue_smallest(price))
    if own and sent > price * sizing.DISTANCE_CEILING:
        raise ValueError(
            f"distance_over_ceiling: the distance {sent} ({plan.bound}, after the price step and "
            f"Alpaca's smallest trail) is over the {sizing.DISTANCE_CEILING} ceiling at {price}"
        )
    return sent, Distance(
        price, plan, PLANNER.spread_floor_m * plan.spread, book.age, sent, distance, row, bid
    )


def plan_distance(
    data: Any, symbol: str, now: datetime, row: str, planner: mover.Mover | None = None
) -> tuple[Decimal, Distance]:
    """`read_book` then `row_distance`: one symbol's distance under one row."""
    planner = planner or mover.Mover(planner_config(row))
    return row_distance(read_book(data, symbol, now), symbol, row, planner)


class _Protect:
    """One run against `trading` and `data` (a market data client, or a callable that builds one)."""

    def __init__(
        self,
        trading: Any,
        data: Any,
        say: Callable[[str], None] | None = None,
        plan_only: bool = False,
        row: str | None = None,
    ) -> None:
        self.trading = trading
        self.data = data
        self.plan_only = plan_only
        self.row = row
        self.planners: dict[str, mover.Mover] = {}
        self.began = 0.0
        """`monotonic()` just before the clock is asked: a later symbol is planned at the clock's time
        plus what the run has taken since, the clock read's own wait counted."""
        self.left = timedelta(0)
        """The time to the close at the clock's read; what is left at a send is this less what the run
        has taken since."""
        self.client: Any = None
        self.report = ProtectReport(say=say)
        self.stamp = ""

    def run(self) -> ProtectReport:
        report = self.report
        names = ", ".join(ROWS)
        if self.row is not None and self.row not in ROWS:
            report.refused = f"no registered row named {self.row!r}: the rows are {names}; nothing was read or sent"
            return report
        if not self.plan_only and self.row is None:
            report.refused = (
                f"a run that sends names no row: name one of {names} (there is no default), or run "
                "plan-only to see each; nothing was read or sent"
            )
            return report
        self.began = monotonic()
        clock = _read("Alpaca's clock", self.trading.get_clock, Clock)
        if not clock.is_open or clock.next_close <= clock.timestamp:
            report.refused = (
                "outside the regular session: Alpaca's clock says the market is closed (next open "
                f"{clock.next_open.astimezone(UTC):%Y-%m-%d %H:%M} UTC), and a stop Alpaca holds works "
                "only in the regular session; nothing else was read or sent"
            )
            return report
        self.left = clock.next_close - clock.timestamp
        if (to_close := self._to_close()) < CLOSE_MARGIN:
            report.refused = (
                f"too near the close: {_whole(to_close)} s to the close, under the "
                f"{_whole(CLOSE_MARGIN)} s one send may take, and no stop is sent after "
                "it; nothing else was read or sent"
            )
            return report
        report.note(None, "session: regular (Alpaca's clock)")
        now = clock.timestamp.astimezone(UTC)
        self.stamp = now.strftime("%Y%m%dT%H%M%SZ")
        configuration = _read(
            "the account configuration",
            self.trading.get_account_configurations,
            AccountConfiguration,
        )
        # The setting is said, never relied on (the lead's ruling, 2026-11-13): what keeps a stop from
        # opening or adding to a short is the holding and the free shares read in the send step
        # (`_still`). The mark tells Alpaca what the sell is for; it is not what this rests on.
        if configuration.no_shorting:
            report.note(
                None, "account: no_shorting true (long-only); each stop is marked sell_to_close"
            )
        else:
            report.note(
                None,
                "account: no_shorting false, so a sell here could open a short: each stop is marked "
                "sell_to_close and sent only for shares Alpaca shows held and free in the send step",
            )
        held = _read("the positions", lambda: session_window.positions(self.trading), dict)
        orders = open_orders(self.trading)
        if len(orders) >= ORDERS_PAGE_LIMIT:
            report.refused = (
                f"{len(orders)} open orders, the most one listing returns, so a covering sell may be "
                "missing from it; nothing was sent"
            )
            return report
        n = 0
        for symbol, qty in sorted(held.items()):
            if qty < 0:
                report.note(
                    report.left_alone,
                    f"left alone {symbol}: a short of {-qty}, which this task neither covers nor "
                    "protects; nothing sent for it",
                )
            if qty <= 0:
                continue
            rest = self._uncovered(symbol, qty, orders)
            if rest is None:
                continue
            whole, ours = rest
            n += 1
            at = now + timedelta(seconds=monotonic() - self.began)
            self._protect(symbol, whole, qty, ours, at, n)
        return report

    def _uncovered(
        self, symbol: str, held: Decimal, orders: list[Order]
    ) -> tuple[Decimal, Decimal] | None:
        """The whole shares of `symbol` no working sell covers and what ours cover, or None when the long
        is named and left (covered in full, in part by a foreign sell, by a sell whose cover cannot be
        summed, or under one share)."""
        report = self.report
        sells = [o for o in orders if o.symbol == symbol and _value(o.side) == "sell"]
        cover = {OURS: Decimal(0), FOREIGN: Decimal(0)}
        for order in sells:
            left = _remaining(order)
            why = ""
            if left is None:
                why = "shows no share quantity or no fill count, so what it covers is not known"
            elif (_value(order.order_class) or "") not in SUMMABLE_CLASSES:
                why = (
                    f"is part of a {_value(order.order_class)} order, whose legs hold the same shares "
                    "once, so what it covers cannot be summed"
                )
            elif _value(order.status) in PENDING_OUT:
                why = f"is {_value(order.status)}, so what it covers is about to change; run again"
            elif _value(order.status) not in RESTING:
                why = (
                    f"is {_value(order.status)}, which is not a sell that rests, so what it covers "
                    "is not established"
                )
            if why:
                report.note(
                    report.unprotected,
                    f"unprotected {symbol}: {held} held; {describe(order)} {why}; left untouched",
                )
                return None
            assert left is not None
            mine = lineage(order.client_order_id) in (OURS, PROTECT)
            cover[OURS if mine else FOREIGN] += max(left, Decimal(0))
        named = "; ".join(describe(o) for o in sells)
        if cover[OURS] + cover[FOREIGN] >= held:
            report.note(
                report.left_alone,
                f"left alone {symbol}: {held} held, fully covered by working sells ({named})",
            )
            return None
        if cover[FOREIGN]:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {held} held, covered in part by a foreign order ({named}); "
                "named and left untouched",
            )
            return None
        rest = held - cover[OURS]
        whole = rest.to_integral_value(rounding=ROUND_FLOOR)
        if whole < 1:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {rest} of {held} uncovered, under one whole share, and a GTC "
                "trailing stop takes whole shares only",
            )
            return None
        if rest != whole:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {rest - whole} of {held} uncovered, a fraction a GTC trailing "
                "stop cannot take; the whole shares are below",
            )
        return whole, cover[OURS]

    def _still(self, symbol: str, whole: Decimal, held: Decimal) -> str:
        """ "" when `symbol` is still the holding that was read with `whole` shares no order holds, by one
        read of the position just before the send; else why not."""
        try:
            position = counted(self.trading.get_open_position)(symbol)
        except APIError as e:
            if (
                e.status_code == 404
            ):  # alpaca-py: "Throws an APIError if the position does not exist"
                return "no longer held"
            raise
        if _value(position.side) != "long":
            return f"no longer a long (Alpaca shows it {_value(position.side)})"
        now = Decimal(str(position.qty))
        if now != held:
            return f"{held} held at the read and {now} now"
        if position.qty_available in (None, ""):
            return "Alpaca's position does not show the quantity no order holds"
        free = Decimal(str(position.qty_available))
        if free < whole:
            return f"only {free} of {held} not held by an order, and the stop was for {whole}"
        return ""

    def _request(
        self, symbol: str, whole: Decimal, distance: Decimal, client_order_id: str
    ) -> TrailingStopOrderRequest:
        return TrailingStopOrderRequest(
            symbol=symbol,
            qty=float(whole),
            side=AlpacaOrderSide.SELL,
            time_in_force=AlpacaTimeInForce.GTC,
            trail_price=venue_trail(distance, TrailingOffsetType.PRICE),
            position_intent=PositionIntent.SELL_TO_CLOSE,
            client_order_id=client_order_id,
        )

    def _to_close(self) -> timedelta:
        """What is left to the close now: the clock's figure less what the run has taken since it asked."""
        return self.left - timedelta(seconds=monotonic() - self.began)

    def _too_near(self, symbol: str, held: Decimal, when: str) -> bool:
        """Name `symbol` unprotected, nothing sent, when less than one send's bound is left to the close."""
        if (to_close := self._to_close()) >= SEND_MARGIN:
            return False
        self.report.note(
            self.report.unprotected,
            f"unprotected {symbol}: {held} held, no stop sent: {_whole(to_close)} s to the close "
            f"{when}, under the {_whole(SEND_MARGIN)} s one send may take, and a stop Alpaca holds "
            "works only in the regular session",
        )
        return True

    def _protect(
        self, symbol: str, whole: Decimal, held: Decimal, ours: Decimal, now: datetime, n: int
    ) -> None:
        report = self.report
        client_order_id = f"{PROTECT_PREFIX}{self.stamp}-{n}-{uuid4().hex[:8]}"
        rows = tuple(ROWS) if self.plan_only else (self.row or "",)
        planned: dict[str, tuple[TrailingStopOrderRequest, Decimal, Distance]] = {}
        refused: dict[str, str] = {}
        if self._too_near(symbol, held, "before its reads"):
            return
        try:
            if self.client is None:
                self.client = self.data() if callable(self.data) else self.data
            book = read_book(self.client, symbol, now)
        except Stopped:
            raise  # the task's stop signal ends the run; it is not a symbol's missing distance
        except Exception as e:  # noqa: BLE001 - named, and the long is left unprotected
            why = str(e) if isinstance(e, (ValueError, TypeError)) else _failure(e)
            report.note(
                report.unprotected, f"unprotected {symbol}: {held} held, no distance: {why}"
            )
            return
        for row in rows:
            try:
                planner = self.planners.get(row)
                if planner is None:
                    planner = self.planners[row] = mover.Mover(planner_config(row))
                distance, plan = row_distance(book, symbol, row, planner)
                planned[row] = (
                    self._request(symbol, whole, distance, client_order_id),
                    distance,
                    plan,
                )
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001 - named, and no stop goes out under this row
                refused[row] = str(e) if isinstance(e, (ValueError, TypeError)) else _failure(e)
        if not planned:
            why = "; ".join(f"under {row}: {said}" for row, said in refused.items())
            report.note(report.unprotected, f"unprotected {symbol}: {held} held, no distance {why}")
            return
        changed = self._still(symbol, whole, held)
        if changed:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {held} held, no stop sent: {changed}; the holding or its "
                "orders changed during the run; run again",
            )
            return
        if self._too_near(symbol, held, "by the send"):
            return
        if (aged := book.aged()) > QUOTE_MOST:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {held} held, no stop sent: the {FEED.value} quote its "
                f"distance was planned on is {aged.total_seconds():.0f} s old by the send, over the "
                f"{QUOTE_MOST.total_seconds():.0f} s a spread floor is taken from; run again",
            )
            return
        for _request, _distance, plan in planned.values():
            plan.quote_age = (
                aged  # the age a line prints is the one just checked, not the first read's
            )

        def what(distance: Decimal, plan: Distance) -> str:
            return (
                f"trailing stop sell {whole} {symbol}, GTC, sell_to_close, distance {distance} "
                f"({plan.source()}), {held} held"
                + (f", {ours} covered by ours" if ours else "")
                + f", lineage {lineage(client_order_id)} {client_order_id}"
            )

        if self.plan_only:
            for row in rows:
                if row in planned:
                    _request, distance, plan = planned[row]
                    report.note(
                        report.planned, f"would send {symbol} under {row}: {what(distance, plan)}"
                    )
                else:
                    report.note(None, f"no stop for {symbol} under {row}: {refused[row]}")
            return
        request, distance, plan = planned[rows[0]]
        said = what(distance, plan)
        try:
            with _sending_once(self.trading):
                order = counted(self.trading.submit_order)(request)
        except Stopped, KeyboardInterrupt:
            report.note(
                report.failed,
                f"failed {symbol}: {said}: the run was stopped during the send; Alpaca may yet show "
                "it working (mise run paper:account)",
            )
            raise
        except Exception as e:  # noqa: BLE001 - every kind ends in the look-up by the stop's own ID
            # alpaca-py's own failures (`session_window.FAILED`) and any other kind alike: unless
            # Alpaca refused it or nothing was sent, the stop may rest, so it is looked up and named
            is_refusal = isinstance(e, APIError) and e.status_code in DEFINITIVE_SUBMIT_REFUSALS
            if is_refusal or not_sent(e):
                report.note(report.failed, f"failed {symbol}: {said}: not placed: {_failure(e)}")
                return
            try:
                found = _look_up(self.trading, client_order_id)
            except (Exception, KeyboardInterrupt) as looked:
                # whatever ends the look-up, the task's stop signal and Ctrl-C included, the stop
                # that may rest at Alpaca is named first
                stopped = isinstance(looked, (Stopped, KeyboardInterrupt))
                how = "was stopped" if stopped else f"failed ({_failure(looked)})"
                report.note(
                    report.failed,
                    f"failed {symbol}: {said}: no answer ({_failure(e)}), and the look-up by its ID "
                    f"{how}; Alpaca may yet show it working (mise run paper:account)",
                )
                if stopped:
                    raise
                return
            if found is None:
                report.note(
                    report.failed,
                    f"failed {symbol}: {said}: no answer and not found by its ID ({_failure(e)}); "
                    "Alpaca may yet show it working (mise run paper:account)",
                )
                return
            if _value(found.status) in session_window.TERMINAL:
                report.note(
                    report.failed,
                    f"failed {symbol}: {said}: no answer ({_failure(e)}), found by its ID but ended: "
                    f"{_value(found.status)}; the long has no stop",
                )
                return
            if _value(found.status) not in RESTING:
                report.note(
                    report.failed,
                    f"failed {symbol}: {said}: no answer ({_failure(e)}), found by its ID as "
                    f"{_value(found.status)}, which is not a stop that rests; it is not sent again "
                    "(mise run paper:account)",
                )
                return
            report.note(
                report.sent,
                f"sent {symbol}: {said}: no answer ({_failure(e)}), found by its ID: "
                f"{_value(found.status)}",
            )
            return
        if not isinstance(order, Order):
            report.note(
                report.failed,
                f"failed {symbol}: {said}: the submit answered a {type(order).__name__}, not an Order, "
                "so no stop is shown; Alpaca may yet show it working (mise run paper:account)",
            )
            return
        answered = _value(order.status)
        if answered in session_window.TERMINAL:
            report.note(
                report.failed,
                f"failed {symbol}: {said}: answered {answered}; the long has no stop",
            )
            return
        if answered not in RESTING:
            report.note(
                report.failed,
                f"failed {symbol}: {said}: answered {answered}, which is not a stop that rests; it "
                "is not sent again (mise run paper:account)",
            )
            return
        report.note(report.sent, f"sent {symbol}: {said}: answered {answered}")


def protect_longs(
    trading: TradingClient,
    data: Any,
    say: Callable[[str], None] | None = None,
    plan_only: bool = False,
    row: str | None = None,
) -> ProtectReport:
    """The run (module header). `data` is an alpaca-py `StockHistoricalDataClient`, or a callable that
    builds one; it is asked only once a long needs a stop. `say` is given each line as it happens.
    `plan_only` makes every read of a sending run and names what each stop would be under every
    registered row, and sends none. A run that sends names its `row` (`ROWS`); there is no default."""
    run = _Protect(trading, data, say, plan_only, row)
    try:
        return run.run()
    except Exception as e:  # noqa: BLE001 - named, with what was sent before it, and exits 1
        run.report.note(run.report.failed, f"failed part way: {_failure(e)}; nothing more was sent")
        return run.report


def _say(line: str) -> None:
    print(line, flush=True)


NO_SUCH_STOP = 2
"""The cancel's exit when a symbol was named and no protect stop is open in it: not a success, since a
symbol typed wrong would otherwise read as done."""
LISTINGS = 2
"""How often the cancel reads the open listing, a second apart, before it says the listing showed no
protect stop: a stop sent a moment before may not be listed on the first read. One the listing shows on
neither read is not canceled, and the line says what the listing showed, not that none is open."""
CANCEL_READS = 20
"""The most reads, one a second, of a stop after its cancel until it has ended: Alpaca's open-orders
listing lags a cancel by seconds (seen on paper, 2026-11-13), so each stop is read by its own ID."""


class _Ended(Exception):
    """The run ended inside one stop's cancel, and that stop's line is already said."""


def _named(order: Order) -> str:
    """An order by name for a line of the cancel: a protect stop without the close-only form's words,
    since this form does cancel one."""
    return describe(order, never_canceled=False)


def _part_filled(order: Order) -> str:
    """What the last read of a stop showed filled, for a line that cannot say how it ended: a fill
    already seen is said, never dropped."""
    filled = _shown(order.filled_qty)
    return f"; {filled} of its {order.qty} shares filled at its last read" if filled else ""


def _cancel_one(
    trading: TradingClient, order: Order, report: ProtectReport, pause: Callable[[float], None]
) -> None:
    """One protect stop's cancel and its end, by name. The cancel goes out once. Alpaca answers 204
    when it takes it and 422 when "the order status is not cancelable" (Delete Order by ID). Whatever
    the answer, a refusal or a lost one included, the stop's own read decides, until it has ended and
    `CANCEL_READS` at most; only a connect timeout, which proves nothing was sent (`not_sent`), is a
    failure with no read. A stop that ended canceled with nothing filled is canceled; one that ended
    canceled with shares filled, or ended another way, or is still working after the reads, is a
    failure that says so. Whatever ends the run inside this cancel (a failure of any kind, a failed
    read, the task's stop signal, Ctrl-C) is raised with the stop named first (`_Ended`; Ctrl-C as
    itself)."""
    named = order.symbol or "?"
    answered = ""
    try:
        with _sending_once(trading):
            counted(trading.cancel_order_by_id)(order.id)
    except APIError as e:
        answered = f"the cancel was answered {e.status_code}"
    except (Exception, KeyboardInterrupt) as e:
        stopped = isinstance(e, (Stopped, KeyboardInterrupt))
        if not stopped and isinstance(e, session_window.FAILED):
            if not_sent(e):
                report.note(
                    report.failed,
                    f"failed {named}: the cancel of {_named(order)} was not sent: {_failure(e)}; "
                    "it is still working (mise run paper:account)",
                )
                return
            answered = f"the cancel's answer was lost ({_failure(e)})"
        else:
            why = "the run was stopped during" if stopped else f"{_failure(e)} inside"
            report.note(
                report.failed,
                f"failed {named}: {why} the cancel of {_named(order)}; its cancel may be at "
                "Alpaca, and it may yet show working (mise run paper:account)",
            )
            if isinstance(e, KeyboardInterrupt):
                raise
            raise _Ended from e
    said = f" ({answered})" if answered else ""
    found = order
    try:
        for read in range(CANCEL_READS):
            found = _read(
                "the stop's own read",
                lambda order_id=order.id: trading.get_order_by_id(order_id),
                Order,
            )
            if _value(found.status) in session_window.TERMINAL:
                break
            if read < CANCEL_READS - 1:
                pause(1)
    except (Exception, KeyboardInterrupt) as e:  # the stop in flight is named before the run ends
        # `_read` wraps what its call raised, the task's stop signal among them
        stopped = isinstance(e, (Stopped, KeyboardInterrupt)) or isinstance(e.__cause__, Stopped)
        why = "the task was stopped" if stopped else _failure(e)
        report.note(
            report.failed,
            f"failed {named}: {_named(order)}: its cancel was sent{said} and its end was not "
            f"read: {why}{_part_filled(found)}; it may yet show working (mise run paper:account)",
        )
        if isinstance(e, KeyboardInterrupt):
            raise
        raise _Ended from e
    status = _value(found.status)
    filled = _shown(found.filled_qty)
    if status == "canceled" and filled == 0:
        report.note(report.sent, f"canceled {named}: {_named(found)}{said}")
    elif status == "canceled" and filled is None:
        report.note(
            report.failed,
            f"failed {named}: {_named(found)}{said}: Alpaca shows no count of what filled before "
            "the cancel ended it, so a clean cancel is not established (mise run paper:account)",
        )
    elif status == "canceled":
        report.note(
            report.failed,
            f"failed {named}: {_named(found)}: {filled} of its {found.qty} shares filled before the "
            f"cancel ended it{said}; the holding changed (mise run paper:account)",
        )
    elif status in session_window.TERMINAL:
        report.note(
            report.failed,
            f"failed {named}: {_named(order)} ended {status} before the cancel reached "
            f"it{said}; nothing was canceled, and the holding may have changed "
            "(mise run paper:account)",
        )
    else:
        report.note(
            report.failed,
            f"failed {named}: {_named(order)} is still {status} after {CANCEL_READS} reads{said}"
            f"{_part_filled(found)}; the cancel was sent once and is not sent again "
            "(mise run paper:account)",
        )


def cancel_stops(
    trading: TradingClient,
    say: Callable[[str], None] | None = None,
    symbol: str | None = None,
    plan_only: bool = False,
    pause: Callable[[float], None] = sleep,
) -> ProtectReport:
    """Cancel every open order of the protect lineage (`custody.PROTECT`), in `symbol` alone when one is
    named, and nothing else: one cancel each with alpaca-py's retries off (`cancel_order_by_id`,
    `alpaca/trading/client.py:217` at the pin), then the order's own read (`_cancel_one`). A stop that
    ended canceled unfilled is `sent`'s counterpart here, `left_alone` holds every order that is not a
    protect stop or not in the symbol, and `failed` every other end, each by name. A run that ends
    inside one stop's cancel, Ctrl-C included, names that stop and each protect stop it did not reach.
    A foreign trailing-stop sell is left alone like any foreign order, and its line says it may be a
    protect stop replaced outside this task (that a replacement carries an ID this task did not
    write is UNVERIFIED: no replace was run). It
    sends no order of any kind and asks for no session; that Alpaca takes a trailing stop's cancel
    outside the regular session is not shown, UNVERIFIED. `plan_only` names what it would cancel and
    cancels nothing."""
    report = ProtectReport(say=say)
    stops: list[Order] = []
    try:
        orders: list[Order] = []
        for listing in range(LISTINGS):
            orders = open_orders(trading)
            if len(orders) >= ORDERS_PAGE_LIMIT:
                report.refused = (
                    f"{len(orders)} open orders, the most one listing returns, so a protect stop "
                    "may be missing from it; nothing was canceled"
                )
                return report
            stops = [
                order
                for order in orders
                if lineage(order.client_order_id) == PROTECT
                and (not symbol or order.symbol == symbol)
            ]
            if stops or listing == LISTINGS - 1:
                break
            pause(1)  # a stop sent a moment ago may not be listed yet: one listing is no "none"
        for order in orders:
            named = order.symbol or "?"
            found = lineage(order.client_order_id)
            if found == PROTECT and (not symbol or order.symbol == symbol):
                continue
            line = f"left alone {named}: {_named(order)}"
            kind = _value(order.type or order.order_type)
            outside = found not in (OURS, PROTECT)
            if outside and kind == "trailing_stop" and _value(order.side) == "sell":
                line += (
                    "; it may be a protect stop replaced outside this task, which still holds "
                    "its shares and is its maker's to cancel"
                )
            report.note(report.left_alone, line)
        while stops:
            order = stops.pop(0)
            if plan_only:
                report.note(report.planned, f"would cancel {order.symbol or '?'}: {_named(order)}")
            else:
                _cancel_one(trading, order, report, pause)
    except (Exception, KeyboardInterrupt) as e:  # named, with what was canceled before it
        if not isinstance(e, (_Ended, KeyboardInterrupt)):
            report.note(report.failed, f"failed part way: {_failure(e)}; nothing more was canceled")
        for order in stops:
            report.note(
                report.failed,
                f"not reached {order.symbol or '?'}: {_named(order)}; no cancel was sent for it",
            )
        if isinstance(e, KeyboardInterrupt):
            raise
    return report


def cancel_task(
    trading: TradingClient | None = None,
    symbol: str | None = None,
    plan_only: bool = False,
    pause: Callable[[float], None] = sleep,
) -> int:
    """`mise run paper:protect <account> --cancel`: run `cancel_stops`, printing each line as it
    happens. Exit 0 when every protect stop named ended canceled with nothing filled (or none was open
    and no symbol was named), 1 when a cancel or a read failed or a stop did not end so, 2 when a symbol
    was named and no protect stop is open in it (`NO_SUCH_STOP`), 3 when the run was refused by name."""
    before = signal.signal(signal.SIGTERM, _stop)
    try:
        try:
            trading = trading or session_window.paper_trading()
        except Exception as e:  # noqa: BLE001 - named, and nothing was read or canceled
            print(
                f"paper:protect: no paper client: {_failure(e)}; nothing was canceled",
                file=sys.stderr,
            )
            return 1
        report = cancel_stops(trading, _say, symbol, plan_only, pause)
    finally:
        signal.signal(signal.SIGTERM, before)
    if report.refused:
        print(f"refused: {report.refused}", file=sys.stderr)
        return report.exit_code
    nothing = not (report.sent or report.planned or report.failed)
    if nothing and symbol:
        _say(f"the open listing showed no protect stop in {symbol} ({LISTINGS} reads)")
    elif nothing:
        _say(f"the open listing showed no protect stop ({LISTINGS} reads)")
    if plan_only:
        _say("plan only: nothing was canceled")
    return NO_SUCH_STOP if nothing and symbol else report.exit_code


def _refuse(why: str) -> int:
    print(f"refused: {why}; nothing was read or sent", file=sys.stderr)
    return REFUSED


def task_from_flags(
    plan_only: str,
    row: str,
    cancel: str = "false",
    symbol: str = "",
    trading: TradingClient | None = None,
    data: Any = None,
    pause: Callable[[float], None] = sleep,
) -> int:
    """`paper:protect`'s flags as the task hands them over, as text: plan-only and cancel are each
    exactly `true` or `false` (mise gives a passed switch `true`, and the task gives `false` for one not
    passed), and anything else is refused by name with nothing read, so a value not known here never
    becomes a send or a cancel. The row is a registered name, or empty for none. A cancel takes no row,
    and only a cancel takes a symbol, which is plain capital letters."""
    for name, value in (("plan-only", plan_only), ("cancel", cancel)):
        if value not in ("true", "false"):
            return _refuse(f"{name} reads {value!r}, which is neither true nor false")
    if symbol and not (symbol.isascii() and symbol.isalpha() and symbol.isupper()):
        return _refuse(f"the symbol {symbol!r} is not plain capital letters")
    if cancel == "true":
        if row:
            return _refuse(f"a cancel takes no row, and {row!r} was named")
        return cancel_task(trading, symbol or None, plan_only == "true", pause)
    if symbol:
        return _refuse("a symbol is named only with --cancel; a send covers every long it finds")
    return protect_task(trading, data, plan_only == "true", row or None)


def protect_task(
    trading: TradingClient | None = None,
    data: Any = None,
    plan_only: bool = False,
    row: str | None = None,
) -> int:
    """`mise run paper:protect tests`: run `protect_longs`, printing each line as it happens; the exit
    code is the report's (module header): a read that fails, or anything else part way, is named and
    exits 1. SIGTERM, which the task's `timeout` sends before its KILL, ends the run the same way, as
    `custody.close_task` handles it; Ctrl-C ends it at once, with every line so far already printed,
    a stop whose send it broke off among them."""
    before = signal.signal(signal.SIGTERM, _stop)
    try:
        try:
            trading = trading or session_window.paper_trading()
        except Exception as e:  # noqa: BLE001 - named, and nothing was read or sent
            print(
                f"paper:protect: no paper client: {_failure(e)}; nothing was sent", file=sys.stderr
            )
            return 1
        report = protect_longs(trading, data or session_window.window_data, _say, plan_only, row)
    finally:
        signal.signal(signal.SIGTERM, before)
    if report.refused:
        print(f"refused: {report.refused}", file=sys.stderr)
        return report.exit_code
    if not (
        report.sent or report.planned or report.left_alone or report.unprotected or report.failed
    ):
        _say("no long held")
    if plan_only:
        _say("plan only: nothing was sent")
    return report.exit_code
