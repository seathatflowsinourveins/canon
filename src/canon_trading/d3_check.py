"""Read one paper-window JSON against the D3 acceptance terms (S5, A1-R).

Contract read 2026-11-13; the numbers are UNCALIBRATED.
Writer references below are at 16d02e1f:
* mover.py:1964-1977: run lists; :3701-3733: entries; :3631-3649: sizing;
  :1195-1205: exits; :1739-1746: fill tuples; :1916-1930: settings rows.
* session_window.py:3244: mover.run; :1087-1156: mover.result; :1722-1751,
  :3375-3407: closing evidence; :4581-4601: order events and denial reasons.
* Since #351 (mover.py at 42f2aa58) the writer records A3's sales and both refusal
  lists: `_sale_sent` (:2199-2240) gives each sale `distance_in_force`, in price
  units, compared to `trailing_offset`; `refusal_row` (:1546-1556) each
  `order_refusals` row; :4179-4188 each `refusals` row, which :4004 keeps out of
  `dropped`. Null trailing AND limit offsets describe a plain limit, not a trailing
  remake. A record written before #351 has no sales, so A3 is not shown on it.

Sizing implements the recorded equity specialization of pinned NautilusTrader
d53849b216e2, crates/risk/src/sizing.rs:74-116 (loss/distance, caps, whole-share
batch), instruments/equity.rs:275-288 (unit size/multiplier), and this repo's
sizing.py:70-79,149-202 and price_step.py:31-33. No trading module is imported.
Python b703b2c5e704: Lib/json/__init__.py:278-360 (strict hooks), pathlib's
read_text, and Decimal's finite tests/directed rounding. Only the requested file
is opened, for reading; stdout is the only output. No network or credential API.

Output: one status line per term with JSON detail. `paths` contains only existing
JSON paths actually read; `missing` names absent paths separately. The first
counterexample has its original value. A failure outranks missing evidence within
a term. Failures of A1-A3 and A5-A7 outrank no entry. A4 is counted, never failed;
even an A4 not shown has no effect on the summary or exit code.
Fixed-arm windows print practice; A1/A4 are not judged, while A2/A3/A5/A6/A7
still expose findings marked fixed arm. Failed terms are named in the summary,
for example `practice (finding: A6, A7)`, and return 5 without calling for a revert; a fixed-arm
window whose shared evidence is not shown is `incomplete` (2), not practice. `practice` keeps exit 0
beside `counts`, the summary line telling them apart: one new exit code (5) was allowed, no more.
A filled BUY with no entry plan leaves A1 and A2 not shown; A1 checks the recorded floor against
spread_floor_m * spread / entry price; A4 times a quick sale by its final fill, not its trigger; A5
reads each fill row's five fields; A6 fails a recorded open remainder whatever `flat` says.
A record still `running` or `stopping` fails A6: read after its window has ended, it belongs to a
window that died before its wind-down finished (`scripts/automation status` calls it DIED once stale or
overtaken). A window still live reads the same, so check a record only after its window has ended.
Exit codes: counts/practice 0 (no evaluated term failed), breaks 1, incomplete 2,
no entry 3, unreadable 4, practice finding 5.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from decimal import DecimalException
from decimal import localcontext
from pathlib import Path
from typing import Any
from typing import Never

type KeyPath = tuple[str | int, ...]
RUN: KeyPath = ("mover", "run")
ENTRIES = (*RUN, "entries")
EXITS = (*RUN, "exits")
FILLS = (*RUN, "fills")
ABSENT = object()
SIXTY_SECONDS_NS = 60_000_000_000

# Exact source-backed message families, not a keyword guess about "our" fault.
# wash_trade: packages/canon-alpaca/tests/test_paper_matrix.py:266,391 documents
# the paper venue's 403 text. replace: only execution.py:3071 (`_rejected_takeover` -> `_reject`,
# an OrderRejected) reaches Mover's `refused` (mover.py:1872-1878) and the window's denials; :2965
# and :3091 are modify rejections (`_modify_rejected`, OrderModifyRejected), which neither keeps, so
# those texts can come only in prospective `order_refusals` rows; so can :2338, the replace's
# HTTP refusal ("Alpaca refused the replace (422): ...", REPLACE_REFUSAL at :533).
# off_step: execution.py:1012-1020 formats this LOCAL denial. With legacy records
# it proves no venue refusal; structured rows explicitly supply `by`.
REFUSAL_PATTERNS = (
    ("wash_trade", r"\bpotential wash trade detected\b", False),
    (
        "refused_replace",
        (
            r"^Alpaca (?:rejected the (?:replace \(trade update\)|replacement order \S+"
            r"(?: \(trade update\))?)|refused the replace \(\d{3}\): [\s\S]*)$"
        ),
        False,
    ),
    (
        "off_step",
        (
            r"(?:limit|stop) price [0-9.eE+\-]+ has more than [24] decimals, "
            r"which Alpaca rejects for a price (?:at or above|below) \$1\.00"
        ),
        True,
    ),
    # mover.py:1484-1494 (`refusal_cause`): Alpaca's own rejection of an off-step price.
    ("off_step", r"\bsub-penny increment does not fulfill minimum pricing criteria\b", False),
)
"""Each cause, its text, and whether that text is the adapter's own denial before the venue, so a
legacy row with no `by` is placed by its text: only the adapter's text is before the venue."""


def json_path(path: KeyPath) -> str:
    return "$" + "".join(f"[{json.dumps(key)}]" for key in path)


class _Unavailable(Exception):
    """Stop this evidence item, without stopping checks of independent items."""


@dataclass
class Term:
    name: str
    record: dict[str, Any] = field(repr=False)
    paths: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    first_failure: dict[str, Any] | None = None
    uncertain: bool = False
    fixed_arm: bool = False

    @property
    def status(self) -> str:
        if self.fixed_arm and self.name in {"A1", "A4"}:
            return "not judged"
        if self.name == "A4":
            return "not shown" if self.uncertain else "counted"
        if self.first_failure is not None:
            return "failed"
        return "not shown" if self.uncertain else "met"

    def _seen(self, path: KeyPath) -> None:
        text = json_path(path)
        if text not in self.paths:
            self.paths.append(text)

    def optional(self, path: KeyPath) -> Any:
        value: Any = self.record
        for index, key in enumerate(path):
            if (isinstance(key, str) and isinstance(value, dict) and key in value) or (
                isinstance(key, int) and isinstance(value, list) and 0 <= key < len(value)
            ):
                value = value[key]
            else:
                self._seen(path[:index])
                return ABSENT
        self._seen(path)
        return value

    def read(self, path: KeyPath) -> Any:
        value = self.optional(path)
        if value is ABSENT:
            text = json_path(path)
            if text not in self.missing:
                self.missing.append(text)
            self.unknown(path, "missing key or index")
        return value

    def unknown(self, path: KeyPath, reason: str) -> Never:
        self.uncertain = True
        self.notes.append(f"{json_path(path)}: {reason}")
        raise _Unavailable

    def fail(self, path: KeyPath, reason: str) -> None:
        if self.name == "A4":
            self.unknown(path, reason)
        value = self.read(path)
        if self.first_failure is None:
            self.first_failure = {"path": json_path(path), "value": value, "reason": reason}

    def number(self, path: KeyPath) -> Decimal:
        value = self.read(path)
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            self.unknown(path, "finite number not shown")
        try:
            number = Decimal(str(value))
        except DecimalException:
            self.unknown(path, "finite number not shown")
        if not number.is_finite():
            self.unknown(path, "finite number not shown")
        return number

    def stamp(self, path: KeyPath) -> int:
        value = self.number(path)
        if value < 0 or value != value.to_integral_value():
            self.unknown(path, "nonnegative integer nanoseconds not shown")
        return int(value)

    def array(self, path: KeyPath) -> list[Any]:
        value = self.read(path)
        if not isinstance(value, list):
            self.unknown(path, "array not shown")
        return value

    def mapping(self, path: KeyPath) -> dict[str, Any]:
        value = self.read(path)
        if not isinstance(value, dict):
            self.unknown(path, "object not shown")
        return value

    def text(self, path: KeyPath) -> str:
        value = self.read(path)
        if not isinstance(value, str) or not value.strip():
            self.unknown(path, "nonempty text not shown")
        return value

    @contextmanager
    def scope(self) -> Generator[None]:
        try:
            yield
        except _Unavailable:
            pass
        except DecimalException as error:
            self.uncertain = True
            self.notes.append(f"arithmetic could not be checked: {type(error).__name__}")


@dataclass
class Check:
    terms: list[Term]
    summary: str

    @property
    def exit_code(self) -> int:
        if self.summary.startswith("practice (finding: "):
            return 5
        return {"counts": 0, "practice": 0, "breaks": 1, "incomplete": 2, "no entry": 3}[
            self.summary
        ]

    def lines(self) -> list[str]:
        lines = []
        for term in self.terms:
            status = term.status
            if term.fixed_arm:
                status += " (fixed arm)"
            elif term.name == "A4" and status == "not shown":
                status = "counted (not shown)"
            elif term.name == "A1" and status == "met" and term.counts.get("fixed_plan_count", 0):
                status += " (no usable spread: fixed plan)"
            detail = {"paths": term.paths, "missing": term.missing, "notes": term.notes}
            payload: dict[str, Any] = dict(detail, counts=term.counts)
            if term.first_failure is not None:
                payload["first_failure"] = term.first_failure
            lines.append(f"{term.name}: {status} | {json.dumps(payload, default=str)}")
        return [*lines, self.summary]


def _price(t: Term, entry: KeyPath) -> Decimal:
    # mover.py:3648-3649: notional = quantity * planned entry limit (not fill VWAP).
    sizing = (*entry, "sizing")
    quantity = t.number((*sizing, "quantity"))
    if quantity <= 0:
        t.fail((*sizing, "quantity"), "entry quantity must be positive")
        raise _Unavailable
    return t.number((*sizing, "notional")) / quantity


def _fixed_arm_entry(t: Term, entry: KeyPath) -> bool:
    # mover.py:3708,3712-3717,3855-3873 distinguishes F2 from a per-symbol fallback.
    mode = t.optional((*entry, "trail_mode"))
    if mode is not ABSENT:
        return mode == "fixed"
    return (
        t.optional((*entry, "trail_source")) == "fixed"
        and t.optional((*entry, "trail_bound")) == "fixed"
        and t.optional((*entry, "spread_floor")) is None
        and t.optional((*entry, "spread")) not in (None, ABSENT)
        and t.number((*entry, "spread")) > 0
    )


def _fixed_arm(t: Term) -> bool:
    # session_window.py:530-533,1764,3103 and mover.py:1916-1927 record the arm/mode.
    arm = t.optional(("arm",))
    if arm in ("F2", "OWN1", "OWN3"):
        return arm == "F2"
    for path in (("mover", "trail_mode"), (*RUN, "rows", "trail_mode")):
        mode = t.optional(path)
        if mode is not ABSENT:
            return mode == "fixed"
    entries = t.array(ENTRIES)
    return bool(entries) and all(_fixed_arm_entry(t, (*ENTRIES, i)) for i in range(len(entries)))


def _fixed_plan(t: Term, entry: KeyPath) -> bool:
    # A1-R: a per-symbol arm with no usable spread takes the recorded fixed plan.
    return (
        t.read((*entry, "spread")) is None
        and t.read((*entry, "spread_floor")) is None
        and t.text((*entry, "trail_source")) == "fixed"
        and t.text((*entry, "trail_bound")) == "fixed"
        and t.number((*entry, "trail")) == t.number((*RUN, "rows", "trail")) > 0
    )


def _opening_floor(t: Term, entry: KeyPath) -> tuple[bool, bool]:
    """Whether the opening meets its floor, and whether cross-dollar tolerance was used."""
    # mover.py:3710-3717: floor/trail are fractions, spread is a dollar amount.
    floor = t.number((*entry, "spread_floor"))
    trail = t.number((*entry, "trail"))
    # mover.py:3640,3707: the actual opening offset L is in dollars.
    distance = t.number((*entry, "sizing", "loss_per_share"))
    price = _price(t, entry)
    if floor <= 0 or trail < floor:
        return False, False
    floor_dollars = floor * price
    if distance >= floor_dollars:
        return True, False
    if distance <= 0 or floor_dollars - distance > _tick(price):
        return False, False
    # mover.py:4351-4373 (#351): a writer that records `sized_trail` widens an L under the floor
    # onto the step or refuses the entry, so only a record written before it may fall short.
    if t.optional((*entry, "sizing", "sized_trail")) is not ABSENT:
        return False, False
    # Only cross-dollar rounding may fall short, by at most the entry's tick.
    # mover.py:3600-3602 floors L on that tick; A2 independently checks its exact value.
    stop = t.number((*entry, "sizing", "stop"))
    used_tolerance = 0 < stop < price and _tick(price) != _tick(stop)
    return used_tolerance, used_tolerance


def _a1_entry(t: Term, entry: KeyPath) -> None:
    # mover.py:3707-3717,3843-3874: fixed fallback has null spread/floor and fixed source/bound.
    spread = t.read((*entry, "spread"))
    floor = t.read((*entry, "spread_floor"))
    trail = t.number((*entry, "trail"))
    source = t.text((*entry, "trail_source"))
    bound = t.text((*entry, "trail_bound"))
    if spread is None:
        if floor is not None:
            t.fail((*entry, "spread_floor"), "a no-spread fixed plan must record no spread floor")
        if source != "fixed":
            t.fail((*entry, "trail_source"), "no usable spread requires the named fixed plan")
        if bound != "fixed":
            t.fail((*entry, "trail_bound"), "no usable spread requires the fixed bound")
        # mover.py:1916-1930 records settings.trail; :3864-3872 uses exactly this fallback.
        fixed = t.number((*RUN, "rows", "trail"))
        if trail <= 0 or trail != fixed:
            t.fail((*entry, "trail"), f"fixed plan distance is {fixed}")
        elif source == bound == "fixed" and floor is None:
            t.counts["fixed_plan_count"] += 1
        return
    if t.number((*entry, "spread")) <= 0:
        t.unknown((*entry, "spread"), "unusable spread is recorded as null by this writer")
    floor_number = t.number((*entry, "spread_floor"))
    if floor_number <= 0:
        t.unknown((*entry, "spread_floor"), "positive quoted spread floor not shown")
    # mover.py:4252: the floor is spread_floor_m * spread / entry price, the multiplier recorded
    # among the run's rows (mover.py:821,2037); a recorded floor that is not that is wrong.
    derived = (
        t.number((*RUN, "rows", "spread_floor_m")) * t.number((*entry, "spread")) / _price(t, entry)
    )
    if floor_number != derived:
        t.fail(
            (*entry, "spread_floor"),
            f"not spread_floor_m * spread / entry price = {derived}",
        )
        raise _Unavailable
    if trail < floor_number:
        t.fail((*entry, "trail"), f"below spread floor {floor_number}")
    # mover.py:3600-3602,3640: L may be rounded below the fractional plan at the dollar boundary.
    meets_floor, used_tolerance = _opening_floor(t, entry)
    if used_tolerance:
        t.counts["p5_tolerance_used"] += 1
    if not meets_floor and trail >= floor_number:
        t.fail(
            (*entry, "sizing", "loss_per_share"),
            f"below spread floor {floor_number} of the entry price",
        )


TRAIL_CAP_REFUSALS = ("floor_over_cap", "distance_over_cap")
"""A refusal row's `reason` for the trail cap; these rows carry `distance` and `cap`."""

# mover.py at 16d02e1f: _buy returns reasons through _enter:3341 into dropped;
# _entry_trail's ValueErrors pass through _buy:3457-3460. Match the complete reason
# after its instrument prefix, not keywords in the notes that share this list.
NAMED_DROP_PATTERNS = (
    # _buy:3444-3452,3500-3518,3652-3673.
    r"no share cap for this window instrument",
    r"no quote yet",
    r"no account balance to size from",
    r"no room for the trail under .+",
    r"no positive bid to check against the planned stop",
    r"the bid .+ is under the planned stop .+ of an entry at .+: the quote is wider than the plan",
    r"no free balance to size from",
    r"the caps leave no share at .+",
    r"the size from risk came to nothing at .+",
    # _buy:3676-3678 -> _switch_off:3829.
    r"the entry switch reads .+",
    # _buy:3453-3456 -> price_step.py:59-71.
    r"no limit price .+ ticks above .+ in ALPACA_US_EQUITY_LIMIT",
    r"no limit price .+ ticks above .+: above the top tier of ALPACA_US_EQUITY_LIMIT, .+",
    # _buy:3469-3471 -> TurnState.turn_refusal:1261-1270.
    r"opening: turn state or trail distance unavailable",
    r"opening: turn depth .+ is under .+ trail distances \(.+\)",
    r"opening: turn needs .+ rising closes from the low; has .+",
    # _buy:3573-3598; every named plan_for_loss refusal in sizing.py:54-66.
    (
        r"sizing refused: (?:distance_unknown|distance_over_ceiling|stop_zero|entry_invalid|"
        r"entry_off_step|planned_loss_invalid|planned_loss_precision|flow_cap_invalid|"
        r"budget_invalid|loss_under_one_share|size_zero) \(.+\)"
    ),
    # _buy:3603-3617.
    r"the distance .+ sized for an entry at .+ is under its step .+: no trail can hold it",
    (
        r"the bid .+ is at or under .+, the distance .+ sized for an entry at .+ under the ask .+: "
        r"the quote is wider than the trail"
    ),
    # _entry_trail:3850,3876,3964-3971 (the no-quote branch uses the reason above).
    r"profile document is missing; no profile entry is allowed",
    r"the spread floor exceeds the 0\.02 cap; no entry satisfies both",
    (
        r"the trail .+ \(.+\) is over the 0\.02 cap, and trail_cap own needs "
        r"size_from_budget to size it; refused"
    ),
    r"the trail .+ \(.+\) is over the 0\.02 cap; refused \(trail_cap refuse\)",
    # _entry_trail:3910-3917 -> trail.py:46 (profile_segment), :66-74 and :112 (profile_distance).
    r"profile time is outside the ranking clock's 96-segment part",
    r"profile_k needs a finite registered Decimal",
    r"profile_multipliers needs a registered tuple of Decimals",
    r"profile_(?:k|pctl|floor|multipliers) must be one of .+",
    r"profile segment must be an index from 0 to 95",
)


def _a1_refusals(t: Term) -> None:
    # The record unit's contract, not on main yet: each row names its cause in `reason`.
    new = (*RUN, "refusals")
    if t.optional(new) is not ABSENT:
        for index in range(len(t.array(new))):
            with t.scope():
                row = (*new, index)
                reason = t.text((*row, "reason"))
                if reason in TRAIL_CAP_REFUSALS:
                    cap = t.number((*row, "cap"))
                    if cap <= 0 or t.number((*row, "distance")) <= cap:
                        t.unknown((*row, "distance"), "distance over a positive cap not shown")
                elif reason == "reentry_max":
                    most = t.number((*row, "reentry_max"))
                    made = t.number((*row, "reentries"))
                    if most < 0 or most != most.to_integral_value() or made < most:
                        t.unknown(
                            (*row, "reentries"), "re-entries at the recorded maximum not shown"
                        )
                else:
                    t.unknown((*row, "reason"), f"not a refusal reason the writer gives: {reason}")
                t.stamp((*row, "when_ns"))
                t.counts["named_refusals"] += 1
    else:
        t.notes.append("no refusal rows; A1 reads recorded entries and any named drops")
    # mover.py:2336,2343 write both lists, and :4004 keeps a refusal row out of `dropped`, so the
    # named drops are counted beside the rows, never instead of them.
    # mover.py:1964-1965,3341: named refusals, mixed with notes. No commit boundary.
    dropped = (*RUN, "dropped")
    if t.optional(dropped) is not ABSENT:
        for index in range(len(t.array(dropped))):
            with t.scope():
                reason = t.text((*dropped, index)).partition(": ")[2]
                if any(re.fullmatch(pattern, reason) for pattern in NAMED_DROP_PATTERNS):
                    t.counts["named_refusals"] += 1
    # Counters are supplementary evidence, not another count of the same refusal.
    refusals = (*RUN, "sizing_refusals")
    if t.optional(refusals) is not ABSENT:
        for name in t.mapping(refusals):
            t.number((*refusals, name))


def _plans_cover_buys(t: Term) -> None:
    """Every filled BUY has the entry plan its client order ID names (mover.py:1977, entries are the
    orders sent; :1739-1746 the fills): a filled entry whose plan is missing is not shown, never met."""
    planned: set[str] = set()
    for index in range(len(t.array(ENTRIES))):
        with t.scope():
            planned.add(t.text((*ENTRIES, index, "client_order_id")))
    for cid, (_, path) in _bought(t).items():
        if cid not in planned:
            with t.scope():
                t.unknown((*path, 0), "filled BUY has no entry plan with its client order ID")


def _a1(t: Term) -> None:
    t.counts.update(fixed_plan_count=0, named_refusals=0, p5_tolerance_used=0)
    # mover.py:1977,3701-3733.
    with t.scope():
        for index in range(len(t.array(ENTRIES))):
            with t.scope():
                _a1_entry(t, (*ENTRIES, index))
    with t.scope():
        _plans_cover_buys(t)
    with t.scope():
        _a1_refusals(t)


def _tick(price: Decimal) -> Decimal:
    # price_step.py:31-33, the recorded Alpaca two/four decimal price contract.
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def _stepped_stop(price: Decimal, fraction: Decimal) -> Decimal:
    """sizing.py:70-79: the fractional loss rounded up, then the stop floored onto its step."""
    with localcontext() as context:
        context.prec, context.rounding = 28, ROUND_CEILING
        unstepped_loss = price * fraction
        context.rounding = ROUND_FLOOR
        unstepped_stop = price - unstepped_loss
        return unstepped_stop.quantize(_tick(unstepped_stop), rounding=ROUND_FLOOR)


def _a2_entry(t: Term, entry: KeyPath) -> None:
    # mover.py:3631-3649 and :3707,3710: all the sizing inputs actually recorded.
    sizing = (*entry, "sizing")
    t.mapping(sizing)
    numbers = {
        key: t.number((*sizing, key))
        for key in (
            "planned_loss",
            "trail",
            "stop",
            "loss_per_share",
            "quantity",
            "budget_notional",
            "flow_shares",
        )
    }
    price = _price(t, entry)
    quantity, stop = numbers["quantity"], numbers["stop"]
    if price <= 0 or price != price.quantize(_tick(price)):
        t.fail(
            (*sizing, "notional"), f"derived entry price {price} is not positive/on Alpaca's step"
        )
        return
    if quantity <= 0 or quantity != quantity.to_integral_value():
        t.fail((*sizing, "quantity"), "equity size must be positive whole shares")
        return
    if stop <= 0 or stop >= price or stop != stop.quantize(_tick(stop)):
        t.fail((*sizing, "stop"), "stop must be positive, below entry, and on Alpaca's step")
        return
    for key in ("planned_loss", "trail", "loss_per_share", "budget_notional", "flow_shares"):
        if numbers[key] <= 0:
            t.fail((*sizing, key), "an accepted sizing plan needs a positive amount")
            return
    if numbers["trail"] != t.number((*entry, "trail")):
        t.fail((*sizing, "trail"), "sizing distance differs from the opening plan")
    fraction = numbers["trail"]
    # mover.py:4340-4364,4409: an L rounded down under the spread floor is taken up onto the entry
    # step and the size taken again on that fraction, `sized_trail`, beside the plan's `trail`.
    if t.optional((*sizing, "sized_trail")) is not ABSENT:
        sized = t.number((*sizing, "sized_trail"))
        if sized != fraction:
            # :4351-4355: only an L rounded down on the entry step under price * floor is widened.
            if t.read((*entry, "spread_floor")) is None:
                t.fail((*sizing, "sized_trail"), "a plan with no spread floor is never widened")
                return
            least = price * t.number((*entry, "spread_floor"))
            planned = (price - _stepped_stop(price, fraction)).quantize(
                _tick(price), rounding=ROUND_FLOOR
            )
            if planned >= least:
                t.fail(
                    (*sizing, "sized_trail"),
                    f"the plan's distance {planned} holds the floor {least}: not widened",
                )
                return
            wider = (price - _stepped_stop(price, fraction)).quantize(
                _tick(price), rounding=ROUND_CEILING
            )
            with localcontext() as context:
                context.prec, context.rounding = 28, ROUND_FLOOR
                widened = wider / price
            if sized != widened:
                t.fail((*sizing, "sized_trail"), f"the plan widened onto the step is {widened}")
                return
            fraction = sized
    # sizing.py:45,144-147,173-174: even the stepped stop must stay within the ceiling.
    if numbers["trail"] > Decimal("0.10"):
        t.fail((*sizing, "trail"), "plan refuses distance_over_ceiling (0.10, UNCALIBRATED)")
    if price - stop > price * Decimal("0.10"):
        t.fail((*sizing, "stop"), "stepped stop exceeds the sizing plan's 0.10 ceiling")
    expected_stop = _stepped_stop(price, fraction)
    if stop != expected_stop:
        t.fail((*sizing, "stop"), f"plan's stepped stop is {expected_stop}")
    # mover.py:3600-3602: L floors on the ENTRY step, even if the stop crosses $1.
    distance = (price - stop).quantize(_tick(price), rounding=ROUND_FLOOR)
    if numbers["loss_per_share"] != distance:
        t.fail((*sizing, "loss_per_share"), f"recorded opening distance should be {distance}")
    # mover.py:3552-3567: cap combines flow, optional max_shares, and start_cap.
    # max_shares is omitted from rows when size_from_budget is on (:1916-1926).
    held_by = t.text((*sizing, "held_by"))
    caps = [numbers["budget_notional"] // price, numbers["flow_shares"]]
    start = t.read((*sizing, "start_cap"))
    if start is not None:
        caps.append(t.number((*sizing, "start_cap")))
    # sizing.py:169-202 and pinned risk sizing.rs:74-116: size against the stop,
    # BEFORE Mover floors L; using planned_loss/L would over-size a cross-dollar stop.
    expected = min(numbers["planned_loss"] / (price - stop), *caps).to_integral_value(
        rounding=ROUND_FLOOR
    )
    # mover.py:4263-4266: max_shares only lowers the cap, so more than `expected` is proven.
    if quantity > expected:
        t.fail((*sizing, "quantity"), f"planned loss and recorded caps allow at most {expected}")
        return
    if held_by == "max_shares":
        t.unknown(
            (*sizing, "held_by"),
            "max_shares cap not recorded: mover.py:1924 omits it with size_from_budget; "
            "mover.py:3625-3650 records no replacement cap",
        )
    if quantity != expected:
        t.fail(
            (*sizing, "quantity"), f"planned loss and recorded caps give {expected} whole shares"
        )


def _a2(t: Term) -> None:
    # mover.py:1977,3707.
    for index in range(len(t.array(ENTRIES))):
        with t.scope():
            _a2_entry(t, (*ENTRIES, index))
    with t.scope():
        _plans_cover_buys(t)


def _a3(t: Term) -> None:
    # mover.py:1975 exposes exits but has NO per-sale writer at this base.
    exits = t.array(EXITS)
    if not exits:
        t.unknown(EXITS, "no exits[].sales evidence; A3 is not shown by an empty exit list")
    for index in range(len(exits)):
        with t.scope():
            sales_path = (*EXITS, index, "sales")  # prospective per-sale record contract
            sales = t.array(sales_path)
            if not sales:
                t.unknown(sales_path, "no per-sale evidence for this holding")
            for sale in range(len(sales)):
                with t.scope():
                    row = (*sales_path, sale)
                    # Prospective trailing_offset / limit_offset / distance row.
                    trailing = t.read((*row, "trailing_offset"))
                    limit = t.read((*row, "limit_offset"))
                    if trailing is None and limit is None:
                        continue  # explicitly a plain limit, with no trailing offset to compare
                    distance = t.number((*row, "distance_in_force"))  # mover.py:2225
                    offset = t.number((*row, "trailing_offset"))
                    if distance <= 0 or offset != distance:
                        t.fail((*row, "trailing_offset"), f"distance in force is {distance}")
                    t.number((*row, "limit_offset"))


def _buys(t: Term) -> dict[str, tuple[int, KeyPath]]:
    found: dict[str, tuple[int, KeyPath]] = {}
    # mover.py:1739-1746,1968: [client_order_id, side, quantity, price, ts_event].
    for index in range(len(t.array(FILLS))):
        with t.scope():
            path = (*FILLS, index)
            side = t.text((*path, 1))
            if side not in {"BUY", "SELL"}:
                t.unknown((*path, 1), "BUY/SELL side not shown")
            if side != "BUY":
                continue
            cid = t.text((*path, 0))
            stamp = t.stamp((*path, 4))
            if cid not in found or stamp < found[cid][0]:
                found[cid] = (stamp, path)
    return found


def _entry_paths(t: Term) -> dict[str, KeyPath]:
    entries = {}
    # mover.py:1977,3702: join entries to exits by client_order_id, not by symbol.
    for index in range(len(t.array(ENTRIES))):
        with t.scope():
            path = (*ENTRIES, index)
            entries[t.text((*path, "client_order_id"))] = path
    return entries


def _a4(t: Term) -> None:
    t.counts.update(inside_60s=0, opening_l_stop_met_when_armed_inside_60s=0, p5_tolerance_used=0)
    buys, entries = _buys(t), _entry_paths(t)
    # mover.py:1197,1202-1204 supplies exit-to-entry identity and trigger evidence.
    for index in range(len(t.array(EXITS))):
        with t.scope():
            path = (*EXITS, index)
            cid = t.text((*path, "entry_client_order_id"))
            reason = t.text((*path, "trigger_reason"))
            triggered = t.stamp((*path, "trigger_ns"))
            if cid not in buys or cid not in entries:
                t.unknown((*path, "entry_client_order_id"), "matching entry and BUY fill not shown")
            # ADR 0106: a sale inside 60 s, timed by the sale; mover.py:1917-1920 sets
            # final_fill_ns only once the holding is sold, so an unsold exit is no sale.
            if t.read((*path, "final_fill_ns")) is None:
                continue
            elapsed = t.stamp((*path, "final_fill_ns")) - buys[cid][0]
            if elapsed < 0:
                t.unknown((*path, "final_fill_ns"), "sale precedes first BUY fill")
            if elapsed >= SIXTY_SECONDS_NS:
                continue
            t.counts["inside_60s"] += 1
            # mover.py:3048,3276,3313: non-distance wind-down/unpriced exits are named.
            if reason in {"wind-down", "unpriced_exit"}:
                continue
            entry = entries[cid]
            if _fixed_plan(t, entry):
                t.notes.append(
                    f"{json_path(path)}: quick sale on no usable spread: fixed plan; S5 observation only"
                )
                continue
            # mover.py:3729,4238-4245: ratchet timing; :4096-4118: other tightenings.
            tightened = t.read((*entry, "ratchet", "tightened"))
            when = t.read((*entry, "ratchet", "when_ns"))
            if not isinstance(tightened, bool):
                t.unknown((*entry, "ratchet", "tightened"), "boolean tightening evidence not shown")
            changed = tightened and (
                when is None or t.stamp((*entry, "ratchet", "when_ns")) <= triggered
            )
            changes = (*entry, "trail_changes")
            if t.optional(changes) is not ABSENT:
                for number in range(len(t.array(changes))):
                    changed |= t.stamp((*changes, number, "when_ns")) <= triggered
            # mover.py:2918-2929 explicitly names an L stop already met when armed.
            if reason == "trailing stop (L under the fill, met when armed)":
                if changed:
                    t.unknown(
                        (*path, "trigger_reason"),
                        "tightening prevents attribution to the opening distance",
                    )
                t.counts["opening_l_stop_met_when_armed_inside_60s"] += 1
                t.notes.append(
                    f"{json_path(path)}: opening L stop met when armed inside 60 seconds; counted"
                )
            else:
                meets_floor, used_tolerance = _opening_floor(t, entry)
                if used_tolerance:
                    t.counts["p5_tolerance_used"] += 1
                if meets_floor:
                    t.notes.append(f"{json_path(path)}: quick sale at/above opening floor; counted")
                else:
                    t.unknown(
                        (*path, "trigger_reason"),
                        "quick sale alone does not show opening-distance causation",
                    )


def _fill_rows(t: Term) -> None:
    """Each fill row is the writer's five fields (mover.py:1739-1746,1860): client order ID, side,
    a positive quantity and price, and its event clock; a short or malformed row is not shown."""
    for index in range(len(t.array(FILLS))):
        with t.scope():
            row = (*FILLS, index)
            if len(t.array(row)) != 5:
                t.unknown(row, "fill row is not [client_order_id, side, quantity, price, ts_event]")
            t.text((*row, 0))
            if t.text((*row, 1)) not in {"BUY", "SELL"}:
                t.unknown((*row, 1), "BUY/SELL side not shown")
            for field in (2, 3):
                if t.number((*row, field)) <= 0:
                    t.unknown((*row, field), "positive quantity and price not shown")
            t.stamp((*row, 4))


def _bought(t: Term) -> dict[str, tuple[Decimal | None, KeyPath]]:
    """Each BUY fill's client order ID with the shares bought under it, read without its clock: a
    bought holding needs its exit whenever it filled. An unread quantity leaves the total None; an
    unread row can only add shares, so it never makes a shortfall."""
    found: dict[str, tuple[Decimal | None, KeyPath]] = {}
    for index in range(len(t.array(FILLS))):
        with t.scope():
            path = (*FILLS, index)
            if t.text((*path, 1)) != "BUY":
                continue
            cid = t.text((*path, 0))
            total, first = found.get(cid, (Decimal(0), path))
            found[cid] = (None, first)
            if total is not None:
                found[cid] = (total + t.number((*path, 2)), first)
    return found


def _a5(t: Term) -> None:
    with t.scope():
        _fill_rows(t)
    bought = _bought(t)
    # mover.py:4473-4475,4515: every entry sent is recorded with its name, so a BUY is placed by its
    # own entry even when its exit row is labelled by a later one.
    sent: dict[str, str] = {}
    entries = t.optional(ENTRIES)
    for index in range(len(entries) if isinstance(entries, list) else 0):
        cid = t.optional((*ENTRIES, index, "client_order_id"))
        name = t.optional((*ENTRIES, index, "instrument_id"))
        if isinstance(cid, str) and isinstance(name, str):
            sent[cid] = name
    represented: dict[str, dict[str, KeyPath]] = {}
    first_row: dict[str, KeyPath] = {}
    identities_readable, names_unread = True, False
    unread: set[str] = set()
    remainders: dict[str, Decimal] = {}
    held: dict[str, Decimal] = {}
    # mover.py:1195-1205: holding identity, quantity and exit evidence.
    for index in range(len(t.array(EXITS))):
        path = (*EXITS, index)
        cid = instrument = None
        with t.scope():
            cid = t.text((*path, "entry_client_order_id"))
        with t.scope():
            instrument = t.text((*path, "instrument_id"))
        if instrument is None and cid in sent:
            instrument = sent[cid]  # :3201-3209, a row's name is its own entry's
        if cid is None:
            identities_readable = False
        else:
            rows = represented.setdefault(cid, {})
            if instrument is not None:
                rows.setdefault(instrument, path)
        if instrument is None:
            names_unread = True
        else:
            first_row.setdefault(instrument, (*path, "quantity"))
        read = False
        with t.scope():
            reason = t.read((*path, "trigger_reason"))
            if not isinstance(reason, str) or not reason.strip():
                t.fail((*path, "trigger_reason"), "holding has no named exit reason")
            quantity = t.number((*path, "quantity"))
            filled = t.number((*path, "filled_quantity"))
            if quantity <= 0 or filled < 0 or filled > quantity:
                t.fail(
                    (*path, "filled_quantity"),
                    "filled quantity must be between zero and holding quantity",
                )
            if instrument is not None:
                remainders[instrument] = remainders.get(instrument, Decimal(0)) + quantity - filled
                held[instrument] = held.get(instrument, Decimal(0)) + quantity
            read = True
            trigger_price = t.read((*path, "trigger_price"))
            # mover.py:3045-3050,3274-3276: these named exits may have no quoted trigger price.
            if (
                reason not in ("wind-down", "unpriced_exit")
                or trigger_price is not None
                or filled != quantity
            ):
                t.number((*path, "trigger_price"))
            triggered = t.stamp((*path, "trigger_ns"))
            final = t.read((*path, "final_fill_ns"))
            if quantity == filled and final is None:
                t.fail((*path, "final_fill_ns"), "sold holding has no final-fill evidence")
            elif filled < quantity and final is not None:
                # mover.py:1772,1798-1799: only a fully sold holding has a final clock.
                t.fail((*path, "final_fill_ns"), "unsold remainder cannot have a final-fill clock")
            elif final is not None and t.stamp((*path, "final_fill_ns")) < triggered:
                t.fail((*path, "final_fill_ns"), "final fill precedes exit decision")
        if not read and instrument is not None:
            unread.add(instrument)
    # :3201-3209: one entry's exit rows are all on its own slot's name, the name it was sent on.
    for cid, rows in represented.items():
        own = sent.get(cid) or next(iter(rows), None)
        for name, row in rows.items():
            if name != own:
                with t.scope():
                    t.fail((*row, "instrument_id"), f"an exit row of an entry on {own}")
    # mover.py:2062-2068,2076-2080: a BUY fill adds its shares to its name's current exit row,
    # which a late fill of an earlier entry finds labelled by a later entry, or by none after a
    # flat (:3201-3209,3268-3334); so the shares are compared by name, not by client order ID.
    shares_by_name: dict[str, Decimal | None] = {}
    first: dict[str, KeyPath] = dict(first_row)
    for cid, (shares, fill_path) in bought.items():
        rows = represented.get(cid, {})
        name = sent.get(cid) or (next(iter(rows)) if len(rows) == 1 else None)
        if name is None:
            if cid not in represented and identities_readable:
                t.fail((*fill_path, 0), "BUY fill has no named exit/holding record")
            continue
        first[name] = (*fill_path, 2)
        total = shares_by_name.get(name, Decimal(0))
        shares_by_name[name] = None if total is None or shares is None else total + shares
    for name in shares_by_name.keys() | held.keys():
        shares = shares_by_name.get(name, Decimal(0))
        recorded = held.get(name, Decimal(0))
        if shares is None or recorded == shares:
            continue
        with t.scope():
            # more held than bought means BUY fills missing from the record, which never fails a
            # term (an exit with no BUY reads incomplete); fewer is proven unless a row is unread
            if recorded > shares:
                t.unknown(first[name], "the record's BUY fills do not account for the shares held")
            if names_unread or name in unread:
                t.unknown(first[name], "the exit records' quantities are not all read")
            t.fail(first[name], f"exit records hold {recorded} of {shares} shares bought")
    # session_window.py:3345-3353: once the account check after the stop ends without raising, the
    # result is read again over the strategy's fills and the check's own closes (`check_fills`,
    # :1752-1755, rows (client order ID, instrument, side, quantity, price)) and says so in
    # `check_closes`; open_shares is then bought less sold over both (:1102-1135). A holding the check
    # closed is A6's breach (it names the check's close), not a wrong exit record.
    net: dict[str, Decimal] = {}
    fills_unread = False
    if t.optional(("mover", "result", "check_closes")) is not ABSENT:
        with t.scope():
            rows = t.array(("check_fills",))
            for index in range(len(rows)):
                read = False
                with t.scope():
                    row = ("check_fills", index)
                    side = t.text((*row, 2))
                    if side not in ("BUY", "SELL"):
                        t.unknown((*row, 2), "side not shown")
                    instrument = t.text((*row, 1))
                    quantity = t.number((*row, 3))
                    signed = quantity if side == "BUY" else -quantity
                    net[instrument] = net.get(instrument, Decimal(0)) + signed
                    read = True
                fills_unread = fills_unread or not read
    # session_window.py:1114-1137: each named instrument's open remainder.
    result_path: KeyPath = ("mover", "result", "by_instrument")
    instrument_rows = t.mapping(result_path)
    for instrument in remainders.keys() | instrument_rows.keys():
        with t.scope():
            path = (*result_path, instrument, "open_shares")
            opened = t.number(path)
            closed = net.get(instrument, Decimal(0))
            expected = remainders.get(instrument, Decimal(0)) + closed
            if opened != expected and fills_unread:
                t.unknown(path, "the account check's closes are not all read")
            elif opened != expected and (names_unread or instrument in unread):
                t.unknown(path, "the exit records' quantities are not all read")
            elif opened != expected and instrument not in remainders:
                t.fail(path, "open holding has no exit evidence/reason")
            elif opened != expected:
                t.fail(path, f"exit records leave {expected} shares")
            elif closed:
                t.notes.append(
                    f"{json_path(path)}: the account check after the stop closed {closed:+}; "
                    "A6 names it"
                )


def _a6(t: Term) -> None:
    # session_window.py:3357-3389 writes final result and ended (not a guessed `closed` key).
    with t.scope():
        result = t.text(("result",))
        if result.startswith("failed"):
            t.fail(("result",), "window record failed")
        elif result.startswith(("running", "stopping")):
            # session_window.py:1793 (`running`) and `_shown_record` :1907-1913 (`stopping`).
            t.fail(
                ("result",),
                "window never closed its record: the account may not be flat "
                "(a window still live reads the same; check its record after its end)",
            )
        elif result != "ok":
            t.unknown(("result",), "window is not recorded as closed ok")
    with t.scope():
        t.text(("ended",))
    # session_window.py:1140,1729-1737,3310-3335: account-check closes are NOT own wind-down.
    for path, expected in (
        (("mover", "result", "flat"), True),
        (("left",), []),
        (("closed_after_stop",), False),
        (("closes",), {}),
    ):
        with t.scope():
            value = t.read(path)
            if type(value) is not type(expected):
                t.unknown(path, f"expected {type(expected).__name__} evidence")
            if value != expected:
                t.fail(path, f"own wind-down requires {expected!r}")
    # session_window.py:1135,1154: open shares and flat come from the same quantities; a recorded
    # remainder contradicts a flat flag and is not flat.
    by_instrument = ("mover", "result", "by_instrument")
    with t.scope():
        if t.optional(by_instrument) is not ABSENT:
            for instrument in t.mapping(by_instrument):
                with t.scope():
                    path = (*by_instrument, instrument, "open_shares")
                    if t.number(path) != 0:
                        t.fail(path, "open shares recorded at the end")


def _refusal(t: Term, path: KeyPath, by: str | None = None) -> None:
    reason = t.text(path)  # sources are listed beside REFUSAL_PATTERNS above
    pattern, local = next(
        (
            (name, local)
            for name, regex, local in REFUSAL_PATTERNS
            if re.search(regex, reason, re.IGNORECASE)
        ),
        (None, False),
    )
    if pattern is None:
        t.notes.append(f"{json_path(path)}: refusal outside A7's three causes: {reason}")
    elif by == "before_venue" or (by is None and local):
        t.notes.append(f"{json_path(path)}: {pattern}, before venue")
    else:
        t.fail(path, f"venue refusal: {pattern}")


def _a7(t: Term) -> None:
    counted: set[str | KeyPath] = set()
    explained: set[str] = set()
    structured_reasons: set[tuple[str, str]] = set()

    def read_reason(path: KeyPath, cid: str | None, by: str | None = None) -> None:
        counted.add(path if cid is None else cid)
        reason = t.text(path)
        if cid is not None:
            explained.add(cid)
            if by is None and (cid, reason) in structured_reasons:
                return  # same reason already read with explicit venue/before-venue provenance
        _refusal(t, path, by)
        if by is not None and cid is not None:
            structured_reasons.add((cid, reason))

    # Prospective order-refusal rows; absent from mover.py:1912-1990 at this base.
    structured = (*RUN, "order_refusals")
    has_structured = t.optional(structured) is not ABSENT
    with t.scope():
        if has_structured:
            for index in range(len(t.array(structured))):
                with t.scope():
                    row = (*structured, index)
                    cid_path = (*row, "client_order_id")
                    cid = None if t.optional(cid_path) is ABSENT else t.text(cid_path)
                    by = t.text((*row, "by"))
                    if by not in {"venue", "before_venue"}:
                        t.unknown((*row, "by"), "venue/before_venue provenance not shown")
                    # The free-text reason classifies the refusal; a cause label cannot replace it.
                    read_reason((*row, "reason"), cid, by)
    # mover.py:1873-1877,1969: denials/rejections as [client_order_id, reason].
    with t.scope():
        refused = (*RUN, "refused")
        if not has_structured or t.optional(refused) is not ABSENT:
            for index in range(len(t.array(refused))):
                with t.scope():
                    read_reason((*refused, index, 1), t.text((*refused, index, 0)))
    # session_window.py:1738,4599-4601: same event reasons in the window's map.
    with t.scope():
        for cid in t.mapping(("denials",)):
            with t.scope():
                read_reason(("denials", cid), cid)
    # session_window.py:4583-4587: orders is scoped to our strategy's client order IDs.
    # :4600-4601 keeps denial/rejection reasons only, never a modify reason.
    # Pinned risk engine mod.rs:1003-1080 and this adapter's execution.py:2170
    # can reject a modify before it reaches the venue. An event name is not provenance.
    with t.scope():
        for cid in t.mapping(("orders",)):
            with t.scope():
                events = ("orders", cid)
                for index in range(len(t.array(events))):
                    with t.scope():
                        path = (*events, index)
                        event = t.text(path)
                        if event == "OrderModifyRejected":
                            counted.add(cid)
                            t.unknown(
                                path,
                                "record keeps no modify reason, so who refused is not shown; "
                                "Mover never calls modify_order today, so this path is unreachable "
                                "with today's strategy",
                            )
                        elif event == "OrderCancelRejected":
                            t.notes.append(
                                f"{json_path(path)}: OrderCancelRejected, outside A7's causes; "
                                "may race a fill, not counted"
                            )
                        elif event in {"OrderDenied", "OrderRejected"}:
                            counted.add(cid)
                            if cid not in explained:
                                t.unknown(path, "event name has no refusal reason/provenance")
    t.counts["refused_orders"] = len(counted)


def check(record: dict[str, Any]) -> Check:
    terms = [Term(f"A{index}", record) for index in range(1, 8)]
    with localcontext() as context:
        context.prec = 28
        fixed_arm = False
        with terms[0].scope():
            fixed_arm = _fixed_arm(terms[0])
        for term, evaluate in zip(terms, (_a1, _a2, _a3, _a4, _a5, _a6, _a7), strict=True):
            term.fixed_arm = fixed_arm
            with term.scope():
                if fixed_arm and term.name in {"A1", "A4"}:
                    term.notes.append("per-symbol distance is not evaluated on the fixed arm")
                    if term.name == "A1":
                        term.counts["fixed_arm_entries"] = len(term.array(ENTRIES))
                else:
                    evaluate(term)
    # mover.py:1977 entries are orders sent; :1739-1746 BUY fills establish actual entries.
    # Read through a term reader so the summary uses evidence printed in that term too.
    # A SELL fill with no BUY fill is a record that lost its entry, not a window without one.
    no_entry = unmatched_sale = False
    reader = terms[4] if fixed_arm else terms[3]  # A4 is not evaluated on the fixed arm
    with reader.scope():
        fills = reader.array(FILLS)
        sides = [reader.text((*FILLS, index, 1)) for index in range(len(fills))]
        if all(side in {"BUY", "SELL"} for side in sides):
            unmatched_sale = "BUY" not in sides and "SELL" in sides
            no_entry = not sides
    if fixed_arm:
        failed = ", ".join(term.name for term in terms if term.status == "failed")
        if failed:
            return Check(terms, f"practice (finding: {failed})")
        # With nothing bought there is no sale for A3 to judge; every other term still needs
        # its evidence, A6's close and A7's refusals above all.
        exempt = {"A3"} if no_entry else set()
        if unmatched_sale or any(
            term.status == "not shown" for term in terms if term.name not in exempt
        ):
            return Check(terms, "incomplete")  # shared evidence missing is not practice
        return Check(terms, "practice")
    if any(
        term.status == "failed"
        for term in terms
        if term.name in {"A1", "A2", "A3", "A5", "A6", "A7"}
    ):
        summary = "breaks"
    elif no_entry:
        summary = "no entry"
    elif unmatched_sale or any(term.status == "not shown" for term in terms if term.name != "A4"):
        summary = "incomplete"
    else:
        summary = "counts"
    return Check(terms, summary)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"non-JSON numeric constant {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "unreadable: usage: python -m canon_trading.d3_check <record.json> "
            "(exit codes: counts/practice 0, breaks 1, incomplete 2, no entry 3, "
            "unreadable 4, practice finding 5)"
        )
        return 4
    try:
        record = json.loads(
            Path(args[0]).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=Decimal,
        )
        if not isinstance(record, dict):
            raise TypeError("window record must be a JSON object")
    # DecimalException: parse_float=Decimal raises it for a number Decimal cannot hold
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        DecimalException,
    ) as error:
        print(f"unreadable: {type(error).__name__}: {error}")
        return 4
    result = check(record)
    print("\n".join(result.lines()))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
