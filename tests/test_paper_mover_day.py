"""One recorded mover day loaded from Alpaca and replayed: data reads only (ADR 0088).

The replay itself (`replay`, `stage_one`) is `tests/test_mover_day.py`'s, loaded here by path since the
tests are not a package; this file loads one symbol-day from Alpaca's Market Data API into a catalog
folder of its own and replays it. It runs only when a paper key pair is in the environment, under the
names in `canon_alpaca.constants`, otherwise pytest reports it as skipped.
`mise run paper:test -- -s -k test_paper_mover_day` runs it alone under account A's lock (ADR 0029,
ADR 0040). Never write the keys to a file. It opens no stream: every read is a request.

**The paper test's data** (Alpaca's Market Data API; https://docs.alpaca.markets/us/reference/stockbars.md
and stockquotes-1.md): the day is 2026-11-11 and the names, in order, are the common stocks the movers
screener (`canon_trading.movers_screener`) gave as gainers that morning, warrants left out: NXXT, then
NCT, then SCKT. A name is passed over, with the reason printed, if it has fewer than `MIN_BARS` minute
bars from 04:00 to 11:30 New York, if its prior close is touched by a corporate action, or if its
quotes from 09:30 to 11:30 show a halt.

* Minute bars from 04:00 to 11:30 New York, SIP (`BARS_FEED`), through `canon_trading.bars_to_catalog.load_bars`, the
  adapter's write-back (ADR 0040).
* Quotes from 09:30 to 11:30 New York, IEX (the adapter's `QUOTES_FEEDS`), through the same write-back
  as ADR 0066 and its paper test (`packages/canon-alpaca/tests/test_paper_quotes_write_back.py`): a
  `LiveNode` with the catalog registered and an actor that calls `request_quotes` with
  `params={"update_catalog": True}`, in requests of `QUOTE_CHUNK`, each in a node of its own, as
  `load_bars` builds one a call. alpaca-py pages each request through to its end, 10,000 quotes a page
  (`RESTClient._get_marketdata`, `page_limit=10_000`, at `alpaca-py@c42add7c0a70`), so the bound is
  checked between requests on the adapter's own count of market data requests (`request_counts`): the
  load stops, by name, once `MAX_PAGES` pages are spent, and a request may overrun it by its own pages.
* The corporate-action case: the prior day's daily bar on SIP, unadjusted (`Adjustment.RAW`) and
  adjusted for every action (`Adjustment.ALL`), through alpaca-py's `StockHistoricalDataClient` with the
  adapter's timeout. If the two closes differ, an action falls between the prior close and now, so the
  unadjusted prior close does not measure the day's move, and the name is passed over. That the SIP
  daily close is the official close is UNVERIFIED.
* A halt reads as a stretch of at least `HALT_GAP` with no IEX quote inside 09:30 to 11:30; a halt
  shorter than that, or one IEX kept quoting through, is not seen (UNVERIFIED: Alpaca's pages do not
  say what IEX quotes in a halt; ADR 0083, gap 4).

Reads only: the process runs in data-only mode (`canon_alpaca.providers.DATA_ONLY_ENV`, ADR 0070), so
every REST client the adapter builds refuses any method but GET before it is sent. The trading API is
read only for the instrument provider's asset lookup (`TradingClient.get_asset`, a GET). No execution
client, no order, no stream; the account is left as it was found. The catalog is `CATALOG_ENV` if set,
else a folder of pytest's own temporary tree; never the repository's `catalog/`.

Registered rows of this file, UNCALIBRATED: `MIN_BARS`, `HALT_GAP`, `WIND_DOWN`, `MAX_PAGES`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.constants import VENUE
from canon_alpaca.data import NEW_YORK
from nautilus_trader.common import DataActor
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TraderId


def _replay_module() -> Any:
    """`tests/test_mover_day.py`, registered under a name of its own so its dataclasses resolve."""
    import importlib.util
    import sys

    name = "canon_mover_day_replay"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("test_mover_day.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- paper: one recorded mover day (data reads only) ---------------------------------------------------

paper = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

REPO_CATALOG = Path(__file__).parents[1] / "catalog"
CATALOG_ENV = "CANON_MOVER_DAY_CATALOG"
"""A catalog folder to load into and keep, outside the repository's `catalog/`; pytest's own otherwise."""
DAY = date(2026, 11, 11)
PRIOR_DAY = date(2026, 11, 10)
CANDIDATES = ("NXXT", "NCT", "SCKT")
"""The movers screener's common-stock gainers of the morning of `DAY`, in its order, warrants left out."""
BARS_FROM = datetime(2026, 11, 11, 4, 0, tzinfo=NEW_YORK)
QUOTES_FROM = datetime(2026, 11, 11, 9, 30, tzinfo=NEW_YORK)
UNTIL = datetime(2026, 11, 11, 11, 30, tzinfo=NEW_YORK)
WIND_DOWN = datetime(2026, 11, 11, 11, 25, tzinfo=NEW_YORK)
"""The replay's end for the strategy: upstream's market exit in the session, five minutes before the data
ends."""
QUOTE_CHUNK = timedelta(minutes=30)
"""One quote request: inside ADR 0066's hour, so the bound is checked four times in the window."""
MAX_PAGES = 300
PAGE = 10_000
BARS_FEED = "sip"
"""The feed of the replayed bars: the consolidated feed, which a paper node's bars are built from."""
MIN_BARS = 120
"""The fewest minute bars from 04:00 to 11:30 for a name to be replayed: a quarter of the minutes."""
HALT_GAP = timedelta(minutes=5)
"""A stretch with no IEX quote this long inside 09:30 to 11:30 reads as a halt: the LULD pause's length."""
LOAD_TIMEOUT_SECS = 600


class QuoteBoundHit(RuntimeError):
    """The quote load spent `MAX_PAGES` pages of `PAGE` quotes before the window was loaded."""


class _RequestQuotes(DataActor):
    """Request one instrument's quotes over one window with catalog write-back, then stop the node
    (the shape of `canon_trading.bars_to_catalog._RequestBars` and the adapter's write-back test)."""

    def __init__(self) -> None:
        super().__init__()  # `DataActor.__new__` takes only a config, so the request is set after
        self.instrument_id: InstrumentId | None = None
        self.window: tuple[datetime, datetime] | None = None
        self.request_id: str | None = None
        self.received: list[int] = []
        self.failure: str | None = None
        self.completed = False

    def on_start(self) -> None:
        assert self.instrument_id is not None and self.window is not None
        start, end = self.window
        self.request_id = self.request_quotes(
            self.instrument_id, start=start, end=end, params={"update_catalog": True}
        )

    def on_historical_quotes(self, quotes) -> None:
        self.received.extend(q.ts_init for q in quotes)

    def on_data_response(self, response) -> None:
        if str(response.correlation_id) != self.request_id:
            return
        if response.failure_reason or response.completed:
            self.failure, self.completed = response.failure_reason, bool(response.completed)
            self.shutdown_system("Quotes requested for the catalog")


def data_pages() -> int:
    """Market data requests this process has sent, by the adapter's count: one a page."""
    from canon_alpaca.providers import DATA_API
    from canon_alpaca.providers import request_counts

    return sum(c.total for c in request_counts() if c.api == DATA_API)


def load_quote_window(
    instrument_id: InstrumentId, start: datetime, end: datetime, root: Path
) -> int:
    """One quote request with write-back into `root`; the quotes delivered. Raises on a failed request
    and on a delivered quote the catalog did not write (as `load_bars` checks bars)."""
    from canon_alpaca.factories import AlpacaDataClientFactory
    from nautilus_trader.common import Environment
    from nautilus_trader.config import DataClientConfig
    from nautilus_trader.config import InstrumentProviderConfig
    from nautilus_trader.config import LiveNodeConfig
    from nautilus_trader.core.datetime import dt_to_unix_nanos
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import NautilusDataType
    from nautilus_trader.persistence import DataCatalogConfig
    from nautilus_trader.persistence import ParquetDataCatalog

    node = LiveNode.build(
        "CANON-MOVER-QUOTES",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("QUOTES-001"),
            timeout_connection_secs=20,
            timeout_disconnection_secs=15,
            delay_post_stop_secs=0,
            catalogs=[DataCatalogConfig(path=str(root))],
            data_clients={
                str(VENUE): DataClientConfig(
                    instrument_provider=InstrumentProviderConfig(load_ids=[str(instrument_id)])
                ),
            },
        ),
        data_factories={str(VENUE): AlpacaDataClientFactory},
    )
    actor = _RequestQuotes()
    actor.instrument_id, actor.window = instrument_id, (start, end)

    try:
        node.add_actor(actor)
        asyncio.run(run_node(node))
        failure, completed, received = actor.failure, actor.completed, actor.received
    finally:
        node.dispose()
        del node, actor
    if failure or not completed:
        raise RuntimeError(f"the {instrument_id} quotes {start} to {end}: failed {failure!r}")
    missing = ParquetDataCatalog(str(root)).get_missing_intervals_for_request(
        dt_to_unix_nanos(start),
        dt_to_unix_nanos(end),
        NautilusDataType.QuoteTick,
        str(instrument_id),
    )
    unwritten = [t for t in received if any(lo <= t <= hi for lo, hi in missing)]
    if unwritten:
        raise RuntimeError(
            f"{len(unwritten)} {instrument_id} quotes were delivered and not written"
        )
    return len(received)


async def run_node(node: Any) -> None:
    async with asyncio.timeout(LOAD_TIMEOUT_SECS):
        await node.run_async()


def load_quotes(instrument_id: InstrumentId, root: Path) -> tuple[int, int]:
    """Quotes from `QUOTES_FROM` to `UNTIL` into `root`, a `QUOTE_CHUNK` a request, stopping by name
    once `MAX_PAGES` pages are spent (`QuoteBoundHit`); the quotes delivered and the pages spent."""
    from canon_trading.bars_to_catalog import catalog_feed

    held = catalog_feed(root)
    if held not in (None, "iex"):
        raise ValueError(f"the catalog at {root} holds {held} data; quotes here are IEX only")
    first = data_pages()
    received = 0
    start = QUOTES_FROM
    while start < UNTIL:
        spent = data_pages() - first
        if spent >= MAX_PAGES:
            raise QuoteBoundHit(
                f"the {instrument_id} quote load spent {spent} pages of {PAGE} quotes, its bound of "
                f"{MAX_PAGES}, before {start:%H:%M} New York; the rest of the window is not loaded"
            )
        end = min(start + QUOTE_CHUNK, UNTIL)
        received += load_quote_window(instrument_id, start, end, root)
        start = end
    spent = data_pages() - first
    if spent > MAX_PAGES:
        raise QuoteBoundHit(
            f"the {instrument_id} quote load spent {spent} pages of {PAGE} quotes, over its bound of "
            f"{MAX_PAGES}"
        )
    return received, spent


def prior_closes(symbol: str) -> tuple[Decimal | None, Decimal | None]:
    """The prior day's SIP daily close, unadjusted and adjusted for every corporate action."""
    from alpaca.data.enums import Adjustment
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.models import BarSet
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from canon_alpaca.providers import credentials_from_env
    from canon_alpaca.providers import require_get_only
    from canon_alpaca.providers import with_http_timeout

    client = with_http_timeout(StockHistoricalDataClient(*credentials_from_env()))
    require_get_only(client)
    closes: list[Decimal | None] = []
    for adjustment in (Adjustment.RAW, Adjustment.ALL):
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            # from days before, so a bar stamped a little before midnight New York is not missed; the
            # one kept is the bar whose open falls on the prior day in New York
            start=datetime.combine(PRIOR_DAY - timedelta(days=4), datetime.min.time(), NEW_YORK),
            end=datetime.combine(DAY, datetime.min.time(), NEW_YORK) - timedelta(seconds=1),
            adjustment=adjustment,
            feed=DataFeed.SIP,
        )
        answer = client.get_stock_bars(request)
        if not isinstance(answer, BarSet):
            raise TypeError(f"get_stock_bars answered {type(answer).__name__}, not a BarSet")
        bars = [
            bar
            for bar in answer.data.get(symbol) or []
            if bar.timestamp.astimezone(NEW_YORK).date() == PRIOR_DAY
        ]
        closes.append(Decimal(str(bars[-1].close)) if bars else None)
    return closes[0], closes[1]


def quote_gaps(quotes: list[QuoteTick]) -> list[tuple[str, str]]:
    """Each stretch inside `QUOTES_FROM` to `UNTIL` of at least `HALT_GAP` with no quote."""
    from nautilus_trader.core.datetime import dt_to_unix_nanos

    edges = [dt_to_unix_nanos(QUOTES_FROM), *(q.ts_init for q in quotes), dt_to_unix_nanos(UNTIL)]
    limit = HALT_GAP.total_seconds() * 1_000_000_000
    stamp = _replay_module().stamp
    return [(stamp(a), stamp(b)) for a, b in pairwise(edges) if b - a >= limit]


def load_day(symbol: str, root: Path) -> tuple[Any, list, Decimal] | str:
    """The name's instrument, bars and quotes in the catalog, and its unadjusted prior close; or why the
    name is passed over."""
    from nautilus_trader.core.datetime import dt_to_unix_nanos
    from nautilus_trader.persistence import ParquetDataCatalog

    from canon_trading.bars_to_catalog import load_bars

    instrument_id = InstrumentId.from_str(f"{symbol}.{VENUE}")
    bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
    # Two roots, since a catalog root holds one feed (`bars_to_catalog`): the bars on SIP, the feed a
    # paper node builds its bars from, and the quotes on IEX, the one feed the adapter's quote request
    # serves (ADR 0066). On IEX alone a mover has too few bars: NXXT 43, NCT 61 and SCKT 99 from 04:00
    # to 11:30 on 2026-11-11, under `MIN_BARS` each (run 2026-11-11).
    bars_root, root = root / "bars-sip", root / "quotes-iex"
    root.mkdir(parents=True, exist_ok=True)  # a catalog opens only a folder that is there
    loaded = load_bars(
        bar_type, BARS_FROM, UNTIL, bars_root, timeout_secs=LOAD_TIMEOUT_SECS, feed=BARS_FEED
    )
    bars = ParquetDataCatalog(str(bars_root)).query_bars(
        [str(bar_type)], start=dt_to_unix_nanos(BARS_FROM), end=dt_to_unix_nanos(UNTIL)
    )
    catalog = ParquetDataCatalog(str(root))
    print(f"{symbol}: {len(bars)} SIP minute bars from 04:00 to 11:30; catalog {loaded}")
    if len(bars) < MIN_BARS:
        return f"{len(bars)} minute bars from 04:00 to 11:30, fewer than {MIN_BARS}"
    raw, adjusted = prior_closes(symbol)
    print(f"{symbol}: {PRIOR_DAY} close, SIP daily: unadjusted {raw}, adjusted for all {adjusted}")
    if raw is None or adjusted is None:
        return f"no SIP daily bar on {PRIOR_DAY}, so no prior close"
    if raw != adjusted:
        return (
            f"a corporate action: the {PRIOR_DAY} close is {raw} unadjusted and {adjusted} adjusted"
        )
    received, pages = load_quotes(instrument_id, root)
    quotes = catalog.query_quote_ticks(
        [str(instrument_id)], start=dt_to_unix_nanos(QUOTES_FROM), end=dt_to_unix_nanos(UNTIL)
    )
    print(
        f"{symbol}: {received} IEX quotes from 09:30 to 11:30 in {pages} pages; {len(quotes)} held"
    )
    gaps = quote_gaps(quotes)
    if gaps:
        return f"no quote for {HALT_GAP} or more, read as a halt: {gaps}"
    instruments = ParquetDataCatalog(str(bars_root)).instruments([str(instrument_id)])
    if not instruments:
        return "the catalog holds no instrument definition"
    return instruments[-1], [*bars, *quotes], raw


@paper
def test_paper_mover_day(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    from canon_alpaca.providers import DATA_ONLY_ENV
    from canon_alpaca.providers import request_counts

    monkeypatch.setenv(DATA_ONLY_ENV, "1")  # every adapter client refuses all but GET (ADR 0070)
    chosen = os.environ.get(CATALOG_ENV)
    root = Path(chosen) if chosen else tmp_path_factory.mktemp("mover-day")
    root = root.resolve()
    assert not root.is_relative_to(REPO_CATALOG.resolve()), f"{root} is the repository's catalog"
    print(f"\ncatalog: {root}")
    day = None
    for symbol in CANDIDATES:
        loaded = load_day(symbol, root)
        if isinstance(loaded, str):
            print(f"{symbol} passed over: {loaded}")
            continue
        day = symbol, *loaded
        break
    print(f"requests: {request_counts()}")
    assert day is not None, f"none of {CANDIDATES} could be replayed; the reasons are above"
    symbol, instrument, data, prior_close = day
    replays = _replay_module()
    result = replays.replay(instrument, data, prior_close, wind_down_at=WIND_DOWN)
    print(f"{symbol} on {DAY} from a prior close of {prior_close}:\n{result.report()}")
    if not any(o.side == "BUY" for o in result.orders):
        print(
            f"{symbol}: no entry on {DAY}; that is the day's answer, not a failure of the harness"
        )
    assert replays.stage_one(result) == []
