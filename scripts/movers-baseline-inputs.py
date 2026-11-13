"""Prepare ADR 0087's evening baseline inputs, through paper:history's one paper pair.

Contracts read at the pins: alpaca-py c42add7c0a70 trading/client.py get_calendar
(GetCalendarRequest inclusive dates) and get_all_assets; the adapter's GET-only timeout,
assets_request and parse_equity; NautilusTrader d53849b216e2 persistence/__init__.pyi
ParquetDataCatalog.write_instruments; history_load's catalog lock and publication delay;
rank_shared.write_atomically (Python os.replace). No account or stream lock is taken.
This script prepares inputs only: the existing history and baseline tasks own bars and arithmetic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from canon_alpaca.providers import assets_request
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import data_only
from canon_alpaca.providers import parse_equity
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import session_of
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.persistence import ParquetDataCatalog
from requests.exceptions import RequestException

from canon_trading import history_load
from canon_trading import movers_rank
from canon_trading.rank_shared import write_atomically

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_DAYS = 60  # bounded query; an insufficient calendar fails, never invents a holiday


class CalendarDay(Protocol):
    date: date


def select_sessions(
    now: datetime, calendar: Sequence[CalendarDay]
) -> tuple[date, tuple[date, ...]]:
    """The first calendar date on/after the runner's 20:00 New York rollover, and its lookback."""
    if now.tzinfo is None:
        raise ValueError("the calendar clock needs a time zone")
    local = now.astimezone(NEW_YORK)
    earliest = local.date() + timedelta(days=local.hour >= 20)
    days = [day.date for day in calendar]
    if days != sorted(set(days)):
        raise ValueError("calendar dates must be distinct and ordered")
    target = next((day for day in days if day >= earliest), None)
    if target is None:
        raise ValueError(f"calendar has no session on or after {earliest}")
    previous = tuple(day for day in days if day < target)[-movers_rank.BASELINE_SESSIONS :]
    if len(previous) != movers_rank.BASELINE_SESSIONS:
        raise ValueError(f"calendar lacks {movers_rank.BASELINE_SESSIONS} sessions before {target}")
    return target, previous


def calendar_client() -> TradingClient:
    if not data_only():
        raise ValueError("baseline inputs require CANON_ALPACA_DATA_ONLY=1 through paper:history")
    key, secret = credentials_from_env()
    client = with_http_timeout(TradingClient(api_key=key, secret_key=secret, paper=True))
    require_get_only(client)
    session = session_of(client)
    assert session is not None
    session.hooks["response"].append(history_load.stop_at_429)
    return client


def prepare_inputs(
    directory: Path,
    catalog_path: Path,
    baseline_root: Path,
    *,
    now: datetime | None = None,
    client: TradingClient | None = None,
) -> dict[str, str | bool | int]:
    now = now or datetime.now(UTC)
    local = now.astimezone(NEW_YORK)
    for path in (directory, catalog_path, baseline_root):
        if not path.is_absolute() or "ledger" in path.resolve().parts:
            raise ValueError("baseline input paths must be absolute and outside ledger/")
    # A failed preparation never leaves yesterday's plan available to the shell.
    (directory / "plan.json").unlink(missing_ok=True)
    client = client if client is not None else calendar_client()
    calendar = client.get_calendar(
        GetCalendarRequest(
            start=local.date() - timedelta(days=CALENDAR_DAYS),
            end=local.date() + timedelta(days=CALENDAR_DAYS),
        )
    )
    if not isinstance(calendar, list):
        raise TypeError("Alpaca returned no calendar list")
    target, previous = select_sessions(now, calendar)
    end = datetime.combine(previous[-1], time(20), NEW_YORK)
    if end > now - history_load.margin("1-MINUTE"):
        raise ValueError(f"SIP session {previous[-1]} is not complete and published yet")
    start = datetime.combine(previous[0], time(4, 1), NEW_YORK)
    output = baseline_root / target.isoformat() / "sip.json"
    cached = output.is_file() and movers_rank.read_baselines(output, target).problem is None
    plan: dict[str, str | bool | int] = {
        "session": target.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "complete": cached,
        "deadline_epoch": int(datetime.combine(target, time(4), NEW_YORK).timestamp()),
    }
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(directory / "sessions.txt", "".join(f"{day}\n" for day in (*previous, target)))
    if not cached:
        assets = client.get_all_assets(assets_request(None))
        if not isinstance(assets, list) or not assets:
            raise ValueError("Alpaca returned no active US-equity asset list")
        stamp = int(now.timestamp() * 1_000_000_000)
        equities = [parse_equity(asset, stamp) for asset in assets]
        symbols = sorted({asset.symbol for asset in assets})
        catalog_path.mkdir(parents=True, exist_ok=True)
        with history_load._catalog_lock(catalog_path):
            history_load._check_feed(catalog_path)
            ParquetDataCatalog(str(catalog_path)).write_instruments(equities)
        write_atomically(directory / "symbols.txt", "".join(f"{symbol}\n" for symbol in symbols))
    write_atomically(directory / "plan.json", json.dumps(plan, allow_nan=False))
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = prepare_inputs(args.directory, args.catalog, args.baseline_dir)
    except (OSError, ValueError, TypeError, RuntimeError, APIError, RequestException) as exc:
        print(f"movers-baseline-inputs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"movers-baseline-inputs: SIP trade date {plan['session']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
