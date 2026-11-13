"""The regular-session protect task offline (src/canon_trading/protect.py, `mise run paper:protect`).

No test here opens a network connection or reads a credential. Alpaca's answers are alpaca-py 0.45.0's own
models, validated from made-up payloads; the trading and data clients are stand-ins that record every call.
"""

import importlib.util
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

import canon_alpaca.execution
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.models import BarSet
from alpaca.data.models import Quote
from alpaca.trading.enums import PositionIntent
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.models import Clock
from alpaca.trading.models import Order
from alpaca.trading.models import Position
from alpaca.trading.requests import TrailingStopOrderRequest
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price

from canon_trading import custody
from canon_trading import mover
from canon_trading import protect
from canon_trading import sizing

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)  # 10:00 New York, in the regular session
OUR_SELL = "O-20261112-145500-001-001-3"  # a window's sell, of our nodes' lineage
FOREIGN_SELL = str(uuid4())  # what Alpaca makes when no ID is sent: the owner's, say
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
"""`GET /v2/account/configurations` as alpaca-py 0.45.0's `AccountConfiguration` reads it (made up, as
`tests/test_custody.py`'s)."""


def _status(code: int):
    return SimpleNamespace(response=SimpleNamespace(status_code=code))


def _position(symbol: str, qty: str, side: str = "long", available: str | None = "all") -> Position:
    """A position; `available` is Alpaca's `qty_available` (the quantity no open order holds): all of
    it by default, a number as text, or None for a payload that does not show it."""
    return Position.model_validate(
        {
            "qty_available": qty if available == "all" else available,
            "asset_id": str(uuid4()),
            "symbol": symbol,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "10.00",
            "qty": qty,
            "side": side,
            "market_value": "100.00",
            "cost_basis": "100.00",
            "unrealized_pl": "0",
            "unrealized_plpc": "0",
            "unrealized_intraday_pl": "0",
            "unrealized_intraday_plpc": "0",
            "current_price": "10.00",
            "lastday_price": "10.00",
            "change_today": "0",
        }
    )


def _order(symbol: str, qty: str | None, client_order_id: str, **fields: Any) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": client_order_id,
        "created_at": "2026-11-12T14:55:00Z",
        "updated_at": "2026-11-12T14:55:00Z",
        "submitted_at": "2026-11-12T14:55:00Z",
        "asset_id": str(uuid4()),
        "symbol": symbol,
        "asset_class": "us_equity",
        "qty": qty,
        "filled_qty": "0",
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "side": "sell",
        "time_in_force": "day",
        "limit_price": "10.50",
        "status": "new",
        "extended_hours": False,
    }
    return Order.model_validate({**payload, **fields})


class Paper:
    """A stand-in for the paper `TradingClient`: the clock, the configuration, the positions, the open
    orders, a submit and a look-up by client order ID, each call recorded. It has no other method, so a
    cancel, a replace or a close would fail on an `AttributeError` as well as show in `calls`."""

    def __init__(
        self,
        *,
        positions=(),
        orders=(),
        no_shorting=True,
        is_open=True,
        next_close="2026-11-12T21:00:00Z",
    ) -> None:
        self.positions = list(positions)
        self.orders = list(orders)
        self.no_shorting = no_shorting
        self.is_open = is_open
        self.next_close = next_close
        self.clock_fails: BaseException | None = None
        self.later: dict[str, Position | None] = {}
        """What a symbol's position reads when it is read again just before its send: another
        position, or None for one that is gone. A symbol not named reads as it did."""
        self.calls: list[tuple[str, Any]] = []
        self.submitted: list[Any] = []
        self.submit_fails: BaseException | None = None
        self.found: dict[str, Order] = {}

    def get_clock(self):
        self.calls.append(("get_clock", None))
        if self.clock_fails is not None:
            raise self.clock_fails
        return Clock.model_validate(
            {
                "timestamp": NOW.isoformat(),
                "is_open": self.is_open,
                "next_open": "2026-11-13T14:30:00Z",
                "next_close": self.next_close,
            }
        )

    def get_open_position(self, symbol):
        self.calls.append(("get_open_position", symbol))
        found = self.later.get(
            symbol, next((p for p in self.positions if p.symbol == symbol), None)
        )
        if found is None:
            raise APIError('{"code": 40410000, "message": "position does not exist"}', _status(404))
        return found

    def get_account_configurations(self):
        self.calls.append(("get_account_configurations", None))
        return AccountConfiguration.model_validate(
            {**CONFIGURATION, "no_shorting": self.no_shorting}
        )

    def get_all_positions(self):
        self.calls.append(("get_all_positions", None))
        return self.positions

    def get_orders(self, filter: Any = None):
        self.calls.append(("get_orders", filter))
        return self.orders

    def submit_order(self, order_data):
        self.calls.append(("submit_order", order_data))
        self.submitted.append(order_data)
        if self.submit_fails is not None:
            raise self.submit_fails
        return _order(
            order_data.symbol,
            str(int(order_data.qty)),
            order_data.client_order_id,
            type="trailing_stop",
            order_type="trailing_stop",
            limit_price=None,
            time_in_force="gtc",
        )

    def get_order_by_client_id(self, client_id):
        self.calls.append(("get_order_by_client_id", client_id))
        if client_id in self.found:
            return self.found[client_id]
        raise APIError('{"code": 40410000, "message": "order not found"}', _status(404))


class Data:
    """alpaca-py's `StockHistoricalDataClient`: one latest quote and minute bars per symbol, the bars
    `width` wide around `mid` (high mid + width/2, low mid - width/2, close mid), complete minutes
    before `NOW`."""

    def __init__(
        self,
        books: dict[str, tuple[str, str, str, str]],
        *,
        quoted: str = "2026-11-12T14:59:59Z",
        minutes: int = 20,
    ) -> None:
        self.books = books  # symbol: (bid, ask, mid, width)
        self.quoted = quoted
        self.minutes = minutes
        self.fails: dict[str, BaseException] = {}
        self.asked: list[tuple[str, Any]] = []

    def get_stock_latest_quote(self, request):
        self.asked.append(("get_stock_latest_quote", request))
        symbol = request.symbol_or_symbols
        if symbol in self.fails:
            raise self.fails[symbol]
        if symbol not in self.books:
            return {}
        bid, ask, _, _ = self.books[symbol]
        return {symbol: Quote(symbol, {"t": self.quoted, "bp": bid, "ap": ask, "bs": 1, "as": 1})}

    def get_stock_bars(self, request):
        self.asked.append(("get_stock_bars", request))
        symbol = request.symbol_or_symbols
        _, _, mid, width = self.books[symbol]
        bars = [
            {
                "t": (NOW - timedelta(minutes=20 - i)).isoformat().replace("+00:00", "Z"),
                "o": float(mid),
                "h": float(D(mid) + D(width) / 2),
                "l": float(D(mid) - D(width) / 2),
                "c": float(mid),
                "v": 1000,
                "n": 10,
                "vw": float(mid),
            }
            for i in range(20)
        ][20 - self.minutes :]
        return BarSet({symbol: bars})


AAA_BOOK = ("9.99", "10.00", "10.00", "0.04")
"""ATR 0.04 on a $10 name: 0.004 of the entry, over the spread floor (two 1-cent spreads, 0.002)."""
BBB_BOOK = ("19.95", "20.00", "20.00", "0.02")
"""ATR 0.02 on a $20 name: 0.001, under the spread floor (two 5-cent spreads, 0.005), so the floor binds."""


def _planned(
    book: tuple[str, str, str, str], *, row: str | None = None, size_from_budget: bool = False
) -> tuple[Decimal, mover.TrailPlan]:
    """The distance the Mover's own planning gives for `book`, computed here apart from the task: the
    Mover's `_entry_trail` on an ATR warmed with the same bars, then `sizing.sized_stop`."""
    bid, ask, mid, width = book
    planner = mover.Mover(
        mover.MoverConfig(
            max_notional=D(1),
            max_move=D(1),
            size_from_budget=size_from_budget,
            loss_share=mover.PAPER_LOSS_SHARE if size_from_budget else None,
            **({"trail_mode": "atr"} if row is None else protect.ROWS[row]),
        )
    )
    atr = AverageTrueRange(planner.settings.atr_n)
    for _ in range(20):
        atr.update_raw(float(D(mid) + D(width) / 2), float(D(mid) - D(width) / 2), float(mid))
    slot = mover.Slot(
        InstrumentId.from_str("AAA.ALPACA"),
        BarType.from_str("AAA.ALPACA-1-MINUTE-LAST-EXTERNAL"),
        deque(),
        deque(),
        atr=atr,
    )
    touch = SimpleNamespace(bid_price=Price.from_str(bid), ask_price=Price.from_str(ask))
    plan = planner._entry_trail(slot, D(ask), touch)
    return D(ask) - sizing.sized_stop(D(ask), plan.trail), plan


def _run(paper: Paper, data: Data, capsys, row: str | None = "OWN1") -> tuple[int, list[str]]:
    """A sending run under `row`: OWN1 unless a case names another, the ATR row most cases plan on."""
    code = protect.protect_task(cast(Any, paper), data, row=row)
    out = capsys.readouterr()
    return code, (out.out + out.err).splitlines()


def _no_sale_open_or_cancel(paper: Paper) -> None:
    """Test 8's assertion, made in every case: every call the fake client saw is a read, a look-up or
    the submit of a GTC trailing stop that sells; nothing was sold by market or limit, opened, canceled
    or replaced."""
    allowed = {
        "get_clock",
        "get_account_configurations",
        "get_all_positions",
        "get_orders",
        "get_open_position",
        "get_order_by_client_id",
        "submit_order",
    }
    assert {name for name, _ in paper.calls} <= allowed
    for request in paper.submitted:
        assert isinstance(request, TrailingStopOrderRequest)
        assert request.side.value == "sell"
        assert request.type.value == "trailing_stop"
        assert request.time_in_force.value == "gtc"
        assert request.extended_hours in (None, False)
        assert (request.trail_price is None) != (request.trail_percent is None)
        # marked: Alpaca is told the sell closes a long, whatever the account's setting
        assert request.position_intent == PositionIntent.SELL_TO_CLOSE


def test_a_protect_client_order_id_is_of_our_lineage() -> None:
    """A stop the task sent covers its long on the next run: its ID is ours (`custody.lineage`)."""
    assert custody.lineage("canon-protect-20261112T150000Z-1-6168c4fe") == custody.PROTECT
    for foreign in ("canon-protect-", "canon-protect-manual", "canon-protect-20261112T150000Z-1"):
        assert custody.lineage(foreign) == custody.FOREIGN


def test_two_uncovered_longs_get_one_gtc_trailing_stop_each_at_the_movers_distance(capsys) -> None:
    """Test 1: a long-only account with two uncovered longs: two stops, GTC, of our lineage, each at the
    distance the Mover's planning gives the symbol, and at least the spread floor."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK})
    code, lines = _run(paper, data, capsys)
    assert code == 0, lines
    _no_sale_open_or_cancel(paper)
    sent = {r.symbol: r for r in paper.submitted}
    assert sorted(sent) == ["AAA", "BBB"]
    for symbol, book, qty, spread in (
        ("AAA", AAA_BOOK, 10, D("0.01")),
        ("BBB", BBB_BOOK, 5, D("0.05")),
    ):
        request = sent[symbol]
        distance, plan = _planned(book)
        assert request.qty == qty
        assert D(str(request.trail_price)) == distance
        assert D(str(request.trail_price)) >= 2 * spread  # the floor: two quoted spreads
        assert custody.lineage(request.client_order_id) == custody.PROTECT
        assert request.client_order_id.startswith(protect.PROTECT_PREFIX)
        (line,) = [x for x in lines if x.startswith(f"sent {symbol}:")]
        assert f"sell {qty} {symbol}" in line and f"distance {distance}" in line
        assert f"from {plan.bound}" in line and "lineage protect" in line
        assert "quote 1 s old" in line
    assert sent["AAA"].trail_price == pytest.approx(0.04)  # ATR 0.04 a share binds
    assert sent["BBB"].trail_price == pytest.approx(0.10)  # two 5-cent spreads bind
    assert "from atr" in next(x for x in lines if x.startswith("sent AAA:"))
    assert "from spread floor" in next(x for x in lines if x.startswith("sent BBB:"))
    assert len(paper.submitted) == 2  # one each, sent once


def test_a_long_fully_covered_by_a_foreign_sell_is_left_alone_and_named(capsys) -> None:
    """Test 2: nothing sent, the long named with why."""
    paper = Paper(positions=[_position("AAA", "10")], orders=[_order("AAA", "10", FOREIGN_SELL)])
    data = Data({"AAA": AAA_BOOK})
    code, lines = _run(paper, data, capsys)
    _no_sale_open_or_cancel(paper)
    assert paper.submitted == []
    assert code == 0
    (line,) = [x for x in lines if x.startswith("left alone AAA:")]
    assert "fully covered" in line and "foreign" in line
    assert FOREIGN_SELL not in "\n".join(lines)  # a foreign order is never named by its ID


def test_a_long_covered_in_part_by_our_lineage_gets_a_stop_for_the_rest(capsys) -> None:
    """Test 3."""
    ours = _order(
        "AAA",
        "4",
        OUR_SELL,
        type="trailing_stop",
        order_type="trailing_stop",
        limit_price=None,
        trail_price="0.05",
    )
    paper = Paper(positions=[_position("AAA", "10")], orders=[ours])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0, lines
    _no_sale_open_or_cancel(paper)
    (request,) = paper.submitted
    assert (request.symbol, request.qty) == ("AAA", 6)
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "4 covered by ours" in line


def test_a_long_covered_in_part_by_a_foreign_order_is_named_and_left_untouched(capsys) -> None:
    """Test 4: the symbol is left untouched; another uncovered long still gets its stop."""
    paper = Paper(
        positions=[_position("AAA", "10"), _position("BBB", "5")],
        orders=[_order("AAA", "4", FOREIGN_SELL)],
    )
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK})
    code, lines = _run(paper, data, capsys)
    _no_sale_open_or_cancel(paper)
    assert [r.symbol for r in paper.submitted] == ["BBB"]
    assert all(r.symbol_or_symbols != "AAA" for _, r in data.asked)  # untouched: not even planned
    assert code == 2  # a long left uncovered, by name
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "covered in part by a foreign order" in line and "left untouched" in line


def test_an_account_that_allows_shorting_gets_the_same_marked_stop_and_is_told_what_it_rests_on(
    capsys,
) -> None:
    """Test 5, turned (the lead's ruling, 2026-11-13): the account's setting no longer decides. That a
    stop never opens or adds to a short rests on the holding and the free shares read in the send step,
    not on the stop's `sell_to_close` mark, and the run says what it does where the account allows
    shorting."""
    paper = Paper(positions=[_position("AAA", "10")], no_shorting=False)
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0, lines
    _no_sale_open_or_cancel(paper)
    (request,) = paper.submitted
    assert request.qty == 10 and request.position_intent == PositionIntent.SELL_TO_CLOSE
    (said,) = [x for x in lines if x.startswith("account: no_shorting false")]
    assert "sell_to_close" in said and "free" in said
    # the send step read the holding and the free shares, after the plan and before the submit
    names = [name for name, _ in paper.calls]
    assert names.index("get_open_position") < names.index("submit_order")
    # a long-only account is named as that, and gets the same marked stop
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0 and [x for x in lines if x.startswith("account: no_shorting true")]
    assert paper.submitted[0].position_intent == PositionIntent.SELL_TO_CLOSE


def test_a_short_is_named_and_left_untouched_and_the_long_beside_it_is_protected(capsys) -> None:
    """A short gets no order of any kind and no market data read: it is named and left. A long in
    another symbol is protected as on any account."""
    paper = Paper(
        positions=[_position("AAA", "10"), _position("BBB", "5", "short")], no_shorting=False
    )
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK})
    code, lines = _run(paper, data, capsys)
    assert code == 0, lines
    _no_sale_open_or_cancel(paper)
    assert [r.symbol for r in paper.submitted] == ["AAA"]
    assert all(request.symbol_or_symbols == "AAA" for _, request in data.asked)
    (line,) = [x for x in lines if x.startswith("left alone BBB:")]
    assert "a short of 5" in line and "nothing sent" in line


BIG_BOOK = ("499.99", "500.00", "500.00", "0.10")
"""A $500 name a cent wide with a 10-cent ATR: the Mover's distance is far under 0.1% of the price."""


def test_the_distance_is_never_under_alpacas_smallest_trail(capsys) -> None:
    """Alpaca refused a `trail_price` under 0.1% of the stock's price on paper (2026-11-13, 422), so a
    distance under it would leave the long with no stop. It is raised to 0.1% of the price, rounded up
    to the price step, and one step over; the line says which floor bound it. One over it stands."""
    planned, _plan = _planned(BIG_BOOK)
    smallest = D("500.00") * D("0.001")
    assert planned < smallest, "the case needs a distance the Mover plans under Alpaca's smallest"
    paper = Paper(positions=[_position("BIG", "3")])
    code, lines = _run(paper, Data({"BIG": BIG_BOOK}), capsys)
    assert code == 0, lines
    (request,) = paper.submitted
    assert D(str(request.trail_price)) == D("0.51")
    (line,) = [x for x in lines if x.startswith("sent BIG:")]
    assert "distance 0.51" in line and "0.1020% of 500.00" in line
    assert "bound by Alpaca's smallest trail" in line and f"over the Mover's {planned}" in line
    # AAA's 0.04 on $10 is 0.4%: over the smallest, and unchanged
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert D(str(paper.submitted[0].trail_price)) == D("0.04")
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "0.4000% of 10.00" in line and "bound by Alpaca's smallest trail" not in line


def test_plan_only_names_every_registered_row_for_each_long_and_sends_nothing(capsys) -> None:
    """Plan-only (the lead's term for a first run on an account, 2026-11-13): every read of a sending
    run, the holding's own read before the send included, and for each long one line under EVERY
    registered row: the distance as a share of the price, the level the stop would open at, and what
    bound it. No order of any kind, and no row need be named."""
    paper = Paper(
        positions=[_position("AAA", "10"), _position("BBB", "5"), _position("BIG", "3")],
        orders=[_order("BBB", "5", FOREIGN_SELL, side="sell")],
        no_shorting=False,
    )
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK, "BIG": BIG_BOOK})
    code = protect.protect_task(cast(Any, paper), data, plan_only=True)
    out = capsys.readouterr()
    lines = (out.out + out.err).splitlines()
    assert code == 0, lines
    assert paper.submitted == [] and "submit_order" not in {name for name, _ in paper.calls}
    _no_sale_open_or_cancel(paper)
    rows = {
        row: next(x for x in lines if x.startswith(f"would send AAA under {row}:"))
        for row in ("F2", "OWN1", "OWN3")
    }
    for line in rows.values():
        assert "trailing stop sell 10 AAA, GTC, sell_to_close" in line
    # AAA: bid 9.99, ask 10.00, ATR 0.04. The fixed row is 2% of the price; the ATR rows 1 and 3 ATRs
    assert "distance 0.20" in rows["F2"] and "2.0000% of 10.00" in rows["F2"]
    assert "the fixed row" in rows["F2"] and "stop opens about 9.79" in rows["F2"]
    assert "distance 0.04" in rows["OWN1"] and "0.4000% of 10.00" in rows["OWN1"]
    assert "from atr" in rows["OWN1"] and "stop opens about 9.95" in rows["OWN1"]
    assert "distance 0.12" in rows["OWN3"] and "1.2000% of 10.00" in rows["OWN3"]
    assert "from atr" in rows["OWN3"] and "stop opens about 9.87" in rows["OWN3"]
    big = next(x for x in lines if x.startswith("would send BIG under OWN1:"))
    assert "distance 0.51" in big and "bound by Alpaca's smallest trail" in big
    assert [x for x in lines if x.startswith("left alone BBB:")]
    assert not [x for x in lines if x.startswith("sent ")]
    assert lines[-1] == "plan only: nothing was sent"
    # one quote and one bar read a symbol, whatever the number of rows; the send step's read is made
    assert [name for name, r in data.asked if r.symbol_or_symbols == "AAA"] == [
        "get_stock_latest_quote",
        "get_stock_bars",
    ]
    assert ("get_open_position", "AAA") in paper.calls
    # a long with no distance is still named, and the exit says a long is left uncovered
    paper = Paper(positions=[_position("ZZZ", "4")])
    assert protect.protect_task(cast(Any, paper), Data({}), plan_only=True) == 2
    assert paper.submitted == []


def test_a_send_without_a_named_row_is_refused_before_any_read(capsys) -> None:
    """No default row (the lead's ruling, 2026-11-13): the run that sends names the registered row it
    plans on, and one that names none, or one nobody registered, reads nothing and sends nothing."""
    for row, said in ((None, "names no row"), ("F9", "no registered row named 'F9'")):
        paper = Paper(positions=[_position("AAA", "10")])
        data = Data({"AAA": AAA_BOOK})
        code, lines = _run(paper, data, capsys, row=row)
        assert code == 3 and paper.calls == [] and data.asked == []
        (line,) = [x for x in lines if x.startswith("refused:")]
        assert said in line and "F2, OWN1, OWN3" in line and "nothing was read or sent" in line


@pytest.mark.parametrize(
    ("row", "distance", "share", "bound"),
    [
        ("F2", "0.20", "2.0000%", "the fixed row"),
        ("OWN1", "0.04", "0.4000%", "from atr"),
        ("OWN3", "0.12", "1.2000%", "from atr"),
    ],
)
def test_the_stop_goes_out_at_the_named_rows_distance(capsys, row, distance, share, bound) -> None:
    """The rows are the Mover's registered arms, read from the registry the window uses
    (`session_window.TRAIL_ARMS`), never a figure of this task's own."""
    assert set(protect.ROWS) == set(protect.session_window.TRAIL_ARMS) == {"F2", "OWN1", "OWN3"}
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys, row=row)
    assert code == 0, lines
    (request,) = paper.submitted
    assert D(str(request.trail_price)) == D(distance)
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert f"row {row}" in line and f"distance {distance}" in line
    assert f"{share} of 10.00" in line and bound in line


OWN_BOOK = ("9.99", "10.00", "10.00", "0.25")
"""Synthetic ATR 0.25 at 10.00: OWN1 plans 0.025 of entry, OWN3 plans 0.075."""


@pytest.mark.parametrize(
    ("row", "book", "distance", "bound"),
    [
        ("OWN1", OWN_BOOK, "0.25", "atr"),
        ("OWN1", ("9.85", "10.00", "10.00", "0.25"), "0.30", "spread floor"),
        ("OWN1", ("9.88", "10.00", "10.00", "0.0625"), "0.24", "spread floor"),
        ("OWN3", OWN_BOOK, "0.75", "atr"),
        ("OWN3", ("9.60", "10.00", "10.00", "0.25"), "0.80", "spread floor"),
    ],
    ids=["own1-atr", "own1-floor", "own1-floor-only", "own3-atr", "own3-floor"],
)
def test_a_found_long_keeps_its_own_distance_over_the_entry_cap(
    capsys, row, book, distance, bound
) -> None:
    """A holding has no entry size left to choose: retain its ATR, raised to the spread floor,
    under either registered own-cap row (the lead's ruling, 2026-11-13)."""
    assert protect.ROWS[row]["trail_cap"] == "own"
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": book}), capsys, row=row)
    assert code == 0, lines
    (request,) = paper.submitted
    assert D(str(request.trail_price)) == D(distance)
    assert request.qty == 10
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert f"row {row}" in line and f"from {bound}" in line
    assert f"distance {distance}" in line and "fixed trail fallback" not in line
    _no_sale_open_or_cancel(paper)


def test_plan_only_keeps_f2_and_names_the_found_longs_own_atr_distance(capsys) -> None:
    """F2 keeps its main behavior; OWN3 shares OWN1's own-cap policy, with its registered k=3."""
    paper = Paper(positions=[_position("AAA", "10")])
    assert protect.protect_task(cast(Any, paper), Data({"AAA": OWN_BOOK}), plan_only=True) == 0
    out = capsys.readouterr().out.splitlines()
    (fixed,) = [x for x in out if x.startswith("would send AAA under F2:")]
    assert "distance 0.2000 (row F2, 2.0000% of 10.0000" in fixed
    assert "the fixed row (2% of the price)" in fixed
    (own1,) = [x for x in out if x.startswith("would send AAA under OWN1:")]
    assert (
        "trailing stop sell 10 AAA, GTC, sell_to_close, distance 0.2500 "
        "(row OWN1, 2.5000% of 10.0000, stop opens about 9.7400 "
        "(the bid 9.99 less the distance; Alpaca sets it from its own mark), from atr, "
        "trail 0.025000 of entry 10.0000, spread 0.01, floor 0.02 (2 spreads), ATR 0.250000"
    ) in own1
    (own3,) = [x for x in out if x.startswith("would send AAA under OWN3:")]
    assert "distance 0.7500 (row OWN3, 7.5000% of 10.0000" in own3
    assert "from atr" in own3
    assert paper.submitted == []
    assert out[-1] == "plan only: nothing was sent"


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
@pytest.mark.parametrize(
    ("book", "source"),
    [
        (("9.99", "10.00", "10.00", "1.25"), "atr"),
        (("9.40", "10.00", "10.00", "0.25"), "spread floor"),
    ],
    ids=["atr", "floor"],
)
def test_an_own_distance_over_the_ceiling_is_named_and_sends_no_stop(
    capsys, row, book, source
) -> None:
    """Refuse the symbol by the sizing ceiling's name, never substitute a fixed fraction."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    code, lines = _run(paper, Data({"AAA": book, "BBB": BBB_BOOK}), capsys, row=row)
    assert code == 2
    assert [r.symbol for r in paper.submitted] == ["BBB"]
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert f"under {row}" in line
    assert "distance_over_ceiling" in line and "0.10" in line and source in line
    paper = Paper(positions=[_position("AAA", "10")])
    code = protect.protect_task(cast(Any, paper), Data({"AAA": book}), plan_only=True)
    out = capsys.readouterr().out.splitlines()
    if source == "spread floor":
        # F2 is below this floor too: every row is refused, as at main.
        assert code == 2
        (refused,) = [x for x in out if x.startswith("unprotected AAA:")]
        assert f"under {row}: distance_over_ceiling" in refused
    else:
        assert code == 0  # F2 still supplies a plan.
        (refused,) = [x for x in out if x.startswith(f"no stop for AAA under {row}:")]
    assert "distance_over_ceiling" in refused and "0.10" in refused
    assert paper.submitted == []


@pytest.mark.parametrize(
    "book",
    [("9.99", "10.00", "10.00", "1.00"), ("9.50", "10.00", "10.00", "0.25")],
    ids=["atr", "floor"],
)
def test_an_own_distance_at_the_ceiling_stands(capsys, book) -> None:
    """The same inclusive ceiling as sizing: equality is allowed."""
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": book}), capsys)
    assert code == 0, lines
    assert D(str(paper.submitted[0].trail_price)) == D("1.00")


def test_an_own_distance_stepped_over_the_ceiling_is_refused(capsys) -> None:
    """Check the stepped distance as well as the fraction, as sizing does."""
    paper = Paper(positions=[_position("AAA", "10")])
    # 0.125 ATR / 1.26 is under the ceiling, but the stop steps down to 1.13:
    # the 0.13 distance exceeds 0.126. The synthetic bars have an exact binary range.
    code, lines = _run(paper, Data({"AAA": ("1.25", "1.26", "1.25", "0.125")}), capsys)
    assert code == 2 and paper.submitted == [], lines
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "distance_over_ceiling" in line


@pytest.mark.parametrize(
    ("book", "distance", "bound", "refusal"),
    [
        (OWN_BOOK, "0.25", "atr", "distance_over_cap"),
        (("9.85", "10.00", "10.00", "0.25"), "0.30", "spread floor", "floor_over_cap"),
    ],
    ids=["atr", "floor"],
)
def test_found_holding_matches_sized_entry_and_leaves_unsized_entry_refused(
    book, distance, bound, refusal
) -> None:
    """The entry defaults stay as at main, before and after planning a found holding."""
    entry_distance, entry_plan = _planned(book, row="OWN1", size_from_budget=True)
    assert entry_distance == D(distance) and entry_plan.bound == bound
    with pytest.raises(mover.DistanceRefusal) as before:
        _planned(book, row="OWN1")
    assert before.value.cause == refusal
    held_distance, held = protect.plan_distance(Data({"AAA": book}), "AAA", NOW, "OWN1")
    assert held_distance == entry_distance and held.plan == entry_plan
    assert _planned(book, row="OWN1", size_from_budget=True) == (entry_distance, entry_plan)
    with pytest.raises(mover.DistanceRefusal) as after:
        _planned(book, row="OWN1")
    assert after.value.cause == refusal


def test_a_holding_that_turned_short_since_the_read_gets_no_stop(capsys) -> None:
    """The send step's read checks the side as well as the count: a position that is no longer a long
    gets nothing, whatever its quantity reads."""
    paper = Paper(positions=[_position("AAA", "10")])
    paper.later["AAA"] = _position("AAA", "10", "short")
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert paper.submitted == [] and code == 2
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "no longer a long" in line


def test_a_stop_signal_during_the_send_names_the_stop_that_may_be_at_alpaca(capsys) -> None:
    """The task's stop signal can arrive inside the one send: the stop may already be at Alpaca, so the
    run names the symbol and says so before it ends, and sends nothing more."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    original = paper.submit_order

    def stopped(order_data):
        original(order_data)
        raise protect.Stopped

    paper.submit_order = stopped  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), capsys)
    assert code == 1 and len(paper.submitted) == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "stopped during the send" in line and "may yet show it working" in line
    assert [x for x in lines if x.startswith("failed part way")]


def test_a_quotes_age_is_measured_when_its_symbol_is_planned_not_when_the_run_began(
    capsys, monkeypatch
) -> None:
    """The clock is read once, at the start, and a later symbol is planned later: its quote's age is
    counted from the clock's time plus the time the run has taken, so a quote does not pass as fresh
    because the run began before it aged."""
    passed = [0.0]
    monkeypatch.setattr(protect, "monotonic", lambda: passed[0])
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    original = paper.submit_order

    def slow(order_data):
        passed[0] += 61.0  # the first symbol's send took a minute
        return original(order_data)

    paper.submit_order = slow  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), capsys)
    assert code == 2 and [r.symbol for r in paper.submitted] == ["AAA"]
    (line,) = [x for x in lines if x.startswith("unprotected BBB:")]
    assert "62 s old" in line


def test_a_submit_answered_with_an_ended_order_is_a_failure_and_not_a_stop(capsys) -> None:
    """Alpaca can answer the submit itself with an order already rejected: that is no protection, as
    the same status found after a lost answer is none (a second reader's finding, 2026-11-13)."""
    paper = Paper(positions=[_position("AAA", "10")])

    def rejected(order_data):
        paper.submitted.append(order_data)
        return _order("AAA", "10", order_data.client_order_id, status="rejected")

    paper.submit_order = rejected  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 1 and len(paper.submitted) == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "answered rejected" in line and "the long has no stop" in line
    assert not [x for x in lines if x.startswith("sent AAA:")]


def test_a_quote_that_aged_past_its_bound_while_its_symbol_was_planned_sends_nothing(
    capsys, monkeypatch
) -> None:
    """The quote is read, then the bars, then the holding: a slow read between the quote and the send
    ages it. Its age is counted again just before the send (a second reader's finding)."""
    passed = [0.0]
    monkeypatch.setattr(protect, "monotonic", lambda: passed[0])
    paper = Paper(positions=[_position("AAA", "10")])
    original = paper.get_open_position

    def slow(symbol):
        passed[0] += 61.0  # the send step's own read took a minute
        return original(symbol)

    paper.get_open_position = slow  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 2 and paper.submitted == []
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "62 s old" in line and "run again" in line


def test_a_quote_stamped_after_the_clocks_read_is_no_seconds_old(capsys) -> None:
    """The quote is read after the clock, so its stamp can be the newer by a moment: its age reads as
    none, never as a negative count (seen on paper, 2026-11-13, as "-0 s old")."""
    paper = Paper(positions=[_position("AAA", "10")])
    data = Data({"AAA": AAA_BOOK}, quoted="2026-11-12T15:00:00.300000Z")
    code, lines = _run(paper, data, capsys)
    assert code == 0, lines
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "quote 0 s old" in line and "-0 s" not in line


CHEAP_BOOK = ("1.03", "1.04", "1.04", "0.004")
"""A $1.04 name: 2% of it is 2.08 cents, and the stop steps down a whole cent, so the dollar trail is
three cents, 2.88% of the price."""


def test_a_trail_the_price_step_carries_over_the_cap_is_said_in_its_line(capsys) -> None:
    """On a low-priced name the step rounds the stop down a whole cent and the dollar trail lands over
    the 2% the row names. The stop still goes out, since the Mover sizes against the same stepped
    stop, and its line says by how much (a second reader's finding)."""
    paper = Paper(positions=[_position("LOW", "100")])
    code, lines = _run(paper, Data({"LOW": CHEAP_BOOK}), capsys, row="F2")
    assert code == 0, lines
    assert D(str(paper.submitted[0].trail_price)) == D("0.03")
    (line,) = [x for x in lines if x.startswith("sent LOW:")]
    assert "2.8846% of 1.04" in line and "over the 0.02 cap by the price step" in line
    # where the step does not carry it over, nothing of the kind is said
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys, row="F2")
    assert "over the 0.02 cap" not in next(x for x in lines if x.startswith("sent AAA:"))


def _paper_test() -> Any:
    """The paper test's module, loaded by path: its cleanup's reporting is checked here, offline."""
    spec = importlib.util.spec_from_file_location(
        "paper_protect_task", ROOT / "tests" / "test_paper_protect_task.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_paper_tests_cleanup_problems_are_reported_when_its_body_failed_too(capsys) -> None:
    """A body that raises must not hide what the cleanup could not put back: a setting left off is
    named in the failure whatever else failed (a second reader's P1, 2026-11-13)."""
    paper_test = _paper_test()

    def body() -> None:
        raise RuntimeError("the task raised")

    def clean(problems: list[str]) -> None:
        problems.append("no_shorting is NOT true after two attempts")

    with pytest.raises(AssertionError) as failure:
        paper_test._finish(body, clean)
    assert "no_shorting is NOT true" in str(failure.value)
    assert isinstance(failure.value.__cause__, RuntimeError)
    assert "LEFT BY THE CLEANUP: no_shorting is NOT true" in capsys.readouterr().out
    # a clean cleanup lets the body's own failure through, and a clean run passes
    with pytest.raises(RuntimeError, match="the task raised"):
        paper_test._finish(body, lambda problems: None)
    paper_test._finish(lambda: None, lambda problems: None)
    with pytest.raises(AssertionError, match="left open"):
        paper_test._finish(lambda: None, lambda problems: problems.append("an order left open"))


@pytest.mark.parametrize(
    ("plan_only", "row", "code", "sends"),
    [
        ("true", "", 0, 0),
        ("true", "F2", 0, 0),
        ("false", "F2", 0, 1),
        ("false", "", 3, 0),
        ("", "F2", 3, 0),
        ("True", "F2", 3, 0),
        ("false", "F9", 3, 0),
    ],
)
def test_the_tasks_flags_send_only_on_an_exact_false_and_a_registered_row(
    capsys, plan_only, row, code, sends
) -> None:
    """`paper:protect` hands its two flags over as text. Anything but an exact `true` or `false` for
    plan-only is refused, so a value this code does not know can never become a send."""
    paper = Paper(positions=[_position("AAA", "10")])
    got = protect.task_from_flags(
        plan_only, row, trading=cast(Any, paper), data=Data({"AAA": AAA_BOOK})
    )
    assert got == code and len(paper.submitted) == sends
    if code == 3:
        assert paper.calls == []


def test_outside_the_regular_session_it_refuses_after_the_clock_read_alone(capsys) -> None:
    """Test 6: Alpaca's clock says closed; no request but the clock read."""
    paper = Paper(positions=[_position("AAA", "10")], is_open=False)
    data = Data({"AAA": AAA_BOOK})
    code, lines = _run(paper, data, capsys)
    assert paper.calls == [("get_clock", None)] and data.asked == []
    assert code == 3
    (line,) = [x for x in lines if x.startswith("refused:")]
    assert "outside the regular session" in line


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        (
            APIError('{"code": 40310000, "message": "insufficient qty"}', _status(403)),
            "not placed: APIError (HTTP 403)",
        ),
        (requests.ReadTimeout("read timed out"), "no answer and not found"),
    ],
    ids=["refused", "no-answer"],
)
def test_a_send_that_fails_is_named_never_resent_and_exits_non_zero(capsys, failure, said) -> None:
    """Test 7: one attempt; a lost answer is looked up by its ID, never sent again."""
    paper = Paper(positions=[_position("AAA", "10")])
    paper.submit_fails = failure
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    _no_sale_open_or_cancel(paper)
    assert len(paper.submitted) == 1
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert said in line
    assert "insufficient qty" not in "\n".join(lines)  # an error by name, never its body


def test_a_lost_answer_found_by_its_id_counts_as_sent_and_is_not_sent_again(capsys) -> None:
    """Test 7's other side: the look-up by ID finds the stop working, so the long is covered."""
    paper = Paper(positions=[_position("AAA", "10")])
    paper.submit_fails = requests.ReadTimeout("read timed out")
    original = paper.get_order_by_client_id

    def found(client_id):
        paper.found[client_id] = _order("AAA", "10", client_id, type="trailing_stop")
        return original(client_id)

    paper.get_order_by_client_id = found  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    _no_sale_open_or_cancel(paper)
    assert len(paper.submitted) == 1 and code == 0
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "no answer (ReadTimeout), found by its ID: new" in line


def test_a_lost_answer_found_by_its_id_but_ended_is_a_failure_and_not_a_stop(capsys) -> None:
    """The look-up finds the order, and Alpaca rejected it: the long has no working stop, and the run
    says so and exits 1, never 'sent'."""
    paper = Paper(positions=[_position("AAA", "10")])
    paper.submit_fails = requests.ReadTimeout("read timed out")
    original = paper.get_order_by_client_id

    def found(client_id):
        paper.found[client_id] = _order(
            "AAA", "10", client_id, type="trailing_stop", status="rejected"
        )
        return original(client_id)

    paper.get_order_by_client_id = found  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert len(paper.submitted) == 1 and code == 1
    assert not [x for x in lines if x.startswith("sent AAA:")]
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "found by its ID but ended: rejected" in line


class Rest(Paper):
    """The stand-in with alpaca-py's count of repeats (`RESTClient._retry`), read at each submit. It is
    no subclass of `RESTClient`: the adapter's census of alpaca-py's REST clients counts every one."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._retry = 3
        self.retries_at_submit: list[int] = []

    def submit_order(self, order_data):
        self.retries_at_submit.append(self._retry)
        return Paper.submit_order(self, order_data)


def test_the_stop_is_sent_with_alpaca_pys_retries_off_and_they_are_put_back(
    capsys, monkeypatch
) -> None:
    """`session_window.send_once` acts on a `RESTClient` alone, so it is stood in for here, as
    custody's own case does."""
    monkeypatch.setattr(protect.session_window, "send_once", lambda t: setattr(t, "_retry", 0))
    paper = Rest(positions=[_position("AAA", "10")])
    code, _ = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0 and paper.retries_at_submit == [0] and paper._retry == 3


@pytest.mark.parametrize(
    ("later", "why"),
    [
        (_position("AAA", "6"), "10 held at the read and 6 now"),
        (_position("AAA", "10", available="4"), "only 4 of 10 not held by an order"),
        (_position("AAA", "10", available=None), "does not show the quantity no order holds"),
        (None, "no longer held"),
    ],
    ids=["sold-in-part", "a-sell-appeared", "not-shown", "gone"],
)
def test_a_holding_that_changed_since_the_read_gets_no_stop_and_is_named(
    capsys, later, why
) -> None:
    """The positions and the orders are two reads, and both age while earlier symbols are planned and
    sent. Just before its send a symbol's position is read again, with Alpaca's own count of the
    shares no order holds: anything but the same holding with that many free sends nothing."""
    paper = Paper(positions=[_position("AAA", "10")])
    paper.later["AAA"] = later
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert paper.submitted == [] and code == 2
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert why in line and "run again" in line


def test_a_stop_for_part_of_a_long_is_checked_against_what_alpaca_says_is_free(capsys) -> None:
    """Ours covers 6 of 10, so the stop is for 4, and Alpaca shows 4 not held by an order: sent."""
    paper = Paper(
        positions=[_position("AAA", "10", available="4")],
        orders=[_order("AAA", "6", OUR_SELL)],
    )
    code, _ = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0 and [r.qty for r in paper.submitted] == [4]


def test_a_clock_that_cannot_be_read_names_no_session(capsys) -> None:
    paper = Paper(positions=[_position("AAA", "10")])
    paper.clock_fails = requests.ReadTimeout("read timed out")
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 1 and paper.submitted == []
    assert not [x for x in lines if x.startswith("session:")]
    assert [x for x in lines if "Alpaca's clock" in x and "ReadTimeout" in x]


def test_each_line_is_printed_as_it_happens_so_a_stopped_run_shows_what_it_sent(capsys) -> None:
    """Two longs; the second's quote read is interrupted as Ctrl-C does. The first stop is at Alpaca
    and its line is already out."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK})
    data.fails["BBB"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        protect.protect_task(cast(Any, paper), data, row="OWN1")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "session: regular (Alpaca's clock)"
    assert [x.split(":")[0] for x in out[1:]] == [
        "account",
        "sent AAA",
    ]  # the setting, then the stop
    assert len(paper.submitted) == 1


def test_the_tasks_stop_signal_ends_the_run_as_a_failure_naming_what_was_sent(capsys) -> None:
    """SIGTERM is what the task's `timeout` sends: handled as `paper:close` handles it, the run ends
    part way by name with exit 1, and the handler is put back."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    data = Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK})
    before = signal.getsignal(signal.SIGTERM)
    original = data.get_stock_latest_quote

    def stopped(request):
        if request.symbol_or_symbols == "BBB":
            os.kill(os.getpid(), signal.SIGTERM)
        return original(request)

    data.get_stock_latest_quote = stopped  # type: ignore[method-assign]
    code = protect.protect_task(cast(Any, paper), data, row="OWN1")
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and signal.getsignal(signal.SIGTERM) is before
    assert [x.split(":")[0] for x in out[1:]] == ["account", "sent AAA", "failed part way"]
    assert len(paper.submitted) == 1


@pytest.mark.parametrize(
    ("order", "why"),
    [
        (
            {"client_order_id": FOREIGN_SELL, "order_class": "oco"},
            "is part of a oco order",
        ),
        (
            {"client_order_id": FOREIGN_SELL, "order_class": "bracket", "status": "held"},
            "is part of a bracket order",
        ),
        ({"client_order_id": OUR_SELL, "status": "pending_cancel"}, "is pending_cancel"),
        ({"client_order_id": OUR_SELL, "status": "pending_replace"}, "is pending_replace"),
    ],
    ids=["foreign-oco", "foreign-bracket-leg", "ours-pending-cancel", "ours-pending-replace"],
)
def test_a_sell_whose_cover_cannot_be_summed_leaves_its_symbol_untouched(
    capsys, order, why
) -> None:
    """Two legs of one order hold the same shares once, and a sell on its way out soon holds none:
    summed as they are listed they would read as a long covered in full. Such a sell is named and
    the symbol left untouched, never 'left alone'."""
    fields = dict(order)
    paper = Paper(
        positions=[_position("AAA", "10")],
        orders=[
            _order("AAA", "5", fields.pop("client_order_id"), **fields),
            _order("AAA", "5", FOREIGN_SELL),
        ],
    )
    data = Data({"AAA": AAA_BOOK})
    code, lines = _run(paper, data, capsys)
    assert paper.submitted == [] and code == 2 and data.asked == []
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert why in line and "left untouched" in line
    assert not [x for x in lines if x.startswith("left alone")]


def test_cover_counts_what_a_sell_has_left_to_fill_and_whole_shares_only(capsys) -> None:
    """Ours sold 4 of 10 already and 6 rest: of 10.5 held now, 4.5 are uncovered, so the stop is for
    4 and the half share is named as uncovered. A sell that names dollars, not shares, covers an
    unknown quantity: its symbol is left untouched."""
    paper = Paper(
        positions=[
            _position("AAA", "10.5", available="4.5"),
            _position("BBB", "5"),
        ],
        orders=[
            _order("AAA", "10", OUR_SELL, filled_qty="4", status="partially_filled"),
            _order("BBB", None, FOREIGN_SELL, notional="50", type="market", limit_price=None),
        ],
    )
    code, lines = _run(paper, Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), capsys)
    assert code == 2 and [(r.symbol, r.qty) for r in paper.submitted] == [("AAA", 4)]
    assert [x for x in lines if x.startswith("unprotected AAA: 0.5 of 10.5 uncovered")]
    assert [x for x in lines if x.startswith("unprotected BBB:") and "no share quantity" in x]


def test_a_full_page_of_open_orders_is_refused_since_a_covering_sell_may_be_missing(capsys) -> None:
    orders = [_order(f"S{n}", "1", FOREIGN_SELL) for n in range(protect.ORDERS_PAGE_LIMIT)]
    paper = Paper(positions=[_position("AAA", "10")], orders=orders)
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 3 and paper.submitted == []
    assert "the most one listing returns" in "\n".join(lines)


@pytest.mark.parametrize(
    ("book", "data_kw", "why"),
    [
        (("9.99", "10.00", "10.00", "0.04"), {"quoted": "2026-11-12T14:58:59Z"}, "61 s old"),
        (("10.01", "10.00", "10.00", "0.04"), {}, "no usable sip spread"),
        (("10.00", "10.00", "10.00", "0.04"), {}, "no usable sip spread"),
        (("0", "10.00", "10.00", "0.04"), {}, "no usable sip spread"),
        (("9.99", "0", "10.00", "0.04"), {}, "has no ask"),
        (("9.40", "10.00", "10.00", "0.04"), {}, "distance_over_ceiling"),
        (None, {}, "no latest sip quote"),
    ],
    ids=[
        "stale-quote",
        "crossed",
        "locked",
        "no-bid",
        "no-ask",
        "floor-over-the-ceiling",
        "no-quote",
    ],
)
def test_a_long_with_no_sound_distance_gets_no_stop_and_is_named(
    capsys, book, data_kw, why
) -> None:
    """A stop is never sent at a distance the planning could not stand behind: an old quote, a
    crossed, locked or one-sided one, none at all, or a spread whose floor is over the ceiling."""
    paper = Paper(positions=[_position("AAA", "10")])
    data = Data({} if book is None else {"AAA": book}, **data_kw)
    code, lines = _run(paper, data, capsys)
    assert paper.submitted == [] and code == 2
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "no distance" in line and why in line


def test_a_cold_atr_takes_the_movers_named_fallback_never_under_the_floor(capsys) -> None:
    """Fewer complete minutes than the ATR needs: the Mover's planning names its fixed fallback, and
    the stop goes out at it."""
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}, minutes=5), capsys)
    assert code == 0 and len(paper.submitted) == 1
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "ATR cold" in line
    assert D(str(paper.submitted[0].trail_price)) >= 2 * D("0.01")


def test_nothing_is_ever_sold_opened_or_canceled_across_every_case(capsys) -> None:
    """Test 8: one account with every case at once (uncovered, covered by ours in part, covered in full
    by a foreign sell, covered in part by a foreign order, a buy of the owner's working): every request
    the client saw is a read or a GTC trailing-stop sell, and the only submits are the two stops."""
    buy = _order("CCC", "3", str(uuid4()), side="buy")
    paper = Paper(
        positions=[
            _position("AAA", "10"),
            _position("BBB", "5"),
            _position("CCC", "7"),
            _position("DDD", "8"),
        ],
        orders=[
            _order("BBB", "2", OUR_SELL),
            _order("CCC", "7", FOREIGN_SELL),
            buy,
            _order("DDD", "1", str(uuid4())),
        ],
    )
    data = Data({s: AAA_BOOK for s in ("AAA", "BBB", "CCC", "DDD")})
    code, _ = _run(paper, data, capsys)
    _no_sale_open_or_cancel(paper)
    assert sorted((r.symbol, r.qty) for r in paper.submitted) == [("AAA", 10), ("BBB", 3)]
    assert code == 2  # DDD is left uncovered by name


def _task() -> dict[str, str]:
    return tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:protect"]


def _run_task(tmp_path: Path, account: str) -> tuple[subprocess.CompletedProcess, str]:
    """The task's body with `/usr/bin/security` swapped for a stand-in that logs its arguments; it is
    run only for names refused before the keychain and the lock."""
    log = tmp_path / "security.log"
    stub = tmp_path / "security"
    stub.write_text(f'#!/bin/bash\necho "$*" >> {log}\necho "made-up-value-$5"\n')
    stub.chmod(0o755)
    body = _task()["run"].replace("/usr/bin/security", str(stub))
    done = subprocess.run(
        ["bash", "-c", body],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "usage_account": account},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, log.read_text() if log.exists() else ""


def test_the_task_reads_account_as_pair_under_its_lock_without_waiting() -> None:
    task = _task()
    run = task["run"]
    assert 'choices "tests" "windows" "protect"' in task["usage"]
    assert "tests) svc=canon-alpaca-paper; lock=canon-paper-test.lock ;;" in run
    # account C: its own keychain service and its own lock, never A's or B's
    assert "protect) svc=canon-alpaca-paper-protect; lock=canon-paper-protect.lock ;;" in run
    assert 'flag "--plan-only"' in task["usage"]
    assert re.search(r'flag "--row <row>"[^\n]*\{\s*choices "F2" "OWN1" "OWN3"\s*\}', task["usage"])
    assert re.search(r'^exec 9>>"\$\{tmp\}\$\{lock\}"$', run, re.MULTILINE)
    assert re.search(
        r"^/usr/bin/lockf -t 0 9 2>/dev/null; held=\$\?$", run, re.MULTILINE
    )  # no wait
    # held (lockf's 75, as `lock_wait` reads it) is a refusal, exit 3; any other failure is exit 1
    assert re.search(
        r"^  75\) echo \"paper:protect: refused: [^\n]*lock is held[^\n]*exit 3 ;;$",
        run,
        re.MULTILINE,
    )
    assert re.search(
        r"^  \*\) echo [^\n]*could not be taken \(lockf exit \$held\)[^\n]*exit 1 ;;$",
        run,
        re.MULTILINE,
    )
    assert "lock_wait" not in run and not re.search(r"/usr/bin/lockf -t [^0 ]", run)
    assert "from canon_trading.protect import task_from_flags" in run
    assert "CANON_ALPACA_DATA_ONLY" not in run
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$ALPACA_", run)


@pytest.mark.parametrize(
    ("account", "said"),
    [
        ("windows", "account windows (B) is refused by name for now"),
        ("live", "no paper account named 'live'"),
    ],
)
def test_the_task_refuses_windows_and_any_other_name_before_the_keychain(
    tmp_path, account, said
) -> None:
    done, asked = _run_task(tmp_path, account)
    assert done.returncode == 3 and asked == ""
    assert said in done.stderr and "nothing was read" in done.stderr


# -- The cancel of protect stops, by name (`paper:protect <account> --cancel`) ------------------------


def test_a_keyboard_interrupt_during_the_send_names_the_stop_that_may_be_at_alpaca(capsys) -> None:
    """Ctrl-C while the submit waits for its answer: the stop may rest at Alpaca, so its line is
    printed before the interrupt ends the run (the second reader's drop-in)."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    original = paper.submit_order

    def interrupted(order_data):
        original(order_data)
        raise KeyboardInterrupt

    paper.submit_order = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        protect.protect_task(cast(Any, paper), Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), row="OWN1")
    out = capsys.readouterr().out.splitlines()
    assert len(paper.submitted) == 1
    (line,) = [x for x in out if x.startswith("failed AAA:")]
    assert "stopped during the send" in line and "may yet show it working" in line


def test_a_submit_answered_with_what_is_no_order_is_a_failure_and_not_a_stop(capsys) -> None:
    """The second reader's drop-in: an answer alpaca-py did not read into its `Order` shows no stop."""
    paper = Paper(positions=[_position("AAA", "10")])

    def raw_answer(order_data):
        paper.submitted.append(order_data)
        return {"id": "raw-id", "status": "rejected"}

    paper.submit_order = raw_answer  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert len(paper.submitted) == 1
    assert code == 1, lines
    assert not [x for x in lines if x.startswith("sent AAA:")], lines
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "not an Order" in line and "may yet show it working" in line


@pytest.mark.parametrize(
    "status", ["suspended", "stopped", "done_for_day", "replaced", "calculated"]
)
def test_a_submit_answered_with_a_status_no_resting_stop_has_is_a_failure(capsys, status) -> None:
    """A stop counts as sent only when the answer shows it resting. One Alpaca answers as suspended,
    say, is open and is no protection: named, never sent again, and the run fails."""
    paper = Paper(positions=[_position("AAA", "10")])
    original = paper.submit_order
    paper.submit_order = lambda order_data: original(order_data).model_copy(  # type: ignore[method-assign]
        update={"status": status}
    )
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 1 and len(paper.submitted) == 1, lines
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert f"answered {status}" in line and "paper:account" in line
    assert not [x for x in lines if x.startswith("sent AAA:")]


def test_the_quote_age_a_line_prints_counts_the_reads_since_the_quote(capsys, monkeypatch) -> None:
    """The second reader's drop-in: the clock reads 15:00:00, the quote takes five seconds to come
    and is stamped 15:00:04, and the send step's read takes twenty more: the quote is 21 s old at
    the send, and the line says so, not the age against the clock's first read."""
    passed = [0.0]
    monkeypatch.setattr(protect, "monotonic", lambda: passed[0])
    paper = Paper(positions=[_position("AAA", "10")])
    data = Data({"AAA": AAA_BOOK}, quoted="2026-11-12T15:00:04Z")
    original_quote = data.get_stock_latest_quote
    original_pos = paper.get_open_position

    def slow_quote(request):
        passed[0] += 5.0
        return original_quote(request)

    def slow_pos(symbol):
        passed[0] += 20.0
        return original_pos(symbol)

    data.get_stock_latest_quote = slow_quote  # type: ignore[method-assign]
    paper.get_open_position = slow_pos  # type: ignore[method-assign]
    code, lines = _run(paper, data, capsys)
    assert code == 0, lines
    (line,) = [x for x in lines if x.startswith("sent AAA:")]
    assert "quote 21 s old" in line, line


def test_the_tasks_file_names_the_protect_task_among_those_that_reach_account_c() -> None:
    header = "\n".join((ROOT / "mise.toml").read_text().splitlines()[:52])
    assert "no task trades on it yet" not in header
    assert "paper:protect" in header and "canon-paper-protect.lock" in header


def test_the_paper_tests_cleanup_names_a_short_listed_with_a_positive_count(monkeypatch) -> None:
    """The second reader's drop-in: alpaca-py's `Position` carries the side apart from the count, so a
    short may list a positive quantity. The cleanup names it and never sells against it."""
    paper_test = _paper_test()
    monkeypatch.setattr(paper_test, "SYMBOL", "SPY")
    monkeypatch.setattr(paper_test.time, "sleep", lambda _: None)
    paper = Paper(positions=[_position("SPY", "1", "short")], no_shorting=True)
    problems: list[str] = []
    paper_test._clean(paper, problems)
    assert paper.submitted == []
    assert any("SHORT" in problem for problem in problems), problems


@pytest.mark.parametrize("ends", ["accepted", "filled"])
def test_the_paper_tests_cleanup_reads_each_order_it_sent_by_its_own_id(monkeypatch, ends) -> None:
    """An interrupt during the test's buy leaves the order's ID only in the list kept before the
    submit. The cleanup reads each such ID itself, since the open listing and the positions can lag
    by a second: a buy still working is canceled, and one that filled with no position shown yet is
    a problem by name, never a silent end (the second reader's P1)."""
    paper_test = _paper_test()
    monkeypatch.setattr(paper_test, "SYMBOL", "SPY")
    monkeypatch.setattr(paper_test.time, "sleep", lambda _: None)
    paper = Cancelable(positions=[], orders=[], no_shorting=True)
    sent_id = f"{paper_test.PREFIX}0123456789abcdef0123-buy"
    buy = _order("SPY", "1", sent_id, side="buy", type="market", order_type="market").model_copy(
        update={"status": ends, "filled_qty": "1" if ends == "filled" else "0", "limit_price": None}
    )
    paper.found[sent_id] = buy
    paper.unlisted = [buy]
    monkeypatch.setattr(paper_test, "SENT", [sent_id])
    problems: list[str] = []
    paper_test._clean(paper, problems)
    if ends == "accepted":
        assert paper.canceled == [str(buy.id)], (paper.calls, problems)
    else:
        assert any("filled" in p and "no position" in p for p in problems), problems


PROTECT_STOP = "canon-protect-20261112T150000Z-1-6168c4fe"
OTHER_STOP = "canon-protect-20261112T150000Z-2-0a1b2c3d"


class Cancelable(Paper):
    """`Paper` with the two calls a cancel makes: the cancel by the order's ID and the order's own read.
    A canceled order still shows in the open listing (Alpaca's listing lags a cancel by seconds), and
    reads as canceled by its own ID after `lag` reads."""

    def __init__(self, *, lag: int = 0, **kw: Any) -> None:
        super().__init__(**kw)
        self.lag = lag
        self.canceled: list[str] = []
        self.cancel_fails: BaseException | None = None
        self.reads: dict[str, int] = {}
        self.ends_as = "canceled"
        self.filled: str | None = "0"
        self.lands = True
        """Whether a cancel that raises was taken by Alpaca all the same."""
        self.unlisted: list[Order] = []
        """Orders Alpaca holds that its open listing does not show yet."""
        self.read_fails: BaseException | None = None

    def cancel_order_by_id(self, order_id):
        self.calls.append(("cancel_order_by_id", str(order_id)))
        if self.cancel_fails is not None:
            raise self.cancel_fails
        self.canceled.append(str(order_id))

    def get_order_by_id(self, order_id):
        self.calls.append(("get_order_by_id", str(order_id)))
        if self.read_fails is not None:
            raise self.read_fails
        order = next(o for o in (*self.orders, *self.unlisted) if str(o.id) == str(order_id))
        seen = self.reads[str(order_id)] = self.reads.get(str(order_id), 0) + 1
        gone = str(order_id) in self.canceled or (self.cancel_fails is not None and self.lands)
        if gone and seen > self.lag:
            return order.model_copy(update={"status": self.ends_as, "filled_qty": self.filled})
        return order


def _stop(symbol: str, qty: str, client_order_id: str) -> Order:
    return _order(
        symbol,
        qty,
        client_order_id,
        type="trailing_stop",
        order_type="trailing_stop",
        limit_price=None,
        time_in_force="gtc",
    )


def _cancel(paper: Paper, capsys, **kw: Any) -> tuple[int, list[str]]:
    code = protect.cancel_task(cast(Any, paper), pause=lambda _seconds: None, **kw)
    out = capsys.readouterr()
    return code, (out.out + out.err).splitlines()


def test_the_cancel_takes_every_open_protect_stop_and_nothing_else(capsys) -> None:
    """One cancel for each open order of the protect lineage, read by its own ID until it has ended;
    an order of our nodes, a foreign one and one with no ID are named and left; nothing is sent."""
    ours, foreign = _order("CCC", "3", OUR_SELL, side="sell"), _order("DDD", "2", FOREIGN_SELL)
    unnamed = _order("EEE", "1", "unused").model_copy(update={"client_order_id": None})
    stops = [_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)]
    paper = Cancelable(orders=[*stops, ours, foreign, unnamed], lag=2)
    code, lines = _cancel(paper, capsys)
    assert code == 0, lines
    assert paper.canceled == [str(o.id) for o in stops] and paper.submitted == []
    assert {name for name, _ in paper.calls} == {
        "get_orders",
        "cancel_order_by_id",
        "get_order_by_id",
    }
    for symbol in ("AAA", "BBB"):
        (line,) = [x for x in lines if x.startswith(f"canceled {symbol}:")]
        assert "a protect stop of ours" in line and "canceled" in line
        assert "never cancels" not in line
    assert len([x for x in lines if x.startswith("left alone")]) == 3
    assert not any(
        str(o.id) in " ".join(lines) for o in (*stops, ours, foreign)
    )  # no venue order ID


def test_the_cancel_takes_one_symbols_stops_when_a_symbol_is_named(capsys) -> None:
    stops = [_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)]
    paper = Cancelable(orders=stops)
    code, lines = _cancel(paper, capsys, symbol="BBB")
    assert code == 0 and paper.canceled == [str(stops[1].id)]
    assert [x for x in lines if x.startswith("left alone AAA:")]


def test_plan_only_names_what_the_cancel_would_take_and_cancels_nothing(capsys) -> None:
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    code, lines = _cancel(paper, capsys, plan_only=True)
    assert code == 0 and paper.canceled == []
    assert "cancel_order_by_id" not in {name for name, _ in paper.calls}
    assert [x for x in lines if x.startswith("would cancel AAA:")]
    assert lines[-1] == "plan only: nothing was canceled"


def test_no_protect_stop_open_is_said_and_is_no_failure(capsys) -> None:
    paper = Cancelable(orders=[_order("DDD", "2", FOREIGN_SELL)])
    code, lines = _cancel(paper, capsys)
    assert code == 0 and paper.canceled == []
    assert "the open listing showed no protect stop (2 reads)" in lines
    assert len([c for c in paper.calls if c[0] == "get_orders"]) == 2  # one listing is no "none"


@pytest.mark.parametrize(
    ("change", "said", "code"),
    [
        (lambda p: setattr(p, "lag", 10_000), "still new after", 1),
        (lambda p: setattr(p, "ends_as", "filled"), "ended filled", 1),
        (
            lambda p: (
                setattr(p, "cancel_fails", requests.ConnectionError("down")),
                setattr(p, "lands", False),
            ),
            "the cancel",
            1,
        ),
    ],
    ids=["never-ends", "elected-first", "cancel-fails"],
)
def test_a_cancel_that_does_not_end_the_stop_canceled_is_a_failure_by_name(
    capsys, change, said, code
) -> None:
    """A stop still working after the wait, one that elected and filled before the cancel reached it,
    and a cancel that could not be sent are each named; none is reported as canceled."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    change(paper)
    got, lines = _cancel(paper, capsys)
    assert got == code
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert said in line and not [x for x in lines if x.startswith("canceled AAA:")]
    assert (
        len([c for c in paper.calls if c[0] == "cancel_order_by_id"]) == 1
    )  # one attempt, never again


def test_a_stop_canceled_after_part_of_it_filled_is_a_failure_that_names_the_shares(capsys) -> None:
    """A stop can elect and sell part of its shares before the cancel lands: Alpaca then shows it
    canceled with a fill. That is no clean cancel; the shares sold are named and the run fails."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    paper.filled = "4"
    code, lines = _cancel(paper, capsys)
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "4 of its 10 shares filled" in line and "before the cancel" in line
    assert not [x for x in lines if x.startswith("canceled AAA:")]


@pytest.mark.parametrize("status", [404, 422])
def test_a_cancel_alpaca_refuses_lets_the_stops_own_read_decide(capsys, status) -> None:
    """A stop already canceled, as when a second run follows the first inside the listing's lag,
    answers the cancel with a refusal (422, "The order status is not cancelable"): the order's own
    read decides, and one that shows it canceled unfilled is a cancel, not a failure. One the read
    shows still working is a failure that names the answer."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    paper.cancel_fails = APIError('{"code": 42210000, "message": "stand-in"}', _status(status))
    code, lines = _cancel(paper, capsys)
    assert code == 0, lines
    (line,) = [x for x in lines if x.startswith("canceled AAA:")]
    assert f"the cancel was answered {status}" in line
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)], lag=10_000)
    paper.cancel_fails = APIError('{"code": 42210000, "message": "stand-in"}', _status(status))
    code, lines = _cancel(paper, capsys)
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert f"answered {status}" in line and "still new" in line


@pytest.mark.parametrize("stopped", [False, True], ids=["read-fails", "stop-signal"])
def test_a_run_that_ends_inside_a_stops_cancel_names_that_stop_and_those_not_reached(
    capsys, stopped
) -> None:
    """A read that fails after the cancel went out, or the task's stop signal there, ends the run:
    the stop whose cancel is at Alpaca is named with its symbol, and so is each protect stop the
    run did not reach."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)])
    paper.read_fails = protect.Stopped() if stopped else requests.ConnectionError("down")
    code, lines = _cancel(paper, capsys)
    assert code == 1 and len(paper.canceled) == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "its cancel was sent" in line and "may yet show" in line
    assert ("the task was stopped" in line) is stopped  # a signal during the read is still a stop
    (rest,) = [x for x in lines if x.startswith("not reached BBB:")]
    assert "a protect stop" in rest


def test_a_stop_signal_during_a_cancel_names_the_stop_whose_cancel_may_be_at_alpaca(capsys) -> None:
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)])
    paper.cancel_fails = protect.Stopped()
    code, lines = _cancel(paper, capsys)
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "its cancel may be at Alpaca" in line and "may yet show" in line
    assert [x for x in lines if x.startswith("not reached BBB:")]
    assert len([c for c in paper.calls if c[0] == "cancel_order_by_id"]) == 1


def test_a_cancel_whose_answer_was_lost_is_decided_by_the_stops_own_read(capsys) -> None:
    """A read timeout on the cancel says nothing of whether Alpaca took it: the stop's own read
    decides, as a lost answer to a send is looked up, and one that shows it canceled is a cancel."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)], lag=3)
    paper.cancel_fails = requests.ReadTimeout("no answer")
    code, lines = _cancel(paper, capsys)
    assert code == 0, lines
    (line,) = [x for x in lines if x.startswith("canceled AAA:")]
    assert "the cancel's answer was lost (ReadTimeout)" in line


def test_a_refused_cancel_of_a_stop_about_to_end_is_read_until_it_ends(capsys) -> None:
    """A stop a broken-off run left pending its cancel refuses a second cancel and ends canceled a
    read or two later: it is read as long as an accepted cancel's stop, not once."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)], lag=3)
    paper.cancel_fails = APIError('{"code": 42210000, "message": "stand-in"}', _status(422))
    code, lines = _cancel(paper, capsys)
    assert code == 0, lines
    assert [x for x in lines if x.startswith("canceled AAA:")]


def test_a_failure_of_another_kind_inside_a_cancel_still_names_its_stop(capsys) -> None:
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)])
    paper.cancel_fails = RuntimeError("not one of alpaca-py's")
    code, lines = _cancel(paper, capsys)
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "its cancel may be at Alpaca" in line and "RuntimeError" in line
    assert [x for x in lines if x.startswith("not reached BBB:")]


@pytest.mark.parametrize("where", ["cancel", "read"])
def test_a_keyboard_interrupt_inside_a_cancel_names_the_stop_first(capsys, where) -> None:
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP), _stop("BBB", "5", OTHER_STOP)])
    if where == "cancel":
        paper.cancel_fails = KeyboardInterrupt()
    else:
        paper.read_fails = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _cancel(paper, capsys)
    out = capsys.readouterr()
    said = (out.out + out.err).splitlines()
    (line,) = [x for x in said if x.startswith("failed AAA:")]
    assert "may yet show working" in line
    assert [x for x in said if x.startswith("not reached BBB:")]


@pytest.mark.parametrize("plan_only", [False, True])
def test_a_named_symbol_with_no_protect_stop_is_said_and_is_not_a_success(
    capsys, plan_only
) -> None:
    """A symbol typed wrong must not read as done: the line names it and the exit is 2."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    code, lines = _cancel(paper, capsys, symbol="ZZZ", plan_only=plan_only)
    assert code == 2 and paper.canceled == []
    assert "the open listing showed no protect stop in ZZZ (2 reads)" in lines


def test_a_foreign_trailing_stop_sell_is_named_as_a_possible_replacement(capsys) -> None:
    """A protect stop replaced outside this task carries an ID Alpaca or its maker chose and reads
    as foreign: it is left alone, and its line says it may be such a replacement, which still
    holds its shares. A foreign order of another kind gets no such line."""
    replaced, other = _stop("AAA", "10", FOREIGN_SELL), _order("DDD", "2", str(uuid4()))
    paper = Cancelable(orders=[replaced, other])
    _code, lines = _cancel(paper, capsys)
    assert paper.canceled == []
    (line,) = [x for x in lines if x.startswith("left alone AAA:")]
    assert "may be a protect stop replaced outside this task" in line
    (line,) = [x for x in lines if x.startswith("left alone DDD:")]
    assert "replaced" not in line


def test_a_canceled_stop_whose_fill_count_is_not_shown_is_no_clean_cancel(capsys) -> None:
    """What Alpaca does not show is not zero: a canceled stop with no filled quantity is a failure."""
    paper = Cancelable(orders=[_stop("AAA", "10", PROTECT_STOP)])
    paper.filled = None
    code, lines = _cancel(paper, capsys)
    assert code == 1
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert "no count of what filled" in line


def test_a_full_page_of_open_orders_refuses_the_cancel(capsys) -> None:
    paper = Cancelable(
        orders=[_stop("AAA", "1", PROTECT_STOP)] * canon_alpaca.execution.ORDERS_PAGE_LIMIT
    )
    code, lines = _cancel(paper, capsys)
    assert code == 3 and paper.canceled == []
    assert "a protect stop may be missing from it" in " ".join(lines)


@pytest.mark.parametrize(
    ("flags", "code", "canceled", "sent"),
    [
        (("false", "", "true", ""), 0, 1, 0),
        (("true", "", "true", ""), 0, 0, 0),
        (("false", "F2", "true", ""), 3, 0, 0),
        (("false", "", "maybe", ""), 3, 0, 0),
        (("false", "", "true", "aaa"), 3, 0, 0),
        (("false", "F2", "false", "AAA"), 3, 0, 0),
    ],
    ids=[
        "cancel",
        "cancel-plan-only",
        "cancel-with-a-row",
        "not-a-switch",
        "bad-symbol",
        "symbol-on-a-send",
    ],
)
def test_the_tasks_flags_cancel_only_on_an_exact_true_and_never_with_a_row(
    capsys, flags, code, canceled, sent
) -> None:
    paper = Cancelable(
        positions=[_position("AAA", "10")], orders=[_stop("AAA", "10", PROTECT_STOP)]
    )
    got = protect.task_from_flags(
        *flags, trading=cast(Any, paper), data=Data({"AAA": AAA_BOOK}), pause=lambda _s: None
    )
    assert got == code and len(paper.canceled) == canceled and len(paper.submitted) == sent
    if code == 3:
        assert paper.calls == []


def test_the_task_hands_the_cancel_and_the_symbol_over() -> None:
    task = _task()
    assert 'flag "--cancel"' in task["usage"] and 'flag "--symbol <symbol>"' in task["usage"]
    assert re.search(
        r"task_from_flags\(\*sys\.argv\[1:5\]\)\)' "
        r'"\$\{usage_plan_only:-false\}" "\$\{usage_row:-\}" "\$\{usage_cancel:-false\}" '
        r'"\$\{usage_symbol:-\}"$',
        task["run"],
        re.MULTILINE,
    )


# The cancel unit's outside reader's three cases (its drop-ins, as written).


@pytest.mark.parametrize(
    "interrupted_type", [protect.Stopped, KeyboardInterrupt], ids=["sigterm", "ctrl-c"]
)
def test_a_lost_submit_answer_interrupted_during_lookup_names_the_stop(
    capsys, monkeypatch, interrupted_type
) -> None:
    paper = Paper(positions=[_position("AAA", "10")])
    original_submit = paper.submit_order
    resting: list[Order] = []

    def accepted_but_answer_lost(order_data):
        order = original_submit(order_data)
        resting.append(order)
        paper.found[order_data.client_order_id] = order
        raise requests.ReadTimeout("the accepted submit's answer was lost")

    def interrupted_lookup(client_order_id):
        paper.calls.append(("get_order_by_client_id", client_order_id))
        assert client_order_id == resting[0].client_order_id
        raise interrupted_type()

    monkeypatch.setattr(paper, "submit_order", accepted_but_answer_lost)
    monkeypatch.setattr(paper, "get_order_by_client_id", interrupted_lookup)
    if interrupted_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            protect.protect_task(cast(Any, paper), Data({"AAA": AAA_BOOK}), row="OWN1")
    else:
        assert protect.protect_task(cast(Any, paper), Data({"AAA": AAA_BOOK}), row="OWN1") == 1

    out = capsys.readouterr()
    said = out.out + out.err
    assert len(paper.submitted) == len(resting) == 1
    assert custody._value(resting[0].status) == "new"
    assert resting[0].client_order_id in said, said
    assert "failed AAA:" in said and "may yet show" in said, said


def test_cleanup_still_cancels_a_saved_buy_when_the_open_listing_fails(
    monkeypatch,
) -> None:
    paper_test = _paper_test()
    monkeypatch.setattr(paper_test, "SYMBOL", "SPY")
    monkeypatch.setattr(paper_test.time, "sleep", lambda _seconds: None)
    client_id = f"{paper_test.PREFIX}0123456789abcdef0123-buy"
    buy = _order(
        "SPY",
        "1",
        client_id,
        side="buy",
        type="market",
        order_type="market",
        limit_price=None,
        status="accepted",
        filled_qty="0",
    )
    paper = Cancelable(positions=[], orders=[], no_shorting=True)
    paper.found[client_id] = buy
    paper.unlisted = [buy]
    monkeypatch.setattr(paper_test, "SENT", [client_id])
    original_listing = paper.get_orders
    listings = 0

    def first_listing_fails(filter=None):
        nonlocal listings
        listings += 1
        if listings == 1:
            raise APIError('{"code": 50300000, "message": "temporary outage"}', _status(503))
        return original_listing(filter)

    monkeypatch.setattr(paper, "get_orders", first_listing_fails)
    problems: list[str] = []
    paper_test._clean(paper, problems)

    assert ("get_order_by_client_id", client_id) in paper.calls
    assert paper.canceled == [str(buy.id)], (paper.calls, problems)
    assert paper.submitted == []


@pytest.mark.parametrize("read_fails", [False, True], ids=["still-working", "read-fails"])
def test_an_unresolved_cancel_keeps_the_partial_fill_it_already_read(
    capsys, monkeypatch, read_fails
) -> None:
    stop = _stop("AAA", "10", PROTECT_STOP)
    partial = stop.model_copy(update={"status": "partially_filled", "filled_qty": "4"})
    paper = Cancelable(orders=[stop])
    reads = 0
    monkeypatch.setattr(protect, "CANCEL_READS", 2)

    def read_after_cancel(order_id):
        nonlocal reads
        reads += 1
        assert str(order_id) == str(stop.id)
        if read_fails and reads == 2:
            raise requests.ReadTimeout("the next order read had no answer")
        return partial

    monkeypatch.setattr(paper, "get_order_by_id", read_after_cancel)
    code, lines = _cancel(paper, capsys)
    said = "\n".join(lines)
    assert code == 1 and paper.canceled == [str(stop.id)]
    assert not any(line.startswith("canceled AAA:") for line in lines)
    assert PROTECT_STOP in said
    assert "4 of its 10 shares filled" in said, said


# The cancel unit's second outside reader's case (its drop-in, as written).


class Lagging(Cancelable):
    """Alpaca's open listing has not shown a just-sent stop on its first read (the unit's own note:
    the listing can lag an order by a second), and shows it on the next."""

    def __init__(self, stop: Order) -> None:
        super().__init__(orders=[])
        self.stop = stop
        self.unlisted = [stop]
        self.listings = 0

    def get_orders(self, filter: Any = None):
        self.calls.append(("get_orders", filter))
        self.listings += 1
        return [] if self.listings == 1 else [self.stop]


def test_a_stop_the_listing_has_not_shown_yet_is_not_reported_as_none(capsys) -> None:
    stop = _stop("AAA", "10", PROTECT_STOP)
    paper = Lagging(stop)
    code, lines = _cancel(paper, capsys)
    assert paper.canceled == [str(stop.id)], lines
    assert code == 0 and [x for x in lines if x.startswith("canceled AAA:")], lines
    assert not [x for x in lines if x.startswith("no protect stop open")], lines


# The cancel unit's third outside reader's three cases on the send (its drop-ins, as written but for
# one parameter's name, marked).


@pytest.mark.parametrize("lookup", ["ctrl-c", "sigterm", "raw-dict", "submit-bug"])
def test_a_broken_submit_or_lookup_after_a_send_still_names_the_stop_and_its_id(
    capsys, lookup
) -> None:
    """Once `submit_order` is entered, an unexpected exception in `submit_order` or a
    `KeyboardInterrupt`, `Stopped`, or `custody.ReadFailed` (non-`Order` answer) during `_look_up`
    must still print `failed AAA:` with the stop's `client_order_id` and resting warning."""
    paper = Paper(positions=[_position("AAA", "10"), _position("BBB", "5")])
    if lookup == "submit-bug":
        paper.submit_fails = RuntimeError("unexpected transport failure after send")
    else:
        paper.submit_fails = requests.ReadTimeout("read timed out")

    def broken_lookup(client_id):  # the reader wrote `_client_id`; renamed for the type check
        if lookup == "ctrl-c":
            raise KeyboardInterrupt
        if lookup == "sigterm":
            raise protect.Stopped
        return {"id": "raw-id", "status": "new"}

    paper.get_order_by_client_id = broken_lookup  # type: ignore[method-assign]
    if lookup == "ctrl-c":
        with pytest.raises(KeyboardInterrupt):
            protect.protect_task(
                cast(Any, paper), Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), row="OWN1"
            )
        lines = capsys.readouterr().out.splitlines()
    else:
        code, lines = _run(paper, Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), capsys)
        assert code == 1
    sent_id = paper.submitted[0].client_order_id
    (line,) = [x for x in lines if x.startswith("failed AAA:")]
    assert sent_id in line and "may yet show it working" in line


@pytest.mark.parametrize(
    "status", ["suspended", "stopped", "done_for_day", "replaced", "calculated"]
)
def test_a_non_resting_open_sell_is_never_counted_as_working_cover(capsys, status) -> None:
    """`RESTING` excludes open statuses that do not protect a long (`protect.py:199-203`), so
    `_uncovered` must not count them as working cover: neither leaving a 10-share long 'left alone'
    with exit 0 nor sending a 6-share stop that leaves 4 shares unprotected."""
    full = Paper(
        positions=[_position("AAA", "10")],
        orders=[_stop("AAA", "10", PROTECT_STOP).model_copy(update={"status": status})],
    )
    code, lines = _run(full, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 2 and full.submitted == [], lines
    assert [x for x in lines if x.startswith("unprotected AAA:")], lines
    assert not [x for x in lines if x.startswith("left alone AAA:")], lines

    partial = Paper(
        positions=[_position("AAA", "10", available="10")],
        orders=[_stop("AAA", "4", PROTECT_STOP).model_copy(update={"status": status})],
    )
    code, lines = _run(partial, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 2 and partial.submitted == [], (code, partial.submitted, lines)


def test_an_open_sell_whose_fill_count_is_not_shown_is_not_counted_as_unfilled_cover(
    capsys,
) -> None:
    """`Order.filled_qty` is optional (`alpaca/trading/models.py:283`), and what is not shown is
    not zero (`custody._shown`). On a 12-share long where a 10-share sell already filled 4 shares
    (`available='6'`) and lists `filled_qty=None`, `_remaining` must not assume 0 filled and send a
    2-share stop that leaves 4 shares unprotected."""
    paper = Paper(
        positions=[_position("AAA", "12", available="6")],
        orders=[
            _order("AAA", "10", OUR_SELL, status="partially_filled").model_copy(
                update={"filled_qty": None}
            )
        ],
    )
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 2 and paper.submitted == [], (code, paper.submitted, lines)
    assert [x for x in lines if x.startswith("unprotected AAA:")], lines


def test_the_paper_tests_checks_of_what_is_working_leave_out_a_listed_order_that_has_ended(
    monkeypatch, capsys
) -> None:
    """Alpaca's open-orders listing can still show an order a second after it filled (seen on paper,
    2026-11-13, with the paper test's own buy): the test's checks of what is working go by each
    order's own read, and the listed status of such an order is printed."""
    paper_test = _paper_test()
    buy = _order("SPY", "1", f"{paper_test.PREFIX}0123456789abcdef0123-buy", side="buy")
    stop = _stop("SPY", "1", PROTECT_STOP)
    paper = Cancelable(orders=[buy, stop])
    filled = buy.model_copy(update={"status": "filled", "filled_qty": "1"})
    monkeypatch.setattr(
        paper, "get_order_by_id", lambda order_id: filled if str(order_id) == str(buy.id) else stop
    )
    working = paper_test._working(paper)
    assert [str(o.id) for o in working] == [str(stop.id)]
    assert "listed as open though ended: listed new, its own read filled" in capsys.readouterr().out


# ---------- The case set: no position without its exit ----------
# The lead's rule of 2026-11-13 (order D): in any session, on any account, a position has its exit. Each
# case below is one of the day's shapes of a long that `paper:protect` finds, and asserts that the long
# ends the run with a working sell that covers it. The marker gathers the set across files
# (`pytest -m no_position_without_its_exit`); a case of a gap not yet closed is xfail strict, so the
# set turns red when its fix lands and the mark is not lifted. A review that finds a new way a position
# had no exit adds its case here in the same pull request. Inputs are quotes, bars and steps only.

EXIT = pytest.mark.no_position_without_its_exit


def _covered(paper: Paper, symbol: str, qty: int, code: int, lines: list[str]) -> None:
    """The long `symbol` ends the run covered: one GTC trailing stop that sells `qty`, sent, and the run
    exit 0, never an `unprotected` line for it."""
    assert code == 0, lines
    assert [(r.symbol, r.qty) for r in paper.submitted] == [(symbol, qty)]
    assert not [x for x in lines if x.startswith(f"unprotected {symbol}:")]
    _no_sale_open_or_cancel(paper)


@EXIT
def test_exit_for_a_quiet_large_name_whose_atr_is_a_fraction_of_a_percent(capsys) -> None:
    """A quiet, high-priced long: ATR 0.05 on a 500 name, a one-cent spread. The ATR distance is under
    Alpaca's smallest trail, so the stop is raised to it, not refused."""
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": ("499.99", "500.00", "500.00", "0.05")}), capsys)
    _covered(paper, "AAA", 10, code, lines)
    (request,) = paper.submitted
    assert D(str(request.trail_price)) >= protect.VENUE_SMALLEST * D("500.00")


@EXIT
def test_exit_for_a_sub_dollar_name_whose_spread_floor_is_over_the_entry_cap(capsys) -> None:
    """A sub-dollar long whose two-spread floor (0.04 on 0.97, about 4%) is over the 0.02 entry cap but
    under the 0.10 ceiling: a found holding keeps that distance (ADR 0142's amendment)."""
    paper = Paper(positions=[_position("AAA", "100")])
    code, lines = _run(paper, Data({"AAA": ("0.95", "0.97", "0.96", "0.01")}), capsys)
    _covered(paper, "AAA", 100, code, lines)


@EXIT
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="gap: a found holding whose distance is over the 0.10 ceiling is refused by name and left "
    "with no exit (ADR 0142); its cover is the lead's ruling and a separate unit",
)
def test_exit_for_a_sub_dollar_name_whose_spread_floor_is_over_the_ceiling(capsys) -> None:
    """A sub-dollar long with a six-cent spread: the two-spread floor (0.12 on 0.97) is over the 0.10
    ceiling. Today the run names it unprotected and sends nothing, so it has no exit."""
    paper = Paper(positions=[_position("AAA", "100")])
    code, lines = _run(paper, Data({"AAA": ("0.91", "0.97", "0.94", "0.01")}), capsys)
    _covered(paper, "AAA", 100, code, lines)


@EXIT
def test_exit_for_a_found_long_whose_atr_is_over_the_entry_cap(capsys) -> None:
    """ATR 0.25 on a 10 name plans 0.025 of the entry under OWN1, over the 0.02 entry cap: a found
    holding keeps it, and is not refused for a cap that sizes entries."""
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": OWN_BOOK}), capsys)
    _covered(paper, "AAA", 10, code, lines)


@EXIT
def test_exit_for_a_long_already_in_gain_when_first_seen(capsys) -> None:
    """A long bought at 5.00 and first seen at 10.00: the stop trails from the market now, and its
    distance is not taken from the entry price."""
    held = _position("AAA", "10").model_copy(
        update={"avg_entry_price": "5.00", "cost_basis": "50.00", "unrealized_pl": "50.00"}
    )
    paper = Paper(positions=[held])
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    _covered(paper, "AAA", 10, code, lines)
    expected, _ = _planned(AAA_BOOK, row="OWN1", size_from_budget=True)
    assert D(str(paper.submitted[0].trail_price)) == expected


@EXIT
def test_exit_for_the_shares_the_owner_added_to_a_protected_long(capsys) -> None:
    """A protect stop covers the 10 shares it was sent for; the owner then buys 5 more. The next run
    covers the 5 the stop does not, and leaves the stop as it is."""
    paper = Paper(
        positions=[_position("AAA", "15", available="5")],
        orders=[_stop("AAA", "10", PROTECT_STOP)],
    )
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    _covered(paper, "AAA", 5, code, lines)


def test_the_close_margin_is_one_sends_bound_not_the_tasks_timeout() -> None:
    """The run checks the time to the close again before each send, so the margin it needs is what one
    send may take: the paper client's own (connect, read) bound on the submit, retries off. The 300 s it
    held before was said to be the task's timeout, which is 10 minutes, so it held for no reason that
    stood (the lead's case of 2026-11-13: a long bought in the last minutes before the close got no stop)."""
    from canon_alpaca import providers

    one_send = providers.CONNECT_TIMEOUT_SECS + providers.READ_TIMEOUT_SECS
    assert protect.SEND_MARGIN.total_seconds() >= one_send
    assert protect.SEND_MARGIN.total_seconds() < one_send + 1
    assert protect.CLOSE_MARGIN == protect.SEND_MARGIN


def test_a_long_bought_a_minute_before_the_close_gets_its_stop(capsys) -> None:
    """A minute before the close is inside the old 300 s refusal: the stop now goes out."""
    paper = Paper(positions=[_position("AAA", "10")], next_close="2026-11-12T15:01:00Z")
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 0, lines
    assert [r.symbol for r in paper.submitted] == ["AAA"]
    _no_sale_open_or_cancel(paper)


def test_under_one_sends_bound_to_the_close_the_run_reads_nothing_else(capsys) -> None:
    margin = int(protect.SEND_MARGIN.total_seconds())
    close = NOW + timedelta(seconds=margin - 1)
    paper = Paper(positions=[_position("AAA", "10")], next_close=close.isoformat())
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 3 and paper.calls == [("get_clock", None)]
    assert f"{margin - 1} s to the close" in "\n".join(lines)
    at_margin = Paper(
        positions=[_position("AAA", "10")],
        next_close=(NOW + timedelta(seconds=margin)).isoformat(),
    )
    assert (
        _run(at_margin, Data({"AAA": AAA_BOOK}), capsys)[0] != 3
    )  # it starts; its send is checked
    on_time = Paper(
        positions=[_position("AAA", "10")],
        next_close=(NOW + timedelta(seconds=margin + 2)).isoformat(),
    )
    assert _run(on_time, Data({"AAA": AAA_BOOK}), capsys)[0] == 0


def test_a_send_the_close_would_overtake_is_refused_by_name_and_not_sent(
    capsys, monkeypatch
) -> None:
    """The time to the close is checked again just before each send: a symbol reached with less than
    one send's bound left is named unprotected and nothing is sent for it, the earlier one's stop kept."""
    passed = [0.0]
    monkeypatch.setattr(protect, "monotonic", lambda: passed[0])
    paper = Paper(
        positions=[_position("AAA", "10"), _position("BBB", "5")],
        next_close="2026-11-12T15:01:00Z",
    )
    original = paper.submit_order

    def slow(order_data):
        passed[0] += 40.0  # the first send took 40 s, leaving 20 s to the close
        return original(order_data)

    paper.submit_order = slow  # type: ignore[method-assign]
    code, lines = _run(paper, Data({"AAA": AAA_BOOK, "BBB": BBB_BOOK}), capsys)
    assert code == 2 and [r.symbol for r in paper.submitted] == ["AAA"]
    (line,) = [x for x in lines if x.startswith("unprotected BBB:")]
    assert "20 s to the close" in line
