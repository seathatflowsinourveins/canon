"""Conversions from alpaca-py models to NautilusTrader types, offline, on payloads from Alpaca's docs.

Payload sources are listed in conftest.py. Expected values are read off those payloads, not computed by
the code under test.
"""

import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.timeframe import TimeFrameUnit
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.data import FinePriceError
from canon_alpaca.data import bar_timeframe
from canon_alpaca.data import parse_bar
from canon_alpaca.data import parse_quote
from canon_alpaca.data import parse_trade
from canon_alpaca.providers import credentials_from_env
from canon_alpaca.providers import parse_equity
from nautilus_trader.model import AggressorSide
from nautilus_trader.model import BarType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import TradeId

AAPL = InstrumentId.from_str("AAPL.ALPACA")
TS_INIT = 1_700_000_000_000_000_000


@pytest.fixture
def equity(alpaca) -> Equity:
    return parse_equity(alpaca.asset(), TS_INIT)


def test_asset_becomes_whole_share_usd_equity(equity) -> None:
    assert equity.id == AAPL
    assert equity.raw_symbol.value == "AAPL"
    assert equity.quote_currency.code == "USD"
    assert equity.price_precision == 4
    assert equity.price_increment == Price.from_str("0.0001")
    assert equity.size_precision == 0
    assert equity.size_increment == Quantity.from_int(1)
    assert equity.ts_event == TS_INIT
    assert equity.ts_init == TS_INIT


def test_asset_flags_are_kept_in_info(equity) -> None:
    assert equity.info == {
        "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "name": "Apple Inc. Common Stock",
        "exchange": "NASDAQ",
        "status": "active",
        "tradable": True,
        "fractionable": True,
        "marginable": True,
        "shortable": True,
        "borrow_status": "easy_to_borrow",
        "attributes": [],
        "cusip": None,
    }


def test_dotted_symbol_keeps_its_dot(alpaca) -> None:
    equity = parse_equity(alpaca.asset(symbol="BRK.B"), TS_INIT)
    assert equity.id == InstrumentId.from_str("BRK.B.ALPACA")
    assert equity.id.symbol.value == "BRK.B"


def test_non_equity_asset_is_refused(alpaca) -> None:
    with pytest.raises(ValueError, match="crypto, not us_equity"):
        parse_equity(alpaca.asset(**{"class": "crypto", "symbol": "BTC/USD"}), TS_INIT)


def test_inactive_asset_is_refused_naming_the_id(alpaca) -> None:
    with pytest.raises(ValueError, match="AAPL.ALPACA is inactive, not active"):
        parse_equity(alpaca.asset(status="inactive"), TS_INIT)


def test_asset_not_tradable_on_alpaca_is_kept_for_its_data(alpaca) -> None:
    equity = parse_equity(alpaca.asset(tradable=False), TS_INIT)
    assert equity.info["tradable"] is False


def test_bar_is_stamped_at_open_and_initialised_at_close(alpaca, equity) -> None:
    bar_type = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")
    bar = parse_bar(AlpacaBar("AAPL", alpaca.BAR_AAPL), bar_type, equity)

    assert bar.bar_type == bar_type
    assert bar.open == Price.from_str("178.2600")
    assert bar.high == Price.from_str("178.2600")
    assert bar.low == Price.from_str("178.2100")
    assert bar.close == Price.from_str("178.2100")
    assert bar.volume == Quantity.from_int(1118)
    open_ns = 1_641_200_400_000_000_000  # 2022-01-03T09:00:00Z
    assert bar.ts_event == open_ns
    assert bar.ts_init == open_ns + 3_600_000_000_000


def test_fractional_volume_is_refused_not_rounded(alpaca, equity) -> None:
    bar_type = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
    raw = {**alpaca.BAR_AAPL, "v": 1118.5}
    with pytest.raises(ValueError, match="fractional"):
        parse_bar(AlpacaBar("AAPL", raw), bar_type, equity)


def test_quote_keeps_round_lot_sizes(alpaca, equity) -> None:
    tick = parse_quote(alpaca.quote(), equity, TS_INIT)

    assert tick.instrument_id == AAPL
    assert tick.bid_price == Price.from_str("87.6600")
    assert tick.ask_price == Price.from_str("87.6800")
    assert tick.bid_size == Quantity.from_int(1)
    assert tick.ask_size == Quantity.from_int(4)
    assert tick.ts_event == 1_614_009_105_335_689_000  # alpaca-py's datetime drops the last 322 ns
    assert tick.ts_init == TS_INIT


@pytest.mark.parametrize(
    ("changes", "side"),
    [
        ({"bp": 0.0, "bs": 0}, "bid"),
        ({"ap": 0.0, "as": 0}, "ask"),
        ({"bp": -1.0}, "bid"),
    ],
)
def test_one_sided_quote_is_refused_naming_the_side(alpaca, equity, changes, side) -> None:
    with pytest.raises(ValueError, match=f"has {side} price"):
        parse_quote(alpaca.quote(**changes), equity, TS_INIT)


SIP_BAR = {"o": 747.05, "h": 747.086488, "l": 747.0, "c": 747.08}
"""A consolidated (sip) bar whose high is finer than the instrument's four decimals, as the study's
loader met it for SPY."""


def test_bar_price_finer_than_the_precision_is_rounded_as_upstream_rounds(alpaca, equity) -> None:
    bar_type = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
    bar = parse_bar(AlpacaBar("AAPL", {**alpaca.BAR_AAPL, **SIP_BAR}), bar_type, equity)

    assert bar.high == Price.from_str("747.0865")
    assert bar.open == Price.from_str("747.0500")


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        (747.086488, "747.0865"),
        (1.00025, "1.0002"),  # half to even, as `Instrument.make_price` rounds
        (1.00015, "1.0002"),
        (126.5500, "126.5500"),
        (0.12345, "0.1234"),  # under a dollar, as extreme movers often are
        (0.98765, "0.9876"),
    ],
)
def test_trade_price_finer_than_the_precision_is_rounded_as_upstream_rounds(
    alpaca, equity, value, kept
) -> None:
    tick = parse_trade(alpaca.trade(p=value), equity, TS_INIT)

    assert tick.price == Price.from_str(kept)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_trade_price_that_is_not_a_number_is_still_refused(alpaca, equity, value) -> None:
    with pytest.raises(ValueError, match="trade price .* is not a finite number"):
        parse_trade(alpaca.trade(p=value), equity, TS_INIT)


def test_a_positive_trade_price_that_rounds_to_zero_is_refused(alpaca, equity) -> None:
    with pytest.raises(ValueError, match="trade price 4e-05 rounds to zero at price precision 4"):
        parse_trade(alpaca.trade(p=0.00004), equity, TS_INIT)


def test_a_positive_bar_price_that_rounds_to_zero_is_refused(alpaca, equity) -> None:
    bar_type = BarType.from_str("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL")
    raw = {**alpaca.BAR_AAPL, "o": 0.0002, "h": 0.0003, "l": 0.00003, "c": 0.0002}
    with pytest.raises(ValueError, match="low 3e-05 rounds to zero"):
        parse_bar(AlpacaBar("AAPL", raw), bar_type, equity)


def test_quote_price_finer_than_the_precision_is_refused_by_name(alpaca, equity) -> None:
    with pytest.raises(
        FinePriceError, match="ask price 747.086488 is finer than price precision 4"
    ):
        parse_quote(alpaca.quote(ap=747.086488), equity, TS_INIT)


def test_sub_penny_price_at_the_precision_is_kept(alpaca, equity) -> None:
    tick = parse_trade(alpaca.trade(p=0.1234), equity, TS_INIT)
    assert tick.price == Price.from_str("0.1234")


def test_trade_has_venue_id_and_no_aggressor(alpaca, equity) -> None:
    tick = parse_trade(alpaca.trade(), equity, TS_INIT)

    assert tick.instrument_id == AAPL
    assert tick.price == Price.from_str("126.5500")
    assert tick.size == Quantity.from_int(1)
    assert tick.aggressor_side == AggressorSide.NO_AGGRESSOR
    assert tick.trade_id == TradeId("96921")
    assert tick.ts_event == 1_614_009_104_208_000_000
    assert tick.ts_init == TS_INIT


@pytest.mark.parametrize(
    ("bar_type", "amount", "unit"),
    [
        ("AAPL.ALPACA-1-MINUTE-LAST-EXTERNAL", 1, TimeFrameUnit.Minute),
        ("AAPL.ALPACA-15-MINUTE-LAST-EXTERNAL", 15, TimeFrameUnit.Minute),
        ("AAPL.ALPACA-4-HOUR-LAST-EXTERNAL", 4, TimeFrameUnit.Hour),
        ("AAPL.ALPACA-1-DAY-LAST-EXTERNAL", 1, TimeFrameUnit.Day),
        ("AAPL.ALPACA-1-WEEK-LAST-EXTERNAL", 1, TimeFrameUnit.Week),
    ],
)
def test_supported_bar_types_map_to_alpaca_timeframes(bar_type, amount, unit) -> None:
    timeframe = bar_timeframe(BarType.from_str(bar_type))
    assert (timeframe.amount, timeframe.unit) == (amount, unit)


@pytest.mark.parametrize(
    ("bar_type", "reason"),
    [
        ("AAPL.ALPACA-1-MINUTE-LAST-INTERNAL", "EXTERNAL bars only"),
        ("AAPL.ALPACA-1-MINUTE-BID-EXTERNAL", "LAST"),
        ("AAPL.ALPACA-1-MONTH-LAST-EXTERNAL", "MINUTE, HOUR, DAY or WEEK"),
        ("AAPL.ALPACA-100-TICK-LAST-EXTERNAL", "MINUTE, HOUR, DAY or WEEK"),
        # NautilusTrader parses these; alpaca-py's TimeFrame refuses them.
        ("AAPL.ALPACA-2-DAY-LAST-EXTERNAL", "amount 1"),
        ("AAPL.ALPACA-2-WEEK-LAST-EXTERNAL", "amount 1"),
    ],
)
def test_unsupported_bar_types_are_refused(bar_type, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        bar_timeframe(BarType.from_str(bar_type))


def test_missing_credentials_name_the_variables(monkeypatch) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_SECRET_KEY, raising=False)
    with pytest.raises(RuntimeError, match=f"{ENV_API_KEY} and {ENV_SECRET_KEY}"):
        credentials_from_env()
