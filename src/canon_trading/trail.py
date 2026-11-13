"""ADR 0096 amendment 10's profile arm, as pure arithmetic over the T5 profile contract.

Every setting is lead, no primary source, UNCALIBRATED. The document is the strict
trail_profile.read_profile result; its same-segment recent statistic precedes long independently
for range and pullback. No engine object or data request belongs here. The clock is movers_rank's
20:00-to-20:00 New York part, also used by trail_profile.segment_profile.
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from . import movers_rank

PROFILE_SETTINGS: dict[str, dict[str, Any]] = {
    "profile_k": {"default": Decimal(2), "alternates": (Decimal(1), Decimal(3))},
    "profile_pctl": {"default": "p75", "arms": ("median", "p75", "p90")},
    "profile_floor": {"default": "median", "arms": ("median", "p75", "off")},
    "profile_multipliers": {
        "default": (Decimal(1), Decimal(1), Decimal(1)),
        "alternates": ((Decimal(1), Decimal("1.25"), Decimal("1.5")),),
    },
}
for _row in PROFILE_SETTINGS.values():
    _row.update(status="UNCALIBRATED", source="lead, no primary source")

RDC_EDGES = (Decimal(3), Decimal(10))
MIN_RECENT_SESSIONS = 5
"""Constants of amendment 10, not settings or additional arms."""


def profile_session_date(now_ns: int) -> date:
    """The ranking clock's session date, including the preceding New York evening."""
    local = movers_rank.ny_time(now_ns)
    return local.date() + timedelta(days=int(local.time() >= movers_rank.PART_START))


def profile_segment(now_ns: int) -> int:
    """Index the same part and segment as movers_rank and the profile builder."""
    start = movers_rank.part_start_ns(profile_session_date(now_ns))
    segment = (now_ns - start) // movers_rank.SEGMENT_NS
    if not 0 <= segment < movers_rank.SEGMENTS:
        raise ValueError("profile time is outside the ranking clock's 96-segment part")
    return segment


def validate_profile_settings(
    profile_k: Decimal,
    profile_pctl: str,
    profile_floor: str,
    profile_multipliers: tuple[Decimal, Decimal, Decimal],
) -> None:
    """Accept exactly the registered bases and alternates, including Decimal tuple elements."""
    values = {
        "profile_k": profile_k,
        "profile_pctl": profile_pctl,
        "profile_floor": profile_floor,
        "profile_multipliers": profile_multipliers,
    }
    for name, value in values.items():
        row = PROFILE_SETTINGS[name]
        if name == "profile_k" and (not isinstance(value, Decimal) or not value.is_finite()):
            raise ValueError(f"{name} needs a finite registered Decimal")
        if name == "profile_multipliers" and (
            type(value) is not tuple
            or any(not isinstance(v, Decimal) or not v.is_finite() for v in value)
        ):
            raise ValueError(f"{name} needs a registered tuple of Decimals")
        allowed = row.get("arms", (row["default"], *row.get("alternates", ())))
        if value not in allowed:
            raise ValueError(f"{name} must be one of {allowed}")


def _statistic(
    symbol: dict[str, Any], segment: int, metric: str, percentile: str
) -> tuple[Decimal | None, str | None]:
    """L8's window choice: recent when present, else long, separately for R and F. A window the file
    does not carry (its rows are null: the recent-only file has no long) has no statistic to give."""
    for window in ("recent", "long"):
        rows = symbol.get(window)
        if rows is None:
            continue
        value = rows[segment][metric][percentile]
        if value is not None:
            return Decimal(str(value)), window
    return None, None


def profile_distance(
    document: dict[str, Any],
    symbol: str,
    segment: int,
    atr: Decimal | None,
    rdc_1: Decimal | float | None,
    *,
    profile_k: Decimal = Decimal(2),
    profile_pctl: str = "p75",
    profile_floor: str = "median",
    profile_multipliers: tuple[Decimal, Decimal, Decimal] = (Decimal(1), Decimal(1), Decimal(1)),
) -> dict[str, Any]:
    """max(K * max(ATR, R), F) * V before bounds, or a named thin reason.

    ATR is already a fraction of price; a cold ATR leaves R as the slot-time prior. Only the recent
    session count and a missing R make a present symbol thin; the file's metric/segment thin flags
    do not forbid using a long statistic. An unavailable pullback differs from an explicit off.
    """
    validate_profile_settings(profile_k, profile_pctl, profile_floor, profile_multipliers)
    if type(segment) is not int or not 0 <= segment < movers_rank.SEGMENTS:
        raise ValueError("profile segment must be an index from 0 to 95")
    if atr is not None and (not atr.is_finite() or atr < 0):
        raise ValueError("atr must be finite and nonnegative, or None while cold")
    ratio = None if rdc_1 is None else Decimal(str(rdc_1))
    unknown = ratio is None or not ratio.is_finite() or ratio <= 0
    bucket = 0 if unknown else sum(ratio >= edge for edge in RDC_EDGES)
    result: dict[str, Any] = {
        "symbol": symbol,
        "segment": segment,
        "K": profile_k,
        "pctl": profile_pctl,
        "R": None,
        "r_window": None,
        "ATR": atr,
        "floor_setting": profile_floor,
        "floor": profile_floor,
        "F": None,
        "f_window": None,
        "ratio": "unknown" if unknown else ratio,
        "bucket": ("under 3", "3 to under 10", "10 and over")[bucket],
        "V": profile_multipliers[bucket],
        "multipliers": profile_multipliers,
        "distance": None,
        "reason": None,
    }
    rows = document["symbols"].get(symbol)
    if rows is None:
        result["reason"] = "not in the profile"
        return result
    if rows.get("recent") is None:
        result["reason"] = "no recent rows"
        return result
    sessions = rows["recent"][segment]["sessions"]
    if sessions < MIN_RECENT_SESSIONS:
        result["reason"] = f"{sessions} recent sessions"
        return result
    r, result["r_window"] = _statistic(rows, segment, "true_range", profile_pctl)
    result["R"] = r
    if r is None:
        result["reason"] = "no true_range statistic"
        return result
    f = None
    if profile_floor != "off":
        f, result["f_window"] = _statistic(rows, segment, "pullback", profile_floor)
        if f is None:
            result["floor"] = "absent: no pullback statistic"
    result["F"] = f
    scaled = profile_k * (r if atr is None else max(atr, r))
    result["distance"] = (scaled if f is None else max(scaled, f)) * result["V"]
    return result


def profile_record(result: dict[str, Any]) -> dict[str, Any]:
    """The calculation's plain JSON values, preserving Decimal text exactly."""
    return {
        key: str(value)
        if isinstance(value, Decimal)
        else [str(v) for v in value]
        if isinstance(value, tuple)
        else value
        for key, value in result.items()
    }
