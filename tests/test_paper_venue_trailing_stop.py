"""Paper proof, in the regular session, that a trailing stop for a long reaches Alpaca through the adapter
as Alpaca's own `trailing_stop`, is replaced and is canceled (ADR 0129).

Contract: Alpaca's trailing stop (`https://docs.alpaca.markets/us/docs/orders-at-alpaca.md`, "Trailing
Stop Orders": `trail_percent`, `hwm`, a stop price of "hwm * 0.99" for a trail of 1.0, a replace's
`trail`) and NautilusTrader's `TRAILING_STOP_MARKET` order with a `BASIS_POINTS` offset, which the adapter
sends when `submit_trailing_stops` is on. The trail is changed with `params={"trailing_offset": ...}`, the
convention of NautilusTrader's Interactive Brokers adapter, together with a real change of quantity,
since a strategy's `modify_order` needs one at the pin.

What is shown, through one execution-only node (the trading stream and REST; no market-data stream): on
a long of two shares adopted at start, a DAY trailing stop for both is accepted; Alpaca holds it as a
`trailing_stop` with the trail asked; a replace to one share with a tighter trail is taken; and the
cancel ends it. What Alpaca holds is read over REST from its open orders, never by a venue order ID.
Recorded and printed, not required, because no page read says when Alpaca fills them in: the high-water
mark, whether the stop price follows from it and the trail, and the lineage custody reads for the
replaced stop. By custody's rule an order Alpaca made by replacing one of ours is foreign, which is what
`paper:close` and a task that places stops will see after any replace.

The order is DAY, not GTC: the paper task ends a run that overstays with a kill, which runs no `finally`,
and a DAY stop left behind ends with the session where a GTC one would rest for days. GTC through the
adapter is shown by the run that needs it, not here.

The node's risk engine is bypassed, as NautilusTrader's execution testing spec sets a node up
(`docs/developer_guide/spec_exec_testing.md`, "Risk engine bypassed"): the engine prices a `DEFAULT`
trailing stop from a cached trade and denies it with none, and this node has no data client. That check
is therefore not exercised here; a node with a trade feed exercises it.

Not shown here: an election. No stop in this test is left to trigger, and the trail is wide.

Run under `mise run paper:test`, selected with `-k`, which holds the account's lock. It skips outside
the regular session and on an account that allows shorting (ADR 0129, "Where it may be used"). It places
nothing while the account holds an open order or a position in the symbol, and cancels and closes what it
opened in a `finally` (`tests/test_paper_protect_seam.py`, `left_as_found`). Its parts are walked offline
in `tests/test_trailing_stop_paper_proof_offline.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from alpaca.common.exceptions import APIError
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType

from canon_trading import custody
from canon_trading import paper_node
from tests import test_paper_protect_seam as seam

pytestmark = seam.pytestmark

AAPL = seam.AAPL
SHARES = 2
"""Two, so the replace has a real change of quantity to carry the trail with."""
FIRST_BPS = Decimal(500)
SECOND_BPS = Decimal(400)
"""Wide trails, 5% then 4%: nothing here is meant to be elected."""
CENT = Decimal("0.01")
READ_SECS = 20.0
"""How long one read waits for Alpaca's listing to show the stop as asked: the listing lags an order's
events (`seam.close_held`'s last wait). UNCALIBRATED."""
READ_FAILURES = (AssertionError, APIError, ValueError, OSError)
"""What a read may raise without ending the run: a listing that never settled, Alpaca's refusal, an answer
that does not parse, and the network. The read is then recorded as failed and the walk goes on to its
cancel, so the node stops by itself and the report still prints."""


def text(value: object) -> str:
    return "none" if value is None else str(getattr(value, "value", value))


def at_alpaca(trading: Any) -> dict[str, str]:
    """What Alpaca holds as the symbol's one open order, with no venue order ID and no price."""
    orders = seam.open_orders(trading)
    assert len(orders) == 1, f"{len(orders)} open {seam.SYMBOL} orders, not one"
    (order,) = orders
    follows = "unknown"
    if order.hwm is not None and order.stop_price is not None and order.trail_percent is not None:
        expected = Decimal(str(order.hwm)) * (1 - Decimal(str(order.trail_percent)) / 100)
        follows = "yes" if abs(Decimal(str(order.stop_price)) - expected) <= CENT else "no"
    return {
        "type": text(order.type),
        "side": text(order.side),
        "quantity": text(order.qty),
        "time in force": text(order.time_in_force),
        "trail percent": text(order.trail_percent),
        "trail price": text(order.trail_price),
        "high-water mark": "none" if order.hwm is None else "set",
        "stop price follows the mark and the trail": follows,
        "position intent": text(order.position_intent),
        "lineage": custody.lineage(order.client_order_id),
    }


def read_stop(trading: Any, quantity: int, percent: Decimal, seconds: float = READ_SECS) -> dict:
    """One read of the stop, waiting up to `seconds` for the listing to show one order of the quantity
    and trail asked with its high-water mark set. What it last saw is returned either way; a read that
    could not be made at all is returned as `{"failed": why}`, never raised."""
    deadline = time.monotonic() + seconds
    seen: dict[str, str] = {"failed": "no read was made"}
    while True:
        try:
            seen = at_alpaca(trading)
        except READ_FAILURES as e:
            # the first line: pytest adds its own account of a failed assertion after it
            seen = {"failed": f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"}
        else:
            settled = (
                Decimal(seen["quantity"]) == quantity
                and seen["trail percent"] != "none"
                and Decimal(seen["trail percent"]) == percent
                and seen["high-water mark"] == "set"
            )
            if settled:
                return seen
        if time.monotonic() >= deadline:
            return seen
        time.sleep(seam.STEP_SECS)


class Trailer(seam.Adopter):
    """The seam test's adopter, whose sell for the adopted long is a DAY trailing stop that it then
    replaces to one share with a tighter trail and cancels. It waits for the whole long before it
    places anything, and at two points for the test to read what Alpaca holds (`read`)."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.read: dict[str, dict[str, str]] = {}
        self.modify_refusals: list[str] = []

    def on_order_event(self, event: Any) -> None:
        seam.Adopter.on_order_event(self, event)
        if type(event).__name__ == "OrderModifyRejected":
            self.modify_refusals.append(str(event.reason))

    def _sell(self, quantity: Any) -> Any:
        instrument = self.cache.instrument(AAPL)
        assert instrument is not None, f"{AAPL} is not in the cache"
        order = self.order_factory.trailing_stop_market(
            AAPL,
            OrderSide.SELL,
            instrument.make_qty(quantity),
            FIRST_BPS,
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
            time_in_force=TimeInForce.DAY,
        )
        self.own.append(order.client_order_id)
        self.submit_order(order)
        return order.client_order_id

    def _timed_out(self) -> bool:
        if self.started_ns is not None and time.time_ns() - self.started_ns > self.limit_ns:
            self._finish(f"timed out while {self.phase}")
            return True
        return False

    def _step(self, event: Any) -> None:
        if self.phase == "waiting for the long":
            # the two buys are adopted one fill at a time: the stop is for the whole long or not placed
            positions = self.cache.positions_open(instrument_id=AAPL, strategy_id=self.strategy_id)
            if 0 < sum((p.signed_qty for p in positions), Decimal(0)) < SHARES:
                self._timed_out()
                return
        if self.phase in (
            "done",
            "waiting for the long",
            "placing",
            "waiting for the sell's cancel",
        ):
            # named, not `super()`: the offline cases walk these steps on a stand-in for the strategy
            seam.Adopter._step(self, event)
            return
        if self._timed_out():
            return
        order = self.cache.order(self.own[-1])
        if order is None or not (order.is_open or order.is_closed):
            return
        if not order.is_open:
            self._finish(f"the trailing stop ended {order.status.name} while {self.phase}")
            return
        if self.phase == "waiting for the sell to work":
            self._mark("stop working")
            self.phase = "waiting for the first read"
        elif self.phase == "waiting for the first read":
            if "first" not in self.read:
                return
            if "failed" in self.read["first"]:
                # nothing was read: do not change an order whose state is unknown, only end it
                self.cancel_order(order.client_order_id)
                self._mark("cancel sent")
                self.phase = "waiting for the sell's cancel"
                return
            instrument = self.cache.instrument(AAPL)
            assert instrument is not None
            self.modify_order(
                order.client_order_id,
                quantity=instrument.make_qty(SHARES - 1),
                params={"trailing_offset": str(SECOND_BPS)},
            )
            self._mark("replace sent")
            self.phase = "waiting for the replace"
        elif self.phase == "waiting for the replace":
            if self.modify_refusals:
                self.cancel_order(order.client_order_id)
                self._finish(f"the replace was refused: {self.modify_refusals[-1]}")
            elif order.quantity.as_decimal() == SHARES - 1 and not order.is_pending_update:
                self._mark("replaced")
                self.phase = "waiting for the second read"
        elif self.phase == "waiting for the second read":
            if "second" not in self.read:
                return
            self.cancel_order(order.client_order_id)
            self._mark("cancel sent")
            self.phase = "waiting for the sell's cancel"


def node_config() -> LiveNodeConfig:
    """The seam test's execution-only paper node, with the risk engine bypassed and the trailing stop
    on. The settings are the seam's (`seam.claiming_node`), kept beside it and not shared: that file
    is the paper tests' own seam and is not widened for one test's two settings."""
    return LiveNodeConfig(
        environment=Environment.SANDBOX,
        trader_id=paper_node.TRADER_ID,
        timeout_connection_secs=paper_node.TIMEOUT_CONNECTION_SECS,
        timeout_reconciliation_secs=20,
        timeout_disconnection_secs=paper_node.TIMEOUT_DISCONNECTION_SECS,
        delay_post_stop_secs=0,
        risk_engine=LiveRiskEngineConfig(bypass=True),
        exec_engine=LiveExecutionEngineConfig(
            reconciliation=True,
            reconciliation_instrument_ids=[str(AAPL)],
            reconciliation_lookback_mins=paper_node.RECONCILIATION_LOOKBACK_MINS,
            open_check_interval_secs=seam.OPEN_CHECK_SECS,
            position_check_interval_secs=seam.POSITION_CHECK_SECS,
            position_check_lookback_mins=seam.FILL_LOOKBACK_MINS,
        ),
        exec_clients={
            "ALPACA": AlpacaExecutionClientConfig(
                instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                submit_trailing_stops=True,
            ),
        },
    )


def node_for(strategy: Any) -> LiveNode:
    """The node, built and not run. Building it sends nothing: the client connects when it runs."""
    node = LiveNode.build(
        "CANON-VENUE-TRAIL", node_config(), exec_factories={"ALPACA": AlpacaExecutionClientFactory}
    )
    node.add_strategy(strategy)
    return node


async def reads(strategy: Any, trading: Any) -> None:
    """Make the strategy's two reads when it waits for them, off the node's loop."""
    asked = {"first": (SHARES, FIRST_BPS / 100), "second": (SHARES - 1, SECOND_BPS / 100)}
    while strategy.phase != "done":
        for name, (quantity, percent) in asked.items():
            if strategy.phase == f"waiting for the {name} read" and name not in strategy.read:
                strategy.read[name] = await asyncio.to_thread(read_stop, trading, quantity, percent)
        await asyncio.sleep(seam.STEP_SECS)


def run(strategy: Trailer, seconds: float, trading: Any) -> None:
    """Run the node while the test reads Alpaca's open order at the strategy's two waits. The reads
    end when the node does, whatever ended it, so a node that stopped by itself is not waited for."""
    node = node_for(strategy)

    async def hosted() -> None:
        async with asyncio.timeout(seconds):
            running = asyncio.ensure_future(node.run_async())
            reading = asyncio.ensure_future(reads(strategy, trading))
            try:
                await asyncio.wait({running, reading}, return_when=asyncio.FIRST_COMPLETED)
                await running  # the node's own end, or what ended it
            finally:
                for task in (reading, running):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

    try:
        asyncio.run(hosted())
    finally:
        node.dispose()


def trailer(limit_secs: float) -> Trailer:
    strategy = Trailer(
        StrategyConfig(
            strategy_id=seam.ADOPTER,
            order_id_tag=custody.ORDER_ID_TAG,
            external_order_instrument_ids=[AAPL],
        )
    )
    strategy.limit_ns = int(limit_secs * 1e9)
    return strategy


def test_paper_a_trailing_stop_rests_at_alpaca_is_replaced_and_is_canceled() -> None:
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    if part != "regular":
        pytest.skip(f"Alpaca's trailing stop is a regular-session order; the open part is {part}")
    if not seam.long_only(trading):
        pytest.skip("the venue trailing stop is for a long-only account until its proof (ADR 0129)")
    with seam.left_as_found(part) as cleaned:
        filled_at = datetime.now(UTC)
        for _ in range(SHARES):
            filled_at = seam.buy_foreign(trading, data, part)
        time.sleep(max(0.0, seam.SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
        strategy = trailer(150)
        run(strategy, 210, trading)
        still_held = seam.held(trading)
    seam.report(part, "a trailing stop through the adapter", strategy, **strategy.read)
    own = strategy.own[-1].value if strategy.own else ""
    names = [name for name, order_id, _ in strategy.events if order_id == own]
    first, second = strategy.read.get("first", {}), strategy.read.get("second", {})
    assert strategy.outcome == "done", (
        strategy.outcome,
        strategy.refusals,
        strategy.modify_refusals,
    )
    assert Decimal(strategy.seen["quantity"]) == SHARES, strategy.seen
    for name, held_stop, quantity, percent in (
        ("first", first, SHARES, FIRST_BPS / 100),
        ("second", second, SHARES - 1, SECOND_BPS / 100),
    ):
        assert "failed" not in held_stop and held_stop, (name, held_stop)
        assert (held_stop["type"], held_stop["side"]) == ("trailing_stop", "sell"), (
            name,
            held_stop,
        )
        assert Decimal(held_stop["quantity"]) == quantity, (name, held_stop)
        assert held_stop["time in force"] == "day", (name, held_stop)
        assert Decimal(held_stop["trail percent"]) == percent, (name, held_stop)
        assert held_stop["trail price"] == "none", (name, held_stop)
        # the mark and the stop price are Alpaca's to fill in, when is not documented: a stop price
        # that contradicts them fails, one not yet shown is only printed
        assert held_stop["stop price follows the mark and the trail"] != "no", (name, held_stop)
    # The stop as placed carries the node's own client order ID. Its replacement carries one Alpaca
    # made, since the adapter's replace names none, and reads foreign by custody's rule
    # (`custody.py`, "Lineage"): printed above, not required.
    assert first["lineage"] == custody.OURS, first
    for expected in ("OrderSubmitted", "OrderAccepted", "OrderCanceled"):
        assert names.count(expected) == 1, names
    # at least one: the engine's open-order check adds an update of its own when it sees the trigger
    # Alpaca moved, which a trailing stop's always has
    assert names.count("OrderUpdated") >= 1, names
    assert "OrderTriggered" not in names and "OrderFilled" not in names, names
    assert still_held == Decimal(SHARES), still_held
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned
