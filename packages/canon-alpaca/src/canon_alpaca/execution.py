"""Execution client: market, limit, stop and stop-limit orders for US equities on Alpaca's paper Trading API.

Contract: `nautilus_trader.live.clients.ExecutionClient`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Execution client hooks", https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md). The
client is constructed with the factory's name, config, cache, clock and trader ID and with a venue, account
ID, account type and OMS type. It overrides `_submit_order`, `_modify_order`, `_cancel_order`, `_query_account`,
`_query_order`, `_cancel_all_orders` and the four reconciliation hooks, emits account, order and fill events through the
base class's `generate_*`
methods, and leaves `_batch_cancel_orders` to its default, "ordered calls of the corresponding
single-order hook" (Alpaca has no endpoint that cancels a list of orders by ID), and `_generate_mass_status`
to the native composition of the bulk report hooks. Nothing is started in the constructor; the alpaca-py
clients and the task that applies trade updates in order are made in `_connect`, and the stream thread is
joined and the task ended in `_disconnect`, each wait bounded by a deadline on the node's loop
(`data.join_within`, `TASK_GRACE_SECS` and `TASK_ABORT_SECS`), as upstream's Coinbase adapter bounds its
task drain at disconnect; a wait that runs out fails the disconnect, logged with its reason. Hooks not overridden here
raise `NotImplementedError`.

Modify (ADR 0042) rests on Alpaca's "Replace Order by ID"
(https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md) and on a paper observation of
2026-11-10, 3 of 3 runs, for what that page does not state. The PATCH answer is "The new Order object with
the new order ID", but a success "does NOT guarantee the existing open order has been replaced", so
`OrderUpdated` is generated only when Alpaca's stream shows the replace took effect: its `replaced` for an
order of the node's chain, which carries the old order with `replaced_by` (observed), with the replacement's
values from the answer or, failing that, from Alpaca; if neither yields them, nothing is generated and
reconciliation settles the order. The replacement gets a client order ID Alpaca makes (one kept is refused,
observed). A 422 refusal is `OrderModifyRejected` unless Alpaca shows the order already replaced as asked (a
second replace is refused "order already replaced", observed);
`order_replace_rejected` is `OrderModifyRejected` too. Denied before Alpaca: no venue order ID, a fractional
quantity, a quantity or price that is not positive, and a sub-penny price; what only Alpaca knows (an order in
`accepted` "cannot be replaced") is left to its answer. Each modify sent gets one outcome, though Alpaca can
refuse it on the HTTP answer and on the stream.

The replace chain is one model, after upstream's Binance spot adapter, the one pinned adapter that modifies
by cancel-replace (`crates/adapters/binance/src/common/dispatch.rs` at the pin: `OrderBinding`,
`record_order_update`, `record_order_update_from_fill`, `begin_replace`, `reject_replace`, `restore_order`,
`cleanup_terminal`, read raw): one record per node order (`OrderRecord`) holds the Alpaca orders it has been,
the one it is on now and the ones it has left, the replaces awaiting an outcome, and the client's own count of
its fills and their trade IDs. It is built from the node's cache when first needed (the order's venue order
IDs, current and before, its fills and trade IDs), as `restore_order` rebuilds a binding, so a reconnect or a
new client loses nothing of it, and it is dropped when the order ends. Every trade update and every report is
judged through it:

* An Alpaca order is the node's by its own client order ID, the client's index, the node's cache
  (`ClientCache.client_order_id`) or, only on a miss, a walk of `replaces` (`_root_id`), which reads at most
  `MAX_REPLACE_CHAIN` orders and fails by name past that or on an order Alpaca does not find.
* A fill is applied once per trade ID, on any order of the chain, and the order ends when the record's count
  reaches its quantity, so a late fill of an order since replaced still counts.
* A replacement is adopted once: by `replaced`, by its own update once the record shows the order it replaces
  replaced by it, or by a fill, which proves it live (`record_order_update_from_fill`); its `new` alone does
  not, since it may still be rejected. A late update of an order the record has left is ignored, but for its
  fills (`record_order_update` returns nothing for a superseded venue order ID).
* A replacement's `rejected` took over only if the order it replaces is replaced by it, as the record shows or
  one read of that order does; then nothing works at Alpaca, and the node's order ends: rejected, or, partly
  filled, canceled with its fills, as `reject_replace` emits the old order's withheld `OrderCanceled` and since
  NautilusTrader has no transition from partly filled to rejected. A replacement rejected without taking over
  is the modify's rejection; one a later replace left behind changes nothing; one rejected after the order
  filled leaves the fill as the outcome (NautilusTrader drops a modify rejection for a filled order, observed).
* Reports apply the same rules to Alpaca's reads: a replacement is reported under the client order ID its chain
  starts from, a rejected one only if it took over, and then as the order's end; an open replacement whose
  chain cannot be read fails the report, a closed one is skipped. `pending_replace` is open, at the order's own
  terms ("waiting to be replaced").

What a replacement's `qty` and `filled_qty`, and its fill activities' `cum_qty`, count once the order before it
had fills is not on Alpaca's pages; on paper, overnight, 2 of 2 runs (ADR 0042), they counted the whole
chain: `FILLED_READING` is that reading, `CHAIN`, and `chain_filled` and `chain_quantity` are the only places
the accounting turns on it; the regular session is UNVERIFIED.

Order events follow NautilusTrader's execution policies ("Command outcomes",
https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md): an order this slice cannot send is
denied before it is submitted (`OrderDenied`, no `OrderSubmitted`); one Alpaca refuses with a documented
status (403 or 422 on "Create an Order", https://docs.alpaca.markets/us/reference/postorder.md) is
rejected; a transport error or any other status leaves the outcome unknown and the order `SUBMITTED`, for
the trade updates stream or reconciliation to resolve. A cancel answered 204 waits for Alpaca's `canceled`
trade update; one answered 422 ("Delete Order by ID", https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md)
is a cancel rejection, unless Alpaca shows the order pending cancel (its `canceled` update follows) or
canceled, when the order is canceled from that read if Alpaca shows no more filled than the node holds,
as upstream's Bybit adapter emits `OrderCanceled` from an order it reads canceled: a dropped stream would
never send the update. Cancel-all cancels the node's open and in-flight orders for the command's instrument,
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
trade reports" for orders whose fills Alpaca lists. An activity listed twice is one fill, counted and
reported once, as NautilusTrader's order "enforces one quantity-bearing fill per `trade_id`" ("Duplicate
fill detection"). An order's reports are kept only when its activities,
or failing that its order (`filled_qty`), show they are all its fills; otherwise, and when the history
cannot be read at all, none are, with a warning, and the engine infers them as before. Upstream's OKX
and Kraken spot adapters (`crates/adapters/okx/src/execution.rs`, `kraken/src/execution/spot.rs` at the
pinned commit) instead track whether their fill history is complete and mark the mass status so
(`set_report_window`; reconciliation.md: an adapter "marks the mass status incomplete to record the
missing history"). NautilusTrader's Python `ExecutionMassStatus` at the pin has no setter for
`reports_complete`, so this client drops the fills it cannot show complete: a departure (ADR 0024).
A fill Alpaca lists under an order since replaced is reported on the order that replaced it, which the
order reports carry under the node's client order ID, as the stream applies a fill on a superseded order
to the node's order. The chain's fills are judged together, every order of it read (`_replaced_fills`):
kept when the live order's `filled_qty`, and its newest listed activity's `cum_qty`, show they are all of
them under `FILLED_READING`, the reading observed on paper (ADR 0042); when they show it counts only its
own, which paper did not show, its own fills are kept and the earlier ones left to the engine.

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
* Post-only, reduce-only, quote-quantity, display quantity (on any order type, a stop-market's
  included), contingent orders and order lists are denied or not implemented.
* `QueryOrder`, which NautilusTrader's in-flight and open-order checks send, is answered with the order's
  status report, as upstream's Binance spot, Bybit, Coinbase, Kraken spot and OKX adapters answer it
  (`_query_order`); an order not found or a failed read sends nothing, and the engine asks again.
* Extended hours (ADR 0038): `submit_order(order, params={"extended_hours": True})` sends a limit order,
  `DAY` or `GTC`, with Alpaca's `extended_hours` set; any other order so marked, an unknown params key and
  a non-boolean value are denied before Alpaca ("Only limit orders with `time_in_force` set to `day` or
  `gtc` orders are accepted as extended hours eligible", "Orders at Alpaca", "Extended Hours Trading").
  Its trade updates are mapped as any order's. A `day` order placed overnight stays active through the next
  trading day and "If unfilled, the order is canceled at 8:00 PM ET" (Alpaca's 24/5 page); which update
  that cancel sends is not documented.
* Cancel-all sends one cancel per order, one at a time, in no specified order, an order already pending
  cancel included; an order placed after the command was read from the cache is not canceled by it.
* A limit price at or above $1.00 with more than 2 decimals, or below $1.00 with more than 4, is denied
  here: Alpaca rejects it ("Sub-penny increments for limit orders", same page).
* The account is a NautilusTrader cash account holding Alpaca's `cash` as its USD balance, all of it free,
  read at connect and on `QueryAccount` (a strategy's `query_account`), as upstream's Kraken spot,
  Coinbase, Binance spot and Bybit adapters fetch their venue's account on a query; Alpaca's stream pushes
  no account update, and NautilusTrader keeps a reported cash account's balance as reported
  (`calculate_account_state` off), so the balance is stale between reads (ADR 0036). Cash finer than a
  cent is reported to the cent with a warning. An answer without `cash` or `currency`, both optional in
  alpaca-py's `TradeAccount`, is refused naming the field: a connect fails, a query leaves the state.
  Buying power, margin and shorting are not modelled.
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
  it is listening are missed. alpaca-py reconnects the stream by itself after a websocket or connection
  error, with backoff, and says so only through Python `logging` (`TradingStream._drive_run`); updates
  sent while it was down are not replayed, and none of upstream's adapters at the pin catches up on a
  reconnect (Kraken spot and Binance spot log it, Coinbase reads its account again). What settles such an
  order is NautilusTrader's in-flight check, on by default, for an order `SUBMITTED`, `PENDING_CANCEL` or
  `PENDING_UPDATE`, and its open-order check, off by default (`open_check_interval_secs`), for an
  accepted one; both send `QueryOrder`. Otherwise it is settled at the next start. If `run()` returns
  (the stream ends by itself) the client logs an error and does not start it again, as in `data.py`.
* Reports: every open order, up to 500, read first, on its own, with no window, plus the orders
  submitted in the engine's window, since Alpaca filters on submission time and a read of every status
  returns only the newest; at most 500 orders per request (`GetOrdersRequest.limit`, "max is 500"), the newest first; a full page is
  logged as possibly truncated. An order Alpaca reports with a type, side, class, status, quantity, time in
  force or client order ID this slice does not map, or a replacement whose chain cannot be read, fails the
  report when it is open and is skipped with a warning naming why when it is closed.
* Order commands are sent once (ADR 0039). alpaca-py 0.45.0 sends a request answered 429 or 504 up to 3
  more times (`RESTClient._request`), a submit, replace or cancel included, and `TradingClient` takes no
  retry setting. NautilusTrader's policies send a submit, modify or cancel "Once, unless the safety test
  holds" ("Delivery and retry limits", `docs/concepts/execution/policies.md` at the pinned commit), and
  upstream's Binance, Coinbase, Derive, Hyperliquid and Lighter adapters send them once ("Retry gate
  conformance", `docs/developer_guide/adapters.md`); Alpaca's "Working with Orders" FAQ says of an order
  that timed out "You should not attempt to resend the order". So `send_order_commands_once` adds a
  requests response hook to the client's session at connect, and `without_resend` has it end an order
  command at Alpaca's first error answer with alpaca-py's own `APIError`; reads keep alpaca-py's retry,
  a repeat that is safe ("Read or other idempotent request": "On a transient failure"). A 504 or 429 is
  an unknown outcome ("An HTTP 5xx without definitive command evidence is ambiguous", adapters.md; "rate
  limits are definitive only when venue-specific semantics prove that the command was not accepted",
  policies; Alpaca's pages read document none for the Trading API): the order stays as it was,
  `SUBMITTED`, `PENDING_UPDATE` or `PENDING_CANCEL`, for a trade update, the engine's in-flight check (`QueryOrder`,
  read by client order ID for an order without a venue order ID) or reconciliation. Whether a 504 is the
  FAQ's "timeout message" is not stated on Alpaca's pages read (UNVERIFIED).
* A submit refused with 403 or 422 is rejected only once Alpaca shows it does not hold the order: Alpaca
  refuses a repeated `client_order_id`, 422 "client_order_id must be unique" (paper, 2026-11-10), and so
  holds an order under it. A cancel refused with 422 is rejected only once Alpaca shows the order neither
  canceled nor pending cancel; one it shows canceled is canceled.
* alpaca-py sets no HTTP timeout; the client's `TradingClient` gets one, (3.05, 27) s, from a transport
  adapter on alpaca-py's private `_session` (`providers.with_http_timeout`, ADR 0039). A connect timeout
  on a call's only attempt proves nothing was sent, so a submit is rejected, a modify modify-rejected and
  a cancel cancel-rejected, each "not sent" (NautilusTrader's policies, "Command outcomes"); any other
  timeout is an unknown outcome, as above.
* Positions in an instrument the client has not loaded are not reported: NautilusTrader fails start-up on
  one ("instrument missing from cache", observed), and upstream's Coinbase adapter keeps its reports to
  the instruments it loaded. A position in another symbol is not read for a report asked for one.
* Start-up fails, by design, on an open order or position this slice cannot represent: a fractional
  position in a loaded instrument, or an open order that is not a simple whole-share US equity market, limit, stop or stop-limit
  order. An open stop is reported with its stop price as the trigger price and the node's trigger type, else
  `DEFAULT`.
* Timestamps lose sub-microsecond precision: alpaca-py parses them into `datetime`.
* A trading client given to the constructor is the caller's: every connect uses it, a disconnect does not
  drop it, and nothing is built from the environment in its place (the adapter guide closes in
  `_disconnect` what the adapter creates in `_connect`). A given stream serves one connect, since
  alpaca-py's stream keeps its handlers and websocket between runs; a second connect fails, naming why.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import re
import threading
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Protocol
from typing import cast
from typing import override
from urllib.parse import urlsplit

import requests
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
from .data import join_within
from .data import logged_failure
from .data import post
from .data import retrieved
from .data import weak_callback
from .data import weak_coroutine
from .providers import AlpacaInstrumentProvider
from .providers import counted
from .providers import credentials_from_env
from .providers import not_sent
from .providers import session_of
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

ORDERS_PAGE_LIMIT = 500
"""The most orders one `get_orders` call returns: `GetOrdersRequest.limit` "Defaults to 50 and max is 500"."""

TASK_GRACE_SECS = 1.0
"""How long `_disconnect` lets an update being applied finish before it cancels the task applying them;
then it waits `TASK_ABORT_SECS` for the task to end. The graceful and forced bounds of upstream's Coinbase
adapter's task drain at disconnect (`crates/adapters/coinbase/src/execution.rs`,
`finish_shutdown(Duration::from_secs(1), Duration::from_secs(2))`, at the pinned commit)."""

TASK_ABORT_SECS = 2.0
"""See `TASK_GRACE_SECS`."""

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
"""The most Alpaca reads one walk of a replace chain makes, back through `replaces` or on through
`replaced_by`. Links the orders already read, the client's index or the node's cache give cost none, so a
chain of any length is followed through them; a walk that would need more reads fails by name."""

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

    Host names are compared whole, so `paper-api.alpaca.markets` is not taken for `api.alpaca.markets`, and
    without the trailing dot of a fully qualified name (`api.alpaca.markets.` names the same host).
    """
    return any(
        token.rstrip(".") in LIVE_HOSTS for token in re.findall(r"[a-z0-9.-]+", str(value).lower())
    )


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


async def drain(consumer: asyncio.Task, idle: asyncio.Event) -> None:
    """Let the update `consumer` is applying finish, for at most `TASK_GRACE_SECS`, then cancel it and wait
    at most `TASK_ABORT_SECS` for it to end: the graceful and forced bounds of upstream's
    `TaskGroup::finish_shutdown`."""
    if not consumer.done():
        try:
            await asyncio.wait_for(idle.wait(), TASK_GRACE_SECS)
        except TimeoutError:
            pass
    consumer.cancel()
    await asyncio.wait({consumer}, timeout=TASK_ABORT_SECS)


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


SENDING_ONCE = threading.local()
"""`active` on a worker thread while it runs an order command (`without_resend`)."""


def send_order_commands_once[C](client: C) -> C:
    """Add `end_at_first_error` to the response hooks of an alpaca-py REST client's `requests.Session`
    ("Event Hooks": a `Session`'s hooks "will then be called on every request made to the session"), once,
    and return the client. alpaca-py 0.45.0 keeps the session at `_session`, the attribute ADR 0039 reaches
    through `session_of`; a client without one is refused, as `with_http_timeout` refuses it."""
    session = session_of(client)
    if session is None:
        raise TypeError(
            f"{type(client).__name__} has no requests.Session at _session; refusing to run with "
            "alpaca-py's resend of order commands (ADR 0039)"
        )
    hooks = session.hooks.setdefault("response", [])
    if end_at_first_error not in hooks:
        hooks.append(end_at_first_error)
    return client


def without_resend[R](fn: Callable[..., R]) -> Callable[..., R]:
    """`fn`, an order command of a client `send_order_commands_once` has prepared, run as an order command:
    on the worker thread that runs it, `end_at_first_error` ends it at Alpaca's first error answer, so
    alpaca-py does not send it again."""

    def call(*args: object) -> R:
        SENDING_ONCE.active = True
        try:
            return fn(*args)
        finally:
            SENDING_ONCE.active = False

    call.__name__ = getattr(fn, "__name__", "call")
    return call


def end_at_first_error(
    response: requests.Response, *args: object, **kwargs: object
) -> requests.Response:
    """A requests response hook ("Event Hooks", Advanced Usage): during an order command, an error answer
    raises alpaca-py's `APIError` at once, the exception `RESTClient._one_request` raises for an answer it
    does not retry, built as it builds it (`APIError(response.text, http_error)`). alpaca-py 0.45.0 sends a
    request answered 429 or 504 up to three more times, an order command included, and its `TradingClient`
    takes no retry setting; NautilusTrader's policies send a submit, modify or cancel "Once, unless the
    safety test holds", and Alpaca's "Working with Orders" says not to resend an order that timed out. A
    429 ends it too: "rate limits are definitive only when venue-specific semantics prove that the command
    was not accepted" (policies), and Alpaca's pages read document none for the Trading API. Any other
    answer, and every answer outside an order command, goes to alpaca-py as it came."""
    if getattr(SENDING_ONCE, "active", False):
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise APIError(response.text, e) from e
    return response


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


def decimal_value(value: object, field: str) -> Decimal:
    """An Alpaca decimal string or number as a finite `Decimal`, raising `ValueError` naming `field` for one
    that is not a number: `Decimal` raises `InvalidOperation`, an `ArithmeticError`, which the callers'
    `ValueError` handling would not take."""
    try:
        amount = Decimal(str(value))
    except ArithmeticError as e:
        raise ValueError(f"{field} {value!r} is not a number") from e
    if not amount.is_finite():
        raise ValueError(f"{field} {value!r} is not a number")
    return amount


def whole_quantity(value: object, field: str) -> Quantity:
    """A whole-share quantity from an Alpaca decimal string or number, refusing a fractional one."""
    amount = decimal_value(value, field)
    if amount != amount.to_integral_value():
        raise ValueError(f"{field} {value} is fractional; Equity is whole-share only")
    return Quantity(int(amount), 0)


def exact_report_price(value: object) -> Price:
    """A price for a report, at the adapter's precision, refusing a missing one and one that would need
    rounding."""
    if value is None:
        raise ValueError("no price")
    amount = decimal_value(value, "price")
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


def holdable_id(value: str) -> ClientOrderId | None:
    """A client order ID as NautilusTrader holds it, or None for one it refuses (empty or not ASCII)."""
    try:
        return ClientOrderId(value)
    except ValueError:
        return None


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


class FilledReading(Enum):
    """What Alpaca's `qty` and `filled_qty` on a replacement, and the `cum_qty` and `leaves_qty` of its
    fill activities, count once the order it replaced had fills. Alpaca's pages do not say; paper showed
    `CHAIN` overnight (ADR 0042), and the regular session is UNVERIFIED. Both readings are written here
    and tested, and `FILLED_READING` names the one in force."""

    CHAIN = "chain"
    """The replacement carries the order on: its `qty` is the order's and its `filled_qty` counts every fill
    of the replace chain."""

    OWN = "own"
    """The replacement is an order for what is left: its `qty` and `filled_qty` are its own, and the chain's
    add the fills of the orders before it."""


FILLED_READING = FilledReading.CHAIN
"""The reading in force, observed on paper (ADR 0042, 2026-11-10 New York, overnight, 2 of 2 runs): a
partly filled order replaced and then filled showed the replacement's `qty` as the order's, its `filled_qty`
and its activity's `cum_qty` counting the fills before it, and `leaves_qty` as what is left of the order.
Whether the regular session answers the same is UNVERIFIED; `OWN` stays written down and tested for it."""


def chain_filled(own: Decimal, earlier: Decimal) -> Decimal:
    """The shares a replace chain has filled, from its live order's `filled_qty` (`own`) and the
    `filled_qty` of the orders before it (`earlier`), under `FILLED_READING`. With `chain_quantity`, the one
    place the client's accounting of a chain turns on the reading."""
    return own if FILLED_READING is FilledReading.CHAIN else own + earlier


def chain_quantity(own: Decimal, earlier: Decimal) -> Decimal:
    """The node order's quantity, from its live Alpaca order's `qty` (`own`) and the `filled_qty` of the
    orders before it (`earlier`), under `FILLED_READING`."""
    return own if FILLED_READING is FilledReading.CHAIN else own + earlier


@dataclasses.dataclass
class Leg:
    """One Alpaca order a node order has been, as last seen: its links, status and `filled_qty`."""

    order_id: str
    replaces: str | None = None
    replaced_by: str | None = None
    status: AlpacaOrderStatus | None = None
    filled: Decimal | None = None


@dataclasses.dataclass
class OrderRecord:
    """Everything the client holds of one node order's replace chain, after upstream's Binance spot
    `OrderBinding` (`current`, `superseded`) and `PendingReplacement` (`crates/adapters/binance/src/common/
    dispatch.rs` at the pin): the Alpaca orders it has been (`legs`), the one it is on now (`current`), the
    ones it has left (`superseded`), the replaces sent and awaiting an outcome, counted by the Alpaca order
    each was sent to (`pending`), the replace answers not yet confirmed (`answers`), and the client's own count of its fills,
    its quantity and the trade IDs applied. It is built from the node's cache when first needed, as Binance
    spot's `restore_order` rebuilds a binding from `order.venue_order_ids()`, so nothing of it rests on what
    an earlier connect kept, and dropped when the order ends (`cleanup_terminal`)."""

    client_order_id: ClientOrderId
    current: str | None = None
    legs: dict[str, Leg] = dataclasses.field(default_factory=dict)
    superseded: set[str] = dataclasses.field(default_factory=set)
    pending: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    answers: dict[str, AlpacaOrder] = dataclasses.field(default_factory=dict)
    filled: Decimal = Decimal(0)
    quantity: Decimal = Decimal(0)
    trades: set[str] = dataclasses.field(default_factory=set)

    def see(self, order: AlpacaOrder) -> Leg:
        """Record Alpaca's view of one of the order's legs. A leg seen replaced stays replaced, with the
        order that replaced it, whatever an older copy delivered later shows: an order is replaced once."""
        order_id = str(order.id)
        leg = self.legs.setdefault(order_id, Leg(order_id))
        if order.replaces is not None:
            leg.replaces = str(order.replaces)
        if leg.status == AlpacaOrderStatus.REPLACED and order.status != AlpacaOrderStatus.REPLACED:
            return leg
        leg.status = order.status
        if order.replaced_by is not None:
            leg.replaced_by = str(order.replaced_by)
        leg.filled = share_count(order.filled_qty)
        return leg

    def took_over(self, order_id: str) -> bool | None:
        """Whether a replacement took the order over, as the record shows the order it replaces: True if
        that order is replaced by it, False if replaced by another, None while the record does not know."""
        leg = self.legs.get(order_id)
        prior = self.legs.get(leg.replaces) if leg is not None and leg.replaces else None
        if prior is None or prior.status != AlpacaOrderStatus.REPLACED or prior.replaced_by is None:
            return None
        return prior.replaced_by == order_id

    def earlier_filled(self, order_id: str) -> Decimal:
        """The `filled_qty` last seen on the orders before `order_id` in the chain, for `chain_filled`."""
        total = Decimal(0)
        leg = self.legs.get(order_id)
        seen: set[str] = set()
        while leg is not None and leg.replaces is not None and leg.replaces not in seen:
            seen.add(leg.replaces)
            leg = self.legs.get(leg.replaces)
            if leg is not None and leg.filled is not None:
                total += leg.filled
        return total


def alpaca_side(order: AlpacaOrder) -> OrderSide:
    side = ORDER_SIDES_FROM_ALPACA.get(order.side) if order.side is not None else None
    if side is None:
        raise ValueError(f"side {getattr(order.side, 'value', order.side)}")
    return side


def is_open_order(order: AlpacaOrder) -> bool:
    """Whether Alpaca's order still works: not closed, and not replaced (its replacement works)."""
    return order.status not in CLOSED_STATUSES and order.status != AlpacaOrderStatus.REPLACED


def order_status_report(
    order: AlpacaOrder,
    ts_init: int,
    trigger_type: TriggerType | None = None,
    client_order_id: ClientOrderId | None = None,
    skipped: Callable[[str], None] | None = None,
    status: OrderStatus | None = None,
    filled: Decimal | None = None,
    quantity: Decimal | None = None,
) -> OrderStatusReport | None:
    """An `OrderStatusReport` for one Alpaca order, None for a closed one this slice does not map, whose
    reason `skipped` is given.

    An open order this slice does not map raises, so reconciliation fails rather than leaving a working
    order unknown to the node. `replaced` is skipped: the order replacing it is reported in its own right,
    under `client_order_id` when the client has traced it back to the node's order. A stop's trigger type
    is `trigger_type` when the node holds the order, else `DEFAULT`: Alpaca's order has no trigger type
    field. `status`, `filled` and `quantity` stand in for what Alpaca's order shows when the client judges
    a replace chain as a whole (`AlpacaExecutionClient._order_report`, `chain_filled`).
    """
    try:
        return mapped_order_report(
            order, ts_init, trigger_type, client_order_id, status, filled, quantity
        )
    except ValueError as e:
        if is_open_order(order):
            raise ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}") from e
        if skipped is not None:
            skipped(str(e))
        return None


def mapped_order_report(
    order: AlpacaOrder,
    ts_init: int,
    trigger_type: TriggerType | None = None,
    client_order_id: ClientOrderId | None = None,
    status: OrderStatus | None = None,
    filled: Decimal | None = None,
    quantity: Decimal | None = None,
) -> OrderStatusReport:
    """`order_status_report`'s report, raising `ValueError` with what this slice does not map, open or
    closed."""
    if order.status == AlpacaOrderStatus.REPLACED:
        raise ValueError("status replaced: its replacement is reported instead")
    nautilus_status = OPEN_STATUSES.get(order.status) or CLOSED_STATUSES.get(order.status)
    if nautilus_status is None:
        raise ValueError(f"status {order.status.value}")
    if order.asset_class not in (None, AssetClass.US_EQUITY):
        raise ValueError(f"asset class {getattr(order.asset_class, 'value', order.asset_class)}")
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
    total = whole_quantity(quantity if quantity is not None else order.qty, "quantity")
    filled_qty = whole_quantity(
        filled if filled is not None else order.filled_qty or 0, "filled quantity"
    )
    any_filled = filled_qty.as_double() > 0
    if status is None:
        # Open with fills is partly filled, as `pending_replace` with fills is at its own terms and as a
        # replacement of a partly filled order shows its chain's fills.
        status = (
            OrderStatus.PARTIALLY_FILLED
            if nautilus_status == OrderStatus.ACCEPTED and any_filled
            else nautilus_status
        )
    return OrderStatusReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(order.symbol), VENUE),
        venue_order_id=VenueOrderId(str(order.id)),
        order_side=alpaca_side(order),
        order_type=order_type,
        time_in_force=time_in_force,
        order_status=status,
        quantity=total,
        filled_qty=filled_qty,
        ts_accepted=dt_to_unix_nanos(order.submitted_at),
        ts_last=dt_to_unix_nanos(order.updated_at),
        ts_init=ts_init,
        client_order_id=client_order_id or own_client_id(order),
        price=exact_report_price(order.limit_price) if order_type in LIMIT_PRICED else None,
        trigger_price=exact_report_price(order.stop_price) if order_type in STOP_TYPES else None,
        trigger_type=(trigger_type or TriggerType.DEFAULT) if order_type in STOP_TYPES else None,
        avg_px=decimal_value(order.filled_avg_price, "average fill price")
        if any_filled and order.filled_avg_price
        else None,
    )


def own_client_id(order: AlpacaOrder) -> ClientOrderId:
    """Alpaca's client order ID as NautilusTrader holds it, raising `ValueError` naming it when it cannot."""
    try:
        return ClientOrderId(order.client_order_id)
    except ValueError as e:
        raise ValueError(f"a client order ID NautilusTrader cannot hold ({e})") from e


def share_count(value: object) -> Decimal | None:
    """`value`, a quantity Alpaca sends as a string, as a finite, non-negative number of shares, or None
    if absent or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        quantity = Decimal(str(value))
    except ArithmeticError:
        return None
    return quantity if quantity.is_finite() and quantity >= 0 else None


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


def moved_report(report: FillReport, venue_order_id: VenueOrderId) -> FillReport:
    """`report` on another Alpaca order: a fill listed under an order since replaced, reported on the
    order that replaced it (`AlpacaExecutionClient._replaced_fills`)."""
    return FillReport(
        account_id=report.account_id,
        instrument_id=report.instrument_id,
        venue_order_id=venue_order_id,
        trade_id=report.trade_id,
        order_side=report.order_side,
        last_qty=report.last_qty,
        last_px=report.last_px,
        commission=report.commission,
        liquidity_side=report.liquidity_side,
        ts_event=report.ts_event,
        ts_init=report.ts_init,
    )


def fill_terms(report: FillReport) -> tuple:
    """What a fill report says of its fill, without its own report ID: two activities with one trade ID
    and the same terms are one fill listed twice."""
    return (
        report.instrument_id,
        report.order_side,
        report.last_qty,
        report.last_px,
        report.ts_event,
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
        quantity=whole_quantity(
            abs(decimal_value(position.qty, "position quantity")),
            f"{position.symbol} position quantity",
        ),
        ts_last=ts_init,
        ts_init=ts_init,
        avg_px_open=decimal_value(position.avg_entry_price, "average entry price"),
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
        # Clients given here are the caller's: used on every connect, never replaced by ones built from
        # the environment, and not dropped at disconnect (the adapter guide closes what the adapter
        # creates in `_connect`). The live fields below are set in `_connect` and cleared on disconnect.
        self._given_trading = trading_client
        self._given_stream = stream
        # A given stream serves one connect: alpaca-py's stream keeps its handlers and its websocket
        # between runs, and a run abandoned at a timed-out disconnect may still be using it.
        self._given_stream_used = False
        self._trading: OrdersClient | None = None
        self._stream: TradeUpdateStream | None = None
        self._stream_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        # One alpaca-py call at a time on its one requests.Session (see data.py); rebuilt in `_connect`.
        self._rest_lock = asyncio.Lock()
        # Orders this client has seen Alpaca acknowledge, and orders it has seen reach a final state, so
        # an acknowledgement is generated once and nothing is generated after the end.
        self._accepted: set[ClientOrderId] = set()
        self._finished: set[ClientOrderId] = set()
        # One record per node order with a replace chain or fills to count (`OrderRecord`), built from
        # the node's cache when first needed: the cache takes a generated event later, not when it is
        # generated (observed), so an order's terms and end are judged on the record.
        self._records: dict[ClientOrderId, OrderRecord] = {}
        # Each Alpaca order ID traced, mapped to the client order ID its chain starts from, held by the
        # node or not (None for none NautilusTrader can hold): the index the node's cache and a walk of
        # `replaces` fill on a miss.
        self._index: dict[str, ClientOrderId | None] = {}
        # Alpaca's acknowledgement of an order that was pending cancel or update when it came, kept for
        # the refusal that puts the order back to `SUBMITTED` (`_accept`).
        self._acknowledged: dict[ClientOrderId, tuple[VenueOrderId, int]] = {}
        # Trade updates, applied in arrival order by one task on the node's loop.
        self._updates: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._consumer: asyncio.Task[object] | None = None
        # Set while that task is not applying an update; a new one with each connect, so a task left
        # over from an earlier connect sets only its own.
        self._idle = asyncio.Event()
        self._idle.set()
        self._log = Logger(type(self).__name__)

    # -- lifecycle ---------------------------------------------------------------------------------

    @override
    async def _connect(self) -> None:
        await logged_failure(self._log, "execution client failed to connect", self._connect_venue())

    async def _connect_venue(self) -> None:
        if self._owner_loop is not self.loop:
            # An `asyncio.Lock` serves one loop. On the same loop it is kept: a call a cancelled hook left
            # running holds it until the call returns, so a given trading client, used again by this
            # connect, never has two calls on its one `requests.Session` at once.
            self._rest_lock = asyncio.Lock()
        self._owner_loop = self.loop
        self._stopping = False
        if self._given_stream is not None and self._given_stream_used:
            raise RuntimeError(
                "a trade updates stream given to the constructor serves one connect; build a new client "
                "to connect again"
            )
        self._trading, self._stream = self._given_trading, self._given_stream
        if self._trading is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._trading is None:
                self._trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
            if self._stream is None:
                self._stream = TradingStream(api_key=api_key, secret_key=secret_key, paper=True)
        with_http_timeout(
            self._trading
        )  # a client passed in too; refused without a session (ADR 0039)
        send_order_commands_once(self._trading)  # refused the same way
        await self._provider.initialize()
        for instrument in self._provider.list_all():
            self._handle_instrument(instrument)
        await self._report_account()
        self._updates = asyncio.Queue()
        self._idle = asyncio.Event()
        self._idle.set()
        self._consumer = self.create_task(
            self._consume_updates(self._updates, self._idle), "alpaca-trade-updates"
        )
        self._consumer.add_done_callback(retrieved)
        self._stream.subscribe_trade_updates(weak_coroutine(self._on_trade_update))
        self._stream_thread = threading.Thread(
            target=run_trade_stream,
            args=(self._stream, self._owner_loop, weak_callback(self._on_stream_exit)),
            name=f"{self.client_id}-trade-updates",
            daemon=True,
        )
        # Used from here on: a connect that failed before this point never ran it.
        self._given_stream_used = self._given_stream is not None
        self._stream_thread.start()

    @override
    async def _disconnect(self) -> None:
        await logged_failure(
            self._log, "execution client failed to disconnect", self._disconnect_venue()
        )

    async def _disconnect_venue(self) -> None:
        self._stopping = True
        thread = self._stream_thread
        if thread is not None and self._stream is not None:
            self._stream.stop()
        # The thread's join and the task's drain run together, so the client's own worst case is the longer
        # of the join and the drain (`TASK_GRACE_SECS` plus `TASK_ABORT_SECS`). Updates that still arrive
        # are dropped while stopping (`_enqueue_update`).
        consumer, self._consumer = self._consumer, None
        queue, idle = self._updates, self._idle
        waits: list[Awaitable[object]] = []
        if thread is not None:
            waits.append(join_within(thread, STREAM_JOIN_TIMEOUT_SECS))
        if consumer is not None:
            waits.append(drain(consumer, idle))
        await asyncio.gather(*waits)
        consumer_ended = consumer is None or consumer.done()
        while not queue.empty():  # so a task that outlived its cancel finds nothing left to apply
            queue.get_nowait()
        # Cleared whether or not the thread stopped, so a later connect builds fresh clients.
        self._stream_thread = None
        self._stream = None
        self._trading = None
        self._accepted.clear()
        self._finished.clear()
        self._records.clear()
        self._index.clear()
        self._acknowledged.clear()
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                f"Alpaca trade updates thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
            )
        if not consumer_ended:
            raise RuntimeError(
                "Alpaca trade updates task did not end within "
                f"{TASK_GRACE_SECS + TASK_ABORT_SECS} s of the disconnect",
            )

    @override
    async def _query_account(self, command: live.QueryAccount) -> None:
        """Read Alpaca's account and report its state, as at connect (Python adapter guide, "Query":
        `_query_account`). A query for an account this client does not hold is refused with a log line."""
        if command.account_id is not None and command.account_id != ACCOUNT_ID:
            self._log.error(
                f"Account query for {command.account_id}: this client holds {ACCOUNT_ID} only; nothing read"
            )
            return
        try:
            await self._report_account()
        except (APIError, OSError, ValueError, TypeError, RuntimeError, ArithmeticError) as e:
            # A failed read, a raw, incomplete or unparsable answer (each named), or a client not
            # connected: the state is left as it was.
            self._log.error(
                f"Alpaca account query failed: {e!r}; the account state is left as it was"
            )

    async def _report_account(self) -> None:
        account = await self._call(self._venue().get_account)
        if not isinstance(account, TradeAccount):
            raise TypeError(
                "alpaca-py returned raw account data; the client must use raw_data=False"
            )
        # Both are optional in alpaca-py's `TradeAccount`: a missing one is refused by name, not read as
        # USD or $0.
        if account.currency is None:
            raise ValueError("Alpaca's account answer has no currency")
        if account.currency != "USD":
            raise ValueError(f"Alpaca account currency is {account.currency}, not USD")
        if account.cash is None:
            raise ValueError("Alpaca's account answer has no cash")
        amount = decimal_value(account.cash, "cash")
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
            placed = await self._call(venue.submit_order, request, once=True)
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
        """Reject an order Alpaca refused, unless Alpaca holds it.

        Alpaca refuses a submit whose `client_order_id` it already holds, 422 "client_order_id must be
        unique" (paper, 2026-11-10), so a refusal does not prove the order absent. The submit itself is
        sent once (`without_resend`), so the order is looked up first: if Alpaca holds it, it is accepted, not
        rejected; if the lookup fails, the outcome is unknown.
        """
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or self._ended(client_order_id):
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
        except (OSError, ValueError, TypeError) as e:
            # A transport error, or a body alpaca-py cannot build: pydantic's `ValidationError` is a
            # `ValueError`, an empty or non-object body a `TypeError` (`Order(**answer)`), as in
            # `_orders_read`.
            self._log_unknown_outcome(
                client_order_id, f"refused ({error.status_code}), then the lookup failed ({e!r})"
            )
            return
        if client_order_id in self._accepted or self._ended(client_order_id):
            return  # a trade update applied during the lookup has settled it
        if isinstance(held, AlpacaOrder):
            self._log.warning(
                f"Alpaca refused a submit of {client_order_id} ({error.status_code}) but holds the "
                f"order: an earlier attempt reached it (status {held.status.value})",
            )
            if held.status == AlpacaOrderStatus.REJECTED:
                self._reject(
                    order,
                    f"Alpaca rejected the order (status rejected, after a refused submit "
                    f"{error.status_code}: {api_error_text(error)})",
                    dt_to_unix_nanos(held.updated_at),
                )
                return
            self._accept(order, VenueOrderId(str(held.id)), dt_to_unix_nanos(held.submitted_at))
            if not is_open_order(held):
                # Closed already, so its update may never come: Alpaca's read goes to the engine as a
                # report, as a `QueryOrder` answer does (`_query_order`).
                await self._send_report(held)
            return
        self._reject(
            order,
            f"Alpaca refused the order ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    @override
    async def _cancel_order(self, command: live.CancelOrder) -> None:
        await self._cancel_logged(command.client_order_id, command.venue_order_id)

    def _pending_cancel(self, client_order_id: ClientOrderId) -> bool:
        order = self.cache.order(client_order_id)
        return order is not None and order.status == OrderStatus.PENDING_CANCEL

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
        may hold an order the node still has as `SUBMITTED`. An order already `PENDING_CANCEL` is canceled
        again, as Kraken spot, Binance spot, Bybit and Interactive Brokers select every open order: its first
        cancel may have had no answer and the order may still work at Alpaca. A strategy sends this command only with `strategy_only=False`, and
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
        pending = [c for c in selected if self._pending_cancel(c)]
        for client_order_id in [c for c in selected if c not in pending] + pending:
            # Read again at its turn: the cancels before it took an HTTP round trip each, in which this
            # order may have filled, closed or been sent a cancel of its own.
            order = self.cache.order(client_order_id)
            if order is None or order.is_closed:
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
        venue_order_id = self._known_venue_id(client_order_id, venue_order_id, order.venue_order_id)
        try:
            if venue_order_id is None:
                found = await self._call(
                    self._venue().get_order_by_client_id, client_order_id.value
                )
                if not isinstance(found, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                venue_order_id = VenueOrderId(str(found.id))
            await self._call(self._venue().cancel_order_by_id, venue_order_id.value, once=True)
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
                if self._ended(client_order_id):
                    return  # an update applied during the attempt ended it
                self.generate_order_cancel_rejected(
                    order,
                    venue_order_id,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                self._refusal_settled(order)
                return
            self._log.error(
                f"Alpaca cancel for {client_order_id} got no answer ({e!r}); the order is left as it is",
            )
        # Answered 204: the cancel is confirmed by Alpaca's `canceled` trade update.

    async def _cancel_refused(
        self, order: Any, venue_order_id: VenueOrderId, error: APIError
    ) -> None:
        """Reject a cancel Alpaca refused, unless Alpaca shows it pending cancel or canceled already.

        A 422 can answer a cancel of an order a cancel before it, or Alpaca, already took. If Alpaca shows
        the order pending cancel, the `canceled` trade update confirms it and nothing is generated here;
        if the lookup fails, nothing is either. If it shows the order canceled, the `canceled` update may
        never come (a dropped stream sends none), so the order is canceled from what Alpaca shows
        (`_canceled_unseen`).
        """
        try:
            held = await self._call(self._venue().get_order_by_id, venue_order_id.value)
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
            self._log.error(
                f"Alpaca refused the cancel of {order.client_order_id} ({error.status_code}) and the "
                f"lookup failed ({e!r}); the order is left as it is",
            )
            return
        if self._ended(order.client_order_id):
            return  # an update applied during the lookup ended it: nothing is left to refuse
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.FILLED:
            # The cancel met a fill: the stream's `fill` ends the order, or, if it was lost, the engine's
            # in-flight check reads the order through `_query_order`.
            self._log.info(
                f"Alpaca refused the cancel of {order.client_order_id}: the order is filled; its fill "
                "settles it"
            )
            return
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.CANCELED:
            await self._canceled_unseen(order.client_order_id, held)
            return
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.PENDING_CANCEL:
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
        self._refusal_settled(order)

    async def _canceled_unseen(self, client_order_id: ClientOrderId, held: AlpacaOrder) -> None:
        """`OrderCanceled` for the node's order when Alpaca shows the Alpaca order that is now the node's
        canceled, as upstream's Bybit adapter emits `OrderCanceled` from an order it reads canceled
        (`cancel_all_orders`, `crates/adapters/bybit/src/execution.rs` at the pinned commit), stamped
        with Alpaca's `canceled_at`, else `updated_at`.

        The Alpaca order is the node's when the record names it current, or when its `replaces` chain
        leads to the node's order and the record has not left it (a replacement the node has not adopted
        yet, which a cancel redirected from its replaced order reaches). Bybit emits the cancel whatever the
        order filled; here the chain's filled shares as Alpaca shows them (`chain_filled`) must equal the
        node's count, since `_finish` drops every later update for it, so a fill the stream also missed
        would be lost: otherwise the order is left, with a warning, for reconciliation, which infers the
        fill. The order is then finished, so a `canceled` update arriving late adds nothing. A node order
        already closed generates nothing."""
        order = self.cache.order(client_order_id)
        if order is None or order.is_closed or client_order_id in self._finished:
            return
        record = self._record(order)
        held_id = str(held.id)
        if held_id != record.current:
            if held_id in record.superseded:
                self._log.info(
                    f"Alpaca shows {held_id} canceled, which {client_order_id} no longer names "
                    "(replaced); ignored"
                )
                return
            try:
                root = await self._root_id(held)
            except (APIError, OSError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Alpaca shows {held_id} canceled and tracing it failed ({e!r}); "
                    f"{client_order_id} is left for reconciliation"
                )
                return
            if root != client_order_id:
                self._log.warning(
                    f"Alpaca shows {held_id} canceled, which is not {client_order_id}'s order; "
                    "the order is left for reconciliation"
                )
                return
            if client_order_id in self._finished:
                return  # an update applied during the trace ended it
        self._see(record, held)
        shown = share_count(held.filled_qty)
        filled = (
            chain_filled(shown, record.earlier_filled(held_id) if held.replaces else Decimal(0))
            if shown is not None
            else None
        )
        if filled is None or filled != max(record.filled, order.filled_qty.as_decimal()):
            self._log.warning(
                f"Alpaca shows {held_id} canceled with {held.filled_qty} filled, where "
                f"{client_order_id} has {order.filled_qty}; the order is left for reconciliation"
            )
            return
        self._finish(client_order_id)
        self.generate_order_canceled(
            order, VenueOrderId(held_id), dt_to_unix_nanos(held.canceled_at or held.updated_at)
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
        venue_order_id = self._known_venue_id(
            order.client_order_id, command.venue_order_id, order.venue_order_id
        )
        denial = self._modify_denial(order, command, venue_order_id)
        if denial is not None or venue_order_id is None:
            self.generate_order_modify_rejected(
                order, venue_order_id, denial or "no venue order ID", self.clock.timestamp_ns()
            )
            self._refusal_settled(order)
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
        record = self._record(order)
        record.pending[venue_order_id.value] += 1
        try:
            answer = await self._call(
                self._venue().replace_order_by_id, venue_order_id.value, request, once=True
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
        answer_id = str(answer.id)
        self._index[answer_id] = order.client_order_id
        if self._records.get(order.client_order_id) is not record:
            return  # the order ended while the replace was answered; its updates are dropped
        if answer_id not in record.legs:  # the answer is older than any update of the replacement
            self._see(record, answer)
        if answer_id != record.current and answer_id not in record.superseded:
            record.answers[answer_id] = answer  # `replaced` takes it; adopted already, not kept

    async def _replace_refused(
        self,
        order: Any,
        venue_order_id: VenueOrderId,
        request: ReplaceOrderRequest,
        error: APIError,
    ) -> None:
        """Reject a modify Alpaca refused, unless Alpaca shows the order replaced as asked.

        A second replace of an order already replaced is answered 422 "order already replaced" (observed),
        and the replace itself is sent once (`without_resend`). So the order is looked up: replaced by an
        order with the terms this request asked for, an earlier replace took it and the `replaced`
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
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
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

    def _modify_rejected(
        self,
        order: Any,
        target: str,
        venue_order_id: VenueOrderId | None,
        reason: str,
        ts_event: int,
    ) -> None:
        """`OrderModifyRejected` for the replace this client sent to Alpaca order `target`, once: Alpaca can
        refuse one on the HTTP answer and on the stream, and the second refusal generates nothing; nor
        does one for an order that has ended, whose record is dropped (NautilusTrader drops a modify
        rejection for a filled order, observed)."""
        record = self._records.get(order.client_order_id)
        if record is None or record.pending[target] <= 0:
            self._log.info(
                f"Alpaca refused a replace of {order.client_order_id} ({reason}) with no modify awaiting an "
                "outcome (answered already, or sent before a reconnect); nothing generated",
            )
            return
        record.pending[target] -= 1
        self.generate_order_modify_rejected(order, venue_order_id, reason, ts_event)
        self._refusal_settled(order)

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
        # alpaca-py's `ReplaceOrderRequest` raises for a quantity or stop price that is not positive ("qty
        # must be greater than 0", "stop_price must be greater than 0", observed with 0.45.0), which left
        # the modify with no outcome; a limit price of 0 it passes, and no order rests at $0.
        for field, value in (
            ("quantity", command.quantity),
            ("limit price", command.price),
            ("stop price", command.trigger_price),
        ):
            if value is not None and not value.as_double() > 0:
                return f"{field} {value} is not positive"
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
        if getattr(order, "display_qty", None) is not None:  # a market order has no such field
            # Alpaca's order has no display quantity field ("Create an Order"); a stop-market order can
            # carry one in NautilusTrader too, and it would be sent at its full quantity.
            return "display quantity is not supported"
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
        """Generate `OrderAccepted` once per order, and only for an order still awaiting it.

        An order Alpaca acknowledges while a cancel or modify sent before the acknowledgement is pending
        (no venue order ID yet) is left pending, so the cancel's outcome is still awaited; the
        acknowledgement is kept, and generated if the cancel or modify is refused, which puts the order
        back to `SUBMITTED` (`_refusal_settled`). Without it the order would stay `SUBMITTED` for the
        in-flight check to resolve.
        """
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or client_order_id in self._finished:
            return
        # The cached order, not the command's snapshot: a cancel may have moved it on since the submit.
        current = self.cache.order(client_order_id) or order
        if current.status in (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED, OrderStatus.RELEASED):
            self._accepted.add(client_order_id)
            self._acknowledged.pop(client_order_id, None)
            self.generate_order_accepted(current, venue_order_id, ts_event)
        elif (
            current.status in (OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_UPDATE)
            and current.venue_order_id is None
        ):
            self._acknowledged[client_order_id] = (venue_order_id, ts_event)
        else:
            self._accepted.add(client_order_id)  # acknowledged already, as the engine holds it

    def _refusal_settled(self, order: Any) -> None:
        """After a cancel or modify refusal for an order Alpaca acknowledged while it was pending:
        `OrderAccepted`, which the refusal's return to `SUBMITTED` now awaits (the engine applies the two
        in the order generated)."""
        acknowledged = self._acknowledged.pop(order.client_order_id, None)
        if acknowledged is None or order.client_order_id in self._accepted:
            return
        record = self._records.get(order.client_order_id)
        if self._ended(order.client_order_id) or (record is not None and record.filled > 0):
            return  # moved on since: a fill, or an end, implies the acknowledgement
        current = self.cache.order(order.client_order_id) or order
        if current.venue_order_id is not None or current.filled_qty.as_double() > 0:
            return  # acknowledged already, as the engine holds it (a query report, a fill)
        self._accepted.add(order.client_order_id)
        self.generate_order_accepted(order, *acknowledged)

    def _known_venue_id(
        self,
        client_order_id: ClientOrderId,
        given: VenueOrderId | None,
        held: VenueOrderId | None,
    ) -> VenueOrderId | None:
        """Alpaca's order ID for a command: the command's, the node's, or one an acknowledgement kept while
        a cancel or modify was pending (`_accept`)."""
        kept = self._acknowledged.get(client_order_id)
        return given or held or (kept[0] if kept is not None else None)

    def _ended(self, client_order_id: ClientOrderId) -> bool:
        """Whether the node's order has ended, as this client has seen it or the engine holds it: read
        again after each `await`, since an update applied meanwhile may have ended it."""
        if client_order_id in self._finished:
            return True
        order = self.cache.order(client_order_id)
        return order is not None and order.is_closed

    def _reject(self, order: Any, reason: str, ts_event: int) -> None:
        """`OrderRejected`, once: nothing is generated for an order that has ended already (an update
        applied while a submit's answer or a lookup was awaited)."""
        if self._ended(order.client_order_id):
            self._log.info(
                f"Alpaca rejection of {order.client_order_id} ({reason}) came after the order ended; "
                "nothing generated"
            )
            return
        self._finish(order.client_order_id)
        self.generate_order_rejected(order, reason, ts_event, False)

    async def _consume_updates(
        self, updates: asyncio.Queue[TradeUpdate], idle: asyncio.Event
    ) -> None:
        """Apply trade updates one at a time, in the order the stream delivered them: an update for a
        replacement may need a lookup on Alpaca before it can be applied. The queue and the event are
        this connect's own, so a task left after a disconnect reads no new queue and marks no new event."""
        while True:
            update = await updates.get()
            idle.clear()
            try:
                await self._apply_trade_update(update)
            except Exception as e:  # noqa: BLE001 - one update's failure must not stop the rest
                self._log.error(f"Alpaca trade update {update.event} could not be applied: {e!r}")
            finally:
                idle.set()

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

    # -- the record of each node order's replace chain (`OrderRecord`) -------------------------------

    def _record(self, order: Any) -> OrderRecord:
        """The node order's record, built from the node's cache when first needed, as Binance spot's
        `restore_order` rebuilds its binding: the order's current venue order ID, the ones it has held
        before as superseded, its filled quantity, its quantity and its trade IDs."""
        client_order_id = order.client_order_id
        record = self._records.get(client_order_id)
        if record is not None:
            return record
        current = order.venue_order_id.value if order.venue_order_id is not None else None
        record = OrderRecord(
            client_order_id,
            current=current,
            filled=order.filled_qty.as_decimal(),
            quantity=order.quantity.as_decimal(),
            trades={str(trade_id) for trade_id in order.trade_ids},
        )
        for venue_order_id in order.venue_order_ids:
            leg_id = venue_order_id.value
            record.legs.setdefault(leg_id, Leg(leg_id))
            self._index[leg_id] = client_order_id
            if leg_id != current:
                record.superseded.add(leg_id)
        self._records[client_order_id] = record
        return record

    def _see(self, record: OrderRecord, alpaca_order: AlpacaOrder) -> Leg:
        """Record Alpaca's view of one of the order's legs and index it to the order."""
        self._index[str(alpaca_order.id)] = record.client_order_id
        return record.see(alpaca_order)

    def _held_by_venue_id(self, alpaca_order_id: str) -> ClientOrderId | None:
        """The node's order the cache indexes this Alpaca order ID to, if it holds one."""
        client_order_id = self.cache.client_order_id(VenueOrderId(alpaca_order_id))
        if client_order_id is None or self.cache.order(client_order_id) is None:
            return None
        return client_order_id

    async def _node_order_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId | None:
        """The node's client order ID for an Alpaca order, or None if the node holds none: its own client
        order ID, then the index, then the node's cache, and a walk of `replaces` only on a miss."""
        own = holdable_id(alpaca_order.client_order_id)
        if own is not None and self.cache.order(own) is not None:
            return own
        root = await self._root_id(alpaca_order, known)
        return root if root is not None and self.cache.order(root) is not None else None

    async def _root_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId | None:
        """The client order ID of the order an Alpaca order's `replaces` chain starts from, or None if it
        has none NautilusTrader can hold.

        A replacement carries a client order ID Alpaca made (observed), so it is traced back through its
        `replaces` links. Each link is resolved from what costs no read first: the client's index, the
        node's cache (`ClientCache.client_order_id`, which holds every venue order ID a node order has
        had), and the orders already fetched; only then is the order read, at most `MAX_REPLACE_CHAIN`
        times, and a walk that would need more fails by name rather than stopping at an order that is not
        the chain's first. An order Alpaca names in `replaces` and does not find (404) is a chain that
        cannot be read, and fails the same way. Every result is indexed by Alpaca's order ID, unique to
        each order, never by a client order ID, which an order made outside the node can leave empty or
        share; a chain that ends without reaching the node's orders is indexed too, so an order traced
        before the node holds its original (the start-up window) resolves once it does, and no chain is
        walked twice. A client order ID NautilusTrader cannot hold (empty or not ASCII, as an order made
        outside the node can carry) ends the chain: the trace stops at the order before it.
        """
        order_id = str(alpaca_order.id)
        if order_id in self._index:
            return self._index[order_id]
        held = self._held_by_venue_id(order_id)
        if held is not None:
            self._index[order_id] = held
            return held
        root = holdable_id(alpaca_order.client_order_id)
        replaces = alpaca_order.replaces
        walked = {order_id}
        reads = 0
        while replaces is not None and str(replaces) not in walked:
            prior_id = str(replaces)
            walked.add(prior_id)
            if prior_id in self._index:
                root = self._index[prior_id] or root
                break
            held = self._held_by_venue_id(prior_id)
            if held is not None:
                root = held
                break
            prior = (known or {}).get(prior_id)
            if prior is None:
                if reads == MAX_REPLACE_CHAIN:
                    raise ValueError(
                        f"Alpaca order {order_id}'s replace chain needs more than {MAX_REPLACE_CHAIN} "
                        "reads to trace"
                    )
                reads += 1
                prior = await self._call(self._venue().get_order_by_id, prior_id)
            if not isinstance(prior, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            prior_cid = holdable_id(prior.client_order_id)
            if prior_cid is None:
                break  # the order before cannot be held: the trace stops here
            root = prior_cid
            if self.cache.order(root) is not None:
                break
            replaces = prior.replaces
        self._index[order_id] = root
        return root

    async def _apply(self, update: TradeUpdate) -> None:
        """Apply one trade update through the node order's record.

        Each Alpaca order of the chain plays one part in the record: the order the node's order is on now
        (`current`), one it has left (`superseded`), or a replacement it has not moved to. A late update of
        a superseded order is ignored, but for its fills, as Binance spot's `record_order_update` returns
        nothing for a superseded venue order ID while a fill on one is the node order's. A replacement is
        adopted once, by `replaced` or by its own update when the record shows it took over (the order it
        replaces is replaced by it) or an execution proves it live, as Binance spot's
        `record_order_update_from_fill` updates from a fill; its `new` alone does not adopt it, since it
        may still be rejected. A replacement's `rejected` took over only if the order it replaces is
        replaced by it, as the record shows or, failing that, one read of that order: then nothing works
        at Alpaca and the node's order ends (`_rejected_takeover`); else the modify failed, or the
        replacement was one a later replace left behind.
        """
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
        leg_id = str(alpaca_order.id)
        venue_order_id = VenueOrderId(leg_id)
        ts_event = dt_to_unix_nanos(update.timestamp)
        if client_order_id in self._finished:
            if event == "rejected" and alpaca_order.replaces is not None:
                # Alpaca: if the old order "is filled before the replacing (new) order reaches the
                # execution venue, the replacing (new) order is rejected". NautilusTrader drops a modify
                # rejection for a filled order (observed), so the fill stands as the outcome.
                self._log.info(
                    f"Alpaca rejected the replacement {leg_id} of {client_order_id}, which had filled "
                    "first; the fill is the outcome",
                )
            return
        if order.is_closed:
            # Ended by the engine, from a `QueryOrder` report (`_query_order`) or reconciliation, which
            # infers the fills Alpaca's read shows: a fill update arriving after it is one of those, so
            # applying it would count it twice. The client's state for the order is dropped.
            self._finish(client_order_id)
            self._log.info(
                f"Alpaca trade update {event} for {client_order_id}, which has ended; ignored"
            )
            return
        record = self._record(order)
        leg = self._see(record, alpaca_order)
        if event == "replaced":
            await self._replaced(order, record, alpaca_order, ts_event)
            return
        if event == "order_replace_rejected":
            record.answers = {k: a for k, a in record.answers.items() if str(a.replaces) != leg_id}
            self._modify_rejected(
                order,
                leg_id,
                venue_order_id,
                "Alpaca rejected the replace (trade update)",
                ts_event,
            )
            return
        if event == "pending_replace":
            return  # NautilusTrader generates `OrderPendingUpdate` itself when a modify is sent
        if leg_id != record.current and (
            leg_id in record.superseded or (leg.replaces is None and record.current is not None)
        ):
            if event in FILL_EVENTS:
                # An execution on the order before it was replaced, which can arrive after the
                # replacement's own fills.
                self._fill(order, record, update, venue_order_id, ts_event)
            else:
                self._log.info(
                    f"Alpaca trade update {event} for {venue_order_id}, which {client_order_id} no longer "
                    "names (replaced); ignored",
                )
            return
        if leg_id != record.current and leg.replaces is not None:
            took_over = record.took_over(leg_id)
            if event == "rejected":
                if took_over is None:
                    took_over = await self._took_over(alpaca_order, None)
                    if self._ended(client_order_id):
                        return  # an update applied during the read ended it
                if took_over:
                    self._adopt(order, record, alpaca_order, ts_event)
                    self._rejected_takeover(order, record, leg_id, ts_event)
                    return
                self._rejected_replacement(order, record, alpaca_order, ts_event)
                return
            if took_over is False:
                self._log.info(
                    f"Alpaca trade update {event} for {leg_id}, a replacement of {client_order_id}'s "
                    "order that another replace left behind; ignored",
                )
                return
            if took_over or event in FILL_EVENTS:
                self._adopt(order, record, alpaca_order, ts_event)
            elif event not in ("canceled", "expired", "order_cancel_rejected"):
                return  # its `new`: the replacement may still be rejected; `replaced` adopts it
        elif record.current is None:
            record.current = leg_id  # the order's own first update, before the submit's answer
        if event == "rejected":
            if alpaca_order.replaces is not None:
                self._rejected_takeover(order, record, leg_id, ts_event)
                return
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
            self._refusal_settled(order)
            return
        if event not in ACCEPTING_EVENTS | FILL_EVENTS | {"canceled", "expired"}:
            self._log.warning(
                f"Alpaca trade update {event} for {client_order_id} is not mapped in this slice; ignored",
            )
            return
        self._accept(order, venue_order_id, ts_event)
        if event in FILL_EVENTS:
            self._fill(order, record, update, venue_order_id, ts_event)
            return  # `_fill` ends the order once its fills reach its quantity
        if event == "canceled":
            self.generate_order_canceled(order, venue_order_id, ts_event)
        elif event == "expired":
            self.generate_order_expired(order, venue_order_id, ts_event)
        if event in TERMINAL_EVENTS:
            self._finish(client_order_id)

    def _rejected_takeover(
        self, order: Any, record: OrderRecord, leg_id: str, ts_event: int
    ) -> None:
        """The node's order, moved to a replacement Alpaca then rejected: nothing works at Alpaca, so it
        ends. Rejected if it has no fill; partly filled, it ends canceled, as Binance spot's
        `reject_replace` emits the old order's withheld `OrderCanceled` when its replacement is rejected,
        since NautilusTrader drops `OrderRejected` for a partly filled order (`crates/model/src/orders/
        mod.rs` at the pin: no `PartiallyFilled` to `Rejected` transition)."""
        if max(record.filled, order.filled_qty.as_decimal()) > 0:
            self._finish(order.client_order_id)
            self._log.info(
                f"Alpaca rejected the replacement {leg_id} of {order.client_order_id} after it took over; "
                "the order ends canceled with its fills"
            )
            self.generate_order_canceled(order, VenueOrderId(leg_id), ts_event)
            return
        self._reject(order, f"Alpaca rejected the replacement order {leg_id}", ts_event)

    def _rejected_replacement(
        self, order: Any, record: OrderRecord, replacement: AlpacaOrder, ts_event: int
    ) -> None:
        """A replacement rejected without taking over: the modify that sent it failed, if one awaits an
        outcome on the order it replaces; a replacement a later replace left behind changes nothing."""
        target = str(replacement.replaces)
        record.answers.pop(str(replacement.id), None)
        prior = record.legs.get(target)
        if prior is not None and prior.replaced_by not in (None, str(replacement.id)):
            self._log.info(
                f"Alpaca rejected {replacement.id}, a replacement of {order.client_order_id}'s order "
                f"{target}, which {prior.replaced_by} replaced; ignored"
            )
            return
        self._modify_rejected(
            order,
            target,
            VenueOrderId(str(replacement.id)),
            f"Alpaca rejected the replacement order {replacement.id} (trade update)",
            ts_event,
        )

    def _finish(self, client_order_id: ClientOrderId) -> None:
        """Mark the node's order ended and drop its record, as Binance spot's `cleanup_terminal` does; the
        index entries of its Alpaca orders go too: a late update of one is still known as the ended
        order's, through the node's cache, which holds every venue order ID the order has had."""
        self._finished.add(client_order_id)
        record = self._records.pop(client_order_id, None)
        for leg_id in record.legs if record is not None else ():
            if self._index.get(leg_id) == client_order_id:
                del self._index[leg_id]
        self._acknowledged.pop(client_order_id, None)

    async def _replaced(
        self, order: Any, record: OrderRecord, old: AlpacaOrder, ts_event: int
    ) -> None:
        """Alpaca's `replaced` for an order of the node's chain: `OrderUpdated` with the replacement's
        values.

        The update carries the old order, with `replaced_by` (observed). The replacement is the PATCH
        answer if it is in, otherwise read from Alpaca, and followed on if Alpaca has replaced it again
        since; if none yields it, nothing is generated and reconciliation settles the order, so no value
        is invented. A replacement the record has already moved to or past, by an earlier `replaced` or
        its own update, is not adopted again. One Alpaca has closed already took over and ended with no
        working order left: the order ends with it, unless Alpaca shows it more filled than the node has
        seen (`chain_filled`), when its own updates, still to come, end it.
        """
        if old.replaced_by is None:
            self._log.error(
                f"Alpaca replaced {old.id} without saying by which order; nothing applied"
            )
            return
        record.superseded.add(str(old.id))
        target = str(old.replaced_by)
        if target == record.current or target in record.superseded:
            return  # adopted already, by the replacement's own update: nothing to read or update
        replacement: AlpacaOrder | RawData | None = record.answers.pop(target, None)
        try:
            if replacement is None:
                replacement = await self._call(self._venue().get_order_by_id, target)
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
                self._see(record, replacement)
                record.superseded.add(str(replacement.id))
                next_id = str(replacement.replaced_by)
                replacement = record.answers.pop(next_id, None) or await self._call(
                    self._venue().get_order_by_id, next_id
                )
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
            self._log.error(
                f"Alpaca replaced {order.client_order_id} by {old.replaced_by}, which could not be read "
                f"({e!r}); no OrderUpdated, reconciliation settles it",
            )
            return
        if not isinstance(replacement, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        if self._ended(order.client_order_id):
            return  # an update or a command's answer ended it during the reads
        replacement_id = str(replacement.id)
        self._adopt(order, record, replacement, ts_event)
        shown = share_count(replacement.filled_qty)
        seen = max(record.filled, order.filled_qty.as_decimal())
        if not is_open_order(replacement) and (
            shown is None or chain_filled(shown, record.earlier_filled(replacement_id)) > seen
        ):
            self._log.info(
                f"Alpaca shows the replacement {replacement_id} of {order.client_order_id} "
                f"{replacement.status.value} with {replacement.filled_qty} filled, more than the node "
                "has seen; its own updates end the order"
            )
            return
        if replacement.status == AlpacaOrderStatus.REJECTED:
            self._rejected_takeover(order, record, replacement_id, ts_event)
        elif replacement.status in (AlpacaOrderStatus.CANCELED, AlpacaOrderStatus.EXPIRED):
            self._finish(order.client_order_id)
            closed_at = dt_to_unix_nanos(
                replacement.canceled_at or replacement.expired_at or replacement.updated_at
            )
            venue_id = VenueOrderId(replacement_id)
            if replacement.status == AlpacaOrderStatus.CANCELED:
                self.generate_order_canceled(order, venue_id, closed_at)
            else:
                self.generate_order_expired(order, venue_id, closed_at)

    def _adopt(
        self, order: Any, record: OrderRecord, replacement: AlpacaOrder, ts_event: int
    ) -> None:
        """`OrderUpdated` moving the node's order to Alpaca's replacement, once per replacement, as Binance
        spot's `record_order_update` moves its binding's `current` and pushes the old one to `superseded`.
        Every replace sent to an order the record has now left has its outcome."""
        replacement_id = str(replacement.id)
        if record.current == replacement_id:
            return  # adopted already: one transition, one `OrderUpdated`
        self._see(record, replacement)
        # The fills before this replacement: the record's view of the orders it replaced, or the node's
        # own count, all of it on those orders until this one is adopted.
        earlier = max(
            record.earlier_filled(replacement_id), record.filled, order.filled_qty.as_decimal()
        )
        qty = share_count(replacement.qty)
        report = order_status_report(
            replacement,
            self.clock.timestamp_ns(),
            order.trigger_type if order.order_type in STOP_TYPES else None,
            order.client_order_id,
            quantity=chain_quantity(qty, earlier) if qty is not None else None,
        )
        if report is None:
            raise ValueError(f"replacement {replacement_id} is not an order this slice maps")
        if record.current is not None:
            record.superseded.add(record.current)
        if replacement.replaces is not None:
            record.superseded.add(str(replacement.replaces))
        record.superseded.discard(replacement_id)
        record.current = replacement_id
        record.quantity = report.quantity.as_decimal()
        record.answers.pop(replacement_id, None)
        for target in record.superseded:
            record.pending.pop(target, None)
        self.generate_order_updated(
            order,
            VenueOrderId(replacement_id),
            report.quantity,
            report.price,
            report.trigger_price,
            None,
            ts_event,
        )

    def _fill(
        self,
        order: Any,
        record: OrderRecord,
        update: TradeUpdate,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """`OrderFilled` for one fill update, once per trade ID, and the node's order ended once its fills
        reach its quantity: on the record's own count, not on Alpaca's event name, so a fill on the order a
        replace superseded that arrives after the replacement's `fill` is still applied (as upstream's
        Binance spot keeps a superseded venue order bound to the node's order,
        `record_order_update_from_fill`), and a fill delivered twice is applied once (NautilusTrader's
        order takes one fill per trade ID, and Binance spot skips a trade it has seen, `has_trade`). A
        fill that cannot be applied is logged and ends nothing."""
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
        client_order_id = order.client_order_id
        trade_id = str(update.execution_id)
        # A trade ID the node's order holds was applied already, by this client or by the engine from a
        # report (`_query_order`, reconciliation), whose fill the record's count has not seen.
        if trade_id in record.trades or (
            order.filled_qty.as_decimal() > record.filled
            and any(str(t) == trade_id for t in order.trade_ids)
        ):
            self._log.info(
                f"Alpaca {update.event} {trade_id} for {client_order_id} was applied already; ignored"
            )
            return
        record.trades.add(trade_id)
        self._acknowledged.pop(client_order_id, None)  # a fill implies it
        record.filled = max(record.filled, order.filled_qty.as_decimal()) + last_qty.as_decimal()
        self.generate_order_filled(
            order,
            venue_order_id,
            None,
            TradeId(trade_id),
            last_qty,
            fill_price(update.price),
            USD,
            None,
            LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event,
        )
        if record.filled >= record.quantity:
            self._finish(client_order_id)

    def _on_stream_exit(self, exit: StreamExit) -> None:
        stream, error = exit
        if stream is self._stream and not self._stopping:
            self._log.error(f"Alpaca trade updates stream ended while connected: {error!r}")

    async def _on_trade_update(self, update: TradeUpdate | RawData) -> None:
        """On the stream's thread: hand the update to the node's loop, where it is queued in order."""
        if self._owner_loop is None:
            return
        if isinstance(update, TradeUpdate):
            post(self._owner_loop, self._enqueue_update, update)
        else:  # alpaca-py hands raw data only to a stream built with `raw_data=True`
            post(
                self._owner_loop,
                self._log.error,
                f"Alpaca trade update not parsed: {update!r:.200}",
            )

    def _enqueue_update(self, update: TradeUpdate) -> None:
        if not self._stopping:
            self._updates.put_nowait(update)

    # -- reconciliation reports --------------------------------------------------------------------

    @override
    async def _generate_order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        return await logged_failure(
            self._log, "order status report failed", self._order_status_report(command)
        )

    @override
    async def _generate_order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        return await logged_failure(
            self._log, "order status reports failed", self._order_status_reports(command)
        )

    @override
    async def _generate_fill_reports(self, command: live.GenerateFillReports) -> list[FillReport]:
        return await logged_failure(self._log, "fill reports failed", self._fill_reports(command))

    @override
    async def _generate_position_status_reports(
        self, command: live.GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        return await logged_failure(
            self._log, "position status reports failed", self._position_status_reports(command)
        )

    @override
    async def _query_order(self, command: live.QueryOrder) -> None:
        """Read one order from Alpaca and hand its status report to the engine.

        NautilusTrader's in-flight check sends `QueryOrder` for an order left `SUBMITTED`, `PENDING_CANCEL`
        or `PENDING_UPDATE` past its threshold, and its open-order check for an order the venue's open
        orders leave out; past the last retry an order never answered is resolved locally, a `SUBMITTED`
        one as rejected (`check_inflight_orders`, `crates/live/src/execution/manager.rs` at the pinned
        commit). Upstream's Binance spot, Bybit, Coinbase, Kraken spot and OKX adapters answer it the same
        way (`query_order` in each `execution.rs` at the pinned commit): the order's status report, sent
        to the engine; an order not found or a read that fails is logged and nothing is sent, so the
        engine's next check asks again. Here the report is `_order_status_report`'s, the one start-up
        reconciliation reads, and it goes to the engine through the base class's `_handle_report`.
        """
        try:
            report = await self._order_status_report_by(
                command.venue_order_id, command.client_order_id
            )
        except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
            self._log.warning(
                f"Alpaca order query for {command.client_order_id} failed ({e!r}); no report sent"
            )
            return
        if report is None:
            self._log.warning(
                f"Alpaca holds no order {command.venue_order_id or command.client_order_id} for the "
                f"query of {command.client_order_id}; no report sent"
            )
            return
        self._handle_report(report)

    async def _send_report(self, held: AlpacaOrder) -> None:
        """Hand Alpaca's read of an order to the engine as its status report, as `_query_order` does."""
        try:
            report = await self._order_report(held)
        except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
            self._log.warning(f"Alpaca order {held.id} could not be reported ({e!r})")
            return
        if report is not None:
            self._handle_report(report)

    async def _order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        return await self._order_status_report_by(command.venue_order_id, command.client_order_id)

    async def _order_status_report_by(
        self, venue_order_id: VenueOrderId | None, client_order_id: ClientOrderId | None
    ) -> OrderStatusReport | None:
        if venue_order_id is None and client_order_id is not None:
            # The node's own record or cache names the Alpaca order its order is on now: one read, where
            # a lookup by client order ID gives the chain's first order and a walk to the order working.
            record = self._records.get(client_order_id)
            held = self.cache.order(client_order_id)
            current = record.current if record is not None else None
            if current is None and held is not None and held.venue_order_id is not None:
                current = held.venue_order_id.value
            venue_order_id = VenueOrderId(current) if current is not None else None
        try:
            if venue_order_id is not None:
                found = await self._call(self._venue().get_order_by_id, venue_order_id.value)
            elif client_order_id is not None:
                found = await self._call(
                    self._venue().get_order_by_client_id, client_order_id.value
                )
            else:
                raise ValueError("an order status report needs a venue or client order ID")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        walked: dict[str, AlpacaOrder] = {}
        for _ in range(MAX_REPLACE_CHAIN):
            if not isinstance(found, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            if found.status != AlpacaOrderStatus.REPLACED or found.replaced_by is None:
                break
            # The node's IDs can name an order since replaced; the live order is its replacement.
            walked[str(found.id)] = found
            try:
                found = await self._call(self._venue().get_order_by_id, str(found.replaced_by))
            except APIError as e:
                if e.status_code != 404:
                    raise
                # As a 404 on the first lookup: Alpaca holds no such order, so there is no report.
                self._log.warning(
                    f"Alpaca order {found.id} was replaced by {found.replaced_by}, which Alpaca does "
                    "not find; no report"
                )
                return None
        if not isinstance(found, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        return await self._order_report(found, walked)

    async def _order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        """Every open order, whenever it was submitted, up to `ORDERS_PAGE_LIMIT` (Alpaca's most per
        request; a full page is logged as possibly truncated), and, unless the command asks for open
        orders only, the orders submitted in its window.

        The open orders are read on their own, with no window, as upstream's Kraken spot adapter reads
        its open orders before its windowed order history (`request_order_status_reports_checked`,
        `crates/adapters/kraken/src/http/spot/client.rs` at the pinned commit): Alpaca's `after` and
        `until` filter on submission time, so a window would miss a working order placed before it, and
        a read of every status returns the newest `ORDERS_PAGE_LIMIT`, which newer closed orders can
        fill without it.
        """
        symbols = (
            [command.instrument_id.symbol.value] if command.instrument_id is not None else None
        )
        # Open orders first, as Kraken spot reads them: an order in both reads keeps the later copy, so
        # one that closed between the two is reported closed.
        requests = [
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT, symbols=symbols)
        ]
        if not command.open_only:
            requests.append(
                GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    limit=ORDERS_PAGE_LIMIT,
                    after=unix_nanos_to_dt(command.start) if command.start is not None else None,
                    until=unix_nanos_to_dt(command.end) if command.end is not None else None,
                    symbols=symbols,
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
        # An order replaced between the two reads can be in the first as working and its replacement in
        # the second: the replacement is reported, under the node's ID, and the stale copy is not. A
        # rejected replacement does not count: the order it replaced may have filled first.
        # While Alpaca shows the old order `pending_replace`, the replace has not taken effect: the old
        # order is reported at its own terms and the replacement is not.
        superseded: set[str] = set()
        not_yet: set[str] = set()
        for order in found.values():
            if order.replaces is None or order.status == AlpacaOrderStatus.REJECTED:
                continue
            prior = found.get(str(order.replaces))
            if prior is not None and prior.status == AlpacaOrderStatus.PENDING_REPLACE:
                not_yet.add(str(order.id))
            else:
                superseded.add(str(order.replaces))
        reports = []
        for order in found.values():
            if str(order.id) in superseded or str(order.id) in not_yet:
                continue
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
        from the order reports as it did before fill reports existed; a body that is not JSON, or not a
        list, counts as unread. An order whose listed fills cannot be shown to be all its fills, as when
        the activities lag the stream, gets no reports (`_complete`), so the engine infers its fills
        rather than taking a part of them.
        """
        try:
            # For one order, every order of its replace chain: a fill listed under an order since
            # replaced is reported on the order that replaced it, as the bulk request reports it.
            chain = (
                await self._chain_ids(command.venue_order_id)
                if command.venue_order_id is not None
                else None
            )
            activities = await self._fill_activities(command, chain)
        except (APIError, OSError, TypeError, KeyError, ValueError) as e:
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
            reports.append(report)
        # Each activity was asked for by a chain member's ID; the chain is applied once its fills are
        # moved to the order now working.
        completed = await self._complete(reports, activities)
        return [r for r in completed if chain is None or r.venue_order_id.value in chain]

    async def _chain_ids(self, venue_order_id: VenueOrderId) -> set[str]:
        """The Alpaca order IDs of `venue_order_id`'s replace chain: the order, the ones it replaced
        (`replaces`) and the ones replacing it (`replaced_by`), each at most `MAX_REPLACE_CHAIN` links;
        a link that cannot be read ends the walk that way, with a warning."""
        ids = {venue_order_id.value}
        try:
            start = await self._call(self._venue().get_order_by_id, venue_order_id.value)
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {venue_order_id} could not be read for its replace chain ({e!r}); its "
                "own fills are read"
            )
            return ids
        if not isinstance(start, AlpacaOrder):
            return ids
        for link in ("replaces", "replaced_by"):
            order = start
            for _ in range(MAX_REPLACE_CHAIN):
                next_id = getattr(order, link)
                if next_id is None or str(next_id) in ids:
                    break
                ids.add(str(next_id))
                try:
                    order = await self._call(self._venue().get_order_by_id, str(next_id))
                except (APIError, OSError, ValueError, TypeError) as e:
                    self._log.warning(
                        f"Alpaca order {next_id}, in the replace chain of {venue_order_id}, could not "
                        f"be read ({e!r}); the chain's fills are read as far as it"
                    )
                    break
                if not isinstance(order, AlpacaOrder):
                    break
        return ids

    async def _fill_activities(
        self, command: live.GenerateFillReports, order_ids: set[str] | None = None
    ) -> list:
        if order_ids is None:
            return await self._fill_pages(command, None)
        activities: list = []
        for order_id in sorted(order_ids):
            activities.extend(await self._fill_pages(command, order_id))
        return activities

    async def _fill_pages(self, command: live.GenerateFillReports, order_id: str | None) -> list:
        params: dict[str, str | int] = {"direction": "desc", "page_size": ACTIVITIES_PAGE_SIZE}
        if order_id is not None:
            params["order_id"] = order_id
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
            token = page[-1].get("id") if isinstance(page[-1], dict) else None
            if not isinstance(token, str) or not token:
                # The reference's `page_token` is "the ID of the end of your current page of results";
                # without it the read cannot go on. What was read is kept, and `_complete` drops an
                # order whose older fills this leaves out.
                self._log.warning(
                    "Alpaca fill history stopped at an activity with no readable `id`; older fills, if "
                    "any, are not reported",
                )
                return activities
            params["page_token"] = token
        self._log.warning(
            f"Alpaca fill history read to its limit of {FILL_PAGES_LIMIT} pages of {ACTIVITIES_PAGE_SIZE}; "
            "older fills, if any, are not reported",
        )
        return activities

    async def _complete(self, reports: list[FillReport], activities: list) -> list[FillReport]:
        """Drop the reports of an order whose listed fills cannot be shown to be all of its fills, so the
        engine infers that order's fills rather than taking a part of them (package README).

        An order's listed fills are all of them when its newest activity leaves nothing to fill
        (`leaves_qty` 0, "The quantity of shares remaining to be filled") and they add up to that
        activity's `cum_qty` ("The cumulative quantity of shares involved in the execution"). Short of
        `cum_qty`, as when the window or the page limit cut older fills off, they are not. Otherwise
        (shares left to fill, either field absent or unreadable) Alpaca's order is read and they are all
        of them only when its `filled_qty` is no more than they add up to, whatever the fills' age: the
        activities can lag the stream by an amount no page states. An order that cannot be read, or that
        alpaca-py cannot build, keeps no reports. The cost is in `_orders_read` and `_replaced_fills`.
        """
        newest: dict[str, dict] = {}
        for activity in activities:  # newest first
            if isinstance(activity, dict) and activity.get("order_id") is not None:
                newest.setdefault(str(activity["order_id"]), activity)
        # NautilusTrader's order "enforces one quantity-bearing fill per `trade_id`" ("Duplicate fill
        # detection", `docs/concepts/execution/index.md` at the pinned commit), so an activity listed
        # twice is one fill, counted and reported once. Whether Alpaca ever repeats a row is not on its
        # pages (UNVERIFIED); a trade ID listed with two quantities or prices cannot be told apart, and
        # its order's fills are left to the engine.
        partial: set[str] = set()
        first: dict[tuple[str, str], FillReport] = {}
        unique: list[FillReport] = []
        for report in reports:
            key = (report.venue_order_id.value, report.trade_id.value)
            seen = first.get(key)
            if seen is None:
                first[key] = report
                unique.append(report)
            elif fill_terms(seen) != fill_terms(report):
                partial.add(key[0])
        conflicted = set(partial)  # never shown complete, chain or not
        reports = unique
        listed: dict[str, Decimal] = {}
        for report in reports:
            key = report.venue_order_id.value
            listed[key] = listed.get(key, Decimal(0)) + report.last_qty.as_decimal()
        to_check: list[str] = []
        for order_id, qty in listed.items():
            last = newest.get(order_id, {})
            cum = share_count(last.get("cum_qty"))
            leaves = share_count(last.get("leaves_qty"))
            # Listed fills short of `cum_qty` are read too: in a replace chain they can be all of an
            # order's own while its `cum_qty` counts the chain's (`FilledReading.CHAIN`); on its own, the
            # order's `filled_qty` shows the fills missing.
            if cum is None or leaves != 0 or qty < cum:
                to_check.append(order_id)
        held = await self._orders_read(to_check)
        moved, chained = await self._replaced_fills(held, listed, newest, partial, conflicted)
        for order_id in to_check:
            if order_id in chained:
                continue
            order = held.get(order_id)
            shown = share_count(order.filled_qty) if order is not None else None
            if shown is None or shown > listed[order_id]:
                partial.add(order_id)
        for order_id in partial - chained:
            self._log.warning(
                f"Alpaca lists only some fills of order {order_id}, or they cannot be shown to be all of "
                "them; its fills are left to the engine to infer",
            )
        return [
            moved_report(r, moved[r.venue_order_id.value]) if r.venue_order_id.value in moved else r
            for r in reports
            if r.venue_order_id.value not in partial
        ]

    async def _replaced_fills(
        self,
        held: dict[str, AlpacaOrder | None],
        listed: dict[str, Decimal],
        newest: dict[str, dict],
        partial: set[str],
        conflicted: set[str],
    ) -> tuple[dict[str, VenueOrderId], set[str]]:
        """For each order read that Alpaca shows replaced, the live order its fills move to, by the
        replaced order's ID, and every order this settles (moved, kept or added to `partial` here, each
        with its own warning).

        Alpaca lists a fill before a replace under the order replaced, which is not reported: its
        replacement is, under the node's client order ID. The fill is the node's order's, as on the
        stream, where a fill on a superseded order is applied to the node's order, as upstream's Binance
        spot keeps a cancel-replace's superseded venue order IDs bound to the node's order
        (`OrderBinding`'s `superseded`, `record_order_update_from_fill`,
        `crates/adapters/binance/src/common/dispatch.rs` at the pinned commit). So it is reported on the
        live order, when that is consistent with the live order's report. Under
        `FILLED_READING`, `CHAIN`, observed on paper overnight (ADR 0042):

        * if the live order lists fills of its own, its newest activity's `cum_qty` must equal its
          `filled_qty` (nothing on it unlisted, read either way); then its own fills adding up to
          `filled_qty` show it counts only its own: they are kept and the earlier ones left to the
          engine; the chain's adding up to it shows it counts them: they move to it;
        * if it lists none, a `filled_qty` equal to the earlier fills is read as counting them (a fill
          of its own that is not listed yet, adding up to the same, cannot be told apart: UNVERIFIED),
          and a `filled_qty` of 0 as not counting them;
        * anything else, or a trade ID repeated with other terms, leaves the chain to the engine.

        A replaced order's newest fill always leaves shares to fill (a filled order cannot be replaced),
        so it is among the orders read; each link not read already costs one `get_order_by_id`.
        """
        ends: dict[str, AlpacaOrder | None] = {}
        for order_id, order in list(held.items()):
            if order is not None and order.status == AlpacaOrderStatus.REPLACED:
                ends[order_id] = await self._live_end(order, held)
        # The orders read on the way to a live order are of its chain too.
        for order_id, order in list(held.items()):
            if (
                order_id not in ends
                and order is not None
                and order.status == AlpacaOrderStatus.REPLACED
            ):
                ends[order_id] = await self._live_end(order, held)
        moved: dict[str, VenueOrderId] = {}
        settled: set[str] = set()
        for order_id, end in ends.items():
            if end is None:
                partial.add(order_id)
                settled.add(order_id)
        for live_id in {str(end.id) for end in ends.values() if end is not None}:
            members = sorted(
                k for k, end in ends.items() if end is not None and str(end.id) == live_id
            )
            live = next(end for end in ends.values() if end is not None and str(end.id) == live_id)
            earlier = sum((listed.get(k, Decimal(0)) for k in members), Decimal(0))
            own = listed.get(live_id, Decimal(0))
            filled = share_count(live.filled_qty)
            settled.update([*members, live_id])
            cum = share_count(newest.get(live_id, {}).get("cum_qty"))
            if conflicted & {*members, live_id} or (own > 0 and (cum is None or cum != filled)):
                outcome = "partial"
            elif FILLED_READING is FilledReading.OWN:
                # Each order counts its own fills: the chain's are all listed when each order's are.
                each = all(
                    listed.get(k, Decimal(0))
                    == share_count(getattr(held.get(k), "filled_qty", None))
                    for k in members
                )
                outcome = "moved" if each and own == filled else "partial"
            elif own > 0:
                if own == filled:
                    outcome = "own only"
                else:
                    outcome = "moved" if own + earlier == filled else "partial"
            elif filled == earlier:
                outcome = "moved"
            else:
                outcome = "own only" if filled == 0 else "partial"
            if outcome == "moved":
                moved.update((k, VenueOrderId(live_id)) for k in members)
                partial.discard(live_id)  # shown complete with the chain
                continue
            partial.update(members)
            if outcome == "own only":
                partial.discard(live_id)
            else:
                partial.add(live_id)
            self._log.warning(
                f"Alpaca lists fills of order {', '.join(members)}, replaced by {live_id}, adding up to "
                f"{earlier}, where {live_id} lists {own} of its own and shows {live.filled_qty} filled; "
                + (
                    "it does not count the earlier fills, which are left to the engine to infer"
                    if outcome == "own only"
                    else "the chain's fills are left to the engine to infer"
                ),
            )
        return moved, settled

    async def _live_end(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder | None]
    ) -> AlpacaOrder | None:
        """The order at the end of a replaced order's `replaced_by` links, from the orders already read
        first, or None, with a warning, when it cannot be read or lies past `MAX_REPLACE_CHAIN`."""
        end = order
        try:
            for _ in range(MAX_REPLACE_CHAIN):
                if end.status != AlpacaOrderStatus.REPLACED or end.replaced_by is None:
                    return end
                found = known.get(str(end.replaced_by)) or await self._call(
                    self._venue().get_order_by_id, str(end.replaced_by)
                )
                if not isinstance(found, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                known[str(found.id)] = found  # read once, and judged with the chain's other orders
                end = found
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {order.id} was replaced and the order replacing it could not be read "
                f"({e!r}); its fills are left to the engine to infer"
            )
            return None
        if end.status != AlpacaOrderStatus.REPLACED or end.replaced_by is None:
            return end
        self._log.warning(
            f"Alpaca order {order.id} has more than {MAX_REPLACE_CHAIN} replaces; its fills are left to "
            "the engine to infer"
        )
        return None

    async def _orders_read(self, order_ids: list[str]) -> dict[str, AlpacaOrder | None]:
        """Each order as Alpaca shows it, None where it cannot be read.

        One order costs one `get_order_by_id`. More cost one `get_orders` ("Get All Orders",
        `GetOrdersRequest`) for the account's newest `ORDERS_PAGE_LIMIT` orders of any status, with no
        symbol or time bound (an activity's symbol is not documented to be its order's), and one `get_order_by_id` for each order not among them, one at a time; if
        none is among them, that is one request more than reading each.
        """
        found: dict[str, AlpacaOrder | None] = {}
        wanted = set(order_ids)
        if len(wanted) > 1:
            try:
                orders = await self._call(
                    self._venue().get_orders,
                    GetOrdersRequest(status=QueryOrderStatus.ALL, limit=ORDERS_PAGE_LIMIT),
                )
                if not isinstance(orders, list):
                    raise TypeError(f"Alpaca's orders endpoint answered {type(orders).__name__}")
                for order in orders:
                    if isinstance(order, AlpacaOrder) and str(order.id) in wanted:
                        found[str(order.id)] = order
            except (APIError, OSError, TypeError, ValueError) as e:
                self._log.warning(f"Alpaca orders not read to check fills ({e!r}); reading each")
        for order_id in order_ids:
            if order_id in found:
                continue
            try:
                order = await self._call(self._venue().get_order_by_id, order_id)
            except (APIError, OSError, TypeError, ValueError) as e:
                # alpaca-py builds `Order(**answer)`: an empty or non-object body is a TypeError, a
                # value its model refuses a pydantic `ValidationError`, which is a `ValueError`.
                self._log.warning(f"Alpaca order {order_id} not read to check its fills ({e!r})")
                found[order_id] = None
                continue
            found[order_id] = order if isinstance(order, AlpacaOrder) else None
        return found

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
            instrument_id = InstrumentId(Symbol(position.symbol), VENUE)
            if command.instrument_id is not None and instrument_id != command.instrument_id:
                continue  # before it is read, so another symbol's position cannot fail this report
            if position.asset_class != AssetClass.US_EQUITY:
                self._log.warning(
                    f"Alpaca position in {position.symbol} ({position.asset_class.value}) is not a US "
                    "equity; not reported",
                )
                continue
            if self._provider.find(instrument_id) is None:
                # NautilusTrader fails start-up on a position report for an instrument it has not
                # loaded ("instrument missing from cache", observed); upstream's Coinbase adapter keeps
                # its reports to the instruments it loaded.
                self._log.warning(
                    f"Alpaca position in {position.symbol} is in an instrument this client has not "
                    "loaded; not reported",
                )
                continue
            reports.append(position_status_report(position, ts_init))
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
        """One Alpaca order's report, None for a closed order skipped with a warning naming why.

        A replacement is reported under the node's client order ID, traced through `replaces`; a
        replaced order is skipped (its replacement is reported), so it needs no lookup. A trace that fails,
        on a transport error or on an order alpaca-py cannot build (pydantic's `ValidationError`, a
        `ValueError`, or the `TypeError` of an empty body, the classes `_orders_read` takes),
        skips a closed order, and fails the report for an open one: the node cannot tell whose order a
        working replacement is, and an open order left out of the reports reads as one Alpaca does not
        hold, as any open order this slice cannot represent fails start-up (ADR 0030).
        """
        try:
            root = (
                await self._root_id(order, known)
                if order.status != AlpacaOrderStatus.REPLACED
                else None
            )
        except (APIError, OSError, ValueError, TypeError) as e:
            if is_open_order(order):
                raise ValueError(
                    f"Alpaca order {order.id} ({order.symbol}) is open and tracing it failed ({e!r})"
                ) from e
            self._log.warning(f"Alpaca order {order.id} is not reported: tracing it failed ({e!r})")
            return None

        # Reported under the chain's first client order ID, the node's, held or not: after a restart
        # the node's original order is created under its own ID.
        node_id = root
        held = self.cache.order(node_id) if node_id is not None else None
        status: OrderStatus | None = None
        filled: Decimal | None = None
        quantity: Decimal | None = None
        if order.replaces is not None and order.status != AlpacaOrderStatus.REPLACED:
            if order.status == AlpacaOrderStatus.REJECTED and not await self._took_over(
                order, known
            ):
                # A rejected replacement (Alpaca rejects it when the old order fills first, or a later
                # replace left it behind) did not take over: the old order carries the outcome. Any other
                # replacement is the node's order now, filled or open, whether or not the node has seen
                # `replaced`; so is a rejected one whose old order Alpaca shows replaced by it.
                return None
            own = share_count(order.filled_qty) or Decimal(0)
            try:
                earlier = (
                    await self._earlier_filled(order, known)
                    if FILLED_READING is FilledReading.OWN
                    else Decimal(0)
                )
            except (APIError, OSError, ValueError, TypeError) as e:
                if is_open_order(order):
                    raise ValueError(
                        f"Alpaca order {order.id} ({order.symbol}) is open and its chain's fills could "
                        f"not be read ({e!r})"
                    ) from e
                self._log.warning(
                    f"Alpaca order {order.id} is not reported: its chain's fills ({e!r})"
                )
                return None
            chained = chain_filled(own, earlier)
            if order.status == AlpacaOrderStatus.REJECTED:
                # A rejected order may show nothing filled whatever the reading: the order it replaced,
                # and the node's own order, show the fills it took over.
                prior = (known or {}).get(str(order.replaces))
                record = self._records.get(node_id) if node_id is not None else None
                chained = max(
                    chained,
                    share_count(prior.filled_qty) or Decimal(0)
                    if prior is not None
                    else Decimal(0),
                    held.filled_qty.as_decimal() if held is not None else Decimal(0),
                    record.filled if record is not None else Decimal(0),
                )
            filled = chained if chained != own else None
            qty = share_count(order.qty)
            if qty is not None and chain_quantity(qty, earlier) != qty:
                quantity = chain_quantity(qty, earlier)
            if order.status == AlpacaOrderStatus.REJECTED and chained > 0:
                # A takeover Alpaca rejected after fills ends canceled, as on the stream
                # (`_rejected_takeover`): a rejected report would lose the fills.
                status = OrderStatus.CANCELED
        # As upstream's Coinbase adapter patches a report from what the node sent: the trigger type
        # the node's order holds, which Alpaca's order does not carry.
        trigger_type = (
            held.trigger_type if held is not None and held.order_type in STOP_TYPES else None
        )
        renamed = (
            node_id if node_id is not None and node_id.value != order.client_order_id else None
        )
        return order_status_report(
            order,
            self.clock.timestamp_ns(),
            trigger_type,
            renamed,
            lambda reason: self._log.warning(
                f"Alpaca order {order.id} ({order.symbol}, {order.status.value}) is not reported: "
                f"{reason}",
            ),
            status,
            filled,
            quantity,
        )

    async def _earlier_filled(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder] | None
    ) -> Decimal:
        """The `filled_qty` of every order a replacement's chain replaced, from the orders already read
        first, then at most `MAX_REPLACE_CHAIN` reads; needed only under `FilledReading.OWN`."""
        total = Decimal(0)
        replaces, walked, reads = order.replaces, {str(order.id)}, 0
        while replaces is not None and str(replaces) not in walked:
            prior_id = str(replaces)
            walked.add(prior_id)
            prior = (known or {}).get(prior_id)
            if prior is None:
                if reads == MAX_REPLACE_CHAIN:
                    raise ValueError(
                        f"more than {MAX_REPLACE_CHAIN} reads to sum the chain's fills"
                    )
                reads += 1
                prior = await self._call(self._venue().get_order_by_id, prior_id)
            if not isinstance(prior, AlpacaOrder):
                raise TypeError("alpaca-py returned raw order data")
            total += share_count(prior.filled_qty) or Decimal(0)
            replaces = prior.replaces
        return total

    async def _took_over(
        self, replacement: AlpacaOrder, known: dict[str, AlpacaOrder] | None
    ) -> bool:
        """Whether Alpaca shows the order a replacement replaces as replaced by it, from the orders
        already fetched first (one replaced by a later replacement after this one was rejected does not
        count); an order that cannot be read counts as not replaced, so the old order's report
        stands, as before. The node's own record comes first: a replacement the node has moved its
        order to took over, whatever a read of the order before it gives, and one the record shows the
        order before it replaced by, or by another, needs no read. The stream (`_apply`) and the reports
        both judge a rejected replacement by this, and a read is recorded."""
        replacement_id = str(replacement.id)
        node_id = self._index.get(replacement_id)
        if node_id is not None:
            record = self._records.get(node_id)
            held = self.cache.order(node_id)
            if (record is not None and record.current == replacement_id) or (
                held is not None
                and held.venue_order_id is not None
                and held.venue_order_id.value == replacement_id
            ):
                return True
        record = self._records.get(node_id) if node_id is not None else None
        known_by_record = record.took_over(replacement_id) if record is not None else None
        if known_by_record is not None:
            return known_by_record
        prior = (known or {}).get(str(replacement.replaces))
        if prior is None:
            try:
                prior = await self._call(self._venue().get_order_by_id, str(replacement.replaces))
            except (APIError, OSError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Alpaca order {replacement.replaces}, which the rejected {replacement.id} replaces, "
                    f"could not be read ({e!r}); the replacement is taken not to have taken over"
                )
                return False
        if isinstance(prior, AlpacaOrder) and record is not None:
            self._see(record, prior)
        return (
            isinstance(prior, AlpacaOrder)
            and prior.status == AlpacaOrderStatus.REPLACED
            and str(prior.replaced_by) == replacement_id
        )

    # -- alpaca-py calls ---------------------------------------------------------------------------

    def _venue(self) -> OrdersClient:
        if self._trading is None:
            raise RuntimeError("Alpaca execution client is not connected")
        return self._trading

    async def _call[R](self, fn: Callable[..., R], *args: object, once: bool = False) -> R:
        """Call a `TradingClient` method in a worker thread, one call at a time; with `once`, an order
        command, its request is sent once (`without_resend`; the client was prepared at connect).

        As `data.AlpacaDataClient._one_historical_call`: the lock is held on the node's loop and released
        by the worker when the call returns, and the call is shielded, so a cancelled hook leaves it
        running and the lock held until it ends.
        """
        loop = asyncio.get_running_loop()
        worker = counted(fn)
        if once:
            worker = without_resend(worker)
        lock = self._rest_lock
        await lock.acquire()
        try:
            call = asyncio.ensure_future(
                asyncio.to_thread(release_after, loop, lock, worker, *args)
            )
        except BaseException:
            lock.release()
            raise
        call.add_done_callback(retrieved)  # read if the hook awaiting it was cancelled meanwhile
        return cast("R", await asyncio.shield(call))
