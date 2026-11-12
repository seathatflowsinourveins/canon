"""Alpaca error codes and status-before-fill output, with placeholder clients and no paper calls.

Contracts: alpaca-py c42add7c0a70 APIError.code/message; NautilusTrader d53849b216e2
ExecutionClient._handle_report(report, fills) and external-order materialization.
"""

import json
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from canon_alpaca.execution import REASON_MAX_CHARS
from canon_alpaca.execution import ChainTooLong
from canon_alpaca.execution import api_error_text
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import FillReport
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderStatusReport

from .test_execution_client import AAPL
from .test_execution_client import FINAL_EVENTS
from .test_execution_client import build_node
from .test_execution_client import dispose_nodes as dispose_nodes  # noqa: PLC0414 - pytest fixture
from .test_execution_client import modifier
from .test_execution_client import run
from .test_execution_client import run_modify
from .test_execution_client import run_with_client
from .test_execution_client import trader
from .test_execution_client import until_idle
from .test_execution_client import venue as venue  # noqa: PLC0414 - pytest fixture
from .test_external_orders import IdleTrader
from .test_external_orders import external_reports as external_reports  # noqa: PLC0414 - pytest fixture
from .test_external_orders import make_order
from .test_external_orders import outside_update
from .test_external_orders import record_reports_and_calls
from .test_external_orders import reduce_only_sells as reduce_only_sells  # noqa: PLC0414 - fixture

QUANTITY_REFUSAL = "insufficient qty available for order (requested: 1, available: 0)"


def refusal(status: int, message: str, code: int | str | None) -> APIError:
    payload: dict[str, object] = {"message": message}
    if code is not None:
        payload["code"] = code
    return APIError(
        json.dumps(payload), SimpleNamespace(response=SimpleNamespace(status_code=status))
    )


@pytest.mark.parametrize("code", [40310000, None])
def test_403_submit_reason_keeps_the_message_and_the_body_code_when_present(venue, code) -> None:
    venue.trading.submit_error = refusal(403, QUANTITY_REFUSAL, code)
    # This is an ordinary SELL, independent of the strategy's reduce-only flag.
    strategy = trader(order_type="limit", side=OrderSide.SELL, quantity=1)

    run(build_node(strategy))

    assert strategy.names() == ["OrderSubmitted", "OrderRejected"]
    message = f"{code}: {QUANTITY_REFUSAL}" if code is not None else QUANTITY_REFUSAL
    assert strategy.events[-1].reason == f"Alpaca refused the order (403): {message}"


@pytest.mark.parametrize("code", [42210000, None])
def test_cancel_reason_keeps_the_message_and_the_body_code_when_present(venue, code) -> None:
    venue.trading.script = "accept"
    venue.trading.cancel_error = refusal(422, "order is not cancelable", code)
    strategy = trader(order_type="limit", cancel_on="OrderAccepted")
    FINAL_EVENTS.add("OrderCancelRejected")
    try:
        run(build_node(strategy))
    finally:
        FINAL_EVENTS.discard("OrderCancelRejected")

    assert strategy.names()[-1] == "OrderCancelRejected"
    message = f"{code}: order is not cancelable" if code is not None else "order is not cancelable"
    assert strategy.events[-1].reason == f"Alpaca refused the cancel (422): {message}"


@pytest.mark.parametrize("code", [42210000, None])
def test_modify_reason_keeps_the_message_and_the_body_code_when_present(venue, code) -> None:
    venue.trading.script = "accept"
    venue.trading.replace_error = refusal(422, "order is not replaceable", code)
    strategy = modifier()

    run_modify(venue, strategy, lambda: "OrderModifyRejected" in strategy.names())

    assert strategy.names()[-2:] == ["OrderPendingUpdate", "OrderModifyRejected"]
    message = (
        f"{code}: order is not replaceable" if code is not None else "order is not replaceable"
    )
    assert (
        strategy.last("OrderModifyRejected").reason
        == f"Alpaca refused the replace (422): {message}"
    )


def test_an_error_code_survives_bounding_a_long_message() -> None:
    text = api_error_text(refusal(403, "x" * REASON_MAX_CHARS, 40310000))

    assert text.startswith("40310000: ")
    assert len(text) == REASON_MAX_CHARS and text.endswith("…")


def test_a_position_intent_mismatch_rejects_the_reduce_only_sell_with_its_code(
    venue, reduce_only_sells
) -> None:
    venue.trading.submit_error = refusal(422, "position intent mismatch", 42210000)
    strategy = IdleTrader()

    async def body(client) -> None:
        order = make_order(strategy, "limit", OrderSide.SELL, True)
        strategy.submit_order(order)
        await until_idle(client, venue)
        assert client.cache.order(order.client_order_id).status == OrderStatus.REJECTED
        rejection = next(
            event for event in strategy.events if type(event).__name__ == "OrderRejected"
        )
        assert (
            rejection.reason == "Alpaca refused the order (422): 42210000: position intent mismatch"
        )
        assert len(venue.trading.submitted) == 1
        assert venue.trading.submitted[0].to_request_fields()["position_intent"] == "sell_to_close"

    run_with_client(body, strategy)


@pytest.mark.parametrize("body", ["not JSON", "{}"])
def test_an_error_body_without_a_code_keeps_the_existing_raw_text(body) -> None:
    assert api_error_text(APIError(body)) == body


def test_an_unknown_orders_status_reaches_the_engine_together_with_its_fill(
    venue, alpaca, monkeypatch, external_reports
) -> None:
    strategy = IdleTrader()
    update = outside_update(
        alpaca, "fill", 2, 2, type="market", order_type="market", limit_price=None
    )

    async def body(client) -> None:
        strategy.set_external_order_instrument_ids([AAPL])
        reports, calls = record_reports_and_calls(client, monkeypatch)
        assert client.cache.order(ClientOrderId("placed-elsewhere")) is None

        await client._apply_trade_update(update)

        assert len(reports) == 1
        status, fills = reports[0]
        assert isinstance(status, OrderStatusReport)
        assert not any(isinstance(report, FillReport) for report, _ in reports)
        assert len(fills) == 1 and isinstance(fills[0], FillReport)
        assert fills[0].venue_order_id == status.venue_order_id
        assert fills[0].trade_id.value == str(update.execution_id)
        await until_idle(client, venue)
        order = client.cache.order(ClientOrderId("placed-elsewhere"))
        assert not order.is_reduce_only and order.filled_qty.as_double() == 2
        assert strategy.filled == 2 and len(client.cache.positions_open()) == 1
        assert calls == []

    run_with_client(body, strategy)


VENUE_ID = "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (
            refusal(429, f"rate limit exceeded for order {VENUE_ID}", 42910000),
            "APIError 429: 42910000",
        ),
        (refusal(404, f"order not found: {VENUE_ID}", 40410000), "APIError 404: 40410000"),
        (refusal(500, "internal server error", None), "APIError 500"),
        (APIError(f"order {VENUE_ID} unreadable"), "APIError"),
        (OSError(f"connection reset reading {VENUE_ID}"), "OSError"),
        (TimeoutError(), "TimeoutError"),
        # a body whose `code` is not a number is not trusted to be free of an order ID
        (refusal(404, "not found", f"order {VENUE_ID} not found"), "APIError 404"),
        (refusal(422, "refused", "42210000"), "APIError 422: 42210000"),
        # the adapter's own named limit carries no ID and keeps its words
        (
            ChainTooLong("more than 16 reads to sum the chain's fills"),
            "more than 16 reads to sum the chain's fills",
        ),
    ],
)
def test_a_failures_cause_keeps_the_status_and_code_and_never_the_text(error, cause) -> None:
    """A failure message on the report paths tells a rate limit from a missing order from a server
    error, and still carries nothing that could name a venue order ID (code review, 2026-11-12)."""
    from canon_alpaca.execution import failure_cause

    assert failure_cause(error) == cause
    assert VENUE_ID not in failure_cause(error)
