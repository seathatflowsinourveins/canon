"""Pure equity sizing through NautilusTrader's FixedRiskSizer.calculate contract.

Verified at v2.0.0rc8, nautilus-trader@d53849b216e2, read 2026-11-12:

* Signature: python/nautilus_trader/risk/__init__.pyi:16-29. Planned loss is supplied as
  equity with risk=1: crates/risk/src/sizing.rs:132-151 multiplies those amounts, and
  :74-86 divides by the absolute entry/stop distance and instrument multiplier.
* Money.from_decimal_exact preserves sub-cent planned loss without a rounded risk ratio
  (crates/model/src/types/money.rs:588-598). Price.from_decimal keeps the stop's decimal
  scale or refuses unrepresentable precision (crates/model/src/types/price.rs:547-560).
* The only caller-supplied quantity bound upstream accepts is hard_limit (:88-90 of
  sizing.rs). Convert the quote-currency budget to whole shares and combine it with the
  explicit flow cap there, before upstream batches down (:92-103). Equity's size increment
  and multiplier are both one (crates/model/src/instruments/equity.rs:275-288).
* unit_batch_size=1 is upstream's default (crates/risk/src/python/sizing.rs:171-180).
  Pass it explicitly; retain commission_rate=0, exchange_rate=1 and units=1. Upstream's
  instrument maximum still applies (crates/risk/src/sizing.rs:106-116).
* The stop sized against is the one Alpaca is sent: price_step.stop_trigger_price rounds the
  trigger down onto Alpaca's step (ADR 0030), and the entry must already be on that step.

The fractional distance ceiling is ADR 0096 amendment 11's registered 0.10; its scale_size
row is slot dollars * cap as planned loss. That policy is UNCALIBRATED, lead-sourced.
No strategy, account lookup, clock, order submission or I/O belongs here.
"""

from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Context
from decimal import Decimal
from decimal import InvalidOperation
from decimal import Overflow
from decimal import localcontext

from nautilus_trader.model import HIGH_PRECISION
from nautilus_trader.model import Equity
from nautilus_trader.model import Money
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.risk import FixedRiskSizer

from canon_trading.price_step import stop_trigger_price
from canon_trading.price_step import tick

DISTANCE_CEILING = Decimal("0.10")
# Upstream's largest Money amount and Quantity, by build (crates/model/src/types/money.rs:135-141,
# quantity.rs:118-124); the wheel reports which through HIGH_PRECISION.
MONEY_MAX = Decimal(17_014_118_346_046) if HIGH_PRECISION else Decimal(9_223_372_036)
QUANTITY_MAX = Decimal(34_028_236_692_093) if HIGH_PRECISION else Decimal(18_446_744_073)
# Python's default precision, owned here so a caller's context cannot change a stop. Each rounding
# below is directed toward the larger loss or the smaller size; a result too large traps.
_LOSS = Context(prec=28, rounding=ROUND_CEILING, traps=[InvalidOperation, Overflow])
_STOP = Context(prec=28, rounding=ROUND_FLOOR, traps=[InvalidOperation, Overflow])


def size_for_loss(
    *,
    instrument: Equity,
    entry: Price,
    distance: Decimal | None,
    planned_loss: Decimal,
    flow_cap: Decimal,
    budget: Decimal,
) -> Quantity:
    """Size a long equity entry, flooring to whole shares and refusing by ValueError name.

    distance is the positive stop distance as a fraction of entry. The stop sized against is the
    one Alpaca is sent: entry less that fraction, rounded down onto its step by
    price_step.stop_trigger_price, so shares times entry less that stop never exceed planned_loss.
    planned_loss and budget are amounts in the instrument's quote currency; flow_cap is shares.
    All three amounts are explicit: callers resolve their own account, slot and flow limits.
    Loss at the hypothetical stop excludes commissions, slippage and any later exit chase.

    An instrument that is not an Equity raises TypeError instrument_invalid. Missing, nonfinite
    or nonpositive distance refuses as distance_unknown; one strictly above 0.10, or a stop that
    lands past it or at zero once stepped down, refuses as distance_over_ceiling. An entry off
    Alpaca's step refuses as entry_off_step. A bound or upstream batch that leaves no whole share
    refuses as size_zero. Other invalid inputs, a planned loss over Money's maximum among them,
    are named <input>_invalid. A planned loss Money cannot hold at its native precision refuses
    as planned_loss_precision; the caller rounds its own amount. A cap past upstream's
    QUANTITY_MAX does not bind. Arithmetic runs in this module's own 28-digit context, never the
    caller's, and each rounding goes toward the larger loss or the smaller size.
    Other upstream errors propagate without a fallback.
    """
    if not isinstance(instrument, Equity):
        raise TypeError("instrument_invalid")
    if distance is None or not distance.is_finite() or distance <= 0:
        raise ValueError("distance_unknown")
    if distance > DISTANCE_CEILING:
        raise ValueError("distance_over_ceiling")

    # Every step below runs in this module's context, never the caller's: floor by default, and
    # ceiling where a loss or a cost is rounded.
    with localcontext(_STOP):
        price = entry.as_decimal()
        if not price.is_finite() or price <= 0:
            raise ValueError("entry_invalid")
        if not planned_loss.is_finite() or planned_loss <= 0:
            raise ValueError("planned_loss_invalid")
        for name, amount in (("flow_cap", flow_cap), ("budget", budget)):
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"{name}_invalid")

        if price != price.quantize(tick(price)):
            raise ValueError("entry_off_step")
        if planned_loss > MONEY_MAX:
            raise ValueError("planned_loss_invalid")

        # The stop Alpaca is sent is the trigger rounded down onto its step (price_step, ADR 0030), so a
        # share loses entry less that stop, never the unstepped fraction of entry. A distance computed by
        # division has 28 digits: its loss rounds up and the trigger down, a stop no higher than exact.
        with localcontext(_LOSS):
            unstepped = price * distance
        stop = stop_trigger_price(price - unstepped)
        loss_per_share = price - stop  # both on a 4-decimal step: exact
        if stop <= 0 or loss_per_share > price * DISTANCE_CEILING:
            raise ValueError("distance_over_ceiling")

        # hard_limit is positive upstream (sizing.rs:61-63); refuse an empty cap before calling it.
        # A cap past QUANTITY_MAX cannot bind, and the budget is divided only when it binds, so the
        # quotient is under a cap that fits.
        hard_limit = min(flow_cap, QUANTITY_MAX)
        with localcontext(_LOSS):
            budget_binds = budget < hard_limit * price
        if budget_binds:
            hard_limit = min(hard_limit, budget // price)
        if hard_limit < 1 or planned_loss < loss_per_share:
            raise ValueError("size_zero")

        stop_loss = Price.from_decimal(stop)
        try:
            equity = Money.from_decimal_exact(planned_loss, instrument.quote_currency)
        except ValueError as e:
            raise ValueError("planned_loss_precision") from e

    quantity = FixedRiskSizer(instrument).calculate(
        entry=entry,
        stop_loss=stop_loss,
        equity=equity,
        risk=Decimal(1),
        hard_limit=hard_limit,
        unit_batch_size=Decimal(1),
    )
    # A sub-share positive risk size batches to zero (sizing.rs:98-103), never a filled-in size.
    if not quantity.is_positive():
        raise ValueError("size_zero")
    return quantity
