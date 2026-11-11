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
