"""Scratch reproductions of three review findings on unit 5 (`Mover.adopt_holding`).

Each test asserts the safe behaviour, so it fails when the finding is real. The engine,
instrument (AAA, price increment 0.0001 like the adapter's equities) and the `Adopting`
driver are tests/test_mover_adopt.py's. No network or file writes.
"""

from decimal import Decimal
from typing import Any

from nautilus_trader.model import Price
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeTick
from nautilus_trader.model import TriggerType

from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import strategy
from tests.test_mover_adopt import Adopting
from tests.test_mover_adopt import quote
from tests.test_mover_positive_exits import print_at

D = Decimal
HALF = SECOND // 2


class Printing(Adopting):
    """The adoption driver, with this strategy's trade subscription from the start."""

    def on_start(self) -> None:
        super().on_start()
        self.subscribe_trades(AAA)


def run(
    kind: type[Adopting],
    level: str,
    *later: QuoteTick | TradeTick,
    trigger: TriggerType | None = None,
) -> Adopting:
    """`test_mover_adopt.replay` without its no-error assertion, so a refusal can be read."""
    eng = engine()
    changes: dict[str, Any] = {"entries": 1, "extended_hours": True}
    if trigger is not None:
        changes["trigger"] = trigger
    s = kind(strategy(**changes).settings)
    s.level = Price.from_str(level)
    eng.add_strategy(s)
    eng.add_data(
        [
            quote("10.0000", 0),
            quote("10.0000", SECOND),
            *later,
            quote("20.0000", 8 * SECOND, instrument=BBB),
        ]
    )
    try:
        eng.run()
        return s
    finally:
        eng.dispose()


def assert_last_price_refused(s: Adopting) -> None:
    """Carry uses quotes until print seeding can preserve its protection (lead, 2026-11-13)."""
    assert len(s.errors) == 1 and isinstance(s.errors[0], ValueError)
    reason = str(s.errors[0])
    assert str(AAA) in reason and "LAST_PRICE" in reason and "BID_ASK" in reason
    assert reason in s.said, "the refusal must log its cause"
    assert s.initial["held"] == D(7) and not s.initial["sells"]
    assert len(s.params) == 1, "only the setup buy may have been submitted"
    assert all(not slot.adopted and slot.adoption_guard is None for slot in s.slots.values())


def test_p1_1_last_price_adoption_is_refused_before_a_late_print_can_seed_a_sell() -> None:
    """P1-1's safe seed and later late print: refuse before a dormant sell can be armed."""
    s = run(
        Printing,
        "10.1000",
        print_at("10.0000", SECOND, SECOND),  # the safe seed at adoption (2 s)
        print_at("0.2000", HALF, 3 * SECOND),  # late: venue time 0.5 s, delivered at 3 s
        print_at("10.2000", 4 * SECOND, 4 * SECOND),  # fresh: activates the dormant sell
        print_at("10.0500", 5 * SECOND, 5 * SECOND),  # fresh: through the 10.10 trigger
        trigger=TriggerType.LAST_PRICE,
    )
    assert_last_price_refused(s)
    assert not s.slots and not s.opened_slots


class HeldOut(Printing):
    """A slot holding a stop at 10.20 (`_hold`), adopted with a supplied level under it."""

    def before_adopt(self) -> None:
        self._open_slot(AAA)
        slot = self.slots[AAA]
        slot.held_since = T0 + SECOND
        slot.held_stop = D("10.20")


def test_p1_2_last_price_adoption_is_refused_without_taking_over_a_held_stop() -> None:
    """P1-2: a late 0.20 seed used to suspend the held 10.20 stop; refuse at adoption instead."""
    s = run(
        HeldOut,
        "9.9000",
        print_at("10.3000", SECOND, SECOND),
        print_at("0.2000", HALF, SECOND + HALF),  # late: the cache's trade at adoption
        # each fresh print through the held 10.20 is followed, before the next one-second step, by a
        # late low print, so the step reads the late one as the last delivered seed
        print_at("10.1000", 3 * SECOND + HALF, 3 * SECOND + HALF),
        print_at("0.2000", HALF + 1, 3 * SECOND + 3 * SECOND // 4),
        print_at("10.1000", 4 * SECOND + HALF, 4 * SECOND + HALF),
        print_at("0.2000", HALF + 2, 4 * SECOND + 3 * SECOND // 4),
        print_at("10.1000", 5 * SECOND + HALF, 5 * SECOND + HALF),
        print_at("0.2000", HALF + 3, 5 * SECOND + 3 * SECOND // 4),
        trigger=TriggerType.LAST_PRICE,
    )
    assert_last_price_refused(s)
    slot = s.slots[AAA]
    assert slot.held_stop == D("10.20") and slot.held_since == T0 + SECOND
    assert slot.protect_id is None and slot.trail_trigger is None


def test_p1_3_an_off_step_adoption_level_rounds_up_and_its_confirmed_breach_sells() -> None:
    """P1-3: the default trigger (BID_ASK). A supplied level 10.0037 is on AAA's 0.0001 increment but
    off Alpaca's cent step above $1. The first bid 10.0000 rests the sell; a second, later bid 10.0020
    is under the supplied level and confirms the breach. Round up to 10.01, then sell the holding;
    flooring a remake to 10.00 would miss this breach."""
    s = run(
        Adopting,
        "10.0037",
        quote("10.0020", 5 * SECOND + HALF),  # confirms: under 10.0037, above 10.00
        quote("10.0020", 6 * SECOND + HALF),
        quote("10.0020", 7 * SECOND + HALF),
    )
    assert s.settings.trigger == TriggerType.BID_ASK
    assert not s.errors, repr(s.errors)
    (adopted,) = s.initial["working"]
    assert adopted.init_event.trigger_price == Price.from_str("10.0100")
    assert adopted.init_event.activation_price == Price.from_str("10.0200")
    assert any(
        "adoption level" in message and "10.0037" in message and "10.0100" in message
        for message in s.said
    ), s.said
    last = s.steps[-1]
    remade = [o for o in last["sells"] if o.client_order_id != adopted.client_order_id]
    assert last["held"] == 0, (
        "a breach confirmed at 10.0020 under the supplied 10.0037 sold nothing; remade triggers: "
        f"{[str(o.trigger_price) for o in remade]}"
    )
    for order in last["sells"]:
        for event in [order, *order.events()]:
            for name in ("price", "trigger_price", "activation_price"):
                price = getattr(event, name, None)
                if price is not None:
                    value = price.as_decimal()
                    assert value > 0 and value % D("0.01" if value >= 1 else "0.0001") == 0
