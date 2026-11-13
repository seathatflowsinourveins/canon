"""One bounded paper node protecting EXTERNAL owner holdings (unit 10i).

Contract: ADRs 0146 (one carrying node per feed span), 0142 (registered row, C's named go),
0069 (account/stream locks), and OwnerProtector's unit-10 seam. No entry strategy or claim is
installed. No end-of-window liquidation or account-wide cancellation is called.

Read at NautilusTrader d53849b216e2, v2.0.0rc8:
* crates/live/src/node/mod.rs:36-49,1183-1336: data instruments arrive before execution connects
  and startup reconciliation; both providers and reconciliation must include every held symbol.
* crates/trading/src/strategy/config.rs:48-84: order_id_tag, external instrument claims and
  manage_stop; false avoids the automatic market exit. OwnerProtector.on_stop cancels its own IDs.
* docs/concepts/execution/reconciliation.md:305-378: unclaimed positions stay EXTERNAL and a
  reduce-only NETTING fill can close the sole matching EXTERNAL long without claiming it.
* crates/common/src/factories/order.rs:950-973: trailing_stop_limit takes a client_order_id, and
  crates/execution/src/order_emulator/emulator.rs:5094: the released LIMIT keeps it. Every sell
  OwnerProtector sends is named in the protect lineage (`custody.PROTECT_CLIENT_ORDER_ID`,
  `canon-protect-`), never one of our nodes' (the lead's ruling, 2026-11-13, 16:04:49).

owner_execution_config enables the adapter's reduce_only_sells and external reports; execution.py
checks the reduce-only side/type at :2542-2553 and writes SELL_TO_CLOSE at :2594-2595. The emulator
releases OwnerProtector's reduce-only, extended-hours trailing sell as a LIMIT.

C's go is procedural in ADR 0142, not an existing code token: --lead-go carries exactly
"protect <row>" from the lead's start instruction; it does not authenticate its author. The
caller must still run C from reviewed main. The base session has no holds/inbox paths, so both
are required inputs and unread/nonempty controls refuse. No default empty folder is invented.
A new owner fill after start on B is SIMULATION ONLY today; its paper showing comes later on A.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING

from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import StrategyId

from . import paper_node
from . import session_window as window
from .owner_protector import OwnerProtector
from .owner_protector import OwnerProtectorConfig
from .owner_protector import owner_execution_config
from .protect import ROWS

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from nautilus_trader.live import LiveNode


@dataclass(frozen=True)
class ProtectorRun:
    account: str
    row: str
    holds_dir: Path
    inbox: Path
    minutes: int = window.DEFAULT_MINUTES
    levels: dict[InstrumentId, Price] = field(default_factory=dict)
    feed: str | None = None
    lead_go: str | None = None

    def __post_init__(self) -> None:
        if self.account not in window.PAPER_ACCOUNT_LOCKS:
            raise ValueError("account must name tests, windows or protect")
        if self.row == "F2":
            raise ValueError("F2 is refused by name: a holding's distance is per symbol")
        if self.row not in ROWS:
            raise ValueError("row must explicitly name a registered holding row: OWN1 or OWN3")
        if (
            not isinstance(self.minutes, int)
            or isinstance(self.minutes, bool)
            or not 1 <= self.minutes <= 16 * 60
        ):
            raise ValueError("minutes must be between 1 and 960 (ADR 0146)")
        if self.feed is not None and self.feed not in ("sip", "boats"):
            raise ValueError("IEX is refused by name: protection requires sip or boats")
        if not self.holds_dir.is_absolute() or not self.inbox.is_absolute():
            raise ValueError("holds-dir and inbox must be absolute paths")
        # Keep the module's validation, including Price types and positive levels, at the seam.
        OwnerProtectorConfig(row=self.row, levels=self.levels, feed=self.feed or "sip")


def parse_levels(text: str) -> dict[InstrumentId, Price]:
    """A level is an optional per-symbol input, never a default for a later holding."""
    levels: dict[InstrumentId, Price] = {}
    if not text:
        return levels
    for item in text.split(","):
        symbol, separator, number = item.partition("=")
        if not separator or re.fullmatch(r"[A-Z][A-Z0-9./-]*", symbol, re.ASCII) is None:
            raise ValueError("levels must be SYMBOL=PRICE pairs separated by commas")
        instrument = InstrumentId.from_str(f"{symbol}.ALPACA")
        if instrument in levels:
            raise ValueError(f"{symbol} has more than one supplied level")
        try:
            price = Price.from_str(number)
        except (ValueError, OverflowError) as e:
            raise ValueError(f"{symbol} needs a positive finite level") from e
        if price.as_decimal() <= 0:
            raise ValueError(f"{symbol} needs a positive level")
        levels[instrument] = price
    return levels


def protector_node(
    config: ProtectorRun, instruments: Sequence[InstrumentId], *, feed: str
) -> tuple[LiveNode, OwnerProtector]:
    """Build, but do not connect: one strategy, no claims and no automatic market exit.

    The roster is the account snapshot's whole instrument set, not just the levels' symbols.
    No notional cap can deny a protective exit; native risk checks and throttles remain enabled.
    """
    missing = config.levels.keys() - set(instruments)
    if missing:
        names = ", ".join(sorted(str(instrument) for instrument in missing))
        raise ValueError(f"{names}: supplied level has no starting holding")
    settings = OwnerProtectorConfig(
        row=config.row,
        levels=config.levels,
        feed=feed,
        strategy_id=StrategyId("OwnerProtector-001"),
        order_id_tag="001",
        manage_stop=False,
    )
    node = paper_node.alpaca_paper_builder(
        "CANON-OWNER-PROTECTOR-PAPER",
        instruments,
        trader_id=paper_node.TRADER_ID,
        max_notional_per_order=None,
        execution_config=owner_execution_config,
    ).build()
    strategy = OwnerProtector(settings)
    try:
        node.add_strategy(strategy)
    except BaseException:
        node.dispose()
        raise
    return node, strategy


def _lead_go(config: ProtectorRun) -> None:
    if config.account == "protect" and config.lead_go != f"protect {config.row}":
        raise window.WindowRefused(
            f"lead's go must name protect and {config.row} in this start step: "
            f"--lead-go 'protect {config.row}'; nothing started"
        )


RECORDER_HOURS = (time(4, 0), time(20, 0))
"""The movers recorder streams on account A's pair on weekdays 04:00-20:00 New York (AGENTS.md,
Automation) and holds no stream lock, so A is refused by name in these hours by Alpaca's clock
(the lead's ruling, 2026-11-13, 16:04:49)."""


def _recorder_hours(config: ProtectorRun, now: datetime) -> None:
    local = now.astimezone(window.NEW_YORK)
    opens, closes = RECORDER_HOURS
    if config.account == "tests" and local.weekday() < 5 and opens <= local.time() < closes:
        raise window.WindowRefused(
            "account A is refused on weekdays 04:00-20:00 New York: the movers recorder streams "
            "on its pair; nothing started"
        )


CHECKOUT = Path(__file__).resolve().parents[2]
"""The checkout this module runs from: never the main checkout (the lead's rule, 16:04:49)."""


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False
    )


def _own_checkout(checkout: Path) -> None:
    """Refuse the main checkout, which the maintenance agent fast-forwards on main
    (home/.claude/canon/maintenance.sh:5-6), on any branch or none, and any checkout on the branch
    main: a protector runs only from a linked worktree of its own at a commit (the lead's rule 2a,
    2026-11-13, 16:04:49). A repository's main worktree is the one whose git directory is its
    common directory (git-rev-parse(1), --git-dir and --git-common-dir); the branch is read by
    `symbolic-ref`, so a tag named main is not taken for it. A checkout git cannot read is refused."""
    dirs = _git(checkout, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    found = dirs.stdout.split()
    if dirs.returncode != 0 or len(found) != 2:
        raise window.WindowRefused(f"{checkout}: its git HEAD cannot be read; nothing started")
    branch = _git(checkout, "symbolic-ref", "-q", "--short", "HEAD")
    if branch.returncode not in (0, 1):
        raise window.WindowRefused(f"{checkout}: its git HEAD cannot be read; nothing started")
    if found[0] == found[1] or branch.stdout.strip() == "main":
        raise window.WindowRefused(
            f"{checkout} is a main checkout or on main, which the maintenance agent moves: run "
            "from a linked worktree of its own at a commit (git worktree add --detach); nothing "
            "started"
        )


def _clean_on_main(checkout: Path) -> None:
    """On account C only: refuse a tree that is not clean at a commit on main (the lead's ruling
    (d)2, 2026-11-13, before proof 4). Clean is `git status --porcelain` empty, untracked files
    included (git-status(1)); on main is HEAD an ancestor of origin's main when the clone has one,
    else of the local main (git-merge-base(1), --is-ancestor: 0 yes, 1 no). A dirty tree on A or B
    is a named limit, not refused."""
    status = _git(checkout, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        raise window.WindowRefused(f"{checkout}: its git status cannot be read; nothing started")
    changed = status.stdout.splitlines()
    if changed:
        raise window.WindowRefused(
            f"{checkout} is not clean ({len(changed)} paths changed or untracked): on C the "
            "protector runs only from a clean tree at a commit on main; nothing started"
        )
    ref = "refs/remotes/origin/main"
    if _git(checkout, "rev-parse", "-q", "--verify", ref).returncode != 0:
        ref = "refs/heads/main"
    on_main = _git(checkout, "merge-base", "--is-ancestor", "HEAD", ref)
    if on_main.returncode == 1:
        raise window.WindowRefused(
            f"{checkout}: HEAD is not a commit on main ({ref}): on C the protector runs only "
            "from reviewed code; nothing started"
        )
    if on_main.returncode != 0:
        raise window.WindowRefused(f"{checkout}: main cannot be read ({ref}); nothing started")


def run_protector(
    config: ProtectorRun, *, trading: TradingClient | None = None, lock_dir: Path | None = None
) -> None:
    """Take the locks, read the controls/account once, then run until this feed span's bound.

    The only after-stop broker calls read positions and open orders. The strategy's own stop
    cancels only its sent IDs. All remaining holdings are named as unprotected by this node,
    even if the owner has separate protection; an outstanding cancel is reported as unresolved.
    """
    _lead_go(config)
    if config.account == "tests":
        # Before any lock or account read (the lead's ruling, 16:04:49): only Alpaca's clock.
        trading = trading if trading is not None else window.paper_trading()
        _recorder_hours(config, window.session_now(trading)[0].timestamp)
    with (
        window.paper_start(
            config.account,
            holds_dir=config.holds_dir,
            inbox=config.inbox,
            lock_dir=window.default_log_dir().parent if lock_dir is None else lock_dir,
        ),
        window.window_signals(),
    ):
        trading = trading if trading is not None else window.paper_trading()
        clock, calendar, read_at = window.session_now(trading)
        _recorder_hours(config, clock.timestamp)
        ends, part, feed = window.protection_span(
            clock, calendar, minutes=config.minutes, feed=config.feed
        )
        held = window.positions(trading)
        if not held:
            raise window.WindowRefused("no starting holding; no protector node started")
        if any(not qty.is_finite() or qty == 0 for qty in held.values()):
            raise window.WindowRefused("a starting holding has an invalid quantity")
        for instrument in config.levels:
            if held.get(instrument.symbol.value, 0) <= 0:
                raise window.WindowRefused(
                    f"{instrument}: supplied level has no starting long holding"
                )
        # Orders in other symbols must reconcile too, with EXTERNAL ownership and no cancel.
        orders = window.open_orders(trading)
        symbols = sorted(set(held) | {order.symbol for order in orders})
        instruments = tuple(InstrumentId.from_str(f"{symbol}.ALPACA") for symbol in symbols)
        if window.time.monotonic() - read_at >= (ends - clock.timestamp).total_seconds():
            raise window.WindowRefused("the feed span ended during the start reads")
        # Re-read together immediately before building: a hold arriving in a broker read stops us.
        window.read_start_controls(config.holds_dir, config.inbox)
        _lead_go(config)
        node, strategy = protector_node(config, instruments, feed=feed)
        print(
            f"paper:protector: {config.account}, {config.row}, {part}, {feed}; ends {ends.isoformat()}"
        )
        failure: BaseException | None = None
        errors: list[str] = []
        try:
            window._run_node(node, ends)
        except BaseException as e:  # noqa: BLE001 - re-raised after naming the holdings left at stop
            failure = e
        finally:
            window._SIGNALS.left_node()
            if not window._ABANDONED:
                try:
                    node.dispose()
                except Exception as e:  # noqa: BLE001 - report and keep the final holdings read
                    errors.append(f"dispose failed: {type(e).__name__}")
            try:
                remaining = window.positions(trading)
                last_seen = ""
            except Exception as e:  # noqa: BLE001 - last seen is explicitly not a current account read
                errors.append(f"end positions read failed: {type(e).__name__}")
                remaining, last_seen = held, "last seen at start; current quantity UNVERIFIED; "
            for symbol, qty in sorted(remaining.items()):
                print(
                    f"{symbol}: {qty} held, {last_seen}left unprotected by this node; "
                    "owner's orders untouched"
                )
            try:
                ours = {str(cid) for cid in strategy.sent_ids}
                working = [
                    order for order in window.open_orders(trading) if order.client_order_id in ours
                ]
                if working:
                    names = ", ".join(sorted({order.symbol for order in working}))
                    errors.append(f"our stop cancels remain unresolved in {names}")
            except Exception as e:  # noqa: BLE001 - a read failure is never a clean stop
                errors.append(f"end orders read failed: {type(e).__name__}")
            if errors:
                print(f"paper:protector: {'; '.join(errors)}; no cleanup sale", file=sys.stderr)
        if failure is not None:
            raise failure
        if errors:
            raise RuntimeError("; ".join(errors))
        if window._SIGNALS.first is not None:
            raise window.WindowSignalled(f"stopped by {window._SIGNALS.first}")


def parse_args(argv: Sequence[str] | None = None) -> ProtectorRun:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", choices=tuple(window.PAPER_ACCOUNT_LOCKS))
    parser.add_argument("--row", required=True, help="OWN1 or OWN3; F2 is refused, no default")
    parser.add_argument(
        "--levels", default="", help="optional SYMBOL=PRICE,... for starting holdings"
    )
    parser.add_argument("--minutes", type=int, default=window.DEFAULT_MINUTES)
    parser.add_argument("--feed", help="sip or boats; otherwise select this span, IEX refused")
    parser.add_argument("--holds-dir", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--lead-go", help="C only: the lead's start instruction, 'protect <row>'")
    args = parser.parse_args(argv)
    try:
        args.levels = parse_levels(args.levels)
        return ProtectorRun(**vars(args))
    except (ValueError, TypeError) as e:
        parser.error(str(e))


def main(argv: Sequence[str] | None = None, *, checkout: Path | None = None) -> int:
    """`checkout` is the task's own (`CHECKOUT`); in-process callers that pass none skip it."""
    config = parse_args(argv)
    code = 0
    try:
        if checkout is not None:
            _own_checkout(checkout)
            if config.account == "protect":
                _clean_on_main(checkout)
        run_protector(config)
    except window.WindowRefused as e:
        print(f"paper:protector: refused: {e}", file=sys.stderr)
        code = 3
    except window.WindowSignalled as e:
        print(f"paper:protector: {e}", file=sys.stderr)
        named = re.search(r"stopped by (SIG[A-Z]+)", str(e))
        code = 128 + signal.Signals[named.group(1)].value if named else 1
    except Exception as e:  # noqa: BLE001 - CLI failure names the type without private broker payloads
        # Broker exception bodies may carry private account data; name the failure class only.
        print(f"paper:protector: failed: {type(e).__name__}; no cleanup sale", file=sys.stderr)
        code = 1
    finally:
        if window._ABANDONED:
            # Same bounded exit as session_window.main; paper_start keeps its locks to exit.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code or 1)
    return code


if __name__ == "__main__":
    raise SystemExit(main(checkout=CHECKOUT))
