"""The movers probe's screener poll: Alpaca's movers and most-actives, read once a minute, in data-only mode
(ADR 0070, its dated addition of 2026-11-11).

Contract: alpaca-py 0.45.0 (the pin), used as shipped: `alpaca.data.historical.screener.ScreenerClient`,
`get_market_movers(MarketMoversRequest(top=...))` and `get_most_actives(MostActivesRequest(by=..., top=...))`
(`alpaca/data/requests.py`), whose answers are `alpaca.data.models.screener.Movers` and `MostActives`.
Alpaca's pages: "Movers" (`GET /v1beta1/screener/{market_type}/movers`, `top` at most 50, gainers and losers
from the previous close, reset at market open) and "Most Actives" (`GET /v1beta1/screener/stocks/most-actives`,
`by` volume or trades, `top` at most 100). Nothing here computes a screen; each answer is logged as it came.

How it is kept from trading (ADR 0070, terms 3 and 5): it runs in data-only mode, which `mise run paper:data`
sets and `main` sets again; its one REST client goes through `canon_alpaca.providers.with_http_timeout`, whose
adapter then refuses every method but GET before anything is sent, and `require_get_only` checks that before
the first request. It builds no trading client and opens no stream, so it takes no stream lock: a session
window or a streaming run is never kept waiting by it.

Bounds: one poll a minute (three GETs to the market data API), at most `MAX_MINUTES` polls a run, so a run
ends inside the task's deadline; a person or a schedule starts the next. Each poll is one JSON line in the log
folder `mise run paper:data` passes, outside the repository. A failed read is logged and counted, the run goes
on, and the run exits 1 if any read failed; a refused method ends it at once. The filename includes
`os.getpid()` (CPython's process-ID contract), so simultaneous scheduled and manual runs keep separate logs.

UNVERIFIED (Alpaca's pages do not say, so a run's lines are the measurement): how often the screener updates,
whether pre-market and after-hours trades move it, and what it shows before the open.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol

from alpaca.common.exceptions import APIError
from alpaca.data.enums import MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.models.screener import MostActives
from alpaca.data.models.screener import Movers
from alpaca.data.requests import MarketMoversRequest
from alpaca.data.requests import MostActivesRequest
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import data_only
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import with_http_timeout

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Sequence

MAX_MINUTES = 85
"""The most polls a run makes, one a minute, so a run and its start-up end inside `paper:data`'s deadline."""
POLL_SECS = 60.0
MOVERS_TOP = 50
"""Alpaca's most for movers ("Movers", `top`): 50 gainers and 50 losers."""
ACTIVES_TOP = 100
"""Alpaca's most for most-actives ("Most Actives", `top`)."""
LOG_PREFIX = "movers-screener"


class Screener(Protocol):
    """The part of alpaca-py's `ScreenerClient` this module uses (signatures as in alpaca-py 0.45.0)."""

    def get_market_movers(self, request_params: MarketMoversRequest) -> object: ...

    def get_most_actives(self, request_params: MostActivesRequest) -> object: ...


class DataOnlyRefused(RuntimeError):
    """The process is not in data-only mode, so no client is built."""


def screener_client() -> ScreenerClient:
    """alpaca-py's `ScreenerClient` on the environment's pair, its session's adapter refusing every method
    but GET, checked before it is returned (ADR 0070, term 3). Refused outside data-only mode."""
    if not data_only():
        raise DataOnlyRefused(
            f"the screener poll runs in data-only mode only ({DATA_ONLY_ENV}=1, ADR 0070); nothing was built"
        )
    api_key, secret_key = credentials_from_env()
    client = with_http_timeout(ScreenerClient(api_key=api_key, secret_key=secret_key))
    require_get_only(client)
    return client


def movers_line(movers: object) -> dict[str, object]:
    """Alpaca's movers answer as logged: its time and each gainer and loser as it came."""
    if not isinstance(movers, Movers):
        raise TypeError("alpaca-py returned raw movers data; the client must use raw_data=False")
    return {
        "last_updated": movers.last_updated.isoformat(),
        "gainers": [m.model_dump() for m in movers.gainers],
        "losers": [m.model_dump() for m in movers.losers],
    }


def actives_line(actives: object) -> dict[str, object]:
    """Alpaca's most-actives answer as logged."""
    if not isinstance(actives, MostActives):
        raise TypeError(
            "alpaca-py returned raw most-actives data; the client must use raw_data=False"
        )
    return {
        "last_updated": actives.last_updated.isoformat(),
        "most_actives": [a.model_dump() for a in actives.most_actives],
    }


def poll(client: Screener) -> tuple[dict[str, object], int]:
    """One poll: movers, then most-actives by volume and by trades. Returns the line's reads and how many
    failed; a failed read is logged by name and the others still run. `MethodRefused` is not caught."""
    reads: dict[str, Callable[[], dict[str, object]]] = {
        "movers": lambda: movers_line(
            client.get_market_movers(MarketMoversRequest(top=MOVERS_TOP))
        ),
        "most_actives_volume": lambda: actives_line(
            client.get_most_actives(MostActivesRequest(by=MostActivesBy.VOLUME, top=ACTIVES_TOP))
        ),
        "most_actives_trades": lambda: actives_line(
            client.get_most_actives(MostActivesRequest(by=MostActivesBy.TRADES, top=ACTIVES_TOP))
        ),
    }
    line: dict[str, object] = {}
    failed = 0
    for name, read in reads.items():
        try:
            line[name] = read()
        except MethodRefused:
            raise
        # alpaca-py turns an HTTP error answer into `APIError` and lets requests' network errors through
        # (`RESTClient._one_request`), which derive from OSError; a malformed answer fails validation.
        except (APIError, OSError, ValueError, TypeError) as e:
            line[name] = {"error": f"{type(e).__name__}: {e}"}
            failed += 1
    return line, failed


def log_path(log_dir: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{LOG_PREFIX}-{stamp}-{os.getpid()}.jsonl"


def run(
    client: Screener,
    minutes: int,
    log: Path,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll `minutes` times, `POLL_SECS` apart on `clock`, one JSON line each in `log`; then a summary line
    with the requests counted. Returns how many reads failed."""
    start = clock()
    failed = 0
    with log.open("a", encoding="utf-8") as out:
        for n in range(minutes):
            wait = start + n * POLL_SECS - clock()
            if wait > 0:
                sleep(wait)
            line, bad = poll(client)
            failed += bad
            record = {"poll": n + 1, "at": datetime.now(UTC).isoformat(), "failed": bad, **line}
            out.write(json.dumps(record) + "\n")
            out.flush()
        summary = {
            "summary": True,
            "polls": minutes,
            "failed_reads": failed,
            "requests": [
                {
                    "account": c.account,
                    "api": c.api,
                    "total": c.total,
                    "peak": c.peak,
                    "rate_limited": c.rate_limited_total,
                }
                for c in request_counts()
            ],
        }
        out.write(json.dumps(summary) + "\n")
    return failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.movers_screener", description=__doc__
    )
    parser.add_argument(
        "--minutes", type=int, default=10, help=f"polls, one a minute, 1 to {MAX_MINUTES}"
    )
    parser.add_argument(
        "--log-dir", type=Path, required=True, help="an absolute folder outside the repository"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be 1 to {MAX_MINUTES}")
    if not args.log_dir.is_absolute():
        parser.error(f"--log-dir must be an absolute folder, got {str(args.log_dir)!r}")
    os.environ[DATA_ONLY_ENV] = (
        "1"  # the task sets it too; the module does not rely on that (ADR 0070)
    )
    client = screener_client()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log = log_path(args.log_dir, datetime.now(UTC))
    print(f"movers-screener: {args.minutes} polls, log {log}", flush=True)
    failed = run(client, args.minutes, log)
    print(f"movers-screener: done, {failed} read(s) failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
