---
status: accepted
date: 2026-11-14
decision-makers: the lead (one embedded engine chosen by measurement, 2026-11-14, as the deputy relays it); master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-12
---

# DuckDB reads the catalog and the run reports read-only, with nanoseconds exact; chosen over Polars by measurement

## Context and Problem Statement

Questions about the Parquet we already hold are answered by scripts written for each one: what a catalog
root covers, what one day of the market's minute bars says, and what the run reports add up to. Two scout
reports of 2026-11-14 (the lead's archive, condensed from sources each names) converge on an embedded
engine that reads Parquet in place, not a server. The lead asked for one engine, DuckDB or Polars,
chosen by a measurement on our own snapshot, with three terms. The engine stays read-only. It reads
nanosecond timestamps exactly. It brings no pyarrow into the root environment (ADR 0108).

## Decision Drivers

* It must never write the catalog, the ledger or a run folder, by a documented means that refuses a write.
* NautilusTrader stores `ts_event` and `ts_init` as `Timestamp(Nanosecond, "UTC")`. It warns that SQL readers
  mapping them to microsecond `TIMESTAMPTZ` can truncate (`docs/concepts/data/catalog.md:12-14` at the pin).
* A coverage grid must come from file names and footers alone. Some files hold days no run may read (ADR 0096).
* No daemon, no ingest, no rewrite of the catalog; a wheel that is cheap to leave.

## Considered Options

1. DuckDB 2.0.0 (`duckdb/duckdb-python` v2.0.0 on engine v2.0.0), MIT, embedded.
2. Polars py-2.1.0, MIT, embedded.
3. A server database (ClickHouse, QuestDB).
4. A table format over the catalog (Delta Lake, Iceberg, Lance).
5. A GPU engine (cuDF, the Polars GPU engine, Sirius).
6. Keep writing a script for each question.

## Decision Outcome

Chosen: **option 1, DuckDB**, opened only through `canon_trading.catalog_query.read_only`, which runs the
lockdown DuckDB's securing page documents: autoload and autoinstall off, the named extensions loaded,
`allowed_paths` set to the exact files a query reads, `enable_external_access` false, `lock_configuration` true.

What decides it is the read-only term. DuckDB documents a means that refuses a write: COPY "cannot read
from or write to files" and ATTACH cannot open a database file. Polars documents no such means. Neither
its 22 I/O and lazy user-guide pages nor its API offers one, so a Polars reader is read-only only by
convention. DuckDB also reads row counts from footers (`parquet_file_metadata`). Polars' only footer
reader returns custom metadata, marked experimental. So a Polars grid over the whole root would read
rows that no run may read.

The measurement was taken on 2026-11-14, on a snapshot of one week of minute bars for about 12,900
instruments (24,767 files, 349 MB). Each engine and read ran in a fresh process, four rounds, in
alternating order. The machine was under load (load average 16 to 30) and nothing was niced. Medians
are shown, with the cold first run in brackets:

| Read | DuckDB, locked | DuckDB, unlocked | Polars |
|---|---|---|---|
| Grid of one day's 11,938 files | 2.88 s (3.51), 124 MB | 0.72 s, 116 MB | 1.00 s (1.15), 181 MB |
| Grid of all 24,767 files, footers only | 7.15 s (7.27), 185 MB | not run | not possible without rows |
| One day of the market's minute bars | 2.46 s (2.60), 214 MB | 0.93 s, 221 MB | 0.95 s (1.04), 186 MB |
| Six run summaries joined to order counts | 0.17 s, 99 MB | 0.16 s, 99 MB | 0.21 s, 93 MB |

For each read, all three produced the same digest of their output. Unlocked, DuckDB and Polars run
alike; the lockdown costs about 1.5 s on 11,938 files (setting the list, then checking each file against
it), the price of the term, and it is paid.

The evidence corrects the brief's premise that a naive read truncates. DuckDB 2.0 reads the catalog's
columns as `TIMESTAMPTZ_NS` at full precision. The loss comes at a cast to microsecond `TIMESTAMPTZ`,
which rounds (…123456789 becomes …123457000). A plain fetch into Python raises, because pytz is not
installed. Polars truncates when it hands a value to Python (`to_list()` gives …123456). The documented
integer paths are exact in both: DuckDB's `epoch_ns(...)` and Polars' `dt.epoch("ns")`.

Polars is kept as a `watch` entry. Not adopted: a server (3) is a daemon plus ingest, and QuestDB takes
Parquet by conversion (the scout; `UNVERIFIED` here); a table format (4) adds a metadata layer or a rewrite
of what the catalog's own consolidation maintains; a GPU engine (5) needs NVIDIA (Polars' GPU page; cuDF and
Sirius per the scout, `UNVERIFIED`), and this machine's GPU is Apple's. ArcticDB, which the scouts also
read, is BSL 1.1, not open source. Option 6 is what this replaces.

### Consequences

* Good: one SQL reader over Parquet and JSON in place, refused any write, with nanoseconds exact. DuckDB's
  PyPI metadata requires nothing, and the lock gains only `duckdb` (pyarrow is in its `all` extra only).
* Bad: a locked instance cannot spill to disk or load an extension later, and a folder cannot be
  allow-listed without becoming writable. An overwrite of an allowed file was refused only because DuckDB
  writes a temporary file beside it. That is observed, not documented, and a test watches it.
* No CLI and no task: DuckDB's docs give neither for this use, and the helper is the smallest form.

### Confirmation

`tests/test_catalog_query.py`, on a catalog the engine wrote: the column type is `TIMESTAMPTZ_NS`; a
microsecond cast loses the nanoseconds; `epoch_ns` reads `ts_event` and `ts_init` back exact; a COPY or
ATTACH into the catalog is refused and the tree is unchanged; an allowed file is not overwritten; a file
not named is refused; the lock cannot be lifted.

## Evidence

Read 2026-11-14.

* https://github.com/duckdb/duckdb-python/releases/tag/v2.0.0 — Python ≥ 3.11; results stream; Arrow
  export moved to the relational API — fetched (gh api).
* https://github.com/duckdb/duckdb/releases/tag/v2.0.0 — engine 2.0.0, Parquet async I/O for local files —
  fetched (gh api); the 844-line list was searched, not read whole.
* https://pypi.org/pypi/duckdb/2.0.0/json — MIT classifier; no required dependency; pyarrow in the `all` extra — fetched.
* https://pypi.org/pypi/polars/2.1.0/json — requires `polars-runtime-32==2.1.0` only, pyarrow in extras — fetched.
* https://github.com/duckdb/duckdb-web/blob/b421c300096722cf2be64d37a35cc5520a3ceb75/docs/2.0/operations_manual/securing_duckdb/overview.md
  — the lockdown and its order; "can be written to as well as read from" — fetched.
* the same commit, `docs/2.0/clients/python/dbapi.md:79-95` — `read_only` is for a database file. Probed:
  an in-memory database refuses it, and a read-only file database still allows COPY TO a file — fetched, run.
* the same commit, `docs/2.0/sql/data_types/timestamp.md:27` (`TIMESTAMPTZ_NS` keeps nanoseconds, new in 2.0),
  `docs/2.0/sql/functions/timestamp.md:172-178` and `timestamptz.md:249-255` (`epoch_ns`),
  `docs/2.0/clients/python/conversion.md:187` (pytz), `docs/2.0/data/parquet/metadata.md:99-120` — fetched.
* `nautilus-trader` at the pin, `docs/concepts/data/catalog.md:12-14` — the truncation warning — resolved
  (`scripts/canon src nautilus-trader`).
* pola-rs/polars at `7f902bd3` (py-2.1.0): `py-polars/src/polars/expr/datetime.py:1765` (`dt.epoch`), 22 user-guide
  pages under `io/` and `lazy/` (no read-only means; `lazy/gpu.md:4`, NVIDIA GPUs), `read_parquet_metadata`'s
  docstring (custom metadata, experimental) — fetched and run.
* https://raw.githubusercontent.com/man-group/ArcticDB/master/LICENSE.txt — Business Source License 1.1 — fetched.
* The two scout reports of 2026-11-14 in the lead's archive (outside the repository) — read; their
  ClickHouse, QuestDB, Delta, Iceberg, Lance, cuDF and Sirius lines are `UNVERIFIED` here.
* The measurement, its scripts and per-run results are outside the repository with the lane's report.
  The snapshot's schema was probed in a scratch environment before the registry entries existed.
