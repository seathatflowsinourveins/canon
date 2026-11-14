"""Fixtures shared by the trading tests: Alpaca-shaped bars in a catalog, and eqs. 1-2 by hand.

pytest's good practices (https://docs.pytest.org/en/stable/explanation/goodpractices.html): under the
`importlib` import mode this repo uses (`pytest.toml`), test modules cannot import each other, and
helpers shared between them go in a `conftest.py` as fixtures.

`alpaca_bars` builds bars the way a paper run gets them: an Alpaca asset payload through
`canon_alpaca.providers.parse_equity`, and bar payloads, as alpaca-py's own `Bar` model parses them,
through `canon_alpaca.data.parse_bar`. The asset is the schema example in Alpaca's "Get Assets"
reference (https://docs.alpaca.markets/us/reference/get-v2-assets-1.md), the bar fields those of its
"Historical bars" response (https://docs.alpaca.markets/us/reference/stockbars.md). The prices are
invented. The default series is 40 daily bars on consecutive weekdays from Monday 2025-03-10, each
opening at midnight New York time; closes climb 101 to 115, fall to 100, then climb to 110, one dollar a
bar, and each bar opens at the previous close.

`write` records the range the bars cover the way a load by `canon_trading.bars_to_catalog` does, as
the window it asked for (`write_bars(start, end)`): from the UTC midnight of the first bar's open to
the UTC midnight after the last bar's `ts_init`, 2025-03-10 to 2025-05-04 for the default series.

`stated_n` reads N as `canon_trading.ranking.rank` states it, in its report or in the message it stops
with, for the tests of what a retirement does to N (ADR 0081).

`equations_one_and_two` works the DSR without skfolio, under the conventions ADR 0031 read in skfolio's
source: the sample standard deviation in both Sharpe ratios, population skewness, raw kurtosis and
sqrt(T - 1).
"""

from __future__ import annotations

import fcntl
import math
import os
import re
import signal
import statistics
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from statistics import NormalDist
from time import monotonic
from time import time_ns
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from alpaca.data.live.websocket import DataStream
from alpaca.data.models import Bar as AlpacaBar
from alpaca.trading.models import Asset
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from collections.abc import Sequence
    from pathlib import Path

    import optuna
    from nautilus_trader.model import Bar
    from nautilus_trader.model import Equity

NEW_YORK = ZoneInfo("America/New_York")
EULER_MASCHERONI = 0.5772156649015329


class AlpacaBars:
    """Alpaca-shaped instruments and daily bars, parsed by the adapter, written to a catalog."""

    CLOSES: tuple[float, ...] = (
        *(float(c) for c in range(101, 116)),
        *(float(c) for c in range(114, 99, -1)),
        *(float(c) for c in range(101, 111)),
    )
    ASSET: dict[str, object] = {  # noqa: RUF012, read only
        "borrow_status": "easy_to_borrow",
        "class": "us_equity",
        "easy_to_borrow": True,
        "exchange": "NASDAQ",
        "fractionable": True,
        "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "marginable": True,
        "name": "Apple Inc. Common Stock",
        "shortable": True,
        "status": "active",
        "symbol": "AAPL",
        "tradable": True,
    }

    def equity(self, symbol: str = "AAPL") -> Equity:
        return parse_equity(Asset.model_validate({**self.ASSET, "symbol": symbol}), 0)

    def daily(
        self,
        instrument: Equity,
        closes: Sequence[float] = CLOSES,
        *,
        first: datetime | None = None,
        previous: float = 100.0,
    ) -> list[Bar]:
        """`closes` as daily bars on consecutive weekdays from `first`, through `parse_bar`."""
        symbol = instrument.id.symbol.value
        bar_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        bars = []
        day = first or datetime(2025, 3, 10, tzinfo=NEW_YORK)
        for close in closes:
            while day.weekday() >= 5:  # Saturday, Sunday
                day += timedelta(days=1)
            payload = {
                "t": day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": previous,
                "h": max(previous, close),
                "l": min(previous, close),
                "c": close,
                "v": 1_000_000,
                "n": 1_000,
                "vw": close,
            }
            bars.append(parse_bar(AlpacaBar(symbol, payload), bar_type, instrument))
            previous = close
            day += timedelta(days=1)
        return bars

    def write(
        self,
        path: Path,
        closes: Sequence[float] = CLOSES,
        *,
        symbols: tuple[str, ...] = ("AAPL",),
        first: datetime | None = None,
        previous: float = 100.0,
        instrument: bool = True,
        bars: bool = True,
        covered_from: int | None = None,
    ) -> list[Bar]:
        """Write each symbol's instrument and bars to the catalog at `path`; return the first's bars.

        The range recorded as covered starts at `covered_from` if given (module docstring).
        """
        day = 86_400 * 1_000_000_000
        path.mkdir(exist_ok=True)  # the catalog does not create its own directory
        catalog = ParquetDataCatalog(str(path))
        written = []
        for symbol in symbols:
            listed = self.equity(symbol)
            series = self.daily(listed, closes, first=first, previous=previous)
            if instrument:
                catalog.write_instruments([listed])
            if bars:
                start = (series[0].ts_init - day) // day * day
                end = series[-1].ts_init // day * day + day
                catalog.write_bars(
                    series, start=start if covered_from is None else covered_from, end=end
                )
            written.append(series)
        return written[0]


@pytest.fixture
def alpaca_bars() -> AlpacaBars:
    return AlpacaBars()


def equations_one_and_two(
    returns: Sequence[float], trial_sharpes: Sequence[float], trials: float | None = None
) -> tuple[float, float]:
    """SR0 and the DSR of `returns` by eqs. 1-2, with N = `trials` or the count of `trial_sharpes`."""
    mean = statistics.fmean(returns)
    variance = statistics.fmean((value - mean) ** 2 for value in returns)
    skewness = statistics.fmean((value - mean) ** 3 for value in returns) / variance**1.5
    kurtosis = statistics.fmean((value - mean) ** 4 for value in returns) / variance**2
    pick = mean / statistics.stdev(returns)
    n = len(trial_sharpes) if trials is None else trials
    normal = NormalDist()
    sr0 = statistics.stdev(trial_sharpes) * (
        (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / n)
        + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (n * math.e))
    )
    spread = math.sqrt(1 - skewness * pick + (kurtosis - 1) / 4 * pick**2)
    return sr0, normal.cdf((pick - sr0) * math.sqrt(len(returns) - 1) / spread)


@pytest.fixture
def worked_dsr() -> Callable[..., tuple[float, float]]:
    """`equations_one_and_two`, for a test to check skfolio or the gate against."""
    return equations_one_and_two


@pytest.fixture
def stated_n() -> Callable[[optuna.Study], int]:
    """N as `rank` states it, whether it ranks the study or stops (`(N = n)` in its message)."""

    def n_of(study: optuna.Study) -> int:
        from canon_trading.ranking import RankingStopped
        from canon_trading.ranking import rank

        try:
            return rank(study).n
        except RankingStopped as stopped:
            found = re.search(r"\(N = (\d+)\)", str(stopped))
            assert found is not None, str(stopped)
            return int(found.group(1))

    return n_of


# --- Account A's market-data stream lock (ADR 0069). The same block is in tests/conftest.py and
# packages/canon-alpaca/tests/conftest.py, and tests/test_session_window.py keeps the two equal. ---
#
# Alpaca allows a user "in many subscriptions (or without one)" one connection to a market-data stream
# endpoint ("Streaming Market Data", https://docs.alpaca.markets/us/docs/streaming-market-data.md). On
# 2026-11-11 two pairs of one login each held a subscribed connection at once (ADR 0069), so each account's
# pair has its own stream lock, and a paper test that opens a stream takes account A's,
# `canon-alpaca-stream-tests.lock`, whose path `mise run paper:test` exports; paper:data's streaming modules,
# on A's pair, take it too, and a window on account B takes B's instead. A test marked `alpaca_stream` holds it
# from its setup until its teardown has seen every stream it started end; a paper test not marked that starts
# an alpaca-py market-data stream is stopped before the stream connects and fails. The lock is flock(2), the
# call lockf(1) makes ("BSD-style locking is used, as described in flock(2)", lockf(1)), so this lock and the
# lockf(1) of paper:data exclude each other (Python's `fcntl.flock`). The wait blocks in flock(2) under an
# interval timer, which sleeps, rather than polling. Offline, with no
# paper pair, it does nothing.
STREAM_LOCK_ENV = "CANON_ALPACA_STREAM_LOCK"
DEADLINE_ENV = "CANON_PAPER_TEST_DEADLINE"
"""The epoch second at which `mise run paper:test` kills the run (its `deadline`), which it exports."""
STREAM_LOCK_WAIT_SECS = 3900
"""As long as `paper:test` waits for account A's lock (`lockwait` in mise.toml), and
`session_window.PAPER_LOCK_WAIT_SECS`, set when a window held the one stream lock. A window no longer takes
this lock; a `paper:data` run that streams does, and may hold it longer than this wait."""
TEST_ROOM_SECS = 900
"""Left before the run's deadline for a streaming test once it has the lock: three times a whole paper run
(about five minutes, observed), so the deadline does not kill a test before its `finally` cleans up."""
STREAM_END_SECS = 60
"""How long the teardown waits for a stream the test started to end before the lock is let go: six times
the adapter's own wait for its stream thread (`canon_alpaca.data.STREAM_JOIN_TIMEOUT_SECS`)."""
STREAM_MARKER = "alpaca_stream"
KEPT_LOCKS: list[int] = []
"""Descriptors held past their test because a stream outlived it; the lock goes when the run ends."""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{STREAM_MARKER}: the test opens an Alpaca market-data stream, so it holds account A's stream lock "
        "(ADR 0069)",
    )


def take_stream_lock(path: str, wait_secs: float) -> int | None:
    """A descriptor holding the exclusive flock(2) lock on `path`, taken within `wait_secs`, or None, the
    descriptor closed, when the lock stayed held that long. Closing the descriptor releases the lock. The
    wait blocks in flock(2) under `ITIMER_REAL`, so it runs on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "the stream lock is taken on the main thread, where SIGALRM is delivered"
        )
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    if wait_secs <= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError

    before = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, wait_secs)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except (
        TimeoutError
    ):  # also when the timer fires just after flock returns: the lock goes with fd
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def refuse_streams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop every alpaca-py market-data stream before it connects (`DataStream.run`, which alpaca-py 0.45.0's
    stock, crypto, option and news streams inherit), and return the list each refused stream is named in."""
    started: list[str] = []

    def refused(stream: DataStream) -> None:
        started.append(type(stream).__name__)
        raise RuntimeError(
            f"this test is not marked {STREAM_MARKER}, so it holds no stream lock and opens no Alpaca "
            "market-data stream (ADR 0069)"
        )

    monkeypatch.setattr(DataStream, "run", refused)
    return started


def track_streams(monkeypatch: pytest.MonkeyPatch) -> list[threading.Event]:
    """Run alpaca-py's market-data streams as before, and return one event per stream started, set once its
    `run()` has returned, so the lock is let go only after the test's streams have ended."""
    ended: list[threading.Event] = []
    run = DataStream.run

    def tracked(stream: DataStream) -> None:
        done = threading.Event()
        ended.append(done)
        try:
            run(stream)
        finally:
            done.set()

    monkeypatch.setattr(DataStream, "run", tracked)
    return ended


def stream_wait_secs(now: float) -> float:
    """How long a streaming test may wait for the lock: `STREAM_LOCK_WAIT_SECS`, less whatever would leave it
    under `TEST_ROOM_SECS` before the run's deadline; `pytest.fail` if the deadline is not named."""
    deadline = os.environ.get(DEADLINE_ENV, "")
    if not deadline.isdigit():
        pytest.fail(
            f"{DEADLINE_ENV} is not set, so the test cannot tell how long it may wait; run paper tests with "
            "`mise run paper:test`, which names it. Nothing was placed."
        )
    return min(STREAM_LOCK_WAIT_SECS, int(deadline) - now - TEST_ROOM_SECS)


@pytest.fixture(autouse=True)
def alpaca_stream_lock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Hold the stream lock for a test marked `alpaca_stream`; refuse a stream to any other paper test."""
    if not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)):
        yield  # offline: nothing reaches Alpaca
        return
    if request.node.get_closest_marker(STREAM_MARKER) is None:
        started = refuse_streams(monkeypatch)
        yield
        if started:
            pytest.fail(
                f"started an Alpaca market-data stream ({', '.join(started)}) without the "
                f"{STREAM_MARKER} marker, so without the stream lock; mark the test (ADR 0069)"
            )
        return
    path = os.environ.get(STREAM_LOCK_ENV)
    if not path:
        pytest.fail(
            f"{STREAM_LOCK_ENV} is not set, so there is no stream lock to take; run paper tests with "
            "`mise run paper:test`, which names it. Nothing was placed."
        )
    wait = stream_wait_secs(datetime.now(UTC).timestamp())
    fd = take_stream_lock(path, wait) if wait >= 0 else None
    if fd is None:
        pytest.fail(
            f"the market-data stream lock stayed held for {max(wait, 0):.0f} s (another paper test or "
            f"paper:data streams on account A's pair), or {TEST_ROOM_SECS} s were no longer left before the run's deadline; "
            "nothing was placed"
        )
    ended = track_streams(monkeypatch)
    try:
        yield
    finally:
        give_up = monotonic() + STREAM_END_SECS
        if all(e.wait(max(0.0, give_up - monotonic())) for e in ended):
            os.close(fd)  # the lock goes with the descriptor (flock(2))
        else:
            KEPT_LOCKS.append(fd)
            pytest.fail(
                f"a market-data stream this test started was still running {STREAM_END_SECS} s after it "
                "ended; the stream lock is kept until this paper run ends (ADR 0069)"
            )


# --- End of the stream lock block. ---


@pytest.fixture(autouse=True)
def no_holdout_opened_in_the_main_checkout(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holdout opened at the default root marks a folder of its own, never the main checkout's
    `ledger/holdouts`."""
    from canon_trading import early_entry_study

    ledger = tmp_path_factory.mktemp("ledger") / "trials.db"
    monkeypatch.setattr(
        early_entry_study, "default_holdout_root", lambda: ledger.parent / "holdouts"
    )


PAPER_DIR_ENV = "CANON_PAPER_SESSION_DIR"
"""`session_window.PAPER_DIR_ENV`, written out so the suite's setup imports no trading code;
`test_no_test_resolves_the_real_paper_records_folder` fails if the two part."""


@pytest.fixture(autouse=True)
def no_test_reaches_the_real_paper_records(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test's session-window records folder is one of its own, never the per-user folder the
    scheduled runner writes and `scripts/automation status` reads. It is set for the whole suite here,
    so a test that names no `--log-dir` still cannot write the real heartbeat or record; a test of the
    real folder's own lookup removes the variable itself."""
    # a name under the run's base folder, made only by a test that writes there (os.urandom: unique
    # per test, so one test's gap markers never reach the next)
    folder = tmp_path_factory.getbasetemp() / "paper-records" / os.urandom(8).hex()
    monkeypatch.setenv(PAPER_DIR_ENV, str(folder))


@pytest.fixture
def references_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Mover window's references are in before its node (the lead's hold of 2026-11-12, item (i)); window
    tests' stub clients list no assets, so the reference read is one already in, of one name."""
    from concurrent.futures import Future

    from canon_trading import movers_rank_actor
    from canon_trading import session_window

    def start(trading: object, notes: list[str]):
        read: Future[movers_rank_actor.ReferenceRead] = Future()
        read.set_result(
            movers_rank_actor.ReferenceRead(
                datetime(2026, 11, 11).date(),
                {
                    "AAA": movers_rank_actor.movers_rank.Reference(
                        close=1.0, session=datetime(2026, 11, 10).date()
                    )
                },
                movers_rank_actor.movers_rank.SplitRead(
                    datetime(2026, 11, 10).date(), datetime(2026, 11, 11).date(), ()
                ),
                time_ns(),
                asked=1,
                wall_secs=0.0,
            )
        )
        return (lambda symbols, cutoff, session: read.result()), read

    monkeypatch.setattr(session_window, "start_reference_read", start)


@pytest.fixture
def new_york_regular_hours(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The real clock read as the nearest 11:00 New York, inside the regular session, for one test.

    A LiveNode keeps the realtime clock at the pin (`crates/common/src/live/clock.rs:55-57`), so a test
    that drives one runs at the hour it is started. `OwnerProtector._plan_holding` judges a quote's open
    session part from that clock's hour and minute in `session_window.NEW_YORK`
    (`src/canon_trading/owner_protector.py:1750-1765`); on sip from 00:00 to 04:00 New York no quote can
    pass, and the test fails by the hour. This puts a fixed UTC offset in `session_window.NEW_YORK` for
    the test, the one at which the real clock reads the nearest 11:00 New York: every stamp stays the
    real clock's and time runs on, and only the reading of New York wall time moves, by under 24 hours
    (`datetime.timezone`'s bound). The judge reads no weekday. A module that holds a zone of its own
    (`mover.NEW_YORK`, `canon_alpaca.data.NEW_YORK`) is not moved. Returns the instant pinned.
    """
    from datetime import timezone

    from canon_trading import session_window

    now = datetime.now(UTC)
    here = now.astimezone(NEW_YORK)
    pinned = here.replace(hour=11, minute=0, second=0, microsecond=0)
    if pinned - here > timedelta(hours=12):
        pinned -= timedelta(days=1)
    elif here - pinned > timedelta(hours=12):
        pinned += timedelta(days=1)
    offset = pinned.replace(tzinfo=None) - now.replace(tzinfo=None)
    monkeypatch.setattr(session_window, "NEW_YORK", timezone(offset, "New York, pinned"))
    return pinned
