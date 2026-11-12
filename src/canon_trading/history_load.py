"""Alpaca's historical `sip` bars or trade prints into a `ParquetDataCatalog`, GET only and paced (ADR 0093).

`mise run paper:history <account>` runs this module with one paper account's pair in data-only mode
(ADR 0070): it sends market data GETs and nothing else, and takes neither the account's lock nor a
stream lock, since it places nothing and opens no stream. A separate catalog lock excludes another
history load into the same root, from the coverage read through the last write.

Contracts:

* alpaca-py 0.45.0 `StockHistoricalDataClient.get_stock_bars(StockBarsRequest)`, which takes a list of
  symbols and follows `next_page_token` through every page of 10,000 rows before it returns
  (`alpaca/data/historical/stock.py`, `page_size=10_000`; `alpaca/common/rest.py`, `_get_marketdata`).
  Alpaca's bars reference: the limit "applies to the total number of data points, not per symbol", and
  the feed is named (`sip`) on every request (https://docs.alpaca.markets/us/reference/stockbars.md).
* The adapter's own window and parsing (`canon_alpaca.data.alpaca_bars_window`, `parse_bar`,
  `bar_timeframe`), followed by the engine's inclusive `ts_init` filter, so a bar written here is the
  bar `canon_trading.bars_to_catalog.load_bars` would write. A gap starts one nanosecond after coverage;
  a venue or test stand-in truncating nanoseconds can return the boundary bar again. Here the stand-in's
  `datetime.fromisoformat` truncates; alpaca-py keeps the request's nanoseconds via `isoformat()`
  (`alpaca/common/requests.py:54` at `c42add7c0a70`). Alpaca's server-side parsing is UNVERIFIED.
  `bar_roundings` names rounded SIP prices with a count and the first example, as the adapter does.
* NautilusTrader v2.0.0rc8 `ParquetDataCatalog.write_bars(data, start, end)`, the bounds overriding the
  file's interval as the node's catalog write-back gives them, and `get_missing_intervals_for_request`
  (https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md, "Reading data"): what a root
  covers is not asked for again, and an empty answer writes nothing, so it is asked for again.
* requests' "Event Hooks" (Advanced Usage): a response hook on the client's session raises `RateLimited`
  at Alpaca's first 429, before alpaca-py would send the request again. alpaca-py retries 429 and 504
  three times (`DEFAULT_RETRY_EXCEPTION_CODES`), its data client takes no retry setting, and a
  `retry_attempts` of 0 keeps the default (`RESTClient.__init__` overrides only a value above 0). The
  adapter's execution client ends an order command the same way (`canon_alpaca.execution`,
  `end_at_first_error`).
* Python 3.14 `fcntl.flock(LOCK_EX | LOCK_NB)` and `open(..., "x")`: an advisory catalog lock and
  exclusive feed-marker creation. The feed is checked again before each write. Legacy writers
  (`load_bars` and quote write-back) do not take this lock: use separate roots or run them separately.

The pace: `PACE`, the row of ADR 0093's table. After each bar call the adapter's request counter
(`canon_alpaca.providers.request_counts`) gives the data and trading GETs the call sent; the next call
starts no sooner than the data GETs allow at `PACE.data_per_minute`. A call that sent any trading
request stops the run, since `PACE.trading_per_minute` is 0: this module needs no trading GET, as the
instruments come from the catalog. Within one call alpaca-py sends its pages back to back, so the
ceiling holds across calls, not inside one. `MAX_ROWS_PER_CALL` bounds planned rows, and an overfull
response stops before any of it is written. Ten full pages is an estimate, not a page ceiling: Alpaca
may return short pages and alpaca-py retries 504s. Each process paces itself; separate roots can share
an account's budget, so their combined rate is UNCALIBRATED and a 429 still stops each run by name.

Instruments: a bar is parsed against its `Equity` (price precision), read from the catalog root. The
definitions are written there by the asset list load, one trading GET for the whole list (ADR 0093). A
symbol with no definition in the root is refused by name before any request or lock-file creation.
The root must already be a directory, and inputs and ledger exclusions are checked before locking it.

What stops a run, by name, keeping what was written: a 429 on any API; a request Alpaca refuses (a 403
on `sip` included; nothing falls back to another feed); a root that holds another feed's data
(`bars_to_catalog.catalog_feed`); and any other failure. A rerun asks only for what the root lacks.

Trades: `--data trades` uses the adapter's `fetch_trades` and `parse_trade`, on Alpaca's documented
GET /v2/stocks/{symbol}/trades (https://docs.alpaca.markets/us/reference/stocktradesingle-1, read
2026-11-11), through alpaca-py 0.45.0's public `RESTClient.get`. Each symbol and missing interval is
fetched in ascending pages, retaining exact raw nanoseconds and excluding canceled/incorrect prints
as the adapter does. Trades at one timestamp keep their separate IDs; none are aggregated into bars.
The `TradeTick` representation drops Alpaca's conditions (`c`), exchange (`x`) and tape (`z`), without
filtering by those condition flags. It sets `aggressor_side` to `NO_AGGRESSOR`; it does not infer a side
(`parse_trade` and `crates/model/src/data/trade.rs` at d53849b216e2).
`ts_init` equals `ts_event`, as on the adapter's historical response. An end must be at least
`PUBLICATION_DELAY` before now, as the bars loader requires for published SIP history.
Both bounds are inclusive, but this loader requires `start < end` and refuses a start before
1970-01-01 UTC, the catalog's epoch, before creating a lock or requesting data.
The CLI validates ISO 8601 and time-zone presence, then uses the pinned core datetime converters
(`python/nautilus_trader/core/datetime.py`) to retain nanoseconds in the supplied bounds too.

The writer is NautilusTrader v2.0.0rc8 `ParquetDataCatalog.write_trade_ticks(ticks, start, end)`:
`python/nautilus_trader/persistence/__init__.pyi:168` and `docs/concepts/data/catalog.md`, "Writing data",
at d53849b216e2. Each write contains one instrument, ordered by `ts_init`, with inclusive nanosecond
bounds; coverage uses `NautilusDataType.TradeTick` and the instrument ID. The same catalog lock,
feed marker, 429 hook and ADR 0093 pace apply. For trades a requests transport adapter spaces every
actual GET start by at least `60 / PACE.data_per_minute` seconds, including pages and SDK 504 retries.
The schedule starts from each actual send, so slow responses and retry backoffs earn no later burst.
It delegates to the existing GET-only timeout/counter adapters and restores them after the load
(requests 2.34.2 `docs/user/advanced.rst`, "Transport Adapters", `BaseAdapter.send` and `Session.mount`
at 6e83187b8feb). The SDK's retry policy and the first-429 stop remain in force.
There is no upper bound on prints per time unit. Fetching at most `MAX_ROWS_PER_CALL + 1` valid prints
detects an overfull answer without treating a truncated result as complete: the interval is loaded in
parts, each cut before the last timestamp its answer reached, written with coverage ending one
nanosecond before that timestamp, and the next part starts there, so no timestamp is split across parts.
More than `MAX_ROWS_PER_CALL` prints at one timestamp stops by name. Prints are written in time order
(the pinned writer refuses others), the sort stable. The adapter's page cap still applies, including to
invalid prints; it fails rather than returning incomplete history. Empty or invalid-only history writes
no coverage. An interval once written is covered for good: a print Alpaca marks canceled or incorrect,
or reports late, after the load is not picked up by a rerun (how late Alpaca does so is UNVERIFIED), so
a load waits at least `PUBLICATION_DELAY`, and a root reloaded from empty takes later corrections.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import StockTradesRequest
from canon_alpaca.constants import VENUE
from canon_alpaca.data import alpaca_bars_window
from canon_alpaca.data import bar_interval_ns
from canon_alpaca.data import bar_roundings
from canon_alpaca.data import bar_timeframe
from canon_alpaca.data import fetch_trades
from canon_alpaca.data import parse_bar
from canon_alpaca.data import parse_trade
from canon_alpaca.data import was_rounded
from canon_alpaca.providers import DATA_API
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import data_only
from canon_alpaca.providers import request_counts
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import session_of
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model import BarType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog
from requests.adapters import BaseAdapter

from canon_trading.bars_to_catalog import DEFAULT_CATALOG
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.bars_to_catalog import PUBLICATION_DELAY
from canon_trading.bars_to_catalog import _holds_data
from canon_trading.bars_to_catalog import catalog_feed
from canon_trading.session_window import SOURCE_ROOT


@dataclass(frozen=True)
class Pace:
    """GETs a minute this module may send, per Alpaca API."""

    data_per_minute: int
    trading_per_minute: int


PACE = Pace(data_per_minute=150, trading_per_minute=0)
"""ADR 0093, "The pace", row `paper:history`: UNCALIBRATED. Alpaca's market data page gives historical
API calls of 200 a minute on its free plan and 10,000 on its paid one; which applies on a paper login is
not measured (UNVERIFIED), so the row stays under the lower one. The trading API is not used (0)."""

FEED = DataFeed.SIP
"""Named on every request; a refusal stops the run and nothing falls back to `iex` (ADR 0093)."""

SYMBOLS_PER_CALL = 200
"""Symbols in one request: about 1.2 KB of query at five characters a symbol. Alpaca states no most
(UNVERIFIED)."""

PAGE_ROWS = 10_000
"""alpaca-py 0.45.0's page size for bars (`stock.py`, `page_size=10_000`), the most Alpaca returns in one
page."""

MAX_ROWS_PER_CALL = 10 * PAGE_ROWS
"""The planned row bound, checked again on the whole response before writing. Count every possible
minute, or 23 hours between daily opens to allow for New York's spring DST, including a boundary row
returned by datetime truncation. Symbols are batched by that count and long windows segmented.
Ten full pages is an estimate: short pages or retries can send more GETs (ADR 0093). alpaca-py gathers
the response before the row check, so a venue breaking the window/spacing contract can exceed this
memory estimate before the run stops."""

BAR_SPECS = {"1-MINUTE": "1-MINUTE-LAST-EXTERNAL", "1-DAY": "1-DAY-LAST-EXTERNAL"}
"""The bar specifications this module loads, by the name `--bars` takes."""

LEDGER = SOURCE_ROOT / "ledger"


class HistoryStopped(RuntimeError):
    """The run stopped, by name; what it wrote before stays."""


class RateLimited(HistoryStopped):
    """Alpaca answered 429; the run stops and asks for nothing more."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def stop_at_429(response: requests.Response, *args: object, **kwargs: object) -> requests.Response:
    """A requests response hook: Alpaca's first 429 ends the call, before alpaca-py sends it again."""
    if response.status_code == 429:
        path = urlsplit(response.url or "").path
        raise RateLimited(
            f"Alpaca answered 429 Too Many Requests on {path}; the load stopped and asks for nothing more"
        )
    return response


def history_client() -> StockHistoricalDataClient:
    """alpaca-py's data client on the environment's pair, GET only, with the 429 hook (this module's
    header). Refused outside data-only mode."""
    if not data_only():
        raise HistoryStopped(
            f"paper:history runs in data-only mode only ({DATA_ONLY_ENV}=1, ADR 0070); nothing was built"
        )
    try:
        api_key, secret_key = credentials_from_env()
        client = with_http_timeout(
            StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        )
        require_get_only(client)
        session = session_of(client)
        assert session is not None  # with_http_timeout raised otherwise
        session.hooks["response"].append(stop_at_429)
        return client
    except HistoryStopped:
        raise
    except Exception as e:
        raise HistoryStopped(
            f"building the GET-only Alpaca history client failed ({type(e).__name__}): {e}"
        ) from e


@dataclass(frozen=True)
class _Sent:
    data: int
    trading: int
    limited: int


def _sent() -> _Sent:
    counts = request_counts()
    return _Sent(
        data=sum(c.total for c in counts if c.api == DATA_API),
        trading=sum(c.total for c in counts if c.api == TRADING_API),
        limited=sum(c.rate_limited_total for c in counts),
    )


def margin(spec: str) -> timedelta:
    """How long before now a window of `spec` bars must end, as `bars_to_catalog.load_bars` holds it."""
    bar_type = BarType.from_str(f"AAPL.{VENUE}-{BAR_SPECS[spec]}")
    interval = timedelta(microseconds=bar_interval_ns(bar_type) // 1_000)
    return max(interval, PUBLICATION_DELAY)


def _segments(
    intervals: Sequence[tuple[int, int]], step: int, max_rows: int
) -> Iterator[tuple[int, int]]:
    """Cut inclusive, disjoint parts using the minimum spacing `step` between bars. Allow for a venue
    or test stand-in truncating nanoseconds and returning a bar just before `lo`. The stand-in does
    this; alpaca-py preserves them (`alpaca/common/requests.py:54` at `c42add7c0a70`). Whether Alpaca's
    server truncates is UNVERIFIED.
    """
    for lo, hi in intervals:
        while lo <= hi:
            request_lo = lo // 1_000 * 1_000
            end = min(hi, request_lo + max_rows * step - 1)
            yield lo, end
            lo = end + 1


def _batches(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


@contextmanager
def _catalog_lock(path: Path) -> Generator[None]:
    """Python 3.14 `fcntl.flock`: one history loader per root, from coverage read through writes.

    The path is an existing root validated by `load_history`. This is an advisory catalog lock, not
    an account or stream lock. Legacy writers that do not take it must use another root or run
    separately (ADR 0093).
    """
    with (path / ".history-load.lock").open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise HistoryStopped(f"another history load holds the catalog at {path}") from e
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


def _check_feed(path: Path) -> None:
    held = catalog_feed(path)
    if held is not None and held != FEED.value:
        raise HistoryStopped(
            f"the catalog at {path} holds {held} data and this load is {FEED.value}: give each feed its own "
            "root (bars_to_catalog's header)"
        )


def _claim_feed(path: Path) -> bool:
    """Recheck and exclusively create the marker under the lock; return whether this load created it."""
    _check_feed(path)
    try:
        with (path / FEED_FILE).open("x", encoding="ascii") as marker:
            marker.write(f"{FEED.value}\n")
    except FileExistsError:
        _check_feed(path)
        return False
    return True


@contextmanager
def _paced_trade_gets(
    client: Any,
    pace: Pace,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Generator[None]:
    """requests 2.34.2's Transport Adapters contract (`BaseAdapter.send`, `Session.mount`): pace
    each actual send, including SDK 504 retries. Delegate to the existing GET-only timeout/counter
    adapters and restore them on exit. All URL prefixes share one schedule; idle time earns no burst.
    """
    session = session_of(client)
    if session is None:
        raise HistoryStopped(
            "the trade history client has no requests.Session; cannot pace its GETs"
        )
    next_at = clock()

    def wait() -> None:
        nonlocal next_at
        delay = next_at - clock()
        if delay > 0:
            sleep(delay)
        next_at = clock() + 60 / pace.data_per_minute

    class PacedAdapter(BaseAdapter):
        def __init__(self, delegate: BaseAdapter):
            self.delegate = delegate

        def send(self, request, *args, **kwargs) -> requests.Response:
            wait()
            return self.delegate.send(request, *args, **kwargs)

        def close(self) -> None:
            self.delegate.close()

    mounted = list(session.adapters.items())
    try:
        for prefix, adapter in mounted:
            session.mount(prefix, PacedAdapter(adapter))
        yield
    finally:
        for prefix, adapter in mounted:
            session.mount(prefix, adapter)


def load_history(
    client: Any,
    symbols: Sequence[str],
    specs: Sequence[str],
    start: datetime,
    end: datetime,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    pace: Pace = PACE,
    symbols_per_call: int = SYMBOLS_PER_CALL,
    max_rows: int = MAX_ROWS_PER_CALL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    out: Callable[[str], None] = print,
) -> int:
    """Load `specs` bars of `symbols` whose `ts_init` lies in `[start, end]` into the catalog root, and
    return the bars written. Raises `HistoryStopped` (or `RateLimited`) naming what stopped it."""
    try:
        for name, value in (("start", start), ("end", end)):
            if value.tzinfo is None:
                raise HistoryStopped(f"{name} must carry a time zone, got {value}")
        if start >= end:
            raise HistoryStopped(f"start {start} is not before end {end}")
        if not symbols:
            raise HistoryStopped("at least one symbol is required")
        if not specs or any(spec not in BAR_SPECS for spec in specs):
            raise HistoryStopped(
                f"bar specifications must be one or more of {', '.join(BAR_SPECS)}, got {list(specs)}"
            )
        for spec in specs:
            latest = now() - margin(spec)
            if end > latest:
                raise HistoryStopped(
                    f"end {end} is after {latest}, less than {margin(spec)} before now, for {spec} bars: "
                    "the catalog would record a bar not yet formed or published as covered"
                )
        path = Path(catalog_path).resolve()
        if (
            path == LEDGER.resolve()
            or LEDGER.resolve() in path.parents
            or "ledger" in Path(catalog_path).parts
        ):
            raise HistoryStopped(
                f"the catalog at {catalog_path} is under ledger/, which holds the trial ledger only"
            )
        if not path.is_dir():
            raise HistoryStopped(f"the catalog at {path} must be an existing directory")
        _check_feed(path)
        catalog = ParquetDataCatalog(str(path))
        ids = [f"{s}.{VENUE}" for s in symbols]
        instruments = {str(i.id): i for i in catalog.instruments(instrument_ids=ids)}
        if absent := [i for i in ids if i not in instruments]:
            raise HistoryStopped(
                f"no instrument definition in {path} for {', '.join(absent[:20])}"
                f"{' and more' if len(absent) > 20 else ''}: the asset list load writes them (ADR 0093); "
                "nothing was requested"
            )
        with _catalog_lock(path):
            _check_feed(path)
            return _load_history(
                client,
                symbols,
                specs,
                start,
                end,
                path,
                catalog,
                instruments,
                pace=pace,
                symbols_per_call=symbols_per_call,
                max_rows=max_rows,
                clock=clock,
                sleep=sleep,
                now=now,
                out=out,
            )
    except HistoryStopped:
        raise
    except Exception as e:
        raise HistoryStopped(
            f"the history load of {', '.join(symbols[:20])} into {catalog_path} failed "
            f"({type(e).__name__}): {e}"
        ) from e


def _load_history(
    client: Any,
    symbols: Sequence[str],
    specs: Sequence[str],
    start: datetime,
    end: datetime,
    path: Path,
    catalog: ParquetDataCatalog,
    instruments: dict[str, Any],
    *,
    pace: Pace,
    symbols_per_call: int,
    max_rows: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    out: Callable[[str], None],
) -> int:
    written = 0
    next_at = clock()
    start_ns, end_ns = dt_to_unix_nanos(start), dt_to_unix_nanos(end)
    for spec in specs:
        bar_types = {s: BarType.from_str(f"{s}.{VENUE}-{BAR_SPECS[spec]}") for s in symbols}
        interval = timedelta(microseconds=bar_interval_ns(next(iter(bar_types.values()))) // 1_000)
        timeframe = bar_timeframe(next(iter(bar_types.values())))
        gaps: dict[tuple[tuple[int, int], ...], list[str]] = {}
        for symbol, bar_type in bar_types.items():
            missing = catalog.get_missing_intervals_for_request(
                start_ns, end_ns, NautilusDataType.Bar, str(bar_type)
            )
            if missing:
                gaps.setdefault(tuple((lo, hi) for lo, hi in missing), []).append(symbol)
        step = bar_interval_ns(next(iter(bar_types.values())))
        if spec == "1-DAY":
            # New York daily opens can be 23 hours apart at spring DST, even though parse_bar
            # stamps each close a fixed 24 hours after its open (data.py, "Bar windows").
            step -= step // 24
        for intervals, group in gaps.items():
            for lo, hi in _segments(intervals, step, max_rows):
                window = alpaca_bars_window(
                    unix_nanos_to_dt(lo), unix_nanos_to_dt(hi), now(), interval
                )
                if window is None:
                    continue
                per_symbol = (hi - lo // 1_000 * 1_000) // step + 1
                size = max(1, min(symbols_per_call, max_rows // per_symbol))
                for batch in _batches(group, size):
                    wait = next_at - clock()
                    if wait > 0:
                        sleep(wait)
                    began = clock()
                    before = _sent()
                    request = StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=timeframe,
                        start=window[0],
                        end=window[1],
                        feed=FEED,
                    )
                    what = (
                        f"{spec} bars of {len(batch)} symbol(s) from {batch[0]}, feed {FEED.value}"
                    )
                    try:
                        result = client.get_stock_bars(request)
                    except HistoryStopped:
                        raise
                    except APIError as e:
                        raise HistoryStopped(
                            f"Alpaca refused the {what} request (HTTP {e.status_code}): {e}; nothing falls "
                            "back to another feed"
                        ) from e
                    except Exception as e:
                        raise HistoryStopped(
                            f"the {what} request failed ({type(e).__name__}): {e}"
                        ) from e
                    after = _sent()
                    if after.limited > before.limited:
                        raise RateLimited(
                            f"Alpaca answered 429 Too Many Requests during the {what} request"
                        )
                    trading = after.trading - before.trading
                    if trading and not pace.trading_per_minute:
                        raise HistoryStopped(
                            f"the {what} request sent {trading} trading API request(s); this load sends "
                            "none (ADR 0093)"
                        )
                    gets = after.data - before.data
                    next_at = began + 60 * max(
                        gets / pace.data_per_minute,
                        trading / pace.trading_per_minute if trading else 0,
                    )
                    if not isinstance(result, BarSet):
                        raise HistoryStopped(
                            "alpaca-py returned raw bar data; the client must use raw_data=False"
                        )
                    rows = sum(len(bars) for bars in result.data.values())
                    if rows > max_rows:
                        raise HistoryStopped(
                            f"the {what} request returned {rows} bars, above the {max_rows} row bound; "
                            "none of this response was written"
                        )
                    count = 0
                    for symbol in batch:
                        bar_type = bar_types[symbol]
                        instrument = instruments[str(bar_type.instrument_id)]
                        bars = []
                        rounded = 0
                        first_rounding = ""
                        try:
                            for bar in result.data.get(symbol, []):
                                parsed = parse_bar(bar, bar_type, instrument)
                                if not lo <= parsed.ts_init <= hi:
                                    continue
                                bars.append(parsed)
                                if fields := bar_roundings(bar, parsed):
                                    rounded += 1
                                    if not first_rounding:
                                        first_rounding = f"{bar.timestamp} {', '.join(fields)}"
                        except Exception as e:
                            raise HistoryStopped(
                                f"parsing {bar_type} on feed {FEED.value} failed "
                                f"({type(e).__name__}): {e}"
                            ) from e
                        if not bars:
                            continue  # an empty answer writes nothing and is asked for again
                        if rounded:
                            out(
                                f"paper:history: Rounded {rounded} of {len(bars)} Alpaca bars for "
                                f"{symbol} on feed {FEED.value} to price precision "
                                f"{instrument.price_precision}, half to even as NautilusTrader's "
                                f"Instrument.make_price rounds; the first: {first_rounding}"
                            )
                        claimed = _claim_feed(path)
                        try:
                            catalog.write_bars(bars, start=lo, end=hi)
                        except Exception as e:
                            if (
                                claimed
                                and not _holds_data(path)
                                and catalog_feed(path) == FEED.value
                            ):
                                (path / FEED_FILE).unlink(missing_ok=True)
                            raise HistoryStopped(
                                f"writing {bar_type} into {path} failed ({type(e).__name__}): {e}"
                            ) from e
                        count += len(bars)
                    written += count
                    out(f"paper:history: {what}: {count} bars, {gets} GET(s)")
    return written


def _when(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text)
        # Keep naive values naive for the CLI's time-zone refusal; aware bounds go through the
        # pinned core converter from the original string, before Python discards its nanoseconds.
        return when if when.tzinfo is None else unix_nanos_to_dt(dt_to_unix_nanos(text))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO 8601 time") from e


def load_trades(
    client: Any,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    pace: Pace = PACE,
    max_rows: int = MAX_ROWS_PER_CALL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    out: Callable[[str], None] = print,
) -> int:
    """Load complete missing intervals of SIP prints through the adapter and the pinned trade-tick
    writer (contracts in this module's header), an overfull interval in parts.
    """
    try:
        for name, value in (("start", start), ("end", end)):
            if value.tzinfo is None:
                raise HistoryStopped(f"{name} must carry a time zone, got {value}")
        if start >= end:
            raise HistoryStopped(f"start {start} is not before end {end}")
        if not symbols or any(not symbol.strip() for symbol in symbols):
            raise HistoryStopped("at least one nonempty symbol is required")
        if end > now() - PUBLICATION_DELAY:
            raise HistoryStopped(
                f"end {end} must be at least {PUBLICATION_DELAY} before now for published SIP trades"
            )
        if max_rows < 1:
            raise HistoryStopped(f"the trade row bound must be positive, got {max_rows}")
        start_ns, end_ns = dt_to_unix_nanos(start), dt_to_unix_nanos(end)
        if start_ns < 0:
            raise HistoryStopped(f"start {start} precedes 1970-01-01 UTC, the catalog's epoch")
        path = Path(catalog_path).resolve()
        if (
            path == LEDGER.resolve()
            or LEDGER.resolve() in path.parents
            or "ledger" in Path(catalog_path).parts
        ):
            raise HistoryStopped(
                f"the catalog at {catalog_path} is under ledger/, which holds the trial ledger only"
            )
        if not path.is_dir():
            raise HistoryStopped(f"the catalog at {path} must be an existing directory")
        _check_feed(path)
        catalog = ParquetDataCatalog(str(path))
        ids = [f"{s}.{VENUE}" for s in dict.fromkeys(s.strip() for s in symbols)]
        instruments = {str(i.id): i for i in catalog.instruments(instrument_ids=ids)}
        if absent := [i for i in ids if i not in instruments]:
            raise HistoryStopped(
                f"no instrument definition in {path} for {', '.join(absent[:20])}: "
                "the asset list load writes them (ADR 0093); nothing was requested"
            )
        written = 0
        with _catalog_lock(path), _paced_trade_gets(client, pace, clock, sleep):
            _check_feed(path)
            for identifier in ids:
                instrument = instruments[identifier]
                missing = catalog.get_missing_intervals_for_request(
                    start_ns, end_ns, NautilusDataType.TradeTick, identifier
                )
                for lo, hi in missing:
                    part: int | None = lo
                    while part is not None:
                        loaded, part = _load_trade_part(
                            client, catalog, path, instrument, part, hi, pace, max_rows, out
                        )
                        written += loaded
        return written
    except HistoryStopped:
        raise
    except Exception as e:
        raise HistoryStopped(
            f"the trade history load of {', '.join(symbols[:20])} into {catalog_path} "
            f"failed ({type(e).__name__}): {e}"
        ) from e


def _load_trade_part(
    client: Any,
    catalog: ParquetDataCatalog,
    path: Path,
    instrument: Any,
    lo: int,
    hi: int,
    pace: Pace,
    max_rows: int,
    out: Callable[[str], None],
) -> tuple[int, int | None]:
    """Fetch and write one part of a missing interval, from `lo` to `hi`: the trades written, and
    where the next part starts, or `None` once the interval is done. An answer over `max_rows` valid
    prints is cut before its last timestamp, so no timestamp is split across parts, the coverage
    written ends one nanosecond before it, and the next part starts there."""
    symbol = instrument.id.symbol.value
    what = f"trades of {symbol}, feed {FEED.value}, interval [{lo}, {hi}]"
    before = _sent()
    try:
        history = fetch_trades(
            client,
            StockTradesRequest(symbol_or_symbols=symbol, feed=FEED, limit=max_rows + 1),
            start_ns=lo,
            end_ns=hi,
        )
    except HistoryStopped:
        raise
    except APIError as e:
        raise HistoryStopped(
            f"Alpaca refused the {what} request (HTTP {e.status_code}): {e}; "
            "nothing falls back to another feed"
        ) from e
    except Exception as e:
        raise HistoryStopped(f"the {what} request failed ({type(e).__name__}): {e}") from e
    after = _sent()
    if after.limited > before.limited:
        raise RateLimited(f"Alpaca answered 429 Too Many Requests during the {what} request")
    trading = after.trading - before.trading
    if trading and not pace.trading_per_minute:
        raise HistoryStopped(
            f"the {what} request sent {trading} trading API request(s); "
            "this load sends none (ADR 0093)"
        )
    gets = after.data - before.data
    # The pinned writer refuses ticks out of time order; the sort is stable, so prints at one
    # timestamp keep the answer's order.
    answered = sorted(history.trades, key=lambda row: row[1])
    covered_to, following = hi, None
    if len(answered) > max_rows:
        last = answered[-1][1]
        answered = [row for row in answered if row[1] < last]
        if not answered:
            raise HistoryStopped(
                f"the {what} request has more than {max_rows} trades at one timestamp, "
                f"{last}; none of this interval was written"
            )
        covered_to, following = last - 1, last
    if dropped := history.canceled + history.incorrect:
        out(
            f"paper:history: Dropped {dropped} invalid {what}: "
            f"canceled={history.canceled}, incorrect={history.incorrect} "
            "(historical u field)"
        )
    ticks = []
    rounded = 0
    first_rounding = ""
    try:
        for trade, ts_event in answered:
            tick = parse_trade(trade, instrument, ts_event, ts_event=ts_event)
            ticks.append(tick)
            if was_rounded(tick.price, trade.price):
                rounded += 1
                first_rounding = first_rounding or (f"{trade.timestamp} trade price {trade.price}")
    except Exception as e:
        raise HistoryStopped(f"parsing {what} failed ({type(e).__name__}): {e}") from e
    if not ticks:
        out(f"paper:history: {what}: 0 trades, {gets} GET(s)")
        return 0, None
    if rounded:
        out(
            f"paper:history: Rounded {rounded} of {len(ticks)} Alpaca trades "
            f"for {symbol} on feed {FEED.value} to price precision "
            f"{instrument.price_precision}, half to even as NautilusTrader's "
            f"Instrument.make_price rounds; the first: {first_rounding}"
        )
    claimed = _claim_feed(path)
    try:
        catalog.write_trade_ticks(ticks, start=lo, end=covered_to)
    except Exception as e:
        if claimed and not _holds_data(path) and catalog_feed(path) == FEED.value:
            (path / FEED_FILE).unlink(missing_ok=True)
        raise HistoryStopped(f"writing {what} into {path} failed ({type(e).__name__}): {e}") from e
    out(f"paper:history: {what}: {len(ticks)} trades to {covered_to}, {gets} GET(s)")
    return len(ticks), following


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m canon_trading.history_load", description=__doc__
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--symbols", help="symbols, comma-separated")
    which.add_argument("--symbols-file", type=Path, help="a file of symbols, one a line")
    parser.add_argument("--start", type=_when, required=True, help="ISO 8601 with a time zone")
    parser.add_argument("--end", type=_when, required=True, help="ISO 8601 with a time zone")
    parser.add_argument(
        "--bars", default="1-MINUTE,1-DAY", help="1-MINUTE, 1-DAY or both, comma-separated"
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="the catalog root")
    parser.add_argument("--data", choices=("bars", "trades"), default="bars")
    args = parser.parse_args(argv)
    for name in ("start", "end"):
        if getattr(args, name).tzinfo is None:
            parser.error(f"--{name} must carry a time zone")
    if args.start >= args.end:
        parser.error(f"--start {args.start} is not before --end {args.end}")
    if args.data == "trades" and dt_to_unix_nanos(args.start) < 0:
        parser.error("--start precedes 1970-01-01 UTC, the catalog's epoch")
    specs = [s.strip() for s in args.bars.split(",") if s.strip()]
    if args.data == "bars" and (not specs or any(s not in BAR_SPECS for s in specs)):
        parser.error(f"--bars takes {', '.join(BAR_SPECS)}, got {args.bars!r}")
    now = utc_now()
    if args.data == "trades" and args.end > now - PUBLICATION_DELAY:
        parser.error(
            f"--end must be at least {PUBLICATION_DELAY} before now for published SIP trades"
        )
    for spec in specs if args.data == "bars" else []:
        if args.end > now - margin(spec):
            parser.error(
                f"--end must be at least {margin(spec)} before now for {spec} bars (bars_to_catalog's rule); "
                "give --bars 1-MINUTE for a more recent end"
            )
    catalog = args.catalog.resolve()
    if (
        catalog == LEDGER.resolve()
        or LEDGER.resolve() in catalog.parents
        or "ledger" in args.catalog.parts
    ):
        parser.error(
            f"--catalog {args.catalog} is under ledger/, which holds the trial ledger only"
        )
    if args.symbols_file is not None:
        try:
            text = args.symbols_file.read_text(encoding="utf-8")
        except OSError as e:
            parser.error(f"--symbols-file: {e}")
        stripped = (line.strip() for line in text.splitlines())
        symbols = [line for line in stripped if line and not line.startswith("#")]
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        parser.error("no symbols given")
    symbols = list(dict.fromkeys(symbols))
    try:
        client = history_client()
        if args.data == "trades":
            written = load_trades(client, symbols, args.start, args.end, args.catalog, now=utc_now)
        else:
            written = load_history(
                client, symbols, specs, args.start, args.end, args.catalog, now=utc_now
            )
    except RateLimited as e:
        print(f"paper:history: {e}", file=sys.stderr, flush=True)
        return 4
    except HistoryStopped as e:
        print(f"paper:history: {e}", file=sys.stderr, flush=True)
        return 1
    print(f"paper:history: done, {written} {args.data} written to {args.catalog}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
