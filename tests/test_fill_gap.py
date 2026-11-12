"""Offline tests of `canon_trading.fill_gap`: paper fills against a replay's, minute by minute, and
the spread at entry (ADR 0089). The replay runs on the generated bars of `conftest.py`; the paper
side is a frame in upstream's fills-report shape, as a window would save it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from nautilus_trader.model import BarType
from nautilus_trader.model import Quantity

from canon_trading.fill_gap import COLUMNS
from canon_trading.fill_gap import compare
from canon_trading.fill_gap import cost_rates
from canon_trading.fill_gap import fills_of
from canon_trading.fill_gap import from_records
from canon_trading.fill_gap import replay_fills
from canon_trading.fill_gap import spread_at_entry
from canon_trading.fill_gap import summarise
from canon_trading.fill_gap import to_records
from canon_trading.ma_cross import MaCross
from canon_trading.ma_cross import MaCrossConfig

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import AlpacaBars

AAPL = "AAPL.ALPACA"


def fill(side: str, px: str, at: str, qty: str = "10", instrument: str = AAPL) -> dict[str, object]:
    return {
        "client_order_id": f"O-{at}-{side}",
        "instrument_id": instrument,
        "order_side": side,
        "last_qty": qty,
        "last_px": px,
        "ts_event": pd.Timestamp(at, tz="UTC").value,
    }


def quote(bid: str, ask: str, at: str, instrument: str = AAPL) -> dict[str, object]:
    return {
        "instrument_id": instrument,
        "bid_price": bid,
        "ask_price": ask,
        "ts_event": pd.Timestamp(at, tz="UTC").value,
    }


def test_a_replay_s_fills_are_upstream_s_fills_report(
    alpaca_bars: AlpacaBars, tmp_path: Path
) -> None:
    alpaca_bars.write(tmp_path)
    bar_type = BarType.from_str(f"{AAPL}-1-DAY-LAST-EXTERNAL")
    config = MaCrossConfig(
        bar_type=bar_type, trade_size=Quantity.from_int(10), fast_period=3, slow_period=8
    )

    fills = replay_fills(tmp_path, [MaCross(config)], starting_balances=["100_000 USD"])

    assert list(fills.columns) == list(COLUMNS)
    assert len(fills) == 4  # the fixture's two round trips (ADR 0067's Confirmation)
    assert list(fills["order_side"]) == ["BUY", "SELL", "BUY", "SELL"]
    assert set(fills["instrument_id"]) == {AAPL}
    assert fills["last_qty"].tolist() == [10.0] * 4
    assert fills["ts_event"].is_monotonic_increasing
    assert str(fills["ts_event"].dt.tz) == "UTC"
    # The JSON shape a window saves reads back as the same frame.
    pd.testing.assert_frame_equal(from_records(to_records(fills)), fills)


def test_no_orders_are_an_empty_frame_of_the_same_columns() -> None:
    assert list(fills_of([]).columns) == list(COLUMNS)
    assert fills_of([]).empty
    assert from_records([]).empty
    assert summarise(compare(fills_of([]), fills_of([]))) == {
        "paper_minutes": 0,
        "replay_minutes": 0,
        "matched": 0,
        "paper_only": 0,
        "replay_only": 0,
        "median_gap_bps": None,
        "mean_gap_bps": None,
        "worst_gap_bps": None,
    }


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("missing", [True, False], ids=["absent", "null"])
def test_an_incomplete_saved_fill_is_refused_by_field_and_row(column: str, missing: bool) -> None:
    good = fill("BUY", "100", "2026-11-11 14:31:01")
    incomplete = fill("BUY", "101", "2026-11-11 14:31:02")
    if missing:
        del incomplete[column]
    else:
        incomplete[column] = None

    with pytest.raises(ValueError, match=rf"fill.*{column}.*rows.*1"):
        from_records([good, incomplete])


@pytest.mark.parametrize("stamp", ["", "NaT", -9223372036854775808])
def test_a_saved_fill_whose_timestamp_decodes_to_nat_is_refused(stamp: str | int) -> None:
    incomplete = fill("BUY", "100", "2026-11-11 14:31:01")
    incomplete["ts_event"] = stamp

    with pytest.raises(ValueError, match="fill.*ts_event.*rows.*0"):
        from_records([incomplete])


@pytest.mark.parametrize("stamp", [0, "1970-01-01T00:00:00Z"])
def test_a_fill_stamped_at_the_epoch_is_refused_as_undated(stamp: str | int) -> None:
    # Upstream's fills report writes a missing ts_event as 0 (`_ns_to_dt(ts or 0)`,
    # nautilus_trader/analysis/reporter.py), so the epoch is how an undated fill arrives.
    undated = fill("BUY", "100", "2026-11-11 14:31:01")
    undated["ts_event"] = stamp

    with pytest.raises(ValueError, match="fill.*ts_event.*epoch.*rows.*0"):
        from_records([undated])


@pytest.mark.parametrize("side", ["paper", "replay"])
def test_a_comparison_refuses_an_undated_fill_instead_of_dropping_it(side: str) -> None:
    incomplete = from_records([fill("BUY", "100", "2026-11-11 14:31:01")])
    incomplete.loc[0, "ts_event"] = pd.NaT
    empty = from_records([])

    with pytest.raises(ValueError, match="fill.*ts_event.*rows.*0"):
        compare(incomplete, empty) if side == "paper" else compare(empty, incomplete)


def test_a_gap_is_what_paper_paid_beyond_the_replay_in_the_same_minute() -> None:
    paper = from_records(
        [
            fill("BUY", "100.05", "2026-11-11 14:31:00.400"),
            fill("SELL", "101.90", "2026-11-11 14:45:07"),
            fill("BUY", "50.00", "2026-11-11 15:00:01", instrument="SPY.ALPACA"),
        ]
    )
    replay = from_records(
        [
            fill("BUY", "100.00", "2026-11-11 14:31:00"),
            fill("SELL", "102.00", "2026-11-11 14:45:00"),
            fill("BUY", "103.00", "2026-11-11 16:10:00"),
        ]
    )

    compared = compare(paper, replay)

    both = compared[compared["found"] == "both"]
    assert both["order_side"].tolist() == ["BUY", "SELL"]
    # A buy filled higher and a sell filled lower are both worse on paper: a positive gap.
    assert both["gap"].tolist() == pytest.approx([0.05, 0.10])
    assert both["gap_bps"].tolist() == pytest.approx([5.0, 0.10 / 102.00 * 1e4])
    assert compared[compared["found"] == "paper only"]["instrument_id"].tolist() == ["SPY.ALPACA"]
    assert compared[compared["found"] == "replay only"]["paper_px"].isna().all()
    assert summarise(compared) == {
        "paper_minutes": 3,
        "replay_minutes": 3,
        "matched": 2,
        "paper_only": 1,
        "replay_only": 1,
        "median_gap_bps": pytest.approx((5.0 + 0.10 / 102.00 * 1e4) / 2),
        "mean_gap_bps": pytest.approx((5.0 + 0.10 / 102.00 * 1e4) / 2),
        "worst_gap_bps": pytest.approx(0.10 / 102.00 * 1e4),
    }


def test_partial_fills_in_one_minute_are_one_price_weighted_by_quantity() -> None:
    paper = from_records(
        [
            fill("BUY", "100.00", "2026-11-11 14:31:01", qty="30"),
            fill("BUY", "100.40", "2026-11-11 14:31:40", qty="10"),
        ]
    )
    replay = from_records([fill("BUY", "100.00", "2026-11-11 14:31:00", qty="40")])

    [row] = compare(paper, replay).to_dict("records")

    assert row["found"] == "both"
    assert (row["paper_qty"], row["replay_qty"]) == (40.0, 40.0)
    assert row["paper_px"] == pytest.approx(100.10)
    assert row["gap_bps"] == pytest.approx(10.0)


def test_a_fill_in_another_minute_or_on_the_other_side_is_not_matched() -> None:
    paper = from_records([fill("BUY", "100.00", "2026-11-11 14:32:00")])
    replay = from_records(
        [
            fill("BUY", "100.00", "2026-11-11 14:31:59.999"),
            fill("SELL", "100.00", "2026-11-11 14:32:00"),
        ]
    )

    compared = compare(paper, replay)

    assert sorted(compared["found"]) == ["paper only", "replay only", "replay only"]
    assert summarise(compared)["matched"] == 0
    assert summarise(compared)["median_gap_bps"] is None


def test_the_spread_at_entry_is_the_last_quote_before_each_buy() -> None:
    paper = from_records(
        [
            fill("BUY", "100.03", "2026-11-11 14:31:00.400"),
            fill("SELL", "100.50", "2026-11-11 14:40:00"),  # an exit: not an entry
            fill("BUY", "100.20", "2026-11-11 15:10:00"),  # its last quote is over a minute old
            fill("BUY", "50.01", "2026-11-11 14:31:00.400", instrument="SPY.ALPACA"),
        ]
    )
    quotes = pd.DataFrame(
        [
            quote("99.90", "100.10", "2026-11-11 14:30:00"),
            quote("100.01", "100.03", "2026-11-11 14:31:00.100"),
            quote("100.02", "100.08", "2026-11-11 14:31:00.900"),  # after the fill
            quote("50.00", "50.02", "2026-11-11 14:30:59", instrument="SPY.ALPACA"),
        ]
    )

    spreads = spread_at_entry(paper, quotes)

    assert len(spreads) == 3  # the three buys
    by_instrument = {(r["instrument_id"], r["last_px"]): r for r in spreads.to_dict("records")}
    aapl = by_instrument[(AAPL, 100.03)]
    assert (aapl["bid_price"], aapl["ask_price"]) == (100.01, 100.03)
    assert aapl["half_spread"] == pytest.approx(0.01)
    assert aapl["half_spread_rate"] == pytest.approx(0.01 / 100.02)
    assert by_instrument[("SPY.ALPACA", 50.01)]["half_spread_rate"] == pytest.approx(0.01 / 50.01)
    assert pd.isna(by_instrument[(AAPL, 100.20)]["half_spread_rate"])
    rates = cost_rates(spreads)
    assert (rates["entries"], rates["with_a_quote"]) == (3, 2)
    median = (0.01 / 100.02 + 0.01 / 50.01) / 2
    assert rates["taker_rate_1x"] == pytest.approx(median)
    assert rates["taker_rate_2x"] == pytest.approx(2 * median)
    assert rates["calibrated"] is False


def test_no_entry_with_a_quote_gives_no_rate() -> None:
    paper = from_records([fill("SELL", "100.50", "2026-11-11 14:40:00")])
    quotes = pd.DataFrame([quote("99.90", "100.10", "2026-11-11 14:30:00")])

    rates = cost_rates(spread_at_entry(paper, quotes))

    assert rates == {
        "entries": 0,
        "with_a_quote": 0,
        "taker_rate_1x": None,
        "taker_rate_2x": None,
        "calibrated": False,
    }
    assert cost_rates(spread_at_entry(paper, pd.DataFrame()))["taker_rate_1x"] is None


def test_a_crossed_or_one_sided_quote_gives_no_spread() -> None:
    paper = from_records([fill("BUY", "100.03", "2026-11-11 14:31:00.400")])
    quotes = pd.DataFrame([quote("100.05", "100.03", "2026-11-11 14:31:00.100")])

    [row] = spread_at_entry(paper, quotes).to_dict("records")

    assert pd.isna(row["half_spread_rate"])
