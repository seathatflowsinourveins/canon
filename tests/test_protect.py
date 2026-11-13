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


def _planned(book: tuple[str, str, str, str]) -> tuple[Decimal, mover.TrailPlan]:
    """The distance the Mover's own planning gives for `book`, computed here apart from the task: the
    Mover's `_entry_trail` on an ATR warmed with the same bars, then `sizing.sized_stop`."""
    bid, ask, mid, width = book
    planner = mover.Mover(mover.MoverConfig(max_notional=D(1), max_move=D(1), trail_mode="atr"))
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


WIDE_BOOK = ("9.99", "10.00", "10.00", "0.08")
"""ATR 0.08 on a $10 name: three of them are 2.4% of the price, over the 2% cap."""


def test_a_row_whose_distance_is_over_the_cap_sends_no_stop_and_says_so(capsys) -> None:
    """The ATR rows are registered with the cap arm `own`, which the Mover lets stand only for an entry
    it sizes; nothing is sized here, so its planning refuses a distance over the cap by name, and the
    long gets no stop under that row. Plan-only shows it beside the rows that do give one."""
    paper = Paper(positions=[_position("AAA", "10")])
    code, lines = _run(paper, Data({"AAA": WIDE_BOOK}), capsys, row="OWN3")
    assert code == 2 and paper.submitted == []
    (line,) = [x for x in lines if x.startswith("unprotected AAA:")]
    assert "under OWN3" in line and "over the 0.02 cap" in line
    paper = Paper(positions=[_position("AAA", "10")])
    assert protect.protect_task(cast(Any, paper), Data({"AAA": WIDE_BOOK}), plan_only=True) == 0
    out = capsys.readouterr().out.splitlines()
    (refused,) = [x for x in out if x.startswith("no stop for AAA under OWN3:")]
    assert "over the 0.02 cap" in refused
    assert [x for x in out if x.startswith("would send AAA under F2:")]
    assert [x for x in out if x.startswith("would send AAA under OWN1:")]


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
    got = protect.task_from_flags(plan_only, row, cast(Any, paper), Data({"AAA": AAA_BOOK}))
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


def test_too_near_the_close_it_refuses_after_the_clock_read_alone(capsys) -> None:
    """The session is read once, so the run must end inside it: with less than the task's own time
    bound left to the close nothing else is read or sent."""
    paper = Paper(positions=[_position("AAA", "10")], next_close="2026-11-12T15:04:59Z")
    code, lines = _run(paper, Data({"AAA": AAA_BOOK}), capsys)
    assert code == 3 and paper.calls == [("get_clock", None)]
    assert "299 s to the close" in "\n".join(lines)
    on_time = Paper(positions=[_position("AAA", "10")], next_close="2026-11-12T15:05:00Z")
    assert _run(on_time, Data({"AAA": AAA_BOOK}), capsys)[0] == 0


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
        (("9.80", "10.00", "10.00", "0.04"), {}, "floor"),
        (None, {}, "no latest sip quote"),
    ],
    ids=["stale-quote", "crossed", "locked", "no-bid", "no-ask", "floor-over-the-cap", "no-quote"],
)
def test_a_long_with_no_sound_distance_gets_no_stop_and_is_named(
    capsys, book, data_kw, why
) -> None:
    """A stop is never sent at a distance the planning could not stand behind: an old quote, a
    crossed, locked or one-sided one, none at all, or a spread whose floor is over the cap."""
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
    assert re.search(
        r"task_from_flags\(sys\.argv\[1\], sys\.argv\[2\]\)\)' "
        r'"\$\{usage_plan_only:-false\}" "\$\{usage_row:-\}"$',
        run,
        re.MULTILINE,
    )
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
