# catalog

The local NautilusTrader `ParquetDataCatalog` lives here. Everything in this folder except this file is
ignored by git: market data is never committed.

Upstream's catalog guide builds the path as `Path.cwd() / "catalog"` and passes it to
`ParquetDataCatalog(...)`, so code run from the repo root finds this folder with no configuration. Under
it, upstream lays data out by type and instrument, for example `catalog/data/quotes/<instrument>/…parquet`
and `catalog/data/trades/…`.

Source: https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md (read 2026-11-09).
