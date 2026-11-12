"""Read Alpaca's trade-condition codes for each tape, GET only, and write them beside the catalog.

`mise run paper:history <account> conditions` runs this module with one paper account's pair in
data-only mode (ADR 0070, ADR 0093). It sends one GET per tape to Alpaca's documented
`GET /v2/stocks/meta/conditions/{ticktype}` with `ticktype` `trade` and `tape` "The one character name of
the tape" (https://docs.alpaca.markets/us/reference/stockmetaconditions-1.md, read 2026-11-12), which
answers a map from each one-character condition code to its name. alpaca-py at `c42add7c0a70` has no call
of its own for stock conditions, so this sends the path through the client's own `RESTClient.get`
(`alpaca/common/rest.py:215-228`), as alpaca-py's `get_option_exchange_codes` does for options
(`alpaca/data/historical/option.py:99-113`). The client is `history_load.history_client()`: GET only,
with the HTTP timeout and the first 429 ending the run. The GETs are paced at `paper:history`'s row of
ADR 0093. Nothing of the account is read; the answer is market metadata. The codes are what the
adapter's trade-condition rule is checked against; the rule itself is not decided here.

With `--symbols`, `--start` and `--end` it also keeps a tape: Alpaca's historical trades for each symbol over
that window (`GET /v2/stocks/trades`, https://docs.alpaca.markets/us/reference/stocktrades-1.md), each
answer page written as Alpaca sent it, so each print keeps its conditions `c` and tape `z`, which the
catalog's trade ticks drop. The pages follow `next_page_token` up to `RAW_MAX_PAGES`, on `sip`, paced as
above; a cap reached with pages left stops the run by name rather than keep a partial tape.
"""

import argparse
import json
from collections.abc import Callable
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from canon_alpaca.trade_conditions import CONDITIONS_PATH
from canon_alpaca.trade_conditions import TAPES
from canon_alpaca.trade_conditions import read_condition_map

from canon_trading.history_load import LEDGER
from canon_trading.history_load import PACE
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import history_client

__all__ = ["LEDGER", "PACE", "PATH", "TAPES", "main", "pacer", "read_trade_conditions"]

PATH = CONDITIONS_PATH
"""Alpaca's path for trade conditions, under the data client's `v2` (the adapter's, shared)."""

RAW_PATH = "/stocks/trades"
"""Alpaca's path for historical trades of several symbols, under the data client's `v2`."""

RAW_MAX_PAGES = 50
"""Pages of one symbol's tape before the read stops: at 10,000 prints a page, half a million prints, far
more than a window of minutes holds (UNCALIBRATED)."""

RAW_PAGE_ROWS = 10_000
"""Alpaca's most prints in one page (alpaca-py's page size for trades, `stock.py`)."""


def pacer(sleep: Callable[[float], None]) -> Callable[[], None]:
    """One pace for a whole run: before every GET but the run's first, a gap of ADR 0093's
    `paper:history` row, whether the GET is a tape's map, a page or the next symbol's first page."""
    gap = 60 / PACE.data_per_minute
    asked = False

    def pace() -> None:
        nonlocal asked
        if asked:
            sleep(gap)
        asked = True

    return pace


def read_trade_conditions(client: Any, *, pace: Callable[[], None]) -> dict[str, dict[str, str]]:
    """One GET per tape, paced; each answer checked (the adapter's `read_condition_map`) before the next
    is asked."""
    tape = "A"

    def get(path: str, data: dict[str, str]) -> object:
        nonlocal tape
        tape = data["tape"]
        try:
            return client.get(path, data)
        except HistoryStopped:
            raise
        except Exception as e:
            raise HistoryStopped(
                f"tape {tape}: the GET failed ({type(e).__name__}): {e}; nothing more was asked"
            ) from e

    try:
        return read_condition_map(get, pace)
    except ValueError as e:
        raise HistoryStopped(str(e)) from e


def read_raw_trades(
    client: Any, symbol: str, start: str, end: str, *, pace: Callable[[], None]
) -> list[dict[str, Any]]:
    """Every page of one symbol's prints over [start, end), as Alpaca answered them, each GET paced."""
    pages: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        if len(pages) == RAW_MAX_PAGES:
            raise HistoryStopped(
                f"{symbol}: {RAW_MAX_PAGES} pages read and Alpaca has more; shorten the window"
            )
        pace()
        query: dict[str, Any] = {
            "symbols": symbol,
            "start": start,
            "end": end,
            "feed": "sip",
            "limit": RAW_PAGE_ROWS,
        }
        if token:
            query["page_token"] = token
        try:
            page = client.get(RAW_PATH, query)
        except HistoryStopped:
            raise
        except Exception as e:
            raise HistoryStopped(
                f"{symbol}: page {len(pages) + 1} failed ({type(e).__name__}): {e}"
            ) from e
        if not isinstance(page, dict) or "trades" not in page:
            raise HistoryStopped(f"{symbol}: page {len(pages) + 1} has no trades field")
        pages.append(page)
        token = page.get("next_page_token")
        if not token:
            return pages


def _out(text: str) -> Path:
    path = Path(text).resolve()
    if path.is_relative_to(LEDGER.resolve()):
        raise argparse.ArgumentTypeError(
            f"{text} is under ledger/; the codes go beside the catalog"
        )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    import time

    parser = argparse.ArgumentParser(
        prog="paper:history conditions", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--out", type=_out, required=True, help="the JSON file to write")
    parser.add_argument("--symbols", help="keep these symbols' prints too, comma-separated")
    parser.add_argument("--start", help="with --symbols: the window's start, RFC 3339")
    parser.add_argument("--end", help="with --symbols: the window's end, RFC 3339")
    args = parser.parse_args(argv)
    symbols = [s for s in (args.symbols or "").split(",") if s]
    if bool(symbols) != bool(args.start) or bool(symbols) != bool(args.end):
        parser.error("--symbols, --start and --end go together")
    client = history_client()
    pace = pacer(time.sleep)
    try:
        codes = read_trade_conditions(client, pace=pace)
        tapes = {s: read_raw_trades(client, s, args.start, args.end, pace=pace) for s in symbols}
    except HistoryStopped as e:
        print(f"paper:history conditions: stopped: {e}")
        return 1
    read_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_text(
        json.dumps({"read_at": read_at, "path": f"/v2{PATH}", "tapes": codes}, indent=2) + "\n"
    )
    partial.replace(out)
    for symbol, pages in tapes.items():
        raw = out.with_name(f"trades-{symbol}-{args.start}.json")
        partial = raw.with_name(raw.name + ".partial")
        record = {"symbol": symbol, "start": args.start, "end": args.end, "read_at": read_at}
        partial.write_text(json.dumps({**record, "pages": pages}) + "\n")
        partial.replace(raw)
        prints = sum(len((p.get("trades") or {}).get(symbol) or []) for p in pages)
        print(f"{symbol}: {prints} prints in {len(pages)} pages: wrote {raw}")
    for tape, mapping in codes.items():
        print(f"tape {tape}: {len(mapping)} codes: {' '.join(repr(c) for c in sorted(mapping))}")
    print(f"paper:history conditions: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
