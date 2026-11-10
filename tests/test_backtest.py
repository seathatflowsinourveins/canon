"""End to end: the moving-average cross over generated bars, through a real `BacktestNode`.

No test here opens a network connection, reads a credential or downloads a file. The bars are made in
this file, written to a `ParquetDataCatalog` in pytest's temporary directory, and read back by the node.

The instrument and the bars are built the way a paper run would get them, so the backtest sees the same
objects: an asset payload goes through `canon_alpaca.providers.parse_equity`, and bar payloads go through
`canon_alpaca.data.parse_bar`. The asset is the schema example in Alpaca's "Get Assets" reference
(https://docs.alpaca.markets/us/reference/get-v2-assets-1.md) and the bar fields are those of its
"Historical bars" response (https://docs.alpaca.markets/us/reference/stockbars.md), both as already used
in `packages/canon-alpaca/tests/conftest.py`. The prices are invented and are not Apple's, and one test
gives the same payloads a second symbol, MSFT, to have a second instrument. Those payloads
are parsed with alpaca-py's own models, which is why this file imports `alpaca`: it is pinned by
`packages/canon-alpaca` and reaches this package through it; the root manifest does not name it.

The series is 40 daily bars on consecutive weekdays from Monday 2025-03-10, each opening at midnight New
York time. Closes climb 101 to 115, fall to 100, then climb to 110, one dollar a bar. Each bar opens at
the previous close, so its high and low are its open and close. Volume is far above the trade size, so
no order outruns a quarter of a bar's volume and every fill is at a bar's close.

With a 3-bar and an 8-bar simple average on that series (worked by hand, bar indices from 0):

* bar 7, close 108: the slow average has its first full window, 104.5, under the fast one, 107. Buy.
* bar 17, close 112: both averages are 113. Nothing.
* bar 18, close 111: fast 112 is under slow 113. Sell, 3 dollars a share above the entry.
* bar 32, close 103: both averages are 102. Nothing.
* bar 33, close 104: fast 103 is over slow 102. Buy.
* bar 39, close 110: the data ends, the strategy stops and closes, 6 dollars a share above the entry.

Ten shares a trade makes that 30 and 60 dollars, and there are no fees, so 100,000 dollars ends as
100,090. The starting balance is what Alpaca gives a new paper account
(https://docs.alpaca.markets/us/docs/paper-trading.md).

One test runs a second strategy beside that one, with a 2-bar and a 12-bar average:

* bar 11, close 112: the slow average has its first full window, 106.5, under the fast one, 111.5. Buy,
  while the first strategy has been long since bar 7.
* bar 17, close 112: fast 112.5 is still over slow 111.5. Nothing.
* bar 18, close 111: fast 111.5 is under slow 1342/12, about 111.83. Sell, a dollar a share under the
  entry.
* bar 32, close 103: fast 102.5 is under slow 103.5. Nothing.
* bar 33, close 104: fast 103.5 is over slow 1238/12, about 103.17. Buy.
* bar 39, close 110: closed on stop, 6 dollars a share above the entry.

That is 10 dollars lost and 60 made. With the first strategy's 90, the shared account ends at 100,140.
The same pair run on two instruments with the same prices comes to the same figures.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import BarType
from nautilus_trader.model import Currency
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderSubmitted
from nautilus_trader.model import Quantity
from nautilus_trader.model import Venue

from canon_trading.backtest import BacktestRun
from canon_trading.backtest import backtest_run_config
from canon_trading.backtest import run_backtest
from canon_trading.backtest import run_backtest_config
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig

if TYPE_CHECKING:
    from conftest import AlpacaBars
    from nautilus_trader.model import Bar

NEW_YORK = ZoneInfo("America/New_York")
USD = Currency.from_str("USD")
BAR_TYPE = BarType.from_str("AAPL.ALPACA-1-DAY-LAST-EXTERNAL")
MSFT_BAR_TYPE = BarType.from_str("MSFT.ALPACA-1-DAY-LAST-EXTERNAL")
STARTING_BALANCE = Money(100_000, USD)
TRADE_SIZE = Quantity.from_int(10)

# (side, index of the bar whose close filled it); see the module docstring for the working.
EXPECTED_FILLS = [
    (OrderSide.BUY, 7),
    (OrderSide.SELL, 18),
    (OrderSide.BUY, 33),
    (OrderSide.SELL, 39),
]
EXPECTED_FILLS_2_12 = [
    (OrderSide.BUY, 11),
    (OrderSide.SELL, 18),
    (OrderSide.BUY, 33),
    (OrderSide.SELL, 39),
]


class Recording(MaCross):
    """`MaCross`, also keeping the bars and fill events the engine hands it."""

    def __init__(self, config: MaCrossConfig) -> None:
        super().__init__(config)
        self.bars_seen: list[Bar] = []
        self.fills_seen: list[OrderFilled] = []

    def on_bar(self, bar: Bar) -> None:
        self.bars_seen.append(bar)
        super().on_bar(bar)

    def on_order_filled(self, event: OrderFilled) -> None:
        self.fills_seen.append(event)


def config(
    *,
    bar_type: BarType = BAR_TYPE,
    trade_size: Quantity = TRADE_SIZE,
    fast: int = 3,
    slow: int = 8,
    **base_fields: object,
) -> MaCrossConfig:
    return MaCrossConfig(
        bar_type=bar_type,
        trade_size=trade_size,
        fast_period=fast,
        slow_period=slow,
        **base_fields,
    )


def run(path: Path, *strategies: MaCross) -> BacktestRun:
    """Run with the engine's default logging.

    NautilusTrader's logger is process-wide and keeps the first configuration it is given, and the
    adapter's tests, which run in this same pytest process, read what a node logs by default.
    """
    return run_backtest(path, strategies, starting_balances=[str(STARTING_BALANCE)])


def fills(orders: list, bars: list[Bar]) -> list[tuple[OrderSide, int]]:
    """Each filled order as (side, index of the bar it filled on), in the order they were sent.

    An order counts only if the simulated venue filled all of it at the close of a bar in the series, at
    the time that bar became available; anything else fails here.
    """
    index_at = {bar.ts_init: index for index, bar in enumerate(bars)}
    found = []
    for order in sorted(orders, key=lambda order: order.ts_init):
        events = order.events()
        assert any(isinstance(event, OrderSubmitted) for event in events)
        assert any(isinstance(event, OrderFilled) for event in events)
        assert order.status == OrderStatus.FILLED
        assert order.venue_order_id is not None
        assert order.filled_qty == TRADE_SIZE
        index = index_at[order.ts_last]
        assert order.avg_px == Decimal(float(bars[index].close))
        found.append((order.side, index))
    return found


def test_the_fixture_is_what_the_docstring_says(alpaca_bars: AlpacaBars) -> None:
    bars = alpaca_bars.daily(alpaca_bars.equity())

    assert len(bars) == 40
    assert [float(bar.close) for bar in bars] == list(alpaca_bars.CLOSES)
    # Each bar is stamped at its New York midnight open, and exists from 24 hours later.
    first_open = datetime(2025, 3, 10, tzinfo=NEW_YORK)
    assert bars[0].ts_event == int(first_open.timestamp()) * 1_000_000_000
    assert all(bar.ts_init - bar.ts_event == 24 * 3600 * 1_000_000_000 for bar in bars)
    opens = [datetime.fromtimestamp(bar.ts_event / 1e9, NEW_YORK) for bar in bars]
    assert all(o.weekday() < 5 and (o.hour, o.minute) == (0, 0) for o in opens)
    assert opens[-1].date().isoformat() == "2025-05-02"


def test_backtest_runs_end_to_end(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    bars = alpaca_bars.write(tmp_path)
    recording = Recording(config())

    backtest = run(tmp_path, recording)

    # Bars: the engine replayed every bar in the catalog, and the strategy received each one, in order.
    assert backtest.result.iterations == len(bars)
    assert recording.bars_seen == bars

    # Orders: four market orders, each submitted, given an ID by the simulated venue and filled in full
    # at the close of the bar that prompted it, at the time that bar became available.
    assert fills(backtest.orders, bars) == EXPECTED_FILLS
    assert backtest.result.total_orders == 4

    # The first three fills reached the running strategy. The fourth answers the close sent from
    # on_stop, and a stopped strategy's handlers are not called (concepts/backtesting/execution-flow,
    # "Shutdown semantics": "Strategy event handlers do not fire for the resulting events"; and
    # concepts/strategies, "Handlers": they "dispatch only while the strategy is `RUNNING`").
    assert [(fill.order_side, fill.last_px) for fill in recording.fills_seen] == [
        (side, bars[index].close) for side, index in EXPECTED_FILLS[:3]
    ]

    # Positions: two round trips. Under NETTING the position reopened under its ID, so the first round
    # trip is the snapshot and the second is the position, and nothing is left open.
    assert backtest.result.total_positions == 2
    [first] = backtest.position_snapshots
    [second] = backtest.positions
    assert first.is_closed
    assert second.is_closed
    assert (first.ts_opened, first.ts_closed) == (bars[7].ts_init, bars[18].ts_init)
    assert (second.ts_opened, second.ts_closed) == (bars[33].ts_init, bars[39].ts_init)
    assert first.realized_pnl == Money(30, USD)
    assert second.realized_pnl == Money(60, USD)

    # Account: both round trips made money and there are no fees, so the balance rose, by their sum.
    assert backtest.account.is_cash_account()
    balance = backtest.account.balance_total(USD)
    assert backtest.account.starting_balances() == {USD: STARTING_BALANCE}
    assert balance > STARTING_BALANCE
    assert balance - STARTING_BALANCE == first.realized_pnl + second.realized_pnl
    assert balance == Money(100_090, USD)


def test_two_strategies_each_trade_their_own_position(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    bars = alpaca_bars.write(tmp_path)
    first = MaCross(config(order_id_tag="A"))
    second = MaCross(config(fast=2, slow=12, order_id_tag="B"))

    backtest = run(tmp_path, first, second)

    # The second enters on bar 11 although the first has held the instrument since bar 7, and each exit
    # closes only its own shares. Deciding from the account's net position would give neither.
    by_strategy: dict[str, list] = {}
    for order in backtest.orders:
        by_strategy.setdefault(str(order.strategy_id), []).append(order)
    assert sorted(by_strategy) == ["MaCross-A", "MaCross-B"]
    assert fills(by_strategy["MaCross-A"], bars) == EXPECTED_FILLS
    assert fills(by_strategy["MaCross-B"], bars) == EXPECTED_FILLS_2_12

    # Each strategy's earlier round trip is a snapshot and its later one its position, as above.
    round_trips = [*backtest.position_snapshots, *backtest.positions]
    assert all(position.is_closed for position in round_trips)
    realized: dict[str, list[Money | None]] = {}
    for position in round_trips:
        realized.setdefault(str(position.strategy_id), []).append(position.realized_pnl)
    assert realized == {
        "MaCross-A": [Money(30, USD), Money(60, USD)],
        "MaCross-B": [Money(-10, USD), Money(60, USD)],
    }
    assert backtest.account.balance_total(USD) == Money(100_140, USD)


def test_strategies_on_two_instruments_both_run_to_the_end(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    bars = alpaca_bars.write(tmp_path, symbols=("AAPL", "MSFT"))
    aapl = MaCross(config(order_id_tag="A"))
    msft = MaCross(config(bar_type=MSFT_BAR_TYPE, fast=2, slow=12, order_id_tag="M"))

    backtest = run(tmp_path, aapl, msft)

    # Both instruments' bars were replayed, and each strategy traded its own instrument on them.
    assert backtest.result.iterations == 2 * len(bars)
    traded: dict[tuple[str, str], list] = {}
    for order in backtest.orders:
        traded.setdefault((str(order.strategy_id), str(order.instrument_id)), []).append(order)
    assert sorted(traded) == [("MaCross-A", "AAPL.ALPACA"), ("MaCross-M", "MSFT.ALPACA")]
    assert fills(traded["MaCross-A", "AAPL.ALPACA"], bars) == EXPECTED_FILLS
    assert fills(traded["MaCross-M", "MSFT.ALPACA"], bars) == EXPECTED_FILLS_2_12
    assert backtest.account.balance_total(USD) == Money(100_140, USD)


def test_a_relative_catalog_path_is_taken_from_the_current_directory(
    alpaca_bars: AlpacaBars, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpaca_bars.write(tmp_path / "catalog")
    monkeypatch.chdir(tmp_path)

    backtest = run(Path("catalog"), MaCross(config()))

    assert backtest.result.total_orders == 4


def test_a_run_without_a_strategy_is_refused(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    alpaca_bars.write(tmp_path)

    with pytest.raises(ValueError, match="at least one strategy"):
        run(tmp_path)


def test_a_run_config_run_without_a_strategy_is_refused(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    """The node would replay the bars with nothing to trade them and return an empty result."""
    alpaca_bars.write(tmp_path)
    config = backtest_run_config(
        tmp_path,
        [MaCross(MaCrossConfig(bar_type=BAR_TYPE, trade_size=TRADE_SIZE))],
        starting_balances=["100000 USD"],
    )

    with pytest.raises(ValueError, match="needs at least one strategy"):
        run_backtest_config(config, [])


def test_a_catalog_without_the_bars_is_refused(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    alpaca_bars.write(tmp_path, bars=False)

    with pytest.raises(LookupError, match="holds no AAPL.ALPACA-1-DAY-LAST-EXTERNAL bars"):
        run(tmp_path, MaCross(config()))


@pytest.mark.parametrize("lacking_first", [True, False])
def test_a_strategy_whose_bars_the_catalog_lacks_is_refused(
    alpaca_bars: AlpacaBars, tmp_path: Path, *, lacking_first: bool
) -> None:
    alpaca_bars.write(tmp_path)
    served = MaCross(config(order_id_tag="A"))
    lacking = MaCross(config(bar_type=MSFT_BAR_TYPE, order_id_tag="M"))
    strategies = [lacking, served] if lacking_first else [served, lacking]

    # The AAPL strategy alone would run. The MSFT one would get no bar and end empty without an error.
    with pytest.raises(LookupError, match="holds no MSFT.ALPACA-1-DAY-LAST-EXTERNAL bars"):
        run(tmp_path, *strategies)


def test_two_bar_types_for_one_instrument_are_refused(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    alpaca_bars.write(tmp_path)
    hourly = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")

    with pytest.raises(ValueError, match=r"are two bar types for AAPL\.ALPACA"):
        run(tmp_path, MaCross(config()), MaCross(config(bar_type=hourly)))


def test_a_catalog_without_the_instrument_fails_the_run(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    alpaca_bars.write(tmp_path, instrument=False)

    # NautilusTrader's own refusal while it builds the engine, in its words at v2.0.0rc8. The entry
    # point lets it through instead of returning an empty result.
    with pytest.raises(RuntimeError, match=r"No instruments found in catalog.*AAPL\.ALPACA"):
        run(tmp_path, MaCross(config()))


@pytest.mark.parametrize("elsewhere_first", [True, False])
def test_a_bar_type_on_another_venue_is_refused(
    alpaca_bars: AlpacaBars, tmp_path: Path, *, elsewhere_first: bool
) -> None:
    alpaca_bars.write(tmp_path)
    here = MaCross(config(order_id_tag="A"))
    elsewhere = MaCross(
        config(bar_type=BarType.from_str("AAPL.XNAS-1-DAY-LAST-EXTERNAL"), order_id_tag="X"),
    )
    strategies = [elsewhere, here] if elsewhere_first else [here, elsewhere]

    with pytest.raises(ValueError, match="is on venue XNAS, not ALPACA"):
        run(tmp_path, *strategies)


def test_a_strategy_whose_instrument_is_not_in_the_cache_cannot_start() -> None:
    # `run_backtest` cannot reach this: its node loads the instrument of every bar type or fails first.
    # A live node can, so the strategy is started here on an engine that was given a venue and nothing
    # else (the low-level API, concepts/backtesting/apis-and-runs and accounts-and-margin).
    engine = BacktestEngine(BacktestEngineConfig())
    try:
        engine.add_venue(
            venue=Venue("ALPACA"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[STARTING_BALANCE],
            fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        )
        engine.add_strategy(MaCross(config()))

        with pytest.raises(RuntimeError, match=r"instrument AAPL\.ALPACA is not in the cache"):
            engine.run()
    finally:
        engine.dispose()


def test_a_fractional_trade_size_cannot_start(alpaca_bars: AlpacaBars, tmp_path: Path) -> None:
    alpaca_bars.write(tmp_path)

    with pytest.raises(RuntimeError, match=r"trade_size 1\.5 is finer than AAPL\.ALPACA allows"):
        run(tmp_path, MaCross(config(trade_size=Quantity.from_str("1.5"))))


@pytest.mark.parametrize(("fast", "slow"), [(8, 3), (5, 5), (0, 5), (-1, 5)])
def test_config_refuses_periods_that_are_not_fast_then_slow(fast: int, slow: int) -> None:
    with pytest.raises(ValueError, match="0 < fast_period < slow_period"):
        MaCrossConfig(bar_type=BAR_TYPE, trade_size=TRADE_SIZE, fast_period=fast, slow_period=slow)


def test_config_refuses_a_zero_trade_size() -> None:
    with pytest.raises(ValueError, match="positive trade_size"):
        MaCrossConfig(bar_type=BAR_TYPE, trade_size=Quantity.from_int(0))


def test_config_keeps_the_base_fields_and_upstreams_default_periods() -> None:
    made = MaCrossConfig(bar_type=BAR_TYPE, trade_size=TRADE_SIZE, order_id_tag="007")

    assert (made.fast_period, made.slow_period) == (10, 20)
    assert made.order_id_tag == "007"
