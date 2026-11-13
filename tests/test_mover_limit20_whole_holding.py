"""Limit 20: a default window's warmed trail protects the whole holding's cost basis.

Contract: the task's one-holding/one-stop ruling; NautilusTrader d53849b216e2,
crates/model/src/position.rs:1050-1065 computes the quantity-weighted average.
Its execution/src/trailing.rs:61-100 seeds and raises a sell's trigger and limit.
Synthetic fills use a real Position through a cache view, with test_mover's native
offline emulator. No real account, catalog, ledger, or paper-state path is used.
"""

from collections import deque
from decimal import ROUND_CEILING
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.core import UUID4
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import AccountId
from nautilus_trader.model import Bar
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Position
from nautilus_trader.model import PositionId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId

from canon_trading import mover
from canon_trading import mover_window
from canon_trading import session_window
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import MOVER_ID
from tests.test_mover import SECOND
from tests.test_mover import T0
from tests.test_mover import USD
from tests.test_mover import Recording
from tests.test_mover import at
from tests.test_mover import bar_type
from tests.test_mover import engine
from tests.test_mover import run
from tests.test_mover_positive_exits import quote_at

D = Decimal


class TwoFillHolding(Recording):
    """Inject late fills at the existing event seam; all protection stays native.

    Two distinct entry orders are necessary: partial fills of just one order give
    its own average the same value as the holding's and cannot expose limit 20.
    The second fill represents an earlier entry's delayed fill into this slot.
    """

    later = False
    """Whether the second fill comes minutes after the warmed tighten, not in its step."""

    def __init__(self, config: mover.MoverConfig) -> None:
        super().__init__(config)
        self.entry_orders: dict[str, SimpleNamespace] = {}
        self.native_holding: Position | None = None
        self.second_after_warm = False
        self.stop_after_warm: Any = None

    @property
    def cache(self) -> Any:
        # The cache-view seam is also used by test_mover_warm_atr_blind.Probe.
        # Python's shared Cache is read-only for positions at this pin; expose
        # the actual Position while keeping quotes, orders and instruments native.
        native = super().cache
        holding = self.native_holding

        class View:
            def positions_open(self, **filters: Any) -> list[Position]:
                assert filters == {"instrument_id": AAA, "strategy_id": MOVER_ID}
                return [] if holding is None else [holding]

            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

        return View()

    def _order(self, client_order_id: ClientOrderId | None) -> Any:
        if client_order_id is not None and client_order_id.value in self.entry_orders:
            return self.entry_orders[client_order_id.value]
        return super()._order(client_order_id)

    def receive_buy(self, name: str, price: str, when: int) -> None:
        fill = OrderFilled(
            trader_id=TraderId("TESTER-001"),
            strategy_id=MOVER_ID,
            instrument_id=AAA,
            client_order_id=ClientOrderId(name),
            venue_order_id=VenueOrderId(name),
            account_id=AccountId("ALPACA-001"),
            trade_id=TradeId(name),
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            last_qty=Quantity.from_int(5),
            last_px=Price.from_str(price),
            currency=USD,
            liquidity_side=LiquiditySide.TAKER,
            event_id=UUID4(),
            ts_event=when,
            ts_init=when,
            reconciliation=False,
            position_id=PositionId("AAA.ALPACA-Mover-001"),
            commission=Money(0, USD),
        )
        self.entry_orders[name] = SimpleNamespace(
            client_order_id=fill.client_order_id,
            avg_px=D(price),
            filled_qty=fill.last_qty,
            is_closed=True,
            is_open=False,
        )
        if self.native_holding is None:
            instrument = self.cache.instrument(AAA)
            assert instrument is not None
            self.native_holding = Position(instrument=instrument, fill=fill)
        else:
            self.native_holding.apply(fill)
        self.on_order_filled(fill)

    def on_start(self) -> None:
        # External completed bars supply the same on_bar seam as the window's
        # internal minute bars, without opening history or market-data clients.
        slot = mover.Slot(AAA, bar_type(AAA), deque(maxlen=4), deque(maxlen=5))
        slot.atr = AverageTrueRange(self.settings.atr_n)
        self.slots[AAA] = slot
        slot.entry_id = ClientOrderId("ENTRY-CURRENT")
        slot.entry_record = {"client_order_id": "ENTRY-CURRENT", "entry_price": None}
        self.entries_sent.extend(
            [slot.entry_record, {"client_order_id": "ENTRY-EARLIER", "entry_price": None}]
        )
        slot.trail_plan = slot.initial_trail = self._entry_trail(
            slot, D("10.00"), quote_at("10.0000", "10.0100", 0)
        )
        assert slot.trail_plan.source == "cold_atr_fallback"
        slot.sized_loss = D("10.00") * slot.trail_plan.trail
        assert slot.sized_loss == D("0.20")
        self.receive_buy("ENTRY-CURRENT", "10.0000", T0)
        self.subscribe_quotes(AAA)
        self.subscribe_bars(slot.bar_type)
        super().on_start()

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)
        slot = self.slots[AAA]
        warm = slot.atr is not None and slot.atr.initialized
        due = not self.later or (slot.tightened and bar.ts_event >= T0 + 17 * MINUTE)
        if not self.second_after_warm and warm and due:
            # In its step, no protection step is interposed between warming and this fill.
            self.second_after_warm = True
            self.receive_buy("ENTRY-EARLIER", "10.4000", bar.ts_init)

    def _on_step(self, event: Any) -> None:
        super()._on_step(event)
        slot = self.slots[AAA]
        stop = self._order(slot.protect_id)
        if (
            self.second_after_warm
            and slot.tightened
            and stop is not None
            and stop.quantity.as_decimal() == 10
        ):
            self.stop_after_warm = stop


class LaterSecondFill(TwoFillHolding):
    later = True


@pytest.mark.parametrize("later", [False, True], ids=["in-the-warm-step", "minutes-after"])
def test_default_window_warmed_atr_stop_uses_the_whole_holding_average(
    tmp_path: Path, later: bool
) -> None:
    """Five at 10.00, then five at 10.40 after 14 bars, in the step the ATR warms or five minutes
    after its tighten; compare both native stop levels. A remake for a changed quantity trails by
    the distance in force, the warmed ATR's, and only the level moves with the average."""
    args = session_window._args(["--strategy", "Mover", "--mover", "--log-dir", str(tmp_path)])
    window = session_window.window_config(args, "regular")
    part = session_window.MOVER_PARTS["regular"]
    configured, _ = mover_window.mover_universe_parts(
        {AAA: D("10.00")},
        extended=False,
        feed=part.feed,
        wind_down_at=at(T0 + 30 * MINUTE),
        market_exit_interval_ms=session_window.MARKET_EXIT_INTERVAL_MS,
        market_exit_max_attempts=session_window.MARKET_EXIT_MAX_ATTEMPTS,
        slots=part.slots,
        entry_arm=session_window.mover_entry_arm("regular")[0],
        entry_notional=D(500),  # synthetic startup allocation; no account read
        size_from_budget=True,
        loss_share=session_window.window_loss_share("windows"),
        trail_cap=window.trail_cap,
        rank_max_age_secs=mover_window.RANK_MAX_AGE_SECS,
        trail_rows={name: getattr(window, name) for name in mover.TRAIL_SETTINGS},
        trigger=session_window.WINDOW_TRIGGERS[window.trigger],
    )
    configured.settings.load_instruments = True  # window_node's paper-window opt-in
    s = (LaterSecondFill if later else TwoFillHolding)(configured.settings)
    assert window.arm_default and window.arm == session_window.DEFAULT_ARM == "OWN1"
    assert (s.settings.trail_mode, s.settings.trail_cap, s.settings.atr_k) == ("atr", "own", D(1))
    assert s.settings.atr_n == 14 and s.settings.reentry_arm == "clock"
    assert s.settings.trigger == TriggerType.BID_ASK and not s.persistence
    assert s.settings.size_from_budget and s.settings.cooldown_secs == 300

    bars = [
        Bar(
            bar_type(AAA),
            Price.from_str("10.0000"),
            Price.from_str("10.1875"),
            Price.from_str("10.0000"),
            Price.from_str("10.0000"),
            Quantity.from_int(1000),
            T0 + minute * MINUTE,
            T0 + (minute + 1) * MINUTE,
        )
        for minute in range(19 if later else 14)
    ]
    # The highest delivered bid equals the resulting holding average, so market ratcheting
    # cannot obscure which cost basis priced the trail. Minutes after: the bid stays at 10.10
    # until the second fill, above the stop, and reaches 10.20 only after it.
    quotes = (
        [
            quote_at("10.1000", "10.1100", 14 * MINUTE - 1),
            quote_at("10.1000", "10.1100", 14 * MINUTE + 3 * SECOND),
            quote_at("10.2000", "10.2100", 19 * MINUTE + 3 * SECOND),
            quote_at("10.2000", "10.2100", 19 * MINUTE + 9 * SECOND),
        ]
        if later
        else [
            quote_at("10.2000", "10.2100", 14 * MINUTE - 1),
            quote_at("10.2000", "10.2100", 14 * MINUTE + 3 * SECOND),
        ]
    )
    data = [quote_at("10.0000", "10.0100", 0), *bars, *quotes]
    eng = engine()
    try:
        run(eng, s, None, data)
        assert s.second_after_warm and s.native_holding is not None
        assert len(s.fills) == 2 and all(fill[1] == "BUY" for fill in s.fills)
        assert s.native_holding.quantity.as_decimal() == D(10)
        average = s.native_holding.avg_px_open
        assert average == (D(5) * D("10.00") + D(5) * D("10.40")) / D(10) == D("10.20")
        slot = s.slots[AAA]
        assert slot.atr is not None and slot.atr.initialized
        atr = D(str(slot.atr.value))
        assert atr == D("0.1875")
        assert s.entry_orders["ENTRY-CURRENT"].avg_px == D("10.00") != average
        assert slot.tightened and slot.trail_plan is not None and slot.trail_plan.bound == "atr"
        stop = s.stop_after_warm
        assert stop is not None and stop.order_type == OrderType.TRAILING_STOP_LIMIT
        assert stop.is_emulated
        assert stop.quantity.as_decimal() == D(10)
        assert stop.trigger_price is not None and stop.price is not None
        assert not s.refused

        # OWN1: max(k * ATR, m * spread), then the existing cent-rounded offsets.
        # Compute the oracle from the holding average, never the entry's average
        # or the possibly mis-selected production TrailPlan.
        raw_distance = max(s.settings.atr_k * atr, s.settings.spread_floor_m * D("0.01"))
        fraction = raw_distance / average
        distance = (average * fraction).quantize(D("0.01"), rounding=ROUND_CEILING)
        limit_offset = (distance + average * fraction / 4).quantize(
            D("0.01"), rounding=ROUND_CEILING
        )
        expected = (average - distance, average - limit_offset)
        actual = (stop.trigger_price.as_decimal(), stop.price.as_decimal())
        assert expected == (D("10.01"), D("9.96"))
        assert actual == expected, (
            f"fills=5@10.00 + 5@10.40; average={average}; ATR={atr}; "
            f"whole-holding distance={distance}; limit offset={limit_offset}; "
            f"expected trigger/limit={expected}; actual trigger/limit={actual}; "
            f"actual offsets={(stop.trailing_offset, stop.limit_offset)}; "
            f"selected trail fraction={slot.trail_plan.trail}"
        )
    finally:
        eng.dispose()


def test_a_basis_under_a_dollar_puts_the_distance_on_the_step_at_the_average() -> None:
    """code-review-l5-latebuy2/F2: an entry filled under $1 in a holding averaged at $1 or more
    trails by a distance on the cent step, never a sub-cent one."""
    slot = mover.Slot(AAA, bar_type(AAA), deque(), deque())
    trailing, limit = mover.offsets_in_force(
        slot, D("1.04"), D("0.0473"), D("0.005"), None, basis=D("0.98")
    )
    assert trailing == D("0.05")
    assert trailing == trailing.quantize(D("0.01"), rounding=ROUND_CEILING)
    assert limit == limit.quantize(D("0.01"), rounding=ROUND_CEILING)
