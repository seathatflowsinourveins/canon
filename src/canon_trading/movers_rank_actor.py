"""The movers ranking actor: the whole market's minute bars in, one ranking generation at most each publish
interval, its top 20 out on the message bus as the signal `movers-rank` (ADR 0107; the score is ADR 0087's).

Contract: NautilusTrader v2.0.0rc8 (`nautilus-trader@d53849b216e2`), `nautilus_trader.common.DataActor` as
shipped (`python/nautilus_trader/common/__init__.pyi:688`; there is no `Actor` class at the pin), used only
through its documented surface: `subscribe_bars` with a list of bar types and `subscribe_instrument_status`
with a list of ids (`:830`, `:860`), `on_bar` and `on_instrument_status` (`:752`, `:759`), the clock's
`set_timer(name, interval, start_time, callback=..., fire_immediately=True)` (`:654`), and
`publish_signal(name, value, ts_event)` (`:720`), which a `Strategy` or a `DataActor` reads with
`subscribe_signal(name)` and `on_signal(signal)`
(`:746`, `:769`). Upstream's own use of the pair is `examples/backtest/callback_borrow_reentrancy.py` at
the pin. A signal is not replayed to a subscriber that subscribed after it was published (observed at the
pin in a `BacktestEngine`), so every generation is published and "no signal yet" means no ranking.

The arithmetic is `canon_trading.movers_rank`'s, pure functions over plain values: the per-symbol `Tally`
of minute bars, the 15-minute segment baseline and its reader, the filters, the score, the generation with
its two clocks, and the top 20. The value's form is `canon_trading.movers_rank_signal`'s codec. This
module wires them to the node and does no arithmetic of its own.

What it reads, and from whom:

* Bars: `<SYM>.ALPACA-1-MINUTE-LAST-EXTERNAL`, Alpaca's own minute bars as the adapter's data client hands
  them to the engine (`canon_alpaca.data.parse_bar`: `ts_event` at the bar's open, `ts_init` at its close),
  for every equity in the cache. Node-built `INTERNAL` bars are not used: at the pin a time bar is built
  for every clock minute, pauses included, unless `time_bars_build_with_no_updates` is off
  (`crates/data/src/engine/config.rs:36-38`). The actor never opens a stream; one stream an account is
  the data client's.
  Bars name `params={"feed": feed, "wildcard": True}` (no revised bars, 2026-11-12) and statuses name
  `params={"feed": feed, "wildcard": True}`: NautilusTrader preserves adapter params, `feed` is ADR 0055's,
  and the two channel keys are ADR 0090's adapter contract (pending on main; ADR 0107).
* Statuses: `InstrumentStatus` for the same ids, kept by symbol; until the adapter publishes them a halt
  is unknown, which `movers_rank` keeps eligible and records (its `HALT_UNKNOWN_ELIGIBLE` row).
* Reference closes and splits, the actor's only reads of its own, both GET on Alpaca's market data API
  through alpaca-py 0.45.0 (`alpaca-py@c42add7c0a70`), on a client whose session refuses every method but
  GET in data-only mode and has the adapter's timeout (`canon_alpaca.providers.with_http_timeout`,
  `require_get_only`), in a worker thread, never on the event loop:
  `StockHistoricalDataClient.get_stock_bars(StockBarsRequest(timeframe=TimeFrame.Day,
  adjustment=Adjustment.SPLIT, feed=DataFeed.SIP))` for the latest completed regular close of each symbol
  ("Historical bars": "`split`: adjust price and volume for forward and reverse stock splits", default
  `raw`), and `CorporateActionsClient.get_corporate_actions(CorporateActionsRequest(types=[forward_split,
  reverse_split], start, end))` for the splits since. They are read at start, again when the trade date
  rolls at 20:00 New York, and retried on the next tick when a read failed or came back unusable. The
  reference is always a session before the trade date (`movers_rank.rank` refuses anything else), so the
  close that settled at 16:00 is read as the next trade date's reference at 20:00, and the split window
  always ends at the trade date.
* The baseline: a file a nightly job writes through `movers_rank.write_baselines`, read once a trade date
  from the config's `baseline_path` with `movers_rank.read_baselines`; one file per feed keeps their
  denominators apart. A missing, unreadable or other-session baseline is no baseline: nothing is
  published, and it is read again on the next tick.

What it publishes: one generation a timer tick, `PUBLISH_OFFSET_SECS` after each minute mark (Alpaca's
minute bars "are emitted right after each minute mark"), with `ts_event` the time the generation was
computed. The first tick is the first strictly future minute offset: `fire_immediately=True` makes
upstream fire at `start_time` instead of one interval later (`crates/common/src/clock/mod.rs`,
`validate_and_prepare_schedule`). Nothing at all, rather than a guess, when the generation is invalid
(a bar `Tally` refuses: an updated bar, which repeats its minute, a bar out of order or outside the trade
date), when a read it needs is missing, failed or stale, or when the latest bar it used ended more than
config `max_data_age_secs`
before. Each name uses that same bound: a stale price and its derived figures become unknown, its row
and bar timestamp stay in the universe, and it cannot enter the top entries. The record names the bound
and the stale symbols; `MAX_DATA_AGE_SECS` is the UNCALIBRATED default.
Each tick's outcome is one log line, `movers-rank record {json}`, with the latency labels of ADR 0107:
`publication`, `receipt`, `generation`, and `entry_eligible` and `submission`, which are the strategy's
and stay null here.

Rows, all UNCALIBRATED: `PUBLISH_INTERVAL_SECS`, `PUBLISH_OFFSET_SECS`, `MAX_DATA_AGE_SECS`,
`REFERENCE_LOOKBACK_DAYS`, `REFERENCE_READ_BOUND_SECS`, `PRE_READ_WAIT_SECS`. `REF_BATCH` and
`UNUSABLE_READS_REFUSED` are choices, not documented limits.

Each reference read says what it did as it happens: every daily-bars batch and the splits read write one
line each from the read's own thread (`READ_LOG`), with the time, the duration and the outcome; each
attempt's outcome is a `movers-rank reference read {json}` line at the first tick that sees it finished,
and every record names the attempt in flight, the last attempt's failure, an attempt past
`REFERENCE_READ_BOUND_SECS`, or the refusal after `UNUSABLE_READS_REFUSED` unusable reads.

Limits (ADR 0087): the move from the regular open is not known (no opens are passed); trade corrections and
cancels come only on the trade channel, which the actor does not read, and it no longer takes revised bars
(the owner's plan of 2026-11-12), so a late trade is missed until it falls in a later minute; whether Alpaca's daily bar close is the official closing price is UNVERIFIED.
"""

from __future__ import annotations

import inspect
import json
import math
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from time import monotonic
from time import time_ns
from typing import TYPE_CHECKING
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment
from alpaca.data.enums import CorporateActionsType
from alpaca.data.enums import DataFeed
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import CorporateActionsRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from canon_alpaca.constants import VENUE
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import DataActor
from nautilus_trader.common import DataActorConfig
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity

from canon_trading import movers_rank
from canon_trading.movers_rank_signal import SIGNAL_NAME
from canon_trading.movers_rank_signal import encode_ranking

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar
    from nautilus_trader.model import InstrumentStatus
    from nautilus_trader.model import MarketStatusAction

NEW_YORK = ZoneInfo("America/New_York")
BAR_SPEC = "1-MINUTE-LAST-EXTERNAL"
PUBLISH_INTERVAL_SECS = 60
"""At most one full-market generation this often (UNCALIBRATED)."""
PUBLISH_OFFSET_SECS = 5
"""Seconds after each minute mark a generation is computed, so the minute's bars are in (UNCALIBRATED)."""
MAX_DATA_AGE_SECS = movers_rank.MAX_DATA_AGE_SECS
"""Default maximum age of each name's latest bar and of a generation's latest bar (UNCALIBRATED)."""
TRADE_DATE_ROLL = time(20, 0)
"""A trade date's bars open in [the day before at 20:00, the day at 20:00) New York (`movers_rank`)."""
REFERENCE_LOOKBACK_DAYS = 10
"""Calendar days of daily bars read for each symbol's latest completed regular close (UNCALIBRATED)."""
REF_BATCH = 500
"""Symbols a daily-bars request names (a choice: the page's limit is on data points, 10,000)."""
REFERENCE_READ_BOUND_SECS = 300.0
"""How long one attempt at the references and splits may run before every record names it as unread by
this bound, rather than "not read yet" (UNCALIBRATED; the whole market's pre-read took about 90 s on
2026-11-12, and a window whose read never answered said only "not read yet" for 12 minutes). The bound
names the attempt; it does not stop it, since the one read worker cannot be interrupted mid-request."""
PRE_READ_WAIT_SECS = 180.0
"""How long the actor's first call waits for the pre-read before reading afresh (UNCALIBRATED; twice
the pre-read's measured 90 s)."""
UNUSABLE_READS_REFUSED = 2
"""Whole-market reads in a row that come back unusable (no reference close, or no split read), after
which the references are refused by name for the trade date and not read again (the lead's order of
2026-11-12: never a silent retry loop). A read that raises is retried each tick and named."""
SPLITS_LIMIT = 1000
"""alpaca-py's `CorporateActionsRequest.limit` default; a read that reaches it is incomplete, so unknown."""
STALE_NAMED = 20
"""Stale symbols a record names; the rest are counted (a choice, for a whole-market universe)."""
TIMER = "movers-rank"
RECORD_PREFIX = "movers-rank record "
READ_PREFIX = "movers-rank reference read "
READ_LOG = "MoversRankReads"
"""The logger name of the lines the read threads write: each batch, and each note of the pre-read."""


def trade_date(ts_ns: int) -> date:
    """The trade date whose bars include one opening at `ts_ns`: the New York date, or the next one from 20:00."""
    ny = datetime.fromtimestamp(ts_ns / 1e9, UTC).astimezone(NEW_YORK)
    return ny.date() + timedelta(days=1) if ny.time() >= TRADE_DATE_ROLL else ny.date()


def reference_cutoff(ts_ns: int) -> date:
    """The first date whose daily bar is not a completed regular close at `ts_ns`, which is the trade date.

    It stays the trade date after 16:00 New York because `movers_rank.rank` accepts only a reference close
    of a session before the trade date: the close that settled at 16:00 is read as the reference of the
    trade date that opens at 20:00, when the cutoff moves with it.
    """
    return trade_date(ts_ns)


@dataclass(frozen=True)
class ReferenceRead:
    """Each symbol's latest completed regular close before `cutoff`, and the splits read with them."""

    cutoff: date
    references: Mapping[str, movers_rank.Reference]
    splits: movers_rank.SplitRead | None
    read_ns: int


def reference_problem(read: ReferenceRead) -> str | None:
    """Why a reference read that returned is not usable, or None: no reference close, or no split read.
    Such a read counts as failed and is retried, and the ranking is unpublished meanwhile (the lead's
    ruling of 2026-11-12 on the ultra review of the composition, finding 12)."""
    if not read.references:
        return "no reference close was read"
    if read.splits is None:
        return "the splits were not read"
    return None


class ReferenceReader(Protocol):
    def __call__(self, symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead: ...


BASE_FIELDS: frozenset[str] = frozenset(
    name for name, _ in inspect.getmembers(DataActorConfig, inspect.isgetsetdescriptor)
)
"""`DataActorConfig`'s own fields at the pin (`actor_id`, `log_events`, `log_commands`): upstream's
`__new__` reads them from the keywords and sets them, so a subclass must not call them unknown."""


class MoversRankActorConfig(DataActorConfig):
    """Parameters of `MoversRankActor`.

    `feed` names the one feed the node's bars come from (`sip` or `boats`) and every record names it;
    `baseline_path` is the file the nightly job writes for that feed through
    `movers_rank.write_baselines`, read by the actor with `movers_rank.read_baselines`, so a separate file
    per feed keeps two feeds' denominators apart. `order_cap` is the strategy's per-order cap in dollars,
    passed to `movers_rank` for its one-share row; the strategy enforces it at the order.
    `reference_reader` reads the references and splits; by default `alpaca_reference_reader`.
    `inline_reads` runs both reads on the caller's thread, for a backtest, whose clock does not wait for a
    worker. `max_data_age_secs` is the inclusive age bound for each name's last bar at compute time;
    a generation with no bar inside the bound publishes nothing. Default: `MAX_DATA_AGE_SECS`.
    `rank_without_baseline` lets a missing baseline file rank every name in the zero-baseline tier on
    its dollars, under the move condition (`movers_rank.rank`'s `no_baseline`), and each record says so;
    a file that exists and cannot be used still publishes nothing. `reference_read_bound_secs` is how
    long one reference attempt may run before the record names it by that bound; default
    `REFERENCE_READ_BOUND_SECS`. The window sets `rank_without_baseline` overnight only,
    until Wave 2's overnight baseline exists (the lead's ruling, 2026-11-12; ultra review of the
    composition, finding 2).
    """

    def __init__(
        self,
        *,
        feed: str,
        order_cap: float,
        baseline_path: str,
        reference_reader: ReferenceReader | None = None,
        inline_reads: bool = False,
        max_data_age_secs: float = MAX_DATA_AGE_SECS,
        rank_without_baseline: bool = False,
        reference_read_bound_secs: float = REFERENCE_READ_BOUND_SECS,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        unknown = sorted(set(_kwargs) - BASE_FIELDS)
        if unknown:
            raise TypeError(f"MoversRankActorConfig got unknown keywords {unknown}")
        if feed not in ("sip", "boats"):
            raise ValueError(f"feed must be sip or boats, got {feed!r}")
        if not math.isfinite(order_cap) or order_cap <= 0:
            raise ValueError(f"an order cap must be a finite positive amount, got {order_cap!r}")
        if not math.isfinite(max_data_age_secs) or max_data_age_secs <= 0:
            raise ValueError(
                f"maximum data age must be finite and positive, got {max_data_age_secs!r}"
            )
        self.feed = feed
        self.order_cap = order_cap
        self.baseline_path = baseline_path
        self.reference_reader = reference_reader
        self.inline_reads = inline_reads
        if not math.isfinite(reference_read_bound_secs) or reference_read_bound_secs <= 0:
            raise ValueError(
                f"the reference read bound must be finite and positive, got {reference_read_bound_secs!r}"
            )
        self.max_data_age_secs = max_data_age_secs
        self.rank_without_baseline = rank_without_baseline
        self.reference_read_bound_secs = reference_read_bound_secs


class MoversRankActor(DataActor):
    """Keep each symbol's `Tally` from the whole market's minute bars and publish the top 20 each interval."""

    def __init__(self, config: MoversRankActorConfig) -> None:
        super().__init__(config)
        self._feed = config.feed
        self._order_cap = config.order_cap
        self._max_data_age_secs = config.max_data_age_secs
        self._rank_without_baseline = config.rank_without_baseline
        self._baseline_path = Path(config.baseline_path)
        self._reference_reader = config.reference_reader or alpaca_reference_reader
        self._inline = config.inline_reads
        self._executor: ThreadPoolExecutor | None = None
        self._assets: dict[str, Mapping[str, object]] = {}
        self._security_types: dict[str, movers_rank.SecurityType] = {}
        self._session: date | None = None
        self._tallies: dict[str, movers_rank.Tally] = {}
        self._statuses: dict[str, MarketStatusAction] = {}
        self._invalid: list[str] = []
        self._receipt_ns: int | None = None
        self._last_bar_open_ns: int | None = None
        """The latest bar opening received: the gap from its minute's end to publication is the record's
        stand-in for the event loop's lag, which the pin exposes no gauge of (the lead's note, 2026-11-12)."""
        self._refs: Future[ReferenceRead] | None = None
        self._refs_key: tuple[date, date] | None = None
        self._read_bound_ns = int(config.reference_read_bound_secs * 1_000_000_000)
        self._refs_attempt = 0
        self._refs_started_ns: int | None = None
        self._refs_seen: Future[ReferenceRead] | None = None
        """The attempt whose outcome was last logged, so each attempt's outcome is logged once."""
        self._refs_last_failure: str | None = None
        self._refs_unusable_run = 0
        self._refs_refused: str | None = None
        """The problem the reads came back with once `UNUSABLE_READS_REFUSED` in a row refuse the key."""
        self.reference_reads: list[dict[str, object]] = []
        """Each finished attempt's outcome, as its `movers-rank reference read` line logs it."""
        self._baseline: Future[movers_rank.BaselineRead] | None = None
        self._baseline_session: date | None = None
        self.last_record: dict[str, object] | None = None
        self.bars_received = 0
        self.started_ns: int | None = None
        """The actor's start, so a record can say how long the first publication took."""
        """Every bar `on_bar` was handed, counted for the record (the whole-market check of 2026-11-12)."""

    def on_start(self) -> None:
        """Subscribe to every cached Alpaca equity's minute bars and statuses, start the reads, set the
        timer."""
        self.started_ns = self.clock.timestamp_ns()
        equities = [i for i in self.cache.instruments(venue=VENUE) if isinstance(i, Equity)]
        if not equities:
            raise RuntimeError("MoversRankActor cannot start: no equity is in the cache")
        self._assets = {i.id.symbol.value: dict(i.info) for i in equities}
        self._security_types = movers_rank.security_types_from_names(self._assets)
        self.subscribe_bars(
            [BarType.from_str(f"{i.id}-{BAR_SPEC}") for i in equities],
            # no revised bars (the owner's plan of 2026-11-12): a `Tally` refuses a revised bar, which
            # repeats its minute, and that voided the whole market's generation; each minute keeps its
            # first emission, and a late trade is missed until the next minute (the limits below)
            params={"feed": self._feed, "wildcard": True},
        )
        self.subscribe_instrument_status(
            [i.id for i in equities], params={"feed": self._feed, "wildcard": True}
        )
        if not self._inline:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="movers-rank-reads"
            )
        now = self.clock.timestamp_ns()
        self._ensure_reads(now)
        start = datetime.fromtimestamp(now / 1e9, UTC).replace(second=0, microsecond=0) + timedelta(
            seconds=PUBLISH_OFFSET_SECS
        )
        if int(start.timestamp()) * 1_000_000_000 <= now:
            start += timedelta(minutes=1)
        self.clock.set_timer(
            TIMER,
            timedelta(seconds=PUBLISH_INTERVAL_SECS),
            start_time=start,
            callback=self.on_tick,
            fire_immediately=True,
        )

    def on_stop(self) -> None:
        if TIMER in self.clock.timer_names():
            self.clock.cancel_timer(TIMER)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def on_bar(self, bar: Bar) -> None:
        """Add `bar` to its symbol's `Tally`; a bar `Tally` refuses invalidates this interval's generation."""
        self.bars_received += 1
        symbol = bar.bar_type.instrument_id.symbol.value
        self._receipt_ns = self.clock.timestamp_ns()
        self._last_bar_open_ns = max(self._last_bar_open_ns or 0, bar.ts_event)
        session = trade_date(bar.ts_event)
        clock_session = trade_date(self._receipt_ns)
        if session > clock_session:
            # one malformed bar dated ahead of the actor's clock is invalid input for its tick; rolling to
            # its trade date would empty today's tallies and make every later bar of today invalid too.
            self._invalid.append(
                f"{symbol}: a bar opening {bar.ts_event} is from trade date {session}, ahead of the "
                f"actor's trade date {clock_session}"
            )
            return
        if self._session is None or session > self._session:
            self._roll(session)
        elif session < self._session:
            self._invalid.append(
                f"{symbol}: a bar opening {bar.ts_event} is from trade date {session}"
            )
            return
        try:
            tally = self._tallies.get(symbol) or movers_rank.Tally(session=session)
            self._tallies[symbol] = tally.add(bar)
        except movers_rank.InvalidGeneration as error:
            self._invalid.append(f"{symbol}: {error}")

    def on_instrument_status(self, status: InstrumentStatus) -> None:
        """Fold `status` into the symbol's held one: a status that says nothing of whether it trades keeps
        a halt, and a halt is not erased by a short-sale restriction change (`movers_rank.status_after`)."""
        symbol = status.instrument_id.symbol.value
        folded = movers_rank.status_after(self._statuses.get(symbol), status.action)
        if folded is None:
            self._statuses.pop(symbol, None)
        else:
            self._statuses[symbol] = folded

    def on_tick(self, event: TimeEvent) -> None:
        """Compute one generation and publish its top 20, or publish nothing and say why."""
        handled = self.clock.timestamp_ns()
        now = event.ts_event
        session = trade_date(now)
        if self._session is None or session > self._session:
            self._roll(session)
        self._ensure_reads(now)
        record: dict[str, object] = {
            "feed": self._feed,
            "max_data_age_secs": self._max_data_age_secs,
            "session": session.isoformat(),
            "published": False,
            "started_ns": self.started_ns,
            "latency_ns": {
                "publication": None,
                "receipt": self._receipt_ns,
                "last_bar_open": self._last_bar_open_ns,
                "handled": handled,
                "generation": now,
                "entry_eligible": None,
                "submission": None,
            },
        }
        reasons = self._take_invalid()
        refs = self._refs_result(now, reasons)
        baseline = self._result(self._baseline, "baseline", reasons)
        no_baseline = baseline is not None and baseline.absent and self._rank_without_baseline
        if no_baseline:
            record["baseline"] = movers_rank.NO_BASELINE
        elif baseline is not None and baseline.problem is not None:
            reasons.append(f"baseline not usable: {baseline.problem}")
        if reasons:
            self._finish(record, reasons)
            return
        assert refs is not None and baseline is not None
        try:
            generation = movers_rank.rank(
                session=session,
                assets=self._assets,
                tallies=self._tallies,
                baselines=baseline.baselines,
                references=refs.references,
                opens={},
                statuses=self._statuses,
                splits=refs.splits,
                security_types=self._security_types,
                order_cap=self._order_cap,
                computed_ns=now,
                max_data_age_secs=self._max_data_age_secs,
                # the day ratio counts from 04:00 unless the file's header lists the overnight feed
                # (ADR 0087, the lead's numerator rule of 2026-11-11)
                overnight_baseline=baseline.overnight,
                no_baseline=no_baseline,
            )
        except movers_rank.InvalidGeneration as error:
            self._finish(record, [f"invalid generation: {error}"])
            return
        record["latency_ns"]["publication"] = generation.published_ns  # type: ignore[index]
        record["rows"] = len(generation.rows)
        record["eligible"] = sum(1 for r in generation.rows if r.eligible)
        stale = [
            r.instrument_id.symbol.value
            for r in generation.rows
            if "last_bar:stale" in r.excluded_by
        ]
        # bounded, so a whole-market universe keeps each minute's record small
        record["stale_symbols"] = stale[:STALE_NAMED]
        record["stale_count"] = len(stale)
        record["assets"] = len(self._assets)
        record["bars"] = self.bars_received
        record["references_read_ns"] = refs.read_ns
        record["symbols"] = len(self._tallies)
        record["top"] = top_rows(generation, now)
        if generation.published_ns is None:
            self._finish(record, ["no bar in the generation"])
            return
        if generation.published_ns < now - int(self._max_data_age_secs * 1_000_000_000):
            self._finish(record, [f"stale: the latest bar used ended at {generation.published_ns}"])
            return
        value = encode_ranking(tuple(movers_rank.top_entries(generation)))
        self.publish_signal(SIGNAL_NAME, value, ts_event=generation.generated_ns)
        record["published"] = True
        record["value"] = value
        self._finish(record, [])

    def _roll(self, session: date) -> None:
        """Start trade date `session`: its tallies begin empty and its baseline is read."""
        self._session = session
        self._tallies = {}

    def _ensure_reads(self, now: int) -> None:
        """Start each read this tick needs and retry one that failed or came back unusable; a read keyed
        to another `(cutoff, session)` pair, or a baseline read for another trade date, is never reused.
        `UNUSABLE_READS_REFUSED` unusable reads in a row refuse the key's references by name."""
        session = trade_date(now)
        cutoff = reference_cutoff(now)
        key = (cutoff, session)
        self._log_refs_outcome(now)
        if self._refs_key != key:
            if self._refs is not None and not self._refs.done():
                # the new key's attempt queues behind this one on the one worker; say so once
                self._log_refs_entry(now, f"unfinished when the reads moved to {key[1]}")
            self._refs_attempt, self._refs_last_failure = 0, None
            self._refs_unusable_run, self._refs_refused = 0, None
        if self._refs_key != key or (
            self._refs_refused is None and self._refs_unusable(self._refs)
        ):
            self._refs_key = key
            symbols = sorted(self._assets)
            self._refs_attempt += 1
            self._refs_started_ns = now
            # neither read holds a reference to the actor: an attempt still running at stop finishes on
            # the worker after the node drops the actor, and an unsendable native actor must not be
            # freed there
            reader = self._reference_reader
            self._refs = self._submit(lambda: reader(symbols, cutoff, session))
            self._log_refs_outcome(now)  # an inline read is already finished
        if self._baseline_session != session or self._baseline_unusable(self._baseline):
            self._baseline_session = session
            path = self._baseline_path
            self._baseline = self._submit(lambda: movers_rank.read_baselines(path, session))

    def _log_refs_outcome(self, now: int) -> None:
        """Log the current attempt's outcome once, at the first tick that sees it finished, and keep what
        the next records name: the last failure, the unusable run, and the refusal it reaches."""
        future = self._refs
        if future is None or not future.done() or future is self._refs_seen:
            return
        self._refs_seen = future
        problem = None
        if future.cancelled():
            outcome = "cancelled"
        elif (error := future.exception()) is not None:
            outcome = f"failed: {type(error).__name__}: {error}"
        elif (problem := reference_problem(future.result())) is not None:
            outcome = f"unusable: {problem}"
        else:
            outcome = f"read: {len(future.result().references)} references and the splits"
        self._refs_unusable_run = self._refs_unusable_run + 1 if problem is not None else 0
        if problem is not None and self._refs_unusable_run >= UNUSABLE_READS_REFUSED:
            self._refs_refused = problem
        self._refs_last_failure = None if outcome.startswith("read: ") else outcome
        self._log_refs_entry(now, outcome)

    def _log_refs_entry(self, now: int, outcome: str) -> None:
        entry: dict[str, object] = {
            "attempt": self._refs_attempt,
            "asked_ns": self._refs_started_ns,
            "seen_ns": now,
            "outcome": outcome,
        }
        self.reference_reads.append(entry)
        line = READ_PREFIX + json.dumps(entry, sort_keys=True)
        if outcome.startswith("read: "):
            self.log.info(line)
        else:
            self.log.warning(line)

    def _refs_result(self, now: int, reasons: list[str]) -> ReferenceRead | None:
        """The references for this tick, or None with a reason that names the attempt, never a bare
        wait: the refusal, an attempt past the bound, or the attempt in flight and the last failure.
        An attempt's time is when it was asked for: on the one worker it may wait behind another."""
        name = "references and splits"
        future = self._refs
        if self._refs_refused is not None:
            reasons.append(
                f"{name} not usable: refused: {UNUSABLE_READS_REFUSED} reads in a row came back "
                f"unusable ({self._refs_refused}); not read again for trade date {self._session}"
            )
            return None
        if future is not None and not future.done() and self._refs_started_ns is not None:
            asked = _iso(self._refs_started_ns)
            last = (
                f"; attempt {self._refs_attempt - 1} {self._refs_last_failure}"
                if self._refs_last_failure is not None and self._refs_attempt > 1
                else ""
            )
            attempt = f"attempt {self._refs_attempt} asked for at {asked} has not returned{last}"
            if now - self._refs_started_ns > self._read_bound_ns:
                reasons.append(
                    f"{name} could not be read within {self._read_bound_ns / 1e9:g} s: {attempt}"
                )
            else:
                reasons.append(f"{name} not read yet: {attempt}")
            return None
        refs = self._result(future, name, reasons)
        if refs is not None and (problem := reference_problem(refs)) is not None:
            reasons.append(f"{name} not usable: {problem}")
            return None
        return refs

    @staticmethod
    def _refs_unusable(future: Future[ReferenceRead] | None) -> bool:
        """Whether a finished reference read failed or returned one `reference_problem` names."""
        if future is None or not future.done() or future.cancelled():
            return False
        if future.exception() is not None:
            return True
        return reference_problem(future.result()) is not None

    def _baseline_unusable(self, future: Future[movers_rank.BaselineRead] | None) -> bool:
        """Whether a finished baseline read failed or read a file it would not use. A file found absent
        on a part that ranks without a baseline is kept for the session, not read again each tick."""
        if future is None or not future.done() or future.cancelled():
            return False
        if future.exception() is not None:
            return True
        result = future.result()
        return result.problem is not None and not (result.absent and self._rank_without_baseline)

    def _submit[T](self, read: Callable[[], T]) -> Future[T]:
        if self._executor is not None:
            return self._executor.submit(read)
        future: Future[T] = Future()
        try:
            future.set_result(read())
        except Exception as error:  # noqa: BLE001, kept on the future and named in the next record
            future.set_exception(error)
        return future

    def _take_invalid(self) -> list[str]:
        taken, self._invalid = self._invalid, []
        return [f"invalid input: {r}" for r in taken]

    @staticmethod
    def _result[T](future: Future[T] | None, name: str, reasons: list[str]) -> T | None:
        if future is None or not future.done():
            reasons.append(f"{name} not read yet")
            return None
        if future.cancelled():
            reasons.append(f"{name} read was cancelled")
            return None
        error = future.exception()
        if error is not None:
            reasons.append(f"{name} read failed: {type(error).__name__}: {error}")
            return None
        return future.result()

    def _finish(self, record: dict[str, object], reasons: list[str]) -> None:
        record["reasons"] = reasons
        self.last_record = record
        line = RECORD_PREFIX + json.dumps(record, sort_keys=True)
        if record["published"]:
            self.log.info(line)
        else:
            self.log.warning(line)


def _iso(ts_ns: int) -> str:
    """`ts_ns` as an ISO 8601 UTC instant to the millisecond, for a reason or a log line."""
    return (
        datetime.fromtimestamp(ts_ns / 1e9, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


TOP_ROWS = 10


def top_rows(
    generation: movers_rank.Generation, now: int, n: int = TOP_ROWS
) -> list[dict[str, object]]:
    """The lead's ranked table (2026-11-11) for one generation: its best `n` in rank order, each with the
    move, the span's dollars, the baseline's normal dollars for the same clock minutes, the ratio and its
    span, the tier (`ratio` against an established baseline, else the zero-baseline `dollar` tier) and
    the age of its data at the tick."""
    rows = {r.instrument_id: r for r in generation.rows}
    found: list[dict[str, object]] = []
    for place, entry in enumerate(movers_rank.top_entries(generation, n), start=1):
        r = rows[entry.instrument_id]
        dollar_tier = r.ratio == math.inf
        found.append(
            {
                "rank": place,
                "symbol": r.instrument_id.symbol.value,
                "move": r.move,
                "dollar_volume": r.dollar_volume,
                "normal_dollar_volume": r.baseline,
                "ratio": None if dollar_tier else r.ratio,
                "ratio_span": r.ratio_span,
                "tier": "dollar" if dollar_tier else "ratio",
                "established": r.established,
                "data_age_secs": None
                if r.last_bar_ns is None
                else round((now - r.last_bar_ns) / 1_000_000_000, 3),
            }
        )
    return found


BAR_NS = 60_000_000_000
"""One minute bar's span: a bar stamped at its opening ends one minute later."""


def publication_lags(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Over the published generations, the timer's lag (from a tick's scheduled time to its handler) and
    the gap from the latest bar's minute end to the actor's receipt of the latest bar, in seconds: the
    record's measure of the event loop's backlog, since the pin exposes no queue depth or loop lag (the
    lead's note of 2026-11-12; NautilusTrader's runner channels apply no backpressure). A generation's
    `publication` stamp is its computed minute, not a wall clock, so it measures no delay."""
    timer: list[float] = []
    bar_gap: list[float] = []
    for record in records:
        latency = record.get("latency_ns")
        if record.get("published") is not True or not isinstance(latency, dict):
            continue
        handled, generated = latency.get("handled"), latency.get("generation")
        received, bar_open = latency.get("receipt"), latency.get("last_bar_open")
        if isinstance(handled, int) and isinstance(generated, int):
            timer.append((handled - generated) / 1e9)
        if isinstance(received, int) and isinstance(bar_open, int):
            bar_gap.append((received - bar_open - BAR_NS) / 1e9)

    def spread(values: list[float]) -> dict[str, object] | str:
        if not values:
            return "unavailable: no published generation carries it"
        ordered = sorted(values)
        return {
            "n": len(ordered),
            "median": round(ordered[len(ordered) // 2], 3),
            "max": round(ordered[-1], 3),
        }

    return {"timer_lag_secs": spread(timer), "bar_end_to_receipt_secs": spread(bar_gap)}


def prefetched_reference_reader(
    read: Future[ReferenceRead],
    symbols: Sequence[str],
    key: tuple[date, date],
    notes: list[str],
    fallback: ReferenceReader | None = None,
    wait_secs: float = PRE_READ_WAIT_SECS,
) -> ReferenceReader:
    """A `ReferenceReader` that hands the actor a read started before the node, so its first generation
    need not wait for the whole market's references (the owner's plan of 2026-11-12, Wave 0 (b)).

    `read` is `alpaca_reference_reader` over `symbols` for `key` = (cutoff, session), already running. The
    actor's first call for that key waits for it on the actor's worker thread. A call for another key, a
    retry after the pre-read was handed over, or a call after the pre-read failed or found no reference,
    reads afresh through `fallback` (default `alpaca_reference_reader`), as does a first call whose wait for
    the pre-read passes `wait_secs`; a later call uses a pre-read that finished after that wait, so a slow
    pre-read is not thrown away (the fresh read and the pre-read then overlap, two reads at once). Each call appends one line to `notes` saying which happened, and how
    many asked-for symbols the pre-read did not cover (those have no reference, so they are unknown, as any
    symbol `read_references` finds no close for), and logs the same line as it happens (`READ_LOG`)."""
    read_fresh = fallback or alpaca_reference_reader
    covered = frozenset(symbols)
    handed: list[bool] = []
    used: list[bool] = []

    def note(line: str) -> None:
        notes.append(line)
        read_log()(line)

    def reader(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        if (cutoff, session) != key:
            note(
                f"read at the actor's call: its key {(cutoff, session)} is not the pre-read's {key}"
            )
            return read_fresh(symbols, cutoff, session)
        if handed:
            if not used and read.done() and read.exception() is None and read.result().references:
                # the pre-read the first call stopped waiting for has since finished: use it
                used.append(True)
                note("read at the actor's call: the pre-read finished after its wait and is used")
                return read.result()
            note("read at the actor's call: a retry after the pre-read was handed over")
            return read_fresh(symbols, cutoff, session)
        handed.append(True)
        try:
            error = read.exception(timeout=wait_secs)  # waits for the pre-read, at most `wait_secs`
        except TimeoutError:
            note(f"read at the actor's call: the pre-read did not finish within {wait_secs:g} s")
            return read_fresh(symbols, cutoff, session)
        if error is not None:
            note(f"read at the actor's call: the pre-read failed ({type(error).__name__}: {error})")
            return read_fresh(symbols, cutoff, session)
        if not read.result().references:
            note("read at the actor's call: the pre-read found no reference")
            return read_fresh(symbols, cutoff, session)
        used.append(True)
        missing = sum(1 for s in symbols if s not in covered)
        note(
            f"pre-read before the node: {len(covered)} symbols; {missing} of the {len(symbols)} asked for "
            "were not in it"
        )
        return read.result()

    return reader


def alpaca_reference_reader(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
    """Each symbol's latest completed regular close before `cutoff` from Alpaca's split-adjusted daily
    `sip` bars, and the forward and reverse splits from the earliest of those closes to `session`, on
    GET-only clients built from the environment's pair. Runs on the actor's worker thread, or the window's
    pre-read thread, and logs each batch from there (`read_log`)."""
    api_key, secret_key = credentials_from_env()
    bars = with_http_timeout(StockHistoricalDataClient(api_key=api_key, secret_key=secret_key))
    actions = with_http_timeout(CorporateActionsClient(api_key=api_key, secret_key=secret_key))
    require_get_only(bars, actions)
    log = read_log()
    references = read_references(bars, symbols, cutoff, log=log)
    return ReferenceRead(
        cutoff=cutoff,
        references=references,
        splits=read_splits(actions, references, session, log=log),
        read_ns=time_ns(),
    )


class DailyBars(Protocol):
    def get_stock_bars(self, request_params: StockBarsRequest) -> object: ...


class CorporateActions(Protocol):
    def get_corporate_actions(self, request_params: CorporateActionsRequest) -> object: ...


def read_log() -> Callable[[str], None]:
    """Log a line from the calling thread under `READ_LOG`. NautilusTrader's Python `Logger` is
    `unsendable` (`crates/common/src/python/logging.rs:434-439` at the pin), so each thread makes its own,
    and each line names that thread, so the pre-read's batches and the actor's are told apart;
    it forwards to the one global logger (`logger::log`), whose writer runs on its own thread. Before a
    node has started its logging the line goes nowhere, which leaves the pre-read's notes in `notes`."""
    import threading

    from nautilus_trader.common import Logger

    info, thread = Logger(READ_LOG).info, threading.current_thread().name
    return lambda line: info(f"[{thread}] {line}")


def read_references(
    client: DailyBars,
    symbols: Sequence[str],
    cutoff: date,
    log: Callable[[str], None] | None = None,
) -> dict[str, movers_rank.Reference]:
    """Each symbol's split-adjusted daily close of the latest New York date before `cutoff` within
    `REFERENCE_LOOKBACK_DAYS`; a symbol with none is left out, so unknown. `log` is handed one line a
    batch as it ends: its number, the time, its symbols, how many had a close, and its duration, or the
    error it failed with, which is raised."""
    start = datetime.combine(cutoff - timedelta(days=REFERENCE_LOOKBACK_DAYS), time(0), NEW_YORK)
    end = datetime.combine(cutoff, time(0), NEW_YORK) - timedelta(microseconds=1)
    found: dict[str, movers_rank.Reference] = {}
    batches = math.ceil(len(symbols) / REF_BATCH)
    for n, i in enumerate(range(0, len(symbols), REF_BATCH), start=1):
        batch = list(symbols[i : i + REF_BATCH])
        began = monotonic()
        try:
            result = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    adjustment=Adjustment.SPLIT,
                    feed=DataFeed.SIP,
                )
            )
        except Exception as error:
            if log is not None:
                log(
                    f"reference batch {n}/{batches} {_iso(time_ns())} {len(batch)} symbols failed "
                    f"after {monotonic() - began:.2f} s: {type(error).__name__}: {error}"
                )
            raise
        if not isinstance(result, BarSet):
            raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
        before = len(found)
        for symbol, daily in result.data.items():
            done = [b for b in daily if b.timestamp.astimezone(NEW_YORK).date() < cutoff]
            if done:
                last = max(done, key=lambda b: b.timestamp)
                found[symbol] = movers_rank.Reference(
                    close=last.close, session=last.timestamp.astimezone(NEW_YORK).date()
                )
        if log is not None:
            log(
                f"reference batch {n}/{batches} {_iso(time_ns())} {len(batch)} symbols, "
                f"{len(found) - before} with a close, {monotonic() - began:.2f} s"
            )
    return found


def read_splits(
    client: CorporateActions,
    references: Mapping[str, movers_rank.Reference],
    session: date,
    log: Callable[[str], None] | None = None,
) -> movers_rank.SplitRead | None:
    """Forward and reverse splits from the earliest reference session to `session`, for every symbol;
    None (unknown) with no reference, or when the read reaches `SPLITS_LIMIT` and may be cut short. `log`
    is handed one line as the read ends, as `read_references` hands one a batch."""
    if not references:
        return None
    start = min(r.session for r in references.values())
    began = monotonic()
    try:
        result = client.get_corporate_actions(
            CorporateActionsRequest(
                types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT],
                start=start,
                end=session,
                limit=SPLITS_LIMIT,
            )
        )
    except Exception as error:
        if log is not None:
            log(
                f"splits read {_iso(time_ns())} failed after {monotonic() - began:.2f} s: "
                f"{type(error).__name__}: {error}"
            )
        raise
    if log is not None:
        log(f"splits read {_iso(time_ns())} answered in {monotonic() - began:.2f} s")
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        raise TypeError(
            "alpaca-py returned raw corporate actions; the client must use raw_data=False"
        )
    found = [a for kind in ("forward_splits", "reverse_splits") for a in data.get(kind, [])]
    if len(found) >= SPLITS_LIMIT:
        return None
    return movers_rank.SplitRead(start=start, end=session, actions=tuple(found))
