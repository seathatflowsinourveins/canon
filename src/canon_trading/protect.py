"""The regular-session protect task: one trailing stop Alpaca holds for each long that no working sell
covers, on one paper account, then exit (`mise run paper:protect tests`, or `protect` for account C,
ADR 0142). A run that sends names the registered row its distance is planned on (`--row`, `ROWS`); there
is no default. `--plan-only` makes every read, names what each stop would be under every row, and sends
nothing.

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
  `own`, which the Mover lets stand only for an entry it sizes: nothing is sized here, so a distance over
  the 0.02 cap is refused by name. The ATR rows were built for a fresh entry; no row is calibrated for a
  standing holding (all UNCALIBRATED, ADR 0088, ADR 0142); then the sizing unit's
  `sizing.sized_stop`, the one computation of the stop a long at `entry` is sized
  against at that fraction, so the distance is entry less that stop, as `Mover._buy` takes L. The entry is
  `price_step.entry_price` of the ask at the Mover's `entry_ticks`, as `_buy` prices
  one. The ATR is NautilusTrader's `AverageTrueRange` at the pin `d53849b216e2`, fed by `update_raw(high,
  low, close)`, which is what its `handle_bar` calls (`crates/indicators/src/volatility/atr.rs:84-86`),
  on alpaca-py's complete minute bars of the last `ATR_LOOKBACK`.

The run. It reads Alpaca's clock first and nothing else unless the regular session is open (`is_open`, as
`custody._part` reads it) with at least `CLOSE_MARGIN` left to its close: a stop Alpaca holds works only
there, the session is read once, and the task's own time bound is that margin, so no stop goes out after
the close. It reads the account's `no_shorting` and says it, the positions and the open orders, and
refuses by name, with nothing sent, a listing of open orders at its maximum (`ORDERS_PAGE_LIMIT`), which
may not hold every sell. A short is named and left: no order and no market data read for it. That a stop
never opens or adds to a short rests on the holding and the free shares read in the send step, with the
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
holder's own included, and nothing here cancels a stop.

Not checked against Alpaca, so UNVERIFIED until a paper run: that Alpaca takes a `trail_price` with
four decimals on a name under $1; how a GTC trailing stop elects at a later open (ADR 0129's gap); which
price Alpaca measures its smallest trail against; and what Alpaca does with a marked stop for more
shares than are held."""

from __future__ import annotations

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
from .custody import _stop
from .custody import _value
from .custody import describe
from .custody import lineage
from .custody import open_orders
from .price_step import entry_price
from .price_step import tick

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

__all__ = ["PROTECT_PREFIX", "ProtectReport", "protect_longs", "protect_task"]

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
    planning reads neither; nothing here sizes or enters, so the ATR rows' cap arm `own`, which the Mover
    lets stand only for an entry it sizes, refuses a distance over the cap by name."""
    return mover.MoverConfig(max_notional=Decimal(1), max_move=Decimal(1), **ROWS[row])


PLANNER = planner_config(session_window.DEFAULT_ARM)
"""The rows the planning shares whatever the trail row: `atr_n`, `entry_ticks`, `spread_floor_m`."""
CLOSE_MARGIN = timedelta(seconds=300)
"""The least time left to the close for a run to start: the task's own bound (`paper:protect`'s
`timeout`), so a run that began in the regular session ends in it."""
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
    """What a working order has left to fill, or None when it names no share quantity."""
    if order.qty in (None, ""):
        return None
    filled = Decimal(str(order.filled_qty)) if order.filled_qty not in (None, "") else Decimal(0)
    return Decimal(str(order.qty)) - filled


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
    read_at = monotonic()
    found = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=FEED)
    )
    quote = found.get(symbol) if isinstance(found, dict) else None
    if quote is None:
        raise ValueError(f"no latest {FEED.value} quote")
    stamped = getattr(quote, "timestamp", None)
    if not isinstance(stamped, datetime):
        raise ValueError(f"the latest {FEED.value} quote carries no time")  # noqa: TRY004 - named
    age = now - stamped.astimezone(UTC)
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
    there is none, the Mover's own refusal of a distance its cap forbids included."""
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
    plan = planner._entry_trail(slot, price, touch)  # ValueError: a floor or distance over the cap
    if plan.spread is None:
        raise ValueError(
            f"no usable {FEED.value} spread (bid {bid}, ask {ask}), so the spread floor is not known"
        )
    # the fixed row carries no floor of its own; the spread floor is held for every row here
    floor = PLANNER.spread_floor_m * plan.spread / price if plan.floor is None else plan.floor
    distance = price - sizing.sized_stop(price, plan.trail)
    if distance <= 0:
        raise ValueError(f"the distance {distance} at {price} leaves no room for a trail")
    if distance != distance.quantize(tick(price)):
        raise ValueError(f"the distance {distance} is off the step {tick(price)} of {price}")
    if distance / price < floor:
        raise ValueError(f"the distance {distance} is under the spread floor at {price}")
    sent = max(distance, venue_smallest(price))
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
        """`monotonic()` at the clock's read: a later symbol is planned at the clock's time plus what
        the run has taken since."""
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
        clock = _read("Alpaca's clock", self.trading.get_clock, Clock)
        self.began = monotonic()
        if not clock.is_open or clock.next_close <= clock.timestamp:
            report.refused = (
                "outside the regular session: Alpaca's clock says the market is closed (next open "
                f"{clock.next_open.astimezone(UTC):%Y-%m-%d %H:%M} UTC), and a stop Alpaca holds works "
                "only in the regular session; nothing else was read or sent"
            )
            return report
        left = clock.next_close - clock.timestamp
        if left < CLOSE_MARGIN:
            report.refused = (
                f"too near the close: {left.total_seconds():.0f} s to the close, under the "
                f"{CLOSE_MARGIN.total_seconds():.0f} s this run may take, and no stop is sent after "
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
                why = "names no share quantity, so what it covers is not known"
            elif (_value(order.order_class) or "") not in SUMMABLE_CLASSES:
                why = (
                    f"is part of a {_value(order.order_class)} order, whose legs hold the same shares "
                    "once, so what it covers cannot be summed"
                )
            elif _value(order.status) in PENDING_OUT:
                why = f"is {_value(order.status)}, so what it covers is about to change; run again"
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

    def _protect(
        self, symbol: str, whole: Decimal, held: Decimal, ours: Decimal, now: datetime, n: int
    ) -> None:
        report = self.report
        client_order_id = f"{PROTECT_PREFIX}{self.stamp}-{n}-{uuid4().hex[:8]}"
        rows = tuple(ROWS) if self.plan_only else (self.row or "",)
        planned: dict[str, tuple[TrailingStopOrderRequest, Decimal, Distance]] = {}
        refused: dict[str, str] = {}
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
        if (aged := book.aged()) > QUOTE_MOST:
            report.note(
                report.unprotected,
                f"unprotected {symbol}: {held} held, no stop sent: the {FEED.value} quote its "
                f"distance was planned on is {aged.total_seconds():.0f} s old by the send, over the "
                f"{QUOTE_MOST.total_seconds():.0f} s a spread floor is taken from; run again",
            )
            return

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
        except Stopped:
            report.note(
                report.failed,
                f"failed {symbol}: {said}: the run was stopped during the send; Alpaca may yet show "
                "it working (mise run paper:account)",
            )
            raise
        except session_window.FAILED as e:
            is_refusal = isinstance(e, APIError) and e.status_code in DEFINITIVE_SUBMIT_REFUSALS
            if is_refusal or not_sent(e):
                report.note(report.failed, f"failed {symbol}: {said}: not placed: {_failure(e)}")
                return
            try:
                found = _look_up(self.trading, client_order_id)
            except session_window.FAILED as looked:
                report.note(
                    report.failed,
                    f"failed {symbol}: {said}: no answer ({_failure(e)}), and the look-up by its ID "
                    f"failed ({_failure(looked)}); Alpaca may yet show it working",
                )
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
            report.note(
                report.sent,
                f"sent {symbol}: {said}: no answer ({_failure(e)}), found by its ID: "
                f"{_value(found.status)}",
            )
            return
        answered = _value(getattr(order, "status", None))
        if answered in session_window.TERMINAL:
            report.note(
                report.failed,
                f"failed {symbol}: {said}: answered {answered}; the long has no stop",
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


def task_from_flags(
    plan_only: str, row: str, trading: TradingClient | None = None, data: Any = None
) -> int:
    """`paper:protect`'s two flags as the task hands them over, as text: plan-only is exactly `true` or
    `false` (mise gives a passed switch `true`, and the task gives `false` for one not passed), and
    anything else is refused by name with nothing read, so a value not known here never becomes a send.
    The row is a registered name, or empty for none."""
    if plan_only not in ("true", "false"):
        print(
            f"refused: plan-only reads {plan_only!r}, which is neither true nor false; nothing was "
            "read or sent",
            file=sys.stderr,
        )
        return REFUSED
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
    `custody.close_task` handles it; Ctrl-C ends it at once, with every line so far already printed."""
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
