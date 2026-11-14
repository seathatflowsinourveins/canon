"""The mover window's session rows, native node build and generated-data replays (ADR 0091)."""

import json
import time
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
import requests
from alpaca.trading.client import TradingClient
from canon_alpaca.constants import USD
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.backtest import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import TradeId
from nautilus_trader.model import TraderId
from nautilus_trader.model import TradeTick
from nautilus_trader.risk import RiskEngineConfig

from canon_trading import mover as mover_module
from canon_trading import session_window
from canon_trading.mover import PAPER_LOSS_SHARE
from canon_trading.mover import Mover
from canon_trading.mover_window import MOVER_ID
from canon_trading.mover_window import OUTER_BOUND_MULTIPLE
from canon_trading.mover_window import mover_parts
from canon_trading.movers_rank_actor import MoversRankActorConfig
from canon_trading.paper_node import risk_limits
from canon_trading.session_window import MoverPick
from canon_trading.session_window import WindowConfig


@pytest.mark.parametrize("part", ["regular", "pre-market", "after-hours", "overnight"])
@pytest.mark.parametrize("liquidity", ["original", "thin", "liquid", "cash_falls"])
def test_account_sizing_trades_and_flattens_in_each_part(
    part, liquidity, alpaca_bars, monkeypatch, tmp_path
):
    """Real BacktestEngine replay; generated trade/quote timing follows test_mover's trade_tape."""
    budget = Decimal(12500 if liquidity in {"original", "cash_falls"} else 211000)
    cash = 2 * budget
    entry_cash = Decimal(5000) if liquidity == "cash_falls" else cash
    account = SimpleNamespace(
        get_account=lambda: SimpleNamespace(buying_power=str(4 * budget), cash=str(cash))
    )
    allocation = session_window.mover_allocation(cast(TradingClient, account), 2)
    risk_calls = []
    real_risk_sizer = mover_module.FixedRiskSizer

    def risk_sizer(*args):
        risk_calls.append(args)
        return real_risk_sizer(*args)

    monkeypatch.setattr(mover_module, "FixedRiskSizer", risk_sizer)
    instrument = alpaca_bars.equity("LLL")
    start = Decimal("0.700") if liquidity == "thin" else Decimal(10)
    starts = datetime(2025, 11, 10, 16, tzinfo=UTC)
    minute = 60_000_000_000
    ts_start = int(starts.timestamp() * 1_000_000_000)
    strategy, ranking, cap = mover_parts(
        instrument.id,
        # the tape's first price: since the ultra review of the composition (finding 14) a price that
        # fell after the start leaves the cap where it was, which test_mover's clamp cases cover
        price=start,
        move=Decimal("0.25"),
        computed_at=starts - timedelta(minutes=3),
        ends=starts + timedelta(minutes=20),
        entry_notional=allocation.entry_notional,
        extended=part != "regular",
        feed=session_window.MOVER_PARTS[part].feed,
        slots=allocation.slots,
        wind_down_at=starts + timedelta(minutes=18),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
    )
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            risk_engine=RiskEngineConfig(
                **risk_limits(
                    instrument.id,
                    max_notional_per_order=allocation.entry_notional * OUTER_BOUND_MULTIPLE,
                )
            ),
        )
    )
    eng.add_venue(
        instrument.id.venue,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(float(entry_cash), USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    eng.add_instrument(instrument)
    closes = [10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.46, 10.00, 9.90, 9.80]
    if liquidity == "thin":
        closes = [
            0.700,
            0.705,
            0.710,
            0.715,
            0.720,
            0.725,
            0.730,
            0.735,
            0.710,
            0.690,
            0.680,
            0.670,
        ]
    volume = {"original": 100_000, "thin": 6842, "liquid": 1_000_000, "cash_falls": 100_000}[
        liquidity
    ]
    # A cent each side of a 70-cent name is a spread wider than the 2% trail, and an entry whose bid is
    # already at the stop it was sized for is refused (S2), so the thin name's quote is a tenth of a cent.
    half = 0.001 if liquidity == "thin" else 0.01
    data = []
    for i, close in enumerate(closes):
        ts_close = ts_start + (i + 1) * minute
        data.append(
            TradeTick(
                instrument.id,
                Price(close, 4),
                Quantity.from_int(volume),
                AggressorSide.BUY,
                TradeId(f"T-{i}"),
                ts_close - 2,
                ts_close - 2,
            )
        )
        data.append(
            QuoteTick(
                instrument.id,
                Price(close - half, 4),
                Price(close + half, 4),
                Quantity.from_int(1_000_000),
                Quantity.from_int(1_000_000),
                ts_close - 1,
                ts_close - 1,
            )
        )
    eng.add_data(data)
    eng.add_strategy(strategy)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    try:
        eng.run()
        orders = eng.cache.orders(strategy_id=MOVER_ID)
        buys = [order for order in orders if order.side.name == "BUY"]
        assert len(buys) == 1
        buy = buys[0]
        assert buy.price is not None
        price = buy.price.as_decimal()
        # The composition's Mover enters on the fourth bar after the ranking reaches its slot, at that
        # close's ask (`half` over the close); the flow cap reads those four bars' dollar volume.
        [at] = [
            i for i, close in enumerate(closes) if Decimal(str(close)) + Decimal(str(half)) == price
        ]
        flow_dollars = sum(Decimal(str(close)) for close in closes[at - 3 : at + 1]) * volume
        budget_shares = min(budget, entry_cash) // price
        flow_shares = strategy.settings.flow_share * flow_dollars // price
        expected = int(min(budget_shares, flow_shares))
        assert not risk_calls, (
            f"FixedRiskSizer used: {buy.filled_qty} shares instead of the budget's {expected}"
        )
        assert buy.filled_qty.as_decimal() == Decimal(expected)
        assert strategy.settings.max_shares is None
        assert cap.max_position == allocation.entry_notional // start >= Decimal(expected)
        assert buy.filled_qty.as_decimal() * buy.price.as_decimal() > Decimal(500)
        assert not eng.cache.positions_open(strategy_id=MOVER_ID)
        assert strategy.refused == [] and cap.tripped is None
        assert all(order.status.name != "DENIED" for order in orders)
        report = session_window.WindowReport(
            "regular" if part == "regular" else "extended",
            "LLL",
            started="flow-budget-test",
            mover={"run": strategy.record()},
        )
        session_window.write_record(tmp_path, report)
        saved = json.loads((tmp_path / "latest.json").read_text())
        [entry] = saved["mover"]["run"]["entries"]
        sizing = entry["sizing"]
        assert entry["client_order_id"] == buy.client_order_id.value
        assert Decimal(sizing["budget_notional"]) == min(budget, entry_cash)
        assert Decimal(sizing["budget_shares"]) == min(budget, entry_cash) // buy.price.as_decimal()
        assert Decimal(sizing["startup_budget_notional"]) == budget
        assert Decimal(sizing["free_cash"]) == entry_cash
        assert Decimal(sizing["flow_dollars"]) == flow_dollars
        assert Decimal(sizing["flow_shares"]) == (
            strategy.settings.flow_share * flow_dollars // buy.price.as_decimal()
        )
        assert sizing["binding"] == ("flow" if liquidity == "thin" else "budget")
        assert sizing["quantity"] == str(expected)
        if liquidity == "thin":
            assert flow_shares < budget_shares and expected == 1352  # at the ask 0.726
    finally:
        eng.dispose()


def test_a_budget_window_record_has_no_max_shares():
    starts = datetime(2025, 11, 10, 16, tzinfo=UTC)
    strategy, _, _ = mover_parts(
        mover_module.InstrumentId.from_str("LLL.ALPACA"),
        price=Decimal(10),
        move=Decimal("0.25"),
        computed_at=starts,
        ends=starts + timedelta(minutes=20),
        extended=False,
        feed="sip",
        wind_down_at=starts + timedelta(minutes=18),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
    )
    assert "max_shares" not in strategy.record()["rows"]
    assert strategy.settings.max_shares is None


def test_a_repeating_startup_allocation_is_recorded_to_cents():
    account = SimpleNamespace(
        get_account=lambda: SimpleNamespace(buying_power="40000", cash="40000")
    )
    allocation = session_window.mover_allocation(cast(TradingClient, account), 3)
    assert allocation.entry_notional == Decimal("13333.33")
    assert allocation.record()["entry_notional"] == "13333.33"
    assert allocation.record()["engine_order_cap"] == "133333.30"


def test_a_startup_allocation_below_one_cent_refuses_by_name():
    account = SimpleNamespace(get_account=lambda: SimpleNamespace(buying_power="0.02", cash="0.02"))
    with pytest.raises(session_window.WindowRefused, match="entry notional.*cent"):
        session_window.mover_allocation(cast(TradingClient, account), 3)


@pytest.mark.parametrize("part", ["regular", "pre-market", "after-hours", "overnight"])
def test_each_parts_startup_entry_sets_strategy_shares_and_engine_cap(
    monkeypatch, tmp_path, part
) -> None:
    seen = []
    real = session_window.alpaca_paper_builder

    def builder(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_window, "alpaca_paper_builder", builder)
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        Decimal(1500),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        Decimal(1500),
    )
    node, strategy, cap = session_window.window_node(
        WindowConfig(
            symbol="LLL",
            session="regular" if part == "regular" else "extended",
            strategy="Mover",
            mover=True,
            log_dir=tmp_path,
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="allocation",
        feed=session_window.MOVER_PARTS[part].feed,
        part=part,
        pick=pick,
        ends=ends,
        entry_notional=Decimal(20000),
        loss_share=PAPER_LOSS_SHARE,
    )
    try:
        assert isinstance(strategy, Mover)
        assert strategy.settings.size_from_budget is True
        assert strategy.settings.max_notional == Decimal(20000)
        assert strategy.settings.max_shares is None
        assert not isinstance(cap, tuple) and cap.max_position == Decimal(13)
        assert seen[0]["max_notional_per_order"] == Decimal(20000) * OUTER_BOUND_MULTIPLE
    finally:
        node.dispose()


def test_a_mover_node_without_startup_allocation_refuses_before_builder(monkeypatch, tmp_path):
    monkeypatch.setattr(
        session_window, "alpaca_paper_builder", lambda *a, **k: pytest.fail("builder called")
    )
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="requires its startup account entry figure"):
        session_window.window_node(
            WindowConfig(symbol="LLL", strategy="Mover", mover=True, log_dir=tmp_path),
            wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
            log_name="no-allocation",
            feed="sip",
            pick=MoverPick("LLL", Decimal(1), {}, Decimal(1)),
            ends=ends,
        )


@pytest.mark.parametrize(
    ("part", "feed", "slots"),
    [("after-hours", "sip", 7), ("overnight", "boats", 7), ("pre-market", "sip", 7)],
)
def test_each_extended_part_builds_mover_with_the_configured_slots(
    tmp_path, part, feed, slots
) -> None:
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        Decimal("1.5"),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        Decimal("1.5"),
    )
    node, strategy, cap = session_window.window_node(
        WindowConfig(
            symbol="LLL", session="extended", strategy="Mover", mover=True, log_dir=tmp_path
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="m",
        feed=feed,
        part=part,
        pick=pick,
        entry_notional=Decimal(500),
        loss_share=PAPER_LOSS_SHARE,
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover)
        rows = strategy.settings
        assert rows.slots == rows.watch == slots
        assert rows.entries is None
        assert rows.extended_hours is True and rows.manage_stop is False
        assert rows.feed == feed and rows.max_notional == Decimal(500)
        assert not isinstance(cap, tuple)
        assert cap.max_position == Decimal(333)
    finally:
        node.dispose()


def test_an_overnight_window_ranks_live_with_adr_0087s_actor_on_the_overnight_feed(
    monkeypatch, tmp_path
) -> None:
    """The lead's decision of 2026-11-11, on the owner's word: the ranking is live in every part,
    overnight included. The overnight node builds ADR 0107's actor on the overnight feed, with the
    window's baseline, its records kept for the window, and no startup ranking substituted."""
    built: list[tuple[object, object]] = []
    real = session_window.window_rank

    def spy(config, records):
        built.append((config, records))
        return real(config, records)

    monkeypatch.setattr(session_window, "window_rank", spy)
    ends = datetime(2026, 11, 12, 4, 30, tzinfo=UTC)
    at = (ends - timedelta(minutes=58)).isoformat()

    def pick(symbol: str, move: float, others=()) -> MoverPick:
        return MoverPick(
            symbol, Decimal(2), {"at": at, "inputs": {"move": move}}, Decimal(2), others
        )

    picks = pick("LLL", 0.42, (pick("MMM", 0.31), pick("NNN", 0.27)))
    baseline = tmp_path / "boats.json"
    node, strategy, caps = session_window.window_node(
        WindowConfig(
            session="extended",
            strategy="Mover",
            mover=True,
            slots=7,
            log_dir=tmp_path,
            baseline_path=baseline,
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="live",
        feed="boats",
        part="overnight",
        pick=picks,
        entry_notional=Decimal(500),
        loss_share=PAPER_LOSS_SHARE,
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover) and isinstance(caps, tuple) and len(caps) == 3
        [(config, records)] = built
        assert isinstance(config, MoversRankActorConfig)
        assert config.feed == "boats" and config.baseline_path == str(baseline.resolve())
        assert records is strategy.ranking_records
        assert strategy.settings.rank_max_age_secs == session_window.RANK_MAX_AGE_SECS
        assert strategy.settings.slots == 7 and strategy.settings.entry_arm == "bridge"
        assert not hasattr(strategy, "startup_rank")
    finally:
        node.dispose()


def test_a_live_record_names_its_ranked_table_and_the_streams_symbols() -> None:
    """The lead's ranked table and symbol count (2026-11-11): from the window's ranking records, the last
    published generation's best ten, each marked with whether it held a slot, and the distinct symbols
    over the window and by its first minute beside the sample's count."""
    second = 1_000_000_000
    top = [{"rank": 1, "symbol": "LLL"}, {"rank": 2, "symbol": "MMM"}]
    run = {
        "opened_slots": ["MMM.ALPACA"],
        "ranking_records": [
            {"published": False, "symbols": 3, "latency_ns": {"generation": 0}},
            {
                "published": True,
                "symbols": 40,
                "latency_ns": {"generation": 30 * second},
                "top": [{"rank": 1, "symbol": "OLD"}],
            },
            {
                "published": True,
                "symbols": 90,
                "latency_ns": {"generation": 75 * second},
                "top": top,
            },
            {
                "published": True,
                "symbols": 120,
                "latency_ns": {"generation": 600 * second},
                "top": top,
            },
        ],
    }
    assert session_window.live_ranked_table(run) == [
        {"rank": 1, "symbol": "LLL", "held_slot": False},
        {"rank": 2, "symbol": "MMM", "held_slot": True},
    ]
    assert session_window.stream_symbols(run, sampled=21) == {
        "assets_loaded": "unavailable: no record counted the assets",
        "window": 120,
        "first_minute": 90,
        "sample": 21,
        "rows_ranked": "unavailable: no generation was published",
        "eligible": "unavailable: no generation was published",
        "first_publication_secs": "unavailable: no published generation with the actor's start",
    }
    run["ranking_records"][3] |= {"assets": 12000, "rows": 110, "eligible": 7}
    counted = session_window.stream_symbols(run, sampled=21)
    assert (counted["assets_loaded"], counted["rows_ranked"], counted["eligible"]) == (
        12000,
        110,
        7,
    )
    empty = session_window.live_ranked_table({"ranking_records": []})
    assert isinstance(empty, str) and empty.startswith("names ranked: none")


def test_an_extended_parts_slot_count_is_a_setting(monkeypatch, tmp_path) -> None:
    """The shipped row is one slot; changing the row reaches MoverConfig instead of a literal cap."""
    row = session_window.MOVER_PARTS["after-hours"]
    monkeypatch.setitem(session_window.MOVER_PARTS, "after-hours", replace(row, slots=2))
    ends = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
    pick = MoverPick(
        "LLL",
        Decimal("1.5"),
        {"at": (ends - timedelta(minutes=33)).isoformat(), "inputs": {"move": 2}},
        Decimal("1.5"),
    )
    node, strategy, _ = session_window.window_node(
        WindowConfig(
            symbol="LLL", session="extended", strategy="Mover", mover=True, log_dir=tmp_path
        ),
        wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
        log_name="m",
        feed="sip",
        part="after-hours",
        pick=pick,
        entry_notional=Decimal(500),
        loss_share=PAPER_LOSS_SHARE,
        ends=ends,
    )
    try:
        assert isinstance(strategy, Mover)
        assert strategy.settings.slots == strategy.settings.watch == 2
    finally:
        node.dispose()


def test_one_account_figure_reaches_every_mover_entry_and_the_engine(monkeypatch, tmp_path):
    from canon_trading import mover_window

    seen = {}
    real_builder = session_window.alpaca_paper_builder
    real_rank = mover_window.window_rank

    def builder(*args, **kwargs):
        seen["engine"] = kwargs["max_notional_per_order"]
        seen["load_all"] = kwargs.get("data_load_all")
        return real_builder(*args, **kwargs)

    def ranking(config, records):
        seen["rank"] = config.order_cap
        return real_rank(config, records)

    monkeypatch.setattr(session_window, "alpaca_paper_builder", builder)
    monkeypatch.setattr(session_window, "window_rank", ranking)
    ends = datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
    node, strategy, cap = session_window.window_node(
        WindowConfig(strategy="Mover", mover=True, slots=4, log_dir=tmp_path),
        wind_down_at=ends - timedelta(seconds=60),
        log_name="figure",
        feed="sip",
        pick=MoverPick("LLL", Decimal(10), {"inputs": {}}, Decimal(10)),
        ends=ends,
        entry_notional=Decimal(10_000),
        loss_share=PAPER_LOSS_SHARE,
    )
    try:
        assert isinstance(strategy, Mover) and not isinstance(cap, tuple)
        assert strategy.settings.max_notional == Decimal(10_000)
        assert strategy.settings.share_caps == {
            session_window.InstrumentId.from_str("LLL.ALPACA"): 1000
        }
        assert strategy.settings.max_shares is None and cap.max_position == Decimal(1000)
        assert strategy.settings.size_from_budget is True
        assert seen == {
            "engine": Decimal(10_000) * mover_window.OUTER_BOUND_MULTIPLE,
            "rank": 10_000.0,
            "load_all": True,
        }
    finally:
        node.dispose()


def test_the_live_node_routes_rank_subscriptions_through_the_real_alpaca_client(monkeypatch):
    """Only transport/provider inputs are offline; the native node and adapter hooks are real.

    rc8 Python adapter template's factory/provider/node contract, with ADR 0087's documented params.
    Both clients are injected, so the adapter connect path never reads credentials.
    """
    from canon_alpaca.constants import USD
    from canon_alpaca.data import AlpacaDataClient
    from nautilus_trader.common import Environment
    from nautilus_trader.config import DataClientConfig
    from nautilus_trader.config import LiveNodeConfig
    from nautilus_trader.live import LiveNode
    from nautilus_trader.live.clients import DataClientFactory
    from nautilus_trader.live.providers import InstrumentProvider
    from nautilus_trader.model import BarType
    from nautilus_trader.model import Equity
    from nautilus_trader.model import InstrumentId
    from nautilus_trader.model import Price
    from nautilus_trader.model import Symbol
    from nautilus_trader.model import TraderId
    from nautilus_trader.trading import Strategy

    instrument_id = InstrumentId.from_str("AAA.ALPACA")
    bars = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")

    class Stream:
        def __init__(self):
            self.stopped = Event()
            self.calls = {}
            self.registrations = 0

        def subscribe_bars(self, handler, *symbols):
            self.calls["bars"] = symbols

        def subscribe_updated_bars(self, handler, *symbols):
            self.calls["updatedBars"] = symbols

        def subscribe_trading_statuses(self, handler, *symbols):
            self.calls["statuses"] = symbols

        def register_trade_cancels(self, handler):
            self.registrations += 1

        def register_trade_corrections(self, handler):
            self.registrations += 1

        def run(self):
            assert self.stopped.wait(5), "the node did not stop its offline stream"

        def stop(self):
            self.stopped.set()

    stream = Stream()
    historical = SimpleNamespace(_session=requests.Session())
    # the client builds its stream on the subscription's feed (ADR 0055): a stream handed to the
    # constructor serves only subscriptions that name none (canon-alpaca's test_data_client `venue`)
    import canon_alpaca.data
    from alpaca.data.enums import DataFeed
    from canon_alpaca.constants import ENV_API_KEY
    from canon_alpaca.constants import ENV_SECRET_KEY

    monkeypatch.setenv(ENV_API_KEY, "placeholder")
    monkeypatch.setenv(ENV_SECRET_KEY, "placeholder")
    built: dict[str, dict] = {}

    def build_stream(**kwargs):
        built["stream"] = kwargs
        return stream

    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", build_stream)

    class Factory(DataClientFactory):
        @staticmethod
        def create(*, name, config, cache, clock):
            provider = InstrumentProvider()
            provider.add(
                Equity(
                    instrument_id=instrument_id,
                    raw_symbol=Symbol("AAA"),
                    currency=USD,
                    price_precision=4,
                    price_increment=Price.from_str("0.0001"),
                    ts_event=0,
                    ts_init=0,
                )
            )
            return AlpacaDataClient(
                name=name,
                config=config,
                cache=cache,
                clock=clock,
                instrument_provider=cast(Any, provider),
                historical_client=cast(Any, historical),
            )

    class Probe(Strategy):
        def on_start(self):
            self.subscribe_bars([bars], params={"feed": "sip", "wildcard": True})
            self.subscribe_instrument_status(
                [instrument_id], params={"feed": "sip", "wildcard": True}
            )
            self.clock.set_time_alert(
                "end",
                self.clock.utc_now() + timedelta(milliseconds=100),
                lambda event: self.shutdown_system("offline subscription check complete"),
            )

    node = LiveNode.build(
        "OFFLINE-RANK",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("TESTER-001"),
            timeout_connection_secs=2,
            timeout_disconnection_secs=2,
            delay_post_stop_secs=0,
            data_clients={"ALPACA": DataClientConfig()},
        ),
        data_factories={"ALPACA": Factory},
    )
    node.add_strategy(Probe())
    try:
        node.run()
    finally:
        node.dispose()
        historical._session.close()
    assert stream.registrations == 2  # the real _connect ran with the injected transport
    assert stream.calls == {"bars": ("*",), "statuses": ("*",)}  # no revised bars (2026-11-12)
    assert built["stream"]["feed"] == DataFeed.SIP


def test_a_firing_record_carries_the_ranked_table_with_absent_columns_named():
    """The lead's decision of 2026-11-11: every firing record carries the node's top ten, each with its
    move, dollar volume, normal dollar volume for the same clock minutes, ratio, tier and slot."""
    from canon_trading.session_window import ranked_table

    ranked = [
        {
            "symbol": f"S{i}",
            "score": 1.0 - i / 100,
            "move": 0.5 - i / 100,
            "dvol_ratio": 4.0,
            "close": 2.0,
            "reference": 1.5,
            "dvol": 1000.0 * (i + 1),
        }
        for i in range(11)
    ]
    sample = {"from": "2026-11-12T03:58:00+00:00", "elapsed_minutes": 2}
    table = ranked_table(
        {"ranked": ranked, "sample": sample}, {"opened_slots": ["S1.ALPACA"]}, tier="unavailable: x"
    )
    assert [row["symbol"] for row in table] == [f"S{i}" for i in range(10)]
    assert table[1] == {
        "rank": 2,
        "symbol": "S1",
        "move": 0.49,
        "dollar_volume": 2000.0,
        "normal_dollar_volume": 500.0,
        "ratio": 4.0,
        "ratio_span": "the stream sample's 2 clock minutes from 2026-11-12T03:58:00+00:00",
        "tier": "unavailable: x",
        "held_slot": True,
    }
    assert table[0]["held_slot"] is False
    zero = ranked_table({"ranked": [dict(ranked[0], dvol_ratio=0.0)]}, {}, tier="t")
    assert zero[0]["normal_dollar_volume"].startswith("unavailable")
    assert zero[0]["ratio_span"].startswith("unavailable")
    assert ranked_table({}, {}, tier="t") == []


def test_a_firing_record_carries_each_entrys_dollars_and_the_realized_result():
    """The lead's order of 2026-11-11: each entry's size in dollars and which bound set it, and the
    window's realized result in dollars, from the strategy's own fills."""
    from canon_trading.session_window import trade_result

    fills = [
        ("E1", "AAA.ALPACA", "BUY", "100", "10.00"),
        ("S1", "AAA.ALPACA", "SELL", "60", "10.50"),
        ("S2", "AAA.ALPACA", "SELL", "40", "9.75"),
        ("E2", "BBB.ALPACA", "BUY", "10", "4.00"),
        ("E2", "BBB.ALPACA", "BUY", "10", "4.20"),
        ("S3", "BBB.ALPACA", "SELL", "5", "5.00"),
    ]
    sent = [
        {
            "client_order_id": "E1",
            "sizing": {"budget_notional": "1000", "flow_dollars": "50000", "binding": "budget"},
        },
        {"client_order_id": "E2"},
    ]
    result = trade_result(fills, sent, entry_figure=Decimal(1000))
    assert result["entries"] == [
        {
            "client_order_id": "E1",
            "instrument_id": "AAA.ALPACA",
            "shares": "100",
            "dollars": "1000.00",
            "entry_figure": "1000",
            "budget_notional": "1000",
            "flow_dollars": "50000",
            "bound": "budget",
        },
        {
            "client_order_id": "E2",
            "instrument_id": "BBB.ALPACA",
            "shares": "20",
            "dollars": "82.00",
            "entry_figure": "1000",
            "budget_notional": "unavailable: no sizing record",
            "flow_dollars": "unavailable: no sizing record",
            "bound": "unavailable: no sizing record",
        },
    ]
    # AAA: 630 + 390 - 1000 = 20.00; BBB: 5 sold at 5.00 against an average of 4.10 = 4.50, 15 open
    assert result["by_instrument"]["AAA.ALPACA"] == {
        "bought": "1000.00",
        "sold": "1020.00",
        "open_shares": "0",
        "realized": "20.00",
    }
    assert result["by_instrument"]["BBB.ALPACA"]["open_shares"] == "15"
    assert result["by_instrument"]["BBB.ALPACA"]["realized"] == "4.50"
    assert result["realized_usd"] == "24.50"
    assert result["flat"] is False
    assert "commission" in result["basis"]


def test_a_name_that_entered_is_not_counted_as_passed_over_for_no_context() -> None:
    """A late context can permit entry after the opening wrote a no-context note."""
    run = {
        "notes": {
            "BBB.ALPACA": ["BBB.ALPACA: entry arm registered: no entry context supplied"],
        },
        "entries": [
            {"instrument_id": "BBB.ALPACA", "client_order_id": "E1", "sizing": {"roster": False}},
        ],
    }
    assert session_window.passed_over_without_context(run) == {"count": 0, "names": []}


def test_the_record_counts_ranked_names_a_registered_arm_could_not_enter_for_no_day_context() -> (
    None
):
    """The lead's order for the 04:00 firing (2026-11-12): a ranked name with no day context is passed over
    by name, and the record counts them."""
    run = {
        "notes": {
            "BBB.ALPACA": ["BBB.ALPACA: entry arm registered: no entry context supplied"],
            "AAA.ALPACA": ["AAA.ALPACA: entry arm registered: no entry context supplied"],
            "CCC.ALPACA": ["entered at move 0.3"],
        }
    }
    assert session_window.passed_over_without_context(run) == {
        "count": 2,
        "names": ["AAA.ALPACA", "BBB.ALPACA"],
    }


def test_the_record_lists_each_market_data_subscription() -> None:
    """The manifest's feeds section (2026-11-12): each subscription's feed, channel, wildcard or listed, and
    instruments."""
    listed = session_window.subscriptions({"opened_slots": ["A.ALPACA", "B.ALPACA"]}, "sip", 12000)
    assert [(s["channel"], s["wildcard"], s["instruments"]) for s in listed] == [
        ("bars", True, 12000),
        ("statuses", True, 12000),
        ("quotes", False, 2),
        ("trades", False, 2),
    ]
    assert {s["feed"] for s in listed} == {"sip"}


def test_the_reference_pre_read_reads_every_loaded_asset_for_the_actors_key(monkeypatch) -> None:
    from canon_trading import movers_rank_actor

    asked: list[tuple[tuple[str, ...], object, object]] = []

    def read(symbols, cutoff, session):
        asked.append((tuple(symbols), cutoff, session))
        found = {
            s: movers_rank_actor.movers_rank.Reference(close=1.0, session=cutoff) for s in symbols
        }
        splits = movers_rank_actor.movers_rank.SplitRead(cutoff, session, ())
        return movers_rank_actor.ReferenceRead(cutoff, found, splits, 7)

    monkeypatch.setattr(movers_rank_actor, "alpaca_reference_reader", read)
    requests_seen: list[object] = []

    def get_all_assets(request):
        requests_seen.append(request)
        return [SimpleNamespace(symbol="BBB"), SimpleNamespace(symbol="AAA")]

    notes: list[str] = []
    reader, read_future = session_window.start_reference_read(
        cast("TradingClient", SimpleNamespace(get_all_assets=get_all_assets)), notes
    )
    assert read_future.result(timeout=10).read_ns == 7
    now = time.time_ns()
    key = (movers_rank_actor.reference_cutoff(now), movers_rank_actor.trade_date(now))
    assert reader(["AAA", "BBB"], *key).read_ns == 7
    assert asked == [(("AAA", "BBB"), *key)] and len(requests_seen) == 1
    assert notes[0].startswith("pre-read took ")
    assert notes[1] == "pre-read before the node: 2 symbols; 0 of the 2 asked for were not in it"


def test_the_reference_pre_read_runs_on_a_daemon_thread_and_names_a_failure(monkeypatch) -> None:
    """A window refused after the pre-read starts is not held at exit by it, and a failed pre-read
    leaves a note, which is what the whole-market check waits on (code-review-high/universe-6, -7)."""
    import threading

    from canon_trading import movers_rank_actor

    release = Event()

    def read(symbols, cutoff, session):
        release.wait(10)
        raise RuntimeError("refused")

    monkeypatch.setattr(movers_rank_actor, "alpaca_reference_reader", read)
    notes: list[str] = []
    session_window.start_reference_read(
        cast(
            "TradingClient",
            SimpleNamespace(get_all_assets=lambda request: [SimpleNamespace(symbol="AAA")]),
        ),
        notes,
    )
    try:
        (thread,) = [t for t in threading.enumerate() if t.name.startswith("reference-pre-read")]
        assert thread.daemon
    finally:
        release.set()
    thread.join(10)
    assert notes == ["pre-read failed: RuntimeError: refused"]


def _reference_read(found: int, asked: int, *, splits_read: bool = True):
    from concurrent.futures import Future

    from canon_trading import movers_rank_actor

    refs = {
        f"S{i}": movers_rank_actor.movers_rank.Reference(close=1.0, session=date(2026, 11, 11))
        for i in range(found)
    }
    splits = (
        movers_rank_actor.movers_rank.SplitRead(date(2026, 11, 11), date(2026, 11, 12), ())
        if splits_read
        else None
    )
    future: Future = Future()
    future.set_result(
        movers_rank_actor.ReferenceRead(
            date(2026, 11, 12),
            refs,
            splits,
            1_794_500_000_000_000_000,
            asked=asked,
            wall_secs=44.567,
        )
    )
    return future


def test_a_mover_window_has_its_references_in_before_the_node_and_records_the_read() -> None:
    """The lead's hold of 2026-11-12, item (i): every reference in before the node starts, with the
    read's wall time and its three counts in the record."""
    got = session_window.references_before_node(_reference_read(3, 5), [], 1.0)
    assert got["before_node"] is True
    assert got["wall_secs"] == 44.57
    assert (got["names_asked"], got["with_close"], got["without_close"]) == (5, 3, 2)
    assert got["in_at"] == "2026-11-12T16:13:20.000+00:00"
    waited = got["waited_secs"]
    assert isinstance(waited, float) and 0 <= waited < 1


def test_a_mover_window_without_its_references_in_is_refused_before_any_node() -> None:
    from concurrent.futures import Future

    pending: Future = Future()
    with pytest.raises(session_window.WindowRefused, match="not in within 0.05 s"):
        session_window.references_before_node(pending, [], 0.05)
    failed: Future = Future()
    failed.set_exception(TimeoutError("read timed out"))
    with pytest.raises(session_window.WindowRefused, match="failed: TimeoutError: read timed out"):
        session_window.references_before_node(failed, [], 1.0)
    with pytest.raises(session_window.WindowRefused, match="did not start .not pre-read: X"):
        session_window.references_before_node(None, ["not pre-read: X"], 1.0)
    with pytest.raises(
        session_window.WindowRefused, match="not usable: no reference close was read"
    ):
        session_window.references_before_node(_reference_read(0, 5), [], 1.0)
    # code review of the snapshot read, finding 3: the gate refuses what the actor would refuse
    with pytest.raises(session_window.WindowRefused, match="not usable: the splits were not read"):
        session_window.references_before_node(_reference_read(3, 5, splits_read=False), [], 1.0)


def test_the_record_times_the_first_publication_from_the_actors_start() -> None:
    start = 1_000_000_000_000
    run = {
        "ranking_records": [
            {"published": False, "started_ns": start, "latency_ns": {"generation": start + 10**9}},
            {
                "published": True,
                "started_ns": start,
                "latency_ns": {"generation": start + 61_250_000_000},
            },
        ]
    }
    assert session_window.first_publication_secs(run) == 61.2
    unavailable = session_window.first_publication_secs({"ranking_records": []})
    assert isinstance(unavailable, str) and unavailable.startswith("unavailable")


def test_the_record_names_each_ratios_dollar_basis_on_this_tree() -> None:
    assert session_window.dollar_basis() == {
        "day_ratio_0087": "typical_x_volume",
        "rdc_1": "absent: not built on this tree",
    }


def test_the_record_counts_roster_and_non_roster_entries_with_each_non_roster_bound() -> None:
    """The lead's ruling of 2026-11-12: (ii-a.1) lets a name outside the start-up roster enter, bounded
    by Mover's sizing alone, so the record counts entries both ways and keeps each non-roster entry's
    dollars and the bound that held; an entry sized without the budget path is counted as unrecorded."""
    run = {
        "entries": [
            {"instrument_id": "AAA.ALPACA", "sizing": {"roster": True, "held_by": "start_cap"}},
            {
                "instrument_id": "BBB.ALPACA",
                "sizing": {"roster": False, "held_by": "flow_share", "notional": "998.40"},
            },
            {
                "instrument_id": "CCC.ALPACA",
                "sizing": {"roster": False, "held_by": "free_balance", "notional": "512.00"},
            },
            {"instrument_id": "DDD.ALPACA"},
        ]
    }
    assert session_window.roster_entries(run) == {
        "roster": 1,
        "non_roster": 2,
        "unrecorded": 1,
        "non_roster_entries": [
            {"instrument_id": "BBB.ALPACA", "dollars": "998.40", "held_by": "flow_share"},
            {"instrument_id": "CCC.ALPACA", "dollars": "512.00", "held_by": "free_balance"},
        ],
    }


def test_the_window_end_request_count_is_refreshed_at_the_final_snapshot(monkeypatch) -> None:
    """The endpoint count at the window's end is taken where the request budget is, after the stop's
    flattening, not before it (code-review-high/universe-2-1)."""
    monkeypatch.setattr(session_window, "endpoint_counts", lambda: {"DELETE a/v2/orders/{id}": 1})
    report = {"mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}}}
    session_window._snapshot_requests(report)
    assert report["mover"]["http_requests"] == {
        "to_node_start": {},
        "to_window_end": {"DELETE a/v2/orders/{id}": 1},
    }


def test_the_window_end_carries_the_trade_condition_counts_by_code(monkeypatch) -> None:
    """The lead's ruling of 2026-11-12: once the trade-condition filter runs, its counts by code are in
    the window's record from the first window."""
    counts = {"stream": {"I": 1067, "kept": 727}, "request": {"kept": 3}}
    monkeypatch.setattr(session_window, "condition_counts", lambda: counts)
    report = {"mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}}}
    session_window._snapshot_requests(report)
    assert report["mover"]["trade_conditions"] == counts


def test_a_window_with_the_filter_off_has_no_trade_condition_field(monkeypatch) -> None:
    monkeypatch.setattr(session_window, "condition_counts", dict)
    report = {"mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}}}
    session_window._snapshot_requests(report)
    assert "trade_conditions" not in report["mover"]


def test_the_window_end_carries_the_late_prints_the_adapter_set_aside(monkeypatch) -> None:
    """A streamed print the data client's sequence check set aside is counted by name with its
    lateness, and the window's record carries the count, per symbol."""
    late = {"AAPL": {"count": 3, "most_late_ms": 1250}, "MSFT": {"count": 1, "most_late_ms": 1}}
    monkeypatch.setattr(session_window, "late_prints_set_aside", lambda: late)
    report = {"mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}}}
    session_window._snapshot_requests(report)
    assert report["mover"]["late_prints_set_aside_by_the_adapter"] == late


def test_a_window_whose_adapter_set_no_print_aside_has_no_such_field(monkeypatch) -> None:
    monkeypatch.setattr(session_window, "late_prints_set_aside", dict)
    report = {"mover": {"http_requests": {"to_node_start": {}, "to_window_end": {}}}}
    session_window._snapshot_requests(report)
    assert "late_prints_set_aside_by_the_adapter" not in report["mover"]


@pytest.mark.parametrize("part", ["regular", "pre-market", "after-hours", "overnight"])
@pytest.mark.parametrize("liquidity", ["original", "thin", "liquid", "cash_falls"])
def test_selected_tier_account_sizing_trades_and_flattens_in_each_part(
    part, liquidity, alpaca_bars, monkeypatch, tmp_path
):
    """Real BacktestEngine replay; generated trade/quote timing follows test_mover's trade_tape."""
    budget = Decimal(12500 if liquidity in {"original", "cash_falls"} else 211000)
    cash = 2 * budget
    entry_cash = Decimal(5000) if liquidity == "cash_falls" else cash
    account = SimpleNamespace(
        get_account=lambda: SimpleNamespace(buying_power=str(4 * budget), cash=str(cash))
    )
    allocation = session_window.mover_allocation(cast(TradingClient, account), 2)
    risk_calls = []
    real_risk_sizer = mover_module.FixedRiskSizer

    def risk_sizer(*args):
        risk_calls.append(args)
        return real_risk_sizer(*args)

    monkeypatch.setattr(mover_module, "FixedRiskSizer", risk_sizer)
    instrument = alpaca_bars.equity("LLL")
    start = Decimal("0.700") if liquidity == "thin" else Decimal(10)
    starts = datetime(2025, 11, 10, 16, tzinfo=UTC)
    minute = 60_000_000_000
    ts_start = int(starts.timestamp() * 1_000_000_000)
    strategy, ranking, cap = mover_parts(
        instrument.id,
        # the tape's first price: since the ultra review of the composition (finding 14) a price that
        # fell after the start leaves the cap where it was, which test_mover's clamp cases cover
        price=start,
        move=Decimal("0.25"),
        computed_at=starts - timedelta(minutes=3),
        ends=starts + timedelta(minutes=20),
        entry_notional=allocation.entry_notional,
        extended=part != "regular",
        feed=session_window.MOVER_PARTS[part].feed,
        slots=allocation.slots,
        wind_down_at=starts + timedelta(minutes=18),
        market_exit_interval_ms=1000,
        market_exit_max_attempts=60,
        trail_rows={"trail_mode": "tier"},
    )
    # T1/T2's selected tier row: the window installs each name's baseline normal (session_window);
    # without one the tape's 12 minutes cannot warm ATR's 14 and nothing enters.
    strategy.baseline_dollar_volume_per_minute[instrument.id] = Decimal(2_000_000)
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            risk_engine=RiskEngineConfig(
                **risk_limits(
                    instrument.id,
                    max_notional_per_order=allocation.entry_notional * OUTER_BOUND_MULTIPLE,
                )
            ),
        )
    )
    eng.add_venue(
        instrument.id.venue,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(float(entry_cash), USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    eng.add_instrument(instrument)
    closes = [10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.46, 10.00, 9.90, 9.80]
    if liquidity == "thin":
        closes = [
            0.700,
            0.705,
            0.710,
            0.715,
            0.720,
            0.725,
            0.730,
            0.735,
            0.710,
            0.690,
            0.680,
            0.670,
        ]
    volume = {"original": 100_000, "thin": 6842, "liquid": 1_000_000, "cash_falls": 100_000}[
        liquidity
    ]
    # The tape runs on past the wind-down (minute 18), flat at its last close: a gap through the
    # trail deeper than the 0.02 chase floor rests at the floor for FLOOR_WAIT_SECS before it follows
    # the bid (the lead's chase_cap = max(trail, 0.02) and floor release, 2026-11-12).
    closes = closes + [closes[-1]] * 8
    # T1/T2 refuses an entry whose spread floor (2 quoted spreads) exceeds the 0.02 cap, so the
    # sub-dollar tape quotes a tenth of a cent each side rather than a cent (2.8% of 0.70).
    half = 0.01 if closes[0] >= 1 else 0.001
    data = []
    for i, close in enumerate(closes):
        ts_close = ts_start + (i + 1) * minute
        data.append(
            TradeTick(
                instrument.id,
                Price(close, 4),
                Quantity.from_int(volume),
                AggressorSide.BUY,
                TradeId(f"T-{i}"),
                ts_close - 2,
                ts_close - 2,
            )
        )
        data.append(
            QuoteTick(
                instrument.id,
                Price(close - half, 4),
                Price(close + half, 4),
                Quantity.from_int(1_000_000),
                Quantity.from_int(1_000_000),
                ts_close - 1,
                ts_close - 1,
            )
        )
    eng.add_data(data)
    eng.add_strategy(strategy)
    eng.add_actor(ranking)
    eng.add_strategy(cap)
    try:
        eng.run()
        orders = eng.cache.orders(strategy_id=MOVER_ID)
        buys = [order for order in orders if order.side.name == "BUY"]
        assert len(buys) == 1
        buy = buys[0]
        assert buy.price is not None
        price = buy.price.as_decimal()
        # The composition's Mover enters on the fourth bar after the ranking reaches its slot, at that
        # close's ask (one cent over the close); the flow cap reads those four bars' dollar volume.
        [at] = [
            i for i, close in enumerate(closes) if Decimal(str(close)) + Decimal(str(half)) == price
        ]
        flow_dollars = sum(Decimal(str(close)) for close in closes[at - 3 : at + 1]) * volume
        budget_shares = min(budget, entry_cash) // price
        flow_shares = strategy.settings.flow_share * flow_dollars // price
        expected = int(min(budget_shares, flow_shares))
        assert not risk_calls, (
            f"FixedRiskSizer used: {buy.filled_qty} shares instead of the budget's {expected}"
        )
        assert buy.filled_qty.as_decimal() == Decimal(expected)
        assert strategy.settings.max_shares is None
        assert cap.max_position == allocation.entry_notional // start >= Decimal(expected)
        assert buy.filled_qty.as_decimal() * buy.price.as_decimal() > Decimal(500)
        assert not eng.cache.positions_open(strategy_id=MOVER_ID)
        assert strategy.refused == [] and cap.tripped is None
        assert all(order.status.name != "DENIED" for order in orders)
        report = session_window.WindowReport(
            "regular" if part == "regular" else "extended",
            "LLL",
            started="flow-budget-test",
            mover={"run": strategy.record()},
        )
        session_window.write_record(tmp_path, report)
        saved = json.loads((tmp_path / "latest.json").read_text())
        [entry] = saved["mover"]["run"]["entries"]
        sizing = entry["sizing"]
        assert entry["client_order_id"] == buy.client_order_id.value
        assert Decimal(sizing["budget_notional"]) == min(budget, entry_cash)
        assert Decimal(sizing["budget_shares"]) == min(budget, entry_cash) // buy.price.as_decimal()
        assert Decimal(sizing["startup_budget_notional"]) == budget
        assert Decimal(sizing["free_cash"]) == entry_cash
        assert Decimal(sizing["flow_dollars"]) == flow_dollars
        assert Decimal(sizing["flow_shares"]) == (
            strategy.settings.flow_share * flow_dollars // buy.price.as_decimal()
        )
        assert sizing["binding"] == ("flow" if liquidity == "thin" else "budget")
        assert sizing["quantity"] == str(expected)
        if liquidity == "thin":
            assert flow_shares < budget_shares and expected == 1352
    finally:
        eng.dispose()
