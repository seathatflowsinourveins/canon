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

`equations_one_and_two` works the DSR without skfolio, under the conventions ADR 0031 read in skfolio's
source: the sample standard deviation in both Sharpe ratios, population skewness, raw kurtosis and
sqrt(T - 1).
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from statistics import NormalDist
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.trading.models import Asset
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.model import BarType
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Sequence
    from pathlib import Path

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
    ) -> list[Bar]:
        """Write each symbol's instrument and bars to the catalog at `path`; return the first's bars."""
        path.mkdir(exist_ok=True)  # the catalog does not create its own directory
        catalog = ParquetDataCatalog(str(path))
        written = []
        for symbol in symbols:
            listed = self.equity(symbol)
            series = self.daily(listed, closes, first=first, previous=previous)
            if instrument:
                catalog.write_instruments([listed])
            if bars:
                catalog.write_bars(series)
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
