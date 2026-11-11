"""The movers probe's stream: the whole market's minute bars, then its trades, on one `sip` connection, with
message rates, memory and a top 50 by the movers ranking score each minute, in data-only mode (ADR 0070, its
dated addition of 2026-11-11).

Contract: alpaca-py 0.45.0 (the pin), used as shipped:

* `alpaca.data.live.stock.StockDataStream(api_key, secret_key, raw_data=True, feed=DataFeed.SIP)`,
  `subscribe_bars(handler, "*")`, then `subscribe_trades(handler, "*")` from another thread while it runs, and
  `run()` in a thread ended by `stop()`, as alpaca-py's own live stream probe runs one
  (`tests/live/streaming.py`, `live_stream_call`). Alpaca's "Streaming Market Data" page: "every symbol using
  the `*` wildcard". `raw_data=True` hands each message over as decoded, with no model built per message.
* `alpaca.data.historical.stock.StockHistoricalDataClient`: `get_stock_snapshot(StockSnapshotRequest(...,
  feed=DataFeed.SIP))` for each symbol's last session close (`daily_bar`, `previous_daily_bar`), and
  `get_stock_bars(StockBarsRequest(..., timeframe=TimeFrame.Minute, feed=DataFeed.SIP))` for the same clock
  minutes of earlier sessions.

The ranking score has no upstream (none ships one): it is the movers requirement's, `move x dvol_ratio`, where
`move` is the last bar's close over the daily close of the last session completed at the run's start,
less one, and `dvol_ratio` the
run's dollar volume (the sum of close x volume over its bars) over the mean dollar volume of the same clock
minutes in the last `NORMAL_SESSIONS` sessions. Only a symbol that has risen is ranked (the practice is long
only), and not one whose close or normal is unknown or zero; ties go by symbol. The arithmetic is here; every input is an upstream read. To bound the reads, a
normal is read only for the `CANDIDATES` symbols with the largest move each minute, so a symbol of small move
and very large volume ratio can be missed (UNCALIBRATED, as every threshold of the requirement is).

How it is kept from trading (ADR 0070, terms 3 and 5): data-only mode, which `mise run paper:data` sets and
`main` sets again; its one REST client goes through `canon_alpaca.providers.with_http_timeout`, so every
method but GET is refused before anything is sent, checked by `require_get_only` before the first request.
It builds no trading client. It streams, so the task runs it under the login's stream lock.

Bounds: `--minutes`, 1 to `MAX_MINUTES`; at most `REF_REQUESTS_PER_MINUTE` snapshot reads and
`NORMAL_BATCHES_PER_MINUTE` normal reads a minute. Each minute is one JSON line in the log folder the task
passes, outside the repository. The run exits 1 if the stream stopped early or logged an error, or a read
failed; a refused method ends it at once.

UNVERIFIED until a run measures it: whether one connection carries `*` for bars and trades on `sip`, at what
rate, and with what memory; whether a snapshot's daily close is the official close and adjusted for splits;
how many symbols a snapshot request takes (`REF_BATCH` is a choice, not a documented limit).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import data_only
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import with_http_timeout

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Sequence

    from alpaca.data.models.bars import Bar
    from alpaca.data.models.snapshots import Snapshot

NEW_YORK = ZoneInfo("America/New_York")
MAX_MINUTES = 60
"""A run is at most an hour, as a sandbox run and a window are (ADR 0044, ADR 0070)."""
TOP = 50
CANDIDATES = 100
"""Symbols with the largest move whose normal is read each minute (UNCALIBRATED)."""
NORMAL_SESSIONS = 5
"""Earlier sessions a normal is the mean of (UNCALIBRATED)."""
LOOKBACK_WEEKDAYS = 10
"""Weekdays looked back for `NORMAL_SESSIONS` sessions; a weekday with no bar for any symbol is a holiday."""
REF_BATCH = 200
REF_REQUESTS_PER_MINUTE = 20
NORMAL_BATCH = 100
NORMAL_BATCHES_PER_MINUTE = 2
REGULAR_CLOSE = (16, 0)
STOP_SECS = 15.0
"""How long the stream's thread may take to end after `stop()`, as alpaca-py's probe waits (10 s), and 5 s
for a handler in flight."""
READ_MARGIN_SECS = 10.0
"""No read starts this close to the next minute's line or the run's end, so a minute's reads stay inside
it, give or take one read (alpaca-py's (connect, read) timeout and its retries)."""
SUBSCRIBE_SECS = 30.0
"""The longest the main loop waits for alpaca-py to send the trades subscription."""
LOG_PREFIX = "movers-stream"
STREAM_LOGGER = "alpaca.data.live.websocket"


class DataOnlyRefused(RuntimeError):
    """The process is not in data-only mode, so no client is built."""


class Stream(Protocol):
    """The part of alpaca-py's `StockDataStream` this module uses."""

    def subscribe_bars(self, handler: Callable, *symbols: str) -> None: ...

    def subscribe_trades(self, handler: Callable, *symbols: str) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


class Historical(Protocol):
    """The part of alpaca-py's `StockHistoricalDataClient` this module uses (alpaca-py 0.45.0)."""

    def get_stock_snapshot(self, request_params: StockSnapshotRequest) -> object: ...

    def get_stock_bars(self, request_params: StockBarsRequest) -> object: ...


def data_client() -> StockHistoricalDataClient:
    """alpaca-py's `StockHistoricalDataClient` on the environment's pair, its session's adapter refusing every
    method but GET, checked before it is returned (ADR 0070, term 3). Refused outside data-only mode."""
    refuse_outside_data_only()
    api_key, secret_key = credentials_from_env()
    client = with_http_timeout(StockHistoricalDataClient(api_key=api_key, secret_key=secret_key))
    require_get_only(client)
    return client


def sip_stream() -> StockDataStream:
    """alpaca-py's stock stream on `sip`, raw messages. Refused outside data-only mode."""
    refuse_outside_data_only()
    api_key, secret_key = credentials_from_env()
    return StockDataStream(api_key, secret_key, raw_data=True, feed=DataFeed.SIP)


def refuse_outside_data_only() -> None:
    if not data_only():
        raise DataOnlyRefused(
            f"the movers stream runs in data-only mode only ({DATA_ONLY_ENV}=1, ADR 0070); nothing was built"
        )


def epoch_seconds(t: object) -> float | None:
    """A raw message's `t`: msgpack's `Timestamp` (`.seconds`, `.nanoseconds`), or a datetime."""
    if isinstance(t, datetime):
        return t.timestamp()
    seconds = getattr(t, "seconds", None)
    if seconds is None:
        return None
    return seconds + getattr(t, "nanoseconds", 0) / 1e9


@dataclass
class Tape:
    """What the stream's handlers saw, read and reset by the main thread each minute. The handlers run on
    the stream's own thread, so every access holds `lock`."""

    window_start: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    close: dict[str, float] = field(default_factory=dict)
    dvol: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    latest_bar: float | None = None

    async def on_bar(self, msg: dict) -> None:
        t = epoch_seconds(msg.get("t"))
        symbol, c, v = msg.get("S"), msg.get("c"), msg.get("v")
        with self.lock:
            self.counts["bars"] += 1
            if not isinstance(symbol, str) or t is None or c is None or v is None:
                self.counts["bars_unread"] += 1
                return
            self.close[symbol] = float(c)
            if t >= self.window_start:
                self.dvol[symbol] += float(c) * float(v)
                self.latest_bar = t if self.latest_bar is None else max(self.latest_bar, t)

    async def on_trade(self, msg: dict) -> None:
        with self.lock:
            self.counts["trades"] += 1

    def minute(self) -> tuple[dict[str, int], dict[str, float], dict[str, float], float | None]:
        """This minute's message counts, reset, and the closes, dollar volumes and latest bar so far."""
        with self.lock:
            counts = dict(self.counts)
            self.counts.clear()
            return counts, dict(self.close), dict(self.dvol), self.latest_bar


class Warnings(logging.Handler):
    """alpaca-py's stream warnings and errors (reconnects, a refused subscription), counted and kept."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[tuple[str, str]] = []
        self.errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelname, record.getMessage()))
        if record.levelno >= logging.ERROR:
            self.errors += 1

    def take(self) -> list[tuple[str, str]]:
        self.acquire()  # `emit` runs under the handler's lock (`Handler.handle`), on the stream's thread
        try:
            taken, self.records = self.records, []
        finally:
            self.release()
        return taken


def session_date(t: datetime) -> date:
    return t.astimezone(NEW_YORK).date()


def reference_close(snapshot: Snapshot, now: datetime) -> float | None:
    """The close of the last completed regular session in `snapshot`: its daily bar or its previous daily
    bar, whichever is latest of those before today in New York, or today's from 16:00 New York."""
    today = session_date(now)
    ny = now.astimezone(NEW_YORK)
    after_close = (ny.hour, ny.minute) >= REGULAR_CLOSE
    done = [
        b
        for b in (snapshot.daily_bar, snapshot.previous_daily_bar)
        if b is not None
        and (
            session_date(b.timestamp) < today
            or (after_close and session_date(b.timestamp) == today)
        )
    ]
    if not done:
        return None
    close = max(done, key=lambda b: b.timestamp).close
    return close if close > 0 else None


def batches(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def read_references(
    client: Historical, symbols: Sequence[str], now: datetime
) -> dict[str, float | None]:
    """Each symbol's last session close from one snapshot read per `REF_BATCH`; a symbol Alpaca returns
    nothing for is unknown (None)."""
    found: dict[str, float | None] = dict.fromkeys(symbols)
    for batch in batches(symbols, REF_BATCH):
        snaps = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=batch, feed=DataFeed.SIP)
        )
        if not isinstance(snaps, dict):
            raise TypeError(
                "alpaca-py returned raw snapshot data; the client must use raw_data=False"
            )
        for symbol, snap in snaps.items():
            if symbol in found and snap is not None:
                found[symbol] = reference_close(snap, now)
    return found


def prior_weekdays(today: date) -> Iterable[date]:
    """The `LOOKBACK_WEEKDAYS` weekdays before `today`, latest first."""
    day, found = today, 0
    while found < LOOKBACK_WEEKDAYS:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            found += 1
            yield day


def read_normals(
    client: Historical,
    symbols: Sequence[str],
    window_start: datetime,
    minutes: int,
) -> dict[str, list[list[float]]]:
    """For each symbol, `NORMAL_SESSIONS` lists of dollar volume by minute of the run's clock window
    (`window_start` for `minutes`, New York time) in the last sessions before today: one bars read per
    weekday, until that many weekdays had a bar for any symbol of the batch, at most `LOOKBACK_WEEKDAYS`."""
    start_ny = window_start.astimezone(NEW_YORK)
    sessions: dict[str, list[list[float]]] = {s: [] for s in symbols}
    held = 0
    for day in prior_weekdays(start_ny.date()):
        if held == NORMAL_SESSIONS:
            break
        begin = datetime.combine(day, start_ny.timetz().replace(tzinfo=None), NEW_YORK)
        bars = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=list(symbols),
                timeframe=TimeFrame.Minute,
                start=begin,
                end=begin + timedelta(minutes=minutes),
                feed=DataFeed.SIP,
            )
        )
        if not isinstance(bars, BarSet):
            raise TypeError("alpaca-py returned raw bar data; the client must use raw_data=False")
        if not any(bars.data.get(s) for s in symbols):
            continue  # a holiday: no session
        held += 1
        for symbol in symbols:
            sessions[symbol].append(minute_dollars(bars.data.get(symbol) or [], begin, minutes))
    return sessions


def minute_dollars(bars: Iterable[Bar], begin: datetime, minutes: int) -> list[float]:
    by_minute = [0.0] * minutes
    for bar in bars:
        offset = int((bar.timestamp - begin).total_seconds() // 60)
        if 0 <= offset < minutes:
            by_minute[offset] += bar.close * bar.volume
    return by_minute


def normal(sessions: list[list[float]], elapsed: int) -> float | None:
    """The mean over sessions of the dollar volume in the window's first `elapsed` minutes; None when no
    session was read or the mean is zero."""
    if not sessions or elapsed <= 0:
        return None
    mean = sum(sum(s[:elapsed]) for s in sessions) / len(sessions)
    return mean if mean > 0 else None


@dataclass(frozen=True)
class Scored:
    symbol: str
    score: float
    move: float
    dvol_ratio: float
    close: float
    reference: float
    dvol: float


def moves(close: dict[str, float], references: dict[str, float | None]) -> dict[str, float]:
    return {
        s: c / ref - 1.0
        for s, c in close.items()
        if (ref := references.get(s)) is not None and ref > 0
    }


def rank(
    close: dict[str, float],
    dvol: dict[str, float],
    references: dict[str, float | None],
    normals: dict[str, list[list[float]]],
    elapsed: int,
    top: int = TOP,
) -> list[Scored]:
    """The top `top` symbols by `move x dvol_ratio`, then by symbol; a symbol that has not risen, or whose
    reference or normal is unknown, is not ranked."""
    scored = []
    for symbol, move in moves(close, references).items():
        if move <= 0:
            continue  # the practice is long only: a symbol that has not risen is not a candidate
        base = normal(normals.get(symbol, []), elapsed)
        if base is None or symbol not in dvol:
            continue
        ratio = dvol[symbol] / base
        ref = references[symbol]
        assert ref is not None
        scored.append(Scored(symbol, move * ratio, move, ratio, close[symbol], ref, dvol[symbol]))
    scored.sort(key=lambda s: (-s.score, s.symbol))
    return scored[:top]


def peak_rss_mib() -> float:
    """The process's peak resident memory (`getrusage`'s `ru_maxrss`: bytes on macOS, KiB on Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 20) if sys.platform == "darwin" else peak / (1 << 10)


@dataclass
class Reads:
    """What the main thread has read over REST, and what failed."""

    references: dict[str, float | None] = field(default_factory=dict)
    normals: dict[str, list[list[float]]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)

    def read(self, what: str, fn: Callable[[], None]) -> bool:
        """Run one read; a failure is recorded by name and returns False."""
        try:
            fn()
        except MethodRefused:
            raise
        # alpaca-py's HTTP errors are `APIError`, requests' network errors OSErrors (`_one_request`)
        except (APIError, OSError, ValueError, TypeError) as e:
            self.failed.append(f"{what}: {type(e).__name__}: {e}")
            return False
        return True


def refresh(
    reads: Reads,
    client: Historical,
    close: dict[str, float],
    window_start: datetime,
    minutes: int,
    clock: Callable[[], float],
    until: float,
) -> int:
    """Read references for symbols not yet looked up, then normals for the top `CANDIDATES` by move not yet
    read, each within its bound per minute, starting no read at or after `until` on `clock`. References are
    taken as of the run's start, so one run's moves share one base. A batch that fails leaves its symbols
    unknown, not read again, so one bad batch cannot hold up the rest. Returns the reads that failed."""
    before = len(reads.failed)
    new = sorted(s for s in close if s not in reads.references)[
        : REF_BATCH * REF_REQUESTS_PER_MINUTE
    ]
    for batch in batches(new, REF_BATCH):
        if clock() >= until:
            break
        if not reads.read(
            f"snapshots ({len(batch)})",
            lambda b=batch: reads.references.update(read_references(client, b, window_start)),
        ):
            reads.references.update(dict.fromkeys(batch))
    leaders = sorted(moves(close, reads.references).items(), key=lambda m: (-m[1], m[0]))[
        :CANDIDATES
    ]
    unread = [s for s, _ in leaders if s not in reads.normals]
    for batch in list(batches(unread, NORMAL_BATCH))[:NORMAL_BATCHES_PER_MINUTE]:
        if clock() >= until:
            break
        if not reads.read(
            f"normals ({len(batch)})",
            lambda b=batch: reads.normals.update(read_normals(client, b, window_start, minutes)),
        ):
            reads.normals.update({s: [] for s in batch})
    return len(reads.failed) - before


def subscribe_trades(stream: Stream, tape: Tape) -> list[str]:
    """Add trades `*` to the running stream. alpaca-py's `_subscribe` waits, with no bound, for its message
    to be sent on the stream's loop (`run_coroutine_threadsafe(...).result()`), so the call runs in its own
    thread and is waited on for `SUBSCRIBE_SECS`; what failed, or that it did not finish, is returned."""
    failed: list[str] = []

    def call() -> None:
        try:
            stream.subscribe_trades(tape.on_trade, "*")
        except Exception as e:  # noqa: BLE001, returned: the run fails on it; the handler stays registered
            failed.append(f"subscribe trades: {type(e).__name__}: {e}")

    helper = threading.Thread(target=call, name="movers-stream-subscribe", daemon=True)
    helper.start()
    helper.join(SUBSCRIBE_SECS)
    if helper.is_alive():
        return [f"subscribe trades: not sent within {SUBSCRIBE_SECS} s"]
    return failed


def minute_start(t: float) -> float:
    return t - t % 60


def run(
    stream: Stream,
    client: Historical,
    minutes: int,
    trades_after: int,
    log: Path,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Stream bars, and trades from minute `trades_after`, for `minutes`; one JSON line a minute in `log`,
    then a summary. Returns 0, or 1 if the stream stopped early or logged an error or a read failed."""
    start = clock()
    window = minute_start(start)
    window_start = datetime.fromtimestamp(window, UTC)
    tape = Tape(window_start=window)
    warnings = Warnings()
    logger = logging.getLogger(STREAM_LOGGER)
    logger.addHandler(warnings)
    reads = Reads()
    stopped_early = False
    trades_on = False
    stream.subscribe_bars(tape.on_bar, "*")
    if trades_after == 0:
        stream.subscribe_trades(tape.on_trade, "*")
        trades_on = True
    failures: list[str] = []

    def runner() -> None:
        try:
            stream.run()
        except Exception as e:  # noqa: BLE001, recorded and the run fails on it
            failures.append(f"stream: {type(e).__name__}: {e}")

    thread = threading.Thread(target=runner, name="movers-stream", daemon=True)
    thread.start()
    try:
        with log.open("a", encoding="utf-8") as out:
            for n in range(1, minutes + 1):
                wait = start + n * 60 - clock()
                if wait > 0:
                    sleep(wait)
                if not thread.is_alive():
                    stopped_early = True
                if not trades_on and n >= trades_after and n < minutes:
                    failures.extend(subscribe_trades(stream, tape))
                    trades_on = True
                counts, close, dvol, latest = tape.minute()
                now = datetime.fromtimestamp(clock(), UTC)
                until = min(start + (n + 1) * 60, start + minutes * 60) - READ_MARGIN_SECS
                failed = refresh(reads, client, close, window_start, minutes, clock, until)
                elapsed = 0 if latest is None else int((latest - window) // 60) + 1
                ranked = rank(close, dvol, reads.references, reads.normals, elapsed)
                line = {
                    "minute": n,
                    "at": now.isoformat(),
                    "messages": counts,
                    "trades_subscribed": trades_on,
                    "peak_rss_mib": round(peak_rss_mib(), 1),
                    "symbols": len(close),
                    "references_known": sum(1 for v in reads.references.values() if v is not None),
                    "references_unknown": sum(1 for v in reads.references.values() if v is None),
                    "normals_read": len(reads.normals),
                    "elapsed_minutes": elapsed,
                    "reads_failed": failed,
                    "stream_log": warnings.take(),
                    "stream_alive": thread.is_alive(),
                    "top": [s.__dict__ for s in ranked],
                }
                out.write(json.dumps(line) + "\n")
                out.flush()
            stream.stop()
            thread.join(STOP_SECS)
            if thread.is_alive():
                failures.append(f"the stream's thread did not end within {STOP_SECS} s of stop()")
            if stopped_early:
                failures.append("the stream stopped before the run's end")
            if warnings.errors:
                failures.append(f"the stream logged {warnings.errors} error(s)")
            failures.extend(reads.failed)
            summary = {
                "summary": True,
                "minutes": minutes,
                "failures": failures,
                "requests": [
                    {
                        "account": c.account,
                        "api": c.api,
                        "total": c.total,
                        "peak": c.peak,
                        "rate_limited": c.rate_limited_total,
                    }
                    for c in request_counts()
                ],
            }
            out.write(json.dumps(summary) + "\n")
    finally:
        stream.stop()
        thread.join(STOP_SECS)
        logger.removeHandler(warnings)
    for f in failures:
        print(f"movers-stream: {f}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.movers_stream", description=__doc__
    )
    parser.add_argument("--minutes", type=int, default=10, help=f"run length, 1 to {MAX_MINUTES}")
    parser.add_argument(
        "--trades-after", type=int, help="the minute trades are added (default: half the run)"
    )
    parser.add_argument(
        "--log-dir", type=Path, required=True, help="an absolute folder outside the repository"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be 1 to {MAX_MINUTES}")
    trades_after = args.minutes // 2 if args.trades_after is None else args.trades_after
    if trades_after < 0:
        parser.error("--trades-after must be 0 or more")
    if not args.log_dir.is_absolute():
        parser.error(f"--log-dir must be an absolute folder, got {str(args.log_dir)!r}")
    os.environ[DATA_ONLY_ENV] = (
        "1"  # the task sets it too; the module does not rely on that (ADR 0070)
    )
    client = data_client()
    stream = sip_stream()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log = args.log_dir / f"{LOG_PREFIX}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    print(
        f"movers-stream: {args.minutes} min on sip, trades from minute {trades_after}, log {log}",
        flush=True,
    )
    return run(stream, client, args.minutes, trades_after, log)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    sys.exit(main())
