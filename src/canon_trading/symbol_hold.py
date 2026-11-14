"""The owner's word for one symbol: a hold the protector reads at its start and at every step.

Contract: the lead's gate unit for the protect account (2026-11-13 23:50, "hold one symbol by the owner's
word"). The instance folder's `holds/` and `inbox/` stop the whole node: any entry in either refuses
a start (session_window.read_start_controls; the runner's pr_controls in scripts/automation). A hold
for one symbol therefore lives in a folder of its own beside them, `symbol-holds/`, so holding one
symbol never stops the node that protects the others. One file, named for the symbol, is one hold.
What the file holds is not read; its name is the word.

`mise run paper:hold-symbol <account> <SYMBOL> on|off` only writes or removes that file. It reads
no credential, no account and no order, and sends nothing. On the protect account the word is the owner's
alone: no lane, coordinator or automation writes or removes a hold there. The task's own text says
so, and nothing in this module can tell who runs it.

Reading (Python's pathlib, Path.iterdir and Path.is_file): an entry whose name is a symbol as
protector_node.parse_levels accepts it, with no `/`, and which is a regular file, is a hold. Any
other entry (a dotfile, a folder, a lowercase or malformed name) is a stray. It holds nothing and
is named at every read, never ignored in silence. A folder that cannot be read is an error to the
caller. The node refuses to start on one, and a running protector keeps the last holds it read
and names the failure at every step. Writing is a plain exclusive create, never a temporary file
and a rename, so the folder never shows a name that is not a hold.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from pathlib import Path

FOLDER = "symbol-holds"
"""The instance folder's per-symbol holds, beside holds/ and inbox/ (scripts/automation)."""
HELD = "held by the owner's word"
"""The one phrase every line, record and monitor finding uses for a symbol on hold."""
ACCOUNTS = ("tests", "windows", "protect")
INSTANCE = "instance"
"""The file install-protector writes in each instance folder (scripts/automation); the task writes a
hold only beside one, so a hold is never made where no runner reads it (review F3)."""
MONITOR_ACCOUNTS = {"A": "tests", "B": "windows", "C": "protect"}
"""The exit monitor's account letters and the protector instance each reads (C only behind the lead's
word, exit_monitor.LEAD_GO; ADR 0158)."""
SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]*", re.ASCII)
"""protector_node.parse_levels' symbol rule without `/`, which cannot name a file."""


def check_symbol(symbol: str) -> str:
    if SYMBOL.fullmatch(symbol) is None:
        raise ValueError(
            f"{symbol!r} is not a symbol this hold can name: capital letters, digits, '.' or '-', "
            "starting with a letter; a '/' cannot name a file"
        )
    return symbol


def instance_holds(account: str, *, home: Path | None = None) -> Path:
    """`<CLAUDE_CONFIG_DIR or ~/.claude>/canon/protector/<account>/symbol-holds`, the folder the
    runner hands its node (scripts/automation: CANON_HOME is $CLAUDE_HOME/canon, CLAUDE_HOME is
    ${CLAUDE_CONFIG_DIR:-$HOME/.claude}; the instance is $CANON_HOME/protector/<account>)."""
    if account not in ACCOUNTS:
        raise ValueError(f"account must name one of {', '.join(ACCOUNTS)}")
    config = home or Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    return config / "canon" / "protector" / account / FOLDER


def read_holds(folder: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    """The held symbols and the strays, sorted. An unread folder raises OSError or ValueError."""
    if not folder.is_absolute():
        raise ValueError("an absolute symbol-holds folder is required")
    held: set[str] = set()
    strays: list[str] = []
    for entry in folder.iterdir():
        if SYMBOL.fullmatch(entry.name) is not None and entry.is_file():
            held.add(entry.name)
        else:
            strays.append(entry.name)
    return frozenset(held), tuple(sorted(strays))


def read_holds_if_made(folder: Path) -> tuple[frozenset[str], tuple[str, ...], bool]:
    """As read_holds, but a folder never made is no hold (False): the task makes it with the first
    hold, so its absence says none was written there. Any other failure still raises."""
    try:
        held, strays = read_holds(folder)
    except FileNotFoundError:
        return frozenset(), (), False
    return held, strays, True


def strays_named(strays: Sequence[str]) -> str:
    return (
        f"symbol-holds entries {', '.join(repr(s) for s in strays)} are not symbol files and "
        "hold nothing"
    )


def write_hold(folder: Path, symbol: str) -> bool:
    """Create the hold; False when it was already there. The folder is created if absent."""
    check_symbol(symbol)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder / symbol).open("x") as handle:
            handle.write(f"{HELD}, written {datetime.now(UTC).isoformat()}\n")
    except FileExistsError:
        return False
    return True


def remove_hold(folder: Path, symbol: str) -> bool:
    """Remove the hold; False when there was none."""
    check_symbol(symbol)
    try:
        (folder / symbol).unlink()
    except FileNotFoundError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write (on) or remove (off) one symbol's hold for a protector instance. "
        "On protect (C) this is the owner's word alone: no lane runs it there."
    )
    parser.add_argument("account", choices=ACCOUNTS)
    parser.add_argument("symbol")
    parser.add_argument("action", choices=("on", "off"))
    parser.add_argument("--folder", type=Path, help="tests only: the symbol-holds folder")
    args = parser.parse_args(argv)
    try:
        check_symbol(args.symbol)
    except ValueError as e:
        parser.error(str(e))
    if args.folder is not None and not args.folder.is_absolute():
        parser.error("--folder must be an absolute path")
    folder = args.folder or instance_holds(args.account)
    if args.folder is None and not (folder.parent / INSTANCE).is_file():
        print(
            f"paper:hold-symbol: no protector instance at {folder.parent}: nothing written. A runner's "
            "instance is made by install-protector under the CLAUDE_CONFIG_DIR it ran with; run this "
            "task under the same one",
            file=sys.stderr,
        )
        return 2
    try:
        if args.action == "on":
            done = write_hold(folder, args.symbol)
            said = (
                f"{args.symbol}: {HELD} on {args.account}; the protector cancels only its own "
                "sell of it within one step, sells nothing, and keeps protecting every other "
                "holding"
            )
            print(said if done else f"{args.symbol}: already {HELD} on {args.account}; unchanged")
        else:
            done = remove_hold(folder, args.symbol)
            print(
                f"{args.symbol}: hold removed on {args.account}; the protector re-arms it at its "
                "level at its next step, never lowered"
                if done
                else f"{args.symbol}: no hold on {args.account}; nothing removed"
            )
        held, strays, _made = read_holds_if_made(folder)
    except (OSError, ValueError) as e:
        print(
            f"paper:hold-symbol: {folder} unread or unwritten: {type(e).__name__}", file=sys.stderr
        )
        return 1
    print(f"held on {args.account}: {', '.join(sorted(held)) or 'none'}")
    if strays:
        print(f"ALERT: {strays_named(strays)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
