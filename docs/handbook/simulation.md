# The simulation practice

How the work is run on NautilusTrader's own engine and simulated venue over recorded market data, what
the venue is set to, and what such a run shows and does not. A run of this kind is one of the two forms
of proof the trading path takes, the other being paper (ADR 0074); when no session is open, the practice
runs end to end here (the owner's word of 2026-11-13). A backtest is read against upstream's simulated
venue with its default settings, its report saved beside it (ADR 0067); the tape runs below turn on
switches that record leaves off, so a tape run names them as its own departure. The sandbox node, which
matches on Alpaca's live data and sends no order to Alpaca, is a separate lane with its own record
(ADR 0070); this page is about runs on recorded data.

Every upstream citation below is at the nautilus-trader pin `d53849b216e2` (v2.0.0rc8), read with
`scripts/canon src nautilus-trader`; paths are relative to that repository. What was not checked against
it is marked UNVERIFIED.

## How a run is made

**The data.** Alpaca's history is loaded GET only by `mise run paper:history` (`canon_trading.history_load`)
into a NautilusTrader `ParquetDataCatalog`, one root per feed, with quotes apart, so feeds never mix:
the consolidated feed's bars and trades in one root and its quotes in another (ADR 0151), and the
overnight feed, `boats`, likewise in two ([catalog/README.md](../../catalog/README.md)). Overnight
history comes only from the overnight feed and ends at least 15 minutes before now (the loader's
`PUBLICATION_DELAY`).

**The high-level API.** `BacktestNode` takes one `BacktestRunConfig`, which holds the venues, the data,
an optional engine configuration, a chunk size and the time bounds (`docs/concepts/backtesting/apis-and-runs.md:96-104`;
`python/nautilus_trader/backtest/__init__.pyi:263-273`). The node is built, has its strategies added
through its run-specific methods, and runs (`apis-and-runs.md:106-148`); an independent run gets a
fresh node, disposed afterwards (`apis-and-runs.md:238-256`). `canon_trading.backtest` follows this for
bars, and refuses a range the catalog does not cover, by name (its module header).

**Streaming chunks.** A `chunk_size` from 1 to 1,000,000 streams the catalog's data in chunks; `None`
loads it all at once (`apis-and-runs.md:155-156`). With the low-level `BacktestEngine`, a run streams by
adding a batch, running with `streaming=True` and clearing the data, then calls `end()` after the last
batch, which flushes the timers and runs the stop handlers (`apis-and-runs.md:74-91`). The low-level API
suits data that is not in a catalog, or components changed between runs on the same data
(`apis-and-runs.md:6-13`); `examples/backtest/` at the pin holds the harness patterns. `canon_trading.backtest`
sets no `chunk_size`, so its runs load their data at once.

**The engine's reports.** The orders, order fills, fills, positions and account reports come from the
engine itself (`docs/concepts/reports.md:15`, `:26-181`; `BacktestNode.generate_*_report`,
`__init__.pyi:174-183`). A run's evidence is those reports and its log, kept beside its report. An error
in a callback during a run need not stop it: upstream's own example logs the error from a nested signal
callback and continues (`examples/backtest/callback_borrow_reentrancy.py:16-20`, though an error in
`on_start` propagates, `:18`), and the engine names the callback in the log, as "Python on_start failed"
(`crates/common/src/python/actor.rs:1037`). So a run's log is read, and any "Python <handler> failed"
line fails the run; no check in the tree reads it yet, so it is not yet built.

## The venue profile

One profile for every run, so runs compare; a run that departs from it names the switch and why. What
`main` builds for a bars backtest is `canon_trading.backtest.simulated_alpaca_venue`: `VENUE_SETTINGS`
(netting, a cash account and an L1 book) with zero maker and taker fees from `FEE_RATES`, and no fill or
latency model set (`src/canon_trading/backtest.py`, its header and `VENUE_SETTINGS`). It passes no
`bar_execution`, so that switch is upstream's resolution of `None`, not read here (UNVERIFIED); the
module's header reads its results as bar fills. A run on recorded quotes and trades sets the switches
below. The profile those runs used on 2026-11-13 is in no tracked file, so each run states its switches
in its own report.

| Switch | What it does at the pin | In a tape run |
|---|---|---|
| `oms_type` | Under netting, with no cached position link for the order, the venue reads the position of the order's own instrument and strategy (`crates/execution/src/matching_engine/mod.rs:5545-5560`) | netting, as the paper node runs |
| `account_type` | On a cash account a sell with no such position is refused as a short sale (`matching_engine/mod.rs:3583-3600`, `:3631-3640`) | cash |
| `book_type` | An L1 book, which quotes, trades or bars update (`docs/concepts/backtesting/fill-models.md:13`); a trade triggers matching (`data-and-venues.md:74`) | L1 |
| `bar_execution` | Bars fill orders only on an L1 venue with it on, and only external time bars; off, bars reach the strategy without filling (`bar-execution.md:30-45`) | off: minute bars rank, they do not fill |
| `trade_execution` | A recorded trade can fill a resting order on the passive side (`trade-execution.md:3-7`) | on |
| `liquidity_consumption` | A fill takes only the displayed size not yet consumed, leaving the rest open (`fill-prices-and-matching.md:165-180`); off, the same displayed size can fill more than one order (`fill-models.md:30-35`) | on |
| `queue_position` | With trade execution, the displayed quantity ahead of each limit order is tracked (`trade-execution.md:115-118`) | on |
| `fill_model` | With none, `DefaultFillModel` uses the recorded book, a touched limit fill-eligible and no probabilistic one-tick slippage, its draws reproducible by `random_seed` (`fill-models.md:21-29`, `:38-52`); a market-style order on an L1 book still fills any residual one tick worse (`fill-prices-and-matching.md:42-44`) | the default |
| `fee_model` | Each venue needs one, an explicit zero-rate model for a frictionless baseline (`apis-and-runs.md:151-153`) | zero maker and taker rates; regulatory fees are not modelled |
| `latency_model` | `StaticLatencyModel` adds a base delay to separate insert, update and cancel delays, and a `LatencyModel` subclass returns each delay itself (`docs/concepts/behavioral_models.md:22`, `:33-34`, `:89-129`); a delayed command is released at the next settlement point (`backtesting/execution-flow.md:97-112`); without one, an order settles at once against the book as it stands (`bar-execution.md:112-113`) | none: zero latency, said so in every report |
| `reject_stop_orders` | Stop orders already in the market are rejected on submission (`crates/backtest/src/config.rs:303-305`) | the default, on; Canon's trails are emulated by the engine, never resting stops at the venue |
| `use_reduce_only` | Reduce-only orders are checked against the position (`matching_engine/mod.rs:3583-3587`) | the default, on: protective sales are reduce-only |

The low-level `BacktestEngine.add_venue` defaults `bar_execution` and `trade_execution` on and
`liquidity_consumption` and `queue_position` off (`__init__.pyi:445-481`); `BacktestVenueConfig` leaves
each switch `None` (`__init__.pyi:342-377`), and what `None` resolves to was not read here: UNVERIFIED. A
latency measured from paper's own records, submit to accepted, is not yet in the profile.

## What a simulation shows

The strategy's own decisions on a recorded tape, on the engine's clock: what was ranked, each entry and
its size, the trail's source and level, every rise of the level, each release and its timing. A fill
against the displayed and printed size, in parts where the size is short, with every part inside its
limit. A level kept across a restart between two runs. A handler error, in the log. Replayed beside a
paper window, each entry and exit agrees or its difference is named one by one: the fill's time, its
price and parts, the trail that followed from them, and the code's commit.

## What a simulation does not show

* The venue's own rules: Alpaca's wash-trade refusal (ADR 0070 names it for the sandbox too), and a
  refusal's end when the order it met is gone; Alpaca's extended-hours order rules.
* The paper venue's fills: paper fills on its own terms and times, with its own partial fills (ADR 0070).
* Latency, unless a model is set; with none, a fill comes at the signal's instant.
* Other participants' answer to our orders, and our own weight in a thin name beyond what liquidity
  consumption models: "Historical data cannot show how a simulated order would have interacted with
  other market participants" (`fill-models.md:3-5`).
* Regulatory fees.
* The fill of a sale of a holding another strategy opened: on a cash account the simulated venue
  refuses it as a short sale before any fill (`matching_engine/mod.rs:3583-3667`, `:5545-5560`), and
  the engine's fallback onto an external long runs only on a fill
  (`docs/concepts/execution/reconciliation.md:305-335`). That sale is shown on paper.
* Whether or when a live venue fills: "Fill and latency models control simulated execution. They do not
  determine whether or when a live venue fills an order" (`docs/concepts/behavioral_models.md:36-37`).

## A run's report

Each report names the commit, the command, the data (feed, kind, span and counts), the venue's switches,
the engine's report counts and the count of handler-error lines, names each difference from paper, and
ends with what a simulation does not show. Results are counts and differences, not merit: a run is not
a trial, and nothing it shows changes a row except through ADR 0083's adoption test ([rows.md](rows.md)).
No account, symbol, quantity or window name enters a tracked file (ADR 0103).
