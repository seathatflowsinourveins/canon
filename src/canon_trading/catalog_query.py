"""A DuckDB connection that may read the named files and nothing else: a read-only query tool (ADR 0153).

Contract: DuckDB 2.0's "Securing DuckDB" page (docs/2.0/operations_manual/securing_duckdb/overview.md
in duckdb/duckdb-web at b421c300). With `enable_external_access` false, COPY "cannot read from or write
to files" and ATTACH "cannot attach to a database in a file". `allowed_paths` lists the files still
"allowed to be queried". Once `enable_external_access` is false the allow-list "can no longer be changed",
and `lock_configuration` keeps every other setting as it is. The same page warns that allow-listed paths
"can be written to as well as read from", so the list holds exact files, never a folder of the catalog.
Autoloading is turned off first (its "Autoloading" section), so a locked instance loads nothing.

Read nanosecond columns through `epoch_ns(...)`. NautilusTrader's catalog stores ts_event and ts_init
as `Timestamp(Nanosecond, "UTC")` and warns that SQL readers "can truncate sub-microsecond values"
(docs/concepts/data/catalog.md:12-14 at the pin). DuckDB 2.0 reads such a column as `TIMESTAMPTZ_NS`
at full precision (docs/2.0/sql/data_types/timestamp.md:27). A cast to `TIMESTAMPTZ` keeps microseconds
only, and fetching the column into Python needs pytz (docs/2.0/clients/python/conversion.md:187), which
this environment does not install. `epoch_ns` returns the integer exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def read_only(
    files: Iterable[str | Path], *, extensions: Iterable[str] = ()
) -> duckdb.DuckDBPyConnection:
    """An in-memory connection allowed to read exactly `files`, with every other file refused.

    `extensions` names the bundled extensions to load before the lock (for example `json`); none can
    be loaded afterwards. Pass the files' paths themselves, not a folder or a glob: the allow-list
    matches files, and a folder on it would be writable.
    """
    paths = [str(path) for path in files]
    con = duckdb.connect(":memory:")
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    for name in extensions:
        con.load_extension(name)
    con.execute("SET allowed_paths = $paths", {"paths": paths})
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
