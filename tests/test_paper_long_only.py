"""A sell while flat on a paper account Alpaca holds long-only is refused by Alpaca, by name.

It runs only when a paper key pair is in the environment (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) and Alpaca's
account configuration says `no_shorting` is true; otherwise pytest reports it skipped with the reason. Run it
with `mise run paper:test -k long_only` (account A, under its lock), after `mise run paper:long-only tests on`.
Never write the keys to a file.

What it calls: alpaca-py 0.45.0's paper `TradingClient` (`get_account_configurations`, `get_clock`,
`get_orders`, `get_open_position`, `submit_order` with a `MarketOrderRequest` under a client order ID of its
own, `get_order_by_client_id`, `cancel_order_by_id`, `close_position`). Sources, read 2026-11-11:
alpaca-py's `AccountConfiguration` ("no_shorting (bool): If true then Account becomes long-only mode") and
https://docs.alpaca.markets/us/reference/patchaccountconfig-1.md ("If true, account becomes long-only mode").
Neither says how Alpaca refuses an order that would sell short on such an account, at the submit (an
`APIError`) or after it (an order `rejected`); the test takes either and prints what Alpaca said. A
refusal at the submit carries Alpaca's message, which must name shorting; an order Alpaca rejects after
accepting it carries no reason in alpaca-py's `Order`, so there the status names the refusal
(UNVERIFIED which of the two Alpaca does until the first run). A market order needs the regular session,
so it skips outside it.

The account is left as it was found: it places nothing, and fails, if the account holds an open SPY order or
a SPY position; afterwards, whatever happened, it cancels the order it sent if Alpaca still holds it open and
closes any SPY position, then fails naming whatever is left.
"""

import os
import time
from decimal import Decimal
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Clock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY

SYMBOL = "SPY"
ENDED = frozenset({"filled", "canceled", "expired", "rejected"})
"""The statuses after which "no further updates will occur" (Alpaca, "Orders at Alpaca", Order Lifecycle)."""
WAIT_SECS = 30

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "(mise run paper:test reads them from the login keychain)"
    ),
)


def paper_trading() -> TradingClient:
    return TradingClient(
        api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
    )


def open_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
    assert isinstance(orders, list)
    return orders


def position_qty(trading: TradingClient) -> Decimal:
    """The signed SPY quantity Alpaca holds, 0 when it holds none (404)."""
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return Decimal(0)
        raise
    assert isinstance(position, AlpacaPosition)
    qty = abs(Decimal(str(position.qty)))
    return -qty if getattr(position.side, "value", position.side) == "short" else qty


def status(order: AlpacaOrder) -> str:
    return str(getattr(order.status, "value", order.status))


def test_paper_a_sell_while_flat_on_a_long_only_account_is_refused_by_alpaca() -> None:
    trading = paper_trading()
    configuration = trading.get_account_configurations()
    assert isinstance(configuration, AccountConfiguration)
    if not configuration.no_shorting:
        pytest.skip("the paper account allows shorting: mise run paper:long-only tests on")
    clock = trading.get_clock()
    assert isinstance(clock, Clock)
    if not clock.is_open:
        pytest.skip("the regular session is closed; a market order would wait for the open")
    assert open_orders(trading) == [], "the paper account holds an open SPY order; placing nothing"
    assert position_qty(trading) == 0, "the paper account holds a SPY position; placing nothing"

    client_order_id = f"canon-long-only-{uuid4().hex[:12]}"
    try:
        try:
            order = trading.submit_order(
                MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=1,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id,
                )
            )
        except APIError as e:
            said = f"refused at the submit: {e.status_code} {e}"
            print(f"Alpaca on a sell of 1 {SYMBOL} while flat, no_shorting true: {said}")
            assert "short" in said.lower(), (
                f"Alpaca refused the sell, but not naming shorting: {said}"
            )
        else:
            assert isinstance(order, AlpacaOrder)
            end = time.monotonic() + WAIT_SECS
            while status(order) not in ENDED and time.monotonic() < end:
                time.sleep(0.5)
                order = trading.get_order_by_client_id(client_order_id)
                assert isinstance(order, AlpacaOrder)
            said = f"accepted, then {status(order)}"
            print(f"Alpaca on a sell of 1 {SYMBOL} while flat, no_shorting true: {said}")
            assert status(order) == "rejected", f"Alpaca did not refuse the sell while flat: {said}"
    finally:
        left: list[str] = []
        for o in open_orders(trading):
            if o.client_order_id == client_order_id:
                trading.cancel_order_by_id(o.id)
        end = time.monotonic() + WAIT_SECS
        while any(o.client_order_id == client_order_id for o in open_orders(trading)):
            if time.monotonic() > end:
                left.append(f"open order {client_order_id}")
                break
            time.sleep(0.5)
        if not left and position_qty(trading) != 0:
            trading.close_position(SYMBOL)
            end = time.monotonic() + WAIT_SECS
            while position_qty(trading) != 0 and time.monotonic() < end:
                time.sleep(0.5)
        if qty := position_qty(trading):
            left.append(f"position of {qty} {SYMBOL}")
        assert not left, f"the paper account was not left as found: {left}"
