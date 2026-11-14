"""An owner's OCO, bracket or OTO order in an instrument the protector's node has loaded, through the pinned
LiveNode under the protector's own adapter config (`owner_execution_config`), on the adapter's offline
stand-ins. AAPL is the node's own order; MSFT is a holding of the owner's, loaded, whose OCO leg the adapter
cannot represent (`mapped_order_report`: "order class oco").

The running open-order check asks the client for open orders only (`open_only`, true by default,
nautilus-trader@d53849b216e2 crates/live/src/node/config.rs:481; the command at
crates/live/src/execution/manager.rs:2156-2166), and a client whose report fails puts no report through
(`request_open_order_reports`, crates/live/src/node/reconciliation.rs:553-575). A running fill request
names one instrument (the position check's, manager.rs:2811-2820), and one that fails costs that key alone,
with nothing inferred for it (reconciliation.rs:861-923, 455-475). Start-up's mass status asks with `open_only` false
(crates/common/src/clients/execution.rs:524-529), and there an order this slice cannot represent still fails
start-up by design (ADR 0030)."""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import re
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.trading.models import Clock
from canon_alpaca import execution
from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecutionEngineConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType

from canon_trading.owner_protector import owner_execution_config

ADAPTER_TESTS = Path(__file__).resolve().parents[1] / "packages" / "canon-alpaca" / "tests"


def _adapter_module(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ADAPTER_TESTS / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_conf = _adapter_module("loaded_outside_adapter_conftest", "conftest.py")
_tec = _adapter_module("loaded_outside_adapter_execution_tests", "test_execution_client.py")
alpaca = _conf.alpaca  # pytest fixture: the adapter's stand-ins
venue = _tec.venue
ADAPTER_DEFAULT_EXEC_CONFIG = _tec.exec_config  # report_external_trailing_stops off

AAPL = InstrumentId.from_str("AAPL.ALPACA")
MSFT = InstrumentId.from_str("MSFT.ALPACA")
MSFT_ASSET_ID = "64bbff51-59d6-4b3c-9351-13ad85e3c752"
LEG_ID = "0a3c4c3e-6f4f-4e59-9a3a-2f7d1e8b1c11"
LEG_CLIENT_ID = "owner-msft-leg"
REPLACEMENT_ID = "5d1e6a90-2b7c-4f3e-8a41-9c0d7e2f3b22"
NODE_REPLACEMENT_ID = "8f2a1c4d-3e5b-4a6c-9d7e-1b2c3d4e5f60"
FAILED_CHECK = "Failed to generate order status reports"
HOUR = timedelta(hours=1)
HANDLER_FAILED = re.compile(r"Python .*? failed")

OPEN_CHECK_ONLY = LiveExecutionEngineConfig(
    reconciliation_startup_delay_secs=0,
    open_check_interval_secs=0.25,
    open_check_threshold_ms=200,
)
"""The paper node's open-order check (`paper_node.exec_engine_config`, open orders only by the engine's
default) every quarter second, and no position check, so a fill the check brings is the check's own."""

OPEN_CHECK_STREAM_FIRST = LiveExecutionEngineConfig(
    reconciliation_startup_delay_secs=0,
    open_check_interval_secs=0.4,
    open_check_threshold_ms=200,
)
"""The same check, slower than the stream's hand-off, so a leg's end can reach the client by its update."""


@pytest.fixture(autouse=True)
def dispose_nodes():
    """Dispose of every node a test built, then collect: a run that failed leaves its node referenced
    from the exception's traceback, and the next build on this thread is refused until it is freed."""
    yield
    while _tec.BUILT_NODES:
        _tec.BUILT_NODES.pop().dispose()
    gc.collect()


@pytest.fixture(autouse=True)
def two_loaded(monkeypatch, venue):
    """The node loads AAPL and MSFT, under the protector's own adapter config."""
    provider = InstrumentProviderConfig(load_ids=[str(AAPL), str(MSFT)])
    monkeypatch.setattr(_tec, "provider_config", lambda: provider)
    monkeypatch.setattr(
        _tec, "exec_config", lambda: owner_execution_config(instrument_provider=provider)
    )
    venue.trading.assets = [
        _conf.ASSET_AAPL,
        {**_conf.ASSET_AAPL, "symbol": "MSFT", "id": MSFT_ASSET_ID},
    ]


def msft_position(qty: str = "3") -> dict:
    return {
        **_conf.POSITION_AAPL,
        "asset_id": MSFT_ASSET_ID,
        "symbol": "MSFT",
        "qty": qty,
        "qty_available": qty,
    }


def owner_leg(order_class: str = "oco", **changes: object) -> dict:
    """The owner's open MSFT sell stop, a leg of `order_class`, submitted now: after the node's own
    orders, so Alpaca lists it first (newest first, "Get All Orders"; the stand-in's `get_orders`)."""
    now = datetime.now(UTC).isoformat()
    fields: dict[str, object] = {
        "created_at": now,
        "submitted_at": now,
        "updated_at": now,
        "id": LEG_ID,
        "client_order_id": LEG_CLIENT_ID,
        "asset_id": MSFT_ASSET_ID,
        "symbol": "MSFT",
        "side": "sell",
        "type": "stop",
        "order_type": "stop",
        "limit_price": None,
        "stop_price": "140",
        "qty": "3",
        "order_class": order_class,
    }
    return _conf.alpaca_order(**{**fields, **changes})


def lines_naming(log: str, text: str) -> list[str]:
    return [line for line in log.splitlines() if text in line]


def engine_log(capfd) -> str:
    """The run's log, read once, with no strategy or actor handler failed in it."""
    log = _tec.logged(capfd)
    assert not HANDLER_FAILED.search(log), HANDLER_FAILED.findall(log)
    return log


OPEN_ONLY = SimpleNamespace(open_only=True, instrument_id=None, start=None, end=None)
"""The running open-order check's command, as the engine builds it (manager.rs:2161-2170)."""


@pytest.mark.parametrize("order_class", ["oco", "bracket", "oto"])
def test_an_owner_leg_in_a_loaded_holding_leaves_the_open_order_check_running_for_the_nodes_order(
    venue, capfd, order_class
) -> None:
    """The node's AAPL limit loses a partial fill's update; the open-order check brings it while the
    owner's MSFT leg stands. Before: the check fails at every cycle on the leg, and the fill is never
    brought."""
    venue.trading.script = "accept"
    venue.trading.positions = [msft_position()]
    strategy = _tec.trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    seen: list = []

    async def body(client) -> None:
        await _tec.until_named(strategy, "OrderAccepted")
        await _tec.until_idle(client, venue)
        venue.trading.orders.append(owner_leg(order_class))  # placed by the owner after the start
        placed = _tec.placed_order(venue)
        venue.trading.update(
            placed["id"],
            "partial_fill",
            1,
            deliver=False,
            status="partially_filled",
            filled_qty="1",
        )
        await asyncio.sleep(1.2)  # several open-order checks
        cached = client.cache.order(strategy.client_order_id)
        seen.append(cached.filled_qty.as_decimal())

    _tec.run_with_client(body, strategy, exec_engine=OPEN_CHECK_ONLY)

    log = engine_log(capfd)
    assert seen == [Decimal(1)], strategy.names()
    assert lines_naming(log, FAILED_CHECK) == []
    named = lines_naming(log, LEG_CLIENT_ID)
    assert len(named) == 1 and "ALERT" in named[0] and LEG_ID not in named[0], named


def test_an_owner_leg_in_a_loaded_holding_is_named_once_on_the_stream(venue, capfd) -> None:
    """Three updates of the owner's MSFT leg, none with a fill, then a fill of the node's own AAPL limit:
    the leg is named once, as a holding whose cover the node cannot see, and the stream goes on to apply
    the node's fill. Before: one WARNING, no ALERT."""
    venue.trading.script = "accept"
    venue.trading.positions = [msft_position()]
    strategy = _tec.trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"
    leg = owner_leg()
    seen: list = []

    async def body(client) -> None:
        await _tec.until_named(strategy, "OrderAccepted")
        await _tec.until_idle(client, venue)
        venue.trading.orders.append(leg)
        for _ in range(3):
            venue.stream.send({"event": "new", "order": dict(leg), "timestamp": leg["updated_at"]})
        await _tec.until_idle(client, venue)
        placed = _tec.placed_order(venue)
        venue.trading.update(
            placed["id"], "partial_fill", 1, status="partially_filled", filled_qty="1"
        )
        await _tec.until_idle(client, venue)
        seen.append(client.cache.order(strategy.client_order_id).filled_qty.as_decimal())

    _tec.run_with_client(body, strategy)

    named = lines_naming(engine_log(capfd), LEG_CLIENT_ID)
    assert len(named) == 1 and "ALERT" in named[0] and LEG_ID not in named[0], named
    assert seen == [Decimal(1)]


def test_a_replaced_owner_leg_is_counted_once_on_the_stream(venue, capfd) -> None:
    """The owner modifies the MSFT leg: Alpaca's `replaced` for it, then `new` for its replacement. One
    order is left out, not two."""
    venue.trading.positions = [msft_position()]
    leg = owner_leg()
    replacement = owner_leg(id=REPLACEMENT_ID, client_order_id="owner-msft-leg-2", replaces=LEG_ID)

    async def body(client) -> None:
        venue.trading.orders.append(leg)
        venue.stream.send({"event": "new", "order": dict(leg), "timestamp": leg["updated_at"]})
        await _tec.until_idle(client, venue)
        leg.update(status="replaced", replaced_by=REPLACEMENT_ID)
        venue.trading.orders.append(replacement)
        venue.stream.send({"event": "replaced", "order": dict(leg), "timestamp": leg["updated_at"]})
        venue.stream.send(
            {"event": "new", "order": dict(replacement), "timestamp": replacement["updated_at"]}
        )
        await _tec.until_idle(client, venue)

    _tec.run_with_client(body, _tec.trader(idle=True))

    alerts = lines_naming(engine_log(capfd), "ALERT")
    assert len(alerts) == 2, alerts
    assert "(1 such order(s) left out now)" in alerts[1], alerts


@pytest.mark.parametrize("ended_by", ["check", "stream"])
def test_an_owner_leg_that_ends_is_no_longer_left_out_and_is_not_named_again(
    venue, capfd, ended_by
) -> None:
    """The owner cancels the MSFT leg: the next running check, or the stream's update of its end, takes it
    off the left-out count, and a later check names nothing more."""
    venue.trading.positions = [msft_position()]
    leg = owner_leg()

    async def body(client) -> None:
        venue.trading.orders.append(leg)
        await asyncio.sleep(0.6)  # checks while it stands
        leg.update(status="canceled", canceled_at=datetime.now(UTC).isoformat())
        if ended_by == "stream":
            venue.stream.send(
                {"event": "canceled", "order": dict(leg), "timestamp": leg["updated_at"]}
            )
            await _tec.until_idle(client, venue)
        await asyncio.sleep(0.6)  # checks after it ended

    _tec.run_with_client(
        body,
        _tec.trader(idle=True),
        exec_engine=OPEN_CHECK_ONLY if ended_by == "check" else OPEN_CHECK_STREAM_FIRST,
    )

    log = engine_log(capfd)
    named = lines_naming(log, LEG_CLIENT_ID)
    assert len([line for line in named if "ALERT" in line]) == 1, named
    ended = lines_naming(log, "left out of this client's reports has ended")
    assert len(ended) == 1 and "(0 such order(s) left out now)" in ended[0], ended
    assert lines_naming(log, FAILED_CHECK) == []


def test_an_owner_leg_in_a_loaded_holding_never_enters_the_nodes_cache(venue) -> None:
    """The base fact the cover means turns on: the protector judges cover from the node's cache
    (`OwnerProtector._uncovered`), and the owner's leg, unrepresentable, is never put there, so the
    protector sees MSFT as uncovered. Passes before and after an adapter-only change: the cover is the
    protector's to read (the lead's ruling on the means)."""
    venue.trading.positions = [msft_position()]
    leg = owner_leg()
    held: list = []

    async def body(client) -> None:
        venue.trading.orders.append(leg)
        venue.stream.send({"event": "new", "order": dict(leg), "timestamp": leg["updated_at"]})
        await _tec.until_idle(client, venue)
        held.extend(
            o
            for o in client.cache.orders(instrument_id=MSFT)
            if o.venue_order_id is not None and o.venue_order_id.value == LEG_ID
        )

    _tec.run_with_client(body, _tec.trader(idle=True))

    assert held == []  # the position's own EXTERNAL fill is in the cache, the leg is not


def test_a_partly_filled_owner_leg_fails_only_its_own_symbols_fill_request(
    venue, capfd, monkeypatch
) -> None:
    """The running fill request names one instrument (the position check's, manager.rs:2811-2820). The
    owner's partly filled MSFT leg fails MSFT's request alone, as ADR 0075 asks: a key whose request
    fails gets nothing inferred (reconciliation.rs:861-923), where one answered without the leg's fills
    would fall back to a synthetic fill (455-475). AAPL's request is answered. Passes before and after:
    the fill request is left as it was."""
    monkeypatch.setattr(execution, "FILL_READ_RETRY_SECS", 0.0)
    venue.trading.positions = [msft_position("2")]
    answered: list = []
    refused: list = []

    def request(instrument_id: InstrumentId) -> SimpleNamespace:
        return SimpleNamespace(
            instrument_id=instrument_id, venue_order_id=None, start=None, end=None
        )

    async def body(client) -> None:
        venue.trading.orders.append(owner_leg(filled_qty="1", status="partially_filled"))
        answered.extend(await client._generate_fill_reports(request(AAPL)))
        try:
            await client._generate_fill_reports(request(MSFT))
        except Exception as e:  # noqa: BLE001 - the request's own failure, whatever its type
            refused.append(str(e))

    _tec.run_with_client(body, _tec.trader(idle=True))

    assert answered == []
    assert len(refused) == 1 and LEG_CLIENT_ID in refused[0], refused
    assert lines_naming(engine_log(capfd), "ALERT") == []


def test_a_full_page_of_open_orders_does_not_end_a_left_out_order(
    venue, capfd, monkeypatch
) -> None:
    """A page of open orders as long as Alpaca's most per request may leave older orders out, so a
    left-out order missing from it has not been shown to end."""
    monkeypatch.setattr(execution, "ORDERS_PAGE_LIMIT", 1)
    venue.trading.positions = [msft_position()]

    async def body(client) -> None:
        venue.trading.orders.append(owner_leg())
        await client._generate_order_status_reports(OPEN_ONLY)
        later = datetime.now(UTC).isoformat()
        venue.trading.orders.append(
            _conf.alpaca_order(
                id=REPLACEMENT_ID,
                client_order_id="owner-msft-limit",
                created_at=later,
                submitted_at=later,
                updated_at=later,
                asset_id=MSFT_ASSET_ID,
                symbol="MSFT",
                side="sell",
                qty="1",
            )
        )
        await client._generate_order_status_reports(OPEN_ONLY)

    _tec.run_with_client(body, _tec.trader(idle=True))

    log = engine_log(capfd)
    assert len(lines_naming(log, "ALERT")) == 1
    assert lines_naming(log, "has ended") == []


class TrailingTrader(_tec.Trader):
    """Idle, with trades subscribed: the pinned risk engine values a DEFAULT trailing stop from a trade
    (as the adapter's `test_venue_trailing_stops.TrailingTrader`)."""

    def on_start(self) -> None:
        super().on_start()
        self.subscribe_trades(AAPL)


def test_an_untraced_replacement_of_the_nodes_own_trailing_stop_is_reported_not_left_out(
    venue, capfd, monkeypatch
) -> None:
    """Under the adapter's default reporting (outside trailing stops not reported), with the node's own
    trailing stops sent, the node's trailing stop is replaced and this client has no record of it (a
    restart, or a missed `replaced`). The open check traces the replacement to the node's order and
    reports it under the node's ID, as it did before any leave-out; it is not an outside order's."""
    provider = InstrumentProviderConfig(load_ids=[str(AAPL), str(MSFT)])
    monkeypatch.setattr(
        _tec,
        "exec_config",
        lambda: AlpacaExecutionClientConfig(
            instrument_provider=provider, submit_trailing_stops=True
        ),
    )
    now = datetime.now(UTC)
    monkeypatch.setattr(
        venue.trading,
        "get_clock",
        lambda: Clock(
            timestamp=datetime.now(UTC), is_open=True, next_open=now, next_close=now + HOUR
        ),
        raising=False,
    )
    venue.trading.script = "accept"
    strategy = TrailingTrader()
    strategy.idle = True
    reports: list = []
    node_id: list = []

    async def body(client) -> None:
        await _tec.until_idle(client, venue)
        order = strategy.order_factory.trailing_stop_market(
            instrument_id=AAPL,
            order_side=OrderSide.SELL,
            quantity=Quantity.from_int(2),
            trailing_offset=Decimal("1.25"),
            trailing_offset_type=TrailingOffsetType.PRICE,
            time_in_force=TimeInForce.GTC,
        )
        node_id.append(order.client_order_id)
        strategy.submit_order(order)
        await _tec.until_idle(client, venue)
        _tec.forget(client)
        placed = _tec.placed_order(venue)
        later = datetime.now(UTC).isoformat()
        venue.trading.orders.append(
            {
                **placed,
                "id": NODE_REPLACEMENT_ID,
                "client_order_id": "alpaca-made-replacement",
                "replaces": placed["id"],
                "status": "new",
                "submitted_at": later,
                "updated_at": later,
            }
        )
        placed.update(status="replaced", replaced_by=NODE_REPLACEMENT_ID)
        reports.extend(await client._generate_order_status_reports(OPEN_ONLY))

    _tec.run_with_client(body, strategy)

    assert [(r.venue_order_id.value, r.client_order_id) for r in reports] == [
        (NODE_REPLACEMENT_ID, node_id[0])
    ]
    assert lines_naming(engine_log(capfd), "ALERT") == []


def test_limit_with_open_check_open_only_off_an_owner_leg_still_fails_the_check(
    venue, capfd
) -> None:
    """A named limit: the leave-out keys on the engine's `open_only`, which no Canon node turns off
    (`open_check_open_only`, default true). With it off, the running check asks as start-up does, and the
    owner's leg fails it as before."""
    venue.trading.positions = [msft_position()]

    async def body(client) -> None:
        venue.trading.orders.append(owner_leg())
        await asyncio.sleep(0.8)

    _tec.run_with_client(
        body,
        _tec.trader(idle=True),
        exec_engine=LiveExecutionEngineConfig(
            reconciliation_startup_delay_secs=0,
            open_check_interval_secs=0.25,
            open_check_threshold_ms=200,
            open_check_open_only=False,
        ),
    )

    assert lines_naming(engine_log(capfd), FAILED_CHECK) != []


@pytest.mark.parametrize("order_class", ["oco", "bracket", "oto"])
def test_control_an_owner_leg_in_a_loaded_holding_still_fails_startup(venue, order_class) -> None:
    """Start-up keeps the adapter's refusal (ADR 0030): the leg is open before the node starts."""
    venue.trading.positions = [msft_position()]
    venue.trading.orders = [owner_leg(order_class)]
    node = _tec.build_node(_tec.trader(submit=False))

    with pytest.raises(BaseException):  # noqa: B017 - the start's own failure, whatever its type
        _tec.run(node)

    assert node.cache.positions_open() == []


def test_control_the_nodes_own_order_it_cannot_represent_still_fails_the_check(
    venue, capfd
) -> None:
    """An order of the node's own that Alpaca shows as one this slice cannot represent stays a refusal
    in the running check (ADR 0030), whatever is left out for an owner's order."""
    venue.trading.script = "accept"
    strategy = _tec.trader(order_type="limit", quantity=3)
    strategy.stopped_for = "held open"

    async def body(client) -> None:
        await _tec.until_named(strategy, "OrderAccepted")
        await _tec.until_idle(client, venue)
        _tec.placed_order(venue)["order_class"] = "oco"
        await asyncio.sleep(0.8)  # several open-order checks

    _tec.run_with_client(body, strategy, exec_engine=OPEN_CHECK_ONLY)

    assert lines_naming(engine_log(capfd), FAILED_CHECK) != []
