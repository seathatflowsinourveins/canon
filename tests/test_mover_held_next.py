"""L5 second-reader probes over the pinned emulator contract and existing offline seams.

Cases from sr-c-337, sr-a-337, sr-c-343 and sr-a-343; no account or network access.
"""

from collections import deque
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Price

from canon_trading import mover
from canon_trading import session_window
from canon_trading.session_window import NEW_YORK
from canon_trading.session_window import WindowConfig
from tests.test_mover_profile import ProfileSeam
from tests.test_mover_profile import held_up_to_10_92
from tests.test_mover_profile import holding
from tests.test_mover_trail import AAA
from tests.test_mover_trail import MINUTE
from tests.test_mover_trail import SECOND
from tests.test_mover_trail import T0
from tests.test_mover_trail import Seam
from tests.test_mover_trail import bar_type
from tests.test_mover_trail import seam_slot
from tests.test_session_window import CALENDAR
from tests.test_session_window import Account
from tests.test_session_window import clock
from tests.test_trail import distance
from tests.test_trail import profile_document

D = Decimal


def test_profile_after_warm_keeps_the_reached_limit_and_cold_exit_bounds():
    seam = ProfileSeam(
        "10.4200",
        trail_mode="profile",
        exit_through=D("0.001"),
        chase_cap=D("0.005"),
    )
    slot = seam_slot(seam)
    slot.bar_type = bar_type(AAA)
    slot.closes = deque([D(10)], maxlen=4)
    slot.dollars = deque(maxlen=5)
    slot.run_open_ns = T0
    slot.atr = cast(Any, SimpleNamespace(initialized=True, value=0.01))
    slot.entry_record = {"client_order_id": "E-1", "trail_changes": []}
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.02"),
        D("0.001"),
        D("0.005"),
        None,
        D("0.005"),
        "fixed",
        source="cold_atr_fallback",
    )
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.4200"),
        ask_price=Price.from_str("10.4250"),
    )
    segment = seam.trail_profile["symbols"]["AAA"]["recent"][60]
    segment["true_range"]["p75"] = 0.005
    seam._protect(slot, D(5), T0)

    # A previous high of 10.60 left these prices before the bid pulled back.
    seam.sent[-1].trigger_price = Price.from_str("10.4000")
    seam.sent[-1].price = Price.from_str("10.3900")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.trail_plan.trail == D("0.010")
    assert slot.trail_plan.exit_through == D("0.001")
    assert slot.trail_plan.chase_cap == D("0.005")
    assert seam.sent[-1].price.as_decimal() == D("10.39")

    # A later minute's profile narrows the trail again; protection must not loosen.
    segment["true_range"]["p75"] = 0.003
    slot.run_open_ns = T0 + 2 * MINUTE
    seam._protect(slot, D(5), T0 + 3 * MINUTE)
    assert slot.trail_plan.trail == D("0.007")
    assert seam.sent[-1].price.as_decimal() >= D("10.39")
    assert slot.trail_plan.exit_through <= D("0.001")
    assert slot.trail_plan.chase_cap <= D("0.005")


def test_a_cold_profile_holding_evaluates_the_warming_bar_once_using_its_own_segment():
    seam, slot = holding(profile_floor="off")
    slot.trail_plan = slot.initial_trail = replace(
        slot.trail_plan, bound="fixed", source="cold_atr_fallback", profile=None
    )
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.003
    seam.trail_profile["symbols"]["AAA"]["recent"][61]["true_range"]["p75"] = 0.007
    slot.run_open_ns = T0 + 14 * MINUTE  # 11:14 NY (segment 60), closing at 11:15 NY (61)
    seam._protect(slot, D(5), T0 + 15 * MINUTE)
    seam._protect(slot, D(5), T0 + 15 * MINUTE + SECOND)
    assert len(seam.sent) == 2
    assert seam.sent[-1].trailing_offset == D("0.06")
    assert len(slot.entry_record["trail_changes"]) == 1
    assert slot.entry_record["trail_changes"][0]["profile"]["segment"] == 60
    assert slot.entry_record.get("profile_skips", []) == []


@pytest.mark.parametrize("selected", ["0.016", "0.02"])
def test_the_warm_profile_uses_the_completed_close_and_marks_even_a_kept_plan(selected):
    seam, slot = holding(profile_floor="off")
    slot.trail_plan = slot.initial_trail = replace(
        slot.trail_plan, bound="fixed", source="cold_atr_fallback", profile=None
    )
    slot.closes = deque([D(5)], maxlen=4)
    slot.atr = cast(Any, SimpleNamespace(initialized=True, value=float(D(selected) * D(5) / 2)))
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.001
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.trail_plan.trail == D(selected)
    assert slot.profile_evaluated_ns == slot.run_open_ns
    changes = list(slot.entry_record["trail_changes"])
    sent = len(seam.sent)
    seam._protect(slot, D(5), T0 + 2 * MINUTE + SECOND)
    assert slot.entry_record["trail_changes"] == changes
    assert slot.entry_record.get("profile_skips", []) == []
    assert len(seam.sent) == sent


def test_a_zero_completed_close_keeps_the_cold_sell_and_retries_when_usable():
    seam, slot = holding(profile_floor="off")
    cold = replace(slot.trail_plan, bound="fixed", source="cold_atr_fallback", profile=None)
    slot.trail_plan = slot.initial_trail = cold
    slot.closes = deque([D(0)], maxlen=4)
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.trail_plan is cold and slot.warm_checked is False
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.profile_evaluated_ns == slot.run_open_ns
    slot.closes.append(D(10))
    seam._protect(slot, D(5), T0 + 2 * MINUTE + SECOND)
    assert slot.warm_checked is True and slot.tightened is True
    assert len(seam.sent) == 2


def test_profile_and_reentry_flags_are_refused_without_mover_and_named_when_auto_skips_them(
    monkeypatch, tmp_path, capsys
) -> None:
    # Every writer in _main uses this --log-dir; run_window and the account/clock are stubs.
    profile_path = tmp_path / "trail-profile.json"
    with pytest.raises(ValueError, match="trail_profile_path"):
        WindowConfig(strategy="SessionMaCross", trail_profile_path=profile_path, log_dir=tmp_path)
    for flag, value in (
        ("--profile-k", "3"),
        ("--profile-pctl", "p90"),
        ("--profile-floor", "off"),
        ("--trail-profile-path", str(profile_path)),
    ):
        with pytest.raises(SystemExit) as stopped:
            session_window._args([flag, value, "--log-dir", str(tmp_path)])
        assert stopped.value.code == 2
    at = datetime(2026, 11, 10, 10, 0, tzinfo=NEW_YORK)
    monkeypatch.setattr(session_window, "paper_trading", lambda: Account())
    monkeypatch.setattr(
        session_window, "session_now", lambda _: (clock(at, is_open=True), CALENDAR, 0.0)
    )
    monkeypatch.setattr(
        session_window,
        "run_window",
        lambda *a, **k: (_ for _ in ()).throw(session_window.WindowSkipped("stop")),
    )
    capsys.readouterr()
    assert (
        session_window.main(
            [
                "--session",
                "auto",
                "--mover",
                "--reentry-arm",
                "turn",
                "--profile-k",
                "3",
                "--trail-profile-path",
                str(profile_path),
                "--log-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "--reentry-arm turn" in out
    assert "--profile-k 3" in out
    assert f"--trail-profile-path {profile_path}" in out


@pytest.mark.parametrize("bad_atr", [D("NaN"), D("Infinity"), D("-Infinity"), D("-0.001")])
def test_profile_distance_refuses_non_finite_or_negative_atr_by_value_error(bad_atr):
    doc = profile_document()
    with pytest.raises(ValueError, match="atr"):
        distance(doc, atr=bad_atr)
    doc["symbols"]["AAA"]["recent"][54]["sessions"] = 4
    with pytest.raises(ValueError, match="atr"):
        distance(doc, atr=bad_atr)


def test_the_rows_table_names_profile_and_the_opening_rows():
    rows = (Path(__file__).parents[1] / "docs/handbook/rows.md").read_text()
    trail_mode = next(row for row in rows.splitlines() if row.startswith("| `TRAIL_MODE`"))
    assert "profile" in trail_mode
    for name in ("OPENING_ARM", "OPENING_MINUTES", "OPENING_F"):
        assert any(row.startswith(f"| `{name}`") for row in rows.splitlines()), name


@pytest.mark.parametrize("refused", [True, False], ids=["refused", "closed"])
def test_a_refused_remake_after_a_held_stop_keeps_the_held_level(refused):
    seam, slot = held_up_to_10_92()
    seam.bid = "11.20"
    seam._protect(slot, D(15), T0 + 3 * MINUTE + 2 * SECOND)
    remade = seam.sent[-1]
    assert (remade.kind, remade.trigger_price) == ("TRAILING", Price.from_str("10.9200"))

    if refused:
        seam.refuse(remade)
    else:
        remade.is_closed, remade.is_open, remade.is_emulated = True, False, False
    sent = len(seam.sent)
    seam.bid = "10.85"
    seam._protect(slot, D(15), T0 + 3 * MINUTE + 7 * SECOND)
    assert [o.kind for o in seam.sent[sent:]] == ["LIMIT"]
    assert slot.exiting is True
    assert slot.exit_trigger == D("10.92")


def test_a_return_through_the_held_stop_records_the_composed_followed_price_sale():
    # sr-c-343 P2's former "retained" reason was superseded by the earlier followed-price check.
    seam, slot = held_up_to_10_92()
    counts_before = dict(seam.protect_paths)
    sent = len(seam.sent)
    seam.bid = "10.85"
    seam._protect(slot, D(15), T0 + 3 * MINUTE + 2 * SECOND)
    assert [order.kind for order in seam.sent[sent:]] == ["LIMIT"]
    assert slot.exit_trigger == D("10.92")
    assert slot.exit_record.trigger_reason == "held stop (followed price)"
    assert slot.entry_record["protect_path"] == "held_stop"
    assert seam.protect_paths["held_stop"] == counts_before["held_stop"] + 1
    assert seam.protect_paths["trail"] == counts_before["trail"]


def test_the_composed_followed_price_sale_counts_only_the_held_stop_path():
    # sr-a-343 P2-3: keep every path assertion under the composed tip's more specific reason.
    seam, slot = held_up_to_10_92()
    seam.bid = "10.85"
    seam._protect(slot, D(15), T0 + 3 * MINUTE + 2 * SECOND)
    assert slot.exit_record.trigger_reason == "held stop (followed price)"
    assert slot.entry_record["protect_path"] == "held_stop"
    assert seam.protect_paths == {"held_stop": 1, "unpriced_exit": 0, "trail": 0}


def test_the_composed_lowest_tick_hold_has_no_zero_stop_or_zero_trigger():
    # sr-a-343 P3-1's zero-stop setup is now refused before a trailing order can be seeded.
    seam = cast(Any, Seam("0"))
    slot = seam_slot(seam, bought="0.0001")
    seam._protect(slot, D(5), T0)
    assert slot.held_stop is None

    seam.bid = "0.0002"
    seam._protect(slot, D(5), T0 + SECOND)
    # The composed floor unit also holds while the limit offset leaves no room above zero.
    assert seam.sent == []
    assert slot.held_stop is None and slot.held_since == T0
    assert seam.sell_triggers == {}
    seam.bid = "0.0101"  # room for the existing cent-rounded limit offset
    seam._protect(slot, D(5), T0 + 2 * SECOND)
    assert [o.kind for o in seam.sent] == ["TRAILING"]
    assert seam.sent[0].trigger_price is None
    assert seam.sell_triggers == {}


@pytest.mark.parametrize("bid", ["0", "10.60"], ids=["held", "trailed"])
@pytest.mark.parametrize(
    ("second_price", "average"), [("10.00", "10.25"), ("10.36", "10.43")], ids=["below", "at"]
)
def test_a_cheaper_second_entry_keeps_the_stop_and_sells_when_the_holding_average_crosses(
    bid, second_price, average
):
    """E-1 at 10.50 then E-2 at 10.00: the combined 10.25 average is below the 10.43 stop."""
    seam = cast(Any, ProfileSeam(bid))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    slot.closes = deque([D(11)], maxlen=4)  # no market price has crossed the held stop
    slot.entry_record = {"client_order_id": "E-1"}
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    if bid == "0":
        assert slot.held_stop == D("10.43")
    else:
        assert seam.sent[-1].trigger_price.as_decimal() == D("10.43")

    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=D(second_price), is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D(average))]
    sent = len(seam.sent)
    seam._protect(slot, D(2), T0 + SECOND)

    assert slot.exiting is True
    assert slot.protection_anchor == D(average)
    assert slot.exit_trigger == D("10.43")
    (sale,) = seam.sent[sent:]
    assert sale.kind == "LIMIT" and sale.quantity.as_decimal() == D(2)
    assert D(seam.sell_triggers[sale.client_order_id.value]) == D("10.43")


@pytest.mark.parametrize("bid", ["0", "10.60"], ids=["held", "trailed"])
def test_a_late_first_entry_raises_the_stop_from_the_combined_holding_average(bid):
    """E-2 at 10.00 then late E-1 at 10.50: the holding's 10.25 average puts L at 10.18."""
    seam = cast(Any, ProfileSeam(bid))
    slot = seam_slot(seam, bought=None)
    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=10.00, is_closed=True)
    slot.sized_loss = D("0.07")
    slot.closes = deque([D(11)], maxlen=4)
    slot.entry_record = {"client_order_id": "E-2"}
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.00"))]
    seam._protect(slot, D(1), T0)
    if bid == "0":
        assert slot.held_stop == D("9.93")
    else:
        assert seam.sent[-1].trigger_price.as_decimal() == D("9.93")

    seam.orders["E-1"] = SimpleNamespace(avg_px=10.50, is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.25"))]
    seam._protect(slot, D(2), T0 + SECOND)

    assert slot.protection_anchor == D("10.25")
    assert slot.exiting is False
    if bid == "0":
        assert seam.sent == [] and slot.held_stop == D("10.18")
        seam.bid = "10.60"
        seam._protect(slot, D(2), T0 + 2 * SECOND)
    sale = seam.sent[-1]
    assert sale.kind == "TRAILING" and sale.quantity.as_decimal() == D(2)
    assert sale.trigger_price.as_decimal() == D("10.18")
    assert sale.trailing_offset == D("0.07")


@pytest.mark.parametrize("bid", ["0", "10.60"], ids=["held", "trailed"])
def test_a_cheaper_entry_above_the_stop_preserves_the_level_without_selling(bid):
    seam = cast(Any, ProfileSeam(bid))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    slot.closes = deque([D(11)], maxlen=4)
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=D("10.48"), is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.49"))]
    seam._protect(slot, D(2), T0 + SECOND)
    assert slot.exiting is False
    assert slot.protection_anchor == D("10.49")
    if bid == "0":
        assert seam.sent == [] and slot.held_stop == D("10.43")
    else:
        assert seam.sent[-1].kind == "TRAILING"
        assert seam.sent[-1].trigger_price.as_decimal() == D("10.43")


def test_a_stop_rising_above_an_unchanged_holding_average_does_not_sell():
    seam = cast(Any, ProfileSeam("11.00"))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    seam.sent[-1].trigger_price = Price.from_str("10.9000")
    seam._protect(slot, D(1), T0 + SECOND)
    assert slot.exiting is False and len(seam.sent) == 1
    assert seam.canceled == []


def test_a_lower_holding_average_above_the_reached_trigger_on_the_step_keeps_trailing():
    seam = cast(Any, ProfileSeam("11.00"))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    seam.sent[-1].trigger_price = Price.from_str("10.4380")  # retained on the step at 10.43
    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=D("10.37"), is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.435"))]
    seam._protect(slot, D(2), T0 + SECOND)
    assert slot.exiting is False
    assert seam.sent[-1].kind == "TRAILING"
    assert seam.sent[-1].trigger_price.as_decimal() == D("10.43")


def test_a_trailed_winner_is_not_sold_by_a_slightly_lower_holding_average():
    # The trail has risen far above the holding's average; a cheaper fill crosses no stop.
    seam = cast(Any, ProfileSeam("11.00"))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    seam.sent[-1].trigger_price = Price.from_str("10.9000")
    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=D("10.48"), is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.49"))]
    seam._protect(slot, D(2), T0 + SECOND)
    assert slot.exiting is False
    # the trail is remade for the whole holding at the level it reached, never a limit sale
    assert [order.kind for order in seam.sent] == ["TRAILING", "TRAILING"]
    assert seam.sent[-1].quantity == 2
    assert seam.sent[-1].trigger_price.as_decimal() == D("10.90")


def test_a_one_entry_holding_sells_when_its_own_cheaper_partial_fill_crosses_the_stop():
    # The crossing rule is any fill's (the lead's H3): one entry order filled twice, the second
    # part cheaper, brings the holding's average from 10.50 to 10.35, under its kept 10.43, so it
    # sells at once (r-dsh-heldnext P3-1, its drop-in inverted to the rule).
    seam = cast(Any, ProfileSeam("10.60"))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    slot.entry_record = {"client_order_id": "E-1"}
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    assert seam.sent[-1].kind == "TRAILING"
    assert seam.sent[-1].trigger_price.as_decimal() == D("10.43")

    seam.orders["E-1"].avg_px = 10.35
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.35"))]
    seam._protect(slot, D(2), T0 + SECOND)

    assert slot.exiting is True
    assert [order.kind for order in seam.sent] == ["TRAILING", "LIMIT"]
    assert seam.sent[-1].quantity == 2
    assert slot.entry_record.get("held_stop_trigger") == "holding average"


def test_a_stop_kept_exactly_at_the_anchor_is_not_crossed_by_a_lower_average():
    # A stop already at the average is a winner's stop: a lower average crosses nothing.
    seam = cast(Any, ProfileSeam("11.00"))
    slot = seam_slot(seam, bought="10.50")
    slot.sized_loss = D("0.07")
    seam.positions = [SimpleNamespace(signed_qty=D(1), avg_px_open=D("10.50"))]
    seam._protect(slot, D(1), T0)
    seam.sent[-1].trigger_price = Price.from_str("10.5000")
    slot.entry_id = ClientOrderId("E-2")
    seam.orders["E-2"] = SimpleNamespace(avg_px=D("10.48"), is_closed=True)
    seam.positions = [SimpleNamespace(signed_qty=D(2), avg_px_open=D("10.49"))]
    seam._protect(slot, D(2), T0 + SECOND)
    assert slot.exiting is False
    assert [order.kind for order in seam.sent] == ["TRAILING", "TRAILING"]
    assert seam.sent[-1].quantity == 2
    assert seam.sent[-1].trigger_price.as_decimal() == D("10.50")


def test_same_quantity_after_late_sale_and_buy_uses_the_new_holding_average():
    from decimal import Decimal
    from types import SimpleNamespace

    from nautilus_trader.model import ClientOrderId
    from nautilus_trader.model import OrderSide
    from nautilus_trader.model import Price
    from nautilus_trader.model import Quantity

    from canon_trading import mover
    from tests.test_mover import SECOND
    from tests.test_mover import T0
    from tests.test_mover_late_fill_blind import LateFillSeam
    from tests.test_mover_late_fill_blind import entry

    D = Decimal
    host = LateFillSeam()
    host.bid = "10.00"
    slot, buy = entry(host)
    buy.quantity = Quantity.from_int(7)
    buy.price = Price.from_str("11.0000")
    slot.sized_loss = D("0.07")
    seam = cast(Any, host)  # the driver's callback is swapped for one that sets the average first
    on_fill = seam.on_order_filled

    def fill_with_average(order, quantity, price, at, average):
        # Correct the driver's constant average before its real callback.
        def receive(event):
            host.positions[0].avg_px_open = D(average)
            on_fill(event)

        seam.on_order_filled = receive
        try:
            host.fill(order, quantity, price, at)
        finally:
            seam.on_order_filled = on_fill

    fill_with_average(buy, 5, "10.0000", T0, "10.00")
    host.step(T0 + SECOND)
    assert host.submitted[-1].trigger_price.as_decimal() == D("9.93")

    # A canceled venue limit from an earlier holding can report a late fill
    # against the current net position. It is not the current emulated stop.
    old_sale = SimpleNamespace(**vars(buy))
    old_sale.client_order_id = ClientOrderId("OLDER-VENUE-SALE")
    old_sale.side = OrderSide.SELL
    old_sale.quantity = Quantity.from_int(2)
    old_sale.filled_qty = Quantity.from_int(0)
    old_sale.avg_px = None
    old_sale.price = Price.from_str("10.2000")
    old_sale.ts_init = T0 - SECOND
    old_sale.is_closed, old_sale.is_open = True, False
    old_sale.is_emulated = False
    old_sale.status = mover.OrderStatus.CANCELED
    host.orders[old_sale.client_order_id.value] = old_sale

    fill_with_average(old_sale, 2, "10.2000", T0 + SECOND + 100_000_000, "10.00")
    fill_with_average(buy, 2, "11.0000", T0 + SECOND + 200_000_000, "10.40")
    assert host.positions[0].signed_qty == D(5)
    assert host.positions[0].avg_px_open == D("10.40")
    before = len(host.submitted)
    host.step(T0 + 2 * SECOND)

    assert slot.exiting, "bid 10.00 is through the new average-minus-L stop"
    assert slot.exit_trigger == D("10.33")
    assert [(order.kind, order.quantity.as_decimal()) for order in host.submitted[before:]] == [
        ("LIMIT", D(5))
    ]
