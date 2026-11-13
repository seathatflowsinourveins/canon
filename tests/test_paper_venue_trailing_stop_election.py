"""Paper proof, in the regular session, of what happens when Alpaca elects a trailing stop the adapter
placed: a stop one cent under the mark is left to trigger, and the test reads how Alpaca books the sale
and whether the fill reaches the node's order (ADR 0129, the gap "the venue ID and order type a fill
arrives under after the election", and its condition (b)).

Contract: Alpaca's trailing stop (`https://docs.alpaca.markets/us/docs/orders-at-alpaca.md`, "Trailing
Stop Orders": `trail_price`, "a dollar value away from the highest water mark", and "Once the stop price
is triggered, the order turns into a market order"; "Order Handling Standards": elected "on the
consolidated print" by a trade at or under the stop that "is not outside of the NBBO") and
NautilusTrader's `TRAILING_STOP_MARKET` order with a `PRICE` offset, which the adapter sends as
`trail_price` when `submit_trailing_stops` is on. The page does not say under which order the fill
arrives, or what position intent that order carries. Both are read here.

What is shown, through the proof's execution-only node (`tests/test_paper_venue_trailing_stop.py`): on a
long of one share adopted at start, a DAY trailing stop of one cent is accepted and left alone. Required:
Alpaca fills one share, the node's own order ends `FILLED` with one `OrderFilled`, and the account holds
none of the share afterwards. A fill Alpaca lists that the node's order did not get is condition (b),
and is the first thing the verdict names (`verdict`). Recorded, not required: each sell Alpaca lists for
the symbol since the test began, with its type, status, quantities, position intent, whether it replaces
or is replaced by another order, and whether it carries the stop's own client order ID. What is printed
(`record`) carries no venue order ID, no client order ID, no price and no time; it is printed whatever
ends the run, a failure of the node included.

One cent is the tightest trail a whole-cent price has, and Alpaca documents no smallest trail: a refusal
ends the walk by name. A stop not elected in `ELECT_SECS` is canceled and the test fails saying nothing
was shown; one that fills while that cancel is on its way is an election all the same. The sale is a
market order, the first this repository's tests have left Alpaca to make.

Run under `mise run paper:test`, selected with `-k`, which holds the account's lock. It skips outside
the regular session and on an account that allows shorting (ADR 0129, "Where it may be used"). It places
nothing while the account holds an open order or a position in the symbol, and cancels and closes what it
opened in a `finally` (`tests/test_paper_protect_seam.py`, `left_as_found`). Its parts are walked offline
in `tests/test_trailing_stop_election_offline.py`.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType

from canon_trading import custody
from tests import test_paper_protect_seam as seam
from tests import test_paper_venue_trailing_stop as paper

pytestmark = seam.pytestmark

AAPL = seam.AAPL
ONE = 1
"""The election case holds one share: the stop is left to sell it."""
TIGHT = Decimal("0.01")
"""One cent under the mark: the first print a cent under the highest since the order elects it."""
ELECT_SECS = 120.0
"""How long the stop is left to trigger before the walk cancels it. UNCALIBRATED."""
SETTLED_SECS = 20.0
"""How long the read after the node waits for Alpaca's listing to show a filled sell: the listing lags
an order's events (`paper.READ_SECS`). UNCALIBRATED."""
SETTLED_STEP = 2.0
"""The wait between those reads: ten at most, where the node's own steps are half a second."""
WHAT = "a tight trailing stop left to trigger"
LISTED = 50
"""The most orders one read asks for: the test's own are a buy and a sell or two."""


class Elected(paper.Trailer):
    """The proof's trailer with a trail of one cent on a long of one share, left alone: the walk ends
    when the node's order ends, and cancels it if `ELECT_SECS` pass first. Nothing is replaced."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.cancel_refusals: list[str] = []

    def on_order_event(self, event: Any) -> None:
        paper.Trailer.on_order_event(self, event)
        if type(event).__name__ == "OrderCancelRejected":
            self.cancel_refusals.append(str(event.reason))

    def _sell(self, quantity: Any) -> Any:
        instrument = self.cache.instrument(AAPL)
        assert instrument is not None, f"{AAPL} is not in the cache"
        order = self.order_factory.trailing_stop_market(
            AAPL,
            OrderSide.SELL,
            instrument.make_qty(quantity),
            TIGHT,
            trailing_offset_type=TrailingOffsetType.PRICE,
            time_in_force=TimeInForce.DAY,
        )
        self.own.append(order.client_order_id)
        self.submit_order(order)
        return order.client_order_id

    def _ended(self, order: Any) -> None:
        self._mark("stop ended")
        self.seen["the node's order ended"] = order.status.name
        self.seen["filled at the node"] = str(order.filled_qty)

    def _step(self, event: Any) -> None:
        if self.phase in ("done", "waiting for the long", "placing"):
            # named, not `super()`: the offline cases walk these steps on a stand-in for the strategy
            seam.Adopter._step(self, event)
            return
        order = self.cache.order(self.own[-1])
        # the order's end is read before the walk's own limit: a fill that reached the node as the
        # limit ran out is the walk's end, and a walk that timed out past it would leave the verdict
        # saying the node's order missed a fill it got
        if not (order is not None and order.is_closed) and self._timed_out():
            return
        if order is None or not (order.is_open or order.is_closed):
            return
        if self.phase == "waiting for the sell to work":
            # a stop this tight can be elected before the node sees it working: either is a start
            self._mark("stop working")
            self.phase = "waiting for the election"
        if not order.is_closed:
            waited = time.time_ns() - self.times["stop working"]
            if self.phase == "waiting for the election" and waited > ELECT_SECS * 1e9:
                self.seen["elected"] = "no: canceled after the wait"
                self.cancel_order(order.client_order_id)
                self._mark("cancel sent")
                self.phase = "waiting for the sell's cancel"
            elif self.phase == "waiting for the sell's cancel" and self.cancel_refusals:
                # the stop is still open and its cancel was refused: end now and say so, and the
                # test's cleaning cancels what is left
                self._finish("the stop's cancel was refused")
            return
        self._ended(order)
        if order.status.name == "FILLED":
            if "elected" in self.seen:
                self.seen["elected"] = "yes: while the walk's cancel was on its way"
            self._finish("done")
        elif self.phase == "waiting for the sell's cancel":
            self._finish("done")  # ended by the walk's own cancel: `seen` says nothing was elected
        else:
            self._finish(f"the trailing stop ended {order.status.name} with no election")


def elected(limit_secs: float) -> Elected:
    strategy = Elected(
        StrategyConfig(
            strategy_id=seam.ADOPTER,
            order_id_tag=custody.ORDER_ID_TAG,
            external_order_instrument_ids=[AAPL],
        )
    )
    strategy.limit_ns = int(limit_secs * 1e9)
    return strategy


def sells_since(trading: Any, since: datetime, own: str) -> list[dict[str, str]]:
    """Every sell of the symbol Alpaca lists since `since`, oldest first, as what a reader needs to
    tell how an election is booked: no venue order ID, no client order ID, no price and no time."""
    orders = trading.get_orders(
        GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=[seam.SYMBOL],
            after=since,
            nested=True,
            limit=LISTED,
        )
    )
    found = []
    for order in sorted(orders, key=lambda o: o.created_at):
        if paper.text(order.side) != "sell":
            continue
        found.append(
            {
                "which": "the stop's own client order ID"
                if order.client_order_id == own
                else f"another client order ID ({custody.lineage(order.client_order_id)})",
                "type": paper.text(order.type),
                "status": paper.text(order.status),
                "quantity": paper.text(order.qty),
                "filled": paper.text(order.filled_qty),
                "position intent": paper.text(order.position_intent),
                "replaces another order": "no" if order.replaces is None else "yes",
                "replaced by another order": "no" if order.replaced_by is None else "yes",
                "legs": str(len(order.legs or [])),
            }
        )
    return found


def settled_sells(
    trading: Any, since: datetime, own: str, seconds: float = SETTLED_SECS
) -> list[dict[str, str]]:
    """The sells, read until one shows filled or `seconds` have passed. What was last seen is returned
    either way; a read that could not be made is returned as `[{"failed": why}]`, never raised."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            sells = sells_since(trading, since, own)
        except paper.READ_FAILURES as e:
            # the kind of failure only: its text can carry the request, with the time it asked from
            sells = [{"failed": type(e).__name__}]
        if time.monotonic() >= deadline or any(s.get("status") == "filled" for s in sells):
            return sells
        time.sleep(SETTLED_STEP)


def record(part: str, strategy: Any, sells: list[dict[str, str]], still_held: object) -> list[str]:
    """What the run prints: the walk's outcome, what the node's order did, Alpaca's sells and what is
    held afterwards. Nothing the seam keeps that names an order or a price is in it."""
    lines = [f"[{part}] {WHAT}: outcome {strategy.outcome!r}"]
    shown = {name: strategy.seen[name] for name in SHOWN if name in strategy.seen}
    if "elected" not in shown and shown.get("the node's order ended") == "FILLED":
        shown["elected"] = "yes"
    lines += [f"  {name}: {value}" for name, value in shown.items()]
    lines += [f"  sell {n} at Alpaca: {sell}" for n, sell in enumerate(sells, 1)]
    lines.append(f"  held after: {still_held}")
    lines.append(f"  events: {[name for name, _, _ in strategy.events]}")
    lines += [f"  refused: {reason}" for _, reason in strategy.refusals]
    lines += [f"  cancel refused: {reason}" for reason in strategy.cancel_refusals]
    return lines


SHOWN = ("quantity", "the node's order ended", "filled at the node", "elected")
"""What of the strategy's own account is printed: no client order ID and no average price."""


def verdict(
    strategy: Any, sells: list[dict[str, str]], still_held: Decimal, cleaned: dict[str, int]
) -> list[str]:
    """What is wrong with a finished run, each by name, or nothing. A fill Alpaca lists that the
    node's order did not get comes first: ADR 0129's condition (b), whatever the walk says of itself.
    How Alpaca lists the sale (one row or two, under which order) is recorded and never judged."""
    at_node = strategy.seen.get("the node's order ended") == "FILLED"
    at_alpaca = any(sell.get("status") == "filled" for sell in sells)
    failed = [sell["failed"] for sell in sells if "failed" in sell]
    own = strategy.own[-1].value if strategy.own else ""
    fills = [name for name, order_id, _ in strategy.events if order_id == own].count("OrderFilled")
    problems = []
    if at_alpaca and not at_node:
        problems.append(
            "Alpaca filled the sale and the node's order did not get it: ADR 0129, condition (b)"
        )
    elif not at_node and strategy.outcome == "done":
        problems.append(f"no election in {ELECT_SECS:.0f} s: nothing is shown")
    elif not at_node:
        problems.append(f"the walk ended: {strategy.outcome}")
    if failed:
        problems.append(f"how Alpaca booked the sale could not be read: {failed[0]}")
    if not at_node:
        return problems
    if not at_alpaca and not failed:
        problems.append("Alpaca lists no filled sell for a stop the node saw filled")
    if fills != 1:
        problems.append(f"{fills} OrderFilled events for the node's order, not one")
    if still_held != 0:
        problems.append(f"{still_held} share(s) still held after the node's order filled")
    if cleaned["orders"] != 0:
        problems.append(f"the cleaning found {cleaned['orders']} open order(s)")
    return problems


def test_paper_a_tight_trailing_stop_is_left_to_trigger_and_its_fill_reaches_the_nodes_order() -> (
    None
):
    trading, data = seam.paper_trading(), seam.paper_data()
    part = seam.open_part(trading)
    if part != "regular":
        pytest.skip(f"Alpaca's trailing stop is a regular-session order; the open part is {part}")
    if not seam.long_only(trading):
        pytest.skip("the venue trailing stop is for a long-only account until its proof (ADR 0129)")
    since = datetime.now(UTC)
    strategy = elected(ELECT_SECS + 90)
    sells: list[dict[str, str]] = [{"failed": "no read was made"}]
    still_held: Decimal | None = None
    try:
        with seam.left_as_found(part) as cleaned:
            filled_at = seam.buy_foreign(trading, data, part)
            time.sleep(max(0.0, seam.SETTLE_SECS - (datetime.now(UTC) - filled_at).total_seconds()))
            try:
                paper.run(strategy, ELECT_SECS + 150, trading)
            finally:
                # read whatever ended the node: the sale may have happened all the same
                own = strategy.own[-1].value if strategy.own else ""
                sells = settled_sells(trading, since, own)
            still_held = seam.held(trading)
    finally:
        print("\n" + "\n".join(record(part, strategy, sells, still_held)))
    assert still_held is not None
    problems = verdict(strategy, sells, still_held, cleaned)
    assert not problems, problems
