"""Paper proofs, on account A, that a sell goes to Alpaca as `position_intent=sell_to_close` through the
adapter (ADR 0111), for an extended-hours limit and, in the regular session, the same limit unmarked.

Contract: NautilusTrader's reduce-only execution contract (`docs/concepts/adapters.md`, "Reduce-only": send a
documented venue instruction or refuse before transport) and Alpaca's order field `position_intent`
(`https://docs.alpaca.markets/reference/postorder`), which the adapter sets on a reduce-only SELL when
`reduce_only_sells` is on (off, it denies every reduce-only order before transport) and, with
`sell_to_close_on_long_exits`, on a plain SELL while the node's cached net position is long.

What is shown, each through an execution-only node (the trading stream and REST; no market-data stream):
a reduce-only SELL while flat is refused, with Alpaca's code and words in the rejection; on a long adopted
at start, a plain SELL with the setting off, a plain SELL with it on and a reduce-only SELL each work, and
the intent Alpaca holds for each order is read back by our client order ID.

Run under `mise run paper:test`, which holds account A's lock. It places nothing while the account holds
an open order or a position in the symbol, and cancels and closes what it opened in a `finally`
(`tests/test_paper_protect_seam.py`, `left_as_found`).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import pytest
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.factories import AlpacaExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window
from tests import test_paper_protect_seam as seam

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

pytestmark = seam.pytestmark

AAPL = seam.AAPL
REDUCE_ONLY = {"reduce_only_sells": True}
"""The adapter's gate for reduce-only SELLs, off by default (ADR 0111)."""


def sell_order(strategy: Any, quantity: Any, reduce_only: bool) -> Any:
    """One DAY sell of the strategy's `kind`: a limit at `sell_price`, a market order, or a stop or
    stop-limit triggered at `stop_price`, each of which the adapter sends to Alpaca as that order type."""
    instrument = strategy.cache.instrument(AAPL)
    assert instrument is not None, f"{AAPL} is not in the cache"
    size, factory, day = instrument.make_qty(quantity), strategy.order_factory, TimeInForce.DAY
    if strategy.kind == "market":
        return factory.market(
            AAPL, OrderSide.SELL, size, time_in_force=day, reduce_only=reduce_only
        )
    if strategy.kind == "stop_market":
        return factory.stop_market(
            AAPL,
            OrderSide.SELL,
            size,
            trigger_price=Price.from_str(strategy.stop_price),
            time_in_force=day,
            reduce_only=reduce_only,
        )
    if strategy.kind == "stop_limit":
        return factory.stop_limit(
            AAPL,
            OrderSide.SELL,
            size,
            price=Price.from_str(str(Decimal(strategy.stop_price) - 1)),
            trigger_price=Price.from_str(strategy.stop_price),
            time_in_force=day,
            reduce_only=reduce_only,
        )
    return factory.limit(
        AAPL,
        OrderSide.SELL,
        size,
        Price.from_str(strategy.sell_price),
        time_in_force=day,
        reduce_only=reduce_only,
    )


class Closer(seam.Adopter):
    """The seam test's adopter, whose one sell for the adopted long may carry the reduce-only flag."""

    reduce_only = False
    kind = "limit"
    stop_price = ""
    hold = False
    """While true the sell is not placed: the test changes the holding at Alpaca first."""

    def _step(self, event: Any) -> None:
        if self.phase == "placing" and self.hold:
            if self.started_ns is not None and time.time_ns() - self.started_ns > self.limit_ns:
                self._finish("timed out while the test held the sell back")
            return
        super()._step(event)

    def _sell(self, quantity: Any) -> Any:
        order = sell_order(self, quantity, self.reduce_only)
        self.own.append(order.client_order_id)
        self.submit_order(order, params=self.params)
        return order.client_order_id


class FlatSeller(Strategy):
    """With no position, send one reduce-only SELL for one share far over the ask and keep what became of
    it. Handlers store; the timer reads the cache and sends (`mover.py` header, "Order events")."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.sell_price = ""
        self.stop_price = ""
        self.kind = "limit"
        self.params: dict[str, Any] | None = None
        self.started_ns: int | None = None
        self.sent: Any = None
        self.refusals: list[str] = []
        self.canceling = False
        self.outcome = "not finished"

    def on_start(self) -> None:
        self.started_ns = time.time_ns()
        self.clock.set_timer("flat-step", timedelta(seconds=seam.STEP_SECS), callback=self._step)

    def on_order_event(self, event: Any) -> None:
        if type(event).__name__ in ("OrderRejected", "OrderDenied"):
            self.refusals.append(f"{type(event).__name__}: {event.reason}")

    def _finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.shutdown_system(outcome)

    def _step(self, event: Any) -> None:
        if self.outcome != "not finished":
            return
        if self.started_ns is not None and time.time_ns() - self.started_ns > 45e9:
            self._finish("timed out")
            return
        if self.sent is None:
            if self.cache.positions_open(instrument_id=AAPL):
                self._finish("the node holds a position; nothing sent")
                return
            order = sell_order(self, 1, reduce_only=True)
            self.sent = order.client_order_id
            self.submit_order(order, params=self.params)
            return
        order = self.cache.order(self.sent)
        if order is None or not (order.is_open or order.is_closed):
            return
        if order.is_open:
            # accepted, which the test exists to catch: cancel it and wait to see it ended, since a
            # cancel sent in the step that stops the node can be dropped
            if not self.canceling:
                self.canceling = True
                self.cancel_order(order.client_order_id)
            return
        self._finish("accepted, then canceled" if self.canceling else f"ended {order.status.name}")


def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        strategy_id=seam.ADOPTER,
        order_id_tag=custody.ORDER_ID_TAG,
        external_order_instrument_ids=[AAPL],
    )


def run(
    strategy: Strategy,
    seconds: float,
    driver: Callable[[], Awaitable[None]] | None = None,
    position_check_secs: float = seam.POSITION_CHECK_SECS,
    adapter: dict[str, Any] | None = None,
) -> None:
    """Run one execution-only paper node, as the seam test builds it, with the adapter settings given."""
    node = LiveNode.build(
        "CANON-SELL-TO-CLOSE",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=paper_node.TRADER_ID,
            timeout_connection_secs=paper_node.TIMEOUT_CONNECTION_SECS,
            timeout_reconciliation_secs=20,
            timeout_disconnection_secs=paper_node.TIMEOUT_DISCONNECTION_SECS,
            delay_post_stop_secs=0,
            exec_engine=LiveExecutionEngineConfig(
                reconciliation=True,
                reconciliation_instrument_ids=[str(AAPL)],
                reconciliation_lookback_mins=paper_node.RECONCILIATION_LOOKBACK_MINS,
                open_check_interval_secs=seam.OPEN_CHECK_SECS,
                position_check_interval_secs=position_check_secs,
                position_check_lookback_mins=seam.FILL_LOOKBACK_MINS,
            ),
            exec_clients={
                "ALPACA": AlpacaExecutionClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(AAPL)]),
                    **(adapter or {}),
                ),
            },
        ),
        exec_factories={"ALPACA": AlpacaExecutionClientFactory},
    )
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


def intent_at_alpaca(trading: Any, client_order_id: Any) -> str:
    """The `position_intent` Alpaca holds for one of our orders, found by our client order ID."""
    order = trading.get_order_by_client_id(client_order_id.value)
    intent = order.position_intent
    return "none" if intent is None else str(getattr(intent, "value", intent))


def test_paper_a_sell_to_close_reaches_alpaca_through_the_adapter() -> None:
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    extended = part != "regular"
    params = {"extended_hours": True} if extended else None
    seen: dict[str, tuple[str, str]] = {}
    with seam.left_as_found(part) as cleaned:
        price = seam.resting_price(data, part)
        flat = FlatSeller(strategy_config())
        flat.sell_price, flat.params = price, params
        run(flat, 90, adapter=REDUCE_ONLY)
        filled_at = seam.buy_foreign(trading, data, part)
        time.sleep(max(0.0, seam.SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
        for name, adapter, reduce_only in (
            ("plain sell, setting off", {}, False),
            ("plain sell, setting on", {"sell_to_close_on_long_exits": True}, False),
            ("reduce-only sell", REDUCE_ONLY, True),
        ):
            closer = Closer(strategy_config())
            closer.sell_price, closer.params = seam.resting_price(data, part), params
            closer.reduce_only = reduce_only
            closer.limit_ns = int(60e9)
            run(closer, 120, adapter=adapter)
            intent = intent_at_alpaca(trading, closer.own[-1]) if closer.own else "no order sent"
            seen[name] = (closer.outcome, intent)
            seam.report(part, name, closer, intent_at_alpaca=intent)
        still_held = seam.held(trading)
    print(f"\n[{part}] a reduce-only SELL while flat: outcome {flat.outcome!r}")
    for reason in flat.refusals:
        print(f"  refused: {reason}")
    print(f"[{part}] intents Alpaca holds: {seen}")
    assert flat.outcome.startswith("ended "), flat.outcome
    assert flat.refusals, "a reduce-only SELL while flat was not refused"
    # The field is what was refused: without it this account's long-only 403 would answer, and a
    # denial before transport would name neither. Alpaca's words are the proof that it was sent.
    assert any("position intent mismatch" in reason for reason in flat.refusals), flat.refusals
    assert all(outcome == "done" for outcome, _ in seen.values()), seen
    assert still_held == Decimal(1), still_held
    assert (cleaned["orders"], cleaned["positions"]) == (0, 1), cleaned


LATE_CHECK_SECS = 120.0
"""A position check this far apart cannot tell the node of the outside sale before the strategy sells."""


def sell_after_the_holding_changed(
    trading: Any, data: Any, part: str, adapter: dict[str, Any] | None = None
) -> Closer:
    """Adopt a foreign long, sell it at Alpaca outside the node, and only then let the strategy place
    its plain sell for the share its node still believes it holds."""
    filled_at = seam.buy_foreign(trading, data, part)
    time.sleep(max(0.0, seam.SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
    closer = Closer(strategy_config())
    closer.sell_price = seam.resting_price(data, part)
    closer.params = {"extended_hours": True} if part != "regular" else None
    closer.hold = True
    closer.limit_ns = int(150e9)

    async def driver() -> None:
        while "adopted" not in closer.times:
            await asyncio.sleep(0.2)
        await asyncio.to_thread(seam.close_held, trading, data, part)
        closer.seen["held at Alpaca when our sell was sent"] = str(seam.held(trading))
        closer.hold = False

    run(closer, 240, driver, position_check_secs=LATE_CHECK_SECS, adapter=adapter)
    return closer


def test_paper_a_plain_sell_after_the_holding_changed_is_refused_by_its_intent() -> None:
    """The lead's case for every closing sell carrying the intent: the share was sold outside the node
    and the node has not heard. With `sell_to_close_on_long_exits` the plain sell carries
    `sell_to_close` from the node's cached long, and Alpaca refuses it by the intent. Without the
    setting the same sell has no intent, and on this account only its long-only setting refuses it."""
    trading, data = seam.paper_trading(), seam.paper_data()
    if not seam.long_only(trading):
        pytest.skip("the paper account allows shorting: mise run paper:long-only tests on")
    part = seam.open_part(trading)
    with seam.left_as_found(part) as cleaned:
        marked = sell_after_the_holding_changed(
            trading, data, part, {"sell_to_close_on_long_exits": True}
        )
        seam.report(part, "holding changed, setting on", marked)
        plain = sell_after_the_holding_changed(trading, data, part)
        seam.report(part, "holding changed, setting off", plain)
    for closer in (marked, plain):
        assert closer.seen.get("held at Alpaca when our sell was sent") == "0", closer.seen
        assert closer.outcome == "the protective sell ended REJECTED", closer.outcome
    assert "position intent mismatch" in marked.refusals[-1][1], marked.refusals
    assert "position intent mismatch" not in plain.refusals[-1][1], plain.refusals
    assert (cleaned["orders"], cleaned["positions"]) == (0, 0), cleaned


STOP_OFFSET = Decimal(50)
"""A sell stop this far under the bid rests and does not trigger."""
REGULAR_KINDS = ("limit", "stop_market", "stop_limit")
"""The order types an execution-only node can send. A market order is not among them: NautilusTrader's
risk engine denies one while the cache holds no price for the instrument (`MARKET_PRICE_UNAVAILABLE`,
`crates/risk/src/engine/mod.rs` at the pin), and this node has no data client. The test shows that denial
by name; `sell_to_close` on a market order needs a node with a feed."""


def test_paper_a_regular_session_order_types_carry_sell_to_close() -> None:
    """In the regular session, the limit, the stop and the stop-limit are shown both ways with
    `reduce_only_sells`: while flat, the reduce-only SELL is refused by Alpaca's intent check; on an
    adopted long, each works. The market sell is denied by the node's own risk engine for want of a
    price and never reaches Alpaca, which is recorded and is no proof of the field on a market order."""
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    if part != "regular":
        pytest.skip(f"the regular session's order types are run in the regular session, not {part}")
    flat: dict[str, tuple[str, str]] = {}
    long: dict[str, str] = {}
    with seam.left_as_found(part) as cleaned:
        bid, _ = session_window.close_limit(data, seam.SYMBOL, "sell", part)
        stop = str((bid - STOP_OFFSET).quantize(Decimal("0.01")))
        for kind in REGULAR_KINDS:
            seller = FlatSeller(strategy_config())
            seller.sell_price, seller.stop_price, seller.kind = (
                seam.resting_price(data, part),
                stop,
                kind,
            )
            run(seller, 90, adapter=REDUCE_ONLY)
            flat[kind] = (seller.outcome, seller.refusals[-1] if seller.refusals else "")
        seller = FlatSeller(strategy_config())
        seller.kind = "market"
        run(seller, 90, adapter=REDUCE_ONLY)
        market = (seller.outcome, seller.refusals[-1] if seller.refusals else "")
        filled_at = seam.buy_foreign(trading, data, part)
        time.sleep(max(0.0, seam.SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
        for kind in REGULAR_KINDS:
            closer = Closer(strategy_config())
            closer.sell_price, closer.stop_price, closer.kind = (
                seam.resting_price(data, part),
                stop,
                kind,
            )
            closer.reduce_only = True
            closer.limit_ns = int(60e9)
            run(closer, 120, adapter=REDUCE_ONLY)
            long[kind] = closer.outcome
            seam.report(part, f"reduce-only {kind} sell on the adopted long", closer)
        seam.close_held(trading, data, part)  # the test's own close of the share it bought, if held
        still_held = seam.held(trading)
    print(f"\n[{part}] reduce-only market SELL while flat, on a node with no price: {market}")
    print(f"\n[{part}] reduce-only SELL while flat, by order type: {flat}")
    print(f"[{part}] reduce-only SELL on the adopted long, by order type: {long}")
    for kind, (outcome, reason) in flat.items():
        assert outcome == "ended REJECTED", (kind, outcome, reason)
        assert "position intent mismatch" in reason, (kind, reason)
    assert market[0] == "ended DENIED" and "MARKET_PRICE_UNAVAILABLE" in market[1], market
    assert long == {"limit": "done", "stop_market": "done", "stop_limit": "done"}
    assert still_held == 0, still_held
    assert (cleaned["orders"], cleaned["positions"]) == (0, 0), cleaned
