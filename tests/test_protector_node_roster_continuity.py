"""F2/F3 roster continuity and the adapter unit's reconciliation characterization.

Contracts: session_window.start_account and OwnerProtector._read_roster; NautilusTrader
d53849b216e2 crates/common/src/clients/execution.rs:516-574 generates mass status before
crates/live/src/execution/manager.rs:3454-3491 filters its reports by instrument.
All accounts, transports, orders and holdings below are synthetic; no credential is read.
"""

from __future__ import annotations

import asyncio
import gc
from decimal import Decimal
from typing import Any
from typing import cast

import pytest
from canon_alpaca.execution import order_status_report
from nautilus_trader.common import logging_sync_to_disk

from canon_trading import paper_node
from canon_trading import protector_node as p
from canon_trading import session_window as w
from canon_trading.owner_protector import OwnerProtector
from tests import test_owner_protector as h
from tests import test_protector_node as n
from tests.test_protect import _order
from tests.test_protect import _position


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override conftest's credential-environment reader for these offline cases."""


@pytest.fixture(autouse=True)
def collect_node_cycles():
    """NautilusTrader d53849b216e2 python/tests/conftest.py:148-155: release native node cycles."""
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def local_books(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CANON_HOME", raising=False)
    monkeypatch.delenv(p.LEVEL_BOOK_ENV, raising=False)


@pytest.fixture(params=[1, 2], ids=["count-one", "count-two-gap-1000ms"])
def count_settings(request):
    if request.param == 1:
        return {"breach_count": 1}
    return {"breach_count": 2, "confirm_gap_ms": 1000}


class Account:
    def __init__(self, *, fractional_position=False, bad_order=False):
        self.fractional_position = fractional_position
        self.orders = (
            [_order("BBB", "0.5", "synthetic-owner-fractional-buy", side="buy")]
            if bad_order
            else []
        )
        self.order_reads = 0

    def get_all_positions(self):
        return [
            _position("AAA", "10"),
            _position("BBB", "0.5" if self.fractional_position else "5"),
        ]

    def get_orders(self, request):
        self.order_reads += 1
        return list(self.orders)


class Roster(h.Harness):
    def __init__(self, **settings):
        super().__init__(**settings)
        self.shutdowns = []

    def shutdown_system(self, reason=None):
        self.shutdowns.append(reason)


def test_unmappable_new_symbol_order_keeps_the_current_roster_protected(count_settings):
    account = Account(bad_order=True)
    with pytest.raises(ValueError, match="quantity 0.5 is fractional"):
        order_status_report(account.orders[0], h.T0, trailing=True)
    guard = Roster(**count_settings, roster=(h.AAA,), trading=account)
    guard.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    guard.observe("10.00", h.T0 + h.SECOND)
    guard._read_roster(h.T0 + 2 * h.SECOND)
    print(f"ROSTER: {count_settings} shutdowns={guard.shutdowns} order_reads={account.order_reads}")
    assert guard.shutdowns == [], "an unmappable BBB order must not stop AAA's protector"
    assert account.order_reads > 0
    assert guard.ended_for is None and not guard.stopping
    assert any(
        "BBB" in line and "unprotected" in line and "fractional" in line for line in guard.said
    )
    guard.observe("9.89", h.T0 + 3 * h.SECOND)
    guard.observe("9.88", h.T0 + 4 * h.SECOND)
    assert len(guard.sent) == 1 and guard.sent[0].instrument_id == h.AAA


def test_new_whole_symbol_without_an_order_still_requests_restart(count_settings):
    account = Account()
    guard = Roster(**count_settings, roster=(h.AAA,), trading=account)
    guard.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    guard.observe("10.00", h.T0 + h.SECOND)
    guard._read_roster(h.T0 + 2 * h.SECOND)
    assert guard.shutdowns == ["new holding outside the roster: BBB 5"]
    assert h.AAA in guard.slots


def test_mappable_new_symbol_order_still_requests_restart(count_settings):
    account = Account()
    account.orders = [_order("BBB", "5", "synthetic-owner-buy", side="buy")]
    guard = Roster(**count_settings, roster=(h.AAA,), trading=account)
    guard._read_roster(h.T0)
    assert account.order_reads == 1
    assert guard.shutdowns == ["new holding outside the roster: BBB 5"]


def test_restart_waits_for_an_unmappable_order_to_leave_the_account(count_settings):
    account = Account(bad_order=True)
    guard = Roster(**count_settings, roster=(h.AAA,), trading=account)
    guard._read_roster(h.T0)
    assert guard.shutdowns == []
    account.orders.clear()
    guard._read_roster(h.T0 + 30 * h.SECOND)
    assert account.order_reads == 2
    assert guard.shutdowns == ["new holding outside the roster: BBB 5"]


def test_unread_open_orders_keep_current_protection_and_retry_at_the_next_roster_read(
    count_settings,
):
    class Unread(Account):
        def get_orders(self, request):
            if self.order_reads == 0:
                self.order_reads += 1
                raise ConnectionError("synthetic unread orders")
            return super().get_orders(request)

    account = Unread()
    guard = Roster(**count_settings, roster=(h.AAA,), trading=account)
    guard.adopt_holding(h.AAA, h.Price.from_str("9.90"))
    guard.observe("10.00", h.T0 + h.SECOND)
    guard._read_roster(h.T0 + 2 * h.SECOND)
    assert guard.shutdowns == []
    assert any(
        "BBB" in line and "unprotected" in line and "ConnectionError" in line for line in guard.said
    )
    guard.observe("9.89", h.T0 + 3 * h.SECOND)
    guard.observe("9.88", h.T0 + 4 * h.SECOND)
    assert len(guard.sent) == 1 and guard.sent[0].instrument_id == h.AAA
    guard._read_roster(h.T0 + 32 * h.SECOND)
    assert account.order_reads == 2
    assert guard.shutdowns == ["new holding outside the roster: BBB 5"]


@pytest.mark.parametrize("quantity", ["0.5", "-0.5"])
def test_start_predicate_skips_fractional_positions_and_keeps_the_other_holdings(quantity):
    start = w.start_account({"AAA": Decimal(10), "BBB": Decimal(quantity)}, {})
    assert start.held == {"AAA": Decimal(10)}
    assert start.refused == {}
    assert set(start.skipped) == {"BBB"}
    assert "BBB" in start.skipped["BBB"] and "fractional" in start.skipped["BBB"]


def native_clients(monkeypatch, account):
    """Keep the builder, mass-status collection and mapper; replace only venue transports."""
    starts, mapper_errors, commands = [], [], []

    class Recording(OwnerProtector):
        def on_start(self):
            starts.append(self)
            super().on_start()

        def on_quote(self, quote):
            super().on_quote(quote)
            if h.AAA in self.slots:
                self.shutdown_system("synthetic AAA protection observed")

    class Execution(h.SyntheticExecution):
        async def _generate_order_status_reports(self, command):
            commands.append(command.instrument_id)
            try:
                return [
                    order_status_report(order, self.clock.timestamp_ns(), trailing=True)
                    for order in account.orders
                ]
            except ValueError as exc:
                mapper_errors.append(str(exc))
                raise

        async def _generate_position_status_reports(self, command):
            now = self.clock.timestamp_ns()
            return [
                h.PositionStatusReport(
                    h.AccountId("ALPACA-001"),
                    h.AAA,
                    h.PositionSide.LONG,
                    h.Quantity.from_int(10),
                    now - h.SECOND,
                    now,
                    avg_px_open=Decimal("10.00"),
                )
            ]

    class DataFactory(h.DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return h.SyntheticData(**kwargs, venue=h.Venue("ALPACA"))

    class ExecutionFactory(h.ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Execution(
                **kwargs,
                venue=h.Venue("ALPACA"),
                account_id=h.AccountId("ALPACA-001"),
                account_type=h.AccountType.CASH,
                oms_type=h.OmsType.NETTING,
            )

    monkeypatch.setattr(paper_node, "AlpacaDataClientFactory", DataFactory)
    monkeypatch.setattr(paper_node, "AlpacaExecutionClientFactory", ExecutionFactory)
    monkeypatch.setattr(p, "OwnerProtector", Recording)
    builder = paper_node.alpaca_paper_builder
    monkeypatch.setattr(
        paper_node,
        "alpaca_paper_builder",
        lambda *args, **kwargs: builder(*args, **kwargs, delay_post_stop_secs=0),
    )
    return starts, mapper_errors, commands


def run_node(node, ends=None):
    async def run():
        async with asyncio.timeout(15):
            await node.run_async()

    asyncio.run(run())


def config(tmp_path):
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.mkdir()
    return p.ProtectorRun(
        account="protect", row="OWN1", holds_dir=holds, inbox=inbox, lead_go="protect OWN1"
    )


def test_fractional_other_position_starts_aaa_and_names_bbb(
    count_settings, tmp_path, monkeypatch, capfd
):
    account = Account(fractional_position=True)
    starts, _errors, _commands = native_clients(monkeypatch, account)
    clock, calendar = n.market("2026-11-13T10:00:00")
    monkeypatch.setattr(w, "session_now", lambda trading: (clock, calendar, w.time.monotonic()))
    monkeypatch.setattr(p, "_book_settings", lambda settings: count_settings)
    monkeypatch.setattr(w, "_run_node", run_node)
    p.run_protector(config(tmp_path), trading=cast(Any, account), lock_dir=tmp_path)
    assert len(starts) == 1 and starts[0].settings.roster == (h.AAA,)
    logging_sync_to_disk()
    logs = capfd.readouterr()
    lines = (logs.out + logs.err).splitlines()
    assert any("BBB" in line and "fractional" in line and "skipped" in line for line in lines)
    assert any(
        "BBB" in line and "fractional" in line and "left unprotected by this node" in line
        for line in lines
    )


def test_unprotected_order_blocks_mass_status_even_outside_reconciliation_roster(
    count_settings, tmp_path, monkeypatch, capfd
):
    """Adapter-unit characterization: a BBB order prevents even an AAA-only node start."""
    account = Account(bad_order=True)
    starts, mapper_errors, commands = native_clients(monkeypatch, account)
    gc.collect()
    node, strategy = p.protector_node(config(tmp_path), (h.AAA,), feed="sip", book=count_settings)
    assert strategy.settings.roster == (h.AAA,)
    try:
        with pytest.raises(RuntimeError, match="Failed to get mass status from ALPACA") as refused:
            run_node(node)
    finally:
        node.dispose()
    assert starts == []
    assert commands == [None], "mass-status asks for all symbols before the roster filter"
    assert len(mapper_errors) == 1 and "quantity 0.5 is fractional" in mapper_errors[0]
    logging_sync_to_disk()
    logs = capfd.readouterr()
    assert "Python on_" not in logs.out + logs.err
    print(f"STOP: {count_settings} roster=AAA strategy_starts={len(starts)} engine={refused.value}")
    print(f"MAPPER: {mapper_errors[0]}")
