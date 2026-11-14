---
status: accepted
date: 2026-11-14
decision-makers: the lead (one embedded engine chosen by measurement, 2026-11-14, as the deputy relays it); master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-12
---

# DuckDB reads the catalog and the run reports read-only, with nanoseconds exact; chosen over Polars by measurement

## Context and Problem Statement

Questions about the Parquet we hold (what a catalog root covers, what one day of the market's minute bars
says, what the run reports add up to) get a script each. Two scout reports of 2026-11-14 converge on an
embedded engine reading Parquet in place. The lead asked for one engine, DuckDB or Polars, chosen by a
measurement on our own data: read-only, nanoseconds exact, no pyarrow in the root environment (ADR 0108).

## Decision Drivers

* It must never write the catalog, the ledger or a run folder, and a write it is asked to make is refused.
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

Chosen: **option 1, DuckDB**, run only through `canon_trading.catalog_query.query(files, sql, params)`.
It returns rows and lets no connection out, and it holds the read-only term in two layers:

1. DuckDB's documented lockdown, which refuses every file but the named ones: autoload off, `temp_directory`
   a private folder, `allowed_paths` the exact files, `enable_external_access` false, `lock_configuration` true.
2. A gate of this repository's own on DuckDB's parser: the query runs only if `extract_statements` types it
   as one `SELECT`.

This corrects the brief: no DuckDB setting makes the files being read refuse a write. The securing page
says allow-listed paths "can be written to as well as read from", and the second reader overwrote a
catalog file in place with `COPY ... (USE_TMP_FILE false)` and found the default `.tmp` folder still open.
Without the gate, six of the test's seventeen refused statements get through, that COPY and LOAD among them.

DuckDB is still the choice. Its lockdown covers all but the named files, and its own parser types the
statement. Polars documents no refusal of any write, in its 22 I/O and lazy user-guide pages or its API.
DuckDB reads row counts from footers (`parquet_file_metadata`). Polars' footer readers give the schema and,
experimentally, custom metadata, but no count, so its grid of the whole root would read forbidden rows.

This record measured DuckDB against Polars itself: unlocked the two ran about equal (0.72 s against
1.00 s for one day's file grid, 0.93 s against 0.95 s for one day's bars) and the read-only lock cost
DuckDB 1.5 to 2.2 s, with the runs not niced and reading the sim lane's working catalog in place rather than
a snapshot, so DuckDB was chosen for its lockdown and its footer row counts, not for speed.
The folder holds one week of minute bars, about 12,900 instruments (24,767 files, 349 MB). Each engine
and read ran in a fresh process, four rounds in alternating order, at a one-minute load average of 16
to 32. Medians, with each engine's first run in brackets:

| Read | DuckDB, locked | DuckDB, unlocked | Polars |
|---|---|---|---|
| Grid of one day's 11,938 files | 2.88 s (3.51), 124 MB | 0.72 s, 116 MB | 1.00 s (1.15), 181 MB |
| Grid of all 24,767 files, footers only | 7.15 s (7.27), 185 MB | not run | not possible without rows |
| One day of the market's minute bars | 2.46 s (2.60), 214 MB | 0.93 s, 221 MB | 0.95 s (1.04), 186 MB |
| Six run summaries joined to order counts | 0.17 s, 99 MB | 0.16 s, 99 MB | 0.21 s, 93 MB |

All three gave the same digest for each read. Three runs in the shipped form, with the private
`temp_directory`, gave 2.92 s and 2.88 s at a load of 32 to 35; the gate's parse is not timed.

The evidence also corrects the brief's premise that a naive read truncates. DuckDB 2.0 reads the columns
as `TIMESTAMPTZ_NS` at full precision. A cast to `TIMESTAMPTZ` rounds (…123456789 to …123457000), a plain
fetch into Python raises without pytz, and Polars' `to_list()` truncates (…123456). `epoch_ns(...)` and
Polars' `dt.epoch("ns")` are exact; DuckDB documents `epoch_ns` with microsecond examples only.

Polars stays a `watch` entry. Not adopted: a server (3), a daemon plus ingest (QuestDB takes Parquet by
conversion, the scout; `UNVERIFIED`); a table format (4), a metadata layer or a rewrite of what the
catalog's consolidation maintains; a GPU engine (5), NVIDIA only (Polars' GPU page; cuDF and Sirius per the
scout, `UNVERIFIED`) on a machine with Apple's GPU. ArcticDB is BSL 1.1. Option 6 is what this replaces.

### Consequences

* Good: one SQL reader over Parquet and JSON in place that runs only a SELECT, with nanoseconds exact. The
  lock gains only `duckdb`, whose PyPI metadata requires nothing (pyarrow is in its `all` extra).
* Bad: the files' safety rests on the gate, which is ours; a future statement typed SELECT that writes
  would pass it, and the seventeen statements tested are what is watched. Extensions linked into the wheel
  still load after the lock (`LOAD json`, a second reader); only the gate refuses LOAD. A folder cannot be
  allow-listed without becoming writable. Spill under memory pressure, to the private folder, is `UNVERIFIED`.
* No CLI and no task: DuckDB's docs give neither for this use, and one function is the smallest form.

### Confirmation

`tests/test_catalog_query.py`, on a catalog the engine wrote, checks four things. The column is
`TIMESTAMPTZ_NS`, a microsecond cast loses the nanoseconds, and `epoch_ns` reads `ts_event` and `ts_init`
back exact. Seventeen statements are refused with the tree's content hashes unchanged: COPY in both forms,
ATTACH, EXPORT, LOAD, INSTALL, SET, RESET, PRAGMA, CALL, EXPLAIN ANALYZE, a second statement, and `query()`
wrapping a COPY. A file not named is refused. The working directory's `.tmp` is neither readable nor allowed.

## Evidence

Read 2026-11-14.

* https://github.com/duckdb/duckdb-python/releases/tag/v2.0.0 and https://github.com/duckdb/duckdb/releases/tag/v2.0.0
  — Python ≥ 3.11, results stream, Arrow export moved; engine 2.0.0, Parquet async I/O for local files —
  fetched (gh api); the engine's 844-line list was searched, not read whole.
* https://pypi.org/pypi/duckdb/2.0.0/json and https://pypi.org/pypi/polars/2.1.0/json — MIT; DuckDB requires
  nothing (pyarrow in `all`), Polars only `polars-runtime-32==2.1.0` (pyarrow in extras) — fetched.
* https://github.com/duckdb/duckdb-web/blob/b421c300096722cf2be64d37a35cc5520a3ceb75/docs/2.0/operations_manual/securing_duckdb/overview.md
  — COPY and ATTACH refused (146-147), "can be written to as well as read from" (169), the order and the
  lock (175), defense in depth, not comprehensive (37, 97-102) — fetched.
* the same commit: `configuration/overview.md:184-185, 225` (`allowed_paths`, `enable_external_access`);
  `configuration/pragmas.md:600-606` (`temp_directory`); `clients/python/reference/index.md`
  (`extract_statements`, `StatementType`); `clients/python/dbapi.md:79-95` (`read_only` is for a database
  file; probed, a read-only file database still allows COPY TO a file) — fetched, run.
* the same commit: `sql/data_types/timestamp.md:27` (`TIMESTAMPTZ_NS`, new in 2.0), `sql/functions/timestamp.md:172-178`
  and `timestamptz.md:249-255` (`epoch_ns`, microsecond examples only), `clients/python/conversion.md:187`
  (pytz), `data/parquet/metadata.md:99-120` (`parquet_file_metadata`) — fetched.
* `nautilus-trader` at the pin, `docs/concepts/data/catalog.md:12-14` — the truncation warning — resolved
  (`scripts/canon src nautilus-trader`).
* pola-rs/polars at `7f902bd3` (py-2.1.0): `py-polars/src/polars/expr/datetime.py:1765` (`dt.epoch`), 22 user-guide
  pages under `io/` and `lazy/` (no read-only means; `lazy/gpu.md:4`, NVIDIA GPUs), `read_parquet_metadata`
  (custom metadata, experimental) and `read_parquet_schema` — fetched and run.
* https://raw.githubusercontent.com/man-group/ArcticDB/master/LICENSE.txt — Business Source License 1.1 — fetched.
* `/code-review` high, 2026-11-14: an allowed file overwritten by `USE_TMP_FILE false`, the open `.tmp`, LOAD
  after the lock, ATTACH of a named file; reproduced as failing tests, fixed by the gate and the private folder.
* evidence-verifier, 2026-11-14: corrected the header's citation, the `epoch_ns` source, the spill claim, the
  load range, the lock's cost and the footer-reader sentence, and added the defense-in-depth caveat.
* The two scout reports of 2026-11-14 in the lead's archive (outside the repository) — read; their
  ClickHouse, QuestDB, Delta, Iceberg, Lance, cuDF and Sirius lines are `UNVERIFIED` here.
* The measurement, its scripts and per-run results are outside the repository with the lane's report;
  the folder's schema was probed in a scratch environment before the registry entries existed.
