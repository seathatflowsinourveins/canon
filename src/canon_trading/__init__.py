"""Strategies and node configuration for the Canon trading stack.

Upstream contract: NautilusTrader keeps node configuration and strategies outside the adapter
package (examples/live/_template/README.md at the pinned commit). The adapter lives in
packages/canon-alpaca.

* `ma_cross` - `MaCross`, a moving-average cross on bars, there to exercise the plumbing
* `backtest` - `run_backtest`, strategies over their bars from a `ParquetDataCatalog` on a simulated
  Alpaca venue
* `bars_to_catalog` - `load_bars`, Alpaca's bars for a range into the `ParquetDataCatalog` through the
  adapter's data client, with NautilusTrader's catalog write-back (ADR 0040)
* `ledger` - `record_trial`, a run meant as a trial recorded in an Optuna study (ADR 0035)
* `returns_matrix` - the daily returns of the ledger's trials on a common range (ADR 0010, step 2)
* `ranking` - `rank`, the pick and its deflated Sharpe ratio against 0.95 (ADR 0010, step 3; ADR 0031)
* `paper_node` - `ema_cross_paper_node`, upstream's built-in `EmaCross` on a paper node under the risk
  engine's native limits (ADR 0037)
* `session_ma_cross` - `SessionMaCross`, `MaCross` guarded for live use, limits outside the session
* `position_cap` - `PositionCap`, a one-share cap on the net position, checked inside the node
* `session_window` - one bounded window of `SessionMaCross` on paper, `mise run paper:session` (ADR 0044)

Nothing here places live orders. See docs/adr/0009-trading-foundation.md.
"""
