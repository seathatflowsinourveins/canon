"""Review F1 of #443's max review (the lead's ruling, 2026-11-13 22:47 and 22:52): a holding first bought
after the protector's start, in a symbol the node did not load, is named at once by an ALERT and has
its exit within a named bound, never left to the run's end.

The fault, reproduced by L6 at a9c5afce on the real adapter, the pinned engine and the adapter tests'
in-memory venue: the node's instruments are the account's symbols read once at start
(`protector_node.run_protector`), and the adapter reports no fill or position in an instrument it did
not load (`packages/canon-alpaca/src/canon_alpaca/execution.py:369`), so an owner's later buy in another
symbol never reaches the node's cache and nothing names it until the stop-time read.

The form chosen is the brief's (b): the run ends by name, `new holding outside the roster`, so that
the runner's successor reads the account fresh at its start. The protector reads the account's
positions (`GET /v2/positions`, alpaca-py's `get_all_positions`, through `session_window.position_reads`)
at its first step and every `ROSTER_POLL_SECS` after, and ends the run with the pinned
`Strategy.shutdown_system` (nautilus-trader d53849b216e2 `python/nautilus_trader/trading/__init__.pyi:525`;
the live node shuts down on it, `crates/live/src/node/mod.rs:1636-1641`, as the backtest engine ends on
it, `crates/backtest/src/engine.rs:882-884`).

These cases run on the pinned `BacktestEngine` (`tests.test_mover.engine`, its AAA and BBB). The
account's GET is a stand-in answering alpaca-py `Position` models (`tests.test_protect._position`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.models import TradeUpdate
from canon_alpaca.execution import AlpacaExecutionClientConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from canon_trading import owner_protector
from canon_trading import session_window
from tests.test_mover import AAA
from tests.test_mover import BBB
from tests.test_mover import SECOND
from tests.test_mover import engine
from tests.test_owner_protector_blind import owner_type
from tests.test_owner_protector_blind import quote
from tests.test_protect import _order
from tests.test_protect import _position

EXTERNAL = StrategyId("EXTERNAL")
HANDLER_ERROR = re.compile(r"Python on_\w+ failed")
BOUND_SECS = 120
"""The bound the unit names: from the owner's fill to the run's end by name, two minutes or less."""


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Override conftest's credential-environment reader for this entirely offline file."""


class Account:
    """The account's `GET /v2/positions`, as alpaca-py's `TradingClient.get_all_positions` answers it.
    `held` is what the venue holds now, by symbol; `failure`, if set, is raised instead."""

    def __init__(self, **held: str) -> None:
        self.held = dict(held)
        self.unreadable: set[str] = set()
        self.failure: Exception | None = None
        self.reads = 0
        self.orders: list[Any] = []

    def get_all_positions(self) -> list[Any]:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        return [
            # "-1" shares no order holds is a listing custody does not accept (tests/test_protect.py).
            _position(symbol, qty, available="-1" if symbol in self.unreadable else "all")
            for symbol, qty in sorted(self.held.items())
        ]

    def get_orders(self, request) -> list[Any]:
        return list(self.orders)


class Owner(Strategy):
    """The owner, under EXTERNAL: buys `held` AAA at its first AAA quote and `later` BBB at its first
    BBB quote, each fill shown on the account's GET as it lands (what the event carries, nothing else)."""

    def __init__(self, account: Account, held: int = 10, later: int = 5) -> None:
        super().__init__(StrategyConfig(strategy_id=EXTERNAL, oms_type=OmsType.NETTING))
        self.account = account
        self.held = held
        self.later = later
        self.bought: set[InstrumentId] = set()
        self.filled_at: dict[str, int] = {}

    def on_start(self) -> None:
        self.subscribe_quotes(AAA)
        self.subscribe_quotes(BBB)

    def on_quote(self, quote) -> None:
        if quote.instrument_id in self.bought:
            return
        self.bought.add(quote.instrument_id)
        size = self.held if quote.instrument_id == AAA else self.later
        self.submit_order(
            self.order_factory.limit(
                quote.instrument_id,
                OrderSide.BUY,
                Quantity.from_int(size),
                quote.ask_price,
                time_in_force=TimeInForce.GTC,
            )
        )

    def on_order_filled(self, event) -> None:
        symbol = event.instrument_id.symbol.value
        before = Decimal(self.account.held.get(symbol, "0"))
        self.account.held[symbol] = str(before + event.last_qty.as_decimal())
        self.filled_at.setdefault(symbol, event.ts_event)


def protector(account: Account | None, **changes: Any):
    configure, recording = owner_type()

    class Said(recording):
        def __init__(self, config) -> None:
            self.said: list[tuple[int, str]] = []
            self.shutdowns: list[tuple[int, str | None]] = []
            super().__init__(config)
            # No supplied level: the holding self-arms, as a start with no level or book does.
            self.supplied_for_holding = True

        def _say(self, text: str) -> None:
            self.said.append((self.clock.timestamp_ns(), text))
            super()._say(text)

        def shutdown_system(self, reason: str | None = None) -> None:
            self.shutdowns.append((self.clock.timestamp_ns(), reason))
            super().shutdown_system(reason)

    settings: dict[str, Any] = {"feed": "sip", "levels": {}, "trading": account}
    settings.update(changes)
    return Said(configure(**settings))


def run(strategies: tuple[Any, ...], quotes: list[Any]):
    eng = engine(liquid=True)
    for strategy in strategies:
        eng.add_strategy(strategy)
    eng.add_data(quotes)
    eng.run(streaming=True)
    return eng


def tape(seconds: range, *, later_at: int | None) -> list[Any]:
    """AAA every second; BBB every second from `later_at` (the owner's buy there), none before."""
    data = [quote(s, "10.00") for s in seconds]
    if later_at is not None:
        data += [quote(s, "20.00", instrument_id=BBB) for s in seconds if s >= later_at]
    return sorted(data, key=lambda tick: (tick.ts_event, str(tick.instrument_id)))


def alerts(strategy, symbol: str) -> list[tuple[int, str]]:
    return [(at, line) for at, line in strategy.said if "ALERT:" in line and symbol in line]


def test_a_long_first_bought_after_the_start_outside_the_roster_is_named_and_ends_the_run(
    capfd,
) -> None:
    """The red case: the roster is AAA, the owner buys BBB 40 s into the run. One ALERT names BBB and
    its quantity, and the run ends by name through ShutdownSystem, both within the bound; the
    protector never arms the holding it cannot see live, and no handler error is logged."""
    account = Account()
    owner = Owner(account)
    guard = protector(account, roster=(AAA,))
    eng = run((owner, guard), tape(range(200), later_at=40))
    try:
        bought = owner.filled_at["BBB"]
        named = alerts(guard, "BBB")
        assert len(named) == 1, guard.said
        at, line = named[0]
        assert line.startswith(f"{BBB}: ALERT: new holding outside the roster: 5 held")
        assert 0 <= at - bought <= BOUND_SECS * SECOND
        assert guard.shutdowns, "the run never ended by name"
        ended_at, reason = guard.shutdowns[0]
        assert reason == "new holding outside the roster: BBB 5"
        assert guard.ended_for == reason
        assert ended_at - bought <= BOUND_SECS * SECOND
        assert len(guard.shutdowns) == 1
        assert all("ALERT:" in line for _at, line in guard.said if str(BBB) in line), (
            "a holding outside the roster is never armed by this run"
        )
        assert any(
            line.startswith(f"{AAA}: no carried level") and "self-armed" in line
            for at, line in guard.said
            if at < ended_at
        ), "the roster's own holding is protected until the end"
    finally:
        eng.end()
        eng.dispose()
    logged = capfd.readouterr()
    errors = [line for line in (logged.out + logged.err).splitlines() if HANDLER_ERROR.search(line)]
    assert not errors, errors


def test_the_account_is_read_at_the_first_step_then_every_poll_interval() -> None:
    """A buy between the node's start read and the strategy's start is caught at the first step; a
    roster holding alone never ends the run, and the account is read once per interval."""
    account = Account()
    owner = Owner(account)
    guard = protector(account, roster=(AAA,))
    eng = run((owner, guard), tape(range(95), later_at=None))
    try:
        assert not guard.shutdowns
        assert guard.ended_for is None
        # Steps from the first second to the ninety-fourth: reads at the first, +30, +60, +90.
        assert account.reads == 1 + 94 // owner_protector.ROSTER_POLL_SECS
    finally:
        eng.end()
        eng.dispose()


def test_a_fractional_new_holding_is_named_and_the_run_goes_on() -> None:
    """A successor skips a fractional holding by name, so it cannot justify ending this run."""
    account = Account(AAA="10", BBB="2.5")
    guard = protector(account, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(70), later_at=None))
    try:
        named = alerts(guard, "BBB")
        assert len(named) == 1, guard.said
        assert "fractional" in named[0][1] and "2.5 held" in named[0][1]
        assert not guard.shutdowns and guard.ended_for is None
    finally:
        eng.end()
        eng.dispose()


def test_an_unreadable_new_holding_is_named_and_the_run_goes_on() -> None:
    """A successor refuses to start on a position it cannot read (`session_window.positions`), so the
    run goes on, and the holding is named."""
    account = Account(AAA="10", BBB="7")
    account.unreadable.add("BBB")
    guard = protector(account, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(70), later_at=None))
    try:
        named = alerts(guard, "BBB")
        assert len(named) == 1, guard.said
        assert "unreadable" in named[0][1]
        assert not guard.shutdowns
    finally:
        eng.end()
        eng.dispose()


def _ended(account: Account, seconds: int = 70) -> tuple[list[tuple[int, str | None]], Any]:
    guard = protector(account, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(seconds), later_at=None))
    try:
        return list(guard.shutdowns), guard
    finally:
        eng.end()
        eng.dispose()


def _start_refuses(account: Account) -> dict[str, str]:
    """What the start's own predicate refuses in this account (`session_window.start_account`)."""
    held, unread = session_window.position_reads(cast(Any, account))
    return session_window.start_account(held, unread, account.get_orders(None)).refused


def _unread(account: Account, symbol: str) -> Account:
    account.unreadable.add(symbol)
    return account


def _unmappable(account: Account) -> Account:
    account.orders = [_order("BBB", "0.5", "synthetic-owner-fractional-buy", side="buy")]
    return account


START_CASES = {
    "accepted: a new whole-share long alone": (lambda: Account(AAA="10", CCC="5"), False),
    "accepted: an unreadable long outside the roster, which the start skips": (
        lambda: _unread(Account(AAA="10", BBB="7", CCC="5"), "BBB"),
        False,
    ),
    "accepted: a roster long turned unreadable, which the start skips": (
        lambda: _unread(Account(AAA="10", CCC="5"), "AAA"),
        False,
    ),
    "accepted: a zero quantity, which the start skips": (
        lambda: Account(AAA="10", BBB="0", CCC="5"),
        False,
    ),
    "accepted: a fractional long outside the roster is skipped": (
        lambda: Account(AAA="10", BBB="2.5", CCC="5"),
        False,
    ),
    "accepted: a roster long turned fractional is skipped": (
        lambda: Account(AAA="10.5", CCC="5"),
        False,
    ),
    "accepted: a fractional short is skipped": (
        lambda: Account(AAA="10", BBB="-1.5", CCC="5"),
        False,
    ),
    "refused: an unmappable open order in an unheld symbol": (
        lambda: _unmappable(Account(AAA="10", CCC="5")),
        True,
    ),
}


@pytest.mark.parametrize("case", sorted(START_CASES))
def test_a_new_whole_share_long_ends_the_run_exactly_when_the_start_would_accept_the_account(
    case,
) -> None:
    """The lead's term (a), 2026-11-13 23:49: the run asks the start's own predicate. For every account
    the start refuses, the run does not end by name; for every account it accepts, a whole-share long
    first bought outside the roster ends it."""
    make, refused = START_CASES[case]
    assert bool(_start_refuses(make())) is refused
    shutdowns, guard = _ended(make())
    if refused:
        assert not shutdowns and guard.ended_for is None, guard.said
    else:
        assert [reason for _at, reason in shutdowns] == ["new holding outside the roster: CCC 5"], (
            guard.said
        )


def test_the_run_calls_the_starts_predicate_and_the_adapters_whole_share_rule(monkeypatch) -> None:
    """Called, never copied: when the predicate, or the adapter's whole-share rule it calls, accepts a
    fractional position instead of skipping it, the run ends at once for that new holding."""
    from canon_alpaca import execution

    assert not _ended(Account(AAA="10", BBB="2.5"))[0]
    with monkeypatch.context() as patch:
        accepted = session_window.StartAccount
        patch.setattr(
            session_window,
            "start_account",
            lambda held, unread, orders=(): accepted(dict(held), dict(unread), {}),
        )
        assert _ended(Account(AAA="10", BBB="2.5"))[0]
    with monkeypatch.context() as patch:
        patch.setattr(execution, "whole_quantity", lambda value, field: value)
        assert _ended(Account(AAA="10", BBB="2.5"))[0]


def test_a_new_long_the_restart_cannot_take_is_named_unprotected_once_and_the_monitor_shows_it_uncovered() -> (
    None
):
    """The lead's term (c): one ALERT names the new holding as unprotected and what blocks the restart,
    across every read of the run, and the exit monitor shows it uncovered."""
    from canon_trading import exit_monitor

    account = _unmappable(Account(AAA="10", BBB="2.5", CCC="5"))
    shutdowns, guard = _ended(account, seconds=95)
    assert not shutdowns and account.reads >= 3
    named = alerts(guard, "CCC")
    assert len(named) == 1, guard.said
    line = named[0][1]
    assert "unprotected" in line and "5 held" in line, line
    assert "BBB" in line and "quantity 0.5 is fractional" in line, line
    findings = exit_monitor.assess(
        "A",
        account.get_all_positions(),
        account.get_orders(None),
        [],
        exit_monitor.empty_memory("A"),
        datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("America/New_York")),
    )
    (ccc,) = [finding for finding in findings if finding["symbol"] == "CCC"]
    assert ccc["state"] == "uncovered", ccc


def test_the_run_ends_at_the_first_read_the_start_would_accept() -> None:
    """The lead's term (b): decided at every read, never latched. Once the unmappable order leaves the
    account, the next read lets the run end."""

    class Rounds(Account):
        def get_orders(self, request) -> list[Any]:
            listed = super().get_orders(request)
            self.orders.clear()
            return listed

    account = _unmappable(Rounds(AAA="10", BBB="3", CCC="5"))
    shutdowns, guard = _ended(account)
    assert account.reads >= 2
    assert [reason for _at, reason in shutdowns] == [
        "new holding outside the roster: BBB 3, CCC 5"
    ], guard.said
    assert shutdowns[0][0] - alerts(guard, "CCC")[0][0] >= owner_protector.ROSTER_POLL_SECS * SECOND


def test_the_start_skips_a_fractional_holding_by_name_and_starts_the_rest(
    controls, tmp_path, monkeypatch, capsys
) -> None:
    """The start calls the same predicate: a fractional position is named and excluded from the node,
    so the whole-share holding can start."""
    from tests.test_protector_node import Listing
    from tests.test_protector_node import listed
    from tests.test_protector_node import real_reader

    module, settings, _strategy, _foreign, _reads, built = real_reader(monkeypatch, controls)
    trading = Listing(listed("AAA", "10"), listed("BBB", "2.5"))
    module.run_protector(settings, trading=trading, lock_dir=tmp_path)
    assert len(built) == 1 and built[0][1] == (AAA,)
    lines = capsys.readouterr().out.splitlines()
    assert any("BBB" in line and "fractional" in line and "skipped" in line for line in lines)
    assert any(
        "BBB" in line and "fractional" in line and "left unprotected by this node" in line
        for line in lines
    )


def test_a_failed_read_is_an_alert_and_is_read_again_at_the_next_interval() -> None:
    account = Account()
    account.failure = ConnectionError("no route")
    guard = protector(account, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(70), later_at=None))
    try:
        failed = [line for _at, line in guard.said if "positions could not be read" in line]
        assert len(failed) == account.reads == 3, guard.said
        assert all("ALERT:" in line for line in failed)
        assert not guard.shutdowns
    finally:
        eng.end()
        eng.dispose()


def test_a_short_outside_the_roster_does_not_end_the_run() -> None:
    """The protector never covers a short; only a long is a holding it would protect."""
    account = Account(AAA="10", BBB="-3")
    guard = protector(account, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(40), later_at=None))
    try:
        assert not guard.shutdowns and not alerts(guard, "BBB")
    finally:
        eng.end()
        eng.dispose()


def test_with_no_roster_the_account_is_not_read() -> None:
    """A bare build, as every earlier test makes, reads nothing new."""
    account = Account(AAA="10", BBB="5")
    guard = protector(account)
    eng = run((Owner(Account()), guard), tape(range(40), later_at=None))
    try:
        assert account.reads == 0 and not guard.shutdowns
    finally:
        eng.end()
        eng.dispose()


def test_a_roster_with_no_reader_is_named_once() -> None:
    guard = protector(None, roster=(AAA,))
    eng = run((Owner(Account()), guard), tape(range(70), later_at=None))
    try:
        unread = [line for _at, line in guard.said if "no account reader" in line]
        assert len(unread) == 1 and unread[0].startswith("ALERT:"), guard.said
    finally:
        eng.end()
        eng.dispose()


def test_the_roster_must_name_instruments() -> None:
    configure, _recording = owner_type()
    with pytest.raises(TypeError, match="roster"):
        configure(levels={}, roster=("AAA",))


def test_the_node_hands_its_roster_to_the_protector(monkeypatch, controls) -> None:
    from tests.test_protector_node import config
    from tests.test_protector_node import subject

    module = subject()
    node, strategy = module.protector_node(config(controls), (AAA, BBB), feed="sip")
    try:
        assert strategy.settings.roster == (AAA, BBB)
    finally:
        node.dispose()


def test_a_run_ended_by_name_says_so_and_returns_without_a_failure(
    controls, tmp_path, monkeypatch, capsys
) -> None:
    """The runner launches the successor when the run exits; a failure must not be what says it."""
    from tests.test_protector_node import runtime
    from tests.test_protector_node import window

    module, settings, strategy, _foreign, _reads, _built = runtime(monkeypatch, controls)

    def ended(_node, _ends) -> None:
        strategy.ended_for = "new holding outside the roster: CCC 4"
        strategy.on_stop()

    monkeypatch.setattr(window, "_run_node", ended)
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    out = capsys.readouterr()
    assert (
        "paper:protector: ended: new holding outside the roster: CCC 4; the next start reads the "
        "account fresh" in out.out
    )
    assert "failed" not in out.err


@pytest.fixture
def controls(tmp_path):
    """As tests/test_protector_node.py's: an empty holds folder and an empty, read inbox."""
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    return holds, inbox


ADAPTER_TESTS = Path(__file__).resolve().parents[1] / "packages" / "canon-alpaca" / "tests"


def _adapter_module(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ADAPTER_TESTS / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_conf = _adapter_module("canon_f1_adapter_conftest", "conftest.py")
_tec = _adapter_module("canon_f1_adapter_exec_tests", "test_execution_client.py")
alpaca = _conf.alpaca  # the adapter tests' own in-memory venue, as fixtures of this module
venue = _tec.venue
dispose_nodes = _tec.dispose_nodes


def owner_fill(alpaca, symbol: str, cid: str) -> list[TradeUpdate]:
    """An owner's buy of 2 at 150.12, as the trade stream sends it: `new`, then `fill` (L6's form)."""

    def update(event: str, status: str, filled: int, fill_qty: int | None) -> TradeUpdate:
        raw = alpaca.alpaca_order(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, cid)),
            client_order_id=cid,
            status=status,
            symbol=symbol,
            filled_qty=str(filled),
            filled_avg_price="150.12" if filled else None,
        )
        return TradeUpdate.model_validate(
            {
                "event": event,
                "order": raw,
                "timestamp": raw["updated_at"],
                "qty": fill_qty,
                "price": "150.12" if fill_qty is not None else None,
                "execution_id": str(uuid.uuid4()) if fill_qty is not None else None,
            }
        )

    return [update("new", "new", 0, None), update("fill", "filled", 2, 2)]


class LiveProtector(owner_protector.OwnerProtector):
    """The protector as built; `started` is what the adapter tests' `run_with_client` waits on."""

    def __init__(self, config: owner_protector.OwnerProtectorConfig) -> None:
        super().__init__(config)
        self.started = False
        self.said: list[str] = []

    def on_start(self) -> None:
        super().on_start()
        self.started = True

    def _say(self, text: str) -> None:
        self.said.append(text)
        super()._say(text)


def test_on_the_live_node_an_owner_buy_the_adapter_never_reports_is_named_and_shuts_the_node(
    venue, alpaca, monkeypatch, capfd
) -> None:
    """L6's reproduction at a9c5afce, as the property: the real adapter (`report_external_orders`), the
    pinned LiveNode and the adapter tests' in-memory venue. The node loaded AAPL only. The owner's
    MSFT buy never reaches the cache (the fault, unchanged: execution.py:369), and the protector's
    read of the venue's positions names it and the node shuts itself down on ShutdownSystem, not on
    the test's stop."""
    monkeypatch.setattr(owner_protector, "ROSTER_POLL_SECS", 1)
    monkeypatch.setattr(
        _tec,
        "exec_config",
        lambda: AlpacaExecutionClientConfig(
            instrument_provider=_tec.provider_config(), report_external_orders=True
        ),
    )
    guard = LiveProtector(
        owner_protector.OwnerProtectorConfig(
            row="OWN1", feed="sip", trading=venue.trading, roster=(_tec.AAPL,)
        )
    )
    seen: dict[str, Any] = {}

    async def body(client) -> None:
        for update in owner_fill(alpaca, "MSFT", "owner-buy-not-loaded"):
            await client._apply_trade_update(update)
        await _tec.until_idle(client, venue)
        # What the venue's GET /v2/positions lists once the buy filled.
        venue.trading.positions = [
            {**alpaca.POSITION_AAPL},
            {**alpaca.POSITION_AAPL, "asset_id": str(uuid.uuid4()), "symbol": "MSFT", "qty": "2"},
        ]
        seen["open"] = sorted(str(p.instrument_id) for p in client.cache.positions_open())
        async with asyncio.timeout(8):
            while guard.ended_for is None:
                await asyncio.sleep(0.05)
            # The node's stop check runs every 100 ms (crates/live/src/node/mod.rs:1550); wait well past
            # it, so the node stops by itself before run_with_client would stop it by its handle.
            await asyncio.sleep(1.0)

    _tec.run_with_client(body, guard)
    lines = _tec.logged(capfd)
    assert "MSFT.ALPACA" not in seen["open"], "the fault this unit leaves to the successor"
    assert guard.ended_for == "new holding outside the roster: MSFT 2"
    assert [line for line in guard.said if "MSFT" in line and "ALERT:" in line]
    assert "Received ShutdownSystem command, shutting down" in lines
    assert "Received stop signal from handle" not in lines
    assert not HANDLER_ERROR.search(lines), [
        line for line in lines.splitlines() if HANDLER_ERROR.search(line)
    ]
