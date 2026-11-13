"""Three chain shapes a review of the build found still counted without a freeze. Each case is red on
the build; written by the owning lane before the fix round. Helpers are the custody test file's."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "custody_review_standins", Path(__file__).with_name("test_custody.py")
)
assert spec is not None and spec.loader is not None
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

SELL = "O-20261111-145500-001-001-2"


def frozen_and_nothing_sold(venue) -> None:
    quantity, why = s.opened(venue)
    assert quantity is None, (quantity, why)
    assert "SPY" in why
    report = s.close(venue)
    assert venue.sent == [], venue.sent
    assert "SPY" in report.frozen, report.lines()


def test_a_positive_count_behind_a_zero_fill_link_of_a_sell_chain_is_not_established() -> None:
    # ours bought 20; our sell chain shows 4, then 0, then 10. Whether the 10 counts the 4 is not
    # known (4 to 0 is itself a decrease): ours may have sold 10 or 14, so the net is not established
    venue = s.ReplacedNotListedOpen(s.REGULAR)
    venue.hold("SPY", 10)
    venue.history[0] = venue.history[0].model_copy(update={"qty": "20", "filled_qty": "20"})
    s.chain(venue, "4", "0", "10", side="sell", first=SELL)
    frozen_and_nothing_sold(venue)


def test_a_decrease_at_a_foreign_tail_of_our_sell_chain_freezes_the_symbol() -> None:
    # ours bought 30; our sells show 10 then 12; a hand's replacement after them shows 5
    venue = s.ReplacedNotListedOpen(s.REGULAR)
    venue.hold("SPY", 18)
    venue.history[0] = venue.history[0].model_copy(update={"qty": "30", "filled_qty": "30"})
    s.chain(venue, "10", "12", "5", side="sell", first=SELL, by_hand=(2,))
    frozen_and_nothing_sold(venue)


def test_two_listed_orders_replacing_the_same_order_freeze_the_symbol() -> None:
    # a fork: two replacements of ours both name the first order as the one they replace
    venue = s.ReplacedNotListedOpen(s.REGULAR)
    venue.hold("SPY", 16, client_order_id=None)
    s.chain(venue, "4", "10")
    fork = venue.history[-1].model_copy(
        update={
            "id": s.uuid4(),
            "client_order_id": s.replacement_client_order_id(s.NODE_BUY, s.STAMP + 9),
        }
    )
    venue.history.append(fork)
    frozen_and_nothing_sold(venue)
