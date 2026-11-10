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
  (with `raise_exception` off, a failed run is logged and its result left out),
  `ParquetDataCatalog.get_intervals` and `ParquetDataCatalog.get_missing_intervals_for_request`, and
  `CacheConfig(bar_capacity)` with `Cache.bars` (the bars the engine's cache keeps, newest first).

A run bounded by `start` and `end` must have its whole range covered by the catalog: any part the
catalog's own `get_missing_intervals_for_request` reports missing is refused, named. Upstream's
historical requests likewise read the intervals a catalog holds and fetch only the missing ones
(concepts/data/catalog.md, "Reading data"), and `canon_trading.bars_to_catalog` checks a load with it.
A catalog covers what it recorded: the window a load requested (`canon_trading.bars_to_catalog`), or,
for bars written without one, their first to last `ts_init`. A hole that no load can fill, such as a
window with no bar at all, which the catalog never records, is closed by upstream's
`ParquetDataCatalog.consolidate_data`, which merges the files around it into one interval (observed at
v2.0.0rc8, 2026-11-10). It marks any hole covered, one never loaded too, so it is for a stretch a load
showed to have no bar. One `ParquetDataCatalog` object asked again after its own write can answer from
before it (observed), so each call here builds its own and writes nothing.

The engine's cache keeps the bars a run replayed, up to `CacheConfig.bar_capacity` of each bar type
(10,000 by default; a run of 40 bars kept 10 at a capacity of 10 and 40 at 100, observed at v2.0.0rc8).
With `keep_bars`, the capacity is raised to the count of each bar type's bars in the range, read from
the catalog after every other check, never below upstream's default, so `BacktestRun.bars` can hold
every bar the run read. `BacktestResult.iterations` counts one per data item the engine processed
(`iteration += 1` in `run_impl`, `crates/backtest/src/engine.rs` at the pinned commit), so on a run of
bars only it is the number of bars replayed; `canon_trading.ledger` checks the cache against it.

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
* Runs are not compared here. A run meant as a trial is recorded by `canon_trading.ledger` (ADR 0035),
  and variants are ranked only by `canon_trading.ranking`, ADR 0010's protocol.
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

import pandas as pd
from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestNode
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import DataCatalogConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import BarType
from nautilus_trader.model import BookType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OmsType
from nautilus_trader.persistence import ParquetDataCatalog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nautilus_trader.backtest import BacktestResult
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import Bar
    from nautilus_trader.model import Position

    from .ma_cross import MaCross


@dataclass(frozen=True)
class BacktestRun:
    """What one run left behind, as the engine's own objects, read before the node was disposed.

    `result` is the node's `BacktestResult`. `orders` is every order in the engine's cache, `positions`
    its positions in their final state, `position_snapshots` the earlier round trips of positions that
    reopened, and `account` the simulated venue's account. `bars` is what the cache keeps of each bar
    type the run loaded (`Cache.bars`), in the order the cache gives them; every bar replayed, if the
    run was built with `keep_bars` and the catalog did not grow in between. Nothing here is computed by
    this repo.
    """

    result: BacktestResult
    orders: list[Any]
    positions: list[Position]
    position_snapshots: list[Position]
    account: Any
    bars: list[Bar]


# The simulated venue's settings, as data: `simulated_alpaca_venue` builds the venue from them and
# `canon_trading.ledger` keys a trial on them (ADR 0035 term 4), so the two cannot drift apart.
VENUE_SETTINGS: dict[str, Any] = {
    "oms_type": OmsType.NETTING,
    "account_type": AccountType.CASH,
    "book_type": BookType.L1_MBP,
}
FEE_RATES: dict[str, Decimal] = {"maker_rate": Decimal(0), "taker_rate": Decimal(0)}


def simulated_alpaca_venue(starting_balances: Sequence[str]) -> BacktestVenueConfig:
    """Return the venue configuration described in this module's header.

    `starting_balances` are strings such as `"100_000 USD"`, the form the high-level API takes.
    """
    return BacktestVenueConfig(
        name=str(VENUE),
        starting_balances=list(starting_balances),
        fee_model=MakerTakerFeeModel(
            maker_rate=FEE_RATES["maker_rate"], taker_rate=FEE_RATES["taker_rate"]
        ),
        **VENUE_SETTINGS,
    )


def backtest_run_config(
    catalog_path: Path,
    strategies: Sequence[MaCross],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
    run_config_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
    keep_bars: bool = False,
) -> BacktestRunConfig:
    """Check `strategies` against the catalog and build the run config `run_backtest` runs.

    The bars replayed are the ones the strategies subscribe to, read from each strategy's `bar_type`, so
    no strategy can be left waiting for bars the run never loads. Strategies may share a bar type; two
    different bar types for one instrument are refused (see this module's known limits). A catalog
    missing the bars of any strategy is refused before anything is built, because the engine would
    otherwise finish that strategy's run empty without an error.

    `run_config_id` becomes the config's `id`; with none, NautilusTrader gives it a fresh UUID4
    (`crates/backtest/src/config.rs` at the pinned commit), so the same config built twice gets two ids.
    `canon_trading.ledger` passes the trial's config key here (ADR 0035).

    `start` and `end`, UTC nanoseconds, bound the bars the node replays (`BacktestRunConfig.start` and
    `end`); with none, it replays every bar the catalog holds. `canon_trading.ledger` passes its study's
    sample (ADR 0041). A catalog that does not cover the whole range for a bar type is refused too,
    naming what is missing (this module's header); with one bound given, the other is the catalog's.

    With `keep_bars`, the engine's cache is sized to keep every bar of the range (this module's
    header); without it, the cache keeps upstream's default.
    """
    _require_strategies(strategies)
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
        intervals = catalog.get_intervals(NautilusDataType.Bar, bar_type)
        if not intervals:
            raise LookupError(f"The catalog at {path} holds no {bar_type} bars")
        if start is None and end is None:
            continue
        low = start if start is not None else min(first for first, _ in intervals)
        high = end if end is not None else max(last for _, last in intervals)
        if low > high:  # one bound given, past the catalog's other end
            raise LookupError(
                f"The catalog at {path} holds no {bar_type} bars from {start} to {end}"
            )
        missing = catalog.get_missing_intervals_for_request(
            low, high, NautilusDataType.Bar, bar_type
        )
        if missing:
            parts = ", ".join(f"{_utc(first)} to {_utc(last)}" for first, last in sorted(missing))
            raise LookupError(
                f"The catalog at {path} holds {bar_type} bars but does not cover the sample: "
                f"missing {parts}; load that range. Only if a load of it returned no bar, consolidating "
                "the catalog's files around it (ParquetDataCatalog.consolidate_data) records it",
            )
    cache = None
    if keep_bars:
        counts = [
            len(catalog.query(NautilusDataType.Bar, identifiers=[bar_type], start=start, end=end))
            for bar_type in bar_types.values()
        ]
        cache = CacheConfig(bar_capacity=max([*counts, CacheConfig().bar_capacity]))

    return BacktestRunConfig(
        venues=[simulated_alpaca_venue(starting_balances)],
        data=[
            BacktestDataConfig(
                data_type=NautilusDataType.Bar,
                catalog=DataCatalogConfig(path=path),
                bar_types=list(bar_types.values()),
            ),
        ],
        engine=BacktestEngineConfig(logging=logging, cache=cache),
        id=run_config_id,
        start=start,
        end=end,
        # Off, a failed run is logged and left out of the results; on, it raises.
        raise_exception=True,
        # The engine's cache is emptied on disposal, so it is read first and disposed below.
        dispose_on_completion=False,
    )


def run_backtest(
    catalog_path: Path,
    strategies: Sequence[MaCross],
    *,
    starting_balances: Sequence[str],
    logging: LoggerConfig | None = None,
) -> BacktestRun:
    """Run `strategies` together, on one account, over their bars in the catalog at `catalog_path`.

    The run config is `backtest_run_config`'s, with the same arguments, and what it refuses is refused
    here. The parameter is typed for `MaCross`, the one strategy there is. The catalog must hold the
    bars and the definition of each instrument; a relative `catalog_path` is taken from the current
    directory. Every other failure (no instrument definition, a strategy that cannot start, an engine
    error) is raised by NautilusTrader and passes through.

    `logging` goes to the engine unchanged; with none, the engine logs at its own defaults. The logging
    subsystem is process-global (concepts/logging, "LogGuard"), and at v2.0.0rc8 the configuration of the
    first engine or node built in a process stays in force for every later one (observed 2026-11-10). So
    `logging` takes effect only in a process that has built none before, and a quiet setting given here
    also silences whatever is built afterwards.

    A run meant as a trial goes through `canon_trading.ledger.record_trial`, which builds its run
    config with `backtest_run_config` and runs it with `run_backtest_config`.
    """
    config = backtest_run_config(
        catalog_path,
        strategies,
        starting_balances=starting_balances,
        logging=logging,
    )
    return run_backtest_config(config, strategies)


def _utc(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()


def _require_strategies(strategies: Sequence[MaCross]) -> None:
    if not strategies:
        raise ValueError("run_backtest needs at least one strategy")


def run_backtest_config(config: BacktestRunConfig, strategies: Sequence[MaCross]) -> BacktestRun:
    """Run `strategies` under `config`, a config `backtest_run_config` built, and dispose the node.

    `canon_trading.ledger` calls this with the config whose key it recorded, so the config hashed is
    the config run. No strategies is refused, as `backtest_run_config` refuses it: the node would run
    the bars with nothing to trade them and return an empty result without an error.
    """
    _require_strategies(strategies)
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
            bars=[
                bar
                for data in config.data
                for bar_type in data.bar_types or ()
                for bar in cache.bars(BarType.from_str(str(bar_type))) or []
            ],
        )
    finally:
        node.dispose()
