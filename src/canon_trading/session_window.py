"""One bounded window of a named strategy on Alpaca's paper environment: start flat, trade, flatten, check, report.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveNode.add_builtin_strategy`, `Cache.quote_count`
and `trade_count`, `LiveExecutionEngineConfig` and `LoggerConfig` with a `FileWriterConfig`
(`live/__init__.pyi`, `common/__init__.pyi`); alpaca-py 0.45.0's `TradingClient` (paper)
`get_clock`, `get_calendar`, `get_orders`, `get_open_position`, `get_order_by_client_id`,
`cancel_order_by_id` and `submit_order` with a `MarketOrderRequest`. Read 2026-11-10 (ADR 0044; the account
check's sources, ADR 0060):

* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md, "Continuous reconciliation":
  `open_check_interval_secs` ("None or 0.0 disables. Recommended: 5-10s") and `position_check_interval_secs`
  ("On discrepancy, queries for missing fills. None disables. Recommended: 30-60s") are off by default.
* https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md, "Runtime checks": the open
  check reconciles cached orders with the venue's; the position check matches the net position with the
  venue's position reports and aligns it.
* https://nautilustrader.io/docs/md/latest/concepts/logging.md: `FileWriterConfig.directory` and `file_name`.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading": pre-market 4:00am to
  9:30am and after-hours 4:00pm to 8:00pm ET, Monday to Friday; overnight 8:00pm to 4:00am; "Orders not
  eligible for extended hours submitted after 4:00pm ET will be queued up for release the next trading day".
* `crates/live/src/node/mod.rs` and `python/node.rs` at the pin (`d53849b216e2`): the node bounds each
  stage of its own stop (`delay_post_stop`, `timeout_disconnection`, `timeout_shutdown`), logs the one that
  ran out and goes on; cancelling the task awaiting `run_async` "requests the same graceful shutdown, waits
  for it to finish", so neither a cancel nor `asyncio.run`'s exit can end a stop that hangs.
* alpaca-py 0.45.0, `common/rest.py` (`RESTClient._request`, `_one_request`) and `common/constants.py`: a
  request answered 429 or 504 is sent again, `DEFAULT_RETRY_ATTEMPTS` (3) times, `DEFAULT_RETRY_WAIT_SECONDS`
  (3) apart; `TradingClient` does not pass `retry_attempts` on, and the constructor ignores anything under 1.
* Python's `asyncio`: `loop.set_default_executor` takes a `ThreadPoolExecutor`, which `asyncio.to_thread`
  then runs its calls in; `concurrent.futures`: `Executor.shutdown(wait=False, cancel_futures=True)`
  cancels what has not started and refuses new work, and `wait(..., timeout=...)`.
* https://docs.alpaca.markets/us/docs/working-with-orders.md, "Using Client Order IDs": a market order
  submitted with a `client_order_id` and read back with `get_order_by_client_id`; its FAQ, on a submit
  that timed out: "The order may have been sent to the market for execution. You should not attempt to
  resend the order or mark the timed-out order as canceled until confirmed by Alpaca Support".
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md: "Once an order is placed, it can be queried
  using either the client-provided order ID or the system-assigned unique ID"; "Order Lifecycle": the four
  statuses after which "no further updates will occur for the order".
* https://docs.alpaca.markets/us/reference/postorder.md: 403 "Buying power or shares is not sufficient",
  422 "Input parameters are not recognized". Not documented there or in the pages above: what Alpaca
  answers a lookup of an ID it does not hold (a 404, as the adapter's lookup reads it; UNVERIFIED), and
  how soon after a submit an order shows in a lookup or a listing.

What one window is (one node, one process): one strategy of `WINDOW_STRATEGIES`, `SessionMaCross` unless
`--strategy` names upstream's built-in `EmaCross`, which runs in regular windows only (ADR 0068, step 3;
`crates/trading/src/examples/strategies/ema_cross/` at the pin); the configured session and feed, checked against Alpaca's
clock and calendar before anything starts; the paper account holding no open order and no position in the
symbol; at least `GAP_SECS` since the last window ended; then the node runs for `minutes` (default 30, at
most 60), clipped to the session's end and to `LONGEST_WINDOW_SECS` (about 49 minutes, so a paper test
waiting for the lock outlasts the window and its bounded end), the record saying so when it is. `SessionMaCross` winds down `FLATTEN_SECS` before the end (`EmaCross` has
no wind-down and trades to the stop), the node
stops, and the account is checked: no open order, no position. The wait for the stop is bounded by
`STOP_TIMEOUT_SECS`, the node's own stop bounds summed; past it the window is recorded failed, the
account is still checked, and `main` ends the process (`os._exit`), since the node's task can be neither
awaited nor cancelled.

The account check after the stop (`_flatten_after_stop`, ADR 0060) answers one question: which requests does this
window not yet know the end of, and has Alpaca shown each ended before the account is called flat? It
keeps them in one set: each order of the node's that the node did not see end (`_unsettled`: sent with
no answer, or still open in its cache), and each close the check itself sends. One leaves the set only on
Alpaca's word, an order under its client order ID in a status after which "no further updates will
occur", or Alpaca's own refusal of a close. Flat is no open order, no position and that set empty. A
position still held once nothing works is closed by a market order for the held quantity under a client
order ID of the check's own (Alpaca's "Using Client Order IDs"), not by `close_position`, which takes no
ID: an order sent that way can be looked up whatever became of its answer. It is sent only while Alpaca's
clock, read just before, says the regular session is open, as upstream's market exit closes again once
nothing works, and never while a request that could close the same shares is sent and not found, since
a lookup that finds nothing does not show the order was never placed and Alpaca says not to resend one
that timed out; such a window is recorded failed, naming the position and the request. What a market
order lacks that `close_position` had: Alpaca does not refuse it for a position that has gone, so it is
sent only after every order the window knows of has been seen ended or canceled in the same poll.

Two departures from upstream bound that check. In it, the window's alpaca-py client sends each request once
(`_retry = 0`, the attribute `RESTClient` keeps its retry count in; the constructor cannot set 0): alpaca-py
would repeat a 429 or 504 for up to `ADAPTER_CALL_SECS`, outside the check's deadline and after its reading
of Alpaca's clock, so a market close could go out after the session closed. The check repeats each step
itself, every poll, under one deadline that no poll starts after (a poll begun before it runs to its end,
within the counted bound: a reading looks up at most `LOOKUPS` requests and a step sends at most `CANCELS`
cancels, so every request is counted). And the node's loop runs its worker
calls in an executor of the window's own (`Calls`): after an abandoned stop no new call can start in it,
and the check waits, at most `ADAPTER_CALL_SECS`, for the ones the adapter left running (its `_call` is
shielded), so an order one of them sends is in the account before the check reads it. The run's node log and a
JSON report go to `log_dir`, outside the repository.

An extended window runs in pre-market or the overnight session, and after-hours once a feed has been seen
to deliver there on paper (`SESSION_FEEDS`; until then such a window is skipped, naming the observation).
The overnight session is the one Alpaca's 24/5 page
(https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md) puts from 8:00 PM to 4:00 AM ET "on
the evening before the trade date", following the NYSE holiday calendar, with its data on `boats` or
`overnight` and its eligible assets marked `overnight_tradable` in the Assets API: so an overnight window
runs only before a trade date on Alpaca's calendar, streams `boats` (`SESSION_FEEDS`, ADR 0055), and starts only on
an asset so marked and not `overnight_halted`. What reached the strategy (quotes, trades, live bars) and
the adapter's own lines about its stream, from the node's log, go in the record; a stream that delivered
nothing, or that the adapter logged an error for, fails the window, since the adapter reports a refused
or ended stream only there (ADR 0055).

Paper only: the execution client is `canon_alpaca`'s, whose config refuses anything but paper trading, and
the alpaca-py client here is built with `paper=True`. The key pair comes from the environment, which
`mise run paper:session` fills as `paper:test` does (ADR 0044, superseding ADR 0029's "only" clause). This evaluates nothing (ADR 0010).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections import deque
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS
from alpaca.common.constants import DEFAULT_RETRY_WAIT_SECONDS
from alpaca.common.exceptions import APIError
from alpaca.common.rest import RESTClient
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Calendar as AlpacaCalendar
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import FEED
from canon_alpaca.constants import VENUE
from canon_alpaca.execution import DEFINITIVE_SUBMIT_REFUSALS
from canon_alpaca.execution import ORDERS_PAGE_LIMIT
from canon_alpaca.providers import CONNECT_TIMEOUT_SECS
from canon_alpaca.providers import READ_TIMEOUT_SECS
from canon_alpaca.providers import counted
from canon_alpaca.providers import not_sent
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import LogLevel
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import FileWriterConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .paper_node import DELAY_POST_STOP_SECS
from .paper_node import EMA_CROSS
from .paper_node import EMA_CROSS_ID
from .paper_node import MAX_ORDER_SUBMIT_RATE
from .paper_node import TIMEOUT_DISCONNECTION_SECS
from .paper_node import TIMEOUT_SHUTDOWN_SECS
from .paper_node import alpaca_paper_builder
from .paper_node import ema_cross_config
from .position_cap import PositionCap
from .position_cap import position_cap_config
from .session_ma_cross import SessionMaCross
from .session_ma_cross import SessionMaCrossConfig

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.common import Cache

NEW_YORK = ZoneInfo("America/New_York")
SESSIONS = ("regular", "extended")
OVERNIGHT_STARTS = dt_time(20, 0)
"""The overnight session's start in New York, "8:00 PM - 4:00 AM ET", on the evening before its trade date
(Alpaca's 24/5 page); its end is the trade date's `session_open`."""


@dataclass(frozen=True)
class SessionFeed:
    """The feed a window streams and warms up on in one part of a session, or None for none, and what
    paper has shown of a feed delivering there."""

    feed: str | None
    observed: str


SESSION_FEEDS = {
    "regular": SessionFeed(
        FEED.value,
        "IEX quotes, trades and bars reached a node in the regular session on paper, in upstream's data "
        "tester, 13:59 to 14:44 New York, 2026-11-10 (the adapter's README)",
    ),
    "pre-market": SessionFeed(
        FEED.value, "not yet probed on paper; its first windows are the probe"
    ),
    "after-hours": SessionFeed(
        None,
        "probed on paper and no stream delivered: iex, overnight and boats each sent no quote or trade "
        "in 45 s on four symbols at 16:46 New York, 2026-11-10, though IEX's REST bars showed trades after "
        "the close; one short probe, not proof (ADR 0055)",
    ),
    "overnight": SessionFeed(
        DataFeed.BOATS.value,
        "boats delivered quotes, trades and bars overnight on paper, 2026-11-10 (ADR 0055, ADR 0044)",
    ),
}
"""Per part of a session, the feed a window uses and what paper has shown of it (ADR 0044). `boats` both
streams and serves bars (`STREAM_FEEDS`, `BARS_FEEDS`), as the strategy needs of its one feed; `overnight`
streams quotes only (ADR 0055). A part where the feeds were probed on paper and none delivered has no feed
and is skipped, naming the probe, as the lead decided on 2026-11-11, so the schedule does not fail every
evening by design; a part not yet probed runs, and its windows are the probe; inside a part with a feed, a
window that receives nothing still fails (`_data_failure`). The feed is named in the subscriptions' and the bars
request's params, `{"feed": ...}` (ADR 0055); one not served fails loudly, never another."""
DEFAULT_MINUTES = 30
MAX_MINUTES = 60
GAP_SECS = 300
"""Between one window's end and the next one's start, so paper tests waiting for the lock get it."""
FLATTEN_SECS = 120
"""`SessionMaCross` winds down this long before the window ends. A built-in has no wind-down: `EmaCross`
trades until the stop, and an order it sent then is the account check's to settle (ADR 0060)."""
STRATEGY_ID = StrategyId("SessionMaCross-001")


EMA_CROSS_SUBMIT_RATE = "1/01:00:00"
"""`EmaCross`'s submit budget in a window: one order an hour, longer than any window (`MAX_MINUTES`), so
one order reaches Alpaca and the risk engine denies every later one with `RATE_LIMIT_EXCEEDED` before it
leaves the node, as ADR 0037's paper test bounds it. One, because `EmaCross` at the pin reads neither its
position nor its working orders and an order refused still flips its side, so any second order can stack
a position (`WINDOW_STRATEGIES`); the cap stays behind it."""


@dataclass(frozen=True)
class WindowStrategy:
    """A strategy a window may run: its ID in the node, the sessions it may run in, with why, its
    submit budget for the risk engine (`paper_node.risk_limits`), and, for one of upstream's built-ins,
    the type name `add_builtin_strategy` takes; None for `SessionMaCross`, the one Python strategy."""

    strategy_id: StrategyId
    sessions: tuple[str, ...]
    why: str = ""
    """Why it runs in `sessions` only, when that is not every session."""
    max_order_submit_rate: str = MAX_ORDER_SUBMIT_RATE
    builtin: str | None = None


WINDOW_STRATEGIES = {
    "SessionMaCross": WindowStrategy(STRATEGY_ID, SESSIONS),
    EMA_CROSS: WindowStrategy(
        EMA_CROSS_ID,
        ("regular",),
        "it does not flatten on stop, and the account check after the stop closes at market only "
        "while the regular session is open (ADR 0068)",
        EMA_CROSS_SUBMIT_RATE,
        builtin=EMA_CROSS,
    ),
}
"""The strategies a window runs, by the name `--strategy` takes (ADR 0068, step 3): the repo's
`SessionMaCross`, a Python strategy, and upstream's built-in `EmaCross`, added by its type name through
`LiveNode.add_builtin_strategy` with `paper_node.ema_cross_config` (one share an order, upstream's periods,
10 and 50), as `paper_node.ema_cross_paper_node` adds it (ADR 0037). Both run beside `PositionCap` under the
same bounds: start flat, one share, the engine's risk limits and the account check after the stop.
`EmaCross` at the pin (`crates/trading/src/examples/strategies/ema_cross/strategy.rs`) keeps only whether
the fast EMA of the mid was above the slow one at the last quote, and sends one market order of
`trade_size` at each change, never reading its position or its working orders. So under latency one cross
still sends one order, but every cross while an order is in flight sends another, and an order Alpaca
refuses does not undo its side, so the alternation breaks and the position stacks (ADR 0037 saw a 4-share
short in under two seconds). Nothing in it guards orders in flight, unlike `SessionMaCross`
(`session_ma_cross`). So a window gives it one order (`EMA_CROSS_SUBMIT_RATE`, the risk engine's own
limit): it holds at most one share, the cap stays as the backstop, and the account check closes it."""
DEFAULT_STRATEGY = "SessionMaCross"
"""What a window runs when no `--strategy` is given, as the schedule gives none (ADR 0050)."""


def _runs_only(name: str, runs: WindowStrategy) -> str:
    """`<name> runs in <sessions> windows only`, from the table."""
    return f"{name} runs in {' and '.join(runs.sessions)} windows only"


STALE_RUNNING_SECS = 70 * 60
"""A record still `running` this long after it started is a window that died (60 minutes at most, plus
start-up, shutdown and the account check)."""
EXTENDED_READY = True
"""Whether extended windows may run: pre-market, after-hours and overnight, each where `SESSION_FEEDS` gives
it a feed (after-hours has none, so it is skipped). The adapter sends
`extended_hours` from the submit params (ADR 0038), its disconnect is bounded (ADR 0039), and an overnight
window passed on paper before this was turned on (ADR 0044, Confirmation)."""
POLL_SECS = 2.5
"""Between the account check's polls after the stop. A poll sends at most seven requests (`LOOKUPS`
lookups, two reads, the clock, `CANCELS` cancels or one close), two when nothing is unsettled: at most
168 a minute, inside the 200 a minute Alpaca's skill gives for the paper API (ADR 0044, Evidence)."""
COMMIT_ENV = "CANON_COMMIT"
"""Set by the scheduled runner, `home/.claude/canon/paper-session.sh`, to the `origin/main` commit it checked
out for this firing (ADR 0050)."""
BY_HAND_ENV = "CANON_PAPER_BY_HAND"
"""`1` in a run of `mise run paper:session` the schedule did not start (`mise.toml`): its records go to
`by-hand/` in the log folder, which `scripts/automation status` does not read; the gap marker and the
node's logs stay in the log folder, so the gap still separates every window (ADR 0050)."""
SCHEDULED_ENV = "CANON_PAPER_SCHEDULED"
"""`1` only in a run the schedule started: the launchd agent's plist sets it (launchd.plist(5),
EnvironmentVariables), so a run of the runner or the task by hand does not have it (ADR 0050)."""
SOURCE_ROOT = Path(__file__).resolve().parents[2]
"""The checkout this module runs from."""
MARKET_EXIT_INTERVAL_MS = 1000
MARKET_EXIT_MAX_ATTEMPTS = 60
MANAGED_DELAY_POST_STOP_SECS = math.ceil(
    MARKET_EXIT_INTERVAL_MS * MARKET_EXIT_MAX_ATTEMPTS / 1000 + DELAY_POST_STOP_SECS
)
"""A regular window's `delay_post_stop` (65 s): after the trader's stop the node processes events, the
managed stop's market exit among them, only for this long before it disconnects its clients
(`LiveNode::stop`, `process_runner_for`, `crates/live/src/node/mod.rs` at the pin); so the market exit at its
longest and the residual 5 s (`paper_node`). The strategy stops once its exit ends (`finalize_market_exit`)."""
STOP_TIMEOUT_SECS = (
    MANAGED_DELAY_POST_STOP_SECS
    + TIMEOUT_DISCONNECTION_SECS
    + TIMEOUT_SHUTDOWN_SECS
    + CONNECT_TIMEOUT_SECS
    + READ_TIMEOUT_SECS
)
"""How long the window waits for the node to stop (about 115 s): the strategy's market exit at its longest,
the node's own stop bounds (`paper_node`), and one Alpaca request at the adapter's timeouts (ADR 0039),
which a call in flight at the disconnect can outlast it by."""
AFTER_STOP_SECS = 60
"""How long the account check after the stop starts polls: a poll begun before it runs to its end, then a
last reading and a cancel of what it shows (`AFTER_END_SECS`)."""
REQUEST_SECS = CONNECT_TIMEOUT_SECS + READ_TIMEOUT_SECS
"""One request at the adapter's timeouts (ADR 0039), sent once, as the account check sends it. The two
timeouts' sum, not a wall-clock bound: the connect timeout applies per address tried and the read timeout
between bytes (ADR 0039), so a host resolving several addresses, or a response trickling in, can take
longer."""
ADAPTER_CALL_SECS = (
    DEFAULT_RETRY_ATTEMPTS + 1
) * REQUEST_SECS + DEFAULT_RETRY_ATTEMPTS * DEFAULT_RETRY_WAIT_SECONDS
"""One adapter REST call at its longest (about 129 s): alpaca-py's first request and its retries, each at
the adapter's timeouts, and the waits between them. A stream call the node runs in a worker thread has no
such bound; one still running after this is recorded and the check goes on."""
LOOKUPS = 2
"""The requests still unsettled that one reading of the account check looks up, in turn: with more, each
waits its turn over the polls, and one never reached is named in what is left."""
CANCELS = 2
"""The cancels one step of the account check sends, those not yet tried first: the strategy's one working
order and the check's close; with more, the rest wait for the next poll."""
READING_SECS = (LOOKUPS + 2) * REQUEST_SECS
"""One reading of the account check at its longest: its lookups, the open orders and the position."""
POLL_AT_LONGEST_SECS = READING_SECS + (1 + CANCELS) * REQUEST_SECS
"""One poll of the account check at its longest: a reading, the clock, and its cancels (a close, sent
instead of them, is one request)."""
LAST_CLOSE_SECS = (
    STOP_TIMEOUT_SECS + ADAPTER_CALL_SECS + AFTER_STOP_SECS + READING_SECS + REQUEST_SECS
)
"""From a window's end until the account check's last market close is sent, at most (about 455 s): the
stop at its bound, the node's last worker calls, and the check's deadline with the poll begun before it,
its reading and the clock read. Every request of that poll is counted."""
AFTER_END_SECS = STOP_TIMEOUT_SECS + ADAPTER_CALL_SECS + AFTER_STOP_SECS + 2 * POLL_AT_LONGEST_SECS
"""From a window's end until its process is done with Alpaca, at most (about 725 s): the stop, the node's
last worker calls, the check's deadline, the poll begun before it at its longest, then the last reading,
the clock and the last cancels, as long again. Every request is counted: a reading looks up no more than
`LOOKUPS` orders and a step cancels no more than `CANCELS`, however many there are."""
SKEW_SECS = 60
"""How far this host's clock may be from Alpaca's: the window runs to an end taken from Alpaca's clock, timed
on this host's."""
END_MARGIN_SECS = math.ceil(LAST_CLOSE_SECS + SKEW_SECS)
"""A window ends at least this long before its session does, so the account check's last market close is
sent while the session is open, with a minute for skew between Alpaca's clock and this host's."""
PAPER_LOCK_WAIT_SECS = 3900
"""How long `mise run paper:test` waits for the paper lock (`lockf -t` in `mise.toml`; a test reads it
there, so the two cannot drift)."""
START_UP_SECS = 30
"""The process's start before Alpaca's clock is first read (`uv run`, the imports)."""
LONGEST_WINDOW_SECS = math.floor(
    PAPER_LOCK_WAIT_SECS - START_UP_SECS - ADAPTER_CALL_SECS - SKEW_SECS - AFTER_END_SECS
)
"""The longest a window runs (about 49 minutes): a paper test queued behind it gets the lock within its
wait, after the start-up, the first clock read (with alpaca-py's retries, `ADAPTER_CALL_SECS`), the window
timed on a host clock up to `SKEW_SECS` behind Alpaca's, and its bounded end (`AFTER_END_SECS`). The reads
after the clock read are inside the window, whose end is absolute."""


def default_log_dir() -> Path:
    """The per-user temporary folder `mise run paper:session` passes, whatever `$TMPDIR` a session sets,
    so the gap marker is found by every caller: on macOS `getconf DARWIN_USER_TEMP_DIR` (getconf(1),
    `confstr(3)`'s `_CS_DARWIN_USER_TEMP_DIR`), and `RuntimeError` if it does not give one, since `$TMPDIR`'s
    folder is not where the gap marker and `scripts/automation status` look; elsewhere, Python's
    `tempfile.gettempdir()`."""
    if sys.platform != "darwin":
        return Path(tempfile.gettempdir()) / "canon-paper-session"
    try:
        found = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            f"getconf DARWIN_USER_TEMP_DIR failed ({type(e).__name__}: {e}), so the per-user folder "
            "for the records is not known; pass --log-dir"
        ) from e
    if not Path(found).is_absolute():
        raise RuntimeError(
            f"getconf DARWIN_USER_TEMP_DIR gave {found!r}, not an absolute folder, so the per-user "
            "folder for the records is not known; pass --log-dir"
        )
    return Path(found) / "canon-paper-session"


def running_commit(root: Path = SOURCE_ROOT) -> str:
    """The commit this run's code is at, for its records: `CANON_COMMIT` when the scheduled runner set it,
    else `git rev-parse HEAD` of `root`, marked when a tracked file differs from it, else `unknown: <why>`.
    Never empty, so a record always says which code ran or that it could not tell. Read from git once a
    process, so every record of one run names the same code."""
    if found := os.environ.get(COMMIT_ENV, "").strip():
        return found
    return _checkout_commit(root)


@functools.cache
def _checkout_commit(root: Path) -> str:
    try:
        head, changed = (
            subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            for args in (["rev-parse", "--verify", "HEAD"], ["status", "--porcelain", "-uno"])
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown: {type(e).__name__}: {e}"
    return f"{head} with uncommitted changes" if changed else head


@dataclass(frozen=True)
class WindowConfig:
    """What one window runs. `session` is `regular` (market orders) or `extended` (marked limits);
    `strategy` a name of `WINDOW_STRATEGIES`, refused by name in a session it does not run in."""

    symbol: str = "SPY"
    session: str = "regular"
    minutes: int = DEFAULT_MINUTES
    fast_period: int = 10
    slow_period: int = 20
    """`SessionMaCross`'s periods; `EmaCross` runs at upstream's (`paper_node.ema_cross_config`)."""
    log_dir: Path = field(default_factory=default_log_dir)
    strategy: str = DEFAULT_STRATEGY
    """A name of `WINDOW_STRATEGIES`."""

    def __post_init__(self) -> None:
        if self.session not in SESSIONS:
            raise ValueError(f"session must be one of {SESSIONS}, got {self.session!r}")
        if (runs := WINDOW_STRATEGIES.get(self.strategy)) is None:
            raise ValueError(
                f"strategy must be one of {tuple(WINDOW_STRATEGIES)}, got {self.strategy!r}"
            )
        if self.session not in runs.sessions:
            raise ValueError(
                f"{_runs_only(self.strategy, runs)}, not in the {self.session} session: {runs.why}"
            )
        if self.symbol != "SPY":
            raise ValueError(
                f"the notional cap is sized for SPY (paper_node.MAX_NOTIONAL_PER_ORDER_USD); got {self.symbol}"
            )
        if not 1 <= self.minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be 1 to {MAX_MINUTES}, got {self.minutes}")
        if self.minutes * 60 <= FLATTEN_SECS:
            raise ValueError(f"a window must be longer than its {FLATTEN_SECS} s wind-down")
        if self.session == "extended" and not EXTENDED_READY:
            raise ValueError(
                "an extended window waits until one has passed on paper (EXTENDED_READY, ADR 0044)"
            )

    @property
    def instrument_id(self) -> InstrumentId:
        return InstrumentId.from_str(f"{self.symbol}.{VENUE}")

    @property
    def bar_type(self) -> BarType:
        """One-minute bars the data engine builds from the trade stream (the adapter streams no bars)."""
        return BarType.from_str(f"{self.instrument_id}-1-MINUTE-LAST-INTERNAL")


def in_new_york(moment: datetime) -> datetime:
    """`moment` in New York time. alpaca-py builds `Calendar.open` and `close` naive, from the response's
    date and HH:MM (`trading/models.py`), which Alpaca gives in New York time; an aware value is converted."""
    return moment.replace(tzinfo=NEW_YORK) if moment.tzinfo is None else moment.astimezone(NEW_YORK)


def _trading_days(calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None) -> dict:
    """The calendar's trading days by date: one day, several, or none."""
    if calendar is None:
        return {}
    days = [calendar] if isinstance(calendar, AlpacaCalendar) else list(calendar)
    return {day.date: day for day in days}


def _session_bounds(day: AlpacaCalendar) -> tuple[datetime, datetime]:
    """A trading day's `session_open` and `session_close` in New York, or `RuntimeError` naming each one
    missing: both `Optional[str]` in alpaca-py's `Calendar`, and no bound is assumed in their place."""
    session_open, session_close = day.session_open, day.session_close
    if not session_open or not session_close:
        missing = [n for n in ("session_open", "session_close") if not getattr(day, n)]
        raise RuntimeError(
            f"Alpaca's calendar for {day.date:%Y-%m-%d} gives no {' or '.join(missing)}, so the "
            "extended session's bounds are not known"
        )
    opens = datetime.strptime(session_open, "%H%M").time()
    closes = datetime.strptime(session_close, "%H%M").time()
    return datetime.combine(day.date, opens, NEW_YORK), datetime.combine(day.date, closes, NEW_YORK)


def session_part(
    session: str, clock: AlpacaClock, calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None
) -> tuple[datetime, str]:
    """When the configured session that is open now ends, and which part of it is open (`regular`,
    `pre-market`, `after-hours` or `overnight`), or `RuntimeError` if none is open now.

    The extended session is pre-market and after-hours on a trading day, from its `session_open` to the
    regular open and from the regular close to its `session_close`, and the overnight session, which
    Alpaca's 24/5 page puts from 8:00 PM to 4:00 AM ET, "on the evening before the trade date", following
    the NYSE holiday calendar: from `OVERNIGHT_STARTS` on the day before a trading day to that day's
    `session_open`."""
    now = clock.timestamp.astimezone(NEW_YORK)
    if session == "regular":
        if not clock.is_open:
            raise RuntimeError(
                "the regular session is closed; Alpaca's clock says it next opens "
                f"{clock.next_open.astimezone(NEW_YORK):%Y-%m-%d %H:%M} New York"
            )
        return clock.next_close.astimezone(NEW_YORK), "regular"
    if clock.is_open:
        raise RuntimeError("the regular session is open; an extended window runs outside it")
    days = _trading_days(calendar)
    # the overnight session open now belongs to today's trade date before 20:00, else to tomorrow's; it
    # needs only that date's `session_open`, its end
    starts_today = now.time() >= OVERNIGHT_STARTS
    trade_date = now.date() + timedelta(days=1) if starts_today else now.date()
    if (trade_day := days.get(trade_date)) is not None:
        starts = datetime.combine(trade_date - timedelta(days=1), OVERNIGHT_STARTS, NEW_YORK)
        if starts <= now:
            if not trade_day.session_open:
                _session_bounds(trade_day)  # raises, naming what is missing
            ends = datetime.combine(
                trade_date, datetime.strptime(trade_day.session_open, "%H%M").time(), NEW_YORK
            )
            if now < ends:
                return ends, "overnight"
    if (today := days.get(now.date())) is not None:
        opens, closes = _session_bounds(today)
        regular_open, regular_close = in_new_york(today.open), in_new_york(today.close)
        if opens <= now < regular_open:
            return regular_open, "pre-market"
        if regular_close <= now < closes:
            return closes, "after-hours"
    if today is None and trade_day is None:
        raise RuntimeError(
            f"{now:%Y-%m-%d} is not a trading day on Alpaca's calendar, and no overnight session "
            "is open for the next one"
        )
    raise RuntimeError(
        f"{now:%H:%M} New York is outside pre-market, after-hours and the overnight session"
    )


def window_feed(part: str) -> str:
    """The feed a window on `part` of its session streams and warms up on (`SESSION_FEEDS`), or
    `WindowSkipped` for a part no feed has been seen to serve on paper, naming what was observed."""
    if (found := SESSION_FEEDS.get(part)) is None:
        raise RuntimeError(f"no feed is set for the {part!r} part of a session (SESSION_FEEDS)")
    if found.feed is None:
        raise WindowSkipped(f"no {part} window: {found.observed}")
    return found.feed


def builtin_config(builtin: str, instrument_id: InstrumentId) -> object:
    """The config a built-in of `WINDOW_STRATEGIES` runs with in a window, or `ValueError` for one the
    window has no config for."""
    if builtin == EMA_CROSS:
        return ema_cross_config(instrument_id)
    raise ValueError(f"no window config for the built-in {builtin!r}")


class CachedData:
    """What a window's record reads of a built-in strategy, which runs in Rust and hands the window no
    Python object: the quotes and trades in the node's cache for the instrument (`Cache.quote_count`,
    `trade_count`), the data engine's copy of what reached the strategies, at most the cache's
    `tick_capacity` (10,000 by default at the pin) each, read before the node is disposed. No warm-up and
    no bars of the window's own, so those counts are 0."""

    dropped: tuple = ()
    history_bars = 0
    history_dropped = 0
    live_bars = 0

    def __init__(self, cache: Cache, instrument_id: InstrumentId) -> None:
        self._cache = cache
        self._instrument_id = instrument_id

    @property
    def quotes(self) -> int:
        return self._cache.quote_count(self._instrument_id)

    @property
    def trades(self) -> int:
        return self._cache.trade_count(self._instrument_id)


def window_node(
    config: WindowConfig, *, wind_down_at: datetime, log_name: str, feed: str
) -> tuple[LiveNode, SessionMaCross | CachedData, PositionCap]:
    """The node, Alpaca's clients, the native risk limits and runtime checks, with the strategy and the cap
    it runs (returned too: the node does not hand its Python strategies back; for a built-in, what the
    record reads of it, `CachedData`). `feed` is named in `SessionMaCross`'s subscriptions and its warm-up
    request (ADR 0055); `EmaCross` subscribes with no params, so it runs only where the feed is the
    adapter's default."""
    runs = WINDOW_STRATEGIES[config.strategy]
    if runs.builtin is not None and feed != FEED.value:
        # holds while `SESSION_FEEDS` gives a built-in's sessions the adapter's default feed
        raise RuntimeError(
            f"{config.strategy} names no feed in its subscription, so it streams {FEED.value}; this "
            f"window's part streams {feed}"
        )
    extended = config.session == "extended"
    # `SessionMaCross`'s managed stop runs a market exit inside the post-stop delay; a built-in has none
    managed = runs.builtin is None and not extended
    node = (
        alpaca_paper_builder(
            "CANON-PAPER-SESSION",
            config.instrument_id,
            # the managed stop's market exit runs inside the post-stop delay
            delay_post_stop_secs=MANAGED_DELAY_POST_STOP_SECS if managed else DELAY_POST_STOP_SECS,
            max_order_submit_rate=runs.max_order_submit_rate,
        )
        .with_logging(
            LoggerConfig(
                fileout_level=LogLevel.INFO,  # file output is off unless a level is given
                file_config=FileWriterConfig(directory=str(config.log_dir), file_name=log_name),
            )
        )
        .build()
    )
    if runs.builtin is not None:
        node.add_builtin_strategy(runs.builtin, builtin_config(runs.builtin, config.instrument_id))
        cap = PositionCap(position_cap_config(config.instrument_id))
        node.add_strategy(cap)
        return node, CachedData(node.cache, config.instrument_id), cap
    strategy = SessionMaCross(
        SessionMaCrossConfig(
            bar_type=config.bar_type,
            trade_size=Quantity.from_int(1),
            fast_period=config.fast_period,
            slow_period=config.slow_period,
            extended_hours=extended,
            feed=feed,
            wind_down_at=wind_down_at,
            warm_up=True,  # the averages start from the adapter's last bars (ADR 0047)
            strategy_id=STRATEGY_ID,
            order_id_tag="001",
            # In the session, `stop()` runs upstream's market exit, with closing orders Alpaca takes.
            manage_stop=not extended,
            market_exit_reduce_only=False,
            market_exit_time_in_force=TimeInForce.DAY,
            market_exit_interval_ms=MARKET_EXIT_INTERVAL_MS,
            market_exit_max_attempts=MARKET_EXIT_MAX_ATTEMPTS,
        )
    )
    cap = PositionCap(position_cap_config(config.instrument_id))
    node.add_strategy(strategy)
    node.add_strategy(cap)
    return node, strategy, cap


@dataclass
class WindowReport:
    """What a window did, written as JSON beside its log."""

    session: str
    symbol: str
    started: str
    ended: str = ""
    seconds: float = 0.0
    planned_seconds: float = 0.0
    """From Alpaca's clock at the start to the window's end, the wind-down included; the stop and the
    account check come after it (`AFTER_END_SECS` at most)."""
    shortened: str = ""
    """Why the window runs shorter than the minutes asked for, if it does."""
    orders: dict[str, list[str]] = field(default_factory=dict)
    fills: int = 0
    denials: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    cap_checks: int = 0
    cap_peak: str = "0"
    cap_tripped: str | None = None
    left: list[str] = field(default_factory=list)
    closed_after_stop: bool = False
    """Whether a close the account check sent after the stop was placed at Alpaca."""
    unsettled: dict[str, list[str]] = field(default_factory=dict)
    """Each order of the node's that it did not see end, by client order ID: its side and why it is here,
    then each status Alpaca showed for it after the stop, in order."""
    closes: dict[str, list[str]] = field(default_factory=dict)
    """Each close the account check sent that may have been placed, by its client order ID: what was
    sent, then what came of it (`no answer: ...`, each status Alpaca showed), in order."""
    closes_not_placed: int = 0
    """The closes the account check sent that placed nothing: refused by Alpaca (403, 422), or never sent
    (a connect timeout); `errors` holds what each was answered."""
    errors: list[str] = field(default_factory=list)
    part: str = ""
    """The part of the session the window ran in: `regular`, `pre-market`, `after-hours`, `overnight`."""
    strategy: str = DEFAULT_STRATEGY
    """The strategy the window ran, a name of `WINDOW_STRATEGIES`."""
    feed: str = ""
    """The feed the strategy streamed and warmed up on (ADR 0055)."""
    overnight: str = ""
    """What Alpaca's asset said of the symbol overnight, for an overnight window."""
    quotes: int = 0
    trades: int = 0
    live_bars: int = 0
    """The quotes, trades and live bars that reached the strategy: none of the first two is a stream that
    delivered nothing (`result` failed); trades and no bars, a quiet market. For a built-in, the node
    cache's counts (`CachedData`), each at most the cache's `tick_capacity` (10,000), so they show that
    data came, not how much."""
    stream: list[str] = field(default_factory=list)
    """The adapter's own log lines about its stream (`STREAM_LINES`): alpaca-py's warnings and errors,
    the stream's end by itself and a subscription it refused. An error among them fails the window."""
    warm_up_bars: int = 0
    warm_up_dropped: int = 0
    result: str = "running"
    """`running` while the window runs, then `ok` or `failed: <why>`. A record still `running` after
    `stale_after` seconds belongs to a window that died; `scripts/automation status` reads both."""
    started_epoch: float = field(default_factory=time.time)
    stale_after: int = STALE_RUNNING_SECS
    commit: str = field(default_factory=running_commit)
    """The code this window runs (`running_commit`), in the `running` record and the result alike."""


class WindowRefused(RuntimeError):
    """The window did not start, for a reason someone must look at (the account not flat, the session not
    open when one was named). Nothing was placed. Recorded as the latest result; the run exits 1."""


class StopTimedOut(RuntimeError):
    """The node did not stop within `STOP_TIMEOUT_SECS`. Recorded as failed after the account check;
    `main` then ends the process, since the node's task still runs and nothing can end it (module header)."""


_ABANDONED: list = []
"""A runner, the node's task and its worker calls (`Calls`) left after a stop timed out, held so neither
the runner nor the task is collected (the hosted run would then drive its shutdown inline,
`python/node.rs`) before `main` ends the process."""


class WindowSkipped(RuntimeError):
    """The window did not start, for a reason no one need act on: one that clears by itself (inside the gap
    after the last window, too little of the session left), or a part of a session with no feed
    (`SESSION_FEEDS`), which recurs each day until its row changes. Nothing was placed. Not recorded as a
    result; the run exits 0."""


def _write_atomically(path: Path, body: str) -> None:
    """Replace `path` whole (`os.replace`), so a reader or a kill never sees half a record, through a
    temporary file of this writer's own (`tempfile.mkstemp`), so two writers never share one."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(body)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def records_dir(log_dir: Path) -> Path:
    """Where this run's records go: `log_dir`, or its `by-hand/` for a run by hand (`BY_HAND_ENV`)."""
    if os.environ.get(BY_HAND_ENV, "") == "1":
        log_dir = log_dir / "by-hand"
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def write_record(log_dir: Path, report: WindowReport | dict) -> None:
    """Write `latest.json`, the one record `scripts/automation status` reads, and, for a window that ran,
    its own `window-<stamp>.json`, in `records_dir`."""
    log_dir = records_dir(log_dir)
    body = json.dumps(asdict(report) if isinstance(report, WindowReport) else report, indent=2)
    if isinstance(report, WindowReport):
        _write_atomically(log_dir / f"window-{report.started}.json", body)
    _write_atomically(log_dir / "latest.json", body)


def write_heartbeat(log_dir: Path, outcome: str) -> None:
    """Write `last-firing.json` on every run, a window or not, a run by hand included: status shows its
    outcome, and `scheduled` says whether the schedule started it (`SCHEDULED_ENV`, or the runner's
    `COMMIT_ENV` without `BY_HAND_ENV`, as a runner installed before `SCHEDULED_ENV` sets it). Whether the schedule
    still fires, status reads from the runner's own stamp, `scheduled-firing.json`, which no run by hand
    writes; a run by hand writes to its own folder (`mise.toml`, paper:session; ADR 0050)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    _write_atomically(
        records_dir(log_dir) / "last-firing.json",
        json.dumps(
            {
                "at_epoch": time.time(),
                "outcome": outcome,
                "commit": running_commit(),
                "scheduled": os.environ.get(SCHEDULED_ENV, "") == "1"
                or (
                    bool(os.environ.get(COMMIT_ENV, "").strip())
                    and os.environ.get(BY_HAND_ENV, "") != "1"
                ),
            }
        ),
    )


def session_now(trading: TradingClient) -> tuple[AlpacaClock, list[AlpacaCalendar], float]:
    """Alpaca's clock and the calendar's days from today to tomorrow (New York; a day that is not a
    trading day is absent), read once per run, and the `time.monotonic()`
    at which the clock's answer arrived: its timestamp is as old as the time since, whatever the read's
    own retries took before it."""
    clock = trading.get_clock()
    read_at = time.monotonic()
    assert isinstance(clock, AlpacaClock)
    today = clock.timestamp.astimezone(NEW_YORK).date()
    # the next day too: an overnight session that starts at 20:00 belongs to the next trading day
    days = trading.get_calendar(GetCalendarRequest(start=today, end=today + timedelta(days=1)))
    return clock, list(days) if isinstance(days, list) else [], read_at


def paper_trading() -> TradingClient:
    """alpaca-py's paper client with the adapter's HTTP timeout mounted (ADR 0039): alpaca-py sends no
    timeout, and the account check after an abandoned stop must not wait on a request for ever."""
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def send_once(trading: TradingClient) -> None:
    """Make alpaca-py send each of `trading`'s requests once (module header): `RESTClient._request` then
    raises a 429 or 504 as an `APIError` instead of repeating it outside the account check's deadline.
    The start-up reads keep alpaca-py's retries; a stand-in client is left as it is."""
    if isinstance(trading, RESTClient):
        trading._retry = 0


def open_orders(trading: TradingClient, symbol: str) -> list:
    orders = trading.get_orders(
        # the most one listing returns ("Defaults to 50 and max is 500"), as the adapter asks
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=ORDERS_PAGE_LIMIT)
    )
    assert isinstance(orders, list)
    return orders


def position_qty(trading: TradingClient, symbol: str) -> Decimal:
    """The signed quantity Alpaca holds in `symbol`, 0 when none (404)."""
    try:
        position = trading.get_open_position(symbol)
    except APIError as e:
        if e.status_code == 404:
            return Decimal(0)
        raise
    assert isinstance(position, AlpacaPosition)
    qty = abs(Decimal(str(position.qty)))
    return -qty if getattr(position.side, "value", position.side) == "short" else qty


def leftovers(trading: TradingClient, symbol: str) -> list[str]:
    return described(open_orders(trading, symbol), position_qty(trading, symbol), symbol)


def described(orders: list, qty: Decimal, symbol: str) -> list[str]:
    """What is left, as a window's record names it: each open order, then the position."""
    left = [f"open order {o.client_order_id}" for o in orders]
    if qty:
        left.append(f"position of {qty} {symbol}")
    return left


def window_seconds(minutes: int, session_left: float) -> tuple[float, str]:
    """How long a window of `minutes` runs with `session_left` seconds of its session to go, and why it
    is shorter, if it is: it ends `END_MARGIN_SECS` before the session does, and runs at most
    `LONGEST_WINDOW_SECS`, so it and its bounded end fit a paper test's wait for the lock."""
    asked = minutes * 60
    by_session = session_left - END_MARGIN_SECS
    seconds = min(asked, LONGEST_WINDOW_SECS, by_session)
    if seconds >= asked:
        return seconds, ""
    if seconds == by_session:
        why = f"ends {END_MARGIN_SECS} s before the session does"
    else:
        why = (
            f"at most {LONGEST_WINDOW_SECS} s, so it and its bounded end fit the paper lock's "
            f"{PAPER_LOCK_WAIT_SECS} s wait"
        )
    return seconds, f"shortened from {minutes} minutes to {seconds:.0f} s: {why}"


def run_window(
    config: WindowConfig,
    *,
    trading: TradingClient | None = None,
    market: tuple[AlpacaClock, AlpacaCalendar | Sequence[AlpacaCalendar] | None] | None = None,
    market_read_at: float | None = None,
) -> WindowReport:
    """Run one window as the module header describes; raise if it cannot start or is not left flat.
    `trading` and `market` (the clock and calendar) are read here unless the caller already has them, in
    which case `market_read_at` is the `time.monotonic()` at which the clock's answer arrived
    (`session_now`)."""
    config.log_dir.mkdir(parents=True, exist_ok=True)
    marker = config.log_dir / "last-window-end"
    # Dated by when it was written, so a marker cut short by a kill still counts.
    if marker.exists() and time.time() - marker.stat().st_mtime < GAP_SECS:
        raise WindowSkipped(f"the last window ended under {GAP_SECS} s ago")
    trading = trading or paper_trading()
    if market is None:
        clock, calendar, read_at = session_now(trading)
    else:
        clock, calendar = market
        read_at = time.monotonic() if market_read_at is None else market_read_at
    try:
        end_of_session, part = session_part(config.session, clock, calendar)
    except RuntimeError as e:
        raise WindowRefused(str(e)) from e
    feed = window_feed(part)
    overnight = overnight_eligibility(trading, config.symbol) if part == "overnight" else ""
    if left := leftovers(trading, config.symbol):
        raise WindowRefused(
            f"the paper account is not flat in {config.symbol}; not starting: {left}"
        )

    now = clock.timestamp.astimezone(NEW_YORK)
    seconds, shortened = window_seconds(config.minutes, (end_of_session - now).total_seconds())
    if seconds <= FLATTEN_SECS + 1:
        raise WindowSkipped(f"under {FLATTEN_SECS + END_MARGIN_SECS} s left in the session")
    # Absolute times, taken from Alpaca's clock: start-up eats into the window, not into the wind-down.
    ends = clock.timestamp.astimezone(UTC) + timedelta(seconds=seconds)
    seconds -= time.monotonic() - read_at  # what the reads since the clock read have left
    if seconds <= FLATTEN_SECS + 1:
        raise WindowSkipped(f"the start-up reads left under {FLATTEN_SECS + 1} s of the window")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = WindowReport(
        config.session,
        config.symbol,
        started=stamp,
        planned_seconds=seconds,
        shortened=shortened,
        part=part,
        strategy=config.strategy,
        feed=feed,
        overnight=overnight,
    )
    write_record(
        config.log_dir, report
    )  # `running`: a window killed from here on leaves this behind
    started = time.monotonic()
    abandoned_before = len(_ABANDONED)
    failure: BaseException | None = None
    ran = False  # whether the node was run: only then is the gap marker written
    cache = None
    unsettled: dict[str, tuple[str, str]] = {}
    strategy_id = WINDOW_STRATEGIES[config.strategy].strategy_id
    try:
        node, strategy, cap = window_node(
            config,
            wind_down_at=ends - timedelta(seconds=FLATTEN_SECS),
            log_name=f"window-{stamp}",
            feed=feed,
        )
        cache = node.cache
        try:
            ran = True
            _run_node(node, ends)
        finally:
            try:
                report.seconds = round(time.monotonic() - started, 1)
                _fill(report, cache, strategy_id)
                report.dropped = list(strategy.dropped)
                report.warm_up_bars = strategy.history_bars
                report.warm_up_dropped = strategy.history_dropped
                report.quotes, report.trades = strategy.quotes, strategy.trades
                report.live_bars = strategy.live_bars
                report.cap_checks, report.cap_peak = cap.checks, str(cap.peak)
                report.cap_tripped = cap.tripped
            finally:
                try:
                    # before the disposal, which clears the cache
                    unsettled = _unsettled(cache, strategy_id)
                finally:
                    if len(_ABANDONED) == abandoned_before:  # else its abandoned task owns it
                        node.dispose()
    except BaseException as e:
        failure = e
        raise
    finally:
        try:
            _wait_for_calls(_ABANDONED[abandoned_before:], report)
            if cache is not None and len(_ABANDONED) > abandoned_before:
                # after the wait: an abandoned node's last submits are in it
                unsettled = _unsettled(cache, strategy_id)
            for client_order_id, (side, why) in unsettled.items():
                report.unsettled[client_order_id] = [f"{side}: {why}"]
            report.left = _flatten_after_stop(
                trading,
                config,
                report,
                unsettled={c: side for c, (side, _) in unsettled.items()},
            )
        except BaseException as e:  # the account could not be checked: never recorded as ok
            failure = failure or e
            report.errors.append(f"checking the account after the stop: {type(e).__name__}: {e}")
            raise
        finally:
            report.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            if ran:
                marker.write_text(str(time.time()))
                _read_stream_lines(report, config.log_dir / f"window-{stamp}.log")
            no_data = _data_failure(report) if ran else None
            if failure is not None:
                report.result = f"failed: {type(failure).__name__}: {failure}"
            elif report.left:
                report.result = f"failed: not left flat: {report.left}"
            elif report.cap_tripped:
                report.result = f"failed: {report.cap_tripped}"
            elif no_data:
                report.result = f"failed: {no_data}"
            else:
                report.result = "ok"
            write_record(config.log_dir, report)
    if report.left:
        raise RuntimeError(f"the paper account was not left flat in {config.symbol}: {report.left}")
    if report.cap_tripped:
        raise RuntimeError(f"the position cap stopped the window: {report.cap_tripped}")
    if no_data:
        raise RuntimeError(f"the window ran without market data: {no_data}")
    return report


STREAM_LINES = ("Alpaca stream on feed ", "Cannot subscribe: ")
"""How the adapter's data client begins each line it logs about its stream: alpaca-py's own warnings and
errors, relayed from the stream's thread, the stream's end by itself, and a subscription it refused, a
feed other than its stream's among them (`canon_alpaca.data`, ADR 0055; the package README)."""
LOG_LEVEL = re.compile(r"^\S+ \[(?P<level>[A-Z]+)\] ")
"""The level of a line of the node's plain-text log, `<timestamp> [LEVEL] <component>: <message>`."""
MAX_STREAM_LINES = 20


def _read_stream_lines(report: WindowReport, log: Path) -> None:
    """Copy the adapter's stream lines from the node's log (`STREAM_LINES`, at most `MAX_STREAM_LINES`,
    then a count) into `report.stream`: the log is where the adapter reports a stream Alpaca refused or
    that ended, since the subscription has returned by then (ADR 0055). A log that cannot be read is
    noted in `errors`."""
    # The node's logger writes on its own thread; wait until what it was sent is on disk
    # (`logging_sync_to_disk`, as the adapter's tests do).
    logging_sync_to_disk()
    try:
        lines = [line.strip() for line in log.read_text(errors="replace").splitlines()]
    except OSError as e:
        _note(report, f"reading the node log for the stream's own lines: {type(e).__name__}: {e}")
        return
    found = [line for line in lines if any(mark in line for mark in STREAM_LINES)]
    report.stream = found[:MAX_STREAM_LINES]
    if len(found) > MAX_STREAM_LINES:
        report.stream.append(f"... and {len(found) - MAX_STREAM_LINES} more such lines")


def _data_failure(report: WindowReport) -> str | None:
    """Why the window's market data cannot be trusted, or None: the adapter logged an error about its
    stream (it ended by itself, or Alpaca refused it), or the stream delivered no quote and no trade for
    the symbol in the whole window. A window with quotes but no trade, and so no bar, is a quiet market,
    not a failure: overnight that is normal (the record's `live_bars` shows it)."""
    if errors := [
        line
        for line in report.stream
        if (level := LOG_LEVEL.match(line)) is not None and level["level"] == "ERROR"
    ]:
        return f"the {report.feed} data stream reported an error: {errors[0]}"
    if not report.quotes and not report.trades:
        return (
            f"no market data: the {report.feed} stream delivered no quote and no trade for "
            f"{report.symbol} in {report.seconds} s"
        )
    return None


def overnight_eligibility(trading: TradingClient, symbol: str) -> str:
    """What Alpaca says of `symbol` in the overnight session, or `WindowRefused` if it is not eligible
    there or is halted for it. Alpaca's 24/5 page: "Assets tradable in the overnight session can be
    identified via the `overnight_tradable` attribute in the Assets API", with `overnight_halted` for an
    eligible asset halted overnight; the page calls them boolean attributes and the asset reference lists
    them as values of `attributes`, so either form counts. Read raw, through alpaca-py's own
    `RESTClient.get` on the path `get_asset` calls, since alpaca-py 0.45.0's `Asset` has no such field."""
    try:
        asset = trading.get(f"/assets/{symbol}")
    except FAILED as e:
        raise WindowRefused(
            f"Alpaca's asset for {symbol} could not be read ({type(e).__name__}: {e}), so whether it "
            "trades overnight is not known; not starting"
        ) from e
    if not isinstance(asset, dict):
        raise WindowRefused(f"Alpaca's asset for {symbol} could not be read: {asset!r}")
    attributes = asset.get("attributes") or []

    def says(name: str) -> bool:
        return asset.get(name) is True or name in attributes

    if not says("overnight_tradable"):
        raise WindowRefused(
            f"{symbol} is not overnight_tradable on Alpaca's asset; no overnight window runs on it"
        )
    if says("overnight_halted"):
        raise WindowRefused(f"{symbol} is overnight_halted on Alpaca's asset; not starting")
    return "overnight_tradable"


TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
"""Alpaca order statuses after which an order works no more: the four of which its documentation says "no
further updates will occur for the order" (orders-at-alpaca, "Order Lifecycle"). Every other status,
`done_for_day`, `stopped`, `suspended`, `calculated` and `replaced` among them (a replaced order goes on
as another, and no window strategy replaces one), is taken as working: an order "may be canceled through
the API up until" filled, canceled or expired, so the check cancels it, and names it while it is there."""
FAILED = (APIError, OSError, ValueError, TypeError)
"""How a call to Alpaca through alpaca-py fails, as the adapter catches it: Alpaca's own error, an
`OSError` (requests' errors, the adapter's timeouts among them), or an answer alpaca-py's models cannot
read. After any of them but Alpaca's refusal of a submit (`DEFINITIVE_SUBMIT_REFUSALS`: 403 "Buying power
or shares is not sufficient", 422 "Input parameters are not recognized") and a connect timeout on a
call's only attempt (`not_sent`), a close may have been placed."""
CLOSE_ID_PREFIX = "canon-close-"
"""What the client order ID of a close the account check sends begins with; the window's start, a count
and a random part follow, so no two closes share one."""


def _value(field: object) -> str:
    return str(getattr(field, "value", field))


@dataclass
class _Unsettled:
    """One request whose end this window does not yet know: an order of the node's that the node did not
    see end, or a close the account check sent. It is settled only by what Alpaca says: an order under its
    client order ID in a terminal status, or, for a close, Alpaca's refusal of the request itself."""

    client_order_id: str
    side: str
    """`buy` or `sell`; empty when the node's order did not say."""
    own: bool = False
    """A close the check sent, not an order of the node's."""
    order: Any = None
    """Alpaca's order under the ID as last seen, still working; None until Alpaca shows one."""
    asked: bool = False
    """Whether Alpaca has answered a lookup of it at all."""

    def missing(self) -> str:
        what = "close" if self.own else "order"
        sent = "sent with no answer" if self.own else "sent, not known to have ended"
        where = "not found at Alpaca" if self.asked else "not looked up in the check's time"
        return f"{what} {self.client_order_id}: {sent}, {where}"


class _AccountCheck:
    """The account as the check after the stop knows it, and the steps it takes (`_flatten_after_stop`)."""

    def __init__(
        self,
        trading: TradingClient,
        symbol: str,
        report: WindowReport,
        unsettled: Mapping[str, str],
    ) -> None:
        self.trading = trading
        self.symbol = symbol
        self.report = report
        self.requests = {c: _Unsettled(c, side) for c, side in unsettled.items()}
        self.turn = deque(self.requests)  # whose lookup comes next
        self.closes_sent = 0
        self.ended: set[str] = set()  # Alpaca's IDs of the orders it showed in a terminal status
        self.listed: list = []  # the open orders of the last reading
        self.held = Decimal(0)  # the position of the last reading
        self.tried: dict[str, int] = {}  # the cancels sent for each order
        self.open_until: float | None = None  # by the clock's last answer; None before any
        for client_order_id, side in unsettled.items():
            report.unsettled.setdefault(client_order_id, [side or "side not known"])

    def read(self) -> list[str]:
        """One reading: look up to `LOOKUPS` unsettled requests up, then read the open orders, which show
        a request as its lookup does, and the position; return what is left (`left`). A lookup that
        fails is recorded and the reading goes on; a failed read of the orders or the position raises."""
        self._look_up()
        self.listed = open_orders(self.trading, self.symbol)
        for order in self.listed:  # the listing shows a request as its lookup does
            if (request := self.requests.get(str(order.client_order_id))) is not None:
                self._seen(request, order)
        self.held = position_qty(self.trading, self.symbol)
        return self.left()

    def left(self) -> list[str]:
        """What keeps the account from being flat, as last read: each working order, the position, and
        each unsettled request Alpaca has not shown."""
        return described(self.working(), self.held, self.symbol) + [
            r.missing() for r in self.requests.values() if r.order is None
        ]

    def working(self) -> list:
        """Every order known to work: the open orders last listed, and each unsettled request Alpaca
        showed working that the listing lacks (the two need not agree); none Alpaca has shown ended."""
        orders = {str(o.id): o for o in self.listed}
        for request in self.requests.values():
            if request.order is not None:
                orders.setdefault(str(request.order.id), request.order)
        return [o for order_id, o in orders.items() if order_id not in self.ended]

    def _look_up(self) -> None:
        for _ in range(min(LOOKUPS, len(self.turn))):
            request = self.requests[self.turn[0]]
            self.turn.rotate(-1)
            try:
                order = self.trading.get_order_by_client_id(request.client_order_id)
            except FAILED as e:
                if isinstance(e, APIError) and e.status_code == 404:
                    request.asked = True  # as the adapter reads a 404; not in Alpaca's reference
                else:
                    _note(
                        self.report,
                        f"looking up {request.client_order_id}: {type(e).__name__}: {e}",
                    )
                continue
            self._seen(request, order)

    def _seen(self, request: _Unsettled, order: Any) -> None:
        """Alpaca showed `order` under the request's ID: working, it is tracked; terminal, the request is
        settled."""
        request.asked = True
        status = _value(getattr(order, "status", None))
        log = (self.report.closes if request.own else self.report.unsettled)[
            request.client_order_id
        ]
        if log[-1] != status:
            log.append(status)
        if request.own:
            self.report.closed_after_stop = True
        if status in TERMINAL:
            self.ended.add(str(getattr(order, "id", "")))
            self._settle(request)
        else:
            request.order = order

    def _settle(self, request: _Unsettled) -> None:
        del self.requests[request.client_order_id]
        self.turn.remove(request.client_order_id)

    def session_open(self, *, fresh: bool) -> bool:
        """Whether Alpaca's clock says the regular session is open. If the read fails and `fresh` is not
        asked for, the clock's last answer stands: open until the close it gave, closed if it said so; a
        failed read is no evidence the session has closed. With no answer yet, or `fresh`, it raises."""
        asked_at = time.monotonic()
        try:
            clock = self.trading.get_clock()
        except FAILED as e:
            if fresh or self.open_until is None:
                raise
            _note(self.report, f"reading Alpaca's clock: {type(e).__name__}: {e}")
            return time.monotonic() < self.open_until
        assert isinstance(clock, AlpacaClock)
        if clock.is_open:
            self.open_until = asked_at + (clock.next_close - clock.timestamp).total_seconds()
        else:
            self.open_until = asked_at
        return bool(clock.is_open)

    def cancel(self, *, final: bool = False) -> None:
        """Cancel what works, those not yet tried first: `CANCELS` orders in a poll; as the check ends,
        every one, a further cancel starting only while the step has taken under the time `CANCELS - 1`
        requests may, so the step stays inside the `CANCELS` requests counted for it. A close of the
        check's own is left to fill while it still closes the position held and the session is open."""
        unwanted, wanted = [], []
        for order in self.working():
            request = self.requests.get(str(order.client_order_id))
            closes = (
                request is not None
                and request.own
                and self.held
                and request.side == _closing_side(self.held)
            )
            (wanted if closes else unwanted).append(order)
        if wanted:
            try:
                still_open = self.session_open(fresh=False)
            except FAILED as e:
                # no answer of the clock's at all: nothing says the session is open
                _note(self.report, f"reading Alpaca's clock: {type(e).__name__}: {e}")
                still_open = False
            if not still_open:
                unwanted += wanted  # it would only queue for the next session
        when = " as the check ends" if final else ""
        began = time.monotonic()
        unwanted.sort(key=lambda o: self.tried.get(str(o.id), 0))
        for sent, order in enumerate(unwanted):
            in_time = time.monotonic() - began < (CANCELS - 1) * REQUEST_SECS
            if sent >= CANCELS and not (final and in_time):
                break
            self.tried[str(order.id)] = self.tried.get(str(order.id), 0) + 1
            try:
                self.trading.cancel_order_by_id(order.id)
            except FAILED as e:  # it filled or ended meanwhile, or the cancel was lost
                _note(self.report, f"canceling {order.client_order_id}{when}: {e}")

    def act(self) -> bool:
        """One step toward a flat account after a reading; False once nothing is left to do or to look
        for. With an order working, cancel (`cancel`) and send no close: a held order keeps its shares
        from one. With none, close the position if nothing sent and not found could close it too and
        Alpaca's clock, read now, says the session is open."""
        if self.working():
            self.cancel()
            return True
        if not self.held:
            return True  # only requests Alpaca has not shown are left: look again
        side = _closing_side(self.held)
        if waiting := [
            r.client_order_id
            for r in self.requests.values()
            if r.order is None and r.side in (side, "")
        ]:
            _note(
                self.report,
                f"not closing the position while {', '.join(waiting)}, sent and not found at Alpaca, "
                f"may still be placed: a second {side} is not sent",
            )
            return True
        if not self.session_open(fresh=True):
            _note(
                self.report,
                "not closing the position: Alpaca's clock says the regular session is closed, "
                "so a market close would only queue",
            )
            return bool(self.requests)
        self._close(side)
        return True

    def _close(self, side: str) -> None:
        """Send a market order for the held quantity under a client order ID of the check's own, which
        finds it again whatever becomes of the answer. A close that placed nothing (Alpaca's refusal, or
        a connect timeout on its only attempt) is settled at once and counted; any other failure leaves
        it unsettled."""
        qty = abs(self.held)
        self.closes_sent += 1
        client_order_id = (
            f"{CLOSE_ID_PREFIX}{self.report.started}-{self.closes_sent}-{uuid4().hex[:8]}"
        )
        request = _Unsettled(client_order_id, side, own=True)
        self.requests[client_order_id] = request
        self.turn.appendleft(client_order_id)  # looked up first in the next reading
        log = self.report.closes.setdefault(
            client_order_id, [f"sent: {side} {qty} {self.symbol} at market"]
        )
        try:
            order = counted(self.trading.submit_order)(
                MarketOrderRequest(
                    symbol=self.symbol,
                    qty=float(qty),
                    side=AlpacaOrderSide(side),
                    time_in_force=AlpacaTimeInForce.DAY,
                    client_order_id=client_order_id,
                )
            )
        except FAILED as e:
            refused = isinstance(e, APIError) and e.status_code in DEFINITIVE_SUBMIT_REFUSALS
            if refused or not_sent(e):  # Alpaca's own answer, or nothing sent: nothing was placed
                del self.report.closes[client_order_id]
                self.report.closes_not_placed += 1
                self._settle(request)
            elif isinstance(e, APIError):
                log.append(f"no answer: HTTP {e.status_code}")
            else:
                log.append(f"no answer: {type(e).__name__}")
            raise
        if getattr(order, "id", None) is None:
            log.append("no answer: no order in the response")
            return
        self._seen(request, order)


def _closing_side(held: Decimal) -> str:
    return "sell" if held > 0 else "buy"


def _flatten_after_stop(
    trading: TradingClient,
    config: WindowConfig,
    report: WindowReport,
    *,
    unsettled: Mapping[str, str] | None = None,
) -> list:
    """What the account holds in the symbol after the stop, once the check has worked at clearing it.

    The check keeps one set of requests whose end the window does not know: each order of the node's in
    `unsettled` (its client order ID and side; `_unsettled`), and each close the check itself sends. A
    request leaves the set only on Alpaca's word: an order under its client order ID in a terminal status
    (`get_order_by_client_id`), or, for a close, Alpaca's refusal of the request (403, 422). The account
    is flat when no order is open, no position is held and the set is empty; nothing else is recorded ok.

    A poll each `POLL_SECS`, none starting once `AFTER_STOP_SECS` have gone; one begun before runs to its
    end. A poll reads (`_AccountCheck.read`: up to `LOOKUPS` lookups, in turn, then the open orders and
    the position) and, if the account is not flat, acts (`act`): an order that works, listed or shown by
    its lookup, is canceled, on every poll until Alpaca shows it ended, in any session, up to `CANCELS`
    a poll; with none working, a position still held is closed by a market order for the held quantity
    under a client order ID of the check's own, sent only if Alpaca's clock, read just before, says the
    regular session is open. A close's answer that is lost (anything but Alpaca's refusal, or a connect
    timeout that sent nothing) leaves the close unsettled: it is looked up by its ID in its turn, and no
    other close is sent while it, or a node
    order on its side, is sent and not found, since Alpaca may still place it (Alpaca: "You should not
    attempt to resend the order"); one never found is named in the result, with the position. A close
    of the check's own that works is left to fill while it still closes what is held and the session is
    open, and canceled otherwise (it would only queue, or sell what is no longer held).

    Past the deadline: one more reading, then every order still known to work is canceled while the step
    has time (`cancel`), the check's own close too unless the clock, or its last answer if it cannot be
    read, says the session is open. A failed call (`FAILED`) is recorded once and the poll's remaining
    steps are skipped, or, for one lookup or one cancel, the next goes on. The result is the last reading; if the last reading failed, what the
    one before it showed with what the check has sent since; if none succeeded, `RuntimeError`."""
    send_once(trading)
    check = _AccountCheck(trading, config.symbol, report, unsettled or {})
    deadline = time.monotonic() + AFTER_STOP_SECS
    left: list[str] | None = None
    last_error = ""
    more = True
    while more and time.monotonic() < deadline:  # a poll begun before the deadline runs to its end
        try:
            left = check.read()
            if not left:
                return left
            more = check.act()
        except FAILED as e:
            last_error = f"{type(e).__name__}: {e}"
            _note(report, f"checking the account after the stop: {last_error}")
        if not more or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECS)
    if not more and left is not None:
        return left  # nothing was sent after that reading, and nothing is left to do or to look for
    try:
        left = check.read()
        if not left:
            return left
    except FAILED as e:
        last_error = f"{type(e).__name__}: {e}"
        _note(report, f"reading the account as the check ends: {last_error}")
        if left is None:
            raise RuntimeError(
                f"the account could not be read in {AFTER_STOP_SECS} s after the stop: {last_error}"
            ) from e
        left = check.left()
    # The process is ending: nothing the window knows of is left working, but a close that will fill.
    check.cancel(final=True)
    return left


def _note(report: WindowReport, error: str) -> None:
    if error not in report.errors:
        report.errors.append(error)


class Calls(ThreadPoolExecutor):
    """The node's loop's default executor, which `asyncio.to_thread` runs the adapter's REST calls in,
    keeping each call's future so the window can wait for those still running after an abandoned stop."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="canon-window-call")
        self.futures: set[Future] = set()
        self.until: float | None = None
        """When the wait for the calls running at the shutdown ends: one `ADAPTER_CALL_SECS` from it."""

    def shut(self) -> None:
        """No new call starts; the wait for those running ends one `ADAPTER_CALL_SECS` from now, however
        many times it is waited on."""
        self.shutdown(wait=False, cancel_futures=True)
        if self.until is None:
            self.until = time.monotonic() + ADAPTER_CALL_SECS

    def settle(self) -> set[Future]:
        """Wait for the running calls until `until`; return those still running."""
        self.shut()
        assert self.until is not None
        left = max(0.0, self.until - time.monotonic())
        return set(wait(self.running(), timeout=left).not_done) if self.running() else set()

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = super().submit(fn, *args, **kwargs)
        self.futures.add(future)
        future.add_done_callback(self.futures.discard)
        return future

    def running(self) -> set[Future]:
        return {f for f in list(self.futures) if not f.done()}


def _wait_for_calls(abandoned: list, report: WindowReport) -> None:
    """Wait for the worker calls an abandoned node left running, at most `ADAPTER_CALL_SECS` from the
    abandonment (not again if `_run_node` waited already), so an order one of them sends is in the
    account before the check reads it; record any still running."""
    still = set().union(*(calls.settle() for _, _, calls in abandoned))
    if still:
        report.errors.append(
            f"{len(still)} call(s) of the abandoned node still running after {ADAPTER_CALL_SECS:g} s"
        )


def _run_node(node: LiveNode, ends: datetime) -> None:
    """`asyncio.run(_run_until(node, ends))`, except after a stop past `STOP_TIMEOUT_SECS`: closing the
    loop then would cancel the node's task and wait for the same stop, so the loop and the task are left
    (`_ABANDONED`) and `StopTimedOut` raised."""
    runner = asyncio.Runner()
    calls = Calls()
    runner.get_loop().set_default_executor(calls)
    started: list[asyncio.Task] = []
    try:
        stuck = runner.run(_run_until(node, ends, started))
    except BaseException:
        if started and not started[0].done():  # interrupted (Ctrl-C) while the stop still runs
            _abandon(runner, started[0], calls)
        else:  # the node ended with its own error: close as after a clean stop, raise that error
            _close_runner(runner, started[0] if started else None, calls)
        raise
    if stuck is None:
        if not _close_runner(runner, started[0], calls):
            raise StopTimedOut(
                f"a worker call of the node still ran {ADAPTER_CALL_SECS:g} s after it stopped; "
                "left running"
            )
        return
    _abandon(runner, stuck, calls)
    raise StopTimedOut(
        f"the node did not stop within {STOP_TIMEOUT_SECS:g} s of the window's end; left running"
    )


async def _run_until(node: LiveNode, ends: datetime, started: list) -> asyncio.Task | None:
    """Run the node until `ends`, stop it, and wait at most `STOP_TIMEOUT_SECS` for the stop, as the node
    bounds each stage of its own; return the node's task if it is still running then, else None. The
    task is also put in `started`, for the caller to see after an interrupt."""
    task = asyncio.ensure_future(node.run_async())
    started.append(task)
    try:
        await asyncio.wait({task}, timeout=max(0.0, (ends - datetime.now(UTC)).total_seconds()))
    finally:
        if not task.done():
            node.handle().stop()
        await asyncio.wait({task}, timeout=STOP_TIMEOUT_SECS)
    if not task.done():
        return task
    task.result()  # the node's own error, if it ended with one
    return None


def _abandon(runner: asyncio.Runner, task: asyncio.Task | None, calls: Calls) -> None:
    """Leave the runner and the node's task (`_ABANDONED`), with no new worker call able to start."""
    calls.shut()
    _ABANDONED.append((runner, task, calls))


def _close_runner(runner: asyncio.Runner, task: asyncio.Task | None, calls: Calls) -> bool:
    """Close the runner once its worker calls have ended, waiting at most `ADAPTER_CALL_SECS` for them
    (`Runner.close` would wait up to 300 s, `THREAD_JOIN_TIMEOUT`); if one still runs, or the wait is
    interrupted, leave it as after an abandoned stop, and return False for the first."""
    try:
        still_running = calls.settle()
    except BaseException:
        # interrupted in the wait: a worker call may still run, as after a stop that hung
        _ABANDONED.append((runner, task, calls))
        raise
    if still_running:
        _ABANDONED.append((runner, task, calls))
        return False
    runner.close()
    return True


LOCAL_REJECTIONS = ("INFLIGHT_TIMEOUT", "NOT_FOUND_AT_VENUE")
"""The reasons the node gives a rejection of its own, for a submit never acknowledged: the two
`create_reconciliation_rejected` calls in `crates/live/src/execution/manager.rs` at the pin (the in-flight
check out of retries, an order its queries did not find). Alpaca may still take such an order."""


def _event_name(event) -> str:
    """An event's type name; the node's own rejection is named apart (`OrderRejectedLocally`)."""
    found = type(event).__name__
    reason = str(getattr(event, "reason", ""))
    if found == "OrderRejected" and any(r in reason for r in LOCAL_REJECTIONS):
        return "OrderRejectedLocally"
    return found


ANSWERED = frozenset(
    {
        "OrderAccepted",
        "OrderRejected",
        "OrderFilled",
        "OrderCanceled",
        "OrderExpired",
        "OrderDenied",
    }
)
"""The events by which the node has heard what became of an order it sent."""


def _unsettled(cache, strategy_id: StrategyId) -> dict[str, tuple[str, str]]:
    """The strategy's orders the node did not see end, by client order ID, each with its side and why it
    is here: `sent with no answer` of Alpaca's (the adapter logs an unknown outcome and leaves the order
    submitted when a submit's answer is lost, `_log_unknown_outcome`, and the node's own rejection of it
    is no answer), answered and still `open at the node` (not `is_closed` in its cache), or `ended by the
    node's own reconciliation`: an event that carries `reconciliation`, as the cancel or rejection the
    node gives an order it could not find or hear of does (`create_reconciliation_canceled`,
    `create_reconciliation_rejected` at the pin), is the node's reconstruction, not Alpaca's message.
    Alpaca may hold any of them after the node has stopped; the account check looks each up
    (`_flatten_after_stop`)."""
    found: dict[str, tuple[str, str]] = {}
    for order in cache.orders(strategy_id=strategy_id):
        names = [_event_name(e) for e in order.events()]
        if "OrderSubmitted" not in names:
            continue
        if not ANSWERED.intersection(names):
            why = "sent with no answer"
        elif not order.is_closed:
            why = "open at the node"
        elif any(getattr(e, "reconciliation", False) for e in order.events()):
            why = "ended by the node's own reconciliation"
        else:
            continue
        found[order.client_order_id.value] = (order.side.name.lower(), why)
    return found


def _fill(report: WindowReport, cache, strategy_id: StrategyId) -> None:
    """The strategy's orders, by its ID, into the report: each one's events, the fills, the denials."""
    for order in cache.orders(strategy_id=strategy_id):
        names = [_event_name(e) for e in order.events()]
        report.orders[order.client_order_id.value] = names
        report.fills += names.count("OrderFilled")
        for event in order.events():
            if type(event).__name__ in {"OrderDenied", "OrderRejected"}:
                report.denials[order.client_order_id.value] = str(event.reason)


def resolve_session(
    clock: AlpacaClock,
    calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None,
    strategy: str = DEFAULT_STRATEGY,
) -> tuple[str | None, str]:
    """The session `--session auto` runs, from Alpaca's clock and calendar, or None and why not: the
    regular session when the clock says it is open; else the extended one, if `EXTENDED_READY`, it is
    open now and `strategy` runs in it (`WINDOW_STRATEGIES`); else nothing. The schedule fires on a timer
    and leaves this choice to Alpaca's clock."""
    if clock.is_open:
        return "regular", "Alpaca's clock says the regular session is open"
    if "extended" not in (runs := WINDOW_STRATEGIES[strategy]).sessions:
        return None, f"the regular session is closed, and {_runs_only(strategy, runs)}: {runs.why}"
    if not EXTENDED_READY:
        return (
            None,
            "the regular session is closed and extended windows are not on (EXTENDED_READY)",
        )
    try:
        _, part = session_part("extended", clock, calendar)
        window_feed(part)
    except RuntimeError as e:  # `WindowSkipped` among them: a part with no feed (`SESSION_FEEDS`)
        return None, str(e)
    return "extended", f"outside the regular session, in its {part} part"


def main(argv: Sequence[str] | None = None) -> int:
    """`_main`, and after a stop that timed out or was interrupted (`_ABANDONED`), whatever error then
    reached here, the process ended with `os._exit` once the record is written: the node's task would
    otherwise hold the interpreter's exit (module header)."""
    try:
        args = _args(argv)
    except RuntimeError as e:  # no folder for the records: nothing is read or written
        print(f"paper:session: {e}", file=sys.stderr)
        return 1
    code = 1
    try:
        code = _main(args)
    except BaseException as e:
        if not _ABANDONED:
            raise
        traceback.print_exc()  # `os._exit` below would discard it
        try:
            write_heartbeat(args.log_dir, f"failed: {type(e).__name__}: {e}")
        except OSError:
            pass
    finally:
        if _ABANDONED:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code or 1)
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One bounded strategy window on Alpaca paper")
    parser.add_argument("--session", choices=(*SESSIONS, "auto"), default="regular")
    parser.add_argument("--strategy", choices=tuple(WINDOW_STRATEGIES), default=DEFAULT_STRATEGY)
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--log-dir", type=Path, help="default: `default_log_dir()`")
    return parser


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The arguments, `--log-dir` defaulted to `default_log_dir()` only when it is not given, and
    `RuntimeError` for a relative one: the records go outside the repository, wherever the run starts
    (an empty `getconf` in the caller's shell leaves only the folder's name)."""
    args = _parser().parse_args(argv)
    if args.log_dir is None:
        args.log_dir = default_log_dir()
    elif not args.log_dir.is_absolute():
        raise RuntimeError(f"--log-dir must be an absolute folder, got {str(args.log_dir)!r}")
    return args


def _heard(log_dir: Path, outcome: str) -> None:
    """The heartbeat for a run that has said its outcome already; one that cannot be written (the log
    directory may be the trouble) is said on stderr, so the run's own outcome and exit code stand."""
    try:
        write_heartbeat(log_dir, outcome)
    except OSError as e:
        print(f"paper:session: the heartbeat could not be written: {e}", file=sys.stderr)


def _heard_failed(log_dir: Path, error: BaseException) -> None:
    """The heartbeat for a failed run (`_heard`)."""
    _heard(log_dir, f"failed: {type(error).__name__}: {error}")


def _main(args: argparse.Namespace) -> int:
    session = args.session
    try:
        trading = market = None
        market_read_at: float | None = None
        if session == "auto":
            trading = paper_trading()
            clock, calendar, market_read_at = session_now(trading)
            market = (clock, calendar)
            resolved, why = resolve_session(clock, calendar, args.strategy)
            if resolved is None:
                print(f"paper:session: nothing to run: {why}")
                _heard(args.log_dir, f"nothing to run: {why}")
                return 0
            session = resolved
        config = WindowConfig(
            symbol=args.symbol,
            session=session,
            minutes=args.minutes,
            log_dir=args.log_dir,
            strategy=args.strategy,
        )
        write_heartbeat(args.log_dir, f"a {session} window of {args.strategy}")
        report = run_window(config, trading=trading, market=market, market_read_at=market_read_at)
    except WindowSkipped as e:
        print(f"paper:session: skipped: {e}")
        _heard(args.log_dir, f"skipped: {e}")
        return 0
    except WindowRefused as e:
        print(f"paper:session: {e}", file=sys.stderr)
        _heard(args.log_dir, f"refused: {e}")  # the refusal stands, said above
        try:
            write_record(
                args.log_dir,
                {
                    "session": session,
                    "strategy": args.strategy,
                    "result": f"refused: {e}",
                    "started_epoch": time.time(),
                    "started": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
                    "stale_after": STALE_RUNNING_SECS,
                    "commit": running_commit(),
                },
            )
        except OSError as written:
            print(f"paper:session: the record could not be written: {written}", file=sys.stderr)
        return 1
    # KeyError: no key pair in the env; OSError: the HTTP layer's errors, the adapter's timeouts among them
    except (RuntimeError, ValueError, KeyError, APIError, OSError) as e:
        print(f"paper:session: {e}", file=sys.stderr)
        _heard_failed(args.log_dir, e)
        return 1
    except Exception as e:  # anything else: heard as failed, then raised with its traceback
        _heard_failed(args.log_dir, e)
        raise
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
