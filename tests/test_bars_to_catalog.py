"""`canon_trading.bars_to_catalog` offline: a real `LiveNode` and catalog, on the adapter's stand-in venue.

The stand-ins are the ones the adapter's own tests use, from `packages/canon-alpaca/tests/conftest.py`,
loaded here as a module because pytest shares a conftest's fixtures only with tests under its folder.
They answer the way Alpaca's bars endpoint documents and hand the adapter alpaca-py's own models (that
file's docstring). No test here opens a network connection or reads a credential; the key pair in the
environment is a placeholder.

The bars are copies of the docs' example bar, one an hour from 09:00 UTC on 2022-01-03, so the bar
opening at 09:00 has `ts_init` 10:00 and a window `[09:00, 14:00]` holds the five bars opening 09:00 to
13:00.
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import canon_alpaca.data
import canon_alpaca.providers
import pytest
from alpaca.data.enums import DataFeed
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.backtest import run_backtest
from canon_trading.bars_to_catalog import FEED_FILE
from canon_trading.bars_to_catalog import catalog_bars
from canon_trading.bars_to_catalog import catalog_feed
from canon_trading.bars_to_catalog import load_bars
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig

STANDINS_PATH = Path(__file__).parents[1] / "packages" / "canon-alpaca" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("canon_alpaca_standins", STANDINS_PATH)
assert _spec is not None and _spec.loader is not None
standins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standins)
collect_on_main_thread = (
    standins.collect_on_main_thread
)  # the stand-ins' autouse fixture, used here too

HOURLY = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")
FIRST_OPEN = datetime(2022, 1, 3, 9, tzinfo=UTC)
HOUR = timedelta(hours=1)
BAR_COUNT = 8  # bars opening 09:00 to 16:00


def ns(t: datetime) -> int:
    return dt_to_unix_nanos(t)


@pytest.fixture
def venue(monkeypatch) -> SimpleNamespace:
    """Replace alpaca-py's client constructors with the stand-ins."""
    monkeypatch.setenv(ENV_API_KEY, "offline-placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "offline-placeholder")
    ns_ = SimpleNamespace(
        historical=standins.FakeHistoricalClient(standins.hourly_bars(FIRST_OPEN, BAR_COUNT)),
    )
    monkeypatch.setattr(
        canon_alpaca.providers, "TradingClient", lambda **_: standins.FakeTradingClient()
    )
    monkeypatch.setattr(canon_alpaca.data, "StockHistoricalDataClient", lambda **_: ns_.historical)
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: standins.FakeStream())
    return ns_


def files_under(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())


def test_a_load_writes_the_bars_and_the_instrument_and_the_backtest_runs_on_them(
    venue, tmp_path: Path
) -> None:
    start, end = FIRST_OPEN, FIRST_OPEN + 5 * HOUR
    loaded = load_bars(HOURLY, start, end, tmp_path)

    assert len(venue.historical.requests) == 1
    assert loaded.bar_type == str(HOURLY)
    assert loaded.count == 5
    assert loaded.intervals == [(ns(start), ns(end))]
    assert loaded.files == [
        f"data/bars/{HOURLY}/2022-01-03T09-00-00-000000000Z_2022-01-03T14-00-00-000000000Z.parquet",
    ]
    catalog = ParquetDataCatalog(str(tmp_path))
    bars = catalog.query_bars([str(HOURLY)])
    assert [b.ts_init for b in bars] == [ns(FIRST_OPEN + (i + 1) * HOUR) for i in range(5)]
    assert all(b.ts_init - b.ts_event == HOUR.total_seconds() * 1e9 for b in bars)
    [instrument] = catalog.instruments(["AAPL.ALPACA"])
    assert isinstance(instrument, Equity)

    strategy = MaCross(
        MaCrossConfig(
            bar_type=HOURLY, trade_size=Quantity.from_int(1), fast_period=2, slow_period=3
        )
    )
    run = run_backtest(tmp_path, [strategy], starting_balances=["100_000 USD"])
    assert run.result.iterations == 5


def test_a_second_load_of_a_covered_range_calls_alpaca_nothing_and_writes_nothing(
    venue, tmp_path: Path
) -> None:
    start, end = FIRST_OPEN, FIRST_OPEN + 5 * HOUR
    first = load_bars(HOURLY, start, end, tmp_path)
    files = files_under(tmp_path)

    again = load_bars(HOURLY, start + HOUR, end - HOUR, tmp_path)

    assert len(venue.historical.requests) == 1
    assert files_under(tmp_path) == files
    assert again == first


def test_an_overlapping_load_fetches_only_the_gap_and_duplicates_no_bar(
    venue, tmp_path: Path
) -> None:
    load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 3 * HOUR, tmp_path)
    loaded = load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 5 * HOUR, tmp_path)

    assert len(venue.historical.requests) == 2
    gap = venue.historical.requests[1]
    # The gap starts 1 ns after 12:00; the adapter sends it one interval earlier, and alpaca-py's
    # `datetime` drops the nanosecond. Alpaca filters on the open, so the bar opening at 11:00 comes
    # back and is dropped, its `ts_init` being outside the gap.
    assert (gap.start, gap.end) == (datetime(2022, 1, 3, 11), datetime(2022, 1, 3, 13))
    assert loaded.count == 5
    bars = ParquetDataCatalog(str(tmp_path)).query_bars([str(HOURLY)])
    assert [b.ts_init for b in bars] == [ns(FIRST_OPEN + (i + 1) * HOUR) for i in range(5)]
    assert len(loaded.files) == 2
    assert loaded.intervals == [
        (ns(FIRST_OPEN), ns(FIRST_OPEN + 3 * HOUR)),
        (ns(FIRST_OPEN + 3 * HOUR) + 1, ns(FIRST_OPEN + 5 * HOUR)),
    ]


def test_an_empty_window_is_not_recorded_and_is_asked_for_again(venue, tmp_path: Path) -> None:
    # The catalog guide: an empty response extends known-empty coverage "when ... the backend supports
    # it". The Parquet catalog at v2.0.0rc8 does not: it writes no file, so a second load asks again.
    start = FIRST_OPEN + timedelta(days=1)  # no bar opens on 2022-01-04 at the stand-in venue
    loaded = load_bars(HOURLY, start, start + 5 * HOUR, tmp_path)
    load_bars(HOURLY, start, start + 5 * HOUR, tmp_path)

    assert loaded.count == 0
    assert loaded.intervals == []
    assert not [f for f in files_under(tmp_path) if f.startswith("data/bars/")]
    assert len(venue.historical.requests) == 2


def test_the_instrument_is_written_once(venue, tmp_path: Path) -> None:
    load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 2 * HOUR, tmp_path)
    load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 4 * HOUR, tmp_path)

    assert len(ParquetDataCatalog(str(tmp_path)).instruments(["AAPL.ALPACA"])) == 1
    assert len([f for f in files_under(tmp_path) if f.startswith("data/equity/")]) == 1


def test_a_failed_request_raises_and_writes_nothing(venue, tmp_path: Path) -> None:
    venue.historical.error = standins.api_error(500, 50010000, "internal error")

    with pytest.raises(RuntimeError, match="request failed"):
        load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 5 * HOUR, tmp_path)

    assert files_under(tmp_path) == []
    assert catalog_bars(tmp_path, HOURLY).intervals == []


@pytest.mark.parametrize(
    ("bar_type", "start", "end", "message"),
    [
        (HOURLY, FIRST_OPEN.replace(tzinfo=None), FIRST_OPEN + HOUR, "time zone"),
        (HOURLY, FIRST_OPEN, FIRST_OPEN, "not before"),
        (
            HOURLY,
            FIRST_OPEN,
            datetime.now(UTC) - timedelta(minutes=30),
            "less than 1:00:00 before now",
        ),
        (
            BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL"),
            FIRST_OPEN,
            datetime.now(UTC) - timedelta(minutes=5),
            "less than 0:15:00 before now",
        ),
        (
            BarType.from_str("AAPL.XNAS-1-HOUR-LAST-EXTERNAL"),
            FIRST_OPEN,
            FIRST_OPEN + HOUR,
            "venue",
        ),
    ],
)
def test_refused_windows_build_no_node(venue, tmp_path, bar_type, start, end, message) -> None:
    catalog = tmp_path / "catalog"
    with pytest.raises(ValueError, match=message):
        load_bars(bar_type, start, end, catalog)

    assert not catalog.exists()
    assert venue.historical.requests == []


def test_a_request_that_never_answers_times_out_and_frees_the_thread_for_a_next_load(
    venue, tmp_path: Path
) -> None:
    # The stand-in waits on the gate inside `get_stock_bars`. The load waits for that worker thread
    # after its own timeout (the module's header), so the gate opens 2 s in, after the 1 s timeout.
    gate = threading.Event()
    venue.historical.gate = gate
    opener = threading.Timer(2, gate.set)
    opener.start()
    try:
        with pytest.raises(TimeoutError, match="did not complete in 1 s"):
            load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 5 * HOUR, tmp_path, timeout_secs=1)
    finally:
        opener.cancel()
        gate.set()
    venue.historical.gate = None
    assert catalog_bars(tmp_path, HOURLY).intervals == []

    assert load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 5 * HOUR, tmp_path).count == 5


def test_a_write_back_that_fails_fails_the_load(venue, tmp_path: Path) -> None:
    # At v2.0.0rc8 the node logs a failed catalog write and the request still succeeds. The write is
    # made to fail by a bars directory the process cannot write to.
    bars_dir = tmp_path / "data" / "bars"
    bars_dir.mkdir(parents=True)
    bars_dir.chmod(0o555)
    try:
        with pytest.raises(RuntimeError, match="5 .* bars were delivered but not written"):
            load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 5 * HOUR, tmp_path)
    finally:
        bars_dir.chmod(0o755)

    assert catalog_bars(tmp_path, HOURLY).intervals == []


def test_a_named_feed_reaches_alpaca_and_a_root_holds_one_feed(venue, tmp_path: Path) -> None:
    # The catalog keys bars by bar type, not feed: a second feed's load into a root is refused, by
    # name, before any node or request, so two feeds never read as one series (the module's header).
    sip_root, iex_root = tmp_path / "sip", tmp_path / "iex"
    window = (FIRST_OPEN, FIRST_OPEN + 2 * HOUR)

    assert load_bars(HOURLY, *window, sip_root, feed="sip").count == 2
    assert [r.feed.value for r in venue.historical.requests] == ["sip"]
    assert catalog_feed(sip_root) == "sip"
    with pytest.raises(ValueError, match="holds sip data and this load is iex"):
        load_bars(HOURLY, *window, sip_root)

    # A root filled before the feed file existed was IEX: it is read so, and refuses sip.
    load_bars(HOURLY, *window, iex_root)
    assert venue.historical.requests[-1].feed.value == "iex"
    (iex_root / FEED_FILE).unlink()
    assert catalog_feed(iex_root) == "iex"
    with pytest.raises(ValueError, match="holds iex data and this load is sip"):
        load_bars(HOURLY, *window, iex_root, feed="sip")
    with pytest.raises(ValueError, match="not one a bars request takes"):
        load_bars(HOURLY, *window, tmp_path / "other", feed="nasdaq")
    assert len(venue.historical.requests) == 2
    assert not (tmp_path / "other").exists()

    # A failed load into an empty root leaves no feed named, so the root can still take any feed.
    venue.historical.error = standins.api_error(500, 50010000, "internal error")
    with pytest.raises(RuntimeError, match="request failed"):
        load_bars(HOURLY, *window, tmp_path / "failed", feed="sip")
    assert catalog_feed(tmp_path / "failed") is None


def test_a_feed_given_as_alpaca_s_enum_is_named_by_its_value_and_a_damaged_feed_file_is_named(
    venue, tmp_path: Path
) -> None:
    load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 2 * HOUR, tmp_path, feed=DataFeed.SIP)
    assert (tmp_path / FEED_FILE).read_text(encoding="ascii") == "sip\n"
    assert load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 2 * HOUR, tmp_path, feed="sip").count == 2

    (tmp_path / FEED_FILE).write_text("", encoding="ascii")
    with pytest.raises(ValueError, match="alpaca-feed names '', not one of .*damaged"):
        load_bars(HOURLY, FIRST_OPEN, FIRST_OPEN + 2 * HOUR, tmp_path, feed="sip")
