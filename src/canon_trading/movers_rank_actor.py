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
  Bars name `params={"feed": feed, "wildcard": True, "updated_bars": True}` and statuses name
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
`REFERENCE_LOOKBACK_DAYS`. `REF_BATCH` is a choice, not a documented limit.

Limits (ADR 0087): the move from the regular open is not known (no opens are passed); trade corrections and
cancels come only on the trade channel, which the actor does not read, so it sees a late trade only as an
updated bar; whether Alpaca's daily bar close is the official closing price is UNVERIFIED.
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
SPLITS_LIMIT = 1000
"""alpaca-py's `CorporateActionsRequest.limit` default; a read that reaches it is incomplete, so unknown."""
TIMER = "movers-rank"
RECORD_PREFIX = "movers-rank record "


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
    a file that exists and cannot be used still publishes nothing. The window sets it overnight only,
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
        self.max_data_age_secs = max_data_age_secs
        self.rank_without_baseline = rank_without_baseline


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
        self._refs: Future[ReferenceRead] | None = None
        self._refs_key: tuple[date, date] | None = None
        self._baseline: Future[movers_rank.BaselineRead] | None = None
        self._baseline_session: date | None = None
        self.last_record: dict[str, object] | None = None

    def on_start(self) -> None:
        """Subscribe to every cached Alpaca equity's minute bars and statuses, start the reads, set the
        timer."""
        equities = [i for i in self.cache.instruments(venue=VENUE) if isinstance(i, Equity)]
        if not equities:
            raise RuntimeError("MoversRankActor cannot start: no equity is in the cache")
        self._assets = {i.id.symbol.value: dict(i.info) for i in equities}
        self._security_types = movers_rank.security_types_from_names(self._assets)
        self.subscribe_bars(
            [BarType.from_str(f"{i.id}-{BAR_SPEC}") for i in equities],
            params={"feed": self._feed, "wildcard": True, "updated_bars": True},
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
        symbol = bar.bar_type.instrument_id.symbol.value
        self._receipt_ns = self.clock.timestamp_ns()
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
            "latency_ns": {
                "publication": None,
                "receipt": self._receipt_ns,
                "generation": now,
                "entry_eligible": None,
                "submission": None,
            },
        }
        reasons = self._take_invalid()
        refs = self._result(self._refs, "references and splits", reasons)
        if refs is not None and (problem := reference_problem(refs)) is not None:
            reasons.append(f"references and splits not usable: {problem}")
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
        record["stale_symbols"] = [
            r.instrument_id.symbol.value
            for r in generation.rows
            if "last_bar:stale" in r.excluded_by
        ]
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
        to another `(cutoff, session)` pair, or a baseline read for another trade date, is never reused."""
        session = trade_date(now)
        cutoff = reference_cutoff(now)
        key = (cutoff, session)
        if self._refs_key != key or self._refs_unusable(self._refs):
            self._refs_key = key
            symbols = sorted(self._assets)
            self._refs = self._submit(lambda: self._reference_reader(symbols, cutoff, session))
        if self._baseline_session != session or self._baseline_unusable(self._baseline):
            self._baseline_session = session
            self._baseline = self._submit(
                lambda: movers_rank.read_baselines(self._baseline_path, session)
            )

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


def alpaca_reference_reader(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
    """Each symbol's latest completed regular close before `cutoff` from Alpaca's split-adjusted daily
    `sip` bars, and the forward and reverse splits from the earliest of those closes to `session`, on
    GET-only clients built from the environment's pair. Runs on the actor's worker thread."""
    api_key, secret_key = credentials_from_env()
    bars = with_http_timeout(StockHistoricalDataClient(api_key=api_key, secret_key=secret_key))
    actions = with_http_timeout(CorporateActionsClient(api_key=api_key, secret_key=secret_key))
    require_get_only(bars, actions)
    references = read_references(bars, symbols, cutoff)
    return ReferenceRead(
        cutoff=cutoff,
        references=references,
        splits=read_splits(actions, references, session),
        read_ns=time_ns(),
    )


class DailyBars(Protocol):
    def get_stock_bars(self, request_params: StockBarsRequest) -> object: ...


class CorporateActions(Protocol):
    def get_corporate_actions(self, request_params: CorporateActionsRequest) -> object: ...


def read_references(
    client: DailyBars, symbols: Sequence[str], cutoff: date
) -> dict[str, movers_rank.Reference]:
    """Each symbol's split-adjusted daily close of the latest New York date before `cutoff` within
    `REFERENCE_LOOKBACK_DAYS`; a symbol with none is left out, so unknown."""
    start = datetime.combine(cutoff - timedelta(days=REFERENCE_LOOKBACK_DAYS), time(0), NEW_YORK)
    end = datetime.combine(cutoff, time(0), NEW_YORK) - timedelta(microseconds=1)
    found: dict[str, movers_rank.Reference] = {}
    for i in range(0, len(symbols), REF_BATCH):
        result = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=list(symbols[i : i + REF_BATCH]),
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                adjustment=Adjustment.SPLIT,
                feed=DataFeed.SIP,
            )
        )
        if not isinstance(result, BarSet):
            raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
        for symbol, daily in result.data.items():
            done = [b for b in daily if b.timestamp.astimezone(NEW_YORK).date() < cutoff]
            if done:
                last = max(done, key=lambda b: b.timestamp)
                found[symbol] = movers_rank.Reference(
                    close=last.close, session=last.timestamp.astimezone(NEW_YORK).date()
                )
    return found


def read_splits(
    client: CorporateActions, references: Mapping[str, movers_rank.Reference], session: date
) -> movers_rank.SplitRead | None:
    """Forward and reverse splits from the earliest reference session to `session`, for every symbol;
    None (unknown) with no reference, or when the read reaches `SPLITS_LIMIT` and may be cut short."""
    if not references:
        return None
    start = min(r.session for r in references.values())
    result = client.get_corporate_actions(
        CorporateActionsRequest(
            types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT],
            start=start,
            end=session,
            limit=SPLITS_LIMIT,
        )
    )
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        raise TypeError(
            "alpaca-py returned raw corporate actions; the client must use raw_data=False"
        )
    found = [a for kind in ("forward_splits", "reverse_splits") for a in data.get(kind, [])]
    if len(found) >= SPLITS_LIMIT:
        return None
    return movers_rank.SplitRead(start=start, end=session, actions=tuple(found))
