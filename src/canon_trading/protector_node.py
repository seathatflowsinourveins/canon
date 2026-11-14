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
* crates/live/src/python/node.rs:1326-1370: add_strategy registers the constructed strategy and
  its config. The level-book keywords implement OwnerProtectorConfig's composed U-e contract;
  start_level.book_path names its account file and start_level.SCHEMA names its written format.

owner_execution_config enables the adapter's reduce_only_sells and external reports; execution.py
checks the reduce-only side/type at :2542-2553 and writes SELL_TO_CLOSE at :2594-2595. The emulator
releases OwnerProtector's reduce-only, extended-hours trailing sell as a LIMIT.

C's go is procedural in ADR 0142, not an existing code token: --lead-go carries exactly
"protect <row>" from the lead's start instruction; it does not authenticate its author. The
caller must still run C from reviewed main. The base session has no holds/inbox paths, so both
are required inputs and unread/nonempty controls refuse. No default empty folder is invented.
A new owner fill after start on B is SIMULATION ONLY today; its paper showing comes later on A.

Review F1 of #443's max review: the roster is the start's instruments, handed to the protector, which
reads the account at its first step and every owner_protector.ROSTER_POLL_SECS, names a long outside
the roster by an ALERT and, for a whole-share one, ends the run by name (owner_protector.ROSTER_ENDED,
through Strategy.shutdown_system; module header there). The run then returns as at its planned end,
saying `paper:protector: ended: <reason>`, and exits 0, since the runner launches its successor once a
run exits, on any exit code (scripts/automation, do_run_protector), and that successor reads the
account at its start. The runner records the end as a boundary; naming it there is the runner's.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

from canon_alpaca.providers import TimeoutAdapter
from canon_alpaca.providers import session_of
from canon_alpaca.providers import timeout_adapter
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import StrategyId

from . import paper_node
from . import session_window as window
from .owner_protector import OwnerProtector
from .owner_protector import OwnerProtectorConfig
from .owner_protector import owner_execution_config
from .protect import ROWS
from .start_level import SCHEMA
from .start_level import book_path

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from nautilus_trader.live import LiveNode


LOCK_WAIT_MAX_SECS = 300
"""The longest a successor waits for its account's locks (U-g, the lead's ruling (d)4, 2026-11-13):
the protector runner launches it about 60 s before its predecessor's planned end and bounds the wait
at that end plus a stop budget of about 30 s. Five minutes keeps a go read at launch fresh."""
LOCK_POLL_SECS = 0.5
"""How often a waiting successor tries the locks again (UNCALIBRATED; flock(2) has no timed wait)."""


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
    lock_wait: int = 0

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
        if (
            not isinstance(self.lock_wait, int)
            or isinstance(self.lock_wait, bool)
            or not 0 <= self.lock_wait <= LOCK_WAIT_MAX_SECS
        ):
            raise ValueError(f"lock-wait must be between 0 and {LOCK_WAIT_MAX_SECS} seconds")
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
    config: ProtectorRun,
    instruments: Sequence[InstrumentId],
    *,
    feed: str,
    book: dict[str, Any] | None = None,
    session_part: str = "regular",
    trading_reader: Callable[[], Any] | None = None,
) -> tuple[LiveNode, OwnerProtector]:
    """Build, but do not connect: one strategy, no claims and no automatic market exit.

    The roster is the account snapshot's whole instrument set, not just the levels' symbols.
    No notional cap can deny a protective exit; native risk checks and throttles remain enabled.

    A book's account_label enables persistence without the runner's environment variable. Resolve
    its default path and give this start its own name before handing the config to the strategy.
    An explicit reader is kept; an injected factory can supply one for a labelled build. The task
    supplies its GET-only reader through _book_settings under the account's locks. Bare builds
    construct no reader and carry no level; building alone makes no account or level-book read.
    """
    missing = config.levels.keys() - set(instruments)
    if missing:
        names = ", ".join(sorted(str(instrument) for instrument in missing))
        raise ValueError(f"{names}: supplied level has no starting holding")
    book_settings: dict[str, Any] = dict(book or {})
    label = book_settings.get("account_label")
    if label is not None:
        if book_settings.get("level_book_path") is None:
            book_settings["level_book_path"] = book_path(label)
        if book_settings.get("run", "") == "":
            book_settings["run"] = f"{label}:{uuid4().hex}"
        if "trading" not in book_settings and trading_reader is not None:
            book_settings["trading"] = trading_reader()
    settings = OwnerProtectorConfig(
        row=config.row,
        levels=config.levels,
        feed=feed,
        strategy_id=StrategyId("OwnerProtector-001"),
        order_id_tag="001",
        manage_stop=False,
        # The span's part (window.protection_span), so U-e's overnight eligibility check runs on an
        # overnight run (OwnerProtectorConfig.session_part; composition, L6 2026-11-13).
        session_part=session_part,
        # Review F1: the instruments this node loads; a long outside them is named, the run ended.
        roster=tuple(instruments),
        **book_settings,
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


LEVEL_BOOK_ENV = "CANON_PROTECTOR_LEVEL_BOOK"
"""The level book's path, which the protector runner (scripts/automation run-protector) sets to
start_level.book_path()'s file. Read only once both locks are held (the lead's ruling, 2026-11-13,
17:10), so a predecessor's stop write, made while it still held the lock, is always seen."""


def _book_levels(path: Path, account: str) -> dict[InstrumentId, Price]:
    """Check the runner's supplied book and count this account's priced rows under the locks.

    Accept both formats read by start_level.LevelBook: legacy schema 1 and the composed writer's
    SCHEMA. A missing or unreadable runner-supplied book refuses the start; a malformed level is
    skipped by name. This summary selects no carried holding: OwnerProtector reads the whole book
    through LevelBook.read and decides identity through its venue reader.
    """
    try:
        data = json.loads(path.read_text())
        entries = data["entries"] if isinstance(data, dict) else None
        if (
            type(data.get("schema")) is not int
            or data["schema"] not in (1, SCHEMA)
            or not isinstance(entries, list)
        ):
            raise ValueError(f"expected schema 1 or {SCHEMA} and an entries array")
    except (OSError, ValueError, KeyError, AttributeError) as e:
        raise window.WindowRefused(
            f"level book unreadable at {path}: {type(e).__name__}; nothing started"
        ) from e
    latest: dict[InstrumentId, tuple[int, Price]] = {}
    for index, row in enumerate(entries):
        if not isinstance(row, dict) or row.get("account_label") != account:
            continue
        if "last_level" in row and row["last_level"] is None:
            continue  # A waiting or frozen holding the writer saved with no level carries none.
        try:
            instrument = InstrumentId.from_str(str(row["instrument_id"]))
            level = Price.from_str(str(row["last_level"]))
            written = row["written_ns"]
            if type(written) is not int or level.as_decimal() <= 0:
                raise ValueError("needs a positive level and integer written_ns")
        except (KeyError, ValueError, TypeError, OverflowError) as e:
            print(
                f"paper:protector: level book entry {index} skipped: {type(e).__name__}", flush=True
            )
            continue
        if instrument not in latest or written > latest[instrument][0]:
            latest[instrument] = (written, level)
    return {instrument: level for instrument, (_written, level) in latest.items()}


def _protector_takes_book() -> bool:
    """Whether this OwnerProtector build reads the level book itself (U-e's account_label and
    level_book_path settings, ue-build 2026-11-13); pnode's copy (sha256 b8f83b71...) does not."""
    parameters = inspect.signature(OwnerProtectorConfig.__init__).parameters
    return "account_label" in parameters and "level_book_path" in parameters


def _protector_takes_reader() -> bool:
    """Whether this OwnerProtector build takes U-e's `trading` slot, the GET-only reader through which
    it reads the venue's fills to decide carry (U-e's fix round, the deputy's message 17:26)."""
    return "trading" in inspect.signature(OwnerProtectorConfig.__init__).parameters


def _trading_reader() -> TradingClient:
    """The account's own paper client, a second one apart from the node's start reads, with every
    adapter on its session replaced by the adapter package's `TimeoutAdapter` at the same timeout and
    `get_only` set: every method but GET is refused with `MethodRefused` before anything is sent (the
    contract `require_get_only` checks in data-only mode, ADR 0070). The node's own execution client is
    untouched, since data-only mode is not set for the process."""
    reader = window.paper_trading()
    session = session_of(reader)
    mounted = timeout_adapter(reader)
    if session is None or mounted is None:
        raise window.WindowRefused(
            "the trading reader has no session with the adapter's timeout, so it cannot be made "
            "GET only; nothing started"
        )
    for old in {id(a): a for a in session.adapters.values()}.values():
        old.close()
    adapter = TimeoutAdapter(mounted.timeout, get_only=True)
    for prefix in list(session.adapters) or ["https://", "http://"]:
        session.mount(prefix, adapter)
    return reader


def _book_settings(config: ProtectorRun) -> dict[str, Any]:
    """The composition with U-e in one call: with the runner's book path set, check the book reads
    (`_book_levels`, refusing by name) and hand the protector its account label and the book's path.
    Every level of this account goes to the protector, held at the start or not; the protector binds
    a level only to its own holding and keeps the entry when that holding never appears (the lead's
    P11 ruling, 16:55, narrowed at 17:08). This node filters none and drops none. Where the protector
    takes U-e's `trading` slot it is handed the account's own GET-only reader (`_trading_reader`), built
    here, under the locks; where it takes none, that is named."""
    path = os.environ.get(LEVEL_BOOK_ENV)
    if not path:
        # A start by hand, with no book: the reader is still handed, since the overnight
        # eligibility check reads through it whether or not a level persists (Codex's P1-a on
        # 62941f39, 2026-11-13: with none, every overnight long was refused as unreadable).
        return {"trading": _trading_reader()} if _protector_takes_reader() else {}
    levels = _book_levels(Path(path), config.account)
    if not _protector_takes_book():
        raise window.WindowRefused(
            "the level book is set, but this protector build reads no level book (U-e is not "
            "composed); nothing started"
        )
    print(
        f"paper:protector: level book {path}: {len(levels)} level(s) of {config.account} handed to "
        "the protector",
        flush=True,
    )
    settings: dict[str, Any] = {"account_label": config.account, "level_book_path": path}
    if _protector_takes_reader():
        settings["trading"] = _trading_reader()
    else:
        print(
            "paper:protector: this protector build takes no trading reader, so it reads no fills "
            "and carries no level by the venue's fills",
            flush=True,
        )
    return settings


@contextlib.contextmanager
def _locked_start(config: ProtectorRun, lock_dir: Path) -> Generator[None]:
    """`window.paper_start`, retried while a lock is held, for at most `config.lock_wait` seconds.

    flock(2) locks belong to an open file description, so the wait cannot pre-take the lock and
    then enter paper_start (a second open conflicts with the first in this process). It retries
    the whole entry instead, only when the refusal's cause is flock's EWOULDBLOCK; paper_start's
    `finally` closes a lock taken part way. Nothing is read, placed or subscribed before both
    locks are held; a hold or unread inbox found after them is never retried (the owner's word).
    """
    deadline = window.time.monotonic() + config.lock_wait
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(
                    window.paper_start(
                        config.account,
                        holds_dir=config.holds_dir,
                        inbox=config.inbox,
                        lock_dir=lock_dir,
                    )
                )
                break
            except window.WindowRefused as e:
                if not isinstance(e.__cause__, BlockingIOError) or config.lock_wait == 0:
                    raise
                left = deadline - window.time.monotonic()
                if left <= 0:
                    raise window.WindowRefused(
                        f"{e}".removesuffix("; nothing started")
                        + f" after waiting {config.lock_wait} s; nothing started"
                    ) from e
                window.time.sleep(min(LOCK_POLL_SECS, left))
        yield


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
        _locked_start(config, window.default_log_dir().parent if lock_dir is None else lock_dir),
        window.window_signals(),
    ):
        trading = trading if trading is not None else window.paper_trading()
        clock, calendar, read_at = window.session_now(trading)
        _recorder_hours(config, clock.timestamp)
        ends, part, feed = window.protection_span(
            clock, calendar, minutes=config.minutes, feed=config.feed
        )
        # Each position read by itself through custody's one rule (`window.position_reads`): a short, by
        # its label or by its sign, is below zero and never read as a long, and one that cannot be read
        # is named with an ALERT and skipped, its symbol and any level for it left out of the node, so
        # every other long is still covered (the lead, 2026-11-13 21:27 and 21:35).
        held, unread = window.position_reads(trading)
        # A zero quantity (a short read as -0 among them) is no holding this node can protect: it is
        # named and skipped like an unreadable one, never a refusal of every other long.
        for symbol, qty in sorted(held.items()):
            if qty == 0:
                unread[symbol] = f"position in {symbol} is listed with quantity {qty}, no holding"
                del held[symbol]
        for symbol, why in sorted(unread.items()):
            print(
                f"ALERT {symbol}: {why}, so this node does not protect it; skipped, nothing sent for it",
                flush=True,
            )
        if unread and any(instrument.symbol.value in unread for instrument in config.levels):
            config = replace(
                config,
                levels={i: p for i, p in config.levels.items() if i.symbol.value not in unread},
            )
        if not held:
            raise window.WindowRefused("no starting holding; no protector node started")
        for instrument in config.levels:
            if held.get(instrument.symbol.value, 0) <= 0:
                raise window.WindowRefused(
                    f"{instrument}: supplied level has no starting long holding"
                )
        # Orders in other symbols must reconcile too, with EXTERNAL ownership and no cancel.
        orders = window.open_orders(trading)
        symbols = sorted((set(held) | {order.symbol for order in orders}) - set(unread))
        instruments = tuple(InstrumentId.from_str(f"{symbol}.ALPACA") for symbol in symbols)
        if window.time.monotonic() - read_at >= (ends - clock.timestamp).total_seconds():
            raise window.WindowRefused("the feed span ended during the start reads")
        # Re-read together immediately before building: a hold arriving in a broker read stops us.
        window.read_start_controls(config.holds_dir, config.inbox)
        _lead_go(config)
        # The level book, under both locks only (the lead's ruling, 2026-11-13, 17:10).
        book = _book_settings(config)
        node, strategy = protector_node(
            config, instruments, feed=feed, book=book, session_part=part
        )
        # The runner (scripts/automation run-protector) reads the epoch to launch the successor.
        print(
            f"paper:protector: {config.account}, {config.row}, {part}, {feed}; "
            f"ends {ends.isoformat()} (epoch {int(ends.timestamp())})",
            flush=True,
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
                remaining, unread_now = window.position_reads(trading)
                last_seen = ""
            except Exception as e:  # noqa: BLE001 - last seen is explicitly not a current account read
                errors.append(f"end positions read failed: {type(e).__name__}")
                # What the start could not read is named again, as the start read it.
                remaining, unread_now, last_seen = (
                    held,
                    unread,
                    "last seen at start; current quantity UNVERIFIED; ",
                )
            for symbol, qty in sorted(remaining.items()):
                print(
                    f"{symbol}: {qty} held, {last_seen}left unprotected by this node; "
                    "owner's orders untouched"
                )
            for symbol, why in sorted(unread_now.items()):
                known = (
                    f"{held[symbol]} held, last seen at start; current quantity UNVERIFIED; "
                    if symbol in held
                    else ""
                )
                print(
                    f"ALERT {symbol}: {why}; {known}left unprotected by this node; "
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
            ended = getattr(strategy, "ended_for", None)
            if ended:
                print(f"paper:protector: ended: {ended}; the next start reads the account fresh")
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
    parser.add_argument(
        "--lock-wait",
        type=int,
        default=0,
        help=f"0-{LOCK_WAIT_MAX_SECS}: wait this long for the account's locks, else refuse",
    )
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
