"""One bounded window of a named strategy on Alpaca's paper environment: start flat, trade, flatten, check, report.

Contract: NautilusTrader v2.0.0rc8's `LiveNode.builder`, `LiveNode.add_builtin_strategy`, `Cache.quote_count`
and `trade_count`, `LiveExecutionEngineConfig` and `LoggerConfig` with a `FileWriterConfig`
(`live/__init__.pyi`, `common/__init__.pyi`); alpaca-py 0.45.0's `TradingClient` (paper)
`get_account`, `get_clock`, `get_calendar`, `get_orders`, `get_all_positions`, `get_asset`, `get_order_by_client_id`,
`cancel_order_by_id` and `submit_order` with a `MarketOrderRequest` or, after the stop outside the regular
session, a `LimitOrderRequest`, `StockHistoricalDataClient.get_stock_latest_quote`, and `get_account_configurations` and
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
  for it to finish", so neither a cancel nor `asyncio.run`'s exit can end a stop that hangs. Its
  `run_async` doc: "The host owns the loop and its signal handling, so this installs no signal handlers".
* CPython v3.14.10 at the pin (`b703b2c5e704`): `Doc/library/signal.rst:20-22`, SIGINT "is translated into
  a KeyboardInterrupt exception if the parent process has not changed it", so a window started with
  SIGINT ignored (as a non-interactive shell starts a command with `&`) keeps it ignored;
  `Doc/library/asyncio-runner.rst:170-178`, `Runner.run` installs a SIGINT handler that cancels the main
  task and then raises KeyboardInterrupt, which `Lib/asyncio/runners.py:112-113` does only on the main
  thread and only when SIGINT's handler is `signal.default_int_handler`; Python's default for SIGTERM
  ends the process with no `finally` run. `window_signals` therefore handles both for the whole window.
* macOS `timeout(1)` (`man timeout`): "For any other signals delivered to timeout, it will propagate them
  to command", to its whole process group; uv at its pin, `crates/uv-command-support/src/child.rs:117-122`,
  forwards SIGTERM to its child, and SIGINT when stdin is not a terminal. So one TERM to the task reaches
  this process two or three times; as in `custody.py:715-720` (`_stop`), a second signal is ignored, and
  `paper:session`'s `bounded` forwards a stop to `timeout` and waits for it within its KILL deadline.
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
* alpaca-py 0.45.0, `trading/client.py:465-479`, `get_account` reads `GET /account` into `TradeAccount`;
  `trading/models.py:565-574` documents `buying_power` (already including the account multiplier) and
  `cash`. Each Mover node's entry is `min(buying_power, cash) / slots` from one startup read, with its
  engine's per-order cap at `OUTER_BOUND_MULTIPLE` times that figure (lead's rule, 2026-11-11).
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
ID: an order sent that way can be looked up whatever became of its answer. It is a market order only while
Alpaca's clock, read just before, says the regular session is open, as upstream's market exit closes again
once nothing works. Outside it (a window that woke after its end, past the close: the resume work's change
A, 2026-11-11), a market order would only queue ("Orders not eligible for extended hours submitted after
4:00pm ET will be queued up for release the next trading day"), and Alpaca takes for extended hours "Only
limit orders with `time_in_force` set to `day` or `gtc`" with `extended_hours` true (orders-at-alpaca); so,
in a part of the extended session open by that clock and the window's calendar (`session_part`), the close
is a DAY limit so marked, `LIMIT_OFFSET` through the latest quote on the part's feed, put on Alpaca's step
by `close_limit` as `custody`'s close-only form puts theirs (the strategy's own still go through
`through_the_touch`, in whole cents). It works only while the check runs and is canceled as it ends; with
no part open none is sent. Either close is never sent while a request that could close the same
shares is sent and not found, since
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

A mover window (`--mover`, ADR 0083, ADR 0091) chooses its name once. In the regular session its strategy
is the one named by the arguments: `Mover` for the schedule, or the one-share `SessionMaCross` bridge
in its marked limit form. By the lead's decision of 2026-11-11, outside the regular session `--mover`
runs `Mover` too: `MOVER_PARTS` states each part's feed and configured slot count. Every venue order
there is a DAY limit marked `extended_hours=True`; the trailing stop-limit is held locally and releases
a marked limit (ADR 0088). Its wind-down is a limit sale and its stop cancels; regular-session market
exits are unchanged. A part's explicit `bridge_reason` runs the bridge on the same choice and feed,
recording the part, strategy and reason. A missing data row refuses the choice by name. The name is chosen before the
node is built (`choose_mover`) and loaded by NautilusTrader's instrument provider at connect
(`InstrumentProviderConfig(load_ids=...)`, `paper_node.alpaca_paper_builder`). The name comes from
`stream_mover`: a `SAMPLE_MINUTES` sample of the whole market's minute bars on the part's feed and the window's own pair
(alpaca-py's `StockDataStream.subscribe_bars(handler, "*")`, as `movers_stream` streams them), ranked by
`movers_stream.rank` on `movers_stream.refresh`'s references and normals. If the sample fails or qualifies
no name, `select_mover` takes Alpaca's gainers (alpaca-py's `ScreenerClient.get_market_movers`), ranked the
same way on `get_stock_snapshot`, `get_stock_bars` and `read_normals`, and the record says why. Either way
the pick is the first that `TradingClient.get_asset` gives `status`
active and `tradable` and a name that is not a warrant, unit or right (`NOT_COMMON`), with no corporate
action since the last session (`get_corporate_actions`), a trade on its feed inside `FRESH_TRADE_SECS` (not halted,
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
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
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
from decimal import ROUND_CEILING
from decimal import Decimal
from decimal import InvalidOperation
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
from alpaca.data.enums import Adjustment
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
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import FEED
from canon_alpaca.constants import VENUE
from canon_alpaca.data import late_prints_set_aside
from canon_alpaca.execution import DEFINITIVE_SUBMIT_REFUSALS
from canon_alpaca.execution import INSTRUMENT_REFUSAL
from canon_alpaca.execution import ORDERS_PAGE_LIMIT
from canon_alpaca.providers import CONNECT_TIMEOUT_SECS
from canon_alpaca.providers import READ_TIMEOUT_SECS
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import counted
from canon_alpaca.providers import endpoint_counts
from canon_alpaca.providers import not_sent
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import with_http_timeout
from canon_alpaca.trade_conditions import condition_counts
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

from . import price_step
from .mover import PAPER_LOSS_SHARE
from .mover import TRAIL_CAP_DEFAULT
from .mover import TRAIL_CAPS
from .mover import TRAIL_SETTINGS
from .mover import TRIGGERS as MOVER_TRIGGERS
from .mover import Mover
from .mover import MoverConfig
from .mover import baseline_per_minute
from .mover import protection_diagnostics
from .mover_window import MOVER
from .mover_window import MOVER_ID
from .mover_window import OUTER_BOUND_MULTIPLE
from .mover_window import PART_SLOTS
from .mover_window import RANK_MAX_AGE_SECS
from .mover_window import entry_diagnostics
from .mover_window import mover_context_feeds
from .mover_window import mover_universe_parts
from .mover_window import part_slots
from .mover_window import window_bars
from .mover_window import window_rank
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
from .paper_node import REQUEST_CEILING as PAPER_REQUEST_CEILING
from .paper_node import TIMEOUT_DISCONNECTION_SECS
from .paper_node import TIMEOUT_SHUTDOWN_SECS
from .paper_node import alpaca_paper_builder
from .paper_node import ema_cross_config
from .position_cap import PositionCap
from .position_cap import position_cap_config
from .price_step import entry_price
from .rank_shared import NOT_COMMON
from .rank_shared import write_atomically
from .request_budget import TRADING_REQUEST_LIMIT_PER_MINUTE
from .request_budget import calibration_eligible
from .request_budget import check_limit
from .request_budget import observation_seconds
from .request_budget import request_record
from .session_ma_cross import LIMIT_OFFSET
from .session_ma_cross import LIMIT_TIMEOUT_SECS
from .session_ma_cross import WARM_UP_SPARE_BARS
from .session_ma_cross import SessionMaCross
from .session_ma_cross import SessionMaCrossConfig
from .session_ma_cross import through_the_touch
from .trail import PROFILE_SETTINGS
from .trail import profile_session_date

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Generator
    from collections.abc import Mapping
    from collections.abc import Sequence
    from types import FrameType

    from nautilus_trader.common import Cache

    from .early_entry_study import Day
    from .movers_rank_actor import ReferenceRead
    from .movers_rank_actor import ReferenceReader
    from .movers_stream import Historical
    from .movers_stream import Stream

NEW_YORK = ZoneInfo("America/New_York")
SESSIONS = ("regular", "extended")
WINDOW_TRIGGERS = {trigger.name.lower(): trigger for trigger in MOVER_TRIGGERS}
"""The `--trigger` choices, by name: those `MoverConfig.trigger` accepts (`mover.TRIGGERS`)."""
PRINT_TRIGGERS = frozenset({"last_price"})
"""The `--trigger` choices that elect on trade prints. A window with one turns its data client's
`trade_condition_filter` on, so only a print Alpaca's minute bars take becomes a tick, and its record
says so; the filter is measured on sip prints only, so a part on another feed refuses it."""
MOVER_SPARES = 2
"""Startup roster names beyond the held slots (UNCALIBRATED); watched so a free slot can rotate."""
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
CALENDAR_BACK = timedelta(days=14)
"""How far back the calendar is read: past any run of weekend and holiday days before a trading day, and
far enough that the registered arm's five earlier sessions are held in the week after a market holiday
(two weeks hold at least eight trading days in any year: the lead's ruling of 2026-11-12 on the ultra
review of the composition, finding 4)."""
DEFAULT_MINUTES = 30
MAX_MINUTES = 60
GAP_SECS = 60
"""Between one window's end and the next one's start: one minute, by the lead's ruling of 2026-11-11. It was
300 s so that a paper test waiting for the shared stream lock got it; that reason lapsed when each account
got its own stream lock (#238). Requests are counted per minute (the window's own budget, any 60 s, ADR
0091; Alpaca's 200 a minute, ADR 0049), so a minute lets the last window's stop and account check leave the
count before the next start reads and orders (ADR 0044)."""
FLATTEN_SECS = 120
"""`SessionMaCross` winds down this long before the window ends. A built-in has no wind-down: `EmaCross`
trades until the stop, and an order it sent then is the account check's to settle (ADR 0060)."""
END_TICK_SECS = 1.0
"""How often the node's run reads the wall clock against the window's end (`_run_until`)."""
SLEPT_SECS = 10.0
"""How far the wall clock may run ahead of the monotonic one over a node's run before the record says the
process was suspended (`WindowReport.overrun`). On this host `time.monotonic` is `mach_absolute_time`
(`time.get_clock_info`), which stops while the machine sleeps, as the wall clock does not (#250's record).
UNCALIBRATED: it only has to be above what a clock adjustment makes, which is not measured here."""
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
        SESSIONS,
    ),
}
"""`Mover` (ADR 0088) runs on the loaded startup roster, re-ranked by ADR 0107's actor. Each start
resolves an account-derived per-slot entry figure for its strategy, actor, share/position monitors and
the engine's larger per-order outer bound; the generic table cap is not used for a Mover node.

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
"""Between the account check's polls after the stop. A poll sends at most seven trading requests,
whatever the names held: a reading (`LOOKUPS` lookups, the open orders and the position), then the
clock and either `CANCELS` cancels, or one close, or outside the regular session the calendar when it
must be read again; a poll that cancels sends no close, and one that reads the calendar sends no quote
and no close. An extended close's quote is one market-data request, and a quote that fails ends the
poll. So at most 168 trading requests a minute, inside the 200 a minute Alpaca's skill gives for the
paper API (ADR 0044, Evidence), and a poll is never longer than `POLL_AT_LONGEST_SECS`."""
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
TRAIL_ARMS: dict[str, dict[str, Any]] = {
    "F2": {"trail_mode": "fixed", "trail_cap": "clamp", "atr_k": Decimal(1)},
    "OWN1": {"trail_mode": "atr", "trail_cap": "own", "atr_k": Decimal(1)},
    "OWN3": {"trail_mode": "atr", "trail_cap": "own", "atr_k": Decimal(3)},
}
"""The three trail arms a Mover window may name (the lead's rotation of 2026-11-12, registered before any
result): F2 the fixed 2% trail; OWN1 and OWN3 ATR mode at k 1 and 3 over the spread floor, with the cap
arm `own`. All sized by `sizing.plan_for_loss` at the same planned loss. UNCALIBRATED; source the lead,
no primary source. A window that names one runs it; one that names no arm and no trail flag runs
`DEFAULT_ARM`."""
DEFAULT_ARM = "OWN1"
"""The arm a Mover window runs when its command line names no arm and no trail flag, marked in the
record (`arm_default`) and the heartbeat as the default taken: the trailing stop's distance per symbol,
from its own volatility and spread over a floor the spread sets (the owner's requirement of 2026-11-12;
ADR 0106's amendment of that date). `MoverConfig`'s and `WindowConfig`'s own defaults stay the fixed
trail, which the bars replay ranks (`tuning_contract.BARS_REPLAY_FIXED_ROWS`). UNCALIBRATED."""
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
"""How long the account check after the stop starts polls with one name held (`after_stop_secs`): a poll
begun before it runs to its end, then a last reading and a cancel of what it shows (`AFTER_END_SECS`)."""


def after_stop_secs(names: int) -> float:
    """How long the account check starts polls with `names` held: `AFTER_STOP_SECS`, and two poll
    intervals more (2 x `POLL_SECS`, 5 s) for each name past the first, since the check closes one name a
    poll and sees it closed in the next (review of 3a8613a, finding 11, the lead's ruling of 2026-11-12).
    It is wall-clock time, not a count of polls: a poll whose requests answer slowly takes longer than
    `POLL_SECS`, so slower answers fit fewer polls in it, a limit the deadline accepts. Names past
    `PART_SLOTS`, the names a window holds at once, add nothing: a run with more `--slots` is refused by
    name (`part_slots`, the lead's ruling of 2026-11-12), so the bounds below hold for every window."""
    return AFTER_STOP_SECS + (min(max(names, 1), PART_SLOTS) - 1) * 2 * POLL_SECS


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
"""One poll of the account check at its longest: a reading, the clock, and its cancels (the calendar, or
a quote and a close, sent instead of them, are one request or two)."""


def last_close_secs(names: int) -> float:
    """From a window's end until the account check's last close is sent, with `names` held, at most: the
    stop at its bound, the node's last worker calls, and the check's deadline (`after_stop_secs`) with
    the poll begun before it, its reading, the clock read and, outside the regular session, the quote
    the limit is priced from. Every request of that poll is counted."""
    return (
        STOP_TIMEOUT_SECS
        + ADAPTER_CALL_SECS
        + after_stop_secs(names)
        + READING_SECS
        + 2 * REQUEST_SECS
    )


def after_end_secs(names: int) -> float:
    """From a window's end until its process is done with Alpaca, with `names` held, at most: the stop,
    the node's last worker calls, the check's deadline (`after_stop_secs`), the poll begun before it at
    its longest, then the last reading, the clock and the last cancels, as long again. Every request is
    counted: a reading looks up no more than `LOOKUPS` orders and a step cancels no more than
    `CANCELS`, however many there are."""
    return STOP_TIMEOUT_SECS + ADAPTER_CALL_SECS + after_stop_secs(names) + 2 * POLL_AT_LONGEST_SECS


LAST_CLOSE_SECS = last_close_secs(PART_SLOTS)
"""`last_close_secs` with the most names a window holds (about 515 s)."""
AFTER_END_SECS = after_end_secs(PART_SLOTS)
"""`after_end_secs` with the most names a window holds (about 755 s)."""
STOP_GRACE_SECS = math.ceil(AFTER_END_SECS) + 30
"""How long `mise run paper:session`'s `bounded()` waits, after it passes a stop on as SIGTERM, before it
kills the window's process group (`stopgrace` in `mise.toml`, which a test holds to this): the wind-down
after a stop at its longest (`AFTER_END_SECS`, which does not depend on the session part: 755 s with
`PART_SLOTS` names in pre-market, regular, after-hours and overnight alike, 725 s with one), and 30 s for the
record's write and the process's exit (UNCALIBRATED). A kill at it leaves the record `stopping`
(`_WindowSignals`); the task's own deadline still kills at its time."""
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
REFERENCES_BEFORE_NODE_SECS = 120.0
"""How long a Mover window waits, before its node is built, for the reference read started with the stream
sample (UNCALIBRATED: the snapshot read measured about 45 s for the whole market on account A on
2026-11-12, at 1.1 s a request of 400)."""
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
    """Choose at startup on the part's feed (`MOVER_PARTS`). Mover loads a roster with spares and
    re-ranks it in the node; `symbol` labels the primary name. The compatibility bridge takes one."""
    slots: int | None = None
    """`Mover`'s slots, where the run sets them; else the session part's (`mover_window.part_slots`)."""
    trigger: str = "bid_ask"
    """What elects the Mover's emulated trailing sell (`MoverConfig.trigger`): `bid_ask`, the bid of
    every quote, or `last_price`, a trade print (persistence step 1, the lead's ruling of 2026-11-12).
    Chosen per window by --trigger; UNCALIBRATED. Under `last_price` a name that stops printing is not
    sold however far its bid falls, until a print, the wind-down, or 60 s with no two-sided quote (#320's
    `unpriced_exit`), the remade sell's check and the held stop included (`Mover._followed` reads the last
    trade, which can be minutes old outside the regular session, and none from before the entry's fill).
    Overnight it is refused: the part streams boats, not sip. It turns the data client's trade-condition filter on
    (`PRINT_TRIGGERS`)."""
    validate_trade_sequence: bool = False
    """Turn on the data client's check that sets aside a streamed print older than the newest one passed
    on (`AlpacaDataClientConfig.validate_trade_sequence`, ADR 0134). Chosen per window by
    --validate-trade-sequence and off unless given; Mover's, since its record carries the check's tally
    (`late_prints_set_aside_by_the_adapter`). Refused together with a print trigger: there Mover judges
    a late print itself (one counts toward a held stop's low) and must see every print, which the
    data client's check would set aside first. Under `bid_ask` no print feeds a hold's low, but the check
    still changes what Mover reads: its own late-print tally, the minute bars built from ticks, and,
    while the check's bound is measured against receipt and not Mover's quote reference, the newest
    trade a held stop or a sale with no bid reads (ADR 0134 names the limit)."""
    spares: int = MOVER_SPARES
    """Additional qualified startup names watched by Mover, beyond its held slots."""
    baseline_path: Path | None = None
    """ADR 0087's baseline file; None selects catalog/movers-baseline/<feed>.json for the part."""
    arm: str | None = None
    """A name of `TRAIL_ARMS`, written to the record and the heartbeat; its rows must be the window's."""
    trail_profile_path: Path | None = None
    """T5's strict profile file; None selects catalog/movers-baseline/trail-profile-v1.json."""
    arm_default: bool = False
    """True only when `arm` is `DEFAULT_ARM`, taken because the command line named no arm and no trail
    flag (`_mover_trail`); written to the record and the heartbeat."""
    trail_mode: str = "fixed"
    trail_cap: str = TRAIL_CAP_DEFAULT
    """What `Mover` does with a non-fixed trail over the 0.02 cap (`mover.TRAIL_CAPS`): `clamp`, main's
    behaviour, by default; `own`, the name's own distance with a smaller size, is an arm a window names
    (the lead's rotation of 2026-11-12: F2, OWN1, OWN3). UNCALIBRATED."""
    atr_k: Decimal = Decimal(1)
    atr_n: int = 14
    spread_floor_m: Decimal = Decimal(2)
    liquidity_tier_cut: Decimal = Decimal(1_000_000)
    ratchet_r: Decimal = Decimal(2)
    ratchet_g: Decimal = Decimal("0.5")
    ratchet_base: str = "atr"
    profile_k: Decimal = Decimal(2)
    profile_pctl: str = "p75"
    profile_floor: str = "median"
    profile_multipliers: tuple[Decimal, Decimal, Decimal] = (Decimal(1), Decimal(1), Decimal(1))
    cooldown_secs: int = 300
    reentry_arm: str = "clock"
    turn_k: int = 2
    turn_d: Decimal = Decimal(1)
    opening_arm: str = "off"
    opening_minutes: int = 30
    opening_f: Decimal = Decimal(1)
    """Opening: lead: the owner's observation; UNCALIBRATED; regular only, off by default."""
    flow_share: Decimal = Decimal("0.05")
    """T1/T2 and amendment 10's UNCALIBRATED window rows; fixed trail and clock cooldown remain the defaults.
    State re-entry arms: lead, no primary source; no clock delay when selected. VWAP is tape-only: a window refuses it.
    ATR multiples and the spread floor: lead, no primary source; fixed 0.02 remains a paired arm.
    The liquidity cut is baseline dollar volume per minute, a stand-in for size, not market cap."""
    request_limit_per_minute: int = TRADING_REQUEST_LIMIT_PER_MINUTE
    """Documented Trading API budget setting for the record, not a reported account limit."""

    def __post_init__(self) -> None:
        if self.trigger not in WINDOW_TRIGGERS:
            raise ValueError(
                f"trigger must be one of {tuple(WINDOW_TRIGGERS)}, got {self.trigger!r}"
            )
        MoverConfig(
            max_notional=Decimal(1),
            max_move=Decimal(1),
            trail_cap=self.trail_cap,
            extended_hours=self.session == "extended",
            **{name: getattr(self, name) for name in TRAIL_SETTINGS},
            trigger=WINDOW_TRIGGERS[self.trigger],
        )
        check_limit(self.request_limit_per_minute)
        if self.reentry_arm == "vwap":
            raise ValueError(
                "reentry_arm vwap reads a tape's session VWAP; a paper window has no live source"
            )
        if self.profile_multipliers != PROFILE_SETTINGS["profile_multipliers"]["default"]:
            raise ValueError(
                "profile_multipliers scale by RDc_1's bucket, and RDc_1 is not built on this tree: "
                "every name would take the first bucket's 1"
            )
        if self.opening_arm != "off":
            # The lead's condition, 2026-11-12: the arm lands off and stays off until it reads
            # Alpaca's minute bars from 09:30; the change that adds that request removes this.
            raise ValueError(
                f"opening_arm {self.opening_arm!r} is refused: the opening is judged on Alpaca's "
                "minute bars from 09:30, which this window does not request yet"
            )
        if self.trigger != "bid_ask" and self.strategy != MOVER:
            raise ValueError(
                f"trigger {self.trigger} elects Mover's trailing sell; {self.strategy} has none"
            )
        if self.validate_trade_sequence and self.strategy != MOVER:
            raise ValueError(
                f"validate_trade_sequence is recorded in Mover's record; {self.strategy} has none"
            )
        if self.validate_trade_sequence and self.trigger in PRINT_TRIGGERS:
            # The lead's ruling, 2026-11-13: under a print trigger Mover counts a late print toward
            # a held stop's low, so it must see every print; the adapter's check would hide them.
            raise ValueError(
                f"validate_trade_sequence with trigger {self.trigger} is refused: Mover judges a "
                "late print itself under a print trigger, and the data client's check would set "
                "that print aside before Mover saw it"
            )
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
        if self.strategy == MOVER and not self.mover:
            raise ValueError(
                f"{MOVER} trades the name the ranking gives, not a given symbol: run it with --mover"
            )
        if self.slots is not None:
            if self.strategy != MOVER:
                raise ValueError(f"slots are {MOVER}'s; {self.strategy} has none")
            part_slots(self.session, self.slots)  # refuses what is not a positive whole number
        if self.strategy != MOVER:
            if self.trail_profile_path is not None:
                raise ValueError(
                    f"trail_profile_path is {MOVER}'s trail input; {self.strategy} has none"
                )
            for name, row in TRAIL_SETTINGS.items():
                if getattr(self, name) != row["default"]:
                    raise ValueError(f"{name} is {MOVER}'s trail row; {self.strategy} has none")
            if self.trail_cap != TRAIL_CAP_DEFAULT:
                raise ValueError(f"trail_cap is {MOVER}'s trail row; {self.strategy} has none")
            if self.arm is not None:
                raise ValueError(f"arm {self.arm} is {MOVER}'s trail arm; {self.strategy} has none")
        if self.arm is not None:
            if (rows := TRAIL_ARMS.get(self.arm)) is None:
                raise ValueError(f"arm must be one of {tuple(TRAIL_ARMS)}, got {self.arm!r}")
            if wrong := [
                f"{n} {getattr(self, n)} not {v}" for n, v in rows.items() if getattr(self, n) != v
            ]:
                raise ValueError(
                    f"arm {self.arm} does not match the window's rows: {', '.join(wrong)}"
                )
        if self.arm_default and self.arm != DEFAULT_ARM:
            raise ValueError(
                f"arm_default marks the default arm {DEFAULT_ARM} taken, not arm {self.arm}"
            )
        if type(self.spares) is not int or self.spares < 0:
            raise ValueError(f"spares must be a nonnegative whole number, got {self.spares!r}")
        if (
            self.strategy == MOVER
            and part_slots(self.session, self.slots) + self.spares > MOVER_CHECKS
        ):
            raise ValueError(f"slots plus spares must be at most {MOVER_CHECKS}")
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


RANKED_TABLE_ROWS = 10


def ranked_table(
    record: Mapping[str, Any], run: Mapping[str, Any], *, tier: str
) -> list[dict[str, Any]]:
    """The lead's decision of 2026-11-11: a firing record's top ten ranked names, each with its move since
    the reference close, dollar volume so far, normal dollar volume for the same clock minutes (the
    sample's ratio's denominator), the ratio and the clock minutes it spans (the lead's ruling of
    2026-11-11), its tier and whether it held a slot. A column the sample
    lacks is marked unavailable by name."""
    held = set(run.get("opened_slots", ()))
    sample = record.get("sample") or {}
    span = (
        f"the stream sample's {sample['elapsed_minutes']} clock minutes from {sample['from']}"
        if "elapsed_minutes" in sample and "from" in sample
        else "unavailable: the record names no sample span"
    )
    rows = []
    for n, scored in enumerate(record.get("ranked", ())[:RANKED_TABLE_ROWS], start=1):
        ratio, dvol = scored["dvol_ratio"], scored["dvol"]
        rows.append(
            {
                "rank": n,
                "symbol": scored["symbol"],
                "move": scored["move"],
                "dollar_volume": dvol,
                "normal_dollar_volume": dvol / ratio
                if ratio > 0
                else "unavailable: the sample's ratio is not positive",
                "ratio": ratio,
                "ratio_span": span,
                "tier": tier,
                "held_slot": f"{scored['symbol']}.{VENUE}" in held,
            }
        )
    return rows


CENT = Decimal("0.01")


def trade_result(
    fills: Sequence[tuple[str, str, str, str, str]],
    sent: Sequence[Mapping[str, Any]],
    *,
    entry_figure: Decimal,
) -> dict[str, Any]:
    """The lead's order of 2026-11-11: each entry's filled size in dollars, with the figure and the bound
    that set it (the entry's own `sizing` record: budget, flow cap or share bound), and the window's
    realized result in dollars. `fills` are (client order ID, instrument,
    side, quantity, price) from the strategy's own fill events; realized is each instrument's sales less
    their shares at its average buy price, so an open remainder carries no result."""
    by_order: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    totals: dict[str, dict[str, Decimal]] = {}
    for cid, instrument, side, qty, px in fills:
        quantity, price = Decimal(qty), Decimal(px)
        by_order.setdefault(cid, []).append((instrument, quantity, price))
        t = totals.setdefault(
            instrument, {"bq": Decimal(0), "bd": Decimal(0), "sq": Decimal(0), "sd": Decimal(0)}
        )
        key = "b" if side == "BUY" else "s"
        t[key + "q"] += quantity
        t[key + "d"] += quantity * price
    entries = []
    for entry in sent:
        cid = str(entry["client_order_id"])
        sizing = entry.get("sizing") or {}
        if parts := by_order.get(cid):
            entries.append(
                {
                    "client_order_id": cid,
                    "instrument_id": parts[0][0],
                    "shares": str(sum(q for _, q, _ in parts)),
                    "dollars": str(sum((q * p for _, q, p in parts), Decimal(0)).quantize(CENT)),
                    "entry_figure": str(entry_figure),
                    "budget_notional": sizing.get(
                        "budget_notional", "unavailable: no sizing record"
                    ),
                    "flow_dollars": sizing.get("flow_dollars", "unavailable: no sizing record"),
                    "bound": sizing.get("binding", "unavailable: no sizing record"),
                }
            )
    by_instrument = {}
    realized_total = Decimal(0)
    incomplete = []
    for instrument, t in totals.items():
        row = {
            "bought": str(t["bd"].quantize(CENT)),
            "sold": str(t["sd"].quantize(CENT)),
            "open_shares": str(t["bq"] - t["sq"]),
        }
        if t["sq"] > t["bq"]:
            # a sale of shares bought by an order whose fill the window never saw: its cost is unknown,
            # so nothing in the name is booked (review of the slot commit, finding 2)
            why = f"sold {t['sq'] - t['bq']} shares more than the window's fills bought"
            incomplete.append(f"{instrument}: {why}")
            row["realized"] = f"unavailable: {why}"
        else:
            average = t["bd"] / t["bq"] if t["bq"] else Decimal(0)
            realized = (t["sd"] - t["sq"] * average).quantize(CENT)
            realized_total += realized
            row["realized"] = str(realized)
        by_instrument[instrument] = row
    return {
        "entries": entries,
        "by_instrument": by_instrument,
        "realized_usd": str(realized_total),
        "incomplete": incomplete,
        "flat": all(t["bq"] == t["sq"] for t in totals.values()),
        "basis": "fill prices at average cost; Alpaca charges no commission on these orders, none counted",
    }


def strategy_fills(cache, strategy_id: StrategyId) -> list[tuple[str, str, str, str, str]]:
    """(client order ID, instrument, side, quantity, price) for each of the strategy's fill events."""
    return [
        (
            order.client_order_id.value,
            str(order.instrument_id),
            order.side.name,
            str(event.last_qty),
            str(event.last_px),
        )
        for order in cache.orders(strategy_id=strategy_id)
        for event in order.events()
        if type(event).__name__ == "OrderFilled"
    ]


LIVE_OPEN_FINDINGS = (
    (
        "entries and rotation are confined to the startup roster: an instrument and its position cap "
        "exist only for the names loaded at node start; on-demand loading for any ranked name is not built"
    ),
    (
        "no boats baseline exists until Wave 2's overnight baseline, so overnight every name ranks in the "
        "zero-baseline dollar tier under the move condition, recorded in each tick's record and the "
        "window's mover record; regular and pre-market still refuse to start with no baseline file"
    ),
    "Antigravity's M-c findings 1 and 8 are deferred to ADR 0087's no-file rule and part-start seeding",
    (
        "its entries are not age-limited: a ranked name is entered whenever its run completes, however long "
        "after ignition (the lead's words, 2026-11-11; the ignition build adds the limit)"
    ),
    (
        "the actor is not seeded with the part's earlier minutes: its tallies count from the node's start "
        "only, after the 3-minute stream sample (a window launched at 04:00 counts from about 04:03), while "
        "a row's ratio_span names the part's clock minutes"
    ),
    (
        "no revised bars: the actor subscribes without updated bars, so each minute keeps its first "
        "emission and a late trade is missed until a later minute (the owner's plan of 2026-11-12, (a))"
    ),
    (
        "Alpaca paper fills check no quantity against the quoted size, model no impact or queue position, "
        "and fill partially at random: on thin names paper overstates what would fill, and only the flow "
        "cap gives size any realism"
    ),
    (
        "a streamed bar is stamped at its receipt, so one handled after a tick's scheduled instant and "
        "before its callback voids that minute's ranking for every name (ultra review of the "
        "composition, finding 9, PLAUSIBLE)"
    ),
    (
        "an Alpaca updated bar for a counted minute is refused by the tally as a duplicate and voids "
        "the next minute's ranking, and the run completing then is refused as stale (finding 10, "
        "PLAUSIBLE; how often updated bars come is UNVERIFIED)"
    ),
    (
        "a completed history response marks every minute to the current one covered, so a just-closed "
        "minute Alpaca has not yet published stays a hole in the name's day before its watch (finding "
        "15, PLAUSIBLE)"
    ),
)
"""What a live-ranking firing's record names as open (the lead's decision of 2026-11-11)."""

PRINT_TRIGGER_FINDINGS = (
    (
        "a print trigger elects on a trade print alone: no check against the quote or the NBBO at "
        "election (ADR 0119)"
    ),
    (
        "under a print trigger a holding with no print of its own since its fill and a cached two-sided "
        "quote is held until a print or the wind-down; the 60 s unpriced exit reads the cached quote, "
        "and the adapter discards a quote with a side at or under zero, so a name whose quotes turn "
        "one-sided keeps its last two-sided one"
    ),
    "the SIP print's venue time against Alpaca's fill time at the entry's boundary is UNVERIFIED",
    (
        "the guard against a print stamped before the entry covers the strategy's reads at arming, "
        "remake and hold only; the armed emulator takes each print as received, so a delayed print "
        "stamped before the fill, or before one already received, can elect an armed stop"
    ),
)
"""What a window under a print trigger names as open, beside `LIVE_OPEN_FINDINGS` (the xr3 fast
reader's note 3 on the trigger unit, 2026-11-12)."""


def _records(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [r for r in run.get("ranking_records", ()) if isinstance(r, dict)]


def live_ranked_table(run: Mapping[str, Any]) -> list[dict[str, Any]] | str:
    """The last published generation's best ten (`movers_rank_actor.top_rows`), each marked with whether
    its name held a slot in the window."""
    published = [r for r in _records(run) if r.get("published") is True and "top" in r]
    if not published:
        return "names ranked: none (no generation was published)"
    held = set(run.get("opened_slots", ()))
    return [
        {**row, "held_slot": f"{row['symbol']}.{VENUE}" in held} for row in published[-1]["top"]
    ]


def stream_symbols(run: Mapping[str, Any], *, sampled: object) -> dict[str, Any]:
    """Distinct symbols the actor's stream had delivered over the window and by its first minute (the
    first record at least 60 s after the first), beside the stream sample's count."""
    counted = [
        (int(r["latency_ns"]["generation"]), int(r["symbols"]))
        for r in _records(run)
        if "symbols" in r and isinstance(r.get("latency_ns"), dict)
    ]
    if not counted:
        return {"window": "unavailable: no generation counted symbols", "sample": sampled}
    first = counted[0][0]
    later = [n for t, n in counted if t - first >= 60 * 1_000_000_000]
    published = [r for r in _records(run) if r.get("published") is True]
    last = published[-1] if published else {}
    return {
        "assets_loaded": next(
            (r["assets"] for r in _records(run) if "assets" in r),
            "unavailable: no record counted the assets",
        ),
        "window": max(n for _, n in counted),
        "first_minute": later[0] if later else "unavailable: the window ranked under a minute",
        "sample": sampled,
        "first_publication_secs": first_publication_secs(run),
        "rows_ranked": last.get("rows", "unavailable: no generation was published"),
        "eligible": last.get("eligible", "unavailable: no generation was published"),
    }


def subscriptions(run: Mapping[str, Any], feed: str, assets: object) -> list[dict[str, Any]]:
    """The window's market-data subscriptions, each with its feed, channel, whether it is the wildcard and
    how many instruments it covers: the actor's wildcard bars and statuses over every loaded asset, and
    Mover's listed quotes and trades for each name it opened a slot for (its bars are INTERNAL, built from
    those trades, so no feed carries them)."""
    slots = len(run.get("opened_slots") or ())
    return [
        {
            "by": "MoversRankActor",
            "feed": feed,
            "channel": c,
            "wildcard": True,
            "instruments": assets,
        }
        for c in ("bars", "statuses")
    ] + [
        {"by": "Mover", "feed": feed, "channel": c, "wildcard": False, "instruments": slots}
        for c in ("quotes", "trades")
    ]


def start_reference_read(
    trading: TradingClient, notes: list[str]
) -> tuple[ReferenceReader, Future[ReferenceRead]]:
    """Start the rank actor's reference read for the whole market before the node exists, so it runs
    during the stream sample and the first generation does not wait for it (the owner's plan of
    2026-11-12, Wave 0 (b); measured at about 90 s for 14,406 symbols on 2026-11-12). The symbols are the
    assets the data client's `load_all` loads (one GET, `assets_request`'s default filter); the key is the
    actor's own (`reference_cutoff`, `trade_date`) for now. The read runs on its own thread with the
    environment's pair, GET only (`alpaca_reference_reader`). Returns the actor's reader and the read
    itself, which `references_before_node` waits for before the node is built."""
    import threading

    from canon_alpaca.providers import assets_request

    from .movers_rank_actor import alpaca_reference_reader
    from .movers_rank_actor import prefetched_reference_reader
    from .movers_rank_actor import reference_cutoff
    from .movers_rank_actor import trade_date as rank_trade_date

    assets = trading.get_all_assets(assets_request(None))
    if not isinstance(assets, list):
        raise TypeError("alpaca-py returned raw asset data; the client must use raw_data=False")
    symbols = sorted({asset.symbol for asset in assets})
    now = time.time_ns()
    key = (reference_cutoff(now), rank_trade_date(now))
    started = time.monotonic()
    future: Future[ReferenceRead] = Future()

    def read() -> None:
        # A daemon thread, not a ThreadPoolExecutor, whose workers the interpreter joins at exit: a
        # window refused after this starts must not wait out the read holding its locks. A failure is
        # noted too, so a caller waiting on `notes` is not left to its timeout.
        try:
            done = alpaca_reference_reader(symbols, *key)
        except BaseException as e:  # noqa: BLE001 - handed to the actor through the future
            notes.append(f"pre-read failed: {type(e).__name__}: {e}")
            future.set_exception(e)
            return
        notes.append(f"pre-read took {time.monotonic() - started:.1f} s")
        future.set_result(done)

    future.set_running_or_notify_cancel()
    threading.Thread(target=read, name="reference-pre-read", daemon=True).start()
    return prefetched_reference_reader(future, symbols, key, notes), future


def references_before_node(
    read: Future[ReferenceRead] | None, notes: list[str], bound_secs: float
) -> dict[str, object]:
    """Wait at most `bound_secs` for the window's reference read, so the node starts with every reference
    in (the lead's hold of 2026-11-12, item (i)), and return what the record carries: the read's wall time,
    the names it asked for, those with a close and those left without one, when it came in and how long
    the window waited. A read that did not start, failed, is not in within the bound or found no close
    refuses the window by name before any node exists."""
    if read is None:
        raise WindowRefused(f"the reference read did not start ({'; '.join(notes)})")
    began = time.monotonic()
    try:  # `exception` raises TimeoutError only for the wait; a read that failed with one returns it
        error = read.exception(timeout=bound_secs)
    except TimeoutError:
        raise WindowRefused(
            f"the reference read was not in within {bound_secs:g} s of the node's start"
        ) from None
    if error is not None:
        raise WindowRefused(
            f"the reference read failed: {type(error).__name__}: {error}"
        ) from error
    done = read.result()
    from .movers_rank_actor import reference_problem

    # the actor's own test of a read, so the node never starts on one the actor would refuse and read again
    # (code review of the snapshot read, finding 3)
    if (problem := reference_problem(done)) is not None:
        raise WindowRefused(f"the reference read is not usable: {problem}")
    return {
        **done.counts(),
        "before_node": True,
        "in_at": datetime.fromtimestamp(done.read_ns / 1e9, UTC).isoformat(timespec="milliseconds"),
        "waited_secs": round(time.monotonic() - began, 2),
    }


def dollar_basis() -> dict[str, str]:
    """Each ratio's dollar basis on this tree, for the record (the lead's ruling of 2026-11-12): ADR 0087's
    day ratio takes `movers_rank.bar_dollars`, and RDc_1 is not built here."""
    from .movers_rank import DOLLAR_BASIS

    return {"day_ratio_0087": DOLLAR_BASIS, "rdc_1": "absent: not built on this tree"}


def roster_entries(run: Mapping[str, Any]) -> dict[str, Any]:
    """Entries counted as in or outside the start-up roster, with each non-roster entry's dollars (its
    quantity at its limit) and the bound that held (`held_by`), from Mover's sizing record (the lead's
    ruling of 2026-11-12: a non-roster name is bounded by Mover's sizing alone). An entry with no such
    record (sized off the budget path, or with no start-up roster) is counted as unrecorded."""
    tally: dict[str, Any] = {"roster": 0, "non_roster": 0, "unrecorded": 0}
    outside: list[dict[str, Any]] = []
    for entry in run.get("entries", []):
        roster = (entry.get("sizing") or {}).get("roster")
        if roster is None:
            tally["unrecorded"] += 1
        elif roster:
            tally["roster"] += 1
        else:
            tally["non_roster"] += 1
            outside.append(
                {
                    "instrument_id": entry["instrument_id"],
                    "dollars": entry["sizing"].get("notional"),
                    "held_by": entry["sizing"].get("held_by"),
                }
            )
    tally["non_roster_entries"] = outside
    return tally


NO_CONTEXT = "no entry context supplied"
"""The note `Mover._watch` makes for a ranked name with no registered day context (`mover.py`)."""


def first_publication_secs(run: Mapping[str, Any]) -> float | str:
    """Seconds from the actor's start to its first published generation (the owner's plan, Wave 0)."""
    for record in _records(run):
        started = record.get("started_ns")
        generated = (record.get("latency_ns") or {}).get("generation")
        if (
            record.get("published") is True
            and isinstance(started, int)
            and isinstance(generated, int)
        ):
            return round((generated - started) / 1e9, 1)
    return "unavailable: no published generation with the actor's start"


def passed_over_without_context(run: Mapping[str, Any]) -> dict[str, Any]:
    """The ranked names a registered arm could not enter because no day context was supplied for them."""
    notes = run.get("notes") or {}
    names = sorted(
        str(i) for i, said in notes.items() if any(NO_CONTEXT in str(n) for n in said or ())
    )
    return {"count": len(names), "names": names}


def mover_baseline_path(config: WindowConfig, feed: str) -> Path:
    """Keep the actor's input relative to the checkout, regardless of the caller's directory."""
    path = config.baseline_path or Path("catalog/movers-baseline") / f"{feed}.json"
    return (SOURCE_ROOT / path).resolve()


def mover_trail_profile_path(config: WindowConfig) -> Path:
    """Resolve T5's file by the same checkout-relative rule as the ranking baseline."""
    path = config.trail_profile_path or Path("catalog/movers-baseline/trail-profile-v1.json")
    return (SOURCE_ROOT / path).resolve()


def _file_sha256(path: Path) -> str | None:
    """The SHA-256 of a file's bytes, or None when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_window_trail_profile(
    config: WindowConfig, *, as_of: date
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and identify one strict snapshot, or stop before node construction by name. The file is
    hashed before and after the strict reader has it, however the reader opens it; a file that is not
    the same bytes both times was replaced during the read, and the window is refused rather than
    recorded under a hash that may not be of the text it parsed."""
    from . import trail_profile
    from .history_load import HistoryStopped

    path = mover_trail_profile_path(config)
    before = _file_sha256(path)  # a file that cannot be read is named by the strict reader, below
    try:
        document = trail_profile.read_profile(path, as_of=as_of)
    except HistoryStopped as error:
        raise WindowRefused(str(error)) from error
    if before is None or _file_sha256(path) != before:
        raise WindowRefused(f"the trail profile {path} changed while it was read")
    return document, {
        "path": str(path),
        "sha256": before,
        "as_of": document["as_of"],
        "written_at": document["written_at"],
        "symbols": len(document["symbols"]),
        # The builder writes `windows` only on a recent-only file; a full file has no such field.
        "windows": document.get("windows", "recent and long"),
    }


@dataclass(frozen=True)
class MoverAllocation:
    """One startup account read, shared by selection, entries, monitors, actor and engine."""

    buying_power: Decimal
    cash: Decimal
    slots: int
    entry_notional: Decimal

    def record(self) -> dict[str, Any]:
        return {
            "buying_power": str(self.buying_power),
            "cash": str(self.cash),
            "slots": self.slots,
            "entry_notional": str(self.entry_notional),
            "engine_cash_limit": str(self.cash) if self.cash < self.buying_power else None,
            "engine_order_cap": str(self.entry_notional * OUTER_BOUND_MULTIPLE),
        }


def mover_allocation(trading: TradingClient, slots: int) -> MoverAllocation:
    """Alpaca TradeAccount.buying_power already includes its multiplier (alpaca-py models.py:565).

    Read once at each node start. The adapter currently reports CASH with cash as free funds; the
    allocation and its record keep that engine funding limit explicit rather than modelling margin.
    """
    from decimal import ROUND_FLOOR

    part_slots("regular", slots)
    try:
        account = trading.get_account()
    except FAILED as error:
        raise WindowRefused(
            f"the startup account could not be read: {type(error).__name__}: {error}"
        ) from error

    def amount(field: str) -> Decimal:
        raw = getattr(account, field, None)
        try:
            value = Decimal(str(raw))
        except InvalidOperation as error:
            raise WindowRefused(
                f"the startup account's {field} is missing or unparsable"
            ) from error
        if not value.is_finite() or value <= 0:
            raise WindowRefused(f"the startup account's {field} must be positive and finite")
        return value

    buying_power, cash = amount("buying_power"), amount("cash")
    entry_notional = (min(buying_power, cash) / slots).quantize(
        Decimal("0.01"), rounding=ROUND_FLOOR
    )
    if entry_notional <= 0:
        raise WindowRefused("the startup account entry notional is below one cent per slot")
    return MoverAllocation(buying_power, cash, slots, entry_notional)


def window_loss_share(account: str) -> Decimal:
    """The share of the slot's startup figure each `Mover` entry's planned loss is measured from
    (`mover.share_loss`): `PAPER_LOSS_SHARE` on paper accounts A and B (`PAPER_ACCOUNTS`), the lead's
    ruling of 2026-11-12. Any other account (C, a live node, or one not named) has no default, since
    there the figure is the owner's: the window is refused by name before any node is built."""
    if account not in PAPER_ACCOUNTS:
        raise WindowRefused(
            f"no planned loss for the account {account!r}: outside paper accounts A and B the loss "
            "an entry is sized for is the owner's figure, and none is given"
        )
    return PAPER_LOSS_SHARE


def window_node(
    config: WindowConfig,
    *,
    wind_down_at: datetime,
    log_name: str,
    feed: str,
    warm_up_start: datetime | None = None,
    pick: MoverPick | None = None,
    ends: datetime | None = None,
    slots: int | None = None,
    part: str = "regular",
    entry_contexts: Mapping[InstrumentId, Day] | None = None,
    entry_notional: Decimal | None = None,
    reference_reader: ReferenceReader | None = None,
    loss_share: Decimal | None = None,
    profile_document: dict[str, Any] | None = None,
) -> tuple[LiveNode, SessionMaCross | Mover | CachedData, PositionCap | tuple[PositionCap, ...]]:
    """The node, Alpaca's clients, the native risk limits and runtime checks, with the strategy and its
    position caps (one monitor, or a tuple for several names). They are returned too: the node does not
    hand its Python strategies back; for a built-in, what the
    record reads of it, `CachedData`). `feed` is named in `SessionMaCross`'s subscriptions and its warm-up
    request (ADR 0055); `EmaCross` subscribes with no params, so it runs only where the feed is the
    adapter's default. `slots` are `Mover`'s, the session part's (`mover_window.part_slots`); with none,
    `config.slots`, else the part's row. The actor's baseline is read from the configured path or the
    per-feed catalog default; no startup ranking is substituted when it cannot publish."""
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
    instruments: InstrumentId | tuple[InstrumentId, ...] = config.instrument_id
    mover: Mover | None = None
    caps: tuple[PositionCap, ...] = ()
    order_cap = runs.max_notional_per_order
    if config.strategy == MOVER:
        if pick is None or ends is None:
            raise RuntimeError(
                f"{MOVER} is built on the window's choice and its end; none was given"
            )
        if entry_notional is None:
            raise RuntimeError("a Mover node requires its startup account entry figure")
        if loss_share is None:
            raise RuntimeError(
                "a Mover node requires its loss share (`window_loss_share`): there is no default"
            )
        if config.trail_mode == "profile" and profile_document is None:
            profile_document, details = read_window_trail_profile(
                config,
                as_of=profile_session_date(int(wind_down_at.timestamp() * 1_000_000_000)),
            )
            pick.record["trail_profile"] = {
                **details,
                "window_instruments": sum(
                    p.symbol in profile_document["symbols"] for p in pick.picks
                ),
            }
        pick.record["rdc_1"] = "absent"
        pick.record["trail_mode"] = config.trail_mode
        order_cap = entry_notional * OUTER_BOUND_MULTIPLE
        prices = {InstrumentId.from_str(f"{p.symbol}.{VENUE}"): p.price for p in pick.picks}
        instruments = tuple(prices)
        selected_slots = config.slots if slots is None else slots
        mover, caps = mover_universe_parts(
            prices,
            extended=extended,
            feed=feed,
            wind_down_at=wind_down_at,
            market_exit_interval_ms=MARKET_EXIT_INTERVAL_MS,
            market_exit_max_attempts=MARKET_EXIT_MAX_ATTEMPTS,
            slots=MOVER_PARTS[part].slots if selected_slots is None else selected_slots,
            entry_arm=mover_entry_arm(part)[0],
            entry_contexts=entry_contexts,
            entry_notional=entry_notional,
            size_from_budget=True,
            loss_share=loss_share,
            trail_cap=config.trail_cap,
            rank_max_age_secs=RANK_MAX_AGE_SECS,
            trail_rows={name: getattr(config, name) for name in TRAIL_SETTINGS},
            trigger=WINDOW_TRIGGERS[config.trigger],
        )
        # Paper opt-in: every name outside the startup roster (with `data_load_all`, the whole market;
        # a name missing from the cache by a native instrument request) may enter under the upstream
        # absent-cap default, bounded by the Mover's sizing alone: no start-time cap, no position
        # monitor. The builder below still caps exactly the startup roster at entry x the multiple.
        mover.settings.load_instruments = True
        # The T1/T2 rows went through MoverConfig's validation above; reuse the startup ranking's
        # normal, so no new history or market-cap request is made.
        mover.baseline_dollar_volume_per_minute = {
            InstrumentId.from_str(f"{p.symbol}.{VENUE}"): normal
            for p in pick.picks
            if (normal := baseline_per_minute(p.record)) is not None
        }
        mover.trail_profile = profile_document if config.trail_mode == "profile" else None
    node = (
        alpaca_paper_builder(
            "CANON-PAPER-SESSION",
            instruments,
            # the managed stop's market exit runs inside the post-stop delay
            delay_post_stop_secs=MANAGED_DELAY_POST_STOP_SECS if managed else DELAY_POST_STOP_SECS,
            max_order_submit_rate=runs.max_order_submit_rate,
            max_notional_per_order=order_cap,
            data_load_all=mover is not None,
            trade_condition_filter=mover is not None and config.trigger in PRINT_TRIGGERS,
            validate_trade_sequence=mover is not None and config.validate_trade_sequence,
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
    if mover is not None:
        from .movers_rank_actor import MoversRankActorConfig

        assert entry_notional is not None and pick is not None
        # live in every part, overnight included (the lead's decision of 2026-11-11, on the owner's word)
        ranking = window_rank(
            MoversRankActorConfig(
                feed=feed,
                order_cap=float(entry_notional),
                baseline_path=str(mover_baseline_path(config, feed)),
                # with no baseline file, overnight only ranks on dollars (ultra review, finding 2)
                rank_without_baseline=part == "overnight",
                reference_reader=reference_reader,
            ),
            mover.ranking_records,
        )
        node.add_strategy(mover)
        for context_feed in mover_context_feeds(mover, feed):
            node.add_actor(context_feed)
        node.add_actor(ranking)
        for cap in caps:
            node.add_strategy(cap)
        return node, mover, caps[0] if len(caps) == 1 else caps
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
    check_fills: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    """Each close of the account check's own that Alpaca showed ended with shares filled, as
    `strategy_fills` gives the strategy's: (client order ID, instrument, side, quantity, average price);
    a Mover window's result counts them (`trade_result`)."""
    closes_not_placed: int = 0
    """The closes the account check sent that placed nothing: refused by Alpaca (403, 422), or never sent
    (a connect timeout); `errors` holds what each was answered."""
    errors: list[str] = field(default_factory=list)
    part: str = ""
    """The part of the session the window ran in: `regular`, `pre-market`, `after-hours`, `overnight`."""
    strategy: str = DEFAULT_STRATEGY
    """The strategy the window ran, a name of `WINDOW_STRATEGIES`."""
    arm: str | None = None
    """The trail arm the window ran (`TRAIL_ARMS`), or None."""
    arm_default: bool = False
    """True when `arm` is `DEFAULT_ARM`, taken because the window named no arm and no trail flag; false
    when the arm was named, or there is none."""
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
    instrument_refusals: int = 0
    """The instrument notifications the node's log shows refused for a full command queue
    (`canon_alpaca.execution.INSTRUMENT_REFUSAL`): an upstream limit at a whole-market start, one line
    for each instrument past the 1,024 the execution client's queue holds, counted here so that they are
    one known number and not read as the window's errors. The instruments themselves are in the cache
    (the adapter's README, "Instrument load and the command queue"). It is the whole window's count: a
    notification refused later, while the queue held real commands, is in it too, so compare it with the
    instruments the data client loaded less 1,024."""
    warm_up_bars: int = 0
    warm_up_dropped: int = 0
    remakes_under_floor: int = 0
    """A Mover's trailing sales remade, for a changed holding or a tightening, under the spread floor at
    their step (`Mover.remakes_under_floor`); each one's sale row in `mover.run` keeps that floor, L and
    the distance sent."""
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
    """One share's latest price at the start, which must fit the per-order cap (`one_share_fits`)."""
    mover: dict[str, Any] = field(default_factory=dict)
    """For a mover window, its choice (`stream_mover`, or `select_mover` as the fallback): the source,
    the symbol, its rank inputs, the names ranked and passed over with why, and the screener's list as
    Alpaca gave it."""
    requests_by_api: list[dict[str, Any]] = field(default_factory=list)
    """What the process sent to Alpaca's REST APIs by the window's end, per account and API, as the
    adapter counts it (`canon_alpaca.providers.request_counts`); the account is a number, never a key."""
    request_budget: dict[str, Any] = field(default_factory=lambda: request_record(()))
    """Process-average RPM, fixed-minute/rolling peaks and headroom against a limit setting. The
    adapter retains no minute buckets; `minute_buckets_kept` explicitly records that limit. Only a
    completed measurement is calibration-eligible; an interruption is preserved across snapshots."""
    fill_prices: dict[str, list[str]] = field(default_factory=dict)
    """Each fill of the strategy's orders, by client order ID: `qty@price` and the venue's time
    (`OrderFilled`'s `last_qty`, `last_px`, `ts_event`). A close the account check sends after the stop is
    not the strategy's: its outcome is in `closes`, its fill price is not recorded (Muse's finding 5)."""
    submit_delays_ms: dict[str, int] = field(default_factory=dict)
    """Per order, from the strategy's submit to the node's receipt of Alpaca's acceptance: `OrderAccepted`'s
    `ts_init` less the order's first event's (`OrderInitialized`), both on the node's clock. An order denied
    or rejected before Alpaca accepted it has none; it is in `denials` (Muse's finding 5)."""
    worst_submit_delay_ms: int | None = None
    overrun: str = ""
    """Set when the process was suspended during the node's run (a machine sleep, `SLEPT_SECS`): for how
    long, and how far past the window's end its stop ended; the account check after it is what makes the
    account flat, in whichever part of the session it woke."""


class WindowRefused(RuntimeError):
    """The window did not start, for a reason someone must look at (the account not flat, the session not
    open when one was named). Nothing was placed. Recorded as the latest result; the run exits 1."""


class StopTimedOut(RuntimeError):
    """The node did not stop within `STOP_TIMEOUT_SECS`. Recorded as failed after the account check;
    `main` then ends the process, since the node's task still runs and nothing can end it (module header)."""


class WindowSignalled(KeyboardInterrupt):
    """The window's node was stopped by a signal, SIGINT or SIGTERM, which the message names; raised as
    an interrupt, so the window's `finally` checks the account and writes the record as for Ctrl-C."""


_ABANDONED: list = []
"""A runner, the node's task and its worker calls (`Calls`) left after a stop timed out, held so neither
the runner nor the task is collected (the hosted run would then drive its shutdown inline,
`python/node.rs`) before `main` ends the process."""


class WindowSkipped(RuntimeError):
    """The window did not start, for a reason no one need act on: one that clears by itself (inside the gap
    after the last window, too little of the session left), or a part of a session with no feed
    (`SESSION_FEEDS`), which recurs each day until its row changes. Nothing was placed. Not recorded as a
    result; the run exits 0."""


def records_dir(log_dir: Path) -> Path:
    """Where this run's records go: `log_dir`, or its `by-hand/` for a run by hand (`BY_HAND_ENV`)."""
    if os.environ.get(BY_HAND_ENV, "") == "1":
        log_dir = log_dir / "by-hand"
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def write_record(
    log_dir: Path, report: WindowReport | dict, *, refresh_requests: bool = True
) -> BaseException | None:
    """Write `latest.json`, the one record `scripts/automation status` reads, and, for a window that ran,
    its own `window-<stamp>.json`, in `records_dir`. A final snapshot already checked by the caller is
    persisted without refreshing it. A snapshot error is recorded and returned after serialization;
    it never prevents the primary window failure from being written. An interruption (Ctrl-C,
    SystemExit) during the snapshot is raised once the record is written, never returned."""
    snapshot_error = _snapshot_requests(report) if refresh_requests else None
    if (
        snapshot_error is not None
        and isinstance(report, WindowReport)
        and report.result == "running"
    ):
        report.result = f"failed: {type(snapshot_error).__name__}: {snapshot_error}"
        report.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    _finish_request_measurement(report)
    if isinstance(report, WindowReport):
        signalled = _SIGNALS.first
        _write_window_record(log_dir, report)
        if _SIGNALS.first != signalled:  # the handler's mark came during this write, which is older
            _write_window_record(log_dir, report)
    else:
        write_atomically(records_dir(log_dir) / "latest.json", json.dumps(report, indent=2))
    if snapshot_error is not None and not isinstance(snapshot_error, Exception):
        raise snapshot_error
    return snapshot_error


def _write_window_record(log_dir: Path, report: WindowReport) -> None:
    """`window-<stamp>.json` and `latest.json` for `report`, as `_shown_record` gives it; changes nothing in
    `report`, so the signal handler may call it between any two steps of the main thread."""
    log_dir = records_dir(log_dir)
    body = json.dumps(_shown_record(report), indent=2)
    write_atomically(log_dir / f"window-{report.started}.json", body)
    write_atomically(log_dir / "latest.json", body)


def _shown_record(report: WindowReport) -> dict:
    """`report` as a record, with a still `running` result shown as stopping once a signal came, so after a
    signal the record never reads `running` again (`_WindowSignals`)."""
    fields = asdict(report)
    if fields["result"] == "running" and _SIGNALS.first is not None:
        fields["result"] = f"stopping: {_SIGNALS.first} received; the window has not finished"
    return fields


def _request_measurement_status(fields: dict, budget: dict) -> str:
    result = fields.get("result")
    if budget.get("measurement_status") == "interrupted":
        return "interrupted"
    if budget.get("snapshot_error"):
        return "unfinalized"
    if result == "running":
        return "running"
    if result == "ok" and fields.get("ended"):
        return "complete"
    if isinstance(result, str) and result.startswith("refused:"):
        return "refused"
    if isinstance(result, str) and result.startswith("failed:"):
        return "failed"
    return "unfinalized"


def _finish_request_measurement(report: WindowReport | dict) -> None:
    """Update finality only; counts and their observation interval remain the checked snapshot's."""
    fields = vars(report) if isinstance(report, WindowReport) else report
    budget = fields.get("request_budget")
    if not isinstance(budget, dict):
        return
    state = _request_measurement_status(fields, budget)
    elapsed = budget.get("observation_seconds")
    budget["measurement_status"] = state
    budget["calibration_eligible"] = calibration_eligible(state, elapsed)


def _snapshot_requests(report: WindowReport | dict) -> BaseException | None:
    """Refresh both request record forms, preserving an interruption through any later write.

    A running record is incomplete even if the process dies without a finalizer. Only an `ok` window
    with an end timestamp can supply a completed request measurement; generic dictionaries without
    request-budget fields retain their existing form.
    """
    fields = vars(report) if isinstance(report, WindowReport) else report
    mover = fields.get("mover")
    http = mover.get("http_requests") if isinstance(mover, dict) else None
    if isinstance(http, dict):  # the endpoint count at the window's true end, as the budget's
        http["to_window_end"] = endpoint_counts()
    # the prints a client's trade-condition filter kept and left out, by code: only with one on
    if isinstance(mover, dict) and (counts := condition_counts()):
        mover["trade_conditions"] = counts
    # the streamed prints a client's sequence check set aside, per symbol with how many and the most any
    # was behind: only with one on, and only once one was
    if isinstance(mover, dict) and (late := late_prints_set_aside()):
        mover["late_prints_set_aside_by_the_adapter"] = late
    if (prior := fields.get("request_budget")) is None:
        return None
    budget = prior if isinstance(prior, dict) else {}
    try:
        elapsed = observation_seconds()
        observation_error = None
        if not math.isfinite(elapsed) or elapsed < 0:
            observation_error = "request observation interval is not finite and nonnegative"
            elapsed = None
        snapshot = request_record(
            request_counts(),
            elapsed_seconds=elapsed,
            limit_per_minute=budget.get("limit_per_minute", TRADING_REQUEST_LIMIT_PER_MINUTE),
            measurement_status=_request_measurement_status(fields, budget),
            ceiling=REQUEST_CEILING,
        )
        if observation_error:
            snapshot["observation_error"] = observation_error
        fields["request_budget"] = snapshot
        fields["requests_by_api"] = snapshot["by_api"]
        return None
    except BaseException as error:  # noqa: BLE001 - returned to the caller after recording, not discarded
        # Preserve the prior counts as explicitly stale data, not a replacement measurement. The
        # caller can still serialize the record and rethrow its primary error instead of this one.
        budget = dict(budget)
        budget["snapshot_error"] = f"{type(error).__name__}: {error}"
        if budget.get("measurement_status") != "interrupted":
            budget["measurement_status"] = "unfinalized"
        budget["calibration_eligible"] = False
        fields["request_budget"] = budget
        if isinstance(report, WindowReport):
            _mark_request_interrupted(report, error)
            report.errors.append(f"request snapshot failed: {type(error).__name__}: {error}")
        return error


def _mark_request_interrupted(report: WindowReport, error: BaseException) -> None:
    """System interruption or a node left running cannot supply a calibration measurement."""
    if not isinstance(error, Exception) or isinstance(error, StopTimedOut):
        report.request_budget["measurement_status"] = "interrupted"


CLOCK_NOTE_MOST_MS = 2_000
"""The lead's rule of 2026-11-13, 08:40: a window whose record shows the venue's clock and this host's
more than 2 s apart goes to the lead. Read on the median of the Mover's `host_clock`, which includes
the feed's latency (tens of milliseconds)."""


def clock_notes(run: dict[str, Any]) -> dict[str, Any]:
    """Name beside the Mover's record the prints whose venue times exceeded the ahead bound, measured
    against their name's quotes (`mover.print_time`), when any did, and the clocks' difference, when its median passes `CLOCK_NOTE_MOST_MS` either way.
    The prints ahead could not be ordered; the difference is the one the lead reads."""
    notes: dict[str, Any] = {}
    ahead = run.get("prints_stamped_ahead") or {}
    if any(count > 0 for count in ahead.values()):
        notes["prints_stamped_ahead"] = dict(ahead)
    clock = run.get("host_clock") or {}
    seen = clock.get("venue_minus_receipt_ms") or {}
    if abs(seen.get("median") or 0) > CLOCK_NOTE_MOST_MS:
        notes["host_clock"] = dict(clock)
    return notes


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


def send_once(trading: object) -> None:
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


FRESH_TRADE_SECS = 120
"""A mover's latest sip trade must be at most this old at its selection (UNCALIBRATED): a proxy for not
halted, with no upstream behind it. alpaca-py 0.45.0 reads no halt over REST (`Asset` has `status` and
`tradable`; Alpaca's asset `attributes` has `overnight_halted` only), and halts come on the stream's
statuses channel (`subscribe_trading_statuses`), whose page does not say a subscription is sent the current
status (ADR 0083, gap 4)."""
MOVER_CHECKS = 20
"""The ranked names checked, best first, before a mover window is refused: each priced and fresh one costs
one asset read."""
MOVER_FEED = DataFeed.SIP.value
"""The default for direct mover reads; a window uses its `MOVER_PARTS` row instead."""
MOVER_SOURCE = "screener"
"""`select_mover`'s source: Alpaca's movers (`movers_screener`), `choose_mover`'s fallback when the stream
sample fails or qualifies no name (ADR 0083's dated line of 2026-11-11)."""
STREAM_SOURCE = "stream"
"""`stream_mover`'s source: a short sample of the whole market's minute bars on the part's feed."""
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


@dataclass(frozen=True)
class MoverPart:
    """A session part's run settings (ADR 0083, ADR 0091), all slot counts UNCALIBRATED.

    `bridge_reason`, when set, explicitly runs SessionMaCross's limit form in place of Mover and is
    recorded with the part. An absent data feed cannot be repaired by changing strategies.
    """

    feed: str
    slots: int = 1
    bridge_reason: str = ""


MOVER_PARTS = {
    "regular": MoverPart(DataFeed.SIP.value, slots=part_slots("regular")),
    "pre-market": MoverPart(DataFeed.SIP.value, slots=part_slots("pre-market")),
    "after-hours": MoverPart(DataFeed.SIP.value, slots=part_slots("after-hours")),
    "overnight": MoverPart(DataFeed.BOATS.value, slots=part_slots("overnight")),
}
"""The mover choice and node's feed, and Mover's slot row, for each part. Alpaca's Trading API 24/5
page puts overnight quotes, trades, snapshots and historical bars on `boats` for the account's plan;
the reference close remains the consolidated regular-session close on `sip`."""


def mover_entry_arm(part: str) -> tuple[str, str]:
    """The registered study's 04:00-16:00 scope; the lead's explicit bridge in other session parts."""
    if part in ("regular", "pre-market"):
        return "registered", ""
    return "bridge", f"{part} is outside the registered arm's 04:00-16:00 New York scope"


def no_mover_form(part: str) -> str | None:
    """Why a mover window cannot run in `part` of a session, or None where it can (`MOVER_PARTS`): its
    name is chosen on the row's feed (the sample's bars, each candidate's latest trade and quote) and the
    node streams it, whatever feed `SESSION_FEEDS` gives the part's other windows, so that row decides
    nothing for it; a mover window asks this in place of `window_feed`."""
    if part in MOVER_PARTS:
        return None
    found = SESSION_FEEDS.get(part)
    other = (
        f"the {part} part's data is on {found.feed}"
        if found is not None and found.feed is not None
        else f"no feed is set for the {part} part (SESSION_FEEDS)"
    )
    return f"a mover window has no {part} form: no row in MOVER_PARTS, and {other}"


class StreamSampleFailed(RuntimeError):
    """The stream sample could not rank: the stream stopped or failed, or no bar came."""


@dataclass(frozen=True)
class MoverPick:
    """The primary qualified name and any additional startup names, with each name's price and inputs.

    The single-name bridge reads the primary fields; Mover's composition uses `picks`, in rank order.
    """

    symbol: str
    price: Decimal
    record: dict[str, Any]
    entry: Decimal
    """The entry limit one share would be sent at (`entry_limit`), which the cap is checked against."""
    others: tuple[MoverPick, ...] = ()

    @property
    def picks(self) -> tuple[MoverPick, ...]:
        return (self, *self.others)


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
    *,
    count: int = 1,
    order_cap: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
    for_mover: bool = False,
    part: str = "regular",
    trade_date: date | None = None,
    entry_notional: Decimal | None = None,
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
            "cap_usd": str(order_cap),
            "fresh_trade_secs": FRESH_TRADE_SECS,
        },
    }
    return first_qualified(
        trading,
        actions,
        ranked,
        snapshots,
        record,
        at,
        since,
        len(symbols),
        count=count,
        order_cap=order_cap,
        for_mover=for_mover,
        part=part,
        trade_date=trade_date,
        entry_notional=entry_notional,
    )


def first_qualified(
    trading: TradingClient,
    actions: CorporateActionsClient,
    ranked: list,
    snapshots: dict,
    record: dict[str, Any],
    at: datetime,
    since: date,
    listed: int,
    *,
    count: int = 1,
    order_cap: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
    for_mover: bool = False,
    part: str = "regular",
    trade_date: date | None = None,
    entry_notional: Decimal | None = None,
) -> MoverPick:
    """The first `count` of `ranked`'s best `MOVER_CHECKS` that pass every filter `select_mover` names,
    in rank order, with `record` completed from the first; or `WindowRefused` naming why each was passed
    over. `snapshots` holds each one's latest trade on the selected feed (alpaca-py's
    `get_stock_snapshot`). For Mover the entry ask must be positive and its entry limit fit `order_cap`;
    an overnight candidate must be eligible there before it is chosen. The window rechecks both after
    the choice."""
    if type(count) is not int or not 1 <= count <= MOVER_CHECKS:
        raise ValueError(f"roster count must be from 1 to {MOVER_CHECKS}, got {count!r}")
    passed: dict[str, str] = record["passed_over"]
    picks: list[MoverPick] = []
    feed = record.get("feed", MOVER_FEED)
    acted = corporate_actions(
        actions,
        [r.symbol for r in ranked[:MOVER_CHECKS]],
        since,
        at,
        trade_date=trade_date,
    )
    for scored in ranked[:MOVER_CHECKS]:
        symbol = scored.symbol
        trade = getattr(snapshots.get(symbol), "latest_trade", None)
        if trade is None:
            passed[symbol] = f"no latest {feed} trade in its snapshot"
            continue
        price = Decimal(str(trade.price))
        age = (at - trade.timestamp).total_seconds()
        if age > FRESH_TRADE_SECS:
            passed[symbol] = f"its latest {feed} trade is {age:.0f} s old (halted, by the proxy)"
            continue
        if for_mover:
            quote = getattr(snapshots.get(symbol), "latest_quote", None)
            ask = Decimal(str(getattr(quote, "ask_price", 0) or 0))
            if not ask.is_finite() or ask <= 0:
                passed[symbol] = f"no positive ask in its latest {feed} quote"
                continue
        try:
            entry = entry_limit(snapshots.get(symbol), price, for_mover=for_mover)
        except ValueError as error:
            passed[symbol] = f"no usable entry limit: {error}"
            continue
        if entry > order_cap:
            passed[symbol] = (
                f"one share's entry limit of {entry} USD (last trade {price}) is over the cap of "
                f"{order_cap} USD"
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
        if part == "overnight":
            try:
                overnight_eligibility(trading, symbol)
            except WindowRefused as e:
                passed[symbol] = str(e)
                continue
        inputs = {
            "symbol": symbol,
            "inputs": asdict(scored),
            "price": str(price),
            "entry_limit": str(entry),
            "last_trade_at": trade.timestamp.isoformat(),
        }
        picks.append(MoverPick(symbol, price, {**record, **inputs}, entry))
        if len(picks) == count:
            break
    if picks:
        record.update(picks[0].record)
        record["roster_requested"] = count
        record["roster"] = [
            {
                "symbol": p.symbol,
                "price": str(p.price),
                "entry_limit": str(p.entry),
                "inputs": p.record["inputs"],
            }
            for p in picks
        ]
        return replace(picks[0], record=record, others=tuple(picks[1:]))
    raise WindowRefused(
        f"no mover qualified among the {len(ranked)} ranked of {listed} listed: "
        + ("; ".join(f"{s}: {why}" for s, why in passed.items()) or "none ranked")
    )


def entry_limit(snapshot: object, last: Decimal, *, for_mover: bool = True) -> Decimal:
    """Mover's actual ask limit; `ValueError` without a positive ask, since a Mover entry is never
    priced from the last trade (main's positive-ask rule, ruling (i) of the composition landing).
    Explicit compatibility callers retain SessionMaCross's through-the-touch offset and
    max(ask, last) screen."""
    quote = getattr(snapshot, "latest_quote", None)
    ask = Decimal(str(getattr(quote, "ask_price", 0) or 0))
    if for_mover:
        if not ask.is_finite() or ask <= 0:
            raise ValueError("no positive ask in the snapshot's latest quote")
        return entry_price(ask, 0)
    return through_the_touch(OrderSide.BUY, Decimal(0), max(ask, last), LIMIT_OFFSET)


def corporate_actions(
    actions: CorporateActionsClient,
    symbols: list[str],
    since: date,
    at: datetime,
    *,
    trade_date: date | None = None,
) -> dict[str, list[str]]:
    """The corporate action types Alpaca lists from `since` through the later of `at`'s New York date
    and `trade_date` (the next day before midnight overnight) for each of `symbols`
    (one `get_corporate_actions` read), by every symbol an action names (`symbol`, `old_symbol`,
    `new_symbol`, `acquiree_symbol` and the like)."""
    if not symbols:
        return {}
    until = max(in_new_york(at).date(), trade_date or in_new_york(at).date())
    found = actions.get_corporate_actions(
        CorporateActionsRequest(symbols=symbols, start=since, end=until)
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


def window_stream(*, feed: str = DataFeed.SIP.value) -> StockDataStream:
    """alpaca-py's stock stream on the environment's pair, on `feed` (`sip` unless named; the pinned
    `StockDataStream.__init__` takes `feed: DataFeed`, alpaca/data/live/stock.py:32), handing messages
    over as decoded (`raw_data=True`), as `movers_stream.sip_stream` builds it outside data-only mode."""
    return StockDataStream(
        os.environ[ENV_API_KEY], os.environ[ENV_SECRET_KEY], raw_data=True, feed=DataFeed(feed)
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


class MoverHistory:
    """The Historical subset of alpaca-py's StockHistoricalDataClient used by `refresh`.

    Its reference reads keep SIP snapshots' consolidated daily bars. Its minute-bar requests for
    normals take the selected part's feed (`StockBarsRequest.feed`, alpaca-py 0.45.0
    `alpaca/data/requests.py`), so an overnight numerator and denominator both measure BOATS.
    """

    def __init__(self, client: Historical, feed: str) -> None:
        self.client = client
        self.feed = DataFeed(feed)

    def get_stock_snapshot(self, request_params: StockSnapshotRequest) -> object:
        return self.client.get_stock_snapshot(request_params)

    def get_stock_bars(self, request_params: StockBarsRequest) -> object:
        request_params.feed = self.feed
        return self.client.get_stock_bars(request_params)


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
    feed: str = MOVER_FEED,
    count: int = 1,
    order_cap: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
    for_mover: bool = False,
    part: str = "regular",
    trade_date: date | None = None,
    entry_notional: Decimal | None = None,
) -> MoverPick:
    """The top name of the movers ranking over a `minutes` sample of the whole market's bars on `feed` that
    passes `first_qualified`'s filters, or `WindowRefused`/`StreamSampleFailed`. The sample is
    `sample_stream`'s; references and normals are `movers_stream.refresh`'s reads (`REFRESH_PASSES`), with
    consolidated daily reference bars on SIP and normals on `feed` (`MoverHistory`), the
    score `movers_stream.rank`'s (UNCALIBRATED, ADR 0083 gap 1), over the sample's own clock minutes. Alpaca's
    gainers are read once beside it, for the record only."""
    close, dvol, window_start, elapsed, counts, incomplete = sample_stream(
        stream, minutes, clock=clock, sleep=sleep
    )
    reads = Reads()
    history = MoverHistory(data, feed)
    for _ in range(REFRESH_PASSES):
        refresh(
            reads,
            history,
            close,
            window_start,
            elapsed,
            clock,
            clock() + REFRESH_SECS,
            overnight=part == "overnight",
        )
        if all(s in reads.references for s in close):
            break
    ranked = rank(close, dvol, reads.references, reads.normals, elapsed, top=MOVER_CHECKS)
    # Alpaca's clock where the caller has it (Muse's finding 3, 2026-11-11): the trade-age proxy is not
    # widened by the host's skew
    at = now() if now is not None else datetime.fromtimestamp(clock(), UTC)
    listed = movers_line(screener.get_market_movers(MarketMoversRequest(top=MOVERS_TOP)))
    record: dict[str, Any] = {
        "source": STREAM_SOURCE,
        "feed": feed,
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
            "cap_usd": str(order_cap),
            "fresh_trade_secs": FRESH_TRADE_SECS,
        },
    }
    if not ranked:
        raise WindowRefused(f"the stream sample ranked no name: {record['sample']}")
    snapshots = data.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=[r.symbol for r in ranked], feed=DataFeed(feed))
    )
    if not isinstance(snapshots, dict):
        raise TypeError("alpaca-py returned raw snapshot data; the client must use raw_data=False")
    return first_qualified(
        trading,
        actions,
        ranked,
        snapshots,
        record,
        at,
        since,
        len(close),
        count=count,
        order_cap=order_cap,
        for_mover=for_mover,
        part=part,
        trade_date=trade_date,
        entry_notional=entry_notional,
    )


def choose_mover(
    trading: TradingClient,
    at: datetime,
    regular_open: datetime,
    since: date,
    *,
    regular_close: datetime,
    feed: str = MOVER_FEED,
    count: int = 1,
    order_cap: Decimal = MAX_NOTIONAL_PER_ORDER_USD,
    for_mover: bool = False,
    part: str = "regular",
    trade_date: date | None = None,
    entry_notional: Decimal | None = None,
) -> MoverPick:
    """A mover window's name, chosen once before the node is built: `stream_mover` over a short sample of
    the window's own stream on `feed`; if that fails or qualifies no name, `select_mover` over Alpaca's movers
    (`MOVER_SOURCE`), and the record says why (`stream_failed`). `at` is Alpaca's clock at the start; the
    fallback's is moved on by the time the sample took. The fallback ranks the regular session's minutes
    from `regular_open`, so it runs only inside that session: after `regular_close` `reference_close`
    gives today's close, and its move would be the after-hours drift alone against the whole day's
    dollar volume (code review, 2026-11-11); before the open it has no minute to rank on."""
    began = time.monotonic()

    def alpaca_now() -> datetime:
        return at + timedelta(seconds=time.monotonic() - began)

    data, screener, actions = window_data(), window_screener(), window_actions()
    try:
        return stream_mover(
            trading,
            window_stream(feed=feed),
            data,
            screener,
            actions,
            since,
            now=alpaca_now,
            feed=feed,
            count=count,
            order_cap=order_cap,
            for_mover=for_mover,
            part=part,
            trade_date=trade_date,
            entry_notional=entry_notional,
        )
    except (StreamSampleFailed, WindowRefused, *FAILED) as e:
        failed = f"{type(e).__name__}: {e}"
    if (now := alpaca_now()) >= regular_close:
        raise WindowRefused(
            f"no mover: the stream sample failed ({failed}), and Alpaca's movers are not read: the "
            "fallback ranks the regular session only, which closed at "
            f"{regular_close.astimezone(NEW_YORK):%H:%M} New York"
        )
    try:
        pick = select_mover(
            trading,
            data,
            screener,
            actions,
            now,
            regular_open,
            since,
            count=count,
            order_cap=order_cap,
            for_mover=for_mover,
            part=part,
            trade_date=trade_date,
            entry_notional=entry_notional,
        )
    except (
        WindowRefused,
        *FAILED,
    ) as e:  # a refusal too: before the open it has no minute to rank on
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
    The Mover record carries the process's requests by endpoint at the node's start and at the window's
    end (`canon_alpaca.providers.endpoint_counts`).
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
    if not config.mover:
        feed = window_feed(part)
    elif (no_form := no_mover_form(part)) is not None:
        raise WindowRefused(no_form)
    else:
        row = MOVER_PARTS[part]
        feed = row.feed
        if config.session == "extended" or config.strategy == MOVER:
            if row.bridge_reason and config.trigger != "bid_ask":
                raise WindowRefused(
                    f"the {part} part runs the bridge in Mover's place ({row.bridge_reason}), "
                    f"which has no trailing sell for trigger {config.trigger}"
                )
            config = replace(config, strategy=DEFAULT_STRATEGY if row.bridge_reason else MOVER)
        if config.strategy == MOVER:
            if config.trigger in PRINT_TRIGGERS and feed != DataFeed.SIP.value:
                raise WindowRefused(
                    f"trigger {config.trigger} turns on the trade-condition filter, measured on sip "
                    f"prints only, and the {part} part streams {feed}"
                )
            slots_in_use = row.slots if config.slots is None else config.slots
            if slots_in_use + config.spares > MOVER_CHECKS:
                raise WindowRefused(
                    f"{part}'s {slots_in_use} slots plus {config.spares} spares exceed the "
                    f"ranking interface's {MOVER_CHECKS} names"
                )
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
    basis: str | None = None
    pick: MoverPick | None = None
    reference_reader: ReferenceReader | None = None
    reference_read: Future[ReferenceRead] | None = None
    reference_notes: list[str] = []
    entry_contexts: dict[InstrumentId, Day] = {}
    allocation: MoverAllocation | None = None
    loss_share: Decimal | None = None
    profile_document: dict[str, Any] | None = None
    profile_details: dict[str, Any] | None = None
    window_symbols = (config.symbol,)
    if config.mover:  # chosen once, before the node is built (ADR 0083's dated line of 2026-11-11)
        days = _trading_days(calendar)
        now_ny = clock.timestamp.astimezone(NEW_YORK)
        trade_date = now_ny.date()
        if part == "overnight" and now_ny.time() >= OVERNIGHT_STARTS:
            trade_date += timedelta(days=1)
        if trade_date not in days or not (earlier := [d for d in days if d < trade_date]):
            raise WindowRefused(
                "the trade date and a trading day before it are not both on Alpaca's calendar read, so the "
                "open and the last completed session are not known"
            )
        if config.strategy == MOVER:
            # before the allocation and baseline reads and any node: outside paper A and B there is
            # no default (the lead, 16:36)
            loss_share = window_loss_share(paper_account())
            from .movers_rank import NO_BASELINE
            from .movers_rank import read_baselines

            if config.trail_mode == "profile":
                profile_document, profile_details = read_window_trail_profile(
                    config, as_of=trade_date
                )
            baseline = mover_baseline_path(config, feed)
            read = read_baselines(baseline, trade_date)
            if read.absent and part == "overnight":
                # overnight ranks in the dollar tier with no file, until Wave 2's overnight baseline
                # exists; a file that cannot be used still refuses (the lead's ruling, 2026-11-12)
                basis = NO_BASELINE
            elif read.problem:
                raise WindowRefused(
                    f"the rank actor baseline {baseline} is unusable: {read.problem}"
                )
            allocation = mover_allocation(
                trading, row.slots if config.slots is None else config.slots
            )
            try:  # a pre-read that cannot start is named; `references_before_node` then refuses the window
                reference_reader, reference_read = start_reference_read(trading, reference_notes)
            except Exception as e:  # noqa: BLE001
                reference_notes.append(f"not pre-read: {type(e).__name__}: {e}")
        # timed after the reads above (the profile's included), so none of them ages the pick
        at = clock.timestamp + timedelta(seconds=time.monotonic() - read_at)
        pick = choose_mover(
            trading,
            at,
            in_new_york(days[trade_date].open),
            max(earlier),
            regular_close=in_new_york(days[trade_date].close),
            feed=feed,
            count=(row.slots if config.slots is None else config.slots) + config.spares
            if config.strategy == MOVER
            else 1,
            order_cap=allocation.entry_notional
            if allocation is not None
            else MAX_NOTIONAL_PER_ORDER_USD,
            for_mover=config.strategy == MOVER,
            part=part,
            trade_date=trade_date,
            entry_notional=allocation.entry_notional if allocation is not None else None,
        )
        config, price, mover = (
            replace(config, symbol=pick.symbol),
            pick.price,
            pick.record,
        )
        window_symbols = (pick.symbol,)  # `Mover`'s path below widens it to every priced name
        mover["feed"] = feed
        if allocation is not None:
            mover["allocation"] = {**allocation.record(), "loss_share": str(loss_share)}
        if row.bridge_reason:
            mover["fallback"] = {
                "part": part,
                "strategy": DEFAULT_STRATEGY,
                "reason": row.bridge_reason,
            }
        if config.strategy != MOVER:
            for chosen in pick.picks:
                one_share_fits(chosen.symbol, chosen.entry)
    else:
        price = share_price(window_data(), config.symbol)
        one_share_fits(config.symbol, price)
    if config.strategy == MOVER and pick is not None:
        assert allocation is not None
        entry_notional = allocation.entry_notional
        # at the limit `Mover` itself would send, which is what the risk engine values its order at; the
        # record's `entry_limit` is then this strategy's, not the bridge's through the touch
        data = window_data()
        priced: list[MoverPick] = []
        overnight_said: list[str] = []
        for chosen in pick.picks:
            try:
                limit = mover_entry_limit(data, chosen.symbol, feed=feed)
            except (ValueError, WindowRefused) as error:
                mover.setdefault("passed_over", {})[chosen.symbol] = (
                    f"no usable entry limit under the entry figure of {entry_notional} USD: {error}"
                )
                continue
            except FAILED as error:
                # a spare whose read raises is passed over by name; the chosen name's refuses (the
                # lead's ruling of 2026-11-12 on the ultra review of the composition, finding 8)
                if chosen is pick.picks[0]:
                    raise WindowRefused(
                        f"the latest quote of the chosen name {chosen.symbol} could not be read "
                        f"({type(error).__name__}: {error}); not starting"
                    ) from error
                mover.setdefault("passed_over", {})[chosen.symbol] = (
                    f"its latest quote could not be read: {type(error).__name__}: {error}"
                )
                continue
            if limit > entry_notional:
                mover.setdefault("passed_over", {})[chosen.symbol] = (
                    f"one share of {chosen.symbol} at its entry limit of {limit} USD is over {MOVER}'s "
                    f"entry figure of {entry_notional} USD"
                )
                continue
            if part == "overnight":
                # re-checked by name: a spare that fails is passed over, the chosen name's failure
                # refuses (the same ruling, finding 7)
                try:
                    overnight_said.append(overnight_eligibility(trading, chosen.symbol))
                except WindowRefused as error:
                    if chosen is pick.picks[0]:
                        raise
                    mover.setdefault("passed_over", {})[chosen.symbol] = (
                        f"the overnight re-check failed: {error}"
                    )
                    continue
            priced.append(replace(chosen, price=limit, entry=limit, others=()))
        if not priced:
            raise WindowRefused(
                f"no mover name remains within the entry figure: {mover['passed_over']}"
            )
        pick = replace(priced[0], record=mover, others=tuple(priced[1:]))
        config, price = replace(config, symbol=pick.symbol), pick.price
        # the priced name's last trade, not a dropped first pick's (review of 3a8613a, cut list)
        mover.update(
            symbol=pick.symbol,
            inputs=priced[0].record["inputs"],
            price=str(pick.price),
            last_trade_at=priced[0].record.get("last_trade_at"),
        )
        window_symbols = tuple(p.symbol for p in priced)
        mover["entry_limit"] = str(pick.price)
        mover["slots"] = row.slots if config.slots is None else config.slots
        mover["allocation"] = allocation.record()
        mover["spares"] = config.spares
        # the window's selection by its --trigger name; `run.rows.trigger` is Mover's own spelling
        mover["trigger"] = config.trigger
        mover["trade_condition_filter"] = config.trigger in PRINT_TRIGGERS
        mover["validate_trade_sequence"] = config.validate_trade_sequence
        mover["roster_requested"] = mover["slots"] + config.spares
        mover["roster"] = [
            {
                "symbol": p.symbol,
                "instrument_id": f"{p.symbol}.{VENUE}",
                "entry_limit": str(p.price),
                "max_shares": int(entry_notional // p.price),
                "inputs": p.record["inputs"],
            }
            for p in priced
        ]
        arm, reason = mover_entry_arm(part)
        mover["entry_arm"], mover["entry_arm_reason"] = arm, reason
        mover["baseline_path"] = str(mover_baseline_path(config, feed))
        if profile_document is not None and profile_details is not None:
            mover["trail_profile"] = {
                **profile_details,
                "window_instruments": sum(
                    symbol in profile_document["symbols"] for symbol in window_symbols
                ),
            }
        mover["rdc_1"] = "absent"
        mover["trail_mode"] = config.trail_mode
        if basis is not None:
            mover["baseline"] = basis
        mover["ranking_source"] = f"live: ADR 0107's actor over the wildcard stream on {feed}"
        mover["open_findings"] = list(LIVE_OPEN_FINDINGS) + (
            list(PRINT_TRIGGER_FINDINGS) if config.trigger in PRINT_TRIGGERS else []
        )
        if arm == "registered":
            entry_contexts = mover_entry_contexts(
                data,
                window_symbols,
                clock.timestamp + timedelta(seconds=time.monotonic() - read_at),
                calendar,
                feed=feed,
            )
        mover["entry_contexts"] = {
            str(instrument_id): {
                "day": context.day.isoformat(),
                "bars": len(context.bars),
                "earlier_sessions": len(context.earlier),
                "reference": context.reference,
                "reference_from": context.reference_from,
                "covered_to": context.covered_to,
                "missing": context.missing,
            }
            for instrument_id, context in entry_contexts.items()
        }
    if part != "overnight":
        overnight = ""
    elif config.strategy == MOVER and pick is not None:
        overnight = "; ".join(overnight_said)  # re-checked name by name above
    else:
        overnight = "; ".join(overnight_eligibility(trading, symbol) for symbol in window_symbols)
    if config.strategy == MOVER and config.mover:
        mover["references"] = references_before_node(
            reference_read, reference_notes, REFERENCES_BEFORE_NODE_SECS
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
        arm=config.arm,
        arm_default=config.arm_default,
        feed=feed,
        overnight=overnight,
        no_shorting=no_shorting,
        share_price=str(price),
        mover=mover,
        request_budget=request_record(
            (),
            limit_per_minute=config.request_limit_per_minute,
        ),
    )
    # from here a signal marks the record stopping at once, whether or not run_window's `finally` runs
    _SIGNALS.record = (config.log_dir, report)
    initial_snapshot_error = write_record(config.log_dir, report)
    if initial_snapshot_error is not None:
        raise initial_snapshot_error  # the failed record is already on disk; no node was started
    started = time.monotonic()
    started_wall = datetime.now(UTC)
    abandoned_before = len(_ABANDONED)
    failure: BaseException | None = None
    ran = False  # whether the node was run: only then is the gap marker written
    cache = None
    result_inputs: tuple[list, list, Decimal] | None = None  # a Mover's fills, entries and figure
    unsettled: dict[str, tuple[str, str]] = {}
    unsettled_symbols: dict[str, str] = {}
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
            slots=(row.slots if config.slots is None else config.slots)
            if config.strategy == MOVER
            else None,
            part=part,
            entry_contexts=entry_contexts,
            entry_notional=None if allocation is None else allocation.entry_notional,
            reference_reader=reference_reader,
            loss_share=loss_share,
            profile_document=profile_document,
        )
        cache = node.cache
        startup_requests = endpoint_counts()
        try:
            ran = True
            _run_node(node, ends)
        finally:
            try:
                report.seconds = round(time.monotonic() - started, 1)
                report.overrun = overrun(started_wall, time.monotonic() - started, ends)
                if isinstance(strategy, Mover):
                    # Before any cache read can fail or disposal can clear it: the account check
                    # must include names that this strategy subscribed after node startup.
                    window_symbols = tuple(
                        dict.fromkeys(
                            (
                                *window_symbols,
                                *(i.symbol.value for i in sorted(strategy.opened_slots, key=str)),
                            )
                        )
                    )
                _fill(report, cache, strategy_id)
                report.dropped = list(strategy.dropped)
                report.warm_up_bars = strategy.history_bars
                report.warm_up_dropped = strategy.history_dropped
                report.quotes, report.trades = strategy.quotes, strategy.trades
                report.live_bars = strategy.live_bars
                if isinstance(strategy, Mover):
                    report.remakes_under_floor = strategy.remakes_under_floor
                caps = cap if isinstance(cap, tuple) else (cap,)
                report.cap_checks = sum(c.checks for c in caps)
                report.cap_peak = str(max((c.peak for c in caps), default=Decimal(0)))
                report.cap_tripped = next((c.tripped for c in caps if c.tripped), None)
                if config.strategy == MOVER:
                    report.mover["position_caps"] = {
                        str(c.instrument_id): {
                            "max_position": str(c.max_position),
                            "checks": c.checks,
                            "peak": str(c.peak),
                            "tripped": c.tripped,
                        }
                        for c in caps
                    }
                if isinstance(strategy, Mover):
                    report.mover["run"] = run = strategy.record()
                    report.mover.update(clock_notes(run))
                    try:  # the lead's order of 2026-11-11; one that cannot be read is named
                        result_inputs = (
                            strategy_fills(cache, strategy_id),
                            run["entries"],
                            strategy.settings.max_notional,
                        )
                        report.mover["result"] = trade_result(
                            result_inputs[0], result_inputs[1], entry_figure=result_inputs[2]
                        )
                    except Exception as e:  # noqa: BLE001
                        report.mover["result"] = f"not read: {type(e).__name__}: {e}"
                    report.mover["sample_ranked_table"] = ranked_table(
                        report.mover, run, tier="unavailable: the stream sample records no tier"
                    )
                    report.mover["ranked_table"] = live_ranked_table(run)
                    report.mover["stream_symbols"] = stream_symbols(
                        run, sampled=(report.mover.get("sample") or {}).get("symbols")
                    )
                    report.mover["passed_over_no_context"] = passed_over_without_context(run)
                    report.mover["dollar_basis"] = dollar_basis()
                    # the window's end count is taken again at the final snapshot, after the stop's
                    # flattening (`_snapshot_requests`)
                    report.mover["http_requests"] = {
                        "to_node_start": startup_requests,
                        "to_window_end": endpoint_counts(),
                    }
                    try:  # a record that cannot be read is named, and fails nothing
                        report.mover["entries_by_roster"] = roster_entries(run)
                    except Exception as e:  # noqa: BLE001
                        report.mover["entries_by_roster"] = f"not read: {type(e).__name__}: {e}"
                    for key in ("entries_by_trail_source", "warm_up"):  # the END line's (F-2)
                        report.mover[key] = run.get(key, "not recorded by the strategy")
                    from .movers_rank_actor import publication_lags

                    report.mover["publication_lags"] = publication_lags(_records(run))
                    report.mover["reference_read"] = list(reference_notes)
                    report.mover["subscriptions"] = subscriptions(
                        run, feed, report.mover["stream_symbols"].get("assets_loaded")
                    )
                    # diagnostics: one that cannot be read is named, and fails nothing; the entry
                    # rows stand on their own, so a protection failure does not discard them
                    try:
                        report.mover["entries"] = mover_entries(run, cache, pick)
                    except Exception as e:  # noqa: BLE001
                        report.mover["entries"] = f"not read: {type(e).__name__}: {e}"
                    if isinstance(report.mover["entries"], str):
                        report.mover["protection_by_part"] = (
                            "not read: the entry rows were not read"
                        )
                    else:
                        try:
                            report.mover["entries"], report.mover["protection_by_part"] = (
                                protection_diagnostics(
                                    report.mover["entries"],
                                    cache.orders(strategy_id=strategy_id),
                                    part=part,
                                    end_ns=strategy.clock.timestamp_ns(),
                                )
                            )
                        except Exception as e:  # noqa: BLE001
                            report.mover["protection_by_part"] = (
                                f"not read: {type(e).__name__}: {e}"
                            )
            finally:
                try:
                    # before the disposal, which clears the cache
                    unsettled = _unsettled(cache, strategy_id)
                    unsettled_symbols = _unsettled_symbols(cache, strategy_id, unsettled)
                finally:
                    if len(_ABANDONED) == abandoned_before:  # else its abandoned task owns it
                        node.dispose()
    except BaseException as e:
        failure = e
        _mark_request_interrupted(report, e)
        raise
    finally:
        _SIGNALS.left_node()  # the wind-down, whether or not the node ran: a signal now is only noted
        try:
            _wait_for_calls(_ABANDONED[abandoned_before:], report)
            if cache is not None and len(_ABANDONED) > abandoned_before:
                # after the wait: an abandoned node's last submits are in it
                unsettled = _unsettled(cache, strategy_id)
                unsettled_symbols = _unsettled_symbols(cache, strategy_id, unsettled)
            for client_order_id, (side, why) in unsettled.items():
                report.unsettled[client_order_id] = [f"{side}: {why}"]
            report.left = _flatten_after_stop(
                trading,
                config,
                report,
                unsettled={c: side for c, (side, _) in unsettled.items()},
                calendar=calendar,
                calendar_on=clock.timestamp.astimezone(NEW_YORK).date(),
                data=window_data,
                symbols=window_symbols,
                unsettled_symbols=unsettled_symbols,
            )
        except BaseException as e:  # the account could not be checked: never recorded as ok
            failure = failure or e
            _mark_request_interrupted(report, e)
            report.errors.append(f"checking the account after the stop: {type(e).__name__}: {e}")
            raise
        else:
            if result_inputs is not None and report.check_fills:
                # the check's own closes are the window's too (review of fire-m-c, finding 11); a
                # result that cannot be read is named and fails nothing, as the first reading
                fills, sent, figure = result_inputs
                try:
                    report.mover["result"] = trade_result(
                        [*fills, *report.check_fills], sent, entry_figure=figure
                    )
                    report.mover["result"]["check_closes"] = len(report.check_fills)
                except Exception as e:  # noqa: BLE001
                    report.mover["result"] = f"not read: {type(e).__name__}: {e}"
        finally:
            snapshot_taken = False
            if len(_ABANDONED) > abandoned_before:
                # an abandoned node's calls may still send after the snapshot: not a full measurement
                report.request_budget["measurement_status"] = "interrupted"
            try:
                if ran:
                    marker.write_text(str(time.time()))
                    _read_stream_lines(report, config.log_dir / f"window-{stamp}.log")
                snapshot_error = _snapshot_requests(report)
                snapshot_taken = True
                if snapshot_error is not None and not isinstance(snapshot_error, Exception):
                    raise snapshot_error  # Ctrl-C or SystemExit during the snapshot
                no_data = _data_failure(report) if ran else None
                over_budget = (
                    request_failure(report)
                    if config.strategy == MOVER and snapshot_error is None
                    else None
                )
                if failure is not None:
                    report.result = f"failed: {type(failure).__name__}: {failure}"
                elif report.left:
                    report.result = f"failed: not left flat: {report.left}"
                elif report.cap_tripped:
                    report.result = f"failed: {report.cap_tripped}"
                elif snapshot_error is not None:
                    # the account's outcome above outranks a bookkeeping error; it is raised below
                    failure = snapshot_error
                    report.result = f"failed: {type(failure).__name__}: {failure}"
                elif no_data:
                    report.result = f"failed: {no_data}"
                elif over_budget:
                    report.result = f"failed: {over_budget}"
                elif _SIGNALS.first is not None:  # it came after the node: the wind-down finished
                    report.result = (
                        f"failed: WindowSignalled: {_SIGNALS.first} after the node stopped; "
                        "the wind-down finished"
                    )
                else:
                    report.result = "ok"
                if _SIGNALS.first is not None and "WindowSignalled" not in report.result:
                    report.errors.append(f"{_SIGNALS.first} received; the wind-down finished")
            except BaseException as e:
                failure = failure or e
                _mark_request_interrupted(report, e)
                report.result = f"failed: {type(failure).__name__}: {failure}"
                raise
            finally:
                report.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                if not snapshot_taken:
                    _snapshot_requests(report)  # preserve the primary preparation failure, if any
                write_record(config.log_dir, report, refresh_requests=False)
    if report.left:
        raise RuntimeError(f"the paper account was not left flat: {report.left}")
    if report.cap_tripped:
        raise RuntimeError(f"the position cap stopped the window: {report.cap_tripped}")
    if snapshot_error is not None:
        raise snapshot_error
    if no_data:
        raise RuntimeError(f"the window ran without market data: {no_data}")
    if over_budget:
        raise RuntimeError(f"the window went over its request budget: {over_budget}")
    return report


def overrun(started_wall: datetime, ran: float, ends: datetime) -> str:
    """What `WindowReport.overrun` says of a node run that began at `started_wall` and ran `ran` seconds
    of the monotonic clock: "" unless the wall clock ran more than `SLEPT_SECS` ahead of it."""
    now = datetime.now(UTC)
    slept = (now - started_wall).total_seconds() - ran
    if slept <= SLEPT_SECS:
        return ""
    past = (now - ends).total_seconds()
    when = (
        f"its stop ended {past:.0f} s past the window's end"
        if past > 0
        else f"it woke {-past:.0f} s before the window's end"
    )
    return f"the process was suspended for about {slept:.0f} s (a machine sleep); {when}"


def mover_entry_contexts(
    data: StockHistoricalDataClient,
    symbols: Sequence[str],
    at: datetime,
    calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None,
    *,
    feed: str,
) -> dict[InstrumentId, Day]:
    """Supply ADR 0092's registered arm with each name's known Day, before the node's bars.

    alpaca-py 0.45.0 `StockBarsRequest`/`get_stock_bars` (`data/requests.py:85-106`,
    `data/historical/stock.py:75-99`) reads split-adjusted minute history on the part's feed in one
    paginated request, and the last completed regular daily reference on SIP in another. Current-day
    bars begin at 04:00 New York and end before this minute; earlier calendar sessions are latest first,
    at most the registered arm's baseline length, and a name with fewer is missing. Sparse minutes stay
    sparse. A failed read raises;
    missing reference/calendar input stays named in Day.missing and never borrows the startup sample's
    reference. A successful requested range supplies coverage, including minutes without a trade.
    """
    from . import early_entry_study as study

    now = at.astimezone(NEW_YORK)
    today = now.date()
    covered_to = min(study.WINDOW_END, max(study.PART_START, now.hour * 60 + now.minute))
    sessions = sorted((d for d in _trading_days(calendar) if d < today), reverse=True)[
        : study.ARMS["registered"].baseline_sessions
    ]
    if not sessions:
        return {
            InstrumentId.from_str(f"{symbol}.{VENUE}"): study.Day(
                symbol,
                today,
                (),
                (),
                None,
                covered_to=covered_to,
                missing="no earlier calendar session",
            )
            for symbol in symbols
        }
    minute = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Minute,
            start=datetime.combine(sessions[-1], dt_time(4), NEW_YORK),
            end=now.replace(second=0, microsecond=0) - timedelta(microseconds=1),
            feed=DataFeed(feed),
            adjustment=Adjustment.SPLIT,
        )
    )
    daily = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=datetime.combine(sessions[0], dt_time(0), NEW_YORK),
            end=datetime.combine(today, dt_time(0), NEW_YORK) - timedelta(microseconds=1),
            feed=DataFeed.SIP,
            adjustment=Adjustment.SPLIT,
        )
    )
    if not isinstance(minute, BarSet) or not isinstance(daily, BarSet):
        raise TypeError("alpaca-py returned raw history data; the client must use raw_data=False")
    contexts: dict[InstrumentId, Day] = {}
    for symbol in symbols:
        by_day: dict[date, list[study.Minute]] = {}
        for bar in sorted(minute.data.get(symbol, ()), key=lambda b: b.timestamp):
            opened = bar.timestamp.astimezone(NEW_YORK)
            clock_minute = opened.hour * 60 + opened.minute
            if (
                opened.date() not in (*sessions, today)
                or not study.PART_START <= clock_minute < study.WINDOW_END
                or opened + timedelta(minutes=1) > now.replace(second=0, microsecond=0)
            ):
                continue
            by_day.setdefault(opened.date(), []).append(
                study.Minute(clock_minute, bar.open, bar.high, bar.low, bar.close, bar.volume)
            )
        references = [
            b
            for b in daily.data.get(symbol, ())
            if b.timestamp.astimezone(NEW_YORK).date() == sessions[0]
        ]
        reference = None if not references else max(references, key=lambda b: b.timestamp).close
        wanted = study.ARMS["registered"].baseline_sessions
        if reference is None or reference <= 0:
            missing = "no prior-session reference close"
        elif len(sessions) < wanted:  # never a normal over fewer sessions than the arm registered
            missing = (
                f"only {len(sessions)} earlier sessions on the calendar read; the registered arm "
                f"takes {wanted}"
            )
        else:
            missing = None
        contexts[InstrumentId.from_str(f"{symbol}.{VENUE}")] = study.Day(
            symbol,
            today,
            tuple(by_day.get(today, ())),
            tuple(tuple(by_day.get(session, ())) for session in sessions),
            reference,
            covered_to=covered_to,
            missing=missing,
            reference_from=f"split-adjusted SIP daily bar of {sessions[0]}",
        )
    return contexts


def mover_entries(run: Mapping[str, Any], cache, pick: MoverPick | None) -> list[dict[str, Any]]:
    """Each entry's two diagnostics (`mover_window.entry_diagnostics`), from the strategy's record, the
    bars the node built (read before the node is disposed, which clears its cache) and the reference
    close the window's choice read for its name (`movers_stream.Scored.reference`)."""
    entries = run.get("entries", [])
    references = {}
    for chosen in () if pick is None else pick.picks:
        reference = chosen.record.get("inputs", {}).get("reference")
        if reference is not None:
            references[f"{chosen.symbol}.{VENUE}"] = Decimal(str(reference))
    bars = window_bars(cache, [str(e["instrument_id"]) for e in entries])
    return entry_diagnostics(entries, run.get("fills", []), bars, references)


def mover_entry_limit(
    data: StockHistoricalDataClient, symbol: str, *, feed: str = MOVER_FEED
) -> Decimal:
    """The limit `Mover` would enter `symbol` at now: the ask of its latest quote on `feed` (alpaca-py's
    `get_stock_latest_quote`) as `price_step.entry_price` makes it a price Alpaca takes; or `WindowRefused`.
    The risk engine values a limit order at its limit price, not at the last trade, so this is the
    price one share is checked at and the shares of an entry are counted at."""
    found = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed(feed))
    )
    quote = found.get(symbol) if isinstance(found, dict) else None
    ask = getattr(quote, "ask_price", None)
    if not ask:
        raise WindowRefused(
            f"Alpaca gave no ask in {symbol}'s latest {feed} quote, so the limit an entry would be sent "
            "at is not known; not starting"
        )
    return entry_price(Decimal(str(ask)), 0)


REQUEST_CEILING = PAPER_REQUEST_CEILING
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


STREAM_LINES = ("Alpaca stream on feed ", "Cannot subscribe: ", "NotImplementedError")
"""How the adapter's data client begins each line it logs about its stream: alpaca-py's own warnings and
errors, relayed from the stream's thread, the stream's end by itself, and a subscription it refused, a
feed other than its stream's among them (`canon_alpaca.data`, ADR 0055; the package README)."""
LOG_LEVEL = re.compile(r"^\S+ \[(?P<level>[A-Z]+)\] ")
"""The level of a line of the node's plain-text log, `<timestamp> [LEVEL] <component>: <message>`."""
MAX_STREAM_LINES = 20


def _read_stream_lines(report: WindowReport, log: Path) -> None:
    """Copy the adapter's stream lines from the node's log (`STREAM_LINES`, at most `MAX_STREAM_LINES`,
    then a count) into `report.stream`: the log is where the adapter reports a stream Alpaca refused or
    that ended, since the subscription has returned by then (ADR 0055). Count, apart from them, the
    instrument notifications upstream refused for a full queue (`report.instrument_refusals`). A log
    that cannot be read is noted in `errors`."""
    # The node's logger writes on its own thread; wait until what it was sent is on disk
    # (`logging_sync_to_disk`, as the adapter's tests do).
    logging_sync_to_disk()
    try:
        lines = [line.strip() for line in log.read_text(errors="replace").splitlines()]
    except OSError as e:
        _note(report, f"reading the node log for the stream's own lines: {type(e).__name__}: {e}")
        return
    report.instrument_refusals = sum(
        all(mark in line for mark in INSTRUMENT_REFUSAL) for line in lines
    )
    found = [line for line in lines if any(mark in line for mark in STREAM_LINES)]
    report.stream = found[:MAX_STREAM_LINES]
    if len(found) > MAX_STREAM_LINES:
        report.stream.append(f"... and {len(found) - MAX_STREAM_LINES} more such lines")


def _data_failure(report: WindowReport) -> str | None:
    """Why the window's market data cannot be trusted, or None: the adapter logged an error about its
    stream (it ended by itself, or Alpaca refused it), or the stream delivered no quote and no trade for
    the symbol in the whole window. A window with quotes but no trade, and so no bar, is a quiet market,
    not a failure: overnight that is normal (the record's `live_bars` shows it). Mover always needs a
    published actor generation; with no slot ever opened that proves delivery without quote/trade
    subscriptions, otherwise an empty slot stream names all the symbols actually subscribed.
    """
    if errors := [
        line
        for line in report.stream
        if (level := LOG_LEVEL.match(line)) is not None and level["level"] == "ERROR"
    ]:
        return f"the {report.feed} data stream reported an error: {errors[0]}"
    if unsupported := next((line for line in report.stream if "NotImplementedError" in line), None):
        return f"an unimplemented data subscription: {unsupported}"
    symbols = report.symbol
    if report.strategy == MOVER:
        run = report.mover.get("run", {})
        records = run.get("ranking_records", [])
        if not any(record.get("published") is True for record in records):
            reasons = records[-1].get("reasons", []) if records else ["the actor recorded no tick"]
            return f"the rank actor produced no published generation: {reasons}"
        if not (opened := run.get("opened_slots", [])):
            return None  # the actor saw bars, but the strategy never subscribed to quotes/trades
        symbols = ", ".join(InstrumentId.from_str(instrument).symbol.value for instrument in opened)
    if not report.quotes and not report.trades:
        return (
            f"no market data: the {report.feed} stream delivered no quote and no trade for "
            f"{symbols} in {report.seconds} s"
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
    extended: bool = False
    """A close of the check's own sent outside the regular session: a marketable DAY limit marked
    `extended_hours` (`_AccountCheck._close`), left to work `LIMIT_TIMEOUT_SECS` and never past the
    check's end."""
    sent_at: float = 0.0
    """When the check sent it (`time.monotonic`)."""
    symbol: str = ""
    """The window name this request belongs to; empty until known, so it can block any name's close."""

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
        symbol: str | Sequence[str],
        report: WindowReport,
        unsettled: Mapping[str, str],
        *,
        calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None = None,
        calendar_on: date | None = None,
        data: Any = None,
        unsettled_symbols: Mapping[str, str] | None = None,
    ) -> None:
        self.trading = trading
        self.symbols = tuple(dict.fromkeys((symbol,) if isinstance(symbol, str) else symbol))
        self.report = report
        self.calendar = calendar
        """Alpaca's calendar as the window read it at its start, which places the extended session's
        parts (`session_part`); read here only if a close outside the regular session needs it and the
        clock's day is not the one it was read on (`calendar_on`; a window that slept into another day)."""
        self.calendar_on = calendar_on
        self.extended_refused: set[str] = set()
        """The names whose close outside the session Alpaca refused (an asset not tradable overnight,
        say): no other is sent for them in this check."""
        self.data = data
        """alpaca-py's market data client, or a callable that builds one, for the quote a close outside
        the regular session is priced from; built on the first such close."""
        self.clock: AlpacaClock | None = None  # the clock's last answer
        known_symbols = unsettled_symbols or {}
        self.requests = {
            c: _Unsettled(c, side, symbol=known_symbols.get(c, "")) for c, side in unsettled.items()
        }
        self.turn = deque(self.requests)  # whose lookup comes next
        self.closes_sent = 0
        self.close_turn = False
        """Whether the next poll with an order working closes instead of canceling: polls alternate, so
        an order that never ends cannot hold another name's close (the lead's ruling of 2026-11-12)."""
        self.ended: set[str] = set()  # Alpaca's IDs of the orders it showed in a terminal status
        self.listed: list = []  # the open orders in the window symbols of the last reading
        self.held = dict.fromkeys(self.symbols, Decimal(0))
        self.others: list = []  # the open orders in other symbols of the last reading
        self.others_held: dict[str, Decimal] = {}  # the positions in other symbols, likewise
        self.tried: dict[str, int] = {}  # the cancels sent for each order
        self.close_tries: Counter[str] = Counter()  # the closes sent or left unpriced in each name
        self.open_until: float | None = None  # by the clock's last answer; None before any
        for client_order_id, side in unsettled.items():
            report.unsettled.setdefault(client_order_id, [side or "side not known"])

    def read(self) -> list[str]:
        """One reading: look up to `LOOKUPS` unsettled requests up, then read the account's open orders,
        which show a request as its lookup does, and its positions; return what is left (`left`). The
        reading is of the whole account; only the window symbols' orders and positions are the check's to act
        on. A lookup that fails is recorded and the reading goes on; a failed read of the orders or the
        positions raises."""
        self._look_up()
        listed = open_orders(self.trading)
        self.listed = [o for o in listed if o.symbol in self.symbols]
        self.others = [o for o in listed if o.symbol not in self.symbols]
        for order in self.listed:  # the listing shows a request as its lookup does
            if (request := self.requests.get(str(order.client_order_id))) is not None:
                self._seen(request, order)
        self.others_held = positions(self.trading)
        self.held = {s: self.others_held.pop(s, Decimal(0)) for s in self.symbols}
        return self.left()

    def left(self) -> list[str]:
        """What keeps the account from being flat, as last read: each working order and the position in
        the symbol, each unsettled request Alpaca has not shown, then what other symbols hold."""
        return (
            [item for s, qty in self.held.items() for item in described(self.working(s), qty, s)]
            + [r.missing() for r in self.requests.values() if r.order is None]
            + elsewhere(self.others, self.others_held)
        )

    def working(self, symbol: str | None = None) -> list:
        """Every order known to work: the open orders last listed, and each unsettled request Alpaca
        showed working that the listing lacks (the two need not agree); none Alpaca has shown ended."""
        orders = {str(o.id): o for o in self.listed}
        for request in self.requests.values():
            if request.order is not None:
                orders.setdefault(str(request.order.id), request.order)
        return [
            o
            for order_id, o in orders.items()
            if order_id not in self.ended and (symbol is None or o.symbol == symbol)
        ]

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
        request.symbol = str(getattr(order, "symbol", "")) or request.symbol
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
            filled, price = (
                getattr(order, "filled_qty", None),
                getattr(order, "filled_avg_price", None),
            )
            if request.own and filled and price and Decimal(str(filled)) > 0:
                self.report.check_fills.append(
                    (
                        request.client_order_id,
                        f"{request.symbol}.{VENUE}",
                        str(_value(getattr(order, "side", None) or request.side)).upper(),
                        str(filled),
                        str(price),
                    )
                )
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
        self.clock = clock
        if clock.is_open:
            self.open_until = asked_at + (clock.next_close - clock.timestamp).total_seconds()
        else:
            self.open_until = asked_at
        return bool(clock.is_open)

    def calendar_due(self) -> bool:
        """Whether `extended_part` must read the calendar: none is held, or it was read on another day than
        the clock's last answer."""
        assert self.clock is not None
        today = self.clock.timestamp.astimezone(NEW_YORK).date()
        return self.calendar is None or (self.calendar_on is not None and self.calendar_on != today)

    def extended_part(self) -> str:
        """The part of the extended session open by the clock's last answer (`session_part`): `pre-market`,
        `after-hours` or `overnight`, or "" with the reason noted. The calendar is the window's own while
        the clock's day is the one it was read on, else read here, as `session_now` reads it."""
        assert self.clock is not None
        today = self.clock.timestamp.astimezone(NEW_YORK).date()
        if self.calendar is not None and self.calendar_on is None:
            self.calendar_on = today  # given with no day: taken as today's
        if self.calendar is None or self.calendar_on != today:
            days = self.trading.get_calendar(
                GetCalendarRequest(start=today - CALENDAR_BACK, end=today + timedelta(days=1))
            )
            self.calendar, self.calendar_on = (list(days) if isinstance(days, list) else []), today
        try:
            return session_part("extended", self.clock, self.calendar)[1]
        except RuntimeError as e:
            _note(self.report, f"not closing the position: no part of a session is open: {e}")
            return ""

    def cancel(self, *, final: bool = False) -> int:
        """Cancel what works, those not yet tried first: `CANCELS` orders in a poll; as the check ends,
        every one, a further cancel starting only while the step has taken under the time `CANCELS - 1`
        requests may, so the step stays inside the `CANCELS` requests counted for it. A close of the
        check's own is left to fill while it still closes the position held and the session is open; one
        sent outside the regular session (`extended`) for `LIMIT_TIMEOUT_SECS`, as the strategy leaves
        its own limits, then canceled, so the next is priced from a fresh quote, and canceled as the check
        ends, so none Alpaca has shown works once the process has gone. Returns the cancels sent."""
        unwanted, wanted = [], []
        for order in self.working():
            request = self.requests.get(str(order.client_order_id))
            held = self.held.get(order.symbol, Decimal(0))
            closes = (
                request is not None and request.own and held and request.side == _closing_side(held)
            )
            if closes and request is not None and request.extended:
                if final or time.monotonic() - request.sent_at >= LIMIT_TIMEOUT_SECS:
                    unwanted.append(order)
                continue
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
        sent = 0
        for order in unwanted:
            in_time = time.monotonic() - began < (CANCELS - 1) * REQUEST_SECS
            if sent >= CANCELS and not (final and in_time):
                break
            self.tried[str(order.id)] = self.tried.get(str(order.id), 0) + 1
            sent += 1
            try:
                self.trading.cancel_order_by_id(order.id)
            except FAILED as e:  # it filled or ended meanwhile, or the cancel was lost
                _note(self.report, f"canceling {order.client_order_id}{when}: {e}")
        return sent

    def act(self) -> bool:
        """One step toward a flat account after a reading; False once nothing is left to do or to look
        for. With an order working, cancel (`cancel`) and send no close in its symbol: a held order keeps
        its shares from one. Close another name's position if nothing sent and not found could close it
        too and Alpaca's clock, read now, says the session is open; outside it, through a limit priced from
        the name's quote. A name with a working order or no price is passed for this poll, and the names
        tried least come first, so one name that cannot finish never holds the others' closes. A poll
        sends at most seven trading requests (`POLL_SECS`): one that sent a cancel sends no close, one
        that read the calendar no quote and no close, and a quote that fails ends the poll. With an order
        working and another name to close, polls alternate between canceling and closing (`close_turn`),
        so an order whose cancel never takes cannot hold every other name's close."""
        working = self.working()
        busy = {o.symbol for o in working}
        closable = any(
            held and symbol not in busy and symbol not in self.extended_refused
            for symbol, held in self.held.items()
        )
        if working and not (self.close_turn and closable) and self.cancel():
            # no close in a poll that canceled: its clock read and close would pass the seven requests
            # a poll counts (`POLL_SECS`); the close goes out in the next poll
            self.close_turn = True
            return True
        self.close_turn = False
        pending = bool(working)
        if not any(self.held.values()):
            # requests Alpaca has not shown: look again; else only other symbols' are left, not the
            # window's to act on
            return pending or bool(self.requests)
        open_now: bool | None = None
        part: str | None = None
        for symbol, held in sorted(self.held.items(), key=lambda item: self.close_tries[item[0]]):
            if not held or symbol in self.extended_refused:
                continue
            if symbol in busy:
                continue
            side = _closing_side(held)
            if waiting := [
                r.client_order_id
                for r in self.requests.values()
                if r.order is None and r.side in (side, "") and r.symbol in ("", symbol)
            ]:
                _note(
                    self.report,
                    f"not closing the position while {', '.join(waiting)}, sent and not found at Alpaca, "
                    f"may still be placed: a second {side} is not sent ({symbol})",
                )
                continue
            # one fresh clock read and at most one close per poll, for every universe size
            if open_now is None:
                open_now = self.session_open(fresh=True)
            if open_now:
                self.close_tries[symbol] += 1
                self._close(symbol, side)
                return True
            # Outside the regular session a market order would only queue: Alpaca takes a limit order
            # marked extended hours, DAY or GTC, in pre-market, after-hours and overnight (module header).
            if part is None:
                calendar_read = self.calendar_due()
                part = self.extended_part()
                if part and calendar_read:
                    # the calendar took this poll's request; the quote and close come next
                    return True
                if part:
                    try:
                        window_feed(part)
                    except RuntimeError as e:  # no feed set for the part, so no quote to price from
                        _note(self.report, f"not closing the position: {e}")
                        part = ""
            if not part:
                return pending or bool(self.requests)
            if self.data is None or callable(self.data):
                self.data = (self.data or window_data)()
                send_once(self.data)
            try:
                limit, quote = close_limit(self.data, symbol, side, part)
            except FAILED as e:  # looked for again in the next poll, after the other names
                _note(
                    self.report, f"no price for a {part} close: {type(e).__name__}: {e} ({symbol})"
                )
                self.close_tries[symbol] += 1
                return True  # one quote a poll (`POLL_SECS`); the names tried least come first next
            self.close_tries[symbol] += 1
            self._close(symbol, side, limit=limit, quote=f"{part}, {quote}")
            return True
        return pending or bool(self.requests)

    def _close(
        self, symbol: str, side: str, *, limit: Decimal | None = None, quote: str = ""
    ) -> None:
        """Send a close for the held quantity in `symbol` under a client order ID of the check's own,
        which finds it again whatever becomes of the answer: in the regular session a DAY market order;
        outside it a DAY limit at `limit`, marked `extended_hours`. A close that placed nothing (Alpaca's
        refusal, or a connect timeout on its only attempt) is settled at once and counted; any other
        failure leaves it unsettled."""
        qty = abs(self.held[symbol])
        self.closes_sent += 1
        client_order_id = (
            f"{CLOSE_ID_PREFIX}{self.report.started}-{self.closes_sent}-{uuid4().hex[:8]}"
        )
        request = _Unsettled(
            client_order_id,
            side,
            own=True,
            extended=limit is not None,
            sent_at=time.monotonic(),
            symbol=symbol,
        )
        self.requests[client_order_id] = request
        self.turn.appendleft(client_order_id)  # looked up first in the next reading
        if limit is None:
            what = f"sent: {side} {qty} {symbol} at market"
            order_request: MarketOrderRequest | LimitOrderRequest = MarketOrderRequest(
                symbol=symbol,
                qty=float(qty),
                side=AlpacaOrderSide(side),
                time_in_force=AlpacaTimeInForce.DAY,
                client_order_id=client_order_id,
            )
        else:
            what = f"sent: {side} {qty} {symbol} limit {limit}, DAY, extended hours ({quote})"
            order_request = LimitOrderRequest(
                symbol=symbol,
                qty=float(qty),
                side=AlpacaOrderSide(side),
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=float(limit),
                extended_hours=True,
                client_order_id=client_order_id,
            )
        log = self.report.closes.setdefault(client_order_id, [what])
        try:
            order = counted(self.trading.submit_order)(order_request)
        except FAILED as e:
            refused = isinstance(e, APIError) and e.status_code in DEFINITIVE_SUBMIT_REFUSALS
            if refused or not_sent(e):  # Alpaca's own answer, or nothing sent: nothing was placed
                del self.report.closes[client_order_id]
                self.report.closes_not_placed += 1
                self._settle(request)
                # nothing works, so the refusal is of the close itself (an asset not tradable in the
                # part): sending it again each poll would be refused again
                if refused and request.extended:
                    self.extended_refused.add(request.symbol)
            elif isinstance(e, APIError):
                log.append(f"no answer: HTTP {e.status_code}")
            else:
                log.append(f"no answer: {type(e).__name__}")
            raise
        if getattr(order, "id", None) is None:
            log.append("no answer: no order in the response")
            return
        self._seen(request, order)


class QuoteTooWide(ValueError):
    """`close_limit`'s refusal of a quote too wide to close on. Its text is the window's own (the bid,
    the ask, the symbol), never an answer's body, so a report may print it."""


PENNY_NAME_ASK = Decimal("0.10")
"""A sell close under Alpaca's finest step goes at the lowest step only when the ask is also under
this: a name truly worth pennies. With a higher ask the bid alone pulls the limit far under the
market, so the close is refused as a quote too wide to close on (the lead, 2026-11-12)."""


def close_limit(data: Any, symbol: str, side: str, part: str) -> tuple[Decimal, str]:
    """The limit of a close of `side` in `part` outside the regular session: `LIMIT_OFFSET` through the
    touch of alpaca-py's latest quote on the part's feed (`get_stock_latest_quote`, `window_feed`),
    put on Alpaca's step by `price_step` (a sell down, a buy up), and the quote it came from, with its
    time, for the record. A sell under the finest step goes at 0.0001 and the record says so, when the
    ask is under `PENNY_NAME_ASK`; with a higher ask, or none, it is refused as too wide a quote.
    `ValueError` with no price on the side the close takes; `window_feed`'s error for a part with no
    feed. The account check after a stop and `custody`'s close-only form both price this way."""
    feed = window_feed(part)
    found = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed(feed))
    )
    quote = found.get(symbol) if isinstance(found, dict) else None
    if quote is None:
        raise ValueError(f"no latest {feed} quote in {symbol}")
    bid = Decimal(str(getattr(quote, "bid_price", None) or 0))
    ask = Decimal(str(getattr(quote, "ask_price", None) or 0))
    touch, name = (bid, "bid") if side == "sell" else (ask, "ask")
    if touch <= 0:
        raise ValueError(f"the latest {feed} quote in {symbol} has no {name} ({touch})")
    raw = touch - LIMIT_OFFSET if side == "sell" else touch + LIMIT_OFFSET
    # a sell down and a buy up onto Alpaca's step, above the tick scheme's top too: a close is never
    # refused there (the lead's ruling (ii), 2026-11-12), where a buy's entry is
    if side == "buy":
        # up to the finest step first: `up_on_step`'s nudge would take a finer value to the tick under it
        limit = price_step.up_on_step(raw.quantize(price_step.SUB_DOLLAR, rounding=ROUND_CEILING))
    else:
        limit = price_step.down_on_step(raw) if raw > 0 else Decimal(0)
    clamp = ""
    if limit < price_step.SUB_DOLLAR:
        under = f"the {name} {touch} less {LIMIT_OFFSET} is {raw}, under Alpaca's finest step"
        if not 0 < ask < PENNY_NAME_ASK:
            # the bid alone says pennies, the ask does not: refused, and the next poll asks again
            raise QuoteTooWide(
                f"{under}, and the ask {ask} is not under {PENNY_NAME_ASK}: a quote too wide to "
                f"close on ({symbol})"
            )
        # a name worth pennies: a close exists to close, so it goes at the lowest step and says so
        limit = price_step.SUB_DOLLAR
        clamp = f"; {under}, so the close is sent at the lowest, {limit}"
    # Already on the step: express that step's decimal places in the close record.
    limit = limit.quantize(price_step.tick(limit))
    at = getattr(quote, "timestamp", None)
    if isinstance(at, datetime):  # alpaca-py's are aware; a naive one is taken as UTC
        when = f"at {(at.astimezone(UTC) if at.tzinfo else at):%H:%M:%S} UTC"
    else:
        when = "with no time given"
    return limit, f"{feed} quote {when}, bid {bid}, ask {ask}{clamp}"


def _closing_side(held: Decimal) -> str:
    return "sell" if held > 0 else "buy"


def _flatten_after_stop(
    trading: TradingClient,
    config: WindowConfig,
    report: WindowReport,
    *,
    unsettled: Mapping[str, str] | None = None,
    calendar: AlpacaCalendar | Sequence[AlpacaCalendar] | None = None,
    calendar_on: date | None = None,
    data: Any = None,
    symbols: Sequence[str] | None = None,
    unsettled_symbols: Mapping[str, str] | None = None,
) -> list:
    """What the account holds after the stop, once the check has worked at clearing every window
    symbol (`symbols`, or `config.symbol` for a single-name run): the reading is of the whole account,
    and what another symbol holds is named and left alone. Each poll sends at most one close, so the
    request bound is independent of the window's size. `unsettled_symbols` attributes each node request
    to its name; a request without a known name blocks any position its side could close.

    The check keeps one set of requests whose end the window does not know: each order of the node's in
    `unsettled` (its client order ID and side; `_unsettled`), and each close the check itself sends. A
    request leaves the set only on Alpaca's word: an order under its client order ID in a terminal status
    (`get_order_by_client_id`), or, for a close, Alpaca's refusal of the request (403, 422). The account
    is flat when no order is open, no position is held and the set is empty; nothing else is recorded ok.

    A poll each `POLL_SECS`, none starting once `after_stop_secs` of the most window names a reading has
    shown held have gone (`AFTER_STOP_SECS` for one, two polls more for each further name up to
    `PART_SLOTS`); one begun before runs to its end. A poll reads (`_AccountCheck.read`: up to `LOOKUPS` lookups, in turn, then the open orders and
    the position) and, if the account is not flat, acts (`act`): an order that works, listed or shown by
    its lookup, is canceled, on every poll until Alpaca shows it ended, in any session, up to `CANCELS`
    a poll; with none working, a position still held is closed for the held quantity under a client
    order ID of the check's own: by a market order if Alpaca's clock, read just before, says the regular
    session is open; else, if a part of the extended session is open by that clock and the window's
    `calendar` (`session_part`), by a DAY limit marked `extended_hours`, `LIMIT_OFFSET` through the touch
    of the latest quote on the part's feed (`close_limit`; `data`, alpaca-py's market data client or a
    callable that builds one, `window_data` when not given), left `LIMIT_TIMEOUT_SECS` and then canceled,
    so the next is priced afresh, and canceled as the check ends; one Alpaca refuses is not sent again in
    the check; with no part open, none is sent and the position is named in the result. The calendar is
    the window's, read on `calendar_on`, and read again for a clock on another day. A close's answer that is lost (anything but Alpaca's refusal, or a connect
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
    check = _AccountCheck(
        trading,
        (config.symbol,) if symbols is None else symbols,
        report,
        unsettled or {},
        calendar=calendar,
        calendar_on=calendar_on,
        data=data,
        unsettled_symbols=unsettled_symbols,
    )
    started = time.monotonic()
    deadline = started + after_stop_secs(1)
    left: list[str] | None = None
    last_error = ""
    more = True
    while more and time.monotonic() < deadline:  # a poll begun before the deadline runs to its end
        try:
            left = check.read()
            if not left:
                return left
            names = sum(1 for held in check.held.values() if held)
            deadline = max(deadline, started + after_stop_secs(names))
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
                f"the account could not be read in {deadline - started:g} s after the stop: {last_error}"
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


class _WindowSignals:
    """What SIGINT and SIGTERM do in a window (module header): the first is named in `first` and later
    ones are ignored; before the node it raises `WindowSignalled`, while the node runs it cancels the
    run's main task (whose `finally` asks the node to stop), and after the node it is only noted, so the
    wind-down (flatten, account check, record) finishes and the record names it."""

    def __init__(self) -> None:
        self.first: str | None = None
        self.phase = "before"
        self.run: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = None
        self.record: tuple[Path, WindowReport] | None = None

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.first is not None:
            return  # a forwarded copy, or a second stop: the first one's stop is under way
        self.first = signal.Signals(signum).name
        if self.record is not None and self.record[1].result == "running":
            # never `running` again: a kill before the window's end leaves this mark. The report is not
            # changed here, only a copy written (`_shown_record`); a result already set is left as it is.
            try:
                _write_window_record(*self.record)
            except Exception as e:  # noqa: BLE001 - the stop goes on; said on stderr
                print(f"paper:session: the stopping mark was not written: {e}", file=sys.stderr)
        if self.phase == "node":
            if self.run is not None:  # else the loop has not started yet: `enter_node` cancels
                loop, task = self.run
                loop.call_soon_threadsafe(self._cancel, task)
        elif self.phase == "before":
            raise WindowSignalled(f"stopped by {self.first}")

    def enter_node(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
        self.phase, self.run = "node", (loop, task)
        if self.first is not None:  # one came as the run started
            task.cancel()

    def _cancel(self, task: asyncio.Task) -> None:
        if (
            self.phase == "node"
        ):  # not once the stop is under way: cancelling its wait would abandon it
            task.cancel()

    def node_starts(self) -> None:
        """From before the node's loop runs: a signal is held for `enter_node`, never raised in the loop."""
        self.phase, self.run = "node", None

    def left_node(self) -> None:
        self.phase, self.run = "after", None


_SIGNALS = _WindowSignals()


@contextlib.contextmanager
def window_signals() -> Generator[None]:
    """SIGINT and SIGTERM handled by `_SIGNALS` for the span, on the main thread only, where Python lets a
    handler be set; the handlers found are put back after (one Python did not set, which `signal.signal`
    returns as None, as the default). SIGINT gets a handler even when the window started with it
    ignored, and `asyncio.Runner` then installs none of its own (module header)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _SIGNALS.__init__()
    found = {s: signal.signal(s, _SIGNALS.handle) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, handler in found.items():
            signal.signal(s, signal.SIG_DFL if handler is None else handler)
        _SIGNALS.__init__()  # nothing of this window's signal reaches a later one in the process (review)


def _run_node(node: LiveNode, ends: datetime) -> None:
    """`asyncio.run(_run_until(node, ends))`, except after a stop past `STOP_TIMEOUT_SECS`: closing the
    loop then would cancel the node's task and wait for the same stop, so the loop and the task are left
    (`_ABANDONED`) and `StopTimedOut` raised. A signal while the node runs cancels the run
    (`window_signals`), and the cancellation is raised as `WindowSignalled`, naming the signal."""
    runner = asyncio.Runner()
    calls = Calls()
    runner.get_loop().set_default_executor(calls)
    started: list[asyncio.Task] = []
    _SIGNALS.node_starts()
    try:
        stuck = runner.run(_run_until(node, ends, started))
    except BaseException as e:
        _SIGNALS.left_node()
        left = bool(started) and not started[0].done()
        if (
            left
        ):  # interrupted (Ctrl-C), or a signal's stop past `STOP_TIMEOUT_SECS`: the stop still runs
            _abandon(runner, started[0], calls)
        else:  # the node ended with its own error: close as after a clean stop, raise that error
            _close_runner(runner, started[0] if started else None, calls)
        if isinstance(e, asyncio.CancelledError) and _SIGNALS.first is not None:
            why = f"stopped by {_SIGNALS.first}"
            if left:
                why += f"; the node did not stop within {STOP_TIMEOUT_SECS:g} s, left running"
            elif started and not started[0].cancelled() and started[0].exception() is not None:
                own = started[0].exception()
                why += f"; the node's stop failed: {type(own).__name__}: {own}"
            raise WindowSignalled(why) from e
        raise
    _SIGNALS.left_node()
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
    current = asyncio.current_task()
    assert current is not None
    _SIGNALS.enter_node(asyncio.get_running_loop(), current)
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
        _SIGNALS.left_node()  # the stop is asked for here: a signal from now on is only noted
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


def _unsettled_symbols(
    cache, strategy_id: StrategyId, unsettled: Mapping[str, tuple[str, str]]
) -> dict[str, str]:
    """Copy each unsettled order's instrument symbol before native disposal clears the cache."""
    return {
        order.client_order_id.value: order.instrument_id.symbol.value
        for order in cache.orders(strategy_id=strategy_id)
        if order.client_order_id.value in unsettled
    }


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
    *,
    mover: bool = False,
) -> tuple[str | None, str]:
    """The session `--session auto` runs, from Alpaca's clock and calendar, or None and why not: the
    regular session when the clock says it is open; else the extended one, if `EXTENDED_READY`, it is
    open now and `strategy` runs in it (`WINDOW_STRATEGIES`), in a part with a feed for the window: for a
    `mover` window one of `MOVER_PARTS` (`no_mover_form`), whatever `SESSION_FEEDS` gives the part's
    other windows, so a part with no mover data row has nothing to run rather than a refusal at every
    firing; else nothing. The schedule fires on a timer and leaves this choice to Alpaca's clock."""
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
        if not mover:
            window_feed(part)
    except RuntimeError as e:  # `WindowSkipped` among them: a part with no feed (`SESSION_FEEDS`)
        return None, str(e)
    if mover and (no_form := no_mover_form(part)) is not None:
        return None, f"the regular session is closed, and {no_form}"
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
        with window_signals():
            code = _main(args)
            if (
                _SIGNALS.first is not None
            ):  # it came after the node: recorded failed, the wind-down finished
                code = 128 + signal.Signals[_SIGNALS.first].value
    except WindowSignalled as e:
        # a stop: said, and the exit a shell gives for the signal (128 + its number)
        print(f"paper:session: {e}", file=sys.stderr)
        named = re.search(r"stopped by (SIG[A-Z]+)", str(e))
        code = 128 + signal.Signals[named.group(1)].value if named else 1
        if _ABANDONED:
            try:
                write_heartbeat(args.log_dir, f"failed: {type(e).__name__}: {e}")
            except OSError:
                pass
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
        "part runs --strategy (with --mover, Mover)",
    )
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument("--symbol", default="SPY")
    chosen.add_argument(
        "--mover",
        action="store_true",
        help="run on the session's top mover, chosen at the start; Mover in every extended part",
    )
    parser.add_argument(
        "--slots",
        type=int,
        help=f"{MOVER}'s slots; default: seven in every part",
    )
    parser.add_argument("--spares", type=int, default=MOVER_SPARES, help="extra Mover roster names")
    # the trail flags default to None, so `_args` tells a flag given at its fixed value from one left
    # out; a Mover window that names none of them and no --arm runs DEFAULT_ARM (`_mover_trail`)
    parser.add_argument(
        "--trail-mode",
        choices=TRAIL_SETTINGS["trail_mode"]["arms"],
        help=f"{MOVER}'s trail: fixed, atr over the spread floor, tier, ratchet or profile; fixed "
        f"when another trail flag or --arm is given, and arm {DEFAULT_ARM}'s rows when none is",
    )
    parser.add_argument(
        "--trail-cap",
        choices=TRAIL_CAPS,
        help=f"{MOVER}'s arm for a trail over the 0.02 cap: clamp, refuse, or own (the name's "
        f"distance, sized down); clamp when another trail flag or --arm is given",
    )
    parser.add_argument(
        "--atr-k",
        type=Decimal,
        choices=(TRAIL_SETTINGS["atr_k"]["default"], *TRAIL_SETTINGS["atr_k"]["alternates"]),
        help=f"{MOVER}'s ATR multiple under --trail-mode atr: a registered row, 1, 2 or 3; 1 when "
        "another trail flag or --arm is given",
    )
    parser.add_argument(
        "--arm",
        choices=tuple(TRAIL_ARMS),
        help=f"{MOVER}'s trail arm, named in the record and the heartbeat; refused unless the trail "
        f"flags give its rows. With no trail flag and no --arm, a {MOVER} window runs "
        f"{DEFAULT_ARM}, named as the default",
    )
    parser.add_argument(
        "--trigger",
        choices=tuple(WINDOW_TRIGGERS),
        default=WindowConfig.trigger,
        help="what elects Mover's trailing sell: bid_ask, every quote's bid (the default), or "
        "last_price, a trade print (persistence step 1)",
    )
    parser.add_argument(
        "--validate-trade-sequence",
        action="store_true",
        help="set aside a streamed trade print older than the newest one passed on (the data client's "
        f"check, ADR 0134); off unless given, and {MOVER}'s, whose record carries its tally; refused "
        f"with --trigger last_price, under which {MOVER} judges a late print itself",
    )
    parser.add_argument(
        "--baseline-path", type=Path, help="default: catalog/movers-baseline/<feed>.json"
    )
    parser.add_argument(
        "--trail-profile-path",
        type=Path,
        help="default: catalog/movers-baseline/trail-profile-v1.json",
    )
    profile_k = PROFILE_SETTINGS["profile_k"]
    # the profile flags default to None, as the trail flags do, so a flag given at its row is told
    # from one left out; `window_config` fills the rows (`_profile_rows`)
    parser.add_argument(
        "--profile-k",
        type=_decimal_argument,
        choices=sorted((profile_k["default"], *profile_k["alternates"])),
    )
    for name in ("profile_pctl", "profile_floor"):
        parser.add_argument(
            "--" + name.replace("_", "-"),
            choices=PROFILE_SETTINGS[name]["arms"],
        )
    parser.add_argument(
        "--profile-multipliers",
        type=_decimal_argument,
        nargs=3,
    )
    parser.add_argument("--log-dir", type=Path, help="default: `default_log_dir()`")
    for name in ARM_FLAGS:
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            choices=[str(arm) for arm in TRAIL_SETTINGS[name]["arms"]],
            help=f"{MOVER}'s registered {name} (default: the code's)",
        )
    return parser


ARM_FLAGS = ("reentry_arm", "opening_arm")
"""Registered arms a run may select on the command line, until a settings file carries them; each is
validated as a `MoverConfig` row. The opening is a regular-session arm, so another part keeps it off."""


def _profile_rows(args: argparse.Namespace) -> dict[str, Any]:
    """The profile's rows from the command line: each as given, or its registered row when left out."""
    return {
        name: row["default"] if getattr(args, name) is None else getattr(args, name)
        for name, row in PROFILE_SETTINGS.items()
    }


def window_config(args: argparse.Namespace, session: str) -> WindowConfig:
    """The window's configuration from the parsed arguments and the session resolved for this run. The
    arms reach only a Mover window, as `slots` does; an auto run's part that is not regular keeps the
    opening off and says so on stderr, so the arm asked for is never dropped unsaid."""
    rows: dict[str, Any] = {}
    profile = _profile_rows(args)
    if args.strategy == MOVER:
        # the trail rows and arm as named, or `DEFAULT_ARM`'s when none is (`_mover_trail`)
        rows.update(
            _mover_trail(args),
            **profile,
            trail_profile_path=args.trail_profile_path,
            trigger=args.trigger,
            validate_trade_sequence=args.validate_trade_sequence,
        )
        for name in ARM_FLAGS:
            if (arm := getattr(args, name)) is None:
                continue
            if name == "opening_arm" and session != "regular":
                print(
                    f"paper:session: --opening-arm {arm} is the regular session's arm; this "
                    f"{session} window keeps it {WindowConfig.opening_arm}",
                    file=sys.stderr,
                )
                continue
            rows[name] = arm
    elif args.trail_mode not in (None, WindowConfig.trail_mode) or any(
        value != getattr(WindowConfig, name) for name, value in profile.items()
    ):
        print(
            f"paper:session: the trail rows are {MOVER}'s; this {args.strategy} window keeps "
            "the fixed trail",
            file=sys.stderr,
        )
    return WindowConfig(
        symbol=args.symbol,
        session=session,
        minutes=args.minutes,
        log_dir=args.log_dir,
        strategy=args.strategy,
        mover=args.mover,
        slots=args.slots if args.strategy == MOVER else None,
        spares=args.spares,
        baseline_path=args.baseline_path,
        **rows,
    )


def _decimal_argument(text: str) -> Decimal:
    """A finite decimal from the command line; argparse reports a ValueError as a usage error, and
    `Decimal` raises `InvalidOperation`, which is not one."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(text) from None
    if not value.is_finite():
        raise ValueError(text)
    return value


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The arguments, `--log-dir` defaulted to `default_log_dir()` only when it is not given, and
    `RuntimeError` for a relative one: the records go outside the repository, wherever the run starts
    (an empty `getconf` in the caller's shell leaves only the folder's name)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.profile_multipliers is not None:
        args.profile_multipliers = tuple(args.profile_multipliers)
    multipliers = PROFILE_SETTINGS["profile_multipliers"]
    if args.profile_multipliers not in (None, multipliers["default"], *multipliers["alternates"]):
        registered = " or ".join(
            " ".join(str(value) for value in row)
            for row in (multipliers["default"], *multipliers["alternates"])
        )
        parser.error(f"--profile-multipliers takes a registered row: {registered}")
    if args.regular_strategy is not None and args.session != "auto":
        parser.error("--regular-strategy names a part's strategy, so it needs --session auto")
    if args.regular_strategy == MOVER and not args.mover:
        parser.error(f"--regular-strategy {MOVER} trades the day's mover, so it needs --mover")
    runs_mover = MOVER in (args.strategy, args.regular_strategy) or (
        args.mover and args.session in ("extended", "auto")  # an extended part runs Mover with it
    )
    if args.slots is not None and not runs_mover:
        parser.error(
            f"--slots are {MOVER}'s, so they need --strategy or --regular-strategy {MOVER}, or "
            "--mover outside the regular session"
        )
    for flag in _trail_flags_given(args):
        if not runs_mover:
            parser.error(
                f"{flag} is {MOVER}'s, so it needs --strategy or --regular-strategy {MOVER}, or "
                "--mover outside the regular session"
            )
    if args.trigger != WindowConfig.trigger and not runs_mover:
        parser.error(
            f"--trigger elects {MOVER}'s trailing sell, so it needs --strategy or --regular-strategy "
            f"{MOVER}, or --mover outside the regular session"
        )
    if args.validate_trade_sequence and not runs_mover:
        parser.error(
            f"--validate-trade-sequence is recorded in {MOVER}'s record, so it needs --strategy or "
            f"--regular-strategy {MOVER}, or --mover outside the regular session"
        )
    if args.validate_trade_sequence and args.trigger in PRINT_TRIGGERS:
        parser.error(
            f"--validate-trade-sequence with --trigger {args.trigger} is refused: {MOVER} judges a "
            "late print itself under a print trigger, and the data client's check would set that "
            "print aside before it saw it"
        )
    # where Mover runs: the regular session under `--regular-strategy Mover`, or `--strategy Mover`
    # where no regular strategy is named; every extended part under `--mover`
    regular = args.session != "extended" and MOVER == (
        args.regular_strategy if args.regular_strategy is not None else args.strategy
    )
    extended = args.session != "regular" and (args.mover or args.strategy == MOVER)
    if args.reentry_arm is not None and not (regular or extended):
        parser.error(f"--reentry-arm is {MOVER}'s, so it needs a part that runs {MOVER}")
    if args.opening_arm is not None and not regular:
        parser.error(f"--opening-arm is the regular session's, so it needs {MOVER} to run there")
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


TRAIL_FLAGS: dict[str, Any] = {
    "trail_mode": TRAIL_SETTINGS["trail_mode"]["default"],
    "trail_cap": TRAIL_CAP_DEFAULT,
    "atr_k": TRAIL_SETTINGS["atr_k"]["default"],
}
"""Mover's trail flags, by their `argparse` names, with the fixed rows each takes when it is left out
and another trail flag or `--arm` is given (main's rows before `DEFAULT_ARM`)."""


def _mover_trail(args: argparse.Namespace) -> dict[str, Any]:
    """A Mover window's trail rows and arm from the command line: as given, the flags left out at the
    fixed rows (`TRAIL_FLAGS`); or, with no trail flag and no `--arm`, `DEFAULT_ARM`'s rows, marked as
    the default taken. `_args` leaves a flag not given unset, so the namespace shows only what was
    named."""
    if not _trail_flags_given(args):
        return {**TRAIL_ARMS[DEFAULT_ARM], "arm": DEFAULT_ARM, "arm_default": True}
    rows = {
        n: default if getattr(args, n) is None else getattr(args, n)
        for n, default in TRAIL_FLAGS.items()
    }
    return {**rows, "arm": args.arm, "arm_default": False}


def _trail_flags_given(args: argparse.Namespace) -> list[str]:
    """Mover's trail flags and `--arm` given on the command line, each as `flag value`. A flag given at
    its fixed row counts: it opts a Mover window out of `DEFAULT_ARM`. The profile's flags and
    `--trail-profile-path` count the same way, by presence: each names a trail the default arm would
    drop."""
    given = [
        f"--{name.replace('_', '-')} {getattr(args, name)}"
        for name in (*TRAIL_FLAGS, "arm")
        if getattr(args, name) is not None
    ]
    for name in PROFILE_SETTINGS:
        if (value := getattr(args, name)) is not None:
            shown = " ".join(map(str, value)) if isinstance(value, tuple) else value
            given.append(f"--{name.replace('_', '-')} {shown}")
    if args.trail_profile_path is not None:
        given.append(f"--trail-profile-path {args.trail_profile_path}")
    return given


def _refused_arm(args: argparse.Namespace) -> dict[str, Any]:
    """The arm a refused window would have run, for its record, as an `ok` record names it: Mover's
    from the command line (`_mover_trail`), none for another strategy."""
    if args.strategy != MOVER:
        return {"arm": None, "arm_default": False}
    trail = _mover_trail(args)
    return {"arm": trail["arm"], "arm_default": trail["arm_default"]}


def _main(args: argparse.Namespace) -> int:
    session = args.session
    config: WindowConfig | None = None
    try:
        trading = market = None
        market_read_at: float | None = None
        if session == "auto":
            trading = paper_trading()
            clock, calendar, market_read_at = session_now(trading)
            market = (clock, calendar)
            if args.regular_strategy is not None and clock.is_open:
                args.strategy = args.regular_strategy  # the schedule names the strategy a part runs
            resolved, why = resolve_session(clock, calendar, args.strategy, mover=args.mover)
            if resolved is None:
                print(f"paper:session: nothing to run: {why}")
                _heard(args.log_dir, f"nothing to run: {why}")
                return 0
            session = resolved
        if args.mover and session == "extended":
            args.strategy = MOVER
        config = window_config(args, session)
        # an auto schedule's part that runs another strategy runs none of Mover's trail flags, entry
        # arms or trigger; it says so, rather than drop them unseen (AGENTS.md rule 5)
        unrun = (
            []
            if args.strategy == MOVER
            else _trail_flags_given(args)
            + [
                f"--{name.replace('_', '-')} {getattr(args, name)}"
                for name in ARM_FLAGS
                if getattr(args, name) is not None
            ]
            + ([] if args.trigger == WindowConfig.trigger else [f"--trigger {args.trigger}"])
            + (["--validate-trade-sequence"] if args.validate_trade_sequence else [])
        )
        if unrun:
            print(
                f"paper:session: {', '.join(unrun)} not run: this {session} window runs {args.strategy}"
            )
        write_heartbeat(
            args.log_dir,
            f"a {session} window of {args.strategy}"
            + ("" if config.arm is None else f", arm {config.arm}")
            + (" (the default)" if config.arm_default else "")
            + ("" if not unrun else f"; {', '.join(unrun)} not run"),
        )
        report = run_window(config, trading=trading, market=market, market_read_at=market_read_at)
    except WindowSkipped as e:
        print(f"paper:session: skipped: {e}")
        _heard(args.log_dir, f"skipped: {e}")
        return 0
    except WindowRefused as e:
        print(f"paper:session: {e}", file=sys.stderr)
        _heard(args.log_dir, f"refused: {e}")  # the refusal stands, said above
        try:
            unrecorded = write_record(
                args.log_dir,
                {
                    "session": session,
                    "strategy": args.strategy,
                    "result": f"refused: {e}",
                    **(
                        {}
                        if config is None
                        else {
                            "trigger": config.trigger,
                            "validate_trade_sequence": config.validate_trade_sequence,
                        }
                    ),
                    "started_epoch": time.time(),
                    "started": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
                    "stale_after": STALE_RUNNING_SECS,
                    "commit": running_commit(),
                    "account": paper_account(),
                    "request_budget": request_record(()),
                    **_refused_arm(args),
                },
            )
            if unrecorded is not None:
                print(
                    f"paper:session: the refused record's request snapshot failed: {unrecorded}",
                    file=sys.stderr,
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
