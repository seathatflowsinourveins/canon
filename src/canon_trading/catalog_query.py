"""One SELECT over the named files, through a DuckDB that may read only them: a read-only query tool (ADR 0153).

Contracts, DuckDB 2.0's docs in duckdb/duckdb-web at b421c300:

* "Securing DuckDB" (docs/2.0/operations_manual/securing_duckdb/overview.md). With
  `enable_external_access` false, COPY "cannot read from or write to files" and ATTACH "cannot attach to a
  database in a file" (146-147). `allowed_paths` names the files still "ALWAYS allowed to be queried"
  (docs/2.0/configuration/overview.md:185). The allow-list "can no longer be changed" once access is off, and
  `lock_configuration` keeps every setting as it is (175). Autoloading is turned off first.
* The same page says allow-listed paths "can be written to as well as read from" (169), and the files read
  here are exactly those. So the files themselves are guarded by what this module adds: the query must
  parse, by the connection's own `extract_statements` (the Python API reference), as one statement of type
  `StatementType.SELECT`. No connection or relation leaves this function, so nothing can call a writer on
  it. That composition is this repository's, not an upstream guarantee, and tests/test_catalog_query.py
  holds it against COPY, ATTACH, LOAD, SET, PRAGMA, CALL and EXPORT.
* `temp_directory` is a private folder removed on return (docs/2.0/configuration/pragmas.md:600-606). The
  default `.tmp` in the working directory would otherwise stay allowed after the lock.

Read nanosecond columns through `epoch_ns(...)`. NautilusTrader's catalog stores ts_event and ts_init
as `Timestamp(Nanosecond, "UTC")` and warns that SQL readers "can truncate sub-microsecond values"
(docs/concepts/data/catalog.md:12-14 at the pin). DuckDB 2.0 reads such a column as `TIMESTAMPTZ_NS`
at full precision (docs/2.0/sql/data_types/timestamp.md:27). A cast to `TIMESTAMPTZ` keeps microseconds
only, and fetching the column into Python needs pytz (docs/2.0/clients/python/conversion.md:187), which
this environment does not install. `epoch_ns` returns the integer; DuckDB documents it with
microsecond examples only, and the tests show it exact on `TIMESTAMPTZ_NS`.
"""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING
from typing import Any

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from pathlib import Path


def query(
    files: Iterable[str | Path],
    sql: str,
    params: Mapping[str, Any] | None = None,
    *,
    extensions: Iterable[str] = (),
) -> list[tuple[Any, ...]]:
    """The rows of one SELECT that may read exactly `files`; anything else is refused before it runs.

    Pass the files' paths themselves, not a folder or a glob: the allow-list matches files, and a folder
    on it would be writable. `extensions` names bundled extensions to load before the lock (for example
    `json`). Raises PermissionError for a statement that is not one SELECT, and DuckDB's
    PermissionException for a file not named.
    """
    paths = [str(path) for path in files]
    with tempfile.TemporaryDirectory(prefix="canon-duckdb-") as spill:
        con = duckdb.connect(":memory:")
        try:
            statements = con.extract_statements(sql)
            kinds = [statement.type for statement in statements]
            if kinds != [duckdb.StatementType.SELECT]:
                names = ", ".join(kind.name for kind in kinds) or "none"
                raise PermissionError(f"catalog_query runs one SELECT only, not: {names}")
            con.execute("SET autoinstall_known_extensions = false")
            con.execute("SET autoload_known_extensions = false")
            for name in extensions:
                con.load_extension(name)
            con.execute("SET temp_directory = $dir", {"dir": spill})
            con.execute("SET allowed_paths = $paths", {"paths": paths})
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            return con.execute(sql, dict(params or {})).fetchall()
        finally:
            con.close()
