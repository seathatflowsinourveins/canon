"""R3 diagnostic: ADR 0092's bar model, composed with the lead's 2026-11-12 R3 rows.

The protection implementation IS early_entry_study.protect, called with each plan's rows as
keywords; its default walk is unchanged for the other studies. No second OHLC walk exists.
Every figure is given under NautilusTrader's default bar path and its documented adaptive one
(docs/concepts/backtesting/bar-execution.md at v2.0.0rc8), as a range.
ATR is the pinned NautilusTrader AverageTrueRange(14), Wilder/previous-close defaults.
Its value and the price distance are frozen at each entry, after completed bars only.
Non-fixed release give = trail/4 per T1's registration; the chase cap is max(trail, 0.02),
as the Mover sets it (mover.py:2144, the coordinator's rule (ii) of 2026-11-12).

The 2026-11-12 R3 ruling registers k=1/2/3, a $1M/min baseline tier cut, initial
run_bars=2 (1/3 also reported), and h=0/5/25/100 bps per side, UNCALIBRATED.
There are no quotes: the spread floor is explicitly NOT applied. These hypothetical
bar arms are not executable Mover settings. No paper fill is read, no trial is ranked,
and per-symbol diagnostics never fit settings. Settings are chosen per tier only.

Catalog contract: NautilusTrader 2.0.0rc8 persistence/__init__.pyi query/get_intervals;
ADR 0092's close*volume ignition and covered minute reference. SQLite's bar_extras
schema was inspected read-only on 2026-11-12: (bar_type, ts_event) identifies the own
vwap. No typical-price substitute exists. Its absence refuses the VWAP arm by name.
Only development dates from the registered sessions file and their earlier lookback
dates can be read; 2026-10-16 onward is refused before constructing a catalog.

It is also the gate for the lead's three registered trail arms (F2, OWN1, OWN3) with fixed 3, 5 and
8% columns: every entry is sized by S1 (sizing.size_for_loss) for one planned loss from the
--slot-budget parameter (default 20000), and its entry and sale are charged on Alpaca's step. One
share per entry is left only to ReplayContext's callers with no slot budget, such as the tests.
Summed dollars without compounding, slots, tick rounding of the walk, fees, or liquidity/impact
simulation. h is charged on entry and exit prices. The walk is the same at every h, but the entry
guard and S1's size at the stepped entry can refuse a bar at one h and not another, and the arm then
scans on to a later bar. Shards (--shard k/n) partition the symbols and
--merge sums their tier aggregates into one summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from decimal import ROUND_FLOOR
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import Price
from nautilus_trader.model import Symbol
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import early_entry_study as study
from canon_trading.price_step import down_on_step
from canon_trading.price_step import entry_price
from canon_trading.price_step import stop_trigger_price
from canon_trading.price_step import tick
from canon_trading.sizing import size_for_loss

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Sequence

HOLDOUT_START = date(2026, 10, 16)
TIER_CUT = 1_000_000.0
HALF_SPREAD_BPS = (0, 5, 25, 100)
ORDERS = ("ohlc", "adaptive")
BIN_BPS = 10
BIN_EDGE_BPS = 10_000
EXITS = (
    "fixed",
    "atr-1",
    "atr-2",
    "atr-3",
    "tier",
    "fixed-3",
    "fixed-5",
    "fixed-8",
    "own-1",
    "own-3",
)
"""`fixed` is the gate's F2, `own-1` and `own-3` its OWN1 and OWN3: the ATR mode with the cap own."""
MAIN_FIXED = {"exit_through": 0.005, "chase_cap": 0.02}
"""Main's fixed mode sends the configured give and chase at any trail (`Mover._entry_trail`, mover.py:2089-2097;
`MoverConfig` defaults :660-661); only the ATR mode derives them from the trail (:2140-2145)."""
SLOT_BUDGET = Decimal(20_000)
"""The lead's slot parameter for the gate, in USD: one round figure of the order B's windows use."""
FLOW_SHARE, FLOW_BARS = Decimal("0.05"), 5
"""Main's `flow_share` and `flow_bars` defaults: the share of the last bars' dollar volume an entry may take."""
GATE_LOSS_TRAIL = Decimal("0.02")
"""The lead's A/B rule: the planned loss is the slot budget's shares times entry less the stepped 2% stop."""
CENT = Decimal("0.01")
TICK_BUCKETS = ("under 3", "3 to 5", "5 to 10", "over 10")
"""The lead's overnight breakdown (2026-11-12) of the trail's distance at entry in Alpaca's ticks: under
3, 3 to under 5, 5 to 10, over 10. An observation, not a registered row."""


def tick_bucket(ticks: float) -> str:
    if ticks < 3:
        return TICK_BUCKETS[0]
    if ticks < 5:
        return TICK_BUCKETS[1]
    return TICK_BUCKETS[2] if ticks <= 10 else TICK_BUCKETS[3]


SIZED = Equity(
    instrument_id=InstrumentId.from_str("R3.ALPACA"),
    raw_symbol=Symbol("R3"),
    currency=Currency.from_str("USD"),
    price_precision=4,
    price_increment=Price.from_str("0.0001"),
    ts_event=0,
    ts_init=0,
)
"""An Alpaca equity on the finest step S1 accepts; S1 reads only its quote currency and lot rules."""
REENTRIES = ("reclaim", *(f"turn-{k}-{d}" for k in (1, 2, 3) for d in (1, 2)), "vwap")


def guard_dates(days: Sequence[date]) -> None:
    if any(d >= HOLDOUT_START for d in days):
        raise ValueError("holdout refused: 2026-10-16 onward cannot be read by R3")


def read_sessions(path: Path) -> tuple[list[date], list[date]]:
    development, lookback = [], []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        fields = text.split()
        if len(fields) != 2 or fields[1] not in {"development", "lookback"}:
            raise ValueError(f"invalid session role on line {number}")
        d = date.fromisoformat(fields[0])
        guard_dates([d])
        (development if fields[1] == "development" else lookback).append(d)
    if not development or len(set(development + lookback)) != len(development + lookback):
        raise ValueError("development sessions missing or duplicate sessions")
    if lookback and max(lookback) >= min(development):
        raise ValueError("lookback must precede development")
    return sorted(development), sorted(lookback)


def baseline_per_minute(day: study.Day) -> float | None:
    sessions = day.earlier[: study.REGISTERED.baseline_sessions]
    if not sessions:
        return None
    # Full 04:00-16:00 clock minutes, including zero-trade minutes, not traded bars.
    return sum(sum(b.dollars for b in s) for s in sessions) / len(sessions) / 720


def population(day: study.Day, tier_cut: float = TIER_CUT) -> dict[str, Any]:
    baseline = baseline_per_minute(day)
    top = baseline is not None and baseline >= tier_cut
    ignition = None
    if day.reference and any(b.close >= day.reference * 1.03 for b in day.bars):
        ignition = study.first_listed(study.listing(day, study.REGISTERED))
    return {
        "selected": top or ignition is not None,
        "top_tier": top,
        "tier": "top" if top else ("other" if baseline is not None else "unknown"),
        "baseline_dollars_per_minute": baseline,
        "ignition": ignition,
    }


def plan(fraction: float) -> dict[str, float]:
    """A trail's rows as the Mover sets them: the give a quarter of the trail, and the chase the
    configured depth under a narrow trail, max(trail, chase_cap) (mover.py:2144, rule (ii))."""
    return {
        "trail": fraction,
        "exit_through": fraction / 4,
        "chase_cap": max(fraction, study.CHASE_CAP),
    }


def protection(
    bars: Sequence[study.Minute],
    entry: float,
    bid: float,
    costs: study.Costs,
    fraction: float,
    *,
    adaptive: bool = False,
    rows: dict[str, float] | None = None,
) -> study.Exit | None:
    """The study's own walk with the trail's rows; a released limit never filled is an exit."""
    return study.protect(
        bars, entry, bid, costs, **(rows or plan(fraction)), adaptive=adaptive, unfilled=True
    )


def atr_values(bars: Sequence[study.Minute]) -> tuple[float | None, ...]:
    indicator = AverageTrueRange(14)
    out = []
    for b in bars:
        indicator.update_raw(b.high, b.low, b.close)
        out.append(indicator.value if indicator.initialized else None)
    return tuple(out)


def trail_fraction(exit_arm: str, entry: float, atr: float | None, top: bool) -> float | None:
    """The arm's trail as a fraction of `entry`. ATR arms cap at main's 0.02; `own-k` keeps its own
    width (the cap arm own, as S2 builds it, not on main until S2 lands). A cold ATR, fewer than 14
    complete bars, takes main's fixed plan (`cold_atr_fallback`). A zero ATR has no trail: main would
    take the spread floor, and there are no quotes here."""
    if exit_arm == "fixed":
        return 0.02
    if exit_arm.startswith("fixed-"):
        return int(exit_arm.removeprefix("fixed-")) / 100
    if exit_arm == "tier" and top:
        return 0.002
    if atr is None:
        return 0.02
    if atr <= 0:
        return None
    if exit_arm.startswith("own-"):
        return int(exit_arm.removeprefix("own-")) * atr / entry
    k = 1 if exit_arm == "tier" else int(exit_arm.removeprefix("atr-"))
    return min(0.02, k * atr / entry)


def cold(exit_arm: str, atr: float | None, top: bool) -> bool:
    """Whether the arm's trail at this bar is main's fixed plan for want of a warm ATR."""
    return atr is None and not exit_arm.startswith("fixed") and not (exit_arm == "tier" and top)


def session_vwap(bars: Sequence[study.Minute], own_vw: dict[int, float]) -> tuple[float, ...]:
    dollars, volume, out = 0.0, 0.0, []
    for b in bars:
        if b.volume > 0:
            vw = own_vw.get(b.start)
            if vw is None or not math.isfinite(vw) or vw <= 0:
                raise ValueError(f"vwap refused: missing own vw at {study.clock(b.start)}")
            dollars += vw * b.volume
            volume += b.volume
        out.append(dollars / volume if volume else float("nan"))
    return tuple(out)


def tally(result: dict[str, Any]) -> None:
    """Count a replay's skips by kind: the guard's, and S1's refusals by their names."""
    result["guard_skips_count"] = sum(s["kind"] == "guard_skip" for s in result["guard_skips"])
    refusals: dict[str, int] = {}
    for skip in result["guard_skips"]:
        if skip["kind"] == "size_refused":
            refusals[skip["reason"]] = refusals.get(skip["reason"], 0) + 1
    result["size_refusals"] = refusals


@dataclass(frozen=True)
class Signal:
    index: int
    depth_trails: float = 0.0
    fall_minutes: int = 0
    turn_minutes: int = 0


def reentry_signal(
    bars: Sequence[study.Minute],
    sold_index: int,
    sale: float,
    distance: float,
    arm: str,
    vwap: Sequence[float] | None,
    eligible: Callable[[int], bool] | None = None,
) -> Signal | None:
    """The sale-bar close seeds the low; entries start strictly on later bar closes."""
    low, low_index, rises = bars[sold_index].close, sold_index, 0
    below = vwap is not None and bars[sold_index].close < vwap[sold_index]
    k, d = (int(x) for x in arm.split("-")[1:]) if arm.startswith("turn-") else (0, 0)
    for i in range(sold_index + 1, len(bars)):
        b = bars[i]
        if b.close < low:
            low, low_index = b.close, i
        rises = rises + 1 if b.close > bars[i - 1].close else 0
        depth = max(0.0, sale - low) / distance
        enter = arm == "reclaim" and b.close > sale
        if arm.startswith("turn-"):
            enter = rises >= k and depth >= d
        elif arm == "vwap" and vwap is not None:
            enter = below and b.close > vwap[i]
            below = below or b.close < vwap[i]
        if enter and (eligible is None or eligible(i)):
            return Signal(
                i,
                depth,
                bars[low_index].start - bars[sold_index].start,
                b.start - bars[sold_index].start,
            )
    return None


def opening_signal(
    bars: Sequence[study.Minute],
    minutes: int,
    fall: int,
    k: int,
    distances: Sequence[float | None],
    eligible: Callable[[int], bool] | None = None,
) -> Signal | None:
    indices = [i for i, b in enumerate(bars) if 570 <= b.start and b.start + 1 <= 570 + minutes]
    if not indices or bars[indices[0]].start != 570:
        return None
    first = indices[0]
    opened, low, low_i, rises = bars[first].open, bars[first].close, first, 0
    for i in indices[1:]:
        if bars[i].close < low:
            low, low_i = bars[i].close, i
        rises = rises + 1 if bars[i].close > bars[i - 1].close else 0
        distance = distances[i]
        if (
            distance
            and rises >= k
            and (opened - low) / distance >= fall
            and (eligible is None or eligible(i))
        ):
            return Signal(
                i, (opened - low) / distance, bars[low_i].start + 1 - 570, bars[i].start + 1 - 570
            )
    return None


def opening_diagnostic(bars: Sequence[study.Minute], minutes: int) -> dict[str, Any]:
    window = [b for b in bars if 570 <= b.start and b.start + 1 <= 570 + minutes]
    if not window or window[0].start != 570:
        return {"refusal": "no 09:30 opening bar"}
    low_i = min(range(len(window)), key=lambda i: window[i].low)
    low = window[low_i]
    # OHLC has the low before the close: this low's own close can participate in the rise.
    later = [low.close, *(b.high for b in window[low_i + 1 :])]
    return {
        "minutes": minutes,
        "open": window[0].open,
        "low": low.low,
        "low_minute": low.start + 1,
        "depth_dollars": window[0].open - low.low,
        "depth_fraction": (window[0].open - low.low) / window[0].open,
        "rise_after_low_dollars": max(later) - low.low,
        "rise_after_low_fraction": max(later) / low.low - 1,
    }


class ReplayContext:
    """One symbol-day: causal indicators and sale results shared across diagnostic arms."""

    def __init__(
        self,
        day: study.Day,
        own_vw: dict[int, float],
        top: bool,
        *,
        slot_budget: Decimal | None = None,
    ):
        self.day, self.bars, self.top = day, day.bars, top
        self.slot_budget = slot_budget
        self._shares: dict[tuple[str, int, int], tuple[int, str] | str] = {}
        self.atr = atr_values(self.bars)
        self.seen = study.listing(day, study.REGISTERED)
        # Whether the name had listed by each bar's close: a gate may read this, never the day's
        # first listing, which is known only once the day is over.
        listed_by, so_far = [], False
        for seen in self.seen:
            so_far = so_far or seen.listed
            listed_by.append(so_far)
        self.listed_by = tuple(listed_by)
        self.vwap, self.vwap_refusal = None, None
        try:
            self.vwap = session_vwap(self.bars, own_vw)
        except ValueError as error:
            self.vwap_refusal = str(error)
        self.paths: dict[tuple[str, int, bool], dict[str, Any]] = {}
        self._distances: dict[str, tuple[float | None, ...]] = {}
        self.initials: dict[tuple[str, int, int, tuple[int, int] | None], Signal | None] = {}
        self.initial_skips: dict[
            tuple[str, int, int, tuple[int, int] | None], tuple[dict[str, Any], ...]
        ] = {}
        self.next_signals: dict[
            tuple[str, int, int, float, float, str],
            tuple[Signal | None, tuple[dict[str, Any], ...]],
        ] = {}

    def rows(self, exit_arm: str, fraction: float) -> dict[str, float]:
        """The rows main sends with this trail: its fixed mode's configured give and chase for a fixed
        arm, the ATR mode's trail/4 and max(trail, 0.02) for every other."""
        if exit_arm.startswith("fixed"):
            return {"trail": fraction, **MAIN_FIXED}
        return plan(fraction)

    def shares(self, exit_arm: str, index: int, h: int) -> int | str:
        sized = self.sized(exit_arm, index, h)
        return sized if isinstance(sized, str) else sized[0]

    def sized(self, exit_arm: str, index: int, h: int) -> tuple[int, str] | str:
        """Whole shares for an entry at bar `index`'s close plus h, or S1's refusal name. With no
        slot budget, one share. Otherwise S1 (`size_for_loss`) sizes the entry on Alpaca's step at
        the trail's distance, for the lead's planned loss, under main's flow cap and the budget."""
        if self.slot_budget is None:
            return 1, "one share"
        key = exit_arm, index, h
        if key in self._shares:
            return self._shares[key]
        b = self.bars[index]
        fraction = trail_fraction(exit_arm, b.close, self.atr[index], self.top)
        try:
            entry = entry_price(Decimal(repr(b.close * (1 + h / 10_000))), 0)
        except ValueError:
            self._shares[key] = "entry_above_top_tier"  # price_step's refusal, not S1's
            return self._shares[key]
        try:
            loss_shares = self.slot_budget // entry
            planned = loss_shares * (entry - stop_trigger_price(entry * (1 - GATE_LOSS_TRAIL)))
            window = self.bars[max(0, index - FLOW_BARS + 1) : index + 1]
            dollars = sum(
                (Decimal(repr(x.close)) * Decimal(repr(x.volume)) for x in window), Decimal(0)
            )
            distance = None if fraction is None else Decimal(repr(fraction))
            planned = planned.quantize(CENT, rounding=ROUND_FLOOR)
            flow = FLOW_SHARE * dollars // entry
            quantity = int(
                size_for_loss(
                    instrument=SIZED,
                    entry=Price.from_str(str(entry)),
                    distance=distance,
                    planned_loss=planned,
                    flow_cap=flow,
                    budget=self.slot_budget,
                ).as_decimal()
            )
            assert distance is not None  # S1 refused a missing distance above
            bounds = {
                "budget": self.slot_budget // entry,
                "flow cap": flow,
                "planned loss": planned // (entry - stop_trigger_price(entry * (1 - distance))),
            }
            self._shares[key] = quantity, "+".join(n for n, v in bounds.items() if v == quantity)
        except ValueError as error:
            if not str(error).replace("_", "").isalpha():
                raise  # not one of S1's refusal names: an error, never a count
            self._shares[key] = str(error)
        return self._shares[key]

    def watched(self, index: int) -> bool:
        """A run may count bar `index`: a listed bar, or for a top-tier name one before it first
        listed (its scaffold). An other-tier name is watched only while listed."""
        return self.seen[index].listed or (self.top and not self.listed_by[index])

    def guard_skip(self, exit_arm: str, index: int, h: int) -> dict[str, Any] | None:
        """A skipped opportunity, at its own close: the existing study's planned-stop guard."""
        b, half = self.bars[index], h / 10_000
        entry, bid = b.close * (1 + half), b.close * (1 - half)
        # The trail is the walk's own, sized from the tape's close; only the entry carries h.
        fraction = trail_fraction(exit_arm, b.close, self.atr[index], self.top)
        if fraction is None:
            return None  # readiness is handled by the signal and sale paths
        floor = study.planned_stop(entry, **self.rows(exit_arm, fraction))
        if bid >= floor:
            return None
        return {
            "kind": "guard_skip",
            "symbol": self.day.symbol,
            "day": str(self.day.day),
            "minute": b.start + 1,
            "time": study.clock(b.start + 1),
            "bid": bid,
            "planned_stop": floor,
        }

    def entry_valid(self, exit_arm: str, index: int, h: int, skips: list[dict[str, Any]]) -> bool:
        """Whether an entry at bar `index` may be sent. A bar with no trail (a zero ATR) is not
        ready, as in the initial scan's distances; a guard skip and an S1 refusal are recorded in
        `skips`, each by its kind."""
        if self.distances(exit_arm)[index] is None:
            return False
        skip = self.guard_skip(exit_arm, index, h)
        if skip is not None:
            skips.append(skip)
            return False
        size = self.shares(exit_arm, index, h)
        if isinstance(size, str):
            b = self.bars[index]
            skips.append(
                {
                    "kind": "size_refused",
                    "symbol": self.day.symbol,
                    "day": str(self.day.day),
                    "minute": b.start + 1,
                    "time": study.clock(b.start + 1),
                    "reason": size,
                }
            )
            return False
        return True

    def distances(self, exit_arm: str) -> tuple[float | None, ...]:
        if exit_arm in self._distances:
            return self._distances[exit_arm]
        out = []
        for b, atr in zip(self.bars, self.atr, strict=True):
            fraction = trail_fraction(exit_arm, b.close, atr, self.top)
            out.append(None if fraction is None else fraction * b.close)
        self._distances[exit_arm] = tuple(out)
        return self._distances[exit_arm]

    def initial(
        self, exit_arm: str, run: int, h: int, opening: tuple[int, int] | None
    ) -> Signal | None:
        key = exit_arm, run, h, opening
        if key in self.initials:
            return self.initials[key]
        distances = self.distances(exit_arm)
        skips: list[dict[str, Any]] = []

        def eligible(index: int) -> bool:
            return self.entry_valid(exit_arm, index, h, skips)

        def opening_eligible(index: int) -> bool:
            # An other-tier name is in the population by its listing; it is bought only while listed.
            return (self.top or self.seen[index].listed) and eligible(index)

        if opening:
            self.initials[key] = opening_signal(
                self.bars, *opening, run, distances, opening_eligible
            )
            self.initial_skips[key] = tuple(skips)
            return self.initials[key]
        rises = 0
        for i, b in enumerate(self.bars):
            rises = (
                rises + 1
                if i > 0
                and self.watched(i)
                and self.watched(i - 1)
                and b.close > self.bars[i - 1].close
                else 0
            )
            if rises >= run and distances[i] is not None and eligible(i):
                self.initials[key] = Signal(i)
                self.initial_skips[key] = tuple(skips)
                return self.initials[key]
        self.initials[key] = None
        self.initial_skips[key] = tuple(skips)
        return None

    def path(self, exit_arm: str, index: int, adaptive: bool = False) -> dict[str, Any]:
        """The protection walk from bar `index`'s close on the tape's own prices. Costs never move
        it: h is charged on the entry and exit prices by `sale`."""
        key = exit_arm, index, adaptive
        if key in self.paths:
            return self.paths[key]
        close = self.bars[index].close
        fraction = trail_fraction(exit_arm, close, self.atr[index], self.top)
        if fraction is None:
            return {"refusal": "atr refused: a zero ATR, and no quoted spread floor"}
        out = protection(
            self.bars[index + 1 :],
            close,
            close,
            study.GROSS,
            fraction,
            adaptive=adaptive,
            rows=self.rows(exit_arm, fraction),
        )
        sold_index = (
            len(self.bars) - 1
            if out is None
            else next(
                j for j in range(index + 1, len(self.bars)) if self.bars[j].start + 1 == out.minute
            )
        )
        sold = self.bars[-1].close if out is None else out.price
        # On a gap/floor exit only the open (or resting floor) is reached before sale, and so on
        # an adaptive low-first sale ("trail-low"). A trail exit visits its high before it sells.
        peak = max([close, *(x.high for x in self.bars[index + 1 : sold_index])])
        if sold_index > index and out is not None and out.why == "trail":
            peak = max(peak, self.bars[sold_index].high)
        elif sold_index > index:
            peak = max(peak, self.bars[sold_index].open, sold)
        result = {
            "index": sold_index,
            "price": sold,
            "distance": close * fraction,
            # The walked distance in Alpaca's ticks at the entry's price (price_step.tick).
            # In decimal, so a distance of exactly three ticks is not a float hair under three.
            "trail_ticks": float(
                Decimal(repr(close)) * Decimal(repr(fraction)) / tick(Decimal(repr(close)))
            ),
            "stop": out is not None,
            "why": "close" if out is None else out.why,
            "peak": peak,
            "trail": fraction,
        }
        self.paths[key] = result
        return result

    def sale(self, exit_arm: str, index: int, h: int, adaptive: bool = False) -> dict[str, Any]:
        """One entry at bar `index`'s close plus h and its exit less h, if the guard lets it in."""
        if self.guard_skip(exit_arm, index, h) is not None:
            return {"refusal": "entry refused: bid under the planned stop"}
        walked = self.path(exit_arm, index, adaptive)
        if "refusal" in walked:
            return walked
        size = self.sized(exit_arm, index, h)
        if isinstance(size, str):
            return {"refusal": f"size refused: {size}"}
        half = h / 10_000
        entry, sold = self.bars[index].close * (1 + half), walked["price"] * (1 - half)
        if self.slot_budget is not None:
            # The prices an order is sent on: the buy S1 sized at, the sale on Alpaca's step under it.
            entry = float(entry_price(Decimal(repr(entry)), 0))
            sold = float(down_on_step(Decimal(repr(sold))))
        return {
            **walked,
            "shares": size[0],
            "bound": size[1],
            "entry": entry,
            "sold": sold,
            "given_back": max(0.0, walked["peak"] - walked["price"]) * (1 - half),
        }

    def replay(
        self,
        exit_arm: str,
        reentry: str,
        run: int,
        h: int,
        opening: tuple[int, int] | None = None,
        *,
        adaptive: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "half_spread_bps": h,
            "order": "adaptive" if adaptive else "ohlc",
            "entries": 0,
            "entry_minutes": [],
            "entry_returns_bps": [],
            "trail_ticks": [],
            "shares": [],
            "per_share_dollars": [],
            "cold_atr_fallbacks": 0,
            "size_refusals": {},
            "size_bounds": {},
            "stop_outs": 0,
            "reentries": 0,
            "net_dollars": 0.0,
            "turnover_dollars": 0.0,
            "given_back_dollars": 0.0,
            "falls": [],
            "refusal": None,
            "guard_skips": [],
            "guard_skips_count": 0,
            "breakeven_half_spread_bps_frozen_path": None,
        }
        if self.day.missing:
            result["refusal"] = f"day refused: {self.day.missing}"
            return result
        if reentry == "vwap" and self.vwap_refusal:
            result["refusal"] = self.vwap_refusal
            return result
        signal = self.initial(exit_arm, run, h, opening)
        result["guard_skips"].extend(self.initial_skips[exit_arm, run, h, opening])
        tally(result)
        if signal is None:
            result["refusal"] = (
                "no guard-valid initial signal"
                if result["guard_skips_count"]
                else "no initial signal S1 could size"
                if result["size_refusals"]
                else "no initial signal with a ready trail"
            )
            return result
        result["initial_signal"] = {
            "minute": self.bars[signal.index].start + 1,
            "depth_trails": signal.depth_trails,
            "fall_minutes": signal.fall_minutes,
            "turn_minutes": signal.turn_minutes,
        }
        while signal:
            sold = self.sale(exit_arm, signal.index, h, adaptive)
            if "refusal" in sold:
                result["refusal"] = sold["refusal"]
                break
            n = sold["shares"]
            result["entries"] += 1
            result["entry_minutes"].append(self.bars[signal.index].start + 1)
            result["entry_returns_bps"].append(
                10_000 * (sold["sold"] - sold["entry"]) / sold["entry"]
            )
            result["shares"].append(n)
            result["trail_ticks"].append(sold["trail_ticks"])
            result["size_bounds"][sold["bound"]] = result["size_bounds"].get(sold["bound"], 0) + 1
            result["per_share_dollars"].append(sold["sold"] - sold["entry"])
            result["cold_atr_fallbacks"] += cold(exit_arm, self.atr[signal.index], self.top)
            result["stop_outs"] += sold["stop"]
            result["net_dollars"] += n * (sold["sold"] - sold["entry"])
            result["turnover_dollars"] += n * (sold["sold"] + sold["entry"])
            result["given_back_dollars"] += n * sold["given_back"]
            if result["entries"] > 1:
                result["falls"].append(
                    {
                        "depth_trails": signal.depth_trails,
                        "fall_minutes": signal.fall_minutes,
                        "turn_minutes": signal.turn_minutes,
                    }
                )
            if not sold["stop"]:
                break
            # The re-entry reads the tape's own sale price, never one net of h.
            key = exit_arm, h, sold["index"], sold["price"], sold["distance"], reentry
            if key not in self.next_signals:
                skips = []
                next_signal = reentry_signal(
                    self.bars,
                    sold["index"],
                    sold["price"],
                    sold["distance"],
                    reentry,
                    self.vwap,
                    lambda index, guard_skips=skips: (
                        self.watched(index) and self.entry_valid(exit_arm, index, h, guard_skips)
                    ),
                )
                self.next_signals[key] = next_signal, tuple(skips)
            signal, skipped = self.next_signals[key]
            result["guard_skips"].extend(skipped)
        result["reentries"] = max(0, result["entries"] - 1)
        tally(result)
        if h == 0 and result["turnover_dollars"]:
            value = 10_000 * result["net_dollars"] / result["turnover_dollars"]
            result["breakeven_half_spread_bps_frozen_path"] = value if value >= 0 else None
        return result


def _covered(intervals: Sequence[tuple[int, int]], d: date) -> bool:
    first, last = study._ns(d, 241), study._ns(d, 960)
    reach = first - 1
    for start, end in sorted(intervals):
        if end < first:
            continue
        if start > reach + 1:
            break
        reach = max(reach, end)
        if reach >= last:
            return True
    return False


def load_symbol(
    root: Path, symbol: str, development: Sequence[date], lookback: Sequence[date]
) -> list[tuple[study.Day, dict[int, float]]]:
    """Bounded minute/daily queries per symbol; no future daily bar or holdout row."""
    guard_dates([*development, *lookback])
    if not development:
        return []
    catalog = ParquetDataCatalog(str(root.resolve()))
    bar_type = f"{symbol}.ALPACA-1-MINUTE-LAST-EXTERNAL"
    intervals = catalog.get_intervals(NautilusDataType.Bar, bar_type)
    loaded = [d for d in development if _covered(intervals, d)]
    if not loaded:
        return []
    earliest, latest = min([*lookback, *development]), max(loaded)
    first, last = study._ns(earliest, 240), study._ns(latest, 960)
    allowed = set(lookback) | set(development)
    closes: dict[date, float] = {}
    for bar in catalog.query(
        NautilusDataType.Bar,
        identifiers=[f"{symbol}.ALPACA-1-DAY-LAST-EXTERNAL"],
        # Query uses ts_init. Exclude a previous, unregistered day's midnight close.
        start=study._ns(earliest, 0) + 1,
        end=last,
    ):
        d = datetime.fromtimestamp(bar.ts_event / 1e9, study.NEW_YORK).date()
        if d not in allowed:
            raise ValueError(f"unregistered daily catalog day read: {d}")
        closes[d] = float(bar.close.as_decimal())
    by_day: dict[date, list[study.Minute]] = defaultdict(list)
    for bar in catalog.query(NautilusDataType.Bar, identifiers=[bar_type], start=first, end=last):
        at = datetime.fromtimestamp(bar.ts_event / 1e9, study.NEW_YORK)
        minute = at.hour * 60 + at.minute
        if at.date() not in allowed:
            raise ValueError(f"unregistered catalog day read: {at.date()}")
        if 240 <= minute < 960:
            by_day[at.date()].append(
                study.Minute(
                    minute,
                    float(bar.open.as_decimal()),
                    float(bar.high.as_decimal()),
                    float(bar.low.as_decimal()),
                    float(bar.close.as_decimal()),
                    float(bar.volume.as_decimal()),
                )
            )
    own: dict[date, dict[int, float]] = defaultdict(dict)
    extras = root / "bar-extras.sqlite"
    if extras.exists():
        with sqlite3.connect(extras.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            for timestamp, vw in connection.execute(
                "SELECT ts_event, vwap FROM bar_extras WHERE bar_type=? AND ts_event>=? AND ts_event<=?",
                (bar_type, first, last),
            ):
                at = datetime.fromtimestamp(timestamp / 1e9, study.NEW_YORK)
                if at.date() not in allowed:
                    raise ValueError(f"unregistered extras day read: {at.date()}")
                if vw is not None:
                    own[at.date()][at.hour * 60 + at.minute] = vw
    for kept in by_day.values():
        kept.sort(key=lambda b: b.start)
    session_bars = {d: tuple(kept) for d, kept in by_day.items()}
    sessions = sorted(set(by_day) | set(closes))
    out = []
    for d in loaded:
        candidates = [s for s in sessions if s < d][-study.MAX_BASELINE :][::-1]
        earlier_dates = [s for s in candidates if _covered(intervals, s)]
        reference_day = candidates[0] if candidates else None
        prior = (
            [b for b in by_day.get(reference_day, ()) if b.start >= 570]
            if reference_day in earlier_dates
            else []
        )
        current = session_bars.get(d, ())
        previous = tuple(session_bars.get(s, ()) for s in earlier_dates)
        reference = closes.get(reference_day) if reference_day is not None else None
        source = f"daily bar of {reference_day}"
        if reference is None:
            reference = prior[-1].close if prior else None
            source = f"last regular minute of {reference_day}" if prior else "missing reference"
        # The study's refusals, by its own names (early_entry_study.load_day).
        missing = None
        if not current:
            missing = "no minute bar in the day's window"
        elif reference_day is None:
            missing = "no bar before the day: the reference close is unknown"
        elif reference is None:
            missing = (
                f"no daily bar for {reference_day}, and its minute bars are not covered to 16:00"
                if reference_day not in earlier_dates
                else f"no daily bar and no regular-session minute bar on {reference_day}"
            )
        out.append(
            (
                study.Day(
                    symbol,
                    d,
                    current,
                    previous,
                    reference,
                    missing=missing,
                    uncovered=len(candidates) - len(earlier_dates),
                    reference_from=source,
                ),
                own[d],
            )
        )
    return out


def code_hashes() -> dict[str, str]:
    """SHA-256 of every module a row's numbers come from: this replay and the study whose walk,
    listing and costs it calls."""
    paths = (Path(__file__), Path(str(study.__file__)))
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def shard(symbols: Sequence[str], spec: str) -> list[str]:
    """Every `n`th symbol from the `k`th, for `k/n`: n shards partition the symbols."""
    k, n = (int(x) for x in spec.split("/"))
    if not 0 <= k < n:
        raise ValueError(f"shard {spec}: k must be in 0..n-1")
    return list(symbols[k::n])


SUMMED = (
    "days",
    "entries",
    "stop_outs",
    "reentries",
    "net_dollars",
    "given_back_dollars",
    "turnover_dollars",
    "refusals",
    "guard_skips_count",
    "falls",
    "depth_sum",
    "fall_minutes_sum",
    "turn_minutes_sum",
    "returns_count",
    "returns_bps_sum",
    "returns_bps_sumsq",
    "cold_atr_fallbacks",
    "size_refusals_count",
    "shares",
)


def new_aggregate(*, histogram: bool = False) -> dict[str, Any]:
    aggregate: dict[str, Any] = dict.fromkeys(SUMMED, 0)
    for field in ("net_dollars", "given_back_dollars", "turnover_dollars", "depth_sum"):
        aggregate[field] = 0.0
    aggregate["returns_bps_sum"] = aggregate["returns_bps_sumsq"] = 0.0
    aggregate["size_refusals"] = {}
    aggregate["size_bounds"] = {}
    aggregate["by_ticks"] = {b: new_returns(histogram=histogram) for b in TICK_BUCKETS}
    if histogram:
        aggregate["returns_bps_bins"] = {}
    return aggregate


RETURNS = ("returns_count", "returns_bps_sum", "returns_bps_sumsq")


def new_returns(*, histogram: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"returns_count": 0, "returns_bps_sum": 0.0, "returns_bps_sumsq": 0.0}
    if histogram:
        out["returns_bps_bins"] = {}
    return out


def add_return(target: dict[str, Any], value: float) -> None:
    target["returns_count"] += 1
    target["returns_bps_sum"] += value
    target["returns_bps_sumsq"] += value * value
    bins = target.get("returns_bps_bins")
    if bins is not None:
        edge = return_bin(value)
        bins[edge] = bins.get(edge, 0) + 1


def merge_returns(target: dict[str, Any], part: dict[str, Any]) -> None:
    for field in RETURNS:
        target[field] += part.get(field, 0)
    if "returns_bps_bins" in target:
        for edge, count in part.get("returns_bps_bins", {}).items():
            target["returns_bps_bins"][int(edge)] = (
                target["returns_bps_bins"].get(int(edge), 0) + count
            )


def returns_figures(a: dict[str, Any]) -> dict[str, Any]:
    """The equal-entry mean, its spread and the median bin from a returns sum."""
    count = a["returns_count"]
    mean = a["returns_bps_sum"] / count if count else None
    return {
        "mean_return_bps": mean,
        "sd_return_bps": (
            math.sqrt(max(0.0, a["returns_bps_sumsq"] / count - mean * mean))
            if count and mean is not None
            else None
        ),
        "median_return_bin_bps": median_bin(a.get("returns_bps_bins", {})),
    }


def return_bin(value: float) -> int:
    """The lower edge of a BIN_BPS-wide bin, clipped to +/-BIN_EDGE_BPS."""
    edge = math.floor(value / BIN_BPS) * BIN_BPS
    return max(-BIN_EDGE_BPS, min(BIN_EDGE_BPS, edge))


def add_to_aggregate(aggregate: dict[str, Any], result: dict[str, Any]) -> None:
    """One symbol-day's replay into a running sum: the equal-entry mean, its spread and a binned
    distribution of per-entry returns come from these sums, with no per-entry record kept."""
    aggregate["days"] += 1
    aggregate["refusals"] += result["refusal"] is not None
    for field in (
        "entries",
        "stop_outs",
        "reentries",
        "net_dollars",
        "given_back_dollars",
        "turnover_dollars",
        "guard_skips_count",
        "cold_atr_fallbacks",
    ):
        aggregate[field] += result[field]
    aggregate["shares"] += sum(result["shares"])
    for bound, count in result["size_bounds"].items():
        aggregate["size_bounds"][bound] = aggregate["size_bounds"].get(bound, 0) + count
    for reason, count in result["size_refusals"].items():
        aggregate["size_refusals_count"] += count
        aggregate["size_refusals"][reason] = aggregate["size_refusals"].get(reason, 0) + count
    for fall in result["falls"]:
        aggregate["falls"] += 1
        aggregate["depth_sum"] += fall["depth_trails"]
        aggregate["fall_minutes_sum"] += fall["fall_minutes"]
        aggregate["turn_minutes_sum"] += fall["turn_minutes"]
    for value, ticks in zip(result["entry_returns_bps"], result["trail_ticks"], strict=True):
        add_return(aggregate, value)
        add_return(aggregate["by_ticks"][tick_bucket(ticks)], value)


def merge_aggregates(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Sum shards' aggregates for one key: every summed field and every bin."""
    merged = new_aggregate(histogram=any("returns_bps_bins" in p for p in parts))
    for part in parts:
        if part.get("returns_count", 0) and "by_ticks" not in part:
            raise ValueError(
                "a shard has per-entry returns but no by_ticks: rerun it at this commit"
            )
        for field in SUMMED:
            merged[field] += part.get(field, 0)
        for bucket, figures in part.get("by_ticks", {}).items():
            merge_returns(merged["by_ticks"][bucket], figures)
        for field in ("size_refusals", "size_bounds"):
            for name, count in part.get(field, {}).items():
                merged[field][name] = merged[field].get(name, 0) + count
        for edge, count in part.get("returns_bps_bins", {}).items():
            merged["returns_bps_bins"][int(edge)] = (
                merged["returns_bps_bins"].get(int(edge), 0) + count
            )
    return merged


def median_bin(bins: dict[int, int]) -> int | None:
    """The lower edge of the bin holding the median per-entry return."""
    total = sum(bins.values())
    if not total:
        return None
    seen = 0
    for edge in sorted(bins):
        seen += bins[edge]
        if 2 * seen >= total:
            return edge
    return None


def report_header(
    commit: str,
    days: Sequence[date],
    *,
    slot_budget: Decimal | None = None,
) -> str:
    sizing = (
        "One share per entry, summed dollars without compounding."
        if slot_budget is None
        else f"Every entry is sized by S1 (`sizing.size_for_loss`) on Alpaca's step, at the trail's "
        f"distance, for the lead's planned loss: the {slot_budget} USD slot parameter's shares times "
        "entry less the stepped 2% stop, about 2% of it. Each entry is charged at the stepped buy "
        "S1 sized it at, and each sale stepped down onto Alpaca's step. Main's flow cap (0.05 of the last 5 bars' "
        "dollar volume) and the budget bound it, and each entry counts the bound that set its size "
        "(budget, flow cap, planned loss; a tie names both). An entry S1 refuses is counted by its "
        "name and the arm scans on; a price past the top tier is entry_above_top_tier. A skip list "
        "holds guard skips and S1 refusals, each by its kind. Summed dollars are without compounding."
    )
    return (
        f"# R3 per-symbol replay — DIAGNOSTIC\n\nCode base: `{commit}`; module hashes in JSONL.\n\n"
        "Per-symbol tables never feed a fitted per-symbol setting. Settings are chosen per tier, "
        "with per-name scaling through ATR. No setting is selected or adopted by this report.\n\n"
        "All rows are UNCALIBRATED: fixed 0.02; ATR(14), Wilder, k=1/2/3, cap 0.02; "
        "tier 0.002 above $1,000,000 baseline dollars/minute, otherwise ATR k=1. "
        "The gate's arms, registered by the lead before any result, source the lead, no primary "
        "source: F2 is `fixed`; OWN1 and OWN3 are `own-1` and `own-3`, the ATR mode at k=1 and 3 with "
        "the cap own (as S2 builds it; not on main until S2 lands); fixed 3, 5 and 8% are "
        "`fixed-3`, `fixed-5` and `fixed-8`. No order price is the replay's own: every arm takes main's "
        "rows at its trail. The fixed arms send main's fixed-mode give 0.005 and chase 0.02 at any trail "
        "(`Mover._entry_trail`, mover.py:2089-2097); ATR and own arms send trail/4 and max(trail, 0.02) "
        "(:2140-2145), so a fixed arm differs from F2 only in its distance. "
        "A cold ATR (fewer than 14 complete bars) enters on main's fixed plan, counted as a cold ATR "
        "entry (`cold_atr_fallback`); a zero ATR has no trail. "
        "Baseline = mean 04:00–16:00 close×volume of up to 5 covered prior sessions / 720 clock minutes. "
        "This is a stand-in for size. ATR uses completed current-day minutes from 04:00 and freezes at entry.\n\n"
        "Reference = latest prior session's daily close when available, otherwise its last regular minute "
        "only if that session's minutes are covered to 16:00. Daily-bar New York dating retains ADR 0092's "
        "UNVERIFIED vendor timestamp convention. Queries never reach holdout dates.\n\n"
        "Spread floor not applied: "
        "no quotes, so ATR and own trails lean narrow against a live window's. Costs h=0/5/25/100 bps per side are a sensitivity grid, UNCALIBRATED, not measured "
        "spreads: h is charged on the entry and exit prices. The walk is the same at every h, but the entry "
        "guard (bid under the planned stop) and S1's size at the h-loaded stepped entry can refuse a bar "
        "at one h and not another, so entries can differ across h. Breakeven "
        "half-spread is the zero-cost path's net dollars / turnover.\n\n"
        "Gates read only what was known at each bar's close: a top-tier name runs on its scaffold until "
        "it first lists and on its listing after; an other-tier name enters, opening arms included, only "
        "on a listed bar. Its place in the population still comes from a listing at some time that day, "
        "so the population is the days a live list would have shown it.\n\n"
        "Initial run_bars=2 base; 1/3 also reported. Re-entry arms: reclaim without cooldown; turn "
        "k=1/2/3, depth=1/2 trails, on the tape's own sale price; own-vw session VWAP cross after below, "
        "refused by name if unavailable. No re-entry on the sale bar.\n\n"
        "A synthetic bid below the scaled planned stop is guard_skip, counted with symbol, close time, "
        "bid and planned-stop price. The same initial, opening or re-entry arm keeps scanning to its "
        "first guard-valid opportunity (the 2026-11-12 ruling).\n\n"
        "Opening arms: first 15/30 minutes, fall from 09:30 open to running lowest close >=1/2 "
        "entry trail distances, 2 rising closes; low diagnostics separately use bar lows. "
        "All times are New York minute-close knowledge times.\n\n"
        "Bar order: every table gives each figure under NautilusTrader's default bar path "
        "(open, high, low, close) and its documented adaptive path (the low first unless the open is "
        "nearer the high), as a range. Limits: a stop sells at exactly its trigger, after the trigger "
        "rises on the same bar's high; a real released limit fills at or under its trigger, so stop "
        "prices lean in the strategy's favour under either path. A released limit never filled is "
        "a stop sold at the last close.\n\n"
        "Per-entry returns are also split by the trail's distance at entry in Alpaca's ticks "
        "(price_step.tick at the entry bar's close): under 3, 3 to under 5, 5 to 10 inclusive, over "
        "10, the lead's breakdown of 2026-11-12, an observation and not a registered row. Every "
        "arm's aggregate in the JSONL carries `by_ticks`; the table below shows F2's.\n\n"
        f"{sizing} Per-entry returns in bps (sale less "
        "entry over entry, after h) are kept as a count, a sum, a sum of squares and 10 bps bins, so "
        "the equal-entry mean and median per arm and tier can be read. No paper fills, impact, quoted "
        "size, tick rounding of the walk, fees, slot limits, split adjustment or delisted assets. Minute bars are "
        "not actual intraminute order. Population coverage below counts the available symbol-days, "
        "not evidence of a whole-market edge. Missing/empty/short-history arms are counted.\n\n"
        f"Requested development days ({len(days)}): {', '.join(map(str, days))}\n\n"
    )


TABLE_HEADER = (
    "Each figure after Days reads `OHLC; adaptive`: the default bar path, then the adaptive one.\n\n"
    "| Group | Exit | Re-entry | h bps | Days | Entries | Stops | Re-entries | Net $ | "
    "Mean bps/entry | Median bin bps | Given back $ | Mean depth/trails | Fall min | Turn min | "
    "Breakeven h bps | Refusals | Guard skips | S1 refusals | Cold ATR entries |\n"
    "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
)

Key = tuple[str, str, str, str, int, int, str]


def aggregate_record(key: Key, a: dict[str, Any]) -> dict[str, Any]:
    group, initial, exit_arm, reentry, run, h, order = key
    n, turnover = a["falls"], a["turnover_dollars"]
    return {
        "kind": "aggregate",
        "group": group,
        "initial": initial,
        "exit": exit_arm,
        "reentry": reentry,
        "run": run,
        "half_spread_bps": h,
        "order": order,
        **a,
        **returns_figures(a),
        "by_ticks": {b: {**f, **returns_figures(f)} for b, f in a["by_ticks"].items()},
        "mean_depth_trails": a["depth_sum"] / n if n else None,
        "mean_fall_minutes": a["fall_minutes_sum"] / n if n else None,
        "mean_turn_minutes": a["turn_minutes_sum"] / n if n else None,
        "breakeven_half_spread_bps_frozen_path": (
            10_000 * a["net_dollars"] / turnover
            if h == 0 and turnover and a["net_dollars"] >= 0
            else None
        ),
    }


def _figure(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


PAIRED = (
    ("entries", None),
    ("stop_outs", None),
    ("reentries", None),
    ("net_dollars", 4),
    ("mean_return_bps", 1),
    ("median_return_bin_bps", 0),
    ("given_back_dollars", 4),
    ("mean_depth_trails", 3),
    ("mean_fall_minutes", 3),
    ("mean_turn_minutes", 3),
    ("breakeven_half_spread_bps_frozen_path", 3),
    ("refusals", None),
    ("guard_skips_count", None),
    ("size_refusals_count", None),
    ("cold_atr_fallbacks", None),
)


def table_row(ohlc: dict[str, Any], adaptive: dict[str, Any] | None) -> str:
    """One arm's row: every figure the bar order can move as a pair, default path first."""
    other = adaptive or {}

    def cell(value: Any, digits: int | None) -> str:
        if digits is None:
            return "-" if value is None else str(value)
        return _figure(value, digits)

    values = [
        ohlc["group"],
        ohlc["exit"],
        ohlc["reentry"],
        ohlc["half_spread_bps"],
        ohlc["days"],
        *(f"{cell(ohlc[f], d)}; {cell(other.get(f), d)}" for f, d in PAIRED),
    ]
    return "| " + " | ".join(map(str, values)) + " |\n"


def write_tier_table(out: Any, sums: dict[Key, dict[str, Any]]) -> None:
    out.write(TABLE_HEADER)
    for key in sorted(k for k in sums if k[0].startswith("tier:") and k[1] == "run-2"):
        if key[6] != "ohlc":
            continue
        adaptive = sums.get((*key[:6], "adaptive"))
        out.write(
            table_row(
                aggregate_record(key, sums[key]),
                None if adaptive is None else aggregate_record((*key[:6], "adaptive"), adaptive),
            )
        )


TICK_HEADER = (
    "F2 (`fixed`, 2%) per-entry return by the trail's distance at entry in Alpaca's ticks, run-2: "
    "under 3, 3 to under 5, 5 to 10 inclusive, over 10. "
    "Each cell reads `entries mean-bps median-bin` under OHLC, then adaptive; an observation, not "
    "a registered row.\n\n"
    "| Group | Re-entry | h bps | " + " | ".join(TICK_BUCKETS) + " |\n"
    "|---|---|---:|" + "---:|" * len(TICK_BUCKETS) + "\n"
)


def _tick_cell(figures: dict[str, Any] | None) -> str:
    if not figures or not figures["returns_count"]:
        return "-"
    f = returns_figures(figures)
    return f"{figures['returns_count']} {f['mean_return_bps']:.1f} {_figure(f['median_return_bin_bps'], 0)}"


def write_tick_table(out: Any, sums: dict[Key, dict[str, Any]]) -> None:
    out.write("\n" + TICK_HEADER)
    for key in sorted(k for k in sums if k[0].startswith("tier:") and k[1:3] == ("run-2", "fixed")):
        if key[6] != "ohlc":
            continue
        adaptive = sums.get((*key[:6], "adaptive"), {}).get("by_ticks", {})
        cells = [
            f"{_tick_cell(sums[key]['by_ticks'][b])}; {_tick_cell(adaptive.get(b))}"
            for b in TICK_BUCKETS
        ]
        out.write(f"| {key[0]} | {key[3]} | {key[5]} | " + " | ".join(cells) + " |\n")


def merge(paths: Sequence[Path], output: Path, name: str) -> Path:
    """One summary from shard runs: their tier aggregates and counts summed. Every shard is complete,
    the shards are exactly 0/n to n-1/n of one n, and they ran the same code on the same inputs."""
    sums: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    used: dict[str, int] = defaultdict(int)
    metadata = []
    for path in paths:
        complete = False
        with path.open() as stream:
            for line in stream:
                record = json.loads(line)
                if record["kind"] == "metadata":
                    metadata.append(record)
                elif record["kind"] == "aggregate" and record["group"].startswith("tier:"):
                    key = tuple(record[f] for f in KEY_FIELDS)
                    sums[key].append(record)
                elif record["kind"] == "completion":
                    complete = True
                    for field, value in record["counts"].items():
                        counts[field] += value
                    for day, value in record["days_used"].items():
                        used[day] += value
        if not complete:
            raise ValueError(f"shard incomplete: {path}")
    specs = [tuple(int(x) for x in m["shard"].split("/")) for m in metadata]
    ns = {n for _, n in specs}
    if len(ns) != 1 or sorted(k for k, _ in specs) != list(range(ns.pop())):
        raise ValueError(f"shards {[m['shard'] for m in metadata]} are not 0/n to n-1/n once each")
    for field in MERGE_SAME:
        if len({json.dumps(m.get(field), sort_keys=True) for m in metadata}) != 1:
            raise ValueError(f"shards differ in {field}")
    hashes = {json.dumps(m["code_sha256"], sort_keys=True) for m in metadata}
    merged = {key: merge_aggregates(parts) for key, parts in sums.items()}
    summary = output / f"{name}-summary.md"
    development = [date.fromisoformat(d) for d in metadata[0]["development"]]
    with summary.open("x") as out:
        budget = metadata[0]["slot_budget"]
        out.write(
            report_header(
                metadata[0]["base_commit"],
                development,
                slot_budget=None if budget is None else Decimal(budget),
            )
        )
        out.write(
            f"Shards: {', '.join(f'`{p}`' for p in paths)}; module hashes {hashes.pop()}.\n\n"
        )
        out.write(
            f"Counts: `{json.dumps(counts, sort_keys=True)}`.\n\nDays used (covered symbol-days): "
            f"`{json.dumps(used, sort_keys=True)}`.\n\n"
        )
        write_tier_table(out, merged)
        write_tick_table(out, merged)
    return summary


MERGE_SAME = (
    "code_sha256",
    "base_commit",
    "sessions_sha256",
    "development",
    "lookback",
    "catalog",
    "slot_budget",
)
KEY_FIELDS = ("group", "initial", "exit", "reentry", "run", "half_spread_bps", "order")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--sessions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--shard", default="0/1", help="k/n: every nth symbol from the kth")
    parser.add_argument("--merge", type=Path, nargs="+", help="shard JSONL files to sum")
    parser.add_argument(
        "--slot-budget",
        type=Decimal,
        default=SLOT_BUDGET,
        help="USD per slot, the lead's parameter: every entry is sized by S1 for 2%% of it",
    )
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.merge:
        print(merge(args.merge, args.output, args.name), flush=True)
        return 0
    if args.catalog is None or args.sessions is None:
        parser.error("--catalog and --sessions are required unless --merge is given")
    development, lookback = read_sessions(args.sessions)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    hashes = code_hashes()
    symbols = shard(
        sorted(
            p.name.removesuffix(".ALPACA-1-MINUTE-LAST-EXTERNAL")
            for p in (args.catalog / "data" / "bars").iterdir()
            if p.is_dir() and p.name.endswith(".ALPACA-1-MINUTE-LAST-EXTERNAL")
        ),
        args.shard,
    )
    sums: dict[Key, dict[str, Any]] = {}
    used: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    destination = args.output / f"{args.name}.jsonl"
    symbol_path = args.output / f"{args.name}-symbols.md"
    with destination.open("x") as stream, symbol_path.open("x") as symbol_table:
        header = report_header(commit, development, slot_budget=args.slot_budget)
        symbol_table.write(header + TABLE_HEADER)
        stream.write(
            json.dumps(
                {
                    "kind": "metadata",
                    "base_commit": commit,
                    "source_sha256": hashes[Path(__file__).name],
                    "code_sha256": hashes,
                    "shard": args.shard,
                    "sessions_sha256": hashlib.sha256(args.sessions.read_bytes()).hexdigest(),
                    "development": list(map(str, development)),
                    "lookback": list(map(str, lookback)),
                    "settings": header,
                    "slot_budget": str(args.slot_budget),
                    "catalog": str(args.catalog),
                    "snapshot_started_utc": datetime.now(study.UTC).isoformat(),
                    "symbols": len(symbols),
                }
            )
            + "\n"
        )
        for number, symbol in enumerate(symbols, 1):
            loaded = load_symbol(args.catalog, symbol, development, lookback)
            counts["symbols_scanned"] += 1
            counts["loaded_symbol_days"] += len(loaded)
            for day, vw in loaded:
                used[str(day.day)] += 1
                counts["reference_missing_symbol_days"] += day.reference is None
                counts["short_baseline_symbol_days"] += len(day.earlier) < 5
                pop = population(day)
                if not pop["selected"]:
                    continue
                counts["selected_symbol_days"] += 1
                counts["top_symbol_days"] += pop["top_tier"]
                counts["ignited_symbol_days"] += pop["ignition"] is not None
                context = ReplayContext(
                    day,
                    vw,
                    pop["top_tier"],
                    slot_budget=args.slot_budget,
                )
                base = {
                    "symbol": symbol,
                    "day": str(day.day),
                    **pop,
                    "reference_from": day.reference_from,
                    "missing": day.missing,
                    "baseline_sessions": min(5, len(day.earlier)),
                }
                stream.write(
                    json.dumps(
                        {
                            "kind": "opening",
                            **base,
                            "windows": [opening_diagnostic(day.bars, m) for m in (15, 30, 390)],
                        }
                    )
                    + "\n"
                )
                initials = [(f"run-{r}", r, None) for r in (2, 1, 3)] + [
                    (f"opening-{m}-{f}", 2, (m, f)) for m in (15, 30) for f in (1, 2)
                ]
                for initial_name, run, opening in initials:
                    for exit_arm in EXITS:
                        for reentry in REENTRIES:
                            metrics = [
                                context.replay(
                                    exit_arm, reentry, run, h, opening, adaptive=order == "adaptive"
                                )
                                for order in ORDERS
                                for h in HALF_SPREAD_BPS
                            ]
                            stream.write(
                                json.dumps(
                                    {
                                        "kind": "replay",
                                        **base,
                                        "initial": initial_name,
                                        "exit": exit_arm,
                                        "reentry": reentry,
                                        "costs": metrics,
                                    },
                                    allow_nan=False,
                                )
                                + "\n"
                            )
                            for result in metrics:
                                for group in (f"tier:{pop['tier']}", f"symbol:{symbol}"):
                                    key = (
                                        group,
                                        initial_name,
                                        exit_arm,
                                        reentry,
                                        run,
                                        result["half_spread_bps"],
                                        result["order"],
                                    )
                                    if key not in sums:
                                        sums[key] = new_aggregate(
                                            histogram=group.startswith("tier:")
                                        )
                                    add_to_aggregate(sums[key], result)
                stream.flush()
            # A symbol's aggregate is complete here; retain only tier aggregates in memory.
            mine = sorted(k for k in sums if k[0] == f"symbol:{symbol}")
            records = {key: aggregate_record(key, sums[key]) for key in mine}
            for key in mine:
                sums.pop(key)
                stream.write(json.dumps(records[key]) + "\n")
            for key in mine:
                if key[1] == "run-2" and key[6] == "ohlc":
                    symbol_table.write(table_row(records[key], records.get((*key[:6], "adaptive"))))
            study._listing.cache_clear()
            if number % 200 == 0:
                print(
                    f"scanned {number}/{len(symbols)}; selected {counts['selected_symbol_days']}",
                    flush=True,
                )
        for key, aggregate in sorted(sums.items()):
            stream.write(json.dumps(aggregate_record(key, aggregate)) + "\n")
        counts["unloaded_symbol_days"] = (
            len(development) * len(symbols) - counts["loaded_symbol_days"]
        )
        stream.write(
            json.dumps(
                {
                    "kind": "completion",
                    "counts": counts,
                    "days_used": used,
                    "finished_utc": datetime.now(study.UTC).isoformat(),
                }
            )
            + "\n"
        )
    summary = args.output / f"{args.name}-summary.md"
    with summary.open("x") as out:
        out.write(header)
        out.write(
            f"JSONL: `{destination}`; module SHA-256 `{json.dumps(hashes, sort_keys=True)}`.\n\n"
        )
        out.write(f"Per-symbol table: `{symbol_path}`.\n\n")
        out.write(
            f"Counts: `{json.dumps(counts, sort_keys=True)}`.\n\nDays used (covered symbol-days): `{json.dumps(used, sort_keys=True)}`.\n\n"
        )
        out.write(
            "Full per-symbol, per-tier, opening and re-entry fall diagnostics are in JSONL. "
            "The following base run-2 table is the tier summary. Dollar sums are of entries sized by S1 "
            "for one planned loss; the per-entry mean and median weight every entry equally. These are "
            "diagnostics, not portfolio returns.\n\n"
        )
        write_tier_table(out, sums)
        write_tick_table(out, sums)
    print(summary, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
