"""On paper account A: a long a node did not open reaches the strategy that claims its instrument, and
that strategy can protect it (the seam the protector stands on).

Each test runs only when a paper key pair is in the environment (`mise run paper:test -k protect_seam`,
account A, under its lock) and one of Alpaca's sessions is open; it says which part it ran in. None opens
a market-data stream: the node has an execution client only, and prices come from one latest-quote read.

What is shown, at NautilusTrader v2.0.0rc8 (`nautilus-trader@d53849b216e2`):

* **The claim.** `StrategyConfig(external_order_instrument_ids=[...])` makes a venue position the cache
  does not hold a position of the claiming strategy (`docs/concepts/strategies.md`, "Claiming external
  orders"; `docs/concepts/execution/reconciliation.md`, "External order creation" and the table of tags:
  a position reconciliation order under a claim carries the tag `RECONCILIATION`, a venue order none).
  At start-up it arrives with reconciliation; while the node runs, with the engine's position check
  (`position_check_interval_secs`), since the adapter drops a trade update for an order the node does
  not hold. The seconds from Alpaca's fill to the strategy's sell working are printed for each.
* **Foreign lineage.** The long is opened by a plain alpaca-py order with no client order ID of ours
  (Alpaca writes its own), so `custody.lineage` answers `foreign` for it.
* **An owner's own sell.** A resting sell placed outside the node holds its shares: the strategy's sell
  for the same share is refused by Alpaca with HTTP 403, code 40310000, "insufficient qty available for
  order" (Alpaca's error guide, section 26; `reviews` of 2026-11-12), which reaches the strategy as the
  rejection's reason. The strategy then cancels the foreign order through the node, sees it canceled,
  and places its own; the seconds between are printed (the `replace` gap).

* **A long-only account's refusals** (ADR 0111). On an account whose `no_shorting` is true, a sell with
  no shares, and a sell for more than the long, are each refused by Alpaca at the submit; the status,
  code and words are printed. The sells are limits far over the ask, so one Alpaca accepted would rest
  and be canceled, never fill.

Every test places nothing, and fails, if the account holds an open order or a position in the symbol, and
afterwards, whatever happened, cancels every open order in it and closes its position.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Generator

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "(mise run paper:test reads them from the login keychain)"
    ),
)

SYMBOL = "AAPL"
AAPL = InstrumentId.from_str(f"{SYMBOL}.ALPACA")
ADOPTER = StrategyId("Adopter-001")
RESTING_OFFSET = Decimal(50)
"""A sell this far over the ask rests and does not fill, as the adapter's own paper tests rest theirs."""
CLEAN_ATTEMPTS = 3
"""How many rounds of cancel-and-close the cleaning makes before it names what is left."""
POSITION_CHECK_SECS = 10.0
"""The position check interval a standing protector is to use (the lead's choice of 2026-11-12 from the
measurement below; its record is not on `main` yet). It is under upstream's recommended "30-60s" (how_to/configure_live_trading); our
windows use 60. Measured on account A, 2026-11-12, from Alpaca's fill to the strategy's sell working, with
`FILL_LOOKBACK_MINS`: at 10 s, 23.3, 29.2 and 23.3 s; at 30 s, 60.2, 60.7 and 60.7 s."""
ADOPTED_WITHIN_SECS = 60.0
"""The bound the poll path is held to here: twice the slowest of the three runs measured at 10 s."""
FILL_LOOKBACK_MINS = 1
"""`position_check_lookback_mins` for a node that adopts while it runs. At the pin the position check asks
for the fills of that window before it considers a synthetic position order
(`crates/live/src/node/reconciliation.rs`, `handle_position_fill_report_result`), and a fill of an order
the node does not hold becomes a reduce-only order that cannot open a position
(`crates/execution/src/engine/mod.rs`, `materialize_external_order_from_fill`), one such fill a cycle.
Measured on account A, 2026-11-12: with the default, 60, a long was not adopted in five checks; with 1, it
was adopted one or two checks after the one that first tried its fill."""
OPEN_CHECK_SECS = 5.0
"""The low end of upstream's recommended "5-10s"; our windows use 10."""
SETTLE_SECS = 70
"""How old the foreign fill is before a node starts: older than the node's one minute of start-up order
history (`paper_node.RECONCILIATION_LOOKBACK_MINS`), so the long arrives as a position, as a long opened
by hand hours earlier would."""
STEP_SECS = 0.5
REFUSED = (OrderStatus.REJECTED, OrderStatus.DENIED)


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def paper_data() -> StockHistoricalDataClient:
    return with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    )


def open_part(trading: TradingClient) -> str:
    """The part of Alpaca's sessions open now (`session_window.session_part`), or skip the test."""
    clock = trading.get_clock()
    assert isinstance(clock, AlpacaClock)
    today = datetime.now(UTC).date()
    calendar = trading.get_calendar(
        GetCalendarRequest(start=today - timedelta(days=1), end=today + timedelta(days=5))
    )
    assert isinstance(calendar, list)
    for session in session_window.SESSIONS:
        try:
            return session_window.session_part(session, clock, calendar)[1]
        except RuntimeError:
            continue
    pytest.skip("none of Alpaca's sessions is open now")


def open_orders(trading: TradingClient) -> list[AlpacaOrder]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
    assert isinstance(orders, list)
    return orders


def held(trading: TradingClient) -> Decimal:
    """The signed quantity Alpaca holds in the symbol, 0 when it holds none (404)."""
    try:
        position = trading.get_open_position(SYMBOL)
    except APIError as e:
        if e.status_code == 404:
            return Decimal(0)
        raise
    assert isinstance(position, AlpacaPosition)
    quantity = abs(Decimal(str(position.qty)))
    return -quantity if getattr(position.side, "value", position.side) == "short" else quantity


def wait_until(done: Callable[[], bool], what: str, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds} s waiting for {what}")
        time.sleep(0.5)


def long_only(trading: TradingClient) -> bool:
    """Whether the account refuses a short sale (`no_shorting`), read from its configuration."""
    configuration = trading.get_account_configurations()
    assert isinstance(configuration, AccountConfiguration)
    return bool(configuration.no_shorting)


def part_now(trading: TradingClient, part: str) -> str:
    """The part of Alpaca's sessions open at this moment, for a step that runs minutes after the test
    read it; `part`, the one the test began in, when no session is open now or the clock cannot be read
    or its answer built, whatever the error: a close must still be placed."""
    try:
        return open_part(trading)
    except pytest.skip.Exception, Exception:  # noqa: BLE001 - any unreadable clock falls back
        return part


def limit_order(
    trading: TradingClient, side: AlpacaOrderSide, price: Decimal, part: str
) -> AlpacaOrder:
    """One share, a DAY limit, marked extended-hours outside the regular session, with no client order ID
    of ours: Alpaca writes its own, so the order is of foreign lineage."""
    order = trading.submit_order(
        LimitOrderRequest(
            symbol=SYMBOL,
            qty=1,
            side=side,
            time_in_force=AlpacaTimeInForce.DAY,
            limit_price=float(price),
            extended_hours=None if part == "regular" else True,
        )
    )
    assert isinstance(order, AlpacaOrder)
    assert custody.lineage(order.client_order_id) == custody.FOREIGN, "the order is not foreign"
    return order


def market_order(trading: TradingClient, side: AlpacaOrderSide) -> AlpacaOrder:
    """One share, a DAY market order, of foreign lineage: the regular session's plain order. Alpaca
    takes a market order only there, which is why every other part uses `limit_order`."""
    order = trading.submit_order(
        MarketOrderRequest(symbol=SYMBOL, qty=1, side=side, time_in_force=AlpacaTimeInForce.DAY)
    )
    assert isinstance(order, AlpacaOrder)
    assert custody.lineage(order.client_order_id) == custody.FOREIGN, "the order is not foreign"
    return order


def buy_foreign(trading: TradingClient, data: StockHistoricalDataClient, part: str) -> datetime:
    """Open a long of one share by a plain order of foreign lineage, and return when Alpaca filled it.
    In the regular session it is a market order: a limit a few cents through the quote this account's
    feed gives did not fill there in 60 s (2026-11-12, 09:31, 359.55 against an IEX ask of 359.50)."""
    if part == "regular":
        limit, quote = "the market", "a DAY market order"
        order = market_order(trading, AlpacaOrderSide.BUY)
    else:
        limit, quote = session_window.close_limit(data, SYMBOL, "buy", part)
        order = limit_order(trading, AlpacaOrderSide.BUY, limit, part)

    def filled() -> bool:
        found = trading.get_order_by_id(order.id)
        assert isinstance(found, AlpacaOrder)
        return getattr(found.status, "value", found.status) == "filled"

    wait_until(filled, f"the foreign buy at {limit} to fill ({part}; {quote})")
    found = trading.get_order_by_id(order.id)
    assert isinstance(found, AlpacaOrder)
    assert found.filled_at is not None
    return found.filled_at


def cancel_open(trading: TradingClient, cleaned: dict[str, int], own: set[str]) -> None:
    """Cancel every open order in the symbol and wait for the listing to empty. Each one canceled that
    the cleaning did not place itself (`own`) is counted as the test's, as it is canceled, so a wait that
    then fails loses no count. One Alpaca says is no longer cancelable closed meanwhile (the listing can
    lag a fill) and is not counted."""
    for order in open_orders(trading):
        try:
            trading.cancel_order_by_id(order.id)
        except APIError as e:
            if e.status_code != 422:
                raise
        else:
            if str(order.id) not in own:
                cleaned["orders"] += 1
    wait_until(lambda: not open_orders(trading), "the canceled orders to close")


def close_held(
    trading: TradingClient,
    data: Any,
    part: str,
    cleaned: dict[str, int] | None = None,
    own: set[str] | None = None,
) -> None:
    """One attempt to close the symbol's position: a DAY market order while Alpaca's clock says the
    regular session is open, as `custody`'s close does there, else a DAY limit through the latest quote
    marked extended hours. The session is read now, not when the test began. A holding found is counted
    once as the test's; the close order's ID is kept in `own`."""
    quantity = held(trading)
    if not quantity:
        return
    if cleaned is not None:
        cleaned["positions"] = 1
    side = AlpacaOrderSide.SELL if quantity > 0 else AlpacaOrderSide.BUY
    now = part_now(trading, part)
    if now == "regular":
        order = trading.submit_order(
            MarketOrderRequest(
                symbol=SYMBOL,
                qty=float(abs(quantity)),
                side=side,
                time_in_force=AlpacaTimeInForce.DAY,
            )
        )
    else:
        limit, _ = session_window.close_limit(data, SYMBOL, "sell" if quantity > 0 else "buy", now)
        order = trading.submit_order(
            LimitOrderRequest(
                symbol=SYMBOL,
                qty=float(abs(quantity)),
                side=side,
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=float(limit),
                extended_hours=True,
            )
        )
    if own is not None:
        own.add(str(getattr(order, "id", "")))
    wait_until(lambda: held(trading) == 0, f"the {SYMBOL} position to close")
    # the open-orders listing can lag the fill that closed it
    wait_until(lambda: not open_orders(trading), "the closing order to leave the open orders")


CLEANING_FAILURES = (AssertionError, APIError, ValueError, OSError)
"""What a cleaning step may raise without ending the cleaning: a wait that timed out, Alpaca's refusal, a
quote that cannot price the close or a response that does not parse (pydantic's error is a ValueError),
and the network."""


@contextmanager
def left_as_found(part: str) -> Generator[dict[str, int]]:
    """Refuse to start while the account holds an open order or a position in the symbol, and leave it
    so: afterwards cancel every open order in it and close its position. `orders` and `positions` count
    what the test left (its orders that were canceled, and whether a position was found); `rounds` is
    how many rounds the cleaning took.

    A cleaning step that fails does not end the cleaning (code review, 2026-11-12): each of
    `CLEAN_ATTEMPTS` rounds cancels what is open, the unfilled close of the round before included, and
    prices a new close from the latest quote; each failure is printed. What the rounds could not clear is
    then named, loudly, because a share or an order left here is of foreign lineage and `paper:close`
    freezes the symbol instead of clearing it."""
    trading, data = paper_trading(), paper_data()
    assert open_orders(trading) == [], (
        f"the paper account holds an open {SYMBOL} order; placing nothing"
    )
    assert held(trading) == 0, f"the paper account holds a {SYMBOL} position; placing nothing"
    cleaned = {"orders": 0, "positions": 0, "rounds": 0}
    try:
        yield cleaned
    finally:
        failures: list[str] = []
        own: set[str] = set()
        for attempt in range(1, CLEAN_ATTEMPTS + 1):
            cleaned["rounds"] = attempt
            try:
                cancel_open(trading, cleaned, own)
            except CLEANING_FAILURES as e:
                failures.append(f"round {attempt}, cancel: {type(e).__name__}: {e}")
            try:
                close_held(trading, data, part, cleaned, own)
            except CLEANING_FAILURES as e:
                failures.append(f"round {attempt}, close: {type(e).__name__}: {e}")
            try:
                if not open_orders(trading) and held(trading) == 0:
                    break
            except CLEANING_FAILURES as e:
                failures.append(f"round {attempt}: the account could not be read: {e}")
        else:
            raise AssertionError(
                f"the paper account was NOT left as found in {SYMBOL} after {CLEAN_ATTEMPTS} rounds; "
                f"clear it by hand before any other run: {'; '.join(failures)}"
            )
        if failures:
            print(f"\n[cleaning] took {cleaned['rounds']} rounds: {'; '.join(failures)}")


class Adopter(Strategy):
    """Claim the symbol, and once a long of it is this strategy's, place a resting sell for it.

    With `foreign_exit`, first wait for the open sell the node did not place, show that a sell of ours
    for the same share is refused, cancel the foreign one through the node, and only then place ours.
    Handlers store what an event carries; the cache is read and commands are sent from the timer only
    (`mover.py` header, "Order events"). It stops the node when done, or when `limit_secs` have passed.
    """

    def __init__(self, config: StrategyConfig) -> None:
        # rc8's `Strategy.__new__` takes the config alone, so the run's values are set by `adopter`
        super().__init__(config)
        self.sell_price = ""
        self.params: dict[str, Any] | None = None
        self.foreign_exit = False
        self.limit_ns = 0
        self.phase = "waiting for the long"
        self.started_ns: int | None = None
        self.times: dict[str, int] = {}
        """Wall-clock nanoseconds of each step the strategy saw."""
        self.seen: dict[str, Any] = {}
        self.events: list[tuple[str, str, int]] = []
        """Each order or position event: its type, its client order or position ID, the wall clock."""
        self.refusals: list[tuple[str, str]] = []
        self.own: list[Any] = []
        self.foreign: Any = None
        self.outcome = "not finished"

    # --- handlers: store only ---------------------------------------------------------------------

    def on_start(self) -> None:
        self.started_ns = time.time_ns()
        self.clock.set_timer("adopter-step", timedelta(seconds=STEP_SECS), callback=self._step)

    def on_order_event(self, event: Any) -> None:
        name = type(event).__name__
        self.events.append((name, event.client_order_id.value, time.time_ns()))
        if name in ("OrderRejected", "OrderDenied"):
            self.refusals.append((event.client_order_id.value, str(event.reason)))

    def on_position_event(self, event: Any) -> None:
        self.events.append((type(event).__name__, str(event.position_id), time.time_ns()))

    # --- the step ---------------------------------------------------------------------------------

    def _mark(self, name: str) -> None:
        self.times.setdefault(name, time.time_ns())

    def _finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.phase = "done"
        self.shutdown_system(outcome)

    def _sell(self, quantity: Any) -> Any:
        instrument = self.cache.instrument(AAPL)
        assert instrument is not None, f"{AAPL} is not in the cache"
        order = self.order_factory.limit(
            AAPL,
            OrderSide.SELL,
            instrument.make_qty(quantity),
            Price.from_str(self.sell_price),
            time_in_force=TimeInForce.DAY,
        )
        self.own.append(order.client_order_id)
        self.submit_order(order, params=self.params)
        return order.client_order_id

    def _step(self, event: Any) -> None:
        if self.phase == "done":
            return
        if self.started_ns is not None and time.time_ns() - self.started_ns > self.limit_ns:
            self._finish(f"timed out while {self.phase}")
            return
        positions = self.cache.positions_open(instrument_id=AAPL, strategy_id=self.strategy_id)
        quantity = sum((p.signed_qty for p in positions), Decimal(0))
        if self.phase == "waiting for the long":
            if quantity <= 0:
                return
            self._mark("adopted")
            self.seen["quantity"] = str(quantity)
            self.seen["average"] = [str(p.avg_px_open) for p in positions]
            self.seen["orders"] = sorted(
                (
                    order.side.name,
                    order.status.name,
                    ",".join(order.tags or []) or "no tag",
                    custody.lineage(order.client_order_id.value),
                )
                for order in self.cache.orders(instrument_id=AAPL, strategy_id=self.strategy_id)
            )
            self.phase = "waiting for the foreign sell" if self.foreign_exit else "placing"
            return
        if self.phase == "waiting for the foreign sell":
            found = [
                order
                for order in self.cache.orders_open(
                    instrument_id=AAPL, strategy_id=self.strategy_id, side=OrderSide.SELL
                )
                if order.client_order_id not in self.own
            ]
            if not found:
                return
            self.foreign = found[0].client_order_id
            self._mark("foreign sell seen")
            self.seen["foreign sell"] = (
                found[0].status.name,
                ",".join(found[0].tags or []) or "no tag",
                custody.lineage(found[0].client_order_id.value),
                str(found[0].quantity),
            )
            self.seen["refused sell"] = self._sell(quantity).value
            self._mark("held sell sent")
            self.phase = "waiting for the refusal"
            return
        if self.phase == "waiting for the refusal":
            order = self.cache.order(self.own[0])
            if order is None or not (order.is_open or order.is_closed):
                return
            if order.status not in REFUSED:
                self.cancel_order(order.client_order_id)
                self._finish(f"a sell for shares a foreign sell holds was {order.status.name}")
                return
            self._mark("refused")
            self.cancel_order(self.foreign)
            self._mark("foreign cancel sent")
            self.phase = "waiting for the foreign cancel"
            return
        if self.phase == "waiting for the foreign cancel":
            order = self.cache.order(self.foreign)
            if order is None or not order.is_closed:
                return
            self.seen["foreign sell ended"] = order.status.name
            self._mark("foreign canceled")
            self.phase = "placing"
            return
        if self.phase == "placing":
            self.seen["protective sell"] = self._sell(quantity).value
            self._mark("sell sent")
            self.phase = "waiting for the sell to work"
            return
        if self.phase == "waiting for the sell to work":
            order = self.cache.order(self.own[-1])
            if order is None or not (order.is_open or order.is_closed):
                return
            if not order.is_open:
                self._finish(f"the protective sell ended {order.status.name}")
                return
            self._mark("sell working")
            self.seen["sell lineage"] = custody.lineage(order.client_order_id.value)
            self.cancel_order(order.client_order_id)
            self.phase = "waiting for the sell's cancel"
            return
        if self.phase == "waiting for the sell's cancel":
            order = self.cache.order(self.own[-1])
            if order is not None and order.is_closed:
                self._mark("sell canceled")
                self._finish("done")


def adopter(sell_price: str, *, extended: bool, foreign_exit: bool, limit_secs: float) -> Adopter:
    strategy = Adopter(
        StrategyConfig(
            strategy_id=ADOPTER,
            order_id_tag=custody.ORDER_ID_TAG,
            external_order_instrument_ids=[AAPL],
        )
    )
    strategy.sell_price = sell_price
    strategy.params = {"extended_hours": True} if extended else None
    strategy.foreign_exit = foreign_exit
    strategy.limit_ns = int(limit_secs * 1e9)
    return strategy


def claiming_node(**checks: Any) -> LiveNode:
    """An execution-only paper node whose engine checks positions and open orders against Alpaca, with
    the trader ID and reconciliation scope every paper node here uses (`paper_node`). `checks` set or
    replace `LiveExecutionEngineConfig` settings of the runtime checks."""
    settings: dict[str, Any] = {
        "reconciliation": True,
        "reconciliation_instrument_ids": [str(AAPL)],
        "reconciliation_lookback_mins": paper_node.RECONCILIATION_LOOKBACK_MINS,
        "open_check_interval_secs": OPEN_CHECK_SECS,
        "position_check_interval_secs": POSITION_CHECK_SECS,
        **checks,
    }
    return LiveNode.build(
        "CANON-PROTECT-SEAM",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=paper_node.TRADER_ID,
            timeout_connection_secs=paper_node.TIMEOUT_CONNECTION_SECS,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=paper_node.TIMEOUT_DISCONNECTION_SECS,
            delay_post_stop_secs=0,
            exec_engine=LiveExecutionEngineConfig(**settings),
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )


def run_node(
    strategy: Adopter,
    seconds: float,
    driver: Callable[[], Awaitable[None]] | None = None,
    **checks: Any,
) -> None:
    node = claiming_node(**checks)
    node.add_strategy(strategy)

    async def hosted() -> None:
        async with asyncio.timeout(seconds):
            if driver is None:
                await node.run_async()
            else:
                await asyncio.gather(node.run_async(), driver())

    try:
        asyncio.run(hosted())
    finally:
        node.dispose()


def resting_price(data: StockHistoricalDataClient, part: str) -> str:
    """A whole-cent sell limit `RESTING_OFFSET` over the latest ask on the part's feed."""
    ask, _ = session_window.close_limit(data, SYMBOL, "buy", part)
    return str((ask + RESTING_OFFSET).quantize(Decimal("0.01")))


def secs(strategy: Adopter, earlier: str | int, later: str) -> float | None:
    """Seconds between two steps the strategy saw (or from a wall-clock time), None if either is missing."""
    start = strategy.times.get(earlier) if isinstance(earlier, str) else earlier
    end = strategy.times.get(later)
    return None if start is None or end is None else round((end - start) / 1e9, 2)


def report(part: str, what: str, strategy: Adopter, **measured: object) -> None:
    print(f"\n[{part}] {what}: outcome {strategy.outcome!r}")
    for name, value in {**strategy.seen, **measured}.items():
        print(f"  {name}: {value}")
    print(f"  events: {[name for name, _, _ in strategy.events]}")
    for _, reason in strategy.refusals:
        print(f"  refused: {reason}")


def test_paper_a_foreign_long_held_at_start_is_adopted_and_protected() -> None:
    """A long opened by a plain foreign order, older than the node's start-up order history, is the
    claiming strategy's position after start-up reconciliation, and its sell for it works at Alpaca."""
    trading, data = paper_trading(), paper_data()
    part = open_part(trading)
    with left_as_found(part) as cleaned:
        filled_at = buy_foreign(trading, data, part)
        time.sleep(max(0.0, SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
        strategy = adopter(
            resting_price(data, part), extended=part != "regular", foreign_exit=False, limit_secs=60
        )
        started = time.time_ns()
        run_node(strategy, 120)
        still_held = held(trading)
    report(
        part,
        "a long held at start",
        strategy,
        node_start_to_adopted_secs=secs(strategy, started, "adopted"),
        node_start_to_sell_working_secs=secs(strategy, started, "sell working"),
        adopted_to_sell_working_secs=secs(strategy, "adopted", "sell working"),
    )
    assert strategy.outcome == "done", (part, strategy.outcome, strategy.refusals)
    assert strategy.seen["quantity"] == "1", strategy.seen
    assert strategy.seen["sell lineage"] == custody.OURS, strategy.seen
    assert still_held == 1, "the adopted share was not left for the test to close"
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned


def test_paper_a_foreign_long_opened_while_the_node_runs_is_adopted_on_the_poll_path() -> None:
    """A long opened by a plain foreign order while the node runs is the claiming strategy's position
    within `ADOPTED_WITHIN_SECS`; the adapter drops the order's stream updates, so this is the poll
    path. The seconds from Alpaca's fill to the strategy's sell working are measured. It needs
    `FILL_LOOKBACK_MINS`: with the default the check did not adopt in five cycles."""
    checks = {"position_check_lookback_mins": FILL_LOOKBACK_MINS}
    trading, data = paper_trading(), paper_data()
    part = open_part(trading)
    fills: list[datetime] = []
    with left_as_found(part) as cleaned:
        strategy = adopter(
            resting_price(data, part),
            extended=part != "regular",
            foreign_exit=False,
            limit_secs=ADOPTED_WITHIN_SECS + 30,
        )

        async def driver() -> None:
            while strategy.started_ns is None:
                await asyncio.sleep(0.2)
            await asyncio.sleep(2)  # the node is running and flat
            fills.append(await asyncio.to_thread(buy_foreign, trading, data, part))

        run_node(strategy, ADOPTED_WITHIN_SECS + 90, driver, **checks)
    assert fills, "the foreign buy was never placed"
    filled_ns = int(fills[0].timestamp() * 1e9)
    report(
        part,
        f"a long opened while the node runs (poll path, {checks})",
        strategy,
        position_check_interval_secs=POSITION_CHECK_SECS,
        fill_to_adopted_secs=secs(strategy, filled_ns, "adopted"),
        fill_to_sell_working_secs=secs(strategy, filled_ns, "sell working"),
    )
    assert strategy.outcome == "done", (part, strategy.outcome, strategy.refusals)
    assert strategy.seen["quantity"] == "1", strategy.seen
    took = secs(strategy, filled_ns, "sell working")
    assert took is not None and took <= ADOPTED_WITHIN_SECS, strategy.times
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned


def test_paper_a_foreign_sell_holds_its_shares_and_is_replaced_through_the_node() -> None:
    """With a resting foreign sell for the adopted share: a sell of ours is refused by Alpaca by name
    (403, 40310000, insufficient qty); the foreign sell, claimed with its instrument, is canceled through
    the node; and only after its cancel is ours accepted. The cancel-to-working gap is measured."""
    trading, data = paper_trading(), paper_data()
    part = open_part(trading)
    with left_as_found(part) as cleaned:
        filled_at = buy_foreign(trading, data, part)
        price = resting_price(data, part)
        limit_order(trading, AlpacaOrderSide.SELL, Decimal(price) + 1, part)
        time.sleep(max(0.0, SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
        strategy = adopter(price, extended=part != "regular", foreign_exit=True, limit_secs=90)
        run_node(strategy, 150)
    report(
        part,
        "a foreign sell for the adopted share (respect, then replace)",
        strategy,
        foreign_cancel_to_canceled_secs=secs(strategy, "foreign cancel sent", "foreign canceled"),
        foreign_cancel_to_our_sell_working_secs=secs(
            strategy, "foreign cancel sent", "sell working"
        ),
    )
    assert strategy.outcome == "done", (part, strategy.outcome, strategy.refusals)
    assert strategy.seen["foreign sell"][2] == custody.FOREIGN, strategy.seen
    refused = [reason for cid, reason in strategy.refusals if cid == strategy.seen["refused sell"]]
    assert refused, "the sell for shares a foreign sell holds was not refused"
    assert "40310000" in refused[0] or "insufficient qty" in refused[0].lower(), refused
    assert strategy.seen["foreign sell ended"] == "CANCELED", strategy.seen
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned


def refusal(trading: TradingClient, quantity: int, price: Decimal, part: str) -> str:
    """What Alpaca says to a plain limit SELL of `quantity` at `price`: its refusal at the submit as
    `status code message`, or `accepted` when it takes the order (which then rests far over the ask)."""
    try:
        trading.submit_order(
            LimitOrderRequest(
                symbol=SYMBOL,
                qty=quantity,
                side=AlpacaOrderSide.SELL,
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=float(price),
                extended_hours=None if part == "regular" else True,
            )
        )
    except APIError as e:
        return f"{e.status_code} {e.code} {e.message}"
    return "accepted"


def test_paper_a_sell_with_no_shares_or_beyond_the_long_is_refused_on_a_long_only_account() -> None:
    """On a long-only account Alpaca refuses, at the submit, a sell while flat and a sell for more
    than the long. Skipped on an account that allows shorting (`mise run paper:long-only tests on`)."""
    trading, data = paper_trading(), paper_data()
    if not long_only(trading):
        pytest.skip("the paper account allows shorting: mise run paper:long-only tests on")
    part = open_part(trading)
    with left_as_found(part) as cleaned:
        price = Decimal(resting_price(data, part))
        flat = refusal(trading, 1, price, part)
        buy_foreign(trading, data, part)
        beyond = refusal(trading, 2, price, part)
        within = refusal(trading, 1, price, part)
    print(f"\n[{part}] a long-only account (no_shorting true), limit SELL far over the ask:")
    print(f"  1 share while flat: {flat}")
    print(f"  2 shares while long 1: {beyond}")
    print(f"  1 share while long 1: {within}")
    assert flat.startswith("403 "), f"a sell with no shares was not refused: {flat}"
    assert beyond.startswith("403 "), f"a sell for more than the long was not refused: {beyond}"
    assert within == "accepted", f"a sell for the share held was refused: {within}"
    assert (cleaned["orders"], cleaned["positions"]) == (1, 1), cleaned
