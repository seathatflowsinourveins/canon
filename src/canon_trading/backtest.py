"""Backtest entry point: strategies over bars from a `ParquetDataCatalog`, on a simulated Alpaca venue.

Contract: `nautilus_trader.backtest.BacktestNode`, the high-level backtest API of NautilusTrader
v2.0.0rc8. Read 2026-11-10:

* https://nautilustrader.io/docs/md/latest/concepts/backtesting/apis-and-runs.md ("High-level API": a
  node takes one `BacktestRunConfig`, is built, has strategies added through its run-specific methods,
  and `run()` returns the results; "Use fresh nodes for independent runs": one node per run, disposed
  afterwards).
* https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md ("`BacktestDataConfig`": `bar_types`
  selects the bars, and the node loads the instrument definitions they need from the same catalog while
  it builds the engine).
* https://nautilustrader.io/docs/md/latest/concepts/backtesting/bar-execution.md (bars drive fills only
  on an `L1_MBP` venue with `bar_execution` on, and only `EXTERNAL` time bars do; separately, a bar's
  `ts_init` must be the close of its interval, or the run sees the bar before it had formed).
* https://nautilustrader.io/docs/md/latest/concepts/backtesting/accounts-and-margin.md (the high-level
  API takes starting balances as strings; every venue needs an explicit fee model).
* The installed package's own docstrings, where the pages above are silent: `BacktestNode.add_strategy`
  ("Adds a constructed Python strategy to the engine for the given run config"), `BacktestNode.run`
  (with `raise_exception` off, a failed run is logged and its result left out) and
  `ParquetDataCatalog.get_intervals`.

The instrument is not built here. The node reads it from the catalog, so a backtest sees whatever was
stored, and what `canon_alpaca.providers.parse_equity` stores is the definition a paper run would load.

The simulated venue, and where each setting comes from:

* Named `ALPACA` (`canon_alpaca.constants.VENUE`), so `<symbol>.ALPACA` instruments route to it.
* `NETTING`: Alpaca keeps one position for a symbol ("Retrieves the account's open position for the
  given symbol", https://docs.alpaca.markets/us/reference/getopenposition-1.md).
* `CASH`: a choice made here, not a venue fact. The one strategy there is, `MaCross`, is long or flat
  and pays in full. Alpaca's paper accounts also allow margin and short selling
  (https://docs.alpaca.markets/us/docs/paper-trading.md, "Paper vs Live"); neither is simulated.
* `L1_MBP` with bar execution left on (its default), the only book type on which bars fill orders.
* A zero-rate fee model, stated because upstream requires one. Alpaca's paper environment "does not
  account for" regulatory fees (same page), so this matches paper, not live.
* Starting balances are the caller's. Alpaca creates a paper account with $100k (same page).

Known limits:

* An order sent from `on_bar` fills at that bar's close (bar-execution, "Order submission timing").
  The adapter stamps a daily bar's `ts_init` 24 hours after its open, hours after the session closed,
  so the fill is at a price the venue no longer offers. Read results as a check of the plumbing.
* An order larger than a quarter of the bar's volume fills its remainder one tick worse
  (bar-execution, "OHLC price simulation"; fill-prices-and-matching, "L1 books").
* One bar type for an instrument in a run. The simulated venue fills from the most granular bar type it
  is given for an instrument (bar-execution, "Processing bar data"). With daily and 12-hour bars of one
  instrument in a run, a strategy on the daily bars filled at other prices than it did alone (observed
  at v2.0.0rc8, 2026-11-10), so a second bar type for an instrument is refused.
* No slippage, latency, partial-fill or market-hours model is configured.
* A `NETTING` position that reopens keeps its ID; each earlier round trip is a snapshot (observed at
  v2.0.0rc8), which is why `BacktestRun` carries both lists.
* Runs are not compared here. Variants are ranked only by ADR 0010's protocol, and the trial ledger it
  needs is not written yet, so until it is a result is reported and not ranked.
* The catalog path is made absolute before use. Upstream's guides build it from `Path.cwd()`; given a
  relative path, the node's catalog query fails at v2.0.0rc8 with "No suitable object store found for
  file://catalog/..." (observed 2026-11-10), although `ParquetDataCatalog` itself reads and writes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestNode
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import DataCatalogConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import BookType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OmsType
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nautilus_trader.backtest import BacktestResult
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import Position

    from .ma_cross import MaCross


@dataclass(frozen=True)
class BacktestRun:
    """What one run left behind, as the engine's own objects, read before the node was disposed.

    `result` is the node's `BacktestResult`. `orders` is every order in the engine's cache, `positions`
    its positions in their final state, `position_snapshots` the earlier round trips of positions that
    reopened, and `account` the simulated venue's account. Nothing here is computed by this repo.
    """

    result: BacktestResult
    orders: list[Any]
    positions: list[Position]
    position_snapshots: list[Position]
    account: Any


def simulated_alpaca_venue(starting_balances: Sequence[str]) -> BacktestVenueConfig:
    """Return the venue configuration described in this module's header.

    `starting_balances` are strings such as `"100_000 USD"`, the form the high-level API takes.
    """
    return BacktestVenueConfig(
        name=str(VENUE),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=list(starting_balances),
        book_type=BookType.L1_MBP,
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
    )


def run_backtest(
    catalog_path: Path,
    strategies: Sequence[MaCross],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
) -> BacktestRun:
    """Run `strategies` together, on one account, over their bars in the catalog at `catalog_path`.

    The bars replayed are the ones the strategies subscribe to, read from each strategy's `bar_type`, so
    no strategy can be left waiting for bars the run never loads. Strategies may share a bar type; two
    different bar types for one instrument are refused (see this module's known limits). The parameter
    is typed for `MaCross`, the one strategy there is.

    The catalog must hold those bars and the definition of each instrument; a relative `catalog_path` is
    taken from the current directory. A catalog missing the bars of any strategy is refused before
    anything is built, because the engine would otherwise finish that strategy's run empty without an
    error. Every other failure (no instrument definition, a strategy that cannot start, an engine error)
    is raised by NautilusTrader and passes through.

    `logging` goes to the engine unchanged; with none, the engine logs at its own defaults. The logging
    subsystem is process-global (concepts/logging, "LogGuard"), and at v2.0.0rc8 the configuration of the
    first engine or node built in a process stays in force for every later one (observed 2026-11-10). So
    `logging` takes effect only in a process that has built none before, and a quiet setting given here
    also silences whatever is built afterwards.
    """
    if not strategies:
        raise ValueError("run_backtest needs at least one strategy")
    bar_types: dict[str, str] = {}  # instrument ID to its one bar type, in the strategies' order
    for strategy in strategies:
        bar_type = str(strategy.bar_type)
        instrument_id = strategy.bar_type.instrument_id
        if instrument_id.venue != VENUE:
            raise ValueError(
                f"{bar_type} is on venue {instrument_id.venue}, not {VENUE}, the one this backtest "
                "simulates",
            )
        other = bar_types.setdefault(str(instrument_id), bar_type)
        if other != bar_type:
            raise ValueError(
                f"{other} and {bar_type} are two bar types for {instrument_id}; a run takes one for "
                "an instrument",
            )
    path = str(Path(catalog_path).resolve())
    catalog = ParquetDataCatalog(path)
    for bar_type in bar_types.values():
        if not catalog.get_intervals(NautilusDataType.Bar, bar_type):
            raise LookupError(f"The catalog at {path} holds no {bar_type} bars")

    config = BacktestRunConfig(
        venues=[simulated_alpaca_venue(starting_balances)],
        data=[
            BacktestDataConfig(
                data_type=NautilusDataType.Bar,
                catalog=DataCatalogConfig(path=path),
                bar_types=list(bar_types.values()),
            ),
        ],
        engine=BacktestEngineConfig(logging=logging),
        # Off, a failed run is logged and left out of the results; on, it raises.
        raise_exception=True,
        # The engine's cache is emptied on disposal, so it is read first and disposed below.
        dispose_on_completion=False,
    )
    node = BacktestNode([config])
    try:
        node.build()
        for strategy in strategies:
            node.add_strategy(config.id, strategy)
        [result] = node.run()
        cache = node.get_engine_cache(config.id)
        return BacktestRun(
            result=result,
            orders=cache.orders(),
            positions=cache.positions(),
            position_snapshots=cache.position_snapshots(),
            account=cache.account_for_venue(VENUE),
        )
    finally:
        node.dispose()
