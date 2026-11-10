"""Against Alpaca's paper environment: a quarter of AAPL's daily bars into a catalog, then the backtest on it.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped. `mise run paper:test` supplies the pair, holds the paper
account's lock and runs it with every other paper test; `mise run paper:test -k bars_to_catalog` runs it
alone, since mise passes what follows the task name on to pytest. Never write the keys to a file.

What it calls: the asset lookup goes to Alpaca's paper Trading API through `TradingClient(paper=True)`,
and the bars to Alpaca's Market Data API, which paper accounts share ("Authentication",
https://docs.alpaca.markets/us/docs/authentication.md). It places, modifies and cancels nothing, and opens
no position, so it leaves the account as it found it. The bars are historical, so the result does not
depend on market hours.

The window runs from noon New York on 2025-01-02 to noon on 2025-04-01. A daily bar's `ts_init` is 24
hours after its midnight open (`canon_alpaca.data`), so the window holds the bars of the trading days
2025-01-02 to 2025-03-31: 60 of them, by the NYSE's calendar for 2025 (closed 2025-01-09, 2025-01-20 and
2025-02-17; https://www.nyse.com/markets/hours-calendars, UNVERIFIED this session). The load goes to a
temporary catalog for the checks, then to the repo's `catalog/`, where a later run finds it covered and
asks Alpaca for nothing.
"""

from __future__ import annotations

import os
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.backtest import run_backtest
from canon_trading.bars_to_catalog import load_bars
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

DAILY = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
NEW_YORK = ZoneInfo("America/New_York")
START = datetime.combine(date(2025, 1, 2), time(12), NEW_YORK)
END = datetime.combine(date(2025, 4, 1), time(12), NEW_YORK)
TRADING_DAYS = 60
DAY_NS = 86_400_000_000_000
REPO_CATALOG = Path(__file__).parents[1] / "catalog"


def test_paper_bars_to_catalog_then_the_backtest_runs_on_them(tmp_path: Path) -> None:
    loaded = load_bars(DAILY, START, END, tmp_path)

    assert loaded.count == TRADING_DAYS
    assert loaded.intervals == [(dt_to_unix_nanos(START), dt_to_unix_nanos(END))]
    bars = ParquetDataCatalog(str(tmp_path)).query_bars([str(DAILY)])
    assert all(bar.ts_init - bar.ts_event == DAY_NS for bar in bars)
    assert bars[0].ts_event == dt_to_unix_nanos(
        datetime.combine(date(2025, 1, 2), time(0), NEW_YORK)
    )
    assert bars[-1].ts_event == dt_to_unix_nanos(
        datetime.combine(date(2025, 3, 31), time(0), NEW_YORK)
    )
    for b in bars:
        low, high = float(b.low), float(b.high)
        assert low <= float(b.open) <= high and low <= float(b.close) <= high, b
    [instrument] = ParquetDataCatalog(str(tmp_path)).instruments([str(DAILY.instrument_id)])
    assert isinstance(instrument, Equity)

    assert load_bars(DAILY, START, END - timedelta(days=30), tmp_path) == loaded

    strategy = MaCross(MaCrossConfig(bar_type=DAILY, trade_size=Quantity.from_int(1)))
    run = run_backtest(tmp_path, [strategy], starting_balances=["100_000 USD"])
    assert run.result.iterations == TRADING_DAYS

    in_repo = load_bars(DAILY, START, END, REPO_CATALOG)
    assert in_repo.count >= TRADING_DAYS
    in_window = ParquetDataCatalog(str(REPO_CATALOG)).query_bars(
        [str(DAILY)], start=dt_to_unix_nanos(START), end=dt_to_unix_nanos(END)
    )
    assert [(b.ts_init, b.close) for b in in_window] == [(b.ts_init, b.close) for b in bars]
    assert ParquetDataCatalog(str(REPO_CATALOG)).instruments([str(DAILY.instrument_id)])
