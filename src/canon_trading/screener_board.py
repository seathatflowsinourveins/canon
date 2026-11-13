"""The latest complete Alpaca screener poll, published atomically and read without a client.

DISPLAY ONLY (the lead's term, 2026-11-13). Alpaca's movers and most-actives lists are the vendor's, and
this board keeps them for a person to look at: it never feeds the ranking, a roster, an entry, a re-entry
or a protector. `tests/test_screener_board.py` holds that none of those modules loads this one, directly or
through another module, and that only the poller that writes the board names it or its file; the reader
says so before any row. A limit outside this module: `session_window`'s mover window still takes Alpaca's
gainers through `movers_screener.movers_line` as `choose_mover`'s fallback (ADR 0083's line of
2026-11-11), which this board neither adds nor changes and which is the lead's to rule on.

Contracts: `movers_screener.movers_line` and `actives_line` supply Alpaca's rows unchanged;
CPython 3.14's `tempfile.NamedTemporaryFile` and `os.replace` publish a closed JSON file in
one folder (Doc/library/tempfile.rst:78-102; Doc/library/os.rst:2827-2833 at the pin).
The board retains the log's UTC `at` and the three original response keys. Both movers
lists share their response's `last_updated`. This module imports only the standard library.

The reader shows lists by `last_updated`, newest first, retaining Alpaca's row order within
each list. Age is measured from the poll's `at`; it says nothing about Alpaca's refresh rate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

BOARD_NAME = "screener-board.json"
STALE_SECS = 120
LIMITS = (
    "Most actives rank share volume and trade count, not dollars.",
    "Alpaca documents no refresh interval and no floor of price or volume for a row.",
)
DISPLAY_ONLY = (
    "Display only: these are Alpaca's lists, for a person to look at; they never feed the ranking, a "
    "roster, an entry, a re-entry or a protector."
)


def default_board_dir() -> Path:
    return Path.home() / ".claude" / "canon" / "watch"


def write_board(board_dir: Path, record: Mapping[str, object]) -> None:
    """Publish one successful poll; a write failure raises and leaves the last board intact."""
    board = {
        "source": "alpaca screener",
        "limits": LIMITS,
        "at": record["at"],
        "movers": record["movers"],
        "most_actives_volume": record["most_actives_volume"],
        "most_actives_trades": record["most_actives_trades"],
    }
    board_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=board_dir,
            prefix=f".{BOARD_NAME}.",
            suffix=".tmp",
            delete=False,
        ) as out:
            temporary = Path(out.name)
            json.dump(board, out)
            out.write("\n")
            out.flush()
            os.fsync(
                out.fileno()
            )  # whole on disk before the swap, so a crash leaves no empty board
        os.replace(temporary, board_dir / BOARD_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.screener_board", description=__doc__
    )
    parser.add_argument(
        "--board-dir",
        type=Path,
        default=default_board_dir(),
        help="board folder (default: ~/.claude/canon/watch/)",
    )
    parser.add_argument("--top", type=int, default=20, help="rows per list (default: 20)")
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    path = args.board_dir / BOARD_NAME
    try:
        board = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Missing screener board: {path}")
        return 2
    except (OSError, ValueError) as error:
        print(f"Malformed screener board: {path}: {type(error).__name__}: {error}")
        return 3
    try:
        # everything is read before anything is printed, so a bad board is named on the first line
        age = (datetime.now(UTC) - datetime.fromisoformat(board["at"])).total_seconds()
        heading = (board["source"], tuple(board["limits"]), board["at"])
        lists = [
            ("Alpaca's movers: gainers", board["movers"], "gainers"),
            ("Alpaca's movers: losers", board["movers"], "losers"),
            ("Alpaca's most actives by volume", board["most_actives_volume"], "most_actives"),
            ("Alpaca's most actives by trades", board["most_actives_trades"], "most_actives"),
        ]
        shown = [
            (title, answer["last_updated"], list(answer[rows][: args.top]))
            for title, answer, rows in sorted(
                lists,
                key=lambda item: datetime.fromisoformat(item[1]["last_updated"]),
                reverse=True,
            )
        ]
    except (KeyError, TypeError, ValueError) as error:
        print(f"Malformed screener board: {path}: {type(error).__name__}: {error}")
        return 3
    source, limits, at = heading
    stale = age > STALE_SECS
    state = "Stale" if stale else "Fresh"
    print(f"{state} screener board: age {age:.1f} seconds; polled at {at}")
    print(DISPLAY_ONLY)
    print(f"Source: {source}")
    for limit in limits:
        print(f"Limit: {limit}")
    for title, last_updated, rows in shown:
        print(f"\n{title} | last_updated: {last_updated}")
        for rank, row in enumerate(rows, 1):
            print(f"  {rank}. {json.dumps(row)}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
