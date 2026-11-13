"""Custody offline: the lineage classifier and `paper:account`'s read (src/canon_trading/custody.py).

No test here opens a network connection or reads a credential. Alpaca's answers are alpaca-py 0.45.0's own
models, validated from made-up payloads; the task's shell body runs with a stand-in `security` and `uv`.
"""

import os
import re
import signal
import subprocess
import tomllib
from collections import deque
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.models import Quote
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Calendar
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.execution import replacement_client_order_id

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
    "canon-close-hand-20261111T150424Z-2-0123abcd",  # the close-only form's
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
    "canon-close-start-20261111T150424Z-1-6168c4fe",  # a form no code writes yet
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


STAMP = 1_762_999_999_123_456_789
"""A clock reading, as the adapter puts one after the count of a replacement's client order ID."""


@pytest.mark.parametrize("client_order_id", [o for o in OURS if o.startswith("O-")])
def test_a_replacement_our_adapter_sent_reads_as_ours(client_order_id) -> None:
    """R1 (the lead, 2026-11-12): the adapter gives its own replacement the node's ID with a clock
    stamp after the count, which is still an ID of our nodes' form."""
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is not None and sent != client_order_id
    assert custody.lineage(sent) == custody.OURS


def test_a_replacement_a_hand_made_is_foreign_although_it_replaces_ours() -> None:
    """R2: an order replaced by hand carries an ID Alpaca made. It freezes its symbol and is never
    canceled, whatever it replaces: nothing a hand touched is ours to end."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    # the stop of ours it replaced is no longer open, and is not listed
    by_hand = venue.open(
        client_order_id=str(uuid4()), symbol="SPY", side="sell", replaces=str(uuid4())
    )
    report = close(venue)
    assert venue.sent == [] and venue.canceled == []
    assert report.frozen["SPY"] == ["open a foreign order: sell 1 SPY limit, new"]
    assert any(
        o.client_order_id == by_hand.client_order_id and _value(o.status) == "new"
        for o in venue.history
    )


def test_our_own_replacement_of_our_stop_is_canceled_like_any_order_of_ours() -> None:
    """R1 where it is acted on: the replaced stop no longer freezes its symbol. The stop it replaced
    is not listed, as one placed before the lookback is not, and with no fill it carries none."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    replacement = venue.open(
        client_order_id=replacement_client_order_id("O-20261111-145500-001-001-2", STAMP),
        symbol="SPY",
        side="sell",
        replaces=str(uuid4()),
    )
    report = close(venue)
    assert venue.canceled == [replacement.client_order_id]
    assert report.frozen == {}


@pytest.mark.parametrize("client_order_id", FOREIGN)
def test_no_foreign_order_reads_as_ours_through_its_replacement(client_order_id) -> None:
    """R3: the replacement's ID is derived from the order's own, so what was foreign stays foreign."""
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is None or custody.lineage(sent) == custody.FOREIGN


@pytest.mark.parametrize(
    "client_order_id",
    [
        *(o for o in OURS if o.startswith(custody.CHECK_CLOSE_PREFIX)),
        "canon-close-20261111T150424Z-1-12345678",  # a random tail that happens to be all digits
    ],
)
def test_a_replacement_of_a_checks_close_is_never_read_as_ours(client_order_id) -> None:
    """The limit of R1: only an ID of the form a node writes keeps its form through a replace. A
    close the account check or the close-only form placed ends in eight hex digits, so its replacement
    gets Alpaca's ID or, when those digits are all decimal, one that is no longer a close's: foreign
    either way. No node replaces such an order."""
    assert custody.lineage(client_order_id) == custody.OURS
    sent = replacement_client_order_id(client_order_id, STAMP)
    assert sent is None or custody.lineage(sent) == custody.FOREIGN


def chain(
    venue: Venue,
    *filled: str,
    qty: str = "20",
    last: str = "filled",
    side: str = "buy",
    by_hand: tuple[int, ...] = (),
    first: str | None = None,
    first_age: timedelta = timedelta(minutes=10),
) -> None:
    """An order of ours for `qty` replaced once for each further entry of `filled`, as Alpaca lists a
    chain: each replaced order with its own fills and `replaced_by`, each replacement with `replaces`
    and a filled quantity that counts the fills before it (ADR 0042, paper, 2026-11-10). A replacement
    is the adapter's, under the first order's ID with a clock stamp, unless its place is in `by_hand`,
    which gives it an ID Alpaca made. The first order was submitted `first_age` ago, each replacement
    within the last ten minutes."""
    ids = [str(uuid4()) for _ in filled]
    first = first or NODE_BUY
    for n, quantity in enumerate(filled):
        is_last = n == len(filled) - 1
        age = first_age if n == 0 else timedelta(minutes=10 - n)
        venue.open(
            id=ids[n],
            client_order_id=first
            if n == 0
            else str(uuid4())
            if n in by_hand
            else replacement_client_order_id(first, STAMP + n),
            symbol="SPY",
            side=side,
            qty=qty,
            filled_qty=quantity,
            status=last if is_last else "replaced",
            replaces=ids[n - 1] if n else None,
            replaced_by=None if is_last else ids[n + 1],
            submitted_at=(venue.now - age).isoformat(),
        )


def opened(venue: Venue) -> tuple[Decimal | None, str]:
    return custody._opened(cast(Any, venue), "SPY", venue.now)


def test_a_hands_replacement_in_the_middle_of_a_chain_takes_its_own_fills_out_of_ours() -> None:
    """Ours filled 16, a hand replaced it and 2 more filled, and the node replaced that one: the last
    order is ours again and shows 20, the whole chain's. Ours filled 16 and then 2 after the hand's
    18: 18, neither the 36 of the two orders of ours nor the chain's 20."""
    venue = Venue(REGULAR)
    chain(venue, "16", "18", "20", by_hand=(1,))
    assert opened(venue) == (Decimal(18), "")


def test_a_chain_of_our_sells_takes_its_fills_away_once() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 12)  # 20 bought by ours in the lookback, 8 of them sold by the chain
    venue.history[-1] = venue.history[-1].model_copy(update={"qty": "20", "filled_qty": "20"})
    chain(venue, "5", "8", side="sell", first="O-20261111-145500-001-001-2")
    assert opened(venue) == (Decimal(12), "")


def test_an_order_of_our_form_that_replaces_a_foreign_one_counts_only_its_own_fills() -> None:
    """Nothing foreign reads as ours through a chain either: the 5 a foreign order filled stay
    foreign when an order of our form replaces it and shows 8."""
    venue = Venue(REGULAR)
    foreign = str(uuid4())
    venue.open(
        id=foreign, client_order_id=str(uuid4()), filled_qty="5", qty="20", status="replaced"
    )
    venue.open(client_order_id=OURS[1], filled_qty="8", qty="20", status="filled", replaces=foreign)
    assert opened(venue) == (Decimal(3), "")


def test_a_replacement_whose_replaced_order_is_not_listed_freezes_the_symbol() -> None:
    """A buy of ours working for more than the lookback filled 18 before it and was replaced inside
    it: the replacement shows 20, of which only 2 are the lookback's, and the listing does not hold
    the order that says so. With 18 foreign shares held the 20 would match and all be sold. What
    cannot be established freezes."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, "18", "20", first_age=custody.LINEAGE_LOOKBACK + timedelta(hours=1))
    quantity, why = opened(venue)
    assert quantity is None
    assert why == (
        "an order of ours in SPY replaces one the listing of the last 1 day, 0:00:00 does not hold, "
        "whose fills it carries, so the quantity ours opened is not established"
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [why]


@pytest.mark.parametrize("filled", [("18", "20"), ("16", "18", "20"), ("0", "20")])
def test_a_chain_of_our_own_replacements_counts_its_fills_once(filled) -> None:
    """Now that the adapter's replacement reads as ours, a partly filled buy and its replacement are
    both in the count, and the replacement's filled quantity already holds the first one's fills. The
    long of 20 is what the chain opened, not the sum: it is closed, and nothing is frozen."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    chain(venue, *filled)
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 20)


def test_a_chain_never_counts_more_than_it_filled_so_a_foreign_share_is_not_sold() -> None:
    """The case the sum would get wrong the dangerous way: 18 foreign shares beside the 20 a chain of
    ours filled. Summed, ours would read 38, the long would match and the close would sell all of it."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 38, client_order_id=None)
    chain(venue, "18", "20")
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [
        "38 SPY held, but our orders opened 20 in the last 1 day, 0:00:00"
    ]


def test_a_replacement_that_carries_no_fills_leaves_the_replaced_orders_own() -> None:
    """A replacement Alpaca rejected after `replaced` may show no fills: the chain still counts what
    the replaced order filled."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 18, client_order_id=None)
    chain(venue, "18", "0", last="rejected")
    report = close(venue)
    assert report.frozen == {}
    (sent,) = venue.sent
    assert (sent.symbol, sent.qty) == ("SPY", 18)


def test_a_replacement_a_hand_made_takes_none_of_our_replaced_orders_fills() -> None:
    """Ours filled 18 and was then replaced by hand: the replacement is foreign and its fills are
    not ours, so the 18 still count and the 20 held do not match."""
    venue = Venue(REGULAR)
    venue.hold("SPY", 20, client_order_id=None)
    by_hand = str(uuid4())
    venue.open(
        client_order_id=NODE_BUY,
        side="buy",
        qty="20",
        filled_qty="18",
        status="replaced",
        replaced_by=by_hand,
        submitted_at=(venue.now - timedelta(minutes=10)).isoformat(),
    )
    venue.open(
        id=by_hand,
        client_order_id=str(uuid4()),
        side="buy",
        qty="20",
        filled_qty="20",
        status="filled",
        submitted_at=(venue.now - timedelta(minutes=9)).isoformat(),
    )
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == [
        "20 SPY held, but our orders opened 18 in the last 1 day, 0:00:00"
    ]


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

    def get_orders(self, filter: Any = None):
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
    [
        ("tests", "canon-alpaca-paper"),
        ("windows", "canon-alpaca-paper-windows"),
        ("protect", "canon-alpaca-paper-protect"),
    ],
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


def test_the_task_takes_no_lock_and_names_its_three_accounts() -> None:
    """GET only, so no lock (the task's note); mise's own `choices` refuses any other account."""
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:account"]
    assert 'choices "tests" "windows" "protect"\n' in task["usage"]
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
        held={"SPY": Decimal(1)},
        closes_sent=0,
        requests={},
        turn=deque(),
        report=SimpleNamespace(started=stamp, closes={}, closes_not_placed=0),
        trading=SimpleNamespace(submit_order=lambda r: sent.append(r) or SimpleNamespace(id=None)),
    )
    for _ in range(3):
        session_window._AccountCheck._close(check, "SPY", "sell")  # ty: ignore[invalid-argument-type]
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


# ---------------------------------------------------------------------------------------------------------
# The close-only form, against a stand-in for Alpaca's paper Trading API and its latest quote


REGULAR = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)  # 10:00 New York, a Wednesday
AFTER_HOURS = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)  # 17:00 New York
SATURDAY = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
NODE_BUY = "O-20261111-145000-001-001-1"


class Venue(Paper):
    """Alpaca's paper Trading API as far as the form uses it: orders kept as alpaca-py `Order` models, a
    position per symbol, the clock and calendar for `now`. A market sell fills at the next lookup; a limit
    sells fills only if `limits_fill`. A cancel of an open order ends it; of an ended one, 422."""

    def __init__(self, now: datetime, *, limits_fill: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.now = now
        self.limits_fill = limits_fill
        self.held: dict[str, Decimal] = {}
        self.history: list[Order] = []
        self.sent: list[Any] = []
        self.canceled: list[str] = []
        self.submit_fails: BaseException | None = None
        self._retry = 3  # alpaca-py's `RESTClient._retry`, which `send_once` sets to 0

    # what the account holds
    def hold(self, symbol: str, qty: int | str, client_order_id: str | None = NODE_BUY) -> None:
        """A position of `qty` that `client_order_id`'s filled buy opened (None: nobody's in the
        lookback)."""
        self.held[symbol] = self.held.get(symbol, Decimal(0)) + Decimal(str(qty))
        if client_order_id is not None:
            self.history.append(
                _order(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    qty=str(qty),
                    filled_qty=str(qty),
                    status="filled",
                    type="market",
                    limit_price=None,
                    submitted_at=(self.now - timedelta(minutes=10)).isoformat(),
                )
            )

    def open(self, **fields: Any) -> Order:
        order = _order(**fields)
        self.history.append(order)
        return order

    def get_all_positions(self):
        self.calls.append(("get_all_positions", None))
        return [
            _position(symbol=s, qty=str(abs(q)), side="long" if q > 0 else "short")
            for s, q in self.held.items()
            if q
        ]

    def get_orders(self, filter: Any = None):
        self.calls.append(("get_orders", filter))
        status = filter.status.value if filter is not None and filter.status else "open"
        found = [
            o
            for o in self.history
            if (status == "all" or _value(o.status) not in session_window.TERMINAL)
            and (not filter.symbols or o.symbol in filter.symbols)
            and (filter.after is None or o.submitted_at > filter.after)
        ]
        return found[: filter.limit or 50]

    def get_clock(self):
        return Clock.model_validate(
            {
                "timestamp": self.now.isoformat(),
                "is_open": time_open(self.now),
                "next_open": "2026-11-12T14:30:00Z",
                "next_close": "2026-11-11T21:00:00Z",
            }
        )

    def get_calendar(self, filters=None):
        return [
            Calendar.model_validate(
                {
                    "date": "2026-11-11",
                    "open": "09:30",
                    "close": "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                    "settlement_date": "2026-11-12",
                }
            ),
            Calendar.model_validate(
                {
                    "date": "2026-11-12",
                    "open": "09:30",
                    "close": "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                    "settlement_date": "2026-11-13",
                }
            ),
        ]

    def submit_order(self, request):
        self.sent.append(request)
        if self.submit_fails is not None:
            raise self.submit_fails
        order = _order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=str(int(request.qty)),
            side=request.side.value,
            type=request.type.value,
            limit_price=None
            if getattr(request, "limit_price", None) is None
            else str(request.limit_price),
            extended_hours=bool(getattr(request, "extended_hours", False)),
            status="accepted",
            submitted_at=self.now.isoformat(),
        )
        self.history.append(order)
        return order

    def _fill(self, order: Order) -> Order:
        if _value(order.status) in session_window.TERMINAL:
            return order
        fills = _value(order.type) == "market" or self.limits_fill
        if not fills or _value(order.side) != "sell":
            return order
        filled = order.model_copy(update={"status": "filled", "filled_qty": order.qty})
        self.history[self.history.index(order)] = filled
        self.held[str(order.symbol)] -= Decimal(str(order.qty))
        return filled

    def get_order_by_client_id(self, client_id):
        for order in self.history:
            if order.client_order_id == client_id:
                return self._fill(order)
        raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))

    def cancel_order_by_id(self, order_id):
        for i, order in enumerate(self.history):
            if str(order.id) == str(order_id):
                if _value(order.status) in session_window.TERMINAL:
                    raise APIError('{"code": 42210000, "message": "not cancelable"}', _status(422))
                self.history[i] = order.model_copy(update={"status": "canceled"})
                self.canceled.append(order.client_order_id)
                return
        raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))


def _status(code: int):
    return SimpleNamespace(response=SimpleNamespace(status_code=code))


def _value(field):
    return str(getattr(field, "value", field))


def time_open(now: datetime) -> bool:
    return now == REGULAR


class Quotes:
    """alpaca-py's `StockHistoricalDataClient.get_stock_latest_quote`, answering one quote per symbol."""

    def __init__(self, bid="1.20", ask="1.24") -> None:
        self.bid, self.ask = bid, ask
        self.asked: list = []

    def get_stock_latest_quote(self, request):
        self.asked.append(request)
        return {
            request.symbol_or_symbols: Quote(
                request.symbol_or_symbols,
                {"t": "2026-11-11T21:59:58Z", "bp": self.bid, "ap": self.ask, "bs": 1, "as": 1},
            )
        }


class Clocks:
    """`sleep` that moves `monotonic` on, so a wait of 60 s takes no time."""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, secs: float) -> None:
        self.t += secs

    def monotonic(self) -> float:
        return self.t


def close(venue: Venue, data: Any = None):
    clocks = Clocks()
    return custody.close_ours(
        cast(Any, venue),
        data if data is not None else Quotes(),
        sleep=clocks.sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )


def test_in_the_regular_session_ours_is_closed_at_market_and_the_account_left_flat() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    report = close(venue)
    (sent,) = venue.sent
    assert isinstance(sent, MarketOrderRequest)
    assert (sent.symbol, sent.qty, _value(sent.side), _value(sent.time_in_force)) == (
        "SPY",
        1,
        "sell",
        "day",
    )
    assert sent.client_order_id.startswith(custody.HAND_CLOSE_PREFIX)
    assert custody.lineage(sent.client_order_id) == custody.OURS
    assert report.part == "regular"
    assert report.found == ["position of 1 SPY (ours opened 1)"]
    assert report.flat and not report.frozen and venue.held["SPY"] == 0
    assert "last shown: filled, filled 1" in report.closes[sent.client_order_id]


def test_a_foreign_open_order_freezes_its_symbol_and_is_never_touched() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    foreign = venue.open(client_order_id=FOREIGN[0], symbol="SPY", side="sell")
    ours = venue.open(client_order_id="O-20261111-145500-001-001-2", symbol="SPY", side="buy")
    report = close(venue)
    assert venue.sent == []  # no close in a frozen symbol
    assert venue.canceled == [ours.client_order_id]  # ours canceled, the foreign one never
    assert report.frozen["SPY"] == ["open a foreign order: sell 1 SPY limit, new"]
    assert FOREIGN[0] not in "\n".join(report.lines())
    assert report.left == []  # a frozen symbol's position is reported frozen, never as ours
    assert report.flat and report.frozen
    assert any(
        o.client_order_id == foreign.client_order_id and _value(o.status) == "new"
        for o in venue.history
    )


def test_a_quantity_ours_did_not_open_freezes_the_symbol() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.hold("SPY", 2, client_order_id=FOREIGN[0])  # the owner's buy beside ours
    venue.hold("AAPL", 5, client_order_id=None)  # held from before the lookback, by nobody we know
    report = close(venue)
    assert venue.sent == [] and venue.canceled == []
    assert report.frozen == {
        "AAPL": ["5 AAPL held, but our orders opened 0 in the last 1 day, 0:00:00"],
        "SPY": ["3 SPY held, but our orders opened 1 in the last 1 day, 0:00:00"],
    }


def test_a_short_is_never_bought_back() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", -1, client_order_id=None)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"] == ["a short of 1 SPY: never bought back here"]
    listed = [f for name, f in venue.calls if name == "get_orders"]
    assert all(_value(f.status) == "open" for f in listed)  # a frozen symbol's history is not read


def test_a_full_listing_of_the_symbols_orders_freezes_it() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    for n in range(2, custody.ORDERS_PAGE_LIMIT + 1):
        venue.history.append(
            _order(client_order_id=f"O-20261111-145000-001-001-{n}", status="canceled")
        )
    report = close(venue)
    assert venue.sent == []
    assert "the quantity ours opened is not established" in report.frozen["SPY"][0]


def test_outside_the_regular_session_a_marked_limit_through_the_touch_is_canceled_after_60_s() -> (
    None
):
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    quotes = Quotes(bid="760.10", ask="760.20")
    report = close(venue, quotes)
    (sent,) = venue.sent
    assert isinstance(sent, LimitOrderRequest)
    assert (sent.limit_price, sent.extended_hours, _value(sent.time_in_force)) == (
        760.05,
        True,
        "day",
    )
    (asked,) = quotes.asked
    assert (asked.symbol_or_symbols, _value(asked.feed)) == ("SPY", "sip")  # after-hours' feed
    log = report.closes[sent.client_order_id]
    assert log[0] == (
        "sent: sell 1 SPY limit 760.05, DAY, extended hours "
        "(sip quote at 21:59:58 UTC, bid 760.1, ask 760.2)"
    )
    assert "canceled after 60 s" in log and venue.canceled == [sent.client_order_id]
    assert report.left == ["position of 1 SPY (ours opened 1)"]  # not flat, by name


def test_outside_the_regular_session_a_limit_that_fills_leaves_the_account_flat() -> None:
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue)
    assert report.flat and venue.canceled == []


def test_a_holding_under_five_cents_is_closed_at_the_lowest_step_and_the_record_says_so() -> None:
    """The lead, 2026-11-12: a close exists to close. A bid of 0.04 less the five-cent offset is under
    Alpaca's finest step, so paper:close sends its sell at 0.0001, names that in its record, and the
    account is left flat; refusing it would leave the position open."""
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0.04", ask="0.05"))
    (sent,) = venue.sent
    assert sent.limit_price == 0.0001
    log = report.closes[sent.client_order_id]
    assert "less 0.05 is -0.01, under Alpaca's finest step" in log[0]
    assert "sent at the lowest, 0.0001" in log[0]
    assert report.flat


def test_a_bid_in_pennies_with_an_ask_that_is_not_sends_no_close_and_names_the_refusal() -> None:
    """The lead, 2026-11-12: a bid of 0.04 with an ask of 52.10 is a quote too wide to close on.
    paper:close sends nothing for it and names why; the position stays, by name."""
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0.04", ask="52.10"))
    assert venue.sent == []
    (error,) = report.errors
    assert "a quote too wide to close on" in error and "ask 52.1" in error
    assert not report.flat


def test_no_bid_sends_no_close_and_says_why() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    report = close(venue, Quotes(bid="0", ask="0"))
    assert venue.sent == []
    assert report.errors == ["SPY: no price for the close: ValueError"]
    assert not report.flat


def test_our_open_order_is_canceled_and_confirmed_ended_before_the_close() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    exit_order = venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    report = close(venue)
    assert venue.canceled[0] == exit_order.client_order_id
    assert report.canceled == [exit_order.client_order_id]
    assert len(venue.sent) == 1 and report.flat


def test_a_close_whose_answer_is_lost_is_looked_up_never_sent_again() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.submit_fails = requests.ReadTimeout("read timed out")
    report = close(venue)
    assert len(venue.sent) == 1  # one attempt
    (log,) = report.closes.values()
    assert log[1] == "no answer: ReadTimeout"
    assert "not found 60 s after it was sent" in log
    assert report.unfound == list(report.closes)  # it may yet appear, so it is named
    assert any(line.startswith("unfound: ") for line in report.lines())
    assert not report.flat


def test_a_close_alpaca_refuses_is_named_not_placed() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.submit_fails = APIError('{"code": 40310000, "message": "insufficient qty"}', _status(403))
    report = close(venue)
    (log,) = report.closes.values()
    assert log[1] == "not placed: APIError (HTTP 403)"
    assert report.left == ["position of 1 SPY (ours opened 1)"]


def test_with_no_session_open_nothing_is_sent_and_ours_is_left_by_name() -> None:
    venue = Venue(SATURDAY)
    venue.hold("SPY", 1)
    report = close(venue)
    assert venue.sent == [] and report.part == ""
    (error,) = report.errors
    assert error.startswith("SPY: no session part is open (2026-11-14 is not a trading day")
    assert error.endswith("), so no close was sent")
    assert report.left == ["position of 1 SPY (ours opened 1)"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [("flat", 0), ("ours", 0), ("frozen", 2), ("left", 1)],
)
def test_the_close_task_exits_by_what_is_left(setup, code, capsys) -> None:
    venue = Venue(REGULAR if setup != "left" else SATURDAY)
    if setup in ("ours", "left"):
        venue.hold("SPY", 1)
    if setup == "frozen":
        venue.hold("AAPL", 5, client_order_id=None)
    clocks = Clocks()
    assert (
        custody.close_task(
            cast(Any, venue),
            Quotes(),
            sleep=clocks.sleep,
            monotonic=clocks.monotonic,
            now=lambda: venue.now,
        )
        == code
    )
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("left: ")


def test_the_close_task_takes_the_accounts_lock_and_reads_its_pair_only() -> None:
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:close"]
    run = task["run"]
    assert 'choices "tests" "windows"' in task["usage"]
    assert "tests) svc=canon-alpaca-paper; lock=canon-paper-test.lock ;;" in run
    assert "windows) svc=canon-alpaca-paper-windows; lock=canon-paper-windows.lock ;;" in run
    assert "close_task" in run and 'lock_wait 9 "$lockwait" ||' in run
    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    block = re.compile(r"^lock_wait\(\) \{\n.*?^\}$", re.MULTILINE | re.DOTALL)
    found, want = block.search(run), block.search(tasks["paper:test"]["run"])
    # paper:test's text, which scripts/test-lanes holds the other paper tasks to
    assert found and want and found.group(0) == want.group(0)
    assert not re.search(r"/usr/bin/lockf -t [^0 ]", run)  # lockf's descriptor form busy-loops
    assert "CANON_ALPACA_DATA_ONLY" not in run
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", run)


def test_a_failure_part_way_cancels_the_close_it_sent_and_is_never_flat() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    looked_up = venue.get_order_by_client_id
    lookups = []

    def lookup_fails_once(client_id):
        lookups.append(client_id)
        if len(lookups) == 1:
            raise KeyError("a bug in the poll")
        return looked_up(client_id)

    venue.get_order_by_client_id = lookup_fails_once
    report = close(venue, Quotes(bid="760.10", ask="760.20"))
    (sent,) = venue.sent
    assert venue.canceled == [sent.client_order_id]  # not left working
    assert report.failed == "KeyError part way through"
    assert "canceled after the failure" in report.closes[sent.client_order_id]
    assert report.left == ["position of 1 SPY (ours opened 1)"] and not report.flat
    assert "failed: KeyError part way through" in report.lines()


def test_only_the_cancels_and_the_submit_are_sent_once_and_the_reads_keep_their_retries(
    monkeypatch,
) -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(session_window, "send_once", lambda t: setattr(t, "_retry", 0))
    for name in ("get_all_positions", "get_orders", "submit_order", "cancel_order_by_id"):
        call = getattr(venue, name)

        def spy(*args, _name=name, _call=call):
            seen.append((_name, venue._retry))
            return _call(*args)

        setattr(venue, name, spy)
    close(venue)
    assert {r for name, r in seen if name.startswith("get_")} == {3}
    assert {r for name, r in seen if name in ("submit_order", "cancel_order_by_id")} == {0}
    assert venue._retry == 3


def test_the_part_is_read_again_after_the_cancel_wait() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id="O-20261111-145500-001-001-2", side="sell")
    cancel = venue.cancel_order_by_id

    def cancel_then_the_bell(order_id):
        cancel(order_id)
        venue.now = AFTER_HOURS  # the regular session ended while the cancel was waited on

    venue.cancel_order_by_id = cancel_then_the_bell
    report = close(venue)
    (sent,) = venue.sent
    assert isinstance(sent, LimitOrderRequest) and sent.extended_hours
    assert report.part == "after-hours"


def test_a_read_that_fails_is_named_and_the_run_is_never_flat() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)

    def positions_fail():
        raise APIError('{"code": 50010000, "message": "internal"}', _status(500))

    venue.get_all_positions = positions_fail
    report = close(venue)
    assert venue.sent == []
    assert report.failed == "the positions failed: APIError (HTTP 500) part way through"
    assert report.left == ["unknown: the account could not be read at the end"]
    assert not report.flat


def test_on_an_account_that_may_short_no_long_is_ours_to_close() -> None:
    venue = Venue(REGULAR, no_shorting=False)
    venue.hold("SPY", 1)
    report = close(venue)
    assert venue.sent == []
    assert report.frozen["SPY"][0].startswith("the account is not long-only (no_shorting is false)")


def test_a_sigterm_part_way_cancels_the_close_it_sent(capsys) -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    clocks = Clocks()

    def sleep(secs: float) -> None:
        clocks.sleep(secs)
        if venue.sent:  # the close is working: the task's timeout sends TERM, then another
            signal.raise_signal(signal.SIGTERM)

    before = signal.getsignal(signal.SIGTERM)
    code = custody.close_task(
        cast(Any, venue),
        Quotes(bid="760.10", ask="760.20"),
        sleep=sleep,
        monotonic=clocks.monotonic,
        now=lambda: venue.now,
    )
    assert code == 1
    (sent,) = venue.sent
    assert venue.canceled == [sent.client_order_id]
    out = capsys.readouterr().out
    assert "failed: Stopped part way through" in out
    assert "canceled after the failure" in out
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_close_that_ends_just_before_its_cancel_is_not_named_a_failed_cancel() -> None:
    venue = Venue(AFTER_HOURS)
    venue.hold("SPY", 1)
    cancel = venue.cancel_order_by_id

    def fills_first(order_id):
        venue.limits_fill = True  # it fills between the last poll and the cancel
        for order in venue.history:
            if str(order.id) == str(order_id):
                venue._fill(order)
        cancel(order_id)

    venue.cancel_order_by_id = fills_first
    report = close(venue, Quotes(bid="760.10", ask="760.20"))
    (log,) = report.closes.values()
    assert "cancel refused (HTTP 422): it had ended; last shown below" in log
    assert log[-1] == "last shown: filled, filled 1"
    assert report.flat


def test_the_data_client_is_built_once_for_the_run() -> None:
    venue = Venue(AFTER_HOURS, limits_fill=True)
    venue.hold("SPY", 1)
    venue.hold("QQQ", 2, client_order_id="O-20261111-145000-001-001-9")
    built: list[Quotes] = []

    def factory() -> Quotes:
        built.append(Quotes())
        return built[-1]

    report = close(venue, factory)
    assert len(venue.sent) == 2 and report.flat
    assert len(built) == 1


def test_a_foreign_order_by_notional_is_named_by_its_notional() -> None:
    venue = Venue(REGULAR)
    venue.hold("SPY", 1)
    venue.open(client_order_id=FOREIGN[0], symbol="SPY", side="buy", qty=None, notional="100")
    report = close(venue)
    assert report.frozen["SPY"] == ["open a foreign order: buy notional 100 SPY limit, new"]
