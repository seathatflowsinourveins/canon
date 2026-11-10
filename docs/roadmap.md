# Roadmap

The steps in the order planned, what "done" means for each, where it stands on `main`, and the record that governs
it. How the pieces fit today is in [architecture.md](architecture.md). The order and the targets are the plan's; where no record sets a
target, the Record column says so. No step has a date; no record gives one.

| # | Step | Done means | State | Record |
|---|---|---|---|---|
| 1 | The harness | Registry and lock, records with evidence and expiry, one check script, landing with a review note, model routing | Done | ADR 0001, 0002, 0005, 0013, 0014, 0017, 0019 |
| 2 | The Alpaca adapter proven on paper | Data, orders, cancels and fills pass through a real node against Alpaca's paper environment | Done for data, market and limit orders, cancels, fills and a rejection, in upstream's data and execution testers; not run: a partial fill through the adapter, `load_all` at full scale | ADR 0030, 0036 |
| 3 | Strategy evaluation | Every backtest a trial in the ledger, the returns matrix, the deflated Sharpe gate at 0.95 | Landed and tested on generated bars and series. A full review found defects: those that let a ledger that should be rejected pass are fixed (one shared ledger, a fixed sample per study, the data each trial read, a key of what this repo sets, V tested within rounding; ADR 0041). Stops that never clear or crash, and smaller defects, are not yet fixed. **No ranking is to be relied on until they are** | ADR 0010, 0031, 0035, 0041 |
| 4 | Order management | Cancel-all, stop orders, modify, the account query, fill history at start-up, request timeouts | Cancel-all, stop and stop-limit orders, and modify by Alpaca's replace landed; the account is read at connect only; fill history is decided (ADR 0024) but not written; every alpaca-py REST request has a timeout, through a requests transport adapter on alpaca-py's private session, since alpaca-py offers none; on paper it waits for the next regular session | ADR 0024, 0036, 0039 |
| 5 | Real market data in the local catalog | Alpaca's bars and instruments written to `catalog/` | Done: `load_bars` requests a bar type's bars through the adapter's data client with the node's catalog write-back and writes the instrument; on paper it loaded 60 AAPL daily bars (2025-01-02 to 2025-03-31) and `run_backtest` ran on them. Raw IEX bars only, the adapter's feed and adjustment | ADR 0040 |
| 6 | Extended hours, and a whole session on paper | Orders in the extended and overnight sessions; a strategy running a whole session on paper under the engine's own risk limits, around the clock on weekdays. This target is intent: no record sets it yet | Begun: `ema_cross_paper_node` runs upstream's `EmaCross` on paper in real time under the risk engine's limits (50 submits a minute); its paper test bounds it to one order a run. `EmaCross` has no position check, so it suits that bounded check and not an unattended run. A limit order submitted with `params={"extended_hours": True}` is sent eligible for the extended and overnight sessions, and passed on paper in after-hours; the data client still streams IEX only, and the overnight feed is not chosen | ADR 0037, 0038; ADR 0036 records why stops are never sent in extended hours; nothing yet for a whole session |
| 7 | Real strategy variants ranked through the gate | Variants of real strategies recorded as trials and ranked by `rank` | Not started: the one strategy, `MaCross`, is there to exercise the plumbing | ADR 0010 |
| 8 | A passing variant on paper beside the incumbent | The pick runs beside the incumbent on separate paper accounts, its fills, costs and trade counts compared with the backtest | Not started | ADR 0010, step 4 |

Where each state comes from: step 2 from the test tables in `packages/canon-alpaca/README.md`; step 3
from `README.md` ("What is not here yet") and the module headers in `src/canon_trading/`; step 4 from
the package README's known limits and `execution.py`; steps 5 to 7 from `README.md`, `backtest.py`,
`ma_cross.py`, `paper_node.py`, ADR 0036 and ADR 0037.

## Not planned

Live trading. No record plans it. Agents work on paper only, and going live is a human action;
ADR 0009 keeps trading on paper at least until NautilusTrader v2.0.0 stable, a condition, not a plan.

## Open questions

* **Overfitting probability.** Its proposed role is a diagnostic beside the gate; no implementation is
  adopted until one passes this repo's tests (ADR 0027 and ADR 0032, both `proposed`).
* **A trial that never traded, or a configuration that never runs to the end**, stops every ranking of
  its study; what value it should count as is undecided (ADR 0010, ADR 0035).
* **One paper account or several.** ADR 0010 step 4 runs the pick and the incumbent on separate paper
  accounts; one paper account serves every session on this machine today (`mise.toml`, `paper:test`).
* **Reconciliation on a shared account.** Inferred fills from the last 500 orders once netted to a short
  that Alpaca did not hold; fill reports keyed by `execution_id`, or a flat position report, would address
  it, and neither is written (package README; ADR 0024). Option 3 of ADR 0024 replaces the generic `get`
  when an alpaca-py release carries the call.
* **Re-pinning NautilusTrader at v2.0.0 stable**, which ADR 0009 requires and ties paper only to.
* **The price increment.** The adapter uses 0.0001 for every US equity; ADR 0030 asks the owner to
  ratify it.
* **The sandbox.** Part of it is adopted, and nothing is on until a trial costs no lane a stop
  (ADR 0025).
* **A deny rule for reading the keychain** with `security find-generic-password`, left to the owner
  (ADR 0029).
* **An OpenTelemetry backend** (ADR 0007) and **an eval of this repo's own** for model routing
  (ADR 0005).
