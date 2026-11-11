"""Against Alpaca's paper environment: SPY's IEX quotes into a temporary catalog by write-back (ADR 0066).

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped. `mise run paper:test -- -s -k quotes_write_back` runs it alone
under the paper lock. Never write the keys to a file.

ADR 0066's first condition: a few minutes of SPY quotes on IEX, one request, written back into a catalog
in a temporary folder, never the repository's `catalog/`, settling the three points that record marks
UNVERIFIED. The route is the one ADR 0040 takes for bars and the catalog guide documents
(https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md, "Reading data"): a `LiveNode` with the
catalog in `LiveNodeConfig.catalogs` and the adapter's data client, and an actor that calls
`request_quotes` with `params={"update_catalog": True}` (`concepts/actors.md`). Each point is also an
offline test in `test_data_client.py`.

1. Write-back works for quotes: three minutes of the regular session of 2026-11-09, 10:00 to 10:03 New
   York, are written, the window is recorded as covered, and a second request over it is answered from
   the catalog with no new file.
2. An empty response: one minute of a Sunday, 2026-11-08 at noon New York, when IEX has no session,
   answers no quotes, writes no file and records no coverage.
3. A request killed in mid-write: the same three minutes are requested in a child process, which is sent
   SIGKILL as soon as its catalog logs "Writing", the line it logs as it starts writing a file (the
   child waits once its request is done, so the kill never finds it gone). What the child left is
   reported and checked: either no data file and no coverage, or the complete file; any
   staging file (`<name>#<n>`) is listed. A request in this process then fills the window, and its
   quotes are the ones the first point stored.

What it calls: the asset lookup goes to Alpaca's paper Trading API through `TradingClient(paper=True)`
and the quotes to Alpaca's Market Data API through `get_stock_quotes`, both through the adapter. It
places, modifies and cancels nothing and opens no position, so it leaves the account as it found it.
The quotes are historical, so the result does not depend on market hours.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.factories import AlpacaDataClientFactory
from nautilus_trader.common import DataActor
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.live import LiveNode
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import TraderId
from nautilus_trader.persistence import DataCatalogConfig
from nautilus_trader.persistence import ParquetDataCatalog

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

SPY = InstrumentId.from_str("SPY.ALPACA")
NEW_YORK = ZoneInfo("America/New_York")
START = datetime(2026, 11, 9, 10, 0, tzinfo=NEW_YORK)
END = datetime(2026, 11, 9, 10, 3, tzinfo=NEW_YORK)
EMPTY_START = datetime(2026, 11, 8, 12, 0, tzinfo=NEW_YORK)  # a Sunday
EMPTY_END = datetime(2026, 11, 8, 12, 1, tzinfo=NEW_YORK)
WRITING = "catalog::write: Writing"
"""The start of the line the catalog logs as it writes a file (observed at v2.0.0rc8)."""
CHILD_TIMEOUT_SECS = 300
REQUEST_TIMEOUT_SECS = 240


class _RequestQuotes(DataActor):
    """Request SPY's quotes over one window with catalog write-back, then stop the node."""

    def __init__(self) -> None:
        super().__init__()  # `DataActor.__new__` takes only a config, so the window is set after
        self.window: tuple[datetime, datetime] | None = None
        self.request_id: str | None = None
        self.received = 0
        self.failure: str | None = None
        self.completed = False

    def on_start(self) -> None:
        assert self.window is not None
        start, end = self.window
        self.request_id = self.request_quotes(
            SPY, start=start, end=end, params={"update_catalog": True}
        )

    def on_historical_quotes(self, quotes) -> None:
        self.received += len(quotes)

    def on_data_response(self, response) -> None:
        if str(response.correlation_id) != self.request_id:
            return
        if response.failure_reason or response.completed:
            self.failure, self.completed = response.failure_reason, bool(response.completed)
            self.shutdown_system("Quotes requested for the catalog")


def write_back(catalog: Path, start: datetime, end: datetime) -> tuple[int, str | None, bool]:
    """Request SPY's quotes over `[start, end]` with write-back into `catalog`; return how many arrived,
    the failure reason and whether the request completed."""
    node = LiveNode.build(
        "CANON-QUOTES",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("QUOTES-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            catalogs=[DataCatalogConfig(path=str(catalog))],
            data_clients={
                "ALPACA": DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(SPY)])
                ),
            },
        ),
        data_factories={"ALPACA": AlpacaDataClientFactory},
    )
    actor = _RequestQuotes()
    actor.window = (start, end)

    async def main() -> None:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECS):
            await node.run_async()

    try:
        node.add_actor(actor)
        asyncio.run(main())
        return actor.received, actor.failure, actor.completed
    finally:
        node.dispose()


def covered(catalog: Path) -> list[tuple[int, int]]:
    return sorted(
        ParquetDataCatalog(str(catalog)).get_intervals(NautilusDataType.QuoteTick, str(SPY))
    )


def files(catalog: Path) -> list[str]:
    return sorted(str(p.relative_to(catalog)) for p in catalog.rglob("*") if p.is_file())


def stored(catalog: Path) -> list:
    return ParquetDataCatalog(str(catalog)).query_quote_ticks([str(SPY)])


def kill_while_writing(catalog: Path) -> tuple[int, list[str]]:
    """Run `write_back` over the window in a child process and SIGKILL it on the catalog's "Writing"
    line; return its exit status and every line it printed (its node log)."""
    child = subprocess.Popen(
        [sys.executable, __file__, str(catalog), START.isoformat(), END.isoformat()],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    guard = threading.Timer(CHILD_TIMEOUT_SECS, child.kill)
    guard.start()
    lines: list[str] = []
    try:
        assert child.stdout is not None
        for line in child.stdout:
            lines.append(line)
            if WRITING in line:
                os.kill(child.pid, signal.SIGKILL)
                break
        child.stdout.close()
        return child.wait(), lines
    finally:
        guard.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.fixture(scope="module")
def loaded(tmp_path_factory) -> tuple[Path, list]:
    """Point 1: the window written back once, into a temporary catalog."""
    catalog = tmp_path_factory.mktemp("quotes")
    received, failure, completed = write_back(catalog, START, END)
    assert failure is None and completed, failure
    quotes = stored(catalog)
    assert len(quotes) == received
    return catalog, quotes


def test_paper_quotes_write_back_writes_the_window_and_a_covered_request_writes_nothing(
    loaded,
) -> None:
    catalog, quotes = loaded
    start_ns, end_ns = dt_to_unix_nanos(START), dt_to_unix_nanos(END)
    written = files(catalog)
    print(f"\nwrote {len(quotes)} SPY IEX quotes for {START} to {END}: {written}")
    assert quotes, "Alpaca served no IEX quote for SPY in three minutes of a regular session"
    assert covered(catalog) == [(start_ns, end_ns)]
    assert len([f for f in written if f.startswith("data/quotes/SPY.ALPACA/")]) == 1, written
    assert all(start_ns <= q.ts_event <= end_ns and q.ts_init == q.ts_event for q in quotes)
    assert [q.ts_init for q in quotes] == sorted(q.ts_init for q in quotes)
    sizes = [int(s) for q in quotes for s in (q.bid_size, q.ask_size)]
    lots = sum(s % 100 == 0 for s in sizes)
    print(f"sizes: {lots} of {len(sizes)} a multiple of 100, largest {max(sizes)}")

    received, failure, completed = write_back(catalog, START, END)
    assert failure is None and completed, failure
    assert received == len(quotes)
    assert files(catalog) == written
    assert covered(catalog) == [(start_ns, end_ns)]


def test_paper_quotes_write_back_of_an_empty_window_records_nothing(tmp_path: Path) -> None:
    received, failure, completed = write_back(tmp_path, EMPTY_START, EMPTY_END)
    print(
        f"\nempty window {EMPTY_START} to {EMPTY_END}: {received} quotes, files {files(tmp_path)}"
    )
    assert failure is None and completed, failure
    assert received == 0
    assert covered(tmp_path) == []
    assert files(tmp_path) == []


def test_paper_quotes_write_back_killed_mid_write_leaves_no_partial_file(
    loaded, tmp_path: Path
) -> None:
    _, quotes = loaded
    status, lines = kill_while_writing(tmp_path)
    left, intervals = files(tmp_path), covered(tmp_path)
    data = [f for f in left if f.endswith(".parquet")]
    staging = [f for f in left if "#" in f]
    print(
        f"\nkilled: exit {status}; data files {data}; staging files {staging}; coverage {intervals}"
    )
    assert any(WRITING in line for line in lines), "the child never wrote: " + "".join(lines[-20:])
    assert status == -signal.SIGKILL, "".join(lines[-20:])
    if data:
        assert intervals == [(dt_to_unix_nanos(START), dt_to_unix_nanos(END))]
        assert len(stored(tmp_path)) == len(quotes)
    else:
        assert intervals == []
        assert stored(tmp_path) == []

    received, failure, completed = write_back(tmp_path, START, END)
    assert failure is None and completed, failure
    assert received == len(quotes)
    assert covered(tmp_path) == [(dt_to_unix_nanos(START), dt_to_unix_nanos(END))]
    again = stored(tmp_path)
    assert [(q.ts_event, q.bid_price, q.ask_price) for q in again] == [
        (q.ts_event, q.bid_price, q.ask_price) for q in quotes
    ]


if __name__ == "__main__":  # the child of `kill_while_writing`
    # It waits once the request is done, so the kill always finds it alive however soon after its
    # "Writing" line the write ended; what it left is then the point, not when the kill landed.
    write_back(
        Path(sys.argv[1]), datetime.fromisoformat(sys.argv[2]), datetime.fromisoformat(sys.argv[3])
    )
    time.sleep(CHILD_TIMEOUT_SECS)
