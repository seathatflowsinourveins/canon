# catalog

The local NautilusTrader `ParquetDataCatalog` lives here. Everything in this folder except this file is
ignored by git: market data is never committed.

Upstream's catalog guide builds the path as `Path.cwd() / "catalog"` and passes it to
`ParquetDataCatalog(...)`, so code run from the repo root finds this folder with no configuration. Under
it, upstream lays data out by type and identifier: bars in `catalog/data/bars/<bar type>/`, instrument
definitions in `catalog/data/equity/<instrument>/`, each file named for the `ts_init` range it covers.

What fills it: `canon_trading.bars_to_catalog.load_bars`, which requests a bar type's bars for a range
from Alpaca through the adapter's data client with NautilusTrader's catalog write-back, and writes the
instrument's definition if none is here (ADR 0040). It fetches only what the catalog does not already
cover. It needs the paper key pair, so it runs inside `mise run paper:test`;
`tests/test_paper_bars_to_catalog.py` loads AAPL's daily bars for the first quarter of 2025 here.

One feed a root. The catalog keys bars by bar type, not by Alpaca's feed, so `load_bars` writes the feed
it loaded into `alpaca-feed` in the root and refuses another feed there. This folder holds IEX; SIP bars
go in `catalog/sip/` (ADR 0077).

Source: https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md (read 2026-11-09 and 2026-11-10).

`mise run paper:history <tests|windows> --data trades --symbols AAPL --start <ISO time with zone>
--end <ISO time with zone> --catalog <existing SIP root>` loads individual SIP prints through the
adapter's historical trades fetch and parser, then NautilusTrader's `write_trade_ticks(ticks, start,
end)`. `--symbols-file` accepts one symbol per line with blank lines and `#` comments; `--bars` applies
only to bar loads. The root must already hold the instrument definitions. Trades go under
`data/trades/<instrument>/`, keeping their IDs, sizes and event nanoseconds, with `ts_init = ts_event`.
The `TradeTick` representation drops Alpaca's conditions (`c`), exchange (`x`) and tape (`z`); it does
not filter by those condition flags. `aggressor_side` is `NO_AGGRESSOR`, with no side inferred. These
are the adapter's `parse_trade` semantics and the pinned `TradeTick` fields
(`crates/model/src/data/trade.rs` at `d53849b216e2`).
This is the pinned v2.0.0rc8 catalog's typed writer and coverage API (`docs/concepts/data/catalog.md`,
"Writing data" and "Reading data", and `python/nautilus_trader/persistence/__init__.pyi`, at
`d53849b216e2`, read 2026-11-11).

Both bounds are inclusive, but the loader requires `start < end`; equal bounds are refused. A start
before 1970-01-01 UTC, the catalog's epoch, is refused before lock creation or any request.
The range must end at least 15 minutes before now. Each missing interval is fetched
completely before writing; a rerun skips covered trade intervals. Empty answers and invalid-only
answers write no coverage and are requested again. The adapter excludes canceled and incorrect prints,
keeps corrected prints, and the loader names those exclusions and any rounded prices. More than
100,000 valid prints in one missing interval are loaded in parts, each cut before the last timestamp
its answer reached, so no timestamp is split; more than 100,000 at one timestamp stops the load by
name. It never marks a truncated response as complete. A covered interval is not asked for again, so
a print Alpaca cancels or corrects after the load stays as loaded.

The `.history-load.lock` is shared by bar and trade history loads in one root. The SIP feed marker,
GET-only transport, ADR 0093's 150 data GETs/minute and zero trading GETs, and first-429/refused-feed
stops are shared too. For trades, each actual GET, including every page and SDK 504 retry, starts at
least `60 / 150` seconds after the previous send. Idle time and retry backoffs earn no later burst.
The transport delegates to the existing GET-only timeout/counter adapter and restores it afterward.
Loads into different roots do not share a rate limiter, and the combined rate is UNCALIBRATED. Legacy
catalog writers that do not take this advisory lock must run separately or use other roots. A legacy root
holding IEX data, including this default root, refuses SIP; give SIP its own root.

First evidence to be run by the Claude lane (UNVERIFIED here): one AAPL night, 2026-11-10 21:00 UTC
through 2026-11-11 09:00 UTC, into a separate root holding its definition, then repeat the same command
and verify zero GETs and zero writes. The tests for this path use offline stand-ins.
