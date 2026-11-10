"""Against Alpaca's paper environment: the daily bars of ADR 0048's study into the repo's catalog.

It runs only when a paper key pair is in the environment, under the names in `canon_alpaca.constants`;
otherwise pytest reports it as skipped. `mise run paper:test -k variant_study_bars` runs it alone under
the paper account's lock, since mise passes what follows the task name on to pytest (ADR 0040). Never
write the keys to a file.

What it calls: the asset lookup goes to Alpaca's paper Trading API and the bars to Alpaca's Market
Data API through `canon_trading.bars_to_catalog.load_bars`, as `test_paper_bars_to_catalog.py` does. It
places, modifies and cancels nothing and opens no position, so it leaves the account as it found it.

The window is `canon_trading.variant_study`'s, the study's sample: the bars whose `ts_init` lies from
noon New York on 2020-07-27 to noon on 2026-01-01, the trading days 2020-07-27 to 2025-12-31. The
checks are what the study needs before it runs: every symbol's first bar opens on 2020-07-27 and its
last on 2025-12-31, so the history covers the sample's start (ADR 0048; a first load from 2020-01-02
found the IEX daily bars of both symbols begin there), and the symbols have bars on
the same days, so their trials can share the study's session days (ADR 0041 term 2). The count of
bars is printed, not asserted: no exchange calendar was read for it.
"""

from __future__ import annotations

import os
from datetime import date
from datetime import datetime
from datetime import time
from pathlib import Path

import pytest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.bars_to_catalog import load_bars
from canon_trading.variant_study import NEW_YORK
from canon_trading.variant_study import SYMBOLS
from canon_trading.variant_study import WINDOW_END
from canon_trading.variant_study import WINDOW_START
from canon_trading.variant_study import bar_type

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "to run this paper test"
    ),
)

REPO_CATALOG = Path(__file__).parents[1] / "catalog"
FIRST_OPEN = datetime.combine(date(2020, 7, 27), time(0), NEW_YORK)
LAST_OPEN = datetime.combine(date(2025, 12, 31), time(0), NEW_YORK)


def test_paper_variant_study_bars_into_the_catalog() -> None:
    REPO_CATALOG.mkdir(exist_ok=True)
    catalog = ParquetDataCatalog(str(REPO_CATALOG))
    opens = {}
    for symbol in SYMBOLS:
        bars_type = bar_type(symbol)
        loaded = load_bars(bars_type, WINDOW_START, WINDOW_END, REPO_CATALOG, timeout_secs=600)
        bars = catalog.query_bars(
            [str(bars_type)],
            start=dt_to_unix_nanos(WINDOW_START),
            end=dt_to_unix_nanos(WINDOW_END),
        )
        print(f"{bars_type}: {len(bars)} bars in the window; catalog {loaded}")
        assert bars, f"no {bars_type} bars in the window"
        print(f"{bars_type}: first open {bars[0].ts_event}, last open {bars[-1].ts_event}")
        assert catalog.instruments([str(bars_type.instrument_id)])
        opens[symbol] = [bar.ts_event for bar in bars]
    for symbol in SYMBOLS:
        assert opens[symbol][0] == dt_to_unix_nanos(FIRST_OPEN), symbol
        assert opens[symbol][-1] == dt_to_unix_nanos(LAST_OPEN), symbol
    first, *others = SYMBOLS
    for symbol in others:
        assert opens[symbol] == opens[first], f"{symbol} and {first} have bars on different days"
