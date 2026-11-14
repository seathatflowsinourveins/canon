"""Adopt an own holding through Mover's existing protection path (24/5 unit 5).

The native engine and instruments are test_mover's. A setup buy establishes the
position before adoption; its fill is deliberately outside this run's entry
bookkeeping, like a holding reconciled at a restart. No network or file writes.
Pinned contracts: common/__init__.pyi:568-576, :1523-1547; emulator.rs:665-699,
:1721-1782; trailing.rs:58-100 (NautilusTrader d53849b216e2).
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from canon_trading.mover import EXTENDED_HOURS
from canon_trading.mover import DistanceRefusal
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import MOVER_ID
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import Recording
from tests.test_mover import engine
from tests.test_mover import strategy
from tests.test_mover_positive_exits import print_at

D = Decimal


def quote(bid: str, offset: int, *, instrument=AAA, event: int | None = None) -> QuoteTick:
    return QuoteTick(
        instrument,
        Price.from_str(bid),
        Price.from_str(f"{D(bid) + D('0.02'):.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        T0 + (offset if event is None else event),
        T0 + offset,
    )


class Adopting(Recording):
    """A native holding, with snapshots before engine shutdown cancels its stop."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.level = Price.from_str("9.9000")
        self.repeat = False
        self.hide_initial_quote = False
        self.add_shares = False
        self.add_price = Price.from_str("10.0200")
        self.adopted_at: int | None = None
        self.seed_id = None
        self.initial: dict[str, Any] = {}
        self.steps: list[dict[str, Any]] = []
        self.errors: list[Exception] = []

    @property
    def cache(self) -> Any:
        native = super().cache
        hide = self.hide_initial_quote

        class View:
            def quote(self, instrument_id):
                found = native.quote(instrument_id)
                if hide and found is not None and found.ts_init <= T0 + SECOND:
                    return None
                return found

            def __getattr__(self, name):
                return getattr(native, name)

        return View()

    def on_start(self) -> None:
        super().on_start()
        self.subscribe_quotes(AAA)
        order = self.order_factory.limit(
            AAA, OrderSide.BUY, Quantity.from_int(7), Price.from_str("10.0200")
        )
        self.seed_id = order.client_order_id
        self.submit_order(order)

    def on_order_filled(self, event: Any) -> None:
        if event.client_order_id != self.seed_id:
            super().on_order_filled(event)

    def snapshot(self, now: int) -> dict[str, Any]:
        positions = self.cache.positions_open(instrument_id=AAA, strategy_id=self.strategy_id)
        sells = self.cache.orders(strategy_id=self.strategy_id, side=OrderSide.SELL)
        return {
            "now": now,
            "held": sum((p.signed_qty for p in positions), D(0)),
            "sells": sells,
            "working": [order for order in sells if not order.is_closed],
            "released": set(self.released),
            "entered": self._entered(),
        }

    def before_adopt(self) -> None:
        """Arrange the existing slot or node state at the public seam."""

    def _on_step(self, event: Any) -> None:
        now = event.ts_event
        if self.add_shares and now == T0 + 3 * SECOND:
            self.submit_order(
                self.order_factory.limit(AAA, OrderSide.BUY, Quantity.from_int(3), self.add_price)
            )
        if self.adopted_at is None and now >= T0 + 2 * SECOND:
            self.adopted_at = now
            try:
                self.before_adopt()
                self.adopt_holding(AAA, self.level)
                self.initial = self.snapshot(now)
                if self.repeat:
                    self.adopt_holding(AAA, Price.from_str("10.1000"))
            except (AttributeError, ValueError, RuntimeError, TypeError) as error:
                self.errors.append(error)
                self.initial = self.snapshot(now)
                return
        if not self.errors:
            super()._on_step(event)
            self.steps.append(self.snapshot(now))


def replay(
    level: str,
    *later: QuoteTick | TradeTick,
    repeat: bool = False,
    hide_initial_quote: bool = False,
    add_shares: bool = False,
    trigger: TriggerType = TriggerType.BID_ASK,
    initial_bid: str = "10.0000",
    kind: type[Adopting] = Adopting,
) -> Adopting:
    eng = engine()
    s = kind(strategy(entries=1, extended_hours=True, trigger=trigger).settings)
    s.level, s.repeat = Price.from_str(level), repeat
    s.hide_initial_quote, s.add_shares = hide_initial_quote, add_shares
    eng.add_strategy(s)
    eng.add_data(
        [
            quote("10.0000", 0),
            quote(initial_bid, SECOND),
            *later,
            quote("20.0000", 8 * SECOND, instrument=BBB),
        ]
    )
    try:
        eng.run()
        assert not s.errors, repr(s.errors)
        assert s.initial, "the setup holding must reach adoption"
        return s
    finally:
        eng.dispose()


@pytest.mark.parametrize("refusal", ["distance", "profile", "anchor"])
def test_refused_adoption_does_not_open_or_subscribe_a_slot(refusal: str) -> None:
    """L6/2: all validation that can refuse precedes the slot's side effects."""
    eng = engine()

    class Refused(Adopting):
        def before_adopt(self) -> None:
            if refusal == "distance":
                self.settings.trail_mode = "atr"
                self.settings.spread_floor_m = D(100)
            elif refusal == "profile":
                self.settings.trail_mode = "profile"

        def _trail_anchor(self, slot, **kwargs):
            if refusal == "anchor":
                raise RuntimeError("unavailable adoption anchor")
            return super()._trail_anchor(slot, **kwargs)

    s = Refused(strategy(protect=False).settings)
    eng.add_strategy(s)
    eng.add_data([quote("10.0000", 0), quote("10.0000", 8 * SECOND)])
    try:
        eng.run()
        assert len(s.errors) == 1
        expected = {"distance": DistanceRefusal, "profile": ValueError, "anchor": RuntimeError}
        assert isinstance(s.errors[0], expected[refusal])
        assert not s.slots and not s.opened_slots
        assert not s.exits and not s.history_loads
        assert len(s.params) == 1  # only the setup buy
    finally:
        eng.dispose()


def test_adopting_a_held_stop_waits_for_a_second_observation() -> None:
    """L6/3: a held stop already through its low must not sell on adoption."""

    class Held(Adopting):
        def before_adopt(self) -> None:
            self._open_slot(AAA)
            slot = self.slots[AAA]
            slot.held_since = T0 + SECOND
            slot.held_stop = D("10.10")
            slot.held_low = D("10.00")

    s = replay("10.1000", quote("10.0000", 5 * SECOND), kind=Held)
    (order,) = s.initial["working"]
    assert order.order_type.name == "TRAILING_STOP_LIMIT" and order.is_emulated
    assert s.initial["held"] == D(7) and not s.initial["released"]
    assert all(row["held"] == D(7) for row in s.steps if row["now"] <= T0 + 5 * SECOND)
    assert s.steps[-1]["held"] == 0


@pytest.mark.parametrize("gate", ["wind_down", "market_exit", "released"])
def test_adoption_refuses_while_an_exit_owns_the_holding(gate: str) -> None:
    """L6/4: adoption cannot create a second sell path beside an exit."""
    eng = engine()

    class Exiting(Adopting):
        def before_adopt(self) -> None:
            if gate == "wind_down":
                self.winding_down = True
            elif gate == "released":
                self._open_slot(AAA)
                assert self.seed_id is not None
                self.slots[AAA].protect_id = self.seed_id
                self.released.add(self.seed_id.value)

        def is_exiting(self):
            return gate == "market_exit" and self.adopted_at is not None

    s = Exiting(strategy(protect=False).settings)
    eng.add_strategy(s)
    eng.add_data([quote("10.0000", 0), quote("10.0000", 8 * SECOND)])
    try:
        eng.run()
        assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
        assert "exit" in str(s.errors[0]) or "wind" in str(s.errors[0])
        assert eng.cache.orders(strategy_id=MOVER_ID, side=OrderSide.SELL) == []
        assert not s.exits
        assert not s.slots if gate != "released" else not s.slots[AAA].adopted
    finally:
        eng.dispose()


def test_adoption_refuses_last_price_before_a_late_delivered_print_can_seed_a_limit() -> None:
    """L6/5: a high bid and late low print must be refused before LAST_PRICE can arm."""

    class LatePrint(Adopting):
        def on_start(self) -> None:
            super().on_start()
            self.subscribe_trades(AAA)

        def before_adopt(self) -> None:
            assert self.cache.trade(AAA).price == Price.from_str("0.2000")

    eng = engine()
    s = LatePrint(strategy(trigger=TriggerType.LAST_PRICE).settings)
    s.level = Price.from_str("0.1000")
    eng.add_strategy(s)
    eng.add_data(
        [
            quote("10.0000", 0),
            print_at("10.0000", SECOND, SECOND),
            print_at("0.2000", SECOND // 2, SECOND + SECOND // 2),
            print_at("10.0000", 4 * SECOND, 4 * SECOND),
            quote("20.0000", 8 * SECOND, instrument=BBB),
        ]
    )
    try:
        eng.run()
        assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
        reason = str(s.errors[0])
        assert str(AAA) in reason and "LAST_PRICE" in reason and "BID_ASK" in reason
        assert reason in s.said
        assert s.initial["held"] == D(7) and not s.initial["sells"]
        assert not s.slots and not s.opened_slots
        assert eng.cache.orders(strategy_id=MOVER_ID, side=OrderSide.SELL) == []
    finally:
        eng.dispose()


def test_adopted_trigger_remakes_use_the_venue_step() -> None:
    """L6/6: a quantity remake puts an engine-raised fractional-cent trigger on the venue step,
    floored as main floors every slot's (the lead's ruling, 2026-11-13, 15:48)."""

    class MoreShares(Adopting):
        def before_adopt(self) -> None:
            self.add_price = Price.from_str("10.5000")

    s = replay(
        "9.9000", quote("10.2137", 2 * SECOND + SECOND // 2), add_shares=True, kind=MoreShares
    )
    orders = s.steps[-1]["sells"]
    assert len(orders) == 2
    remade = orders[-1]
    assert remade.init_event.trigger_price == Price.from_str("10.0000")
    assert D(s.sale_rows[remade.client_order_id.value]["kept_trigger"]) == D("10.00")


@pytest.mark.parametrize("retained", ["fill", "hold"])
def test_adoption_confirms_against_the_higher_trigger_it_actually_armed(retained: str) -> None:
    """L6/7: a second bid between the supplied and kept levels elects the stop."""

    class Retained(Adopting):
        def before_adopt(self) -> None:
            self._open_slot(AAA)
            slot = self.slots[AAA]
            if retained == "fill":
                slot.sized_loss = D("0.01")  # 10.02 average -> 10.01 kept trigger
            else:
                slot.held_since = T0 + SECOND
                slot.held_stop = D("10.01")

    s = replay("9.9000", quote("10.0000", 5 * SECOND), kind=Retained)
    assert s.steps[-1]["held"] == 0
    (order,) = s.initial["working"]
    assert order.init_event.trigger_price == Price.from_str("10.0100")
    assert s.initial["held"] == D(7) and not s.initial["released"]


@pytest.mark.parametrize(
    ("level", "on_step"), [("9.9000", "9.9000"), ("9.9555", "9.9600"), ("9.95551", "9.9600")]
)
def test_adoption_arms_one_reduce_only_emulated_sell_for_the_whole_own_long(
    level: str, on_step: str
) -> None:
    s = replay(level)
    (order,) = s.initial["working"]
    assert order.order_type.name == "TRAILING_STOP_LIMIT"
    assert order.is_emulated and order.is_reduce_only
    assert order.quantity.as_decimal() == s.initial["held"] == D(7)
    assert order.trigger_price == Price.from_str(on_step)
    assert order.init_event.trigger_price == Price.from_str(on_step)
    assert order.emulation_trigger == order.trigger_type == TriggerType.BID_ASK
    assert order.time_in_force == TimeInForce.DAY
    assert s.params[-1] == EXTENDED_HOURS
    slot = s.slots[AAA]
    assert slot.trail_plan is not None and slot.initial_trail == slot.trail_plan
    assert slot.trail_plan.trail == s.settings.trail
    assert order.trailing_offset == D("0.21")  # 2% of 10.02, rounded up to the entry's cent


@pytest.mark.parametrize("level", ["10.0000", "10.1000"])
def test_a_first_bid_at_or_below_the_level_rests_through_repeated_steps(level: str) -> None:
    s = replay(level)
    for snapshot in [s.initial, *s.steps]:
        if snapshot["now"] < s.adopted_at:
            continue
        assert snapshot["held"] == D(7)
        assert not snapshot["released"]
        (order,) = snapshot["working"]
        assert order.is_emulated and order.is_reduce_only
        assert order.trigger_price == Price.from_str(level)
        assert order.filled_qty.as_decimal() == 0
    assert not s.slots[AAA].exiting


@pytest.mark.parametrize("level", ["10.0000", "10.1000"])
def test_a_second_breaching_quote_on_a_later_step_sells_the_adopted_holding(level: str) -> None:
    second_at = 5 * SECOND + SECOND // 2
    s = replay(level, quote("10.0000", second_at))
    assert s.initial["held"] == D(7) and not s.initial["released"]
    before = [row for row in s.steps if row["now"] < T0 + second_at]
    assert all(row["held"] == D(7) for row in before)
    assert s.steps[-1]["held"] == 0
    filled = [order for order in s.steps[-1]["sells"] if order.filled_qty.as_decimal()]
    assert sum((order.filled_qty.as_decimal() for order in filled), D(0)) == D(7)
    assert all(order.is_reduce_only for order in filled)
    assert all(at >= T0 + second_at for _, side, _, _, at in s.fills if side == "SELL")


def test_a_replayed_quote_timestamp_does_not_confirm_the_first_bid() -> None:
    s = replay("10.1000", quote("10.0000", 5 * SECOND, event=SECOND))
    assert all(row["held"] == D(7) and not row["released"] for row in s.steps)


def test_adoption_before_a_quote_rests_on_the_first_quote_and_sells_on_the_second() -> None:
    s = replay(
        "10.1000",
        quote("10.0000", 4 * SECOND),
        quote("10.0000", 6 * SECOND),
        hide_initial_quote=True,
    )
    (order,) = s.initial["working"]
    assert order.is_emulated and order.trigger_price == Price.from_str("10.1000")
    assert all(row["held"] == D(7) for row in s.steps if row["now"] <= T0 + 6 * SECOND)
    assert s.steps[-1]["held"] == 0


@pytest.mark.parametrize(("level", "on_step"), [("10.1000", "10.1000"), ("10.1001", "10.1100")])
def test_more_own_shares_are_covered_while_the_first_quote_still_waits(
    level: str, on_step: str
) -> None:
    s = replay(level, add_shares=True)
    last = s.steps[-1]
    assert last["held"] == D(10)
    (order,) = last["working"]
    assert order.quantity.as_decimal() == D(10)
    assert order.is_emulated and order.is_reduce_only
    assert order.init_event.trigger_price == Price.from_str(on_step)
    assert not s.released


def test_a_remade_adopted_sell_floors_a_native_off_step_trigger_onto_the_step() -> None:
    class MoreShares(Adopting):
        def before_adopt(self) -> None:
            self.add_price = Price.from_str("10.2000")

    s = replay(
        "9.9555",
        quote("10.1737", 2 * SECOND + SECOND // 2),
        add_shares=True,
        kind=MoreShares,
    )
    first = s.initial["working"][0]
    (remade,) = s.steps[-1]["working"]
    assert remade.client_order_id != first.client_order_id
    assert remade.quantity.as_decimal() == s.steps[-1]["held"] == D(10)
    # The native engine's 10.1737 - 0.21 is 9.9637: a remake floors it onto the step, as main does;
    # raising it to 9.97 would sell a bid between the two that never met the reached trigger
    # (tests/test_mover_adopt_remake_floor.py).
    assert remade.init_event.trigger_price == Price.from_str("9.9600")
    assert remade.init_event.price == Price.from_str("9.9000")
    assert sum("adoption level" in message and "rounded" in message for message in s.said) == 1


def test_last_price_adoption_is_refused_even_with_safe_fresh_prints() -> None:
    second_at = 5 * SECOND + SECOND // 2
    eng = engine()
    s = Adopting(strategy(trigger=TriggerType.LAST_PRICE).settings)
    eng.add_strategy(s)
    eng.add_data(
        [
            quote("10.0000", 0),
            print_at("10.0000", SECOND, SECOND),
            print_at("10.0000", second_at, second_at),
            quote("20.0000", 8 * SECOND, instrument=BBB),
        ]
    )
    try:
        eng.run()
        assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
        reason = str(s.errors[0])
        assert str(AAA) in reason and "LAST_PRICE" in reason and "BID_ASK" in reason
        assert reason in s.said
        assert not s.slots and not s.opened_slots and not s.history_loads
        assert s.initial["held"] == D(7) and not s.initial["sells"]
        assert eng.cache.orders(strategy_id=MOVER_ID, side=OrderSide.SELL) == []
        assert len(s.params) == 1  # only the setup buy
    finally:
        eng.dispose()


def test_an_adoption_level_already_on_the_step_is_unchanged_without_a_move_log() -> None:
    s = replay("10.0100")
    (order,) = s.initial["working"]
    assert order.init_event.trigger_price == Price.from_str("10.0100")
    assert order.init_event.activation_price == Price.from_str("10.0200")
    assert not any("adoption level" in message and "rounded" in message for message in s.said)


@pytest.mark.parametrize(
    ("given", "on_step", "activation"),
    [("0.10001", "0.1001", "0.2701"), ("0.99999", "1.0000", "1.0100")],
)
def test_a_sub_dollar_adoption_level_rounds_up_on_its_band_and_sells(
    given: str, on_step: str, activation: str
) -> None:
    # The earlier 10.02 fill still anchors offsets; the supplied level alone moves.
    s = replay(given, quote("0.0900", 5 * SECOND), initial_bid="0.0900")
    (order,) = s.initial["working"]
    assert order.init_event.trigger_price == Price.from_str(on_step)
    assert order.init_event.activation_price == Price.from_str(activation)
    assert any(
        "adoption level" in message and given in message and on_step in message
        for message in s.said
    )
    assert s.initial["held"] == D(7) and not s.initial["released"]
    assert s.steps[-1]["held"] == 0
    for sale in s.steps[-1]["sells"]:
        if sale.price is not None:
            price = sale.price.as_decimal()
            assert price > 0 and price % D("0.01" if price >= 1 else "0.0001") == 0


@pytest.mark.parametrize("level", ["0.0000", "-0.0001"])
def test_a_non_positive_adoption_level_is_refused_by_name_before_arming(level: str) -> None:
    eng = engine()
    s = Adopting(strategy().settings)
    s.level = Price.from_str(level)
    eng.add_strategy(s)
    eng.add_data([quote("10.0000", 0), quote("10.0000", 8 * SECOND)])
    try:
        eng.run()
        assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
        reason = str(s.errors[0])
        assert str(AAA) in reason and level in reason and "positive" in reason
        assert reason in s.said
        assert not s.slots and not s.opened_slots
        assert s.initial["held"] == D(7) and not s.initial["sells"]
        assert len(s.params) == 1
    finally:
        eng.dispose()


@pytest.mark.parametrize(("second", "remaining"), [("0.1600", D(7)), ("0.0900", D(0))])
def test_a_gap_below_the_trail_offsets_keeps_the_supplied_stop_until_it_is_met(
    second: str, remaining: Decimal
) -> None:
    s = replay("0.1000", quote(second, 5 * SECOND), initial_bid="0.1500")
    (order,) = s.initial["working"]
    assert order.is_emulated and order.trigger_price == Price.from_str("0.1000")
    assert order.trailing_offset == D("0.21")  # the earlier 10.02 fill still anchors the plan
    assert s.steps[-1]["held"] == remaining
    for row in s.steps:
        for sale in row["sells"]:
            assert sale.price is None or sale.price.as_decimal() > 0


def test_adoption_refuses_persistence_instead_of_bypassing_its_configured_counts() -> None:
    eng = engine()
    s = Adopting(strategy(breach_count=3, trigger=TriggerType.LAST_PRICE).settings)
    eng.add_strategy(s)
    eng.add_data([quote("10.0000", 0), quote("10.0000", 8 * SECOND)])
    try:
        eng.run()
        assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
        assert "persistence" in str(s.errors[0])
        assert not s.slots and not s.opened_slots and not s.exits
        assert eng.cache.orders(strategy_id=MOVER_ID, side=OrderSide.SELL) == []
    finally:
        eng.dispose()


def test_adoption_sends_no_entry_and_consumes_no_entry_allowance() -> None:
    s = replay("9.9000")
    assert s.entries_sent == [] and s.fills == []
    assert all(row["entered"] == 0 for row in s.steps)
    assert s.slots[AAA].entry_id is None
    assert s.slots[AAA].entry_record is None
    assert len(s.params) == 2  # the setup buy, then exactly one protective sell


def test_adopting_twice_preserves_the_first_stop_without_another_sale() -> None:
    s = replay("9.9000", repeat=True)
    (first,) = s.initial["working"]
    assert len(s.steps[-1]["sells"]) == 1
    assert s.slots[AAA].protect_id == first.client_order_id
    assert first.trigger_price == Price.from_str("9.9000")


def test_the_engine_can_only_raise_the_adopted_level() -> None:
    s = replay("9.9000", quote("10.5000", 4 * SECOND))
    assert s.initial["working"][0].trigger_price == Price.from_str("9.9000")
    triggers = [
        order.trigger_price.as_decimal()
        for row in s.steps
        for order in row["working"]
        if order.is_emulated
    ]
    assert triggers == sorted(triggers)
    assert triggers[-1] > D("9.9000")


@pytest.mark.parametrize("owner", ["Other-001", "EXTERNAL"])
def test_adoption_refuses_another_strategy_or_external_by_name_without_changes(owner: str) -> None:
    eng = engine()

    class ForeignHolding(Recording):
        @property
        def cache(self) -> Any:
            native = super().cache

            class View:
                def positions_open(self, instrument_id=None, strategy_id=None, **_kwargs):
                    if instrument_id != AAA or strategy_id not in (None, StrategyId(owner)):
                        return []
                    return [
                        SimpleNamespace(
                            strategy_id=StrategyId(owner),
                            instrument_id=AAA,
                            signed_qty=D(7),
                            avg_px_open=D("10.02"),
                        )
                    ]

                def __getattr__(self, name):
                    return getattr(native, name)

            return View()

    s = ForeignHolding(strategy().settings)
    eng.add_strategy(s)
    try:
        with pytest.raises(ValueError, match=owner):
            s.adopt_holding(AAA, Price.from_str("9.9000"))
        assert not s.slots and not s.opened_slots
        assert not s.params and not s.entries_sent and not s.exits
        assert eng.cache.orders(strategy_id=MOVER_ID) == []
    finally:
        eng.dispose()
