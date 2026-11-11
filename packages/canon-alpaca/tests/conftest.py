"""Offline fixtures: Alpaca payloads and stand-ins for the three alpaca-py clients the adapter uses.

Every payload is hand-built from an example in Alpaca's docs, read 2026-11-09, and parsed with the pinned
alpaca-py's own models, so the adapter's conversions are tested against what alpaca-py hands it:

* asset: "Get Assets", schema example (https://docs.alpaca.markets/us/reference/get-v2-assets-1.md)
* bar: "Historical bars", response example (https://docs.alpaca.markets/us/reference/stockbars.md)
* historical quote: "Historical quotes", the `stock_quote` schema example
  (https://docs.alpaca.markets/us/reference/stockquotes-1.md), read 2026-11-11, given as raw JSON, which
  the stand-in parses with alpaca-py's `QuoteSet`, as `get_stock_quotes` does
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
import fcntl
import gc
import inspect
import json
import logging
import os
import queue
import signal
import threading
import time as time_module
import traceback
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from time import monotonic
from types import SimpleNamespace
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.live.stock import StockDataStream
from alpaca.data.live.websocket import DataStream
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.data.models import QuoteSet
from alpaca.data.models import Trade
from alpaca.data.models import TradeCancel
from alpaca.data.models import TradeCorrection
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import StockQuotesRequest
from alpaca.trading.models import Asset
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import OrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
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


HISTORICAL_QUOTE_AAPL = {
    "ap": 387.7,
    "as": 1,
    "ax": "C",
    "bp": 387.67,
    "bs": 1,
    "bx": "N",
    "c": ["R"],
    "t": "2021-02-06T13:35:08.946977536Z",
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


def quotes_at(*times: datetime) -> list[dict]:
    """Copies of the reference's historical quote, one stamped at each UTC time given, to the microsecond."""
    return [
        {**HISTORICAL_QUOTE_AAPL, "t": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        for t in times
    ]


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
        raise api_error(404, 40410000, "asset not found")


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
    `cancel_order_by_id` answers 204 and sends `canceled` (for an order already canceled, 204 and no
    update, as paper answered a second cancel), or raises `cancel_error`; with
    `took_before_error`, it first marks the order `pending_cancel`, sending no update; `refuse_cancel` refuses
    the cancel of each order it returns an error for, and the others are canceled.
    It cancels orders placed through it and orders in `orders`, as Alpaca holds both. With `execution_ids` false, fills carry no `execution_id`. `orders` and `positions` are what the reads return, with the orders placed through it; `get_orders` applies
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

    def _send(
        self,
        event: str,
        placed: dict,
        qty: int | None = None,
        deliver: bool = True,
        **status: str,
    ) -> dict:
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
        if deliver:
            assert self.stream is not None, "no trade stream to send to"
            self.stream.send(update)
        return update

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
        if placed["status"] == "canceled" and self.cancel_error is None:
            return  # a second cancel: 204 and no update, as paper answered it (TC-E44, ADR 0036)
        if self.cancel_error is not None:
            # Taken, and refused on the repeat: Alpaca's order is pending cancel; no update is sent.
            placed.update(status="pending_cancel")
            raise self.cancel_error
        self._send("canceled", placed, status="canceled", canceled_at=datetime.now(UTC).isoformat())

    def replace_order_by_id(self, order_id: object, order_data: object = None) -> Order:
        """Answer as the paper observations of 2026-11-10 saw (ADR 0042): the replacement, status `new`
        (`partially_filled`, with the order's fills, for an order partly filled), with an Alpaca-made
        client order ID and `replaces`; then, by `replace_script`, the stream's
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
            # Paper, 2 of 2 runs (ADR 0042): a replacement of a partly filled order is answered
            # `partially_filled`, carrying the order's `qty` and `filled_qty`.
            "status": "partially_filled" if int(old.get("filled_qty") or 0) > 0 else "new",
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

    def update(
        self,
        order_id: str,
        event: str,
        qty: int | None = None,
        deliver: bool = True,
        **status: str,
    ) -> dict:
        """Send one trade update for an order the stand-in holds, as Alpaca would, and return it. With
        `deliver` false Alpaca's state and its fill activity change but the update is held back, as a
        stream that is slow or down holds it; `stream.send` delivers it later."""
        return self._send(event, self._placed[order_id], qty, deliver, **status)

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
        for raw in [*self._placed.values(), *self.orders]:  # as `get_order_by_id` sees them
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
    """Stands in for `StockHistoricalDataClient.get_stock_bars` and `get_stock_quotes`.

    It answers the way Alpaca's bars endpoint documents (https://docs.alpaca.markets/us/reference/stockbars.md):
    the bars whose timestamp, the open, lies in the inclusive `[start, end]`, in ascending time, at most
    `limit` of them. Quotes are answered the same way, as the quotes reference documents its inclusive
    `start` and `end` (https://docs.alpaca.markets/us/reference/stockquotes-1.md), from `quotes`, raw JSON
    parsed with alpaca-py's `QuoteSet`. alpaca-py has already made `start` and `end` naive UTC. With `gate`
    set, each call sets `entered` and then waits for `gate`.
    """

    def __init__(
        self,
        bars: list[dict] | None = None,
        error: Exception | None = None,
        quotes: list[dict] | None = None,
    ) -> None:
        self._session = requests.Session()  # where alpaca-py's RESTClient keeps it (ADR 0039)
        self.bars = bars if bars is not None else [BAR_AAPL]
        self.quotes = quotes if quotes is not None else []
        self.error = error
        self.requests: list[StockBarsRequest | StockQuotesRequest] = []
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

    def get_stock_quotes(self, request_params: StockQuotesRequest) -> QuoteSet:
        with self._count_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            symbol = request_params.symbol_or_symbols
            assert isinstance(symbol, str)
            return QuoteSet({symbol: self._select(request_params, self.quotes)})
        finally:
            with self._count_lock:
                self._active -= 1

    def _answer(self, request_params: StockBarsRequest) -> BarSet:
        symbol = request_params.symbol_or_symbols
        assert isinstance(symbol, str)
        return BarSet({symbol: self._select(request_params, self.bars)})

    def _select(
        self, request_params: StockBarsRequest | StockQuotesRequest, records: list[dict]
    ) -> list[dict]:
        self.requests.append(request_params)
        if self.gate is not None:
            self.entered.set()
            assert self.gate.wait(10), "gate never opened"
        if self.error is not None:
            raise self.error
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
            raise api_error(422, 42210000, "start is after end (stand-in)")
        selected = []
        for raw in sorted(records, key=lambda b: b["t"]):
            stamped = datetime.fromisoformat(raw["t"]).replace(tzinfo=None)
            if start <= stamped <= end:
                selected.append(raw)
        return selected[:limit] if limit else selected


class FakeStream:
    """Stands in for `StockDataStream`: `run()` blocks on its own asyncio loop until `stop()`.

    The venue side is modelled as Alpaca's stream documents it ("Streaming Market Data": the server
    keeps the subscriptions each subscribe message adds and each unsubscribe removes): `wire` holds what
    has been sent, and while running the stand-in sends each message in `quotes` to the handler for that
    message's symbol, and each in `trades` (then, after them, those in `trade_events`, cancels and
    corrections) to the trade handler, once per symbol on the wire per connection, the way alpaca-py's
    `_dispatch` awaits a coroutine handler per message. Cancels and corrections go to the handlers set
    with `register_trade_cancels` and `register_trade_corrections`, which alpaca-py keys under "*". With
    `ends_on_its_own`, `run()` returns after its first delivery without a `stop()`, as alpaca-py's does
    after an insufficient-subscription error. With `ignores_stop`, `run()` keeps going after `stop()`
    until `release` is set. With `refused` set, `run()` logs it as alpaca-py's `_drive_run` logs an
    authentication error, on its stream logger and thread, and returns without delivering, as it does
    for "insufficient subscription".

    As in alpaca-py 0.45.0 (`DataStream._subscribe`, `_send_subscribe_msg`, `_unsubscribe`,
    `_ensure_coroutine`, `_drive_run`, `stop`): a handler that is not a coroutine function raises
    `ValueError`; a subscribe records its handlers and then, only while running, sends every handler map
    whole; an unsubscribe sends only while running, and deletes its handlers only if that succeeded, and
    deleting a symbol never subscribed raises `KeyError`. `run()` connects only once something is
    subscribed, sends the maps as they are then, and only after that counts as running, so a subscribe
    made between the two is not sent (`snapshot_gate` holds the stand-in there, as a slow send would
    hold alpaca-py's). `stop()` stops the current run, or, before any run, the first; between runs it
    does nothing, and a later `run()` starts again. With `send_gate`, each send while running sets
    `sending` and waits for it. With `send_error` set, every send raises it. A symbol unsubscribed and
    subscribed again is served again.
    """

    def __init__(self) -> None:
        self.ends_on_its_own = False
        self.refused: str | None = None
        self.ignores_stop = False
        self.release = threading.Event()
        self.send_error: Exception | None = None
        self.send_gate: threading.Event | None = None
        self.sending = threading.Event()
        self.snapshot_gate: threading.Event | None = None
        self.snapshot_taken = threading.Event()
        self.quotes: list[Callable[[], Message]] = [quote]
        self.trades: list[Callable[[], Message]] = [trade]
        self.trade_events: list[Callable[[], Message]] = []
        self.quote_handlers: dict[str, Handler] = {}
        self.trade_handlers: dict[str, Handler] = {}
        self.cancel_handlers: dict[str, Handler] = {}
        self.correction_handlers: dict[str, Handler] = {}
        self.wire: dict[str, set[str]] = {"quotes": set(), "trades": set()}
        self.unsubscribed: list[tuple[str, tuple[str, ...]]] = []  # unsubscribes sent
        self.run_threads: list[str] = []
        self.running = threading.Event()
        self.finished = threading.Event()
        self.stopped = threading.Event()  # stop() was called, for tests to read
        self.subscribes_sent = 0  # subscribes sent while running
        self.max_active = 0  # the most subscribe or unsubscribe calls inside the stand-in at once
        self._active = 0
        self._lock = threading.Lock()
        self._run_stop: threading.Event | None = None  # the current run's stop
        self._started_once = False
        self._stop_before_first_run = False
        self._changed = threading.Event()
        self._sent: set[tuple[str, str]] = set()  # (channel, symbol) served on this connection

    def _enter(self) -> None:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)

    def _leave(self) -> None:
        with self._lock:
            self._active -= 1

    def _send(self) -> None:
        if self.send_gate is not None:
            self.sending.set()
            assert self.send_gate.wait(10), "send gate never opened"
        if self.send_error is not None:
            raise self.send_error

    def _snapshot(self) -> None:
        """What `_send_subscribe_msg` sends: every handler map, whole; the server adds them."""
        self.wire["quotes"].update(self.quote_handlers)
        self.wire["trades"].update(self.trade_handlers)

    def _subscribe(self, handler: Handler, symbols: tuple[str, ...], handlers: dict) -> None:
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("handler must be a coroutine function")
        self._enter()
        try:
            handlers.update(dict.fromkeys(symbols, handler))
            self._changed.set()
            if self.running.is_set():
                self._send()
                self.subscribes_sent += 1
                self._snapshot()
        finally:
            self._leave()

    def _unsubscribe(self, channel: str, symbols: tuple[str, ...], handlers: dict) -> None:
        self._enter()
        try:
            if self.running.is_set():
                self._send()
                self.unsubscribed.append((channel, symbols))
                self.wire[channel].difference_update(symbols)
            for s in symbols:
                del handlers[s]
                self._sent.discard((channel, s))
        finally:
            self._leave()

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
        stop = threading.Event()
        with self._lock:
            self._run_stop = stop
            if self._stop_before_first_run:
                stop.set()
                self._stop_before_first_run = False
            self._started_once = True
        self.run_threads.append(threading.current_thread().name)
        try:
            asyncio.run(self._serve(stop))
        finally:
            self.running.clear()
            with self._lock:
                if self._run_stop is stop:
                    self._run_stop = None
            self.finished.set()

    def stop(self) -> None:
        self.stopped.set()
        with self._lock:
            run = self._run_stop
            if run is None:
                if not self._started_once:
                    self._stop_before_first_run = True
                return
        run.set()
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

    async def _serve(self, stop: threading.Event) -> None:
        # alpaca-py waits for a market subscription before it connects.
        while not (self.quote_handlers or self.trade_handlers):
            if stop.is_set():
                return
            self._changed.clear()
            await asyncio.to_thread(self._changed.wait, 0.05)
        if self.refused is not None:
            logging.getLogger("alpaca.data.live.websocket").error(
                f"error during websocket communication: {self.refused}"
            )
            return
        # A new connection: the server holds nothing until the maps are sent, then counts as running.
        self.wire = {"quotes": set(), "trades": set()}
        self._sent.clear()
        self._snapshot()
        self.snapshot_taken.set()
        if self.snapshot_gate is not None:
            assert await asyncio.to_thread(self.snapshot_gate.wait, 10), (
                "snapshot gate never opened"
            )
        self.running.set()
        sent = self._sent
        while not stop.is_set() or (self.ignores_stop and not self.release.is_set()):
            for channel, builders in (
                ("quotes", self.quotes),
                ("trades", self.trades + self.trade_events),
            ):
                for symbol in sorted(self.wire[channel]):
                    if (channel, symbol) in sent:
                        continue
                    sent.add((channel, symbol))  # the server's messages for it, once
                    for build in builders:
                        message = build()
                        if message.symbol != symbol:
                            continue  # the server sends a symbol's messages only for it
                        handler = self._handler_for(message)
                        if handler is not None:
                            await handler(message)
            if self.ends_on_its_own and sent:
                return
            self._changed.clear()
            await asyncio.to_thread(self._changed.wait, 0.05)


class MemorySocket:
    """In place of the websocket under alpaca-py 0.45.0's real `DataStream`, so its own `_drive_run`,
    `_subscribe`, `_unsubscribe` and `_dispatch` run offline (`memory_stream`).

    alpaca-py packs each message with msgpack; `memory_stream` swaps the module's `msgpack` for one
    that passes the message through, so what alpaca-py sends is read as the dict it built. `sent` holds
    each message in order; `wire` is what the server would hold from them. With `gate` set, the next send
    sets `holding` and waits for it, after alpaca-py has built the message. `recv` hands over what
    `deliver` queued, as a list of messages as the server batches them, or times out after 20 ms, which
    `_consume` treats as no data and goes round again."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.gate: threading.Event | None = None
        self.holding = threading.Event()
        self.closed = False
        self._incoming: queue.Queue[list[dict]] = queue.Queue()

    @property
    def wire(self) -> dict[str, set[str]]:
        held: dict[str, set[str]] = {"quotes": set(), "trades": set()}
        for message in self.sent:
            for channel, symbols in held.items():
                if message["action"] == "subscribe":
                    symbols.update(message.get(channel, []))
                else:
                    symbols.difference_update(message.get(channel, []))
        return held

    def deliver(self, *messages: dict) -> None:
        self._incoming.put(list(messages))

    async def send(self, frames: object) -> None:
        messages: list[dict] = []
        if isinstance(frames, list):  # `_send_unsubscribe_msg` sends one packed message
            messages.extend(frames)
        else:  # `_send_subscribe_msg` sends it as frames, each a slice of the packed message
            for frame in cast("Iterable[list[dict]]", frames):
                messages.extend(frame)
        self.sent.extend(messages)
        gate, self.gate = self.gate, None
        if gate is not None:
            self.holding.set()
            assert await asyncio.to_thread(gate.wait, 10), "socket gate never opened"

    async def recv(self) -> list[dict]:
        try:
            return self._incoming.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.02)
            raise TimeoutError from None

    async def close(self) -> None:
        self.closed = True


def wire_quote() -> dict:
    """The docs' quote as the stream sends it, before alpaca-py's `_cast`: `t` as a msgpack timestamp,
    whose `to_datetime()` gives the fixture's `datetime`."""
    moment = QUOTE_AAPL["t"]
    return {**QUOTE_AAPL, "t": SimpleNamespace(to_datetime=lambda: moment)}


def memory_stream(monkeypatch: pytest.MonkeyPatch) -> tuple[StockDataStream, MemorySocket]:
    """alpaca-py's real `StockDataStream` on a `MemorySocket`: no network, no credentials.

    Only the connection and authentication (`_start_ws`) and msgpack are replaced; everything that
    subscribes, sends and dispatches is alpaca-py's."""
    import alpaca.data.live.websocket

    passthrough = SimpleNamespace(packb=lambda message: [dict(message)], unpackb=lambda raw: raw)
    monkeypatch.setattr(alpaca.data.live.websocket, "msgpack", passthrough)
    stream = StockDataStream(api_key="offline-placeholder", secret_key="offline-placeholder")
    socket = MemorySocket()

    async def start_ws() -> None:
        stream._ws = cast("Any", socket)  # a websocket's `send`, `recv` and `close`, in memory

    stream._start_ws = start_ws
    return stream, socket


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


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    """Collect, before each test body, what an earlier failure left held only in reference cycles.

    pytest keeps a failed test's exception in `sys.last_value` for post-mortem debugging until the next
    test's call phase deletes it, just before the body runs (`_pytest.runner.pytest_runtest_call`), so the
    failed test's own teardown collection (`collect_on_main_thread`) cannot free it. Cycles in what it
    held kept the failed run's NautilusTrader node alive, and the one-`LiveNode`-per-thread guard
    (`NODE_THREAD_OWNER`, `crates/live/src/node/mod.rs` at the pinned commit) then refused the next
    test's `build_node`: one failure failed the test after it. Collected here, they are gone first.
    """
    gc.collect()
    return (yield)


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
        quotes_at=quotes_at,
        HISTORICAL_QUOTE_AAPL=HISTORICAL_QUOTE_AAPL,
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
        memory_stream=memory_stream,
        wire_quote=wire_quote,
    )


# --- Account A's market-data stream lock (ADR 0069). The same block is in tests/conftest.py and
# packages/canon-alpaca/tests/conftest.py, and tests/test_session_window.py keeps the two equal. ---
#
# Alpaca allows a user "in many subscriptions (or without one)" one connection to a market-data stream
# endpoint ("Streaming Market Data", https://docs.alpaca.markets/us/docs/streaming-market-data.md). On
# 2026-11-11 two pairs of one login each held a subscribed connection at once (ADR 0069), so each account's
# pair has its own stream lock, and a paper test that opens a stream takes account A's,
# `canon-alpaca-stream-tests.lock`, whose path `mise run paper:test` exports; paper:data's streaming modules,
# on A's pair, take it too, and a window on account B takes B's instead. A test marked `alpaca_stream` holds it
# from its setup until its teardown has seen every stream it started end; a paper test not marked that starts
# an alpaca-py market-data stream is stopped before the stream connects and fails. The lock is flock(2), the
# call lockf(1) makes ("BSD-style locking is used, as described in flock(2)", lockf(1)), so this lock and the
# lockf(1) of paper:data exclude each other (Python's `fcntl.flock`). The wait blocks in flock(2) under an
# interval timer, which sleeps, rather than polling. Offline, with no
# paper pair, it does nothing.
STREAM_LOCK_ENV = "CANON_ALPACA_STREAM_LOCK"
DEADLINE_ENV = "CANON_PAPER_TEST_DEADLINE"
"""The epoch second at which `mise run paper:test` kills the run (its `deadline`), which it exports."""
STREAM_LOCK_WAIT_SECS = 3900
"""As long as `paper:test` waits for account A's lock (`lockwait` in mise.toml), and
`session_window.PAPER_LOCK_WAIT_SECS`, set when a window held the one stream lock. A window no longer takes
this lock; a `paper:data` run that streams does, and may hold it longer than this wait."""
TEST_ROOM_SECS = 900
"""Left before the run's deadline for a streaming test once it has the lock: three times a whole paper run
(about five minutes, observed), so the deadline does not kill a test before its `finally` cleans up."""
STREAM_END_SECS = 60
"""How long the teardown waits for a stream the test started to end before the lock is let go: six times
the adapter's own wait for its stream thread (`canon_alpaca.data.STREAM_JOIN_TIMEOUT_SECS`)."""
STREAM_MARKER = "alpaca_stream"
KEPT_LOCKS: list[int] = []
"""Descriptors held past their test because a stream outlived it; the lock goes when the run ends."""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{STREAM_MARKER}: the test opens an Alpaca market-data stream, so it holds account A's stream lock "
        "(ADR 0069)",
    )


def take_stream_lock(path: str, wait_secs: float) -> int | None:
    """A descriptor holding the exclusive flock(2) lock on `path`, taken within `wait_secs`, or None, the
    descriptor closed, when the lock stayed held that long. Closing the descriptor releases the lock. The
    wait blocks in flock(2) under `ITIMER_REAL`, so it runs on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "the stream lock is taken on the main thread, where SIGALRM is delivered"
        )
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    if wait_secs <= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError

    before = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, wait_secs)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except (
        TimeoutError
    ):  # also when the timer fires just after flock returns: the lock goes with fd
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def refuse_streams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop every alpaca-py market-data stream before it connects (`DataStream.run`, which alpaca-py 0.45.0's
    stock, crypto, option and news streams inherit), and return the list each refused stream is named in."""
    started: list[str] = []

    def refused(stream: DataStream) -> None:
        started.append(type(stream).__name__)
        raise RuntimeError(
            f"this test is not marked {STREAM_MARKER}, so it holds no stream lock and opens no Alpaca "
            "market-data stream (ADR 0069)"
        )

    monkeypatch.setattr(DataStream, "run", refused)
    return started


def track_streams(monkeypatch: pytest.MonkeyPatch) -> list[threading.Event]:
    """Run alpaca-py's market-data streams as before, and return one event per stream started, set once its
    `run()` has returned, so the lock is let go only after the test's streams have ended."""
    ended: list[threading.Event] = []
    run = DataStream.run

    def tracked(stream: DataStream) -> None:
        done = threading.Event()
        ended.append(done)
        try:
            run(stream)
        finally:
            done.set()

    monkeypatch.setattr(DataStream, "run", tracked)
    return ended


def stream_wait_secs(now: float) -> float:
    """How long a streaming test may wait for the lock: `STREAM_LOCK_WAIT_SECS`, less whatever would leave it
    under `TEST_ROOM_SECS` before the run's deadline; `pytest.fail` if the deadline is not named."""
    deadline = os.environ.get(DEADLINE_ENV, "")
    if not deadline.isdigit():
        pytest.fail(
            f"{DEADLINE_ENV} is not set, so the test cannot tell how long it may wait; run paper tests with "
            "`mise run paper:test`, which names it. Nothing was placed."
        )
    return min(STREAM_LOCK_WAIT_SECS, int(deadline) - now - TEST_ROOM_SECS)


@pytest.fixture(autouse=True)
def alpaca_stream_lock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Hold the stream lock for a test marked `alpaca_stream`; refuse a stream to any other paper test."""
    if not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)):
        yield  # offline: nothing reaches Alpaca
        return
    if request.node.get_closest_marker(STREAM_MARKER) is None:
        started = refuse_streams(monkeypatch)
        yield
        if started:
            pytest.fail(
                f"started an Alpaca market-data stream ({', '.join(started)}) without the "
                f"{STREAM_MARKER} marker, so without the stream lock; mark the test (ADR 0069)"
            )
        return
    path = os.environ.get(STREAM_LOCK_ENV)
    if not path:
        pytest.fail(
            f"{STREAM_LOCK_ENV} is not set, so there is no stream lock to take; run paper tests with "
            "`mise run paper:test`, which names it. Nothing was placed."
        )
    wait = stream_wait_secs(datetime.now(UTC).timestamp())
    fd = take_stream_lock(path, wait) if wait >= 0 else None
    if fd is None:
        pytest.fail(
            f"the market-data stream lock stayed held for {max(wait, 0):.0f} s (another paper test or "
            f"paper:data streams on account A's pair), or {TEST_ROOM_SECS} s were no longer left before the run's deadline; "
            "nothing was placed"
        )
    ended = track_streams(monkeypatch)
    try:
        yield
    finally:
        give_up = monotonic() + STREAM_END_SECS
        if all(e.wait(max(0.0, give_up - monotonic())) for e in ended):
            os.close(fd)  # the lock goes with the descriptor (flock(2))
        else:
            KEPT_LOCKS.append(fd)
            pytest.fail(
                f"a market-data stream this test started was still running {STREAM_END_SECS} s after it "
                "ended; the stream lock is kept until this paper run ends (ADR 0069)"
            )


# --- End of the stream lock block. ---
