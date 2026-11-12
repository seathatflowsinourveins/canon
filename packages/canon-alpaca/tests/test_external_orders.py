"""Sell-to-close intent and external order reports, against the pinned clients on an offline node.

Contracts: NautilusTrader's reduce-only execution contract (adapters.md at d53849b216e2) and
Alpaca's pinned OrderRequest.position_intent, read 2026-11-12.
"""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from alpaca.trading.enums import PositionIntent
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import TradeUpdate
from canon_alpaca.execution import AlpacaExecutionClientConfig
from canon_alpaca.execution import order_status_report
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import FillReport
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType

from . import test_execution_client as execution_tests
from .test_execution_client import AAPL
from .test_execution_client import Trader
from .test_execution_client import build_node
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - pytest fixture
from .test_execution_client import logged
from .test_execution_client import provider_config
from .test_execution_client import run
from .test_execution_client import run_with_client
from .test_execution_client import until_idle
from .test_execution_client import venue as venue  # noqa: PLC0414 - pytest fixture


class IdleTrader(Trader):
    """Keep the node up while a test submits orders and reads the resulting cache."""

    def __init__(self) -> None:
        super().__init__()
        self.stopped_for = "the test owns shutdown"

    def on_start(self) -> None:
        self.started = True
        self.subscribe_quotes(AAPL)

    def on_quote(self, quote) -> None:
        pass


@pytest.fixture
def configuration_reads(venue, monkeypatch):
    """Record a configuration read if one is attempted; the finished client must never call this."""
    reads = []

    def read():
        reads.append("get_account_configurations")
        return AccountConfiguration.model_validate(
            {
                "fractional_trading": True,
                "max_margin_multiplier": "1",
                "no_shorting": True,
                "suspend_trade": False,
                "trade_confirm_email": "none",
                "ptp_no_exception_entry": False,
            }
        )

    monkeypatch.setattr(venue.trading, "get_account_configurations", read, raising=False)
    return reads


def make_order(strategy, kind: str, side: OrderSide, reduce_only: bool, client_order_id=None):
    fields = {
        "instrument_id": AAPL,
        "order_side": side,
        "quantity": Quantity.from_int(2),
        "time_in_force": TimeInForce.GTC,
        "reduce_only": reduce_only,
        "client_order_id": client_order_id,
    }
    if kind in ("limit", "stop_limit"):
        fields["price"] = Price.from_str("150.00")
    if kind in ("stop_market", "stop_limit"):
        fields["trigger_price"] = Price.from_str("140.00")
    return getattr(strategy.order_factory, kind)(**fields)


@pytest.mark.parametrize("kind", ["limit", "stop_market", "stop_limit"])
def test_reduce_only_sell_carries_sell_to_close_without_reading_configuration(
    venue, kind, configuration_reads, reduce_only_sells
) -> None:
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        reduced = make_order(strategy, kind, OrderSide.SELL, True)
        ordinary = make_order(strategy, kind, OrderSide.SELL, False, reduced.client_order_id)
        await until_idle(client, venue)  # let the subscribed quote reach the risk engine
        strategy.submit_order(reduced)
        await until_idle(client, venue)
        expected = client._order_request(ordinary).to_request_fields()
        expected["position_intent"] = "sell_to_close"
        assert len(venue.trading.submitted) == 1
        request = venue.trading.submitted[0]
        assert request.position_intent == PositionIntent.SELL_TO_CLOSE
        assert request.to_request_fields() == expected
        assert client.cache.order(reduced.client_order_id).status == OrderStatus.ACCEPTED

    run_with_client(body, strategy)
    assert configuration_reads == []


def test_a_resting_reduce_only_sell_cannot_be_modified_to_a_larger_quantity(
    venue, capfd, reduce_only_sells
) -> None:
    """Alpaca's replace carries no position intent, and what it checks when a `sell_to_close` order
    grows is not shown on paper, so the send-or-reject contract is kept by refusing the larger
    quantity before transport; a smaller one, which cannot open a short, is sent."""
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        reduced = make_order(strategy, "limit", OrderSide.SELL, True)
        await until_idle(client, venue)
        strategy.submit_order(reduced)
        await until_idle(client, venue)
        strategy.modify_order(reduced.client_order_id, quantity=Quantity.from_int(3))
        await until_idle(client, venue)
        assert venue.trading.replace_requests == []
        strategy.modify_order(reduced.client_order_id, quantity=Quantity.from_int(1))
        await until_idle(client, venue)
        assert len(venue.trading.replace_requests) == 1

    run_with_client(body, strategy)
    assert "raising the quantity of a reduce-only SELL is not supported" in logged(capfd)


def test_a_reduce_only_market_sell_carries_the_intent_once_the_scope_names_the_market_order(
    venue, configuration_reads, monkeypatch
) -> None:
    """The one-line change ADR 0111 promises for the market order's proof, on the reduce-only path."""
    monkeypatch.setattr(
        execution_tests,
        "exec_config",
        scoped_config(
            reduce_only_sells=True,
            sell_to_close_order_types=frozenset(
                {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.STOP_LIMIT}
            ),
        ),
    )
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        order = strategy.order_factory.market(
            AAPL, OrderSide.SELL, Quantity.from_int(2), reduce_only=True
        )
        await until_idle(client, venue)  # let the subscribed quote reach the risk engine
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert len(venue.trading.submitted) == 1
        assert venue.trading.submitted[0].position_intent == PositionIntent.SELL_TO_CLOSE

    run_with_client(body, strategy)
    assert configuration_reads == []


def test_an_empty_scope_denies_every_reduce_only_sell_before_transport(
    venue, capfd, monkeypatch
) -> None:
    monkeypatch.setattr(
        execution_tests,
        "exec_config",
        scoped_config(reduce_only_sells=True, sell_to_close_order_types=frozenset()),
    )
    strategy = IdleTrader()

    async def body(client) -> None:
        order = make_order(strategy, "limit", OrderSide.SELL, True)
        await until_idle(client, venue)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert venue.trading.submitted == []

    run_with_client(body, strategy)
    assert "reduce-only LIMIT SELL is not supported" in logged(capfd)


def test_the_config_serialises_with_its_scope_as_the_order_types_names() -> None:
    """Instance fields are serialised with the config (the adapter guide, "Define adapter
    configuration"), and the pinned serialiser takes no frozenset: with the scope kept as one, `json()`
    raised for every config, the default one included, which on `main` it does not."""
    default = json.loads(AlpacaExecutionClientConfig().json())
    assert default["sell_to_close_order_types"] == ["LIMIT", "STOP_LIMIT", "STOP_MARKET"]
    assert default["reduce_only_sells"] is False
    wider = AlpacaExecutionClientConfig(
        sell_to_close_order_types={OrderType.MARKET, OrderType.LIMIT}
    )
    assert json.loads(wider.json())["sell_to_close_order_types"] == ["LIMIT", "MARKET"]
    assert (
        json.loads(AlpacaExecutionClientConfig(sell_to_close_order_types=()).json())[
            "sell_to_close_order_types"
        ]
        == []
    )


def test_reduce_only_buy_has_no_enforcing_instruction(
    venue, configuration_reads, capfd, reduce_only_sells
) -> None:
    strategy = IdleTrader()

    async def body(client) -> None:
        order = make_order(strategy, "limit", OrderSide.BUY, True)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert venue.trading.submitted == []

    run_with_client(body, strategy)
    assert "reduce-only BUY is not supported: Alpaca has no instruction that enforces it" in logged(
        capfd
    )
    assert configuration_reads == []


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("kind", ["limit", "market", "stop_market", "stop_limit"])
def test_plain_orders_are_unchanged_and_make_no_configuration_read(
    venue, kind, side, configuration_reads
) -> None:
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        order = make_order(strategy, kind, side, False)
        expected = client._order_request(order)
        await until_idle(client, venue)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert venue.trading.submitted == [expected]
        assert client.cache.order(order.client_order_id).status == OrderStatus.ACCEPTED
        await client._query_account(SimpleNamespace(account_id=None))
        assert "position_intent" not in expected.to_request_fields()

    run_with_client(body, strategy)
    assert configuration_reads == []


def test_connect_makes_no_account_configuration_read(venue, configuration_reads) -> None:
    async def body(client) -> None:
        assert client.is_connected

    run_with_client(body)
    assert configuration_reads == []


@pytest.fixture
def long_exit_intent(monkeypatch):
    def enable(enabled: bool) -> None:
        def config():
            return AlpacaExecutionClientConfig(
                instrument_provider=provider_config(), sell_to_close_on_long_exits=enabled
            )

        monkeypatch.setattr(execution_tests, "exec_config", config)

    return enable


def test_long_exit_intent_setting_is_off_by_default_and_boolean() -> None:
    assert AlpacaExecutionClientConfig().sell_to_close_on_long_exits is False
    assert (
        AlpacaExecutionClientConfig(sell_to_close_on_long_exits=True).sell_to_close_on_long_exits
        is True
    )
    with pytest.raises(TypeError, match="sell_to_close_on_long_exits must be a bool"):
        AlpacaExecutionClientConfig(sell_to_close_on_long_exits=cast("Any", "true"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["limit", "market", "stop_market", "stop_limit"])
def test_plain_long_closing_sells_use_the_opt_in_intent_only_when_enabled(
    venue, kind, enabled, long_exit_intent, configuration_reads
) -> None:
    """A market sell is outside the default scope (ADR 0111: the field is shown to be enforced for a
    limit, a stop and a stop-limit; a market order is owed), so it stays a plain sell either way."""
    long_exit_intent(enabled)
    strategy = IdleTrader()

    async def body(client) -> None:
        opening = make_order(strategy, "limit", OrderSide.BUY, False)
        strategy.submit_order(opening)
        await until_idle(client, venue)
        assert not venue.trading.submitted[0].position_intent
        assert len(client.cache.positions_open()) == 1
        venue.trading.script = "accept"
        closing = make_order(strategy, kind, OrderSide.SELL, False)
        expected = client._order_request(closing).to_request_fields()
        if enabled and kind != "market":
            expected["position_intent"] = "sell_to_close"
        strategy.submit_order(closing)
        await until_idle(client, venue)
        assert len(venue.trading.submitted) == 2
        assert venue.trading.submitted[-1].to_request_fields() == expected
        assert not client.cache.order(closing.client_order_id).is_reduce_only

    run_with_client(body, strategy)
    assert configuration_reads == []


def scoped_config(**settings: Any):
    def config():
        return AlpacaExecutionClientConfig(instrument_provider=provider_config(), **settings)

    return config


@pytest.mark.parametrize("kind", ["limit", "market", "stop_market", "stop_limit"])
def test_a_market_sell_of_a_cached_long_carries_the_intent_only_when_the_scope_names_it(
    venue, kind, monkeypatch
) -> None:
    """The scope is a setting, so the market order's proof turns it on without a code change."""
    monkeypatch.setattr(
        execution_tests,
        "exec_config",
        scoped_config(
            sell_to_close_on_long_exits=True,
            sell_to_close_order_types=frozenset(
                {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.STOP_LIMIT}
            ),
        ),
    )
    strategy = IdleTrader()

    async def body(client) -> None:
        strategy.submit_order(make_order(strategy, "limit", OrderSide.BUY, False))
        await until_idle(client, venue)
        venue.trading.script = "accept"
        strategy.submit_order(make_order(strategy, kind, OrderSide.SELL, False))
        await until_idle(client, venue)
        assert venue.trading.submitted[-1].position_intent == PositionIntent.SELL_TO_CLOSE

    run_with_client(body, strategy)


def test_a_reduce_only_market_sell_is_denied_before_transport_while_market_is_outside_the_scope(
    venue, reduce_only_sells
) -> None:
    """Upstream's contract: send an instruction that enforces the flag or reject before transport.
    For a market order the enforcement is not shown, so the flag is never sent on one unproven."""
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        await until_idle(client, venue)
        order = make_order(strategy, "market", OrderSide.SELL, True)
        strategy.submit_order(order)
        await until_idle(client, venue)
        denied = [event for event in strategy.events if type(event).__name__ == "OrderDenied"]
        assert len(denied) == 1
        assert denied[0].reason == (
            "reduce-only MARKET SELL is not supported: sell_to_close is not shown to be enforced "
            "for that order type"
        )
        assert venue.trading.submitted == []

    run_with_client(body, strategy)


def test_the_scope_defaults_to_the_three_proven_types_and_refuses_what_the_adapter_cannot_send() -> (
    None
):
    proven = frozenset({OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.STOP_LIMIT})
    assert frozenset(AlpacaExecutionClientConfig().sell_to_close_order_types) == proven
    wider = AlpacaExecutionClientConfig(sell_to_close_order_types=[OrderType.MARKET, *proven])
    assert frozenset(wider.sell_to_close_order_types) == proven | {OrderType.MARKET}
    assert AlpacaExecutionClientConfig(sell_to_close_order_types=()).sell_to_close_order_types == ()
    with pytest.raises(ValueError, match="TRAILING_STOP_MARKET"):
        AlpacaExecutionClientConfig(sell_to_close_order_types=[OrderType.TRAILING_STOP_MARKET])
    for value in ("LIMIT", ["LIMIT"], 3):
        with pytest.raises(TypeError, match="sell_to_close_order_types"):
            AlpacaExecutionClientConfig(sell_to_close_order_types=cast("Any", value))


def test_long_exit_intent_does_not_mark_a_plain_sell_from_flat(
    venue, long_exit_intent, configuration_reads
) -> None:
    long_exit_intent(True)
    venue.trading.script = "accept"
    strategy = IdleTrader()

    async def body(client) -> None:
        assert client.cache.positions_open() == []
        order = make_order(strategy, "limit", OrderSide.SELL, False)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert venue.trading.submitted == [client._order_request(order)]
        assert "position_intent" not in venue.trading.submitted[0].to_request_fields()

    run_with_client(body, strategy)
    assert configuration_reads == []


@pytest.fixture
def external_reports(monkeypatch) -> None:
    """Turn on the one config field; keep the ordinary windows' default covered separately."""

    def config():
        return AlpacaExecutionClientConfig(
            instrument_provider=provider_config(), report_external_orders=True
        )

    monkeypatch.setattr(execution_tests, "exec_config", config)


def outside_update(alpaca, event: str, filled: int = 0, fill_qty: int | None = None, **fields):
    status = {
        "new": "new",
        "partial_fill": "partially_filled",
        "fill": "filled",
        "canceled": "canceled",
    }[event]
    raw = alpaca.alpaca_order(
        client_order_id="placed-elsewhere",
        status=status,
        filled_qty=str(filled),
        filled_avg_price="150.1234567" if filled else None,
        **fields,
    )
    return TradeUpdate.model_validate(
        {
            "event": event,
            "order": raw,
            "timestamp": raw["updated_at"],
            "qty": fill_qty,
            "price": "150.1234567" if fill_qty is not None else None,
            "execution_id": str(uuid.uuid4()) if fill_qty is not None else None,
        }
    )


def record_reports_and_calls(client, monkeypatch):
    reports, calls = [], []
    handle, call = client._handle_report, client._call

    def record(report, fills=None):
        reports.append((report, fills))
        handle(report, fills)

    async def counted(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return await call(fn, *args, **kwargs)

    monkeypatch.setattr(client, "_handle_report", record)
    monkeypatch.setattr(client, "_call", counted)
    return reports, calls


def test_external_reporting_config_is_off_by_default_and_accepts_only_a_boolean() -> None:
    assert AlpacaExecutionClientConfig().report_external_orders is False
    assert AlpacaExecutionClientConfig(report_external_orders=True).report_external_orders is True
    with pytest.raises(TypeError, match="report_external_orders must be a bool"):
        AlpacaExecutionClientConfig(report_external_orders=cast("Any", "true"))


def test_external_fill_is_still_dropped_with_the_default_config(
    venue, alpaca, monkeypatch, capfd
) -> None:
    update = outside_update(alpaca, "fill", 2, 2)

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        assert reports == [] and calls == []
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None

    run_with_client(body, IdleTrader())
    text = logged(capfd)
    line = next(line for line in text.splitlines() if "which this node does not hold" in line)
    assert "AAPL" in line and "fill" in line
    assert str(update.order.id) not in line


def test_external_market_fill_reports_status_and_its_real_trade_id_without_a_request(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    update = outside_update(
        alpaca, "fill", 2, 2, type="market", order_type="market", limit_price=None
    )

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        assert calls == []
        assert len(reports) == 1
        status, fills = reports[0]
        assert status.order_status == OrderStatus.FILLED
        assert len(fills) == 1
        assert fills[0].trade_id.value == str(update.execution_id)
        assert fills[0].last_px.as_decimal() == Decimal("150.1234567")
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.FILLED and order.filled_qty.as_double() == 2
        assert [str(trade_id) for trade_id in order.trade_ids] == [str(update.execution_id)]

    run_with_client(body, IdleTrader())


@pytest.mark.parametrize("first", ["new", "partial_fill"])
def test_two_external_updates_before_creation_keep_every_fill_once(
    venue, alpaca, monkeypatch, external_reports, first
) -> None:
    initial = outside_update(
        alpaca, first, 1 if first == "partial_fill" else 0, 1 if first == "partial_fill" else None
    )
    final = outside_update(alpaca, "fill", 2, 1 if first == "partial_fill" else 2)

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(initial)
        # No yield to the node between the two: both enter the client before the engine creates it.
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None
        await client._apply_trade_update(final)
        await until_idle(client, venue)
        assert len(reports) == 2 and calls == []
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.FILLED and order.filled_qty.as_double() == 2
        expected = {str(final.execution_id)}
        if first == "partial_fill":
            expected.add(str(initial.execution_id))
        assert {str(trade_id) for trade_id in order.trade_ids} == expected

    run_with_client(body, IdleTrader())


def test_two_external_partial_fills_before_creation_then_a_normal_fill(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    updates = [outside_update(alpaca, "partial_fill", n, 1, qty="3") for n in (1, 2)]

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        for update in updates:
            await client._apply_trade_update(update)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.PARTIALLY_FILLED and order.filled_qty.as_double() == 2
        assert len(reports) == 2
        final = outside_update(alpaca, "fill", 3, 1, qty="3")
        await client._apply_trade_update(final)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.FILLED and order.filled_qty.as_double() == 3
        assert len(order.trade_ids) == 3 and len(reports) == 2 and calls == []

    run_with_client(body, IdleTrader())


def test_external_new_then_cancel_before_creation_reports_no_fill(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(outside_update(alpaca, "new"))
        await client._apply_trade_update(outside_update(alpaca, "canceled"))
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.CANCELED and order.filled_qty.as_double() == 0
        assert len(reports) == 2 and calls == []
        assert all(not fills for _, fills in reports)

    run_with_client(body, IdleTrader())


@pytest.mark.parametrize("before_creation", [True, False])
def test_duplicate_external_fill_is_applied_once(
    venue, alpaca, monkeypatch, external_reports, before_creation
) -> None:
    update = outside_update(alpaca, "fill", 2, 2)

    async def body(client) -> None:
        _, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        if not before_creation:
            await until_idle(client, venue)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.FILLED and order.filled_qty.as_double() == 2
        assert [str(trade_id) for trade_id in order.trade_ids] == [str(update.execution_id)]
        assert calls == []

    run_with_client(body, IdleTrader())


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("type", None, "order type None"),
        ("status", "suspended", "status suspended"),
        ("client_order_id", "", "client order ID NautilusTrader cannot hold"),
        ("client_order_id", "订单-1", "client order ID NautilusTrader cannot hold"),
    ],
)
def test_an_external_update_that_cannot_be_mapped_is_named_and_not_reported(
    venue, alpaca, monkeypatch, external_reports, capfd, field, value, reason
) -> None:
    update = outside_update(alpaca, "new")
    order = update.order.model_copy(update={field: value})
    if field == "status":
        from alpaca.trading.enums import OrderStatus as AlpacaStatus

        order = update.order.model_copy(update={field: AlpacaStatus(value)})
    update = update.model_copy(update={"order": order})

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        assert reports == [] and calls == []

    run_with_client(body, IdleTrader())
    assert reason in logged(capfd)


def test_incomplete_external_stream_fills_are_left_to_reconciliation_without_inference_or_new_reads(
    venue, alpaca, monkeypatch, external_reports, capfd
) -> None:
    # This is the first update observed, but it accounts for only one of the two filled shares.
    update = outside_update(alpaca, "partial_fill", 2, 1, qty="3")

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        assert reports == [] and calls == []
        assert client._external_fills == {}
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None

    run_with_client(body, IdleTrader())
    assert "left to reconciliation" in logged(capfd)


def test_a_claimed_external_market_fill_opens_the_strategy_position_immediately(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    strategy = IdleTrader()
    update = outside_update(
        alpaca, "fill", 2, 2, type="market", order_type="market", limit_price=None
    )

    async def body(client) -> None:
        strategy.set_external_order_instrument_ids([AAPL])
        _, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.strategy_id == strategy.strategy_id
        assert not order.is_reduce_only
        assert strategy.filled == 2
        positions = client.cache.positions_open()
        assert len(positions) == 1 and positions[0].quantity.as_double() == 2
        assert positions[0].strategy_id == strategy.strategy_id and calls == []

    run_with_client(body, strategy)


def test_an_external_fill_without_a_trade_id_never_infers_a_fill(
    venue, alpaca, monkeypatch, external_reports, capfd
) -> None:
    update = outside_update(alpaca, "fill", 2, 2).model_copy(update={"execution_id": None})

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        await until_idle(client, venue)
        assert reports == [] and calls == []
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None

    run_with_client(body, IdleTrader())
    assert "no execution_id to use as the trade ID" in logged(capfd)


def test_external_fills_across_a_replace_before_creation_keep_their_trade_ids(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    initial = outside_update(alpaca, "partial_fill", 1, 1)
    final = outside_update(
        alpaca,
        "fill",
        2,
        1,
        id=str(uuid.uuid4()),
        replaces=str(initial.order.id),
    )
    final = final.model_copy(
        update={"order": final.order.model_copy(update={"client_order_id": "alpaca-replacement"})}
    )

    async def body(client) -> None:
        _, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(initial)
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None
        await client._apply_trade_update(final)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert order.status == OrderStatus.FILLED and order.filled_qty.as_double() == 2
        assert {str(trade_id) for trade_id in order.trade_ids} == {
            str(initial.execution_id),
            str(final.execution_id),
        }
        assert calls == []

    run_with_client(body, IdleTrader())


def trailing_order(alpaca, field: str, stop_price: str | None = "147.25"):
    return alpaca.alpaca_order(
        client_order_id="outside-trailing-stop",
        type="trailing_stop",
        order_type="trailing_stop",
        side="sell",
        limit_price=None,
        stop_price=stop_price,
        trail_price="1.25" if field == "trail_price" else None,
        trail_percent="1.25" if field == "trail_percent" else None,
        status="new",
    )


@pytest.mark.parametrize(
    "field,offset_type,offset",
    [
        ("trail_price", TrailingOffsetType.PRICE, Decimal("1.25")),
        ("trail_percent", TrailingOffsetType.BASIS_POINTS, Decimal(125)),
    ],
)
def test_open_trailing_stop_report_keeps_the_offset_and_current_trigger(
    alpaca, field, offset_type, offset
) -> None:
    order = AlpacaOrder(**trailing_order(alpaca, field))
    with pytest.raises(ValueError, match="is open with order type trailing_stop"):
        order_status_report(order, 42)  # not mapped unless asked
    report = order_status_report(order, 42, trailing=True)
    assert report is not None
    assert report.order_type == OrderType.TRAILING_STOP_MARKET
    assert report.trailing_offset == offset and report.trailing_offset_type == offset_type
    assert report.trigger_price == Price.from_str("147.25")
    assert report.trigger_type == TriggerType.DEFAULT
    assert report.order_side == OrderSide.SELL and report.order_status == OrderStatus.ACCEPTED


@pytest.fixture
def trailing_reports(monkeypatch) -> None:
    def config():
        return AlpacaExecutionClientConfig(
            instrument_provider=provider_config(), report_external_trailing_stops=True
        )

    monkeypatch.setattr(execution_tests, "exec_config", config)


def test_the_trailing_stop_setting_is_off_by_default_and_boolean() -> None:
    assert AlpacaExecutionClientConfig().report_external_trailing_stops is False
    assert (
        AlpacaExecutionClientConfig(
            report_external_trailing_stops=True
        ).report_external_trailing_stops
        is True
    )
    with pytest.raises(TypeError, match="report_external_trailing_stops must be a bool"):
        AlpacaExecutionClientConfig(report_external_trailing_stops=cast("Any", 1))


@pytest.mark.parametrize("field", ["trail_price", "trail_percent"])
@pytest.mark.parametrize("stop_price", ["147.25", None])
def test_startup_materializes_an_external_trailing_stop(
    venue, alpaca, field, stop_price, trailing_reports
) -> None:
    raw = trailing_order(alpaca, field, stop_price)
    venue.trading.orders = [raw]
    node = build_node(execution_tests.trader(submit=False))

    run(node)

    order = node.cache.order(ClientOrderId(raw["client_order_id"]))
    assert order is not None and order.order_type == OrderType.TRAILING_STOP_MARKET
    assert order.status == OrderStatus.ACCEPTED
    assert not order.is_emulated
    assert order.trailing_offset == Decimal("1.25" if field == "trail_price" else "125")
    assert order.trailing_offset_type == (
        TrailingOffsetType.PRICE if field == "trail_price" else TrailingOffsetType.BASIS_POINTS
    )
    assert order.trigger_price == (Price.from_str(stop_price) if stop_price is not None else None)
    assert venue.trading.submitted == []


def test_submitting_a_trailing_stop_is_still_refused_before_alpaca(venue, capfd) -> None:
    strategy = execution_tests.trader(order_type="trailing")

    run(build_node(strategy))

    assert strategy.names() == ["OrderDenied"]
    assert venue.trading.submitted == []
    assert "order type TRAILING_STOP_MARKET is not supported" in logged(capfd)


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"type": None, "order_type": None}, "order type None"),
        ({"order_class": "bracket"}, "order class bracket"),
        ({"order_class": "oco"}, "order class oco"),
        ({"order_class": "oto"}, "order class oto"),
        ({"order_class": "mleg"}, "order class mleg"),
        ({"time_in_force": "opg"}, "time in force opg"),
        ({"time_in_force": "cls"}, "time in force cls"),
        ({"time_in_force": "ioc"}, "time in force ioc"),
        ({"time_in_force": "fok"}, "time in force fok"),
    ],
)
def test_other_unmapped_open_order_fields_still_fail_by_name(alpaca, fields, reason) -> None:
    raw = alpaca.alpaca_order(status="new", **fields)
    with pytest.raises(ValueError, match=f"is open with {reason}"):
        order_status_report(AlpacaOrder(**raw), 0)


@pytest.mark.parametrize(
    "fields,reason",
    [
        (
            {"trail_price": None, "trail_percent": None},
            "exactly one of trail_price or trail_percent",
        ),
        ({"trail_price": "1", "trail_percent": "1"}, "exactly one of trail_price or trail_percent"),
        ({"trail_price": "NaN"}, "trail_price.*is not a number"),
        ({"trail_price": "0"}, "trail_price.*must be positive"),
    ],
)
def test_an_open_trailing_stop_with_unrepresentable_offset_still_fails_by_name(
    alpaca, fields, reason
) -> None:
    raw = trailing_order(alpaca, "trail_price") | fields
    with pytest.raises(ValueError, match=reason):
        order_status_report(AlpacaOrder(**raw), 0, trailing=True)


# High review, one fix round: every test below was run against the preceding implementation.


@pytest.fixture
def reduce_only_sells(monkeypatch) -> None:
    def config():
        return AlpacaExecutionClientConfig(
            instrument_provider=provider_config(), reduce_only_sells=True
        )

    monkeypatch.setattr(execution_tests, "exec_config", config)


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("kind", ["limit", "market", "stop_market", "stop_limit"])
def test_fix_round_gate_defaults_to_the_existing_denial(venue, side, kind) -> None:
    strategy = IdleTrader()
    venue.trading.script = "accept"

    async def body(client) -> None:
        await until_idle(client, venue)
        order = make_order(strategy, kind, side, True)
        strategy.submit_order(order)
        await until_idle(client, venue)
        denied = [event for event in strategy.events if type(event).__name__ == "OrderDenied"]
        assert len(denied) == 1 and denied[0].reason == "reduce-only is not supported"
        assert client.cache.order(order.client_order_id).status == OrderStatus.DENIED
        assert venue.trading.submitted == []

    run_with_client(body, strategy)


def test_fix_round_gate_is_boolean_and_the_unknown_setting_names_all_three() -> None:
    assert AlpacaExecutionClientConfig().reduce_only_sells is False
    enabled = AlpacaExecutionClientConfig(reduce_only_sells=True)
    assert enabled.reduce_only_sells is True and enabled.sell_to_close_on_long_exits is False
    for value in ("true", 1, None):
        with pytest.raises(TypeError, match="reduce_only_sells must be a bool"):
            AlpacaExecutionClientConfig(reduce_only_sells=cast("Any", value))
    with pytest.raises(TypeError) as refused:
        AlpacaExecutionClientConfig(feed="sip")
    for field in (
        "report_external_orders",
        "sell_to_close_on_long_exits",
        "reduce_only_sells",
        "sell_to_close_order_types",
        "report_external_trailing_stops",
    ):
        assert field in str(refused.value)


def test_fix_round_missing_execution_id_leaves_the_order_to_reconciliation_once(
    venue, alpaca, monkeypatch, external_reports, capfd
) -> None:
    updates = [outside_update(alpaca, "partial_fill", n, 1, qty="4") for n in (1, 2, 3)]
    updates[1] = updates[1].model_copy(update={"execution_id": None})
    updates.append(outside_update(alpaca, "fill", 4, 1, qty="4"))

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        # Keep the native queue pending, including the complete first partial report.
        monkeypatch.setattr(
            client, "_handle_report", lambda r, fills=None: reports.append((r, fills))
        )
        for update in updates:
            await client._apply_trade_update(update)
        assert len(reports) == 1 and calls == []
        assert client._external_fills == {}
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None

    run_with_client(body, IdleTrader())
    text = logged(capfd)
    lines = [line for line in text.splitlines() if "left to reconciliation" in line]
    assert len(lines) == 1 and "no execution_id" in lines[0]
    assert "placed-elsewhere" in lines[0] and str(updates[0].order.id) not in lines[0]
    assert "held back" not in text


@pytest.mark.parametrize("failure", ["APIError", "OSError"])
def test_fix_round_a_failed_root_read_does_not_poison_later_stream_reports(
    venue, alpaca, monkeypatch, external_reports, capfd, failure
) -> None:
    prior = alpaca.alpaca_order(id=str(uuid.uuid4()), client_order_id="outside-root")
    updates = [
        outside_update(alpaca, "partial_fill", n, 1, qty="3", replaces=prior["id"]) for n in (1, 2)
    ]
    updates.append(outside_update(alpaca, "fill", 3, 1, qty="3", replaces=prior["id"]))
    error = (
        alpaca.api_error(500, 50010000, f"cannot read {prior['id']}")
        if failure == "APIError"
        else OSError(f"cannot read {prior['id']}")
    )

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        venue.trading.order_lookup_error = lambda _: error
        await client._apply_trade_update(updates[0])
        venue.trading.order_lookup_error = None
        venue.trading.orders = [prior]
        for update in updates[1:]:
            await client._apply_trade_update(update)
        assert reports == [] and client._external_fills == {}
        assert calls == ["get_order_by_id"]

    run_with_client(body, IdleTrader())
    text = logged(capfd)
    lines = [line for line in text.splitlines() if "left to reconciliation" in line]
    assert len(lines) == 1 and failure in lines[0]
    assert "AAPL" in lines[0] and "placed-elsewhere" in lines[0]
    assert str(updates[0].order.id) not in lines[0] and prior["id"] not in lines[0]
    assert "held back" not in text


def test_fix_round_closed_market_orders_release_their_stored_fills(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        for number in range(12):
            update = outside_update(
                alpaca,
                "fill",
                2,
                2,
                id=str(uuid.uuid4()),
                type="market",
                order_type="market",
                limit_price=None,
            )
            update = update.model_copy(
                update={
                    "order": update.order.model_copy(
                        update={"client_order_id": f"outside-market-{number}"}
                    )
                }
            )
            await client._apply_trade_update(update)
            assert len(client._external_fills) == 0
        await until_idle(client, venue)
        assert len(reports) == 12 and calls == []
        assert len(client._external_fills) == 0

    run_with_client(body, IdleTrader())


def test_fix_round_superseded_legs_release_their_entries_without_losing_fills(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    first = outside_update(alpaca, "partial_fill", 1, 1, qty="3")
    second = outside_update(
        alpaca, "partial_fill", 2, 1, qty="3", id=str(uuid.uuid4()), replaces=str(first.order.id)
    )
    third = outside_update(
        alpaca, "fill", 3, 1, qty="3", id=str(second.order.id), replaces=str(first.order.id)
    )

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        monkeypatch.setattr(
            client, "_handle_report", lambda r, fills=None: reports.append((r, fills))
        )
        await client._apply_trade_update(first)
        await client._apply_trade_update(second)
        assert (
            len(client._external_fills) == 1 and str(first.order.id) not in client._external_fills
        )
        await client._apply_trade_update(third)
        assert len(client._external_fills) == 0 and calls == []
        _, fills = reports[-1]
        assert {fill.trade_id.value for fill in fills} == {
            str(update.execution_id) for update in (first, second, third)
        }

    run_with_client(body, IdleTrader())


def test_fix_round_a_replaced_event_moves_pending_fills_off_the_old_leg(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    first = outside_update(alpaca, "partial_fill", 1, 1, qty="3")
    target = str(uuid.uuid4())
    replaced = first.model_copy(
        update={
            "event": "replaced",
            "order": AlpacaOrder(
                **(first.order.model_dump() | {"status": "replaced", "replaced_by": target})
            ),
        }
    )

    async def body(client) -> None:
        reports, _ = record_reports_and_calls(client, monkeypatch)
        monkeypatch.setattr(
            client, "_handle_report", lambda r, fills=None: reports.append((r, fills))
        )
        await client._apply_trade_update(first)
        await client._apply_trade_update(replaced)
        assert str(first.order.id) not in client._external_fills
        assert len(client._external_fills) == 1
        assert set(client._external_fills[target]) == {str(first.execution_id)}

    run_with_client(body, IdleTrader())


@pytest.mark.parametrize("field,value", [("type", None), ("client_order_id", "")])
def test_fix_round_unmappable_fills_are_never_stored(
    venue, alpaca, monkeypatch, external_reports, field, value
) -> None:
    update = outside_update(alpaca, "fill", 2, 2)
    update = update.model_copy(update={"order": update.order.model_copy(update={field: value})})

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(update)
        assert reports == [] and calls == []
        assert len(client._external_fills) == 0

    run_with_client(body, IdleTrader())


def test_fix_round_materialization_during_mapping_delivers_each_fill_once_without_a_read(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    initial = outside_update(alpaca, "new")
    final = outside_update(alpaca, "fill", 2, 2)
    venue.trading.activities = [
        alpaca.fill_activity(
            final.order.model_dump(),
            str(final.execution_id),
            2,
            "150.1234567",
            final.timestamp,
            cum_qty=2,
        )
    ]

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        await client._apply_trade_update(initial)
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None
        original = client._order_report
        generated = []
        generate = client.generate_order_filled

        def filled(**kwargs):
            generated.append(kwargs["trade_id"].value)
            generate(**kwargs)

        async def materialize(order, known=None):
            report = await original(order, known)
            await until_idle(client, venue)
            cached = client.cache.order(ClientOrderId("placed-elsewhere"))
            assert cached is not None and cached.filled_qty.as_double() == 0
            return report

        monkeypatch.setattr(client, "_order_report", materialize)
        monkeypatch.setattr(client, "generate_order_filled", filled)
        await client._apply_trade_update(final)
        await until_idle(client, venue)
        assert calls == []
        delivered = (
            generated
            + [report.trade_id.value for report, _ in reports if isinstance(report, FillReport)]
            + [fill.trade_id.value for _, fills in reports for fill in fills or []]
        )
        assert delivered == [str(final.execution_id)]
        cached = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert cached.status == OrderStatus.FILLED and cached.filled_qty.as_double() == 2
        assert [trade_id.value for trade_id in cached.trade_ids] == [str(final.execution_id)]

    run_with_client(body, IdleTrader())


def test_fix_round_unloaded_outside_instrument_is_skipped_once_per_symbol(
    venue, alpaca, monkeypatch, external_reports, capfd
) -> None:
    updates = [
        outside_update(alpaca, "new", symbol="MSFT"),
        outside_update(alpaca, "fill", 2, 2, symbol="MSFT"),
    ]

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        for update in updates:
            await client._apply_trade_update(update)
        assert reports == [] and calls == [] and client._external_fills == {}

    run_with_client(body, IdleTrader())
    lines = [line for line in logged(capfd).splitlines() if "has not loaded" in line]
    assert len(lines) == 1 and "MSFT" in lines[0] and "not reported" in lines[0]
    assert str(updates[0].order.id) not in lines[0]


@pytest.mark.parametrize("field", ["trail_price", "trail_percent"])
def test_fix_round_fine_advisory_trailing_trigger_does_not_fail_startup(
    venue, alpaca, capfd, field, trailing_reports
) -> None:
    raw = trailing_order(alpaca, field, "147.251234")
    venue.trading.orders = [raw]
    node = build_node(execution_tests.trader(submit=False))
    run(node)
    order = node.cache.order(ClientOrderId(raw["client_order_id"]))
    assert order is not None and order.status == OrderStatus.ACCEPTED
    assert order.trigger_price is None
    assert order.trailing_offset == Decimal("1.25" if field == "trail_price" else "125")
    assert venue.trading.submitted == []
    lines = [line for line in logged(capfd).splitlines() if "advisory trailing trigger" in line]
    assert len(lines) == 1 and "AAPL" in lines[0] and "147.251234" in lines[0]
    assert raw["id"] not in lines[0]


@pytest.mark.parametrize("reason", ["unmapped", "replaced", "rejected_replace_trace"])
def test_fix_round_outside_logs_do_not_contain_venue_ids(
    venue, alpaca, monkeypatch, external_reports, capfd, reason
) -> None:
    update = outside_update(alpaca, "new")
    prior = alpaca.alpaca_order(id=str(uuid.uuid4()), client_order_id="outside-root")
    if reason == "unmapped":
        update = update.model_copy(update={"order": update.order.model_copy(update={"type": None})})
    elif reason == "replaced":
        update = update.model_copy(
            update={
                "event": "replaced",
                "order": AlpacaOrder(
                    **(
                        update.order.model_dump()
                        | {"status": "replaced", "replaced_by": str(uuid.uuid4())}
                    )
                ),
            }
        )
    else:
        update = update.model_copy(
            update={
                "event": "rejected",
                "order": AlpacaOrder(
                    **(update.order.model_dump() | {"status": "rejected", "replaces": prior["id"]})
                ),
            }
        )

    async def body(client) -> None:
        reports, calls = record_reports_and_calls(client, monkeypatch)
        if reason == "rejected_replace_trace":
            venue.trading.orders = [prior]

            def fail_second_read(_):
                return OSError(f"unreadable {prior['id']}") if len(calls) > 1 else None

            venue.trading.order_lookup_error = fail_second_read
        await client._apply_trade_update(update)
        assert reports == []
        assert calls == (
            ["get_order_by_id", "get_order_by_id"] if reason == "rejected_replace_trace" else []
        )

    run_with_client(body, IdleTrader())
    lines = [line for line in logged(capfd).splitlines() if "Alpaca order " in line]
    assert lines and all(str(update.order.id) not in line for line in lines)
    assert all(prior["id"] not in line for line in lines)
    assert any(
        "AAPL" in line
        and "buy" in line
        and "placed-elsewhere" in line
        and ("None" if reason == "unmapped" else "limit") in line
        for line in lines
    )


def test_fix_round_outside_startup_mapping_failures_omit_venue_ids(venue, alpaca, capfd) -> None:
    raw = alpaca.alpaca_order(client_order_id="outside-unmapped", type=None, order_type=None)
    venue.trading.orders = [raw]
    with pytest.raises(RuntimeError, match="Failed to get mass status"):
        run(build_node(execution_tests.trader(submit=False)))
    lines = [
        line for line in logged(capfd).splitlines() if "Alpaca order status reports failed" in line
    ]
    assert len(lines) == 1 and raw["id"] not in lines[0]
    assert "AAPL buy None" in lines[0] and "client lineage 'outside-unmapped'" in lines[0]
    assert "is open with order type None" in lines[0]
