"""Sanitized request-budget records, not an account's reported limit (ADR 0083).

Contract: Alpaca's archived Trading API documentation, "Rate Limit", states 200 requests/minute/key
(https://raw.githubusercontent.com/alpacahq/alpaca-docs/master/content/api-references/trading-api/_index.md,
read 2026-11-11). `canon_alpaca.providers.request_counts` counts transport attempts per account/API,
including retries, and retains fixed UTC-minute and rolling 60-second peaks, not minute buckets.

The observation clock starts at this module's import, before the window's preflight requests. It is the
wall clock (`time.time`), the counter's own: a Mac's monotonic clock stops while the machine sleeps, which
would inflate the average over a slept window (`session_window._run_until`, 2026-11-11). Totals
and averages cover this process, including preflight, the node, and cleanup, rather than node runtime.
The setting below is a documented planning budget; no account limit or response header is read here.
Current paper/live enforcement and per-key versus per-account scope are UNVERIFIED (ADRs 0049/0069).
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from typing import Any

from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import RequestCount

TRADING_REQUEST_LIMIT_PER_MINUTE = 200
_OBSERVATION_STARTED = time.time()


def observation_seconds() -> float:
    """Elapsed process observation, including the window's preflight and cleanup requests."""
    return time.time() - _OBSERVATION_STARTED


def check_limit(limit_per_minute: object) -> None:
    """Refuse a limit setting that is not a positive whole number (a bool is not one)."""
    if type(limit_per_minute) is not int or limit_per_minute <= 0:
        raise ValueError("request limit per minute must be a positive integer setting")


def calibration_eligible(status: str, elapsed_seconds: object) -> bool:
    """Only a completed measurement over a known, finite, positive interval calibrates the budget."""
    return (
        status == "complete"
        and isinstance(elapsed_seconds, (int, float))
        and not isinstance(elapsed_seconds, bool)
        and math.isfinite(elapsed_seconds)
        and elapsed_seconds > 0
    )


def request_record(
    counts: Iterable[RequestCount],
    *,
    elapsed_seconds: float | None = None,
    limit_per_minute: int = TRADING_REQUEST_LIMIT_PER_MINUTE,
    measurement_status: str = "unfinalized",
    ceiling: int | None = None,
) -> dict[str, Any]:
    """Record the adapter's sanitized snapshots against a setting; never infer missing minute bins.

    The rolling peak, rather than a fixed-minute peak, supplies headroom, so a burst across a minute
    boundary cannot disappear. A missing/zero interval leaves the average unknown. A market-data
    API row is retained but is never compared with the Trading API's budget. Only an explicitly
    completed measurement with a known positive observation interval is eligible for request-budget
    calibration; a running or interrupted snapshot still describes traffic, not a completed window.
    `ceiling`, the count in any 60 seconds at which a window fails (`session_window.REQUEST_CEILING`),
    is what the Trading API's headroom is measured against when given, else the limit setting (review
    of 3a8613a, cut list: a failed window's peak of 185 recorded 15 of headroom against 200).
    """
    check_limit(limit_per_minute)
    if ceiling is not None:
        check_limit(ceiling)
    if elapsed_seconds is not None and (not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
        raise ValueError("request observation seconds must be finite and nonnegative")
    rows = []
    for count in counts:
        limit = limit_per_minute if count.api == TRADING_API else None
        rows.append(
            {
                "account": count.account,
                "api": count.api,
                "total": count.total,
                "average_per_minute": count.total * 60 / elapsed_seconds
                if elapsed_seconds
                else None,
                "peak_minute": count.peak,
                "peak_minute_at": count.peak_minute.isoformat() if count.peak_minute else None,
                "peak_60s": count.peak_60s,
                "limit_per_minute": limit,
                "ceiling_60s": ceiling if limit is not None else None,
                "headroom_60s": (ceiling or limit) - count.peak_60s if limit is not None else None,
                "rate_limited": count.rate_limited_total,
            }
        )
    return {
        "limit_per_minute": limit_per_minute,
        "minute_buckets_kept": False,
        "count_scope": "process",
        "observation_seconds": elapsed_seconds,
        "measurement_status": measurement_status,
        "calibration_eligible": calibration_eligible(measurement_status, elapsed_seconds),
        "by_api": rows,
    }
