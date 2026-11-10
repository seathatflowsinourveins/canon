"""Execution client: market, limit, stop and stop-limit orders for US equities on Alpaca's paper Trading API.

Contract: `nautilus_trader.live.clients.ExecutionClient`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Execution client hooks", https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md). The
client is constructed with the factory's name, config, cache, clock and trader ID and with a venue, account
ID, account type and OMS type. It overrides `_submit_order`, `_modify_order`, `_cancel_order`,
`_cancel_all_orders` and the four reconciliation hooks, emits account, order and fill events through the
base class's `generate_*`
methods, and leaves `_batch_cancel_orders` to its default, "ordered calls of the corresponding
single-order hook" (Alpaca has no endpoint that cancels a list of orders by ID), and `_generate_mass_status`
to the native composition of the bulk report hooks. Nothing is started in the constructor; the alpaca-py
clients and the task that applies trade updates in order are made in `_connect`, and the stream thread is
joined and the task ended in `_disconnect`. Hooks not overridden here raise `NotImplementedError`.

Modify (ADR 0042) rests on Alpaca's "Replace Order by ID"
(https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md) and on a paper observation of
2026-11-10, 3 of 3 runs, for what that page does not state. The PATCH answer is "The new Order object with
the new order ID", but a success "does NOT guarantee the existing open order has been replaced", so
`OrderUpdated` is generated only on the stream's `replaced` for the node's order, which carries the old
order with `replaced_by` (observed), with the replacement's values from the answer or, failing that, from
Alpaca; if neither yields them, nothing is generated and reconciliation settles the order. The replacement
gets a client order ID Alpaca makes (one kept is refused, observed), so its updates and reports are traced
back to the node's order through its `replaces` links, cached and walked again after a restart. A 422
refusal is `OrderModifyRejected` unless Alpaca shows the order already replaced as asked (a second replace is
refused "order already replaced", observed, as alpaca-py's own retry would be); `order_replace_rejected`
and a replacement's `rejected` are `OrderModifyRejected` too, the second because Alpaca rejects the
replacement when the old order fills first. Denied before Alpaca: no venue order ID, a fractional quantity
and a sub-penny price; what only Alpaca knows (an order in `accepted` "cannot be replaced") is left to its
answer. Each modify sent gets one outcome, though Alpaca can refuse it on the HTTP answer and on the
stream. As upstream's Binance spot adapter does for its cancel-replace (`crates/adapters/binance/src/common/
dispatch.rs` at the pin: `record_order_update`, `record_order_update_from_fill`, `defer_replace_cancel`),
the client keeps each order's current Alpaca order and the ones it has superseded: a replacement is adopted
once, by `replaced` or by its own first update, whichever comes first, so a fill on it is never applied to
the old terms; and a late cancel or expiry of a superseded order is ignored. A replacement is traced to the
root of its `replaces` chain, cached even when the node does not hold that root yet, and reported under
it unless Alpaca rejected it. `pending_replace` is open, at the order's own terms ("waiting to be
replaced"). A replacement rejected after its old order filled leaves the fill as the outcome:
NautilusTrader drops a modify rejection for a filled order (observed).

Order events follow NautilusTrader's execution policies ("Command outcomes",
https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md): an order this slice cannot send is
denied before it is submitted (`OrderDenied`, no `OrderSubmitted`); one Alpaca refuses with a documented
status (403 or 422 on "Create an Order", https://docs.alpaca.markets/us/reference/postorder.md) is
rejected; a transport error or any other status leaves the outcome unknown and the order `SUBMITTED`, for
the trade updates stream or reconciliation to resolve. A cancel answered 204 waits for Alpaca's `canceled`
trade update; one answered 422 ("Delete Order by ID", https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md)
is a cancel rejection. Cancel-all cancels the node's open and in-flight orders for the command's instrument,
and side if it names one, each by ID through the same path, so a refused one gets `OrderCancelRejected`
("Cancel, cancel-all, or batch cancel": `OrderCancelRejected`, same policies page, at the pinned commit).
Alpaca's "Delete All Orders" (https://docs.alpaca.markets/us/reference/deleteallorders-1.md) "Attempts to
cancel all open orders" of the account, wider than the command, so it is not called; upstream's Kraken spot
adapter, whose venue cancel-all is account-wide too, selects and cancels by ID the same way
(`crates/adapters/kraken/src/execution/spot.rs` at the pinned commit). Fills come only from the stream's
`fill` and `partial_fill` events, whose `qty` and
`price` are "the number of shares" and "the price per share" for that fill event, not cumulative
(https://docs.alpaca.markets/us/docs/websocket-streaming.md).

Reconciliation (https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md): order status
reports come from Alpaca's orders endpoint and position reports from its positions endpoint. Fill reports
come from Alpaca's account activities of type `FILL`, `GET /v2/account/activities/FILL` with the paper
server listed (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md), called
through alpaca-py's generic `RESTClient.get` ("Performs a single GET request"), since 0.45.0's
`TradingClient` has no method for it (ADR 0024). Each report's trade ID is the UUID after `::` in the
activity's `id`, which a paper test matched to the stream's `execution_id` for every fill, so the engine's
duplicate fill detection sees one fill once ("Duplicate fill detection", `docs/concepts/execution/index.md`
at the pinned commit). With fill reports, the engine no longer "Infers `OrderFilled` events for missing
trade reports" for orders whose fills Alpaca lists.

Venue side, alpaca-py 0.45.0:

* `TradingClient(api_key, secret_key, paper=True)` for `submit_order`, `cancel_order_by_id`,
  `replace_order_by_id`, `get_order_by_id`, `get_order_by_client_id`, `get_orders`, `get_all_positions`,
  `get_account` and `get` (fill activities only). The
  client is always built with `paper=True` and never with `url_override`, whatever the config holds; the
  config refuses both (`AlpacaExecutionClientConfig`). alpaca-py's REST client is synchronous, so each call
  runs in a worker thread, one at a time on its one `requests.Session`, as in `data.py`.
* `TradingStream(api_key, secret_key, paper=True)` for the trade updates stream. Like the market data
  stream, its `run()` drives its own asyncio loop, so it runs on a thread this client owns, started in
  `_connect` after the handler is subscribed (before `run()`, `subscribe_trade_updates` only records the
  handler) and stopped and joined in `_disconnect`. Updates are handed to the node's loop, where every
  event is generated; the handler holds the client weakly (`data.weak_coroutine`).

Known limits:

* Market, limit, stop and stop-limit orders only, `DAY` or `GTC` only, whole shares only, simple orders
  only. Alpaca marks IOC, FOK, OPG and CLS for whole-share market and limit orders "Yes*", and "Please
  contact the sales team for any TIF marked with a *", and for stop and stop-limit orders "No" ("Orders at
  Alpaca", https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Order Types vs Supported Time in
  Force"), so they are denied.
* Stop and stop-limit orders: a stop "is elected on the consolidated print" (same page, "Order Handling
  Standards"), the last trade, so only `DEFAULT` and `LAST_PRICE` trigger types are sent (both as Alpaca's
  stop; Alpaca has no trigger type field), and any other is denied. Upstream's Kraken spot adapter maps
  `DEFAULT` and `LAST_PRICE` to its last-trade trigger too, and refuses what its venue has no trigger for.
  Alpaca's extended hours table
  gives stop and stop-limit orders "No" in every time in force; a stop marked eligible is denied (below,
  "Extended hours"). The stop price and a stop-limit's limit price follow the sub-penny rule ("Sub-penny
  increments for stop orders"). Alpaca documents no status or trade update for a stop's election, so no
  `OrderTriggered` is generated: an elected stop-limit stays `ACCEPTED` until it fills or ends. Trailing
  stops stay denied: their stop price moves with Alpaca's high-water mark and no update reports it.
* Post-only, reduce-only, quote-quantity, display quantity, contingent orders and order lists are denied
  or not implemented; so are queries.
* Extended hours (ADR 0038): `submit_order(order, params={"extended_hours": True})` sends a limit order,
  `DAY` or `GTC`, with Alpaca's `extended_hours` set; any other order so marked, an unknown params key and
  a non-boolean value are denied before Alpaca ("Only limit orders with `time_in_force` set to `day` or
  `gtc` orders are accepted as extended hours eligible", "Orders at Alpaca", "Extended Hours Trading").
  Its trade updates are mapped as any order's. A `day` order placed overnight stays active through the next
  trading day and "If unfilled, the order is canceled at 8:00 PM ET" (Alpaca's 24/5 page); which update
  that cancel sends is not documented.
* Cancel-all sends one cancel per order, one at a time, in no specified order, skipping an order already
  pending cancel; an order placed after the command was read from the cache is not canceled by it.
* A limit price at or above $1.00 with more than 2 decimals, or below $1.00 with more than 4, is denied
  here: Alpaca rejects it ("Sub-penny increments for limit orders", same page).
* The account is a NautilusTrader cash account holding Alpaca's `cash` as its USD balance, all of it free,
  read at connect only; cash finer than a cent is reported to the cent with a warning. Buying power, margin and shorting are not modelled.
* No commission: Alpaca's trade updates carry none, so fills pass `None` and NautilusTrader's own fallback
  applies. No liquidity side: Alpaca reports none, so fills are `NO_LIQUIDITY_SIDE`.
* A fill's trade ID is Alpaca's `execution_id`, which the stream page shows in its example but does not
  define. A fill without one is not emitted; it is logged, and reconciliation can infer it later.
* A fill price is kept exactly, at whatever precision Alpaca sends; the paper example has seven decimals.
* Trade updates for orders this node does not hold are logged and dropped; reconciliation creates them.
* An Alpaca order event this slice does not map (`pending_cancel`, `done_for_day`, `stopped`,
  `calculated`, `suspended`, `held`, `restated`) is logged as a warning; `pending_replace` is ignored.
  NautilusTrader generates `OrderPendingCancel` and `OrderPendingUpdate` itself when a command is sent.
* The stream's subscription is not acknowledged through alpaca-py's interface, so trade updates sent before
  it is listening are missed; reconciliation's checks then resolve the order. If the stream ends by itself
  the client logs an error and does not restart it, as in `data.py`.
* Reports: the orders submitted in the engine's window plus every open order, since Alpaca filters on
  submission time; at most 500 orders per request (`GetOrdersRequest.limit`, "max is 500"), the newest
  first; a full page is logged as possibly truncated. An order Alpaca reports with a type, side, class, status,
  quantity or time in force this slice does not map fails the report when it is open and is skipped with a
  warning when it is closed.
* alpaca-py retries a request answered 429 or 504 up to 3 times (`RESTClient._request`), a submit or
  cancel included, and the client cannot turn that off. What Alpaca answers a repeated `client_order_id`
  is not documented on the pages read (UNVERIFIED), so a submit refused with 403 or 422 is rejected only
  once Alpaca shows it does not hold the order, and a cancel refused with 422 only once Alpaca shows the
  order neither canceled nor pending cancel.
* alpaca-py sets no HTTP timeout; the client's `TradingClient` gets one, (3.05, 27) s, from a transport
  adapter on alpaca-py's private `_session` (`providers.with_http_timeout`, ADR 0039). A connect timeout
  on a call's only attempt proves nothing was sent, so a submit is rejected, a modify modify-rejected and
  a cancel cancel-rejected, each "not sent" (NautilusTrader's policies, "Command outcomes"); any other
  timeout is an unknown outcome, as above.
* Start-up fails, by design, on an open order or position this slice cannot represent: a fractional
  position, or an open order that is not a simple whole-share US equity market, limit, stop or stop-limit
  order. An open stop is reported with its stop price as the trigger price and the node's trigger type, else
  `DEFAULT`.
* Timestamps lose sub-microsecond precision: alpaca-py parses them into `datetime`.
"""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Protocol
from typing import cast
from typing import override
from urllib.parse import urlsplit

from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import OrderType as AlpacaOrderType
from alpaca.trading.enums import PositionSide as AlpacaPositionSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.requests import StopOrderRequest
from alpaca.trading.stream import TradingStream
from nautilus_trader.common import Logger
from nautilus_trader.config import ExecutionClientConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.live.clients import ExecutionClient
from nautilus_trader.model import AccountBalance
from nautilus_trader.model import AccountType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Equity
from nautilus_trader.model import FillReport
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderStatusReport
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId

from .constants import ACCOUNT_ID
from .constants import PRICE_PRECISION
from .constants import USD
from .constants import VENUE
from .data import STREAM_JOIN_TIMEOUT_SECS
from .data import post
from .data import weak_callback
from .data import weak_coroutine
from .providers import AlpacaInstrumentProvider
from .providers import counted
from .providers import credentials_from_env
from .providers import not_sent
from .providers import with_http_timeout

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from uuid import UUID

    from alpaca.common.types import RawData
    from alpaca.trading.requests import OrderRequest
    from nautilus_trader import live
    from nautilus_trader.common import Clock
    from nautilus_trader.live import ClientCache
    from nautilus_trader.model import TraderId

PAPER_ONLY_RULE = "paper trading only: agents never place, modify or cancel live orders (AGENTS.md rule 8, ADR 0009)"

LIVE_HOSTS = frozenset(
    host
    for host in (
        urlsplit(BaseURL.TRADING_LIVE.value).hostname,
        urlsplit(BaseURL.TRADING_STREAM_LIVE.value).hostname,
    )
    if host
)
"""Alpaca's live trading hosts, from the pinned alpaca-py's `BaseURL` (`TRADING_LIVE`, `TRADING_STREAM_LIVE`)."""

ACTIVITIES_PATH = "/account/activities/FILL"
"""Alpaca's "Retrieve Account Activities of Specific Type" for fills, `GET /v2/account/activities/FILL`;
alpaca-py's `RESTClient.get` adds the `/v2` (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md)."""

ACTIVITIES_PAGE_SIZE = 100
"""The reference's `page_size` maximum."""

FILL_PAGES_LIMIT = 10
"""Pages of fills read per report request before stopping with a warning: 1,000 fills, twice the 500 orders
`ORDERS_PAGE_LIMIT` lets the order reports hold."""

LAG_CHECK_NANOS = 60_000_000_000
"""How recent an order's newest listed fill must be for the client to read the order and check that every
fill is listed: on paper the activities lagged the stream by about a second (package README)."""

ORDERS_PAGE_LIMIT = 500
"""The most orders one `get_orders` call returns: `GetOrdersRequest.limit` "Defaults to 50 and max is 500"."""

REASON_MAX_CHARS = 200
"""Venue text in a rejection reason is cut to this length; NautilusTrader's policies ask adapters to "bound
untrusted venue text before emission"."""

DEFINITIVE_SUBMIT_REFUSALS = frozenset({403, 422})
"""Statuses "Create an Order" documents for a refused order: 403 "Buying power or shares is not
sufficient", 422 "Input parameters are not recognized". Any other failure leaves the outcome unknown."""

NOT_CANCELABLE = 422
""""Delete Order by ID": "If the order is no longer cancelable, the request will be rejected with status
422; otherwise accepted with return status 204"."""

REPLACE_REFUSAL = 422
"""The status a paper observation saw for a refused replace ("client_order_id must be unique", "order already
replaced"); "Replace Order by ID" documents no error status, so any other leaves the outcome unknown."""

MAX_REPLACE_CHAIN = 16
"""The most `replaces` links followed back from an order the node does not hold to one it does."""

ORDER_SIDES = {OrderSide.BUY: AlpacaOrderSide.BUY, OrderSide.SELL: AlpacaOrderSide.SELL}
ORDER_SIDES_FROM_ALPACA = {v: k for k, v in ORDER_SIDES.items()}

TIMES_IN_FORCE = {TimeInForce.DAY: AlpacaTimeInForce.DAY, TimeInForce.GTC: AlpacaTimeInForce.GTC}
"""The times in force Alpaca offers whole-share market, limit, stop and stop-limit orders without a sales
contact."""
TIMES_IN_FORCE_FROM_ALPACA = {v: k for k, v in TIMES_IN_FORCE.items()}

ORDER_TYPES_FROM_ALPACA = {
    AlpacaOrderType.MARKET: OrderType.MARKET,
    AlpacaOrderType.LIMIT: OrderType.LIMIT,
    AlpacaOrderType.STOP: OrderType.STOP_MARKET,
    AlpacaOrderType.STOP_LIMIT: OrderType.STOP_LIMIT,
}

ORDER_TYPES = frozenset(ORDER_TYPES_FROM_ALPACA.values())
STOP_TYPES = frozenset({OrderType.STOP_MARKET, OrderType.STOP_LIMIT})
LIMIT_PRICED = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})

STOP_TRIGGER_TYPES = frozenset({TriggerType.DEFAULT, TriggerType.LAST_PRICE})
"""Alpaca elects a stop "on the consolidated print" ("Orders at Alpaca", "Order Handling Standards"): the
last trade. `DEFAULT` is "the venue's default trigger type"; `LAST_PRICE` "Uses the last traded price"."""

OPEN_STATUSES = {
    AlpacaOrderStatus.NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PENDING_NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
    # "The order is waiting to be replaced by another order" ("Order Lifecycle"): still working, at its
    # own terms, until `replaced`.
    AlpacaOrderStatus.PENDING_REPLACE: OrderStatus.ACCEPTED,
}
"""Alpaca's open order statuses this slice maps. `new`, `accepted` and `pending_new` all mean Alpaca has
received the order ("Order Lifecycle" on the stream page and "Get Order by ID"); NautilusTrader's
`ACCEPTED` is the venue's acknowledgement that it is "received and valid"."""

CLOSED_STATUSES = {
    AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
    AlpacaOrderStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
}

ACCEPTING_EVENTS = frozenset({"new", "accepted", "pending_new"})
FILL_EVENTS = frozenset({"fill", "partial_fill"})
TERMINAL_EVENTS = frozenset({"fill", "canceled", "expired", "rejected"})


class PaperOnlyError(ValueError):
    """A setting that would point the execution client anywhere but Alpaca's paper Trading API."""


class AlpacaExecutionClientConfig(ExecutionClientConfig):
    """Configuration of the Alpaca execution client: the common fields only, and paper trading only.

    The adapter guide asks for adapter fields as keyword arguments of a Python `__init__`, with `**kwargs`
    for the common fields ("Define adapter configuration"). This config has no adapter fields: the client
    always builds alpaca-py's clients with `paper=True` and no `url_override`. Its `__init__` refuses what
    would mean otherwise, naming the rule:

    * `paper` other than `True`;
    * `url_override` in any form, and say so when it names a live host;
    * any other keyword, since NautilusTrader's native `ExecutionClientConfig` drops an unknown keyword
      without a word (observed with v2.0.0rc8), and a value naming a live host gets the paper-only error.
    """

    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset({"instrument_provider", "routing"})

    def __init__(
        self, *, paper: object = True, url_override: object = None, **kwargs: object
    ) -> None:
        if paper is not True:
            raise PaperOnlyError(
                f"AlpacaExecutionClientConfig refuses paper={paper!r}: {PAPER_ONLY_RULE}"
            )
        if url_override is not None:
            where = "Alpaca's live trading host" if names_live_host(url_override) else "a URL"
            raise PaperOnlyError(
                f"AlpacaExecutionClientConfig refuses url_override={url_override!r} ({where}): the client "
                f"connects to Alpaca's paper Trading API only; {PAPER_ONLY_RULE}",
            )
        for key, value in kwargs.items():
            if key in self.COMMON_FIELDS:
                continue
            if names_live_host(value):
                raise PaperOnlyError(
                    f"AlpacaExecutionClientConfig refuses {key}={value!r}, Alpaca's live trading host: "
                    f"{PAPER_ONLY_RULE}",
                )
            raise TypeError(
                f"AlpacaExecutionClientConfig has no setting {key!r}: it takes only "
                f"{sorted(self.COMMON_FIELDS)}, and NautilusTrader would drop an unknown one silently",
            )


def names_live_host(value: object) -> bool:
    """Whether `value` names one of Alpaca's live trading hosts, as a URL or a bare host name.

    Host names are compared whole, so `paper-api.alpaca.markets` is not taken for `api.alpaca.markets`.
    """
    return any(token in LIVE_HOSTS for token in re.findall(r"[a-z0-9.-]+", str(value).lower()))


class OrdersClient(Protocol):
    """The part of alpaca-py's `TradingClient` this client uses (signatures as in alpaca-py 0.45.0)."""

    def submit_order(self, order_data: OrderRequest) -> AlpacaOrder | RawData: ...

    def cancel_order_by_id(self, order_id: UUID | str) -> None: ...

    def get_order_by_id(self, order_id: UUID | str) -> AlpacaOrder | RawData: ...

    def get_order_by_client_id(self, client_id: str) -> AlpacaOrder | RawData: ...

    def get_orders(self, filter: GetOrdersRequest | None = None) -> list[AlpacaOrder] | RawData: ...

    def get_all_positions(self) -> list[AlpacaPosition] | RawData: ...

    def get_account(self) -> TradeAccount | RawData: ...

    def replace_order_by_id(
        self, order_id: UUID | str, order_data: ReplaceOrderRequest | None = None
    ) -> AlpacaOrder | RawData: ...

    def get(self, path: str, data: dict | str | None = None) -> Any: ...


type TradeUpdateHandler = Callable[[TradeUpdate | RawData], Awaitable[None]]


class TradeUpdateStream(Protocol):
    """The part of alpaca-py's `TradingStream` this client uses."""

    def subscribe_trade_updates(self, handler: TradeUpdateHandler) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


type StreamExit = tuple[TradeUpdateStream, BaseException | None]


def run_trade_stream(
    stream: TradeUpdateStream,
    loop: asyncio.AbstractEventLoop,
    on_exit: Callable[[StreamExit], None],
) -> None:
    """Run alpaca-py's trade stream on the calling thread and report, naming the stream, when it returns."""
    try:
        stream.run()
    except BaseException as e:
        post(loop, on_exit, (stream, e))
        raise
    post(loop, on_exit, (stream, None))


def release_after[R](
    loop: asyncio.AbstractEventLoop, lock: asyncio.Lock, fn: Callable[..., R], *args: object
) -> R:
    """In a worker thread: return `fn(*args)`, then release `lock` on the node's loop, however `fn` ends."""
    try:
        return fn(*args)
    finally:
        try:
            loop.call_soon_threadsafe(lock.release)
        except RuntimeError:
            pass  # the loop is closed, and the lock with it


def not_sent_reason(error: BaseException) -> str:
    """The reason for a command a connect timeout proves was never sent (ADR 0039)."""
    return f"not sent: Alpaca's API did not accept the connection ({bounded(repr(error))})"


def bounded(text: str) -> str:
    """Venue text cut to `REASON_MAX_CHARS`."""
    return text if len(text) <= REASON_MAX_CHARS else text[: REASON_MAX_CHARS - 1] + "…"


def api_error_text(error: APIError) -> str:
    """Alpaca's error message, without its JSON wrapper when it has one, bounded."""
    try:
        message = str(error.message)
    except ValueError, KeyError, TypeError:
        message = str(error)
    return bounded(message)


def whole_quantity(value: object, field: str) -> Quantity:
    """A whole-share quantity from an Alpaca decimal string or number, refusing a fractional one."""
    amount = Decimal(str(value))
    if amount != amount.to_integral_value():
        raise ValueError(f"{field} {value} is fractional; Equity is whole-share only")
    return Quantity(int(amount), 0)


def exact_report_price(value: object) -> Price:
    """A price for a report, at the adapter's precision, refusing a missing one and one that would need
    rounding."""
    if value is None:
        raise ValueError("no price")
    amount = Decimal(str(value))
    if amount != amount.quantize(Decimal(1).scaleb(-PRICE_PRECISION)):
        raise ValueError(f"price {value} is finer than {PRICE_PRECISION} decimals")
    return Price(float(amount), PRICE_PRECISION)


def wire_price(price: Price) -> float:
    """A NautilusTrader price as alpaca-py's request classes take it: their price fields are floats."""
    return float(Decimal(str(price)))


def fill_price(value: float) -> Price:
    """The fill price exactly as Alpaca sent it: the shortest decimal form of alpaca-py's float."""
    return Price.from_str(repr(float(value)))


def sub_penny_denial(price: Price, what: str = "limit price") -> str | None:
    """Why Alpaca would reject this limit or stop price, or None: 2 decimals at or above $1.00, 4 below."""
    amount = Decimal(str(price))
    places = 2 if amount >= 1 else 4
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        return (
            f"{what} {amount.normalize()} has more than {places} decimals, which Alpaca rejects "
            f"for a price {'at or above' if amount >= 1 else 'below'} $1.00"
        )
    return None


EXTENDED_HOURS = "extended_hours"
"""The submit parameter that makes an order eligible outside the regular session. Venue-specific order
parameters travel in the command's `params` (adapter guide: commands "preserve native identifiers and
params"), as upstream's Bybit, Kraken and OKX adapters read theirs and `ExecTesterConfig.order_params`
sends them; the name is Alpaca's own field ("Orders at Alpaca", "Extended Hours Trading")."""


def extended_hours_param(params: object) -> tuple[bool, str | None]:
    """The submit's `extended_hours` flag, and why the params are refused, if they are: a parameter this
    client does not take is refused rather than dropped."""
    if not params:
        return False, None
    if not isinstance(params, dict):
        return False, f"order params {params!r} are not a mapping"
    unknown = sorted(str(k) for k in params if k != EXTENDED_HOURS)
    if unknown:
        return False, f"order params {unknown} are not taken; only {EXTENDED_HOURS!r}"
    value = params.get(EXTENDED_HOURS, False)
    if not isinstance(value, bool):
        return False, f"{EXTENDED_HOURS} must be true or false, got {value!r}"
    return value, None


def extended_hours_denial(order: Any) -> str | None:
    """Why Alpaca would reject this order as extended-hours eligible, or None: "Only limit orders with
    `time_in_force` set to `day` or `gtc` orders are accepted as extended hours eligible. All other order
    types and TIF values will be rejected with an error" ("Orders at Alpaca", "Extended Hours Trading")."""
    if order.order_type != OrderType.LIMIT:
        return f"{EXTENDED_HOURS} is for limit orders only at Alpaca, not {order.order_type.name}"
    return None  # `_denial` has already refused a time in force other than `DAY` and `GTC`


def replaces_as_asked(replacement: AlpacaOrder, request: ReplaceOrderRequest, order: Any) -> bool:
    """Whether Alpaca's replacement has the terms the replace request asked for, a quantity the request
    leaves out being the node order's own: a request that omits a field asks to keep it."""
    pairs = (
        (request.qty if request.qty is not None else order.quantity, replacement.qty),
        (request.limit_price, replacement.limit_price),
        (request.stop_price, replacement.stop_price),
    )
    return all(
        asked is None or (held is not None and Decimal(str(held)) == Decimal(str(asked)))
        for asked, held in pairs
    )


def alpaca_side(order: AlpacaOrder) -> OrderSide:
    side = ORDER_SIDES_FROM_ALPACA.get(order.side) if order.side is not None else None
    if side is None:
        raise ValueError(f"side {getattr(order.side, 'value', order.side)}")
    return side


def order_status_report(
    order: AlpacaOrder,
    ts_init: int,
    trigger_type: TriggerType | None = None,
    client_order_id: ClientOrderId | None = None,
) -> OrderStatusReport | None:
    """An `OrderStatusReport` for one Alpaca order, None for a closed one this slice does not map.

    An open order this slice does not map raises, so reconciliation fails rather than leaving a working
    order unknown to the node. `replaced` is skipped: the order replacing it is reported in its own right,
    under `client_order_id` when the client has traced it back to the node's order. A stop's trigger type
    is `trigger_type` when the node holds the order, else `DEFAULT`: Alpaca's order has no trigger type
    field.
    """
    status = order.status
    is_open = status not in CLOSED_STATUSES and status not in (AlpacaOrderStatus.REPLACED,)
    try:
        if status == AlpacaOrderStatus.REPLACED:
            raise ValueError("status replaced: its replacement is reported instead")
        nautilus_status = OPEN_STATUSES.get(status) or CLOSED_STATUSES.get(status)
        if nautilus_status is None:
            raise ValueError(f"status {status.value}")
        if order.asset_class not in (None, AssetClass.US_EQUITY):
            raise ValueError(
                f"asset class {getattr(order.asset_class, 'value', order.asset_class)}"
            )
        if order.order_class not in (AlpacaOrderClass.SIMPLE, AlpacaOrderClass.EMPTY):
            raise ValueError(f"order class {order.order_class.value}")
        order_type = ORDER_TYPES_FROM_ALPACA.get(order.type) if order.type is not None else None
        if order_type is None:
            raise ValueError(f"order type {getattr(order.type, 'value', order.type)}")
        time_in_force = TIMES_IN_FORCE_FROM_ALPACA.get(order.time_in_force)
        if time_in_force is None:
            raise ValueError(f"time in force {order.time_in_force.value}")
        if order.symbol is None or order.qty is None:
            raise ValueError("no symbol or no share quantity (a notional order)")
        quantity = whole_quantity(order.qty, "quantity")
        filled_qty = whole_quantity(order.filled_qty or 0, "filled quantity")
        filled = filled_qty.as_double() > 0
        return OrderStatusReport(
            account_id=ACCOUNT_ID,
            instrument_id=InstrumentId(Symbol(order.symbol), VENUE),
            venue_order_id=VenueOrderId(str(order.id)),
            order_side=alpaca_side(order),
            order_type=order_type,
            time_in_force=time_in_force,
            # `pending_replace` is open at its own terms, partly filled if it has fills.
            order_status=OrderStatus.PARTIALLY_FILLED
            if status == AlpacaOrderStatus.PENDING_REPLACE and filled
            else nautilus_status,
            quantity=quantity,
            filled_qty=filled_qty,
            ts_accepted=dt_to_unix_nanos(order.submitted_at),
            ts_last=dt_to_unix_nanos(order.updated_at),
            ts_init=ts_init,
            client_order_id=client_order_id or ClientOrderId(order.client_order_id),
            price=exact_report_price(order.limit_price) if order_type in LIMIT_PRICED else None,
            trigger_price=exact_report_price(order.stop_price)
            if order_type in STOP_TYPES
            else None,
            trigger_type=(trigger_type or TriggerType.DEFAULT)
            if order_type in STOP_TYPES
            else None,
            avg_px=Decimal(str(order.filled_avg_price))
            if filled and order.filled_avg_price
            else None,
        )
    except ValueError as e:
        if is_open:
            raise ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}") from e
        return None


def fill_time(activity: dict) -> int:
    """An activity's `transaction_time`, "The time at which the execution occurred", in Unix nanoseconds."""
    return dt_to_unix_nanos(datetime.fromisoformat(str(activity["transaction_time"])))


def fill_report(activity: dict, ts_init: int) -> FillReport:
    """A `FillReport` for one Alpaca `FILL` activity ("Retrieve Account Activities of Specific Type").

    The trade ID is the UUID after `::` in the activity's `id`, which on paper is the trade update's
    `execution_id` for the same fill (ADR 0024), so a fill seen on the stream and again here has one ID.
    The price is kept exactly as Alpaca writes it. Alpaca's fill has no commission and no liquidity side:
    the commission is zero dollars, which upstream's execution spec expects a stream that omits fees to
    show ("TC-E85"), and the side `NO_LIQUIDITY_SIDE`, as on the stream. An activity has no client order
    ID; the engine matches the report by `order_id`. Raises `ValueError` for one this slice cannot map.
    """
    activity_id = str(activity["id"])
    _, sep, execution_id = activity_id.partition("::")
    if not sep or not execution_id:
        raise ValueError(f"id {activity_id} is not in `::` form")
    side = {"buy": OrderSide.BUY, "sell": OrderSide.SELL, "sell_short": OrderSide.SELL}.get(
        activity["side"]
    )
    if side is None:
        raise ValueError(f"side {activity['side']}")
    symbol = str(activity["symbol"])
    if "/" in symbol:
        raise ValueError(f"symbol {symbol} is not a US equity")
    return FillReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(symbol), VENUE),
        venue_order_id=VenueOrderId(str(activity["order_id"])),
        trade_id=TradeId(execution_id),
        order_side=side,
        last_qty=whole_quantity(activity["qty"], "fill quantity"),
        last_px=Price.from_str(str(activity["price"])),
        commission=Money(0, USD),
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        ts_event=fill_time(activity),
        ts_init=ts_init,
    )


def position_status_report(position: AlpacaPosition, ts_init: int) -> PositionStatusReport:
    """A `PositionStatusReport` for one Alpaca US equity position.

    The direction is taken from `side`; Alpaca documents `qty` as "The number of shares" without saying
    whether a short is negative, so its magnitude is used. Alpaca gives positions no timestamp, so
    `ts_last` is the time of the request.
    """
    side = {
        AlpacaPositionSide.LONG: PositionSide.LONG,
        AlpacaPositionSide.SHORT: PositionSide.SHORT,
    }[position.side]
    return PositionStatusReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(position.symbol), VENUE),
        position_side=side,
        quantity=whole_quantity(abs(Decimal(position.qty)), f"{position.symbol} position quantity"),
        ts_last=ts_init,
        ts_init=ts_init,
        avg_px_open=Decimal(position.avg_entry_price),
    )


class AlpacaExecutionClient(ExecutionClient):
    """Send market, limit, stop and stop-limit orders for US equities to Alpaca's paper Trading API and
    report their events."""

    def __init__(
        self,
        *,
        name: str,
        config: ExecutionClientConfig,
        cache: ClientCache,
        clock: Clock,
        trader_id: TraderId,
        instrument_provider: AlpacaInstrumentProvider,
        trading_client: OrdersClient | None = None,
        stream: TradeUpdateStream | None = None,
    ) -> None:
        """Retain configuration and local state. The alpaca-py clients are built in `_connect`.

        Only an `AlpacaExecutionClientConfig` is accepted, so the paper-only check always runs.
        """
        if not isinstance(config, AlpacaExecutionClientConfig):
            raise TypeError(
                f"AlpacaExecutionClient takes an AlpacaExecutionClientConfig, got {type(config).__name__}: "
                f"its checks hold the rule {PAPER_ONLY_RULE}",
            )
        super().__init__(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            trader_id=trader_id,
            venue=VENUE,
            account_id=ACCOUNT_ID,
            account_type=AccountType.CASH,
            oms_type=OmsType.NETTING,
            base_currency=USD,
            instrument_provider=instrument_provider,
        )
        self._provider = instrument_provider
        self._trading = trading_client
        self._stream = stream
        self._stream_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        # One alpaca-py call at a time on its one requests.Session (see data.py); rebuilt in `_connect`.
        self._rest_lock = asyncio.Lock()
        # Orders this client has seen Alpaca acknowledge, and orders it has seen reach a final state, so
        # an acknowledgement is generated once and nothing is generated after the end.
        self._accepted: set[ClientOrderId] = set()
        self._finished: set[ClientOrderId] = set()
        # A replacement's Alpaca client order ID, mapped to the node's order it replaces: a cache of the
        # `replaces` chain, which is walked again when it misses (a new client after a restart).
        self._replacements: dict[str, ClientOrderId] = {}
        # PATCH answers by the replacement's order ID, read when Alpaca's `replaced` update arrives.
        self._answers: dict[str, AlpacaOrder] = {}
        # Replaces sent and not yet answered by an outcome, by the Alpaca order each one targeted: each
        # modify gets one outcome, though Alpaca can refuse it both on the HTTP answer and on the stream.
        self._modifying: dict[str, ClientOrderId] = {}
        # The Alpaca order each node order was last moved to by `OrderUpdated`, and the Alpaca orders
        # since replaced, each with its node order: a replacement is adopted once, whether its first
        # update or Alpaca's `replaced` comes first, and a late update of a replaced order is ignored.
        self._adopted: dict[ClientOrderId, str] = {}
        self._superseded: dict[str, ClientOrderId] = {}
        # Trade updates, applied in arrival order by one task on the node's loop.
        self._updates: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._consumer: asyncio.Task[object] | None = None
        self._applying = False
        self._log = Logger(type(self).__name__)

    # -- lifecycle ---------------------------------------------------------------------------------

    @override
    async def _connect(self) -> None:
        # As in data.py: NautilusTrader logs a failed connect without its message, so it is logged here.
        try:
            await self._connect_venue()
        except Exception as e:
            self._log.error(f"Alpaca execution client failed to connect: {e}")
            raise

    async def _connect_venue(self) -> None:
        self._owner_loop = self.loop
        self._stopping = False
        self._rest_lock = asyncio.Lock()
        if self._trading is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._trading is None:
                self._trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
            if self._stream is None:
                self._stream = TradingStream(api_key=api_key, secret_key=secret_key, paper=True)
        with_http_timeout(
            self._trading
        )  # a client passed in too; refused without a session (ADR 0039)
        await self._provider.initialize()
        for instrument in self._provider.list_all():
            self._handle_instrument(instrument)
        await self._report_account()
        self._updates = asyncio.Queue()
        self._consumer = self.create_task(self._consume_updates(), "alpaca-trade-updates")
        self._stream.subscribe_trade_updates(weak_coroutine(self._on_trade_update))
        self._stream_thread = threading.Thread(
            target=run_trade_stream,
            args=(self._stream, self._owner_loop, weak_callback(self._on_stream_exit)),
            name=f"{self.client_id}-trade-updates",
            daemon=True,
        )
        self._stream_thread.start()

    @override
    async def _disconnect(self) -> None:
        self._stopping = True
        thread = self._stream_thread
        if thread is not None and self._stream is not None:
            self._stream.stop()
            await asyncio.to_thread(thread.join, STREAM_JOIN_TIMEOUT_SECS)
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        # Cleared whether or not the thread stopped, so a later connect builds fresh clients.
        self._stream_thread = None
        self._stream = None
        self._trading = None
        self._accepted.clear()
        self._finished.clear()
        self._replacements.clear()
        self._answers.clear()
        self._modifying.clear()
        self._adopted.clear()
        self._superseded.clear()
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                f"Alpaca trade updates thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
            )

    async def _report_account(self) -> None:
        account = await self._call(self._venue().get_account)
        if not isinstance(account, TradeAccount):
            raise TypeError(
                "alpaca-py returned raw account data; the client must use raw_data=False"
            )
        if account.currency not in (None, "USD"):
            raise ValueError(f"Alpaca account currency is {account.currency}, not USD")
        amount = Decimal(account.cash or "0")
        cents = amount.quantize(Decimal("0.01"))
        if cents != amount:
            # `Money` holds USD at 2 decimals and `Money.from_str` would round without a word.
            self._log.warning(f"Alpaca cash {amount} is reported to NautilusTrader as {cents} USD")
        cash = Money.from_str(f"{cents} USD")
        self.generate_account_state(
            balances=[AccountBalance(cash, Money(0, USD), cash)],
            margins=[],
            reported=True,
            ts_event=self.clock.timestamp_ns(),
            info={
                "account_number": account.account_number,
                "status": account.status.value,
                "equity": account.equity,
                "buying_power": account.buying_power,
                "trading_blocked": account.trading_blocked,
            },
        )

    # -- order commands ----------------------------------------------------------------------------

    @override
    async def _submit_order(self, command: live.SubmitOrder) -> None:
        order = command.order
        extended_hours, denial = extended_hours_param(command.params)
        denial = denial or self._denial(order)
        if denial is None and extended_hours:
            denial = extended_hours_denial(order)
        if denial is not None:
            self.generate_order_denied(order, denial)
            return
        request = self._order_request(order, extended_hours)
        venue = self._venue()  # raises before `OrderSubmitted` when not connected: nothing was sent
        self.generate_order_submitted(order)
        try:
            placed = await self._call(venue.submit_order, request)
        except APIError as e:
            if e.status_code in DEFINITIVE_SUBMIT_REFUSALS:
                await self._refused(order, e)
                return
            self._log_unknown_outcome(
                order.client_order_id, f"HTTP {e.status_code}: {api_error_text(e)}"
            )
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the call's only attempt: nothing reached Alpaca (ADR 0039).
                self._reject(
                    order,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                return
            self._log_unknown_outcome(order.client_order_id, repr(e))
            return
        if not isinstance(placed, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        if placed.status == AlpacaOrderStatus.REJECTED:
            self._reject(
                order,
                "Alpaca rejected the order on submission",
                dt_to_unix_nanos(placed.updated_at),
            )
            return
        self._accept(order, VenueOrderId(str(placed.id)), dt_to_unix_nanos(placed.submitted_at))

    async def _refused(self, order: Any, error: APIError) -> None:
        """Reject an order Alpaca refused, unless the refusal answered alpaca-py's own retry.

        alpaca-py repeats a request answered 429 or 504 (`RESTClient._request`) with the same
        `client_order_id`, so a 403 or 422 can answer the repeat of a submit Alpaca had already taken.
        What Alpaca answers a repeated `client_order_id` is not documented, so the order is looked up
        first: if Alpaca holds it, it is accepted, not rejected; if the lookup fails, the outcome is
        unknown.
        """
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or client_order_id in self._finished:
            return  # a trade update has already shown that Alpaca holds it
        try:
            held = await self._call(self._venue().get_order_by_client_id, client_order_id.value)
        except APIError as e:
            if e.status_code != 404:
                self._log_unknown_outcome(
                    client_order_id,
                    f"refused ({error.status_code}), then the lookup failed (HTTP {e.status_code})",
                )
                return
            held = None
        except OSError as e:
            self._log_unknown_outcome(
                client_order_id, f"refused ({error.status_code}), then the lookup failed ({e!r})"
            )
            return
        if isinstance(held, AlpacaOrder):
            self._log.warning(
                f"Alpaca refused a submit of {client_order_id} ({error.status_code}) but holds the "
                "order: an earlier attempt reached it",
            )
            self._accept(order, VenueOrderId(str(held.id)), dt_to_unix_nanos(held.submitted_at))
            return
        self._reject(
            order,
            f"Alpaca refused the order ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    @override
    async def _cancel_order(self, command: live.CancelOrder) -> None:
        await self._cancel_logged(command.client_order_id, command.venue_order_id)

    async def _cancel_logged(
        self, client_order_id: ClientOrderId, venue_order_id: VenueOrderId | None
    ) -> None:
        try:
            await self._cancel(client_order_id, venue_order_id)
        except (ValueError, TypeError, RuntimeError) as e:
            # `_cancel` handles Alpaca's answers itself; these are a raw or unparsable answer (pydantic's
            # ValidationError is a ValueError) and a client not connected. Logged with no event, so a
            # cancel-all or the default batch cancel goes on to the next order.
            self._log.error(
                f"Alpaca cancel of {client_order_id} failed ({e!r}); the order is left as it is"
            )

    @override
    async def _cancel_all_orders(self, command: live.CancelAllOrders) -> None:
        """Cancel, one at a time by ID, every open or in-flight order the node holds for the command's
        instrument, and side if it names one.

        Alpaca's "Delete All Orders" cancels every open order of the account, whatever its symbol, so it
        is not used: as upstream's Kraken spot adapter does for its account-wide cancel-all, the matching
        orders are taken from the cache and canceled by ID. In-flight orders are included because Alpaca
        may hold an order the node still has as `SUBMITTED`. An order already `PENDING_CANCEL` has a cancel
        in flight and is left to it. A strategy sends this command only with `strategy_only=False`, and
        then means every strategy's orders, so none is filtered by strategy. A failure on one order is
        logged and the rest are still canceled.
        """
        selected: dict[ClientOrderId, None] = {}
        for found in (self.cache.orders_open, self.cache.orders_inflight):
            for order in found(instrument_id=command.instrument_id, side=command.order_side):
                selected.setdefault(order.client_order_id)
        if not selected:
            self._log.info(f"No open or in-flight {command.instrument_id} orders to cancel")
            return
        for client_order_id in selected:
            # Read again at its turn: the cancels before it took an HTTP round trip each, in which this
            # order may have filled, closed or been sent a cancel of its own.
            order = self.cache.order(client_order_id)
            if order is None or order.is_closed or order.status == OrderStatus.PENDING_CANCEL:
                continue
            await self._cancel_logged(client_order_id, order.venue_order_id)

    async def _cancel(
        self, client_order_id: ClientOrderId, venue_order_id: VenueOrderId | None
    ) -> None:
        """Cancel one order by Alpaca's order ID, looking it up by client order ID if the node has none."""
        order = self.cache.order(client_order_id)
        if order is None:
            self._log.error(f"Cannot cancel {client_order_id}: the node holds no such order")
            return
        venue_order_id = venue_order_id or order.venue_order_id
        try:
            if venue_order_id is None:
                found = await self._call(
                    self._venue().get_order_by_client_id, client_order_id.value
                )
                if not isinstance(found, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                venue_order_id = VenueOrderId(str(found.id))
            await self._call(self._venue().cancel_order_by_id, venue_order_id.value)
        except APIError as e:
            if e.status_code == NOT_CANCELABLE and venue_order_id is not None:
                await self._cancel_refused(order, venue_order_id, e)
                return
            self._log.error(
                f"Alpaca cancel for {client_order_id} got no definitive answer "
                f"(HTTP {e.status_code}: {api_error_text(e)}); the order is left as it is",
            )
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the cancel's only attempt: it never reached Alpaca (ADR 0039).
                self.generate_order_cancel_rejected(
                    order,
                    venue_order_id,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                return
            self._log.error(
                f"Alpaca cancel for {client_order_id} got no answer ({e!r}); the order is left as it is",
            )
        # Answered 204: the cancel is confirmed by Alpaca's `canceled` trade update.

    async def _cancel_refused(
        self, order: Any, venue_order_id: VenueOrderId, error: APIError
    ) -> None:
        """Reject a cancel Alpaca refused, unless the refusal answered alpaca-py's own retry.

        As for a submit, a 422 can answer the repeat of a cancel Alpaca had already taken. If Alpaca shows
        the order canceled or pending cancel, the `canceled` trade update confirms it and nothing is
        generated here; if the lookup fails, nothing is either.
        """
        try:
            held = await self._call(self._venue().get_order_by_id, venue_order_id.value)
        except (APIError, OSError) as e:
            self._log.error(
                f"Alpaca refused the cancel of {order.client_order_id} ({error.status_code}) and the "
                f"lookup failed ({e!r}); the order is left as it is",
            )
            return
        if isinstance(held, AlpacaOrder) and held.status in (
            AlpacaOrderStatus.CANCELED,
            AlpacaOrderStatus.PENDING_CANCEL,
        ):
            return
        if (
            isinstance(held, AlpacaOrder)
            and held.status == AlpacaOrderStatus.REPLACED
            and held.replaced_by is not None
        ):
            # The cancel named the order a replace has just replaced; cancel the replacement instead.
            await self._cancel(order.client_order_id, VenueOrderId(str(held.replaced_by)))
            return
        self.generate_order_cancel_rejected(
            order,
            venue_order_id,
            f"Alpaca refused the cancel ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    @override
    async def _modify_order(self, command: live.ModifyOrder) -> None:
        """Replace the order on Alpaca ("Replace Order by ID", `TradingClient.replace_order_by_id`).

        Alpaca answers with "The new Order object with the new order ID" and says a success "does NOT
        guarantee the existing open order has been replaced". So `OrderUpdated` is not generated from the
        answer: it comes with Alpaca's `replaced` trade update for the node's order, which carries the old
        order with `replaced_by` (observed on paper, ADR 0042). The answer is kept for that update's values,
        and the replacement's Alpaca client order ID is mapped to the node's order. Alpaca refuses a
        replacement that keeps the original client order ID (observed), so none is sent.
        """
        order = self.cache.order(command.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot modify {command.client_order_id}: the node holds no such order"
            )
            return
        venue_order_id = command.venue_order_id or order.venue_order_id
        denial = self._modify_denial(order, command, venue_order_id)
        if denial is not None or venue_order_id is None:
            self.generate_order_modify_rejected(
                order, venue_order_id, denial or "no venue order ID", self.clock.timestamp_ns()
            )
            return
        request = ReplaceOrderRequest(
            qty=int(command.quantity.as_double()) if command.quantity is not None else None,
            # "Required if original order's `type` field was `limit` or `stop_limit`" (Replace Order by
            # ID), so the current price is sent when the command does not change it.
            limit_price=wire_price(command.price or order.price)
            if order.order_type in LIMIT_PRICED
            else None,
            stop_price=wire_price(command.trigger_price or order.trigger_price)
            if order.order_type in STOP_TYPES
            else None,
        )
        self._modifying[venue_order_id.value] = order.client_order_id
        try:
            answer = await self._call(
                self._venue().replace_order_by_id, venue_order_id.value, request
            )
        except APIError as e:
            if e.status_code == REPLACE_REFUSAL:
                await self._replace_refused(order, venue_order_id, request, e)
                return
            self._log_unknown_replace(order, f"HTTP {e.status_code}: {api_error_text(e)}")
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the replace's only attempt: it never reached Alpaca (ADR 0039).
                self._modify_rejected(
                    order,
                    venue_order_id.value,
                    venue_order_id,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                return
            self._log_unknown_replace(order, repr(e))
            return
        if not isinstance(answer, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        self._replacements[answer.client_order_id] = order.client_order_id
        if order.client_order_id not in self._finished and self._adopted.get(
            order.client_order_id
        ) != str(answer.id):
            self._answers[str(answer.id)] = answer  # `replaced` takes it; adopted already, not kept

    async def _replace_refused(
        self,
        order: Any,
        venue_order_id: VenueOrderId,
        request: ReplaceOrderRequest,
        error: APIError,
    ) -> None:
        """Reject a modify Alpaca refused, unless the refusal answered alpaca-py's own retry.

        alpaca-py repeats a request answered 429 or 504, and a second replace of an order already replaced
        is answered 422 "order already replaced" (observed). So the order is looked up: replaced by an
        order with the terms this request asked for, the refusal answered the retry and the `replaced`
        trade update brings `OrderUpdated`; replaced with other terms, it was another modify, and this one
        is rejected; if a lookup fails, the outcome is unknown.
        """
        try:
            held = await self._call(self._venue().get_order_by_id, venue_order_id.value)
            replacement = None
            for _ in range(MAX_REPLACE_CHAIN):  # to the live order, as `_replaced` follows it
                if (
                    not isinstance(held, AlpacaOrder)
                    or held.status != AlpacaOrderStatus.REPLACED
                    or held.replaced_by is None
                ):
                    break
                held = replacement = await self._call(
                    self._venue().get_order_by_id, str(held.replaced_by)
                )
        except (APIError, OSError) as e:
            self._log_unknown_replace(
                order, f"refused ({error.status_code}), then the lookup failed ({e!r})"
            )
            return
        if isinstance(replacement, AlpacaOrder) and replaces_as_asked(replacement, request, order):
            self._log.warning(
                f"Alpaca refused a replace of {order.client_order_id} ({error.status_code}) but shows "
                "the order replaced as asked: an earlier attempt reached it",
            )
            return
        self._modify_rejected(
            order,
            venue_order_id.value,
            venue_order_id,
            f"Alpaca refused the replace ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    def _settle_modify(self, target: str) -> bool:
        """Mark the replace that targeted Alpaca order `target` answered; whether one was awaiting it."""
        return self._modifying.pop(target, None) is not None

    def _modify_rejected(
        self,
        order: Any,
        target: str,
        venue_order_id: VenueOrderId | None,
        reason: str,
        ts_event: int,
    ) -> None:
        """`OrderModifyRejected` for the replace this client sent to Alpaca order `target`, once: Alpaca can
        refuse one on the HTTP answer and on the stream, and the second refusal generates nothing."""
        if not self._settle_modify(target):
            self._log.info(
                f"Alpaca refused a replace of {order.client_order_id} ({reason}) with no modify awaiting an "
                "outcome (answered already, or sent before a reconnect); nothing generated",
            )
            return
        self.generate_order_modify_rejected(order, venue_order_id, reason, ts_event)

    def _log_unknown_replace(self, order: Any, detail: str) -> None:
        self._log.error(
            f"Alpaca replace of {order.client_order_id} got no definitive answer ({detail}); a trade "
            "update or reconciliation resolves it",
        )

    @staticmethod
    def _modify_denial(
        order: Any, command: live.ModifyOrder, venue_order_id: VenueOrderId | None
    ) -> str | None:
        """Why the client will not send this modify, or None: only what it can prove. What Alpaca alone
        knows, such as an order in `accepted` that "cannot be replaced", is left to Alpaca's answer."""
        if venue_order_id is None:
            return "Alpaca has not acknowledged the order yet (no venue order ID to replace)"
        if command.quantity is not None and not command.quantity.as_double().is_integer():
            return f"quantity {command.quantity} is fractional; whole shares only"
        if command.price is not None:
            if order.order_type not in LIMIT_PRICED:
                return f"a {order.order_type.name} order has no limit price to change"
            denial = sub_penny_denial(command.price)
            if denial is not None:
                return denial
        if command.trigger_price is not None:
            if order.order_type not in STOP_TYPES:
                return f"a {order.order_type.name} order has no stop price to change"
            return sub_penny_denial(command.trigger_price, "stop price")
        return None

    def _denial(self, order: Any) -> str | None:
        """Why this slice will not send the order, or None. Checked before anything reaches Alpaca."""
        instrument = (
            self._provider.find(order.instrument_id) if order.instrument_id.venue == VENUE else None
        )
        if not isinstance(instrument, Equity):
            return f"{order.instrument_id} is not a loaded {VENUE} equity"
        if not instrument.info.get("tradable", False):
            return f"{order.instrument_id} is not tradable on Alpaca (asset tradable=false)"
        if order.order_type not in ORDER_TYPES:
            return (
                f"order type {order.order_type.name} is not supported; market, limit, stop and "
                "stop-limit only"
            )
        if order.time_in_force not in TIMES_IN_FORCE:
            return f"time in force {order.time_in_force.name} is not supported; DAY or GTC only"
        if order.is_quote_quantity:
            return "quote quantity is not supported; whole shares only"
        if order.is_reduce_only:
            return "reduce-only is not supported"
        if (
            order.order_list_id is not None
            or order.linked_order_ids
            or order.parent_order_id is not None
        ):
            return "contingent orders and order lists are not supported"
        if not order.quantity.as_double().is_integer():
            return f"quantity {order.quantity} is fractional; whole shares only"
        if len(order.client_order_id.value) > 128:
            return "client order ID is longer than Alpaca's 128 characters"
        if order.order_type in STOP_TYPES:
            if order.trigger_type not in STOP_TRIGGER_TYPES:
                return (
                    f"trigger type {getattr(order.trigger_type, 'name', order.trigger_type)} is not "
                    "offered: Alpaca elects a stop on the last trade (DEFAULT or LAST_PRICE)"
                )
            denial = sub_penny_denial(order.trigger_price, "stop price")
            if denial is not None:
                return denial
        if order.order_type in LIMIT_PRICED:
            if order.is_post_only:
                return "post-only is not supported"
            if order.display_qty is not None:
                return "display quantity is not supported"
            return sub_penny_denial(order.price)
        return None

    @staticmethod
    def _order_request(order: Any, extended_hours: bool = False) -> OrderRequest:
        fields: dict[str, Any] = {
            "symbol": order.instrument_id.symbol.value,
            "qty": float(int(order.quantity.as_double())),
            "side": ORDER_SIDES[order.side],
            "time_in_force": TIMES_IN_FORCE[order.time_in_force],
            "client_order_id": order.client_order_id.value,
        }
        if extended_hours:
            fields["extended_hours"] = True  # only ever with a limit order, `DAY` or `GTC`
        if order.order_type == OrderType.LIMIT:
            return LimitOrderRequest(**fields, limit_price=wire_price(order.price))
        if order.order_type == OrderType.STOP_MARKET:
            return StopOrderRequest(**fields, stop_price=wire_price(order.trigger_price))
        if order.order_type == OrderType.STOP_LIMIT:
            return StopLimitOrderRequest(
                **fields,
                stop_price=wire_price(order.trigger_price),
                limit_price=wire_price(order.price),
            )
        return MarketOrderRequest(**fields)

    def _log_unknown_outcome(self, client_order_id: ClientOrderId, detail: str) -> None:
        self._log.error(
            f"Alpaca submit for {client_order_id} got no definitive answer ({detail}); the order stays "
            "SUBMITTED until a trade update or reconciliation resolves it",
        )

    # -- order events, on the node's loop ----------------------------------------------------------

    def _accept(self, order: Any, venue_order_id: VenueOrderId, ts_event: int) -> None:
        """Generate `OrderAccepted` once per order, and only for an order still awaiting it."""
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or client_order_id in self._finished:
            return
        self._accepted.add(client_order_id)
        # The cached order, not the command's snapshot: a cancel may have moved it on since the submit.
        current = self.cache.order(client_order_id) or order
        if current.status in (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED, OrderStatus.RELEASED):
            self.generate_order_accepted(current, venue_order_id, ts_event)

    def _reject(self, order: Any, reason: str, ts_event: int) -> None:
        self._finish(order.client_order_id)
        self.generate_order_rejected(order, reason, ts_event, False)

    async def _consume_updates(self) -> None:
        """Apply trade updates one at a time, in the order the stream delivered them: an update for a
        replacement may need a lookup on Alpaca before it can be applied."""
        while True:
            update = await self._updates.get()
            self._applying = True
            try:
                await self._apply_trade_update(update)
            except Exception as e:  # noqa: BLE001 - one update's failure must not stop the rest
                self._log.error(f"Alpaca trade update {update.event} could not be applied: {e!r}")
            finally:
                self._applying = False

    async def _apply_trade_update(self, update: TradeUpdate) -> None:
        if self._stopping:
            return
        try:
            await self._apply(update)
        except (ValueError, TypeError, RuntimeError, APIError, OSError) as e:
            # Nothing else reports it: name the update and keep the stream.
            self._log.error(
                f"Alpaca trade update {update.event} for {update.order.client_order_id} failed: {e!r}",
            )

    async def _node_order_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId | None:
        """The node's client order ID for an Alpaca order, or None if the node holds none."""
        own = ClientOrderId(alpaca_order.client_order_id)
        if self.cache.order(own) is not None:
            return own
        root = await self._root_id(alpaca_order, known)
        return root if self.cache.order(root) is not None else None

    async def _root_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId:
        """The client order ID of the order an Alpaca order's `replaces` chain starts from.

        A replacement carries a client order ID Alpaca made (observed), so it is traced back through its
        `replaces` links, from the orders already fetched first, at most `MAX_REPLACE_CHAIN` of them.
        Every result is cached, a chain that ends without reaching the node's orders included, so an
        order traced before the node holds its original (the start-up window) resolves once it does, and
        no chain is walked twice; the cache is rebuilt after a restart.
        """
        cached = self._replacements.get(alpaca_order.client_order_id)
        if cached is not None:
            return cached
        root = ClientOrderId(alpaca_order.client_order_id)
        replaces = alpaca_order.replaces
        for _ in range(MAX_REPLACE_CHAIN):
            if replaces is None:
                break
            prior = (known or {}).get(str(replaces))
            if prior is None:
                try:
                    prior = await self._call(self._venue().get_order_by_id, str(replaces))
                except APIError as e:
                    if e.status_code != 404:
                        raise
                    break
            if not isinstance(prior, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            mapped = self._replacements.get(prior.client_order_id)
            if mapped is not None:
                root = mapped
                break
            root = ClientOrderId(prior.client_order_id)
            if self.cache.order(root) is not None:
                break
            replaces = prior.replaces
        else:
            self._log.warning(
                f"Alpaca order {alpaca_order.id} has more than {MAX_REPLACE_CHAIN} replaces; it is traced "
                f"to the order {MAX_REPLACE_CHAIN} links back",
            )
        self._replacements[alpaca_order.client_order_id] = root
        return root

    async def _apply(self, update: TradeUpdate) -> None:
        alpaca_order = update.order
        client_order_id = await self._node_order_id(alpaca_order)
        order = self.cache.order(client_order_id) if client_order_id is not None else None
        if client_order_id is None or order is None:
            self._log.info(
                f"Alpaca trade update {update.event} for order {alpaca_order.id} ({alpaca_order.symbol}), "
                "which this node does not hold; reconciliation reports it",
            )
            return
        event = getattr(update.event, "value", update.event)
        venue_order_id = VenueOrderId(str(alpaca_order.id))
        ts_event = dt_to_unix_nanos(update.timestamp)
        is_replacement = alpaca_order.client_order_id != client_order_id.value
        current = self._adopted.get(client_order_id) or (
            order.venue_order_id.value if order.venue_order_id is not None else None
        )
        taken_over = current == venue_order_id.value
        if event == "rejected" and is_replacement and not taken_over:
            # Alpaca: if the old order "is filled before the replacing (new) order reaches the execution
            # venue, the replacing (new) order is rejected". The node's order is the old one: the modify
            # is what failed. Once that order is filled, NautilusTrader drops a modify rejection for it
            # (observed), so the fill stands as the outcome and the modify is settled with a log line.
            target = str(alpaca_order.replaces)
            if client_order_id in self._finished:
                self._settle_modify(target)
                self._log.info(
                    f"Alpaca rejected the replacement {alpaca_order.id} of {client_order_id}, which had "
                    "filled first; the fill is the outcome",
                )
                return
            self._modify_rejected(
                order,
                target,
                venue_order_id,
                f"Alpaca rejected the replacement order {alpaca_order.id} (trade update)",
                ts_event,
            )
            return
        if client_order_id in self._finished:
            return
        if event == "replaced":
            await self._replaced(order, alpaca_order, ts_event)
            return
        if event == "order_replace_rejected":
            self._drop_answers(lambda answer: str(answer.replaces) == str(alpaca_order.id))
            self._modify_rejected(
                order,
                venue_order_id.value,
                venue_order_id,
                "Alpaca rejected the replace (trade update)",
                ts_event,
            )
            return
        if event == "pending_replace":
            return  # NautilusTrader generates `OrderPendingUpdate` itself when a modify is sent
        if self._superseded.get(venue_order_id.value) == client_order_id:
            if event in FILL_EVENTS:
                # An execution on the order before it was replaced.
                self._fill(order, update, venue_order_id, ts_event)
                if event in TERMINAL_EVENTS:
                    self._finish(client_order_id)
            else:
                self._log.info(
                    f"Alpaca trade update {event} for {venue_order_id}, which {client_order_id} no longer "
                    "names (replaced); ignored",
                )
            return
        if (
            is_replacement
            and not taken_over
            and alpaca_order.replaces is not None
            and event in FILL_EVENTS
        ):
            # A fill on the replacement before Alpaca's `replaced` was applied: an execution proves the
            # replacement live, so the node's order takes its terms first, or the fill could be refused as
            # an overfill (as Binance spot's `record_order_update_from_fill` updates from a fill). Its
            # other updates wait for `replaced`: a replacement may still be rejected.
            self._adopt(order, alpaca_order, ts_event)
        if event == "rejected":
            self._reject(
                order,
                f"Alpaca rejected the order (trade update, status {alpaca_order.status.value})",
                ts_event,
            )
            return
        if event == "order_cancel_rejected":
            self.generate_order_cancel_rejected(
                order, venue_order_id, "Alpaca rejected the cancel (trade update)", ts_event
            )
            return
        if event not in ACCEPTING_EVENTS | FILL_EVENTS | {"canceled", "expired"}:
            self._log.warning(
                f"Alpaca trade update {event} for {client_order_id} is not mapped in this slice; ignored",
            )
            return
        self._accept(order, venue_order_id, ts_event)
        if event in FILL_EVENTS:
            self._fill(order, update, venue_order_id, ts_event)
        elif event == "canceled":
            self.generate_order_canceled(order, venue_order_id, ts_event)
        elif event == "expired":
            self.generate_order_expired(order, venue_order_id, ts_event)
        if event in TERMINAL_EVENTS:
            self._finish(client_order_id)

    def _finish(self, client_order_id: ClientOrderId) -> None:
        """Mark the node's order ended and drop what was kept for its replaces."""
        # A modify still awaiting its outcome stays in `_modifying`: its rejection can follow the fill.
        self._finished.add(client_order_id)
        self._adopted.pop(client_order_id, None)
        for key in [k for k, cid in self._superseded.items() if cid == client_order_id]:
            del self._superseded[key]
        self._drop_answers(
            lambda answer: self._replacements.get(answer.client_order_id) == client_order_id
        )

    def _drop_answers(self, which: Callable[[AlpacaOrder], bool]) -> None:
        for key in [k for k, answer in self._answers.items() if which(answer)]:
            del self._answers[key]

    async def _replaced(self, order: Any, old: AlpacaOrder, ts_event: int) -> None:
        """Alpaca's `replaced` for the node's order: `OrderUpdated` with the replacement's values.

        The update carries the old order, with `replaced_by` (observed). The replacement is the PATCH
        answer if it is in, otherwise read from Alpaca, and followed on if Alpaca has replaced it again
        since; if none yields it, nothing is generated and reconciliation settles the order, so no value
        is invented. A replacement already adopted, by an earlier `replaced` or its own first update, is
        not adopted again.
        """
        if old.replaced_by is None:
            self._log.error(
                f"Alpaca replaced {old.id} without saying by which order; nothing applied"
            )
            return
        self._superseded[str(old.id)] = order.client_order_id
        if self._adopted.get(order.client_order_id) == str(old.replaced_by):
            return  # adopted already, by the replacement's own fill: nothing to read or update
        replacement: AlpacaOrder | RawData | None = self._answers.pop(str(old.replaced_by), None)
        try:
            if replacement is None:
                replacement = await self._call(self._venue().get_order_by_id, str(old.replaced_by))
            for _ in range(MAX_REPLACE_CHAIN):
                if not isinstance(replacement, AlpacaOrder):
                    raise TypeError(
                        "alpaca-py returned raw order data; the client must use raw_data=False"
                    )
                if (
                    replacement.status != AlpacaOrderStatus.REPLACED
                    or replacement.replaced_by is None
                ):
                    break
                # Replaced again since: the live order is further on.
                self._superseded[str(replacement.id)] = order.client_order_id
                replacement = await self._call(
                    self._venue().get_order_by_id, str(replacement.replaced_by)
                )
        except (APIError, OSError) as e:
            self._log.error(
                f"Alpaca replaced {order.client_order_id} by {old.replaced_by}, which could not be read "
                f"({e!r}); no OrderUpdated, reconciliation settles it",
            )
            return
        if not isinstance(replacement, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        self._adopt(order, replacement, ts_event)

    def _adopt(self, order: Any, replacement: AlpacaOrder, ts_event: int) -> None:
        """`OrderUpdated` moving the node's order to Alpaca's replacement, once per replacement."""
        client_order_id = order.client_order_id
        if self._adopted.get(client_order_id) == str(replacement.id):
            return  # adopted already: one transition, one `OrderUpdated`
        self._replacements[replacement.client_order_id] = client_order_id
        if replacement.replaces is not None:
            self._superseded[str(replacement.replaces)] = client_order_id
        report = order_status_report(
            replacement,
            self.clock.timestamp_ns(),
            order.trigger_type if order.order_type in STOP_TYPES else None,
            client_order_id,
        )
        if report is None:
            raise ValueError(f"replacement {replacement.id} is not an order this slice maps")
        self._adopted[client_order_id] = str(replacement.id)
        self._answers.pop(str(replacement.id), None)
        if replacement.replaces is not None:
            self._settle_modify(str(replacement.replaces))
        self.generate_order_updated(
            order,
            VenueOrderId(str(replacement.id)),
            report.quantity,
            report.price,
            report.trigger_price,
            None,
            ts_event,
        )

    def _fill(
        self, order: Any, update: TradeUpdate, venue_order_id: VenueOrderId, ts_event: int
    ) -> None:
        try:
            if update.execution_id is None:
                raise ValueError("it has no execution_id to use as the trade ID")
            if update.qty is None or update.price is None:
                raise ValueError("it has no qty or price")
            last_qty = whole_quantity(update.qty, "fill quantity")
        except ValueError as e:
            self._log.error(
                f"Alpaca {update.event} for {order.client_order_id} not applied: {e}; reconciliation "
                "can infer it from the order's filled quantity",
            )
            return
        self.generate_order_filled(
            order,
            venue_order_id,
            None,
            TradeId(str(update.execution_id)),
            last_qty,
            fill_price(update.price),
            USD,
            None,
            LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event,
        )

    def _on_stream_exit(self, exit: StreamExit) -> None:
        stream, error = exit
        if stream is self._stream and not self._stopping:
            self._log.error(f"Alpaca trade updates stream ended while connected: {error!r}")

    async def _on_trade_update(self, update: TradeUpdate | RawData) -> None:
        """On the stream's thread: hand the update to the node's loop, where it is queued in order."""
        if isinstance(update, TradeUpdate) and self._owner_loop is not None:
            post(self._owner_loop, self._enqueue_update, update)

    def _enqueue_update(self, update: TradeUpdate) -> None:
        if not self._stopping:
            self._updates.put_nowait(update)

    # -- reconciliation reports --------------------------------------------------------------------

    @override
    async def _generate_order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        return await self._logged("order status report", self._order_status_report(command))

    @override
    async def _generate_order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        return await self._logged("order status reports", self._order_status_reports(command))

    @override
    async def _generate_fill_reports(self, command: live.GenerateFillReports) -> list[FillReport]:
        return await self._logged("fill reports", self._fill_reports(command))

    @override
    async def _generate_position_status_reports(
        self, command: live.GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        return await self._logged("position status reports", self._position_status_reports(command))

    async def _logged[R](self, what: str, reports: Awaitable[R]) -> R:
        """Await `reports`, logging why they failed: NautilusTrader v2.0.0rc8 logs a failed report request
        as "Failed to get mass status" without the exception's message (observed)."""
        try:
            return await reports
        except Exception as e:
            self._log.error(f"Alpaca {what} failed: {e}")
            raise

    async def _order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        try:
            if command.venue_order_id is not None:
                found = await self._call(
                    self._venue().get_order_by_id, command.venue_order_id.value
                )
            elif command.client_order_id is not None:
                found = await self._call(
                    self._venue().get_order_by_client_id, command.client_order_id.value
                )
            else:
                raise ValueError("an order status report needs a venue or client order ID")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        for _ in range(MAX_REPLACE_CHAIN):
            if not isinstance(found, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            if found.status != AlpacaOrderStatus.REPLACED or found.replaced_by is None:
                break
            # The node's IDs can name an order since replaced; the live order is its replacement.
            found = await self._call(self._venue().get_order_by_id, str(found.replaced_by))
        if not isinstance(found, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        return await self._order_report(found)

    async def _order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        """Orders submitted in the command's window, and, unless it asks for open orders only, every open
        order whenever it was submitted: Alpaca's `after` and `until` filter on submission time, so a
        window alone would miss a working order placed before it."""
        symbols = (
            [command.instrument_id.symbol.value] if command.instrument_id is not None else None
        )
        windowed = GetOrdersRequest(
            status=QueryOrderStatus.OPEN if command.open_only else QueryOrderStatus.ALL,
            limit=ORDERS_PAGE_LIMIT,
            after=unix_nanos_to_dt(command.start) if command.start is not None else None,
            until=unix_nanos_to_dt(command.end) if command.end is not None else None,
            symbols=symbols,
        )
        requests = [windowed]
        if not command.open_only and (command.start is not None or command.end is not None):
            requests.append(
                GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT, symbols=symbols
                )
            )
        found: dict[str, AlpacaOrder] = {}
        for request in requests:
            orders = await self._call(self._venue().get_orders, request)
            if not isinstance(orders, list):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            if len(orders) >= ORDERS_PAGE_LIMIT:
                self._log.warning(
                    f"Alpaca returned {len(orders)} orders, its most per request; older ones may be "
                    "missing",
                )
            found.update((str(order.id), order) for order in orders)
        reports = []
        for order in found.values():
            report = await self._order_report(order, found)
            if report is not None:
                reports.append(report)
        return reports

    async def _fill_reports(self, command: live.GenerateFillReports) -> list[FillReport]:
        """One report per Alpaca `FILL` activity in the command's window, newest first from Alpaca, or
        none if the history cannot be read.

        The window and the instrument are applied here, on each activity's `transaction_time` and
        `symbol`: what Alpaca's `after` and `until` compare for a fill is not documented (ADR 0024), and
        the endpoint has no symbol filter. Alpaca returns activities newest first by default, so paging
        stops at the first activity before the window's start, or after `FILL_PAGES_LIMIT` pages. If the
        history cannot be read, the failure is logged and no fill is reported, so the engine infers fills
        from the order reports as it did before fill reports existed. An order whose listed fills Alpaca
        shows short of its filled quantity, as when the activities lag the stream, gets no reports
        (`_complete`), so the engine infers its fills rather than taking a part of them.
        """
        try:
            activities = await self._fill_activities(command)
        except (APIError, OSError, TypeError, KeyError) as e:
            self._log.error(
                f"Alpaca fill history could not be read ({e!r}); fills are inferred from the order reports"
            )
            return []
        ts_init = self.clock.timestamp_ns()
        reports: list[FillReport] = []
        for activity in activities:
            try:
                report = fill_report(activity, ts_init)
            except (KeyError, TypeError, ValueError, ArithmeticError) as e:
                name = activity.get("id") if isinstance(activity, dict) else repr(activity)
                self._log.warning(f"Alpaca fill activity {name} is not reported: {e}")
                continue
            if command.start is not None and report.ts_event < command.start:
                continue
            if command.end is not None and report.ts_event > command.end:
                continue
            if command.instrument_id is not None and report.instrument_id != command.instrument_id:
                continue
            if (
                command.venue_order_id is not None
                and report.venue_order_id != command.venue_order_id
            ):
                continue
            reports.append(report)
        return await self._complete(reports, activities, ts_init)

    async def _fill_activities(self, command: live.GenerateFillReports) -> list:
        params: dict[str, str | int] = {"direction": "desc", "page_size": ACTIVITIES_PAGE_SIZE}
        if command.venue_order_id is not None:
            params["order_id"] = command.venue_order_id.value
        activities: list = []
        for _ in range(FILL_PAGES_LIMIT):
            page = await self._call(self._venue().get, ACTIVITIES_PATH, params)
            if not isinstance(page, list):
                raise TypeError(f"Alpaca's activities endpoint answered {type(page).__name__}")
            activities.extend(page)
            try:
                oldest = fill_time(page[-1]) if page else None
            except KeyError, TypeError, ValueError:
                oldest = (
                    None  # an unreadable last row; paging goes on, and the row is skipped later
                )
            if len(page) < ACTIVITIES_PAGE_SIZE or (
                command.start is not None and oldest is not None and oldest < command.start
            ):
                return activities
            params["page_token"] = page[-1]["id"]
        self._log.warning(
            f"Alpaca fill history read to its limit of {FILL_PAGES_LIMIT} pages of {ACTIVITIES_PAGE_SIZE}; "
            "older fills, if any, are not reported",
        )
        return activities

    async def _complete(
        self, reports: list[FillReport], activities: list, ts_init: int
    ) -> list[FillReport]:
        """Drop the reports of an order whose listed fills are not all of its fills, so the engine infers
        that order's fills rather than taking a part of them (package README).

        Two signs: the listed fills add up to less than the newest one's `cum_qty` ("The cumulative
        quantity of shares involved in the execution"), as when the window or the page limit cut older
        fills off; or the newest leaves shares to fill, is recent (`LAG_CHECK_NANOS`), and Alpaca's order
        shows more filled than the listed fills, as when the activities lag the stream.
        """
        newest: dict[str, dict] = {}
        for activity in activities:  # newest first
            if isinstance(activity, dict) and activity.get("order_id") is not None:
                newest.setdefault(str(activity["order_id"]), activity)
        listed: dict[str, float] = {}
        for report in reports:
            key = report.venue_order_id.value
            listed[key] = listed.get(key, 0.0) + report.last_qty.as_double()
        partial: set[str] = set()
        for order_id, qty in listed.items():
            last = newest.get(order_id, {})
            try:
                cum = float(last.get("cum_qty") or 0)
                leaves = float(last.get("leaves_qty") or 0)
                recent = ts_init - fill_time(last) <= LAG_CHECK_NANOS
            except KeyError, TypeError, ValueError:
                partial.add(order_id)
                continue
            if qty < cum:
                partial.add(order_id)
                continue
            if leaves <= 0 or not recent:
                continue
            try:
                order = await self._call(self._venue().get_order_by_id, order_id)
            except (APIError, OSError) as e:
                self._log.warning(f"Alpaca order {order_id} not read to check its fills ({e!r})")
                partial.add(order_id)
                continue
            if isinstance(order, AlpacaOrder) and float(order.filled_qty or 0) > qty:
                partial.add(order_id)
        for order_id in partial:
            self._log.warning(
                f"Alpaca lists only some fills of order {order_id}; its fills are left to the engine to infer",
            )
        return [r for r in reports if r.venue_order_id.value not in partial]

    async def _position_status_reports(
        self, command: live.GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        positions = await self._call(self._venue().get_all_positions)
        if not isinstance(positions, list):
            raise TypeError(
                "alpaca-py returned raw position data; the client must use raw_data=False"
            )
        ts_init = self.clock.timestamp_ns()
        reports = []
        for position in positions:
            if position.asset_class != AssetClass.US_EQUITY:
                self._log.warning(
                    f"Alpaca position in {position.symbol} ({position.asset_class.value}) is not a US "
                    "equity; not reported",
                )
                continue
            report = position_status_report(position, ts_init)
            if command.instrument_id is None or report.instrument_id == command.instrument_id:
                reports.append(report)
        # Alpaca's "All Open Positions" lists the account's open positions, and "Once a position is closed,
        # it will no longer be queryable" (https://docs.alpaca.markets/us/reference/getallopenpositions.md).
        # NautilusTrader takes only "An explicit flat report" as evidence of flat ("Missing reports do not
        # mean flat", reconciliation.md at the pinned commit), so each loaded instrument Alpaca lists no
        # position in is reported flat; otherwise fills inferred from part of the order history can leave
        # an external position Alpaca does not hold.
        held = {report.instrument_id for report in reports}
        for instrument in self._provider.list_all():
            wanted = command.instrument_id is None or instrument.id == command.instrument_id
            if wanted and instrument.id not in held:
                reports.append(
                    PositionStatusReport(
                        account_id=ACCOUNT_ID,
                        instrument_id=instrument.id,
                        position_side=PositionSide.FLAT,
                        quantity=Quantity(0, 0),
                        ts_last=ts_init,
                        ts_init=ts_init,
                    )
                )
        return reports

    async def _order_report(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> OrderStatusReport | None:
        # A replacement is reported under the node's client order ID, traced through `replaces`; a
        # replaced order is skipped by `order_status_report`, so it needs no lookup.
        try:
            root = (
                await self._root_id(order, known)
                if order.status != AlpacaOrderStatus.REPLACED
                else None
            )
        except (APIError, OSError) as e:
            self._log.warning(f"Alpaca order {order.id} is not reported: tracing it failed ({e!r})")
            return None
        # Reported under the chain's first client order ID, the node's, held or not: after a restart
        # the node's original order is created under its own ID.
        node_id = root
        held = self.cache.order(node_id) if node_id is not None else None
        if (
            node_id is not None
            and node_id.value != order.client_order_id
            and order.status == AlpacaOrderStatus.REJECTED
        ):
            # A rejected replacement (Alpaca rejects it when the old order fills first) did not take
            # over: the old order carries the outcome. Any other replacement is the node's order now,
            # filled or open, whether or not the node has seen `replaced`.
            return None
        # As upstream's Coinbase adapter patches a report from what the node sent: the trigger type
        # the node's order holds, which Alpaca's order does not carry.
        trigger_type = (
            held.trigger_type if held is not None and held.order_type in STOP_TYPES else None
        )
        renamed = (
            node_id if node_id is not None and node_id.value != order.client_order_id else None
        )
        report = order_status_report(order, self.clock.timestamp_ns(), trigger_type, renamed)
        if report is None:
            self._log.warning(
                f"Alpaca order {order.id} ({order.symbol}, {order.status.value}) is not reported: "
                "its type, side, class, quantity or time in force is not in this slice",
            )
        return report

    # -- alpaca-py calls ---------------------------------------------------------------------------

    def _venue(self) -> OrdersClient:
        if self._trading is None:
            raise RuntimeError("Alpaca execution client is not connected")
        return self._trading

    async def _call[R](self, fn: Callable[..., R], *args: object) -> R:
        """Call a `TradingClient` method in a worker thread, one call at a time.

        As `data.AlpacaDataClient._one_historical_call`: the lock is held on the node's loop and released
        by the worker when the call returns, and the call is shielded, so a cancelled hook leaves it
        running and the lock held until it ends.
        """
        loop = asyncio.get_running_loop()
        lock = self._rest_lock
        await lock.acquire()
        try:
            call = asyncio.ensure_future(
                asyncio.to_thread(release_after, loop, lock, counted(fn), *args)
            )
        except BaseException:
            lock.release()
            raise
        return cast("R", await asyncio.shield(call))
