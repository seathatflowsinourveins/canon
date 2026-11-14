"""DuckDB reads NautilusTrader's catalog read-only and keeps nanoseconds exact (ADR 0153)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.catalog_query import read_only

if TYPE_CHECKING:
    from pathlib import Path

# 2025-11-12T15:00:00Z, with non-zero digits below the microsecond in both timestamps.
BASE = 1_762_959_600_000_000_000
MINUTE = 60_000_000_000


def write_catalog(root: Path) -> tuple[list[Bar], Path]:
    """Three bars written by the engine's own catalog; returns them and the one file it wrote."""
    bar_type = BarType.from_str("TEST.ALPACA-1-MINUTE-LAST-EXTERNAL")
    bars = [
        Bar(
            bar_type,
            Price.from_str("10.00"),
            Price.from_str("10.50"),
            Price.from_str("9.90"),
            Price.from_str("10.20"),
            Quantity.from_int(100),
            BASE + i * MINUTE + 123_456_789,
            BASE + i * MINUTE + 123_456_999,
        )
        for i in range(3)
    ]
    root.mkdir(exist_ok=True)
    ParquetDataCatalog(str(root)).write_bars(bars)
    (written,) = root.rglob("*.parquet")
    return bars, written


@pytest.fixture
def catalog(tmp_path: Path) -> tuple[list[Bar], Path]:
    return write_catalog(tmp_path / "catalog")


def test_the_catalog_column_is_nanosecond_utc(catalog: tuple[list[Bar], Path]) -> None:
    _, path = catalog
    con = read_only([path])
    described = con.execute(
        "SELECT column_name, column_type FROM (DESCRIBE SELECT ts_event, ts_init FROM read_parquet($f))",
        {"f": str(path)},
    ).fetchall()
    assert described == [("ts_event", "TIMESTAMPTZ_NS"), ("ts_init", "TIMESTAMPTZ_NS")]


def test_a_microsecond_cast_loses_the_nanoseconds(catalog: tuple[list[Bar], Path]) -> None:
    """The hazard NautilusTrader's catalog page names, shown here so the exact read below means something."""
    bars, path = catalog
    con = read_only([path])
    (row,) = con.execute(
        "SELECT epoch_ns(ts_event::TIMESTAMPTZ) FROM read_parquet($f) ORDER BY ts_event LIMIT 1",
        {"f": str(path)},
    ).fetchall()
    assert row[0] != bars[0].ts_event
    assert row[0] % 1_000 == 0


def test_timestamps_read_back_exact(catalog: tuple[list[Bar], Path]) -> None:
    bars, path = catalog
    con = read_only([path])
    rows = con.execute(
        "SELECT epoch_ns(ts_event), epoch_ns(ts_init) FROM read_parquet($f) ORDER BY ts_event",
        {"f": str(path)},
    ).fetchall()
    assert rows == [(bar.ts_event, bar.ts_init) for bar in bars]


def test_a_write_into_the_catalog_is_refused(catalog: tuple[list[Bar], Path]) -> None:
    _, path = catalog
    root = path.parents[3]
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    con = read_only([path])
    with pytest.raises(duckdb.PermissionException):
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{path}')) TO '{path.parent / 'new.parquet'}'"
        )
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"COPY (SELECT 1 AS x) TO '{root / 'new.csv'}'")
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"ATTACH '{root / 'new.duckdb'}'")
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_an_allowed_file_is_not_overwritten(catalog: tuple[list[Bar], Path]) -> None:
    """Observed, not documented: DuckDB writes a temporary file beside the target, which is refused.

    The securing page says an allow-listed path can be written; this test says whether that still
    holds against an overwrite through COPY, and fails if a DuckDB release lets one through.
    """
    _, path = catalog
    content = path.read_bytes()
    con = read_only([path])
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"COPY (SELECT 1 AS x) TO '{path}' (FORMAT parquet)")
    assert path.read_bytes() == content


def test_a_file_not_named_is_refused(catalog: tuple[list[Bar], Path], tmp_path: Path) -> None:
    _, path = catalog
    other = tmp_path / "other.csv"
    other.write_text("x\n1\n")
    con = read_only([path])
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_csv('{other}')")


def test_the_lock_cannot_be_lifted(catalog: tuple[list[Bar], Path]) -> None:
    _, path = catalog
    con = read_only([path])
    with pytest.raises(duckdb.InvalidInputException):
        con.execute("SET enable_external_access = true")
    with pytest.raises(duckdb.InvalidInputException):
        con.execute("SET lock_configuration = false")
