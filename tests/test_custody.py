"""Custody offline: the lineage classifier and `paper:account`'s read (src/canon_trading/custody.py).

No test here opens a network connection or reads a credential. Alpaca's answers are alpaca-py 0.45.0's own
models, validated from made-up payloads; the task's shell body runs with a stand-in `security` and `uv`.
"""

import os
import re
import subprocess
import tomllib
from collections import deque
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import MarketOrderRequest

from canon_trading import custody
from canon_trading import paper_node
from canon_trading import session_window

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_NUMBER = "PA3MADEUP0001"
ACCOUNT_ID = str(uuid4())

OURS = [
    "O-20261111-165400-001-001-1",  # SessionMaCross in a window, as its record names it (2026-11-11)
    "O-20261111-150428-001-001-3",
    "O-20261111-150428-001-001-1234",
    "canon-close-20261111T150424Z-1-6168c4fe",  # the account check's close (ADR 0060)
]
FOREIGN = [
    str(uuid4()),  # what Alpaca generates when no ID is sent (the dashboard, another tool)
    "O-20261111-165400-002-001-1",  # another trader's tag
    "O-20261111-165400-001-CAP-1",  # another strategy's tag
    "O-20261111-165400-001-001-",  # no count
    "o-20261111-165400-001-001-1",
    "x O-20261111-165400-001-001-1",
    "O-20261111-165400-001-001-1 ",
    "canon-close-",  # the prefix alone
    "canon-close-manual",  # the prefix, typed by anyone
    "canon-close-hand-20261111T150424Z-1-6168c4fe",  # a form no code writes
    "canon-close-20261111T150424Z-1-6168C4FE",
    "O-\u0662\u0660\u0662\u0666\u0661\u0661\u0661\u0661-165400-001-001-1",  # digits, but not ASCII
    "canon-resend-0001",
    "my-order-7",
]
MISSING: list[Any] = [None, "", "   "]
CONFIGURATION: dict[str, Any] = {
    "no_shorting": True,
    "suspend_trade": False,
    "fractional_trading": True,
    "max_margin_multiplier": "4",
    "trade_confirm_email": "all",
    "ptp_no_exception_entry": False,
    "max_options_trading_level": 3,
    "disable_overnight_trading": False,
}
"""`GET /v2/account/configurations` as alpaca-py 0.45.0's `AccountConfiguration` reads it (the same
made-up answer as `tests/test_session_window.py`'s)."""


@pytest.mark.parametrize("client_order_id", OURS)
def test_our_client_order_ids_are_ours(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.OURS


@pytest.mark.parametrize("client_order_id", FOREIGN)
def test_any_other_client_order_id_is_foreign(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.FOREIGN


@pytest.mark.parametrize("client_order_id", MISSING)
def test_an_order_with_no_client_order_id_is_never_ours(client_order_id) -> None:
    assert custody.lineage(client_order_id) == custody.MISSING


def test_the_lineage_is_the_one_our_nodes_and_checks_are_built_with() -> None:
    """The pattern's parts are the ones the code that writes the IDs uses: `paper_node.TRADER_ID`'s tag,
    the order ID tag each order-sending strategy is given, and the account check's close prefix."""
    assert custody.TRADER_TAG == "001" and paper_node.TRADER_ID.value == "TESTER-001"
    instrument = session_window.WindowConfig(
        session="regular", log_dir=ROOT, symbol="SPY"
    ).instrument_id
    assert paper_node.ema_cross_config(instrument).order_id_tag == custody.ORDER_ID_TAG
    source = (ROOT / "src/canon_trading/session_window.py").read_text()
    assert source.count(f'order_id_tag="{custody.ORDER_ID_TAG}"') == 1
    assert session_window.CLOSE_ID_PREFIX == custody.CHECK_CLOSE_PREFIX


def _order(**fields: Any) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": OURS[0],
        "created_at": "2026-11-11T16:54:00Z",
        "updated_at": "2026-11-11T16:54:00Z",
        "submitted_at": "2026-11-11T16:54:00Z",
        "asset_id": str(uuid4()),
        "symbol": "SPY",
        "asset_class": "us_equity",
        "qty": "1",
        "filled_qty": "0",
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": "680.05",
        "status": "new",
        "extended_hours": True,
    }
    return Order.model_validate({**payload, **fields})


def _position(**fields: Any) -> Position:
    payload = {
        "asset_id": str(uuid4()),
        "symbol": "SPY",
        "exchange": "ARCA",
        "asset_class": "us_equity",
        "avg_entry_price": "680.12",
        "qty": "1",
        "side": "long",
        "market_value": "681.00",
        "cost_basis": "680.12",
        "unrealized_pl": "0.88",
        "unrealized_plpc": "0.0013",
        "unrealized_intraday_pl": "0.88",
        "unrealized_intraday_plpc": "0.0013",
        "current_price": "681.00",
        "lastday_price": "679.00",
        "change_today": "0.003",
    }
    return Position.model_validate({**payload, **fields})


class Paper:
    """A stand-in for the paper `TradingClient` that answers the four reads and records each call."""

    def __init__(self, *, positions=(), orders=(), no_shorting=True) -> None:
        self.positions = list(positions)
        self.orders = list(orders)
        self.no_shorting = no_shorting
        self.calls: list[tuple[str, Any]] = []

    def get_account(self):
        self.calls.append(("get_account", None))
        return TradeAccount.model_validate(
            {
                "id": ACCOUNT_ID,
                "account_number": ACCOUNT_NUMBER,
                "status": "ACTIVE",
                "currency": "USD",
                "buying_power": "199318.76",
                "cash": "99319.88",
                "portfolio_value": "100000.88",
                "equity": "100000.88",
            }
        )

    def get_all_positions(self):
        self.calls.append(("get_all_positions", None))
        return self.positions

    def get_orders(self, filter=None):
        self.calls.append(("get_orders", filter))
        return self.orders

    def get_account_configurations(self):
        self.calls.append(("get_account_configurations", None))
        return AccountConfiguration.model_validate(
            {**CONFIGURATION, "no_shorting": self.no_shorting}
        )


def data_only_unset(monkeypatch) -> None:
    """Data-only mode off for the test, and whatever was there restored after it, even when the code
    under test sets it (a `setenv` is recorded; a `delenv` of an absent variable is not)."""
    monkeypatch.setenv(custody.DATA_ONLY_ENV, "0")
    monkeypatch.delenv(custody.DATA_ONLY_ENV)


checked: list = []


def test_the_account_read_names_balances_positions_orders_lineage_and_no_shorting(
    capsys, monkeypatch
) -> None:
    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", checked.append)
    ours, foreign, missing = (
        _order(symbol="SPY", side="sell", type="limit", limit_price="679.95"),
        _order(client_order_id=FOREIGN[0], symbol="AAPL", type="market", limit_price=None),
        _order(client_order_id="", symbol="AAPL", side="sell"),
    )
    paper = Paper(positions=[_position()], orders=[foreign, ours, missing])
    checked.clear()
    assert custody.account_task(cast(Any, paper)) == 0
    out = capsys.readouterr().out
    assert checked == [paper]  # checked to refuse all but GET before any read
    assert os.environ[custody.DATA_ONLY_ENV] == "1"
    assert out.splitlines() == [
        "equity: 100000.88",
        "cash: 99319.88",
        "buying_power: 199318.76",
        "positions: 1",
        "  SPY long 1 avg_entry 680.12 market_value 681.00 unrealized_pl 0.88",
        "open orders: 3",
        "  AAPL buy 1 market limit - status new foreign",
        "  AAPL sell 1 limit limit 680.05 status new missing",
        "  SPY sell 1 limit limit 679.95 status new ours",
        "no_shorting: true",
    ]
    for identifier in (ACCOUNT_NUMBER, ACCOUNT_ID, str(ours.id), *OURS[:1], FOREIGN[0]):
        assert identifier not in out
    (_, request), *_ = [c for c in paper.calls if c[0] == "get_orders"]
    assert request.status.value == "open" and request.limit == 500


def test_a_failed_read_is_named_without_its_body_and_exits_1(capsys, monkeypatch) -> None:
    from alpaca.common.exceptions import APIError

    class Refused(Paper):
        def get_all_positions(self):
            raise APIError(f'{{"code": 40110000, "message": "{ACCOUNT_NUMBER} is not allowed"}}')

    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", lambda client: None)
    assert custody.account_task(cast(Any, Refused())) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "paper:account: the positions failed: APIError" in captured.err
    assert ACCOUNT_NUMBER not in captured.err


def test_the_account_read_builds_a_client_that_refuses_all_but_get(monkeypatch, capsys) -> None:
    """With no client given, the task builds `session_window.paper_trading`'s, in data-only mode: a POST
    is refused before anything is sent (ADR 0070's adapter check)."""
    from canon_alpaca.providers import MethodRefused

    monkeypatch.setenv("ALPACA_API_KEY", "made-up-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "made-up-secret")
    data_only_unset(monkeypatch)
    built: list = []
    real = session_window.paper_trading

    def spy():
        client = real()
        built.append(client)

        def stop() -> None:
            raise OSError("stopped before any request")

        monkeypatch.setattr(
            client, "get_account", stop
        )  # the first read: nothing reaches the network
        return client

    monkeypatch.setattr(session_window, "paper_trading", spy)
    assert custody.account_task() == 1
    (client,) = built
    with pytest.raises(MethodRefused):
        client.submit_order(
            MarketOrderRequest(symbol="SPY", qty=1, side="buy", time_in_force="day")
        )
    err = capsys.readouterr().err
    assert "the account failed: OSError" in err and "made-up" not in err


def _task_body() -> str:
    return tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:account"]["run"]


def _run_task(tmp_path: Path, account: str) -> tuple[subprocess.CompletedProcess, str]:
    """The task's own body with `/usr/bin/security` and `uv` swapped for stand-ins: `security` logs its
    arguments and prints a made-up value; `uv` prints what it was asked and the flags it was given."""
    log = tmp_path / "security.log"
    stub = tmp_path / "security"
    stub.write_text(f'#!/bin/bash\necho "$*" >> {log}\necho "made-up-value-$5"\n')
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/bash\necho "uv $*"\n'
        'echo "data-only=$CANON_ALPACA_DATA_ONLY account=$CANON_PAPER_ACCOUNT"\n'
        '[ -n "$ALPACA_API_KEY" ] && [ -n "$ALPACA_SECRET_KEY" ] && echo "pair given"\n'
    )
    stub.chmod(0o755)
    uv.chmod(0o755)
    body = _task_body().replace("/usr/bin/security", str(stub))
    done = subprocess.run(
        ["bash", "-c", body],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "usage_account": account},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, log.read_text() if log.exists() else ""


@pytest.mark.skipif(not Path("/usr/bin/timeout").exists(), reason="no /usr/bin/timeout here")
@pytest.mark.parametrize(
    ("account", "service"),
    [("tests", "canon-alpaca-paper"), ("windows", "canon-alpaca-paper-windows")],
)
def test_the_task_reads_one_named_accounts_pair_in_data_only_mode_and_prints_neither_value(
    tmp_path, account, service
) -> None:
    done, asked = _run_task(tmp_path, account)
    assert done.returncode == 0, done.stderr
    assert asked.splitlines() == [
        f"find-generic-password -s {service} -a ALPACA_API_KEY -w",
        f"find-generic-password -s {service} -a ALPACA_SECRET_KEY -w",
    ]
    assert done.stdout.splitlines() == [
        f"account: {account}",
        (
            "uv run --locked python -c from canon_trading.custody import account_task; "
            "raise SystemExit(account_task())"
        ),
        f"data-only=1 account={account}",
        "pair given",
    ]
    assert "made-up" not in done.stdout + done.stderr


def test_the_task_refuses_any_other_account_before_asking_the_keychain(tmp_path) -> None:
    done, asked = _run_task(tmp_path, "live")
    assert done.returncode == 3 and asked == ""
    assert "no paper account named 'live'" in done.stderr


def test_the_task_takes_no_lock_and_names_its_two_accounts() -> None:
    """GET only, so no lock (the task's note); mise's own `choices` refuses any other account."""
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:account"]
    assert 'choices "tests" "windows"' in task["usage"]
    assert "lockf" not in task["run"]
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", task["run"])


def test_a_client_built_before_data_only_mode_is_refused_before_any_read(
    monkeypatch, capsys
) -> None:
    """A client given to the task that would send more than GET is refused before anything is read."""
    monkeypatch.setenv("ALPACA_API_KEY", "made-up-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "made-up-secret")
    data_only_unset(monkeypatch)
    client = session_window.paper_trading()  # outside data-only mode: its adapter sends any method
    monkeypatch.setattr(client, "get_account", lambda: pytest.fail("Alpaca was read"))
    assert custody.account_task(client) == 1
    assert "would send a method other than GET" in capsys.readouterr().err


def test_the_account_checks_own_close_ids_read_as_ours(monkeypatch) -> None:
    """The ID the check after a window's stop writes, made by its own `_close` with the stamp
    `run_window` gives its report (the form a paper record showed, 2026-11-11), is ours."""
    stamp = datetime(2026, 11, 11, 15, 4, 24, tzinfo=UTC).strftime("%Y%m%dT%H%M%SZ")
    sent: list = []
    check = SimpleNamespace(
        held=Decimal(1),
        closes_sent=0,
        symbol="SPY",
        requests={},
        turn=deque(),
        report=SimpleNamespace(started=stamp, closes={}, closes_not_placed=0),
        trading=SimpleNamespace(submit_order=lambda r: sent.append(r) or SimpleNamespace(id=None)),
    )
    for _ in range(3):
        session_window._AccountCheck._close(check, "sell")  # ty: ignore[invalid-argument-type]
    ids = [r.client_order_id for r in sent]
    assert len(set(ids)) == 3 and all(custody.lineage(i) == custody.OURS for i in ids)
    source = (ROOT / "src/canon_trading/session_window.py").read_text()
    assert (
        'stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")' in source
    )  # the report's `started`


def test_a_listing_with_something_other_than_orders_is_a_named_read_failure(
    capsys, monkeypatch
) -> None:
    data_only_unset(monkeypatch)
    monkeypatch.setattr(custody, "require_get_only", lambda client: None)
    paper = Paper(positions=[_position()], orders=[{"symbol": "SPY"}])
    assert custody.account_task(cast(Any, paper)) == 1
    assert (
        "the open orders answered something other than a list of Order" in capsys.readouterr().err
    )
