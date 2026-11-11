---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Alpaca's bars into the local catalog: the node's catalog write-back, through the adapter's data client

## Context and Problem Statement

`canon_trading.backtest.run_backtest` reads bars and instrument definitions from the `ParquetDataCatalog`
in `catalog/`, and nothing wrote Alpaca's bars there: the one backtest ran on bars its test made. How do
a symbol's historical bars from Alpaca get into the catalog, how does a second load over the same range
behave, and what identifies the data a run read?

## Decision Drivers

* ADR 0001: take the path upstream documents for this, and write as little here as it allows.
* The bars a backtest reads must be the ones the adapter delivers to a live node, with the same
  timestamps (`ts_init` the close of the interval, as `canon_trading.backtest` requires).
* Loading twice must not duplicate, refuse or silently change what is stored.

## Considered Options

1. **Catalog write-back.** A `LiveNode` with the catalog in `LiveNodeConfig.catalogs` and the adapter's
   data client; an actor calls `request_bars` with `params={"update_catalog": True}`. The catalog guide
   documents it for historical requests and names "a Python adapter" among the clients it works with.
2. **Fetch, then `write_*`**, as upstream's Databento and CSV how-tos do for files and its FinFeed
   tutorial does for a remote API: the domain objects are written directly.
3. **The streaming writer** (`StreamingConfig`): what a running node processes, instruments included,
   is staged in Feather files and promoted into the catalog, by default when the node closes.

## Decision Outcome

Chosen: **option 1**. It is the documented path for history requested from a data client, and the
catalog does the bookkeeping: a request reads what the catalog covers and asks the client only for the
missing intervals. Option 2 would need that bookkeeping written here, since an overlapping `write_bars`
raises unless `skip_disjoint_check` is set. Option 3 records a run's stream, not a requested range.
Write-back writes an instrument definition only for an instrument request, which the adapter does not
serve, so the definition is written with `write_instruments`.

`canon_trading.bars_to_catalog.load_bars(bar_type, start, end, catalog_path)` does it. Conventions:

* **Window.** The bars stored are those whose `ts_init`, the close of their interval, lies in
  `[start, end]`, both time-zone aware; the adapter makes Alpaca's window match (`canon_alpaca.data`,
  "Bar windows"). The whole window is recorded as covered, so an `end` less than one bar interval, or
  less than 15 minutes (the delay Alpaca's bars reference gives a feed without real-time access),
  before now is refused: a bar not yet formed or published would be missing for good.
* **Bars.** What the adapter serves: `LAST`-price `EXTERNAL` MINUTE, HOUR, DAY or WEEK bars from the
  IEX feed, with Alpaca's default adjustment, `raw` (alpaca-py sends no `adjustment`). A split inside a
  range shows as a price jump. Another feed or adjustment is an adapter change.
* **Files.** `catalog/data/bars/<bar type>/<start>_<end>.parquet`, named for the window's bounds, and
  `catalog/data/equity/<instrument>/…parquet` for the definition, as the guide lays them out (the
  installed `write_instruments` docstring says `data/instruments/`; the catalog wrote `data/equity/`).
* **Re-runs.** A covered range is not fetched again and nothing is written. An overlapping range fetches
  only the uncovered part, into a file of its own; no bar is stored twice. A window with no bar at all
  is not recorded (the Parquet catalog writes no file for an empty response) and is asked for again.
  The definition is written only if the catalog holds none for the instrument: the adapter stamps it
  with its load time and the catalog keeps every version, so it is dated after its bars;
  `run_backtest`, which sets no time bounds, loads it.
* **Failures.** At the pin a failed write-back is only logged and the request succeeds; the guide's
  "Catalog write failures ... fail the request" came with upstream pull request 5494, after the rc8 tag.
  So `load_bars` fails a load when any bar it was delivered lies in a part the catalog still reports
  missing. A failure part-way leaves what was written before it.
* **Data identity.** Per bar type, the catalog offers the covered `ts_init` intervals
  (`get_intervals`), the data files (`query_files`) and the bars (`query_bars`); it has no digest
  method. `catalog_bars` returns the first two and the bar count as `CatalogBars`, which is what
  `run_backtest` reads, since it replays every bar of its bar types. File names are not stable: a gap
  fill adds a file and `consolidate_*` replaces files with one. A content identity is the reader's to
  compute, as the FinFeed tutorial does with a SHA-256 of its records; the trial ledger's record of the
  data a run read (`canon_trading.ledger`) can take `CatalogBars` and add one.
* **Credentials.** The key pair comes from the environment through the adapter, so here `load_bars`
  runs only inside `mise run paper:test` (ADR 0029). Historical bars need no open session.

Loading data is not a strategy evaluation: nothing is ranked, and no trial is recorded.

### Consequences

* Good: little code here; the catalog's coverage rules decide what is fetched; backtest bars are the
  live node's bars.
* Bad: a `LiveNode` per load. Raw prices only. Empty windows are re-requested. A bounded
  `BacktestDataConfig` might not find a definition dated after its bounds (UNVERIFIED). A stored bar is
  never re-fetched, though Alpaca's stream documents `updatedBars` revising a minute bar after it closes
  (whether the historical endpoint changes too is UNVERIFIED; the 15-minute margin is the only guard).

### Confirmation

`tests/test_bars_to_catalog.py` runs offline in `scripts/ci`, on the adapter tests' stand-in venue: a
load writes the bars and the definition and `run_backtest` runs on them; a covered range calls Alpaca
nothing and changes no file; an overlap asks only for the gap; an empty window is not recorded; the
definition is written once; a failed request writes nothing; a failed write-back fails the load; a
request that never answers times out and records nothing; bad windows, a window ending too close to
now among them, build no node.

On paper, 2026-11-10: `mise run paper:test tests/test_paper_bars_to_catalog.py -k bars_to_catalog`
passed. It loaded **AAPL, `AAPL.ALPACA-1-DAY-LAST-EXTERNAL`, `ts_init` from noon New York 2025-01-02 to
noon 2025-04-01: 60 daily bars**, the trading days 2025-01-02 to 2025-03-31, each opening at midnight
New York and stamped 24 hours later (so the "UNVERIFIED" midnight open in `canon_alpaca.data` held for
these bars); a second load over part of the range returned the same `CatalogBars`; `run_backtest` with
`MaCross` ran 60 iterations on them. The same range is in this machine's `catalog/`, in one file.

Since ADR 0055 (2026-11-10) a bars request may name its feed in params; `load_bars` sends only
`update_catalog`, which the data client leaves alone, so the loaded bars are still IEX.

Since 2026-11-11 the data client answers quote requests by the same path, IEX only (ADR 0066). What
write-back does with quotes, with an empty response and with a process killed while it writes is in
ADR 0066's Confirmation: as here, an empty response records nothing; a data file is staged as
`<name>#<n>` and renamed into place, so no partial file is read.

## Evidence

Read 2026-11-10. Upstream's docs are the `latest` pages of https://nautilustrader.io/docs/llms.txt, which
now track the rc9 line, fetched with `curl` as raw markdown, not summarised; where they and the pin could
differ, the installed v2.0.0rc8 was run.

* https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md — "Reading data" (`catalogs`,
  covered and missing intervals, `update_catalog`, "a Python adapter", empty coverage only where the
  backend supports it; record limits and write failures as of the later change below); "Writing data";
  "File naming" (`data/equity/`); "Direct catalog access"; `BacktestDataConfig` (fetched).
* https://nautilustrader.io/docs/md/latest/how_to/stream_parquet_catalog.md — Feather staging,
  instruments staged per class, promotion on close by default (fetched).
* `…/how_to/data_catalog_databento.md`, `…/how_to/loading_external_data.md`,
  `…/tutorials/backtest_orderbook_finfeed.md` — `write_*` loaders; FinFeed's SHA-256 manifest (fetched).
* `…/concepts/actors.md` — `DataActor`, `request_bars`, `on_data_response`, `completed` (fetched).
* https://github.com/nautechsystems/nautilus_trader/pull/5494 — write failures fail the request, merged
  2026-10-31, after the rc8 tag (fetched by the second reader).
* nautilus-trader 2.0.0rc8 as installed: `LiveNodeConfig.catalogs`; `ParquetDataCatalog` docstrings
  (`write_instruments` naming `data/instruments/`, `get_intervals`, `query_files`); write-back took the
  adapter's response, an overlap fetched only the gap, an empty response wrote nothing, and a write to
  an unwritable directory logged "Catalog write-back failed" while the request succeeded (run, offline).
  `scripts/canon outdated` shows v2.0.0rc9 newer than the pin (run).
* https://docs.alpaca.markets/us/reference/stockbars.md — `adjustment` "Default: `raw`"; `feed`; the
  15-minute default `end` without real-time access (fetched). alpaca-py 0.45.0: `adjustment` defaults to
  `None`, which `to_request_fields` drops (bundled).
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md — `updatedBars` (fetched by the
  second reader).
* `mise run paper:test tests/test_paper_bars_to_catalog.py -k bars_to_catalog`, 2026-11-10: 1 passed,
  13 deselected; the output held neither key (run). https://mise.jdx.dev/tasks/task-arguments.html:
  without a spec, mise "forwards extra arguments to the command" (fetched); to the script's last
  command, as a scratch task on mise 2026.11.5 showed (run).
* Second reader, 2026-11-10 (`evidence-verifier`): the mechanism holds. It found the rc8 write-back
  behaviour, the option 2 and 3 descriptions, the unstated docstring disagreement and `updatedBars`;
  all are applied above, with the load's write check the first called for.
