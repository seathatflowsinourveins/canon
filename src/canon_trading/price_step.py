"""Alpaca's US equity price step, as upstream's tiered tick scheme: the function every sender is to put
its price on Alpaca's step through. Until each has moved to it, others still quantize on their own; ADR
0030 lists them.

Alpaca takes at most 2 decimals for a price at or above $1.00 and 4 below ("Orders at Alpaca",
"Sub-penny increments", https://docs.alpaca.markets/us/docs/orders-at-alpaca.md). That is stated as
NautilusTrader's `TieredTickScheme` (v2.0.0rc8, `nautilus-trader@d53849b216e2`,
`crates/model/src/instruments/tick_scheme.rs`), read through `next_ask_price` for a buy, rounding up,
and `next_bid_price` for a sell, rounding down (`impl TickSchemeRule for TieredTickScheme`, `:525-555`;
the `TickScheme` dispatch to it, `:595-600`). Moved from `mover.py`; every sender is to price
through it (ADR 0030).

The scheme ends at its last expanded tick. Its last tier may be given an infinite stop (`:347-348`),
but every tier is expanded into a finite list, at most `max_ticks_per_tier` ticks even then
(`:425-457`), and its max price is the last of them (`:492-495`). So above 4,999.99 (`TOP`) the
sell-side prices computed here (`exit_price`, `stop_trigger_price`, `mover.chase_floor`) go on
Alpaca's cent through `down_on_step` or `up_on_step`, and a buy is refused by name (`entry_price`).
The emulated trailing sell's released limit is not priced here: upstream computes it at the
instrument's 0.0001 (`mover.offsets` records that gap)."""

from decimal import ROUND_CEILING
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


TOP = PRICES.max_price.as_decimal()
"""The scheme's last expanded tick, 4,999.99. Above it the scheme cannot round (see the module text)."""


def entry_price(ask: Decimal, ticks: int) -> Decimal:
    """A buy limit `ticks` ticks through the ask: the ask, or the next price above it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_ask_price`). Past the scheme's last tick the buy is refused by
    a name that says so (the lead's ruling (ii), 2026-11-12)."""
    price = PRICES.next_ask_price(float(ask) - NUDGE, ticks)
    if price is not None:
        return price.as_decimal()
    if not ask.is_finite():
        raise ValueError(f"no limit price {ticks} ticks above {ask} in {PRICES.name}")
    # past the last tick (`tick_scheme.rs:566-575`)
    raise ValueError(
        f"no limit price {ticks} ticks above {ask}: above the top tier of {PRICES.name}, {TOP}"
    )


def _finite(price: Decimal, ticks: int = 0) -> None:
    """The scheme's binding refuses a negative offset (`crates/model/src/python/instruments/
    tick_scheme.rs:235-241`); the same refusal, and one for a price that is not a number, here."""
    if ticks < 0:
        raise ValueError(f"n must be >= 0, was {ticks}")
    if not price.is_finite():
        raise ValueError(f"no price on Alpaca's step for {price}")


def down_on_step(price: Decimal) -> Decimal:
    """`price` rounded down to Alpaca's step: `next_bid_price` at zero ticks inside the scheme's
    expansion, after an exact floor to the finest step, so the nudge cannot lift a finer Decimal onto
    the tick above it. Past the last tick, where `next_bid_price` answers that tick for any value
    (`tick_scheme.rs:550-552`), it is floored to Alpaca's cent; at or below the lowest tick it is the
    exact floor, never raised to that tick."""
    _finite(price)
    exact = price.quantize(SUB_DOLLAR, rounding=ROUND_FLOOR)
    if exact > TOP:
        return exact.quantize(CENT, rounding=ROUND_FLOOR).quantize(SUB_DOLLAR)
    if exact <= PRICES.min_price.as_decimal():
        return exact
    found = PRICES.next_bid_price(float(exact) + NUDGE, 0)
    if found is None:
        raise ValueError(f"no price at or under {price} in {PRICES.name}")
    return found.as_decimal()


def up_on_step(price: Decimal) -> Decimal:
    """`price` rounded up to Alpaca's step: `next_ask_price` at zero ticks inside the scheme's
    expansion, as `mover.chase_floor` always priced; past the last tick, Alpaca's cent above it. For a
    bound on a sale (`chase_floor`), not a buy's limit, which `entry_price` refuses there."""
    _finite(price)
    if price > TOP:
        return price.quantize(CENT, rounding=ROUND_CEILING).quantize(SUB_DOLLAR)
    # up onto the finest step exactly first, so the nudge below cannot take a finer price under
    # itself (code-review-high-succ2/3), as `down_on_step` takes its floor exactly first
    price = price.quantize(SUB_DOLLAR, rounding=ROUND_CEILING)
    found = PRICES.next_ask_price(float(price) - NUDGE, 0)
    if found is None:
        raise ValueError(f"no price at or over {price} in {PRICES.name}")
    return found.as_decimal()


def exit_price(bid: Decimal, ticks: int, floor: Decimal | None) -> Decimal:
    """A sell limit `ticks` ticks through the bid: the bid, or the next price below it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_bid_price`), the lowest tick at least; never below `floor`.

    Past the scheme's last tick the bid goes on Alpaca's cent (`down_on_step`) and `ticks` cents under
    it; an offset that runs back into the expansion goes on from its last tick through `next_bid_price`
    (the lead's ruling (ii), 2026-11-12)."""
    _finite(bid, ticks)
    at = down_on_step(bid)
    if at > TOP:
        above = int((at - TOP) / CENT)
        if ticks <= above:
            price = at - ticks * CENT
            return price if floor is None else max(price, floor)
        found = PRICES.next_bid_price(float(TOP) + NUDGE, ticks - above)
    else:
        found = PRICES.next_bid_price(float(bid) + NUDGE, ticks)
    price = PRICES.min_price.as_decimal() if found is None else found.as_decimal()
    return price if floor is None else max(price, floor)


def stop_trigger_price(trigger: Decimal) -> Decimal:
    """A long's stop trigger rounded down to Alpaca's step (`down_on_step`)."""
    return down_on_step(trigger)
