"""Paper fills against a replay's fills for the same minute, and the spread at entry.

Contract: ADR 0010 step 4 (a pick on paper "checks fills, costs and trade counts against the
backtest") in the shape ADR 0089 gives roadmap step 8: each paper fill beside the replay's fill of
the same instrument, side and minute, and the quoted spread when a position was entered, which is
the cost a later study is read against. This module is wiring over two upstreams and computes no
statistic of its own beyond a difference, a mean and a median:

* NautilusTrader v2.0.0rc8, `nautilus_trader.analysis.reporter.ReportProvider.generate_fills_report`
  (`python/nautilus_trader/analysis/reporter.py:111` at the pinned commit `d53849b216e2`): one row
  for each `OrderFilled` event of the orders it is given, the event's own `to_dict()`. A backtest's
  orders (`canon_trading.backtest.BacktestRun.orders`) and a live node's (`Cache.orders`) go through
  the same call, so both sides of the comparison have one shape, upstream's. `QuoteTick.to_dict()`
  gives the quote's (`instrument_id`, `bid_price`, `ask_price`, `ts_event`).
* pandas v3.1.1: `DataFrame.groupby` for the fills of one minute, `DataFrame.merge` (`how="outer"`,
  with `indicator`, which names the side each row came from) for the two sides, and `merge_asof`
  (`direction="backward"`, `by`, `tolerance`; both frames sorted by the key first) for the last
  quote at or before a fill. Read 2026-11-11 in the installed package's own docstrings.

What a minute is: the UTC minute that holds the fill's `ts_event` (`Series.dt.floor("min")`). A
replay's order sent from `on_bar` fills at that bar's close (`canon_trading.backtest`, known
limits), and a minute bar becomes available at its close (its `ts_init`), so a replay's fill is
stamped the first instant of the minute after the bar's; a paper order sent on the same bar fills
some milliseconds into that minute. A paper fill that came a minute later, or never, is not matched: it is listed as
`paper only`, and the replay's as `replay only`, and both are counted, never dropped. Several fills
of one instrument and side in a minute are one row at their quantity-weighted price.

The gap is what paper paid beyond the replay: paper's price less the replay's for a buy, the
replay's less paper's for a sell, so a positive gap is worse on paper on either side. `gap_bps` is
that over the replay's price, in basis points.

The spread at entry: long only, a buy opens (ADR 0083: an order here buys to open or sells what is
held), so each paper buy is an entry. Its quote is the last one of its instrument at or before the
fill and no older than `QUOTE_AGE`; older, or none, and the entry has no spread, which is counted.
A quote whose bid is not below its ask (crossed or locked) or not above zero gives none either.
`half_spread_rate` is half the spread over the mid. `cost_rates` gives its median over the entries
with a quote as the `taker_rate` of upstream's `MakerTakerFeeModel`, the model
`canon_trading.backtest` already builds at zero, at one and at two times (ADR 0083's adoption test
reads a rule at doubled costs). It is a row for a later study and is applied nowhere: no run reads
it until a trial's key can carry a fee rate (ADR 0067), and `calibrated` is false until a record
says which measurement calibrates it.

Known limits:

* A minute is the unit: two paper fills a second apart either side of a minute's end are two rows.
* The replay fills a touched limit and a bar-driven market order at the bar's close, with no
  latency and no partial fill (ADR 0067), so a gap measures paper against that venue, not against
  the market.
* Prices and quantities are floats here. They are compared as measurements, never sent anywhere.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

import pandas as pd
from nautilus_trader.analysis.reporter import ReportProvider

from .backtest import backtest_run_config
from .backtest import run_backtest_config

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Sequence
    from pathlib import Path

    from .ma_cross import MaCross

COLUMNS = ("client_order_id", "instrument_id", "order_side", "last_qty", "last_px", "ts_event")
"""The fields of upstream's fills report the comparison reads, in the order a record keeps them."""
KEYS = ["instrument_id", "order_side", "minute"]
QUOTE_AGE = timedelta(minutes=1)
"""The oldest quote read as the one a fill met: the comparison's own unit, a minute."""
BUY = "BUY"


def _require_values(frame: pd.DataFrame) -> None:
    """Refuse incomplete upstream fill fields before pandas can omit an NA group key or value."""
    for column in COLUMNS:
        missing = frame[column].isna()
        if missing.any():
            rows = frame.index[missing].tolist()
            raise ValueError(f"fill records have missing {column} at rows {rows}")


def _typed(frame: pd.DataFrame) -> pd.DataFrame:
    """`frame` with `COLUMNS` only, numbers as floats and `ts_event` as UTC timestamps, by time."""
    typed = frame.reindex(columns=list(COLUMNS))
    _require_values(typed)
    typed = typed.astype({"client_order_id": "str", "instrument_id": "str", "order_side": "str"})
    typed["last_qty"] = pd.to_numeric(typed["last_qty"]).astype("float64")
    typed["last_px"] = pd.to_numeric(typed["last_px"]).astype("float64")
    typed["ts_event"] = pd.to_datetime(typed["ts_event"], utc=True).astype("datetime64[ns, UTC]")
    _require_values(typed)
    # Upstream's fills report writes a missing ts_event as 0 (`_ns_to_dt(ts or 0)`, reporter.py at
    # the pin), so a fill at the Unix epoch is an undated one, refused like a missing stamp.
    epoch = typed["ts_event"] == pd.Timestamp(0, tz="UTC")
    if epoch.any():
        rows = typed.index[epoch].tolist()
        raise ValueError(
            f"fill records have ts_event at the Unix epoch, upstream's missing stamp, at rows {rows}"
        )
    return typed.sort_values("ts_event", kind="stable").reset_index(drop=True)


def fills_of(orders: Iterable[Any]) -> pd.DataFrame:
    """Upstream's fills report of `orders`, one row a fill event, as the comparison reads it."""
    report = ReportProvider.generate_fills_report(list(orders))
    return _typed(report.reset_index() if len(report) else pd.DataFrame(columns=list(COLUMNS)))


def to_records(fills: pd.DataFrame) -> list[dict[str, Any]]:
    """`fills` as JSON-serialisable rows, `ts_event` in UTC nanoseconds as upstream's event holds it."""
    rows = fills.reindex(columns=list(COLUMNS)).copy()
    rows["ts_event"] = [stamp.value for stamp in rows["ts_event"]]
    return rows.to_dict("records")


def from_records(rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
    """The frame `to_records` wrote, or upstream's `OrderFilled.to_dict()` rows.

    Missing fields and values (including a timestamp decoded as NaT) fail by field and row rather
    than disappearing when the fills are grouped by minute.
    """
    return _typed(pd.DataFrame(list(rows), columns=list(COLUMNS)))


def replay_fills(
    catalog_path: Path,
    strategies: Sequence[MaCross],
    *,
    starting_balances: Sequence[str],
    start: int | None = None,
    end: int | None = None,
) -> pd.DataFrame:
    """The fills of `strategies` replayed over the catalog's bars from `start` to `end`.

    `canon_trading.backtest.backtest_run_config` and `run_backtest_config`, whose refusals are this
    call's, on the simulated venue that module describes. It records nothing: a replay read against
    paper is not a trial (ADR 0010 step 4 is not a significance test).
    """
    config = backtest_run_config(
        catalog_path, strategies, starting_balances=starting_balances, start=start, end=end
    )
    return fills_of(run_backtest_config(config, strategies).orders)


def _by_minute(fills: pd.DataFrame, side: str) -> pd.DataFrame:
    """One row for each instrument, order side and UTC minute: the quantity and the weighted price."""
    fills = _typed(fills)
    rows = fills.assign(
        minute=fills["ts_event"].dt.floor("min"), notional=fills["last_px"] * fills["last_qty"]
    )
    grouped = rows.groupby(KEYS, sort=True, as_index=False).agg(
        qty=("last_qty", "sum"), notional=("notional", "sum")
    )
    grouped[f"{side}_px"] = grouped["notional"] / grouped["qty"]
    return grouped.rename(columns={"qty": f"{side}_qty"}).drop(columns="notional")


def compare(paper: pd.DataFrame, replay: pd.DataFrame) -> pd.DataFrame:
    """Each paper minute beside the replay's, and what was found on one side only (module header)."""
    merged = _by_minute(paper, "paper").merge(
        _by_minute(replay, "replay"), how="outer", on=KEYS, indicator="found"
    )
    merged["found"] = (
        merged["found"]
        .astype("str")
        .map({"both": "both", "left_only": "paper only", "right_only": "replay only"})
    )
    worse = merged["order_side"].map(lambda side: 1.0 if side == BUY else -1.0).astype("float64")
    merged["gap"] = (merged["paper_px"] - merged["replay_px"]) * worse
    merged["gap_bps"] = merged["gap"] / merged["replay_px"] * 1e4
    return merged.sort_values(["minute", "instrument_id", "order_side"]).reset_index(drop=True)


def _number(value: float) -> float | None:
    return None if pd.isna(value) else float(value)


def summarise(compared: pd.DataFrame) -> dict[str, Any]:
    """The counts of `compare`'s rows and the matched rows' gap in basis points; none with no match."""
    found = compared["found"].value_counts()
    matched = compared.loc[compared["found"] == "both", "gap_bps"]
    both, paper_only, replay_only = (
        int(found.get(name, 0)) for name in ("both", "paper only", "replay only")
    )
    return {
        "paper_minutes": both + paper_only,
        "replay_minutes": both + replay_only,
        "matched": both,
        "paper_only": paper_only,
        "replay_only": replay_only,
        "median_gap_bps": _number(matched.median()),
        "mean_gap_bps": _number(matched.mean()),
        "worst_gap_bps": _number(matched.max()),
    }


def spread_at_entry(
    paper: pd.DataFrame, quotes: pd.DataFrame, *, age: timedelta = QUOTE_AGE
) -> pd.DataFrame:
    """Each paper buy with the last quote of its instrument at or before it (module header).

    `quotes` holds upstream's `QuoteTick.to_dict()` fields `instrument_id`, `bid_price`, `ask_price`
    and `ts_event`, in UTC nanoseconds or as timestamps. An entry with no quote within `age`, or
    with one that is crossed, locked or one-sided, keeps its row with no spread.
    """
    entries = paper[paper["order_side"] == BUY].sort_values("ts_event", kind="stable")
    read = quotes.reindex(columns=["instrument_id", "bid_price", "ask_price", "ts_event"]).astype(
        {"instrument_id": "str"}
    )
    for price in ("bid_price", "ask_price"):
        read[price] = pd.to_numeric(read[price]).astype("float64")
    read["ts_event"] = pd.to_datetime(read["ts_event"], utc=True).astype("datetime64[ns, UTC]")
    read = read.sort_values("ts_event", kind="stable").rename(columns={"ts_event": "quoted"})
    met = pd.merge_asof(
        entries,
        read,
        left_on="ts_event",
        right_on="quoted",
        by="instrument_id",
        direction="backward",
        tolerance=age,
    )
    sound = (met["bid_price"] > 0) & (met["bid_price"] < met["ask_price"])
    met["half_spread"] = ((met["ask_price"] - met["bid_price"]) / 2).where(sound)
    met["half_spread_rate"] = met["half_spread"] / ((met["ask_price"] + met["bid_price"]) / 2)
    return met.reset_index(drop=True)


def cost_rates(spreads: pd.DataFrame) -> dict[str, Any]:
    """The median half-spread rate of `spread_at_entry`'s entries, at one and two times.

    A row for a later study's `MakerTakerFeeModel(taker_rate=...)`, applied nowhere and
    `calibrated` false (module header); no rate when no entry met a quote.
    """
    rates = spreads["half_spread_rate"].dropna()
    rate = _number(rates.median())
    return {
        "entries": len(spreads),
        "with_a_quote": len(rates),
        "taker_rate_1x": rate,
        "taker_rate_2x": None if rate is None else 2 * rate,
        "calibrated": False,
    }
