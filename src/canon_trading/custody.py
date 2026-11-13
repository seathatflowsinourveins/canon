"""Custody on a paper account: which orders our nodes wrote, a read of the account that says so, and a close
that touches only those.

Contract: alpaca-py 0.45.0's `TradingClient` (paper) `get_account`, `get_all_positions`, `get_orders` with a
`GetOrdersRequest` and `get_account_configurations`, and its `TradeAccount`, `Position`, `Order` and
`AccountConfiguration` models, `get_order_by_client_id`, `cancel_order_by_id` and `submit_order` with a
`MarketOrderRequest` or a `LimitOrderRequest`, and `StockHistoricalDataClient.get_stock_latest_quote` with a
`StockLatestQuoteRequest` (`trading/client.py`, `trading/models.py`, `trading/requests.py`,
`data/historical/stock.py`, read in `.venv` and at the pin `c42add7c0a70` 2026-11-11);
NautilusTrader's client order ID generator at the pin (`d53849b216e2`). Sources, read 2026-11-11:

* `crates/common/src/generators/client_order_id.rs` at the pin: unless a strategy asks for UUIDs, its
  client order IDs are `O-{YYYYMMDD}-{HHMMSS}-{trader tag}-{strategy tag}-{count}`, the time in UTC to the
  second, with hyphens by default (`use_hyphens_in_client_order_ids`, `crates/trading/src/strategy/config.rs`);
  `TraderId::get_tag` is the part after the last hyphen (`crates/model/src/identifiers/trader_id.rs`).
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md: "Once an order is placed, it can be queried
  using either the client-provided order ID or the system-assigned unique ID";
  https://docs.alpaca.markets/us/reference/postorder.md: `client_order_id` is "Automatically generated if
  not sent", so an order placed without one (the dashboard, another tool) carries an ID Alpaca made.
* https://docs.alpaca.markets/us/reference/getallorders-1.md: `status=open`, "Defaults to 50 and max is 500";
  alpaca-py's `GetOrdersRequest`: `status` "open, closed or all", `after` "only ones submitted after this
  timestamp", `symbols`; `Order.filled_qty` and `side`.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading", and the 24/5 page
  (https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md): outside the regular session only a
  limit order marked `extended_hours`, `day` or `gtc`, is taken (ADR 0044, ADR 0055).
* Alpaca's error guide (https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca, read
  for ADR 0060): shares an open sell holds "cannot be used again", and after a cancel "wait for confirmation
  that the open orders are actually canceled before sending the close request".

Lineage (the owner's requirement of 2026-11-11, restated: the owner may place orders on these accounts, so a
node is to touch only what its own lineage wrote; any other order, or a quantity that does not match, is to
freeze that symbol and be reported, never touched). `paper:account` shows the classification and the
close-only form below acts on it. The check after a window's stop does not consult it yet: it cancels every
open order in the window's symbol (ADR 0060, "Bad", 2026-11-11). Our nodes are built by `paper_node.alpaca_paper_builder` under
`paper_node.TRADER_ID` (`TESTER-001`), and every strategy that sends orders there is given the order ID tag
`001` (`SessionMaCross` in `session_window`, `EmaCross` in `paper_node`), so they write
`O-YYYYMMDD-HHMMSS-001-001-N`; the account check after a window's stop writes
`canon-close-YYYYMMDDTHHMMSSZ-N-<8 hex>` (`session_window.CLOSE_ID_PREFIX`, ADR 0060), and the close-only
form the same with `hand-` after the prefix. An ID of any other form is foreign, and an order with no ID
is never ours either (`MISSING`), since nothing shows it is ours. A replacement is read by its own ID like
any order: whose it is never follows from what it replaces. One our adapter sent carries the replaced
order's ID with a clock stamp after its count (`canon_alpaca.execution.replacement_client_order_id`, ADR
0129), which is of our nodes' form, so it is ours; that holds for an ID a node wrote, and a close of the
check's or of this form is replaced by no node and would not keep its form. One made any other way, by hand
or by another tool, carries an ID Alpaca made or one its maker chose, and is foreign whatever it replaces. On
account A, the paper tests' own orders that carry IDs of another form (a test's hand-made ID, another trader's
or another strategy tag) read foreign too: the rule names what our window and paper nodes write, and nothing
else is treated as theirs.

`mise run paper:account <tests|windows|protect>` prints the account through `account_task`: equity, cash and buying
power, each position, each open order with its lineage, and `no_shorting`. It reads only: the task and
`account_task` set data-only mode (`CANON_ALPACA_DATA_ONLY=1`, ADR 0070), so the client
`session_window.paper_trading` builds refuses every method but GET before anything is sent. No account
number, account ID, order ID or client order ID is printed; an order is named by its symbol, side,
quantity, type, limit, status and lineage.

Quantity (`read_holdings`). Alpaca's position carries no client order ID, so the quantity ours opened is read
from the venue's orders: the net fill (buys add, sells take away) of every order of our lineage in the
symbol submitted in the last `LINEAGE_LOOKBACK` (`status=all`, so a partly filled open order counts). An
order that replaces another counts only what it filled beyond that one: Alpaca's replacement "carries the
order on", its filled quantity counting the fills before it (ADR 0042, paper, 2026-11-10), so a chain of
ours is counted once, and the fills of a foreign order in a chain stay foreign whatever replaced it. A
replacement of ours with fills whose replaced order the listing does not hold (one working since before the
lookback) carries fills that cannot be told apart, and freezes the symbol; so does one whose replaced order the
lookback's listing does hold but shows no count. Every count this module reads from an order is read one
way (`_shown`): a count is a finite number not below zero, and what is absent, empty, no number, not finite
or below zero is not zero but unknown. An order in the symbol's listing, ours or not, whose fill count or
quantity is no count leaves the quantity ours opened not established; so does an open order, a close of
ours last shown without a fill count, and counts too large to sum. An order placed by notional names no
quantity, which Alpaca lists as null beside a notional above zero, and that alone is not a missing one. A
position's quantity is signed at Alpaca, so its rule is its own (`_held`): it must be a finite number; one
below zero is a short, as is any position labelled short whatever it shows; what is no finite number is not
known, and is carried as unknown, never as zero. What no order holds is read for no decision: left out it
passes, and listed it must be a count for a long. Each of these freezes its symbol by name, at the run's
last read as at its first, and none ends the run before our open orders are canceled: when the account
cannot be read whole, they are canceled from the open orders alone and the run then fails by name. A
replacement with no fills, as a resting
stop tightened days after it was placed, carries none and counts nothing. A long
is ours to close only when it is exactly that net. On an account held long-only, ours at the lookback's start
is not negative, so the net over the lookback is never more than what ours holds now, and a match never takes
a foreign share; a lookback too short to hold our entry gives a mismatch, which freezes the symbol rather than
closing it. So the form reads Alpaca's `no_shorting` with the account, and on an account that is not held
long-only it freezes every position. A listing that returns its maximum (`ORDERS_PAGE_LIMIT`) may not hold
every order, so it freezes the symbol too. A short is frozen: the form never buys.

The close-only form (`close_ours`; `mise run paper:close <tests|windows>` by hand, under the account's lock).
It reads Alpaca's clock for the part of the session (`session_window.session_part`), and the account. It
cancels each open order of ours in every symbol (a cancel touches only ours, and an order of ours left working
could add to a position nobody closes) and waits, at most `CANCEL_WAIT_SECS`, for Alpaca to show each ended,
then reads the account again. In each symbol nothing froze, it closes the long ours opened: in the regular
session with a DAY market sell, outside it with a DAY limit sell marked `extended_hours`, priced
`session_ma_cross.LIMIT_OFFSET` through the touch from alpaca-py's latest quote on the part's feed
(`session_window.window_feed`) and put on Alpaca's step by `session_window.close_limit`, as the account
check after a stop prices its own; a sell under the finest step goes at 0.0001, named in the record. Each
close is one attempt under a client order ID of our lineage, sent once (`session_window.send_once`), left
working for `CLOSE_WAIT_SECS`, looked up by its ID, and canceled at the end if it has not ended; a close whose
answer was lost is looked up, never sent again, and one never found is named `unfound`, since Alpaca may yet
show it working. SIGTERM (the task's `timeout`) or Ctrl-C stops the run as a failure and cancels each close it
sent first. What of ours is left is reported by name, and every frozen symbol with why. With no session part
open it sends no close. It writes no window record and no gap marker. Not documented by Alpaca, so UNVERIFIED
until a paper run shows it: what the latest-quote endpoint answers while the feed's session is closed, and how
fresh its quote is."""

from __future__ import annotations

import os
import re
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.execution import DEFINITIVE_SUBMIT_REFUSALS
from canon_alpaca.execution import ORDERS_PAGE_LIMIT
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import counted
from canon_alpaca.providers import not_sent
from canon_alpaca.providers import require_get_only

from . import session_window
from .paper_node import TRADER_ID

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Generator

    from alpaca.trading.client import TradingClient

OURS = "ours"
FOREIGN = "foreign"
MISSING = "missing"
"""What `lineage` answers: an ID our nodes write, an ID of any other form, or no ID at all."""

ORDER_ID_TAG = "001"
"""The order ID tag every strategy that sends orders on our nodes is given (`session_window.window_node`,
`paper_node.ema_cross_config`); a test holds the two to it."""
TRADER_TAG = TRADER_ID.value.rsplit("-", 1)[-1]
"""`TraderId::get_tag`: the part of `paper_node.TRADER_ID` after its last hyphen."""
NODE_CLIENT_ORDER_ID = re.compile(
    rf"O-[0-9]{{8}}-[0-9]{{6}}-{TRADER_TAG}-{ORDER_ID_TAG}-[0-9]+", re.ASCII
)
"""The client order IDs our nodes write (module header, "Lineage")."""
CHECK_CLOSE_PREFIX = "canon-close-"
"""What the client order ID of a close our own checks send begins with (`session_window.CLOSE_ID_PREFIX`;
a test holds the two equal)."""
CLOSE_FORMS = ("hand-",)
"""What follows `CHECK_CLOSE_PREFIX` in a close this module's close-only form sends (`HAND_CLOSE_PREFIX`)."""
CLOSE_CLIENT_ORDER_ID = re.compile(
    rf"{re.escape(CHECK_CLOSE_PREFIX)}(?:{'|'.join(CLOSE_FORMS)})?[0-9]{{8}}T[0-9]{{6}}Z-[0-9]+-[0-9a-f]{{8}}",
    re.ASCII,
)
"""The whole of a close's client order ID: the prefix, the form, the run's UTC stamp, a count and eight hex
digits, as `session_window._AccountCheck._close` writes it; not the prefix alone, which anyone could type."""


def lineage(client_order_id: object) -> str:
    """`OURS` for a client order ID our nodes or checks write, `MISSING` for none (None or empty), else
    `FOREIGN`. Only `OURS` may ever be canceled or closed."""
    if client_order_id is None or not str(client_order_id).strip():
        return MISSING
    found = str(client_order_id)
    if NODE_CLIENT_ORDER_ID.fullmatch(found) or CLOSE_CLIENT_ORDER_ID.fullmatch(found):
        return OURS
    return FOREIGN


def _value(field: object) -> str:
    """An alpaca-py enum's value, or the field as text; `-` for none."""
    if field is None:
        return "-"
    return str(getattr(field, "value", field))


def order_line(order: Order) -> str:
    """One open order as `paper:account` names it: symbol, side, quantity, type, limit, status, lineage."""
    qty = _quantity_named(order)
    kind = _value(order.type if order.type is not None else order.order_type)
    return (
        f"{order.symbol} {_value(order.side)} {qty} {kind} limit {_value(order.limit_price)} "
        f"status {_value(order.status)} {lineage(order.client_order_id)}"
    )


def position_line(position: Position) -> str:
    """One position: symbol, side, quantity, average entry, market value, unrealised result."""
    listed = _listed(position)
    qty = NO_NUMBER if listed.held is None else position.qty
    note = f" ({listed.unknown})" if listed.unknown else ""
    if listed.short and _value(position.side) != "short":
        note += " (below zero: read as a short)"
    return (
        f"{position.symbol} {_value(position.side)} {qty} avg_entry {position.avg_entry_price} "
        f"market_value {_value(position.market_value)} unrealized_pl {_value(position.unrealized_pl)}"
        f"{note}"
    )


class ReadFailed(RuntimeError):
    """One of Alpaca's reads failed: named by the read, the error's type and Alpaca's status, never by its
    body, which could carry an identifier."""


def _read[T](name: str, call: Callable[[], object], kind: type[T], item: type | None = None) -> T:
    """`call()`, which must answer a `kind` (a list of `item`s, when given), or `ReadFailed` naming
    `name`."""
    try:
        found = call()
    except Exception as e:
        raise ReadFailed(f"{name} failed: {_failure(e)}") from e
    if not isinstance(found, kind):
        raise ReadFailed(f"{name} answered a {type(found).__name__}, not a {kind.__name__}")
    if item is not None and isinstance(found, list) and not all(isinstance(i, item) for i in found):
        raise ReadFailed(f"{name} answered something other than a list of {item.__name__}")
    return found


def open_orders(trading: TradingClient) -> list[Order]:
    """Every open order on the account, at most `ORDERS_PAGE_LIMIT` ("Defaults to 50 and max is 500"),
    as `session_window.open_orders` reads one symbol's."""
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT, nested=False)
    return _read("the open orders", lambda: trading.get_orders(request), list, Order)


def account_lines(trading: TradingClient) -> list[str]:
    """The account as `paper:account` prints it (module header): four reads, all GET."""
    account = _read("the account", trading.get_account, TradeAccount)
    positions = _read("the positions", trading.get_all_positions, list, Position)
    orders = open_orders(trading)
    configuration = _read(
        "the account configuration", trading.get_account_configurations, AccountConfiguration
    )
    lines = [
        f"equity: {_value(account.equity)}",
        f"cash: {_value(account.cash)}",
        f"buying_power: {_value(account.buying_power)}",
        f"positions: {len(positions)}",
        *(f"  {position_line(p)}" for p in sorted(positions, key=lambda p: p.symbol)),
        f"open orders: {len(orders)}"
        + (
            f" (the first {ORDERS_PAGE_LIMIT}; there may be more)"
            if len(orders) >= ORDERS_PAGE_LIMIT
            else ""
        ),
        *(
            f"  {order_line(o)}"
            for o in sorted(orders, key=lambda o: (o.symbol or "", order_line(o)))
        ),
        f"no_shorting: {str(configuration.no_shorting).lower()}",
    ]
    return lines


def account_task(trading: TradingClient | None = None) -> int:
    """`mise run paper:account <tests|windows|protect>`: print `account_lines` in data-only mode; 0, or 1 naming
    the read that failed (`ReadFailed`), or the client that would send more than GET. The client, built
    here or given, is checked to refuse every method but GET before anything is read (`require_get_only`,
    as the data-only modules check theirs)."""
    os.environ[DATA_ONLY_ENV] = (
        "1"  # the task sets it too; the module does not rely on that (ADR 0070)
    )
    try:
        if trading is None:
            trading = session_window.paper_trading()  # the paper client with the adapter's timeout
        require_get_only(trading)
        lines = account_lines(trading)
    except (ReadFailed, MethodRefused) as e:
        print(f"paper:account: {e}; nothing else was read", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------------------------------------
# The close-only form (module header, "The close-only form")

LINEAGE_LOOKBACK = timedelta(hours=24)
"""How far back the venue's orders are read for the quantity ours opened (module header, "Quantity")."""
CLOSE_WAIT_SECS = 60.0
"""How long the one close is left working before it is canceled."""
CANCEL_WAIT_SECS = 30.0
"""How long the form waits for Alpaca to show each of our canceled orders ended before it closes, since
shares an open sell holds cannot be sold again until the cancel is confirmed (Alpaca's error guide)."""
HAND_CLOSE_PREFIX = f"{CHECK_CLOSE_PREFIX}hand-"
"""The client order ID prefix of a close the by-hand form sends: of our lineage (`CHECK_CLOSE_PREFIX`)."""


def _number(field: object) -> Decimal | None:
    """What a listing shows as a number, or None when it shows none or one that is not finite."""
    if field in (None, ""):
        return None
    try:
        value = Decimal(str(field))
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _shown(field: object) -> Decimal | None:
    """The one read of a quantity Alpaca lists: a count as the listing shows it, or None when it shows
    none, one that is no finite number, or one below zero. alpaca-py's quantities are strings or numbers
    with no bound (`Order.filled_qty`, `alpaca/trading/models.py:283` at the pin, is optional too), and
    what is not shown is not zero: whatever rests on a quantity that is no count is not established."""
    value = _number(field)
    return value if value is not None and value >= 0 else None


class _Unread(str):
    """A freeze reason that says a quantity could not be read. It is text like any other reason; the
    type is what lets a run keep it when a later read of the same symbol is clean (`_CloseOnly._run`):
    a listing that could not be read once in a run is not trusted in that run."""

    __slots__ = ()


NO_COUNT = "a quantity that is no count"
NO_NUMBER = "a quantity that is no number"
"""What a line of a report says in place of a quantity that cannot be read (`_shown`, `_number`)."""


@dataclass(frozen=True)
class _Listed:
    """One position as Alpaca lists it. `held` is its signed quantity, below zero for a short, or None
    when its quantity is no finite number. `short` says it is a short, by its label or by its sign.
    `unknown` names what in the listing cannot be read, or is ""."""

    held: Decimal | None
    short: bool = False
    unknown: str = ""


def _listed(position: Position) -> _Listed:
    """How this module reads one listed position, for the close form and for the account's lines alike.

    A position's quantity (`qty`) is signed at Alpaca, so the count rule of `_shown` is not its rule: it
    must be a finite number, read before any sign is taken off. One below zero is a short, and so is any
    position whose `side` is short, whatever quantity it shows (a short listed with none, or with one
    above zero, is still never read as a long). What is no finite number is not known.

    What no order holds (`qty_available`) is optional in alpaca-py (`alpaca/trading/models.py:201` at the
    pin) and is not among the fields Alpaca's reference requires of a position
    (https://docs.alpaca.markets/us/reference/getallopenpositions.md, read 2026-11-13), and nothing here
    decides on it: left out it passes; listed, it must be a count for a long and a finite number for a
    short. The day this module decides on it, it becomes required in that change."""
    symbol = str(position.symbol)
    # read by name: a listing may leave either out altogether
    qty = _number(getattr(position, "qty", None))
    available = getattr(position, "qty_available", None)
    short = _value(position.side) == "short" or (qty is not None and qty < 0)
    if qty is None:
        return _Listed(
            None,
            short,
            _Unread(
                f"the position in {symbol} shows no quantity that can be counted, so what is held "
                "there is not known"
            ),
        )
    unknown = ""
    if available is not None and (_number(available) if short else _shown(available)) is None:
        unknown = _Unread(
            f"the position in {symbol} lists a quantity no order holds that cannot be counted"
        )
    # `copy_abs` and `copy_negate` take no context, so no quantity is too large for them
    return _Listed(qty.copy_abs().copy_negate() if short else qty, short, unknown)


def _held(trading: TradingClient) -> dict[str, _Listed]:
    """Each position Alpaca lists, by symbol (`_listed`)."""
    return {
        str(position.symbol): _listed(position)
        for position in _read("the positions", trading.get_all_positions, list, Position)
    }


def _by_notional(order: Order) -> bool:
    """Whether an order names a dollar amount in place of a share quantity: Alpaca then lists its
    quantity as null (`Order.qty`, `alpaca/trading/models.py:238-240` at the pin), which is not a
    missing count. An empty quantity is not that form, and neither is a notional that is no amount
    above zero."""
    notional = _shown(order.notional)
    return order.qty is None and notional is not None and notional > 0


def _no_count(order: Order) -> str:
    """ "" when both of an order's quantities are counts (`_shown`), else which is not: its fill count,
    or its quantity. An order placed by notional names no quantity (`_by_notional`)."""
    if _shown(order.filled_qty) is None:
        return "fill count"
    return "" if _by_notional(order) or _shown(order.qty) is not None else "quantity"


def _quantity_named(order: Order) -> str:
    """An order's quantity for a line of a report: as listed when it is a count, its notional when it
    was placed by one, and named as no count otherwise, never printed as if it were one."""
    if _by_notional(order):
        return f"notional {_value(order.notional)}"
    return str(order.qty) if _shown(order.qty) is not None else NO_COUNT


def describe(order: Order) -> str:
    """An order by name for a report: ours by its client order ID, a foreign one by what it is (never its
    ID, which may be the owner's)."""
    qty = _quantity_named(order)
    what = f"{_value(order.side)} {qty} {order.symbol} {_value(order.type or order.order_type)}"
    if lineage(order.client_order_id) == OURS:
        return f"{order.client_order_id} ({what}, {_value(order.status)})"
    return f"a {lineage(order.client_order_id)} order: {what}, {_value(order.status)}"


@dataclass
class Holding:
    """One symbol's custody: what Alpaca holds, what our orders opened, and what is open in it."""

    symbol: str
    held: Decimal | None
    """Alpaca's signed position, zero when it lists none, or None when it lists one whose quantity is
    not known: never zero in its place."""
    ours_open: list[Order] = field(default_factory=list)
    foreign_open: list[Order] = field(default_factory=list)
    opened: Decimal | None = None
    """Our orders' net fill over `LINEAGE_LOOKBACK`, or None when it could not be established."""
    frozen: list[str] = field(default_factory=list)
    """Why the symbol is frozen, by name: empty when it is ours to close."""

    def ours_left(self) -> list[str]:
        """What of ours is left in the symbol, by name: each open order of ours, and the position when the
        symbol is not frozen and ours opened it. A frozen symbol's position is not established as ours, so
        it is reported as frozen, never as ours."""
        left = [f"open order {describe(o)}" for o in self.ours_open]
        if self.held and self.opened and not self.frozen:
            left.append(f"position of {self.held} {self.symbol} (ours opened {self.opened})")
        return left


def _not_counted(symbol: str, what: str) -> str:
    return _Unread(
        f"an order in {symbol} shows no {what} that can be counted, so the quantity ours opened is "
        "not established"
    )


def _opened(trading: TradingClient, symbol: str, now: datetime) -> tuple[Decimal | None, str]:
    """Our orders' net fill in `symbol` over `LINEAGE_LOOKBACK`, from the venue's orders (`status=all`),
    or None and why when the listing is full, so it may not hold them all."""
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        symbols=[symbol],
        after=now - LINEAGE_LOOKBACK,
        limit=ORDERS_PAGE_LIMIT,
        nested=False,
    )
    orders = _read(f"the orders in {symbol}", lambda: trading.get_orders(request), list, Order)
    if len(orders) >= ORDERS_PAGE_LIMIT:
        return None, (
            f"{len(orders)} orders in {symbol} in the last {LINEAGE_LOOKBACK}, the most one listing "
            "returns, so the quantity ours opened is not established"
        )
    for order in orders:
        if what := _no_count(order):
            # ours or not: a row that shows no count says nothing sure about the symbol, and read as
            # zero a sale of ours would vanish from the net, or a hand's fills be taken for ours
            return None, _not_counted(symbol, what)
    try:
        return _net_of_ours(symbol, orders)
    except ArithmeticError:
        # counts the arithmetic cannot carry (a number too large to sum) are no counts either
        return None, _Unread(
            f"the fill counts of the orders in {symbol} cannot be summed, so the quantity ours opened "
            "is not established"
        )


def _net_of_ours(symbol: str, orders: list[Order]) -> tuple[Decimal | None, str]:
    """`_opened`'s sum over a listing whose every row shows counts (`_no_count`)."""
    listed = {str(o.id): o for o in orders}
    opened = Decimal(0)
    for order in orders:
        if lineage(order.client_order_id) != OURS:
            continue
        filled = _shown(order.filled_qty)
        if filled is None:
            return None, _not_counted(symbol, "fill count")
        if order.replaces is not None and filled > 0:
            # a replacement carries on the fills of the order it replaces, whoever placed that one:
            # only what it filled beyond them is its own (one with no fills carries none)
            replaced = listed.get(str(order.replaces))
            if replaced is None:
                return None, (
                    f"an order of ours in {symbol} replaces one the listing of the last "
                    f"{LINEAGE_LOOKBACK} does not hold, whose fills it carries, so the quantity ours "
                    "opened is not established"
                )
            before = _shown(replaced.filled_qty)
            if before is None:
                return None, _not_counted(symbol, "fill count")
            filled = max(Decimal(0), filled - before)
        opened += filled if _value(order.side) == "buy" else -filled
    return opened, ""


def read_holdings(trading: TradingClient, now: datetime) -> list[Holding]:
    """Each symbol with a position or an open order, with its custody: a symbol is frozen when a foreign or
    unmarked order is open in it, when what Alpaca holds is short, or when it is not exactly what our orders
    opened, or when the account is not held long-only, on which the quantity rule rests, or when a
    position, an open order or an order in the symbol's listing shows a quantity that cannot be read
    (module header, "Quantity"). A position whose quantity is not known has `held` None, with why in
    `frozen`, and nothing is sent for it. A read that fails raises `ReadFailed`."""
    listed = _held(trading)
    orders = open_orders(trading)
    configuration = _read(
        "the account configuration", trading.get_account_configurations, AccountConfiguration
    )
    found = {
        symbol: Holding(symbol, listed[symbol].held if symbol in listed else Decimal(0))
        for symbol in sorted({*listed, *(str(o.symbol) for o in orders if o.symbol)})
    }
    for symbol, position in listed.items():
        if position.unknown:
            found[symbol].frozen.append(position.unknown)
    for order in orders:
        if (holding := found.get(order.symbol)) is not None:
            mine = lineage(order.client_order_id) == OURS
            (holding.ours_open if mine else holding.foreign_open).append(order)
    full = len(orders) >= ORDERS_PAGE_LIMIT
    for holding in found.values():
        holding.frozen.extend(f"open {describe(o)}" for o in holding.foreign_open)
        holding.frozen.extend(
            _Unread(f"an open order in {holding.symbol} shows no {what} that can be counted")
            for o in (*holding.ours_open, *holding.foreign_open)
            if (what := _no_count(o))
        )
        if full:
            holding.frozen.append(f"{len(orders)} open orders, the most one listing returns")
        position = listed.get(holding.symbol)
        if position is not None and position.short:
            size = (
                "a quantity that is not known"
                if position.held is None
                else position.held.copy_abs()
            )
            holding.frozen.append(f"a short of {size} {holding.symbol}: never bought back here")
        elif holding.held and not configuration.no_shorting:
            holding.frozen.append(
                "the account is not long-only (no_shorting is false), so what our orders opened does "
                'not establish whose the position is (module header, "Quantity")'
            )
        if holding.frozen:
            continue  # nothing here is closed, so what ours opened is not read
        holding.opened, why = _opened(trading, holding.symbol, now)
        if holding.opened is None:
            holding.frozen.append(why)
        elif holding.held is not None and holding.held > 0 and holding.held != holding.opened:
            holding.frozen.append(
                f"{holding.held} {holding.symbol} held, but our orders opened {holding.opened} in the "
                f"last {LINEAGE_LOOKBACK}"
            )
    for holding in found.values():
        holding.frozen[:] = dict.fromkeys(
            holding.frozen
        )  # two orders alike are one reason, named once
    return list(found.values())


@dataclass
class CloseReport:
    """What the close-only form found and did, by name."""

    part: str = ""
    found: list[str] = field(default_factory=list)
    frozen: dict[str, list[str]] = field(default_factory=dict)
    canceled: list[str] = field(default_factory=list)
    closes: dict[str, list[str]] = field(default_factory=dict)
    left: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed: str = ""
    """What stopped the run part way, by name; when set, the run is never reported flat."""
    unfound: list[str] = field(default_factory=list)
    """Closes sent with no answer and never found: Alpaca may yet show one working, so the run is not
    flat."""

    @property
    def flat(self) -> bool:
        """Nothing of ours is left and the run was not stopped part way. Frozen symbols are reported apart:
        they are not ours to close."""
        return not self.left and not self.failed and not self.unfound

    def lines(self) -> list[str]:
        out = [f"session part: {self.part or 'none open'}"]
        out += [f"found: {f}" for f in self.found] or ["found: nothing of ours"]
        for symbol, why in self.frozen.items():
            out += [f"frozen {symbol}: {w}" for w in why]
        out += [f"canceled: {c}" for c in self.canceled]
        for client_order_id, log in self.closes.items():
            out.append(f"close {client_order_id}: {'; '.join(log)}")
        out += [f"error: {e}" for e in self.errors]
        out += [f"failed: {self.failed}"] if self.failed else []
        out += [
            f"unfound: {u}, sent with no answer and never found; Alpaca may yet show it working, as an "
            "order of ours (mise run paper:account)"
            for u in self.unfound
        ]
        out += [f"left: {left}" for left in self.left] or ["left: nothing of ours"]
        return out


@contextmanager
def _sending_once(trading: TradingClient) -> Generator[None]:
    """`session_window.send_once` around one cancel or submit, alpaca-py's retries restored after, so
    the form's reads keep them."""
    before = getattr(trading, "_retry", None)
    session_window.send_once(trading)
    try:
        yield
    finally:
        if before is not None:
            trading._retry = before  # type: ignore[attr-defined]


def _part(trading: TradingClient) -> tuple[str, str]:
    """The session part open now (`session_window.session_part`) and "", or "" and why none is, in
    `session_part`'s words (a closed market, or a calendar missing a bound)."""
    clock, calendar, _ = session_window.session_now(trading)
    if clock.is_open:
        return "regular", ""
    try:
        return session_window.session_part("extended", clock, calendar)[1], ""
    except RuntimeError as e:
        return "", str(e)


def _look_up(trading: TradingClient, client_order_id: str) -> Order | None:
    try:
        order = trading.get_order_by_client_id(client_order_id)
    except APIError as e:
        if e.status_code == 404:
            return None
        raise
    assert isinstance(order, Order)
    return order


def _ended(order: Order | None) -> bool:
    return order is not None and _value(order.status) in session_window.TERMINAL


class _CloseOnly:
    """One run of the form against `trading` (and `data` for a quote outside the regular session)."""

    def __init__(self, trading, data, prefix, *, sleep, monotonic, now) -> None:
        self.trading = trading
        self.data = data
        self.prefix = prefix
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.report = CloseReport()
        self.stamp = now().strftime("%Y%m%dT%H%M%SZ")
        self.no_part = ""
        self.client: Any = None
        """The market data client, built on the first quote and kept for the run."""
        self.unread: dict[str, list[str]] = {}
        """Per symbol, what the run's first read could not count (`_carry`)."""
        self.closing: set[str] = set()
        """The symbols whose long the run found ours to close."""
        self.sent: list[str] = []
        self.done: set[str] = set()
        """The closes sent, and those `_work` has seen ended or canceled."""

    def run(self) -> CloseReport:
        report = self.report
        try:
            self._run()
        except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - named; the run is never flat
            report.failed = f"{_failure(e)} part way through"
            self._cancel_sent()
        try:
            last = read_holdings(self.trading, self.now())
            carried = self._carry(last)  # before `ours_left`: a frozen position is never ours left
            report.left = [x for h in last for x in h.ours_left()]
            for h in last:
                # a symbol frozen only at this read (a position that cannot be counted now) is named,
                # beside what `_run` and `_work` recorded: a frozen position is in no `ours_left`
                known = report.frozen.setdefault(h.symbol, []) if h.frozen else []
                known.extend(
                    f"at the end: {why}"
                    for why in h.frozen
                    if why not in known and why not in carried
                )
                if h.frozen and h.symbol in self.closing and (h.held is None or h.held > 0):
                    # ours when the run began, and not shown closed: never reported as nothing left
                    report.left.append(
                        f"the position in {h.symbol} this run was closing: frozen at the end, so it "
                        "is not shown closed"
                    )
        except Exception as e:  # noqa: BLE001 - named; the run is not flat
            report.failed = report.failed or f"the last read: {_failure(e)}"
            report.left = ["unknown: the account could not be read at the end"]
        return report

    def _run(self) -> None:
        report = self.report
        try:
            report.part, self.no_part = _part(self.trading)
            holdings = read_holdings(self.trading, self.now())
        except Stopped:
            raise  # a stop is not a failed read: it starts no cancel, as Ctrl-C starts none
        except Exception:
            # what is held could not be read, so nothing is closed; our own open orders need no such
            # read to be told as ours, and are canceled before the failure ends the run
            self._cancel_ours_alone()
            raise
        # what this read could not count is kept for the run (`_carry`), and what it froze is in the
        # report from here on, so a run that fails next still names it
        self.unread = {
            h.symbol: list(dict.fromkeys(w for w in h.frozen if isinstance(w, _Unread)))
            for h in holdings
            if any(isinstance(w, _Unread) for w in h.frozen)
        }
        report.frozen = {h.symbol: list(h.frozen) for h in holdings if h.frozen}
        report.found = [x for h in holdings for x in h.ours_left()]
        if any(h.ours_open for h in holdings):
            self._cancel_ours([o for h in holdings for o in h.ours_open])
            holdings = read_holdings(self.trading, self.now())  # a fill while canceling changes it
            report.part, self.no_part = _part(
                self.trading
            )  # the wait may have crossed into another part
            self._carry(holdings)
            listed = {h.symbol for h in holdings}
            report.frozen = {
                # a symbol with nothing listed in it any more is still named
                **{
                    symbol: [f"at the first read: {w}" for w in reasons]
                    for symbol, reasons in self.unread.items()
                    if symbol not in listed
                },
                **{h.symbol: list(h.frozen) for h in holdings if h.frozen},
            }
        closes = [h for h in holdings if self._closable(h)]
        self.closing = {h.symbol for h in closes}
        for n, h in enumerate(closes, 1):
            if client_order_id := self._close(h, n):
                self.sent.append(client_order_id)
        if self.sent:
            self._work(list(self.sent))

    def _carry(self, holdings: list[Holding]) -> set[str]:
        """Freeze, in a later read's holdings, each symbol whose quantity the run's first read could not
        count, and return the reasons added. That read replaces the first, but not what the first could
        not read: a listing that could not be read once in a run is not trusted in that run, however it
        is listed now."""
        added: set[str] = set()
        for h in holdings:
            for why in self.unread.get(h.symbol, ()):
                if why not in h.frozen:  # still unreadable now: named as it is, once
                    h.frozen.append(f"at the first read: {why}")
                    added.add(h.frozen[-1])
        return added

    def _cancel_sent(self) -> None:
        """After a failure part way: cancel each close sent that `_work` has not seen end, so none is left
        working; each is named in its log."""
        for client_order_id in self.sent:
            if client_order_id in self.done:
                continue
            log = self.report.closes[client_order_id]
            try:
                order = _look_up(self.trading, client_order_id)
                if order is None:
                    log.append("after the failure: not found")
                    self.report.unfound.append(client_order_id)
                    continue
                if _ended(order):
                    log.append("after the failure: ended")
                    continue
                with _sending_once(self.trading):
                    self.trading.cancel_order_by_id(order.id)
                log.append("canceled after the failure")
            except Exception as e:  # noqa: BLE001 - named in its log
                log.append(f"cancel after the failure failed: {_failure(e)}")

    def _cancel_ours_alone(self) -> None:
        """When the account could not be read whole: cancel our open orders from the open orders alone,
        by their client order IDs. If that listing cannot be read either, none is canceled and it is
        said: which orders are ours is never guessed."""
        try:
            ours = [o for o in open_orders(self.trading) if lineage(o.client_order_id) == OURS]
        except Exception as e:  # noqa: BLE001 - named; the first failure is the run's
            self.report.errors.append(f"our open orders were not canceled: {_failure(e)}")
            return
        self.report.found = [f"open order {describe(o)}" for o in ours]
        if ours:
            self._cancel_ours(ours)

    def _cancel_ours(self, orders: list[Order]) -> None:
        """Cancel each open order of ours, then wait, at most `CANCEL_WAIT_SECS`, for Alpaca to show each
        ended."""
        waiting = []
        for order in orders:
            waiting.append(
                str(order.client_order_id)
            )  # a cancel refused may be one that just filled
            try:
                with _sending_once(self.trading):
                    self.trading.cancel_order_by_id(order.id)
                self.report.canceled.append(str(order.client_order_id))
            except session_window.FAILED as e:
                self.report.errors.append(f"canceling {order.client_order_id}: {_failure(e)}")
        ends = self.monotonic() + CANCEL_WAIT_SECS
        while waiting and self.monotonic() < ends:
            self.sleep(session_window.POLL_SECS)
            for client_order_id in list(waiting):
                try:
                    if _ended(_look_up(self.trading, client_order_id)):
                        waiting.remove(client_order_id)
                except session_window.FAILED as e:
                    self.report.errors.append(f"looking up {client_order_id}: {_failure(e)}")
        for client_order_id in waiting:
            self.report.errors.append(
                f"{client_order_id} not shown ended {CANCEL_WAIT_SECS:.0f} s after its cancel"
            )

    def _closable(self, h: Holding) -> bool:
        """Whether `h` is ours to close now: not frozen, a long ours opened, nothing of ours still open,
        and a session part open."""
        if h.frozen or h.held is None or h.held <= 0 or h.ours_open:
            return False
        if not self.report.part:
            self.report.errors.append(
                f"{h.symbol}: no session part is open ({self.no_part}), so no close was sent"
            )
            return False
        return True

    def _close(self, h: Holding, n: int) -> str:
        """Send the one close for `h` and return its client order ID, or "" when none was sent."""
        client_order_id = f"{self.prefix}{self.stamp}-{n}-{uuid4().hex[:8]}"
        if h.held is None:  # `_closable` lets none such through: an unknown quantity is never sold
            return ""
        qty = float(h.held)
        if self.report.part == "regular":
            request: MarketOrderRequest | LimitOrderRequest = MarketOrderRequest(
                symbol=h.symbol,
                qty=qty,
                side=AlpacaOrderSide.SELL,
                time_in_force=AlpacaTimeInForce.DAY,
                client_order_id=client_order_id,
            )
            what = f"sell {h.held} {h.symbol} at market, DAY"
        else:
            try:
                price, quote = self._price(h.symbol)
            except Exception as e:  # noqa: BLE001 - named in the report, and no close is sent
                self.report.errors.append(f"{h.symbol}: no price for the close: {_failure(e)}")
                return ""
            request = LimitOrderRequest(
                symbol=h.symbol,
                qty=qty,
                side=AlpacaOrderSide.SELL,
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=float(price),
                extended_hours=True,
                client_order_id=client_order_id,
            )
            what = f"sell {h.held} {h.symbol} limit {price}, DAY, extended hours ({quote})"
        log = self.report.closes.setdefault(client_order_id, [f"sent: {what}"])
        try:
            with _sending_once(self.trading):
                order = counted(self.trading.submit_order)(request)
        except session_window.FAILED as e:
            refused = isinstance(e, APIError) and e.status_code in DEFINITIVE_SUBMIT_REFUSALS
            if refused or not_sent(e):
                log.append(f"not placed: {_failure(e)}")
                return ""
            log.append(f"no answer: {_failure(e)}")  # looked up by its ID in `_work`
            return client_order_id
        log.append(f"answered: {_value(getattr(order, 'status', None))}")
        return client_order_id

    def _price(self, symbol: str) -> tuple[Decimal, str]:
        """The close's limit and the quote it came from, for the log: `session_window.close_limit`, which
        the account check after a window's stop prices its own by too."""
        if self.client is None:
            self.client = self.data() if callable(self.data) else self.data
        return session_window.close_limit(self.client, symbol, "sell", self.report.part)

    def _work(self, sent: list[str]) -> None:
        """Leave each close working for `CLOSE_WAIT_SECS`, looking it up by its ID each poll; cancel any
        not ended at the end, and record what Alpaca last showed of it."""
        waiting = list(sent)
        ends = self.monotonic() + CLOSE_WAIT_SECS
        seen: dict[str, Order] = {}
        while waiting and self.monotonic() < ends:
            self.sleep(session_window.POLL_SECS)
            for client_order_id in list(waiting):
                try:
                    order = _look_up(self.trading, client_order_id)
                except session_window.FAILED as e:
                    self.report.errors.append(f"looking up {client_order_id}: {_failure(e)}")
                    continue
                if order is not None:
                    seen[client_order_id] = order
                if _ended(order):
                    waiting.remove(client_order_id)
                    self.done.add(client_order_id)
        for client_order_id in waiting:
            log = self.report.closes[client_order_id]
            try:
                order = seen.get(client_order_id) or _look_up(self.trading, client_order_id)
                if order is None:
                    log.append(f"not found {CLOSE_WAIT_SECS:.0f} s after it was sent")
                    self.report.unfound.append(client_order_id)
                    self.done.add(client_order_id)
                    continue
                with _sending_once(self.trading):
                    self.trading.cancel_order_by_id(order.id)
                self.done.add(client_order_id)
                log.append(f"canceled after {CLOSE_WAIT_SECS:.0f} s")
            except session_window.FAILED as e:
                if isinstance(e, APIError) and e.status_code == 422:
                    # Alpaca refuses a cancel of an order that has ended: it ended after the last poll
                    log.append("cancel refused (HTTP 422): it had ended; last shown below")
                    self.done.add(client_order_id)
                else:
                    log.append(f"cancel failed: {_failure(e)}")
        for client_order_id in sent:
            try:
                order = _look_up(self.trading, client_order_id)
            except session_window.FAILED as e:
                self.report.errors.append(f"looking up {client_order_id}: {_failure(e)}")
                continue
            if order is None:
                continue
            filled = _shown(order.filled_qty)
            self.report.closes[client_order_id].append(
                f"last shown: {_value(order.status)}, filled "
                f"{'not shown as a count' if filled is None else filled}"
            )
            if filled is None:
                # what it sold is not known, so the symbol is not reported as closed
                symbol = str(order.symbol)
                self.report.frozen.setdefault(symbol, []).append(
                    f"the close sent in {symbol} was last shown with no fill count that can be "
                    "counted, so what it sold is not known"
                )


def _failure(e: BaseException) -> str:
    """An error by name: a `ReadFailed` by its own text, which names the read; any other by its type and
    Alpaca's status, never its body, which could carry an identifier."""
    if isinstance(e, (ReadFailed, session_window.QuoteTooWide)):
        return str(e)
    status = getattr(e, "status_code", None)
    return f"{type(e).__name__}" + (f" (HTTP {status})" if status is not None else "")


def close_ours(
    trading: TradingClient,
    data: Any,
    *,
    prefix: str = HAND_CLOSE_PREFIX,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CloseReport:
    """The close-only form (module header): cancel our open orders, close what ours opened where nothing
    froze the symbol, one attempt each, and report what is left of ours by name. `data` is an alpaca-py
    `StockHistoricalDataClient`, or a callable that builds one, used only outside the regular session."""
    return _CloseOnly(trading, data, prefix, sleep=sleep, monotonic=monotonic, now=now).run()


class Stopped(Exception):
    """The task was sent SIGTERM (the task's `timeout`, or a person): raised in the run, which then cancels
    each close it sent before it ends (`_CloseOnly.run`)."""


def _stop(signum: int, frame: object) -> None:
    """Raise `Stopped` once: a second SIGTERM (`uv run` passes one on, and `timeout` may send another) is
    ignored, so it cannot break off the cancels; the `timeout`'s KILL still bounds them."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Stopped(f"stopped by signal {signum}")


def close_task(trading: TradingClient | None = None, data: Any = None, **timing: Any) -> int:
    """`mise run paper:close <tests|windows>`: run `close_ours` and print its report. 0 when nothing of
    ours is left and nothing is frozen, 1 when something of ours is left, 2 when only frozen symbols are
    (a symbol whose close was last shown without a fill count is one, even when nothing is held after;
    so is one whose quantity the run's first read could not count, however it is listed later).
    SIGTERM, which the task's `timeout` sends before its KILL, and Ctrl-C stop the run as a failure, after
    which each close sent is canceled (Python's `signal.signal`; the default for SIGTERM ends the process
    with no cleanup). `timing` goes to `close_ours` (`sleep`, `monotonic`, `now`), for tests."""
    before = signal.signal(signal.SIGTERM, _stop)
    try:
        trading = trading or session_window.paper_trading()
        report = close_ours(trading, data or session_window.window_data, **timing)
    except Exception as e:  # noqa: BLE001 - named and failed, never swallowed
        print(f"paper:close: failed: {_failure(e)}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, before)
    print("\n".join(report.lines()))
    if not report.flat:
        return 1
    return 2 if report.frozen else 0
