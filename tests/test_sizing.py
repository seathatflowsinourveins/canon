"""Pure equity sizing over upstream FixedRiskSizer.calculate, with synthetic prices and amounts."""

from decimal import Decimal
from inspect import Parameter
from inspect import signature
from itertools import product
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Symbol
from nautilus_trader.risk import FixedRiskSizer


@pytest.fixture
def instrument() -> Equity:
    return Equity(
        instrument_id=InstrumentId.from_str("AAA.ALPACA"),
        raw_symbol=Symbol("AAA"),
        currency=Currency.from_str("USD"),
        price_precision=4,
        price_increment=Price.from_str("0.0001"),
        ts_event=0,
        ts_init=0,
    )


def test_basic_quantity_is_planned_loss_over_stop_distance(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(200)


def test_flow_cap_bounds_the_quantity_and_floors_whole_shares(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal("7.9"),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(7)


def test_budget_bounds_notional_without_rounding_up(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal("79.99"),
    )

    assert quantity.as_decimal() == Decimal(7)
    assert quantity.as_decimal() * Decimal(10) <= Decimal("79.99")


def test_distance_over_the_ceiling_is_refused_by_name(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^distance_over_ceiling$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.10000001"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_exactly_the_ceiling_is_accepted(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.10"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(100)


@pytest.mark.parametrize(
    "distance", [None, Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal("-0.01")]
)
def test_unknown_or_unusable_distance_is_refused_by_name(
    instrument: Equity, distance: Decimal | None
) -> None:
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^distance_unknown$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=distance,
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


@pytest.mark.parametrize("planned_loss", [Decimal("0.49"), Decimal("0.00000000000000000001")])
def test_a_planned_loss_under_one_shares_loss_is_refused_apart_from_size_zero(
    instrument: Equity, planned_loss: Decimal
) -> None:
    """Review finding 4 (/code-review high, S2): `size_zero` named both no whole share under the caps
    and a planned loss under one share's loss at the stop, so a count could not tell them apart."""
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^loss_under_one_share$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=planned_loss,
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


@pytest.mark.parametrize(
    ("planned_loss", "flow_cap", "budget"),
    [
        (Decimal(100), Decimal("0.99"), Decimal(10000)),
        (Decimal(100), Decimal(1000), Decimal("9.99")),
        (Decimal(100), Decimal(0), Decimal(10000)),
        (Decimal(100), Decimal(1000), Decimal(0)),
    ],
)
def test_size_that_leaves_no_whole_share_is_refused_by_name(
    instrument: Equity, planned_loss: Decimal, flow_cap: Decimal, budget: Decimal
) -> None:
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^size_zero$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=planned_loss,
            flow_cap=flow_cap,
            budget=budget,
        )


def test_scale_size_equivalence_grid_and_a_three_share_slot_at_six_percent_gives_one(
    instrument: Equity,
) -> None:
    from canon_trading.sizing import size_for_loss

    price = Decimal(10)
    increment = instrument.size_increment.as_decimal()
    slots = [Decimal(value) for value in ("1", "3", "7", "50", "1000")]
    caps = [Decimal(value) for value in ("0.005", "0.02", "0.03")]
    distances = [Decimal(value) for value in ("0.01", "0.02", "0.03", "0.05", "0.06", "0.10")]
    for slot_quantity, cap, distance in product(slots, caps, distances):
        slot_dollars = slot_quantity * price
        # Floor the entire ratio in increments, never a rounded cap / distance first.
        expected = min(slot_quantity, slot_quantity * cap // (distance * increment) * increment)
        if expected == 0:
            with pytest.raises(ValueError, match="^loss_under_one_share$"):
                size_for_loss(
                    instrument=instrument,
                    entry=Price.from_str("10.0000"),
                    distance=distance,
                    planned_loss=slot_dollars * cap,
                    flow_cap=slot_quantity,
                    budget=slot_dollars,
                )
        else:
            quantity = size_for_loss(
                instrument=instrument,
                entry=Price.from_str("10.0000"),
                distance=distance,
                planned_loss=slot_dollars * cap,
                flow_cap=slot_quantity,
                budget=slot_dollars,
            )
            assert quantity.as_decimal() == expected, (slot_quantity, cap, distance)

    assert size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.06"),
        planned_loss=Decimal(30) * Decimal("0.02"),
        flow_cap=Decimal(3),
        budget=Decimal(30),
    ).as_decimal() == Decimal(1)


def test_upstream_batch_default_is_one_and_one_is_passed_through(
    instrument: Equity, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canon_trading import sizing

    upstream = FixedRiskSizer(instrument)
    # A raw 1.5-share size must batch to 1, rather than round to 2 at instrument precision.
    default_quantity = upstream.calculate(
        entry=Price.from_str("10.0000"),
        stop_loss=Price.from_str("9.5000"),
        equity=Money.from_decimal_exact(Decimal("0.75"), instrument.quote_currency),
        risk=Decimal(1),
    )
    explicit_quantity = upstream.calculate(
        entry=Price.from_str("10.0000"),
        stop_loss=Price.from_str("9.5000"),
        equity=Money.from_decimal_exact(Decimal("0.75"), instrument.quote_currency),
        risk=Decimal(1),
        unit_batch_size=Decimal(1),
    )
    assert default_quantity.as_decimal() == explicit_quantity.as_decimal() == Decimal(1)
    calculate = Mock(wraps=upstream.calculate)
    factory = Mock(return_value=SimpleNamespace(calculate=calculate))
    monkeypatch.setattr(sizing, "FixedRiskSizer", factory)

    quantity = sizing.size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal("0.75"),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(1)
    factory.assert_called_once_with(instrument)
    calculate.assert_called_once()
    assert calculate.call_args.kwargs["unit_batch_size"] == Decimal(1)


def test_loss_flow_cap_and_budget_are_explicit_required_arguments() -> None:
    from canon_trading.sizing import size_for_loss

    for name in ("planned_loss", "flow_cap", "budget"):
        assert signature(size_for_loss).parameters[name].default is Parameter.empty


def test_subcent_planned_loss_is_preserved(instrument: Equity) -> None:
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("0.5000"),
        distance=Decimal("0.02"),
        planned_loss=Decimal("0.019"),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(1)
    assert quantity.as_decimal() * Decimal("0.50") * Decimal("0.02") <= Decimal("0.019")


def test_the_stop_is_stepped_down_to_alpacas_tick_before_sizing(instrument: Equity) -> None:
    # Review finding 2: 10.00 less 0.0123% is 9.99877, which Alpaca takes as 9.99, so a share loses
    # 0.01 at the stop as sent, and 0.0123 allows one share, not ten.
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.000123"),
        planned_loss=Decimal("0.0123"),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(1)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("planned_loss", Decimal(0)),
        ("planned_loss", Decimal(-1)),
        ("planned_loss", Decimal("NaN")),
        ("planned_loss", Decimal("Infinity")),
        ("flow_cap", Decimal(-1)),
        ("flow_cap", Decimal("NaN")),
        ("flow_cap", Decimal("Infinity")),
        ("budget", Decimal(-1)),
        ("budget", Decimal("NaN")),
        ("budget", Decimal("Infinity")),
    ],
)
def test_invalid_amounts_are_refused_by_name(instrument: Equity, name: str, value: Decimal) -> None:
    from canon_trading.sizing import size_for_loss

    amounts = {
        "planned_loss": Decimal(100),
        "flow_cap": Decimal(1000),
        "budget": Decimal(10000),
    }
    amounts[name] = value
    with pytest.raises(ValueError, match=f"^{name}_invalid$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            **amounts,
        )


@pytest.mark.parametrize("price", ["0.0000", "-1.0000"])
def test_nonpositive_entry_is_refused(instrument: Equity, price: str) -> None:
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^entry_invalid$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str(price),
            distance=Decimal("0.05"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_an_upstream_failure_propagates_without_a_fallback_size(
    instrument: Equity, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canon_trading import sizing

    calculate = Mock(side_effect=ValueError("upstream failure"))
    monkeypatch.setattr(
        sizing, "FixedRiskSizer", Mock(return_value=SimpleNamespace(calculate=calculate))
    )
    with pytest.raises(ValueError, match="^upstream failure$"):
        sizing.size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_module_header_names_the_upstream_contract() -> None:
    from canon_trading import sizing

    assert sizing.__doc__ is not None
    assert "FixedRiskSizer.calculate" in sizing.__doc__
    assert "python/nautilus_trader/risk/__init__.pyi:16-29" in sizing.__doc__
    assert "d53849b216e2" in sizing.__doc__


def test_a_planned_loss_beyond_moneys_native_precision_is_refused_by_name(
    instrument: Equity,
) -> None:
    from canon_trading.sizing import size_for_loss

    # A slot's dollars computed by division carry 28 significant digits.
    with pytest.raises(ValueError, match="^planned_loss_precision$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=Decimal(1000) / Decimal(3) * Decimal("0.02"),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_a_distance_built_from_a_float_sizes_against_the_step_below_it(
    instrument: Equity,
) -> None:
    # Second review, finding 1: a computed distance carries 28 or more digits. Its trigger is
    # rounded toward the larger loss, 9.4999... here, and stepped down to 9.49: 100 / 0.51 is 196.
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal.from_float(0.05),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(196)


@pytest.mark.parametrize(
    ("entry", "distance", "expected"),
    [("12.34", Decimal("0.37") / Decimal("12.34"), 270), ("10.01", Decimal(1) / 15, 149)],
)
def test_a_distance_computed_by_division_is_sized_not_refused(
    instrument: Equity, entry: str, distance: Decimal, expected: int
) -> None:
    # Second review, finding 1: 0.37/12.34 to 28 digits times 12.34 is a hair under 0.37, so the stop
    # is 11.97 and 100 / 0.37 is 270; 10.01 less a fifteenth is 9.3426..., 9.34, and 100 / 0.67 is 149.
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str(entry),
        distance=distance,
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(100000),
    )

    assert quantity.as_decimal() == Decimal(expected)


@pytest.mark.parametrize(("entry", "stop"), [("1.12", "1.00"), ("0.0005", "0.0004")])
def test_a_stop_stepped_past_the_ceiling_is_refused_by_name(
    instrument: Equity, entry: str, stop: str
) -> None:
    # Second review, findings 2 and 3: a tenth under 1.12 is 1.008, which steps to 1.00, a 10.7%
    # stop; 0.0005 steps to 0.0004, 20%.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^distance_over_ceiling$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str(entry),
            distance=Decimal("0.10"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(100000),
        )


def test_an_entry_off_alpacas_step_is_refused_by_name(instrument: Equity) -> None:
    # Second review, finding 4: Alpaca takes 10.02 for a buy at 10.0123, and 199 shares sized from
    # 10.0123 lose 101.49 from 10.02 at a 9.51 stop, over the planned 100.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^entry_off_step$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0123"),
            distance=Decimal("0.05"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_a_planned_loss_just_over_moneys_maximum_is_invalid_not_imprecise(
    instrument: Equity,
) -> None:
    # Second review, finding 5: a cent over MONEY_MAX has a whole part that fits.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^planned_loss_invalid$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=Decimal("17014118346046.01"),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


@pytest.mark.parametrize(
    ("flow_cap", "budget"),
    [("1e29", "1e31"), ("8e28", "1e31"), ("1e30", "1e40"), ("1e50", "1e40")],
)
def test_caps_beyond_what_a_quantity_holds_do_not_bind_and_are_not_misnamed(
    instrument: Equity, flow_cap: str, budget: str
) -> None:
    # Second review, findings 6 and 7: these failed inside upstream unnamed, or named the flow cap
    # for a budget; neither can bind past upstream's QUANTITY_MAX, so the planned loss sizes 200.
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(flow_cap),
        budget=Decimal(budget),
    )

    assert quantity.as_decimal() == Decimal(200)


def test_the_instruments_max_quantity_bounds_the_result() -> None:
    from canon_trading.sizing import size_for_loss

    instrument = Equity(
        instrument_id=InstrumentId.from_str("AAA.ALPACA"),
        raw_symbol=Symbol("AAA"),
        currency=Currency.from_str("USD"),
        price_precision=4,
        price_increment=Price.from_str("0.0001"),
        ts_event=0,
        ts_init=0,
        max_quantity=Quantity.from_int(50),
    )
    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(10000),
    )

    assert quantity.as_decimal() == Decimal(50)


def test_a_loss_per_share_the_default_context_would_round_is_refused_not_sized(
    instrument: Equity,
) -> None:
    # Review finding 1 (/code-review, 2026-11-12): the exact loss per share, 0.5 and a 1 in the 31st
    # place, is over the planned 0.5, but precision 28 rounded it to 0.5 and one share was sized.
    # Rounded toward the larger loss, the stop as sent is 9.49 and a share loses 0.51.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^loss_under_one_share$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.0500000000000000000000000000001"),
            planned_loss=Decimal("0.5"),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_a_callers_decimal_context_does_not_round_the_stop(
    instrument: Equity, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding 1: under a caller's precision 2 the stop became 9.7 and stepped to 9.70; the
    # exact 9.676667 steps to 9.67. Stepping itself must not fail in the caller's context.
    from decimal import localcontext

    from canon_trading import sizing

    upstream = FixedRiskSizer(instrument)
    calculate = Mock(wraps=upstream.calculate)
    monkeypatch.setattr(
        sizing, "FixedRiskSizer", Mock(return_value=SimpleNamespace(calculate=calculate))
    )
    with localcontext() as context:
        context.prec = 2
        sizing.size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0100"),
            distance=Decimal("0.0333"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(100000),
        )

    assert calculate.call_args.kwargs["stop_loss"].as_decimal() == Decimal("9.67")


def test_a_planned_loss_beyond_moneys_bounds_is_refused_as_invalid_not_as_precision(
    instrument: Equity,
) -> None:
    # Review finding 3: Money's bounds error was named planned_loss_precision, which tells the
    # caller to round an amount that is too large, not too precise.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^planned_loss_invalid$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=Decimal("1e20"),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_a_budget_whose_share_count_overflows_the_context_is_bounded_by_the_flow_cap(
    instrument: Equity,
) -> None:
    # Review finding 4: budget // price over 28 digits was refused as budget_invalid, though the
    # budget is valid and the flow cap binds.
    from canon_trading.sizing import size_for_loss

    quantity = size_for_loss(
        instrument=instrument,
        entry=Price.from_str("1.0000"),
        distance=Decimal("0.05"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal("1e30"),
    )

    assert quantity.as_decimal() == Decimal(1000)


def test_an_instrument_that_is_not_an_equity_is_refused_by_name() -> None:
    # Review finding 5: the whole-share pre-checks assume an increment and multiplier of one.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(TypeError, match="^instrument_invalid$"):
        size_for_loss(
            instrument=SimpleNamespace(multiplier=Quantity.from_int(50)),  # ty: ignore[invalid-argument-type]
            entry=Price.from_str("10.0000"),
            distance=Decimal("0.05"),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(10000),
        )


def test_the_loss_at_the_stop_as_sent_never_exceeds_the_planned_loss(instrument: Equity) -> None:
    # Review finding 2, the lead's property (2026-11-12): the stop Alpaca is sent is the exact
    # trigger on its step (price_step.stop_trigger_price), above zero and within the ceiling, and
    # shares times entry less that stop is at most the planned loss; one more share would exceed it.
    from decimal import localcontext

    from canon_trading.price_step import stop_trigger_price
    from canon_trading.sizing import DISTANCE_CEILING
    from canon_trading.sizing import size_for_loss

    entries = (
        "0.0001", "0.0005", "0.0731", "0.5123", "0.9999", "1.00", "1.07", "1.12", "1.93",
        "10.01", "12.34", "123.45", "4999.99",
    )  # fmt: skip
    distances = (
        Decimal("0.0001"), Decimal("0.0123"), Decimal("0.0333"), Decimal("0.04"), Decimal("0.05"),
        Decimal("0.0777"), Decimal("0.10"), Decimal(1) / 15, Decimal("0.37") / Decimal("12.34"),
        Decimal.from_float(0.05),
    )  # fmt: skip
    losses = ("0.37", "5", "100", "1234.56")
    sized = 0
    for entry, fraction, loss in product(entries, distances, losses):
        price, planned = Decimal(entry), Decimal(loss)
        try:
            quantity = size_for_loss(
                instrument=instrument,
                entry=Price.from_str(entry),
                distance=fraction,
                planned_loss=planned,
                flow_cap=Decimal(10**9),
                budget=Decimal(10**12),
            ).as_decimal()
        except ValueError as e:
            assert str(e) in {
                "size_zero",
                "loss_under_one_share",
                "distance_over_ceiling",
                "stop_zero",
            }, (
                entry,
                fraction,
                loss,
                e,
            )
            continue
        sized += 1
        with localcontext() as context:
            context.prec = 100
            stop = stop_trigger_price(price - price * fraction)
        per_share = price - stop
        assert stop > 0, (entry, fraction, loss)
        assert per_share <= price * DISTANCE_CEILING, (entry, fraction, loss, stop)
        assert quantity * per_share <= planned, (entry, fraction, loss, quantity)
        assert (quantity + 1) * per_share > planned, (entry, fraction, loss, quantity)
    assert sized > 200


@pytest.mark.parametrize(
    ("entry", "distance"), [("0.0001", "0.10"), ("0.0001", "0.05"), ("0.0001", "0.01")]
)
def test_a_stop_stepped_down_to_zero_is_refused_as_its_own_name(
    instrument: Equity, entry: str, distance: str
) -> None:
    # The lead's requirement for S2 (2026-11-12): each refusal is counted by its own name, and a stop
    # that steps down to zero is no stop at all, not one past the ceiling.
    from canon_trading.sizing import size_for_loss

    with pytest.raises(ValueError, match="^stop_zero$"):
        size_for_loss(
            instrument=instrument,
            entry=Price.from_str(entry),
            distance=Decimal(distance),
            planned_loss=Decimal(100),
            flow_cap=Decimal(1000),
            budget=Decimal(100000),
        )


def test_the_plan_returns_the_stop_it_was_sized_against(instrument: Equity) -> None:
    # The lead's requirement for S2 (2026-11-12): the stop sized and the stop sent are one value, so
    # the plan hands the caller that stop with the quantity; 10.42 less 3% is 10.1074, sent at 10.10.
    from canon_trading.sizing import plan_for_loss

    plan = plan_for_loss(
        instrument=instrument,
        entry=Price.from_str("10.42"),
        distance=Decimal("0.03"),
        planned_loss=Decimal(100),
        flow_cap=Decimal(1000),
        budget=Decimal(100000),
    )

    assert plan.stop == Decimal("10.10")
    assert plan.quantity.as_decimal() == Decimal(100) // (Decimal("10.42") - plan.stop)


def test_every_refusal_name_is_listed_for_counting() -> None:
    # The Mover counts each refusal by its name; a name raised here and missing from REFUSALS would
    # go uncounted, so the two are one set.
    import re
    from pathlib import Path

    from canon_trading import sizing

    text = Path(sizing.__file__).read_text()
    raised = set(re.findall(r'ValueError\("([a-z_]+)"\)', text))
    raised |= {"flow_cap_invalid", "budget_invalid"}  # raised as f"{name}_invalid"
    assert set(sizing.REFUSALS) == raised
