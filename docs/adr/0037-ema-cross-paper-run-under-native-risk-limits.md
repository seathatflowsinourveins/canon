---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# A real-time paper run of upstream's built-in `EmaCross` through the Alpaca adapter, under native risk limits

## Context and Problem Statement

The owner asked (2026-11-10) for one of NautilusTrader's own strategies to trade end to end on paper, on
native features, capped at 200 orders a minute. No strategy had traded through the adapter in real time
(ADR 0030, ADR 0036). Which built-in, and what does the run show? The risk limits are ADR 0049's.

## Decision Drivers

* Upstream's code over ours (ADR 0001).
* Paper only, one share an order, a position never over one share, the account left as found (ADR 0009).

## Considered Options

The built-in strategies the pin configures from Python: 1. `EmaCross`; 2. `GridMarketMaker`; 3.
`HurstVpinDirectional`, `CompositeMarketMaker`, `DeltaNeutralVol`, the actor `BookImbalanceActor`. The
testers `ExecTester` and `DataTester` test the adapter (ADR 0036) and decide nothing.

## Decision Outcome

Chosen: **option 1**, the only one the adapter can run. `EmaCross` keeps fast and slow EMAs (10 and 50,
upstream's defaults) of the quote mid and sends one market order of `trade_size` on each cross, a buy
when the fast crosses above, a sell when below. Option 2 sends every order post-only at prices to four
decimals; the adapter denies both (ADR 0036). Of option 3, `CompositeMarketMaker` also posts
post-only, `HurstVpinDirectional` needs trade aggressor sides (the adapter sends none), and the others
need options or order book deltas. `canon_trading.paper_node.ema_cross_paper_node` builds the node: the
adapter's clients, reconciliation on, `EmaCross` added by `add_builtin_strategy` at one share, and the
risk engine on with `risk_limits`: 50 submits and 25 modifies a minute and $1,000 an order, from Alpaca's
200 calls a minute (ADR 0049, split from this record on 2026-11-10).

**The paper test's own budget**, `1/00:05:00`, is a property of `tests/test_paper_ema_cross.py`, not the
ceiling: one order reaches Alpaca, the engine denies the rest; without it the first run built a 4-share
short. The order may be a sell, a one-share short. Independently of the throttle, the test reads the
position before the start, every 50 ms and at stop, and fails over one share; after the node stops it
closes what the run opened, outside the node's budget, in a `finally`, failing on whatever is left. A
probe, a one-share sell limit a cent over the cap, must be denied with `max=1000.00 USD`.

**What the run is.** An end-to-end check of the stack on paper: quotes in, a built-in strategy deciding in
real time, orders through the risk engine, the adapter and Alpaca, fills back to the strategy. Not a
strategy evaluation: ADR 0010 governs that, and nothing here ranks or recommends one. `EmaCross` has no
position check and assumes its orders alternate, so on a venue that refuses the opposite side while an
order is open (Alpaca's paper did, observed; no page read documents the rule) it builds a one-sided
position: as shipped, fit for this bounded check, not for an unattended paper run.

**The repo's `MaCross`** is not covered by it. `MaCross` decides on bars with simple averages, is long or
flat, enters only with no position of its own, so a refused order cannot stack one, and flattens on
stop. `EmaCross` decides on quotes, alternates with no position check, can go short and keeps its
position on stop. The backtest stays on `MaCross`; `EmaCross` takes no bars. `MaCross` still does not
track its working orders, which matters before it runs on a venue.

### Consequences

* Good: upstream's strategy trades through the adapter in real time, under upstream's throttle and cap.
* Bad: one order end to end, not sustained trading.

### Confirmation

Offline, in `scripts/ci` (`tests/test_risk_limits.py`, `BacktestEngine`, a simulated `ALPACA` cash venue,
the same `risk_limits`): `1/00:05:00` passes one of three; `EmaCross` sends one share an order,
alternating. The limits' own offline checks are ADR 0049's.

On paper, 2026-11-10, SPY, `mise run paper:test`, in the regular session. In every run, start-up
reconciliation inferred fills from the account's earlier filled SPY orders and held a position
`LONG 10 SPY.ALPACA-EXTERNAL` that Alpaca does not hold (it reported none); the test's checks read the
strategy's own position and Alpaca's, not that one. Whether it let the risk engine pass the cash
account's sells is UNVERIFIED; it is the adapter's to resolve.

* 14:54 New York, `50/00:01:00`, no run budget: the averages crossed 8 times in 1.4 s on IEX quotes.
  Alpaca took 1.0 to 1.9 s to fill each sell and refused each buy meanwhile, 403 "potential wash trade
  detected. use complex orders". Four sells filled at $749.16 to $749.18, a 4-share short. The position
  check then ran only until the first fill, so the test passed; it now also reads at stop. The clean-up
  flattened the account and found no open SPY order and no SPY position.
* 15:42, `1/00:05:00`, 14 s: the probe denied; 7 orders, 6 denied `RATE_LIMIT_EXCEEDED` in the node, 1
  sent: a market sell of one share filled at $749.33 in 0.7 s, its events reaching the strategy; one
  share short at peak, closed by the clean-up, nothing left. Passed. 15:48: 2 orders, a buy filled at
  $749.78; then the whole task, the adapter's six paper tests and this one: 7 passed. 15:53, after
  review: 4 orders, 3 denied for the rate, 1 filled; passed.

After review (2026-11-10), ADR 0044 gives this node the position cap and the execution engine's scoped
reconciliation and runtime checks; the paper test now skips outside the regular session and fails on any
position the node holds that is not the strategy's. When the cap trips it stops the node and nothing
flattens: `EmaCross` does not on stop and its config exposes no exit setting, so the position stays at
Alpaca for the caller to close, as the paper test's clean-up does (offline in
`tests/test_session_window.py`, after a second reader's review, 2026-11-10).

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* `crates/trading/src/examples/strategies/{ema_cross,grid_mm}/`: data, orders, post-only,
  `add_builtin_strategy` from Python (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/execution/index.md, "Risk engine" (fetched, raw).
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: builder, reconciliation (raw).
* nautilus-trader 2.0.0rc8 in `.venv`, `trading/__init__.pyi`, `live/__init__.pyi` (`bundled`).
* The offline tests and the paper runs (run).
* `crates/trading/src/examples/strategies/ema_cross/{config,strategy}.rs` at the pin: `on_stop` only
  unsubscribes; the Python config takes no base exit settings (fetched, raw).
* Second reader (`evidence-verifier`), 2026-11-10: corrected the option list (the testers), option 3's
  reasons, the in-flight cap, the fill times and "per account"; found the reconciled phantom position and
  the MCP page. All applied (the in-flight cap, "per account" and the MCP page now in ADR 0049). It
  noted v2.0.0rc9 is tagged, with no change to these claims found.
* The risk limits, Alpaca's limit, their offline checks and evidence, moved to ADR 0049 on 2026-11-10,
  with no change of decision.
