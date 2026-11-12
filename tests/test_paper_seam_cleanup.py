"""The paper seam test's cleaning (`tests/test_paper_protect_seam.py`, `left_as_found`), offline: a step
that fails does not end it, and what it cannot clear is named. Every paper test cancels what it opened and
closes any position it opened in a `finally` (`.claude/rules/trading-safety.md`); this holds the helper to
that when the first try at the close fails."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from canon_trading import session_window
from tests import test_paper_protect_seam as seam

PART_NOW = seam.part_now  # the seam's own, which the cleaning cases replace with a fixed part


class Account:
    """One symbol's open orders and position, with a script of what each close attempt does."""

    def __init__(self, closes: list[str]) -> None:
        self.orders: list[Any] = []
        self.position = Decimal(0)
        self.closes = closes
        self.canceled: list[int] = []
        self.submitted = 0
        self.requests: list[Any] = []

    def cancel_order_by_id(self, order_id: int) -> None:
        self.canceled.append(order_id)
        self.orders = [order for order in self.orders if order.id != order_id]

    def submit_order(self, request: Any) -> Any:
        self.submitted += 1
        self.requests.append(request)
        order = SimpleNamespace(id=100 + self.submitted)
        if self.closes.pop(0) == "fills":
            self.position = Decimal(0)
        else:
            self.orders.append(order)
        return order


def case(monkeypatch: pytest.MonkeyPatch, closes: list[str], quote: str = "ok") -> Account:
    account = Account(closes)

    def wait_until(done: Any, what: str, seconds: float = 60.0) -> None:
        if not done():
            raise AssertionError(f"timed out waiting for {what}")

    def close_limit(data: Any, symbol: str, side: str, part: str) -> tuple[Decimal, None]:
        if quote != "ok":
            raise ValueError("the latest quote is one-sided")
        return Decimal(100), None

    monkeypatch.setattr(seam, "paper_trading", lambda: account)
    monkeypatch.setattr(seam, "part_now", lambda trading, part: part)
    monkeypatch.setattr(seam, "paper_data", lambda: object())
    monkeypatch.setattr(seam, "open_orders", lambda trading: list(trading.orders))
    monkeypatch.setattr(seam, "held", lambda trading: trading.position)
    monkeypatch.setattr(seam, "wait_until", wait_until)
    monkeypatch.setattr(session_window, "close_limit", close_limit)
    return account


def opened(account: Account) -> None:
    """What a test body leaves: one resting order and one share."""
    account.orders.append(SimpleNamespace(id=1))
    account.position = Decimal(1)


def test_a_close_that_does_not_fill_is_canceled_and_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["rests", "fills"])
    with seam.left_as_found("overnight") as cleaned:
        opened(account)
    assert (account.orders, account.position) == ([], Decimal(0))
    assert account.canceled == [1, 101]  # the test's order, then the first close that did not fill
    assert account.submitted == 2
    assert cleaned == {"orders": 1, "positions": 1, "rounds": 2}  # what the test left; two rounds


def test_what_three_rounds_cannot_close_is_named_and_no_order_is_left_resting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["rests", "rests", "rests"])
    with (
        pytest.raises(AssertionError, match="NOT left as found in AAPL after 3 rounds") as named,
        seam.left_as_found("overnight"),
    ):
        opened(account)
    assert "round 1" in str(named.value)
    assert "round 3" in str(named.value)
    assert account.submitted == 3
    assert account.canceled == [1, 101, 102]
    assert account.position == Decimal(1)


def test_a_quote_that_cannot_price_the_close_still_cancels_the_open_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, [], quote="one-sided")
    with (
        pytest.raises(AssertionError, match="the latest quote is one-sided"),
        seam.left_as_found("overnight"),
    ):
        opened(account)
    assert account.canceled == [1]
    assert account.orders == []


def test_an_order_that_closed_meanwhile_is_not_counted_as_the_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["fills"])

    def cancel(order_id: int) -> None:
        account.orders = []
        gone = SimpleNamespace(response=SimpleNamespace(status_code=422), request=None)
        raise seam.APIError('{"code": 42210000, "message": "order is not cancelable"}', gone)

    monkeypatch.setattr(account, "cancel_order_by_id", cancel)
    with seam.left_as_found("overnight") as cleaned:
        opened(account)
    assert cleaned == {"orders": 0, "positions": 1, "rounds": 1}


def test_a_failing_test_body_is_still_cleaned_and_its_own_failure_is_the_one_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["fills"])
    with pytest.raises(RuntimeError, match="the test body failed"), seam.left_as_found("overnight"):
        opened(account)
        raise RuntimeError("the test body failed")
    assert (account.orders, account.position) == ([], Decimal(0))


def test_an_account_already_holding_the_symbol_is_refused_before_anything_is_placed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, [])
    account.orders.append(SimpleNamespace(id=7))
    with pytest.raises(AssertionError, match="placing nothing"), seam.left_as_found("overnight"):
        pytest.fail("the body ran")
    assert (account.canceled, account.submitted) == ([], 0)


def test_a_response_that_does_not_parse_in_the_cancel_step_does_not_end_the_cleaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["fills"])
    reads = iter([None, None, ValueError("1 validation error for Order")])

    def open_orders(trading: Any) -> list[Any]:
        problem = next(reads, None)
        if problem is not None:
            raise problem
        return list(trading.orders)

    monkeypatch.setattr(seam, "open_orders", open_orders)
    with seam.left_as_found("overnight") as cleaned:
        opened(account)
    assert (account.orders, account.position) == ([], Decimal(0))
    assert account.submitted == 1  # the close still ran in the round whose cancel step failed
    assert cleaned == {"orders": 1, "positions": 1, "rounds": 1}


def test_an_order_canceled_before_a_wait_that_fails_is_still_counted_as_the_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["fills"])
    waits = iter([AssertionError("timed out waiting for the canceled orders to close")])

    def wait_until(done: Any, what: str, seconds: float = 60.0) -> None:
        problem = next(waits, None)
        if problem is not None:
            raise problem
        assert done(), what

    monkeypatch.setattr(seam, "wait_until", wait_until)
    with seam.left_as_found("overnight") as cleaned:
        opened(account)
    assert account.canceled == [1]
    assert (cleaned["orders"], cleaned["positions"]) == (1, 1)


def test_a_holding_that_cannot_be_read_in_the_first_round_is_still_counted_once_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = case(monkeypatch, ["fills"])
    reads = iter([None, OSError("connection reset")])

    def held(trading: Any) -> Decimal:
        problem = next(reads, None)
        if problem is not None:
            raise problem
        return trading.position

    monkeypatch.setattr(seam, "held", held)
    with seam.left_as_found("overnight") as cleaned:
        opened(account)
    assert (account.orders, account.position) == ([], Decimal(0))
    assert (cleaned["orders"], cleaned["positions"]) == (1, 1)


def test_the_close_is_a_market_order_in_the_regular_session_and_an_extended_limit_outside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limit through this account's quote did not fill in the regular session (2026-11-12, 09:31), so
    the cleaning closes there as `custody`'s close does, by market order."""
    for part, kind, extended in (
        ("regular", "MarketOrderRequest", None),
        ("overnight", "LimitOrderRequest", True),
    ):
        account = case(monkeypatch, ["fills"])
        with seam.left_as_found(part):
            opened(account)
        (request,) = account.requests
        assert type(request).__name__ == kind, part
        assert request.extended_hours is extended, part
        assert account.position == Decimal(0)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("a clock answer alpaca-py cannot build"),
        AssertionError("not a clock"),
        OSError("no network"),
        pytest.skip.Exception("no session is open"),
    ],
)
def test_the_part_falls_back_to_the_starting_one_whenever_the_clock_cannot_be_read(
    monkeypatch, error
) -> None:
    """A close must not fail to be placed because the clock's answer could not be read or built."""

    def unreadable(trading: Any) -> str:
        raise error

    monkeypatch.setattr(seam, "open_part", unreadable)
    assert PART_NOW(cast("Any", object()), "pre-market") == "pre-market"
    monkeypatch.setattr(seam, "open_part", lambda trading: "regular")
    assert PART_NOW(cast("Any", object()), "pre-market") == "regular"
