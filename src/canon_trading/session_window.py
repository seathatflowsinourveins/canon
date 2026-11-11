"""One bounded window of a named strategy on Alpaca's paper environment: start flat, trade, flatten, check, report.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveNode.add_builtin_strategy`, `Cache.quote_count`
and `trade_count`, `LiveExecutionEngineConfig` and `LoggerConfig` with a `FileWriterConfig`
(`live/__init__.pyi`, `common/__init__.pyi`); alpaca-py 0.45.0's `TradingClient` (paper)
`get_clock`, `get_calendar`, `get_orders`, `get_all_positions`, `get_asset`, `get_order_by_client_id`,
`cancel_order_by_id` and `submit_order` with a `MarketOrderRequest`, and `get_account_configurations` and
`set_account_configurations` with an `AccountConfiguration`; for a mover window, `ScreenerClient`
`get_market_movers`, `CorporateActionsClient.get_corporate_actions`, `StockHistoricalDataClient`
`get_stock_latest_trade`, `get_stock_snapshot` and `get_stock_bars`, and `StockDataStream.subscribe_bars`
(2026-11-11, ADR 0083). Read 2026-11-10 (ADR 0044; the account
check's sources, ADR 0060), and the long-only sources 2026-11-11:

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
* alpaca-py 0.45.0, `trading/models.py` `AccountConfiguration` ("no_shorting (bool): If true then Account
  becomes long-only mode") and `trading/client.py` (`get_account_configurations`, `GET
  /account/configurations`; `set_account_configurations`, a `PATCH` of `to_patch_payload()`, the whole
  configuration); https://docs.alpaca.markets/us/reference/patchaccountconfig-1.md ("If true, account
  becomes long-only mode", served from the paper host too). What Alpaca answers an order that would sell
  short on such an account is not on either page (UNVERIFIED until `tests/test_paper_long_only.py` runs).
* https://docs.alpaca.markets/us/reference/postorder.md: 403 "Buying power or shares is not sufficient",
  422 "Input parameters are not recognized". Not documented there or in the pages above: what Alpaca
  answers a lookup of an ID it does not hold (a 404, as the adapter's lookup reads it; UNVERIFIED), and
  how soon after a submit an order shows in a lookup or a listing.

Long-only (the lead's rule, 2026-11-11): a window never opens a short; a sell is allowed only against a
long it holds. It is held at the venue: the start check reads Alpaca's account configuration and refuses,
by name, an account whose `no_shorting` is not true (`VENUE_LONG_ONLY`; `paper:long-only` sets it, by
alpaca-py's documented call, and reverses it by the same call). And the table refuses by name a
strategy whose shipped behaviour sells while flat (`WindowStrategy.sells_while_flat`), so a node where the
setting is somehow off still holds none: upstream's `EmaCross` is one.

What one window is (one node, one process): one strategy of `WINDOW_STRATEGIES` that the table does not
refuse: `SessionMaCross` (upstream's built-in `EmaCross` stays in the table, ADR 0068 step 3, and is
refused by name for selling while flat, "Long-only" above); the configured session and feed, checked against Alpaca's
clock and calendar before anything starts; the paper account holding no open order and no position in any
symbol (`leftovers`); one share of the symbol, at its latest sip trade, inside the per-order notional cap
(`one_share_fits`); at least `GAP_SECS` since the last window ended; then the node runs for `minutes` (default 30, at
most 60), clipped to the session's end and to `LONGEST_WINDOW_SECS` (about 49 minutes, a bound set when a
streaming paper test waited for the window's stream lock, which since 2026-11-11 is account B's alone), the record saying so when it is. `SessionMaCross` winds down `FLATTEN_SECS` before the end (`EmaCross` has
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

An extended window runs in each part of the extended session that `SESSION_FEEDS` gives a feed: overnight on
`boats`, pre-market and after-hours on `sip` (a part given none is skipped, naming the observation). A pre-market window's warm-up
reaches back into the prior session (`warm_up_start`): NautilusTrader's `Strategy.request_bars` takes any
`start`, and the adapter pages Alpaca's bars from it to the end it is given (`canon_alpaca.data`).
The overnight session is the one Alpaca's 24/5 page
(https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md) puts from 8:00 PM to 4:00 AM ET "on
the evening before the trade date", following the NYSE holiday calendar, with its data on `boats` or
`overnight` and its eligible assets marked `overnight_tradable` in the Assets API: so an overnight window
runs only before a trade date on Alpaca's calendar, streams `boats` (`SESSION_FEEDS`, ADR 0055), and starts only on
an asset so marked and not `overnight_halted`. What reached the strategy (quotes, trades, live bars) and
the adapter's own lines about its stream, from the node's log, go in the record; a stream that delivered
nothing, or that the adapter logged an error for, fails the window, since the adapter reports a refused
or ended stream only there (ADR 0055).

A mover window (`--mover`, ADR 0083's dated line of 2026-11-11) is a one-day bridge and a check of the
stack on a mover (its spreads, halts and feed), not the practice's strategy: the same `SessionMaCross`, one
share, in the regular session only, streaming `sip`, with every order in the strategy's limit form (a
marked DAY limit through the touch, `WindowConfig.limits`), since an entry is never a market order; the
account check after the stop may still close at market in the session. Its name is chosen once before the
node is built (`choose_mover`) and loaded by NautilusTrader's instrument provider at connect
(`InstrumentProviderConfig(load_ids=...)`, `paper_node.alpaca_paper_builder`). The name comes from
`stream_mover`: a `SAMPLE_MINUTES` sample of the whole market's sip minute bars on the window's own pair
(alpaca-py's `StockDataStream.subscribe_bars(handler, "*")`, as `movers_stream` streams them), ranked by
`movers_stream.rank` on `movers_stream.refresh`'s references and normals. If the sample fails or qualifies
no name, `select_mover` takes Alpaca's gainers (alpaca-py's `ScreenerClient.get_market_movers`), ranked the
same way on `get_stock_snapshot`, `get_stock_bars` and `read_normals`, and the record says why. Either way
the pick is the first that `TradingClient.get_asset` gives `status`
active and `tradable` and a name that is not a warrant, unit or right (`NOT_COMMON`), with no corporate
action since the last session (`get_corporate_actions`), a sip trade inside `FRESH_TRADE_SECS` (not halted,
by a proxy: alpaca-py reads no halt over REST) and one share inside the cap. The start check and the check
after the stop read the whole account (`get_orders` with no symbol, `get_all_positions`); the check acts on
the window's symbol only and names anything else as left.

Paper only: the execution client is `canon_alpaca`'s, whose config refuses anything but paper trading, and
the alpaca-py client here is built with `paper=True`. The key pair comes from the environment, which
`mise run paper:session` fills as `paper:test` does (ADR 0044, superseding ADR 0029's "only" clause). This evaluates nothing (ADR 0010).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS
from alpaca.common.constants import DEFAULT_RETRY_WAIT_SECONDS
from alpaca.common.exceptions import APIError
from alpaca.common.rest import RESTClient
from alpaca.data.enums import DataFeed
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models.bars import BarSet
from alpaca.data.models.corporate_actions import CorporateActionsSet
from alpaca.data.requests import CorporateActionsRequest
from alpaca.data.requests import MarketMoversRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.requests import StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import AccountConfiguration
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
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import counted
from canon_alpaca.providers import not_sent
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import LogLevel
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.config import FileWriterConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce

from .mover import Mover
from .mover import entry_price
from .mover_window import ENTRY_NOTIONAL_USD
from .mover_window import MOVER
from .mover_window import MOVER_ID
from .mover_window import OUTER_BOUND_USD
from .mover_window import mover_parts
from .movers_screener import MOVERS_TOP
from .movers_screener import movers_line
from .movers_stream import STREAM_LOGGER
from .movers_stream import Reads
from .movers_stream import Tape
from .movers_stream import Warnings
from .movers_stream import epoch_seconds
from .movers_stream import minute_dollars
from .movers_stream import minute_start
from .movers_stream import rank
from .movers_stream import read_normals
from .movers_stream import reference_close
from .movers_stream import refresh
from .paper_node import DELAY_POST_STOP_SECS
from .paper_node import EMA_CROSS
from .paper_node import EMA_CROSS_ID
from .paper_node import MAX_NOTIONAL_PER_ORDER_USD
from .paper_node import MAX_ORDER_SUBMIT_RATE
from .paper_node import TIMEOUT_DISCONNECTION_SECS
from .paper_node import TIMEOUT_SHUTDOWN_SECS
from .paper_node import alpaca_paper_builder
from .paper_node import ema_cross_config
from .position_cap import PositionCap
from .position_cap import position_cap_config
from .session_ma_cross import LIMIT_OFFSET
from .session_ma_cross import WARM_UP_SPARE_BARS
from .session_ma_cross import SessionMaCross
from .session_ma_cross import SessionMaCrossConfig
from .session_ma_cross import through_the_touch

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.common import Cache

    from .movers_stream import Stream

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
        DataFeed.SIP.value,
        "sip delivered on paper in pre-market: a read-only probe on account A, 08:23 to 08:26 New York, "
        "2026-11-11, received 1,167 SPY quotes, 240 trades and 3 bars, where iex, 08:26 to 08:36, sent 5 SPY "
        "trades and 2 bars; iex and boats sent nothing at 04:13 to 04:58, before IEX Exchange's 08:00 "
        "pre-market open. The warm-up reaches back into the prior session (`warm_up_start`, ADR 0044)",
    ),
    "after-hours": SessionFeed(
        DataFeed.SIP.value,
        "iex, overnight and boats each sent no quote or trade in 45 s on four symbols at 16:46 New York, "
        "2026-11-10 (ADR 0055); the sip feed authenticated on paper on 2026-11-10, and authenticated and "
        "delivered on 2026-11-11 (pre-market's row); after-hours has not yet been run on it, so its windows "
        "are the probe",
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
evening and every morning by design; a part not yet probed on its feed runs, and its windows are the probe
(after-hours, and pre-market before 08:00, on `sip`); inside a part with a feed, a
window that receives nothing still fails (`_data_failure`). The feed is named in the subscriptions' and the bars
request's params, `{"feed": ...}` (ADR 0055); one not served fails loudly, never another."""
CALENDAR_BACK = timedelta(days=7)
"""How far back the calendar is read: past any run of weekend and holiday days before a trading day."""
DEFAULT_MINUTES = 30
MAX_MINUTES = 60
GAP_SECS = 300
"""Between one window's end and the next one's start."""
FLATTEN_SECS = 120
"""`SessionMaCross` winds down this long before the window ends. A built-in has no wind-down: `EmaCross`
trades until the stop, and an order it sent then is the account check's to settle (ADR 0060)."""
END_TICK_SECS = 1.0
"""How often the node's run reads the wall clock against the window's end (`_run_until`)."""
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
    max_notional_per_order: Decimal = MAX_NOTIONAL_PER_ORDER_USD
    """The risk engine's per-order notional cap for its node (`paper_node.risk_limits`)."""
    sells_while_flat: str = ""
    """Why its shipped behaviour sells while flat, if it does: such a strategy is refused by name in
    every window, since a window never opens a short (module header, "Long-only")."""


WINDOW_STRATEGIES = {
    "SessionMaCross": WindowStrategy(STRATEGY_ID, SESSIONS),
    EMA_CROSS: WindowStrategy(
        EMA_CROSS_ID,
        ("regular",),
        "it does not flatten on stop, and the account check after the stop closes at market only "
        "while the regular session is open (ADR 0068)",
        EMA_CROSS_SUBMIT_RATE,
        builtin=EMA_CROSS,
        sells_while_flat=(
            "at the pin it sends a market order of `trade_size` at each cross whatever it holds, so a "
            "cross down while flat opens a short (on paper on 2026-11-11 it sold 1 SPY while flat and "
            "held the short for the whole window)"
        ),
    ),
    MOVER: WindowStrategy(
        MOVER_ID,
        ("regular",),
        "its first window is the regular session's (ADR 0088); outside it every order of its is a marked "
        "limit, which has not run on paper yet",
        max_notional_per_order=OUTER_BOUND_USD,
    ),
}
"""`Mover` (ADR 0088) runs only in a mover window (`--mover`), on the one name `choose_mover` gives, which
`mover_window.FixedRank` publishes as the ranking; it sizes its own entries (`mover_window`'s rows), so its
node's per-order cap is the outer bound and its position cap the shares the largest entry buys.

The strategies a window runs, by the name `--strategy` takes (ADR 0068, step 3): the repo's
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


VENUE_LONG_ONLY = True
"""Whether the start check refuses an account that Alpaca does not hold long-only (`no_shorting` false).
On from the landing that added it: the owner approved on 2026-11-11 setting `no_shorting` on both paper
accounts (`paper:long-only`). Off, the check only records what it read (report mode, the state before the
setting is made)."""
STALE_RUNNING_SECS = 70 * 60
"""A record still `running` this long after it started is a window that died (60 minutes at most, plus
start-up, shutdown and the account check)."""
EXTENDED_READY = True
"""Whether extended windows may run: pre-market, after-hours and overnight, each where `SESSION_FEEDS` gives
it a feed (every part has one; a part given none is skipped). The adapter sends
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
ACCOUNT_ENV = "CANON_PAPER_ACCOUNT"
"""The paper account a run's task read the pair of, by the name `paper:check` gives it: `windows` (account B),
set by `paper:session` beside its reads of B's pair (`mise.toml`; ADR 0069). A name, never a key."""
PAPER_ACCOUNTS = ("windows", "tests")
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
"""How long a paper test that streams waits for account A's market-data stream lock (the conftests'
`STREAM_LOCK_WAIT_SECS`; a window holds account B's, not that one, since 2026-11-11), and how long
`mise run paper:test` waits for account A's lock (`lockwait` in `mise.toml`); no window takes that lock,
since windows run on account B under their own (ADR 0069). Tests read both, so the three cannot drift."""
START_UP_SECS = 30
"""The process's start before Alpaca's clock is first read (`uv run`, the imports)."""
LONGEST_WINDOW_SECS = math.floor(
    PAPER_LOCK_WAIT_SECS - START_UP_SECS - ADAPTER_CALL_SECS - SKEW_SECS - AFTER_END_SECS
)
"""The longest a window runs (about 49 minutes): sized so that a paper test queued behind it for one shared
stream lock would get the lock within its wait (`PAPER_LOCK_WAIT_SECS`); each account's pair has had its own
stream lock since 2026-11-11 (ADR 0069), and the bound is kept as it was. It counts the start-up, the first
clock read (with alpaca-py's retries, `ADAPTER_CALL_SECS`), the window
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


def paper_account() -> str:
    """The paper account a record names: `ACCOUNT_ENV` when it is one of `PAPER_ACCOUNTS`, else `unknown`.
    Any other value is not echoed, so nothing the variable held by mistake reaches a record."""
    name = os.environ.get(ACCOUNT_ENV, "")
    return name if name in PAPER_ACCOUNTS else "unknown"


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
    mover: bool = False
    """Run on the name `choose_mover` gives at the start, in place of `symbol` (`MOVER_FEED`, regular
    session only); `symbol` is then the one chosen."""

    def __post_init__(self) -> None:
        if self.session not in SESSIONS:
            raise ValueError(f"session must be one of {SESSIONS}, got {self.session!r}")
        if (runs := WINDOW_STRATEGIES.get(self.strategy)) is None:
            raise ValueError(
                f"strategy must be one of {tuple(WINDOW_STRATEGIES)}, got {self.strategy!r}"
            )
        if runs.sells_while_flat:
            raise ValueError(
                f"{self.strategy} sells while flat: {runs.sells_while_flat}; a window never opens a "
                "short (long-only)"
            )
        if self.session not in runs.sessions:
            raise ValueError(
                f"{_runs_only(self.strategy, runs)}, not in the {self.session} session: {runs.why}"
            )
        if self.mover and self.session != "regular":
            raise ValueError(
                "a mover window runs in the regular session only (marked DAY limits on sip), not in "
                f"the {self.session} session"
            )
        if self.strategy == MOVER and not self.mover:
            raise ValueError(
                f"{MOVER} trades the name the ranking gives, not a given symbol: run it with --mover"
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
    def limits(self) -> bool:
        """Whether every order of `SessionMaCross` is its marked DAY limit through the touch: outside the
        regular session, and in a mover window in any session, since an entry is never a market order
        (ADR 0083's dated line of 2026-11-11)."""
        return self.session == "extended" or self.mover

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


def warm_up_start(
    part: str,
    now: datetime,
    calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None,
    *,
    span: timedelta,
) -> datetime | None:
    """Where a window's warm-up starts when its own part of the session is too thin for the slow average:
    in pre-market, `span` before the regular close of the last trading day before `now`'s (Alpaca's
    calendar), so the request reaches back into the prior session, as `request_bars` takes any `start`
    (`SessionMaCross._warm_up_from`); None elsewhere, which warms up on the strategy's own span.
    `RuntimeError` if the calendar holds no earlier trading day."""
    if part != "pre-market":
        return None
    today = in_new_york(now).date()
    days = _trading_days(calendar)
    if not (earlier := [d for d in days if d < today]):
        raise RuntimeError(
            f"no trading day before {today} on the calendar read, so the pre-market warm-up "
            "cannot reach back into the prior session"
        )
    return in_new_york(days[max(earlier)].close) - span


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
    config: WindowConfig,
    *,
    wind_down_at: datetime,
    log_name: str,
    feed: str,
    warm_up_start: datetime | None = None,
    pick: MoverPick | None = None,
    ends: datetime | None = None,
) -> tuple[LiveNode, SessionMaCross | Mover | CachedData, PositionCap]:
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
    limits = config.limits
    extended = config.session == "extended"
    # `SessionMaCross`'s managed stop runs a market exit inside the post-stop delay; a built-in has none.
    # `Mover`'s orders in the session are unmarked, so its stop there is the managed market exit too.
    managed = (not extended) if config.strategy == MOVER else (runs.builtin is None and not limits)
    node = (
        alpaca_paper_builder(
            "CANON-PAPER-SESSION",
            config.instrument_id,
            # the managed stop's market exit runs inside the post-stop delay
            delay_post_stop_secs=MANAGED_DELAY_POST_STOP_SECS if managed else DELAY_POST_STOP_SECS,
            max_order_submit_rate=runs.max_order_submit_rate,
            max_notional_per_order=runs.max_notional_per_order,
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
    if config.strategy == MOVER:
        if pick is None or ends is None:
            raise RuntimeError(
                f"{MOVER} is built on the window's choice and its end; none was given"
            )
        mover, ranking, cap = mover_parts(
            config.instrument_id,
            price=pick.price,
            move=Decimal(str(round(float(pick.record["inputs"]["move"]), 6))),
            computed_at=datetime.fromisoformat(pick.record["at"]),
            ends=ends,
            extended=extended,
            feed=feed,
            wind_down_at=wind_down_at,
            market_exit_interval_ms=MARKET_EXIT_INTERVAL_MS,
            market_exit_max_attempts=MARKET_EXIT_MAX_ATTEMPTS,
        )
        node.add_strategy(mover)
        node.add_actor(ranking)
        node.add_strategy(cap)
        return node, mover, cap
    strategy = SessionMaCross(
        SessionMaCrossConfig(
            bar_type=config.bar_type,
            trade_size=Quantity.from_int(1),
            fast_period=config.fast_period,
            slow_period=config.slow_period,
            extended_hours=limits,  # every order a marked DAY limit through the touch
            feed=feed,
            wind_down_at=wind_down_at,
            warm_up=True,  # the averages start from the adapter's last bars (ADR 0047)
            warm_up_start=warm_up_start,  # in pre-market, the prior session's (`warm_up_start`)
            strategy_id=STRATEGY_ID,
            order_id_tag="001",
            # In the session, `stop()` runs upstream's market exit, with closing orders Alpaca takes.
            manage_stop=not limits,
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
    account: str = field(default_factory=paper_account)
    """The paper account the window ran on (`paper_account`), in the `running` record and the result alike."""
    no_shorting: bool | None = None
    """Alpaca's `no_shorting` for the account, read by the start check (`venue_no_shorting`): true, the
    account is long-only at the venue."""
    share_price: str = ""
    """One share's latest sip price at the start, which must fit the per-order cap (`one_share_fits`)."""
    mover: dict[str, Any] = field(default_factory=dict)
    """For a mover window, its choice (`stream_mover`, or `select_mover` as the fallback): the source,
    the symbol, its rank inputs, the names ranked and passed over with why, and the screener's list as
    Alpaca gave it."""
    requests_by_api: list[dict[str, Any]] = field(default_factory=list)
    """What the process sent to Alpaca's REST APIs by the window's end, per account and API, as the
    adapter counts it (`canon_alpaca.providers.request_counts`); the account is a number, never a key."""
    fill_prices: dict[str, list[str]] = field(default_factory=dict)
    """Each fill of the strategy's orders, by client order ID: `qty@price` and the venue's time
    (`OrderFilled`'s `last_qty`, `last_px`, `ts_event`). A close the account check sends after the stop is
    not the strategy's: its outcome is in `closes`, its fill price is not recorded (Muse's finding 5)."""
    submit_delays_ms: dict[str, int] = field(default_factory=dict)
    """Per order, from the strategy's submit to the node's receipt of Alpaca's acceptance: `OrderAccepted`'s
    `ts_init` less the order's first event's (`OrderInitialized`), both on the node's clock. An order denied
    or rejected before Alpaca accepted it has none; it is in `denials` (Muse's finding 5)."""
    worst_submit_delay_ms: int | None = None


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


def write_atomically(path: Path, body: str) -> None:
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
        write_atomically(log_dir / f"window-{report.started}.json", body)
    write_atomically(log_dir / "latest.json", body)


def write_heartbeat(log_dir: Path, outcome: str) -> None:
    """Write `last-firing.json` on every run, a window or not, a run by hand included: status shows its
    outcome, and `scheduled` says whether the schedule started it (`SCHEDULED_ENV`, or the runner's
    `COMMIT_ENV` without `BY_HAND_ENV`, as a runner installed before `SCHEDULED_ENV` sets it). Whether the schedule
    still fires, status reads from the runner's own stamp, `scheduled-firing.json`, which no run by hand
    writes; a run by hand writes to its own folder (`mise.toml`, paper:session; ADR 0050)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    write_atomically(
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
    """Alpaca's clock and the calendar's days from a week before today to tomorrow (New York; a day that is not a
    trading day is absent), read once per run, and the `time.monotonic()`
    at which the clock's answer arrived: its timestamp is as old as the time since, whatever the read's
    own retries took before it."""
    clock = trading.get_clock()
    read_at = time.monotonic()
    assert isinstance(clock, AlpacaClock)
    today = clock.timestamp.astimezone(NEW_YORK).date()
    # the next day too: an overnight session that starts at 20:00 belongs to the next trading day; and
    # the week before, for the last trading day's close a pre-market warm-up reaches back to
    days = trading.get_calendar(
        GetCalendarRequest(start=today - CALENDAR_BACK, end=today + timedelta(days=1))
    )
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


def open_orders(trading: TradingClient) -> list:
    """Every open order in the account, whatever its symbol."""
    orders = trading.get_orders(
        # the most one listing returns ("Defaults to 50 and max is 500"), as the adapter asks
        GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT)
    )
    assert isinstance(orders, list)
    return orders


def positions(trading: TradingClient) -> dict[str, Decimal]:
    """The signed quantity Alpaca holds in each symbol of the account (alpaca-py's `get_all_positions`,
    `GET /v2/positions`); a symbol with none is absent."""
    found = trading.get_all_positions()
    assert isinstance(found, list)
    held: dict[str, Decimal] = {}
    for position in found:
        assert isinstance(position, AlpacaPosition)
        qty = abs(Decimal(str(position.qty)))
        short = getattr(position.side, "value", position.side) == "short"
        held[position.symbol] = -qty if short else qty
    return held


def venue_no_shorting(trading: TradingClient) -> bool:
    """Alpaca's `no_shorting` for the account (alpaca-py's `get_account_configurations`): true, the
    account is long-only (module header, "Long-only")."""
    configuration = trading.get_account_configurations()
    assert isinstance(configuration, AccountConfiguration)
    return configuration.no_shorting


LONG_ONLY_ACTIONS = ("check", "on", "off")


def long_only_task(action: str, trading: TradingClient | None = None) -> int:
    """`mise run paper:long-only <account> <action>`: `check` prints the account's `no_shorting`; `on`
    and `off` set it through alpaca-py's `set_account_configurations`, sending back the configuration as
    read with only `no_shorting` changed, and print it before and after. Prints the setting's value and
    nothing else; exits 1 naming any other field Alpaca's answer changed, and if the setting did not
    take."""
    if action not in LONG_ONLY_ACTIONS:
        raise ValueError(f"the action is check, on or off, got {action!r}")
    trading = trading or paper_trading()
    if action == "check":
        print(f"no_shorting: {str(venue_no_shorting(trading)).lower()}")
        return 0
    before = trading.get_account_configurations()
    assert isinstance(before, AccountConfiguration)
    if before.no_shorting is (action == "on"):  # nothing to change: no PATCH
        print(f"no_shorting: {str(before.no_shorting).lower()} (unchanged)")
        return 0
    after = trading.set_account_configurations(
        before.model_copy(update={"no_shorting": action == "on"})
    )
    assert isinstance(after, AccountConfiguration)
    print(f"no_shorting: {str(before.no_shorting).lower()} -> {str(after.no_shorting).lower()}")
    was, now = before.model_dump(), after.model_dump()
    changed = sorted(k for k in was if k != "no_shorting" and was[k] != now.get(k))
    failed = 0
    if changed:
        print(
            f"paper:long-only: Alpaca's answer changed other fields too: {changed}", file=sys.stderr
        )
        failed = 1
    if after.no_shorting is not (action == "on"):
        print("paper:long-only: Alpaca's answer does not carry the setting", file=sys.stderr)
        failed = 1
    return failed


def leftovers(trading: TradingClient) -> list[str]:
    """What the whole account holds: each open order and each position, whatever its symbol."""
    return elsewhere(open_orders(trading), positions(trading))


def described(orders: list, qty: Decimal, symbol: str) -> list[str]:
    """What is left in the window's symbol, as its record names it: each open order, then the position."""
    left = [f"open order {o.client_order_id}" for o in orders]
    if qty:
        left.append(f"position of {qty} {symbol}")
    return left


def elsewhere(orders: list, held: Mapping[str, Decimal]) -> list[str]:
    """What is left in symbols other than the window's, each named with its symbol: the account is not
    flat while any is there, and the window sends nothing for one, since it is not the window's."""
    return [f"open order {o.client_order_id} in {o.symbol}" for o in orders] + [
        f"position of {qty} {symbol}" for symbol, qty in held.items() if qty
    ]


def window_seconds(minutes: int, session_left: float) -> tuple[float, str]:
    """How long a window of `minutes` runs with `session_left` seconds of its session to go, and why it
    is shorter, if it is: it ends `END_MARGIN_SECS` before the session does, and runs at most
    `LONGEST_WINDOW_SECS`."""
    asked = minutes * 60
    by_session = session_left - END_MARGIN_SECS
    seconds = min(asked, LONGEST_WINDOW_SECS, by_session)
    if seconds >= asked:
        return seconds, ""
    if seconds == by_session:
        why = f"ends {END_MARGIN_SECS} s before the session does"
    else:
        why = (
            f"at most {LONGEST_WINDOW_SECS} s, the longest window, so it and its bounded end fit a "
            f"{PAPER_LOCK_WAIT_SECS} s wait"
        )
    return seconds, f"shortened from {minutes} minutes to {seconds:.0f} s: {why}"


NOT_COMMON = re.compile(r"\b(warrants?|units?|rights?)\b", re.IGNORECASE)
"""What in Alpaca's asset `name` marks a warrant, a unit or a right, which a mover window passes over; common
shares and depositary receipts are kept. A proxy with no upstream behind it: alpaca-py 0.45.0's `Asset` and
Alpaca's assets page give `class` `us_equity` and no security type."""
FRESH_TRADE_SECS = 120
"""A mover's latest sip trade must be at most this old at its selection (UNCALIBRATED): a proxy for not
halted, with no upstream behind it. alpaca-py 0.45.0 reads no halt over REST (`Asset` has `status` and
`tradable`; Alpaca's asset `attributes` has `overnight_halted` only), and halts come on the stream's
statuses channel (`subscribe_trading_statuses`), whose page does not say a subscription is sent the current
status (ADR 0083, gap 4)."""
MOVER_CHECKS = 10
"""The ranked names checked, best first, before a mover window is refused: each priced and fresh one costs
one asset read."""
MOVER_FEED = DataFeed.SIP.value
"""What a mover window streams: the consolidated feed the ranking reads (`movers_stream`, ADR 0083)."""
MOVER_SOURCE = "screener"
"""`select_mover`'s source: Alpaca's movers (`movers_screener`), `choose_mover`'s fallback when the stream
sample fails or qualifies no name (ADR 0083's dated line of 2026-11-11)."""
STREAM_SOURCE = "stream"
"""`stream_mover`'s source: a short sample of the whole market's sip minute bars on the window's own pair."""
SAMPLE_MINUTES = 3
"""Clock minutes of bars the stream sample ranks on (UNCALIBRATED; the lead's 2026-11-11 direction: two or
three). The sample's time counts against the window, as every start-up read does."""
BAR_GRACE_SECS = 10.0
"""How long after a minute ends the sample waits for that minute's bars (UNCALIBRATED). A symbol whose
last-minute bar comes later is left out of the ranking, as one missing a minute is (a limit: which names a
late delivery drops depends on the order Alpaca sends them in)."""
SAMPLE_STOP_SECS = 15.0
"""How long the sample's stream thread may take to end after `stop()` (`movers_stream.STOP_SECS`)."""
REFRESH_PASSES = 4
"""`movers_stream.refresh` calls after the sample: each reads up to 4,000 references and 200 normals."""
REFRESH_SECS = 60.0
"""The longest one `refresh` pass starts reads for."""


class StreamSampleFailed(RuntimeError):
    """The stream sample could not rank: the stream stopped or failed, or no bar came."""


@dataclass(frozen=True)
class MoverPick:
    """The name a mover window runs on, its latest sip price, and what its record keeps of the choice."""

    symbol: str
    price: Decimal
    record: dict[str, Any]
    entry: Decimal
    """The entry limit one share would be sent at (`entry_limit`), which the cap is checked against."""


def window_data() -> StockHistoricalDataClient:
    """alpaca-py's market data client on the environment's pair, with the adapter's HTTP timeout
    (ADR 0039); paper and live keys read the same market data API."""
    return with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    )


def window_actions() -> CorporateActionsClient:
    """alpaca-py's corporate actions client on the environment's pair, with the adapter's HTTP timeout."""
    return with_http_timeout(
        CorporateActionsClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    )


def window_screener() -> ScreenerClient:
    """alpaca-py's screener client on the environment's pair, with the adapter's HTTP timeout."""
    return with_http_timeout(
        ScreenerClient(api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY])
    )


def share_price(data: StockHistoricalDataClient, symbol: str) -> Decimal:
    """`symbol`'s latest sip trade price (alpaca-py's `get_stock_latest_trade`), or `WindowRefused`."""
    found = data.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.SIP)
    )
    if not isinstance(found, dict) or (trade := found.get(symbol)) is None:
        raise WindowRefused(
            f"Alpaca gave no latest sip trade for {symbol}, so whether one share fits the cap is not "
            "known; not starting"
        )
    return Decimal(str(trade.price))


def one_share_fits(symbol: str, price: Decimal) -> None:
    """`WindowRefused` unless one share at `price` fits the risk engine's per-order notional cap
    (`paper_node.MAX_NOTIONAL_PER_ORDER_USD`), which every window's order of one share must pass."""
    if price > MAX_NOTIONAL_PER_ORDER_USD:
        raise WindowRefused(
            f"one share of {symbol} at {price} USD does not fit the per-order notional cap of "
            f"{MAX_NOTIONAL_PER_ORDER_USD} USD; not starting"
        )


def select_mover(
    trading: TradingClient,
    data: StockHistoricalDataClient,
    screener: ScreenerClient,
    actions: CorporateActionsClient,
    at: datetime,
    regular_open: datetime,
    since: date,
) -> MoverPick:
    """The top name of the movers ranking at `at` among Alpaca's gainers that Alpaca reports active and
    tradable, whose latest sip trade is inside `FRESH_TRADE_SECS`, whose one share fits the cap, which is
    not a warrant, a unit or a right (`NOT_COMMON`), and which has no corporate action from `since`, the
    last completed session, to today (alpaca-py's `get_corporate_actions`), since one would change what its
    move is measured from; or `WindowRefused` naming why each checked name was passed over.

    Every input is an upstream read through alpaca-py 0.45.0: the gainers from `get_market_movers` (top
    50), each one's last completed close and latest trade from `get_stock_snapshot`, today's minute bars
    from the regular open from `get_stock_bars`, and its normal from `movers_stream.read_normals`; the
    score is `movers_stream.rank`'s, `move x dvol_ratio` (UNCALIBRATED, ADR 0083 gap 1)."""
    elapsed = int((at - regular_open).total_seconds() // 60)
    if elapsed < 1:
        raise WindowRefused("no minute of the regular session has passed to rank a mover on")
    listed = movers_line(screener.get_market_movers(MarketMoversRequest(top=MOVERS_TOP)))
    gainers = cast("list[dict[str, Any]]", listed["gainers"])  # `Mover.model_dump()`, in order
    symbols = [str(g["symbol"]) for g in gainers]
    if not symbols:
        raise WindowRefused("Alpaca's movers listed no gainer; no mover window runs")
    snapshots = data.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.SIP)
    )
    if not isinstance(snapshots, dict):
        raise TypeError("alpaca-py returned raw snapshot data; the client must use raw_data=False")
    references = {
        s: reference_close(snap, at) if (snap := snapshots.get(s)) is not None else None
        for s in symbols
    }
    bars = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Minute,
            start=regular_open,
            end=at,
            feed=DataFeed.SIP,
        )
    )
    if not isinstance(bars, BarSet):
        raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
    close: dict[str, float] = {}
    dvol: dict[str, float] = {}
    # completed minutes only, the ones `minute_dollars` counts (Muse's finding 2, 2026-11-11)
    done = regular_open + timedelta(minutes=elapsed)
    for symbol in symbols:
        if found := [b for b in bars.data.get(symbol, []) if b.timestamp < done]:
            close[symbol] = max(found, key=lambda b: b.timestamp).close
            dvol[symbol] = sum(minute_dollars(found, regular_open, elapsed))
    normals = read_normals(data, symbols, regular_open, elapsed)
    ranked = rank(close, dvol, references, normals, elapsed, top=len(symbols))
    record: dict[str, Any] = {
        "source": MOVER_SOURCE,
        "at": at.isoformat(),
        "elapsed_minutes": elapsed,
        "screener": gainers,
        "ranked": [asdict(r) for r in ranked[:MOVER_CHECKS]],
        "passed_over": {},
        "bounds": {
            "cap_usd": str(MAX_NOTIONAL_PER_ORDER_USD),
            "fresh_trade_secs": FRESH_TRADE_SECS,
        },
    }
    return first_qualified(trading, actions, ranked, snapshots, record, at, since, len(symbols))


def first_qualified(
    trading: TradingClient,
    actions: CorporateActionsClient,
    ranked: list,
    snapshots: dict,
    record: dict[str, Any],
    at: datetime,
    since: date,
    listed: int,
) -> MoverPick:
    """The first of `ranked`'s best `MOVER_CHECKS` that passes every filter `select_mover` names, with
    `record` completed; or `WindowRefused` naming why each was passed over. `snapshots` holds each one's
    latest sip trade (alpaca-py's `get_stock_snapshot`)."""
    passed: dict[str, str] = record["passed_over"]
    acted = corporate_actions(actions, [r.symbol for r in ranked[:MOVER_CHECKS]], since, at)
    for scored in ranked[:MOVER_CHECKS]:
        symbol = scored.symbol
        trade = getattr(snapshots.get(symbol), "latest_trade", None)
        if trade is None:
            passed[symbol] = "no latest sip trade in its snapshot"
            continue
        price = Decimal(str(trade.price))
        age = (at - trade.timestamp).total_seconds()
        if age > FRESH_TRADE_SECS:
            passed[symbol] = f"its latest sip trade is {age:.0f} s old (halted, by the proxy)"
            continue
        entry = entry_limit(snapshots.get(symbol), price)
        if entry > MAX_NOTIONAL_PER_ORDER_USD:
            passed[symbol] = (
                f"one share's entry limit of {entry} USD (last trade {price}) is over the cap of "
                f"{MAX_NOTIONAL_PER_ORDER_USD} USD"
            )
            continue
        try:
            asset = trading.get_asset(symbol)
        except FAILED as e:
            passed[symbol] = f"its asset could not be read: {type(e).__name__}: {e}"
            continue
        status = _value(getattr(asset, "status", None))
        if status != "active" or getattr(asset, "tradable", False) is not True:
            passed[symbol] = (
                f"Alpaca's asset is {status}, tradable {getattr(asset, 'tradable', None)}"
            )
            continue
        if (kind := NOT_COMMON.search(name := str(getattr(asset, "name", "") or ""))) is not None:
            passed[symbol] = f"Alpaca's asset name {name!r} marks a {kind[1].lower()}"
            continue
        if symbol in acted:
            passed[symbol] = f"a corporate action since {since}: {', '.join(acted[symbol])}"
            continue
        record.update(
            symbol=symbol,
            inputs=asdict(scored),
            price=str(price),
            entry_limit=str(entry),
            last_trade_at=trade.timestamp.isoformat(),
        )
        return MoverPick(symbol, price, record, entry)
    raise WindowRefused(
        f"no mover qualified among the {len(ranked)} ranked of {listed} listed: "
        + ("; ".join(f"{s}: {why}" for s, why in passed.items()) or "none ranked")
    )


def entry_limit(snapshot: object, last: Decimal) -> Decimal:
    """The limit one share of a mover would be bought at: `SessionMaCross`'s through the touch
    (`through_the_touch`, the ask plus `LIMIT_OFFSET`), on the snapshot's latest sip quote's ask, or the
    last trade where that is higher or there is no ask; the risk engine prices a limit order at its limit
    (Muse's finding 1, 2026-11-11), so this is the notional the per-order cap is checked on."""
    quote = getattr(snapshot, "latest_quote", None)
    ask = Decimal(str(getattr(quote, "ask_price", 0) or 0))
    return through_the_touch(OrderSide.BUY, Decimal(0), max(ask, last), LIMIT_OFFSET)


def corporate_actions(
    actions: CorporateActionsClient, symbols: list[str], since: date, at: datetime
) -> dict[str, list[str]]:
    """The corporate action types Alpaca lists from `since` to `at`'s New York date for each of `symbols`
    (one `get_corporate_actions` read), by every symbol an action names (`symbol`, `old_symbol`,
    `new_symbol`, `acquiree_symbol` and the like)."""
    if not symbols:
        return {}
    found = actions.get_corporate_actions(
        CorporateActionsRequest(symbols=symbols, start=since, end=in_new_york(at).date())
    )
    if not isinstance(found, CorporateActionsSet):
        raise TypeError(
            "alpaca-py returned raw corporate actions; the client must use raw_data=False"
        )
    acted: dict[str, list[str]] = {}
    for kind, listed in found.data.items():
        for action in listed:
            named = {v for k, v in action.model_dump().items() if k.endswith("symbol") and v}
            for symbol in sorted(named & set(symbols)):
                acted.setdefault(symbol, []).append(kind)
    return acted


def window_stream() -> StockDataStream:
    """alpaca-py's stock stream on the environment's pair, on `sip`, handing messages over as decoded
    (`raw_data=True`), as `movers_stream.sip_stream` builds it outside data-only mode."""
    return StockDataStream(
        os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], raw_data=True, feed=DataFeed.SIP
    )


def sample_stream(
    stream: Stream,
    minutes: int,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, float], dict[str, float], datetime, int, dict[str, int], int]:
    """The whole market's sip minute bars (`subscribe_bars(handler, "*")`, run in a thread ended by
    `stop()`, as `movers_stream.run` streams them) from the current clock minute for `minutes` minutes:
    each symbol's last close, its dollar volume since the sample's first minute (`movers_stream.Tape`),
    the start of the first minute a bar was received for, the minutes with bars from it, the message
    counts, and how many symbols were left out for lacking a bar in one of those minutes; or
    `StreamSampleFailed`."""
    start = clock()
    window = minute_start(start)
    tape = Tape(window_start=window)
    failures: list[str] = []

    def runner() -> None:
        try:
            stream.run()
        except Exception as e:  # noqa: BLE001, the sample fails on it
            failures.append(f"{type(e).__name__}: {e}")

    warnings = Warnings()
    logger = logging.getLogger(STREAM_LOGGER)
    logger.addHandler(warnings)
    seen: dict[str, set[float]] = {}

    async def on_bar(msg: dict) -> None:  # on the stream's thread: the GIL guards `seen`'s updates
        t, symbol = epoch_seconds(msg.get("t")), msg.get("S")
        complete = (
            msg.get("c") is not None and msg.get("v") is not None
        )  # as `Tape.on_bar` reads it
        if isinstance(symbol, str) and t is not None and t >= window and complete:
            seen.setdefault(symbol, set()).add(minute_start(t))
        await tape.on_bar(msg)

    stream.subscribe_bars(on_bar, "*")
    thread = threading.Thread(target=runner, name="mover-sample", daemon=True)
    thread.start()
    try:
        wait = window + minutes * 60 + BAR_GRACE_SECS - clock()
        if wait > 0:
            sleep(wait)
        alive = thread.is_alive()
    finally:
        stream.stop()
        thread.join(SAMPLE_STOP_SECS)
        logger.removeHandler(warnings)
    if failures or not alive:
        raise StreamSampleFailed(f"the sample's stream stopped early: {failures or 'no error'}")
    if (
        thread.is_alive()
    ):  # its connection may still hold the login's stream (movers_stream.run checks)
        raise StreamSampleFailed(
            f"the sample's stream did not end within {SAMPLE_STOP_SECS} s of stop()"
        )
    if (
        logged := warnings.take()
    ):  # a reconnect drops minutes of bars: a partial sample ranks nothing
        raise StreamSampleFailed(f"the sample's stream logged {logged}")
    counts, close, dvol, latest = tape.minute()
    # The sample's minutes start at the first a bar was received for: the host's minute at the start may
    # have closed before the subscription, so that no symbol has its bar (code review, 2026-11-11).
    first = min((m for minutes_seen in seen.values() for m in minutes_seen), default=window)
    elapsed = 0 if latest is None else int((latest - first) // 60) + 1
    if elapsed < 1 or not dvol:
        raise StreamSampleFailed(f"the sample saw no bar in its {minutes} minute(s): {counts}")
    # a symbol is ranked only on a bar in every minute of the sample (Muse's finding 4, 2026-11-11)
    # and is left out of the closes too, so it takes none of `refresh`'s reads (second review, 2026-11-11)
    incomplete = [s for s in close if len(seen.get(s, ())) < elapsed]
    for s in incomplete:
        close.pop(s, None)
        dvol.pop(s, None)
    return close, dvol, datetime.fromtimestamp(first, UTC), elapsed, counts, len(incomplete)


def stream_mover(
    trading: TradingClient,
    stream: Stream,
    data: StockHistoricalDataClient,
    screener: ScreenerClient,
    actions: CorporateActionsClient,
    since: date,
    minutes: int = SAMPLE_MINUTES,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> MoverPick:
    """The top name of the movers ranking over a `minutes` sample of the whole market's sip bars that
    passes `first_qualified`'s filters, or `WindowRefused`/`StreamSampleFailed`. The sample is
    `sample_stream`'s; references and normals are `movers_stream.refresh`'s reads (`REFRESH_PASSES`), the
    score `movers_stream.rank`'s (UNCALIBRATED, ADR 0083 gap 1), over the sample's own clock minutes. Alpaca's
    gainers are read once beside it, for the record only."""
    close, dvol, window_start, elapsed, counts, incomplete = sample_stream(
        stream, minutes, clock=clock, sleep=sleep
    )
    reads = Reads()
    for _ in range(REFRESH_PASSES):
        refresh(reads, data, close, window_start, elapsed, clock, clock() + REFRESH_SECS)
        if all(s in reads.references for s in close):
            break
    ranked = rank(close, dvol, reads.references, reads.normals, elapsed, top=MOVER_CHECKS)
    # Alpaca's clock where the caller has it (Muse's finding 3, 2026-11-11): the trade-age proxy is not
    # widened by the host's skew
    at = now() if now is not None else datetime.fromtimestamp(clock(), UTC)
    listed = movers_line(screener.get_market_movers(MarketMoversRequest(top=MOVERS_TOP)))
    record: dict[str, Any] = {
        "source": STREAM_SOURCE,
        "at": at.isoformat(),
        "sample": {
            "from": window_start.isoformat(),
            "minutes": minutes,
            "elapsed_minutes": elapsed,
            "incomplete": incomplete,
            "messages": counts,
            "symbols": len(close),
            "references_known": sum(1 for v in reads.references.values() if v is not None),
            "references_unread": sum(1 for s in close if s not in reads.references),
            "normals_read": len(reads.normals),
            "reads_failed": reads.failed,
        },
        "screener": listed["gainers"],
        "ranked": [asdict(r) for r in ranked],
        "passed_over": {},
        "bounds": {
            "cap_usd": str(MAX_NOTIONAL_PER_ORDER_USD),
            "fresh_trade_secs": FRESH_TRADE_SECS,
        },
    }
    if not ranked:
        raise WindowRefused(f"the stream sample ranked no name: {record['sample']}")
    snapshots = data.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=[r.symbol for r in ranked], feed=DataFeed.SIP)
    )
    if not isinstance(snapshots, dict):
        raise TypeError("alpaca-py returned raw snapshot data; the client must use raw_data=False")
    return first_qualified(trading, actions, ranked, snapshots, record, at, since, len(close))


def choose_mover(
    trading: TradingClient, at: datetime, regular_open: datetime, since: date
) -> MoverPick:
    """A mover window's name, chosen once before the node is built: `stream_mover` over a short sample of
    the window's own sip stream; if that fails or qualifies no name, `select_mover` over Alpaca's movers
    (`MOVER_SOURCE`), and the record says why (`stream_failed`). `at` is Alpaca's clock at the start; the
    fallback's is moved on by the time the sample took."""
    began = time.monotonic()

    def alpaca_now() -> datetime:
        return at + timedelta(seconds=time.monotonic() - began)

    data, screener, actions = window_data(), window_screener(), window_actions()
    try:
        return stream_mover(
            trading,
            window_stream(),
            data,
            screener,
            actions,
            since,
            now=alpaca_now,
        )
    except (StreamSampleFailed, WindowRefused, *FAILED) as e:
        failed = f"{type(e).__name__}: {e}"
    try:
        pick = select_mover(trading, data, screener, actions, alpaca_now(), regular_open, since)
    except FAILED as e:
        raise WindowRefused(
            f"no mover: the stream sample failed ({failed}), then Alpaca's movers did: "
            f"{type(e).__name__}: {e}"
        ) from e
    pick.record["stream_failed"] = failed
    return pick


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
    try:  # the slow average's bars and the spare ones, in the bar type's own interval
        history_from = warm_up_start(
            part,
            clock.timestamp,
            calendar,
            span=(config.slow_period + WARM_UP_SPARE_BARS) * config.bar_type.spec.timedelta,
        )
    except RuntimeError as e:
        raise WindowRefused(str(e)) from e
    if left := leftovers(trading):
        raise WindowRefused(f"the paper account is not flat; not starting: {left}")
    no_shorting = venue_no_shorting(trading)
    if VENUE_LONG_ONLY and not no_shorting:
        raise WindowRefused(
            "the paper account allows shorting (Alpaca's account configuration no_shorting is false), "
            "and a window never opens a short; not starting. To set it: mise run paper:long-only "
            f"{account if (account := paper_account()) in PAPER_ACCOUNTS else 'windows'} on"
        )
    mover: dict[str, Any] = {}
    pick: MoverPick | None = None
    if config.mover:  # chosen once, before the node is built (ADR 0083's dated line of 2026-11-11)
        at = clock.timestamp + timedelta(seconds=time.monotonic() - read_at)
        days = _trading_days(calendar)
        today = clock.timestamp.astimezone(NEW_YORK).date()
        if today not in days or not (earlier := [d for d in days if d < today]):
            raise WindowRefused(
                "today and a trading day before it are not both on Alpaca's calendar read, so the "
                "open and the last completed session are not known"
            )
        pick = choose_mover(trading, at, in_new_york(days[today].open), max(earlier))
        config, price, mover, feed = (
            replace(config, symbol=pick.symbol),
            pick.price,
            pick.record,
            MOVER_FEED,
        )
        one_share_fits(config.symbol, pick.entry)
    else:
        price = share_price(window_data(), config.symbol)
        one_share_fits(config.symbol, price)
    if config.strategy == MOVER and pick is not None:
        # at the limit `Mover` itself would send, which is what the risk engine values its order at; the
        # record's `entry_limit` is then this strategy's, not the bridge's through the touch
        limit = mover_entry_limit(window_data(), config.symbol)
        if limit > ENTRY_NOTIONAL_USD:
            raise WindowRefused(
                f"one share of {config.symbol} at its entry limit of {limit} USD is over {MOVER}'s "
                f"largest entry of {ENTRY_NOTIONAL_USD} USD; not starting"
            )
        mover["entry_limit"] = str(limit)
        pick = replace(pick, price=limit)  # the shares the largest entry buys are counted at it
    overnight = overnight_eligibility(trading, config.symbol) if part == "overnight" else ""

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
        no_shorting=no_shorting,
        share_price=str(price),
        mover=mover,
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
            warm_up_start=history_from,
            pick=pick,
            ends=ends,
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
                if isinstance(strategy, Mover):
                    report.mover["run"] = strategy.record()
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
            report.requests_by_api = [
                {
                    "account": c.account,
                    "api": c.api,
                    "total": c.total,
                    "peak_minute": c.peak,
                    "peak_60s": c.peak_60s,
                    "rate_limited": c.rate_limited_total,
                }
                for c in request_counts()
            ]
            if ran:
                marker.write_text(str(time.time()))
                _read_stream_lines(report, config.log_dir / f"window-{stamp}.log")
            no_data = _data_failure(report) if ran else None
            over_budget = request_failure(report) if config.strategy == MOVER else None
            if failure is not None:
                report.result = f"failed: {type(failure).__name__}: {failure}"
            elif report.left:
                report.result = f"failed: not left flat: {report.left}"
            elif report.cap_tripped:
                report.result = f"failed: {report.cap_tripped}"
            elif no_data:
                report.result = f"failed: {no_data}"
            elif over_budget:
                report.result = f"failed: {over_budget}"
            else:
                report.result = "ok"
            write_record(config.log_dir, report)
    if report.left:
        raise RuntimeError(f"the paper account was not left flat: {report.left}")
    if report.cap_tripped:
        raise RuntimeError(f"the position cap stopped the window: {report.cap_tripped}")
    if no_data:
        raise RuntimeError(f"the window ran without market data: {no_data}")
    if over_budget:
        raise RuntimeError(f"the window went over its request budget: {over_budget}")
    return report


def mover_entry_limit(data: StockHistoricalDataClient, symbol: str) -> Decimal:
    """The limit `Mover` would enter `symbol` at now: the ask of its latest sip quote (alpaca-py's
    `get_stock_latest_quote`) as `mover.entry_price` makes it a price Alpaca takes; or `WindowRefused`.
    The risk engine values a limit order at its limit price, not at the last trade, so this is the
    price one share is checked at and the shares of an entry are counted at."""
    found = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.SIP)
    )
    quote = found.get(symbol) if isinstance(found, dict) else None
    ask = getattr(quote, "ask_price", None)
    if not ask:
        raise WindowRefused(
            f"Alpaca gave no ask in {symbol}'s latest sip quote, so the limit an entry would be sent "
            "at is not known; not starting"
        )
    return entry_price(Decimal(str(ask)), 0)


REQUEST_CEILING = 180
"""The most requests a mover node may send Alpaca's trading API in any 60 seconds (UNCALIBRATED, ADR 0088):
under Alpaca's 200 a minute for an account; a window at or over it fails by name, as one that met a 429
does."""


def request_failure(report: WindowReport) -> str | None:
    """Why a mover window's requests fail it, or None: a count at or over `REQUEST_CEILING` in some 60
    seconds, or any 429, as the adapter counted them (`WindowReport.requests_by_api`)."""
    for count in report.requests_by_api:
        if count["rate_limited"]:
            return f"Alpaca answered 429 to {count['rate_limited']} request(s) of the {count['api']} API"
        if count["api"] == TRADING_API and count["peak_60s"] >= REQUEST_CEILING:
            return (
                f"{count['peak_60s']} requests to the {count['api']} API in 60 s, at or over the "
                f"ceiling of {REQUEST_CEILING}"
            )
    return None


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
        self.listed: list = []  # the open orders in the symbol of the last reading
        self.held = Decimal(0)  # the position in the symbol of the last reading
        self.others: list = []  # the open orders in other symbols of the last reading
        self.others_held: dict[str, Decimal] = {}  # the positions in other symbols, likewise
        self.tried: dict[str, int] = {}  # the cancels sent for each order
        self.open_until: float | None = None  # by the clock's last answer; None before any
        for client_order_id, side in unsettled.items():
            report.unsettled.setdefault(client_order_id, [side or "side not known"])

    def read(self) -> list[str]:
        """One reading: look up to `LOOKUPS` unsettled requests up, then read the account's open orders,
        which show a request as its lookup does, and its positions; return what is left (`left`). The
        reading is of the whole account; only the symbol's orders and position are the check's to act
        on. A lookup that fails is recorded and the reading goes on; a failed read of the orders or the
        positions raises."""
        self._look_up()
        listed = open_orders(self.trading)
        self.listed = [o for o in listed if o.symbol == self.symbol]
        self.others = [o for o in listed if o.symbol != self.symbol]
        for order in self.listed:  # the listing shows a request as its lookup does
            if (request := self.requests.get(str(order.client_order_id))) is not None:
                self._seen(request, order)
        self.others_held = positions(self.trading)
        self.held = self.others_held.pop(self.symbol, Decimal(0))
        return self.left()

    def left(self) -> list[str]:
        """What keeps the account from being flat, as last read: each working order and the position in
        the symbol, each unsettled request Alpaca has not shown, then what other symbols hold."""
        return (
            described(self.working(), self.held, self.symbol)
            + [r.missing() for r in self.requests.values() if r.order is None]
            + elsewhere(self.others, self.others_held)
        )

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
            # requests Alpaca has not shown: look again; else only other symbols' are left, not the
            # window's to act on
            return bool(self.requests)
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
    """What the account holds after the stop, once the check has worked at clearing the symbol: the
    reading is of the whole account, and what another symbol holds is named and left alone.

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
        # `ends` is a wall-clock time: read again every `END_TICK_SECS`, since the loop's own clock is
        # monotonic and stopped while the machine slept, which carried a window past its part (2026-11-11)
        # ... and also by the monotonic clock, so a wall clock stepped back cannot lengthen it (code review)
        bound = time.monotonic() + max(0.0, (ends - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(0)  # the node's task takes one step before any stop (code review)
        while not task.done():
            left = min((ends - datetime.now(UTC)).total_seconds(), bound - time.monotonic())
            if left <= 0:
                break
            await asyncio.wait({task}, timeout=min(left, END_TICK_SECS))
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
        events = order.events()
        names = [_event_name(e) for e in events]
        cid = order.client_order_id.value
        report.orders[cid] = names
        report.fills += names.count("OrderFilled")
        accepted = [e for e in events if type(e).__name__ == "OrderAccepted"]
        if events and accepted:
            delay = (int(accepted[0].ts_init) - int(events[0].ts_init)) // 1_000_000
            report.submit_delays_ms[cid] = delay
            worst = report.worst_submit_delay_ms
            report.worst_submit_delay_ms = delay if worst is None else max(worst, delay)
        for e in events:
            if type(e).__name__ == "OrderFilled":
                when = datetime.fromtimestamp(int(e.ts_event) / 1e9, UTC).isoformat()
                report.fill_prices.setdefault(cid, []).append(f"{e.last_qty}@{e.last_px} {when}")
        for event in events:
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
    parser.add_argument(
        "--regular-strategy",
        choices=tuple(n for n, runs in WINDOW_STRATEGIES.items() if "regular" in runs.sessions),
        help="with --session auto: the strategy of a window in the regular session; every other "
        "part runs --strategy",
    )
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument("--symbol", default="SPY")
    chosen.add_argument(
        "--mover",
        action="store_true",
        help="run on today's top mover, chosen at the start (regular session only)",
    )
    parser.add_argument("--log-dir", type=Path, help="default: `default_log_dir()`")
    return parser


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The arguments, `--log-dir` defaulted to `default_log_dir()` only when it is not given, and
    `RuntimeError` for a relative one: the records go outside the repository, wherever the run starts
    (an empty `getconf` in the caller's shell leaves only the folder's name)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.regular_strategy is not None and args.session != "auto":
        parser.error("--regular-strategy names a part's strategy, so it needs --session auto")
    if args.regular_strategy == MOVER and not args.mover:
        parser.error(f"--regular-strategy {MOVER} trades the day's mover, so it needs --mover")
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
            if args.regular_strategy is not None and clock.is_open:
                args.strategy = args.regular_strategy  # the schedule names the strategy a part runs
            resolved, why = resolve_session(clock, calendar, args.strategy)
            if args.mover and resolved not in (None, "regular"):
                resolved, why = None, f"{why}, and a mover window runs in the regular session only"
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
            mover=args.mover,
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
                    "account": paper_account(),
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
