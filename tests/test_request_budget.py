"""Request-budget records and offline fast cycles (ADR 0083).

The counter inputs are sanitized RequestCount snapshots, with no account identifiers or credentials.
Contracts read at the pins: NautilusTrader rc8 BacktestEngine.add_venue/add_data and Strategy's limit
orders/cancels (backtest and trading stubs); its Python ExecutionClient command/report hooks
(python/nautilus_trader/live/clients.py); alpaca-py 0.45.0 TradingClient's submit/cancel/query methods
and RESTClient._request retry loop. The real adapter and SDK run against an in-memory HTTP transport.
"""

import asyncio
import importlib
import importlib.util
import json
from collections import Counter
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from alpaca.trading.client import TradingClient
from canon_alpaca import providers
from canon_alpaca.execution import AlpacaExecutionClient
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import send_order_commands_once
from canon_alpaca.providers import AlpacaInstrumentProvider
from canon_alpaca.providers import RequestCount
from canon_alpaca.providers import RequestCounter
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import Clock
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue
from nautilus_trader.model import VenueOrderId
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy
from requests import Response
from requests.adapters import HTTPAdapter

from canon_trading.paper_node import MAX_ORDER_SUBMIT_RATE
from canon_trading.paper_node import OPEN_CHECK_INTERVAL_SECS
from canon_trading.paper_node import POSITION_CHECK_INTERVAL_SECS


def budget_module():
    name = "canon_trading.request_budget"
    assert importlib.util.find_spec(name) is not None, "request budget record is not implemented"
    return importlib.import_module(name)


def count(*, account=1, api="trading", total=30, peak=20, peak_60s=25):
    return RequestCount(
        account, api, total, peak, datetime(2026, 11, 11, tzinfo=UTC), peak_60s, 0, ()
    )


def test_request_record_keeps_both_peaks_and_measures_the_average_over_its_observation():
    budget = budget_module()
    record = budget.request_record([count()], elapsed_seconds=90)
    assert record["limit_per_minute"] == 200
    assert record["minute_buckets_kept"] is False
    assert record["count_scope"] == "process"
    assert record["observation_seconds"] == 90
    assert record["by_api"] == [
        {
            "account": 1,
            "api": "trading",
            "total": 30,
            "average_per_minute": 20.0,
            "peak_minute": 20,
            "peak_minute_at": "2026-11-11T00:00:00+00:00",
            "peak_60s": 25,
            "limit_per_minute": 200,
            "headroom_60s": 175,
            "rate_limited": 0,
        }
    ]


def test_account_budgets_are_separate_and_data_requests_have_no_trading_limit():
    budget = budget_module()
    record = budget.request_record(
        [count(peak_60s=181), count(account=2), count(api="data", total=1000, peak_60s=400)],
        elapsed_seconds=60,
        limit_per_minute=180,
    )
    trading, other, data = record["by_api"]
    assert trading["headroom_60s"] == -1
    assert other["headroom_60s"] == 155
    assert data["average_per_minute"] == 1000
    assert data["limit_per_minute"] is None and data["headroom_60s"] is None
    assert record["limit_per_minute"] == 180


@pytest.mark.parametrize("elapsed", [None, 0])
def test_a_record_without_an_observation_interval_does_not_invent_a_rate(elapsed):
    budget = budget_module()
    record = budget.request_record([count()], elapsed_seconds=elapsed)
    assert record["by_api"][0]["average_per_minute"] is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_a_request_budget_limit_is_a_positive_integer_setting(limit):
    budget = budget_module()
    with pytest.raises(ValueError, match="positive integer"):
        budget.request_record([], elapsed_seconds=60, limit_per_minute=limit)


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf")])
def test_invalid_observation_intervals_are_refused_instead_of_serializing_a_false_rate(elapsed):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        budget_module().request_record([count()], elapsed_seconds=elapsed)


@pytest.mark.parametrize(
    ("state", "eligible"),
    [
        ("unfinalized", False),
        ("running", False),
        ("interrupted", False),
        ("failed", False),
        ("refused", False),
        ("complete", True),
    ],
)
def test_only_a_completed_request_measurement_can_be_used_for_calibration(state, eligible):
    record = budget_module().request_record([count()], elapsed_seconds=60, measurement_status=state)
    assert record["measurement_status"] == state
    assert record["calibration_eligible"] is eligible


@pytest.mark.parametrize("elapsed", [None, 0])
def test_even_a_completed_measurement_needs_an_observation_interval_for_calibration(elapsed):
    record = budget_module().request_record(
        [count()], elapsed_seconds=elapsed, measurement_status="complete"
    )
    assert record["calibration_eligible"] is False


def test_a_plain_counter_snapshot_is_not_assumed_to_be_a_complete_window():
    record = budget_module().request_record([count()], elapsed_seconds=60)
    assert record["measurement_status"] == "unfinalized"
    assert record["calibration_eligible"] is False


T0 = 1_762_790_400_000_000_000
SECOND = 1_000_000_000
USD = Currency.from_str("USD")
VENUE = Venue("ALPACA")


class FastCycle(Strategy):
    """A workload, not a trading rule: buy, sell with C cancel/resubmits, buy back, then end flat.

    The matching engine supplies fills and cancels. A chase waits five seconds before cancel and
    another quote before resubmit, as Mover's one-second step does; eight fit the complete cycle in
    one minute. This deliberately measures transport cost without asserting Mover's entry signals.
    """

    def __init__(self):
        super().__init__()
        self.ids: list[InstrumentId] = []
        self.chases = 0
        self.commands: list[tuple[int, str, Any]] = []
        self.sales: dict = {}
        self.filled: list = []

    def on_start(self):
        for instrument_id in self.ids:
            self.subscribe_quotes(instrument_id)

    def place(self, quote, side, *, resting=False):
        chase = ((quote.ts_init - T0) // SECOND - 3) // 6
        price = (
            Price(Decimal("10.10") - chase * Decimal("0.01"), 2)
            if resting
            else (quote.ask_price if side == OrderSide.BUY else quote.bid_price)
        )
        order = self.order_factory.limit(
            quote.instrument_id, side, Quantity.from_int(1), price, time_in_force=TimeInForce.DAY
        )
        self.commands.append((quote.ts_init, "submit", order))
        self.submit_order(order)
        if side == OrderSide.SELL:
            self.sales[quote.instrument_id] = order

    def on_quote(self, quote):
        second = (quote.ts_init - T0) // SECOND
        if second in (1, 55):
            self.place(quote, OrderSide.BUY)
        elif second == 3:
            self.place(quote, OrderSide.SELL, resting=self.chases > 0)
        elif self.chases and second in range(8, 8 + 6 * self.chases, 6):
            order = self.sales[quote.instrument_id]
            self.commands.append((quote.ts_init, "cancel", order))
            self.cancel_order(order.client_order_id)
        elif self.chases and second in range(9, 9 + 6 * self.chases, 6):
            self.place(quote, OrderSide.SELL, resting=second != 3 + 6 * self.chases)
        elif second == 58:
            self.place(quote, OrderSide.SELL)

    def on_order_filled(self, event):
        self.filled.append(event)


def simulated_cycle(slots, chases):
    """NautilusTrader rc8 BacktestEngine, cash/netting, quotes only, native 50 submits/minute."""
    ids = [InstrumentId.from_str(f"SLOT{i}.ALPACA") for i in range(slots)]
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("BUDGET-001"),
            risk_engine=RiskEngineConfig(max_order_submit_rate=MAX_ORDER_SUBMIT_RATE),
        )
    )
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
        bar_execution=False,
    )
    for instrument_id in ids:
        eng.add_instrument(
            Equity(
                instrument_id=instrument_id,
                raw_symbol=Symbol(instrument_id.symbol.value),
                currency=USD,
                price_precision=2,
                price_increment=Price.from_str("0.01"),
                ts_event=0,
                ts_init=0,
                info={"tradable": True},
            )
        )
    strategy = FastCycle()
    strategy.ids, strategy.chases = ids, chases
    eng.add_strategy(strategy)
    eng.add_data(
        [
            QuoteTick(
                instrument_id,
                Price.from_str("10.00"),
                Price.from_str("10.02"),
                Quantity.from_int(100),
                Quantity.from_int(100),
                T0 + second * SECOND,
                T0 + second * SECOND,
            )
            for second in range(61)
            for instrument_id in ids
        ]
    )
    eng.run()
    return eng, strategy


def measured_requests(monkeypatch, eng, strategy, *, extended, get_retries):
    """Replay the simulated commands through the real adapter and alpaca-py down to HTTPAdapter.send.

    The network boundary and unbound event sink are replaced, and the normal counter is test-local
    on the simulation clock. The transport never reads auth headers, returns hand-built order JSON,
    and counts every attempt; GETs can fail 504 three times.
    """
    trading = TradingClient(
        api_key="offline-placeholder", secret_key="offline-placeholder", paper=True
    )
    with_http_timeout(trading)
    send_order_commands_once(trading)
    # SDK retry waits are zero in simulated time; attempts still run through its retry loop.
    trading._retry_wait = 0
    hits: list[tuple[int, str, str]] = []
    by_client_id: dict = {}
    retries: Counter = Counter()
    now = 0
    # The normal provider counter is replaced only for this test, as test_request_counter's fixture
    # does; synthetic attempts never consume a later window test's process budget.
    counter = RequestCounter(clock=lambda: T0 / SECOND + now)
    monkeypatch.setattr(providers, "REQUESTS", counter)

    def transport(_adapter, request, **_kwargs):
        path = urlsplit(request.url).path
        assert urlsplit(request.url).hostname == "paper-api.alpaca.markets"
        hits.append((now, request.method, path))
        response = Response()
        response.status_code = 200
        response.request = request
        payload: Any = []
        if request.method == "GET" and retries[(now, path)] < get_retries:
            retries[(now, path)] += 1
            response.status_code = 504
            payload = {"code": 504, "message": "simulated retry"}
        elif request.method == "POST":
            body = json.loads(request.body)
            assert body["type"] == "limit" and body["time_in_force"] == "day"
            assert body.get("extended_hours", False) is extended
            order_id = str(UUID(int=len(by_client_id) + 1))
            by_client_id[body["client_order_id"]] = order_id
            payload = {
                **body,
                "id": order_id,
                "asset_id": str(UUID(int=999)),
                "asset_class": "us_equity",
                "created_at": "2025-11-10T16:00:00Z",
                "updated_at": "2025-11-10T16:00:00Z",
                "submitted_at": "2025-11-10T16:00:00Z",
                "filled_qty": "0",
                "status": "accepted",
                "order_class": "simple",
                "extended_hours": extended,
            }
        elif request.method == "DELETE":
            response.status_code = 204
            payload = None
        response._content = json.dumps(payload).encode() if payload is not None else b""
        return response

    monkeypatch.setattr(HTTPAdapter, "send", transport)
    provider = AlpacaInstrumentProvider()
    for instrument in eng.cache.instruments():
        provider.add(instrument)
    clock = Clock.new_test()
    client = AlpacaExecutionClient(
        name="BUDGET",
        config=AlpacaExecutionClientConfig(),
        cache=cast(Any, eng.cache),
        clock=clock,
        trader_id=eng.trader_id,
        instrument_provider=provider,
    )
    client._trading = cast(Any, trading)
    # Matching/fills were checked by the backtest; no live runtime owns this client's event output.
    client._output = cast(Any, SimpleNamespace(order_submitted=lambda *args: None))
    schedule = list(strategy.commands)
    schedule.extend(
        (T0 + int(second * SECOND), "open", None)
        for second in range(0, 60, int(OPEN_CHECK_INTERVAL_SECS))
    )
    schedule.extend(
        (T0 + int(second * SECOND), "position", None)
        for second in range(0, 60, int(POSITION_CHECK_INTERVAL_SECS))
    )

    async def replay():
        nonlocal now
        for at, kind, order in sorted(schedule, key=lambda item: item[0]):
            now = (at - T0) // SECOND
            clock.set_time(at)
            if kind == "submit":
                await client._submit_order(
                    cast(Any, SimpleNamespace(order=order, params={"extended_hours": extended}))
                )
            elif kind == "cancel":
                await client._cancel_order(
                    cast(
                        Any,
                        SimpleNamespace(
                            client_order_id=order.client_order_id,
                            venue_order_id=VenueOrderId(by_client_id[order.client_order_id.value]),
                        ),
                    )
                )
            elif kind == "open":
                assert (
                    await client._order_status_reports(
                        cast(Any, SimpleNamespace(instrument_id=None, open_only=True))
                    )
                    == []
                )
            else:
                await client._position_status_reports(
                    cast(Any, SimpleNamespace(instrument_id=None))
                )

    try:
        asyncio.run(replay())
    finally:
        trading._session.close()
    measured = counter.counts()
    assert len(measured) == 1 and measured[0].api == "trading"
    assert measured[0].total == len(hits)
    assert measured[0].peak == measured[0].peak_60s == len(hits)
    return hits


def test_synthetic_requests_do_not_change_the_process_counter_used_by_other_windows(monkeypatch):
    original = providers.REQUESTS
    before = original.counts()
    eng, strategy = simulated_cycle(1, 0)
    try:
        measured_requests(monkeypatch, eng, strategy, extended=False, get_retries=0)
        assert original.counts() == before, (
            "synthetic traffic must not exhaust another test's budget"
        )
    finally:
        eng.dispose()


@pytest.mark.parametrize("slots", [1, 2, 3, 4])
@pytest.mark.parametrize("chases", [0, 8])
@pytest.mark.parametrize("get_retries", [0, 3])
@pytest.mark.parametrize("extended", [False, True])
def test_fast_cycle_counts_actual_adapter_attempts_per_order_and_minute(
    monkeypatch, slots, chases, get_retries, extended
):
    eng, strategy = simulated_cycle(slots, chases)
    try:
        for instrument_id in strategy.ids:
            fills = [e for e in strategy.filled if e.instrument_id == instrument_id]
            assert [e.order_side for e in fills] == [
                OrderSide.BUY,
                OrderSide.SELL,
                OrderSide.BUY,
                OrderSide.SELL,
            ]
            assert all(T0 <= e.ts_event < T0 + 60 * SECOND for e in fills)
        assert not eng.cache.positions_open()
        hits = measured_requests(
            monkeypatch, eng, strategy, extended=extended, get_retries=get_retries
        )
        methods = Counter(method for _, method, _ in hits)
        assert methods["POST"] == slots * (4 + chases)
        assert methods["DELETE"] == slots * chases
        assert methods["GET"] == 7 * (1 + get_retries)
        minutes = Counter(second // 60 for second, _, _ in hits)
        peak = max(minutes.values())
        expected = 7 * (1 + get_retries) + slots * (4 + 2 * chases)
        assert peak == len(hits) == expected
        record = budget_module().request_record(
            [count(total=len(hits), peak=peak, peak_60s=peak)], elapsed_seconds=60
        )
        assert record["by_api"][0]["average_per_minute"] == expected
        assert record["by_api"][0]["headroom_60s"] == 200 - expected
        # Neither the 200 planning budget nor the window's 180 failure ceiling is loosened.
        assert peak < 180
    finally:
        eng.dispose()


def test_a_machine_sleep_counts_in_the_observation_interval(monkeypatch) -> None:
    """The adapter counts requests on the wall clock; a Mac's monotonic clock stops while it sleeps
    (`session_window._run_until`, 2026-11-11). Twenty slept minutes are twenty minutes of observation,
    so the average is not inflated by the sleep."""
    from canon_trading import request_budget

    start = 1_800_000_000.0
    asleep = SimpleNamespace(time=lambda: start + 1200.0, monotonic=lambda: start)
    monkeypatch.setattr(request_budget, "time", asleep)
    monkeypatch.setattr(request_budget, "_OBSERVATION_STARTED", start)
    assert request_budget.observation_seconds() == 1200.0
