"""Alpaca's historical bars into the local `ParquetDataCatalog`, through the adapter's data client.

Contract: catalog write-back of historical requests, NautilusTrader v2.0.0rc8 (ADR 0040). Read
2026-11-10:

* https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md ("Reading data": with catalogs
  registered through `LiveNodeConfig.catalogs`, a historical request reads the intervals the catalog
  holds and requests only the missing ones from the data client; `params={"update_catalog": True}`
  writes successful responses back; write-back needs a client that reports completion, "such as ... a
  Python adapter", and leaves out a request with a record limit, which this module never sends. "Writing data": instrument definitions
  have their own writer, `write_instruments`. "File naming": files are named for their `ts_init` range
  under `data/bars/<bar type>/`. "Direct catalog access": `LiveNodeConfig.catalogs` does not preload).
* https://nautilustrader.io/docs/md/latest/concepts/actors.md (a `DataActor` requests bars in
  `on_start`; `on_data_response` runs after the typed hook for each envelope, and `failure_reason` is
  checked at completion).
* The same guide, "File naming": instruments go in "one directory per concrete instrument class, such
  as ... `data/equity/`". The installed `ParquetDataCatalog.write_instruments` docstring names
  `data/instruments/{instrument_id}/` instead; the catalog wrote `data/equity/AAPL.ALPACA/` (observed at
  v2.0.0rc8), so the guide is followed here. The docstring's "allowing multiple historical versions of
  the same instrument to be written across separate calls" holds: each call adds a file named for the
  definition's `ts_init`. `get_intervals` and `query_files` are as their docstrings give them.

How a load runs: a `LiveNode` with the `ALPACA` data client and the catalog registered, and one actor
that requests the bar type over `[start, end]` with `update_catalog` and stops the node when the
response completes. The adapter returns exactly the bars whose `ts_init`, the close of their interval,
lies in the window (`canon_alpaca.data`, "Bar windows"), and the catalog records the window's bounds as
covered, including any stretch inside them with no bar. So a second load over a covered range makes
no call to Alpaca, and one over an overlapping range asks only for the part not covered and writes it to a file of its own (observed at
v2.0.0rc8 against the adapter's stand-in venue, 2026-11-10; `tests/test_bars_to_catalog.py`). A window
with no bar at all is the exception: the Parquet catalog writes no file for an empty response, so it is
not recorded and a later load asks for it again (the guide extends empty coverage only "when ... the
backend supports it"; observed the same way).

The instrument definition is written only when the catalog holds none for the instrument. The adapter
stamps a definition with the time it was loaded (`canon_alpaca.providers`), so writing it on every load
would add a version each time. The definition kept is the first one loaded, and it is dated after the
bars it describes. `canon_trading.backtest` gives the node no time bounds and the node loads it (observed
at v2.0.0rc8); a `BacktestDataConfig` with bounds might not find it (UNVERIFIED). It is written after the
bars, so a load that dies in between leaves bars without a definition until the next load writes it.

What this refuses, and why:

* A window ending less than one bar interval, or less than 15 minutes, before now. The catalog records
  the whole window as covered and no later load fetches it again, so a bar not yet formed, or formed and
  not yet published, would be missing for good. The 15 minutes are the delay Alpaca's bars reference
  gives for a feed without real-time access ("otherwise 15 minutes before the current time"); whether a
  bar can appear later still on the IEX feed is UNVERIFIED.
* A window that is empty or inverted, and a naive `datetime` (a time zone is needed to place it).
* A load whose bars did not all reach the catalog. At v2.0.0rc8 a failed write-back is logged and the
  request still succeeds; the guide's "Catalog write failures ... fail the request" describes a later
  change (upstream pull request 5494, merged after the rc8 tag). So after the node stops, any delivered
  bar inside a part the catalog still reports missing (`get_missing_intervals_for_request`) fails the
  load.
* A request that fails, or a node that does not finish in `timeout_secs`. The failure is raised. What
  was already written stays: bars of an earlier load, and any part of this request the node wrote back
  before the failure (the adapter answers a request in one response, but the node may split a request
  over the catalog's gaps). `timeout_secs` stops the node, but `asyncio.run` then waits for the
  adapter's worker thread, and alpaca-py sets no HTTP timeout (`canon_alpaca.data`, "Known limits"), so
  a call that never returns still holds the load until the connection ends.

Feeds (ADR 0055, ADR 0077): `feed` names the bars request's feed, passed through as
`params={"feed": ...}`, the adapter's own parameter (`canon_alpaca.constants.FEED_PARAM`), one of
`BARS_FEEDS`; with none, no feed is named and the adapter's default, IEX, is used, as before. The
catalog keys bars by bar type, not feed (`data/bars/<bar type>/`), so two feeds' bars of one bar type
in one root would read as one series, and a window one feed covered would never be fetched for the
other. So each catalog root holds one feed: the file `alpaca-feed` in the root names it, and a load
naming another feed is refused, by name, before any node is built. No upstream records a feed per
catalog; the file is this module's. It is written before the request, so a load cut short still
leaves its feed named, and removed again if the load fails and the root holds no data file. A root
with data and no such file was filled before the file existed, by this module, which could then load
IEX only, or by the quote write-back, IEX only (ADR 0066), so it is read as IEX. Limits: the file is
read and written without a lock, so two loads into one root at once are not kept apart (paper loads run
one at a time under the paper lock); a root whose file is deleted by hand reads as IEX again; and only
this module reads the file, so a quote load into a root checks `catalog_feed` itself.

Known limits, set by the adapter and not here: the feed `BARS_FEEDS` allows, Alpaca's default `raw` adjustment
(alpaca-py sends no `adjustment`, and https://docs.alpaca.markets/us/reference/stockbars.md gives
"Default: `raw`"), and MINUTE, HOUR, DAY or WEEK bars of `LAST` price from an `EXTERNAL` source.
Bars inside a covered window are never fetched again, so a bar Alpaca later revises is not picked up
(UNVERIFIED whether Alpaca revises a closed bar).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from alpaca.data.enums import DataFeed
from canon_alpaca.constants import BARS_FEEDS
from canon_alpaca.constants import FEED
from canon_alpaca.constants import FEED_PARAM
from canon_alpaca.constants import VENUE
from canon_alpaca.data import bar_interval_ns
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.common import DataActor
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import TraderId
from nautilus_trader.persistence import DataCatalogConfig
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from nautilus_trader.common import LoggerConfig
    from nautilus_trader.model import BarType

PUBLICATION_DELAY = timedelta(minutes=15)
"""The least time between a window's end and now (this module's header)."""

DEFAULT_CATALOG = Path("catalog")
"""Upstream's catalog guide builds the path from the working directory; from the repo root this is it."""

_FEEDS = tuple(f.value for f in BARS_FEEDS)

FEED_FILE = "alpaca-feed"
"""The file in a catalog root that names the one Alpaca feed its data came from (this module's header)."""


def _holds_data(path: Path) -> bool:
    data = path / "data"
    return data.is_dir() and any(p.is_file() for p in data.rglob("*"))


def catalog_feed(catalog_path: Path) -> str | None:
    """The feed the catalog root at `catalog_path` holds, or `None` for a root that holds no data yet.

    The name in its `FEED_FILE`; with no such file, `FEED` (IEX) if the root holds data, since every
    load made before the file existed was IEX (this module's header).
    """
    path = Path(catalog_path).resolve()
    marker = path / FEED_FILE
    if not marker.is_file():
        return FEED.value if _holds_data(path) else None
    held = marker.read_text(encoding="ascii").strip()
    if held not in _FEEDS:
        raise ValueError(f"{marker} names {held!r}, not one of {', '.join(_FEEDS)}; it is damaged")
    return held


@dataclass(frozen=True)
class CatalogBars:
    """What the catalog holds for one bar type, as the catalog itself reports it (ADR 0040).

    `intervals` are the covered `ts_init` ranges, inclusive, in nanoseconds (`get_intervals`). `files` are
    the data files of the bar type (`query_files`), relative to the catalog root and named for their
    ranges. `count` is the number of bars stored, counted by reading them all,
    which grows with the catalog. A backtest through
    `canon_trading.backtest` reads every bar of its bar types, so this is what such a run reads.
    """

    bar_type: str
    intervals: list[tuple[int, int]]
    files: list[str]
    count: int


def catalog_bars(catalog_path: Path, bar_type: BarType) -> CatalogBars:
    """Report what the catalog at `catalog_path` holds for `bar_type`; it writes and fetches nothing."""
    catalog = ParquetDataCatalog(str(Path(catalog_path).resolve()))
    name = str(bar_type)
    return CatalogBars(
        bar_type=name,
        intervals=sorted(catalog.get_intervals(NautilusDataType.Bar, name)),
        files=sorted(catalog.query_files(NautilusDataType.Bar, [name])),
        count=len(catalog.query_bars([name])),
    )


class _RequestBars(DataActor):
    """Request one bar type over one window with catalog write-back, then stop the node."""

    def __init__(self) -> None:
        # `DataActor` is a native class whose `__new__` takes only a config, so the caller sets the
        # request after construction. `start` and `end` are the actor's own methods, hence `window`.
        super().__init__()
        self.bar_type: BarType | None = None
        self.window: tuple[datetime, datetime] | None = None
        self.params: dict[str, object] | None = None
        self.request_id: str | None = None
        self.failures: list[str | None] = []
        self.completed = False
        self.received: list[
            int
        ] = []  # `ts_init` of every bar delivered, from the catalog or Alpaca

    def on_start(self) -> None:
        if self.bar_type is None or self.window is None or self.params is None:
            raise RuntimeError("the bar type, window and params are set before the node runs")
        start, end = self.window
        self.request_id = self.request_bars(self.bar_type, start=start, end=end, params=self.params)

    def on_historical_bars(self, bars) -> None:
        self.received.extend(bar.ts_init for bar in bars)

    def on_data_response(self, response) -> None:
        if str(response.correlation_id) != self.request_id:
            return
        self.failures.append(response.failure_reason)
        if response.failure_reason or response.completed:
            self.completed = self.completed or bool(response.completed)
            self.shutdown_system("Bars requested for the catalog")


def load_bars(
    bar_type: BarType,
    start: datetime,
    end: datetime,
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    timeout_secs: float = 120,
    logging: LoggerConfig | None = None,
    feed: str | DataFeed | None = None,
) -> CatalogBars:
    """Load `bar_type`'s bars whose `ts_init` lies in `[start, end]` into the catalog, and report it.

    Idempotent: what the catalog already covers is not fetched again. The key pair comes from the
    environment, as the adapter reads it (`canon_alpaca.constants`). `logging` goes to the node; the
    logging subsystem is process-global (see `canon_trading.backtest.run_backtest`). `feed` names the
    request's feed, and a root holding another feed's data is refused (this module's header).
    """
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise ValueError(f"{name} must carry a time zone, got {value}")
    if start >= end:
        raise ValueError(f"start {start} is not before end {end}")
    instrument_id = bar_type.instrument_id
    if instrument_id.venue != VENUE:
        raise ValueError(f"{bar_type} is on venue {instrument_id.venue}, not {VENUE}")
    margin = max(timedelta(microseconds=bar_interval_ns(bar_type) // 1_000), PUBLICATION_DELAY)
    latest = datetime.now(UTC) - margin
    if end > latest:
        raise ValueError(
            f"end {end} is after {latest}, less than {margin} before now; the catalog would record a "
            "bar not yet formed or published as covered"
        )

    if feed is not None:
        # A `DataFeed` member is a `str` whose f-string is its name, so it is read as its value.
        feed = feed.value if isinstance(feed, DataFeed) else feed
        if feed not in _FEEDS:
            raise ValueError(f"feed {feed!r} is not one a bars request takes: {', '.join(_FEEDS)}")
    named = FEED.value if feed is None else feed
    path = Path(catalog_path).resolve()
    held = catalog_feed(path)
    if held is not None and held != named:
        raise ValueError(
            f"the catalog at {path} holds {held} data and this load is {named}: the catalog keys bars "
            "by bar type, not feed, so the two would read as one series; give each feed its own root"
        )
    path.mkdir(parents=True, exist_ok=True)
    marker = path / FEED_FILE
    claimed = not marker.is_file()
    if claimed:
        marker.write_text(f"{named}\n", encoding="ascii")
    try:
        return _load(bar_type, start, end, path, timeout_secs, logging, feed)
    except BaseException:
        if claimed:
            try:
                if not _holds_data(path):
                    marker.unlink(missing_ok=True)
            except OSError:
                pass  # the file stays, the strict side, and the load's own error is raised
        raise


def _load(
    bar_type: BarType,
    start: datetime,
    end: datetime,
    path: Path,
    timeout_secs: float,
    logging: LoggerConfig | None,
    feed: str | None,
) -> CatalogBars:
    instrument_id = bar_type.instrument_id
    node = LiveNode.build(
        "CANON-CATALOG",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("CATALOG-001"),
            logging=logging,
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            catalogs=[DataCatalogConfig(path=str(path))],
            data_clients={
                str(VENUE): DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(instrument_id)]),
                ),
            },
        ),
        data_factories={str(VENUE): AlpacaDataClientFactory},
    )
    actor = _RequestBars()
    actor.bar_type, actor.window = bar_type, (start, end)
    actor.params = {"update_catalog": True} | ({} if feed is None else {FEED_PARAM: feed})
    try:
        node.add_actor(actor)
        # A timeout is raised below, once the node is gone: a traceback holding its frame would keep
        # the node alive, and NautilusTrader builds no second `LiveNode` on a thread while one exists.
        timed_out = not asyncio.run(_run(node, timeout_secs))
        completed = actor.completed
        failures = [reason for reason in actor.failures if reason]
        received = actor.received
        instrument = node.cache.instrument(instrument_id)
    finally:
        node.dispose()
        del node, actor
    if timed_out:
        # Stopping the node cancels the request, so a failure reported now may be that cancel.
        after = f"; reported: {'; '.join(failures)}" if failures else ""
        raise TimeoutError(f"the {bar_type} request did not complete in {timeout_secs} s{after}")
    if failures:
        raise RuntimeError(f"the {bar_type} request failed: {'; '.join(failures)}")
    if not completed:
        raise RuntimeError(f"the node stopped before the {bar_type} request completed")
    if instrument is None:
        raise LookupError(f"{instrument_id} was not loaded")

    catalog = ParquetDataCatalog(str(path))
    missing = catalog.get_missing_intervals_for_request(
        dt_to_unix_nanos(start), dt_to_unix_nanos(end), NautilusDataType.Bar, str(bar_type)
    )
    unwritten = [t for t in received if any(lo <= t <= hi for lo, hi in missing)]
    if unwritten:
        # A delivered bar in a part the catalog does not cover came from Alpaca and was not written.
        raise RuntimeError(
            f"{len(unwritten)} {bar_type} bars were delivered but not written to the catalog; the "
            "node's log says why"
        )
    if not catalog.instruments([str(instrument_id)]):
        catalog.write_instruments([instrument])
    return catalog_bars(path, bar_type)


async def _run(node: LiveNode, timeout_secs: float) -> bool:
    """Run the node until it stops; False if this deadline, and nothing inside the node, ended it."""
    deadline = asyncio.timeout(timeout_secs)
    try:
        async with deadline:
            await node.run_async()
    except TimeoutError:
        if deadline.expired():
            return False
        raise
    return True
