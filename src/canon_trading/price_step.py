"""Alpaca's US equity price step, as upstream's tiered tick scheme: the function every sender is to put
its price on Alpaca's step through. Until each has moved to it, others still quantize on their own; ADR
0030 lists them.

Alpaca takes at most 2 decimals for a price at or above $1.00 and 4 below ("Orders at Alpaca",
"Sub-penny increments", https://docs.alpaca.markets/us/docs/orders-at-alpaca.md). That is stated as
NautilusTrader's `TieredTickScheme` (v2.0.0rc8, `nautilus-trader@d53849b216e2`,
`crates/model/src/instruments/tick_scheme.rs`), read through `next_ask_price` for a buy, rounding up,
and `next_bid_price` for a sell, rounding down (`impl TickSchemeRule for TieredTickScheme`, `:525-555`;
the `TickScheme` dispatch to it, `:595-600`). Moved unchanged from `mover.py`; every sender
is to price through it (ADR 0030).
"""

from decimal import ROUND_FLOOR
from decimal import Decimal

from nautilus_trader.model import TieredTickScheme

CENT = Decimal("0.01")
SUB_DOLLAR = Decimal("0.0001")


def tick(price: Decimal) -> Decimal:
    """Alpaca's smallest limit-price step at `price`: a cent at or above $1.00, a hundredth of one below."""
    return CENT if price >= 1 else SUB_DOLLAR


PRICES = TieredTickScheme(
    "ALPACA_US_EQUITY_LIMIT",
    [(0.0001, 1.0, 0.0001), (1.0, 5000.0, 0.01)],
    4,
    1_000_000,
)
"""Alpaca's limit prices as upstream's tiered tick scheme (`nautilus_trader.model.TieredTickScheme`,
`crates/model/src/instruments/tick_scheme.rs`): four decimals under a dollar, two from a dollar up, to
5,000 dollars. Every limit price the Mover computes is one of its ticks, so a price that crosses a
dollar lands on the other band's step. The adapter's instrument has one increment, the finer, and no
scheme of its own (`canon_alpaca.constants.PRICE_INCREMENT`)."""


NUDGE = 0.00001
"""A tenth of the finest tick. The scheme takes a float, and the float nearest a four-decimal price can
sit a hair under it (10.29 is 10.28999...), which the scheme would read as between two ticks. A price
is moved this much toward its own side first: up for a bid, which is then floored, down for an ask."""


def entry_price(ask: Decimal, ticks: int) -> Decimal:
    """A buy limit `ticks` ticks through the ask: the ask, or the next price above it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_ask_price`)."""
    price = PRICES.next_ask_price(float(ask) - NUDGE, ticks)
    if price is None:
        raise ValueError(f"no limit price {ticks} ticks above {ask} in {PRICES.name}")
    return price.as_decimal()


def exit_price(bid: Decimal, ticks: int, floor: Decimal | None) -> Decimal:
    """A sell limit `ticks` ticks through the bid: the bid, or the next price below it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_bid_price`), the lowest tick at least; never below `floor`."""
    found = PRICES.next_bid_price(float(bid) + NUDGE, ticks)
    price = PRICES.min_price.as_decimal() if found is None else found.as_decimal()
    return price if floor is None else max(price, floor)


def stop_trigger_price(trigger: Decimal) -> Decimal:
    """A long's stop trigger rounded down to Alpaca's step: `next_bid_price` at zero ticks inside the
    scheme's expansion. The trigger is first floored to the finest step, so the nudge cannot lift a
    finer Decimal onto the tick above it. Outside the expansion it is floored on Alpaca's step there,
    never clamped to the scheme's last or lowest tick: a cent above `PRICES.max_price`, the finest step
    at or below `PRICES.min_price`."""
    exact = trigger.quantize(SUB_DOLLAR, rounding=ROUND_FLOOR)
    if exact > PRICES.max_price.as_decimal():
        return exact.quantize(CENT, rounding=ROUND_FLOOR)
    if exact <= PRICES.min_price.as_decimal():
        return exact
    found = PRICES.next_bid_price(float(exact) + NUDGE, 0)
    if found is None:
        raise ValueError(f"no stop trigger at or under {trigger} in {PRICES.name}")
    return found.as_decimal()
