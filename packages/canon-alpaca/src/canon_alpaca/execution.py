"""Execution client: market and limit orders for US equities on Alpaca's paper Trading API, and their events.

Contract: `nautilus_trader.live.clients.ExecutionClient`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Execution client hooks", https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md). The
client is constructed with the factory's name, config, cache, clock and trader ID and with a venue, account
ID, account type and OMS type. It overrides `_submit_order` and `_cancel_order` and the four reconciliation
hooks, emits account, order and fill events through the base class's `generate_*` methods, and leaves
`_generate_mass_status` to the native composition of the bulk report hooks. Nothing is started in the
constructor; the alpaca-py clients are made in `_connect` and the stream thread is joined in `_disconnect`.
Hooks not overridden here raise `NotImplementedError` from the base class.

Order events follow NautilusTrader's execution policies ("Command outcomes",
https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md): an order this slice cannot send is
denied before it is submitted (`OrderDenied`, no `OrderSubmitted`); one Alpaca refuses with a documented
status (403 or 422 on "Create an Order", https://docs.alpaca.markets/us/reference/postorder.md) is
rejected; a transport error or any other status leaves the outcome unknown and the order `SUBMITTED`, for
the trade updates stream or reconciliation to resolve. A cancel answered 204 waits for Alpaca's `canceled`
trade update; one answered 422 ("Delete Order by ID", https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md)
is a cancel rejection. Fills come only from the stream's `fill` and `partial_fill` events, whose `qty` and
`price` are "the number of shares" and "the price per share" for that fill event, not cumulative
(https://docs.alpaca.markets/us/docs/websocket-streaming.md).

Reconciliation (https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md): order status
reports come from Alpaca's orders endpoint and position reports from its positions endpoint. There are no
fill reports, because alpaca-py 0.45.0's `TradingClient` has no method for Alpaca's account activities
(only its `BrokerClient` has one, and this slice does not call the endpoint through the generic `get`); for an order status report without fills the engine "Infers
`OrderFilled` events for missing trade reports", at the report's average price.

Venue side, alpaca-py 0.45.0:

* `TradingClient(api_key, secret_key, paper=True)` for `submit_order`, `cancel_order_by_id`,
  `get_order_by_id`, `get_order_by_client_id`, `get_orders`, `get_all_positions` and `get_account`. The
  client is always built with `paper=True` and never with `url_override`, whatever the config holds; the
  config refuses both (`AlpacaExecutionClientConfig`). alpaca-py's REST client is synchronous, so each call
  runs in a worker thread, one at a time on its one `requests.Session`, as in `data.py`.
* `TradingStream(api_key, secret_key, paper=True)` for the trade updates stream. Like the market data
  stream, its `run()` drives its own asyncio loop, so it runs on a thread this client owns, started in
  `_connect` after the handler is subscribed (before `run()`, `subscribe_trade_updates` only records the
  handler) and stopped and joined in `_disconnect`. Updates are handed to the node's loop, where every
  event is generated; the handler holds the client weakly (`data.weak_coroutine`).

Known limits:

* Market and limit orders only, `DAY` or `GTC` only, whole shares only, simple orders only. Alpaca marks
  IOC, FOK, OPG and CLS for whole-share market and limit orders "Yes*", and "Please contact the sales team
  for any TIF marked with a *" ("Orders at Alpaca", https://docs.alpaca.markets/us/docs/orders-at-alpaca.md),
  so they are denied. Post-only, reduce-only, quote-quantity, display quantity, contingent orders and order
  lists are denied or not implemented; so are modify, cancel-all, query and extended hours.
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
* An Alpaca order event this slice does not map (`pending_cancel`, `replaced`, `pending_replace`,
  `order_replace_rejected`, `done_for_day`, `stopped`, `calculated`, `suspended`, `held`, `restated`) is
  logged as a warning. NautilusTrader generates `OrderPendingCancel` itself when a cancel is sent.
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
  order neither canceled nor pending cancel. alpaca-py sets no HTTP timeout.
* Start-up fails, by design, on an open order or position this slice cannot represent: a fractional
  position, or an open order that is not a simple whole-share US equity market or limit order.
* Timestamps lose sub-microsecond precision: alpaca-py parses them into `datetime`.
"""

from __future__ import annotations

import asyncio
import re
import threading
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
from .providers import credentials_from_env

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from uuid import UUID

    from alpaca.common.types import RawData
    from alpaca.trading.requests import OrderRequest
    from nautilus_trader import live
    from nautilus_trader.common import Clock
    from nautilus_trader.live import ClientCache
    from nautilus_trader.model import FillReport
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

ORDER_SIDES = {OrderSide.BUY: AlpacaOrderSide.BUY, OrderSide.SELL: AlpacaOrderSide.SELL}
ORDER_SIDES_FROM_ALPACA = {v: k for k, v in ORDER_SIDES.items()}

TIMES_IN_FORCE = {TimeInForce.DAY: AlpacaTimeInForce.DAY, TimeInForce.GTC: AlpacaTimeInForce.GTC}
"""The times in force Alpaca offers whole-share market and limit orders without a sales contact."""
TIMES_IN_FORCE_FROM_ALPACA = {v: k for k, v in TIMES_IN_FORCE.items()}

ORDER_TYPES_FROM_ALPACA = {
    AlpacaOrderType.MARKET: OrderType.MARKET,
    AlpacaOrderType.LIMIT: OrderType.LIMIT,
}

OPEN_STATUSES = {
    AlpacaOrderStatus.NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PENDING_NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
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
    """A price for a report, at the adapter's precision, refusing one that would need rounding."""
    amount = Decimal(str(value))
    if amount != amount.quantize(Decimal(1).scaleb(-PRICE_PRECISION)):
        raise ValueError(f"price {value} is finer than {PRICE_PRECISION} decimals")
    return Price(float(amount), PRICE_PRECISION)


def fill_price(value: float) -> Price:
    """The fill price exactly as Alpaca sent it: the shortest decimal form of alpaca-py's float."""
    return Price.from_str(repr(float(value)))


def sub_penny_denial(price: Price) -> str | None:
    """Why Alpaca would reject this limit price, or None: 2 decimals at or above $1.00, 4 below."""
    amount = Decimal(str(price))
    places = 2 if amount >= 1 else 4
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        return (
            f"limit price {amount.normalize()} has more than {places} decimals, which Alpaca rejects "
            f"for a price {'at or above' if amount >= 1 else 'below'} $1.00"
        )
    return None


def alpaca_side(order: AlpacaOrder) -> OrderSide:
    side = ORDER_SIDES_FROM_ALPACA.get(order.side) if order.side is not None else None
    if side is None:
        raise ValueError(f"side {getattr(order.side, 'value', order.side)}")
    return side


def order_status_report(order: AlpacaOrder, ts_init: int) -> OrderStatusReport | None:
    """An `OrderStatusReport` for one Alpaca order, None for a closed one this slice does not map.

    An open order this slice does not map raises, so reconciliation fails rather than leaving a working
    order unknown to the node. `replaced` is skipped: the order replacing it is reported in its own right.
    """
    status = order.status
    is_open = status not in CLOSED_STATUSES and status not in (AlpacaOrderStatus.REPLACED,)
    try:
        if status == AlpacaOrderStatus.REPLACED:
            raise ValueError("status replaced: modify is not in this slice")
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
            order_status=nautilus_status,
            quantity=quantity,
            filled_qty=filled_qty,
            ts_accepted=dt_to_unix_nanos(order.submitted_at),
            ts_last=dt_to_unix_nanos(order.updated_at),
            ts_init=ts_init,
            client_order_id=ClientOrderId(order.client_order_id),
            price=exact_report_price(order.limit_price) if order_type == OrderType.LIMIT else None,
            avg_px=Decimal(str(order.filled_avg_price))
            if filled and order.filled_avg_price
            else None,
        )
    except ValueError as e:
        if is_open:
            raise ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}") from e
        return None


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
    """Send market and limit orders for US equities to Alpaca's paper Trading API and report their events."""

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
        self._said_no_fill_reports = False
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
        self._said_no_fill_reports = False
        self._rest_lock = asyncio.Lock()
        if self._trading is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._trading is None:
                self._trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
            if self._stream is None:
                self._stream = TradingStream(api_key=api_key, secret_key=secret_key, paper=True)
        await self._provider.initialize()
        for instrument in self._provider.list_all():
            self._handle_instrument(instrument)
        await self._report_account()
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
        # Cleared whether or not the thread stopped, so a later connect builds fresh clients.
        self._stream_thread = None
        self._stream = None
        self._trading = None
        self._accepted.clear()
        self._finished.clear()
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
        denial = self._denial(order)
        if denial is not None:
            self.generate_order_denied(order, denial)
            return
        request = self._order_request(order)
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
        order = self.cache.order(command.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot cancel {command.client_order_id}: the node holds no such order"
            )
            return
        venue_order_id = command.venue_order_id or order.venue_order_id
        try:
            if venue_order_id is None:
                found = await self._call(
                    self._venue().get_order_by_client_id, command.client_order_id.value
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
                f"Alpaca cancel for {command.client_order_id} got no definitive answer "
                f"(HTTP {e.status_code}: {api_error_text(e)}); the order is left as it is",
            )
            return
        except OSError as e:
            self._log.error(
                f"Alpaca cancel for {command.client_order_id} got no answer ({e!r}); the order is left as it is",
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
        self.generate_order_cancel_rejected(
            order,
            venue_order_id,
            f"Alpaca refused the cancel ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    def _denial(self, order: Any) -> str | None:
        """Why this slice will not send the order, or None. Checked before anything reaches Alpaca."""
        instrument = (
            self._provider.find(order.instrument_id) if order.instrument_id.venue == VENUE else None
        )
        if not isinstance(instrument, Equity):
            return f"{order.instrument_id} is not a loaded {VENUE} equity"
        if not instrument.info.get("tradable", False):
            return f"{order.instrument_id} is not tradable on Alpaca (asset tradable=false)"
        if order.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            return f"order type {order.order_type.name} is not supported; market and limit only"
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
        if order.order_type == OrderType.LIMIT:
            if order.is_post_only:
                return "post-only is not supported"
            if order.display_qty is not None:
                return "display quantity is not supported"
            return sub_penny_denial(order.price)
        return None

    @staticmethod
    def _order_request(order: Any) -> OrderRequest:
        fields = {
            "symbol": order.instrument_id.symbol.value,
            "qty": float(int(order.quantity.as_double())),
            "side": ORDER_SIDES[order.side],
            "time_in_force": TIMES_IN_FORCE[order.time_in_force],
            "client_order_id": order.client_order_id.value,
        }
        if order.order_type == OrderType.LIMIT:
            return LimitOrderRequest(**fields, limit_price=float(Decimal(str(order.price))))
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
        self._finished.add(order.client_order_id)
        self.generate_order_rejected(order, reason, ts_event, False)

    def _apply_trade_update(self, update: TradeUpdate) -> None:
        if self._stopping:
            return
        try:
            self._apply(update)
        except (ValueError, TypeError, RuntimeError) as e:
            # On the loop's callback path nothing else reports it: name the update and keep the stream.
            self._log.error(
                f"Alpaca trade update {update.event} for {update.order.client_order_id} failed: {e!r}",
            )

    def _apply(self, update: TradeUpdate) -> None:
        alpaca_order = update.order
        client_order_id = ClientOrderId(alpaca_order.client_order_id)
        order = self.cache.order(client_order_id)
        if order is None:
            self._log.info(
                f"Alpaca trade update {update.event} for order {alpaca_order.id} ({alpaca_order.symbol}), "
                "which this node does not hold; reconciliation reports it",
            )
            return
        if client_order_id in self._finished:
            return
        event = getattr(update.event, "value", update.event)
        venue_order_id = VenueOrderId(str(alpaca_order.id))
        ts_event = dt_to_unix_nanos(update.timestamp)
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
            self._finished.add(client_order_id)

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
        """On the stream's thread: hand the update to the node's loop."""
        if isinstance(update, TradeUpdate) and self._owner_loop is not None:
            post(self._owner_loop, self._apply_trade_update, update)

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
        if not self._said_no_fill_reports:
            self._said_no_fill_reports = True
            self._log.info(
                "Alpaca fill history is not read (alpaca-py's TradingClient has no account activities "
                "method); NautilusTrader infers fills from the order status reports",
            )
        return []

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
        if not isinstance(found, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        return self._order_report(found)

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
            report = self._order_report(order)
            if report is not None:
                reports.append(report)
        return reports

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
        return reports

    def _order_report(self, order: AlpacaOrder) -> OrderStatusReport | None:
        report = order_status_report(order, self.clock.timestamp_ns())
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
            call = asyncio.ensure_future(asyncio.to_thread(release_after, loop, lock, fn, *args))
        except BaseException:
            lock.release()
            raise
        return cast("R", await asyncio.shield(call))
