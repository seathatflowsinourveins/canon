"""Blind custody contract F3/F4 (lead, 2026-11-13), against main b42fe47f.

An unestablished replacement-chain quantity freezes its symbol; zero-fill links
do not end the walk. Alpaca's Order.replaces/replaced_by and filled_qty fields:
alpaca-py c42add7c0a70, alpaca/trading/models.py. All answers use the public
Venue/chain/close stand-ins in test_custody.py, with its existing Venue subclass
that excludes replaced orders from the open listing. No account or disk state.

The file loader follows test_custody_quantity_blind.py so this file can run alone
under the repository's pytest importlib mode; TYPE_CHECKING keeps the helpers typed.
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from canon_trading import custody

if TYPE_CHECKING:
    from tests import test_custody as standins
else:
    spec = importlib.util.spec_from_file_location(
        "custody_chain_standins", Path(__file__).with_name("test_custody.py")
    )
    assert spec is not None and spec.loader is not None
    standins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standins)


def resting_order(venue: standins.Venue) -> str:
    """Our unfilled order must still be cancelled when its symbol freezes."""
    order = venue.open(
        client_order_id="O-20261111-145500-001-001-77",
        symbol="SPY",
        side="sell",
        qty="1",
        filled_qty="0",
        limit_price="999",
        submitted_at=(venue.now - timedelta(minutes=1)).isoformat(),
    )
    return order.client_order_id


def assert_unknown_chain_frozen(venue: standins.Venue) -> None:
    held = venue.held["SPY"]
    resting = resting_order(venue)
    report = standins.close(venue)
    assert not report.failed and not report.errors, report.lines()
    # One comparison preserves all four observations in a failing base run.
    assert (venue.sent, venue.canceled, venue.held["SPY"], set(report.frozen)) == (
        [],
        [resting],
        held,
        {"SPY"},
    ), report.lines()
    # A mismatch against an invented zero also freezes, but does not establish
    # the chain: the reason must retain that uncertainty rather than assert zero.
    assert any("not established" in reason for reason in report.frozen["SPY"]), report.lines()


def test_f3_positive_fill_decrease_freezes_and_cancels_ours() -> None:
    """18 -> 2 must not clamp the negative delta to zero and authorize 18 shares."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(venue, "18", "2")
    venue.hold("SPY", 2, client_order_id=None)
    venue.hold("SPY", 16, client_order_id=standins.FOREIGN[0])
    assert_unknown_chain_frozen(venue)


@pytest.mark.parametrize("by_hand", [(), (1,)], ids=["our-middle", "foreign-middle"])
def test_f4_zero_fill_middle_cannot_hide_a_start_outside_lookback(
    by_hand: tuple[int, ...],
) -> None:
    """The final 20 carries an old 18 through a zero-fill link; 18 held are foreign."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(
        venue,
        "18",
        "0",
        "20",
        by_hand=by_hand,
        first_age=custody.LINEAGE_LOOKBACK + timedelta(hours=1),
    )
    venue.hold("SPY", 2, client_order_id=None)
    venue.hold("SPY", 18, client_order_id=standins.FOREIGN[0])
    assert_unknown_chain_frozen(venue)


# Turned by the owning lane after the tester's hand, on the lead's ruling of 2026-11-13: what a
# replacement shows after a zero-fill link with earlier fills is not documented, so the count is not
# established and the symbol freezes. The tester's case required 10 to be counted once and closed, and
# was named test_f4_inside_chain_counts_ten_once_across_a_zero_fill_middle.
@pytest.mark.parametrize("by_hand", [(), (1,)], ids=["our-middle", "foreign-middle"])
def test_f4_inside_chain_across_a_zero_fill_middle_is_not_counted(
    by_hand: tuple[int, ...],
) -> None:
    """4 -> 0 -> 10 inside the lookback is 10 or 14 by the reading, so nothing is closed."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(venue, "4", "0", "10", by_hand=by_hand)
    venue.hold("SPY", 10, client_order_id=None)
    assert_unknown_chain_frozen(venue)


def test_f4_inside_chain_does_not_sell_four_foreign_shares_by_counting_four_twice() -> None:
    """The same 4 -> 0 -> 10 chain cannot authorize a close of 14 held."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(venue, "4", "0", "10")
    venue.hold("SPY", 10, client_order_id=None)
    venue.hold("SPY", 4, client_order_id=standins.FOREIGN[0])
    resting = resting_order(venue)
    report = standins.close(venue)
    assert not report.failed and not report.errors, report.lines()
    assert (venue.sent, venue.canceled, venue.held["SPY"], set(report.frozen)) == (
        [],
        [resting],
        Decimal(14),
        {"SPY"},
    ), report.lines()
    # Turned by the owning lane after the tester's hand, on the lead's ruling of 2026-11-13: the reason
    # no longer names 10 as what ours opened, since that count is not established. The tester's line
    # was: any("our orders opened 10" in reason ...).
    assert any("not established" in reason for reason in report.frozen["SPY"]), report.lines()


@pytest.mark.parametrize(
    "fills",
    [("4", "0", "4"), ("4", "4", "4")],
    ids=["zero-middle", "equal-positive-fills"],
)
def test_f4_cycle_freezes_instead_of_establishing_four_or_zero(
    fills: tuple[str, ...],
) -> None:
    """A closed cycle has no start; equal positive counts isolate it from F3."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(venue, *fills, last="replaced")
    first, last = venue.history[0], venue.history[-1]
    first.replaces = last.id
    last.replaced_by = first.id
    venue.hold("SPY", 4, client_order_id=standins.FOREIGN[0])
    assert_unknown_chain_frozen(venue)


@pytest.mark.parametrize(
    "fills",
    [("8", "0", "5"), ("4", "8", "0", "5")],
    ids=["decrease-across-zero", "intermediate-peak-before-zero"],
)
def test_f4_decreasing_fills_across_a_zero_link_freeze_and_cancel_ours(
    fills: tuple[str, ...],
) -> None:
    """The last five is below the earlier eight, even when the chain began at four."""
    venue = standins.ReplacedNotListedOpen(standins.REGULAR)
    standins.chain(venue, *fills)
    venue.hold("SPY", 5, client_order_id=None)
    venue.hold("SPY", 8, client_order_id=standins.FOREIGN[0])
    assert_unknown_chain_frozen(venue)
