"""DuckDB reads NautilusTrader's catalog read-only and keeps nanoseconds exact (ADR 0153)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import duckdb
import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading.catalog_query import query

if TYPE_CHECKING:
    from pathlib import Path

# 2025-11-12T15:00:00Z, with non-zero digits below the microsecond in both timestamps.
BASE = 1_762_959_600_000_000_000
MINUTE = 60_000_000_000
REFUSED = (PermissionError, duckdb.Error)


class Catalog:
    """Three bars written by the engine's own catalog under `root`, in the one file `path`."""

    def __init__(self, root: Path) -> None:
        bar_type = BarType.from_str("TEST.ALPACA-1-MINUTE-LAST-EXTERNAL")
        self.bars = [
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
        ParquetDataCatalog(str(root)).write_bars(self.bars)
        self.root = root
        (self.path,) = root.rglob("*.parquet")

    def tree(self) -> dict[str, str]:
        """Every file under the root by relative path, with a hash of its content."""
        return {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "catalog")


def test_the_catalog_column_is_nanosecond_utc(catalog: Catalog) -> None:
    described = query(
        [catalog.path],
        "SELECT column_name, column_type FROM (DESCRIBE SELECT ts_event, ts_init FROM read_parquet($f))",
        {"f": str(catalog.path)},
    )
    assert described == [("ts_event", "TIMESTAMPTZ_NS"), ("ts_init", "TIMESTAMPTZ_NS")]


def test_a_microsecond_cast_loses_the_nanoseconds(catalog: Catalog) -> None:
    """The hazard NautilusTrader's catalog page names, shown so the exact read below means something."""
    ((first,),) = query(
        [catalog.path],
        "SELECT epoch_ns(ts_event::TIMESTAMPTZ) FROM read_parquet($f) ORDER BY ts_event LIMIT 1",
        {"f": str(catalog.path)},
    )
    assert first != catalog.bars[0].ts_event
    assert first % 1_000 == 0


def test_timestamps_read_back_exact(catalog: Catalog) -> None:
    rows = query(
        [catalog.path],
        "SELECT epoch_ns(ts_event), epoch_ns(ts_init) FROM read_parquet($f) ORDER BY ts_event",
        {"f": str(catalog.path)},
    )
    assert rows == [(bar.ts_event, bar.ts_init) for bar in catalog.bars]


@pytest.mark.parametrize(
    "statement",
    [
        "COPY (SELECT 1 AS x) TO '{path}' (FORMAT parquet)",
        "COPY (SELECT 1 AS x) TO '{path}' (FORMAT parquet, USE_TMP_FILE false)",
        "COPY (SELECT * FROM read_parquet('{path}')) TO '{folder}/new.parquet'",
        "COPY (SELECT 1 AS x) TO '{root}/new.csv'",
        "ATTACH '{path}' AS z",
        "ATTACH '{root}/new.duckdb'",
        "EXPORT DATABASE '{root}/export'",
        "LOAD json",
        "INSTALL httpfs",
        "SET temp_directory = '{root}'",
        "RESET allowed_paths",
        "SET enable_external_access = true",
        "PRAGMA version",
        "CALL pragma_version()",
        "EXPLAIN ANALYZE COPY (SELECT 1 AS x) TO '{root}/new.csv'",
        "SELECT 1; COPY (SELECT 1 AS x) TO '{path}' (USE_TMP_FILE false)",
        "SELECT * FROM query('COPY (SELECT 1 AS x) TO ''{path}'' (USE_TMP_FILE false)')",
    ],
)
def test_nothing_but_one_select_runs(catalog: Catalog, statement: str) -> None:
    before = catalog.tree()
    sql = statement.format(path=catalog.path, folder=catalog.path.parent, root=catalog.root)
    with pytest.raises(REFUSED):
        query([catalog.path], sql)
    assert catalog.tree() == before


def test_a_file_not_named_is_refused(catalog: Catalog, tmp_path: Path) -> None:
    other = tmp_path / "other.csv"
    other.write_text("x\n1\n")
    with pytest.raises(duckdb.PermissionException):
        query([catalog.path], f"SELECT * FROM read_csv('{other}')")


def test_the_working_directory_temporary_folder_is_not_open(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckDB's default `.tmp` stays on the allow-list after the lock unless `temp_directory` moves it."""
    work = tmp_path / "work"
    (work / ".tmp").mkdir(parents=True)
    (work / ".tmp" / "secret.csv").write_text("x\n1\n")
    monkeypatch.chdir(work)
    with pytest.raises(duckdb.PermissionException):
        query([catalog.path], "SELECT * FROM read_csv('.tmp/secret.csv')")
    ((allowed,),) = query(
        [catalog.path], "SELECT value FROM duckdb_settings() WHERE name = 'allowed_directories'"
    )
    assert ".tmp" not in str(allowed)
    assert "canon-duckdb-" in str(allowed)
