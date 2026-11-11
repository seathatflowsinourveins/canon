"""Against Alpaca's paper environment: the minute bars of ADR 0077's quote study into the repo's catalog.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped. `mise run paper:test -k quote_study_bars` runs it alone under the
paper account's lock, since mise passes what follows the task name on to pytest (ADR 0040). Never write
the keys to a file.

What it calls: the asset lookup goes to Alpaca's paper Trading API and the bars to Alpaca's Market Data
API through `canon_trading.bars_to_catalog.load_bars`, as `test_paper_variant_study_bars.py` does for
ADR 0048's study. It places, modifies and cancels nothing, opens no position and no stream, so it leaves
the account as it found it.

Two feeds, each in its own catalog root, since the catalog keys bars by bar type and not feed
(`canon_trading.bars_to_catalog`, "Feeds"): IEX, the study's registered feed, into `catalog/`, and SIP
into `catalog/sip/`, loaded beside it so either can be the study's data identity (ADR 0077). The SIP
case is an expected failure while the adapter refuses SIP's finer prices (`FEEDS`).

The window is the study's sample: bars whose `ts_init` lies from 00:00 New York on 2025-11-03 to 18:00
New York on 2026-10-30, the last full session day of October 2026. ADR 0077 registered it to 2025-11-28,
before any data of it was loaded, and lengthened it on 2026-11-11, after that month's bars were loaded
and before any quote and any trial; the record owns the change. A minute bar's `ts_init` is its open plus
one minute (`canon_alpaca.data`, "Bar windows"). The bars are loaded before
the quotes, which wait on the unit of Alpaca's quote sizes, so the catalog holds the instrument
definitions and the study's session days can be read before any quote or trial.

What is asserted, on each feed, is what the study needs from its bars: both symbols have bars on the
same UTC days, the catalog holds their definitions, no bar falls on an exchange holiday of the window
(`CLOSED`, from NYSE Group's 2025 to 2027 calendar) and every other weekday has bars. On IEX, also:
every bar is booked on the UTC day of its New York open, so the study's session days (ADR 0041 terms 1
and 2, `canon_trading.ledger.session_day`) are exactly those 250 trading days for both symbols: T =
250, the first after the account's registration day and the last the session day of the sample's end.
What is printed, not asserted: the count of bars, the first and last bar of each early-close day
(`EARLY`), and SIP's UTC session days, since a SIP bar whose `ts_init` is after 00:00 UTC (19:00 New York
in winter, 20:00 in summer) is booked on the next UTC day.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import VENUE
from canon_alpaca.data import NEW_YORK
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.bars_to_catalog import load_bars
from canon_trading.ledger import session_day

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

REPO_CATALOG = Path(__file__).parents[1] / "catalog"
PRECISION_REFUSAL = "is not representable at price precision"
"""The adapter's words when it refuses a price finer than the instrument's precision (`canon_alpaca.data`)."""


class PrecisionRefused(RuntimeError):
    """A load that failed only because the adapter refused a price finer than the instrument's precision."""


FEEDS = (
    pytest.param("iex", REPO_CATALOG, None, id="iex"),
    pytest.param(
        "sip",
        REPO_CATALOG / "sip",
        "sip",
        id="sip",
        # Loaded 2026-11-11: a SIP bar after 2025-11-28 holds a price finer than the instrument's
        # precision of 4 (a high of 747.086488), which the adapter refuses rather than round
        # (`canon_alpaca.data`). Strict, so the case reports when that changes (ADR 0077).
        # Only that refusal counts: any other failure of the case is a failure (`PrecisionRefused`).
        marks=pytest.mark.xfail(
            strict=True, raises=PrecisionRefused, reason="SIP prices finer than precision 4"
        ),
    ),
)
"""Each feed, its catalog root and the feed `load_bars` names: none for IEX, the adapter's default, as
the catalog's IEX bars were loaded before. IEX is the study's registered feed (ADR 0077)."""

# ADR 0077's plan. Once a trial is recorded, changing any of it is a new study, not an edit of this one; the
# one change made before, the lengthened sample, is owned by the record.
SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")
WINDOW_START = datetime(2025, 11, 3, 0, tzinfo=NEW_YORK)
WINDOW_END = datetime(2026, 10, 30, 18, tzinfo=NEW_YORK)
CLOSED: frozenset[date] = frozenset(
    {
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King, Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day, observed
        date(2026, 9, 7),  # Labor Day
    }
)
"""The weekdays of the window the market is closed, from NYSE Group's 2025 to 2027 calendar (ADR 0077)."""
EARLY: frozenset[date] = frozenset({date(2025, 11, 28), date(2025, 12, 24)})
"""The early closes of the window, at 1:00 p.m. New York, from the same calendar."""

MINUTE_NS = 60 * 1_000_000_000


def bar_type(symbol: str) -> BarType:
    """One-minute bars of `symbol`, last price, from Alpaca on the IEX feed (ADR 0040, ADR 0077)."""
    return BarType.from_str(f"{symbol}.{VENUE}-1-MINUTE-LAST-EXTERNAL")


def weekdays(start: date, end: date) -> set[date]:
    """Every Monday to Friday from `start` to `end`, both included."""
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return {day for day in days if day.weekday() < 5}


def new_york(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1e9, UTC).astimezone(NEW_YORK)


def utc_date(ts: int) -> date:
    return datetime.fromtimestamp(ts / 1e9, UTC).date()


@pytest.mark.parametrize(("feed", "root", "named"), FEEDS)
def test_paper_quote_study_bars_into_the_catalog(feed: str, root: Path, named: str | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(root))
    expected_days = weekdays(WINDOW_START.date(), WINDOW_END.date()) - CLOSED
    assert len(expected_days) == 250
    sessions: dict[str, list[date]] = {}
    for symbol in SYMBOLS:
        bars_type = bar_type(symbol)
        try:
            loaded = load_bars(
                bars_type,
                WINDOW_START,
                WINDOW_END,
                root,
                timeout_secs=900,
                feed=named,
            )
        except RuntimeError as exc:
            if PRECISION_REFUSAL in str(exc):
                raise PrecisionRefused(str(exc)) from exc
            raise
        bars = catalog.query_bars(
            [str(bars_type)],
            start=dt_to_unix_nanos(WINDOW_START),
            end=dt_to_unix_nanos(WINDOW_END),
        )
        print(f"{feed} {bars_type}: {len(bars)} bars in the window; catalog {loaded}")
        assert bars, f"no {feed} {bars_type} bars in the window"
        assert catalog.instruments([str(bars_type.instrument_id)])
        days: set[date] = set()
        early: dict[date, list[datetime]] = {day: [] for day in EARLY}
        booked: set[date] = set()
        late = []
        for bar in bars:
            opened = new_york(bar.ts_init - MINUTE_NS)
            days.add(opened.date())
            if opened.date() in early:
                early[opened.date()].append(opened)
            on = utc_date(session_day(bar.ts_init))
            booked.add(on)
            if on != opened.date():
                late.append(opened)
        assert days == expected_days, (
            f"{feed} {symbol}: bars on {sorted(days - expected_days)}, "
            f"none on {sorted(expected_days - days)}; none is expected on {sorted(CLOSED)}"
        )
        for day, opens in sorted(early.items()):
            print(
                f"{feed} {symbol} {day}: {len(opens)} bars, "
                f"opens {opens[0]:%H:%M} to {opens[-1]:%H:%M}"
            )
        sessions[symbol] = sorted(booked)
        print(
            f"{feed} {symbol}: {len(sessions[symbol])} UTC session days, {len(late)} bars booked on the "
            "UTC day after their New York open"
        )
        if feed == "iex":
            assert not late, f"{symbol}: the bar opening {late[0]:%Y-%m-%d %H:%M} New York is late"
    first, *others = SYMBOLS
    for symbol in others:
        assert sessions[symbol] == sessions[first], (
            f"{feed}: {symbol} and {first} have bars on different UTC days"
        )
    # On IEX every bar of each symbol is booked on its New York day, and both symbols' days are the 250
    # above, so they share the study's session days; the first, 2025-11-03, is after the registration
    # day (2025-11-02) and the last, 2026-10-30, is the session day of the sample's end.
    assert utc_date(session_day(dt_to_unix_nanos(WINDOW_END))) == max(expected_days)
    assert utc_date(session_day(dt_to_unix_nanos(WINDOW_START))) == min(expected_days)
