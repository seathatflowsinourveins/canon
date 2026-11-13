"""The protect task against Alpaca's paper API, on an account whose long-only setting is off for the run.

What it shows (ADR 0142): with one share held and `no_shorting` false, plan-only prints every registered
row and sends nothing; the run that sends under the fixed row F2 leaves one trailing stop Alpaca holds for
the share, `GTC`, marked `sell_to_close`, of the protect lineage, at a distance not under Alpaca's smallest
trail; while it rests Alpaca shows no share of the holding free; a second run sends nothing and names the
long as covered; the task's own cancel (`--cancel`) then names the stop plan-only, cancels it, and Alpaca
shows the share free again. What it does not show: a stop for more shares than are held, and how a `GTC`
trailing stop elects, or holds its shares, at a later open.

ADR 0111's form: the setting goes off and comes back inside this one run, restored whatever its body did
(`_finish`). It runs
only when a paper key pair is in the environment, `CANON_PAPER_PROTECT_TASK=1` asks for it and the paper
runner's own marker is there, so a routine `mise run paper:test` skips it. Run it alone, in the regular
session, on account A:

    CANON_PAPER_PROTECT_TASK=1 mise run paper:test -- -s -k test_paper_protect_task tests/test_paper_protect_task.py

`CANON_PAPER_PROTECT_SYMBOL` names another stock to buy the one share of, so the plan-only lines show
every row's distance for that name; it is read from the environment and written nowhere. Never write the
keys to a file.

Contract: alpaca-py at `c42add7c0a70`: `TradingClient.get_clock`, `get_account_configurations`,
`set_account_configurations`, `get_all_positions`, `get_orders(GetOrdersRequest)`,
`submit_order(MarketOrderRequest)`, `get_order_by_client_id`, `get_order_by_id`, `cancel_order_by_id`
(`alpaca/trading/client.py`), and `canon_trading.protect.protect_longs`, the task's own run.

The account is left as it was found. It places nothing, and fails, unless the account is long-only, holds
no position and no open order, and the regular session has fifteen minutes left. Whatever happens after
that, in this order: every order it or the task sent is canceled and read until it has ended; the
long-only setting is put back and read; only then, on a long-only account again, where Alpaca itself
refuses a sell beyond the holding (ADR 0111), the one share is sold if it is still held; then the account
is read flat. Each step is tried whatever the one before it did, and whatever is left is named. No order
ID, account number or price is printed.

A limit: the cleanup runs in this process, and the paper runner ends a run past its deadline, or one it is
told to stop, with a KILL, which runs none of it. The stop is `GTC` and would then stay with its share and
the setting off, and `paper:close` does not cancel a protect stop. So the test starts only with
`DEADLINE_ROOM_SECS` left of the runner's deadline, and prints what to do by hand before its first send.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import MarketOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY

from canon_trading import custody
from canon_trading import protect
from canon_trading import session_window

SYMBOL = os.environ.get("CANON_PAPER_PROTECT_SYMBOL", "SPY")
"""The stock the one share is of: SPY, or the name the run's environment gives."""
ONE = Decimal(1)
PREFIX = "canon-check-protect-"
"""The test's own buy and closing sell; foreign to `custody.lineage`, as every paper test's orders are."""
WAIT_SECS = 30
"""The most reads, one a second, of an order until it has ended, of the holding, and of the listing."""
ENDED = frozenset({"filled", "canceled", "expired", "rejected"})
CLOSE_MARGIN_SECS = 15 * 60
ROW = "F2"
"""The row the sending run names: the fixed trail, the widest of the three, least likely to elect here."""
DEADLINE_ROOM_SECS = 600
"""The least left of the paper runner's deadline (`CANON_PAPER_TEST_DEADLINE`) to start: the waits of a
slow run and of its cleanup, a dozen of `WAIT_SECS` at most each, and three runs of the task."""
BY_HAND = (
    "if this run is killed before its cleanup, by hand: mise run paper:long-only tests on; then "
    "mise run paper:account tests; a protect stop left open and the one share are the test's own, "
    "and no task cancels the one or sells the other (paper:close freezes both): cancel and sell "
    "them in Alpaca's own screen"
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY)),
    reason=(
        f"Alpaca paper credentials not in the environment: set {ENV_API_KEY} and {ENV_SECRET_KEY} "
        "(mise run paper:test reads them from the login keychain)"
    ),
)


def _status(order: Any) -> str:
    return str(getattr(order.status, "value", order.status))


def _no_shorting(trading: Any) -> bool:
    return trading.get_account_configurations().no_shorting is True


def _set_no_shorting(trading: Any, value: bool) -> bool:
    """Send the configuration back as read with only `no_shorting` changed (as `paper:long-only` does),
    and say what a fresh read shows."""
    before = trading.get_account_configurations()
    if before.no_shorting is not value:
        trading.set_account_configurations(before.model_copy(update={"no_shorting": value}))
    return trading.get_account_configurations().no_shorting is value


def _open_orders(trading: Any) -> list[Any]:
    return list(trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)))


def _working(trading: Any) -> list[Any]:
    """The listed open orders whose own read does not show them ended. Alpaca's open-orders listing
    can still show an order a second after it filled or was canceled (seen on paper, 2026-11-13: this
    test's own filled buy), so the body's checks of what is working go by each order's own read. One
    still listed though ended is printed with the status the listing gave it."""
    working = []
    for order in _open_orders(trading):
        own = trading.get_order_by_id(order.id)
        if _status(own) in ENDED:
            print(
                f"listed as open though ended: listed {_status(order)}, its own read {_status(own)}"
            )
        else:
            working.append(own)
    return working


def _held(trading: Any) -> dict[str, Decimal]:
    """Each position signed by its side (`session_window.positions`): alpaca-py's `Position` carries
    the side apart from the count, so a short may list a positive quantity."""
    return session_window.positions(trading)


SENT: list[str] = []
"""The client order ID of each order `_market` sent, kept before its submit: an interrupt during the
submit loses the answer, and the cleanup reads each ID itself."""


def _by_client_id(trading: Any, client_order_id: str) -> Any:
    """The order of that ID, or None when Alpaca shows none (`custody._look_up`)."""
    return custody._look_up(trading, client_order_id)


def _until(read: Any, wanted: Any) -> Any:
    """`read()` once a second, at most `WAIT_SECS` times, until `wanted(value)`; the last value read."""
    value = read()
    for _ in range(WAIT_SECS - 1):
        if wanted(value):
            break
        time.sleep(1)
        value = read()
    return value


def _market(trading: Any, side: OrderSide, label: str, intent: PositionIntent | None) -> Any:
    """One DAY market order of one share, sent once; a lost answer is looked up by its ID, never sent
    again. The order as Alpaca shows it once it has ended, or as last read."""
    client_order_id = f"{PREFIX}{uuid4().hex[:20]}-{label}"
    request = MarketOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=side,
        time_in_force=TimeInForce.DAY,
        position_intent=intent,
        client_order_id=client_order_id,
    )
    SENT.append(client_order_id)
    try:
        with custody._sending_once(trading):
            trading.submit_order(request)
    except APIError as e:
        if e.status_code in (403, 422):
            SENT.remove(client_order_id)  # refused: there is no such order to look for
            raise AssertionError(f"{label}: refused by Alpaca, HTTP {e.status_code}") from None
        print(f"{label}: answer lost (HTTP {e.status_code}); looking the order up")
    except OSError as e:
        print(f"{label}: answer lost ({type(e).__name__}); looking the order up")

    order = _until(
        lambda: _by_client_id(trading, client_order_id),
        lambda found: found is not None and _status(found) in ENDED,
    )
    assert order is not None, f"{label}: never showed at Alpaca; it may yet show working"
    print(f"{label}: {_status(order)}, filled {order.filled_qty}")
    return order


def _cancel_and_wait(trading: Any, order: Any, problems: list[str]) -> None:
    """One cancel, sent once, then the order's own read until it has ended."""
    try:
        with custody._sending_once(trading):
            trading.cancel_order_by_id(order.id)
    except APIError as e:
        if e.status_code not in (404, 422):  # gone, or no longer cancelable: the read decides
            problems.append(f"a cancel failed, HTTP {e.status_code}")
    ended = _until(
        lambda: trading.get_order_by_id(order.id),
        lambda found: _status(found) in ENDED,
    )
    if _status(ended) not in ENDED:
        problems.append(f"an order did not end after its cancel: {_status(ended)}")
    else:
        print(f"cleanup: one {_status(ended)} order, filled {ended.filled_qty}")


def _cancel_ours(trading: Any, problems: list[str]) -> None:
    """Cancel every open order in the symbol that the task or this test sent, and read each until it
    has ended, by its own read: the open-orders listing lags a cancel by seconds."""
    for order in _open_orders(trading):
        client_order_id = str(order.client_order_id or "")
        ours = client_order_id.startswith(PREFIX) or (
            custody.lineage(client_order_id) == custody.PROTECT
        )
        if order.symbol != SYMBOL or not ours:
            problems.append(f"an order this run did not send is open: {custody.describe(order)}")
            continue
        _cancel_and_wait(trading, order, problems)


def _cancel_sent(trading: Any, problems: list[str]) -> None:
    """Each order this run sent, by the ID kept before its submit: the listing can lag an order by a
    second, and an interrupted submit kept no answer. Each is its own attempt, whatever the listing or
    another order did; one that could not be read or canceled is named with its ID."""
    for client_order_id in SENT:
        try:
            order = _by_client_id(trading, client_order_id)
            if order is None:
                problems.append(
                    f"{client_order_id} ({SYMBOL}) does not show at Alpaca, and may yet"
                )
            elif _status(order) not in ENDED:
                _cancel_and_wait(trading, order, problems)
        except BaseException as e:  # noqa: BLE001 - the next kept ID is still tried; this one is named
            problems.append(
                f"{client_order_id} ({SYMBOL}) could not be read or canceled "
                f"({type(e).__name__}); it may yet show working"
            )


def _bought(trading: Any) -> Decimal:
    """The shares this run's own market orders bought less those they sold, by each order's own read."""
    net = Decimal(0)
    for client_order_id in SENT:
        order = _by_client_id(trading, client_order_id)
        if order is not None:
            filled = Decimal(str(order.filled_qty or 0))
            net += filled if getattr(order.side, "value", order.side) == "buy" else -filled
    return net


def _clean(trading: Any, problems: list[str]) -> None:
    """The module header's order: cancel, restore the setting, sell the share on a long-only account,
    read flat. Each step is tried whatever the one before it did."""

    def cancel() -> None:
        _cancel_ours(trading, problems)

    def cancel_sent() -> None:
        _cancel_sent(trading, problems)

    def restore() -> None:
        if not _set_no_shorting(trading, True) and not _set_no_shorting(trading, True):
            problems.append(
                "no_shorting is NOT true after two attempts: run mise run paper:long-only tests on"
            )
        print(f"no_shorting after the run: {str(_no_shorting(trading)).lower()}")

    def close() -> None:
        if not _no_shorting(trading):
            problems.append("the share is not sold: the account is not long-only, see above")
            return
        bought = _bought(trading)
        held = _until(
            lambda: _held(trading).get(SYMBOL, Decimal(0)),
            lambda found: found != 0 or bought <= 0,
        )
        if bought > 0 and held == 0:
            problems.append(
                f"this run's orders filled {bought} {SYMBOL} and no position of it shows after "
                f"{WAIT_SECS} reads (a stop that elected may have sold it): "
                "mise run paper:account tests"
            )
        if held < 0:
            problems.append(f"A SHORT of {-held} {SYMBOL} is open: close it by hand")
        elif held == ONE:
            closed = _market(trading, OrderSide.SELL, "close", PositionIntent.SELL_TO_CLOSE)
            if _status(closed) != "filled":
                problems.append(f"the closing sell did not fill: {_status(closed)}")
        elif held != 0:
            problems.append(f"{held} {SYMBOL} held, not the one share bought: left as found")

    def final() -> None:
        held = _until(lambda: _held(trading), lambda found: not found)
        if held:
            problems.append(f"positions left: {sorted(held)}")
        left = _until(lambda: _open_orders(trading), lambda found: not found)
        if left:
            problems.append(f"{len(left)} open orders left")

    for name, step in (
        ("the cancel of what was sent", cancel),
        ("the cancel of this run's own orders by their kept IDs", cancel_sent),
        ("the long-only setting's restore", restore),
        ("the close of the share", close),
        ("the final read", final),
    ):
        try:
            step()
        except BaseException as e:  # noqa: BLE001 - every later step still runs; all is named
            problems.append(f"{name} failed: {type(e).__name__}")


def _finish(body: Any, clean: Any) -> None:
    """Run `body`, then `clean(problems)` whatever `body` did, and report every problem the cleanup
    named whatever else failed: each is printed, and the run fails naming them all, with `body`'s own
    failure as the cause when it had one. With no problem, `body`'s failure is raised as it was."""
    problems: list[str] = []
    failure: BaseException | None = None
    try:
        body()
    except BaseException as e:  # noqa: BLE001 - kept, and raised again below
        failure = e
    try:
        clean(problems)
    except BaseException as e:  # noqa: BLE001 - the cleanup's own failure is a problem by name
        problems.append(f"the cleanup itself failed: {type(e).__name__}")
    for problem in problems:
        print(f"LEFT BY THE CLEANUP: {problem}")
    if problems:
        raise AssertionError("; ".join(problems)) from failure
    if failure is not None:
        raise failure


def test_paper_protect_task_on_an_account_that_allows_shorting(capsys: Any) -> None:
    marker = os.environ.get("CANON_ALPACA_STREAM_LOCK", "")
    if os.environ.get("CANON_PAPER_PROTECT_TASK") != "1":
        pytest.skip("asked for only by CANON_PAPER_PROTECT_TASK=1: it changes an account setting")
    if not marker or Path(marker).name != "canon-alpaca-stream-tests.lock":
        pytest.skip("not under mise run paper:test on account A")
    if os.environ.get("CANON_PAPER_ACCOUNT", "tests") != "tests":
        pytest.skip("this is only for the test account")
    if not (SYMBOL.isascii() and SYMBOL.isalpha() and SYMBOL.isupper() and len(SYMBOL) <= 5):
        pytest.skip("CANON_PAPER_PROTECT_SYMBOL is not a plain stock symbol")
    deadline = os.environ.get("CANON_PAPER_TEST_DEADLINE", "")
    if not deadline.isdigit() or int(deadline) - time.time() < DEADLINE_ROOM_SECS:
        pytest.skip(f"needs {DEADLINE_ROOM_SECS} s of the paper runner's deadline left")
    trading: Any = session_window.paper_trading()
    clock = trading.get_clock()
    if not clock.is_open:
        pytest.skip("needs the regular session")
    if (clock.next_close - clock.timestamp).total_seconds() < CLOSE_MARGIN_SECS:
        pytest.skip(
            "needs fifteen minutes left to the close: no paper run covers the protect task's last "
            "fifteen minutes before a close, which only the simulation cases in tests/test_protect.py "
            "cover (the close margin's limit)"
        )
    before = _no_shorting(trading)
    print(f"no_shorting before the run: {str(before).lower()}")
    assert before, "the account is not long-only at the start; nothing sent"
    assert not _held(trading), "the account holds a position at the start; nothing sent"
    assert not _open_orders(trading), "the account holds an open order at the start; nothing sent"

    SENT.clear()

    def body() -> None:
        bought = _market(trading, OrderSide.BUY, "buy", None)
        assert _status(bought) == "filled", f"the buy did not fill: {_status(bought)}"
        held = _until(lambda: _held(trading), lambda found: found.get(SYMBOL) == ONE)
        assert held == {SYMBOL: ONE}, f"the holding after the buy is {sorted(held)}"
        assert _set_no_shorting(trading, False), "the long-only setting did not go off"
        print("no_shorting while running: false")

        planned = protect.protect_longs(trading, session_window.window_data, print, plan_only=True)
        assert planned.exit_code == 0 and planned.planned, planned.lines()
        named = {row for row in protect.ROWS for line in planned.said if f"under {row}:" in line}
        assert named == set(protect.ROWS), f"plan-only left a row unnamed: {sorted(named)}"
        assert not planned.sent and not _working(trading), "plan-only sent an order"

        unnamed = protect.protect_longs(trading, session_window.window_data, print)
        assert unnamed.exit_code == 3 and not _working(trading), "a send with no row went out"

        sent = protect.protect_longs(trading, session_window.window_data, print, row=ROW)
        assert sent.exit_code == 0 and len(sent.sent) == 1, sent.lines()
        (listed,) = _working(trading)
        stop = _until(
            lambda: trading.get_order_by_id(listed.id), lambda found: found.hwm is not None
        )
        assert stop.symbol == SYMBOL and _status(stop) in ("new", "accepted"), _status(stop)
        assert custody.lineage(str(stop.client_order_id)) == custody.PROTECT
        terms = (
            str(getattr(stop.side, "value", stop.side)),
            str(getattr(stop.order_type, "value", stop.order_type)),
            str(getattr(stop.time_in_force, "value", stop.time_in_force)),
            str(getattr(stop.position_intent, "value", stop.position_intent)),
            Decimal(str(stop.qty)),
        )
        assert terms == ("sell", "trailing_stop", "gtc", "sell_to_close", ONE), terms
        assert stop.trail_price is not None and stop.hwm is not None
        smallest = Decimal(str(stop.hwm)) * protect.VENUE_SMALLEST
        assert Decimal(str(stop.trail_price)) >= smallest, "the trail is under Alpaca's smallest"
        print(
            "the stop at Alpaca: sell 1, trailing_stop, gtc, sell_to_close, protect lineage, "
            f"trail {Decimal(str(stop.trail_price)) / Decimal(str(stop.hwm)):.4%} of its mark"
        )

        free = _until(
            lambda: Decimal(str(trading.get_open_position(SYMBOL).qty_available)),
            lambda found: found == 0,
        )
        assert free == 0, f"Alpaca shows {free} of the share free while the GTC stop rests"
        print("while the GTC stop rests, Alpaca shows 0 of the 1 share free")

        again = protect.protect_longs(trading, session_window.window_data, print, row=ROW)
        assert again.exit_code == 0 and not again.sent, again.lines()
        assert len(again.left_alone) == 1 and "fully covered" in again.left_alone[0]
        assert len(_working(trading)) == 1, "the second run sent or lost an order"

        would = protect.cancel_stops(trading, print, SYMBOL, plan_only=True)
        assert would.exit_code == 0 and len(would.planned) == 1, would.lines()
        (resting,) = _working(trading)
        assert _status(trading.get_order_by_id(resting.id)) in ("new", "accepted"), (
            "the plan-only cancel canceled"
        )
        taken = protect.cancel_stops(trading, print, SYMBOL)
        assert taken.exit_code == 0 and len(taken.sent) == 1, taken.lines()
        assert _status(trading.get_order_by_id(resting.id)) == "canceled"
        freed = _until(
            lambda: Decimal(str(trading.get_open_position(SYMBOL).qty_available)),
            lambda found: found == ONE,
        )
        assert freed == ONE, f"Alpaca shows {freed} of the share free after the stop's cancel"
        print("after the task's cancel, Alpaca shows the 1 share free again")

    with capsys.disabled():
        print(BY_HAND)
        _finish(body, lambda problems: _clean(trading, problems))
