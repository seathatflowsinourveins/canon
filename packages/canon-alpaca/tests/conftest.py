"""Offline fixtures: Alpaca payloads and stand-ins for the three alpaca-py clients the adapter uses.

Every payload is hand-built from an example in Alpaca's docs, read 2026-11-09, and parsed with the pinned
alpaca-py's own models, so the adapter's conversions are tested against what alpaca-py hands it:

* asset: "Get Assets", schema example (https://docs.alpaca.markets/us/reference/get-v2-assets-1.md)
* bar: "Historical bars", response example (https://docs.alpaca.markets/us/reference/stockbars.md)
* trade and quote: "Real-time Stock Data", schema examples
  (https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md). The stream sends `t` as a msgpack
  timestamp, and alpaca-py's `DataStream._cast` converts it with `Timestamp.to_datetime()`, a UTC
  `datetime` with microsecond precision, before building the model. The fixtures write that `datetime`
  directly, so the tests do not import msgpack (not in `canon/upstreams.json`).
* trade cancel and trade correction: the same page, schema examples, read 2026-11-10. The symbol is
  changed to AAPL and the timestamp written as `to_datetime()` gives it.
* order: "Create an Order", response example `EquityOrderResponse`
  (https://docs.alpaca.markets/us/reference/postorder.md), read 2026-11-10
* trade update: "Websocket Streaming", the `trade_updates` example
  (https://docs.alpaca.markets/us/docs/websocket-streaming.md), read 2026-11-10. Its order is a crypto
  sell; here the update's fields (`execution_id`, `price`, `timestamp`) are kept and the order is the
  equity order above, moved through the statuses the event implies.
* fill activity: "Retrieve Account Activities of Specific Type", the `TradingActivities` example
  (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md), read 2026-11-10; its
  `id` written as paper writes it (ADR 0024)
* account: "Get Account", response example (https://docs.alpaca.markets/us/reference/getaccount-1.md)
* position: "All Open Positions", the second schema example, AAPL
  (https://docs.alpaca.markets/us/reference/getallopenpositions.md)

The stand-ins implement only the methods the adapter calls, with alpaca-py 0.45.0's signatures, and are as
strict as alpaca-py where the adapter depends on it: the bars stand-in filters the way Alpaca's bars
endpoint documents, the stream stand-in checks handlers and unsubscribes the way
`alpaca/data/live/websocket.py` does, the orders stand-in answers with the statuses "Create an Order" and
"Delete Order by ID" document, and the trade stream stand-in casts its messages the way
`alpaca/trading/stream.py` does. No test here opens a network connection or reads a credential.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import queue
import threading
import time as time_module
import traceback
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import Trade
from alpaca.data.models import TradeCancel
from alpaca.data.models import TradeCorrection
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.models import Asset
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import OrderRequest
from canon_alpaca.providers import timeout_adapter

type Handler = Callable[[object], Awaitable[None]]
type Message = Quote | Trade | TradeCancel | TradeCorrection

NEW_YORK = ZoneInfo("America/New_York")

ASSET_AAPL = {
    "borrow_status": "easy_to_borrow",
    "class": "us_equity",
    "easy_to_borrow": True,
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "name": "Apple Inc. Common Stock",
    "shortable": True,
    "status": "active",
    "symbol": "AAPL",
    "tradable": True,
}

BAR_AAPL = {
    "c": 178.21,
    "h": 178.26,
    "l": 178.21,
    "n": 65,
    "o": 178.26,
    "t": "2022-01-03T09:00:00Z",
    "v": 1118,
    "vw": 178.235733,
}

TRADE_AAPL = {
    "T": "t",
    "i": 96921,
    "S": "AAPL",
    "x": "D",
    "p": 126.55,
    "s": 1,
    "t": datetime(2021, 2, 22, 15, 51, 44, 208_000, tzinfo=UTC),  # "2021-02-22T15:51:44.208Z"
    "c": ["@", "I"],
    "z": "C",
}

QUOTE_AAPL = {
    "T": "q",
    "S": "AAPL",  # the docs' example is AMD; the symbol is changed so one instrument serves all tests
    "bx": "U",
    "bp": 87.66,
    "bs": 1,
    "ax": "Q",
    "ap": 87.68,
    "as": 4,
    # The docs' "2021-02-22T15:51:45.335689322Z", as to_datetime() gives it: the last 322 ns are dropped.
    "t": datetime(2021, 2, 22, 15, 51, 45, 335_689, tzinfo=UTC),
    "c": ["R"],
    "z": "C",
}


TRADE_CANCEL_AAPL = {
    "T": "x",
    "S": "AAPL",  # the docs' example is GOOGL
    "i": 465,
    "x": "D",
    "p": 105.31,
    "s": 300,
    "a": "C",
    "z": "C",
    "t": datetime(2023, 4, 6, 13, 15, 42, 835_409, tzinfo=UTC),  # "2023-04-06T13:15:42.83540958Z"
}

TRADE_CORRECTION_AAPL = {
    "T": "c",
    "S": "AAPL",  # the docs' example is EEM
    "x": "M",
    "oi": 52983525033527,
    "op": 39.1582,
    "os": 440000,
    "oc": [" ", "7"],
    "ci": 52983525034326,
    "cp": 39.1809,
    "cs": 440000,
    "cc": [" ", "7"],
    "z": "B",
    "t": datetime(2023, 4, 6, 14, 25, 6, 542_305, tzinfo=UTC),  # "2023-04-06T14:25:06.542305024Z"
}


ORDER_AAPL = {
    "asset_class": "us_equity",
    "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "canceled_at": None,
    "client_order_id": "5680c4bc-9ac1-4a12-a44c-df427ba53032",
    "created_at": "2023-12-12T22:31:24.668464435Z",
    "expired_at": None,
    "extended_hours": False,
    "failed_at": None,
    "filled_at": None,
    "filled_avg_price": None,
    "filled_qty": "0",
    "hwm": None,
    "id": "7b08df51-c1ac-453c-99f9-323a5f075f0d",
    "legs": None,
    "limit_price": "150",
    "notional": None,
    "order_class": "",
    "order_type": "limit",
    "qty": "2",
    "replaced_at": None,
    "replaced_by": None,
    "replaces": None,
    "side": "buy",
    "source": None,
    "status": "accepted",
    "stop_price": None,
    "submitted_at": "2023-12-12T22:31:24.577215743Z",
    "subtag": None,
    "symbol": "AAPL",
    "time_in_force": "gtc",
    "trail_percent": None,
    "trail_price": None,
    "type": "limit",
    "updated_at": "2023-12-12T22:31:24.668464435Z",
}

FILL_UPDATE = {
    "event": "fill",
    "execution_id": "2f63ea93-423d-4169-b3f6-3fdafc10c418",
    "position_qty": "0",
    "price": "105.8988475",
    "qty": "1790.86",
    "timestamp": "2022-04-19T17:45:05.024916716Z",
}

ACCOUNT = {
    "account_blocked": False,
    "account_number": "PALPACA_123",
    "accrued_fees": "0",
    "admin_configurations": {},
    "balance_asof": "2023-09-27",
    "buying_power": "245432.61",
    "cash": "122086.5",
    "created_at": "2023-01-01T18:20:20.272275Z",
    "crypto_status": "ACTIVE",
    "crypto_tier": 1,
    "currency": "USD",
    "effective_buying_power": "245432.61",
    "equity": "123346.11",
    "id": "1d9eed04-be39-4e01-9b84-a48ac5bbafcf",
    "initial_margin": "629.8",
    "intraday_adjustments": "0",
    "last_equity": "122011.09751111286868",
    "last_maintenance_margin": "480.73",
    "long_market_value": "1259.61",
    "maintenance_margin": "377.88",
    "multiplier": "2",
    "non_marginable_buying_power": "122086.5",
    "options_buying_power": "122716.305",
    "options_trading_level": 2,
    "pending_reg_taf_fees": "0",
    "pending_transfer_in": "0",
    "portfolio_value": "123346.11",
    "position_market_value": "1259.61",
    "regt_buying_power": "245432.61",
    "short_market_value": "0",
    "shorting_enabled": True,
    "sma": "123369.74",
    "status": "ACTIVE",
    "trade_suspended_by_user": False,
    "trading_blocked": False,
    "transfers_blocked": False,
    "user_configurations": None,
}

POSITION_AAPL = {
    "asset_class": "us_equity",
    "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "asset_marginable": False,
    "avg_entry_price": "174.78",
    "change_today": "-0.0018326556325525",
    "cost_basis": "349.56",
    "current_price": "174.29",
    "exchange": "NASDAQ",
    "lastday_price": "174.61",
    "market_value": "348.58",
    "qty": "2",
    "qty_available": "2",
    "side": "long",
    "symbol": "AAPL",
    "unrealized_intraday_pl": "-0.98",
    "unrealized_intraday_plpc": "-0.0028035244307129",
    "unrealized_pl": "-0.98",
    "unrealized_plpc": "-0.0028035244307129",
}

CLOSED_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


FILL_ACTIVITY = {
    "activity_type": "FILL",
    "cum_qty": "2",
    "id": "20220202135509981::2d7be4ff-d1f3-43e9-856a-0f5cf5c5088e",
    "leaves_qty": "0",
    "order_id": "b5abe576-6a8a-49f3-a353-46b72c1ccae9",
    "order_status": "filled",
    "price": "174.78",
    "qty": "2",
    "side": "buy",
    "symbol": "AAPL",
    "transaction_time": "2022-02-02T18:55:09.981482Z",
    "type": "fill",
}
"""The `TradingActivities` example of "Retrieve Account Activities of Specific Type"
(https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md), read 2026-11-10."""


def fill_activity(
    order: dict,
    execution_id: str,
    qty: int,
    price: str,
    at: datetime,
    kind: str = "fill",
    cum_qty: int | None = None,
    leaves_qty: int = 0,
) -> dict:
    """The docs' fill activity for one fill of `order`, its `id` as paper writes it (ADR 0024); `cum_qty`
    is `qty` unless given."""
    stamp = at.astimezone(NEW_YORK).strftime("%Y%m%d%H%M%S%f")[:17]
    return {
        **FILL_ACTIVITY,
        "id": f"{stamp}::{execution_id}",
        "order_id": order["id"],
        "price": price,
        "qty": str(qty),
        "side": order["side"],
        "symbol": order["symbol"],
        "transaction_time": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "type": kind,
        "cum_qty": str(qty if cum_qty is None else cum_qty),
        "leaves_qty": str(leaves_qty),
    }


def alpaca_order(**changes: object) -> dict:
    """A copy of the docs' equity order with `changes` applied."""
    return {**ORDER_AAPL, **changes}


def api_error(status: int, code: int, message: str) -> APIError:
    """An `APIError` as alpaca-py raises it: Alpaca's JSON body, and the HTTP error carrying the status."""
    response = SimpleNamespace(status_code=status)
    return APIError(
        json.dumps({"code": code, "message": message}), SimpleNamespace(response=response)
    )


def asset(**changes: object) -> Asset:
    return Asset.model_validate({**ASSET_AAPL, **changes})


def quote(**changes: object) -> Quote:
    return Quote(QUOTE_AAPL["S"], {**QUOTE_AAPL, **changes})


def trade(**changes: object) -> Trade:
    return Trade(TRADE_AAPL["S"], {**TRADE_AAPL, **changes})


def trade_cancel() -> TradeCancel:
    return TradeCancel("AAPL", dict(TRADE_CANCEL_AAPL))


def trade_correction() -> TradeCorrection:
    return TradeCorrection("AAPL", dict(TRADE_CORRECTION_AAPL))


def bars_opening(*opens: datetime) -> list[dict]:
    """Copies of the docs' bar, one opening at each UTC time given."""
    return [{**BAR_AAPL, "t": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")} for t in opens]


def hourly_bars(first_open: datetime, count: int) -> list[dict]:
    """`count` copies of the docs' bar, opening one hour apart from `first_open`."""
    return bars_opening(*(first_open + timedelta(hours=i) for i in range(count)))


class FakeTradingClient:
    """Stands in for `TradingClient`'s two asset reads.

    Like alpaca-py 0.45.0's `RESTClient`, it holds a `requests.Session` at `_session`, where the adapter
    mounts its timeout adapter (ADR 0039). `_count_sends` counts one request sent through that adapter, or
    `sends_per_call` of them, as when alpaca-py repeats a request answered 429 or 504 (`_count_sends`).
    """

    def __init__(self, assets: list[dict] | None = None) -> None:
        self._session = requests.Session()
        self.sends_per_call = 1
        self.assets = assets if assets is not None else [ASSET_AAPL]
        self.errors: dict[str, Exception] = {}  # raised by get_asset for that symbol
        self.all_requests: list[object] = []
        self.symbol_requests: list[object] = []

    def _count_sends(self) -> None:
        adapter = timeout_adapter(self)
        if adapter is not None:
            for _ in range(self.sends_per_call):
                adapter.record_send()

    def get_all_assets(self, filter: object = None) -> list[Asset]:
        self.all_requests.append(filter)
        return [Asset.model_validate(a) for a in self.assets]

    def get_asset(self, symbol_or_asset_id: object) -> Asset:
        self.symbol_requests.append(symbol_or_asset_id)
        if isinstance(symbol_or_asset_id, str) and symbol_or_asset_id in self.errors:
            raise self.errors[symbol_or_asset_id]
        for a in self.assets:
            if a["symbol"] == symbol_or_asset_id:
                return Asset.model_validate(a)
        raise APIError('{"code": 40410000, "message": "asset not found"}')


class FakeOrdersClient(FakeTradingClient):
    """Stands in for `TradingClient`: the asset reads, and the order, position and account calls.

    `submit_order` answers as "Create an Order" documents: the new order, status `accepted`, or an
    `APIError` with status 403 or 422. It then sends the order's trade updates to `stream`, as set by
    `script`: "accept" (`new`), "fill" (`new`, then one `fill` for the whole quantity), "partial" (`new`,
    a `partial_fill` of 1, then a `fill` for the rest), "reject" (`new`, then `rejected`), or "silent"
    (nothing).
    `submit_error` is raised instead of answering; with `took_before_error`, the order is taken (and its
    updates sent) first, as when alpaca-py's retry of a submit Alpaca had already taken is refused.
    `submit_delay` holds the answer back that many seconds after the updates are sent.
    `cancel_order_by_id` answers 204 and sends `canceled`, or raises `cancel_error`; with
    `took_before_error`, it first marks the order `pending_cancel`, sending no update; `refuse_cancel` refuses
    the cancel of each order it returns an error for, and the others are canceled.
    It cancels orders placed through it and orders in `orders`, as Alpaca holds both. With `execution_ids` false, fills carry no `execution_id`. `orders` and `positions` are what the reads return; `get_orders` applies
    the `status`, `symbols`, `after` and `until` filters the way alpaca-py's `GetOrdersRequest` documents
    them (`after` and `until` on the submission time).
    `get` answers `/account/activities/FILL` as "Retrieve Account Activities of Specific Type" documents
    it: `activities`, filtered by `order_id`, ordered by `direction` (newest first by default), at most
    `page_size` (100 by default), after the activity whose `id` is `page_token`. Each fill this stand-in
    sends with an `execution_id` is added there, its `id` that fill's New York time to the millisecond,
    `::`, and the `execution_id`, as paper showed (ADR 0024). `activity_requests` records every call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stream: FakeTradeStream | None = None
        self.script = "fill"
        self.fill_price = FILL_UPDATE["price"]
        self.submit_error: Exception | None = None
        self.cancel_error: Exception | None = None
        # Given Alpaca's order, the error its cancel is refused with, or None to cancel it.
        self.refuse_cancel: Callable[[dict], Exception | None] | None = None
        self.took_before_error = False
        self.execution_ids = True  # whether fill updates carry `execution_id`
        self.submit_delay = 0.0
        self.submitted: list[OrderRequest] = []
        self.replace_script = "replaced"
        self.replace_delay = 0.0  # holds the replace answer back after `replaced` is sent
        self.replace_error: Exception | None = None
        self.lookup_error: Exception | None = None  # raised by `get_order_by_client_id`
        # Given an order ID, the error `get_order_by_id` raises for it, or None to answer.
        self.order_lookup_error: Callable[[str], Exception | None] | None = None
        self.replace_requests: list[tuple[str, object]] = []
        self.cancels: list[str] = []
        self.order_queries: list[GetOrdersRequest | None] = []
        self.orders: list[dict] = []
        self.positions: list[dict] = []
        self.activities: list[dict] = []
        self.activity_requests: list[dict] = []
        self.activities_error: Exception | None = None  # raised by `get`
        self.ignores_order_filter = False  # answer every order's fills, whatever `order_id` asks
        self.account = dict(ACCOUNT)
        self._placed: dict[str, dict] = {}

    def submit_order(self, order_data: OrderRequest) -> Order:
        self._count_sends()
        self.submitted.append(order_data)
        if self.submit_error is not None and not self.took_before_error:
            raise self.submit_error
        now = datetime.now(UTC).isoformat()
        placed = alpaca_order(
            id=str(uuid.uuid4()),
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            qty=str(int(order_data.qty or 0)),
            side=order_data.side.value if order_data.side else None,
            type=order_data.type.value,
            order_type=order_data.type.value,
            time_in_force=order_data.time_in_force.value,
            limit_price=str(getattr(order_data, "limit_price", None) or "") or None,
            stop_price=str(getattr(order_data, "stop_price", None) or "") or None,
            created_at=now,
            submitted_at=now,
            updated_at=now,
        )
        self._placed[placed["id"]] = placed
        answer = Order(**placed)  # the REST answer is the order as submitted, before any update
        qty = int(placed["qty"])
        if self.script in ("accept", "fill", "partial"):
            self._send("new", placed, status="new")
        if self.script == "fill":
            self._send("fill", placed, qty, status="filled", filled_qty=str(qty))
        elif self.script == "partial":
            self._send("partial_fill", placed, 1, status="partially_filled", filled_qty="1")
            self._send("fill", placed, qty - 1, status="filled", filled_qty=str(qty))
        elif self.script == "reject":
            self._send("new", placed, status="new")
            self._send("rejected", placed, status="rejected")
        if self.submit_error is not None:
            raise self.submit_error
        time_module.sleep(self.submit_delay)
        return answer

    def _send(self, event: str, placed: dict, qty: int | None = None, **status: str) -> None:
        placed.update(status, updated_at=datetime.now(UTC).isoformat())
        update: dict[str, object] = {
            "event": event,
            "order": dict(placed),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if qty is not None:
            update.update(price=self.fill_price, qty=str(qty), position_qty=str(qty))
            if self.execution_ids:
                update["execution_id"] = str(uuid.uuid4())
                self.activities.append(
                    fill_activity(
                        placed,
                        str(update["execution_id"]),
                        qty,
                        str(self.fill_price),
                        datetime.fromisoformat(str(update["timestamp"])),
                        event,
                        cum_qty=int(placed.get("filled_qty") or qty),
                        leaves_qty=int(placed["qty"]) - int(placed.get("filled_qty") or qty),
                    )
                )
            placed["filled_avg_price"] = self.fill_price
        assert self.stream is not None, "no trade stream to send to"
        self.stream.send(update)

    def cancel_order_by_id(self, order_id: object) -> None:
        self._count_sends()
        self.cancels.append(str(order_id))
        if self.cancel_error is not None and not self.took_before_error:
            raise self.cancel_error
        placed = self._placed.get(str(order_id)) or next(
            (raw for raw in self.orders if raw["id"] == str(order_id)), None
        )
        if placed is None:
            raise api_error(404, 40410000, "order not found")
        refusal = self.refuse_cancel(placed) if self.refuse_cancel is not None else None
        if refusal is not None:
            raise refusal
        if self.cancel_error is not None:
            # Taken, and refused on the repeat: Alpaca's order is pending cancel; no update is sent.
            placed.update(status="pending_cancel")
            raise self.cancel_error
        self._send("canceled", placed, status="canceled", canceled_at=datetime.now(UTC).isoformat())

    def replace_order_by_id(self, order_id: object, order_data: object = None) -> Order:
        """Answer as the paper observation of 2026-11-10 saw (ADR 0042): the replacement, status `new`,
        with an Alpaca-made client order ID and `replaces`; then, by `replace_script`, the stream's
        `replaced` carrying the old order with `replaced_by` ("replaced"), `order_replace_rejected` for the
        old order ("reject"), or nothing ("silent", for `confirm_replace` to send later). A second replace
        of a replaced order answers 422 "order already replaced", and one keeping a client order ID 422
        "client_order_id must be unique", as observed. `replace_error` is raised instead of answering;
        with `took_before_error`, the replace is taken first, as for a submit."""
        self._count_sends()
        self.replace_requests.append((str(order_id), order_data))
        if self.replace_error is not None and not self.took_before_error:
            raise self.replace_error
        old = self._placed.get(str(order_id)) or next(
            (raw for raw in self.orders if raw["id"] == str(order_id)), None
        )
        if old is None:
            raise api_error(404, 40410000, "order not found")
        if old["status"] == "replaced":
            raise api_error(422, 42210000, "order already replaced")
        if getattr(order_data, "client_order_id", None) is not None:
            raise api_error(422, 40010001, "client_order_id must be unique")
        now = datetime.now(UTC).isoformat()
        new = {
            **old,
            "id": str(uuid.uuid4()),
            "client_order_id": str(uuid.uuid4()),
            "replaces": old["id"],
            "replaced_by": None,
            "status": "new",
            "created_at": now,
            "submitted_at": now,
            "updated_at": now,
        }
        for field in ("qty", "limit_price", "stop_price"):
            value = getattr(order_data, field, None)
            if value is not None:
                new[field] = str(value)
        if self.replace_script == "reject":
            self._send("order_replace_rejected", old)
            raise api_error(422, 42210000, "replace rejected (stand-in)")
        self._placed[new["id"]] = new
        old.update(replaced_by=new["id"], replaced_at=now)
        answer = Order(**new)
        if self.replace_script == "replaced":
            self._send("replaced", old, status="replaced")
        else:
            old.update(status="replaced")
        if self.replace_error is not None:
            raise self.replace_error
        time_module.sleep(self.replace_delay)
        return answer

    def confirm_replace(self, old_id: str) -> None:
        """Send the `replaced` update for a replace answered under the "silent" script."""
        old = self._placed[old_id]
        self._send("replaced", old, status="replaced")

    def update(self, order_id: str, event: str, qty: int | None = None, **status: str) -> None:
        """Send one trade update for an order the stand-in holds, as Alpaca would."""
        self._send(event, self._placed[order_id], qty, **status)

    def get_order_by_id(self, order_id: object) -> Order:
        refusal = self.order_lookup_error(str(order_id)) if self.order_lookup_error else None
        if refusal is not None:
            raise refusal
        for raw in [*self._placed.values(), *self.orders]:
            if raw["id"] == str(order_id):
                return Order(**raw)
        raise api_error(404, 40410000, "order not found")

    def get_order_by_client_id(self, client_id: str) -> Order:
        self._count_sends()
        if self.lookup_error is not None:
            raise self.lookup_error
        for raw in [*self._placed.values(), *self.orders]:
            if raw["client_order_id"] == client_id:
                return Order(**raw)
        raise api_error(404, 40410000, "order not found")

    def get_orders(self, filter: GetOrdersRequest | None = None) -> list[Order]:
        self.order_queries.append(filter)
        status = filter.status.value if filter is not None and filter.status else "open"
        symbols = filter.symbols if filter is not None else None
        selected = []
        for raw in self.orders:
            closed = raw["status"] in CLOSED_ORDER_STATUSES
            if (status == "open" and closed) or (status == "closed" and not closed):
                continue
            if symbols and raw["symbol"] not in symbols:
                continue
            order = Order(**raw)
            # "after"/"until": only orders "submitted after" / "submitted until" (GetOrdersRequest).
            if (
                filter is not None
                and filter.after is not None
                and order.submitted_at <= filter.after
            ):
                continue
            if (
                filter is not None
                and filter.until is not None
                and order.submitted_at > filter.until
            ):
                continue
            selected.append(order)
        # "Defaults to desc", on the submission time; "limit": "Defaults to 50 and max is 500"
        # ("Get All Orders", https://docs.alpaca.markets/us/reference/getallorders-1.md).
        selected.sort(key=lambda order: order.submitted_at, reverse=True)
        limit = filter.limit if filter is not None and filter.limit is not None else 50
        return selected[:limit]

    def get_all_positions(self) -> list[Position]:
        return [Position(**p) for p in self.positions]

    def get_account(self) -> TradeAccount:
        return TradeAccount.model_validate(self.account)

    def get(self, path: str, data: dict | None = None) -> list[dict]:
        if self.activities_error is not None:
            raise self.activities_error
        assert path == "/account/activities/FILL", (
            f"the stand-in answers only fill activities: {path}"
        )
        params = dict(data or {})
        self.activity_requests.append(params)
        selected = [
            a
            for a in self.activities
            if self.ignores_order_filter
            or "order_id" not in params
            or a["order_id"] == params["order_id"]
        ]
        selected.sort(
            key=lambda a: datetime.fromisoformat(a["transaction_time"]),
            reverse=params.get("direction", "desc") == "desc",
        )
        if "page_token" in params:
            ids = [a["id"] for a in selected]
            selected = selected[ids.index(params["page_token"]) + 1 :]
        return [dict(a) for a in selected[: int(params.get("page_size", 100))]]


class FakeTradeStream:
    """Stands in for `TradingStream`: `run()` blocks on its own asyncio loop until `stop()`.

    As in alpaca-py 0.45.0: `subscribe_trade_updates` refuses a handler that is not a coroutine function
    and, before `run()`, only records it; each message sent is cast with `TradeUpdate(**data)` and awaited
    on the stream's loop. `send` may be called from any thread; messages sent before `run()` wait for it.
    With `ignores_stop`, `run()` keeps going after `stop()` until `release` is set.
    """

    def __init__(self) -> None:
        self.handler: Handler | None = None
        self.ignores_stop = False
        self.release = threading.Event()
        self.run_threads: list[str] = []
        self.running = threading.Event()
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self._messages: queue.Queue[dict] = queue.Queue()

    def subscribe_trade_updates(self, handler: Handler) -> None:
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("handler must be a coroutine function")
        assert not self.running.is_set(), (
            "alpaca-py blocks a subscribe on the running stream's loop"
        )
        self.handler = handler

    def send(self, data: dict) -> None:
        self._messages.put(data)

    def run(self) -> None:
        self.run_threads.append(threading.current_thread().name)
        self.running.set()
        try:
            asyncio.run(self._serve())
        finally:
            self.running.clear()
            self.finished.set()

    def stop(self) -> None:
        self.stopped.set()

    async def _serve(self) -> None:
        while not self.stopped.is_set() or (self.ignores_stop and not self.release.is_set()):
            try:
                data = await asyncio.to_thread(self._messages.get, True, 0.05)
            except queue.Empty:
                continue
            if self.handler is not None:
                await self.handler(TradeUpdate(**data))


class FakeHistoricalClient:
    """Stands in for `StockHistoricalDataClient.get_stock_bars`.

    It answers the way Alpaca's bars endpoint documents (https://docs.alpaca.markets/us/reference/stockbars.md):
    the bars whose timestamp, the open, lies in the inclusive `[start, end]`, in ascending time, at most
    `limit` of them. alpaca-py has already made `start` and `end` naive UTC. With `gate` set, each call
    sets `entered` and then waits for `gate`.
    """

    def __init__(self, bars: list[dict] | None = None, error: Exception | None = None) -> None:
        self._session = requests.Session()  # where alpaca-py's RESTClient keeps it (ADR 0039)
        self.bars = bars if bars is not None else [BAR_AAPL]
        self.error = error
        self.requests: list[StockBarsRequest] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.max_active = 0  # the most calls that were inside `get_stock_bars` at once
        self._active = 0
        self._count_lock = threading.Lock()

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet:
        with self._count_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            return self._answer(request_params)
        finally:
            with self._count_lock:
                self._active -= 1

    def _answer(self, request_params: StockBarsRequest) -> BarSet:
        self.requests.append(request_params)
        if self.gate is not None:
            self.entered.set()
            assert self.gate.wait(10), "gate never opened"
        if self.error is not None:
            raise self.error
        symbol = request_params.symbol_or_symbols
        assert isinstance(symbol, str)
        start, end, limit = request_params.start, request_params.end, request_params.limit
        assert start is None or start.tzinfo is None
        assert end is None or end.tzinfo is None
        # Alpaca's documented defaults: "the beginning of the current day" and "the current time" (for
        # a feed with real-time access). The reference names no time zone; midnight New York is assumed.
        now = datetime.now(UTC)
        if start is None:
            day = now.astimezone(NEW_YORK).date()
            start = datetime.combine(day, time(0), NEW_YORK).astimezone(UTC).replace(tzinfo=None)
        if end is None:
            end = now.replace(tzinfo=None)
        if start > end:
            # The reference does not say what Alpaca answers; refusing makes an inverted window visible.
            raise APIError('{"code": 42210000, "message": "start is after end (stand-in)"}')
        selected = []
        for raw in sorted(self.bars, key=lambda b: b["t"]):
            opened = datetime.fromisoformat(raw["t"]).replace(tzinfo=None)
            if start <= opened <= end:
                selected.append(raw)
        return BarSet({symbol: selected[:limit] if limit else selected})


class FakeStream:
    """Stands in for `StockDataStream`: `run()` blocks on its own asyncio loop until `stop()`.

    While running, it sends each message in `quotes` to the handler subscribed for that message's symbol,
    and each in `trades` (then, after them, those in `trade_events`, cancels and corrections) to the trade
    handler, once per subscription, the way alpaca-py's stream awaits a coroutine handler per message.
    Cancels and corrections go to the handlers set with `register_trade_cancels` and
    `register_trade_corrections`, which alpaca-py keys under "*". With `ends_on_its_own`, `run()` returns
    after its first delivery without a `stop()`, as alpaca-py's does after an insufficient-subscription
    error. With `ignores_stop`, `run()` keeps going after `stop()` until `release` is set.

    As in alpaca-py 0.45.0 (`DataStream._subscribe`, `_unsubscribe`, `_ensure_coroutine`): a handler that is
    not a coroutine function raises `ValueError`; a subscribe records its handlers and then sends; an
    unsubscribe sends first and deletes its handlers only if the send succeeded, and deleting a symbol never
    subscribed raises `KeyError`. Sends are made only while running. With `send_error` set, every send
    raises it.
    """

    def __init__(self) -> None:
        self.ends_on_its_own = False
        self.ignores_stop = False
        self.release = threading.Event()
        self.send_error: Exception | None = None
        self.quotes: list[Callable[[], Message]] = [quote]
        self.trades: list[Callable[[], Message]] = [trade]
        self.trade_events: list[Callable[[], Message]] = []
        self.quote_handlers: dict[str, Handler] = {}
        self.trade_handlers: dict[str, Handler] = {}
        self.cancel_handlers: dict[str, Handler] = {}
        self.correction_handlers: dict[str, Handler] = {}
        self.unsubscribed: list[tuple[str, tuple[str, ...]]] = []
        self.run_threads: list[str] = []
        self.running = threading.Event()
        self.finished = threading.Event()
        self.stopped = threading.Event()
        self._changed = threading.Event()

    def _send(self) -> None:
        if self.running.is_set() and self.send_error is not None:
            raise self.send_error

    def _subscribe(self, handler: Handler, symbols: tuple[str, ...], handlers: dict) -> None:
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("handler must be a coroutine function")
        handlers.update(dict.fromkeys(symbols, handler))
        self._changed.set()
        self._send()

    def _unsubscribe(self, channel: str, symbols: tuple[str, ...], handlers: dict) -> None:
        self._send()
        self.unsubscribed.append((channel, symbols))
        for s in symbols:
            del handlers[s]

    def subscribe_quotes(self, handler: Handler, *symbols: str) -> None:
        self._subscribe(handler, symbols, self.quote_handlers)

    def subscribe_trades(self, handler: Handler, *symbols: str) -> None:
        self._subscribe(handler, symbols, self.trade_handlers)

    def register_trade_cancels(self, handler: Handler) -> None:
        self.cancel_handlers = {"*": handler}

    def register_trade_corrections(self, handler: Handler) -> None:
        self.correction_handlers = {"*": handler}

    def unsubscribe_quotes(self, *symbols: str) -> None:
        self._unsubscribe("quotes", symbols, self.quote_handlers)

    def unsubscribe_trades(self, *symbols: str) -> None:
        self._unsubscribe("trades", symbols, self.trade_handlers)

    def run(self) -> None:
        self.run_threads.append(threading.current_thread().name)
        self.running.set()
        try:
            asyncio.run(self._serve())
        finally:
            self.running.clear()
            self.finished.set()

    def stop(self) -> None:
        self.stopped.set()
        self._changed.set()

    def _handler_for(self, message: Message) -> Handler | None:
        channel = {
            TradeCancel: self.cancel_handlers,
            TradeCorrection: self.correction_handlers,
            Trade: self.trade_handlers,
            Quote: self.quote_handlers,
        }[type(message)]
        symbol = message.symbol
        return channel.get(symbol, channel.get("*"))

    async def _serve(self) -> None:
        sent: set[tuple[str, str]] = set()
        while not self.stopped.is_set() or (self.ignores_stop and not self.release.is_set()):
            for channel, handlers, builders in (
                ("quotes", self.quote_handlers, self.quotes),
                ("trades", self.trade_handlers, self.trades + self.trade_events),
            ):
                for symbol in list(handlers):
                    if (channel, symbol) in sent:
                        continue
                    sent.add((channel, symbol))
                    for build in builders:
                        message = build()
                        handler = self._handler_for(message)
                        if message.symbol == symbol and handler is not None:
                            await handler(message)
            if self.ends_on_its_own and sent:
                return
            self._changed.clear()
            await asyncio.to_thread(self._changed.wait, 0.05)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Once a failure's report is written, drop the local variables its traceback keeps.

    NautilusTrader allows one `LiveNode` per thread, and a failed test's traceback would keep its node
    alive, so every later test would fail to build one.
    """
    report = yield
    if call.excinfo is not None:
        traceback.clear_frames(call.excinfo.tb)
    return report


@pytest.fixture(autouse=True)
def collect_on_main_thread():
    """Collect each test's garbage on the main thread before the next test starts threads.

    A client and its NautilusTrader `ClientRuntime` refer to each other, so they are freed by Python's
    cyclic collector, which runs on whichever thread happens to allocate. Their native `Logger` and
    `Clock` raise if dropped off the thread that made them, so a later test's worker or stream thread
    must not be the one that collects them. What that raise produces is an "unraisable exception"
    warning, log noise rather than a crash; collecting here keeps it out of the test output.
    """
    yield
    gc.collect()


@pytest.fixture
def alpaca() -> SimpleNamespace:
    """The payload builders and stand-ins, for tests to construct as they need."""
    return SimpleNamespace(
        ASSET_AAPL=ASSET_AAPL,
        BAR_AAPL=BAR_AAPL,
        asset=asset,
        quote=quote,
        trade=trade,
        trade_cancel=trade_cancel,
        trade_correction=trade_correction,
        hourly_bars=hourly_bars,
        bars_opening=bars_opening,
        ORDER_AAPL=ORDER_AAPL,
        POSITION_AAPL=POSITION_AAPL,
        FILL_UPDATE=FILL_UPDATE,
        FILL_ACTIVITY=FILL_ACTIVITY,
        fill_activity=fill_activity,
        alpaca_order=alpaca_order,
        api_error=api_error,
        TradingClient=FakeTradingClient,
        OrdersClient=FakeOrdersClient,
        TradeStream=FakeTradeStream,
        HistoricalClient=FakeHistoricalClient,
        Stream=FakeStream,
    )
