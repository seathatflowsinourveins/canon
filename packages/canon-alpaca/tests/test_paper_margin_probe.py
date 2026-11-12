"""Paper probe: which buying-power figure Alpaca checks a buy against, for a marginable name whose last
close is under $2.50 and for one well above it.

Alpaca's documentation does not say what an order on a non-marginable asset is checked against: "Orders
at Alpaca", "Buying Power" says only that "The order's calculated value is then compared against your
available buying power" (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, read 2026-11-12). A
post on Alpaca's forum (https://forum.alpaca.markets/t/insufficient-day-trading-buying-power/14691, July
2024) says a stock whose last close is under $2.50 counts as non-marginable, whatever its asset flag. This
test asks the venue; its result is recorded in ADR 0106 as a relation, with no account figure.

The paper test runs only when a paper key pair is in the environment and `CANON_MARGIN_PROBE=1` is set,
so `mise run paper:test` does not place its orders unless asked. Run it as
`CANON_MARGIN_PROBE=1 mise run paper:test -- -k which_buying_power_a_buy` (that selects this one test
of the paper files), which holds account A's lock. It uses REST only and opens no stream (the conftest
refuses a stream to an unmarked test). It runs in the regular session only, and fails by name outside it.

It picks, from Alpaca's active, tradable, marginable assets on NYSE, NASDAQ and AMEX, read in symbol
order, the first name whose `sip` snapshot has a last close and an ask under $2.50, a bid of at least
$1.00 and a spread under 1%, and the first whose last close and bid are at least $5.00, with an ask of at
most $100 and the same spread. The last close is the snapshot's previous daily bar, the last full session
while the regular session is open. Neither name may have an open order or a position on the account. For
each name it:

- reads the account (`GET /v2/account`); the upper figure is `buying_power`, or `regt_buying_power` when
  that is lower, so a refusal is not the Reg T or day-trading check;
- places one DAY limit buy priced 20% under the bid, so it rests, with a quantity whose value at the limit
  is just above `non_marginable_buying_power` and whose value at the ask is under the upper figure: the
  venue accepts it only if it checks against more than the non-marginable figure;
- reads the account again, then, whatever happened, finds the order by its client order ID, cancels it
  if it is still open, waits for it to end, closes any position the name holds, and reads the account a
  third time.

A submit that raises is resolved by looking the order up by its client order ID, since alpaca-py resends
a POST answered 429 or 504 (`alpaca/common/rest.py:204`): an order found is accepted, whatever the submit
raised. Only the status and Alpaca's error code of a refusal are kept, never its body. It prints, per name,
the outcome and each figure's change as a multiple of the order's value at its limit, rounded to two
places: relations, not account figures. It passes when every order was accepted or refused with HTTP 403
and code 40310000 (insufficient buying power), nothing it placed is still open, and it holds no position
in either name. A fill fails the test by name, after the position is closed.

Sources: alpaca-py at the pin c42add7c, `TradingClient.get_all_assets`, `get_account`, `submit_order`,
`get_order_by_client_id`, `cancel_order_by_id`, `get_clock`, `get_orders`, `get_open_position`,
`close_position` (`alpaca/trading/client.py`); `TradeAccount.buying_power`, `regt_buying_power` and
`non_marginable_buying_power` (`alpaca/trading/models.py:616,618,626`);
`StockHistoricalDataClient.get_stock_snapshot` (`alpaca/data/historical/stock.py:205`) and `Snapshot`'s
`latest_quote` and `previous_daily_bar` (`alpaca/data/models/snapshots.py:24,27`); `APIError`'s
`status_code` and `code` (`alpaca/common/exceptions.py:10-29`).
"""

import json
import math
import os
import time
import uuid
from collections.abc import Callable
from decimal import ROUND_DOWN
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import AssetExchange
from alpaca.trading.enums import AssetStatus
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import OrderStatus
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import Asset
from alpaca.trading.models import Order
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import with_http_timeout

PROBE_ENV = "CANON_MARGIN_PROBE"

paper_only = pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and os.environ.get(ENV_SECRET_KEY))
    or os.environ.get(PROBE_ENV) != "1",
    reason=(
        f"the margin probe runs only with Alpaca paper credentials ({ENV_API_KEY}, {ENV_SECRET_KEY}) "
        f"and {PROBE_ENV}=1 in the environment"
    ),
)

EXCHANGES = {AssetExchange.NYSE, AssetExchange.NASDAQ, AssetExchange.AMEX}
THRESHOLD = Decimal("2.50")
"""The forum post's line on the last close (the module docstring); UNCALIBRATED."""
BELOW_BID = Decimal("0.80")
"""The limit as a share of the bid, so the order rests."""
ABOVE_LOW = Decimal("1.05")
"""The order's value at its limit as a multiple of the non-marginable figure."""
MAX_SPREAD = Decimal("0.01")
SNAPSHOT_BATCH = 200
MAX_BATCHES = 40
CENT = Decimal("0.01")
ENDED = {
    OrderStatus.CANCELED,
    OrderStatus.FILLED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.REPLACED,
}
FUNDING_REFUSAL = (403, 40310000)
"""Alpaca's insufficient buying power: HTTP 403, code 40310000 (ADR 0106's Evidence)."""


def paper_trading() -> TradingClient:
    return with_http_timeout(
        TradingClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY], paper=True
        )
    )


def paper_data() -> StockHistoricalDataClient:
    return with_http_timeout(
        StockHistoricalDataClient(
            api_key=os.environ[ENV_API_KEY], secret_key=os.environ[ENV_SECRET_KEY]
        )
    )


def wait_until(done: Callable[[], bool], what: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds} s waiting for {what}")
        time.sleep(0.5)


def figures(trading: TradingClient) -> dict[str, Decimal]:
    """The account's buying-power figures; a missing one is named, and no value is put in a message."""
    held = trading.get_account()
    if not isinstance(held, TradeAccount):
        raise TypeError("the account read is not an account")
    named = {
        "buying_power": held.buying_power,
        "non_marginable_buying_power": held.non_marginable_buying_power,
    }
    missing = [name for name, value in named.items() if value is None]
    if missing:
        raise AssertionError(f"the account report has no {', '.join(missing)}")
    if held.regt_buying_power is not None:
        named["regt_buying_power"] = held.regt_buying_power
    return {name: Decimal(str(value)) for name, value in named.items()}


def refusal_of(error: APIError) -> tuple[int | None, int | None]:
    """The status and Alpaca's code, never the body, which can carry account figures."""
    try:
        code = int(error.code)
    except ValueError, KeyError, TypeError:
        code = None
    return error.status_code, code


def open_orders(trading: TradingClient, symbol: str) -> list[Order]:
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
    assert isinstance(orders, list)
    return orders


def holds(trading: TradingClient, symbol: str) -> bool:
    try:
        trading.get_open_position(symbol)
    except APIError as e:
        if e.status_code == 404:
            return False
        raise
    return True


def find(trading: TradingClient, client_id: str) -> Order | None:
    try:
        order = trading.get_order_by_client_id(client_id)
    except APIError as e:
        if e.status_code == 404:
            return None
        raise
    assert isinstance(order, Order)
    return order


def snapshot_prices(snapshot: Any) -> tuple[Decimal, Decimal, Decimal] | None:
    """(bid, ask, last close), or None when any is missing or the quote is crossed or wide."""
    quote, previous = snapshot.latest_quote, snapshot.previous_daily_bar
    if quote is None or previous is None:
        return None
    bid, ask = Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
    close = Decimal(str(previous.close))
    if bid <= 0 or ask <= bid or (ask - bid) / ask >= MAX_SPREAD:
        return None
    return bid, ask, close


def pick(trading: TradingClient, data: StockHistoricalDataClient) -> tuple[str, str]:
    """The first low and the first ordinary marginable name, in symbol order."""
    assets = trading.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    assert isinstance(assets, list)
    symbols = sorted(
        a.symbol
        for a in assets
        if isinstance(a, Asset)
        and a.tradable
        and a.marginable
        and a.exchange in EXCHANGES
        and a.symbol.isalpha()
    )
    low = high = None
    for start in range(0, min(len(symbols), SNAPSHOT_BATCH * MAX_BATCHES), SNAPSHOT_BATCH):
        batch = symbols[start : start + SNAPSHOT_BATCH]
        snapshots = data.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=batch, feed=DataFeed.SIP)
        )
        assert isinstance(snapshots, dict)
        for symbol in batch:
            prices = None if symbol not in snapshots else snapshot_prices(snapshots[symbol])
            if prices is None:
                continue
            bid, ask, close = prices
            if low is None and bid >= 1 and ask < THRESHOLD and close < THRESHOLD:
                low = symbol
            if high is None and bid >= 5 and close >= 5 and ask <= 100:
                high = symbol
        if low is not None and high is not None:
            return low, high
    raise AssertionError(
        f"no marginable name with a tight quote found with a last close under ${THRESHOLD}, and one "
        f"at $5 or more, in the first {SNAPSHOT_BATCH * MAX_BATCHES} symbols"
    )


def end_order(trading: TradingClient, symbol: str, client_id: str) -> bool:
    """Cancel what is still open of this order, wait for it to end and close any position the name
    holds; whether anything filled."""
    filled = False
    order = find(trading, client_id)
    if order is not None:
        if order.status not in ENDED:
            try:
                trading.cancel_order_by_id(order.id)
            except APIError:
                pass  # filled or ended meanwhile: the read below says which
            order_id = order.id

            def ended() -> bool:
                held = trading.get_order_by_id(order_id)
                return isinstance(held, Order) and held.status in ENDED

            wait_until(ended, f"{symbol}'s order to end", seconds=60.0)
            order = find(trading, client_id)
        filled = order is not None and Decimal(str(order.filled_qty or 0)) > 0
    if holds(trading, symbol):
        trading.close_position(symbol)
        wait_until(lambda: not holds(trading, symbol), f"{symbol}'s position closed", seconds=60.0)
        filled = True
    return filled


def probe(trading: TradingClient, data: StockHistoricalDataClient, symbol: str) -> dict[str, Any]:
    snapshots = data.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=symbol, feed=DataFeed.SIP)
    )
    assert isinstance(snapshots, dict)
    prices = None if symbol not in snapshots else snapshot_prices(snapshots[symbol])
    if prices is None:
        raise AssertionError(f"{symbol}: no tight quote and last close to price the order from")
    bid, ask, close = prices
    limit = (bid * BELOW_BID).quantize(CENT, rounding=ROUND_DOWN)
    before = figures(trading)
    low = before["non_marginable_buying_power"]
    high = min(before["buying_power"], before.get("regt_buying_power", before["buying_power"]))
    if not low < high:
        raise AssertionError("account A reports no buying power above its non-marginable figure")
    quantity = math.ceil(low * ABOVE_LOW / limit)
    at_limit, at_ask = quantity * limit, quantity * ask
    if not (low < at_limit and at_ask < high):
        raise AssertionError(
            f"{symbol}: no whole quantity puts the order's value between the figures"
        )
    client_id = f"canon-margin-probe-{uuid.uuid4()}"
    request = LimitOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=float(limit),
        client_order_id=client_id,
    )
    refused: tuple[int | None, int | None] | None = None
    result: dict[str, Any] = {"last_close_under_threshold": close < THRESHOLD}
    try:
        try:
            trading.submit_order(request)
        except APIError as e:
            refused = refusal_of(e)
        except Exception as e:  # noqa: BLE001 - a timeout's order may be live; the lookup decides
            result["submit_raised"] = type(e).__name__
        if find(trading, client_id) is not None:
            refused = None  # the venue holds it, whatever the submit raised
        elif refused is None:
            raise AssertionError(f"{symbol}: the submit raised and no order with its ID is found")
        after = figures(trading)
    finally:
        result["filled"] = end_order(trading, symbol, client_id)
    released = figures(trading)
    result["outcome"] = "refused" if refused else "accepted"
    result["refusal"] = refused
    for name in before:
        result[f"{name} change at the order, in order values"] = round(
            (after[name] - before[name]) / at_limit, 2
        )
        result[f"{name} change at the cancel, in order values"] = round(
            (released[name] - after[name]) / at_limit, 2
        )
    return result


def problem(symbol: str, result: dict[str, Any]) -> str | None:
    """Why a probe's result is not evidence about funding, or None."""
    if result["filled"]:
        return f"{symbol}: the resting buy filled; its position was closed"
    refusal = result["refusal"]
    if refusal is not None and tuple(refusal) != FUNDING_REFUSAL:
        status, code = refusal
        return f"{symbol}: refused with HTTP {status}, code {code}, not insufficient buying power"
    return None


@paper_only
def test_paper_margin_probe_which_buying_power_a_buy_is_checked_against() -> None:
    trading, data = paper_trading(), paper_data()
    clock = trading.get_clock()
    if not getattr(clock, "is_open", False):
        raise AssertionError("the margin probe runs in the regular session only")
    names = pick(trading, data)
    for symbol in names:
        if open_orders(trading, symbol) or holds(trading, symbol):
            raise AssertionError(f"{symbol} already has an open order or a position on account A")
    results = [probe(trading, data, symbol) for symbol in names]
    for symbol, result in zip(names, results, strict=True):
        print(f"margin probe: {symbol} {result}")  # shown with -s or -rA; relations only
    for symbol in names:
        if open_orders(trading, symbol) or holds(trading, symbol):
            raise AssertionError(f"{symbol}: an order is still open or a position is still held")
    problems = [
        found
        for symbol, result in zip(names, results, strict=True)
        if (found := problem(symbol, result))
    ]
    if problems:
        raise AssertionError("; ".join(problems))


# -- offline: the pick, the sizing and the clean-up, against fakes of the two clients --------------


def http_error(status: int, code: int, message: str) -> APIError:
    """An `APIError` as alpaca-py raises it (`alpaca/common/exceptions.py:10-29`)."""
    response = SimpleNamespace(status_code=status)
    return APIError(
        json.dumps({"code": code, "message": message}), SimpleNamespace(response=response)
    )


class FakeTrading:
    """Accounts are read in order; `submit` is what the submit does: "accept", "refuse", "lost"
    (raises, but the venue holds it), "gone" (raises, and nothing is held) or "fill"."""

    def __init__(
        self,
        assets: list[Asset],
        figures: list[tuple[str, str]],
        submit: str = "accept",
        refusal: tuple[int, int] = FUNDING_REFUSAL,
        fail_account_read: int | None = None,
    ):
        self.assets, self.figures, self.submit = assets, list(figures), submit
        self.refusal, self.fail_account_read = refusal, fail_account_read
        self.reads = 0
        self.sent: list[LimitOrderRequest] = []
        self.orders: dict[str, Order] = {}
        self.canceled: list[object] = []
        self.position = False
        self.closed: list[str] = []

    def get_all_assets(self, request: GetAssetsRequest) -> list[Asset]:
        return self.assets

    def get_account(self) -> TradeAccount:
        self.reads += 1
        if self.reads == self.fail_account_read:
            raise http_error(500, 50010000, "internal error")
        buying_power, non_marginable = self.figures.pop(0)
        return TradeAccount.model_construct(
            buying_power=buying_power,
            non_marginable_buying_power=non_marginable,
            regt_buying_power=None,
        )

    def submit_order(self, request: LimitOrderRequest) -> Order:
        self.sent.append(request)
        client_id = str(request.client_order_id)
        if self.submit == "refuse":
            raise http_error(*self.refusal, "refused")
        status = OrderStatus.FILLED if self.submit == "fill" else OrderStatus.ACCEPTED
        filled = request.qty if self.submit == "fill" else 0
        order = Order.model_construct(id="o-1", status=status, filled_qty=filled)
        if self.submit != "gone":
            self.orders[client_id] = order
            self.position = self.submit == "fill"
        if self.submit in ("lost", "gone"):
            raise TimeoutError("read timed out")
        return order

    def get_order_by_client_id(self, client_id: str) -> Order:
        if client_id not in self.orders:
            raise http_error(404, 40410000, "order not found")
        return self.orders[client_id]

    def get_order_by_id(self, order_id: object) -> Order:
        (order,) = (o for o in self.orders.values() if o.id == order_id)
        return order

    def cancel_order_by_id(self, order_id: object) -> None:
        order = self.get_order_by_id(order_id)
        if order.status == OrderStatus.FILLED:
            raise http_error(422, 42210000, "order is not cancelable")
        self.canceled.append(order_id)
        order.status = OrderStatus.CANCELED

    def get_open_position(self, symbol: str) -> object:
        if not self.position:
            raise http_error(404, 40410000, "position does not exist")
        return object()

    def close_position(self, symbol: str) -> None:
        self.closed.append(symbol)
        self.position = False


class FakeData:
    def __init__(self, prices: dict[str, tuple[float, float, float]]):
        self.prices = prices

    def get_stock_snapshot(self, request: StockSnapshotRequest) -> dict[str, object]:
        wanted = request.symbol_or_symbols
        symbols = [wanted] if isinstance(wanted, str) else wanted
        return {
            s: SimpleNamespace(
                latest_quote=SimpleNamespace(
                    bid_price=self.prices[s][0], ask_price=self.prices[s][1]
                ),
                previous_daily_bar=SimpleNamespace(close=self.prices[s][2]),
            )
            for s in symbols
            if s in self.prices
        }


def as_trading(fake: FakeTrading) -> TradingClient:
    return cast(TradingClient, fake)


def as_data(fake: FakeData) -> StockHistoricalDataClient:
    return cast(StockHistoricalDataClient, fake)


def an_asset(symbol: str, *, marginable: bool = True, exchange=AssetExchange.NASDAQ) -> Asset:
    return Asset.model_construct(
        symbol=symbol, tradable=True, marginable=marginable, exchange=exchange
    )


FIGURES = [("40000", "10000")] * 3
LOW_NAME = {"EEE": (2.00, 2.01, 2.10)}
HIGH_NAME = {"GGG": (20.00, 20.01, 19.50)}


def test_the_probe_picks_by_last_close_and_quote_in_symbol_order() -> None:
    prices = {
        "AAA": (1.50, 1.51, 1.40),  # not marginable
        "BBB": (0.50, 0.501, 0.50),  # bid under $1.00
        "CCC": (2.00, 2.20, 2.10),  # spread too wide
        "DDD": (2.00, 2.001, 2.00),  # OTC
        "DEF": (2.40, 2.41, 2.60),  # quoted under $2.50, last close above
        "EEE": (2.00, 2.01, 2.10),  # the low name
        "EFG": (2.49, 2.50, 2.40),  # ask exactly $2.50
        "FFF": (4.90, 4.91, 5.10),  # bid under $5
        "FGH": (150.0, 150.1, 150.0),  # ask over $100
        "GGG": (20.00, 20.01, 19.50),  # the ordinary name
    }
    assets = [
        an_asset(
            s,
            marginable=s != "AAA",
            exchange=AssetExchange.OTC if s == "DDD" else AssetExchange.NASDAQ,
        )
        for s in prices
    ] + [an_asset("BR.K")]
    assert pick(as_trading(FakeTrading(assets, [])), as_data(FakeData(prices))) == ("EEE", "GGG")


def test_the_probe_reads_snapshots_in_batches_and_names_a_failed_search() -> None:
    names = [f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(SNAPSHOT_BATCH + 1)]
    prices = {s: (3.00, 3.01, 3.00) for s in names}
    prices[names[-1]] = (2.00, 2.01, 2.00)  # the low name only in the second batch
    prices[names[0]] = (20.00, 20.01, 20.00)
    trading = FakeTrading([an_asset(s) for s in names], [])
    assert pick(as_trading(trading), as_data(FakeData(prices))) == (names[-1], names[0])
    with pytest.raises(AssertionError, match="no marginable name with a tight quote"):
        pick(as_trading(trading), as_data(FakeData({s: (3.00, 3.01, 3.00) for s in names})))


@pytest.mark.parametrize("prices", [LOW_NAME, HIGH_NAME], ids=["low", "high"])
@pytest.mark.parametrize("submit", ["accept", "refuse", "lost"])
def test_the_probe_sizes_between_the_figures_and_ends_the_order(prices, submit) -> None:
    trading = FakeTrading([], FIGURES, submit=submit)
    (symbol,) = prices
    result = probe(as_trading(trading), as_data(FakeData(prices)), symbol)

    (sent,) = trading.sent
    limit, quantity = Decimal(str(sent.limit_price)), Decimal(str(sent.qty))
    bid, ask = Decimal(str(prices[symbol][0])), Decimal(str(prices[symbol][1]))
    assert limit == (bid * BELOW_BID).quantize(CENT, rounding=ROUND_DOWN)
    assert Decimal(10000) < quantity * limit
    assert quantity * ask < Decimal(40000)
    assert result["outcome"] == ("refused" if submit == "refuse" else "accepted")
    assert trading.canceled == ([] if submit == "refuse" else ["o-1"])
    assert result["filled"] is False and trading.closed == []
    assert result["last_close_under_threshold"] is (symbol == "EEE")
    assert problem(symbol, result) is None


def test_a_fill_is_closed_and_fails_the_probe_by_name() -> None:
    trading = FakeTrading([], FIGURES, submit="fill")
    result = probe(as_trading(trading), as_data(FakeData(LOW_NAME)), "EEE")
    assert trading.canceled == [] and trading.closed == ["EEE"]
    assert problem("EEE", result) == "EEE: the resting buy filled; its position was closed"


def test_an_order_is_still_ended_when_a_read_fails_after_the_submit() -> None:
    trading = FakeTrading([], FIGURES, fail_account_read=2)
    with pytest.raises(APIError):
        probe(as_trading(trading), as_data(FakeData(LOW_NAME)), "EEE")
    assert trading.canceled == ["o-1"]


def test_a_submit_whose_order_is_not_found_is_named_and_nothing_is_canceled() -> None:
    trading = FakeTrading([], FIGURES, submit="gone")
    with pytest.raises(AssertionError, match="no order with its ID is found"):
        probe(as_trading(trading), as_data(FakeData(LOW_NAME)), "EEE")
    assert trading.canceled == []


def test_a_refusal_other_than_funding_is_not_evidence_and_keeps_no_body() -> None:
    trading = FakeTrading([], FIGURES, submit="refuse", refusal=(422, 42210000))
    result = probe(as_trading(trading), as_data(FakeData(LOW_NAME)), "EEE")
    assert result["refusal"] == (422, 42210000)
    assert problem("EEE", result) == (
        "EEE: refused with HTTP 422, code 42210000, not insufficient buying power"
    )


@pytest.mark.parametrize(
    ("figures", "message"),
    [
        (("10000", "10000"), "no buying power above its non-marginable figure"),
        (("10200", "10000"), "no whole quantity puts the order's value between the figures"),
    ],
)
def test_the_probe_places_nothing_without_room_and_names_no_figure(figures, message) -> None:
    trading = FakeTrading([], [figures])
    with pytest.raises(AssertionError) as raised:
        probe(as_trading(trading), as_data(FakeData(LOW_NAME)), "EEE")
    assert message in str(raised.value)
    assert "10000" not in str(raised.value) and "10200" not in str(raised.value)
    assert trading.sent == []
