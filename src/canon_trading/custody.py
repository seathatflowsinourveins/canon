"""Custody on a paper account: which orders our nodes wrote, and a read of the account that says so.

Contract: alpaca-py 0.45.0's `TradingClient` (paper) `get_account`, `get_all_positions`, `get_orders` with a
`GetOrdersRequest` and `get_account_configurations`, and its `TradeAccount`, `Position`, `Order` and
`AccountConfiguration` models (`trading/client.py`, `trading/models.py`, read in `.venv` 2026-11-11);
NautilusTrader's client order ID generator at the pin (`d53849b216e2`). Sources, read 2026-11-11:

* `crates/common/src/generators/client_order_id.rs` at the pin: unless a strategy asks for UUIDs, its
  client order IDs are `O-{YYYYMMDD}-{HHMMSS}-{trader tag}-{strategy tag}-{count}`, the time in UTC to the
  second, with hyphens by default (`use_hyphens_in_client_order_ids`, `crates/trading/src/strategy/config.rs`);
  `TraderId::get_tag` is the part after the last hyphen (`crates/model/src/identifiers/trader_id.rs`).
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md: "Once an order is placed, it can be queried
  using either the client-provided order ID or the system-assigned unique ID";
  https://docs.alpaca.markets/us/reference/postorder.md: `client_order_id` is "Automatically generated if
  not sent", so an order placed without one (the dashboard, another tool) carries an ID Alpaca made.
* https://docs.alpaca.markets/us/reference/getallorders-1.md: `status=open`, "Defaults to 50 and max is 500".

Lineage (the owner's requirement of 2026-11-11, restated: the owner may place orders on these accounts, so a
node is to touch only what its own lineage wrote; any other order, or a quantity that does not match, is to
freeze that symbol and be reported, never touched). This module classifies; so far only `paper:account`
reads the classification, and nothing here sends, cancels or closes anything. The check after a window's
stop does not consult it yet: it cancels every open order in the window's symbol (ADR 0060, "Bad",
2026-11-11). Our nodes are built by `paper_node.alpaca_paper_builder` under
`paper_node.TRADER_ID` (`TESTER-001`), and every strategy that sends orders there is given the order ID tag
`001` (`SessionMaCross` in `session_window`, `EmaCross` in `paper_node`), so they write
`O-YYYYMMDD-HHMMSS-001-001-N`; the account check after a window's stop writes
`canon-close-YYYYMMDDTHHMMSSZ-N-<8 hex>` (`session_window.CLOSE_ID_PREFIX`, ADR 0060). An ID of any other form is foreign, and an order with no ID
is never ours either (`MISSING`), since nothing shows it is ours. An order Alpaca made by replacing one of ours
carries an ID Alpaca made, and is foreign by this rule: no window strategy replaces an order (ADR 0060). On
account A, the paper tests' own orders that carry IDs of another form (a test's hand-made ID, another trader's
or another strategy tag) read foreign too: the rule names what our window and paper nodes write, and nothing
else is treated as theirs.

`mise run paper:account <tests|windows>` prints the account through `account_task`: equity, cash and buying
power, each position, each open order with its lineage, and `no_shorting`. It reads only: the task and
`account_task` set data-only mode (`CANON_ALPACA_DATA_ONLY=1`, ADR 0070), so the client
`session_window.paper_trading` builds refuses every method but GET before anything is sent. No account
number, account ID, order ID or client order ID is printed; an order is named by its symbol, side,
quantity, type, limit, status and lineage.
"""

from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetOrdersRequest
from canon_alpaca.execution import ORDERS_PAGE_LIMIT
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import require_get_only

from .paper_node import TRADER_ID

if TYPE_CHECKING:
    from collections.abc import Callable

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
CLOSE_CLIENT_ORDER_ID = re.compile(
    rf"{re.escape(CHECK_CLOSE_PREFIX)}[0-9]{{8}}T[0-9]{{6}}Z-[0-9]+-[0-9a-f]{{8}}", re.ASCII
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
    qty = order.qty if order.qty is not None else f"notional {_value(order.notional)}"
    kind = _value(order.type if order.type is not None else order.order_type)
    return (
        f"{order.symbol} {_value(order.side)} {qty} {kind} limit {_value(order.limit_price)} "
        f"status {_value(order.status)} {lineage(order.client_order_id)}"
    )


def position_line(position: Position) -> str:
    """One position: symbol, side, quantity, average entry, market value, unrealised result."""
    return (
        f"{position.symbol} {_value(position.side)} {position.qty} avg_entry {position.avg_entry_price} "
        f"market_value {_value(position.market_value)} unrealized_pl {_value(position.unrealized_pl)}"
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
        status = getattr(e, "status_code", None)
        raise ReadFailed(
            f"{name} failed: {type(e).__name__}"
            + (f" (HTTP {status})" if status is not None else "")
        ) from e
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
    """`mise run paper:account <tests|windows>`: print `account_lines` in data-only mode; 0, or 1 naming
    the read that failed (`ReadFailed`), or the client that would send more than GET. The client, built
    here or given, is checked to refuse every method but GET before anything is read (`require_get_only`,
    as the data-only modules check theirs)."""
    os.environ[DATA_ONLY_ENV] = (
        "1"  # the task sets it too; the module does not rely on that (ADR 0070)
    )
    try:
        if trading is None:
            from .session_window import paper_trading  # the paper client with the adapter's timeout

            trading = paper_trading()
        require_get_only(trading)
        lines = account_lines(trading)
    except (ReadFailed, MethodRefused) as e:
        print(f"paper:account: {e}; nothing else was read", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0
