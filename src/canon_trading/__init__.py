"""Strategies and node configuration for the Canon trading stack.

Upstream contract: NautilusTrader keeps node configuration and strategies outside the adapter
package (examples/live/_template/README.md at the pinned commit). The adapter lives in
packages/canon-alpaca.

* `ma_cross` - `MaCross`, a moving-average cross on bars, there to exercise the plumbing
* `backtest` - `run_backtest`, strategies over their bars from a `ParquetDataCatalog` on a simulated
  Alpaca venue

Nothing here places live orders. See docs/adr/0009-trading-foundation.md.
"""
